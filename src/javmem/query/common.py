"""五个查询工具共用的：出错、入参、时间、拒绝的日志（query-api 第 4.6、5、7 节）。

原来写在原文检索里（javmem.raw.search），J5.5 挪到这里给五个工具一起用，写法没改。
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Collection, Mapping
from datetime import datetime, time, timedelta
from typing import Any

from javmem.config import ConfigError, Settings
from javmem.timeutil import epoch, parse_aware

NOT_FOUND_MESSAGE = "记忆里没有查到。"

UNKNOWN_IDENTITY = "unknown_identity"
FORBIDDEN = "forbidden"
INVALID_ARGUMENT = "invalid_argument"
QUERY_TOO_SHORT = "query_too_short"
UNKNOWN_RELATION = "unknown_relation"
UNAVAILABLE = "unavailable"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_CURSOR = re.compile(r"c(\d{1,12})", re.ASCII)  # 位数有上限，不然写进 SQL 时会溢出


class ToolError(Exception):
    """工具答不了：code 是 query-api 4.6 的错误码。message 里不放记忆内容；extra 是 error 里另带的字段，
    比如 unknown_relation 另带 allowed。"""

    def __init__(self, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def invalid(message: str) -> ToolError:
    return ToolError(INVALID_ARGUMENT, message)


def failure(tool: str, code: str, message: str, **extra: object) -> dict[str, Any]:
    """ok 是 false 时只有 tool 和 error 两个字段，不带任何结果（4.6）。"""
    return {"ok": False, "tool": tool, "error": {"code": code, "message": message, **extra}}


def check_names(arguments: Mapping[str, object], allowed: Collection[str]) -> None:
    """写了认不出的入参报 invalid_argument：不然打错一个过滤条件，结果反而变多。"""
    if set(arguments) - set(allowed):
        raise invalid("有认不出的参数。")


def can_be_searched(text: str) -> bool:
    """JSON 里的 "\\ud800" 是合法转义，所以入参能带进来写不进 SQL 的字符。"""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\x00" not in text


def text_argument(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid(f"{name} 要写成文本。")
    if not can_be_searched(value):
        raise invalid(f"{name} 里有不能查的字符。")
    return value


def int_argument(arguments: Mapping[str, object], name: str, default: int, low: int, high: int) -> int:
    value = arguments.get(name)
    if value is None:
        return default
    if type(value) is not int or not low <= value <= high:  # True 也是 int，所以不用 isinstance
        raise invalid(f"{name} 要写 {low} 到 {high} 的整数。")
    return value


def bool_argument(arguments: Mapping[str, object], name: str, default: bool) -> bool:
    value = arguments.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise invalid(f"{name} 要写 true 或 false。")
    return value


def partitions_argument(arguments: Mapping[str, object]) -> list[str] | None:
    partitions = arguments.get("partitions")
    if partitions is not None and not (
        isinstance(partitions, list) and all(isinstance(name, str) and can_be_searched(name) for name in partitions)
    ):
        raise invalid("partitions 要写成文本列表。")
    return partitions


def cursor_offset(arguments: Mapping[str, object]) -> int:
    """cursor 是 c 加偏移量（上一页的 next_cursor）。没写是 0。"""
    cursor = text_argument(arguments, "cursor")
    match = None if cursor is None else _CURSOR.fullmatch(cursor)
    if cursor is not None and match is None:
        raise invalid("cursor 不对。")
    return int(match.group(1)) if match else 0


def is_date(text: str) -> bool:
    return _DATE.fullmatch(text.strip()) is not None


def time_bound(name: str, value: str | None, settings: Settings) -> datetime | None:
    """绝对时间：YYYY-MM-DD，或者带时区的完整时间。只写日期时按服务配置的时区算。"""
    if value is None:
        return None
    text = value.strip()
    if _DATE.fullmatch(text):
        try:
            return datetime.combine(datetime.fromisoformat(text).date(), time.min, tzinfo=settings.timezone)
        except ValueError:
            raise invalid(f"{name} 的时间格式不对。") from None
    moment = parse_aware(text)
    if moment is None:
        raise invalid(f"{name} 的时间格式不对。")
    return moment


def time_range(
    arguments: Mapping[str, object], settings: Settings, start: str = "time_from", end: str = "time_to"
) -> tuple[int | None, int | None]:
    """时间段：起点含，终点不含。终点只写日期时，那一天整天都算；写完整时间时含这一秒。"""
    to_text = text_argument(arguments, end)
    begin = time_bound(start, text_argument(arguments, start), settings)
    finish = time_bound(end, to_text, settings)
    if finish is not None:
        try:
            finish += timedelta(days=1) if _DATE.fullmatch(to_text.strip()) else timedelta(seconds=1)  # type: ignore[union-attr]
        except OverflowError:  # 9999 年的最后一天
            raise invalid(f"{end} 的时间格式不对。") from None
    if begin is not None and finish is not None and begin >= finish:
        raise invalid(f"{start} 晚于 {end}。")
    return (epoch(begin) if begin else None, epoch(finish) if finish else None)


def log_denied(settings: Settings, caller: str | None, tool: str, code: str, moment: str) -> None:
    """每次 unknown_identity 和 forbidden 都记一条：身份、工具、错误码、时间。查询词和结果不进日志（第 7 节）。"""
    # 不用 ensure_ascii=False：身份来自环境变量，里面可能有写不进文件的字符
    line = json.dumps({"time": moment, "caller": (caller or "")[:100], "tool": tool, "code": code})
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(settings.logs_dir / "query-denied.log", "a", encoding="utf-8", newline="\n") as log:
            log.write(line + "\n")
    except (OSError, ConfigError):
        print(line, file=sys.stderr, flush=True)
