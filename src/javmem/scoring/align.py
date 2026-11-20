"""两边的实体对齐（J3.8）：这一边的实体对到金标准的哪一个。

金标准的名字：实体表的标准名和别名、各单元另加的实体、别名条目里的写法（“X 就是 Y”）。这一边的名字：实体的名字、
名字里拼在一起的几段（斜杠、竖线、括号隔开的）、它的别名。比对写法用 L5 的（javmem.pipeline.merge.name_key）。

名字不一样的，按“桥”对上：别名表（第一版、第二版都认，javmem.zh.lexicon）一组里的名字是同一个东西。一组名字里
只对上金标准一个实体的，整组都算那个实体的叫法；对上好几个的不用（列在“桥对上了几个”里）。认人认事拆过的底表、
第二版别名表（D103：累购、FCN 一张一个）就是这么接进来的。

一个名字对上几个金标准实体的，先看哪个对得最准（标准名 > 实体表的别名 > 别名条目的写法 > 桥），再看类型；
还分不开的算有歧义，列出来，不算对上。

锚点（anchor）：金标准一个实体在这一边可能有好几个（没合并），挑一个当“就是它”的：名字就是标准名的；没有的话
挂的事实、关系最多的。别名对不对齐看叫法落没落在锚点上。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from javmem.pipeline.merge import name_key
from javmem.scoring.common import Entity, Side
from javmem.scoring.gold import NOT_ALIAS_KINDS, Gold
from javmem.zh.lexicon import Lexicon

CANONICAL, TABLE_ALIAS, MENTION, BRIDGE = 0, 1, 2, 3
_PIECES = re.compile(r"\s*[/／|｜]\s*|[()（）]")
# 金标准里能当证据的叫法：这几种写法的别名条目
_MENTION_KINDS = ("别名", "简称", "代码", "笔误", "改名", "邮箱")
# 类型差不多的：L3 常把股票写成公司、把单子写成资产（别名表第二版的 from_types 也是这个意思）
_NEAR_TYPES = {frozenset({"资产", "公司或机构"}), frozenset({"订单或合同", "资产"}), frozenset({"项目", "公司或机构"})}


def _key(text: str) -> str:
    return name_key(text)


def pieces(name: str) -> list[str]:
    """名字里拼在一起的几段：斜杠、竖线、括号隔开的。一段的不算。"""
    found = [one.strip() for one in _PIECES.split(name) if one and one.strip()]
    return found if len(found) >= 2 else []


@dataclass
class NameIndex:
    """比对写法 → {金标准实体: 对得多准}（数越小越准）。demoted：实体表里底表那一行的别名，后来拆出来的实体
    （“预标统一”、决策号这种出处）也有的，让给后来的（D103：累购、FCN 一张一个，合称只当泛指）。"""

    entries: dict[str, dict[str, int]] = field(default_factory=dict)
    demoted: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)  # (叫法, 让出去的实体, 让给谁)

    def add(self, text: str, entity: str, rank: int) -> None:
        key = _key(text)
        if not key:
            return
        known = self.entries.setdefault(key, {})
        if rank < known.get(entity, 99):
            known[entity] = rank

    def lookup(self, text: str) -> dict[str, int]:
        return self.entries.get(_key(text), {})


@dataclass
class BridgeGroup:
    origin: str
    names: tuple[str, ...]
    hits: tuple[str, ...]  # 对上的金标准实体


@dataclass
class BridgeReport:
    used: list[BridgeGroup] = field(default_factory=list)
    ambiguous: list[BridgeGroup] = field(default_factory=list)
    unmatched: list[BridgeGroup] = field(default_factory=list)


BASE_ORIGIN = "底表"


def gold_index(gold: Gold) -> NameIndex:
    index = NameIndex()
    table: dict[str, dict[str, tuple[str, str]]] = {}  # 比对写法 → {实体: (写法, 出处)}
    for entity in gold.entities.values():
        index.add(entity.name, entity.name, CANONICAL)
        for name in entity.names:
            table.setdefault(_key(name), {})[entity.name] = (name, entity.origin)
    for key, owners in table.items():
        later = tuple(sorted(name for name, (_, origin) in owners.items() if origin != BASE_ORIGIN))
        for name, (text, origin) in owners.items():
            if later and origin == BASE_ORIGIN:
                index.add(text, name, MENTION)
                index.demoted.append((text, name, later))
            else:
                index.add(text, name, TABLE_ALIAS)
    for item in gold.items("alias"):
        alias = item.alias or {}
        if alias.get("kind", "别名") in _MENTION_KINDS and alias.get("entity") in gold.entities:
            index.add(alias["mention"], alias["entity"], MENTION)
    return index


def _best(found: Mapping[str, int]) -> tuple[str, ...]:
    if not found:
        return ()
    top = min(found.values())
    return tuple(sorted(name for name, rank in found.items() if rank == top))


def lexicon_groups(lexicon: Lexicon, origin: str) -> list[tuple[str, tuple[str, ...]]]:
    """别名表里“是同一个”的几组：第一版的 aliases，第二版的组和 Bot 名单。"""
    out: list[tuple[str, tuple[str, ...]]] = []
    if lexicon.version >= 2:
        out += [(f"{origin} 第 {number + 1} 组", group.names) for number, group in enumerate(lexicon.groups)]
        out += [(f"{origin} 的 Bot 第 {number + 1} 个", bot.names) for number, bot in enumerate(lexicon.bots)]
    else:
        out += [(f"{origin} 第 {number + 1} 组", names) for number, (_, names) in enumerate(lexicon.aliases)]
    return out


_ROW = re.compile(r"^\|(.+)\|\s*$")
_NOTE = re.compile(r"[（(][^（）()]*[）)]")
_PREFIX = re.compile(r"^(?:别名|又叫|也叫|写法)[:：]\s*")


def table_groups(text: str, origin: str) -> list[tuple[str, tuple[str, ...]]]:
    """Markdown 表格里的“标准名 | 叫法”两列（认人认事的底表就是这样写的）当成一组组名字。
    表头第一格是“标准名”、第二格带“叫法”的才认；第二格按顿号、分号拆开，去掉“别名：”和括号里的说明，
    带引号的（说明，不是叫法）不要。名字两头的括号里的也当一个叫法（“全名（小名）”）。"""
    out: list[tuple[str, tuple[str, ...]]] = []
    active = False
    number = 0
    for line in text.split("\n"):
        found = _ROW.match(line.strip())
        if not found:
            active = False
            continue
        cells = [cell.strip() for cell in found.group(1).split("|")]
        if len(cells) >= 2 and cells[0] == "标准名" and "叫法" in cells[1]:
            active = True
            continue
        if not active or len(cells) < 2 or set(cells[0]) <= set("-: "):
            continue
        number += 1
        names: list[str] = []
        for piece in [cells[0], *re.split(r"[、；;]", cells[1])]:
            piece = _PREFIX.sub("", piece.strip().strip("`"))
            if not piece or re.search(r"[“”\"「」]", piece) or len(piece) > 60:
                continue
            names.append(piece)
            outer = _NOTE.sub("", piece).strip()
            if outer and outer != piece:
                names.append(outer)
            inner = [one.strip("（）() ") for one in _NOTE.findall(piece)]
            names += [one for one in inner if one and len(one) <= 20 and not re.search(r"[，,。]|\d+ 句", one)]
        if names:
            out.append((f"{origin} 第 {number} 行", tuple(dict.fromkeys(names))))
    return out


def add_bridges(index: NameIndex, groups: Iterable[tuple[str, tuple[str, ...]]]) -> BridgeReport:
    """一组名字只对上金标准一个实体的，整组算那个实体的叫法（BRIDGE）。先把每组的对上算好再加，组和组之间不连着传。"""
    report = BridgeReport()
    pending: list[tuple[tuple[str, ...], str]] = []
    for origin, names in groups:
        found: dict[str, int] = {}
        for name in names:
            for entity, rank in index.lookup(name).items():
                found[entity] = min(rank, found.get(entity, 99))
        hits = _best(found)
        group = BridgeGroup(origin, tuple(names), hits)
        if len(hits) == 1:
            report.used.append(group)
            pending.append((tuple(names), hits[0]))
        elif hits:
            report.ambiguous.append(group)
        else:
            report.unmatched.append(group)
    for names, entity in pending:
        for name in names:
            index.add(name, entity, BRIDGE)
    return report


@dataclass
class Alignment:
    to_gold: dict[str, str]  # 这一边的实体 → 金标准实体
    ambiguous: dict[str, tuple[str, ...]]
    unaligned: list[str]
    anchor: dict[str, str]  # 金标准实体 → 这一边的锚点
    members: dict[str, list[str]]  # 金标准实体 → 对上它的这一边的实体
    mixed: dict[str, tuple[str, ...]]  # 这一边一个实体的名字、别名对上了金标准几个实体（可能合错了）
    keys: dict[str, set[str]]  # 这一边每个比对写法 → 带着它（名字或别名）的实体

    def gold_of(self, entity_id: str | None) -> str | None:
        return self.to_gold.get(entity_id) if entity_id else None


def _type_ok(side_type: str, gold_type: str) -> bool:
    return side_type == gold_type or frozenset({side_type, gold_type}) in _NEAR_TYPES


def _choose(found: Mapping[str, int], side_type: str, gold: Gold) -> tuple[str, ...]:
    best = _best(found)
    if len(best) <= 1:
        return best
    typed = tuple(name for name in best if gold.entities[name].type == side_type)
    if len(typed) == 1:
        return typed
    near = tuple(name for name in best if _type_ok(side_type, gold.entities[name].type))
    return near if len(near) == 1 else best


def align(side: Side, gold: Gold, index: NameIndex, weights: Mapping[str, int] | None = None) -> Alignment:
    weights = weights or {}
    to_gold: dict[str, str] = {}
    ambiguous: dict[str, tuple[str, ...]] = {}
    unaligned: list[str] = []
    mixed: dict[str, tuple[str, ...]] = {}
    keys: dict[str, set[str]] = {}
    for entity in side.entities.values():
        for text in (entity.name, *entity.aliases):
            key = _key(text)
            if key:
                keys.setdefault(key, set()).add(entity.id)
        chosen: tuple[str, ...] = ()
        for level in _levels(entity):
            found: dict[str, int] = {}
            for text in level:
                for name, rank in index.lookup(text).items():
                    found[name] = min(rank, found.get(name, 99))
            if found:
                chosen = _choose(found, entity.type, gold)
                break
        if len(chosen) == 1:
            to_gold[entity.id] = chosen[0]
        elif chosen:
            ambiguous[entity.id] = chosen
        else:
            unaligned.append(entity.id)
        every: set[str] = set()
        for text in (entity.name, *entity.aliases):
            hit = _best(index.lookup(text))
            if len(hit) == 1:
                every.add(hit[0])
        if len(every) > 1:
            mixed[entity.id] = tuple(sorted(every))
    members: dict[str, list[str]] = {}
    for side_id, name in to_gold.items():
        members.setdefault(name, []).append(side_id)
    anchor: dict[str, str] = {}
    for name, ids in members.items():
        canonical = _key(name)
        exact = [one for one in ids if _key(side.entities[one].name) == canonical]
        pool = exact or ids
        anchor[name] = max(pool, key=lambda one: (weights.get(one, 0), -len(side.entities[one].name), one))
    return Alignment(to_gold, ambiguous, unaligned, anchor, members, mixed, keys)


def _levels(entity: Entity) -> list[list[str]]:
    """按先后试：名字本身，名字拆开的几段，别名。"""
    return [[entity.name], pieces(entity.name), list(entity.aliases)]


def gold_names(gold: Gold, index: NameIndex) -> dict[str, set[str]]:
    """金标准每个实体的所有比对写法（标准名、别名、写法、桥）。"""
    out: dict[str, set[str]] = {}
    for key, found in index.entries.items():
        for name in found:
            out.setdefault(name, set()).add(key)
    return out


def not_alias_kind(kind: str | None) -> bool:
    return kind in NOT_ALIAS_KINDS
