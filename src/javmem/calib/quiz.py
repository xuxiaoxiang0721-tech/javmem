"""`javmem quiz`（校准工具包，可选）：拿自己的题让模型答一遍、判一遍分。**要调模型、花钱**：用 `JAVMEM_LLM_*`
配的那个（就是抽取在用的），每次调用记进 token 账，预算照 `--budget` 按标签算；先 `--dry-run` 看要调几次。演示里不跑。

**一道题怎么走：**

1. **检索：** 用 `JAVMEM_CALLER` 这个身份问 `memory_search`，和 Bot 问的一样（主图开不开照 `JAVMEM_GRAPH_SEARCH`）。
   候选按先后放进上下文，默认 1,500 字（`--context`），放法和 `javmem regress` 量命中率的一样。要发给模型，所以和抽取
   一样挑过：private 的包里的不发；剩下的照打码模式表（`JAVMEM_REDACTION_FILE`）打一遍码，还扫得出来的不发。
2. **答：** 发给模型的是“现在是哪天”（题上写的查询时间点，没写的用今天）、问题、上下文那几条（编号、内容、日子、
   确定程度）。只能照给的条目答，不够就说不知道。回 `{answer, known}`。
3. **判**（照题库的严格写法，一题只记一种）：
   - 记忆里没有的题：说不知道算**对**；答出了内容算**硬答**。
   - 模型说不知道的：记**不知道**。
   - 数字条件这一类，先按规则比数和日子（`rules_pass`）：标准答案或者任何一种可接受的写法，写到的数、日子回答里都
     要有；一种都对不上，就算**错**，不再问判分模型。
   - 别的（包括过了规则的）问判分模型：照严格的提示词判对、错，缺了关键信息算错。
4. **出数：** 每类、总的各几道对、错、硬答、不知道；没答成的（调用失败、预算用完）、没判的（没写标准答案）另记。

**数和日子怎么比（`rules_pass`）：** 千分位、全角、万、亿、千、百万、千万、k、bp 换算以后比数值，不看正负号；写法精度
不一样的，按粗的那一边写到的那一位四舍五入以后相等也算（粗的那一边至少要两位有效数字）；错一位就对不上。日子认
年-月-日、月/日、几月几日（号），只比月和日；只写到月的只比月。时刻（15:00）不比。

**存在哪：** `--out` 目录（仓库外；新的、空的，或者上一次 `javmem quiz` 用过的）：`results.jsonl` 一题一行（题号、类别、
判成什么、谁判的、回答、上下文条目的编号、判分的理由），`summary.json` 只有数，`calls.db` 存每一次调用的回复，
按发出去的全文算键：同一个目录再跑一遍，问过的不再问，不花钱。里面有问题、上下文、回答，都是原话：放仓库外；
撤回、彻底删除管不到这个目录，用完自己删。

提示词在 `javmem/pipeline/prompts/` 下（`quiz-answer-v1`、`quiz-judge-v1`），带版本号；改了提示词或者输出结构要换版本号，
也要改这里钉着的指纹。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

from javmem.config import Settings

EXIT_OK, EXIT_PARTIAL, EXIT_INPUT = 0, 1, 2
QUESTIONS_FORMAT = "javmem-regress-questions/1"
SUMMARY_FORMAT = "javmem-quiz/1"
TAG = "javmem-quiz"
ANSWER_PROMPT_VERSION = "quiz-answer-v1"
JUDGE_PROMPT_VERSION = "quiz-judge-v1"
ANSWER_PROMPT_SHA256 = "d1fbcd2d964bfd158cb818eda3883fbbe4589cdb860eef46d753eaefd7288007"  # fingerprint()：提示词连同输出结构
JUDGE_PROMPT_SHA256 = "0429cad30d8302cb8b21efbe97f8713cd8ae9f226109fd4a418b2ae8c2882c27"
ANSWER_SCHEMA_NAME = "javmem_quiz_answer_v1"
JUDGE_SCHEMA_NAME = "javmem_quiz_judge_v1"
ANSWER_PURPOSE, JUDGE_PURPOSE = "quiz_answer", "quiz_judge"
NUMERIC_CATEGORY = "数字条件"
ABSENT_CATEGORY = "记忆里没有"
CONTEXT_CHARS = 1500
OUTPUT_ESTIMATE = 600  # --dry-run 估钱用：一次回复大约多少 token
VERDICTS = ("对", "错", "硬答", "不知道", "没答成", "没判")
ANSWER_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False, "required": ["answer", "known"],
    "properties": {"answer": {"type": "string"}, "known": {"type": "boolean"}},
}
JUDGE_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "reason"],
    "properties": {"verdict": {"type": "string", "enum": ["correct", "wrong"]}, "reason": {"type": "string"}},
}
SCHEMAS = {ANSWER_PROMPT_VERSION: ANSWER_SCHEMA, JUDGE_PROMPT_VERSION: JUDGE_SCHEMA}
Out = Callable[[str], None]


class QuizInputError(ValueError):
    """给的东西用不了。消息里不带题目、原话。"""


def add_quiz_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--questions", required=True, help=f"题的文件（{QUESTIONS_FORMAT}，要写标准答案 answer）")
    parser.add_argument("--out", required=True, help="输出目录（仓库外；新的、空的，或者上一次 javmem quiz 用过的：问过的不再问）")
    parser.add_argument("--context", type=int, default=CONTEXT_CHARS, help=f"给回答模型的上下文最多多少字，默认 {CONTEXT_CHARS}")
    parser.add_argument("--limit", type=int, help="只答前几道")
    parser.add_argument("--tag", default=TAG, help=f"记账的标签，预算按它算，默认 {TAG}")
    parser.add_argument("--budget", type=int, help="这个标签一共最多用多少 token（记账里已经有的也算）")
    parser.add_argument("--dry-run", action="store_true", help="只检索、数要调几次模型、估多少 token，不调模型、不写文件")
    parser.add_argument("--fake", action="store_true", help="用假模型，不连网，试流程用（不记账）")


# ---------- 提示词 ----------


def load_prompt(version: str) -> str:
    text = (resources.files("javmem.pipeline") / "prompts" / f"{version}.md").read_text(encoding="utf-8")
    return text.replace("\r\n", "\n")


def fingerprint(version: str) -> str:
    """提示词连同输出结构的指纹。"""
    text = load_prompt(version) + "\n" + json.dumps(SCHEMAS[version], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- 数和日子的规则 ----------

_MULT = {"千万": Decimal(10) ** 7, "百万": Decimal(10) ** 6, "亿": Decimal(10) ** 8, "万": Decimal(10) ** 4,
         "千": Decimal(10) ** 3, "k": Decimal(10) ** 3, "K": Decimal(10) ** 3, "bps": Decimal("0.01"), "bp": Decimal("0.01")}
_THOUSANDS = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+)(?![\d,])")
_FULL_DATE = re.compile(r"(?<![\d.])\d{4}\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?(?!\d)")
_YEAR_MONTH = re.compile(r"(?<![\d.])\d{4}\s*年\s*(\d{1,2})\s*月(?!\s*\d)")
_MONTH_DAY = re.compile(r"(?<![\d.])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?(?![\d万亿千])")
_SLASH_DAY = re.compile(r"(?<![\d./])(\d{1,2})/(\d{1,2})(?![\d/])")
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
# 单位：千克、千米、K 线这种不是“千”
_NUMBER = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(千万|百万|亿|万|千(?![克米瓦伏卡])|bps|bp|[kK](?![A-Za-z线]))?(?![\d.])")


@dataclass(frozen=True)
class Value:
    kind: str  # num 数；date 日子（月 × 100 + 日；只写到月的日是 0）
    value: Decimal
    unit: Decimal  # 写到的最后一位代表多少（日子是 1）
    digits: int  # 有效数字几位（日子是 0）


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return _THOUSANDS.sub(lambda match: match.group(1).replace(",", ""), text)


def values(text: str) -> list[Value]:
    """一段话里写到的数和日子（日子先认，认过的不再当数；时刻不算）。"""
    text = _plain(text)
    found: list[Value] = []

    def day(month: str, date: str | None) -> None:
        m, d = int(month), int(date) if date else 0
        if 1 <= m <= 12 and 0 <= d <= 31:
            found.append(Value("date", Decimal(m * 100 + d), Decimal(1), 0))

    for pattern, build in ((_FULL_DATE, lambda match: day(match.group(1), match.group(2))),
                           (_YEAR_MONTH, lambda match: day(match.group(1), None)),
                           (_MONTH_DAY, lambda match: day(match.group(1), match.group(2))),
                           (_SLASH_DAY, lambda match: day(match.group(1), match.group(2)))):
        for match in pattern.finditer(text):
            build(match)
        text = pattern.sub(" ", text)
    text = _TIME.sub(" ", text)
    for match in _NUMBER.finditer(text):
        written, mult = match.group(1), _MULT.get(match.group(2) or "", Decimal(1))
        try:
            number = Decimal(written)
        except InvalidOperation:
            continue
        decimals = len(written.split(".", 1)[1]) if "." in written else 0
        digits = len(written.replace(".", "").lstrip("0")) or 1
        found.append(Value("num", number * mult, Decimal(10) ** -decimals * mult, digits))
    return found


def _same(wanted: Value, got: Value) -> bool:
    if wanted.kind != got.kind:
        return False
    if wanted.kind == "date":
        month, day = divmod(int(wanted.value), 100)
        other_month, other_day = divmod(int(got.value), 100)
        return month == other_month and (day == other_day or not day or not other_day)
    if wanted.value == got.value:
        return True
    coarse, fine = (wanted, got) if wanted.unit >= got.unit else (got, wanted)
    if coarse.unit == fine.unit or coarse.digits < 2:
        return False
    rounded = (fine.value / coarse.unit).quantize(Decimal(1), rounding=ROUND_HALF_UP) * coarse.unit
    return rounded == coarse.value


def rules_pass(forms: Sequence[str], answer: str) -> bool:
    """标准答案或者任何一种可接受的写法，写到的数、日子回答里都有（不写数的那一种直接算过：交判分模型）。"""
    got = values(answer)
    for form in forms:
        wanted = values(form)
        if all(any(_same(one, other) for other in got) for one in wanted):
            return True
    return False


# ---------- 题 ----------


@dataclass(frozen=True)
class Question:
    id: str
    query: str
    category: str
    absent: bool
    answer: str | None
    accept: tuple[str, ...]
    as_of: str | None


def load_questions(path: Path) -> list[Question]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QuizInputError(f"题的文件读不了（{type(error).__name__}）") from None
    if not isinstance(data, Mapping) or data.get("format") != QUESTIONS_FORMAT or not isinstance(data.get("questions"), list):
        raise QuizInputError(f"题的文件开头要写 format: {QUESTIONS_FORMAT}，题放在 questions 里")
    out: list[Question] = []
    seen: set[str] = set()
    for number, raw in enumerate(data["questions"], 1):
        if not isinstance(raw, Mapping) or not str(raw.get("id") or "").strip() or not str(raw.get("query") or "").strip():
            raise QuizInputError(f"第 {number} 道题要有 id 和 query")
        qid = str(raw["id"]).strip()
        if qid in seen:
            raise QuizInputError(f"题号重了：{qid}")
        seen.add(qid)
        accept = raw.get("accept") or []
        if not isinstance(accept, list):
            raise QuizInputError(f"{qid}：accept 要是一串写法")
        absent = bool(raw.get("absent"))
        out.append(Question(qid, str(raw["query"]).strip(), str(raw.get("category") or (ABSENT_CATEGORY if absent else "")),
                            absent, str(raw["answer"]).strip() if raw.get("answer") not in (None, "") else None,
                            tuple(str(one) for one in accept if str(one).strip()),
                            str(raw["as_of"]).strip() if raw.get("as_of") else None))
    return out


# ---------- 检索 ----------


def _when(item: Mapping[str, object]) -> str:
    valid = item.get("valid_time") if isinstance(item.get("valid_time"), Mapping) else {}
    start = str(valid.get("from") or "") if valid else ""  # type: ignore[union-attr]
    if start:
        end = str(valid.get("to") or "")  # type: ignore[union-attr]
        return f"{start} 到 {end}" if end else f"{start} 起"
    evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
    said = next((str(one.get("said_at") or "") for one in evidence if isinstance(one, Mapping) and one.get("said_at")), "")  # type: ignore[union-attr]
    return f"{said[:10]} 说的" if said else ""


_STATUS = {"inferred": "推断", "pending": "待确认"}


def _packets_of(item: Mapping[str, object]) -> set[str]:
    """一条候选出自哪几个包：原文句看句子编号的包号，事实、关系看证据。"""
    if str(item.get("kind")) == "raw":
        return {str(item.get("id") or "").split("#", 1)[0]}
    evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
    return {str(one.get("packet_id")) for one in evidence if isinstance(one, Mapping) and one.get("packet_id")}  # type: ignore[union-attr]


@dataclass(frozen=True)
class Outbound:
    """要发给模型的条目怎么挑：private 的包里的不发；剩下的照打码模式表打一遍码，还扫得出来的不发。"""

    private: frozenset[str]
    patterns: Sequence[object]

    def shown(self, item: Mapping[str, object], text: str) -> str | None:
        from javmem.redaction import redact, scan_types

        if _packets_of(item) & self.private:
            return None
        cleaned, _ = redact(text, self.patterns)  # type: ignore[arg-type]
        return None if scan_types(cleaned, self.patterns) else cleaned  # type: ignore[arg-type]


def context_items(query: str, settings: Settings, evidence: Mapping[tuple[str, str], tuple[str, ...]], budget: int,
                  outbound: Outbound) -> tuple[list[object], int]:
    """memory_search 的候选（翻页到 6,000 字或者没有下一页），照 javmem regress 的放法放进上下文。
    返回 (放进去的, 没发的几条：private 的包里的、打完码还扫得出来的)。"""
    from javmem.query import tools
    from javmem.regress.evidence import MAX_PAGES, PAGE_CHARS, PAGE_LIMIT, Item, fill, window

    found: list[Mapping[str, object]] = []
    cursor: object = "c0"
    used = pages = 0
    while cursor and used < PAGE_CHARS and pages < MAX_PAGES:
        reply = tools.memory_search({"query": query, "limit": PAGE_LIMIT, "cursor": cursor}, settings=settings)  # type: ignore[arg-type]
        if reply.get("error"):
            raise QuizInputError(f"查询出错：{(reply.get('error') or {}).get('code', '')}")  # type: ignore[union-attr]
        for item in reply.get("items") or []:
            found.append(item)
            used += len(str(item.get("content", "")))
        cursor = reply.get("next_cursor")
        pages += 1
    items = []
    held = 0
    for item in found:
        kind, key = str(item.get("kind")), str(item.get("id"))
        notes = [note for note in (_when(item), _STATUS.get(str(item.get("status") or ""), "")) if note]
        tail = f"（{'，'.join(notes)}）" if notes else ""
        body = window(str(item.get("content") or ""), "") if kind == "raw" else str(item.get("content") or "")
        text = outbound.shown(item, f"[{key}] {body}{tail}")
        if text is None:
            held += 1
            continue
        items.append(Item(key, "raw", text, (key,)) if kind == "raw" else Item(key, "fact", text, evidence.get((kind, key), ())))
    return fill(items, budget), held


def answer_text(question: Question, today: str, items: Sequence[object]) -> str:
    lines = [f"现在是 {question.as_of or today}。", f"问题：{question.query}", "查到的条目（按相关程度排）："]
    lines += [f"{number}. {item.text}" for number, item in enumerate(items, 1)] or ["（一条都没查到）"]  # type: ignore[attr-defined]
    return "\n".join(lines)


def judge_text(question: Question, today: str, answer: str) -> str:
    lines = [f"问题：{question.query}", f"现在是 {question.as_of or today}。", f"标准答案：{question.answer}"]
    lines.append("可接受的别的写法：" + ("；".join(question.accept) if question.accept else "没有"))
    lines.append(f"要判的回答：{answer}")
    return "\n".join(lines)


# ---------- 调模型 ----------


class QuizFake:
    """--fake 的假模型：上下文里第一条当回答（没查到就说不知道），判分一律判对。不连网、不记账。"""

    name = "fake-quiz"
    cache_name = "fake-quiz@fake/json_schema"
    service_tier = ""
    max_output_tokens = 200

    def complete(self, system: str, user: str, schema: Mapping[str, object], schema_name: str):  # noqa: ANN201
        from javmem.llm import ModelReply, Usage

        if schema_name == JUDGE_SCHEMA_NAME:
            body: dict[str, object] = {"verdict": "correct", "reason": "假模型"}
        else:
            first = next((line.split(". ", 1)[1] for line in user.splitlines() if re.match(r"^1\. ", line)), "")
            body = {"answer": first or "不知道", "known": bool(first)}
        content = json.dumps(body, ensure_ascii=False)
        return ModelReply(content, "stop", Usage(len(user) // 2, len(content) // 2), self.name, 0.0, None)


def make_model(args: argparse.Namespace):  # noqa: ANN201 - OpenAIChat 或者假模型
    if args.fake:
        return QuizFake()
    from javmem.llm import LLMSettings, OpenAIChat

    return OpenAIChat(LLMSettings.from_env())


def _key(purpose: str, model: object, version: str, user: str) -> str:
    text = "\n".join(("javmem-quiz", purpose, str(getattr(model, "cache_name", "")), version, fingerprint(version), user))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse(content: str, schema: Mapping[str, object]) -> dict[str, object]:
    from javmem.pipeline.extract import FormatError

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        raise FormatError("不是 JSON") from None
    properties = schema["properties"]  # type: ignore[index]
    if not isinstance(parsed, dict) or set(parsed) != set(properties):  # type: ignore[arg-type]
        raise FormatError("字段不对")
    for name, rule in properties.items():  # type: ignore[union-attr]
        value = parsed[name]
        if rule["type"] == "string" and not isinstance(value, str):
            raise FormatError(f"{name} 要是字符串")
        if rule["type"] == "boolean" and not isinstance(value, bool):
            raise FormatError(f"{name} 要是 true 或 false")
        if "enum" in rule and value not in rule["enum"]:
            raise FormatError(f"{name} 不在可选的里面")
    return parsed


class Calls:
    """--out 目录里的 calls.db：一次调用一行，按发出去的全文算键。"""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, purpose TEXT NOT NULL, model TEXT NOT NULL,"
                          " prompt_version TEXT NOT NULL, parsed TEXT NOT NULL, input_tokens INTEGER NOT NULL,"
                          " output_tokens INTEGER NOT NULL, created_at TEXT NOT NULL)")
        self.conn.commit()

    def get(self, key: str) -> dict[str, object] | None:
        row = self.conn.execute("SELECT parsed FROM calls WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, key: str, purpose: str, model: str, version: str, parsed: Mapping[str, object], usage: object) -> None:
        self.conn.execute("INSERT OR REPLACE INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (key, purpose, model, version, json.dumps(parsed, ensure_ascii=False),
                           getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0),
                           datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _runner(purpose: str, version: str, schema_name: str, model: object, ledger: object, options: object, calls: Calls):  # noqa: ANN202
    from javmem.pipeline.extract import _Run

    class _QuizRun(_Run):
        def parse(self, content: str, request: object) -> dict[str, object]:
            return _parse(content, SCHEMAS[version])

        def save(self, window: object, request: object, key: str, reply: object, parsed: dict[str, object], usage: object) -> None:
            calls.put(key, purpose, str(getattr(model, "cache_name", "")), version, parsed, usage)

        def record_call(self, row: Mapping[str, object]) -> None:  # 调用记在 token 账里就够了
            return None

    _QuizRun.purpose, _QuizRun.schema, _QuizRun.schema_name = purpose, SCHEMAS[version], schema_name
    return _QuizRun(model, None, ledger, options, None, system=load_prompt(version))  # type: ignore[arg-type]


@dataclass
class Asked:
    """一次调用的结果：parsed 是校验过的回复；cached 是从 calls.db 取的；failed 是没拿到（detail 说为什么）。"""

    parsed: dict[str, object] | None = None
    cached: bool = False
    detail: str = ""


def _ask(run: object, calls: Calls, key: str, qid: str, user: str) -> Asked:
    from javmem.pipeline.extract import DONE

    found = calls.get(key)
    if found is not None:
        return Asked(found, cached=True)
    outcome = run.work(SimpleNamespace(window_id=qid), SimpleNamespace(text=user), key)  # type: ignore[attr-defined]
    if outcome.status != DONE:
        return Asked(detail=outcome.detail or outcome.status)
    return Asked(calls.get(key))


# ---------- 一整遍 ----------


def _judge_one(question: Question, answer: Mapping[str, object], ask_judge: Callable[[str], Asked], today: str) -> dict[str, object]:
    text, known = str(answer.get("answer") or ""), bool(answer.get("known"))
    if question.absent:
        return {"verdict": "硬答" if known else "对", "by": "记忆里没有"}
    if not known:
        return {"verdict": "不知道", "by": "回答"}
    if question.category == NUMERIC_CATEGORY and not rules_pass([question.answer or "", *question.accept], text):
        return {"verdict": "错", "by": "规则", "reason": "标准答案里的数、日子回答里没有"}
    judged = ask_judge(judge_text(question, today, text))
    if judged.parsed is None:
        return {"verdict": "没答成", "by": "判分模型", "reason": judged.detail}
    return {"verdict": "对" if judged.parsed["verdict"] == "correct" else "错", "by": "判分模型",
            "reason": str(judged.parsed.get("reason") or ""), "cached": judged.cached}


def _summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    def counts(group: Sequence[Mapping[str, object]]) -> dict[str, int]:
        out = {"questions": len(group)}
        out.update({verdict: sum(1 for row in group if row["verdict"] == verdict) for verdict in VERDICTS})
        return out

    categories = sorted({str(row["category"]) for row in rows})
    return {"format": SUMMARY_FORMAT, "total": counts(rows),
            "by_category": {name: counts([row for row in rows if row["category"] == name]) for name in categories}}


def _line(name: str, found: Mapping[str, int]) -> str:
    wrong = found["错"] + found["硬答"]
    return (f"{name} {found['questions']} 道：对 {found['对']}、错 {wrong}（其中硬答 {found['硬答']}）、不知道 {found['不知道']}"
            + (f"、没答成 {found['没答成']}" if found["没答成"] else "") + (f"、没判 {found['没判']}" if found["没判"] else ""))


def _check_out(out_dir: Path) -> None:
    from javmem.scoring.run import inside_repo

    if inside_repo(out_dir):
        raise QuizInputError("输出目录在仓库里：里面有原话，要放仓库外")
    if out_dir.exists() and (not out_dir.is_dir() or (any(out_dir.iterdir()) and not (out_dir / "calls.db").is_file())):
        raise QuizInputError(f"{out_dir} 不是新的、空的，也不是 javmem quiz 用过的目录")


def run_quiz(args: argparse.Namespace, settings: Settings, out: Out = print) -> int:
    from javmem.access import UnknownIdentity, load_identity
    from javmem.ledger import connect_readonly
    from javmem.pipeline.extract import ExtractOptions
    from javmem.redaction import load_patterns
    from javmem.regress.evidence import evidence_of

    try:
        out_dir = Path(args.out)
        _check_out(out_dir)
        questions = load_questions(Path(args.questions))
        if args.limit:
            questions = questions[: args.limit]
        try:
            identity = load_identity(settings)
        except UnknownIdentity:
            raise QuizInputError("要设 JAVMEM_CALLER（身份表里的一个身份）：照这个身份能看到的检索") from None
        db = settings.library(identity.env).db_path if settings.data_dir is not None else None
        if db is None or not db.is_file():
            raise QuizInputError(f"{identity.env} 库还没有账本（或者没设 JAVMEM_DATA_DIR）")
        conn = connect_readonly(db)
        try:
            evidence = evidence_of(conn)
            private = frozenset(str(row[0]) for row in conn.execute(
                "SELECT packet_id FROM packets WHERE state = 'current' AND privacy = 'private'"))
        finally:
            conn.close()
        outbound = Outbound(private, load_patterns(settings.redaction_file))
        today = datetime.now().astimezone().date().isoformat()
        prepared = [(question, *context_items(question.query, settings, evidence, args.context, outbound)) for question in questions]
    except QuizInputError as error:
        out(str(error))
        return EXIT_INPUT
    if args.dry_run:  # 不调模型：模型的设置可以还没配
        texts = [answer_text(question, today, items) for question, items, _ in prepared]
        chars = sum(len(text) + len(load_prompt(ANSWER_PROMPT_VERSION)) for text in texts)
        judged = sum(1 for question, _, _ in prepared if not question.absent and question.answer)
        held = sum(count for _, _, count in prepared)
        estimate = int(chars * 0.8) + OUTPUT_ESTIMATE * (len(texts) + judged) + judged * 600
        out(f"要调答题 {len(texts)} 次、判分最多 {judged} 次（规则判错、说不知道的、记忆里没有的不判），大约 {estimate:,} token；"
            f"查到的条目里 {held} 条不发（private 的包里的、打完码还扫得出来的）；没调模型、没写文件")
        return EXIT_OK
    from javmem.usage import TokenLedger

    model = make_model(args)
    ledger = None
    if not args.fake:
        import os

        path = os.environ.get("JAVMEM_TOKEN_LEDGER", "").strip()
        ledger = TokenLedger(Path(path) if path else settings.require_data_dir() / "logs" / "token-ledger.csv")
    out_dir.mkdir(parents=True, exist_ok=True)
    calls = Calls(out_dir / "calls.db")
    options = replace(ExtractOptions(concurrency=1), tag=args.tag, budget=args.budget)
    answer_run = _runner(ANSWER_PURPOSE, ANSWER_PROMPT_VERSION, ANSWER_SCHEMA_NAME, model, ledger, options, calls)
    judge_run = _runner(JUDGE_PURPOSE, JUDGE_PROMPT_VERSION, JUDGE_SCHEMA_NAME, model, ledger, options, calls)
    judge_run.budget = answer_run.budget  # 答题、判分花的是同一份预算
    rows: list[dict[str, object]] = []
    reused = 0
    try:
        for question, items, held in prepared:
            row: dict[str, object] = {"id": question.id, "category": question.category or "没写类别", "absent": question.absent,
                                      "context": [item.key for item in items], "held": held}  # type: ignore[attr-defined]
            if not question.absent and not question.answer:
                rows.append({**row, "verdict": "没判", "by": "没写标准答案"})
                continue
            user = answer_text(question, today, items)
            asked = _ask(answer_run, calls, _key(ANSWER_PURPOSE, model, ANSWER_PROMPT_VERSION, user), question.id, user)
            reused += asked.cached
            if asked.parsed is None:
                rows.append({**row, "verdict": "没答成", "by": "回答模型", "reason": asked.detail})
                if answer_run.stop.is_set():
                    break
                continue
            row.update(answer=asked.parsed.get("answer"), known=asked.parsed.get("known"))

            def ask_judge(text: str, qid: str = question.id) -> Asked:
                return _ask(judge_run, calls, _key(JUDGE_PURPOSE, model, JUDGE_PROMPT_VERSION, text), qid, text)

            judged = _judge_one(question, asked.parsed, ask_judge, today)
            reused += bool(judged.pop("cached", False))
            rows.append({**row, **judged})
            if judge_run.stop.is_set():
                break
    finally:
        calls.close()
    (out_dir / "results.jsonl").write_bytes("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8"))
    summary = _summary(rows)
    summary.update(model=str(getattr(model, "cache_name", "")), prompts={ANSWER_PROMPT_VERSION: fingerprint(ANSWER_PROMPT_VERSION),
                   JUDGE_PROMPT_VERSION: fingerprint(JUDGE_PROMPT_VERSION)}, context_chars=args.context, reused=reused)
    (out_dir / "summary.json").write_bytes((json.dumps(summary, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    out(_line(f"答题判分（模型 {summary['model']}）", summary["total"]))  # type: ignore[arg-type]
    for name, found in summary["by_category"].items():  # type: ignore[union-attr]
        out("  " + _line(name, found))
    out(f"查到的条目里 {sum(count for _, _, count in prepared)} 条没发（private 的包里的、打完码还扫得出来的）；"
        f"问过的从 {out_dir / 'calls.db'} 取了 {reused} 次；每题的回答、理由在 {out_dir / 'results.jsonl'}（有原话）")
    stopped = answer_run.stopped or judge_run.stopped
    if stopped or len(rows) < len(prepared):
        out(f"没答完：{'预算用完了' if stopped == 'budget' else stopped or '停了'}（答了 {len(rows)} / {len(prepared)} 道）")
        return EXIT_PARTIAL
    return EXIT_OK
