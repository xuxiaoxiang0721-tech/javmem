"""接 Graphiti 的那一层（javmem.side.graph）：没装 graphiti-core（CI 就没装）整个跳过。在 .venv-side 里跑。

本机 Qwen 用测试自己在本机随机端口起的假服务，体检库不连。"""

from __future__ import annotations

import asyncio
import http.server
import importlib.util
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

if importlib.util.find_spec("graphiti_core") is None:  # 不 import：import 之前要先关上报统计
    pytest.skip("没装 graphiti-core（pip install -e .[side]）", allow_module_level=True)

from javmem.pipeline.relcheck import ALLOWED
from javmem.side import graph
from javmem.side.config import SideSettings
from javmem.side.store import SideStore
from javmem.usage import TokenLedger


def test_kit_turns_off_telemetry_and_dotenv_and_builds_types() -> None:
    k = graph.kit(2)
    import os

    assert os.environ["GRAPHITI_TELEMETRY_ENABLED"] == "false"
    assert os.environ["EMBEDDING_DIM"] == "2560"
    assert "posthog" not in sys.modules
    import dotenv

    assert dotenv.load_dotenv() is False
    assert not k.telemetry.is_telemetry_enabled()
    assert sorted(k.entity_types) == sorted(graph.ENTITY_LABELS.values())
    assert sorted(k.edge_types) == sorted(graph.RELATION_LABELS.values())
    assert list(k.edge_types["KIN_OF"].model_fields) == ["kinship"]
    assert all(not model.model_fields for name, model in k.edge_types.items() if name != "KIN_OF")
    pairs = {(graph.LABEL_RELATION[name], graph.LABEL_ENTITY[s], graph.LABEL_ENTITY[o])
             for (s, o), names in k.edge_type_map.items() for name in names}
    assert pairs == {(relation, s, o) for relation, allowed in ALLOWED.items() for s, o in allowed}
    import graphiti_core.llm_client.client as client_module

    assert client_module.get_extraction_language_instruction() == graph.LANGUAGE_NOTE


def test_check_reply() -> None:
    k = graph.kit(2)
    model = k.edge_types["KIN_OF"]
    assert graph._check_reply('{"kinship": "妻子"}', "stop", model, k.ValidationError) == ""
    assert "JSON" in graph._check_reply('{"kinship": ', "stop", model, k.ValidationError)
    assert graph._check_reply('{"kinship": 3}', "stop", model, k.ValidationError).startswith("kinship")
    assert graph._check_reply("[]", "stop", None, k.ValidationError) == "要回一个 JSON 对象"
    assert "截断" in graph._check_reply("{}", "length", None, k.ValidationError)


class _FakeQwen:
    """本机随机端口上的假 Qwen：按 replies 一个一个回（字符串是 content，数字是 HTTP 状态码）。"""

    def __init__(self, replies: list[str | int]) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, object]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                fake.bodies.append(json.loads(self.rfile.read(length).decode("utf-8")))
                reply = fake.replies.pop(0) if fake.replies else "{}"
                if isinstance(reply, int):
                    self.send_response(reply)
                    self.end_headers()
                    return
                payload = json.dumps({"choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                                "prompt_tokens_details": {"cached_tokens": 4}}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"

    def __enter__(self) -> _FakeQwen:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(graph, "RETRY_DELAY", 0.0)
    yield


def _client(url: str, tmp_path: Path) -> tuple[object, SideStore, TokenLedger]:
    store = SideStore.open(tmp_path, "test")
    ledger = TokenLedger(tmp_path / "ledger.csv")
    settings = SideSettings.from_env({"JAVMEM_SIDE_QWEN_URL": url, "JAVMEM_SIDE_LANES": "1"})
    recorder = graph.Recorder(store, ledger, "j6-test", settings.qwen_model)
    return graph.build_qwen_client(settings, recorder, timeout=10), store, ledger


def _ask(client: object, model: object) -> dict[str, object]:
    from graphiti_core.prompts.models import Message

    async def main() -> dict[str, object]:
        token = graph._CURRENT.set(("g_r1", "g#w1"))
        try:
            return await client.generate_response(  # type: ignore[attr-defined]
                [Message(role="system", content="系统"), Message(role="user", content="问")], model,
                prompt_name="extract_edges.extract_attributes")
        finally:
            graph._CURRENT.reset(token)

    return asyncio.run(main())


def test_qwen_client_reasks_with_the_error_then_succeeds(tmp_path: Path, no_wait: None) -> None:
    model = graph.kit(2).edge_types["KIN_OF"]
    with _FakeQwen(['{"kinship": ', '{"kinship": "哥哥"}']) as fake:
        client, store, ledger = _client(fake.url, tmp_path)
        try:
            assert _ask(client, model) == {"kinship": "哥哥"}
            first, second = fake.bodies
            assert first["temperature"] == 0 and first["chat_template_kwargs"] == {"enable_thinking": False}
            assert first["response_format"]["type"] == "json_schema"  # type: ignore[index]
            assert "格式不对" in second["messages"][-1]["content"]  # type: ignore[index]
            statuses = [(row["prompt"], row["attempt"], row["status"]) for row in store.calls()]
            assert statuses == [("side:extract_edges.extract_attributes", 1, "ok"),
                                ("side:extract_edges.extract_attributes", 1, "format_error"),
                                ("side:extract_edges.extract_attributes", 2, "ok")]
            text = (tmp_path / "ledger.csv").read_text(encoding="utf-8")
            assert "j6-test" in text and "qwen3.8-27b" in text and "哥哥" not in text  # 账里不记内容
            assert ledger.used("j6-test") == 24
        finally:
            store.close()


def test_qwen_client_gives_up_after_two_fixes(tmp_path: Path, no_wait: None) -> None:
    model = graph.kit(2).edge_types["KIN_OF"]
    with _FakeQwen(["不是 JSON"] * 3) as fake:
        client, store, _ = _client(fake.url, tmp_path)
        try:
            with pytest.raises(graph.SideFormatError) as raised:
                _ask(client, model)
            assert raised.value.prompt == "extract_edges.extract_attributes"
            assert len(fake.bodies) == 1 + graph.MAX_FIXES
        finally:
            store.close()


def test_qwen_client_stops_when_the_server_keeps_failing(tmp_path: Path, no_wait: None) -> None:
    with _FakeQwen([500, 500]) as fake:
        client, store, _ = _client(fake.url, tmp_path)
        try:
            with pytest.raises(graph.SideTransportError):
                _ask(client, None)
            assert [row["status"] for row in store.calls()] == ["http_500", "http_500"]
        finally:
            store.close()


def test_errors_in_the_timestamp_step_are_not_swallowed(tmp_path: Path, no_wait: None) -> None:
    """Graphiti 补日子那一步把出错都吞了（except Exception）：格式错、连不上都要照样冒出来，不然窗口会记成好了。"""
    from datetime import UTC, datetime

    from graphiti_core.edges import EntityEdge
    from graphiti_core.utils.maintenance import edge_operations

    graph.kit(2)
    edge = EntityEdge(source_node_uuid="a", target_node_uuid="b", name="HOLDS", fact="主人持有 X2", group_id="g_r1",
                      created_at=datetime.now(UTC), reference_time=datetime(2026, 11, 8, tzinfo=UTC))

    async def main(client: object) -> None:
        with graph.window_scope("g_r1", "g#w1"):
            await edge_operations._extract_edge_timestamps(client, edge, None)

    with _FakeQwen(["不是 JSON"] * 3) as fake:
        client, store, _ = _client(fake.url, tmp_path)
        try:
            with pytest.raises(graph.SideFormatError):
                asyncio.run(main(client))
        finally:
            store.close()
    with _FakeQwen([500, 500]) as fake:
        client, store, _ = _client(fake.url, tmp_path / "again")
        try:
            with pytest.raises(graph.SideTransportError):
                asyncio.run(main(client))
        finally:
            store.close()


def test_group_ids_are_checked() -> None:
    assert graph.check_group_id("2026-11-08_2330_desk-bot_r1")
    for bad in ("", "a b", "x'}) DETACH DELETE n //", None):
        with pytest.raises(graph.SideGraphError):
            graph.check_group_id(bad)  # type: ignore[arg-type]
