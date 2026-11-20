"""主图连 Neo4j 的那一层（J5.1、J5.2）：连上、第一次改密码、核设置、防串库的标记、建约束和索引、一批批写和删、清空、导出。

只有这里 import neo4j（用到时才 import）。用同步的驱动；一批一个事务，一个事务里几句 Cypher 一起提交。
图里的名字都带 Jm 前缀（Graphiti 那边的名字是 Entity、RELATES_TO 这些，分开了一眼看得出是谁的）：

- 节点：JmEntity 实体、JmAlias 别名、JmFact 事（一条事实一个）、JmScope 可见范围、JmMarker 防串库的标记。
- 边：JM_INVOLVES 事 → 实体（role：subject、object、mention），JM_ALIAS_OF 别名 → 实体，JM_RELATION 实体 → 实体（七种关系
  都是这一种边，type 写关系名）。

和 javmem.graph.memory.MemoryGraph 是同一套方法（测试用那个，真图用这个）；sync 只认这套方法。
"""

from __future__ import annotations

import math
import secrets
import struct
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from javmem.embedding import EMBEDDING_DIM
from javmem.graph.config import GraphTarget
from javmem.timeutil import iso

MARKER = "JmMarker"
ENTITY = "JmEntity"
ALIAS = "JmAlias"
FACT = "JmFact"
SCOPE = "JmScope"
INVOLVES = "JM_INVOLVES"
ALIAS_OF = "JM_ALIAS_OF"
RELATION = "JM_RELATION"
KIND = "javmem-graph"
DATABASE = "neo4j"
DEFAULT_PASSWORD = "neo4j"
OUR_LABELS = frozenset({MARKER, ENTITY, ALIAS, FACT, SCOPE})
OUR_TYPES = frozenset({INVOLVES, ALIAS_OF, RELATION})

# 起来以后核的设置：往外报的关掉，全文索引默认 cjk
EXPECTED_CONFIG = {
    "dbms.usage_report.enabled": "false",
    "client.allow_telemetry": "false",
    "db.index.fulltext.default_analyzer": "cjk",
}
MEMORY_SETTINGS = ("server.memory.heap.max_size", "server.memory.pagecache.size", "dbms.memory.transaction.total.max")

# 编号唯一：同一个编号图里只有一个（按编号重复写不出错）
CONSTRAINTS = {
    "jm_marker_id": f"CREATE CONSTRAINT jm_marker_id IF NOT EXISTS FOR (n:{MARKER}) REQUIRE n.marker_id IS UNIQUE",
    "jm_entity_id": f"CREATE CONSTRAINT jm_entity_id IF NOT EXISTS FOR (n:{ENTITY}) REQUIRE n.id IS UNIQUE",
    "jm_alias_id": f"CREATE CONSTRAINT jm_alias_id IF NOT EXISTS FOR (n:{ALIAS}) REQUIRE n.id IS UNIQUE",
    "jm_fact_id": f"CREATE CONSTRAINT jm_fact_id IF NOT EXISTS FOR (n:{FACT}) REQUIRE n.id IS UNIQUE",
    "jm_scope_id": f"CREATE CONSTRAINT jm_scope_id IF NOT EXISTS FOR (n:{SCOPE}) REQUIRE n.id IS UNIQUE",
    "jm_relation_id": f"CREATE CONSTRAINT jm_relation_id IF NOT EXISTS FOR ()-[r:{RELATION}]-() REQUIRE r.id IS UNIQUE",
}
# 按值找（J5.4、J5.5）：别名的写法（按名字完全一样地找实体走别名：每个别名带着自己的范围）、实体类型、事的主体和时间、
# 关系名和状态、负责关系里“代表哪一家”（J5.4 检索按认出来的实体拉关系时，代表的是它的也算，不然整片扫）。
# 实体的标准名不按写法建索引（看不到标准名的范围也能按写法找到它：第二遍审阅第 1 条）
OBSOLETE_INDEXES = ("jm_entity_key",)  # 以前的版本建过、这一版不要了的：ensure_schema 删掉
RANGE_INDEXES = {
    "jm_alias_key": f"CREATE INDEX jm_alias_key IF NOT EXISTS FOR (n:{ALIAS}) ON (n.key)",
    "jm_entity_type": f"CREATE INDEX jm_entity_type IF NOT EXISTS FOR (n:{ENTITY}) ON (n.type)",
    "jm_fact_subject": f"CREATE INDEX jm_fact_subject IF NOT EXISTS FOR (n:{FACT}) ON (n.subject_id)",
    "jm_fact_sort": f"CREATE INDEX jm_fact_sort IF NOT EXISTS FOR (n:{FACT}) ON (n.sort_epoch)",
    "jm_relation_type": f"CREATE INDEX jm_relation_type IF NOT EXISTS FOR ()-[r:{RELATION}]-() ON (r.type)",
    "jm_relation_state": f"CREATE INDEX jm_relation_state IF NOT EXISTS FOR ()-[r:{RELATION}]-() ON (r.state)",
    "jm_relation_org": f"CREATE INDEX jm_relation_org IF NOT EXISTS FOR ()-[r:{RELATION}]-() ON (r.org_id)",
}
_CJK = "OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}}"
# 全文（cjk，两个字一组）：每个都连着范围编号（实体名连的是 name_scope_text），J5.4 可以在全文检索里面就只搜看得到的那几种
FULLTEXT_INDEXES = {
    "jm_alias_name": f"CREATE FULLTEXT INDEX jm_alias_name IF NOT EXISTS FOR (n:{ALIAS}) ON EACH [n.name, n.scope_text] {_CJK}",
    # 实体名只连着“标准名看得到的范围”（name_scope_text），不连实体的 scope_text：看不到标准名的范围搜不到它（审阅第 3 条）
    "jm_entity_name": f"CREATE FULLTEXT INDEX jm_entity_name IF NOT EXISTS FOR (n:{ENTITY}) ON EACH [n.name, n.name_scope_text] {_CJK}",
    "jm_fact_text": f"CREATE FULLTEXT INDEX jm_fact_text IF NOT EXISTS FOR (n:{FACT}) ON EACH [n.content, n.scope_text] {_CJK}",
    "jm_relation_text": (
        f"CREATE FULLTEXT INDEX jm_relation_text IF NOT EXISTS FOR ()-[r:{RELATION}]-() "
        f"ON EACH [r.content, r.details, r.scope_text] {_CJK}"
    ),
}
_VECTOR = "OPTIONS {indexConfig: {`vector.dimensions`: " + str(EMBEDDING_DIM) + ", `vector.similarity_function`: 'cosine'}}"
# 向量（Qwen3-Embedding-4B，2560 维，定了不改）：事一个、关系一个
VECTOR_INDEXES = {
    "jm_fact_vector": f"CREATE VECTOR INDEX jm_fact_vector IF NOT EXISTS FOR (n:{FACT}) ON (n.vector) {_VECTOR}",
    "jm_relation_vector": f"CREATE VECTOR INDEX jm_relation_vector IF NOT EXISTS FOR ()-[r:{RELATION}]-() ON (r.vector) {_VECTOR}",
}
SEARCH_INDEXES = {**FULLTEXT_INDEXES, **VECTOR_INDEXES}
# 每个全文、向量索引该连着哪几个属性：已经有的索引和这里对不上（以前的版本建的），ensure_schema 删了重建
SEARCH_PROPERTIES = {
    "jm_alias_name": ["name", "scope_text"],
    "jm_entity_name": ["name", "name_scope_text"],
    "jm_fact_text": ["content", "scope_text"],
    "jm_relation_text": ["content", "details", "scope_text"],
    "jm_fact_vector": ["vector"],
    "jm_relation_vector": ["vector"],
}


def stale_search_indexes(report: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """已经有、可是和这一版对不上的全文、向量索引（属性不一样、全文不是 cjk、向量不是 2560 维）。"""
    stale = []
    for name, want in SEARCH_PROPERTIES.items():
        info = report.get(name)
        if info is None:
            continue
        wrong = list(info.get("properties") or []) != want
        if name in FULLTEXT_INDEXES and info.get("analyzer") != "cjk":
            wrong = True
        if name in VECTOR_INDEXES and info.get("dimensions") != EMBEDDING_DIM:
            wrong = True
        if wrong:
            stale.append(name)
    return stale

_LABEL_OF = {"entity": ENTITY, "alias": ALIAS, "fact": FACT, "scope": SCOPE}


class GraphRefused(Exception):
    """防串库：这个图不是这本账本的（不空又没有标记、标记对不上、库对不上……），一个字不写。消息里没有内容。"""


class GraphUnavailable(Exception):
    """图连不上、不回话。欠着的留着，下次再同步。"""


class GraphAuthError(Exception):
    """账号、密码不对。"""


def vector_floats(blob: bytes) -> list[float]:
    """账本里的向量（float32 小端）换成 Neo4j 收的数。"""
    return list(struct.unpack(f"<{EMBEDDING_DIM}f", blob))


def vector_blob(values: Sequence[float]) -> bytes:
    """从图里读回来的向量换回 float32 小端（存的就是 float32，换回去和账本里的一个字节都不差）。"""
    return struct.pack(f"<{len(values)}f", *values)


def _driver_errors() -> tuple[type[BaseException], ...]:
    from neo4j.exceptions import ServiceUnavailable, SessionExpired

    return (ServiceUnavailable, SessionExpired, OSError)


def set_initial_password(target: GraphTarget, *, timeout: float | None = None) -> bool:
    """图第一次起来是默认账号、要先改密码：用 bolt 连上，改成环境变量里的那个（当查询参数传，不拼进语句）。
    改了返回 True；默认密码已经连不上（改过了）返回 False。改完再拿默认密码连一次，还连得上就报错。"""
    from neo4j import GraphDatabase
    from neo4j.exceptions import AuthError

    new = target.require_password()
    config = _timeouts(timeout)
    try:
        with GraphDatabase.driver(target.uri, auth=(target.user, DEFAULT_PASSWORD), **config) as driver:
            driver.execute_query("ALTER CURRENT USER SET PASSWORD FROM $old TO $new", old=DEFAULT_PASSWORD, new=new,
                                 database_="system")
    except AuthError:
        return False
    except _driver_errors() as error:
        raise GraphUnavailable(type(error).__name__) from None
    with GraphDatabase.driver(target.uri, auth=(target.user, DEFAULT_PASSWORD), **config) as driver:
        try:
            driver.execute_query("RETURN 1", database_=DATABASE)
        except AuthError:
            return True
    raise GraphRefused("改了密码以后默认密码还连得上")


def _timeouts(timeout: float | None) -> dict[str, object]:
    """驱动的设置：服务器的提示（标签还不存在、写法过时这类）不要，不然每句都往 stderr 打一大段；给了 timeout 的，
    连接、取连接、事务重试都最多等这么久。"""
    config: dict[str, object] = {"notifications_min_severity": "OFF"}
    if timeout is not None:
        config.update(connection_timeout=timeout, connection_acquisition_timeout=timeout, max_transaction_retry_time=timeout)
    return config


class NeoGraph:
    """一个 Neo4j 图。connect() 只连上；写东西以前要 check_marker()（init 用 create_marker()）。"""

    def __init__(self, target: GraphTarget, *, timeout: float | None = None) -> None:
        self.target = target
        self.timeout = timeout
        self.driver: Any = None

    # ---------- 连接 ----------

    def connect(self) -> None:
        from neo4j import GraphDatabase
        from neo4j.exceptions import AuthError

        try:
            self.driver = GraphDatabase.driver(
                self.target.uri, auth=(self.target.user, self.target.require_password()), **_timeouts(self.timeout)
            )
            self.driver.verify_connectivity()
        except AuthError:
            self.close()
            raise GraphAuthError(f"图的账号、密码不对（{self.target.password_name}）") from None
        except _driver_errors() as error:
            self.close()
            raise GraphUnavailable(type(error).__name__) from None

    def close(self) -> None:
        if self.driver is not None:
            self.driver.close()
            self.driver = None

    def __enter__(self) -> NeoGraph:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        try:
            result = self.driver.execute_query(cypher, parameters_=params, database_=DATABASE)
        except _driver_errors() as error:
            raise GraphUnavailable(type(error).__name__) from None
        return [record.data() for record in result.records]

    def _write(self, work: Callable[[Any], None]) -> None:
        """几句 Cypher 一个事务：一起提交，出错一起撤销（驱动遇到临时的错会整个重来，所以 work 要能重做）。"""
        try:
            with self.driver.session(database=DATABASE) as session:
                session.execute_write(work)
        except _driver_errors() as error:
            raise GraphUnavailable(type(error).__name__) from None

    def _auto(self, cypher: str, **params: Any) -> None:
        """CALL … IN TRANSACTIONS 要在自动提交的事务里跑。"""
        try:
            with self.driver.session(database=DATABASE) as session:
                session.run(cypher, params).consume()
        except _driver_errors() as error:
            raise GraphUnavailable(type(error).__name__) from None

    # ---------- 设置 ----------

    def config(self) -> dict[str, str]:
        try:
            rows = self.driver.execute_query("CALL dbms.listConfig() YIELD name, value RETURN name, value", database_="system")
        except _driver_errors() as error:
            raise GraphUnavailable(type(error).__name__) from None
        return {record["name"]: str(record["value"]) for record in rows.records}

    def analyzers(self) -> list[str]:
        rows = self.query("CALL db.index.fulltext.listAvailableAnalyzers() YIELD analyzer RETURN analyzer")
        return sorted(str(row["analyzer"]) for row in rows)

    # ---------- 防串库 ----------

    def markers(self) -> list[dict[str, Any]]:
        return self.query(f"MATCH (m:{MARKER}) RETURN properties(m) AS props")

    def has_other_content(self) -> bool:
        """图里有没有不是我们标记的东西（节点或者边）。"""
        found = self.query(f"MATCH (n) WHERE NOT n:{MARKER} RETURN 1 AS one LIMIT 1")
        return bool(found)

    def check_marker(self, marker_id: str | None, library: str) -> None:
        check_marker([row["props"] for row in self.markers()], self.has_other_content(), marker_id, library)

    def create_marker(self, library: str, projection: str) -> str:
        if self.markers() or self.has_other_content():
            raise GraphRefused("图不是空的：不放标记")
        marker_id = secrets.token_hex(16)
        self.query(
            f"CREATE (m:{MARKER} {{marker_id: $id, library: $library, kind: $kind, projection: $projection, created_at: $at}})",
            id=marker_id, library=library, kind=KIND, projection=projection, at=iso(_now()),
        )
        return marker_id

    def set_marker_projection(self, projection: str) -> None:
        self.query(f"MATCH (m:{MARKER}) SET m.projection = $projection", projection=projection)

    def remove_marker(self, marker_id: str) -> None:
        """init 放了标记、账本却没接上时，把刚放的拿掉（只拿这一个编号的）。"""
        self.query(f"MATCH (m:{MARKER} {{marker_id: $id}}) DELETE m", id=marker_id)

    # ---------- 约束和索引 ----------

    def ensure_schema(self, *, search: bool = True) -> None:
        """建约束和索引（有了的不重建）。search：全文、向量索引也建；和这一版对不上的先删了再建。"""
        for name in OBSOLETE_INDEXES:
            self.query(f"DROP INDEX {name} IF EXISTS")
        if search:
            for name in stale_search_indexes(self.index_report()):
                self.query(f"DROP INDEX {name} IF EXISTS")
        for statement in [*CONSTRAINTS.values(), *RANGE_INDEXES.values(), *(SEARCH_INDEXES.values() if search else ())]:
            self.query(statement)

    def drop_search_indexes(self) -> None:
        for name in SEARCH_INDEXES:
            self.query(f"DROP INDEX {name} IF EXISTS")

    def await_indexes(self, seconds: int = 3600) -> None:
        self.query("CALL db.awaitIndexes($seconds)", seconds=int(seconds))

    def index_report(self) -> dict[str, dict[str, Any]]:
        rows = self.query(
            "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, options, state "
            "RETURN name, type, entityType, labelsOrTypes, properties, options, state"
        )
        report = {}
        for row in rows:
            config = (row.get("options") or {}).get("indexConfig") or {}
            report[row["name"]] = {
                "type": row["type"],
                "entity": row["entityType"],
                "on": list(row["labelsOrTypes"] or []),
                "properties": list(row["properties"] or []),
                "state": row["state"],
                "analyzer": config.get("fulltext.analyzer"),
                "dimensions": config.get("vector.dimensions"),
                "similarity": config.get("vector.similarity_function"),
            }
        return report

    def constraint_names(self) -> list[str]:
        return sorted(str(row["name"]) for row in self.query("SHOW CONSTRAINTS YIELD name RETURN name"))

    # ---------- 写 ----------

    def upsert_scopes(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            self._write(lambda tx: tx.run(
                f"UNWIND $rows AS row MERGE (n:{SCOPE} {{id: row.id}}) SET n += row.props", rows=list(rows)).consume())

    def upsert_entities(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            self._write(lambda tx: tx.run(
                f"UNWIND $rows AS row MERGE (n:{ENTITY} {{id: row.id}}) SET n += row.props", rows=list(rows)).consume())

    def ensure_entities(self, ids: Sequence[str]) -> None:
        """事、别名、关系要挂上的实体先占个位（属性等实体那一轮再写）。"""
        if ids:
            self._write(lambda tx: tx.run(f"UNWIND $ids AS id MERGE (:{ENTITY} {{id: id}})", ids=sorted(set(ids))).consume())

    def upsert_aliases(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        rows = list(rows)

        def work(tx: Any) -> None:
            tx.run(f"UNWIND $rows AS row MERGE (n:{ALIAS} {{id: row.id}}) SET n += row.props", rows=rows).consume()
            tx.run(
                f"UNWIND $rows AS row MATCH (n:{ALIAS} {{id: row.id}})-[x:{ALIAS_OF}]->(e) WHERE e.id <> row.entity DELETE x",
                rows=rows,
            ).consume()
            tx.run(
                f"UNWIND $rows AS row MATCH (n:{ALIAS} {{id: row.id}}), (e:{ENTITY} {{id: row.entity}}) MERGE (n)-[:{ALIAS_OF}]->(e)",
                rows=rows,
            ).consume()

        self._write(work)

    def upsert_facts(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """rows 每一行：id、props、links（[{entity, role}]）、vector（float 列表；写成 None 是这一次不动向量）、
        drop_vector（True：把图里的向量去掉）。"""
        if not rows:
            return
        plain = [{"id": row["id"], "props": row["props"]} for row in rows]
        links = [{"fact": row["id"], "entity": link["entity"], "role": link["role"]} for row in rows for link in row["links"]]
        vectors = [{"id": row["id"], "vector": row["vector"]} for row in rows if row.get("vector") is not None]
        dropped = [row["id"] for row in rows if row.get("drop_vector")]

        def work(tx: Any) -> None:
            tx.run(f"UNWIND $rows AS row MERGE (n:{FACT} {{id: row.id}}) SET n += row.props", rows=plain).consume()
            tx.run(f"UNWIND $ids AS id MATCH (n:{FACT} {{id: id}})-[x:{INVOLVES}]->() DELETE x", ids=[row["id"] for row in plain]).consume()
            tx.run(  # MERGE：同一批里同一条事实写了两遍，也只挂一条
                f"UNWIND $links AS link MATCH (n:{FACT} {{id: link.fact}}), (e:{ENTITY} {{id: link.entity}}) "
                f"MERGE (n)-[:{INVOLVES} {{role: link.role}}]->(e)",
                links=links,
            ).consume()
            if vectors:
                tx.run(
                    f"UNWIND $rows AS row MATCH (n:{FACT} {{id: row.id}}) CALL db.create.setNodeVectorProperty(n, 'vector', row.vector)",
                    rows=vectors,
                ).consume()
            if dropped:
                tx.run(f"UNWIND $ids AS id MATCH (n:{FACT} {{id: id}}) REMOVE n.vector", ids=dropped).consume()

        self._write(work)

    def upsert_relations(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """rows 每一行：id、subject、object、props、vector、drop_vector（同 upsert_facts）。两头换了的（合并以后）先删了旧的边。"""
        if not rows:
            return
        plain = [{"id": row["id"], "subject": row["subject"], "object": row["object"], "props": row["props"]} for row in rows]
        vectors = [{"id": row["id"], "vector": row["vector"]} for row in rows if row.get("vector") is not None]
        dropped = [row["id"] for row in rows if row.get("drop_vector")]

        def work(tx: Any) -> None:
            tx.run(
                f"UNWIND $rows AS row MATCH (a)-[r:{RELATION} {{id: row.id}}]->(b) "
                "WHERE a.id <> row.subject OR b.id <> row.object DELETE r",
                rows=plain,
            ).consume()
            tx.run(
                f"UNWIND $rows AS row MATCH (a:{ENTITY} {{id: row.subject}}), (b:{ENTITY} {{id: row.object}}) "
                f"MERGE (a)-[r:{RELATION} {{id: row.id}}]->(b) SET r += row.props",
                rows=plain,
            ).consume()
            if vectors:
                tx.run(
                    f"UNWIND $rows AS row MATCH ()-[r:{RELATION} {{id: row.id}}]->() "
                    "CALL db.create.setRelationshipVectorProperty(r, 'vector', row.vector)",
                    rows=vectors,
                ).consume()
            if dropped:
                tx.run(f"UNWIND $ids AS id MATCH ()-[r:{RELATION} {{id: id}}]->() REMOVE r.vector", ids=dropped).consume()

        self._write(work)

    def delete(self, kind: str, ids: Sequence[str]) -> None:
        if not ids:
            return
        ids = sorted(set(ids))
        if kind == "relation":
            self._write(lambda tx: tx.run(f"UNWIND $ids AS id MATCH ()-[r:{RELATION} {{id: id}}]->() DELETE r", ids=ids).consume())
            return
        label = _LABEL_OF[kind]
        self._write(lambda tx: tx.run(f"UNWIND $ids AS id MATCH (n:{label} {{id: id}}) DETACH DELETE n", ids=ids).consume())

    def wipe(self, batch: int = 10_000) -> None:
        """除了标记全删：先一批批删边（一个实体可能连着几十万条），再一批批删节点，每一批一个事务，不超过事务内存的上限。"""
        self._auto(f"MATCH ()-[r]->() CALL (r) {{ DELETE r }} IN TRANSACTIONS OF {int(batch)} ROWS")
        self._auto(f"MATCH (n) WHERE NOT n:{MARKER} CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF {int(batch)} ROWS")

    # ---------- 读（导出、比对、测试） ----------

    def labels_and_types(self) -> tuple[dict[str, int], dict[str, int]]:
        """图里每种标签几个节点、每种边几条（连我们不认识的也数，“主图查不到支线”看这个）。"""
        labels = {str(row["label"]): int(row["n"]) for row in self.query(
            "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS n")}
        types = {str(row["type"]): int(row["n"]) for row in self.query("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n")}
        return labels, types

    def iter_items(self, kind: str, *, vectors: bool = True, page: int = 1000) -> Iterator[dict[str, Any]]:
        """按编号从小到大读出一种东西：{id, props, …}。props 里没有向量；vectors 是 True 时另带 vector（float 列表或 None）。"""
        after = ""
        while True:
            rows = self.query(_PAGE[kind] + (", n.vector AS vector" if vectors and kind == "fact" else "")
                              + (", r.vector AS vector" if vectors and kind == "relation" else "")
                              + " ORDER BY id LIMIT $page", after=after, page=page)
            if not rows:
                return
            for row in rows:
                props = dict(row["props"])
                props.pop("vector", None)
                item = {"id": row["id"], "props": props}
                for key in ("links", "entities", "subject", "object"):
                    if key in row:
                        item[key] = row[key]
                if vectors and kind in ("fact", "relation"):
                    item["vector"] = row.get("vector")
                yield item
            after = rows[-1]["id"]


_PAGE = {
    "entity": f"MATCH (n:{ENTITY}) WHERE n.id > $after RETURN n.id AS id, properties(n) AS props",
    "alias": f"MATCH (n:{ALIAS}) WHERE n.id > $after RETURN n.id AS id, properties(n) AS props, "
             f"[(n)-[:{ALIAS_OF}]->(e) | e.id] AS entities",
    "fact": f"MATCH (n:{FACT}) WHERE n.id > $after RETURN n.id AS id, n {{.*, vector: null}} AS props, "
            f"[(n)-[x:{INVOLVES}]->(e) | {{entity: e.id, role: x.role}}] AS links",
    "relation": f"MATCH (a)-[r:{RELATION}]->(b) WHERE r.id > $after RETURN r.id AS id, r {{.*, vector: null}} AS props, "
                "a.id AS subject, b.id AS object",
    "scope": f"MATCH (n:{SCOPE}) WHERE n.id > $after RETURN n.id AS id, properties(n) AS props",
}


def check_marker(markers: Sequence[Mapping[str, Any]], other_content: bool, marker_id: str | None, library: str) -> None:
    """防串库（真图、内存里的图共用）。一个字不写的几种情况都在这里。"""
    if len(markers) > 1:
        raise GraphRefused("图里有好几个 javmem 的标记：一个字不写")
    if not markers:
        if other_content:
            raise GraphRefused("图不是空的，又没有 javmem 主图的标记：一个字不写（是不是配到了别的 Neo4j？）")
        if marker_id:
            raise GraphRefused("图是空的，可账本记着上一个图：图整个重建过的话先 javmem graph init --reset")
        raise GraphRefused("图还没初始化：先跑 javmem graph init")
    marker = markers[0]
    if marker.get("kind") != KIND:
        raise GraphRefused("图里的标记不是 javmem 主图的：一个字不写")
    if marker.get("library") != library:
        raise GraphRefused("图的标记写的是另一个库（正式库、测试库互不相通）：一个字不写")
    if not marker_id or marker.get("marker_id") != marker_id:
        raise GraphRefused("图的标记和账本记的对不上（一个图只配一个数据目录的一个库）：一个字不写")


def _now() -> Any:
    from datetime import datetime

    return datetime.now().astimezone().replace(microsecond=0)


def finite_vector(values: Sequence[float] | None) -> bool:
    return values is not None and len(values) == EMBEDDING_DIM and all(math.isfinite(value) for value in values)
