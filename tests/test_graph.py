"""主图（J5.1、J5.2）不要 Neo4j 的那几层：设置、账本 → 图的规矩、触发器记欠着、同步（内存里的图）、重建、导出比对、
进图前检查的插口、写账以后接着同步。名字和句子都是编的。要真 Neo4j 的在 test_graph_neo4j.py。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import timedelta, timezone
from pathlib import Path

import pytest
from helpers import World
from test_relcheck import relation
from test_write import day_packet, deliver, l3_fact, write

from javmem.cli import main
from javmem.config import ConfigError
from javmem.embedding import FakeEmbeddings
from javmem.graph import dirty, neo
from javmem.graph.config import GraphSettings
from javmem.graph.gate import AdmitAll, GateItem, GateResult, Verdict
from javmem.graph.memory import MemoryGraph
from javmem.graph.project import PROJECTION_VERSION, Projector, scope_info, time_bounds
from javmem.graph.sync import (
    GraphBusy,
    GraphNotSetUp,
    RelationVectors,
    compare,
    enable,
    expected_lines,
    export,
    export_lines,
    graph_lock,
    read_state,
    sync,
)
from javmem.ledger import open_ledger, transaction
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.window import Window
from javmem.withdraw import withdraw

TZ = timezone(timedelta(hours=8))
ZHOU = {"key": "z", "mention": "老周", "name": "周建国", "type": "人"}
ORDER = {"key": "o", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}
NOTE = {"key": "n", "mention": "X1 票据", "name": "X1 票据", "type": "订单或合同"}
BANK = {"key": "b", "mention": "海港银行", "name": "海港银行", "type": "公司或机构"}


def rich_extract(window: Window) -> dict[str, object]:
    """几种都有：带实体和角色的事实、会被替代的事实（出货日期）、推断的事实、关系（生效的、已结束的）。"""
    facts: list[dict[str, object]] = []
    relations: list[dict[str, object]] = []
    entities: dict[str, dict[str, str]] = {}
    for line in window.targets:
        text = line.text.rstrip("。")
        if "老周负责越南那批订单" in text:
            certainty = "inferred" if "大概" in text else "stated"
            facts.append(l3_fact(window, text, "周建国负责越南那批订单", subject="z", attribute="负责的订单", value="越南那批订单",
                                 certainty=certainty, entities=[{"key": "z", "role": "subject"}, {"key": "o", "role": "object"}]))
            entities.update(z=ZHOU, o=ORDER)
        for day in ("11-15", "11-20"):
            if day in text and "出货" in text:
                facts.append(l3_fact(window, text, f"越南那批订单出货日期是 {day}", subject="o", attribute="出货日期", value=day))
                entities["o"] = ORDER
        if "X1 票据" in text:
            fields: dict[str, object] = {"state": "已结束" if "转走" in text else "生效"}
            if "11 月 1 日起" in text:
                fields.update(valid_from="2026-11-01", time_precision="day")
            if "11 月 5 日转走" in text:
                fields.update(valid_to="2026-11-05", time_precision="day")
            relations.append(relation(NOTE, "托管于", BANK, (line.ref, text), **fields))
            entities.update(n=NOTE, b=BANK)
    return {"entities": list(entities.values()), "facts": facts, "relations": relations}


PACKETS = [
    day_packet("2026-11-01", "我：老周负责越南那批订单。", "我：X1 票据 11 月 1 日起在海港银行。"),
    day_packet("2026-11-03", "我：越南那批订单 11-15 出货。"),
    day_packet("2026-11-05", "我：越南那批订单改到 11-20 出货。", "我：X1 票据 11 月 5 日转走了，不在海港银行了。"),
    day_packet("2026-11-06", "我：大概还是老周负责越南那批订单。", domain="[business, research]"),
]
PACKET_IDS = ["2026-11-01_2330_desk-bot", "2026-11-03_2330_desk-bot", "2026-11-05_2330_desk-bot", "2026-11-06_2330_desk-bot"]


def ledger(world: World) -> sqlite3.Connection:
    return open_ledger(world.settings.library("prod"))


def connect_graph(world: World, graph: MemoryGraph | None = None) -> MemoryGraph:
    """账本接上一个新的内存里的图（javmem graph init 做的那几样）。"""
    graph = graph if graph is not None else MemoryGraph()
    conn = ledger(world)
    try:
        marker = graph.create_marker("prod", PROJECTION_VERSION)
        graph.ensure_schema()
        enable(conn, marker)
    finally:
        conn.close()
    return graph


def run_sync(world: World, graph: MemoryGraph, *, mode: str = "incremental", gate=None, embedder=None, chunk: int = 500):
    conn = ledger(world)
    try:
        with VectorCache(world.data / "vectors") as cache:
            vectors = RelationVectors(cache, embedder if embedder is not None else FakeEmbeddings())
            return sync(conn, graph, library="prod", tz=TZ, gate=gate, vectors=vectors, mode=mode, chunk=chunk)
    finally:
        conn.close()


def digest(graph: MemoryGraph) -> str:
    return export(graph)[0]


def expected(world: World, gate=None) -> list[str]:
    conn = ledger(world)
    try:
        with VectorCache(world.data / "vectors") as cache:
            return list(expected_lines(conn, tz=TZ, gate=gate, vectors=RelationVectors(cache, FakeEmbeddings()), library="prod"))
    finally:
        conn.close()


def assert_matches_ledger(world: World, graph: MemoryGraph, gate=None) -> None:
    found = compare(export_lines(graph), iter(expected(world, gate)))
    assert found == {"only_graph": [], "only_ledger": [], "different": []}


def staged(world: World, graph: MemoryGraph, *, chunk: int = 500) -> None:
    """一包一包写账、每次写完同步一次，最后撤回一包、再同步。"""
    for packet in PACKETS:
        deliver(world, packet)
        write(world, rich_extract).close()
        run_sync(world, graph, chunk=chunk)
    withdraw(world.settings, "prod", packets=[PACKET_IDS[1]])
    run_sync(world, graph, chunk=chunk)


# ---------- 设置 ----------


def test_settings_only_local_bolt_and_separate_graphs() -> None:
    settings = GraphSettings.from_env({})
    assert (settings.prod.uri, settings.test.uri, settings.sync, settings.timeout) == (
        "bolt://127.0.0.1:7688", "bolt://127.0.0.1:7689", True, 5.0)
    assert settings.prod.describe()["password_set"] is False
    secret = GraphSettings.from_env({"JAVMEM_GRAPH_NEO4J_PASSWORD": "made-up-pw-1234"})
    assert "made-up-pw-1234" not in repr(secret) and "made-up-pw-1234" not in json.dumps(secret.describe())
    for env, message in (
        ({"JAVMEM_GRAPH_NEO4J_URI": "bolt://10.0.0.5:7688"}, "本机"),
        ({"JAVMEM_GRAPH_NEO4J_URI": "neo4j://127.0.0.1:7688"}, "bolt"),
        ({"JAVMEM_GRAPH_NEO4J_URI": "bolt://user:pw@127.0.0.1:7688"}, "用户名"),
        ({"JAVMEM_GRAPH_TEST_NEO4J_URI": "bolt://127.0.0.1:7688"}, "同一个图"),
        ({"JAVMEM_GRAPH_NEO4J_URI": "bolt://127.0.0.1:7690"}, "体检库"),
        ({"JAVMEM_SIDE_NEO4J_URI": "bolt://127.0.0.1:7689"}, "体检库"),
        ({"JAVMEM_GRAPH_NEO4J_PASSWORD": "short"}, "8 到 256"),
        ({"JAVMEM_GRAPH_SYNC": "maybe"}, "on 或 off"),
        ({"JAVMEM_GRAPH_TIMEOUT_SECONDS": "0"}, "1 到"),
    ):
        with pytest.raises(ConfigError, match=message):
            GraphSettings.from_env(env)
    off = GraphSettings.from_env({"JAVMEM_GRAPH_SYNC": "off", "JAVMEM_GRAPH_TEST_NEO4J_PASSWORD": "x" * 12})
    assert off.sync is False and off.test.password == "x" * 12 and off.prod.password == ""


# ---------- 账本第 26 版：触发器记欠着 ----------


def test_no_triggers_until_the_ledger_is_connected(world: World) -> None:
    """没接上图的账本一个触发器都没有（写账一点不受影响）；接上才装，--reset 拆掉。"""
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    conn = ledger(world)
    try:
        assert dirty.installed(conn) == 0 and conn.execute("SELECT count(*) FROM graph_dirty").fetchone()[0] == 0
        assert read_state(conn).enabled is False
    finally:
        conn.close()
    connect_graph(world)
    conn = ledger(world)
    try:
        state = read_state(conn)
        assert state.enabled and state.triggers == dirty.TRIGGERS_VERSION and dirty.installed(conn) == 35
        from javmem.graph.sync import forget

        forget(conn)
        assert dirty.installed(conn) == 0 and read_state(conn).enabled is False
    finally:
        conn.close()


def test_old_triggers_are_replaced_and_the_sync_goes_full(world: World) -> None:
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    conn = ledger(world)
    try:
        dirty.drop_triggers(conn)
        conn.execute("UPDATE graph_state SET triggers = 't0'")
    finally:
        conn.close()
    report = run_sync(world, graph)
    assert report.mode == "all"
    conn = ledger(world)
    try:
        assert read_state(conn).triggers == dirty.TRIGGERS_VERSION and dirty.installed(conn) == 35
    finally:
        conn.close()
    assert_matches_ledger(world, graph)


@pytest.mark.parametrize("change, kind", [
    ("UPDATE facts SET status = 'pending', pending_reason = 'manual_review' WHERE rowid = (SELECT min(rowid) FROM facts)", "fact"),
    ("DELETE FROM fact_vectors WHERE rowid = (SELECT min(rowid) FROM fact_vectors)", "fact"),
    ("UPDATE relations SET state = 'ended'", "relation"),
    ("UPDATE entities SET canonical_name = canonical_name || '先生' WHERE type = '人'", "entity"),
    ("UPDATE aliases SET withdrawn_at = '2026-11-09T00:00:00+08:00' WHERE rowid = (SELECT min(rowid) FROM aliases)", "alias"),
    ("DELETE FROM evidence WHERE alias_id IS NOT NULL AND rowid = (SELECT min(rowid) FROM evidence WHERE alias_id IS NOT NULL)", "alias"),
    ("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'", "packet"),
    ("INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('ambiguous_entity', '{\"entity_id\": \"E9\"}', '[]', 'x')",
     "entity"),
    ("INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('ambiguous_entity', 'not json', '[]', 'x')", None),
    ("INSERT INTO entity_redirects (old_id, new_id, merge_id) VALUES ('E-old', 'E-new', 'M1')", "entity"),
])
def test_each_change_is_recorded_as_owed(world: World, change: str, kind: str | None) -> None:
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    connect_graph(world)
    conn = ledger(world)
    try:
        conn.execute(change)
        rows = conn.execute("SELECT kind, gen FROM graph_dirty").fetchall()
        assert ({row[0] for row in rows} == {kind}) if kind else not rows
        assert all(row[1] == 1 for row in rows)
    finally:
        conn.close()


def test_mark_and_owed(world: World) -> None:
    conn = ledger(world)
    try:
        assert dirty.mark(conn, "fact", ["F1"]) == 0  # 没接上图：不记
    finally:
        conn.close()
    connect_graph(world)
    conn = ledger(world)
    try:
        assert dirty.mark(conn, "fact", ["F1", "F1", "F2"]) == 2
        counts, since = dirty.owed(conn)
        assert counts == {"fact": 2} and since is not None and since.endswith("Z")
        with pytest.raises(ValueError):
            dirty.mark(conn, "nonsense", ["x"])
    finally:
        conn.close()


# ---------- 规矩：什么进、什么不进 ----------


def test_what_goes_in_and_what_stays_out(world: World) -> None:
    graph = connect_graph(world)
    for packet in PACKETS:
        deliver(world, packet)
    write(world, rich_extract).close()
    report = run_sync(world, graph)
    assert report.mode == "rebuild" and report.status == "ok"
    labels, types = graph.labels_and_types()
    facts = {item["id"]: item for item in graph.iter_items("fact")}
    contents = sorted(item["props"]["content"] for item in facts.values())
    assert contents == ["周建国负责越南那批订单", "周建国负责越南那批订单", "越南那批订单出货日期是 11-15", "越南那批订单出货日期是 11-20"]
    old = next(item for item in facts.values() if item["props"]["content"].endswith("11-15"))
    new = next(item for item in facts.values() if item["props"]["content"].endswith("11-20"))
    assert old["props"]["superseded_by"] == new["id"] and new["props"]["replaces"] == [old["id"]]  # 被替代的留着
    inferred = [item for item in facts.values() if item["props"]["status"] == "inferred"]
    assert len(inferred) == 1 and inferred[0]["props"]["inferred"] is True and inferred[0]["props"]["partitions"] == ["business", "research"]
    assert all(len(item["vector"]) == 2560 for item in facts.values())
    assert all("quote" not in json.dumps(item["props"], ensure_ascii=False) for item in facts.values())  # 原句不进图
    zhou_fact = next(item for item in facts.values() if item["props"]["status"] == "verified" and "负责" in item["props"]["content"])
    assert sorted(link["role"] for link in zhou_fact["links"]) == ["object", "subject"]
    (rel,) = list(graph.iter_items("relation"))
    assert rel["props"]["type"] == "托管于" and rel["props"]["state"] == "ended" and rel["props"]["content"] == "X1 票据 托管于 海港银行"
    assert rel["props"]["valid_from"] == "2026-11-01" and rel["props"]["from_lo"] is not None and len(rel["vector"]) == 2560
    entities = {item["props"]["name"]: item for item in graph.iter_items("entity")}
    assert set(entities) >= {"周建国", "越南那批订单", "X1 票据", "海港银行"}
    assert len(entities["周建国"]["props"]["scope_ids"]) == 2  # 两个范围的事实、别名都连着它
    aliases = {item["props"]["name"]: item for item in graph.iter_items("alias")}
    assert len(aliases["老周"]["props"]["scope_ids"]) == 2 and aliases["老周"]["entities"] == [entities["周建国"]["id"]]
    assert labels[neo.SCOPE] == 2 and types[neo.RELATION] == 1
    assert_matches_ledger(world, graph)


def test_pending_facts_leave_the_graph_and_come_back(world: World) -> None:
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    before = digest(graph)
    conn = ledger(world)
    try:
        fact_id = conn.execute("SELECT fact_id FROM facts WHERE content = '周建国负责越南那批订单'").fetchone()[0]
        conn.execute("UPDATE facts SET status = 'pending', pending_reason = 'frozen_conflict' WHERE fact_id = ?", (fact_id,))
    finally:
        conn.close()
    run_sync(world, graph)
    assert fact_id not in {item["id"] for item in graph.iter_items("fact")}
    assert_matches_ledger(world, graph)
    conn = ledger(world)
    try:
        conn.execute("UPDATE facts SET status = 'verified', pending_reason = NULL WHERE fact_id = ?", (fact_id,))
    finally:
        conn.close()
    run_sync(world, graph)
    assert digest(graph) == before


def test_aliases_in_an_open_ambiguity_scope_stay_out(world: World) -> None:
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    conn = ledger(world)
    try:
        entity_id = conn.execute("SELECT entity_id FROM entities WHERE canonical_name = '周建国'").fetchone()[0]
        scope = conn.execute("SELECT partitions, sensitive FROM packets").fetchone()
        key = json.dumps({"partitions": json.loads(scope[0]), "sensitive": json.loads(scope[1])}, ensure_ascii=False, sort_keys=True)
        conn.execute("INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('ambiguous_entity', ?, '[]', 'x')",
                     (json.dumps({"entity_id": entity_id, "scope": key}, ensure_ascii=False),))
    finally:
        conn.close()
    run_sync(world, graph)
    assert "老周" not in {item["props"]["name"] for item in graph.iter_items("alias")}
    assert_matches_ledger(world, graph)


def test_time_bounds_follow_the_precision() -> None:
    day = time_bounds("2026-11-09", "day", TZ)
    assert day[1] - day[0] == 86400
    month = time_bounds("2026-11", "month", TZ)
    assert month[1] - month[0] == 30 * 86400
    quarter = time_bounds("2026-Q4", "quarter", TZ)
    assert quarter[0] == time_bounds("2026-10", "month", TZ)[0] and quarter[1] == time_bounds("2027", "year", TZ)[0]
    minute = time_bounds("2026-11-09T10:30+00:00", "minute", TZ)
    assert minute[1] - minute[0] == 60 and minute[0] == day[0] + 18 * 3600 + 30 * 60  # 照它自己的时区
    assert time_bounds(None, "day", TZ) == (None, None) and time_bounds("2026-11-09", "unknown", TZ) == (None, None)
    assert time_bounds("不是日子", "day", TZ) == (None, None)


def test_scope_ids_are_stable_and_order_free() -> None:
    a = scope_info('["business", "research"]', '[]')
    b = scope_info('["research", "business", "business"]', '[]')
    assert a == b and a.scope_id.startswith("S") and len(a.scope_id) == 13
    assert scope_info('["business"]', '["private"]').scope_id != scope_info('["business"]', '[]').scope_id


# ---------- 同步：重复一样、一点点和整张一样 ----------


def test_staged_sync_equals_rebuild_and_the_ledger(world: World) -> None:
    graph = connect_graph(world)
    staged(world, graph)
    first = digest(graph)
    again = run_sync(world, graph)
    assert sum(again.written.values()) == 0 and sum(again.deleted.values()) == 0 and digest(graph) == first
    every = run_sync(world, graph, mode="all")
    assert every.mode == "all" and sum(every.written.values()) == 0 and digest(graph) == first
    rebuilt = run_sync(world, graph, mode="rebuild")
    assert rebuilt.mode == "rebuild" and digest(graph) == first
    assert_matches_ledger(world, graph)
    fresh = connect_graph(world)  # 删了图、重起、init、整张同步
    run_sync(world, fresh)
    assert digest(fresh) == first


def test_small_chunks_give_the_same_graph(world: World) -> None:
    big, small = connect_graph(world), MemoryGraph()
    staged(world, big)
    conn = ledger(world)
    try:
        small_marker = small.create_marker("prod", PROJECTION_VERSION)
        enable(conn, small_marker)
    finally:
        conn.close()
    run_sync(world, small, chunk=1)
    assert digest(small) == digest(big)


def test_withdrawn_packet_leaves_no_trace(world: World) -> None:
    graph = connect_graph(world)
    staged(world, graph)
    contents = {item["props"]["content"] for item in graph.iter_items("fact")}
    assert "越南那批订单出货日期是 11-15" not in contents  # 第二包撤回了
    conn = ledger(world)
    try:
        assert conn.execute("SELECT count(*) FROM graph_dirty").fetchone()[0] == 0
        counts = dict(conn.execute("SELECT kind, count(*) FROM graph_items GROUP BY kind").fetchall())
    finally:
        conn.close()
    labels, _ = graph.labels_and_types()
    assert counts["fact"] == labels[neo.FACT] and counts["entity"] == labels[neo.ENTITY]


def test_a_change_during_the_sync_stays_owed(world: World) -> None:
    """第几轮：同步读完欠着的以后账本又改了，改的那条留到下一次。"""
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    conn = ledger(world)
    try:
        fact_id = conn.execute("SELECT fact_id FROM facts LIMIT 1").fetchone()[0]
        conn.execute("UPDATE facts SET frozen = 1 WHERE fact_id = ?", (fact_id,))
    finally:
        conn.close()

    class Meddling(AdmitAll):
        def admit(self, conn: sqlite3.Connection, library: str, items: Sequence[GateItem]) -> GateResult:
            other = sqlite3.connect(str(conn.execute("PRAGMA database_list").fetchone()[2]))
            other.execute("UPDATE facts SET frozen = 0 WHERE fact_id = ?", (fact_id,))
            other.commit()
            other.close()
            return super().admit(conn, library, items)

    run_sync(world, graph, gate=Meddling())
    conn = ledger(world)
    try:
        assert ("fact", fact_id) in {(row[0], row[1]) for row in conn.execute("SELECT kind, item_id FROM graph_dirty")}
    finally:
        conn.close()
    run_sync(world, graph)
    assert_matches_ledger(world, graph)


def test_a_sync_cut_short_is_finished_next_time(world: World) -> None:
    graph = connect_graph(world)
    for packet in PACKETS:
        deliver(world, packet)
    write(world, rich_extract).close()
    graph.fail_after = 3
    with pytest.raises(neo.GraphUnavailable):
        run_sync(world, graph, chunk=1)
    graph.fail_after = None
    report = run_sync(world, graph)
    assert report.mode == "all"  # 上一回整张的没做完：再来一次整张的
    assert_matches_ledger(world, graph)


def test_marker_must_match(world: World) -> None:
    graph = connect_graph(world)
    other = MemoryGraph()
    other.create_marker("prod", PROJECTION_VERSION)
    with pytest.raises(neo.GraphRefused, match="对不上"):
        run_sync(world, other)
    wrong_library = MemoryGraph()
    wrong_library.marker_rows.append({**graph.marker_rows[0], "library": "test"})
    with pytest.raises(neo.GraphRefused, match="另一个库"):
        run_sync(world, wrong_library)
    foreign = MemoryGraph()
    foreign.foreign["Episodic"] = 3  # 体检库、别的系统的 Neo4j 那种：有东西、没有我们的标记
    with pytest.raises(neo.GraphRefused, match="不是空的"):
        run_sync(world, foreign)
    with pytest.raises(neo.GraphRefused, match="init --reset"):
        run_sync(world, MemoryGraph())  # 空的，可账本记着上一个图
    conn = ledger(world)
    try:
        conn.execute("UPDATE graph_state SET enabled = 0")
        with pytest.raises(GraphNotSetUp):
            sync(conn, graph, library="prod", tz=TZ)
    finally:
        conn.close()


def test_counts_go_down_without_dropping_too_early(world: World) -> None:
    """数数的两张表：3 条减 2 条还剩 1 条（范围、实体还在图里），再减 1 条才删。"""
    from javmem.graph.sync import SyncReport, _Run

    connect_graph(world)
    conn = ledger(world)
    try:
        run = _Run(conn, MemoryGraph(), "prod", AdmitAll(), None, SyncReport(), 1, Projector(conn, TZ), None, 500)
        run.scope_infos["S1"] = (["business"], [])

        def counts() -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
            return ([tuple(row) for row in conn.execute("SELECT entity_id, scope_id, n FROM graph_entity_scopes")],
                    [tuple(row) for row in conn.execute("SELECT scope_id, n FROM graph_scopes")])

        for delta, want in ((3, ([("E1", "S1", 3)], [("S1", 3)])), (-2, ([("E1", "S1", 1)], [("S1", 1)])), (-1, ([], []))):
            with transaction(conn):
                run._apply_counts({("E1", "S1"): delta}, {"S1": delta})
            assert counts() == want
    finally:
        conn.close()


def test_one_sync_at_a_time(tmp_path: Path) -> None:
    with graph_lock(tmp_path, "prod"):
        with pytest.raises(GraphBusy), graph_lock(tmp_path, "prod"):
            pass
        with graph_lock(tmp_path, "test"):
            pass


# ---------- 合并 ----------


def test_a_merge_moves_everything_to_the_new_entity(merged_world: tuple[World, Path]) -> None:
    from test_merging import STANDARD, TYPO, proposal_of

    world, path = merged_world
    graph = connect_graph(world)
    run_sync(world, graph)
    assert TYPO in {item["id"] for item in graph.iter_items("entity")}
    assert main(["entities", "--library", "prod", "--merge", proposal_of(world, path, "柏庭舟"), "--note", "测试"]) == 0
    report = run_sync(world, graph)
    assert report.mode == "incremental"
    entities = {item["id"]: item for item in graph.iter_items("entity")}
    assert TYPO not in entities and TYPO in entities[STANDARD]["props"]["merged_from"]
    assert all(link["entity"] != TYPO for item in graph.iter_items("fact") for link in item["links"])
    assert_matches_ledger(world, graph)


# ---------- 进图前检查的插口（J5.3） ----------


class HoldInvestment:
    """挡住 investment 分区的事实、关系，原因写 manual_review；带“大概”的这次不判。"""

    version = "hold-investment-1"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def admit(self, conn: sqlite3.Connection, library: str, items: Sequence[GateItem]) -> GateResult:
        result = GateResult()
        for item in items:
            self.seen.append(item.item_id)
            key = (item.kind, item.item_id)
            if "research" in item.partitions:
                result.verdicts[key] = Verdict.LATER
            elif item.kind == "relation":
                result.verdicts[key] = Verdict.HOLD
                result.reasons[key] = "manual_review"
            else:
                result.verdicts[key] = Verdict.ADMIT
        return result


def test_the_gate_decides_and_the_sync_counts(world: World) -> None:
    graph = connect_graph(world)
    for packet in PACKETS:
        deliver(world, packet)
    write(world, rich_extract).close()
    run_sync(world, graph)
    assert len(list(graph.iter_items("relation"))) == 1
    gate = HoldInvestment()
    report = run_sync(world, graph, gate=gate)
    assert report.mode == "all"  # 换了检查的版本：全部重交
    assert report.gate.held == {"manual_review": 1} and report.gate.later == 1 and report.gate.admitted == 3
    assert "挡了 1 条（manual_review 1）" in report.text() and "这次没判 1 条" in report.text()
    assert list(graph.iter_items("relation")) == []  # 挡住的、图里原来有的删掉
    assert not any(item["props"]["status"] == "inferred" for item in graph.iter_items("fact"))
    conn = ledger(world)
    try:
        owed = {(row[0], row[1]) for row in conn.execute("SELECT kind, item_id FROM graph_dirty")}
        assert len(owed) == 1 and next(iter(owed))[0] == "fact"  # 这次没判的欠着
        state = read_state(conn)
        assert state.gate == "hold-investment-1"
        assert conn.execute("SELECT count(*) FROM relations").fetchone()[0] == 1  # 没过的留在账本，一行不动
    finally:
        conn.close()
    gate.seen.clear()
    run_sync(world, graph, gate=gate)
    assert len(gate.seen) == 1  # 下次只交欠着的那一条
    assert_matches_ledger(world, graph, gate=gate)


# ---------- 关系的向量 ----------


def test_relation_vectors_owed_when_the_service_is_down(world: World) -> None:
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    report = run_sync(world, graph, embedder=FakeEmbeddings(["auth"]))
    assert report.vectors_owed == 1 and report.owed_after == 1
    (rel,) = list(graph.iter_items("relation"))
    assert rel["vector"] is None
    report = run_sync(world, graph)
    assert report.vectors_owed == 0 and report.owed_after == 0 and report.vectors_embedded == 1
    (rel,) = list(graph.iter_items("relation"))
    assert len(rel["vector"]) == 2560
    again = run_sync(world, graph)
    assert sum(again.written.values()) == 0 and again.vectors_embedded == 0  # 缓存里有：不再算


def test_check_finds_what_differs(world: World) -> None:
    graph = connect_graph(world)
    staged(world, graph)
    fact_id = sorted(graph.nodes[neo.FACT])[0]
    graph.nodes[neo.FACT][fact_id]["content"] = "改过的"
    graph.delete("alias", [sorted(graph.nodes[neo.ALIAS])[0]])
    graph.foreign["Episodic"] = 1
    found = compare(export_lines(graph), iter(expected(world)))
    assert found["different"] == ["_counts:", f"fact:{fact_id}"] and len(found["only_ledger"]) == 1


# ---------- 写账以后接着同步 ----------


@pytest.fixture
def spot_world(world: World) -> World:
    from spotter import SpotModel, extract_into_store

    for packet in PACKETS[:2]:
        world.drop("inbox/desk-bot", packet)
    world.intake()
    extract_into_store(world, SpotModel([("老周", [("周建国", "人", "老周")]), ("越南那批订单", [("越南那批订单", "订单或合同", "越南那批订单")])]))
    return world


def test_write_reports_a_ledger_that_is_not_connected(spot_world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert "主图：这本账还没接上图（javmem graph init），没同步" in capsys.readouterr().out


def test_write_syncs_and_a_down_graph_only_leaves_owed(spot_world: World, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    graph = connect_graph(spot_world)
    run_sync(spot_world, graph)  # 接上以后头一回是整张的（写账里不整张装：见 test_graph_review）
    summary = tmp_path / "summary.json"
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert "没设 JAVMEM_GRAPH_NEO4J_PASSWORD，没连图" in capsys.readouterr().out  # 没设密码：不去连
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "x" * 16)

    class Down(MemoryGraph):
        def connect(self) -> None:
            raise neo.GraphUnavailable("ServiceUnavailable")

    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: Down())
    world = spot_world
    world.drop("inbox/desk-bot", PACKETS[2])
    world.intake()
    from spotter import SpotModel, extract_into_store

    extract_into_store(world, SpotModel([("越南那批订单", [("越南那批订单", "订单或合同", "越南那批订单")])]))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings", "--summary", str(summary)]) == 0
    out = capsys.readouterr().out
    assert "图连不上（ServiceUnavailable），没同步，欠着" in out
    saved = json.loads(summary.read_text(encoding="utf-8"))["graph"]
    assert saved["status"] == "unavailable" and saved["owed_after"] > 0
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: graph)
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0  # 下次写账补上
    out = capsys.readouterr().out
    assert "主图：同步，写了" in out and "还欠着 0 条" in out
    assert_matches_ledger(world, graph)
    monkeypatch.setenv("JAVMEM_GRAPH_SYNC", "off")
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert "JAVMEM_GRAPH_SYNC=off，没同步" in capsys.readouterr().out


def test_the_hook_never_raises(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """账本已经写好了：写完账接着同步这一步出什么错（连同又打开账本读状态）都不往外抛，只记在汇总里。"""
    from javmem.graph import hook

    def broken(_library: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hook, "open_ledger", broken)
    report = hook.after_write(world.settings, "prod", embedder=None, cache=None)
    assert report.status == "error" and "OperationalError" in report.message
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_URI", "bolt://example.org:7688")
    report = hook.after_write(world.settings, "prod", embedder=None, cache=None)
    assert report.status == "config" and "本机" in report.message


def test_init_on_memory_graph_and_takes_its_marker_back_on_failure(world: World, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    from javmem.graph import cli as graph_cli

    graph = MemoryGraph()
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "x" * 16)
    monkeypatch.setattr(neo, "set_initial_password", lambda target, timeout=None: False)
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: graph)
    real_enable = graph_cli.enable

    def broken(conn: sqlite3.Connection, marker_id: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(graph_cli, "enable", broken)
    assert main(["graph", "init", "--library", "prod"]) == 2
    assert graph.marker_rows == []  # 账本没接上：刚放的标记拿掉了
    monkeypatch.setattr(graph_cli, "enable", real_enable)
    assert main(["graph", "init", "--library", "prod"]) == 0
    out = capsys.readouterr().out
    assert "标记放好了" in out and "jm_fact_vector：2560 维" in out and len(graph.marker_rows) == 1
    wrong = MemoryGraph(config={**neo.EXPECTED_CONFIG, "dbms.usage_report.enabled": "true"})
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: wrong)
    assert main(["graph", "init", "--library", "prod"]) == 2
    assert "设置不对：dbms.usage_report.enabled" in capsys.readouterr().out and wrong.marker_rows == []


def test_graph_status_without_connecting(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["graph", "status", "--library", "prod"]) == 1
    assert "还没接上图" in capsys.readouterr().out
    connect_graph(world)
    conn = ledger(world)
    try:
        dirty.mark(conn, "fact", ["F1"])
    finally:
        conn.close()
    assert main(["graph", "status", "--library", "prod"]) == 1
    out = capsys.readouterr().out
    assert "欠着：fact 1" in out and "下一次同步是整张的" in out


def test_graph_commands_need_a_password_and_refuse_repo_paths(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["graph", "sync", "--library", "prod"]) == 2
    assert "JAVMEM_GRAPH_NEO4J_PASSWORD" in capsys.readouterr().out
    repo = Path(__file__).resolve().parents[1]
    assert main(["graph", "export", "--library", "prod", "--out", str(repo / "x.jsonl")]) == 2
    assert "仓库里" in capsys.readouterr().out
    assert main(["graph", "rebuild", "--library", "prod"]) == 2
    assert "--yes" in capsys.readouterr().out


# ---------- 主图查不到支线（第 6 步留下的） ----------


def test_the_side_line_never_reaches_the_main_graph(world: World) -> None:
    from test_side import PACKET_ID, FakeGraph, _packet, _ran

    from javmem.side.store import SideStore

    _packet(world)
    side_graph = FakeGraph()
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, side_graph)
    assert side_graph.groups  # 支线跑完了这一包，体检库里有它那一组
    graph = connect_graph(world)
    write(world, rich_extract).close()
    run_sync(world, graph)
    text = "\n".join(export_lines(graph))
    labels, types = graph.labels_and_types()
    assert set(labels) <= neo.OUR_LABELS and set(types) <= neo.OUR_TYPES
    for group_id, episodes in side_graph.groups.items():
        assert group_id not in text and PACKET_ID not in text
        assert all(str(one["uuid"]) not in text for one in episodes)
    with pytest.raises(ConfigError, match="体检库"):
        GraphSettings.from_env({"JAVMEM_GRAPH_NEO4J_URI": "bolt://127.0.0.1:7690"})


from test_merging import merged_world  # noqa: E402,F401  合并那条测试用它的账本
