"""管理操作只给管理员（J5.6；01 p4：管理操作也算写，只给主人和 CC）。

撤回、彻底删除、冻结、处理待确认、合并拆开、重建主图都只在命令行；Bot 只有收件目录和查询服务两个口。查询服务（MCP）里
只有五个只读工具（J5.5 的测试钉着名单），这里再钉一样：叫管理命令的名字回“没有这个工具”，账本一个字没动。样例都是编的。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from helpers import World
from query_world import build_world, rows

from javmem.query.mcp import MODERN, Server

META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {}}
ADMIN_NAMES = ["withdraw", "purge", "unwithdraw", "freeze", "review", "decide", "entities", "merge", "graph", "graph_rebuild",
               "rebuild", "sync"]


@pytest.fixture(scope="module")
def qworld(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(tmp_path_factory.mktemp("admin-only"))


def snapshot(world: World) -> dict[str, object]:
    """账本里管理命令会动的几样。"""
    return {
        "packets": [tuple(row) for row in rows(world, "SELECT packet_id, revision, withdrawn_at, purged_at, extraction FROM packets ORDER BY 1, 2")],
        "facts": [tuple(row) for row in rows(world, "SELECT fact_id, frozen, status, withdrawn_at FROM facts ORDER BY 1")],
        "reviews": [tuple(row) for row in rows(world, "SELECT review_id, decided_at FROM review_queue ORDER BY 1")],
        "entities": [tuple(row) for row in rows(world, "SELECT entity_id, canonical_name FROM entities ORDER BY 1")],
        "withdrawn": [tuple(row) for row in rows(world, "SELECT sentence_id FROM raw_sentences WHERE withdrawn_at IS NOT NULL ORDER BY 1")],
    }


@pytest.mark.parametrize("caller", ["desk-bot", "owner-console"])
def test_the_query_service_has_no_admin_tools(qworld: World, caller: str) -> None:
    server = Server(replace(qworld.settings, caller=caller))
    before = snapshot(qworld)
    packet_id = before["packets"][0][0]  # type: ignore[index]
    fact_id = before["facts"][0][0] if before["facts"] else "F0"  # type: ignore[index]
    for message_id, name in enumerate(ADMIN_NAMES, 1):
        body = {"_meta": META, "name": name, "arguments": {"packet": packet_id, "fact": fact_id, "library": "prod", "yes": True}}
        reply = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": message_id, "method": "tools/call", "params": body}))
        assert reply is not None and "result" not in reply
        assert reply["error"]["code"] == -32602 and "没有这个工具" in reply["error"]["message"]
    listed = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {"_meta": META}}))
    assert listed is not None
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in listed["result"]["tools"])
    assert not {tool["name"] for tool in listed["result"]["tools"]} & set(ADMIN_NAMES)
    assert snapshot(qworld) == before  # 账本一个字没动
