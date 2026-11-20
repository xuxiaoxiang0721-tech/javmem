"""把字节、HTML 转成文字。邮件正文和附件的正文（packet-spec 第 3、6 节）都用这一份。

都是从邮件导入挪过来的：
- 解字节：7 位的先按声明的编码；8 位的先试 UTF-8，再按声明的编码、GB18030，最后 latin-1 兜底。
- HTML：段落空一行，引用块每行前面加 > ，表格同一行的格子空一格，脚本和样式不要；
  乱到解析器读不下去、或者会慢到拖住整批的，改成线性地去掉标签。
- 内嵌图片（标签里 data: 开头的属性值）先去掉：一张图几万字，留着的话解析器读不下去、走兜底，
  标签和 base64 都进了正文。
- 实体：解析器解开一遍以后还剩的不间断空格和编号写的字（原文转了两次的 &amp;nbsp;、&amp;#8220;）再解一遍。
  纯文字的正文不在这里，导入时照录（packet-spec 第 6 节），客户端写进去的 &nbsp; 不解。
"""

from __future__ import annotations

import codecs
import html
import re
from collections.abc import Iterable, Sequence
from html.parser import HTMLParser

_LOOKS_LIKE_GBK = frozenset({"gbk", "gb2312", "gb_2312-80", "x-gbk", "cp936", "euc-cn", "x-euc-cn", "gb18030"})
_NO_CHARSET = frozenset({"", "unknown-8bit", "x-unknown", "unknown", "default", "x-user-defined"})


def codec_name(charset: str | None) -> str | None:
    name = (charset or "").replace("\x00", "").strip().strip('"').lower().split("*")[0]
    if name in _NO_CHARSET:
        return None
    if name in _LOOKS_LIKE_GBK:
        return "gb18030"  # GBK、GB2312 都是它的子集
    if name in ("iso-8859-1", "latin-1", "latin1"):
        name = "cp1252"  # 标 Latin-1 的实际多半是 Windows-1252
    try:
        return codecs.lookup(name).name
    except (LookupError, ValueError, TypeError):
        return None


def decode_bytes(data: bytes, charsets: Sequence[str | None]) -> tuple[str, bool]:
    """返回文字，和是不是没按声明的编码解出来。

    7 位的数据（字节都在 ASCII 范围里）先按声明的编码解：ISO-2022-JP、HZ、UTF-7 就是 7 位的，
    UTF-16 的英文里全是 NUL。8 位的数据先试 UTF-8：它有自己的结构，别的编码的中文几乎不会碰巧
    是合法的 UTF-8，还能接住标错了编码的信。再按声明的编码、GB18030，最后 latin-1 兜底，它不会失败。
    """
    declared = [codec for codec in (codec_name(charset) for charset in charsets) if codec]
    if data.isascii():
        for codec in declared:
            if codec.startswith(("utf_16", "utf_32")) and b"\x00" not in data:
                continue  # 没有 NUL 的 ASCII 不会是 UTF-16，是标错了
            try:
                return data.decode(codec), False
            except (LookupError, UnicodeDecodeError, ValueError):
                continue
        return data.decode("ascii"), bool(declared)
    for codec in dict.fromkeys(["utf-8", *declared, "gb18030"]):
        try:
            return data.decode(codec), bool(declared) and codec != declared[0]
        except (LookupError, UnicodeDecodeError, ValueError):
            continue
    return data.decode("latin-1"), True


class _HtmlText(HTMLParser):
    """HTML 邮件转纯文字。段落之间空一行，引用块每行前面加 > ，表格同一行的格子空一格，脚本和样式不要。"""

    _SKIP = frozenset({"script", "style", "title"})
    _PARAGRAPH = frozenset({"p", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "dl"})
    _LINE = frozenset({"div", "li", "tr", "dt", "dd", "section", "article", "header", "footer", "center"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.buffer: list[str] = []
        self.skip = 0
        self.quote = 0
        self.pre = 0

    def flush(self, *, paragraph: bool = False, force: bool = False) -> None:
        text = "".join(self.buffer)
        self.buffer = []
        lines = text.split("\n") if self.pre else [" ".join(text.split())]
        for line in lines:
            if line or force:
                self.lines.append("> " * self.quote + line if line else "")
        if paragraph:
            self.lines.append("")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self.skip += 1
        elif tag == "br":
            self.flush(force=True)
        elif tag == "blockquote":
            self.flush(paragraph=True)
            self.quote += 1
        elif tag == "pre":
            self.flush(paragraph=True)
            self.pre += 1
        elif tag in self._PARAGRAPH:
            self.flush(paragraph=True)
        elif tag in self._LINE:
            self.flush()
            if tag == "li":
                self.buffer.append("- ")
        elif tag in ("td", "th") and "".join(self.buffer).strip():
            self.buffer.append(" ")  # 同一行的格子之间空一格，不然 100 和 200 连成 100200

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> 只算一次换行；<title/>、<script src=x/> 自己就闭合了，里面什么都没有，不能开始跳过
        if tag not in self._SKIP:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "br":
            self.flush(force=True)  # 有的邮件写 </br>
        elif tag == "blockquote":
            self.flush(paragraph=True)
            self.quote = max(0, self.quote - 1)
        elif tag == "pre":
            self.flush(paragraph=True)
            self.pre = max(0, self.pre - 1)
        elif tag in self._PARAGRAPH:
            self.flush(paragraph=True)
        elif tag in self._LINE:
            self.flush()

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.buffer.append(data)


# 兜底用的几个正则。标签里引号中的 > 不算标签结束：title='a>b'。
# 每次尝试都有上限，整体是线性的：一封全是没闭合的 <script> 的信不能把整批拖住
_TAG_BODY = r"(?:\"[^\"]{0,2000}\"|'[^']{0,2000}'|[^'\"<>]){0,4000}"
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_BLOCK_OPEN = re.compile(rf"<(script|style|title)\b{_TAG_BODY}(?<!/)>", re.IGNORECASE)
_OPEN_BLOCK = re.compile(rf"<(?:script|style)\b{_TAG_BODY}(?<!/)>[^<]*", re.IGNORECASE)  # 没闭合的：代码到下一个标签为止
# 换行的标签和解析器那边一样（段落、列表项、表格的行、div 这些）；表格的格子空一格，不然 100 和 200 连成 100200
_BREAK_TAG = re.compile(
    rf"<\s*/?\s*(?:br|p|div|li|tr|h[1-6]|table|blockquote|ul|ol|dl|dt|dd|hr|pre|section|article|header|footer|center)\b"
    rf"{_TAG_BODY}>",
    re.IGNORECASE,
)
_CELL_TAG = re.compile(rf"<\s*/?\s*(?:td|th)\b{_TAG_BODY}>", re.IGNORECASE)
_ANY_TAG = re.compile(rf"<{_TAG_BODY}>")
# 内嵌图片：标签里属性值是 data: 开头的（<img src="data:image/png;base64,…">）、style 里的 url(data:…)，换成空值。
# 只动标签里面的，看得见的字里写着 data: 的不动。结束的引号、括号一个一个往后找，没闭合的一大串也是线性的
# 引号里的 > 不算标签结束（D93：<img alt="a>b" src="data:…"> 以前在 a> 那里断开，后面的 data: 没去掉，整封走兜底）；
# 引号没闭合的照以前，到下一个 > 为止
_TAG_SPAN = re.compile(r"""<[A-Za-z](?:[^<>"']|"[^"]*"|'[^']*')*>|<[A-Za-z][^<>]*>""")
_DATA_IN_TAG = re.compile(r"""(?P<attr>=\s*)(?:(?P<q1>["'])\s*)?data:|url\(\s*(?:(?P<q2>["'])\s*)?data:""", re.IGNORECASE)
_BARE_VALUE_END = re.compile(r"""[\s"'>]""")
# 解析器解开一遍以后还剩的实体：只认不间断空格和用编号写的字（原文转了两次的 &amp;nbsp;、&amp;#8220;）。
# &amp;、&lt; 这些不再解：读信的人看到的就是 &amp;、&lt;
_LEFTOVER_ENTITY = re.compile(r"&(?:nbsp|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});")


def _clean_tag(tag: str) -> str:
    """一个标签里，data: 开头的属性值换成 ""，url(data:…) 换成 url()。找不到结尾的就到此为止，后面原样留着。"""
    pieces: list[str] = []
    position = 0
    while True:
        match = _DATA_IN_TAG.search(tag, position)
        if match is None:
            break
        if match.group("attr") is not None:
            quote = match.group("q1")
            if quote:
                close = tag.find(quote, match.end())
                if close < 0:
                    break
                end = close + 1
            else:
                found = _BARE_VALUE_END.search(tag, match.end())
                end = found.start() if found else len(tag)
            replacement = match.group("attr") + '""'
        else:
            quote = match.group("q2")
            close = tag.find(quote, match.end()) if quote else match.end()
            paren = tag.find(")", close) if close >= 0 else -1
            if paren < 0:
                break
            end = paren + 1
            replacement = "url()"
        pieces.append(tag[position : match.start()])
        pieces.append(replacement)
        position = end
    pieces.append(tag[position:])
    return "".join(pieces)


def _drop_data_uris(markup: str) -> str:
    """内嵌图片的 base64 不要：一张图几万字，解析器读不下去会走兜底，标签和 base64 都进了正文。"""
    if "data:" not in markup.lower():
        return markup
    return _TAG_SPAN.sub(lambda match: _clean_tag(match.group()) if "data:" in match.group().lower() else match.group(), markup)


def _unescape_leftovers(text: str) -> str:
    """解析器解开一遍以后还剩的 &nbsp; 和用编号写的字再解一遍，&nbsp; 解成普通空格。原文里本来的不间断空格不动。"""
    if "&" not in text:
        return text

    def one(match: re.Match[str]) -> str:
        value = html.unescape(match.group())
        return " " if value == "\xa0" else value

    return _LEFTOVER_ENTITY.sub(one, text)


def _remove_closed_blocks(markup: str) -> str:
    """去掉成对的 script、style、title 和它们中间的内容。用 find 找结束标签，不用 .*?：
    后者对每个没闭合的开始标签都扫到底，一万个就要几秒。某种标签后面再也找不到结束标签，就不再找它。"""
    lower = markup.lower()
    pieces: list[str] = []
    position = 0
    unclosed: set[str] = set()
    for match in _BLOCK_OPEN.finditer(markup):
        name = match.group(1).lower()
        if match.start() < position or name in unclosed:
            continue
        close = lower.find("</" + name, match.end())
        if close < 0:
            unclosed.add(name)
            continue
        end = lower.find(">", close)
        pieces.append(markup[position : match.start()])
        position = len(markup) if end < 0 else end + 1
    pieces.append(markup[position:])
    return "".join(pieces)


def _join_lines(lines: Iterable[str]) -> str:
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):  # 连着的空行只留一个
            kept.append(line)
    return "\n".join(kept).strip("\n")


def _strip_tags(markup: str) -> str:
    """解析器读不下去时的兜底：去掉注释、成对的脚本样式和没闭合的脚本样式里的代码，
    换行的标签换成换行，别的标签都去掉。"""
    text = _OPEN_BLOCK.sub("", _remove_closed_blocks(_COMMENT.sub("", markup)))
    text = _ANY_TAG.sub("", _CELL_TAG.sub(" ", _BREAK_TAG.sub("\n", text)))
    return _join_lines(" ".join(line.split()) for line in _unescape_leftovers(html.unescape(text)).split("\n"))


_TAG_SPECIAL = re.compile(r"[>'\"]")
_MARKED_SECTION_BAD = re.compile(r"<!\[(?![A-Za-z])")
_MAX_QUOTED = 4096  # 一个属性值再长也不该超过这些（data: 开头的内嵌图片先去掉了），超了走兜底，字一样留得住


def _safe_for_parser(markup: str) -> bool:
    """这段 HTML 交给 html.parser 会不会慢到拖住整批。

    Python 自带的 html.parser 碰到没闭合的注释、声明、引号没闭合的标签时是平方级的：50 KB 就要几秒，
    1 MB 要几分钟，一封恶意信就能把整批拖住。先线性地扫一遍，有这些情况就直接走兜底（兜底是线性的）。
    """
    last_comment = markup.rfind("<!--")
    if last_comment >= 0 and markup.find("-->", last_comment + 4) < 0:
        return False
    markup = _COMMENT.sub("", markup)  # 注释都闭合了；注释里的东西 html.parser 不看（Outlook 在这里放 <![endif]>）
    # <![if …]>、<![endif]>、<![CDATA[ 这种字母紧跟在 <![ 后面、后面又闭合了的，解析器认得；<![ endif]> 这种会让它出错
    if _MARKED_SECTION_BAD.search(markup):
        return False
    last_section = markup.rfind("<![")
    if last_section >= 0 and markup.find("]>", last_section) < 0:
        return False
    for opener in ("<!", "<?"):
        last = markup.rfind(opener)
        if last >= 0 and markup.find(">", last) < 0:
            return False
    position = 0
    while True:
        start = markup.find("<", position)
        if start < 0:
            return True
        following = markup[start + 1 : start + 2]
        if not (following.isalpha() or following == "/"):
            position = start + 1
            continue
        cursor = start + 1
        while True:  # 找这个标签的 >，引号里的不算
            special = _TAG_SPECIAL.search(markup, cursor)
            if special is None:
                return False
            if special.group() == ">":
                position = special.end()
                break
            close = markup.find(special.group(), special.end())
            if close < 0 or close - special.end() > _MAX_QUOTED:
                return False
            cursor = close + 1


def _html_to_text(markup: str) -> tuple[str, bool]:
    """(文字, 是不是用了兜底)。解析器出错、会慢到拖住整批、或者脚本样式没闭合把后面全吞了，都改用兜底。

    内嵌图片的 data: 先去掉；解析器解开一遍实体以后还剩的再解一遍。
    """
    markup = _drop_data_uris(markup)
    if not _safe_for_parser(markup):
        return _strip_tags(markup), True
    parser = _HtmlText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 外来的 HTML 什么样都有，html.parser 会抛 AssertionError
        return _strip_tags(markup), True
    if parser.skip:
        return _strip_tags(markup), True
    parser.flush()
    return _unescape_leftovers(_join_lines(parser.lines)), False


def html_to_text(markup: str) -> str:
    return _html_to_text(markup)[0]
