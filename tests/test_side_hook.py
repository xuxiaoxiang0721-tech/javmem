"""第 6 步第二轮的审阅（审 9f48c83）查出来的：撤回了的名字留在第二证人的待确认里、撤回被支线卡住、
compare --queue 信得过旧的导出、彻底删除的提示说错了、待确认里主线写的是说法编号、主线读账本是两次快照，
加上缺的测试（真的体检库那条路、支线正在跑、purge 命令、--json、别的包的待确认留着、不给 --main 的 compare）。
体检库是假的（只有一条要 neo4j 驱动的连一个不回话的本机端口），样例都是编的。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from helpers import World
from test_side import PACKET_ID, FakeGraph, _packet
from test_side_compare import _side
from test_side_witness import _group_with_witness_item

from javmem.side.store import SideStore

WITHDRAW_PACKET = "UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'"
PASSWORD = "made-up-test-pw-1"


def _witness_row(world: World, *, packet: str = PACKET_ID, quotes: tuple[str, ...] = (), key: str = "k1",
                 kind: str = "witness_mismatch") -> None:
    world.execute("INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES (?, ?, ?, '2026-11-09')",
                  kind, json.dumps({"key": key, "packet": packet, "revision": 1}), json.dumps(list(quotes)))


def _witness_keys(world: World) -> list[str]:
    return sorted(json.loads(row["claims"])["key"]
                  for row in world.rows("SELECT claims FROM review_queue WHERE kind = 'witness_mismatch'"))


def _sentence_ids(world: World) -> list[str]:
    return [row["sentence_id"] for row in world.rows("SELECT sentence_id FROM raw_sentences ORDER BY id")]


def _with_revision(side: object, revision: int = 1) -> object:
    import dataclasses

    return dataclasses.replace(side, revisions={PACKET_ID: revision})  # type: ignore[type-var]


class _OnlineGraph:
    """换掉 javmem.side.graph.SideGraph（“真的体检库”那条路）：连得上，删组都成；fail_on 那一步出错。"""

    fail_on = ""

    def __init__(self, settings: object, store: SideStore, **_: object) -> None:
        self.graph = FakeGraph()
        self.graph.groups[f"{PACKET_ID}_r1"] = [{"uuid": "x", "name": "x"}]

    async def open(self) -> None:
        if self.fail_on == "open":
            raise OSError("connection refused")

    async def close(self) -> None:
        if self.fail_on == "close":
            raise RuntimeError("close failed")

    async def delete_group(self, group_id: str) -> tuple[int, int]:
        return await self.graph.delete_group(group_id)

    async def group_ids(self) -> set[str]:
        if self.fail_on == "group_ids":
            raise OSError("connection reset")
        return await self.graph.group_ids()


def _online(monkeypatch: pytest.MonkeyPatch, graph_class: type = _OnlineGraph, fail_on: str = "") -> None:
    import javmem.side.graph as graph_module

    monkeypatch.setenv("JAVMEM_SIDE_NEO4J_PASSWORD", PASSWORD)
    monkeypatch.setattr(graph_class, "fail_on", fail_on)
    monkeypatch.setattr(graph_module, "SideGraph", graph_class)


# ---------- 第 1 条：第二证人的待确认按账本的现状删 ----------


def test_drop_stale_goes_by_the_ledger_not_by_what_was_swept(world: World) -> None:
    from javmem.side.witness import drop_stale

    _packet(world)
    first, second = _sentence_ids(world)[:2]
    _witness_row(world, key="ok", quotes=(first,))
    _witness_row(world, key="sentence-withdrawn", quotes=(second,))
    _witness_row(world, key="sentence-missing", quotes=(f"{PACKET_ID}#9.9.9",))
    _witness_row(world, key="packet-missing", packet="2026-11-07_2330_desk-bot")
    _witness_row(world, key="not-mine", kind="other", packet="2026-11-07_2330_desk-bot")
    world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-09T00:00:00+08:00' WHERE sentence_id = ?", second)
    ledger = world.settings.library("prod").db_path
    assert drop_stale(ledger) == 3
    assert _witness_keys(world) == ["ok"]
    world.execute(WITHDRAW_PACKET)
    assert drop_stale(ledger) == 1
    assert _witness_keys(world) == []
    assert len(world.rows("SELECT * FROM review_queue WHERE kind = 'other'")) == 1  # 别的种类不动


def test_withdraw_while_the_side_branch_runs_still_drops_its_witness_items(world: World) -> None:
    from javmem.side.hook import after_withdraw
    from javmem.side.store import side_dir, side_lock

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    lines: list[str] = []
    with side_lock(side_dir(world.data, "prod")):  # 一个 javmem side run 正在跑
        after_withdraw(world.settings, "prod", out=lines.append, graph=FakeGraph())
    assert any("支线正在跑" in line for line in lines)
    assert _witness_keys(world) == []


@pytest.mark.parametrize("fail_on", ["group_ids", "close"])
def test_a_failure_after_the_group_was_deleted_still_reports_and_drops(world: World, monkeypatch: pytest.MonkeyPatch,
                                                                      fail_on: str) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    _online(monkeypatch, fail_on=fail_on)
    lines: list[str] = []
    after_withdraw(world.settings, "prod", out=lines.append)
    with SideStore.open(world.data, "prod") as store:
        assert store.groups() == [] and store.pending_clears() == []
    assert _witness_keys(world) == []
    assert any(f"删了 {PACKET_ID}_r1" in line for line in lines)


@pytest.mark.parametrize("online", [True, False])
def test_side_sweep_commands_drop_stale_witness_items(world: World, online: bool) -> None:
    from javmem.side import cli as side_cli

    _group_with_witness_item(world)
    _witness_row(world, key="k2")
    world.execute(WITHDRAW_PACKET)
    lines: list[str] = []
    with SideStore.open(world.data, "prod") as store:
        if online:  # javmem side sweep、run、export 都走这一步
            asyncio.run(side_cli._sweep(FakeGraph(), world.settings, "prod", store, lines.append))
        else:  # 体检库连不上
            asyncio.run(side_cli._sweep_offline(world.settings, "prod", store, lines.append))
    assert _witness_keys(world) == []
    assert any("第二证人的待确认" in line for line in lines)


def test_other_packets_keep_their_witness_items(world: World) -> None:
    from test_write import day_packet, deliver

    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    deliver(world, day_packet("2026-11-02", "我：X3 也在海港银行。"))
    _witness_row(world, key="other-packet", packet="2026-11-02_2330_desk-bot")
    world.execute(WITHDRAW_PACKET + " WHERE packet_id = ?", PACKET_ID)
    after_withdraw(world.settings, "prod", out=lambda _: None, graph=FakeGraph())
    assert _witness_keys(world) == ["other-packet"]


# ---------- 第 2 条：撤回不让支线卡住 ----------


def test_a_locked_ledger_does_not_hold_up_the_withdraw(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    import javmem.side.witness as witness
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    monkeypatch.setattr(witness, "LEDGER_TIMEOUT", 0.2, raising=False)
    other = sqlite3.connect(world.settings.library("prod").db_path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # 别的程序拿着账本的写锁（不是 javmem 的）
    graph = FakeGraph()
    graph.groups[f"{PACKET_ID}_r1"] = [{"uuid": "x", "name": "x"}]
    lines: list[str] = []
    started = time.monotonic()
    try:
        after_withdraw(world.settings, "prod", out=lines.append, graph=graph)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert time.monotonic() - started < 10
    assert graph.groups == {}  # 体检库照样扫了
    assert any("待确认没清" in line for line in lines)
    assert _witness_keys(world) == ["k1"]  # 没清成：之后 javmem side sweep、下一次撤回都会清


def test_the_hook_takes_the_ledger_write_locks_like_review(world: World) -> None:
    from javmem.pipeline.write import WriteLock
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    lines: list[str] = []
    with WriteLock(world.data):  # 写账在跑
        after_withdraw(world.settings, "prod", out=lines.append, graph=FakeGraph())
    assert _witness_keys(world) == ["k1"]
    assert any("待确认没清" in line for line in lines)


def test_a_silent_checkup_db_does_not_hold_up_the_withdraw(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("neo4j")
    import socket
    import threading
    import time

    import javmem.side.hook as hook

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    monkeypatch.setattr(hook, "QUICK_TIMEOUT", 1.0, raising=False)
    monkeypatch.setattr(hook, "OPEN_DEADLINE", 3.0, raising=False)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []

    def accept_forever() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)  # 连得上，不回话

    threading.Thread(target=accept_forever, daemon=True).start()
    monkeypatch.setenv("JAVMEM_SIDE_NEO4J_URI", f"bolt://127.0.0.1:{server.getsockname()[1]}")
    monkeypatch.setenv("JAVMEM_SIDE_NEO4J_PASSWORD", PASSWORD)
    lines: list[str] = []
    started = time.monotonic()
    try:
        hook.after_withdraw(world.settings, "prod", out=lines.append)
    finally:
        server.close()
        for one in held:
            one.close()
    assert time.monotonic() - started < 10
    assert lines and "体检库" in lines[0]  # 等之前先说一声
    with SideStore.open(world.data, "prod") as store:
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", "包撤回了")]


# ---------- 第 3 条：compare --queue 不信旧的导出 ----------


def test_queue_skips_packets_that_are_not_current_any_more(world: World) -> None:
    from javmem.side.compare import compare
    from javmem.side.witness import queue_contradictions

    _packet(world)
    world.execute(WITHDRAW_PACKET)
    main = _side("main", [("m1", "X1 FCN", "托管于", "东岸证券", "生效")])
    side = _with_revision(_side("side", [("s1", "X1 FCN", "托管于", "海港银行", "生效")]))
    result = compare(main, side, None)  # type: ignore[arg-type]
    queued = queue_contradictions(world.settings.library("prod").db_path, result, main, side)  # type: ignore[arg-type]
    assert tuple(queued) == (0, 1)  # 进了 0 条；1 条的包不是现行的了
    assert _witness_keys(world) == []


@pytest.mark.parametrize("change, expected", [("", (1, 0)), ("revision", (0, 1)), ("no-revision", (0, 1)),
                                              ("sentence", (0, 1))])
def test_queue_checks_the_export_against_the_ledger(world: World, change: str, expected: tuple[int, int]) -> None:
    from javmem.side.compare import compare
    from javmem.side.witness import queue_contradictions

    _packet(world)
    main = _side("main", [("m1", "X1 FCN", "托管于", "东岸证券", "生效")])
    side = _side("side", [("s1", "X1 FCN", "托管于", "海港银行", "生效")])
    if change != "no-revision":  # 旧的导出没写是哪一版：认不出是不是现行的那一版，不进
        side = _with_revision(side, 2 if change == "revision" else 1)
    if change == "sentence":
        world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-09T00:00:00+08:00' WHERE sentence_id = ?",
                      f"{PACKET_ID}#1.1.1")
    result = compare(main, side, None)  # type: ignore[arg-type]
    queued = queue_contradictions(world.settings.library("prod").db_path, result, main, side)  # type: ignore[arg-type]
    assert tuple(queued) == expected
    assert len(_witness_keys(world)) == expected[0]


# ---------- 第 4 条：彻底删除的提示 ----------


def test_purge_hint_only_for_revisions_that_were_in_the_checkup_db(world: World) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)  # 第 1 版有一组，包是现行的
    graph = FakeGraph()
    graph.groups[f"{PACKET_ID}_r1"] = [{"uuid": "x", "name": "x"}]
    lines: list[str] = []
    after_withdraw(world.settings, "prod", purged={PACKET_ID: [2]}, out=lines.append, graph=graph)  # 删的是第 2 版
    assert graph.groups  # 现行那一版的组不动
    assert not any("整个重建" in line for line in lines)


def test_purge_hint_says_pending_when_the_checkup_db_is_down(world: World) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    lines: list[str] = []
    after_withdraw(world.settings, "prod", purged={PACKET_ID: [1]}, out=lines.append)  # 没配密码：连不上
    hint = [line for line in lines if "整个重建" in line]
    assert hint and "待清" in hint[0] and "已经删了" not in hint[0]


def test_purge_hint_while_the_side_branch_runs(world: World) -> None:
    from javmem.side.hook import after_withdraw
    from javmem.side.store import side_dir, side_lock

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    lines: list[str] = []
    with side_lock(side_dir(world.data, "prod")):
        after_withdraw(world.settings, "prod", purged={PACKET_ID: [1]}, out=lines.append, graph=FakeGraph())
    assert any("整个重建" in line for line in lines)


def test_purge_after_withdraw_says_the_group_was_deleted(world: World) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    graph = FakeGraph()
    graph.groups[f"{PACKET_ID}_r1"] = [{"uuid": "x", "name": "x"}]
    after_withdraw(world.settings, "prod", out=lambda _: None, graph=graph)  # 撤回时整组删了
    lines: list[str] = []
    after_withdraw(world.settings, "prod", purged={PACKET_ID: [1]}, out=lines.append, graph=graph)
    hint = [line for line in lines if "整个重建" in line]
    assert hint and "删了" in hint[0] and "待清" not in hint[0]


def test_cli_purge_runs_the_side_sweep(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    from javmem.cli import main

    _group_with_witness_item(world)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_ID]) == 0
    capsys.readouterr()
    assert main(["purge", "--library", "prod", "--packet", PACKET_ID, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "整个重建" in out and "待清" in out  # 没配体检库：这一组还在待清
    assert _witness_keys(world) == []


def test_cli_withdraw_json_keeps_stdout_clean(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    from javmem.cli import main

    _group_with_witness_item(world)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_ID, "--json"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "待清" in captured.err and "待清" not in captured.out


# ---------- 第 5、6 条：主线那一边写关系编号；读账本拿着写账、收件的锁 ----------


def test_queue_writes_relation_ids_for_the_main_line(world: World) -> None:
    from javmem.side.compare import compare
    from javmem.side.witness import queue_contradictions

    _packet(world)
    main = _side("main", [("R1#0", "X1 FCN", "托管于", "东岸证券", "生效")])
    side = _with_revision(_side("side", [("s1", "X1 FCN", "托管于", "海港银行", "生效")]))
    result = compare(main, side, None)  # type: ignore[arg-type]
    queue_contradictions(world.settings.library("prod").db_path, result, main, side,  # type: ignore[arg-type]
                         relation_of={"R1#0": "R1"})
    (row,) = world.rows("SELECT claims FROM review_queue WHERE kind = 'witness_mismatch'")
    claims = json.loads(row["claims"])
    assert claims["main"]["relations"] == ["R1"] and claims["main"]["claims"] == ["R1#0"]


def _ledger_world(world: World, tmp_path: Path) -> tuple[str, Path]:
    """J4.4 起的账本：真的写了一条关系（X1 票据托管于海港银行）；支线说在东岸证券。返回包号、支线导出的文件。"""
    from test_relations_write import bank_extract
    from test_write import day_packet, deliver, write

    from javmem.scoring.common import Entity, Relation, Side, dump_side

    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    packet = "2026-11-01_2330_desk-bot"
    (sentence,) = [row["sentence_id"] for row in
                   world.rows("SELECT sentence_id FROM raw_sentences WHERE packet_id = ?", packet)]
    side = Side("side", frozenset({packet}),
                {"s:note": Entity("s:note", "X1 票据", "订单或合同"), "s:bank": Entity("s:bank", "东岸证券", "公司或机构")},
                {}, {"s1": Relation("s1", "s:note", "托管于", "s:bank", {}, state="生效", evidence=(sentence,))},
                revisions={packet: 1})
    path = tmp_path / "side.json"
    dump_side(side, path)
    return packet, path


def _compare_args(path: Path, tmp_path: Path, *, queue: bool) -> object:
    import argparse

    return argparse.Namespace(side_command="compare", library="prod", main=None, side=str(path), packet=None,
                              out=str(tmp_path / "compare.json"), queue=queue)


def test_compare_queue_reads_the_main_line_from_the_ledger(world: World, tmp_path: Path) -> None:
    from javmem.side.cli import EXIT_OK, run

    _, path = _ledger_world(world, tmp_path)
    lines: list[str] = []
    assert run(_compare_args(path, tmp_path, queue=True), world.settings, out=lines.append) == EXIT_OK  # type: ignore[arg-type]
    (row,) = world.rows("SELECT claims FROM review_queue WHERE kind = 'witness_mismatch'")
    claims = json.loads(row["claims"])
    assert claims["kind"] == "另一头不一样"
    relation_ids = {one[0] for one in world.rows("SELECT relation_id FROM relations")}
    assert claims["main"]["relations"] and set(claims["main"]["relations"]) <= relation_ids
    assert claims["main"]["claims"] and all(one.endswith("#0") for one in claims["main"]["claims"])


def test_compare_reads_the_main_line_under_the_ledger_locks(world: World, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.side.mainline as mainline
    from javmem.pipeline.write import WriteBusy, WriteLock
    from javmem.side.cli import EXIT_BUSY, EXIT_OK, run

    _, path = _ledger_world(world, tmp_path)
    real = mainline.side_from_ledger
    seen: list[str] = []

    def reading(*args: object, **kwargs: object) -> object:
        try:
            WriteLock(world.data).close()
            seen.append("free")
        except WriteBusy:
            seen.append("held")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mainline, "side_from_ledger", reading)
    assert run(_compare_args(path, tmp_path, queue=True), world.settings, out=lambda _: None) == EXIT_OK  # type: ignore[arg-type]
    assert seen == ["held"]  # 要写待确认的：读账本的两步都在写账的锁里，中间没人写得进来，一直拿到写完
    lines: list[str] = []
    with WriteLock(world.data):
        assert run(_compare_args(path, tmp_path, queue=True), world.settings, out=lines.append) == EXIT_BUSY  # type: ignore[arg-type]
    assert any("在跑" in line for line in lines)


def test_compare_queue_needs_a_ledger_with_relations(world: World, tmp_path: Path) -> None:
    from javmem.scoring.common import dump_side
    from javmem.side.cli import EXIT_INPUT, EXIT_OK, run

    _packet(world)  # 没写过关系（J4.4 以前那种账本：关系只在结果库里）
    path = tmp_path / "side.json"
    dump_side(_with_revision(_side("side", [("s1", "X1 FCN", "托管于", "海港银行", "生效")])), path)  # type: ignore[arg-type]
    lines: list[str] = []
    assert run(_compare_args(path, tmp_path, queue=True), world.settings, out=lines.append) == EXIT_INPUT  # type: ignore[arg-type]
    assert any("关系表" in line for line in lines)
    assert run(_compare_args(path, tmp_path, queue=False), world.settings, out=lambda _: None) == EXIT_OK  # type: ignore[arg-type]


# ---------- 另外：hook 压驱动的日志、记闸拦下的、Ctrl+C 不冒出来 ----------


class _Noisy(_OnlineGraph):
    async def open(self) -> None:
        import logging
        import socket

        logging.getLogger("neo4j").warning("驱动的提示")
        try:
            socket.create_connection(("127.0.0.1", 9), timeout=1)  # 闸只放体检库的端口
        except OSError:
            pass


class _Interrupted(_OnlineGraph):
    async def open(self) -> None:
        raise KeyboardInterrupt


def test_the_hook_quiets_the_driver_and_records_blocked_connections(world: World, monkeypatch: pytest.MonkeyPatch,
                                                                   caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    _online(monkeypatch, _Noisy)
    lines: list[str] = []
    with caplog.at_level(logging.WARNING):
        after_withdraw(world.settings, "prod", out=lines.append)
    assert not [record for record in caplog.records if record.name.startswith("neo4j")]
    with SideStore.open(world.data, "prod") as store:
        assert [row["port"] for row in store.blocked()] == [9]
    assert any("闸拦下" in line for line in lines)


def test_ctrl_c_during_the_hook_does_not_burst_out(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    _online(monkeypatch, _Interrupted)
    lines: list[str] = []
    try:
        after_withdraw(world.settings, "prod", out=lines.append)
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C 冒出来了")
    assert any("Ctrl+C" in line for line in lines)


# ---------- 第二遍审阅（审 24e0eb0）查出来的 ----------

MADE_UP_NAME = "Qorvex-Made-Up-4471"


def _row(world: World, *, key: str, quotes: list[str], revision: int | None = 1, created: str = "2026-11-09T12:00:00+08:00",
         main: list[str] | None = None) -> None:
    """照现在的写法造一条第二证人的待确认：claims 里有版本、两头的名字（编的）。"""
    claims: dict[str, object] = {"key": key, "witness": "side", "kind": "另一头不一样", "packet": PACKET_ID,
                                 "relation": "托管于", "main": {"relations": main or [], "claims": []},
                                 "side": {"relations": ["s1"], "subject": MADE_UP_NAME, "object": "made-up"}}
    if revision is not None:
        claims["revision"] = revision
    world.execute("INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('witness_mismatch', ?, ?, ?)",
                  json.dumps(claims, ensure_ascii=False), json.dumps(quotes), created)


def _second_revision(world: World) -> None:
    """同一个包的第 2 版，排版没变（只改了一个字）：句子编号按位置编，和第 1 版的一样。"""
    from helpers import build_packet
    from test_side import BODY

    world.drop("inbox/desk-bot", build_packet(body=BODY.replace("X2", "X3"), revision=2))
    world.intake()
    assert world.packet_row(PACKET_ID, 2)["state"] == "current"


def _ran_group(world: World) -> None:
    from test_side import _ran

    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, FakeGraph())


def _file_bytes(path: Path) -> bytes:
    return b"".join(one.read_bytes() for one in path.parent.glob(path.name + "*"))


# 严重 1：换了版本、包里有句子后来撤回了的，都不算数了（不靠哪一遍按包删）


def test_rows_of_a_replaced_revision_or_without_a_revision_are_stale(world: World) -> None:
    from javmem.side.witness import drop_stale

    _packet(world)
    first = _sentence_ids(world)[0]
    _row(world, key="rev1", quotes=[first])
    _row(world, key="no-revision", quotes=[first], revision=None)  # 以前的写法没记是哪一版：认不出，算不算数了
    ledger = world.settings.library("prod").db_path
    assert drop_stale(ledger) == 1 and _witness_keys(world) == ["rev1"]
    _second_revision(world)
    assert first in _sentence_ids(world)  # 同一个句子编号现在是第 2 版的
    assert drop_stale(ledger) == 1 and _witness_keys(world) == []


def test_rows_of_a_packet_with_a_sentence_withdrawn_after_them_are_stale(world: World) -> None:
    from javmem.side.witness import drop_stale

    _packet(world)
    ids = _sentence_ids(world)
    _row(world, key="before", quotes=[ids[0]], created="2026-11-09T12:00:00+08:00")
    _row(world, key="after", quotes=[ids[0]], created="2026-11-11T12:00:00+08:00")
    # 撤回的是包里另一句（不在 quotes 里）：体检库里那一组作废了，它之前比出来的待确认也不算数了
    world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-10T12:00:00+08:00' WHERE sentence_id = ?", ids[2])
    assert drop_stale(world.settings.library("prod").db_path) == 1
    assert _witness_keys(world) == ["after"]  # 撤回以后才比出来的（支线重跑过）留着


def test_rows_survive_a_renumbering_of_the_main_relation(world: World, tmp_path: Path) -> None:
    """主线的关系照常会换编号（分段重算：最早那一处撤回了、补进来一个更早的包），write.py 改别的几种待确认，不改这一种
    里写的关系编号：编号对不上不算不算数了（主人定过的也不能因此删掉、再问一遍）。撤回了的关系，证据句撤回了，按句子就清了。"""
    from javmem.side.witness import drop_stale

    _ledger_world(world, tmp_path)
    packet = "2026-11-01_2330_desk-bot"
    (sentence,) = [row[0] for row in world.rows("SELECT sentence_id FROM raw_sentences WHERE packet_id = ?", packet)]
    world.execute("INSERT INTO review_queue (kind, claims, quotes, created_at, decision) VALUES ('witness_mismatch', ?, ?, ?, ?)",
                  json.dumps({"key": "decided", "packet": packet, "revision": 1, "main": {"relations": ["R0000000000000000"]}}),
                  json.dumps([sentence]), "2026-11-09T12:00:00+08:00", "keep_both")
    assert drop_stale(world.settings.library("prod").db_path) == 0
    assert _witness_keys(world) == ["decided"]
    world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-10T12:00:00+08:00' WHERE sentence_id = ?", sentence)
    assert drop_stale(world.settings.library("prod").db_path) == 1  # 证据句撤回了：照样清


def test_a_busy_first_clean_is_caught_up_by_the_next_side_sweep(world: World) -> None:
    from javmem.pipeline.write import WriteLock
    from javmem.side import cli as side_cli
    from javmem.side.hook import after_withdraw

    _packet(world)
    _ran_group(world)
    ids = _sentence_ids(world)
    _row(world, key="k", quotes=[ids[0]], created="2026-11-09T12:00:00+08:00")
    world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-10T12:00:00+08:00' WHERE sentence_id = ?", ids[2])
    graph = FakeGraph()
    graph.groups[f"{PACKET_ID}_r1"] = [{"uuid": "x", "name": "x"}]
    with WriteLock(world.data):  # 写账在跑：这一回清不了
        after_withdraw(world.settings, "prod", out=lambda _: None, graph=graph)
    assert graph.groups == {} and _witness_keys(world) == ["k"]
    with SideStore.open(world.data, "prod") as store:  # “之后跑一遍 javmem side sweep”：组已经没了，照样清得掉
        asyncio.run(side_cli._sweep(FakeGraph(), world.settings, "prod", store, lambda _: None))
    assert _witness_keys(world) == []


# 严重 2：彻底删除以后，删掉的东西不留在文件里


def test_purge_leaves_no_window_text_in_the_side_db_file(world: World) -> None:
    from javmem.cli import main
    from javmem.side.store import side_dir

    _packet(world)
    _ran_group(world)
    with SideStore.open(world.data, "prod") as store:
        needle = max((row[0] for row in store.conn.execute("SELECT text FROM lines WHERE text <> ''")), key=len)[:14]
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_ID]) == 0
    assert main(["purge", "--library", "prod", "--packet", PACKET_ID, "--yes"]) == 0
    assert needle.encode("utf-8") not in _file_bytes(side_dir(world.data, "prod") / "side.db")
    assert needle.encode("utf-8") not in _file_bytes(world.settings.library("prod").db_path)


def test_purge_leaves_no_witness_names_in_the_ledger_file(world: World) -> None:
    from javmem.cli import main

    _packet(world)
    _ran_group(world)
    _row(world, key="old-revision", quotes=[_sentence_ids(world)[0]])
    _second_revision(world)
    assert main(["purge", "--library", "prod", "--packet", PACKET_ID, "--revision", "1", "--yes"]) == 0
    assert _witness_keys(world) == []
    assert MADE_UP_NAME.encode("utf-8") not in _file_bytes(world.settings.library("prod").db_path)


# 一般的：没有 side.db 也清；作废了的组的旧导出不进；连上以后删组也有时限


def test_withdraw_cleans_witness_items_even_without_a_side_db(world: World, tmp_path: Path) -> None:
    from javmem.cli import main
    from javmem.side.cli import EXIT_OK, run
    from javmem.side.store import side_dir

    packet, path = _ledger_world(world, tmp_path)
    assert run(_compare_args(path, tmp_path, queue=True), world.settings, out=lambda _: None) == EXIT_OK  # type: ignore[arg-type]
    assert len(_witness_keys(world)) == 1
    assert not (side_dir(world.data, "prod") / "side.db").exists()  # 只比过、没跑过支线
    assert main(["withdraw", "--library", "prod", "--packet", packet]) == 0
    assert _witness_keys(world) == []


@pytest.mark.parametrize("started, expected", [(None, (0, 1)), ("2026-11-10T00:00:00+08:00", (0, 1)),
                                               ("2026-11-12T00:00:00+08:00", (1, 0))])
def test_queue_rejects_an_export_made_before_a_sentence_withdrawal(world: World, started: str | None,
                                                                   expected: tuple[int, int]) -> None:
    import dataclasses

    from javmem.side.compare import compare
    from javmem.side.witness import queue_contradictions

    _packet(world)
    world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-11T12:00:00+08:00' WHERE sentence_id = ?",
                  _sentence_ids(world)[2])  # 包里另一句撤回了：那时候体检库里的那一组作废了
    main_line = _side("main", [("m1", "X1 FCN", "托管于", "东岸证券", "生效")])
    side = _with_revision(_side("side", [("s1", "X1 FCN", "托管于", "海港银行", "生效")]))
    if started is not None:  # 支线导出写着这一组是哪时候开始抽的
        side = dataclasses.replace(side, described={"groups_started": {PACKET_ID: started}})  # type: ignore[type-var]
    result = compare(main_line, side, None)  # type: ignore[arg-type]
    queued = queue_contradictions(world.settings.library("prod").db_path, result, main_line, side)  # type: ignore[arg-type]
    assert tuple(queued) == expected


class _SlowGraph(_OnlineGraph):
    async def delete_group(self, group_id: str) -> tuple[int, int]:
        await asyncio.sleep(2.0)  # 连上了、回了话，删的时候卡住（等锁、没响应）
        return await self.graph.delete_group(group_id)


def test_the_whole_online_sweep_has_a_deadline(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    import javmem.side.hook as hook

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    _online(monkeypatch, _SlowGraph)
    monkeypatch.setattr(hook, "SWEEP_DEADLINE", 0.3, raising=False)
    lines: list[str] = []
    started = time.monotonic()
    hook.after_withdraw(world.settings, "prod", out=lines.append)
    assert time.monotonic() - started < 1.5
    with SideStore.open(world.data, "prod") as store:  # 过了时限按连不上办：原文先删、记待清
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", "包撤回了")]
        assert store.conn.execute("SELECT count(*) FROM lines").fetchone()[0] == 0


# 小的：先清再查重；别的错不让撤回报失败；只读的 compare 不等锁；Ctrl+C 也先删原文；早就删过的不提示


def test_a_stale_row_with_the_same_key_does_not_block_a_valid_one(world: World) -> None:
    from javmem.side.compare import compare
    from javmem.side.witness import key_of, queue_contradictions

    _packet(world)
    main_line = _side("main", [("m1", "X1 FCN", "托管于", "东岸证券", "生效")])
    side = _with_revision(_side("side", [("s1", "X1 FCN", "托管于", "海港银行", "生效")]))
    result = compare(main_line, side, None)  # type: ignore[arg-type]
    (item,) = result.queue()
    _row(world, key=key_of(item, "side"), quotes=[f"{PACKET_ID}#9.9.9"])  # 以前进过的同一条，证据早就没了
    queued = queue_contradictions(world.settings.library("prod").db_path, result, main_line, side)  # type: ignore[arg-type]
    assert queued.added == 1 and len(_witness_keys(world)) == 1


def test_other_database_errors_in_the_cleanup_do_not_fail_the_withdraw(world: World,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.side.witness as witness
    from javmem.cli import main

    _group_with_witness_item(world)

    def broken(*_: object, **__: object) -> int:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(witness, "drop_stale", broken)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_ID]) == 0  # 撤回已经做完了
    with SideStore.open(world.data, "prod") as store:  # 支线照样扫了：撤回了的原文删了、记了待清
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", "包撤回了")]
        assert store.conn.execute("SELECT count(*) FROM lines").fetchone()[0] == 0


def test_report_only_compare_reads_once_more_instead_of_waiting_for_the_locks(world: World, tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.side.mainline as mainline
    from javmem.pipeline.write import WriteLock
    from javmem.side.cli import EXIT_OK, run

    _, path = _ledger_world(world, tmp_path)
    real = mainline.side_from_ledger
    calls: list[int] = []

    def reading(*args: object, **kwargs: object) -> object:
        calls.append(1)
        if len(calls) == 1:  # 两步读的中间有人写了一笔：要重读
            world.execute("INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('other', '{}', '[]', '2026-11-09')")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mainline, "side_from_ledger", reading)
    with WriteLock(world.data):  # 写账在跑：只读的比照样比（不拿锁）
        assert run(_compare_args(path, tmp_path, queue=False), world.settings, out=lambda _: None) == EXIT_OK  # type: ignore[arg-type]
    assert len(calls) == 2


class _InterruptedWhileDeleting(_OnlineGraph):
    async def delete_group(self, group_id: str) -> tuple[int, int]:
        raise KeyboardInterrupt


def test_ctrl_c_while_deleting_still_drops_the_text(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)
    _online(monkeypatch, _InterruptedWhileDeleting)
    lines: list[str] = []
    after_withdraw(world.settings, "prod", out=lines.append)
    assert any("Ctrl+C" in line and "原文删了" in line for line in lines)
    with SideStore.open(world.data, "prod") as store:
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", "包撤回了")]
        assert store.conn.execute("SELECT count(*) FROM lines").fetchone()[0] == 0


def test_ctrl_c_before_the_sweep_does_not_claim_the_text_is_gone(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.side.hook as hook

    _group_with_witness_item(world)
    world.execute(WITHDRAW_PACKET)

    def interrupted(*_: object, **__: object) -> int:
        raise KeyboardInterrupt  # 头一步清待确认（账本锁着时会等几秒）的时候就按了

    monkeypatch.setattr(hook, "clean", interrupted)
    lines: list[str] = []
    hook.after_withdraw(world.settings, "prod", out=lines.append)
    assert any("Ctrl+C" in line for line in lines)
    assert not any("原文删了" in line for line in lines)  # 还没扫：不能说撤回了的原文已经删了
    with SideStore.open(world.data, "prod") as store:
        assert store.conn.execute("SELECT count(*) FROM lines").fetchone()[0] > 0


def test_no_purge_hint_for_a_packet_that_was_purged_before(world: World) -> None:
    from javmem.side.hook import after_withdraw
    from javmem.side.store import side_dir, side_lock

    _group_with_witness_item(world)
    lines: list[str] = []
    with side_lock(side_dir(world.data, "prod")):
        after_withdraw(world.settings, "prod", purged={PACKET_ID: []}, out=lines.append, graph=FakeGraph())
    assert not any("整个重建" in line for line in lines)  # 早就彻底删除过了：这一次一版都没删
