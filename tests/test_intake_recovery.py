"""中断了不丢包（packet-spec 第 8 节）：每一步断了，下次启动对账都接得上。

用一个假的“断电”异常打断收件，再跑一遍，看结果和没断过一样。
"""

from __future__ import annotations

import os
import stat

import pytest
from helpers import CHAT_ID, World, build_packet, warnings_of

import javmem.intake as intake
from javmem.intake import _LibraryIntake


class PowerCut(BaseException):
    """模拟断电。用 BaseException，不会被收件里的 except Exception 接住。"""


def cut_before(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    def crash(*args: object, **kwargs: object) -> None:
        raise PowerCut

    monkeypatch.setattr(_LibraryIntake, method, crash)


def crash_once(world: World, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    """在某一步之前断一次电。"""
    with monkeypatch.context() as patch:
        cut_before(patch, method)
        with pytest.raises(PowerCut):
            world.intake()


V2 = build_packet(revision=2, body="## 原话\n- [10:30] 我：第二版。\n")


def test_cut_after_registering_before_archiving(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.drop("inbox/desk-bot", build_packet())
    crash_once(world, monkeypatch, "_archive")

    assert world.packet_row(CHAT_ID)["state"] == "registered"
    assert world.files("inbox/desk-bot") == [f"{CHAT_ID}.md"]
    assert world.files("archive") == []

    report = world.intake()

    assert report.count("recovered") == 1
    assert world.packet_row(CHAT_ID)["state"] == "current"
    assert world.files("inbox/desk-bot") == []
    assert world.files("archive") == [f"2026/11/{CHAT_ID}.md"]
    assert world.files("failed") == []


def test_cut_after_archiving_before_finishing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：一句。\n- [坏] 坏的一行\n"))
    crash_once(world, monkeypatch, "_finish")

    assert world.packet_row(CHAT_ID)["state"] == "registered"
    assert world.files("inbox/desk-bot") == []
    assert world.files("archive") == [f"2026/11/{CHAT_ID}.md"]
    assert warnings_of(world, CHAT_ID) == []

    report = world.intake()

    assert report.count("recovered") == 1
    assert world.packet_row(CHAT_ID)["state"] == "current"
    assert warnings_of(world, CHAT_ID) == [("bad_line", 18)]  # 收尾的事都补上了


def test_cut_while_finishing_rolls_back(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """收尾是一个事务：记了一半警告就断电，什么都不留。"""
    real = intake.add_warning
    calls = {"n": 0}

    def crash_on_second(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PowerCut
        real(*args, **kwargs)

    body = "## 原话\n- [10:30] 我：一句。\n- [坏] 坏的一行\n- [又坏] 又一行\n"
    world.drop("inbox/desk-bot", build_packet(body=body))
    with monkeypatch.context() as patch:
        patch.setattr(intake, "add_warning", crash_on_second)
        with pytest.raises(PowerCut):
            world.intake()

    assert world.packet_row(CHAT_ID)["state"] == "registered"
    assert warnings_of(world, CHAT_ID) == []
    world.intake()
    assert [kind for kind, _ in warnings_of(world, CHAT_ID)] == ["bad_line", "bad_line"]


def recover_replacement(world: World) -> None:
    report = world.intake()
    assert report.count("recovered") == 1
    assert world.packet_row(CHAT_ID, 1)["state"] == "superseded"
    assert world.packet_row(CHAT_ID, 1)["archive_path"].endswith(f"{CHAT_ID}.r1.md")
    assert world.packet_row(CHAT_ID, 2)["state"] == "current"
    assert world.files("archive") == sorted([f"2026/11/{CHAT_ID}.md", f"2026/11/{CHAT_ID}.r1.md"])
    assert "第二版" in (world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md").read_text(encoding="utf-8")
    assert "第一句话" in (world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.r1.md").read_text(encoding="utf-8")
    assert world.files("inbox/desk-bot") == []


def test_replacement_cut_before_archiving(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    world.drop("inbox/desk-bot", V2)
    crash_once(world, monkeypatch, "_archive")

    # 旧版本还是现行的
    assert world.packet_row(CHAT_ID, 1)["state"] == "current"
    assert world.packet_row(CHAT_ID, 2)["state"] == "registered"

    recover_replacement(world)


def test_cut_after_archiving_before_clearing_the_inbox(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """归档到位了，收件目录那份还没删。下次把多的那份清掉，不用再走一遍跳过。"""
    world.drop("inbox/desk-bot", build_packet())
    crash_once(world, monkeypatch, "_clear_inbox")

    assert world.files("inbox/desk-bot") == [f"{CHAT_ID}.md"]
    assert world.files("archive") == [f"2026/11/{CHAT_ID}.md"]

    report = world.intake()

    assert report.count("recovered") == 1
    assert world.files("inbox/desk-bot") == []
    assert world.files("archive") == [f"2026/11/{CHAT_ID}.md"]
    assert world.files("archive/skipped") == []
    assert world.packet_row(CHAT_ID)["state"] == "current"


@pytest.mark.parametrize("method", ["_clear_inbox", "_finish"])
def test_archive_is_read_only_whenever_the_cut_comes(world: World, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    world.drop("inbox/desk-bot", build_packet())
    crash_once(world, monkeypatch, method)
    archived = world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md"
    assert not os.stat(archived).st_mode & stat.S_IWRITE
    world.intake()
    assert not os.stat(archived).st_mode & stat.S_IWRITE


def test_archive_reaches_the_disk_before_the_inbox_copy_is_removed(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """真断电时不能两边都没有：先落盘，再改名到位，最后才删收件目录那份。"""
    path = world.drop("inbox/desk-bot", build_packet())
    calls = []
    real_fsync, real_replace, real_unlink = os.fsync, os.replace, os.unlink

    def fsync(descriptor: int) -> None:
        calls.append("fsync")
        real_fsync(descriptor)

    def replace(source: object, target: object) -> None:
        if str(source).endswith(".incoming"):
            calls.append("replace")
        real_replace(source, target)

    def unlink(target: object) -> None:
        if str(target) == str(path):
            calls.append("unlink")
        real_unlink(target)

    monkeypatch.setattr(intake.os, "fsync", fsync)
    monkeypatch.setattr(intake.os, "replace", replace)
    monkeypatch.setattr(intake.os, "unlink", unlink)
    assert world.intake().count("accepted") == 1

    assert calls.index("fsync") < calls.index("replace") < calls.index("unlink")


def test_half_written_archive_copy_is_cleaned_up(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """断在归档的临时文件改名之前：临时文件不碍事，下次重写。"""
    real = intake.os.replace
    world.drop("inbox/desk-bot", build_packet())
    with monkeypatch.context() as patch:

        def cut_on_rename(source: object, target: object) -> None:
            if str(source).endswith(".incoming"):
                raise PowerCut
            real(source, target)

        patch.setattr(intake.os, "replace", cut_on_rename)
        with pytest.raises(PowerCut):
            world.intake()

    assert world.files("archive") == [f"2026/11/.{CHAT_ID}.md.incoming"]
    assert world.files("inbox/desk-bot") == [f"{CHAT_ID}.md"]

    assert world.intake().count("recovered") == 1
    assert world.files("archive") == [f"2026/11/{CHAT_ID}.md"]
    assert world.files("inbox/desk-bot") == []


def test_inbox_file_changed_after_archiving_is_left_for_the_next_round(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归档之后、删收件目录那份之前，打包方又发了一份不同的：不能把新发的这份删了。"""
    real = _LibraryIntake._write_archive
    changed = build_packet(revision=2, body="## 原话\n- [10:30] 我：刚又发了一份。\n")

    def resend_right_after(self: _LibraryIntake, dest: object, raw: bytes) -> None:
        real(self, dest, raw)
        world.drop("inbox/desk-bot", changed)

    world.drop("inbox/desk-bot", build_packet())
    with monkeypatch.context() as patch:
        patch.setattr(_LibraryIntake, "_write_archive", resend_right_after)
        assert world.intake().count("accepted") == 1

    assert world.files("inbox/desk-bot") == [f"{CHAT_ID}.md"]
    assert world.intake().count("replaced") == 1
    assert world.packet_row(CHAT_ID, 2)["state"] == "current"


def test_replacement_cut_after_moving_the_old_file(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    world.drop("inbox/desk-bot", V2)
    crash_once(world, monkeypatch, "_write_archive")

    assert world.files("archive") == [f"2026/11/{CHAT_ID}.r1.md"]
    assert world.files("inbox/desk-bot") == [f"{CHAT_ID}.md"]

    recover_replacement(world)


def test_replacement_cut_before_finishing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    world.drop("inbox/desk-bot", V2)
    crash_once(world, monkeypatch, "_finish")

    assert world.files("archive") == sorted([f"2026/11/{CHAT_ID}.md", f"2026/11/{CHAT_ID}.r1.md"])
    assert world.packet_row(CHAT_ID, 1)["state"] == "current"

    recover_replacement(world)


def test_file_gone_after_registering_voids_the_registration(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    path = world.drop("inbox/desk-bot", build_packet())
    crash_once(world, monkeypatch, "_archive")
    path.unlink()

    report = world.intake()

    assert [(o.result, o.code) for o in report.outcomes] == [("error", "registration_lost")]
    assert world.packet_row(CHAT_ID) is None
    assert warnings_of(world, CHAT_ID) == [("registration_lost", 0)]

    # 这次不算收过，重投能收
    world.drop("inbox/desk-bot", build_packet())
    assert world.intake().count("accepted") == 1


def test_file_overwritten_after_registering_goes_through_the_normal_door(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断电之后打包方又发了一份不同的内容：旧登记作废，新文件正常走一遍。"""
    world.drop("inbox/desk-bot", build_packet())
    crash_once(world, monkeypatch, "_archive")
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：重发的内容。\n"))

    report = world.intake()

    assert report.count("accepted") == 1
    row = world.packet_row(CHAT_ID)
    assert row["state"] == "current"
    assert "重发的内容" in (world.data / row["archive_path"]).read_text(encoding="utf-8")


def test_replacement_file_gone_puts_the_old_one_back(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    path = world.drop("inbox/desk-bot", V2)
    crash_once(world, monkeypatch, "_write_archive")
    path.unlink()

    report = world.intake()

    assert [(o.result, o.code) for o in report.outcomes] == [("error", "registration_lost")]
    assert world.packet_row(CHAT_ID, 2) is None
    assert world.packet_row(CHAT_ID, 1)["state"] == "current"
    assert world.files("archive") == [f"2026/11/{CHAT_ID}.md"]


def test_archive_file_the_ledger_does_not_know_is_kept_aside(world: World) -> None:
    """账本丢过：归档里有同名的文件，内容不一样。不覆盖，挪开留着。"""
    month = world.data / "archive" / "2026" / "11"
    month.mkdir(parents=True)
    (month / f"{CHAT_ID}.md").write_text("账本不认识的内容", encoding="utf-8")

    world.drop("inbox/desk-bot", build_packet())
    assert world.intake().count("accepted") == 1

    assert world.files("archive") == sorted([f"2026/11/{CHAT_ID}.conflict.md", f"2026/11/{CHAT_ID}.md"])
    assert (month / f"{CHAT_ID}.conflict.md").read_text(encoding="utf-8") == "账本不认识的内容"
    assert warnings_of(world, CHAT_ID) == [("archive_conflict", 0)]


def test_archive_file_with_the_same_content_is_reused(world: World) -> None:
    """账本丢过，同样的包又来了。归档里已经有了，收件目录这份直接清掉。"""
    month = world.data / "archive" / "2026" / "11"
    month.mkdir(parents=True)
    (month / f"{CHAT_ID}.md").write_text(build_packet(), encoding="utf-8")

    world.drop("inbox/desk-bot", build_packet())
    assert world.intake().count("accepted") == 1
    assert world.packet_row(CHAT_ID)["state"] == "current"
    assert world.files("inbox/desk-bot") == []
    assert world.files("archive/skipped") == []
