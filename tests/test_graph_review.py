"""主图审阅（11-18）查出来的几处，写成“该是什么样”的测试。图是内存里的，名字和句子都是编的。
审阅的复现脚本照“现在错在哪”写的，这里照“改好了是什么样”写；修之前这几条都是红的。"""

from __future__ import annotations

from datetime import timezone

import pytest
from helpers import World, build_packet
from test_graph import PACKET_IDS, PACKETS, TZ, assert_matches_ledger, connect_graph, expected, ledger, rich_extract, run_sync
from test_write import day_packet, deliver, write

from javmem.cli import main
from javmem.embedding import EmbeddingReply, FakeEmbeddings, _estimate_tokens, fake_vector, pack_vector
from javmem.graph import dirty, neo
from javmem.graph.memory import MemoryGraph
from javmem.graph.project import Projector, scope_info
from javmem.graph.sync import RelationVectors, compare, expected_lines, export_lines, sync
from javmem.pipeline.vectors import VectorCache
from javmem.withdraw import withdraw


def test_an_item_written_before_a_lost_reply_is_still_removed_later(world: World) -> None:
    """审阅第 1 条：图里写进去了、回话丢了（账本没记上），之后账本里这条没了，同步照样把它从图里删掉。"""

    class ReplyLost(MemoryGraph):
        boom = False

        def upsert_facts(self, rows: object) -> None:
            super().upsert_facts(rows)  # type: ignore[arg-type]
            if self.boom:
                self.boom = False
                raise neo.GraphUnavailable("SessionExpired")

    graph = connect_graph(world, ReplyLost())
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    deliver(world, PACKETS[1])
    write(world, rich_extract).close()
    conn = ledger(world)
    new = conn.execute("SELECT fact_id FROM facts WHERE content LIKE '%11-15'").fetchone()[0]
    conn.close()
    graph.boom = True
    with pytest.raises(neo.GraphUnavailable):
        run_sync(world, graph)
    assert new in graph.nodes[neo.FACT]
    withdraw(world.settings, "prod", packets=[PACKET_IDS[1]])
    run_sync(world, graph)
    assert new not in graph.nodes[neo.FACT]
    assert_matches_ledger(world, graph)


def test_a_rebuild_cut_right_after_the_wipe_comes_back(world: World) -> None:
    """审阅第 2 条：整张重建断在清空图以后，下一次同步把图装回来，不会一直空着还报正常。"""

    class DropFails(MemoryGraph):
        fail_drop = False

        def drop_search_indexes(self) -> None:
            if self.fail_drop:
                self.fail_drop = False
                raise neo.GraphUnavailable("ServiceUnavailable")
            super().drop_search_indexes()

    graph = connect_graph(world, DropFails())
    for packet in PACKETS:
        deliver(world, packet)
    write(world, rich_extract).close()
    run_sync(world, graph)
    graph.fail_drop = True
    with pytest.raises(neo.GraphUnavailable):
        run_sync(world, graph, mode="rebuild")
    run_sync(world, graph)
    assert graph.nodes[neo.FACT]
    assert_matches_ledger(world, graph)


def test_a_cut_short_rebuild_gets_its_search_indexes_back(world: World) -> None:
    """审阅第 4 条：重建断在装了几批以后，下一次同步把全文、向量索引也建回来。"""
    graph = connect_graph(world)
    for packet in PACKETS:
        deliver(world, packet)
    write(world, rich_extract).close()
    graph.fail_after = 3
    with pytest.raises(neo.GraphUnavailable):
        run_sync(world, graph, chunk=1)
    graph.fail_after = None
    run_sync(world, graph)
    assert set(neo.SEARCH_INDEXES) <= graph.indexes
    graph.drop_search_indexes()  # 一点点同步的时候也补
    run_sync(world, graph)
    assert set(neo.SEARCH_INDEXES) <= graph.indexes


def test_replaces_follows_when_the_older_fact_leaves(world: World) -> None:
    """审阅第 6 条：被替代的那条变成待确认，替代它的那条“替代了谁”跟着变。"""
    graph = connect_graph(world)
    for packet in PACKETS[:3]:
        deliver(world, packet)
    write(world, rich_extract).close()
    run_sync(world, graph)
    conn = ledger(world)
    old_id, new_id = conn.execute("SELECT fact_id, superseded_by FROM facts WHERE content LIKE '%11-15'").fetchone()
    conn.execute("UPDATE facts SET status = 'pending', pending_reason = 'frozen_conflict' WHERE fact_id = ?", (old_id,))
    conn.close()
    run_sync(world, graph)
    assert graph.nodes[neo.FACT][new_id]["replaces"] == []
    assert_matches_ledger(world, graph)


LEX = """format: javmem-lexicon/2
default_where: [business, finance]
aliases:
  - type: 人
    names: [柏庭舟, 柏廷舟]
"""


def _books_packet(day: str, *lines: str) -> str:
    body = f"## 原话 · {day} 10:30–10:59\n" + "".join(f"- [10:{30 + i:02d}] {line}\n" for i, line in enumerate(lines))
    return build_packet(source="books-bot", stamp=f"{day}_2330", body=body, created_at=f"{day}T23:30:00+08:00",
                        covers_from=f"{day}T00:00:00+08:00", covers_to=f"{day}T23:30:00+08:00",
                        domain="[finance]", sensitive="[payroll]")


def test_the_standard_name_is_not_searchable_from_a_scope_that_cannot_see_it(world: World, tmp_path, monkeypatch) -> None:
    """审阅第 3 条：别名表一组两个写法，business 的包只写了一个，finance+payroll 的包只写了标准名那个。
    标准名进全文索引时只带看得到它的那个范围；business 范围里的叫法是 business 自己写的那个。"""
    from spotter import SpotModel, extract_into_store

    path = tmp_path / "config" / "lexicon.yaml"
    path.parent.mkdir()
    path.write_text(LEX, encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    world.drop("inbox/desk-bot", day_packet("2026-09-01", "我：柏廷舟负责资金池。"))
    world.drop("inbox/books-bot", _books_packet("2026-09-06", "我：柏庭舟以后负责报价。"))
    world.intake()
    extract_into_store(world, SpotModel([("柏廷舟", [("柏廷舟", "人", "柏廷舟")]), ("柏庭舟", [("柏庭舟", "人", "柏庭舟")])],
                                        [("资金池", "负责", "资金池"), ("报价", "负责", "报价")]))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    graph = connect_graph(world)
    run_sync(world, graph)
    business = scope_info('["business"]', "[]").scope_id
    payroll = scope_info('["finance"]', '["payroll"]').scope_id
    standard = next(item for item in graph.iter_items("entity") if item["props"]["name"] == "柏庭舟")
    assert business in standard["props"]["scope_ids"]  # 实体在 business 里看得到（那边有它的事）
    assert standard["props"]["name_scope_ids"] == [payroll]  # 标准名只在 payroll 那个范围看得到
    assert standard["props"]["name_scope_text"] == payroll.lower()
    assert "n.scope_text" not in neo.FULLTEXT_INDEXES["jm_entity_name"] and "n.name_scope_text" in neo.FULLTEXT_INDEXES["jm_entity_name"]
    conn = ledger(world)
    try:
        projector = Projector(conn, TZ)
        assert projector.name_in_scope(standard["id"], business) == "柏廷舟"
        other = scope_info('["research"]', "[]").scope_id
        assert projector.name_in_scope(standard["id"], other) == "〔人〕"  # 标准名只在别的范围有出处：不拿来用
    finally:
        conn.close()
    aliases = {item["props"]["name"]: item["props"] for item in graph.iter_items("alias")}
    for props in aliases.values():
        assert len(props["first_seen_by_scope"]) == len(props["scope_ids"]) and "first_seen" not in props
    assert_matches_ledger(world, graph)


def test_one_lost_trigger_is_noticed(world: World) -> None:
    """审阅第 5 条：触发器丢了几个（以后的迁移重建一张表就会这样），同步认出来、补上、整张同步一遍。"""
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    conn = ledger(world)
    fact_id = conn.execute("SELECT fact_id FROM facts WHERE content = '周建国负责越南那批订单'").fetchone()[0]
    conn.execute("DROP TRIGGER graph_mark_fact_update")
    conn.execute("UPDATE facts SET status = 'pending', pending_reason = 'manual_review' WHERE fact_id = ?", (fact_id,))
    conn.close()
    report = run_sync(world, graph)
    assert report.mode == "all" and fact_id not in graph.nodes[neo.FACT]
    conn = ledger(world)
    try:
        assert dirty.installed(conn) == len(dirty.expected_triggers())
    finally:
        conn.close()
    assert_matches_ledger(world, graph)


def test_a_timezone_change_goes_full(world: World) -> None:
    """审阅第 8 条：换了 JAVMEM_TIMEZONE（照精度换成的时刻跟着变），同步认出来、整张算一遍。"""
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    conn = ledger(world)
    try:
        with VectorCache(world.data / "vectors") as cache:
            report = sync(conn, graph, library="prod", tz=timezone.utc, vectors=RelationVectors(cache, FakeEmbeddings()))
            want = list(expected_lines(conn, tz=timezone.utc, vectors=RelationVectors(cache, FakeEmbeddings()), library="prod"))
    finally:
        conn.close()
    assert report.mode == "all"
    assert compare(export_lines(graph), iter(want)) == {"only_graph": [], "only_ledger": [], "different": []}


def test_relation_vectors_follow_the_embedding_model(world: World) -> None:
    """审阅第 7 条：换了向量模型，关系的向量照新模型重算。"""

    class Other(FakeEmbeddings):
        def embed(self, texts):  # type: ignore[no-untyped-def]
            self.calls.append(list(texts))
            return EmbeddingReply([pack_vector(fake_vector("other:" + text)) for text in texts], _estimate_tokens(texts), 0.0)

    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    (rel,) = list(graph.iter_items("relation"))
    first = rel["vector"]
    other = Other(name="real-model")
    run_sync(world, graph, embedder=other)
    (rel,) = list(graph.iter_items("relation"))
    assert other.calls and rel["vector"] != first and rel["props"]["vector_model"] == "real-model"


def test_relation_vectors_come_from_the_cache_without_a_service(world: World) -> None:
    """审阅第 9 条：没配向量服务时也先查缓存（模型名照设置），缓存里有的不算欠着。"""
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)  # 假向量进了缓存
    graph_two = MemoryGraph()
    conn = ledger(world)
    try:
        from javmem.graph.sync import enable

        enable(conn, graph_two.create_marker("prod", "x"))
        with VectorCache(world.data / "vectors") as cache:
            report = sync(conn, graph_two, library="prod", tz=TZ, vectors=RelationVectors(cache, None, model="fake-embedding"))
    finally:
        conn.close()
    assert report.vectors_owed == 0
    (rel,) = list(graph_two.iter_items("relation"))
    assert rel["vector"] is not None


def test_the_write_hook_leaves_full_syncs_to_the_command(spot_world: World, monkeypatch, capsys) -> None:
    """审阅第 9 条：写完账接着同步碰上要整张的（init 以后头一回、换了版本），不在写账里整张装，叫人跑 javmem graph sync。"""
    graph = connect_graph(spot_world)
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "x" * 16)
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: graph)
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    out = capsys.readouterr().out
    assert "要整张同步" in out and "javmem graph sync --library prod" in out
    assert not graph.nodes[neo.FACT]
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 0
    assert "整张重建" in capsys.readouterr().out
    assert_matches_ledger(spot_world, graph)


def test_fake_relation_vectors_are_refused_on_a_ledger_with_real_vectors(world: World, monkeypatch, capsys) -> None:
    """审阅第 7 条：账本里已经有真向量的，graph sync --fake-embeddings 不让跑（写账那边也是这样挡的）。"""
    graph = connect_graph(world)
    deliver(world, PACKETS[0])
    write(world, rich_extract, embedder=FakeEmbeddings(name="real-model")).close()
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "x" * 16)
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: graph)
    assert main(["graph", "sync", "--library", "prod", "--fake-embeddings"]) == 2
    assert "真向量" in capsys.readouterr().out and not graph.nodes[neo.FACT]


from test_graph import spot_world  # noqa: E402,F401


# ---------- 第二遍审阅（修过的部分） ----------


def _zhou_extract(window):  # type: ignore[no-untyped-def]
    """写着“负责越南那批订单”的那一行：一条事实，主体照原文写的叫法（全名或者“老周”），标准名都是“周建国”。"""
    from test_graph import ORDER
    from test_write import l3_fact

    facts, found = [], {}
    for line in window.targets:
        text = line.text.rstrip("。")
        if "负责越南那批订单" in text:
            mention = "周建国" if "周建国" in text else "老周"
            found["z"] = {"key": "z", "mention": mention, "name": "周建国", "type": "人"}
            found["o"] = ORDER
            facts.append(l3_fact(window, text, f"{mention}负责越南那批订单", subject="z", attribute="负责的订单",
                                 value="越南那批订单", entities=[{"key": "z", "role": "subject"}, {"key": "o", "role": "object"}]))
    return {"entities": list(found.values()), "facts": facts, "relations": []}


def test_a_renamed_standard_name_is_judged_by_its_new_spelling(world: World) -> None:
    """第二遍审阅第 1 条：撤回让标准名改了名（只剩“老周”有出处），账本里对齐用的写法没跟着改；图里照新的标准名算它在哪个
    范围看得到，不留旧名字的写法。"""
    deliver(world, day_packet("2026-11-01", "我：周建国负责越南那批订单。"), day_packet("2026-11-03", "我：老周负责越南那批订单。"))
    write(world, _zhou_extract).close()
    graph = connect_graph(world)
    run_sync(world, graph)
    withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
    write(world, _zhou_extract).close()  # 改回排队的包照结果库重写
    run_sync(world, graph)
    (person,) = [item for item in graph.iter_items("entity") if item["props"]["type"] == "人"]
    business = scope_info('["business"]', "[]").scope_id
    assert person["props"]["name"] == "老周" and "name_key" not in person["props"]
    assert person["props"]["name_scope_ids"] == [business]
    assert "jm_entity_key" not in neo.RANGE_INDEXES
    assert_matches_ledger(world, graph)


def test_a_vector_from_a_lost_write_goes_when_the_ledger_drops_it(world: World) -> None:
    """第二遍审阅第 2 条：带着向量写进了图、回话丢了，下一次同步以前账本里这条的向量没了：图里的向量也拿掉。"""

    class ReplyLost(MemoryGraph):
        boom = False

        def upsert_facts(self, rows: object) -> None:
            super().upsert_facts(rows)  # type: ignore[arg-type]
            if self.boom:
                self.boom = False
                raise neo.GraphUnavailable("SessionExpired")

    graph = connect_graph(world, ReplyLost())
    deliver(world, PACKETS[0])
    write(world, rich_extract).close()
    run_sync(world, graph)
    deliver(world, PACKETS[1])
    write(world, rich_extract).close()
    conn = ledger(world)
    new = conn.execute("SELECT fact_id FROM facts WHERE content LIKE '%11-15'").fetchone()[0]
    conn.close()
    graph.boom = True
    with pytest.raises(neo.GraphUnavailable):
        run_sync(world, graph)
    assert new in graph.vectors
    conn = ledger(world)
    conn.execute("DELETE FROM fact_vectors WHERE fact_id = ?", (new,))
    conn.close()
    run_sync(world, graph)
    assert new not in graph.vectors
    assert_matches_ledger(world, graph)
