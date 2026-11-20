"""管理命令改完账本当场同步主图（J5.6）：撤回、彻底删除、解除撤回、冻结、处理待确认、每天的提醒、合并拆开。

图用内存里的（javmem.graph.memory.MemoryGraph），命令行连图都连到它；要真 Neo4j 的在 test_graph_neo4j.py。名字和句子都是编的。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from helpers import World
from test_graph import PACKET_IDS, PACKETS, TZ, assert_matches_ledger, connect_graph, ledger, rich_extract, run_sync
from test_merging import merged_world  # noqa: F401  合并那条测试用它的账本
from test_review import frozen_and_contradicted, reviews
from test_write import deliver, write

from javmem.cli import main
from javmem.graph import hook, neo
from javmem.graph.gate import AdmitAll
from javmem.graph.memory import MemoryGraph
from javmem.graph.project import Projector
from javmem.graph.sync import graph_lock, needs_full
from javmem.ledger import transaction
from javmem.pipeline import precheck
from javmem.pipeline.vectors import VectorCache, vector_key
from javmem.withdraw import withdraw

SHIP_15 = "越南那批订单出货日期是 11-15"  # 只有第二包说过（PACKETS[1]）


def facts_in(graph: MemoryGraph) -> dict[str, dict[str, object]]:
    return {item["id"]: item["props"] for item in graph.iter_items("fact")}


def contents(graph: MemoryGraph) -> set[object]:
    return {props["content"] for props in facts_in(graph).values()}


def owed(world: World) -> int:
    return int(world.rows("SELECT count(*) AS n FROM graph_dirty")[0]["n"])


def plug(monkeypatch: pytest.MonkeyPatch, graph: MemoryGraph) -> None:
    """命令行连图都连到这个内存里的图（设了密码才会去连）。"""
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "x" * 16)
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: graph)


@pytest.fixture
def graphed(world: World, monkeypatch: pytest.MonkeyPatch) -> tuple[World, MemoryGraph]:
    """四个包写进账本（不经过命令行，不接着同步），接上一个内存里的图、整张同步过。"""
    graph = connect_graph(world)
    for packet in PACKETS:
        deliver(world, packet)
        write(world, rich_extract).close()
    run_sync(world, graph)
    plug(monkeypatch, graph)
    return world, graph


class Down(MemoryGraph):
    def connect(self) -> None:
        raise neo.GraphUnavailable("ServiceUnavailable")


# ---------- 撤回 ----------


def test_withdrawing_syncs_the_graph_at_once(graphed: tuple[World, MemoryGraph], capsys: pytest.CaptureFixture[str]) -> None:
    world, graph = graphed
    assert SHIP_15 in contents(graph)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    out = capsys.readouterr().out
    assert "主图：同步，" in out and "还欠着 0 条" in out and "主图还没清" not in out
    assert SHIP_15 not in contents(graph)
    assert owed(world) == 0
    assert_matches_ledger(world, graph)


def test_json_output_carries_the_graph_and_the_line_goes_to_stderr(graphed: tuple[World, MemoryGraph],
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    world, graph = graphed
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1], "--json"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["graph"]["status"] == "ok" and report["graph"]["owed_after"] == 0
    assert "主图：同步，" in captured.err and "主图" not in captured.out.replace('"graph"', "")
    assert_matches_ledger(world, graph)


@pytest.mark.parametrize(
    ("setup", "said"),
    [
        ("down", "图连不上（ServiceUnavailable），没同步"),
        ("no_password", "没设 JAVMEM_GRAPH_NEO4J_PASSWORD，没连图"),
        ("off", "JAVMEM_GRAPH_SYNC=off，没同步"),
    ],
)
def test_a_withdrawal_the_graph_did_not_get_says_so_and_stays_owed(
    graphed: tuple[World, MemoryGraph], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    setup: str, said: str,
) -> None:
    """撤回照样做完；主图没同步成：说清楚、退出码 1（要人看），欠着的留着，下一次同步补上。"""
    world, graph = graphed
    if setup == "down":
        monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: Down())
    elif setup == "no_password":
        monkeypatch.delenv("JAVMEM_GRAPH_NEO4J_PASSWORD")
    else:
        monkeypatch.setenv("JAVMEM_GRAPH_SYNC", "off")
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 1
    out = capsys.readouterr().out
    assert said in out and "主图还没清" in out and "javmem graph sync --library prod" in out
    assert world.packet_row(PACKET_IDS[1])["withdrawn_at"] is not None  # 撤回本身做完了
    assert SHIP_15 in contents(graph) and owed(world) > 0
    plug(monkeypatch, graph)
    monkeypatch.delenv("JAVMEM_GRAPH_SYNC", raising=False)
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 0
    assert SHIP_15 not in contents(graph)
    assert_matches_ledger(world, graph)


def test_a_ledger_without_a_graph_prints_nothing_about_it(world: World, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    for packet in PACKETS:
        deliver(world, packet)
        write(world, rich_extract).close()
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "x" * 16)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    assert "主图" not in capsys.readouterr().out


def test_the_admin_sync_waits_for_a_running_sync(graphed: tuple[World, MemoryGraph]) -> None:
    """图正在同步（锁被占着）：等一会儿；锁放了就接着同步，等过了照实说。"""
    world, graph = graphed
    withdraw(world.settings, "prod", packets=[PACKET_IDS[1]])  # 只改账本，不接着同步
    with graph_lock(world.data, "prod"):
        report = hook.after_admin(world.settings, "prod", wait=0.3)
    assert report.status == "busy" and "等了" in report.message and report.owed_after > 0
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with graph_lock(world.data, "prod"):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(10)
    timer = threading.Timer(0.5, release.set)
    timer.start()
    try:
        report = hook.after_admin(world.settings, "prod", wait=10)
    finally:
        release.set()
        holder.join()
        timer.cancel()
    assert report.status == "ok" and report.owed_after == 0
    assert SHIP_15 not in contents(graph)


def test_when_a_full_sync_is_owed_the_admin_sync_still_clears_what_changed(
    graphed: tuple[World, MemoryGraph], capsys: pytest.CaptureFixture[str],
) -> None:
    """升级以后还没整张同步（这里是进图前检查换了版本）：管理命令照样把这次改到的同步了，整张同步照旧欠着。"""
    world, graph = graphed
    conn = ledger(world)
    try:
        with transaction(conn):
            conn.execute("UPDATE graph_state SET gate = 'old-gate' WHERE id = 1")
    finally:
        conn.close()
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    out = capsys.readouterr().out
    assert "只同步了这次改到的，还欠一次整张同步：进图前检查换了版本，跑 javmem graph sync --library prod" in out
    assert SHIP_15 not in contents(graph)
    conn = ledger(world)
    try:
        assert needs_full(conn, AdmitAll().version, TZ, "fake-embedding") == "进图前检查换了版本"
    finally:
        conn.close()
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 0  # 照旧整张的
    conn = ledger(world)
    try:
        assert needs_full(conn, AdmitAll().version, TZ, "fake-embedding") is None
    finally:
        conn.close()
    assert_matches_ledger(world, graph)


def test_an_empty_graph_that_needs_a_full_sync_is_left_to_graph_sync(world: World, monkeypatch: pytest.MonkeyPatch,
                                                                      capsys: pytest.CaptureFixture[str]) -> None:
    """接上了、还没整张同步过（图里什么都没有）：没有要清的，不去同步，也不算没同步成。"""
    graph = connect_graph(world)
    for packet in PACKETS:
        deliver(world, packet)
        write(world, rich_extract).close()
    plug(monkeypatch, graph)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    out = capsys.readouterr().out
    assert "图里还没有东西，这次没同步：跑 javmem graph sync --library prod" in out and "主图还没清" not in out
    assert facts_in(graph) == {}


def test_missing_triggers_are_not_taken_for_a_clean_sync(graphed: tuple[World, MemoryGraph],
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """记欠账的触发器不全（以后的迁移重建了一张表，那张表上的就没了）：这次改的不一定记上了，只同步欠着的会漏；
    不同步、照实说、退出码 1，整张同步一遍才清（审阅第 1 条）。"""
    world, graph = graphed
    conn = ledger(world)
    try:
        with transaction(conn):
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name LIKE 'fact%'").fetchall():
                conn.execute(f'DROP TRIGGER "{name}"')
    finally:
        conn.close()
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 1
    out = capsys.readouterr().out
    assert "记欠账的触发器不全或者是旧的，这次改的不一定都记上了，没同步" in out and "主图还没清" in out
    assert SHIP_15 in contents(graph)
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 0  # 换上触发器、整张同步一遍
    assert SHIP_15 not in contents(graph)
    assert_matches_ledger(world, graph)


def test_an_old_trigger_version_counts_as_untracked_but_an_empty_graph_does_not(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """触发器是旧的一版也算记欠账的不全；接上以后还没同步过（图里什么都没有）的不算没同步成（复审第 1 条）。"""
    graph = connect_graph(world)
    for packet in PACKETS:
        deliver(world, packet)
        write(world, rich_extract).close()
    plug(monkeypatch, graph)
    conn = ledger(world)
    try:
        with transaction(conn):
            conn.execute("UPDATE graph_state SET triggers = 't1' WHERE id = 1")
    finally:
        conn.close()
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0  # 图里还没有东西：没有要清的
    out = capsys.readouterr().out
    assert "记欠账的触发器不全或者是旧的，图里还没有东西" in out and "主图还没清" not in out
    run_sync(world, graph)  # 整张同步一遍：触发器换成这一版
    conn = ledger(world)
    try:
        with transaction(conn):
            conn.execute("UPDATE graph_state SET triggers = 't1' WHERE id = 1")
    finally:
        conn.close()
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[2]]) == 1
    out = capsys.readouterr().out
    assert "这次改的不一定都记上了" in out and "下次同步补上" not in out and "主图还没清" in out


def test_a_rebuild_running_elsewhere_is_waited_for_not_reported_as_cut_short(graphed: tuple[World, MemoryGraph]) -> None:
    """别的进程的整张重建正在跑（账本这边记的刚清掉、还没装）：先等锁，等过了照“正在同步”说，不说“上一回没做完”（复审第 2 条）。"""
    world, graph = graphed
    withdraw(world.settings, "prod", packets=[PACKET_IDS[1]])
    conn = ledger(world)
    try:
        with transaction(conn):
            conn.execute("UPDATE graph_state SET rebuilt_at = NULL WHERE id = 1")
            for table in ("graph_items", "graph_entity_scopes", "graph_scopes"):
                conn.execute(f"DELETE FROM {table}")
    finally:
        conn.close()
    with graph_lock(world.data, "prod"):
        report = hook.after_admin(world.settings, "prod", wait=0.3)
    assert report.status == "busy"


def test_a_service_on_another_model_is_not_mixed_into_the_graph(graphed: tuple[World, MemoryGraph],
                                                                monkeypatch: pytest.MonkeyPatch,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    """配了向量服务、可它的模型和图里的不是一个（换了模型、还没整张同步）：管理命令不调服务，照图的模型只查缓存，
    图里不混两种模型的向量（复审第 4 条）。服务的地址是本机一个没人听的口，调了就会出错。"""
    world, graph = graphed
    conn = ledger(world)
    try:
        with transaction(conn):
            conn.execute("UPDATE graph_state SET projection = substr(projection, 1, instr(projection, '#')) || 'made-up-model'")
    finally:
        conn.close()
    monkeypatch.setenv("JAVMEM_EMBEDDING_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("JAVMEM_EMBEDDING_MODEL", "other-model")
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[0]]) == 0
    out = capsys.readouterr().out
    assert "主图：同步，" in out and "还欠一次整张同步" not in out
    models = {item["props"]["vector_model"] for item in graph.iter_items("relation")}
    assert "other-model" not in models


def test_graph_commands_and_admin_commands_take_the_same_gate(graphed: tuple[World, MemoryGraph],
                                                              monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    """进图前检查：写账、管理命令、javmem graph sync、check 都从 hook.default_gate 取（J5.3 照开关选口子），几处一起换（复审第 11 条）。"""
    world, graph = graphed

    class NewGate(AdmitAll):
        version = "test-gate-2"

    monkeypatch.setattr(precheck, "gate", lambda env=None: NewGate())
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 0  # 检查换了版本：整张同步一遍
    assert world.rows("SELECT gate FROM graph_state WHERE id = 1")[0]["gate"] == "test-gate-2"
    capsys.readouterr()
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    out = capsys.readouterr().out
    assert "主图：同步，" in out and "还欠一次整张同步" not in out
    assert main(["graph", "check", "--library", "prod", "--fake-embeddings"]) == 0


def test_a_rebuild_cut_short_is_not_taken_for_an_empty_graph(graphed: tuple[World, MemoryGraph],
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    """整张重建断在清图中间（账本这边记的清了、图里还有东西）：不当成“图里还没有东西”，照实说、退出码 1（审阅第 2 条）。"""
    world, graph = graphed
    conn = ledger(world)
    try:
        with transaction(conn):
            conn.execute("UPDATE graph_state SET rebuilt_at = NULL WHERE id = 1")
            for table in ("graph_items", "graph_entity_scopes", "graph_scopes"):
                conn.execute(f"DELETE FROM {table}")
    finally:
        conn.close()
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 1
    out = capsys.readouterr().out
    assert "上一回整张同步没做完，图里可能还有以前的东西，这次没同步" in out and "主图还没清" in out
    assert SHIP_15 in contents(graph)
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 0  # 清空重装
    assert SHIP_15 not in contents(graph)
    assert_matches_ledger(world, graph)


def test_a_wrong_marker_is_refused_and_said(graphed: tuple[World, MemoryGraph], monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    world, graph = graphed
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: MemoryGraph())  # 别的图：空的、没有我们的标记
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 1
    out = capsys.readouterr().out
    assert "一个字没写" in out and "先看连的是不是这本账的图" in out
    assert SHIP_15 in contents(graph)


def test_relation_vectors_follow_the_model_of_the_last_full_sync(graphed: tuple[World, MemoryGraph],
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    """没配向量服务：关系的向量照上一次整张同步用的模型查缓存，不照环境变量的默认模型（审阅第 4 条），
    不会多打一句“还欠一次整张同步”。"""
    world, graph = graphed
    conn = ledger(world)
    try:
        with transaction(conn):
            conn.execute("UPDATE graph_state SET projection = substr(projection, 1, instr(projection, '#')) || 'made-up-model'")
    finally:
        conn.close()
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[0]]) == 0
    out = capsys.readouterr().out
    assert "主图：同步，" in out and "还欠一次整张同步" not in out
    relations = {item["id"]: item["props"] for item in graph.iter_items("relation")}
    assert relations and {props["vector_model"] for props in relations.values()} <= {"made-up-model", "fake-embedding"}


def test_bad_graph_settings_say_nothing_for_a_ledger_without_a_graph(world: World, monkeypatch: pytest.MonkeyPatch,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """没接主图的账本：图的设置写错了也什么都不打、退出码照常（审阅第 5 条）。"""
    for packet in PACKETS:
        deliver(world, packet)
        write(world, rich_extract).close()
    monkeypatch.setenv("JAVMEM_GRAPH_SYNC", "maybe")
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    assert main(["purge", "--library", "prod", "--packet", PACKET_IDS[1], "--yes"]) == 0
    assert "主图" not in capsys.readouterr().out


def test_the_admin_hook_never_raises(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    del graph

    def broken(_library: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hook, "open_ledger", broken)
    report = hook.after_admin(world.settings, "prod")
    assert report.status == "error" and "OperationalError" in report.message
    assert report.status not in hook.FINE


def relation_texts(world: World) -> set[str]:
    """账本现在每条关系拿来算向量的那一句。"""
    conn = ledger(world)
    try:
        projector = Projector(conn, TZ)
        return {item.vector_text for item in projector.relations(projector.all_ids("relation")).values() if item.vector_text}
    finally:
        conn.close()


def cached(world: World, texts: set[str]) -> set[str]:
    with VectorCache(world.data / "vectors") as cache:
        keys = {row[0] for row in cache._conn.execute("SELECT vector_key FROM vectors")}
    return {text for text in texts if vector_key("fake-embedding", text) in keys}


def test_withdrawing_keeps_the_vectors_of_relations_still_there(graphed: tuple[World, MemoryGraph]) -> None:
    """以后要修 J5.1、J5.2 第 2 条：清向量缓存时，现在还在的关系的向量留着；撤回了的、变了的关系，旧的那一句删掉。"""
    world, graph = graphed
    before = relation_texts(world)
    assert before and cached(world, before) == before
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0  # 这一包没有关系
    assert relation_texts(world) == before and cached(world, before) == before
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[0]]) == 0  # 关系从这一包起
    after = relation_texts(world)
    assert cached(world, after) == after  # 剩下的、变了以后的（同步时算的）都在
    assert cached(world, before - after) == set()  # 撤回了的、变了的那几句删了
    assert_matches_ledger(world, graph)


# ---------- 彻底删除、解除撤回 ----------


def test_purge_says_the_disk_may_keep_what_was_deleted(graphed: tuple[World, MemoryGraph],
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    world, graph = graphed
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    capsys.readouterr()
    assert main(["purge", "--library", "prod", "--packet", PACKET_IDS[1], "--yes"]) == 0
    out = capsys.readouterr().out
    assert "彻底删除的这几版写过的东西，图里已经没有了" in out and "Neo4j 社区版删了的东西，磁盘上可能还留着" in out
    assert "javmem graph init --reset --library prod" in out and "javmem graph rebuild 只是清空重装" in out
    assert main(["purge", "--library", "prod", "--packet", PACKET_IDS[1], "--yes"]) == 0  # 早就删过了：不再说
    assert "磁盘上可能还留着" not in capsys.readouterr().out
    assert_matches_ledger(world, graph)


def test_purge_with_the_graph_down_says_both(graphed: tuple[World, MemoryGraph], monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    world, graph = graphed
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: Down())
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 1
    capsys.readouterr()
    assert main(["purge", "--library", "prod", "--packet", PACKET_IDS[1], "--yes", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["graph"]["status"] == "unavailable"
    assert "主图还没清" in captured.err and "这一次没同步成（见上一行）" in captured.err
    assert "磁盘上可能还留着" in captured.err


def test_unwithdrawing_syncs_too(graphed: tuple[World, MemoryGraph], capsys: pytest.CaptureFixture[str]) -> None:
    world, graph = graphed
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_IDS[1]]) == 0
    capsys.readouterr()
    assert main(["withdraw", "--library", "prod", "--undo", "--packet", PACKET_IDS[1]]) == 0
    out = capsys.readouterr().out
    assert "解除撤回 1 个包" in out and "主图：同步，" in out
    assert owed(world) == 0
    assert_matches_ledger(world, graph)


# ---------- 冻结、处理待确认、每天的提醒 ----------


def test_freezing_and_deciding_reach_the_graph(world: World, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    old, new = frozen_and_contradicted(world)  # 还没接图：冻结时不打主图那一行
    assert "主图" not in capsys.readouterr().out
    graph = connect_graph(world)
    run_sync(world, graph)
    plug(monkeypatch, graph)
    assert new not in facts_in(graph) and facts_in(graph)[old]["frozen"] is True  # 待确认的不进图
    review_id = int(reviews(world)[0]["review_id"])
    assert main(["review", "--library", "prod", "--decide", str(review_id), "--keep", "new"]) == 0
    assert "主图：同步，" in capsys.readouterr().out
    assert new in facts_in(graph) and facts_in(graph)[old]["frozen"] is False
    assert_matches_ledger(world, graph)
    assert main(["freeze", "--library", "prod", "--fact", new]) == 0
    assert "主图：同步，" in capsys.readouterr().out
    assert facts_in(graph)[new]["frozen"] is True
    assert main(["freeze", "--library", "prod", "--fact", new]) == 0  # 本来就冻结了：没有要同步的，不打那一行
    assert "主图" not in capsys.readouterr().out
    assert main(["freeze", "--library", "prod", "--fact", new, "--undo"]) == 0
    assert "主图：同步，" in capsys.readouterr().out
    assert facts_in(graph)[new]["frozen"] is False
    assert_matches_ledger(world, graph)


def test_the_daily_reminder_syncs_quietly(world: World, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    """30 天没人处理的照规矩两边都留：主图跟着同步，提醒里不打主图那一行（那是每天给主人看的）。"""
    old, new = frozen_and_contradicted(world)
    graph = connect_graph(world)
    run_sync(world, graph)
    plug(monkeypatch, graph)
    conn = ledger(world)
    try:
        with transaction(conn):
            conn.execute("UPDATE review_queue SET created_at = '2026-09-01T08:00:00+08:00'")
    finally:
        conn.close()
    capsys.readouterr()
    assert main(["review", "--remind", "--library", "prod", "--force"]) == 0
    out = capsys.readouterr().out
    assert "照规矩关掉了" in out and "主图" not in out
    assert new in facts_in(graph) and owed(world) == 0
    assert_matches_ledger(world, graph)


# ---------- 合并拆开 ----------


def test_merging_and_undoing_reach_the_graph(merged_world: tuple[World, Path], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                             capsys: pytest.CaptureFixture[str]) -> None:
    from test_merging import STANDARD, TYPO
    from test_merging_undo import typo_merge

    world, path = merged_world
    graph = connect_graph(world)
    run_sync(world, graph)
    plug(monkeypatch, graph)
    assert main([*typo_merge(world, path), "--note", "测试"]) == 0  # 人写的别名表里没有这一组：撤销以后分得开
    assert "主图：同步，" in capsys.readouterr().out
    entities = {item["id"] for item in graph.iter_items("entity")}
    assert TYPO not in entities and STANDARD in entities
    assert_matches_ledger(world, graph)
    assert main(["entities", "--library", "prod", "--undo", "M0001", "--note", "撤销测试", "--json"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # 标准输出只有 JSON
    assert "主图：同步，" in captured.err
    assert TYPO in {item["id"] for item in graph.iter_items("entity")}
    assert_matches_ledger(world, graph)
