"""对齐格式 `javmem-score/1`（J3.8）：一边的抽取结果转成这个样子再和金标准比。

javmem 自己的账本用 javmem.scoring.ledger 转。以后的对手（Graphiti 或别的，D110 还没定）照这里写一个转换，存成
JSON 文件，`javmem score --side 名字=文件.json` 就能打分，和 javmem 用同一套比法。

    {
      "format": "javmem-score/1",
      "side": "名字",
      "described": {"提示词": "…", "模型": "…"},          # 随便写，原样进报告
      "packets": ["包号", …],                              # 这一边抽过的包；包不全的单元不打分
      "revisions": {"包号": 1},                            # 可以不写：抽的是包的哪一版，和金标准标的那一版不一样的不打分
      "pending": ["包号#3.1.1"],                           # 可以不写：还没抽完的句子（所在的窗口还没结果）；单元碰上了不打分
      "entities": [{"id": "e1", "name": "…", "type": "人", "aliases": ["…"]}],
      "facts": [{"id": "f1", "text": "…", "subject": "e1", "attribute": "…", "value": "…",
                 "when": {"from": "2026-10-23", "to": null, "precision": "day"},
                 "certainty": "原话", "speaker": "…", "entities": ["e1", "e2"],
                 "evidence": ["包号#2.1.3"], "state": "现行", "superseded_by": null}],
      "relations": [{"id": "r1", "subject": "e1", "relation": "持有", "object": "e2", "fields": {"kinship": "…"},
                     "when": {"from": "2026-10-13", "to": null, "precision": "day"}, "state": "生效",
                     "certainty": "原话", "evidence": ["包号#5.2.1"]}],
      "changes": [{"id": "c1", "before": {"kind": "fact", "id": "f1"}, "after": {"kind": "fact", "id": "f2"},
                   "at": "2026-10-23T16:54+08:00", "evidence": ["包号#6.1.1"]}],
      "cost": {"per_packet": {"包号": {"calls": 3, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                                       "cached_tokens": 0, "seconds": 0.0}}}
    }

- `subject`、`object`、`entities` 写 `entities` 里的 `id`。实体的类型是七类之一（Bot 是第七类，D13）。
- `evidence` 是原文句子的编号（账本的 `包号#小节.行.句`）。一条一条记不到句子的（比如只知道是哪一段喂进去的），
  写那一段所有句子的编号。
- `when.from`、`when.to` 写 `2026-10-23`、`2026-10-23T16:54+08:00`、`2026-10`、`2026-Q4`、`2026`，不知道写 null；
  `precision` 是 minute、day、month、quarter、year、unknown 这几种；漏写了精度的，照 `from` 的写法猜。
- 关系的 `fields.kinship`（亲属）写主体是客体的什么人：甲 → 乙、kinship 写“妻子”，是说甲是乙的妻子。
- `certainty`：原话、推断、含糊，不知道写 null。事实的 `state`：现行、被替代、待确认；关系的：生效、已结束、暂停、被取代。
- `changes` 是这一边自己记下的“前后变了”。事实被替代（`superseded_by`）不用另写，打分时自己算；关系结束了、
  换了一头的，打分时也自己算（javmem.scoring.match）。
- 关系只认七种：持有、托管于、负责、任职于、客户是、亲属（D13），隶属于（D210：部门、分公司、子公司 → 它所属的那家）。
  别的写进来报错。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "javmem-score/1"
RELATION_TYPES = ("持有", "托管于", "负责", "任职于", "客户是", "亲属", "隶属于")
ENTITY_TYPES = ("人", "公司或机构", "项目", "订单或合同", "地点", "资产", "Bot")
PRECISIONS = ("minute", "day", "month", "quarter", "year", "unknown")
CERTAINTIES = ("原话", "推断", "含糊")
FACT_STATES = ("现行", "被替代", "待确认")
RELATION_STATES = ("生效", "已结束", "暂停", "被取代")
REF_KINDS = ("fact", "relation")
COST_FIELDS = ("calls", "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "seconds")


class ScoreInputError(ValueError):
    """对齐格式的文件、金标准、审阅结果写得不对。消息里只有位置和字段名，不带原文。"""


@dataclass(frozen=True)
class When:
    start: str | None = None
    end: str | None = None
    precision: str = "unknown"

    def as_dict(self) -> dict[str, object]:
        return {"from": self.start, "to": self.end, "precision": self.precision}


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    type: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    subject: str | None = None
    attribute: str | None = None
    value: str | None = None
    when: When = When()
    certainty: str | None = None
    speaker: str | None = None
    entities: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    state: str = "现行"
    superseded_by: str | None = None


@dataclass(frozen=True)
class Relation:
    id: str
    subject: str
    relation: str
    object: str
    fields: Mapping[str, str] = field(default_factory=dict)
    when: When = When()
    state: str = "生效"
    certainty: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ref:
    kind: str
    id: str


@dataclass(frozen=True)
class Change:
    id: str
    before: Ref | None
    after: Ref | None
    at: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Cost:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0

    @property
    def tokens(self) -> int:
        """输入加输出（推理的算在输出里，接口就是这么记的）。"""
        return self.input_tokens + self.output_tokens

    def plus(self, other: Cost) -> Cost:
        return Cost(
            self.calls + other.calls,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
            self.cached_tokens + other.cached_tokens,
            round(self.seconds + other.seconds, 3),
        )

    def as_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in COST_FIELDS}


@dataclass(frozen=True)
class Side:
    """一边的抽取结果。entities、facts、relations 按 id 存。"""

    name: str
    packets: frozenset[str]
    entities: Mapping[str, Entity]
    facts: Mapping[str, Fact]
    relations: Mapping[str, Relation]
    changes: tuple[Change, ...] = ()
    cost: Mapping[str, Cost] = field(default_factory=dict)
    described: Mapping[str, object] = field(default_factory=dict)
    revisions: Mapping[str, int] = field(default_factory=dict)  # 抽的是包的哪一版（可以不写）
    pending: frozenset[str] = frozenset()  # 还没抽完的句子：所在的窗口还没结果（可以不写）

    def as_dict(self) -> dict[str, object]:
        return {
            "format": FORMAT,
            "side": self.name,
            "described": dict(self.described),
            "packets": sorted(self.packets),
            "revisions": dict(sorted(self.revisions.items())),
            "pending": sorted(self.pending),
            "entities": [
                {"id": e.id, "name": e.name, "type": e.type, "aliases": list(e.aliases)} for e in self.entities.values()
            ],
            "facts": [
                {
                    "id": f.id, "text": f.text, "subject": f.subject, "attribute": f.attribute, "value": f.value,
                    "when": f.when.as_dict(), "certainty": f.certainty, "speaker": f.speaker,
                    "entities": list(f.entities), "evidence": list(f.evidence), "state": f.state,
                    "superseded_by": f.superseded_by,
                }
                for f in self.facts.values()
            ],
            "relations": [
                {
                    "id": r.id, "subject": r.subject, "relation": r.relation, "object": r.object,
                    "fields": dict(r.fields), "when": r.when.as_dict(), "state": r.state,
                    "certainty": r.certainty, "evidence": list(r.evidence),
                }
                for r in self.relations.values()
            ],
            "changes": [
                {
                    "id": c.id,
                    "before": {"kind": c.before.kind, "id": c.before.id} if c.before else None,
                    "after": {"kind": c.after.kind, "id": c.after.id} if c.after else None,
                    "at": c.at, "evidence": list(c.evidence),
                }
                for c in self.changes
            ],
            "cost": {"per_packet": {packet: cost.as_dict() for packet, cost in sorted(self.cost.items())}},
        }


# ---------- 读 ----------


def _text(value: object, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ScoreInputError(f"{where} 要是字符串")
    return value


def _choice(value: object, allowed: Sequence[str], where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if value not in allowed:
        raise ScoreInputError(f"{where} 只能是 {'、'.join(allowed)}")
    return value  # type: ignore[return-value]


def _strings(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(one, str) for one in value):
        raise ScoreInputError(f"{where} 要是字符串的列表")
    return tuple(value)


def _when(value: object, where: str) -> When:
    if value is None:
        return When()
    if not isinstance(value, dict) or set(value) - {"from", "to", "precision"}:
        raise ScoreInputError(f"{where} 要写成 {{from, to, precision}}")
    start = _text(value.get("from"), f"{where}.from", optional=True)
    if "precision" in value:
        precision = _choice(value["precision"], PRECISIONS, f"{where}.precision") or "unknown"
    else:
        from javmem.scoring.dates import infer_precision  # dates 用到 When，这里用到时才 import

        precision = infer_precision(start)
    return When(start, _text(value.get("to"), f"{where}.to", optional=True), precision)


def _ref(value: object, where: str) -> Ref | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "id"}:
        raise ScoreInputError(f"{where} 要写成 {{kind, id}} 或 null")
    return Ref(_choice(value["kind"], REF_KINDS, f"{where}.kind") or "", _text(value["id"], f"{where}.id") or "")


def _record(value: object, where: str, allowed: set[str], required: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ScoreInputError(f"{where} 要是对象")
    unknown = set(value) - allowed
    if unknown:
        raise ScoreInputError(f"{where} 有认不出的字段：{'、'.join(sorted(unknown))}")
    missing = required - set(value)
    if missing:
        raise ScoreInputError(f"{where} 少了字段：{'、'.join(sorted(missing))}")
    return value


def side_from_dict(data: object) -> Side:
    """读对齐格式，写错了报 ScoreInputError（位置、字段名，不带原文）。"""
    top = _record(data, "$", {"format", "side", "described", "packets", "revisions", "pending", "entities", "facts",
                             "relations", "changes", "cost"}, {"format", "side", "packets", "entities"})
    if top["format"] != FORMAT:
        raise ScoreInputError(f"$.format 只认 {FORMAT}")
    name = _text(top["side"], "$.side") or ""
    described = top.get("described") or {}
    if not isinstance(described, dict):
        raise ScoreInputError("$.described 要是对象")
    entities: dict[str, Entity] = {}
    for number, raw in enumerate(top.get("entities") or []):
        where = f"$.entities[{number}]"
        row = _record(raw, where, {"id", "name", "type", "aliases"}, {"id", "name", "type"})
        entity = Entity(_text(row["id"], f"{where}.id") or "", _text(row["name"], f"{where}.name") or "",
                        _choice(row["type"], ENTITY_TYPES, f"{where}.type") or "", _strings(row.get("aliases"), f"{where}.aliases"))
        if entity.id in entities:
            raise ScoreInputError(f"{where}.id 重复了")
        entities[entity.id] = entity

    def known(entity_id: object, where: str, *, optional: bool = False) -> str | None:
        if entity_id is None and optional:
            return None
        if not isinstance(entity_id, str) or entity_id not in entities:
            raise ScoreInputError(f"{where} 要写 entities 里有的 id")
        return entity_id

    facts: dict[str, Fact] = {}
    for number, raw in enumerate(top.get("facts") or []):
        where = f"$.facts[{number}]"
        row = _record(raw, where, {"id", "text", "subject", "attribute", "value", "when", "certainty", "speaker",
                                   "entities", "evidence", "state", "superseded_by"}, {"id", "text"})
        fact = Fact(
            _text(row["id"], f"{where}.id") or "",
            _text(row["text"], f"{where}.text") or "",
            known(row.get("subject"), f"{where}.subject", optional=True),
            _text(row.get("attribute"), f"{where}.attribute", optional=True),
            _text(row.get("value"), f"{where}.value", optional=True),
            _when(row.get("when"), f"{where}.when"),
            _choice(row.get("certainty"), CERTAINTIES, f"{where}.certainty", optional=True),
            _text(row.get("speaker"), f"{where}.speaker", optional=True),
            tuple(known(one, f"{where}.entities") or "" for one in _strings(row.get("entities"), f"{where}.entities")),
            _strings(row.get("evidence"), f"{where}.evidence"),
            _choice(row.get("state", "现行"), FACT_STATES, f"{where}.state") or "现行",
            _text(row.get("superseded_by"), f"{where}.superseded_by", optional=True),
        )
        if fact.id in facts:
            raise ScoreInputError(f"{where}.id 重复了")
        facts[fact.id] = fact
    for fact in facts.values():
        if fact.superseded_by is not None and fact.superseded_by not in facts:
            raise ScoreInputError("$.facts 里有 superseded_by 指着不在 facts 里的 id")
    relations: dict[str, Relation] = {}
    for number, raw in enumerate(top.get("relations") or []):
        where = f"$.relations[{number}]"
        row = _record(raw, where, {"id", "subject", "relation", "object", "fields", "when", "state", "certainty",
                                   "evidence"}, {"id", "subject", "relation", "object"})
        fields = row.get("fields") or {}
        if not isinstance(fields, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items()):
            raise ScoreInputError(f"{where}.fields 要是字符串到字符串的对象")
        relation = Relation(
            _text(row["id"], f"{where}.id") or "",
            known(row["subject"], f"{where}.subject") or "",
            _choice(row["relation"], RELATION_TYPES, f"{where}.relation") or "",
            known(row["object"], f"{where}.object") or "",
            dict(fields),
            _when(row.get("when"), f"{where}.when"),
            _choice(row.get("state", "生效"), RELATION_STATES, f"{where}.state") or "生效",
            _choice(row.get("certainty"), CERTAINTIES, f"{where}.certainty", optional=True),
            _strings(row.get("evidence"), f"{where}.evidence"),
        )
        if relation.id in relations:
            raise ScoreInputError(f"{where}.id 重复了")
        relations[relation.id] = relation
    changes = []
    for number, raw in enumerate(top.get("changes") or []):
        where = f"$.changes[{number}]"
        row = _record(raw, where, {"id", "before", "after", "at", "evidence"}, {"id"})
        change = Change(_text(row["id"], f"{where}.id") or "", _ref(row.get("before"), f"{where}.before"),
                        _ref(row.get("after"), f"{where}.after"), _text(row.get("at"), f"{where}.at", optional=True),
                        _strings(row.get("evidence"), f"{where}.evidence"))
        for side_ref in (change.before, change.after):
            if side_ref is not None and side_ref.id not in (facts if side_ref.kind == "fact" else relations):
                raise ScoreInputError(f"{where} 指着不在 {side_ref.kind}s 里的 id")
        changes.append(change)
    cost: dict[str, Cost] = {}
    raw_cost = top.get("cost") or {}
    if not isinstance(raw_cost, dict) or set(raw_cost) - {"per_packet"}:
        raise ScoreInputError("$.cost 要写成 {per_packet: {包号: {...}}}")
    per_packet = raw_cost.get("per_packet") or {}
    if not isinstance(per_packet, dict):
        raise ScoreInputError("$.cost.per_packet 要是对象")
    for packet, numbers in per_packet.items():
        where = "$.cost.per_packet 里的一包"
        if not isinstance(numbers, dict) or set(numbers) - set(COST_FIELDS):
            raise ScoreInputError(f"{where} 只能有 {'、'.join(COST_FIELDS)}")
        if not all(isinstance(numbers.get(name, 0), (int, float)) and not isinstance(numbers.get(name, 0), bool)
                   for name in COST_FIELDS):
            raise ScoreInputError(f"{where} 的数要是数字")
        cost[packet] = Cost(*(int(numbers.get(name, 0)) for name in COST_FIELDS[:-1]), float(numbers.get("seconds", 0.0)))
    revisions = top.get("revisions") or {}
    if not isinstance(revisions, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) for k, v in revisions.items()
    ):
        raise ScoreInputError("$.revisions 要写成 {包号: 版本号}")
    pending = frozenset(_strings(top.get("pending") or [], "$.pending"))
    return Side(name, frozenset(_strings(top["packets"], "$.packets")), entities, facts, relations, tuple(changes),
                cost, dict(described), dict(revisions), pending)


def load_side(path: Path) -> Side:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScoreInputError(f"对齐格式的文件读不了：{type(error).__name__}") from None
    return side_from_dict(data)


def dump_side(side: Side, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(side.as_dict(), ensure_ascii=False, indent=1) + "\n"
    path.write_bytes(text.encode("utf-8"))
