"""带时区的时间。不同时区的文本不能直接比，比较和排序用 epoch。"""

from __future__ import annotations

from datetime import datetime


def parse_aware(value: object) -> datetime | None:
    """解析带时区的 ISO 8601。不是文本、格式不对、没带时区，都返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())
