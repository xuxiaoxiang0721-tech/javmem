"""L3 抽取：一个窗口调一次模型，一次抽实体、事实和实体之间的关系（关系从提示词 l3-v2 起，J3.9）。

- 输入是 render_window 写出来、按打码模式表又过了一遍的文字。`privacy: private` 的窗口不发。
- 输出用结构化输出（严格的 JSON Schema），回来以后再自己校验一遍：不是 JSON、字段不对算格式错误，
  重试一次。引用了上文（C 编号）、编号不存在、引文是空的，不算格式错误，照收，标在这条事实或关系上，
  交给 L4 隔离。
- 每条事实、关系的证据：T 编号换成原文句子编号、包号和版本，同时留着模型给的引文，L4 拿它核对。
  句子编号按这一次窗口的清单换：分句的规矩改了以后，文字没变的窗口用存下的结果，编号自然换成新的。
- 关系（定义卡 D13 的六种，D210 加了第七种隶属于；老提示词的输出格式照旧六种，见 PART_OF_PROMPTS）先只存在结果库里（每个窗口的 `extraction`）：账本 J4.1 起有关系表，可关系的核对、合并
  （J4.3、J4.4）还没写；L4、L5 现在只读事实和实体。打分（javmem.scoring.results）从结果库读关系。说话人（`speaker`，
  事实、关系都有）是“这条说法出自谁”，事实的 L6 写进账本的 attributed_to（J4.1），账本的 speaker 是证据那一句是谁说的。
- 结果按“窗口文字、提示词版本、模型”做键存下来（javmem.pipeline.store），存过的不再调。
- 同时最多 2 个请求；限流就按回复说的时间退避，几个请求一起等；每次调用的 token 记进账。
- 提示词在 prompts/ 下，带版本号。用哪个照配置 JAVMEM_L3_PROMPT（PROMPTS 里的几个；J4.2 起默认 l3-v17，luna 线的，
  sol 线配 l3-v16；原来是 l3-v4）。提示词或输出结构改了，要换版本号，也要在 PROMPTS 里写它的指纹，
  不然测试不过：不换版本号的话，存下的旧结果会被当成新提示词的结果。
- 预处理 B（认人认事，javmem.pipeline.preb）：配了本机模型才做。一个包一个包按收包的先后，先收名字、标〔指〕，
  标完这个包就把它的窗口交给 L3（照旧两路），本机模型接着做下一个包。标注存在结果库里，写窗口时照原样加上（prepare）。
- 预处理 A（相对的日子换成具体的日子，javmem.pipeline.prea）：提示词选了带 A 那一句的（PRE_A_PROMPTS）才做，不另加开关：
  键里有提示词版本，写账、合并工具照 extracted_by 里的提示词版本算键，自然知道当初加没加 A。整批开头照规则标好、存进
  结果库；写窗口时先加 A、再加 B（B 在 A 的文字上加）。不调模型。
- 放进账本里的现有关系（J4.2 后一半，javmem.pipeline.prer）：提示词选了带这一句的（PRE_R_PROMPTS）才放，也不另加开关。
  抽之前照账本挑出两头都在这个窗口里的现行关系，写成开头说明下面的一段，存进结果库；写窗口时照存下的放（在 A、B 之后）。
  要一个包一个包抽、抽一个写一个（命令行的 extract 一次只抽导出最早的那个包），后面的包才看得到前面的包写的关系。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Protocol

from javmem.config import ConfigError
from javmem.ledger import connect_readonly
from javmem.llm import RATE_LIMIT, SERVER, TRANSPORT, UNAVAILABLE, ModelError, ModelReply, Usage
from javmem.pipeline import prea, prer
from javmem.pipeline.preb import PRE_B_VERSION, PreB, apply_notes, window_context
from javmem.pipeline.render import RenderedWindow, render_window
from javmem.pipeline.store import ResultStore
from javmem.pipeline.window import Window, WindowLine
from javmem.redaction import RedactionPattern
from javmem.timeutil import iso
from javmem.usage import TokenLedger

# 能选的 L3 提示词：版本 → 提示词连同输出结构的指纹（prompt_fingerprint）
PROMPTS = {
    "l3-v4": "d035dc791decdb1178988e561d9aa41af715b1d0b49fc0640fcad8cc7811bba2",  # J3.9 第二遍，J4.2 以前的默认
    "l3-v16": "e351e9bd8e96a41bbb05585eca6e498e22506fd19c620644987f1b5118a8f726",  # sol 线：l3-v6（开始日子的规矩）+〔指〕
    "l3-v17": "73a808e03ca6c68f54e07074384d7bd5d976d41d289aeac34d8e986a4e64b3dd",  # luna 线：l3-v9（放宽门槛、开始日子）+〔指〕
    "l3-v20": "c0703e37516d608827a4c0ce81bbe290ec8e765e6c4889ca0ca707a1da5e9764",  # sol 线：l3-v16 +〔日子〕（预处理 A）
    "l3-v21": "541f27d959bb16e370881d691b6283f8aabbdac33a0c3e42f63e6436c96ee811",  # luna 线：l3-v17 +〔日子〕（预处理 A）
    "l3-v22": "a53b3d6ab0af7846403f5fbc49690aabc1d88886678764bc906caa250e187e75",  # sol 线：l3-v20 + 现有关系（J4.2 后一半）
    "l3-v23": "51c6608323a954ee2561bd3de80e420ccc0e557989ded9711478ea1e09c948c6",  # luna 线：l3-v21 + 现有关系（J4.2 后一半）
    "l3-v24": "9694cb4cf2d80e3107064294c50b9aaa63002623810687ef1898157b889c1a83",  # sol 线：l3-v20 + 隶属于（D210、D213）
    "l3-v25": "709faeff59047c76e306be33551a726c9b6bb397c8184e3f2898ee45fa74f486",  # luna 线：l3-v21 + 隶属于（D210、D213）
}
# 带预处理 A 那一句的提示词：选了它们就做 A、写窗口时加 A（D150）；选别的不做，库里存着的 A 标注也不加
PRE_A_PROMPTS = frozenset({"l3-v20", "l3-v21", "l3-v22", "l3-v23", "l3-v24", "l3-v25"})
# 带“放进账本里的现有关系”那一句的提示词：选了它们才放、写窗口时照存下的放（D154 第 4 条）；一次抽一个包
PRE_R_PROMPTS = frozenset({"l3-v22", "l3-v23"})
# 讲了第七种关系隶属于的提示词（D210、D213）：用七种的输出格式（SCHEMA_V5）。别的照旧用六种的 SCHEMA：老提示词的指纹不变、
# 存下的结果照用，模型也填不了提示词没讲过的隶属于
PART_OF_PROMPTS: frozenset[str] = frozenset({"l3-v24", "l3-v25"})
DEFAULT_PROMPT = "l3-v17"  # J4.2（11-16 主会话定）：默认换成 luna 线的；用哪个模型（D126）还没定，跑 sol 时配 l3-v16
PROMPT_VERSION = DEFAULT_PROMPT  # 没点名时用的版本
PROMPT_SHA256 = PROMPTS[DEFAULT_PROMPT]
ENV_PROMPT = "JAVMEM_L3_PROMPT"
SCHEMA_NAME = "javmem_l3_v4"
SCHEMA_NAME_V5 = "javmem_l3_v5"  # 七种关系的那一份（PART_OF_PROMPTS）

ENTITY_TYPES = ("人", "公司或机构", "项目", "订单或合同", "地点", "资产")
PRECISIONS = ("minute", "day", "month", "quarter", "year", "unknown")
CERTAINTIES = ("stated", "inferred", "vague")
ROLES = ("subject", "object", "mention")
D13_RELATION_TYPES = ("持有", "托管于", "负责", "任职于", "客户是", "亲属")  # 定义卡的六种（D13）：老提示词的输出格式
# 关系清单（D210 加了隶属于：部门、分公司、子公司 → 它所属的那家）。账本里、查询接口、打分都照它：七种都可能有
RELATION_TYPES = (*D13_RELATION_TYPES, "隶属于")
RELATION_STATES = ("生效", "已结束", "暂停")  # 暂停：D101，“先不管”不算结束
RELATION_CERTAINTIES = ("stated", "inferred")  # 含糊的、没定下来的不抽成关系（04 p3：含糊的不进图）


def _object(properties: dict[str, object]) -> dict[str, object]:
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


_TEXT: dict[str, object] = {"type": "string"}
_TEXT_OR_NULL: dict[str, object] = {"type": ["string", "null"]}
_EVIDENCE: dict[str, object] = {"type": "array", "items": _object({"ref": _TEXT, "quote": _TEXT})}


def _choice(values: Sequence[str]) -> dict[str, object]:
    return {"type": "string", "enum": list(values)}


def _schema(relation_types: Sequence[str]) -> dict[str, object]:
    """L3 的输出格式。两份只差关系能填哪几种（relation_types）。"""
    return _object(
        {
            "entities": {
                "type": "array",
                "items": _object({"key": _TEXT, "mention": _TEXT, "name": _TEXT, "type": _choice(ENTITY_TYPES)}),
            },
            "facts": {
                "type": "array",
                "items": _object(
                    {
                        "text": _TEXT,
                        "subject": _TEXT_OR_NULL,
                        "attribute": _TEXT_OR_NULL,
                        "value": _TEXT_OR_NULL,
                        "occurred_at": _TEXT_OR_NULL,
                        "time_precision": _choice(PRECISIONS),
                        "certainty": _choice(CERTAINTIES),
                        "speaker": _TEXT,
                        "entities": {"type": "array", "items": _object({"key": _TEXT, "role": _choice(ROLES)})},
                        "evidence": _EVIDENCE,
                    }
                ),
            },
            # 关系的样子和 javmem.scoring.results 开头写的一样：两头写顶层 entities 的 key，亲属写 kinship（主体是客体的什么人）
            "relations": {
                "type": "array",
                "items": _object(
                    {
                        "subject": _TEXT,
                        "relation": _choice(relation_types),
                        "object": _TEXT,
                        "kinship": _TEXT_OR_NULL,
                        "state": _choice(RELATION_STATES),
                        "valid_from": _TEXT_OR_NULL,
                        "valid_to": _TEXT_OR_NULL,
                        "time_precision": _choice(PRECISIONS),
                        "certainty": _choice(RELATION_CERTAINTIES),
                        "speaker": _TEXT,
                        "evidence": _EVIDENCE,
                    }
                ),
            },
        }
    )


SCHEMA: dict[str, object] = _schema(D13_RELATION_TYPES)  # 老提示词的：关系六种（D13）
SCHEMA_V5: dict[str, object] = _schema(RELATION_TYPES)  # 带隶属于的提示词的：关系七种（D210）


def schema_for(version: str) -> dict[str, object]:
    """这一版提示词用的输出格式：讲了隶属于的（PART_OF_PROMPTS）是七种的那一份，别的是六种的。"""
    return SCHEMA_V5 if version in PART_OF_PROMPTS else SCHEMA


def schema_name_for(version: str) -> str:
    return SCHEMA_NAME_V5 if version in PART_OF_PROMPTS else SCHEMA_NAME


def relation_types_for(version: str) -> tuple[str, ...]:
    """这一版提示词能抽的关系种类。"""
    return RELATION_TYPES if version in PART_OF_PROMPTS else D13_RELATION_TYPES


def prompt_from_env(env: Mapping[str, str] | None = None) -> str:
    """JAVMEM_L3_PROMPT：L3 用哪个提示词。没写的用默认的；写了不认得的报配置错误（什么都不动）。"""
    env = os.environ if env is None else env
    value = env.get(ENV_PROMPT, "").strip() or DEFAULT_PROMPT
    if value not in PROMPTS:
        raise ConfigError(f"{ENV_PROMPT} 只能写这几个：{'、'.join(PROMPTS)}")
    return value


def load_prompt(version: str = PROMPT_VERSION) -> str:
    text = (resources.files("javmem.pipeline") / "prompts" / f"{version}.md").read_text(encoding="utf-8")
    return text.replace("\r\n", "\n")


def prompt_fingerprint(version: str = PROMPT_VERSION) -> str:
    """提示词连同输出结构（这一版用的那一份，schema_for）的指纹。"""
    text = load_prompt(version) + "\n" + json.dumps(schema_for(version), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(window_sha: str, prompt_version: str, model: str) -> str:
    """结果的键：窗口文字（发出去的，打过码）、提示词版本、模型。"""
    return hashlib.sha256("\n".join(("javmem-l3", window_sha, prompt_version, model)).encode("utf-8")).hexdigest()


# ---------- 校验和换编号 ----------


class FormatError(Exception):
    """模型的回复不是 JSON，或者字段不对。消息里不抄回复的内容。"""


def _matches(value: object, kind: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "null": value is None,
    }[kind]


def _check(value: object, schema: Mapping[str, object], path: str) -> None:
    kinds = schema["type"]
    if not any(_matches(value, kind) for kind in (kinds if isinstance(kinds, list) else [kinds])):
        raise FormatError(f"{path} 的类型不对")
    if value is None:
        return
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        raise FormatError(f"{path} 的取值不在范围里")
    if isinstance(value, dict):
        properties = schema["properties"]
        assert isinstance(properties, dict)
        if set(value) != set(properties):
            raise FormatError(f"{path} 的字段不对")
        for name, item in value.items():
            _check(item, properties[name], f"{path}.{name}")
    elif isinstance(value, list):
        items = schema["items"]
        assert isinstance(items, dict)
        for index, item in enumerate(value):
            _check(item, items, f"{path}[{index}]")


def parse_output(content: str, schema: Mapping[str, object] = SCHEMA) -> dict[str, object]:
    """校验 L3 的回复。schema 是这一次发出去的那一份（schema_for），不给的照六种的老格式。"""
    try:
        data = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        raise FormatError("回复不是 JSON") from None
    try:
        _check(data, schema, "$")
        # 转义写出来的孤立代理（"\ud83d"）写不进结果库，也不是正经的文字
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (RecursionError, UnicodeEncodeError):
        raise FormatError("回复里有写不进库的字符或者嵌套太深") from None
    assert isinstance(data, dict)
    return data


def _clean_quote(quote: str, ref: str) -> str:
    """去掉模型顺手带上的编号和〔〕标注，它们不是原文。开头连着几个〔〕的（〔引用〕〔指：…〕，预处理 B）都去掉：
    原文里的〔〕都换成了［］，开头的〔只能是标注。"""
    text = quote.strip()
    if text.startswith(ref + " "):
        text = text[len(ref) + 1 :].lstrip()
    while text.startswith("〔") and "〕" in text:
        text = text[text.index("〕") + 1 :].lstrip()
    return text


def _map_evidence(
    proofs: Sequence[Mapping[str, str]], lines: Mapping[str, WindowLine], redacted: set[str]
) -> tuple[list[dict[str, object]], set[str]]:
    """一条事实或关系的证据：T 编号换成原文句子编号。返回 (证据, 问题)。有问题的那一条照收，标上 problem。"""
    problems: set[str] = set()
    evidence = []
    for proof in proofs:
        ref = proof["ref"].strip()
        quote = _clean_quote(proof["quote"], ref)
        entry: dict[str, object] = {"ref": ref, "quote": quote}
        line = lines.get(ref)
        if line is None:
            entry["problem"] = "unknown_ref"
        else:
            entry.update(
                sentence_id=line.sentence_id,
                packet_id=line.packet_id,
                revision=line.revision,
                kind=line.kind,
                origin=line.origin,
                redacted=ref in redacted,
            )
            if not line.target:
                entry["problem"] = "context_ref"
        if not quote and "problem" not in entry:
            entry["problem"] = "empty_quote"
        if "problem" in entry:
            problems.add(str(entry["problem"]))
        evidence.append(entry)
    if not any("problem" not in entry for entry in evidence):
        problems.add("no_usable_evidence")
    return evidence, problems


_DATE_SHAPES = (
    (re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?\s?(?:Z|[+-]\d{2}:?\d{2})?$"), "minute"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "day"),
    (re.compile(r"^\d{4}-\d{2}$"), "month"),
    (re.compile(r"^\d{4}-?Q[1-4]$", re.IGNORECASE), "quarter"),
    (re.compile(r"^\d{4}$"), "year"),
)


def _precision_of(text: object) -> str:
    """照日子的写法猜精度（和打分那边一样）：认不出的 unknown。"""
    if not isinstance(text, str):
        return "unknown"
    value = text.strip()
    return next((precision for shape, precision in _DATE_SHAPES if shape.match(value)), "unknown")


def map_output(parsed: Mapping[str, object], window: Window, redacted_refs: Sequence[str] = ()) -> dict[str, object]:
    """把 T 编号换成原文句子编号。有问题的照收，标在 problems 里，交给 L4（关系的到 J4.3）。

    旧样子的结果（l3-v1 的样子，没有 relations、事实没有 speaker；手写的测试数据常常这样）：关系当没有，说话人当 None。
    关系的精度只有一个、管开始和结束两个日子：模型写了 unknown、日子却写着的，照日子的写法猜（不然日子就作废了），
    猜过的在关系的 notes 里记 precision_guessed（不是问题，不隔离）。两头落在重复的 key 上的标 duplicate_key：
    L4、L5 认头一个，别处不一定。"""
    lines = {line.ref: line for line in window.lines}
    redacted = set(redacted_refs)
    raw_entities = parsed["entities"]
    assert isinstance(raw_entities, list)
    keys: dict[str, dict[str, object]] = {}
    duplicated: set[str] = set()
    entities = []
    for item in raw_entities:
        entry = {name: item[name] for name in ("key", "mention", "name", "type")}
        if item["key"] in keys:
            entry["problems"] = ["duplicate_key"]
            duplicated.add(str(item["key"]))
        else:
            keys[item["key"]] = entry
        entities.append(entry)

    facts = []
    raw_facts = parsed["facts"]
    assert isinstance(raw_facts, list)
    for item in raw_facts:
        evidence, problems = _map_evidence(item["evidence"], lines, redacted)
        for role in item["entities"]:
            if role["key"] not in keys:
                problems.add("unknown_entity")
        if item["subject"] is not None and item["subject"] not in keys:
            problems.add("unknown_subject")
        fact = {
            name: item[name]
            for name in ("text", "subject", "attribute", "value", "occurred_at", "time_precision", "certainty")
        }
        fact["speaker"] = item.get("speaker")
        fact["entities"] = [dict(role) for role in item["entities"]]
        fact["evidence"] = evidence
        fact["problems"] = sorted(problems)
        facts.append(fact)

    relations = []
    raw_relations = parsed.get("relations") or []
    assert isinstance(raw_relations, list)
    for item in raw_relations:
        evidence, problems = _map_evidence(item["evidence"], lines, redacted)
        for end in ("subject", "object"):
            if item[end] not in keys:
                problems.add(f"unknown_{end}")
            elif item[end] in duplicated:
                problems.add("duplicate_key")
        relation = {
            name: item[name]
            for name in (
                "subject", "relation", "object", "kinship", "state", "valid_from", "valid_to", "time_precision",
                "certainty", "speaker",
            )
        }
        notes = []
        if relation["time_precision"] == "unknown":
            guessed = _precision_of(relation["valid_from"])
            if guessed == "unknown":
                guessed = _precision_of(relation["valid_to"])
            if guessed != "unknown":
                relation["time_precision"] = guessed
                notes.append("precision_guessed")
        relation["evidence"] = evidence
        relation["problems"] = sorted(problems)
        relation["notes"] = notes
        relations.append(relation)
    return {"window_id": window.window_id, "entities": entities, "facts": facts, "relations": relations}


# ---------- 跑一遍 ----------


class Model(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def cache_name(self) -> str: ...

    def complete(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> ModelReply: ...


CACHED = "cached"
DONE = "done"
FAILED = "failed"
PRIVATE = "private"
NOT_STARTED = "not_started"


@dataclass(frozen=True)
class ExtractOptions:
    tag: str = "javmem"  # 记账的标签，预算按它算
    budget: int | None = None  # 这个标签一共最多用多少 token（记账里已经有的也算）
    deadline: float | None = None  # time.monotonic() 的时刻，过了就不再开始新的调用
    limit: int | None = None  # 这次最多新调几个窗口
    concurrency: int = 2
    rate_limit_retries: int = 6
    transport_retries: int = 2
    format_retries: int = 1
    backoff_base: float = 2.0
    backoff_cap: float = 60.0
    stop_after_transport_failures: int = 3  # 连着几个窗口都网络出错，就停下
    stop_after_rate_limited_windows: int = 3  # 连着几个窗口重试完了还是限流，就停下
    timeout_retries: int = 1  # 等回复超时的，只再试一次：接口那边多半照样算了钱
    # Flex 资源不够（不收钱）：D169 定的是等，不改用标准价。几个请求一起等，30、60、120 秒往上翻，一回最多 10 分钟；
    # 一个窗口最多再等 8 回，连着 3 个窗口等完了还是没资源，整批停。资源一直不回来时，一个窗口要等 45 分钟到一个半小时
    # （头一个最短；同时两路时等的时间翻得快），到整批停最多约 4 小时
    unavailable_retries: int = 8
    unavailable_wait: float = 30.0
    unavailable_cap: float = 600.0
    stop_after_unavailable_windows: int = 3
    clock: Callable[[], float] = time.monotonic  # Flex 资源不够时等到几点用的钟（测试换成跟着 sleep 走的假钟）
    tokens_per_char: float = 0.8  # 预算估算用：一个字大约几个 token
    output_reserve: int = 8000  # 预算估算用：模型没说一次最多输出多少时，按这么多留
    max_input_chars: int = 60000  # 窗口写出来超过这么多字的不发，记失败（too_big）
    retry_failed: bool = False  # 以前截断、拒答、太长、内容被挡、格式错过的窗口，这次也重调
    sleep: Callable[[float], None] = time.sleep

    def describe(self) -> dict[str, object]:
        return {
            "tag": self.tag,
            "budget": self.budget,
            "limit": self.limit,
            "concurrency": self.concurrency,
            "has_deadline": self.deadline is not None,
            "retry_failed": self.retry_failed,
        }


@dataclass
class WindowOutcome:
    window_id: str
    cache_key: str
    status: str
    attempts: int = 0
    detail: str = ""
    usage: Usage = field(default_factory=Usage)
    seconds: float = 0.0


@dataclass
class RunResult:
    run_id: str
    outcomes: list[WindowOutcome]
    stopped: str | None  # 整个停下的原因：模型调不通、预算、网络

    def count(self, status: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == status)


class _Throttle:
    """限流时几个请求一起等。等多久：回复说的和 2、4、8 秒往上翻的，取长的；超过 backoff_cap 的整批停下。"""

    def __init__(self, options: ExtractOptions) -> None:
        self.options = options
        self.lock = threading.Lock()
        self.until = 0.0
        self.streak = 0

    def wait(self) -> None:
        with self.lock:
            delay = self.until - time.monotonic()
        if delay > 0:
            self.options.sleep(delay)

    def hit(self, retry_after: float | None) -> float:
        """记一次限流，返回接口要我们等多少秒（它没说就是 0）。超过 backoff_cap 的由调用方整批停下。

        自己翻倍的等法封顶在 backoff_cap，不会因为自己翻上去就停；只看接口说的那个数，加抖动之前比。
        """
        with self.lock:
            self.streak += 1
            hint = retry_after if retry_after is not None and math.isfinite(retry_after) else 0.0
            if hint > self.options.backoff_cap:
                return hint
            delay = max(min(self.options.backoff_base * 2 ** (self.streak - 1), self.options.backoff_cap), hint)
            delay += secrets.randbelow(1000) / 1000 * min(1.0, delay)  # 错开一点，别一起撞回去
            self.until = max(self.until, time.monotonic() + delay)
            return hint

    def ok(self) -> None:
        with self.lock:
            self.streak = 0


PATIENCE_STEP = 5.0  # Flex 资源不够时一段睡几秒：等的时候也看整批停没停、到没到时间


class _Patience:
    """Flex 资源不够时几个请求一起等：从 unavailable_wait 秒往上翻，一回最多 unavailable_cap 秒。接口说要等更久的也只等到
    封顶，不因为这个停下整批（那是限流的规矩）：资源不够不收钱，多等只是慢。"""

    def __init__(self, options: ExtractOptions) -> None:
        self.options = options
        self.lock = threading.Lock()
        self.until = 0.0
        self.streak = 0

    def wait(self, stop: threading.Event, deadline: float | None) -> None:
        """等到该等的时候。几秒一段地睡，每一段都重看一眼等到几点：另一路在这期间又碰上资源不够、把时间拉长了，照新的等，
        不先发出去。整批停了（Ctrl+C、致命错）、到时间了（--max-minutes）就不等了，不干等十分钟。
        睡了多久也自己数着：钟要是不走（测试里假的 sleep），照睡过的算，不会一直等下去。"""
        clock = self.options.clock
        start = clock()
        waited = 0.0
        while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
            with self.lock:
                left = self.until - max(clock(), start + waited)
            if left <= 0:
                return
            step = min(left, PATIENCE_STEP)
            self.options.sleep(step)
            waited += step

    def hit(self, retry_after: float | None) -> None:
        with self.lock:
            self.streak += 1
            cap = self.options.unavailable_cap
            delay = min(self.options.unavailable_wait * 2 ** min(self.streak - 1, 30), cap)
            if retry_after is not None and math.isfinite(retry_after):
                delay = max(delay, min(retry_after, cap))
            delay += secrets.randbelow(1000) / 1000 * min(1.0, delay)  # 错开一点，别一起撞回去
            self.until = max(self.until, self.options.clock() + delay)

    def ok(self) -> None:
        with self.lock:
            self.streak = 0


class _Budget:
    """预算：开始一次调用前按“输入的估计加一次最多的输出”留出来，调完按实际的结账。

    留不出来时，别的请求还在跑就等它结账再看（它多半用不了留的那么多）；都没在跑才算预算用完。
    """

    def __init__(self, ledger: TokenLedger | None, options: ExtractOptions) -> None:
        self.limit = options.budget
        self.used = ledger.used(options.tag) if ledger is not None and self.limit is not None else 0
        self.reserved = 0
        self.cond = threading.Condition()

    def reserve(self, estimate: int, stop: threading.Event) -> bool:
        with self.cond:
            while self.limit is not None and self.used + self.reserved + estimate > self.limit:
                if self.reserved == 0 or stop.is_set():
                    return False
                self.cond.wait(timeout=1.0)
            self.reserved += estimate
            return True

    def settle(self, estimate: int, actual: int) -> None:
        with self.cond:
            self.reserved -= estimate
            self.used += actual
            self.cond.notify_all()


class _StopRun(Exception):
    """账或者结果库写不进去，整批停下。"""


def _now() -> str:
    return iso(datetime.now(timezone.utc).astimezone())


def window_row(window: Window, rendered: RenderedWindow, key: str, model: str, prompt: str = PROMPT_VERSION) -> dict[str, object]:
    """存进结果库的窗口清单：每个编号是哪句原文。private 的、打码没打干净的不存发出去的文字。"""
    redacted = set(rendered.redacted_refs)
    manifest = [
        {
            "ref": line.ref,
            "sentence_id": line.sentence_id,
            "packet_id": line.packet_id,
            "revision": line.revision,
            "kind": line.kind,
            "origin": line.origin,
            "field": line.field,
            "clipped": line.clipped,
            "redacted": line.ref in redacted,
            "shown": rendered.shown.get(line.ref, ""),  # 窗口里写出来的样子（打过码、［］换过），L4 比引文用
        }
        for line in window.lines
    ]
    keep_text = not window.scope.private and not rendered.leftover
    return {
        "window_id": window.window_id,
        "cache_key": key,
        "window_sha": rendered.sha256,
        "prompt_version": prompt,
        "model": model,
        "source": window.source,
        "channel": window.channel,
        "scope": json.dumps(window.scope.as_dict(), ensure_ascii=False),
        "packets": json.dumps(window.packets),
        "manifest": json.dumps(manifest, ensure_ascii=False),
        "input_text": rendered.text if keep_text else "",
        "redactions": json.dumps(rendered.redactions, ensure_ascii=False),
        "built_at": _now(),
    }


class _Request(Protocol):
    """一次要发的：给模型的文字。L3 是 RenderedWindow，L4 的模型复核是 recheck.RecheckRequest。"""

    @property
    def text(self) -> str: ...


class _Run:
    """一批调用的规矩：同时最多几个、限流退避、预算、记账、超时和格式错的重试、整批停下。

    L3 抽取用它；L4 的模型复核（javmem.pipeline.recheck）换掉输出结构、解析、存结果、记调用这几处接着用，
    重试的逻辑只有这一份。
    """

    purpose = "l3_extract"  # 记账的用途
    schema: Mapping[str, object] = SCHEMA  # L3 的照提示词换成那一版的（run_extraction）；子类换成自己的输出结构
    schema_name = SCHEMA_NAME

    def __init__(
        self,
        model: Model,
        store: ResultStore,
        ledger: TokenLedger | None,
        options: ExtractOptions,
        log: Callable[[str], None] | None,
        *,
        system: str | None = None,
        prompt: str = PROMPT_VERSION,
    ) -> None:
        self.model = model
        self.store = store
        self.ledger = ledger
        self.options = options
        self.log = log
        self.prompt = prompt
        self.system = load_prompt(prompt) if system is None else system
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
        self.throttle = _Throttle(options)
        self.patience = _Patience(options)
        self.budget = _Budget(ledger, options)
        self.stop = threading.Event()
        self.stopped: str | None = None
        self.lock = threading.Lock()
        self.transport_streak = 0
        self.rate_streak = 0
        self.unavailable_streak = 0
        self.requested_tier = str(getattr(model, "service_tier", "") or "")  # 要的是哪一档：回复没说实际是哪一档时照它记账
        self.finished = 0
        self.pending: dict[str, int] = {}  # 窗口 → 还没结的预算预留
        max_output = getattr(model, "max_output_tokens", None)
        if max_output is None:
            settings = getattr(model, "settings", None)
            max_output = getattr(settings, "max_output_tokens", None)
        self.max_output = int(max_output) if max_output else options.output_reserve

    def halt(self, reason: str) -> None:
        with self.lock:
            if self.stopped is None:
                self.stopped = reason
        self.stop.set()

    def run_options(self, **fields: object) -> dict[str, object]:
        """记进结果库 runs 的这一次运行的选项。走 Flex 跑的记着要的那一档：结果的键不变，靠它分得清（D169）。"""
        described = {**self.options.describe(), **fields}
        if self.requested_tier:
            described["service_tier"] = self.requested_tier
        return described

    # ---------- 钩子：L3 的；模型复核换掉这几个 ----------

    def parse(self, content: str, request: _Request) -> dict[str, object]:
        """校验模型的回复（照发出去的那一份格式），不对抛 FormatError。"""
        return parse_output(content, self.schema)

    def save(
        self, window: Window, request: _Request, key: str, reply: ModelReply, parsed: dict[str, object], usage: Usage
    ) -> None:
        """存结果。写不进去抛 OSError、sqlite3.Error，整批停下。"""
        assert isinstance(request, RenderedWindow)
        extraction = map_output(parsed, window, request.redacted_refs)
        self.store.save_result(
            cache_key=key,
            window_sha=request.sha256,
            prompt_version=self.prompt,
            model=self.model.cache_name,
            output=reply.content,
            parsed=parsed,
            response_model=reply.model,
            usage=usage,
            seconds=reply.seconds,
            created_at=_now(),
        )
        self.store.save_extraction(window.window_id, key, extraction)

    def record_call(self, row: Mapping[str, object]) -> None:
        self.store.record_call(row)

    def record(
        self, window: Window, key: str, attempt: int, status: str, detail: str, usage: Usage, seconds: float,
        tier: str | None = None, response_model: str | None = None,
    ) -> None:
        """记一次调用：结果库的 calls 和 token 账。账先记。哪个写不进去都整批停下：记不了账就不能再花钱。

        tier：回复说实际按哪一档服务的（没说的照要的那一档）。D169：记账的用途后面加上这一档（`_flex` 是半价），钱按那一档算；
        标准的（default、没说）照旧不加。账本的列不变。
        response_model：回复说它实际用的是哪个模型（D205 ⑪），记进 token 账最后一列；没拿到回复的是空的。结果库的 calls 不记
        （成功的那一次结果库 results 里本来就记着）。"""
        served = tier if tier is not None else self.requested_tier
        purpose = self.purpose if served in ("", "default", "auto") else f"{self.purpose}_{served}"
        row = {
            "run_id": self.run_id,
            "window_id": window.window_id,
            "cache_key": key,
            "attempt": attempt,
            "status": status,
            "detail": detail[:200],
            **usage.as_dict(),
            "seconds": round(seconds, 3),
            "at": _now(),
        }
        if self.ledger is not None:
            try:
                self.ledger.append({**row, "tag": self.options.tag, "purpose": purpose, "model": self.model.cache_name,
                                    "response_model": response_model})
            except OSError as error:
                self.halt(f"ledger: token 账写不进去（{type(error).__name__}）")
                raise _StopRun from None
        try:
            self.record_call(row)
        except (OSError, sqlite3.Error) as error:
            self.halt(f"store: 结果库写不进去（{type(error).__name__}）")
            raise _StopRun from None

    def input_estimate(self, rendered: _Request) -> int:
        return int((len(self.system) + len(rendered.text)) * self.options.tokens_per_char)

    def blocked(self, outcome: WindowOutcome) -> bool:
        """这一次不能开始了：整批停了，或者到时间了。"""
        if self.stop.is_set():
            outcome.detail = outcome.detail or self.stopped or ""
            return True
        if self.options.deadline is not None and time.monotonic() >= self.options.deadline:
            outcome.detail = "deadline"
            if outcome.attempts:
                outcome.status = FAILED
            return True
        return False

    def work(self, window: Window, rendered: _Request, key: str) -> WindowOutcome:
        outcome = WindowOutcome(window.window_id, key, NOT_STARTED)
        try:
            return self._work(window, rendered, key, outcome)
        except _StopRun:  # 账或者库写不进去，整批已经停了
            outcome.status = FAILED if outcome.attempts else NOT_STARTED
            outcome.detail = self.stopped or ""
            return outcome
        except Exception as error:  # 一个窗口出了想不到的错，不让整批停下；钱已经记了账
            outcome.status, outcome.detail = FAILED, f"error: {type(error).__name__}"
            return outcome
        except BaseException:  # Ctrl+C 这类：马上让别的线程别再开始新的调用
            self.halt("interrupted")
            raise
        finally:
            pending = self.pending.pop(window.window_id, 0)  # 出了意外没结的预留放开
            if pending:
                self.budget.settle(pending, 0)

    def _work(self, window: Window, rendered: _Request, key: str, outcome: WindowOutcome) -> WindowOutcome:
        rate = transport = formats = timeouts = unavailable = 0
        options = self.options
        while True:
            if self.blocked(outcome):
                return outcome
            input_estimate = self.input_estimate(rendered)
            reserve = input_estimate + self.max_output  # 一次最多可能用这么多
            if not self.budget.reserve(reserve, self.stop):
                self.halt("budget")
                outcome.detail = "budget"
                if outcome.attempts:
                    outcome.status = FAILED
                return outcome
            self.pending[window.window_id] = reserve
            self.throttle.wait()
            self.patience.wait(self.stop, options.deadline)
            if self.blocked(outcome):  # 等的这一会儿可能整批停了或者到时间了
                self.budget.settle(self.pending.pop(window.window_id), 0)
                return outcome
            outcome.attempts += 1
            started = time.monotonic()
            try:
                reply = self.model.complete(self.system, rendered.text, self.schema, self.schema_name)
            except ModelError as error:
                elapsed = time.monotonic() - started
                outcome.seconds += elapsed
                outcome.status, outcome.detail = FAILED, error.kind
                self.pending.pop(window.window_id, None)
                if error.usage is not None and error.usage.total:
                    charged, status = error.usage, error.kind  # 回复里写了用量：按它记
                elif error.timeout:
                    charged, status = Usage(input_estimate, self.max_output), f"{error.kind}_estimated"
                else:
                    charged, status = Usage(), error.kind
                self.budget.settle(reserve, charged.total)
                self.record(window, key, outcome.attempts, status, error.detail, charged, elapsed,
                            tier=getattr(error, "service_tier", None), response_model=getattr(error, "response_model", None))
                outcome.usage = outcome.usage + charged
                if error.timeout:
                    # 发出去了、没拿到回复：只再试一次；还不行算一次网络出错，连着几个窗口这样就整批停下
                    timeouts += 1
                    if timeouts <= options.timeout_retries:
                        continue
                    with self.lock:
                        self.transport_streak += 1
                        streak = self.transport_streak
                    if streak >= options.stop_after_transport_failures:
                        self.halt(f"{error.kind}: 连着 {streak} 个窗口拿不到回复")
                    return outcome
                if error.fatal:
                    self.halt(f"{error.kind}: {error.detail}" if error.detail else error.kind)
                    return outcome
                if error.kind == UNAVAILABLE:  # Flex 资源不够：不收钱，几个请求一起多等一会儿再试（D169：等）
                    unavailable += 1
                    self.patience.hit(error.retry_after)
                    if unavailable <= options.unavailable_retries:
                        continue
                    with self.lock:
                        self.unavailable_streak += 1
                        streak = self.unavailable_streak
                    if streak >= options.stop_after_unavailable_windows:
                        self.halt(f"{UNAVAILABLE}: 连着 {streak} 个窗口等不到 Flex 的资源")
                    return outcome
                if error.kind == RATE_LIMIT:
                    rate += 1
                    wait = self.throttle.hit(error.retry_after)
                    if wait > options.backoff_cap:
                        self.halt(f"{RATE_LIMIT}: 接口要等 {wait:.0f} 秒")
                        return outcome
                    if rate <= options.rate_limit_retries:
                        continue
                    with self.lock:
                        self.rate_streak += 1
                        streak = self.rate_streak
                    if streak >= options.stop_after_rate_limited_windows:
                        self.halt(f"{RATE_LIMIT}: 连着 {streak} 个窗口一直限流")
                    return outcome
                if error.kind in (SERVER, TRANSPORT):
                    transport += 1
                    if transport <= options.transport_retries:
                        options.sleep(min(options.backoff_base * 2 ** (transport - 1), options.backoff_cap))
                        continue
                    with self.lock:
                        self.transport_streak += 1
                        streak = self.transport_streak
                    if streak >= options.stop_after_transport_failures:
                        self.halt(f"{error.kind}: 连着 {streak} 个窗口出错")
                    return outcome
                return outcome  # 模型拒绝回答、窗口太长、内容被挡，重试也一样
            self.pending.pop(window.window_id, None)
            # 先把钱记上：后面解析、写库出什么错，这一笔都不能少记
            usage, estimated = reply.usage, False
            if usage.total == 0:  # 接口没给用量：按估计的记
                usage, estimated = Usage(input_estimate, self.max_output), True
            self.budget.settle(reserve, usage.total)
            self.throttle.ok()
            self.patience.ok()
            with self.lock:
                self.transport_streak = 0
                self.rate_streak = 0
                self.unavailable_streak = 0
            outcome.usage = outcome.usage + usage
            outcome.seconds += reply.seconds
            status, detail = "ok", ""
            parsed: dict[str, object] | None = None
            if reply.finish_reason == "length":
                status = "length"
            else:
                try:
                    parsed = self.parse(reply.content, rendered)
                except FormatError as error:
                    status, detail = "format_error", str(error)
            self.record(window, key, outcome.attempts, f"{status}_estimated" if estimated else status, detail, usage, reply.seconds,
                        tier=getattr(reply, "service_tier", None), response_model=getattr(reply, "model", None))
            if status == "length":
                outcome.status, outcome.detail = FAILED, "length"
                return outcome
            if parsed is None:
                formats += 1
                outcome.status, outcome.detail = FAILED, "format_error"
                if formats <= options.format_retries:
                    continue
                return outcome
            try:
                self.save(window, rendered, key, reply, parsed, usage)
            except (OSError, sqlite3.Error) as error:  # 结果存不下来，再调也是白花钱
                self.halt(f"store: 结果库写不进去（{type(error).__name__}）")
                outcome.status, outcome.detail = FAILED, "store"
                return outcome
            outcome.status, outcome.detail = DONE, ""
            return outcome

    def progress(self, outcome: WindowOutcome, total: int) -> None:
        with self.lock:
            self.finished += 1
            finished = self.finished
        if self.log is not None:
            usage = outcome.usage
            self.log(
                f"[{finished}/{total}] {outcome.window_id} {outcome.status}"
                f"{'（' + outcome.detail + '）' if outcome.detail else ''}，调了 {outcome.attempts} 次，"
                f"输入 {usage.input_tokens}，输出 {usage.output_tokens}，{outcome.seconds:.1f} 秒"
            )


def forget_private_revisions(
    ledger_path: Path, store: ResultStore, requeue: Callable[[set[str]], bool] | None = None
) -> int:
    """包重发成 private 的新版本以后：旧版本在结果库里的窗口（文字里有旧版本的原文，拿它当上文的窗口也算）连同
    只被它们用到的 L3 结果、复核判定删掉。新版本是 private 的，收件时就不排队、不会重抽，以前要
    javmem purge --revision 旧版本 才删（L4 模型复核那一段留下的）。拿到结果库的锁以后调。返回删了几个窗口。

    拿旧版本当上文的别的包，窗口删了、包不改回排队：它们的说法只来自自己的正文，账本不用动；以后整本重建时
    这几个窗口要重抽。

    预处理 B：别的包的标注里用了旧版本里学来的名字的（名字写进了它们的 L3 结果、账本），窗口删掉，包要改回排队重抽：
    requeue(包号) 改，改成了返回 True。改不成（没给 requeue、写账正在跑）的，这一次先不动这些窗口和那些名字，
    下次再来（旧版本里学来的名字不是现行的，本来就不会再用）。"""
    conn = connect_readonly(ledger_path)
    try:
        rows = conn.execute(
            "SELECT old.packet_id, old.revision FROM packets AS current JOIN packets AS old"
            " ON old.packet_id = current.packet_id AND old.revision < current.revision"
            " WHERE current.state = 'current' AND current.privacy = 'private'"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0
    revisions = [(str(row[0]), int(row[1])) for row in rows]
    doomed, _ = store.doomed(revisions=revisions)
    noted, targets = store.doomed_by_notes(revisions=revisions)
    if noted and (requeue is None or not requeue(targets)):
        keep = set(noted)
        store.delete_windows([pair for pair in doomed if pair not in keep])
        return len(doomed) - len(keep)
    store.delete_windows(doomed)
    store.forget_preb(revisions=revisions)  # 预处理 B 从旧版本里学来的名字、没有窗口的标注也删
    store.forget_prea(revisions=revisions)  # 预处理 A 没有窗口的标注也删
    store.forget_prer(revisions=revisions)  # 放进去的现有关系：没有窗口的、出自旧版本的也删
    return len(doomed)


def prepare(
    windows: Sequence[Window],
    model: Model,
    patterns: Sequence[RedactionPattern],
    *,
    store: ResultStore | None = None,
    prompt: str = PROMPT_VERSION,
    fresh_a: bool = False,
    fresh_r: prer.PreR | None = None,
) -> list[tuple[Window, RenderedWindow, str]]:
    """每个窗口写成文字、打码、算出键。给了结果库的：库里存着这个窗口的标注就照原样加上，和抽的时候一字不差，
    键也一样（核对、写账、合并工具、接着跑都靠这个）。没标过的照原样。

    先加预处理 A 的〔日子〕（提示词带 A 的才加，PRE_A_PROMPTS），再加预处理 B 的〔指〕，最后放账本里的现有关系（提示词带
    这一句的才放，PRE_R_PROMPTS）。都按没加标注的窗口文字的指纹认；B 和现有关系还按可见范围和包的版本认（window_context）：
    同一个包换了范围、版本重发的，以前的不用。
    fresh_a、fresh_r（空跑用）：A、现有关系没存过的窗口当场算，不存，算出来的文字、键和真跑一样。"""
    with_a = prompt in PRE_A_PROMPTS
    with_r = prompt in PRE_R_PROMPTS
    prepared = []
    for window in windows:
        rendered = render_window(window, patterns)
        plain = rendered
        plain_sha = rendered.sha256
        if store is not None and not window.scope.private:
            if with_a:
                dates = store.prea_notes(window.window_id, plain_sha, prea.PRE_A_VERSION)
                if dates is None and fresh_a and not rendered.leftover:
                    dates, _ = prea.window_notes(window, rendered)
                if dates and not rendered.leftover:  # 打码没打干净的本来就不发：不加 A（这种行不存文字，撤回只能照 noted_sha 认）
                    rendered = prea.apply_notes(rendered, dates)
            context = window_context(window)
            notes = store.preb_notes(window.window_id, plain_sha, PRE_B_VERSION, context)
            if notes:
                rendered = apply_notes(rendered, notes)
            if with_r and not rendered.leftover:  # 打码没打干净的不发，也不放
                block = store.prer_notes(window.window_id, plain_sha, prer.PRE_R_VERSION, context, prer.notes_digest(notes))
                if block is None and fresh_r is not None:
                    block = fresh_r.block_for(window, plain, notes or {}, patterns)
                if block:
                    rendered = prer.apply_block(rendered, block)
        prepared.append((window, rendered, cache_key(rendered.sha256, prompt, model.cache_name)))
    return prepared


def run_extraction(
    windows: Sequence[Window],
    model: Model,
    store: ResultStore,
    *,
    patterns: Sequence[RedactionPattern],
    ledger: TokenLedger | None = None,
    options: ExtractOptions = ExtractOptions(),
    log: Callable[[str], None] | None = None,
    prompt: str = PROMPT_VERSION,
    pre_b: PreB | None = None,
    pre_r: prer.PreR | None = None,
) -> RunResult:
    """L3。存过的直接用，private 的不发，打码没打干净的不发，其余的调模型。断了重跑接着来。

    这一批里文字一样的几个窗口只调一次，结果按各自的清单换编号。Ctrl+C 或者别的意外：不再开始新的调用，
    正在跑的跑完，记下这一次跑到哪了，再把错报出去。

    pre_b（预处理 B，认人认事，b4）：一个包一个包按收包的先后，先收名字、标〔指〕（本机模型），标完这个包就把它的窗口
    交给 L3，本机模型接着做下一个包；L3 照旧 options.concurrency 路一起跑，不等名单。整批停了（预算、密钥、网络）、
    到时间了，后面的包不再标、不再抽。

    预处理 A（相对的日子换成具体的日子）：提示词带 A 的（PRE_A_PROMPTS），整批开头照规则把没标过的窗口标好、存进结果库，
    写窗口时照存下的加上（prepare）。不调模型，不等 B。

    pre_r（放进账本里的现有关系）：提示词带这一句的（PRE_R_PROMPTS）一定要给，不然报 ValueError。窗口交给 L3 之前照
    现在的账本算好、存进结果库（有 B 的，等这个包标完 B 再算：要用〔指〕标的名字）。账本要已经写好前面的包：一次只抽
    一个包、抽一个写一个，是调用的人（命令行的 extract）管的。
    """
    if prompt in PRE_R_PROMPTS and pre_r is None:
        raise ValueError(f"提示词 {prompt} 要放账本里的现有关系：要给 pre_r（读哪个账本）")
    if prompt in PRE_A_PROMPTS:
        prea.PreA(log=log).annotate(windows, store, patterns)
    if prompt not in PRE_R_PROMPTS:
        pre_r = None  # 别的提示词不放，给了也不用
    run = _Run(model, store, ledger, options, log, prompt=prompt)
    run.schema, run.schema_name = schema_for(prompt), schema_name_for(prompt)  # 讲了隶属于的提示词用七种的那一份（D210）
    described = run.run_options(model=model.cache_name, prompt=prompt)
    if prompt in PRE_A_PROMPTS:
        described["pre_a"] = prea.PRE_A_VERSION
    if pre_r is not None:
        described["pre_r"] = prer.PRE_R_VERSION
    if pre_b is not None:
        described["pre_b"] = PRE_B_VERSION
        pre_b.deadline = options.deadline  # 过了时间本机模型也不再问（一个包当中也停）
    store.start_run(run.run_id, _now(), described)
    outcomes: dict[str, WindowOutcome] = {}
    followers: dict[str, list[tuple[Window, RenderedWindow]]] = {}  # 键 → 文字和它一样、跟着用结果的窗口
    leaders: set[str] = set()
    futures: list[Future[WindowOutcome]] = []
    live = pre_b is not None  # 主线程忙着标后面的包：L3 抽完一个就报一行，不等收尾

    def report(done: Future[WindowOutcome]) -> None:
        if not done.cancelled() and done.exception() is None:
            run.progress(done.result(), len(futures))

    def classify(group: Sequence[Window], pool: ThreadPoolExecutor) -> None:
        """这几个窗口：存过的直接用，不发的记下来，要调的交给 L3。"""
        for window, rendered, key in prepare(group, model, patterns, store=store, prompt=prompt):
            store.save_window(window_row(window, rendered, key, model.cache_name, prompt))
            if window.scope.private:
                outcomes[window.window_id] = WindowOutcome(window.window_id, key, PRIVATE)
                continue
            if rendered.leftover:  # 打码模式表还扫得出来：不发，也不存发出去的文字
                outcomes[window.window_id] = WindowOutcome(window.window_id, key, FAILED, detail="unredacted")
                continue
            cached = store.result(key)
            if cached is not None:
                store.save_extraction(window.window_id, key, map_output(cached.parsed, window, rendered.redacted_refs))
                outcomes[window.window_id] = WindowOutcome(window.window_id, key, CACHED)
                continue
            failed_before = None if options.retry_failed else store.last_failure(key)
            if failed_before is not None:  # 重调多半还是一样的结果，白花钱；要重调加 --retry-failed
                outcomes[window.window_id] = WindowOutcome(window.window_id, key, FAILED, detail=f"failed_before: {failed_before}")
                continue
            if key in leaders:
                followers.setdefault(key, []).append((window, rendered))
                outcomes[window.window_id] = WindowOutcome(window.window_id, key, NOT_STARTED, detail="same_text")
                continue
            if len(rendered.text) > options.max_input_chars:  # 发出去也只会被拒，还可能让整批停下
                outcomes[window.window_id] = WindowOutcome(window.window_id, key, FAILED, detail="too_big")
                continue
            if options.limit is not None and len(futures) >= options.limit:
                outcomes[window.window_id] = WindowOutcome(window.window_id, key, NOT_STARTED, detail="limit")
                continue
            leaders.add(key)
            future = pool.submit(run.work, window, rendered, key)
            if live:
                future.add_done_callback(report)
            futures.append(future)

    try:
        pool = ThreadPoolExecutor(max_workers=max(1, options.concurrency))
        try:
            if pre_b is None:
                if pre_r is not None:
                    pre_r.annotate(windows, store, patterns)
                classify(windows, pool)
            else:
                for group in pre_b.groups(windows):
                    late = options.deadline is not None and time.monotonic() >= options.deadline
                    full = options.limit is not None and len(futures) >= options.limit
                    if not (run.stop.is_set() or late or full):  # 整批停了、到时间了、调够了：后面的包不再标
                        pre_b.run_packet(group, store, patterns)
                        if pre_r is not None:  # 放现有关系要用这个包的〔指〕：标完再算
                            pre_r.annotate(group, store, patterns)
                    classify(group, pool)  # 存过的照样用（标过的照存下的加上），别的照常记成没开始
            for future in futures:
                outcome = future.result()
                outcomes[outcome.window_id] = outcome
                if not live:
                    run.progress(outcome, len(futures))
        except BaseException:
            run.halt("interrupted")
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True)

        for key, waiting in followers.items():
            stored = store.result(key)
            for window, rendered in waiting:
                if stored is not None:
                    store.save_extraction(window.window_id, key, map_output(stored.parsed, window, rendered.redacted_refs))
                    outcomes[window.window_id] = WindowOutcome(window.window_id, key, CACHED)
    finally:
        counts = {
            status: sum(1 for outcome in outcomes.values() if outcome.status == status)
            for status in (CACHED, DONE, FAILED, PRIVATE, NOT_STARTED)
        }
        store.finish_run(run.run_id, _now(), {"stopped": run.stopped, **counts})
    result = RunResult(run.run_id, [outcomes[window.window_id] for window in windows], run.stopped)
    return result


def summarize(
    windows: Sequence[Window],
    store: ResultStore,
    model: Model,
    patterns: Sequence[RedactionPattern],
    *,
    prompt: str = PROMPT_VERSION,
) -> dict[str, object]:
    """这批窗口一整遍的数：格式错误率、每包的 token 和耗时、失败和重试。不抄原文。

    文字一样的几个窗口共用一个键，调用和 token 只算在第一个窗口上。
    """
    prepared = prepare(windows, model, patterns, store=store, prompt=prompt)
    keys = {key for _, _, key in prepared}
    calls = store.calls(keys)
    redactions: dict[str, int] = {}
    for window, rendered, _ in prepared:
        if not window.scope.private:
            for kind, count in rendered.redactions.items():
                redactions[kind] = redactions.get(kind, 0) + count
    by_key: dict[str, list[object]] = {}
    for row in calls:
        by_key.setdefault(row["cache_key"], []).append(row)
    statuses: dict[str, int] = {}
    for row in calls:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    answered = sum(
        count
        for status, count in statuses.items()
        if status.split("_estimated")[0] in ("ok", "format_error", "length")
    )
    format_errors = statuses.get("format_error", 0) + statuses.get("format_error_estimated", 0)

    per_packet: dict[str, dict[str, float]] = {}
    totals = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0, "seconds": 0.0}
    done = private = missing = unsent = 0
    facts = entities = relations = 0
    problems: dict[str, int] = {}
    relation_problems: dict[str, int] = {}
    relation_types: dict[str, int] = {}
    counted: set[str] = set()
    for window, rendered, key in prepared:
        if window.scope.private:
            private += 1
            continue
        if rendered.leftover:
            unsent += 1
        stored = store.result(key)
        if stored is None:
            missing += 1
        else:
            done += 1
            extraction = store.extraction(window.window_id, key) or {}
            window_facts = extraction.get("facts", [])
            assert isinstance(window_facts, list)
            facts += len(window_facts)
            entity_rows = extraction.get("entities", [])
            assert isinstance(entity_rows, list)
            entities += len(entity_rows)
            for fact in window_facts:
                for problem in fact["problems"]:
                    problems[problem] = problems.get(problem, 0) + 1
            window_relations = extraction.get("relations") or []
            assert isinstance(window_relations, list)
            relations += len(window_relations)
            for relation in window_relations:
                kind = str(relation.get("relation"))
                relation_types[kind] = relation_types.get(kind, 0) + 1
                for problem in relation.get("problems") or []:
                    relation_problems[problem] = relation_problems.get(problem, 0) + 1
        rows = by_key.get(key, []) if key not in counted else []
        counted.add(key)
        window_usage = {name: sum(row[name] for row in rows) for name in totals}  # type: ignore[index]
        for name in totals:
            totals[name] += window_usage[name]
        chars: dict[str, int] = {}
        for line in window.targets:
            chars[line.packet_id] = chars.get(line.packet_id, 0) + len(line.text)
        whole = sum(chars.values()) or 1
        for packet_id, count in chars.items():
            entry = per_packet.setdefault(
                packet_id, {"windows": 0, **{name: 0 for name in totals}}  # type: ignore[dict-item]
            )
            entry["windows"] += 1
            for name in totals:
                entry[name] += window_usage[name] * count / whole
    return {
        "prompt_version": prompt,
        "model": model.cache_name,
        "windows": len(prepared),
        "done": done,
        "private": private,
        "missing": missing,
        "not_sent_unredacted": unsent,
        "calls": len(calls),
        "call_statuses": statuses,
        "format_error_rate": round(format_errors / answered, 4) if answered else None,
        "retries": len(calls) - len(by_key),
        "redactions_before_send": redactions,
        "facts": facts,
        "entities": entities,
        "fact_problems": problems,
        "relations": relations,
        "relation_types": dict(sorted(relation_types.items())),
        "relation_problems": relation_problems,
        "tokens": {name: round(value, 1) if name == "seconds" else int(value) for name, value in totals.items()},
        "per_packet": {
            packet_id: {name: round(value, 1) if name == "seconds" else round(value) for name, value in entry.items()}
            for packet_id, entry in sorted(per_packet.items())
        },
    }
