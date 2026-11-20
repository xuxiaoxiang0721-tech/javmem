"""软变化的犹豫词表（J4.5，04 p4“规则再用犹豫词表补扫一遍”）。

分七组。前三组（暂停、往后推、拿不准）单独出现就算候选句；后四组（可能、条件、打算、问）太常见，只有这句话
抽出的说法和一条比它早说的旧东西对得上时才算（javmem.soft.find）。

词表是写死在代码里的普通说法，不是原话；改了要换 WORDS_VERSION，找出来的候选跟着变。
"""

from __future__ import annotations

import re
import unicodedata

WORDS_VERSION = "soft-words-v1"

STRONG = ("暂停", "往后推", "拿不准")
WEAK = ("可能", "条件", "打算", "问")
GROUPS = STRONG + WEAK

_PATTERNS: dict[str, str] = {
    # 先停下来，没说不要了
    "暂停": (
        r"先放放|放一放|先放下|放下吧|先放着|先不管|先别管|暂缓|缓一缓|先缓|先停|停一下|停一停|暂停|暂时不|暂时先|"
        r"先不做|先不用|先别做|先不要|先搁|搁置|先等等|"
        r"\bon[ -]hold\b|\bhold off\b|\bpaus(?:e|es|ed|ing)\b|\bput\b[^.;!?]{0,30}\bhold\b"
    ),
    # 往后推
    "往后推": (
        r"再说|以后再|回头再|过阵子|过段时间|晚点再|下次再|先这样|等[^，。；！？,.;!?]{1,12}再|"
        r"\bpostpon(?:e|es|ed|ing)\b|\bdefer(?:s|red|ring)?\b|\bpush(?:ed|ing)? (?:it )?back\b|"
        r"\bfor now\b|\bfor the time being\b"
    ),
    # 没定下来
    "拿不准": (
        r"看情况|视情况|看看再|原则上|不一定|待定|未定|还没定|没定下|不确定|说不准|"
        r"\btbd\b|\btentative(?:ly)?\b|\bpending\b|\bnot sure\b|\bnot decided\b|\bundecided\b"
    ),
    "可能": r"可能|也许|或许|大概|\bmaybe\b|\bperhaps\b|\bpossibl[ey]\b|\bprobably\b",
    "条件": r"如果|要是|假如|假设|万一|除非|只要|的话|\bif\b|\bunless\b",
    "打算": r"打算|考虑|想要|准备|计划|琢磨|\bplan(?:s|ning)? to\b|\bconsider(?:s|ing)?\b|\bthinking of\b",
    "问": r"[?？]\s*$|吗\s*[。！!]?\s*$|是否|有没有|对吗|对不对|行不行",
}
_COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in _PATTERNS.items()}


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def hedges(text: str) -> tuple[str, ...]:
    """这句话碰上了哪几组，按 GROUPS 的先后。"""
    cleaned = _clean(text)
    return tuple(name for name in GROUPS if _COMPILED[name].search(cleaned))


def is_strong(groups: tuple[str, ...] | list[str]) -> bool:
    """碰上了前三组之一：单独出现就算候选句。"""
    return any(group in STRONG for group in groups)
