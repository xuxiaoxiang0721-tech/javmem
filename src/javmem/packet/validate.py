"""校验一个包（packet-spec 第 9 节）。纯函数，不碰文件和库，转换器和导入工具也能直接用。

查的顺序是规范定的，查到第一条就停：
先编码，再包头里和登记表无关的项，再来源，再包头里和登记表有关的项，
再环境，再有没有原话，最后时间范围。放的位置在读内容之前就查，见 intake。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta

from javmem.packet.body import parse_body
from javmem.packet.errors import (
    BAD_HEADER,
    ENV_MISMATCH,
    NO_RAW_SECTION,
    TIME_OUT_OF_RANGE,
    UNKNOWN_SOURCE,
    Rejected,
)
from javmem.packet.header import (
    check_header,
    decode,
    hash_text,
    id_time_warnings,
    split_front_matter,
    unknown_field_warnings,
)
from javmem.packet.model import ChatSection, Packet, PacketHeader
from javmem.sources import Registry

_ONE_DAY = timedelta(days=1)


def parse_packet(raw: bytes, *, filename: str, domains: Collection[str] | None = None) -> Packet:
    """解码、查包头、解析内容。不看登记表。重读已经收下的包也用它。"""
    text = decode(raw)
    fields, lines, first_line = split_front_matter(text)
    header = check_header(fields, filename=filename, domains=domains)
    sections, notes, attachments, warnings = parse_body(header, lines, first_line)
    header_lines = text.split("\n")[1 : first_line - 2]
    warnings = [*unknown_field_warnings(fields, header_lines), *id_time_warnings(header, header_lines), *warnings]
    return Packet(header, text, hash_text(text), sections, notes, warnings, attachments)


def _check_source(header: PacketHeader, directory: str, registry: Registry) -> None:
    if header.source not in registry.sources:
        raise Rejected(UNKNOWN_SOURCE, "source 不在登记表里")
    if header.source != directory:
        raise Rejected(UNKNOWN_SOURCE, "source 和所在的来源目录不一样")


def _check_against_registry(header: PacketHeader, registry: Registry) -> None:
    source = registry.sources[header.source]
    if header.channel != source.channel:
        raise Rejected(BAD_HEADER, "channel 和登记表不一样")
    if header.mailbox != source.mailbox:
        raise Rejected(BAD_HEADER, "mailbox 和登记表不一样")
    if not set(header.sensitive) <= source.may_carry:
        raise Rejected(BAD_HEADER, "sensitive 里有这个来源 may_carry 之外的标记")


def _check_env(header: PacketHeader, library: str, registry: Registry) -> None:
    if library == "prod" and header.env == "test":
        raise Rejected(ENV_MISMATCH, "env 是 test，却投进了正式的收件目录")
    if library == "test" and header.env == "prod":
        raise Rejected(ENV_MISMATCH, "env 是 prod，却投进了测试的收件目录")
    if registry.sources[header.source].env == "test" and header.env == "prod":
        raise Rejected(ENV_MISMATCH, "登记为 test 的来源，包头写了 prod")


def _check_time(moment: datetime, file_line: int, header: PacketHeader) -> None:
    shown = moment.strftime("%Y-%m-%d %H:%M")
    if moment < header.covers_from - _ONE_DAY:
        raise Rejected(TIME_OUT_OF_RANGE, f"第 {file_line} 行的时间 {shown} 早于 covers_from 超过一天")
    if moment > header.covers_to + _ONE_DAY:
        raise Rejected(TIME_OUT_OF_RANGE, f"第 {file_line} 行的时间 {shown} 晚于 covers_to 超过一天")


def _check_times(packet: Packet) -> None:
    """引用行不算，它引的本来就是旧话。邮件查 date。"""
    for section in packet.sections:
        if isinstance(section, ChatSection):
            for utterance in section.utterances:
                _check_time(utterance.said_at, utterance.file_line, packet.header)
        else:
            _check_time(section.headers.date, section.file_line, packet.header)


def validate_packet(raw: bytes, *, filename: str, directory: str, library: str, registry: Registry) -> Packet:
    """按规范校验，不合格抛 Rejected。directory 是来源目录名，library 是 prod 或 test。"""
    packet = parse_packet(raw, filename=filename, domains=registry.domains)
    _check_source(packet.header, directory, registry)
    _check_against_registry(packet.header, registry)
    _check_env(packet.header, library, registry)
    if not packet.sections:
        raise Rejected(NO_RAW_SECTION, "没有原话小节，或者原话小节里一句合规的原话都没有")
    _check_times(packet)
    return packet
