"""巡检盯着的快慢（巡检加快慢检查，11-16）：待确认、一组、人和事、磁盘、写账。

这几样现在都不慢，可会越攒越多，攒多了写账会慢：到了数就提醒（数在 config.PatrolLimits，环境变量可改），处理掉就不报，
不能标看过了。只报数、编号、大小，不抄原文。巡检只读；写账记的那一行（write_runs）是 javmem write 写的。样例都是编的。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import CHAT_PACKET, MAIL_PACKET, World, build_packet
from test_speed import full_scans

from javmem import cli, patrol
from javmem.cli import main
from javmem.config import ConfigError, PatrolLimits, Settings
from javmem.embedding import Embedder, EmbeddingError, EmbeddingReply, FakeEmbeddings, FakeEmbeddingServer
from javmem.ledger import connect, latest_version, load_migrations, migrate, schema_version, transaction
from javmem.patrol import PatrolItem, WriteTimer, record_write_run, run_patrol

SECRET = "槐荫渡口第七号木箱贴了蓝签"  # 编的：待确认、事实、实体里都放它，巡检的输出里不该出现
STAMP = "2026-11-16T09:00:00+08:00"
GROWTH_KINDS = {"review_queue_large", "group_large", "entities_many", "disk_low", "write_slower"}


def limited(world: World, **limits: object) -> World:
    """这个世界的巡检换几个数。"""
    return replace(world, settings=replace(world.settings, patrol=replace(world.settings.patrol, **limits)))


def found(world: World, kind: str) -> list[PatrolItem]:
    return [item for item in run_patrol(world.settings) if item.kind == kind]


def output_of(items: list[PatrolItem]) -> str:
    return json.dumps([item.to_dict() for item in items], ensure_ascii=False)


@pytest.fixture
def ready(world: World) -> World:
    """收过一次件：两本账本都建好了，巡检本来没事。"""
    world.intake()
    assert run_patrol(world.settings) == []
    return world


def roomy(monkeypatch: pytest.MonkeyPatch, free: int) -> list[str]:
    """磁盘剩多少由测试定。返回巡检问过哪几个目录。"""
    asked: list[str] = []

    def usage(path: object) -> SimpleNamespace:
        asked.append(str(path))
        return SimpleNamespace(total=10**15, used=0, free=free)

    monkeypatch.setattr(patrol.shutil, "disk_usage", usage)
    return asked


# ========== 配置 ==========


def test_limits_have_the_documented_defaults() -> None:
    assert Settings.from_env({}).patrol == PatrolLimits(
        review_items=2000, group_facts=1000, entities=20000, disk_ratio=2.0, write_ratio=2.0, write_runs=30, write_min_runs=5
    )


def test_limits_are_read_from_the_environment() -> None:
    settings = Settings.from_env(
        {
            "JAVMEM_PATROL_REVIEW_MAX": "300",
            "JAVMEM_PATROL_GROUP_MAX": " 40 ",
            "JAVMEM_PATROL_ENTITIES_MAX": "5000",
            "JAVMEM_PATROL_DISK_RATIO": "1.5",
            "JAVMEM_PATROL_WRITE_RATIO": "3",
            "JAVMEM_PATROL_WRITE_RUNS": "10",
            "JAVMEM_PATROL_WRITE_MIN_RUNS": "3",
        }
    )
    assert settings.patrol == PatrolLimits(300, 40, 5000, 1.5, 3.0, 10, 3)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("JAVMEM_PATROL_REVIEW_MAX", "lots"),
        ("JAVMEM_PATROL_REVIEW_MAX", "0"),
        ("JAVMEM_PATROL_REVIEW_MAX", "-5"),
        ("JAVMEM_PATROL_REVIEW_MAX", "2,000"),
        ("JAVMEM_PATROL_REVIEW_MAX", "1.5"),
        ("JAVMEM_PATROL_REVIEW_MAX", "1e3"),
        ("JAVMEM_PATROL_GROUP_MAX", "0"),
        ("JAVMEM_PATROL_ENTITIES_MAX", "99999999999"),
        ("JAVMEM_PATROL_DISK_RATIO", "0.5"),
        ("JAVMEM_PATROL_DISK_RATIO", "two"),
        ("JAVMEM_PATROL_DISK_RATIO", "nan"),
        ("JAVMEM_PATROL_DISK_RATIO", "inf"),
        ("JAVMEM_PATROL_DISK_RATIO", "101"),
        ("JAVMEM_PATROL_WRITE_RATIO", "1"),
        ("JAVMEM_PATROL_WRITE_RATIO", "1e400"),
        ("JAVMEM_PATROL_WRITE_RUNS", "0"),
        ("JAVMEM_PATROL_WRITE_RUNS", "5000"),
        ("JAVMEM_PATROL_WRITE_MIN_RUNS", "1"),
    ],
)
def test_bad_limits_are_refused(name: str, text: str) -> None:
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({name: text})


def test_a_bad_limit_stops_the_command_before_anything_is_touched(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PATROL_GROUP_MAX", "很多")
    assert main(["patrol"]) == 2
    assert main(["intake"]) == 2  # 和 JAVMEM_STALE_HOURS 一样：写错了哪个命令都不跑
    assert not (world.data / "ledger").exists()


# ========== 1. 待确认太多 ==========


def add_reviews(world: World, open_count: int, decided_count: int) -> None:
    claims = json.dumps({"text": SECRET}, ensure_ascii=False)
    quotes = json.dumps([SECRET], ensure_ascii=False)
    rows = [(claims, quotes, None, None)] * open_count + [(claims, quotes, "both", STAMP)] * decided_count
    conn = world.ledger()
    try:
        conn.executemany(
            "INSERT INTO review_queue (kind, claims, quotes, decision, decided_at, created_at)"
            " VALUES ('manual_review', ?, ?, ?, ?, '2026-11-16T08:00:00+08:00')",
            rows,
        )
    finally:
        conn.close()


def test_too_many_reviews_are_reported_with_both_counts(ready: World) -> None:
    world = limited(ready, review_items=5)
    add_reviews(world, 2, 3)
    assert found(world, "review_queue_large") == []  # 正好 5 条，没超过
    add_reviews(world, 0, 1)  # 处理过的也算：写账照样要看
    [item] = found(world, "review_queue_large")
    assert (item.library, item.stats) == ("prod", {"total": 6, "open": 2, "decided": 4, "limit": 5})
    assert "6" in item.message and "2" in item.message and "4" in item.message
    assert item.count is None and item.ref is None  # 不是“来了几次”，也不能标看过了
    assert SECRET not in output_of([item])


def test_the_review_limit_defaults_to_two_thousand(ready: World) -> None:
    add_reviews(ready, 1500, 500)
    assert found(ready, "review_queue_large") == []
    add_reviews(ready, 0, 1)
    assert [item.stats["total"] for item in found(ready, "review_queue_large")] == [2001]  # type: ignore[index]


def test_decided_means_it_has_a_decision_time(ready: World) -> None:
    """处理过没有看 decided_at（和写账、javmem review 一样），不看 decision 那一列有没有字。"""
    world = limited(ready, review_items=1)
    add_reviews(world, 1, 0)
    conn = world.ledger()
    try:
        conn.execute(
            "INSERT INTO review_queue (kind, claims, quotes, decided_at, created_at) VALUES ('manual_review', '{}', '[]', ?, ?)",
            (STAMP, STAMP),
        )
    finally:
        conn.close()
    assert [(item.stats["open"], item.stats["decided"]) for item in found(world, "review_queue_large")] == [(1, 1)]  # type: ignore[index]


# ========== 2. 一组太长 ==========


def add_group(world: World, size: int, *, group: str | None, subject: str = "E7") -> None:
    conn = world.ledger()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO entities (entity_id, canonical_name, type, created_at, updated_at)"
            " VALUES (?, ?, '人', 't', 't')",
            (subject, f"{SECRET}{subject}"),
        )
        start = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
        conn.executemany(
            "INSERT INTO facts (fact_id, content, subject_entity_id, attribute, value, status, scope_partitions, group_key,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'inferred', '[\"business\"]', ?, 't', 't')",
            [(f"F{start + i}", f"{SECRET}{start + i}", subject, SECRET, str(i), group) for i in range(size)],
        )
    finally:
        conn.close()


def test_the_largest_group_is_reported_by_subject_and_size(ready: World) -> None:
    world = limited(ready, group_facts=4)
    add_group(world, 4, group="g1", subject="E7")
    add_group(world, 3, group="g2", subject="E8")
    assert found(world, "group_large") == []  # 最大的一组正好 4 条
    add_group(world, 2, group="g2", subject="E8")  # g2 到 5 条
    add_group(world, 6, group="g3", subject="E9")
    [item] = found(world, "group_large")
    assert (item.library, item.stats) == ("prod", {"facts": 6, "subject_entity_id": "E9", "groups_over": 2, "limit": 4})
    assert "E9" in item.message
    assert SECRET not in output_of([item])  # 属性、值、事实文字都不抄


def test_facts_without_a_group_are_not_a_group(ready: World) -> None:
    world = limited(ready, group_facts=2)
    add_group(world, 10, group=None)  # 没有主体属性的、多值的，group_key 是空的
    assert found(world, "group_large") == []


def test_finding_the_largest_group_only_reads_the_group_index(ready: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """要知道最大的一组多大，只读 facts_by_group 这个索引，不回表读事实（每一行带着事实文字）；跑过 ANALYZE 也一样。
    巡检也不碰说法、证据、原文、向量这几张大表。"""
    world = limited(ready, group_facts=2)
    add_group(world, 3, group="g1")
    add_group(world, 2, group=None, subject="E8")
    conn = world.ledger()
    try:
        conn.execute("ANALYZE")
    finally:
        conn.close()

    seen: list[str] = []
    original = patrol.open_ledger

    def traced(library):  # noqa: ANN001, ANN202
        conn = original(library)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(patrol, "open_ledger", traced)
    assert [item.stats["facts"] for item in found(world, "group_large")] == [3]  # type: ignore[index]
    conn = world.ledger()
    try:
        assert any("group_key" in sql for sql in seen)  # 真的记下了
        assert full_scans(conn, seen, "facts", covering_ok=True) == []
        for table in ("fact_claims", "evidence", "raw_sentences", "fact_vectors", "relations", "relation_claims"):
            assert full_scans(conn, seen, table) == [], table
        plan = " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {patrol.LARGE_GROUPS_SQL}", (2,)))
        assert "COVERING INDEX facts_by_group" in plan
    finally:
        conn.close()


def test_the_group_index_is_used_even_when_the_statistics_are_skewed(ready: World) -> None:
    """统计表很歪的时候（说索引有一亿行、表只有十行），不写死索引的话 SQLite 会改成整张扫事实表；写死了照样走索引。"""
    add_group(ready, 3, group="g1")
    conn = ready.ledger()
    try:
        conn.execute("ANALYZE")
        conn.execute("DELETE FROM sqlite_stat1 WHERE tbl = 'facts'")
        conn.execute(
            "INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES ('facts', 'facts_by_group', '100000000 1'), ('facts', NULL, '10')"
        )
        conn.execute("ANALYZE sqlite_schema")  # 让改过的统计表生效

        def plan(sql: str) -> str:
            return " | ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", (1,)))

        if not plan(patrol.LARGE_GROUPS_SQL.replace(" INDEXED BY facts_by_group", "")).startswith("SCAN facts"):
            pytest.skip("这一版 SQLite 统计表歪了也不改成整张扫，写死索引防的那种情形碰不上")
        assert "COVERING INDEX facts_by_group" in plan(patrol.LARGE_GROUPS_SQL)
    finally:
        conn.close()


# ========== 3. 人和事太多 ==========


def add_entities(world: World, count: int, *, aliases: int = 0) -> None:
    conn = world.ledger()
    try:
        start = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        conn.executemany(
            "INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at) VALUES (?, ?, '人', 't', 't')",
            [(f"E{start + i}", f"{SECRET}{start + i}") for i in range(count)],
        )
        owner = f"E{start}"
        conn.executemany(
            "INSERT INTO aliases (alias_id, entity_id, alias, status, created_at) VALUES (?, ?, ?, 'verified', 't')",
            [(f"A{start + i}", owner, f"{SECRET}别名{i}") for i in range(aliases)],
        )
    finally:
        conn.close()


def test_too_many_entities_are_reported(ready: World) -> None:
    world = limited(ready, entities=3)
    add_entities(world, 3)
    assert found(world, "entities_many") == []
    add_entities(world, 1)
    [item] = found(world, "entities_many")
    assert (item.library, item.stats) == ("prod", {"entities": 4, "aliases": 0, "limit": 3})
    assert SECRET not in output_of([item])


def test_too_many_aliases_alone_are_reported(ready: World) -> None:
    world = limited(ready, entities=3)
    add_entities(world, 1, aliases=4)
    [item] = found(world, "entities_many")
    assert item.stats == {"entities": 1, "aliases": 4, "limit": 3}
    assert "别名" in item.message


# ========== 4. 账本太大、磁盘快满 ==========


def ledger_bytes(world: World, library: str) -> int:
    db = world.settings.library(library).db_path
    wal = db.with_name(db.name + "-wal")
    return db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


def test_low_disk_is_reported_with_the_ledger_size(ready: World, monkeypatch: pytest.MonkeyPatch) -> None:
    size = ledger_bytes(ready, "prod")
    asked = roomy(monkeypatch, 2 * size)
    assert [item.library for item in found(ready, "disk_low")] == [
        name for name in ("prod", "test") if 2 * size < 2 * ledger_bytes(ready, name)
    ]  # 正好两倍不报
    assert set(asked) == {str(ready.settings.library("prod").db_path.parent)}  # 问的是账本所在的那个盘

    roomy(monkeypatch, 2 * size - 1)
    items = {item.library: item for item in found(ready, "disk_low")}
    assert items["prod"].stats == {"ledger_bytes": size, "free_bytes": 2 * size - 1, "limit": 2.0}
    assert "2 倍" in items["prod"].message

    roomy(monkeypatch, 10**12)  # 腾出地方以后就不报了
    assert found(ready, "disk_low") == []


def test_the_disk_check_counts_the_wal_too(ready: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """账本旁边的 -wal 也算账本的大小：写到一半、还没并回去的都在里面。"""
    db = ready.settings.library("prod").db_path
    holder = connect(db)  # 一直开着：-wal 不会被收回去
    try:
        holder.execute("PRAGMA wal_autocheckpoint = 0")
        with transaction(holder):
            holder.executemany(
                "INSERT INTO rules (rule_id, version, layer, created_at) VALUES (?, 'v', 'L4', 't')",
                [(f"X{i}" * 50,) for i in range(200)],
            )
        assert db.with_name(db.name + "-wal").stat().st_size > 0
        roomy(monkeypatch, 0)
        [item] = [item for item in found(ready, "disk_low") if item.library == "prod"]
        assert item.stats["ledger_bytes"] == ledger_bytes(ready, "prod") > db.stat().st_size  # type: ignore[index]
    finally:
        holder.close()


# ========== 5. 写账变慢 ==========
# 比法是 11-16 定的：最近一次按窗口算（扣掉等向量服务的时间），和前面窗口数差不多（2/3 到 1.5 倍）的 30 次的
# 中间数比，2 倍或更多就报；连同最近一次凑不够 5 次不报。


def add_runs(world: World, *runs: tuple[float, int], embedding: float = 0.0, packets: int = 1, stopped: bool = False) -> None:
    """记几次写账：(用了几秒, 写了几个窗口)。embedding 是其中等向量服务的秒数，packets 是写完了几个包。"""
    conn = world.ledger()
    try:
        for seconds, windows in runs:
            record_write_run(
                conn,
                started_at=STAMP,
                seconds=seconds,
                embedding_seconds=embedding,
                packets=packets,
                windows=windows,
                facts=0,
                stopped=stopped,
            )
    finally:
        conn.close()


def test_slower_writing_needs_five_runs_on_record(ready: World) -> None:
    add_runs(ready, (1.0, 1), (1.0, 1), (1.0, 1), (3.0, 1))
    assert found(ready, "write_slower") == []  # 才 4 次
    add_runs(ready, (3.0, 1))
    [item] = found(ready, "write_slower")  # 前 4 次 1、1、1、3 秒，中间数 1 秒
    assert item.library == "prod"
    assert item.stats == {
        "started_at": STAMP,
        "windows": 1,
        "seconds_per_window": 3.0,
        "median_seconds_per_window": 1.0,
        "runs_compared": 4,
        "ratio": 3.0,
        "limit": 2.0,
    }
    assert item.count is None and item.ref is None


def test_twice_the_median_is_already_slower(ready: World) -> None:
    add_runs(ready, *[(2.0, 1)] * 4, (3.98, 1))
    assert found(ready, "write_slower") == []
    add_runs(ready, (4.0, 1))  # 前 5 次里有一次 3.98，中间数还是 2
    assert [item.stats["ratio"] for item in found(ready, "write_slower")] == [2.0]  # type: ignore[index]


def test_writing_speed_is_per_window(ready: World) -> None:
    add_runs(ready, *[(10.0, 10)] * 4, (19.0, 10))  # 10 个窗口 19 秒：每个窗口 1.9 秒
    assert found(ready, "write_slower") == []
    add_runs(ready, (20.0, 10), packets=3)  # 写完了几个包不管
    assert [item.stats["seconds_per_window"] for item in found(ready, "write_slower")] == [2.0]  # type: ignore[index]


def test_runs_with_far_fewer_or_more_windows_are_not_compared(ready: World) -> None:
    """每次写账都有一块和窗口数无关的固定活（补向量、出汇总）：一次写 4 个窗口 6 秒、一次写 1 个窗口 3 秒，按窗口算差
    2 倍，可并没有变慢（审阅第一遍第 1 条的例子）。"""
    add_runs(ready, *[(6.0, 4)] * 30)
    add_runs(ready, (3.0, 1))
    assert found(ready, "write_slower") == []  # 前面没有窗口数差不多的
    add_runs(ready, *[(3.0, 1)] * 4)
    assert found(ready, "write_slower") == []
    add_runs(ready, (7.0, 1))
    [item] = found(ready, "write_slower")
    assert (item.stats["median_seconds_per_window"], item.stats["runs_compared"]) == (3.0, 5)  # type: ignore[index]


def test_about_as_many_windows_is_two_thirds_to_one_and_a_half(ready: World) -> None:
    add_runs(ready, *[(10.0, 1)] * 10, *[(50.0, 5)] * 10)  # 1 个、5 个窗口：和 3 个差得多（又都慢）
    add_runs(ready, *[(2.0, 2)] * 2, *[(4.0, 4)] * 2)  # 2 个、4 个窗口：算差不多，每个窗口 1 秒
    add_runs(ready, (6.0, 3))  # 3 个窗口，每个 2 秒
    [item] = found(ready, "write_slower")
    assert (item.stats["median_seconds_per_window"], item.stats["runs_compared"]) == (1.0, 4)  # type: ignore[index]


def test_both_ends_of_about_as_many_windows_count(ready: World) -> None:
    """最近一次 10 个窗口：7 个（不少于 2/3）、15 个（正好 1.5 倍）算差不多；6 个、16 个不算（审阅第三遍第 2 条）。"""
    add_runs(ready, *[(60.0, 6)] * 10, *[(160.0, 16)] * 10)  # 差得多的，又都慢（每个窗口 10 秒）
    add_runs(ready, *[(7.0, 7)] * 2, *[(15.0, 15)] * 2)  # 差不多的，每个窗口 1 秒
    add_runs(ready, (20.0, 10))  # 每个窗口 2 秒
    [item] = found(ready, "write_slower")
    assert (item.stats["median_seconds_per_window"], item.stats["runs_compared"]) == (1.0, 4)  # type: ignore[index]


def test_the_last_thirty_runs_with_about_as_many_windows_are_compared(ready: World) -> None:
    """从最近往前一行一行找，窗口数差得多的跳过，凑够 30 次就停。"""
    add_runs(ready, *[(10.0, 1)] * 30)  # 最早的 30 次：慢
    add_runs(ready, *[(1.0, 1)] * 30)  # 接着 30 次：快
    add_runs(ready, *[(100.0, 10)] * 30)  # 最近 30 次：窗口数差得多
    add_runs(ready, (2.5, 1))
    [item] = found(ready, "write_slower")
    assert (item.stats["median_seconds_per_window"], item.stats["runs_compared"]) == (1.0, 30)  # type: ignore[index]


def test_waiting_for_the_embedding_service_does_not_count(ready: World) -> None:
    """真实写账大半时间在等向量服务，它快慢和账本多大无关：扣掉再比。"""
    add_runs(ready, *[(1.0, 1)] * 4)
    add_runs(ready, (30.0, 1), embedding=29.0)
    assert found(ready, "write_slower") == []
    add_runs(ready, (30.0, 1), embedding=28.0)
    assert [item.stats["seconds_per_window"] for item in found(ready, "write_slower")] == [2.0]  # type: ignore[index]


def test_stopped_runs_and_runs_without_windows_are_not_compared(ready: World) -> None:
    add_runs(ready, *[(1.0, 1)] * 5)
    add_runs(ready, *[(10.0, 1)] * 6, stopped=True)  # 向量服务调不通、停下了的
    add_runs(ready, *[(10.0, 0)] * 6, packets=3)  # 窗口都写过了、只把包改成 done 的
    assert found(ready, "write_slower") == []  # 最近一次算数的是第 5 次正常的
    add_runs(ready, (2.5, 1))
    [item] = found(ready, "write_slower")  # 拿来比的只有那 5 次正常的，中间数 1 秒
    assert (item.stats["median_seconds_per_window"], item.stats["runs_compared"]) == (1.0, 5)  # type: ignore[index]


def test_packets_left_queued_still_count_by_their_windows(ready: World) -> None:
    """有窗口还没结果、包留着排队的那几次：写了的窗口照样算，一个包都没写完也照样拿来比。"""
    add_runs(ready, *[(5.0, 5)] * 4)
    add_runs(ready, (5.0, 5), packets=0)  # 窗口写了 5 个，包一个都没写完：每个窗口 1 秒，不慢
    assert found(ready, "write_slower") == []
    add_runs(ready, (10.0, 5), packets=0)
    assert [item.stats["runs_compared"] for item in found(ready, "write_slower")] == [5]  # type: ignore[index]


def test_the_write_limits_can_be_changed(ready: World) -> None:
    world = limited(ready, write_ratio=1.5, write_min_runs=2, write_runs=1)
    add_runs(world, (9.0, 1), (1.0, 1), (1.4, 1))
    assert found(world, "write_slower") == []
    add_runs(world, (2.1, 1))  # 只和前 1 次（1.4 秒）比
    assert [item.stats["ratio"] for item in found(world, "write_slower")] == [1.5]  # type: ignore[index]


def test_a_median_of_nothing_is_not_compared(ready: World) -> None:
    add_runs(ready, *[(1.0, 1)] * 4, embedding=1.0)  # 全在等向量服务：写账自己的是 0
    add_runs(ready, (5.0, 1))
    assert found(ready, "write_slower") == []


def test_odd_limits_made_in_code_do_not_crash_the_patrol(ready: World) -> None:
    """环境变量来的数挡得住；代码里直接造的（比前 0 次、不到 1 次不报）也不能让巡检崩掉。"""
    add_runs(ready, (1.0, 1))
    assert found(limited(ready, write_min_runs=1), "write_slower") == []
    add_runs(ready, (5.0, 1))
    assert found(limited(ready, write_runs=0, write_min_runs=1), "write_slower") == []


def test_a_normal_run_clears_the_reminder(ready: World) -> None:
    add_runs(ready, *[(1.0, 1)] * 4, (5.0, 1))
    assert len(found(ready, "write_slower")) == 1
    add_runs(ready, (1.1, 1))
    assert found(ready, "write_slower") == []


# ---- javmem write 记的那一行 ----


def test_each_write_records_how_long_it_took(world: World, tmp_path: Path) -> None:
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    summary_file = tmp_path / "write.json"
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings", "--summary", str(summary_file)]) == 0
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    [row] = world.rows("SELECT * FROM write_runs")
    assert (row["packets"], row["windows"]) == (summary["this_run"]["packets_done"], summary["this_run"]["windows_written"])
    assert row["packets"] == 2 and row["windows"] >= 2
    assert (row["facts"], row["stopped"]) == (summary["facts"], 0)
    assert 0 <= row["embedding_seconds"] <= row["seconds"] < 600
    assert row["started_at"].startswith("20")

    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0  # 什么都没写：不记
    assert len(world.rows("SELECT * FROM write_runs")) == 1
    assert world.rows("SELECT count(*) AS n FROM write_runs", library="test")[0]["n"] == 0  # 各记各的账本


def test_the_time_spent_waiting_for_embeddings_is_recorded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    original = FakeEmbeddings.embed

    def slow(self: FakeEmbeddings, texts: list[str]) -> EmbeddingReply:
        calls.append(len(texts))
        time.sleep(0.03)
        return original(self, texts)

    monkeypatch.setattr(FakeEmbeddings, "embed", slow)
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    [row] = world.rows("SELECT * FROM write_runs")
    assert calls
    assert 0.9 * 0.03 * len(calls) <= row["embedding_seconds"] <= row["seconds"]  # 留一点余量：睡眠的钟点不是那么准


def test_a_stopped_write_is_recorded_as_stopped(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    with FakeEmbeddingServer(api_key="k", replies=[400] * 50) as server:
        monkeypatch.setenv("JAVMEM_EMBEDDING_BASE_URL", server.base_url)
        monkeypatch.setenv("JAVMEM_EMBEDDING_API_KEY", "k")
        assert main(["write", "--library", "prod", "--fake"]) == 4  # 向量服务一直拒：账照写，停下
    [row] = world.rows("SELECT * FROM write_runs")
    assert (row["stopped"], row["packets"]) == (1, 2)


def test_a_failed_write_is_not_recorded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0

    def broken(conn: sqlite3.Connection) -> dict[str, object]:
        raise sqlite3.OperationalError("编的：磁盘满了")

    monkeypatch.setattr(cli, "ledger_summary", broken)
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 5
    assert world.rows("SELECT count(*) AS n FROM write_runs")[0]["n"] == 0


def test_write_runs_only_hold_numbers(world: World) -> None:
    """撤回、彻底删除不用管 write_runs：它只记数（几秒、几个包、几条事实），不记原话、名字、包里的内容。"""
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    conn = world.ledger()
    try:
        columns = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(write_runs)")}
        assert {name for name, kind in columns.items() if kind not in ("INTEGER", "REAL")} == {"started_at"}
        [row] = conn.execute("SELECT * FROM write_runs").fetchall()
        started = datetime.fromisoformat(row["started_at"])  # 带时区的时刻，写到秒
        assert started.tzinfo is not None and started.isoformat(timespec="seconds") == row["started_at"]
        assert all(type(row[name]) in (int, float) for name in columns if name != "started_at")
    finally:
        conn.close()


def test_a_run_that_wrote_nothing_is_not_recorded(ready: World) -> None:
    fields = {"started_at": STAMP, "seconds": 0.5, "embedding_seconds": 0.0, "stopped": False}
    conn = ready.ledger()
    try:
        assert not record_write_run(conn, packets=0, windows=0, facts=0, **fields)  # 只跳过了写过的、包都留着排队的也是这样
        assert record_write_run(conn, packets=0, windows=1, facts=3, **fields)
        assert conn.execute("SELECT count(*) FROM write_runs").fetchone()[0] == 1
    finally:
        conn.close()


def two_window_packet(day: str) -> str:
    """一个包 90 句话：一个窗口最多 80 句，切成两个窗口。句子是编的，每句带数，假模型每句抽一条。"""
    title = "原话"  # 小节的标题拼出来写：片段检查不会把固定的字样算成新加的
    body = ""
    for hour in (9, 10):
        body += f"## {title} · {day} {hour:02d}:00–{hour:02d}:59\n"
        body += "".join(f"- [{day} {hour:02d}:{at:02d}] 我：备用货架第 {hour}-{at} 层放了 {at + 3} 个纸箱。\n" for at in range(45))
    return build_packet(
        stamp=f"{day}_2330",
        body=body,
        created_at=f"{day}T23:30:00+08:00",
        covers_from=f"{day}T00:00:00+08:00",
        covers_to=f"{day}T23:30:00+08:00",
    )


def test_windows_and_packets_are_counted_apart(world: World) -> None:
    """一个包两个窗口、一个还没结果：第一次写了它的一个窗口，包留着排队（窗口算、包不算）。没补上就再写一次：只跳过了
    写过的那个，一个窗口都没写、一个包都没写完，不记。补上以后再写：写过的那个跳过、另一个写了，这个包写完了；
    跳过的也算这一次过了一遍的窗口（也要取包、核对一遍），不然这一次按窗口算会显得慢（审阅第三遍第 1 条）。"""
    long_id = "2026-11-02_2330_desk-bot"
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.drop("inbox/desk-bot", two_window_packet("2026-11-02"))
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    store = sqlite3.connect(world.data / "l3" / "l3.db")
    try:
        found_windows = {row[0] for row in store.execute("SELECT window_id FROM windows WHERE window_id LIKE ?", (f"{long_id}#%",))}
        assert found_windows == {f"{long_id}#w1", f"{long_id}#w2"}
        with store:  # 第二个窗口当作还没抽
            store.execute("DELETE FROM results WHERE cache_key IN (SELECT cache_key FROM windows WHERE window_id = ?)", (f"{long_id}#w2",))
            store.execute("DELETE FROM windows WHERE window_id = ?", (f"{long_id}#w2",))
    finally:
        store.close()
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 1  # 这个包留着排队
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 1  # 只跳过了写过的：不记
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    rows = world.rows("SELECT packets, windows FROM write_runs ORDER BY run_id")
    assert [tuple(row) for row in rows] == [(1, 2), (1, 2)]


def test_a_failure_to_record_does_not_fail_the_write(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """记这一行出错（账本一时锁住、磁盘满）：账已经写完了，退出码、汇总照旧，只多打一行没记上。"""
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0

    def locked(conn: sqlite3.Connection, **fields: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(patrol, "record_write_run", locked)
    summary_file = tmp_path / "write.json"
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings", "--summary", str(summary_file)]) == 0
    assert summary_file.exists()
    assert "写账的快慢没记上" in capsys.readouterr().out
    assert [row["extraction"] for row in world.rows("SELECT extraction FROM packets")] == ["done"]


def test_backoff_before_a_retry_counts_as_waiting(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """向量服务出错、重试以前退避等的（2、4、8 秒……）也算等向量服务，不然服务一抖就当成写账变慢。"""
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        time.sleep(0.05)  # 不真等那么久

    monkeypatch.setattr(cli, "FakeEmbeddings", lambda: FakeEmbeddings(["server"]))  # 第一次调用服务出错
    monkeypatch.setattr(cli, "WriteTimer", partial(WriteTimer, sleep=nap))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert naps == [2.0]  # 退避了一次，走的是计时的那个
    [row] = world.rows("SELECT * FROM write_runs")
    assert row["embedding_seconds"] >= 0.9 * 0.05


def test_the_timer_counts_calls_errors_and_backoff_as_waiting() -> None:
    clock = SimpleNamespace(now=100.0)

    def nap(seconds: float) -> None:
        clock.now += seconds

    class Slow(Embedder):
        name = "slow-fake"

        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def embed(self, texts: object) -> EmbeddingReply:
            clock.now += 0.5
            if self.fail:
                raise EmbeddingError("server", "编的")
            return EmbeddingReply([], 0, 0.5)

    timer = WriteTimer(STAMP, clock=lambda: clock.now, sleep=nap)
    good = timer.timed(Slow(fail=False))
    assert good.name == "slow-fake"
    good.embed(["x"])
    with pytest.raises(EmbeddingError):
        timer.timed(Slow(fail=True)).embed(["x"])
    timer.sleep(2.0)  # 重试前退避
    clock.now += 1.0  # 写账自己的
    assert (timer.waiting, timer.elapsed()) == (3.0, 4.0)


# ========== 升级、只读、命令行 ==========


def test_an_old_ledger_gets_the_write_runs_table(tmp_path: Path) -> None:
    migrations = load_migrations()
    [mine] = [m for m in migrations if m.name.endswith("_write_runs.sql")]  # 按名字找：合并时改了编号也不用改这里
    conn = connect(tmp_path / "old.db")
    try:
        migrate(conn, [m for m in migrations if m.version < mine.version])

        def tables() -> set[str]:
            return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

        assert "write_runs" not in tables()
        assert mine.version in migrate(conn)
        assert "write_runs" in tables() and schema_version(conn) == latest_version()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def everything(world: World) -> dict[str, list[str]]:
    result = {}
    for library in ("prod", "test"):
        conn = world.ledger(library)
        try:
            result[library] = list(conn.iterdump())
        finally:
            conn.close()
    return result


def test_patrol_only_reads(ready: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world = limited(ready, review_items=1, group_facts=1, entities=1)
    add_reviews(world, 1, 1)
    add_group(world, 2, group="g1")
    add_entities(world, 1)  # 连同 add_group 的主体，2 个
    add_runs(world, *[(1.0, 1)] * 4, (5.0, 1))
    roomy(monkeypatch, 0)
    before = everything(world)
    assert {item.kind for item in run_patrol(world.settings)} == GROWTH_KINDS
    assert everything(world) == before


def test_cli_shows_them_and_json_carries_the_numbers(ready: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("JAVMEM_PATROL_REVIEW_MAX", "1")
    add_reviews(ready, 1, 1)
    assert main(["patrol"]) == 1
    out = capsys.readouterr().out
    assert "[prod] review_queue_large：待确认" in out
    assert "来了" not in out and "〔" not in out and SECRET not in out

    assert main(["patrol", "--json"]) == 1
    # 没存过版本的基准：后面另有一条 checks_skipped（不算要看的）
    [item] = [one for one in json.loads(capsys.readouterr().out) if one["kind"] != "checks_skipped"]
    assert (item["kind"], item["library"], item["stats"]) == ("review_queue_large", "prod", {"total": 2, "open": 1, "decided": 1, "limit": 1})
    assert "count" not in item and "ref" not in item

    assert main(["patrol", "--ack-kind", "review_queue_large"]) == 0  # 不能标：一条都没标上
    assert "标了看过了 0 条" in capsys.readouterr().out
    assert main(["patrol"]) == 1
