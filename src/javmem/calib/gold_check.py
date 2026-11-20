"""`javmem gold check`（U3，D141 ③）：核一份金标准写得对不对。只报条目编号和哪一种不对，不抄内容。

金标准是打分（`javmem score`）读的那种：每个单元一个 JSON（`javmem-gold-prelabel/1`），加一份实体表
（`javmem-gold-entities/1`），格式见 docs/gold-format.md。先照打分的读法读一遍（结构不对就读不了，退出码 2），
再一条一条核：

- 每条都要有出处；出处的包要是这个单元的包；给了数据目录的，出处的句子编号账本里要有，写了原话片段的，要是那一句的
  一段（空白、全角半角不算差别）。
- 实体的类型是那几种之一（人、公司或机构、项目、订单或合同、地点、资产、Bot）；名字要在实体表里，或者是这个单元
  `new_entities` 里写着的。别名指向的实体也一样。
- 关系是那七种之一（持有、托管于、负责、任职于、客户是、亲属、隶属于），两头的类型要是允许的那几种（和 L4 核对关系
  的允许表是同一张）。
- 事实写了分类的，分类是那五种之一；日子的精度是 minute、day、month、quarter、year、unknown 之一。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from javmem.pipeline.extract import RELATION_TYPES
from javmem.pipeline.relcheck import ALLOWED
from javmem.scoring.common import ENTITY_TYPES
from javmem.scoring.dates import precision_rank
from javmem.scoring.gold import FACT_CLASSES, Gold, GoldItem, GoldUnit, split_sid

NO_EVIDENCE = "没有出处"
OUTSIDE = "出处不在这个单元的包里"
NO_SENTENCE = "出处的句子账本里没有"
QUOTE = "原话片段不是那一句的一段"
ENTITY_TYPE = "实体的类型不对"
NAME = "名字不在实体表里"
RELATION = "关系不是那七种"
ENDS = "关系两头的类型不对"
FACT_CLASS = "事实的分类不对"
PRECISION = "日子的精度不对"


@dataclass
class CheckResult:
    """只有编号和数。problems 是 (条目编号, 哪一种不对)，按编号排。"""

    units: int = 0
    items: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)
    ledger: bool = False  # 核了账本里的句子没有

    def kinds(self) -> Counter[str]:
        return Counter(kind for _, kind in self.problems)

    def bad_items(self) -> int:
        return len({item_id for item_id, _ in self.problems})


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _item_problems(unit: GoldUnit, item: GoldItem, names: set[str], conn: sqlite3.Connection | None) -> set[str]:
    found: set[str] = set()
    for pair in (item.subject, item.object):
        if pair is None:
            continue
        name, kind = pair
        if kind not in ENTITY_TYPES:
            found.add(ENTITY_TYPE)
        if name not in names:
            found.add(NAME)
    if item.type == "alias" and item.alias is not None and item.alias.get("entity") not in names:
        found.add(NAME)
    if item.type == "relation":
        if item.relation not in RELATION_TYPES:
            found.add(RELATION)
        elif item.subject is not None and item.object is not None:
            if (item.subject[1], item.object[1]) not in ALLOWED.get(item.relation or "", frozenset()):
                found.add(ENDS)
    if item.type == "fact" and item.fact_class is not None and item.fact_class not in FACT_CLASSES:
        found.add(FACT_CLASS)
    if item.when.precision != "unknown" and precision_rank(item.when.precision) == 0:
        found.add(PRECISION)
    if not item.evidence:
        found.add(NO_EVIDENCE)
    for evidence in item.evidence:
        packet, _ = split_sid(evidence.sid)
        if packet not in unit.packets:
            found.add(OUTSIDE)
        if conn is None:
            continue
        row = conn.execute("SELECT text FROM raw_sentences WHERE sentence_id = ?", (evidence.sid,)).fetchone()
        if row is None:
            found.add(NO_SENTENCE)
        elif evidence.quote and _norm(evidence.quote) not in _norm(str(row[0])):
            found.add(QUOTE)
    return found


def check(gold: Gold, conn: sqlite3.Connection | None = None) -> CheckResult:
    """conn：这一份金标准对着的那个库的账本（只读）；不给就不核出处的句子、原话片段。"""
    result = CheckResult(units=len(gold.units), ledger=conn is not None)
    known = set(gold.entities)
    for unit_id in sorted(gold.units):
        unit = gold.units[unit_id]
        names = known | {name for name, _ in unit.new_entities}
        for item in unit.items:
            result.items += 1
            result.problems += [(item.id, kind) for kind in sorted(_item_problems(unit, item, names, conn))]
    result.problems.sort()
    return result
