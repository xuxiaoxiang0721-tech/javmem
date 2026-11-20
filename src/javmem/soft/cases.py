"""一道软变化的“题”（`javmem-soft-cases/1`）：旧的事实或关系、后来的几行话、要判的是哪几行。

测试题（仓库外）、金标准转出来的（主会话转）、账本上找出来的（javmem.soft.find）都写成这个样子，judge 一视同仁：

    {
      "format": "javmem-soft-cases/1",
      "cases": [
        {
          "id": "C01",
          "packet_date": "2026-11-07",                 # 可以不写：要判那一行的日子
          "split": "dev",                              # 可以不写：测试题分 dev（调试）、test（考试）
          "private": false,                            # 可以不写：private 的不发给模型
          "partitions": ["business"],                  # 可以不写：可见范围的分区，只记着
          "old": [
            {"ref": "O1", "kind": "relation", "text": "林工 负责 东湖那批订单",
             "state": "生效", "since": "2026-10-12", "last_said": "2026-11-02",
             "id": null, "key": null}                  # id、key：账本里的编号和键，找出来的才有
          ],
          "lines": [
            {"ref": "T1", "at": "2026-11-07T10:28:00+08:00", "speaker": "我", "role": "owner",
             "text": "……", "sid": null, "quoted": false}
          ],
          "target": ["T2"],
          "meta": {}                                   # 随便记什么，不发给模型
        }
      ]
    }

- `old` 至少一条，编号 O1、O2……不重复；`kind` 是 fact 或 relation；`text` 是给模型看的一句话。
- `lines` 编号 T1、T2……不重复，按说话的先后；`at` 是带时区的 ISO 8601；`role` 是 owner（主人，写出来是“我”）、
  bot、other；`quoted` 是引的旧信、只帮着理解的行。
- `target` 是要判的那几行的编号，都要在 `lines` 里。
- 写错了报 SoftInputError，消息里只有位置和字段名，不带原文。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from javmem.timeutil import parse_aware

FORMAT = "javmem-soft-cases/1"
KINDS = ("fact", "relation")
ROLES = ("owner", "bot", "other")
SPLITS = ("dev", "test")
_OLD_REF = re.compile(r"O[1-9][0-9]{0,2}")
_LINE_REF = re.compile(r"T[1-9][0-9]{0,2}")
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


class SoftInputError(ValueError):
    """题目、答案、结论的文件写得不对。消息里只有位置和字段名，不带原文。"""


@dataclass(frozen=True)
class OldItem:
    ref: str
    kind: str
    text: str
    state: str | None = None
    since: str | None = None
    last_said: str | None = None
    id: str | None = None
    key: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref, "kind": self.kind, "text": self.text, "state": self.state, "since": self.since,
            "last_said": self.last_said, "id": self.id, "key": self.key,
        }


@dataclass(frozen=True)
class Line:
    ref: str
    at: str
    speaker: str
    role: str
    text: str
    sid: str | None = None
    quoted: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref, "at": self.at, "speaker": self.speaker, "role": self.role, "text": self.text,
            "sid": self.sid, "quoted": self.quoted,
        }


@dataclass(frozen=True)
class Case:
    id: str
    old: tuple[OldItem, ...]
    lines: tuple[Line, ...]
    target: tuple[str, ...]
    packet_date: str | None = None
    split: str | None = None
    private: bool = False
    partitions: tuple[str, ...] = ()
    meta: Mapping[str, object] = field(default_factory=dict)

    @property
    def target_lines(self) -> list[Line]:
        wanted = set(self.target)
        return [line for line in self.lines if line.ref in wanted]

    @property
    def said_at(self) -> str:
        """要判的那几行里最早的说话时间。"""
        return min(self.target_lines, key=lambda line: parse_aware(line.at)).at  # type: ignore[arg-type, return-value]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "packet_date": self.packet_date,
            "split": self.split,
            "private": self.private,
            "partitions": list(self.partitions),
            "old": [item.as_dict() for item in self.old],
            "lines": [line.as_dict() for line in self.lines],
            "target": list(self.target),
            "meta": dict(self.meta),
        }


def _text(value: object, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise SoftInputError(f"{where} 要是不空的字符串")
    return value


def _record(value: object, where: str, allowed: set[str], required: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SoftInputError(f"{where} 要是对象")
    unknown = set(value) - allowed
    if unknown:
        raise SoftInputError(f"{where} 有认不出的字段：{'、'.join(sorted(unknown))}")
    missing = required - set(value)
    if missing:
        raise SoftInputError(f"{where} 少了字段：{'、'.join(sorted(missing))}")
    return value


def _old(raw: object, where: str) -> OldItem:
    row = _record(raw, where, {"ref", "kind", "text", "state", "since", "last_said", "id", "key"}, {"ref", "kind", "text"})
    ref = _text(row["ref"], f"{where}.ref") or ""
    if not _OLD_REF.fullmatch(ref):
        raise SoftInputError(f"{where}.ref 要写成 O1、O2 这样")
    kind = row["kind"]
    if kind not in KINDS:
        raise SoftInputError(f"{where}.kind 只能是 {'、'.join(KINDS)}")
    return OldItem(
        ref, str(kind), _text(row["text"], f"{where}.text") or "",
        *(_text(row.get(name), f"{where}.{name}", optional=True) for name in ("state", "since", "last_said", "id", "key")),
    )


def _line(raw: object, where: str) -> Line:
    row = _record(raw, where, {"ref", "at", "speaker", "role", "text", "sid", "quoted"}, {"ref", "at", "speaker", "role", "text"})
    ref = _text(row["ref"], f"{where}.ref") or ""
    if not _LINE_REF.fullmatch(ref):
        raise SoftInputError(f"{where}.ref 要写成 T1、T2 这样")
    at = _text(row["at"], f"{where}.at") or ""
    if parse_aware(at) is None:
        raise SoftInputError(f"{where}.at 要写成带时区的 ISO 8601")
    role = row["role"]
    if role not in ROLES:
        raise SoftInputError(f"{where}.role 只能是 {'、'.join(ROLES)}")
    quoted = row.get("quoted", False)
    if not isinstance(quoted, bool):
        raise SoftInputError(f"{where}.quoted 要是 true 或 false")
    return Line(
        ref, at, _text(row["speaker"], f"{where}.speaker") or "", str(role), _text(row["text"], f"{where}.text") or "",
        _text(row.get("sid"), f"{where}.sid", optional=True), quoted,
    )


def case_from_dict(raw: object, where: str = "$") -> Case:
    row = _record(
        raw, where, {"id", "packet_date", "split", "private", "partitions", "old", "lines", "target", "meta"},
        {"id", "old", "lines", "target"},
    )
    case_id = _text(row["id"], f"{where}.id") or ""
    if not _CASE_ID.fullmatch(case_id):
        raise SoftInputError(f"{where}.id 只能用字母、数字和 ._:-，最多 64 个")
    old_raw, lines_raw, target_raw = row["old"], row["lines"], row["target"]
    if not isinstance(old_raw, list) or not old_raw:
        raise SoftInputError(f"{where}.old 要是不空的列表")
    if not isinstance(lines_raw, list) or not lines_raw:
        raise SoftInputError(f"{where}.lines 要是不空的列表")
    if not isinstance(target_raw, list) or not target_raw or not all(isinstance(one, str) for one in target_raw):
        raise SoftInputError(f"{where}.target 要是不空的字符串列表")
    old = tuple(_old(item, f"{where}.old[{n}]") for n, item in enumerate(old_raw))
    lines = tuple(_line(item, f"{where}.lines[{n}]") for n, item in enumerate(lines_raw))
    if len({item.ref for item in old}) != len(old):
        raise SoftInputError(f"{where}.old 的编号重复了")
    if len({line.ref for line in lines}) != len(lines):
        raise SoftInputError(f"{where}.lines 的编号重复了")
    known = {line.ref for line in lines}
    if not set(target_raw) <= known or len(set(target_raw)) != len(target_raw):
        raise SoftInputError(f"{where}.target 要写 lines 里有的编号，不重复")
    split = row.get("split")
    if split is not None and split not in SPLITS:
        raise SoftInputError(f"{where}.split 只能是 {'、'.join(SPLITS)}")
    private = row.get("private", False)
    if not isinstance(private, bool):
        raise SoftInputError(f"{where}.private 要是 true 或 false")
    partitions = row.get("partitions") or []
    if not isinstance(partitions, list) or not all(isinstance(one, str) for one in partitions):
        raise SoftInputError(f"{where}.partitions 要是字符串列表")
    meta = row.get("meta") or {}
    if not isinstance(meta, dict):
        raise SoftInputError(f"{where}.meta 要是对象")
    return Case(
        case_id, old, lines, tuple(target_raw), _text(row.get("packet_date"), f"{where}.packet_date", optional=True),
        split, private, tuple(partitions), dict(meta),
    )


def cases_from_dict(data: object) -> list[Case]:
    top = _record(data, "$", {"format", "cases"}, {"format", "cases"})
    if top["format"] != FORMAT:
        raise SoftInputError(f"$.format 只认 {FORMAT}")
    raw = top["cases"]
    if not isinstance(raw, list):
        raise SoftInputError("$.cases 要是列表")
    cases = [case_from_dict(item, f"$.cases[{n}]") for n, item in enumerate(raw)]
    if len({case.id for case in cases}) != len(cases):
        raise SoftInputError("$.cases 的 id 重复了")
    return cases


def load_cases(path: Path) -> list[Case]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SoftInputError(f"题目文件读不了：{type(error).__name__}") from None
    return cases_from_dict(data)


def dump_cases(cases: Sequence[Case], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"format": FORMAT, "cases": [case.as_dict() for case in cases]}, ensure_ascii=False, indent=1)
    path.write_bytes((text + "\n").encode("utf-8"))
