"""巡检：packet-spec 第 12 节每一条都有一个测试。提醒里不抄原文。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from helpers import CHAT_ID, SHANGHAI, World, build_packet

from javmem.cli import main
from javmem.patrol import PatrolItem, run_patrol

NOW = datetime(2026, 11, 9, 8, 30, tzinfo=SHANGHAI)


@pytest.fixture
def clocked(world: World) -> World:
    return world.at(NOW)


def kinds(world: World) -> list[str]:
    return [item.kind for item in run_patrol(world.settings)]


def find(world: World, kind: str) -> list[PatrolItem]:
    return [item for item in run_patrol(world.settings) if item.kind == kind]


def test_clean_after_a_good_intake(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    assert kinds(clocked) == []


def test_never_run_is_flagged(world: World) -> None:
    assert [(item.kind, item.library) for item in run_patrol(world.settings)] == [
        ("task_stale", "prod"),
        ("task_stale", "test"),
    ]


def test_failed_packets_are_reported_by_source_directory(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet(drop=("kind",)))
    clocked.drop("inbox/desk-bot", build_packet(stamp="2026-11-07_2330", drop=("kind",)))
    clocked.drop("inbox/books-bot", build_packet("books-bot", drop=("kind",)))
    clocked.intake()
    items = find(clocked, "failed_packets")
    assert {(item.source, item.count) for item in items} == {("desk-bot", 2), ("books-bot", 1)}


def test_failed_unknown_is_reported(clocked: World) -> None:
    (clocked.data / "inbox").mkdir()
    (clocked.data / "inbox" / "x.md").write_text("放错地方", encoding="utf-8")
    clocked.intake()
    items = find(clocked, "failed_unknown")
    assert [item.count for item in items] == [1]
    assert "_unknown" in items[0].message


def test_stale_task_is_flagged_after_the_configured_hours(world: World) -> None:
    world.at(NOW).intake()
    assert find(world.at(NOW + timedelta(hours=25)), "task_stale") == []
    assert len(find(world.at(NOW + timedelta(hours=27)), "task_stale")) == 2  # 默认 26 小时


def test_a_graph_that_falls_behind_is_flagged(world: World) -> None:
    """主图欠着的最早那一条超过 stale_hours（默认 26 小时）就提醒（J5.4）；没接上图的账本不提醒。只读账本，不连图。"""
    from datetime import timezone

    world.at(NOW).intake()
    world.execute("INSERT INTO graph_dirty (kind, item_id, gen, since) VALUES ('fact', 'F1', 1, '2026-11-08T00:00:00Z'),"
                  " ('relation', 'R1', 1, '2026-11-09T00:00:00Z')")
    later = world.at(NOW + timedelta(hours=10))  # 11-09 10:30Z：最早那一条欠了 34.5 小时
    assert find(later, "graph_behind") == []  # 没接上图
    world.execute("UPDATE graph_state SET enabled = 1, marker_id = 'made-up' WHERE id = 1")
    (item,) = find(later, "graph_behind")
    assert (item.library, item.count) == ("prod", 2)
    assert item.message.startswith("主图欠着 2 条没同步，最早的是 34 小时前，看 javmem graph status --library prod")
    assert find(world.at(datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)), "graph_behind") == []  # 25 小时：还不到
    world.execute("DELETE FROM graph_dirty")
    assert find(later, "graph_behind") == []  # 同步完了


def test_withdrawn_things_still_in_the_graph_are_flagged_after_an_hour(world: World) -> None:
    """撤回、换版本以后欠着的（主图里还留着撤回了的东西）过 1 小时就报，叫人跑 javmem graph sync；别的照旧 26 小时。
    账本里认：图里有、账本里撤回了或者没了的事实、关系、别名，撤回了、彻底删了、换下来一版的包。刚收进来的包不算。"""
    from datetime import timezone

    world.drop("inbox/desk-bot", build_packet())  # CHAT_ID：刚收进来的
    world.at(NOW).intake()
    assert world.packet_row(CHAT_ID) is not None
    world.execute("UPDATE graph_state SET enabled = 1, marker_id = 'made-up' WHERE id = 1")
    world.execute("INSERT INTO graph_items (kind, item_id, fingerprint) VALUES ('fact', 'F-gone', 'x')")
    world.execute("INSERT INTO graph_dirty (kind, item_id, gen, since) VALUES ('fact', 'F-gone', 1, '2026-11-09T00:00:00Z'),"
                  " ('packet', ?, 1, '2026-11-09T00:10:00Z'), ('fact', 'F-new', 1, '2026-11-08T20:00:00Z')", CHAT_ID)
    clock = world.at(datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc))  # 最早那条撤回的欠了 1.5 小时
    (item,) = find(clock, "graph_withdrawn")
    assert (item.library, item.count) == ("prod", 1)  # 刚收进来的包、图里没有的新事实不算撤回那一种
    assert item.message == ("撤回了的还在主图里：撤回、换版本以后有 1 条没同步，最早的是 1 小时前，"
                            "跑 javmem graph sync --library prod")
    assert find(clock, "graph_behind") == []  # 别的才欠了 5.5 小时
    world.execute("UPDATE packets SET withdrawn_at = '2026-11-09T08:00:00+08:00' WHERE packet_id = ?", CHAT_ID)
    assert find(clock, "graph_withdrawn")[0].count == 2  # 撤回了的包也算
    assert find(world.at(datetime(2026, 11, 9, 0, 50, tzinfo=timezone.utc)), "graph_withdrawn") == []  # 还不到 1 小时
    (behind,) = find(world.at(datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)), "graph_behind")
    assert behind.count == 1 and "主图欠着 1 条没同步，最早的是 28 小时前" in behind.message


def test_a_real_withdrawal_counts_as_withdrawn_and_a_new_packet_does_not(world: World) -> None:
    """照真的走一遍：接上（内存里的）图、同步，再收一个新包、撤回一个旧包，触发器记下的欠账分得开两种。"""
    from datetime import timezone

    from test_graph import PACKET_IDS, PACKETS, connect_graph, rich_extract, run_sync
    from test_write import deliver, write

    from javmem.withdraw import withdraw

    clocked = world.at(NOW)
    for packet in PACKETS[:3]:
        deliver(clocked, packet)
    write(clocked, rich_extract).close()
    graph = connect_graph(clocked)
    assert run_sync(clocked, graph).status == "ok"
    deliver(clocked, PACKETS[3])  # 新包：收进来、还没写账，欠着的是“登记了 → 现行”那一种
    later = world.at(datetime.now(timezone.utc) + timedelta(hours=2))  # 欠账的时间是墙上的钟
    assert find(later, "graph_withdrawn") == []
    withdraw(clocked.settings, "prod", packets=[PACKET_IDS[0]])
    (item,) = find(later, "graph_withdrawn")
    assert item.count >= 2 and "跑 javmem graph sync --library prod" in item.message  # 撤回的包、跟着撤回的事实
    assert run_sync(clocked, graph).status == "ok"
    assert find(later, "graph_withdrawn") == []  # 同步完了


def test_stale_hours_can_be_configured(world: World) -> None:
    from dataclasses import replace

    world.at(NOW).intake()
    tighter = World(world.data, replace(world.settings, stale_hours=1, clock=lambda: NOW + timedelta(hours=2)))
    assert len(find(tighter, "task_stale")) == 2


def test_extraction_failed_packets_are_flagged(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    clocked.execute("UPDATE packets SET extraction = 'failed' WHERE packet_id = ?", CHAT_ID)
    items = find(clocked, "extraction_failed")
    assert [(item.packet_id, item.revision) for item in items] == [(CHAT_ID, 1)]


def daily(stamp: str, start: str, end: str, source: str = "desk-bot", **fields: object) -> str:
    return build_packet(source, stamp, covers_from=start, covers_to=end, created_at=end, **fields)


def test_gap_in_daily_packets_is_flagged(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", daily("2026-11-06_2330", "2026-11-05T23:30:00+08:00", "2026-11-06T23:30:00+08:00"))
    clocked.drop("inbox/desk-bot", daily("2026-11-08_2330", "2026-11-07T23:30:00+08:00", "2026-11-08T23:30:00+08:00"))
    clocked.intake()
    items = find(clocked, "daily_gap")
    assert [item.source for item in items] == ["desk-bot"]
    assert "2026-11-06T23:30:00+08:00" in items[0].message
    assert "2026-11-07T23:30:00+08:00" in items[0].message


def test_contiguous_daily_packets_have_no_gap(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", daily("2026-11-07_2330", "2026-11-06T23:30:00+08:00", "2026-11-07T23:30:00+08:00"))
    clocked.drop("inbox/desk-bot", daily("2026-11-08_2330", "2026-11-07T23:30:00+08:00", "2026-11-08T23:30:00+08:00"))
    clocked.intake()
    assert find(clocked, "daily_gap") == []


def test_packets_split_by_permission_are_not_a_problem(clocked: World) -> None:
    """按权限拆开的包时间段相同。"""
    start, end = "2026-11-07T23:30:00+08:00", "2026-11-08T23:30:00+08:00"
    clocked.drop("inbox/books-bot", daily("2026-11-08_2330", start, end, "books-bot", domain="[finance]"))
    split = build_packet(
        "books-bot",
        packet_id="2026-11-08_2330_books-bot_2",
        covers_from=start,
        covers_to=end,
        domain="[business]",
    )
    clocked.drop("inbox/books-bot", split)
    clocked.intake()
    assert find(clocked, "daily_gap") == []


def test_timezones_do_not_fool_the_gap_check(clocked: World) -> None:
    """同一个时刻，写成两个时区，不算缺口。"""
    clocked.drop("inbox/desk-bot", daily("2026-11-07_2330", "2026-11-06T23:30:00+08:00", "2026-11-07T23:30:00+08:00"))
    clocked.drop("inbox/desk-bot", daily("2026-11-08_2330", "2026-11-07T15:30:00+00:00", "2026-11-08T23:30:00+08:00"))
    clocked.intake()
    assert find(clocked, "daily_gap") == []


@pytest.mark.parametrize("kind", ["adhoc", "backfill"])
def test_adhoc_and_backfill_do_not_count_for_gaps(clocked: World, kind: str) -> None:
    clocked.drop("inbox/desk-bot", daily("2026-11-01_2330", "2026-10-31T23:30:00+08:00", "2026-11-01T23:30:00+08:00", kind=kind))
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    assert find(clocked, "daily_gap") == []


def test_each_source_has_its_own_timeline(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", daily("2026-11-01_2330", "2026-10-31T23:30:00+08:00", "2026-11-01T23:30:00+08:00"))
    clocked.drop("inbox/books-bot", daily("2026-11-08_2330", "2026-11-07T23:30:00+08:00", "2026-11-08T23:30:00+08:00", "books-bot"))
    clocked.intake()
    assert find(clocked, "daily_gap") == []


def test_same_revision_different_content_is_flagged(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    clocked.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：改了。\n"))
    clocked.intake()
    assert [item.packet_id for item in find(clocked, "same_revision_different")] == [CHAT_ID]


def test_withdrawn_packet_coming_back_is_flagged(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    clocked.execute("UPDATE packets SET withdrawn_at = '2026-11-08T23:40:00+08:00' WHERE packet_id = ?", CHAT_ID)
    clocked.drop("inbox/desk-bot", build_packet(revision=2))
    clocked.intake()
    assert [item.packet_id for item in find(clocked, "withdrawn_resent")] == [CHAT_ID]


def test_parse_warnings_are_flagged_with_their_lines(clocked: World) -> None:
    body = "## 原话\n- [10:30] 我：一句。\n- [坏] 坏的一行\n- [10:20] 我：倒退。\n没缩进\n\n## 备注\n内容\n"
    clocked.drop("inbox/desk-bot", build_packet(body=body))
    clocked.intake()
    found = [(item.kind, item.line) for item in run_patrol(clocked.settings)]
    assert found == [("bad_line", 18), ("time_backwards", 19), ("unrecognized_line", 20), ("unknown_heading", 22)]


def test_redaction_hit_reports_packet_and_type_only(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：后台密码是 abcd1234。\n"))
    clocked.intake()
    items = find(clocked, "redaction_hit")
    assert [item.packet_id for item in items] == [CHAT_ID]
    assert "密码" in items[0].message
    assert "abcd1234" not in items[0].message


def test_stray_files_and_directories_in_a_source_directory_are_flagged(clocked: World) -> None:
    source_dir = clocked.data / "inbox" / "desk-bot"
    (source_dir / ".tmp").mkdir(parents=True)
    (source_dir / "sub").mkdir()
    (source_dir / "notes.txt").write_text("不是包", encoding="utf-8")
    clocked.intake()
    messages = [item.message for item in find(clocked, "inbox_stray")]
    assert any("notes.txt" in message for message in messages)
    assert any("sub" in message for message in messages)
    assert not any(".tmp" in message for message in messages)


def test_unregistered_directory_is_flagged(clocked: World) -> None:
    (clocked.data / "inbox-test" / "no-such-bot").mkdir(parents=True)
    clocked.intake()
    items = find(clocked, "inbox_unknown_dir")
    assert [(item.library, "no-such-bot" in item.message) for item in items] == [("test", True)]


def test_packet_that_stays_in_the_inbox_is_flagged(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """一个包一直收不进来，不能巡检还说没事。"""
    from javmem.intake import _LibraryIntake

    def explode(self: object, path: object, packet: object) -> None:
        raise OverflowError("写库时溢出")

    path = world.drop("inbox/desk-bot", build_packet())
    an_hour_ago = time.time() - 3600
    os.utime(path, (an_hour_ago, an_hour_ago))
    monkeypatch.setattr(_LibraryIntake, "_register", explode)
    assert world.intake().count("error") == 1

    items = find(world, "inbox_stuck")
    assert [item.library for item in items] == ["prod"]
    assert f"inbox/desk-bot/{CHAT_ID}.md" in items[0].message


def test_file_in_the_inbox_root_that_stays_is_flagged(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.intake as intake

    root = world.data / "inbox"
    root.mkdir()
    path = root / "x.md"
    path.write_text("放错地方", encoding="utf-8")
    an_hour_ago = time.time() - 3600
    os.utime(path, (an_hour_ago, an_hour_ago))

    def busy(source: object, target: object) -> None:
        raise PermissionError("被别的程序占着")

    monkeypatch.setattr(intake.os, "replace", busy)
    world.intake()
    assert len(find(world, "inbox_stuck")) == 1


def test_packet_that_arrived_after_the_last_run_is_not_flagged(world: World) -> None:
    world.at(NOW).intake()
    path = world.drop("inbox/desk-bot", build_packet())
    later = (NOW + timedelta(minutes=5)).timestamp()
    os.utime(path, (later, later))
    assert find(world.at(NOW + timedelta(minutes=6)), "inbox_stuck") == []


def test_settled_checks_wait_while_an_intake_is_running(clocked: World) -> None:
    """收件跑到一半时账上和归档本来就对不上，这时巡检不能误报。"""
    from javmem.intake import intake_lock

    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    archived = clocked.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md"
    archived.chmod(0o600)
    archived.unlink()

    with intake_lock(clocked.data):
        assert find(clocked, "archive_missing") == []
    assert len(find(clocked, "archive_missing")) == 1


def test_failed_packet_without_a_note_is_flagged(clocked: World) -> None:
    """断在挪文件和写说明之间，失败包没有说明。"""
    directory = clocked.data / "failed" / "desk-bot"
    directory.mkdir(parents=True)
    (directory / f"{CHAT_ID}.md").write_text("没有说明", encoding="utf-8")
    clocked.intake()
    items = find(clocked, "failed_without_note")
    assert [item.source for item in items] == ["desk-bot"]
    assert f"{CHAT_ID}.md" in items[0].message


def test_note_without_a_packet_is_not_counted(clocked: World) -> None:
    """失败包被打包方拿走修了，说明还留着。不算失败包。"""
    directory = clocked.data / "failed" / "desk-bot"
    directory.mkdir(parents=True)
    (directory / f"{CHAT_ID}.error.txt").write_text("code: bad_header\n", encoding="utf-8")
    clocked.intake()
    assert kinds(clocked) == []


def test_linked_directory_in_the_inbox_is_flagged(clocked: World, tmp_path: Path) -> None:
    from test_intake import make_link

    make_link(clocked.data / "inbox" / "desk-bot", tmp_path / "outside")
    clocked.intake()
    assert len(find(clocked, "inbox_stray")) == 1


def test_unknown_field_is_flagged(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet(sensitve="[payroll]"))
    clocked.intake()
    items = find(clocked, "unknown_field")
    assert [(item.packet_id, item.line) for item in items] == [(CHAT_ID, 14)]
    assert "sensitve" in items[0].message


def test_archive_file_missing_is_flagged(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    archived = clocked.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md"
    archived.chmod(0o600)
    archived.unlink()
    assert [item.packet_id for item in find(clocked, "archive_missing")] == [CHAT_ID]


def test_archive_file_unknown_to_the_ledger_is_flagged(clocked: World) -> None:
    clocked.intake()
    month = clocked.data / "archive" / "2026" / "11"
    month.mkdir(parents=True)
    (month / "2026-11-01_2330_desk-bot.md").write_text("账上没有", encoding="utf-8")
    items = find(clocked, "archive_orphan")
    assert len(items) == 1
    assert "2026-11-01_2330_desk-bot" in items[0].message


def test_skipped_and_kept_versions_are_not_orphans(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    clocked.drop("inbox/desk-bot", build_packet())  # 跳过
    clocked.intake()
    clocked.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：改了。\n"))  # 留档
    clocked.intake()
    assert kinds(clocked) == []


def test_unfinished_registration_is_flagged(clocked: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.intake import _LibraryIntake

    class PowerCut(BaseException):
        pass

    def crash(*args: object, **kwargs: object) -> None:
        raise PowerCut

    clocked.drop("inbox/desk-bot", build_packet())
    with monkeypatch.context() as patch:
        patch.setattr(_LibraryIntake, "_archive", crash)
        with pytest.raises(PowerCut):
            clocked.intake()
    assert [item.packet_id for item in find(clocked, "registration_stuck")] == [CHAT_ID]


def test_broken_registry_is_reported_not_raised(clocked: World) -> None:
    clocked.intake()
    clocked.settings.sources_file.write_text("[不合法", encoding="utf-8")
    assert kinds(clocked) == ["sources_unreadable"]


def test_test_library_is_checked_too(clocked: World) -> None:
    clocked.drop("inbox-test/desk-bot", build_packet(env="test", body="## 原话\n- [10:30] 我：一句。\n- [坏] 坏的一行\n"))
    clocked.intake()
    assert [(item.kind, item.library) for item in run_patrol(clocked.settings)] == [("bad_line", "test")]


def test_nothing_from_the_packet_ends_up_in_the_output(clocked: World) -> None:
    body = "## 原话\n- [10:30] 我：这句话不该出现，密码是 abcd1234。\n- [坏] 这句话也不该出现\n"
    clocked.drop("inbox/desk-bot", build_packet(body=body))
    clocked.drop("inbox/desk-bot", build_packet(stamp="2026-11-07_2330", drop=("kind",), body="## 原话\n- [10:30] 我：这句话不该出现。\n"))
    clocked.intake()
    dump = json.dumps([item.to_dict() for item in run_patrol(clocked.settings)], ensure_ascii=False)
    assert "这句话" not in dump
    assert "abcd1234" not in dump


# ---- 命令行 ----


def test_cli_patrol_exit_codes(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["patrol"]) == 1
    assert "件要看" in capsys.readouterr().out
    world.intake()
    assert main(["patrol"]) == 0
    assert "没有要看的" in capsys.readouterr().out


def test_cli_patrol_json(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    main(["patrol", "--json"])
    items = json.loads(capsys.readouterr().out)
    assert {item["kind"] for item in items} == {"task_stale", "checks_skipped"}
    # 没存过版本的基准（javmem versions --save）：和基准比的那几样这回没看
    assert [item["reason"] for item in items if item["kind"] == "checks_skipped"] == ["no_versions_baseline"]


def test_cli_intake_reports_each_file(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.drop("inbox/books-bot", build_packet("books-bot", drop=("kind",)))
    assert main(["intake"]) == 0
    output = capsys.readouterr().out
    assert f"收下 [prod] inbox/desk-bot/{CHAT_ID}.md" in output
    assert "拒收 [prod] inbox/books-bot/2026-11-08_2330_books-bot.md（bad_header）" in output
    assert "收下 1，替换 0，跳过 0，拒收 1，接着做完 0，出错 0" in output


def test_cli_intake_without_registry_says_so(data_dir: object, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["intake"]) == 2
    assert "JAVMEM_SOURCES_FILE" in capsys.readouterr().err


def test_cli_intake_when_busy(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    from javmem.intake import intake_lock

    with intake_lock(world.data):
        assert main(["intake"]) == 3


def stop_after(rounds: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """让 --watch 跑几轮就停，就像按了 Ctrl+C。"""
    import javmem.cli as cli

    left = {"rounds": rounds}

    def fake_sleep(seconds: float) -> None:
        left["rounds"] -= 1
        if left["rounds"] == 0:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)


def test_cli_watch_keeps_collecting(world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    stop_after(1, monkeypatch)
    world.drop("inbox/desk-bot", build_packet())
    assert main(["intake", "--watch", "60"]) == 0
    assert f"收下 [prod] inbox/desk-bot/{CHAT_ID}.md" in capsys.readouterr().out
    assert world.files("inbox/desk-bot") == []


def test_cli_watch_survives_a_broken_registry(world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    stop_after(1, monkeypatch)
    path = world.drop("inbox/desk-bot", build_packet())
    world.settings.sources_file.write_text("[不合法", encoding="utf-8")
    assert main(["intake", "--watch", "60"]) == 0
    assert "来源登记表" in capsys.readouterr().err
    assert path.exists()


@pytest.mark.parametrize("seconds", ["0", "-5"])
def test_cli_watch_needs_a_positive_interval(world: World, seconds: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["intake", f"--watch={seconds}"]) == 2
    assert "--watch" in capsys.readouterr().err


def test_cli_reports_ledger_trouble_without_a_traceback(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    db = world.settings.library("prod").db_path
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a database at all, just some bytes......")
    assert main(["intake"]) == 2
    assert "错误" in capsys.readouterr().err


def test_cli_init_makes_source_directories(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == 0
    assert "4 个来源" in capsys.readouterr().out
    data = world.data
    assert (data / "inbox" / "desk-bot" / ".tmp").is_dir()
    assert (data / "inbox-test" / "desk-bot" / ".tmp").is_dir()
    assert (data / "failed" / "desk-bot").is_dir()
    assert (data / "failed" / "_unknown").is_dir()
    # 还在试的来源只有测试目录
    assert (data / "inbox-test" / "trial-bot" / ".tmp").is_dir()
    assert not (data / "inbox" / "trial-bot").exists()
    assert (data / "failed" / "trial-bot").is_dir()
