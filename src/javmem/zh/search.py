"""原文检索的统一写法（J2.14，D77）：原句和查询词都换成这个样子再比，raw_search 查的是它。

- NFKC、不分大小写，空白都去掉：`远帆ADR` 和 `远帆 ADR`、`ＫＶＬＴ` 和 `KVLT`、`宽带+电视` 和 `宽带 + 电视` 一样。
  逗号：全角的、不是千分位的半角的都换成顿号（`你好,世界` 和 `你好，世界` 一样，`100，200 两档` 不当千分位）。
- 数两头加记号 `⟦ ⟧`：数不会查到更长的数里（`5万` 查不到 `15万`，`5%` 查不到 `15%`）。千分位去掉，带“万、亿、千、k、m”
  这些单位的换成整数（`6830万`、`6,830 万`、`68300000` 一样，`230万` 查得到 `2,300,000`），单位就不再单独留着
  （查 `万元` 查不到 `5万元`）；小数点后面多余的 0 去掉；整数照写（`0571` 还是 `0571`）。
- 日期写成 `⟦10⟧月⟦15⟧日` 的样子：`10/15`、`10-15`、`10 月 15 日`、`10月15号`、`十月十五日` 一样；写了年份的（19xx、20xx）
  前面多一段 `⟦2026⟧年`，所以 `10月15日` 也查得到 `2026-10-15`，`2026-10` 和 `2026年10月` 一样；`8/2` 查不到 `8/28`，
  `10月` 查得到 `10月15日`。只写几号的 `18 号` 是 `⟦18⟧日`。
- **原句存两份写法**（中间隔一个控制字符，查询词里不会有它）：一份上面那样，斜杠、横线隔开的两个数都当日期、“号”都当“日”；
  另一份斜杠、横线照写、“号”照写。查询词只换成头一份的样子：查 `11/18`、`11月18日` 都查得到“11/18 周三开会”，
  查 `15人` 查得到“10-15人”（后一份里是 `⟦10⟧-⟦15⟧人`），查 `号线` 查得到“5号线”，查 `18号` 也查得到“18号去北京”。
  一样的就只存一份。代价是查 `10月` 也会查到“10-15人”这种范围。
- 别的照原样。中文写的数（五万、三点五）不换，只在日期里换（十月十五日）。

改了写法要换 VERSION，加一个账本迁移重算原文索引里存的那一份（见 javmem.raw.index.fill_norm）。
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from javmem.zh.numbers import MAX_DIGITS, cn_number

VERSION = "s1"
OPEN, CLOSE = "⟦", "⟧"
LANE = chr(31)  # 原句的两份写法中间的分隔（单元分隔符）：查询词里的控制字符都换成空白，不会有它

_CN_SMALL = r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})"
_YEAR = r"(19\d{2}|20\d{2})"  # 只认 19xx、20xx 当年份：区号“0571-12”不是年月
_YMD = re.compile(rf"(?<!\d){_YEAR}\s?[-/.年]\s?(\d{{1,2}})\s?[-/.月]\s?(\d{{1,2}})\s?[日号]?(?!\d)")
_YM_MARKS = re.compile(rf"(?<![\d.]){_YEAR}\s?[-/]\s?(\d{{1,2}})(?![\d/.-]|\s?[月日号])")  # 2026-10、2026/10
_YM = re.compile(rf"(?<!\d){_YEAR}\s?年\s?({_CN_SMALL})\s?月")
_MD = re.compile(rf"(?<![\d./:,])({_CN_SMALL})\s?([/月-])\s?({_CN_SMALL})\s?([日号])?(?![\d/]|[.,]\d|:\d)")
_M = re.compile(rf"(?<![\d.])({_CN_SMALL})\s?月")
_D = re.compile(r"(?<![\d./:,-])(\d{1,2})\s?([日号])(?!\d)")
_NUMBER = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s?(万亿|千万|百万|亿|万|千|mn|bn|million|billion|thousand|[km])(?![a-z]))?"
)
_SCALES = {
    "万亿": 10**12, "千万": 10_000_000, "百万": 1_000_000, "亿": 100_000_000, "万": 10_000, "千": 1_000,
    "k": 1_000, "m": 1_000_000, "mn": 1_000_000, "bn": 1_000_000_000,
    "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000,
}
_SPACES = re.compile(r"\s+")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
_COMMA = re.compile(r"(?<!\d),|,(?!\d{3}(?!\d))")  # 不是千分位的半角逗号，和全角的一样换成顿号
_MARKED = re.compile(f"({re.escape(OPEN)}[^{re.escape(CLOSE)}]*{re.escape(CLOSE)})")


def _small(text: str) -> int | None:
    return int(text) if text.isdigit() else cn_number(text)


def _mark(number: int | str) -> str:
    return f"{OPEN}{number}{CLOSE}"


def _dates(text: str, *, marks: bool) -> str:
    """日期换成统一的样子。marks 是 False 的那一份：斜杠、横线隔开的两个数不当日期，“号”照写。"""

    def ymd(match: re.Match[str]) -> str:
        month, day = int(match.group(2)), int(match.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return match.group(0)
        return f"{_mark(int(match.group(1)))}年{_mark(month)}月{_mark(day)}日"

    def ym(match: re.Match[str]) -> str:
        month = _small(match.group(2))
        if month is None or not 1 <= month <= 12:
            return match.group(0)
        return f"{_mark(int(match.group(1)))}年{_mark(month)}月"

    def md(match: re.Match[str]) -> str:
        month, day = _small(match.group(1)), _small(match.group(3))
        arabic = match.group(1).isdigit() and match.group(3).isdigit()
        if match.group(2) != "月" and (not arabic or not marks):
            return match.group(0)  # 十/五 这种不是日期；另一份里斜杠、横线照写
        if match.group(2) == "月" and not match.group(4) and not arabic:
            return match.group(0)
        if month is None or day is None or not (1 <= month <= 12 and 1 <= day <= 31):
            return match.group(0)
        return f"{_mark(month)}月{_mark(day)}日"

    def m(match: re.Match[str]) -> str:
        month = _small(match.group(1))
        if month is None or not 1 <= month <= 12:
            return match.group(0)
        return f"{_mark(month)}月"

    def d(match: re.Match[str]) -> str:
        if match.group(2) == "号" and not marks:
            return match.group(0)  # 另一份里“号”照写：5号线、18号楼
        return f"{_mark(int(match.group(1)))}日"

    text = _YMD.sub(ymd, text)
    text = _YM_MARKS.sub(ym, text)
    text = _YM.sub(ym, text)
    text = _MD.sub(md, text)
    text = _M.sub(m, text)
    return _D.sub(d, text)


def _number(match: re.Match[str]) -> str:
    whole, fraction, unit = match.group(1).replace(",", ""), match.group(2), match.group(3)
    if len(whole) + len(fraction or "") > MAX_DIGITS:
        return match.group(0)
    if unit is None:
        if fraction is None:
            return _mark(whole)  # 整数照写：007 还是 007
        fraction = fraction.rstrip("0")
        return _mark(f"{whole}.{fraction}" if fraction else whole)
    value = Decimal(f"{whole}.{fraction}" if fraction else whole) * _SCALES[unit]
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return _mark(text)


def _clean(text: str) -> str:
    # 全角的逗号 NFKC 以后是半角的，“100，200 两档”会被当成千分位：先换成顿号
    clean = unicodedata.normalize("NFKC", text.replace("，", "、")).casefold()
    clean = _CONTROLS.sub(" ", clean).replace(OPEN, " ").replace(CLOSE, " ")
    return _COMMA.sub("、", clean)


def _lane(clean: str, *, marks: bool) -> str:
    out = []
    for part in _MARKED.split(_dates(clean, marks=marks)):
        if part.startswith(OPEN) and part.endswith(CLOSE):
            out.append(part)  # 日期里已经写好的
        else:
            out.append(_NUMBER.sub(_number, part))
    return _SPACES.sub("", "".join(out))


def search_key(text: str) -> str:
    """查询词的统一写法：只有头一份（斜杠、横线隔开的两个数当日期，“号”当“日”）。"""
    return _lane(_clean(text), marks=True)


def searchable(text: str) -> str:
    """原句存进原文索引的统一写法：两份写法，不一样时用 LANE 隔开。"""
    clean = _clean(text)
    first, second = _lane(clean, marks=True), _lane(clean, marks=False)
    return first if first == second else first + LANE + second
