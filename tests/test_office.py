"""Excel、Word 附件的正文（D91 第 6 项，packet-spec 第 3 节“附件小节”）。样例都是编的，文件现拼（tests/officegen.py）。

- .xlsx、.xlsm 用 openpyxl（只读，取单元格的值，不取公式），.xls 用 xlrd，.docx 用 python-docx（段落和表格）；
  老的 .doc 先不读。
- 跟 PDF 一样在子进程里读，限时；一个文件截到 2 万字，超了标截断。
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import pytest
from officegen import make_docx, make_encrypted_office, make_ole2, make_xls, make_xlsx, rewrite_sheet
from pdfgen import make_pdf
from test_attachments import MAIL_FIXTURES, PATTERNS
from test_pdf_child import imported

from javmem import attachments as reader
from javmem.packet import parse_packet

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def read(data: bytes, media_type: str, name: str, **options: object) -> reader.AttachmentText:
    return reader.read_attachment(data, media_type=media_type, name=name, patterns=PATTERNS, **options)


@pytest.mark.parametrize(
    ("media_type", "name", "kind"),
    [
        (XLSX, "样例一.xlsx", reader.KIND_EXCEL),
        ("application/vnd.ms-excel", "样例二.xls", reader.KIND_EXCEL),
        ("application/vnd.ms-excel.sheet.macroEnabled.12", "下单.xlsm", reader.KIND_EXCEL),
        ("application/octet-stream", "形式发票.XLSX", reader.KIND_EXCEL),
        (DOCX, "合同.docx", reader.KIND_WORD),
        ("application/msword", "样例三.doc", reader.KIND_WORD),
        ("application/octet-stream", "合同.docx", reader.KIND_WORD),
        ("application/vnd.ms-powerpoint", "介绍.ppt", reader.KIND_OTHER),
    ],
)
def test_office_kinds(media_type: str, name: str, kind: str) -> None:
    assert reader.kind_of(media_type, name) == kind


def test_xlsx_cells_sheet_by_sheet() -> None:
    data = make_xlsx(
        {
            "报价": [["品名", "数量", "单价"], ["木纹色餐桌", 2000, 12.5], [None, None, None], ["交货", datetime(2026, 11, 15), None]],
            "备注": [["含包装"]],
        },
        formula="=B2*C2",  # 公式不取：openpyxl 写的公式没有算好的值，这一格就是空的
    )
    result = read(data, XLSX, "样例一.xlsx")
    assert (result.state, result.reason) == ("full", None)
    assert result.text == "[工作表 报价]\n品名\t数量\t单价\n木纹色餐桌\t2000\t12.5\n交货\t2026-11-15\n\n[工作表 备注]\n含包装"
    assert "B2" not in result.text


def test_sheets_without_a_single_cell_are_left_out() -> None:
    result = read(make_xlsx({"空表": [], "图表说明": [[None, None]], "明细": [["箱号"], ["A-01"]]}), XLSX, "明细.xlsx")
    assert result.text == "[工作表 明细]\n箱号\nA-01"


def test_a_sheet_that_claims_a_huge_range_is_read_by_its_real_cells() -> None:
    def far_away(xml: str) -> str:
        xml = re.sub(r'<dimension ref="[^"]*"/>', '<dimension ref="A1:XFD1048576"/>', xml)
        far = '<row r="900000"><c r="B900000" t="inlineStr"><is><t>最后一格</t></is></c></row>'
        return xml.replace("</sheetData>", far + "</sheetData>")

    data = rewrite_sheet(make_xlsx({"装箱": [["箱号"], ["A-01"]]}), far_away)
    started = time.perf_counter()
    result = read(data, XLSX, "装箱.xlsx")
    assert (result.state, result.text) == ("full", "[工作表 装箱]\n箱号\nA-01\n\t最后一格")
    assert time.perf_counter() - started < 20  # 照写的范围补齐每一行，要读几分钟


def test_old_xls_is_read_with_xlrd() -> None:
    data = make_xls({"装箱单": [["箱号", "件数"], ["A-01", 24], ["A-02", 12.5]]})
    result = read(data, "application/vnd.ms-excel", "样例二.xls")
    assert (result.state, result.text) == ("full", "[工作表 装箱单]\n箱号\t件数\nA-01\t24\nA-02\t12.5")


def test_docx_paragraphs_and_tables_in_order_without_headers() -> None:
    data = make_docx(["示例家居公司 订货确认", "交货期如下。"], [["品名", "数量"], ["餐桌", "2000"]], header="第 1 页 页眉")
    result = read(data, DOCX, "确认.docx")
    assert (result.state, result.text) == ("full", "示例家居公司 订货确认\n交货期如下。\n品名\t数量\n餐桌\t2000")


def test_files_that_are_not_read() -> None:
    assert read(make_encrypted_office(), XLSX, "加密.xlsx").reason == "encrypted"
    assert read(make_encrypted_office(), DOCX, "加密.docx").reason == "encrypted"
    assert read(make_ole2("WordDocument", b"\0" * 4200), "application/msword", "老合同.doc").reason == "unsupported"
    assert read(b"not an office file", XLSX, "坏的.xlsx").reason == "unreadable"
    assert read(b"PK\x03\x04broken zip", DOCX, "坏的.docx").reason == "unreadable"
    assert read(make_xlsx({"空": []}), XLSX, "空.xlsx").reason == "no_text"
    assert read(make_docx([], header="只有页眉"), DOCX, "空.docx").reason == "no_text"


def test_long_sheets_are_cut_and_marked() -> None:
    rows = [[f"第 {number} 行", "字" * 30, number] for number in range(1, 3001)]
    result = read(make_xlsx({"明细": rows}), XLSX, "明细.xlsx")
    assert (result.state, result.reason) == ("truncated", "too_long")
    assert len(result.text) <= reader.MAX_TEXT_CHARS
    assert result.text.split("\n")[-1].startswith("第 ")  # 在换行处截断


def test_blank_lines_after_the_limit_do_not_count_as_cut() -> None:
    data = make_docx(["一二三四五六七八九十", "十九八七六五四三二一", "", ""])
    result = read(data, DOCX, "短.docx", max_chars=21)
    assert (result.state, result.text) == ("full", "一二三四五六七八九十\n十九八七六五四三二一")


def test_office_text_is_redacted() -> None:
    data = make_xlsx({"账户": [["系统", "后台密码是 " + "abcd" + "1234"]]})
    result = read(data, XLSX, "账户.xlsx")
    assert "abcd1234" not in result.text and "[已隐去:密码]" in result.text
    assert result.redactions == {"密码": 1}


def test_office_files_are_read_in_a_child_and_time_out_like_pdfs(monkeypatch: pytest.MonkeyPatch) -> None:
    kinds: list[str] = []
    real = reader.run_child  # 起读附件的子进程都走它（U3：Windows 上起来以后放进作业对象限内存）

    def watching(command: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess:
        kinds.append(command[-1])
        return real(command, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reader, "run_child", watching)
    read(make_xlsx({"a": [["1"]]}), XLSX, "a.xlsx")
    read(make_docx(["一段"]), DOCX, "b.docx")
    assert kinds == ["excel", "word"]

    def stopped(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, reader.STOPPED_BY_ITSELF, b"", b"")

    monkeypatch.setattr(reader, "run_child", stopped)
    budget = reader.PdfBudget()
    assert read(make_xlsx({"a": [["1"]]}), XLSX, "a.xlsx", budget=budget).reason == "timeout"
    assert read(make_pdf(["Unit price 9.8 USD."]), "application/pdf", "b.pdf", budget=budget).reason == "timeout"
    assert budget.skipped == 1  # Excel 超时了，这封信后面的 PDF 不读


def test_a_missing_office_library_stops_the_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    (root / "openpyxl").mkdir(parents=True)
    (root / "openpyxl" / "__init__.py").write_text("raise ImportError('没装 openpyxl')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    with pytest.raises(reader.PdfUnavailable) as caught:
        read(make_xlsx({"a": [["1"]]}), XLSX, "a.xlsx")
    assert "openpyxl" in str(caught.value)


def test_mail_import_writes_the_office_text(tmp_path: Path) -> None:
    message = EmailMessage()
    message["From"] = "Tran Mai <tran.mai@example.org>"
    message["To"] = "李明 <li.ming@example.com>"
    message["Subject"] = "PI"
    message["Date"] = "Tue, 03 Nov 2026 09:12:00 +0700"
    message["Message-ID"] = "<office-0001@example.org>"
    message.set_content("Hi Ming,\n\nPI attached.\n")
    message.add_attachment(make_xlsx({"PI": [["Item", "Qty"], ["Table", 2000]]}), maintype="application",
                           subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="PI.xlsx")
    path = tmp_path / "in" / "pi.eml"
    path.parent.mkdir(parents=True)
    path.write_bytes(bytes(message))
    report = imported(tmp_path, path)
    assert report.attachments == {"full": 1}
    [packet] = (tmp_path / "out" / "mail-sales-a").glob("*.md")
    [body] = parse_packet(packet.read_bytes(), filename=packet.name).attachments
    assert (body.name, body.text) == ("PI.xlsx", "full")
    assert [line.text for line in body.lines] == ["[工作表 PI]", "Item\tQty", "Table\t2000"]
    assert MAIL_FIXTURES.is_dir()
