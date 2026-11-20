"""J5.3 进图前检查的审阅（没参与写的子代理，只读审 2bfb5e4，11-18）复现出来的几条，修之前都是红的。例子都是编的；判的结论
直接写在 FactCheck 上（test_precheck.judged），不调模型。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World
from test_chat_answers import code_of_open, said
from test_decisions import exported_to, rebuilt
from test_graph import connect_graph, run_sync
from test_precheck import judged, ship, value_code
from test_withdraw_review import rebuilt_world
from test_relations_judge import judged as judged_relations
from test_relations_write import bank_extract
from test_write import day_packet, deliver, l3_fact, write

from javmem.cli import main
from javmem.graph.gate import GateItem, Verdict
from javmem.ledger import open_ledger
from javmem.pipeline import precheck
from javmem.review import freeze

UNSURE_1115 = judged(value_code({"11-15": "llm_unsure"}))
ALL_OK = judged(lambda fact: None)
ORDER_ENTITY = {"key": "e1", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}


def plain(window):  # noqa: ANN001, ANN201
    """没有属性的事实（不分组、不参加新旧）：越南那批订单 11-15 出货。"""
    found = [l3_fact(window, line.text.rstrip("。"), "越南那批订单 11-15 出货") for line in window.targets if "11-15" in line.text]
    return {"entities": [], "facts": found}


def by_value(world: World) -> list[tuple[object, ...]]:
    return [tuple(row) for row in world.rows(
        "SELECT value, frozen, status, pending_reason, judged_by, superseded_by IS NOT NULL AS old FROM facts ORDER BY value")]


# ---------- 审阅第 1 条（严重）：整本重建、导回以后主人对转人工事实的决定 ----------


@pytest.mark.parametrize("choice", [["--keep", "new"], ["--drop"], ["--keep", "hidden"]], ids=["keep", "drop", "hidden"])
def test_the_owners_decision_and_its_trail_survive_a_rebuild(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             choice: list[str]) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    day = day_packet("2026-11-01", "我：越南那批订单 11-15 出货。")
    deliver(world, day)
    write(world, ship, order=UNSURE_1115).close()
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), *choice]) == 0
    before = [tuple(row) for row in world.rows("SELECT fact_id, status, pending_reason, judged_by FROM facts")]
    trail = sorted((row["check_kind"], row["tier"], row["trusted"]) for row in world.rows("SELECT * FROM audit_log"))
    path = exported_to(world, tmp_path)
    new = rebuilt_world(tmp_path, monkeypatch)
    deliver(new, day)
    write(new, ship, order=UNSURE_1115).close()
    assert main(["review", "--library", "prod", "--import", str(path)]) == 0
    assert [tuple(row) for row in new.rows("SELECT fact_id, status, pending_reason, judged_by FROM facts")] == before
    assert sorted((row["check_kind"], row["tier"], row["trusted"]) for row in new.rows("SELECT * FROM audit_log")) == trail
    assert new.rows("SELECT count(*) AS n FROM review_queue WHERE decided_at IS NULL")[0]["n"] == 0


# ---------- 审阅第 2 条（严重）：冻结的事实只剩转人工的说法 ----------


def test_a_frozen_fact_whose_claims_turn_held_stays_the_owners(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship, order=ALL_OK).close()
    fact_id = world.rows("SELECT fact_id FROM facts")[0]["fact_id"]
    assert main(["freeze", "--library", "prod", "--fact", fact_id]) == 0
    world.execute("UPDATE packets SET extraction = 'queued'")
    world.execute("UPDATE fact_claims SET l4_version = 'l4-v6'")  # 窗口重写，这一次第二档拿不准
    write(world, ship, order=UNSURE_1115).close()
    deliver(world, day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    write(world, ship, order=ALL_OK).close()
    assert by_value(world)[0][:5] == ("11-15", 1, "verified", None, "owner")  # 主人担保的：照旧现行、进图
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE kind = 'frozen_conflict'")[0]["n"] == 1  # 新值进冲突
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE kind = 'manual_review' AND decided_at IS NULL")[0]["n"] == 0


@pytest.mark.parametrize("attribute", [True, False], ids=["grouped", "no_group"])
def test_freezing_a_held_fact_vouches_for_it_and_unfreezing_holds_it_again(world: World, monkeypatch: pytest.MonkeyPatch,
                                                                         attribute: bool) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship if attribute else plain, order=UNSURE_1115 if attribute else
          judged(lambda fact: "llm_unsure")).close()
    fact_id = world.rows("SELECT fact_id FROM facts")[0]["fact_id"]
    assert main(["freeze", "--library", "prod", "--fact", fact_id]) == 0
    row = world.rows("SELECT status, pending_reason, judged_by FROM facts")[0]
    assert (row["status"], row["pending_reason"], row["judged_by"]) == ("verified", None, "owner")
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE decided_at IS NULL")[0]["n"] == 0
    assert main(["freeze", "--library", "prod", "--fact", fact_id, "--undo"]) == 0
    row = world.rows("SELECT status, pending_reason, judged_by FROM facts")[0]
    assert (row["status"], row["pending_reason"], row["judged_by"]) == ("pending", "manual_review", None)
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE decided_at IS NULL")[0]["n"] == 1


# ---------- 审阅第 3 条：转人工的那一处的文字跟着判过的说法进了图 ----------


def two_ways(window):  # noqa: ANN001, ANN201
    found = []
    for line in window.targets:
        if "已经付了" in line.text:  # 早说的这一处多写了“货款已付清”：判成拿不准、转人工
            found.append(l3_fact(window, line.text.rstrip("。"), "越南那批订单 11-15 出货，货款已付清",
                                 subject="e1", attribute="出货日期", value="11-15"))
        elif "11-15" in line.text:
            found.append(l3_fact(window, line.text.rstrip("。"), "越南那批订单 11-15 出货", subject="e1", attribute="出货日期",
                                 value="11-15"))
    return {"entities": [ORDER_ENTITY], "facts": found}


def test_a_held_claim_lends_nothing_to_a_judged_fact(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单货款已经付了，11-15 出货。"),
            day_packet("2026-11-03", "我：越南那批订单 11-15 出货。"))
    write(world, two_ways, order=judged(lambda fact: "llm_unsure" if "货款" in str(fact.raw.get("text")) else None)).close()
    (fact,) = world.rows("SELECT fact_id, content, judged_by, as_of FROM facts")
    assert (fact["content"], fact["judged_by"]) == ("越南那批订单 11-15 出货", "local")
    held = world.rows("SELECT proofs FROM fact_claims WHERE judge_hold IS NOT NULL")[0]["proofs"]
    evidence = {row["sentence_id"] for row in world.rows("SELECT sentence_id FROM evidence WHERE fact_id IS NOT NULL")}
    assert evidence and not any(sentence in held for sentence in evidence)  # 转人工那一处的证据不算这条事实的证据（别名的出处另算）
    graph = connect_graph(world)
    run_sync(world, graph, gate=precheck.gate())
    assert [item["props"]["content"] for item in graph.iter_items("fact")] == ["越南那批订单 11-15 出货"]


# ---------- 审阅第 4 条：对话里的回答撤回以后，没组的转人工事实 ----------


def test_a_withdrawn_chat_answer_puts_a_groupless_held_fact_back_on_hold(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    order = judged(lambda fact: "llm_unsure" if "11-15" in str(fact.raw.get("text")) else None)
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, plain, order=order).close()
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-02", f"desk-bot：越南那批订单的出货日子记下来吗？（待确认 {code}）", "我：对，有这回事。"))
    write(world, plain, order=order).close()
    assert [tuple(row) for row in world.rows("SELECT status, judged_by FROM facts")] == [("verified", "owner")]
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "对，有这回事。")]) == 0
    assert [tuple(row) for row in world.rows("SELECT decision, decided_at FROM review_queue")] == [(None, None)]
    assert [tuple(row) for row in world.rows("SELECT status, pending_reason, judged_by FROM facts")] == [
        ("pending", "manual_review", None)]


# ---------- 审阅第 5 条：整本重建照旧补回来的冻结事实 ----------


def test_a_brought_back_frozen_fact_is_still_the_owners(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    day = day_packet("2026-11-01", "我：越南那批订单 11-15 出货。")
    deliver(world, day)
    write(world, plain, order=ALL_OK).close()
    old = str(world.rows("SELECT fact_id FROM facts")[0]["fact_id"])
    freeze(world.settings, "prod", [old])
    changed = day.replace("11-15 出货", "下周出货").replace("privacy: normal", "privacy: normal\nrevision: 2")
    deliver(world, changed)
    write(world, plain, order=ALL_OK).close()  # 换了版本：冻结的那条没了说法，只剩冻结标记撑着
    path = exported_to(world, tmp_path)
    new = rebuilt(tmp_path, monkeypatch, plain, [changed])
    main(["review", "--library", "prod", "--import", str(path)])
    assert [tuple(row) for row in new.rows("SELECT fact_id, frozen, judged_by FROM facts")] == [(old, 1, "owner")]
    conn = open_ledger(new.settings.library("prod"))
    try:
        result = precheck.PrecheckGate().admit(conn, "prod", [GateItem("fact", old, ("business",), (), {}, "x")])
    finally:
        conn.close()
    assert result.verdicts[("fact", old)] is Verdict.ADMIT


# ---------- 审阅第 7 条：关掉以后重写窗口，开着时留下的转人工待确认 ----------


def test_switching_off_and_rewriting_closes_the_held_review(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship, order=UNSURE_1115).close()
    monkeypatch.delenv("JAVMEM_PRECHECK")
    world.execute("UPDATE packets SET extraction = 'queued'")
    world.execute("UPDATE fact_claims SET l4_version = 'l4-v6'")
    write(world, ship).close()
    assert [tuple(row) for row in world.rows("SELECT status, pending_reason FROM facts")] == [("verified", None)]
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE decided_at IS NULL")[0]["n"] == 0


# ---------- 第二遍审阅（804a911）提的：修第 3 条带出来的、第 1、2、3 条各漏的一处 ----------


BANK_ENTITY = {"key": "e2", "mention": "北桥银行", "name": "北桥银行", "type": "公司或机构"}  # 原话里没有这个名字


# ---------- 第 3 条修过以后：判过的那一处是推断，转人工的那一处是说定了的 ----------


def mixed_status(window):  # noqa: ANN001, ANN201
    found = []
    for line in window.targets:
        if "已经付了" in line.text:  # 说定了的（verified），判成拿不准、转人工
            found.append(l3_fact(window, line.text.rstrip("。"), "越南那批订单 11-15 出货，货款已付清",
                                 subject="e1", attribute="出货日期", value="11-15"))
        elif "11-15" in line.text:  # 推断的（inferred），本机认了
            found.append(l3_fact(window, line.text.rstrip("。"), "越南那批订单可能 11-15 出货",
                                 subject="e1", attribute="出货日期", value="11-15", certainty="inferred"))
    return {"entities": [ORDER_ENTITY], "facts": found}


def test_a_held_verified_claim_does_not_make_the_fact_verified(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单货款已经付了，11-15 出货。"),
            day_packet("2026-11-03", "desk-bot：越南那批订单应该是 11-15 出货。"))
    write(world, mixed_status, order=judged(lambda fact: "llm_unsure" if "货款" in str(fact.raw.get("text")) else None)).close()
    (fact,) = world.rows("SELECT content, status, stated, judged_by FROM facts")
    claims = [tuple(row) for row in world.rows("SELECT status, judge_hold FROM fact_claims ORDER BY claim_id")]
    assert fact["content"] == "越南那批订单可能 11-15 出货"
    assert fact["status"] == "inferred"  # 只有判过的那一处算数：它是推断的


# ---------- 第 3 条修过以后：转人工那一处才有的实体，主人后来说留下 ----------


def extra_entity(window):  # noqa: ANN001, ANN201
    found = []
    for line in window.targets:
        if "还是" in line.text:  # 后说的这一处多带了一个原话里没有的实体（北桥银行），判成拿不准、转人工
            found.append(l3_fact(window, line.text.rstrip("。"), "越南那批订单 11-15 出货（北桥银行）", subject="e1",
                                 attribute="出货日期", value="11-15",
                                 entities=[{"key": "e1", "role": "subject"}, {"key": "e2", "role": "mention"}]))
        elif "11-15" in line.text:
            found.append(l3_fact(window, line.text.rstrip("。"), "越南那批订单 11-15 出货", subject="e1", attribute="出货日期",
                                 value="11-15"))
    return {"entities": [ORDER_ENTITY, BANK_ENTITY], "facts": found}


def test_keeping_a_held_claim_with_its_own_entity(world: World, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
            day_packet("2026-11-03", "我：越南那批订单还是 11-15 出货。"))
    write(world, extra_entity, order=judged(lambda fact: "llm_unsure" if "北桥" in str(fact.raw.get("text")) else None)).close()
    entities = [row["canonical_name"] for row in world.rows("SELECT canonical_name FROM entities ORDER BY canonical_name")]
    held = json.loads(world.rows("SELECT entities FROM fact_claims WHERE judge_hold IS NOT NULL")[0]["entities"])
    review = world.rows("SELECT review_id FROM review_queue WHERE decided_at IS NULL")[0]["review_id"]
    code = main(["review", "--library", "prod", "--decide", str(review), "--keep", "new"])
    out = capsys.readouterr()
    assert code == 0  # 主人说留下要能定
    assert world.rows("SELECT decision FROM review_queue WHERE review_id = ?", review)[0]["decision"] == "keep_new"


# ---------- 第 1 条修过以后：关系那一路（判断 C 拿不准、开着进图前检查）主人定的留痕 ----------


def test_the_owners_trail_on_a_held_relation_survives_a_rebuild(world: World, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    day = day_packet("2026-11-01", "我：X1 票据在海港银行。")
    deliver(world, day)
    write(world, bank_extract, order=judged_relations("llm_unsure")).close()
    review = world.rows("SELECT review_id, claims FROM review_queue")[0]
    assert json.loads(review["claims"]).get("precheck") is True
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0
    trail = sorted((row["check_kind"], row["item_kind"], row["tier"], row["trusted"]) for row in world.rows("SELECT * FROM audit_log"))
    path = exported_to(world, tmp_path)
    new = rebuilt_world(tmp_path, monkeypatch)
    deliver(new, day)
    write(new, bank_extract, order=judged_relations("llm_unsure")).close()
    main(["review", "--library", "prod", "--import", str(path)])
    after = sorted((row["check_kind"], row["item_kind"], row["tier"], row["trusted"]) for row in new.rows("SELECT * FROM audit_log"))
    assert after == trail


def test_withdrawing_the_judged_claim_leaves_the_held_one(world: World, monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    """同上的账：撤回判过的那一处的证据句，事实只剩转人工的那一处（它带着已经删掉的实体）。"""
    from test_chat_answers import said

    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
            day_packet("2026-11-03", "我：越南那批订单还是 11-15 出货。"))
    write(world, extra_entity, order=judged(lambda fact: "llm_unsure" if "北桥" in str(fact.raw.get("text")) else None)).close()
    code = main(["withdraw", "--library", "prod", "--sentence", said(world, "越南那批订单 11-15 出货。")])
    out = capsys.readouterr()
    assert code == 0


@pytest.mark.parametrize("grouped", [True, False], ids=["grouped", "no_group"])
def test_a_frozen_held_fact_after_a_rebuild(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grouped: bool) -> None:
    """主人冻结了一条转人工的事实（第 2 条修过以后：放出来、待确认关掉），整本重建、导回以后还是不是这样。"""
    from test_precheck import ship, value_code

    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    extract = ship if grouped else plain
    order = judged(value_code({"11-15": "llm_unsure"})) if grouped else judged(lambda fact: "llm_unsure")
    day = day_packet("2026-11-01", "我：越南那批订单 11-15 出货。")
    deliver(world, day)
    write(world, extract, order=order).close()
    fact_id = world.rows("SELECT fact_id FROM facts")[0]["fact_id"]
    assert main(["freeze", "--library", "prod", "--fact", fact_id]) == 0
    before = [tuple(row) for row in world.rows("SELECT fact_id, frozen, status, pending_reason, judged_by FROM facts")]
    open_before = world.rows("SELECT count(*) AS n FROM review_queue WHERE decided_at IS NULL")[0]["n"]
    path = exported_to(world, tmp_path)
    new = rebuilt_world(tmp_path, monkeypatch)
    deliver(new, day)
    write(new, extract, order=order).close()
    main(["review", "--library", "prod", "--import", str(path)])
    after = [tuple(row) for row in new.rows("SELECT fact_id, frozen, status, pending_reason, judged_by FROM facts")]
    open_after = [row["kind"] for row in new.rows("SELECT kind FROM review_queue WHERE decided_at IS NULL")]
    assert after == before
    assert len(open_after) == open_before == 0
