"""邮件导入重跑（D88）：先认 Message-ID，导过的信不读附件的正文。样例都是编的。

以前每次导入都把输入里的每一封信连同附件读一遍，再按状态跳过导过的：重跑一年的邮箱，导过的信的 PDF
也要一个一个起子进程再读。编号（没有 Message-ID 时按内容生成的也算）和内容指纹都用不着附件的正文，
所以先读邮件头和正文，状态里有的、这一批里重复的到这里就跳过，附件不读。

别的都照旧，下面一条一条守着：生成编号的算法；“同一个 Message-ID、内容不一样”；没导过的信、上次没投出去的信
照旧读附件；状态文件的格式；读 PDF 的子进程起不来整批停（D79 第 11 条），只是要读 PDF 的时候才碰得到它。
"""

from __future__ import annotations

import json
import os
import tracemalloc
import weakref
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from pdfgen import make_pdf
from test_pdf_child import fake_pdfminer

import javmem.mailimport as mailimport
from javmem import attachments as reader
from javmem.attachments import PdfUnavailable
from javmem.cli import main
from javmem.mailimport import (
    NOTE_GENERATED_ID,
    NOTE_ID_CLASH,
    NOTE_PDF_SKIPPED,
    ImportReport,
    import_mail,
    load_mail_config,
    parse_mail,
)
from javmem.packet import Rejected, parse_packet
from javmem.packet.model import REASON_TIMEOUT, TEXT_NONE
from javmem.redaction import default_patterns
from javmem.sources import load_registry

FIXTURES = Path(__file__).parent / "fixtures" / "mail"
SOURCE = "mail-sales-a"
ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=ZONE)
PATTERNS = default_patterns()
SENDER = "Lena Ortiz <lena.ortiz@example.net>"

PLAN = make_pdf(pages=[["Crate plan, page one."], ["Spare lids go on page two."]])
LABELS = make_pdf(["Label size 10 x 15 cm.", "Water based ink only."])
PALLETS = make_pdf(["Pallet count 48."])

# 改以前（423a11a）的代码导完 mailbox() 写的状态文件。编号、指纹、往来串的算法和状态文件的格式，哪样变了这里都对不上
OLD_STATE = {
    "covers": {},
    "env": "test",
    "format": 2,
    "mailbox": "li.ming@example.com",
    "messages": {
        "<crate-0001@example.net>": {
            "digest": "874e72a86791f257", "packet": "2026-11-02_1020_mail-sales-a", "thread": "<crate-0001@example.net>"
        },
        "<crate-0002@example.net>": {
            "digest": "3ebeb08c5c4894e4", "packet": "2026-11-02_1020_mail-sales-a", "thread": "<crate-0001@example.net>"
        },
        "<crate-0004@example.net>": {
            "digest": "af22e1d1c9ee309c", "packet": "2026-11-04_1050_mail-sales-a", "thread": "<crate-0004@example.net>"
        },
        "<crate-0005@example.net>": {
            "digest": "b985f3a88a29d92d", "packet": "2026-11-05_1055_mail-sales-a", "thread": "<crate-0005@example.net>"
        },
        "<generated-7669239d4a073806fde50097eff8ea02@javmem.invalid>": {
            "digest": "230c308a57503f46",
            "packet": "2026-11-03_1035_mail-sales-a",
            "thread": "<generated-7669239d4a073806fde50097eff8ea02@javmem.invalid>",
        },
    },
    "packets": [
        "2026-11-02_1020_mail-sales-a",
        "2026-11-03_1035_mail-sales-a",
        "2026-11-04_1050_mail-sales-a",
        "2026-11-05_1055_mail-sales-a",
    ],
    "source": "mail-sales-a",
    "threads": {
        "<crate-0001@example.net>": {
            "last": "2026-11-02T02:20:00+00:00", "people": ["d8246bd28419ee55"], "sensitive": [], "subject": "c6a38dc6037c1c66"
        },
        "<crate-0004@example.net>": {
            "last": "2026-11-04T02:50:00+00:00", "people": ["d8246bd28419ee55"], "sensitive": [], "subject": "a0f75d8073af39cf"
        },
        "<crate-0005@example.net>": {
            "last": "2026-11-05T02:55:00+00:00", "people": ["d8246bd28419ee55"], "sensitive": [], "subject": "51d2361f4faea3bc"
        },
        "<generated-7669239d4a073806fde50097eff8ea02@javmem.invalid>": {
            "last": "2026-11-03T02:35:00+00:00", "people": ["d8246bd28419ee55"], "sensitive": [], "subject": "2a4ac7804f53dd74"
        },
    },
}
# 现在的代码写的：只多了一个 digest_version（指纹是哪一版规矩算的，D93 审阅第 9 条），别的和改以前一个字节都不差
NEW_STATE = {**OLD_STATE, "digest_version": mailimport.DIGEST_VERSION}


def state_text(data: dict) -> str:
    """状态文件的写法（State.save）：键排好序，不缩进。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def letter(
    folder: Path,
    name: str,
    *,
    when: str,
    subject: str,
    body: str,
    message_id: str | None,
    files: list[tuple[str, str, bytes]] | None = None,
    reply_to: str | None = None,
    sender: str = SENDER,
    to: str = "li.ming@example.com",
    extra: list[tuple[str, str]] | None = None,
) -> Path:
    message = EmailMessage()
    for key, value in extra or []:  # Received、Cc 这些
        message[key] = value
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = when
    if message_id is not None:
        message["Message-ID"] = message_id
    if reply_to is not None:
        message["In-Reply-To"] = reply_to
        message["References"] = reply_to
    message.set_content(body)
    for filename, media_type, data in files or []:
        maintype, subtype = media_type.split("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(bytes(message))
    return path


def mailbox(folder: Path) -> Path:
    """编的邮箱：五封信分在四天（四个包），7 个附件，5 个是 PDF。第 3 封没有 Message-ID。"""
    letter(folder, "01.eml", when="Mon, 02 Nov 2026 10:05:00 +0800", subject="Crates for the spring batch",
           body="Hi Ming,\n\nThe plan is attached.\n", message_id="<crate-0001@example.net>",
           files=[("plan.pdf", "application/pdf", PLAN), ("notes.txt", "text/plain", b"Blue lids only.\n")])
    letter(folder, "02.eml", when="Mon, 02 Nov 2026 10:20:00 +0800", subject="Re: Crates for the spring batch",
           body="Labels too.\n", message_id="<crate-0002@example.net>", reply_to="<crate-0001@example.net>",
           files=[("labels.pdf", "application/pdf", LABELS)])
    letter(folder, "03.eml", when="Tue, 03 Nov 2026 10:35:00 +0800", subject="Pallets", body="Count below.\n",
           message_id=None, files=[("pallets.pdf", "application/pdf", PALLETS)])
    letter(folder, "04.eml", when="Wed, 04 Nov 2026 10:50:00 +0800", subject="Two sheets", body="Both sheets.\n",
           message_id="<crate-0004@example.net>",
           files=[("a.pdf", "application/pdf", PALLETS), ("b.pdf", "application/pdf", LABELS)])
    letter(folder, "05.eml", when="Thu, 05 Nov 2026 10:55:00 +0800", subject="Terms", body="See the terms.\n",
           message_id="<crate-0005@example.net>", files=[("terms.html", "text/html", b"<p>Net 30 days.</p>")])
    return folder


def run(tmp_path: Path, inputs: list[Path], *, now: datetime = NOW) -> ImportReport:
    registry = load_registry(FIXTURES / "sources.yaml")
    return import_mail(
        inputs,
        source=SOURCE,
        registry=registry,
        config=load_mail_config(None, registry, SOURCE),
        patterns=PATTERNS,
        out_dir=tmp_path / "out",
        work_dir=tmp_path / "work",
        zone=ZONE,
        now=now,
    )


def state_path(tmp_path: Path) -> Path:
    return tmp_path / "work" / "state" / f"{SOURCE}.test.json"


def delivered(tmp_path: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted((tmp_path / "out" / SOURCE).glob("*.md"))}


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """这次读了几个附件的正文、起了几次读 PDF 的子进程。"""
    counts = {"attachments": 0, "pdf": 0}
    real_read, real_run = mailimport.read_attachment, reader.run_child  # 起读附件的子进程都走 run_child（U3）

    def counting_read(*args: object, **kwargs: object) -> reader.AttachmentText:
        counts["attachments"] += 1
        return real_read(*args, **kwargs)  # type: ignore[arg-type]

    def counting_run(*args: object, **kwargs: object) -> object:
        counts["pdf"] += 1
        return real_run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(mailimport, "read_attachment", counting_read)
    monkeypatch.setattr(reader, "run_child", counting_run)
    return counts


def taken(counts: dict[str, int]) -> tuple[int, int]:
    """读了几个附件、几个 PDF，数完清零。"""
    found = (counts["attachments"], counts["pdf"])
    counts.update(attachments=0, pdf=0)
    return found


# ---------- 导过的信不读附件 ----------


def test_a_second_run_reads_no_attachment_and_changes_nothing(tmp_path: Path, reads: dict[str, int]) -> None:
    inbox = mailbox(tmp_path / "in")
    first = run(tmp_path, [inbox])
    assert taken(reads) == (7, 5)
    assert (first.ok, first.imported, len(first.packets)) == (True, 5, 4)
    assert first.attachments == {"full": 7}
    assert first.notes == {NOTE_GENERATED_ID: 1}
    packets, state = delivered(tmp_path), state_path(tmp_path).read_bytes()

    second = run(tmp_path, [inbox], now=NOW + timedelta(hours=1))
    assert taken(reads) == (0, 0)
    expected = {
        "source": SOURCE,
        "started_at": "2026-11-10T10:00:00+08:00",
        "env": "test",
        "kind": "backfill",
        "seen": 5,
        "imported": 0,
        "already_imported": 5,
        "duplicates": 0,
        "resent": [],
        "bad": [],
        "notes": {},
        "redactions": {},
        "attachments": {},
        "packets": [],
        "rejected": [],
        "ok": True,
    }
    assert second.to_dict() == expected
    # 报告文件逐字照旧，键的先后也是
    assert Path(second.report_path).read_text(encoding="utf-8") == json.dumps(expected, ensure_ascii=False, indent=1)
    assert delivered(tmp_path) == packets  # 第二遍一个包都没投
    assert state_path(tmp_path).read_bytes() == state  # 状态文件的内容也没变


def test_only_the_new_mail_reads_its_attachments(tmp_path: Path, reads: dict[str, int]) -> None:
    inbox = mailbox(tmp_path / "in")
    run(tmp_path, [inbox])
    taken(reads)
    letter(inbox, "06.eml", when="Fri, 06 Nov 2026 09:10:00 +0800", subject="Re: Pallets", body="Two more.\n",
           message_id="<crate-0006@example.net>", files=[("pallets-2.pdf", "application/pdf", PALLETS)])
    second = run(tmp_path, [inbox], now=NOW + timedelta(hours=1))
    assert taken(reads) == (1, 1)
    assert (second.imported, second.already_imported) == (1, 5)
    assert second.attachments == {"full": 1}
    [packet] = second.packets
    path = tmp_path / "out" / str(packet["path"])
    [body] = parse_packet(path.read_bytes(), filename=path.name).attachments
    assert (body.name, body.text) == ("pallets-2.pdf", "full")


def test_a_generated_message_id_is_made_without_reading_attachments(tmp_path: Path, reads: dict[str, int]) -> None:
    """没有 Message-ID 的信按原样的邮件头、附件名单（名字、类型、大小）和正文生成编号，用不着附件的正文。
    编号的算法没改：值和改以前一样。"""
    inbox = mailbox(tmp_path / "in")
    expected = "<generated-7669239d4a073806fde50097eff8ea02@javmem.invalid>"  # 改以前的代码算的
    full = parse_mail("x", (inbox / "03.eml").read_bytes(), now=NOW, patterns=PATTERNS)
    assert full.message_id == expected
    taken(reads)
    run(tmp_path, [inbox])
    assert expected in json.loads(state_path(tmp_path).read_text(encoding="utf-8"))["messages"]
    taken(reads)
    second = run(tmp_path, [inbox / "03.eml"], now=NOW + timedelta(hours=1))
    assert taken(reads) == (0, 0)
    assert (second.imported, second.already_imported, second.notes) == (0, 1, {})


def test_a_generated_message_id_still_counts_cc_and_the_top_received(tmp_path: Path) -> None:
    """生成编号照旧算原样的 Cc 和最上面一条 Received（审阅第 4 条）：值和改以前一样。"""
    path = letter(
        tmp_path / "in", "a.eml", when="Tue, 03 Nov 2026 10:36:00 +0800", subject="Pallets, second count",
        body="Count below, again.\n", message_id=None, files=[("pallets.pdf", "application/pdf", PALLETS)],
        extra=[
            ("Received", "from relay.example.net by mx.example.com; Tue, 03 Nov 2026 10:36:10 +0800"),
            ("Received", "from desk.example.net by relay.example.net; Tue, 03 Nov 2026 10:36:02 +0800"),
            ("Cc", "Bo Han <bo.han@example.net>"),
        ],
    )
    mail = parse_mail("x", path.read_bytes(), now=NOW, patterns=PATTERNS)
    assert mail.message_id == "<generated-d783cceaa634d2a9691c8f9f87466c6f@javmem.invalid>"  # 改以前的代码算的


# ---------- 同一个 Message-ID、内容不一样 ----------


def test_a_changed_mail_under_an_imported_id_is_still_flagged_without_reading(tmp_path: Path, reads: dict[str, int]) -> None:
    run(tmp_path, [mailbox(tmp_path / "in")])
    taken(reads)
    state = state_path(tmp_path).read_bytes()
    changed = letter(tmp_path / "later", "02b.eml", when="Mon, 02 Nov 2026 10:20:00 +0800",
                     subject="Re: Crates for the spring batch", body="Labels and tape.\n",
                     message_id="<crate-0002@example.net>", files=[("labels-v2.pdf", "application/pdf", PALLETS)])
    second = run(tmp_path, [changed], now=NOW + timedelta(hours=1))
    assert taken(reads) == (0, 0)  # 指纹是发件人、时间、主题、正文，用不着附件的正文
    assert (second.imported, second.already_imported, second.packets) == (0, 1, [])
    assert second.notes == {NOTE_ID_CLASH: 1}
    assert state_path(tmp_path).read_bytes() == state


def test_copies_in_one_batch_read_attachments_once_and_the_first_one_wins(tmp_path: Path, reads: dict[str, int]) -> None:
    folder = tmp_path / "twins"
    head = {"when": "Mon, 09 Nov 2026 08:30:00 +0800", "subject": "Crate count", "message_id": "<twin-0001@example.net>"}
    letter(folder, "a.eml", **head, body="Forty crates.\n", files=[("count.pdf", "application/pdf", PALLETS)])
    (folder / "b.eml").write_bytes((folder / "a.eml").read_bytes())  # 一模一样的一份
    letter(folder, "c.eml", **head, body="Fifty crates.\n", files=[("count.pdf", "application/pdf", LABELS)])
    report = run(tmp_path, [folder])
    assert taken(reads) == (1, 1)  # 只读了先来的那封
    assert (report.imported, report.duplicates) == (1, 2)
    assert report.notes == {NOTE_ID_CLASH: 1}
    [text] = [path.read_text(encoding="utf-8") for path in (tmp_path / "out" / SOURCE).glob("*.md")]
    assert "Forty crates." in text and "Fifty crates." not in text
    assert "Pallet count 48." in text and "Label size" not in text


# ---------- 没导过的、上次没投出去的照旧读 ----------


def test_mails_left_by_a_reader_that_could_not_start_are_read_next_time(
    tmp_path: Path, reads: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = mailbox(tmp_path / "in")
    with monkeypatch.context() as broken:
        broken.syspath_prepend(fake_pdfminer(tmp_path / "site", broken=True))
        with pytest.raises(PdfUnavailable):
            run(tmp_path, [inbox])
    assert delivered(tmp_path) == {} and not state_path(tmp_path).exists()
    taken(reads)
    again = run(tmp_path, [inbox], now=NOW + timedelta(hours=1))
    assert taken(reads) == (7, 5)
    assert (again.ok, again.imported, again.already_imported, len(again.packets)) == (True, 5, 0, 4)


def test_mails_whose_packet_was_not_delivered_are_read_next_time(
    tmp_path: Path, reads: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = mailbox(tmp_path / "in")
    real = mailimport.deliver
    calls: list[str] = []

    def flaky(directory: Path, name: str, content: str) -> Path:
        calls.append(name)
        if len(calls) == 2:
            raise OSError("磁盘满了")
        return real(directory, name, content)

    monkeypatch.setattr(mailimport, "deliver", flaky)
    with pytest.raises(OSError):
        run(tmp_path, [inbox])
    monkeypatch.setattr(mailimport, "deliver", real)
    assert list(delivered(tmp_path)) == ["2026-11-02_1020_mail-sales-a.md"]  # 第 1、2 封投出去了
    taken(reads)
    again = run(tmp_path, [inbox], now=NOW + timedelta(hours=1))
    assert taken(reads) == (4, 3)  # 第 3、4、5 封：4 个附件，3 个 PDF
    assert (again.imported, again.already_imported, len(again.packets)) == (3, 2, 3)
    assert again.attachments == {"full": 4}


def test_mails_whose_packet_failed_validation_are_read_next_time(
    tmp_path: Path, reads: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = mailbox(tmp_path / "in")
    real = mailimport.validate_packet

    def picky(data: bytes, *, filename: str, **options: object) -> object:
        if filename.startswith("2026-11-04"):
            raise Rejected("bad_header", "编的")
        return real(data, filename=filename, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(mailimport, "validate_packet", picky)
    first = run(tmp_path, [inbox])
    monkeypatch.setattr(mailimport, "validate_packet", real)
    assert [item["packet_id"] for item in first.rejected] == ["2026-11-04_1050_mail-sales-a"]
    assert first.imported == 4
    taken(reads)
    again = run(tmp_path, [inbox], now=NOW + timedelta(hours=1))
    assert taken(reads) == (2, 2)  # 第 4 封的两个 PDF
    assert (again.imported, again.already_imported) == (1, 4)
    assert [item["packet_id"] for item in again.packets] == ["2026-11-04_1050_mail-sales-a"]


def test_an_unreadable_new_mail_is_still_bad_and_the_rest_go_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = mailimport.read_attachment

    def failing(data: bytes, **options: object) -> reader.AttachmentText:
        if options["name"] == "labels.pdf":
            raise ValueError("编的")
        return real(data, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(mailimport, "read_attachment", failing)
    report = run(tmp_path, [mailbox(tmp_path / "in")])
    assert [(Path(item["location"]).name, item["reason"]) for item in report.bad] == [("02.eml", "unreadable")]
    assert report.imported == 4
    assert "<crate-0002@example.net>" not in json.loads(state_path(tmp_path).read_text(encoding="utf-8"))["messages"]


def test_a_copy_after_an_unreadable_mail_is_still_imported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """读坏了的那封不算“这一批里已经有了”：后面同一封的副本照导，和以前一样。"""
    folder = tmp_path / "in"
    letter(folder, "a.eml", when="Mon, 09 Nov 2026 08:30:00 +0800", subject="Crate count", body="Forty crates.\n",
           message_id="<twin-0002@example.net>", files=[("count.pdf", "application/pdf", PALLETS)])
    (folder / "b.eml").write_bytes((folder / "a.eml").read_bytes())
    real, calls = mailimport.read_attachment, []

    def once(data: bytes, **options: object) -> reader.AttachmentText:
        calls.append(options["name"])
        if len(calls) == 1:
            raise ValueError("编的")
        return real(data, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(mailimport, "read_attachment", once)
    report = run(tmp_path, [folder])
    assert [(Path(item["location"]).name, item["reason"]) for item in report.bad] == [("a.eml", "unreadable")]
    assert (report.imported, report.duplicates) == (1, 0)


# ---------- 打码的数照旧 ----------


def test_redaction_counts_keep_their_order_in_the_report(tmp_path: Path) -> None:
    """先读邮件头和正文、后读附件，打码的数还是按原来的先后记：主题、附件的正文、正文。报告里类型的先后不变。"""
    folder = tmp_path / "in"
    letter(folder, "a.eml", when="Mon, 09 Nov 2026 09:00:00 +0800", subject="Portal login, password: Tz8kq2",
           body="Use the key sk-test0000000000000000 for the upload.\n", message_id="<keys-0001@example.net>",
           files=[("steps.txt", "text/plain", b"The verification code 305718 expires soon.\n")])
    report = run(tmp_path, [folder])
    assert list(report.redactions.items()) == [("密码", 1), ("验证码", 1), ("密钥", 1)]
    text = Path(report.report_path).read_text(encoding="utf-8")
    assert text.index('"密码"') < text.index('"验证码"') < text.index('"密钥"')


def test_redaction_counts_of_one_mail_go_names_attachment_text_attachment_name_body(tmp_path: Path) -> None:
    """一封信里四处各一种类型（审阅第 3 条）：显示名、附件的正文、附件名、正文，先后照旧。"""
    path = letter(
        tmp_path / "in", "a.eml", when="Mon, 09 Nov 2026 09:00:00 +0800", subject="Access notes",
        body="Use password: Tz8kq2 to log in.\n", message_id="<keys-0002@example.net>",
        sender="Ops ghp_" + "a" * 24 + " <ops@example.net>",
        files=[("sk-test1111111111111111.txt", "text/plain", b"The verification code 305718 expires soon.\n")],
    )
    mail = parse_mail("x", path.read_bytes(), now=NOW, patterns=PATTERNS)
    assert list(mail.redactions.items()) == [("令牌", 1), ("验证码", 1), ("密钥", 1), ("密码", 1)]
    assert mail.sender.name == "Ops [已隐去:令牌]"
    assert [item.name for item in mail.attachments] == ["[已隐去:密钥].txt"]


def test_redaction_counts_go_sender_name_recipient_names_subject_body(tmp_path: Path) -> None:
    """邮件头里的先后（D89，复查第 2 条）：发件人的显示名、收件人的显示名、主题，最后是正文。四处各一种类型。"""
    path = letter(
        tmp_path / "in", "a.eml", when="Mon, 09 Nov 2026 09:30:00 +0800", subject="Portal login, password: Qw7rt9",
        body="The verification code 418265 is inside.\n", message_id="<keys-0003@example.net>",
        sender="Ops ghp_" + "b" * 24 + " <ops@example.net>", to="Key sk-test2222222222222222 <keys@example.net>",
    )
    mail = parse_mail("x", path.read_bytes(), now=NOW, patterns=PATTERNS)
    assert list(mail.redactions.items()) == [("令牌", 1), ("密钥", 1), ("密码", 1), ("验证码", 1)]
    assert [item.name for item in mail.to] == ["Key [已隐去:密钥]"]


def test_the_skipped_pdf_note_stays_after_the_other_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pdf_skipped_after_timeout 照旧排在这封信别的 notes 后面（审阅第 3 条）。读 PDF 换成每个都到点的假的。"""

    def timing_out(data: bytes, **options: object) -> reader.AttachmentText:
        budget = options["budget"]
        if budget.timed_out:  # 和 read_attachment 一样：前面有到点的，这个不读
            budget.skipped += 1
        budget.timed_out = True
        return reader.AttachmentText(TEXT_NONE, REASON_TIMEOUT)

    monkeypatch.setattr(mailimport, "read_attachment", timing_out)
    path = letter(tmp_path / "in", "a.eml", when="Mon, 09 Nov 2026 10:00:00 +0800", subject="Two scans",
                  body="Both attached.\n", message_id=None,
                  files=[("a.pdf", "application/pdf", PALLETS), ("b.pdf", "application/pdf", LABELS)])
    mail = parse_mail("x", path.read_bytes(), now=NOW, patterns=PATTERNS)
    assert mail.notes == [NOTE_GENERATED_ID, NOTE_PDF_SKIPPED]


# ---------- 附件的字节只留到这封信读完 ----------


def test_attachment_bytes_are_let_go_once_their_mail_is_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """读下一封、投包的时候，前面的信的 MailHead 和它的附件（_RawAttachment，拿着字节）都已经放掉了
    （审阅第 1 条、复查第 1 条）。"""
    watched: list[weakref.ref] = []
    alive: list[int] = []
    real_head, real_deliver = mailimport.read_head, mailimport.deliver

    def watching(*args: object, **kwargs: object) -> object:
        alive.append(sum(ref() is not None for ref in watched))
        head = real_head(*args, **kwargs)  # type: ignore[arg-type]
        if isinstance(head, mailimport.MailHead):
            watched.append(weakref.ref(head))
            watched.extend(weakref.ref(item) for item in head.attachments)
        return head

    def delivering(*args: object, **kwargs: object) -> object:
        alive.append(sum(ref() is not None for ref in watched))
        return real_deliver(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mailimport, "read_head", watching)
    monkeypatch.setattr(mailimport, "deliver", delivering)
    inbox = mailbox(tmp_path / "in")
    run(tmp_path, [inbox])
    run(tmp_path, [inbox], now=NOW + timedelta(hours=1))  # 重跑：全是导过的
    assert len(watched) == 10 + 14  # 两遍各 5 封、7 个附件
    assert alive == [0] * 14  # 读 10 封、投 4 个包：每次前面的信都已经放掉了


def test_decoded_attachment_bytes_do_not_pile_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """附件解出来的字节，不管放在哪儿，都不留到下一封（D89，复查第 1 条）。四封信各带一张 4 MiB 的图
    （图不读，不起子进程）。读下一封、投包的时候，比开始时多出来的内存要小于一封信原样的大小加半张图：
    还拿着上一封的字节，就会多出一整张。"""
    size = 4 * 1024 * 1024
    folder = tmp_path / "in"
    for number in range(4):
        letter(folder, f"{number}.eml", when=f"Mon, 09 Nov 2026 1{number}:00:00 +0800", subject=f"Photo {number}",
               body="Photo attached.\n", message_id=f"<photo-{number:04d}@example.net>",
               files=[("photo.png", "image/png", os.urandom(size))])
    largest = max(path.stat().st_size for path in folder.iterdir())  # 一封信原样（base64 以后）的大小
    over: list[int] = []
    real_head, real_deliver = mailimport.read_head, mailimport.deliver

    def watching(*args: object, **kwargs: object) -> object:
        over.append(tracemalloc.get_traced_memory()[0] - start)
        return real_head(*args, **kwargs)  # type: ignore[arg-type]

    def delivering(*args: object, **kwargs: object) -> object:
        over.append(tracemalloc.get_traced_memory()[0] - start)
        return real_deliver(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mailimport, "read_head", watching)
    monkeypatch.setattr(mailimport, "deliver", delivering)
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    try:
        start = tracemalloc.get_traced_memory()[0]
        report = run(tmp_path, [folder])
    finally:
        if not tracing:
            tracemalloc.stop()
    assert report.attachments == {"none:image": 4}
    assert len(over) == 5  # 读 4 封、投 1 个包
    assert max(over) < largest + size // 2, (over, largest)


# ---------- 状态文件 ----------


def test_the_state_file_keeps_its_format_and_older_entries_still_work(tmp_path: Path, reads: dict[str, int]) -> None:
    inbox = mailbox(tmp_path / "in")
    run(tmp_path, [inbox])
    text = state_path(tmp_path).read_text(encoding="utf-8")
    assert text == state_text(NEW_STATE)
    data = json.loads(text)
    for entry in data["messages"].values():  # 没记指纹的老条目：不比指纹，照样认得出导过
        del entry["digest"]
    state_path(tmp_path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    taken(reads)
    second = run(tmp_path, [inbox], now=NOW + timedelta(hours=1))
    assert taken(reads) == (0, 0)
    assert (second.imported, second.already_imported, second.notes) == (0, 5, {})


def test_a_state_file_written_by_the_old_code_is_used_as_it_is(tmp_path: Path, reads: dict[str, int]) -> None:
    """改以前的代码写的状态文件（指纹是它算的）：改以后接着导，一封都不重导，也不误报内容不一样（审阅第 2 条）。"""
    inbox = mailbox(tmp_path / "in")
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(state_text(OLD_STATE), encoding="utf-8")
    second = run(tmp_path, [inbox])
    assert taken(reads) == (0, 0)
    assert (second.imported, second.already_imported, second.notes, second.packets) == (0, 5, {}, [])
    assert state_path(tmp_path).read_text(encoding="utf-8") == state_text(OLD_STATE)  # 一个包都没投出去，状态文件原样


# ---------- 读 PDF 的子进程起不来：要读 PDF 时整批停 ----------


def test_a_reader_that_cannot_start_only_stops_a_batch_that_needs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inbox = mailbox(tmp_path / "in")
    run(tmp_path, [inbox])
    packets, state = delivered(tmp_path), state_path(tmp_path).read_bytes()
    monkeypatch.setenv("JAVMEM_SOURCES_FILE", str(FIXTURES / "sources.yaml"))
    monkeypatch.syspath_prepend(fake_pdfminer(tmp_path / "site", broken=True))

    def command(*inputs: Path) -> int:
        return main(["mail-import", *map(str, inputs), "--source", SOURCE, "--work-dir", str(tmp_path / "work"),
                     "--out", str(tmp_path / "out")])

    assert command(inbox) == 0  # 都导过：一个 PDF 都不用读，用不着子进程
    assert "以前导过 5 封" in capsys.readouterr().out
    news = letter(tmp_path / "news", "07.eml", when="Sat, 07 Nov 2026 11:00:00 +0800", subject="New quote",
                  body="Quote attached.\n", message_id="<crate-0007@example.net>",
                  files=[("quote.pdf", "application/pdf", PALLETS)])
    assert command(inbox, news) == 2  # 有一封要读 PDF：整批停，什么都不写
    assert "读 PDF" in "".join(capsys.readouterr())
    assert delivered(tmp_path) == packets and state_path(tmp_path).read_bytes() == state
    plain = letter(tmp_path / "plain", "08.eml", when="Sat, 07 Nov 2026 12:00:00 +0800", subject="No PDF",
                   body="Nothing to read.\n", message_id="<crate-0008@example.net>",
                   files=[("memo.txt", "text/plain", b"Ship on Monday.\n")])
    assert command(inbox, plain) == 0  # 新信不带 PDF：照导
    assert "导入 1 封，以前导过 5 封" in capsys.readouterr().out
