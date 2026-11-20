"""L4 核对：L3 抽出来的每条事实，按 G1 到 G7 对着原文核一遍。这里全用规则，不调模型，不碰账本。

规则过了的事实还可以让模型再看一遍原文支不支持（D63，javmem.pipeline.recheck）：模型在 `javmem extract --recheck`
时调，判定存在结果库里；`javmem write --recheck`、`javmem verify --recheck` 按键取出来用（recheck.apply_rechecks），
判部分支持、不支持的也算 G7 没过。不加 `--recheck` 的只用规则。

比对用模型看到的那句（RenderedWindow.shown，就是窗口清单里的 shown：打过码、〔〕换成了［］），
不用原文索引里的原句：打过码的句子拿原句比会对不上。G1 比的时候空白、全角半角、大小写、弯直引号、
markdown 的 ``*``、反引号、表格的竖线和 ``---`` 分隔线不算差别，引文两头的标点也不算（数前面的正负号算），
别的一个字都不能差。G2 到 G7 按词看：英文一个词一个，汉字两个一组，空白不去掉。

| 规则 | 看什么 | 没过的原因 |
| --- | --- | --- |
| L3 | L3 换编号时标过问题的（`problems` 不是空的）、结构不对的，直接隔离 | 照抄 L3 的：unknown_ref、context_ref、empty_quote、no_usable_evidence、unknown_entity、unknown_subject；bad_shape |
| G1 | 引文在原文里：在它引的那一句里 | quote_not_found、quote_in_context、quote_ambiguous、empty_quote |
| G2 | 数字、币种：事实里写的在原文里有。带单位的按换算以后的值比，负号要对上，中文写的数也算（两千五、一万二、三点五万、百分之三十）；基点、挨着币种的 W、一个字母的单位（原文跟着同类单位才认数本身）；月日、几号、季度当日期对得上原文时间的不按数比 | number_mismatch、currency_added |
| G3 | 否定：原文里被否定的那件事，事实里没了否定（说反了）；原文没有否定，事实里却有；双重否定当肯定；只有否定的短回答接在问句后面、事实却没有否定 | negation_dropped、negation_added |
| G4 | 条件：原文里“如果、若、要是、万一、一旦、除非、只要……就、if、unless”带的条件，事实里没了 | condition_dropped |
| G5 | 语气：“可能、打算、应该、多半、听说、might”写成了说定了的事；只有问句（不是请求）的不算事实；软性变化：暂时的（先放放、暂缓、回头再说）写成说定了的（取消、放弃），或者反过来 | modality_upgrade、question_as_fact、soft_change_hardened、soft_change_softened |
| G6 | 时间：事实文字里的日期、时刻、“下周三”这类相对说法，对得上原文写的、按说话时间换算的，或者说话那天、那一刻；写了不存在的日期也算没过。原文里的时刻还认 7:29 PM、下午2:00、三点一刻 | time_unsupported |

J2.14 加的中文写法（中文的数、币种、时刻）在 javmem.zh；规则的细节见 docs/development.md 的“L4 核对”。
| G7 | 改写：事实文字的内容一半以上在这个窗口里看得到；文字不能是空的。加了 `--recheck` 的，模型判部分支持、不支持的也不过 | paraphrase_unsupported、empty_text；model_partial、model_unsupported |

**看的是证据所在的那一小句，不只是模型挑的引文：** 提示词让引文“能看出这条事实就行”，模型常常只引后半句。
G3、G5 看引文所在的小句（按逗号、分号、句号这类切），G4 再往前带一小句（条件常在前面）。

**三级：证据、窗口、都没有。** 数字、币种、日期、加上的否定，先看证据那几句；证据里没有、窗口别的行里有的
（表头里的日期、上文里说过的币种、按提示词从上文带进来的名字），也算有，在 notes 里记一笔
（`G2:number_elsewhere`、`G2:currency_elsewhere`、`G6:time_elsewhere`、`G3:negation_elsewhere`）；窗口里都没有的
才拦。实体的名字也要在窗口里写着才算数：名字是模型写的。

- **引文对不上它引的那一句时，** 能确定在哪就改过来，记在 notes：同一句话（同一封邮件）里连着的几句拼起来
  才对得上的，拆成几条证据（`G1:spans_lines`）；只在别的能当证据的话里找得到的，改指那几句（`G1:ref_fixed`）。
  只在上文、引用这些不能当证据的行里找得到，或者在好几句里都有，都算没过。
- **一条事实有几条证据，** 没过 G1 的那几条去掉，记在 dropped；一条都没过才算这条事实没过 G1。
- **occurred_at 对不上** 不整条隔离：先换成说话人那边的时区，再降精度（分钟降到天、天降到月……），降到哪一级
  对得上就用哪一级（`G6:precision_lowered`）；哪一级都对不上就去掉时间，事实留着（`G6:time_dropped`）。
  事实文字里写错了日期才整条隔离（time_unsupported）：给人看的是文字。
- **没写年月的日期：** 同一句里有“今年、明年、下个月”的，按它算（“下个月 3 号”是下个月的 3 号）；没有的，
  取离说话那天最近的；同一句里写过年份的，那几年也算。
- **确定程度：** L3 的 stated 是 verified（核对过，原话直接说的），inferred、vague 都是 inferred。证据全是只在
  引用里看到过的话（quoted_only，原信不在账本里，D101）的，不管 L3 写的是什么，都是 inferred。
- **G7 只兜底。** 只拿证据比的话，按提示词带上出处、把“他”换成名字、把英文写成中文的好事实都过不了；
  意思改没改由 G2 到 G6 管。

**关系**（J4.3）另有 R1 到 R6，在 javmem.pipeline.relcheck：原句逐字可查（照 G1）、两头在原文里出现、类型对得上
允许表、结束词、日子有根据、说法出自谁。verify_window 一起核，结果在 WindowCheck.relations。

规则版本 L4_VERSION，改了规则要改版本号：账本里记着每条事实是哪一版核的（加了模型复核的，后面再带上复核的
提示词版本和模型，见 recheck.checked_by）。
"""

from __future__ import annotations

import calendar
import math
import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from javmem.pipeline.extract import PROMPT_VERSION, Model, prepare
from javmem.pipeline.normalize import QUOTED_ONLY, Scope
from javmem.pipeline.render import RenderedWindow
from javmem.pipeline.store import ResultStore
from javmem.pipeline.window import Window, WindowBlock, WindowLine
from javmem.redaction import RedactionPattern
from javmem.zh import clock as zh_clock
from javmem.zh import numbers as zh_numbers
from javmem.zh.lexicon import EMPTY as EMPTY_LEXICON
from javmem.zh.lexicon import Lexicon

if TYPE_CHECKING:
    from javmem.pipeline.relcheck import RelationCheck

L4_VERSION = "l4-v8"  # v5：J2.14 中文规则（口语的数、时刻的写法、语气词、软性变化、否定词后面的那个词）
# v6（D101）：证据全是只在引用里看到过的话（quoted_only）的事实一律 inferred
# v7（J4.3）：关系也核（javmem.pipeline.relcheck 的 R1 到 R6）；事实的核法和 v6 一样
# v8（D210、D213）：关系加隶属于（允许表、结束词），任职于的结束词认部门；事实的核法和 v7 一样

RULES: dict[str, str] = {
    "L3": "L3 换编号时标过的问题、结构不对",
    "G1": "引文在原文里",
    "G2": "数字、币种",
    "G3": "否定",
    "G4": "条件",
    "G5": "语气",
    "G6": "时间",
    "G7": "改写：规则兜底；加了模型复核的，模型判部分支持、不支持的也不过",
}
RULE_ORDER = tuple(RULES)

VERIFIED = "verified"
INFERRED = "inferred"

# ---------- 比对用的写法 ----------

_QUOTE_MARKS = str.maketrans(
    {"“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "〃": '"', "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'"}
)
_MARKUP = frozenset("*`|｜")  # markdown 的强调、代码记号和表格竖线，模型常常顺手去掉或者多写
_RULE_LINE = re.compile(r"[-—:]{3,}")  # 表格的分隔线 --- ---: :---，模型常常多写或者少写一段
_EDGE = frozenset("。，、；：！？.,;:!?…‥—–-~～·・()（）[]［］【】「」『』《》〈〉<>\"'")
_SPACES = re.compile(r"\s+")
_MARKUP_RUN = re.compile(r"[*`|｜]+")


_DASHES = str.maketrans({char: "-" for char in "‐‑‒–—―−"})  # 各种横线、减号都当成 -


def fold_map(text: str) -> tuple[str, list[int]]:
    """G1 比对用的写法：NFKC、不分大小写、引号和横线统一、去掉空白、markdown 记号和表格分隔线。

    返回新文字，以及新文字里每个字在 text 里的位置，对上以后能找回原样的那一段。
    """
    chars: list[str] = []
    where: list[int] = []
    skip = {index for match in _RULE_LINE.finditer(text) for index in range(*match.span())}
    for index, char in enumerate(text):
        if index in skip:
            continue
        for piece in unicodedata.normalize("NFKC", char).translate(_QUOTE_MARKS).translate(_DASHES).casefold():
            if piece.isspace() or piece in _MARKUP:
                continue
            chars.append(piece)
            where.append(index)
    return "".join(chars), where


def fold(text: str) -> str:
    return fold_map(text)[0]


def _strip_edges(folded: str) -> str:
    """两头的标点去掉：模型常把句号换成点、少带一个右括号。数前面的正负号留着。"""
    start, end = 0, len(folded)
    while start < end and folded[start] in _EDGE:
        if folded[start] in "-+" and start + 1 < end and folded[start + 1].isdigit():
            break
        start += 1
    while end > start and folded[end - 1] in _EDGE:
        end -= 1
    return folded[start:end]


def fold_quote(quote: str) -> str:
    """引文的比对写法。"""
    return _strip_edges(fold(quote))


def soft(text: str) -> str:
    """G2 到 G7 按词看的写法：NFKC、引号统一、不分大小写，markdown 记号换成空格，连着的空白并成一个。"""
    cleaned = _MARKUP_RUN.sub(" ", unicodedata.normalize("NFKC", text).translate(_QUOTE_MARKS).casefold())
    return _SPACES.sub(" ", cleaned).strip()


_MINUS = str.maketrans({"−": "-", "－": "-"})


def _nfkc(text: str) -> str:
    """找数字、日期用的写法：全角换半角，减号统一，markdown 记号换成空格，连着的空白并成一个。"""
    cleaned = _MARKUP_RUN.sub(" ", unicodedata.normalize("NFKC", text).translate(_QUOTE_MARKS).translate(_MINUS))
    return _SPACES.sub(" ", cleaned)


# ---------- 结果 ----------


@dataclass(frozen=True)
class Finding:
    rule: str  # L3、G1 到 G7
    code: str

    def as_text(self) -> str:
        return f"{self.rule}:{self.code}"


@dataclass(frozen=True)
class Proof:
    """过了 G1 的一条证据。quote 是原样的那一段：没打过码的取原句里的字，打过码的取模型看到的样子。

    start、end 是这一段在 shown 里的位置，G3 到 G5 拿它找所在的小句。
    """

    ref: str
    sentence_id: str
    packet_id: str
    revision: int
    quote: str
    shown: str  # 模型看到的那一整句
    kind: str
    origin: str
    field: str | None
    redacted: bool
    speaker: str
    speaker_role: str
    said_at: datetime
    note: str = ""  # spans_lines、ref_fixed，或者空的
    start: int = 0
    end: int = 0


@dataclass
class FactCheck:
    index: int  # 在 L3 结果里是第几条，从 0 数
    raw: Mapping[str, object]  # L3 的这条事实，原样
    proofs: list[Proof] = field(default_factory=list)
    dropped: list[tuple[Mapping[str, object], Finding]] = field(default_factory=list)  # 没过 G1 的证据
    failures: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    occurred_at: str | None = None
    time_precision: str = "unknown"
    status: str = VERIFIED
    recheck: Mapping[str, str] | None = None  # 模型复核的判定 {verdict, problem}；没发去复核的是 None
    judge: Mapping[str, object] | None = None  # 进图前检查判的经过（javmem.pipeline.precheck.apply 填，J5.3）；没判的是 None

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def first_rule(self) -> str | None:
        """没过的规则里排在最前面的。进图前检查的判断（C，javmem.pipeline.precheck）排在 G7 后面。"""
        rules = {finding.rule for finding in self.failures}
        return next((rule for rule in (*RULE_ORDER, "C") if rule in rules), None)


@dataclass
class WindowCheck:
    window_id: str
    scope: Scope
    facts: list[FactCheck]
    entities: list[Mapping[str, object]]  # L3 的实体，key 重复的、结构不对的去掉了
    bad_entities: list[tuple[Mapping[str, object], Finding]]
    relations: list[RelationCheck] = field(default_factory=list)  # 关系的核对（javmem.pipeline.relcheck，J4.3）

    @property
    def passed(self) -> list[FactCheck]:
        return [fact for fact in self.facts if fact.passed]

    @property
    def quarantined(self) -> list[FactCheck]:
        return [fact for fact in self.facts if not fact.passed]

    @property
    def passed_relations(self) -> list[RelationCheck]:
        return [relation for relation in self.relations if relation.passed]

    @property
    def quarantined_relations(self) -> list[RelationCheck]:
        return [relation for relation in self.relations if not relation.passed]


# ---------- G1 引文在原文里 ----------


@dataclass(frozen=True)
class _Ref:
    line: WindowLine
    block: WindowBlock
    position: int  # 在这一块里是第几行
    shown: str
    folded: str
    where: tuple[int, ...]
    redacted: bool


def _refs(window: Window, rendered: RenderedWindow) -> dict[str, _Ref]:
    redacted = set(rendered.redacted_refs)
    refs = {}
    for block in window.blocks:
        for position, line in enumerate(block.lines):
            shown = rendered.shown.get(line.ref, "")
            folded, where = fold_map(shown)
            refs[line.ref] = _Ref(line, block, position, shown, folded, tuple(where), line.ref in redacted)
    return refs


def _verbatim(ref: _Ref, start: int, end: int) -> str:
    """原样的那一段。没打过码时模型看到的和原句一个字对一个字（只换了〔〕和换行），取原句里的字。"""
    line = ref.line
    if not ref.redacted and not line.clipped and len(line.text) == len(ref.shown):
        return line.text[start:end]
    return ref.shown[start:end]


def _proof(ref: _Ref, start: int, end: int, note: str = "") -> Proof:
    line = ref.line
    return Proof(
        ref=line.ref,
        sentence_id=line.sentence_id,
        packet_id=line.packet_id,
        revision=line.revision,
        quote=_verbatim(ref, start, end),
        shown=ref.shown,
        kind=line.kind,
        origin=line.origin,
        field=line.field,
        redacted=ref.redacted,
        speaker=ref.block.speaker,
        speaker_role=ref.block.speaker_role,
        said_at=ref.block.said_at,
        note=note,
        start=start,
        end=end,
    )


@dataclass(frozen=True)
class _Needle:
    full: str  # 引文的比对写法，两头的标点没去：对得上就连着括号一起存
    core: str  # 两头的标点去了


def _find(folded: str, needle: _Needle) -> tuple[int, int] | None:
    """在比对写法里找，返回 [起, 止)。先找带着两头标点的，找不到再找去掉的。"""
    for text in (needle.full, needle.core):
        if text:
            position = folded.find(text)
            if position >= 0:
                return position, position + len(text)
    return None


def _single(ref: _Ref, needle: _Needle, note: str = "") -> Proof | None:
    found = _find(ref.folded, needle)
    if found is None:
        return None
    low, high = found
    return _proof(ref, ref.where[low], ref.where[high - 1] + 1, note)


_MAX_RUN = 4  # 一条引文最多跨几句


def _run_proofs(run: Sequence[_Ref], needle: _Needle, note: str) -> list[Proof] | None:
    """连着的几句拼起来找；对上了、而且跨了两句以上，拆成每句一条。"""
    joined = "".join(ref.folded for ref in run)
    found = _find(joined, needle)
    if found is None:
        return None
    start, end = found
    proofs = []
    offset = 0
    for ref in run:
        low, high = max(start, offset), min(end, offset + len(ref.folded))
        if low < high:
            proofs.append(_proof(ref, ref.where[low - offset], ref.where[high - offset - 1] + 1, note))
        offset += len(ref.folded)
    return proofs if len(proofs) >= 2 else None


def _across(refs: dict[str, _Ref], block_refs: Sequence[_Ref], needle: _Needle, cited: _Ref | None) -> list[Proof] | None:
    """同一块里、连着的几句能当证据的拼起来才对得上。cited 给了的，拼出来的几句要有它引的那一句。"""
    for size in range(2, _MAX_RUN + 1):
        for start in range(0, len(block_refs) - size + 1):
            run = block_refs[start : start + size]
            if cited is not None and cited not in run:
                continue
            if not all(ref.line.target for ref in run):
                continue
            proofs = _run_proofs(run, needle, "spans_lines" if cited is not None else "ref_fixed")
            if proofs is None:
                continue
            if cited is not None and cited.line.ref not in {proof.ref for proof in proofs}:
                continue  # 拼出来的几句不包括它引的那一句：算改指，后面再找
            return proofs
    return None


def check_evidence(refs: dict[str, _Ref], proof: Mapping[str, object]) -> tuple[list[Proof], Finding | None]:
    """G1：一条证据。过了返回这一条（或者拆开、改指以后的几条），没过返回原因。"""
    problem = proof.get("problem")
    if problem:
        return [], Finding("L3", str(problem))
    cited = refs.get(str(proof.get("ref", "")))
    if cited is None:  # L3 换编号时就该标 unknown_ref
        return [], Finding("L3", "unknown_ref")
    quote = proof.get("quote")
    if not isinstance(quote, str):
        return [], Finding("G1", "empty_quote")
    folded = fold(quote)
    needle = _Needle(folded.strip(), _strip_edges(folded))
    if not needle.core:
        return [], Finding("G1", "empty_quote")
    found = _single(cited, needle)
    if found is not None:
        return [found], None
    blocks = {}
    for ref in refs.values():
        blocks.setdefault(id(ref.block), []).append(ref)
    spanned = _across(refs, blocks[id(cited.block)], needle, cited)
    if spanned is not None:
        return spanned, None
    elsewhere = [ref for ref in refs.values() if ref.line.target and ref is not cited and _find(ref.folded, needle)]
    if len(elsewhere) == 1:
        fixed = _single(elsewhere[0], needle, "ref_fixed")
        assert fixed is not None
        return [fixed], None
    if len(elsewhere) > 1:
        return [], Finding("G1", "quote_ambiguous")
    runs = [proofs for refs_of in blocks.values() if (proofs := _across(refs, refs_of, needle, None)) is not None]
    if len(runs) == 1:
        return runs[0], None
    if len(runs) > 1:
        return [], Finding("G1", "quote_ambiguous")
    if any(_find(ref.folded, needle) for ref in refs.values() if not ref.line.target):
        return [], Finding("G1", "quote_in_context")
    return [], Finding("G1", "quote_not_found")


# ---------- 中文的数（javmem.zh.numbers，J2.14：口语的两千五、一万二也认） ----------

cn_number = zh_numbers.cn_number


def _cn_or_int(text: str) -> int | None:
    if text.isdigit():
        return int(text) if len(text) <= zh_numbers.MAX_LENGTH else None
    return cn_number(text)


# ---------- 时间（G6），G2 也要用它挡掉日期里的数 ----------

_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_EN_MONTHS = {name: number for number, name in enumerate(calendar.month_abbr) if name}
_EN_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


@dataclass
class Times:
    """对得上的时间：每一级各一个集合。细的一级对上了，粗的几级也算对上。invalid：写了一个不存在的日期。"""

    minutes: set[tuple[date, int, int]] = field(default_factory=set)
    days: set[date] = field(default_factory=set)
    months: set[tuple[int, int]] = field(default_factory=set)
    quarters: set[tuple[int, int]] = field(default_factory=set)
    years: set[int] = field(default_factory=set)
    clock: set[tuple[int, int]] = field(default_factory=set)  # 只写了几点几分、没写哪天的
    invalid: bool = False

    def add_day(self, day: date) -> None:
        self.days.add(day)
        self.add_month(day.year, day.month)

    def add_month(self, year: int, month: int) -> None:
        self.months.add((year, month))
        self.quarters.add((year, (month - 1) // 3 + 1))
        self.years.add(year)

    def add_quarter(self, year: int, quarter: int) -> None:
        self.quarters.add((year, quarter))
        self.years.add(year)

    def add_minute(self, day: date, hour: int, minute: int) -> None:
        self.minutes.add((day, hour, minute))
        self.add_day(day)

    def update(self, other: Times) -> None:
        self.minutes |= other.minutes
        self.days |= other.days
        self.months |= other.months
        self.quarters |= other.quarters
        self.years |= other.years
        self.clock |= other.clock

    @property
    def empty(self) -> bool:
        return not (self.days or self.months or self.quarters or self.years)


def _valid(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except (ValueError, OverflowError):
        return None


def _shift(day: date, days: int) -> date | None:
    try:
        return day + timedelta(days=days)
    except OverflowError:
        return None


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def _nearest_year(month: int, day: int, said: date) -> date | None:
    """没写年份的月日：取离说话那天最近的那一年。"""
    options = [_valid(year, month, day) for year in (said.year - 1, said.year, said.year + 1)]
    dates = [option for option in options if option is not None]
    return min(dates, key=lambda option: abs((option - said).days)) if dates else None


def _nearest_month(day: int, said: date) -> date | None:
    """只写了几号：取离说话那天最近的那个月的这一天。"""
    options = []
    for shift in (-1, 0, 1):
        year, month = _add_months(said.year, said.month, shift)
        option = _valid(year, month, day)
        if option is not None:
            options.append(option)
    return min(options, key=lambda option: abs((option - said).days)) if options else None


_S = r"\s?"  # 日期里允许的空白：_nfkc 已经把连着的空白并成一个
_CN_SMALL = r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})"
# 写明了的日期，从具体到笼统排：前面认过的那一段，后面的不再认（“2026 年 11 月 9 日”里的“11 月 9 日”）
_DATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ymd", re.compile(rf"(?<!\d)(\d{{4}})[-/.](\d{{1,2}})[-/.](\d{{1,2}})(?:[T ](\d{{1,2}}):(\d{{2}}))?(?!\d)")),
    ("dmy", re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")),
    ("en_dmy", re.compile(rf"(?<!\d)(\d{{1,2}})[- ]{_EN_MONTH}\.?[- ](\d{{4}}|\d{{2}})(?!\d)", re.I)),
    ("cn_ymd", re.compile(rf"(\d{{4}}){_S}年{_S}({_CN_SMALL}){_S}月{_S}({_CN_SMALL}){_S}[日号]")),
    ("cn_ym", re.compile(rf"(\d{{4}}){_S}年{_S}({_CN_SMALL}){_S}月")),
    ("ym", re.compile(r"(?<![\d.])(\d{4})-(\d{1,2})(?![\d-])")),
    ("yq", re.compile(rf"(?<!\d)(\d{{4}}){_S}-?{_S}[qQ]([1-4])(?!\d)")),
    ("cn_y", re.compile(rf"(?<!\d)(\d{{4}}){_S}年")),
    ("cn_md", re.compile(rf"(?<!\d)({_CN_SMALL}){_S}月{_S}({_CN_SMALL}){_S}[日号]")),
    ("en_md", re.compile(rf"\b{_EN_MONTH}\.?\s(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s(\d{{4}}))?\b(?![- ]\d)", re.I)),
    ("en_dm", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s{_EN_MONTH}\b\.?(?:,?\s(\d{{4}}))?(?![- ]\d)", re.I)),
    ("md", re.compile(r"(?<![\d./:-])(\d{1,2})[/-](\d{1,2})(?![\d/.]|-\d|:\d)")),
    ("cn_m", re.compile(rf"(?<!\d)({_CN_SMALL}){_S}月(?:份)?")),
    ("cn_d", re.compile(r"(?<![\d./-])(\d{1,2})\s?[日号](?![A-Za-z0-9元圆币楼])")),
    ("q", re.compile(rf"(?<![\dA-Za-z])[qQ]([1-4])(?!\d)|第?{_S}([一二三四1-4]){_S}季度")),
    # 只写了月份的英文：首字母大写才算，May 和情态动词撞，不认
    ("en_m", re.compile(r"\b(Jan|January|Feb|February|Mar|March|Apr|April|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\b(?!\.?\s?\d)")),
)
# 事实文字里查的写法。月日（11-20）、几号（20 号）、季度（Q4）可能是分数、编号、楼号，按数交给 G2 比
_TEXT_DATE_KINDS = frozenset(kind for kind, _ in _DATE_PATTERNS) - {"md", "cn_d", "q"}
_CLOCK = zh_clock.HHMM
_CLOCK_RANGE_TAIL = zh_clock.RANGE_TAIL

# 相对的说法，按说话那天换算
_RELATIVE_DAYS = {
    "大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明早": 1, "明晚": 1, "明儿": 1,
    "今天": 0, "今日": 0, "今早": 0, "今晚": 0, "今晨": 0, "今夜": 0, "今儿": 0, "当天": 0, "当日": 0,
    "昨天": -1, "昨日": -1, "昨晚": -1, "昨夜": -1, "前天": -2, "大前天": -3,
    "tomorrow": 1, "today": 0, "tonight": 0, "yesterday": -1,
}
# 英文的几个按 ASCII 不分大小写认：tonıght 这种土耳其字母不算
_RELATIVE_DAY = re.compile(
    "|".join(sorted((re.escape(word) for word in _RELATIVE_DAYS if not word.isascii()), key=len, reverse=True))
    + r"|(?a:(?i:\b(?:tomorrow|today|tonight|yesterday)\b))"
)
_WEEKDAY = re.compile(r"(上个|下个|这个|上|下|本|这)?\s?(?:周|星期|礼拜)\s?([一二三四五六日天])(?![次回遍趟])")
_WEEK = re.compile(r"(上|下|本|这)\s?个?\s?(?:周|星期|礼拜)(?![一二三四五六日天])|(?a:(?i:\b(last|this|next) week\b))")
# “下个月底”的“底”跟着它前面的那个月（J2.14）
_MONTH_REL = re.compile(
    r"(?P<rel>上|下|本|这)\s?个?\s?月(?:份)?(?P<edge>底|末|初)?|(?a:(?i:\b(?P<en>last|this|next) month\b))|(?P<alone>月底|月末|月初|月中)"
)
_YEAR_REL = re.compile(r"(今年|明年|去年|前年|后年)|(年底|年末|年初)|(?a:(?i:\b(last|this|next) year\b))")
_QUARTER_REL = re.compile(r"(本|这|下|上)\s?个?\s?季度")
_YEAR_DELTA = {"今年": 0, "明年": 1, "去年": -1, "前年": -2, "后年": 2, "this": 0, "next": 1, "last": -1}
_MONTH_DELTA = {"上": -1, "下": 1, "本": 0, "这": 0, "last": -1, "next": 1, "this": 0}


@dataclass(frozen=True)
class _Anchor:
    """一句里相对的年、月：“下个月 3 号”“明年 11 月”拿它当锚。"""

    year: int | None = None
    month: tuple[int, int] | None = None


def _anchor(text: str, day0: date, position: int | None = None) -> _Anchor:
    """position 处的日期用哪个相对的年、月当锚：它前面离它最近的那个；前面没有，用这一句里第一个。
    “这个月 20 号付首款，下个月 20 号付尾款”两个 20 号各有各的月。"""
    years = [
        (match.start(), day0.year + _YEAR_DELTA[(match.group(1) or match.group(3)).lower()])
        for match in _YEAR_REL.finditer(text)
        if match.group(1) or match.group(3)
    ]
    months = [
        (match.start(), _add_months(day0.year, day0.month, _MONTH_DELTA[(match["rel"] or match["en"]).lower()]))
        for match in _MONTH_REL.finditer(text)
        if match["rel"] or match["en"]
    ]

    def pick(marks: list[tuple[int, object]]) -> object:
        before = [value for start, value in marks if position is not None and start < position]
        if before:
            return before[-1]
        return marks[0][1] if marks else None

    return _Anchor(pick(years), pick(months))  # type: ignore[arg-type]


def _explicit_dates(text: str, day0: date, times: Times) -> bool:
    """写明了的日期加进 times。text 是 _nfkc 以后的。返回有没有。"""
    explicit = False
    for _, one, found in _date_matches(text, day0):
        times.update(one)
        times.invalid = times.invalid or one.invalid
        explicit = explicit or found
    return explicit


def _date_matches(
    text: str, day0: date, spans: list[tuple[int, int]] | None = None
) -> list[tuple[str, Times, bool]]:
    """一句里每一处写明的日期：(写法, 能是哪些时间, 认没认出来)。text 是 _nfkc 以后的。给了 spans 的，每一处的
    (起点, 终点) 按同样的顺序放进去。

    从具体到笼统认，认过的那一段后面的写法不再认（“2026 年 11 月 9 日”里的“11 月 9 日”）。没写年月的按这一句里
    相对的年、月算（“下个月 3 号”“明年 11 月”），没有就取离说话那天最近的；这一句里写过年份的，那几年也算。
    写了不存在的日期，那一处的 invalid。
    """
    results: list[tuple[str, Times, bool]] = []
    years = {int(year) for year in re.findall(r"(?<!\d)(20\d{2}|19\d{2})(?!\d)", text)}
    claimed: list[tuple[int, int]] = []
    for kind, pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < other_end and other_start < end for other_start, other_end in claimed):
                continue
            claimed.append((start, end))
            if spans is not None:
                spans.append((start, end))  # 下面每一处正好放一个结果
            anchor = _anchor(text, day0, start)
            times = Times()
            explicit = False
            groups = match.groups()
            found: list[date] = []
            invalid = False
            if kind == "ymd":
                day = _valid(int(groups[0]), int(groups[1]), int(groups[2]))
                invalid = day is None
                if day is not None:
                    found.append(day)
                    if groups[3] is not None and int(groups[3]) <= 23 and int(groups[4]) <= 59:
                        times.minutes.add((day, int(groups[3]), int(groups[4])))
            elif kind == "dmy":
                first, second, year = int(groups[0]), int(groups[1]), int(groups[2])
                found = [option for day, month in {(first, second), (second, first)} if (option := _valid(year, month, day))]
                invalid = not found
            elif kind == "en_dmy":
                month = _EN_MONTHS.get(groups[1][:3].title())
                year = int(groups[2]) + (2000 if len(groups[2]) == 2 else 0)
                day = _valid(year, month, int(groups[0])) if month else None
                invalid = day is None
                found = [day] if day else []
            elif kind == "cn_ymd":
                day = _valid(int(groups[0]), _cn_or_int(groups[1]) or 0, _cn_or_int(groups[2]) or 0)
                invalid = day is None
                found = [day] if day else []
            elif kind in ("cn_md", "md", "en_md", "en_dm"):
                if kind in ("cn_md", "md"):
                    month, day_number, year_text = _cn_or_int(groups[0]), _cn_or_int(groups[1]), None
                elif kind == "en_md":
                    month, day_number, year_text = _EN_MONTHS.get(groups[0][:3].title()), int(groups[1]), groups[2]
                else:
                    month, day_number, year_text = _EN_MONTHS.get(groups[1][:3].title()), int(groups[0]), groups[2]
                if not month or not day_number or not 1 <= month <= 12:
                    invalid = kind == "cn_md"
                else:
                    candidates = {int(year_text)} if year_text else ({anchor.year} if anchor.year else set()) | years
                    options = [option for year in candidates if (option := _valid(year, month, day_number))]
                    if not year_text and anchor.year is None:
                        near = _nearest_year(month, day_number, day0)
                        if near is not None:
                            options.append(near)
                    invalid = not options and kind != "md"
                    found = options

            elif kind in ("cn_ym", "ym"):
                month = _cn_or_int(groups[1])
                if month and 1 <= month <= 12:
                    times.add_month(int(groups[0]), month)
                    explicit = True
                else:
                    times.invalid = True
                results.append((kind, times, explicit))
                continue
            elif kind == "yq":
                times.add_quarter(int(groups[0]), int(groups[1]))
                results.append((kind, times, True))
                continue
            elif kind == "cn_y":
                times.years.add(int(groups[0]))
                results.append((kind, times, True))
                continue
            elif kind in ("cn_m", "en_m"):
                month = _cn_or_int(groups[0]) if kind == "cn_m" else _EN_MONTHS.get(groups[0][:3].title())
                if not month or not 1 <= month <= 12:
                    times.invalid = kind == "cn_m"
                    results.append((kind, times, False))
                    continue
                if anchor.year is not None:
                    times.add_month(anchor.year, month)
                else:
                    near = _nearest_year(month, 1, day0)
                    if near is not None:
                        times.add_month(near.year, month)
                results.append((kind, times, True))
                continue
            elif kind == "cn_d":
                number = int(groups[0])
                if anchor.month is not None:
                    day = _valid(anchor.month[0], anchor.month[1], number)
                else:
                    day = _nearest_month(number, day0)
                found = [day] if day else []
                invalid = not found
            elif kind == "q":
                quarter = _cn_or_int(groups[0] or groups[1])
                if quarter and 1 <= quarter <= 4:
                    for year in ({anchor.year} if anchor.year else {day0.year, day0.year + 1}):
                        times.add_quarter(year, quarter)
                    explicit = True
                results.append((kind, times, explicit))
                continue
            if invalid:
                times.invalid = True
            for day in found:
                times.add_day(day)
                explicit = True
            results.append((kind, times, explicit))
    return results


def _relative_dates(text: str, day0: date, times: Times) -> bool:
    """相对的说法按说话那天换算，加进 times。返回有没有。"""
    explicit = False
    for match in _RELATIVE_DAY.finditer(text):
        shift = _RELATIVE_DAYS.get(match.group(0).lower())
        day = _shift(day0, shift) if shift is not None else None
        if day is not None:
            explicit = True
            times.add_day(day)
    start_of_week = _shift(day0, -day0.weekday())
    for match in _WEEKDAY.finditer(text):
        prefix, name = match.group(1) or "", match.group(2)
        shifts = {"上": (-7,), "上个": (-7,), "下": (7,), "下个": (7,), "本": (0,), "这": (0,), "这个": (0,)}.get(
            prefix, (-7, 0, 7)
        )
        for shift in shifts:
            day = _shift(start_of_week, shift + _WEEKDAYS[name]) if start_of_week else None
            if day is not None:
                explicit = True
                times.add_day(day)
    for match in _WEEK.finditer(text):
        word = (match.group(1) or match.group(2) or "").lower()
        start = _shift(start_of_week, {"上": -7, "last": -7, "下": 7, "next": 7}.get(word, 0)) if start_of_week else None
        for offset in range(7):
            day = _shift(start, offset) if start else None
            if day is not None:
                explicit = True
                times.add_day(day)
    for match in _MONTH_REL.finditer(text):
        explicit = True
        word = (match["rel"] or match["en"] or "").lower()
        if word:
            year, month = _add_months(day0.year, day0.month, _MONTH_DELTA[word])
        else:
            year, month = _anchor(text, day0, match.start()).month or (day0.year, day0.month)
        if not 1 <= year <= 9999:
            continue
        times.add_month(year, month)
        edge = match["alone"] or (f"月{match['edge']}" if match["edge"] else None)
        if edge in ("月底", "月末"):
            last = calendar.monthrange(year, month)[1]
            for offset in range(3):
                times.add_day(date(year, month, last - offset))
        elif edge == "月初":
            for offset in range(3):
                times.add_day(date(year, month, 1 + offset))
    for match in _YEAR_REL.finditer(text):
        explicit = True
        word = (match.group(1) or match.group(3) or "").lower()
        if word:
            times.years.add(day0.year + _YEAR_DELTA[word])
            continue
        anchor = _anchor(text, day0, match.start())
        year = anchor.year if anchor.year is not None else day0.year  # “明年年底”是明年的年底
        if 1 <= year <= 9999:
            times.add_month(year, 12 if match.group(2) in ("年底", "年末") else 1)
    for match in _QUARTER_REL.finditer(text):
        explicit = True
        index = day0.year * 4 + (day0.month - 1) // 3 + {"下": 1, "上": -1}.get(match.group(1), 0)
        times.add_quarter(index // 4, index % 4 + 1)
    return explicit


def _clock_pairs(text: str) -> set[tuple[int, int]]:
    """事实里写成 HH:MM 的时刻，时间段 10:00-12:00 的后一个也算（它前面的 - 是连接号）。"""
    return zh_clock.hhmm(text)


def _clocks(text: str) -> set[tuple[int, int]]:
    """原文里的时刻：HH:MM（带 PM、“下午”的也算加 12 小时的那个）、三点一刻、3 点 15、六点半……（javmem.zh.clock）。"""
    return zh_clock.clocks(text)


def sentence_times(text: str, said: datetime, *, said_day: bool = True) -> Times:
    """一句话对得上的时间：写明了的日期、相对的说法；都没有就是说话那天（said_day 是 False 时不算）。

    写了几点几分的配上这几天。
    """
    times = Times()
    day0 = said.date()
    clean = _nfkc(text)
    explicit = _explicit_dates(clean, day0, times)
    explicit = _relative_dates(clean, day0, times) or explicit
    clocks = _clocks(clean)
    times.clock = set(clocks)
    if not explicit and not said_day:
        return times
    if not explicit:
        times.add_day(day0)
        if not clocks:
            times.add_minute(day0, said.hour, said.minute)
    for hour, minute in clocks:
        for known in list(times.days):
            times.minutes.add((known, hour, minute))
    return times


def said_times(moments: Iterable[datetime]) -> Times:
    """说话的那一天、那一刻。只到天：事实写“2026 年 11 月交货”不能因为是 11 月说的就算对上。"""
    times = Times()
    for moment in moments:
        times.days.add(moment.date())
        times.minutes.add((moment.date(), moment.hour, moment.minute))
    return times


def text_times(text: str, said: datetime) -> list[Times]:
    """事实文字里每一处写明的日期、相对的说法各自能是哪些时间。认法和原文的一样。"""
    clean = _nfkc(text)
    found = [
        times
        for kind, times, _ in _date_matches(clean, said.date())
        if kind in _TEXT_DATE_KINDS and (times.invalid or not times.empty)
    ]
    for pattern in (_RELATIVE_DAY, _WEEKDAY):
        for match in pattern.finditer(clean):
            times = Times()
            _relative_dates(match.group(0), said.date(), times)
            if not times.empty:
                found.append(times)
    return found


def _fits(one: Times, allowed: Times) -> bool:
    """文字里的一处日期（可能是几种）至少有一种在允许的里面，按它自己最细的一级比。不存在的日期对不上。"""
    if one.invalid:
        return False
    if one.days:
        return bool(one.days & allowed.days)
    if one.months:
        return bool(one.months & allowed.months)
    if one.quarters:
        return bool(one.quarters & allowed.quarters)
    return bool(one.years & allowed.years)


_OCCURRED = re.compile(
    r"(?P<y>\d{4})(?:-(?P<m>\d{1,2})(?:-(?P<d>\d{1,2})(?:[T ](?P<H>\d{1,2}):(?P<M>\d{2})(?::\d{2}(?:\.\d+)?)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?)?)?|-?[qQ](?P<q>[1-4]))?"
)
_COARSER = {"minute": "day", "day": "month", "month": "year", "quarter": "year", "year": None}


@dataclass(frozen=True)
class Occurred:
    precision: str
    year: int
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None
    quarter: int | None = None
    offset: timedelta | None = None

    def at(self, precision: str) -> str:
        """写成这一级的 ISO 写法。"""
        if precision == "year":
            return f"{self.year:04d}"
        if precision == "quarter":
            quarter = self.quarter or ((self.month or 1) - 1) // 3 + 1
            return f"{self.year:04d}-Q{quarter}"
        if precision == "month":
            return f"{self.year:04d}-{self.month:02d}"
        if precision == "day":
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        text = f"{self.year:04d}-{self.month:02d}-{self.day:02d}T{self.hour:02d}:{self.minute:02d}"
        if self.offset is not None:
            total = int(self.offset.total_seconds()) // 60
            sign = "-" if total < 0 else "+"
            hours, minutes = divmod(abs(total), 60)
            text += f"{sign}{hours:02d}:{minutes:02d}"
        return text

    def local(self, offset: timedelta | None) -> Occurred | None:
        """带时区的时刻换成说话人那边的时区。换不了（年份在日历两头）返回 None。"""
        if self.precision != "minute" or self.offset is None or offset is None or self.offset == offset:
            return self
        try:
            moment = datetime(self.year, self.month or 1, self.day or 1, self.hour or 0, self.minute or 0)
            moment = moment - self.offset + offset
        except (OverflowError, ValueError):
            return None
        return Occurred("minute", moment.year, moment.month, moment.day, moment.hour, moment.minute, offset=offset)


def parse_occurred(value: object) -> Occurred | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    match = _OCCURRED.fullmatch(value.strip())
    if match is None:
        return None
    year = int(match["y"])
    if match["q"]:
        return Occurred("quarter", year, quarter=int(match["q"]))
    if not match["m"]:
        return Occurred("year", year)
    month = int(match["m"])
    if not 1 <= month <= 12:
        return None
    if not match["d"]:
        return Occurred("month", year, month)
    day = int(match["d"])
    if _valid(year, month, day) is None:
        return None
    if not match["H"]:
        return Occurred("day", year, month, day)
    hour, minute = int(match["H"]), int(match["M"])
    if hour > 23 or minute > 59:
        return None
    offset = None
    if match["tz"]:
        tz = match["tz"]
        if tz == "Z":
            offset = timedelta(0)
        else:
            digits = tz[1:].replace(":", "")
            if int(digits[:2]) > 14 or int(digits[2:]) > 59:
                return None
            offset = (-1 if tz[0] == "-" else 1) * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return Occurred("minute", year, month, day, hour, minute, offset=offset)


def _matches(value: Occurred, precision: str, times: Times) -> bool:
    if precision == "minute":
        moment = _valid(value.year, value.month or 1, value.day or 1)
        return moment is not None and (moment, value.hour, value.minute) in times.minutes
    if precision == "day":
        moment = _valid(value.year, value.month or 1, value.day or 1)
        return moment is not None and moment in times.days
    if precision == "month":
        return (value.year, value.month) in times.months
    if precision == "quarter":
        quarter = value.quarter or ((value.month or 1) - 1) // 3 + 1
        return (value.year, quarter) in times.quarters
    return value.year in times.years


# ---------- 窗口：证据之外，“窗口里别处有”用 ----------


@dataclass
class _WindowFacts:
    """整个窗口（每一行，能当证据的和上文都算）里有的数、币种、时间、比对写法的全文。"""

    numbers: _Numbers
    currencies: set[str]
    times: Times  # 每一行写明了的日期和相对的说法，不算说话那天：给 occurred_at 用
    spoken: Times  # 加上每一行说话的那一天、那一刻：给事实文字里的日期用
    folded: str  # fold 的写法
    soft: str  # soft 的写法
    clocks: set[tuple[int, int]]
    texts: tuple[str, ...] = ()  # 每一行原样（一个字母的单位要看原文这个数后面跟着什么）


def _window_facts(refs: Mapping[str, _Ref], extras: Sequence[str]) -> _WindowFacts:
    texts = [ref.shown for ref in refs.values()]
    times = Times()
    seen: set[tuple[str, datetime]] = set()
    clocks: set[tuple[int, int]] = set()
    for ref in refs.values():
        if (ref.shown, ref.block.said_at) not in seen:
            seen.add((ref.shown, ref.block.said_at))
            found = sentence_times(ref.shown, ref.block.said_at, said_day=False)
            times.update(found)
            clocks |= found.clock
    spoken = said_times(ref.block.said_at for ref in refs.values())
    spoken.update(times)
    everything = texts + list(extras)
    return _WindowFacts(
        numbers=_source_numbers(everything),
        currencies=set().union(*(_currency_codes(_nfkc(text)) for text in everything)) if everything else set(),
        times=times,
        spoken=spoken,
        folded=fold(" ".join(everything)),
        soft=" " + " ".join(soft(text) for text in everything) + " ",
        clocks=clocks,
        texts=tuple(everything),
    )


# ---------- G2 数字、币种 ----------

_UNITS = r"万亿|千万|百万|亿|万|千|million|billion|thousand|bn|mn|[kKmMbB](?![a-zA-Z])"
_NUMBER = re.compile(rf"(?<![\d.])(-)?(\d+(?:,\d{{3}})*(?:\.\d+)?)(?:\s?({_UNITS}))?")
_SCALE = {
    "万": 10_000, "千": 1_000, "百万": 1_000_000, "千万": 10_000_000, "亿": 100_000_000, "万亿": 10**12,
    "k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "bn": 1_000_000_000, "mn": 1_000_000,
    "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000,
}
_MAX_DIGITS = 30  # 再长的不当数比：不是金额，换算也会溢出
# 事实里的中文数，后面跟着量词、单位才算数（“三个人”“第三季度”），“一起、一点、万一”不算
_MEASURE = (
    r"个|人|位|名|次|回|遍|天|日|周|星期|月|年|号|份|张|股|元|块|角|分钟|小时|秒|箱|件|台|套|条|项|家|笔|期|档|倍|成|层|楼|"
    r"场|轮|批|单|只|支|本|页|行|列|岁|斤|公斤|吨|米|公里|季度|季|篇|首|部|辆|架|艘|间|座|所|处|种|类|步|度|折|码|%|％"
)
_CN_FACT_NUMBER = re.compile(
    rf"(第)?([零〇一二两三四五六七八九十百千万亿]+)(?=\s?(?:{_MEASURE}))|([零〇一二两三四五六七八九十百千万亿]*[一二两三四五六七八九][十百千万亿]+)"
)
_NOT_CN_NUMBER = ("一点", "一些", "一下", "一样", "一共", "一起", "一定", "一直", "一切", "一般", "一边", "一会", "一旦",
                  "一致", "一同", "一律", "一向", "一再", "一并", "一手", "万一", "统一", "唯一", "不一", "一天天")
_NOT_CN_AMOUNT = re.compile(r"(?<![零〇一二两三四五六七八九十百])千万(?=[不别要记得务])")  # 千万不要、千万记得
# 币种的写法在 javmem.zh.numbers（CURRENCIES、currency_codes）
# 口语的 W、w 是万（RMB60W），只在挨着币种时：不挨着的（2000W 的取暖器）是瓦（J2.14）
_W_UNIT = re.compile(r"\s?[wW](?![a-zA-Z])")
# 基点：50bp 是 0.5 个百分点（J2.14）
_BASIS_POINTS = re.compile(r"\s?(?:bps?|个基点|基点)(?![a-zA-Z])", re.IGNORECASE)
_AMBIGUOUS_UNITS = frozenset("kmb")  # 一个字母的单位：事实里的 5m 可能是 5 米、100M 可能是 100 兆
# 原文这个数后面跟着这些（同一类的单位），事实里写成一个字母的单位也算对得上（“5 米”写成“5m”）；
# 原文是“300 万”“5k”这种量级的，写成“300m”“5m”差了几个数量级，不算
_SAME_KIND = {
    "m": r"米|公尺|分钟|min|mbps|mb|兆",
    "k": r"千米|公里|千克|公斤|km|kg|kb|千瓦|kw",
    "b": r"字节|bytes?|bps",
}


@dataclass
class _Numbers:
    plain: set[str]  # 不看正负号的
    negative: set[str]  # 带负号的


def _canonical(number: str) -> str:
    """数的写法统一：去掉千分位，小数点后面多余的 0 去掉。"""
    digits = number.replace(",", "")
    if "." in digits:
        digits = digits.rstrip("0").rstrip(".")
    return digits.lstrip("0") or "0"


def _scaled(number: str, unit: str | None) -> str | None:
    """带“万、亿、k、M”的数换成整数写法：3200万 → 32000000。"""
    if not unit:
        return None
    factor = _SCALE.get(unit.lower())
    if factor is None or len(number) > _MAX_DIGITS:
        return None
    try:
        value = float(number.replace(",", "")) * factor
    except ValueError:
        return None
    if not math.isfinite(value) or value > 1e21:
        return None
    return str(int(value)) if value == int(value) else _canonical(f"{value:.6f}")


@dataclass(frozen=True)
class _Value:
    number: str  # 写法统一的数
    scaled: str | None  # 带单位的换算以后
    alternatives: tuple[str, ...]  # 带 % 的换成小数（85% 和 0.85）；基点换成百分数（50bp 和 0.5%、0.005）
    negative: bool
    ambiguous: str | None = None  # 单位是一个字母的（k、m、b，小写）：5m 可能是 5 米
    raw: str = ""  # 写的那个数，去掉千分位（按数值比用）


def _mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    for start, end in spans:
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def _values(text: str, *, skip_dates: bool, skip: Sequence[tuple[int, int]] = ()) -> list[_Value]:
    """text 是 _nfkc 以后的。skip_dates：G6 查的那几种日期写法、时刻里的数不算；skip 里的那几段也不算。"""
    masked = _mask(text, skip)
    if skip_dates:
        for kind, pattern in _DATE_PATTERNS:
            if kind in _TEXT_DATE_KINDS:
                for match in pattern.finditer(masked):
                    masked = masked[: match.start()] + " " * (match.end() - match.start()) + masked[match.end() :]
        for match in _CLOCK.finditer(masked):
            end = match.end()
            tail = _CLOCK_RANGE_TAIL.match(masked, end)  # 10:00-12:00 的后一个时刻
            if tail is not None:
                end = tail.end()
            masked = masked[: match.start()] + " " * (end - match.start()) + masked[end:]
    found = []
    for match in _NUMBER.finditer(masked):
        digits = match.group(2)
        if len(digits) > _MAX_DIGITS:
            continue
        unit, end = match.group(3), match.end()
        if unit is None and (w_unit := _W_UNIT.match(masked, end)) is not None:
            before = masked[max(0, match.start() - 4) : match.start()]
            if zh_numbers.CURRENCY_BEFORE.search(before) or zh_numbers.CURRENCY_AFTER.match(masked[w_unit.end() :]):
                unit, end = "万", w_unit.end()
        alternatives: list[str] = []
        if unit is None and masked[end : end + 1] == "%":
            alternatives.append(_canonical(f"{float(digits.replace(',', '')) / 100:.6f}"))
        if unit is None and _BASIS_POINTS.match(masked, end):
            points = Decimal(digits.replace(",", ""))
            alternatives += [zh_numbers.decimal_text(points / 100), zh_numbers.decimal_text(points / 10_000)]
        # 负号看原文：挡掉的日期、时刻换成了空格，“10:00-12” 的 “-” 前面原本是数字，是连接号
        negative = match.group(1) is not None and not text[: match.start()].rstrip().endswith(tuple("0123456789"))
        ambiguous = unit.lower() if unit is not None and unit.lower() in _AMBIGUOUS_UNITS else None
        found.append(
            _Value(_canonical(digits), _scaled(digits, unit), tuple(alternatives), negative, ambiguous, digits.replace(",", ""))
        )
    return found


def _source_numbers(texts: Iterable[str]) -> _Numbers:
    """原文里的数：带单位的换算一份；带负号的另记；中文写的数换成数字（javmem.zh.numbers.cn_values：两千五、一万二、
    三点五万、百分之三十；几个字连着的只有“九十月份”“三四天”这种才一个字一个数）。"""
    numbers = _Numbers(set(), set())
    for text in texts:
        clean = _nfkc(text)
        for value in _values(clean, skip_dates=False):
            for item in (value.number, value.scaled, *value.alternatives):
                if item is not None:
                    numbers.plain.add(item)
                    if value.negative:
                        numbers.negative.add(item)
        numbers.plain |= zh_numbers.cn_values(clean)
    return numbers


def _claim_values(claim: str, skip: Sequence[tuple[int, int]] = ()) -> list[_Value]:
    """事实里的数：阿拉伯数字（日期、时刻里的不算），加上中文写的小数（三点五万、百分之三十）、后面跟着量词的中文数。
    skip 里的那几段不算（当日期对得上原文的月日、几号、季度）。"""
    values = _values(claim, skip_dates=True, skip=skip)
    masked = _mask(claim, skip)
    for word in _NOT_CN_NUMBER:
        masked = masked.replace(word, " " * len(word))
    masked = _NOT_CN_AMOUNT.sub("  ", masked)
    decimals = zh_numbers.cn_decimals(masked)
    values += [_Value(number, None, alternatives, False) for _, _, number, alternatives in decimals]
    masked = _mask(masked, [(start, end) for start, end, _, _ in decimals])
    for match in _CN_FACT_NUMBER.finditer(masked):
        run = match.group(2) or match.group(3)
        if zh_numbers.after_digit(masked, match.start()) and all(char in "十百千万亿" for char in run):
            continue  # “5 万元”的“万”是 5 的单位，前面按 5 万算过了
        number = cn_number(run)
        if number is not None:
            values.append(_Value(str(number), None, (), False))
    return values


def _has(numbers: _Numbers, value: _Value, texts: Sequence[str] = ()) -> bool:
    """带单位的按换算以后的比，带负号的要对上负号。单位是一个字母的，原文这个数后面跟着同一类的单位（texts 里找）也算。"""
    pool = numbers.negative if value.negative else numbers.plain
    if value.scaled is not None:
        if value.scaled in pool:
            return True
        if value.ambiguous is None:
            return False
        pattern = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)\s?(?:{_SAME_KIND[value.ambiguous]})", re.IGNORECASE)
        wanted = Decimal(value.raw or value.number)
        return any(
            Decimal(match.group(1)) == wanted for text in texts for match in pattern.finditer(_nfkc(text).replace(",", ""))
        )
    return value.number in pool or any(item in pool for item in value.alternatives)


_currency_codes = zh_numbers.currency_codes
_DATES_AS_NUMBERS = frozenset({"md", "cn_d", "q"})


def _fitting_dates(claim: str, proofs: Sequence[Proof], window: _WindowFacts) -> list[tuple[int, int]]:
    """事实里的月日（11/18）、几号（18 号）、季度（Q4），当日期对得上证据（或者窗口里别的行）的时间的那几段（J2.14）。
    这几段不再按数比：“下周三交货”写成“11/18 交货”“18 号交货”是对的。对不上的照旧按数比。"""
    if not proofs:
        return []
    allowed = Times()
    for proof in proofs:
        allowed.update(sentence_times(proof.shown, proof.said_at))
    allowed.update(said_times(proof.said_at for proof in proofs))
    spans: list[tuple[int, int]] = []
    found = _date_matches(claim, min(proof.said_at for proof in proofs).date(), spans)
    return [
        span
        for (kind, times, _), span in zip(found, spans, strict=True)
        if kind in _DATES_AS_NUMBERS and not times.empty and (_fits(times, allowed) or _fits(times, window.spoken))
    ]


def check_numbers(
    fact: Mapping[str, object], proofs: Sequence[Proof], names: Sequence[str], window: _WindowFacts
) -> tuple[list[Finding], list[str]]:
    """G2：事实文字和值里的数、币种，原文里要有。返回 (没过的, notes)。

    “3200万”和“32,000,000”算一个数；事实里写了单位的按换算以后的比，“3 万”对不上“3 千”；负号要对上。
    G6 查的日期写法、时刻里的数不在这里比；月日（11-20）、几号、季度在这里按数比，当日期对得上原文时间的不比。
    实体的名字在窗口里写着的才算原文。
    """
    evidence = [proof.shown for proof in proofs] + list(names)
    plain = _source_numbers(evidence)
    claim = _nfkc(str(fact.get("text") or "")) + "\n" + _nfkc(str(fact.get("value") or ""))
    findings: list[Finding] = []
    notes: list[str] = []
    for value in _claim_values(claim, _fitting_dates(claim, proofs, window)):
        if _has(plain, value, evidence):
            continue
        if _has(window.numbers, value, window.texts):
            notes.append("G2:number_elsewhere")
            continue
        findings.append(Finding("G2", "number_mismatch"))
        break
    added = _currency_codes(claim) - set().union(*(_currency_codes(_nfkc(text)) for text in evidence))
    if added:
        if added <= window.currencies:
            notes.append("G2:currency_elsewhere")
        else:
            findings.append(Finding("G2", "currency_added"))
    return findings, notes


# ---------- G3 否定、G4 条件、G5 语气 ----------

_CLAUSE = re.compile(r"[，,；;：:。！？!?|｜\n]+")
_CLAUSE_MARK = re.compile(r"[，,；;：:。！？!?|｜\n]")
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_CJK_RUN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

# 带“不、没、无”却不是否定的写法，先挡掉
_NOT_NEGATION = re.compile(
    r"不过|不同(?!意|步|时)|不少|不错|不断|不仅|不久|不论|不如|不止|不妨|不禁|不得不|"
    r"不一会|差不多|无论|未来|非常|除非|是否|别的|别人|别处|别名|别称|别号|分别|区别|级别|类别|特别|个别|告别|识别|"
    r"辨别|差别|性别|鉴别|派别|别墅|无人机|无线|无锡|非洲|是非|淹没|埋没|出没|莫斯科|否则|"
    r"说不定|说不准|保不齐|没准|"  # 语气词（G5 管），不是否定（J2.14）
    r"(?:^|(?<=[，。；！？,.;!?\s]))要不(?:然)?|不然"
)
# 比较的说法：“不低于”就是 ≥。事实里写了它不算“凭空加了否定”，但算是保留了原文里的否定
_COMPARATIVES = re.compile(r"不低于|不高于|不少于|不多于|不超过|不超出|不大于|不小于|不晚于|不早于|不足|不到|未满|未达|未到|没到|没有到|没达到")
_A_NOT_A = re.compile(r"([\u3400-\u9fff])[不没]\1")  # 是不是、要不要、有没有：问句，不是否定
_NEGATION = re.compile(
    r"[不没沒未无無非别別勿毋甭]|并非|(?a:(?i:\b(?:not|no|never|none|nor|neither|cannot|without|nothing|nobody)\b|n't\b))"
)
# 用别的说法写的否定：原文里有它，事实写成“不、未”也算有出处；事实里有它，算原文的否定留着
_NEGATIVE_VERBS = re.compile(
    r"禁止|拒绝|拒|取消|停止|停掉|停用|暂停|撤销|撤回|作废|删除|删掉|避免|免除|缺少|缺了|少了|失败|否认|否决|关闭|关掉|"
    r"退出|放弃|叫停|喊停|停下|别再|不再|仅当|除非|除外|以外|之外|待[定办付收交批审确签核]|仍待|尚待|有待|"
    r"(?a:(?i:\b(?:refuse|reject|cancel|stop|avoid|deny|fail|lack|except|excluding)\w*\b))"
)
# 比较：原文写“≥”“低于”，事实写成“不低于”“不到”也算有出处；但事实里的“低于”不算留着原文的“不低于”
_COMPARISON = re.compile(r"低于|少于|小于|短于|(?a:(?i:\b(?:below|less|fewer)\b))|[≥≤≠]")
_NEGATIVE_WORDS = re.compile(f"{_NEGATIVE_VERBS.pattern}|{_COMPARISON.pattern}")
_CONDITION = re.compile(
    r"如果|假如|倘若|若(?!干)|(?<![主需只还就想重不必])要是|除非|一旦|万一|前提是|条件是|"
    r"只要(?=[^。！？!?；;]*?(?:就|便|都))|(?a:(?i:\b(?:if|unless|only if|in case|provided that|as long as)\b))"
)
# 写在后面也管前面那一段的条件词：“周五发货，除非定金没到”。“如果、若”写在后面的多半另起一件事（“若有变化再通知”）
_TRAILING_CUE = re.compile(r"除非|前提是|条件是|(?a:(?i:\b(?:if|unless|only if|in case|provided that|as long as)\b))")
# 条件后面接结果的词：没有逗号的一句话按它切成“条件 / 结果”
_THEN = re.compile(r"就|则|那么|那(?=[是就么样])|便|(?a:(?i:\bthen\b))")
# 事实里写了这些，算保留了条件（单个的字“时、后、就”不算）
_CONDITION_WORDS = re.compile(
    r"条件|前提|视情况|取决于|看情况|届时|到时|的时候|之时|时候|考虑到|鉴于|基于|只有|仅当|假设|的话|一旦|只要|除非|"
    r"如要|如需|如有|如若|如是|若要|若需|若有|"
    r"万一|如果|若|要是|否则|不然|(?a:(?i:\b(?:when|once|after|if|unless|provided|depending)\b))"
)
_HEDGE = re.compile(
    r"可能|也许|或许|大概|估计|似乎|好像|看来|看起来|不确定|不一定|说不定|说不准|未必|预计|预期|有望|争取|尽量|考虑|打算|"
    r"计划|准备|希望|倾向|建议|拟|待定|暂定|初步|应该|想要|想|"
    # J2.14：口语的推测、传闻、看情况，句首的“要不”（建议）
    r"多半|八成(?=[是能会要就得在不没有])|恐怕|怕是|没准|保不齐|兴许|听说|据说|据传|传闻|看情况|视情况|"
    r"(?:^|(?<=[，。；！？,.;!?\s]))要不(?![然要是])|"
    r"(?a:(?i:\b(?:maybe|might|may|perhaps|probably|possibly|likely|plan(?:s|ned|ning)?|consider(?:s|ed|ing)?|hope|"
    r"expect(?:s|ed)?|tentative|tbd|would like|should)\b))"
)
_NOT_HEDGE = re.compile(r"想法|理想|思想|想到|回想|联想|梦想|计划书|交易计划|准备金|建议书|听说过")
# 事实自己写着拿不准的（“是否签收仍需确认”“待确认”），不算写成了说定了的（J2.14）
_CLAIM_UNSURE = re.compile(r"是否|能否|会否|待确认|未确认|需确认|有待|不确定|尚不清楚|不清楚")
_QUESTION_WORDS = re.compile(r"什么|怎么|为什么|为何|哪|谁|几|多少|是否|是不是|有没有|能不能|可不可以|要不要|会不会|对不对|好不好|行不行")
# 请对方做事的问句是请求，不是只有问题：“可以帮我……吗？”“Could you …?”；“请问”是问
_REQUEST = re.compile(
    r"帮我|帮忙|请(?!问)|麻烦|能不能|能否|可否|可不可以|可以帮|可以吗|好吗|行吗|(?a:(?i:\b(?:could|can|would|will) you\b|\bplease\b))"
)
_STOPWORDS = frozenset(
    "a an the of to in on at for and or but is are was were be been being am will would shall should can could do does did "
    "has have had it its this that these those there here as by with from into than then so not no".split()
)

# ---- J2.14 加的几样 ----

# 软性变化（G5）：暂时的说法和说定了的说法。“先放放”写成“取消”、“取消”写成“暂缓”都改了意思
_SOFT_CHANGE = re.compile(
    r"先放放|放一放|放一边|先搁|搁置|暂缓|缓一缓|缓缓|先缓|延后|推迟|延期|改期|改到|挪到|延到|往后推|先停|暂停|先不|暂不|"
    r"暂时|观望|再看看|再看|回头再|以后再|再说|再议|再考虑|不急|先等|待定|暂定|改成|改为|改坐|改乘|换成|改在|推到|顺延|往后挪|"
    r"(?a:(?i:\b(?:on hold|hold off|pause[sd]?|postpone[sd]?|defer(?:red|s)?)\b))"
)
_HARD_CHANGE = re.compile(
    r"取消|作废|终止|放弃|否决|拒绝|撤销|废除|废止|作罢|不再|永久|彻底|再也不|决定不|不(?![急错少多久早行够])"
    + _CJK.pattern
    + r"了|(?a:(?i:\b(?:cancel(?:led|ed|s)?|terminate[sd]?|abandon(?:ed|s)?|reject(?:ed|s)?|scrap(?:ped|s)?)\b))"
)
# 否定（G3）：几个字的否定词整个算一处，被否定的那个词从它后面找（“无需审批”的“审批”，“没有一个人同意”的“人同意”）
_NEGATION_WORDS = (
    "没有一个", "没有任何", "一个也没有", "一个都没有", "一点也不", "一点都不", "从来没有", "从来不", "从来没", "绝对不",
    "并没有", "还没有", "不需要", "不可以", "不应该", "不愿意", "一个也没", "一个都没", "没一个", "从不", "从没", "从未",
    "绝不", "决不", "并不", "并没", "并未", "尚未", "还没", "还未", "未曾", "无需", "无须", "毋须", "不必", "不用", "不需",
    "无法", "没法", "不再", "别再", "不要", "不能", "不会", "不可", "不得", "不肯", "不该", "不应", "不想", "不愿", "不准",
    "不许", "没有", "没能", "不是",
)
_NEGATION_CUE = re.compile(
    "|".join(sorted((re.escape(word) for word in _NEGATION_WORDS), key=len, reverse=True)) + "|" + _NEGATION.pattern
)
# 双重否定是肯定（“不是不能接受”“没有不同意的”“不得不去”“非去不可”）。“没有不良记录”的“不良”是一个词
_ONE_WORD = r"(?![良利满安便足适当妥法宜慎测幸正平稳])"  # 后面跟着这些的“不X”是一个词（不良、不适、不当）
_DOUBLE_NEGATION = re.compile(
    r"(?:不是|并不是|并非|没有|没)(?:不|没有?)" + _ONE_WORD + r"|不(?:能|会|得|可|敢)不" + _ONE_WORD + r"|"
    r"非" + _CJK.pattern + r"{1,4}?不可(?=$|[了的啊呀吧\s，。！？,.!?；;])|不无" + _ONE_WORD + r"|无不" + _ONE_WORD
    + r"|未尝不|未必不"
)
# 否定词后面先跳过这些字再找被否定的词：“别再提醒”的“提醒”，“不是这个月”的“这个月”
_SKIP_AFTER_CUE = frozenset("再能会要可以是必需用得该应曾太很想肯敢愿准许够")
_PARTICLES = "了的吧啦呢呀啊"  # 句尾的语气字
# 整条回答就是这些的是肯定的套话，不是否定（句子里的“没关系、没问题”照旧是否定：“这事跟他没关系”）
_AFFIRMATIVE_REPLY = re.compile(r"没问题|没错|没事|没关系|不客气|别客气|没毛病|不要紧|没说的|不碍事|没啥")
# 否定的短回答后面跟着的客气话：“不用了，谢谢”“不用啦，我记得”
_POLITE = re.compile(r"(?:谢谢|多谢|感谢|好的|好|行|知道了|我记着呢|我记得|记着呢|没事|辛苦了?)(?:你|您|啦|了|哈|呢|啊)*")
_MAX_SKIP = 3
# 回答里只有一个否定的短句：“不用。”“先不买。”“算了”
_BARE_NEGATIVE = re.compile(
    r"(?:先|暂时|暂|还是|那就|就|那)?(?:不用|不要|不必|不需要|不了|不行|没有|算了|不|没|别|甭)"
    + _CJK.pattern
    + r"{0,2}(?:了|吧|啦|的)?"
)


def _negations(text: str, *, comparatives: bool) -> str:
    """去掉不是否定的写法以后的文字（位置不变）。comparatives：比较的说法（不低于）也去掉。"""
    masked = _NOT_NEGATION.sub(lambda match: " " * len(match.group(0)), text)
    if comparatives:
        masked = _COMPARATIVES.sub(lambda match: " " * len(match.group(0)), masked)
    return _A_NOT_A.sub(lambda match: " " * len(match.group(0)), masked)


def _has_negation(text: str, *, comparatives: bool = False) -> bool:
    return bool(_NEGATION.search(_negations(soft(text), comparatives=comparatives)))


def _has_condition(text: str) -> bool:
    return bool(_CONDITION.search(soft(text)))


def _has_hedge(text: str) -> bool:
    cleaned = _NOT_HEDGE.sub(lambda match: " " * len(match.group(0)), soft(text))
    return bool(_HEDGE.search(cleaned))


def _grams(text: str) -> set[str]:
    """比较两段话说的是不是一件事：汉字两个一组（一个字的也算一组），英文和数字一个词一个。"""
    cleaned = soft(text)
    grams = {run[index : index + 2] for run in _CJK_RUN.findall(cleaned) for index in range(max(1, len(run) - 1))}
    return grams | {word for word in _WORD.findall(cleaned) if word not in _STOPWORDS}


def _overlap(part: str, fact_text: str) -> float:
    """part 里有多少在事实文字里。"""
    grams = _grams(part)
    return len(grams & _grams(fact_text)) / len(grams) if grams else 0.0


_CLAUSE_OVERLAP = 0.5  # 原文的一小句有这么多在事实里，才算事实说的就是这一小句
_MIN_CLAUSE_GRAMS = 2


_CLAUSE_REACH = 40  # 往两边最多带多少字：表格压成一行时没有标点，不能把整行都算进来


def _clause_around(text: str, start: int, end: int, *, before: int = 0) -> str:
    """text[start:end] 所在的那一小句（带着句末的标点），before 给了再往前带几小句。两边最多各带 _CLAUSE_REACH 个字。"""
    left = start
    seen = 0
    while left > 0 and start - left < _CLAUSE_REACH * (before + 1):
        if _CLAUSE_MARK.match(text[left - 1]):
            if seen >= before:
                break
            seen += 1
        left -= 1
    right = end
    while right < len(text) and right - end < _CLAUSE_REACH and not _CLAUSE_MARK.match(text[right]):
        right += 1
    while right < len(text) and _CLAUSE_MARK.match(text[right]):
        right += 1
    return text[left:right]


_HEDGE_REACH = 6  # 语气词最远在引文前面几个字


def _hedge_spans(proofs: Sequence[Proof]) -> list[str]:
    """引文连同它前面紧挨着的几个字（同一小句里），看语气用。"""
    spans = []
    for proof in proofs:
        start = proof.start
        reach = 0
        while start > 0 and reach < _HEDGE_REACH and not _CLAUSE_MARK.match(proof.shown[start - 1]):
            start -= 1
            reach += 1
        spans.append(proof.shown[start : proof.end])
    return spans


_AFTER_REACH = 40  # 引文后面紧跟着写在后面的条件时，往后最多带几个字
_TRAILING_CONDITION = re.compile(
    r"\s*[,，;；]?\s*(?:除非|前提是|条件是|(?a:(?i:(?:if|unless|only if|in case|provided that|as long as)\b)))"
)


def _contexts(proofs: Sequence[Proof], *, before: int = 0, conditions: bool = False) -> list[str]:
    """每条证据所在的小句。conditions：看条件、语气用，往前多带 before 小句；往后只带到引文为止，
    除非引文后面紧跟着写在后面的条件（“，除非……”“, if …”），那就带上那一小句（引文后面的“若……”多半另起一件事，
    管不到引文说的事）。"""
    texts = []
    for proof in proofs:
        around = _clause_around(proof.shown, proof.start, proof.end, before=before)
        if conditions:
            left = max(proof.shown.find(around), 0)
            cut = proof.end
            trailing = _TRAILING_CONDITION.match(proof.shown, proof.end)
            if trailing is not None:
                mark = _CLAUSE_MARK.search(proof.shown, trailing.end())
                cut = min(mark.start() if mark is not None else len(proof.shown), trailing.end() + _AFTER_REACH)
            around = proof.shown[left:cut]
        texts.append(around)
    return texts


def _clauses_about(texts: Iterable[str], fact_text: str) -> list[str]:
    """几段话里和这条事实说的是同一件事的那几小句（表格一格算一小句，条件和结果各算一段）。"""
    found = []
    for text in texts:
        for clause, _ in _parts(text):
            if len(_grams(clause)) >= _MIN_CLAUSE_GRAMS and _overlap(clause, fact_text) >= _CLAUSE_OVERLAP:
                found.append(clause)
    return found


def _predicates(rest: str) -> list[str]:
    """否定词后面的那个词：汉字从三个字试到两个字，英文是后面一个词。先跳过“再、能、会、要、可以、是”这些字
    （“别再提醒”的“提醒”，J2.14）；不跳的也试。后面只剩一两个字的（“没开”“不读”“不去了”），一个字也算，
    “能、会”这类虚字不算；别的不按一个字找（“不是王晓明”的“王”、“不开车”的“开”不算）。"""
    rest = rest.lstrip()
    run = _CJK_RUN.match(rest)
    if run is not None:
        word = run.group(0)
        skipped = word
        while skipped and skipped[0] in _SKIP_AFTER_CUE and len(word) - len(skipped) < _MAX_SKIP:
            skipped = skipped[1:]
        found: dict[str, None] = {}
        for candidate in dict.fromkeys(item for item in (skipped, word) if item):
            for size in (3, 2, 1):
                if size == 1 and (len(candidate.rstrip(_PARTICLES)) != 1 or candidate[0] in _FUNCTION_CHARS):
                    continue  # “没开”“不去了”的一个字算，“开车”“上班”不拆
                if len(candidate) >= size:
                    found.setdefault(candidate[:size], None)
        return list(found)
    english = _WORD.match(rest)
    return [english.group(0)] if english is not None and not english.group(0).isdigit() else []


def _content_units(text: str) -> set[str]:
    """一个字一个字比时的内容：汉字去掉“的、了、是、我”这些虚字，英文和数字一个词一个。"""
    cleaned = soft(text)
    chars = {char for run in _CJK_RUN.findall(cleaned) for char in run if char not in _FUNCTION_CHARS}
    return chars | {word for word in _WORD.findall(cleaned) if word not in _STOPWORDS}


def _char_overlap(part: str, fact_text: str) -> float:
    units = _content_units(part)
    return len(units & _content_units(fact_text)) / len(units) if units else 0.0


def _about(part: str, fact_text: str) -> bool:
    """原文这一段说的是不是事实里的事：汉字两个一组一半以上在事实里；或者一个字一个字比，一大半在事实里
    （“明天的会我不去了”和“主人明天去开会”，J2.14）。"""
    if _overlap(part, fact_text) >= _CLAUSE_OVERLAP:
        return True
    units = _content_units(part)
    return len(units) >= _MIN_CLAUSE_GRAMS and _char_overlap(part, fact_text) > _CLAUSE_OVERLAP


def _likeness(part: str, piece: str) -> float:
    return max(_overlap(part, piece), _char_overlap(part, piece))


def _negated_clause(fact: str, position: int) -> bool:
    """事实里 position 所在的那一小句有没有否定：有就算原文的否定留着（“不得代签或代为付款”“只显示尚未结束的”）。"""
    left = position
    while left > 0 and not _CLAUSE_MARK.match(fact[left - 1]):
        left -= 1
    right = position
    while right < len(fact) and not _CLAUSE_MARK.match(fact[right]):
        right += 1
    clause = fact[left:right]
    return bool(_NEGATION.search(_negations(clause, comparatives=False))) or bool(_NEGATIVE_VERBS.search(clause))


_PUNCT_OR_SPACE = re.compile(r"[\W_]+")
_COORDINATOR = re.compile(r"也(?!许)|又|而且|并且|但是|但|(?a:(?i:\b(?:and|nor|but)\b))")


def _fact_clauses(fact: str) -> list[tuple[int, str]]:
    """事实（soft 写法）切成小句：(起点, 小句)。"""
    found = []
    offset = 0
    for clause in _CLAUSE.split(fact):
        start = fact.find(clause, offset)
        offset = start + len(clause)
        if clause.strip():
            found.append((start, clause))
    return found


NEGATION_DROPPED = "negation_dropped"
NEGATION_ADDED = "negation_added"


def _closest(content: str, fact: str) -> list[tuple[int, str]]:
    """事实里和原文这一段最像的那几小句：(起点, 小句)。"""
    scores = [(_likeness(content, piece), start, piece) for start, piece in _fact_clauses(fact)]
    best = max((score for score, _, _ in scores), default=0.0)
    return [(start, piece) for score, start, piece in scores if score == best and best > 0]


def _mentions(word: str, text: str) -> list[str]:
    english = word.isascii()
    pattern = rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])" if english else re.escape(word)
    return re.findall(pattern, text)


def _flipped(clause: str, claim: str, quote: str = "") -> str | None:
    """原文这一小句里被否定的那个词，在事实里和它说同一件事的那一小句里出现了、那一小句却没有否定：说反了
    （返回 negation_dropped）。原文是双重否定（“不是不能接受”，是肯定），事实那一小句却带着否定：返回 negation_added。

    要事实说的就是这一小句（去掉否定词以后，一半以上在事实里）；只是没写进来不算。很短的一小句（“不去”）
    看引文去掉否定词以后是不是整段在事实里（“老周说：不去”写成“老周说去”）。原文这一小句里那个词本来就不止一处
    （“首月不可退、之后每月可退”）不查；事实里和它一样像的几小句有一句带着否定，算留着。几个字的否定词（无需、
    还没、没有一个）整个算一处，被否定的词从它后面找，先跳过“再、能、是”这些字（J2.14）。
    """
    text = soft(clause)
    fact = soft(claim)
    cleaned = _negations(text, comparatives=False)
    double = _DOUBLE_NEGATION.search(cleaned)
    if double is not None:  # 双重否定：当肯定看，事实里那一小句带着否定的是说反了（J2.14）
        content = text[: double.start()] + " " + text[double.end() :]
        if _about(content, fact):
            closest = _closest(content, fact)
        elif len(_content_units(content)) <= 2 and text in soft(quote):
            closest = _fact_clauses(fact)  # “他不会不来的”这种很短的：看整条事实
        else:
            return None
        for word in _predicates(text[double.end() :]):
            for start, piece in closest:
                if not _mentions(word, piece) or not _negated_clause(fact, start):
                    continue
                if _DOUBLE_NEGATION.search(_negations(piece, comparatives=False)):
                    continue  # 事实那一小句也是双重否定（照抄的）：算留着
                return NEGATION_ADDED
        return None
    cues = list(_NEGATION_CUE.finditer(cleaned))
    if len(cues) > 1:  # “不发货也不退款”：几件事各有各的否定，切开一件一件查；切不开的不查
        pieces = [piece for piece in _COORDINATOR.split(text) if piece.strip()]
        if len(pieces) > 1 and all(
            len(_NEGATION_CUE.findall(_negations(piece, comparatives=False))) <= 1 for piece in pieces
        ):
            return next((reason for piece in pieces if (reason := _flipped(piece, claim, quote))), None)
        return None
    if len(cues) != 1:
        return None
    match = cues[0]
    content = text[: match.start()] + " " + text[match.end() :]
    if len(_grams(content)) <= 1:
        if not text or text not in soft(quote):
            return None
        stripped = _PUNCT_OR_SPACE.sub("", soft(quote).replace(match.group(0), "", 1))
        compact = _PUNCT_OR_SPACE.sub("", fact)
        position = compact.find(stripped) if stripped else -1
        while position >= 0:
            if not _NEGATION.search(_negations(compact[max(0, position - 3) : position], comparatives=False)):
                return NEGATION_DROPPED
            position = compact.find(stripped, position + 1)
        return None
    if not _about(content, fact):
        return None
    closest = _closest(content, fact)
    if any(_negated_clause(fact, start) for start, _ in closest):
        return None  # 和原文一样像的几小句里有一句带着否定：算留着
    for word in _predicates(text[match.end() :]):
        if len(_mentions(word, text)) > 1:
            continue  # 原文这一小句里这个词本来就不止一处（首月不可退、之后每月可退）
        if any(_mentions(word, piece) for _, piece in closest):
            return NEGATION_DROPPED
    return None


def _compact(clause: str) -> str:
    return _PUNCT_OR_SPACE.sub("", soft(clause))


def _affirmative_reply(clause: str) -> bool:
    return _AFFIRMATIVE_REPLY.fullmatch(_compact(clause).rstrip(_PARTICLES)) is not None


def _without_affirmative_replies(text: str) -> str:
    """整小句就是“没问题”“没事”这种肯定的套话的，挡掉（看有没有否定用）。"""
    return "，".join(clause for clause in _CLAUSE.split(text) if not _affirmative_reply(clause))


def _bare_negative_answer(own: Sequence[str], claim: str) -> bool:
    """证据里有一条问句、一条只有否定的短回答（“要不要明天提醒你？”“不用。”，J2.14）：回答就这一小句，后面最多跟着
    客气话（“不用了，谢谢”）；“不用，我坐地铁去”后面还有话的不算。问句说的就是事实里的事。
    “没问题”“没事”这些肯定的套话不算否定。"""
    questions = [text for text in own if _is_question(text)]
    if not questions:
        return False
    if not any(_about(clause, claim) for text in questions for clause in _CLAUSE.split(text) if clause.strip()):
        return False
    for text in own:
        clauses = [clause for clause in _CLAUSE.split(text) if clause.strip()]
        if not clauses or _is_question(clauses[0]) or _affirmative_reply(clauses[0]):
            continue
        if not all(_POLITE.fullmatch(_compact(rest)) for rest in clauses[1:]):
            continue
        if _BARE_NEGATIVE.fullmatch(_PUNCT_OR_SPACE.sub("", _negations(soft(clauses[0]), comparatives=False))):
            return True
    return False


_SHORT_FOLLOWER = 4  # 紧跟在后面、这么短的小句，算接着说同一件事（“样书还没寄到我手里，先不校对”）


def _change_context(own: Sequence[str], claim: str) -> str:
    """（G5 软性变化）证据里说的是这件事的那几小句：事实里除了出处、暂时和说定了的字眼以外的内容，一半以上在这一小句里；
    加上紧挨在它前后、很短、没写主语的小句。一句都对不上的看全部。“周三的会取消了，改成周五线上开”写成“会改到周五线上开”，
    看的只是后一小句。"""
    core = _content_units(_FRAMING.sub(" ", _SOFT_CHANGE.sub(" ", _HARD_CHANGE.sub(" ", soft(claim)))))
    clauses = [clause for text in own for clause in _CLAUSE.split(soft(text)) if clause.strip()]
    if not core:
        return " ".join(clauses)
    sizes = [len(_content_units(clause)) for clause in clauses]
    about = [len(core & _content_units(clause)) / len(core) >= _CLAUSE_OVERLAP for clause in clauses]
    keep = list(about)
    for index in range(len(clauses)):  # 紧跟在后面的短句（可以连着几句），和紧挨在前面的短句（“先放放吧，学吉他那件事”）
        if not keep[index] and sizes[index] <= _SHORT_FOLLOWER and index > 0 and keep[index - 1]:
            keep[index] = True
    for index in range(len(clauses) - 1):
        if not keep[index] and sizes[index] <= _SHORT_FOLLOWER and about[index + 1]:
            keep[index] = True
    chosen = [clause for clause, kept in zip(clauses, keep, strict=True) if kept]
    return " ".join(chosen or clauses)


def _negative_claim(claim: str) -> bool:
    """事实里有否定，或者别的说法写的否定、暂时不做的说法。"""
    folded = soft(claim)
    return _has_negation(claim, comparatives=True) or bool(_NEGATIVE_VERBS.search(folded) or _SOFT_CHANGE.search(folded))


def _strip_hedges(text: str) -> str:
    return _HEDGE.sub(" ", soft(text))


_SHORT_CLAUSE_UNITS = 6  # 这么短的小句才一个字一个字比：长的一个字一个字比，总能凑上一半
_PARENTHESES = re.compile(r"[()（）]")
def _days_of(text: str, said: datetime) -> set[date]:
    """一段话里写的日子（写明的、月日、几号、相对的说法），按说话那天换算。"""
    clean = _nfkc(text)
    times = Times()
    for _, one, _ in _date_matches(clean, said.date()):
        times.update(one)
    _relative_dates(clean, said.date(), times)
    return times.days


def _times_differ(one: str, other: str, said: datetime) -> bool:
    """判“说的是不是一件事”时，两边都写了日子、却一天都对不上的，不是一件事（“周五可能到货”和“周六一定发货”）；
    按说话那天换算以后比，“可能下周三发货”和“11/18 发货”是同一天。"""
    mine, theirs = _days_of(one, said), _days_of(other, said)
    return bool(mine) and bool(theirs) and not mine & theirs


_TIME_SPANS = tuple(pattern for _, pattern in _DATE_PATTERNS) + (_RELATIVE_DAY, _WEEKDAY, _WEEK, _MONTH_REL, _YEAR_REL)


def _strip_times(text: str) -> str:
    """去掉写日子的那几段（写明的、月日、相对的说法），比别的内容用。"""
    clean = _nfkc(text)
    for pattern in _TIME_SPANS:
        clean = pattern.sub(lambda match: " " * len(match.group(0)), clean)
    return clean


def _same_days(one: str, other: str, said: datetime) -> bool:
    return bool(_days_of(one, said) & _days_of(other, said))


def _alike(part: str, fact_text: str) -> bool:
    """（G5）汉字两个一组一半以上在事实里；很短的再一个字一个字比。"""
    if _overlap(part, fact_text) >= _CLAUSE_OVERLAP:
        return True
    return len(_content_units(part)) <= _SHORT_CLAUSE_UNITS and _char_overlap(part, fact_text) >= _CLAUSE_OVERLAP


def _hedged_about(texts: Iterable[tuple[str, datetime]], fact_text: str) -> list[str]:
    """（G5）几段话里和这条事实说的是同一件事的那几小句（括号里的另算一小句：“西门（可能更近）”的“可能”
    说的是西门）。比之前先去掉语气词（“这班车大概率要晚点”和“这班车会晚点”）；很短的小句汉字两个一组对不上的，
    再一个字一个字比（“没准明天就修好了”和“电梯明天修好”，J2.14）。"""
    found = []
    for text, said in texts:
        for part, _ in _parts(text):
            for clause in _PARENTHESES.split(part):
                plain = _strip_hedges(clause)
                if len(_grams(plain)) < _MIN_CLAUSE_GRAMS or _times_differ(plain, fact_text, said):
                    continue
                if _alike(plain, fact_text):
                    found.append(clause)
                elif _same_days(plain, fact_text, said):  # 日子对上了（“明天”写成了“11/11”）：去掉日子再比别的
                    rest = _strip_times(plain)
                    if not _content_units(rest) or _alike(rest, _strip_times(fact_text)):
                        found.append(clause)
    return found


def _negated_bits(text: str) -> list[str]:
    """事实里每一处否定连同后面一个字（英文是后面一个词），比如“不写”“not pay”。拿去窗口里找。"""
    cleaned = soft(text)
    masked = _negations(cleaned, comparatives=True)
    bits = []
    for match in _NEGATION.finditer(masked):
        rest = cleaned[match.end() :]
        follow = _WORD.match(rest.lstrip()) if match.group(0).isascii() else None
        if follow is not None:
            bits.append(cleaned[match.start() : match.end()] + " " + follow.group(0))
        else:
            bits.append(cleaned[match.start() : match.end() + 1])
    return bits


COND = "cond"  # 条件那一段
RESULT = "result"  # 条件管着的那一段结果
PLAIN = "plain"


def _parts(text: str) -> list[tuple[str, str]]:
    """切成几段：(文字, cond / result / plain)。按逗号这类切；条件词前面有字的（“We ship next week if …”），
    前面那段是结果；条件那一段里有“就、则、那是、then”的，按它切开，后面是结果；都没有，条件后面紧跟的一段是结果。

    条件词在整句上找：“只要发票齐全，就能报销”的“只要”要看到逗号后面的“就”。
    """
    whole = soft(text)
    cues = list(_CONDITION.finditer(whole))
    parts: list[list[str]] = []
    offset = 0
    for clause in _CLAUSE.split(whole):
        start = whole.find(clause, offset)
        offset = start + len(clause)
        if not clause.strip():
            continue
        inside = [cue for cue in cues if start <= cue.start() < start + len(clause)]
        if not inside:
            parts.append([clause.strip(), PLAIN])
            continue
        cue_start, cue_end = inside[0].start() - start, inside[0].end() - start
        head, tail = clause[:cue_start].strip(), clause[cue_start:]
        then = _THEN.search(tail, cue_end - cue_start)
        if head:
            parts.append([head, RESULT])
        if then is not None:
            parts.append([tail[: then.start()].strip(), COND])
            if tail[then.start() :].strip():
                parts.append([tail[then.start() :].strip(), RESULT])
        elif not head and _TRAILING_CUE.match(tail.lstrip()) and parts and parts[-1][1] == PLAIN:
            parts[-1][1] = RESULT  # 写在后面的条件，管它前面紧挨着的那一段
            parts.append([tail.strip(), COND])
        else:
            parts.append([tail.strip(), COND])
            if not head:
                parts.append(["", "pending"])  # 结果在下一段
    resolved: list[tuple[str, str]] = []
    waiting = False
    for text_part, kind in parts:
        if kind == "pending":
            waiting = True
            continue
        if waiting and kind == PLAIN:
            resolved.append((text_part, RESULT))
            waiting = False
            continue
        waiting = False
        resolved.append((text_part, kind))
    return resolved


_CONDITION_OVERLAP = 0.3  # 条件那一段有这么多在事实里，就算条件留着（改成了“来自总部的通知”这种写法）


def _drops_condition(text: str, claim: str) -> bool:
    """原文有一段是条件，事实说的是它管着的那段结果，条件那一段（去掉“如果”）的内容几乎不在事实里。

    “如果尺寸合适那就买两张，记得问一下运费”：条件管的是“那就买两张”，事实说“记得问一下运费”不算丢了条件。
    """
    parts = _parts(text)
    conditional = [part for part, kind in parts if kind == COND]
    results = [part for part, kind in parts if kind == RESULT]
    if not conditional or not results:
        return False
    if not any(len(_grams(part)) >= _MIN_CLAUSE_GRAMS and _overlap(part, claim) >= _CONDITION_OVERLAP for part in results):
        return False
    return all(_overlap(_CONDITION.sub(" ", part), claim) < _CONDITION_OVERLAP for part in conditional)


def _is_question(clause: str) -> bool:
    """以问号、“吗”结尾；“呢、嘛、么”结尾的要有疑问词。"""
    text = soft(clause).rstrip("。.!！ ")
    if text.endswith(("?", "吗")):
        return True
    return text.endswith(("呢", "嘛", "么")) and bool(_QUESTION_WORDS.search(text))


def check_polarity(
    fact: Mapping[str, object], proofs: Sequence[Proof], window: _WindowFacts
) -> tuple[list[Finding], list[str]]:
    """G3、G4、G5，看证据所在的那一小句。返回 (没过的, notes)。"""
    claim = str(fact.get("text") or "") + " " + str(fact.get("value") or "")
    own = _contexts(proofs)
    wide = _contexts(proofs, before=1, conditions=True)
    findings: list[Finding] = []
    notes: list[str] = []
    # G3：原文里被否定的那件事，事实里没了否定；或者原文那几小句里一处否定都没有，事实里却有
    flipped = next(
        (
            reason
            for proof, text in zip(proofs, own, strict=True)
            for clause in _CLAUSE.split(text)
            if clause.strip() and (reason := _flipped(clause, claim, proof.quote))
        ),
        None,
    )
    if flipped is not None:
        findings.append(Finding("G3", flipped))
    elif _bare_negative_answer(own, claim) and not _negative_claim(claim):
        findings.append(Finding("G3", NEGATION_DROPPED))  # “要不要提醒？”“不用。”写成了要提醒
    elif _has_negation(claim, comparatives=True) and not any(
        _has_negation(plain) or _NEGATIVE_WORDS.search(soft(plain)) for plain in map(_without_affirmative_replies, own)
    ):
        bits = _negated_bits(claim)
        if bits and all(bit in window.soft for bit in bits):
            notes.append("G3:negation_elsewhere")
        else:
            findings.append(Finding("G3", "negation_added"))
    # G4：带条件的原文，事实说的是条件以外的那几段，条件却没了
    if not _has_condition(claim) and not _CONDITION_WORDS.search(soft(claim)) and any(
        _drops_condition(text, claim) for text in wide
    ):
        findings.append(Finding("G4", "condition_dropped"))
    # G5：原文里是“可能、打算”，事实写成了说定了的。语气词要在引文里，或者紧挨在引文前面同一小句里
    hedged_claim = _has_hedge(claim) or _CLAIM_UNSURE.search(soft(claim)) is not None
    if fact.get("certainty") == "stated" and not hedged_claim and any(
        _has_hedge(clause)
        for clause in _hedged_about(zip(_hedge_spans(proofs), (proof.said_at for proof in proofs), strict=True), claim)
    ):
        findings.append(Finding("G5", "modality_upgrade"))
    # G5（J2.14）：软性变化。原文是暂时的（先放放、暂缓、先不、回头再说），事实写成说定了的（取消、放弃、作废），
    # 事实里又没有暂时的字眼；或者反过来，原文说定了取消，事实写成暂缓。事实自己带着“可能”的不管
    claim_text = soft(claim)
    context = _change_context(own, claim)
    if not hedged_claim:
        hard = [match.group(0) for match in _HARD_CHANGE.finditer(claim_text)]
        claim_soft = _SOFT_CHANGE.search(claim_text) is not None
        if hard and not claim_soft and _SOFT_CHANGE.search(context) and not any(word in context for word in hard):
            findings.append(Finding("G5", "soft_change_hardened"))
        elif claim_soft and not hard and _HARD_CHANGE.search(context) and not _SOFT_CHANGE.search(context):
            findings.append(Finding("G5", "soft_change_softened"))
    if (
        fact.get("certainty") == "stated"
        and proofs
        and all(_is_question(clause) and not _REQUEST.search(soft(clause)) for clause in own)
    ):
        findings.append(Finding("G5", "question_as_fact"))
    return findings, notes


# ---------- G6 时间 ----------


def check_time(
    fact: Mapping[str, object], proofs: Sequence[Proof], window: _WindowFacts
) -> tuple[list[Finding], list[str], str | None, str]:
    """G6。返回 (没过的, notes, occurred_at, time_precision)。

    occurred_at：证据那几句对得上的时间（写明了的、相对的；都没有就是说话那天）。
    事实文字里的日期、时刻、相对的说法：同上，说话那天、那一刻也总是算的（“9 月 29 日 Bot 报告说……”）。
    都对不上的再看窗口里别的行。
    """
    evidence = Times()
    clocks: set[tuple[int, int]] = set()
    seen: set[tuple[str, datetime]] = set()
    for proof in proofs:
        if (proof.shown, proof.said_at) not in seen:
            seen.add((proof.shown, proof.said_at))
            found = sentence_times(proof.shown, proof.said_at)
            evidence.update(found)
            clocks |= found.clock
    spoken = said_times(proof.said_at for proof in proofs)
    for_text = Times()
    for_text.update(evidence)
    for_text.update(spoken)
    findings: list[Finding] = []
    notes: list[str] = []
    claim = str(fact.get("text") or "") + " " + str(fact.get("value") or "")
    said_moments = sorted({proof.said_at for proof in proofs})
    unsupported = False
    for said in said_moments[:1]:
        for one in text_times(claim, said):
            if _fits(one, for_text):
                continue
            if _fits(one, window.spoken):
                notes.append("G6:time_elsewhere")
                continue
            unsupported = True
            break
    allowed_clocks = clocks | {(moment.hour, moment.minute) for moment in said_moments}
    for hour, minute in _clock_pairs(_nfkc(claim)):
        if (hour, minute) in allowed_clocks:
            continue
        if (hour, minute) in window.clocks:
            notes.append("G6:time_elsewhere")
            continue
        unsupported = True
        break
    if unsupported:
        findings.append(Finding("G6", "time_unsupported"))
    raw_value = fact.get("occurred_at")
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return findings, notes, None, "unknown"
    parsed = parse_occurred(raw_value)
    value = parsed.local(proofs[0].said_at.utcoffset()) if parsed is not None else None
    if value is None:
        notes.append("G6:time_dropped")
        return findings, notes, None, "unknown"
    if fact.get("time_precision") != value.precision:
        notes.append("G6:precision_fixed")
    precision: str | None = value.precision
    while precision is not None:
        lowered = precision != value.precision
        if _matches(value, precision, evidence):
            if lowered:
                notes.append("G6:precision_lowered")
            return findings, notes, value.at(precision), precision
        if _matches(value, precision, window.times):
            notes.append("G6:time_elsewhere")
            if lowered:
                notes.append("G6:precision_lowered")
            return findings, notes, value.at(precision), precision
        precision = _COARSER[precision]
    notes.append("G6:time_dropped")
    return findings, notes, None, "unknown"


# ---------- G7 改写 ----------

_FUNCTION_CHARS = frozenset(
    "的了是在和与及并且或而为于以将把被对从到等也都就还又很更最这那其该此之个些位名条项次件已会要能可"
    "他她它们我你您吗呢吧啊呀哦着过给让向由所"
)
_FRAMING = re.compile(
    r"主人|用户|对方|本人|自己|表示|称|说|提到|提及|转述|告知|告诉|确认|要求|认为|指出|回复|回答|通知|提醒|建议|"
    r"报告|汇报|记录|写道|发来|发了|邮件|消息|原话|据|根据|按照|按|约定|规定|决定|同意|安排|需要|希望|计划|打算|准备|"
    r"(?a:\b(?:bot|says?|said|stated?|reports?|reported|according|mentions?|mentioned|told|tells)\b)"
)
# 事实文字的内容至少这么多在窗口里看得到。只拿证据那几句比的话，按提示词带上出处、把“他”换成名字、
# 把英文写成中文的好事实都过不了：36 个测试包里对证据覆盖最低的几十条看下来都是对的。所以 G7 只兜底，
# 挡住窗口里根本没有的内容；意思改没改由 G2 到 G6 管
PARAPHRASE_MIN_COVERAGE = 0.5
_PARAPHRASE_MIN_UNITS = 4  # 内容太少的不判


def _units(claim: str) -> list[str]:
    """内容单位：汉字一个一个（虚词不算），英文一个词一个（常用的虚词、数字不算）。"""
    cleaned = _FRAMING.sub(" ", soft(claim))
    units = [unit for unit in _CJK.findall(cleaned) if unit not in _FUNCTION_CHARS]
    return units + [word for word in _WORD.findall(cleaned) if not word.isdigit() and word not in _STOPWORDS]


def coverage(fact_text: str, sources: Sequence[str] | _WindowFacts) -> tuple[float, int]:
    """事实文字的内容有多少在 sources 里出现。返回 (比例, 内容单位数)。数字和日期归 G2、G6 管，这里不算。"""
    if isinstance(sources, _WindowFacts):
        spaced = sources.soft
    else:
        spaced = " " + " ".join(soft(text) for text in sources) + " "
    units = _units(fact_text)
    if not units:
        return 1.0, 0
    hits = 0
    for unit in units:
        if unit.isascii():
            hits += bool(re.search(rf"(?<![a-z0-9]){re.escape(unit)}(?![a-z0-9])", spaced))
        else:
            hits += unit in spaced
    return hits / len(units), len(units)


def check_paraphrase(fact: Mapping[str, object], window: _WindowFacts) -> list[Finding]:
    """G7：事实文字大部分要在这个窗口里看得到（每一行、实体的名字、说话人、来源）。"""
    ratio, units = coverage(str(fact.get("text") or ""), window)
    if units >= _PARAPHRASE_MIN_UNITS and ratio < PARAPHRASE_MIN_COVERAGE:
        return [Finding("G7", "paraphrase_unsupported")]
    return []


# ---------- 一个窗口 ----------


def _entity_names(fact: Mapping[str, object], entities: Mapping[str, Mapping[str, object]], window: _WindowFacts) -> list[str]:
    """这条事实涉及的实体的写法，要在窗口里写着才算原文：名字是模型写的。"""
    keys = [str(role.get("key")) for role in fact.get("entities") or [] if isinstance(role, Mapping)]
    if fact.get("subject") is not None:
        keys.append(str(fact["subject"]))
    names = []
    for key in keys:
        entity = entities.get(key)
        if entity is not None:
            for name in (entity.get("mention"), entity.get("name")):
                if isinstance(name, str) and name.strip() and fold(name) in window.folded:
                    names.append(name)
    return names


def _shape_problem(fact: Mapping[str, object]) -> str | None:
    """L3 的结果结构不对（手写的、旧库里的）：不往下核，按 L3 的问题隔离。"""
    evidence = fact.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(item, Mapping) for item in evidence):
        return "bad_shape"
    if not isinstance(fact.get("text"), (str, type(None))):
        return "bad_shape"
    if not isinstance(fact.get("entities"), (list, type(None))):
        return "bad_shape"
    return None


def check_fact(
    index: int,
    fact: Mapping[str, object],
    refs: dict[str, _Ref],
    entities: Mapping[str, Mapping[str, object]],
    window: _WindowFacts,
) -> FactCheck:
    result = FactCheck(index=index, raw=fact)
    result.status = VERIFIED if fact.get("certainty") == "stated" else INFERRED
    raw_problems = fact.get("problems") or []
    if isinstance(raw_problems, str):
        problems = [raw_problems]
    elif isinstance(raw_problems, (list, tuple)):
        problems = [str(problem) for problem in raw_problems if problem]
    else:
        problems = ["bad_shape"]
    if problems:  # L3 换编号时就标了问题的，直接隔离
        result.failures.extend(Finding("L3", problem) for problem in problems)
        return result
    shape = _shape_problem(fact)
    if shape is not None:
        result.failures.append(Finding("L3", shape))
        return result
    if not str(fact.get("text") or "").strip():  # 结构上允许空字符串，可它什么都没说
        result.failures.append(Finding("G7", "empty_text"))
        return result
    seen: set[str] = set()
    notes: list[str] = []
    for proof in fact.get("evidence") or []:
        assert isinstance(proof, Mapping)
        found, failure = check_evidence(refs, proof)
        if failure is not None:
            result.dropped.append((proof, failure))
            continue
        for item in found:
            if item.sentence_id in seen:
                continue
            seen.add(item.sentence_id)
            result.proofs.append(item)
            if item.note:
                notes.append(f"G1:{item.note}")
    if not result.proofs:
        failures = [failure for _, failure in result.dropped] or [Finding("G1", "quote_not_found")]
        result.failures.append(next((failure for failure in failures if failure.rule == "G1"), failures[0]))
        return result
    if all(proof.kind == QUOTED_ONLY for proof in result.proofs):  # 证据全是只在引用里看到过的话（D101）：一律推断
        result.status = INFERRED
    names = _entity_names(fact, entities, window)
    for check in (
        lambda: check_numbers(fact, result.proofs, names, window),
        lambda: check_polarity(fact, result.proofs, window),
    ):
        found_failures, found_notes = check()
        result.failures.extend(found_failures)
        notes.extend(found_notes)
    time_failures, time_notes, occurred_at, precision = check_time(fact, result.proofs, window)
    result.failures.extend(time_failures)
    notes.extend(time_notes)
    result.occurred_at, result.time_precision = occurred_at, precision
    result.failures.extend(check_paraphrase(fact, window))
    result.failures.sort(key=lambda finding: RULE_ORDER.index(finding.rule))
    result.notes = sorted(set(notes))
    return result


def verify_window(
    window: Window,
    rendered: RenderedWindow,
    extraction: Mapping[str, object],
    *,
    lexicon: Lexicon = EMPTY_LEXICON,
    owner_names: Collection[str] = (),
) -> WindowCheck:
    """L4。extraction 是 L3 换好编号的结果（ResultStore.extraction）。事实按 G1 到 G7 核；关系按 R1 到 R6 核
    （javmem.pipeline.relcheck，J4.3），lexicon、owner_names 是别名表和主人的名字的比对写法，只有关系用。"""
    from javmem.pipeline.relcheck import check_relations  # relcheck 借这里的比对、日子换算，放在这里引免得互相引

    refs = _refs(window, rendered)
    entities: dict[str, Mapping[str, object]] = {}
    kept: list[Mapping[str, object]] = []
    bad: list[tuple[Mapping[str, object], Finding]] = []
    raw_entities = extraction.get("entities")
    for entity in raw_entities if isinstance(raw_entities, list) else []:
        if not isinstance(entity, Mapping) or not isinstance(entity.get("key"), str):
            bad.append((entity if isinstance(entity, Mapping) else {"entity": str(entity)[:0]}, Finding("L3", "bad_shape")))
            continue
        problems = entity.get("problems") or []
        if problems:
            first = problems if isinstance(problems, str) else str(problems[0]) if isinstance(problems, (list, tuple)) else "bad_shape"
            bad.append((entity, Finding("L3", first)))
            continue
        entities[str(entity["key"])] = entity
        kept.append(entity)
    window_folded = fold(" ".join(rendered.shown.values()))
    extras = [
        str(entity.get(name) or "")
        for entity in kept
        for name in ("mention", "name")
        if isinstance(entity.get(name), str) and entity.get(name) and fold(str(entity[name])) in window_folded
    ]
    extras += sorted({block.speaker for block in window.blocks}) + [window.source]
    context = _window_facts(refs, [extra for extra in extras if extra])
    raw_facts = extraction.get("facts")
    facts = [
        check_fact(index, fact, refs, entities, context)
        for index, fact in enumerate(raw_facts if isinstance(raw_facts, list) else [])
        if isinstance(fact, Mapping)
    ]
    relations = check_relations(window, rendered, extraction, refs, entities, lexicon=lexicon, owner_names=owner_names)
    return WindowCheck(window.window_id, window.scope, facts, kept, bad, relations)


def summarize_checks(checks: Sequence[WindowCheck]) -> dict[str, object]:
    """这一批的数：每条规则拦下多少条（一条事实没过几条规则就各算一次）、按第一条没过的规则算多少条，
    去掉的证据、改过来的证据、时间降精度和去掉的。关系的另算一块（relations，relcheck.summarize_relations）。不抄原文。"""
    from javmem.pipeline.relcheck import summarize_relations

    facts = [fact for check in checks for fact in check.facts]
    by_rule: dict[str, int] = {}
    by_code: dict[str, int] = {}
    first: dict[str, int] = {}
    notes: dict[str, int] = {}
    dropped: dict[str, int] = {}
    for fact in facts:
        for rule in sorted({finding.rule for finding in fact.failures}, key=RULE_ORDER.index):
            by_rule[rule] = by_rule.get(rule, 0) + 1
        for code in sorted({finding.as_text() for finding in fact.failures}):
            by_code[code] = by_code.get(code, 0) + 1
        if fact.first_rule is not None:
            first[fact.first_rule] = first.get(fact.first_rule, 0) + 1
        if fact.passed:
            for note in fact.notes:
                notes[note] = notes.get(note, 0) + 1
            for _, finding in fact.dropped:
                dropped[finding.as_text()] = dropped.get(finding.as_text(), 0) + 1
    return {
        "l4_version": L4_VERSION,
        "windows": len(checks),
        "facts": len(facts),
        "passed": sum(1 for fact in facts if fact.passed),
        "quarantined": sum(1 for fact in facts if not fact.passed),
        "by_rule": {rule: by_rule[rule] for rule in RULE_ORDER if rule in by_rule},
        "by_first_rule": {rule: first[rule] for rule in RULE_ORDER if rule in first},
        "by_code": dict(sorted(by_code.items())),
        "evidence_dropped_from_passed_facts": dict(sorted(dropped.items())),
        "notes_on_passed_facts": dict(sorted(notes.items())),
        "status": {
            VERIFIED: sum(1 for fact in facts if fact.passed and fact.status == VERIFIED),
            INFERRED: sum(1 for fact in facts if fact.passed and fact.status == INFERRED),
        },
        "entities_quarantined": sum(len(check.bad_entities) for check in checks),
        "relations": summarize_relations([check.relations for check in checks]),
    }


# ---------- 一批 ----------

NO_RESULT = "no_result"  # 结果库里没有这个窗口现在的键：还没抽、抽失败了，或者窗口变了
NOT_SENT = "not_sent"  # private 的、打码没打干净的，L3 没发
ERROR = "error"  # 核这个窗口时出了想不到的错（代码的毛病），别的窗口照常；修好以后重跑
NO_RECHECK = "no_recheck"  # 加了 --recheck，过了规则的事实还没有模型复核的判定（recheck.apply_rechecks 标的）


@dataclass(frozen=True)
class Checked:
    """一个窗口：发给模型的文字、键、L3 的结果、L4 的结论。没结果的 check 是 None，skipped 说为什么。"""

    window: Window
    rendered: RenderedWindow
    cache_key: str
    extraction: Mapping[str, object] | None
    check: WindowCheck | None
    skipped: str | None = None
    error: str | None = None  # skipped 是 ERROR 时：异常的类型名，不带内容
    recheck_key: str | None = None  # 模型复核结果的键（加了 --recheck 才有）；NO_RECHECK 的事实那一段发不出去时是 None
    recheck_unsent: str | None = None  # NO_RECHECK、复核那一段发不出去的：unredacted、too_big（排队修，写账照它标 failed）


def verify_windows(
    windows: Sequence[Window],
    store: ResultStore,
    model: Model,
    patterns: Sequence[RedactionPattern],
    *,
    prompt: str = PROMPT_VERSION,
    lexicon: Lexicon = EMPTY_LEXICON,
    owner_names: Collection[str] = (),
) -> list[Checked]:
    """按键从结果库取每个窗口 L3 的结果（同一批包切出来的窗口和键都一样），跑 L4。不调模型。prompt 是抽的时候用的
    L3 提示词版本；预处理 B 的标注照结果库里存下的加上，键才对得上。lexicon、owner_names（别名表、主人的名字）
    给关系的 R2、R3、R6 用（javmem.pipeline.relcheck），写账要和 javmem write 用的一样。

    一个窗口出了想不到的错，记成 ERROR（只记异常的类型），别的窗口照常，不让整批停下，也不打出原文。
    """
    checked = []
    for window, rendered, key in prepare(windows, model, patterns, store=store, prompt=prompt):
        if window.scope.private or rendered.leftover:
            checked.append(Checked(window, rendered, key, None, None, NOT_SENT))
            continue
        extraction = store.extraction(window.window_id, key)
        if extraction is None:
            checked.append(Checked(window, rendered, key, None, None, NO_RESULT))
            continue
        try:
            check = verify_window(window, rendered, extraction, lexicon=lexicon, owner_names=owner_names)
        except Exception as error:  # noqa: BLE001 - 一个窗口的毛病不让整批停下
            checked.append(Checked(window, rendered, key, extraction, None, ERROR, type(error).__name__))
            continue
        checked.append(Checked(window, rendered, key, extraction, check))
    return checked
