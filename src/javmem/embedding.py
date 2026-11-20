"""事实的向量：本机的向量服务，模型 Qwen3-Embedding-4B，2560 维，定了不改。

OpenAI 兼容的 ``POST {JAVMEM_EMBEDDING_BASE_URL}/embeddings``。线上也在用这个服务，所以一次只发一个请求，
一批最多 16 条；限流、服务器出错、网络出错按 2、4、8 秒往上翻退避，一批最多试 4 次，还不行整批停下。
密钥（JAVMEM_EMBEDDING_API_KEY）只从环境变量读，不打印、不写文件，报错里也不带；服务的报错只留状态码和
type、code 这两个短标识，不抄 message（它可能抄着发过去的文字）。

- 向量存成 float32 小端，2560 个数一共 10240 字节（pack_vector、unpack_vector）。维度不对、数不是有限的，
  都算服务回得不对，整批停下：存进去的向量维度不会错。
- 文档（事实）不加指令前缀。Qwen3-Embedding 查询时要加 “Instruct: …\\nQuery: …”，那是检索（J5.4）的事。
- FakeEmbeddings 按文字的 sha256 给出固定的单位向量，测试和 CI 用，不连网。FakeEmbeddingServer 是起在本机
  随机端口上的假服务，测 HTTP 这一层用，测完就关。
"""

from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import math
import os
import random
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from javmem.config import ConfigError
from javmem.llm import _is_loopback

EMBEDDING_DIM = 2560
MAX_BATCH = 16
DEFAULT_MODEL = "Qwen/Qwen3-Embedding-4B"
DEFAULT_TIMEOUT = 60.0

ENV_BASE_URL = "JAVMEM_EMBEDDING_BASE_URL"
ENV_API_KEY = "JAVMEM_EMBEDDING_API_KEY"
ENV_MODEL = "JAVMEM_EMBEDDING_MODEL"
ENV_TIMEOUT = "JAVMEM_EMBEDDING_TIMEOUT_SECONDS"

# 出错的种类
CONFIG = "config"  # 地址、模型不对（404）
AUTH = "auth"  # 密钥不对（401、403）
BAD_REQUEST = "bad_request"  # 请求本身不对（400、413、422）
RATE_LIMIT = "rate_limit"
SERVER = "server"
TRANSPORT = "transport"
BAD_REPLY = "bad_reply"  # 回的不是 JSON、条数或者维度不对
FATAL = frozenset({CONFIG, AUTH, BAD_REQUEST, BAD_REPLY})


class EmbeddingError(Exception):
    """调不通。detail 里没有密钥和发过去的文字。"""

    def __init__(self, kind: str, detail: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after

    @property
    def fatal(self) -> bool:
        return self.kind in FATAL


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str
    model: str = DEFAULT_MODEL
    api_key: str = field(default="", repr=False)
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EmbeddingSettings:
        env = os.environ if env is None else env
        base_url = env.get(ENV_BASE_URL, "").strip()
        if not base_url:
            raise ConfigError(f"没有设置 {ENV_BASE_URL}")
        try:
            parts = urlsplit(base_url)
            hostname, _ = parts.hostname, parts.port
        except ValueError:
            raise ConfigError(f"{ENV_BASE_URL} 要写成 http://127.0.0.1:端口/v1 这样的地址") from None
        if parts.scheme not in ("https", "http") or not hostname:
            raise ConfigError(f"{ENV_BASE_URL} 要写成 http://127.0.0.1:端口/v1 这样的地址")
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
        timeout_text = env.get(ENV_TIMEOUT, "").strip()
        try:
            timeout = float(timeout_text) if timeout_text else DEFAULT_TIMEOUT
        except ValueError:
            raise ConfigError(f"{ENV_TIMEOUT} 要写成秒数") from None
        if not math.isfinite(timeout) or not 1 <= timeout <= 600:
            raise ConfigError(f"{ENV_TIMEOUT} 要在 1 到 600 秒之间")
        return cls(base_url=base_url.rstrip("/"), model=model, api_key=api_key, timeout=timeout)

    def describe(self) -> dict[str, object]:
        """不是密钥的设置，可以打印、写进报告。"""
        return {"base_url": self.base_url, "model": self.model, "timeout": self.timeout, "api_key_set": bool(self.api_key)}


def pack_vector(values: Sequence[float]) -> bytes:
    """2560 个有限的数（布尔值不算数），不能全是 0；超出 float32 的算回得不对。"""
    if len(values) != EMBEDDING_DIM or not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values
    ):
        raise EmbeddingError(BAD_REPLY, f"向量要是 {EMBEDDING_DIM} 个有限的数")
    if not any(values):
        raise EmbeddingError(BAD_REPLY, "向量全是 0")
    try:
        return struct.pack(f"<{EMBEDDING_DIM}f", *values)
    except (OverflowError, struct.error):
        raise EmbeddingError(BAD_REPLY, "向量里的数超出 float32") from None


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    if len(blob) != 4 * EMBEDDING_DIM:
        raise ValueError("向量的长度不对")
    return struct.unpack(f"<{EMBEDDING_DIM}f", blob)


@dataclass(frozen=True)
class EmbeddingReply:
    vectors: list[bytes]  # 和输入一样的顺序，每个都是 pack_vector 的
    tokens: int  # 服务说用了多少 token；没说按字数估
    seconds: float
    estimated: bool = False


class Embedder:
    """向量服务的样子：name 记进向量缓存的键和账本，embed 一次最多 MAX_BATCH 条。"""

    name: str

    def embed(self, texts: Sequence[str]) -> EmbeddingReply:  # pragma: no cover - 接口
        raise NotImplementedError


def _estimate_tokens(texts: Iterable[str]) -> int:
    return sum(max(1, len(text) // 2) for text in texts)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟着重定向走：跟过去会把密钥带到别的地址，“不是本机的要 https”也被绕过去了。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]  # noqa: ANN001
        return None


class OpenAIEmbeddings(Embedder):
    """真实的服务。不走代理：本机的服务，环境变量里的代理设置管不着它。也不跟重定向。"""

    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        self.name = settings.model
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def embed(self, texts: Sequence[str]) -> EmbeddingReply:
        if not 1 <= len(texts) <= MAX_BATCH:
            raise ValueError(f"一批要 1 到 {MAX_BATCH} 条")
        body = json.dumps({"model": self.settings.model, "input": list(texts)}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        request = urllib.request.Request(self.settings.base_url + "/embeddings", data=body, headers=headers, method="POST")
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.settings.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise _http_error(error) from None
        except (urllib.error.URLError, OSError, socket.timeout, http.client.HTTPException) as error:
            raise EmbeddingError(TRANSPORT, type(error).__name__) from None
        seconds = time.monotonic() - started
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise EmbeddingError(BAD_REPLY, "回的不是 JSON") from None
        vectors = _vectors_of(data, len(texts))
        usage = data.get("usage") if isinstance(data, dict) else None
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            return EmbeddingReply(vectors, tokens, seconds)
        return EmbeddingReply(vectors, _estimate_tokens(texts), seconds, estimated=True)


def _short(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", value) else ""


def _http_error(error: urllib.error.HTTPError) -> EmbeddingError:
    """只留状态码和 type、code 两个短标识。"""
    tags = []
    try:
        data = json.loads(error.read(4096).decode("utf-8"))
        detail = data.get("error") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            tags = [tag for tag in (_short(detail.get("type")), _short(detail.get("code"))) if tag]
    except Exception:  # noqa: BLE001 - 报错的正文读不了就算了
        pass
    text = " ".join([f"HTTP {error.code}", *tags])
    retry_after = None
    header = error.headers.get("Retry-After") if error.headers else None
    if header:
        try:
            retry_after = float(header)
        except ValueError:
            retry_after = None
    if 300 <= error.code < 400:
        return EmbeddingError(CONFIG, text)  # 重定向：不跟过去
    if error.code in (401, 403):
        return EmbeddingError(AUTH, text)
    if error.code == 404:
        return EmbeddingError(CONFIG, text)
    if error.code == 429:
        return EmbeddingError(RATE_LIMIT, text, retry_after=retry_after)
    if error.code >= 500:
        return EmbeddingError(SERVER, text, retry_after=retry_after)
    return EmbeddingError(BAD_REQUEST, text)


def _vectors_of(data: object, expected: int) -> list[bytes]:
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != expected:
        raise EmbeddingError(BAD_REPLY, "条数不对")
    ordered: dict[int, bytes] = {}
    for position, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise EmbeddingError(BAD_REPLY, "缺 embedding")
        index = item.get("index", position)
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < expected or index in ordered:
            raise EmbeddingError(BAD_REPLY, "index 不对")
        ordered[index] = pack_vector(item["embedding"])
    return [ordered[index] for index in range(expected)]


# ---------- 假的 ----------


def fake_vector(text: str) -> list[float]:
    """按文字给一个固定的单位向量：同样的文字每次一样，不同的文字几乎正交。"""
    rng = random.Random(int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big"))
    values = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class FakeEmbeddings(Embedder):
    """测试和 CI 用，不连网。script 按顺序让它出错（rate_limit、server、transport、auth、bad_reply），用完了正常回。
    bad_texts 里的文字，带着它的那一批总是回“请求不对”（模拟服务一直拒的那一条）。"""

    def __init__(self, script: Iterable[str] = (), *, name: str = "fake-embedding", bad_texts: Iterable[str] = ()) -> None:
        self.name = name
        self.script = list(script)
        self.bad_texts = set(bad_texts)
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> EmbeddingReply:
        if not 1 <= len(texts) <= MAX_BATCH:
            raise ValueError(f"一批要 1 到 {MAX_BATCH} 条")
        self.calls.append(list(texts))
        if self.bad_texts & set(texts):
            raise EmbeddingError(BAD_REQUEST, "假服务：这条拒")
        if self.script:
            kind = self.script.pop(0)
            if kind != "ok":
                raise EmbeddingError(kind, "假服务", retry_after=0.0 if kind == RATE_LIMIT else None)
        return EmbeddingReply([pack_vector(fake_vector(text)) for text in texts], _estimate_tokens(texts), 0.0)


class FakeEmbeddingServer:
    """起在 127.0.0.1 随机端口上的假向量服务，OpenAI 兼容的 /embeddings 和 /models。

    with FakeEmbeddingServer(api_key="k") as server: server.base_url ……。replies 按顺序给出每个请求的回法：
    一个状态码（429、500、401……）、"bad_json"、"short"（少回一条）、"wrong_dim"、"bad_status"（坏的状态行）、
    "short_body"（说好的长度没发完就断）、"huge"（超出 float32 的数）、"bool"、"zero"、"redirect"（302 到别处，
    别处收到的请求头记在 redirected），用完了正常回。同一时间有几个请求在跑记在 peak。
    """

    def __init__(self, *, api_key: str = "", replies: Iterable[int | str] = (), model: str = DEFAULT_MODEL) -> None:
        self.api_key = api_key
        self.replies: list[int | str] = list(replies)
        self.model = model
        self.requests: list[dict[str, object]] = []
        self.redirected: list[dict[str, str]] = []
        self.peak = 0
        self._busy = 0
        self._lock = threading.Lock()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> FakeEmbeddingServer:
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # 不往 stderr 打
                pass

            def _send(self, status: int, payload: object, headers: Mapping[str, str] | None = None) -> None:
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/").endswith("/elsewhere"):
                    owner.redirected.append({"authorization": self.headers.get("Authorization", "")})
                    self._send(200, {"object": "list", "data": []})
                    return
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list", "data": [{"id": owner.model, "object": "model"}]})
                else:
                    self._send(404, {"error": {"type": "not_found"}})

            def do_POST(self) -> None:  # noqa: N802
                with owner._lock:
                    owner._busy += 1
                    owner.peak = max(owner.peak, owner._busy)
                try:
                    self._handle()
                finally:
                    with owner._lock:
                        owner._busy -= 1

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                if self.path.rstrip("/").endswith("/elsewhere"):
                    owner.redirected.append({"authorization": self.headers.get("Authorization", "")})
                    self._send(200, {"object": "list", "data": []})
                    return
                if owner.api_key and self.headers.get("Authorization") != f"Bearer {owner.api_key}":
                    self._send(401, {"error": {"type": "invalid_api_key", "message": "密钥不对"}})
                    return
                if not self.path.rstrip("/").endswith("/embeddings"):
                    self._send(404, {"error": {"type": "not_found"}})
                    return
                request = json.loads(raw.decode("utf-8"))
                owner.requests.append(request)
                texts = request.get("input")
                if isinstance(texts, str):
                    texts = [texts]
                with owner._lock:
                    reply = owner.replies.pop(0) if owner.replies else "ok"
                if isinstance(reply, int):
                    self._send(reply, {"error": {"type": "fake", "code": str(reply), "message": "原文" + "".join(texts)}},
                               {"Retry-After": "0"} if reply == 429 else None)
                    return
                if reply == "bad_json":
                    self._send(200, b"{not json")
                    return
                if reply == "bad_status":
                    self.wfile.write(b"HTTP/1.1 abc\r\n\r\n")
                    self.close_connection = True
                    return
                if reply == "short_body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    self.wfile.write(b'{"data": [')
                    self.close_connection = True
                    return
                if reply == "redirect":
                    self.send_response(302)
                    self.send_header("Location", self.path.rsplit("/", 1)[0] + "/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                vectors = [fake_vector(text) for text in texts]
                if reply == "huge":
                    vectors = [[1e39] * EMBEDDING_DIM for _ in texts]
                if reply == "bool":
                    vectors = [[True] * EMBEDDING_DIM for _ in texts]
                if reply == "zero":
                    vectors = [[0.0] * EMBEDDING_DIM for _ in texts]
                if reply == "short":
                    vectors = vectors[:-1]
                if reply == "wrong_dim":
                    vectors = [vector[:-1] for vector in vectors]
                data = [{"object": "embedding", "index": index, "embedding": vector} for index, vector in enumerate(vectors)]
                tokens = _estimate_tokens(texts)
                self._send(200, {"object": "list", "data": data, "model": owner.model,
                                 "usage": {"prompt_tokens": tokens, "total_tokens": tokens}})

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------- 一批一批地调 ----------


@dataclass
class EmbedStats:
    calls: int = 0
    texts: int = 0
    tokens: int = 0
    retries: int = 0
    seconds: float = 0.0
    skipped: int = 0  # 服务一直拒（请求不对）、跳过的文字


RecordCall = Callable[[str, int, str, int, float, bool], None]  # (批的指纹, 第几次, 结果, token, 秒, 估的)


PROBE_TEXT = "ok"  # 一批拆开以后每一条都被拒时，拿它探一下服务是不是配错了
PROBE_DIGEST = hashlib.sha256(PROBE_TEXT.encode("utf-8")).hexdigest()


def embed_all(
    texts: Sequence[str],
    embedder: Embedder,
    *,
    record: RecordCall | None = None,
    attempts: int = 4,
    backoff: float = 2.0,
    cap: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    stats: EmbedStats | None = None,
    store: Callable[[dict[str, bytes]], None] | None = None,
) -> dict[str, bytes]:
    """把 texts 一批最多 MAX_BATCH 条、一次一个请求地算成向量。

    - 限流、服务器、网络出错退避重试，一批试满 attempts 次还不行抛 EmbeddingError；密钥、地址、回得不对马上抛。
    - 一批回“请求不对”的，拆成一条一条再试；一条自己就被拒的（太长、内容不行），跳过这一条，别的照算。
      拆开的那几条一条都没过，拿一句短话探一下：也被拒，就是服务或者模型配错了，抛 EmbeddingError，不再往下发；
      探的过了，就是这几条自己的毛病，跳过。
    - 每批算完就交给 store（存进缓存），后面哪一批出错，前面算好的不白算。
    - 每次调用都 record。
    """
    stats = stats if stats is not None else EmbedStats()
    unique = list(dict.fromkeys(texts))
    found: dict[str, bytes] = {}
    pending: list[tuple[list[str], int | None]] = [
        (unique[start : start + MAX_BATCH], None) for start in range(0, len(unique), MAX_BATCH)
    ]
    split: dict[int, list[int]] = {}  # 拆开的一批 → [还没试的条数, 过了的条数]

    def settle(group: int | None, passed: bool) -> None:
        if group is None:
            return
        split[group][0] -= 1
        split[group][1] += int(passed)
        if split[group] == [0, 0]:  # 拆开的每一条都被拒：拿一句短话探一下，也被拒就是服务或者模型配错了
            try:
                embedder.embed([PROBE_TEXT])
            except EmbeddingError as error:
                stats.calls += 1
                if record is not None:
                    record(PROBE_DIGEST, 1, error.kind, 0, 0.0, False)
                if error.kind == BAD_REQUEST:
                    raise EmbeddingError(BAD_REQUEST, "连一句短话也被拒了，多半是服务或者模型配错了") from None
                raise
            stats.calls += 1
            if record is not None:
                record(PROBE_DIGEST, 1, "ok_estimated", 0, 0.0, True)

    while pending:
        batch, group = pending.pop(0)
        digest = hashlib.sha256("\n".join(batch).encode("utf-8")).hexdigest()
        for attempt in range(1, attempts + 1):
            try:
                reply = embedder.embed(batch)
            except EmbeddingError as error:
                if record is not None:
                    record(digest, attempt, error.kind, 0, 0.0, False)
                stats.calls += 1
                if error.kind == BAD_REQUEST:
                    if len(batch) > 1:
                        split[len(split)] = [len(batch), 0]
                        pending[:0] = [([text], len(split) - 1) for text in batch]  # 拆开一条一条再试
                    else:
                        stats.skipped += 1
                        settle(group, passed=False)
                    break
                if error.fatal or attempt == attempts:
                    raise
                stats.retries += 1
                wait = min(backoff * 2 ** (attempt - 1), cap)
                if error.retry_after is not None and math.isfinite(error.retry_after):
                    wait = max(wait, min(error.retry_after, cap))
                sleep(wait)
                continue
            if record is not None:
                record(digest, attempt, "ok_estimated" if reply.estimated else "ok", reply.tokens, reply.seconds, reply.estimated)
            stats.calls += 1
            stats.texts += len(batch)
            stats.tokens += reply.tokens
            stats.seconds += reply.seconds
            done = dict(zip(batch, reply.vectors, strict=True))
            found.update(done)
            if store is not None:
                store(done)
            settle(group, passed=True)
            break
    return found
