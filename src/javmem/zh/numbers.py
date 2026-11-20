"""中文的数、币种、金额和日期的统一写法（J2.14）。纯函数。

- **中文的数**（`cn_number`）：一二三、十五、三千五百、二〇二六，口语的“两千五”（2500）、“一万二”（12000）、
  “三百二”（320）：单位后面只跟一个数字的，那个数字是下一级（“一万零二”才是 10002）。两个数字连着、中间没有单位的
  （“两千五一条”的“两千五一”）不是一个数，原文里按数字之间切开算（`cn_values`）。
- **原文里的数**（`cn_values`，给 L4 的 G2 当出处）：上面那些，加上“三点五万”（35000）、“百分之三十”（30 和 0.3）；
  几个字连着的只在两种时候一个字一个数：后面跟着“月”又不是一个月份的（“九十月份”是 9 月和 10 月），两个数字连着的
  （“三四天”“一两个”）。别的不再一个字一个字算：不然“百分之三十”会把 3% 放过去。
- **币种**（`currency_codes`）：USD、美元、美金、$……；数后面单写的“元”“块钱”、句末的“块”是人民币。
- **值的统一写法**（`value_key`，给 L5 的合并用）：值只是一个金额（可以带币种、“约”）或者一个日期的，换成统一的写法：
  `5 万`、`5万`、`50000` 都是 `50000`；`USD 384k`、`USD 384,000` 都是 `usd384000`；`2026/11/18`、`11月18日`（没写年份的
  往后看，取说话那天前 30 天到后 335 天里的那一次）都是 `2026-11-18`，只写 `11/18` 的要属性像日期。已经是这个样子的（`50000`、`2026-11-18`）和 L5
  原来的比对写法一样。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
UNITS = {"十": 10, "百": 100, "千": 1000}
BIG = {"万": 10_000, "亿": 100_000_000}
RUN = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+")
MAX_LENGTH = 20  # 再长的不当数：不是数，换算也会溢出
MAX_DIGITS = 30  # 阿拉伯数字也一样

# 只从一串中文数的开头认、长度有上限：不然几万个“一”连着时每个位置都往后扫一遍，慢成平方级
_CN_START = r"(?<![零〇一二两三四五六七八九十百千万亿])"
_PERCENT = re.compile(r"百分之([零〇一二两三四五六七八九十百千]{1,20})(?:点([零〇一二三四五六七八九]{1,20}))?")
_DECIMAL = re.compile(_CN_START + r"([零〇一二两三四五六七八九十百千]{1,20})点([零〇一二三四五六七八九]{1,20})(?:\s?([万亿]))?")
# 事实里“三点五十”“三点零五”“三点一刻”是时刻，不是小数
_CLOCK_AFTER = re.compile(r"[十分刻半钟]")


def cn_number(text: str) -> int | None:
    """中文写的数换成数，口语的“两千五”“一万二”也认。认不出、太长、两个数字连着没有单位的，返回 None。"""
    if not text or len(text) > MAX_LENGTH:
        return None
    if any(char not in DIGITS and char not in UNITS and char not in BIG for char in text):
        return None
    if len(text) > 1 and all(char in DIGITS for char in text):  # 二〇二六 这种一位一位写的
        return int("".join(str(DIGITS[char]) for char in text))
    total = section = 0
    digit: int | None = None  # 还没乘上单位的那个数字
    last = 0  # 上一个单位：口语里单位后面只跟一个数字的，那个数字是它的下一级
    zero = False  # 中间写了“零”：一万零二、一百零五
    for char in text:
        if char in DIGITS:
            if DIGITS[char] == 0:
                zero = True
                continue
            if digit is not None:
                return None  # 两千五一：两个数字连着，中间没有单位
            digit = DIGITS[char]
        elif char in UNITS:
            section += (digit if digit is not None else 1) * UNITS[char]
            digit, last, zero = None, UNITS[char], False
        else:
            if section or digit is not None:
                total += (section + (digit or 0)) * BIG[char]
            elif total:
                total *= BIG[char]  # 一万亿：前面整个乘上去
            else:
                total = BIG[char]  # 单写一个“万”是一万（“万人”）
            section, digit, last, zero = 0, None, BIG[char], False
    if digit is not None:
        section += digit * (last // 10) if last >= 100 and not zero else digit
    return total + section


def canonical(number: str) -> str:
    """数的统一写法：去掉千分位，小数点后面多余的 0 去掉（0.85 写成 .85，L4 一直是这样比的）。"""
    digits = number.replace(",", "")
    if "." in digits:
        digits = digits.rstrip("0").rstrip(".")
    return digits.lstrip("0") or "0"


def decimal_text(value: Decimal) -> str:
    """Decimal 写成 canonical 的样子，不用科学记数法。"""
    return canonical(format(value, "f"))


def _decimal(whole: str, fraction: str | None) -> Decimal | None:
    number = cn_number(whole)
    if number is None:
        return None
    if not fraction:
        return Decimal(number)
    return Decimal(f"{number}.{''.join(str(DIGITS[char]) for char in fraction)}")


def _pieces(run: str) -> list[str]:
    """两个数字连着、中间没有单位的地方切开：两千五一 → 两千五、一。"""
    pieces = [run[0]]
    for before, char in zip(run, run[1:]):
        if DIGITS.get(char) and DIGITS.get(before):
            pieces.append(char)
        else:
            pieces[-1] += char
    return pieces


def _one_by_one(run: str, after: str, number: int | None) -> bool:
    """几个字连着的什么时候一个字一个数：九十月份（后面跟着“月”又不是一个月份），三四天、一两个（两个数字连着）。"""
    if after == "月" and (number is None or not 1 <= number <= 12):
        return True
    return len(run) == 2 and all(DIGITS.get(char) for char in run)


def after_digit(text: str, start: int) -> bool:
    """start 前面紧挨着（隔一个空格也算）一个阿拉伯数字。"""
    before = text[max(0, start - 2) : start].rstrip()
    return bool(before) and before[-1].isdigit()


def cn_values(text: str) -> set[str]:
    """原文里中文写的数，canonical 的写法。text 是 NFKC 以后的。"""
    found: set[str] = set()
    masked = text
    for pattern in (_PERCENT, _DECIMAL):
        for match in pattern.finditer(masked):
            value = _decimal(match.group(1), match.group(2))
            if value is not None:
                found.add(decimal_text(value))
                if pattern is _PERCENT:
                    found.add(decimal_text(value / 100))
                elif match.group(3):
                    found.add(decimal_text(value * BIG[match.group(3)]))
            masked = masked[: match.start()] + " " * (match.end() - match.start()) + masked[match.end() :]
    for match in RUN.finditer(masked):
        run = match.group(0)
        if all(char in BIG for char in run) or (after_digit(masked, match.start()) and all(char in UNITS for char in run)):
            continue  # “5 万”的“万”是 5 的单位，前面算过了；单写的“万、亿”（万科、几万、万一）不是数
        number = cn_number(run)
        if number is not None:
            found.add(str(number))
        elif len(run) <= MAX_LENGTH:
            found.update(str(value) for piece in _pieces(run) if (value := cn_number(piece)) is not None)
        if _one_by_one(run, masked[match.end() : match.end() + 1], number):
            found.update(str(DIGITS[char]) if char in DIGITS else "10" for char in run if char in DIGITS or char == "十")
    return found


def cn_decimals(text: str) -> list[tuple[int, int, str, tuple[str, ...]]]:
    """事实里中文写的小数（三点五万、百分之三十）：(起点, 终点, canonical 的值, 别的写法)。百分之几记成几，
    别的写法是小数（百分之三十也是 0.3）。“三点五十”“三点零五”这种像时刻的不算。"""
    found: list[tuple[int, int, str, tuple[str, ...]]] = []
    for pattern in (_PERCENT, _DECIMAL):
        for match in pattern.finditer(text):
            if any(start < match.end() and match.start() < end for start, end, _, _ in found):
                continue
            if pattern is _DECIMAL and (match.group(2).startswith(("零", "〇")) or _CLOCK_AFTER.match(text, match.end())):
                continue
            value = _decimal(match.group(1), match.group(2))
            if value is None:
                continue
            alternatives: tuple[str, ...] = ()
            if pattern is _PERCENT:
                alternatives = (decimal_text(value / 100),)
            elif match.group(3):
                value *= BIG[match.group(3)]
            found.append((match.start(), match.end(), decimal_text(value), alternatives))
    return found


# ---------- 币种 ----------

CURRENCIES: dict[str, tuple[str, ...]] = {
    "USD": ("usd", "美元", "美金", "us$"),
    "JPY": ("jpy", "日元", "日圆", "円"),
    "CNY": ("cny", "rmb", "人民币"),
    "HKD": ("hkd", "港币", "港元", "hk$"),
    "SGD": ("sgd", "新币", "新元", "新加坡元", "s$"),
    "EUR": ("eur", "欧元", "€"),
    "GBP": ("gbp", "英镑", "£"),
    "AUD": ("aud", "澳元", "澳币", "a$"),
    "CHF": ("chf", "瑞郎", "瑞士法郎"),
    "KRW": ("krw", "韩元", "₩"),
    "TWD": ("twd", "新台币", "台币", "nt$"),
}
# 数后面单写一个“元”“块钱”是人民币
PLAIN_YUAN = re.compile(r"(?:\d|[零一二两三四五六七八九十百千万亿])\s?(?:[wWkK]\s?)?(?:元(?![旦素宵件])|块钱|块(?![\u3400-\u9fff]))")


def currency_codes(text: str) -> set[str]:
    """一段话里写了哪些币种。text 是 NFKC 以后的。"""
    folded = text.casefold()
    found = set()
    for code, words in CURRENCIES.items():
        for word in words:
            if word.isascii() and word.isalpha():
                if re.search(rf"(?<![a-z]){word}(?![a-z])", folded):
                    found.add(code)
            elif word.isascii():  # us$、s$ 这种：前面不能紧跟着字母
                if re.search(rf"(?<![a-z]){re.escape(word)}", folded):
                    found.add(code)
            elif word in folded:
                found.add(code)
    if re.search(r"(?<![a-z])\$", folded):  # 光一个 $ 当美元
        found.add("USD")
    if PLAIN_YUAN.search(folded):
        found.add("CNY")
    return found


# 金额前后紧挨着币种时，口语的 W、w 是万（RMB60W、60w 美金）；不挨着的不算（2000W 是瓦）
CURRENCY_BEFORE = re.compile(r"(?:usd|us\$|hkd|hk\$|sgd|s\$|rmb|cny|jpy|eur|gbp|aud|a\$|nt\$|\$|€|£)\s?$", re.I)
CURRENCY_AFTER = re.compile(r"^\s?(?:美元|美金|港币|港元|人民币|日元|新币|新元|欧元|英镑|澳元|刀|元|块|usd|hkd|sgd|rmb|cny|jpy)", re.I)


# ---------- 值的统一写法（L5） ----------

# approximately 写在 approx 前面：不然只吃掉前半截
_APPROX = re.compile(r"^(?:约合|大约|大概|大致|差不多|将近|接近|约|近|~|≈|about|approximately|approx\.?|around)\s*")
_ROUGHLY = re.compile(r"\s*(?:左右|上下)$")  # “5万多”比 5 万多，不算同一个值
_MINUS = str.maketrans({"−": "-", "－": "-", "–": "-"})
_CURRENCY_WORD = re.compile(
    "|".join(
        sorted(
            {re.escape(word) for words in CURRENCIES.values() for word in words} | {r"\$", "元", "块钱", "块"},
            key=len,
            reverse=True,
        )
    )
)
_SCALES = {
    "万亿": 10**12, "千万": 10_000_000, "百万": 1_000_000, "亿": 100_000_000, "万": 10_000, "千": 1_000,
    "k": 1_000, "m": 1_000_000, "mn": 1_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000,
}
_AMOUNT = re.compile(
    r"(-)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*(万亿|千万|百万|亿|万|千|mn|bn|million|billion|thousand|[kmb])?",
)
_MIXED_AMOUNT = re.compile(r"(\d{1,12})\s*([万千])\s*([1-9])")  # 口语的“2万5”是 25000
_CN_AMOUNT = re.compile(r"([零〇一二两三四五六七八九十百千]+)(?:点([零〇一二三四五六七八九]+))?\s*([万亿])?")
_YMD = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?")
_MD_WORDS = re.compile(r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[日号]")
_MD_MARKS = re.compile(r"(\d{1,2})\s*[-/]\s*(\d{1,2})")
# 属性像日期的，值里的 11/18、11-18 才当日期；别的属性里的 3/4、1/2、3-5 是分数、比例、范围
_DATE_ATTRIBUTE = re.compile(
    r"日期|日子|时间|哪天|生日|截止|到期|交付|交货|发货|到货|入住|出发|返回|开始|结束|deadline|date|due", re.IGNORECASE
)


def _small(text: str) -> int | None:
    return int(text) if text.isdigit() else cn_number(text)


def _valid(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except (ValueError, OverflowError):
        return None


def _key_number(number: Decimal) -> str:
    """比对写法里的数：整数写法，小数点后面多余的 0 去掉（0.85 还是 0.85，和 L5 原来的写法一样）。"""
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def amount_key(value: str) -> str | None:
    """值只是一个金额（可以带一个币种、“约”“左右”）的统一写法：币种代码（小写）加整数写法的数。别的返回 None。"""
    text = unicodedata.normalize("NFKC", value).translate(_MINUS).strip().casefold()
    text = _ROUGHLY.sub("", _APPROX.sub("", text)).strip()
    if not text or len(text) > 40:
        return None
    codes = currency_codes(text)
    if len(codes) > 1:
        return None
    rest = _CURRENCY_WORD.sub(" ", text).strip()
    sign = ""
    match = _AMOUNT.fullmatch(rest)
    mixed = _MIXED_AMOUNT.fullmatch(rest)
    if match is not None:
        digits = match.group(2).replace(",", "")
        if len(digits) > MAX_DIGITS:
            return None
        number = Decimal(digits) * _SCALES.get(match.group(3) or "", 1)
        sign = "-" if match.group(1) else ""
    elif mixed is not None:
        scale = BIG.get(mixed.group(2)) or UNITS[mixed.group(2)]
        number = Decimal(int(mixed.group(1)) * scale + int(mixed.group(3)) * scale // 10)
    elif RUN.fullmatch(rest):
        parsed_int = cn_number(rest)
        if parsed_int is None:
            return None
        number = Decimal(parsed_int)
    else:
        match = _CN_AMOUNT.fullmatch(rest)
        if match is None:
            return None
        parsed = _decimal(match.group(1), match.group(2))
        if parsed is None:
            return None
        number = parsed * (BIG[match.group(3)] if match.group(3) else 1)
    code = next(iter(codes)).lower() if codes else ""
    return f"{code}{sign}{_key_number(number)}"


def date_key(value: str, said: date | None, attribute: str | None = None) -> str | None:
    """值只是一个日期的统一写法 YYYY-MM-DD。

    没写年份的取说话那天前 30 天到后 335 天里的那一次（said 是空的就不认）：说话那天差几天的，同一个写法总是同一个键，
    跨年也一样。只写了 11/18、11-18 的，属性要像日期（交货日期、截止时间……），不然 3/4、1/2 是分数。
    """
    text = unicodedata.normalize("NFKC", value).strip()
    match = _YMD.fullmatch(text)
    if match is not None:
        day = _valid(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return day.isoformat() if day else None
    if said is None:
        return None
    match = _MD_WORDS.fullmatch(text)
    if match is None and attribute and _DATE_ATTRIBUTE.search(attribute):
        match = _MD_MARKS.fullmatch(text)
    if match is None:
        return None
    month, day_number = _small(match.group(1)), _small(match.group(2))
    if not month or not day_number:
        return None
    # 往后看：取说话那天前 30 天到后 335 天里的那一次（12 月底说的“1月5日”是明年的；11 月说的“1月15日”是明年的）。
    # 说话那天离得近的几次，说的同一个写法总是同一个键
    for year in (said.year - 1, said.year, said.year + 1):
        day = _valid(year, month, day_number)
        if day is not None and -30 <= (day - said).days < 335:
            return day.isoformat()
    return None


def value_key(value: str, said: date | None = None, attribute: str | None = None) -> str | None:
    """值只是一个金额或一个日期的，返回统一的写法；别的返回 None（调用方照原来的比对写法）。"""
    try:
        return date_key(value, said, attribute) or amount_key(value)
    except (InvalidOperation, ValueError, OverflowError):
        return None
