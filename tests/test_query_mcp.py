"""查询服务的 MCP 这一层（J5.5，javmem.query.mcp）和两个命令（javmem serve、javmem query）。

两代协议都测：现行的 2026-07-28（没有握手，每个请求的 _meta 写版本，server/discover）和握手版（2025-11-25、
2025-06-18，先 initialize）。另起一个子进程从头跑一遍，看标准输出里只有协议的消息。样例都是编的。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import World
from query_world import build_world

from javmem.cli import main
from javmem.query.mcp import MODERN, SUPPORTED, TOOL_DEFINITIONS, Server, encode
from javmem.query.tools import TOOLS, memory_search

META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {}}
NAMES = ["memory_search", "entity_card", "timeline", "relation_at", "raw_search"]


@pytest.fixture(scope="module")
def qworld(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(tmp_path_factory.mktemp("query-mcp"))


def server(world: World, caller: str | None = "desk-bot") -> Server:
    return Server(replace(world.settings, caller=caller))


def ask(target: Server, method: str, params: dict | None = None, *, message_id: object = 1, modern: bool = True) -> dict:
    body: dict = dict(params or {})
    if modern:
        body["_meta"] = META
    reply = target.handle_line(json.dumps({"jsonrpc": "2.0", "id": message_id, "method": method, "params": body}))
    assert reply is not None
    return reply


# ---------- 现行版 2026-07-28 ----------


def test_discover(qworld: World) -> None:
    reply = ask(server(qworld), "server/discover")
    result = reply["result"]
    assert (reply["jsonrpc"], reply["id"]) == ("2.0", 1)
    assert result["resultType"] == "complete" and result["supportedVersions"] == list(SUPPORTED)
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "javmem"
    assert "推断" in result["instructions"]


def test_tools_list_has_the_five_read_only_tools(qworld: World) -> None:
    result = ask(server(qworld), "tools/list")["result"]
    assert result["resultType"] == "complete"
    assert [tool["name"] for tool in result["tools"]] == NAMES
    for tool in result["tools"]:
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["destructiveHint"] is False
        assert tool["inputSchema"]["type"] == "object" and tool["inputSchema"]["additionalProperties"] is False
        assert tool["description"] and tool["title"]
    assert set(TOOLS) == set(NAMES)
    schema = {tool["name"]: tool["inputSchema"] for tool in result["tools"]}
    assert schema["memory_search"]["required"] == ["query"] and schema["relation_at"]["required"] == ["relation", "at"]
    assert schema["relation_at"]["properties"]["relation"]["enum"] == ["持有", "托管于", "负责", "任职于", "客户是", "亲属", "隶属于"]  # D210


def test_tools_call_puts_the_answer_in_structured_content_and_text(qworld: World) -> None:
    result = ask(server(qworld), "tools/call", {"name": "memory_search", "arguments": {"query": "越南那批订单最近怎么样"}})["result"]
    expected = memory_search({"query": "越南那批订单最近怎么样"}, settings=replace(qworld.settings, caller="desk-bot"))
    assert result["resultType"] == "complete" and result["isError"] is False
    assert result["structuredContent"] == json.loads(json.dumps(expected))
    assert result["content"] == [{"type": "text", "text": json.dumps(expected, ensure_ascii=False)}]


def test_a_tool_error_is_an_error_result_not_a_protocol_error(qworld: World) -> None:
    result = ask(server(qworld, caller=None), "tools/call", {"name": "entity_card", "arguments": {"entity": "老周"}})["result"]
    assert result["isError"] is True and result["structuredContent"]["error"]["code"] == "unknown_identity"
    bad = ask(server(qworld), "tools/call", {"name": "entity_card", "arguments": {}})["result"]
    assert bad["isError"] is True and bad["structuredContent"]["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("meta", [None, {}, {"io.modelcontextprotocol/protocolVersion": "1900-01-01"},
                                  {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}])
def test_a_missing_or_unknown_version_is_refused(qworld: World, meta: dict | None) -> None:
    params: dict = {} if meta is None else {"_meta": meta}
    reply = server(qworld).handle_line(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": params}))
    assert reply is not None and reply["id"] == 7
    assert reply["error"]["code"] == -32022 and reply["error"]["data"]["supported"] == list(SUPPORTED)
    requested = None if not meta else meta.get("io.modelcontextprotocol/protocolVersion")
    assert reply["error"]["data"]["requested"] == requested


# ---------- 握手版 ----------


@pytest.mark.parametrize(("asked", "answered"), [("2025-06-18", "2025-06-18"), ("2025-11-25", "2025-11-25"),
                                                  ("2024-11-05", "2025-11-25"), (None, "2025-11-25")])
def test_legacy_handshake(qworld: World, asked: str | None, answered: str) -> None:
    target = server(qworld)
    reply = ask(target, "initialize", {"protocolVersion": asked, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
                modern=False)
    assert reply["result"]["protocolVersion"] == answered
    assert reply["result"]["serverInfo"]["name"] == "javmem" and reply["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert target.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None
    listed = ask(target, "tools/list", modern=False, message_id="a")
    assert listed["id"] == "a" and "resultType" not in listed["result"]
    assert [tool["name"] for tool in listed["result"]["tools"]] == NAMES
    called = ask(target, "tools/call", {"name": "raw_search", "arguments": {"query": "报价单"}}, modern=False)["result"]
    assert called["isError"] is False and called["structuredContent"]["result"] == "hit"
    assert ask(target, "ping", modern=False)["result"] == {}


def test_ping_before_the_handshake(qworld: World) -> None:
    """审阅第 8 条：握手版允许 initialize 以前就 ping；现行版的 ping 照样核版本。"""
    assert ask(server(qworld), "ping", modern=False)["result"] == {}
    assert ask(server(qworld), "ping")["result"] == {"resultType": "complete"}


# ---------- 出错的几种 ----------


def test_protocol_errors(qworld: World) -> None:
    target = server(qworld)
    assert ask(target, "resources/list")["error"]["code"] == -32601
    assert ask(target, "tools/call", {"name": "delete_everything"})["error"]["code"] == -32602
    assert ask(target, "tools/call", {"name": "raw_search", "arguments": ["报价单"]})["error"]["code"] == -32602
    assert target.handle_line("{不是 JSON") == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "不是 JSON。"}}
    assert target.handle_line(b"\xff\xfe") ["error"]["code"] == -32700  # type: ignore[index]
    assert target.handle_line(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]))["error"]["code"] == -32600  # type: ignore[index]
    assert target.handle_line(json.dumps({"jsonrpc": "1.0", "id": 1, "method": "ping"}))["error"]["code"] == -32600  # type: ignore[index]
    assert target.handle_line(json.dumps({"jsonrpc": "2.0", "id": True, "method": "ping"}))["error"]["code"] == -32600  # type: ignore[index]
    assert target.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}))["error"]["code"] == -32602  # type: ignore[index]
    assert target.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})) is None
    assert target.handle_line(json.dumps({"jsonrpc": "2.0", "id": 9, "result": {}})) is None  # 客户端回给我们的


def test_an_unexpected_failure_answers_without_content(qworld: World, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(arguments: dict, *, settings: object) -> dict:
        raise RuntimeError("越南那批订单")  # 消息里有内容也不能带出去

    target = Server(replace(qworld.settings, caller="desk-bot"), tools={"memory_search": broken})
    reply = ask(target, "tools/call", {"name": "memory_search", "arguments": {"query": "x"}})
    assert reply["error"] == {"code": -32603, "message": "服务出错了，这次答不了。"}
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "越南那批订单" not in err


def test_encode_is_one_line_and_survives_lone_surrogates() -> None:
    assert encode({"text": "报价单\n第二行"}) == '{"text": "报价单\\n第二行"}\n'.encode()
    assert encode({"text": "\ud800"}) == b'{"text": "\\ud800"}\n'


def test_the_tool_definitions_match_the_documented_arguments() -> None:
    """入参和文档第 5 节的表一样：服务按工具名认的参数，就是 inputSchema 里的那几个。"""
    expected = {
        "memory_search": {"query", "limit", "partitions", "time_from", "time_to", "include_history", "cursor"},
        "entity_card": {"entity", "entity_id", "recent_limit", "partitions"},
        "timeline": {"entity", "entity_id", "time_from", "time_to", "include", "order", "limit", "partitions", "cursor"},
        "relation_at": {"entity", "entity_id", "relation", "at", "role", "partitions"},
        "raw_search": {"query", "limit", "partitions", "time_from", "time_to", "source", "packet_id", "thread_id", "speaker",
                       "context", "cursor"},
    }
    assert {tool["name"]: set(tool["inputSchema"]["properties"]) for tool in TOOL_DEFINITIONS} == expected


# ---------- 真起一个进程 ----------


def child_env(world: World, caller: str) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if not name.startswith("JAVMEM_")}
    root = world.data.parent
    src = str(Path(__file__).resolve().parent.parent / "src")
    env.update(
        JAVMEM_DATA_DIR=str(world.data), JAVMEM_SOURCES_FILE=str(root / "sources.yaml"),
        JAVMEM_ACCESS_FILE=str(root / "access.yaml"), JAVMEM_LEXICON_FILE=str(root / "lexicon.yaml"),
        JAVMEM_TIMEZONE="+08:00", JAVMEM_CALLER=caller,
        PYTHONPATH=os.pathsep.join([src, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]),
        PYTHONIOENCODING="utf-8",
    )
    return env


def test_serve_over_stdio_from_start_to_end(qworld: World) -> None:
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": META}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": META}},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"_meta": META, "name": "raw_search",
                                                                          "arguments": {"query": "报价单", "limit": 2}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"_meta": META, "name": "memory_search",
                                                                          "arguments": {"query": "越南那批订单最近怎么样"}}},
    ]
    stdin = "".join(json.dumps(line, ensure_ascii=False) + "\r\n" for line in lines).encode("utf-8") + b"\n"
    done = subprocess.run([sys.executable, "-X", "utf8", "-m", "javmem", "serve"], input=stdin, capture_output=True,
                          env=child_env(qworld, "desk-bot"), timeout=120, check=False)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    replies = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]  # 每一行都是 JSON：没有别的输出
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4]
    assert replies[0]["result"]["supportedVersions"] == list(SUPPORTED)
    assert [tool["name"] for tool in replies[1]["result"]["tools"]] == NAMES
    raw = replies[2]["result"]["structuredContent"]
    assert (raw["result"], len(raw["items"]), raw["library"]) == ("hit", 2, "prod")
    found = replies[3]["result"]["structuredContent"]
    assert found["result"] == "hit" and found["degraded"] == ["graph"]


def test_serve_without_an_identity_still_lists_tools(qworld: World) -> None:
    env = child_env(qworld, "")
    stdin = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}}) + "\n"
             + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"_meta": META, "name": "entity_card", "arguments": {"entity": "老周"}}}) + "\n")
    done = subprocess.run([sys.executable, "-X", "utf8", "-m", "javmem", "serve"], input=stdin.encode("utf-8"),
                          capture_output=True, env=env, timeout=120, check=False)
    replies = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]
    assert len(replies[0]["result"]["tools"]) == 5
    assert replies[1]["result"]["isError"] is True
    assert replies[1]["result"]["structuredContent"]["error"]["code"] == "unknown_identity"


# ---------- javmem query ----------


def use_world(world: World, monkeypatch: pytest.MonkeyPatch, caller: str) -> None:
    for name, value in child_env(world, caller).items():
        if name.startswith("JAVMEM_"):
            monkeypatch.setenv(name, value)


def test_query_command(qworld: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    use_world(qworld, monkeypatch, "desk-bot")
    assert main(["query", "entity_card", '{"entity": "老周"}']) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["entity"]["name"] == "周建华"
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"query": "报价单"}'))
    assert main(["query", "raw_search"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "hit"


def test_query_command_errors(qworld: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    use_world(qworld, monkeypatch, "no-such-bot")
    assert main(["query", "entity_card", '{"entity": "老周"}']) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unknown_identity"
    use_world(qworld, monkeypatch, "desk-bot")
    assert main(["query", "entity_card", "{不是 JSON"]) == 2
    assert main(["query", "entity_card", "[1]"]) == 2
    with pytest.raises(SystemExit):
        main(["query", "delete_everything", "{}"])
