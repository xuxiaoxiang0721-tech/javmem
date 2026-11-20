"""第 6 项（读 Excel、Word 附件）审阅后补的（D91）。样例都是编的，文件现拼（tests/officegen.py）。

审阅第 1 条 xlrd 往标准输出打的警告；第 2 条 docx 里包起来的字（内容控件、修订、域、格子里的表格）；第 3 条子进程
交回的字没有上限；第 4、15 条竖着合并的格子；第 6 条写成 Office 类型的 CSV、网页；第 7、8、13、14 条时刻、浮点尾巴、
秒、错误值；第 9 条压缩炸弹；第 11 条格子里写着 EncryptedPackage；第 12 条行号倒着写；第 16 条每一行最后一格在很远的列。
"""

from __future__ import annotations

import re
import time as clock
from datetime import datetime, time

import pytest
from officegen import (
    XlsError,
    docx_with_body,
    make_docx,
    make_encrypted_office,
    make_ole2,
    make_xls,
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
XLS = "application/vnd.ms-excel"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SDT = '<w:sdt><w:sdtPr><w:alias w:val="填空"/></w:sdtPr><w:sdtContent>{}</w:sdtContent></w:sdt>'


def read(data: bytes, media_type: str, name: str, **options: object) -> reader.AttachmentText:
    return reader.read_attachment(data, media_type=media_type, name=name, patterns=PATTERNS, **options)


def test_xlrd_warnings_on_stdout_do_not_spoil_the_result() -> None:
    data = make_xls({"装柜": [["柜号", "箱数"], ["C-03", 30]]}) + b"\0" * 100  # 大小不是 512 的整数倍，xlrd 打一行警告
    result = read(data, XLS, "样例四.xls")
    assert (result.state, result.text) == ("full", "[工作表 装柜]\n柜号\t箱数\nC-03\t30")


def test_docx_text_inside_wrappers_is_read_and_deleted_or_boxed_text_is_not() -> None:
    field = (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>3</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )
    text_box = '<w:r><w:pict><v:shape><v:textbox><w:txbxContent>' + w_p("文本框里的字") + "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>"
    either = '<mc:AlternateContent><mc:Choice Requires="w14">' + w_run("甲方") + "</mc:Choice><mc:Fallback>" + w_run("甲方") + "</mc:Fallback></mc:AlternateContent>"
    body = (
        w_p("订单号 XS-0042")
        + SDT.format(w_p("总价：八千六百元整"))
        + w_p("填空一：", SDT.format(w_run("七号柜")))
        + w_p("单价 ", '<w:ins w:id="1" w:author="编"><w:r><w:t>9.6 美元</w:t></w:r></w:ins>',
              '<w:del w:id="2" w:author="编"><w:r><w:delText>9.2 美元</w:delText></w:r></w:del>')
        + w_p("经办：", '<w:fldSimple w:instr=" MERGEFIELD 经办 "><w:r><w:t>赵一</w:t></w:r></w:fldSimple>')
        + w_p('<w:smartTag w:uri="urn:x" w:element="place"><w:r><w:t>示例港三号码头</w:t></w:r></w:smartTag>')
        + w_p('<w:hyperlink w:anchor="a"><w:r><w:t>见附表</w:t></w:r></w:hyperlink>')
        + w_p("第 ", field, " 页")
        + w_p('<w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>', "制表位不是字")
        + w_p("备注", text_box)
        + w_p(either, "签字")
        + w_tbl([w_tr(w_tc(w_p("规格") + w_tbl([w_tr(w_tc(w_p("长 120")), w_tc(w_p("宽 60")))], 2)), w_tc(w_p("另议")))], 2)
        + '<w:customXml w:element="note">' + w_p("自定义标记里的一段") + "</w:customXml>"
    )
    result = read(docx_with_body(body), DOCX, "订单.docx")
    assert result.state == "full"
    assert result.text.split("\n") == [
        "订单号 XS-0042",
        "总价：八千六百元整",
        "填空一：七号柜",
        "单价 9.6 美元",
        "经办：赵一",
        "示例港三号码头",
        "见附表",
        "第 3 页",
        "制表位不是字",
        "备注",
        "甲方签字",
        "规格 长 120 宽 60\t另议",
        "",
        "自定义标记里的一段",
    ]


def test_one_huge_cell_or_paragraph_comes_back_cut() -> None:
    def huge_cell(xml: str) -> str:  # openpyxl 写的字串放在共享表里，这里直接写进格子
        return re.sub(r'<c r="A1"[^>]*>.*?</c>', '<c r="A1" t="inlineStr"><is><t>' + "字" * 300_000 + "</t></is></c>", xml)

    sheet = rewrite_sheet(make_xlsx({"长": [["占位"]]}), huge_cell)
    found = reader._office_text(reader.KIND_EXCEL, sheet, 20_000)
    assert found is not None and len(found[0]) <= 20_001 and found[2]
    found = reader._office_text(reader.KIND_WORD, make_docx(["字" * 300_000, "后面一段"]), 20_000)
    assert found is not None and len(found[0]) <= 20_001 and found[2]
    assert read(sheet, XLSX, "长.xlsx").state == "truncated"


def test_vertically_merged_cells_are_written_once_and_never_break_the_file() -> None:
    spanned = w_tc(w_p("品名、规格"), '<w:gridSpan w:val="2"/>')  # 上一行这一格横跨两列，下一行第二格接着它往下合并
    body = w_p("报价") + w_tbl([w_tr(spanned), w_tr(w_tc(w_p("书柜")), w_tc(w_p(""), "<w:vMerge/>"))], 2) + w_p("以上含税。")
    result = read(docx_with_body(body), DOCX, "报价.docx")
    assert (result.state, result.text) == ("full", "报价\n品名、规格\n书柜\n\n以上含税。")

    rows = [w_tr(w_tc(w_p("柜类"), '<w:vMerge w:val="restart"/>'), w_tc(w_p("型号 1")))]
    rows += [w_tr(w_tc(w_p(""), "<w:vMerge/>"), w_tc(w_p(f"型号 {number}"))) for number in range(2, 1501)]
    started = clock.perf_counter()
    result = read(docx_with_body(w_p("价目") + w_tbl(rows, 2)), DOCX, "价目.docx")
    assert clock.perf_counter() - started < 20  # 以前一层一层往上找，一千多行就 RecursionError
    lines = result.text.split("\n")
    assert (result.state, lines[:3], lines[-1]) == ("full", ["价目", "柜类\t型号 1", "\t型号 2"], "\t型号 1500")


def test_text_and_web_pages_written_as_office_types_are_read_by_their_extension() -> None:
    assert reader.kind_of(XLS, "样例.csv") == reader.KIND_TEXT  # Windows 把 .csv 记成 application/vnd.ms-excel
    result = read("型号,数量\nK-1,4\n".encode(), XLS, "样例.csv")
    assert (result.state, result.text) == ("full", "型号,数量\nK-1,4")
    result = read("<table><tr><td>型号</td><td>K-1</td></tr></table>".encode(), "application/msword", "样例.htm")
    assert (result.state, result.text) == ("full", "型号 K-1")
    assert read(b"not an office file", XLSX, "样例.xlsx").reason == "unreadable"  # 扩展名也是 Office 的照旧


def test_times_errors_and_seconds_read_the_same_from_xls_and_xlsx() -> None:
    expected = "[工作表 考勤]\n到岗\t08:30:00\t#DIV/0!"
    assert read(make_xls({"考勤": [["到岗", time(8, 30), XlsError(0x07)]]}), XLS, "样例.xls").text == expected
    assert read(make_xlsx({"考勤": [["到岗", time(8, 30), "#DIV/0!"]]}), XLSX, "样例.xlsx").text == expected
    data = make_xlsx({"出库": [["时间", datetime(2026, 12, 2, 14, 5, 9), datetime(2026, 12, 2, 14, 5)]]})
    assert read(data, XLSX, "样例.xlsx").text == "[工作表 出库]\n时间\t2026-12-02 14:05:09\t2026-12-02 14:05"


def test_numbers_are_written_the_way_excel_shows_them() -> None:
    def stored_like_excel(xml: str) -> str:  # Excel 存 17 位有效数字
        return xml.replace("<v>41.25</v>", "<v>41.249999999999993</v>").replace("<v>0.3</v>", "<v>0.30000000000000004</v>")

    data = rewrite_sheet(make_xlsx({"金额": [[41.25, 0.3, 1234.5678]]}), stored_like_excel)
    assert read(data, XLSX, "样例.xlsx").text == "[工作表 金额]\n41.25\t0.3\t1234.5678"
    assert read(make_xls({"金额": [[0.1 + 0.2, 7.0]]}), XLS, "样例.xls").text == "[工作表 金额]\n0.3\t7"


def test_office_files_that_unzip_too_big_are_not_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MAX_UNZIPPED_BYTES", 4 * 1024 * 1024)
    bomb = with_extra_part(make_docx(["正文一段"]), "word/media/image1.png", 8 * 1024 * 1024)
    assert len(bomb) < 100_000
    assert read(bomb, DOCX, "样例.docx").reason == "too_large"
    def padded(xml: str) -> str:  # 工作簿里写明的工作表撑到 8 MB：边解边读，不算（没写明的部件 D93 起照算）
        return xml.replace("</sheetData>", " " * (8 * 1024 * 1024) + "</sheetData>")

    assert read(rewrite_sheet(make_xlsx({"明细": [["K-1", 4]]}), padded), XLSX, "样例.xlsx").text == "[工作表 明细]\nK-1\t4"


def test_the_word_encryptedpackage_inside_a_cell_or_text_is_just_text() -> None:
    xls = make_xls({"说明": [["流的名字", "EncryptedPackage"]]})
    assert read(xls, XLS, "样例.xls").text == "[工作表 说明]\n流的名字\tEncryptedPackage"
    doc = make_ole2("WordDocument", "正文里写着 EncryptedPackage。".encode("utf-16-le").ljust(4200, b"\0"))
    assert read(doc, "application/msword", "样例.doc").reason == "unsupported"
    assert read(make_encrypted_office(), XLSX, "样例.xlsx").reason == "encrypted"


def test_rows_written_out_of_order_are_all_kept() -> None:
    def swap(xml: str) -> str:
        start, end = xml.index('<row r="2"'), xml.index('<row r="3"')
        third = xml[end : xml.index("</row>", end) + len("</row>")]
        return xml[:start] + third + xml[start:end] + xml[end + len(third) :]

    data = rewrite_sheet(make_xlsx({"装柜": [["柜号", "箱数"], ["C-01", 12], ["C-02", 18]]}), swap)
    assert read(data, XLSX, "样例.xlsx").text == "[工作表 装柜]\n柜号\t箱数\nC-02\t18\nC-01\t12"


def test_rows_that_end_far_to_the_right_are_quick() -> None:
    def far(xml: str) -> str:  # 每一行在最后一列（XFD）都有一个带格式的空格子
        return re.sub(r'(<row r="(\d+)"[^>]*>)(.*?)</row>', lambda m: f'{m[1]}{m[3]}<c r="XFD{m[2]}" s="0"/></row>', xml)

    data = rewrite_sheet(make_xlsx({"明细": [["1"]] * 30_000}), far)
    started = clock.perf_counter()
    result = read(data, XLSX, "样例.xlsx")
    assert clock.perf_counter() - started < 8  # 以前每一行补到一万六千多格，一万行要二十来秒
    assert result.text.split("\n")[:3] == ["[工作表 明细]", "1", "1"]
