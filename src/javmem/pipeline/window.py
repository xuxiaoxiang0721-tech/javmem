"""L2 切窗：把 L1 的结果切成窗口，L3 一个窗口调一次模型。

规矩：

- **可见范围不同的不进同一个窗口（D30）。** 范围是包的分区加敏感标记，带上文、往来串都一样。
  往来串里后来的回信多了敏感标记（D49）的，按范围分开，各成各的窗口。
- **一个包号只算一份：** 这一批里同一个包号有几份的，只用 revision 最大的；以前处理过的包
  （context_pool）里有这一批的包号的，不用它当上文：旧版本里删掉、挪走的内容不能再带进来。
- **对话：** 一个包里按顺序装，装满 max_chars 字就开下一个窗口。时间写在每句话前面，
  所以隔了半小时的两段也可以在一个窗口里。一句话自己超过 max_chars 才在句子之间拆开。
- **上文：** 每个窗口带上它前面的几句话，只帮着理解“他”“那批”这类指代，不能当证据，
  L3 也不从它抽。包是拆开的（continues 指着上一包，来源和范围都一样，上一包也确实在前面）时，
  第一个窗口的上文从上一包的末尾取。对话的上文留每句话后面的一段；邮件的上文只取邮件头和
  新写的正文，从前面留，引用的旧信和签名不要。
- **邮件：** 一个往来串放进同一个窗口，按 date 排，往来串可以跨包。小的往来串几个装一个窗口；
  一个往来串自己就超了（字数或者封数）才按封拆开，后面的窗口带前面几封当上文。以前处理过的包里
  同一往来串的信，当上文放在前面。
- **引用的旧话：** 窗口里前面已经有它的原文，就不再放。
- **附件的正文**是 L1 接在它属于的那句话、那封信后面的，跟着那个 Unit 走：同一个窗口，太长了一起按句子拆开，
  往来串里也跟着那封信。附件的正文不当上文。
- 只有上文、没有一句能当证据的，不出窗口。
- 每一行都带原文句子编号，窗口是这些编号的一张表，原文不动。上文截短了的行记着全文，
  写给模型之前先打码再截，不然截掉关键词以后，后面的值就认不出来了。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import chain

from javmem.pipeline.normalize import (
    FORWARDED,
    HEADER,
    NEW,
    QUOTED,
    Line,
    MailInfo,
    NormalizedPacket,
    Scope,
    Unit,
    normal_text,
    quote_body,
)

L2_VERSION = "l2-v4"

CLIP_TAIL = "tail"  # 只留了后面一段，前面加 …
CLIP_HEAD = "head"  # 只留了前面一段，后面加 …


@dataclass(frozen=True)
class WindowConfig:
    max_chars: int = 6000  # 一个窗口里，上文之外的原文最多多少字
    max_units: int = 80  # 最多几句话（邮件是几封）
    context_chars: int = 1500  # 带的上文最多多少字
    context_units: int = 6  # 上文最多几句话
    context_unit_chars: int = 600  # 上文里的一句话最多留多少字
    context_max_gap: timedelta = timedelta(hours=24)  # 上文最早到窗口开头之前多久


DEFAULT_CONFIG = WindowConfig()


@dataclass(frozen=True)
class WindowLine:
    """窗口里的一行，就是一句原文句。ref 是窗口里的编号：T 开头的能当证据，C 开头的是上文。"""

    ref: str
    sentence_id: str
    packet_id: str
    revision: int
    text: str  # 原文逐字；截短了的上文是截剩下的那段，加了 …
    kind: str
    origin: str
    quoted: bool
    field: str | None = None
    relayed_from: str | None = None
    restates: tuple[str, ...] = ()
    clip: str = ""  # 截短了的上文：CLIP_TAIL 或 CLIP_HEAD
    full_text: str | None = None  # 截短了的上文的全文
    span: tuple[int, int] | None = None  # 在 WindowBlock.source_text 里的位置，打码用
    attachment: int | None = None  # 附件的正文：第几块附件
    attachment_name: str | None = None

    @property
    def target(self) -> bool:
        return self.ref.startswith("T")

    @property
    def clipped(self) -> bool:
        return bool(self.clip)


@dataclass(frozen=True)
class WindowBlock:
    """窗口里的一块：对话的一句话，或者一封邮件。"""

    unit_id: str
    packet_id: str
    speaker: str
    speaker_role: str
    said_at: datetime
    origin: str
    context: bool  # 整块是窗口前面带的上文
    lines: tuple[WindowLine, ...]
    mail: MailInfo | None = None
    thread_no: int | None = None  # 邮件：窗口里第几个往来串
    part: int = 0
    source_text: str = ""  # 这句话、这封邮件原样的全文（Unit.source_text），打码用，不写给模型


@dataclass(frozen=True)
class Window:
    window_id: str
    source: str
    channel: str
    scope: Scope
    blocks: tuple[WindowBlock, ...]

    @property
    def lines(self) -> list[WindowLine]:
        return [line for block in self.blocks for line in block.lines]

    @property
    def targets(self) -> list[WindowLine]:
        return [line for line in self.lines if line.target]

    @property
    def packets(self) -> list[tuple[str, int]]:
        """窗口里出现的包和版本，上文的也算。"""
        return sorted({(line.packet_id, line.revision) for line in self.lines})

    @property
    def target_packets(self) -> list[str]:
        """有能当证据的句子的包。"""
        return sorted({line.packet_id for line in self.targets})

    def line(self, ref: str) -> WindowLine | None:
        return next((line for line in self.lines if line.ref == ref), None)

    def stats(self) -> dict[str, object]:
        targets = self.targets
        context = [line for line in self.lines if not line.target]
        return {
            "window_id": self.window_id,
            "source": self.source,
            "channel": self.channel,
            "packets": [packet_id for packet_id, _ in self.packets],
            "target_sentences": len(targets),
            "context_sentences": len(context),
            "target_chars": sum(len(line.text) for line in targets),
            "context_chars": sum(len(line.text) for line in context),
        }


def _chars(lines: Iterable[Line]) -> int:
    return sum(len(line.text) for line in lines)


def _pieces(units: Iterable[Unit], config: WindowConfig) -> list[Unit]:
    """去掉不进窗口的句子；一句话（一封邮件）超过 max_chars 的，在句子之间拆成几段。"""
    pieces: list[Unit] = []
    for unit in units:
        lines = tuple(line for line in unit.lines if not line.dropped)
        if not lines:
            continue
        kept = replace(unit, lines=lines)
        if _chars(lines) <= config.max_chars:
            pieces.append(kept)
            continue
        part, current, size = 1, [], 0
        for line in lines:
            if current and size + len(line.text) > config.max_chars:
                pieces.append(replace(kept, lines=tuple(current), part=part))
                part, current, size = part + 1, [], 0
            current.append(line)
            size += len(line.text)
        pieces.append(replace(kept, lines=tuple(current), part=part))
    return pieces


def _pack(units: Sequence[Unit], config: WindowConfig) -> list[tuple[int, int]]:
    """按顺序装，装满就开下一个。返回每个窗口的 [起, 止)。"""
    groups = []
    start, size = 0, 0
    for index, unit in enumerate(units):
        chars = _chars(unit.lines)
        if index > start and (size + chars > config.max_chars or index - start >= config.max_units):
            groups.append((start, index))
            start, size = index, 0
        size += chars
    if start < len(units):
        groups.append((start, len(units)))
    return groups


@dataclass(frozen=True)
class _Shown:
    line: Line
    text: str  # 窗口里写出来的：截短了的是截剩下的
    clip: str


def _keep_tail(lines: Sequence[Line], room: int) -> list[_Shown]:
    """对话的上文：留后面的。"""
    kept: list[_Shown] = []
    size = 0
    for line in reversed(lines):
        left = room - size
        if left <= 0:
            break
        if len(line.text) > left:
            if not kept:  # 最后一句就太长了，留它后面的一段
                kept.insert(0, _Shown(line, "…" + line.text[len(line.text) - left :], CLIP_TAIL))
            break
        kept.insert(0, _Shown(line, line.text, ""))
        size += len(line.text)
    return kept


def _keep_head(lines: Sequence[Line], room: int) -> list[_Shown]:
    """邮件的上文：只要主题、发件人和新写的正文，从前面留。收件人、抄送一长串会把正文挤掉。"""
    kept: list[_Shown] = []
    size = 0
    for line in lines:
        if line.kind not in (HEADER, NEW, FORWARDED) or (line.kind == HEADER and line.field not in ("subject", "from")):
            continue
        left = room - size
        if left <= 0:
            break
        if len(line.text) > left:
            kept.append(_Shown(line, line.text[:left] + "…", CLIP_HEAD))
            break
        kept.append(_Shown(line, line.text, ""))
        size += len(line.text)
    return kept


def _context(
    before: Sequence[Unit], end: int, history: Sequence[Unit], start: datetime, config: WindowConfig
) -> list[tuple[Unit, list[_Shown]]]:
    """窗口前面带的上文：before[:end] 从后往前取，不够再取 history（上一包）的末尾。"""
    chosen: list[tuple[Unit, list[_Shown]]] = []
    total = 0
    candidates = chain((before[index] for index in range(end - 1, -1, -1)), reversed(history))  # 现取，不复制
    for unit in candidates:
        if start - unit.said_at > config.context_max_gap:
            break
        keep = _keep_head if unit.mail is not None else _keep_tail
        # 附件的正文不当上文：上文是帮着认“他”“那批”的，附件末尾的一段只占地方。它属于的那句话照样当上文
        kept = keep([line for line in unit.lines if line.attachment is None], config.context_unit_chars)
        if not kept:
            continue
        size = sum(len(shown.text) for shown in kept)
        if chosen and total + size > config.context_chars:
            break
        chosen.insert(0, (unit, kept))
        total += size
        if len(chosen) >= config.context_units:
            break
    return chosen


class _Assembler:
    """一个窗口一个：按顺序编号，记住前面放过的原文，引用的旧话已经有了就不再放。"""

    def __init__(self) -> None:
        self.targets = 0
        self.contexts = 0
        self.seen = ""
        self.blocks: list[WindowBlock] = []

    def add(self, unit: Unit, shown: Iterable[_Shown], *, context: bool, thread_no: int | None = None) -> None:
        kept = []
        for item in shown:
            line = item.line
            if line.kind == QUOTED:
                body = quote_body(line.text)
                if len(body) < 2 or body in self.seen:
                    continue
            if context or not line.target:
                self.contexts += 1
                ref = f"C{self.contexts}"
            else:
                self.targets += 1
                ref = f"T{self.targets}"
            self.seen += "\n" + normal_text(line.text)
            kept.append(
                WindowLine(
                    ref=ref,
                    sentence_id=line.sentence_id,
                    packet_id=unit.packet_id,
                    revision=unit.revision,
                    text=item.text,
                    kind=line.kind,
                    origin=line.origin,
                    quoted=line.quoted,
                    field=line.field,
                    relayed_from=line.relayed_from,
                    restates=line.restates,
                    clip=item.clip,
                    full_text=line.text if item.clip else None,
                    span=line.span,
                    attachment=line.attachment,
                    attachment_name=line.attachment_name,
                )
            )
        if kept:
            self.blocks.append(
                WindowBlock(
                    unit_id=unit.unit_id,
                    packet_id=unit.packet_id,
                    speaker=unit.speaker,
                    speaker_role=unit.speaker_role,
                    said_at=unit.said_at,
                    origin=unit.origin,
                    context=context,
                    lines=tuple(kept),
                    mail=unit.mail,
                    thread_no=thread_no,
                    part=unit.part,
                    source_text=unit.source_text,
                )
            )

    def add_unit(self, unit: Unit, *, thread_no: int | None = None) -> None:
        self.add(unit, (_Shown(line, line.text, "") for line in unit.lines), context=False, thread_no=thread_no)

    def add_context(self, chosen: Sequence[tuple[Unit, list[_Shown]]], thread_no: int | None = None) -> None:
        for unit, shown in chosen:
            self.add(unit, shown, context=True, thread_no=thread_no)

    def window(self, window_id: str, source: str, channel: str, scope: Scope) -> Window | None:
        if not self.targets:
            return None
        return Window(window_id, source, channel, scope, tuple(self.blocks))


class _Numbering:
    """窗口编号：第一句能当证据的话在哪个包，就叫 包号#w第几个。"""

    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)

    def next(self, packet_id: str) -> str:
        self.counts[packet_id] += 1
        return f"{packet_id}#w{self.counts[packet_id]}"


def _chat_windows(
    packet: NormalizedPacket, previous: NormalizedPacket | None, config: WindowConfig, number: _Numbering
) -> list[Window]:
    pieces = _pieces(packet.units, config)
    history = _pieces(previous.units, config) if previous is not None else []
    windows = []
    for start, end in _pack(pieces, config):
        group = pieces[start:end]
        assembler = _Assembler()
        assembler.add_context(_context(pieces, start, history, group[0].said_at, config))
        for unit in group:
            assembler.add_unit(unit)
        if assembler.targets:  # 只有上文的不出窗口，也不占编号
            window = assembler.window(number.next(packet.packet_id), packet.header.source, "chat", packet.scope)
            if window is not None:
                windows.append(window)
    return windows


def _thread_key(order: dict[str, int], unit: Unit) -> tuple[float, int, str, int, int, str]:
    message = unit.mail.message_id if unit.mail is not None else ""
    return (
        unit.said_at.timestamp(),
        order.get(unit.packet_id, len(order)),
        unit.packet_id,
        unit.section_no,
        unit.part,
        message,
    )


def _threads(units: Iterable[Unit], order: dict[str, int]) -> dict[str, list[Unit]]:
    threads: dict[str, list[Unit]] = defaultdict(list)
    for unit in units:
        assert unit.mail is not None
        threads[unit.mail.thread_id].append(unit)
    for thread in threads.values():
        thread.sort(key=lambda unit: _thread_key(order, unit))
    return threads


def _first_target_packet(units: Iterable[Unit]) -> str:
    return next(unit.packet_id for unit in units if any(line.target for line in unit.lines))


def _mail_windows(
    packets: Sequence[NormalizedPacket],
    pool: Sequence[NormalizedPacket],
    config: WindowConfig,
    number: _Numbering,
) -> list[Window]:
    order = {packet.packet_id: index for index, packet in enumerate(packets)}
    groups: dict[tuple[str, Scope], list[Unit]] = defaultdict(list)
    for packet in packets:
        groups[(packet.header.source, packet.scope)].extend(_pieces(packet.units, config))
    earlier: dict[tuple[str, Scope], list[Unit]] = defaultdict(list)
    for packet in pool:
        earlier[(packet.header.source, packet.scope)].extend(_pieces(packet.units, config))

    windows: list[Window] = []
    for key in sorted(groups):
        source, scope = key
        threads = _threads(groups[key], order)
        before = _threads(earlier.get(key, []), {})
        ordered = sorted(threads.items(), key=lambda item: (_thread_key(order, item[1][0]), item[0]))

        batch: list[tuple[str, list[Unit]]] = []
        size = count = 0

        def emit(parts: Sequence[tuple[str, list[Unit], list[Unit]]]) -> None:
            """parts 是 (往来串, 这个窗口里它的信, 它前面的信)。"""
            assembler = _Assembler()
            for thread_no, (_, units, prior) in enumerate(parts, 1):
                if prior:
                    assembler.add_context(_context(prior, len(prior), (), units[0].said_at, config), thread_no)
                for unit in units:
                    assembler.add_unit(unit, thread_no=thread_no)
            if assembler.targets:
                first = _first_target_packet(unit for _, units, _ in parts for unit in units)
                window = assembler.window(number.next(first), source, "email", scope)
                if window is not None:
                    windows.append(window)

        def flush() -> None:
            nonlocal batch, size, count
            if batch:
                emit([(thread_id, units, before.get(thread_id, [])) for thread_id, units in batch])
            batch, size, count = [], 0, 0

        for thread_id, units in ordered:
            chars = sum(_chars(unit.lines) for unit in units)
            if chars > config.max_chars or len(units) > config.max_units:
                # 一个往来串自己就超了：按封拆开，后面的带前面的当上文
                flush()
                for start, end in _pack(units, config):
                    prior = [*before.get(thread_id, []), *units[:start]]
                    emit([(thread_id, units[start:end], prior)])
                continue
            if batch and (size + chars > config.max_chars or count + len(units) > config.max_units):
                flush()
            batch.append((thread_id, units))
            size += chars
            count += len(units)
        flush()
    return windows


def _latest(packets: Iterable[NormalizedPacket]) -> list[NormalizedPacket]:
    """同一个包号只留 revision 最大的一份，一样大的留先来的。"""
    best: dict[str, NormalizedPacket] = {}
    for packet in packets:
        kept = best.get(packet.packet_id)
        if kept is None or packet.header.revision > kept.header.revision:
            best[packet.packet_id] = packet
    return list(best.values())


def build_windows(
    packets: Sequence[NormalizedPacket],
    *,
    context_pool: Sequence[NormalizedPacket] = (),
    config: WindowConfig = DEFAULT_CONFIG,
) -> list[Window]:
    """L2。packets 是这一批要抽的包，context_pool 是以前处理过的包，只拿来当上文。

    同一批输入每次切出来一样，窗口编号也一样。
    """
    batch = _latest(packets)
    batch_ids = {packet.packet_id for packet in batch}
    pool = sorted(
        (packet for packet in _latest(context_pool) if packet.packet_id not in batch_ids),
        key=lambda packet: packet.packet_id,
    )
    ordered = sorted(batch, key=lambda packet: (packet.header.covers_from.timestamp(), packet.packet_id))
    known = {packet.packet_id: packet for packet in (*pool, *ordered)}
    number = _Numbering()
    windows: list[Window] = []
    for packet in ordered:
        if packet.header.channel == "email":
            continue
        previous = known.get(packet.header.continues) if packet.header.continues else None
        if previous is not None and (
            previous.packet_id == packet.packet_id
            or previous.scope != packet.scope
            or previous.header.source != packet.header.source
            or previous.header.channel != packet.header.channel
            or previous.header.covers_from >= packet.header.covers_from  # 上一包要在前面
        ):
            previous = None
        windows.extend(_chat_windows(packet, previous, config, number))
    mail = [packet for packet in ordered if packet.header.channel == "email"]
    mail_pool = [packet for packet in pool if packet.header.channel == "email"]
    windows.extend(_mail_windows(mail, mail_pool, config, number))
    return windows
