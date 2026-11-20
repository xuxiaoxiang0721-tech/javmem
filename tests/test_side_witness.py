"""J6.2、J6.4 的后一半：撤回、彻底删除接上支线（javmem.side.hook），第二证人的矛盾进待确认（javmem.side.witness），
主线那一边从账本读（javmem.side.mainline）。体检库是假的，样例都是编的。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from helpers import World
from test_side import PACKET_ID, FakeGraph, _packet, _ran
from test_side_compare import _side

from javmem.side.store import SideStore


def _group_with_witness_item(world: World) -> None:
    _packet(world)
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, FakeGraph())
    world.execute(
        "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('witness_mismatch', ?, '[]', '2026-11-09')",
        json.dumps({"key": "k1", "packet": PACKET_ID}),
    )


def test_withdraw_hook_sweeps_the_group_and_drops_witness_items(world: World) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'")
    graph = FakeGraph()
    graph.groups[f"{PACKET_ID}_r1"] = [{"uuid": "x", "name": "x"}]
    lines: list[str] = []
    after_withdraw(world.settings, "prod", out=lines.append, graph=graph)
    assert graph.groups == {}
    with SideStore.open(world.data, "prod") as store:
        assert store.groups() == []
    assert world.rows("SELECT * FROM review_queue WHERE kind = 'witness_mismatch'") == []
    assert any("体检库" in line for line in lines)


def test_withdraw_hook_does_nothing_without_the_side_branch(world: World) -> None:
    from javmem.side.hook import after_withdraw

    lines: list[str] = []
    after_withdraw(world.settings, "prod", out=lines.append)
    assert lines == [] and not (world.data / "side").exists()


def test_withdraw_hook_offline_drops_text_and_records_pending(world: World) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'")
    lines: list[str] = []
    after_withdraw(world.settings, "prod", out=lines.append)  # 没配体检库的密码：连不上
    with SideStore.open(world.data, "prod") as store:
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", "包撤回了")]
        assert store.conn.execute("SELECT count(*) FROM lines").fetchone()[0] == 0
    assert any("待清" in line for line in lines)


def test_purge_hint_for_packets_that_were_in_the_checkup_db(world: World) -> None:
    from javmem.side.hook import after_withdraw

    _group_with_witness_item(world)
    world.execute("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'")
    lines: list[str] = []
    after_withdraw(world.settings, "prod", purged=[PACKET_ID], out=lines.append, graph=FakeGraph())
    assert any("整个重建" in line for line in lines)


def test_cli_withdraw_runs_the_side_sweep(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    from javmem.cli import main

    _group_with_witness_item(world)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET_ID]) == 0
    with SideStore.open(world.data, "prod") as store:
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", "包撤回了")]
    assert "待清" in capsys.readouterr().out


def test_current_relations_condition() -> None:
    from javmem.side.mainline import eligible_ids

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE relations (relation_id TEXT, withdrawn_at TEXT, superseded_by TEXT, status TEXT, state TEXT);"
        "CREATE TABLE relation_claims (claim_id TEXT, relation_id TEXT);"
        "INSERT INTO relations VALUES ('ok', NULL, NULL, 'confirmed', 'active'), ('paused', NULL, NULL, 'confirmed', 'paused'),"
        " ('ended', NULL, NULL, 'confirmed', 'ended'), ('gone', '2026', NULL, 'confirmed', 'active'),"
        " ('old', NULL, 'ok', 'confirmed', 'active'), ('held', NULL, NULL, 'pending', 'active');"
        "INSERT INTO relation_claims VALUES ('c1', 'ok'), ('c2', 'held');"
    )
    assert eligible_ids(conn) == {"ok", "paused", "ended", "c1"}


def test_contradictions_go_into_the_review_queue_once(world: World) -> None:
    """两种真矛盾都进、只进一回。主线是真写进账本的关系（待确认里记的是账本里的关系编号，撤回了、没了的关系那一条就不算数了）。"""
    from test_relcheck import BANK, NOTE, OWNER, STOCK, relation
    from test_write import day_packet, deliver, write

    from javmem.scoring.common import Entity, Relation, Side
    from javmem.side.compare import compare
    from javmem.side.mainline import read_main_line
    from javmem.side.witness import queue_contradictions

    def extract(window: object) -> dict[str, object]:
        found = []
        for line in window.targets:  # type: ignore[attr-defined]
            text = line.text.rstrip("。")
            if "X1 票据" in text:
                found.append(relation(NOTE, "托管于", BANK, (line.ref, text)))
            if "X2" in text:
                found.append(relation(OWNER, "持有", STOCK, (line.ref, text)))
        return {"entities": [OWNER, NOTE, BANK, STOCK], "facts": [], "relations": found}

    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。", "我：我还拿着 X2。"))
    write(world, extract).close()
    packet = "2026-11-01_2330_desk-bot"
    sentences = [row[0] for row in world.rows("SELECT sentence_id FROM raw_sentences WHERE packet_id = ? ORDER BY id", packet)]
    line = read_main_line(world.settings, "prod", [packet])
    side = Side("side", frozenset({packet}),
                {"s:note": Entity("s:note", "X1 票据", "订单或合同"), "s:broker": Entity("s:broker", "东岸证券", "公司或机构"),
                 "s:me": Entity("s:me", "主人", "人"), "s:x2": Entity("s:x2", "X2", "资产")},
                {}, {"s1": Relation("s1", "s:note", "托管于", "s:broker", {}, state="生效", evidence=(sentences[0],)),
                     "s2": Relation("s2", "s:me", "持有", "s:x2", {}, state="已结束", evidence=(sentences[1],))},
                revisions={packet: 1})
    result = compare(line.side, side, None)  # type: ignore[arg-type]
    ledger = world.settings.library("prod").db_path
    assert queue_contradictions(ledger, result, line.side, side, relation_of=line.relation_of).added == 2  # type: ignore[arg-type]
    rows = world.rows("SELECT claims, quotes FROM review_queue WHERE kind = 'witness_mismatch'")
    assert len(rows) == 2
    claims = [json.loads(row["claims"]) for row in rows]
    assert {one["kind"] for one in claims} == {"另一头不一样", "一边结束了"}
    assert all(one["packet"] == packet and one["key"] and one["revision"] == 1 for one in claims)
    assert all(json.loads(row["quotes"]) and "#" in json.loads(row["quotes"])[0] for row in rows)  # 句子编号，不是原话
    again = queue_contradictions(ledger, result, line.side, side, relation_of=line.relation_of)  # type: ignore[arg-type]
    assert again.added == 0  # 再比一遍不重复进
    assert len(world.rows("SELECT * FROM review_queue WHERE kind = 'witness_mismatch'")) == 2


def test_compare_queue_flag_needs_the_ledger_main_line(tmp_path: Path) -> None:
    import argparse

    from javmem.config import Settings
    from javmem.side.cli import EXIT_INPUT, run

    args = argparse.Namespace(side_command="compare", library="prod", main=str(tmp_path / "m.json"),
                              side=str(tmp_path / "s.json"), packet=None, out=str(tmp_path / "o.json"), queue=True)
    lines: list[str] = []
    assert run(args, Settings.from_env({"JAVMEM_DATA_DIR": str(tmp_path / "data"), "JAVMEM_TIMEZONE": "+08:00"}),
               out=lines.append) == EXIT_INPUT
    assert any("--queue" in line for line in lines)


def test_sweep_result_reports_packets(world: World) -> None:
    """扫一遍给出删了哪几个包（撤回那边要按包删第二证人的待确认）。"""
    from javmem.side.sweep import sweep

    _packet(world)
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, FakeGraph())
        world.execute("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'")
        graph = FakeGraph()
        graph.groups[f"{PACKET_ID}_r1"] = [{"uuid": "x", "name": "x"}]
        result = asyncio.run(sweep(world.settings.library("prod").db_path, store, graph))
    assert result.packets == [PACKET_ID]
