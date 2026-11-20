"""J4.4 第 4 段：软变化（“先放放、暂缓、可能”这类说得不干脆的话）L5 怎么写。例子都是编的；结论是编的 soft.db，不调模型。

照 J4.5 的“交给 L5”（javmem.soft.rules）和 J4.4 做法第 4 段：第一次写账时，候选那一处会改到组里以前说的，先挡住
（自己一行、待确认 soft_change），旧的照旧；判了（模型的结论存在 soft.db、主人答六选一、30 天没人管）照 rules.action
执行，记进那条待确认、写一行 audit_log；以后重写窗口、整本重建都照记下的办，不再找 soft.db。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import World
from test_relcheck import BANK, BROKER, NOTE, relation
from test_relations_newold import by_bank
from test_write import checked_items, day_packet, deliver, dump, l3_fact, write

from javmem.cli import main
from javmem.config import Settings
from javmem.decisions import export_decisions, import_decisions
from javmem.ledger import connect_readonly, open_ledger, transaction
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.window import Window
from javmem.pipeline.write import LedgerWriter
from javmem.review import remind
from javmem.soft.cases import Case
from javmem.soft.claude import SoftSettings
from javmem.soft.find import find_cases
from javmem.soft.judge import PROMPT_VERSION, SoftStore, case_key, render_case

DAICHUAN = {"key": "e1", "mention": "岱川", "name": "岱川实业", "type": "公司或机构"}


def bank_moves(window: Window) -> dict[str, object]:
    """X1 票据：写着哪家的那一行托管于那一家；写着“先停”的标暂停，写着“转走”的标已结束。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        if "X1 票据" not in text:
            continue
        for bank in (BANK, BROKER):
            if str(bank["name"]) in text:
                state = "暂停" if "先停" in text else "已结束" if "转走" in text else "生效"
                found.append(relation(NOTE, "托管于", bank, (line.ref, text), state=state))
    return {"entities": [NOTE, BANK, BROKER], "facts": [], "relations": found}


def prices(window: Window) -> dict[str, object]:
    """岱川的零件单价：写着几就是几；写着“可能”的标含糊。"""
    found = []
    for line in window.targets:
        text = line.text
        if "单价" not in text:
            continue
        value = "13.0" if "13.0" in text else "12.5"
        found.append(l3_fact(window, f"单价{'可能改成' if '可能' in text else '是'} {value}", text, subject="e1",
                             attribute="零件单价", value=value, certainty="vague" if "可能" in text else "stated"))
    return {"entities": [DAICHUAN], "facts": found}


def case_of(world: World, words: str) -> Case:
    """javmem soft find 出的题里，要判的那一句有这几个字的那一道。"""
    conn = connect_readonly(world.settings.library("prod").db_path)
    try:
        cases = find_cases(conn).cases
    finally:
        conn.close()
    (case,) = [case for case in cases if any(words in line.text for line in case.target_lines)]
    return case


def judged(world: World, case: Case, verdict: str, **fields: object) -> None:
    """往数据目录的 soft.db 里存一份编的结论（和 javmem soft judge 存的一样的样子）：第一条旧的判 verdict。"""
    request = render_case(case)
    model = SoftSettings.from_env().cache_name
    first = {"target": case.old[0].ref, "about": True, "verdict": verdict, "condition": None, "effective_at": None,
             "effective_precision": "unknown", "new_value": None, "sure": True, "finance": False, "quote": "编的",
             "reason": "编的", "quote_ok": True, "consistent": True, "date_ok": True, **fields}
    items = [first] + [{**first, "target": old.ref, "verdict": "A", "about": False} for old in case.old[1:]]
    with SoftStore(world.data / "soft"):
        pass  # 建表
    conn = sqlite3.connect(world.data / "soft" / "soft.db")
    try:
        conn.execute(
            "INSERT INTO verdicts (key, case_id, prompt_version, model, request_sha, request, output, parsed, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)",
            (case_key(request, model), case.id, PROMPT_VERSION, model, request.sha256, request.text,
             json.dumps({"items": items}, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def apply(world: World) -> int:
    """写账最后一步：照软变化的结论执行（javmem write 每次都跑这一步）。"""
    conn = open_ledger(world.settings.library("prod"))
    try:
        writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding", register=False)
        with transaction(conn):
            return writer.apply_soft_verdicts(world.data / "soft", "2026-11-30T10:00:00+08:00")
    finally:
        conn.close()


def paused_later(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据在海港银行那边先停一下。"))
    write(world, bank_moves).close()


# ---------- 第一次写账：先挡住 ----------


def test_a_pause_said_after_the_old_one_is_held_until_it_is_judged(world: World) -> None:
    paused_later(world)
    rows = world.rows("SELECT relation_id, state, status, pending_reason FROM relations ORDER BY as_of_epoch")
    assert [(row["state"], row["status"], row["pending_reason"]) for row in rows] == [
        ("active", "verified", None), ("paused", "pending", "soft_change")]
    (review,) = world.rows("SELECT kind, claims, decided_at FROM review_queue")
    claims = json.loads(review["claims"])
    assert review["kind"] == "soft_change" and review["decided_at"] is None
    assert (claims["item"], claims["relation"], claims["reasons"]) == ("relation", rows[1]["relation_id"], ["not_judged"])
    assert apply(world) == 0  # 没有 soft.db（还没配密钥、没判）：旧的照旧
    assert [old.id for old in case_of(world, "先停").old] == [rows[0]["relation_id"]]  # 挡住的自己一行，题里配得上旧的


def test_a_hedged_repeat_that_changes_nothing_is_not_held(world: World) -> None:
    """只是又说了一遍（状态一样，说得不干脆）的，不是“改到以前说的”，照常写（组里以前没说过的、同一句话里说的也一样，
    见 test_relations_newold.py 的暂停那天、同一句话里两家）。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据可能还在海港银行吧。"))
    write(world, bank_moves).close()
    assert world.rows("SELECT count(*) AS n FROM review_queue")[0]["n"] == 0
    (row,) = world.rows("SELECT state, status FROM relations")
    assert (row["state"], row["status"]) == ("active", "verified")


# ---------- 照结论执行 ----------


def test_a_pause_verdict_pauses_the_old_one_from_the_judged_day(world: World) -> None:
    paused_later(world)
    case = case_of(world, "先停")
    judged(world, case, "B", effective_at="2026-11-04", effective_precision="day", condition="等对方回复")
    assert apply(world) == 1
    (row,) = world.rows("SELECT state, status, paused_at, paused_at_precision, pause_condition FROM relations")
    assert (row["state"], row["paused_at"], row["paused_at_precision"], row["pause_condition"]) == ("paused", "2026-11-04", "day", "等对方回复")
    (review,) = world.rows("SELECT decision, decided_by, claims FROM review_queue")
    assert (review["decision"], review["decided_by"]) == ("pause", "auto")
    claims = json.loads(review["claims"])
    assert (claims["verdict"], claims["case"], claims["action"]["kind"]) == ("B", case.id, "pause")
    assert claims["judge"] == f"{SoftSettings.from_env().cache_name} {PROMPT_VERSION}"
    (audit,) = world.rows("SELECT item_kind, trusted, tier, judge FROM audit_log WHERE check_kind = 'soft_change'")
    assert (audit["item_kind"], audit["trusted"], audit["tier"]) == ("relation", "B pause", "model")
    assert audit["judge"] == f"{SoftSettings.from_env().cache_name} {PROMPT_VERSION}"
    assert apply(world) == 0  # 定过的不再找


def test_a_keep_verdict_leaves_the_old_one_and_the_held_claim_stays_an_intent(world: World) -> None:
    paused_later(world)
    judged(world, case_of(world, "先停"), "E")
    assert apply(world) == 1
    rows = world.rows("SELECT state, pending_reason FROM relations ORDER BY as_of_epoch")
    assert [(row["state"], row["pending_reason"]) for row in rows] == [("active", None), ("paused", "soft_change")]
    (review,) = world.rows("SELECT decision, decided_by FROM review_queue")
    assert (review["decision"], review["decided_by"]) == ("keep", "auto")


def test_an_unsure_verdict_waits_for_the_owner_with_the_model_suggestion(world: World) -> None:
    paused_later(world)
    judged(world, case_of(world, "先停"), "B", sure=False)
    assert apply(world) == 1
    (review,) = world.rows("SELECT decided_at, claims FROM review_queue")
    claims = json.loads(review["claims"])
    assert review["decided_at"] is None and (claims["suggestion"], claims["reasons"]) == ("B", ["not_sure"])
    assert world.rows("SELECT state FROM relations ORDER BY as_of_epoch")[0]["state"] == "active"
    assert apply(world) == 0  # 判过要主人看的不再找


def test_a_replace_verdict_moves_the_custody_from_the_judged_day(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据可能要放到东岸证券。"))
    write(world, bank_moves).close()
    rows = by_bank(world)
    assert (rows["海港银行"]["state"], rows["东岸证券"]["pending_reason"]) == ("active", "soft_change")  # 会顶掉海港银行：先挡住
    judged(world, case_of(world, "可能要放到"), "D", effective_at="2026-11-08", effective_precision="day")
    assert apply(world) == 1
    rows = by_bank(world)
    assert (rows["海港银行"]["state"], rows["海港银行"]["valid_to"]) == ("ended", "2026-11-08")
    assert rows["海港银行"]["superseded_by"] == rows["东岸证券"]["relation_id"]
    assert (rows["东岸证券"]["state"], rows["东岸证券"]["valid_from"], rows["东岸证券"]["status"]) == ("active", "2026-11-08", "verified")


def test_the_owner_can_answer_a_held_claim(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    paused_later(world)
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod"]) == 0
    shown = capsys.readouterr().out
    assert "软变化" in shown and "--verdict" in shown
    assert main(["review", "--library", "prod", "--decide", str(review), "--verdict", "C", "--at", "2026-11-03"]) == 0
    (row,) = world.rows("SELECT state, valid_to FROM relations")
    assert (row["state"], row["valid_to"]) == ("ended", "2026-11-03")
    (audit,) = world.rows("SELECT tier, judge, trusted FROM audit_log WHERE check_kind = 'soft_change'")
    assert (audit["tier"], audit["judge"], audit["trusted"]) == ("owner", "owner", "C end")


def test_an_unjudged_held_claim_is_kept_as_an_intent_after_30_days(world: World) -> None:
    paused_later(world)
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    world.execute("UPDATE review_queue SET created_at = '2026-01-01T00:00:00+08:00'")
    assert remind(world.settings, "prod", force=True).expired == [review]
    (row,) = world.rows("SELECT decision, decided_by FROM review_queue")
    assert (row["decision"], row["decided_by"]) == ("keep", "auto")  # D151：当成不改
    rows = world.rows("SELECT state, pending_reason FROM relations ORDER BY as_of_epoch")
    assert [(one["state"], one["pending_reason"]) for one in rows] == [("active", None), ("paused", "soft_change")]
    (audit,) = world.rows("SELECT tier FROM audit_log WHERE check_kind = 'soft_change'")
    assert audit["tier"] == "rule"


def test_javmem_write_applies_the_verdicts_as_its_last_step(world: World) -> None:
    paused_later(world)
    judged(world, case_of(world, "先停"), "B", effective_at="2026-11-04", effective_precision="day")
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0  # 没有排队的包也跑这一步
    (row,) = world.rows("SELECT state, paused_at FROM relations")
    assert (row["state"], row["paused_at"]) == ("paused", "2026-11-04")


# ---------- 事实 ----------


def test_a_held_fact_does_not_replace_the_old_value_until_it_is_judged(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), day_packet("2026-11-05", "我：岱川的零件单价可能改成 13.0。"))
    write(world, prices).close()
    facts = {row["value"]: row for row in world.rows("SELECT * FROM facts")}
    assert facts["12.5"]["superseded_by"] is None  # 旧的照旧现行
    assert (facts["13.0"]["status"], facts["13.0"]["pending_reason"]) == ("pending", "soft_change")
    (review,) = world.rows("SELECT claims FROM review_queue")
    assert json.loads(review["claims"])["fact"] == facts["13.0"]["fact_id"]
    judged(world, case_of(world, "可能改成"), "D", effective_at="2026-11-10", effective_precision="day")
    assert apply(world) == 1
    facts = {row["value"]: row for row in world.rows("SELECT * FROM facts")}
    assert (facts["12.5"]["superseded_by"], facts["12.5"]["superseded_at"]) == (facts["13.0"]["fact_id"], "2026-11-10T00:00:00+08:00")
    assert facts["13.0"]["status"] != "pending"


def test_a_paused_fact_stays_alongside_the_old_value(world: World) -> None:
    """D153 第 4 条：事实不加“暂停”：判了暂停，旧的照旧现行，这句话另记的那条也现行。"""
    deliver(world, day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), day_packet("2026-11-05", "我：岱川的零件单价可能改成 13.0。"))
    write(world, prices).close()
    judged(world, case_of(world, "可能改成"), "B")
    assert apply(world) == 1
    facts = {row["value"]: row for row in world.rows("SELECT * FROM facts")}
    assert facts["12.5"]["superseded_by"] is None and facts["13.0"]["superseded_by"] is None
    assert facts["13.0"]["status"] != "pending"


# ---------- 照记下的办 ----------


def test_rewriting_the_windows_keeps_the_decision(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据可能要放到东岸证券。"))
    items = checked_items(world, bank_moves)
    write(world, bank_moves).close()
    judged(world, case_of(world, "可能要放到"), "D", effective_at="2026-11-08", effective_precision="day")
    apply(world)
    (world.data / "soft" / "soft.db").unlink()  # 定过的记在待确认里，重写窗口不再找 soft.db
    conn = open_ledger(world.settings.library("prod"))
    before = dump(conn, surrogate=False)
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
    with VectorCache(world.data / "vectors") as cache:
        for item in items:
            writer.write_window(item, cache.get("fake-embedding", writer.texts_needed(item)))
    assert dump(conn, surrogate=False)["relations"] == before["relations"]
    conn.close()


def test_the_decision_survives_a_rebuild_through_export_and_import(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packets = [day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据在海港银行那边先停一下。")]
    deliver(world, *packets)
    write(world, bank_moves).close()
    judged(world, case_of(world, "先停"), "B", effective_at="2026-11-04", effective_precision="day")
    apply(world)
    exported = export_decisions(world.settings, "prod")
    data = tmp_path / "rebuilt"
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    rebuilt = World(data.resolve(), Settings.from_env())
    deliver(rebuilt, *packets)
    write(rebuilt, bank_moves).close()
    assert len(rebuilt.rows("SELECT 1 FROM relations")) == 2  # 新账本里先又挡住了
    import_decisions(rebuilt.settings, "prod", exported)
    (row,) = rebuilt.rows("SELECT state, paused_at FROM relations")
    assert (row["state"], row["paused_at"]) == ("paused", "2026-11-04")
    assert [one["decision"] for one in rebuilt.rows("SELECT decision FROM review_queue")] == ["pause"]  # 没处理的那条关掉了


def test_freezing_a_held_claim_accepts_it(world: World) -> None:
    """主人冻结了挡住的那一行（或者那条事实）：主人担保，不再挡着等判，照常算，那条待确认关掉。"""
    paused_later(world)
    held = world.rows("SELECT relation_id FROM relations WHERE pending_reason = 'soft_change'")[0]["relation_id"]
    assert main(["freeze", "--library", "prod", "--relation", held]) == 0
    (row,) = world.rows("SELECT state, status, frozen FROM relations")
    assert (row["state"], row["frozen"]) == ("paused", 1) and row["status"] != "pending"
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE kind = 'soft_change'")[0]["n"] == 0
