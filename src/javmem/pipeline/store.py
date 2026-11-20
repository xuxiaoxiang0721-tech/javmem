"""L3 的结果库：按“窗口文字、提示词版本、模型”做键，J2.8 以后直接读，不重调模型。

一个 SQLite 文件 `l3.db`，放在仓库外（结果是真实内容）。四张表：

- `results`：一个键一行。模型原样的输出、校验过的结构（引用还是 T 编号）、这次成功的调用用了多少 token。
- `windows`：一个窗口一行，按 (窗口编号, 键)。窗口清单（每个编号是哪句原文、哪个包和版本、标签、origin、
  发之前打没打码）、发出去的文字、换成原文句子编号的抽取结果。
- `calls`：每一次调用都记，成功、格式错、限流、重试都算，格式错误率和重试次数从这里算。
- `runs`：跑一次一行，选项和汇总（L4 的模型复核也记在这里，选项里 purpose 是 l4_recheck）。

同样的原文换了窗口编号，键不变，结果直接用；换成句子编号用的是这个窗口自己的清单。

L4 的模型复核（javmem.pipeline.recheck）也存在这里，另外三张表：

- `rechecks`：一个复核的键一行（窗口文字、事实那一段、提示词版本、模型）。模型原样的回复、校验过的判定、token。
- `recheck_windows`：哪个窗口（窗口编号, L3 的键）用哪个复核的键，F 编号对着 L3 结果里的第几条事实。
  和判定一起写进去（一个事务），所以每个判定至少有一个窗口用着；窗口删了（撤回、彻底删除），只被它用着的判定也删。
- `recheck_calls`：复核的每一次调用，和 `calls` 一样只有状态和 token，`cache_key` 是复核的键。

L4 的判断 C（javmem.pipeline.reljudge）也存在这里，和复核一样三张表：

- `judgments`：一个判的键一行（哪一档、提示词版本、模型、发过去的那段话）。本机那一档存 P(能) 和回的第一个字、前十个候选字；
  第二档存答案、理由（模型照原话写的，可能带着名字）和原样的回复、token。
- `judgment_windows`：哪个窗口（窗口编号, L3 的键）的第几条关系用哪个判的键。和判定一起写进去；窗口删了（撤回、彻底删除、
  这一次不存文字），只被它用着的判定也删。
- `judgment_calls`：判的每一次调用，只有状态和 token，`cache_key` 是判的键。
- `fact_judgment_windows`：进图前检查（javmem.pipeline.precheck，J5.3）判事实用的，和 `judgment_windows` 一样，只是连的是
  第几条事实。判定、调用记录和关系的放在同一张 `judgments`、`judgment_calls` 里（提示词版本 f1 分得开，键里有它）。

预处理 B（认人认事，javmem.pipeline.preb，J4.2）也存在这里，不进账本：

- `preb_notes`：一个窗口的标注〔指：说法 = 名字〕，按（窗口编号、没加标注的窗口文字的指纹、预处理版本、context）。
  context 是窗口的可见范围和正文那几个包的版本（preb.window_context）：同一个包换了范围、版本重发，窗口编号、文字可能一样，
  以前的标注是照以前那个范围的名单标的，不能照样用。`noted_sha` 是只加了 B 的标注以后的指纹（没有 A 的标注时就是
  `windows.window_sha`；带着 A 的〔日子〕时对不上，撤回连带另外按去掉标注的指纹对，见下面 A 那一段）；
  `packets`、`sentences` 是这个窗口有哪些包、哪些原文句子。核对、写账、合并工具算键时照原样加上（extract.prepare），
  和抽的时候一字不差。
- `preb_note_items`：标注里用到了名单上哪几项系统自己认出来的（撤回时找要重抽的窗口）。
- `preb_learned`：收过名字的窗口（键和 preb_notes 一样），接着跑不重收。
- `preb_items`、`preb_aliases`：名单里系统自己认出来的项和学来的“也叫”，每一个都记着从哪个包哪个版本、哪个窗口、
  哪一句（说法那一句）学来的，那个窗口的可见范围，和 `sources`：这段字在那个窗口里出现过的每一句（名字可能是照别的
  句子写的，撤回哪一句都跟着删）。
- `preb_blocked`：防合错拦下的；`depends` 是比的时候名单上那一项用到的学来的东西（`item:编号`、`alias:编号`）。

这些表里不写名单上别的窗口学来的项的名字（写成“#编号”），只有标注本身（发给 L3 的那段文字的一部分）带着名字。
撤回、彻底删除：`doomed` 把标注里用了撤回内容里学来的项的窗口也算上（只连一层），`delete_windows` 连着删这些窗口的
标注、收名字的记号、拦下的，`forget_preb` 再删撤回内容里学来的项和“也叫”、比过它们的拦下的那几行，以及没有窗口的
那些标注。只删、不改回排队的地方（彻底删除旧版本、包重发成 private），用 `doomed_by_notes` 找出标注里用了那些名字的
别的包，改回排队重抽。

预处理 A（相对的日子换成具体的日子，javmem.pipeline.prea）也存在这里，不进账本：

- `prea_notes`：一个窗口的标注〔日子：说法 = 日子〕，按（窗口编号、没加标注的窗口文字的指纹、规则版本）。A 只用这个窗口
  自己的字和抬头，不按可见范围分。`noted_sha` 是加了 A 以后的指纹（B 再在它上面加）；`packets`、`sentences` 是这个
  窗口有哪些包、句子。存过的不改。`detail` 只有说法、日子、行号、抬头时间，不存原文的句子。

抽之前放进去的现有关系（J4.2 后一半，javmem.pipeline.prer）也存在这里，不进账本：

- `prer_notes`：一个窗口放进去的那一段（`block`，开头说明下面另起的一段，一条都没有的是空的），按（窗口编号、没加标注的
  窗口文字的指纹、版本、context（和 B 一样）、`notes_sha`（这个窗口 B 的〔指〕的指纹：B 后来标上了，照新的重算））。
  `relations` 是放了哪几条关系的编号，`sources` 是撤回连带要看的出处 [包号, 版本, 句子编号]（这几条关系当时的证据，
  这一段写着的名字、让一头算在窗口里的写法在账本里的出处），`packets`、`sentences` 是这个窗口自己有哪些包、句子。存过的不改：先存的为准，后来账本变了，
  已经抽过的窗口文字也一字不变。撤回、彻底删除：`doomed`、`doomed_by_notes` 把放进去的关系当时的证据里有撤回内容的窗口
  也算上（只连一层），`delete_windows` 连着删这些窗口的这一行（只删同一个 context 的：同一个包别的版本的照留），
  `forget_prer` 再删窗口里有这些内容的、放进去的关系出自这些内容的（没有窗口的那些也算）。

窗口这一行（`windows`）存的是发出去的文字（`input_text`），A、B 的标注、放进去的现有关系都在里面。`strip_pre_notes`
去掉这几样，就是没加标注的那份，它的指纹就是这几样的键：哪种提示词、加了几样，撤回、彻底删除都按它对。撤回、彻底删除：
`delete_windows` 连着删这些窗口的 A 标注，`forget_prea` 再按包、句子删（没有窗口的那些也算）。A 的标注只按文字认、不按
版本：包重发了新版本、文字没变，新版本的窗口照用同一条；所以删的时候先看还有没有别的活着的窗口用着它（同一个窗口编号、
去掉标注以后同一份文字），用着的留下，记的包、句子换成那个窗口现在的。A 的标注里没有别的窗口的东西，不连带别的窗口。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from javmem.llm import Usage

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOCK_FILE = "l3.lock"


class StoreBusy(Exception):
    """这个结果库已经有一个抽取在跑：两个一起跑，并发就翻倍了，预算也各算各的。"""


@dataclass(frozen=True)
class Doomed:
    """撤回、彻底删除要删的一个窗口（ResultStore.doomed_windows，J5.6）。bodies 是正文的包（T 行）；whole 是整包撤回
    带出来的：窗口里有撤回的包（正文、上文都算），或者标注、放进去的现有关系用了从撤回的包里来的。"""

    window_id: str
    cache_key: str
    bodies: frozenset[str]
    whole: bool

STORE_FILE = "l3.db"
# 重调多半还是一样的失败：输出被截断、模型拒绝回答、窗口太长、内容被接口挡了、重试过还是格式错
PERMANENT_FAILURES = frozenset({"length", "refusal", "too_long", "content_rejected", "format_error"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    cache_key        TEXT PRIMARY KEY,
    window_sha       TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    model            TEXT NOT NULL,
    output           TEXT NOT NULL,
    parsed           TEXT NOT NULL,
    response_model   TEXT,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    seconds          REAL NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS windows (
    window_id      TEXT NOT NULL,
    cache_key      TEXT NOT NULL,
    window_sha     TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    source         TEXT NOT NULL,
    channel        TEXT NOT NULL,
    scope          TEXT NOT NULL,
    packets        TEXT NOT NULL,
    manifest       TEXT NOT NULL,
    input_text     TEXT NOT NULL,
    redactions     TEXT NOT NULL,
    extraction     TEXT,
    built_at       TEXT NOT NULL,
    PRIMARY KEY (window_id, cache_key)
);
CREATE TABLE IF NOT EXISTS calls (
    call_id          INTEGER PRIMARY KEY,
    run_id           TEXT NOT NULL,
    window_id        TEXT NOT NULL,
    cache_key        TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    status           TEXT NOT NULL,
    detail           TEXT,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    seconds          REAL NOT NULL,
    at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_by_key ON calls (cache_key);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    options     TEXT NOT NULL,
    summary     TEXT
);
CREATE TABLE IF NOT EXISTS rechecks (
    recheck_key      TEXT PRIMARY KEY,
    window_sha       TEXT NOT NULL,
    facts_sha        TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    model            TEXT NOT NULL,
    output           TEXT NOT NULL,
    parsed           TEXT NOT NULL,
    response_model   TEXT,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    seconds          REAL NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recheck_windows (
    window_id   TEXT NOT NULL,
    cache_key   TEXT NOT NULL,
    recheck_key TEXT NOT NULL,
    facts       TEXT NOT NULL,
    linked_at   TEXT NOT NULL,
    PRIMARY KEY (window_id, cache_key, recheck_key)
);
CREATE INDEX IF NOT EXISTS recheck_windows_by_key ON recheck_windows (recheck_key);
CREATE TABLE IF NOT EXISTS recheck_calls (
    call_id          INTEGER PRIMARY KEY,
    run_id           TEXT NOT NULL,
    window_id        TEXT NOT NULL,
    cache_key        TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    status           TEXT NOT NULL,
    detail           TEXT,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    seconds          REAL NOT NULL,
    at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recheck_calls_by_key ON recheck_calls (cache_key);
CREATE TABLE IF NOT EXISTS judgments (
    judge_key        TEXT PRIMARY KEY,
    tier             TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    model            TEXT NOT NULL,
    probability      REAL,
    answer           TEXT NOT NULL,
    reason           TEXT NOT NULL,
    output           TEXT NOT NULL,
    response_model   TEXT,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    seconds          REAL NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS judgment_windows (
    window_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    judge_key TEXT NOT NULL,
    relation  INTEGER NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (window_id, cache_key, judge_key, relation)
);
CREATE INDEX IF NOT EXISTS judgment_windows_by_key ON judgment_windows (judge_key);
CREATE TABLE IF NOT EXISTS fact_judgment_windows (
    window_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    judge_key TEXT NOT NULL,
    fact      INTEGER NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (window_id, cache_key, judge_key, fact)
);
CREATE INDEX IF NOT EXISTS fact_judgment_windows_by_key ON fact_judgment_windows (judge_key);
CREATE TABLE IF NOT EXISTS judgment_calls (
    call_id          INTEGER PRIMARY KEY,
    run_id           TEXT NOT NULL,
    window_id        TEXT NOT NULL,
    cache_key        TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    status           TEXT NOT NULL,
    detail           TEXT,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    seconds          REAL NOT NULL,
    at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS judgment_calls_by_key ON judgment_calls (cache_key);
CREATE TABLE IF NOT EXISTS preb_notes (
    window_id  TEXT NOT NULL,
    window_sha TEXT NOT NULL,
    version    TEXT NOT NULL,
    context    TEXT NOT NULL,
    noted_sha  TEXT NOT NULL,
    packets    TEXT NOT NULL,
    sentences  TEXT NOT NULL,
    notes      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    seconds    REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (window_id, window_sha, version, context)
);
CREATE TABLE IF NOT EXISTS preb_note_items (
    window_id  TEXT NOT NULL,
    window_sha TEXT NOT NULL,
    version    TEXT NOT NULL,
    context    TEXT NOT NULL,
    item_id    INTEGER NOT NULL,
    PRIMARY KEY (window_id, window_sha, version, context, item_id)
);
CREATE INDEX IF NOT EXISTS preb_note_items_by_item ON preb_note_items (item_id);
CREATE TABLE IF NOT EXISTS preb_learned (
    window_id  TEXT NOT NULL,
    window_sha TEXT NOT NULL,
    version    TEXT NOT NULL,
    context    TEXT NOT NULL,
    packets    TEXT NOT NULL,
    sentences  TEXT NOT NULL,
    detail     TEXT NOT NULL,
    seconds    REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (window_id, window_sha, version, context)
);
CREATE TABLE IF NOT EXISTS preb_items (
    item_id     INTEGER PRIMARY KEY,
    version     TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    partitions  TEXT NOT NULL,
    sensitive   TEXT NOT NULL,
    packet_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    window_id   TEXT NOT NULL,
    window_sha  TEXT NOT NULL,
    ref         TEXT NOT NULL,
    sentence_id TEXT NOT NULL,
    sources     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preb_aliases (
    alias_id    INTEGER PRIMARY KEY,
    version     TEXT NOT NULL,
    target      TEXT NOT NULL,
    alias       TEXT NOT NULL,
    partitions  TEXT NOT NULL,
    sensitive   TEXT NOT NULL,
    packet_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    window_id   TEXT NOT NULL,
    window_sha  TEXT NOT NULL,
    ref         TEXT NOT NULL,
    sentence_id TEXT NOT NULL,
    sources     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preb_blocked (
    window_id      TEXT NOT NULL,
    window_sha     TEXT NOT NULL,
    version        TEXT NOT NULL,
    context        TEXT NOT NULL,
    ref            TEXT NOT NULL,
    mention        TEXT NOT NULL,
    item           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    mention_values TEXT NOT NULL,
    item_values    TEXT NOT NULL,
    depends        TEXT NOT NULL,
    stage          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (window_id, window_sha, version, context, ref, mention, item, kind, stage)
);
CREATE TABLE IF NOT EXISTS prea_notes (
    window_id  TEXT NOT NULL,
    window_sha TEXT NOT NULL,
    version    TEXT NOT NULL,
    noted_sha  TEXT NOT NULL,
    packets    TEXT NOT NULL,
    sentences  TEXT NOT NULL,
    notes      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (window_id, window_sha, version)
);
"""
# 放进去的现有关系（javmem.pipeline.prer）。表结构改过（加了 notes_sha、entities），旧的打开时照新的搬一遍（_upgrade_prer_notes）
_PRER_NOTES = """
CREATE TABLE IF NOT EXISTS prer_notes (
    window_id  TEXT NOT NULL,
    window_sha TEXT NOT NULL,
    version    TEXT NOT NULL,
    context    TEXT NOT NULL,
    notes_sha  TEXT NOT NULL,
    packets    TEXT NOT NULL,
    sentences  TEXT NOT NULL,
    block      TEXT NOT NULL,
    relations  TEXT NOT NULL,
    sources    TEXT NOT NULL,
    entities   TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (window_id, window_sha, version, context, notes_sha)
);
"""
_SCHEMA += _PRER_NOTES
_PRER_COLUMNS = (
    "window_id", "window_sha", "version", "context", "notes_sha", "packets", "sentences", "block", "relations", "sources",
    "entities", "detail", "created_at",
)
_PREB_VERSION_BEFORE_NOTES_SHA = "b4"  # notes_sha 加上以前的行都是 b4 的标注：搬的时候照它算 notes_sha
# 预处理加进窗口的标注：B 的〔指：…〕、A 的〔日子：…〕
_PRE_LABELS = ("〔指：", "〔日子：")
# 抽之前放进去的现有关系（javmem.pipeline.prer）：开头说明下面空一行，这一行打头，下面一条一行“- ”开头
RELATIONS_HEADER = "【账本里已经记着的关系：以前的记忆，只帮着认人、看变没变，不是原文，不能当证据】"
RELATION_MARK = "- "


def strip_known_relations(text: str) -> str:
    """去掉放进去的现有关系那一段（第二、三行是空行和 RELATIONS_HEADER，接着的“- ”行）。原文每一行前面都有编号、
    抬头和【】的段名是 render 写的，“- ”开头的行只能是这一段的；没有这一段的原样返回。"""
    rows = text.split("\n")
    if len(rows) < 3 or rows[1] != "" or rows[2] != RELATIONS_HEADER:
        return text
    end = 3
    while end < len(rows) and rows[end].startswith(RELATION_MARK):
        end += 1
    return "\n".join([rows[0], *rows[end:]])


def strip_pre_notes(text: str) -> str:
    """发出去的窗口文字去掉预处理加的东西（〔指：…〕〔日子：…〕标注、放进去的现有关系那一段），就是没加标注的那份
    （render_window 写出来的样子）。原文里的〔〕都换成了［］，一行编号后面开头连着的〔…〕只能是标注：render 自己的
    （〔引用〕这些）留着，预处理的去掉。"""
    out = []
    for row in strip_known_relations(text).split("\n"):
        ref, space, rest = row.partition(" ")
        if space and rest.startswith("〔"):
            kept: list[str] = []
            at = 0
            while rest.startswith("〔", at) and "〕" in rest[at:]:
                end = rest.index("〕", at) + 1
                if not rest.startswith(_PRE_LABELS, at):
                    kept.append(rest[at:end])
                at = end
            row = f"{ref} {''.join(kept)}{rest[at:]}"
        out.append(row)
    return "\n".join(out)


def _plain_sha(input_text: str, window_sha: str) -> str:
    """窗口这一行没加标注的那份文字的指纹。没存发出去的文字的（private、打码没打干净的）本来就没加标注。"""
    if not input_text:
        return window_sha
    return hashlib.sha256(strip_pre_notes(input_text).encode("utf-8")).hexdigest()


def _noted(row: sqlite3.Row, noted: set[tuple[str, str]]) -> bool:
    """windows 这一行是不是 noted 里的：(窗口编号, 指纹) 对得上 B 记下的 noted_sha，或者去掉预处理标注以后的指纹对得上
    标注的键。没存发出去的文字的行（打码没打干净的）只能照 noted_sha 认。只在窗口编号对得上时才去算指纹。"""
    if not any(window_id == row["window_id"] for window_id, _ in noted):
        return False
    if (row["window_id"], row["window_sha"]) in noted:
        return True
    return (row["window_id"], _plain_sha(row["input_text"], row["window_sha"])) in noted


def _row_context(row: sqlite3.Row) -> str:
    """windows 那一行的 context：和 preb.window_context 一样（可见范围、正文那几个包的版本，按 T 行算）。这里自己算，
    不引 preb（preb 引这里）；两边一样有测试钉着。"""
    packets = sorted(
        {(str(item["packet_id"]), int(item["revision"])) for item in json.loads(row["manifest"]) if str(item.get("ref", "")).startswith("T")}
    )
    text = json.dumps({"scope": json.loads(row["scope"]), "packets": [list(one) for one in packets]}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest_sentences(manifest: str) -> str:
    """windows 那一行的清单里有哪些原文句子（JSON 文字，和标注那几张表的 sentences 一样）。"""
    return json.dumps(sorted({str(item["sentence_id"]) for item in json.loads(manifest) if item.get("sentence_id")}),
                      ensure_ascii=False)


def _touches(
    packets_json: str, sentences_json: str, packets: Collection[str], sentences: Collection[str],
    revisions: Collection[tuple[str, int]],
) -> bool:
    """这些包、句子（JSON 文字）里有没有要删的包（任何版本）、版本、句子。"""
    inside = {(str(packet_id), int(revision)) for packet_id, revision in json.loads(packets_json)}
    return (any(packet_id in packets for packet_id, _ in inside) or bool(inside & set(revisions))
            or bool(set(json.loads(sentences_json)) & set(sentences)))

# 学来的一项、一个“也叫”的出处：主出处（说法那一句）一列一列存，sources 是这段字出现过的每一句 [包号, 版本, 句子编号]
_PREB_SOURCE = "partitions, sensitive, packet_id, revision, window_id, window_sha, ref, sentence_id, sources, created_at"
_PREB_SOURCE_VALUES = (
    ":partitions, :sensitive, :packet_id, :revision, :window_id, :window_sha, :ref, :sentence_id, :sources, :created_at"
)


def _sources_hit(
    sources: str, packets: Collection[str], sentences: Collection[str], revisions: Collection[tuple[str, int]]
) -> bool:
    """学来的一项、一个“也叫”的出处（[包号, 版本, 句子编号] 的 JSON 列表）里，有一处是这些包、这些版本、这些句子。"""
    for packet_id, revision, sentence_id in json.loads(sources):
        if packet_id in packets or (packet_id, int(revision)) in revisions or sentence_id in sentences:
            return True
    return False


@dataclass(frozen=True)
class StoredResult:
    cache_key: str
    output: str
    parsed: dict[str, object]
    usage: Usage
    seconds: float
    response_model: str | None


@dataclass(frozen=True)
class StoredRecheck:
    recheck_key: str
    output: str
    parsed: dict[str, object]  # {"verdicts": [{id, verdict, problem}]}，按 F 编号的顺序
    usage: Usage
    seconds: float
    response_model: str | None


@dataclass(frozen=True)
class StoredJudgment:
    judge_key: str
    tier: str  # local（本机那一档）、llm（第二档）
    model: str
    probability: float | None  # 本机那一档的 P(能)；第二档没有
    answer: str  # 本机回的第一个字；第二档的“能 / 不能 / 拿不准”
    reason: str  # 第二档的理由
    usage: Usage
    seconds: float


class ResultStore:
    """几个线程可以一起写，一把锁管着一个连接。"""

    def __init__(self, directory: Path, *, exclusive: bool = False) -> None:
        """exclusive：要调模型的这样打开，同一个结果库同一时间只能有一个。锁跟着进程走。"""
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
                raise StoreBusy("这个结果库已经有一个抽取在跑") from None
            self._handle = handle
        self.path = directory / STORE_FILE
        self._conn = sqlite3.connect(self.path, autocommit=True, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._upgrade_prer_notes()

    def _upgrade_prer_notes(self) -> None:
        """prer_notes 是旧的表结构（db9f431 没有 notes_sha、entities）：建新表搬过去。notes_sha 照库里这个窗口的 B 标注算
        （和 prer.notes_digest 一样），已经抽过的窗口键不变；entities 空着（撤回以后核的时候当旧行，照撤回连带算）。"""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(prer_notes)")}
        if set(_PRER_COLUMNS) <= columns:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                columns = {row[1] for row in self._conn.execute("PRAGMA table_info(prer_notes)")}  # 拿到写锁以后再看一次
                if not set(_PRER_COLUMNS) <= columns:
                    rows = self._conn.execute("SELECT * FROM prer_notes").fetchall()
                    self._conn.execute("DROP TABLE prer_notes")
                    self._conn.execute(_PRER_NOTES)
                    for row in rows:
                        values = {name: row[name] for name in row.keys() if name in _PRER_COLUMNS}
                        values.setdefault("entities", "")
                        if "notes_sha" not in values:
                            values["notes_sha"] = self._old_notes_sha(row)
                        self._conn.execute(
                            f"INSERT OR IGNORE INTO prer_notes ({', '.join(_PRER_COLUMNS)})"
                            f" VALUES ({', '.join(':' + name for name in _PRER_COLUMNS)})",
                            values,
                        )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _old_notes_sha(self, row: sqlite3.Row) -> str:
        """拿着锁调：旧的一行当时用的 B 标注的指纹（和 prer.notes_digest 一样：没有标注的是空的）。"""
        found = self._conn.execute(
            "SELECT notes FROM preb_notes WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?",
            (row["window_id"], row["window_sha"], _PREB_VERSION_BEFORE_NOTES_SHA, row["context"]),
        ).fetchone()
        notes = json.loads(found["notes"]) if found is not None else None
        if not notes:
            return ""
        return hashlib.sha256(json.dumps(notes, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

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

    def __enter__(self) -> ResultStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 写 ----------

    def save_window(self, row: Mapping[str, object]) -> None:
        """窗口清单。同一个窗口编号和键，再存一次更新清单，抽取结果留着。

        这一次不存发出去的文字的（private 的、打码没打干净的），以前存的文字和抽取结果也一起清掉，
        这个窗口的模型复核（判定里模型会说原文哪里不对）也一起清掉。
        """
        sql = (
            "INSERT INTO windows (window_id, cache_key, window_sha, prompt_version, model, source, channel, scope,"
            " packets, manifest, input_text, redactions, built_at)"
            " VALUES (:window_id, :cache_key, :window_sha, :prompt_version, :model, :source, :channel, :scope,"
            " :packets, :manifest, :input_text, :redactions, :built_at)"
            " ON CONFLICT (window_id, cache_key) DO UPDATE SET manifest = excluded.manifest,"
            " packets = excluded.packets, scope = excluded.scope, redactions = excluded.redactions,"
            " input_text = excluded.input_text,"
            " extraction = CASE WHEN excluded.input_text = '' THEN NULL ELSE windows.extraction END"
        )
        with self._lock:
            if row["input_text"]:
                self._conn.execute(sql, dict(row))
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(sql, dict(row))
                self._drop_rechecks([(str(row["window_id"]), str(row["cache_key"]))])
                self._drop_judgments([(str(row["window_id"]), str(row["cache_key"]))])  # 判断 C 第二档的理由里可能有原文
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def save_result(
        self,
        *,
        cache_key: str,
        window_sha: str,
        prompt_version: str,
        model: str,
        output: str,
        parsed: Mapping[str, object],
        response_model: str | None,
        usage: Usage,
        seconds: float,
        created_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cache_key,
                    window_sha,
                    prompt_version,
                    model,
                    output,
                    json.dumps(parsed, ensure_ascii=False),
                    response_model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.reasoning_tokens,
                    usage.cached_tokens,
                    seconds,
                    created_at,
                ),
            )

    def save_extraction(self, window_id: str, cache_key: str, extraction: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE windows SET extraction = ? WHERE window_id = ? AND cache_key = ?",
                (json.dumps(extraction, ensure_ascii=False), window_id, cache_key),
            )

    def record_call(self, row: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls (run_id, window_id, cache_key, attempt, status, detail, input_tokens, output_tokens,"
                " reasoning_tokens, cached_tokens, seconds, at) VALUES (:run_id, :window_id, :cache_key, :attempt,"
                " :status, :detail, :input_tokens, :output_tokens, :reasoning_tokens, :cached_tokens, :seconds, :at)",
                dict(row),
            )

    # ---------- L4 的模型复核 ----------

    def save_recheck(
        self,
        *,
        recheck_key: str,
        window_sha: str,
        facts_sha: str,
        prompt_version: str,
        model: str,
        output: str,
        parsed: Mapping[str, object],
        response_model: str | None,
        usage: Usage,
        seconds: float,
        created_at: str,
        window_id: str,
        cache_key: str,
        facts: Sequence[tuple[str, int]],
    ) -> None:
        """存一个判定，连同用它的窗口（窗口编号、L3 的键、F 编号对着第几条事实），一个事务。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO rechecks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        recheck_key,
                        window_sha,
                        facts_sha,
                        prompt_version,
                        model,
                        output,
                        json.dumps(parsed, ensure_ascii=False),
                        response_model,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.reasoning_tokens,
                        usage.cached_tokens,
                        seconds,
                        created_at,
                    ),
                )
                self._link(window_id, cache_key, recheck_key, facts, created_at)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def link_recheck(self, window_id: str, cache_key: str, recheck_key: str, facts: Sequence[tuple[str, int]], at: str) -> None:
        """又一个窗口用这个判定（存过的、这一批里文字和事实都一样的）。判定已经不在了就不连。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute("SELECT 1 FROM rechecks WHERE recheck_key = ?", (recheck_key,)).fetchone():
                    self._link(window_id, cache_key, recheck_key, facts, at)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _link(self, window_id: str, cache_key: str, recheck_key: str, facts: Sequence[tuple[str, int]], at: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO recheck_windows (window_id, cache_key, recheck_key, facts, linked_at) VALUES (?, ?, ?, ?, ?)",
            (window_id, cache_key, recheck_key, json.dumps([{"id": fact_id, "index": index} for fact_id, index in facts]), at),
        )

    def _drop_rechecks(self, pairs: Sequence[tuple[str, str]]) -> int:
        """调用方开着事务、拿着锁：这些窗口（窗口编号, L3 的键）用的复核链接删掉，只被它们用着的判定也删掉。
        返回删了几个判定。"""
        keys: set[str] = set()
        for window_id, cache_key in pairs:
            rows = self._conn.execute(
                "SELECT recheck_key FROM recheck_windows WHERE window_id = ? AND cache_key = ?", (window_id, cache_key)
            ).fetchall()
            keys.update(str(row[0]) for row in rows)
        self._conn.executemany("DELETE FROM recheck_windows WHERE window_id = ? AND cache_key = ?", list(pairs))
        dropped = 0
        for key in sorted(keys):
            cursor = self._conn.execute(
                "DELETE FROM rechecks WHERE recheck_key = ?1 AND NOT EXISTS (SELECT 1 FROM recheck_windows WHERE recheck_key = ?1)",
                (key,),
            )
            dropped += cursor.rowcount
        return dropped

    def record_recheck_call(self, row: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO recheck_calls (run_id, window_id, cache_key, attempt, status, detail, input_tokens,"
                " output_tokens, reasoning_tokens, cached_tokens, seconds, at) VALUES (:run_id, :window_id, :cache_key,"
                " :attempt, :status, :detail, :input_tokens, :output_tokens, :reasoning_tokens, :cached_tokens, :seconds, :at)",
                dict(row),
            )

    def recheck(self, recheck_key: str) -> StoredRecheck | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM rechecks WHERE recheck_key = ?", (recheck_key,)).fetchone()
        if row is None:
            return None
        return StoredRecheck(
            recheck_key=row["recheck_key"],
            output=row["output"],
            parsed=json.loads(row["parsed"]),
            usage=Usage(row["input_tokens"], row["output_tokens"], row["reasoning_tokens"], row["cached_tokens"]),
            seconds=row["seconds"],
            response_model=row["response_model"],
        )

    def last_recheck_failure(self, recheck_key: str) -> str | None:
        """这个复核的键最后一次调用要是截断、拒答、太长、内容被挡、格式错这类重调也没用的，返回它；不然 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM recheck_calls WHERE cache_key = ? ORDER BY call_id DESC LIMIT 1", (recheck_key,)
            ).fetchone()
        if row is None:
            return None
        status = row["status"].removesuffix("_estimated")
        return status if status in PERMANENT_FAILURES else None

    def recheck_calls(self, recheck_keys: set[str] | None = None) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM recheck_calls ORDER BY call_id").fetchall()
        return [row for row in rows if recheck_keys is None or row["cache_key"] in recheck_keys]

    def recheck_links(self) -> list[sqlite3.Row]:
        """哪个窗口用哪个判定。"""
        with self._lock:
            return self._conn.execute("SELECT * FROM recheck_windows ORDER BY window_id, cache_key").fetchall()

    # ---------- L4 的判断 C ----------

    def save_judgment(self, row: Mapping[str, object], links: Sequence[tuple[str, str, int]], *, kind: str = "relation") -> None:
        """存一个判定，连同用它的关系（窗口编号, L3 的键, 第几条关系），一个事务。row 是 judgments 的一行。kind 是 fact 的
        （进图前检查判的事实）连到 fact_judgment_windows。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO judgments (judge_key, tier, prompt_version, model, probability, answer, reason,"
                    " output, response_model, input_tokens, output_tokens, reasoning_tokens, cached_tokens, seconds, created_at)"
                    " VALUES (:judge_key, :tier, :prompt_version, :model, :probability, :answer, :reason, :output,"
                    " :response_model, :input_tokens, :output_tokens, :reasoning_tokens, :cached_tokens, :seconds, :created_at)",
                    dict(row),
                )
                self._link_judgment(str(row["judge_key"]), links, str(row["created_at"]), kind)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def link_judgment(self, judge_key: str, links: Sequence[tuple[str, str, int]], at: str, *, kind: str = "relation") -> None:
        """又有关系（kind 是 fact 的是事实）用这个判定（存过的、这一批里要问的话一样的）。判定已经不在了就不连。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute("SELECT 1 FROM judgments WHERE judge_key = ?", (judge_key,)).fetchone():
                    self._link_judgment(judge_key, links, at, kind)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _link_judgment(self, judge_key: str, links: Sequence[tuple[str, str, int]], at: str, kind: str = "relation") -> None:
        if kind not in ("relation", "fact"):
            raise ValueError(kind)
        table, column = ("fact_judgment_windows", "fact") if kind == "fact" else ("judgment_windows", "relation")
        self._conn.executemany(
            f"INSERT OR REPLACE INTO {table} (window_id, cache_key, judge_key, {column}, linked_at) VALUES (?, ?, ?, ?, ?)",
            [(window_id, cache_key, judge_key, int(index), at) for window_id, cache_key, index in links],
        )

    def _drop_judgments(self, pairs: Sequence[tuple[str, str]]) -> int:
        """调用方开着事务、拿着锁：这些窗口（窗口编号, L3 的键）用的判定链接删掉（关系的、事实的），只被它们用着的判定也删掉。
        返回删了几个。"""
        keys: set[str] = set()
        for table in ("judgment_windows", "fact_judgment_windows"):
            for window_id, cache_key in pairs:
                rows = self._conn.execute(
                    f"SELECT judge_key FROM {table} WHERE window_id = ? AND cache_key = ?", (window_id, cache_key)
                ).fetchall()
                keys.update(str(row[0]) for row in rows)
            self._conn.executemany(f"DELETE FROM {table} WHERE window_id = ? AND cache_key = ?", list(pairs))
        dropped = 0
        for key in sorted(keys):
            cursor = self._conn.execute(
                "DELETE FROM judgments WHERE judge_key = ?1 AND NOT EXISTS (SELECT 1 FROM judgment_windows WHERE judge_key = ?1)"
                " AND NOT EXISTS (SELECT 1 FROM fact_judgment_windows WHERE judge_key = ?1)",
                (key,),
            )
            dropped += cursor.rowcount
        return dropped

    def record_judgment_call(self, row: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO judgment_calls (run_id, window_id, cache_key, attempt, status, detail, input_tokens,"
                " output_tokens, reasoning_tokens, cached_tokens, seconds, at) VALUES (:run_id, :window_id, :cache_key,"
                " :attempt, :status, :detail, :input_tokens, :output_tokens, :reasoning_tokens, :cached_tokens, :seconds, :at)",
                dict(row),
            )

    def judgment(self, judge_key: str) -> StoredJudgment | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM judgments WHERE judge_key = ?", (judge_key,)).fetchone()
        if row is None:
            return None
        return StoredJudgment(
            judge_key=row["judge_key"],
            tier=row["tier"],
            model=row["model"],
            probability=row["probability"],
            answer=row["answer"],
            reason=row["reason"],
            usage=Usage(row["input_tokens"], row["output_tokens"], row["reasoning_tokens"], row["cached_tokens"]),
            seconds=row["seconds"],
        )

    def last_judgment_failure(self, judge_key: str, format_attempts: int = 2) -> str | None:
        """这个判的键最后一次调用要是截断、拒答、太长、内容被挡这类重调也没用的，返回它；不然 None。格式错要最近连着
        format_attempts 次都是才算（照 L3 的规矩格式错重试一次）：只错了一次、还没来得及重试就停了（到时间、预算、Ctrl+C）的，
        下次接着问。判断 C 回不了的会被挡下、写进账本，写完的包不再抽，所以这里不能比 L3 松。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status FROM judgment_calls WHERE cache_key = ? ORDER BY call_id DESC LIMIT ?",
                (judge_key, max(1, format_attempts)),
            ).fetchall()
        if not rows:
            return None
        statuses = [str(row["status"]).removesuffix("_estimated") for row in rows]
        if statuses[0] not in PERMANENT_FAILURES:
            return None
        if statuses[0] == "format_error" and (len(statuses) < format_attempts or set(statuses) != {"format_error"}):
            return None
        return statuses[0]

    def judgment_calls(self, judge_keys: set[str] | None = None) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM judgment_calls ORDER BY call_id").fetchall()
        return [row for row in rows if judge_keys is None or row["cache_key"] in judge_keys]

    def judgment_links(self) -> list[sqlite3.Row]:
        """哪个窗口的第几条关系用哪个判定。"""
        with self._lock:
            return self._conn.execute("SELECT * FROM judgment_windows ORDER BY window_id, cache_key, relation").fetchall()

    def fact_judgment_links(self) -> list[sqlite3.Row]:
        """哪个窗口的第几条事实用哪个判定（进图前检查）。"""
        with self._lock:
            return self._conn.execute("SELECT * FROM fact_judgment_windows ORDER BY window_id, cache_key, fact").fetchall()

    def start_run(self, run_id: str, started_at: str, options: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, started_at, options) VALUES (?, ?, ?)",
                (run_id, started_at, json.dumps(options, ensure_ascii=False)),
            )

    def finish_run(self, run_id: str, finished_at: str, summary: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?",
                (finished_at, json.dumps(summary, ensure_ascii=False), run_id),
            )

    # ---------- 读 ----------

    def result(self, cache_key: str) -> StoredResult | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM results WHERE cache_key = ?", (cache_key,)).fetchone()
        if row is None:
            return None
        return StoredResult(
            cache_key=row["cache_key"],
            output=row["output"],
            parsed=json.loads(row["parsed"]),
            usage=Usage(row["input_tokens"], row["output_tokens"], row["reasoning_tokens"], row["cached_tokens"]),
            seconds=row["seconds"],
            response_model=row["response_model"],
        )

    def last_failure(self, cache_key: str) -> str | None:
        """这个键最后一次调用要是截断、拒答、太长、内容被挡、格式错这类重调也没用的，返回它；不然 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM calls WHERE cache_key = ? ORDER BY call_id DESC LIMIT 1", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        status = row["status"].removesuffix("_estimated")
        return status if status in PERMANENT_FAILURES else None

    def calls(self, cache_keys: set[str] | None = None) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM calls ORDER BY call_id").fetchall()
        return [row for row in rows if cache_keys is None or row["cache_key"] in cache_keys]

    def extraction(self, window_id: str, cache_key: str) -> dict[str, object] | None:
        """这个窗口换成原文句子编号的结果。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT extraction FROM windows WHERE window_id = ? AND cache_key = ?", (window_id, cache_key)
            ).fetchone()
        return json.loads(row["extraction"]) if row is not None and row["extraction"] is not None else None

    def windows(self) -> Iterator[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM windows ORDER BY window_id, built_at").fetchall()
        yield from rows

    # ---------- 撤回 ----------

    def doomed(
        self,
        packets: Collection[str] = (),
        sentences: Collection[str] = (),
        revisions: Collection[tuple[str, int]] = (),
        extra: Collection[tuple[str, str]] = (),
    ) -> tuple[list[tuple[str, str]], frozenset[str]]:
        """撤回、彻底删除用：窗口里有这些包（任何版本，上文的也算）、这些版本、这些句子的窗口（窗口编号, 键），
        以及这些窗口的正文是哪些包（它们的窗口少了内容，要重抽）。只读。

        预处理 B：标注里用了从这些内容里学来的项的窗口也算（标注是发给 L3 的文字，名字是从撤回的内容里来的），
        只连一层：那些窗口自己学来的项不跟着算。抽之前放进去的现有关系也一样：sources 里有这些内容的窗口也算；extra 是
        另外要算上的（窗口编号, 没加标注的指纹），撤回以后照账本核出来对不上的那些（prer.stale_keys）。"""
        found = self.doomed_windows(packets, sentences, revisions, extra)
        return [(one.window_id, one.cache_key) for one in found], frozenset(packet for one in found for packet in one.bodies)

    def doomed_windows(
        self,
        packets: Collection[str] = (),
        sentences: Collection[str] = (),
        revisions: Collection[tuple[str, int]] = (),
        extra: Collection[tuple[str, str]] = (),
        whole_sentences: Collection[str] = (),
    ) -> list[Doomed]:
        """doomed 的细账（J5.6：撤回时照它清账本）：每个要删的窗口带着正文是哪些包，是不是整包撤回带出来的（窗口里有
        这些包，或者标注、放进去的现有关系用了从这些包里来的，或者窗口里有 whole_sentences 里的句子：跟着撤回一个包才
        标上的，比如别的包里引用它原话的行）。只按别的句子、版本、extra 算上的不是。只读。"""
        packets, sentences, revisions = set(packets), set(sentences), {(p, int(r)) for p, r in revisions}
        whole_sentences = set(whole_sentences)
        with self._lock:
            rows = self._conn.execute(
                "SELECT window_id, cache_key, window_sha, input_text, packets, manifest FROM windows"
            ).fetchall()
            withdrawn = self._preb_withdrawn_items(packets, sentences, revisions)
            noted = self._preb_noted_with(withdrawn) | self._prer_hit(packets, sentences, revisions) | set(extra)
            by_packets = (
                self._preb_noted_with(self._preb_withdrawn_items(packets, (), ())) | self._prer_hit(packets, (), ())
                if packets else set()
            )
        found: list[Doomed] = []
        for row in rows:
            inside = {(str(packet_id), int(revision)) for packet_id, revision in json.loads(row["packets"])}
            manifest = json.loads(row["manifest"])
            has_packet = any(packet_id in packets for packet_id, _ in inside)
            if not (
                has_packet
                or inside & revisions
                or any(item.get("sentence_id") in sentences for item in manifest)
                or _noted(row, noted)
            ):
                continue
            bodies = frozenset(str(item["packet_id"]) for item in manifest if str(item.get("ref", "")).startswith("T"))
            whole = has_packet or _noted(row, by_packets) or any(item.get("sentence_id") in whole_sentences for item in manifest)
            found.append(Doomed(row["window_id"], row["cache_key"], bodies, whole))
        return found

    def delete_windows(self, doomed: Collection[tuple[str, str]]) -> int:
        """删掉这些窗口，以及只被它们用到的结果（里面有原文和模型的回复）。模型复核的链接和只被它们用着的判定也删
        （模型会说原文哪里不对），判断 C 的也一样（第二档的理由照原话写）。调用记录（calls、recheck_calls、judgment_calls）
        不删，它们只有状态和 token。返回删了几个 L3 结果。

        预处理 B：这些窗口的标注、收名字的记号、拦下的也删（里面有这个窗口的原文）。学来的项不在这里删，见 forget_preb。"""
        results = 0
        if not doomed:
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._drop_rechecks(list(doomed))
                self._drop_judgments(list(doomed))
                self._drop_prea_windows(list(doomed))
                self._drop_preb_windows(list(doomed))
                self._drop_prer_windows(list(doomed))
                self._conn.executemany("DELETE FROM windows WHERE window_id = ? AND cache_key = ?", list(doomed))
                for key in sorted({key for _, key in doomed}):
                    cursor = self._conn.execute(
                        "DELETE FROM results WHERE cache_key = ?1 AND NOT EXISTS (SELECT 1 FROM windows WHERE cache_key = ?1)",
                        (key,),
                    )
                    results += cursor.rowcount
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return results

    # ---------- 预处理 A（相对的日子，javmem.pipeline.prea） ----------

    def prea_notes(self, window_id: str, window_sha: str, version: str) -> dict[str, list[list[str]]] | None:
        """这个窗口（没加标注的文字的指纹）这个版本存下的预处理 A 的标注；没标过的 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT notes FROM prea_notes WHERE window_id = ? AND window_sha = ? AND version = ?",
                (window_id, window_sha, version),
            ).fetchone()
        return json.loads(row["notes"]) if row is not None else None

    def save_prea_notes(self, row: Mapping[str, object]) -> None:
        """一个窗口的预处理 A 的标注（packets、sentences、notes、detail 已经是 JSON 文字）。存过的不改：先存的为准，
        以后规则改了、版本号没换，已经抽过的窗口文字也一字不变。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO prea_notes (window_id, window_sha, version, noted_sha, packets, sentences, notes, detail,"
                " created_at) VALUES (:window_id, :window_sha, :version, :noted_sha, :packets, :sentences, :notes, :detail,"
                " :created_at)",
                dict(row),
            )

    def forget_prea(
        self,
        packets: Collection[str] = (),
        sentences: Collection[str] = (),
        revisions: Collection[tuple[str, int]] = (),
    ) -> int:
        """撤回、彻底删除时，在 delete_windows 之后调：窗口里有这些包（任何版本）、这些版本、这些句子的预处理 A 的标注删掉
        （结果库里没有窗口的那些也算：标完了还没抽就停下的）。A 的标注里只有这个窗口自己的字，不连带别的窗口。
        返回删了几条。"""
        packets, sentences, revisions = set(packets), set(sentences), {(p, int(r)) for p, r in revisions}
        if not (packets or sentences or revisions):
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                gone = []
                for row in self._conn.execute("SELECT window_id, window_sha, version, packets, sentences FROM prea_notes").fetchall():
                    if not _touches(row["packets"], row["sentences"], packets, sentences, revisions):
                        continue
                    key = (row["window_id"], row["window_sha"], row["version"])
                    live = self._prea_live_window(row["window_id"], row["window_sha"], set(), packets, sentences, revisions)
                    if live is None:
                        gone.append(key)
                        continue
                    self._conn.execute(  # 新版本的窗口文字没变、照用这一条：留下，记的包、句子换成它现在的
                        "UPDATE prea_notes SET packets = ?, sentences = ? WHERE window_id = ? AND window_sha = ? AND version = ?",
                        (live["packets"], _manifest_sentences(live["manifest"]), *key),
                    )
                self._conn.executemany(
                    "DELETE FROM prea_notes WHERE window_id = ? AND window_sha = ? AND version = ?", gone
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return len(gone)

    def _drop_prea_windows(self, doomed: Sequence[tuple[str, str]]) -> None:
        """拿着锁、在事务里、删 windows 之前调：这些窗口的预处理 A 的标注删掉。窗口这一行存着发出去的文字，去掉预处理的
        标注就是没加标注的那份（A 的键）；没存文字的（private 那些）本来就没标。"""
        gone = set(doomed)
        for window_id, cache_key in doomed:
            row = self._conn.execute(
                "SELECT window_sha, input_text FROM windows WHERE window_id = ? AND cache_key = ?", (window_id, cache_key)
            ).fetchone()
            if row is None:
                continue
            for plain in sorted({row["window_sha"], _plain_sha(row["input_text"], row["window_sha"])}):
                if self._prea_live_window(window_id, plain, gone) is not None:
                    continue  # 别的窗口（新版本的、文字一样的）还用着这一条
                self._conn.execute("DELETE FROM prea_notes WHERE window_id = ? AND window_sha = ?", (window_id, plain))

    def _prea_live_window(
        self,
        window_id: str,
        plain_sha: str,
        gone: Collection[tuple[str, str]],
        packets: Collection[str] = (),
        sentences: Collection[str] = (),
        revisions: Collection[tuple[str, int]] = (),
    ) -> sqlite3.Row | None:
        """拿着锁调：还用着这条 A 标注的窗口（同一个窗口编号、去掉标注以后同一份文字），不算 gone 里的、不算里面有
        这些包、版本、句子的（它们也要删）、不算没存发出去的文字的（private、打码没打干净的，从来不加 A）。没有就是 None。"""
        for row in self._conn.execute(
            "SELECT cache_key, window_sha, input_text, packets, manifest FROM windows WHERE window_id = ?", (window_id,)
        ).fetchall():
            if not row["input_text"] or (window_id, row["cache_key"]) in gone:
                continue
            if _plain_sha(row["input_text"], row["window_sha"]) != plain_sha:
                continue
            if _touches(row["packets"], _manifest_sentences(row["manifest"]), packets, sentences, revisions):
                continue
            return row
        return None

    # ---------- 抽之前放进去的现有关系（javmem.pipeline.prer） ----------

    def prer_notes(self, window_id: str, window_sha: str, version: str, context: str, notes_sha: str = "") -> str | None:
        """这个窗口（没加标注的文字的指纹、可见范围和包的版本 context、B 的〔指〕的指纹 notes_sha）这个版本存下的那一段；
        空的是一条都没放，没算过的 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT block FROM prer_notes WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?"
                " AND notes_sha = ?",
                (window_id, window_sha, version, context, notes_sha),
            ).fetchone()
        return str(row["block"]) if row is not None else None

    def save_prer_notes(self, row: Mapping[str, object]) -> None:
        """一个窗口放进去的那一段（packets、sentences、relations、sources、detail 已经是 JSON 文字）。存过的不改：先存的为准，
        后来账本变了，已经抽过的窗口文字也一字不变。"""
        values = {"entities": "", **dict(row)}
        with self._lock:
            self._conn.execute(
                f"INSERT OR IGNORE INTO prer_notes ({', '.join(_PRER_COLUMNS)})"
                f" VALUES ({', '.join(':' + name for name in _PRER_COLUMNS)})",
                values,
            )

    def prer_rows(self) -> list[sqlite3.Row]:
        """放过的每一行（撤回以后照账本核一遍用，prer.stale_keys）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT window_id, window_sha, version, context, notes_sha, block, entities FROM prer_notes"
            ).fetchall()

    def forget_prer_keys(self, keys: Collection[tuple[str, str, str, str, str]]) -> int:
        """删掉这几行（窗口编号, 没加标注的指纹, 版本, context, notes_sha）：撤回以后核出来写着的名字、认出一头的别名已经
        对不上账本的（prer.stale_keys），在 delete_windows 之后调。返回删了几行。"""
        if not keys:
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    "DELETE FROM prer_notes WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?"
                    " AND notes_sha = ?",
                    sorted(set(keys)),
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return len(set(keys))

    def forget_prer(
        self,
        packets: Collection[str] = (),
        sentences: Collection[str] = (),
        revisions: Collection[tuple[str, int]] = (),
    ) -> int:
        """撤回、彻底删除时，在 delete_windows 之后调：窗口里有这些包（任何版本）、这些版本、这些句子的，放进去的关系当时的
        证据里有这些内容的，都删掉（结果库里没有窗口的那些也算：算好了还没抽就停下的）。返回删了几行。"""
        packets, sentences, revisions = set(packets), set(sentences), {(p, int(r)) for p, r in revisions}
        if not (packets or sentences or revisions):
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                gone = [
                    (row["window_id"], row["window_sha"], row["version"], row["context"], row["notes_sha"])
                    for row in self._conn.execute(
                        "SELECT window_id, window_sha, version, context, notes_sha, packets, sentences, sources FROM prer_notes"
                    ).fetchall()
                    if _touches(row["packets"], row["sentences"], packets, sentences, revisions)
                    or _sources_hit(row["sources"], packets, sentences, revisions)
                ]
                self._conn.executemany(
                    "DELETE FROM prer_notes WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?"
                    " AND notes_sha = ?",
                    gone,
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return len(gone)

    def _prer_hit(
        self, packets: Collection[str], sentences: Collection[str], revisions: Collection[tuple[str, int]]
    ) -> set[tuple[str, str]]:
        """拿着锁调：放进去的关系当时的证据里有这些包（任何版本）、版本、句子的窗口，(窗口编号, 没加标注的指纹)。"""
        if not (packets or sentences or revisions):
            return set()
        return {
            (row["window_id"], row["window_sha"])
            for row in self._conn.execute("SELECT window_id, window_sha, sources FROM prer_notes")
            if _sources_hit(row["sources"], packets, sentences, revisions)
        }

    def _drop_prer_windows(self, doomed: Sequence[tuple[str, str]]) -> None:
        """拿着锁、在事务里、删 windows 之前调：这些窗口放进去的那一行删掉。只删同一个 context 的（照窗口那一行算回来，
        _row_context）：同一个包重发了、文字没变的新版本窗口，编号和没加标注的指纹一样，它自己那一行还用着，不能删。"""
        for window_id, cache_key in doomed:
            row = self._conn.execute(
                "SELECT window_sha, input_text, scope, manifest FROM windows WHERE window_id = ? AND cache_key = ?",
                (window_id, cache_key),
            ).fetchone()
            if row is None or not row["input_text"]:  # 没存发出去的文字的（private、打码没打干净的）从来不放
                continue
            self._conn.execute(
                "DELETE FROM prer_notes WHERE window_id = ? AND window_sha = ? AND context = ?",
                (window_id, _plain_sha(row["input_text"], row["window_sha"]), _row_context(row)),
            )

    # ---------- 预处理 B（认人认事，javmem.pipeline.preb） ----------

    def preb_notes(self, window_id: str, window_sha: str, version: str, context: str) -> dict[str, list[list[str]]] | None:
        """这个窗口（没加标注的文字的指纹、可见范围和包的版本 context）这个版本存下的标注；没标过的 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT notes FROM preb_notes WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?",
                (window_id, window_sha, version, context),
            ).fetchone()
        return json.loads(row["notes"]) if row is not None else None

    def save_preb_notes(
        self, row: Mapping[str, object], item_ids: Collection[int], blocked: Sequence[Mapping[str, object]] = ()
    ) -> None:
        """一个窗口的标注、用到的系统认出来的项、拦下的，一个事务写进去。row 是 preb_notes 的一行（packets、sentences、
        notes、detail 已经是 JSON 文字）。"""
        key = (str(row["window_id"]), str(row["window_sha"]), str(row["version"]), str(row["context"]))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO preb_notes (window_id, window_sha, version, context, noted_sha, packets, sentences,"
                    " notes, detail, seconds, created_at) VALUES (:window_id, :window_sha, :version, :context, :noted_sha,"
                    " :packets, :sentences, :notes, :detail, :seconds, :created_at)",
                    dict(row),
                )
                self._conn.execute(
                    "DELETE FROM preb_note_items WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?", key
                )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO preb_note_items (window_id, window_sha, version, context, item_id) VALUES (?, ?, ?, ?, ?)",
                    [(*key, int(item_id)) for item_id in sorted(set(item_ids))],
                )
                self._insert_blocked(blocked)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def preb_learned(self, window_id: str, window_sha: str, version: str, context: str) -> bool:
        """这个窗口这个版本收过名字没有。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM preb_learned WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?",
                (window_id, window_sha, version, context),
            ).fetchone()
        return row is not None

    def save_preb_learning(
        self,
        *,
        items: Sequence[Mapping[str, object]] = (),
        aliases: Sequence[Mapping[str, object]] = (),
        blocked: Sequence[Mapping[str, object]] = (),
        learned: Mapping[str, object] | None = None,
    ) -> list[int]:
        """学到的一起写进去，一个事务：新的项（按先后，返回它们的编号）；“也叫”（target 写 'item:编号'、'name:名单上的名字'，
        或者 new_item 写这一次新的第几项）；拦下的；收过名字的记号（learned 是 preb_learned 的一行）。"""
        ids: list[int] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    cursor = self._conn.execute(
                        f"INSERT INTO preb_items (version, name, kind, {_PREB_SOURCE})"
                        f" VALUES (:version, :name, :kind, {_PREB_SOURCE_VALUES})",
                        {key: value for key, value in item.items() if key != "item_id"},
                    )
                    ids.append(int(cursor.lastrowid))  # type: ignore[arg-type]
                for alias in aliases:
                    row = {key: value for key, value in alias.items() if key != "new_item"}
                    if alias.get("new_item") is not None:
                        row["target"] = f"item:{ids[int(alias['new_item'])]}"  # type: ignore[call-overload]
                    self._conn.execute(
                        f"INSERT INTO preb_aliases (version, target, alias, {_PREB_SOURCE})"
                        f" VALUES (:version, :target, :alias, {_PREB_SOURCE_VALUES})",
                        row,
                    )
                self._insert_blocked(blocked)
                if learned is not None:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO preb_learned (window_id, window_sha, version, context, packets, sentences, detail,"
                        " seconds, created_at) VALUES (:window_id, :window_sha, :version, :context, :packets, :sentences,"
                        " :detail, :seconds, :created_at)",
                        dict(learned),
                    )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return ids

    def preb_items(self, version: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM preb_items WHERE version = ? ORDER BY item_id", (version,)).fetchall()

    def preb_aliases(self, version: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM preb_aliases WHERE version = ? ORDER BY alias_id", (version,)).fetchall()

    def preb_blocked(self, version: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM preb_blocked WHERE version = ? ORDER BY rowid", (version,)).fetchall()

    def doomed_by_notes(
        self,
        packets: Collection[str] = (),
        sentences: Collection[str] = (),
        revisions: Collection[tuple[str, int]] = (),
        extra: Collection[tuple[str, str]] = (),
    ) -> tuple[list[tuple[str, str]], set[str]]:
        """只看预处理 B 和放进去的现有关系：标注里用了从这些内容里学来的项的窗口、放进去的关系当时的证据里有这些内容的窗口
        （窗口编号, 键），和它们的正文是哪些包（要改回排队重抽）。doomed 已经把它们算在里面；只删别的、不改回排队的地方
        （彻底删除旧版本、包重发成 private）用这个补上排队。只读。"""
        packets, sentences, revisions = set(packets), set(sentences), {(p, int(r)) for p, r in revisions}
        with self._lock:
            noted = self._preb_noted_with(self._preb_withdrawn_items(packets, sentences, revisions))
            noted |= self._prer_hit(packets, sentences, revisions) | set(extra)
            rows = self._conn.execute(
                "SELECT window_id, cache_key, window_sha, input_text, manifest FROM windows"
            ).fetchall()
        found: list[tuple[str, str]] = []
        targets: set[str] = set()
        for row in rows:
            if _noted(row, noted):
                found.append((row["window_id"], row["cache_key"]))
                targets |= {str(one["packet_id"]) for one in json.loads(row["manifest"]) if str(one.get("ref", "")).startswith("T")}
        return found, targets

    def forget_preb(
        self,
        packets: Collection[str] = (),
        sentences: Collection[str] = (),
        revisions: Collection[tuple[str, int]] = (),
    ) -> int:
        """撤回、彻底删除时，在 delete_windows 之后调：名字出现过的句子有一句是这些包（任何版本）、这些版本、这些句子的，
        学来的项和“也叫”删掉，这些项的“也叫”一起删；标注里用到这些项的、窗口里有这些内容的标注、收名字的记号、拦下的
        也删（结果库里没有窗口的那些也算：标完了还没抽就停下的）；拦下的那几行里比过这些项、这些“也叫”的也删。
        返回删了几项。"""
        packets, sentences, revisions = set(packets), set(sentences), {(p, int(r)) for p, r in revisions}
        if not (packets or sentences or revisions):
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                withdrawn = self._preb_withdrawn_items(packets, sentences, revisions)
                keys = set(self._preb_note_keys_with(withdrawn))
                for table in ("preb_notes", "preb_learned"):
                    for row in self._conn.execute(f"SELECT window_id, window_sha, version, context, packets, sentences FROM {table}"):
                        inside = {(str(packet_id), int(revision)) for packet_id, revision in json.loads(row["packets"])}
                        if (any(packet_id in packets for packet_id, _ in inside) or inside & revisions
                                or set(json.loads(row["sentences"])) & sentences):
                            keys.add((row["window_id"], row["window_sha"], row["version"], row["context"]))
                self._drop_preb_keys(keys)
                targets = {f"item:{item_id}" for item_id in withdrawn}
                aliases = [
                    int(row["alias_id"])
                    for row in self._conn.execute("SELECT alias_id, target, sources FROM preb_aliases")
                    if row["target"] in targets or _sources_hit(row["sources"], packets, sentences, revisions)
                ]
                gone = targets | {f"alias:{alias_id}" for alias_id in aliases}
                stale = [
                    row["rowid"]
                    for row in self._conn.execute("SELECT rowid, depends FROM preb_blocked")
                    if set(json.loads(row["depends"])) & gone
                ]
                self._conn.executemany("DELETE FROM preb_blocked WHERE rowid = ?", [(rowid,) for rowid in stale])
                self._conn.executemany("DELETE FROM preb_aliases WHERE alias_id = ?", [(alias_id,) for alias_id in aliases])
                self._conn.executemany("DELETE FROM preb_note_items WHERE item_id = ?", [(item_id,) for item_id in withdrawn])
                self._conn.executemany("DELETE FROM preb_items WHERE item_id = ?", [(item_id,) for item_id in withdrawn])
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return len(withdrawn)

    def _insert_blocked(self, rows: Sequence[Mapping[str, object]]) -> None:
        """拿着锁、在事务里调。接着跑时同一条不记第二遍。"""
        self._conn.executemany(
            "INSERT OR IGNORE INTO preb_blocked (window_id, window_sha, version, context, ref, mention, item, kind, mention_values,"
            " item_values, depends, stage, created_at) VALUES (:window_id, :window_sha, :version, :context, :ref, :mention, :item,"
            " :kind, :mention_values, :item_values, :depends, :stage, :created_at)",
            [dict(row) for row in rows],
        )

    def _preb_withdrawn_items(
        self, packets: Collection[str], sentences: Collection[str], revisions: Collection[tuple[str, int]]
    ) -> set[int]:
        """拿着锁调：名字出现过的句子里有这些内容的项的编号。"""
        if not (packets or sentences or revisions):
            return set()
        return {
            int(row["item_id"])
            for row in self._conn.execute("SELECT item_id, sources FROM preb_items")
            if _sources_hit(row["sources"], packets, sentences, revisions)
        }

    def _preb_note_keys_with(self, item_ids: Collection[int]) -> list[tuple[str, str, str, str]]:
        """拿着锁调：标注里用到了这些项的标注的键（窗口编号, 没加标注的指纹, 版本, context）。"""
        keys: set[tuple[str, str, str, str]] = set()
        for item_id in item_ids:
            keys |= {
                (row[0], row[1], row[2], row[3])
                for row in self._conn.execute(
                    "SELECT window_id, window_sha, version, context FROM preb_note_items WHERE item_id = ?", (int(item_id),)
                )
            }
        return sorted(keys)

    def _preb_noted_with(self, item_ids: Collection[int]) -> set[tuple[str, str]]:
        """拿着锁调：标注里用到了这些项的窗口，(窗口编号, 没加标注的指纹) 和 (窗口编号, B 记下的 noted_sha) 都放进去：
        windows 那一行去掉预处理的标注对上前一种（发出去的文字还带着 A 的〔日子〕、照不同的提示词发过几回的，都对得上），
        没存发出去的文字的行照后一种认（_noted）。"""
        keys = self._preb_note_keys_with(item_ids)
        found = {(window_id, window_sha) for window_id, window_sha, _, _ in keys}
        for window_id, window_sha, version, context in keys:
            row = self._conn.execute(
                "SELECT noted_sha FROM preb_notes WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?",
                (window_id, window_sha, version, context),
            ).fetchone()
            if row is not None:
                found.add((window_id, row["noted_sha"]))
        return found

    def _drop_preb_windows(self, doomed: Sequence[tuple[str, str]]) -> None:
        """拿着锁、在事务里、删 windows 之前调：这些窗口的标注、收名字的记号、拦下的删掉。窗口这一行的指纹是加了标注的
        （对上 noted_sha），没标过的就是原样的（对上 window_sha）。不管可见范围、版本：这个窗口编号、这段文字的都删。"""
        keys: set[tuple[str, str, str, str]] = set()
        for window_id, cache_key in doomed:
            row = self._conn.execute(
                "SELECT window_sha, input_text FROM windows WHERE window_id = ? AND cache_key = ?", (window_id, cache_key)
            ).fetchone()
            if row is None:
                continue
            sha = row["window_sha"]
            plain = {sha, _plain_sha(row["input_text"], sha)} | {  # 去掉预处理标注的那份（还带着 A 的〔日子〕时也对得上）
                found["window_sha"]
                for found in self._conn.execute(
                    "SELECT window_sha FROM preb_notes WHERE window_id = ? AND (noted_sha = ? OR window_sha = ?)",
                    (window_id, sha, sha),
                )
            }
            for table in ("preb_notes", "preb_learned"):
                for one in plain:
                    keys |= {
                        (window_id, one, version, context)
                        for version, context in self._conn.execute(
                            f"SELECT version, context FROM {table} WHERE window_id = ? AND window_sha = ?", (window_id, one)
                        )
                    }
            for one in plain:
                self._conn.execute("DELETE FROM preb_blocked WHERE window_id = ? AND window_sha = ?", (window_id, one))
        self._drop_preb_keys(keys)

    def _drop_preb_keys(self, keys: Collection[tuple[str, str, str, str]]) -> None:
        """拿着锁、在事务里调：这几个（窗口编号, 没加标注的指纹, 版本, context）的标注、用到的项、收名字的记号、拦下的删掉。"""
        for table in ("preb_notes", "preb_note_items", "preb_learned", "preb_blocked"):
            self._conn.executemany(
                f"DELETE FROM {table} WHERE window_id = ? AND window_sha = ? AND version = ? AND context = ?", sorted(keys)
            )

    def vacuum(self) -> bool:
        """彻底删除以后：删掉的内容在文件的空页里还有，整理一遍，日志也清掉。有人正读着、整理不完，返回 False。"""
        with self._lock:
            try:
                self._conn.execute("VACUUM")
            except sqlite3.OperationalError:
                return False
            return self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0


