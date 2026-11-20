"""第二证人（J6.2）、每周体检（J6.3）的比对：同一个包，支线的关系和主线的关系放在一起看。纯函数，两边都是对齐格式
（javmem.scoring.common.Side），不连体检库、不读账本。主线的关系 J4.4 合进 dev 以后从账本读成 Side（现在用 javmem score
那一套从结果库读，或者给一份导出的对齐格式）。

- 名字用同一张别名表对齐（04 p5）：比对写法（name_key），别名表里同一组的换成那一组的标准名；主人（叫法、别名“主人”）
  都换成“主人”。
- 对不上分四种：
  1. 另一头不一样：同一样东西、“同一时间一个”的关系，两边另一头不一样。托管于看主体（一样东西放在哪家）；
     负责看客体，只看一件事、一张单子（项目、订单或合同）归谁管：一个客户、一家机构本来就可以有好几个人负责
     （主人在每家都有一个客户经理），不算矛盾。
  2. 一边结束了：同一条关系（两头、种类都一样），一边生效、一边已结束（被取代的也算结束）。
  3. 只有主线有。
  4. 只有支线有。
  1、2 是真矛盾，建议进待确认（要主人定，接进待确认等 J4.4）；3、4 进体检报告。
- 体检报告（J6.3）：主线漏了什么（只有支线有的）按关系种类、两头的类型数；支线抽的不在七种里的关系按名字数。
  带包号和句子编号，不抄原话。
"""

from __future__ import annotations

import collections
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from javmem.pipeline.merge import FIRST_PERSON, OWNER_NAMES, name_key
from javmem.scoring.common import Relation, Side
from javmem.zh.lexicon import Lexicon

OTHER_END = "另一头不一样"
ENDED = "一边结束了"
MAIN_ONLY = "只有主线有"
SIDE_ONLY = "只有支线有"
KINDS = (OTHER_END, ENDED, MAIN_ONLY, SIDE_ONLY)
CONTRADICTIONS = (OTHER_END, ENDED)  # 建议进待确认的两种
FUNCTIONAL = {"托管于": "subject", "负责": "object", "隶属于": "subject"}  # 这一头一样、另一头只能有一个（隶属于：D210）
# 负责只看这几类的客体（一件事、一张单子归一个人管）；人、公司或机构可以有好几个人负责
FUNCTIONAL_OBJECT_TYPES = {"负责": frozenset({"项目", "订单或合同"})}
OWNER = "主人"
_OWNER_KEYS = frozenset(name_key(name) for name in (*OWNER_NAMES, *FIRST_PERSON, "用户", "user"))
_LIVE = "生效"


@dataclass(frozen=True)
class Key:
    subject: str
    relation: str
    object: str


@dataclass(frozen=True)
class Mismatch:
    kind: str
    packet: str
    relation: str
    main: Key | None
    side: Key | None
    main_ids: tuple[str, ...] = ()
    side_ids: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()  # 句子编号，不抄原话


@dataclass
class Comparison:
    packets: list[str] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    counts: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)  # 包 → 关系 → {main, side, both}

    def by_kind(self) -> dict[str, int]:
        found = collections.Counter(item.kind for item in self.mismatches)
        return {kind: found.get(kind, 0) for kind in KINDS}

    def queue(self) -> list[Mismatch]:
        """建议进待确认的（只有矛盾的两种）。"""
        return [item for item in self.mismatches if item.kind in CONTRADICTIONS]


class Names:
    """一边的实体 → 对齐用的名字。"""

    def __init__(self, side: Side, lexicon: Lexicon | None, owner_keys: Collection[str] = ()) -> None:
        self.side = side
        self.lexicon = lexicon
        self.owner_keys = frozenset(owner_keys)

    def of(self, entity_id: str) -> str:
        entity = self.side.entities.get(entity_id)
        if entity is None:
            return entity_id
        names = (entity.name, *entity.aliases)
        if OWNER in names or any(name_key(name) in _OWNER_KEYS or name_key(name) in self.owner_keys for name in names):
            return OWNER
        key = name_key(entity.name)
        if self.lexicon is not None:
            group = self.lexicon.group_by_key(key, entity.type)
            if group is None:
                for alias in entity.aliases:
                    group = self.lexicon.group_by_key(name_key(alias), entity.type)
                    if group is not None:
                        break
            if group is not None:
                return name_key(group.standard)
        return key


def _packet_of(relation: Relation) -> str | None:
    packets = {sid.split("#", 1)[0] for sid in relation.evidence if "#" in sid}
    return min(packets) if packets else None


def _keyed(side: Side, names: Names, packets: Collection[str]) -> dict[str, dict[Key, list[Relation]]]:
    out: dict[str, dict[Key, list[Relation]]] = {packet: {} for packet in packets}
    for relation in side.relations.values():
        packet = _packet_of(relation)
        if packet not in out:
            continue
        key = Key(names.of(relation.subject), relation.relation, names.of(relation.object))
        out[packet].setdefault(key, []).append(relation)
    return out


def _live(relations: Iterable[Relation]) -> bool:
    return any(relation.state == _LIVE for relation in relations)


def _evidence(*groups: Iterable[Relation]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(sid for group in groups for relation in group for sid in relation.evidence))


def compare(main: Side, side: Side, lexicon: Lexicon | None, *, packets: Collection[str] | None = None,
            owner_keys: Collection[str] = ()) -> Comparison:
    """两边都抽过的包（或者点名的包）一包一包比。"""
    chosen = sorted((set(main.packets) & set(side.packets)) if packets is None else set(packets))
    main_keys = _keyed(main, Names(main, lexicon, owner_keys), chosen)
    side_keys = _keyed(side, Names(side, lexicon, owner_keys), chosen)
    result = Comparison(packets=chosen)
    for packet in chosen:
        mine, theirs = main_keys[packet], side_keys[packet]
        counts: dict[str, dict[str, int]] = collections.defaultdict(lambda: {"main": 0, "side": 0, "both": 0})
        for key in mine:
            counts[key.relation]["main"] += 1
        for key in theirs:
            counts[key.relation]["side"] += 1
        paired_main: set[Key] = set()
        paired_side: set[Key] = set()
        for key in sorted(set(mine) & set(theirs), key=lambda one: (one.relation, one.subject, one.object)):
            counts[key.relation]["both"] += 1
            paired_main.add(key)
            paired_side.add(key)
            if _live(mine[key]) != _live(theirs[key]):
                result.mismatches.append(Mismatch(
                    ENDED, packet, key.relation, key, key, tuple(r.id for r in mine[key]), tuple(r.id for r in theirs[key]),
                    _evidence(mine[key], theirs[key])))
        for relation, end in FUNCTIONAL.items():
            def anchor(key: Key, end: str = end) -> str:
                return key.subject if end == "subject" else key.object

            def counted(key: Key, found: Mapping[Key, list[Relation]], one_side: Side, relation: str = relation) -> bool:
                allowed = FUNCTIONAL_OBJECT_TYPES.get(relation)
                if allowed is None:
                    return True
                if key.object == OWNER:
                    return False
                kinds = {getattr(one_side.entities.get(item.object), "type", None) for item in found[key]}
                return bool(kinds & allowed)

            main_live = [key for key in mine if key.relation == relation and key not in paired_main and _live(mine[key])
                         and counted(key, mine, main)]
            side_live = [key for key in theirs if key.relation == relation and key not in paired_side and _live(theirs[key])
                         and counted(key, theirs, side)]
            for main_key in main_live:
                for side_key in side_live:
                    if anchor(main_key) == anchor(side_key) and side_key not in paired_side:
                        paired_main.add(main_key)
                        paired_side.add(side_key)
                        result.mismatches.append(Mismatch(
                            OTHER_END, packet, relation, main_key, side_key, tuple(r.id for r in mine[main_key]),
                            tuple(r.id for r in theirs[side_key]), _evidence(mine[main_key], theirs[side_key])))
                        break
        for key in sorted(set(mine) - paired_main, key=lambda one: (one.relation, one.subject, one.object)):
            result.mismatches.append(Mismatch(MAIN_ONLY, packet, key.relation, key, None,
                                              tuple(r.id for r in mine[key]), (), _evidence(mine[key])))
        for key in sorted(set(theirs) - paired_side, key=lambda one: (one.relation, one.subject, one.object)):
            result.mismatches.append(Mismatch(SIDE_ONLY, packet, key.relation, None, key, (),
                                              tuple(r.id for r in theirs[key]), _evidence(theirs[key])))
        result.counts[packet] = dict(counts)
    return result


def checkup_report(comparison: Comparison, side: Side) -> dict[str, object]:
    """体检报告（J6.3）：主线漏了什么。按关系种类、两头的类型数；支线抽的不在七种里的关系按名字数。不抄原话。"""
    missed = [item for item in comparison.mismatches if item.kind == SIDE_ONLY]
    by_relation = collections.Counter(item.relation for item in missed)
    by_types: collections.Counter[str] = collections.Counter()
    for item in missed:
        for relation_id in item.side_ids:
            relation = side.relations.get(relation_id)
            if relation is None:
                continue
            subject, obj = side.entities.get(relation.subject), side.entities.get(relation.object)
            by_types[f"{subject.type if subject else '?'} → {obj.type if obj else '?'}"] += 1
    six = {fact.id[2:] for fact in side.facts.values()} & {relation.id[2:] for relation in side.relations.values()}
    unmapped = collections.Counter(
        fact.attribute or "" for fact in side.facts.values()
        if fact.id[2:] not in six and any(sid.split("#", 1)[0] in comparison.packets for sid in fact.evidence)
    )
    return {
        "packets": comparison.packets,
        "by_kind": comparison.by_kind(),
        "main_missed_by_relation": dict(sorted(by_relation.items())),
        "main_missed_by_types": dict(sorted(by_types.items())),
        "side_names_outside_six": dict(sorted(unmapped.items(), key=lambda item: (-item[1], item[0]))),
        "main_missed": [
            {"packet": item.packet, "relation": item.relation, "evidence": list(item.evidence)} for item in missed
        ],
        "contradictions": [
            {"kind": item.kind, "packet": item.packet, "relation": item.relation, "evidence": list(item.evidence)}
            for item in comparison.queue()
        ],
    }


def counts_table(comparison: Comparison) -> list[dict[str, object]]:
    """每包每种关系两边各几条、两边都有的几条（不用金标准也能看）。"""
    rows = []
    for packet in comparison.packets:
        for relation, numbers in sorted(comparison.counts.get(packet, {}).items()):
            rows.append({"packet": packet, "relation": relation, **numbers})
    return rows


def as_dict(comparison: Comparison) -> dict[str, object]:
    def key(value: Key | None) -> dict[str, str] | None:
        return None if value is None else {"subject": value.subject, "relation": value.relation, "object": value.object}

    return {
        "packets": comparison.packets,
        "by_kind": comparison.by_kind(),
        "counts": counts_table(comparison),
        "mismatches": [
            {"kind": item.kind, "packet": item.packet, "relation": item.relation, "main": key(item.main),
             "side": key(item.side), "main_ids": list(item.main_ids), "side_ids": list(item.side_ids),
             "evidence": list(item.evidence)}
            for item in comparison.mismatches
        ],
    }


def totals(comparison: Comparison) -> Mapping[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = collections.defaultdict(lambda: {"main": 0, "side": 0, "both": 0})
    for per_packet in comparison.counts.values():
        for relation, numbers in per_packet.items():
            for name, value in numbers.items():
                out[relation][name] += value
    return dict(out)
