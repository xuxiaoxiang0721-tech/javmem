"""把结构写成记忆包的文本（packet-spec 第 3 到 6、8 节），转换器和导入工具共用。

这里只管写法：怎么转义、怎么缩进、怎么投递。写出来的包合不合规，由 validate_packet 说了算。
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from pathlib import Path

import yaml

from javmem.config import ConfigError
from javmem.packet.header import SCHEMA
from javmem.timeutil import iso
from javmem.yamlio import load_yaml

MAX_RAW_CHARS = 20_000  # 规范第 2 节：原话超过 2 万字就拆
TMP_DIR = ".tmp"

# NUL 和孤立代理写不进账本，也不是文字
_BAD_CHARS = re.compile("[\x00\ud800-\udfff]")
_PLAIN = re.compile(r"[\w.+-]+")  # 不加引号也许行的样子，还要读回来核一遍
_YAML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def clean_text(text: str) -> str:
    """换行符统一成 LF，写不进包的字符换成 U+FFFD。"""
    return _BAD_CHARS.sub("�", text).replace("\r\n", "\n").replace("\r", "\n")


def _yaml_escape(char: str) -> str:
    if char in _YAML_ESCAPES:
        return _YAML_ESCAPES[char]
    code = ord(char)
    # YAML 不收这些字符的原样写法，U+0085、U+2028、U+2029 还会被当成换行
    if code < 0x20 or 0x7F <= code <= 0x9F:
        return f"\\x{code:02X}"
    if char in "  ﻿￾￿":
        return f"\\u{code:04X}"
    return char


def yaml_text(text: str) -> str:
    """写成一行 YAML 双引号文本。读回来和原来一字不差，孤立代理除外，它先换成 U+FFFD。"""
    return '"' + "".join(_yaml_escape(char) for char in _BAD_CHARS.sub("�", text)) + '"'


def _reads_back(document: str, expected: object) -> bool:
    try:
        return load_yaml(document) == {"v": expected}
    except (yaml.YAMLError, RecursionError):
        return False


def yaml_scalar(text: str) -> str:
    """不加引号读回来还是同一段文字就不加，不然加引号。on、yes、null、2024 都会被 YAML 读成别的。"""
    if _PLAIN.fullmatch(text) and _reads_back(f"v: {text}", text):
        return text
    return yaml_text(text)


def yaml_list(items: Iterable[str]) -> str:
    """写成一行 YAML 列表，每一项读回来都和原来一样。"""
    written = []
    for item in items:
        plain = _PLAIN.fullmatch(item) is not None and _reads_back(f"v: [{item}]", [item])
        written.append(item if plain else yaml_text(item))
    return "[" + ", ".join(written) + "]"


def id_stamp(moment: datetime) -> str:
    """packet_id 里的日期和时分。不用 strftime：Linux 上它不给小于 1000 的年份补零。"""
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"_{moment.hour:02d}{moment.minute:02d}"
    )


def next_packet_id(covers_to: datetime, source: str, used: Collection[str]) -> str:
    """日期和时分取 covers_to（规范第 4 节），重名加 _2、_3。"""
    base = f"{id_stamp(covers_to)}_{source}"
    if base not in used:
        return base
    number = 2
    while f"{base}_{number}" in used:
        number += 1
    return f"{base}_{number}"


def front_matter(
    *,
    packet_id: str,
    created_at: datetime,
    source: str,
    framework: str,
    channel: str,
    env: str,
    kind: str,
    covers_from: datetime,
    covers_to: datetime,
    domain: Sequence[str],
    sensitive: Sequence[str] = (),
    continues: str | None = None,
    revision: int = 1,
    mailbox: str | None = None,
) -> str:
    """包头，字段顺序和规范第 14 节的例子一样。privacy 现在一律 normal（规范第 4 节）。"""
    lines = [
        "---",
        f"schema: {SCHEMA}",
        f"packet_id: {yaml_scalar(packet_id)}",
        f"created_at: {iso(created_at)}",
        f"source: {yaml_scalar(source)}",
        f"framework: {yaml_scalar(framework)}",
        f"channel: {yaml_scalar(channel)}",
    ]
    if mailbox is not None:
        lines.append(f"mailbox: {yaml_text(mailbox)}")
    lines += [
        f"env: {yaml_scalar(env)}",
        f"kind: {yaml_scalar(kind)}",
        f"covers_from: {iso(covers_from)}",
        f"covers_to: {iso(covers_to)}",
        f"domain: {yaml_list(domain)}",
        "privacy: normal",
        f"sensitive: {yaml_list(sensitive)}",
    ]
    if continues is not None:
        lines.append(f"continues: {yaml_scalar(continues)}")
    if revision != 1:
        lines.append(f"revision: {revision}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def check_out_dir(out_dir: Path, env: str) -> None:
    """输出目录直接是数据目录下的收件目录时，名字要和 env 对得上（规范第 8 节）。

    不然打包方自己校验能过，收件却报 env_mismatch；导入工具已经记成导过，改对目录再跑也不会再导。
    """
    name = out_dir.name.lower()
    if (name == "inbox" and env == "test") or (name == "inbox-test" and env == "prod"):
        raise ConfigError(f"输出目录是 {out_dir.name}，和 env {env} 对不上")


def continuation(line: str) -> str:
    """续行和邮件正文都缩进两格，这样写什么都变不成包的结构。空行可以不加。"""
    return f"  {line}" if line else ""


def attachment_block(
    *,
    name: str,
    media_type: str,
    size: int | None,
    of_section: int,
    of_line: int | None,
    state: str,
    reason: str | None,
    text: str,
) -> str:
    """附件小节里的一块（packet-spec 第 3 节“附件小节”）：### 标题、yaml 围栏、空一行、正文每行缩进两格。

    of_line 是空的写成邮件的 {section}，不然是对话的 {section, line}。name 要先洗过、打过码。
    """
    shown = " ".join(name.split()) or "未命名附件"  # 标题只给人看，压成一行
    of = f"{{section: {of_section}}}" if of_line is None else f"{{section: {of_section}, line: {of_line}}}"
    lines = [f"### 附件 · {shown}", "", "```yaml", f"name: {yaml_text(name)}", f"type: {yaml_text(media_type)}"]
    if size is not None:
        lines.append(f"size: {size}")
    lines += [f"of: {of}", f"text: {state}"]
    if reason:
        lines.append(f"reason: {yaml_scalar(reason)}")
    lines.append("```")
    if text:
        lines += ["", *(continuation(line) for line in text.split("\n"))]
    return "\n".join(lines) + "\n"


def attachment_section(notes: Sequence[str], blocks: Sequence[str]) -> str:
    """整个附件小节：开头一行一条说明，后面一块一块附件。都没有就是空的。"""
    if not notes and not blocks:
        return ""
    parts = ["## 附件\n" + "".join(f"- {note}\n" for note in notes)]
    parts += blocks
    return "\n".join(parts)


def deliver(directory: Path, name: str, content: str) -> Path:
    """按规范第 8 节投递：先写 .tmp，落盘，再改名移进目录。这样不会读到写了一半的文件。"""
    temporary = directory / TMP_DIR
    temporary.mkdir(parents=True, exist_ok=True)
    path = temporary / name
    with open(path, "wb") as stream:
        stream.write(content.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    target = directory / name
    os.replace(path, target)
    return target
