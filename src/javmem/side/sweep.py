"""“扫一遍”（J6.4 的前一半，不碰 withdraw.py）：账本里现在撤回了的、彻底删除了的、换了版本的包，单条撤回了的句子，
和支线的库对，碰到的组在体检库里整组删、在支线的库里也删。

- 账本只读打开，只读原文那一层（packets、raw_sentences），主线的结果一张都不读。
- 包撤回了、彻底删除了、换了新版本：删这一组（新版本下次跑时重抽）。
- 有句子单条撤回了：删这一组（撤回的句子 L1 不进窗口，重跑就没了），记进 rerun。
- 体检库里有、支线的库里没有的组（跑到一半进程没了这类）：也删。
- 体检库连不上（graph 是 None）：支线的库里这一组的原文（行）当场删掉，组留着、记一笔“待清”，体检库起来以后
  （下次支线的哪个命令连上了）先整组删完。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from javmem.ledger import connect_readonly
from javmem.side.store import GroupRow, SideStore

WITHDRAWN = "包撤回了"
PURGED = "包彻底删除了"
SUPERSEDED = "包换了新版本"
SENTENCES = "有句子单条撤回了"
ORPHAN = "支线的库里没有这一组"
_CHUNK = 500


class GroupGraph(Protocol):
    async def delete_group(self, group_id: str) -> tuple[int, int]: ...

    async def group_ids(self) -> set[str]: ...


@dataclass
class SweepResult:
    cleared: list[tuple[str, str]] = field(default_factory=list)  # (组, 原因)
    pending: list[tuple[str, str]] = field(default_factory=list)  # 体检库连不上，记成待清的
    rerun: list[str] = field(default_factory=list)  # 单条撤回以后要重跑的包
    packets: list[str] = field(default_factory=list)  # 删了的（和记成待清的）组是哪几个包：第二证人的待确认跟着删


def _withdrawn_or_missing(conn: sqlite3.Connection, sentence_ids: Collection[str]) -> bool:
    ids = sorted(sentence_ids)
    seen: set[str] = set()
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT sentence_id, withdrawn_at FROM raw_sentences WHERE sentence_id IN ({marks})", chunk):
            if row["withdrawn_at"] is not None:
                return True
            seen.add(row["sentence_id"])
    return len(seen) != len(ids)


def stale_groups(conn: sqlite3.Connection, store: SideStore) -> list[tuple[GroupRow, str]]:
    """支线的库里哪些组要删、为什么。conn 是账本（只读）。"""
    out: list[tuple[GroupRow, str]] = []
    for group in store.groups():
        row = conn.execute("SELECT state, withdrawn_at FROM packets WHERE packet_id = ? AND revision = ?",
                           (group.packet_id, group.revision)).fetchone()
        if row is None:
            out.append((group, PURGED))
        elif row["withdrawn_at"] is not None:
            out.append((group, WITHDRAWN))
        elif row["state"] != "current":
            out.append((group, SUPERSEDED))
        elif _withdrawn_or_missing(conn, store.group_sentences(group.group_id)):
            out.append((group, SENTENCES))
    return out


def count_todo(ledger_path: Path, store: SideStore) -> int:
    """扫一遍要删几组（账本里不算数了的，加上记着待清的），只读。没有要删的，撤回就不用去连体检库。"""
    conn = connect_readonly(ledger_path)
    try:
        stale = {group.group_id for group, _ in stale_groups(conn, store)}
    finally:
        conn.close()
    return len(stale | {group_id for group_id, _ in store.pending_clears()})


async def sweep(ledger_path: Path, store: SideStore, graph: GroupGraph | None, *,
                result: SweepResult | None = None) -> SweepResult:
    """result 给了就往里记：中途出了错，已经删了的、记了待清的照样留在里面（撤回那边要照它说、照它删待确认）。"""
    result = SweepResult() if result is None else result
    conn = connect_readonly(ledger_path)
    try:
        stale = stale_groups(conn, store)
    finally:
        conn.close()
    pending = dict(store.pending_clears())
    known = {group.group_id: group for group in store.groups()}
    todo = [(group, reason) for group, reason in stale]
    todo += [(known[group_id], reason) for group_id, reason in pending.items()
             if group_id in known and group_id not in {group.group_id for group, _ in stale}]
    for group, reason in todo:
        if group.packet_id not in result.packets:
            result.packets.append(group.packet_id)
        if graph is None:
            store.mark_pending_clear(group, reason)  # 先记待清再删原文：中间进程没了，这一组下次照样认得出来
            store.purge_text(group.group_id)
            result.pending.append((group.group_id, reason))
            continue
        nodes, edges = await graph.delete_group(group.group_id)
        store.drop_group(group.group_id, reason, nodes, edges)
        result.cleared.append((group.group_id, reason))
        if reason == SENTENCES:
            result.rerun.append(group.packet_id)
    if graph is not None:
        mine = {group.group_id for group in store.groups()}
        for group_id in sorted(await graph.group_ids() - mine):
            nodes, edges = await graph.delete_group(group_id)
            store.note_cleared(group_id, ORPHAN, nodes, edges)
            result.cleared.append((group_id, ORPHAN))
    return result
