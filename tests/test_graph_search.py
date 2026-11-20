"""主图检索（J5.4，javmem.graph.search）。名字、句子都是编的（tests/query_world.py 照查询接口文档编的那个小世界）。

不要 Neo4j 的（CI 两路都跑）在前面：范围编号、Lucene 查询、合起来排、枢纽、向量那一路多取再滤、问话的向量、看不到的叫法
换掉以后数词、开关。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import query_world
from helpers import World, as_json
from neo4j.exceptions import Neo4jError
from query_world import AS_OF, build_world, chat, day_body, write_all
from query_world import extract as world_extract
from test_graph import run_sync
from test_graph_neo4j import PASSWORD, URI, _target, neo_ready, real  # noqa: F401 - 要真图的测试共用这两个 fixture
from test_relcheck import relation
from test_write import day_packet, deliver, l3_fact
from test_write import write as write_ledger

from javmem.config import ConfigError
from javmem.embedding import EmbeddingReply, FakeEmbeddings, pack_vector
from javmem.graph import neo
from javmem.graph import search as gs
from javmem.graph.config import GraphSettings, GraphTarget, search_switch
from javmem.graph.project import PROJECTION_VERSION, scope_info
from javmem.graph.sync import enable, projection_key, read_state
from javmem.pipeline.window import Window
from javmem.query import graphhook
from javmem.query.graphhook import GraphRequest, Hit
from javmem.query.tools import memory_search
from javmem.withdraw import withdraw


@pytest.fixture(scope="module")
def qworld(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(tmp_path_factory.mktemp("graph-search"))


def captured_request(world: World, monkeypatch: pytest.MonkeyPatch, caller: str = "desk-bot", **arguments: object) -> GraphRequest:
    """memory_search 交给检索器的那个 GraphRequest（服务照真的算）。"""
    requests: list[GraphRequest] = []

    class Capture:
        def search(self, request: GraphRequest) -> list[Hit]:
            requests.append(request)
            return []

    monkeypatch.setattr(graphhook, "open_searcher", lambda settings, library: Capture())
    memory_search({"query": "越南那批订单最近怎么样", **arguments}, settings=replace(world.settings, caller=caller))
    (request,) = requests
    return request


# ---------- 范围编号 ----------


def test_scope_ids_are_the_ones_j52_writes_into_the_graph(qworld: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """服务给的（分区、敏感标记）换成的编号，和 J5.2 照事实、关系的范围算出来写进图里的是同一个。"""
    rows = qworld.rows("SELECT scope_partitions, scope_sensitive FROM facts UNION SELECT scope_partitions, scope_sensitive FROM relations")
    by_scope = {(row[0], row[1]): scope_info(row[0], row[1]).scope_id for row in rows}
    business = by_scope[('["business"]', "[]")]
    payroll = by_scope[('["finance"]', '["payroll"]')]
    desk = gs.visible_scope_ids(captured_request(qworld, monkeypatch))
    assert desk == [business]
    owner = gs.visible_scope_ids(captured_request(qworld, monkeypatch, "owner-console"))
    assert {business, payroll} <= set(owner)
    finance = captured_request(qworld, monkeypatch, "owner-console", partitions=["finance"])
    assert gs.visible_scope_ids(finance) == [payroll]
    # 入参 partitions 服务已经算进去了；检索器再筛一遍也一样
    assert gs.visible_scope_ids(replace(captured_request(qworld, monkeypatch, "owner-console"), partitions=("finance",))) == [payroll]


# ---------- 过滤的写法 ----------


def test_filters_carry_the_request() -> None:
    from datetime import datetime, timezone

    request = GraphRequest(
        library="prod", question="q", words=(), seeds=(), scopes=((("business",), ()),), partitions=None, time_from=10,
        time_to=20, include_history=True, as_of=datetime.fromtimestamp(1_000, timezone.utc), want=5, ledger_hits=(),
        hidden_in=lambda items: set(),
    )
    filters = gs.Filters.of(request)
    assert filters.params() == {"scopes": gs.visible_scope_ids(request), "t_from": 10, "t_to": 20, "history": True, "as_of": 1_000}
    for text in (gs.fact_filter("f"), gs.relation_filter("r")):
        assert "$scopes" in text and "$history" in text and "$t_from" in text and "$t_to" in text
    assert "$as_of" in gs.relation_filter("r")


# ---------- Lucene 查询 ----------


def test_lucene_query_quotes_words_and_puts_scopes_inside() -> None:
    query = gs.lucene_query(["content"], ["越南订单", 'a"b\\c', "越南订单", "  ", "Nov  15"], ["Sabc", "Sdef"])
    assert query == '+scope_text:(sabc sdef) +(content:"越南订单" content:"a\\"b\\\\c" content:"Nov 15")'
    both = gs.lucene_query(["content", "details"], ["舱位"], ["S1"])
    assert both == '+scope_text:(s1) +(content:"舱位" details:"舱位")'
    assert gs.lucene_query(["content"], [], ["S1"]) is None
    assert gs.lucene_query(["content"], ["舱位"], []) is None  # 一个看得到的范围都没有：不查


def test_lucene_query_keeps_at_most_128_words_and_special_characters_stay_inside_quotes() -> None:
    words = [f"词{index}" for index in range(300)]
    query = gs.lucene_query(["content"], words, ["S1"], max_terms=128)
    assert query is not None and query.count("content:") == 128 and '"词127"' in query and '"词128"' not in query
    tricky = gs.lucene_query(["content"], ["AND", "OR NOT", "a:b", "(x)", "*?~^", "[1 TO 2]", "+-!/"], ["S1"])
    assert tricky == ('+scope_text:(s1) +(content:"AND" content:"OR NOT" content:"a:b" content:"(x)" content:"*?~^"'
                      ' content:"[1 TO 2]" content:"+-!/")')
    many = gs.lucene_query(["content"], ["舱位"], [f"S{index}" for index in range(gs.MAX_LUCENE_SCOPES + 1)])
    assert many == '+(content:"舱位")'  # 范围太多就只在后面的条件里滤


# ---------- 合起来排 ----------


def test_rrf_adds_up_ranks_and_ties_go_to_the_newest_then_the_id() -> None:
    entity = [gs.Found("fact", "F1", 100), gs.Found("relation", "R1", 300)]
    fulltext = [gs.Found("relation", "R1", 300), gs.Found("fact", "F2", 200)]
    ledger = [gs.Found("fact", "F3"), gs.Found("fact", "F1")]
    hits = gs.merge_routes([entity, fulltext, [], ledger], k=60, want=10, said={("fact", "F3"): 50})
    # R1：1/62 + 1/61；F1：1/61 + 1/62（一样，R1 新）；F2、F3 各一个第 2 名、第 1 名
    assert [(hit.kind, hit.id) for hit in hits] == [("relation", "R1"), ("fact", "F1"), ("fact", "F3"), ("fact", "F2")]
    assert hits[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert gs.merge_routes([entity, fulltext, [], ledger], k=60, want=2, said={}) == hits[:2]
    # 一样的分、一样新的，按哪一样、编号
    tied = gs.merge_routes([[gs.Found("relation", "R9", 5)], [gs.Found("fact", "F9", 5)]], k=60, want=5)
    assert [hit.id for hit in tied] == ["F9", "R9"]
    # 一路里同一条只算第一次
    once = gs.merge_routes([[gs.Found("fact", "F1"), gs.Found("fact", "F1"), gs.Found("fact", "F2")]], k=60, want=5)
    assert [(hit.id, hit.score) for hit in once] == [("F1", pytest.approx(1 / 61)), ("F2", pytest.approx(1 / 62))]
    assert gs.merge_routes([[], [], [], []], k=60, want=5) == []


def test_three_way_ties_are_exact() -> None:
    """三路的名次一样多、先后不一样，分数也一样（用分数算，不让小数加的先后定输赢）。"""
    a = [gs.Found("fact", "F1", 1), gs.Found("fact", "F2", 1), gs.Found("fact", "F3", 1)]
    b = [gs.Found("fact", "F3", 1), gs.Found("fact", "F1", 1), gs.Found("fact", "F2", 1)]
    c = [gs.Found("fact", "F2", 1), gs.Found("fact", "F3", 1), gs.Found("fact", "F1", 1)]
    hits = gs.merge_routes([a, b, c], k=60, want=3)
    assert [hit.id for hit in hits] == ["F1", "F2", "F3"]
    assert hits[0].score == hits[1].score == hits[2].score


def test_entity_and_vector_routes_rank_by_similarity_then_hops_then_newest() -> None:
    items = [
        gs.Scored("fact", "F1", 0.5, 10, hop=1),
        gs.Scored("fact", "F2", 0.5, 20, hop=1),
        gs.Scored("fact", "F3", 0.5, 30, hop=2),
        gs.Scored("relation", "R1", 0.9, 5, hop=2),
        gs.Scored("fact", "F4", None, 99, hop=0),
        gs.Scored("fact", "F1", 0.4, 10, hop=0),  # 同一条又从 0 跳找到：留跳数少的
    ]
    assert [one.id for one in gs.rank_scored(items, 10)] == ["R1", "F2", "F3", "F1", "F4"]
    assert [one.id for one in gs.rank_scored(items, 2)] == ["R1", "F2"]
    plain = [gs.Scored("fact", "F1", None, 10, hop=1), gs.Scored("fact", "F2", None, 20, hop=0), gs.Scored("fact", "F3", None, 30, hop=1)]
    assert [one.id for one in gs.rank_scored(plain, 10)] == ["F2", "F3", "F1"]  # 没有问话的向量：跳数、新旧


# ---------- 枢纽 ----------


def test_hubs_are_the_owner_and_bots(qworld: World) -> None:
    owner = qworld.rows("SELECT entity_id, type FROM entities WHERE canonical_name = '主人'")
    if owner:
        assert owner[0]["entity_id"] == gs.OWNER_ID
    assert gs.is_hub(gs.OWNER_ID, "人") and gs.is_hub("E123", "Bot")
    assert not gs.is_hub("E123", "人") and not gs.is_hub("E123", None)


# ---------- 向量那一路：多取再滤 ----------


class FakeVectorIndex:
    """假的向量索引：items 是 (编号, 余弦, 看不看得到)，最像的在前；看不到的过不了滤（看不到、时间不对都是这样）。"""

    def __init__(self, items: list[tuple[str, float, bool]]) -> None:
        self.items = sorted(items, key=lambda one: -one[1])
        self.asked: list[int] = []

    def fetch(self, k: int) -> tuple[int, float | None, list[gs.Scored]]:
        self.asked.append(k)
        top = self.items[:k]
        kept = [gs.Scored("fact", item_id, score, 0) for item_id, score, seen in top if seen and score >= 0.35]
        return len(top), (top[-1][1] if top else None), kept


def visible(count: int, score: float = 0.9) -> list[tuple[str, float, bool]]:
    return [(f"F{index:05d}", score - index / 100_000, True) for index in range(count)]


def hidden(count: int, score: float = 0.95) -> list[tuple[str, float, bool]]:
    return [(f"H{index:05d}", score, False) for index in range(count)]


def test_the_vector_route_keeps_fetching_until_enough_visible_ones() -> None:
    """看不到的挤在最前面 1,000 条：照样取到够 50 条，名次、条数和没有那些看不到的时一样。"""
    alone = FakeVectorIndex(visible(3000))
    crowded = FakeVectorIndex(hidden(1000) + visible(3000))
    first, short = gs.gather(alone.fetch, theta=0.35)
    second, crowded_short = gs.gather(crowded.fetch, theta=0.35)
    assert alone.asked == [200] and crowded.asked == [200, 800, 3200] and not short and not crowded_short
    assert gs.rank_scored(first, 50) == gs.rank_scored(second, 50)
    assert [one.id for one in gs.rank_scored(second, 3)] == ["F00000", "F00001", "F00002"]


def test_the_vector_route_stops_when_everything_was_taken_or_below_theta_or_at_the_cap() -> None:
    small = FakeVectorIndex(hidden(100) + visible(20))
    kept, short = gs.gather(small.fetch, theta=0.35)
    assert small.asked == [200] and len(kept) == 20 and not short  # 图里一共不到 200 条：全取了
    low = FakeVectorIndex(visible(10) + [(f"L{index:05d}", 0.2, True) for index in range(5000)])
    kept, short = gs.gather(low.fetch, theta=0.35)
    assert low.asked == [200] and len(kept) == 10 and not short  # 再往后都低于 θ：不再取
    full = FakeVectorIndex(hidden(19990) + visible(10))
    kept, short = gs.gather(full.fetch, theta=0.35)
    assert full.asked == [200, 800, 3200, 10000] and kept == [] and short  # 取到 10,000 还不够：记下来


# ---------- 问话的向量 ----------


class CountingEmbedder(FakeEmbeddings):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.texts.extend(texts)
        return super().embed(texts)


class BrokenEmbedder(FakeEmbeddings):
    def embed(self, texts):  # type: ignore[no-untyped-def]
        raise OSError("服务没开")


def test_query_vectors_use_the_qwen3_query_form_and_remember_recent_questions() -> None:
    embedder = CountingEmbedder()
    vectors = gs.QueryVectors(embedder, size=2)
    first = vectors.vector("越南那批订单最近怎么样")
    assert first is not None and len(first) == 2560
    assert embedder.texts == [gs.query_text("越南那批订单最近怎么样")]
    assert embedder.texts[0] == ("Instruct: Given a question the user asks about their own past conversations, retrieve memory"
                                 " facts that help answer it\nQuery:越南那批订单最近怎么样")
    assert vectors.vector("越南那批订单最近怎么样") == first and len(embedder.texts) == 1
    vectors.vector("第二句")
    vectors.vector("第三句")  # 只记两句：第一句挤掉了
    vectors.vector("越南那批订单最近怎么样")
    assert len(embedder.texts) == 4


class CountingBroken(BrokenEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().embed(texts)


def test_without_the_embedding_service_there_is_no_vector_and_one_log_line() -> None:
    said: list[str] = []
    broken = gs.QueryVectors(BrokenEmbedder(), say=said.append)
    assert broken.vector("越南那批订单最近怎么样") is None and broken.failures == 1
    assert said == ["问话的向量没算出来（OSError），30 秒里不走向量那一路"]
    assert "越南" not in said[0]
    assert gs.QueryVectors(None).vector("越南那批订单最近怎么样") is None


def test_after_the_embedding_service_fails_it_is_left_alone_for_a_while() -> None:
    """连不上以后这一阵不再等它（不然每一句都要等满超时，审阅第 7 条）。"""
    embedder = CountingBroken()
    vectors = gs.QueryVectors(embedder, say=lambda text: None, cooldown=30)
    assert vectors.vector("第一句") is None and vectors.vector("第二句") is None
    assert embedder.calls == 1
    vectors._skip_until = 0.0  # 过了这一阵
    assert vectors.vector("第三句") is None and embedder.calls == 2


# ---------- 全文：数词 ----------


def test_words_are_counted_like_the_ledger_view_does() -> None:
    """对上几个词照比对写法（和账本视图的 _contains 一样）；同一个词只算一次。"""
    assert gs.words_in("越南订单的定金到账", ["定金", "越南订单", "定金", "尾款"]) == 2
    assert gs.words_in("ＡＢＣ 公司的 Shipment", ["abc", "shipment", "舱位"]) == 2


# ---------- 开关 ----------


def test_the_search_switch() -> None:
    assert GraphSettings.from_env({}).search is True
    assert GraphSettings.from_env({"JAVMEM_GRAPH_SEARCH": "off"}).search is False
    assert GraphSettings.from_env({"JAVMEM_GRAPH_SEARCH": "on", "JAVMEM_GRAPH_SYNC": "off"}).describe()["search"] is True
    # 写错了当 off，不报配置错误：不连累写账以后的同步（审阅第 4 条）
    assert GraphSettings.from_env({"JAVMEM_GRAPH_SEARCH": "maybe"}).search is False
    assert search_switch({"JAVMEM_GRAPH_SEARCH": "maybe"}) == (False, "JAVMEM_GRAPH_SEARCH 要写成 on 或 off，这次当 off")
    assert search_switch({}) == (True, None) and search_switch({"JAVMEM_GRAPH_SEARCH": "0"}) == (False, None)
    # 反过来，写账的开关写错了，查询那一边不核它
    with pytest.raises(ConfigError, match="JAVMEM_GRAPH_SYNC"):
        GraphSettings.from_env({"JAVMEM_GRAPH_SYNC": "maybe"})
    assert GraphSettings.from_env({"JAVMEM_GRAPH_SYNC": "maybe"}, check_sync=False).search is True


def test_params_are_written_down_with_the_version() -> None:
    described = gs.GS2.describe()
    # gs2（D201）：排名的参数和 gs1 一样，只换了时限
    assert described["version"] == "gs2" and described["per_route"] == 80 and described["rrf_k"] == 60
    assert not hasattr(gs, "GS1")  # 旧名字不留着：用旧名字以后对数会糊涂
    assert described["theta"] == 0.27 and described["hops"] == 2
    assert described["vector_first"] == 200 and described["vector_max"] == 10_000
    # 不再每一路 1.5 秒、超了扔掉这一路（D196）：整个这一步 30 秒，出事才用
    assert described["search_seconds"] == 30.0 and "route_seconds" not in described
    assert pack_vector([1.0] * 2560)  # 问话的向量和事实的向量同一个维度


# ---------- 出错 ----------


class FakeTimeout(Neo4jError):
    code = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"  # 5.26 事务超时回的就是这个


class FakeBroken(Neo4jError):
    code = "Neo.ClientError.Procedure.ProcedureCallFailed"


class FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def data(self) -> list[dict]:
        return self.rows


class FakeSession:
    """记下每一句查询给的超时；每一句“花” spend 秒（拨假钟）。"""

    def __init__(self, clock: list[float], spend: float = 0.0) -> None:
        self.clock = clock
        self.spend = spend
        self.timeouts: list[float] = []

    def run(self, query, params):  # type: ignore[no-untyped-def]
        self.timeouts.append(query.timeout)
        self.clock[0] += self.spend
        return FakeResult([{"one": 1}])


def test_a_broken_route_is_dropped_and_a_timeout_drops_the_whole_graph_step() -> None:
    """图那边报别的错（不是超时）只丢那一路，别的照回：同样的问话每次都一样错。超时（Neo4j 到点掐掉）整个主图这一步不要，
    不悄悄少一路（D196）。"""
    said: list[str] = []
    searcher = gs.GraphSearch(GraphTarget("prod", "bolt://127.0.0.1:7688", "neo4j", "made-up-pass"), marker_id="m", say=said.append)
    budgets: list[float] = []

    def slow(budget: gs.Budget) -> list[gs.Found]:
        raise FakeTimeout("太慢")

    def broken(budget: gs.Budget) -> list[gs.Found]:
        raise FakeBroken("坏了")

    def fine(budget: gs.Budget) -> list[gs.Found]:
        budgets.append(budget.left)
        return [gs.Found("fact", "F1")]

    budget = gs.Budget(object(), searcher.params.search_seconds)
    assert searcher._route(budget, "vector", broken, []) == []
    assert searcher._route(budget, "entity", fine, []) == [gs.Found("fact", "F1")]
    assert budgets == [30.0]  # 整个这一步的查询最多 30 秒
    assert said == ["vector 那一路出错（Neo.ClientError.Procedure.ProcedureCallFailed），这次不要它"]
    assert searcher.stats == {"vector_error": 1}
    with pytest.raises(graphhook.GraphUnavailable, match="timeout"):
        searcher._route(budget, "fulltext", slow, [])
    assert said[-1] == "主图这一步的查询超过 30 秒（停在 fulltext 那一路），这次只用账本视图；30 秒里不再去查"
    assert searcher.stats == {"vector_error": 1, "timeout": 1} and searcher._down_until > 0


def test_the_budget_counts_only_time_spent_in_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """每一句查询的超时是这一步剩下的；查询以外（问服务名字）花的时间不算；用完了再查就当这一步超时（复审第 4 条）。"""
    clock = [100.0]
    monkeypatch.setattr(gs.time, "monotonic", lambda: clock[0])
    session = FakeSession(clock, spend=0.6)
    budget = gs.Budget(session, 1.5)
    assert budget.run("RETURN 1 AS one") == [{"one": 1}]
    clock[0] += 30  # 查询以外的时间（问服务名字）：不算
    budget.run("RETURN 1 AS one")
    assert session.timeouts == [1.5, pytest.approx(0.9)] and budget.left == pytest.approx(0.3)
    budget.run("RETURN 1 AS one")
    with pytest.raises(gs.OutOfTime):
        budget.run("RETURN 1 AS one")
    searcher = gs.GraphSearch(GraphTarget("prod", "bolt://127.0.0.1:7688", "neo4j", "made-up-pass"), marker_id="m",
                              say=lambda text: None)

    def used_up(budget: gs.Budget) -> list[gs.Found]:
        raise gs.OutOfTime

    with pytest.raises(graphhook.GraphUnavailable, match="timeout"):
        searcher._route(budget, "fulltext", used_up, [])
    assert searcher.stats == {"timeout": 1}


class TimedSession:
    """假的只读会话：每一句查询“花”几秒照查询的字查表（拨假钟），没写的不花时间；花的比给的超时还多，就像 Neo4j 那样
    到点掐掉、抛超时。核标记那一句回检索器要的那个标记。"""

    def __init__(self, clock: list[float], costs: dict[str, float]) -> None:
        self.clock = clock
        self.costs = costs
        self.timeouts: list[float] = []

    def __enter__(self) -> TimedSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, query, params=None):  # type: ignore[no-untyped-def]
        self.timeouts.append(query.timeout)
        cost = self.costs.get(query.text, 0.0)
        if cost > query.timeout:
            self.clock[0] += query.timeout
            raise FakeTimeout("到点掐掉")
        self.clock[0] += cost
        if query.text == gs._MARKER:
            return FakeResult([{"id": "m", "library": "prod", "kind": neo.KIND}])
        return FakeResult([])


ROUTE_QUERIES = {"entity": ["实体 1", "实体 2"], "fulltext": ["全文 事", "全文 关系"], "vector": ["向量 事", "向量 关系"]}
ROUTE_FOUND = {
    "entity": [gs.Found("fact", "F1", 50), gs.Found("relation", "R1", 40)],
    "fulltext": [gs.Found("fact", "F2", 30), gs.Found("fact", "F1", 50)],
    "vector": [gs.Found("fact", "F3", 20), gs.Found("relation", "R1", 40)],
}


def timed_searcher(monkeypatch: pytest.MonkeyPatch, costs: dict[str, float],
                   said: list[str] | None = None) -> tuple[gs.GraphSearch, TimedSession, list[float]]:
    """一个检索器：会话换成 TimedSession（假钟），三路换成照 ROUTE_QUERIES 跑几句查询、再回 ROUTE_FOUND 的假的，问话的向量
    是现成的。"""
    clock = [1_000.0]
    monkeypatch.setattr(gs.time, "monotonic", lambda: clock[0])
    searcher = gs.GraphSearch(GraphTarget("prod", "bolt://127.0.0.1:1", "neo4j", "made-up-pass"), marker_id="m",
                              say=said.append if said is not None else lambda text: None)
    session = TimedSession(clock, costs)
    monkeypatch.setattr(searcher, "_session", lambda: session)
    monkeypatch.setattr(searcher.vectors, "vector", lambda question: [1.0])

    def route(name: str):  # type: ignore[no-untyped-def]
        def work(budget: gs.Budget, *rest: object) -> list[gs.Found]:
            for text in ROUTE_QUERIES[name]:
                budget.run(text)
            return ROUTE_FOUND[name]

        return work

    for name in ROUTE_QUERIES:
        monkeypatch.setattr(searcher, f"_{name}_route", route(name))
    return searcher, session, clock


def plain_request() -> GraphRequest:
    return GraphRequest(
        library="prod", question="越南那批订单", words=("越南那批订单",), seeds=("E-made-up",), scopes=((("business",), ()),),
        partitions=None, time_from=None, time_to=None, include_history=False, as_of=AS_OF, want=10, ledger_hits=(),
        hidden_in=lambda items: set(),
    )


def test_a_slow_route_is_waited_for_so_the_same_question_gets_the_same_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """全文那一路两句查询各花 1 秒（机器忙、缓存冷）：照样等它跑完，名次和不慢的时候一样，不记超时。以前每一路只给 1.5 秒、
    超了整路扔掉，同一句话快的时候、慢的时候答的不一样（D196）。"""
    fast, _, _ = timed_searcher(monkeypatch, {})
    expected = fast.search(plain_request())
    assert {hit.id for hit in expected} == {"F1", "F2", "F3", "R1"}
    said: list[str] = []
    slow, session, clock = timed_searcher(monkeypatch, {"全文 事": 1.0, "全文 关系": 1.0}, said)
    assert slow.search(plain_request()) == expected
    assert clock[0] == pytest.approx(1_002.0)
    assert not slow.stats and not said
    # 每一句的超时是这一步剩下的：30 秒减去前面的查询花的
    assert session.timeouts[0] == 30.0 and session.timeouts[-1] == pytest.approx(28.0)


def test_a_graph_step_over_30_seconds_is_not_used_and_the_graph_rests_a_while(monkeypatch: pytest.MonkeyPatch) -> None:
    """出事了（图卡住、太忙），这一步的查询加起来过了 30 秒：到点掐掉，这次整个主图不用（抛 GraphUnavailable，服务照账本
    视图答，degraded: ["graph"]），记一行、数进 stats；30 秒里再来的直接当连不上，不再一句句等满。"""
    said: list[str] = []
    searcher, session, clock = timed_searcher(monkeypatch, {"实体 2": 12.0, "全文 关系": 20.0}, said)
    with pytest.raises(graphhook.GraphUnavailable, match="timeout"):
        searcher.search(plain_request())
    assert session.timeouts[-1] == pytest.approx(18.0)  # 到“全文 关系”那一句只剩 18 秒
    assert searcher.stats == {"timeout": 1}
    assert said == ["主图这一步的查询超过 30 秒（停在 fulltext 那一路），这次只用账本视图；30 秒里不再去查"]
    asked = len(session.timeouts)
    with pytest.raises(graphhook.GraphUnavailable, match="cooldown"):
        searcher.search(plain_request())
    assert len(session.timeouts) == asked and searcher.stats["cooldown"] == 1  # 没再去查
    clock[0] += 30
    session.costs.clear()
    assert {hit.id for hit in searcher.search(plain_request())} == {"F1", "F2", "F3", "R1"}


def test_memory_search_answers_from_the_ledger_view_when_the_graph_step_runs_out_of_time(
        qworld: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 30 秒的那一次，memory_search 照账本视图答：和主图没接的时候一字不差，degraded: ["graph"]。记的那一行只有种类，
    没有问话、没有内容（D201）；30 秒里再问，不再去查主图，照样照账本视图答。"""
    question = {"query": "越南那批订单最近怎么样", "limit": 5}
    settings = replace(qworld.settings, caller="desk-bot")
    monkeypatch.setattr(graphhook, "open_searcher", lambda settings, library: None)
    without = as_json(memory_search(question, settings=settings))
    said: list[str] = []
    searcher, session, _ = timed_searcher(monkeypatch, {"全文 事": 31.0}, said)
    monkeypatch.setattr(graphhook, "open_searcher", lambda settings, library: searcher)
    answer = as_json(memory_search(question, settings=settings))
    assert answer["degraded"] == ["graph"] and searcher.stats["timeout"] == 1
    assert answer["items"] and answer == without
    assert said == ["主图这一步的查询超过 30 秒（停在 fulltext 那一路），这次只用账本视图；30 秒里不再去查"]
    words = {"越南", "订单", "最近", *(item["content"] for item in answer["items"])}  # type: ignore[union-attr]
    assert not any(word in line for line in said for word in words)
    asked = len(session.timeouts)
    assert as_json(memory_search(question, settings=settings)) == without  # 30 秒里再问
    assert searcher.stats["cooldown"] == 1 and len(session.timeouts) == asked


def test_a_graph_that_is_not_there_raises_graph_unavailable_quickly() -> None:
    """连不上（端口上没有 Neo4j）：抛 graphhook.GraphUnavailable，服务退回账本视图；这一阵里再来的直接抛，不再去连
    （审阅第 7 条）。"""
    import time

    said: list[str] = []
    searcher = gs.GraphSearch(GraphTarget("prod", "bolt://127.0.0.1:1", "neo4j", "made-up-pass"), marker_id="m", timeout=2,
                              say=said.append)
    request = GraphRequest(
        library="prod", question="越南那批订单", words=("越南那批订单",), seeds=(), scopes=((("business",), ()),),
        partitions=None, time_from=None, time_to=None, include_history=False, as_of=AS_OF, want=5, ledger_hits=(),
        hidden_in=lambda items: set(),
    )
    started = time.monotonic()
    with pytest.raises(graphhook.GraphUnavailable):
        searcher.search(request)
    assert time.monotonic() - started < 10
    again = time.monotonic()
    with pytest.raises(graphhook.GraphUnavailable, match="cooldown"):
        searcher.search(request)
    assert time.monotonic() - again < 0.5 and searcher.stats["cooldown"] == 1 and searcher.stats["unavailable"] == 1
    assert len(said) == 1 and "30 秒里不再去连" in said[0]
    searcher.close()


class MarkerSession:
    def run(self, query, params):  # type: ignore[no-untyped-def]
        raise FakeBroken("库没起来")


def test_an_error_while_checking_the_marker_counts_as_unavailable() -> None:
    said: list[str] = []
    searcher = gs.GraphSearch(GraphTarget("prod", "bolt://127.0.0.1:1", "neo4j", "made-up-pass"), marker_id="m",
                              say=said.append)
    with pytest.raises(graphhook.GraphUnavailable):
        searcher._check_marker(gs.Budget(MarkerSession(), 30.0), "prod")
    assert searcher.stats["unavailable"] == 1 and searcher._down_until > 0


def test_hidden_checks_in_batches_and_remembers() -> None:
    asked: list[int] = []

    def check(items):  # type: ignore[no-untyped-def]
        asked.append(len(items))
        return {(kind, item_id) for kind, item_id, text in items if "藏" in text}

    hidden = gs.Hidden(check)
    items = [("fact", f"F{index:04d}", "藏" if index % 2 else "露") for index in range(250)]
    assert hidden.first_clean(items, 60) == [("fact", f"F{index:04d}") for index in range(0, 120, 2)]
    assert asked == [100, 100]  # 两批就凑够了 60 条干净的
    assert hidden(items[:10]) == {("fact", f"F{index:04d}") for index in range(1, 10, 2)} and asked == [100, 100]


def mark_connected(world: World, *, projection: str | None = None, rebuilt: bool = True) -> None:
    """账本记着接上了一个图（javmem graph init、整张同步过以后的样子），不真连。"""
    world.execute(
        "UPDATE graph_state SET enabled = 1, marker_id = 'made-up-marker', rebuilt_at = ?, projection = ? WHERE id = 1",
        "2026-11-10T09:00:00+08:00" if rebuilt else None,
        projection if projection is not None else projection_key(world.settings.timezone, None),
    )


def test_open_searcher_only_when_the_graph_is_set_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = build_world(tmp_path / "h")
    env = {"JAVMEM_GRAPH_NEO4J_PASSWORD": "made-up-pass-1"}
    try:
        assert gs.open_graph_searcher(world.settings, "prod", env=env) is None  # 账本没接上图
        mark_connected(world)
        found = gs.open_graph_searcher(world.settings, "prod", env=env)
        assert isinstance(found, gs.GraphSearch) and found.marker_id == "made-up-marker" and found._driver is None
        assert gs.open_graph_searcher(world.settings, "prod", env=env) is found  # 进程里留着（驱动、问话的向量）
        assert gs.open_graph_searcher(world.settings, "prod", env={}) is None  # 没设这个库的图的密码
        assert gs.open_graph_searcher(world.settings, "prod", env={**env, "JAVMEM_GRAPH_SEARCH": "off"}) is None
        assert gs.open_graph_searcher(world.settings, "prod", env={**env, "JAVMEM_GRAPH_SEARCH": "bad"}) is None
        assert gs.open_graph_searcher(world.settings, "test", env={"JAVMEM_GRAPH_TEST_NEO4J_PASSWORD": "made-up-pass-2"}) is None
        mark_connected(world, rebuilt=False)  # 整张同步做到一半
        assert gs.open_graph_searcher(world.settings, "prod", env=env) is None
        mark_connected(world, projection="g2@28800/28800#-")  # 图还是以前的样子：要先 javmem graph sync
        assert gs.open_graph_searcher(world.settings, "prod", env=env) is None
        # 向量服务的模型和图里的向量对不上：不走向量那一路
        service = {**env, "JAVMEM_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1"}
        mark_connected(world, projection=projection_key(world.settings.timezone, "fake-embedding"))
        assert gs.open_graph_searcher(world.settings, "prod", env=service).vectors.embedder is None  # type: ignore[union-attr]
        mark_connected(world, projection=projection_key(world.settings.timezone, "Qwen/Qwen3-Embedding-4B"))
        searcher = gs.open_graph_searcher(world.settings, "prod", env=service)
        embedder = searcher.vectors.embedder  # type: ignore[union-attr]
        assert embedder is not None and embedder.settings.timeout == 2.0  # type: ignore[attr-defined]
        # 服务照的是环境变量：graphhook.open_searcher 就是它
        monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "made-up-pass-1")
        assert isinstance(graphhook.open_searcher(world.settings, "prod"), gs.GraphSearch)
    finally:
        gs.close_searchers()


def test_the_graph_is_not_searched_while_a_withdrawal_is_not_in_it_yet(world: World) -> None:
    """撤回、换版本以后欠着没同步（主图里还留着撤回了的东西）：这一阵不查主图，只用账本视图；同步了照常（复审第 1 条）。"""
    from test_graph import PACKET_IDS, PACKETS, connect_graph, rich_extract, run_sync

    for packet in PACKETS[:3]:
        deliver(world, packet)
    write_ledger(world, rich_extract).close()
    graph = connect_graph(world)
    env = {"JAVMEM_GRAPH_NEO4J_PASSWORD": "made-up-pass-1"}
    try:
        assert run_sync(world, graph).status == "ok"
        assert gs.open_graph_searcher(world.settings, "prod", env=env) is not None
        deliver(world, PACKETS[3])  # 新收的包也欠着，那种照查
        assert gs.open_graph_searcher(world.settings, "prod", env=env) is not None
        withdraw(world.settings, "prod", packets=[PACKET_IDS[0]])
        assert gs.ledger_graph(world.settings, "prod") is None
        assert gs.open_graph_searcher(world.settings, "prod", env=env) is None
        assert run_sync(world, graph).status == "ok"
        assert gs.open_graph_searcher(world.settings, "prod", env=env) is not None
    finally:
        gs.close_searchers()


def test_without_the_ledger_check_nothing_is_matched_by_text_or_vector() -> None:
    """hidden_in 是必填的；万一给了 None，全都当字里有看不到的叫法（宁可不比，复审第 5 条）。"""
    items = [("fact", "F1", "一句话"), ("relation", "R1", "另一句")]
    assert gs.Hidden(None)(items) == {("fact", "F1"), ("relation", "R1")}
    assert gs.Hidden(None).first_clean(items, 5) == []
    with pytest.raises(TypeError):
        GraphRequest(library="prod", question="q", words=(), seeds=(), scopes=(), partitions=None,  # type: ignore[call-arg]
                     time_from=None, time_to=None, include_history=False, as_of=AS_OF, want=5, ledger_hits=())


def test_nothing_visible_means_only_the_ledger_hits() -> None:
    searcher = gs.GraphSearch(GraphTarget("prod", "bolt://127.0.0.1:1", "neo4j", "made-up-pass"), marker_id="m")
    request = GraphRequest(
        library="prod", question="越南那批订单", words=(), seeds=(), scopes=(), partitions=None, time_from=None, time_to=None,
        include_history=False, as_of=AS_OF, want=5, ledger_hits=(Hit("fact", "F2", 3.0), Hit("fact", "F1", 2.0)),
        hidden_in=lambda items: set(),
    )
    assert [hit.id for hit in searcher.search(request)] == ["F2", "F1"]  # 一个看得到的范围都没有：不去连图


# ---------- 要真 Neo4j 的（给了测试用的图才跑，见 test_graph_neo4j.py） ----------

needs_neo4j = pytest.mark.skipif(not (URI and PASSWORD), reason="没给测试用的 Neo4j（JAVMEM_TEST_NEO4J_URI、_PASSWORD）")

WANG = {"key": "g1", "mention": "王强", "name": "王强", "type": "人"}
FACTORY = {"key": "g2", "mention": "示例工厂", "name": "示例工厂", "type": "公司或机构"}
BANK = {"key": "g3", "mention": "海港银行", "name": "海港银行", "type": "公司或机构"}
FREIGHT = {"key": "g4", "mention": "示例物流", "name": "示例物流", "type": "公司或机构"}
OWNER = {"key": "g5", "mention": "我", "name": "主人", "type": "人"}
PROJECT = {"key": "g6", "mention": "示例项目乙", "name": "示例项目乙", "type": "项目"}
DOCK = {"key": "g7", "mention": "示例码头", "name": "示例码头", "type": "公司或机构"}
STORE = {"key": "g8", "mention": "示例仓库", "name": "示例仓库", "type": "公司或机构"}
# 两跳的链：王强 → 示例工厂 → 海港银行 → 示例物流；主人连着示例工厂（客户）、示例项目乙（枢纽）；王强负责示例码头（测试里改成
# 已结束）。
# 名字、句子都是编的
CHAIN_PACKET = chat("2026-11-03", day_body(("10:00–10:08", [
    ("10:00", "我", "王强负责示例工厂。"),
    ("10:01", "我", "示例工厂的客户是海港银行。"),
    ("10:02", "我", "海港银行的客户是示例物流。"),
    ("10:03", "我", "王强说示例工厂的样品下周到。"),
    ("10:04", "我", "示例工厂是我的客户。"),
    ("10:05", "我", "我负责示例项目乙。"),
    ("10:06", "我", "王强负责示例码头。"),
    ("10:07", "我", "示例码头的样品到了。"),
    ("10:08", "我", "示例仓库的样品到了。"),
])))
# 看不到的（finance、payroll，desk-bot 看不到）：和看得到的很像；王强负责示例仓库只在这里说过；王强的 12 条更新的事
HIDDEN_PACKET = chat("2026-11-04", day_body(("10:00–10:15", [
    ("10:00", "我", "王强负责示例工厂的工资。"),
    ("10:01", "我", "王强说示例工厂的样品下周到，工资也一起发。"),
    ("10:02", "我", "示例工厂的客户是海港银行，工资走海港银行。"),
    ("10:03", "我", "王强负责示例仓库。"),
    *[(f"10:{4 + index:02d}", "我", f"王强说第{index + 1}批工资单到了。") for index in range(12)],
])), source="books-bot", domain="[finance]", sensitive="[payroll]")


def graph_extract(window: Window) -> dict[str, object]:
    """假 L3：tests/query_world.py 那几句之外，再认上面两个包里的话。"""
    out = world_extract(window)
    entities = {str(item["key"]): item for item in out["entities"]}  # type: ignore[union-attr]
    facts = list(out["facts"])  # type: ignore[arg-type]
    relations = list(out["relations"])  # type: ignore[arg-type]
    for line in window.targets:
        text = line.text.rstrip("。")
        ref = line.ref
        if text in ("王强负责示例工厂", "王强负责示例工厂的工资"):
            entities.update(g1=WANG, g2=FACTORY)
            relations.append(relation(WANG, "负责", FACTORY, (ref, text)))
        elif text.startswith("示例工厂的客户是海港银行"):
            entities.update(g2=FACTORY, g3=BANK)
            relations.append(relation(FACTORY, "客户是", BANK, (ref, text)))
        elif text == "海港银行的客户是示例物流":
            entities.update(g3=BANK, g4=FREIGHT)
            relations.append(relation(BANK, "客户是", FREIGHT, (ref, text)))
        elif text.startswith("王强说示例工厂的样品下周到"):
            entities.update(g1=WANG, g2=FACTORY)
            facts.append(l3_fact(window, text, text, subject="g1", attribute="样品", value="下周到",
                                 entities=[{"key": "g1", "role": "subject"}, {"key": "g2", "role": "mention"}]))
        elif text == "示例工厂是我的客户":  # 写成“我负责示例工厂”的话，负责的人换成了主人，王强那条就被替代了
            entities.update(g5=OWNER, g2=FACTORY)
            relations.append(relation(OWNER, "客户是", FACTORY, (ref, text)))
        elif text == "我负责示例项目乙":
            entities.update(g5=OWNER, g6=PROJECT)
            relations.append(relation(OWNER, "负责", PROJECT, (ref, text)))
        elif text in ("王强负责示例码头", "王强负责示例仓库"):
            other = DOCK if "码头" in text else STORE
            entities.update(g1=WANG, **{str(other["key"]): other})
            relations.append(relation(WANG, "负责", other, (ref, text)))
        elif text in ("示例码头的样品到了", "示例仓库的样品到了"):
            other = DOCK if "码头" in text else STORE
            entities[str(other["key"])] = other
            facts.append(l3_fact(window, text, text, subject=str(other["key"]), attribute="样品", value="到了"))
        elif text.startswith("王强说第") and text.endswith("批工资单到了"):
            entities["g1"] = WANG
            facts.append(l3_fact(window, text, text, subject="g1", attribute=text[3:-3], value="到了"))
    return {"entities": list(entities.values()), "facts": facts, "relations": relations}


class BigramEmbeddings(FakeEmbeddings):
    """测试用的向量：两个字一组数进 2560 格（问话只算 Query: 后面那一段），字面上像的余弦就高。"""

    def __init__(self) -> None:
        super().__init__(name="bigram-test")

    def embed(self, texts):  # type: ignore[no-untyped-def]
        return EmbeddingReply([pack_vector(bigram_vector(text)) for text in texts], sum(len(text) for text in texts), 0.0)


def bigram_vector(text: str) -> list[float]:
    if "\nQuery:" in text:
        text = text.split("\nQuery:", 1)[1]
    chars = [char for char in text if char.isalnum()]
    grams = ["".join(chars[index:index + 2]) for index in range(len(chars) - 1)] or chars or ["空"]
    values = [0.0] * 2560
    for gram in grams:
        values[int(hashlib.sha256(gram.encode("utf-8")).hexdigest()[:8], 16) % 2560] += 1.0
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def graph_world(root: Path) -> World:
    """查询接口那个小世界，加上两跳的链；向量用 BigramEmbeddings。"""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(query_world, "FakeEmbeddings", BigramEmbeddings)
        world = build_world(root)
        world.drop("inbox/desk-bot", CHAIN_PACKET)
        world.intake()
        write_all(world, graph_extract, "prod")
    return world


def add_hidden(world: World) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(query_world, "FakeEmbeddings", BigramEmbeddings)
        world.drop("inbox/books-bot", HIDDEN_PACKET)
        world.intake()
        write_all(world, graph_extract, "prod")


def live_hidden(world: World, caller: str):  # type: ignore[no-untyped-def]
    """服务那个 hidden_in（tools._hidden_in），每次另开一个读事务问：记下来的 GraphRequest 事后再查也能用。"""
    from javmem.query import tools

    def check(items):  # type: ignore[no-untyped-def]
        def answer(view, request, out):  # type: ignore[no-untyped-def]
            return {"found": tools._hidden_in(view, items)}

        settings = replace(world.settings, caller=caller)
        return tools.run_tool("memory_search", {"query": "随便"}, settings, tools._parse_search, answer)["found"]

    return check


@dataclass
class Graphed:
    world: World
    graph: neo.NeoGraph
    searcher: gs.GraphSearch
    hits: list[list[Hit]]
    requests: list[GraphRequest]

    def sync(self, mode: str = "incremental") -> None:
        report = run_sync(self.world, self.graph, mode=mode, embedder=BigramEmbeddings())
        assert report.status == "ok", report.message
        self.graph.await_indexes(120)

    def ask(self, monkeypatch: pytest.MonkeyPatch, caller: str = "desk-bot", **arguments: object) -> dict:
        """memory_search 照真的走（服务算好 GraphRequest），检索器是真的主图检索器。"""
        outer = self

        class Recording:
            def search(self, request: GraphRequest) -> list[Hit]:
                # 记下来的那一份留着以后再查：服务的读事务那时已经关了，hidden_in 换成每次另开一个读事务问的那一个
                outer.requests.append(replace(request, hidden_in=live_hidden(outer.world, caller)))
                found = outer.searcher.search(request)
                outer.hits.append(found)
                return found

        monkeypatch.setattr(graphhook, "open_searcher", lambda settings, library: Recording())
        return as_json(memory_search(arguments, settings=replace(self.world.settings, caller=caller)))  # type: ignore[return-value]

    def graph_only(self, request: GraphRequest) -> list[Hit]:
        return self.searcher.search(replace(request, ledger_hits=()))


@pytest.fixture
def graphed(real: neo.NeoGraph, tmp_path: Path) -> Iterator[Graphed]:
    world = graph_world(tmp_path / "w")
    conn = world.ledger()
    try:
        enable(conn, real.create_marker("prod", PROJECTION_VERSION))
        marker = read_state(conn).marker_id
    finally:
        conn.close()
    assert marker
    searcher = gs.GraphSearch(_target(), marker_id=marker, embedder=BigramEmbeddings(), timeout=30,
                              params=replace(gs.GS2, theta=0.2))
    done = Graphed(world, real, searcher, [], [])
    done.sync()
    try:
        yield done
    finally:
        searcher.close()


def contents(answer: dict) -> list[str]:
    return [item["content"] for item in answer["items"]]


def entity(world: World, name: str) -> str:
    return world.rows("SELECT entity_id FROM entities WHERE canonical_name = ?", name)[0][0]


def relation_id(world: World, subject: str, kind: str, obj: str) -> str:
    return world.rows(
        "SELECT r.relation_id FROM relations AS r JOIN entities AS s ON s.entity_id = r.subject_entity_id"
        " JOIN entities AS o ON o.entity_id = r.object_entity_id WHERE s.canonical_name = ? AND r.relation_type = ?"
        " AND o.canonical_name = ? AND r.scope_partitions = '[\"business\"]'", subject, kind, obj)[0][0]


@needs_neo4j
def test_a_recognised_name_pulls_its_facts_and_relations_from_the_graph(graphed: Graphed, monkeypatch) -> None:
    answer = graphed.ask(monkeypatch, query="越南那批订单最近怎么样", limit=10)
    assert (answer["answered_from"], answer["degraded"]) == ("graph", [])
    assert "周建华 负责 越南那批订单" in contents(answer)
    assert "李明 负责 越南那批订单" not in contents(answer)  # 已经结束，默认只要现在有效的
    # 待确认的图里没有，从账本那一路合进来（照接口文档 4.3 是“还没定”）
    statuses = {item["content"]: (item["status"], item.get("pending_reason")) for item in answer["items"]}
    assert statuses["周建华说越南那批订单的舱位 14 日前订好"] == ("pending", "frozen_conflict")
    (hits,) = graphed.hits
    pending = graphed.world.rows("SELECT fact_id FROM facts WHERE status = 'pending'")[0][0]
    graph_ids = {row["id"] for row in graphed.graph.query("MATCH (f:JmFact) RETURN f.id AS id")}
    assert pending not in graph_ids and pending in {hit.id for hit in hits}
    # 两个字的人名、别名都认得出：老周就是周建华
    zhou = graphed.ask(monkeypatch, query="老周管什么", limit=10)
    assert "周建华 负责 越南那批订单" in contents(zhou) and zhou["degraded"] == []


@needs_neo4j
def test_two_hops_reach_the_end_of_the_chain(graphed: Graphed, monkeypatch) -> None:
    """王强 → 示例工厂 → 海港银行 → 示例物流：问王强，一跳拿到“示例工厂 客户是 海港银行”，两跳拿到“海港银行 客户是 示例物流”。"""
    answer = graphed.ask(monkeypatch, query="王强那边的情况", limit=20)
    found = contents(answer)
    assert "王强 负责 示例工厂" in found and "示例工厂 客户是 海港银行" in found and "海港银行 客户是 示例物流" in found
    request = graphed.requests[-1]
    near = gs.GraphSearch(_target(), marker_id=graphed.searcher.marker_id, params=replace(gs.GS2, hops=1))
    far = gs.GraphSearch(_target(), marker_id=graphed.searcher.marker_id, params=replace(gs.GS2, hops=0))
    try:
        one_hop = {hit.id for hit in near.search(replace(request, ledger_hits=(), words=()))}
        no_hop = {hit.id for hit in far.search(replace(request, ledger_hits=(), words=()))}
    finally:
        near.close()
        far.close()
    w = graphed.world
    assert relation_id(w, "示例工厂", "客户是", "海港银行") in one_hop
    assert relation_id(w, "海港银行", "客户是", "示例物流") not in one_hop
    assert relation_id(w, "示例工厂", "客户是", "海港银行") not in no_hop
    assert relation_id(w, "王强", "负责", "示例工厂") in no_hop


@needs_neo4j
def test_what_you_cannot_see_never_shows_and_does_not_move_anything(graphed: Graphed, monkeypatch) -> None:
    """同样的问话：另加一堆看不到的、很像的（finance、payroll）以后，desk-bot 拿到的名次、条数一样；看得到的身份照样有。"""
    questions = ["王强那边的情况", "示例工厂的样品", "海港银行的客户", "示例工厂的工资"]
    before = {}
    for question in questions:
        graphed.ask(monkeypatch, query=question, limit=20)
        before[question] = graphed.graph_only(graphed.requests[-1])
    add_hidden(graphed.world)
    graphed.sync()
    hidden = {row[0] for row in graphed.world.rows(
        "SELECT fact_id FROM facts WHERE scope_partitions = '[\"finance\"]' UNION"
        " SELECT relation_id FROM relations WHERE scope_partitions = '[\"finance\"]'")}
    assert len(hidden) >= 4  # 看不到的真进了图
    in_graph = {row["id"] for row in graphed.graph.query(
        "MATCH (f:JmFact) RETURN f.id AS id UNION MATCH ()-[r:JM_RELATION]->() RETURN r.id AS id")}
    assert hidden <= in_graph
    for question in questions:
        answer = graphed.ask(monkeypatch, query=question, limit=20)
        after = graphed.graph_only(graphed.requests[-1])
        assert after == before[question], question
        assert not hidden & {hit.id for hit in graphed.hits[-1]}
        assert not hidden & {item["id"] for item in answer["items"]}
    books = graphed.ask(monkeypatch, "books-bot", query="示例工厂的工资", limit=20)
    assert hidden & {item["id"] for item in books["items"]}


@needs_neo4j
def test_a_question_about_nothing_in_memory_gets_nothing_from_the_graph(graphed: Graphed, monkeypatch) -> None:
    answer = graphed.ask(monkeypatch, query="完全没有的东西")
    assert graphed.hits[-1] == [] and answer["result"] == "none"
    assert answer["not_found"]["searched"] == ["graph", "ledger", "raw"]
    assert graphed.graph_only(graphed.requests[-1]) == []


@needs_neo4j
def test_the_searcher_session_cannot_write(graphed: Graphed) -> None:
    with graphed.searcher._session() as session:
        with pytest.raises(Neo4jError, match="(?i)access|read"):
            with session.begin_transaction(timeout=5) as tx:
                tx.run("CREATE (:JmEntity {id: 'E-should-not-exist'})").consume()
                tx.commit()
    assert graphed.graph.query("MATCH (e:JmEntity {id: 'E-should-not-exist'}) RETURN count(e) AS n")[0]["n"] == 0


@needs_neo4j
def test_a_graph_of_another_ledger_is_not_searched(graphed: Graphed, monkeypatch, capsys) -> None:
    other = gs.GraphSearch(_target(), marker_id="not-this-one")
    try:
        graphed.ask(monkeypatch, query="越南那批订单最近怎么样")
        with pytest.raises(graphhook.GraphUnavailable):
            other.search(graphed.requests[-1])
        with pytest.raises(graphhook.GraphUnavailable):
            other.search(replace(graphed.requests[-1], library="test"))
    finally:
        other.close()
    assert "标记和账本记的对不上" in capsys.readouterr().err


@needs_neo4j
def test_a_query_that_times_out_drops_the_whole_graph_step_and_is_logged(graphed: Graphed, monkeypatch, capsys) -> None:
    """一句查询到点被掐掉：这次整个主图不用，memory_search 照账本视图答、degraded: ["graph"]，记一行；不悄悄少一路（D196）。"""
    normal = graphed.ask(monkeypatch, query="王强那边的情况", limit=20)
    assert normal["degraded"] == [] and graphed.hits[-1]

    def slow(*args: object) -> list[gs.Found]:
        raise FakeTimeout("太慢")

    monkeypatch.setattr(graphed.searcher, "_fulltext_route", slow)
    with pytest.raises(graphhook.GraphUnavailable, match="timeout"):
        graphed.graph_only(graphed.requests[-1])
    assert graphed.searcher.stats["timeout"] == 1 and not any(key.endswith("_timeout") for key in graphed.searcher.stats)
    assert "主图这一步的查询超过 30 秒（停在 fulltext 那一路）" in capsys.readouterr().err
    answer = graphed.ask(monkeypatch, query="王强那边的情况", limit=20)  # 30 秒里再来的：直接当主图没接
    assert answer["degraded"] == ["graph"] and graphed.searcher.stats["cooldown"] == 1


def probe_transactions(graph: neo.NeoGraph, probe: str) -> list[str]:
    """图那边还在跑的、查询里带着 probe 的事务编号。管事务的命令在自动提交的会话里问。"""
    with graph.driver.session(database=neo.DATABASE) as session:
        rows = session.run("SHOW TRANSACTIONS YIELD transactionId, currentQuery RETURN transactionId, currentQuery").data()
    return [str(row["transactionId"]) for row in rows if probe in str(row["currentQuery"] or "")]


@needs_neo4j
def test_a_query_past_the_time_limit_really_stops_in_the_graph(graphed: Graphed, monkeypatch, capsys) -> None:
    """总时限到了，Neo4j 那边到点掐掉那一句（事务的时限）：这次整个主图不用；图里没有留着接着跑的查询，不会越堆越多（D201）。
    这里把总时限调小到 1 秒，全文那一路换成一句自己停不下来的查询（只数数，一百亿行）。"""
    import threading
    import time

    graphed.ask(monkeypatch, query="王强那边的情况", limit=20)
    request = replace(graphed.requests[-1], ledger_hits=())
    searcher = gs.GraphSearch(_target(), marker_id=graphed.searcher.marker_id, params=replace(gs.GS2, search_seconds=1.0))
    probe = "javmem-endless-probe"
    endless = (f"WITH '{probe}' AS probe UNWIND range(1, 100000) AS a UNWIND range(1, 100000) AS b "
               "WITH probe, a, b WHERE a * b < 0 RETURN probe, count(*) AS n")

    def slow(budget: gs.Budget, *rest: object) -> list[gs.Found]:
        budget.run(endless)
        return []

    def kill() -> None:  # 万一图那边停不下来：测试别卡死，掐掉它（这时下面照样算错）
        ids = probe_transactions(graphed.graph, probe)
        if ids:
            with graphed.graph.driver.session(database=neo.DATABASE) as session:
                session.run("TERMINATE TRANSACTIONS $ids", ids=ids).consume()

    monkeypatch.setattr(searcher, "_fulltext_route", slow)
    watchdog = threading.Timer(60, kill)
    watchdog.start()
    started = time.monotonic()
    try:
        with pytest.raises(graphhook.GraphUnavailable, match="timeout"):
            searcher.search(request)
        took = time.monotonic() - started
    finally:
        watchdog.cancel()
        searcher.close()
    assert took < 20, took  # 1 秒的时限，Neo4j 大约两秒看一次事务超时没有
    deadline = time.monotonic() + 10
    while probe_transactions(graphed.graph, probe) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert probe_transactions(graphed.graph, probe) == []
    assert searcher.stats["timeout"] == 1
    err = capsys.readouterr().err
    assert "主图这一步的查询超过 1 秒（停在 fulltext 那一路）" in err and "王强" not in err


@needs_neo4j
def test_memory_search_through_the_real_hook(graphed: Graphed, monkeypatch: pytest.MonkeyPatch) -> None:
    """J5.5 的几个例子换上真的检索器再跑一遍（服务照环境变量连图，test_query_search.py 那几条用的是假的检索器）。"""
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_URI", URI)
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", PASSWORD)

    def ask(caller: str = "desk-bot", **arguments: object) -> dict:
        return as_json(memory_search(arguments, settings=replace(graphed.world.settings, caller=caller)))  # type: ignore[return-value]

    try:
        answer = ask(query="越南那批订单最近怎么样", limit=5)
        assert (answer["answered_from"], answer["degraded"], answer["result"]) == ("graph", [], "hit")
        assert "周建华 负责 越南那批订单" in contents(answer) and "李明 负责 越南那批订单" not in contents(answer)
        old = ask(query="越南那批订单最近怎么样", limit=50, include_history=True)
        assert "李明 负责 越南那批订单" in contents(old)
        pages = [ask(query="越南那批订单最近怎么样", limit=2, cursor=cursor) for cursor in ("c0", "c2")]
        whole = ask(query="越南那批订单最近怎么样", limit=4)
        assert [item["id"] for page in pages for item in page["items"]] == [item["id"] for item in whole["items"]]
        empty = ask(query="完全没有的东西")
        assert empty["result"] == "none" and empty["not_found"]["searched"] == ["graph", "ledger", "raw"]
        hidden = ask(query="工资单")
        assert hidden["result"] == "none" and hidden["degraded"] == []
        assert contents(ask("books-bot", query="工资单")) == ["十一月的工资单月底发"]
        trial = ask("trial-bot", query="越南那批")  # 测试库没接上图：只用账本视图
        assert trial["library"] == "test" and trial["degraded"] == ["graph"]
        monkeypatch.setenv("JAVMEM_GRAPH_SEARCH", "off")
        assert ask(query="越南那批订单最近怎么样", limit=5)["degraded"] == ["graph"]
    finally:
        gs.close_searchers()


def only(graphed: Graphed, monkeypatch: pytest.MonkeyPatch, question: str, **change: object) -> list[Hit]:
    """服务照真的算好 GraphRequest，只拿主图那几路（不带账本那一路），可以再改几样（words=() 关掉全文，seeds=() 关掉实体）。"""
    graphed.ask(monkeypatch, query=question, limit=20)
    return graphed.graph_only(replace(graphed.requests[-1], **change))


def fact_id(world: World, content: str) -> str:
    return world.rows("SELECT fact_id FROM facts WHERE content = ?", content)[0][0]


@needs_neo4j
def test_expansion_only_follows_visible_current_relations_and_skips_the_owner(graphed: Graphed, monkeypatch) -> None:
    """往外扩只走看得到的、现在有效的关系（王强负责示例仓库只在看不到的包里说过；王强负责示例码头已经结束），也不扩进主人
    （主人连着示例工厂，主人负责示例项目乙不能从王强扩过来）。"""
    w = graphed.world
    add_hidden(w)
    w.execute("UPDATE relations SET state = 'ended' WHERE relation_id = ?", relation_id(w, "王强", "负责", "示例码头"))
    graphed.sync()
    searcher = gs.GraphSearch(_target(), marker_id=graphed.searcher.marker_id, params=replace(gs.GS2, theta=0.2))
    try:
        graphed.ask(monkeypatch, query="王强那边的情况", limit=20)
        found = {hit.id for hit in searcher.search(replace(graphed.requests[-1], ledger_hits=(), words=()))}
    finally:
        searcher.close()
    assert relation_id(w, "示例工厂", "客户是", "海港银行") in found  # 一跳照样有
    assert relation_id(w, "主人", "客户是", "示例工厂") in found  # 一跳到的示例工厂直接连着的关系，一头是主人也算
    assert relation_id(w, "主人", "负责", "示例项目乙") not in found  # 不扩进主人
    assert fact_id(w, "示例仓库的样品到了") not in found  # 只能经看不到的关系扩到
    assert fact_id(w, "示例码头的样品到了") not in found  # 只能经已经结束的关系扩到


@needs_neo4j
def test_the_owner_as_the_named_entity_is_not_expanded(graphed: Graphed, monkeypatch) -> None:
    w = graphed.world
    hits = {hit.id for hit in only(graphed, monkeypatch, "主人负责哪些", words=())}
    assert graphed.requests[-1].seeds == (gs.OWNER_ID,)
    assert {relation_id(w, "主人", "客户是", "示例工厂"), relation_id(w, "主人", "负责", "示例项目乙")} <= hits
    assert relation_id(w, "示例工厂", "客户是", "海港银行") not in hits  # 枢纽只拿它直接连着的


@needs_neo4j
def test_each_entity_keeps_its_slots_for_what_this_identity_can_see(graphed: Graphed, monkeypatch) -> None:
    """王强另有 12 条更新的、看不到的事：每个实体最多 10 条是滤完以后挑的，看得到的那条照样有。"""
    add_hidden(graphed.world)
    graphed.sync()
    hits = {hit.id for hit in only(graphed, monkeypatch, "王强那边的情况", words=())}
    assert fact_id(graphed.world, "王强说示例工厂的样品下周到") in hits


@needs_neo4j
def test_a_relation_on_behalf_of_the_named_entity_is_pulled(graphed: Graphed, monkeypatch) -> None:
    """“代表哪一家”是认出来的实体的关系，不扩也拿得到（jm_relation_org）。"""
    w = graphed.world
    chain = relation_id(w, "海港银行", "客户是", "示例物流")
    w.execute("UPDATE relations SET org_entity_id = ? WHERE relation_id = ?", entity(w, "示例工厂"), chain)
    graphed.sync()
    searcher = gs.GraphSearch(_target(), marker_id=graphed.searcher.marker_id, params=replace(gs.GS2, hops=0))
    try:
        graphed.ask(monkeypatch, query="示例工厂", limit=20)
        hits = {hit.id for hit in searcher.search(replace(graphed.requests[-1], ledger_hits=(), words=()))}
    finally:
        searcher.close()
    assert chain in hits


@needs_neo4j
def test_fulltext_and_vector_each_find_what_only_they_can(graphed: Graphed, monkeypatch) -> None:
    w = graphed.world
    words_only = gs.GraphSearch(_target(), marker_id=graphed.searcher.marker_id, params=replace(gs.GS2, theta=0.2))
    try:  # 没有问话的向量、没认出实体：只剩全文那一路
        graphed.ask(monkeypatch, query="定金到账没有", limit=20)
        assert graphed.requests[-1].seeds == ()
        by_words = {hit.id for hit in words_only.search(replace(graphed.requests[-1], ledger_hits=()))}
    finally:
        words_only.close()
    assert fact_id(w, "越南那批订单的定金到账") in by_words
    by_vector = {hit.id for hit in only(graphed, monkeypatch, "样品下周到", seeds=(), words=())}
    assert fact_id(w, "王强说示例工厂的样品下周到") in by_vector
    # 全文那一路也只要现在有效的：已经结束的“李明 负责 越南那批订单”不出来，include_history 才出来
    ended = relation_id(w, "李明", "负责", "越南那批订单")
    assert ended not in {hit.id for hit in only(graphed, monkeypatch, "李明负责的订单", seeds=())}
    assert ended in {hit.id for hit in only(graphed, monkeypatch, "李明负责的订单", seeds=(), include_history=True)}


# ---------- 看不到的叫法不能把看得到的事实搜出来（审阅第 1 条） ----------

ZHOU_FULL = {"key": "e1", "mention": "老周", "name": "周建华", "type": "人"}


def zhou_extract(window: Window) -> dict[str, object]:
    """“老周就是周建华”、“老周说舱位订好了”（L3 写成“周建华说舱位订好了”），照 J5.5 test_query_view 那个例子编的。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        if text == "老周就是周建华":
            found.append(l3_fact(window, text, "周建华也叫老周", subject="e1", attribute="称呼", value="老周"))
        if text == "老周说舱位订好了":
            found.append(l3_fact(window, text, "周建华说舱位订好了", subject="e1", attribute="舱位", value="订好了"))
    return {"entities": [ZHOU_FULL], "facts": found}


def zhou_world(world: World, real: neo.NeoGraph, *, hidden_scope: bool) -> str:
    """回图的标记。hidden_scope：“老周就是周建华”在 finance 的包里（desk-bot 看不到全名）。"""
    if hidden_scope:
        deliver(world, day_packet("2026-11-01", "我：老周就是周建华。", source="books-bot", domain="[finance]"),
                folder="inbox/books-bot")
        deliver(world, day_packet("2026-11-02", "我：老周说舱位订好了。"))
    else:
        deliver(world, day_packet("2026-11-01", "我：老周就是周建华。"), day_packet("2026-11-02", "我：老周说舱位订好了。"))
    write_ledger(world, zhou_extract, embedder=BigramEmbeddings()).close()
    conn = world.ledger()
    try:
        enable(conn, real.create_marker("prod", PROJECTION_VERSION))
        marker = read_state(conn).marker_id
    finally:
        conn.close()
    assert run_sync(world, real, embedder=BigramEmbeddings()).status == "ok"
    real.await_indexes(120)
    assert marker
    return marker


def asked(world: World, monkeypatch: pytest.MonkeyPatch, searcher: object, question: str) -> dict:
    monkeypatch.setattr(graphhook, "open_searcher", lambda settings, library: searcher)
    return as_json(memory_search({"query": question}, settings=replace(world.settings, caller="desk-bot")))  # type: ignore[return-value]


@needs_neo4j
@pytest.mark.parametrize("synced", [True, False])
def test_a_withdrawn_full_name_finds_nothing(world: World, real: neo.NeoGraph, monkeypatch, synced: bool) -> None:
    """撤回了“老周就是周建华”那一包：拿“周建华”去搜，接不接主图都查不到（图同步了、还没同步都一样）。"""
    marker = zhou_world(world, real, hidden_scope=False)
    searcher = gs.GraphSearch(_target(), marker_id=marker, embedder=BigramEmbeddings(), params=replace(gs.GS2, theta=0.2))
    try:
        before = asked(world, monkeypatch, searcher, "周建华")
        assert before["result"] == "hit"  # 撤回以前看得到全名：搜得到（这条测试不是空跑）
        withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
        if synced:
            assert run_sync(world, real, embedder=BigramEmbeddings()).status == "ok"
            real.await_indexes(120)
        else:
            assert world.rows("SELECT count(*) FROM graph_dirty")[0][0] > 0  # 图慢一拍
        assert asked(world, monkeypatch, None, "周建华")["result"] == "none"  # 只用账本视图
        answer = asked(world, monkeypatch, searcher, "周建华")
        assert (answer["result"], answer["degraded"]) == ("none", [])
        assert asked(world, monkeypatch, searcher, "老周")["result"] == "hit"  # 看得到的叫法照样查得到
    finally:
        searcher.close()


@needs_neo4j
def test_a_full_name_seen_only_elsewhere_finds_nothing_even_by_vector(world: World, real: neo.NeoGraph, monkeypatch) -> None:
    """全名只在 finance 的包里：desk-bot 拿“周建华”去搜，全文、向量都不把“老周说舱位订好了”带出来。"""
    marker = zhou_world(world, real, hidden_scope=True)
    searcher = gs.GraphSearch(_target(), marker_id=marker, embedder=BigramEmbeddings(), params=replace(gs.GS2, theta=0.2))
    try:
        assert asked(world, monkeypatch, None, "周建华")["result"] == "none"
        answer = asked(world, monkeypatch, searcher, "周建华")
        assert (answer["result"], answer["degraded"]) == ("none", [])
        seen = asked(world, monkeypatch, searcher, "老周")
        assert [item["content"] for item in seen["items"]] == ["老周说舱位订好了"]  # 看得到的叫法：换了名字给出来
        books = as_json(memory_search({"query": "周建华"}, settings=replace(world.settings, caller="books-bot")))
        assert books["result"] == "hit"  # 看得到全名的身份照样查得到
    finally:
        searcher.close()


# ---------- 复审（第二遍）抓到的：图里的字过时了的那一阵 ----------

LAOZHOU = {"key": "e1", "mention": "老周", "name": "老周", "type": "人"}
ZHOUDONG = {"key": "e5", "mention": "周董", "name": "周董", "type": "人"}
SHIPPING = {"key": "e2", "mention": "海运部", "name": "海运部", "type": "公司或机构"}


def merged_extract(window: Window) -> dict[str, object]:
    """同一条事实两处说法：早的那处（以后撤回）写着“周董”，晚的那处没有；事实的文字取最早的说法。"""
    facts = []
    entities: dict[str, dict[str, object]] = {}
    for line in window.targets:
        text = line.text.rstrip("。")
        if text == "老周和周董说舱位订好了":
            entities.update(e1=LAOZHOU, e5=ZHOUDONG)
            facts.append(l3_fact(window, text, text, subject="e1", attribute="舱位", value="订好了",
                                 entities=[{"key": "e1", "role": "subject"}, {"key": "e5", "role": "mention"}]))
        elif text == "老周说舱位订好了":
            entities["e1"] = LAOZHOU
            facts.append(l3_fact(window, text, text, subject="e1", attribute="舱位", value="订好了"))
    return {"entities": list(entities.values()), "facts": facts}


def ship_extract(window: Window) -> dict[str, object]:
    """A“老周说舱位订好了”写成“周建华说舱位订好了”；B“海运部说舱位很紧张”；D“老周就是周建华”写成“周建华也叫老周”；
    还有几批货的话（池子那条测试用）。"""
    facts = []
    entities: dict[str, dict[str, object]] = {}
    for line in window.targets:
        text = line.text.rstrip("。")
        if text == "老周说舱位订好了":
            entities["e1"] = ZHOU_FULL
            facts.append(l3_fact(window, text, "周建华说舱位订好了", subject="e1", attribute="舱位", value="订好了"))
        elif text == "海运部说舱位很紧张":
            entities["e2"] = SHIPPING
            facts.append(l3_fact(window, text, text, subject="e2", attribute="舱位", value="很紧张"))
        elif text == "老周就是周建华":
            entities["e1"] = ZHOU_FULL
            facts.append(l3_fact(window, text, "周建华也叫老周", subject="e1", attribute="称呼", value="老周"))
        elif text.startswith("老周说第") and text.endswith("批货到了"):
            entities["e1"] = ZHOU_FULL
            facts.append(l3_fact(window, text, "周建华说" + text[3:], subject="e1", attribute=text[3:-2], value="到了"))
        elif text.startswith("海运部说第") and text.endswith("批货到了"):
            entities["e2"] = SHIPPING
            facts.append(l3_fact(window, text, text, subject="e2", attribute=text[4:-2], value="到了"))
    return {"entities": list(entities.values()), "facts": facts}


def connected_world(world: World, real: neo.NeoGraph, extract, *packets: str) -> str:  # type: ignore[no-untyped-def]
    deliver(world, *packets)
    write_ledger(world, extract, embedder=BigramEmbeddings()).close()
    conn = world.ledger()
    try:
        enable(conn, real.create_marker("prod", PROJECTION_VERSION))
        marker = read_state(conn).marker_id
    finally:
        conn.close()
    assert run_sync(world, real, embedder=BigramEmbeddings()).status == "ok"
    real.await_indexes(120)
    assert marker
    return marker


class Capture:
    """真的检索器，记下它回的名次。"""

    def __init__(self, inner: gs.GraphSearch) -> None:
        self.inner = inner
        self.hits: list[list[Hit]] = []

    def search(self, request: GraphRequest) -> list[Hit]:
        found = self.inner.search(request)
        self.hits.append(found)
        return found


@needs_neo4j
def test_a_withdrawn_name_left_in_old_graph_text_finds_nothing(world: World, real: neo.NeoGraph, monkeypatch) -> None:
    """撤回了写着“周董”的那一包、图还没同步：事实还在（晚的那处撑着），账本里的文字换成了“老周说舱位订好了”，“周董”连同
    别名都删了，图里还是旧的字。拿“周董”去搜，接不接主图都查不到（图里的字和账本现在的不一样，就不信它）。"""
    marker = connected_world(world, real, merged_extract, day_packet("2026-11-01", "我：老周和周董说舱位订好了。"),
                             day_packet("2026-11-02", "我：老周说舱位订好了。"))
    (fact,) = [row[0] for row in world.rows("SELECT fact_id FROM facts")]
    searcher = gs.GraphSearch(_target(), marker_id=marker, embedder=BigramEmbeddings(), params=replace(gs.GS2, theta=0.2))
    try:
        assert asked(world, monkeypatch, searcher, "周董")["result"] == "hit"  # 撤回以前查得到（不是空跑）
        withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
        assert world.rows("SELECT content FROM facts WHERE fact_id = ?", fact)[0][0] == "老周说舱位订好了"
        assert real.query("MATCH (f:JmFact {id: $id}) RETURN f.content AS c", id=fact)[0]["c"] == "老周和周董说舱位订好了"
        assert asked(world, monkeypatch, None, "周董")["result"] == "none"
        capture = Capture(searcher)
        assert asked(world, monkeypatch, capture, "周董")["result"] == "none"
        assert fact not in {hit.id for hit in capture.hits[-1]}
    finally:
        searcher.close()


@needs_neo4j
def test_rows_gone_from_the_ledger_neither_show_nor_move_anything(world: World, real: neo.NeoGraph, monkeypatch) -> None:
    """撤回“老周就是周建华”那一包、图还没同步：那一包的事实 D 在账本里删了，图里还在。带看不到的全名问和换成编的名字问，
    看得到的那两条先后一样；D 哪一路的名次里都没有（不占名额）。"""
    marker = connected_world(world, real, ship_extract, day_packet("2026-11-01", "我：老周说舱位订好了。"),
                             day_packet("2026-11-02", "我：海运部说舱位很紧张。"), day_packet("2026-11-03", "我：老周就是周建华。"))
    d_id = world.rows("SELECT fact_id FROM facts WHERE content = '周建华也叫老周'")[0][0]
    for embedder in (None, BigramEmbeddings()):
        searcher = gs.GraphSearch(_target(), marker_id=marker, embedder=embedder, params=replace(gs.GS2, theta=0.2))
        try:
            if embedder is None:
                withdraw(world.settings, "prod", packets=["2026-11-03_2330_desk-bot"])
                assert not world.rows("SELECT 1 FROM facts WHERE fact_id = ?", d_id)
                assert real.query("MATCH (f:JmFact {id: $id}) RETURN f.id AS id", id=d_id)  # 图还没同步
            hidden, made_up = Capture(searcher), Capture(searcher)
            with_hidden = asked(world, monkeypatch, hidden, "周建华 舱位 订好")
            with_made_up = asked(world, monkeypatch, made_up, "吴大伟 舱位 订好")
            assert contents(with_hidden) == contents(with_made_up), embedder
            assert d_id not in {hit.id for hit in hidden.hits[-1]} | {hit.id for hit in made_up.hits[-1]}
            only_name = Capture(searcher)
            assert asked(world, monkeypatch, only_name, "周建华")["result"] == "none"
            assert d_id not in {hit.id for hit in only_name.hits[-1]}
            by_entity = Capture(searcher)  # 认出“老周”：实体那一路也不把账本里没有了的 D 连过来
            asked(world, monkeypatch, by_entity, "老周 舱位")
            assert d_id not in {hit.id for hit in by_entity.hits[-1]}
        finally:
            searcher.close()


@needs_neo4j
def test_a_full_pool_of_texts_with_a_hidden_name_does_not_change_the_answer(world: World, real: neo.NeoGraph,
                                                                             monkeypatch) -> None:
    """全文那一路的池子（这里调小到 100）满了、干净的又不够，就接着往下取：带看不到的全名问和换成编的名字问，答出来一样。"""
    flagged = [f"我：老周说第{index}批货到了。" for index in range(150)]
    clean = [f"我：海运部说第{index}批货到了。" for index in range(100)]
    packets = [day_packet("2026-11-01", "我：老周就是周建华。")]
    packets += [day_packet(f"2026-11-{2 + part:02d}", *flagged[part * 30:(part + 1) * 30]) for part in range(5)]
    packets += [day_packet(f"2026-11-{7 + part:02d}", *clean[part * 25:(part + 1) * 25]) for part in range(4)]
    marker = connected_world(world, real, ship_extract, *packets)
    searcher = gs.GraphSearch(_target(), marker_id=marker, params=replace(gs.GS2, fulltext_pool=100))
    try:
        withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
        assert run_sync(world, real, embedder=BigramEmbeddings()).status == "ok"
        real.await_indexes(120)
        answers = [asked(world, monkeypatch, searcher, question) for question in ("周建华 批货 舱位", "吴大伟 批货 舱位")]
        assert answers[0]["answered_from"] == answers[1]["answered_from"] == "graph"
        assert contents(answers[0]) == contents(answers[1])
    finally:
        searcher.close()


@needs_neo4j
def test_time_spent_checking_names_does_not_count_against_the_budget(graphed: Graphed, monkeypatch) -> None:
    """总时限只算查询的时间（这里调小到 1 秒）；问服务“字里有没有看不到的叫法”花的时间不算（不然看不到的名字多、问得久，
    这一步就超时、不用主图）。"""
    graphed.ask(monkeypatch, query="定金到账没有", limit=20)
    request = graphed.requests[-1]
    searcher = gs.GraphSearch(_target(), marker_id=graphed.searcher.marker_id, params=replace(gs.GS2, search_seconds=1.0))
    check = request.hidden_in

    def slow(items):  # type: ignore[no-untyped-def]
        import time

        time.sleep(2.6)  # Neo4j 大约两秒看一次事务超时没有：少于这个看不出来
        return check(items)  # type: ignore[misc]

    try:
        found = {hit.id for hit in searcher.search(replace(request, ledger_hits=(), hidden_in=slow))}
    finally:
        searcher.close()
    assert fact_id(graphed.world, "越南那批订单的定金到账") in found
    assert "timeout" not in searcher.stats


@needs_neo4j
def test_relations_are_checked_on_their_content_and_details(world: World, real: neo.NeoGraph, monkeypatch) -> None:
    """关系拿去比的字是“内容（另带的字段）”：撤回以后看不到的全名，写在名字那里、职位那里，都认得出。"""
    order = {"key": "e3", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}

    def extract(window: Window) -> dict[str, object]:
        found = ship_extract(window)
        entities = {str(item["key"]): item for item in found["entities"]}  # type: ignore[union-attr]
        relations = []
        for line in window.targets:
            text = line.text.rstrip("。")
            if text == "老周负责越南那批订单":
                entities.update(e1=ZHOU_FULL, e3=order)
                relations.append(relation(ZHOU_FULL, "负责", order, (line.ref, text), position="周建华组的组长"))
        return {"entities": list(entities.values()), "facts": found["facts"], "relations": relations}

    connected_world(world, real, extract, day_packet("2026-11-01", "我：老周就是周建华。"),
                    day_packet("2026-11-02", "我：老周负责越南那批订单。"))
    (rid,) = [row[0] for row in world.rows("SELECT relation_id FROM relations")]
    text = real.query(f"MATCH ()-[r:JM_RELATION {{id: $id}}]->() RETURN {gs._RELATION_TEXT_OF} AS text", id=rid)[0]["text"]
    check = live_hidden(world, "desk-bot")
    assert check([("relation", rid, text)]) == set()  # 撤回以前看得到全名：干净
    withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
    assert check([("relation", rid, text)]) == {("relation", rid)}
    assert check([("relation", rid, "老周 负责 越南那批订单（职位：周建华组的组长）")]) == {("relation", rid)}
    assert check([("relation", "R-not-there", "随便")]) == {("relation", "R-not-there")}  # 账本里没有了的：也不信


@needs_neo4j
def test_the_relation_index_for_who_it_is_on_behalf_of_is_there(graphed: Graphed) -> None:
    report = graphed.graph.index_report()
    assert report["jm_relation_org"]["properties"] == ["org_id"] and report["jm_relation_org"]["state"] == "ONLINE"


# ---------- 图里的过滤和账本视图挑的一样 ----------


def ledger_choice(world: World, caller: str, arguments: dict) -> tuple[set[tuple[str, str]], GraphRequest]:
    """账本视图（J5.5）对每一条事实、关系说行不行（看得到、现行、时间段），和它交给检索器的 GraphRequest。"""
    from javmem.query import tools

    def answer(view, request, out):  # type: ignore[no-untyped-def]
        items = [Hit("fact", row[0], 0.0) for row in view.conn.execute("SELECT fact_id FROM facts")]
        items += [Hit("relation", row[0], 0.0) for row in view.conn.execute("SELECT relation_id FROM relations")]
        chosen = {(hit.kind, hit.id) for hit in items if tools._valid_row(view, hit, request) is not None}
        graph_request = GraphRequest(
            library="prod", question="", words=(), seeds=(), scopes=tools._visible_scopes(view),
            partitions=None if request.partitions is None else tuple(request.partitions), time_from=request.time_from,
            time_to=request.time_to, include_history=request.include_history, as_of=view.as_of, want=10, ledger_hits=(),
            hidden_in=lambda items: set(),  # 这里只拿它的过滤条件
        )
        return {"chosen": chosen, "request": graph_request}

    out = tools.run_tool("memory_search", {"query": "随便", **arguments}, replace(world.settings, caller=caller),
                         tools._parse_search, answer)
    return out["chosen"], out["request"]


def graph_choice(graph: neo.NeoGraph, request: GraphRequest) -> set[tuple[str, str]]:
    params = gs.Filters.of(request).params()
    facts = graph.query(f"MATCH (f:JmFact) WHERE {gs.fact_filter('f')} RETURN f.id AS id", **params)
    relations = graph.query(f"MATCH ()-[r:JM_RELATION]->() WHERE {gs.relation_filter('r')} RETURN r.id AS id", **params)
    return {("fact", row["id"]) for row in facts} | {("relation", row["id"]) for row in relations}


@needs_neo4j
def test_the_graph_filters_choose_what_the_ledger_view_chooses(graphed: Graphed) -> None:
    """同一个身份、同一组入参，图里的条件挑出来的和账本视图挑的一样（图里没有的待确认除外）：开始日子在将来的、暂停的照出，
    结束日子过了的、被替代的默认不出；时间段、partitions、include_history。"""
    w = graphed.world
    add_hidden(w)
    order = entity(w, "越南那批订单")
    w.execute("UPDATE facts SET superseded_by = (SELECT fact_id FROM facts WHERE attribute = '尾款'),"
              " superseded_at = '2026-11-09T14:10:00+08:00' WHERE attribute = '定金'")
    w.execute("UPDATE relations SET valid_from = '2026-12', valid_from_precision = 'month' WHERE relation_id = ?",
              relation_id(w, "王强", "负责", "示例工厂"))
    w.execute("UPDATE relations SET state = 'paused' WHERE relation_id = ?", relation_id(w, "示例工厂", "客户是", "海港银行"))
    w.execute("UPDATE relations SET valid_to = '2026-11-05', valid_to_precision = 'day' WHERE relation_id = ?",
              relation_id(w, "海港银行", "客户是", "示例物流"))
    w.execute("UPDATE relations SET valid_to = '2026-11', valid_to_precision = 'month' WHERE subject_entity_id != ?"
              " AND relation_type = '任职于' AND scope_partitions = '[\"business\"]'", order)
    # 只结束、没被替代的；只被替代、没结束的（两条条件分开钉住，审阅的变异 M05、M06）
    w.execute("UPDATE relations SET state = 'ended' WHERE relation_id = ?", relation_id(w, "周海", "任职于", "示例物流"))
    w.execute("UPDATE relations SET superseded_by = ?, superseded_at = '2026-11-02T10:00:00+08:00' WHERE relation_id = ?",
              relation_id(w, "李明", "任职于", "示例贸易"), relation_id(w, "周建华", "任职于", "示例贸易"))
    # 被看不到的那条替代了的：对看不到它的身份还算现在有效（状态只认看得到的内容，M02、M07）
    hidden_fact = w.rows("SELECT fact_id FROM facts WHERE scope_partitions = '[\"finance\"]' ORDER BY fact_id LIMIT 1")[0][0]
    w.execute("UPDATE facts SET superseded_by = ?, superseded_at = '2026-11-04T10:00:00+08:00' WHERE attribute = '出货日期'"
              " AND scope_partitions = '[\"business\"]'", hidden_fact)
    graphed.sync(mode="all")
    in_graph = graph_choice(graphed.graph, replace(ledger_choice(w, "owner-console", {"include_history": True})[1]))
    assert len(in_graph) >= 15
    cases = [
        ("desk-bot", {}),
        ("desk-bot", {"include_history": True}),
        ("desk-bot", {"time_from": "2026-10-01"}),
        ("desk-bot", {"time_to": "2026-10-01"}),
        ("desk-bot", {"time_from": "2026-11-09", "time_to": "2026-11-09"}),
        ("desk-bot", {"time_from": "2026-11-05", "time_to": "2026-11-05", "include_history": True}),
        ("desk-bot", {"time_from": "2026-12-15"}),
        ("owner-console", {}),
        ("owner-console", {"partitions": ["finance"]}),
        ("owner-console", {"include_history": True, "time_to": "2026-11-04"}),
        ("books-bot", {"time_from": "2026-11-04", "time_to": "2026-11-04"}),
    ]
    for caller, arguments in cases:
        chosen, request = ledger_choice(w, caller, arguments)
        assert graph_choice(graphed.graph, request) == chosen & in_graph, (caller, arguments)
