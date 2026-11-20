"""调模型：抽取模型的设置、真实的客户端、测试用的假模型。

设置都从环境变量 JAVMEM_LLM_* 读，见 docs/development.md 的“配置”。
密钥只从 JAVMEM_LLM_API_KEY 读：不打印，不写文件，报错里也不带。
发出去的内容和模型的回复都不进日志和报错。

真实的客户端走 OpenAI 兼容的 Chat Completions 接口，只用标准库：
https 加证书校验；配了代理就用 HTTP CONNECT 走它，代理只认本机的 http 地址。

Flex（D169，11-17）：配了 JAVMEM_LLM_SERVICE_TIER=flex，请求里写 service_tier: "flex"，OpenAI 按批量的价（标准价的一半）算，
回得慢、偶尔资源不够（429，官网说不收钱，UNAVAILABLE，等一会儿再试）。回复里的 service_tier 是实际服务的那一档，记账照它。
结果的键不变。不配的请求一个字不变。
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from javmem.config import ConfigError

ENV_BASE_URL = "JAVMEM_LLM_BASE_URL"
ENV_MODEL = "JAVMEM_LLM_MODEL"
ENV_API_KEY = "JAVMEM_LLM_API_KEY"
ENV_REASONING_EFFORT = "JAVMEM_LLM_REASONING_EFFORT"
ENV_PROXY_URL = "JAVMEM_LLM_PROXY_URL"
ENV_TIMEOUT = "JAVMEM_LLM_TIMEOUT_SECONDS"
ENV_READ_TIMEOUT = "JAVMEM_LLM_READ_TIMEOUT_SECONDS"
ENV_STRUCTURED_OUTPUT = "JAVMEM_LLM_STRUCTURED_OUTPUT"
ENV_MAX_OUTPUT_TOKENS = "JAVMEM_LLM_MAX_OUTPUT_TOKENS"
ENV_SERVICE_TIER = "JAVMEM_LLM_SERVICE_TIER"
# L4 的模型复核（D63）：没写的跟抽取模型一样；地址、密钥、代理、超时、输出方式都用 JAVMEM_LLM_* 的
ENV_RECHECK_MODEL = "JAVMEM_RECHECK_MODEL"
ENV_RECHECK_REASONING_EFFORT = "JAVMEM_RECHECK_REASONING_EFFORT"
ENV_RECHECK_MAX_OUTPUT_TOKENS = "JAVMEM_RECHECK_MAX_OUTPUT_TOKENS"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT_TOKENS = 32000
EFFORT_AUTO = "auto"  # 不传 reasoning_effort，用接口自己的默认
EFFORT_OFF = "off"  # 不是推理模型：传 temperature 0
OUTPUT_MODES = ("json_schema", "json_object")
SERVICE_TIERS = ("flex",)  # 配了才在请求里写 service_tier；不配和以前一样
FLEX_READ_TIMEOUT = 900.0  # Flex 回得慢：等回复至少这么久（官网的例子是 15 分钟）。超时了多半照样算钱，多等不花钱
_EFFORT = re.compile(r"[a-z]{2,20}", re.ASCII)

# 出错的种类。后四种是停下整个这次运行的：密钥、额度、请求本身、代理，重试也没用
RATE_LIMIT = "rate_limit"
SERVER = "server_error"
TRANSPORT = "transport"
REFUSAL = "refusal"
TOO_LONG = "too_long"  # 这个窗口超出了模型能收的长度，只是这一个窗口的事
CONTENT_REJECTED = "content_rejected"  # 这个窗口的内容被接口的内容审核挡了，只是这一个窗口的事
UNAVAILABLE = "unavailable"  # Flex 资源不够（429，官网说不收钱）：这一次没做，等一会儿再试，不算限流
_CONTENT_CODES = frozenset({"content_filter", "invalid_prompt", "content_policy_violation", "moderation_blocked"})
AUTH = "auth"
QUOTA = "quota"
BAD_REQUEST = "bad_request"
PROXY = "proxy"
FATAL_KINDS = frozenset({AUTH, QUOTA, BAD_REQUEST, PROXY})


class ModelError(Exception):
    """调模型出错。detail 里没有发出去的内容，也没有密钥。"""

    def __init__(
        self,
        kind: str,
        detail: str = "",
        *,
        retry_after: float | None = None,
        timeout: bool = False,
        usage: Usage | None = None,
        service_tier: str | None = None,
        response_model: str | None = None,
    ) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after
        self.timeout = timeout  # 请求发出去了、没拿到回复（超时、中途断了）：接口那边可能已经算了钱
        self.usage = usage  # 回复里写了用量的（模型拒绝、结构认不出），照样要记账
        self.service_tier = service_tier  # 回了 200 却出错的：回复说是哪一档服务的，钱按那一档算
        self.response_model = response_model  # 回了 200 却出错的：回复说它用的是哪个模型，记进 token 账（D205 ⑪）

    @property
    def fatal(self) -> bool:
        return self.kind in FATAL_KINDS


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0  # 推理的 token 也算在里面
    reasoning_tokens: int = 0
    cached_tokens: int = 0  # 输入里命中缓存的部分

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
            self.cached_tokens + other.cached_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
        }


@dataclass(frozen=True)
class ModelReply:
    content: str
    finish_reason: str | None
    usage: Usage
    model: str | None  # 接口说它用的是哪个模型
    seconds: float
    service_tier: str | None = None  # 接口说它按哪一档服务的（flex、default……）；没说的是 None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(f"{name} 要写成秒数") from None
    if not 0.1 <= value <= 3600:
        raise ConfigError(f"{name} 要在 0.1 到 3600 秒之间")
    return value


def _check_proxy(value: str) -> tuple[str, int] | None:
    """代理只认本机的 http://地址:端口，不带用户名密码。"""
    if not value:
        return None
    parts = urlsplit(value)
    try:
        loopback = parts.hostname is not None and ipaddress.ip_address(parts.hostname).is_loopback
        port = parts.port
    except ValueError:
        loopback, port = False, None
    if parts.scheme != "http" or not loopback or port is None or parts.username or parts.password:
        raise ConfigError(f"{ENV_PROXY_URL} 只能是本机的 http 代理，比如 http://127.0.0.1:7890")
    assert parts.hostname is not None
    return parts.hostname, port


def _effort(value: str, name: str) -> str:
    effort = value.strip().lower() or EFFORT_AUTO
    if not _EFFORT.fullmatch(effort):
        raise ConfigError(f"{name} 写 auto、off，或者 low、medium、high 这样的档位")
    return effort


def _max_tokens(value: str, name: str) -> int:
    text = value.strip()
    try:
        tokens = int(text) if text else DEFAULT_MAX_OUTPUT_TOKENS
    except ValueError:
        raise ConfigError(f"{name} 要写成整数") from None
    if not 256 <= tokens <= 200000:
        raise ConfigError(f"{name} 要在 256 到 200000 之间")
    return tokens


def _model(value: str, name: str) -> str:
    model = value.strip() or DEFAULT_MODEL
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
        raise ConfigError(f"{name} 写法不对")
    return model


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class LLMSettings:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = field(default="", repr=False)
    reasoning_effort: str = EFFORT_AUTO
    proxy: tuple[str, int] | None = None
    timeout: float = DEFAULT_TIMEOUT
    read_timeout: float = DEFAULT_TIMEOUT
    structured_output: str = "json_schema"
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    service_tier: str = ""  # "" 不写 service_tier（和以前一样）；"flex" 走 Flex

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMSettings:
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
            # 不在这里带用户名、密码、查询参数：describe() 会打印地址，查询参数还会被丢掉
            raise ConfigError(f"{ENV_BASE_URL} 里不能带用户名、密码、? 后面的参数和 #")
        if parts.scheme == "http" and not _is_loopback(hostname):
            raise ConfigError(f"{ENV_BASE_URL} 不是本机的要用 https")
        api_key = env.get(ENV_API_KEY, "").strip()
        if api_key and not re.fullmatch(r"[\x21-\x7e]+", api_key):
            raise ConfigError(f"{ENV_API_KEY} 只能是一串可见的英文字母、数字和符号，中间不能有空格和换行")
        effort = _effort(env.get(ENV_REASONING_EFFORT, ""), ENV_REASONING_EFFORT)
        mode = env.get(ENV_STRUCTURED_OUTPUT, "").strip() or "json_schema"
        if mode not in OUTPUT_MODES:
            raise ConfigError(f"{ENV_STRUCTURED_OUTPUT} 只能是 {'、'.join(OUTPUT_MODES)}")
        max_tokens = _max_tokens(env.get(ENV_MAX_OUTPUT_TOKENS, ""), ENV_MAX_OUTPUT_TOKENS)
        timeout = _float(env, ENV_TIMEOUT, DEFAULT_TIMEOUT)
        model = _model(env.get(ENV_MODEL, ""), ENV_MODEL)
        tier = env.get(ENV_SERVICE_TIER, "").strip().lower()
        if tier and tier not in SERVICE_TIERS:
            raise ConfigError(f"{ENV_SERVICE_TIER} 不写，或者写 {'、'.join(SERVICE_TIERS)}")
        read_timeout = _float(env, ENV_READ_TIMEOUT, timeout)
        if tier == "flex":
            read_timeout = max(read_timeout, FLEX_READ_TIMEOUT)
        return cls(
            base_url=base_url.rstrip("/"),
            model=model,
            api_key=api_key,
            reasoning_effort=effort,
            proxy=_check_proxy(env.get(ENV_PROXY_URL, "").strip()),
            timeout=timeout,
            read_timeout=read_timeout,
            structured_output=mode,
            max_output_tokens=max_tokens,
            service_tier=tier,
        )

    @classmethod
    def recheck_from_env(cls, env: Mapping[str, str] | None = None) -> LLMSettings:
        """L4 模型复核用的：JAVMEM_RECHECK_MODEL、_REASONING_EFFORT、_MAX_OUTPUT_TOKENS，没写的跟抽取模型一样。"""
        env = os.environ if env is None else env
        base = cls.from_env(env)
        model = env.get(ENV_RECHECK_MODEL, "").strip()
        effort = env.get(ENV_RECHECK_REASONING_EFFORT, "").strip()
        tokens = env.get(ENV_RECHECK_MAX_OUTPUT_TOKENS, "").strip()
        return replace(
            base,
            model=_model(model, ENV_RECHECK_MODEL) if model else base.model,
            reasoning_effort=_effort(effort, ENV_RECHECK_REASONING_EFFORT) if effort else base.reasoning_effort,
            max_output_tokens=_max_tokens(tokens, ENV_RECHECK_MAX_OUTPUT_TOKENS) if tokens else base.max_output_tokens,
        )

    def describe(self) -> dict[str, object]:
        """不是密钥的设置，可以打印、写进报告。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "proxy": f"http://{self.proxy[0]}:{self.proxy[1]}" if self.proxy else None,
            "timeout": self.timeout,
            "read_timeout": self.read_timeout,
            "structured_output": self.structured_output,
            "max_output_tokens": self.max_output_tokens,
            "service_tier": self.service_tier or None,
            "api_key_set": bool(self.api_key),
        }


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Retry-After、retry-after-ms，或者 x-ratelimit-reset-* 里的 1s、6m0s、20ms。"""
    if "retry-after-ms" in headers:
        try:
            value = float(headers["retry-after-ms"]) / 1000
            if math.isfinite(value) and value >= 0:
                return value
        except ValueError:
            pass
    if "retry-after" in headers:
        try:
            value = float(headers["retry-after"])
            if math.isfinite(value) and value >= 0:
                return value
        except ValueError:
            pass
    waits = []
    for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        text = headers.get(name, "")
        parts = re.findall(r"([\d.]+)(ms|s|m|h)", text)
        if parts:
            scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
            try:
                waits.append(sum(float(number) * scale[unit] for number, unit in parts))
            except ValueError:  # “.s” 这种写坏了的，当没说
                continue
    finite = [wait for wait in waits if math.isfinite(wait)]
    return max(finite) if finite else None


_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def _error_info(data: bytes) -> tuple[str, str]:
    """错误回复里的 type 和 code，只认短的英文标识。message 不要：它可能抄着发出去的内容。"""
    try:
        error = json.loads(data.decode("utf-8")).get("error") or {}
    except (ValueError, UnicodeDecodeError, AttributeError, RecursionError):
        return "", ""
    if not isinstance(error, dict):
        return "", ""
    kind, code = str(error.get("type") or ""), str(error.get("code") or "")
    return (kind if _LABEL.fullmatch(kind) else ""), (code if _LABEL.fullmatch(code) else "")


def _count(value: object) -> int:
    """回复里的 token 数。不是正常的整数就当没有。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    return 0


def _usage_of(reply: Mapping[str, object]) -> Usage:
    usage = reply.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    return Usage(
        _count(usage.get("prompt_tokens")),
        _count(usage.get("completion_tokens")),
        _count(details.get("reasoning_tokens")),
        _count(prompt_details.get("cached_tokens")),
    )


def _text_of(content: object) -> str | None:
    """回复的正文：一段文字，或者几段 {type: text, text: …} 拼起来。别的认不出来。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        pieces = []
        for part in content:
            # 推理摘要、图片这类不是正文的段落跳过，一段正文都没有才算认不出来
            if isinstance(part, dict) and part.get("type", "text") in ("text", "output_text"):
                if isinstance(part.get("text"), str):
                    pieces.append(part["text"])
        return "".join(pieces) if pieces else None
    return None


def cache_name_of(settings: LLMSettings) -> str:
    """结果的键里用的“模型”：模型名加推理档位和输出方式，这两个也会改变回复。"""
    return f"{settings.model}@{settings.reasoning_effort}/{settings.structured_output}"


@dataclass(frozen=True)
class ModelName:
    """只要名字、不调用的时候用（--dry-run 算键），不要密钥。"""

    settings: LLMSettings

    @property
    def name(self) -> str:
        return self.settings.model

    @property
    def cache_name(self) -> str:
        return cache_name_of(self.settings)

    def complete(self, system: str, user: str, schema: Mapping[str, object], schema_name: str) -> ModelReply:
        raise ModelError(AUTH, "只算键，不调用")


class _TunnelMark:
    """记下代理的 CONNECT 通没通：通了以后的超时（远端握手慢）是网络的事，不是代理连不上。"""

    tunnel_ok = False

    def _tunnel(self) -> None:
        super()._tunnel()  # type: ignore[misc]
        self.tunnel_ok = True


class _HTTPSConnection(_TunnelMark, http.client.HTTPSConnection):
    pass


class _HTTPConnection(_TunnelMark, http.client.HTTPConnection):
    pass


class OpenAIChat:
    """OpenAI 兼容的 Chat Completions。一个实例可以在几个线程里同时用，每次请求一个连接。"""

    def __init__(self, settings: LLMSettings) -> None:
        if not settings.api_key:
            raise ConfigError(f"没有设置 {ENV_API_KEY}")
        self.settings = settings
        self._url = urlsplit(settings.base_url + "/chat/completions")

    @property
    def name(self) -> str:
        return self.settings.model

    @property
    def cache_name(self) -> str:
        return cache_name_of(self.settings)

    @property
    def service_tier(self) -> str:
        """要的是哪一档（"" 是没写，按接口的默认）。记账时回复没说实际是哪一档的，照这个算。"""
        return self.settings.service_tier

    def payload(self, system: str, user: str, schema: Mapping[str, object], schema_name: str) -> dict[str, object]:
        settings = self.settings
        body: dict[str, object] = {
            "model": settings.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": settings.max_output_tokens,
        }
        if settings.structured_output == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        else:
            body["response_format"] = {"type": "json_object"}
        if settings.reasoning_effort == EFFORT_OFF:
            body["temperature"] = 0
        elif settings.reasoning_effort != EFFORT_AUTO:
            body["reasoning_effort"] = settings.reasoning_effort
        if settings.service_tier:
            body["service_tier"] = settings.service_tier
        return body

    def _connection(self) -> http.client.HTTPConnection:
        settings = self.settings
        url = self._url
        secure = url.scheme == "https"
        port = url.port or (443 if secure else 80)
        host = settings.proxy[0] if settings.proxy else url.hostname
        connect_port = settings.proxy[1] if settings.proxy else port
        assert host is not None and url.hostname is not None
        connection: http.client.HTTPConnection
        if secure:
            connection = _HTTPSConnection(host, connect_port, timeout=settings.timeout, context=ssl.create_default_context())
        else:
            connection = _HTTPConnection(host, connect_port, timeout=settings.timeout)
        if settings.proxy:
            connection.set_tunnel(url.hostname, port)
        return connection

    def _post(self, body: bytes) -> tuple[int, dict[str, str], bytes]:
        settings = self.settings
        connection = self._connection()
        try:
            try:
                connection.connect()
            except OSError as error:
                # 代理拒绝连接，或者连上了却一直不回 CONNECT：算代理的事，停下。代理回了错误码是它转不过去，算网络的事
                tunnel = "Tunnel connection failed" in str(error)
                through = getattr(connection, "tunnel_ok", False)  # CONNECT 已经通了：后面是远端的事
                if (
                    settings.proxy
                    and not tunnel
                    and not through
                    and isinstance(error, (ConnectionRefusedError, TimeoutError))
                ):
                    raise ModelError(PROXY, f"代理连不上：{type(error).__name__}") from None
                raise ModelError(TRANSPORT, "代理转不过去" if tunnel else f"连不上：{type(error).__name__}") from None
            if connection.sock is not None:
                connection.sock.settimeout(settings.read_timeout)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {settings.api_key}",
                "User-Agent": "javmem",
            }
            try:
                connection.request("POST", self._url.path, body=body, headers=headers)
            except (ValueError, UnicodeError):
                raise ModelError(AUTH, "密钥写不进请求头") from None  # 原来的报错里有密钥，什么都不抄
            try:
                response = connection.getresponse()
                data = response.read()
            except (TimeoutError, socket.timeout):
                raise ModelError(TRANSPORT, "超时", timeout=True) from None
            except (OSError, http.client.HTTPException) as error:
                # 请求已经发出去了，回复中途断了：接口那边多半照样算了钱
                raise ModelError(TRANSPORT, f"回复中途断了：{type(error).__name__}", timeout=True) from None
            return response.status, {name.lower(): value for name, value in response.getheaders()}, data
        except ModelError:
            raise
        except (TimeoutError, socket.timeout):
            raise ModelError(TRANSPORT, "超时", timeout=True) from None
        except (OSError, http.client.HTTPException) as error:
            raise ModelError(TRANSPORT, type(error).__name__) from None
        finally:
            connection.close()

    def complete(self, system: str, user: str, schema: Mapping[str, object], schema_name: str) -> ModelReply:
        body = json.dumps(self.payload(system, user, schema, schema_name), ensure_ascii=False).encode("utf-8")
        started = time.monotonic()
        status, headers, data = self._post(body)
        seconds = time.monotonic() - started
        if status != 200:
            kind, code = _error_info(data)
            label = (f"HTTP {status} " + "/".join(part for part in (kind, code) if part)).strip()
            if status == 429:
                if code == "insufficient_quota" or kind == "insufficient_quota":
                    raise ModelError(QUOTA, label)
                if self.settings.service_tier == "flex" and "rate_limit" not in code and kind not in ("requests", "tokens"):
                    # Flex 资源不够（官网叫 Resource Unavailable，不收钱）。认不出代码的 429 也算这一种：都是等一会儿再试
                    raise ModelError(UNAVAILABLE, label, retry_after=_retry_after(headers))
                raise ModelError(RATE_LIMIT, label, retry_after=_retry_after(headers))
            if status in (401, 403):
                raise ModelError(AUTH, f"HTTP {status}")  # 这种回复里可能带着打了星号的密钥，什么都不抄
            if status in (400, 413) and code == "context_length_exceeded":
                raise ModelError(TOO_LONG, label)
            if status in (400, 422) and (code in _CONTENT_CODES or kind in _CONTENT_CODES):
                raise ModelError(CONTENT_REJECTED, label)
            if status in (400, 404, 413, 422):
                raise ModelError(BAD_REQUEST, label)
            if status in (408, 409) or status >= 500:
                raise ModelError(SERVER, label, retry_after=_retry_after(headers))
            raise ModelError(TRANSPORT, label)
        try:
            reply = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ModelError(SERVER, "回复不是 JSON", timeout=True) from None  # 回了 200，多半算了钱，用量看不到
        if not isinstance(reply, dict):
            raise ModelError(SERVER, "回复的结构认不出来", timeout=True)
        usage = _usage_of(reply)  # 先取用量：后面认不出来的、拒绝回答的，钱照样算了
        tier = reply.get("service_tier")  # 实际服务的那一档：出错的也带上，记账照它
        tier = tier if isinstance(tier, str) and _LABEL.fullmatch(tier) else None
        served = reply.get("model") if isinstance(reply.get("model"), str) else None  # 实际用的模型：出错的也带上，记进账
        choices = reply.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message_part = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message_part, dict):
            raise ModelError(SERVER, "回复的结构认不出来", usage=usage, timeout=usage.total == 0, service_tier=tier,
                             response_model=served)
        if message_part.get("refusal"):
            raise ModelError(REFUSAL, "模型拒绝回答", usage=usage, timeout=usage.total == 0, service_tier=tier,
                             response_model=served)
        content = _text_of(message_part.get("content"))
        if content is None:
            raise ModelError(SERVER, "回复的正文认不出来", usage=usage, timeout=usage.total == 0, service_tier=tier,
                             response_model=served)
        finish = choice.get("finish_reason")
        return ModelReply(
            content=content,
            finish_reason=finish if isinstance(finish, str) else None,
            usage=usage,
            model=served,
            seconds=seconds,
            service_tier=tier,
        )


# ---------- 假模型 ----------

_TARGET_LINE = re.compile(r"^(T\d+) (?:〔[^〕]*〕)?(.*)$", re.MULTILINE)


def fake_extract(user: str) -> dict[str, object]:
    """假模型的“抽取”：每一行带数字的 T 行算一条事实，证据就是这一行；不抽关系。确定的，不连网。"""
    facts = []
    for ref, text in _TARGET_LINE.findall(user):
        if any(char.isdigit() for char in text):
            facts.append(
                {
                    "text": text,
                    "subject": None,
                    "attribute": None,
                    "value": None,
                    "occurred_at": None,
                    "time_precision": "unknown",
                    "certainty": "stated",
                    "speaker": "",
                    "entities": [],
                    "evidence": [{"ref": ref, "quote": text}],
                }
            )
    return {"entities": [], "facts": facts, "relations": []}


_FACTS_HEADING = "【要核的事实】\n"  # 和 javmem.pipeline.recheck.FACTS_HEADING 一样
_FACT_LINE = re.compile(r"^(F\d+)（", re.MULTILINE)


def fake_recheck(user: str) -> dict[str, object]:
    """假模型的“复核”（L4 的模型复核）：【要核的事实】下面每一条都判支持。确定的，不连网。"""
    _, _, facts = user.rpartition(_FACTS_HEADING)
    return {"verdicts": [{"id": fact_id, "verdict": "supported", "problem": ""} for fact_id in _FACT_LINE.findall(facts)]}


def fake_judgment(user: str) -> dict[str, object]:
    """假模型对合并提议的看法（javmem.samecheck）：一律拿不准。确定的，不连网。"""
    return {"same": "unsure", "reason": "假模型，没看原话"}


def _wants_judgment(schema: Mapping[str, object]) -> bool:
    """要的是合并提议的看法（有 same）。"""
    properties = schema.get("properties")
    return isinstance(properties, Mapping) and "same" in properties


def fake_relation_answer(user: str) -> dict[str, object]:
    """假模型对判断 C 第二档的回答（javmem.pipeline.reljudge）：一律看得出。确定的，不连网。"""
    return {"答案": "能", "理由": "假模型"}


def _wants_relation_answer(schema: Mapping[str, object]) -> bool:
    """要的是判断 C 第二档的回答（有“答案”）。"""
    properties = schema.get("properties")
    return isinstance(properties, Mapping) and "答案" in properties


def _wants_verdicts(schema: Mapping[str, object]) -> bool:
    """要的是复核的输出结构（有 verdicts）。"""
    properties = schema.get("properties")
    return isinstance(properties, Mapping) and "verdicts" in properties


Behaviour = str | Callable[[str, str], ModelReply]


class FakeModel:
    """测试和 CI 用的假模型。script 按顺序给出每次调用怎么回，用完了就正常回：要的是复核的输出结构（有 verdicts）
    就回 fake_recheck（每条都支持），合并提议的看法回 fake_judgment，判断 C 第二档的回答回 fake_relation_answer，
    不然回 fake_extract。script 里可以写：

    ok、rate_limit、server_error、transport、timeout、auth、quota、proxy、refusal、too_long、content_rejected、
    unavailable、bad_json、bad_schema、length，
    或者一个 (system, user) -> ModelReply 的函数。service_tier 给了的（比如 flex），当成要的是这一档、回复也说是这一档。
    """

    def __init__(
        self,
        script: Iterable[Behaviour] = (),
        *,
        delay: float = 0.0,
        name: str = "fake-model",
        max_output_tokens: int | None = None,
        service_tier: str | None = None,
    ) -> None:
        self.max_output_tokens = max_output_tokens
        self.script: deque[Behaviour] = deque(script)
        self.delay = delay
        self._name = name
        self._service_tier = service_tier
        self.calls: list[tuple[str, str]] = []
        self.in_flight = 0
        self.peak = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def cache_name(self) -> str:
        return f"{self._name}@fake/json_schema"

    @property
    def service_tier(self) -> str:
        return self._service_tier or ""

    def _reply(self, content: str, finish: str, system: str, user: str) -> ModelReply:
        usage = Usage((len(system) + len(user)) // 2, max(1, len(content) // 2))
        return ModelReply(content, finish, usage, self._name, self.delay, self._service_tier)

    def complete(self, system: str, user: str, schema: Mapping[str, object], schema_name: str) -> ModelReply:
        with self._lock:
            self.calls.append((system, user))
            behaviour = self.script.popleft() if self.script else "ok"
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            if callable(behaviour):
                return behaviour(system, user)
            if behaviour == "timeout":
                raise ModelError(TRANSPORT, "超时", timeout=True)
            if behaviour in (RATE_LIMIT, SERVER, TRANSPORT, AUTH, QUOTA, PROXY, REFUSAL, TOO_LONG, CONTENT_REJECTED, UNAVAILABLE):
                raise ModelError(behaviour, "假模型", retry_after=0.0 if behaviour in (RATE_LIMIT, UNAVAILABLE) else None)
            if behaviour == "bad_json":
                return self._reply("{不是 JSON", "stop", system, user)
            if behaviour == "bad_schema":
                return self._reply('{"entities": [], "facts": [{"text": 1}]}', "stop", system, user)
            if behaviour == "length":
                return self._reply('{"entities": [', "length", system, user)
            if _wants_judgment(schema):
                body = fake_judgment(user)
            elif _wants_relation_answer(schema):
                body = fake_relation_answer(user)
            else:
                body = fake_recheck(user) if _wants_verdicts(schema) else fake_extract(user)
            return self._reply(json.dumps(body, ensure_ascii=False), "stop", system, user)
        finally:
            with self._lock:
                self.in_flight -= 1
