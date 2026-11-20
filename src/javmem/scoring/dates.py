"""日子的比法（J3.8）：几种写法都换成一段日子（含两头），精度不一样也能比。

认的写法：`2026-10-23`、`2026-10-23 16:54`、`2026-10-23T16:54+08:00`、`2026-10-23 16:54 +08:00`、`2026-10`、`2026-Q4`、
`2026`（带时区的换成北京时间那天；金标准一律是北京时间）。精度比写法粗的按精度算（写了 `2026-10-23`、精度是 month 的，
算整个十月）。
认不出的、精度是 unknown 的，当不知道。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone

from javmem.scoring.common import When

BEIJING = timezone(timedelta(hours=8))
TOLERANCE_DAYS = 7  # 关系的日子差几天以内算对得上（D120；原来 3 天，04 p7）。开始、结束日子都照这个
_RANK = {"minute": 5, "day": 4, "month": 3, "quarter": 2, "year": 1, "unknown": 0}
_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s?(Z|[+-]\d{2}:?\d{2})?)?$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$", re.IGNORECASE)
_YEAR = re.compile(r"^(\d{4})$")


def precision_rank(precision: str | None) -> int:
    return _RANK.get(precision or "unknown", 0)


def infer_precision(text: str | None) -> str:
    """没写精度的，照写法猜：带时分的 minute，年月日 day，年月 month，季度 quarter，只有年 year，认不出 unknown。"""
    if not text:
        return "unknown"
    value = text.strip()
    full = _FULL.match(value)
    if full:
        return "minute" if full.group(4) is not None else "day"
    if _MONTH.match(value):
        return "month"
    if _QUARTER.match(value):
        return "quarter"
    if _YEAR.match(value):
        return "year"
    return "unknown"


def single_day(when: When) -> bool:
    """这个日子准到一天以内。"""
    found = interval(when.start, when.precision)
    return found is not None and found[0] == found[1]


def _offset(text: str) -> timezone:
    if text == "Z":
        return timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))


def interval(text: str | None, precision: str | None = None) -> tuple[date, date] | None:
    """一段日子（含两头）。不知道的返回 None。"""
    if not text or precision == "unknown":
        return None
    value = text.strip()
    try:
        full = _FULL.match(value)
        if full:
            year, month, day = int(full.group(1)), int(full.group(2)), int(full.group(3))
            if full.group(4) is not None and full.group(7) is not None:
                moment = datetime(year, month, day, int(full.group(4)), int(full.group(5)), tzinfo=_offset(full.group(7)))
                local = moment.astimezone(BEIJING).date()
                year, month, day = local.year, local.month, local.day
            shown = "day"
            first = last = date(year, month, day)
        elif (found := _MONTH.match(value)) is not None:
            year, month = int(found.group(1)), int(found.group(2))
            shown, first, last = "month", date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        elif (found := _QUARTER.match(value)) is not None:
            year, quarter = int(found.group(1)), int(found.group(2))
            month = quarter * 3 - 2
            shown = "quarter"
            first = date(year, month, 1)
            last = date(year, month + 2, calendar.monthrange(year, month + 2)[1])
        elif (found := _YEAR.match(value)) is not None:
            year = int(found.group(1))
            shown, first, last = "year", date(year, 1, 1), date(year, 12, 31)
        else:
            return None
    except ValueError:
        return None
    coarse = precision if precision in _RANK and precision_rank(precision) < precision_rank(shown) else shown
    if coarse == "month":
        return date(first.year, first.month, 1), date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    if coarse == "quarter":
        month = (first.month - 1) // 3 * 3 + 1
        return date(first.year, month, 1), date(first.year, month + 2, calendar.monthrange(first.year, month + 2)[1])
    if coarse == "year":
        return date(first.year, 1, 1), date(first.year, 12, 31)
    return first, last


def close(a: When, b: When, tolerance_days: int) -> bool | None:
    """两个开始日子差得不多（两段日子各往外放 tolerance_days 天以后挨得上）。有一个不知道的返回 None。"""
    first, second = interval(a.start, a.precision), interval(b.start, b.precision)
    if first is None or second is None:
        return None
    return first[0].toordinal() - tolerance_days <= second[1].toordinal() and second[0].toordinal() - tolerance_days <= first[1].toordinal()
