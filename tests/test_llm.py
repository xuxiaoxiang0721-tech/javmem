"""模型客户端：设置、请求的样子、各种回复。全在本机：一个假的接口，一个假的代理，不连外网。"""

from __future__ import annotations

import json
import select
import socket
import socketserver
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from javmem.config import ConfigError
from javmem.llm import (
    AUTH,
    BAD_REQUEST,
    DEFAULT_MODEL,
    FLEX_READ_TIMEOUT,
    PROXY,
    QUOTA,
    RATE_LIMIT,
    REFUSAL,
    SERVER,
    TRANSPORT,
    UNAVAILABLE,
    LLMSettings,
    ModelError,
    OpenAIChat,
    _retry_after,
)

KEY = "test-key-not-real-0000"
SCHEMA = {"type": "object", "additionalProperties": False, "required": [], "properties": {}}


def settings(**env: str) -> LLMSettings:
    return LLMSettings.from_env({"JAVMEM_LLM_API_KEY": KEY, **env})


# ---------- 设置 ----------


def test_defaults() -> None:
    value = settings()
    assert value.model == DEFAULT_MODEL
    assert value.base_url == "https://api.openai.com/v1"
    assert value.reasoning_effort == "auto"
    assert value.structured_output == "json_schema"
    assert value.proxy is None
    assert value.read_timeout == value.timeout


def test_key_never_shown() -> None:
    value = settings()
    assert KEY not in repr(value)
    assert KEY not in json.dumps(value.describe())
    assert value.describe()["api_key_set"] is True
    with pytest.raises(ConfigError) as error:
        OpenAIChat(LLMSettings.from_env({}))
    assert "JAVMEM_LLM_API_KEY" in str(error.value)


@pytest.mark.parametrize(
    "proxy",
    ["https://127.0.0.1:7890", "http://10.0.0.8:7890", "http://127.0.0.1", "http://user:pw@127.0.0.1:7890", "socks5://127.0.0.1:1080"],
)
def test_proxy_must_be_local_http(proxy: str) -> None:
    with pytest.raises(ConfigError):
        settings(JAVMEM_LLM_PROXY_URL=proxy)


def test_proxy_accepted() -> None:
    assert settings(JAVMEM_LLM_PROXY_URL="http://127.0.0.1:7890").proxy == ("127.0.0.1", 7890)


@pytest.mark.parametrize(
    "env",
    [
        {"JAVMEM_LLM_BASE_URL": "http://api.example.com/v1"},
        {"JAVMEM_LLM_BASE_URL": "ftp://127.0.0.1/v1"},
        {"JAVMEM_LLM_REASONING_EFFORT": "very high!"},
        {"JAVMEM_LLM_STRUCTURED_OUTPUT": "text"},
        {"JAVMEM_LLM_MAX_OUTPUT_TOKENS": "ten"},
        {"JAVMEM_LLM_TIMEOUT_SECONDS": "0"},
        {"JAVMEM_LLM_MODEL": "bad model name"},
        {"JAVMEM_LLM_SERVICE_TIER": "cheap"},
    ],
)
def test_bad_settings(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        settings(**env)


def test_payload_follows_reasoning_effort() -> None:
    auto = OpenAIChat(settings()).payload("系统", "窗口", SCHEMA, "demo")
    assert "reasoning_effort" not in auto and "temperature" not in auto
    assert auto["max_completion_tokens"] == 32000
    assert auto["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "demo", "strict": True, "schema": SCHEMA},
    }
    assert auto["messages"] == [{"role": "system", "content": "系统"}, {"role": "user", "content": "窗口"}]
    high = OpenAIChat(settings(JAVMEM_LLM_REASONING_EFFORT="high")).payload("s", "u", SCHEMA, "demo")
    assert high["reasoning_effort"] == "high" and "temperature" not in high
    off = OpenAIChat(settings(JAVMEM_LLM_REASONING_EFFORT="off")).payload("s", "u", SCHEMA, "demo")
    assert off["temperature"] == 0 and "reasoning_effort" not in off
    loose = OpenAIChat(settings(JAVMEM_LLM_STRUCTURED_OUTPUT="json_object")).payload("s", "u", SCHEMA, "demo")
    assert loose["response_format"] == {"type": "json_object"}


def test_cache_name_includes_effort_and_mode() -> None:
    assert OpenAIChat(settings()).cache_name != OpenAIChat(settings(JAVMEM_LLM_REASONING_EFFORT="high")).cache_name


def test_service_tier_is_only_sent_when_flex_is_asked_for() -> None:
    """D169（11-17）：配了 JAVMEM_LLM_SERVICE_TIER=flex 才在请求里写 service_tier（半价）；不配的请求一个字不变。"""
    plain = settings()
    assert plain.service_tier == "" and plain.describe()["service_tier"] is None
    assert "service_tier" not in OpenAIChat(plain).payload("s", "u", SCHEMA, "demo")
    assert OpenAIChat(plain).service_tier == ""
    flex = settings(JAVMEM_LLM_SERVICE_TIER="Flex")
    assert flex.service_tier == "flex" and flex.describe()["service_tier"] == "flex"
    assert OpenAIChat(flex).payload("s", "u", SCHEMA, "demo")["service_tier"] == "flex"
    assert OpenAIChat(flex).service_tier == "flex"
    # 结果的键不变：同一个窗口、提示词、模型，走不走 Flex 都是同一份结果，写账、核对、合并照旧按键找
    assert OpenAIChat(flex).cache_name == OpenAIChat(plain).cache_name
    # L4 模型复核跟着抽取模型
    assert LLMSettings.recheck_from_env({"JAVMEM_LLM_API_KEY": KEY, "JAVMEM_LLM_SERVICE_TIER": "flex"}).service_tier == "flex"


def test_flex_waits_longer_for_the_reply() -> None:
    """Flex 回得慢：等回复至少 FLEX_READ_TIMEOUT 秒（超时了多半照样算钱，多等不花钱）；配得更长的照配的，连上的超时不变。"""
    assert settings(JAVMEM_LLM_READ_TIMEOUT_SECONDS="300").read_timeout == 300
    slow = settings(JAVMEM_LLM_SERVICE_TIER="flex", JAVMEM_LLM_READ_TIMEOUT_SECONDS="300")
    assert slow.read_timeout == FLEX_READ_TIMEOUT == 900
    assert slow.timeout == 120
    assert settings(JAVMEM_LLM_SERVICE_TIER="flex", JAVMEM_LLM_READ_TIMEOUT_SECONDS="1800").read_timeout == 1800


def test_retry_after_headers() -> None:
    assert _retry_after({"retry-after": "3"}) == 3
    assert _retry_after({"retry-after-ms": "250"}) == 0.25
    assert _retry_after({"x-ratelimit-reset-requests": "1s", "x-ratelimit-reset-tokens": "6m0s"}) == 360
    assert _retry_after({"x-ratelimit-reset-tokens": "20ms"}) == pytest.approx(0.02)
    assert _retry_after({}) is None


# ---------- 本机的假接口 ----------


class FakeAPI:
    def __init__(self) -> None:
        self.replies: list[tuple[int, dict[str, str], dict[str, object]]] = []
        self.requests: list[tuple[str, dict[str, str], dict[str, object]]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                api.requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(length))))
                status, headers, body = api.replies.pop(0)
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api() -> Iterator[FakeAPI]:
    server = FakeAPI()
    yield server
    server.close()


def ok_body(content: str = '{"entities": [], "facts": []}', **extra: object) -> dict[str, object]:
    return {
        "model": "demo-model-2026",
        "choices": [{"message": {"role": "assistant", "content": content, **extra}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 10},
            "prompt_tokens_details": {"cached_tokens": 64},
        },
    }


def client(api: FakeAPI, **env: str) -> OpenAIChat:
    return OpenAIChat(settings(JAVMEM_LLM_BASE_URL=api.url, **env))


def test_ok_reply(api: FakeAPI) -> None:
    api.replies.append((200, {}, ok_body()))
    reply = client(api).complete("系统", "窗口", SCHEMA, "demo")
    assert reply.content == '{"entities": [], "facts": []}'
    assert reply.finish_reason == "stop"
    assert reply.model == "demo-model-2026"
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (120, 30)
    assert (reply.usage.reasoning_tokens, reply.usage.cached_tokens) == (10, 64)
    path, headers, body = api.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == DEFAULT_MODEL
    assert body["messages"][1]["content"] == "窗口"


@pytest.mark.parametrize(
    ("status", "headers", "body", "kind"),
    [
        (429, {"Retry-After": "7"}, {"error": {"type": "requests", "code": "rate_limit_exceeded"}}, RATE_LIMIT),
        (429, {}, {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}, QUOTA),
        (401, {}, {"error": {"message": f"Incorrect API key provided: {KEY[:6]}****"}}, AUTH),
        (400, {}, {"error": {"type": "invalid_request_error", "message": "Invalid schema: api_key=sk-abcdefghijklmnop1234"}}, BAD_REQUEST),
        (500, {}, {"error": {"message": "boom"}}, SERVER),
        (503, {}, {}, SERVER),
    ],
)
def test_error_replies(api: FakeAPI, status: int, headers: dict[str, str], body: dict[str, object], kind: str) -> None:
    api.replies.append((status, headers, body))
    with pytest.raises(ModelError) as error:
        client(api).complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == kind
    assert KEY not in str(error.value)
    assert KEY[:6] not in str(error.value)
    assert "sk-abcdefghijklmnop1234" not in str(error.value)
    if kind == RATE_LIMIT:
        assert error.value.retry_after == 7
    assert error.value.fatal == (kind in (QUOTA, AUTH, BAD_REQUEST))


def test_reply_says_which_tier_served_it(api: FakeAPI) -> None:
    """回复里的 service_tier 是实际服务的那一档：要了 flex，接口也可能按标准的做，记账照它。"""
    api.replies.append((200, {}, {**ok_body(), "service_tier": "flex"}))
    api.replies.append((200, {}, {**ok_body(), "service_tier": "default"}))
    api.replies.append((200, {}, {**ok_body(), "service_tier": "flex 档; 不是标识"}))
    api.replies.append((200, {}, ok_body()))
    flex = client(api, JAVMEM_LLM_SERVICE_TIER="flex")
    assert flex.complete("s", "u", SCHEMA, "demo").service_tier == "flex"
    assert flex.complete("s", "u", SCHEMA, "demo").service_tier == "default"
    assert flex.complete("s", "u", SCHEMA, "demo").service_tier is None  # 只认短的英文标识
    assert client(api).complete("s", "u", SCHEMA, "demo").service_tier is None
    assert [body.get("service_tier") for _, _, body in api.requests] == ["flex", "flex", "flex", None]


@pytest.mark.parametrize(
    ("tier", "body", "kind"),
    [
        ("flex", {"error": {"type": "service_unavailable", "code": "resource_unavailable"}}, UNAVAILABLE),
        ("flex", {"error": {"message": "Resource Unavailable"}}, UNAVAILABLE),  # 认不出代码的 429：都是等一会儿再试
        ("flex", {"error": {"type": "requests", "code": "rate_limit_exceeded"}}, RATE_LIMIT),
        ("flex", {"error": {"type": "tokens"}}, RATE_LIMIT),  # 只有 type 的限流
        ("flex", {"error": {"code": "rate_limit_exceeded"}}, RATE_LIMIT),  # 只有 code 的限流
        ("flex", {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}, QUOTA),
        ("", {"error": {"code": "resource_unavailable"}}, RATE_LIMIT),  # 不走 Flex 的照旧
    ],
)
def test_flex_without_resources_is_its_own_kind(api: FakeAPI, tier: str, body: dict[str, object], kind: str) -> None:
    """Flex 资源不够回 429（官网说不收钱）：单独一种，不算限流、不停整批、不记钱。"""
    api.replies.append((429, {"Retry-After": "5"}, body))
    env = {"JAVMEM_LLM_SERVICE_TIER": tier} if tier else {}
    with pytest.raises(ModelError) as error:
        client(api, **env).complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == kind
    if kind == UNAVAILABLE:
        assert not error.value.fatal and error.value.usage is None and not error.value.timeout
        assert error.value.retry_after == 5


def test_billed_error_replies_carry_the_tier_that_served_them(api: FakeAPI) -> None:
    """回了 200 却拒答、结构认不出的，钱照样算了：错误上也带着回复说的那一档，记账照它（审阅第 3 条）。"""
    api.replies.append((200, {}, {**ok_body(content="", refusal="不能回答"), "service_tier": "default"}))
    api.replies.append((200, {}, {"model": "m", "usage": {"prompt_tokens": 5, "completion_tokens": 1}, "service_tier": "flex"}))
    api.replies.append((200, {}, {**ok_body(), "choices": [{"message": {"role": "assistant", "content": 42}}], "service_tier": "flex"}))
    flex = client(api, JAVMEM_LLM_SERVICE_TIER="flex")
    with pytest.raises(ModelError) as refused:
        flex.complete("s", "u", SCHEMA, "demo")
    assert refused.value.kind == REFUSAL and refused.value.service_tier == "default" and refused.value.usage is not None
    with pytest.raises(ModelError) as odd:
        flex.complete("s", "u", SCHEMA, "demo")
    assert odd.value.kind == SERVER and odd.value.service_tier == "flex"
    with pytest.raises(ModelError) as unreadable:  # 正文认不出来的
        flex.complete("s", "u", SCHEMA, "demo")
    assert unreadable.value.kind == SERVER and unreadable.value.service_tier == "flex" and unreadable.value.usage is not None


def test_refusal(api: FakeAPI) -> None:
    api.replies.append((200, {}, ok_body(content="", refusal="不能回答")))
    with pytest.raises(ModelError) as error:
        client(api).complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == REFUSAL


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_proxy_down_is_its_own_error() -> None:
    chat = OpenAIChat(settings(JAVMEM_LLM_PROXY_URL=f"http://127.0.0.1:{free_port()}"))
    with pytest.raises(ModelError) as error:
        chat.complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == PROXY and error.value.fatal


class _Tunnel(socketserver.BaseRequestHandler):
    """最小的 HTTP CONNECT 代理：读 CONNECT，连目标，两边对着转。"""

    seen: list[str] = []

    def handle(self) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        line = data.split(b"\r\n", 1)[0].decode("ascii")
        _Tunnel.seen.append(line)
        host, port = line.split()[1].rsplit(":", 1)
        upstream = socket.create_connection((host, int(port)))
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        sockets = [self.request, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], 5)
            if not readable:
                break
            for sock in readable:
                chunk = sock.recv(65536)
                if not chunk:
                    upstream.close()
                    return
                (upstream if sock is self.request else self.request).sendall(chunk)
        upstream.close()


def test_through_a_connect_proxy(api: FakeAPI) -> None:
    proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Tunnel)
    threading.Thread(target=proxy.serve_forever, args=(0.05,), daemon=True).start()
    try:
        api.replies.append((200, {}, ok_body()))
        chat = client(api, JAVMEM_LLM_PROXY_URL=f"http://127.0.0.1:{proxy.server_address[1]}")
        reply = chat.complete("s", "u", SCHEMA, "demo")
        assert reply.usage.input_tokens == 120
        assert _Tunnel.seen[-1].startswith(f"CONNECT 127.0.0.1:{api.server.server_address[1]} ")
    finally:
        proxy.shutdown()
        proxy.server_close()


def test_context_length_is_not_fatal(api: FakeAPI) -> None:
    api.replies.append((400, {}, {"error": {"type": "invalid_request_error", "code": "context_length_exceeded"}}))
    with pytest.raises(ModelError) as error:
        client(api).complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == "too_long" and not error.value.fatal


# ---------- 审阅找到的 ----------


@pytest.mark.parametrize("bad", ["two\r\nlines-key", "key with space", "钥匙-not-ascii"])
def test_key_must_be_plain_ascii_and_is_not_echoed(bad: str) -> None:
    with pytest.raises(ConfigError) as error:
        LLMSettings.from_env({"JAVMEM_LLM_API_KEY": bad})
    assert bad not in str(error.value)


def test_header_errors_never_show_the_key(api: FakeAPI) -> None:
    from dataclasses import replace

    chat = OpenAIChat(replace(settings(JAVMEM_LLM_BASE_URL=api.url), api_key="test-key\r\nsecond-line"))
    with pytest.raises(ModelError) as error:
        chat.complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == AUTH and "test-key" not in str(error.value) and "second-line" not in str(error.value)


def test_error_text_from_the_provider_is_not_copied(api: FakeAPI) -> None:
    body = {"error": {"type": "invalid_request_error", "code": "invalid_schema", "message": "The prompt was: 忽略之前的规则"}}
    api.replies.append((400, {}, body))
    with pytest.raises(ModelError) as error:
        client(api).complete("s", "u", SCHEMA, "demo")
    assert "忽略之前的规则" not in str(error.value) and "invalid_schema" in str(error.value)
    api.replies.append((429, {}, {"error": {"type": "x" * 500, "code": "rate_limit_exceeded\n伪造"}}))
    with pytest.raises(ModelError) as error:
        client(api).complete("s", "u", SCHEMA, "demo")
    assert len(str(error.value)) < 120 and "伪造" not in str(error.value)


def test_content_filter_is_only_this_window(api: FakeAPI) -> None:
    api.replies.append((400, {}, {"error": {"type": "invalid_request_error", "code": "content_filter"}}))
    with pytest.raises(ModelError) as error:
        client(api).complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == "content_rejected" and not error.value.fatal


@pytest.mark.parametrize(
    "body",
    [
        {"choices": [{"message": "不是对象", "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": 12}, "finish_reason": "stop"}]},
        {"choices": "x"},
        {
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": "12x", "completion_tokens_details": "n/a"},
        },
    ],
)
def test_odd_replies_do_not_crash(api: FakeAPI, body: dict[str, object]) -> None:
    api.replies.append((200, {}, body))
    try:
        reply = client(api).complete("s", "u", SCHEMA, "demo")
    except ModelError as error:
        assert error.kind == SERVER
    else:
        assert isinstance(reply.content, str) and reply.usage.input_tokens == 0


def test_content_given_as_parts_is_joined(api: FakeAPI) -> None:
    parts = [{"type": "text", "text": '{"entities": [], '}, {"type": "text", "text": '"facts": []}'}]
    api.replies.append((200, {}, ok_body(content=parts)))
    assert client(api).complete("s", "u", SCHEMA, "demo").content == '{"entities": [], "facts": []}'


@pytest.mark.parametrize(
    "url", ["https://user:pw@api.example.com/v1", "https://api.example.com/v1?api-key=x", "https://api.example.com/v1#x"]
)
def test_base_url_without_credentials_or_query(url: str) -> None:
    with pytest.raises(ConfigError) as error:
        settings(JAVMEM_LLM_BASE_URL=url)
    assert "pw" not in str(error.value) and "api-key=x" not in str(error.value)


def test_hung_proxy_is_a_proxy_error() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        chat = OpenAIChat(settings(JAVMEM_LLM_PROXY_URL=f"http://127.0.0.1:{port}", JAVMEM_LLM_TIMEOUT_SECONDS="0.3"))
        with pytest.raises(ModelError) as error:
            chat.complete("s", "u", SCHEMA, "demo")
        assert error.value.kind == PROXY
    finally:
        listener.close()


# ---------- 复看找到的：花了钱的都要记账 ----------


def test_refusal_carries_its_usage(api: FakeAPI) -> None:
    api.replies.append((200, {}, ok_body(content="", refusal="不能回答")))
    with pytest.raises(ModelError) as error:
        client(api).complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == REFUSAL and error.value.usage is not None and error.value.usage.input_tokens == 120


def test_non_text_parts_are_skipped(api: FakeAPI) -> None:
    parts = [{"type": "reasoning", "summary": []}, {"type": "text", "text": '{"entities": [], "facts": []}'}]
    api.replies.append((200, {}, ok_body(content=parts)))
    assert client(api).complete("s", "u", SCHEMA, "demo").content == '{"entities": [], "facts": []}'


def test_bad_shape_still_carries_usage(api: FakeAPI) -> None:
    body = ok_body()
    body["choices"] = [{"message": "不是对象", "finish_reason": "stop"}]
    api.replies.append((200, {}, body))
    with pytest.raises(ModelError) as error:
        client(api).complete("s", "u", SCHEMA, "demo")
    assert error.value.kind == SERVER and error.value.usage is not None and error.value.usage.output_tokens == 30


def test_bad_reset_header_does_not_crash() -> None:
    assert _retry_after({"x-ratelimit-reset-requests": ".s"}) is None


def test_dropped_reply_may_be_billed() -> None:
    class Drop(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(65536)
                if not chunk:
                    return
                data += chunk
            self.request.close()  # 读完请求就断，什么都不回

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Drop)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        chat = OpenAIChat(settings(JAVMEM_LLM_BASE_URL=f"http://127.0.0.1:{server.server_address[1]}/v1"))
        with pytest.raises(ModelError) as error:
            chat.complete("s", "u", SCHEMA, "demo")
        assert error.value.timeout  # 发出去了、没拿到回复：可能算了钱
    finally:
        server.shutdown()
        server.server_close()


def test_tls_timeout_behind_a_working_proxy_is_not_a_proxy_error() -> None:
    """② 代理回了 CONNECT 200，后面远端握手超时：是网络的事，不是代理连不上。"""

    class Silent(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data += chunk
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.request.settimeout(2)
            try:
                while self.request.recv(4096):  # 收下 TLS 的 ClientHello，什么都不回
                    pass
            except OSError:
                pass

    proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Silent)
    threading.Thread(target=proxy.serve_forever, args=(0.05,), daemon=True).start()
    try:
        chat = OpenAIChat(
            settings(JAVMEM_LLM_PROXY_URL=f"http://127.0.0.1:{proxy.server_address[1]}", JAVMEM_LLM_TIMEOUT_SECONDS="0.3")
        )
        with pytest.raises(ModelError) as error:
            chat.complete("s", "u", SCHEMA, "demo")
        assert error.value.kind == TRANSPORT and not error.value.fatal
    finally:
        proxy.shutdown()
        proxy.server_close()
