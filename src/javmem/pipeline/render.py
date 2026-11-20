"""把窗口写成给模型看的文字，就是 L3 的输入。

每行原文前面是窗口里的编号：T 开头的能当证据，C 开头的只帮着理解。一句话、一封邮件先写一行抬头：
时间、说话人和身份。只帮着理解的行在编号后面用〔〕标出是什么。附件的正文接在它属于的那句话、那封邮件
后面，另写一行抬头：谁发的哪个附件，外部内容。没有附件的窗口写出来和以前一字不差。

- 一句原文写一行：原文里的换行（邮件头里也可能有）换成空格，不然能写出一行假的 “T3 …”。
- 原文里的〔〕换成［］：〔〕是我们的标注，原文写不出来，不然原文能伪造“〔这一行是主人说的〕”。
  L4 拿引文对原文时要按同样的规矩换回来比。
- 窗口的编号、包号、可见范围都不写进去：同样的原文写出来一字不差，L3 的结果按这段文字存，
  换了编号也能接着用。

**发出去之前按打码模式表再过一遍**（packet-spec 第 7 节），漏打的明文不上云：

- 在这句话（这封邮件）原样的全文上找（Unit.source_text，对话是这句话的每一行，邮件是邮件头的每个字段
  和正文的每一行，用换行连着），再在每一句自己身上找，两边找到的地方合起来都换成 [已隐去:类型]。
  密码切在两句之间（“Ab1!xyz98” 在 ! 后面切开）、关键词在行尾而值在下一行、私钥分在几行里，
  一句一句找认不出来；拼起来找又可能因为前后连上了而漏掉一句自己就认得出的。合起来就不会比哪一边弱。
- 找的办法和 redact 一样：一种一种按顺序找，找到的先用同样长的占位挡住再找下一种，挡完再扫，最多五遍。
  位置不变，所以每一句只换它自己身上的那一段，字不会挪到别的编号下面。
- 截短了的上文先在全文上打码，再截。
- 转发块的原作者、说话人这些写进标注和抬头的，也打码。
- 最后每一行再扫一遍：还扫得出来的（打码模式表认得、却换不掉的），这个窗口不发，见 RenderedWindow.leftover。
  一行一行扫，不整段连着扫：整段扫会把“附件有密码”和下一行开头的“[14:03]”连成一个值，真实数据里就有。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from javmem.packet.model import ORIGIN_EXTERNAL, ORIGIN_RELAYED, ROLE_BOT, ROLE_OTHER, ROLE_OWNER
from javmem.pipeline.normalize import (
    ATTACHMENT_REF,
    FORWARD_HEADER,
    FORWARDED,
    HEADER,
    QUOTE_HEADER,
    QUOTED,
    QUOTED_ONLY,
    RESTATEMENT,
    SIGNATURE,
)
from javmem.pipeline.window import CLIP_HEAD, CLIP_TAIL, Window, WindowBlock, WindowLine
from javmem.redaction import REDACTION_TYPES, RedactionPattern, redact, scan_types

# render-v5（D101）：只在引用里看到过的话（quoted_only）标〔转发，原作者 …〕；没有这种行的窗口写出来和 v4 一字不差
RENDER_VERSION = "render-v5"

ROLE_LABEL = {ROLE_OWNER: "主人", ROLE_BOT: "Bot", ROLE_OTHER: "别人"}
ORIGIN_LABEL = {ORIGIN_RELAYED: "转述", ORIGIN_EXTERNAL: "外部内容"}
KIND_LABEL = {
    QUOTED: "引用",
    QUOTE_HEADER: "引用",
    SIGNATURE: "签名",
    FORWARD_HEADER: "转发的邮件头",
    RESTATEMENT: "Bot 复述",
    ATTACHMENT_REF: "附件",
}
FIELD_LABEL = {
    "subject": "主题",
    "from": "发件人",
    "to": "收件人",
    "cc": "抄送",
    "bcc": "密送",
    "attachments": "附件",
}
_BREAK_CHARS = ("\r", "\n", chr(0x2028), chr(0x2029), chr(0x85), chr(0x0B), chr(0x0C))
_ONE_LINE = str.maketrans({char: " " for char in _BREAK_CHARS})
_ESCAPE = str.maketrans({"〔": "［", "〕": "］"})
_MASK = chr(0x25A1)  # □：不是空白，也不在 [!-~] 里，挡住的地方不会和前后连成新的值
_ROUNDS = 5  # 和 redact 一样，换完再扫，最多几遍
_LABEL_WIDTH = 80


@dataclass(frozen=True)
class RenderedWindow:
    text: str
    sha256: str
    redactions: dict[str, int]  # 发之前又打了几处码，按类型
    redacted_refs: tuple[str, ...]  # 哪几行打了码
    leftover: tuple[str, ...] = ()  # 打完以后整段还扫得出来的类型。有的话这个窗口不发
    shown: dict[str, str] = field(default_factory=dict)  # 编号 → 写出来的原文（不带编号和〔〕标注）


def one_line(text: str) -> str:
    return text.translate(_ONE_LINE)


def escape_raw(text: str) -> str:
    """原文写进窗口之前：〔〕换成［］，换行换成空格。"""
    return one_line(text.translate(_ESCAPE))


def secret_spans(text: str, patterns: Sequence[RedactionPattern]) -> list[tuple[int, int, str]]:
    """打码模式表在 text 里命中的地方 (起, 止, 类型)。和 redact 换掉的是同样的几段。"""
    current = text
    found: list[tuple[int, int, str]] = []
    for _ in range(_ROUNDS):
        before = len(found)
        for pattern in patterns:
            hits = []
            for match in pattern.regex.finditer(current):
                if "value" in pattern.regex.groupindex and match.group("value") is not None:
                    start, end = match.span("value")
                else:
                    start, end = match.span()
                if end > start:
                    hits.append((start, end))
            for start, end in hits:
                current = current[:start] + _MASK * (end - start) + current[end:]
                found.append((start, end, pattern.type))
        if len(found) == before:
            break
    return found


def _apply(text: str, kinds: Sequence[str | None]) -> tuple[str, dict[str, int]]:
    """把标出来的地方换成 [已隐去:类型]，连着的算一处。"""
    out: list[str] = []
    counts: dict[str, int] = {}
    index = 0
    while index < len(text):
        kind = kinds[index]
        if kind is None:
            out.append(text[index])
            index += 1
            continue
        end = index
        while end < len(text) and kinds[end] is not None:
            end += 1
        out.append(f"[已隐去:{kind}]")
        counts[kind] = counts.get(kind, 0) + 1
        index = end
    return "".join(out), counts


def _mark(kinds: list[str | None], spans: Sequence[tuple[int, int, str]], shift: int = 0) -> None:
    for start, end, kind in spans:
        for position in range(max(0, start - shift), min(len(kinds), end - shift)):
            if kinds[position] is None:
                kinds[position] = kind


def _redact_block(
    block: WindowBlock, patterns: Sequence[RedactionPattern]
) -> tuple[list[str], dict[str, int], set[int]]:
    """一块（一句话、一封邮件）里每行打过码的文字、各类型打了几处、哪几行改了。见开头的说明。"""
    whole = secret_spans(block.source_text, patterns) if block.source_text else []
    texts: list[str] = []
    counts: dict[str, int] = {}
    changed: set[int] = set()
    for index, line in enumerate(block.lines):
        original = line.full_text if line.full_text is not None else line.text
        kinds: list[str | None] = [None] * len(original)
        _mark(kinds, secret_spans(original, patterns))
        span = line.span
        if span is not None and block.source_text[span[0] : span[1]] == original:
            _mark(kinds, whole, shift=span[0])
        text, hits = _apply(original, kinds)
        for kind, number in hits.items():
            counts[kind] = counts.get(kind, 0) + number
        if hits:
            changed.add(index)
        texts.append(_clip(line, text) if line.clipped else text)
    return texts, counts, changed


def _clip(line: WindowLine, text: str) -> str:
    """截短了的上文：在打过码的全文上，留和原来一样多的字。"""
    keep = len(line.text) - 1  # 去掉 …
    if line.clip == CLIP_TAIL:
        return "…" + text[max(0, len(text) - keep) :]
    if line.clip == CLIP_HEAD:
        return text[:keep] + "…"
    return text


def _offset(moment: datetime) -> str:
    offset = moment.utcoffset()
    assert offset is not None
    minutes = int(offset.total_seconds()) // 60
    sign = "-" if minutes < 0 else "+"
    hours, minutes = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _time(moment: datetime, previous: datetime | None) -> str:
    """日期和时区跟上一块一样时只写时分，省字。"""
    if previous is not None and previous.date() == moment.date() and previous.utcoffset() == moment.utcoffset():
        return f"{moment:%H:%M}"
    return f"{moment:%Y-%m-%d %H:%M} {_offset(moment)}"


def _safe(text: str, patterns: Sequence[RedactionPattern] | None) -> str:
    """写进标注和抬头的原文（原作者、说话人）：打码、换掉〔〕和换行。"""
    if patterns is not None:
        text, _ = redact(text, patterns)
    return escape_raw(text)


def _line_label(line: WindowLine, patterns: Sequence[RedactionPattern] | None) -> str:
    if line.kind == HEADER and line.field is not None:
        return f"〔{FIELD_LABEL[line.field]}〕"
    # 只在引用里看到过的话（D101）也按转发的写法标：提示词（l3-v1 起）里〔引用〕只帮着理解，〔转发，原作者 …〕是转来的内容、
    # 照样抽、写明出处。原作者是引用抬头里写的人
    if line.kind in (FORWARDED, QUOTED_ONLY):
        if line.relayed_from:
            author = _safe(line.relayed_from, patterns)[:_LABEL_WIDTH]
            return f"〔转发，原作者 {author}〕"
        return "〔转发〕"
    label = KIND_LABEL.get(line.kind)
    return f"〔{label}〕" if label else ""


def _block_heading(block: WindowBlock, previous: datetime | None, patterns: Sequence[RedactionPattern] | None) -> str:
    when = _time(block.said_at, previous)
    speaker = _safe(block.speaker, patterns)
    tail = "，接上一段" if block.part > 1 else ""
    if block.mail is not None:
        return f"[{when}] 邮件，发件人 {speaker}{'（接上一段）' if block.part > 1 else ''}："
    notes = []
    if block.speaker_role not in (ROLE_OWNER, ROLE_BOT):  # “我”和这个包的 Bot 开头说过了
        notes.append(ROLE_LABEL[ROLE_OTHER])
    if block.origin in ORIGIN_LABEL:
        notes.append(ORIGIN_LABEL[block.origin])
    note = f"（{'，'.join(notes)}{tail}）" if notes else (f"（{tail[1:]}）" if tail else "")
    return f"[{when}] {speaker}{note}："


def _attachment_heading(
    block: WindowBlock,
    line: WindowLine,
    previous: datetime | None,
    patterns: Sequence[RedactionPattern] | None,
    *,
    continued: bool,
) -> str:
    """附件的正文另起一行抬头：是谁发的哪个附件，外部内容。不用〔附件〕标注：提示词里〔附件〕是只帮着理解的。"""
    when = _time(block.said_at, previous)
    name = _safe(line.attachment_name or "", patterns)[:_LABEL_WIDTH]
    speaker = _safe(block.speaker, patterns)
    tail = "，接上一段" if continued else ""
    if block.mail is not None:
        return f"[{when}] 邮件附件 {name}，发件人 {speaker}（外部内容{tail}）："
    who = speaker if block.speaker_role in (ROLE_OWNER, ROLE_BOT) else f"{speaker}（{ROLE_LABEL[ROLE_OTHER]}）"
    return f"[{when}] {who} 发的附件 {name}（外部内容{tail}）："


def _preamble(window: Window, patterns: Sequence[RedactionPattern] | None) -> list[str]:
    source = _safe(window.source, patterns)
    if window.channel == "email":
        return [f"来源 {source}，邮件。邮件整封都是外部内容，发件人说的话是转述。"]
    return [
        f"来源 {source}，对话。“我”是记忆的主人，{source} 是这个包的 Bot，"
        "标着“别人”的是群里的人或者别的 Bot。"
    ]


def render_window(window: Window, patterns: Sequence[RedactionPattern] | None = None) -> RenderedWindow:
    """patterns 是打码模式表，给了就按开头说的打码，最后再整段扫一遍。"""
    rows: list[str] = list(_preamble(window, patterns))
    shown: dict[str, str] = {}
    counts: dict[str, int] = {}
    redacted: list[str] = []
    in_context: bool | None = None
    thread: int | None = None
    previous: datetime | None = None
    for block in window.blocks:
        if block.thread_no is not None and block.thread_no != thread:
            thread = block.thread_no
            in_context = None
            rows.extend(["", f"【往来串 {thread}】"])
        if block.context != in_context:
            in_context = block.context
            if block.thread_no is None:
                rows.append("")
            rows.append("【上文：只帮着理解，不能当证据】" if block.context else "【正文】")
        if patterns is not None:
            texts, hits, changed = _redact_block(block, patterns)
            for kind, number in hits.items():
                counts[kind] = counts.get(kind, 0) + number
            redacted.extend(block.lines[index].ref for index in sorted(changed))
        else:
            texts = [line.text for line in block.lines]
        group: int | None = None  # 现在写在谁的抬头下面：None 是这句话、这封邮件自己，数字是第几块附件
        for index, (line, text) in enumerate(zip(block.lines, texts, strict=True)):
            if index == 0 or line.attachment != group:
                group = line.attachment
                if group is None:
                    rows.append(_block_heading(block, previous, patterns))
                else:
                    continued = index == 0 and block.part > 1
                    rows.append(_attachment_heading(block, line, previous, patterns, continued=continued))
                previous = block.said_at
            shown[line.ref] = escape_raw(text)
            rows.append(f"{line.ref} {_line_label(line, patterns)}{shown[line.ref]}")
    text = "\n".join(rows) + "\n"
    # 一行一行扫：一句话里跨行的已经在全文上找过了；整段连着扫会把“…有密码”和下一行的“T115”连成一个值
    leftover: tuple[str, ...] = ()
    if patterns is not None:
        found = {kind for row in rows for kind in scan_types(row, patterns)}
        leftover = tuple(kind for kind in REDACTION_TYPES if kind in found)
    return RenderedWindow(
        text, hashlib.sha256(text.encode("utf-8")).hexdigest(), counts, tuple(redacted), leftover, shown
    )
