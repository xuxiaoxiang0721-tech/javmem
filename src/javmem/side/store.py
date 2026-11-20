"""支线自己的库 side.db：一个数据目录的一个库一份，放在 <数据目录>/side/<库>/。

记着：配的是哪个体检库（标记编号）、每一组（包、版本、可见范围、跑到哪了）、每个窗口（发没发、结果、错在哪一步、
episode 的编号）、每个窗口里哪一行是哪句原文（写进窗口的文字，打过码；对证据用）、每次调模型的用量（不记内容）、
闸拒绝过的连接、删过的组（只记包号和数）。

- 一组整组删的时候，这一组的窗口、行（原文）跟着删；调用的用量、删过的记录留着（没有内容）。同一个组名后来重跑的，
  每包的成本只算这一组这一回开始以后的调用（GroupRow.started_at）。
- 体检库连不上、这一组又要删（撤回了）：先把这一组的行（原文）删掉，组留着、记一笔待清，等体检库起来再整组删。
- 跳过的窗口（private、打码没打干净……）只记句子编号，行的文字是空的。
- 支线跑的时候拿自己的锁（.side.lock），撤回（J6.4）也拿这把锁：跑到一半撤回了的，等跑完再删。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - 只在 Windows 上跑
    import fcntl

from javmem.side.episodes import Episode

SCHEMA_VERSION = 1
GROUP_STATES = ("running", "partial", "done")
WINDOW_STATES = ("todo", "done", "failed", "skipped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups (
    group_id    TEXT PRIMARY KEY,
    packet_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    partitions  TEXT NOT NULL,
    sensitive   TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('running', 'partial', 'done')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (packet_id, revision)
);
CREATE TABLE IF NOT EXISTS windows (
    window_id      TEXT PRIMARY KEY,
    group_id       TEXT NOT NULL REFERENCES groups (group_id) ON DELETE CASCADE,
    number         INTEGER NOT NULL,
    text_sha       TEXT,
    status         TEXT NOT NULL CHECK (status IN ('todo', 'done', 'failed', 'skipped')),
    reason         TEXT,
    step           TEXT,
    episode_uuid   TEXT,
    reference_time TEXT NOT NULL,
    seconds        REAL,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS windows_by_group ON windows (group_id, number);
CREATE TABLE IF NOT EXISTS lines (
    window_id   TEXT NOT NULL REFERENCES windows (window_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    ref         TEXT NOT NULL,
    sentence_id TEXT NOT NULL,
    block       INTEGER NOT NULL,
    text        TEXT NOT NULL,
    PRIMARY KEY (window_id, seq)
);
CREATE INDEX IF NOT EXISTS lines_by_sentence ON lines (sentence_id);
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    group_id      TEXT NOT NULL,
    window_id     TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    status        TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    seconds       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_by_group ON calls (group_id);
CREATE TABLE IF NOT EXISTS pending_clears (
    group_id  TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL,
    revision  INTEGER NOT NULL,
    reason    TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cleared (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    group_id  TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    revision  INTEGER NOT NULL,
    reason    TEXT NOT NULL,
    nodes     INTEGER NOT NULL,
    edges     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS blocked (
    id    INTEGER PRIMARY KEY,
    at    TEXT NOT NULL,
    host  TEXT NOT NULL,
    port  INTEGER,
    layer TEXT NOT NULL
);
"""


class SideBusy(Exception):
    """这个库的支线已经有一个命令在跑。"""


class SideStoreError(Exception):
    """side.db 不是支线的库，或者版本不对。"""


def now_text() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def side_dir(data_dir: Path, library: str) -> Path:
    return data_dir / "side" / library


@contextmanager
def side_lock(directory: Path) -> Iterator[None]:
    """同一个库的支线同时只有一个命令在跑。锁跟着进程走，进程死了系统会放开。"""
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".side.lock", "a+b") as handle:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SideBusy("这个库的支线已经有一个命令在跑") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class GroupRow:
    group_id: str
    packet_id: str
    revision: int
    partitions: tuple[str, ...]
    sensitive: tuple[str, ...]
    state: str
    started_at: str = ""  # 这一组这一回开始的时候（整组删了重来的是新的一回）


@dataclass(frozen=True)
class WindowRow:
    window_id: str
    group_id: str
    number: int
    text_sha: str | None
    status: str
    reason: str | None
    step: str | None
    episode_uuid: str | None
    reference_time: str
    seconds: float | None


@dataclass(frozen=True)
class LineRow:
    window_id: str
    seq: int
    ref: str
    sentence_id: str
    block: int
    text: str


class SideStore:
    def __init__(self, path: Path, library: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.library = library
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA secure_delete = ON")  # 删了的原文抹成 0：撤回、彻底删除以后不留在文件里（scrub 再收紧）
        self.conn.executescript(_SCHEMA)
        version = self.meta("schema")
        if version is None:
            self.set_meta("schema", str(SCHEMA_VERSION))
            self.set_meta("library", library)
        elif version != str(SCHEMA_VERSION):
            raise SideStoreError("side.db 的版本不对")
        if self.meta("library") != library:
            raise SideStoreError("side.db 配的不是这个库")

    @classmethod
    def open(cls, data_dir: Path, library: str) -> SideStore:
        return cls(side_dir(data_dir, library) / "side.db", library)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> SideStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- meta ----------

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                          (key, value))

    # ---------- 组 ----------

    def groups(self) -> list[GroupRow]:
        rows = self.conn.execute("SELECT * FROM groups ORDER BY packet_id, revision").fetchall()
        return [_group(row) for row in rows]

    def group(self, group_id: str) -> GroupRow | None:
        row = self.conn.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        return _group(row) if row else None

    def start_group(self, episode: Episode) -> GroupRow:
        """开始（或接着）跑一组。已经有的不动它的窗口。"""
        found = self.group(episode.group_id)
        if found is not None:
            self.conn.execute("UPDATE groups SET state = 'running', finished_at = NULL WHERE group_id = ?",
                              (episode.group_id,))
            return self.group(episode.group_id) or found
        self.conn.execute(
            "INSERT INTO groups (group_id, packet_id, revision, partitions, sensitive, state, started_at)"
            " VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (episode.group_id, episode.packet_id, episode.revision, json.dumps(list(episode.scope.partitions)),
             json.dumps(list(episode.scope.sensitive)), now_text()),
        )
        return self.group(episode.group_id)  # type: ignore[return-value]

    def finish_group(self, group_id: str) -> str:
        """窗口都有结果了（done、failed、skipped）是 done，不然 partial。"""
        left = self.conn.execute(
            "SELECT count(*) FROM windows WHERE group_id = ? AND status = 'todo'", (group_id,)
        ).fetchone()[0]
        state = "partial" if left else "done"
        self.conn.execute("UPDATE groups SET state = ?, finished_at = ? WHERE group_id = ?", (state, now_text(), group_id))
        return state

    def drop_group(self, group_id: str, reason: str, nodes: int, edges: int) -> None:
        """体检库里这一组删干净以后，支线的库里也删（窗口、行跟着删），留一条删过的记录（不带内容）。"""
        found = self.group(group_id)
        if found is None:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
            self.conn.execute("DELETE FROM pending_clears WHERE group_id = ?", (group_id,))
            self.conn.execute(
                "INSERT INTO cleared (at, group_id, packet_id, revision, reason, nodes, edges) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_text(), group_id, found.packet_id, found.revision, reason, nodes, edges),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def purge_text(self, group_id: str) -> int:
        """体检库连不上时先删这一组的原文（行），组和窗口留着等体检库起来整组删。返回删了几行。"""
        return self.conn.execute(
            "DELETE FROM lines WHERE window_id IN (SELECT window_id FROM windows WHERE group_id = ?)", (group_id,)
        ).rowcount

    def note_cleared(self, group_id: str, reason: str, nodes: int, edges: int) -> None:
        """体检库里删了支线的库里没有的组（跑到一半进程没了这类），只记一笔。"""
        self.conn.execute(
            "INSERT INTO cleared (at, group_id, packet_id, revision, reason, nodes, edges) VALUES (?, ?, '', 0, ?, ?, ?)",
            (now_text(), group_id, reason, nodes, edges),
        )

    def mark_pending_clear(self, group: GroupRow, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO pending_clears (group_id, packet_id, revision, reason, at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (group_id) DO UPDATE SET reason = excluded.reason, at = excluded.at",
            (group.group_id, group.packet_id, group.revision, reason, now_text()),
        )

    def reset(self) -> int:
        """体检库整个重建以后（停了容器、删了它的数据目录重起）：支线的库里的组、窗口、行、用量、待清都清掉，
        标记编号也清掉，下次 init 放新的。删过的记录留着。返回清掉了几组。"""
        count = self.conn.execute("SELECT count(*) FROM groups").fetchone()[0]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("lines", "windows", "groups", "calls", "pending_clears"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute("DELETE FROM meta WHERE key = 'marker_id'")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.scrub()
        return int(count)

    def scrub(self) -> None:
        """删了的东西不留在文件里（彻底删除以后、reset 以后）：secure_delete 删的时候已经抹成 0，这里再整个重写一遍
        （VACUUM，没有空页），把 WAL 写回去截掉。"""
        self.conn.execute("VACUUM")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def pending_clears(self) -> list[tuple[str, str]]:
        return [(row["group_id"], row["reason"]) for row in self.conn.execute("SELECT * FROM pending_clears ORDER BY at")]

    # ---------- 窗口 ----------

    def windows(self, group_id: str) -> list[WindowRow]:
        rows = self.conn.execute("SELECT * FROM windows WHERE group_id = ? ORDER BY number", (group_id,)).fetchall()
        return [_window(row) for row in rows]

    def window(self, window_id: str) -> WindowRow | None:
        row = self.conn.execute("SELECT * FROM windows WHERE window_id = ?", (window_id,)).fetchone()
        return _window(row) if row else None

    def put_window(self, episode: Episode) -> WindowRow:
        """记下一个窗口（还没跑）。跳过的直接记成 skipped，只记句子编号、不存文字。已经有的不动。
        窗口编号里没有版本：同一个编号还记在别的组（上一版）里的，报错，先扫一遍把上一版删了。"""
        found = self.window(episode.window_id)
        if found is not None:
            if found.group_id != episode.group_id:
                raise SideStoreError("这个窗口还记在上一版的组里：先 javmem side sweep")
            return found
        status = "skipped" if episode.skip else "todo"
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO windows (window_id, group_id, number, text_sha, status, reason, reference_time)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (episode.window_id, episode.group_id, episode.number, episode.sha256 or None, status, episode.skip,
                 episode.reference_time.isoformat()),
            )
            self.conn.executemany(
                "INSERT INTO lines (window_id, seq, ref, sentence_id, block, text) VALUES (?, ?, ?, ?, ?, ?)",
                [(episode.window_id, seq, line.ref, line.sentence_id, line.block, "" if episode.skip else line.text)
                 for seq, line in enumerate(episode.lines)],
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.window(episode.window_id)  # type: ignore[return-value]

    def finish_window(self, window_id: str, status: str, *, episode_uuid: str | None = None, reason: str | None = None,
                      step: str | None = None, seconds: float | None = None) -> None:
        if status not in WINDOW_STATES:
            raise ValueError(status)
        self.conn.execute(
            "UPDATE windows SET status = ?, episode_uuid = ?, reason = ?, step = ?, seconds = ?, finished_at = ?"
            " WHERE window_id = ?",
            (status, episode_uuid, reason, step, seconds, now_text(), window_id),
        )

    def reset_window(self, window_id: str) -> None:
        """抽不成的窗口重来（--retry-failed）。"""
        self.conn.execute(
            "UPDATE windows SET status = 'todo', reason = NULL, step = NULL, episode_uuid = NULL, seconds = NULL,"
            " finished_at = NULL WHERE window_id = ? AND status = 'failed'",
            (window_id,),
        )

    def lines(self, window_id: str) -> list[LineRow]:
        rows = self.conn.execute("SELECT * FROM lines WHERE window_id = ? ORDER BY seq", (window_id,)).fetchall()
        return [LineRow(row["window_id"], row["seq"], row["ref"], row["sentence_id"], row["block"], row["text"]) for row in rows]

    def group_sentences(self, group_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT l.sentence_id FROM lines AS l JOIN windows AS w ON w.window_id = l.window_id WHERE w.group_id = ?",
            (group_id,),
        )
        return {row[0] for row in rows}

    # ---------- 用量、闸 ----------

    def record_call(self, *, group_id: str, window_id: str, prompt: str, attempt: int, status: str, input_tokens: int,
                    output_tokens: int, cached_tokens: int, seconds: float) -> None:
        self.conn.execute(
            "INSERT INTO calls (at, group_id, window_id, prompt, attempt, status, input_tokens, output_tokens, cached_tokens,"
            " seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_text(), group_id, window_id, prompt, attempt, status, input_tokens, output_tokens, cached_tokens,
             round(seconds, 3)),
        )

    def calls(self, group_ids: Iterable[str] | None = None) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM calls ORDER BY id").fetchall()
        if group_ids is None:
            return rows
        wanted = set(group_ids)
        return [row for row in rows if row["group_id"] in wanted]

    def record_blocked(self, at: str, host: str, port: int | None, layer: str) -> None:
        self.conn.execute("INSERT INTO blocked (at, host, port, layer) VALUES (?, ?, ?, ?)", (at, host, port, layer))

    def blocked(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM blocked ORDER BY id").fetchall()

    def cleared(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM cleared ORDER BY id").fetchall()


def _group(row: sqlite3.Row) -> GroupRow:
    return GroupRow(row["group_id"], row["packet_id"], row["revision"], tuple(json.loads(row["partitions"])),
                    tuple(json.loads(row["sensitive"])), row["state"], row["started_at"])


def _window(row: sqlite3.Row) -> WindowRow:
    return WindowRow(row["window_id"], row["group_id"], row["number"], row["text_sha"], row["status"], row["reason"],
                     row["step"], row["episode_uuid"], row["reference_time"], row["seconds"])
