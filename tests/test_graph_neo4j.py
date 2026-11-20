"""主图连真 Neo4j 的那一层（J5.1、J5.2）。只有给了测试用的图才跑，没给整个跳过（CI 的 ubuntu 那一路起一个）：

    JAVMEM_TEST_NEO4J_URI=bolt://127.0.0.1:端口   JAVMEM_TEST_NEO4J_PASSWORD=<那个图的密码，没改过的图就是要改成的那个>

测试开头会清掉 JAVMEM_*（conftest），所以这两个在 import 的时候就读下来。只清“空的，或者标记是测试自己放的”的图：
接着真数据的图（标记不是测试放的）整个跳过，一个字不碰。名字和句子都是编的。
"""

from __future__ import annotations

import os
import random
from collections.abc import Iterator

import pytest
from helpers import World
from test_graph import PACKET_IDS, PACKETS, assert_matches_ledger, digest, ledger, rich_extract, run_sync, staged
from test_write import day_packet, deliver, l3_fact, write

from javmem.cli import main
from javmem.graph import neo
from javmem.graph.config import GraphTarget
from javmem.graph.memory import MemoryGraph
from javmem.graph.project import PROJECTION_VERSION
from javmem.graph.sync import enable
from javmem.pipeline.window import Window

URI = os.environ.get("JAVMEM_TEST_NEO4J_URI", "").strip()
PASSWORD = os.environ.get("JAVMEM_TEST_NEO4J_PASSWORD", "")
pytestmark = pytest.mark.skipif(not (URI and PASSWORD), reason="没给测试用的 Neo4j（JAVMEM_TEST_NEO4J_URI、_PASSWORD）")


def _target() -> GraphTarget:
    return GraphTarget("prod", URI, "neo4j", PASSWORD, "JAVMEM_TEST_NEO4J_PASSWORD")


@pytest.fixture(scope="session")
def neo_ready() -> None:
    """CI 里是新起的图：先改默认密码。刚起来的图网页先通、bolt 晚一点才收，连不上的等一会儿再试，最多一分钟。"""
    import time

    deadline = time.monotonic() + 60
    while True:
        try:
            neo.set_initial_password(_target(), timeout=10)
            return
        except neo.GraphUnavailable:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


@pytest.fixture
def real(neo_ready: None) -> Iterator[neo.NeoGraph]:
    graph = neo.NeoGraph(_target(), timeout=30)
    graph.connect()
    markers = [row["props"] for row in graph.markers()]
    if (markers and not all(marker.get("pytest") for marker in markers)) or (not markers and graph.has_other_content()):
        graph.close()
        pytest.skip("这个图接着别的数据（标记不是测试放的）：不清")
    graph._auto("MATCH ()-[r]->() CALL (r) { DELETE r } IN TRANSACTIONS OF 10000 ROWS")
    graph._auto("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS")
    graph.ensure_schema()
    try:
        yield graph
    finally:
        # 下一次测试认得出是测试放的：有标记的标上 pytest；没标记、又有东西的（直接往图里写的测试），放一个测试的标记
        graph.query(f"MATCH (m:{neo.MARKER}) SET m.pytest = true")
        if not graph.markers() and graph.has_other_content():
            graph.query(f"CREATE (:{neo.MARKER} {{marker_id: 'pytest', kind: $kind, library: 'pytest', pytest: true}})",
                        kind=neo.KIND)
        graph.close()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """javmem graph 命令照环境变量连：正式库的图配成测试用的这个。测试用的图正好是测试图那个口（本机借 7689 跑的时候），
    测试库的图换一个没人用的口：不然设置那一层认成正式库、测试库是同一个图，一个字不写（D37）。"""
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_URI", URI)
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", PASSWORD)
    if URI.rstrip("/").endswith(":7689"):
        monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_URI", "bolt://127.0.0.1:7699")


def connect_real(world: World, graph: neo.NeoGraph) -> neo.NeoGraph:
    conn = ledger(world)
    try:
        enable(conn, graph.create_marker("prod", PROJECTION_VERSION))
    finally:
        conn.close()
    return graph


def test_init_checks_settings_indexes_and_is_repeatable(world: World, real: neo.NeoGraph, env: None,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    real.drop_search_indexes()
    assert main(["graph", "init", "--library", "prod"]) == 0
    out = capsys.readouterr().out
    assert "dbms.usage_report.enabled = false" in out and "db.index.fulltext.default_analyzer = cjk" in out
    assert "jm_fact_text：cjk" in out and "jm_fact_vector：2560 维" in out and "标记放好了" in out
    report = real.index_report()
    assert all(report[name]["analyzer"] == "cjk" for name in neo.FULLTEXT_INDEXES)
    assert all(report[name]["dimensions"] == 2560 for name in neo.VECTOR_INDEXES)
    assert {name: report[name]["properties"] for name in neo.SEARCH_PROPERTIES} == neo.SEARCH_PROPERTIES
    assert neo.stale_search_indexes(report) == []
    assert set(neo.CONSTRAINTS) <= set(real.constraint_names())
    assert main(["graph", "init", "--library", "prod"]) == 0
    assert "早就接上这本账了" in capsys.readouterr().out
    for packet in PACKETS:
        deliver(world, packet)
    write(world, rich_extract).close()
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 0
    assert "主图：整张重建" in capsys.readouterr().out
    assert main(["graph", "check", "--library", "prod", "--fake-embeddings"]) == 0
    assert "两边不一样的：0" in capsys.readouterr().out
    assert main(["graph", "status", "--library", "prod"]) == 0
    assert "欠着：没有" in capsys.readouterr().out


def test_staged_sync_on_neo4j_matches_memory_and_rebuild(world: World, real: neo.NeoGraph) -> None:
    graph = connect_real(world, real)
    staged(world, graph)
    first = digest(graph)
    again = run_sync(world, graph)
    assert sum(again.written.values()) == 0 and digest(graph) == first
    run_sync(world, graph, mode="rebuild")
    assert digest(graph) == first
    assert_matches_ledger(world, graph)
    memory = MemoryGraph()  # 同一本账，内存里的图整张同步出来一样
    conn = ledger(world)
    try:
        enable(conn, memory.create_marker("prod", PROJECTION_VERSION))
    finally:
        conn.close()
    run_sync(world, memory)
    assert digest(memory) == first


def test_chinese_names_are_found_by_fulltext(real: neo.NeoGraph) -> None:
    """cjk 两个字一组：两个字的人名、三个字的人名、四个字以上的公司名都搜得到；全文检索里面就能按范围编号滤。"""
    real.upsert_scopes([{"id": "S1", "props": {"partitions": ["business"], "sensitive": []}}])
    names = [("E1", "王强", "人"), ("E2", "周建国", "人"), ("E3", "海港银行", "公司或机构"), ("E4", "示例贸易有限公司", "公司或机构")]
    real.upsert_entities([{"id": one, "props": {"name": name, "type": kind, "scope_text": "s1"}} for one, name, kind in names])
    real.upsert_aliases([{"id": "A1", "entity": "E1", "props": {"name": "小王", "scope_text": "s1"}}])
    real.upsert_facts([{"id": "F1", "props": {"content": "王强把示例贸易有限公司的单子交给周建国", "scope_text": "s1"},
                        "links": [{"entity": "E1", "role": "subject"}], "vector": None}])
    real.await_indexes(120)
    for query, entity in (("王强", "E1"), ("周建国", "E2"), ("海港银行", "E3"), ("示例贸易", "E4"), ("小王", None)):
        hits = [row["id"] for row in real.query(
            "CALL db.index.fulltext.queryNodes('jm_entity_name', $q) YIELD node RETURN node.id AS id", q=query)]
        if entity is not None:
            assert entity in hits, query
    assert [row["id"] for row in real.query(
        "CALL db.index.fulltext.queryNodes('jm_alias_name', '小王') YIELD node RETURN node.id AS id")] == ["A1"]
    for query in ("王强", "周建国", "示例贸易"):
        assert [row["id"] for row in real.query(
            "CALL db.index.fulltext.queryNodes('jm_fact_text', $q) YIELD node RETURN node.id AS id", q=query)] == ["F1"]
    inside = "CALL db.index.fulltext.queryNodes('jm_fact_text', $q) YIELD node RETURN node.id AS id"
    assert [row["id"] for row in real.query(inside, q="+content:王强 +scope_text:s1")] == ["F1"]
    assert real.query(inside, q="+content:王强 +scope_text:s2") == []


def test_vectors_are_float32_and_the_index_is_2560(real: neo.NeoGraph) -> None:
    rng = random.Random(7)
    values = [rng.gauss(0, 1) for _ in range(2560)]
    norm = sum(value * value for value in values) ** 0.5
    vector = [value / norm for value in values]
    real.upsert_entities([{"id": "E1", "props": {"name": "王强"}}])
    real.upsert_facts([{"id": "F1", "props": {"content": "编的"}, "links": [], "vector": vector}])
    real.await_indexes(120)
    (stored,) = [row["v"] for row in real.query("MATCH (n:JmFact {id: 'F1'}) RETURN n.vector AS v")]
    assert neo.vector_blob(stored) == neo.vector_blob(vector)  # 存的是 float32，读回来一个字节都不差
    hits = real.query("CALL db.index.vector.queryNodes('jm_fact_vector', 1, $v) YIELD node, score RETURN node.id AS id, score",
                      v=vector)
    assert hits[0]["id"] == "F1" and hits[0]["score"] > 0.999
    assert real.index_report()["jm_relation_vector"]["dimensions"] == 2560


def test_same_id_twice_is_one_and_a_moved_relation_moves(real: neo.NeoGraph) -> None:
    real.upsert_entities([{"id": one, "props": {"name": one}} for one in ("E1", "E2", "E3")])
    row = {"id": "R1", "subject": "E1", "object": "E2", "props": {"type": "负责", "content": "E1 负责 E2"}, "vector": None}
    real.upsert_relations([row])
    real.upsert_relations([row])
    real.upsert_relations([{**row, "subject": "E3"}])
    assert real.query("MATCH (a)-[r:JM_RELATION]->(b) RETURN a.id AS s, b.id AS o") == [{"s": "E3", "o": "E2"}]
    real.upsert_facts([{"id": "F1", "props": {"content": "编的"}, "links": [{"entity": "E1", "role": "subject"}]}] * 2)
    assert real.query("MATCH (n:JmFact) RETURN count(n) AS n")[0]["n"] == 1
    assert real.query("MATCH (:JmFact)-[x:JM_INVOLVES]->() RETURN count(x) AS n")[0]["n"] == 1
    real.delete("entity", ["E1"])
    assert real.query("MATCH ()-[x:JM_INVOLVES]->() RETURN count(x) AS n")[0]["n"] == 0


def test_a_foreign_graph_is_refused(world: World, real: neo.NeoGraph, env: None, capsys: pytest.CaptureFixture[str]) -> None:
    real.query("CREATE (:Episodic {group_id: 'made-up-group'})")  # 体检库那种：有东西、没有我们的标记
    with pytest.raises(neo.GraphRefused, match="不是空的"):
        real.check_marker(None, "prod")
    assert main(["graph", "init", "--library", "prod"]) == 2
    assert "一个字不写" in capsys.readouterr().out
    assert real.query("MATCH (n:JmMarker) RETURN count(n) AS n")[0]["n"] == 0


def fulltext(graph: neo.NeoGraph, index: str, query: str) -> list[str]:
    return [row["id"] for row in graph.query(f"CALL db.index.fulltext.queryNodes('{index}', $q) YIELD node RETURN node.id AS id", q=query)]


CEN = {"key": "c", "mention": "岑小燕", "name": "岑小燕", "type": "人"}  # 编的名字，只有 UNIQUE 这一包说过
UNIQUE = day_packet("2026-11-07", "我：岑小燕下周三去海港银行。")
UNIQUE_ID = "2026-11-07_2330_desk-bot"


def unique_extract(window: Window) -> dict[str, object]:
    """rich_extract 再加一样：说到岑小燕的那一句写一条带实体的事实（实体、别名、事实都只出自这一包）。"""
    result = rich_extract(window)
    for line in window.targets:
        if "岑小燕" in line.text:
            text = line.text.rstrip("。")
            result["facts"].append(l3_fact(window, text, "岑小燕下周三去海港银行", subject="c",  # type: ignore[union-attr]
                                           entities=[{"key": "c", "role": "subject"}]))
            result["entities"].append(CEN)  # type: ignore[union-attr]
    return result


def stocked_real(world: World, real: neo.NeoGraph) -> neo.NeoGraph:
    graph = connect_real(world, real)
    for packet in [*PACKETS, UNIQUE]:
        deliver(world, packet)
        write(world, unique_extract).close()
    run_sync(world, graph)
    real.await_indexes(120)
    return graph


def test_withdrawing_clears_the_real_graph_and_its_indexes(world: World, real: neo.NeoGraph, env: None,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    """J5.6：撤回一包，javmem withdraw 当场同步：Neo4j 里没有它派生的点，全文、向量索引都查不到它的事；只有它说过的
    名字，实体名、别名的全文索引也查不到。"""
    stocked_real(world, real)
    gone = [row["id"] for row in real.query("MATCH (n:JmFact) WHERE n.content CONTAINS '11-15' RETURN n.id AS id")]
    assert len(gone) == 1  # 只有第二包说过
    vector = real.query("MATCH (n:JmFact {id: $id}) RETURN n.vector AS v", id=gone[0])[0]["v"]
    assert gone[0] in fulltext(real, "jm_fact_text", "出货日期")
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    assert "主图：同步，" in capsys.readouterr().out
    real.await_indexes(120)
    assert real.query("MATCH (n) WHERE n.id IN $ids RETURN count(n) AS n", ids=gone)[0]["n"] == 0
    left = fulltext(real, "jm_fact_text", "出货日期")
    assert left and gone[0] not in left  # 索引还在用，只是没有撤回的那一条了
    if vector is not None:
        near = real.query("CALL db.index.vector.queryNodes('jm_fact_vector', 5, $v) YIELD node RETURN node.id AS id", v=vector)
        assert gone[0] not in [row["id"] for row in near]
    # 只有一包说过的名字：撤回以前搜得到，撤回以后实体名、别名、事的全文索引都搜不到
    assert fulltext(real, "jm_entity_name", "岑小燕") and fulltext(real, "jm_alias_name", "岑小燕")
    named = fulltext(real, "jm_fact_text", "岑小燕")
    assert named
    assert main(["withdraw", "--library", "prod", "--packet", UNIQUE_ID]) == 0
    real.await_indexes(120)
    for index in ("jm_entity_name", "jm_alias_name", "jm_fact_text"):
        assert fulltext(real, index, "岑小燕") == [], index
    assert real.query("MATCH (n) WHERE n.id IN $ids RETURN count(n) AS n", ids=named)[0]["n"] == 0
    assert main(["graph", "check", "--library", "prod", "--fake-embeddings"]) == 0
    assert "两边不一样的：0" in capsys.readouterr().out


def test_a_new_graph_after_purge_comes_back_the_same(world: World, real: neo.NeoGraph, env: None,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """J5.6：彻底删除以后要磁盘也干净，就换一个新图：图整个清空（连标记，和删了数据目录重起一样是空的）、
    javmem graph init --reset、整张同步，导出和原来一样。"""
    graph = stocked_real(world, real)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    assert main(["purge", "--library", "prod", "--packet", PACKET_IDS[1], "--yes"]) == 0
    out = capsys.readouterr().out
    assert "磁盘上可能还留着" in out and "javmem graph init --reset --library prod" in out
    first = digest(graph)
    real._auto("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS")
    assert real.markers() == []
    assert main(["graph", "init", "--library", "prod", "--reset"]) == 0
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 0
    assert "主图：整张重建" in capsys.readouterr().out
    assert digest(real) == first
    assert_matches_ledger(world, real)


def test_wipe_keeps_the_marker_and_the_side_group_is_never_ours(world: World, real: neo.NeoGraph) -> None:
    graph = connect_real(world, real)
    for packet in PACKETS:
        deliver(world, packet)
    write(world, rich_extract).close()
    run_sync(world, graph)
    labels, types = graph.labels_and_types()
    assert set(labels) <= neo.OUR_LABELS and set(types) <= neo.OUR_TYPES
    assert graph.query("MATCH (n) WHERE n.group_id IS NOT NULL RETURN count(n) AS n")[0]["n"] == 0
    graph.wipe(batch=3)  # 一批批删边、删节点，标记留着
    labels, types = graph.labels_and_types()
    assert labels == {neo.MARKER: 1} and types == {}
    run_sync(world, graph, mode="rebuild")
    assert_matches_ledger(world, graph)
