"""进图前检查的插口（给 J5.3 留的，D178）。这一步是“全放”（AdmitAll）：账本认了的都进。

插在同步的哪一步：事实、关系先照账本的规矩（没撤回、不是待确认、核对过或推断）算出“该进图”的那一批、写好要进图的字段以后，
算指纹、写图以前，整批交给 Gate.admit。每次同步只交这一次要重算的那几条（欠着的和扩出来的）；javmem graph sync --all、
整张重建交全部。实体、别名不过这道：实体跟着进了图的事实、关系走，别名照 javmem.graph.project 的规矩。

拿到什么：账本的连接（只读着用）、哪个库、一批 GateItem（哪一样、编号、可见范围、要写进图的全部字段（不带向量）、字段的指纹）。
要原句、证据，按编号回账本查（evidence、relation_evidence）。

返回什么：每一条一个结论（Verdict）：ADMIT 进图；HOLD 不进图（没过检查，或者财务投资转了人工），可以带一句原因（reasons），
同步的汇总按原因数；LATER 这次判不了，这次不进图，欠着的留着，下次同步再交。没给结论的当 LATER。version 算进每一条的指纹：
换了版本 javmem graph sync --all 一遍，全部重交。

没过的怎么留在账本：同步这边不动账本里的这一条，只是不写进图，图里原来有的删掉。判了什么、依据哪段原文，检查自己写进
留痕表 audit_log；财务投资转人工的自己写待确认。结论要变的时候（主人在待确认里定了），调 javmem.graph.dirty.mark 记一笔
欠着，下次同步再交。检查自己缓存结论，同一条没变就不再调模型。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class Verdict(str, Enum):
    ADMIT = "admit"
    HOLD = "hold"
    LATER = "later"


@dataclass(frozen=True)
class GateItem:
    kind: str  # fact 或 relation
    item_id: str
    partitions: tuple[str, ...]
    sensitive: tuple[str, ...]
    props: Mapping[str, object]  # 要写进图的全部字段（不带向量）
    fingerprint: str  # 这些字段的指纹（不含检查的版本）


@dataclass
class GateResult:
    verdicts: dict[tuple[str, str], Verdict] = field(default_factory=dict)  # (哪一样, 编号) → 结论
    reasons: dict[tuple[str, str], str] = field(default_factory=dict)  # HOLD 的原因，汇总时按它数


class Gate(Protocol):
    version: str

    def admit(self, conn: sqlite3.Connection, library: str, items: Sequence[GateItem]) -> GateResult: ...


class AdmitAll:
    """这一步的检查：账本认了的都进。"""

    version = "admit-all"

    def admit(self, conn: sqlite3.Connection, library: str, items: Sequence[GateItem]) -> GateResult:
        return GateResult({(item.kind, item.item_id): Verdict.ADMIT for item in items})


@dataclass
class GateStats:
    """一次同步里检查的数：放了几条、按原因挡了几条、这次没判的几条（同步的汇总打出来）。"""

    admitted: int = 0
    held: Counter[str] = field(default_factory=Counter)
    later: int = 0

    def add(self, items: Sequence[GateItem], result: GateResult) -> dict[tuple[str, str], Verdict]:
        """把这一批的结论数进来，返回每一条最后算什么（没给结论的当 LATER）。"""
        verdicts = {}
        for item in items:
            key = (item.kind, item.item_id)
            verdict = result.verdicts.get(key, Verdict.LATER)
            verdicts[key] = verdict
            if verdict is Verdict.ADMIT:
                self.admitted += 1
            elif verdict is Verdict.HOLD:
                self.held[result.reasons.get(key) or "other"] += 1
            else:
                self.later += 1
        return verdicts

    def describe(self) -> dict[str, object]:
        return {"admitted": self.admitted, "held": dict(sorted(self.held.items())), "later": self.later}

    def text(self) -> str:
        held = sum(self.held.values())
        reasons = "、".join(f"{reason} {count}" for reason, count in sorted(self.held.items()))
        parts = [f"放了 {self.admitted} 条"]
        if held:
            parts.append(f"挡了 {held} 条（{reasons}）")
        if self.later:
            parts.append(f"这次没判 {self.later} 条")
        return "，".join(parts)


def merged(verdicts: Mapping[tuple[str, str], Verdict]) -> dict[str, int]:
    return dict(Counter(verdict.value for verdict in verdicts.values()))
