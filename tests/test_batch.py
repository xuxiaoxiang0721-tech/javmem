"""从账本和归档取要处理的包，以及 javmem windows。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from helpers import CHAT_ID, CHAT_PACKET, MAIL_ID, MAIL_PACKET, World, build_packet

from javmem.cli import main
from javmem.config import Settings
from javmem.ledger import transaction
from javmem.pipeline.batch import load_queued
from javmem.pipeline.normalize import WITHDRAWN
from javmem.pipeline.render import render_window
from javmem.pipeline.window import build_windows
from javmem.raw import withdraw_sentence


@pytest.fixture
def stocked(world: World) -> World:
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    report = world.intake()
    assert report.count("accepted") == 2
    return world


def test_loads_queued_packets(stocked: World) -> None:
    batch = load_queued(stocked.settings, "prod")
    assert [packet.packet_id for packet in batch.packets] == [MAIL_ID, CHAT_ID]  # 按 covers_from
    assert batch.problems == []
    assert load_queued(stocked.settings, "test").packets == []  # 测试库是另一个库


def test_only_queued_current_packets(stocked: World) -> None:
    stocked.execute("UPDATE packets SET extraction = 'done' WHERE packet_id = ?", MAIL_ID)
    assert [packet.packet_id for packet in load_queued(stocked.settings, "prod").packets] == [CHAT_ID]
    stocked.execute("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00' WHERE packet_id = ?", CHAT_ID)
    assert load_queued(stocked.settings, "prod").packets == []


def test_skips_packet_being_replaced(stocked: World) -> None:
    conn = stocked.ledger()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO packets SELECT packet_id, 2, source, env, framework, channel, mailbox, kind, created_at,"
                " covers_from, covers_to, covers_from_epoch, covers_to_epoch, partitions, sensitive, privacy, entities,"
                " continues, content_hash, inbox_path, archive_path, 'registered', extraction, received_at, NULL, NULL,"
                " NULL, NULL, NULL, NULL FROM packets WHERE packet_id = ?",  # 最后两个是 0024 的 extraction_note、0029 的 queued_at
                (CHAT_ID,),
            )
    finally:
        conn.close()
    assert [packet.packet_id for packet in load_queued(stocked.settings, "prod").packets] == [MAIL_ID]


def test_withdrawn_sentences_are_marked(stocked: World) -> None:
    sentence_id = f"{CHAT_ID}#2.1.1"
    conn = stocked.ledger()
    try:
        with transaction(conn):
            assert withdraw_sentence(conn, sentence_id, "2026-11-09T00:00:00+08:00")
    finally:
        conn.close()
    chat = next(packet for packet in load_queued(stocked.settings, "prod").packets if packet.packet_id == CHAT_ID)
    kinds = {line.sentence_id: line.kind for unit in chat.units for line in unit.lines}
    assert kinds[sentence_id] == WITHDRAWN


def test_archive_that_does_not_match_is_a_problem(stocked: World) -> None:
    row = stocked.rows("SELECT archive_path FROM packets WHERE packet_id = ?", CHAT_ID)[0]
    path = stocked.data / row["archive_path"]
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    path.write_text(CHAT_PACKET.replace("小李跟", "老周跟"), encoding="utf-8")
    batch = load_queued(stocked.settings, "prod")
    assert [packet.packet_id for packet in batch.packets] == [MAIL_ID]
    assert batch.problems == [(CHAT_ID, "归档和包登记对不上")]


def test_only_named_packets(stocked: World) -> None:
    batch = load_queued(stocked.settings, "prod", packet_ids=[CHAT_ID, "2026-01-01_0000_desk-bot"])
    assert [packet.packet_id for packet in batch.packets] == [CHAT_ID]
    assert batch.problems == [("2026-01-01_0000_desk-bot", "不在排队的包里")]


def test_windows_command(stocked: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["windows", "--library", "prod", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["totals"]["packets"] == 2
    assert report["totals"]["windows"] == 2
    by_id = {entry["packet_id"]: entry for entry in report["packets"]}
    assert by_id[CHAT_ID]["windows"] == [f"{CHAT_ID}#w1"]
    assert by_id[CHAT_ID]["kinds"]["attachment_ref"] == 1
    assert by_id[CHAT_ID]["attachment_notes"] == 1
    # 签名里写部门、公司的那一行（Purchasing, Example Home Co.）能当证据（D101）
    assert by_id[MAIL_ID]["kinds"]["signature"] == 5
    assert by_id[MAIL_ID]["kinds"]["signature_info"] == 1
    window = report["windows"][0]
    assert {"target_sentences", "context_sentences", "target_chars", "context_chars", "rendered_chars"} <= set(window)
    assert "越南" not in json.dumps(report, ensure_ascii=False)  # 只有数，不抄原文


def test_windows_show(stocked: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["windows", "--library", "prod", "--show", f"{CHAT_ID}#w1"]) == 0
    assert "T3 越南那批订单还是小李跟。" in capsys.readouterr().out
    assert main(["windows", "--library", "prod", "--show", "nope#w1"]) == 1


def test_raw_index_that_does_not_match_the_archive_is_a_problem(stocked: World) -> None:
    stocked.execute("UPDATE raw_sentences SET text = '别的话。' WHERE sentence_id = ?", f"{CHAT_ID}#2.1.1")
    batch = load_queued(stocked.settings, "prod")
    assert batch.problems == [(CHAT_ID, "原文索引和归档对不上")]


def _split_pair() -> tuple[str, str]:
    """拆开的一段对话：后一包 continues 指着前一包。"""
    first = build_packet(
        stamp="2026-11-08_1100",
        body="## 原话 · 2026-11-08 10:30–10:32\n- [10:30] 我：越南那批订单下周出货。\n- [10:32] desk-bot：好的，记下了。\n",
        covers_from="2026-11-08T10:00:00+08:00",
        covers_to="2026-11-08T11:00:00+08:00",
    )
    second = build_packet(
        stamp="2026-11-08_1200",
        body="## 原话 · 2026-11-08 11:05–11:06\n- [11:05] 我：那批的单价不变。\n- [11:06] desk-bot：明白。\n",
        covers_from="2026-11-08T11:00:00+08:00",
        covers_to="2026-11-08T12:00:00+08:00",
        continues="2026-11-08_1100_desk-bot",
    )
    return first, second


def _second_window_text(world: World) -> str:
    batch = load_queued(world.settings, "prod")
    windows = build_windows(batch.packets, context_pool=batch.context)
    window = next(window for window in windows if window.target_packets == ["2026-11-08_1200_desk-bot"])
    return render_window(window).text


def test_previous_part_already_done_is_loaded_as_context(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """拆开的包分两次处理：上一包已经 done 了，也读进来当上文，窗口和整批一起处理时一字不差，L3 的结果对得上。"""
    first, second = _split_pair()
    world.drop("inbox/desk-bot", first)
    world.drop("inbox/desk-bot", second)
    world.intake()
    together = _second_window_text(world)
    assert "越南那批订单下周出货" in together  # 上一包的末尾当上文

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(other))
    later = World(other.resolve(), Settings.from_env())
    later.drop("inbox/desk-bot", first)
    later.intake()
    later.execute("UPDATE packets SET extraction = 'done'")  # 上一包先处理完了
    later.drop("inbox/desk-bot", second)
    later.intake()
    batch = load_queued(later.settings, "prod")
    assert [packet.packet_id for packet in batch.packets] == ["2026-11-08_1200_desk-bot"]
    assert [packet.packet_id for packet in batch.context] == ["2026-11-08_1100_desk-bot"]
    assert _second_window_text(later) == together
