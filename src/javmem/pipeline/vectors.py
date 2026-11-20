"""向量缓存：按“模型、文字”存算好的向量，账本整本重建时不用再调向量服务。

一个 SQLite 文件 ``vectors.db``，默认在数据目录的 ``vectors/`` 下（真实内容的向量，放仓库外）。
键是 ``sha256("javmem-embedding\\n" + 模型 + "\\n" + 文字)``，同样的文字换个窗口、换条事实都用同一个向量。
缓存只是省事：删了它，下次写账按需重算；账本里每条事实自己的向量在 ``fact_vectors``。

撤回（J2.11）要清：账本里没有事实再用这段文字的，缓存里这一行也删掉（delete_unused）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from javmem.embedding import EMBEDDING_DIM
from javmem.timeutil import iso

VECTOR_FILE = "vectors.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    vector_key  TEXT PRIMARY KEY,
    model       TEXT    NOT NULL,
    text_sha256 TEXT    NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB    NOT NULL,
    created_at  TEXT    NOT NULL
);
"""


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def vector_key(model: str, text: str) -> str:
    return hashlib.sha256("\n".join(("javmem-embedding", model, text)).encode("utf-8")).hexdigest()


class VectorCache:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / VECTOR_FILE
        self._conn = sqlite3.connect(self.path, autocommit=True, timeout=30)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> VectorCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, model: str, texts: Iterable[str]) -> dict[str, bytes]:
        """有的那些：文字 → 向量。"""
        found = {}
        for text in dict.fromkeys(texts):
            row = self._conn.execute(
                "SELECT vector FROM vectors WHERE vector_key = ? AND dim = ?", (vector_key(model, text), EMBEDDING_DIM)
            ).fetchone()
            if row is not None:
                found[text] = row[0]
        return found

    def put(self, model: str, vectors: Mapping[str, bytes]) -> None:
        now = iso(datetime.now(timezone.utc))
        rows = [(vector_key(model, text), model, text_sha256(text), EMBEDDING_DIM, vector, now) for text, vector in vectors.items()]
        self._write("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?, ?, ?)", rows)

    def _write(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        """一个事务写完，出错全部撤销。连接是 autocommit 的，事务自己开。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(sql, rows)
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def count(self) -> int:
        return self._conn.execute("SELECT count(*) FROM vectors").fetchone()[0]

    def delete_unused(self, model: str, keep: Iterable[str]) -> int:
        """账本里没有事实再用的文字的向量删掉（撤回以后用）。keep 是还在用的事实文字。返回删了几行。"""
        wanted = {vector_key(model, text) for text in keep}
        rows = [row[0] for row in self._conn.execute("SELECT vector_key FROM vectors WHERE model = ?", (model,))]
        stale = [key for key in rows if key not in wanted]
        self._write("DELETE FROM vectors WHERE vector_key = ?", [(key,) for key in stale])
        return len(stale)

    def models(self) -> list[str]:
        return [row[0] for row in self._conn.execute("SELECT DISTINCT model FROM vectors ORDER BY model")]

    def vacuum(self) -> bool:
        """彻底删除以后：删掉的向量在文件的空页里还有，整理一遍，日志也清掉。有人正读着、整理不完，返回 False。"""
        try:
            self._conn.execute("VACUUM")
        except sqlite3.OperationalError:
            return False
        return self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
