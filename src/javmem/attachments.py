"""附件的正文（packet-spec 第 3 节“附件小节”）：哪些类型读、怎么转成文字、太大了怎么办。

1.2 转换器和邮件导入共用。打包方先拿到附件的字节，交给 read_attachment，拿回整理好、截好、打过码的正文，
再按 javmem.packet.render.attachment_block 写进包里。

| 附件 | 怎么读 |
| --- | --- |
| 纯文字（text/plain、text/csv、text/tab-separated-values、text/markdown） | 按声明的编码解，没声明的先 UTF-8，再 GB18030，最后 latin-1 |
| HTML（text/html） | 和 HTML 邮件一样转成纯文字（javmem.textconv） |
| PDF | 只取文字层，不做文字识别。要密码才打得开、或者写着不许提取文字的不读；读出来是乱码的页不收 |
| Excel（.xlsx、.xlsm 用 openpyxl，.xls 用 xlrd） | 只取单元格的值（公式取存着的算好的值），不取公式。一个工作表一段，先写“[工作表 名字]”，一行一行写，格子之间用 tab，空行不写，一格字都没有的表不写。小数照 Excel 显示的写（15 位有效数字），只有时刻的写时刻，错误值写 #DIV/0! 这样 |
| Word（.docx，python-docx） | 正文的段落和表格按原来的先后写（内容控件、插入的修订、域的结果都是正文），表格一行一行写，格子之间用 tab，合并的格子只写一次；页眉、页脚、脚注、批注、文本框、删掉的修订、域代码不要。老的 .doc 先不读（unsupported） |
| 图片、别的类型 | 不读，只记名字 |

类型说不清（application/octet-stream 这类）的再看扩展名；写着 Excel、Word 类型、扩展名却是纯文字、网页的
（Windows 把 .csv 记成 application/vnd.ms-excel），按扩展名读。文件超过 20 MB 不打开，xlsx、docx 解开以后超过
200 MB 的也不打开（压缩炸弹；xlsx 的工作表边解边读，不算）；转出来超过 2 万字的，
在 2 万字以内最后一个换行处截断，那个换行离 2 万字太远（截下来不到一半）的在 2 万字处截（D93）。PDF 一页一页读，收到超过 2 万字就停，后面的页不读。

PDF 的字体没有对照表时，读出来是一串符号（字形名 a1、a2 对到装饰符号）或者 (cid:12) 这种占位：
真实数据里一份报告 20 页有 5 页 92% 以上是这种。一页里乱码占一半以上就不收这一页，记 garbled。

PDF 在子进程里读，到点（PDF_SECONDS）就停，记 timeout：几 KB 的 PDF 就能让 pdfminer 跑上几个小时
（表单对象一层画两次下一层；一页几千个散开的字，排版分析接近平方），邮件导入要读完一批才投包，会整批卡住。
Excel、Word 也一样在子进程里读，时限、CPU、内存和 PDF 的一样（D91）：xlsx、docx 是压缩包，几 KB 能解出几 GB。
先看文件头：xlsx、docx 是 ZIP，xls、doc 是 OLE2；OLE2 里装着 EncryptedPackage 的是要密码的 xlsx、docx，记 encrypted。

- 子进程用 -I 起（不看 PYTHONPATH 这些环境变量、不从当前目录找模块），sys.path 是父进程的（D79 第 11 条）：
  依赖靠 PYTHONPATH、pip install --user 装的，子进程也找得到。起不来（找不到 pdfminer、javmem）抛 PdfUnavailable，
  调用方停下报出来，不悄悄记成 unreadable（那样每个 PDF 都成了 unreadable，邮件还记成导过了）。
- 一封信里的 PDF、Excel、Word 共用一个 PdfBudget（D79 第 12 条）：有一个读到点还没读完，这封信后面的不再读，
  也记 timeout。一封信带上百个故意做的慢文件，也只耗一个文件的时限。
- 子进程自己也有时限（D79 第 15 条），比父进程的多几秒：父进程被杀了（计划任务到点、崩了），它也会自己停。
  Linux、macOS 用 RLIMIT_CPU，再加一个看墙上时间的线程（Windows 上只有这个）。内存（解压炸弹）也限 2 GB：Linux、macOS
  上子进程自己设 RLIMIT_AS；Windows 上起来以后、喂文件以前放进一个作业对象（run_child，D79 第 7 条、D93 第 16 条，U3）。

读的时候出了任何想不到的错都算 unreadable：外来的文件什么样都有，一个附件不能让整批停下。
"""

from __future__ import annotations

import codecs
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from javmem.config import ConfigError
from javmem.packet.model import (
    NONE_REASONS,
    REASON_ENCRYPTED,
    REASON_GARBLED,
    REASON_IMAGE,
    REASON_MISSING,
    REASON_NO_TEXT,
    REASON_TIMEOUT,
    REASON_TOO_LARGE,
    REASON_TOO_LONG,
    REASON_UNREADABLE,
    REASON_UNSUPPORTED,
    TEXT_FULL,
    TEXT_NONE,
    TEXT_TRUNCATED,
    TRUNCATED_REASONS,
)
from javmem.packet.render import clean_text
from javmem.redaction import RedactionPattern, redact
from javmem.textconv import decode_bytes, html_to_text

MAX_FILE_BYTES = 20 * 1024 * 1024  # 文件超过 20 MB 不打开
MAX_TEXT_CHARS = 20_000  # 转出来的文字最多收这么多字，和规范第 2 节原话的 2 万字一样
PDF_SECONDS = 60  # 一个 PDF 最多读这么久。平常的几十页报告几秒就读完了
_CHILD_MEMORY = 2 * 1024**3  # 读附件的子进程最多用这么多内存（Linux、macOS 子进程自己设 RLIMIT_AS；Windows 放进作业对象）
_CHILD_GRACE = 5  # 子进程自己的时限比父进程的多这么多秒：平常是父进程到点杀掉它，父进程没了它自己停
CANNOT_START = 70  # 读 PDF 的子进程起不来（找不到 javmem、pdfminer）时的退出码
STOPPED_BY_ITSELF = 71  # 子进程自己到点停下的退出码（看墙上时间的那个线程）
_SIGXCPU = 24  # Linux、macOS 上 CPU 时间到了的信号；被它停掉的子进程退出码是 -24

REASONS = (*TRUNCATED_REASONS, *NONE_REASONS)

KIND_TEXT = "text"
KIND_HTML = "html"
KIND_PDF = "pdf"
KIND_EXCEL = "excel"
KIND_WORD = "word"
KIND_IMAGE = "image"
KIND_OTHER = "other"
CHILD_KINDS = (KIND_PDF, KIND_EXCEL, KIND_WORD)  # 在子进程里读的

_TEXT_TYPES = frozenset({"text/plain", "text/csv", "text/tab-separated-values", "text/markdown", "text/x-markdown"})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_PDF_TYPES = frozenset({"application/pdf", "application/x-pdf"})
_EXCEL_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.ms-excel",
        "application/excel",
        "application/x-excel",
        "application/x-msexcel",
    }
)
# application/msword 是老的 .doc，也常常被写在 .docx 上：先当 Word，看了文件头再说
_WORD_TYPES = frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"})
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# 说不清是什么的类型，再看扩展名
_VAGUE_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        "application/unknown",
        "application/x-download",
        "application/force-download",
        "application/download",
    }
)
_BY_EXTENSION = {
    **dict.fromkeys((".txt", ".text", ".csv", ".tsv", ".md", ".markdown", ".log"), KIND_TEXT),
    **dict.fromkeys((".html", ".htm", ".xhtml"), KIND_HTML),
    ".pdf": KIND_PDF,
    **dict.fromkeys((".xlsx", ".xlsm", ".xls"), KIND_EXCEL),
    **dict.fromkeys((".docx", ".doc"), KIND_WORD),
    **dict.fromkeys(
        (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff", ".svg"), KIND_IMAGE
    ),
}
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# 换页符这类分隔换成换行；别的控制字符换成空格，tab 留着
_SEPARATORS = re.compile("[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_CONTROLS = re.compile("[\x01-\x08\x0e-\x1b\x1f\x7f]")
# PDF 的字体没有对照表时，pdfminer 读出来的是 (cid:12) 这种占位，或者按字形名对成一串符号（a1、a2 对到
# 杂项符号、装饰符号），或者私用区的字。一页里这些占了一半以上，这一页就是乱码，不收
_CID = re.compile(r"\(cid:\d+\)")
_GARBLED = re.compile("[\u2600-\u27bf\u2b00-\u2bff\ue000-\uf8ff\ufffd]")
GARBLED_SHARE = 0.5

# pdfminer 碰到写得不规范的 PDF 会打一堆警告，里面有文件对象的名字。不要它们，出错了我们自己记
logging.getLogger("pdfminer").setLevel(logging.ERROR)
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttachmentText:
    """读一个附件的结果。text 是整理好、截好、打过码的正文，没收正文的是空的。"""

    state: str  # full / truncated / none
    reason: str | None = None
    text: str = ""
    chars: int = 0  # 转出来一共多少字，截断以前的。PDF 读到超过上限就停，只数到停下的那一页
    redactions: Mapping[str, int] = field(default_factory=dict)


def _none(reason: str) -> AttachmentText:
    return AttachmentText(TEXT_NONE, reason)


class PdfUnavailable(ConfigError):
    """读 PDF（Excel、Word）的子进程起不来：找不到 pdfminer（openpyxl、xlrd、python-docx）、javmem，或者起不了 Python。
    每个这样的文件都会这样，不是这个文件的毛病：调用方停下报出来（命令行退出码 2），装好再跑，不记成 unreadable。"""


@dataclass
class PdfBudget:
    """一封信里的 PDF、Excel、Word 共用一个（D79 第 12 条）：有一个读到点还没读完，后面的不再读，也记 timeout。"""

    timed_out: bool = False
    skipped: int = 0  # 因为前面的超时了没读的


def kind_of(media_type: str, name: str) -> str:
    """按类型认，说不清的再看扩展名。"""
    declared = media_type.split(";", 1)[0].strip().lower()
    if declared in _TEXT_TYPES:
        return KIND_TEXT
    if declared in _HTML_TYPES:
        return KIND_HTML
    if declared in _PDF_TYPES:
        return KIND_PDF
    if declared in _EXCEL_TYPES or declared in _WORD_TYPES:
        # Windows 把 .csv 记成 application/vnd.ms-excel，网页另存的也常写成 Office 的类型：扩展名是纯文字、网页的按扩展名读
        by_name = _by_extension(name)
        if by_name in (KIND_TEXT, KIND_HTML):
            return by_name
        return KIND_EXCEL if declared in _EXCEL_TYPES else KIND_WORD
    if declared.startswith("image/"):
        return KIND_IMAGE
    if declared in _VAGUE_TYPES:
        return _by_extension(name)
    return KIND_OTHER


def _by_extension(name: str) -> str:
    dot = name.rfind(".")
    return _BY_EXTENSION.get(name[dot:].strip().lower(), KIND_OTHER) if dot >= 0 else KIND_OTHER


def _decode(data: bytes, charset: str | None) -> str:
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return data.decode(codec, errors="replace")
    return decode_bytes(data, [charset])[0]


def charset_of(media_type: str) -> str | None:
    """类型里写的编码：text/plain; charset=shift_jis。"""
    message = EmailMessage()
    try:
        message["Content-Type"] = media_type
        return message.get_content_charset()
    except Exception:  # noqa: BLE001 写得乱的类型，email 包会抛各种异常
        return None


def garbled_page(text: str) -> bool:
    """一页读出来的字里，乱码（符号、私用区、U+FFFD、(cid:N)）占了一半以上。空白不算字。"""
    cids = len(_CID.findall(text))
    visible = sum(1 for char in _CID.sub("", text) if not char.isspace()) + cids
    return visible > 0 and (len(_GARBLED.findall(text)) + cids) >= GARBLED_SHARE * visible


PDFMINER_MODULES = (
    "pdfminer.converter",
    "pdfminer.layout",
    "pdfminer.pdfdocument",
    "pdfminer.pdfinterp",
    "pdfminer.pdfpage",
    "pdfminer.pdfparser",
)


def load_pdfminer() -> None:
    """把 _pdf_text 要用的 pdfminer 模块都导一遍。子进程起来先调它：哪一个连带的依赖坏了（pdfminer.pdfdocument
    一导入就要 cryptography，Windows 上常见“DLL load failed”），都在这里报出来，算起不来（审阅第 6 条）。"""
    import importlib

    for name in PDFMINER_MODULES:
        importlib.import_module(name)


def _pdf_text(data: bytes, limit: int) -> tuple[str, int, bool] | None:
    """PDF 的文字层、没收的乱码页有几页、是不是还有页没读。要密码、或者写着不许提取文字的返回 None。

    一页一页读，乱码的页不收，收到超过 limit 字就停。(cid:N) 这种占位去掉。用到的模块见 PDFMINER_MODULES。
    """
    from pdfminer.converter import TextConverter
    from pdfminer.layout import LAParams
    from pdfminer.pdfdocument import PDFDocument, PDFEncryptionError, PDFPasswordIncorrect
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    parser = PDFParser(io.BytesIO(data))
    try:
        document = PDFDocument(parser, password="")
    except (PDFPasswordIncorrect, PDFEncryptionError):
        return None
    if not document.is_extractable:  # 加了密、权限里写着不许提取文字（不用密码也能打开的那种）
        return None
    output = io.StringIO()
    manager = PDFResourceManager()
    device = TextConverter(manager, output, laparams=LAParams())
    pages: list[str] = []
    kept = garbled = 0
    more = False
    try:
        interpreter = PDFPageInterpreter(manager, device)
        iterator = PDFPage.create_pages(document)
        for page in iterator:
            start = output.tell()
            interpreter.process_page(page)
            output.seek(start)
            text = output.read()
            if garbled_page(text):
                garbled += 1
                continue
            pages.append(_CID.sub("", text))
            kept += len(pages[-1])
            if kept > limit:
                # 整理以后可能又不到 limit 了，可后面的页没读：还是截断了
                more = next(iterator, None) is not None
                break
    finally:
        device.close()
    return "".join(pages), garbled, more


# 子进程起来先导一遍的（load_reader）：openpyxl 的工作表解析器、lxml 是直接用的，也在里面
OFFICE_MODULES = {KIND_EXCEL: ("openpyxl", "openpyxl.worksheet._reader", "xlrd"), KIND_WORD: ("docx", "lxml.etree")}
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024  # xlsx、docx 解开以后最多这么大（不算 xlsx 的工作表），超了不打开：压缩炸弹
# 还要比文件本身大这么多倍以上，才算压缩炸弹（D93 第 24 条）：没压缩的大图（BMP、TIFF）、样式表特别大的正常文件照读。
# 文件不超过 20 MB，压得不狠的解开以后也就 1 GB 以内
_BOMB_RATIO = 50
_MAX_GAP = 50  # 一行里两个有字的格子中间最多写这么多个空格子（故意把一格放到一万多列以外的那种，只写这么多）
# workbook.xml.rels 里的一条关系，和它的 Type、Target；[Content_Types].xml 里的一条 Override，和它的 PartName、
# ContentType（只用正则找，父进程里不起 XML 解析器）
_RELATIONSHIP = re.compile(rb"<(?:\w+:)?Relationship\b([^<>]*)>")
_REL_ATTRIBUTE = re.compile(rb'\b(Type|Target)\s*=\s*"([^"<>]*)"')
_OVERRIDE = re.compile(rb"<(?:\w+:)?Override\b([^<>]*)>")
_OVERRIDE_ATTRIBUTE = re.compile(rb'\b(PartName|ContentType)\s*=\s*"([^"<>]*)"')
_WORKSHEET_TYPE = b"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
# 工作表的名字：xl/worksheets/ 下面直接放着的 .xml。样式表、主题、工作簿这些 openpyxl 按固定的名字、按
# [Content_Types].xml 整个读进来的，名字都不在这里（D93 第二遍审阅第 1 条）
_WORKSHEET_NAME = re.compile(r"xl/worksheets/[^/]+\.xml", re.IGNORECASE)
_RELS_LIMIT = 1024 * 1024  # workbook.xml.rels、[Content_Types].xml 最多这么大，再大的不信它，一个部件都不放过
# 要密码的 xlsx、docx 是一个 OLE2，目录里有一项叫 EncryptedPackage 的流。目录项 128 字节对齐：名字（UTF-16，带结尾的 0）
# 放在 64 字节里，名字长度 34（0x22），类型 2（流）。名字后面那几个字节规范没要求补 0，不看（D93 第 23 条）
_ENCRYPTED_ENTRY = "EncryptedPackage".encode("utf-16-le") + b"\0\0"


def load_reader(kind: str) -> None:
    """子进程起来先把要用的库都导一遍：PDF 是 pdfminer（见 load_pdfminer），Excel 是 openpyxl、xlrd，Word 是 python-docx。
    哪一个没装、连带的依赖坏了（python-docx 要 lxml），都在这里报出来，算起不来。"""
    import importlib

    if kind == KIND_PDF:
        load_pdfminer()
        return
    for name in OFFICE_MODULES[kind]:
        importlib.import_module(name)


def _encrypted_ooxml(data: bytes) -> bool:
    """要密码才打得开的 xlsx、docx：一个 OLE2 文件，目录里有一项 EncryptedPackage 流。只认目录项：格子里、
    老 .doc 的正文里写着这个词的不算（审阅第 11 条）。"""
    if not data.startswith(_OLE2_MAGIC):
        return False
    start = 0
    while (found := data.find(_ENCRYPTED_ENTRY, start)) >= 0:
        entry = data[found : found + 67]
        if found % 128 == 0 and len(entry) == 67 and entry[64:67] == b"\x22\x00\x02":
            return True
        start = found + 1
    return False


def _unzipped_too_large(data: bytes, kind: str) -> bool:
    """xlsx、docx 解开以后超过 MAX_UNZIPPED_BYTES（审阅第 9 条）：python-docx 把包里每个部件都解开读进内存，图片也一样，
    几 MB 的文件能解出几十 GB（Windows 上子进程不限内存）。openpyxl 只读模式下工作表本身是边解边读的，不算它们；
    工作表的 _rels 它是整个读进来的，照算（D93 第 16 条）。看压缩包目录里写的大小：zipfile 读的时候不会读出比它多的字节。
    压缩包坏了的交给读的库去报。"""
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            sheets = _sheet_parts(archive) if kind == KIND_EXCEL else set()
            total = sum(info.file_size for info in archive.infolist() if info.filename not in sheets)
    except Exception:  # noqa: BLE001
        return False
    # 比的是文件真的大小（D93 审阅第 2 条）：目录里写的“压缩以后多大”随便写，多放一项写成 4 GB，比例就过了
    return total > MAX_UNZIPPED_BYTES and total > _BOMB_RATIO * len(data)


def _sheet_parts(archive: Any) -> set[str]:
    """openpyxl 只读模式下边解边读的工作表，这几样都要对得上才算（别的部件它整个读进来，要算）：
    - workbook.xml.rels 里有一条工作表关系指着它，没有别的关系（共享字串表、外部链接……）指着它；
    - [Content_Types].xml 没把它写成别的东西：共享字串表、工作簿是 openpyxl 按这个找的（D93 审阅第 13 条）；
    - 名字是 xl/worksheets/ 下面的 .xml：样式表、主题按固定的名字找。
    这两份清单都是文件自己写的，只信对得上的那一部分。读不出来（CRC 不对这类）、太大的，一个都不放过（D93 第二遍审阅第 1 条：
    多写一条指着共享字串表的工作表关系、把 workbook.xml.rels 的 CRC 写错，以前都能让共享字串表不算）。"""
    try:
        relationships = _small_part(archive, "xl/_rels/workbook.xml.rels")
        types = _small_part(archive, "[Content_Types].xml")
    except Exception:  # noqa: BLE001
        return set()
    if relationships is None or types is None:
        return set()
    sheets: set[str] = set()
    others: set[str] = set()
    for match in _RELATIONSHIP.finditer(relationships):
        attributes = dict(_REL_ATTRIBUTE.findall(match.group(1)))
        target = _part_name(attributes.get(b"Target", b"").decode("utf-8", "replace"))
        (sheets if attributes.get(b"Type", b"").endswith(b"/worksheet") else others).add(target)
    for match in _OVERRIDE.finditer(types):
        attributes = dict(_OVERRIDE_ATTRIBUTE.findall(match.group(1)))
        if attributes.get(b"ContentType", b"") != _WORKSHEET_TYPE:
            others.add(attributes.get(b"PartName", b"").decode("utf-8", "replace").lstrip("/"))
    return {name for name in sheets - others if _WORKSHEET_NAME.fullmatch(name)}


def _small_part(archive: Any, name: str) -> bytes | None:
    """压缩包里一个小部件的全文；没有、比 _RELS_LIMIT 大的给 None。读坏了照样抛出去。"""
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _RELS_LIMIT:
        return None
    return archive.read(info)


def _part_name(target: str) -> str:
    """workbook.xml.rels 里写的 Target 换成压缩包里的名字：/ 开头的从包的根算，别的从 xl/ 算。"""
    import posixpath

    return target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)


def _cell(value: object) -> str:
    """一个单元格写成一段字：小数照 Excel 显示的，最多 15 位有效数字（存的是 41.249999999999993，显示 41.25），
    整数样的去掉 .0；日期写成 2026-11-03，带时刻的加上时刻（有秒的写到秒）；只有时刻的写成 08:30:00；
    换行、连着的空白换成一个空格。"""
    import datetime as _datetime

    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return format(value, ".15g")
    if isinstance(value, _datetime.datetime):
        if value.time() == _datetime.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="seconds" if value.second or value.microsecond else "minutes")
    if isinstance(value, _datetime.time):
        return value.isoformat(timespec="seconds")
    if isinstance(value, _datetime.date):
        return value.isoformat()
    return " ".join(str(value).split())


class _Collector:
    """收一行一行的字，收到超过 limit 字就说够了（more：后面还有字没收）。空行等后面真来了字才补上：
    末尾的空行不写，也不会因为够了以后又来一个空行就记成截断。一行、一格太长的截到正好超过 limit，记 more：
    子进程最多交回 limit 字出头（审阅第 3 条：一格几千万字，以前整格交回，父进程吃掉几 GB 内存）。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.lines: list[str] = []
        self.chars = 0
        self.more = False
        self._blank = False

    def room(self) -> int:
        """再收多少字就够了：多收一个字，好知道是截了。"""
        return max(0, self.limit - self.chars + 1)

    def add(self, line: str) -> bool:
        """加一行，返回还要不要接着读。"""
        if not line:
            self._blank = bool(self.lines)
            return True
        if self.chars > self.limit:
            self.more = True
            return False
        if self._blank:
            self.lines.append("")
            self.chars += 1
            self._blank = False
        room = self.room()
        if len(line) > room:
            line, self.more = line[:room], True
        self.lines.append(line)
        self.chars += len(line) + 1
        return not self.more

    def cell(self, value: object) -> str:
        """一个格子（_cell）。字太长的先截掉，不把几千万字的格子整个拆一遍；截的时候按收掉空白以后的字数算
        （D93 第 18 条：一格前面 2.5 万个空格加“备注”，以前截掉的只是空格，后面的行、表都不读了）。"""
        cap = self.limit + 1
        if not isinstance(value, str) or len(value) <= cap:
            return _cell(value)
        words: list[str] = []
        size = position = 0
        while (found := _NON_BLANK.search(value, position)) is not None:
            start = found.start()
            blank = _BLANK.search(value, start)
            stop = blank.start() if blank else len(value)
            if size + stop - start > cap:
                words.append(value[start : start + cap - size])
                self.more = True
                break
            words.append(value[start:stop])
            size += stop - start + 1
            position = stop
        return " ".join(words)

    def row(self, cells: Sequence[str]) -> bool:
        """一行格子：去掉末尾的空格子，整行都是空的不写。"""
        cells = list(cells)
        while cells and not cells[-1]:
            cells.pop()
        return self.add("\t".join(cells)) if any(cells) else not self.more


def _sheet(collector: _Collector, title: str, rows: Iterable[Sequence[str]]) -> bool:
    """一个工作表：先写“[工作表 名字]”，再一行一行写，和前一个表之间空一行；一格字都没有的表不写。
    返回还要不要接着读。"""
    titled = False
    for cells in rows:  # 一格太长、截了的（collector.more），这一行照样写上再停
        if not any(cells):
            if collector.more:
                return False
            continue
        if not titled:
            collector.add("")
            if collector.chars > collector.limit:  # 前面的表已经够了
                collector.more = True
                return False
            collector.add(f"[工作表 {title}]")  # 这一行里有一格截了（more）也照写表名，下面接着写这一行（D93 第 17 条）
            titled = True
        if not collector.row(cells):
            return False
    return not collector.more


def _sparse_row(cells: Iterable[Mapping[str, object]], collector: _Collector) -> list[str]:
    """openpyxl 解析出来的一行（只有文件里写了的格子）：有值的按列放，中间空的补上，最多补 _MAX_GAP 个。
    同一列写了两格的只写后面那个（D93 第 22 条，和 Excel 一样）。"""
    by_column: dict[int, object] = {}
    for cell in cells:
        if cell["value"] is not None:
            by_column[int(cell["column"])] = cell["value"]  # type: ignore[call-overload]
    row: list[str] = []
    last = 0
    for column, value in sorted(by_column.items()):
        if column - last > 1:
            row.extend([""] * min(column - last - 1, _MAX_GAP))
        row.append(collector.cell(value))
        last = max(last, column)
    return row


def _xlsx_text(data: bytes, collector: _Collector) -> None:
    """.xlsx、.xlsm：openpyxl 的只读模式打开，每个工作表直接用它的解析器一行一行读（审阅第 12、16 条）。
    iter_rows 会把每一行补到这一行最后一格那么宽（一格放在 XFD 列，一行就是一万六千多格，三万行到点），
    文件里行号倒着写的那一行还会悄悄丢掉。WorkSheetParser 是 openpyxl 3.1.5 里的（版本写死了，换版本时
    tests/test_office*.py 会先报出来）；文件里写的范围（dimension）不看，只读真有的格子。"""
    import warnings

    from openpyxl import load_workbook
    from openpyxl.worksheet._reader import WorkSheetParser

    warnings.filterwarnings("ignore")  # openpyxl 碰到它不认识的扩展会打警告，不要它们
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            with sheet._get_source() as source:
                parser = WorkSheetParser(
                    source,
                    sheet._shared_strings,
                    data_only=True,
                    epoch=workbook.epoch,
                    date_formats=workbook._date_formats,
                    timedelta_formats=workbook._timedelta_formats,
                )
                rows = (_sparse_row(cells, collector) for _number, cells in parser.parse())
                if not _sheet(collector, sheet.title, rows):
                    return
    finally:
        workbook.close()


def _xls_text(data: bytes, collector: _Collector) -> bool:
    """老的 .xls。要密码的返回 False。xlrd 的警告（文件大小不是 512 的整数倍这类）默认打到标准输出，混进子进程交回的结果，
    读得好好的文件成了 unreadable（审阅第 1 条）：写进 os.devnull。"""
    import datetime as _datetime

    import xlrd

    with open(os.devnull, "w", encoding="utf-8") as quiet:
        try:
            book = xlrd.open_workbook(file_contents=data, on_demand=True, logfile=quiet)
        except xlrd.XLRDError as error:
            if "encrypt" in str(error).lower():
                return False
            raise

        def text(kind: int, value: object) -> str:
            if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                return ""
            if kind == xlrd.XL_CELL_ERROR:  # 和 .xlsx 一样写成 #DIV/0!（审阅第 14 条）
                return xlrd.error_text_from_code.get(value, "#ERROR!")
            if kind == xlrd.XL_CELL_BOOLEAN:
                return _cell(bool(value))
            if kind == xlrd.XL_CELL_DATE and isinstance(value, float):
                if 0 <= value < 1:  # 只有时刻（考勤表的 08:30）：不写 1899-12-31，那天文件里没有（审阅第 7 条）
                    seconds = round(value * 86400) % 86400
                    return _cell(_datetime.time(seconds // 3600, seconds // 60 % 60, seconds % 60))
                try:
                    return _cell(xlrd.xldate_as_datetime(value, book.datemode))
                except (ValueError, OverflowError):  # 写成日期格式的数不像日期
                    return _cell(value)
            return collector.cell(value)

        try:
            for index in range(book.nsheets):
                sheet = book.sheet_by_index(index)
                rows = (
                    [text(kind, value) for kind, value in zip(sheet.row_types(row), sheet.row_values(row), strict=True)]
                    for row in range(sheet.nrows)
                )
                if not _sheet(collector, sheet.name, rows):
                    return True
                book.unload_sheet(index)
        finally:
            book.release_resources()
    return True


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_W_P, _W_TBL, _W_TR, _W_TC, _W_T = (_W + name for name in ("p", "tbl", "tr", "tc", "t"))
# 段里不收的：属性（pPr 里的 tab 是制表位，不是字）、删掉的修订、挪走的原文、文本框、域代码、脚注尾注批注的引用、
# 注音（w:rt，D93 第 20 条；w:rubyBase 里的正文照收）
_SKIP_IN_PARAGRAPH = frozenset(
    _W + name
    for name in (
        "pPr", "rPr", "del", "moveFrom", "txbxContent", "instrText", "delText", "delInstrText",
        "footnoteReference", "endnoteReference", "commentReference", "rt",
    )
)
# mc:AlternateContent 的几个 Choice 和 Fallback 是同一样东西的几种写法（_not_chosen）
_MC_PARTS = frozenset((_MC + "Choice", _MC + "Fallback"))
_W_TABS = frozenset((_W + "tab", _W + "ptab"))
_NON_BLANK = re.compile(r"\S")
_BLANK = re.compile(r"\s")
# 段落里连着 9 个以上的空格写成一个（D93 第 18 条）：一段末尾 3 万个空格，以前也算 3 万字，把后面的段落挤掉了
_LONG_BLANKS = re.compile(r"[ \u00a0\u3000]{9,}")
_BLANK_RUN = re.compile(r"[ \u00a0\u3000]+")  # 算字数时一串空格只算一个，跨了几个 run 的也一样（D93 审阅第 10 条）
_W_BREAKS = frozenset((_W + "br", _W + "cr"))
# 正文、表格、行里这几样只是把段落、表格、行、格子包起来（内容控件、自定义标记、插入的修订、智能标记），进到里面去读
_W_WRAPPERS = frozenset(_W + name for name in ("sdt", "sdtContent", "customXml", "ins", "moveTo", "smartTag"))


def _placeholder(element: Any) -> bool:
    """还没填的内容控件（w:sdt 的属性里有 showingPlcHdr）：里面显示的是“单击此处输入日期”这类提示字，不收（D93 第 20 条）。"""
    if element.tag != _W + "sdt":
        return False
    properties = element.find(_W + "sdtPr")
    marker = properties.find(_W + "showingPlcHdr") if properties is not None else None
    # 开关写成 val="0"、"false"、"off" 的是“没在显示提示字”，里面是填了的字（D93 审阅第 4 条）
    return marker is not None and marker.get(_W + "val", "true").lower() not in ("0", "false", "off")


def _not_chosen(element: Any) -> bool:
    """mc:AlternateContent 里不读的那几份（D93 第 19 条）：只读第一个 Choice，没有 Choice 才读 Fallback。
    正文这一层、格子里、段里都照这样，不会两份都收，也不会两份都丢。"""
    parent = element.getparent()
    if parent is None:
        return False
    first = parent.find(_MC + "Choice")
    if element.tag == _MC + "Fallback":
        return first is not None
    return element is not first


def _paragraph_text(paragraph: Any, cap: int) -> tuple[str, bool]:
    """一段的字，按文档里的先后：w:t 的字、tab、换行，进到超链接、修订、内容控件、域、智能标记里去，_SKIP_IN_PARAGRAPH 的不收。
    收到超过 cap 字就停，返回（字，是不是截了）。"""
    from lxml import etree

    parts: list[str] = []
    size = 0
    blank_before = False  # 上一段是不是空格结尾：接着的空格和它连成一串，只算一个
    walker = etree.iterwalk(paragraph, events=("start",))
    for _event, element in walker:
        tag = element.tag
        if tag in _SKIP_IN_PARAGRAPH or (tag in _MC_PARTS and _not_chosen(element)) or _placeholder(element):
            walker.skip_subtree()
            continue
        if tag == _W_T:
            piece = element.text or ""
        elif tag in _W_TABS:
            piece = "\t"
        elif tag in _W_BREAKS:
            piece = "\n"
        elif tag == _W + "noBreakHyphen":
            piece = "-"
        else:
            continue
        parts.append(piece)
        counted = _BLANK_RUN.sub(" ", piece)
        if blank_before and counted.startswith(" "):
            counted = counted[1:]
        if counted:
            blank_before = counted.endswith(" ")
        size += len(counted)
        if size > cap:
            return _LONG_BLANKS.sub(" ", "".join(parts))[:cap], True
    return _LONG_BLANKS.sub(" ", "".join(parts)), False


def _wrapped(element: Any, tag: str, unfilled: bool = False) -> Iterator[tuple[Any, bool]]:
    """element 下面的 tag（表格的行、行里的格子），进到内容控件、自定义标记里去找。给出（那一行、那一格，是不是包在
    没填的内容控件里）：没填的格子要写空、占着那一列（D93 审阅第 3 条），不能整个跳过，后面的格子就往前挪了。"""
    for child in element.iterchildren():
        if child.tag == tag:
            yield child, unfilled
        elif child.tag in _W_WRAPPERS:
            yield from _wrapped(child, tag, unfilled or _placeholder(child))


def _merged_from_above(cell: Any) -> bool:
    """竖着合并、接着上面那格的（w:vMerge 没写 restart）。"""
    properties = cell.find(_W + "tcPr")
    merge = properties.find(_W + "vMerge") if properties is not None else None
    return merge is not None and merge.get(_W + "val", "continue") == "continue"


def _cell_text(cell: Any, cap: int) -> tuple[str, bool]:
    """一个格子里的字：每一段（连格子里套着的表格里的）按先后连起来，中间空一格。"""
    from lxml import etree

    parts: list[str] = []
    size = 0
    walker = etree.iterwalk(cell, events=("start",))
    for _event, element in walker:
        if (element.tag in _MC_PARTS and _not_chosen(element)) or _placeholder(element):
            walker.skip_subtree()
            continue
        if element.tag != _W_P:
            continue
        walker.skip_subtree()
        text, cut = _paragraph_text(element, max(0, cap - size))
        if text.strip():  # 空段落不算字（D93 第 18 条：格子里 2 万个空段落，以前把格子里的字挤掉了）
            parts.append(text.strip())
            size += len(parts[-1]) + 1
        if cut or size > cap:
            return " ".join(parts)[:cap], True
    return " ".join(parts), False


def _docx_table(table: Any, collector: _Collector) -> bool:
    """一个表格，一行一行写，格子之间用 tab。横着合并的格子（gridSpan）本来就是一格；竖着合并的（vMerge）字只写在最上面
    那格，下面的写空（审阅第 4、15 条：python-docx 的 row.cells 一层一层往上找合并的头，长的要几十秒、抛 RecursionError，
    对不齐还抛 ValueError，整个文件都丢）。返回还要不要接着读。"""
    for row, unfilled_row in _wrapped(table, _W_TR):
        if unfilled_row:  # 一整行都是没填的控件：只有提示字，不写
            continue
        cells = []
        for cell, unfilled in _wrapped(row, _W_TC):
            if unfilled or _merged_from_above(cell):
                cells.append("")
                continue
            text, cut = _cell_text(cell, collector.room())
            collector.more = collector.more or cut
            cells.append(_cell(text))
        if not collector.row(cells):
            return False
    collector.add("")
    return True


def _docx_blocks(container: Any, collector: _Collector) -> bool:
    """正文（或者内容控件里）的段落、表格，按原来的先后。返回还要不要接着读。"""
    for child in container.iterchildren():
        tag = child.tag
        if tag == _W_P:
            text, cut = _paragraph_text(child, collector.room())
            collector.more = collector.more or cut
            if not collector.add(text.strip()) or collector.more:
                return False
        elif tag == _W_TBL:
            if not _docx_table(child, collector):
                return False
        elif tag in _W_WRAPPERS and not _placeholder(child) and not _docx_blocks(child, collector):
            return False
        elif tag == _MC + "AlternateContent":  # 正文这一层的：读第一个 Choice，没有就读 Fallback（D93 第 19 条）
            chosen = child.find(_MC + "Choice")
            chosen = child.find(_MC + "Fallback") if chosen is None else chosen
            if chosen is not None and not _docx_blocks(chosen, collector):
                return False
    return True


def _docx_text(data: bytes, collector: _Collector) -> None:
    """.docx：python-docx 打开，正文自己按 XML 走一遍（审阅第 2 条）。python-docx 的段落、表格只看正文下面直接的那一层，
    内容控件（w:sdt）、修订（w:ins）、域（w:fldSimple）、智能标记里的字，格子里套着的表格，都会悄悄丢掉。
    页眉、页脚、脚注、批注不在正文里，不读。"""
    from docx import Document

    _docx_blocks(Document(io.BytesIO(data)).element.body, collector)


def _office_text(kind: str, data: bytes, limit: int) -> tuple[str, int, bool] | None:
    """Excel、Word 的字、（PDF 那样的）没收的乱码页数（总是 0）、是不是还有没读的。要密码才打得开的返回 None。"""
    if _encrypted_ooxml(data):
        return None
    collector = _Collector(limit)
    if kind == KIND_EXCEL:
        if data.startswith(_OLE2_MAGIC):
            if not _xls_text(data, collector):
                return None
        else:
            _xlsx_text(data, collector)
    else:
        _docx_text(data, collector)
    return "\n".join(collector.lines), 0, collector.more


# 子进程：先按父进程的 sys.path 找 javmem 和要用的库（PDF 是 pdfminer 的每个模块，Excel、Word 是 openpyxl、xlrd、
# python-docx，连同它们一导入就要的依赖），找不到、导不进来就用 CANNOT_START 退出（标准错误写是什么没找到），
# 都导进来了先给自己设时限，再读标准输入的文件
_CHILD = (
    "import json, sys\n"
    "sys.path[:] = json.loads(sys.argv[1])\n"
    "kind = sys.argv[4] if len(sys.argv) > 4 else 'pdf'\n"
    "try:\n"
    "    from javmem.attachments import load_reader, reader_child\n"
    "    load_reader(kind)\n"
    "except BaseException as error:\n"
    "    sys.stderr.write(type(error).__name__ + ': ' + str(error)[:300])\n"
    f"    sys.exit({CANNOT_START})\n"
    "reader_child(kind, int(sys.argv[2]), float(sys.argv[3]))\n"
)


def limit_child(seconds: float) -> threading.Timer:
    """读 PDF 的子进程给自己设时限（D79 第 15 条）：父进程被杀了也会自己停。返回看时间的线程，读完了要 cancel。

    Linux、macOS：RLIMIT_CPU，CPU 时间到了内核发 SIGXCPU 停掉它（硬上限再多一秒）；RLIMIT_AS 限内存。
    哪儿都有的：一个看墙上时间的线程，到点直接退出（STOPPED_BY_ITSELF）。Windows 上没有 resource，只有这个线程。
    """
    cpu = max(1, math.ceil(seconds))
    try:
        import resource
    except ImportError:  # Windows
        pass
    else:
        for which, value in ((resource.RLIMIT_AS, (_CHILD_MEMORY, _CHILD_MEMORY)), (resource.RLIMIT_CPU, (cpu, cpu + 1))):
            try:
                resource.setrlimit(which, value)
            except (ValueError, OSError):
                pass  # macOS 常常不让设内存
    timer = threading.Timer(seconds, os._exit, args=(STOPPED_BY_ITSELF,))
    timer.daemon = True
    timer.start()
    return timer


def reader_child(kind: str, limit: int, seconds: float) -> None:
    """读 PDF、Excel、Word 的子进程：先设时限，标准输入是文件的字节，读完了停掉看时间的线程，标准输出写 JSON。

    结果写到原来的标准输出（先复制一份），读的时候文件描述符 1 换成 os.devnull：读的库往标准输出打的东西
    （xlrd 的警告，C 写的扩展直接写的也算）混进结果，父进程就解不开了（审阅第 1 条）。警告、日志也都不要。"""
    import logging
    import warnings

    timer = limit_child(seconds)
    warnings.simplefilter("ignore")
    logging.disable(logging.CRITICAL)
    result = os.fdopen(os.dup(1), "wb")
    quiet = os.open(os.devnull, os.O_WRONLY)
    os.dup2(quiet, 1)
    os.close(quiet)
    data = sys.stdin.buffer.read()
    found = _pdf_text(data, limit) if kind == KIND_PDF else _office_text(kind, data, limit)
    timer.cancel()
    payload = {"encrypted": True} if found is None else {"text": found[0], "garbled": found[1], "more": found[2]}
    result.write(json.dumps(payload).encode("ascii"))  # ascii：孤立代理也写得出去
    result.flush()


def pdf_child(limit: int, seconds: float) -> None:
    """读 PDF 的子进程（reader_child 的 PDF 那一种）。"""
    reader_child(KIND_PDF, limit, seconds)


def child_command(limit: int, seconds: float, kind: str = KIND_PDF) -> list[str]:
    """起读 PDF（Excel、Word）的子进程的命令。-I：不看 PYTHONPATH 这些环境变量、不从当前目录找模块；sys.path 用父进程的
    （D79 第 11 条），javmem 从和这里一样的地方取、放在最前面。表示当前目录的空串不要。seconds 是子进程自己的时限。"""
    root = str(Path(__file__).resolve().parent.parent)
    paths = list(dict.fromkeys([root, *(entry for entry in sys.path if entry)]))
    return [sys.executable, "-I", "-c", _CHILD, json.dumps(paths), str(limit), repr(float(seconds)), kind]


def _windows_job(pid: int, memory: int) -> object | None:
    """Windows：建一个作业对象，每个进程最多用 memory 字节，句柄一关里面的进程跟着停（父进程崩了也不留着它），把 pid
    放进去。返回作业对象的句柄（读完了要关）；建不了、放不进去的回 None（照旧读，记一行警告）。"""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    limit_process_memory, kill_on_job_close, extended_limit_information = 0x00000100, 0x00002000, 9
    set_quota, terminate = 0x0100, 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        _log.warning("读附件的子进程建不了作业对象，这一次不限内存")
        return None
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = limit_process_memory | kill_on_job_close
    info.ProcessMemoryLimit = memory
    process = None
    try:
        if kernel32.SetInformationJobObject(job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)):
            process = kernel32.OpenProcess(set_quota | terminate, False, pid)
            if process and kernel32.AssignProcessToJobObject(job, process):
                return job
    finally:
        if process:
            kernel32.CloseHandle(process)
    kernel32.CloseHandle(job)
    _log.warning("读附件的子进程放不进作业对象，这一次不限内存")
    return None


def _close_job(job: object) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle(job)


def run_child(command: Sequence[str], data: bytes, *, timeout: float, memory: int = _CHILD_MEMORY) -> subprocess.CompletedProcess[bytes]:
    """起读附件的子进程、从标准输入喂文件的字节、等它读完；到点杀掉它，抛 subprocess.TimeoutExpired（和 subprocess.run 一样）。

    Windows 上没有 RLIMIT_AS：起来以后、喂文件以前，把它放进一个作业对象，每个进程最多用 memory 字节（D79 第 7 条、
    D93 第 16 条，U3）。子进程要等喂了文件才开始读，所以没有空当；分配不出来就是子进程出错，调用方照旧记成 unreadable。
    Linux、macOS 上子进程自己设 RLIMIT_AS（limit_child），这里不另外限。"""
    process = subprocess.Popen(list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    job = _windows_job(process.pid, memory) if os.name == "nt" else None
    try:
        try:
            stdout, stderr = process.communicate(input=data, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
    finally:
        if job is not None:
            _close_job(job)


def stopped_by_itself(returncode: int) -> bool:
    """子进程到了自己的时限停下的：看墙上时间的线程退出的，或者 CPU 时间到了被 SIGXCPU 停掉的。"""
    return returncode in (STOPPED_BY_ITSELF, -_SIGXCPU)


_CHILD_NAMES = {KIND_PDF: ("PDF", "pdfminer.six"), KIND_EXCEL: ("Excel", "openpyxl、xlrd"), KIND_WORD: ("Word", "python-docx")}


def _pdf_text_in_child(data: bytes, limit: int, seconds: float, kind: str = KIND_PDF) -> tuple[str, int, bool] | None | str:
    """在子进程里读（PDF 是 _pdf_text，Excel、Word 是 _office_text），到点就杀掉，返回 REASON_TIMEOUT；
    子进程自己到点停下的也是。起不来抛 PdfUnavailable，别的出错抛 RuntimeError。"""
    what, library = _CHILD_NAMES[kind]
    try:
        done = run_child(child_command(limit, seconds + _CHILD_GRACE, kind), data, timeout=seconds)
    except subprocess.TimeoutExpired:
        return REASON_TIMEOUT
    except OSError as error:  # 起不了 Python
        raise PdfUnavailable(f"读 {what} 的子进程起不来：{type(error).__name__}") from None
    if done.returncode == CANNOT_START:
        why = done.stderr.decode("utf-8", "replace").strip()[:300]
        raise PdfUnavailable(f"读 {what} 的子进程起不来（{why}）。装好 {library} 再跑")
    if stopped_by_itself(done.returncode):
        return REASON_TIMEOUT
    if done.returncode != 0:
        raise RuntimeError(f"读 {what} 的子进程出错了")
    payload = json.loads(done.stdout.decode("ascii"))
    if payload.get("encrypted"):
        return None
    return str(payload["text"]), int(payload["garbled"]), bool(payload["more"])


def tidy(text: str) -> str:
    """换行统一成 LF，分隔符换成换行，每行去掉行尾的空白，连着的空行只留一个，头尾的空行不要。"""
    text = _CONTROLS.sub(" ", _SEPARATORS.sub("\n", clean_text(text)))
    kept: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if line or (kept and kept[-1]):
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept)


_BACKOFF = 200  # 硬截时往回找空白、往回退一串值，最多这么远
# 一串值里的字。硬截落在这样一串里的，退到这一串前面（D93 审阅第 5 条）：截下来的半截密钥、半截密码打码认不出来，会漏出去。
# 先按打码认的值（_VALUE_CHAR_WIDE，[!-~]）退，最多 _BACKOFF 个字：密码里的 @、#、! 也算进一串（D96 第 40 条：以前只退到 @
# 后面，“密码：”后面留下两个字）。这一串比 _BACKOFF 长的（压成一行的 CSV、JSON），再按 _VALUE_CHAR 退：@、#、! 照样
# 算进一串，只有引号、逗号、分号算断开（D96 审阅第 8 条：只认字母数字的时候，JSON 里的密码只退到 @ 后面，值的头两个字留下了）。
# 退到一半以前的，再按窄一点的规则退一遍：把 & | ? = : 也当断开（D96 第二遍审阅第 2 条：a=1&…、id|…、k:v:… 这种一万多字的
# 一整串，第二遍退到了一半以前，照原截点截，token= 后面留下一两个字、sk- 密钥留下半截），还不行再只认字母、数字、_+/=.~-
# （D93 那一种）。哪一种退下来还在一半以后就用哪一种；都不行（从头就是一整串）的，照原来的截点截：不截成空的
_VALUE_CHAR_WIDE = re.compile(r"[!-~]")
_VALUE_CHARS = (
    re.compile(r"(?![\"',;])[!-~]"),
    re.compile(r"(?![\"',;&|?=:])[!-~]"),
    re.compile(r"[A-Za-z0-9_+/=.~-]"),
)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """超过 limit 字的，在 limit 字以内最后一个换行处截断。那个换行离 limit 太远、截下来不到一半的（一格、一段特别长，
    表名、标题后面就是它），在 limit 字处硬截（D93 第 26 条）：往回找最近的空白（最多 _BACKOFF 字），不把一个词
    （打码要认的密码、令牌）截成两半；找不到的就在第 limit 个字截。"""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit + 1)
    if cut < limit // 2:
        start = max(0, limit - _BACKOFF)
        space = max(text.rfind(" ", start, limit + 1), text.rfind("\t", start, limit + 1))
        cut = space if space > limit // 2 else limit
        cut = _value_cut(text, cut, limit)
    return text[:cut].rstrip(), True


def _value_cut(text: str, cut: int, limit: int) -> int:
    """截点落在一串值里的，退到这一串前面（_VALUE_CHAR_WIDE 那一段说明）。"""
    start = _run_start(text, cut, _VALUE_CHAR_WIDE, _BACKOFF)
    if start is not None and start > limit // 2:
        return start
    for chars in _VALUE_CHARS:
        start = _run_start(text, cut, chars, len(text))
        if start is not None and start > limit // 2:
            return start
    return cut


def _run_start(text: str, cut: int, chars: re.Pattern[str], limit: int) -> int | None:
    """截点前后都是 chars 里的字时，这一串从哪里开始：往回最多找 limit 个字，找到前面不是 chars 的字就是它。
    截点不在一串里的给截点本身；退到文字开头、超过 limit 的给 None（没有截得开的地方）。"""
    if not (0 < cut < len(text) and chars.match(text[cut]) and chars.match(text[cut - 1])):
        return cut
    floor = max(0, cut - limit)
    start = cut - 1
    while start > floor and chars.match(text[start - 1]):
        start -= 1
    return start if start > 0 and not chars.match(text[start - 1]) else None


def read_attachment(
    data: bytes,
    *,
    media_type: str,
    name: str,
    patterns: Sequence[RedactionPattern],
    charset: str | None = None,
    max_bytes: int = MAX_FILE_BYTES,
    max_chars: int = MAX_TEXT_CHARS,
    pdf_seconds: float | None = None,
    budget: PdfBudget | None = None,
) -> AttachmentText:
    """读一个附件的正文。charset 是邮件里这一部分声明的编码；没给的看类型里写没写（text/plain; charset=…）。
    pdf_seconds 不给是 PDF_SECONDS，Excel、Word 也用它。budget 是这封信的 PdfBudget：前面有 PDF、Excel、Word
    超时了，这个不读，记 timeout。读它们的子进程起不来抛 PdfUnavailable。"""
    kind = kind_of(media_type, name)
    if kind == KIND_IMAGE:
        return _none(REASON_IMAGE)
    if kind == KIND_OTHER:
        return _none(REASON_UNSUPPORTED)
    if len(data) > max_bytes:
        return _none(REASON_TOO_LARGE)
    if kind == KIND_WORD and data.startswith(_OLE2_MAGIC) and not _encrypted_ooxml(data):
        return _none(REASON_UNSUPPORTED)  # 老的 .doc 先不读（D91）
    if kind in (KIND_EXCEL, KIND_WORD) and not data.startswith((_ZIP_MAGIC, _OLE2_MAGIC)):
        return _none(REASON_UNREADABLE)  # 写着是 Excel、Word，文件头却不是
    if kind in (KIND_EXCEL, KIND_WORD) and data.startswith(_ZIP_MAGIC) and _unzipped_too_large(data, kind):
        return _none(REASON_TOO_LARGE)
    charset = charset or charset_of(media_type)
    garbled, unread = 0, False
    try:
        if kind in CHILD_KINDS:
            if budget is not None and budget.timed_out:
                budget.skipped += 1
                return _none(REASON_TIMEOUT)
            seconds = PDF_SECONDS if pdf_seconds is None else pdf_seconds
            found = _pdf_text_in_child(data, max_chars, seconds, kind)
            if found == REASON_TIMEOUT:
                if budget is not None:
                    budget.timed_out = True
                return _none(REASON_TIMEOUT)
            if found is None:
                return _none(REASON_ENCRYPTED)
            raw, garbled, unread = found  # type: ignore[misc]
        elif kind == KIND_HTML:
            raw = html_to_text(_decode(data, charset))
        else:
            raw = _decode(data, charset)
        text = tidy(raw)
    except PdfUnavailable:
        raise
    except Exception:  # noqa: BLE001 外来的文件什么样都有，pdfminer 会抛各种异常
        return _none(REASON_UNREADABLE)
    if not text.strip():
        return _none(REASON_NO_TEXT)  # 扫描件；PDF 每一页都是乱码的也算
    kept, truncated = _truncate(text, max_chars)
    redacted, counts = redact(kept, patterns)
    if truncated or unread:  # 截断了，或者 PDF 读到够了就停、后面还有页没读
        return AttachmentText(TEXT_TRUNCATED, REASON_TOO_LONG, redacted, len(text), counts)
    if garbled:
        return AttachmentText(TEXT_TRUNCATED, REASON_GARBLED, redacted, len(text), counts)
    return AttachmentText(TEXT_FULL, None, redacted, len(text), counts)
