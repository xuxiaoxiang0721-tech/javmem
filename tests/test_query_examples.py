"""J5.5 的验收：01 p4 的五个例子各问一遍（走 MCP 服务，tools/call），和 query-api 第 5 节的例子比。

例子世界照文档里编的名字和句子搭（tests/query_world.py），编号是账本算出来的，和文档里写的 R097、F305 不一样，
所以比的是样子（每一层有哪些字段）和关键的值（名字、状态、时间、原句）。每条结果都要有四样：内容、状态、生效时间、出处。
另外：没抽完的包（queued、running、failed、skipped）raw_search 都查得到，memory_search 从原文层答。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from helpers import QUERY_API, World, fenced
from query_world import AS_OF_TEXT, NEW_ID, build_world

from javmem.query.mcp import MODERN, Server

FOUR = ("content", "status", "valid_time", "evidence")
META = {"io.modelcontextprotocol/protocolVersion": MODERN}


@pytest.fixture(scope="module")
def qworld(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(tmp_path_factory.mktemp("query-examples"))


def ask(world: World, name: str, arguments: dict, caller: str = "desk-bot") -> dict:
    """走一遍 MCP 服务的 tools/call，拿 structuredContent；文字那一份和它一样。"""
    target = Server(replace(world.settings, caller=caller))
    reply = target.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"_meta": META, "name": name, "arguments": arguments}}))
    assert reply is not None
    result = reply["result"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["isError"] is (not result["structuredContent"]["ok"])
    return result["structuredContent"]


def doc(heading: str, nth: int) -> dict:
    """接口文档里 heading 那一节的第 nth 个 JSON 例子（0 是调用，1 是返回）。"""
    return json.loads(fenced(QUERY_API, heading, "```json", nth))


def every_result(answer: dict) -> list[dict]:
    found = []
    for name in ("items", "aliases", "relations", "recent"):
        found.extend(answer.get(name) or [])
    return found


def shape(item: dict) -> set[str]:
    """一条结果的字段（pending_reason 只有待确认的才有，不算样子的差别）。"""
    return set(item) - {"pending_reason"}


def assert_same_shape(ours: list[dict], theirs: list[dict]) -> None:
    """同一种 kind 的结果，字段一样；出处、fact、relation 里面的字段也一样。"""
    expected = {item["kind"]: item for item in theirs}
    for item in ours:
        model = expected.get(item["kind"])
        if model is None:
            continue
        assert shape(item) == shape(model), item["id"]
        for evidence in item["evidence"]:
            assert set(evidence) - {"email"} == set(model["evidence"][0]) - {"email"}
        for nested in ("fact", "relation"):
            if nested in model:
                extras = {"superseded_by", "superseded_at"} if nested == "fact" else set()  # D66：被替代的事实才带
                assert set(item[nested]) - extras == set(model[nested]), item["id"]
        assert set(item["valid_time"]) == {"from", "to", "precision"}


def test_every_example_call_in_the_document_is_accepted(qworld: World) -> None:
    """文档第 5 节五个例子的调用原样发过去，都答得了。"""
    for heading in ("### 5.1", "### 5.2", "### 5.3", "### 5.4", "### 5.5"):
        call = doc(heading, 0)
        answer = ask(qworld, call["name"], call["arguments"])
        assert answer["ok"] is True and answer["result"] == "hit", heading
        for item in every_result(answer):
            assert all(name in item for name in FOUR), (heading, item["id"])


def test_memory_search_example(qworld: World) -> None:
    """“越南那批订单最近怎么样”。"""
    theirs = doc("### 5.1", 1)
    ours = ask(qworld, "memory_search", doc("### 5.1", 0)["arguments"])
    assert set(ours) == set(theirs)
    assert (ours["tool"], ours["library"], ours["as_of"], ours["result"], ours["answered_from"]) == (
        "memory_search", "prod", AS_OF_TEXT, "hit", "graph")
    assert ours["degraded"] == ["graph"]  # 主图还没接（J5.4），从账本视图答
    assert_same_shape(ours["items"], theirs["items"])
    (zhou,) = [item for item in ours["items"] if item["content"] == "周建华 负责 越南那批订单"]
    assert (zhou["status"], zhou["valid_time"]) == ("verified", {"from": "2026-11-09", "to": None, "precision": "day"})
    assert zhou["evidence"][0]["quote"] == "刚才说的改一下，越南那批从下周一开始交给老周。"
    assert zhou["evidence"][0]["position"] == {"section": 3, "line": 1, "sentence": 1}
    assert zhou["relation"]["replaces"] and zhou["relation"]["state"] == "active"
    assert any(item["status"] == "pending" for item in ours["items"])  # 待确认的也回，说明还没定


def test_entity_card_example(qworld: World) -> None:
    """“老周”。"""
    theirs = doc("### 5.2", 1)
    ours = ask(qworld, "entity_card", doc("### 5.2", 0)["arguments"])
    assert set(ours) == set(theirs)
    assert ours["entity"] == {**theirs["entity"], "id": ours["entity"]["id"]}  # 周建华，人，按“老周”对上的
    assert_same_shape(ours["aliases"] + ours["relations"] + ours["recent"],
                      theirs["aliases"] + theirs["relations"] + theirs["recent"])
    assert ours["aliases"][0]["content"] == "老周"
    assert ours["aliases"][0]["evidence"][-1]["quote"] == "老周就是周建华，在示例贸易管物流。"
    assert {item["content"] for item in ours["relations"]} == {item["content"] for item in theirs["relations"]}
    assert len(ours["recent"]) == 1


def test_ambiguous_example(qworld: World) -> None:
    """5.2 名字对到不止一个实体：候选从看得到的里出，提示也只用看得到的。"""
    theirs = json.loads(fenced(QUERY_API, "### 5.2", "```json", 2))
    ours = ask(qworld, "entity_card", {"entity": "周总"})
    assert set(ours) == set(theirs)
    assert sorted((c["name"], c["type"], c["matched_alias"], c["hint"]) for c in ours["candidates"]) == sorted(
        (c["name"], c["type"], c["matched_alias"], c["hint"]) for c in theirs["candidates"]
    )


def test_timeline_example(qworld: World) -> None:
    """“越南订单 10 月以来”。"""
    theirs = doc("### 5.3", 1)
    ours = ask(qworld, "timeline", doc("### 5.3", 0)["arguments"])
    assert set(ours) == set(theirs)
    assert ours["entity"] == {**theirs["entity"], "id": ours["entity"]["id"]}
    assert ours["range"] == theirs["range"]
    assert_same_shape(ours["items"], theirs["items"])
    shown = [(item["at"], item.get("change"), item["content"]) for item in ours["items"]]
    assert shown[0] == ("2026-10-08", None, "越南那批订单的定金到账")
    handover = [one for one in shown if one[0] == "2026-11-09" and one[1]]
    assert handover == [("2026-11-09", "ended", "李明 负责 越南那批订单"), ("2026-11-09", "started", "周建华 负责 越南那批订单")]


def test_relation_at_example(qworld: World) -> None:
    """“10 月 1 日越南订单谁负责”。"""
    theirs = doc("### 5.4", 1)
    ours = ask(qworld, "relation_at", doc("### 5.4", 0)["arguments"])
    assert set(ours) == set(theirs)
    assert (ours["at"], ours["entity"]["name"], ours["entity"]["matched_alias"]) == ("2026-10-01", "越南那批订单", "越南订单")
    assert_same_shape(ours["items"], theirs["items"])
    (item,) = ours["items"]
    model = theirs["items"][0]
    for name in ("content", "status", "valid_time", "time_match", "kind"):
        assert item[name] == model[name], name
    assert (item["relation"]["state"], item["relation"]["type"]) == ("ended", "负责")
    assert item["relation"]["superseded_by"]


def test_raw_search_example(qworld: World) -> None:
    """“报价单”：第一条来自刚到的包，还在排队抽取。"""
    theirs = doc("### 5.5", 1)
    ours = ask(qworld, "raw_search", doc("### 5.5", 0)["arguments"])
    assert set(ours) == set(theirs)
    assert [item["id"] for item in ours["items"]] == [item["id"] for item in theirs["items"]]
    assert ours["items"] == theirs["items"]
    assert ours["next_cursor"] == theirs["next_cursor"]


@pytest.mark.parametrize("state", ["queued", "running", "failed", "skipped"])
def test_packets_not_extracted_yet_are_found(tmp_path, state: str) -> None:
    """第 9 节：raw_search 不看抽取到哪了；memory_search 主图和账本视图查不到时回退原文层，也查得到。"""
    world = build_world(tmp_path / "w")
    world.execute("UPDATE packets SET extraction = ? WHERE packet_id = ?", state, NEW_ID)
    raw = ask(world, "raw_search", {"query": "新的报价单"})
    assert [(item["id"], item["extraction"]) for item in raw["items"]] == [(f"{NEW_ID}#1.1.1", state)]
    found = ask(world, "memory_search", {"query": "新的报价单", "limit": 1})
    assert found["answered_from"] == "raw" and found["items"][0]["extraction"] == state


def test_not_found_examples_have_the_documented_fields(qworld: World) -> None:
    """第 6 节查不到的几个例子：字段和文档一样（searched 写的是这次真查了的层，规矩题 2）。"""
    blocks = [json.loads(fenced(QUERY_API, "## 6.", "```json", nth)) for nth in range(3)]
    asked = [
        ask(qworld, "memory_search", {"query": "完全没有的东西"}),
        ask(qworld, "entity_card", {"entity": "没有这个人"}),
        ask(qworld, "relation_at", {"entity": "越南订单", "relation": "负责", "at": "2026-09-01", "role": "object"}),
    ]
    for ours, theirs in zip(asked, blocks, strict=True):
        assert set(ours) == set(theirs)
        assert set(ours["not_found"]) == set(theirs["not_found"])
        assert ours["not_found"]["message"] == theirs["not_found"]["message"]
        assert ours["not_found"]["reason"] == theirs["not_found"]["reason"]
    # entity_card、relation_at 只查账本视图（规矩题 2）：和文档的例子一字不差；memory_search 的例子是主图接上以后的样子
    for ours, theirs in zip(asked[1:], blocks[1:], strict=True):
        assert ours["not_found"]["searched"] == theirs["not_found"]["searched"] == ["ledger"]
    assert asked[0]["not_found"]["searched"] == ["ledger", "raw"]
    assert asked[2]["not_found"]["nearest"]["after"]["content"] == blocks[2]["not_found"]["nearest"]["after"]["content"]


def test_the_three_answers_for_what_you_may_not_see(qworld: World) -> None:
    """D33 的三种（文档第 7 节的例子）：不认识的身份报错；明着要看不到的分区报错；没明着要，当它不存在。"""
    assert ask(qworld, "raw_search", {"query": "工资单"}, caller="no-such-bot")["error"]["code"] == "unknown_identity"
    forbidden = ask(qworld, "raw_search", {"query": "工资单", "partitions": ["finance"]})
    assert forbidden == json.loads(fenced(QUERY_API, "## 7.", "```json", 1))
    hidden = ask(qworld, "raw_search", {"query": "工资单"})
    theirs = json.loads(fenced(QUERY_API, "## 7.", "```json", 3))
    assert hidden == {**theirs, "as_of": AS_OF_TEXT}
    seen = ask(qworld, "raw_search", {"query": "工资单"}, caller="books-bot")
    assert seen["result"] == "hit" and seen["items"][0]["scope"] == {"partitions": ["finance"], "sensitive": ["payroll"]}
