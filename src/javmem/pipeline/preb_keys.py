"""预处理 B 防合错的硬规矩用的：从一段字里认出日子、编号、金额，比两边对不对得上（导航 D136 第 ① 步）。

规矩：日子、编号、金额对不上的，名字再像也不是同一样东西。只拦“明摆着对不上”的，宁可放过：

- 只比同一类（日子比日子、编号比编号、金额比金额）；两边有一边这一类一个都没有，不比。
- 日子写法先统一：7/14、07-14、7月14日、2026-07-14、14 Jul 都当 07-14（只比月、日）。
- 编号：两个以上大写字母开头、九个字以上、带三个以上数字的（XS1111111111、ZZ9A8B7C6D 这类），比大写。
- 金额：数带着单位（万、千万、百万、亿、M、K、million）或者币种（USD、SGD、JPY、$、¥……），换成一个数比
  （470 万 = 4.7M）。百分比、不带单位的数（价格、股数）不算；一两位整数直接跟 M、前面没有币种的（“6M”“12m”，
  多半是期限或者 3M 这家公司）也不算。
- 说法的“紧挨着的字”：说法本身，加上同一小句里（不跨，。；！？、,;!? 这些标点）紧贴着它前后的：中间只隔空格、括号、
  “的”。比如“X3 累购（7/14）”“7/18 的 X3 累购”“那张（XS2222222222）”。不拿整个小句：
  “那张 9/15 付息 1.2 万”里的 9/15、1.2 万是事件的日子和金额，不是那张单子本身的。

例子都是编的。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

DATE, CODE, AMOUNT = "日子", "编号", "金额"
KINDS = (DATE, CODE, AMOUNT)
Values = dict[str, frozenset[str]]

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
           "oct": 10, "nov": 11, "dec": 12}
# 月份只认整词（Marvelous 里的 Mar 不算）
_MONTH = (r"(january|february|march|april|may|june|july|august|september|october|november|december"
          r"|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?(?![a-z])")
_DATES = (
    # 2026年7月14日、2026/7/14、7/14、7月14日、7月14号（数字不能紧挨着别的数字、小数点；后面不能是 %）
    (re.compile(r"(?<![\d.])(?:20\d{2}\s*[年/.]\s*)?(\d{1,2})\s*[/月]\s*(\d{1,2})\s*(?:日|号)?(?![\d.%])"), (1, 2)),
    (re.compile(r"(?<![\d.])20\d{2}-(\d{1,2})-(\d{1,2})(?![\d.])"), (1, 2)),  # 2026-07-14
    (re.compile(r"(?<![\d.\-])(\d{2})-(\d{2})(?![\d.\-])"), (1, 2)),  # 07-14（两边都是两位）
    (re.compile(r"(?<![\d.])(\d{1,2})\s*" + _MONTH, re.IGNORECASE), (2, 1)),  # 14 Jul
    (re.compile(r"(?<![a-z])" + _MONTH + r"\s*(\d{1,2})(?![\d.%])", re.IGNORECASE), (1, 2)),  # Jul 14
)
_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}[A-Z0-9]{7,})(?![A-Za-z0-9])")
_UNITS = {"千万": 1e7, "百万": 1e6, "万": 1e4, "亿": 1e8, "million": 1e6, "mn": 1e6, "m": 1e6, "k": 1e3}
# 前后只看字母、数字、小数点：中文紧挨着数（“本金470万”）照样认
_AMOUNT_UNIT = re.compile(r"(?<![A-Za-z0-9.])(\d[\d,]*(?:\.\d+)?)\s*(千万|百万|万|亿|million|mn|m|k)(?![a-z])", re.IGNORECASE)
_AMOUNT_CURRENCY = re.compile(
    r"(?:USD|SGD|JPY|HKD|CNY|RMB|US\$|S\$|\$|¥|￥|美元|新币|日元|港币)\s*(\d[\d,]*(?:\.\d+)?)\s*"
    r"(千万|百万|万|亿|million|mn|m|k)?(?![A-Za-z0-9.])",
    re.IGNORECASE,
)
_CLAUSE_STOPS = "，。；！？、,;!?\n"
_GAP = re.compile(r"[\s（(【\[「的]*")  # 说法和日子、编号、金额之间只能隔这些
# 紧贴在说法后面的日子、金额，后面接着这些字的是事件的（“那张 9/15 到期”“付 1.2 万”），不是这一样东西本身的
EVENT = re.compile(r"\s*(到期|付息|派息|交割|观察|敲出|敲入|定价|结算|行权|截止|起息|生效|开始|开会|见面|以前|以后|之前|之后|前|后|起|止"
                   r"|付|收|还|交|到|要|会)")
_SYNONYMS = (("固定票息票据", " fcn "), ("固定票息", " fcn "), ("累购器", " 累购 "), ("累购合约", " 累购 "), ("accumulator", " 累购 "),
             ("aq", " 累购 "))
_FILLER = ("那张", "这张", "那单", "这单", "那笔", "这笔", "那个", "这个", "合约", "合同", "订单", "单子", "的")
_TOKEN = re.compile(r"[a-z]+|\d+|[一-鿿]+")


def _date(month: str, day: str) -> str | None:
    if month.isdigit():
        m = int(month)
    else:
        word = month.lower().rstrip(".")
        m = _MONTHS.get(word, 0) or _MONTHS.get(word[:4], 0) or _MONTHS.get(word[:3], 0)
    d = int(day)
    return f"{m:02d}-{d:02d}" if 1 <= m <= 12 and 1 <= d <= 31 else None


def _amount(number: str, unit: str | None) -> str | None:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    value *= _UNITS.get((unit or "").lower(), _UNITS.get(unit or "", 1.0))
    return f"{value:.0f}" if value >= 1 else None


def spans(text: str) -> list[tuple[int, int, str, str]]:
    """字里认出来的 (起, 止, 哪一类, 统一以后的值)。"""
    found: list[tuple[int, int, str, str]] = []
    for pattern, (mi, di) in _DATES:
        for match in pattern.finditer(text):
            value = _date(match.group(mi), match.group(di))
            if value:
                found.append((match.start(), match.end(), DATE, value))
    for match in _CODE.finditer(text):
        code = match.group(1)
        if sum(ch.isdigit() for ch in code) >= 3:
            found.append((match.start(), match.end(), CODE, code.upper()))
    for pattern in (_AMOUNT_UNIT, _AMOUNT_CURRENCY):
        for match in pattern.finditer(text):
            # “6M”“12m”：一两位整数直接跟 M、前面没有币种的，多半是期限（6 个月）或者 3M 这家公司，不算金额。
            # 1.2M、USD 5M、5 million、500k、470 万照旧算
            if pattern is _AMOUNT_UNIT and match.group(2).lower() == "m" and re.fullmatch(r"\d{1,2}", match.group(1)):
                continue
            value = _amount(match.group(1), match.group(2))
            if value:
                found.append((match.start(), match.end(), AMOUNT, value))
    return found


def _collect(found: Iterable[tuple[int, int, str, str]]) -> Values:
    out: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for _, _, kind, value in found:
        out[kind].add(value)
    return {kind: frozenset(values) for kind, values in out.items()}


def values_of(text: str) -> Values:
    """一段字里所有的日子、编号、金额（名字、别名、学来的写法用这个）。"""
    return _collect(spans(unicodedata.normalize("NFKC", text or "")))


def values_of_names(names: Iterable[str]) -> Values:
    """名单上一项的名字和“也叫”合起来的值（一项可以有几个编号，比如报价单号、下单号）。"""
    out: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for name in names:
        for kind, values in values_of(name).items():
            out[kind].update(values)
    return {kind: frozenset(values) for kind, values in out.items()}


def values_near(line: str, mention: str) -> Values:
    """说法本身加上同一小句里紧贴着它前后的日子、编号、金额（见开头的说明）。说法不在这一行里的，只看说法本身。"""
    line = unicodedata.normalize("NFKC", line or "")
    mention = unicodedata.normalize("NFKC", mention or "")
    at = line.find(mention) if mention else -1
    if at < 0:
        return values_of(mention)
    start = max(line.rfind(stop, 0, at) for stop in _CLAUSE_STOPS) + 1
    ends = [found for found in (line.find(stop, at + len(mention)) for stop in _CLAUSE_STOPS) if found >= 0]
    end = min(ends) if ends else len(line)
    kept = [(s, e, k, v) for s, e, k, v in spans(line) if s >= at and e <= at + len(mention)]  # 说法里头的
    for s, e, kind, value in spans(line[start:at]):  # 紧贴在前面的
        if _GAP.fullmatch(line[start + e : at]):
            kept.append((s, e, kind, value))
    after = at + len(mention)
    for s, e, kind, value in spans(line[after:end]):  # 紧贴在后面的（后面接着“到期、付息”这类字的不算）
        if _GAP.fullmatch(line[after : after + s]) and not (kind != CODE and EVENT.match(line[after + e : end])):
            kept.append((s, e, kind, value))
    return _collect(kept)


def clash(one: Mapping[str, frozenset[str]], other: Mapping[str, frozenset[str]]) -> list[tuple[str, list[str], list[str]]]:
    """两边明摆着对不上的类：同一类两边都有值、一个都对不上。→ [(哪一类, 这边的值, 那边的值)]。"""
    out = []
    for kind in KINDS:
        a, b = one.get(kind) or frozenset(), other.get(kind) or frozenset()
        if a and b and not (a & b):
            out.append((kind, sorted(a), sorted(b)))
    return out


def base_key(text: str) -> str:
    """去掉日子、编号、金额以后的“底子”：大小写、空格、标点、先后不管，“AQ、累购器”当累购、“固定票息票据”当 FCN，
    去掉“那张、合约、的”这类字。几项底子一样、只差日子编号金额的，是“只差日子、编号、金额的几项”。"""
    text = unicodedata.normalize("NFKC", text or "")
    for start, end, _, _ in sorted(spans(text), reverse=True):
        text = text[:start] + " " + text[end:]
    text = text.casefold()
    for old, new in _SYNONYMS:
        text = re.sub(rf"(?<![a-z]){re.escape(old)}(?![a-z])", new, text) if old.isascii() else text.replace(old, new)
    for word in _FILLER:
        text = text.replace(word, " ")
    return "·".join(sorted(set(_TOKEN.findall(text))))
