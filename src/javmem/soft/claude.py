"""软变化判定调的模型：Claude（D20，主人选的），设置、真的客户端、测试用的假模型。

**设置**都从环境变量 JAVMEM_SOFT_* 读（Windows 用户级，主人自己配）：

- `JAVMEM_SOFT_API_KEY`：Claude 的密钥。只读它，不打印、不写文件、不放进命令行，报错里也不带。
- `JAVMEM_SOFT_MODEL`：默认 `claude-sonnet-5-5`（D166：考试 20 题上和 Opus 5.5 一样准，一次调用的钱约一半；
  写 `claude-opus-5-5` 换回 Opus）。`JAVMEM_SOFT_EFFORT`：默认 `high`（low、medium、high、xhigh、max）。
  请求里不写 `thinking`：是自适应思考，effort 管想多少；Sonnet 5.5、Opus 5.5 都不收 `thinking: disabled`。
- `JAVMEM_SOFT_BASE_URL`：默认 `https://api.anthropic.com`。
- `JAVMEM_SOFT_PROXY_URL`：只认本机的 http 代理；没配就用 `JAVMEM_LLM_PROXY_URL`（抽取模型那个）。
- `JAVMEM_SOFT_TIMEOUT_SECONDS`（默认 300）、`JAVMEM_SOFT_MAX_OUTPUT_TOKENS`（默认 16000，思考也算在里面）。

**真的客户端**用 Anthropic 官方 SDK（`anthropic`，可选依赖 `pip install -e .[claude]`，只有真调时才 import）。
每次发：Messages API 的 beta 那一套（服务端换模型重答要 beta）；系统提示词带 `cache_control`（固定的，第二次起
按缓存算）；`output_config` 里写 effort 和 JSON Schema（回的一定是合格的 JSON）；`fallbacks: "default"`：被安全分类
拒了，接口自己换一个 Claude 模型重答（Sonnet 5.5 只在 cyber、frontier_llm 两类上换 Sonnet 5 重答，别的几类直接回拒答），
回复里的 `model` 是实际答的那个，被拒的那次也算钱（用量记在 `usage.iterations`，记账照它加）。整条链都拒了的
（stop_reason refusal）算拒答。SDK 自己的重试关掉（max_retries=0），重试、退避、记账都走
javmem.pipeline.extract 那一套，每次调用都记账。

**SDK 读的环境变量**：密钥和地址写明了传进去，它就不读 ANTHROPIC_API_KEY、ANTHROPIC_BASE_URL 和本机的配置文件；
可它照样读 ANTHROPIC_CUSTOM_HEADERS（加请求头，能换掉密钥那一行）和 ANTHROPIC_LOG（调试日志里有发出去的原文），
这两个有值就不建客户端。ANTHROPIC_OPEN_TELEMETRY*（追踪里记请求内容）传 open_telemetry 关掉。系统代理（环境变量
`HTTP(S)_PROXY`、系统设置里配的代理）SDK 自带的 httpx2 客户端不管 trust_env 都会用，所以客户端自己建：只走 JAVMEM_SOFT_PROXY_URL，
不跟重定向（密钥那一行不会被带去别的地方）。anthropic、httpx2、httpcore2 的日志压到 WARNING。

发出去的内容和模型的回复都不进日志和报错。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from javmem.config import ConfigError
from javmem.llm import (
    AUTH,
    BAD_REQUEST,
    ENV_PROXY_URL as ENV_LLM_PROXY_URL,
    PROXY,
    QUOTA,
    RATE_LIMIT,
    REFUSAL,
    SERVER,
    TOO_LONG,
    TRANSPORT,
    ModelError,
    ModelReply,
    Usage,
    _check_proxy,
    _count,
    _is_loopback,
    _retry_after,
)

if TYPE_CHECKING:  # 只给类型检查看：SDK 是可选依赖，真调时才 import
    import httpx2

ENV_API_KEY = "JAVMEM_SOFT_API_KEY"
ENV_MODEL = "JAVMEM_SOFT_MODEL"
ENV_EFFORT = "JAVMEM_SOFT_EFFORT"
ENV_BASE_URL = "JAVMEM_SOFT_BASE_URL"
ENV_PROXY_URL = "JAVMEM_SOFT_PROXY_URL"
ENV_TIMEOUT = "JAVMEM_SOFT_TIMEOUT_SECONDS"
ENV_MAX_OUTPUT_TOKENS = "JAVMEM_SOFT_MAX_OUTPUT_TOKENS"

DEFAULT_MODEL = "claude-sonnet-5-5"  # D166（主人 11-17 定的）：以前是 claude-opus-5-5
DEFAULT_EFFORT = "high"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_OUTPUT_TOKENS = 16000
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # 服务端换模型重答（fallbacks: "default"）
# SDK 写明了密钥也照样读的、会改请求或者把原文写出去的环境变量：有值就不建客户端
REFUSED_ENV = (
    ("ANTHROPIC_CUSTOM_HEADERS", "它会往请求里加请求头，连密钥那一行都能换掉"),
    ("ANTHROPIC_LOG", "它会打开调试日志，日志里有发出去的原文"),
)
QUIET_LOGGERS = ("anthropic", "httpx2", "httpcore2")
CONNECT_TIMEOUT = 10.0  # 连上接口（或者本机的代理）、握手最多等几秒：连不上的不要每次等满整个超时（SDK 自己默认 5 秒）


def proxy_url(proxy: tuple[str, int] | None) -> str | None:
    """代理的地址；IPv6 的主机加方括号（http://[::1]:7890）。"""
    if proxy is None:
        return None
    host, port = proxy
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(f"{name} 要写成秒数") from None
    if not 1 <= value <= 3600:
        raise ConfigError(f"{name} 要在 1 到 3600 秒之间")
    return value


@dataclass(frozen=True)
class SoftSettings:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = field(default="", repr=False)
    effort: str = DEFAULT_EFFORT
    proxy: tuple[str, int] | None = None
    timeout: float = DEFAULT_TIMEOUT
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SoftSettings:
        """写错了报 ConfigError，消息里不带密钥。密钥没配不报错：dry run、假模型用不着，真调之前再查（require_key）。"""
        env = os.environ if env is None else env
        base_url = env.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
        try:
            parts = urlsplit(base_url)
            hostname, _ = parts.hostname, parts.port
        except ValueError:
            raise ConfigError(f"{ENV_BASE_URL} 要写成 https://… 的地址") from None
        if parts.scheme not in ("https", "http") or not hostname:
            raise ConfigError(f"{ENV_BASE_URL} 要写成 https://… 的地址")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ConfigError(f"{ENV_BASE_URL} 里不能带用户名、密码、? 后面的参数和 #")
        if parts.scheme == "http" and not _is_loopback(hostname):
            raise ConfigError(f"{ENV_BASE_URL} 不是本机的要用 https")
        api_key = env.get(ENV_API_KEY, "").strip()
        if api_key and not re.fullmatch(r"[\x21-\x7e]+", api_key):
            raise ConfigError(f"{ENV_API_KEY} 只能是一串可见的英文字母、数字和符号，中间不能有空格和换行")
        model = env.get(ENV_MODEL, "").strip() or DEFAULT_MODEL
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
            raise ConfigError(f"{ENV_MODEL} 写法不对")
        effort = env.get(ENV_EFFORT, "").strip().lower() or DEFAULT_EFFORT
        if effort not in EFFORTS:
            raise ConfigError(f"{ENV_EFFORT} 只能是 {'、'.join(EFFORTS)}")
        proxy_text = env.get(ENV_PROXY_URL, "").strip() or env.get(ENV_LLM_PROXY_URL, "").strip()
        try:
            proxy = _check_proxy(proxy_text)
        except ConfigError:
            raise ConfigError(f"{ENV_PROXY_URL}（没配时用 {ENV_LLM_PROXY_URL}）只能是本机的 http 代理，比如 http://127.0.0.1:7890") from None
        tokens_text = env.get(ENV_MAX_OUTPUT_TOKENS, "").strip()
        try:
            tokens = int(tokens_text) if tokens_text else DEFAULT_MAX_OUTPUT_TOKENS
        except ValueError:
            raise ConfigError(f"{ENV_MAX_OUTPUT_TOKENS} 要写成整数") from None
        if not 1024 <= tokens <= 128000:
            raise ConfigError(f"{ENV_MAX_OUTPUT_TOKENS} 要在 1024 到 128000 之间")
        return cls(base_url.rstrip("/"), model, api_key, effort, proxy, _float(env, ENV_TIMEOUT, DEFAULT_TIMEOUT), tokens)

    def require_key(self) -> None:
        if not self.api_key:
            raise ConfigError(f"没配 {ENV_API_KEY}：真调 Claude 要密钥（配进用户级环境变量），测试用 --fake")

    @property
    def cache_name(self) -> str:
        """结论的键里用的“模型”：模型名加 effort，effort 也会改变回复。"""
        return f"{self.model}@{self.effort}"

    def describe(self) -> dict[str, object]:
        """不是密钥的设置，可以打印、写进报告。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "effort": self.effort,
            "proxy": proxy_url(self.proxy),
            "timeout": self.timeout,
            "max_output_tokens": self.max_output_tokens,
            "fallbacks": "default",
            "api_key_set": bool(self.api_key),
        }


def request_kwargs(settings: SoftSettings, system: str, user: str, schema: Mapping[str, object]) -> dict[str, object]:
    """发给 `client.beta.messages.create` 的参数。不连网、不要 SDK，测试直接看它。"""
    return {
        "model": settings.model,
        "max_tokens": settings.max_output_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": settings.effort, "format": {"type": "json_schema", "schema": dict(schema)}},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }


def _get(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _counts(usage: object) -> tuple[int, int, int, int]:
    """一份用量：（输入连缓存命中的和写进缓存的，输出，思考，缓存命中的）。"""
    read = _count(_get(usage, "cache_read_input_tokens"))
    written = _count(_get(usage, "cache_creation_input_tokens"))
    details = _get(usage, "output_tokens_details")
    thinking = _count(_get(details, "thinking_tokens")) if details is not None else 0
    return _count(_get(usage, "input_tokens")) + read + written, _count(_get(usage, "output_tokens")), thinking, read


def usage_of(response: object) -> Usage:
    """回复里的用量。账里的“输入”连缓存命中的、写进缓存的都算（和抽取模型的记法一样：缓存是输入里的一部分）。

    服务端换模型重答时，被拒的那次也算钱，只记在 `usage.iterations`：每一项加起来，和最外面那份取大的，宁多记不少记。"""
    usage = _get(response, "usage")
    if usage is None:
        return Usage()
    counts = list(_counts(usage))
    iterations = _get(usage, "iterations")
    if isinstance(iterations, (list, tuple)):
        for place in range(len(counts)):
            counts[place] = max(counts[place], sum(_counts(step)[place] for step in iterations))
    return Usage(*counts)


def text_of(response: object) -> str:
    """回复的正文：text 那几块拼起来（思考的块跳过）。"""
    pieces = []
    blocks = _get(response, "content")
    for block in blocks if isinstance(blocks, (list, tuple)) else []:
        if _get(block, "type") == "text" and isinstance(_get(block, "text"), str):
            pieces.append(str(_get(block, "text")))
    return "".join(pieces)


_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def _error_type(error: object) -> str:
    """错误回复里的 error.type，只认短的英文标识；message 不要：它可能抄着发出去的内容。"""
    body = getattr(error, "body", None)
    inner = body.get("error") if isinstance(body, Mapping) else None
    kind = inner.get("type") if isinstance(inner, Mapping) else None
    return kind if isinstance(kind, str) and _LABEL.fullmatch(kind) else ""


def _not_sent(error: BaseException) -> tuple[bool, str]:
    """连接出的错：请求是不是一定没到接口（连不上、排不上队、代理不转），和底下那个错的名字。
    沿 __cause__ 找 httpx2 的错；找不到的、读回复时出的（超时、中途断了）都当可能已经到了，可能算了钱。"""
    import httpx2

    never = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.ProxyError, httpx2.UnsupportedProtocol)
    seen: set[int] = set()
    current = error.__cause__
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, never):
            return True, type(current).__name__
        if isinstance(current, httpx2.TransportError):
            return False, type(current).__name__
        current = current.__cause__
    return False, ""


def _refused(error: BaseException) -> bool:
    """底下是不是“连接被拒”（ConnectionRefusedError）。沿 __cause__（没有就 __context__）一路找下去。"""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        current = current.__cause__ or current.__context__
    return False


def model_error(error: BaseException, *, proxied: bool = False) -> ModelError:
    """SDK 的错换成 javmem 的 ModelError（种类决定重试不重试、整批停不停）。认不出的当服务器错。
    proxied：配了本机代理。走代理时连接被拒，就是代理没开：和 L3 一样算代理的错，整批停下，不一道一道地重试。"""
    import anthropic  # 只有真调才走到这里

    name = type(error).__name__
    status = getattr(error, "status_code", None)
    detail = f"{name}{f' {status}' if status else ''}{f' {_error_type(error)}' if _error_type(error) else ''}"
    if isinstance(error, anthropic.APIConnectionError):  # APITimeoutError 也是它
        never, cause = _not_sent(error)
        if proxied and never and _refused(error):
            return ModelError(PROXY, f"代理连不上：{detail}{f' {cause}' if cause else ''}")
        # 发出去了才断的（读超时、回复中途断了）：和 L3 一样当“可能已经算了钱”，只再试一次、按估计记账
        return ModelError(TRANSPORT, f"{detail}{f' {cause}' if cause else ''}", timeout=not never)
    # 限额用完是 RateLimitError 的子类（429）、组织被停用是 BadRequestError 的子类：先认它们，整批停下，不当限流重试
    if isinstance(error, (anthropic.EnforcedSpendLimitReachedError, anthropic.OrganizationOnHoldError)):
        return ModelError(QUOTA, detail)
    if isinstance(error, anthropic.RateLimitError):
        headers = getattr(getattr(error, "response", None), "headers", None) or {}
        return ModelError(RATE_LIMIT, detail, retry_after=_retry_after({k.lower(): v for k, v in dict(headers).items()}))
    if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ModelError(AUTH, detail)
    if isinstance(error, (anthropic.RequestTooLargeError, anthropic.ContentTooLargeError)):
        return ModelError(TOO_LONG, detail)
    if isinstance(error, anthropic.APIStatusError):
        if status is not None and (status >= 500 or status in (408, 409, 499)):  # SDK 自己也重试这几种
            return ModelError(SERVER, detail)
        if _error_type(error) in ("billing_error", "insufficient_credits", "credit_balance_too_low"):
            return ModelError(QUOTA, detail)
        return ModelError(BAD_REQUEST, detail)  # 请求本身不对（模型名、参数）：重试也没用，整批停下
    return ModelError(SERVER, name)


def _keepalive() -> list[tuple[int, int, int]]:
    """和 SDK 自带的客户端一样开 TCP keepalive：effort 高的一次要想几分钟，中间没有字节，免得连接被路上的设备掐掉。"""
    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 60), ("TCP_KEEPCNT", 5)):
        option = getattr(socket, name, None)
        if option is not None:
            options.append((socket.IPPROTO_TCP, option, value))
    return options


def http_client(settings: SoftSettings) -> httpx2.Client:
    """给 SDK 用的 httpx2 客户端：只走设置里的代理（没配就直连），不读系统代理、SSL_CERT_* 这些环境变量，不跟重定向。"""
    import httpx2

    transport = httpx2.HTTPTransport(proxy=proxy_url(settings.proxy), trust_env=False, socket_options=_keepalive())
    return httpx2.Client(transport=transport, trust_env=False, follow_redirects=False,
                         timeout=httpx2.Timeout(settings.timeout, connect=CONNECT_TIMEOUT))


class ClaudeModel:
    """真的 Claude。client 可以传进来（测试用一个假的，有 `beta.messages.create` 就行）；不传的第一次调用时建。"""

    def __init__(self, settings: SoftSettings, *, client: object | None = None) -> None:
        self.settings = settings
        self._client = client
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.settings.model

    @property
    def cache_name(self) -> str:
        return self.settings.cache_name

    @property
    def max_output_tokens(self) -> int:
        return self.settings.max_output_tokens

    def client(self) -> object:
        with self._lock:
            if self._client is None:
                self.settings.require_key()
                for name, why in REFUSED_ENV:
                    if os.environ.get(name, "").strip():
                        raise ConfigError(f"环境变量 {name} 有值，Anthropic 的 SDK 会读它：{why}。真调之前先把它删掉")
                try:
                    import anthropic
                    import httpx2  # SDK 自己带的
                except ImportError:
                    raise ConfigError("没装 Anthropic 的 SDK：pip install -e .[claude]") from None
                for name in QUIET_LOGGERS:
                    logging.getLogger(name).setLevel(logging.WARNING)  # DEBUG 那一级的日志里有发出去的原文
                self._client = anthropic.Anthropic(
                    api_key=self.settings.api_key,  # 写明了密钥和地址：SDK 不读 ANTHROPIC_API_KEY、ANTHROPIC_BASE_URL 和本机的配置
                    base_url=self.settings.base_url,
                    timeout=httpx2.Timeout(self.settings.timeout, connect=CONNECT_TIMEOUT),
                    max_retries=0,
                    open_telemetry={"traces": False, "propagation": False},
                    http_client=http_client(self.settings),
                )
            return self._client

    def complete(self, system: str, user: str, schema: Mapping[str, object], schema_name: str) -> ModelReply:
        del schema_name  # Anthropic 的结构化输出不用名字
        client = self.client()
        started = time.monotonic()
        try:
            response = client.beta.messages.create(**request_kwargs(self.settings, system, user, schema))  # type: ignore[attr-defined]
        except ModelError:
            raise
        except Exception as error:  # SDK 的错：换成 ModelError，不带内容
            if type(error).__module__.split(".")[0] != "anthropic":
                raise
            raise model_error(error, proxied=self.settings.proxy is not None) from None
        seconds = time.monotonic() - started
        usage = usage_of(response)
        stop = _get(response, "stop_reason")
        served = _get(response, "model")
        served = served if isinstance(served, str) else None  # 实际用的模型：拒答、太长的也带上，记进 token 账（D205 ⑪）
        if stop == "refusal":
            details = _get(response, "stop_details")
            category = _get(details, "category")
            label = category if isinstance(category, str) and _LABEL.fullmatch(category) else ""
            raise ModelError(REFUSAL, f"refusal{f' {label}' if label else ''}", usage=usage, response_model=served)
        if stop == "model_context_window_exceeded":  # 题太长，装不下：和请求太大一样，不重试
            raise ModelError(TOO_LONG, "model_context_window_exceeded", usage=usage, response_model=served)
        finish = "length" if stop == "max_tokens" else "stop"
        return ModelReply(text_of(response), finish, usage, served, seconds)


# ---------- 假模型 ----------

_OLD_LINE = re.compile(r"^(O\d+) (?:关系|事实)：", re.MULTILINE)
_TARGET_HEAD = re.compile(r"^【要判的】(.+)$", re.MULTILINE)
_SHOWN_LINE = re.compile(r"^(T\d+) \[[^\]]+\] [^：\n]*：(.*)$", re.MULTILINE)
_FAKE_RULES = (
    ("B", ("先停", "先不管", "暂停", "先放", "on hold")),
    ("C", ("取消", "算了", "不做了")),
    ("D", ("交给", "换成", "改由")),
    ("E", ("如果", "可能", "考虑", "打算")),
)


def fake_soft(user: str) -> dict[str, object]:
    """假模型的“判定”：按要判那几行里的几个词，每条旧的回一样的结论。确定的，不连网，只给测试用。"""
    shown = dict(_SHOWN_LINE.findall(user))
    head = _TARGET_HEAD.search(user)
    targets = re.findall(r"T\d+", head.group(1)) if head else []
    text = "".join(shown.get(ref, "") for ref in targets)
    verdict = next((letter for letter, words in _FAKE_RULES if any(word in text for word in words)), "A")
    first = shown.get(targets[0], "") if targets else ""
    quote = first[: min(len(first), 8)]
    return {
        "items": [
            {
                "target": ref,
                "about": verdict != "A",
                "verdict": verdict,
                "condition": None,
                "effective_at": None,
                "effective_precision": "unknown",
                "new_value": None,
                "sure": True,
                "finance": False,
                "quote": quote,
                "reason": "假模型按词判的",
            }
            for ref in _OLD_LINE.findall(user)
        ]
    }


Behaviour = str | Callable[[str, str], ModelReply]


class FakeSoftModel:
    """测试用的假模型。script 按顺序给出每次调用怎么回，用完了就回 fake_soft。script 里可以写：
    ok、rate_limit、server_error、transport、timeout、auth、quota、refusal、too_long、bad_json、bad_schema、length，
    或者一个 (system, user) -> ModelReply 的函数。"""

    def __init__(self, script: Iterable[Behaviour] = (), *, name: str = "fake-claude", max_output_tokens: int = 2000) -> None:
        self.script: deque[Behaviour] = deque(script)
        self._name = name
        self.max_output_tokens = max_output_tokens
        self.calls: list[tuple[str, str]] = []
        self.in_flight = 0
        self.peak = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def cache_name(self) -> str:
        return f"{self._name}@fake"

    def _reply(self, content: str, finish: str, system: str, user: str) -> ModelReply:
        return ModelReply(content, finish, Usage((len(system) + len(user)) // 2, max(1, len(content) // 2)), self._name, 0.0)

    def complete(self, system: str, user: str, schema: Mapping[str, object], schema_name: str) -> ModelReply:
        with self._lock:
            self.calls.append((system, user))
            behaviour = self.script.popleft() if self.script else "ok"
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            if callable(behaviour):
                return behaviour(system, user)
            if behaviour == "timeout":
                raise ModelError(TRANSPORT, "超时", timeout=True)
            if behaviour in (RATE_LIMIT, SERVER, TRANSPORT, AUTH, QUOTA, TOO_LONG):
                raise ModelError(behaviour, "假模型", retry_after=0.0 if behaviour == RATE_LIMIT else None)
            if behaviour == REFUSAL:
                raise ModelError(REFUSAL, "refusal", usage=Usage(100, 5))
            if behaviour == "bad_json":
                return self._reply("{不是 JSON", "stop", system, user)
            if behaviour == "bad_schema":
                return self._reply('{"items": [{"target": 1}]}', "stop", system, user)
            if behaviour == "length":
                return self._reply('{"items": [', "length", system, user)
            return self._reply(json.dumps(fake_soft(user), ensure_ascii=False), "stop", system, user)
        finally:
            with self._lock:
                self.in_flight -= 1
