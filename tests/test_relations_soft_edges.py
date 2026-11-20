"""J4.4 第三轮审阅（审 5873b3d，第 4 段软变化）查出来的那几种情况。例子都是编的；结论是编的 soft.db，不调模型。"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from helpers import World
from test_relcheck import BANK, BROKER, NOTE, relation
from test_relations_newold import by_bank
from test_relations_soft import DAICHUAN, apply, case_of, judged, paused_later
from test_relations_write import resent
from test_write import day_packet, deliver, l3_fact, write

from javmem.cli import main
from javmem.config import Settings
from javmem.decisions import export_decisions, import_decisions
from javmem.ledger import open_ledger
from javmem.pipeline.window import Window
from javmem.review import freeze
from javmem.soft.claude import SoftSettings
from javmem.soft.judge import PROMPT_VERSION, SoftStore
from javmem.timeutil import parse_aware
from javmem.withdraw import withdraw

DAYS = {"11 月 9 日起": "2026-11-09", "10 月 1 日起": "2026-10-01", "11 月 5 日起": "2026-11-05"}


def lines(window: Window) -> dict[str, object]:
    """X1 票据：写着哪家的那一行托管于那一家（“先不要放海港银行了，放东岸证券”只算东岸证券）；写着“先停”的标暂停，
    “转走”的标已结束；写着“X 月 X 日起”的照写开始日子。“那边先停一下”单独一行的，和上一行一起算一处说法。"""
    found = []
    targets = list(window.targets)
    for number, line in enumerate(targets):
        text = line.text.rstrip("。")
        if text.startswith("那边先停一下") and number:
            previous = targets[number - 1]
            found.append(relation(NOTE, "托管于", BANK, (previous.ref, previous.text.rstrip("。")), (line.ref, text), state="暂停"))
            continue
        if "X1 票据" not in text or (number + 1 < len(targets) and targets[number + 1].text.startswith("那边先停一下")):
            continue
        fields: dict[str, object] = {}
        for marker, day in DAYS.items():
            if marker in text:
                fields.update(valid_from=day, time_precision="day")
        for bank in (BANK, BROKER):
            if str(bank["name"]) not in text or (bank is BANK and "先不要放海港银行" in text):
                continue
            state = "暂停" if "先停" in text else "已结束" if "转走" in text else "生效"
            found.append(relation(NOTE, "托管于", bank, (line.ref, text), state=state, **fields))
    return {"entities": [NOTE, BANK, BROKER], "facts": [], "relations": found}


def prices(window: Window) -> dict[str, object]:
    """岱川的零件单价：一行里第一个“数.数”是值；有“可能”“吧”“如果”的标含糊。"""
    found = []
    for line in window.targets:
        text = line.text
        match = re.search(r"\d+\.\d+", text)
        if "单价" not in text or match is None:
            continue
        phrase = text.split("：", 1)[-1].rstrip("。？?")
        vague = any(word in text for word in ("可能", "吧", "如果"))
        found.append(l3_fact(window, phrase, f"岱川的零件单价是 {match.group(0)}", subject="e1", attribute="零件单价",
                             value=match.group(0), certainty="vague" if vague else "stated"))
    return {"entities": [DAICHUAN], "facts": found}


def facts_by_value(world: World) -> dict[str, dict]:
    return {row["value"]: dict(row) for row in world.rows("SELECT * FROM facts")}


def open_soft(world: World) -> list[dict]:
    return [json.loads(row["claims"]) for row in world.rows(
        "SELECT claims FROM review_queue WHERE kind = 'soft_change' AND decided_at IS NULL")]


def rebuilt_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "rebuilt") -> World:
    data = tmp_path / name
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    return World(data.resolve(), Settings.from_env())


# ---------- S1：判了结束，那天比旧的那一段开始还早（取消了还没开始的安排） ----------


def test_cancelling_before_the_start_ends_the_old_one_without_a_phantom_row(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据 11 月 9 日起在海港银行。"),
            day_packet("2026-11-05", "我：X1 票据先不要放海港银行了，放东岸证券。"))
    write(world, lines).close()
    judged(world, case_of(world, "先不要"), "C", effective_at="2026-11-05", effective_precision="day")
    assert apply(world) == 1
    rows = world.rows("SELECT r.*, e.canonical_name AS bank FROM relations AS r JOIN entities AS e ON e.entity_id = r.object_entity_id")
    assert len(rows) == 2  # 没有多出一行没有说法的
    (bank,) = [row for row in rows if row["bank"] == "海港银行"]
    assert (bank["state"], bank["valid_to"]) == ("ended", "2026-11-05")


def test_a_released_end_before_the_start_ends_the_same_relation(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据 11 月 9 日起在海港银行。"),
            day_packet("2026-11-05", "我：X1 票据在海港银行那边先停一下。"))
    write(world, lines).close()
    judged(world, case_of(world, "先停"), "C", effective_at="2026-11-05", effective_precision="day")
    assert apply(world) == 1
    (row,) = world.rows("SELECT state, valid_from, valid_to FROM relations")
    assert (row["state"], row["valid_to"]) == ("ended", "2026-11-05")


# ---------- S2、S5、小的第 5 条：整本重建导回 ----------


def test_importing_a_keep_decision_closes_the_new_open_one(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packets = [day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据在海港银行那边先停一下。")]
    deliver(world, *packets)
    write(world, lines).close()
    judged(world, case_of(world, "先停"), "E")
    apply(world)
    exported = export_decisions(world.settings, "prod")
    rebuilt = rebuilt_world(tmp_path, monkeypatch)
    deliver(rebuilt, *packets)
    write(rebuilt, lines).close()
    assert len(open_soft(rebuilt)) == 1  # 新账本里先又挡住了
    import_decisions(rebuilt.settings, "prod", exported)
    assert open_soft(rebuilt) == []  # 导回来的定过的盖过它，不再问主人
    assert [row["decision"] for row in rebuilt.rows("SELECT decision FROM review_queue WHERE kind = 'soft_change'")] == ["keep"]


def test_a_rebuild_with_soft_db_still_imports_the_owner_decisions(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packets = [day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据在海港银行那边先停一下。")]
    deliver(world, *packets)
    write(world, lines).close()
    judged(world, case_of(world, "先停"), "B", effective_at="2026-11-04", effective_precision="day")
    apply(world)
    relation_id = world.rows("SELECT relation_id FROM relations")[0]["relation_id"]
    freeze(world.settings, "prod", [relation_id])
    exported = export_decisions(world.settings, "prod")
    old_soft = world.data / "soft"
    rebuilt = rebuilt_world(tmp_path, monkeypatch)
    shutil.copytree(old_soft, rebuilt.data / "soft")  # 为了不重新付钱判，soft.db 也搬过去了
    deliver(rebuilt, *packets)
    write(rebuilt, lines).close()
    apply(rebuilt)  # javmem write 最后一步照 soft.db 自己定了
    report = import_decisions(rebuilt.settings, "prod", exported)  # 不因为“已经有定过的决定”拒导
    assert report.frozen == 1
    (row,) = rebuilt.rows("SELECT state, frozen, paused_at FROM relations")
    assert (row["state"], row["frozen"], row["paused_at"]) == ("paused", 1, "2026-11-04")


def test_an_open_soft_change_keeps_its_age_through_a_rebuild(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packets = [day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据在海港银行那边先停一下。")]
    deliver(world, *packets)
    write(world, lines).close()
    world.execute("UPDATE review_queue SET created_at = '2026-10-23T10:00:00+08:00'")
    exported = export_decisions(world.settings, "prod")
    rebuilt = rebuilt_world(tmp_path, monkeypatch)
    deliver(rebuilt, *packets)
    write(rebuilt, lines).close()
    import_decisions(rebuilt.settings, "prod", exported)
    assert [row["created_at"] for row in rebuilt.rows("SELECT created_at FROM review_queue")] == ["2026-10-23T10:00:00+08:00"]


# ---------- S3：包重发，候选句的编号按位置算 ----------


def test_a_resent_sentence_with_other_words_does_not_take_the_old_decision(world: World) -> None:
    paused_later(world)
    judged(world, case_of(world, "先停"), "B", effective_at="2026-11-04", effective_precision="day")
    apply(world)
    deliver(world, resent("2026-11-03", "我：X1 票据在海港银行那边暂时不停了，照常放着。"))  # 同一个位置换了一句
    write(world, lines).close()
    (row,) = [row for row in world.rows("SELECT state, paused_at FROM relations") if row["state"] != "ended"]
    assert (row["state"], row["paused_at"]) == ("active", None)


def test_a_resent_sentence_that_moved_keeps_its_decision(world: World) -> None:
    paused_later(world)
    judged(world, case_of(world, "先停"), "B", effective_at="2026-11-04", effective_precision="day")
    apply(world)
    deliver(world, resent("2026-11-03", "我：早上好。", "我：X1 票据在海港银行那边先停一下。"))  # 同一句挪了一个位置
    write(world, lines).close()
    (row,) = world.rows("SELECT state, paused_at FROM relations")
    assert (row["state"], row["paused_at"]) == ("paused", "2026-11-04")
    assert open_soft(world) == []


# ---------- S4、N1、N2、N7、小的第 2 条：事实 ----------


def test_a_paused_fact_is_still_replaced_by_a_later_value(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), day_packet("2026-11-05", "我：岱川的零件单价可能改成 13.0。"))
    write(world, prices).close()
    judged(world, case_of(world, "可能改成"), "B")
    apply(world)
    deliver(world, day_packet("2026-11-20", "我：岱川的零件单价定成 14.0。"))
    write(world, prices).close()
    facts = facts_by_value(world)
    assert facts["12.5"]["superseded_by"] == facts["14.0"]["fact_id"]
    assert facts["13.0"]["superseded_by"] == facts["14.0"]["fact_id"]


def test_a_hedged_repeat_of_the_current_value_is_not_held(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), day_packet("2026-11-03", "我：岱川的零件单价是 13.0。"),
            day_packet("2026-11-05", "我：岱川的零件单价还是 13.0 吧？"))
    write(world, prices).close()
    assert open_soft(world) == []
    assert facts_by_value(world)["13.0"]["status"] != "pending"


def test_a_withdrawn_held_fact_claim_takes_its_review_away(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), day_packet("2026-11-05", "我：岱川的零件单价可能改成 13.0。"),
            day_packet("2026-11-10", "我：岱川的零件单价是 13.0。"))
    write(world, prices).close()
    assert len(open_soft(world)) == 1
    held = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%可能改成%'")[0]["sentence_id"]
    withdraw(world.settings, "prod", sentences=[held])
    assert open_soft(world) == []


def test_the_judged_day_only_replaces_the_time_of_its_own_replacement(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), day_packet("2026-11-05", "我：岱川的零件单价可能改成 13.0。"),
            day_packet("2026-11-08", "我：岱川的零件单价是 12.8。"), day_packet("2026-11-12", "我：岱川的零件单价是 13.0。"))
    write(world, prices).close()
    judged(world, case_of(world, "可能改成"), "D", effective_at="2026-11-06", effective_precision="day")
    apply(world)
    facts = facts_by_value(world)
    assert facts["12.5"]["superseded_by"] == facts["13.0"]["fact_id"]
    assert str(facts["12.5"]["superseded_at"]).startswith("2026-11-06") and parse_aware(facts["12.5"]["superseded_at"]) is not None
    assert str(facts["12.8"]["superseded_at"]).startswith("2026-11-12")  # 12.8 是 11-12 那一句替代的，不是判的那天


# ---------- N3、N4、N5：关系 ----------


def test_the_candidate_sentence_is_the_hedged_one(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"),
            day_packet("2026-11-03", "我：X1 票据在海港银行。", "我：那边先停一下。"))
    write(world, lines).close()
    (claims,) = open_soft(world)
    text = world.rows("SELECT text FROM raw_sentences WHERE sentence_id = ?", claims["sentence"])[0]["text"]
    assert "先停" in text


def test_a_hedged_history_line_is_not_held(world: World) -> None:
    """D153 第 1 条：后说的讲的是更早的事只补历史，不改现在的；带了犹豫词也一样，不挡。"""
    deliver(world, day_packet("2026-11-05", "我：X1 票据 11 月 5 日起在东岸证券。"),
            day_packet("2026-11-10", "我：X1 票据 10 月 1 日起可能在海港银行。"))
    write(world, lines).close()
    assert open_soft(world) == []
    rows = by_bank(world)
    assert (rows["海港银行"]["state"], rows["海港银行"]["superseded_by"]) == ("ended", rows["东岸证券"]["relation_id"])


def test_a_held_row_is_not_offered_as_the_old_one(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据在海港银行那边先停一下。"),
            day_packet("2026-11-05", "我：X1 票据可能要放到东岸证券。"))
    write(world, lines).close()
    current = world.rows("SELECT relation_id FROM relations WHERE pending_reason IS NULL")[0]["relation_id"]
    case = case_of(world, "可能要放到")
    assert [(old.id, old.state) for old in case.old] == [(current, "生效")]


# ---------- N6：主人答的哪天要认得出 ----------


def test_the_owner_day_has_to_be_readable(world: World) -> None:
    paused_later(world)
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--verdict", "C", "--at", "下下个月"]) == 1
    assert main(["review", "--library", "prod", "--decide", str(review), "--verdict", "C", "--at", "2026-11-31"]) == 1
    assert world.rows("SELECT decided_at FROM review_queue")[0]["decided_at"] is None
    assert main(["review", "--library", "prod", "--decide", str(review), "--verdict", "B", "--at", "2026-11"]) == 0
    (row,) = world.rows("SELECT paused_at, paused_at_precision FROM relations")
    assert (row["paused_at"], row["paused_at_precision"]) == ("2026-11", "month")


# ---------- 小的第 1、6 条：判出来加给旧的那一处 ----------


def test_a_directive_does_not_change_the_old_ones_status_or_last_said(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据可能要放到东岸证券。"))
    write(world, lines).close()
    before = by_bank(world)["海港银行"]
    judged(world, case_of(world, "可能要放到"), "B", effective_at="2026-11-06", effective_precision="day")
    apply(world)
    after = by_bank(world)["海港银行"]
    assert (after["state"], after["paused_at"]) == ("paused", "2026-11-06")
    assert (after["status"], after["as_of"]) == (before["status"], before["as_of"])


def test_a_directive_against_a_frozen_one_is_recorded_as_not_done(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据可能要放到东岸证券。"))
    write(world, lines).close()
    bank = by_bank(world)["海港银行"]["relation_id"]
    freeze(world.settings, "prod", [bank])
    judged(world, case_of(world, "可能要放到"), "B", effective_at="2026-11-06", effective_precision="day")
    apply(world)
    assert by_bank(world)["海港银行"]["state"] == "active"  # 冻结的不改
    (review,) = world.rows("SELECT claims FROM review_queue WHERE kind = 'soft_change'")
    assert json.loads(review["claims"]).get("not_done") == "frozen"
    (audit,) = world.rows("SELECT trusted FROM audit_log WHERE check_kind = 'soft_change'")
    assert "冻结" in audit["trusted"]


# ---------- 小的第 3 条：soft.db 没变就不再出一遍题 ----------


def test_the_last_step_does_not_search_again_while_soft_db_is_unchanged(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.soft.find as find

    paused_later(world)
    with SoftStore(world.data / "soft"):
        pass  # 建表
    conn = sqlite3.connect(world.data / "soft" / "soft.db")  # 存一份别的题的结论：soft.db 有了，这一题还是没判
    conn.execute("INSERT INTO verdicts (key, case_id, prompt_version, model, request_sha, request, output, parsed, created_at)"
                 " VALUES ('k-other', 'C000000000000', ?, ?, 'x', 'x', '{}', '{\"items\": []}', '2026-11-17T00:00:00+08:00')",
                 (PROMPT_VERSION, SoftSettings.from_env().cache_name))
    conn.commit()
    conn.close()
    calls = []
    real = find.find_cases
    monkeypatch.setattr(find, "find_cases", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    assert apply(world) == 0 and apply(world) == 0
    assert len(calls) == 1
    judged(world, case_of(world, "先停"), "E")  # soft.db 变了：再找一遍
    assert apply(world) == 1 and len(calls) == 2


# ---------- 小的第 4 条：迁移 0022 照 c26a05d 那一份，后来加的在 0023 ----------


def test_a_ledger_opened_with_the_first_j44_migration_upgrades(tmp_path: Path) -> None:
    """用 c26a05d 那一份 0022（只有 identity_key）建到第 22 版的账本，这一版代码接着跑 0023（第三轮审阅小的第 4 条）。"""
    from javmem.ledger import connect, load_migrations, migrate, schema_version

    migrations = load_migrations()
    assert "paused_at" not in migrations[21].sql  # 0022 照交过第一轮审阅的那一份
    conn = connect(tmp_path / "old.db")
    try:
        migrate(conn, migrations[:22])
        assert schema_version(conn) == 22
        migrate(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(relation_claims)")}
        assert {"identity_key", "paused_at", "soft_sentence", "judge_hold"} <= columns
        assert conn.execute("SELECT count(*) FROM soft_apply").fetchone()[0] == 0
    finally:
        conn.close()


# ---------- 小的第 7 条：提示文字 ----------


def test_the_reminder_says_soft_changes_were_kept_as_they_are(world: World) -> None:
    from javmem.review import remind

    paused_later(world)
    world.execute("UPDATE review_queue SET created_at = '2026-01-01T00:00:00+08:00'")
    reminder = remind(world.settings, "prod", force=True)
    assert reminder.expired and "当成不改" in reminder.line()


def test_a_judged_end_with_nothing_left_to_end_keeps_its_claim(world: World) -> None:
    """判了结束、放出来的那一处，可那时旧的那一段已经被别的说法结束了：这一处照常留着（自己一段），不丢。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据在海港银行那边先停一下。"))
    write(world, lines).close()
    judged(world, case_of(world, "先停"), "C", effective_at="2026-11-03", effective_precision="day")
    deliver(world, day_packet("2026-11-02", "我：X1 票据从海港银行转走了。"))  # 晚到的：11-02 就转走了
    write(world, lines).close()
    apply(world)
    assert world.rows("SELECT count(*) AS n FROM relation_claims")[0]["n"] == 3
