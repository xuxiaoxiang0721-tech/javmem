"""同一个编号改了以后重发的信（D91 第 7 项）：同一批里留 Date 晚的那封，没导的记进报告的 resent。

跨批的（先导了旧版，后来的一批又来了新版）：导过的跳过，附件不读（D88），新版进不来；记 duplicate_id_different_content，
resent 里记下新版的位置、Date 和以前导进的是哪个包（D93）。
信都是编的。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_mail_rerun import LABELS, NOW, PALLETS, PLAN, SOURCE, letter, reads, run, taken  # noqa: F401 reads 是 fixture

import javmem.mailimport as mailimport
from javmem.cli import _print_mail_import
from javmem.mailimport import BAD_UNREADABLE, NOTE_ID_CLASH, BadMail

ID = "<resent-0001@example.net>"


def version(folder: Path, name: str, when: str, body: str, pdf: bytes) -> Path:
    return letter(folder, name, when=f"Mon, 09 Nov 2026 {when}:00 +0800", subject="Crate count", body=body,
                  message_id=ID, files=[("count.pdf", "application/pdf", pdf)])


def packet_text(tmp_path: Path) -> str:
    [text] = [path.read_text(encoding="utf-8") for path in (tmp_path / "out" / SOURCE).glob("*.md")]
    return text


def dropped(location: Path, when: str, kept: Path, kept_when: str) -> dict[str, str]:
    return {"location": str(location), "date": f"2026-11-09T{when}:00+08:00", "kept": str(kept),
            "kept_date": f"2026-11-09T{kept_when}:00+08:00"}


def test_a_resent_copy_with_a_later_date_replaces_the_first(tmp_path: Path, reads: dict[str, int]) -> None:
    folder = tmp_path / "in"
    first = version(folder, "a.eml", "08:30", "Forty crates.\n", PALLETS)
    second = version(folder, "b.eml", "09:10", "Fifty crates, not forty.\n", LABELS)
    report = run(tmp_path, [folder])
    assert taken(reads) == (2, 2)  # 读先来的那封时还不知道后面有新版
    assert (report.imported, report.duplicates, report.notes) == (1, 1, {NOTE_ID_CLASH: 1})
    assert report.resent == [dropped(first, "08:30", second, "09:10")]
    text = packet_text(tmp_path)
    assert "Fifty crates, not forty." in text and "Forty crates." not in text
    assert "Label size" in text and "Pallet count 48." not in text
    assert report.to_dict()["resent"] == report.resent


def test_an_older_copy_that_comes_later_is_dropped_without_reading_it(tmp_path: Path, reads: dict[str, int]) -> None:
    folder = tmp_path / "in"
    newer = version(folder, "a.eml", "09:10", "Fifty crates, not forty.\n", LABELS)
    older = version(folder, "b.eml", "08:30", "Forty crates.\n", PALLETS)
    report = run(tmp_path, [folder])
    assert taken(reads) == (1, 1)
    assert report.resent == [dropped(older, "08:30", newer, "09:10")]
    assert "Fifty crates, not forty." in packet_text(tmp_path)


def test_the_latest_of_several_versions_wins_and_a_tie_keeps_the_one_that_came_first(tmp_path: Path) -> None:
    folder = tmp_path / "in"
    one = version(folder, "1.eml", "08:30", "One crate.\n", PALLETS)
    three = version(folder, "2.eml", "09:50", "Three crates.\n", LABELS)
    two = version(folder, "3.eml", "09:20", "Two crates.\n", PALLETS)
    again = version(folder, "4.eml", "09:50", "Three crates, said again.\n", PLAN)
    same = folder / "5.eml"
    same.write_bytes(three.read_bytes())  # 一模一样的一份：只是重复，不记 resent
    report = run(tmp_path, [folder])
    assert (report.imported, report.duplicates, report.notes) == (1, 4, {NOTE_ID_CLASH: 3})  # 一封不一样的记一次
    assert report.resent == [
        dropped(one, "08:30", three, "09:50"),
        dropped(two, "09:20", three, "09:50"),
        dropped(again, "09:50", three, "09:50"),
    ]
    text = packet_text(tmp_path)
    assert "Three crates." in text and "said again" not in text and "One crate." not in text


def test_a_newer_version_in_a_later_batch_is_skipped_but_named(tmp_path: Path, reads: dict[str, int], capsys: pytest.CaptureFixture[str]) -> None:
    """跨批的（D93 第 28 条）：照旧跳过、附件不读（D88），新版进不来；resent 里记下这一封的位置、Date，以前导进了哪个包。"""
    version(tmp_path / "first", "a.eml", "08:30", "Forty crates.\n", PALLETS)
    first = run(tmp_path, [tmp_path / "first"])
    before = packet_text(tmp_path)
    taken(reads)
    later = version(tmp_path / "later", "b.eml", "09:10", "Fifty crates, not forty.\n", LABELS)
    same = tmp_path / "later" / "c.eml"
    same.write_bytes((tmp_path / "first" / "a.eml").read_bytes())  # 和导过的一模一样的一份：只是导过了，不记
    second = run(tmp_path, [tmp_path / "later"], now=NOW + timedelta(hours=1))
    assert taken(reads) == (0, 0)  # D88：导过的编号不读附件
    assert (second.imported, second.already_imported, second.notes) == (0, 2, {NOTE_ID_CLASH: 1})
    packet = first.packets[0]["packet_id"]
    assert second.resent == [{"location": str(later), "date": "2026-11-09T09:10:00+08:00", "kept_packet": packet}]
    assert packet_text(tmp_path) == before
    _print_mail_import(second)
    assert f"同一个编号改了以后重发，没导 {later}（2026-11-09T09:10:00+08:00）：以前导过，在包 {packet} 里" in capsys.readouterr().out


def test_a_newer_copy_that_cannot_be_read_leaves_the_older_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "in"
    version(folder, "a.eml", "08:30", "Forty crates.\n", PALLETS)
    broken = version(folder, "b.eml", "09:10", "Fifty crates, not forty.\n", LABELS)
    real = mailimport.read_attachments

    def failing(head: mailimport.MailHead) -> mailimport.Mail | BadMail:
        if head.mail.location == str(broken):
            return BadMail(head.mail.location, BAD_UNREADABLE)
        return real(head)

    monkeypatch.setattr(mailimport, "read_attachments", failing)
    report = run(tmp_path, [folder])
    assert report.bad == [{"location": str(broken), "reason": BAD_UNREADABLE}]
    assert (report.imported, report.resent) == (1, [])
    assert "Forty crates." in packet_text(tmp_path)


def test_the_command_line_names_the_dropped_copy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "in"
    first = version(folder, "a.eml", "08:30", "Forty crates.\n", PALLETS)
    second = version(folder, "b.eml", "09:10", "Fifty crates, not forty.\n", LABELS)
    _print_mail_import(run(tmp_path, [folder]))
    assert f"改了以后重发，没导 {first}（2026-11-09T08:30:00+08:00），导的是 {second}" in capsys.readouterr().out


def test_every_dropped_copy_names_the_copy_that_was_kept_in_the_end(tmp_path: Path) -> None:
    """审阅第 10 条：三份依次更晚，第一份换下来时写的“导的是第二份”，第二份也被换下来以后要改成第三份。"""
    folder = tmp_path / "in"
    first = version(folder, "1.eml", "08:30", "One crate.\n", PALLETS)
    second = version(folder, "2.eml", "09:10", "Two crates.\n", LABELS)
    third = version(folder, "3.eml", "09:50", "Three crates.\n", PLAN)
    report = run(tmp_path, [folder])
    assert report.resent == [dropped(first, "08:30", third, "09:50"), dropped(second, "09:10", third, "09:50")]


def test_a_cross_batch_entry_does_not_break_a_later_replacement(tmp_path: Path) -> None:
    """D93 审阅第 1 条：resent 里有跨批的那条（没有 kept），同一批里又有更晚的一封换掉先来的：以前 KeyError，整批停下。"""
    version(tmp_path / "first", "a.eml", "08:30", "Forty crates.\n", PALLETS)
    run(tmp_path, [tmp_path / "first"])
    later = tmp_path / "later"
    version(later, "1.eml", "09:10", "Fifty crates, not forty.\n", LABELS)
    letter(later, "2.eml", when="Mon, 09 Nov 2026 09:20:00 +0800", subject="Lids", body="Blue lids.\n", message_id="<lids-0001@example.net>")
    letter(later, "3.eml", when="Mon, 09 Nov 2026 09:40:00 +0800", subject="Lids", body="Green lids.\n", message_id="<lids-0001@example.net>")
    report = run(tmp_path, [later], now=NOW + timedelta(hours=1))
    assert report.imported == 1 and len(report.resent) == 2
    assert [("kept_packet" in item, "kept" in item) for item in report.resent] == [(True, False), (False, True)]


def test_an_upgrade_does_not_flag_every_mail_whose_text_rules_changed(tmp_path: Path) -> None:
    """D93 审阅第 9 条：状态是旧版本的代码写的（转文字、打码的规矩变了，指纹跟着变）：原样重导不报改了重发。
    旧版本的指纹只扔掉、不补记：升级以前导过的信，后来改了重发的认不出来；升级以后导的照样认得出。"""
    version(tmp_path / "in", "a.eml", "08:30", "Forty crates.\n", PALLETS)
    run(tmp_path, [tmp_path / "in"])
    path = tmp_path / "work" / "state" / f"{SOURCE}.test.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("digest_version", None)  # 旧版本写的状态：没有这个字段，指纹是旧的规矩算的
    state["messages"][ID]["digest"] = "0" * 16
    path.write_text(json.dumps(state), encoding="utf-8")
    again = run(tmp_path, [tmp_path / "in"], now=NOW + timedelta(hours=1))
    assert (again.already_imported, again.notes, again.resent) == (1, {}, [])
    later = tmp_path / "later"
    version(later, "b.eml", "09:10", "Fifty crates, not forty.\n", PALLETS)  # 升级以前导过的那封改了重发：认不出来，不报
    letter(later, "c.eml", when="Mon, 09 Nov 2026 09:20:00 +0800", subject="Lids", body="Blue lids.\n",
           message_id="<lids-0001@example.net>")
    third = run(tmp_path, [later], now=NOW + timedelta(hours=2))
    assert (third.imported, third.already_imported, third.resent) == (1, 1, []) and NOTE_ID_CLASH not in third.notes
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["digest_version"] == mailimport.DIGEST_VERSION and "digest" not in saved["messages"][ID]
    changed = tmp_path / "changed"
    letter(changed, "d.eml", when="Mon, 09 Nov 2026 09:40:00 +0800", subject="Lids", body="Green lids.\n",
           message_id="<lids-0001@example.net>")
    fourth = run(tmp_path, [changed], now=NOW + timedelta(hours=3))
    assert (fourth.notes.get(NOTE_ID_CLASH), len(fourth.resent)) == (1, 1)  # 升级以后导的，改了重发照样认得出
