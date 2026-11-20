"""欠着没同步进图的（账本第 26 版的 graph_dirty，0026_graph_sync.sql）。

平时是触发器记的（triggers.sql）：javmem graph init 接上图的时候才装（install_triggers），init --reset 拆掉
（drop_triggers），没接上图的账本一个都没有。这里另给两样：

- mark：别的地方（J5.3 进图前检查的结论变了、管理命令）自己记一笔欠着，下次同步再算这几条。这本账还没接上图的时候什么都
  不记（接上以后第一次同步本来就是整张的）。
- owed：欠着多少（按种类）、最早从什么时候欠的，javmem graph status 和写账以后那一行用。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from importlib import resources

KINDS = ("entity", "alias", "fact", "relation", "packet")
# 改了 triggers.sql 就加一：同步时认出装着的是旧的，换成新的、整张同步一遍。t2：被替代的这一条有什么改动，替代它的那条都记（审阅第 6 条）
TRIGGERS_VERSION = "t2"
TRIGGER_PREFIX = "graph_mark_"
_TRIGGER_NAME = re.compile(r"^CREATE TRIGGER (\w+)", re.MULTILINE)


def triggers_sql() -> str:
    return (resources.files("javmem.graph") / "triggers.sql").read_text(encoding="utf-8")


def expected_triggers() -> frozenset[str]:
    """triggers.sql 里的触发器的名字。"""
    return frozenset(_TRIGGER_NAME.findall(triggers_sql()))


def installed_triggers(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE ? ESCAPE '\\'",
        (TRIGGER_PREFIX.replace("_", "\\_") + "%",),
    ))


def triggers_ok(conn: sqlite3.Connection) -> bool:
    """装着的正好是这一版的那几个（以后的迁移重建一张表，那张表上的触发器会跟着没了：审阅第 5 条）。"""
    return installed_triggers(conn) == expected_triggers()


def drop_triggers(conn: sqlite3.Connection) -> int:
    """拆掉记欠账的触发器。不自己开事务。返回拆了几个。"""
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE ? ESCAPE '\\'", (TRIGGER_PREFIX.replace("_", "\\_") + "%",))]
    for name in names:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    return len(names)


def install_triggers(conn: sqlite3.Connection) -> None:
    """装上（已经装着的先拆了再装，换成这一版），记下版本。要在一个事务里调（javmem.ledger.transaction）：
    autocommit 的连接上，executescript（建触发器）不会自己提交。"""
    drop_triggers(conn)
    conn.executescript(triggers_sql())
    conn.execute("UPDATE graph_state SET triggers = ? WHERE id = 1", (TRIGGERS_VERSION,))


def installed(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' AND name LIKE ? ESCAPE '\\'",
        (TRIGGER_PREFIX.replace("_", "\\_") + "%",),
    ).fetchone()[0])


def mark(conn: sqlite3.Connection, kind: str, ids: Iterable[str]) -> int:
    """记这几条欠着（同一条已经欠着的只改成这一轮）。返回记了几条。不自己开事务：调的地方在自己的事务里调就行。"""
    if kind not in KINDS:
        raise ValueError(f"没有这一样：{kind}")
    wanted = sorted({str(one) for one in ids if one})
    if not wanted:
        return 0
    row = conn.execute("SELECT enabled, gen FROM graph_state WHERE id = 1").fetchone()
    if row is None or not row[0]:
        return 0
    conn.executemany(
        "INSERT INTO graph_dirty (kind, item_id, gen) VALUES (?, ?, ?)"
        " ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen",
        [(kind, one, row[1]) for one in wanted],
    )
    return len(wanted)


# 欠着的一条算不算“撤回了的还在主图里”（J5.4：巡检的 graph_withdrawn、查询那一边这一阵不查主图都看它）。graph_dirty 只记了
# 哪一样、编号，没记为什么，所以照账本现在的样子认（graph_dirty 的别名是 d）：
# - 包登记变了的（packet）：这个包撤回了、彻底删了、有被新版本换下来的那一版，或者登记整个没了。刚收进来的
#   （登记了 → 现行）也记成 packet，那种不算。
# - 事实、关系、别名：图里有（graph_items），账本里却撤回了或者没了。
WITHDRAWN_CONDITION = """
(d.kind = 'packet' AND (NOT EXISTS (SELECT 1 FROM packets AS p WHERE p.packet_id = d.item_id)
    OR EXISTS (SELECT 1 FROM packets AS p WHERE p.packet_id = d.item_id
               AND (p.withdrawn_at IS NOT NULL OR p.purged_at IS NOT NULL OR p.state = 'superseded'))))
OR (d.kind IN ('fact', 'relation', 'alias')
    AND EXISTS (SELECT 1 FROM graph_items AS g WHERE g.kind = d.kind AND g.item_id = d.item_id)
    AND CASE d.kind
        WHEN 'fact' THEN NOT EXISTS (SELECT 1 FROM facts AS f WHERE f.fact_id = d.item_id AND f.withdrawn_at IS NULL)
        WHEN 'relation' THEN NOT EXISTS (SELECT 1 FROM relations AS r WHERE r.relation_id = d.item_id AND r.withdrawn_at IS NULL)
        ELSE NOT EXISTS (SELECT 1 FROM aliases AS a WHERE a.alias_id = d.item_id AND a.withdrawn_at IS NULL) END)
"""


def withdrawn_owed(conn: sqlite3.Connection) -> bool:
    """有没有撤回、换版本以后还没同步进图的（主图里还留着撤回了的东西）。碰上第一条就停。"""
    return conn.execute(f"SELECT 1 FROM graph_dirty AS d WHERE {WITHDRAWN_CONDITION} LIMIT 1").fetchone() is not None


def owed(conn: sqlite3.Connection) -> tuple[dict[str, int], str | None]:
    """欠着的：每一样几条，最早的那一条从什么时候欠的（UTC）。"""
    counts = {str(row[0]): int(row[1]) for row in conn.execute("SELECT kind, count(*) FROM graph_dirty GROUP BY kind")}
    oldest = conn.execute("SELECT min(since) FROM graph_dirty").fetchone()[0]
    return counts, oldest
