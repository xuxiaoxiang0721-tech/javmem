"""一条一条比（J3.8）：事实、关系、状态变化、别名。不调模型，靠出处的句子、对齐的实体和字面比。

**事实：** 金标准一条事实，找这一边证据和它落在同一句（或者同一段话：同一小节同一行）的事实，按下面的比：
- 主体：对齐以后是不是同一个；不是主体、但提到了（涉及的实体里有、文字里写了它的名字），算“提到了”。
- 内容：金标准的值（值太短的连属性一起，没有值的用整句）里的数、日子、英文词、中文的两字片段，这一边的
  文字、属性、值、时间里有多少（cover）。
- 完整：同一句、主体对得上或者说不准、值盖住六成以上、值里的数一个不少；同一段的、只是提到了主体的，要盖住七成半。
  主体不对：同一句、值盖住六成以上、数一个不少，可主体对到了金标准别的实体。
  部分：同一句的，值或者整句盖住三成以上；同一段的，值要盖住一半以上（同一段里别的事实常常字面挨得上）。
  别的算没抽到。阈值是对着真实数据看过例子定的，见 COMPLETE 这几个数。
一条事实可以盖住金标准好几条（这一边常把几件事写成一条），金标准一条只取对得最好的那一条。

**关系（D17、04 p7）：** 主体、关系、客体三个都对上才算对（亲属另比具体关系，D90，反过来写的算同一条）；
方向反了单独记，不算对；开始日子允许差几天（默认 7 天，D120；原来 3 天），金标准不知道开始日子的不比。金标准同一条关系跨单元合成一条（D103）。

**状态变化：** 这一边记下的“前后变了”（事实被替代、关系结束或换了一头、这一边自己写的变化），后一头的出处落在
金标准这条变化“后来”那几句上的，算判出来了；金标准写了“以前”那几句的，前一头也要落上。金标准说不算变化的
（is_change 是 false，“说明白了”），考的是别当成变了：这一边在那几句上记了变化、或者把关系当成那天新开始的，算错。

**别名：** 金标准一个叫法对到一个实体。这一边带着这个叫法（名字或别名）的实体，只有锚点一个才算对；锚点有、别的实体
也有的算“另有实体也叫这个”；落在别的实体上的看是没合并（也对到同一个金标准实体）还是对错了（对到别的）；这一边
没这个叫法的算没认出。金标准自己的实体表里这个叫法就对着别的实体的，不算分，单列。
“不是同一个”的考别合错：叫法落在那个实体的锚点上算错。“泛称（不建实体）”的考别建实体。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from javmem.pipeline.merge import name_key
from javmem.scoring.align import TABLE_ALIAS, Alignment
from javmem.scoring.common import Change, Fact, Ref, Relation, Side, When
from javmem.scoring.dates import TOLERANCE_DAYS, close, interval, precision_rank, single_day
from javmem.scoring.gold import (
    GoldItem,
    GoldRelation,
    GoldUnit,
    SentenceIndex,
    in_unit,
    is_not_labeled,
    kin_inverse,
    kin_term,
    kinship,
    sid_order,
    utterance,
)
from javmem.zh.search import search_key

# ---------- 字面比 ----------

_STOP = frozenset("的了是在和与及等也都就把被将对于从到为有这那个们之其而且或并已还会要")
_NUMBER = re.compile(r"⟦[^⟧]*⟧[年月日]?")
_WORD = re.compile(r"[a-z][a-z0-9]+")
_CJK = re.compile(r"[㐀-鿿]+")
WEIGHTS = {"numbers": 2.0, "words": 1.5, "grams": 1.0}

COMPLETE = 0.6  # 完整：值盖住多少
COMPLETE_LOOSE = 0.75  # 完整，但只是同一段、或者只是提到了主体
PARTIAL = 0.3  # 部分：同一句，值或者整句盖住多少
PARTIAL_LOOSE = 0.5  # 部分，但只是同一段：值要盖住多少（同一段里别的事实常常字面挨得上）


@dataclass(frozen=True)
class Units:
    numbers: frozenset[str] = frozenset()
    words: frozenset[str] = frozenset()
    grams: frozenset[str] = frozenset()

    @property
    def weight(self) -> float:
        return sum(len(getattr(self, kind)) * weight for kind, weight in WEIGHTS.items())

    def __or__(self, other: Units) -> Units:
        return Units(self.numbers | other.numbers, self.words | other.words, self.grams | other.grams)


def units(text: str | None) -> Units:
    """一段字里拿来比的几样：数和日子（统一写法，javmem.zh.search）、英文词、中文的两字片段（虚字不算）。"""
    if not text:
        return Units()
    folded = unicodedata.normalize("NFKC", text).lower()
    grams: set[str] = set()
    for run in _CJK.findall(folded):
        if len(run) == 1:
            if run not in _STOP:
                grams.add(run)
            continue
        for start in range(len(run) - 1):
            gram = run[start : start + 2]
            if gram[0] not in _STOP and gram[1] not in _STOP:
                grams.add(gram)
    return Units(frozenset(_NUMBER.findall(search_key(text))), frozenset(_WORD.findall(folded)), frozenset(grams))


def cover(ref: Units, candidate: Units) -> float | None:
    """ref 里的有多少在 candidate 里（按权重）。ref 是空的返回 None。"""
    total = ref.weight
    if not total:
        return None
    hit = sum(
        weight * len(getattr(ref, kind) & getattr(candidate, kind)) for kind, weight in WEIGHTS.items()
    )
    return round(hit / total, 4)


def _day(when: When) -> str:
    found = interval(when.start, when.precision)
    return found[0].isoformat() if found else ""


def side_units(fact: Fact) -> Units:
    return units(" ".join(part for part in (fact.text, fact.attribute, fact.value, _day(fact.when)) if part))


def gold_value_units(item: GoldItem) -> Units:
    value = units(item.value)
    if value.weight >= 2:
        return value
    both = units(" ".join(part for part in (item.attribute, item.value) if part))
    return both if both.weight >= 2 else units(item.content)


# ---------- 范围：哪几句算这个单元的 ----------


@dataclass
class Scope:
    unit: GoldUnit
    sentences: SentenceIndex | None = None

    def holds(self, sid: str) -> bool:
        return bool(in_unit(self.unit, sid, self.sentences))

    def labeled(self, sids: Iterable[str]) -> list[str]:
        """落在这个单元、又不是不标的句子。"""
        return [sid for sid in sids if self.holds(sid) and not is_not_labeled(self.unit, sid)]

    def touched(self, sids: Iterable[str]) -> bool:
        return any(self.holds(sid) for sid in sids)


def overlap(side_sids: Iterable[str], gold_sids: Iterable[str]) -> int:
    """2：落在同一句；1：同一段话；0：挨不上。"""
    side = set(side_sids)
    gold = set(gold_sids)
    if side & gold:
        return 2
    if {utterance(sid) for sid in side} & {utterance(sid) for sid in gold}:
        return 1
    return 0


# ---------- 实体 ----------


@dataclass
class EntityView:
    """金标准的实体在这一边怎么认：对齐、每个金标准实体的比对写法（看文字里提没提到它）。"""

    alignment: Alignment
    gold_keys: Mapping[str, set[str]]

    def gold_of(self, side_id: str | None) -> str | None:
        return self.alignment.gold_of(side_id)

    def mentioned(self, gold_name: str, fact: Fact) -> bool:
        if any(self.gold_of(one) == gold_name for one in fact.entities):
            return True
        text = name_key(fact.text)
        return any(len(key) >= 2 and key in text for key in self.gold_keys.get(gold_name, ()))

    def subject_level(self, gold_name: str | None, fact: Fact) -> int | None:
        """2：主体就是它；1：不是主体，但提到了；0：主体是别的实体；None：说不准（金标准没写主体、这一边的主体没对上）。"""
        if gold_name is None:
            return None
        side = self.gold_of(fact.subject)
        if side == gold_name:
            return 2
        if self.mentioned(gold_name, fact):
            return 1
        if side is None:
            return None
        return 0


# ---------- 事实 ----------


@dataclass(frozen=True)
class FactMatch:
    gold: str
    unit: str
    side: str | None
    grade: str  # 完整、部分、没抽到
    evidence: int = 0
    subject: int | None = None
    value_cover: float | None = None
    text_cover: float | None = None


def grade_pair(item: GoldItem, fact: Fact, view: EntityView, *, evidence: int) -> FactMatch:
    gold_name = item.subject[0] if item.subject else None
    level = view.subject_level(gold_name, fact)
    candidate = side_units(fact)
    value = cover(gold_value_units(item), candidate)
    whole = cover(units(item.content), candidate)
    numbers = units(item.value).numbers
    numbers_ok = not numbers or numbers <= candidate.numbers
    grade = "没抽到"
    if evidence:
        needed = COMPLETE if evidence == 2 and level != 1 else COMPLETE_LOOSE
        if level != 0 and value is not None and value >= needed and numbers_ok:
            grade = "完整"
        elif level == 0 and evidence == 2 and value is not None and value >= COMPLETE and numbers_ok:
            grade = "主体不对"
        elif evidence == 2 and ((value or 0) >= PARTIAL or (whole or 0) >= PARTIAL):
            grade = "部分"
        elif evidence == 1 and (value or 0) >= PARTIAL_LOOSE:
            grade = "部分"
    return FactMatch(item.id, item.unit, fact.id, grade, evidence, level, value, whole)


_GRADE_RANK = {"完整": 3, "主体不对": 2, "部分": 1, "没抽到": 0}
FOUND = ("完整", "主体不对", "部分")


def _better(a: FactMatch, b: FactMatch | None) -> bool:
    if b is None:
        return True
    return (_GRADE_RANK[a.grade], a.evidence, a.value_cover or 0, a.text_cover or 0) > (
        _GRADE_RANK[b.grade], b.evidence, b.value_cover or 0, b.text_cover or 0
    )


def by_sentence(facts: Iterable[Fact]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """句子 → 用它当证据的事实；那一段话 → 事实。"""
    sentence: dict[str, set[str]] = {}
    passage: dict[str, set[str]] = {}
    for fact in facts:
        for sid in fact.evidence:
            sentence.setdefault(sid, set()).add(fact.id)
            passage.setdefault(utterance(sid), set()).add(fact.id)
    return sentence, passage


def candidates(sids: Iterable[str], sentence: Mapping[str, set[str]], passage: Mapping[str, set[str]]) -> set[str]:
    found: set[str] = set()
    for sid in sids:
        found |= sentence.get(sid, set())
        found |= passage.get(utterance(sid), set())
    return found


def match_facts(items: Sequence[GoldItem], side: Side, view: EntityView,
                index: tuple[Mapping[str, set[str]], Mapping[str, set[str]]]) -> tuple[list[FactMatch], list[FactMatch]]:
    """(金标准每条事实取对得最好的那一条, 所有对得上的对子)。"""
    sentence, passage = index
    best: list[FactMatch] = []
    pairs: list[FactMatch] = []
    for item in items:
        chosen: FactMatch | None = None
        for fact_id in sorted(candidates(item.sids, sentence, passage)):
            fact = side.facts[fact_id]
            pair = grade_pair(item, fact, view, evidence=overlap(fact.evidence, item.sids))
            if pair.grade != "没抽到":
                pairs.append(pair)
            if _better(pair, chosen):
                chosen = pair
        if chosen is None or chosen.grade == "没抽到":
            chosen = FactMatch(item.id, item.unit, None, "没抽到")
        best.append(chosen)
    return best, pairs


FOUND_BACK = 0.4  # 这一边的事实算对得上金标准：它自己的字有多少在金标准那一条里（同一句）
FOUND_BACK_LOOSE = 0.6  # 同一段的
FOUND_NUMBERS = 2  # 同一句、数和日子对上这么多个的也算


def gold_units(item: GoldItem) -> Units:
    parts = [item.content, item.attribute, item.value]
    if item.change:
        parts += [str(item.change.get("before") or ""), str(item.change.get("after") or ""), str(item.change.get("target") or "")]
    return units(" ".join(part for part in parts if part))


def found_back(fact: Fact, items: Iterable[GoldItem]) -> str | None:
    """这一边的一条事实在金标准里有没有（倒过来看：它自己的字有多少在金标准某一条里，出处要挨得上）。
    算“对得上金标准”的比例用；比覆盖率松，因为这一边常把金标准的几条并成一条、或者只写了其中一件。"""
    mine = side_units(fact)
    for item in items:
        evidence = overlap(fact.evidence, item.sids)
        if not evidence:
            continue
        theirs = gold_units(item)
        back = cover(mine, theirs)
        if back is not None and back >= (FOUND_BACK if evidence == 2 else FOUND_BACK_LOOSE):
            return item.id
        if evidence == 2 and len(mine.numbers & theirs.numbers) >= FOUND_NUMBERS:
            return item.id
    return None


def relation_as_fact(item: GoldItem, side: Side, view: EntityView,
                     index: tuple[Mapping[str, set[str]], Mapping[str, set[str]]]) -> str | None:
    """金标准一条关系，这一边有没有抽成一条事实（两头都提到了、出处挨得上）。返回那条事实。"""
    if item.subject is None or item.object is None:
        return None
    sentence, passage = index
    for fact_id in sorted(candidates(item.sids, sentence, passage)):
        fact = side.facts[fact_id]
        if not overlap(fact.evidence, item.sids):
            continue
        if view.subject_level(item.subject[0], fact) in (1, 2) and view.subject_level(item.object[0], fact) in (1, 2):
            return fact_id
    return None


# ---------- 关系 ----------


@dataclass(frozen=True)
class RelationVerdict:
    side: str
    verdict: str  # 对、日子不对、日子没给、具体关系不对、方向反了、金标准没有、两头对不上实体
    gold: tuple[str, str, str] | None = None
    reason: str = ""


_UMBRELLA_KIN = frozenset({"子女", "配偶", "父母", "兄弟", "姐妹"})  # 统称


def kin_ok(gold_kin: str | None, side_kin: str | None, reversed_: bool) -> bool:
    """亲属的具体关系对不对（D90）。kinship 写的是主体是客体的什么人。
    同一个方向：写法一样，或者一边只写了“子女”“配偶”这种统称、类一样、亲子的哪一头也一样；
    反过来写的（主体客体对调）：要是金标准那个说法反过来的说法（妻子对丈夫、女儿对父亲或母亲、哥哥对弟弟或妹妹）。"""
    if not gold_kin:
        return True
    if not side_kin:
        return False
    if reversed_:
        return kin_term(side_kin) in kin_inverse(gold_kin)
    if kin_term(gold_kin) == kin_term(side_kin):
        return True
    gold_class, side_class = kinship(gold_kin), kinship(side_kin)
    if gold_class is None or side_class is None or gold_class != side_class:
        return False
    return bool({kin_term(gold_kin), kin_term(side_kin)} & _UMBRELLA_KIN)


def _kin_matches(found: GoldRelation, subject: str, side_kin: str | None) -> bool:
    """这一边的亲属（主体是 subject）和金标准合起来的那几条里的哪一条对得上。"""
    if not found.kins:
        return True
    return any(kin_ok(term, side_kin, reversed_=(gold_subject != subject)) for gold_subject, term in found.kins)


HELD = "金标准这条在改"  # 金标准这一条审的时候改了、还没写成结构：不算对也不算错


def judge_relations(
    relations: Iterable[Relation], gold: Sequence[GoldRelation], view: EntityView, *, tolerance_days: int = TOLERANCE_DAYS,
    held: Iterable[tuple[str, str, str]] = (),
) -> tuple[list[RelationVerdict], dict[tuple[str, str, str], list[RelationVerdict]]]:
    """held：审的时候改了、还没写成结构的金标准关系（主体、关系、客体），对上它的不算对也不算错。
    开始日子：金标准准到一天、这一边只写到月、季、年的，算“日子太粗”（严的不算对，不看日子的算对）。"""
    by_key = {relation.key: relation for relation in gold}
    held_keys = set(held)
    out: list[RelationVerdict] = []
    hits: dict[tuple[str, str, str], list[RelationVerdict]] = {}
    for relation in relations:
        subject, obj = view.gold_of(relation.subject), view.gold_of(relation.object)
        if subject is None or obj is None:
            out.append(RelationVerdict(relation.id, "两头对不上实体"))
            continue
        key = (subject, relation.relation, obj)
        reverse = (obj, relation.relation, subject)
        found = by_key.get(key)
        if found is None and reverse in by_key:
            if relation.relation == "亲属":
                found = by_key[reverse]
            else:
                out.append(RelationVerdict(relation.id, "方向反了", reverse))
                continue
        if found is None:
            if key in held_keys or (relation.relation == "亲属" and reverse in held_keys):
                out.append(RelationVerdict(relation.id, HELD, key))
            else:
                out.append(RelationVerdict(relation.id, "金标准没有"))
            continue
        if found.key[1] == "亲属" and not _kin_matches(found, subject, relation.fields.get("kinship")):
            verdict = RelationVerdict(relation.id, "具体关系不对", found.key)
        elif not found.starts:
            verdict = RelationVerdict(relation.id, "对", found.key, "金标准不知道开始日子")
        elif interval(relation.when.start, relation.when.precision) is None:
            verdict = RelationVerdict(relation.id, "日子没给", found.key)
        elif any(close(relation.when, start, tolerance_days) for start in found.starts):
            exact = single_day(relation.when) or not any(single_day(start) for start in found.starts)
            verdict = RelationVerdict(relation.id, "对" if exact else "日子太粗", found.key)
        else:
            verdict = RelationVerdict(relation.id, "日子不对", found.key)
        out.append(verdict)
        hits.setdefault(found.key, []).append(verdict)
    return out, hits


_ENDED = ("已结束", "被取代")


def _start_point(relation: Relation) -> date | None:
    span = interval(relation.when.start, relation.when.precision)
    return span[0] if span else None


def _end_point(relation: Relation) -> date | None:
    span = interval(relation.when.end, relation.when.precision) if relation.when.end else None
    return span[1] if span else None


def _segments(group: Sequence[Relation]) -> list[list[Relation]]:
    """一组两头、关系都一样的，按说的先后（出处最早的一句）分段：一条写了结束（结束日子或者已结束、被取代），它那一段就到它
    为止，后面的另起一段；可后面写的开始日子不晚于前面某一段的结束的，是又说了一遍那一段，归那一段。"""
    ordered = sorted(group, key=lambda one: (min((sid_order(sid) for sid in one.evidence), default=()), one.id))
    segments: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for relation in ordered:
        start = _start_point(relation)
        home = next((one for one in segments if one["closed"] and one["end"] is not None and start is not None
                     and start <= one["end"]), None) if start is not None else None  # type: ignore[operator]
        if home is None:
            if current is None:
                current = {"members": [], "closed": False, "end": None}
                segments.append(current)
            home = current
        home["members"].append(relation)  # type: ignore[attr-defined]
        if (relation.state in _ENDED or relation.when.end) and home is current:
            current["closed"], current["end"] = True, _end_point(relation)
            current = None
    return [one["members"] for one in segments]  # type: ignore[misc]


def _merge_segment(segment: Sequence[Relation]) -> Relation:
    """一段合成一条：开始日子取有日子的里面最早的，同一天的取最准的；写了结束的那条的结束；出处合在一起。"""
    first = segment[0]
    dated = [one for one in segment if _start_point(one) is not None]
    best = min(dated, key=lambda one: (_start_point(one), -precision_rank(one.when.precision))) if dated else None
    closing = next((one for one in reversed(segment) if one.state in _ENDED or one.when.end), None)
    precision = best.when.precision if best else (closing or first).when.precision
    state = closing.state if closing else (segment[-1].state or "生效")  # 没结束的一段：最后说的那条的（生效、暂停）
    evidence = tuple(sorted({sid for one in segment for sid in one.evidence}, key=sid_order))
    return Relation(f"合:{first.id}", first.subject, first.relation, first.object, dict(first.fields),
                    When(best.when.start if best else None, closing.when.end if closing else None, precision),
                    state, first.certainty, evidence)


def merge_relations(relations: Iterable[Relation], view: EntityView) -> tuple[dict[str, Relation], dict[str, tuple[str, ...]]]:
    """D115：这一边两头和关系都一样的，先合成一条再判。两头按对到的金标准实体算，对不上金标准的按这一边自己的实体算；
    亲属还要具体说法一样（照旧比具体说法）。中间写了结束的，前后算两段、不合（_segments）。
    返回 (合成的关系, 合成的那条 → 原来的几条)。只有一条的也照样“合成”，编号前面加“合:”。"""
    groups: dict[tuple[object, ...], list[Relation]] = {}
    for relation in relations:
        ends = tuple(("金标准", view.gold_of(one)) if view.gold_of(one) is not None else ("这一边", one)
                     for one in (relation.subject, relation.object))
        kin = kin_term(relation.fields.get("kinship")) if relation.relation == "亲属" else None
        groups.setdefault((ends[0], relation.relation, ends[1], kin), []).append(relation)
    merged: dict[str, Relation] = {}
    members: dict[str, tuple[str, ...]] = {}
    for group in groups.values():
        for segment in _segments(group):
            one = _merge_segment(segment)
            merged[one.id] = one
            members[one.id] = tuple(relation.id for relation in segment)
    return merged, members


# ---------- 状态变化 ----------


def first_after(new: Sequence[str], old: Sequence[str]) -> tuple[str, ...]:
    """变的那一句：后一头的证据里，前一头最后一句之后的第一句（同一包里按小节、行、句，跨包按包号）。没有在它之后的
    （晚到的旧消息），取后一头最早的一句。后一头又说过几遍的（合到了现行的那条上），那几遍不算变的那一句。"""
    if not new:
        return ()
    ordered = sorted(new, key=sid_order)
    last_old = max((sid_order(one) for one in old), default=None)
    later = [one for one in ordered if last_old is None or sid_order(one) > last_old]
    return (later[0] if later else ordered[0],)


def fact_changes(facts: Mapping[str, Fact], known: Iterable[Change] = ()) -> list[Change]:
    """事实被替代的，记一条变化：前一头是被替代的那条，后一头是替代它的（superseded_by），出处是变的那一句（first_after）。
    这一边自己写过的不重复记。"""
    seen = {(c.before.id, c.after.id) for c in known if c.before and c.after and c.before.kind == c.after.kind == "fact"}
    return [
        Change(f"f-{fact.id}", Ref("fact", fact.id), Ref("fact", fact.superseded_by), None,
               first_after(facts[fact.superseded_by].evidence, fact.evidence))
        for fact in facts.values()
        if fact.superseded_by in facts and (fact.id, fact.superseded_by) not in seen
    ]


def relation_changes(relations: Mapping[str, Relation], *, tolerance_days: int = TOLERANCE_DAYS) -> list[Change]:
    """关系结束了、被取代了、暂停了，记一条变化；同一个主体（负责看客体）同一种关系在那几天开始的另一条，算后一头。
    出处：有后一头的，是后一头在前一头最后一句之后的第一句；没有的，是结束的那条最后一句（说结束、暂停多半是最后说的）。"""
    out: list[Change] = []
    for relation in relations.values():
        if relation.state not in ("已结束", "被取代", "暂停"):
            continue
        after: Ref | None = None
        ended = When(relation.when.end, None, relation.when.precision) if relation.when.end else None  # 结束的那天
        for other in relations.values():
            if other.id == relation.id or other.relation != relation.relation:
                continue
            same_end = other.object == relation.object if relation.relation == "负责" else other.subject == relation.subject
            if not same_end:
                continue
            if ended is not None and close(other.when, ended, tolerance_days):
                after = Ref("relation", other.id)
                break
        if after is not None:
            evidence = first_after(relations[after.id].evidence, relation.evidence)
        else:
            evidence = (max(relation.evidence, key=sid_order),) if relation.evidence else ()
        out.append(Change(f"r-{relation.id}", Ref("relation", relation.id), after, relation.when.end, evidence))
    return out


def _item_evidence(side: Side, ref: Ref | None) -> tuple[str, ...]:
    if ref is None:
        return ()
    if ref.kind == "fact":
        fact = side.facts.get(ref.id)
        return fact.evidence if fact else ()
    relation = side.relations.get(ref.id)
    return relation.evidence if relation else ()


@dataclass(frozen=True)
class ChangeVerdict:
    gold: str
    unit: str
    verdict: str  # 判对、只对上后一头、抽到了新的没判成变化、漏了；不算变化的：没判错、当成变了、当成新开始
    right: bool
    side: str | None = None


def _about(side: Side, change: Change, view: EntityView, gold_name: str) -> bool | None:
    """这一条变化说的是不是金标准这个实体：后一头（没有就前一头）的主体、涉及的实体、文字里提没提到它。
    这一边的实体一个都没对上金标准的，说不准，返回 None。"""
    ref = change.after or change.before
    if ref is None:
        return None
    if ref.kind == "fact":
        fact = side.facts.get(ref.id)
        if fact is None:
            return None
        if view.mentioned(gold_name, fact) or view.gold_of(fact.subject) == gold_name:
            return True
        known = {view.gold_of(one) for one in (fact.subject, *fact.entities)} - {None}
        return False if known else None
    relation = side.relations.get(ref.id)
    if relation is None:
        return None
    ends = {view.gold_of(relation.subject), view.gold_of(relation.object)}
    if gold_name in ends:
        return True
    return False if ends - {None} else None


def judge_changes(items: Sequence[GoldItem], side: Side, changes: Sequence[Change],
                  gold_relations: Mapping[str, GoldRelation], *, tolerance_days: int = TOLERANCE_DAYS,
                  view: EntityView | None = None) -> list[ChangeVerdict]:
    """gold_relations：金标准关系条目的编号 → 它合进去的那一条（看“说明白了”的那条关系本来哪天开始）。
    给了 view 的，这一边的变化说的是金标准别的实体的（同一句里另一件事变了），不算这一条。"""
    out: list[ChangeVerdict] = []
    # 变的那一句：这一边写了出处的用它写的（打分自己算的变化写的就是变的那一句），没写的用后一头的证据
    after_of = {change.id: set(change.evidence or _item_evidence(side, change.after)) for change in changes}
    before_of = {change.id: set(_item_evidence(side, change.before)) for change in changes}
    sentence, passage = by_sentence(side.facts.values())
    relation_sentence: dict[str, set[str]] = {}
    for relation in side.relations.values():
        for sid in relation.evidence:
            relation_sentence.setdefault(utterance(sid), set()).add(relation.id)
    for item in items:
        if item.change is None:
            continue
        after = item.role_sids("after") or item.sids
        before = item.role_sids("before")
        hit = [change for change in changes if overlap(after_of[change.id], after)]
        if view is not None and item.subject is not None:
            hit = [change for change in hit if _about(side, change, view, item.subject[0]) is not False]
        if item.change.get("is_change") is False:
            if hit:
                out.append(ChangeVerdict(item.id, item.unit, "当成变了", False, hit[0].id))
                continue
            fresh = None
            linked = [gold_relations[link] for link in item.links if link in gold_relations]
            for relation_id in sorted({rid for sid in after for rid in relation_sentence.get(utterance(sid), set())}):
                relation = side.relations[relation_id]
                if not close(relation.when, item.when, tolerance_days):
                    continue
                if not linked or all(not any(close(item.when, start, tolerance_days) for start in one.starts) for one in linked):
                    fresh = relation_id
                    break
            if fresh is not None:
                out.append(ChangeVerdict(item.id, item.unit, "当成新开始", False, fresh))
            else:
                out.append(ChangeVerdict(item.id, item.unit, "没判错", True))
            continue
        full = [change for change in hit if not before or overlap(before_of[change.id], before)]
        if full:
            out.append(ChangeVerdict(item.id, item.unit, "判对", True, full[0].id))
        elif hit:
            out.append(ChangeVerdict(item.id, item.unit, "只对上后一头", False, hit[0].id))
        elif candidates(after, sentence, passage) or any(relation_sentence.get(utterance(sid)) for sid in after):
            out.append(ChangeVerdict(item.id, item.unit, "抽到了新的没判成变化", False))
        else:
            out.append(ChangeVerdict(item.id, item.unit, "漏了", False))
    return out


# ---------- 别名 ----------


@dataclass(frozen=True)
class AliasVerdict:
    key: tuple[str, str, str]  # (写法的比对写法, 标准名, 种类)
    items: tuple[str, ...]
    verdict: str  # 对、没认出、没合并、对错了、有歧义；不是同一个的：没合错、合错了、查不了；泛称的：没建、建了实体
    right: bool | None  # None：查不了，不算
    side: tuple[str, ...] = ()


def judge_aliases(merged: Mapping[tuple[str, str, str], Sequence[GoldItem]], alignment: Alignment,
                  names: Mapping[str, Mapping[str, int]] | None = None) -> list[AliasVerdict]:
    """names：金标准的比对写法 → {实体: 对得多准}（javmem.scoring.align.NameIndex.entries）。一个叫法在金标准的实体表里
    （标准名、别名）就对着别的实体的，金标准自己两处说法不一，不算分，单列。"""
    out: list[AliasVerdict] = []
    for key, items in sorted(merged.items()):
        mention, entity, kind = key
        ids = tuple(item.id for item in items)
        holders = tuple(sorted(alignment.keys.get(mention, set())))
        anchor = alignment.anchor.get(entity)
        if kind not in ("不是同一个", "泛称（不建实体）") and names is not None:
            others = {name for name, rank in names.get(mention, {}).items() if rank <= TABLE_ALIAS and name != entity}
            if others:
                out.append(AliasVerdict(key, ids, "金标准里这个叫法也对着别的实体", None, holders))
                continue
        if kind == "不是同一个":
            if mention == name_key(entity):
                out.append(AliasVerdict(key, ids, "查不了", None, holders))
            elif anchor is not None and anchor in holders:
                out.append(AliasVerdict(key, ids, "合错了", False, holders))
            else:
                out.append(AliasVerdict(key, ids, "没合错", True, holders))
            continue
        if kind == "泛称（不建实体）":
            out.append(AliasVerdict(key, ids, "建了实体" if holders else "没建", not holders, holders))
            continue
        if not holders:
            out.append(AliasVerdict(key, ids, "没认出", False))
        elif anchor is not None and holders == (anchor,):
            out.append(AliasVerdict(key, ids, "对", True, holders))
        elif anchor is not None and anchor in holders:
            out.append(AliasVerdict(key, ids, "另有实体也叫这个", False, holders))
        else:
            golds = {alignment.gold_of(one) for one in holders}
            if entity in golds or golds <= {None}:
                out.append(AliasVerdict(key, ids, "没合并", False, holders))
            else:
                out.append(AliasVerdict(key, ids, "对错了", False, holders))
    return out
