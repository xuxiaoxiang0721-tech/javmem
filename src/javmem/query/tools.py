"""五个查询工具（J5.5，query-api 第 5 到 9 节）。

入参、返回都照接口文档；身份只看 settings.caller（也就是启动时的 JAVMEM_CALLER），每次调用都重读身份配置。
查不到不算出错；看不到的和不存在的一模一样（D33）。每次调用一个只读连接、一个读事务。

- entity_card、timeline、relation_at 只查账本视图（规矩题 2，主会话 11-18 照建议定：主图是账本的投影、待确认的不进图，
  精确查账本更新更全），searched 照实写 ["ledger"]，主图连不连得上都不影响它们。
- memory_search 先查主图和账本视图（主图检索是 J5.4 的口子，javmem.query.graphhook），查不到再查原文层。
- raw_search 就是原文层的 javmem.raw.raw_search。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from javmem.access import Identity, UnknownIdentity, load_identity
from javmem.config import ConfigError, Settings
from javmem.ledger import connect_readonly, latest_version, schema_version
from javmem.pipeline.extract import RELATION_TYPES
from javmem.pipeline.merge import name_key
from javmem.query import graphhook
from javmem.query.common import (
    FORBIDDEN,
    NOT_FOUND_MESSAGE,
    UNAVAILABLE,
    UNKNOWN_IDENTITY,
    UNKNOWN_RELATION,
    ToolError,
    bool_argument,
    can_be_searched,
    check_names,
    cursor_offset,
    failure,
    int_argument,
    invalid,
    is_date,
    log_denied,
    partitions_argument,
    text_argument,
    time_bound,
    time_range,
)
from javmem.query.graphhook import Hit
from javmem.query.view import NO_NAME, LedgerView, NameMatch, name_key_of, period, said_epoch
from javmem.raw import raw_search
from javmem.raw.search import SENTENCE_COLUMNS, fallback_limit, sentence_item, short_candidates, word_condition
from javmem.timeutil import epoch, iso
from javmem.zh.search import search_key
from javmem.zh.segment import query_words

ENTITY_CARD = "entity_card"
TIMELINE = "timeline"
RELATION_AT = "relation_at"
RAW_SEARCH = "raw_search"
MEMORY_SEARCH = "memory_search"

LEDGER = "ledger"
NEVER = 2**62  # 没有结束：比哪个时间都晚
FOREVER_AGO = -(2**62)


@dataclass(frozen=True)
class _Target:
    name: str | None
    entity_id: str | None


def _target(arguments: Mapping[str, object]) -> _Target:
    """entity、entity_id 二选一（第 5 节）。"""
    name = text_argument(arguments, "entity")
    entity_id = text_argument(arguments, "entity_id")
    if (name is None) == (entity_id is None):
        raise invalid("entity 和 entity_id 要写一个，也只能写一个。")
    if name is not None and not name.strip():
        raise invalid("entity 不能是空的。")
    return _Target(name, entity_id)


def _open(view_args: dict[str, Any], settings: Settings, identity: Identity) -> sqlite3.Connection:
    library = settings.library(identity.env)
    conn = connect_readonly(library.db_path)
    try:
        conn.execute("BEGIN")
        if schema_version(conn) != latest_version():
            raise sqlite3.OperationalError("账本版本不对")
    except BaseException:
        conn.close()
        raise
    view_args["library"] = library.name
    return conn


def run_tool(
    tool: str,
    arguments: Mapping[str, object],
    settings: Settings,
    parse: Callable[[Mapping[str, object], Settings], Any],
    answer: Callable[[LedgerView, Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """几个工具都一样的那一套：先认身份（D33 第一种），再看入参，明着要看不到的分区报 forbidden（第二种），然后在一个读事务
    里答；账本连不上报 unavailable。"""
    now = settings.now()
    as_of = iso(now)
    try:
        identity = load_identity(settings)
    except UnknownIdentity:
        log_denied(settings, settings.caller, tool, UNKNOWN_IDENTITY, as_of)
        return failure(tool, UNKNOWN_IDENTITY, "不认识这个身份。")
    try:
        request = parse(arguments, settings)
        forbidden = identity.forbidden_partitions(getattr(request, "partitions", None) or [])
        if forbidden:
            raise ToolError(FORBIDDEN, f"这个身份不能查分区 {'、'.join(forbidden)}。")  # 错误里只提它自己写的分区名
    except ToolError as error:
        if error.code == FORBIDDEN:
            log_denied(settings, identity.name, tool, FORBIDDEN, as_of)
        return failure(tool, error.code, error.message, **error.extra)
    try:
        envelope: dict[str, Any] = {"ok": True, "tool": tool}
        conn = _open(envelope, settings, identity)
        try:
            view = LedgerView(conn, identity, settings, as_of=now, partitions=getattr(request, "partitions", None))
            envelope["as_of"] = as_of
            return answer(view, request, envelope)
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ConfigError):
        return failure(tool, UNAVAILABLE, "账本连不上，这次答不了。")


def _not_found(reason: str, searched: list[str], try_tools: list[str]) -> dict[str, Any]:
    return {"reason": reason, "searched": searched, "try": try_tools, "message": NOT_FOUND_MESSAGE}


# ---------- 找实体（entity_card、timeline、relation_at 共用） ----------


def _relations_of(
    view: LedgerView, entity_id: str, *, roles: tuple[str, ...] = ("subject", "object", "org"), relation: str | None = None
) -> list[sqlite3.Row]:
    """这个实体是主体、客体或者代表的那一家的、看得到的关系（结束了的也在），带最早一处说法的时间。"""
    conditions, params = view.relation_conditions("r")
    sides = " OR ".join(f"r.{role}_entity_id = ?" for role in roles)
    values: list[object] = [entity_id] * len(roles)
    if relation is not None:
        conditions.append("r.relation_type = ?")
        params = [*params, relation]
    return view.conn.execute(
        "SELECT r.*, (SELECT min(c.said_from_epoch) FROM relation_claims AS c WHERE c.relation_id = r.relation_id) AS first_said"
        f" FROM relations AS r WHERE ({sides}) AND {' AND '.join(conditions)} ORDER BY r.relation_id",
        [*values, *params],
    ).fetchall()


def _facts_of(view: LedgerView, entity_id: str, *, current_only: bool) -> list[sqlite3.Row]:
    """这个实体是主体或者涉及的、看得到的事实，带最早一处说法的时间。"""
    conditions, params = view.fact_conditions("f")
    if current_only:
        current, current_params = view.current_fact_condition("f")
        conditions = [*conditions, current]
        params = [*params, *current_params]
    return view.conn.execute(
        "SELECT f.*, (SELECT min(c.said_from_epoch) FROM fact_claims AS c WHERE c.fact_id = f.fact_id) AS first_said"
        " FROM facts AS f WHERE (f.subject_entity_id = ? OR f.fact_id IN (SELECT x.fact_id FROM fact_entities AS x"
        f" WHERE x.entity_id = ?)) AND {' AND '.join(conditions)} ORDER BY f.fact_id",
        [entity_id, entity_id, *params],
    ).fetchall()


def _fact_at(view: LedgerView, row: sqlite3.Row) -> tuple[int, str]:
    """一条事实排在什么时间（5.3 的 at）：发生时间；不知道的用最早一句证据的时间。"""
    found = period(row["occurred_from"], view.settings.timezone)
    if found is not None:
        return found[0], str(row["occurred_from"])
    said = view.earliest_said("evidence", "fact_id", row["fact_id"])
    if said is not None:
        return said
    moment = said_epoch(row["as_of"])
    return (moment if moment is not None else 0), str(row["as_of"] or "")


def _sort_key_fact(view: LedgerView, row: sqlite3.Row) -> int:
    """排序用的时间（不回原文找最早一句，用说法里记着的最早时间）。"""
    found = period(row["occurred_from"], view.settings.timezone)
    if found is not None:
        return found[0]
    if row["first_said"] is not None:
        return int(row["first_said"])
    return int(row["as_of_epoch"] or 0)


def _current_relations(view: LedgerView, entity_id: str) -> list[sqlite3.Row]:
    """现在的关系（5.2）：现在的状态是 active 或 paused 的，最近有人说的在前。"""
    rows = [row for row in _relations_of(view, entity_id) if view.relation_state(row) in ("active", "paused")]
    return sorted(rows, key=lambda row: (-(row["as_of_epoch"] or 0), row["relation_id"]))


def _hint(view: LedgerView, entity_id: str) -> str:
    """候选的一句提示（5.2）：只用看得到的。先挑一条现在的关系，没有就最近一条事实，再没有写类型。"""
    for row in _current_relations(view, entity_id):
        kind = f"{row['relation_type']}（{row['kinship']}）" if row["kinship"] else str(row["relation_type"])
        if row["subject_entity_id"] == entity_id:
            return f"{kind} {view.entity_name(row['object_entity_id'])}"
        if row["object_entity_id"] == entity_id:
            return f"{view.entity_name(row['subject_entity_id'])} {kind}"
    facts = _facts_of(view, entity_id, current_only=True)
    if facts:
        latest = max(facts, key=lambda row: (row["as_of_epoch"] or 0, row["fact_id"]))
        return view.fact_content(latest)  # 看不到的叫法换掉了的那一份（审阅第 2 条）
    return view.entity_type(entity_id) or ""


def _entity(view: LedgerView, entity_id: str, matched_alias: str | None) -> dict[str, Any]:
    return {"id": entity_id, "name": view.entity_name(entity_id), "type": view.entity_type(entity_id), "matched_alias": matched_alias}


def _resolve(view: LedgerView, target: _Target) -> tuple[str, str | None] | list[NameMatch] | None:
    """对到一个：(编号, 按哪个别名对上的)；对到几个：候选；对不上：None。都只从看得到的里出。"""
    if target.entity_id is not None:
        found = view.find_by_id(target.entity_id)
        return None if found is None else (found, None)
    matches = view.find_by_name(target.name or "")
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].entity_id, matches[0].matched_alias
    return matches


def _candidates(view: LedgerView, matches: list[NameMatch]) -> list[dict[str, Any]]:
    return [
        {
            "entity_id": match.entity_id,
            "name": view.entity_name(match.entity_id),
            "type": view.entity_type(match.entity_id),
            "matched_alias": match.matched_alias,
            "hint": _hint(view, match.entity_id),
        }
        for match in matches
    ]


# ---------- entity_card ----------


@dataclass(frozen=True)
class _CardRequest:
    target: _Target
    recent_limit: int
    partitions: list[str] | None


def _parse_card(arguments: Mapping[str, object], settings: Settings) -> _CardRequest:
    check_names(arguments, {"entity", "entity_id", "recent_limit", "partitions"})
    return _CardRequest(_target(arguments), int_argument(arguments, "recent_limit", 5, 0, 20), partitions_argument(arguments))


def _answer_card(view: LedgerView, request: _CardRequest, out: dict[str, Any]) -> dict[str, Any]:
    resolved = _resolve(view, request.target)
    out["degraded"] = []
    if isinstance(resolved, list):
        out["result"] = "ambiguous"
        out["candidates"] = _candidates(view, resolved)
        return _reorder(out)
    if resolved is None:
        out.update(result="none", aliases=[], relations=[], recent=[])
        out["not_found"] = _not_found("no_entity", [LEDGER], [RAW_SEARCH])
        return _reorder(out)
    entity_id, matched = resolved
    out["result"] = "hit"
    out["entity"] = _entity(view, entity_id, matched)
    shown = name_key(out["entity"]["name"])
    out["aliases"] = [view.alias_item(row) for row in view.aliases_of(entity_id) if row["alias_key"] != shown]
    out["relations"] = [view.relation_item(row) for row in _current_relations(view, entity_id)]
    facts = sorted(
        _facts_of(view, entity_id, current_only=True),
        key=lambda row: (-_sort_key_fact(view, row), -(row["as_of_epoch"] or 0), row["fact_id"]),
    )
    out["recent"] = [view.fact_item(row) for row in facts[: request.recent_limit]]
    from javmem.query.pending import pending_for  # D83：和这个实体有关、这个身份看得到的没处理的待确认（5.2 最后一段）

    out["pending"] = pending_for(view, entity_id)
    return _reorder(out)


_ORDER = ("ok", "tool", "library", "as_of", "result", "answered_from", "degraded", "entity", "range", "at", "candidates",
          "aliases", "relations", "recent", "pending", "items", "not_found", "next_cursor")


def _reorder(out: dict[str, Any]) -> dict[str, Any]:
    """字段照文档例子的先后排（JSON 不在乎先后，人看着顺）。"""
    return {key: out[key] for key in sorted(out, key=lambda key: _ORDER.index(key) if key in _ORDER else len(_ORDER))}


def entity_card(arguments: Mapping[str, object], *, settings: Settings) -> dict[str, Any]:
    """一个人、公司、项目的当前情况：别名、当前关系、最近的事（5.2）。"""
    return run_tool(ENTITY_CARD, arguments, settings, _parse_card, _answer_card)


# ---------- timeline ----------


@dataclass(frozen=True)
class _TimelineRequest:
    target: _Target
    time_from: int
    time_to: int | None
    from_text: str
    to_text: str | None
    include: tuple[str, ...]
    descending: bool
    limit: int
    offset: int
    partitions: list[str] | None


def _parse_timeline(arguments: Mapping[str, object], settings: Settings) -> _TimelineRequest:
    check_names(arguments, {"entity", "entity_id", "time_from", "time_to", "include", "order", "limit", "partitions", "cursor"})
    target = _target(arguments)
    if text_argument(arguments, "time_from") is None:
        raise invalid("缺 time_from。")
    include = arguments.get("include")
    if include is None:  # 写 null 当没写，和别的可选入参一样（审阅第 9 条）
        include = ["fact", "relation"]
    if not (isinstance(include, list) and include and all(kind in ("fact", "relation") for kind in include)):
        raise invalid("include 要写 fact、relation 里的一种或两种。")
    order = text_argument(arguments, "order") or "asc"
    if order not in ("asc", "desc"):
        raise invalid("order 要写 asc 或 desc。")
    partitions = partitions_argument(arguments)
    offset = cursor_offset(arguments)
    start, end = time_range(arguments, settings)
    if end is None and start is not None and start >= epoch(settings.now()):
        raise invalid("time_from 晚于 time_to。")  # 终点没写就是现在（审阅第 9 条）
    return _TimelineRequest(
        target=target,
        time_from=int(start),  # type: ignore[arg-type]
        time_to=end,
        from_text=str(arguments["time_from"]).strip(),
        to_text=text_argument(arguments, "time_to"),
        include=tuple(dict.fromkeys(include)),
        descending=order == "desc",
        limit=int_argument(arguments, "limit", 20, 1, 100),
        offset=offset,
        partitions=partitions,
    )


def _overlaps(span: tuple[int, int], start: int, end: int) -> bool:
    return span[0] < end and start < span[1]


def _relation_events(view: LedgerView, row: sqlite3.Row) -> list[tuple[str, int, int, str]]:
    """一条关系在时间线上的几个点：(change, 起, 止, at 的写法)。开始不知道用最早一句证据的时间；结束了又不知道哪天的，
    用被替代、被纠正的时间，再没有用最早说它结束的那一处的时间。"""
    tz = view.settings.timezone
    events: list[tuple[str, int, int, str]] = []
    start = period(row["valid_from"], tz)
    if start is not None:
        events.append(("started", start[0], start[1], str(row["valid_from"])))
    else:
        said = view.earliest_said("relation_evidence", "relation_id", row["relation_id"])
        if said is not None:
            events.append(("started", said[0], said[0] + 1, said[1]))
    paused = period(row["paused_at"], tz) if row["state"] == "paused" else None
    if paused is not None:
        events.append(("paused", paused[0], paused[1], str(row["paused_at"])))
    end = period(row["valid_to"], tz)
    if end is not None and (row["state"] == "ended" or end[1] <= view.as_of_epoch):
        events.append(("ended", end[0], end[1], str(row["valid_to"])))
    elif end is None and view.relation_state(row) == "ended":
        when = _end_said(view, row)
        if when is not None:
            events.append(("ended", when[0], when[0] + 1, when[1]))
    return events


def _end_said(view: LedgerView, row: sqlite3.Row) -> tuple[int, str] | None:
    for column in ("superseded_at", "corrected_at"):
        moment = said_epoch(row[column])
        if moment is not None:
            return moment, str(row[column])
    found = view.conn.execute(
        "SELECT said_from_epoch, said_from FROM relation_claims WHERE relation_id = ? AND state = 'ended'"
        " ORDER BY said_from_epoch LIMIT 1",
        (row["relation_id"],),
    ).fetchone()
    return None if found is None else (int(found[0]), str(found[1]))


# 同一个时间的几条：旧的先结束、再暂停、新的再开始，事实排在后面（文档 5.3 例子里 11 月 9 日那三条的先后），再按编号
_CHANGE_ORDER = {"ended": 0, "paused": 1, "started": 2}
_FACT_RANK = 3


def _answer_timeline(view: LedgerView, request: _TimelineRequest, out: dict[str, Any]) -> dict[str, Any]:
    end = request.time_to if request.time_to is not None else view.as_of_epoch
    out["degraded"] = []
    resolved = _resolve(view, request.target)
    if isinstance(resolved, list):  # 只有候选，没有结果列表（5.2）
        out["result"] = "ambiguous"
        out["candidates"] = _candidates(view, resolved)
        return _reorder(out)
    out["range"] = {"from": request.from_text, "to": request.to_text.strip() if request.to_text else iso(view.as_of)}
    if resolved is None:
        out.update(result="none", items=[], next_cursor=None)
        out["not_found"] = _not_found("no_entity", [LEDGER], [RAW_SEARCH])
        return _reorder(out)
    entity_id, matched = resolved
    out["entity"] = _entity(view, entity_id, matched)
    events: list[tuple[int, int, str, str, Callable[[], dict[str, Any]], str | None]] = []
    if "fact" in request.include:
        for row in _facts_of(view, entity_id, current_only=False):  # 被替代的也带（规矩题 3，主会话 11-18 照建议定）
            moment, text = _fact_at(view, row)
            span = period(row["occurred_from"], view.settings.timezone) or (moment, moment + 1)
            if _overlaps(span, request.time_from, end):
                events.append((moment, _FACT_RANK, row["fact_id"], text, lambda row=row: view.fact_item(row), None))
    if "relation" in request.include:
        for row in _relations_of(view, entity_id):
            for change, low, high, text in _relation_events(view, row):
                if _overlaps((low, high), request.time_from, end):
                    events.append((low, _CHANGE_ORDER[change], row["relation_id"], text,
                                   lambda row=row: view.relation_item(row), change))
    events.sort(key=lambda one: (one[0], one[1], one[2]), reverse=request.descending)
    page = events[request.offset : request.offset + request.limit]
    items = []
    for _moment, _rank, item_id, text, build, change in page:
        item = build()
        shaped = {"id": item_id, "kind": item["kind"], "at": text}
        if change is not None:
            shaped["change"] = change
        shaped.update((key, value) for key, value in item.items() if key not in ("id", "kind"))
        items.append(shaped)
    out["items"] = items
    out["result"] = "hit" if items else "none"
    if not items:
        out["not_found"] = _not_found("no_events_in_range", [LEDGER], [RAW_SEARCH])
    more = len(events) > request.offset + request.limit
    out["next_cursor"] = f"c{request.offset + request.limit}" if more else None
    return _reorder(out)


def timeline(arguments: Mapping[str, object], *, settings: Settings) -> dict[str, Any]:
    """一个实体一段时间里发生的事、关系怎么变的（5.3）。按事情发生的时间排，不按包到的时间排。"""
    return run_tool(TIMELINE, arguments, settings, _parse_timeline, _answer_timeline)


# ---------- relation_at ----------


@dataclass(frozen=True)
class _RelationAtRequest:
    target: _Target
    relation: str
    at_text: str
    at_start: int
    at_end: int
    whole_day: bool
    roles: tuple[str, ...]
    partitions: list[str] | None


def _parse_relation_at(arguments: Mapping[str, object], settings: Settings) -> _RelationAtRequest:
    check_names(arguments, {"entity", "entity_id", "relation", "at", "role", "partitions"})
    target = _target(arguments)
    relation = text_argument(arguments, "relation")
    if relation is None:
        raise invalid("缺 relation。")
    at_text = text_argument(arguments, "at")
    if at_text is None:
        raise invalid("缺 at。")
    role = text_argument(arguments, "role") or "any"
    if role not in ("subject", "object", "any"):
        raise invalid("role 要写 subject、object 或 any。")
    partitions = partitions_argument(arguments)
    moment = time_bound("at", at_text, settings)
    assert moment is not None
    whole_day = is_date(at_text)
    try:
        finish = moment + (timedelta(days=1) if whole_day else timedelta(seconds=1))
    except OverflowError:
        raise invalid("at 的时间格式不对。") from None
    if relation not in RELATION_TYPES:
        raise ToolError(UNKNOWN_RELATION, "关系名不在关系清单里。", allowed=list(RELATION_TYPES))
    return _RelationAtRequest(
        target=target,
        relation=relation,
        at_text=at_text.strip(),
        at_start=epoch(moment),
        at_end=epoch(finish),
        whole_day=whole_day,
        roles=("subject", "object") if role == "any" else (role,),
        partitions=partitions,
    )


def _day_of(span: tuple[int, int], view: LedgerView) -> tuple[int, int]:
    """按那一天比（5.4）：比一天还细的时间换成它所在的那一天（服务配置的时区）。"""
    if span[1] - span[0] >= 86400:
        return span
    moment = datetime.fromtimestamp(span[0], view.settings.timezone)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return epoch(start), epoch(start + timedelta(days=1))


@dataclass(frozen=True)
class _Bounds:
    start: tuple[int, int]  # 开始在这一段里
    end: tuple[int, int]  # 结束在这一段里（[起, 止)：起以前一定还在，止以后一定不在了）
    corrected: bool  # 被主人纠正掉的（J4.6）


def _bounds(view: LedgerView, row: sqlite3.Row, whole_day: bool) -> _Bounds:
    tz = view.settings.timezone
    start = period(row["valid_from"], tz)
    if start is None:  # 开始不知道：最早一处说法的时候一定已经开始了，再早说不准
        first = row["first_said"] if row["first_said"] is not None else (said_epoch(row["as_of"]) or 0)
        start = (FOREVER_AGO, int(first) + 1)
    elif whole_day:
        start = _day_of(start, view)
    end = period(row["valid_to"], tz)
    corrected = row["corrected_at"] is not None and view.relation_state(row) == "ended"
    if end is not None:
        if whole_day:
            end = _day_of(end, view)
    elif view.relation_state(row) == "ended":  # 结束了，不知道哪天：最后有人说它还在的时候以前一定还在
        last = view.conn.execute(
            "SELECT max(said_to_epoch) FROM relation_claims WHERE relation_id = ? AND state IN ('active', 'paused')",
            (row["relation_id"],),
        ).fetchone()[0]
        ended = _end_said(view, row)
        high = ended[0] if ended is not None else (said_epoch(row["as_of"]) or 0)
        low = min(int(last), high) if last is not None else high
        end = (low, max(high, low))
    else:
        end = (NEVER, NEVER)
    return _Bounds(start, end, corrected)


def _time_match(bounds: _Bounds, start: int, end: int, whole_day: bool) -> str | None:
    """certain：开始那一段整个在问的时间以前（按天问时，开始那一段在问的那一天结束以前就完了的也算：那一天它已经在了，
    审阅第 5 条）、结束那一段整个在以后；possible：照精度说不准的。被纠正掉的最多 possible，纠正那一句以后不算
    （规矩题 4，主会话 11-18 照建议定）。"""
    began_surely = bounds.start[1] <= start or (whole_day and bounds.start[1] <= end)
    began_maybe = bounds.start[0] < end
    if bounds.end == (start, end):  # 结束那天就是问的那一天：不含 to，那一天不算
        lasted_surely = lasted_maybe = False
    else:
        lasted_surely = end <= bounds.end[0]
        lasted_maybe = start < bounds.end[1]
    if not (began_maybe and lasted_maybe):
        return None
    if bounds.corrected:
        return "possible"
    return "certain" if began_surely and lasted_surely else "possible"


def _answer_relation_at(view: LedgerView, request: _RelationAtRequest, out: dict[str, Any]) -> dict[str, Any]:
    out["degraded"] = []
    resolved = _resolve(view, request.target)
    if isinstance(resolved, list):  # 只有候选，没有结果列表（5.2）
        out["result"] = "ambiguous"
        out["candidates"] = _candidates(view, resolved)
        return _reorder(out)
    out["at"] = request.at_text
    if resolved is None:
        out.update(result="none", items=[])
        out["not_found"] = _not_found("no_entity", [LEDGER], [RAW_SEARCH])
        return _reorder(out)
    entity_id, matched = resolved
    out["entity"] = _entity(view, entity_id, matched)
    rows = _relations_of(view, entity_id, roles=request.roles, relation=request.relation)
    hits: list[tuple[int, int, str, sqlite3.Row, str]] = []
    before: tuple[int, str, sqlite3.Row] | None = None
    after: tuple[int, str, sqlite3.Row] | None = None
    for row in rows:
        bounds = _bounds(view, row, request.whole_day)
        match = _time_match(bounds, request.at_start, request.at_end, request.whole_day)
        if match is not None:
            hits.append((0 if match == "certain" else 1, -bounds.start[0], row["relation_id"], row, match))
            continue
        if bounds.start[0] >= request.at_end:
            if after is None or (bounds.start[0], row["relation_id"]) < (after[0], after[1]):
                after = (bounds.start[0], row["relation_id"], row)
        elif bounds.end[1] <= request.at_start or bounds.end == (request.at_start, request.at_end):
            if before is None or (bounds.end[1], row["relation_id"]) > (before[0], before[1]):
                before = (bounds.end[1], row["relation_id"], row)
    hits.sort(key=lambda one: one[:3])
    items = []
    for *_order, row, match in hits:
        item = view.relation_item(row)
        items.append({"id": item["id"], "kind": item["kind"], "time_match": match,
                      **{key: value for key, value in item.items() if key not in ("id", "kind")}})
    out["items"] = items
    out["result"] = "hit" if items else "none"
    if not items:
        found = _not_found("no_relation_at_time", [LEDGER], [RAW_SEARCH])
        found["nearest"] = {
            "before": view.relation_item(before[2]) if before else None,
            "after": view.relation_item(after[2]) if after else None,
        }
        out["not_found"] = found
    return _reorder(out)


def relation_at(arguments: Mapping[str, object], *, settings: Settings) -> dict[str, Any]:
    """某个时间点某种关系是谁（5.4）。items[].relation.state 是现在的状态，不是那个时间点的。"""
    return run_tool(RELATION_AT, arguments, settings, _parse_relation_at, _answer_relation_at)


# ---------- memory_search ----------

MAX_QUERY = 500
MAX_CANDIDATES = 1000  # 账本视图一次最多拿多少条候选：都是看得到的，最近有人说的先拿
MAX_SCAN = 20000  # 按时间筛的时候一次最多拿多少条（时间要在 Python 里换算）
MAX_PAGES = 10  # 没认出实体、按词找时，换掉看不到的叫法以后对得上的不够上限那么多条，最多往下取几页（照 J5.4 全文那一路）
GRAPH_MARGIN = 20  # 问主图时多要的几条：图慢一拍时有几条在账本里已经撤回、被替代
GRAPH = "graph"
RAW = "raw"


@dataclass(frozen=True)
class _SearchRequest:
    query: str
    limit: int
    offset: int
    partitions: list[str] | None
    time_from: int | None
    time_to: int | None
    include_history: bool


def _parse_search(arguments: Mapping[str, object], settings: Settings) -> _SearchRequest:
    check_names(arguments, {"query", "limit", "partitions", "time_from", "time_to", "include_history", "cursor"})
    query = arguments.get("query")
    if not isinstance(query, str):
        raise invalid("缺 query，或者 query 不是文本。")
    if not can_be_searched(query):
        raise invalid("query 里有不能查的字符。")
    if not 1 <= len(query) <= MAX_QUERY:
        raise invalid(f"query 要 1 到 {MAX_QUERY} 个字。")
    partitions = partitions_argument(arguments)
    offset = cursor_offset(arguments)
    start, end = time_range(arguments, settings)
    return _SearchRequest(
        query=query,
        limit=int_argument(arguments, "limit", 10, 1, 50),
        offset=offset,
        partitions=partitions,
        time_from=start,
        time_to=end,
        include_history=bool_argument(arguments, "include_history", False),
    )


def _mentioned(view: LedgerView, question: str) -> tuple[list[str], set[str]]:
    """问话里提到了哪些实体：这个身份看得到的名字（标准名照规矩题 1、别名要有看得到的出处）里，比对写法出现在问话里的、
    两个字以上的；长的盖住短的（“越南那批订单”里的“越南”不另算）。返回 (实体编号, 认出来的那几个名字的比对写法)。"""
    text = name_key_of(question)
    if not text:
        return [], set()
    found: dict[str, set[str]] = {}
    alias_sql, params = view.alias_visible_sql("a")
    for row in view.conn.execute(
        "SELECT a.entity_id, a.alias_key FROM aliases AS a"
        f" WHERE length(a.alias_key) >= 2 AND instr(?, a.alias_key) > 0 AND {alias_sql}",
        [text, *params],
    ):
        found.setdefault(str(row["alias_key"]), set()).add(str(row["entity_id"]))
    for row in view.conn.execute("SELECT entity_id, canonical_name FROM entities"):
        key = name_key_of(str(row["canonical_name"]))
        if len(key) < 2 or key not in text:
            continue
        entity = str(row["entity_id"])
        if view.entity_visible(entity) and view.entity_name(entity) == row["canonical_name"]:
            found.setdefault(key, set()).add(entity)
    keys = {key for key in found if not any(key != other and key in other for other in found)}
    return sorted({entity for key in keys for entity in found[key]}), keys


def _dictionary(view: LedgerView) -> list[str]:
    """切词的词典，一个身份一份：别名表（JAVMEM_LEXICON_FILE）里规矩题 1 下这个身份看得到的名字（它算数的那些组的标准名、
    Bot 名单的标准名）、words（Lexicon.words_for），加上这个身份看得到的别名（javmem.zh.segment 说的“账本里看得到的别名”；
    组里别的写法有看得到的出处的，从这里进来）。

    它看不到的名字不放（J5.4 复审顺带看到的；隐私两处的审阅 R5、R6）：一个看不到的名字在不在词典里，切出来不一样、答的
    跟着变，就能试出别名表里有没有它。只看身份、不按入参 partitions 收窄（入参只筛结果；放进来的都是这个身份看得到的）。
    只用来切问话，不拿别名去扩展查询。"""
    lexicon = view.lexicon()
    words = list(lexicon.words_for(view.identity.partitions)) if lexicon is not None else []
    alias_sql, params = view.alias_visible_sql("a", requested=False)
    words.extend(
        str(row[0])
        for row in view.conn.execute(f"SELECT DISTINCT a.alias FROM aliases AS a WHERE {alias_sql} ORDER BY a.alias", params)
    )
    return words


def _contains(text: str, word: str) -> bool:
    key = name_key_of(word)
    return bool(key) and key in name_key_of(text)


def _words_in(text: str, words: Sequence[str]) -> int:
    """对上几个词。text 是照看不到的叫法换掉了的那一份（审阅第 2 条）；里面的“[没有看得到的叫法]”不是记忆里的字，不算
    （隔开，两边的字也不会接起来凑出一个词；J5.5 隐私两处的审阅 R1b）。"""
    if NO_NAME in text:
        text = text.replace(NO_NAME, "\x00")  # 问话里不能有控制字符（common.can_be_searched），对不上它
    return sum(_contains(text, word) for word in words)


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _gain_facts(view: LedgerView, word: str) -> list[str]:
    """换掉看不到的叫法以后会多出这个词的事实的编号（J5.5 隐私两处的审阅 R1）：牵涉的实体现在写的名字（view.entity_name，
    换上去的就是它）里有这个词、这个实体又有这个身份看不到的叫法，事实原样的字里（照比对写法，和 mask_names 一样）写着其中
    一个。原样的字里没有这个词、换上去的名字里有的，不然 SQL 按原样的字数词时就漏了：L3 字里写的是看不到的全名还是看得到的
    名字，看起来一字不差，查不查得到却不一样。

    先按实体的索引把这些实体牵涉的事实拿出来、在这里挑（复审 N1：原来每个实体给 SQL 拼一段 OR，实体一多每一行都要算一遍，
    过了一千段 SQLite 报表达式太深），SQL 里一个词只多一个 IN。只放宽 SQL 那一关，名次照换过的字数词定；看得到、现行这些
    还是 SQL 里筛。只认整个落在换上去的名字里面的词；跨着它的边界凑出来的、换上去的名字要照比对写法才有这个词的认不出
    （找候选的实体按原样的写法 LIKE，见开发说明）。"""
    key = name_key_of(word)
    if not key:
        return []
    pattern = _like(word)
    hidden_of: dict[str, frozenset[str]] = {}
    for (entity_id,) in view.conn.execute(  # 现在写的名字是标准名或者一个别名：先按原样的写法找，再照比对写法核
        "SELECT entity_id FROM entities WHERE canonical_name LIKE ? ESCAPE '\\'"
        " UNION SELECT entity_id FROM aliases WHERE alias LIKE ? ESCAPE '\\' ORDER BY 1",
        [pattern, pattern],
    ):
        entity_id = str(entity_id)
        hidden = view.name_keys(entity_id)[1]  # 名字、叫法一次调用里按实体记着，几个词共用
        if not hidden:
            continue
        shown = view.entity_name(entity_id)
        if shown != NO_NAME and key in name_key_of(shown):
            hidden_of[entity_id] = hidden
    if not hidden_of:
        return []
    ids = json.dumps(sorted(hidden_of))
    found: set[str] = set()
    for row in view.conn.execute(
        "SELECT f.fact_id, f.content, f.subject_entity_id AS entity_id FROM facts AS f"
        " WHERE f.subject_entity_id IN (SELECT value FROM json_each(?)) AND f.withdrawn_at IS NULL"
        " UNION ALL SELECT f.fact_id, f.content, x.entity_id FROM fact_entities AS x JOIN facts AS f ON f.fact_id = x.fact_id"
        " WHERE x.entity_id IN (SELECT value FROM json_each(?)) AND f.withdrawn_at IS NULL",
        [ids, ids],
    ):
        text = name_key_of(str(row["content"] or ""))
        if any(one in text for one in hidden_of[str(row["entity_id"])]):
            found.add(str(row["fact_id"]))
    return sorted(found)


def _in_range(span: tuple[int, int], request: _SearchRequest) -> bool:
    start = request.time_from if request.time_from is not None else FOREVER_AGO
    end = request.time_to if request.time_to is not None else NEVER
    return _overlaps(span, start, end)


def _fact_span(view: LedgerView, row: sqlite3.Row) -> tuple[int, int]:
    """事实的时间（只要这段时间内发生的）：发生时间；不知道的按最早一处说法。"""
    found = period(row["occurred_from"], view.settings.timezone)
    if found is not None:
        return found
    moment = _sort_key_fact(view, row)
    return moment, moment + 1


def _relation_span(view: LedgerView, row: sqlite3.Row) -> tuple[int, int]:
    """关系生效的那一段（只要这段时间内生效的）：从开始的时候，到结束最晚可能的时候（审阅第 4、5 条）。
    - 开始不知道的，从第一次有人说起它算（再早说不准，筛的时候不算进去）。
    - 结束日子写到天（或者更细）的，那一天已经不算（4.4：含 from 不含 to）；写到月、季、年的，那一段里都可能还在。"""
    bounds = _bounds(view, row, False)
    low = bounds.start[0]
    if low == FOREVER_AGO:
        low = bounds.start[1] - 1
    end = period(row["valid_to"], view.settings.timezone)
    high = end[0] if end is not None and end[1] - end[0] <= 86400 else bounds.end[1]
    return low, max(high, low + 1)


def _fact_ok(view: LedgerView, row: sqlite3.Row, request: _SearchRequest) -> bool:
    return _in_range(_fact_span(view, row), request)


def _relation_ok(view: LedgerView, row: sqlite3.Row, request: _SearchRequest) -> bool:
    if not request.include_history and view.relation_state(row) == "ended":
        return False
    return _in_range(_relation_span(view, row), request)


_FACT_COLUMNS = "f.*, (SELECT min(c.said_from_epoch) FROM fact_claims AS c WHERE c.fact_id = f.fact_id) AS first_said"
_RELATION_COLUMNS = (
    "r.*, (SELECT min(c.said_from_epoch) FROM relation_claims AS c WHERE c.relation_id = r.relation_id) AS first_said"
)


def _fact_filters(view: LedgerView, request: _SearchRequest) -> tuple[list[str], list[object]]:
    conditions, params = view.fact_conditions("f")
    if not request.include_history:
        current, current_params = view.current_fact_condition("f")
        conditions, params = [*conditions, current], [*params, *current_params]
    return conditions, params


def _ledger_hits(view: LedgerView, request: _SearchRequest, words: list[str], seeds: list[str], names: set[str]) -> list[Hit]:
    """账本视图自己找（主图接上以前就是它；接上以后交给检索器合）：提到了实体的拿它们的事实和关系；一个都没提到的拿词去比
    现行事实的文字，至少对上一半的词才算。排：提到的实体是主体、客体的在前，再按对上几个词，一样的最近有人说的在前，再按编号。

    候选按最近有人说的先拿，一次最多 MAX_CANDIDATES 条；按时间筛的时候一次最多 MAX_SCAN 条（时间要在 Python 里换算，
    先截再筛会把对得上的悄悄丢掉，审阅第 7 条）。能写进 SQL 的（看得到、现行、没结束、对上一半的词）都在截之前筛；
    按词找的那一种，字里写着看不到的叫法、只靠它才对上的不占名额（_word_pool）。"""
    plain = [word for word in words if not any(name_key_of(word) in name or name in name_key_of(word) for name in names)]
    scored: list[tuple[float, int, str, str]] = []
    marks = ", ".join("?" for _ in seeds)
    cap = MAX_SCAN if request.time_from is not None or request.time_to is not None else MAX_CANDIDATES
    if seeds:
        conditions, params = _fact_filters(view, request)
        rows = view.conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts AS f WHERE (f.subject_entity_id IN ({marks}) OR f.fact_id IN"
            f" (SELECT x.fact_id FROM fact_entities AS x WHERE x.entity_id IN ({marks}) AND x.role IN ('subject', 'object')))"
            f" AND {' AND '.join(conditions)} ORDER BY f.as_of_epoch DESC, f.fact_id LIMIT ?",
            [*seeds, *seeds, *params, cap],
        ).fetchall()
        main = {row["fact_id"] for row in rows}
        rows += view.conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts AS f WHERE f.fact_id IN (SELECT x.fact_id FROM fact_entities AS x"
            f" WHERE x.entity_id IN ({marks})) AND {' AND '.join(conditions)} ORDER BY f.as_of_epoch DESC, f.fact_id LIMIT ?",
            [*seeds, *params, cap],
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            if row["fact_id"] in seen or not _fact_ok(view, row, request):
                continue
            seen.add(row["fact_id"])
            weight = 2 if row["fact_id"] in main else 1
            score = weight + _words_in(view.fact_content(row), plain)  # 照看不到的叫法换掉了的那一份比（审阅第 2 条）
            scored.append((score, int(row["as_of_epoch"] or 0), "fact", row["fact_id"]))
        conditions, params = view.relation_conditions("r")
        if not request.include_history:
            conditions = [*conditions, "r.state != 'ended'"]  # 写账时就结束了的；到期日过了的在下面再看（relation_state）
        for row in view.conn.execute(
            f"SELECT {_RELATION_COLUMNS} FROM relations AS r WHERE (r.subject_entity_id IN ({marks})"
            f" OR r.object_entity_id IN ({marks}) OR r.org_entity_id IN ({marks})) AND {' AND '.join(conditions)}"
            " ORDER BY r.as_of_epoch DESC, r.relation_id LIMIT ?",
            [*seeds, *seeds, *seeds, *params, cap],
        ):
            if not _relation_ok(view, row, request):
                continue
            weight = 2 if row["subject_entity_id"] in seeds or row["object_entity_id"] in seeds else 1
            score = weight + _words_in(view.relation_content(row), plain)
            scored.append((score, int(row["as_of_epoch"] or 0), "relation", row["relation_id"]))
    elif plain:
        # 至少对上一半的词才算（一个、两个词的对上一个就算）：一句话里只有一个常见词（“没有”）对得上的，不拿事实凑。
        # 回退原文层照文档还是任一词命中就算（5.1 第 2 步）
        needed = (len(plain) + 1) // 2
        conditions, params = _fact_filters(view, request)
        # 对上几个词先在 SQL 里数（截之前筛）：原样的字里有这个词，或者换掉看不到的叫法以后会有（_gain_facts 挑好的编号）；
        # 换掉以后在 _word_pool 里照换过的那一份再数一遍
        terms: list[str] = []
        term_params: list[object] = []
        for word in plain:
            gains = _gain_facts(view, word)
            if gains:
                terms.append("(f.content LIKE ? ESCAPE '\\' OR f.fact_id IN (SELECT value FROM json_each(?)))")
                term_params += [_like(word), json.dumps(gains)]
            else:
                terms.append("(f.content LIKE ? ESCAPE '\\')")
                term_params.append(_like(word))
        where = f"({' + '.join(terms)}) >= ? AND {' AND '.join(conditions)}"
        for score, said, fact_id in _word_pool(view, request, plain, needed, where, [*term_params, needed, *params], cap):
            scored.append((score, said, "fact", fact_id))
    scored.sort(key=lambda one: (-one[0], -one[1], one[2], one[3]))
    return [Hit(kind, item_id, float(score)) for score, _said, kind, item_id in scored]


def _word_pool(
    view: LedgerView, request: _SearchRequest, plain: list[str], needed: int, where: str, params: list[object], cap: int
) -> list[tuple[int, int, str]]:
    """没认出实体时按词找的池子：(对上几个词, 最近有人说的时刻, 事实编号)，最近有人说的在前。

    where 是 SQL 里先数的那一关：原样的字里有这个词、或者换掉看不到的叫法以后会有的，够一半（截之前筛）。一页 cap 条，
    最近有人说的先拿；这里照看不到的叫法换掉了的那一份再数一遍（审阅第 2 条），还对得上一半的才算进 cap 条。字里写着看不到
    的叫法、只靠它才对上的那些换掉以后对不上，不占名额（J5.4 复审顺带看到的）：一页满了、算进去的又不够 cap 条，接着上一页
    最后一条往下取一页，最多 MAX_PAGES 页；够了就截。SQL 那一关把换掉以后才对得上的也放进来（审阅 R1），所以挑出来的
    就是“字里看不到的叫法都换过了”时按换过的字数词、最近有人说的那 cap 条，和带着看不到的叫法的有多少条无关（照 J5.4 全文
    那一路）。认不出的两种：跨着换上去的名字的边界凑出来的词；取到 MAX_PAGES 页还不够的，后面的进不来（对上的词更多也进不来，
    前几名可能变），标准错误记一行。时间段在算完名额以后再筛：按时间筛时 cap 是要看的条数，和以前一样，时间段对不上的也占
    名额。"""
    counted: list[tuple[int, int, str, bool]] = []  # (对上几个词, 时刻, 编号, 时间段对不对得上)：算进名额的，先后照 SQL
    after: tuple[object, str] | None = None  # 上一页最后一条（最近有人说的时刻、编号）
    full = False
    for _page in range(MAX_PAGES):
        page_where, page_params = where, list(params)
        if after is not None:  # 接着上一页最后一条往下（先后照 as_of_epoch 新的在前、空的在最后，再按编号）
            moment, last = after
            if moment is None:
                page_where += " AND f.as_of_epoch IS NULL AND f.fact_id > ?"
                page_params.append(last)
            else:
                page_where += " AND (f.as_of_epoch < ? OR (f.as_of_epoch = ? AND f.fact_id > ?) OR f.as_of_epoch IS NULL)"
                page_params += [moment, moment, last]
        rows = view.conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts AS f WHERE {page_where} ORDER BY f.as_of_epoch DESC, f.fact_id LIMIT ?",
            [*page_params, cap],
        ).fetchall()
        if rows:
            after = (rows[-1]["as_of_epoch"], str(rows[-1]["fact_id"]))
        full = len(rows) >= cap
        # 这一页满了（要看够不够、要不要往下取）或者可能截在这一页里，时间段对不上的也要数清楚占不占名额；不然它们怎样都
        # 不影响留下哪几条，不用换名字（和以前一样省事）
        exact = full or len(counted) + len(rows) > cap
        todo = [(row, in_range) for row in rows if (in_range := _fact_ok(view, row, request)) or exact]
        entities = view.facts_entities([row for row, _ in todo])
        for row, in_range in todo:
            score = _words_in(view.fact_content(row, entities[str(row["fact_id"])]), plain)
            if score >= needed:
                counted.append((score, int(row["as_of_epoch"] or 0), str(row["fact_id"]), in_range))
                if len(counted) >= cap:  # 够了：后面的怎样都排不进这 cap 条
                    break
        if not full or len(counted) >= cap:
            break
    if full and len(counted) < cap:
        print(f"javmem 查询：账本视图按词找取到 {MAX_PAGES} 页还不够，这次少几条", file=sys.stderr, flush=True)
    return [(score, said, fact_id) for score, said, fact_id, in_range in counted if in_range]


def _valid_row(view: LedgerView, hit: Hit, request: _SearchRequest) -> sqlite3.Row | None:
    """一条名次回账本对一遍：还在、看得到、现行（没开 include_history）、时间对得上。图慢一拍时有对不上的，丢掉。"""
    if hit.kind == "fact":
        conditions, params = _fact_filters(view, request)
        row = view.conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts AS f WHERE f.fact_id = ? AND {' AND '.join(conditions)}", [hit.id, *params]
        ).fetchone()
        return row if row is not None and _fact_ok(view, row, request) else None
    if hit.kind == "relation":
        conditions, params = view.relation_conditions("r")
        row = view.conn.execute(
            f"SELECT {_RELATION_COLUMNS} FROM relations AS r WHERE r.relation_id = ? AND {' AND '.join(conditions)}",
            [hit.id, *params],
        ).fetchone()
        return row if row is not None and _relation_ok(view, row, request) else None
    return None


def _visible_scopes(view: LedgerView) -> tuple[graphhook.Scope, ...]:
    """这个身份看得到的可见范围：账本里现行的包有的那几种，写法和事实、关系的 scope_* 两列一样（排好序、去了重）。"""
    conditions, params = view.packet_conditions("p")
    scopes = {
        (tuple(sorted(set(json.loads(row[0])))), tuple(sorted(set(json.loads(row[1])))))
        for row in view.conn.execute(
            f"SELECT DISTINCT p.partitions, p.sensitive FROM packets AS p WHERE {' AND '.join(conditions)}", params
        )
    }
    return tuple(sorted(scopes))


_CHUNK = 500  # 按编号查的时候一次多少个（SQLite 一句的参数有上限）


def _hidden_in(view: LedgerView, items: Sequence[tuple[str, str, str]]) -> set[tuple[str, str]]:
    """主图检索交来的几条（哪一样、编号、图里拿去比字、算向量的那段字）里信不过的那几条（graphhook 规矩第 6 条），照账本
    现在的样子算：
    - 账本里没有了、撤回了的（图慢一拍时还在图里，不能让它们占名额：复审第 2 条）；
    - 事实：图里的字和账本现在的不一样（图里的字过时了，旧字里撤回了的名字、词账本已经不认识：复审第 1 条）；
    - 字里写着这个身份看不到的叫法的（view.shown_text 换得动就是有）。叫法只认这一条牵涉的实体的（事实的主体、涉及的，
      关系的两头和代表哪一家），和返回时换名字的是同一套。"""
    ends: dict[tuple[str, str], list[str]] = {}
    current: dict[tuple[str, str], str | None] = {}  # 事实：账本里现在的字；关系：None（照图里的算不出来，靠上面撤回那一条）
    gone: set[tuple[str, str]] = set()
    for kind, table, column, columns in (
        ("fact", "facts", "fact_id", "subject_entity_id, content, withdrawn_at"),
        ("relation", "relations", "relation_id", "subject_entity_id, object_entity_id, org_entity_id, withdrawn_at"),
    ):
        ids = sorted({item_id for one, item_id, _ in items if one == kind})
        for start in range(0, len(ids), _CHUNK):
            part = ids[start : start + _CHUNK]
            marks = ", ".join("?" for _ in part)
            for row in view.conn.execute(f"SELECT {column}, {columns} FROM {table} WHERE {column} IN ({marks})", part):
                key = (kind, str(row[0]))
                if row["withdrawn_at"] is not None:
                    gone.add(key)
                if kind == "fact":
                    ends[key] = [str(row["subject_entity_id"])] if row["subject_entity_id"] else []
                    current[key] = str(row["content"] or "")
                else:
                    ends[key] = [str(row[name]) for name in ("subject_entity_id", "object_entity_id", "org_entity_id") if row[name]]
                    current[key] = None
            if kind == "fact":
                for row in view.conn.execute(f"SELECT fact_id, entity_id FROM fact_entities WHERE fact_id IN ({marks})", part):
                    ends.setdefault(("fact", str(row[0])), []).append(str(row[1]))
    found: set[tuple[str, str]] = set()
    for kind, item_id, text in items:
        key = (kind, item_id)
        entities = ends.get(key)
        if entities is None or key in gone:
            found.add(key)
        elif current.get(key) is not None and text != current[key]:
            found.add(key)
        elif text and view.shown_text(text, entities) != text:
            found.add(key)
    return found


def _raw_hits(view: LedgerView, words: list[str], request: _SearchRequest) -> tuple[list[dict[str, Any]], bool]:
    """回退原文层（5.1 第 2 步）：任一词命中就算，按命中的词数排，一样多时新的在前。返回 (这一页, 还有没有下一页)。
    两个字的词候选不多的先找候选（raw.search.short_candidates）：几个词都有索引可走时原文表不用整张读（D196）。"""
    keys = list(dict.fromkeys(key for key in (search_key(word) for word in words) if key))
    if not keys:
        return [], False
    limit = fallback_limit(view.conn)
    pieces = [word_condition(key, short_candidates(view.conn, key, limit)) for key in keys]
    score = " + ".join(f"({sql})" for sql, _ in pieces)
    score_params = [value for _, values in pieces for value in values]
    conditions, params = view.packet_conditions("p")
    conditions = [*conditions, "s.withdrawn_at IS NULL", "(" + " OR ".join(f"({sql})" for sql, _ in pieces) + ")"]
    params = [*params, *score_params]
    for condition, value in (("s.said_at_epoch >= ?", request.time_from), ("s.said_at_epoch < ?", request.time_to)):
        if value is not None:
            conditions.append(condition)
            params.append(value)
    rows = view.conn.execute(
        f"SELECT {SENTENCE_COLUMNS}, ({score}) AS hits"
        " FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
        f" WHERE {' AND '.join(conditions)} ORDER BY hits DESC, s.said_at_epoch DESC, s.id DESC LIMIT ? OFFSET ?",
        [*score_params, *params, request.limit + 1, request.offset],
    ).fetchall()
    return [sentence_item(view.conn, row) for row in rows[: request.limit]], len(rows) > request.limit


def _answer_search(view: LedgerView, request: _SearchRequest, out: dict[str, Any]) -> dict[str, Any]:
    seeds, names = _mentioned(view, request.query)
    words = query_words(request.query, _dictionary(view), slot=view.identity.name)  # 分词器一个身份留一个
    ledger_hits = _ledger_hits(view, request, words, seeds, names)
    hits: list[Hit] = ledger_hits
    searched = [LEDGER]
    out["degraded"] = [GRAPH]
    searcher = graphhook.open_searcher(view.settings, out["library"])
    if searcher is not None:
        try:
            hits = list(searcher.search(graphhook.GraphRequest(
                library=out["library"],
                question=request.query,
                words=tuple(words),
                seeds=tuple(seeds),
                scopes=_visible_scopes(view),
                partitions=None if request.partitions is None else tuple(request.partitions),
                time_from=request.time_from,
                time_to=request.time_to,
                include_history=request.include_history,
                as_of=view.as_of,
                want=request.offset + request.limit + 1 + GRAPH_MARGIN,
                ledger_hits=tuple(ledger_hits),
                hidden_in=lambda items: _hidden_in(view, items),
            )))
            searched = [GRAPH, LEDGER]
            out["degraded"] = []
        except graphhook.GraphUnavailable:
            hits = ledger_hits
        except Exception as error:  # 检索器自己出了想不到的错：照主图连不上办，查询照答，标准错误里记一行种类（不带内容）
            print(f"javmem 查询：主图检索出错（{type(error).__name__}），这次只用账本视图", file=sys.stderr, flush=True)
            hits = ledger_hits
    valid: list[tuple[Hit, sqlite3.Row]] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        if (hit.kind, hit.id) in seen:
            continue
        seen.add((hit.kind, hit.id))
        row = _valid_row(view, hit, request)
        if row is not None:
            valid.append((hit, row))
    if valid:
        page = valid[request.offset : request.offset + request.limit]
        out["items"] = [view.fact_item(row) if hit.kind == "fact" else view.relation_item(row) for hit, row in page]
        # 5.1 第 1 步：主图和账本视图有结果，answered_from 是 graph；翻过头了这一页是空的，照第 3 步写 null（审阅第 6 条）
        out["answered_from"] = GRAPH if out["items"] else None
        more = len(valid) > request.offset + request.limit
    else:
        searched.append(RAW)
        out["items"], more = _raw_hits(view, words, request)
        out["answered_from"] = RAW if out["items"] else None
    out["result"] = "hit" if out["items"] else "none"
    if not out["items"]:
        out["not_found"] = _not_found("no_match", searched, [])
    out["next_cursor"] = f"c{request.offset + request.limit}" if more else None
    return _reorder(out)


def memory_search(arguments: Mapping[str, object], *, settings: Settings) -> dict[str, Any]:
    """一句话问题或关键词，查事实和关系；主图和账本视图查不到再查原文层（5.1）。"""
    return run_tool(MEMORY_SEARCH, arguments, settings, _parse_search, _answer_search)


# ---------- 五个工具 ----------

TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    MEMORY_SEARCH: memory_search,
    ENTITY_CARD: entity_card,
    TIMELINE: timeline,
    RELATION_AT: relation_at,
    RAW_SEARCH: raw_search,
}


def call_tool(name: str, arguments: Mapping[str, object], *, settings: Settings) -> dict[str, Any]:
    """按名字调一个工具。名字不认抛 KeyError（MCP 那一层当协议错）。"""
    return TOOLS[name](arguments, settings=settings)
