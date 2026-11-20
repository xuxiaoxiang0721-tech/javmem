"""账本：SQLite。主图是它的投影，能整张重建。

正式库和测试库是两个文件。表结构靠 migrations/ 下编了号的 SQL 文件一步步升，
版本号记在 PRAGMA user_version。要改表就加新文件，已经发出去的文件不再改。
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

from javmem.config import Library

# 设计稿 01 p3–4 的表。前 11 张是第一版（0001），关系表和关系证据表是 J4.1 加的（0020）：01 的“证据”管事实和关系，
# evidence 管事实和别名，关系的证据在 relation_evidence。
LEDGER_TABLES = (
    "packets",            # 包登记
    "raw_sentences",      # 原文索引
    "entities",           # 实体
    "aliases",            # 别名
    "facts",              # 事实
    "fact_entities",      # 事实和实体
    "evidence",           # 证据
    "quarantine",         # 隔离
    "review_queue",       # 待确认
    "rules",              # 规则
    "audit_log",          # 留痕
    "relations",          # 关系
    "relation_evidence",  # 证据（关系的）
)

_MIGRATION_FILE = re.compile(r"(\d{4})_[a-z0-9_]+\.sql", re.ASCII)


class LedgerError(Exception):
    """账本的版本或迁移文件不对。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    """读包里带的迁移文件。编号必须从 1 起连续，少一个就报错。"""
    migrations = []
    for entry in (resources.files("javmem.ledger") / "migrations").iterdir():
        match = _MIGRATION_FILE.fullmatch(entry.name)
        if match:
            migrations.append(Migration(int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    migrations.sort(key=lambda m: m.version)
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise LedgerError("迁移文件的编号要从 1 起连续")
    return migrations


def connect(path: Path) -> sqlite3.Connection:
    """打开账本写。事务都显式开，见 transaction()。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """只读打开，查询走这个。文件不存在时报 sqlite3.OperationalError，不会新建。"""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, autocommit=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


_SAVEPOINTS = itertools.count(1)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """一个写事务：出错就全部撤销。

    外面已经开着事务的（合并工具在一个事务里重写几个窗口，每个窗口自己又是一个事务，J3.6），这一段用保存点：出错只撤
    这一段、错接着往外抛，外面的事务提交了才算数。"""
    if conn.in_transaction:
        name = f"javmem_{next(_SAVEPOINTS)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


@lru_cache(maxsize=1)
def latest_version() -> int:
    """代码里最新的账本版本。迁移文件跟着包走，运行中不会变，所以记住。"""
    return load_migrations()[-1].version


def _fill_raw_norm(conn: sqlite3.Connection) -> None:
    """0010：已有的句子先按空的 norm 登进 raw_norm_fts，再逐句算出来（触发器删掉空的、加上新的）。"""
    from javmem.raw.index import fill_norm  # 原文层在账本上面一层，用到才导入

    conn.execute("INSERT INTO raw_norm_fts (rowid, norm) SELECT id, norm FROM raw_sentences")
    fill_norm(conn)


def _fill_copy_key(conn: sqlite3.Connection) -> None:
    """0012：已有的句子补上比引用用的指纹（D101）。"""
    from javmem.raw.index import fill_copy_key  # 原文层在账本上面一层，用到才导入

    fill_copy_key(conn)


# 只用 SQL 做不了的那一步（按文件名认）：和这个文件在同一个事务里，SQL 跑完接着跑
_PYTHON_STEPS = {"0010_raw_norm.sql": _fill_raw_norm, "0012_quote_copies.sql": _fill_copy_key}
# 要重建一张被外键引用的表的（SQLite 改不了 CHECK）：照 SQLite 的办法，事务外先关外键，事务里重建完查一遍外键，
# 有不对的整个撤销，提交以后再开
_WITHOUT_FOREIGN_KEYS = frozenset({"0014_bot_entities.sql"})


class _ForeignKeyProblem(Exception):
    pass


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None) -> list[int]:
    """把账本升到最新，返回这次升了哪几版。

    每一版一个事务，中途失败这一版全部撤销，版本号不动。几个进程同时升也没事：
    拿到写锁之后再看一次版本，别人升过的就跳过。SQL 做不了的（0010 按统一写法补原文索引里的那一列）
    在同一个事务里接着跑 Python 的那一步。
    """
    migrations = load_migrations() if migrations is None else migrations
    latest = migrations[-1].version if migrations else 0
    if schema_version(conn) > latest:
        raise LedgerError("账本的版本比代码新，先升级代码")

    applied = []
    for migration in migrations:
        if migration.version <= schema_version(conn):
            continue
        rebuild = migration.name in _WITHOUT_FOREIGN_KEYS
        enforced = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        if rebuild and enforced:
            conn.execute("PRAGMA foreign_keys = OFF")  # 事务里改不了，只能在外面
        try:
            with transaction(conn):
                if migration.version <= schema_version(conn):
                    continue
                # 重建表的：只看这一次多出来的外键问题（账本里原来就有的不算在它头上）
                broken = set(map(tuple, conn.execute("PRAGMA foreign_key_check"))) if rebuild else set()
                conn.executescript(migration.sql)
                step = _PYTHON_STEPS.get(migration.name)
                if step is not None:
                    step(conn)
                if rebuild and set(map(tuple, conn.execute("PRAGMA foreign_key_check"))) - broken:
                    raise _ForeignKeyProblem
                conn.execute(f"PRAGMA user_version = {int(migration.version)}")
        except _ForeignKeyProblem:
            raise LedgerError(f"迁移 {migration.name} 重建表以后外键对不上，撤销了，账本没动") from None
        finally:
            if rebuild and enforced:
                conn.execute("PRAGMA foreign_keys = ON")
        applied.append(migration.version)
    return applied


def open_ledger(library: Library) -> sqlite3.Connection:
    """打开某个库的账本，没有就建，旧的就升到最新。"""
    conn = connect(library.db_path)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    """账本里的表，不算全文索引自己的内部表。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'raw_fts%' AND name NOT LIKE 'raw_norm_fts%' ORDER BY name"
    )
    return [row["name"] for row in rows]
