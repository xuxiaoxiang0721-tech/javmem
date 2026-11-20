"""附件正文第二轮（D93）：报告第 4 节第 16 到 26 条。样例都是编的，文件现拼（tests/officegen.py）。"""

from __future__ import annotations

import io
import random
import re
import zipfile

import pytest
from officegen import (
    docx_with_body,
    make_docx,
    make_encrypted_office,
    make_xlsx,
    rewrite_sheet,
    w_p,
    w_run,
    w_tbl,
    w_tc,
    w_tr,
    with_extra_part,
)
from test_attachments import PATTERNS

from javmem import attachments as reader

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def read(data: bytes, media_type: str, name: str, **options: object) -> reader.AttachmentText:
    return reader.read_attachment(data, media_type=media_type, name=name, patterns=PATTERNS, **options)


# ---------- 第 26 条：最后一个换行离 2 万字太远时硬截；第 17 条：截了的第一行照写 ----------


def test_a_long_line_is_cut_hard_when_the_last_break_is_too_early() -> None:
    text = "条款\n" + "甲方按期付款，乙方按期交货。" * 3000  # 只有一个换行，在最前面
    result = read(text.encode("utf-8"), "text/plain", "条款.txt")
    assert (result.state, result.reason) == ("truncated", "too_long")
    assert result.text.startswith("条款\n甲方") and 19_000 <= len(result.text) <= reader.MAX_TEXT_CHARS


def test_a_hard_cut_backs_off_to_a_space_and_never_halves_a_word() -> None:
    result = read(("Terms\n" + "alpha beta gamma delta " * 2000).encode(), "text/plain", "terms.txt")
    assert result.state == "truncated" and len(result.text) >= 19_000
    assert result.text.split(" ")[-1] in ("alpha", "beta", "gamma", "delta")


def test_a_break_past_the_half_way_mark_is_still_where_the_cut_goes() -> None:
    lines = "\n".join([f"第 {number} 行" + "字" * 20 for number in range(1, 2001)])
    result = read(lines.encode("utf-8"), "text/plain", "长.txt")
    assert result.text.split("\n")[-1].startswith("第 ") and result.text.endswith("字")


def test_a_huge_first_cell_keeps_its_row_and_twenty_thousand_characters() -> None:
    def huge(xml: str) -> str:  # openpyxl 写的字串放在共享表里，这里直接写进格子
        return re.sub(r'<c r="B1"[^>]*>.*?</c>', '<c r="B1" t="inlineStr"><is><t>' + "交" * 300_000 + "</t></is></c>", xml)

    sheet = rewrite_sheet(make_xlsx({"条款": [["编号", "占位"]]}), huge)
    result = read(sheet, XLSX, "样例.xlsx")
    assert result.state == "truncated"
    assert result.text.startswith("[工作表 条款]\n编号\t交交") and len(result.text) >= 19_000


def test_a_title_and_one_huge_paragraph_keep_both() -> None:
    result = read(make_docx(["购销合同", "条" * 30_000]), DOCX, "样例.docx")
    assert result.state == "truncated"
    assert result.text.startswith("购销合同\n条条") and len(result.text) >= 19_000


# ---------- 第 16 条：xlsx 工作表的 _rels 也算进解开以后的大小 ----------


def test_a_huge_sheet_relationship_part_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MAX_UNZIPPED_BYTES", 4 * 1024 * 1024)
    bomb = with_extra_part(make_xlsx({"明细": [["K-1", 4]]}), "xl/worksheets/_rels/sheet1.xml.rels", 8 * 1024 * 1024)
    assert read(bomb, XLSX, "样例.xlsx").reason == "too_large"
    def padded(xml: str) -> str:  # 真的工作表撑到 8 MB（中间是空白），它是边解边读的，照旧不算
        return xml.replace("</sheetData>", " " * (8 * 1024 * 1024) + "</sheetData>")

    assert read(rewrite_sheet(make_xlsx({"明细": [["K-1", 4]]}), padded), XLSX, "样例.xlsx").text == "[工作表 明细]\nK-1\t4"


# ---------- 第 24 条：压得不狠的大文件照读，只挡压缩炸弹 ----------


def test_a_big_file_that_barely_compresses_is_still_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MAX_UNZIPPED_BYTES", 4 * 1024 * 1024)
    noise = random.Random(7).randbytes(8 * 1024 * 1024)  # 编的字节，几乎压不动（没压缩的大图那样）
    data = with_extra_part(make_docx(["正文一段"]), "word/media/image1.bmp", 0, content=noise)
    result = read(data, DOCX, "样例.docx")
    assert (result.state, result.text) == ("full", "正文一段")
    bomb = with_extra_part(make_docx(["正文一段"]), "word/media/image1.png", 8 * 1024 * 1024)  # 全是 0：压缩炸弹
    assert read(bomb, DOCX, "样例.docx").reason == "too_large"


# ---------- 第 18 条：截断按收掉空白以后的字数算 ----------


def test_whitespace_does_not_use_up_the_limit() -> None:
    def spaces(xml: str) -> str:
        return re.sub(r'<c r="A2"[^>]*>.*?</c>', '<c r="A2" t="inlineStr"><is><t xml:space="preserve">' + " " * 25_000 + "备注</t></is></c>", xml)

    sheet = rewrite_sheet(make_xlsx({"明细": [["K-1", 4], ["占位"], ["K-3", 6]], "第二张": [["K-9", 1]]}), spaces)
    result = read(sheet, XLSX, "样例.xlsx")
    assert (result.state, result.text) == ("full", "[工作表 明细]\nK-1\t4\n备注\nK-3\t6\n\n[工作表 第二张]\nK-9\t1")
    body = w_p('<w:r><w:t xml:space="preserve">第一段' + " " * 30_000 + "</w:t></w:r>") + w_p("第二段") + w_p("第三段")
    assert read(docx_with_body(body), DOCX, "样例.docx").text == "第一段\n第二段\n第三段"
    table = w_tbl([w_tr(w_tc("<w:p/>" * 20_001 + w_p("格子里的字")), w_tc(w_p("右边")))], 2) + w_p("表后一段")
    result = read(docx_with_body(table), DOCX, "样例.docx")
    assert (result.state, result.text) == ("full", "格子里的字\t右边\n\n表后一段")


# ---------- 第 19 条：mc:AlternateContent 只读第一个 Choice，没有 Choice 才读 Fallback ----------

CHOICE = '<mc:AlternateContent><mc:Choice Requires="w14">{}</mc:Choice><mc:Fallback>{}</mc:Fallback></mc:AlternateContent>'


def test_alternate_content_is_read_once_everywhere() -> None:
    body = w_p("前") + CHOICE.format(w_p("选的那份"), w_p("备用那份")) + w_p("后")
    assert read(docx_with_body(body), DOCX, "样例.docx").text == "前\n选的那份\n后"
    cell = w_tbl([w_tr(w_tc(CHOICE.format(w_p("选的那份"), w_p("备用那份"))), w_tc(w_p("右")))], 2)
    assert read(docx_with_body(cell), DOCX, "样例.docx").text == "选的那份\t右"
    two = (
        '<mc:AlternateContent><mc:Choice Requires="w15">' + w_run("甲") + '</mc:Choice><mc:Choice Requires="w14">'
        + w_run("甲") + "</mc:Choice><mc:Fallback>" + w_run("甲") + "</mc:Fallback></mc:AlternateContent>"
    )
    assert read(docx_with_body(w_p(two, "方")), DOCX, "样例.docx").text == "甲方"
    only_fallback = "<mc:AlternateContent><mc:Fallback>" + w_p("只有备用的") + "</mc:Fallback></mc:AlternateContent>"
    assert read(docx_with_body(only_fallback), DOCX, "样例.docx").text == "只有备用的"


# ---------- 第 20 条：没填的内容控件里的提示字、注音不收 ----------


def test_placeholder_text_and_ruby_annotations_are_left_out() -> None:
    unfilled = '<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>' + w_run("单击此处填写日期") + "</w:sdtContent></w:sdt>"
    filled = '<w:sdt><w:sdtPr/><w:sdtContent>' + w_run("另行通知") + "</w:sdtContent></w:sdt>"
    body = w_p("签约日期：", unfilled) + w_p("交货日期：", filled)
    block = '<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>' + w_p("在这里写备注") + "</w:sdtContent></w:sdt>"
    ruby = '<w:r><w:ruby><w:rubyPr/><w:rt><w:r><w:t>ning</w:t></w:r></w:rt><w:rubyBase><w:r><w:t>宁</w:t></w:r></w:rubyBase></w:ruby></w:r>'
    text = read(docx_with_body(body + block + w_p(ruby, "波港")), DOCX, "样例.docx").text
    assert text == "签约日期：\n交货日期：另行通知\n宁波港"


# ---------- 第 22 条：xlsx 一行里两个格子写着同一列，只写后面那个 ----------


def test_two_cells_in_the_same_column_keep_the_later_one() -> None:
    def twice(xml: str) -> str:
        return re.sub(r'(<row r="1"[^>]*>)', r'\1<c r="A1" t="inlineStr"><is><t>先</t></is></c>', xml)

    sheet = rewrite_sheet(make_xlsx({"明细": [["后", 5]]}), twice)
    assert read(sheet, XLSX, "样例.xlsx").text == "[工作表 明细]\n后\t5"


# ---------- 第 23 条：目录项名字后面没补 0 的加密文件也认得出 ----------


def test_an_encrypted_package_entry_without_zero_padding_is_still_encrypted() -> None:
    data = bytearray(make_encrypted_office())
    entry = data.find("EncryptedPackage".encode("utf-16-le"))
    data[entry + 34 : entry + 64] = b"\x01" * 30  # 名字后面不是 0（规范没要求补 0）
    assert read(bytes(data), XLSX, "样例.xlsx").reason == "encrypted"
    assert read(bytes(data), DOCX, "样例.docx").reason == "encrypted"


def patch_compress_size(data: bytes, name: str, value: int) -> bytes:
    """把压缩包中央目录里 name 那一项写的压缩以后多大改成 value（本地头、数据都不动）：目录里的数随便写得出来。"""
    raw = bytearray(data)
    at = 0
    while (at := raw.find(b"PK\x01\x02", at)) >= 0:
        length = int.from_bytes(raw[at + 28 : at + 30], "little")
        if bytes(raw[at + 46 : at + 46 + length]) == name.encode():
            raw[at + 20 : at + 24] = value.to_bytes(4, "little")
            return bytes(raw)
        at += 4
    raise AssertionError(name)


def test_a_bomb_cannot_hide_behind_a_faked_packed_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """D93 审阅第 2 条：50 倍以前用的是目录里写的压缩以后多大，多放一项、写成 4 GB 就过了。现在用文件真的大小。"""
    monkeypatch.setattr(reader, "MAX_UNZIPPED_BYTES", 4 * 1024 * 1024)
    bomb = with_extra_part(make_docx(["正文一段"]), "word/media/image1.png", 8 * 1024 * 1024)
    bomb = patch_compress_size(with_extra_part(bomb, "customXml/item9.bin", 0, content=b"x" * 10), "customXml/item9.bin", 0xFFFFFFF0)
    assert read(bomb, DOCX, "样例.docx").reason == "too_large"


def test_only_the_parts_the_workbook_says_are_sheets_are_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """D93 审阅第 13 条：共享字串表挪到 xl/worksheets/ 下面，openpyxl 照样整个读进来，要算。"""
    monkeypatch.setattr(reader, "MAX_UNZIPPED_BYTES", 4 * 1024 * 1024)
    source = zipfile.ZipFile(io.BytesIO(make_xlsx({"明细": [["K-1", 4]]})))  # openpyxl 写的字串是行内的，没有共享表，自己加一张
    strings = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
    relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace(b"</Relationships>", f'<Relationship Type="{relationship}" Target="worksheets/strings.xml" Id="rId9"/></Relationships>'.encode())
            if info.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", f'<Override PartName="/xl/worksheets/strings.xml" ContentType="{strings}"/></Types>'.encode())
            target.writestr(info, data)
        target.writestr("xl/worksheets/strings.xml", bytes(8 * 1024 * 1024))
    assert read(buffer.getvalue(), XLSX, "样例.xlsx").reason == "too_large"


def test_an_unfilled_cell_keeps_its_column_and_filled_ones_are_read() -> None:
    """D93 审阅第 3、4 条：整格包在没填的内容控件里的，那一格写空，后面的不往前挪；showingPlcHdr 写着 0、false 的是填了的。"""
    unfilled_cell = '<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>' + w_tc(w_p("单击此处填写")) + "</w:sdtContent></w:sdt>"
    rows = [w_tr(w_tc(w_p("品名")), w_tc(w_p("单价")), w_tc(w_p("数量"))), w_tr(w_tc(w_p("餐桌")), unfilled_cell, w_tc(w_p("2000")))]
    assert read(docx_with_body(w_tbl(rows, 3)), DOCX, "样例.docx").text == "品名\t单价\t数量\n餐桌\t\t2000"
    filled = '<w:sdt><w:sdtPr><w:showingPlcHdr w:val="{}"/></w:sdtPr><w:sdtContent>{}</w:sdtContent></w:sdt>'
    body = w_p("交货：", filled.format("0", w_run("另行通知"))) + filled.format("false", w_p("付款方式：电汇"))
    assert read(docx_with_body(body), DOCX, "样例.docx").text == "交货：另行通知\n付款方式：电汇"


def test_a_hard_cut_never_leaves_half_a_secret() -> None:
    """D93 审阅第 5 条：截点前面 200 个字都没有空白时，截点落在一串字母、数字里的，退到这一串前面，不留半截密钥。"""
    key = "sk-" + "AbCd" * 10
    result = read(("k\n" + "a," * 9990 + key + ",tail").encode(), "text/plain", "样例.txt")
    assert result.state == "truncated" and "sk-" not in result.text and result.text.endswith("a,")
    value = "Ab" + "12xy"
    result = read(("说明\n" + "中" * 19991 + "，密码：" + value + "，其余").encode("utf-8"), "text/plain", "说明.txt")
    assert result.state == "truncated" and "Ab" not in result.text and result.text.endswith("，密码：")


def test_blanks_split_over_many_runs_do_not_use_up_the_limit() -> None:
    """D93 审阅第 10 条：空格分在很多个短的 run 里（每个 8 个），也按收掉以后的字数算。"""
    runs = "".join(w_run(" " * 8) for _ in range(3000))
    body = w_p(runs, w_run("备注：样品已寄出")) + w_p("第二段：交期另行通知")
    result = read(docx_with_body(body), DOCX, "样例.docx")
    assert (result.state, result.text) == ("full", "备注：样品已寄出\n第二段：交期另行通知")


# ---------- 第二遍审阅第 1 条：边解边读的工作表只认名字像、关系写明、别处没写成别的东西的 ----------

STRINGS_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def edit_xlsx(data: bytes, *, relationships: str = "", overrides: str = "", parts: dict[str, bytes] | None = None) -> bytes:
    """workbook.xml.rels 里多写几条关系、[Content_Types].xml 里多写几条 Override；parts 里的部件有就换掉，没有就加上。"""
    parts = dict(parts or {})
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            body = parts.pop(info.filename, None) or source.read(info.filename)
            if info.filename == "xl/_rels/workbook.xml.rels":
                body = body.replace(b"</Relationships>", relationships.encode() + b"</Relationships>")
            if info.filename == "[Content_Types].xml":
                body = body.replace(b"</Types>", overrides.encode() + b"</Types>")
            target.writestr(info, body)
        for name, body in parts.items():
            target.writestr(name, body)
    return buffer.getvalue()


def patch_crc(data: bytes, name: str) -> bytes:
    """把压缩包中央目录里 name 那一项的 CRC 改掉：读这一项时 zipfile 报 BadZipFile。"""
    raw = bytearray(data)
    at = 0
    while (at := raw.find(b"PK\x01\x02", at)) >= 0:
        length = int.from_bytes(raw[at + 28 : at + 30], "little")
        if bytes(raw[at + 46 : at + 46 + length]) == name.encode():
            crc = int.from_bytes(raw[at + 16 : at + 20], "little") ^ 0xFFFFFFFF
            raw[at + 16 : at + 20] = crc.to_bytes(4, "little")
            return bytes(raw)
        at += 4
    raise AssertionError(name)


@pytest.mark.parametrize(
    ("relationships", "overrides", "name"),
    [
        # 多写一条没人用的工作表关系，指着共享字串表（审阅复现的：596 KB 的文件，共享字串表解开 213 MB）
        (
            f'<Relationship Type="{REL}worksheet" Target="sharedStrings.xml" Id="rId90"/>'
            f'<Relationship Type="{REL}sharedStrings" Target="sharedStrings.xml" Id="rId91"/>',
            f'<Override PartName="/xl/sharedStrings.xml" ContentType="{STRINGS_TYPE}"/>',
            "xl/sharedStrings.xml",
        ),
        # 放在 xl/worksheets/ 下面、又写成工作表的共享字串表：[Content_Types].xml 说它是共享字串表，openpyxl 按这个找
        (
            f'<Relationship Type="{REL}worksheet" Target="worksheets/strings.xml" Id="rId90"/>',
            f'<Override PartName="/xl/worksheets/strings.xml" ContentType="{STRINGS_TYPE}"/>',
            "xl/worksheets/strings.xml",
        ),
        # 写成工作表的样式表：openpyxl 按固定的名字整个读进来
        (f'<Relationship Type="{REL}worksheet" Target="styles.xml" Id="rId90"/>', "", "xl/styles.xml"),
        # 外部链接也指着它：openpyxl 当外部链接整个读进来
        (
            f'<Relationship Type="{REL}externalLink" Target="worksheets/sheet9.xml" Id="rId90"/>'
            f'<Relationship Type="{REL}worksheet" Target="worksheets/sheet9.xml" Id="rId91"/>',
            "",
            "xl/worksheets/sheet9.xml",
        ),
    ],
)
def test_a_part_read_whole_cannot_pass_as_a_sheet(
    monkeypatch: pytest.MonkeyPatch, relationships: str, overrides: str, name: str
) -> None:
    """D93 第二遍审阅第 1 条：只信 workbook.xml.rels 里写的关系，多写一条工作表关系，就能让整个读进来的部件不算。"""
    monkeypatch.setattr(reader, "MAX_UNZIPPED_BYTES", 4 * 1024 * 1024)
    data = edit_xlsx(make_xlsx({"明细": [["K-1", 4]]}), relationships=relationships, overrides=overrides,
                     parts={name: bytes(8 * 1024 * 1024)})
    assert reader._unzipped_too_large(data, reader.KIND_EXCEL)


def test_a_relationship_list_that_cannot_be_read_leaves_nothing_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """D93 第二遍审阅第 1 条：workbook.xml.rels 读坏了（CRC 不对），以前整个判断说“不是炸弹”。现在一个部件都不放过。"""
    monkeypatch.setattr(reader, "MAX_UNZIPPED_BYTES", 4 * 1024 * 1024)

    def padded(xml: str) -> str:
        return xml.replace("</sheetData>", " " * (8 * 1024 * 1024) + "</sheetData>")

    data = rewrite_sheet(make_xlsx({"明细": [["K-1", 4]]}), padded)
    assert not reader._unzipped_too_large(data, reader.KIND_EXCEL)  # 好好的工作表照旧不算
    assert reader._unzipped_too_large(patch_crc(data, "xl/_rels/workbook.xml.rels"), reader.KIND_EXCEL)
    assert reader._unzipped_too_large(patch_crc(data, "[Content_Types].xml"), reader.KIND_EXCEL)


# ---------- D96 第 40 条：硬截往回退，符号也算进一串，退不到头的不退到 0 ----------


def test_a_hard_cut_backs_off_over_symbols_in_a_password() -> None:
    """D96 第 40 条：密码里有 @ 时，以前只退到 @ 后面，“密码：”后面留下两个字原样进包（打码要 4 个字才认）。"""
    secret = "A" + "@" + "bcdefgh" + "9"
    prefix = "说明\n" + "中" * 19989 + "，密码："  # 截点落在密码的第 5 个字上
    result = read((prefix + secret + "，其余").encode("utf-8"), "text/plain", "说明.txt")
    assert result.state == "truncated" and result.text.endswith("，密码：") and "@" not in result.text


def test_a_hard_cut_inside_one_endless_run_keeps_the_limit() -> None:
    """D96 第 40 条：从头就是一整串没有空白的字（base64 这类），以前退到 0，这一块只剩个头；现在照原来的截点截。"""
    result = read(("QUJD" * 7500).encode(), "text/plain", "样例.txt")
    assert (result.state, len(result.text)) == ("truncated", 20000)


def test_a_hard_cut_in_minified_json_leaves_nothing_of_a_password_with_symbols() -> None:
    """D96 第一遍审阅第 8 条：压成一行的 JSON，截点前 200 个字没有空白、密码里有 @、截点落在 @ 后面的，
    第二遍按字母数字退只退到 @ 后面，值的头两个字原样进包。第二遍也把 @、# 算进一串，引号、逗号、分号算断开。"""
    secret = "A" + "@" + "bcdefgh" + "9"
    prefix = "kk" + "ab," * 6661 + '"password":"'  # 截点落在密码的第 4 个字上
    result = read((prefix + secret + '","next":1}').encode(), "text/plain", "样例.txt")
    assert result.state == "truncated" and result.text.endswith('"password":"') and "@" not in result.text


@pytest.mark.parametrize("head", [" ", '"', "说明\n"])
def test_a_hard_cut_that_would_back_off_past_half_keeps_the_limit(head: str) -> None:
    """D96 第一遍审阅第 8 条：开头一两个字再接一整串的，以前退到开头，截完只剩一两个字；退到一半以前的照原截点截。"""
    result = read((head + "QUJD" * 7500).encode("utf-8"), "text/plain", "样例.txt")
    assert result.state == "truncated" and len(result.text) >= 19990


@pytest.mark.parametrize(
    ("prefix", "keyword", "ending"),
    [("a=1&" * 4998, "token=", "token="), ("id|" * 6665, "sk-", "|"), ("k:v:" * 4997, "password:", "password:")],
)
def test_a_hard_cut_in_a_long_run_split_by_symbols_leaves_nothing_of_the_value(prefix: str, keyword: str, ending: str) -> None:
    """D96 第二遍审阅第 2 条：一行一万多字、截点附近没有空白、引号、逗号、分号的（a=1&…、id|…、k:v:…），
    第二遍把整行算成一串，从一半以前开始，就照原截点截，token= 后面留下一两个字、sk- 密钥留下半截，打码认不出。
    退到一半以前的，再把 & | ? = : 也当断开退一遍，还不行再只认字母数字。"""
    value = "Ab" + "cd" * 10 + "9"
    result = read((prefix + keyword + value + "&tail=1").encode(), "text/plain", "样例.txt")
    assert result.state == "truncated" and result.text.endswith(ending) and "Ab" + "cd" not in result.text

