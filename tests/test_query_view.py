"""账本视图（J5.5，javmem.query.view）：看得到的规矩、名字、每条结果的四样和别的字段、证据。样例都是编的。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from helpers import SHANGHAI, World, as_json
from query_world import AS_OF, build_world
from test_write import day_packet, deliver, l3_fact, write

from javmem.access import load_identity
from javmem.ledger import connect_readonly
from javmem.pipeline.merge import entity_id_of, name_key
from javmem.query.tools import memory_search
from javmem.query.view import NO_NAME, LedgerView, mask_names, period
from javmem.withdraw import withdraw


@pytest.fixture(scope="module")
def qworld(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(tmp_path_factory.mktemp("query-view"))


@contextmanager
def view_of(world: World, caller: str = "desk-bot", *, library: str | None = None, **options: object) -> Iterator[LedgerView]:
    settings = world.as_caller(caller).settings
    identity = load_identity(settings)
    conn = connect_readonly(settings.library(library or identity.env).db_path)
    try:
        conn.execute("BEGIN")
        yield LedgerView(conn, identity, settings, as_of=settings.now(), **options)  # type: ignore[arg-type]
    finally:
        conn.close()


def one(view: LedgerView, sql: str, *params: object) -> sqlite3.Row:
    (row,) = view.conn.execute(sql, params).fetchall()
    return row


# ---------- 时间段 ----------


def test_period_by_precision() -> None:
    tz = timezone(timedelta(hours=8))

    def span(text: str) -> tuple[str, str]:
        found = period(text, tz)
        assert found is not None
        return tuple(datetime.fromtimestamp(x, tz).isoformat() for x in found)  # type: ignore[return-value]

    assert span("2026-11-09") == ("2026-11-09T00:00:00+08:00", "2026-11-10T00:00:00+08:00")
    assert span("2026-11") == ("2026-11-01T00:00:00+08:00", "2026-12-01T00:00:00+08:00")
    assert span("2026-12") == ("2026-12-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert span("2026-Q4") == ("2026-10-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert span("2026-Q1") == ("2026-01-01T00:00:00+08:00", "2026-04-01T00:00:00+08:00")
    assert span("2026") == ("2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert span("2026-11-09T10:30+07:00") == ("2026-11-09T11:30:00+08:00", "2026-11-09T11:31:00+08:00")
    assert span("2026-11-09T10:30") == ("2026-11-09T10:30:00+08:00", "2026-11-09T10:31:00+08:00")
    for bad in (None, "", "下周", "2026-13", "2026-02-30", "2026-Q5"):
        assert period(bad, tz) is None


# ---------- 看得到的 ----------


def visible_attributes(view: LedgerView) -> set[str]:
    conditions, params = view.fact_conditions("f")
    return {row[0] for row in view.conn.execute(f"SELECT f.attribute FROM facts AS f WHERE {' AND '.join(conditions)}", params)}


def test_facts_follow_partitions_and_every_sensitive_tag(qworld: World) -> None:
    """管账的包是 finance 分区、payroll 标记：desk-bot 分区就不对，books-bot 两样都能看（文档 3.3）。测试身份开的是测试库。"""
    for caller, seen in (("desk-bot", False), ("books-bot", True), ("owner-console", True), ("trial-bot", False)):
        with view_of(qworld, caller) as view:
            assert ("发放时间" in visible_attributes(view)) is seen, caller
    with view_of(qworld, "trial-bot") as view:
        assert visible_attributes(view) == {"出货日期"}  # 测试库里只有测试包的那一条


def test_requested_partitions_narrow_what_is_seen(qworld: World) -> None:
    with view_of(qworld, "owner-console", partitions=["finance"]) as view:
        conditions, params = view.fact_conditions("f")
        rows = view.conn.execute(f"SELECT f.attribute FROM facts AS f WHERE {' AND '.join(conditions)}", params).fetchall()
        assert {row[0] for row in rows} == {"发放时间"}
        assert view.find_by_name("老周") == []  # 别名的出处都在 business 的包里


def test_an_entity_is_seen_through_facts_relations_or_aliases(qworld: World) -> None:
    with view_of(qworld, "desk-bot") as view:
        payroll = one(view, "SELECT entity_id FROM entities WHERE canonical_name = '十一月工资单'")[0]
        zhou = one(view, "SELECT entity_id FROM entities WHERE canonical_name = '周建华'")[0]
        assert view.entity_visible(zhou) and not view.entity_visible(payroll)
        assert view.find_by_id(payroll) is None
    with view_of(qworld, "books-bot") as view:
        assert view.entity_visible(payroll) and view.find_by_id(payroll) == payroll


def test_withdrawn_sentences_are_not_evidence_even_if_a_row_still_points_at_them(qworld: World, tmp_path) -> None:
    """撤回会去掉证据；这里直接把一句标成撤回（不经撤回命令），账本视图照样不给它。"""
    world = build_world(tmp_path / "w")
    with view_of(world) as view:
        row = one(view, "SELECT * FROM facts WHERE attribute = '定金'")
        assert view.fact_item(row)["evidence_total"] == 1
    world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-10T00:00:00+08:00' WHERE text LIKE '%定金到账%'")
    with view_of(world) as view:
        item = view.fact_item(one(view, "SELECT * FROM facts WHERE attribute = '定金'"))
        assert (item["evidence"], item["evidence_total"]) == ([], 0)


# ---------- 名字 ----------


def test_standard_names_with_visible_evidence_or_from_the_lexicon_are_shown(qworld: World) -> None:
    with view_of(qworld) as view:
        names = {row[0]: view.entity_name(row[1]) for row in view.conn.execute("SELECT canonical_name, entity_id FROM entities")}
        assert names["周建华"] == "周建华"  # 原文里有“老周就是周建华”
        assert names["李明"] == "李明"  # 原文里没有“李明”，可它是别名表里写的标准名
        assert names["越南那批订单"] == "越南那批订单"


def test_a_standard_name_without_evidence_falls_back_to_the_earliest_alias(qworld: World) -> None:
    no_lexicon = replace(qworld, settings=replace(qworld.settings, lexicon_file=None))
    with view_of(no_lexicon) as view:
        liming = one(view, "SELECT entity_id FROM entities WHERE canonical_name = '李明'")[0]
        assert view.entity_name(liming) == "小李"
        assert [m.entity_id for m in view.find_by_name("李明")] == []  # 看不到的名字对不上
        assert [(m.entity_id, m.matched_alias) for m in view.find_by_name("小李")] == [(liming, "小李")]


ZHOU = {"key": "e1", "mention": "老周", "name": "周建华", "type": "人"}


def zhou_facts(window):
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        if text == "老周就是周建华":
            found.append(l3_fact(window, text, "周建华也叫老周", subject="e1", attribute="称呼", value="老周"))
        if text == "老周说舱位订好了":
            found.append(l3_fact(window, text, "周建华说舱位订好了", subject="e1", attribute="舱位", value="订好了"))
    return {"entities": [ZHOU], "facts": found}


def test_a_standard_name_whose_evidence_was_withdrawn_is_not_shown(world: World) -> None:
    """撤回了“老周就是周建华”那一包，别的包里“老周”的事实不能把全名带出来。"""
    first = day_packet("2026-11-01", "我：老周就是周建华。")
    deliver(world, first, day_packet("2026-11-02", "我：老周说舱位订好了。"))
    write(world, zhou_facts).close()
    world = world.as_caller("desk-bot")
    with view_of(world) as view:
        zhou = one(view, "SELECT entity_id FROM entities")[0]
        assert view.entity_name(zhou) == "周建华"
    withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
    with view_of(world) as view:
        assert view.entity_name(zhou) == "老周"
        assert view.find_by_name("周建华") == []
        item = view.fact_item(one(view, "SELECT * FROM facts"))
        assert item["fact"]["subject"] == {"id": zhou, "name": "老周"}
        # 审阅第 2 条：事实的文字是 L3 写的，里面还写着全名：换成看得到的叫法
        assert item["content"] == "老周说舱位订好了"
    found = memory_search({"query": "周建华"}, settings=world.settings)
    assert found["result"] == "none"
    assert "周建华" not in json.dumps(memory_search({"query": "舱位"}, settings=world.settings), ensure_ascii=False)


def test_without_any_visible_name_the_standard_name_is_not_used_as_a_fallback(world: World) -> None:
    """审阅第 1 条：两个叫法的出处都只在撤回的那一句里，实体还靠别的事实看得到。名字不能退回去写标准名，
    拿标准名也对不上（3.2：标准名换成看得到的别名；第 9 节：撤回的内容查不到）。"""
    deliver(world, day_packet("2026-11-01", "我：老周就是周建华。", "我：老周说舱位订好了。"))
    write(world, zhou_facts).close()
    world = world.as_caller("desk-bot")
    withdraw(world.settings, "prod", sentences=["2026-11-01_2330_desk-bot#1.1.1"])
    with view_of(world) as view:
        zhou = one(view, "SELECT entity_id FROM entities")[0]
        assert view.entity_visible(zhou)
        assert view.entity_name(zhou) == NO_NAME
        assert view.find_by_name("周建华") == [] and view.find_by_name("老周") == []
        assert view.find_by_id(zhou) == zhou


def test_mask_names_works_in_the_l5_spelling_and_keeps_visible_names_whole() -> None:
    """复审 A、B、C：照比对写法找（空格、大小写、ß、İ 都认得出），换的是原文里对上的那一段；藏起来的名字整个落在
    看得到的名字里面的不换（“周建”在“周建华”里），看得到的名字落在藏起来的里面的照换（“Mai”在“Tran Mai”里）。"""
    assert mask_names("周建华和周建一起订舱位", {"周建": "老周"}, {"周建华"}) == "周建华和老周一起订舱位"
    assert mask_names("Tran Mai 说舱位订好了", {name_key("Tran Mai"): "Mai"}, {"mai"}) == "Mai 说舱位订好了"
    assert mask_names("周 建华说的", {"周建华": "老周"}, set()) == "老周说的"
    assert mask_names("Strauß sagte ja", {name_key("Strauß"): "S."}, set()) == "S. sagte ja"
    assert mask_names("İzmir'de buluştuk", {name_key("İzmir"): "某地"}, set()) == "某地'de buluştuk"
    assert mask_names("没有要换的", {"周建": "老周"}, set()) == "没有要换的"
    assert mask_names("周建周建", {"周建": "老周"}, {"老周"}) == "老周老周"
    assert mask_names("", {"周建": "老周"}, set()) == ""


def test_mask_names_keeps_words_and_straddled_visible_names_whole() -> None:
    """第二遍核查：英文的叫法要在词的边界上（“email”里的“mai”、“Linda”里的“li”、跨词凑出来的“maia”都不换）；
    藏起来的叫法和看得到的名字只叠住一部分的也不换（不然看得到的“周建”被拦腰换掉）。"""
    assert mask_names("email me", {"mai": "X"}, set()) == "email me"
    assert mask_names("deliver to Linda", {"li": "X"}, set()) == "deliver to Linda"
    assert mask_names("Tran Mai and", {"maia": "X"}, set()) == "Tran Mai and"
    assert mask_names("Mai said yes", {"mai": "M."}, set()) == "M. said yes"
    assert mask_names("Tran Mai 说的", {name_key("Tran Mai"): "Mai"}, {"mai"}) == "Mai 说的"
    assert mask_names("周建华说", {"建华": "X"}, {"周建"}) == "周建华说"


ZHOU_LATIN = {"key": "e1", "mention": "Mai", "name": "Tran Mai", "type": "人"}


def latin_facts(window):
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        if text == "Mai 就是 Tran Mai":
            found.append(l3_fact(window, text, "Tran Mai 也叫 Mai", subject="e1", attribute="称呼", value="Mai"))
        if text == "Mai 说舱位订好了":
            found.append(l3_fact(window, text, "Tran Mai 说舱位订好了", subject="e1", attribute="舱位", value="订好了"))
    return {"entities": [ZHOU_LATIN], "facts": found}


def test_a_withdrawn_latin_name_with_a_space_is_replaced_too(world: World) -> None:
    """复审 B：撤回以后只剩编号来历那一列的“tranmai”（去了空格、小写），原文里写的是“Tran Mai”，也要换掉。"""
    deliver(world, day_packet("2026-11-01", "我：Mai 就是 Tran Mai。"), day_packet("2026-11-02", "我：Mai 说舱位订好了。"))
    write(world, latin_facts).close()
    world = world.as_caller("desk-bot")
    withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
    with view_of(world) as view:
        item = view.fact_item(one(view, "SELECT * FROM facts"))
    assert item["content"] == "Mai 说舱位订好了" and item["fact"]["subject"]["name"] == "Mai"
    # 问“Tran Mai”里含着看得到的“Mai”，认得出实体，可回的字里没有“Tran Mai”；只问“Tran”对不上（只在撤回了的那一包里）
    both = memory_search({"query": "Tran Mai"}, settings=world.settings)
    assert "tran" not in json.dumps(both, ensure_ascii=False).casefold()
    assert memory_search({"query": "Tran"}, settings=world.settings)["result"] == "none"


def test_names_match_by_the_l5_spelling(qworld: World) -> None:
    with view_of(qworld) as view:
        zhou = one(view, "SELECT entity_id FROM entities WHERE canonical_name = '周建华'")[0]
        for text in ("周建华", " 周建华 ", "周 建华", "周建华。"):
            assert [(m.entity_id, m.matched_alias) for m in view.find_by_name(text)] == [(zhou, None)], text
        assert view.find_by_name("周") == []  # 不是包含就算
        assert view.find_by_name("") == []


def test_merged_old_ids_follow_the_redirect_only_from_a_visible_name(qworld: World, tmp_path) -> None:
    """合并以前的旧编号照合并以后的答（第 5 节）；可旧编号是名字算出来的（审阅第 3 条），只认这个身份看得到的名字算出来
    的那种：不然猜一个看不到的旧名字、算出编号来，就能试出它并到了谁身上。"""
    world = build_world(tmp_path / "w")
    with view_of(world) as view:
        zhou = one(view, "SELECT entity_id FROM entities WHERE canonical_name = '周建华'")[0]
    seen_old = entity_id_of("人", name_key("老周"))  # “老周”是看得到的别名
    unseen_old = entity_id_of("人", name_key("周大炮"))  # 这个身份没见过的名字
    for old in (seen_old, unseen_old, "E0000000000000old"):
        world.execute("INSERT INTO entity_redirects (old_id, new_id, merge_id) VALUES (?, ?, 'M1')", old, zhou)
    with view_of(world) as view:
        assert view.find_by_id(seen_old) == zhou
        assert view.find_by_id(unseen_old) is None
        assert view.find_by_id("E0000000000000old") is None
        assert view.find_by_id("E00000000000none") is None
        assert view.find_by_id(zhou) == zhou


# ---------- 每条结果 ----------

FOUR = ("content", "status", "valid_time", "evidence")


def test_fact_item_has_the_four_things_and_the_fact_fields(qworld: World) -> None:
    with view_of(qworld) as view:
        item = as_json(view.fact_item(one(view, "SELECT * FROM facts WHERE attribute = '定金'")))
    assert all(name in item for name in FOUR)
    assert item["id"].startswith("F") and item["kind"] == "fact"
    assert item["content"] == "越南那批订单的定金到账" and item["status"] == "verified" and "pending_reason" not in item
    assert item["valid_time"] == {"from": "2026-10-08", "to": None, "precision": "day"}
    assert item["evidence"] == [
        {
            "packet_id": "2026-10-08_2330_desk-bot", "revision": 1, "quote": "越南订单的定金到账了。",
            "speaker": "我", "speaker_role": "owner", "said_at": "2026-10-08T15:20:00+08:00",
            "source": "desk-bot", "channel": "chat", "origin": "said", "position": {"section": 1, "line": 1, "sentence": 1},
        }
    ]
    assert item["evidence_total"] == 1 and item["scope"] == {"partitions": ["business"], "sensitive": []}
    assert item["secondhand"] is False
    assert item["fact"]["subject"]["name"] == "越南那批订单"
    assert (item["fact"]["attribute"], item["fact"]["value"], item["fact"]["frozen"]) == ("定金", "已到账", False)
    assert "superseded_by" not in item["fact"]


def test_pending_items_carry_the_reason(qworld: World) -> None:
    with view_of(qworld) as view:
        rows = view.conn.execute("SELECT * FROM facts WHERE attribute = '订舱期限' ORDER BY as_of_epoch").fetchall()
        frozen, new = (view.fact_item(row) for row in rows)
    assert (frozen["status"], frozen["fact"]["frozen"]) == ("verified", True) and "pending_reason" not in frozen
    assert (new["status"], new["pending_reason"]) == ("pending", "frozen_conflict")


def test_relation_item_fields_and_replacement(qworld: World) -> None:
    with view_of(qworld) as view:
        rows = {row["subject_entity_id"]: row for row in view.conn.execute("SELECT * FROM relations WHERE relation_type = '负责'")}
        names = {key: view.entity_name(key) for key in rows}
        items = {names[key]: as_json(view.relation_item(row)) for key, row in rows.items()}
    old, new = items["李明"], items["周建华"]
    assert old["content"] == "李明 负责 越南那批订单" and all(name in old for name in FOUR)
    assert old["valid_time"] == {"from": "2026-09-20", "to": "2026-11-09", "precision": "day"}
    assert old["relation"]["state"] == "ended" and old["relation"]["superseded_by"] == new["id"]
    assert new["relation"]["replaces"] == old["id"] and new["relation"]["state"] == "active"
    assert new["relation"]["pause_condition"] is None
    assert [e["quote"] for e in old["evidence"]] == ["越南那批订单还是小李跟。", "越南那批订单交给小李跟。"]  # 新的在前


def test_relation_state_is_now_not_when_it_was_written(qworld: World) -> None:
    """结束日子整段过去了的，现在答 ended，账本不改。"""
    later = qworld.at(datetime(2027, 1, 1, tzinfo=SHANGHAI))
    with view_of(qworld) as now_view, view_of(later) as later_view:
        row = one(now_view, "SELECT * FROM relations WHERE relation_type = '任职于' AND valid_from = '2026-10'")
        assert now_view.relation_state(row) == "active"
        patched = dict(row)
        patched["valid_to"] = "2026-12"
        assert later_view.relation_state(patched) == "ended"  # type: ignore[arg-type]
        assert now_view.relation_state(patched) == "active"  # type: ignore[arg-type]


def test_alias_items_have_unknown_time(qworld: World) -> None:
    with view_of(qworld) as view:
        row = one(view, "SELECT * FROM aliases WHERE alias = '老周'")
        item = view.alias_item(row)
    assert item["kind"] == "alias" and item["content"] == "老周" and item["status"] == "verified"
    assert item["valid_time"] == {"from": None, "to": None, "precision": "unknown"}
    assert item["evidence_total"] == 2 and item["scope"] == {"partitions": ["business"], "sensitive": []}


SAID = {"key": "e1", "mention": "老周", "name": "周建国", "type": "人"}


def said_often(window):
    found = []
    for line in window.targets:
        if "老周负责越南那批订单" in line.text:
            found.append(l3_fact(window, line.text.rstrip("。"), "周建国负责越南那批订单", subject="e1",
                                 attribute="负责的订单", value="越南那批订单"))
    return {"entities": [SAID], "facts": found}


def test_evidence_is_capped_at_five_newest_first_and_counted(world: World) -> None:
    days = [f"2026-11-{day:02d}" for day in range(1, 8)]
    deliver(world, *(day_packet(day, "我：老周负责越南那批订单。") for day in days))
    write(world, said_often).close()
    with view_of(world.as_caller("desk-bot")) as view:
        item = view.fact_item(one(view, "SELECT * FROM facts"))
    assert item["evidence_total"] == 7
    assert [e["said_at"][:10] for e in item["evidence"]] == days[::-1][:5]


def test_secondhand_is_about_the_visible_evidence(qworld: World) -> None:
    """转发的那一句（[转发 自 小李]）是转述：secondhand；主人自己说的不是。"""
    with view_of(qworld) as view:
        relayed = view.fact_item(one(view, "SELECT * FROM facts WHERE attribute = '出货日期'"))
        own = view.fact_item(one(view, "SELECT * FROM facts WHERE attribute = '定金'"))
    assert (relayed["secondhand"], relayed["evidence"][0]["origin"]) == (True, "relayed")
    assert own["secondhand"] is False
    assert json.loads(json.dumps(relayed["scope"])) == {"partitions": ["business"], "sensitive": []}
