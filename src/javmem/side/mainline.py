"""第二证人比对时主线那一边（J6.2）：J4.4 起的账本从关系表读；J4.4 以前的账本照旧从 L3 结果库读
（javmem.scoring.results，打分也是这么读的）。只读。

- 关系表照 J4.4 的“现行”：没撤回、没被替代、不是待确认（`withdrawn_at IS NULL AND superseded_by IS NULL AND
  status <> 'pending'`），状态是生效、暂停的；另外结束了的那一段（`state = 'ended'`）也带上，比“一边结束了”要用。
- 实体、别名、证据照打分的读法（javmem.scoring.ledger.side_from_ledger），一处说法一条；另给一张说法编号 → 关系编号的
  对照（relation_of），待确认里主线那一边写账本里的关系编号。
- 读分两步（先挑现行的关系、再照打分的读法读），两步要读到同一个样子：要写待确认的（--queue）调用方拿着收件锁、写账锁
  （javmem.side.witness.ledger_locks，locked=True），一直拿到写完；只出报告的不拿锁，照 `PRAGMA data_version` 看两步中间
  有没有人写过，写过就重读，最多读 READ_TRIES 遍。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from javmem.config import Settings
from javmem.ledger import connect_readonly
from javmem.scoring.common import Side
from javmem.scoring.ledger import open_quietly, side_from_ledger, writes_relations
from javmem.scoring.results import side_from_result
from javmem.side.witness import LedgerBusy

READ_TRIES = 3
CURRENT = "withdrawn_at IS NULL AND superseded_by IS NULL AND status <> 'pending' AND state IN ('active', 'paused', 'ended')"


@dataclass(frozen=True)
class MainLine:
    side: Side
    from_ledger: bool  # J4.4 起的账本（关系表）；False 是 J4.4 以前的，从 L3 结果库读的，没有关系编号
    relation_of: dict[str, str] = field(default_factory=dict)  # 说法编号 → 关系编号；没有说法的冻结关系对它自己


def current_relations(conn: sqlite3.Connection) -> dict[str, str]:
    """照“现行”（加上结束了的那一段）能拿来比的：说法编号（打分的读法一处说法一条）→ 关系编号；没有说法的冻结关系，
    打分照关系那一行读，编号就是它自己。"""
    relations = {row[0] for row in conn.execute(f"SELECT relation_id FROM relations WHERE {CURRENT}")}
    out = {row[0]: row[1] for row in conn.execute("SELECT claim_id, relation_id FROM relation_claims") if row[1] in relations}
    out.update({relation_id: relation_id for relation_id in relations})
    return out


def eligible_ids(conn: sqlite3.Connection) -> set[str]:
    """能拿来比的关系编号和它们的说法编号。"""
    return set(current_relations(conn))


def read_main_line(settings: Settings, library: str, packets: Collection[str], *, locked: bool = False) -> MainLine:
    """locked：调用方拿着 javmem.side.witness.ledger_locks，读一遍就是同一个样子；不然照 data_version 看两步中间有没有人
    写过，写过就重读（见模块说明），读了 READ_TRIES 遍每一遍中间都有人写的，报 LedgerBusy。"""
    ledger = settings.library(library).db_path
    if locked:
        return _read(settings, library, ledger, packets)
    watch = connect_readonly(ledger)
    try:
        for _ in range(READ_TRIES):
            before = watch.execute("PRAGMA data_version").fetchone()[0]
            line = _read(settings, library, ledger, packets)
            if watch.execute("PRAGMA data_version").fetchone()[0] == before:
                return line
    finally:
        watch.close()
    raise LedgerBusy(f"账本一直在写（读了 {READ_TRIES} 遍，每一遍中间都有人写）")


def _read(settings: Settings, library: str, ledger: Path, packets: Collection[str]) -> MainLine:
    conn = open_quietly(ledger)
    try:
        from_ledger = writes_relations(conn)
        keep = current_relations(conn) if from_ledger else {}
    finally:
        conn.close()
    if not from_ledger:
        side, _ = side_from_result(settings.require_data_dir(), list(packets), name="main", library=library)
        return MainLine(side, False)
    side = side_from_ledger(ledger, list(packets), name="main")
    relations = {rid: relation for rid, relation in side.relations.items() if rid in keep}
    return MainLine(dataclasses.replace(side, relations=relations, changes=()), True,
                    {rid: keep[rid] for rid in relations})


def main_side(settings: Settings, library: str, packets: Collection[str]) -> Side:
    return read_main_line(settings, library, packets).side
