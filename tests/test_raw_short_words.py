"""两个字的词在原文层不再整张扫（D196）：先找候选（javmem.raw.search.short_candidates：三字索引按前缀找后面还有字的，
账本 0028 的索引找在整句最后的），再照原来的 LIKE 核，结果和原来整张扫一字不差。名字、句子都是编的。
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from helpers import CHAT_ID, SHANGHAI, World, as_json, build_packet

from javmem.ledger import connect_readonly, load_migrations, migrate, schema_version
from javmem.query import graphhook, tools
from javmem.query.tools import memory_search
from javmem.raw import raw_search, withdraw_sentence
from javmem.raw import search as rs
from javmem.zh.search import LANE, search_key

AS_OF = datetime(2026, 11, 10, 9, 30, tzinfo=SHANGHAI)
BODY = "\n".join([
    "## 原话",
    "- [10:30] 我：我们明天去港口看货。",  # 1.1.1 在句中
    "- [10:31] 我：货已经到了港口",  # 1.2.1 在整句最后（后面没有字，三字索引里没有）
    "- [10:32] 我：港口",  # 1.3.1 整句就这两个字（一个三字都没有）
    "- [10:33] 我：3号到港口",  # 1.4.1 两份写法（⟦3⟧日到港口、⟦3⟧号到港口）：前一份末尾在接缝前面，后一份末尾在整句最后
    "- [10:34] 我：AB 两个字母",  # 1.5.1 英文：查 ab、AB 都一样
    "- [10:35] 我：港务局说口岸开了。",  # 1.6.1 “港”“口”都有，不挨着：不算
    "- [10:36] 我：码头的价格涨了",  # 1.7.1
    "- [10:37] 我：到港口3号",  # 1.8.1 前一份的末尾（⟧日）只在接缝前面
    "",
])
HARBOR = [f"{CHAT_ID}#1.{line}.1" for line in (8, 4, 3, 2, 1)]  # 有“港口”的，新的在前
QUESTIONS = ["港口", "ab", "AB", "口岸", "码头", "看货", "字母", "港口 码头", "港口 价格", "看货 口岸 字母", "没有 这个"]


@pytest.fixture
def harbor(world: World) -> World:
    world = world.at(AS_OF).as_caller("desk-bot")
    world.drop("inbox/desk-bot", build_packet(body=BODY))
    assert world.intake().count("accepted") == 1
    return world


def ids(result: dict) -> list[str]:
    return [item["id"] for item in result["items"]]


def search(world: World, **arguments: object) -> dict:
    return as_json(raw_search(arguments, settings=world.settings))  # type: ignore[return-value]


def fallback(world: World, monkeypatch: pytest.MonkeyPatch, query: str) -> dict:
    """memory_search 回退原文层：主图没接，这本小账本只收了包、没抽，账本视图里什么都没有。"""
    monkeypatch.setattr(graphhook, "open_searcher", lambda settings, library: None)
    answer = as_json(memory_search({"query": query, "limit": 20}, settings=world.settings))
    assert answer["answered_from"] in ("raw", None)  # type: ignore[index]
    return answer  # type: ignore[return-value]


def like_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """改回原来的样子：两个字的词都走 LIKE（整张扫）。"""
    monkeypatch.setattr(rs, "short_candidates", lambda conn, key, limit: None)
    monkeypatch.setattr(tools, "short_candidates", lambda conn, key, limit: None)


def traced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """记下 raw_search、memory_search 在只读连接上跑的每一句（参数已经代进去了）。"""
    statements: list[str] = []
    real = connect_readonly

    def tracing(path):  # type: ignore[no-untyped-def]
        conn = real(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(rs, "connect_readonly", tracing)
    monkeypatch.setattr(tools, "connect_readonly", tracing)
    return statements


def plan_of(world: World, sql: str) -> list[str]:
    conn = connect_readonly(world.settings.library("prod").db_path)
    try:
        return [str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    finally:
        conn.close()


def scans_the_raw_table(plan: list[str]) -> bool:
    """原文表不是按编号一句句取的：整张扫（SCAN s）、顺着时间索引走（SCAN s USING INDEX …），或者先走包、再把每个包的
    句子全读一遍（SEARCH s USING INDEX raw_sentences_by_packet，原来回退原文层就是这样）。"""
    raw = [line for line in plan if re.match(r"(SCAN|SEARCH) s\b", line)]
    return not raw or any(not line.startswith("SEARCH s USING INTEGER PRIMARY KEY") for line in raw)


# ---------- 找得到的地方（和原来整张扫的一样） ----------


def test_two_character_words_are_found_wherever_they_are(harbor: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """句中、整句最后、整句就这两个字、两份写法的接缝前面，raw_search 和回退原文层都找得到；和原来整张扫比，每个问法的
    返回一个字都不差。"""
    assert ids(search(harbor, query="港口")) == HARBOR
    assert ids(search(harbor, query="ab")) == ids(search(harbor, query="AB")) == [f"{CHAT_ID}#1.5.1"]
    assert search(harbor, query="港口 码头")["result"] == "none"  # raw_search 要几个词命中同一句
    assert set(ids(fallback(harbor, monkeypatch, "港口 码头"))) == {*HARBOR, f"{CHAT_ID}#1.7.1"}  # 回退原文层任一词就算
    now = {query: (search(harbor, query=query), fallback(harbor, monkeypatch, query)) for query in QUESTIONS}
    like_only(monkeypatch)
    for query in QUESTIONS:
        assert (search(harbor, query=query), fallback(harbor, monkeypatch, query)) == now[query], query


def test_candidates_cover_every_two_characters_of_every_sentence(harbor: World) -> None:
    """每一句统一写法里挨着的两个字（不跨两份写法中间的接缝），拿去找候选，都找回这一句：候选只多不少，照 LIKE 核完和整张扫
    一样。大小写会变的非 ASCII 字不走候选（SQLite 换小写和 Python 的 casefold 不完全一样），照旧 LIKE。"""
    conn = connect_readonly(harbor.settings.library("prod").db_path)
    try:
        rows = [(row["id"], row["norm"]) for row in conn.execute("SELECT id, norm FROM raw_sentences")]
        keys = {norm[i : i + 2] for _, norm in rows for i in range(len(norm) - 1)}
        keys = {key for key in keys if LANE not in key}
        assert "⟧日" in keys and "港口" in keys and "ab" in keys
        for key in sorted(keys):
            candidates = rs.short_candidates(conn, key, 10_000)
            assert candidates is not None, key
            assert {sid for sid, norm in rows if key in norm} <= set(candidates), key
        assert rs.short_candidates(conn, "港口", 10_000) == sorted(
            row[0] for row in conn.execute("SELECT id FROM raw_sentences WHERE norm LIKE '%港口%'"))
        assert rs.short_candidates(conn, "港口", 4) is None  # 5 句，超过门槛：照旧 LIKE
        assert rs.short_candidates(conn, search_key("ΑΒ"), 10_000) is None  # 希腊字母分大小写
        assert rs.short_candidates(conn, "港口看", 10_000) is None  # 三个字的走三字索引，不在这里
    finally:
        conn.close()


def test_cased_letters_outside_ascii_keep_the_old_way(world: World) -> None:
    world = world.at(AS_OF).as_caller("desk-bot")
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：ΑΒΓ 三个希腊字母\n"))
    world.intake()
    assert ids(search(world, query="ΑΒ")) == ids(search(world, query="αβ")) == [f"{CHAT_ID}#1.1.1"]


# ---------- 不再整张扫 ----------


def test_two_character_words_do_not_scan_the_raw_table(harbor: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """查询计划：候选不多的两字词，原文表按编号一句句取（SEARCH s USING INTEGER PRIMARY KEY），不再整张扫，也不再顺着时间
    索引走到底（D196：5,000 包那本 242 万句，冷着 16 秒）。raw_search 一个两字词、回退原文层两个两字词各看一遍。"""
    statements = traced(monkeypatch)
    assert ids(search(harbor, query="港口")) == HARBOR
    assert fallback(harbor, monkeypatch, "港口 码头")["items"]
    found = [sql for sql in statements if "FROM raw_sentences s JOIN packets p" in sql and "ORDER BY s.said_at_epoch DESC" in sql]
    hits = [sql for sql in statements if ") AS hits" in sql]
    assert len(found) == 1 and len(hits) == 1
    for sql in (*found, *hits):
        plan = plan_of(harbor, sql)
        assert not scans_the_raw_table(plan), plan
        assert any(line.startswith("SEARCH s USING INTEGER PRIMARY KEY") for line in plan), plan


def test_a_very_common_two_character_word_still_scans_and_finds_the_same(harbor: World,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """候选太多的词（这里把两处门槛都调到 2 句）照旧整张扫：一句句按编号取反而比整张读一遍慢。结果和走候选的一样。"""
    before = {query: (search(harbor, query=query), fallback(harbor, monkeypatch, query)) for query in QUESTIONS}
    monkeypatch.setattr(rs, "RAW_SEARCH_CANDIDATES", 2)
    monkeypatch.setattr(rs, "FALLBACK_MIN_CANDIDATES", 2)
    statements = traced(monkeypatch)
    for query in QUESTIONS:
        assert (search(harbor, query=query), fallback(harbor, monkeypatch, query)) == before[query], query

    def only(word: str) -> list[list[str]]:
        """只查这一个词的那两句（raw_search 一句、回退原文层一句）的查询计划。"""
        found = [sql for sql in statements if ("ORDER BY s.said_at_epoch DESC" in sql or ") AS hits" in sql)
                 and set(re.findall(r"LIKE '%([^']*)%'", sql)) == {word}]
        assert len(found) == 2, found
        return [plan_of(harbor, sql) for sql in found]

    assert all(scans_the_raw_table(plan) for plan in only("港口")), only("港口")  # 5 句，过了门槛
    assert not any(scans_the_raw_table(plan) for plan in only("码头")), only("码头")  # 1 句：照样走候选


# ---------- 撤回、换版本 ----------


def test_withdrawn_and_replaced_two_character_hits_are_gone(harbor: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """撤回了的句子、换了版本以后没了的句子，三字索引、句末那个索引里删掉的那一半要真没了：两条路都找不到。"""
    conn = harbor.ledger()
    try:
        assert withdraw_sentence(conn, f"{CHAT_ID}#1.2.1", "2026-11-10T09:00:00+08:00")
    finally:
        conn.close()
    assert ids(search(harbor, query="港口")) == [one for one in HARBOR if not one.endswith("#1.2.1")]
    assert f"{CHAT_ID}#1.2.1" not in ids(fallback(harbor, monkeypatch, "港口"))
    harbor.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：我们明天去码头看货\n"))
    assert harbor.intake().count("replaced") == 1
    assert search(harbor, query="港口")["result"] == "none"
    assert fallback(harbor, monkeypatch, "港口")["result"] == "none"
    assert ids(search(harbor, query="看货")) == [f"{CHAT_ID}#1.1.1"]  # 第二版里在整句最后
    assert search(harbor, query="看货")["items"][0]["evidence"][0]["revision"] == 2


# ---------- 账本 0028 ----------


def test_old_ledgers_get_the_tail_index_when_upgraded(harbor: World) -> None:
    """账本 0028：只加一个索引（每句统一写法的最后两个字）。老账本升上来，以前的句子都在索引里，按它查走索引。"""
    [tail] = [one for one in load_migrations() if one.name == "0028_raw_norm_tail.sql"]
    conn = harbor.ledger()
    try:
        conn.execute("DROP INDEX raw_sentences_norm_tail")  # 照升以前的样子：0027 的账本
        conn.execute(f"PRAGMA user_version = {tail.version - 1}")
        # 只升这一步：后面的迁移（0029 加了一列）这本账里已经做过了，再做一遍会报“列已经有了”
        assert migrate(conn, [one for one in load_migrations() if one.version <= tail.version]) == [tail.version]
        assert schema_version(conn) == tail.version
        lookup = "SELECT sentence_id FROM raw_sentences WHERE substr(norm, -2) = ?"
        plan = [str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + lookup, ("港口",))]
        assert any("raw_sentences_norm_tail" in line for line in plan), plan
        assert {row[0] for row in conn.execute(lookup, ("港口",))} == {f"{CHAT_ID}#1.{line}.1" for line in (2, 3, 4)}
    finally:
        conn.close()
