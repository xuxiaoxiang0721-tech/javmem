"""几点几分的各种写法（J2.14），给 L4 的 G6 当原文里的时刻。纯函数。

- `HH:MM`（时间段 `10:00-12:00` 的后一个也算）；后面跟着 `PM`、`p.m.` 的，或者前面紧挨着“下午、晚上、傍晚”这些的，
  也算加 12 小时的那个（`7:29 PM` 是 19:29，`下午2:00` 是 14:00）；`12:xx AM` 也算 0:xx。
- 中文的“几点”：`三点`、`3 点 15`（不写“分”也算，后面紧跟着万、元、号这些的不算）、`三点二十`、`六点半`、
  `三点一刻`（15 分）、`三点三刻`（45 分）、`两点`。前面写着“下午、晚上”的只算加 12 小时的那个，写着“早上、上午、
  凌晨”的只算写的那个，都没写的两个都算（“3 点”可能是 15 点）。“一点也不急”“差一点”的“一点”不是钟点。

都是原文这一边用的：多认出一个时刻，只是让事实里的那个时刻多一个出处。
"""

from __future__ import annotations

import re

from javmem.zh.numbers import cn_number

HHMM = re.compile(r"(?<![\d:.+\-])([01]?\d|2[0-3])\s?:\s?([0-5]\d)(?![\d.]|:\d)")
RANGE_TAIL = re.compile(r"\s?[-–~～至到]\s?([01]?\d|2[0-3])\s?:\s?([0-5]\d)(?![\d.]|:\d)")
_PM_AFTER = re.compile(r"\s?(?:pm|p\.m\.)(?![a-z])", re.IGNORECASE)
_AM_AFTER = re.compile(r"\s?(?:am|a\.m\.)(?![a-z])", re.IGNORECASE)
_PM_BEFORE = re.compile(r"(?:下午|晚上|傍晚|夜里|夜间|晚间|今晚|明晚|昨晚|午后)\s?$")  # 一个字的“晚”不算：最晚8点
_AM_BEFORE = re.compile(r"(?:上午|早上|早晨|凌晨|清晨|今早|明早|昨早)\s?$")  # 一个字的“早”不算：最早3点、早晚8点
# “一点也不急”“一点儿”“差一点”“早一点”的“一点”不是一点钟
_NOT_ONE_OCLOCK_AFTER = re.compile(r"[也都儿点些]")
_NOT_ONE_OCLOCK_BEFORE = re.compile(r"[有差好快慢大小多少早晚高低重轻]$")
_CN_HOUR = r"(\d{1,2}|[一二两三四五六七八九十]{1,3})"
_CN_CLOCK = re.compile(
    rf"(?<![\d.]){_CN_HOUR}\s?点\s?"
    r"(?:(半)|(一刻|三刻)|(\d{1,2})(?:\s?分|(?![\d.万千百亿元块%％号日月个]))"
    r"|([零〇][一二三四五六七八九]|[一二三四五六七八九十]{1,3})(?:分|(?![万千百亿元块号日月个])))?"
)


def _hour(text: str) -> int | None:
    hour = int(text) if text.isdigit() else cn_number(text)
    return hour if hour is not None and 0 <= hour <= 23 else None


def hhmm(text: str) -> set[tuple[int, int]]:
    """写成 HH:MM 的时刻，只按写的那样认（事实那一边用）。"""
    found = set()
    for match in HHMM.finditer(text):
        found.add((int(match.group(1)), int(match.group(2))))
        tail = RANGE_TAIL.match(text, match.end())
        if tail is not None:
            found.add((int(tail.group(1)), int(tail.group(2))))
    return found


def clocks(text: str) -> set[tuple[int, int]]:
    """原文里的时刻：(时, 分)。text 是 NFKC 以后的。"""
    found: set[tuple[int, int]] = set()
    for match in HHMM.finditer(text):
        pairs = [(int(match.group(1)), int(match.group(2)))]
        end = match.end()
        tail = RANGE_TAIL.match(text, end)
        if tail is not None:
            pairs.append((int(tail.group(1)), int(tail.group(2))))
            end = tail.end()
        afternoon = bool(_PM_AFTER.match(text, end) or _PM_BEFORE.search(text[max(0, match.start() - 4) : match.start()]))
        morning = bool(_AM_AFTER.match(text, end))
        for hour, minute in pairs:
            found.add((hour, minute))
            if afternoon and 1 <= hour <= 11:
                found.add((hour + 12, minute))
            if morning and hour == 12:
                found.add((0, minute))
    for match in _CN_CLOCK.finditer(text):
        hour = _hour(match.group(1))
        if hour is None:
            continue
        before = text[max(0, match.start() - 4) : match.start()]
        if match.group(1) == "一" and match.lastindex == 1 and (  # 光写“一点”，后面没有几分
            _NOT_ONE_OCLOCK_AFTER.match(text, match.end()) or _NOT_ONE_OCLOCK_BEFORE.search(before)
        ):
            continue
        if match.group(2):
            minute: int | None = 30
        elif match.group(3):
            minute = 15 if match.group(3) == "一刻" else 45
        elif match.group(4):
            minute = int(match.group(4))
        elif match.group(5):
            minute = cn_number(match.group(5).lstrip("零〇")) if match.group(5)[0] in "零〇" else cn_number(match.group(5))
        else:
            minute = 0
        if minute is None or not 0 <= minute <= 59:
            continue
        afternoon, morning = bool(_PM_BEFORE.search(before)), bool(_AM_BEFORE.search(before))
        if not afternoon or hour >= 12:
            found.add((hour, minute))  # “早上 8 点”只是 8 点
        if hour < 12 and not morning:
            found.add((hour + 12, minute))  # “下午 3 点”“晚上 8 点”；没写上午下午的，两个都算
    return found
