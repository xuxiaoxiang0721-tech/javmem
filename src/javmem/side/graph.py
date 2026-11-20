"""接 Graphiti：只有这个模块 import graphiti_core（可选依赖 `[side]`）。

**import 之前**（prepare_import）：Graphiti 的上报统计（posthog）关掉（GRAPHITI_TELEMETRY_ENABLED=false，关了它连 posthog
都不 import、不写编号文件）；它 import 时会去找 .env 读进环境变量，也关掉（python-dotenv 的开关，加上把 load_dotenv 换成
什么都不做的）；它 import 时读的两个设置先定好：并发（SEMAPHORE_LIMIT）、向量维度（EMBEDDING_DIM，2560）。

**三个客户端都是我们自己的**（不给的话它自己建连 OpenAI 的）：
- QwenClient：只发给本机 Qwen，关思考、temperature 0、JSON Schema 输出，不走代理、没有密钥。按 Graphiti 那一步要的结构
  自己核一遍，错了带着错在哪重问，最多两次；还不对抛 SideFormatError（Graphiti 不重试它），这个窗口算抽不成。
  同时最多几路在这里卡（Graphiti 自己的并发上限一层套一层时会超）。每次调用记进 token 账（标签、Graphiti 的哪一步、
  格式错、重问也记）和支线的库。
- SideEmbedder：javmem 现成的向量客户端（本机，2560 维），一次一个请求、一批不超过 16 条。
- QuietReranker：查的时候才用的重排，不连网（抽的时候用不到）。

**体检库**：先用我们自己的连接核标记（防串库），核过了才建 Graphiti 的驱动（它一建就去建索引）。按组删用自己写的语句
（不用 Graphiti 的 clear_data：组名传成空的就把整个库清掉；也不用 remove_episode：删完还要调模型重写摘要）。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from javmem.embedding import EMBEDDING_DIM, MAX_BATCH, EmbeddingError, OpenAIEmbeddings, unpack_vector
from javmem.pipeline.relcheck import ALLOWED
from javmem.side.config import SideSettings
from javmem.side.episodes import Episode
from javmem.side.store import SideStore, now_text
from javmem.usage import TokenLedger

GRAPHITI_VERSION = "0.31.2"
MARKER_LABEL = "JavmemSideMarker"
DATABASE = "neo4j"
PREVIOUS_EPISODES = 3  # 同一个包的前几个窗口当上文
MAX_FIXES = 2  # 格式错了最多重问几次
RETRY_DELAY = 10.0  # 本机 Qwen 连不上、服务出错，等几秒再试一次
SOURCE_DESCRIPTION = "javmem 的一个窗口：Bot 和主人的对话原文"
DEFAULT_PASSWORD = "neo4j"  # Neo4j 第一次起来的默认密码，要先改
_GROUP_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")

# 中文的类型名 → Graphiti 里的名字（它只认英文的标识符）。导出时换回中文
ENTITY_LABELS = {"人": "Person", "公司或机构": "Organization", "项目": "Project", "订单或合同": "Contract", "地点": "Location",
                 "资产": "Asset", "Bot": "Bot"}
RELATION_LABELS = {"持有": "HOLDS", "托管于": "CUSTODIED_AT", "负责": "RESPONSIBLE_FOR", "任职于": "WORKS_AT",
                   "客户是": "CLIENT_OF", "亲属": "KIN_OF", "隶属于": "PART_OF"}  # 隶属于：D210
LABEL_ENTITY = {label: name for name, label in ENTITY_LABELS.items()}
LABEL_RELATION = {label: name for name, label in RELATION_LABELS.items()}

ENTITY_DOCS = {
    "Person": "人：有名字或者固定叫法的具体的人。记忆的主人（原文里的“我”）也是人，名字写“主人”。比如：小周、王经理。"
              "不算：一类人（客户们、同事们）。",
    "Organization": "公司或机构：公司、银行、券商、基金公司、律所、学校、政府部门，也包括一家的部门、分公司、子公司。"
                    "比如：东岸证券、海港银行、东岸证券 财富管理部。"
                    "上市股票的发行公司是公司或机构，股票本身是资产。",
    "Project": "项目：一件持续在做的事、一类活。比如：新店装修、年底对账、给某家做的网站。",
    "Contract": "订单或合同：一张单子、一份合同、一笔交易。FCN、累购这类单子一张一个，原文怎么叫这一张就怎么写，比如“X1 FCN”“X3 累购”。",
    "Location": "地点：城市、地址、具体的地方。",
    "Asset": "资产：上市的股票、ADR、债、基金、定存。比如：X2 股票、某某债券基金。",
    "Bot": "Bot：AI 助手、机器人、自动程序，包括这段对话里的 Bot。",
}
RELATION_DOCS = {
    "HOLDS": "持有：主体现在拥有客体——股票、ADR、债、基金、定存，FCN、累购这类单子（一张一个），没上市公司的股份。只算已经成交的。"
             "方向：持有人 → 东西。不算：打算买、在观察、Bot 建议买卖、还没成交的指令；FCN、累购挂钩的股票；贷款、房产、收藏品。",
    "CUSTODIED_AT": "托管于：资产或单子 → 放它的那家银行、券商。在某家、某家的账户里、结单周报列在某家名下，都算。"
                    "不算：发行方（谁发的这张票据，不等于放在哪家）。",
    "RESPONSIBLE_FOR": "负责：人或 Bot → 他管的一件事、一类活、一张单子、一个客户、一家机构。客户经理、对接人 → 他负责的客户"
                       "（客户是主人的，客体写主人）。主人把一类活长期交给某个 Bot，也算。不算：一次性的派活、“跟进一下”。",
    "WORKS_AT": "任职于：人 → 他工作的公司或机构（签名里的公司、某家的某人、入职、接任）；说了部门的连那个部门（部门也是"
                "公司或机构，部门归哪家是隶属于）。不算：光说“同事”、还没定的人选。",
    "CLIENT_OF": "客户是：客户 → 卖方或服务方，读作“主体是客体的客户”。开户、入金、有账户、签了合同、付了钱、下了单都算。"
                 "不算：还在谈、报过价；外面两家公司之间谁是谁的客户。",
    "KIN_OF": "亲属：人 → 人，两头都要是具体的人。kinship 写主体是客体的什么人（妻子、哥哥、舅舅）。不算：朋友、同学、家教、助理。",
    "PART_OF": "隶属于：部门、分公司、子公司 → 它所属的那家公司或机构（某家的某某部、名字里写着是谁的分公司、某家旗下、"
               "签名里部门和公司写在一起）。同一时间只归一家。人不连这一条：人在哪个部门是任职于（WORKS_AT）。"
               "不算：免责声明里提到的子公司、关联公司；只说股权的（那是持有）；产品归哪家。",
}
INSTRUCTIONS = """这是一段 Bot 和记忆的主人的对话原文（方括号里是这句话说的时间，北京时间）。
- “我”是记忆的主人：当实体时名字写“主人”，不写“我”。
- 人、公司、股票、单子的名字照原文写，不翻译、不改写。fact 用原文的语言写（原文是中文就写中文）。
- 只记已经成立的事。打算、建议、猜测、Bot 的建议不算关系。
- 关系能归进 FACT_TYPES 的七种的，用那七种的名字和方向。"""
LANGUAGE_NOTE = (
    "\n\nWrite every extracted name and fact in the same language as the source text (Chinese stays Chinese). "
    "Keep names exactly as written in the source; never translate them."
)

_CURRENT = contextvars.ContextVar("javmem_side_current", default=("", ""))  # (组, 窗口)
_PROMPT = contextvars.ContextVar("javmem_side_prompt", default="unknown")
# 这个窗口里我们的客户端抛过的错（格式错、连不上）。Graphiti 有一步把出错都吞了（补日子那一步，except Exception）：
# 那一步之后照这里再抛出来；整个 add_episode 回来以后也查一遍，吞了的一律不算跑成。列表是同一个，子任务里加的这里看得到
_RAISED: contextvars.ContextVar[list[Exception] | None] = contextvars.ContextVar("javmem_side_raised", default=None)


@contextlib.contextmanager
def window_scope(group_id: str, window_id: str) -> Iterator[list[Exception]]:
    """跑一个窗口：记账记到哪个组、哪个窗口，收这个窗口里抛过的错。"""
    raised: list[Exception] = []
    current, collected = _CURRENT.set((group_id, window_id)), _RAISED.set(raised)
    try:
        yield raised
    finally:
        _RAISED.reset(collected)
        _CURRENT.reset(current)


def _noted(error: Exception) -> Exception:
    raised = _RAISED.get()
    if raised is not None:
        raised.append(error)
    return error


def _worst(raised: Sequence[Exception]) -> Exception:
    """一个窗口里抛过几个错（几路同时问）：连不上的先算（整批停），没有才是格式错（这个窗口抽不成）。"""
    return next((error for error in reversed(raised) if isinstance(error, SideTransportError)), raised[-1])


class SideGraphError(Exception):
    """体检库、Graphiti 用不了：没装、标记对不上、配错了。消息里不带原文、不带密码。"""


class SideFormatError(Exception):
    """Qwen 重问以后格式还是不对：这个窗口抽不成。"""

    def __init__(self, prompt: str, detail: str) -> None:
        super().__init__(f"{prompt}：{detail}")
        self.prompt, self.detail = prompt, detail


class SideTransportError(Exception):
    """本机 Qwen、向量服务连不上、出错：停下，不往下跑。"""


# ---------- import 之前 ----------


def prepare_import(lanes: int) -> None:
    """在 import graphiti_core 之前调。已经 import 过、又没关上报的，报错（关不了了）。"""
    if "graphiti_core" in sys.modules and os.environ.get("GRAPHITI_TELEMETRY_ENABLED") != "false":
        raise SideGraphError("Graphiti 在关上报统计之前就 import 了")
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["SEMAPHORE_LIMIT"] = str(lanes)
    os.environ["EMBEDDING_DIM"] = str(EMBEDDING_DIM)
    try:
        import dotenv
        import dotenv.main
    except ImportError:
        return

    def _no_dotenv(*args: object, **kwargs: object) -> bool:
        return False

    dotenv.load_dotenv = _no_dotenv  # type: ignore[assignment]
    dotenv.main.load_dotenv = _no_dotenv  # type: ignore[assignment]


def _language_instruction(group_id: str | None = None) -> str:
    return LANGUAGE_NOTE


@dataclass
class _Kit:
    """import 进来的 Graphiti 的几样（只 import 一次）。"""

    Graphiti: Any
    Neo4jDriver: Any
    EpisodeType: Any
    LLMClient: Any
    LLMConfig: Any
    ModelSize: Any
    EmbedderClient: Any
    CrossEncoderClient: Any
    BaseModel: Any
    ValidationError: Any
    entity_types: dict[str, Any]
    edge_types: dict[str, Any]
    edge_type_map: dict[tuple[str, str], list[str]]
    telemetry: Any


_KIT: _Kit | None = None


def kit(lanes: int = 2) -> _Kit:
    global _KIT
    if _KIT is not None:
        return _KIT
    prepare_import(lanes)
    try:
        import graphiti_core
        import graphiti_core.llm_client.client as client_module
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.client import CrossEncoderClient
        from graphiti_core.driver.neo4j_driver import Neo4jDriver
        from graphiti_core.embedder.client import EmbedderClient
        from graphiti_core.llm_client import LLMClient
        from graphiti_core.llm_client.config import LLMConfig, ModelSize
        from graphiti_core.nodes import EpisodeType
        from graphiti_core.telemetry import telemetry
        from pydantic import BaseModel, Field, ValidationError, create_model
    except ImportError as error:
        raise SideGraphError(f"没装 Graphiti（pip install -e .[side]）：{type(error).__name__}") from None
    version = getattr(graphiti_core, "__version__", None)
    if version is None:
        import importlib.metadata

        version = importlib.metadata.version("graphiti-core")
    if version != GRAPHITI_VERSION:
        raise SideGraphError(f"装的 graphiti-core 是 {version}，支线照 {GRAPHITI_VERSION} 写的")
    if telemetry.is_telemetry_enabled():
        raise SideGraphError("Graphiti 的上报统计没关掉")
    client_module.get_extraction_language_instruction = _language_instruction  # 名字、事实照原文的语言写
    from graphiti_core.utils.maintenance import edge_operations

    swallowing = edge_operations._extract_edge_timestamps  # 补日子：它把出错都吞了

    async def timestamps_without_swallowing(*args: Any, **kwargs: Any) -> None:
        raised = _RAISED.get()
        before = len(raised) if raised is not None else 0
        await swallowing(*args, **kwargs)
        if raised is not None and len(raised) > before:
            raise _worst(raised)

    edge_operations._extract_edge_timestamps = timestamps_without_swallowing

    entity_types = {label: create_model(label, __base__=BaseModel, __doc__=ENTITY_DOCS[label]) for label in ENTITY_LABELS.values()}
    edge_types: dict[str, Any] = {}
    for label in RELATION_LABELS.values():
        if label == "KIN_OF":
            edge_types[label] = create_model(
                label, __base__=BaseModel, __doc__=RELATION_DOCS[label],
                kinship=(str | None, Field(None, description="主体是客体的什么人，比如 妻子、哥哥、舅舅；原文没说写 null")),
            )
        else:
            edge_types[label] = create_model(label, __base__=BaseModel, __doc__=RELATION_DOCS[label])
    edge_type_map: dict[tuple[str, str], list[str]] = {}
    for relation, pairs in ALLOWED.items():
        for subject, obj in sorted(pairs):
            edge_type_map.setdefault((ENTITY_LABELS[subject], ENTITY_LABELS[obj]), []).append(RELATION_LABELS[relation])
    _KIT = _Kit(Graphiti, Neo4jDriver, EpisodeType, LLMClient, LLMConfig, ModelSize, EmbedderClient, CrossEncoderClient,
                BaseModel, ValidationError, entity_types, edge_types, edge_type_map, telemetry)
    return _KIT


# ---------- 记账 ----------


@dataclass
class Recorder:
    """每次调用记进 token 账（不算进上限的本机模型照样记）和支线的库。不记内容。"""

    store: SideStore | None
    ledger: TokenLedger | None
    tag: str
    model: str

    def record(self, *, purpose: str, model: str | None = None, attempt: int, status: str, input_tokens: int = 0,
               output_tokens: int = 0, cached_tokens: int = 0, seconds: float = 0.0, key: str = "") -> None:
        group_id, window_id = _CURRENT.get()
        if self.ledger is not None:
            self.ledger.append({
                "at": now_text(), "tag": self.tag, "purpose": purpose, "model": model or self.model, "window_id": window_id,
                "cache_key": key, "attempt": attempt, "status": status, "input_tokens": input_tokens,
                "output_tokens": output_tokens, "reasoning_tokens": 0, "cached_tokens": cached_tokens,
                "seconds": round(seconds, 3),
            })
        if self.store is not None and window_id:
            self.store.record_call(group_id=group_id, window_id=window_id, prompt=purpose, attempt=attempt, status=status,
                                   input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens,
                                   seconds=seconds)


# ---------- 客户端 ----------


_TRANSIENT = (urllib.error.URLError, OSError, socket.timeout, http.client.HTTPException)


def _short_error(error: Exception) -> str:
    """校验错只留位置和种类，不留值（值里可能有原话）。"""
    errors = getattr(error, "errors", None)
    if callable(errors):
        try:
            items = errors()
            return "；".join(f"{'.'.join(str(part) for part in item.get('loc', ()))} {item.get('type')}" for item in items[:5])
        except Exception:  # noqa: BLE001
            pass
    if isinstance(error, json.JSONDecodeError):
        return f"不是完整的 JSON（第 {error.pos} 个字符附近）"
    return type(error).__name__


def _check_reply(text: str, finish: str, response_model: Any, validation_error: type[Exception]) -> str:
    """回的对不对：对的返回空字符串，不对的返回错在哪（只有位置和种类，不带值）。"""
    if finish == "length":
        return "回答太长被截断了"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        return _short_error(error)
    if not isinstance(data, dict):
        return "要回一个 JSON 对象"
    if response_model is not None:
        try:
            response_model.model_validate(data)
        except validation_error as error:
            return _short_error(error) or "结构不对"
    return ""


def build_qwen_client(settings: SideSettings, recorder: Recorder, *, timeout: float = 300.0) -> Any:
    k = kit(settings.lanes)

    class QwenClient(k.LLMClient):  # type: ignore[misc, name-defined]
        """本机 Qwen（llama.cpp 的 OpenAI 兼容接口）。"""

        def __init__(self) -> None:
            config = k.LLMConfig(model=settings.qwen_model, small_model=settings.qwen_model, temperature=0, max_tokens=16384)
            super().__init__(config, cache=False)
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            self._semaphore: asyncio.Semaphore | None = None

        def _lanes(self) -> asyncio.Semaphore:
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(settings.lanes)
            return self._semaphore

        async def generate_response(self, messages: Any, response_model: Any = None, max_tokens: int | None = None,
                                    model_size: Any = None, group_id: str | None = None, prompt_name: str | None = None,
                                    *, attribute_extraction: bool = False, model: str | None = None) -> dict[str, Any]:
            token = _PROMPT.set(prompt_name or "unknown")
            try:
                return await super().generate_response(
                    messages, response_model, max_tokens, model_size or k.ModelSize.medium, group_id, prompt_name,
                    attribute_extraction=attribute_extraction, model=model,
                )
            finally:
                _PROMPT.reset(token)

        def _post(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(settings.qwen_url, data=data, headers={"Content-Type": "application/json"})
            started = time.monotonic()
            with self._opener.open(request, timeout=timeout) as response:
                out = json.loads(response.read().decode("utf-8"))
            return out, time.monotonic() - started

        async def _ask(self, body: dict[str, Any], prompt: str, attempt: int, key: str) -> tuple[str, str]:
            """问一次（连不上、服务出错的等 10 秒再试一次）。返回 (回的文字, 为什么停)。"""
            for tries in (1, 2):
                async with self._lanes():
                    try:
                        out, seconds = await asyncio.to_thread(self._post, body)
                    except urllib.error.HTTPError as error:
                        status = f"http_{error.code}"
                        recorder.record(purpose=f"side:{prompt}", attempt=attempt, status=status, key=key)
                        if error.code == 400:
                            raise SideFormatError(prompt, "Qwen 不收这个请求（400）") from None
                        if tries == 2 or error.code < 500:
                            raise SideTransportError(f"本机 Qwen 出错：{status}") from None
                        retry = True
                    except (*_TRANSIENT, ValueError) as error:
                        recorder.record(purpose=f"side:{prompt}", attempt=attempt, status="transport_error", key=key)
                        if tries == 2:
                            raise SideTransportError(f"本机 Qwen 连不上：{type(error).__name__}") from None
                        retry = True
                    else:
                        retry = False
                if retry:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                usage = out.get("usage") or {}
                details = usage.get("prompt_tokens_details") or {}
                choice = (out.get("choices") or [{}])[0]
                text = ((choice.get("message") or {}).get("content")) or ""
                recorder.record(
                    purpose=f"side:{prompt}", attempt=attempt, status="ok", key=key,
                    input_tokens=int(usage.get("prompt_tokens") or 0), output_tokens=int(usage.get("completion_tokens") or 0),
                    cached_tokens=int(details.get("cached_tokens") or 0) if isinstance(details, Mapping) else 0,
                    seconds=seconds,
                )
                return text, str(choice.get("finish_reason") or "")
            raise SideTransportError("本机 Qwen 连不上")  # pragma: no cover - 上面两次之内一定返回或抛

        async def _generate_response(self, messages: Any, response_model: Any = None, max_tokens: int = 16384,
                                     model_size: Any = None, *, model: str | None = None) -> dict[str, Any]:
            try:
                return await self._answer(messages, response_model, max_tokens)
            except (SideFormatError, SideTransportError) as error:
                raise _noted(error)

        async def _answer(self, messages: Any, response_model: Any, max_tokens: int) -> dict[str, Any]:
            prompt = _PROMPT.get()
            chat = [{"role": message.role, "content": message.content} for message in messages]
            body: dict[str, Any] = {
                "model": settings.qwen_model, "temperature": 0, "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False}, "messages": chat,
            }
            if response_model is not None:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": re.sub(r"[^A-Za-z0-9_-]", "_", response_model.__name__)[:64], "strict": True,
                    "schema": response_model.model_json_schema()}}
            key = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            detail = ""
            for attempt in range(1, MAX_FIXES + 2):
                text, finish = await self._ask(body, prompt, attempt, key)
                detail = _check_reply(text, finish, response_model, k.ValidationError)
                if not detail:
                    return json.loads(text)
                recorder.record(purpose=f"side:{prompt}", attempt=attempt, status="format_error", key=key)
                if attempt > MAX_FIXES:
                    break
                body = dict(body)
                body["messages"] = [*chat, {"role": "assistant", "content": text[:2000]},
                                    {"role": "user", "content": f"上面的回答格式不对：{detail}。照要求的 JSON 结构重新回答，只回 JSON。"}]
            raise SideFormatError(prompt, detail)

    return QwenClient()


def build_embedder(embeddings: OpenAIEmbeddings, recorder: Recorder) -> Any:
    k = kit()

    class SideEmbedder(k.EmbedderClient):  # type: ignore[misc, name-defined]
        """本机的向量服务：一次一个请求、一批最多 16 条；限流、出错按 2、4、8 秒退避，一批最多试 4 次。"""

        def __init__(self) -> None:
            self._lock: asyncio.Lock | None = None

        def _guard(self) -> asyncio.Lock:
            if self._lock is None:
                self._lock = asyncio.Lock()
            return self._lock

        async def _embed(self, texts: list[str]) -> list[list[float]]:
            texts = [text if text.strip() else "（空）" for text in texts]
            delay = 2.0
            for attempt in range(1, 5):
                async with self._guard():
                    try:
                        reply = await asyncio.to_thread(embeddings.embed, texts)
                    except EmbeddingError as error:
                        recorder.record(purpose="side_embed", model=embeddings.name, attempt=attempt, status=error.kind)
                        if error.fatal or attempt == 4:
                            raise SideTransportError(f"向量服务出错：{error.kind}") from None
                    else:
                        recorder.record(purpose="side_embed", model=embeddings.name, attempt=attempt, status="ok",
                                        input_tokens=reply.tokens, seconds=reply.seconds)
                        return [list(unpack_vector(blob)) for blob in reply.vectors]
                await asyncio.sleep(delay)
                delay *= 2
            raise SideTransportError("向量服务出错")  # pragma: no cover

        async def create(self, input_data: Any) -> list[float]:
            if isinstance(input_data, str):
                texts = [input_data]
            elif isinstance(input_data, list) and input_data and all(isinstance(one, str) for one in input_data):
                texts = [input_data[0]]  # 和 Graphiti 自带的一样：给一串的只要第一条的向量
            else:
                raise SideGraphError("向量只给文字")
            return (await self._embed(texts))[0]

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            out: list[list[float]] = []
            for start in range(0, len(input_data_list), MAX_BATCH):
                out.extend(await self._embed(list(input_data_list[start:start + MAX_BATCH])))
            return out

    return SideEmbedder()


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"\s+", "", text or "").lower()
    return {clean[i:i + 2] for i in range(len(clean) - 1)} or ({clean} if clean else set())


def build_reranker() -> Any:
    k = kit()

    class QuietReranker(k.CrossEncoderClient):  # type: ignore[misc, name-defined]
        """不连网的重排：按两个字一组的重合给分。只在查的时候用，抽的时候用不到。"""

        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            wanted = _bigrams(query)
            scored = [(passage, len(wanted & _bigrams(passage)) / len(wanted) if wanted else 0.0) for passage in passages]
            return sorted(scored, key=lambda item: -item[1])

    return QuietReranker()


# ---------- 体检库 ----------


def check_group_id(group_id: str) -> str:
    if not isinstance(group_id, str) or not _GROUP_ID.match(group_id):
        raise SideGraphError("组名不对：不能是空的，只能有字母、数字、- 和 _")
    return group_id


async def set_initial_password(settings: SideSettings) -> bool:
    """体检库第一次起来是默认账号、要先改密码：用 bolt 连上，改成环境变量里的那个（当查询参数传，不拼进语句）。
    改了返回 True；默认密码已经连不上（改过了）返回 False。"""
    from neo4j import AsyncGraphDatabase
    from neo4j.exceptions import AuthError

    new = settings.require_password()
    try:
        async with AsyncGraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, DEFAULT_PASSWORD)) as driver:
            await driver.execute_query("ALTER CURRENT USER SET PASSWORD FROM $old TO $new", old=DEFAULT_PASSWORD, new=new,
                                       database_="system")
    except AuthError:
        return False
    async with AsyncGraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, DEFAULT_PASSWORD)) as driver:
        try:
            await driver.execute_query("RETURN 1", database_=DATABASE)
        except AuthError:
            return True
    raise SideGraphError("改了密码以后默认密码还连得上")


EXPECTED_CONFIG = {
    "dbms.usage_report.enabled": "false",
    "client.allow_telemetry": "false",
    "db.index.fulltext.default_analyzer": "cjk",
}


class SideGraph:
    """体检库的连接。open() 核过标记才能写；graphiti() 第一次用时才建 Graphiti。"""

    def __init__(self, settings: SideSettings, store: SideStore, *, recorder: Recorder | None = None,
                 embeddings: OpenAIEmbeddings | None = None, timeouts: float | None = None) -> None:
        """timeouts：连不上、不回话、事务重试最多等几秒（撤回接上支线时给短的，不让撤回卡住；不给照驱动的默认）。"""
        self.settings = settings
        self.store = store
        self.recorder = recorder
        self.embeddings = embeddings
        self.timeouts = timeouts
        self.driver: Any = None
        self._graphiti: Any = None

    async def connect(self) -> None:
        """只连上，不核标记（init 先核设置再放标记）。别的命令用 open()。"""
        from neo4j import AsyncGraphDatabase

        config: dict[str, float] = {}
        if self.timeouts is not None:
            config = {"connection_timeout": self.timeouts, "connection_acquisition_timeout": self.timeouts,
                      "max_transaction_retry_time": self.timeouts}
        self.driver = AsyncGraphDatabase.driver(self.settings.neo4j_uri,
                                                auth=(self.settings.neo4j_user, self.settings.require_password()), **config)
        await self.driver.verify_connectivity()

    async def open(self, *, create_marker: bool = False) -> None:
        await self.connect()
        await self.check_marker(create_marker)

    async def check_marker(self, create: bool = False) -> None:
        await self._check_marker(create)

    async def is_empty_without_marker(self) -> bool:
        """体检库是空的、也没有标记（整个重建过）：reset 才清支线的库。"""
        markers = (await self.query(f"MATCH (m:{MARKER_LABEL}) RETURN count(m) AS n"))[0]["n"]
        others = (await self.query(f"MATCH (n) WHERE NOT n:{MARKER_LABEL} RETURN count(n) AS n"))[0]["n"]
        return markers == 0 and others == 0

    async def close(self) -> None:
        if self._graphiti is not None:
            await self._graphiti.close()
            self._graphiti = None
        if self.driver is not None:
            await self.driver.close()
            self.driver = None

    async def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        result = await self.driver.execute_query(cypher, parameters_=params, database_=DATABASE)
        return [record.data() for record in result.records]

    async def _check_marker(self, create: bool) -> None:
        markers = await self.query(f"MATCH (m:{MARKER_LABEL}) RETURN m.marker_id AS id, m.library AS library")
        others = (await self.query(f"MATCH (n) WHERE NOT n:{MARKER_LABEL} RETURN count(n) AS n"))[0]["n"]
        mine = self.store.meta("marker_id")
        if len(markers) > 1:
            raise SideGraphError("体检库里有好几个支线的标记：一个字不写")
        if not markers:
            if others:
                raise SideGraphError("体检库不是空的，又没有支线的标记：一个字不写（是不是配到了别的 Neo4j？）")
            if mine:
                raise SideGraphError("体检库是空的，可支线的库记着上一个体检库：要重建先 javmem side reset")
            if not create:
                raise SideGraphError("体检库还没初始化：先跑 javmem side init")
            marker_id = secrets.token_hex(16)
            await self.query(f"CREATE (m:{MARKER_LABEL} {{marker_id: $id, library: $library, kind: 'javmem-side', "
                             "created_at: $at})", id=marker_id, library=self.store.library, at=now_text())
            self.store.set_meta("marker_id", marker_id)
            return
        marker = markers[0]
        if mine != marker["id"] or marker["library"] != self.store.library:
            raise SideGraphError("体检库的标记和支线的库对不上：一个字不写（一个体检库只配一个数据目录的一个库）")

    async def config(self) -> dict[str, str]:
        rows = await self.driver.execute_query("CALL dbms.listConfig() YIELD name, value RETURN name, value",
                                               database_="system")
        return {record["name"]: str(record["value"]) for record in rows.records}

    async def graphiti(self) -> Any:
        if self._graphiti is not None:
            return self._graphiti
        if self.recorder is None or self.embeddings is None:
            raise SideGraphError("没给模型和向量的客户端")
        k = kit(self.settings.lanes)
        driver = k.Neo4jDriver(self.settings.neo4j_uri, self.settings.neo4j_user, self.settings.require_password())
        task = getattr(driver, "_init_task", None)  # 在事件循环里建的驱动会自己去建索引
        if task is not None:
            await task
        else:
            await driver.build_indices_and_constraints()
        self._graphiti = k.Graphiti(
            graph_driver=driver, llm_client=build_qwen_client(self.settings, self.recorder),
            embedder=build_embedder(self.embeddings, self.recorder), cross_encoder=build_reranker(),
            store_raw_episode_content=True, max_coroutines=self.settings.lanes,
        )
        if k.telemetry.is_telemetry_enabled():
            raise SideGraphError("Graphiti 的上报统计没关掉")
        return self._graphiti

    async def fulltext_analyzers(self) -> dict[str, str]:
        rows = await self.query("SHOW FULLTEXT INDEXES YIELD name, options RETURN name, options")
        return {row["name"]: str((row["options"] or {}).get("indexConfig", {}).get("fulltext.analyzer")) for row in rows}

    async def add_window(self, episode: Episode, previous: Sequence[str]) -> str:
        k = kit(self.settings.lanes)
        graphiti = await self.graphiti()
        with window_scope(episode.group_id, episode.window_id) as raised:
            result = await graphiti.add_episode(
                name=episode.window_id, episode_body=episode.text, source_description=SOURCE_DESCRIPTION,
                reference_time=episode.reference_time, source=k.EpisodeType.message, group_id=check_group_id(episode.group_id),
                entity_types=k.entity_types, previous_episode_uuids=list(previous), edge_types=k.edge_types,
                edge_type_map=k.edge_type_map, custom_extraction_instructions=INSTRUCTIONS,
            )
            if raised:  # Graphiti 别的地方又吞了一次：不算跑成（存进去的那一份由 run 整组删）
                raise _worst(raised)
        return str(result.episode.uuid)

    async def find_episode(self, group_id: str, window_id: str) -> str | None:
        """这个窗口在体检库里已经有了的（跑到一半进程没了），返回 episode 的编号。"""
        rows = await self.query("MATCH (e:Episodic {group_id: $g, name: $n}) RETURN e.uuid AS uuid LIMIT 1",
                                g=check_group_id(group_id), n=window_id)
        return str(rows[0]["uuid"]) if rows else None

    async def count_group(self, group_id: str) -> tuple[int, int]:
        check_group_id(group_id)
        nodes = (await self.query(f"MATCH (n) WHERE n.group_id = $g AND NOT n:{MARKER_LABEL} RETURN count(n) AS n",
                                  g=group_id))[0]["n"]
        edges = (await self.query("MATCH ()-[r]->() WHERE r.group_id = $g RETURN count(r) AS n", g=group_id))[0]["n"]
        return int(nodes), int(edges)

    async def delete_group(self, group_id: str) -> tuple[int, int]:
        """整组删：这一组的节点（连带它们的关系）、关系都删，标记不删。删完数一遍，剩下的不是 0 就报错。"""
        check_group_id(group_id)
        before = await self.count_group(group_id)
        while True:
            rows = await self.query(f"MATCH (n) WHERE n.group_id = $g AND NOT n:{MARKER_LABEL} WITH n LIMIT 1000 "
                                    "DETACH DELETE n RETURN count(*) AS c", g=group_id)
            if not rows or rows[0]["c"] == 0:
                break
        await self.query("MATCH ()-[r]->() WHERE r.group_id = $g DELETE r", g=group_id)
        if await self.count_group(group_id) != (0, 0):
            raise SideGraphError("删完这一组还剩东西")
        return before

    async def group_ids(self) -> set[str]:
        rows = await self.query(f"MATCH (n) WHERE n.group_id IS NOT NULL AND NOT n:{MARKER_LABEL} "
                                "RETURN DISTINCT n.group_id AS g")
        return {row["g"] for row in rows}

    async def export_group(self, group_id: str) -> dict[str, list[dict[str, Any]]]:
        """一组里的实体、关系、episode（只读）。"""
        check_group_id(group_id)
        entities = await self.query(
            "MATCH (n:Entity {group_id: $g}) RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels, "
            "n.summary AS summary", g=group_id)
        edges = await self.query(
            "MATCH (s:Entity)-[r:RELATES_TO {group_id: $g}]->(t:Entity) RETURN r.uuid AS uuid, r.name AS name, "
            "r.fact AS fact, s.uuid AS source, t.uuid AS target, toString(r.valid_at) AS valid_at, "
            "toString(r.invalid_at) AS invalid_at, toString(r.expired_at) AS expired_at, r.episodes AS episodes, "
            "r.kinship AS kinship", g=group_id)
        episodes = await self.query(
            "MATCH (e:Episodic {group_id: $g}) RETURN e.uuid AS uuid, e.name AS name", g=group_id)
        return {"entities": entities, "edges": edges, "episodes": episodes}


def utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)
