"""和这个实体有关的待确认（D83，J5.5 最后一个提交；接口文档 5.2 加的那一段等主人点头）。样例都是编的。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from helpers import QUERY_API, World, as_json, fenced
from query_world import FINANCE_FIRST, FINANCE_SECOND, WHO_IS_ZHOU, build_world, write_all, extract

from javmem.query.pending import ASK, MAX_PENDING
from javmem.query.tools import entity_card
from javmem.review import freeze, open_items
from javmem.withdraw import withdraw

FOUR = ("content", "status", "valid_time", "evidence")


@pytest.fixture(scope="module")
def qworld(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(tmp_path_factory.mktemp("query-pending"))


def card(world: World, caller: str = "desk-bot", **arguments: object) -> dict:
    return as_json(entity_card(arguments, settings=replace(world.settings, caller=caller)))


def codes(world: World) -> dict[str, str]:
    conn = world.ledger("prod")
    try:
        return {item.code: item.kind for item in open_items(conn, world.settings)}
    finally:
        conn.close()


def test_a_frozen_conflict_is_pending_for_every_entity_it_touches(qworld: World) -> None:
    order = card(qworld, entity="越南那批订单")
    (entry,) = order["pending"]
    assert entry["kind"] == "frozen_conflict" and entry["ask"] == ASK["frozen_conflict"]
    assert codes(qworld) == {entry["code"]: "frozen_conflict"}  # 和 javmem review 列出来的编号一样
    assert [(item["side"], item["fact"]["value"]) for item in entry["items"]] == [("frozen", "2026-11-12"), ("new", "2026-11-14")]
    for item in entry["items"]:
        assert all(name in item for name in FOUR)
    assert card(qworld, entity="老周")["pending"] == order["pending"]  # 周建华是那两条里提到的人
    assert card(qworld, entity="示例贸易")["pending"] == []


def test_pending_is_only_on_a_hit(qworld: World) -> None:
    assert "pending" not in card(qworld, entity="没有这个人")
    assert "pending" not in card(qworld, entity="周总")  # 对到两个，只有候选
    assert card(qworld, "trial-bot", entity="越南那批")["pending"] == []  # 测试库里没有待确认


def test_decided_reviews_are_not_given(tmp_path) -> None:
    world = build_world(tmp_path / "w")
    world.execute("UPDATE review_queue SET decided_at = '2026-11-10T08:00:00+08:00', decision = 'keep_frozen'")
    assert card(world, entity="越南那批订单")["pending"] == []


def test_at_most_five_oldest_first(tmp_path) -> None:
    world = build_world(tmp_path / "w")
    (row,) = world.rows("SELECT kind, claims, quotes FROM review_queue")
    for day in range(1, 8):
        world.execute("INSERT INTO review_queue (kind, claims, quotes, created_at, code) VALUES (?, ?, ?, ?, ?)",
                      row["kind"], row["claims"], row["quotes"], f"2026-10-{day:02d}T08:00:00+08:00", f"Q00000{day}")
    pending = card(world, entity="越南那批订单")["pending"]
    assert len(pending) == MAX_PENDING
    assert [entry["code"] for entry in pending] == [f"Q00000{day}" for day in range(1, 6)]


def test_reviews_in_scopes_you_cannot_see_are_not_given(tmp_path) -> None:
    """管账的那一边的冲突：desk-bot 看不到（分区不对），books-bot 看得到；入参 partitions 也照样滤。"""
    world = build_world(tmp_path / "w")
    world.drop("inbox/books-bot", FINANCE_FIRST)
    world.intake()
    write_all(world, extract, "prod")
    (first,) = world.rows("SELECT fact_id FROM facts WHERE attribute = '尾款金额'")
    freeze(world.settings, "prod", [first["fact_id"]])
    world.drop("inbox/books-bot", FINANCE_SECOND)
    world.intake()
    write_all(world, extract, "prod")
    assert sorted(codes(world).values()) == ["frozen_conflict", "frozen_conflict"]
    desk = card(world, entity="越南那批订单")["pending"]
    books = card(world, "books-bot", entity="越南那批订单")["pending"]
    assert [entry["items"][0]["scope"]["partitions"] for entry in desk] == [["business"]]
    assert sorted(entry["items"][0]["scope"]["partitions"][0] for entry in books) == ["business", "finance"]
    only = card(world, "owner-console", entity="越南那批订单", partitions=["finance"])["pending"]
    assert [entry["items"][0]["fact"]["attribute"] for entry in only] == ["尾款金额"]


def test_withdrawn_frozen_facts_are_not_asked_about(tmp_path) -> None:
    """冻结的那一条证据都撤回了：内容撤回了，不再给出去。"""
    world = build_world(tmp_path / "w")
    withdraw(world.settings, "prod", packets=["2026-11-09_2330_desk-bot"])
    kinds = {row["kind"] for row in world.rows("SELECT kind FROM review_queue WHERE decided_at IS NULL")}
    pending = card(world, entity="越南那批订单")["pending"]
    assert all(entry["kind"] != "evidence_withdrawn" for entry in pending)
    for entry in pending:
        for item in entry["items"]:
            assert item["fact"]["value"] != "2026-11-12"
    assert kinds  # 撤回以后队列里是有东西的（藏起来的那一条）


def test_an_ambiguous_name_is_asked_about_with_visible_candidates(tmp_path) -> None:
    world = build_world(tmp_path / "w")
    world.drop("inbox/desk-bot", WHO_IS_ZHOU)
    world.intake()
    write_all(world, extract, "prod")
    pending = card(world, entity="周海")["pending"]
    (entry,) = [one for one in pending if one["kind"] == "ambiguous_entity"]
    assert entry["items"] == [] and entry["entity"]["name"] == "周总"
    assert sorted(one["name"] for one in entry["candidates"]) == ["周建华", "周海"]
    assert entry["ask"] == ASK["ambiguous_entity"]
    assert card(world, "books-bot", entity="周海")["pending"]  # books-bot 也看得到 business
    hidden = card(world, "owner-console", entity="周海", partitions=["finance"])
    assert hidden["result"] == "none"  # 周海在 finance 里根本没有


def test_the_documented_example_has_the_same_fields(qworld: World) -> None:
    theirs = json.loads(fenced(QUERY_API, "#### 和这个实体有关的待确认", "```json"))["pending"][0]
    (ours,) = card(qworld, entity="越南那批订单")["pending"]
    assert set(ours) == set(theirs)
    assert [set(item) - {"pending_reason"} for item in ours["items"]] == [set(item) - {"pending_reason"} for item in theirs["items"]]
