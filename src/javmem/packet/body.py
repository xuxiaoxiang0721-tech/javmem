"""解析包的内容（packet-spec 第 3、5、6 节）。

这里不看登记表，也不查时间范围，只把文本变成结构，顺手记下不拒收的警告。
唯一会拒收的是邮件头：某封邮件缺必填的邮件头字段算 bad_header。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from javmem.packet.errors import BAD_HEADER, Rejected
from javmem.packet.header import yaml_error_line
from javmem.packet.model import (
    BAD_ATTACHMENT,
    BAD_LINE,
    ODD_SPEAKER,
    ORIGIN_EXTERNAL,
    ORIGIN_RELAYED,
    ORIGIN_SAID,
    OWNER_SPEAKER,
    ROLE_BOT,
    ROLE_OTHER,
    ROLE_OWNER,
    SUSPECT_CONTINUATION,
    TIME_BACKWARDS,
    NONE_REASONS,
    TEXT_FULL,
    TEXT_NONE,
    TEXT_STATES,
    TEXT_TRUNCATED,
    TRUNCATED_REASONS,
    UNKNOWN_HEADING,
    UNRECOGNIZED_LINE,
    Address,
    AttachmentBody,
    AttachmentLine,
    AttachmentNote,
    ChatSection,
    MailHeaders,
    MailSection,
    PacketHeader,
    ParseWarning,
    TextLine,
    Utterance,
)
from javmem.timeutil import parse_aware
from javmem.yamlio import load_yaml

_HEADING = re.compile(r"##(?:\s.*)?")
_ATTACHMENT_HEADING = re.compile(r"###(?:\s.*)?")  # 附件小节里一块附件的开头
_UTTERANCE = re.compile(r"- \[([^\]]*)\]\s*(.*)")
_LOOKS_LIKE_UTTERANCE = re.compile(r"-\s*\[")
_CLOCK = re.compile(r"(?:(\d{4})-(\d{2})-(\d{2}) )?(\d{2}):(\d{2})", re.ASCII)
# 标记写得不规范也认：认多了只是当转述说，漏认了外部内容就成了本人的话
_MARKER = re.compile(r"\[(转发[^\]]*|外部[^\]]*|语音)\]\s*")
# 不像人名：斜杠和 http 是网址的冒号被当成了说话人的结尾。只提醒
_ODD_SPEAKER = re.compile(r"/|^https?$")
# D41：说话人里有方括号的，多半是漏写了说话人，行首的标记（[转发 …]、[外部:…]）被当成了说话人。
# 算解析不了的行：不并进上一句，不进原文索引，也不抽取，归档里还在
_BRACKET_SPEAKER = re.compile(r"[\[\]]")

_RAW = "raw"
_SUMMARY = "summary"
_ATTACHMENT = "attachment"
_UNKNOWN = "unknown"


@dataclass
class _Block:
    kind: str
    file_line: int
    lines: list[tuple[int, str]] = field(default_factory=list)  # (文件行号, 原样的一行)


def _classify_heading(line: str) -> str:
    title = line[2:].strip()
    if title.startswith("原话"):
        return _RAW
    if title.startswith("摘要"):
        return _SUMMARY
    if title.startswith("附件"):
        return _ATTACHMENT
    return _UNKNOWN


def _split_blocks(lines: list[str], first_line: int, warnings: list[ParseWarning]) -> list[_Block]:
    """按行首的二级标题切成小节。第一个标题之前的文字不属于任何小节。"""
    blocks: list[_Block] = []
    for offset, line in enumerate(lines):
        number = first_line + offset
        if _HEADING.fullmatch(line):
            kind = _classify_heading(line)
            if kind == _UNKNOWN:
                warnings.append(ParseWarning(UNKNOWN_HEADING, number))
            blocks.append(_Block(kind, number))
        elif blocks:
            blocks[-1].lines.append((number, line))
        elif line.strip():
            warnings.append(ParseWarning(UNRECOGNIZED_LINE, number))
    return blocks


def _parse_clock(text: str, header: PacketHeader) -> datetime | None:
    """只写 HH:MM 的，日期取 covers_to 那一天。时区都跟 covers_to。"""
    match = _CLOCK.fullmatch(text.strip())
    if match is None:
        return None
    year, month, day, hour, minute = match.groups()
    cover = header.covers_to
    try:
        if year is None:
            return datetime(cover.year, cover.month, cover.day, int(hour), int(minute), tzinfo=cover.tzinfo)
        return datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=cover.tzinfo)
    except ValueError:
        return None


def _split_speaker(rest: str) -> tuple[str, str] | None:
    """说话人到第一个冒号为止，全角半角都认。"""
    positions = [position for position in (rest.find("："), rest.find(":")) if position >= 0]
    if not positions:
        return None
    colon = min(positions)
    speaker = rest[:colon].strip()
    return (speaker, rest[colon + 1 :].strip()) if speaker else None


def speaker_role(speaker: str, source: str) -> str:
    if speaker == OWNER_SPEAKER:
        return ROLE_OWNER
    return ROLE_BOT if speaker == source else ROLE_OTHER


def origin_of(text: str) -> str:
    """行首的标记管一整句话。标记可以连着写，比如 [语音][转发 自 小李]。"""
    origin = ORIGIN_SAID
    while (match := _MARKER.match(text)) is not None:
        marker = match.group(1)
        if marker.startswith("外部"):
            return ORIGIN_EXTERNAL
        if marker.startswith("转发"):
            origin = ORIGIN_RELAYED
        text = text[match.end() :]
    return origin


def is_quoted(text: str) -> bool:
    return text.startswith("> ") or text == ">"


def _parse_utterance(line: str, file_line: int, number: int, header: PacketHeader) -> Utterance | None:
    match = _UTTERANCE.fullmatch(line)
    if match is None:
        return None
    said_at = _parse_clock(match.group(1), header)
    parts = _split_speaker(match.group(2))
    if said_at is None or parts is None:
        return None
    speaker, text = parts
    utterance = Utterance(number, file_line, said_at, speaker, speaker_role(speaker, header.source))
    if text:
        utterance.lines.append(TextLine(text, False, file_line))
    return utterance


def _parse_chat_block(block: _Block, header: PacketHeader, warnings: list[ParseWarning]) -> list[Utterance]:
    utterances: list[Utterance] = []
    current: Utterance | None = None
    for file_line, line in block.lines:
        if not line.strip():
            continue
        if _LOOKS_LIKE_UTTERANCE.match(line):
            parsed = _parse_utterance(line, file_line, len(utterances) + 1, header)
            if parsed is None or _BRACKET_SPEAKER.search(parsed.speaker):
                # 解析不了的行不并进上一句，它后面的续行也不并，不能让一句话算到别人头上
                warnings.append(ParseWarning(BAD_LINE, file_line))
                current = None
            else:
                if _ODD_SPEAKER.search(parsed.speaker):
                    warnings.append(ParseWarning(ODD_SPEAKER, file_line))
                utterances.append(parsed)
                current = parsed
            continue
        indented = line.startswith("  ")
        if current is None or not indented:
            warnings.append(ParseWarning(UNRECOGNIZED_LINE, file_line))
        elif _parse_utterance(line.strip(), file_line, 0, header) is not None:
            warnings.append(ParseWarning(SUSPECT_CONTINUATION, file_line))
        if current is not None:
            text = line.strip()
            current.lines.append(TextLine(text, is_quoted(text), file_line))

    for utterance in utterances:
        first_text = next((line.text for line in utterance.lines if line.text and not line.quoted), "")
        utterance.origin = origin_of(first_text)
    return utterances


def _parse_chat(blocks: list[_Block], header: PacketHeader, warnings: list[ParseWarning]) -> list[ChatSection]:
    sections: list[ChatSection] = []
    previous: datetime | None = None
    for block in blocks:
        if block.kind != _RAW:
            continue
        utterances = _parse_chat_block(block, header, warnings)
        for utterance in utterances:
            if previous is not None and utterance.said_at < previous:
                warnings.append(ParseWarning(TIME_BACKWARDS, utterance.file_line))
            previous = utterance.said_at
        # 一句合规的原话都没有，不算原话小节，也不占小节编号
        if any(utterance.has_text for utterance in utterances):
            sections.append(ChatSection(len(sections) + 1, block.file_line, utterances))
    return sections


def _address(value: object) -> Address | None:
    if not isinstance(value, Mapping):
        return None
    addr, name = value.get("addr"), value.get("name")
    if not isinstance(addr, str) or not addr or not (name is None or isinstance(name, str)):
        return None
    return Address(name or None, addr)


def _check_mail_headers(fields: Mapping, file_line: int) -> MailHeaders:
    def fail(message: str) -> Rejected:
        return Rejected(BAD_HEADER, f"第 {file_line} 行那封邮件{message}")

    for name in ("message_id", "thread_id", "in_reply_to", "from", "to", "date", "subject"):
        if name not in fields:
            raise fail(f"缺邮件头字段 {name}")

    message_id, thread_id = fields["message_id"], fields["thread_id"]
    if not isinstance(message_id, str) or not message_id:
        raise fail("的 message_id 要写成文本")
    if not isinstance(thread_id, str) or not thread_id:
        raise fail("的 thread_id 要写成文本")

    in_reply_to = fields["in_reply_to"]
    if not (in_reply_to is None or isinstance(in_reply_to, str)):
        raise fail("的 in_reply_to 要写成文本或 null")

    references = fields.get("references", [])
    if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
        raise fail("的 references 要写成文本列表")

    sender = _address(fields["from"])
    if sender is None:
        raise fail("的 from 要写 name 和 addr")

    def address_list(name: str, value: object) -> tuple[Address, ...]:
        if not isinstance(value, list):
            raise fail(f"的 {name} 要写成列表")
        addresses = [_address(item) for item in value]
        if None in addresses:
            raise fail(f"的 {name} 要写 name 和 addr")
        return tuple(addresses)

    date = parse_aware(fields["date"])
    if date is None:
        raise fail("的 date 要写带时区的时间")

    subject = fields["subject"]
    if not isinstance(subject, str):
        raise fail("的 subject 要写成文本")

    attachments = fields.get("attachments", [])
    if not isinstance(attachments, list):
        raise fail("的 attachments 要写成列表")
    names = []
    for item in attachments:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise fail("的 attachments 要写 name")
        names.append(item["name"])

    inline_images = fields.get("inline_images", 0)
    if isinstance(inline_images, bool) or not isinstance(inline_images, int) or inline_images < 0:
        raise fail("的 inline_images 要写成不小于 0 的整数")

    return MailHeaders(
        message_id=message_id,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=tuple(references),
        sender=sender,
        to=address_list("to", fields["to"]),
        cc=address_list("cc", fields.get("cc", [])),
        bcc=address_list("bcc", fields.get("bcc", [])),
        date=date,
        subject=subject,
        attachments=tuple(names),
        inline_images=inline_images,
    )


def _parse_mail(block: _Block, number: int, warnings: list[ParseWarning]) -> MailSection:
    lines = block.lines
    index = 0
    while index < len(lines) and not lines[index][1].strip():
        index += 1
    if index == len(lines) or lines[index][1].rstrip() != "```yaml":
        raise Rejected(BAD_HEADER, f"第 {block.file_line} 行那封邮件没有邮件头")

    opening = index
    index += 1
    while index < len(lines) and lines[index][1].rstrip() != "```":
        index += 1
    if index == len(lines):
        raise Rejected(BAD_HEADER, f"第 {block.file_line} 行那封邮件的邮件头没有结束")

    first_yaml_line = lines[opening][0] + 1
    try:
        fields = load_yaml("\n".join(line for _, line in lines[opening + 1 : index]))
    except (yaml.YAMLError, RecursionError) as error:
        line_number = yaml_error_line(error, first_yaml_line)
        raise Rejected(BAD_HEADER, f"邮件头解析不了，第 {line_number} 行") from None
    if not isinstance(fields, Mapping):
        raise Rejected(BAD_HEADER, f"第 {block.file_line} 行那封邮件的邮件头写法不对")
    headers = _check_mail_headers(fields, block.file_line)

    index += 1
    if index < len(lines) and not lines[index][1].strip():  # 邮件头和正文之间空一行
        index += 1

    body: list[str] = []
    for file_line, line in lines[index:]:
        if not line.strip():
            body.append("")
        elif line.startswith("  "):
            body.append(line[2:])
        else:
            warnings.append(ParseWarning(UNRECOGNIZED_LINE, file_line))
            body.append(line)
    while body and not body[-1]:
        body.pop()
    return MailSection(number, block.file_line, headers, body)


@dataclass
class _AttachmentDraft:
    file_line: int  # ### 那一行
    lines: list[tuple[int, str]] = field(default_factory=list)


def _split_attachment_section(block: _Block) -> tuple[list[tuple[int, str]], list[_AttachmentDraft]]:
    """附件小节：开头是说明，行首的 ### 开始一块附件，到下一个 ### 或者小节结尾。"""
    notes: list[tuple[int, str]] = []
    drafts: list[_AttachmentDraft] = []
    for file_line, line in block.lines:
        if _ATTACHMENT_HEADING.fullmatch(line):
            drafts.append(_AttachmentDraft(file_line))
        elif drafts:
            drafts[-1].lines.append((file_line, line))
        else:
            notes.append((file_line, line))
    return notes, drafts


def _owner_of(of: object, channel: str, sections: list[ChatSection | MailSection]) -> tuple[int, int | None] | None:
    """of 指着的那句话（对话：{section, line}）或者那封信（邮件：{section}）。指不到返回 None。"""
    if not isinstance(of, Mapping) or not set(of) <= {"section", "line"}:
        return None
    section_no, line_no = of.get("section"), of.get("line")
    if type(section_no) is not int:  # True 也是 int
        return None
    section = next((item for item in sections if item.number == section_no), None)
    if section is None:
        return None
    if channel == "email":
        return (section_no, None) if "line" not in of else None
    if type(line_no) is not int or not isinstance(section, ChatSection):
        return None
    return (section_no, line_no) if any(item.number == line_no for item in section.utterances) else None


def _attachment_head(
    draft: _AttachmentDraft, channel: str, sections: list[ChatSection | MailSection]
) -> tuple[dict[str, object], int] | None:
    """一块附件的 yaml。写错了返回 None。返回 (字段, yaml 之后的下一行在 draft.lines 里的下标)。"""
    lines = draft.lines
    index = 0
    while index < len(lines) and not lines[index][1].strip():
        index += 1
    if index == len(lines) or lines[index][1].rstrip() != "```yaml":
        return None
    opening = index
    index += 1
    while index < len(lines) and lines[index][1].rstrip() != "```":
        index += 1
    if index == len(lines):
        return None
    try:
        fields = load_yaml("\n".join(line for _, line in lines[opening + 1 : index]))
    except (yaml.YAMLError, RecursionError):
        return None
    if not isinstance(fields, Mapping):
        return None

    name, kind, size, state, reason = (fields.get(key) for key in ("name", "type", "size", "text", "reason"))
    owner = _owner_of(fields.get("of"), channel, sections)
    if not (isinstance(name, str) and name.strip() and isinstance(kind, str) and kind.strip()) or owner is None:
        return None
    if size is not None and (type(size) is not int or size < 0):
        return None
    if state not in TEXT_STATES:
        return None
    # reason 要是规范第 3 节那张表里的，而且对得上：截断的原因写在没收的上面也算写错
    if (state == TEXT_TRUNCATED and reason not in TRUNCATED_REASONS) or (state == TEXT_NONE and reason not in NONE_REASONS):
        return None
    head = {
        "name": name,
        "type": kind,
        "size": size,
        "of": owner,
        "text": state,
        "reason": reason if state != TEXT_FULL else None,
    }
    return head, index + 1


def _parse_attachments(
    blocks: list[_Block], channel: str, sections: list[ChatSection | MailSection], warnings: list[ParseWarning]
) -> tuple[list[AttachmentNote], list[AttachmentBody]]:
    """附件小节：说明和附件的正文（packet-spec 第 3 节“附件小节”）。

    说明和正文的行连着编号（position.line），空行、### 那一行和 yaml 不算。写错了的附件块记一条
    bad_attachment，它的行不编号、不进原文索引，归档里还在。
    """
    notes: list[AttachmentNote] = []
    bodies: list[AttachmentBody] = []
    count = 0
    for block in blocks:
        if block.kind != _ATTACHMENT:
            continue
        note_lines, drafts = _split_attachment_section(block)
        for file_line, line in note_lines:
            text = line.strip()
            if text.startswith("- "):
                text = text[2:].strip()
            if text:
                count += 1
                notes.append(AttachmentNote(count, file_line, text))
        for draft in drafts:
            found = _attachment_head(draft, channel, sections)
            body_lines = draft.lines[found[1] :] if found is not None else []
            head = found[0] if found is not None else None
            if head is not None and head["text"] == TEXT_NONE and any(line.strip() for _, line in body_lines):
                head = None  # 写着没收正文，下面却有正文：说不清是哪样，不收
            if head is None:
                warnings.append(ParseWarning(BAD_ATTACHMENT, draft.file_line))
                continue
            section_no, line_no = head["of"]  # type: ignore[misc]
            body = AttachmentBody(
                number=len(bodies) + 1,
                file_line=draft.file_line,
                name=str(head["name"]),
                type=str(head["type"]),
                size=head["size"],  # type: ignore[arg-type]
                of_section=section_no,
                of_line=line_no,
                text=str(head["text"]),
                reason=head["reason"],  # type: ignore[arg-type]
            )
            for file_line, line in body_lines:
                if not line.strip():
                    continue
                if line.startswith("  "):
                    text = line[2:]
                else:  # 正文要缩进两格，和邮件一样；没缩进的照收，记一条
                    warnings.append(ParseWarning(UNRECOGNIZED_LINE, file_line))
                    text = line
                count += 1
                body.lines.append(AttachmentLine(count, file_line, text))
            bodies.append(body)
    return notes, bodies


def parse_body(
    header: PacketHeader, lines: list[str], first_line: int
) -> tuple[list[ChatSection | MailSection], list[AttachmentNote], list[AttachmentBody], list[ParseWarning]]:
    warnings: list[ParseWarning] = []
    blocks = _split_blocks(lines, first_line, warnings)
    sections: list[ChatSection | MailSection]
    if header.channel == "email":
        raw_blocks = [block for block in blocks if block.kind == _RAW]
        sections = [_parse_mail(block, number, warnings) for number, block in enumerate(raw_blocks, 1)]
    else:
        sections = list(_parse_chat(blocks, header, warnings))
    notes, attachments = _parse_attachments(blocks, header.channel, sections, warnings)
    warnings.sort(key=lambda warning: warning.line)
    return sections, notes, attachments, warnings
