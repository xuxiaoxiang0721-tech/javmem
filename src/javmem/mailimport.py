"""邮件导入（packet-spec 第 6 节）：把 .eml、.mbox 转成邮件包。

一个邮箱一个来源。导入工具是打包方，和 Bot 一样往来源目录投包，收件按规范校验。

- 邮件头解码好再写：RFC 2047、RFC 2231、没声明编码的 8 位字节都还原成文字，写不出孤立代理。
- 正文照录，HTML 转成纯文字，编码统一成 UTF-8。引用、签名、转发原样留着，认它们是 L1 的事。
- 主题、显示名、附件名和正文过一遍打码模式表，这是唯一可以动原文的地方。
- 坏邮件（没有发件人、没有能用的时间、根本不是邮件、读不了）不进包，写进报告。一封信再怪也不让整批停下。
- 自己记住处理过哪些 message_id，同一批再导一次不重复。状态和报告放在自己的目录，不放收件目录。
  正式和测试各记各的（规范第 10 节）。先读邮件头和正文认编号、比指纹，导过的信不读附件的正文（D88）。
- 按规则（发件人、主题）分到不同的可见范围，往来串里先来的信的敏感标记也加到后来的回信上；
  按天切，太长在两封邮件之间切。
"""

from __future__ import annotations

import base64
import binascii
import email
import email.errors
import email.policy
import email.utils
import hashlib
import io
import json
import os
import re
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone, tzinfo
from email.message import Message
from pathlib import Path

import yaml

from javmem.attachments import (
    KIND_IMAGE,
    REASON_UNSUPPORTED,
    AttachmentText,
    PdfBudget,
    PdfUnavailable,
    kind_of,
    read_attachment,
)
from javmem.config import ConfigError
from javmem.packet import Rejected, validate_packet
from javmem.packet.model import TEXT_NONE, Address
from javmem.packet.render import (
    MAX_RAW_CHARS,
    attachment_block,
    attachment_section,
    check_out_dir,
    clean_text,
    continuation,
    deliver,
    front_matter,
    next_packet_id,
    yaml_text,
)
from javmem.redaction import RedactionPattern, redact
from javmem.sources import Registry
from javmem.textconv import _html_to_text, html_to_text  # noqa: F401  html_to_text 也从这里给出去
from javmem.textconv import decode_bytes as _decode_bytes
from javmem.timeutil import iso, parse_aware
from javmem.yamlio import load_yaml

if os.name == "nt":
    import msvcrt
else:
    import fcntl

FRAMEWORK = "mail-import"
KINDS = ("backfill", "daily", "adhoc")
STATE_FORMAT = 2
# 指纹（Mail.digest）算的是转好、打好码的正文：转文字、拼正文、打码的规矩一改，同一封信的指纹就变了。
# 改了这些规矩就把它加一：状态里记的是旧版本的，指纹扔掉不用，不误报改了重发（D93 审阅第 9 条）。
# 不补记：再碰到时同一个编号的几版里先读到的不一定是导了的那一版，补记的会是没导的旧版。
# 所以升级以前导过的信，后来改了重发的认不出来，和没记指纹的老条目一样
DIGEST_VERSION = 2
STATE_DIR = "state"
REPORT_DIR = "reports"
GENERATED_DOMAIN = "javmem.invalid"
FUTURE_SLACK = timedelta(days=1)  # 比导入的时刻晚一天以上的 Date 不可信（规范第 6 节）
_MAILBOX_KEY = "*"  # daily 包按邮箱接，不按可见范围接：巡检按来源看缺口

# 坏邮件：不进包，写进报告
BAD_NOT_A_MAIL = "not_a_mail"  # 一个认得出的邮件头都没有
BAD_NO_FROM = "no_from"
BAD_NO_DATE = "no_date"  # Date 没有、读不了或者太晚，最上面一条 Received 也用不了
BAD_UNREADABLE = "unreadable"  # 读的时候出了想不到的错

# 进了包，但报告里要有数的情况
NOTE_GENERATED_ID = "generated_message_id"
NOTE_DATE_FROM_RECEIVED = "date_from_received"
NOTE_DATE_NO_ZONE = "date_without_zone"  # Date 没写时区，按 UTC 算
NOTE_CHARSET_FALLBACK = "charset_fallback"  # 声明的编码解不了，换了一种
NOTE_HTML_ONLY = "html_only"
NOTE_HTML_FALLBACK = "html_fallback"  # HTML 太乱，解析器读不下去，改成直接去掉标签
NOTE_BROKEN_MULTIPART = "broken_multipart"  # 说是 multipart 却切不开（缺 boundary 或者对不上），整段当正文
NOTE_ADDRESS_PARTLY = "address_partly_parsed"  # 地址列表切不干净，认得出的地址都留着，显示名丢了
NOTE_THREAD_BY_SUBJECT = "thread_by_subject"  # 头不全，按主题和来往的人归串
NOTE_THREAD_SCOPE = "thread_scope"  # 往来串里先来的信带了敏感标记，这封回信也加上了
NOTE_ID_CLASH = "duplicate_id_different_content"  # 同一个 Message-ID，内容不一样：同一批里留 Date 晚的那封，以前导过的照旧跳过
NOTE_NO_MAIL = "no_mail_found"  # 给的文件和目录里一封信都没找到
NOTE_PDF_SKIPPED = "pdf_skipped_after_timeout"  # 这封信前面有 PDF（Excel、Word）读到点还没读完，后面的没读（D79 第 12 条）

_HEADER_NAMES = ("from", "to", "cc", "date", "subject", "message-id", "received", "in-reply-to", "references")
_MESSAGE_ID = re.compile(r"<[^<>\s]+>")
_EMAIL = re.compile(r"[^\s<>,;:\"'()\[\]]+@[^\s<>,;:\"'()\[\]]+")
_FOLD = re.compile(r"\r?\n(?=[ \t])")
_ENCODED_WORD = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?\s]*)\?=")
_REPLY_PREFIX = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|回复|答复|回覆|转发|轉發|转寄)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*)+",
    re.IGNORECASE,
)
_SNIFF_BYTES = 4096


class ImportBusy(Exception):
    """同一个来源已经有一个导入在跑。"""


# ---------- 配置：按规则分可见范围 ----------


@dataclass(frozen=True)
class Scope:
    domain: tuple[str, ...]
    sensitive: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{','.join(self.domain)}|{','.join(self.sensitive)}"


@dataclass(frozen=True)
class MailRule:
    sender: re.Pattern[str] | None
    subject: re.Pattern[str] | None
    scope: Scope

    def matches(self, sender: Address, subject: str) -> bool:
        """from 对地址、显示名、“名字 <地址>”三种写法，哪种对上都算。"""
        if self.sender is not None:
            forms = [sender.addr, *([sender.name, f"{sender.name} <{sender.addr}>"] if sender.name else [])]
            if not any(self.sender.search(form) for form in forms):
                return False
        return self.subject is None or self.subject.search(subject) is not None


@dataclass(frozen=True)
class MailConfig:
    rules: tuple[MailRule, ...]
    default: Scope

    def scope_for(self, sender: Address, subject: str) -> Scope:
        return next((rule.scope for rule in self.rules if rule.matches(sender, subject)), self.default)


def _regex(value: object, where: str) -> re.Pattern[str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where} 要写成正则文本")
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error:
        raise ConfigError(f"{where} 不是合法的正则") from None


def _check_keys(entry: Mapping, allowed: frozenset[str], where: str) -> None:
    """认不出的字段一律报错。sensitive 打错字时规则照样能用，只是标记悄悄没了。"""
    if any(key not in allowed for key in entry):
        raise ConfigError(f"{where} 有认不出的字段，可能是写错了字")


def _scope(entry: Mapping, where: str, registry: Registry, source_id: str, fallback: Scope | None) -> Scope:
    source = registry.sources[source_id]
    domain = entry.get("domain", list(fallback.domain) if fallback else list(source.default_domain))
    sensitive = entry.get("sensitive", [])
    for name, value in (("domain", domain), ("sensitive", sensitive)):
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ConfigError(f"{where} 的 {name} 要写成文本列表")
    if not domain:
        raise ConfigError(f"{where} 要写 domain，登记表的 default_domain 也是空的")
    if not set(domain) <= registry.domains:
        raise ConfigError(f"{where} 的 domain 不在登记表的 domains 里")
    if not set(sensitive) <= source.may_carry:
        raise ConfigError(f"{where} 的 sensitive 不在这个来源的 may_carry 里")
    return Scope(tuple(dict.fromkeys(domain)), tuple(dict.fromkeys(sensitive)))


def mail_source(registry: Registry, source_id: str) -> str:
    """来源要登记过，而且是邮件来源。返回它的邮箱地址。"""
    source = registry.sources.get(source_id)
    if source is None:
        raise ConfigError("这个来源不在登记表里")
    if source.channel != "email" or not source.mailbox:
        raise ConfigError("这个来源不是邮件来源")
    return source.mailbox


def load_mail_config(path: Path | None, registry: Registry, source_id: str) -> MailConfig:
    """读分包规则。没有规则文件时都进登记表的 default_domain。写错了什么都不导。"""
    mail_source(registry, source_id)
    if path is None:
        return MailConfig((), _scope({}, "默认", registry, source_id, None))
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise ConfigError("分包规则读不了") from None
    except (yaml.YAMLError, RecursionError):
        raise ConfigError("分包规则不是合法的 YAML") from None
    if not isinstance(data, Mapping):
        raise ConfigError("分包规则的写法不对")
    _check_keys(data, frozenset({"rules", "default"}), "分包规则")
    default_entry = data.get("default", {})
    if not isinstance(default_entry, Mapping):
        raise ConfigError("分包规则的 default 写法不对")
    _check_keys(default_entry, frozenset({"domain", "sensitive"}), "分包规则的 default")
    default = _scope(default_entry, "分包规则的 default", registry, source_id, None)
    entries = data.get("rules", [])
    if not isinstance(entries, list):
        raise ConfigError("分包规则的 rules 要写成列表")
    rules = []
    for number, entry in enumerate(entries, 1):
        where = f"分包规则第 {number} 条"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where} 的写法不对")
        _check_keys(entry, frozenset({"from", "subject", "domain", "sensitive"}), where)
        sender, subject = _regex(entry.get("from"), f"{where} 的 from"), _regex(entry.get("subject"), f"{where} 的 subject")
        if sender is None and subject is None:
            raise ConfigError(f"{where} 至少要写 from 或 subject")
        rules.append(MailRule(sender, subject, _scope(entry, where, registry, source_id, default)))
    return MailConfig(tuple(rules), default)


# ---------- 读信 ----------


@dataclass(frozen=True)
class Attachment:
    name: str
    type: str
    size: int
    content: AttachmentText | None = None  # 正文（javmem.attachments），读完信才有；只留整理好、截好、打过码的文字


@dataclass(frozen=True)
class _RawAttachment:
    """读信时的附件：字节只留到这封信读完。"""

    name: str
    type: str
    size: int
    data: bytes | None  # 作为附件转发的整封邮件、内嵌图不读，是 None
    charset: str | None
    inline: bool = False  # 内嵌图（_inline_image）：不进附件名单，只数几张（D91）


@dataclass
class Mail:
    location: str
    message_id: str
    in_reply_to: str | None
    references: tuple[str, ...]
    sender: Address
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    bcc: tuple[Address, ...]
    date: datetime
    subject: str
    attachments: tuple[Attachment, ...]
    body: str
    inline_images: int = 0  # 内嵌图几张，读完附件才有（D91）
    notes: list[str] = field(default_factory=list)
    redactions: dict[str, int] = field(default_factory=dict)
    thread_id: str = ""
    scope: Scope | None = None

    @property
    def size(self) -> int:
        return len(self.body) + len(self.subject)

    @property
    def order(self) -> tuple[float, str]:
        return (self.date.timestamp(), self.message_id)

    @property
    def digest(self) -> str:
        """这封信内容的指纹，用来认出“同一个 Message-ID、内容却不一样”的。"""
        parts = [self.sender.addr, iso(self.date), self.subject, self.body]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class BadMail:
    location: str
    reason: str


def _repair(text: str, charsets: Sequence[str | None]) -> str:
    """8 位的邮件头按 surrogateescape 读进来会带孤立代理。还原成字节再解。"""
    if not any("\udc80" <= char <= "\udcff" for char in text):
        return text
    return _decode_bytes(text.encode("utf-8", "surrogateescape"), charsets)[0]


def _unfold(value: str) -> str:
    return _FOLD.sub("", value).strip()


def _word_bytes(encoding: str, text: str) -> bytes | None:
    """一个编码词的内容。坏的返回 None，原样留着。"""
    if not text.isascii():
        return None
    if encoding in "bB":
        try:
            return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
        except (binascii.Error, ValueError):
            return None
    raw = text.replace("_", " ").encode("ascii")
    return re.sub(rb"=([0-9A-Fa-f]{2})", lambda match: bytes([int(match.group(1), 16)]), raw)


def _decode_words(value: str, charsets: Sequence[str | None]) -> str:
    """解 RFC 2047 的编码词（=?UTF-8?B?...?=），顺手修好 8 位字节。

    自己解，不用 email.header.decode_header：它把编码词旁边的普通文字也当转义解，
    “\\\\nas\\users” 会被改掉。两个编码词中间只隔空白时空白不要（RFC 2047 第 6.2 节），
    同一种编码连着的几个编码词先拼成字节再解：一个汉字可能被切在两个编码词里。
    """
    value = _repair(_unfold(value), charsets)
    if "=?" not in value:
        return clean_text(value)
    out: list[str] = []
    pending = b""
    pending_charset: str | None = None

    def flush() -> None:
        nonlocal pending, pending_charset
        if pending_charset is not None:
            out.append(_decode_bytes(pending, [pending_charset])[0])
        pending, pending_charset = b"", None

    position = 0
    for match in _ENCODED_WORD.finditer(value):
        between = value[position : match.start()]
        charset, encoding, text = match.groups()
        data = _word_bytes(encoding, text)
        if data is None:
            flush()
            out.append(between + match.group(0))
        else:
            if between and (pending_charset is None or between.strip()):
                flush()
                out.append(between)
            if pending_charset is not None and pending_charset.lower() != charset.lower():
                flush()
            pending += data
            pending_charset = charset
        position = match.end()
    flush()
    out.append(value[position:])
    return clean_text("".join(out))


def _raw_headers(message: Message, name: str) -> list[str]:
    return [value for key, value in message.raw_items() if key.lower() == name]


def _first_header(message: Message, name: str) -> str | None:
    values = _raw_headers(message, name)
    return values[0] if values else None


def _getaddresses(values: list[str]) -> list[tuple[str, str]]:
    """不用严格模式：结尾多一个逗号、用分号分隔时，严格模式整串都返回空。"""
    try:
        return email.utils.getaddresses(values, strict=False)
    except TypeError:  # 老一点的 Python 没有 strict 这个参数，本来就不严格
        return email.utils.getaddresses(values)


_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"')
_PAREN_COMMENT = re.compile(r"\((?:\\.|[^()\\])*\)")


def _bare_addresses(values: Sequence[str]) -> list[str]:
    """引号里的显示名和括号里的注释去掉以后还看得到的地址。“li.ming@… via Sales” 这种显示名里的不算。"""
    text = _QUOTED.sub(" ", " ".join(values))
    for _ in range(3):  # 注释可以套注释
        text = _PAREN_COMMENT.sub(" ", text)
    return _EMAIL.findall(text)


def _addresses(message: Message, name: str, charsets: Sequence[str | None], notes: list[str]) -> tuple[Address, ...]:
    """先按原样切地址，再解显示名：解开以后名字里可能有逗号，先解就切错了。

    切不干净的（引号没配对、显示名里的逗号没加引号），把认得出的地址都捞出来，显示名不要，报告里记一笔。
    没有 @ 的不算地址：`Li, Ming <…>` 会切出一个叫 Li 的。
    """
    values = [_unfold(value) for value in _raw_headers(message, name)]
    addresses: list[Address] = []
    broken = False
    for display, addr in _getaddresses(values):
        addr = clean_text(_repair(addr, charsets)).strip()
        if not addr:
            continue
        if "@" not in addr or any(char.isspace() or char in '<>,;"' for char in addr):
            broken = True
            continue
        display = " ".join(_decode_words(display, charsets).split())
        addresses.append(Address(display or None, addr))
    seen = {item.addr.lower() for item in addresses}
    found = [clean_text(_repair(addr, charsets)) for addr in _bare_addresses(values)]
    missing = [addr for addr in dict.fromkeys(found) if addr.lower() not in seen]
    if broken or missing:
        addresses += [Address(None, addr) for addr in missing]
        notes.append(NOTE_ADDRESS_PARTLY)
    return tuple(addresses)


def _message_id(value: str | None, charsets: Sequence[str | None]) -> str | None:
    if value is None:
        return None
    text = clean_text(_repair(_unfold(value), charsets))
    match = _MESSAGE_ID.search(text)
    if match is not None:
        return match.group(0)
    text = text.strip("<> \t")
    return f"<{text}>" if text and not any(char.isspace() for char in text) else None


def _parse_date(value: str | None) -> tuple[datetime | None, bool]:
    """(时间, 是不是没写时区)。没写时区和写 -0000 的，按 RFC 5322 当 UTC。"""
    if not value:
        return None, False
    try:
        moment = email.utils.parsedate_to_datetime(_unfold(value))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None, False
    if moment is None:
        return None, False
    if moment.utcoffset() is not None:
        return moment, False
    return moment.replace(tzinfo=timezone.utc), "-0000" not in value


def _usable(moment: datetime | None, now: datetime) -> bool:
    try:
        return moment is not None and moment <= now + FUTURE_SLACK
    except OverflowError:
        return False


def _received_value(message: Message) -> str | None:
    value = _first_header(message, "received")
    return _unfold(value).rsplit(";", 1)[1] if value is not None and ";" in value else None


@dataclass
class _Parts:
    refs: frozenset[str] = frozenset()  # 正文里引用着的 Content-ID（_cid_refs）
    plain: list[str] = field(default_factory=list)
    html: list[str] = field(default_factory=list)
    attachments: list[_RawAttachment] = field(default_factory=list)
    fallback: bool = False
    broken: bool = False


def _filename(part: Message, charsets: Sequence[str | None]) -> str | None:
    try:
        name = part.get_filename()
    except Exception:  # noqa: BLE001 参数写得乱的头，email 包会抛各种异常
        return None
    if not name:
        return None
    return " ".join(_decode_words(name, charsets).split()) or None


def _payload_bytes(part: Message) -> bytes:
    try:
        data = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        data = None
    if isinstance(data, bytes):
        return data
    raw = part.get_payload()
    return raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else b""


def _content_type(part: Message) -> str:
    try:
        return part.get_content_type()
    except Exception:  # noqa: BLE001
        return "application/octet-stream"


def _disposition(part: Message) -> str | None:
    try:
        return part.get_content_disposition()
    except Exception:  # noqa: BLE001
        return None


def _charset(part: Message) -> str | None:
    try:
        return part.get_content_charset()
    except Exception:  # noqa: BLE001
        return None


def _body_text(part: Message) -> tuple[str, bool]:
    return _decode_bytes(_payload_bytes(part), [_charset(part)])


# 纯文字正文里，发信的客户端把引用的 > 写成了 &gt;（D93）：行首的几个（中间可以隔空格）换回 >
_PLAIN_QUOTE = re.compile(r"^([ \t]*)((?:&gt;[ \t]*)+)", re.MULTILINE)


def _plain_body(text: str) -> str:
    """纯文字的正文（D93，规范第 6 节）：发信的客户端写进去的 &nbsp; 换成空格，行首的 &gt; 换成 >，L1 才认得出那几行是引用；
    别的实体（&amp;、&lt;、句子中间的 &gt;、编号写的字）照录，读信的人看到的就是它们。"""
    text = text.replace("&nbsp;", " ")
    return _PLAIN_QUOTE.sub(lambda match: match[1] + match[2].replace("&gt;", ">"), text)


_BOUNDARY_LINE = re.compile(rb"^--([0-9A-Za-z'()+_,./:=?-]{1,70}?)(--)?[ \t]*\r?$", re.MULTILINE)
_CTE_BASE64 = re.compile(r"\s*content-transfer-encoding\s*:\s*base64\s*", re.IGNORECASE)
_BASE64_TEXT = re.compile(r"[A-Za-z0-9+/=]+")


def _recovered_children(part: Message) -> list[Message]:
    """缺 boundary、boundary 对不上的 multipart：正文里像分隔线、而且出现两次以上的那个当 boundary 重新切。

    只出现一次的不算：正文里写一行 --verbose 不该把它前面的话切掉。
    """
    payload = part.get_payload()
    if not isinstance(payload, str):
        return []
    raw = payload.encode("utf-8", "surrogateescape")
    counts: dict[bytes, int] = {}
    for match in _BOUNDARY_LINE.finditer(raw):
        counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    boundary = next((token for token, count in counts.items() if count >= 2), None)
    if boundary is None:
        return []
    rebuilt = email.message_from_bytes(
        b'Content-Type: multipart/mixed; boundary="' + boundary + b'"\r\n\r\n' + raw, policy=email.policy.default
    )
    if not rebuilt.is_multipart():
        return []
    return [child for child in rebuilt.get_payload() if isinstance(child, Message)]


def _drop_base64_runs(text: str) -> str:
    """切不开的 multipart 整段当正文时，附件的 base64 不进正文（规范第 6 节）。

    只去掉写着 Content-Transfer-Encoding: base64 的那一段头后面、空行之后连着的 base64 行，
    正文里自己写的几行校验和不动。
    """
    kept: list[str] = []
    armed = in_payload = False
    for line in text.split("\n"):
        stripped = line.strip()
        if in_payload:
            if stripped and _BASE64_TEXT.fullmatch(stripped):
                continue
            in_payload = False
        elif armed and not stripped:
            armed, in_payload = False, True
        elif _CTE_BASE64.fullmatch(line):
            armed = True
        kept.append(line)
    return "\n".join(kept)


def _is_body_part(part: Message, charsets: Sequence[str | None]) -> bool:
    return (
        not part.is_multipart()
        and _content_type(part) in ("text/plain", "text/html")
        and _disposition(part) != "attachment"
        and _filename(part, charsets) is None
    )


def _choose_alternative(children: list[Message], charsets: Sequence[str | None]) -> Message:
    """有不空的纯文字就用它，没有就用最后一个（最完整的那个）。"""
    for child in children:
        if _content_type(child) == "text/plain" and _is_body_part(child, charsets) and _body_text(child)[0].strip():
            return child
    return children[-1]


def _collect(part: Message, parts: _Parts, charsets: Sequence[str | None], *, body: bool = True) -> None:
    """正文：text/plain 优先，没有才用 text/html。别的都算附件，只记名字、类型、大小。

    body 是 False 时这一支没被选作正文（multipart/alternative 的另几份），只收它里面的附件。
    """
    kind = _content_type(part)
    if kind == "message/rfc822":  # 作为附件转发的整封邮件，内容不进包
        inner = part.get_payload()
        try:
            size = len(inner[0].as_bytes()) if isinstance(inner, list) and inner else len(_payload_bytes(part))
        except Exception:  # noqa: BLE001 里面那封写不回字节时，大小记 0，不能把整封信丢掉
            size = 0
        parts.attachments.append(_RawAttachment(_filename(part, charsets) or "未命名.eml", kind, size, None, None))
        return
    if part.is_multipart():
        children = [child for child in part.get_payload() if isinstance(child, Message)]
        if kind == "multipart/alternative" and children:
            chosen = _choose_alternative(children, charsets)
            for child in children:
                _collect(child, parts, charsets, body=body and child is chosen)
            return
        for child in children:
            _collect(child, parts, charsets, body=body)
        return
    if kind.startswith("multipart/"):  # 说是 multipart，却没切开：缺 boundary 或者对不上
        parts.broken = True
        children = _recovered_children(part)
        if children:  # 正文里认得出分隔线：按它重新切，附件还是附件
            for child in children:
                _collect(child, parts, charsets, body=body)
        elif body:  # 认不出：整段当正文，但附件的 base64 不要
            text, fallback = _body_text(part)
            parts.plain.append(_drop_base64_runs(text))
            parts.fallback = parts.fallback or fallback
        return
    if _is_body_part(part, charsets):
        if body:
            text, fallback = _body_text(part)
            parts.fallback = parts.fallback or fallback
            (parts.plain if kind == "text/plain" else parts.html).append(text)
        return  # 没被选作正文的另一份正文不算附件
    data = _payload_bytes(part)
    name = _filename(part, charsets) or "未命名附件"
    if _inline_image(part, kind, name, parts.refs):  # 生成编号时还算着它（_generated_id），所以照样留一份名字、类型、大小
        parts.attachments.append(_RawAttachment(name, kind, len(data), None, None, inline=True))
        return
    parts.attachments.append(_RawAttachment(name, kind, len(data), data, _charset(part)))


# 正文里引用内嵌图：HTML 里的 src="cid:…"，纯文字那一份里的 [cid:…] 占位
_CID_REF = re.compile(rb"cid:([^\"'\s<>()\[\]]+)", re.IGNORECASE)


def _content_id_key(value: str) -> str:
    """Content-ID 比较用的样子：去掉尖括号和空白，不分大小写。"""
    return value.strip().strip("<>").strip().lower()


def _cid_refs(part: Message, charsets: Sequence[str | None]) -> frozenset[str]:
    """这封信当正文用的那几份（HTML、纯文字，和 _collect 认的一样：没被选作正文的另一份也算）里引用着哪些 Content-ID。
    cid: 后面的按网址写法解开（%40 是 @）。D93 第 21 条：HTML 附件里的、作为附件转发的整封信里的不算；
    说是 multipart 却没切开的，按正文里的分隔线重新切开以后照样扫。"""
    kind = _content_type(part)
    if kind == "message/rfc822":
        return frozenset()
    if part.is_multipart():
        children = [child for child in part.get_payload() if isinstance(child, Message)]
    elif kind.startswith("multipart/"):
        children = _recovered_children(part)
        if not children:  # 认不出分隔线：整段当正文
            return _refs_in(part)
    else:
        return _refs_in(part) if _is_body_part(part, charsets) else frozenset()
    found: set[str] = set()
    for child in children:
        found |= _cid_refs(child, charsets)
    return frozenset(found)


def _refs_in(part: Message) -> frozenset[str]:
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception:  # noqa: BLE001 写得乱的部分，email 包会抛各种异常
        return frozenset()
    if not isinstance(payload, bytes):
        return frozenset()
    return frozenset(_content_id_key(urllib.parse.unquote(match[1].decode("latin-1"))) for match in _CID_REF.finditer(payload))


def _inline_image(part: Message, kind: str, name: str, refs: frozenset[str]) -> bool:
    """内嵌图（D91）：图片（类型说不清的看扩展名），它的 Content-ID 在这封信的正文里被引用着（_cid_refs）。签名里的小图、
    贴进正文的截图都是这样（Outlook、Gmail、网易、Foxmail）。Content-Disposition 怎么写不看（审阅第 21 条）：
    没被引用的（回形针加的照片、扫描仪发来的整封一张图）是真的附件；被引用着的，标的是附件也算内嵌图。"""
    if not refs or kind_of(kind, name) != KIND_IMAGE:
        return False
    try:
        content_id = _content_id_key(str(part.get("Content-ID") or ""))
    except Exception:  # noqa: BLE001 写得乱的头：当真的附件
        return False
    return bool(content_id) and content_id in refs


def _text_charsets(message: Message) -> list[str | None]:
    found = []
    for part in message.walk():
        charset = _charset(part)
        if charset and charset not in found:
            found.append(charset)
    return found


def _generated_id(message: Message, attachments: Sequence[_RawAttachment], body: str) -> str:
    """原信没有 Message-ID 时按内容生成（规范第 6 节）。

    用原样的邮件头，不用算出来的时间：Date 不可信时用 Received，过几天再导可能又可信了，编号不能变。
    收件人和附件也算进去：同一秒、同一个人、同样的主题和正文发给不同的人，是两封信。
    """
    fields = [_unfold(value) for name in ("from", "to", "cc", "date", "received", "subject") for value in _raw_headers(message, name)[:1]]
    parts = [*fields, *(f"{item.name}|{item.type}|{item.size}" for item in attachments), body]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8", "surrogateescape")).hexdigest()
    return f"<generated-{digest[:32]}@{GENERATED_DOMAIN}>"


@dataclass(frozen=True)
class MailHead:
    """一封信读完了邮件头和正文、还没读附件的正文（D88）。编号和内容指纹都已经算得出来：导入时先拿它认
    message_id，导过的、这一批里重复的到这里就跳过，附件的正文不读（PDF 要起子进程，一个几秒到 60 秒）。

    它拿着附件的字节，导入时只活在 _take 里，这封信读完附件就放掉。打码的数分开记，读完附件再按原来的先后
    拼起来（报告里类型的先后不变）：显示名和主题，每个附件的正文和附件名，最后是正文。
    """

    mail: Mail  # attachments 还是空的，redactions 也是空的
    attachments: tuple[_RawAttachment, ...]
    patterns: tuple[RedactionPattern, ...]
    head_counts: Mapping[str, int]
    body_counts: Mapping[str, int]


def _add_counts(counts: dict[str, int], found: Mapping[str, int]) -> None:
    for kind, count in found.items():
        counts[kind] = counts.get(kind, 0) + count


def parse_mail(location: str, raw: bytes, *, now: datetime, patterns: Sequence[RedactionPattern]) -> Mail | BadMail:
    """读一封信，连同附件的正文。不进包的返回 BadMail，带原因（read_head、read_attachments）。"""
    head = read_head(location, raw, now=now, patterns=patterns)
    return head if isinstance(head, BadMail) else read_attachments(head)


def read_head(location: str, raw: bytes, *, now: datetime, patterns: Sequence[RedactionPattern]) -> MailHead | BadMail:
    """读邮件头和正文，附件只记名字、类型、大小和字节（D88）。外来的信什么样都有：读的时候出了任何想不到的错，
    都记成 unreadable，这一封不进包，别的照导。"""
    try:
        return _read_head(location, raw, now=now, patterns=patterns)
    except Exception:  # noqa: BLE001
        return BadMail(location, BAD_UNREADABLE)


def read_attachments(head: MailHead) -> Mail | BadMail:
    """读附件的正文，读完字节就不要了。读的时候出了任何想不到的错，这一封记成 unreadable，别的照导。
    读 PDF、Excel、Word 的子进程起不来（PdfUnavailable）不是这封信的毛病，往外抛：整批停下，一个包都不投，
    也不记成导过（D79 第 11 条）。"""
    try:
        return _read_attachments(head)
    except PdfUnavailable:
        raise
    except Exception:  # noqa: BLE001
        return BadMail(head.mail.location, BAD_UNREADABLE)


def _read_head(location: str, raw: bytes, *, now: datetime, patterns: Sequence[RedactionPattern]) -> MailHead | BadMail:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    if not any(key.lower() in _HEADER_NAMES for key in message.keys()):
        return BadMail(location, BAD_NOT_A_MAIL)
    charsets = _text_charsets(message)
    notes: list[str] = []

    senders = _addresses(message, "from", charsets, notes)
    if not senders:
        return BadMail(location, BAD_NO_FROM)
    moment, no_zone = _parse_date(_first_header(message, "date"))
    if not _usable(moment, now):
        moment, no_zone = _parse_date(_received_value(message))
        if not _usable(moment, now):
            return BadMail(location, BAD_NO_DATE)
        notes.append(NOTE_DATE_FROM_RECEIVED)
    if no_zone:
        notes.append(NOTE_DATE_NO_ZONE)

    parts = _Parts(refs=_cid_refs(message, charsets))
    _collect(message, parts, charsets)
    if parts.plain:
        body = "\n".join(_plain_body(text) for text in parts.plain)
    else:
        body = ""
        if parts.html:
            body, used_fallback = _html_to_text("\n".join(parts.html))
            notes.append(NOTE_HTML_ONLY)
            if used_fallback:
                notes.append(NOTE_HTML_FALLBACK)
    if parts.fallback:
        notes.append(NOTE_CHARSET_FALLBACK)
    if parts.broken:
        notes.append(NOTE_BROKEN_MULTIPART)
    body = clean_text(body).rstrip("\n")
    subject_header = _first_header(message, "subject")
    subject = _decode_words(subject_header, charsets) if subject_header is not None else ""
    subject = " ".join(subject.split("\n"))

    message_id = _message_id(_first_header(message, "message-id"), charsets)
    if message_id is None:
        message_id = _generated_id(message, parts.attachments, body)
        notes.append(NOTE_GENERATED_ID)
    references = tuple(
        _MESSAGE_ID.findall(" ".join(clean_text(_repair(_unfold(value), charsets)) for value in _raw_headers(message, "references")))
    )
    in_reply_to = _message_id(_first_header(message, "in-reply-to"), charsets)
    to = _addresses(message, "to", charsets, notes)
    cc = _addresses(message, "cc", charsets, notes)
    bcc = _addresses(message, "bcc", charsets, notes)

    head_counts: dict[str, int] = {}
    body_counts: dict[str, int] = {}

    def hide(text: str, counts: dict[str, int]) -> str:
        redacted, found = redact(text, patterns)
        _add_counts(counts, found)
        return redacted

    def hide_names(addresses: Sequence[Address]) -> tuple[Address, ...]:
        return tuple(Address(hide(item.name, head_counts) if item.name else None, item.addr) for item in addresses)

    sender, shown_to, shown_cc, shown_bcc = hide_names(senders[:1])[0], hide_names(to), hide_names(cc), hide_names(bcc)
    shown_subject = hide(subject, head_counts)
    mail = Mail(
        location=location,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        sender=sender,
        to=shown_to,
        cc=shown_cc,
        bcc=shown_bcc,
        date=moment,
        subject=shown_subject,
        attachments=(),
        body=hide(body, body_counts),
        notes=list(dict.fromkeys(notes)),
    )
    return MailHead(mail, tuple(parts.attachments), tuple(patterns), head_counts, body_counts)


def _read_attachments(head: MailHead) -> Mail:
    """附件名打码；读得了的读正文（packet-spec 第 3 节“附件小节”），正文也打码。"""
    counts = dict(head.head_counts)
    budget = PdfBudget()  # 这封信的 PDF 共用：有一个读到点还没读完，后面的不再读（D79 第 12 条）
    attachments = []
    for item in head.attachments:
        if item.inline:  # 内嵌图不读、不进名单，只数几张
            continue
        content = read_attachment(
            item.data or b"", media_type=item.type, name=item.name, patterns=head.patterns, charset=item.charset, budget=budget
        )
        _add_counts(counts, content.redactions)
        name, found = redact(item.name, head.patterns)
        _add_counts(counts, found)
        attachments.append(Attachment(name, item.type, item.size, content))
    _add_counts(counts, head.body_counts)
    notes = [*head.mail.notes, NOTE_PDF_SKIPPED] if budget.skipped else list(head.mail.notes)
    inline = sum(1 for item in head.attachments if item.inline)
    return replace(head.mail, attachments=tuple(attachments), inline_images=inline, notes=notes, redactions=counts)


# ---------- 读文件 ----------


def _finish(lines: list[bytes]) -> bytes:
    if lines and not lines[-1].strip():
        lines = lines[:-1]  # 分隔前的那个空行是 mbox 加的
    return b"".join(lines)


def _mbox_messages(lines: Iterable[bytes]) -> Iterator[bytes]:
    """按 mboxrd 切：空行后面以 “From ” 开头的行是分隔，正文里的 “>From ” 去掉一个 >。

    开头的 BOM 和空行不算。第一行有内容的不是 “From ” 的不是 mbox，整个当一封信。只有空白的一封都没有。
    """
    iterator = iter(lines)
    leading: list[bytes] = []
    first: bytes | None = None
    for number, line in enumerate(iterator):
        if number == 0 and line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        if line.strip():
            first = line
            break
        leading.append(line)
    if first is None:
        return
    if not first.startswith(b"From "):
        yield b"".join([*leading, first, *iterator])
        return
    current: list[bytes] = []
    previous_blank = False
    for line in iterator:
        if line.startswith(b"From ") and previous_blank:
            yield _finish(current)
            current = []
        else:
            current.append(re.sub(rb"^>(>*From )", rb"\1", line))
        previous_blank = not line.strip()
    yield _finish(current)


def split_mbox(data: bytes) -> list[bytes]:
    """切一个 mbox。空文件一封都没有。"""
    return list(_mbox_messages(io.BytesIO(data)))


def iter_mbox(path: Path) -> Iterator[bytes]:
    """一边读一边切，几 GB 的导出也不用整个读进内存。"""
    with open(path, "rb") as stream:
        yield from _mbox_messages(stream)


def _starts_like_mbox(path: Path) -> bool:
    try:
        with open(path, "rb") as stream:
            head = stream.read(_SNIFF_BYTES)
    except OSError:
        return False
    return head.removeprefix(b"\xef\xbb\xbf").lstrip(b"\r\n \t").startswith(b"From ")


def _is_mbox(path: Path) -> bool:
    """.mbox、名字就叫 mbox 的、没后缀又以 From 开头的才是 mbox。.eml 永远是一封：
    有的客户端存 .eml 时开头带一行 mbox 的信封行，正文里空行后面的 “From what I see” 不能当分隔。"""
    suffix = path.suffix.lower()
    if suffix == ".eml":
        return False
    return suffix == ".mbox" or path.name.lower() == "mbox" or (suffix == "" and _starts_like_mbox(path))


def _one_mail(data: bytes) -> bytes:
    """一封信的文件：去掉开头的 BOM 和 mbox 信封行（From 开头、后面不是冒号）。"""
    data = data.removeprefix(b"\xef\xbb\xbf")
    if data.startswith(b"From "):
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    return data


def _mail_files(directory: Path) -> list[Path]:
    """目录里的 .eml、.mbox，Apple Mail 导出目录里叫 mbox 的文件，还有没后缀、内容像 mbox 的（Thunderbird）。"""
    found = []
    for item in sorted(directory.rglob("*")):
        if not item.is_file():
            continue
        suffix = item.suffix.lower()
        if suffix in (".eml", ".mbox") or item.name.lower() == "mbox" or (suffix == "" and _starts_like_mbox(item)):
            found.append(item)
    return found


def iter_inputs(paths: Sequence[Path]) -> Iterator[tuple[str, bytes]]:
    """给出 (位置, 一封信的原始字节)。按名字排，每次顺序一样。"""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(_mail_files(path))
        elif path.is_file():
            files.append(path)
        else:
            raise ConfigError("要导入的文件或目录不存在")
    for file in files:
        if _is_mbox(file):
            for number, raw in enumerate(iter_mbox(file), 1):
                yield f"{file}#{number}", raw
        else:
            yield str(file), _one_mail(file.read_bytes())


# ---------- 状态 ----------


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalized_subject(subject: str) -> str:
    return " ".join(_REPLY_PREFIX.sub("", subject).split()).casefold()


class State:
    """导入工具自己的状态：处理过哪些 message_id、往来串、用过的包号、例行包接到哪了。

    一个来源、一个 env 一份：正式库和测试库各算各的（规范第 10 节）。主题和地址只存指纹，不存原文。
    """

    def __init__(self, path: Path, source: str, mailbox: str, env: str) -> None:
        self.path = path
        self.source, self.mailbox, self.env = source, mailbox, env
        self.messages: dict[str, dict[str, str]] = {}
        self.threads: dict[str, dict[str, object]] = {}
        self.packets: list[str] = []
        self.covers: dict[str, str] = {}
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise ConfigError("导入工具的状态文件读不了") from None
        if not isinstance(data, Mapping) or data.get("format") != STATE_FORMAT:
            raise ConfigError("导入工具的状态文件格式不对")
        if (data.get("source"), data.get("mailbox"), data.get("env")) != (source, mailbox, env):
            raise ConfigError("状态文件是别的来源、别的邮箱或者别的 env 的")
        messages, threads = data.get("messages", {}), data.get("threads", {})
        packets, covers = data.get("packets", []), data.get("covers", {})
        well_formed = (
            isinstance(messages, dict)
            and all(isinstance(info, dict) and isinstance(info.get("thread"), str) for info in messages.values())
            and isinstance(threads, dict)
            and all(isinstance(info, dict) for info in threads.values())
            and isinstance(packets, list)
            and all(isinstance(item, str) for item in packets)
            and isinstance(covers, dict)
            and all(isinstance(item, str) for item in covers.values())
        )
        if not well_formed:
            raise ConfigError("导入工具的状态文件格式不对")
        self.messages, self.threads, self.packets, self.covers = dict(messages), dict(threads), list(packets), dict(covers)
        if data.get("digest_version") != DIGEST_VERSION:  # 旧版本的规矩算的指纹，和新算的比不出内容变没变：不用
            for info in self.messages.values():
                info.pop("digest", None)

    def save(self) -> None:
        """先写临时文件再改名，断在中间也不会留下半个状态文件。不缩进，状态大了也省一点。"""
        data = {
            "format": STATE_FORMAT,
            "digest_version": DIGEST_VERSION,
            "source": self.source,
            "mailbox": self.mailbox,
            "env": self.env,
            "messages": self.messages,
            "threads": self.threads,
            "packets": self.packets,
            "covers": self.covers,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    """同一个来源同一时间只有一个导入在跑，不然两边都以为那几封没导过。进程死了系统会放开锁。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ImportBusy("这个来源已经有一个导入在跑") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


# ---------- 往来串和可见范围 ----------


def _people(mail: Mail, mailbox: str) -> set[str]:
    """来往的人，不算这个邮箱自己，每封信里都有它。"""
    everyone = {item.addr.lower() for item in (mail.sender, *mail.to, *mail.cc)}
    return {_digest(addr) for addr in everyone - {mailbox.lower()}}


def resolve_threads(mails: Sequence[Mail], state: State, mailbox: str) -> dict[str, dict[str, object]]:
    """thread_id 取往来串第一封的 message_id（规范第 6 节）。

    有 References 用它的第一项，没有就用 In-Reply-To，都没有就是自己。指到的那封已经认得的，
    跟它在同一串。两个都没有、主题却带 Re: 这类前缀的，按去掉前缀的主题和来往的人找以前的串。
    返回更新后的往来串表，导完才写回状态。
    """
    known = {message_id: info["thread"] for message_id, info in state.messages.items()}
    threads = {thread: dict(info) for thread, info in state.threads.items()}
    for mail in sorted(mails, key=lambda item: item.order):
        people = _people(mail, mailbox)
        subject = _digest(normalized_subject(mail.subject))
        if mail.references:
            thread = known.get(mail.references[0], mail.references[0])
        elif mail.in_reply_to:
            thread = known.get(mail.in_reply_to, mail.in_reply_to)
        elif _REPLY_PREFIX.match(mail.subject) and normalized_subject(mail.subject):
            # 去掉前缀什么都不剩的（主题就是一个 Re:）不猜，不然会挂到任何一个空主题的串上
            candidates = [
                (str(info.get("last", "")), thread_id)
                for thread_id, info in threads.items()
                if info.get("subject") == subject and people & set(info.get("people", []))
            ]
            thread = max(candidates)[1] if candidates else mail.message_id
            if candidates:
                mail.notes.append(NOTE_THREAD_BY_SUBJECT)
        else:
            thread = mail.message_id
        mail.thread_id = thread
        known[mail.message_id] = thread
        info = threads.setdefault(thread, {"subject": subject, "people": [], "last": ""})
        info["people"] = sorted(set(info["people"]) | people)
        info["last"] = max(str(info["last"]), iso(mail.date.astimezone(timezone.utc)))
    return threads


def assign_scopes(mails: Sequence[Mail], config: MailConfig, threads: dict[str, dict[str, object]], registry: Registry, source: str) -> None:
    """每封先按自己的规则分。往来串里先来的信带了敏感标记，后来的回信也加上这些标记：回信可能带着引用。

    只加标记，不动分区：标记每个都要能看（D31），多一个标记只会让更少的人看得到，不会放宽。
    只往后传：比它早的信引用不到它，不动。以前导过的串，标记从状态里接着传。
    """
    may_carry = registry.sources[source].may_carry
    carried: dict[str, set[str]] = {}
    for thread_id, info in threads.items():
        stored = info.get("sensitive")
        if isinstance(stored, list):
            carried[thread_id] = {tag for tag in stored if isinstance(tag, str) and tag in may_carry}
    for mail in sorted(mails, key=lambda item: item.order):
        own = config.scope_for(mail.sender, mail.subject)
        inherited = carried.get(mail.thread_id, set()) - set(own.sensitive)
        mail.scope = Scope(own.domain, own.sensitive + tuple(sorted(inherited)))
        if inherited:
            mail.notes.append(NOTE_THREAD_SCOPE)
        carried.setdefault(mail.thread_id, set()).update(mail.scope.sensitive)
    for thread_id, tags in carried.items():
        info = threads.setdefault(thread_id, {"subject": "", "people": [], "last": ""})
        info["sensitive"] = sorted(tags)


# ---------- 分包和写包 ----------


@dataclass
class PlannedPacket:
    scope: Scope
    mails: list[Mail]
    covers_from: datetime
    covers_to: datetime
    packet_id: str = ""
    continues: str | None = None
    group: int = 0  # 同一段时间里因为太长切开的几包是一组，后一包接前一包

    @property
    def chars(self) -> int:
        return sum(mail.size for mail in self.mails)


def _chunks(mails: Sequence[Mail], limit: int) -> list[list[Mail]]:
    """太长就把时间段切短，在两封邮件之间切（规范第 6 节）。一封再长也不拆。"""
    chunks: list[list[Mail]] = [[]]
    used = 0
    for mail in sorted(mails, key=lambda item: item.order):
        if limit > 0 and chunks[-1] and used + mail.size > limit:
            chunks.append([])
            used = 0
        chunks[-1].append(mail)
        used += mail.size
    return chunks


def _thread_order(mails: Sequence[Mail]) -> list[Mail]:
    """同一往来串排在一起，串内按 date 从早到晚，不同的串按各自第一封排（规范第 6 节）。"""
    first: dict[str, tuple[float, str]] = {}
    for mail in mails:
        first[mail.thread_id] = min(first.get(mail.thread_id, mail.order), mail.order)
    return sorted(mails, key=lambda mail: (first[mail.thread_id], mail.thread_id, mail.order))


def plan_packets(
    mails: Sequence[Mail],
    *,
    kind: str,
    zone: tzinfo,
    max_chars: int,
    state: State,
    now: datetime,
    source: str,
) -> list[PlannedPacket]:
    """按可见范围分开；补历史和即时包按天切，例行包从这个邮箱上一次的 covers_to 接到这次封包。"""
    planned: list[PlannedPacket] = []
    group = 0
    previous = parse_aware(state.covers.get(_MAILBOX_KEY))
    scopes = sorted({mail.scope for mail in mails if mail.scope is not None}, key=lambda scope: scope.key)
    for scope in scopes:
        mine = [mail for mail in mails if mail.scope == scope]
        if kind == "daily":
            group += 1
            chunks = _chunks(mine, max_chars)
            start = previous.astimezone(zone) if previous else None
            for number, chunk in enumerate(chunks, 1):
                earliest = min(mail.date for mail in chunk).astimezone(zone)
                latest = max(mail.date for mail in chunk).astimezone(zone)
                covers_from = min(start, earliest) if start is not None else earliest
                covers_to = max(now.astimezone(zone), latest) if number == len(chunks) else latest
                planned.append(PlannedPacket(scope, chunk, covers_from, covers_to, group=group))
                start = covers_to
            continue
        days: dict[calendar_date, list[Mail]] = {}
        for mail in mine:
            days.setdefault(mail.date.astimezone(zone).date(), []).append(mail)
        for day in sorted(days):
            group += 1
            for chunk in _chunks(days[day], max_chars):
                covers_from = min(mail.date for mail in chunk).astimezone(zone)
                covers_to = max(mail.date for mail in chunk).astimezone(zone)
                planned.append(PlannedPacket(scope, chunk, covers_from, covers_to, group=group))
    used = set(state.packets)
    for packet in sorted(planned, key=lambda item: (item.covers_to, item.scope.key)):
        packet.packet_id = next_packet_id(packet.covers_to, source, used)
        used.add(packet.packet_id)
        packet.mails = _thread_order(packet.mails)
    last_of_group: dict[int, str] = {}
    for packet in planned:  # 同一组按切开的先后排着
        packet.continues = last_of_group.get(packet.group)
        last_of_group[packet.group] = packet.packet_id
    return sorted(planned, key=lambda item: item.packet_id)


def _address_yaml(address: Address) -> str:
    name = yaml_text(address.name) if address.name else "null"
    return f"{{name: {name}, addr: {yaml_text(address.addr)}}}"


def _address_list(field_name: str, addresses: Sequence[Address]) -> list[str]:
    if not addresses:
        return [f"{field_name}: []"]
    return [f"{field_name}:", *(f"  - {_address_yaml(address)}" for address in addresses)]


def render_mail(mail: Mail) -> str:
    """一封邮件一个原话小节：标题、邮件头的 yaml 围栏、空一行、正文每行缩进两格（规范第 6 节）。"""
    stamp = f"{mail.date.year:04d}-{mail.date.month:02d}-{mail.date.day:02d} {mail.date.hour:02d}:{mail.date.minute:02d}"
    header = [
        f"message_id: {yaml_text(mail.message_id)}",
        f"thread_id: {yaml_text(mail.thread_id)}",
        f"in_reply_to: {yaml_text(mail.in_reply_to) if mail.in_reply_to else 'null'}",
        f"references: [{', '.join(yaml_text(item) for item in mail.references)}]",
        f"from: {_address_yaml(mail.sender)}",
        *_address_list("to", mail.to),
    ]
    if mail.cc:
        header += _address_list("cc", mail.cc)
    if mail.bcc:
        header += _address_list("bcc", mail.bcc)
    header += [f"date: {iso(mail.date)}", f"subject: {yaml_text(mail.subject)}"]
    if mail.attachments:
        header.append("attachments:")
        header += [
            f"  - {{name: {yaml_text(item.name)}, type: {yaml_text(item.type)}, size: {item.size}}}"
            for item in mail.attachments
        ]
    else:
        header.append("attachments: []")
    if mail.inline_images:
        header.append(f"inline_images: {mail.inline_images}")
    block = [f"## 原话 · 邮件 · {stamp}", "", "```yaml", *header, "```"]
    if mail.body:
        block += ["", *(continuation(line) for line in mail.body.split("\n"))]
    return "\n".join(block) + "\n"


def render_packet(packet: PlannedPacket, *, source: str, mailbox: str, env: str, kind: str, created_at: datetime) -> str:
    head = front_matter(
        packet_id=packet.packet_id,
        created_at=created_at,
        source=source,
        framework=FRAMEWORK,
        channel="email",
        env=env,
        kind=kind,
        covers_from=packet.covers_from,
        covers_to=packet.covers_to,
        domain=packet.scope.domain,
        sensitive=packet.scope.sensitive,
        continues=packet.continues,
        mailbox=mailbox,
    )
    text = head + "\n" + "\n".join(render_mail(mail) for mail in packet.mails)
    section = attachment_section([], attachment_blocks(packet.mails))
    return text + ("\n" + section if section else "")


def attachment_blocks(mails: Sequence[Mail]) -> list[str]:
    """包里每封信的附件，一个附件一块，of 指着第几封（packet-spec 第 3 节“附件小节”）。不读的也写一块，记下原因。"""
    blocks = []
    for number, mail in enumerate(mails, 1):
        for item in mail.attachments:
            content = item.content or AttachmentText(TEXT_NONE, REASON_UNSUPPORTED)
            blocks.append(
                attachment_block(
                    name=item.name,
                    media_type=item.type,
                    size=item.size,
                    of_section=number,
                    of_line=None,
                    state=content.state,
                    reason=content.reason,
                    text=content.text,
                )
            )
    return blocks


# ---------- 一次导入 ----------


@dataclass
class ImportReport:
    """一次导入的结果。只有位置、编号和数，不抄邮件内容。"""

    source: str
    started_at: str
    env: str
    kind: str
    seen: int = 0
    imported: int = 0
    already_imported: int = 0  # 以前导过，这次跳过
    duplicates: int = 0  # 这一批里重复的
    # 同一个编号改了以后重发、这一批里没导的那几封（D91）：位置、Date，导的是哪一封、它的 Date（kept、kept_date）；
    # 以前的批次已经导过的（D93）：位置、Date，以前导进的是哪个包（kept_packet）
    resent: list[dict[str, str]] = field(default_factory=list)
    bad: list[dict[str, str]] = field(default_factory=list)
    notes: dict[str, int] = field(default_factory=dict)
    redactions: dict[str, int] = field(default_factory=dict)
    attachments: dict[str, int] = field(default_factory=dict)  # 附件收了多少：full、truncated、none:原因
    packets: list[dict[str, object]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    report_path: str | None = None

    @property
    def ok(self) -> bool:
        return not self.bad and not self.rejected and self.seen > 0

    def add(self, counts: dict[str, int], items: Mapping[str, int] | Sequence[str]) -> None:
        pairs = items.items() if isinstance(items, Mapping) else ((item, 1) for item in items)
        for name, count in pairs:
            counts[name] = counts.get(name, 0) + count

    def to_dict(self) -> dict[str, object]:
        data = dict(vars(self))
        data.pop("report_path")
        data["ok"] = self.ok
        return data


def _write_report(report: ImportReport, report_dir: Path, now: datetime) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime('%Y%m%d-%H%M%S')}_{report.source}_{report.env}"
    path = report_dir / f"{stem}.json"
    number = 2
    while path.exists():
        path = report_dir / f"{stem}_{number}.json"
        number += 1
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _inside(child: Path, parent: Path) -> bool:
    child, parent = child.resolve(), parent.resolve()
    return child == parent or parent in child.parents


def import_mail(
    inputs: Sequence[Path],
    *,
    source: str,
    registry: Registry,
    config: MailConfig,
    patterns: Sequence[RedactionPattern],
    out_dir: Path,
    work_dir: Path,
    zone: tzinfo,
    now: datetime,
    env: str = "test",
    kind: str = "backfill",
    max_chars: int = MAX_RAW_CHARS,
) -> ImportReport:
    """导一批邮件，包写到 out_dir/<来源>/，状态和报告写到 work_dir 下。"""
    mailbox = mail_source(registry, source)
    if kind not in KINDS:
        raise ConfigError(f"kind 只能是 {'、'.join(KINDS)}")
    if _inside(work_dir, out_dir) or _inside(out_dir, work_dir):
        raise ConfigError("导入工具的状态和报告目录不能和输出目录套在一起")
    check_out_dir(out_dir, env)
    now = now.astimezone(zone).replace(microsecond=0)
    report = ImportReport(source=source, started_at=iso(now), env=env, kind=kind)
    report_dir = work_dir / REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)  # 先建好：写不了报告就什么都别做
    with _lock(work_dir / STATE_DIR / f"{source}.lock"):
        state = State(work_dir / STATE_DIR / f"{source}.{env}.json", source, mailbox, env)
        try:
            _import(inputs, report, state, config=config, registry=registry, patterns=patterns, out_dir=out_dir,
                    zone=zone, now=now, env=env, kind=kind, max_chars=max_chars, source=source, mailbox=mailbox)
        finally:
            # 中途出错（磁盘满、文件被占着）也留一份报告：已经投出去的包在里面，状态也存到了那一包
            report.report_path = str(_write_report(report, report_dir, now))
    return report


def _resent(dropped: Mail, kept: Mail) -> dict[str, str]:
    return {"location": dropped.location, "date": iso(dropped.date), "kept": kept.location, "kept_date": iso(kept.date)}


def _take(
    location: str,
    raw: bytes,
    report: ImportReport,
    state: State,
    mails: list[Mail],
    seen: dict[str, int],
    *,
    now: datetime,
    patterns: Sequence[RedactionPattern],
) -> None:
    """一封信：先读邮件头和正文，认编号、比指纹，再读附件（D88）。编号和指纹都用不着附件的正文，
    导过的、这一批里重复的不读附件。要导的读完附件放进 mails（seen 记着每个编号在第几个），别的记进报告。

    同一个编号、内容不一样的（改了以后重发的），这一批里留 Date 晚的那封，一样晚的留先来的；没导的那封记进
    报告的 resent（D91）。后来的那封更晚，就读它的附件、换掉先来的那封（先来的那封的附件白读了一遍）。
    以前导过的照旧跳过，附件不读（D88），新版进不来：记 duplicate_id_different_content，resent 里记下这一封的位置、
    Date 和以前导进的是哪个包（D93），要人看一下。

    MailHead 只活在这个函数里：附件的字节到这封信读完就放掉，不留到读下一封、投包的时候。
    """
    head = read_head(location, raw, now=now, patterns=patterns)
    if isinstance(head, BadMail):
        report.bad.append({"location": head.location, "reason": head.reason})
        return
    message_id, digest = head.mail.message_id, head.mail.digest
    known = state.messages.get(message_id)
    if known is not None:
        report.already_imported += 1
        if known.get("digest", digest) != digest:  # 没记指纹的（老条目、版本不对扔掉了的）不比
            report.add(report.notes, [NOTE_ID_CLASH])
            report.resent.append(
                {"location": head.mail.location, "date": iso(head.mail.date), "kept_packet": str(known.get("packet", ""))}
            )
        return
    index = seen.get(message_id)
    if index is not None:
        report.duplicates += 1
        kept = mails[index]
        if kept.digest == digest:
            return
        report.add(report.notes, [NOTE_ID_CLASH])
        if head.mail.date <= kept.date:
            report.resent.append(_resent(head.mail, kept))
            return
    mail = read_attachments(head)
    if isinstance(mail, BadMail):  # 读坏了的不算“这一批里已经有了”：后面同一封的副本照导；更晚的那封读坏了，留着先来的
        report.bad.append({"location": mail.location, "reason": mail.reason})
        return
    if index is not None:
        replaced = mails[index]
        for item in report.resent:  # 以前换下来的，“导的是”写的是现在换下来的这封：改成真导的这封（审阅第 10 条）
            if item.get("kept") == replaced.location:  # 跨批的那几条没有 kept（D93 审阅第 1 条）
                item["kept"], item["kept_date"] = mail.location, iso(mail.date)
        report.resent.append(_resent(replaced, mail))
        mails[index] = mail
        return
    seen[message_id] = len(mails)
    mails.append(mail)


def _import(
    inputs: Sequence[Path],
    report: ImportReport,
    state: State,
    *,
    config: MailConfig,
    registry: Registry,
    patterns: Sequence[RedactionPattern],
    out_dir: Path,
    zone: tzinfo,
    now: datetime,
    env: str,
    kind: str,
    max_chars: int,
    source: str,
    mailbox: str,
) -> None:
    mails: list[Mail] = []
    seen: dict[str, int] = {}  # message_id → 在 mails 里第几个
    for location, raw in iter_inputs(inputs):
        report.seen += 1
        _take(location, raw, report, state, mails, seen, now=now, patterns=patterns)
    if report.seen == 0:
        report.add(report.notes, [NOTE_NO_MAIL])

    threads = resolve_threads(mails, state, mailbox)
    assign_scopes(mails, config, threads, registry, source)
    for packet in plan_packets(mails, kind=kind, zone=zone, max_chars=max_chars, state=state, now=now, source=source):
        name = f"{packet.packet_id}.md"
        text = render_packet(packet, source=source, mailbox=mailbox, env=env, kind=kind, created_at=now)
        try:
            checked = validate_packet(text.encode("utf-8"), filename=name, directory=source, library=env, registry=registry)
        except Rejected as rejected:
            report.rejected.append({"packet_id": packet.packet_id, "code": rejected.code, "detail": rejected.detail})
            continue
        except (OverflowError, ValueError, UnicodeError) as error:  # 只防万一，不让一个包拖垮整批
            report.rejected.append({"packet_id": packet.packet_id, "code": "unwritable", "detail": type(error).__name__})
            continue
        deliver(out_dir / source, name, text)
        warnings: dict[str, int] = {}
        report.add(warnings, [warning.kind for warning in checked.warnings])
        report.packets.append(
            {
                "packet_id": packet.packet_id,
                "path": f"{source}/{name}",
                "mails": len(packet.mails),
                "threads": len({mail.thread_id for mail in packet.mails}),
                "chars": packet.chars,
                "covers_from": iso(packet.covers_from),
                "covers_to": iso(packet.covers_to),
                "domain": list(packet.scope.domain),
                "sensitive": list(packet.scope.sensitive),
                "continues": packet.continues,
                "warnings": warnings,
            }
        )
        # 包投出去了才记成处理过：没投出去的下次还会再导
        for mail in packet.mails:
            state.messages[mail.message_id] = {"thread": mail.thread_id, "packet": packet.packet_id, "digest": mail.digest}
            report.imported += 1
            report.add(report.notes, mail.notes)
            report.add(report.redactions, mail.redactions)
            report.add(
                report.attachments,
                [
                    item.content.state if item.content.state != TEXT_NONE else f"{TEXT_NONE}:{item.content.reason}"
                    for item in mail.attachments
                    if item.content is not None
                ],
            )
        state.packets.append(packet.packet_id)
        if kind == "daily":
            reached = parse_aware(state.covers.get(_MAILBOX_KEY))
            if reached is None or packet.covers_to > reached:
                state.covers[_MAILBOX_KEY] = iso(packet.covers_to)
        state.threads = {thread: info for thread, info in threads.items()}
        state.save()
