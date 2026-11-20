"""内嵌图不进附件名单（D91 第 8 项，规范第 6 节的 inline_images）。信都是编的。

内嵌图：image/*，有 Content-ID，Content-Disposition 不是 attachment（签名里的小图、贴进正文的截图）。
名单后面只记一句“另有内嵌图 N 张”，不写附件块；真的附件照记名字、类型、大小。
"""

from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import pytest
from test_mail_rerun import PALLETS, SENDER, SOURCE, run

from javmem.mailimport import parse_mail, read_head
from javmem.packet import Rejected, parse_packet
from javmem.packet.model import MailSection
from javmem.raw.sentences import iter_sentences
from javmem.redaction import default_patterns

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2  # 编的字节，图片不读
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 3


def html_mail(
    folder: Path,
    name: str,
    *,
    inline: int,
    real: list[tuple[str, str, bytes, str | None]] | None = None,
    message_id: str | None = "<inline-0001@example.net>",
) -> Path:
    """一封 HTML 信：正文里引用 inline 张内嵌图（multipart/related），后面跟着 real 里的附件（名字、类型、字节、Content-ID）。"""
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = "li.ming@example.com"
    message["Subject"] = "Crate photos"
    message["Date"] = "Tue, 10 Nov 2026 09:00:00 +0800"
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content("Hi Ming,\n\nThe crates are below.\n")
    pictures = "".join(f'<img src="cid:logo{number}@example.net">' for number in range(1, inline + 1))
    message.add_alternative(f"<p>Hi Ming,</p><p>The crates are below.</p>{pictures}", subtype="html")
    html = message.get_payload()[1]
    for number in range(1, inline + 1):
        html.add_related(PNG, "image", "png", cid=f"<logo{number}@example.net>", filename=f"image00{number}.png",
                         disposition="inline")  # 签名小图常带 inline；email 包见了文件名就默认当附件
    for filename, media_type, data, cid in real or []:
        maintype, subtype = media_type.split("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename, cid=cid)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(bytes(message))
    return path


def only_packet(tmp_path: Path) -> tuple[bytes, object]:
    [path] = (tmp_path / "out" / SOURCE).glob("*.md")
    data = path.read_bytes()
    return data, parse_packet(data, filename=path.name)


def test_inline_images_leave_the_list_and_only_their_count_is_kept(tmp_path: Path) -> None:
    real = [("count.pdf", "application/pdf", PALLETS, None), ("crate.jpg", "image/jpeg", JPEG, "<photo-1@example.net>")]
    html_mail(tmp_path / "in", "a.eml", inline=3, real=real)
    report = run(tmp_path, [tmp_path / "in"])
    assert report.attachments == {"full": 1, "none:image": 1}  # 内嵌图不算附件
    data, packet = only_packet(tmp_path)
    text = data.decode("utf-8")
    assert "image001.png" not in text and "\ninline_images: 3\n" in text
    [section] = [item for item in packet.sections if isinstance(item, MailSection)]
    assert (section.headers.attachments, section.headers.inline_images) == (("count.pdf", "crate.jpg"), 3)
    assert [body.name for body in packet.attachments] == ["count.pdf", "crate.jpg"]  # 内嵌图也不写附件块
    [line] = [sentence.text for sentence in iter_sentences(packet) if sentence.field == "attachments"]
    assert line == "count.pdf, crate.jpg, 另有内嵌图 3 张"


def test_a_mail_with_only_inline_images_lists_nothing_but_the_count(tmp_path: Path) -> None:
    html_mail(tmp_path / "in", "a.eml", inline=1)
    run(tmp_path, [tmp_path / "in"])
    data, packet = only_packet(tmp_path)
    assert "\nattachments: []\ninline_images: 1\n" in data.decode("utf-8")
    assert packet.attachments == []
    [line] = [sentence.text for sentence in iter_sentences(packet) if sentence.field == "attachments"]
    assert line == "另有内嵌图 1 张"


def test_images_shown_in_the_body_without_a_content_id_are_real_attachments(tmp_path: Path) -> None:
    path = html_mail(tmp_path / "in", "a.eml", inline=0)
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = "li.ming@example.com"
    message["Subject"] = "Crate photo"
    message["Date"] = "Tue, 10 Nov 2026 09:00:00 +0800"
    message["Message-ID"] = "<inline-0002@example.net>"
    message.set_content("Photo of the crate.\n")
    message.add_attachment(JPEG, maintype="image", subtype="jpeg", filename="crate.jpg", disposition="inline")
    path.write_bytes(bytes(message))
    run(tmp_path, [tmp_path / "in"])
    data, packet = only_packet(tmp_path)
    [section] = [item for item in packet.sections if isinstance(item, MailSection)]
    assert (section.headers.attachments, section.headers.inline_images) == (("crate.jpg",), 0)
    assert b"inline_images" not in data  # 没有内嵌图的不写这个字段


def test_generated_ids_still_count_the_inline_images(tmp_path: Path) -> None:
    """没有 Message-ID 的信按内容生成编号，附件那一份算着内嵌图（D91 以前就是这样）：以前导过的信编号不变。"""
    path = html_mail(tmp_path, "a.eml", inline=2, message_id=None)
    head = read_head(str(path), path.read_bytes(), now=datetime.fromisoformat("2026-11-12T09:00:00+08:00"),
                     patterns=default_patterns())
    assert head.mail.message_id == GENERATED


@pytest.mark.parametrize("value", ["-1", '"2"', "true", "1.5"])
def test_a_wrong_inline_count_is_rejected(tmp_path: Path, value: str) -> None:
    html_mail(tmp_path / "in", "a.eml", inline=2)
    run(tmp_path, [tmp_path / "in"])
    [path] = (tmp_path / "out" / SOURCE).glob("*.md")
    data = path.read_bytes().replace(b"\ninline_images: 2\n", f"\ninline_images: {value}\n".encode())
    with pytest.raises(Rejected, match="inline_images"):
        parse_packet(data, filename=path.name)


def mixed_mail(folder: Path, parts: list[tuple[str, str, str | None, str | None]], *, html: str | None, plain: str) -> Path:
    """一封信：纯文字 plain，html 不是 None 时再带一份 HTML；parts 是（文件名、类型、Content-ID、Content-Disposition）。"""
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = "li.ming@example.com"
    message["Subject"] = "Crate photos"
    message["Date"] = "Tue, 10 Nov 2026 09:00:00 +0800"
    message["Message-ID"] = "<inline-0003@example.net>"
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")
    for filename, media_type, cid, disposition in parts:
        maintype, subtype = media_type.split("/")
        message.add_attachment(PNG, maintype=maintype, subtype=subtype, filename=filename, cid=cid, disposition=disposition)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "a.eml"
    path.write_bytes(bytes(message))
    return path


def headers_of(tmp_path: Path) -> tuple[tuple[str, ...], int]:
    _, packet = only_packet(tmp_path)
    [section] = [item for item in packet.sections if isinstance(item, MailSection)]
    return section.headers.attachments, section.headers.inline_images


def test_only_images_the_body_refers_to_are_inline(tmp_path: Path) -> None:
    """审阅第 21 条：正文里用 cid: 引用着的才算内嵌图，写着 attachment 的也算；没被引用的、扫描件那种整封一张图的是真附件；
    写成 application/octet-stream、名字是图片的也认。"""
    parts = [
        ("logo.png", "image/png", "<logo@example.net>", "attachment"),  # 正文引用着它，标的却是附件
        ("stamp.png", "application/octet-stream", "<stamp@example.net>", "inline"),  # 类型说不清，名字是图片
        ("photo.png", "image/png", "<photo@example.net>", "inline"),  # 没被引用
    ]
    html = '<p>Hi Ming,</p><img src="cid:logo@example.net"><img src="CID:stamp%40example.net">'
    mixed_mail(tmp_path / "in", parts, html=html, plain="Hi Ming,\n")
    run(tmp_path, [tmp_path / "in"])
    assert headers_of(tmp_path) == (("photo.png",), 2)


def test_plain_text_placeholders_count_as_references(tmp_path: Path) -> None:
    """只有纯文字正文、里面写着 [cid:…] 占位的（转了一道只剩纯文字的 Outlook 信），那张图也算内嵌图。"""
    mixed_mail(tmp_path / "in", [("image001.png", "image/png", "<image001.png@01DC0000.00000000>", "inline")],
               html=None, plain="Regards,\n[cid:image001.png@01DC0000.00000000]\n")
    run(tmp_path, [tmp_path / "in"])
    assert headers_of(tmp_path) == ((), 1)


def test_a_mail_that_is_just_one_scanned_image_keeps_its_name(tmp_path: Path) -> None:
    mixed_mail(tmp_path / "in", [("scan0001.png", "image/png", "<scan0001@example.net>", "inline")], html=None, plain="")
    run(tmp_path, [tmp_path / "in"])
    assert headers_of(tmp_path) == (("scan0001.png",), 0)


# ---------- D93 第 21 条：只认当正文用的那几份里的引用 ----------

RAW_HEAD = "From: Lena Ortiz <lena.ortiz@example.net>\r\nTo: li.ming@example.com\r\nSubject: Crates\r\nDate: Tue, 10 Nov 2026 09:00:00 +0800\r\nMessage-ID: <refs-0001@example.net>\r\n"
PNG64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="  # 编的一张 1×1 的图


def raw_image(name: str, cid: str, disposition: str = "inline") -> str:
    return (f'Content-Type: image/png; name="{name}"\r\nContent-ID: {cid}\r\nContent-Disposition: {disposition}; filename="{name}"\r\n'
            f"Content-Transfer-Encoding: base64\r\n\r\n{PNG64}\r\n")


def raw_part(media_type: str, body: str, disposition: str | None = None) -> str:
    extra = f"Content-Disposition: {disposition}\r\n" if disposition else ""
    return f"Content-Type: {media_type}; charset=utf-8\r\n{extra}\r\n{body}\r\n"


def raw_multipart(kind: str, *parts: str, boundary: str = "b1") -> str:
    return f'Content-Type: multipart/{kind}; boundary="{boundary}"\r\n\r\n' + "".join(f"--{boundary}\r\n{part}" for part in parts) + f"--{boundary}--\r\n"


def names_and_count(body: str) -> tuple[list[str], int]:
    mail = parse_mail("x", (RAW_HEAD + body).encode(), now=datetime.fromisoformat("2026-11-12T09:00:00+08:00"), patterns=default_patterns())
    return [item.name for item in mail.attachments], mail.inline_images


def test_references_in_an_html_attachment_or_a_forwarded_mail_do_not_count() -> None:
    attached_page = raw_multipart(
        "mixed", raw_part("text/plain", "See the attached page."),
        raw_part("text/html", '<img src="cid:pic@example.net">', 'attachment; filename="page.html"'),
        raw_image("pic.png", "<pic@example.net>", "attachment"),
    )
    assert names_and_count(attached_page) == (["page.html", "pic.png"], 0)
    forwarded = raw_multipart(
        "mixed", raw_part("text/plain", "Forwarding the old one."),
        'Content-Type: message/rfc822\r\nContent-Disposition: attachment; filename="old.eml"\r\n\r\n'
        "From: a@example.net\r\nSubject: old\r\nDate: Mon, 09 Nov 2026 09:00:00 +0800\r\n"
        + raw_multipart("related", raw_part("text/html", '<img src="cid:same@example.net">'), boundary="in"),
        raw_image("photo.png", "<same@example.net>", "attachment"),
    )
    assert names_and_count(forwarded) == (["old.eml", "photo.png"], 0)


def test_references_in_a_recovered_html_part_count() -> None:
    """说是 multipart 却缺 boundary，按正文里的分隔线重新切开：切出来的 HTML 引用着的图是内嵌图。"""
    broken = ("Content-Type: multipart/related\r\n\r\n--zz\r\n" + raw_part("text/html", '<p>Crates.</p><img src="cid:r@example.net">')
              + "--zz\r\n" + raw_image("r.png", "<r@example.net>") + "--zz--\r\n")
    assert names_and_count(broken) == ([], 1)


GENERATED = "<generated-44e57ce3ea6f79308f844e17119ed421@javmem.invalid>"  # D91 以前的代码算出来的
