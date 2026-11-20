"""读金标准（J3.5 的预标，`javmem-gold-prelabel/1`），叠上审阅结果，整理成打分用的样子。

一个单元一个 JSON（结构见金标准自己的说明），外加一份实体表（`javmem-gold-entities/1`）。这里认的规矩：

- **审阅结果（D109）：** 审过的以审的为准，没审的照预标。审阅结果按条目编号，每条 `{"v": "ok"|"wrong"|"fix", "text": …}`：
  ok 照留；wrong 去掉；fix 带着 `item`（改好的整条，和预标一样的结构）的换成它，只有 `text` 的先不算分、单列
  （人写的话改不成结构，等改成结构再算）。补的条目放 `additions`，带 `item` 的加进来，只有 `text` 的单列。
  两种写法都认：`{"reviews": {编号: {…}}, "additions": […]}`，或者直接 `{编号: {…}}`。
- **这一轮审哪些（D109）：** 要问人的（note 开头是“问 某人：”）、拿不准的、影响大的，加上剩下的里编号的 sha256
  除以 10 余 0 的。JSON 里没标，这里照规则算（round1_ids）。
- **不标的句子（`not_labeled`）：** 抽到了不算错，没抽到不扣分。写 range 的是一段（含两头），写 sids 的是这几句；
  邮件单元写 packet 的只管那一包；什么都没写、只有 reason 的，是这个单元所有附件正文的句子（邮件那串的附件不标，D98）。
- **只在引用、签名、附件里的（`only_in_quote`、`only_in_signature`、`only_in_attachment`）：** 单列算分，抽不到不扣分。
- **同一条关系在几个单元里都有，合成一条（D103）：** 按（主体、关系、客体）合；开始日子用最准的那个（精度最高的，
  一样高的取说得最多的）；不是追加证据、开始日子又差得远的，算另一段，那天开始也对（gold_relations）。
- **亲属（D90）：** 一头是主人时主人当客体；打分只比具体关系（kinship），反过来写的算同一条。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from javmem.scoring.common import ScoreInputError, When
from javmem.scoring.dates import TOLERANCE_DAYS, close, interval, precision_rank

GOLD_SCHEMA = "javmem-gold-prelabel/1"
ENTITY_SCHEMA = "javmem-gold-entities/1"
ITEM_TYPES = ("fact", "relation", "change", "alias")
FACT_CLASSES = ("事实", "主人的偏好和规矩", "主人的决定和计划", "Bot 的建议和判断", "和主人有关的行情")
ONLY_IN = ("only_in_quote", "only_in_signature", "only_in_attachment")
ADDITIONAL = "追加证据"
VERDICTS = ("ok", "wrong", "fix")
ASK = re.compile(r"^问\s+[^\s“”\"「」：:]{1,20}[:：]")  # note 开头写“问 某人：”的是要问人的（金标准的写法）
NOT_ALIAS_KINDS = ("不是同一个", "泛称（不建实体）")


@dataclass(frozen=True)
class Evidence:
    sid: str
    quote: str = ""
    role: str | None = None
    sentence: str = ""
    said_at: str | None = None


@dataclass(frozen=True)
class GoldItem:
    id: str
    unit: str
    type: str
    content: str
    subject: tuple[str, str] | None
    evidence: tuple[Evidence, ...]
    when: When = When()
    certainty: str | None = None
    speaker: str | None = None
    flags: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    unsure: bool = False
    important: bool = False
    note: str = ""
    fact_class: str | None = None
    attribute: str | None = None
    value: str | None = None
    relation: str | None = None
    object: tuple[str, str] | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
    state: str | None = None
    change: Mapping[str, object] | None = None
    alias: Mapping[str, str] | None = None

    @property
    def sids(self) -> frozenset[str]:
        return frozenset(e.sid for e in self.evidence)

    @property
    def only_in(self) -> str | None:
        for flag in ONLY_IN:
            if flag in self.flags:
                return flag
        return None

    @property
    def ask(self) -> bool:
        return bool(ASK.match(self.note))

    def role_sids(self, role: str) -> frozenset[str]:
        return frozenset(e.sid for e in self.evidence if e.role == role)


@dataclass(frozen=True)
class NotLabeled:
    packet: str | None
    start: str | None = None
    end: str | None = None
    sids: frozenset[str] = frozenset()
    attachments: bool = False  # 只写了 reason：这个单元所有附件正文的句子


@dataclass(frozen=True)
class GoldUnit:
    id: str
    kind: str
    packets: tuple[str, ...]
    items: tuple[GoldItem, ...]
    not_labeled: tuple[NotLabeled, ...] = ()
    thread_id: str | None = None
    messages: tuple[str, ...] = ()
    blind: bool = False
    title: str = ""
    new_entities: tuple[tuple[str, str], ...] = ()
    revisions: Mapping[str, int] = field(default_factory=dict)  # 标的是包的哪一版

    @property
    def is_dialog(self) -> bool:
        return self.kind == "dialog"


@dataclass(frozen=True)
class GoldEntity:
    name: str
    type: str
    names: frozenset[str]  # 实体表的别名（像名字的那些），不含标准名
    origin: str


@dataclass
class ReviewStats:
    reviewed: int = 0
    by_verdict: dict[str, int] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)  # 判错、去掉的
    replaced: list[str] = field(default_factory=list)  # 改好的整条换上的
    pending_fix: list[str] = field(default_factory=list)  # 改了、只有文字的：先不算分
    added: list[str] = field(default_factory=list)
    pending_additions: int = 0
    unknown_ids: list[str] = field(default_factory=list)
    round1: int = 0
    round1_reviewed: int = 0
    sample: int = 0
    sample_reviewed: int = 0
    sample_bad: int = 0  # 抽查的一成里判错或者要改的

    def as_dict(self) -> dict[str, object]:
        return {
            "reviewed": self.reviewed,
            "by_verdict": dict(sorted(self.by_verdict.items())),
            "dropped": len(self.dropped),
            "replaced": len(self.replaced),
            "pending_fix": len(self.pending_fix),
            "added": len(self.added),
            "pending_additions": self.pending_additions,
            "unknown_ids": len(self.unknown_ids),
            "round1": self.round1,
            "round1_reviewed": self.round1_reviewed,
            "sample": self.sample,
            "sample_reviewed": self.sample_reviewed,
            "sample_bad": self.sample_bad,
            "sample_bad_rate": round(self.sample_bad / self.sample_reviewed, 4) if self.sample_reviewed else None,
        }


@dataclass
class Gold:
    units: dict[str, GoldUnit]
    entities: dict[str, GoldEntity]
    review: ReviewStats
    pending: tuple[GoldItem, ...] = ()  # 审过、改了但还没写成结构的预标（不算分，报告里单列）

    def items(self, kind: str | None = None) -> list[GoldItem]:
        return [item for unit in self.units.values() for item in unit.items if kind is None or item.type == kind]


# ---------- 读单元 ----------


def _pair(value: object, where: str) -> tuple[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not isinstance(value.get("type"), str):
        raise ScoreInputError(f"{where} 要写成 {{name, type}}")
    return value["name"], value["type"]


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_item(raw: object, unit: str) -> GoldItem:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise ScoreInputError(f"单元 {unit} 有一条没有编号")
    where = raw["id"]
    kind = raw.get("type")
    if kind not in ITEM_TYPES:
        raise ScoreInputError(f"{where} 的 type 只能是 {'、'.join(ITEM_TYPES)}")
    evidence = []
    for one in raw.get("evidence") or []:
        if not isinstance(one, dict) or not isinstance(one.get("sid"), str):
            raise ScoreInputError(f"{where} 的出处要写 sid")
        evidence.append(Evidence(one["sid"], _str(one.get("quote")) or "", _str(one.get("role")),
                                 _str(one.get("sentence")) or "", _str(one.get("said_at"))))
    when = raw.get("when") or {}
    if not isinstance(when, dict):
        raise ScoreInputError(f"{where} 的 when 要是对象")
    fields = raw.get("fields") or {}
    if not isinstance(fields, dict):
        raise ScoreInputError(f"{where} 的 fields 要是对象")
    change = raw.get("change")
    alias = raw.get("alias")
    if kind == "change" and not isinstance(change, dict):
        raise ScoreInputError(f"{where} 是状态变化，要有 change")
    if kind == "alias" and (not isinstance(alias, dict) or not isinstance(alias.get("mention"), str)
                            or not isinstance(alias.get("entity"), str)):
        raise ScoreInputError(f"{where} 是别名，要有 alias.mention 和 alias.entity")
    if kind == "relation" and (not isinstance(raw.get("relation"), str) or raw.get("object") is None):
        raise ScoreInputError(f"{where} 是关系，要有 relation 和 object")
    return GoldItem(
        id=where,
        unit=unit,
        type=kind,
        content=_str(raw.get("content")) or "",
        subject=_pair(raw.get("subject"), f"{where} 的 subject"),
        evidence=tuple(evidence),
        when=When(_str(when.get("from")), _str(when.get("to")), _str(when.get("precision")) or "unknown"),
        certainty=_str(raw.get("certainty")),
        speaker=_str(raw.get("speaker")),
        flags=tuple(one for one in raw.get("flags") or [] if isinstance(one, str)),
        links=tuple(one for one in raw.get("links") or [] if isinstance(one, str)),
        unsure=bool(raw.get("unsure")),
        important=bool(raw.get("important")),
        note=_str(raw.get("note")) or "",
        fact_class=_str(raw.get("fact_class")),
        attribute=_str(raw.get("attribute")),
        value=_str(raw.get("value")),
        relation=_str(raw.get("relation")),
        object=_pair(raw.get("object"), f"{where} 的 object"),
        fields={k: v for k, v in fields.items() if isinstance(k, str) and isinstance(v, str)},
        state=_str(raw.get("state")),
        change=dict(change) if isinstance(change, dict) else None,
        alias={k: v for k, v in alias.items() if isinstance(v, str)} if isinstance(alias, dict) else None,
    )


def _not_labeled(raw: object, unit: str, packets: Sequence[str]) -> NotLabeled:
    if not isinstance(raw, dict):
        raise ScoreInputError(f"单元 {unit} 的 not_labeled 要是对象")
    packet = _str(raw.get("packet"))
    if packet is None and len(packets) == 1:
        packet = packets[0]
    if "range" in raw:
        pair = raw["range"]
        if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(one, str) for one in pair):
            raise ScoreInputError(f"单元 {unit} 的 not_labeled.range 要写两头")
        return NotLabeled(packet, pair[0], pair[1])
    if "sids" in raw:
        sids = raw["sids"]
        if not isinstance(sids, list) or not all(isinstance(one, str) for one in sids):
            raise ScoreInputError(f"单元 {unit} 的 not_labeled.sids 要是字符串的列表")
        return NotLabeled(packet, sids=frozenset(sids))
    return NotLabeled(_str(raw.get("packet")), attachments=True)


def parse_unit(data: object, origin: str = "") -> GoldUnit:
    if not isinstance(data, dict) or data.get("schema") != GOLD_SCHEMA:
        raise ScoreInputError(f"{origin or '金标准的单元'} 的 schema 不是 {GOLD_SCHEMA}")
    unit = data.get("unit")
    if not isinstance(unit, str):
        raise ScoreInputError(f"{origin} 没有 unit")
    packets = tuple(one for one in data.get("packets") or [] if isinstance(one, str))
    if not packets:
        raise ScoreInputError(f"单元 {unit} 没有 packets")
    items = tuple(parse_item(raw, unit) for raw in data.get("items") or [])
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise ScoreInputError(f"单元 {unit} 的编号 {item.id} 重复了")
        seen.add(item.id)
    new_entities = tuple(
        (one["name"], one["type"]) for one in data.get("new_entities") or []
        if isinstance(one, dict) and isinstance(one.get("name"), str) and isinstance(one.get("type"), str)
    )
    return GoldUnit(
        id=unit,
        kind=_str(data.get("unit_kind")) or "dialog",
        packets=packets,
        items=items,
        not_labeled=tuple(_not_labeled(raw, unit, packets) for raw in data.get("not_labeled") or []),
        thread_id=_str(data.get("thread_id")),
        messages=tuple(one for one in data.get("messages") or [] if isinstance(one, str)),
        blind=bool(data.get("blind")),
        title=_str(data.get("title")) or "",
        new_entities=new_entities,
        revisions={k: v for k, v in (data.get("revisions") or {}).items()
                   if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)},
    )


# 实体表的别名里有几条是说明，不是叫法（“包里说话人‘我’”“孩子的事里叫‘妈妈’”这种）：带引号的、带“里叫”“说话人”的不当叫法
_DESCRIPTIVE = re.compile(r"[“”\"「」『』]|里叫|说话人")


def parse_entities(data: object) -> dict[str, GoldEntity]:
    if not isinstance(data, dict) or data.get("schema") != ENTITY_SCHEMA:
        raise ScoreInputError(f"实体表的 schema 不是 {ENTITY_SCHEMA}")
    out: dict[str, GoldEntity] = {}
    for number, raw in enumerate(data.get("entities") or []):
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not isinstance(raw.get("type"), str):
            raise ScoreInputError(f"实体表第 {number + 1} 个要有 name 和 type")
        names = frozenset(
            one for one in raw.get("aliases") or [] if isinstance(one, str) and one != raw["name"] and not _DESCRIPTIVE.search(one)
        )
        out[raw["name"]] = GoldEntity(raw["name"], raw["type"], names, _str(raw.get("from")) or "实体表")
    return out


# ---------- 审阅结果（D109） ----------


def round1_ids(items: Iterable[GoldItem]) -> tuple[set[str], set[str]]:
    """这一轮要审的：(要紧的, 抽查的一成)。要紧的是要问人的、拿不准的、影响大的；剩下的里编号的 sha256 除以 10 余 0 的抽查。"""
    important: set[str] = set()
    sample: set[str] = set()
    for item in items:
        if item.important or item.unsure or item.ask:
            important.add(item.id)
        elif int(hashlib.sha256(item.id.encode("utf-8")).hexdigest(), 16) % 10 == 0:
            sample.add(item.id)
    return important, sample


def _reviews(data: object) -> tuple[dict[str, dict[str, object]], list[object]]:
    if not isinstance(data, dict):
        raise ScoreInputError("审阅结果要是对象")
    if "reviews" in data or "additions" in data:
        if set(data) - {"reviews", "additions"}:
            raise ScoreInputError("审阅结果只能有 reviews 和 additions")
        reviews = data.get("reviews") or {}
        additions = data.get("additions") or []
    else:
        reviews, additions = data, []
    if not isinstance(reviews, dict) or not isinstance(additions, list):
        raise ScoreInputError("审阅结果的 reviews 要是对象、additions 要是列表")
    out: dict[str, dict[str, object]] = {}
    for item_id, entry in reviews.items():
        if not isinstance(entry, dict) or entry.get("v") not in VERDICTS:
            raise ScoreInputError(f"审阅结果里 {item_id} 的 v 只能是 {'、'.join(VERDICTS)}")
        out[item_id] = entry
    return out, additions


def apply_review(units: dict[str, GoldUnit], data: object | None) -> tuple[dict[str, GoldUnit], ReviewStats, list[GoldItem]]:
    every = [item for unit in units.values() for item in unit.items]
    important, sample = round1_ids(every)
    stats = ReviewStats(round1=len(important) + len(sample), sample=len(sample))
    if data is None:
        return units, stats, []
    reviews, additions = _reviews(data)
    known = {item.id for item in every}
    stats.unknown_ids = sorted(set(reviews) - known)
    pending: list[GoldItem] = []
    changed: dict[str, list[GoldItem]] = {}
    for unit in units.values():
        kept: list[GoldItem] = []
        for item in unit.items:
            entry = reviews.get(item.id)
            if entry is None:
                kept.append(item)
                continue
            verdict = str(entry["v"])
            stats.reviewed += 1
            stats.by_verdict[verdict] = stats.by_verdict.get(verdict, 0) + 1
            if item.id in important or item.id in sample:
                stats.round1_reviewed += 1
            if item.id in sample:
                stats.sample_reviewed += 1
                if verdict != "ok":
                    stats.sample_bad += 1
            if verdict == "ok":
                kept.append(item)
            elif verdict == "wrong":
                stats.dropped.append(item.id)
            elif isinstance(entry.get("item"), dict):
                fixed = parse_item({**entry["item"], "id": item.id}, unit.id)
                kept.append(fixed)
                stats.replaced.append(item.id)
            else:
                stats.pending_fix.append(item.id)
                pending.append(item)
        changed[unit.id] = kept
    added_ids: set[str] = set()
    for number, raw in enumerate(additions):
        if not isinstance(raw, dict) or not isinstance(raw.get("unit"), str) or raw["unit"] not in units:
            raise ScoreInputError(f"审阅结果的 additions 第 {number + 1} 条要写 unit（有的单元号）")
        if isinstance(raw.get("item"), dict):
            item = parse_item(raw["item"], raw["unit"])
            if item.id in known or item.id in added_ids:  # 编号在所有单元里都不能重
                raise ScoreInputError(f"审阅结果的 additions 第 {number + 1} 条的编号和已有的重复了")
            added_ids.add(item.id)
            changed[raw["unit"]].append(item)
            stats.added.append(item.id)
        else:
            stats.pending_additions += 1
    return {uid: replace(unit, items=tuple(changed[uid])) for uid, unit in units.items()}, stats, pending


# ---------- 合起来读 ----------


def load_gold(unit_files: Sequence[Path], entity_table: Path, review: Path | None = None) -> Gold:
    units: dict[str, GoldUnit] = {}
    for path in unit_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ScoreInputError(f"金标准的文件 {path.name} 读不了：{type(error).__name__}") from None
        unit = parse_unit(data, path.name)
        if unit.id in units:
            raise ScoreInputError(f"单元 {unit.id} 有两份")
        units[unit.id] = unit
    try:
        entities = parse_entities(json.loads(entity_table.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScoreInputError(f"实体表读不了：{type(error).__name__}") from None
    for unit in units.values():
        for name, entity_type in unit.new_entities:
            entities.setdefault(name, GoldEntity(name, entity_type, frozenset(), f"单元 {unit.id}"))
    review_data = None
    if review is not None:
        try:
            review_data = json.loads(review.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ScoreInputError(f"审阅结果读不了：{type(error).__name__}") from None
    units, stats, pending = apply_review(units, review_data)
    return Gold(units, entities, stats, tuple(pending))


def unit_files(folder: Path, extra: Sequence[Path] = ()) -> list[Path]:
    """金标准文件夹里的单元（`*.json`，实体表除外），加上另外点名的（比如盲测那两包）。"""
    found = [path for path in sorted(folder.glob("*.json")) if _is_unit(path)]
    for path in extra:
        if path.is_dir():
            found.extend(one for one in sorted(path.glob("*.json")) if _is_unit(one))
        else:
            found.append(path)
    return found


def _is_unit(path: Path) -> bool:
    """整个读进来看 schema（单元文件不大）；读不了的、不是这种格式的不算单元。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("schema") == GOLD_SCHEMA


# ---------- 句子在不在单元里、是不是不标的 ----------


def tail_key(tail: str) -> tuple[tuple[int, object], ...]:
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in tail.split("."))


def split_sid(sid: str) -> tuple[str, str]:
    packet, _, tail = sid.partition("#")
    return packet, tail


def sid_order(sid: str) -> tuple[str, tuple[tuple[int, object], ...]]:
    """句子的先后：包号（开头是日子）再按小节、行、句。同一包里的先后就是说话的先后。"""
    packet, tail = split_sid(sid)
    return packet, tail_key(tail)


def utterance(sid: str) -> str:
    """一句所在的那一段话（包号#小节.行）：模型引的是同一段里别的句子，也算挨着。"""
    packet, tail = split_sid(sid)
    parts = tail.split(".")
    return f"{packet}#{'.'.join(parts[:2])}" if len(parts) >= 3 else sid


def is_not_labeled(unit: GoldUnit, sid: str) -> bool:
    packet, tail = split_sid(sid)
    for entry in unit.not_labeled:
        if entry.attachments:
            if (entry.packet is None or entry.packet == packet) and tail.startswith("attachment."):
                return True
            continue
        if entry.packet is not None and entry.packet != packet:
            continue
        if entry.sids:
            if tail in entry.sids:
                return True
            continue
        if entry.start is not None and entry.end is not None:
            key, start, end = tail_key(tail), tail_key(entry.start), tail_key(entry.end)
            if start <= key <= end:
                return True
    return False


@dataclass(frozen=True)
class SentenceIndex:
    """邮件单元要知道一句是哪一封信、哪个往来串的：从原文账本取（只读）。对话单元用不着。"""

    message: Mapping[str, str | None] = field(default_factory=dict)
    thread: Mapping[str, str | None] = field(default_factory=dict)


def in_unit(unit: GoldUnit, sid: str, sentences: SentenceIndex | None = None) -> bool | None:
    """这一句在不在这个单元里。邮件单元没有句子的索引时说不准，返回 None。"""
    packet, _ = split_sid(sid)
    if packet not in unit.packets:
        return False
    if unit.is_dialog:
        return True
    if sentences is None:
        return None
    if unit.thread_id is not None:
        return sentences.thread.get(sid) == unit.thread_id
    if unit.messages:
        return sentences.message.get(sid) in unit.messages
    return True


# ---------- 关系合成一条（D103） ----------

# 亲属的具体关系：写法归一，再分成（哪一类、主体是哪一头）。配偶、兄弟姐妹两头一样；亲子分主体是孩子还是父母
_KIN_SYNONYMS = {
    "老婆": "妻子", "太太": "妻子", "夫人": "妻子", "爱人": "配偶", "配偶": "配偶",
    "老公": "丈夫", "先生": "丈夫",
    "妈妈": "母亲", "母": "母亲", "妈": "母亲", "爸爸": "父亲", "父": "父亲", "爸": "父亲",
    "儿": "儿子", "女": "女儿", "孩子": "子女", "子女": "子女",
}
_KIN_CLASS = {
    "妻子": ("配偶", None), "丈夫": ("配偶", None), "配偶": ("配偶", None),
    "儿子": ("亲子", "child"), "女儿": ("亲子", "child"), "子女": ("亲子", "child"),
    "母亲": ("亲子", "parent"), "父亲": ("亲子", "parent"), "父母": ("亲子", "parent"),
    "哥哥": ("同胞", None), "弟弟": ("同胞", None), "姐姐": ("同胞", None), "妹妹": ("同胞", None),
    "兄弟": ("同胞", None), "姐妹": ("同胞", None),
}


def kinship(term: str | None) -> tuple[str, str | None] | None:
    """亲属的具体关系归成（哪一类，主体是孩子还是父母）。认不出的照原样当一类。"""
    if not term:
        return None
    word = re.sub(r"\s+", "", term)
    word = _KIN_SYNONYMS.get(word, word)
    return _KIN_CLASS.get(word, (word, None))


def kin_term(term: str | None) -> str | None:
    if not term:
        return None
    word = re.sub(r"\s+", "", term)
    return _KIN_SYNONYMS.get(word, word)


# 反过来写时对得上的说法：甲是乙的妻子 → 乙是甲的丈夫（统称“配偶”也行）；儿女 → 父母（父母那头是男是女看不出，哪个都行）；
# 哥哥 → 弟弟或妹妹（这一头是男是女看不出）
_SIBLINGS = {"哥哥", "弟弟", "姐姐", "妹妹", "兄弟", "姐妹"}
_KIN_INVERSE = {
    "妻子": {"丈夫", "配偶"}, "丈夫": {"妻子", "配偶"}, "配偶": {"妻子", "丈夫", "配偶"},
    "儿子": {"父亲", "母亲", "父母"}, "女儿": {"父亲", "母亲", "父母"}, "子女": {"父亲", "母亲", "父母"},
    "父亲": {"儿子", "女儿", "子女"}, "母亲": {"儿子", "女儿", "子女"}, "父母": {"儿子", "女儿", "子女"},
    "哥哥": {"弟弟", "妹妹", "兄弟", "姐妹"}, "姐姐": {"弟弟", "妹妹", "兄弟", "姐妹"},
    "弟弟": {"哥哥", "姐姐", "兄弟", "姐妹"}, "妹妹": {"哥哥", "姐姐", "兄弟", "姐妹"},
    "兄弟": _SIBLINGS, "姐妹": _SIBLINGS,
}


def kin_inverse(term: str | None) -> frozenset[str]:
    """反过来写时对得上的说法（归一过的写法）。认不出的只认它自己。"""
    word = kin_term(term)
    if not word:
        return frozenset()
    return frozenset(_KIN_INVERSE.get(word, {word}))


@dataclass(frozen=True)
class GoldRelation:
    """合起来的一条关系。key 是（主体、关系、客体）的标准名（亲属两头不分先后，key 照头一条的写法）。
    starts：算对的开始日子（最准的那个，加上另起的几段）。kins：亲属每一条怎么写的（主体, 具体关系），反过来写的也在。"""

    key: tuple[str, str, str]
    items: tuple[GoldItem, ...]
    best: When | None
    starts: tuple[When, ...]
    final_state: str | None
    kin: str | None  # 亲属的具体关系（头一条的主体相对客体）
    only_in: bool  # 每一条都是只在引用、签名、附件里的
    kins: tuple[tuple[str, str], ...] = ()

    @property
    def units(self) -> frozenset[str]:
        return frozenset(item.unit for item in self.items)

    @property
    def subject_type(self) -> str | None:
        return self.items[0].subject[1] if self.items[0].subject else None

    @property
    def object_type(self) -> str | None:
        return self.items[0].object[1] if self.items[0].object else None


def _best_start(items: Sequence[GoldItem]) -> When | None:
    """最准的开始日子：精度最高的；一样高的取说得最多的（按换成的日子数，写法不同的同一天算一个），再一样取先出现的。"""
    dated = [(item.when, interval(item.when.start, item.when.precision)) for item in items if item.when.start]
    dated = [(when, days) for when, days in dated if days is not None]
    if not dated:
        return None
    top = max(precision_rank(when.precision) for when, _ in dated)
    best = [(when, days) for when, days in dated if precision_rank(when.precision) == top]
    counts: dict[object, int] = {}
    for _, days in best:
        counts[days] = counts.get(days, 0) + 1
    most = max(counts.values())
    return next(when for when, days in best if counts[days] == most)


def gold_relations(units: Iterable[GoldUnit], *, tolerance_days: int = TOLERANCE_DAYS, order: Sequence[str] | None = None) -> list[GoldRelation]:
    """同一条关系跨单元合成一条（D103）。亲属两头不分先后（D90：反过来写的算同一条）。order 是单元的时间先后
    （最后的状态取最晚的那个单元），不给按单元的先后。"""
    groups: dict[tuple[str, str, str], list[GoldItem]] = {}
    unit_list = list(units)
    rank = {uid: number for number, uid in enumerate(order or [u.id for u in unit_list])}
    for unit in unit_list:
        for item in unit.items:
            if item.type != "relation" or item.subject is None or item.object is None or item.relation is None:
                continue
            key = (item.subject[0], item.relation, item.object[0])
            reverse = (item.object[0], item.relation, item.subject[0])
            if item.relation == "亲属" and key not in groups and reverse in groups:
                key = reverse
            groups.setdefault(key, []).append(item)
    out = []
    for key, items in groups.items():
        best = _best_start(items)
        starts: list[When] = [best] if best is not None else []
        for item in items:
            if ADDITIONAL in item.flags or interval(item.when.start, item.when.precision) is None:
                continue
            if all(close(item.when, one, tolerance_days) is False for one in starts):
                starts.append(item.when)
        latest = max(items, key=lambda item: (rank.get(item.unit, -1), item.id))
        kins = tuple((item.subject[0], item.fields["kinship"]) for item in items  # type: ignore[index]
                     if item.fields.get("kinship"))
        first_kin = next((term for subject, term in kins if subject == key[0]), None)
        out.append(GoldRelation(key, tuple(items), best, tuple(starts), latest.state, first_kin,
                                all(item.only_in for item in items), kins))
    return out


def merged_aliases(units: Iterable[GoldUnit]) -> dict[tuple[str, str, str], list[GoldItem]]:
    """别名按（写法的比对写法、标准名、种类）合起来：同一个叫法在几个单元里都标了，只考一次。"""
    from javmem.pipeline.merge import name_key

    out: dict[tuple[str, str, str], list[GoldItem]] = {}
    for unit in units:
        for item in unit.items:
            if item.type != "alias" or item.alias is None:
                continue
            key = (name_key(item.alias["mention"]), item.alias["entity"], item.alias.get("kind", "别名"))
            out.setdefault(key, []).append(item)
    return out


def sentence_text(units: Iterable[GoldUnit]) -> dict[str, str]:
    """金标准出处里带着的整句（给人看对不上的时候用）。"""
    out: dict[str, str] = {}
    for unit in units:
        for item in unit.items:
            for one in item.evidence:
                if one.sentence and one.sid not in out:
                    out[one.sid] = one.sentence
    return out


def unit_order(units: Mapping[str, GoldUnit]) -> list[str]:
    """单元按时间先后：出处里最晚的那一句说话的时间。说不出时间的排最前。"""

    def latest(unit: GoldUnit) -> str:
        times = [one.said_at for item in unit.items for one in item.evidence if one.said_at]
        return max(times) if times else ""

    return sorted(units, key=lambda uid: (latest(units[uid]), uid))
