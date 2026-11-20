"""D72 审阅（2262bd3）提的，每一条先写成测试、看着红了再修。样例都是编的，模型和向量用假的。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from helpers import MAIL_ID, MAIL_PACKET, World, build_packet
from pdfgen import make_fanout_pdf
from test_attachments import (
    CHAT_ID,
    FILE_PACKET,
    PATTERNS,
    block,
    convert,
    owner_search,
    parsed,
    pipeline,
    read,
    v12_attachment,
    v12_package,
)
from test_write import l3_fact, write

from javmem import attachments as reader
from javmem.cli import main
from javmem.convert12 import NOTE_ATTACHMENT_BAD_PATH
from javmem.packet import parse_packet
from javmem.packet.model import BAD_ATTACHMENT
from javmem.packet.render import attachment_block, attachment_section
from javmem.pipeline.normalize import WITHDRAWN, normalize
from javmem.pipeline.render import render_window
from javmem.pipeline.window import build_windows
from javmem.raw import iter_sentences


def one_file(name: str, text: str, *, line: int = 1) -> str:
    return attachment_section(
        [],
        [attachment_block(name=name, media_type="text/plain", size=None, of_section=1, of_line=line, state="full", reason=None, text=text)],
    )


# ---------- 严重：读 PDF 没有时间上限 ----------


def test_a_pdf_that_takes_too_long_is_given_up() -> None:
    """几 KB 的 PDF（表单对象一层画两次下一层）能让 pdfminer 跑上几小时，邮件导入整批卡住。在子进程里读，到点就停。"""
    started = time.monotonic()
    result = read(make_fanout_pdf(26), "application/pdf", pdf_seconds=2)
    assert (result.state, result.reason, result.text) == ("none", "timeout", "")
    assert time.monotonic() - started < 30
    assert reader.PDF_SECONDS >= 30  # 平常的 PDF 有足够的时间


# ---------- 一般 ----------


def test_converter_survives_a_nul_in_relative_path(tmp_path: Path) -> None:
    """relative_path 里有 NUL，resolve() 抛 ValueError，整批一个包都不写。"""
    source = v12_package(tmp_path / "in", [v12_attachment("a.txt", "text/plain", b"x", relative="attachments/a\u0000.txt")])
    v12_package(source, [], package_id="desk-files-002")
    report, packets = convert(tmp_path, source)
    assert len(packets) == 2
    [body] = next(packet for packet in packets if packet.attachments).attachments
    assert (body.text, body.reason) == ("none", "missing")
    assert NOTE_ATTACHMENT_BAD_PATH in next(result.notes for result in report.packages if result.package_id == "desk-files-001")


QUOTING_FILE = build_packet(
    body="## 原话 · 10:30–10:31\n- [10:30] desk-bot：把合同发我。\n- [10:31] 我：[文件:合同.txt]\n  > [引用 10:30 desk-bot] 把合同发我。\n\n"
    + one_file("合同.txt", "违约金按 5% 算。", line=2)
)


def test_withdrawing_my_line_takes_the_attachment_even_when_it_quotes_something() -> None:
    """那句话带着引用行，主人撤回自己那句，附件没跟着撤。引用行不算新说的，判断“整句撤回了”时不算它。"""
    units = normalize(parsed(QUOTING_FILE), withdrawn={f"{CHAT_ID}#1.2.1"}).units
    attached = [line for unit in units for line in unit.lines if line.attachment is not None]
    assert attached and all(line.kind == WITHDRAWN for line in attached)


def test_withdrawing_the_sentence_that_names_the_file_takes_the_attachment() -> None:
    """写给模型的附件抬头里有附件名。写着附件名的那一句撤回了（邮件是邮件头的附件名那一句），附件也撤回，名字不再发出去。"""
    packet = parsed(build_packet(body="## 原话 · 10:30–10:30\n- [10:30] 我：合同在这。[文件:合同.txt]\n\n" + one_file("合同.txt", "违约金按 5% 算。")))
    assert [s.text for s in iter_sentences(packet)][:2] == ["合同在这。", "[文件:合同.txt]"]
    kinds = {line.sentence_id: line.kind for unit in normalize(packet, withdrawn={f"{CHAT_ID}#1.1.2"}).units for line in unit.lines}
    assert kinds[f"{CHAT_ID}#1.1.1"] != WITHDRAWN and kinds[f"{CHAT_ID}#attachment.1.1"] == WITHDRAWN

    mail = parse_packet(MAIL_PACKET.encode("utf-8"), filename=f"{MAIL_ID}.md")
    withdrawn = {f"{MAIL_ID}#2.attachments"}
    kinds = {line.sentence_id: line.kind for unit in normalize(mail, withdrawn=withdrawn).units for line in unit.lines}
    assert kinds[f"{MAIL_ID}#attachment.1.1"] == kinds[f"{MAIL_ID}#attachment.2.1"] == WITHDRAWN
    assert kinds[f"{MAIL_ID}#2.subject"] != WITHDRAWN  # 信本身不动
    windows = build_windows([normalize(mail, withdrawn=withdrawn)])
    assert "报价单.pdf" not in "\n".join(render_window(window, PATTERNS).text for window in windows)


def test_withdrawing_the_attachment_names_withdraws_the_attachment_in_the_ledger(world: World) -> None:
    pipeline(world, ("inbox/mail-sales-a", MAIL_PACKET))
    assert main(["withdraw", "--library", "prod", "--sentence", f"{MAIL_ID}#2.attachments"]) == 0
    assert owner_search(world, "木纹色餐桌")["result"] == "none"
    assert owner_search(world, "Hi Mai")["result"] == "hit"  # 信的正文不动


LETTER_PACKET = build_packet(
    body="## 原话 · 10:30–10:31\n- [10:30] 我：供应商的回信，你看下。\n- [10:31] 我：[文件:回信.txt]\n\n"
    + one_file("回信.txt", "致李明：\n我同意把违约金降到 2%，下周签补充协议。\n张三", line=2)
)


def test_first_person_in_a_file_the_owner_sent_is_not_the_owner(world: World) -> None:
    """附件的句子跟着那句话算主人说的，文件里的“我”（写信的人）就被挂到了“主人”上。"""

    def extract(window):  # noqa: ANN001, ANN202
        entity = {"key": "e1", "mention": "我", "name": "我", "type": "人"}
        found = [
            l3_fact(window, "我同意把违约金降到 2%", "我同意把违约金降到 2%", subject="e1", attribute="违约金", value="2%")
            for line in window.targets
            if "违约金" in line.text
        ]
        return {"entities": [entity], "facts": found}

    world.drop("inbox/desk-bot", LETTER_PACKET)
    world.intake()
    conn = write(world, extract)
    try:
        [row] = conn.execute("SELECT subject_entity_id FROM facts").fetchall()
        assert row["subject_entity_id"] is None  # 文件里的“我”是泛称，不是主人
        assert conn.execute("SELECT count(*) FROM entities WHERE canonical_name = '主人'").fetchone()[0] == 0
    finally:
        conn.close()


# ---------- 小 ----------


@pytest.mark.parametrize(
    "bad",
    [
        block(text="none", reason="whatever"),
        block(text="none", reason="too_long"),  # 截断的原因写在了没收的上面
        block(text="truncated", reason="image"),
    ],
)
def test_reason_must_be_one_the_spec_lists(bad: str) -> None:
    """reason 不核对取值，规范里“取值不对”算写错的块。"""
    if "text: none" in bad:
        bad = bad.split("\n\n  第一行")[0] + "\n"  # 没收正文的块下面不写正文
    packet = parsed(build_packet(body="## 原话 · 10:30–10:30\n- [10:30] 我：合同。\n\n## 附件\n" + bad))
    assert packet.attachments == [] and [warning.kind for warning in packet.warnings] == [BAD_ATTACHMENT]


def test_attachment_lines_starting_with_a_quote_mark_are_quoted() -> None:
    """query-api 5.5 的 quoted 是“去掉缩进后以 > 开头的行”，附件的正文也一样。"""
    packet = parsed(build_packet(body="## 原话 · 10:30–10:30\n- [10:30] 我：[文件:信.txt]\n\n" + one_file("信.txt", "好的，就这么定。\n> 你上次说的 11 月 15 日。")))
    quoted = {s.text: s.quoted for s in iter_sentences(packet) if s.section_kind == "attachment"}
    assert quoted == {"好的，就这么定。": False, "> 你上次说的 11 月 15 日。": True}


def test_converter_reads_the_charset_in_the_media_type(tmp_path: Path) -> None:
    """media_type 里写的编码不用，Shift_JIS 的文字读成乱码还标 full。"""
    data = "納期は11月15日です。".encode("shift_jis")
    source = v12_package(tmp_path / "in", [v12_attachment("a.txt", "text/plain; charset=shift_jis", data)])
    (source / "attachments").mkdir()
    (source / "attachments" / "a.txt").write_bytes(data)
    _, [packet] = convert(tmp_path, source)
    assert [line.text for line in packet.attachments[0].lines] == ["納期は11月15日です。"]


def test_flat_directory_without_fingerprints_does_not_borrow_another_packages_file(tmp_path: Path) -> None:
    """不写 --attachments 时一批包共用一个 attachments 目录。1.2 没写指纹、大小又正好一样，就会读到别的包的文件
    （审阅里业务的包读到了管账的包的工资表）。没有指纹就认不出是不是它说的那个文件，不读。"""
    salary = "工资表：示例 月薪 38000。".encode("utf-8")
    source = v12_package(tmp_path / "in", [{**v12_attachment("a.txt", "text/plain", salary), "sha256": None}])
    (source / "attachments").mkdir()
    (source / "attachments" / "a.txt").write_bytes(salary)
    _, [packet] = convert(tmp_path, source)
    assert (packet.attachments[0].text, packet.attachments[0].reason) == ("none", "missing")
    # 给了 --attachments（一个包一个目录），没有指纹也认得出
    own = tmp_path / "files" / "desk-files-001" / "attachments"
    own.mkdir(parents=True)
    (own / "a.txt").write_bytes(salary)
    _, [packet] = convert(tmp_path, source, attachments_dir=tmp_path / "files")
    assert packet.attachments[0].text == "full"


def test_an_attachment_nobody_mentions_is_not_read_or_counted_as_read(tmp_path: Path) -> None:
    """source_refs 指的话都没进包的附件，正文读了又丢掉，报告里还记成 full。"""
    data = "没人提的附件。".encode("utf-8")
    source = v12_package(tmp_path / "in", [v12_attachment("a.txt", "text/plain", data, refs=("r9",))])
    (source / "attachments").mkdir()
    (source / "attachments" / "a.txt").write_bytes(data)
    report, [packet] = convert(tmp_path, source)
    assert packet.attachments == [] and [note.text for note in packet.notes] == [f"附件 a.txt，text/plain，{len(data)} 字节"]
    assert report.packages[0].attachments == {"unlinked": 1}
    assert report.attachments == []


def test_a_new_revision_with_new_attachment_text_stays_withdrawn(world: World) -> None:
    """新版本的附件多了一句新的话（指纹对不上），也要跟着撤回了的那句话撤回（L1 的规矩，收件时 mark_on_arrival）。"""
    pipeline(world, ("inbox/desk-bot", FILE_PACKET))
    assert main(["withdraw", "--library", "prod", "--sentence", f"{CHAT_ID}#1.2.1"]) == 0
    second = FILE_PACKET.replace("privacy: normal", "privacy: normal\nrevision: 2").replace(
        "  违约金按 5% 算。", "  违约金按 5% 算。\n  新加的一句：付款 30 天。"
    )
    world.drop("inbox/desk-bot", second)
    world.intake()
    assert world.packet_row(CHAT_ID, 2)["state"] == "current"
    assert owner_search(world, "新加的一句")["result"] == "none"
    assert owner_search(world, "违约金")["result"] == "none"
