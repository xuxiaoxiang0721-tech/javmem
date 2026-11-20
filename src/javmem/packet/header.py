"""解码和包头校验（packet-spec 第 4、9 节）。这里只查和登记表无关的项。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
from datetime import datetime

import yaml

from javmem.packet.errors import BAD_HEADER, NOT_UTF8, Rejected
from javmem.packet.model import (
    ID_TIME_MISMATCH,
    UNKNOWN_FIELD,
    PacketHeader,
    ParseWarning,
)
from javmem.sources import RESERVED_TAG
from javmem.timeutil import parse_aware
from javmem.yamlio import load_yaml

SCHEMA = "javmem-packet/1"
MAX_REVISION = 2**63 - 1  # 账本里放得下的最大整数，再大写库时会溢出
REQUIRED_FIELDS = (
    "schema",
    "packet_id",
    "created_at",
    "source",
    "framework",
    "channel",
    "env",
    "kind",
    "covers_from",
    "covers_to",
    "domain",
    "privacy",
)

KNOWN_FIELDS = frozenset(REQUIRED_FIELDS) | {"sensitive", "entities", "continues", "revision", "mailbox"}

PACKET_ID = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})_([a-z0-9]+(?:-[a-z0-9]+)*)(?:_[1-9]\d*)?", re.ASCII)
_FRAMEWORK = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
_SHOWABLE_FIELD = re.compile(r"[A-Za-z0-9_-]{1,40}", re.ASCII)


def decode(raw: bytes) -> str:
    """严格 UTF-8，带 BOM 不算错。换行符统一成 LF。"""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        line = raw.count(b"\n", 0, error.start) + 1
        raise Rejected(NOT_UTF8, f"第 {line} 行不是合法的 UTF-8") from None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(raw: bytes) -> str | None:
    """文件内容的指纹，换行符不同、带不带 BOM 都算同样的内容。不是 UTF-8 时返回 None。"""
    try:
        return hash_text(decode(raw))
    except Rejected:
        return None


def yaml_error_line(error: BaseException, first_line: int) -> int:
    """YAML 错误对应文件的第几行。错误信息里带原文，所以只取行号。"""
    mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
    return first_line + mark.line if mark is not None else first_line


def split_front_matter(text: str) -> tuple[Mapping, list[str], int]:
    """分开 YAML 头和后面的内容，返回包头字段、后面的行、后面第一行的行号。

    只按 LF 切行。str.splitlines 还会在换页符、U+2028 这些地方切，会把一句话切开。
    """
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        raise Rejected(BAD_HEADER, "文件开头不是 YAML 头")
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            break
    else:
        raise Rejected(BAD_HEADER, "YAML 头没有结束的 ---")

    try:
        fields = load_yaml("\n".join(lines[1:index]))
    except (yaml.YAMLError, RecursionError) as error:
        raise Rejected(BAD_HEADER, f"YAML 头解析不了，第 {yaml_error_line(error, 2)} 行") from None
    if not isinstance(fields, Mapping):
        raise Rejected(BAD_HEADER, "YAML 头的写法不对")
    return fields, lines[index + 1 :], index + 2


def unknown_field_warnings(fields: Mapping, header_lines: list[str]) -> list[ParseWarning]:
    """认不出的包头字段只提醒。规范没说要拒，但 sensitive 打错字，这个包就没有标记了。

    header_lines 是 YAML 头的内容，第一项在文件的第 2 行。字段名长得奇怪就不抄。
    """
    warnings = []
    for name in fields:
        if name in KNOWN_FIELDS:
            continue
        showable = isinstance(name, str) and _SHOWABLE_FIELD.fullmatch(name) is not None
        line = next(
            (number for number, text in enumerate(header_lines, 2) if showable and text.startswith(f"{name}:")),
            0,
        )
        warnings.append(ParseWarning(UNKNOWN_FIELD, line, name if showable else ""))
    return warnings


def id_time_warnings(header: PacketHeader, header_lines: list[str]) -> list[ParseWarning]:
    """packet_id 的日期和时分要取 covers_to，按它自己的时区（规范第 4 节）。拒收清单没列这条，所以只提醒。"""
    if header.packet_id[:15] == header.covers_to.strftime("%Y-%m-%d_%H%M"):
        return []
    line = next((number for number, text in enumerate(header_lines, 2) if text.startswith("packet_id:")), 0)
    return [ParseWarning(ID_TIME_MISMATCH, line)]


def _fail(message: str) -> Rejected:
    return Rejected(BAD_HEADER, message)


def _time_field(fields: Mapping, name: str) -> datetime:
    moment = parse_aware(fields[name])
    if moment is None:
        raise _fail(f"{name} 要写带时区的时间")
    return moment


def _choice(fields: Mapping, name: str, allowed: tuple[str, ...]) -> str:
    value = fields[name]
    if not isinstance(value, str) or value not in allowed:
        raise _fail(f"{name} 的取值不合规")
    return value


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _fail(f"{name} 要写成文本列表")
    return tuple(dict.fromkeys(value))  # 去重，不改顺序


def _check_packet_id(packet_id: object, *, filename: str, source: str) -> str:
    if not isinstance(packet_id, str):
        raise _fail("packet_id 的格式不对")
    match = PACKET_ID.fullmatch(packet_id)
    if match is None:
        raise _fail("packet_id 的格式不对")
    year, month, day, hour, minute, id_source = match.groups()
    try:
        datetime(int(year), int(month), int(day), int(hour), int(minute))
    except ValueError:
        raise _fail("packet_id 里的日期或时分不对") from None
    if id_source != source:
        raise _fail("packet_id 和 source 对不上")
    if filename != f"{packet_id}.md":
        raise _fail("packet_id 和文件名对不上")
    return packet_id


def check_header(fields: Mapping, *, filename: str, domains: Collection[str] | None) -> PacketHeader:
    """查包头。domains 是允许的分区，None 表示不查（重读已经收下的包时用）。"""
    if "schema" not in fields:
        raise _fail("缺必填字段 schema")
    if fields["schema"] != SCHEMA:
        raise _fail("schema 认不出")
    for name in REQUIRED_FIELDS:
        if name not in fields:
            raise _fail(f"缺必填字段 {name}")

    source = fields["source"]
    if not isinstance(source, str) or not source:
        raise _fail("source 要写成文本")
    packet_id = _check_packet_id(fields["packet_id"], filename=filename, source=source)

    framework = fields["framework"]
    if not isinstance(framework, str) or _FRAMEWORK.fullmatch(framework) is None:
        raise _fail("framework 的取值不合规")

    channel = _choice(fields, "channel", ("chat", "email"))
    env = _choice(fields, "env", ("prod", "test"))
    kind = _choice(fields, "kind", ("daily", "adhoc", "backfill"))
    privacy = _choice(fields, "privacy", ("normal", "private"))

    created_at = _time_field(fields, "created_at")
    covers_from = _time_field(fields, "covers_from")
    covers_to = _time_field(fields, "covers_to")
    if covers_from > covers_to:
        raise _fail("covers_from 晚于 covers_to")

    domain = _string_list(fields["domain"], "domain")
    if not domain:
        raise _fail("domain 不能是空列表")
    if domains is not None and not set(domain) <= set(domains):
        raise _fail("domain 的取值不合规")

    sensitive = _string_list(fields["sensitive"], "sensitive") if "sensitive" in fields else ()
    if RESERVED_TAG in sensitive:
        raise _fail(f"sensitive 不能写保留字 {RESERVED_TAG}")
    entities = _string_list(fields["entities"], "entities") if "entities" in fields else ()

    continues = fields.get("continues")
    if "continues" in fields and (not isinstance(continues, str) or PACKET_ID.fullmatch(continues) is None):
        raise _fail("continues 的格式不对")

    revision = fields.get("revision", 1)
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:  # True 也是 int，所以不用 isinstance
        raise _fail("revision 要写从 1 起的整数")

    mailbox = fields.get("mailbox")
    if channel == "email" and (not isinstance(mailbox, str) or not mailbox):
        raise _fail("邮件包缺 mailbox")
    if mailbox is not None and not isinstance(mailbox, str):
        raise _fail("mailbox 要写成文本")

    return PacketHeader(
        packet_id=packet_id,
        revision=revision,
        created_at=created_at,
        source=source,
        framework=framework,
        channel=channel,
        env=env,
        kind=kind,
        covers_from=covers_from,
        covers_to=covers_to,
        domain=domain,
        privacy=privacy,
        sensitive=sensitive,
        entities=entities,
        continues=continues,
        mailbox=mailbox,
    )
