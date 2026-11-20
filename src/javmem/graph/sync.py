"""同步进图（J5.2）：把账本里欠着的同步进图，或者整张重建；导出、比对。

一次同步：

1. 核图里的标记（防串库），核过了才写。
2. 把“第几轮”加一（G → G+1），以后的改动都记成新的一轮；只同步 G 以前欠着的。
3. 读出欠着的，往外扩：事实连带它的实体（可见范围、在不在图里会变）；关系连带两头；别名连带它的实体；名字可能变了的实体
   （它自己、它的别名、合并跳转、歧义变了）连带它的关系（内容里有名字）和别名；包登记变了连带出处在它里面的别名。
   扩出来的也记成欠着（第 G 轮），断在半路下次照样补。
4. 事、关系、别名一批一批：照 javmem.graph.project 算出“该是什么样”（事、关系过一道进图前检查），算指纹，和上次写进图的
   （graph_items）一样的跳过，变了的写进图，不该在了的删掉；每一批在图里提交了，接着在账本里记下新的指纹、数数（实体、
   范围连着几条），删掉这一批第 G 轮以前欠着的。
5. 实体一批一批：连着它的事、关系、别名数出来大于 0 的才在图里，可见范围就是那几个；再是可见范围的节点。
6. 记下同步完的时间。整张同步（第一次、`--all`、图的样子或检查换了版本）把账本里每一条都算一遍；整张重建先把图清空，
   装完再建全文、向量索引。

同一个库同时只有一个同步在跑（graph_lock）；同步的这段时间里账本又改了的是第 G+1 轮记的，留着下次。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Protocol

from javmem.embedding import EmbeddingError, Embedder, EmbedStats, RecordCall, embed_all
from javmem.graph import dirty
from javmem.graph.gate import AdmitAll, Gate, GateItem, GateStats, Verdict
from javmem.graph.neo import ALIAS_OF, ENTITY, INVOLVES, MARKER, RELATION, SCOPE, ALIAS, FACT, vector_blob, vector_floats
from javmem.graph.project import CHUNK, PROJECTION_VERSION, Item, Projector, chunks, scope_text, vector_sha
from javmem.ledger import transaction
from javmem.pipeline.vectors import VectorCache
from javmem.timeutil import iso

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover
    import fcntl

ITEM_KINDS = ("fact", "relation", "alias")  # 先写的三样；实体、范围按它们算


class GraphNotSetUp(Exception):
    """这本账还没接上图（javmem graph init 以前）。"""


class GraphBusy(Exception):
    """这个库已经有一个同步在跑。"""


class Graph(Protocol):
    """同步要的那几样（javmem.graph.neo.NeoGraph、javmem.graph.memory.MemoryGraph 都是）。"""

    def check_marker(self, marker_id: str | None, library: str) -> None: ...
    def set_marker_projection(self, projection: str) -> None: ...
    def ensure_schema(self, *, search: bool = True) -> None: ...
    def drop_search_indexes(self) -> None: ...
    def await_indexes(self, seconds: int = 3600) -> None: ...
    def upsert_scopes(self, rows: Sequence[Mapping[str, Any]]) -> None: ...
    def upsert_entities(self, rows: Sequence[Mapping[str, Any]]) -> None: ...
    def ensure_entities(self, ids: Sequence[str]) -> None: ...
    def upsert_aliases(self, rows: Sequence[Mapping[str, Any]]) -> None: ...
    def upsert_facts(self, rows: Sequence[Mapping[str, Any]]) -> None: ...
    def upsert_relations(self, rows: Sequence[Mapping[str, Any]]) -> None: ...
    def delete(self, kind: str, ids: Sequence[str]) -> None: ...
    def wipe(self, batch: int = 10_000) -> None: ...
    def labels_and_types(self) -> tuple[dict[str, int], dict[str, int]]: ...
    def iter_items(self, kind: str, *, vectors: bool = True, page: int = 1000) -> Iterator[dict[str, Any]]: ...


# ---------- 账本这边的状态 ----------


@dataclass(frozen=True)
class GraphState:
    enabled: bool
    marker_id: str | None
    gen: int
    triggers: str | None
    projection: str | None
    gate: str | None
    synced_at: str | None
    rebuilt_at: str | None


def read_state(conn: sqlite3.Connection) -> GraphState:
    row = conn.execute(
        "SELECT enabled, marker_id, gen, triggers, projection, gate, synced_at, rebuilt_at FROM graph_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return GraphState(False, None, 1, None, None, None, None, None)
    return GraphState(bool(row[0]), row[1], int(row[2]), row[3], row[4], row[5], row[6], row[7])


def enable(conn: sqlite3.Connection, marker_id: str) -> None:
    """接上一个新图（javmem graph init）：记下标记、装上记欠账的触发器，清掉以前那个图的指纹、数数、欠着的
    （下一次同步是整张的）。一个事务：装了一半不算。"""
    with transaction(conn):
        conn.execute("DELETE FROM graph_items")
        conn.execute("DELETE FROM graph_entity_scopes")
        conn.execute("DELETE FROM graph_scopes")
        conn.execute("DELETE FROM graph_dirty")
        conn.execute(
            "UPDATE graph_state SET enabled = 1, marker_id = ?, projection = NULL, gate = NULL, synced_at = NULL,"
            " rebuilt_at = NULL WHERE id = 1",
            (marker_id,),
        )
        dirty.install_triggers(conn)


def forget(conn: sqlite3.Connection) -> None:
    """javmem graph init --reset：图整个重建过（空的、没有标记），让账本忘了上一个图，拆掉记欠账的触发器。"""
    with transaction(conn):
        dirty.drop_triggers(conn)
        conn.execute("DELETE FROM graph_items")
        conn.execute("DELETE FROM graph_entity_scopes")
        conn.execute("DELETE FROM graph_scopes")
        conn.execute("DELETE FROM graph_dirty")
        conn.execute(
            "UPDATE graph_state SET enabled = 0, marker_id = NULL, triggers = NULL, projection = NULL, gate = NULL,"
            " synced_at = NULL, rebuilt_at = NULL WHERE id = 1"
        )


@contextmanager
def graph_lock(data_dir: Path, library: str, *, wait: float = 0.0,
               sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    """同一个库同时只有一个同步在跑。拿不到就是别人在同步：默认不等（欠着的下次补）；wait 是最多等几秒，每秒看一次
    （管理命令接着同步时等一会儿，J5.6）。锁跟着进程走，进程死了系统会放开。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / f".graph-{library}.lock", "a+b") as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise GraphBusy("这个库已经有一个同步在跑") from None
                sleep(min(1.0, left))
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _reading(conn: sqlite3.Connection) -> Iterator[None]:
    """一段读：放在一个读事务里，看到的是同一个时刻的账本。"""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")


# ---------- 关系的向量 ----------


class RelationVectors:
    """关系的向量：先查写账用的同一个向量缓存，没有的调向量服务（一次一个请求、一批不超过 16 条），算完进缓存。
    没给向量服务的照 model（设置的模型名）只查缓存；缓存里没有、服务调不通的，这条关系先不带向量进图，欠着，下次再补。
    broken：这一次向量服务调不通了，不再调。"""

    def __init__(self, cache: VectorCache | None, embedder: Embedder | None, record: RecordCall | None = None, *,
                 model: str | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cache, self.embedder, self.record, self.sleep = cache, embedder, record, sleep
        self.stats = EmbedStats()
        self.broken = None
        self.model_name = model

    @property
    def model(self) -> str | None:
        """关系的向量是哪个模型算的：写进每条关系（vector_model），换了模型指纹跟着变、照新模型重算（审阅第 7 条）。"""
        if self.embedder is not None:
            return self.embedder.name
        return self.model_name

    def fill(self, items: Sequence[Item]) -> None:
        wanted = sorted({item.vector_text for item in items if item.vector_text})
        model = self.model
        if not wanted or model is None:
            return
        found = self.cache.get(model, wanted) if self.cache is not None else {}
        missing = [text for text in wanted if text not in found]
        if missing and self.embedder is not None and self.broken is None:

            def keep(done: dict[str, bytes]) -> None:
                if self.cache is not None:
                    self.cache.put(model, done)
                found.update(done)

            try:
                embed_all(missing, self.embedder, record=self.record, sleep=self.sleep, stats=self.stats, store=keep)
            except EmbeddingError as error:
                self.broken = str(error)
        for item in items:
            if item.vector_text:
                item.vector = found.get(item.vector_text)


# ---------- 汇总 ----------


@dataclass
class SyncReport:
    # ok、not_set_up、disabled、no_password、no_driver、unavailable、refused、auth、busy、config、error、full_needed；
    # 管理命令接着同步（J5.6，graph.hook.after_admin）还有 idle（没有要同步的）、untracked（记欠账的触发器不全）、
    # unclean（上一回整张同步断在半路）
    status: str = "ok"
    message: str = ""
    mode: str = "incremental"  # incremental 一点点；all 整张算一遍；rebuild 清空重来
    seeds: int = 0  # 读到的欠着的
    work: dict[str, int] = field(default_factory=dict)  # 扩完以后每一样算了几条
    written: Counter[str] = field(default_factory=Counter)
    deleted: Counter[str] = field(default_factory=Counter)
    unchanged: Counter[str] = field(default_factory=Counter)
    gate: GateStats = field(default_factory=GateStats)
    gate_version: str = ""
    vectors_embedded: int = 0  # 关系的向量这一次调服务算的
    vectors_owed: int = 0  # 关系的向量没补上、欠着的
    owed_after: int = 0  # 同步完还欠着的（这一次又改了的、没补上的）
    owed_since: str | None = None
    full_owed: str | None = None  # 该整张同步、这一次只同步了欠着的（管理命令接着同步，J5.6）：为什么该整张
    library: str = ""  # 哪个库（提示里写命令用）
    seconds: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "message": self.message,
            "mode": self.mode,
            "seeds": self.seeds,
            "work": dict(self.work),
            "written": dict(sorted(self.written.items())),
            "deleted": dict(sorted(self.deleted.items())),
            "unchanged": dict(sorted(self.unchanged.items())),
            "gate": {"version": self.gate_version, **self.gate.describe()},
            "vectors_embedded": self.vectors_embedded,
            "vectors_owed": self.vectors_owed,
            "owed_after": self.owed_after,
            "owed_since": self.owed_since,
            "full_owed": self.full_owed,
            "seconds": {key: round(value, 3) for key, value in self.seconds.items()},
        }

    def text(self) -> str:
        if self.status != "ok":
            if self.status in ("untracked", "unclean"):  # 下一次管理命令照样不同步：要跑的命令已经写在 message 里
                owed = f"，欠着 {self.owed_after} 条" if self.owed_after else ""
            else:
                owed = f"，欠着 {self.owed_after} 条，下次同步补上" if self.owed_after else ""
            return f"主图：{self.message}{owed}"
        names = (("fact", "事"), ("relation", "关系"), ("alias", "别名"), ("entity", "实体"), ("scope", "范围"))
        written = "、".join(f"{label} {self.written[kind]}" for kind, label in names if self.written[kind]) or "0 条"
        deleted = sum(self.deleted.values())
        unchanged = sum(self.unchanged.values())
        mode = {"incremental": "同步", "all": "整张同步", "rebuild": "整张重建"}[self.mode]
        parts = [f"主图：{mode}，写了 {written}"]
        if deleted:
            parts.append(f"删了 {deleted} 条")
        if unchanged:
            parts.append(f"没变 {unchanged} 条")
        parts.append(f"检查：{self.gate.text()}")
        if self.vectors_owed:
            parts.append(f"关系的向量欠着 {self.vectors_owed} 条")
        parts.append(f"还欠着 {self.owed_after} 条")
        total = self.seconds.get("total")
        if total is not None:
            parts.append(f"用了 {total:.1f} 秒")
        text = "，".join(parts)
        if self.full_owed is not None:
            command = f"javmem graph sync --library {self.library}" if self.library else "javmem graph sync"
            text += f"；只同步了这次改到的，还欠一次整张同步：{self.full_owed}，跑 {command}"
        return text


# ---------- 同步 ----------


@dataclass
class _Old:
    fingerprint: str
    vector_sha: str | None
    links: tuple[str, ...]
    scopes: tuple[str, ...]


def _old_rows(conn: sqlite3.Connection, kind: str, ids: Sequence[str]) -> dict[str, _Old]:
    found = {}
    for part in chunks(sorted(set(ids))):
        for row in conn.execute(
            f"SELECT item_id, fingerprint, vector_sha, links, scopes FROM graph_items WHERE kind = ? AND item_id IN"
            f" ({', '.join('?' * len(part))})",
            [kind, *part],
        ):
            found[row[0]] = _Old(row[1], row[2], tuple(json.loads(row[3])), tuple(json.loads(row[4])))
    return found


def _graph_ids(conn: sqlite3.Connection, kind: str) -> list[str]:
    return [row[0] for row in conn.execute("SELECT item_id FROM graph_items WHERE kind = ? ORDER BY item_id", (kind,))]


@dataclass
class _Run:
    conn: sqlite3.Connection
    graph: Graph
    library: str
    gate: Gate
    vectors: RelationVectors | None
    report: SyncReport
    gen: int
    projector: Projector
    log: Callable[[str], None] | None
    chunk: int
    touched: set[str] = field(default_factory=set)  # 连着的实体有变动的：实体那一轮要重算
    owed: set[tuple[str, str]] = field(default_factory=set)
    scope_infos: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)

    def say(self, text: str) -> None:
        if self.log is not None:
            self.log(text)

    # ---------- 事、关系、别名 ----------

    def items(self, kind: str, ids: Sequence[str]) -> None:
        started = time.perf_counter()
        done = 0
        total = len(ids)
        for part in chunks(sorted(set(ids)), self.chunk):
            with _reading(self.conn):
                projected = getattr(self.projector, {"fact": "facts", "relation": "relations", "alias": "aliases"}[kind])(part)
                old = _old_rows(self.conn, kind, part)
            for info in self.projector.scopes.values():
                self.scope_infos.setdefault(info.scope_id, (list(info.partitions), list(info.sensitive)))
            if kind == "relation":  # 关系的向量是哪个模型算的：换了模型，指纹跟着变（审阅第 7 条）
                for item in projected.values():
                    item.props["vector_model"] = self.vectors.model if self.vectors is not None else None
            if kind in ("fact", "relation") and projected:
                gate_items = [
                    GateItem(kind, item_id, tuple(item.props["partitions"]), tuple(item.props["sensitive"]),  # type: ignore[arg-type]
                             dict(item.props), item.fingerprint())
                    for item_id, item in sorted(projected.items())
                ]
                verdicts = self.report.gate.add(gate_items, self.gate.admit(self.conn, self.library, gate_items))
                for key, verdict in verdicts.items():
                    if verdict is not Verdict.ADMIT:
                        projected.pop(key[1], None)
                        if verdict is Verdict.LATER:
                            self.owed.add(key)
            need: set[str] = set()  # 关系：这一次要（重新）配向量的；别的关系的向量照旧
            if kind == "relation" and self.vectors is not None:
                need = {item_id for item_id, item in projected.items()
                        if not (item_id in old and old[item_id].fingerprint == item.fingerprint(self.gate.version)
                                and old[item_id].vector_sha is not None)}
                embedded = self.vectors.stats.texts
                self.vectors.fill([projected[item_id] for item_id in sorted(need)])
                self.report.vectors_embedded += self.vectors.stats.texts - embedded
            self._apply(kind, part, projected, old, need)
            done += len(part)
            if total > self.chunk:
                rate = done / max(time.perf_counter() - started, 1e-6)
                self.say(f"  {kind}：{done}/{total}（每秒 {rate:.0f} 条）")
        self.report.seconds[kind] = self.report.seconds.get(kind, 0.0) + time.perf_counter() - started

    def _apply(self, kind: str, part: Sequence[str], projected: Mapping[str, Item], old: Mapping[str, _Old],
               need: set[str]) -> None:
        upserts: list[tuple[Item, str, str | None]] = []
        deletes: list[str] = []
        for item_id in part:
            item = projected.get(item_id)
            before = old.get(item_id)
            if item is None:
                if before is not None:
                    deletes.append(item_id)
                continue
            fingerprint = item.fingerprint(self.gate.version)
            if kind == "relation" and item_id not in need:
                new_vector = before.vector_sha if before is not None else None  # 向量照旧（字段没变、向量早就有）
            else:
                new_vector = vector_sha(item.vector)
            if kind == "relation" and self.vectors is not None and item_id in need and item.vector is None:
                self.owed.add((kind, item_id))  # 向量没补上：先不带向量进图，欠着，下次再补
                self.report.vectors_owed += 1
            if before is not None and before.fingerprint == fingerprint and before.vector_sha == new_vector:
                self.report.unchanged[kind] += 1
                continue
            upserts.append((item, fingerprint, new_vector))
        if upserts:
            entities = sorted({entity for item, _, _ in upserts for entity in item.links})
            self._hold_places(kind, [item.item_id for item, _, _ in upserts if item.item_id not in old], entities)
            self.graph.ensure_entities(entities)
            rows = [self._row(kind, item, old.get(item.item_id), new_vector) for item, _, new_vector in upserts]
            if kind == "fact":
                self.graph.upsert_facts(rows)
            elif kind == "relation":
                self.graph.upsert_relations(rows)
            else:
                self.graph.upsert_aliases(rows)
        if deletes:
            self.graph.delete(kind, deletes)
        deltas: Counter[tuple[str, str]] = Counter()
        scope_deltas: Counter[str] = Counter()
        for item_id in deletes:
            before = old[item_id]
            self._count(deltas, scope_deltas, before.links, before.scopes, -1)
        for item, _, _ in upserts:
            before = old.get(item.item_id)
            if before is not None:
                self._count(deltas, scope_deltas, before.links, before.scopes, -1)
            self._count(deltas, scope_deltas, item.links, item.scopes, +1)
        with transaction(self.conn):
            if deletes:
                self.conn.executemany("DELETE FROM graph_items WHERE kind = ? AND item_id = ?", [(kind, one) for one in deletes])
            if upserts:
                self.conn.executemany(
                    "INSERT INTO graph_items (kind, item_id, fingerprint, vector_sha, links, scopes) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (kind, item_id) DO UPDATE SET fingerprint = excluded.fingerprint,"
                    " vector_sha = excluded.vector_sha, links = excluded.links, scopes = excluded.scopes",
                    [(kind, item.item_id, fingerprint, new_vector, json.dumps(list(item.links)), json.dumps(list(item.scopes)))
                     for item, fingerprint, new_vector in upserts],
                )
            self._apply_counts(deltas, scope_deltas)
            self._settle(kind, part)
        self.report.written[kind] += len(upserts)
        self.report.deleted[kind] += len(deletes)

    def _hold_places(self, kind: str, new_ids: Sequence[str], entities: Sequence[str] = ()) -> None:
        """写图以前先在账本记一笔“图里可能有它”（指纹是空的、连着的和范围都是空的）：图里写进去了、回话丢了（账本没
        记上），下一次同步照样认得出它，账本里没了就删、变了就重写（审阅第 1 条）。事、关系挂上去的实体也是（占位的节点）。
        已经在 graph_items 里的不动：它原来的指纹、连着的、范围还要拿来算这一次的增减。"""
        rows = [(kind, one) for one in new_ids] + [("entity", one) for one in entities]
        if not rows:
            return
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT INTO graph_items (kind, item_id, fingerprint) VALUES (?, ?, '') ON CONFLICT (kind, item_id) DO NOTHING",
                rows,
            )

    def _row(self, kind: str, item: Item, before: _Old | None, new_vector: str | None) -> dict[str, Any]:
        row: dict[str, Any] = {"id": item.item_id, "props": item.props}
        # 占位的那一行（指纹是空的）：上一回写进没写进、带没带向量都不知道，有就发、没有就删（第二遍审阅第 2 条）
        unknown = before is not None and before.fingerprint == ""
        send = item.vector is not None and (before is None or unknown or before.vector_sha != new_vector)
        row["vector"] = vector_floats(item.vector) if send and item.vector is not None else None
        row["drop_vector"] = new_vector is None and before is not None and (unknown or before.vector_sha is not None)
        if kind == "fact":
            row["links"] = [{"entity": entity, "role": role} for entity, role in item.involves]
        elif kind == "relation":
            row["subject"], row["object"] = item.subject, item.object
        else:
            row["entity"] = item.entity
        return row

    def _count(self, deltas: Counter[tuple[str, str]], scope_deltas: Counter[str], links: Sequence[str],
               scopes: Sequence[str], sign: int) -> None:
        for entity in set(links):
            self.touched.add(entity)
            for scope in scopes:
                deltas[(entity, scope)] += sign
        for scope in scopes:
            scope_deltas[scope] += sign

    def _apply_counts(self, deltas: Mapping[tuple[str, str], int], scope_deltas: Mapping[str, int]) -> None:
        """数数的两张表加减；减到 0 的那一行删掉（实体、范围不在图里了）。"""
        for (entity, scope), delta in sorted(deltas.items()):
            if delta > 0:
                self.conn.execute(
                    "INSERT INTO graph_entity_scopes (entity_id, scope_id, n) VALUES (?, ?, ?)"
                    " ON CONFLICT (entity_id, scope_id) DO UPDATE SET n = n + excluded.n",
                    (entity, scope, delta),
                )
            elif delta < 0:
                self.conn.execute("DELETE FROM graph_entity_scopes WHERE entity_id = ? AND scope_id = ? AND n + ? <= 0",
                                  (entity, scope, delta))
                self.conn.execute("UPDATE graph_entity_scopes SET n = n + ? WHERE entity_id = ? AND scope_id = ?",
                                  (delta, entity, scope))
        for scope, delta in sorted(scope_deltas.items()):
            if delta == 0:
                continue
            if delta > 0:
                partitions, sensitive = self.scope_infos.get(scope, ([], []))
                self.conn.execute(
                    "INSERT INTO graph_scopes (scope_id, partitions, sensitive, n) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (scope_id) DO UPDATE SET n = n + excluded.n",
                    (scope, json.dumps(partitions, ensure_ascii=False), json.dumps(sensitive, ensure_ascii=False), delta),
                )
            else:  # 先删减到 0 的，再减剩下的（反过来的话，减完再按减过的数判断会多删）
                self.conn.execute("DELETE FROM graph_scopes WHERE scope_id = ? AND n + ? <= 0", (scope, delta))
                self.conn.execute("UPDATE graph_scopes SET n = n + ? WHERE scope_id = ?", (delta, scope))

    def _settle(self, kind: str, part: Sequence[str]) -> None:
        """这一批算完了：第 G 轮以前欠着的删掉；没补上的（关系的向量、这次没判的）记成新的一轮，留着下次。"""
        owed = [one for one in part if (kind, one) in self.owed]
        done = [one for one in part if (kind, one) not in self.owed]
        self.conn.executemany("DELETE FROM graph_dirty WHERE kind = ? AND item_id = ? AND gen <= ?",
                              [(kind, one, self.gen) for one in done])
        self.conn.executemany(
            "INSERT INTO graph_dirty (kind, item_id, gen) VALUES (?, ?, ?)"
            " ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen",
            [(kind, one, self.gen + 1) for one in owed],
        )

    # ---------- 实体、范围 ----------

    def entities(self, ids: Sequence[str]) -> None:
        started = time.perf_counter()
        for part in chunks(sorted(set(ids)), self.chunk):
            with _reading(self.conn):
                base = self.projector.entities(part)
                scopes: dict[str, list[str]] = {}
                for row in self.conn.execute(
                    f"SELECT entity_id, scope_id FROM graph_entity_scopes WHERE entity_id IN ({', '.join('?' * len(part))})"
                    " ORDER BY entity_id, scope_id",
                    part,
                ):
                    scopes.setdefault(row[0], []).append(row[1])
                old = _old_rows(self.conn, "entity", part)
            upserts: list[tuple[Item, str]] = []
            deletes: list[str] = []
            for entity_id in part:
                here = scopes.get(entity_id)
                if entity_id not in base or not here:
                    deletes.append(entity_id)  # 不在 graph_items 里的也删（断在半路时占的位）：图里没有就什么都不做
                    continue
                props = {**base[entity_id], "scope_ids": list(here), "scope_text": scope_text(here)}
                item = Item("entity", entity_id, props, scopes=tuple(here))
                fingerprint = item.fingerprint()
                before = old.get(entity_id)
                if before is not None and before.fingerprint == fingerprint:
                    self.report.unchanged["entity"] += 1
                    continue
                upserts.append((item, fingerprint))
            if upserts:
                self.graph.upsert_entities([{"id": item.item_id, "props": item.props} for item, _ in upserts])
            if deletes:
                self.graph.delete("entity", deletes)
            with transaction(self.conn):
                gone = [one for one in deletes if one in old]
                if gone:
                    self.conn.executemany("DELETE FROM graph_items WHERE kind = 'entity' AND item_id = ?", [(one,) for one in gone])
                if upserts:
                    self.conn.executemany(
                        "INSERT INTO graph_items (kind, item_id, fingerprint, scopes) VALUES ('entity', ?, ?, ?)"
                        " ON CONFLICT (kind, item_id) DO UPDATE SET fingerprint = excluded.fingerprint, scopes = excluded.scopes",
                        [(item.item_id, fingerprint, json.dumps(list(item.scopes))) for item, fingerprint in upserts],
                    )
                self._settle("entity", part)
            self.report.written["entity"] += len(upserts)
            self.report.deleted["entity"] += sum(1 for one in deletes if one in old)
        self.report.seconds["entity"] = self.report.seconds.get("entity", 0.0) + time.perf_counter() - started

    def scopes(self) -> None:
        wanted = {row[0]: {"partitions": json.loads(row[1]), "sensitive": json.loads(row[2])}
                  for row in self.conn.execute("SELECT scope_id, partitions, sensitive FROM graph_scopes")}
        old = _old_rows(self.conn, "scope", _graph_ids(self.conn, "scope"))
        upserts = []
        for scope_id, props in sorted(wanted.items()):
            item = Item("scope", scope_id, props)
            fingerprint = item.fingerprint()
            if scope_id in old and old[scope_id].fingerprint == fingerprint:
                self.report.unchanged["scope"] += 1
                continue
            upserts.append((item, fingerprint))
        deletes = sorted(set(old) - set(wanted))
        if upserts:
            self._hold_places("scope", [item.item_id for item, _ in upserts if item.item_id not in old])
            self.graph.upsert_scopes([{"id": item.item_id, "props": item.props} for item, _ in upserts])
        if deletes:
            self.graph.delete("scope", deletes)
        with transaction(self.conn):
            self.conn.executemany("DELETE FROM graph_items WHERE kind = 'scope' AND item_id = ?", [(one,) for one in deletes])
            self.conn.executemany(
                "INSERT INTO graph_items (kind, item_id, fingerprint) VALUES ('scope', ?, ?)"
                " ON CONFLICT (kind, item_id) DO UPDATE SET fingerprint = excluded.fingerprint",
                [(item.item_id, fingerprint) for item, fingerprint in upserts],
            )
        self.report.written["scope"] += len(upserts)
        self.report.deleted["scope"] += len(deletes)


def _expand(run: _Run) -> dict[str, set[str]]:
    """欠着的往外扩：见模块说明第 3 步。"""
    conn, projector = run.conn, run.projector
    seeds: dict[str, set[str]] = {kind: set() for kind in ("entity", "alias", "fact", "relation", "packet")}
    with _reading(conn):
        for row in conn.execute("SELECT kind, item_id FROM graph_dirty WHERE gen <= ?", (run.gen,)):
            seeds[row[0]].add(row[1])
        run.report.seeds = sum(len(ids) for ids in seeds.values())
        facts, relations = set(seeds["fact"]), set(seeds["relation"])
        aliases = set(seeds["alias"]) | projector.aliases_of_packets(sorted(seeds["packet"]))
        named = set(seeds["entity"])
        named |= _links(conn, "alias", aliases) | projector.entities_of("alias", sorted(aliases))
        touching_relations, touching_aliases = projector.touching(sorted(named))
        relations |= touching_relations
        aliases |= touching_aliases
        entities = set(named)
        for kind, ids in (("fact", facts), ("relation", relations), ("alias", aliases)):
            entities |= _links(conn, kind, ids) | projector.entities_of(kind, sorted(ids))
    return {"fact": facts, "relation": relations, "alias": aliases, "entity": entities, "packet": seeds["packet"]}


def _links(conn: sqlite3.Connection, kind: str, ids: set[str]) -> set[str]:
    found: set[str] = set()
    for row in _old_rows(conn, kind, sorted(ids)).values():
        found.update(row.links)
    return found


def projection_key(tz: tzinfo, vector_model: str | None = None) -> str:
    """图的样子：规矩的版本，加上时区（照精度换成的起止时刻跟着时区变：审阅第 8 条）和关系的向量用哪个模型（换了模型
    要照新模型重算：审阅第 7 条）。和上一次整张同步时的不一样，就整张算一遍。"""
    offsets = []
    for month in (1, 7):
        offset = datetime(2026, month, 1, 12, tzinfo=tz).utcoffset()
        offsets.append(int(offset.total_seconds()) if offset is not None else 0)
    return f"{PROJECTION_VERSION}@{offsets[0]}/{offsets[1]}#{vector_model or '-'}"


def needs_full(conn: sqlite3.Connection, gate_version: str, tz: tzinfo, vector_model: str | None = None) -> str | None:
    """这一次同步该不该整张算一遍：该的话说为什么，不该是 None。写完账接着同步碰上该整张的不做（交给 javmem graph sync）。"""
    state = read_state(conn)
    if state.rebuilt_at is None:
        return "还没整张同步过，或者上一回整张的没做完"
    if state.projection != projection_key(tz, vector_model):
        return "图的样子换了版本（或者换了时区、向量模型）"
    if state.gate != gate_version:
        return "进图前检查换了版本"
    if state.triggers != dirty.TRIGGERS_VERSION or not dirty.triggers_ok(conn):
        return "记欠账的触发器不全或者是旧的"
    return None


def sync(
    conn: sqlite3.Connection,
    graph: Graph,
    *,
    library: str,
    tz: tzinfo,
    gate: Gate | None = None,
    vectors: RelationVectors | None = None,
    mode: str = "incremental",
    log: Callable[[str], None] | None = None,
    chunk: int = CHUNK,
    index_wait: int = 3600,
    allow_full: bool = True,
) -> SyncReport:
    """同步一次（见模块说明）。mode：incremental 只算欠着的（该整张的时候自己改成整张）；all 账本里每一条都算一遍；
    rebuild 先把图清空再整张装。conn 是这个库的账本（已经升到最新），graph 连好了的。
    allow_full=False（管理命令接着同步，J5.6）：该整张的时候也只算欠着的，照现在的样子写；整张同步照旧欠着
    （needs_full 下次照样说为什么，report.full_owed 写着），触发器也不换。
    防串库对不上抛 javmem.graph.neo.GraphRefused；图连不上抛 GraphUnavailable，欠着的都留着。"""
    if mode not in ("incremental", "all", "rebuild"):
        raise ValueError(mode)
    gate = gate if gate is not None else AdmitAll()
    report = SyncReport(gate_version=gate.version, library=library)
    started = time.perf_counter()
    state = read_state(conn)
    if not state.enabled:
        raise GraphNotSetUp("这本账还没接上图：先跑 javmem graph init")
    graph.check_marker(state.marker_id, library)
    vector_model = vectors.model if vectors is not None else None
    key = projection_key(tz, vector_model)
    stale_triggers = state.triggers != dirty.TRIGGERS_VERSION or not dirty.triggers_ok(conn)
    pending_full = needs_full(conn, gate.version, tz, vector_model)
    full = mode != "incremental" or (allow_full and pending_full is not None)
    if not full and pending_full is not None:
        report.full_owed = pending_full
    with transaction(conn):
        if stale_triggers and full:  # 装着的触发器是旧的、少了几个、没装上：换成这一版，这一遍整张的（漏记了的照样补上）
            dirty.install_triggers(conn)
        gen = int(conn.execute("SELECT gen FROM graph_state WHERE id = 1").fetchone()[0])
        conn.execute("UPDATE graph_state SET gen = gen + 1" + (", rebuilt_at = NULL" if full else "") + " WHERE id = 1")
    empty = conn.execute("SELECT 1 FROM graph_items LIMIT 1").fetchone() is None
    rebuild = mode == "rebuild" or (full and empty)
    report.mode = "rebuild" if rebuild else "all" if full else "incremental"
    run = _Run(conn, graph, library, gate, vectors, report, gen, Projector(conn, tz), log, chunk)
    if rebuild:
        moment = time.perf_counter()
        run.say("清空图……")
        # 先清账本这边记的，再清图：反过来的话断在中间，图空了、账本却记着“都在”，以后按指纹全当没变（审阅第 2 条）。
        # 这样断在中间的，下一次同步看 graph_items 是空的，照样整张重建
        with transaction(conn):
            conn.execute("DELETE FROM graph_items")
            conn.execute("DELETE FROM graph_entity_scopes")
            conn.execute("DELETE FROM graph_scopes")
        graph.wipe()
        graph.drop_search_indexes()
        report.seconds["wipe"] = time.perf_counter() - moment
    if full:
        with _reading(conn):
            work = {kind: set(run.projector.all_ids(kind)) | set(_graph_ids(conn, kind)) for kind in (*ITEM_KINDS, "entity")}
    else:
        work = _expand(run)
        with transaction(conn):  # 扩出来的也记成欠着（第 G 轮）：断在半路，下次照样补
            conn.executemany(
                "INSERT INTO graph_dirty (kind, item_id, gen) VALUES (?, ?, ?) ON CONFLICT (kind, item_id) DO NOTHING",
                [(kind, one, gen) for kind in (*ITEM_KINDS, "entity") for one in sorted(work[kind])],
            )
    report.work = {kind: len(work[kind]) for kind in (*ITEM_KINDS, "entity")}
    run.say("算：" + "、".join(f"{kind} {count}" for kind, count in report.work.items()))
    for kind in ITEM_KINDS:
        run.items(kind, sorted(work[kind]))
    run.entities(sorted(work["entity"] | run.touched))
    run.scopes()
    moment = time.perf_counter()
    if full:
        run.say("建全文、向量索引……")
    graph.ensure_schema(search=True)  # 每一遍都补：没有的建上、和这一版对不上的重建（断了的重建会删了它们：审阅第 4 条）
    if full:
        graph.await_indexes(index_wait)
        report.seconds["indexes"] = time.perf_counter() - moment
    now = iso(datetime.now().astimezone().replace(microsecond=0))
    with transaction(conn):
        if full:
            conn.execute("DELETE FROM graph_dirty WHERE gen <= ?", (gen,))
            conn.execute("UPDATE graph_state SET synced_at = ?, rebuilt_at = ?, projection = ?, gate = ? WHERE id = 1",
                         (now, now, key, gate.version))
        else:
            conn.execute("DELETE FROM graph_dirty WHERE kind = 'packet' AND gen <= ?", (gen,))
            conn.execute("UPDATE graph_state SET synced_at = ? WHERE id = 1", (now,))
    if full:
        graph.set_marker_projection(key)
    report.owed_after = int(conn.execute("SELECT count(*) FROM graph_dirty").fetchone()[0])
    report.owed_since = conn.execute("SELECT min(since) FROM graph_dirty").fetchone()[0]
    report.seconds["total"] = time.perf_counter() - started
    return report


# ---------- 导出、比对 ----------


def _clean(props: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in sorted(props.items()) if key not in ("id", "vector") and value is not None}


def _vector_sha(values: Sequence[float] | None) -> str | None:
    return hashlib.sha256(vector_blob(values)).hexdigest() if values else None


def export_lines(graph: Graph, *, vectors: bool = True) -> Iterator[str]:
    """图按编号排好的样子，一行一条（JSON），向量写成 sha256。第一行是每种标签、每种边各几个（连我们不认识的也数）。"""
    labels, types = graph.labels_and_types()
    labels.pop(MARKER, None)
    yield json.dumps({"kind": "_counts", "labels": dict(sorted(labels.items())), "types": dict(sorted(types.items()))},
                     ensure_ascii=False, sort_keys=True)
    for kind in ("scope", "entity", "alias", "fact", "relation"):
        for item in graph.iter_items(kind, vectors=vectors):
            line: dict[str, Any] = {"kind": kind, "id": item["id"], "props": _clean(item["props"])}
            if kind == "fact":
                line["links"] = sorted([link["entity"], link["role"]] for link in item.get("links", []))
            if kind == "alias":
                line["entities"] = sorted(item.get("entities", []))
            if kind == "relation":
                line["subject"], line["object"] = item["subject"], item["object"]
            if vectors and kind in ("fact", "relation"):
                line["vector"] = _vector_sha(item.get("vector"))
            yield json.dumps(line, ensure_ascii=False, sort_keys=True)


def export(graph: Graph, out: Path | None = None, *, vectors: bool = True) -> tuple[str, dict[str, int]]:
    """导出（out 给了就写进文件），返回整份的摘要和每一样几条。"""
    digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    handle = out.open("w", encoding="utf-8", newline="\n") if out is not None else None
    try:
        for line in export_lines(graph, vectors=vectors):
            digest.update(line.encode("utf-8") + b"\n")
            counts[json.loads(line)["kind"]] += 1
            if handle is not None:
                handle.write(line + "\n")
    finally:
        if handle is not None:
            handle.close()
    counts.pop("_counts", None)
    return digest.hexdigest(), dict(counts)


def expected_lines(conn: sqlite3.Connection, *, tz: tzinfo, gate: Gate | None = None,
                   vectors: RelationVectors | None = None, library: str = "") -> Iterator[str]:
    """账本照现在的样子“该有的图”，写成和 export_lines 一样的行（javmem graph check 拿两边逐行比）。
    实体、范围这里从头数一遍（不看同步记的数），和同步那边一点点数出来的互相核。关系的向量只查缓存，不调服务；
    缓存里没有的写成 "?"，比的时候这一样不算。"""
    gate = gate if gate is not None else AdmitAll()
    projector = Projector(conn, tz)
    kept: dict[str, dict[str, Item]] = {}
    with _reading(conn):
        for kind in ITEM_KINDS:
            method = {"fact": projector.facts, "relation": projector.relations, "alias": projector.aliases}[kind]
            kept[kind] = method(projector.all_ids(kind))
    for item in kept["relation"].values():  # 和同步一样：关系带上向量是哪个模型算的
        item.props["vector_model"] = vectors.model if vectors is not None else None
    for kind in ("fact", "relation"):  # 检查放在读事务外面交（和同步一样：J5.3 的检查自己可能要写留痕）
        found = kept[kind]
        if found:
            items = [GateItem(kind, item_id, tuple(item.props["partitions"]), tuple(item.props["sensitive"]),  # type: ignore[arg-type]
                              dict(item.props), item.fingerprint()) for item_id, item in sorted(found.items())]
            result = gate.admit(conn, library, items)
            kept[kind] = {key: item for key, item in found.items()
                          if result.verdicts.get((kind, key), Verdict.LATER) is Verdict.ADMIT}
    with _reading(conn):
        entity_scopes: dict[str, set[str]] = {}
        scope_seen: set[str] = set()
        for found in kept.values():
            for item in found.values():
                scope_seen.update(item.scopes)
                for entity in item.links:
                    entity_scopes.setdefault(entity, set()).update(item.scopes)
        base = projector.entities(sorted(entity_scopes))
    if vectors is not None:  # 缓存里有的照缓存；check 给的是只查缓存的（假向量照算，不连服务）
        vectors.fill(list(kept["relation"].values()))
    entities = {one: props for one, props in base.items() if entity_scopes.get(one)}
    involves = sum(len(item.involves) for item in kept["fact"].values())
    alias_of = sum(1 for item in kept["alias"].values() if item.entity in entities)
    labels = {ENTITY: len(entities), ALIAS: len(kept["alias"]), FACT: len(kept["fact"]), SCOPE: len(scope_seen)}
    types = {INVOLVES: involves, ALIAS_OF: alias_of, RELATION: len(kept["relation"])}
    yield json.dumps({"kind": "_counts", "labels": {k: v for k, v in sorted(labels.items()) if v},
                      "types": {k: v for k, v in sorted(types.items()) if v}}, ensure_ascii=False, sort_keys=True)
    for scope_id in sorted(scope_seen):
        info = projector.scopes[scope_id]
        yield json.dumps({"kind": "scope", "id": scope_id,
                          "props": _clean({"partitions": list(info.partitions), "sensitive": list(info.sensitive)})},
                         ensure_ascii=False, sort_keys=True)
    for entity_id in sorted(entities):
        here = sorted(entity_scopes[entity_id])
        props = {**entities[entity_id], "scope_ids": here, "scope_text": scope_text(here)}
        yield json.dumps({"kind": "entity", "id": entity_id, "props": _clean(props)}, ensure_ascii=False, sort_keys=True)
    for alias_id, item in sorted(kept["alias"].items()):
        yield json.dumps({"kind": "alias", "id": alias_id, "props": _clean(item.props),
                          "entities": [item.entity] if item.entity in entities else []}, ensure_ascii=False, sort_keys=True)
    for fact_id, item in sorted(kept["fact"].items()):
        yield json.dumps({"kind": "fact", "id": fact_id, "props": _clean(item.props),
                          "links": sorted([entity, role] for entity, role in item.involves),
                          "vector": vector_sha(item.vector)}, ensure_ascii=False, sort_keys=True)
    for relation_id, item in sorted(kept["relation"].items()):
        yield json.dumps({"kind": "relation", "id": relation_id, "props": _clean(item.props), "subject": item.subject,
                          "object": item.object, "vector": vector_sha(item.vector) if item.vector else "?"},
                         ensure_ascii=False, sort_keys=True)


def compare(graph_lines: Iterator[str], expected: Iterator[str]) -> dict[str, list[str]]:
    """两边逐条比：只在图里的、只在账本里的、两边不一样的（只列编号）。关系的向量写成 "?" 的不比。"""
    def keyed(lines: Iterator[str]) -> dict[tuple[str, str], dict[str, Any]]:
        found = {}
        for line in lines:
            row = json.loads(line)
            found[(row["kind"], row.get("id", ""))] = row
        return found

    have, want = keyed(graph_lines), keyed(expected)
    only_graph = sorted(f"{kind}:{one}" for kind, one in set(have) - set(want))
    only_ledger = sorted(f"{kind}:{one}" for kind, one in set(want) - set(have))
    different = []
    for key in sorted(set(have) & set(want)):
        a, b = dict(have[key]), dict(want[key])
        if b.get("vector") == "?":
            a.pop("vector", None)
            b.pop("vector", None)
        if a != b:
            different.append(f"{key[0]}:{key[1]}")
    return {"only_graph": only_graph, "only_ledger": only_ledger, "different": different}
