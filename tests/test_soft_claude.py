"""软变化判定调的 Claude（J4.5，javmem.soft.claude）：设置、发出去的参数、回复和出错怎么换。不连网：客户端是假的。"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from javmem.config import ConfigError
from javmem.llm import AUTH, BAD_REQUEST, PROXY, QUOTA, RATE_LIMIT, REFUSAL, SERVER, TOO_LONG, TRANSPORT, ModelError
from javmem.soft.claude import (
    DEFAULT_MODEL,
    FALLBACK_BETA,
    ClaudeModel,
    SoftSettings,
    request_kwargs,
    usage_of,
)
from javmem.soft.judge import SCHEMA

KEY = "sk-test-not-a-real-key-0000"


def settings(**env: str) -> SoftSettings:
    return SoftSettings.from_env({"JAVMEM_SOFT_API_KEY": KEY, **env})


def test_defaults_and_description_without_the_key() -> None:
    soft = SoftSettings.from_env({})
    assert (soft.model, soft.effort, soft.base_url, soft.proxy) == (DEFAULT_MODEL, "high", "https://api.anthropic.com", None)
    assert DEFAULT_MODEL == "claude-sonnet-5-5" and soft.cache_name == "claude-sonnet-5-5@high"  # D166：默认 Sonnet 5.5
    with pytest.raises(ConfigError):
        soft.require_key()
    described = settings().describe()
    assert described["api_key_set"] is True and KEY not in str(described) and KEY not in repr(settings())


def test_proxy_falls_back_to_the_extraction_proxy_and_must_be_local() -> None:
    assert SoftSettings.from_env({"JAVMEM_LLM_PROXY_URL": "http://127.0.0.1:7890"}).proxy == ("127.0.0.1", 7890)
    assert SoftSettings.from_env({"JAVMEM_SOFT_PROXY_URL": "http://127.0.0.1:1080",
                                  "JAVMEM_LLM_PROXY_URL": "http://127.0.0.1:7890"}).proxy == ("127.0.0.1", 1080)
    with pytest.raises(ConfigError):
        SoftSettings.from_env({"JAVMEM_SOFT_PROXY_URL": "http://10.0.0.5:3128"})


@pytest.mark.parametrize(
    "env",
    [
        {"JAVMEM_SOFT_API_KEY": "sk test with spaces"},
        {"JAVMEM_SOFT_BASE_URL": "http://api.example.com"},
        {"JAVMEM_SOFT_BASE_URL": "https://user:pw@api.example.com"},
        {"JAVMEM_SOFT_EFFORT": "turbo"},
        {"JAVMEM_SOFT_MODEL": "claude opus"},
        {"JAVMEM_SOFT_MAX_OUTPUT_TOKENS": "12"},
        {"JAVMEM_SOFT_TIMEOUT_SECONDS": "soon"},
    ],
)
def test_bad_settings_are_config_errors_without_the_key(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as error:
        SoftSettings.from_env(env)
    assert "sk test" not in str(error.value)


def test_request_has_cache_effort_schema_and_fallbacks_but_no_sampling() -> None:
    kwargs = request_kwargs(settings(JAVMEM_SOFT_EFFORT="medium"), "系统提示词", "一道题", SCHEMA)
    assert kwargs["model"] == DEFAULT_MODEL and kwargs["max_tokens"] == 16000
    assert kwargs["system"] == [{"type": "text", "text": "系统提示词", "cache_control": {"type": "ephemeral"}}]
    assert kwargs["messages"] == [{"role": "user", "content": "一道题"}]
    assert kwargs["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert kwargs["betas"] == [FALLBACK_BETA] and kwargs["fallbacks"] == "default"
    # 不发采样参数；不写 thinking（自适应思考）：Sonnet 5.5、Opus 5.5 都不收 thinking disabled；不用工具：强制 tool_choice 它们也不收
    assert not {"temperature", "top_p", "top_k", "thinking", "tools", "tool_choice"} & set(kwargs)


def test_the_model_can_be_switched_back_to_opus() -> None:
    """D166 默认换成 Sonnet 5.5；JAVMEM_SOFT_MODEL 照旧能换回 Opus 5.5，结论的键跟着换。"""
    opus = settings(JAVMEM_SOFT_MODEL="claude-opus-5-5")
    assert opus.cache_name == "claude-opus-5-5@high"
    assert request_kwargs(opus, "系统", "题", SCHEMA)["model"] == "claude-opus-5-5"


class FakeClient:
    """有 beta.messages.create 就行：记下参数，回给定的东西或者抛给定的错。"""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def response(stop: str = "end_turn", text: str = '{"items": []}', **usage: object) -> SimpleNamespace:
    counts = {"input_tokens": 300, "output_tokens": 900, "cache_read_input_tokens": 2400, "cache_creation_input_tokens": 0,
              "output_tokens_details": SimpleNamespace(thinking_tokens=700), **usage}
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop, stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
        model="claude-sonnet-5-5", usage=SimpleNamespace(**counts),
    )


def test_a_reply_becomes_text_usage_and_the_model_that_answered() -> None:
    client = FakeClient(response())
    model = ClaudeModel(settings(), client=client)
    reply = model.complete("系统", "题", SCHEMA, "javmem_soft_v1")
    assert reply.content == '{"items": []}' and reply.finish_reason == "stop" and reply.model == "claude-sonnet-5-5"
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.reasoning_tokens, reply.usage.cached_tokens) == (
        2700, 900, 700, 2400)  # 输入连缓存命中的算
    assert client.calls[0]["fallbacks"] == "default"
    assert model.cache_name == "claude-sonnet-5-5@high" and model.max_output_tokens == 16000


def test_truncated_and_refused_replies() -> None:
    assert ClaudeModel(settings(), client=FakeClient(response("max_tokens"))).complete("s", "u", SCHEMA, "n").finish_reason == "length"
    with pytest.raises(ModelError) as error:
        ClaudeModel(settings(), client=FakeClient(response("refusal", text=""))).complete("s", "u", SCHEMA, "n")
    assert error.value.kind == REFUSAL and error.value.usage is not None and error.value.usage.total > 0
    assert error.value.detail == "refusal cyber"


def test_usage_without_cache_fields() -> None:
    plain = SimpleNamespace(usage=SimpleNamespace(input_tokens=5, output_tokens=7))
    assert usage_of(plain).as_dict() == {"input_tokens": 5, "output_tokens": 7, "reasoning_tokens": 0, "cached_tokens": 0}
    assert usage_of(SimpleNamespace()).total == 0


def test_a_real_client_needs_the_key() -> None:
    with pytest.raises(ConfigError):
        ClaudeModel(SoftSettings.from_env({})).complete("s", "u", SCHEMA, "n")


def _status_error(anthropic, name: str, status: int, kind: str = "error"):  # type: ignore[no-untyped-def]
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    reply = httpx2.Response(status, request=request, headers={"retry-after": "7"}, json={"error": {"type": kind, "message": "原文不进报错"}})
    return getattr(anthropic, name)("原文不进报错", response=reply, body={"error": {"type": kind, "message": "原文不进报错"}})


def test_sdk_errors_become_model_errors_without_the_message() -> None:
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    cases = [
        (_status_error(anthropic, "RateLimitError", 429, "rate_limit_error"), RATE_LIMIT),
        (_status_error(anthropic, "AuthenticationError", 401, "authentication_error"), AUTH),
        (_status_error(anthropic, "PermissionDeniedError", 403, "permission_error"), AUTH),
        (_status_error(anthropic, "BadRequestError", 400, "invalid_request_error"), BAD_REQUEST),
        (_status_error(anthropic, "BadRequestError", 400, "billing_error"), QUOTA),
        (_status_error(anthropic, "InternalServerError", 500, "api_error"), SERVER),
        (_status_error(anthropic, "RequestTooLargeError", 413, "request_too_large"), TOO_LONG),
        (anthropic.APIConnectionError(request=request), TRANSPORT),
        (anthropic.APITimeoutError(request=request), TRANSPORT),
    ]
    for raised, kind in cases:
        with pytest.raises(ModelError) as error:
            ClaudeModel(settings(), client=FakeClient(raised)).complete("s", "u", SCHEMA, "n")
        assert error.value.kind == kind, type(raised).__name__
        assert "原文不进报错" not in str(error.value)
    with pytest.raises(ModelError) as limited:
        ClaudeModel(settings(), client=FakeClient(cases[0][0])).complete("s", "u", SCHEMA, "n")
    assert limited.value.retry_after == 7.0
    with pytest.raises(ModelError) as timed_out:
        ClaudeModel(settings(), client=FakeClient(cases[-1][0])).complete("s", "u", SCHEMA, "n")
    assert timed_out.value.timeout is True


def test_other_exceptions_are_not_swallowed() -> None:
    with pytest.raises(ZeroDivisionError):
        ClaudeModel(settings(), client=FakeClient(ZeroDivisionError())).complete("s", "u", SCHEMA, "n")


# ---------- 审阅以后补的（先红后修） ----------


def test_spend_limit_and_hold_are_quota_not_rate_limit() -> None:
    """限额用完（429，SDK 里是 RateLimitError 的子类）、组织被停用：整批停下，不当限流重试。"""
    anthropic = pytest.importorskip("anthropic")
    for raised in (_status_error(anthropic, "EnforcedSpendLimitReachedError", 429, "rate_limit_error"),
                   _status_error(anthropic, "OrganizationOnHoldError", 400, "invalid_request_error")):
        with pytest.raises(ModelError) as error:
            ClaudeModel(settings(), client=FakeClient(raised)).complete("s", "u", SCHEMA, "n")
        assert error.value.kind == QUOTA and error.value.fatal, type(raised).__name__


def test_declined_fallback_attempts_are_booked_too() -> None:
    """服务端换模型重答时，被拒的那次也算钱，只记在 `usage.iterations`：记账按它加起来，不少记。"""
    iterations = [
        SimpleNamespace(type="message", model="claude-sonnet-5-5", input_tokens=3000, output_tokens=2500,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
        SimpleNamespace(type="fallback_message", model="claude-sonnet-5", input_tokens=100, output_tokens=50,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
    ]
    served = response(input_tokens=100, output_tokens=50, cache_read_input_tokens=0, iterations=iterations,
                      output_tokens_details=None)
    usage = usage_of(served)
    assert (usage.input_tokens, usage.output_tokens) == (3100, 2550)


def test_a_reply_cut_off_after_sending_counts_as_maybe_charged() -> None:
    """请求发出去了、回复中途断了：和 L3 一样当“可能已经算了钱”（timeout），只再试一次、按估计记账；连都没连上的照常重试。"""
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    cut = anthropic.APIConnectionError(request=request)
    cut.__cause__ = httpx2.RemoteProtocolError("peer closed connection")
    never = anthropic.APIConnectionError(request=request)
    never.__cause__ = httpx2.ConnectError("refused")
    slow_connect = anthropic.APITimeoutError(request=request)
    slow_connect.__cause__ = httpx2.ConnectTimeout("connect timed out")
    for raised, timeout in ((cut, True), (never, False), (slow_connect, False)):
        with pytest.raises(ModelError) as error:
            ClaudeModel(settings(), client=FakeClient(raised)).complete("s", "u", SCHEMA, "n")
        assert (error.value.kind, error.value.timeout) == (TRANSPORT, timeout), type(raised.__cause__).__name__


def test_context_window_exceeded_is_too_long() -> None:
    with pytest.raises(ModelError) as error:
        ClaudeModel(settings(), client=FakeClient(response("model_context_window_exceeded"))).complete("s", "u", SCHEMA, "n")
    assert error.value.kind == TOO_LONG


@pytest.mark.parametrize("name", ["ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG"])
def test_environment_that_would_change_or_log_the_request_is_refused(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """写明了密钥，SDK 还是会读这两个：请求头被换掉（连密钥那一行）、请求原文写进日志。有就不建客户端。
    （审阅还提了 ANTHROPIC_API_VERSION：2.8.0 不读这个环境变量，接口版本是写死的。）"""
    monkeypatch.setenv(name, "x-api-key: someone-else" if name == "ANTHROPIC_CUSTOM_HEADERS" else "debug")
    with pytest.raises(ConfigError) as error:
        ClaudeModel(settings()).client()
    assert name in str(error.value) and KEY not in str(error.value)


def _proxy_of(client: object) -> tuple[bytes, int] | None:
    """真的客户端发请求实际走的代理（httpx2 通道底下 httpcore2 连接池上记的），直连是 None。"""
    pool = client._client._transport._pool  # type: ignore[attr-defined]
    url = getattr(pool, "_proxy_url", None)
    return None if url is None else (url.host, url.port)


def test_the_real_client_ignores_proxy_variables_and_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 自带的 httpx2 客户端不管 trust_env，都按 HTTP(S)_PROXY（Windows 上还有系统的代理设置）另挂走代理的通道：
    只关 trust_env 不够，挂上的通道（_mounts）也得是空的。"""
    pytest.importorskip("anthropic")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    direct = ClaudeModel(settings()).client()
    assert direct._client.trust_env is False and direct._client._mounts == {}  # type: ignore[attr-defined]
    assert _proxy_of(direct) is None
    assert direct._client.follow_redirects is False  # type: ignore[attr-defined]  带密钥的请求不跟去别的地方
    telemetry = direct._open_telemetry  # type: ignore[attr-defined]  不把请求内容写进追踪，也不往请求里加追踪头
    assert telemetry["traces"]["enabled"] is False and telemetry["propagation"] is False
    proxied = ClaudeModel(settings(JAVMEM_SOFT_PROXY_URL="http://127.0.0.1:7890")).client()
    assert proxied._client._mounts == {} and _proxy_of(proxied) == (b"127.0.0.1", 7890)  # type: ignore[attr-defined]


def test_connecting_has_its_own_short_timeout() -> None:
    """连不上的不要每次等满 300 秒（3 次就是一刻钟）：连接、握手另有一个短的上限，等回复照旧 300 秒。"""
    pytest.importorskip("anthropic")
    client = ClaudeModel(settings()).client()
    assert (client.timeout.connect, client.timeout.read) == (10.0, 300.0)  # type: ignore[attr-defined]


def test_a_refused_local_proxy_stops_the_batch() -> None:
    """配了本机代理、连接被拒：代理没开，和 L3 一样算代理的错、整批停下；没配代理的照常算网络错误重试。"""
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def refused() -> BaseException:
        inner = httpx2.ConnectError("refused")
        inner.__cause__ = ConnectionRefusedError(10061, "refused")
        outer = anthropic.APIConnectionError(request=request)
        outer.__cause__ = inner
        return outer

    with pytest.raises(ModelError) as proxied:
        ClaudeModel(settings(JAVMEM_SOFT_PROXY_URL="http://127.0.0.1:7890"), client=FakeClient(refused())).complete(
            "s", "u", SCHEMA, "n")
    assert proxied.value.kind == PROXY and proxied.value.fatal
    with pytest.raises(ModelError) as direct:
        ClaudeModel(settings(), client=FakeClient(refused())).complete("s", "u", SCHEMA, "n")
    assert (direct.value.kind, direct.value.timeout) == (TRANSPORT, False)


def test_ipv6_proxy_is_written_with_brackets() -> None:
    soft = settings(JAVMEM_SOFT_PROXY_URL="http://[::1]:7890")
    assert soft.proxy == ("::1", 7890) and soft.describe()["proxy"] == "http://[::1]:7890"
    if importlib.util.find_spec("anthropic") is not None:
        assert _proxy_of(ClaudeModel(soft).client()) == (b"::1", 7890)
