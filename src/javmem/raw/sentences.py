"""分句（query-api 4.5）：把解析好的包变成原文索引的一行一行。

按换行和句末标点切，标点跟着前一句（D39）：

- 中文的 。！？； 直接切，后面紧跟的右引号、右括号也跟着前一句，不会单独成一句，也不会跑到下一句开头。
- 英文的 . ! ? ; 后面跟空白才切，所以 12.5、网址里的 ? 和 ;、代码里的 (?: 都不切。
- 连着的几个句末标点只切一次：？！、?!、！”。 都是。

position.sentence 和原文句的 id 都按这个算。

原话小节和附件小节（说明和附件的正文）进索引，邮件还有主题、发件人、收件人、抄送、密送和附件名。
摘要不进，它不算证据。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from javmem.packet.body import is_quoted
from javmem.packet.model import (
    ORIGIN_ATTACHMENT_NOTE,
    ORIGIN_EXTERNAL,
    ROLE_BOT,
    ROLE_OTHER,
    AttachmentBody,
    ChatSection,
    MailSection,
    Packet,
    PacketHeader,
)

SECTION_RAW = "raw"
SECTION_ATTACHMENT = "attachment"

# 撤回了的邮件主题、发件人：这封信别的句子还在时，查询结果、发给模型的窗口、账本里写成这个（D73 第 16 条）
MASKED = "[已撤回]"
MAIL_WIDE_FIELDS = frozenset({"subject", "from"})  # 跟着整封信每一句走的邮件头

# 中文句末标点后面紧跟着也跟着前一句的右引号、右括号
CLOSERS = "”’」』）】》〉〕］｝"
# 中文的一串句末标点（中间夹着英文的 !?; 也算一串）连同后面的右引号、右括号，可以连着几串；
# 或者英文的一串 .!?; 后面跟着空白
_SENTENCE_END = re.compile(rf"(?:[。！？；][。！？；!?;]*[{CLOSERS}]*)+|[.!?;]+(?=\s)")


@dataclass(frozen=True)
class Sentence:
    """原文索引的一行。"""

    sentence_id: str
    section_kind: str
    section_no: int | None
    line_no: int | None
    sentence_no: int | None
    field: str | None
    text: str
    speaker: str
    speaker_role: str
    said_at: datetime
    origin: str
    quoted: bool = False
    message_id: str | None = None
    thread_id: str | None = None
    subject: str | None = None


def split_sentences(line: str) -> list[str]:
    """切一行。标点跟着前面那句，首尾的空白去掉，空的不要。"""
    parts = []
    start = 0
    for match in _SENTENCE_END.finditer(line):
        parts.append(line[start : match.end()])
        start = match.end()
    parts.append(line[start:])
    return [part.strip() for part in parts if part.strip()]


def _chat_sentences(packet_id: str, section: ChatSection) -> Iterator[Sentence]:
    """一句话连同续行算一行，sentence 在这一行里连着数。"""
    for utterance in section.utterances:
        number = 0
        for line in utterance.lines:
            for text in split_sentences(line.text):
                number += 1
                yield Sentence(
                    sentence_id=f"{packet_id}#{section.number}.{utterance.number}.{number}",
                    section_kind=SECTION_RAW,
                    section_no=section.number,
                    line_no=utterance.number,
                    sentence_no=number,
                    field=None,
                    text=text,
                    speaker=utterance.speaker,
                    speaker_role=utterance.speaker_role,
                    said_at=utterance.said_at,
                    origin=utterance.origin,
                    quoted=line.quoted,
                )


def _mail_sentences(packet_id: str, section: MailSection) -> Iterator[Sentence]:
    """邮件的 line 是正文第几行，空行也数。邮件头每个字段一行，排在正文前面。"""
    headers = section.headers
    common = {
        "section_kind": SECTION_RAW,
        "section_no": section.number,
        "speaker": str(headers.sender),
        "speaker_role": ROLE_OTHER,
        "said_at": headers.date,
        "origin": ORIGIN_EXTERNAL,
        "message_id": headers.message_id,
        "thread_id": headers.thread_id,
        "subject": headers.subject,
    }
    fields = {
        "subject": headers.subject.strip(),
        "from": str(headers.sender),
        "to": ", ".join(str(address) for address in headers.to),
        "cc": ", ".join(str(address) for address in headers.cc),
        "bcc": ", ".join(str(address) for address in headers.bcc),
        # 名单后面只记一句内嵌图有几张（D91）
        "attachments": ", ".join([*headers.attachments, *([f"另有内嵌图 {headers.inline_images} 张"] if headers.inline_images else [])]),
    }
    for field, text in fields.items():
        if text:
            yield Sentence(
                sentence_id=f"{packet_id}#{section.number}.{field}",
                line_no=None,
                sentence_no=None,
                field=field,
                text=text,
                **common,
            )
    for line_no, line in enumerate(section.body, 1):
        stripped = line.strip()
        quoted = is_quoted(stripped)
        for number, text in enumerate(split_sentences(stripped), 1):
            yield Sentence(
                sentence_id=f"{packet_id}#{section.number}.{line_no}.{number}",
                line_no=line_no,
                sentence_no=number,
                field=None,
                text=text,
                quoted=quoted,
                **common,
            )


def _note_sentences(packet: Packet, header: PacketHeader) -> Iterator[Sentence]:
    """附件小节的说明是 Bot 写的，没有自己的时间，said_at 用 covers_to，speaker 用来源编号。"""
    for note in packet.notes:
        for number, text in enumerate(split_sentences(note.text), 1):
            yield Sentence(
                sentence_id=f"{header.packet_id}#attachment.{note.number}.{number}",
                section_kind=SECTION_ATTACHMENT,
                section_no=None,
                line_no=note.number,
                sentence_no=number,
                field=None,
                text=text,
                speaker=header.source,
                speaker_role=ROLE_BOT,
                said_at=header.covers_to,
                origin=ORIGIN_ATTACHMENT_NOTE,
            )


def attachment_owner(packet: Packet, body: AttachmentBody) -> dict[str, object] | None:
    """附件的说话人、时间这些跟着它属于的那句话（对话）、那封信（邮件）。解析时核过 of 指得到。"""
    section = next((item for item in packet.sections if item.number == body.of_section), None)
    if isinstance(section, MailSection):
        headers = section.headers
        return {
            "speaker": str(headers.sender),
            "speaker_role": ROLE_OTHER,
            "said_at": headers.date,
            "message_id": headers.message_id,
            "thread_id": headers.thread_id,
            "subject": headers.subject,
        }
    if isinstance(section, ChatSection):
        utterance = next((item for item in section.utterances if item.number == body.of_line), None)
        if utterance is not None:
            return {"speaker": utterance.speaker, "speaker_role": utterance.speaker_role, "said_at": utterance.said_at}
    return None


def _attachment_sentences(packet: Packet) -> Iterator[Sentence]:
    """附件的正文（packet-spec 第 3 节“附件小节”）。文件的内容算外部内容；section_no 是第几块附件，
    position 照样是 {section: "attachment", line, sentence}，取上下文时只在同一块里取。"""
    for body in packet.attachments:
        owner = attachment_owner(packet, body)
        if owner is None:
            continue
        for line in body.lines:
            stripped = line.text.strip()
            quoted = is_quoted(stripped)  # 和原话、邮件一样：去掉缩进以后 > 开头的行（query-api 5.5）
            for number, text in enumerate(split_sentences(stripped), 1):
                yield Sentence(
                    sentence_id=f"{packet.header.packet_id}#attachment.{line.number}.{number}",
                    section_kind=SECTION_ATTACHMENT,
                    section_no=body.number,
                    line_no=line.number,
                    sentence_no=number,
                    field=None,
                    text=text,
                    origin=ORIGIN_EXTERNAL,
                    quoted=quoted,
                    **owner,  # type: ignore[arg-type]
                )


def iter_sentences(packet: Packet) -> Iterator[Sentence]:
    """按包里的先后给出每一行。"""
    packet_id = packet.header.packet_id
    for section in packet.sections:
        if isinstance(section, ChatSection):
            yield from _chat_sentences(packet_id, section)
        else:
            yield from _mail_sentences(packet_id, section)
    yield from _note_sentences(packet, packet.header)
    yield from _attachment_sentences(packet)
