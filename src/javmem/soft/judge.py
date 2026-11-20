"""软变化判定的那一次调用（J4.5）：一道题（javmem.soft.cases）写成发给模型的文字，回来的结论校验好，存进 `soft.db`。

**发什么：** 提示词 prompts/soft-v3.md 当系统提示词（固定的，可以缓存）。每道题：

    【包日期】2026-11-07（要判的那几行是 2026-11-07 10:30 说的）
    【旧的】
    O1 关系：林工 负责 东湖那批订单。生效；2026-10-12 起；最后一次说到 2026-11-02。
    【前后文】
    T1 [2026-11-07 10:28] 我：……
    T2 [2026-11-07 10:30] desk-bot〔引用〕：……
    【要判的】T2

原文里的〔〕换成［］、换行换成空格（和 L3 一样，原文伪造不了我们的标注）；每一行、旧的那几句、说话人、包日期都按
打码模式表打一遍码，写出来的每一行再整行扫一遍（拼成“说话人：原文”可能就是一个验证码），还扫得出密码、密钥这类的，
这道题不发（`leftover`）；private 的不发；太长的不发（`too_big`）。

**回什么：** 结构化输出（SCHEMA），每条旧的一项：target、about、verdict（A 到 F）、condition、effective_at、
effective_precision、new_value、sure、finance、quote、reason。回来以后自己再校验一遍：不是 JSON、字段不对、旧的漏了、
多了、重复了，算格式错误，重试一次。另外算三样记在每一项上：`quote_ok`（引文在要判的那几行里找得到，比法和 L4 的 G1
一样）、`consistent`（说没说到它却判了暂停、结束、换新的，算说不通）、`date_ok`（effective_at 的写法认得出；认不出的
换成 null）。

**键和存法：** 键是“提示词版本和指纹、模型（带 effort）、发出去的文字、第几遍（--repeat）”，同样的输入不再调。存在
数据目录的 `soft.db`（不进账本，删了重跑）：`verdicts` 一个键一行（发出去的文字、原样的回复、校验过的结论、用量），
`calls` 每次调用一行（只有状态和 token），`runs` 跑一次一行。

**怎么调：** 照 javmem.pipeline.extract 的那一套（L3、L4 复核都用它）：同时最多 2 个，限流退避，预算按标签算，每次调用
记账（purpose `soft_change`），超时只再试一次，格式错重试一次；截断、拒答、太长、格式错过的默认不再调（--retry-failed
才重调）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from javmem.llm import ModelReply, Usage
from javmem.pipeline.extract import (
    CACHED,
    DONE,
    FAILED,
    NOT_STARTED,
    ExtractOptions,
    FormatError,
    Model,
    WindowOutcome,
    _now,
    _Run,
)
from javmem.pipeline.render import escape_raw
from javmem.pipeline.verify import fold, fold_quote
from javmem.redaction import RedactionPattern, redact, scan_types
from javmem.soft import rules
from javmem.soft.cases import Case
from javmem.timeutil import parse_aware
from javmem.usage import TokenLedger

if os.name == "nt":
    import msvcrt
else:
    import fcntl

PROMPT_VERSION = "soft-v3"  # v2：看不出是哪一条的暂停都判 B（主人 11-17 定的）；v3：经手的人亲口宣布的算数，sure 照规矩判得出来就写 true（只对着调试 10 题调的）
PROMPT_SHA256 = "91b4ace8361ba819ef3c0214657847563e02ef99463e094a824d9b7cdc1ffe6b"  # prompt_fingerprint() 算的
SCHEMA_NAME = "javmem_soft_v1"
PURPOSE = "soft_change"
VERDICTS_FORMAT = "javmem-soft-verdicts/1"
STORE_FILE = "soft.db"
LOCK_FILE = "soft.lock"
UNSENT = "unsent"  # 不发的：private、打码没打干净、太长
PRECISIONS = ("minute", "day", "month", "quarter", "year", "unknown")
PERMANENT_FAILURES = frozenset({"length", "refusal", "too_long", "content_rejected", "format_error"})


def _nullable(kind: str, **extra: object) -> dict[str, object]:
    return {"anyOf": [{"type": kind, **extra}, {"type": "null"}]}


_ITEM_FIELDS = (
    "target", "about", "verdict", "condition", "effective_at", "effective_precision", "new_value", "sure", "finance",
    "quote", "reason",
)
SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "about": {"type": "boolean"},
                    "verdict": {"type": "string", "enum": list(rules.VERDICTS)},
                    "condition": _nullable("string"),
                    "effective_at": _nullable("string"),
                    "effective_precision": {"type": "string", "enum": list(PRECISIONS)},
                    "new_value": _nullable("string"),
                    "sure": {"type": "boolean"},
                    "finance": {"type": "boolean"},
                    "quote": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": list(_ITEM_FIELDS),
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def load_prompt() -> str:
    return resources.files("javmem.pipeline").joinpath("prompts", f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")


def prompt_fingerprint() -> str:
    """提示词连同输出结构的指纹：改了任何一样都要换版本号、更新 PROMPT_SHA256（测试会提醒）。"""
    text = load_prompt() + "\n" + json.dumps(SCHEMA, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- 发什么 ----------


@dataclass(frozen=True)
class SoftRequest:
    """一道题要发的文字。shown：每个 T 编号写出来的样子（打过码、［］换过），比引文用。"""

    case_id: str
    text: str
    olds: tuple[str, ...]
    targets: tuple[str, ...]
    shown: Mapping[str, str]
    said_at: str
    private: bool = False
    leftover: tuple[str, ...] = ()
    redactions: Mapping[str, int] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _clock(value: str) -> str:
    moment = parse_aware(value)
    return moment.strftime("%Y-%m-%d %H:%M") if moment is not None else value


def shown_text(text: str, patterns: Sequence[RedactionPattern] = ()) -> str:
    """一行原文在题里写出来的样子（〔〕换成［］、换行换成空格，再打码），和 render_case 里每一行一样。撤回、彻底删除时
    拿撤回句子的这个样子去比 soft.db 里存着的题（javmem.withdraw），tests/test_withdraw_soft.py 钉着两边一样。"""
    return redact(escape_raw(text), patterns)[0]


def render_case(case: Case, patterns: Sequence[RedactionPattern] = ()) -> SoftRequest:
    """一道题写成发给模型的文字。打完码还扫得出来的记在 leftover（这道题不发）。"""
    counts: dict[str, int] = {}
    leftover: set[str] = set()

    def clean(text: str) -> str:
        shown, found = redact(escape_raw(text), patterns)
        for kind, count in found.items():
            counts[kind] = counts.get(kind, 0) + count
        leftover.update(scan_types(shown, patterns))
        return shown

    said = parse_aware(case.said_at)
    day = clean(case.packet_date) if case.packet_date else (said.date().isoformat() if said is not None else case.said_at[:10])
    out = [f"【包日期】{day}（要判的那几行是 {_clock(case.said_at)} 说的）", "【旧的】"]
    for item in case.old:
        pieces = [piece for piece in (
            clean(item.state) if item.state else None,
            f"{clean(item.since)} 起" if item.since else None,
            f"最后一次说到 {clean(item.last_said)}" if item.last_said else None,
        ) if piece]
        kind = "关系" if item.kind == "relation" else "事实"
        body = clean(item.text).rstrip("。")
        out.append(f"{item.ref} {kind}：{body}。" + ("；".join(pieces) + "。" if pieces else ""))
    out.append("【前后文】")
    shown: dict[str, str] = {}
    for line in case.lines:
        speaker = "我" if line.role == "owner" else clean(line.speaker)
        text = clean(line.text)
        shown[line.ref] = text
        out.append(f"{line.ref} [{_clock(line.at)}] {speaker}{'〔引用〕' if line.quoted else ''}：{text}")
    out.append(f"【要判的】{'、'.join(case.target)}")
    # 照 L3（render_window）：写出来的每一行再扫一遍。每一段单独扫不出来的，拼成“说话人：原文”可能就是一个验证码
    leftover.update(kind for row in out for kind in scan_types(row, patterns))
    return SoftRequest(
        case.id, "\n".join(out) + "\n", tuple(item.ref for item in case.old), case.target, shown, case.said_at,
        case.private, tuple(sorted(leftover)), dict(sorted(counts.items())),
    )


def _key_of(request_sha: str, model: str, repeat: int = 0) -> str:
    parts: dict[str, object] = {"prompt": PROMPT_VERSION, "prompt_sha": PROMPT_SHA256, "model": model, "request": request_sha}
    if repeat:
        parts["repeat"] = repeat
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def case_key(request: SoftRequest, model: str, *, repeat: int = 0) -> str:
    """结论的键：提示词版本和指纹、模型（带 effort）、发出去的文字，第几遍（0 是平常的那一遍）。"""
    return _key_of(request.sha256, model, repeat)


def store_signature(path: Path, model: str) -> str | None:
    """soft.db 里这一版提示词、这个模型的结论是什么样子（几行、最后一行建的时间）。L5 写账最后一步拿它比上一次看过的：
    没变就不再出一遍题找结论（J4.4）。没有结论是 None。"""
    if not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=30)
    try:
        row = conn.execute(
            "SELECT count(*), max(created_at) FROM verdicts WHERE prompt_version = ? AND model = ?", (PROMPT_VERSION, model)
        ).fetchone()
    except sqlite3.OperationalError:  # 还没判过一道（表都没有）
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    return f"{PROMPT_VERSION}|{model}|{row[0]}|{row[1]}"


def stored_items(path: Path, case_id: str, model: str) -> list[dict[str, object]] | None:
    """L5 照结论执行时找结论（J4.4 第 4 段）：这道题平常那一遍（不是 --repeat 的）、这一版提示词、这个模型存下来的，
    取最新的那一份的 items（parse_reply 校验过的样子）。没有是 None。按题号找，不按发出去的文字：执行完旧的状态变了，
    再出题文字就变了，按文字的键会找不到、又要重新付钱判。只读，不建表。"""
    if not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=30)
    try:
        rows = conn.execute(
            "SELECT key, request_sha, parsed FROM verdicts WHERE case_id = ? AND prompt_version = ? AND model = ?"
            " ORDER BY created_at DESC, key",
            (case_id, PROMPT_VERSION, model),
        ).fetchall()
    except sqlite3.OperationalError:  # 还没判过一道（表都没有）
        return None
    finally:
        conn.close()
    for key, request_sha, parsed in rows:
        if key != _key_of(str(request_sha), model):
            continue  # --repeat 的那几遍不算
        items = json.loads(parsed).get("items")
        return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else None
    return None


# ---------- 回什么 ----------

_RANK = {"minute": 0, "day": 1, "month": 2, "quarter": 3, "year": 4}
_DATE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?")
_SHAPES = (
    (re.compile(r"\d{4}"), "year"),
    (re.compile(r"\d{4}-Q[1-4]"), "quarter"),
    (re.compile(r"\d{4}-(0[1-9]|1[0-2])"), "month"),
)


def normal_date(value: str, said_at: str) -> tuple[str, str] | None:
    """effective_at 换成认得的写法和它的精度；认不出返回 None。带时刻没写时区的，用要判那几行的时区。"""
    value = value.strip()
    for pattern, precision in _SHAPES:
        if pattern.fullmatch(value):
            return value, precision
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
        return value, "day"
    match = _DATE_TIME.fullmatch(value)
    if match is None:
        return None
    zone = match.group(3)
    if zone is None:
        said = parse_aware(said_at)
        zone = said.strftime("%z") if said is not None else "+00:00"
        zone = zone if ":" in zone else f"{zone[:3]}:{zone[3:]}"
    elif zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"  # +0800 写成 +08:00
    text = f"{match.group(1)}T{match.group(2)}{zone}"
    if parse_aware(text) is None:
        return None
    return text, "minute"


def _check_item(raw: object, where: str) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != set(_ITEM_FIELDS):
        raise FormatError(f"{where} 的字段不对")
    checks: dict[str, Callable[[object], bool]] = {
        "target": lambda v: isinstance(v, str),
        "about": lambda v: isinstance(v, bool),
        "verdict": lambda v: v in rules.VERDICTS,
        "condition": lambda v: v is None or isinstance(v, str),
        "effective_at": lambda v: v is None or isinstance(v, str),
        "effective_precision": lambda v: v in PRECISIONS,
        "new_value": lambda v: v is None or isinstance(v, str),
        "sure": lambda v: isinstance(v, bool),
        "finance": lambda v: isinstance(v, bool),
        "quote": lambda v: isinstance(v, str),
        "reason": lambda v: isinstance(v, str),
    }
    for name, ok in checks.items():
        if not ok(raw[name]):
            raise FormatError(f"{where}.{name} 不对")
    return dict(raw)


def quote_found(quote: str, request: SoftRequest) -> bool:
    """引文在要判的那几行里找得到：比法和 L4 的 G1 一样（空白、全半角、两头的标点不算差别），空的不算。"""
    folded = fold_quote(quote)
    return bool(folded) and any(folded in fold(request.shown.get(ref, "")) for ref in request.targets)


def parse_reply(content: str, request: SoftRequest) -> dict[str, object]:
    """校验模型的回复，不对抛 FormatError。返回 {"items": [...]}，每项照旧的的先后，加上 quote_ok、consistent、date_ok。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        raise FormatError("不是 JSON") from None
    if not isinstance(data, dict) or set(data) != {"items"} or not isinstance(data["items"], list):
        raise FormatError("要写成 {items: [...]}")
    items = [_check_item(raw, f"items[{n}]") for n, raw in enumerate(data["items"])]
    targets = [str(item["target"]) for item in items]  # 上面查过是字符串
    if sorted(targets) != sorted(request.olds) or len(set(targets)) != len(targets):
        raise FormatError("每条旧的要回一项，不漏、不多、不重复")
    order = {ref: n for n, ref in enumerate(request.olds)}
    out = []
    for item in sorted(items, key=lambda one: order[str(one["target"])]):
        at = item["effective_at"]
        normal = normal_date(str(at), request.said_at) if at is not None else None
        date_ok = at is None or normal is not None
        if normal is None:
            item["effective_at"], item["effective_precision"] = None, "unknown"
        else:
            item["effective_at"] = normal[0]
            claimed = str(item["effective_precision"])
            if claimed == "unknown" or _RANK[claimed] < _RANK[normal[1]]:
                item["effective_precision"] = normal[1]  # 精度没写、或者比写法还细的，照写法算
        item["quote_ok"] = quote_found(str(item["quote"]), request)
        item["consistent"] = item["about"] is True or item["verdict"] not in ("B", "C", "D")
        item["date_ok"] = date_ok
        out.append(item)
    return {"items": out}


# ---------- 存 ----------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS verdicts (
    key            TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    request_sha    TEXT NOT NULL,
    request        TEXT NOT NULL,
    output         TEXT NOT NULL,
    parsed         TEXT NOT NULL,
    response_model TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0,
    seconds        REAL NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    key            TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    status         TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '',
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0,
    seconds        REAL NOT NULL DEFAULT 0,
    at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_by_key ON calls (key, id);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    options     TEXT NOT NULL,
    summary     TEXT
);
"""


class StoreBusy(Exception):
    """这个 soft.db 已经有一个判定在跑：两个一起跑，并发就翻倍了，预算也各算各的。"""


@dataclass(frozen=True)
class StoredVerdict:
    key: str
    case_id: str
    parsed: dict[str, object]
    response_model: str | None
    usage: Usage
    created_at: str


class SoftStore:
    """数据目录里的 soft.db。几个线程可以一起写，一把锁管着一个连接。exclusive：要调模型的这样打开，同一时间只能有一个。"""

    def __init__(self, directory: Path, *, exclusive: bool = False) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._handle = None
        if exclusive:
            handle = open(directory / LOCK_FILE, "a+b")  # noqa: SIM115
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise StoreBusy("这个 soft.db 已经有一个判定在跑") from None
            self._handle = handle
        self.path = directory / STORE_FILE
        self._conn = sqlite3.connect(self.path, autocommit=True, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
        if self._handle is not None:
            if os.name == "nt":
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> SoftStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def save_verdict(
        self, *, key: str, request: SoftRequest, model: str, reply: ModelReply, parsed: Mapping[str, object], usage: Usage
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO verdicts (key, case_id, prompt_version, model, request_sha, request, output, parsed, response_model,"
                " input_tokens, output_tokens, reasoning_tokens, cached_tokens, seconds, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET output = excluded.output, parsed = excluded.parsed,"
                " response_model = excluded.response_model, created_at = excluded.created_at",
                (
                    key, request.case_id, PROMPT_VERSION, model, request.sha256, request.text, reply.content,
                    json.dumps(parsed, ensure_ascii=False, sort_keys=True), reply.model, usage.input_tokens,
                    usage.output_tokens, usage.reasoning_tokens, usage.cached_tokens, round(reply.seconds, 3), _now(),
                ),
            )

    # ---- 撤回、彻底删除用（javmem.withdraw，U3）----

    def verdict_texts(self) -> list[tuple[str, str, str]]:
        """每条判定的 (键, 发出去的题, 原样的回复)：撤回时拿撤回句子的写法来比。"""
        with self._lock:
            return [(str(row["key"]), str(row["request"]), str(row["output"]))
                    for row in self._conn.execute("SELECT key, request, output FROM verdicts")]

    def delete_verdicts(self, keys: Sequence[str]) -> int:
        """删这几条判定（删的时候就把内容抹成 0），返回删了几条。调用记录（calls，只有状态和 token）留着。"""
        deleted = 0
        with self._lock:
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute("BEGIN")
            try:
                for start in range(0, len(keys), 500):
                    chunk = list(keys[start : start + 500])
                    marks = ", ".join("?" for _ in chunk)
                    deleted += self._conn.execute(f"DELETE FROM verdicts WHERE key IN ({marks})", chunk).rowcount  # noqa: S608
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return deleted

    def scrub(self) -> bool:
        """彻底删除以后：整理文件（VACUUM）、把日志写回去截掉，删掉的字不留在空页、日志里。有人正读着、做不完的返回 False。"""
        with self._lock:
            try:
                self._conn.execute("PRAGMA secure_delete = ON")
                self._conn.execute("VACUUM")
                busy = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.OperationalError:
                return False
        return busy is None or not busy[0]

    def verdict(self, key: str) -> StoredVerdict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM verdicts WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        usage = Usage(row["input_tokens"], row["output_tokens"], row["reasoning_tokens"], row["cached_tokens"])
        return StoredVerdict(row["key"], row["case_id"], json.loads(row["parsed"]), row["response_model"], usage, row["created_at"])

    def record_call(self, row: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls (run_id, case_id, key, attempt, status, detail, input_tokens, output_tokens,"
                " reasoning_tokens, cached_tokens, seconds, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["run_id"], row["window_id"], row["cache_key"], row["attempt"], row["status"], row.get("detail", ""),
                    row.get("input_tokens", 0), row.get("output_tokens", 0), row.get("reasoning_tokens", 0),
                    row.get("cached_tokens", 0), row.get("seconds", 0.0), row["at"],
                ),
            )

    def last_failure(self, key: str) -> str | None:
        """这个键最后一次调用要是截断、拒答、太长、格式错这类重调也没用的，返回它；不然 None。"""
        with self._lock:
            row = self._conn.execute("SELECT status FROM calls WHERE key = ? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        if row is None:
            return None
        status = str(row["status"]).removesuffix("_estimated")
        return status if status in PERMANENT_FAILURES else None

    def calls(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM calls ORDER BY id").fetchall()

    def start_run(self, run_id: str, options: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, started_at, options) VALUES (?, ?, ?)",
                (run_id, _now(), json.dumps(options, ensure_ascii=False, sort_keys=True)),
            )

    def finish_run(self, run_id: str, summary: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?",
                (_now(), json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id),
            )


# ---------- 一批怎么调 ----------


@dataclass(frozen=True)
class _CaseRef:
    """extract._Run 认的“窗口”：只用它的编号。"""

    window_id: str


class _SoftRun(_Run):
    purpose = PURPOSE
    schema = SCHEMA
    schema_name = SCHEMA_NAME

    def parse(self, content: str, request: object) -> dict[str, object]:
        assert isinstance(request, SoftRequest)
        return parse_reply(content, request)

    def save(self, window: object, request: object, key: str, reply: ModelReply, parsed: dict[str, object], usage: Usage) -> None:
        assert isinstance(request, SoftRequest)
        self.store.save_verdict(key=key, request=request, model=self.model.cache_name, reply=reply, parsed=parsed, usage=usage)  # type: ignore[attr-defined]

    def record_call(self, row: Mapping[str, object]) -> None:
        self.store.record_call(row)  # type: ignore[attr-defined]


@dataclass
class JudgeResult:
    run_id: str
    outcomes: dict[str, WindowOutcome]  # 题号 → 这一次怎么样了
    requests: dict[str, SoftRequest]
    keys: dict[str, str]
    stopped: str | None

    def count(self, status: str) -> int:
        return sum(1 for outcome in self.outcomes.values() if outcome.status == status)

    def summary(self) -> dict[str, object]:
        statuses: dict[str, int] = {}
        for outcome in self.outcomes.values():
            statuses[outcome.status] = statuses.get(outcome.status, 0) + 1
        usage = Usage()
        for outcome in self.outcomes.values():
            usage = usage + outcome.usage
        return {"cases": len(self.outcomes), "statuses": dict(sorted(statuses.items())), "stopped": self.stopped, **usage.as_dict()}


def plan(cases: Sequence[Case], model_name: str, store: SoftStore, patterns: Sequence[RedactionPattern], *,
         repeat: int = 0, max_input_chars: int = 60000, retry_failed: bool = False) -> dict[str, int]:
    """不调模型，数一数：几道题、存过的、不发的、以前失败过的、要调的、一共多少字。"""
    counts = {"cases": len(cases), "cached": 0, "unsent": 0, "failed_before": 0, "to_call": 0, "chars": 0}
    for case in cases:
        request = render_case(case, patterns)
        key = case_key(request, model_name, repeat=repeat)
        if request.private or request.leftover or len(request.text) > max_input_chars:
            counts["unsent"] += 1
        elif store.verdict(key) is not None:
            counts["cached"] += 1
        elif not retry_failed and store.last_failure(key) is not None:
            counts["failed_before"] += 1
        else:
            counts["to_call"] += 1
            counts["chars"] += len(request.text)
    return counts


def run_judge(
    cases: Sequence[Case],
    model: Model,
    store: SoftStore,
    *,
    patterns: Sequence[RedactionPattern] = (),
    ledger: TokenLedger | None = None,
    options: ExtractOptions | None = None,
    log: Callable[[str], None] | None = None,
    repeat: int = 0,
) -> JudgeResult:
    """一批题：存过的直接用，不发的、以前失败过的跳过，剩下的调模型。"""
    options = options or ExtractOptions(tag="javmem-soft")
    run = _SoftRun(model, store, ledger, options, log, system=load_prompt(), prompt=PROMPT_VERSION)  # type: ignore[arg-type]
    store.start_run(run.run_id, {"purpose": PURPOSE, "prompt": PROMPT_VERSION, "model": model.cache_name, "repeat": repeat,
                                 **options.describe()})
    outcomes: dict[str, WindowOutcome] = {}
    requests: dict[str, SoftRequest] = {}
    keys: dict[str, str] = {}
    todo: list[tuple[_CaseRef, SoftRequest, str]] = []
    for case in cases:
        request = render_case(case, patterns)
        key = case_key(request, model.cache_name, repeat=repeat)
        requests[case.id], keys[case.id] = request, key
        if request.private:
            outcomes[case.id] = WindowOutcome(case.id, key, UNSENT, detail="private")
        elif request.leftover:
            outcomes[case.id] = WindowOutcome(case.id, key, UNSENT, detail="leftover")
        elif len(request.text) > options.max_input_chars:
            outcomes[case.id] = WindowOutcome(case.id, key, UNSENT, detail="too_big")
        elif store.verdict(key) is not None:
            outcomes[case.id] = WindowOutcome(case.id, key, CACHED)
        elif not options.retry_failed and (failure := store.last_failure(key)) is not None:
            outcomes[case.id] = WindowOutcome(case.id, key, FAILED, detail=f"{failure}（以前失败过，--retry-failed 才重调）")
        else:
            todo.append((_CaseRef(case.id), request, key))
    if options.limit is not None:
        for ref, _, key in todo[options.limit:]:
            outcomes[ref.window_id] = WindowOutcome(ref.window_id, key, NOT_STARTED, detail="limit")
        todo = todo[: options.limit]
    pool = ThreadPoolExecutor(max_workers=max(1, options.concurrency))
    try:
        futures = [pool.submit(run.work, ref, request, key) for ref, request, key in todo]  # type: ignore[arg-type]
        for future in futures:
            outcome = future.result()
            outcomes[outcome.window_id] = outcome
            run.progress(outcome, len(todo))
    except BaseException:  # Ctrl+C 这类：排着队的不再开始，正在调的停在下一次之前（和 L3 一样）
        run.halt("interrupted")
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=True)
    result = JudgeResult(run.run_id, {case.id: outcomes[case.id] for case in cases}, requests, keys, run.stopped)
    store.finish_run(run.run_id, result.summary())
    return result


# ---------- 结论写出去 ----------


def verdict_records(cases: Sequence[Case], result: JudgeResult, store: SoftStore) -> list[dict[str, object]]:
    """每道题一项：键、这一次的状态、每条旧的结论和 L5 的动作。没有结论的（不发、失败、没开始），items 是空的，
    L5 照“还没判”办（rules.not_judged）。"""
    out = []
    for case in cases:
        outcome = result.outcomes[case.id]
        stored = store.verdict(result.keys[case.id])
        items = []
        if stored is not None:
            for item in stored.parsed.get("items", []):  # type: ignore[union-attr]
                items.append({**item, "action": rules.action(item, said_at=case.said_at).as_dict()})
        out.append({
            "case": case.id,
            "key": result.keys[case.id],
            "status": outcome.status if stored is None else (DONE if outcome.status == DONE else CACHED),
            "detail": outcome.detail,
            "response_model": stored.response_model if stored is not None else None,
            "items": items,
            "not_judged": None if stored is not None else rules.not_judged().as_dict(),
        })
    return out


def dump_verdicts(records: Sequence[Mapping[str, object]], path: Path, *, model: str, repeat: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"format": VERDICTS_FORMAT, "prompt": PROMPT_VERSION, "model": model, "repeat": repeat,
            "written_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), "cases": list(records)}
    path.write_bytes((json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
