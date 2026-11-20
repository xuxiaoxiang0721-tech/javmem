"""放在内存里的图（测试用）：和 javmem.graph.neo.NeoGraph 同一套方法、同样的规矩。

照真图的样子：按编号覆盖（同一个编号只有一个）；SET n += props 里写成 None 的属性去掉；挂边时另一头不在就不挂；删节点连边删；
向量存成 float32（读回来和账本里的一个字节都不差）；导出按编号从小到大。同一批测试用真图（有 Neo4j 时）再跑一遍，
两边得一样。
"""

from __future__ import annotations

import secrets
import struct
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from javmem.graph import neo
from javmem.timeutil import iso


def _float32(values: Sequence[float]) -> list[float]:
    return list(struct.unpack(f"<{len(values)}f", struct.pack(f"<{len(values)}f", *values)))


def _apply(target: dict[str, Any], props: Mapping[str, Any]) -> None:
    for key, value in props.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = list(value) if isinstance(value, (list, tuple)) else value


class MemoryGraph:
    def __init__(self, *, config: Mapping[str, str] | None = None, analyzers: Sequence[str] = ("cjk", "standard")) -> None:
        self.settings = dict(neo.EXPECTED_CONFIG if config is None else config)
        self.available = sorted(analyzers)
        self.marker_rows: list[dict[str, Any]] = []
        self.nodes: dict[str, dict[str, dict[str, Any]]] = {label: {} for label in (neo.ENTITY, neo.ALIAS, neo.FACT, neo.SCOPE)}
        self.vectors: dict[str, list[float]] = {}  # 事的向量
        self.involves: dict[str, set[tuple[str, str]]] = {}  # 事 → {(实体, 角色)}
        self.alias_of: dict[str, set[str]] = {}  # 别名 → {实体}
        self.relations: dict[str, dict[str, Any]] = {}  # 关系 → {subject, object, props, vector}
        self.foreign: dict[str, int] = {}  # 不是我们的标签（测试里装成别的 Neo4j）
        self.indexes: set[str] = set()
        self.connected = False
        self.writes = 0  # 写了几批（测试看“第二遍一条都没写”）
        self.fail_after: int | None = None  # 再写这么多批以后就连不上（测试断在半路）

    # ---------- 连接 ----------

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def __enter__(self) -> MemoryGraph:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _write(self) -> None:
        if self.fail_after is not None:
            if self.fail_after <= 0:
                raise neo.GraphUnavailable("MemoryGraph")
            self.fail_after -= 1
        self.writes += 1

    # ---------- 设置、标记 ----------

    def config(self) -> dict[str, str]:
        return dict(self.settings)

    def analyzers(self) -> list[str]:
        return list(self.available)

    def markers(self) -> list[dict[str, Any]]:
        return [{"props": dict(row)} for row in self.marker_rows]

    def has_other_content(self) -> bool:
        return any(self.nodes[label] for label in self.nodes) or bool(self.relations) or any(self.foreign.values())

    def check_marker(self, marker_id: str | None, library: str) -> None:
        neo.check_marker([dict(row) for row in self.marker_rows], self.has_other_content(), marker_id, library)

    def create_marker(self, library: str, projection: str) -> str:
        if self.marker_rows or self.has_other_content():
            raise neo.GraphRefused("图不是空的：不放标记")
        marker_id = secrets.token_hex(16)
        self.marker_rows.append({"marker_id": marker_id, "library": library, "kind": neo.KIND, "projection": projection,
                                 "created_at": iso(neo._now())})
        return marker_id

    def set_marker_projection(self, projection: str) -> None:
        for row in self.marker_rows:
            row["projection"] = projection

    def remove_marker(self, marker_id: str) -> None:
        self.marker_rows = [row for row in self.marker_rows if row["marker_id"] != marker_id]

    # ---------- 约束和索引 ----------

    def ensure_schema(self, *, search: bool = True) -> None:
        self.indexes |= set(neo.CONSTRAINTS) | set(neo.RANGE_INDEXES) | (set(neo.SEARCH_INDEXES) if search else set())

    def drop_search_indexes(self) -> None:
        self.indexes -= set(neo.SEARCH_INDEXES)

    def await_indexes(self, seconds: int = 3600) -> None:
        return None

    def index_report(self) -> dict[str, dict[str, Any]]:
        report = {}
        for name in sorted(self.indexes - set(neo.CONSTRAINTS)):
            report[name] = {
                "type": "FULLTEXT" if name in neo.FULLTEXT_INDEXES else "VECTOR" if name in neo.VECTOR_INDEXES else "RANGE",
                "state": "ONLINE",
                "analyzer": "cjk" if name in neo.FULLTEXT_INDEXES else None,
                "dimensions": neo.EMBEDDING_DIM if name in neo.VECTOR_INDEXES else None,
            }
        return report

    def constraint_names(self) -> list[str]:
        return sorted(self.indexes & set(neo.CONSTRAINTS))

    # ---------- 写 ----------

    def _merge(self, label: str, item_id: str) -> dict[str, Any]:
        return self.nodes[label].setdefault(item_id, {"id": item_id})

    def upsert_scopes(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            self._write()
            for row in rows:
                _apply(self._merge(neo.SCOPE, row["id"]), row["props"])

    def upsert_entities(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            self._write()
            for row in rows:
                _apply(self._merge(neo.ENTITY, row["id"]), row["props"])

    def ensure_entities(self, ids: Sequence[str]) -> None:
        if ids:
            self._write()
            for one in ids:
                self._merge(neo.ENTITY, one)

    def upsert_aliases(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        self._write()
        for row in rows:
            _apply(self._merge(neo.ALIAS, row["id"]), row["props"])
            edges = self.alias_of.setdefault(row["id"], set())
            edges.intersection_update({row["entity"]})
            if row["entity"] in self.nodes[neo.ENTITY]:
                edges.add(row["entity"])

    def upsert_facts(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        self._write()
        for row in rows:
            _apply(self._merge(neo.FACT, row["id"]), row["props"])
            self.involves[row["id"]] = {
                (link["entity"], link["role"]) for link in row["links"] if link["entity"] in self.nodes[neo.ENTITY]
            }
            if row.get("vector") is not None:
                self.vectors[row["id"]] = _float32(row["vector"])
            if row.get("drop_vector"):
                self.vectors.pop(row["id"], None)

    def upsert_relations(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        self._write()
        for row in rows:
            old = self.relations.get(row["id"])
            if old is not None and (old["subject"], old["object"]) != (row["subject"], row["object"]):
                del self.relations[row["id"]]
            if row["subject"] not in self.nodes[neo.ENTITY] or row["object"] not in self.nodes[neo.ENTITY]:
                continue
            edge = self.relations.setdefault(row["id"], {"subject": row["subject"], "object": row["object"],
                                                          "props": {"id": row["id"]}, "vector": None})
            _apply(edge["props"], row["props"])
            if row.get("vector") is not None:
                edge["vector"] = _float32(row["vector"])
            if row.get("drop_vector"):
                edge["vector"] = None

    def delete(self, kind: str, ids: Sequence[str]) -> None:
        if not ids:
            return
        self._write()
        for one in ids:
            if kind == "relation":
                self.relations.pop(one, None)
            elif kind == "fact":
                self.nodes[neo.FACT].pop(one, None)
                self.involves.pop(one, None)
                self.vectors.pop(one, None)
            elif kind == "alias":
                self.nodes[neo.ALIAS].pop(one, None)
                self.alias_of.pop(one, None)
            elif kind == "scope":
                self.nodes[neo.SCOPE].pop(one, None)
            elif kind == "entity":
                self.nodes[neo.ENTITY].pop(one, None)
                for edges in self.involves.values():
                    edges.difference_update({pair for pair in edges if pair[0] == one})
                for edges in self.alias_of.values():
                    edges.discard(one)
                for rid in [rid for rid, edge in self.relations.items() if one in (edge["subject"], edge["object"])]:
                    del self.relations[rid]
            else:
                raise ValueError(kind)

    def wipe(self, batch: int = 10_000) -> None:
        self._write()
        for label in self.nodes:
            self.nodes[label].clear()
        self.vectors.clear()
        self.involves.clear()
        self.alias_of.clear()
        self.relations.clear()
        self.foreign.clear()

    # ---------- 读 ----------

    def labels_and_types(self) -> tuple[dict[str, int], dict[str, int]]:
        labels = {label: len(rows) for label, rows in self.nodes.items() if rows}
        if self.marker_rows:
            labels[neo.MARKER] = len(self.marker_rows)
        labels.update({label: count for label, count in self.foreign.items() if count})
        types = {}
        involves = sum(len(edges) for edges in self.involves.values())
        alias_of = sum(len(edges) for edges in self.alias_of.values())
        for name, count in ((neo.INVOLVES, involves), (neo.ALIAS_OF, alias_of), (neo.RELATION, len(self.relations))):
            if count:
                types[name] = count
        return labels, types

    def iter_items(self, kind: str, *, vectors: bool = True, page: int = 1000) -> Iterator[dict[str, Any]]:
        if kind == "relation":
            for rid in sorted(self.relations):
                edge = self.relations[rid]
                item = {"id": rid, "props": dict(edge["props"]), "subject": edge["subject"], "object": edge["object"]}
                if vectors:
                    item["vector"] = edge["vector"]
                yield item
            return
        label = {"entity": neo.ENTITY, "alias": neo.ALIAS, "fact": neo.FACT, "scope": neo.SCOPE}[kind]
        for one in sorted(self.nodes[label]):
            item: dict[str, Any] = {"id": one, "props": dict(self.nodes[label][one])}
            if kind == "fact":
                item["links"] = [{"entity": entity, "role": role} for entity, role in sorted(self.involves.get(one, set()))]
                if vectors:
                    item["vector"] = self.vectors.get(one)
            if kind == "alias":
                item["entities"] = sorted(self.alias_of.get(one, set()))
            yield item
