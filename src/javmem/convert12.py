"""把旧的 1.2 记忆包转成 javmem-packet/1。旧格式不直接收（packet-spec 开头）。

1.2 是 JSON：raw_records 是一条一条的原话，summaries、fact_candidates 是 Bot 写的。
怎么对到新格式，一张表：

| 1.2 | 新格式 |
| --- | --- |
| raw_records 的 text | 原话行，逐字。多行的第二行起缩进两格 |
| role、source_actor | 说话人：我、来源编号，或者另一个 Bot 的显示名 |
| occurred_at | 原话行的时间，到分钟（规范第 5 节）。秒没地方放 |
| window_start、window_end | covers_from、covers_to，再放宽到包住所有原话：晚到的记录会落在窗口外面 |
| packaged_at | created_at |
| previous_package_id | continues，只有上一包也在这一批里、是同一个来源才写 |
| summaries、fact_candidates、不是原话的记录 | 摘要小节，不算证据，不进原文索引 |
| attachments | 附件小节，一个附件一块：名字、类型、大小，of 指着 source_refs 那句话，读得了的写正文 |

附件的文件按 relative_path 找：相对这个 1.2 包所在的目录，给了 attachments_dir 就相对
attachments_dir/<1.2 包号>/。找不到、路径跑到外面去了、指纹或大小和 1.2 写的对不上的，不读，
reason 写 missing。读正文的规矩在 javmem.attachments（packet-spec 第 3 节“附件小节”）。附件的文件只读。

Bot 的名字、分区和包的种类都从配置来，这里不写死。配置的样子见 docs/development.md。
写进包的每一段文字都先洗过：换行统一、写不进包的字符换掉；显示名、附件名和类型压成一行再打码，
它们出现在一行的中间，多一个换行就能在包里伪造一句话。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import yaml

from javmem.attachments import (
    KIND_IMAGE,
    KIND_OTHER,
    MAX_FILE_BYTES,
    REASON_MISSING,
    REASON_TOO_LARGE,
    AttachmentText,
    kind_of,
    read_attachment,
)
from javmem.config import ConfigError
from javmem.packet import Rejected, validate_packet
from javmem.packet.model import OWNER_SPEAKER, TEXT_NONE
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
)
from javmem.redaction import RedactionPattern, redact
from javmem.sources import Registry
from javmem.timeutil import parse_aware
from javmem.yamlio import load_yaml

SCHEMA_12 = "1.2"
MANIFEST_NAME = "manifest.json"
DEFAULT_KIND = "daily"
KINDS = ("daily", "adhoc", "backfill")
DEFAULT_SYSTEM_SPEAKER = "系统"
UNKNOWN_BOT = "不明的 Bot"
SECTION_GAP = timedelta(minutes=30)  # 规范第 2 节：中间停了 30 分钟以上就算两段

# 行首的外部标记（规范第 5 节）。认不出的 content_type 当普通消息
_EXTERNAL_BY_CONTENT_TYPE = {"document_excerpt": "文件"}
_EXTERNAL_BY_ROLE = {"tool": "工具"}
_VERBATIM = (None, "verbatim_excerpt", "extracted_text", "redacted_excerpt")
_NOT_ORIGINAL = {"summary": "摘要", "inference": "推断"}  # 别的叫“不是原话”
_FRAMEWORK = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)  # 规范第 4 节
_SHOWABLE_KEY = re.compile(r"[A-Za-z_][\w-]{0,40}", re.ASCII)
_PLACEHOLDER = re.compile(r"\[已隐去:([^\]\n]*)\]")
# 收件校验时会把 covers 前后各放宽一天，太靠日历两头的时间一加一减就超出 datetime 能表示的范围
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc) + timedelta(days=3)
_LATEST = datetime.max.replace(tzinfo=timezone.utc) - timedelta(days=3)
_CONFIG_KEYS = frozenset({"rules", "kinds", "system_speaker", "owner_names"})
_RULE_KEYS = frozenset({"match", "source", "framework", "domain", "sensitive"})
_KIND_KEYS = frozenset({"match", "kind"})

# 不进原话的原因
SKIP_CONTEXT = "context"  # 带回来的旧内容，不算这包新说的
SKIP_RETRACTED = "retracted"  # 撤回的不录
SKIP_SUPERSEDED = "superseded"  # 被撤回或者被改过的那一版，录最后一版
SKIP_NOT_FINAL = "not_final"  # 没说完的草稿
SKIP_EMPTY = "empty"
SKIP_NO_TIME = "no_time"  # occurred_at、observed_at 都没有
SKIP_BAD_TIME = "bad_time"  # 写了时间，但读不了或者超出能表示的范围
MOVED_TO_SUMMARY = "moved_to_summary"  # 不是原话，进摘要小节

# 转出来了，但报告里要有数的情况
NOTE_TRUNCATED = "source_truncated"
NOTE_REDACTED_BY_SOURCE = "source_redacted"
NOTE_TIME_FROM_OBSERVED = "time_from_observed_at"
NOTE_NAIVE_TIME = "naive_time"  # 时间没写时区，按这个包的时区算
NOTE_UNKNOWN_SPEAKER = "unknown_speaker"
NOTE_SUPERSEDES_ELSEWHERE = "supersedes_not_in_batch"
NOTE_AMBIGUOUS_TARGET = "ambiguous_target"  # 要改的那条 record_id 在包里不止一条，不知道改哪条，都留着
NOTE_PREVIOUS_ELSEWHERE = "previous_not_in_batch"
NOTE_PREVIOUS_MISMATCH = "previous_other_source"  # 上一包是别的来源或者就是它自己，不写 continues
NOTE_QUOTE_LIKE = "quote_like_lines"  # 多行消息里 > 开头的行，收件会认成引用行
# 附件：读不到文件的几种（packet-spec 里都写 reason: missing），和找不到它属于哪句话的
NOTE_ATTACHMENT_NOT_FOUND = "attachment_not_found"  # 文件不在，或者 1.2 说没有原文件
NOTE_ATTACHMENT_BAD_PATH = "attachment_bad_path"  # relative_path 是绝对路径、跑到包目录外面去了
NOTE_ATTACHMENT_MISMATCH = "attachment_mismatch"  # 文件的指纹或大小和 1.2 写的对不上，不是它说的那个文件
NOTE_ATTACHMENT_UNVERIFIED = "attachment_unverified"  # 一批包共用一个目录，1.2 又没写指纹：认不出是不是它的文件
NOTE_ATTACHMENT_UNLINKED = "attachment_unlinked"  # source_refs 指的话都没写进包：只写一行说明，不写附件块

BAD_JSON = "bad_json"
BAD_SCHEMA = "unsupported_schema"
NO_RULE = "no_rule"
NO_TIME_ZONE = "no_time_zone"
DUPLICATE = "duplicate_package_id"  # 同一批里两个文件是同一个包号，后面那个不转
NO_RECORDS = "no_records"  # 没有内容不发空包，这不算错


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    source: str
    framework: str
    domain: tuple[str, ...]
    sensitive: tuple[str, ...]


@dataclass(frozen=True)
class ConvertConfig:
    rules: tuple[Rule, ...]
    kinds: tuple[tuple[re.Pattern[str], str], ...]
    system_speaker: str
    owner_names: frozenset[str]


def clean_name(text: str) -> str:
    """名字这类只占一行中间的文字：洗过，所有空白（换行、全角空格）压成一个空格。"""
    return " ".join(clean_text(text).split())


def _check_keys(entry: Mapping, allowed: frozenset[str], where: str) -> None:
    """认不出的字段一律报错。sensitive 打错字时规则照样能用，只是标记悄悄没了。"""
    for key in entry:
        if key not in allowed:
            shown = f" {key}" if isinstance(key, str) and _SHOWABLE_KEY.fullmatch(key) else ""
            raise ConfigError(f"{where} 有认不出的字段{shown}，可能是写错了字")


def _compile(regex: object, where: str) -> re.Pattern[str]:
    if not isinstance(regex, str) or not regex:
        raise ConfigError(f"{where} 的 match 要写成正则文本")
    try:
        return re.compile(regex)
    except re.error:
        raise ConfigError(f"{where} 的 match 不是合法的正则") from None


def _text_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{where} 要写成文本列表")
    return tuple(value)


def _parse_rule(entry: object, number: int, registry: Registry) -> Rule:
    where = f"转换配置第 {number} 条规则"
    if not isinstance(entry, Mapping):
        raise ConfigError(f"{where} 的写法不对")
    _check_keys(entry, _RULE_KEYS, where)
    source_id = entry.get("source")
    source = registry.sources.get(source_id) if isinstance(source_id, str) else None
    if source is None:
        raise ConfigError(f"{where} 的 source 不在来源登记表里")
    if source.channel != "chat":
        raise ConfigError(f"{where} 的 source 不是对话来源")
    framework = entry.get("framework", source.framework)
    if not isinstance(framework, str) or _FRAMEWORK.fullmatch(framework) is None:
        raise ConfigError(f"{where} 的 framework 要写小写英文、数字和连字符，登记表里也没有")
    domain = _text_list(entry.get("domain", list(source.default_domain)), f"{where} 的 domain")
    if not domain:
        raise ConfigError(f"{where} 要写 domain，登记表的 default_domain 也是空的")
    if not set(domain) <= registry.domains:
        raise ConfigError(f"{where} 的 domain 不在登记表的 domains 里")
    sensitive = _text_list(entry.get("sensitive", []), f"{where} 的 sensitive")
    if not set(sensitive) <= source.may_carry:
        raise ConfigError(f"{where} 的 sensitive 不在这个来源的 may_carry 里")
    return Rule(_compile(entry.get("match"), where), source.id, framework, domain, sensitive)


def _parse_kind(entry: object, number: int) -> tuple[re.Pattern[str], str]:
    where = f"转换配置 kinds 第 {number} 条"
    if not isinstance(entry, Mapping):
        raise ConfigError(f"{where} 的写法不对")
    _check_keys(entry, _KIND_KEYS, where)
    kind = entry.get("kind")
    if kind not in KINDS:
        raise ConfigError(f"{where} 的 kind 只能是 {'、'.join(KINDS)}")
    return _compile(entry.get("match"), where), kind


def load_convert_config(path: Path, registry: Registry) -> ConvertConfig:
    """读转换配置。写错了什么都不转，不然一个错字就把所有包转到错的来源去了。"""
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise ConfigError("转换配置读不了") from None
    except (yaml.YAMLError, RecursionError):
        raise ConfigError("转换配置不是合法的 YAML") from None
    if not isinstance(data, Mapping):
        raise ConfigError("转换配置的写法不对")
    _check_keys(data, _CONFIG_KEYS, "转换配置")
    entries = data.get("rules")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("转换配置要写 rules 列表")
    kinds = data.get("kinds", [])
    if not isinstance(kinds, list):
        raise ConfigError("转换配置的 kinds 要写成列表")
    speaker = data.get("system_speaker", DEFAULT_SYSTEM_SPEAKER)
    if not isinstance(speaker, str) or not clean_name(speaker):
        raise ConfigError("转换配置的 system_speaker 要写成文本")
    owners = _text_list(data.get("owner_names", []), "转换配置的 owner_names")
    return ConvertConfig(
        rules=tuple(_parse_rule(entry, number, registry) for number, entry in enumerate(entries, 1)),
        kinds=tuple(_parse_kind(entry, number) for number, entry in enumerate(kinds, 1)),
        system_speaker=speaker,
        owner_names=frozenset(name for name in (clean_name(owner) for owner in owners) if name),
    )


@dataclass
class PackageResult:
    """一个 1.2 包转成了什么。只有编号和数，不抄原文。"""

    file: str
    package_id: str | None = None
    source: str | None = None
    records: int = 0
    written: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    notes: dict[str, int] = field(default_factory=dict)
    redactions: dict[str, int] = field(default_factory=dict)
    attachments: dict[str, int] = field(default_factory=dict)  # 附件收了多少：full、truncated、none:原因
    packets: list[str] = field(default_factory=list)
    error: str | None = None  # 没转成的原因

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def note(self, what: str, count: int = 1) -> None:
        if count:
            self.notes[what] = self.notes.get(what, 0) + count

    def add_redactions(self, counts: Mapping[str, int]) -> None:
        for kind, count in counts.items():
            self.redactions[kind] = self.redactions.get(kind, 0) + count


@dataclass
class PacketResult:
    packet_id: str
    path: str  # 相对输出目录，/ 分隔
    package_id: str
    part: int
    parts: int
    utterances: int
    chars: int
    warnings: dict[str, int]


@dataclass
class ConvertReport:
    packages: list[PackageResult] = field(default_factory=list)
    packets: list[PacketResult] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)  # 没过校验的包
    # 每条原话进了哪个包的哪里。record_id 是 1.2 的，section、line 就是新格式的 position
    records: list[dict[str, object]] = field(default_factory=list)
    # 每个附件进了哪个包的第几块、属于哪句话、收了多少字。不抄内容
    attachments: list[dict[str, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected and all(package.error is None for package in self.packages)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "packages": [vars(package) for package in self.packages],
            "packets": [vars(packet) for packet in self.packets],
            "rejected": self.rejected,
            "records": self.records,
            "attachments": self.attachments,
        }


@dataclass
class _Line:
    said_at: datetime
    speaker: str
    text: str
    record_id: str | None


@dataclass(frozen=True)
class _Attachment:
    attachment_id: str  # 1.2 的 attachment_id，只进报告
    name: str  # 洗过、压成一行、打过码
    media_type: str  # 同上
    size: int | None
    references: tuple[str, ...]  # source_refs：提到它的那条记录
    note: str  # 找不到它属于哪句话时，写这一行说明
    item: Mapping  # 1.2 的原样，读正文时用
    base: Path | None  # relative_path 相对的目录
    shared: bool  # base 是一批包共用的目录（没写 --attachments）


@dataclass
class _Package:
    result: PackageResult
    rule: Rule
    kind: str
    timezone: tzinfo
    created_at: datetime
    covers_from: datetime
    covers_to: datetime
    previous: str | None
    lines: list[_Line]
    summaries: list[str]
    attachments: list[_Attachment]


@dataclass
class _Part:
    package: _Package
    number: int
    sections: list[list[_Line]]
    covers_from: datetime
    covers_to: datetime
    packet_id: str = ""
    continues: str | None = None


def _text_field(record: Mapping, name: str) -> str | None:
    value = record.get(name)
    return value if isinstance(value, str) else None


def _record_id(record: Mapping) -> str | None:
    """record_id 只当编号用，也要洗：报告是 JSON，孤立代理写不出去。"""
    value = _text_field(record, "record_id")
    return clean_text(value) if value is not None else None


def _redacted_name(name: str, patterns: Sequence[RedactionPattern], result: PackageResult) -> str:
    text, counts = redact(clean_name(name), patterns)
    result.add_redactions(counts)
    return text


def _display_name(name: str, source: str, patterns: Sequence[RedactionPattern], result: PackageResult) -> str:
    """显示名打码，冒号换成 -，撞了“我”、来源编号加后缀（规范第 5 节）。

    打码的 [已隐去:密码] 在名字里写成（已隐去-密码）：冒号会截断说话人；说话人带方括号的行收件算解析不了
    （D41），所以别的方括号也换成全角的。
    """
    text = _PLACEHOLDER.sub(r"（已隐去-\1）", _redacted_name(name, patterns, result))
    text = text.replace("：", "-").replace(":", "-").replace("[", "［").replace("]", "］")
    if not text:
        return UNKNOWN_BOT
    return f"{text}（显示名）" if text in (OWNER_SPEAKER, source) else text


def _speaker(
    record: Mapping, source: str, config: ConvertConfig, patterns: Sequence[RedactionPattern], result: PackageResult
) -> str:
    role = _text_field(record, "role")
    actor = record.get("source_actor")
    actor = actor if isinstance(actor, Mapping) else {}
    if role == "user":
        name = actor.get("author_name")
        name = clean_name(name) if isinstance(name, str) else ""
        # 配了 owner_names 时，写了名字又不在里面的才是群里的别人；没写名字的还是主人
        if config.owner_names and name and name not in config.owner_names:
            return _display_name(name, source, patterns, result)
        return OWNER_SPEAKER
    if role == "tool":
        return source
    if role == "assistant":
        # Bot 之间的消息：别的 Bot 发来的算它的，不能算到这个 Bot 头上
        if actor.get("direction") not in ("inbound", "inbound_from_agent"):
            return source
        name = actor.get("agent_name") or actor.get("author_name")
        if isinstance(name, str) and clean_name(name):
            return _display_name(name, source, patterns, result)
        result.note(NOTE_UNKNOWN_SPEAKER)
        return UNKNOWN_BOT
    return _display_name(config.system_speaker, source, patterns, result)


def _external_marker(record: Mapping) -> str | None:
    by_role = _EXTERNAL_BY_ROLE.get(_text_field(record, "role"))
    return by_role or _EXTERNAL_BY_CONTENT_TYPE.get(_text_field(record, "content_type"))


def _in_zone(value: object, zone: tzinfo) -> tuple[datetime | None, bool]:
    """读一个时间，换到这个包的时区。返回 (时间, 是不是没写时区)。

    读不了、换不过去、太靠日历两头的都是 None。
    """
    if not isinstance(value, str):
        return None, False
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None, False
    naive = moment.utcoffset() is None
    try:
        moment = (moment.replace(tzinfo=zone) if naive else moment).astimezone(zone)
    except (OverflowError, ValueError):
        return None, naive
    return (moment if _EARLIEST <= moment <= _LATEST else None), naive


def _written(value: object) -> bool:
    """写了一个时间。空串和空白跟没写一样。"""
    return value is not None and not (isinstance(value, str) and not value.strip())


def _record_time(record: Mapping, zone: tzinfo) -> tuple[datetime | None, str | None, list[str]]:
    """(时间, 不录的原因, 要记的笔记)。写了 occurred_at 就只认它；没写才用 observed_at。"""
    notes = []
    if _written(record.get("occurred_at")):
        moment, naive = _in_zone(record.get("occurred_at"), zone)
        if moment is None:
            return None, SKIP_BAD_TIME, notes
    else:
        moment, naive = _in_zone(record.get("observed_at"), zone)  # 改过的记录可能不知道什么时候说的
        if moment is None:
            return None, SKIP_NO_TIME, notes
        notes.append(NOTE_TIME_FROM_OBSERVED)
    if naive:
        notes.append(NOTE_NAIVE_TIME)
    return moment, None, notes


def _is_original(record: Mapping) -> bool:
    return record.get("content_kind") in (None, "original") and record.get("representation") in _VERBATIM


def _usable(record: Mapping, zone: tzinfo) -> bool:
    """这条会不会作为原话写进包。改正那条写不进去，它要改的那条就得留着，不然两版都没了。"""
    text = record.get("text")
    return (
        record.get("delivery_role") != "context"
        and record.get("operation") != "retract"
        and record.get("finality") in (None, "final")
        and isinstance(text, str)
        and bool(text.strip())
        and _is_original(record)
        and _record_time(record, zone)[0] is not None
    )


def _summary_item(label: str, text: str) -> str:
    first, *rest = clean_text(text).strip().split("\n")
    return "\n".join([f"- {label}：{first}", *(continuation(line) for line in rest)])


def _targets_of(record: Mapping, package_id: str) -> list[tuple[str, str]]:
    """这条记录撤回或者改掉了哪些记录，写成（1.2 包号, record_id）。"""
    if record.get("operation") not in ("retract", "correct"):
        return []
    targets = record.get("supersedes")
    return [
        ((_text_field(target, "package_id") or package_id), clean_text(target["item_id"]))
        for target in (targets if isinstance(targets, list) else [])
        if isinstance(target, Mapping) and isinstance(target.get("item_id"), str)
    ]


def _records(data: Mapping) -> list[Mapping]:
    records = data.get("raw_records")
    if not isinstance(records, list):
        return []
    items = [record for record in records if isinstance(record, Mapping)]

    def sequence(record: Mapping) -> int:
        value = record.get("sequence")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return sorted(items, key=sequence)


def _package_zone(data: Mapping) -> tuple[tzinfo | None, datetime | None, datetime | None, datetime | None]:
    """这个包的时区（取窗口和封包时间里第一个带时区的），和窗口、封包时间。"""
    coverage = data.get("coverage") if isinstance(data.get("coverage"), Mapping) else {}
    window_start = parse_aware(coverage.get("window_start"))
    window_end = parse_aware(coverage.get("window_end"))
    packaged_at = parse_aware(data.get("packaged_at"))
    zone = next((moment.tzinfo for moment in (window_end, window_start, packaged_at) if moment is not None), None)
    return zone, window_start, window_end, packaged_at


@dataclass
class _Targets:
    superseded: set[tuple[str, str]]
    notes: dict[str, Counter]  # 1.2 包号 → 要记的笔记


def _supersede_targets(loaded: Sequence[tuple[Path, Mapping, Rule]]) -> _Targets:
    """这一批里被撤回或者被改过的记录。它们可能在别的包里，所以要先看完一批。

    撤回一律算。改正只有它自己写得进包才算。要改的 record_id 在那个包里不止一条时谁都不动。
    """
    ids: dict[str, Counter] = {}
    for _, data, _ in loaded:
        ids[data["package_id"]] = Counter(_record_id(record) for record in _records(data) if _record_id(record) is not None)
    found = _Targets(set(), {})
    for _, data, _ in loaded:
        package_id = data["package_id"]
        zone = _package_zone(data)[0]
        notes = found.notes.setdefault(package_id, Counter())
        for record in _records(data):
            itself = (package_id, _record_id(record))
            targets = [target for target in _targets_of(record, package_id) if target != itself]  # 指向自己的不算
            if not targets:
                continue
            if record.get("operation") == "correct" and (zone is None or not _usable(record, zone)):
                continue
            for target_package, target_id in targets:
                count = ids.get(target_package, Counter())[target_id]
                if count == 0:
                    notes[NOTE_SUPERSEDES_ELSEWHERE] += 1  # 目标不在这一批，转换器够不着
                elif count > 1:
                    notes[NOTE_AMBIGUOUS_TARGET] += 1
                else:
                    found.superseded.add((target_package, target_id))
    return found


def _convert_record(
    record: Mapping,
    *,
    source: str,
    zone: tzinfo,
    config: ConvertConfig,
    patterns: Sequence[RedactionPattern],
    result: PackageResult,
    summaries: list[str],
) -> _Line | None:
    """一条记录变成一句原话。不进原话的记下原因，返回 None。"""
    if record.get("delivery_role") == "context":
        result.skip(SKIP_CONTEXT)
        return None
    if record.get("operation") == "retract":
        result.skip(SKIP_RETRACTED)
        return None
    if record.get("finality") not in (None, "final"):
        result.skip(SKIP_NOT_FINAL)
        return None

    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        result.skip(SKIP_EMPTY)
        return None
    text, counts = redact(clean_text(text), patterns)
    result.add_redactions(counts)

    moment, reason, notes = _record_time(record, zone)
    speaker = _speaker(record, source, config, patterns, result)
    if not _is_original(record):
        result.skip(MOVED_TO_SUMMARY)
        label = _NOT_ORIGINAL.get(_text_field(record, "content_kind") or "", "不是原话")
        said = f"{_full_time(moment)} {speaker}" if moment is not None else speaker
        summaries.append(_summary_item(f"{label}（{said}）", text))
        return None
    if moment is None:
        result.skip(reason or SKIP_NO_TIME)
        return None
    for note in notes:
        result.note(note)

    if record.get("truncated") is True:
        result.note(NOTE_TRUNCATED)
    if record.get("representation") == "redacted_excerpt":
        result.note(NOTE_REDACTED_BY_SOURCE)
    # 第二行起以 > 开头的行，收件会认成引用行（规范第 5 节），记个数
    result.note(NOTE_QUOTE_LIKE, sum(1 for line in text.split("\n")[1:] if line.strip() == ">" or line.strip().startswith("> ")))
    marker = _external_marker(record)
    if marker is not None:
        text = f"[外部:{marker}] {text}"
    return _Line(moment, speaker, text, _record_id(record))


def _attachment_bytes(item: Mapping, base: Path | None, result: PackageResult, *, shared: bool) -> bytes | AttachmentText:
    """1.2 写的附件文件的字节。读不到的返回 AttachmentText（没收正文），原因记进报告。附件的文件只读。

    shared：base 是一批包共用的目录（平铺的 1.2 包，没写 --attachments）。这时同一个 relative_path 可能是别的包的
    文件，1.2 没写指纹的认不出是不是它说的那个，不读（大小碰巧一样，业务的包就读到了管账的包的工资表）。
    """
    missing = AttachmentText(TEXT_NONE, REASON_MISSING)
    relative = item.get("relative_path")
    digest, size = item.get("sha256"), item.get("size_bytes")
    if base is None or item.get("availability") not in (None, "original_file") or not isinstance(relative, str):
        result.note(NOTE_ATTACHMENT_NOT_FOUND)
        return missing
    if shared and not (isinstance(digest, str) and digest.strip()):
        result.note(NOTE_ATTACHMENT_UNVERIFIED)
        return missing
    try:
        path = Path(relative.strip())
        if not relative.strip() or "\x00" in relative or path.is_absolute() or path.drive or ".." in path.parts:
            result.note(NOTE_ATTACHMENT_BAD_PATH)
            return missing
        root = base.resolve()
        target = (base / path).resolve()
        if root not in target.parents:  # 符号链接也不能把它带到包目录外面去
            result.note(NOTE_ATTACHMENT_BAD_PATH)
            return missing
        if not target.is_file():
            result.note(NOTE_ATTACHMENT_NOT_FOUND)
            return missing
        if target.stat().st_size > MAX_FILE_BYTES:  # 太大的不读进内存
            return AttachmentText(TEXT_NONE, REASON_TOO_LARGE)
        data = target.read_bytes()
    except ValueError:  # 路径里有写不进系统调用的字符
        result.note(NOTE_ATTACHMENT_BAD_PATH)
        return missing
    except OSError:
        result.note(NOTE_ATTACHMENT_NOT_FOUND)
        return missing
    if (isinstance(digest, str) and hashlib.sha256(data).hexdigest() != digest.strip().lower()) or (
        isinstance(size, int) and not isinstance(size, bool) and size != len(data)
    ):
        result.note(NOTE_ATTACHMENT_MISMATCH)
        return missing
    return data


def _attachment(
    item: Mapping, base: Path | None, patterns: Sequence[RedactionPattern], result: PackageResult, *, shared: bool
) -> _Attachment | None:
    """一个附件：名字、类型压成一行、打过码。正文到它有了着落（提到它的那句话写进了包）才读（_read_content）。
    路径、指纹、下载地址都不进包。"""
    raw_name = item.get("original_name")
    if not isinstance(raw_name, str) or not clean_name(raw_name):
        return None
    name = _redacted_name(raw_name, patterns, result)
    raw_media = item.get("media_type")
    raw_media = clean_name(raw_media) if isinstance(raw_media, str) else ""
    media = _redacted_name(raw_media, patterns, result) if raw_media else "application/octet-stream"
    size = item.get("size_bytes")
    size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
    references = item.get("source_refs")
    refs = tuple(clean_text(ref) for ref in references if isinstance(ref, str)) if isinstance(references, list) else ()
    attachment_id = item.get("attachment_id")
    note = "，".join([f"附件 {name}", *([media] if raw_media else []), *([f"{size} 字节"] if size is not None else [])])
    return _Attachment(
        attachment_id=clean_text(attachment_id) if isinstance(attachment_id, str) else "",
        name=name,
        media_type=media,
        size=size,
        references=refs,
        note=note,
        item=item,
        base=base,
        shared=shared,
    )


def _read_content(attachment: _Attachment, patterns: Sequence[RedactionPattern], result: PackageResult) -> AttachmentText:
    """读附件的正文（javmem.attachments），打码的数、收了多少记进报告。"""
    raw_name = clean_name(str(attachment.item.get("original_name")))
    raw_media = attachment.item.get("media_type")
    raw_media = clean_name(raw_media) if isinstance(raw_media, str) else ""
    if kind_of(raw_media, raw_name) in (KIND_IMAGE, KIND_OTHER):
        content = read_attachment(b"", media_type=raw_media, name=raw_name, patterns=patterns)
    else:
        found = _attachment_bytes(attachment.item, attachment.base, result, shared=attachment.shared)
        content = found if isinstance(found, AttachmentText) else read_attachment(
            found, media_type=raw_media, name=raw_name, patterns=patterns
        )
    result.add_redactions(content.redactions)
    key = content.state if content.state != TEXT_NONE else f"{TEXT_NONE}:{content.reason}"
    result.attachments[key] = result.attachments.get(key, 0) + 1
    return content


def _summary_items(data: Mapping, patterns: Sequence[RedactionPattern], result: PackageResult) -> list[str]:
    """summaries 和 fact_candidates 都不是原话。摘要小节不算证据，也不进原文索引（规范第 7 节）。"""
    items = []
    for label, key, text_key in (("摘要", "summaries", "text"), ("候选事实", "fact_candidates", "statement")):
        entries = data.get(key)
        for entry in entries if isinstance(entries, list) else []:
            text = entry.get(text_key) if isinstance(entry, Mapping) else None
            if isinstance(text, str) and text.strip():
                redacted, counts = redact(clean_text(text), patterns)
                result.add_redactions(counts)
                items.append(_summary_item(label, redacted))
    return items


def _prepare(
    file: str,
    data: Mapping,
    rule: Rule,
    *,
    config: ConvertConfig,
    patterns: Sequence[RedactionPattern],
    targets: _Targets,
    base: Path | None = None,
    shared: bool = True,
) -> tuple[PackageResult, _Package | None]:
    """base 是附件的 relative_path 相对的目录，shared 是这个目录一批包共用（没写 --attachments）。"""
    package_id = data["package_id"]
    result = PackageResult(file, package_id, rule.source)
    zone, window_start, window_end, packaged_at = _package_zone(data)
    if zone is None:
        result.error = NO_TIME_ZONE
        return result, None
    for note, count in targets.notes.get(package_id, Counter()).items():
        result.note(note, count)

    summaries = _summary_items(data, patterns, result)
    lines: list[_Line] = []
    for record in _records(data):
        result.records += 1
        if (package_id, _record_id(record)) in targets.superseded:
            result.skip(SKIP_SUPERSEDED)
            continue
        line = _convert_record(
            record,
            source=rule.source,
            zone=zone,
            config=config,
            patterns=patterns,
            result=result,
            summaries=summaries,
        )
        if line is not None:
            lines.append(line)
    result.written = len(lines)
    if not lines:
        result.skip(NO_RECORDS)
        return result, None

    # 窗口是这次采集的时间段，晚到的记录会落在它外面。covers 要包住所有原话（规范第 4、9 节）
    moments = [line.said_at for line in lines]
    edges = [_in_zone(moment.isoformat(), zone)[0] if moment else None for moment in (window_start, window_end)]
    covers_from = min([*moments, *([edges[0]] if edges[0] else [])])
    covers_to = max([*moments, *([edges[1]] if edges[1] else [])])
    created = _in_zone(packaged_at.isoformat(), zone)[0] if packaged_at else None

    attachments = []
    entries = data.get("attachments")
    for item in entries if isinstance(entries, list) else []:
        attachment = _attachment(item, base, patterns, result, shared=shared) if isinstance(item, Mapping) else None
        if attachment is not None:
            attachments.append(attachment)

    kind = next((name for pattern, name in config.kinds if pattern.search(package_id)), DEFAULT_KIND)
    continuity = data.get("continuity") if isinstance(data.get("continuity"), Mapping) else {}
    previous = continuity.get("previous_package_id")
    return result, _Package(
        result=result,
        rule=rule,
        kind=kind,
        timezone=zone,
        created_at=created or covers_to,
        covers_from=covers_from,
        covers_to=covers_to,
        previous=previous if isinstance(previous, str) else None,
        lines=lines,
        summaries=summaries,
        attachments=attachments,
    )


def _sections(lines: Sequence[_Line]) -> list[list[_Line]]:
    """一段对话一个小节，中间停了 30 分钟以上就算两段。顺序照 1.2 的 sequence，不重排。"""
    sections: list[list[_Line]] = []
    for line in lines:
        if sections and abs(line.said_at - sections[-1][-1].said_at) < SECTION_GAP:
            sections[-1].append(line)
        else:
            sections.append([line])
    return sections


def _size(lines: Sequence[_Line]) -> int:
    return sum(len(line.text) for line in lines)


def _split_oversized(section: list[_Line], limit: int) -> list[list[_Line]]:
    """一段对话自己就超了，只能在两句话之间切。一句话再长也不拆。"""
    chunks: list[list[_Line]] = [[]]
    for line in section:
        if chunks[-1] and _size(chunks[-1]) + len(line.text) > limit:
            chunks.append([])
        chunks[-1].append(line)
    return chunks


def _group(sections: list[list[_Line]], limit: int) -> list[list[list[_Line]]]:
    """原话超过 2 万字，按对话拆成几个包（规范第 2 节）。limit 是 0 就不拆。"""
    if limit <= 0:
        return [sections]
    groups: list[list[list[_Line]]] = [[]]
    used = 0
    for section in sections:
        for chunk in _split_oversized(section, limit) if _size(section) > limit else [section]:
            size = _size(chunk)
            if groups[-1] and used + size > limit:
                groups.append([])
                used = 0
            groups[-1].append(chunk)
            used += size
    return groups


def _floor_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def _parts(package: _Package, limit: int) -> list[_Part]:
    """拆出来的后一包 covers_from 接前一包的 covers_to（规范第 2 节）。

    原话的时间只到分钟，所以拆开处都取整到分钟：读出来的时间都落在时间段里，前后两包也接得上。
    只有一种例外：顺序照 1.2 不重排，后一包开头的话可能比前一包最后一句早一两分钟。
    这时 covers_from 往前挪到那句话，和前一包重一点。
    """
    groups = _group(_sections(package.lines), limit)
    parts = []
    reached = _floor_minute(package.covers_from)
    for number, sections in enumerate(groups, 1):
        moments = [line.said_at for section in sections for line in section]
        covers_from = min(reached, _floor_minute(min(moments)))
        covers_to = package.covers_to if number == len(groups) else max(reached, _floor_minute(max(moments)))
        parts.append(_Part(package, number, sections, covers_from, covers_to))
        reached = covers_to
    return parts


def _full_time(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d} {moment.hour:02d}:{moment.minute:02d}"


def _clock(moment: datetime, covers_to: datetime) -> str:
    """covers_to 那天的只写时分，别的写全日期（规范第 5 节）。"""
    return _full_time(moment)[-5:] if moment.date() == covers_to.date() else _full_time(moment)


def _section_title(section: Sequence[_Line], covers_to: datetime) -> str:
    """标题里是这段的起止时间，只给人看。跨天或者不是 covers_to 那天的写全日期。"""
    start = min(line.said_at for line in section)
    end = max(line.said_at for line in section)
    if start.date() == end.date() == covers_to.date():
        return f"## 原话 · {_clock(start, covers_to)}–{_clock(end, covers_to)}"
    return f"## 原话 · {_full_time(start)}–{_full_time(end)}"


def _render_line(line: _Line, covers_to: datetime) -> str:
    first, *rest = line.text.split("\n")
    head = f"- [{_clock(line.said_at, covers_to)}] {line.speaker}：{first}"
    return "\n".join([head, *(continuation(item) for item in rest)])


def _render(part: _Part, *, env: str, revision: int) -> str:
    package = part.package
    blocks = [
        front_matter(
            packet_id=part.packet_id,
            created_at=package.created_at,
            source=package.rule.source,
            framework=package.rule.framework,
            channel="chat",
            env=env,
            kind=package.kind,
            covers_from=part.covers_from,
            covers_to=part.covers_to,
            domain=package.rule.domain,
            sensitive=package.rule.sensitive,
            continues=part.continues,
            revision=revision,
        )
    ]
    for section in part.sections:
        lines = [_render_line(line, part.covers_to) for line in section]
        blocks.append("\n".join([_section_title(section, part.covers_to), *lines]) + "\n")
    return "\n".join(blocks)


@dataclass
class _PartAttachments:
    notes: list[str] = field(default_factory=list)  # 找不到它属于哪句话的附件，写一行说明
    # (第几个小节, 第几句话, 附件, 读出来的正文)
    blocks: list[tuple[int, int, _Attachment, AttachmentText]] = field(default_factory=list)


def _append_extras(text: str, summaries: Sequence[str], attached: _PartAttachments | None) -> str:
    """摘要和附件都接在原话后面。摘要不算证据；附件的正文是外部内容，进原文索引（规范第 3 节“附件小节”）。"""
    if summaries:
        text += "\n## 摘要（Bot 写的）\n" + "\n".join(summaries) + "\n"
    if attached is not None:
        blocks = [
            attachment_block(
                name=item.name,
                media_type=item.media_type,
                size=item.size,
                of_section=section_no,
                of_line=line_no,
                state=content.state,
                reason=content.reason,
                text=content.text,
            )
            for section_no, line_no, item, content in attached.blocks
        ]
        section = attachment_section(attached.notes, blocks)
        if section:
            text += "\n" + section
    return text


def _attachments_for_parts(
    package: _Package, parts: Sequence[_Part], patterns: Sequence[RedactionPattern]
) -> dict[int, _PartAttachments]:
    """附件跟着提到它的那句话走（source_refs），写进那句话所在的包，of 指着它，这时才读它的正文。

    提到它的话都没写进包（撤回了、不是原话……）的，不知道属于哪句话，在最后一包写一行说明，不写附件块，
    正文也不读（报告里记 unlinked）。
    """
    where: dict[str, tuple[int, int, int]] = {}
    for part in parts:
        for section_no, section in enumerate(part.sections, 1):
            for line_no, line in enumerate(section, 1):
                if line.record_id is not None:
                    where.setdefault(line.record_id, (part.number, section_no, line_no))
    placed: dict[int, _PartAttachments] = {}
    result = package.result
    for item in package.attachments:
        found = next((where[ref] for ref in item.references if ref in where), None)
        if found is None:
            result.note(NOTE_ATTACHMENT_UNLINKED)
            result.attachments["unlinked"] = result.attachments.get("unlinked", 0) + 1
            placed.setdefault(parts[-1].number, _PartAttachments()).notes.append(item.note)
            continue
        number, section_no, line_no = found
        content = _read_content(item, patterns, result)
        placed.setdefault(number, _PartAttachments()).blocks.append((section_no, line_no, item, content))
    for attached in placed.values():
        attached.blocks.sort(key=lambda block: (block[0], block[1]))  # 按它属于的那句话排，同一句话的照 1.2 的顺序
    return placed


def _read_package(
    path: Path, config: ConvertConfig, results: list[PackageResult], seen: set[str]
) -> tuple[Mapping, Rule] | None:
    result = PackageResult(path.name)

    def fail(code: str) -> None:
        result.error = code
        results.append(result)

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return fail(BAD_JSON)
    if not isinstance(data, Mapping) or data.get("schema_version") != SCHEMA_12:
        return fail(BAD_SCHEMA)
    package_id = data.get("package_id")
    if not isinstance(package_id, str) or not package_id or clean_text(package_id) != package_id:
        return fail(BAD_SCHEMA)
    result.package_id = package_id
    if package_id in seen:
        return fail(DUPLICATE)
    seen.add(package_id)
    rule = next((rule for rule in config.rules if rule.pattern.search(package_id)), None)
    if rule is None:
        return fail(NO_RULE)
    return data, rule


def _link(packages: Sequence[_Package], planned: Sequence[list[_Part]]) -> None:
    """上一包在这一批里、是同一个来源、又不是它自己，才写 continues。拆开的几包一个接一个。"""
    last = {package.result.package_id: (package.rule.source, parts[-1].packet_id) for package, parts in zip(packages, planned)}
    for package, parts in zip(packages, planned):
        previous = None
        if package.previous is not None:
            found = last.get(package.previous)
            if found is None:
                package.result.note(NOTE_PREVIOUS_ELSEWHERE)
            elif found[0] != package.rule.source or package.previous == package.result.package_id:
                package.result.note(NOTE_PREVIOUS_MISMATCH)
            else:
                previous = found[1]
        for part in parts:
            part.continues = previous
            previous = part.packet_id


def convert_directory(
    input_dir: Path,
    out_dir: Path,
    *,
    config: ConvertConfig,
    registry: Registry,
    patterns: Sequence[RedactionPattern],
    env: str = "test",
    revision: int = 1,
    max_chars: int = MAX_RAW_CHARS,
    attachments_dir: Path | None = None,
) -> ConvertReport:
    """转一个目录下的 1.2 包，写到 out_dir/<来源>/<packet_id>.md。输入只读，附件的文件也只读。

    out_dir 可以直接是数据目录下的 inbox-test：写法就是规范第 8 节的投递。
    每个包写之前都过一遍 validate_packet，没过的不写。
    附件的 relative_path 相对这个 1.2 包所在的目录；给了 attachments_dir 就相对 attachments_dir/<1.2 包号>/。
    """
    if not input_dir.is_dir():
        raise ConfigError("放 1.2 包的目录不存在")
    if attachments_dir is not None and not attachments_dir.is_dir():
        raise ConfigError("放附件的目录不存在")
    check_out_dir(out_dir, env)
    report = ConvertReport()
    loaded: list[tuple[Path, Mapping, Rule]] = []
    seen: set[str] = set()
    for path in sorted(input_dir.glob("*.json")):
        if path.name == MANIFEST_NAME:
            continue
        read = _read_package(path, config, report.packages, seen)
        if read is not None:
            loaded.append((path, read[0], read[1]))

    targets = _supersede_targets(loaded)
    packages: list[_Package] = []
    for path, data, rule in loaded:
        base = path.parent if attachments_dir is None else _package_dir(attachments_dir, data["package_id"])
        result, package = _prepare(
            path.name, data, rule, config=config, patterns=patterns, targets=targets, base=base, shared=attachments_dir is None
        )
        report.packages.append(result)
        if package is not None:
            packages.append(package)
    report.packages.sort(key=lambda result: result.file)  # 报告按文件名排，没转成的也在自己的位置上

    # 按来源和时间排好再编号，同一批输入每次编出来都一样
    packages.sort(key=lambda package: (package.rule.source, package.covers_to, package.result.package_id))
    used: set[str] = set()
    planned: list[list[_Part]] = []
    for package in packages:
        parts = _parts(package, max_chars)
        for part in parts:
            part.packet_id = next_packet_id(part.covers_to, package.rule.source, used)
            used.add(part.packet_id)
            package.result.packets.append(part.packet_id)
        planned.append(parts)
    _link(packages, planned)

    for package, parts in zip(packages, planned):
        attached = _attachments_for_parts(package, parts, patterns)
        for part in parts:
            text = _render(part, env=env, revision=revision)
            text = _append_extras(
                text,
                package.summaries if part.number == len(parts) else (),
                attached.get(part.number),
            )
            _write(part, text, len(parts), out_dir, registry=registry, env=env, report=report, attached=attached.get(part.number))
    return report


def _package_dir(attachments_dir: Path, package_id: str) -> Path | None:
    """attachments_dir/<1.2 包号>/。包号要是一个普通的目录名，不然会跑到 attachments_dir 外面去。"""
    if Path(package_id).name != package_id or package_id in (".", "..") or "\\" in package_id or "/" in package_id:
        return None
    return attachments_dir / package_id


def _write(
    part: _Part,
    text: str,
    parts: int,
    out_dir: Path,
    *,
    registry: Registry,
    env: str,
    report: ConvertReport,
    attached: _PartAttachments | None = None,
) -> None:
    package = part.package
    source = package.rule.source
    name = f"{part.packet_id}.md"
    try:
        packet = validate_packet(text.encode("utf-8"), filename=name, directory=source, library=env, registry=registry)
    except Rejected as rejected:
        report.rejected.append({"packet_id": part.packet_id, "code": rejected.code, "detail": rejected.detail})
        return
    except (OverflowError, ValueError, UnicodeError) as error:  # 前面都拦住了，这里只防万一，不让一个包拖垮整批
        report.rejected.append({"packet_id": part.packet_id, "code": "unwritable", "detail": type(error).__name__})
        return
    deliver(out_dir / source, name, text)
    warnings: dict[str, int] = {}
    for warning in packet.warnings:
        warnings[warning.kind] = warnings.get(warning.kind, 0) + 1
    report.packets.append(
        PacketResult(
            packet_id=part.packet_id,
            path=f"{source}/{name}",
            package_id=package.result.package_id,
            part=part.number,
            parts=parts,
            utterances=sum(len(section) for section in part.sections),
            chars=sum(_size(section) for section in part.sections),
            warnings=warnings,
        )
    )
    for section_number, section in enumerate(part.sections, 1):
        for line_number, line in enumerate(section, 1):
            report.records.append(
                {
                    "package_id": package.result.package_id,
                    "record_id": line.record_id,
                    "packet_id": part.packet_id,
                    "section": section_number,
                    "line": line_number,
                }
            )
    for number, (section_number, line_number, item, content) in enumerate(attached.blocks if attached else (), 1):
        report.attachments.append(
            {
                "package_id": package.result.package_id,
                "attachment_id": item.attachment_id,
                "packet_id": part.packet_id,
                "block": number,
                "of": {"section": section_number, "line": line_number},
                "text": content.state,
                "reason": content.reason,
                "chars": content.chars,
                "kept_chars": len(content.text),
            }
        )
