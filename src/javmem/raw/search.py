"""raw_search 的底层查询（query-api 第 3、4、5.5、6、7、9 节）。

入参和返回都按工具的样子写，查询服务（J5.5，javmem.query）直接调它。
不调模型，不看抽取结果，也不用别名表：别名是抽取的结果，还可能来自看不到的包。
查询词和原句都按统一写法比（D77，javmem.zh.search）：空格、全角半角不算差别，数不会查到更长的数里。

过滤都在 SQL 里，命中、排序和翻页只在这个身份看得到的内容上算。
看不到的和不存在的，返回一模一样。

入参检查、时间、出错、拒绝的日志是五个工具共用的，在 javmem.query.common（J5.5 从这里挪过去的）。
一句原文怎么写成一条结果、一条出处（sentence_item、evidence_of），账本视图（javmem.query.view）也用。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from javmem.access import (
    Identity,
    UnknownIdentity,
    has_any_partition,
    load_identity,
    visible_packet,
)
from javmem.config import ConfigError, Settings
from javmem.ledger import connect_readonly, latest_version, schema_version
from javmem.query.common import (
    FORBIDDEN,
    NOT_FOUND_MESSAGE,
    QUERY_TOO_SHORT,
    UNAVAILABLE,
    UNKNOWN_IDENTITY,
    ToolError,
    can_be_searched,
    check_names,
    cursor_offset,
    failure,
    int_argument,
    invalid,
    log_denied,
    partitions_argument,
    text_argument,
    time_range,
)
from javmem.raw.sentences import MASKED, SECTION_ATTACHMENT, SECTION_RAW
from javmem.timeutil import iso
from javmem.zh.search import search_key

TOOL = "raw_search"

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_CONTEXT = 5
MAX_WORDS = 32
MIN_WORD_LENGTH = 2
TRIGRAM_LENGTH = 3
SHORT_LENGTH = 2
# 两个字的词先找候选、再照 LIKE 核（D196，short_candidates）。候选太多的词照旧 LIKE：一句句按编号取反而比整张读一遍慢。
# raw_search 按时间倒着找够一页就停，常见的词很快找够，超过这么多句就照旧按时间走
RAW_SEARCH_CANDIDATES = 5_000
# 回退原文层（memory_search，任一词就算、命中多的在前）：超过原文表句数的 1/20 照旧整张扫；原文表小的时候至少这么多。
# D196 在 5,000 包那本（242 万句）上量过（热着）：到 1/20（12 万句）候选比整张扫快一倍（0.75 秒对 1.50 秒），到 11% 前后
# 两样一样快；定 1/20 留一倍的余地
FALLBACK_SHARE = 20
FALLBACK_MIN_CANDIDATES = 5_000
_TOP = chr(0x10FFFF)  # 前缀范围的上头：比哪个字都大
_VOCAB = "temp.javmem_raw_norm_vocab"  # 三字索引的词表（fts5vocab），每个连接第一次用时建在临时库里

_ARGUMENTS = {
    "query",
    "limit",
    "partitions",
    "time_from",
    "time_to",
    "source",
    "packet_id",
    "thread_id",
    "speaker",
    "context",
    "cursor",
}
# 一句原文写成一条结果要的列：原文索引的一行，加上它所在的包的来源、渠道、可见范围、抽到哪了
SENTENCE_COLUMNS = "s.*, p.source, p.channel, p.partitions, p.sensitive, p.extraction"
# 这一句所在的那封信，发件人那一句撤回了（对话包没有 message_id，是 NULL，等号永远不成立）
_MASKED_SENDER = (
    "EXISTS (SELECT 1 FROM raw_sentences AS w WHERE w.packet_id = s.packet_id AND w.revision = s.revision"
    " AND w.message_id = s.message_id AND w.field = 'from' AND w.withdrawn_at IS NOT NULL)"
)


@dataclass(frozen=True)
class _Request:
    words: list[str]
    limit: int
    offset: int
    context: int
    partitions: list[str] | None
    time_from: int | None  # 含
    time_to: int | None  # 不含
    source: str | None
    packet_id: str | None
    thread_id: str | None
    speaker: str | None


def _parse_request(arguments: Mapping[str, object], settings: Settings) -> _Request:
    check_names(arguments, _ARGUMENTS)

    query = arguments.get("query")
    if not isinstance(query, str):
        raise invalid("缺 query，或者 query 不是文本。")
    if not can_be_searched(query):
        raise invalid("query 里有不能查的字符。")

    partitions = partitions_argument(arguments)
    offset = cursor_offset(arguments)

    words = query.split()
    if len(words) > MAX_WORDS:
        raise invalid(f"query 最多 {MAX_WORDS} 个词。")
    time_from, time_to = time_range(arguments, settings)
    return _Request(
        words=words,
        limit=int_argument(arguments, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
        offset=offset,
        context=int_argument(arguments, "context", 0, 0, MAX_CONTEXT),
        partitions=partitions,
        time_from=time_from,
        time_to=time_to,
        source=text_argument(arguments, "source"),
        packet_id=text_argument(arguments, "packet_id"),
        thread_id=text_argument(arguments, "thread_id"),
        speaker=text_argument(arguments, "speaker"),
    )


def _folds_the_same(key: str) -> bool:
    """ASCII 和不分大小写的字（汉字、数字、标点）：三字索引换小写（SQLite）和统一写法换小写（Python 的 casefold）两边一样。
    别的分大小写的字不一定（比如切罗基文字 casefold 成大写，SQLite 换成小写），三字索引里的字可能和统一写法对不上。"""
    return all(char.isascii() or char.lower() == char == char.upper() for char in key)


def short_candidates(conn: sqlite3.Connection, key: str, limit: int) -> list[int] | None:
    """一个两个字的词（已经换成统一写法）可能在哪几句里（原文表的编号，从小到大）。只多不少，要照 LIKE 再核（word_condition）：
    - 后面还有字的：三字索引（raw_norm_fts，trigram）里一定有一个以这两个字开头的三字，用 SQLite 自带的 fts5vocab 按前缀
      范围取（只读，建在这个连接的临时库里）。两份写法中间隔着 LANE，前一份末尾的后面有字，算在这一种里；
    - 在整句最后的（后面没有字了，三字索引里没有）：账本 0028 的索引，每句统一写法的最后两个字。
    超过 limit 句回 None，照旧整张扫（一句句按编号取反而比整张读一遍慢，D196 在 242 万句上量的）。不是两个字的、有分大小写的
    非 ASCII 字的（_folds_the_same）也回 None。"""
    if len(key) != SHORT_LENGTH or not _folds_the_same(key):
        return None
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VOCAB} USING fts5vocab(main, raw_norm_fts, instance)")
    found = {row[0] for row in conn.execute(f"SELECT DISTINCT doc FROM {_VOCAB} WHERE term >= ? AND term <= ? LIMIT ?",
                                            (key, key + _TOP, limit + 1))}
    if len(found) <= limit:
        found.update(row[0] for row in conn.execute("SELECT id FROM raw_sentences WHERE substr(norm, -2) = ? LIMIT ?",
                                                    (key, limit + 1)))
    return sorted(found) if len(found) <= limit else None


def fallback_limit(conn: sqlite3.Connection) -> int:
    """回退原文层一个两字词最多走多少句的候选：原文表句数（最大的编号，不用数一遍）的 1/FALLBACK_SHARE，至少
    FALLBACK_MIN_CANDIDATES。"""
    top = conn.execute("SELECT max(id) FROM raw_sentences").fetchone()[0] or 0
    return max(FALLBACK_MIN_CANDIDATES, int(top) // FALLBACK_SHARE)


def word_condition(key: str, candidates: list[int] | None = None) -> tuple[str, list[object]]:
    """一个已经换成统一写法的词命中一句的条件（对着 raw_sentences 的别名 s）：三个字以上走全文索引，两个字走子串匹配。
    两个字的给了候选（short_candidates）就先限在候选里，再照子串核：结果和不给一样，只是不用把原文表整张读一遍（D196）。
    memory_search 回退原文层时一个词一个词地数命中了几个（javmem.query.tools），用的是这一个。"""
    if len(key) >= TRIGRAM_LENGTH:
        return ("s.id IN (SELECT rowid FROM raw_norm_fts WHERE raw_norm_fts MATCH ?)", ['"' + key.replace('"', '""') + '"'])
    escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if candidates is None:
        return ("s.norm LIKE ? ESCAPE '\\'", [f"%{escaped}%"])
    return ("(s.id IN (SELECT value FROM json_each(?)) AND s.norm LIKE ? ESCAPE '\\')",
            [json.dumps(candidates), f"%{escaped}%"])


def _match_conditions(conn: sqlite3.Connection, words: list[str]) -> tuple[list[str], list[object]]:
    """词和原句都按统一写法比（D77，javmem.zh.search）：换完三个字以上走全文索引，两个字走子串匹配（候选不多的先找候选，
    short_candidates）。几个词要命中同一句。"""
    keys = [search_key(word) for word in words]
    conditions = []
    params: list[object] = []
    if any(not key for key in keys):
        conditions.append("0")  # 换完什么都不剩的词，什么都命中不了
    long_words = [key for key in keys if len(key) >= TRIGRAM_LENGTH]
    short_words = [key for key in keys if 0 < len(key) < TRIGRAM_LENGTH]
    if long_words:
        phrases = " AND ".join('"' + word.replace('"', '""') + '"' for word in long_words)
        conditions.append("s.id IN (SELECT rowid FROM raw_norm_fts WHERE raw_norm_fts MATCH ?)")
        params.append(phrases)
    for word in short_words:
        condition, values = word_condition(word, short_candidates(conn, word, RAW_SEARCH_CANDIDATES))
        conditions.append(condition)
        params.extend(values)
    return conditions, params


def _find_rows(conn: sqlite3.Connection, identity: Identity, request: _Request) -> list[sqlite3.Row]:
    conditions, params = visible_packet(identity)
    conditions.append("s.withdrawn_at IS NULL")

    if request.partitions is not None:
        sql, names = has_any_partition(request.partitions)
        conditions.append(sql)
        params.extend(names)

    match_conditions, match_params = _match_conditions(conn, request.words)
    conditions.extend(match_conditions)
    params.extend(match_params)

    for condition, value in (
        ("s.said_at_epoch >= ?", request.time_from),
        ("s.said_at_epoch < ?", request.time_to),
        ("p.source = ?", request.source),
        ("s.packet_id = ?", request.packet_id),
        ("s.thread_id = ?", request.thread_id),
        # 发件人那一句撤回了的信（D73 第 16 条），说话人是“[已撤回]”，按原来的发件人查不到它
        (f"s.speaker = ? AND NOT {_MASKED_SENDER}", request.speaker),
    ):
        if value is not None:
            conditions.append(condition)
            params.append(value)

    # 多取一条，看还有没有下一页
    params.extend([request.limit + 1, request.offset])
    return conn.execute(
        f"SELECT {SENTENCE_COLUMNS}"
        " FROM raw_sentences s JOIN packets p ON p.packet_id = s.packet_id AND p.revision = s.revision"
        f" WHERE {' AND '.join(conditions)}"
        " ORDER BY s.said_at_epoch DESC, s.id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()


def position_of(row: sqlite3.Row) -> dict[str, Any]:
    """query-api 4.5 出处里那一项 position：对话、邮件正文是 {section, line, sentence}，邮件头是 {section, field}，
    附件小节的 section 写 "attachment"。"""
    section = row["section_no"] if row["section_kind"] == SECTION_RAW else SECTION_ATTACHMENT
    if row["field"] is not None:
        return {"section": section, "field": row["field"]}
    return {"section": section, "line": row["line_no"], "sentence": row["sentence_no"]}


def masked_fields(conn: sqlite3.Connection, row: sqlite3.Row) -> set[str]:
    """这一句所在的那封信撤回了主题、发件人的哪几样（按事实撤回，信里别的句子还在，D73 第 16 条）：
    查询结果里这封信每一句的主题、说话人写成“[已撤回]”。对话包是空的。"""
    if row["message_id"] is None:
        return set()
    return {
        found[0]
        for found in conn.execute(
            "SELECT field FROM raw_sentences WHERE packet_id = ? AND revision = ? AND message_id = ?"
            " AND field IN ('subject', 'from') AND withdrawn_at IS NOT NULL",
            (row["packet_id"], row["revision"], row["message_id"]),
        )
    }


def _context(
    conn: sqlite3.Connection, row: sqlite3.Row, size: int, masked: set[str]
) -> dict[str, list[dict[str, str]]]:
    """前后的句子，只在同一个版本的同一个小节里取，撤回的跳过。邮件头不算句子。同一个小节就是同一封信、
    同一块附件，说话人跟着这一句换成“[已撤回]”。"""
    if row["field"] is not None:
        return {"before": [], "after": []}

    def neighbours(comparison: str, order: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT speaker, said_at, origin, text FROM raw_sentences"
            " WHERE packet_id = ? AND revision = ? AND section_kind = ? AND section_no IS ? AND field IS NULL"
            f" AND withdrawn_at IS NULL AND seq {comparison} ? ORDER BY seq {order} LIMIT ?",
            (row["packet_id"], row["revision"], row["section_kind"], row["section_no"], row["seq"], size),
        ).fetchall()

    def shown(rows: list[sqlite3.Row]) -> list[dict[str, str]]:
        return [
            {"speaker": MASKED if "from" in masked else r["speaker"], "said_at": r["said_at"], "origin": r["origin"], "text": r["text"]}
            for r in rows
        ]

    return {"before": shown(list(reversed(neighbours("<", "DESC")))), "after": shown(neighbours(">", "ASC"))}


def evidence_of(row: sqlite3.Row, masked: set[str]) -> dict[str, Any]:
    """一句原文写成 query-api 4.5 的一条出处。row 要有 SENTENCE_COLUMNS 那几列（来源、渠道来自包登记）。"""
    evidence: dict[str, Any] = {
        "packet_id": row["packet_id"],
        "revision": row["revision"],
        "quote": row["text"],
        "speaker": MASKED if "from" in masked else row["speaker"],
        "speaker_role": row["speaker_role"],
        "said_at": row["said_at"],
        "source": row["source"],
        "channel": row["channel"],
        "origin": row["origin"],
        "position": position_of(row),
    }
    if row["message_id"] is not None:
        evidence["email"] = {
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "subject": MASKED if "subject" in masked else row["subject"],
        }
    return evidence


def sentence_item(conn: sqlite3.Connection, row: sqlite3.Row, context: int = 0) -> dict[str, Any]:
    """一句原文写成一条结果（kind raw，5.5 的“结果”那张表）。row 要有 SENTENCE_COLUMNS 那几列。"""
    masked = masked_fields(conn, row)
    item: dict[str, Any] = {
        "id": row["sentence_id"],
        "kind": "raw",
        "content": row["text"],
        "status": "pending",
        "pending_reason": "not_checked",
        "valid_time": {"from": row["said_at"], "to": None, "precision": "minute"},
        "evidence": [evidence_of(row, masked)],
        "evidence_total": 1,
        "scope": {"partitions": json.loads(row["partitions"]), "sensitive": json.loads(row["sensitive"])},
        "secondhand": row["origin"] != "said",
        "extraction": row["extraction"],
        "quoted": bool(row["quoted"]),
    }
    if context > 0:
        item["context"] = _context(conn, row, context, masked)
    return item


def _search(identity: Identity, request: _Request, settings: Settings, as_of: str) -> dict[str, Any]:
    library = settings.library(identity.env)
    conn = connect_readonly(library.db_path)
    try:
        # 一个读事务：命中和上下文都来自同一个时刻。不然查到一半收件换了版本，
        # 上下文就会带上新版本的内容，那个版本这个身份可能看不到
        conn.execute("BEGIN")
        if schema_version(conn) != latest_version():
            raise sqlite3.OperationalError("账本版本不对")
        rows = _find_rows(conn, identity, request)
        more = len(rows) > request.limit
        items = [sentence_item(conn, row, request.context) for row in rows[: request.limit]]
    finally:
        conn.close()

    result: dict[str, Any] = {
        "ok": True,
        "tool": TOOL,
        "library": library.name,
        "as_of": as_of,
        "result": "hit" if items else "none",
        "degraded": [],
        "items": items,
    }
    if not items:
        result["not_found"] = {"reason": "no_match", "searched": ["raw"], "try": [], "message": NOT_FOUND_MESSAGE}
    result["next_cursor"] = f"c{request.offset + request.limit}" if more else None
    return result


def raw_search(arguments: Mapping[str, object], *, settings: Settings) -> dict[str, Any]:
    """原文全文检索。身份来自 settings.caller，也就是环境变量 JAVMEM_CALLER，不是入参。"""
    as_of = iso(settings.now())
    try:
        identity = load_identity(settings)
    except UnknownIdentity:
        log_denied(settings, settings.caller, TOOL, UNKNOWN_IDENTITY, as_of)
        return failure(TOOL, UNKNOWN_IDENTITY, "不认识这个身份。")

    try:
        request = _parse_request(arguments, settings)
        forbidden = identity.forbidden_partitions(request.partitions or [])
        if forbidden:
            # 错误里只提它自己写的分区名
            raise ToolError(FORBIDDEN, f"这个身份不能查分区 {'、'.join(forbidden)}。")
        if not request.words or any(len(word) < MIN_WORD_LENGTH for word in request.words):
            raise ToolError(QUERY_TOO_SHORT, "每个词至少两个字。")
    except ToolError as error:
        if error.code == FORBIDDEN:
            log_denied(settings, identity.name, TOOL, FORBIDDEN, as_of)
        return failure(TOOL, error.code, error.message)

    try:
        return _search(identity, request, settings, as_of)
    except (sqlite3.Error, OSError, ConfigError):
        return failure(TOOL, UNAVAILABLE, "原文层连不上，这次答不了。")
