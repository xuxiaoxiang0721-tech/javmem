"""收件：packet-spec 第 8 到 10 节。六种拒收、位置表、revision 四种情况、测试包只进测试库。"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from helpers import (
    CHAT_ID,
    CHAT_PACKET,
    MAIL_ID,
    MAIL_PACKET,
    SHANGHAI,
    World,
    build_mail_packet,
    build_packet,
    warnings_of,
)

import javmem.intake as intake_module
from javmem.config import ConfigError
from javmem.intake import IntakeBusy, intake_lock, run_intake
from javmem.packet.model import (
    BAD_LINE,
    TIME_BACKWARDS,
    UNKNOWN_HEADING,
    UNRECOGNIZED_LINE,
)

NOW = datetime(2026, 11, 8, 23, 31, 5, tzinfo=SHANGHAI)


@pytest.fixture
def clocked(world: World) -> World:
    return world.at(NOW)


def read_error(world: World, relative: str) -> dict[str, str]:
    text = (world.data / relative).read_text(encoding="utf-8")
    return dict(line.split(": ", 1) for line in text.strip().split("\n"))


# ---- 规范的两个例子 ----


def test_chat_example_lands_in_the_ledger_and_archive(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", CHAT_PACKET)
    report = clocked.intake()

    assert report.count("accepted") == 1
    assert clocked.files("inbox/desk-bot") == []
    assert clocked.files("archive") == [f"2026/11/{CHAT_ID}.md"]
    row = clocked.packet_row(CHAT_ID)
    assert row["state"] == "current"
    assert row["extraction"] == "queued"
    assert row["source"] == "desk-bot"
    assert row["env"] == "prod"
    assert json.loads(row["partitions"]) == ["business"]
    assert json.loads(row["sensitive"]) == []
    assert row["covers_from"] == "2026-11-07T23:30:00+08:00"
    assert row["covers_to"] == "2026-11-08T23:30:00+08:00"
    assert row["received_at"] == "2026-11-08T23:31:05+08:00"
    assert row["withdrawn_at"] is None
    assert row["inbox_path"] == f"inbox/desk-bot/{CHAT_ID}.md"
    assert row["archive_path"] == f"archive/2026/11/{CHAT_ID}.md"
    assert warnings_of(clocked, CHAT_ID) == []


def test_mail_example_lands_in_the_ledger_and_archive(clocked: World) -> None:
    clocked.drop("inbox/mail-sales-a", MAIL_PACKET)
    assert clocked.intake().count("accepted") == 1
    row = clocked.packet_row(MAIL_ID)
    assert row["channel"] == "email"
    assert row["mailbox"] == "li.ming@example.com"
    assert clocked.files("archive") == [f"2026/11/{MAIL_ID}.md"]
    assert warnings_of(clocked, MAIL_ID) == []


def test_archive_is_read_only_and_byte_for_byte(world: World) -> None:
    content = CHAT_PACKET.replace("\n", "\r\n").encode("utf-8")
    world.drop("inbox/desk-bot", content, name=f"{CHAT_ID}.md")
    world.intake()
    archived = world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md"
    assert archived.read_bytes() == content
    assert not os.stat(archived).st_mode & stat.S_IWRITE


# ---- 六种拒收 ----


@pytest.mark.parametrize(
    ("code", "content"),
    [
        ("bad_header", build_packet(drop=("kind",))),
        ("unknown_source", build_packet("books-bot")),
        ("env_mismatch", build_packet(env="test")),
        ("no_raw_section", build_packet(body="## 摘要（Bot 写的）\n- 只有摘要\n")),
        ("time_out_of_range", build_packet(body="## 原话\n- [2026-11-01 10:42] 我：太早了。\n")),
        ("not_utf8", build_packet().encode("utf-8") + "这段是别的编码".encode("gbk")),
    ],
)
def test_six_rejections_go_to_failed_with_an_error_file(clocked: World, code: str, content: str | bytes) -> None:
    path = clocked.drop("inbox/desk-bot", content)
    report = clocked.intake()

    assert [(o.result, o.code) for o in report.outcomes] == [("failed", code)]
    assert clocked.files("inbox/desk-bot") == []
    stem = path.name.removesuffix(".md")
    assert clocked.files("failed/desk-bot") == sorted([path.name, f"{stem}.error.txt"])
    error = read_error(clocked, f"failed/desk-bot/{stem}.error.txt")
    assert error["code"] == code
    assert error["file"] == path.name
    assert error["inbox"] == "inbox/desk-bot"
    assert error["checked_at"] == "2026-11-08T23:31:05+08:00"
    assert error["detail"]

    # 不入库，不归档
    assert clocked.rows("SELECT count(*) AS n FROM packets")[0]["n"] == 0
    assert clocked.files("archive") == []


def test_error_file_matches_the_spec_example(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet(body="## 原话\n\n\n- [2026-11-01 10:42] 我：太早了。\n"))
    clocked.intake()
    text = (clocked.data / "failed" / "desk-bot" / f"{CHAT_ID}.error.txt").read_text(encoding="utf-8")
    assert text == (
        "code: time_out_of_range\n"
        f"file: {CHAT_ID}.md\n"
        "inbox: inbox/desk-bot\n"
        "checked_at: 2026-11-08T23:31:05+08:00\n"
        "detail: 第 19 行的时间 2026-11-01 10:42 早于 covers_from 超过一天\n"
    )


@pytest.mark.parametrize(
    "content",
    [
        build_packet(secret="[这句话不该出现"),  # YAML 解析不了
        build_packet(source="这句话不该出现"),  # source 不对
        build_packet(domain="[这句话不该出现]"),  # 分区不对
        build_packet(sensitive="[这句话不该出现]"),  # 超出 may_carry
        build_packet(body="## 原话\n- [2026-11-01 10:42] 我：这句话不该出现。\n"),
        build_packet(body="## 摘要\n- 这句话不该出现。\n"),
    ],
)
def test_error_file_never_copies_the_packet(world: World, content: str) -> None:
    world.drop("inbox/desk-bot", content, name=f"{CHAT_ID}.md")
    world.intake()
    text = (world.data / "failed" / "desk-bot" / f"{CHAT_ID}.error.txt").read_text(encoding="utf-8")
    assert "这句话不该出现" not in text


def test_same_name_in_failed_gets_a_number_on_both_files(world: World) -> None:
    for _ in range(3):
        world.drop("inbox/desk-bot", build_packet(drop=("kind",)))
        world.intake()
    assert world.files("failed/desk-bot") == sorted(
        [
            f"{CHAT_ID}.md",
            f"{CHAT_ID}.error.txt",
            f"{CHAT_ID}_2.md",
            f"{CHAT_ID}_2.error.txt",
            f"{CHAT_ID}_3.md",
            f"{CHAT_ID}_3.error.txt",
        ]
    )


def test_rejected_file_that_cannot_be_moved_leaves_no_notes_behind(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件被别的程序占着，每轮都拒不了。不能每轮多一个说明文件。"""
    real = os.replace
    busy = {"on": True}

    def flaky(source: object, target: object) -> None:
        if busy["on"] and "inbox" in str(source):
            raise PermissionError("被别的程序占着")
        real(source, target)

    path = world.drop("inbox/desk-bot", build_packet(drop=("kind",)))
    monkeypatch.setattr(intake_module.os, "replace", flaky)
    for _ in range(3):
        report = world.intake()
        assert [(o.result, o.code) for o in report.outcomes] == [("error", "PermissionError")]
        assert path.exists()
        assert world.files("failed/desk-bot") == []

    busy["on"] = False
    assert world.intake().count("failed") == 1
    assert world.files("failed/desk-bot") == sorted([f"{CHAT_ID}.error.txt", f"{CHAT_ID}.md"])


@pytest.mark.parametrize(
    "line",
    ['subject: "bad-\\uDCE9"', 'subject: "\\uD800"'],
)
def test_unwritable_mail_header_goes_to_failed_and_can_be_fixed(world: World, line: str) -> None:
    """写不进账本的字符要在校验时拦下。不能先归档再炸，那样包号就永远卡死了。"""
    from helpers import build_mail_packet, mail_header_block

    def mail(subject: str) -> str:
        block = mail_header_block(subject=subject)
        return build_mail_packet(sections=f"## 原话 · 邮件\n\n{block}\n  正文。\n")

    world.drop("inbox/mail-sales-a", mail(line.split(": ", 1)[1]))
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "bad_header")]
    assert world.files("archive") == []
    assert world.rows("SELECT count(*) AS n FROM packets")[0]["n"] == 0

    world.drop("inbox/mail-sales-a", mail('"改好了"'))
    assert world.intake().count("accepted") == 1


def test_unwritable_entity_goes_to_failed_instead_of_staying_in_the_inbox(world: World) -> None:
    path = world.drop("inbox/desk-bot", build_packet(entities='["\\uD800"]'))
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "bad_header")]
    assert not path.exists()
    assert world.files("failed/desk-bot")


def test_failed_does_not_count_as_received(world: World) -> None:
    """修好了直接重投，不用加 revision。"""
    world.drop("inbox/desk-bot", build_packet(drop=("kind",)))
    world.intake()
    world.drop("inbox/desk-bot", build_packet())
    assert world.intake().count("accepted") == 1
    assert world.packet_row(CHAT_ID)["revision"] == 1


def test_a_strange_file_does_not_stop_the_others(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """解析时出了想不到的错，进 failed，后面的包照收。"""
    import javmem.intake as intake

    real = intake.validate_packet

    def explode_on_one(raw: bytes, **kwargs: object):
        if b"explode" in raw:
            raise RuntimeError("这句话不该出现")
        return real(raw, **kwargs)

    monkeypatch.setattr(intake, "validate_packet", explode_on_one)
    world.drop("inbox/desk-bot", build_packet(stamp="2026-11-07_2330", note="explode"))
    world.drop("inbox/desk-bot", build_packet())
    report = world.intake()
    assert report.count("failed") == 1
    assert report.count("accepted") == 1
    error = read_error(world, "failed/desk-bot/2026-11-07_2330_desk-bot.error.txt")
    assert error["code"] == "bad_header"
    assert "RuntimeError" in error["detail"]
    assert "这句话不该出现" not in error["detail"]


# ---- 第 9 节的位置表，8 行 ----


def test_position_row_1_right_directory_and_env(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    assert world.intake().count("accepted") == 1


def test_position_row_2_test_directory(world: World) -> None:
    world.drop("inbox-test/desk-bot", build_packet(env="test"))
    assert world.intake().count("accepted") == 1


def test_position_row_3_header_names_another_source(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet("books-bot"))
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "unknown_source")]
    assert "2026-11-08_2330_books-bot.md" in world.files("failed/desk-bot")


def test_position_row_4_test_packet_in_prod_directory(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(env="test"))
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "env_mismatch")]
    assert f"{CHAT_ID}.md" in world.files("failed/desk-bot")


def test_position_row_5_prod_packet_in_test_directory(world: World) -> None:
    world.drop("inbox-test/desk-bot", build_packet())
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "env_mismatch")]
    assert f"{CHAT_ID}.md" in world.files("failed/desk-bot")
    assert read_error(world, f"failed/desk-bot/{CHAT_ID}.error.txt")["inbox"] == "inbox-test/desk-bot"


def test_position_row_6_test_source_in_prod_directory(world: World) -> None:
    world.drop("inbox/trial-bot", build_packet("trial-bot"))
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "env_mismatch")]
    assert "2026-11-08_2330_trial-bot.md" in world.files("failed/trial-bot")


@pytest.mark.parametrize("root", ["inbox", "inbox-test"])
def test_position_row_7_inbox_root(world: World, root: str) -> None:
    (world.data / root).mkdir()
    (world.data / root / f"{CHAT_ID}.md").write_text(build_packet(), encoding="utf-8")
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "unknown_source")]
    assert f"{CHAT_ID}.md" in world.files("failed/_unknown")
    error = read_error(world, f"failed/_unknown/{CHAT_ID}.error.txt")
    assert error["inbox"] == root


@pytest.mark.parametrize("root", ["inbox", "inbox-test"])
def test_position_row_8_unregistered_directory(world: World, root: str) -> None:
    world.drop(f"{root}/no-such-bot", build_packet("no-such-bot"))
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "unknown_source")]
    assert "2026-11-08_2330_no-such-bot.md" in world.files("failed/_unknown")
    assert world.files(f"{root}/no-such-bot") == []


def test_position_comes_before_reading_the_file(world: World) -> None:
    """根目录和没登记目录里的坏文件报 unknown_source，不报 not_utf8。"""
    (world.data / "inbox").mkdir()
    (world.data / "inbox" / "a.md").write_bytes(b"\xff\xfe")
    (world.data / "inbox" / "no-such-bot").mkdir()
    (world.data / "inbox" / "no-such-bot" / "b.md").write_bytes(b"\xff\xfe")
    assert {o.code for o in world.intake().outcomes} == {"unknown_source"}


def test_any_file_in_the_inbox_root_is_refused(world: World) -> None:
    (world.data / "inbox").mkdir()
    (world.data / "inbox" / "notes.txt").write_text("随便什么", encoding="utf-8")
    assert world.intake().count("failed") == 1
    assert world.files("failed/_unknown") == ["notes.txt", "notes.txt.error.txt"]


def test_failed_goes_by_directory_not_by_header(world: World) -> None:
    """包头写别的来源，失败包也放在文件所在的目录，不能塞进别的来源的目录。"""
    world.drop("inbox/desk-bot", build_packet("books-bot"))
    world.intake()
    assert world.files("failed/books-bot") == []


# ---- 不收的东西 ----


def test_tmp_directory_is_not_scanned(world: World) -> None:
    temporary = world.data / "inbox" / "desk-bot" / ".tmp"
    temporary.mkdir(parents=True)
    (temporary / f"{CHAT_ID}.md").write_text(build_packet(), encoding="utf-8")
    report = world.intake()
    assert report.outcomes == []
    assert (temporary / f"{CHAT_ID}.md").exists()


def test_other_files_and_subdirectories_stay_where_they_are(world: World) -> None:
    source_dir = world.data / "inbox" / "desk-bot"
    (source_dir / "sub").mkdir(parents=True)
    (source_dir / "sub" / f"{CHAT_ID}.md").write_text(build_packet(), encoding="utf-8")
    (source_dir / "notes.txt").write_text("不是包", encoding="utf-8")
    report = world.intake()
    assert report.outcomes == []
    assert (source_dir / "notes.txt").exists()
    assert (source_dir / "sub" / f"{CHAT_ID}.md").exists()


def test_dot_directories_in_the_inbox_root_are_left_alone(world: World) -> None:
    """可能有东西正在写，里面的文件不当作投错的包。"""
    temporary = world.data / "inbox" / ".tmp"
    temporary.mkdir(parents=True)
    (temporary / "half-written.md").write_text("写了一半", encoding="utf-8")
    assert world.intake().outcomes == []
    assert (temporary / "half-written.md").exists()


def make_link(link: Path, target: Path) -> None:
    """建一个指向 target 的目录链接。Windows 上普通用户建不了符号链接，用目录联接（junction）。"""
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_linked_source_directory_is_not_followed(world: World, tmp_path: Path) -> None:
    """来源目录是个链接，指向数据目录外面：外面的文件不能被收走。"""
    outside = tmp_path / "outside"
    make_link(world.data / "inbox" / "desk-bot", outside)
    (outside / f"{CHAT_ID}.md").write_text(build_packet(), encoding="utf-8")

    report = world.intake()

    assert report.outcomes == []
    assert (outside / f"{CHAT_ID}.md").exists()
    assert world.files("archive") == []


def test_linked_directory_in_a_source_directory_stays_out(world: World, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    make_link(world.data / "inbox" / "desk-bot" / "linked", outside)
    (outside / f"{CHAT_ID}.md").write_text(build_packet(), encoding="utf-8")
    assert world.intake().outcomes == []
    assert (outside / f"{CHAT_ID}.md").exists()


def test_upper_case_suffix_is_refused_rather_than_ignored(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(), name=f"{CHAT_ID}.MD")
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "bad_header")]


# ---- revision 四种情况 ----


def test_new_revision_defaults_to_one(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    assert world.packet_row(CHAT_ID)["revision"] == 1


def test_same_revision_same_content_is_skipped_quietly(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    world.drop("inbox/desk-bot", build_packet())
    report = world.intake()
    assert [(o.result, o.code) for o in report.outcomes] == [("skipped", "same_revision")]
    assert world.files("archive/skipped") == [f"{CHAT_ID}.md"]
    assert world.files("failed") == []
    assert warnings_of(world, CHAT_ID) == []


def test_same_revision_ignores_line_endings(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    world.drop("inbox/desk-bot", build_packet().replace("\n", "\r\n"))
    world.intake()
    assert warnings_of(world, CHAT_ID) == []


def test_same_revision_different_content_is_skipped_and_warned(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：内容改了，忘了加 revision。\n"))
    report = world.intake()
    assert [(o.result, o.code) for o in report.outcomes] == [("skipped", "same_revision")]
    assert warnings_of(world, CHAT_ID) == [("same_revision_different", 0)]
    # 原来的还在，内容没变
    archived = world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md"
    assert "忘了加" not in archived.read_text(encoding="utf-8")


def test_skipped_files_get_a_number_when_the_name_is_taken(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    for _ in range(2):
        world.drop("inbox/desk-bot", build_packet())
        world.intake()
    assert world.files("archive/skipped") == [f"{CHAT_ID}.md", f"{CHAT_ID}_2.md"]


def test_larger_revision_replaces_and_keeps_the_old_one(clocked: World) -> None:
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    original = (clocked.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md").read_bytes()

    changed = build_packet(revision=2, body="## 原话\n- [10:30] 我：改过的内容。\n")
    clocked.drop("inbox/desk-bot", changed)
    report = clocked.intake()

    assert report.count("replaced") == 1
    month = clocked.data / "archive" / "2026" / "11"
    assert clocked.files("archive") == sorted([f"2026/11/{CHAT_ID}.md", f"2026/11/{CHAT_ID}.r1.md"])
    assert (month / f"{CHAT_ID}.r1.md").read_bytes() == original
    assert (month / f"{CHAT_ID}.md").read_bytes() == changed.encode("utf-8")

    old, new = clocked.packet_row(CHAT_ID, 1), clocked.packet_row(CHAT_ID, 2)
    assert (old["state"], new["state"]) == ("superseded", "current")
    assert old["archive_path"] == f"archive/2026/11/{CHAT_ID}.r1.md"
    assert new["archive_path"] == f"archive/2026/11/{CHAT_ID}.md"
    assert old["superseded_at"] == "2026-11-08T23:31:05+08:00"
    assert new["extraction"] == "queued"  # 新包重新走处理线


def test_replacing_twice_keeps_each_old_version(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    for revision in (2, 5):
        world.drop("inbox/desk-bot", build_packet(revision=revision, body=f"## 原话\n- [10:30] 我：第 {revision} 版。\n"))
        assert world.intake().count("replaced") == 1
    assert world.files("archive") == sorted(
        [f"2026/11/{CHAT_ID}.md", f"2026/11/{CHAT_ID}.r1.md", f"2026/11/{CHAT_ID}.r2.md"]
    )
    assert [row["revision"] for row in world.rows("SELECT revision FROM packets WHERE state = 'current'")] == [5]


def test_smaller_revision_is_skipped(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(revision=3))
    world.intake()
    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：晚到的旧版本。\n"))
    report = world.intake()
    assert [(o.result, o.code) for o in report.outcomes] == [("skipped", "older_revision")]
    assert world.packet_row(CHAT_ID, 3)["state"] == "current"
    assert world.packet_row(CHAT_ID, 2) is None
    assert warnings_of(world, CHAT_ID) == []


def test_replacing_leaves_frozen_facts_and_decisions_alone(world: World) -> None:
    """替换不动冻结的事实和定过的待确认（规范第 10 节）。J2.11 接上清理之后这个测试还要过。"""
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    t = "2026-11-08T23:31:05+08:00"
    world.execute(
        "INSERT INTO facts (fact_id, content, status, frozen, scope_partitions, created_at, updated_at)"
        " VALUES ('F1', '冻结的事实', 'verified', 1, '[\"business\"]', ?, ?)",
        t,
        t,
    )
    world.execute(
        "INSERT INTO evidence (fact_id, packet_id, revision, sentence_id, quote, position, created_at)"
        " VALUES ('F1', ?, 1, ?, '第一句话。', '{}', ?)",
        CHAT_ID,
        f"{CHAT_ID}#1.1.1",
        t,
    )
    world.execute(
        "INSERT INTO review_queue (kind, claims, quotes, decision, decided_at, created_at)"
        " VALUES ('frozen_conflict', '[]', '[]', '留原值', ?, ?)",
        t,
        t,
    )

    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：改了。\n"))
    assert world.intake().count("replaced") == 1

    assert world.rows("SELECT frozen FROM facts WHERE fact_id = 'F1'")[0]["frozen"] == 1
    assert world.rows("SELECT count(*) AS n FROM evidence")[0]["n"] == 1
    assert world.rows("SELECT decision FROM review_queue")[0]["decision"] == "留原值"


# ---- 撤回过的包号不再收 ----


@pytest.mark.parametrize("revision", [1, 2, 99])
def test_withdrawn_packet_id_is_not_taken_back(clocked: World, revision: int) -> None:
    """撤回本身是 J2.11 的事。这里直接改账本的状态，看收件认不认。"""
    clocked.drop("inbox/desk-bot", build_packet())
    clocked.intake()
    clocked.execute("UPDATE packets SET withdrawn_at = '2026-11-08T23:40:00+08:00' WHERE packet_id = ?", CHAT_ID)

    clocked.drop("inbox/desk-bot", build_packet(revision=revision, body="## 原话\n- [10:30] 我：又发来了。\n"))
    report = clocked.intake()

    assert [(o.result, o.code) for o in report.outcomes] == [("skipped", "withdrawn")]
    assert clocked.files("archive/skipped") == [f"{CHAT_ID}.md"]
    assert warnings_of(clocked, CHAT_ID) == [("withdrawn_resent", 0)]
    current = clocked.rows("SELECT revision, withdrawn_at FROM packets WHERE packet_id = ?", CHAT_ID)
    assert [(row["revision"], row["withdrawn_at"] is not None) for row in current] == [(1, True)]


# ---- 测试包只进测试库 ----


def test_test_packet_only_reaches_the_test_library(world: World) -> None:
    world.drop("inbox-test/desk-bot", build_packet(env="test"))
    assert world.intake().count("accepted") == 1

    assert world.rows("SELECT count(*) AS n FROM packets", library="test")[0]["n"] == 1
    assert world.rows("SELECT count(*) AS n FROM packets", library="prod")[0]["n"] == 0
    assert world.files("archive-test") == [f"2026/11/{CHAT_ID}.md"]
    assert world.files("archive") == []
    assert world.packet_row(CHAT_ID, library="test")["env"] == "test"


def test_prod_packet_only_reaches_the_prod_library(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    assert world.rows("SELECT count(*) AS n FROM packets", library="test")[0]["n"] == 0
    assert world.files("archive-test") == []


def test_test_source_can_use_the_test_directory(world: World) -> None:
    world.drop("inbox-test/trial-bot", build_packet("trial-bot", env="test"))
    assert world.intake().count("accepted") == 1
    assert world.packet_row("2026-11-08_2330_trial-bot", library="test") is not None


def test_same_packet_id_in_both_libraries_are_two_packets(world: World) -> None:
    """正式库和测试库各算各的。"""
    world.drop("inbox/desk-bot", build_packet(revision=5))
    world.drop("inbox-test/desk-bot", build_packet(env="test", revision=1, body="## 原话\n- [10:30] 我：试跑。\n"))
    assert world.intake().count("accepted") == 2
    assert world.packet_row(CHAT_ID, 5, library="prod")["state"] == "current"
    assert world.packet_row(CHAT_ID, 1, library="test")["state"] == "current"


def test_skipped_test_packet_goes_to_the_test_archive(world: World) -> None:
    for _ in range(2):
        world.drop("inbox-test/desk-bot", build_packet(env="test"))
        world.intake()
    assert world.files("archive-test/skipped") == [f"{CHAT_ID}.md"]
    assert world.files("archive") == []


# ---- 分区和敏感标记进账本 ----


def test_private_packet_gets_the_tag_and_skips_extraction(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(privacy="private"))
    world.intake()
    row = world.packet_row(CHAT_ID)
    assert json.loads(row["sensitive"]) == ["private"]
    assert row["extraction"] == "skipped"


def test_packet_with_two_partitions_and_a_tag(world: World) -> None:
    packet = build_packet("books-bot", domain="[business, finance]", sensitive="[payroll]")
    world.drop("inbox/books-bot", packet)
    world.intake()
    row = world.packet_row("2026-11-08_2330_books-bot")
    assert json.loads(row["partitions"]) == ["business", "finance"]
    assert json.loads(row["sensitive"]) == ["payroll"]


# ---- 警告 ----


def test_parse_warnings_are_recorded_with_line_numbers(world: World) -> None:
    body = (
        "## 原话\n"
        "- [10:30] 我：一句。\n"  # 第 17 行
        "- [坏] 我：解析不了。\n"
        "- [10:20] 我：时间倒退。\n"
        "没有缩进的一行\n"
        "\n"
        "## 备注\n"
        "- 认不出的标题下面\n"
    )
    world.drop("inbox/desk-bot", build_packet(body=body))
    world.intake()
    assert warnings_of(world, CHAT_ID) == [
        (BAD_LINE, 18),
        (TIME_BACKWARDS, 19),
        (UNRECOGNIZED_LINE, 20),
        (UNKNOWN_HEADING, 22),
    ]


def test_redaction_hit_warns_with_type_only_and_keeps_the_file(world: World) -> None:
    body = "## 原话\n- [10:30] 我：后台密码是 abcd1234，令牌是 efgh5678。\n"
    world.drop("inbox/desk-bot", build_packet(body=body))
    assert world.intake().count("accepted") == 1
    rows = world.rows("SELECT kind, detail FROM patrol_warnings ORDER BY detail")
    assert sorted((row["kind"], row["detail"]) for row in rows) == [("redaction_hit", "令牌"), ("redaction_hit", "密码")]
    dump = json.dumps([dict(row) for row in world.rows("SELECT * FROM patrol_warnings")], ensure_ascii=False)
    assert "abcd1234" not in dump and "efgh5678" not in dump
    archived = world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md"
    assert "abcd1234" in archived.read_text(encoding="utf-8")  # 不改文件


def test_redaction_hit_in_a_rejected_packet_warns_too(world: World) -> None:
    """明文密钥留在 failed 里也要提醒，只报文件名和类型。"""
    body = "## 原话\n- [2026-11-01 10:42] 我：后台密码是 abcd1234。\n"
    world.drop("inbox/desk-bot", build_packet(body=body))
    world.intake()
    rows = world.rows("SELECT kind, packet_id, detail FROM patrol_warnings")
    assert [(row["kind"], row["packet_id"], row["detail"]) for row in rows] == [("redaction_hit", CHAT_ID, "密码")]


def test_redaction_hit_in_a_skipped_packet_warns_too(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(revision=3))
    world.intake()
    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：后台密码是 abcd1234。\n"))
    world.intake()
    rows = world.rows("SELECT kind, revision, detail FROM patrol_warnings")
    assert [(row["kind"], row["revision"], row["detail"]) for row in rows] == [("redaction_hit", 2, "密码")]


def test_files_rejected_by_position_are_not_read_for_redaction(world: World) -> None:
    """放错位置的不读内容。"""
    (world.data / "inbox").mkdir()
    (world.data / "inbox" / "x.md").write_text("后台密码是 abcd1234", encoding="utf-8")
    world.intake()
    assert world.rows("SELECT count(*) AS n FROM patrol_warnings")[0]["n"] == 0


def test_unknown_field_warning_reaches_the_ledger(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(sensitve="[payroll]"))
    world.intake()
    rows = world.rows("SELECT kind, line, detail FROM patrol_warnings WHERE packet_id = ?", CHAT_ID)
    assert [(row["kind"], row["line"], row["detail"]) for row in rows] == [("unknown_field", 14, "sensitve")]


def test_warnings_of_a_replaced_revision_stop_nagging(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：一句。\n- [坏] 坏的一行\n"))
    world.intake()
    assert warnings_of(world, CHAT_ID) != []
    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：修好了。\n"))
    world.intake()
    assert warnings_of(world, CHAT_ID) == []


def test_same_warning_again_counts_up_instead_of_adding_a_row(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    for number in range(2):
        world.drop("inbox/desk-bot", build_packet(body=f"## 原话\n- [10:30] 我：第 {number} 次改。\n"))
        world.intake()
    rows = world.rows("SELECT seen_count FROM patrol_warnings")
    assert [row["seen_count"] for row in rows] == [2]


# ---- 配置出错一个文件都不动 ----


def test_no_registry_moves_nothing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    path = world.drop("inbox/desk-bot", build_packet())
    from dataclasses import replace

    with pytest.raises(ConfigError):
        run_intake(replace(world.settings, sources_file=None))
    assert path.exists()
    assert world.files("failed") == []


@pytest.mark.parametrize("text", ["[不合法", "sources: {}\n"])
def test_broken_registry_moves_nothing(world: World, text: str) -> None:
    path = world.drop("inbox/desk-bot", build_packet())
    world.settings.sources_file.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        world.intake()
    assert path.exists()
    assert world.files("failed") == []


def test_no_data_dir_is_a_config_error() -> None:
    from javmem.config import Settings

    with pytest.raises(ConfigError):
        run_intake(Settings.from_env({}))


# ---- 同一时间只有一个收件 ----


def test_second_intake_is_refused_while_one_is_running(world: World) -> None:
    with intake_lock(world.data):
        with pytest.raises(IntakeBusy):
            world.intake()
    world.intake()  # 放开之后又能跑


def run_module(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "javmem", *argv],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )


def test_lock_works_across_processes(world: World) -> None:
    path = world.drop("inbox/desk-bot", build_packet())
    with intake_lock(world.data):
        result = run_module("intake")
    assert result.returncode == 3, result.stderr
    assert path.exists()  # 没动

    assert run_module("intake").returncode == 0
    assert not path.exists()


def test_lock_is_released_when_the_process_dies(world: World) -> None:
    """进程死了不会把收件卡住，不用人去删锁文件。"""
    script = (
        "import os, sys, pathlib\n"
        "from javmem.intake import intake_lock\n"
        "lock = intake_lock(pathlib.Path(sys.argv[1]))\n"
        "lock.__enter__()\n"
        "os._exit(9)\n"
    )
    died = subprocess.run([sys.executable, "-c", script, str(world.data)], check=False)
    assert died.returncode == 9
    assert (world.data / ".intake.lock").exists()

    world.drop("inbox/desk-bot", build_packet())
    assert world.intake().count("accepted") == 1


# ---- 一个文件挪不动不影响别的 ----


def test_file_that_cannot_be_moved_stays_and_is_retried(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.intake as intake

    real = os.replace
    busy = {"on": True}

    def flaky(source: object, target: object) -> None:
        if busy["on"] and "2026-11-07" in str(source):
            raise PermissionError("被别的程序占着")
        real(source, target)

    world.drop("inbox/desk-bot", build_packet(stamp="2026-11-07_2330"))
    world.drop("inbox/desk-bot", build_packet())
    monkeypatch.setattr(intake.os, "replace", flaky)
    report = world.intake()
    assert report.count("accepted") == 1
    assert [(o.result, o.code) for o in report.outcomes if o.result == "error"] == [("error", "PermissionError")]
    assert "2026-11-07_2330_desk-bot.md" in world.files("inbox/desk-bot")

    # 这个包登记了还没做完。占着的时候再扫也不撞登记
    report = world.intake()
    assert report.count("accepted") == 0
    assert "2026-11-07_2330_desk-bot.md" in world.files("inbox/desk-bot")

    busy["on"] = False
    report = world.intake()
    assert report.count("recovered") == 1
    assert world.files("inbox/desk-bot") == []
    assert world.packet_row("2026-11-07_2330_desk-bot")["state"] == "current"


def test_unexpected_error_on_one_packet_does_not_stop_the_others(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """写库时才出的怪问题：这个包留下次，别的包照收，也不会每次都崩。"""
    real = intake_module._LibraryIntake._register

    def explode_on_one(self, path: Path, packet) -> None:
        if packet.header.packet_id.startswith("2026-11-07"):
            raise OverflowError("写库时溢出")
        real(self, path, packet)

    world.drop("inbox/desk-bot", build_packet(stamp="2026-11-07_2330"))
    world.drop("inbox/desk-bot", build_packet())
    monkeypatch.setattr(intake_module._LibraryIntake, "_register", explode_on_one)

    report = world.intake()

    assert report.count("accepted") == 1
    assert [(o.result, o.code) for o in report.outcomes if o.result == "error"] == [("error", "OverflowError")]
    assert "2026-11-07_2330_desk-bot.md" in world.files("inbox/desk-bot")


def test_ledger_trouble_stops_the_run_instead_of_blaming_each_file(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def locked(self, path: Path, packet) -> None:
        raise sqlite3.OperationalError("database is locked")

    path = world.drop("inbox/desk-bot", build_packet())
    monkeypatch.setattr(intake_module._LibraryIntake, "_register", locked)
    with pytest.raises(sqlite3.OperationalError):
        world.intake()
    assert path.exists()
    assert world.files("failed") == []


def test_huge_revision_goes_to_failed_instead_of_crashing_every_run(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(revision=str(2**63)))
    world.drop("inbox/desk-bot", build_packet(stamp="2026-11-07_2330"))
    report = world.intake()
    assert report.count("failed") == 1
    assert report.count("accepted") == 1
    assert world.intake().outcomes == []  # 下一次也不崩


def test_paths_with_spaces_and_chinese_are_fine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from helpers import SOURCES_YAML

    from javmem.config import Settings

    data = tmp_path / "数据 目录"
    sources = tmp_path / "来源 登记.yaml"
    sources.write_text(SOURCES_YAML, encoding="utf-8")
    settings = Settings.from_env({"JAVMEM_DATA_DIR": str(data), "JAVMEM_SOURCES_FILE": str(sources)})
    spaced = World(data.resolve(), settings)
    spaced.drop("inbox/desk-bot", build_packet())
    assert spaced.intake().count("accepted") == 1


def test_mail_packet_with_bad_mailbox_goes_to_failed(world: World) -> None:
    world.drop("inbox/mail-sales-a", build_mail_packet(mailbox="someone.else@example.com"))
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("failed", "bad_header")]
    assert world.files("failed/mail-sales-a")


def test_a_message_id_that_happens_to_contain_akia_is_not_a_key(world: World) -> None:
    """J3.2：邮件服务器生成的编号里碰巧有一段 AKia…，收件时报了 23 条“打码模式表命中：密钥”（D91）。"""
    from helpers import build_mail_packet, mail_header_block

    message_id = '"<2026.' + "AKia" + 'Zz9Qw1Er2Ty3Ui4O@mail.example.com>"'
    block = mail_header_block(message_id=message_id, thread_id=message_id)
    world.drop("inbox/mail-sales-a", build_mail_packet(sections=f"## 原话 · 邮件 · 2026-11-03 09:12\n\n{block}\n  正文。\n"))
    assert world.intake().count("accepted") == 1
    assert world.rows("SELECT kind, detail FROM patrol_warnings WHERE kind = 'redaction_hit'") == []
