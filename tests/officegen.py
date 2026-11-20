"""测试用的 Excel、Word 文件，内容都是编的。

xlsx 用 openpyxl 写，docx 用 python-docx 写。老的 .xls 没有现成的库能写（只装了读的 xlrd），这里自己拼一个最小的：
一个 OLE2 复合文件，里面一个 Workbook 流（BIFF8：工作簿的 BOF、两个 XF（常规、h:mm）、每个工作表的 BOUNDSHEET、EOF，
再是每个工作表的 BOF、DIMENSIONS、一格一格的 LABEL、NUMBER、BOOLERR、EOF）。流补到 4096 字节以上，就不用小流
（mini stream）了。

python-docx 写不出来的（内容控件、修订、文本框、竖着合并的格子），用 docx_with_body 直接写 <w:body> 里的 XML。
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import time

_FREE, _END, _FAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF


def make_xlsx(sheets: dict[str, Sequence[Sequence[object]]], *, formula: str | None = None) -> bytes:
    """几个工作表，每个一组行。formula 给了就在第一个表的 Z1 写一个公式（openpyxl 写的公式没有算好的值）。"""
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(list(row))
    if formula is not None:
        workbook.worksheets[0]["Z1"] = formula
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def rewrite_sheet(data: bytes, change: Callable[[str], str]) -> bytes:
    """把 xlsx 里第一个工作表的 XML 改一下：写的范围不对、行号跳得很远的那种文件。"""
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            body = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                body = change(body.decode("utf-8")).encode("utf-8")
            target.writestr(info, body)
    return buffer.getvalue()


def docx_with_body(body_xml: str) -> bytes:
    """一个 docx，<w:body> 里是给的 XML（sectPr 照留）。命名空间用 python-docx 模板里声明了的：w、mc、wps、v、wp、a 这些。"""
    source = zipfile.ZipFile(io.BytesIO(make_docx(["占位"])))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "word/document.xml":
                xml, count = re.subn(r"<w:body>.*?(<w:sectPr)", lambda m: "<w:body>" + body_xml + m.group(1),
                                     data.decode("utf-8"), flags=re.S)
                assert count == 1
                data = xml.encode("utf-8")
            target.writestr(info, data)
    return buffer.getvalue()


def with_extra_part(data: bytes, name: str, size: int, *, content: bytes | None = None) -> bytes:
    """在压缩包里多放一个部件：给了 content 就放它，没给就放 size 字节全是 0 的（压缩以后很小，压缩炸弹的样子）。"""
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.writestr(name, bytes(size) if content is None else content)
    return buffer.getvalue()


def w_run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def w_p(*parts: str) -> str:
    """一段：给的是字就写成一个 run，给的是 XML（< 开头）就原样放进去。"""
    return "<w:p>" + "".join(part if part.startswith("<") else w_run(part) for part in parts) + "</w:p>"


def w_tc(inner: str, props: str = "") -> str:
    return f'<w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/>{props}</w:tcPr>{inner}</w:tc>'


def w_tr(*cells: str) -> str:
    return "<w:tr>" + "".join(cells) + "</w:tr>"


def w_tbl(rows: Sequence[str], columns: int) -> str:
    grid = '<w:gridCol w:w="2000"/>' * columns
    return f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(rows)}</w:tbl>'


def make_docx(paragraphs: Sequence[str], table: Sequence[Sequence[str]] = (), *, header: str | None = None) -> bytes:
    """几段话，后面一个表格（可以没有），header 给了就写进页眉。"""
    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for row, values in zip(grid.rows, table, strict=True):
            for cell, value in zip(row.cells, values, strict=True):
                cell.text = value
    if header is not None:
        document.sections[0].header.paragraphs[0].text = header
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _record(kind: int, payload: bytes) -> bytes:
    return struct.pack("<HH", kind, len(payload)) + payload


def _bof(sheet: bool) -> bytes:
    return _record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010 if sheet else 0x0005, 0, 0, 0, 0x0006))


@dataclass(frozen=True)
class XlsError:
    """.xls 里的错误值格子（BOOLERR）：0x07 是 #DIV/0!，0x2A 是 #N/A。"""

    code: int


def _xf(number_format: int) -> bytes:
    """一个 XF（格式）记录：number_format 是内置的数字格式编号，0 常规，20 是 h:mm。"""
    return _record(0x00E0, struct.pack("<HHHBBBBIiH", 0, number_format, 0x0001, 0x20, 0, 0, 0, 0, 0, 0x20C0))


_XFS = _xf(0) + _xf(20)  # 第 0 个常规，第 1 个 h:mm（只有时刻的格子用它）


def _short_string(text: str) -> bytes:
    data = text.encode("utf-16-le")
    return struct.pack("<BB", len(text), 1) + data


def _cell_records(rows: Sequence[Sequence[object]]) -> bytes:
    out = b""
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, time):  # 只有时刻：一天里的几分之几，格式是 h:mm
                fraction = (value.hour * 3600 + value.minute * 60 + value.second) / 86400
                out += _record(0x0203, struct.pack("<HHHd", row_index, column, 1, fraction))
            elif isinstance(value, XlsError):
                out += _record(0x0205, struct.pack("<HHHBB", row_index, column, 0, value.code, 1))
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                out += _record(0x0203, struct.pack("<HHHd", row_index, column, 0, float(value)))
            else:
                text = str(value).encode("utf-16-le")
                out += _record(0x0204, struct.pack("<HHHHB", row_index, column, 0, len(str(value)), 1) + text)
    return out


def _workbook_stream(sheets: dict[str, Sequence[Sequence[object]]]) -> bytes:
    names = list(sheets)
    bodies = []
    for rows in sheets.values():
        width = max((len(row) for row in rows), default=0)
        dimensions = _record(0x0200, struct.pack("<IIHHH", 0, len(rows), 0, width, 0))
        bodies.append(_bof(True) + dimensions + _cell_records(rows) + _record(0x000A, b""))
    head_size = len(_bof(False)) + len(_XFS) + sum(len(_record(0x0085, b"\0" * 6 + _short_string(name))) for name in names) + 4
    globals_ = _bof(False) + _XFS
    offset = head_size
    for name, body in zip(names, bodies, strict=True):
        globals_ += _record(0x0085, struct.pack("<IBB", offset, 0, 0) + _short_string(name))
        offset += len(body)
    globals_ += _record(0x000A, b"")
    stream = globals_ + b"".join(bodies)
    return stream + b"\0" * max(0, 4200 - len(stream))


def _directory_entry(name: str, kind: int, child: int, start: int, size: int) -> bytes:
    raw = name.encode("utf-16-le") + b"\0\0" if name else b""
    entry = raw.ljust(64, b"\0")
    entry += struct.pack("<HBB", len(raw), kind, 1)
    entry += struct.pack("<III", _NOSTREAM, _NOSTREAM, child)
    entry += b"\0" * 16 + b"\0" * 4 + b"\0" * 16  # CLSID、状态、时间
    entry += struct.pack("<IQ", start, size)
    return entry


def make_ole2(stream_name: str, stream: bytes) -> bytes:
    """一个只装一个流的 OLE2 复合文件（512 字节一个扇区，流不小于 4096 字节）。"""
    sectors = (len(stream) + 511) // 512
    fat = [_FAT, _END] + [index + 1 for index in range(2, 2 + sectors - 1)] + [_END]
    fat += [_FREE] * (128 - len(fat))
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16
    header += struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6) + b"\0" * 6
    header += struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, _END, 0, _END, 0)
    header += struct.pack("<109I", 0, *([_FREE] * 108))
    directory = _directory_entry("Root Entry", 5, 1, _END, 0) + _directory_entry(stream_name, 2, _NOSTREAM, 2, len(stream))
    directory += (_directory_entry("", 0, _NOSTREAM, 0, 0)) * 2
    body = struct.pack("<128I", *fat) + directory + stream.ljust(sectors * 512, b"\0")
    return header + body


def make_xls(sheets: dict[str, Sequence[Sequence[object]]]) -> bytes:
    """老的 .xls（BIFF8），格子只有字和数。"""
    return make_ole2("Workbook", _workbook_stream(sheets))


def make_encrypted_office() -> bytes:
    """要密码的 xlsx、docx 长的样子：一个 OLE2，里面装着 EncryptedPackage 流（里面的字节是编的）。"""
    return make_ole2("EncryptedPackage", b"\x11" * 4200)
