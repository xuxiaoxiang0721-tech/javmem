"""entity_card、timeline、relation_at（J5.5，query-api 5.2 到 5.4、第 6 到 8 节）。样例都是编的（tests/query_world.py）。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from helpers import World, as_json
from query_world import AS_OF_TEXT, build_world

from javmem.query.tools import entity_card, relation_at, timeline

FOUR = ("content", "status", "valid_time", "evidence")
TOOLS = {"entity_card": entity_card, "timeline": timeline, "relation_at": relation_at}


@pytest.fixture(scope="module")
def qworld(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(tmp_path_factory.mktemp("query-tools"))


def call(world: World, tool: str, caller: str | None = "desk-bot", **arguments: object) -> dict:
    settings = replace(world.settings, caller=caller)
    return as_json(TOOLS[tool](arguments, settings=settings))


def results(answer: dict) -> list[dict]:
    """返回里所有的结果（四样都要有的那些）。"""
    found = []
    for name in ("items", "aliases", "relations", "recent"):
        found.extend(answer.get(name) or [])
    nearest = (answer.get("not_found") or {}).get("nearest") or {}
    found.extend(item for item in nearest.values() if item)
    return found


def entity_id(world: World, name: str) -> str:
    (row,) = world.rows("SELECT entity_id FROM entities WHERE canonical_name = ?", name)
    return row[0]


# ---------- entity_card ----------


def test_entity_card_for_an_alias(qworld: World) -> None:
    """01 p4 的例子“老周”。"""
    answer = call(qworld, "entity_card", entity="老周", recent_limit=2)
    assert (answer["ok"], answer["tool"], answer["library"], answer["as_of"]) == (True, "entity_card", "prod", AS_OF_TEXT)
    assert (answer["result"], answer["degraded"]) == ("hit", [])
    assert answer["entity"] == {"id": entity_id(qworld, "周建华"), "name": "周建华", "type": "人", "matched_alias": "老周"}
    assert [item["content"] for item in answer["aliases"]] == ["老周", "周总"]  # 不含当名字用的“周建华”，最早说到的在前
    assert [item["content"] for item in answer["relations"]] == ["周建华 负责 越南那批订单", "周建华 任职于 示例贸易"]
    assert len(answer["recent"]) == 2
    for item in results(answer):
        assert all(name in item for name in FOUR), item["id"]
    assert "items" not in answer and "next_cursor" not in answer


def test_entity_card_by_standard_name_or_id_has_no_matched_alias(qworld: World) -> None:
    by_name = call(qworld, "entity_card", entity="周建华")
    by_id = call(qworld, "entity_card", entity_id=entity_id(qworld, "周建华"))
    assert by_name["entity"]["matched_alias"] is None and by_id["entity"]["matched_alias"] is None
    assert by_name["entity"]["id"] == by_id["entity"]["id"]


def test_recent_is_current_facts_newest_first_and_limited(qworld: World) -> None:
    answer = call(qworld, "entity_card", entity="越南那批订单", recent_limit=20)
    contents = [item["content"] for item in answer["recent"]]
    assert "越南那批订单的定金到账" == contents[-1]  # 10 月 8 日，最早
    assert contents.index("越南那批订单的尾款可能暂缓") < contents.index("越南那批订单的定金到账")
    assert call(qworld, "entity_card", entity="越南那批订单", recent_limit=0)["recent"] == []
    relations = [item["content"] for item in answer["relations"]]
    assert relations == ["周建华 负责 越南那批订单"]  # 李明那一条已经结束，不算当前


def test_both_sides_of_a_frozen_conflict_are_returned(qworld: World) -> None:
    """4.3：冻结的原值还是 verified，新说法是 pending。"""
    answer = call(qworld, "entity_card", entity="越南那批订单", recent_limit=20)
    booking = {item["fact"]["value"]: item for item in answer["recent"] if item["fact"]["attribute"] == "订舱期限"}
    assert (booking["2026-11-12"]["status"], booking["2026-11-12"]["fact"]["frozen"]) == ("verified", True)
    assert (booking["2026-11-14"]["status"], booking["2026-11-14"]["pending_reason"]) == ("pending", "frozen_conflict")


def test_ambiguous_names_give_candidates_with_hints(qworld: World) -> None:
    answer = call(qworld, "entity_card", entity="周总")
    assert answer["result"] == "ambiguous"
    assert set(answer) == {"ok", "tool", "library", "as_of", "result", "degraded", "candidates"}
    assert sorted((c["name"], c["matched_alias"], c["hint"], c["type"]) for c in answer["candidates"]) == [
        ("周建华", "周总", "负责 越南那批订单", "人"),
        ("周海", "周总", "任职于 示例物流", "人"),
    ]
    for tool, extra in (("timeline", {"time_from": "2026-10-01"}), ("relation_at", {"relation": "负责", "at": "2026-11-01"})):
        assert call(qworld, tool, entity="周总", **extra)["result"] == "ambiguous"


def test_unknown_names_are_none_with_a_hint_to_try_raw(qworld: World) -> None:
    answer = call(qworld, "entity_card", entity="没有这个人")
    assert answer["result"] == "none"
    assert (answer["aliases"], answer["relations"], answer["recent"]) == ([], [], [])
    assert answer["not_found"] == {"reason": "no_entity", "searched": ["ledger"], "try": ["raw_search"], "message": "记忆里没有查到。"}


# ---------- timeline ----------


def test_timeline_since_october(qworld: World) -> None:
    """01 p4 的例子“越南订单 10 月以来”。"""
    answer = call(qworld, "timeline", entity="越南订单", time_from="2026-10-01")
    assert answer["result"] == "hit" and answer["entity"]["matched_alias"] == "越南订单"
    assert answer["range"] == {"from": "2026-10-01", "to": AS_OF_TEXT}
    shown = [(item["at"], item.get("change"), item["content"]) for item in answer["items"]]
    assert shown[0] == ("2026-10-08", None, "越南那批订单的定金到账")
    assert ("2026-11-09", "ended", "李明 负责 越南那批订单") in shown
    assert ("2026-11-09", "started", "周建华 负责 越南那批订单") in shown
    assert shown.index(("2026-11-09", "ended", "李明 负责 越南那批订单")) < shown.index(
        ("2026-11-09", "started", "周建华 负责 越南那批订单")
    )  # 文档的例子：同一天先结束、再开始
    assert all(item["at"] >= "2026-10" for item in answer["items"])  # 9 月 20 日开始的那一下不在这段时间里
    for item in answer["items"]:
        assert all(name in item for name in FOUR)
    assert answer["next_cursor"] is None


def test_timeline_filters_orders_and_pages(qworld: World) -> None:
    only_relations = call(qworld, "timeline", entity="越南那批订单", time_from="2026-01-01", include=["relation"])
    assert {item["kind"] for item in only_relations["items"]} == {"relation"}
    assert [item["change"] for item in only_relations["items"]] == ["started", "ended", "started"]
    backwards = call(qworld, "timeline", entity="越南那批订单", time_from="2026-01-01", include=["relation"], order="desc")
    assert [item["change"] for item in backwards["items"]] == ["started", "ended", "started"][::-1]
    first = call(qworld, "timeline", entity="越南那批订单", time_from="2026-01-01", limit=2)
    second = call(qworld, "timeline", entity="越南那批订单", time_from="2026-01-01", limit=2, cursor=first["next_cursor"])
    everything = call(qworld, "timeline", entity="越南那批订单", time_from="2026-01-01", limit=100)
    assert first["next_cursor"] == "c2"
    assert [i["id"] for i in first["items"] + second["items"]] == [i["id"] for i in everything["items"][:4]]


def test_timeline_includes_replaced_facts_with_who_replaced_them(qworld: World, tmp_path) -> None:
    """规矩题 3（主会话 11-18 照建议定）：timeline 带上被替代的事实，写明被谁替代（D66 的两个字段）。"""
    world = build_world(tmp_path / "w")
    world.execute("UPDATE facts SET superseded_by = (SELECT fact_id FROM facts WHERE attribute = '尾款'),"
                  " superseded_at = '2026-11-09T14:10:00+08:00' WHERE attribute = '定金'")
    answer = call(world, "timeline", entity="越南那批订单", time_from="2026-10-01")
    (deposit,) = [item for item in answer["items"] if item["content"] == "越南那批订单的定金到账"]
    assert deposit["fact"]["superseded_at"] == "2026-11-09T14:10:00+08:00" and deposit["fact"]["superseded_by"].startswith("F")
    card = call(world, "entity_card", entity="越南那批订单", recent_limit=20)
    assert "越南那批订单的定金到账" not in [item["content"] for item in card["recent"]]  # 当前情况只要现行的


def test_timeline_odd_arguments(qworld: World) -> None:
    """审阅第 9 条：include 写 null 当没写（和别的可选入参一样）；起点在现在以后、又没写终点（终点默认现在）是参数不对。"""
    assert call(qworld, "timeline", entity="老周", time_from="2026-10-01", include=None)["result"] == "hit"
    later = call(qworld, "timeline", entity="老周", time_from="2027-01-01")
    assert later["ok"] is False and later["error"]["code"] == "invalid_argument"
    assert call(qworld, "timeline", entity="老周", time_from="2027-01-01", time_to="2027-02-01")["result"] == "none"


def test_timeline_with_nothing_in_range(qworld: World) -> None:
    answer = call(qworld, "timeline", entity="越南那批订单", time_from="2025-01-01", time_to="2025-12-31")
    assert answer["result"] == "none" and answer["items"] == []
    assert answer["not_found"]["reason"] == "no_events_in_range" and answer["entity"]["name"] == "越南那批订单"
    assert answer["range"] == {"from": "2025-01-01", "to": "2025-12-31"}


# ---------- relation_at ----------


def test_relation_at_october_first(qworld: World) -> None:
    """01 p4 的例子“10 月 1 日越南订单谁负责”。"""
    answer = call(qworld, "relation_at", entity="越南订单", relation="负责", at="2026-10-01", role="object")
    assert answer["result"] == "hit" and answer["at"] == "2026-10-01"
    (item,) = answer["items"]
    assert (item["content"], item["time_match"]) == ("李明 负责 越南那批订单", "certain")
    assert item["relation"]["state"] == "ended"  # 现在的状态，不是那时候的
    assert all(name in item for name in FOUR)
    assert "next_cursor" not in answer


def test_relation_at_on_the_handover_day_and_by_role(qworld: World) -> None:
    answer = call(qworld, "relation_at", entity="越南订单", relation="负责", at="2026-11-09")
    assert [(item["content"], item["time_match"]) for item in answer["items"]] == [("周建华 负责 越南那批订单", "certain")]
    as_subject = call(qworld, "relation_at", entity="越南订单", relation="负责", at="2026-10-01", role="subject")
    assert as_subject["result"] == "none"  # 订单是客体


def test_relation_at_coarse_start_is_only_possible(qworld: World) -> None:
    """只知道“10 月起”，问 10 月 1 日：possible；问 11 月 1 日：certain。"""
    early = call(qworld, "relation_at", entity="周海", relation="任职于", at="2026-10-01")
    later = call(qworld, "relation_at", entity="周海", relation="任职于", at="2026-11-01")
    assert [item["time_match"] for item in early["items"]] == ["possible"]
    assert [item["time_match"] for item in later["items"]] == ["certain"]
    exact = call(qworld, "relation_at", entity="周海", relation="任职于", at="2026-11-01T08:00:00+08:00")
    assert [item["time_match"] for item in exact["items"]] == ["certain"]
    # 审阅第 5 条：按天问时，10 月的最后一天它已经开始了（哪怕是那天才开始的，按天比也算那一天在）
    last_day = call(qworld, "relation_at", entity="周海", relation="任职于", at="2026-10-31")
    assert [item["time_match"] for item in last_day["items"]] == ["certain"]
    middle = call(qworld, "relation_at", entity="周海", relation="任职于", at="2026-10-15")
    assert [item["time_match"] for item in middle["items"]] == ["possible"]
    moment = call(qworld, "relation_at", entity="周海", relation="任职于", at="2026-10-31T08:00:00+08:00")
    assert [item["time_match"] for item in moment["items"]] == ["possible"]  # 问的是 31 日早上 8 点：那天什么时候开始的说不准


def test_relation_at_before_any_record_gives_nearest(qworld: World) -> None:
    answer = call(qworld, "relation_at", entity="越南订单", relation="负责", at="2026-09-01", role="object")
    assert answer["result"] == "none" and answer["items"] == []
    found = answer["not_found"]
    assert (found["reason"], found["searched"], found["try"]) == ("no_relation_at_time", ["ledger"], ["raw_search"])
    assert found["nearest"]["before"] is None
    assert found["nearest"]["after"]["content"] == "李明 负责 越南那批订单"
    assert "time_match" not in found["nearest"]["after"]


def test_unknown_relation_lists_the_allowed_ones(qworld: World) -> None:
    answer = call(qworld, "relation_at", entity="越南订单", relation="管理", at="2026-10-01")
    assert answer == {
        "ok": False,
        "tool": "relation_at",
        "error": {
            "code": "unknown_relation",
            "message": "关系名不在关系清单里。",
            "allowed": ["持有", "托管于", "负责", "任职于", "客户是", "亲属", "隶属于"],  # D210 加了隶属于
        },
    }


# ---------- 越权（D33）、测试库 ----------

CALLS = {
    "entity_card": {"entity": "老周"},
    "timeline": {"entity": "老周", "time_from": "2026-10-01"},
    "relation_at": {"entity": "老周", "relation": "负责", "at": "2026-11-09"},
}


@pytest.mark.parametrize("tool", sorted(CALLS))
@pytest.mark.parametrize("caller", [None, "no-such-bot"])
def test_unknown_identity_is_an_error_for_every_tool(qworld: World, tool: str, caller: str | None) -> None:
    assert call(qworld, tool, caller, **CALLS[tool]) == {
        "ok": False, "tool": tool, "error": {"code": "unknown_identity", "message": "不认识这个身份。"},
    }


@pytest.mark.parametrize("tool", sorted(CALLS))
def test_asking_for_a_partition_you_cannot_see_is_forbidden(qworld: World, tool: str) -> None:
    answer = call(qworld, tool, **CALLS[tool], partitions=["finance"])
    assert answer == {"ok": False, "tool": tool, "error": {"code": "forbidden", "message": "这个身份不能查分区 finance。"}}
    allowed = call(qworld, tool, **CALLS[tool], partitions=["business"])
    assert allowed["ok"] is True


def test_content_you_cannot_see_does_not_exist(qworld: World) -> None:
    """D33 第三种：管账的包对 desk-bot 当不存在；books-bot 照常看得到。"""
    hidden = call(qworld, "entity_card", entity="工资单")
    assert hidden["result"] == "none" and hidden["not_found"]["reason"] == "no_entity"
    payroll = entity_id(qworld, "十一月工资单")
    assert call(qworld, "entity_card", entity_id=payroll)["result"] == "none"
    seen = call(qworld, "entity_card", "books-bot", entity="工资单")
    assert seen["result"] == "hit" and seen["recent"][0]["scope"] == {"partitions": ["finance"], "sensitive": ["payroll"]}


def test_denials_are_logged_without_the_query(qworld: World) -> None:
    call(qworld, "entity_card", "no-such-bot", entity="只查一次的名字")
    call(qworld, "timeline", entity="只查一次的名字", time_from="2026-10-01", partitions=["finance"])
    text = (qworld.data / "logs" / "query-denied.log").read_text(encoding="utf-8")
    assert '"tool": "entity_card", "code": "unknown_identity"' in text
    assert '"tool": "timeline", "code": "forbidden"' in text
    assert "只查一次的名字" not in text


def test_a_test_identity_only_sees_the_test_library(qworld: World) -> None:
    trial = call(qworld, "entity_card", "trial-bot", entity="越南那批", recent_limit=5)
    assert trial["library"] == "test" and trial["result"] == "hit"
    # 测试库里只有“越南那批”这个叫法有出处：L3 补全的“越南那批订单”这个身份看不到，名字、文字都写“越南那批”（规矩题 1）
    assert trial["entity"]["name"] == "越南那批"
    assert [item["content"] for item in trial["recent"]] == ["越南那批改到 11 月 20 日出货"]
    assert call(qworld, "entity_card", "trial-bot", entity="老周")["result"] == "none"  # 正式库里的人测试库里没有
    prod = call(qworld, "entity_card", entity="越南那批订单", recent_limit=20)
    assert "越南那批订单改到 11 月 20 日出货" not in [item["content"] for item in prod["recent"]]


def test_quarantined_facts_are_not_returned(qworld: World) -> None:
    assert qworld.rows("SELECT count(*) FROM quarantine")[0][0] >= 1
    answer = call(qworld, "entity_card", entity="越南那批订单", recent_limit=20)
    assert not any("邮件里提出" in item["content"] for item in answer["recent"])


# ---------- 入参 ----------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("entity_card", {}),
        ("entity_card", {"entity": "老周", "entity_id": "E1"}),
        ("entity_card", {"entity": "  "}),
        ("entity_card", {"entity": 3}),
        ("entity_card", {"entity": "老周", "recent_limit": 21}),
        ("entity_card", {"entity": "老周", "recent_limit": True}),
        ("entity_card", {"entity": "老周", "no_such_argument": 1}),
        ("entity_card", {"entity": "老周", "partitions": "business"}),
        ("timeline", {"entity": "老周"}),
        ("timeline", {"entity": "老周", "time_from": "上周"}),
        ("timeline", {"entity": "老周", "time_from": "2026-10-01", "time_to": "2026-09-01"}),
        ("timeline", {"entity": "老周", "time_from": "2026-10-01", "include": []}),
        ("timeline", {"entity": "老周", "time_from": "2026-10-01", "include": ["alias"]}),
        ("timeline", {"entity": "老周", "time_from": "2026-10-01", "order": "up"}),
        ("timeline", {"entity": "老周", "time_from": "2026-10-01", "limit": 101}),
        ("timeline", {"entity": "老周", "time_from": "2026-10-01", "cursor": "x"}),
        ("relation_at", {"entity": "老周", "at": "2026-10-01"}),
        ("relation_at", {"entity": "老周", "relation": "负责"}),
        ("relation_at", {"entity": "老周", "relation": "负责", "at": "10 月 1 日"}),
        ("relation_at", {"entity": "老周", "relation": "负责", "at": "2026-10-01", "role": "both"}),
    ],
)
def test_invalid_arguments(qworld: World, tool: str, arguments: dict) -> None:
    answer = TOOLS[tool](arguments, settings=replace(qworld.settings, caller="desk-bot"))
    assert answer["ok"] is False and set(answer) == {"ok", "tool", "error"}
    assert answer["error"]["code"] == "invalid_argument", answer


def test_unavailable_when_the_ledger_is_missing(tmp_path) -> None:
    world = build_world(tmp_path / "w")
    (world.data / "ledger" / "prod.db").rename(world.data / "ledger" / "gone.db")
    answer = call(world, "entity_card", entity="老周")
    assert answer["error"]["code"] == "unavailable"
    assert not (world.data / "ledger" / "prod.db").exists()  # 查询不建库
