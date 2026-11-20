"""包 → 窗口 → Graphiti 的 episode。不 import Graphiti。

- 和主线同一套：L1（normalize，撤回了的句子不进来）、L2（build_windows，上文池照主线给）、打码（render_window 加打码模式表）。
  窗口一样，打分、比对才能一句一句对上。
- 不带上文：上文那几块（C 行）去掉再写，Graphiti 自己拿同一组的前几个窗口当上文。没有上文了，“【正文】”那一行也去掉。
- 不加预处理 A 的〔日子〕、B 的〔指〕：那是主线的预处理。
- 文字照 render_window 写出来的样子，只去掉每行开头的编号（T3、C2 这种，Graphiti 会把编号当东西抽）。哪一行是哪句原文
  记在 Episode.lines 里，对证据用；正文里只帮着理解的行（C 开头的：引用、Bot 复述这些）照样写给 Graphiti，可不当证据。
- 不进支线的窗口整个跳过：private 的；打完码还扫得出来的（leftover）；写出来超过 6 万字的（照 L3）；邮件的（mail：
  邮件包是员工的工作邮件，现在的说明是照“Bot 和主人的对话”写的，用不上，要另写了再跑）。这些窗口不发 Qwen，
  文字不写进 Episode（text 和每行的文字都是空的），体检库、支线的库都不存，只记句子编号（导出时算没跑完，单元不打分）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from javmem.config import Settings
from javmem.pipeline.batch import load_queued
from javmem.pipeline.extract import ExtractOptions
from javmem.pipeline.normalize import Scope
from javmem.pipeline.render import render_window
from javmem.pipeline.window import Window, build_windows
from javmem.redaction import RedactionPattern, load_patterns

MAX_CHARS = ExtractOptions.max_input_chars  # 和 L3 一样：写出来超过这么多字的不发
SKIP_PRIVATE = "private"
SKIP_LEFTOVER = "leftover"
SKIP_TOO_BIG = "too_big"
SKIP_MIXED = "mixed"  # 一个窗口里有两个包的正文（对话不会有）
SKIP_MAIL = "mail"  # 邮件窗口支线先不跑
SKIP_EMPTY = "empty"
_GROUP_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_REF = re.compile(r"^([TC]\d+) ")
_BODY_MARK = "【正文】"


@dataclass(frozen=True)
class EpisodeLine:
    ref: str  # 窗口里的编号，T3 这种
    sentence_id: str  # 原文的句子编号：包号#小节.行.句
    text: str  # 写进窗口的原文（打过码、换过〔〕和换行）
    block: int  # 窗口里的第几块（一句话、一封邮件、一块附件），对证据时一块当一个候选


@dataclass(frozen=True)
class Episode:
    window_id: str
    packet_id: str
    revision: int
    number: int  # 这个包的第几个窗口，从 1 起
    scope: Scope
    reference_time: datetime  # 窗口第一句话说的时间（UTC）
    text: str = ""  # 给 Graphiti 的文字；跳过的窗口是空的
    sha256: str = ""
    lines: tuple[EpisodeLine, ...] = ()
    skip: str | None = None  # 不发的原因；None 是要发

    @property
    def group_id(self) -> str:
        return group_id_of(self.packet_id, self.revision)


def group_id_of(packet_id: str, revision: int) -> str:
    """体检库里的组名：包号加版本。Graphiti 只认字母、数字、- 和 _；包号里有别的字的，用指纹。"""
    plain = f"{packet_id}_r{revision}"
    if _GROUP_ID.match(plain):
        return plain
    return "p_" + hashlib.sha256(f"{packet_id}\n{revision}".encode()).hexdigest()[:32]


def _body_blocks(window: Window) -> Window:
    return dataclasses.replace(window, blocks=tuple(block for block in window.blocks if not block.context))


def episode_of(window: Window, number: int, patterns: Sequence[RedactionPattern] | None) -> Episode:
    """一个窗口写成 episode（纯函数，测试直接用）。"""
    body = _body_blocks(window)
    lines = [line for block in body.blocks for line in block.lines]
    packets = {(line.packet_id, line.revision) for line in lines}
    if not lines:
        packet_id, revision = window.window_id.split("#", 1)[0], 0
        return Episode(window.window_id, packet_id, revision, number, window.scope, datetime.now(UTC), skip=SKIP_EMPTY)
    packet_id, revision = lines[0].packet_id, lines[0].revision
    reference = body.blocks[0].said_at.astimezone(UTC)
    ids_only = tuple(  # 跳过的窗口只留句子编号
        EpisodeLine(line.ref, line.sentence_id, "", block_no) for block_no, block in enumerate(body.blocks) for line in block.lines
    )
    base = Episode(window.window_id, packet_id, revision, number, window.scope, reference)
    if len(packets) > 1:
        return dataclasses.replace(base, skip=SKIP_MIXED, lines=ids_only)
    if window.scope.private:  # 不写、不扫，文字一个字都不留
        return dataclasses.replace(base, skip=SKIP_PRIVATE, lines=ids_only)
    if window.channel == "email":
        return dataclasses.replace(base, skip=SKIP_MAIL, lines=ids_only)
    rendered = render_window(body, patterns)
    if rendered.leftover:
        return dataclasses.replace(base, skip=SKIP_LEFTOVER, lines=ids_only)
    refs = {line.ref for line in lines}
    rows = []
    for row in rendered.text.split("\n"):
        if row == _BODY_MARK:
            continue
        found = _REF.match(row)
        if found and found.group(1) in refs:
            row = row[found.end():]
        rows.append(row)
    text = "\n".join(rows).strip("\n") + "\n"
    if len(text) > MAX_CHARS:
        return dataclasses.replace(base, skip=SKIP_TOO_BIG, lines=ids_only)
    out_lines = tuple(
        EpisodeLine(line.ref, line.sentence_id, rendered.shown.get(line.ref, ""), block_no)
        for block_no, block in enumerate(body.blocks)
        for line in block.lines
    )
    return dataclasses.replace(
        base, text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), lines=out_lines
    )


def episodes_of(windows: Sequence[Window], patterns: Sequence[RedactionPattern] | None) -> list[Episode]:
    """一批窗口写成 episode，每个包里按窗口的先后编号。"""
    counters: dict[str, int] = {}
    out = []
    for window in windows:
        body = _body_blocks(window)
        first = next((line for block in body.blocks for line in block.lines), None)
        key = first.packet_id if first is not None else window.window_id
        counters[key] = counters.get(key, 0) + 1
        out.append(episode_of(window, counters[key], patterns))
    return out


def load_episodes(
    settings: Settings, library: str, packet_ids: Collection[str]
) -> tuple[list[Episode], list[tuple[str, str]]]:
    """点名的几个包现在的样子（撤回了的包、句子不进来），切好窗口写成 episode。第二个值是没用上的包和原因。
    账本只读打开（load_queued），主线的结果一张都不读。"""
    batch = load_queued(settings, library, packet_ids=list(packet_ids), any_state=True)
    patterns = load_patterns(settings.redaction_file)
    windows = build_windows(batch.packets, context_pool=batch.context)
    wanted = set(packet_ids)
    episodes = [one for one in episodes_of(windows, patterns) if one.packet_id in wanted]
    return episodes, list(batch.problems)
