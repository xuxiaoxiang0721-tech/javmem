"""包重发成 private 的新版本以后，旧版本留在 L3 结果库里的窗口文字、L3 结果、复核判定（L4 模型复核那一段第 4 节
第 6 条，11-10 定了放进附件正文那一段修）。先写测试、看着红了再修。样例都是编的，模型用假的。"""

from __future__ import annotations

import json

from helpers import CHAT_ID, World, build_packet

from javmem.cli import main
from javmem.pipeline.store import ResultStore

BODY = "## 原话 · 10:30–10:31\n- [10:30] 我：越南那批订单 11-15 出货。\n- [10:31] desk-bot：好的，记下了 2 件事。\n"
NORMAL = build_packet(body=BODY)
PRIVATE = build_packet(body=BODY, privacy="private", revision=2)


def store_rows(world: World) -> tuple[list, list, list]:
    with ResultStore(world.data / "l3") as store:
        windows = list(store.windows())
        results = store._conn.execute("SELECT cache_key, output FROM results").fetchall()
        rechecks = store._conn.execute("SELECT recheck_key FROM rechecks").fetchall()
    return windows, results, rechecks


def revisions_in(windows: list) -> set[tuple[str, int]]:
    return {(str(packet), int(revision)) for row in windows for packet, revision in json.loads(row["packets"])}


def test_old_revision_of_a_packet_resent_as_private_is_dropped_from_the_store(world: World) -> None:
    world.drop("inbox/desk-bot", NORMAL)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake", "--recheck"]) == 0
    windows, results, rechecks = store_rows(world)
    assert (CHAT_ID, 1) in revisions_in(windows) and results and rechecks

    world.drop("inbox/desk-bot", PRIVATE)
    world.intake()
    assert world.packet_row(CHAT_ID, 2)["extraction"] == "skipped"  # private 的不抽，不排队
    assert main(["extract", "--library", "prod", "--fake"]) == 0  # 拿到结果库的锁以后清掉旧版本留下的
    windows, results, rechecks = store_rows(world)
    assert (CHAT_ID, 1) not in revisions_in(windows)
    assert results == [] and rechecks == []  # 只被这些窗口用着的 L3 结果、复核判定一起删
    with ResultStore(world.data / "l3") as store:
        text = "\n".join(row["input_text"] or "" for row in store.windows())
    assert "11-15" not in text


def test_a_normal_new_revision_keeps_the_store_as_it_is(world: World) -> None:
    """只管 private 的：普通的新版本照旧（文字一样的窗口直接用存下的结果）。"""
    world.drop("inbox/desk-bot", NORMAL)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    before, results, _ = store_rows(world)
    world.drop("inbox/desk-bot", build_packet(body=BODY, revision=2))
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    after, kept, _ = store_rows(world)
    assert [row["cache_key"] for row in kept] == [row["cache_key"] for row in results]  # 存下的结果照用，没删
    assert {row["cache_key"] for row in after} == {row["cache_key"] for row in before}
