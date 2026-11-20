"""账本 → 图里该有什么（J5.2）。进不进、带哪些字段，规矩都在这里；同步（javmem.graph.sync）照它算、照它比指纹。

**原句不进图**（主人定的）：证据只在账本里，查询接口要原句、说话人、位置时按编号回账本拿。图里只有编号和内容。

进不进：

- 事实、关系：没撤回、状态是核对过（verified）或推断（inferred）的才进；推断的进、标出来（D19）。已结束、被替代的照样进，
  带着失效时间、被谁替代。待确认（pending）的也不放进来，以后查询接口从账本视图拿；隔离表里的本来就不在这两张表里。
  进之前还要过一道进图前检查（javmem.graph.gate，J5.3 的插口）。
- 别名：没撤回、状态是核对过或推断，出处所在的包是现行的、没撤回、没彻底删除的才算；一个别名的可见范围是这些出处的包的
  范围合起来。这个实体在某个范围里有没处理的“实体有歧义”（待确认），这个范围的出处先不算。一个范围都不剩就不进。
- 实体：至少连着一条进了图的事实、关系（主体、客体、代表哪一家）或别名才进；可见范围是这些东西的范围合起来
  （query-api 3.2：一个实体至少有一条看得到的事实、关系或别名，它就看得到）。这一样同步那边按数数的表算。

可见范围：一个范围一个编号（S 加十二位，从账本的范围写法算，整本重建也不变）。事实、关系各只有一个（D30：不同范围的不
合并），别名、实体可能有几个。scope_text 是范围编号连起来（小写），进全文索引，J5.4 可以在全文检索里只搜看得到的那几种。

看不到的名字不能让人搜到（query-api 3.2“过滤在检索之内”，审阅第 3 条）：

- 实体的标准名只带“标准名看得到的范围”（name_scope_ids、name_scope_text：别名里和标准名一样写法的那一条，出处在哪几个
  范围），实体名的全文索引连的是它，不是实体的 scope_text。标准名哪个范围都没有出处的（别名表定的、L3 补全的），
  name_scope_text 是空的：谁看得到它 J5.5 定。按名字找实体走别名（每个别名带着自己的范围）。
- 别名“最早记下的先后”分范围记（first_seen_by_scope，和 scope_ids 一一对上），按身份看得到的范围取。
- 关系的内容那一句照账本给主人看的写法（javmem.review）：“主体 关系 客体”，亲属带具体说法。名字用这条关系所在的范围里
  看得到的叫法：标准名在这个范围里有出处就用标准名，没有就用这个范围里最早记下的别名；这个范围里一个别名都没有的，
  标准名哪个范围都没有出处才用标准名，标准名只在别的范围有出处的写成“〔类型〕”。

改了这里的规矩（多放、少放一个字段这种），PROJECTION_VERSION 加一：同步认出来，每一条都重算一遍。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo

from javmem.pipeline.merge import name_key, scope_key
from javmem.pipeline.normalize import Scope
from javmem.pipeline.vectors import text_sha256
from javmem.pipeline.verify import parse_occurred

PROJECTION_VERSION = "g3"  # g2：实体的标准名只带看得到它的范围、别名最早记下的分范围（审阅第 3 条）；g3：标准名照现在的写法算、不放旧写法
SHOWN = ("verified", "inferred")  # 进图的状态（待确认的那一种不放进来）
CHUNK = 500  # 一次按编号查多少条（SQLite 一句的参数有上限）


@dataclass(frozen=True)
class ScopeInfo:
    scope_id: str
    partitions: tuple[str, ...]
    sensitive: tuple[str, ...]


def scope_info(partitions: str, sensitive: str) -> ScopeInfo:
    """账本里的范围（两列 JSON 列表）→ 范围编号。和 L5 合并用的同一个写法（javmem.pipeline.merge.scope_key）。"""
    scope = Scope(tuple(sorted(set(json.loads(partitions)))), tuple(sorted(set(json.loads(sensitive or "[]")))))
    return scope_of(scope)


def scope_of(scope: Scope) -> ScopeInfo:
    digest = hashlib.sha256(("javmem-scope\n" + scope_key(scope)).encode("utf-8")).hexdigest()
    return ScopeInfo("S" + digest[:12], scope.partitions, scope.sensitive)


def scope_text(scope_ids: Iterable[str]) -> str:
    return " ".join(sorted({scope_id.lower() for scope_id in scope_ids}))


@dataclass
class Item:
    """图里该有的一样东西。props 里写成 None 的，图里就不该有这个属性。"""

    kind: str
    item_id: str
    props: dict[str, object]
    links: tuple[str, ...] = ()  # 连着哪几个实体
    scopes: tuple[str, ...] = ()  # 可见范围的编号
    involves: tuple[tuple[str, str], ...] = ()  # 事：(实体, 角色)
    subject: str | None = None  # 关系的两头
    object: str | None = None
    entity: str | None = None  # 别名的实体
    vector: bytes | None = None  # 事：账本里的向量；关系：同步时补
    vector_text: str | None = None  # 关系：拿哪一句算向量

    def fingerprint(self, gate_version: str = "") -> str:
        """所有字段算一个 sha256（不含向量，向量另算）。gate_version 是进图前检查的版本：换了版本，指纹都变。"""
        body = {
            "kind": self.kind,
            "props": self.props,
            "links": list(self.links),
            "scopes": list(self.scopes),
            "involves": [list(pair) for pair in self.involves],
            "subject": self.subject,
            "object": self.object,
            "entity": self.entity,
            "projection": PROJECTION_VERSION,
            "gate": gate_version,
        }
        text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def vector_sha(blob: bytes | None) -> str | None:
    return hashlib.sha256(blob).hexdigest() if blob is not None else None


def chunks(items: Sequence[str], size: int = CHUNK) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _marks(count: int) -> str:
    return ", ".join("?" * count)


# ---------- 时间：照精度换成起止两个时刻 ----------


def time_bounds(text: str | None, precision: str | None, tz: tzinfo) -> tuple[int | None, int | None]:
    """一个照精度写的时间（2026-11-09、2026-11、2026-Q4、2026、带时区的到分钟）→ 它覆盖的那一段的起止时刻（epoch 秒，
    含起不含止）。日子、月、季、年按 tz（JAVMEM_TIMEZONE）的零点算；到分钟的照它自己的时区。不知道、认不出是 (None, None)。"""
    if not text or precision in (None, "unknown"):
        return None, None
    parsed = parse_occurred(text)
    if parsed is None:
        return None, None
    try:
        if parsed.precision == "minute":
            zone = tz if parsed.offset is None else _offset_zone(parsed.offset)
            start = datetime(parsed.year, parsed.month or 1, parsed.day or 1, parsed.hour or 0, parsed.minute or 0, tzinfo=zone)
            return int(start.timestamp()), int(start.timestamp()) + 60
        if parsed.precision == "day":
            day = datetime(parsed.year, parsed.month or 1, parsed.day or 1)
            start = datetime.combine(day.date(), time(), tzinfo=tz)
            return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())
        if parsed.precision == "month":
            month = parsed.month or 1
            start = datetime(parsed.year, month, 1, tzinfo=tz)
            end = datetime(parsed.year + month // 12, month % 12 + 1, 1, tzinfo=tz)
            return int(start.timestamp()), int(end.timestamp())
        if parsed.precision == "quarter":
            quarter = parsed.quarter or ((parsed.month or 1) - 1) // 3 + 1
            start = datetime(parsed.year, 3 * quarter - 2, 1, tzinfo=tz)
            end = datetime(parsed.year + quarter // 4, (3 * quarter) % 12 + 1, 1, tzinfo=tz)
            return int(start.timestamp()), int(end.timestamp())
        start = datetime(parsed.year, 1, 1, tzinfo=tz)
        return int(start.timestamp()), int(datetime(parsed.year + 1, 1, 1, tzinfo=tz).timestamp())
    except (OverflowError, ValueError, OSError):
        return None, None


def _offset_zone(offset: timedelta) -> tzinfo:
    from datetime import timezone

    return timezone(offset)


# ---------- 账本 → 图 ----------


@dataclass
class Projector:
    """在一个账本连接上算“图里该有什么”。一次同步用一个（缓存了没处理的歧义、名字），账本变了另建一个。"""

    conn: sqlite3.Connection
    tz: tzinfo
    scopes: dict[str, ScopeInfo] = field(default_factory=dict, init=False, repr=False)  # 算到过的范围：编号 → 分区、敏感标记
    _ambiguity: dict[str, set[str] | None] | None = field(default=None, init=False, repr=False)
    _names: dict[str, list[tuple[str, int, str, str]]] = field(default_factory=dict, init=False, repr=False)
    _entities: dict[str, tuple[str, str | None, str]] = field(default_factory=dict, init=False, repr=False)

    def _scope(self, partitions: str, sensitive: str) -> ScopeInfo:
        info = scope_info(partitions, sensitive)
        self.scopes.setdefault(info.scope_id, info)
        return info

    # ---------- 没处理的“实体有歧义” ----------

    def ambiguity(self) -> dict[str, set[str] | None]:
        """实体 → 有没处理的歧义的那几个范围的编号；None 是没记范围的旧条目，哪个范围都算。"""
        if self._ambiguity is None:
            found: dict[str, set[str] | None] = {}
            rows = self.conn.execute(
                "SELECT json_extract(claims, '$.entity_id') AS entity_id, json_extract(claims, '$.scope') AS scope"
                " FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL AND json_valid(claims)"
            ).fetchall()
            for row in rows:
                entity_id = row[0]
                if not isinstance(entity_id, str):
                    continue
                scope = row[1]
                if found.get(entity_id, set()) is None:
                    continue
                if not isinstance(scope, str):
                    found[entity_id] = None
                    continue
                try:
                    parsed = json.loads(scope)
                    info = scope_info(json.dumps(parsed.get("partitions", [])), json.dumps(parsed.get("sensitive", [])))
                except (ValueError, AttributeError, TypeError):
                    found[entity_id] = None
                    continue
                current = found.setdefault(entity_id, set())
                assert current is not None
                current.add(info.scope_id)
            self._ambiguity = found
        return self._ambiguity

    def _ambiguous(self, entity_id: str, scope_id: str) -> bool:
        if entity_id not in self.ambiguity():
            return False
        scopes = self.ambiguity()[entity_id]
        return scopes is None or scope_id in scopes

    # ---------- 实体的名字 ----------

    def _load_entities(self, ids: Iterable[str]) -> None:
        missing = sorted({one for one in ids if one and one not in self._entities})
        for part in chunks(missing):
            for row in self.conn.execute(
                f"SELECT entity_id, canonical_name, type FROM entities WHERE entity_id IN ({_marks(len(part))})", part
            ):
                # 对齐用的写法照现在的标准名算：撤回让标准名改了名时，账本里的 name_key 还是旧名字的（第二遍审阅第 1 条）
                self._entities[row[0]] = (row[1], name_key(row[1]), row[2])

    def _load_names(self, ids: Iterable[str]) -> None:
        """这些实体的别名在哪几个范围里有出处：(范围编号, 最早那条出处的先后, 叫法, 对齐用的写法)。"""
        missing = sorted({one for one in ids if one and one not in self._names})
        for one in missing:
            self._names[one] = []
        for part in chunks(missing):
            rows = self.conn.execute(
                "SELECT a.entity_id, a.alias, a.alias_key, p.partitions, p.sensitive, min(v.evidence_id) AS first"
                " FROM aliases AS a JOIN evidence AS v ON v.alias_id = a.alias_id"
                " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision"
                f" WHERE a.entity_id IN ({_marks(len(part))}) AND a.withdrawn_at IS NULL"
                " AND a.status IN ('verified', 'inferred') AND p.state = 'current' AND p.withdrawn_at IS NULL"
                " AND p.purged_at IS NULL GROUP BY a.alias_id, p.partitions, p.sensitive",
                part,
            ).fetchall()
            for row in rows:
                scope = self._scope(row["partitions"], row["sensitive"])
                if self._ambiguous(row["entity_id"], scope.scope_id):
                    continue
                self._names[row["entity_id"]].append((scope.scope_id, int(row["first"]), row["alias"], row["alias_key"] or ""))

    def name_scopes(self, entity_id: str) -> list[str]:
        """标准名在哪几个范围里有出处（别名里有和标准名一样写法的那一条，出处在这些范围里）。"""
        self._load_entities([entity_id])
        self._load_names([entity_id])
        _, key, _ = self._entities.get(entity_id, (None, None, ""))
        if not key:
            return []
        return sorted({scope for scope, _, _, alias_key in self._names.get(entity_id, []) if alias_key == key})

    def name_in_scope(self, entity_id: str | None, scope_id: str) -> str | None:
        """这个实体在这个范围里看得到的叫法：
        1. 标准名在这个范围里有出处，用标准名；
        2. 没有，用这个范围里最早记下的别名；
        3. 这个范围里一个别名都没有：标准名哪个范围都没有出处的（别名表定的、L3 补全的），照旧用标准名（谁看得到它，
           J5.5 定）；标准名只在别的范围有出处的不用（看不到的名字不能进这个范围的内容、全文和向量：审阅第 3 条），
           写成“〔类型〕”。"""
        if entity_id is None:
            return None
        self._load_entities([entity_id])
        self._load_names([entity_id])
        canonical, key, kind = self._entities.get(entity_id, (None, None, ""))
        names = self._names.get(entity_id, [])
        here = sorted((first, alias, alias_key) for scope, first, alias, alias_key in names if scope == scope_id)
        if canonical is not None and key and any(alias_key == key for _, _, alias_key in here):
            return canonical
        if here:
            return here[0][1]
        if canonical is not None and key and any(alias_key == key for _, _, _, alias_key in names):
            return f"〔{kind}〕"
        return canonical

    # ---------- 事实 ----------

    def facts(self, ids: Sequence[str]) -> dict[str, Item]:
        """这些编号里该进图的事实（不该进的不在结果里）。还没过进图前检查。"""
        found: dict[str, Item] = {}
        for part in chunks(sorted(set(ids))):
            marks = _marks(len(part))
            rows = self.conn.execute(
                "SELECT fact_id, content, subject_entity_id, attribute, value, occurred_from, occurred_to, time_precision,"
                " status, frozen, superseded_by, superseded_at, secondhand, stated, owner_said, sources, origins,"
                " attributed_to, speaker_role, as_of_epoch, scope_partitions, scope_sensitive"
                f" FROM facts WHERE fact_id IN ({marks}) AND withdrawn_at IS NULL AND status IN ('verified', 'inferred')",
                part,
            ).fetchall()
            if not rows:
                continue
            kept = [row["fact_id"] for row in rows]
            keep_marks = _marks(len(kept))
            involves: dict[str, set[tuple[str, str]]] = {one: set() for one in kept}
            for row in self.conn.execute(
                f"SELECT fact_id, entity_id, role FROM fact_entities WHERE fact_id IN ({keep_marks})", kept
            ):
                involves[row[0]].add((row[1], row[2]))
            first = dict(self.conn.execute(
                f"SELECT fact_id, min(said_from_epoch) FROM fact_claims WHERE fact_id IN ({keep_marks}) GROUP BY fact_id", kept
            ).fetchall())
            vectors = {row[0]: (row[1], row[2]) for row in self.conn.execute(
                f"SELECT fact_id, text_sha256, vector FROM fact_vectors WHERE fact_id IN ({keep_marks})", kept
            )}
            replaces: dict[str, list[str]] = {}
            for row in self.conn.execute(
                f"SELECT superseded_by, fact_id FROM facts WHERE superseded_by IN ({keep_marks}) AND withdrawn_at IS NULL"
                " AND status IN ('verified', 'inferred')",
                kept,
            ):
                replaces.setdefault(row[0], []).append(row[1])
            for row in rows:
                fact_id = row["fact_id"]
                scope = self._scope(row["scope_partitions"], row["scope_sensitive"])
                from_lo, from_hi = time_bounds(row["occurred_from"], row["time_precision"], self.tz)
                to_lo, to_hi = time_bounds(row["occurred_to"], row["time_precision"], self.tz)
                said = first.get(fact_id)
                said = int(said) if said is not None else row["as_of_epoch"]
                vector = vectors.get(fact_id)
                blob = vector[1] if vector is not None and vector[0] == text_sha256(row["content"]) else None
                pairs = tuple(sorted(involves[fact_id]))
                props: dict[str, object] = {
                    "content": row["content"],
                    "subject_id": row["subject_entity_id"],
                    "attribute": row["attribute"],
                    "value": row["value"],
                    "occurred_from": row["occurred_from"],
                    "occurred_to": row["occurred_to"],
                    "precision": row["time_precision"],
                    "from_lo": from_lo,
                    "from_hi": from_hi,
                    "to_lo": to_lo,
                    "to_hi": to_hi,
                    "status": row["status"],
                    "inferred": row["status"] == "inferred",
                    "frozen": bool(row["frozen"]),
                    "superseded_by": row["superseded_by"],
                    "superseded_at": row["superseded_at"],
                    "replaces": sorted(replaces.get(fact_id, [])),
                    "secondhand": bool(row["secondhand"]),
                    "stated": bool(row["stated"]),
                    "owner_said": bool(row["owner_said"]),
                    "sources": sorted(json.loads(row["sources"] or "[]")),
                    "origins": sorted(json.loads(row["origins"] or "[]")),
                    "attributed_to": row["attributed_to"],
                    "speaker_role": row["speaker_role"],
                    "first_said_epoch": said,
                    "as_of_epoch": row["as_of_epoch"],
                    "sort_epoch": from_lo if from_lo is not None else said,
                    "scope_id": scope.scope_id,
                    "partitions": list(scope.partitions),
                    "sensitive": list(scope.sensitive),
                    "scope_text": scope_text([scope.scope_id]),
                }
                found[fact_id] = Item(
                    "fact", fact_id, props, links=tuple(sorted({entity for entity, _ in pairs})), scopes=(scope.scope_id,),
                    involves=pairs, vector=blob,
                )
        return found

    # ---------- 关系 ----------

    def relations(self, ids: Sequence[str]) -> dict[str, Item]:
        found: dict[str, Item] = {}
        for part in chunks(sorted(set(ids))):
            rows = self.conn.execute(
                f"SELECT * FROM relations WHERE relation_id IN ({_marks(len(part))}) AND withdrawn_at IS NULL"
                " AND status IN ('verified', 'inferred')",
                part,
            ).fetchall()
            if not rows:
                continue
            kept = [row["relation_id"] for row in rows]
            keep_marks = _marks(len(kept))
            first = dict(self.conn.execute(
                f"SELECT relation_id, min(said_from_epoch) FROM relation_claims WHERE relation_id IN ({keep_marks})"
                " GROUP BY relation_id",
                kept,
            ).fetchall())
            replaces: dict[str, list[str]] = {}
            for row in self.conn.execute(
                f"SELECT superseded_by, relation_id FROM relations WHERE superseded_by IN ({keep_marks})"
                " AND withdrawn_at IS NULL AND status IN ('verified', 'inferred')",
                kept,
            ):
                replaces.setdefault(row[0], []).append(row[1])
            ends = {one for row in rows for one in (row["subject_entity_id"], row["object_entity_id"], row["org_entity_id"]) if one}
            self._load_entities(ends)
            self._load_names(ends)
            for row in rows:
                found[row["relation_id"]] = self._relation(row, first, replaces)
        return found

    def _relation(self, row: sqlite3.Row, first: Mapping[str, int], replaces: Mapping[str, list[str]]) -> Item:
        relation_id = row["relation_id"]
        scope = self._scope(row["scope_partitions"], row["scope_sensitive"])
        subject, obj, org = row["subject_entity_id"], row["object_entity_id"], row["org_entity_id"]
        subject_name = self.name_in_scope(subject, scope.scope_id) or subject
        object_name = self.name_in_scope(obj, scope.scope_id) or obj
        org_name = self.name_in_scope(org, scope.scope_id) if org else None
        kind = f"{row['relation_type']}（{row['kinship']}）" if row["kinship"] else row["relation_type"]
        content = f"{subject_name} {kind} {object_name}"
        details = "；".join(
            f"{label}：{value}"
            for label, value in (
                ("职位", row["position"]), ("部门", row["department"]), ("角色", row["role"]), ("代表", org_name),
                ("服务", row["service"]), ("账户", row["account"]),
            )
            if value
        ) or None
        from_lo, from_hi = time_bounds(row["valid_from"], row["valid_from_precision"], self.tz)
        to_lo, to_hi = time_bounds(row["valid_to"], row["valid_to_precision"], self.tz)
        paused_lo, _ = time_bounds(row["paused_at"], row["paused_at_precision"], self.tz)
        said = first.get(relation_id)
        said = int(said) if said is not None else row["as_of_epoch"]
        props: dict[str, object] = {
            "type": row["relation_type"],
            "kinship": row["kinship"],
            "account": row["account"],
            "role": row["role"],
            "org_id": org,
            "position": row["position"],
            "department": row["department"],
            "service": row["service"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "from_precision": row["valid_from_precision"],
            "to_precision": row["valid_to_precision"],
            "from_basis": row["valid_from_basis"],
            "to_basis": row["valid_to_basis"],
            "from_lo": from_lo,
            "from_hi": from_hi,
            "to_lo": to_lo,
            "to_hi": to_hi,
            "state": row["state"],
            "pause_condition": row["pause_condition"],
            "paused_at": row["paused_at"],
            "paused_precision": row["paused_at_precision"],
            "paused_epoch": paused_lo,
            "superseded_by": row["superseded_by"],
            "superseded_at": row["superseded_at"],
            "replaces": sorted(replaces.get(relation_id, [])),
            "corrected_at": row["corrected_at"],
            "status": row["status"],
            "inferred": row["status"] == "inferred",
            "frozen": bool(row["frozen"]),
            "secondhand": bool(row["secondhand"]),
            "stated": bool(row["stated"]),
            "owner_said": bool(row["owner_said"]),
            "sources": sorted(json.loads(row["sources"] or "[]")),
            "origins": sorted(json.loads(row["origins"] or "[]")),
            "attributed_to": row["attributed_to"],
            "speaker_role": row["speaker_role"],
            "first_said_epoch": said,
            "as_of_epoch": row["as_of_epoch"],
            "sort_epoch": from_lo if from_lo is not None else said,
            "scope_id": scope.scope_id,
            "partitions": list(scope.partitions),
            "sensitive": list(scope.sensitive),
            "scope_text": scope_text([scope.scope_id]),
            "subject_id": subject,
            "object_id": obj,
            "subject_name": subject_name,
            "object_name": object_name,
            "content": content,
            "details": details,
        }
        return Item(
            "relation", relation_id, props, links=tuple(sorted({one for one in (subject, obj, org) if one})),
            scopes=(scope.scope_id,), subject=subject, object=obj,
            vector_text=content if details is None else f"{content}（{details}）",
        )

    # ---------- 别名 ----------

    def aliases(self, ids: Sequence[str]) -> dict[str, Item]:
        found: dict[str, Item] = {}
        for part in chunks(sorted(set(ids))):
            marks = _marks(len(part))
            rows = self.conn.execute(
                "SELECT a.alias_id, a.entity_id, a.alias, a.alias_key, a.status, e.canonical_name"
                " FROM aliases AS a JOIN entities AS e ON e.entity_id = a.entity_id"
                f" WHERE a.alias_id IN ({marks}) AND a.withdrawn_at IS NULL AND a.status IN ('verified', 'inferred')",
                part,
            ).fetchall()
            if not rows:
                continue
            kept = [row["alias_id"] for row in rows]
            scopes: dict[str, dict[str, int]] = {one: {} for one in kept}
            for row in self.conn.execute(
                "SELECT v.alias_id, p.partitions, p.sensitive, min(v.evidence_id) AS first FROM evidence AS v"
                " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision"
                f" WHERE v.alias_id IN ({_marks(len(kept))}) AND p.state = 'current' AND p.withdrawn_at IS NULL"
                " AND p.purged_at IS NULL GROUP BY v.alias_id, p.partitions, p.sensitive",
                kept,
            ):
                scope = self._scope(row["partitions"], row["sensitive"])
                held = scopes[row["alias_id"]]
                held[scope.scope_id] = min(int(row["first"]), held.get(scope.scope_id, int(row["first"])))
            for row in rows:
                alias_id, entity_id = row["alias_id"], row["entity_id"]
                usable = {scope: first for scope, first in scopes[alias_id].items() if not self._ambiguous(entity_id, scope)}
                if not usable:
                    continue
                ids_here = tuple(sorted(usable))
                props: dict[str, object] = {
                    "name": row["alias"],
                    "key": row["alias_key"],
                    "status": row["status"],
                    "canonical": bool(row["alias_key"]) and row["alias_key"] == name_key(row["canonical_name"]),
                    "entity_id": entity_id,
                    "scope_ids": list(ids_here),
                    "scope_text": scope_text(ids_here),
                    # 每个范围里最早那条出处的先后（和 scope_ids 一一对上）：按身份看得到的那几个范围取最早的，
                    # 不让看不到的范围影响先后（审阅第 3 条）
                    "first_seen_by_scope": [usable[scope] for scope in ids_here],
                }
                found[alias_id] = Item("alias", alias_id, props, links=(entity_id,), scopes=ids_here, entity=entity_id)
        return found

    # ---------- 实体（进不进、可见范围按图里连着它的数，见 sync） ----------

    def entities(self, ids: Sequence[str]) -> dict[str, dict[str, object]]:
        """这些实体在账本里的样子：标准名、对齐写法、类型、合并以前的旧编号，标准名在哪几个范围看得到
        （name_scope_ids、name_scope_text：实体名的全文索引只带这个，看不到标准名的范围搜不到它，审阅第 3 条）。
        账本里没有了的不在结果里。"""
        found: dict[str, dict[str, object]] = {}
        self._load_names(ids)
        for part in chunks(sorted(set(ids))):
            marks = _marks(len(part))
            merged: dict[str, list[str]] = {}
            for row in self.conn.execute(f"SELECT new_id, old_id FROM entity_redirects WHERE new_id IN ({marks})", part):
                merged.setdefault(row[0], []).append(row[1])
            for row in self.conn.execute(
                f"SELECT entity_id, canonical_name, name_key, type FROM entities WHERE entity_id IN ({marks})", part
            ):
                self._entities.setdefault(row["entity_id"], (row["canonical_name"], name_key(row["canonical_name"]), row["type"]))
                name_scopes = self.name_scopes(row["entity_id"])
                found[row["entity_id"]] = {  # 对齐用的写法不放进图：按写法精确找走别名（每个别名带着自己的范围）
                    "name": row["canonical_name"],
                    "type": row["type"],
                    "merged_from": sorted(merged.get(row["entity_id"], [])),
                    "name_scope_ids": name_scopes,
                    "name_scope_text": scope_text(name_scopes),
                }
        return found

    # ---------- 扩：一条变了，还有哪些要跟着重算 ----------

    def aliases_of_packets(self, packet_ids: Sequence[str]) -> set[str]:
        found: set[str] = set()
        for part in chunks(sorted(set(packet_ids))):
            found.update(row[0] for row in self.conn.execute(
                f"SELECT DISTINCT alias_id FROM evidence WHERE packet_id IN ({_marks(len(part))}) AND alias_id IS NOT NULL", part
            ))
        return found

    def entities_of(self, kind: str, ids: Sequence[str]) -> set[str]:
        """账本里这些事、关系、别名现在连着的实体。"""
        found: set[str] = set()
        for part in chunks(sorted(set(ids))):
            marks = _marks(len(part))
            if kind == "fact":
                found.update(row[0] for row in self.conn.execute(
                    f"SELECT entity_id FROM fact_entities WHERE fact_id IN ({marks})", part))
            elif kind == "relation":
                for row in self.conn.execute(
                    f"SELECT subject_entity_id, object_entity_id, org_entity_id FROM relations WHERE relation_id IN ({marks})", part
                ):
                    found.update(one for one in row if one)
            elif kind == "alias":
                found.update(row[0] for row in self.conn.execute(f"SELECT entity_id FROM aliases WHERE alias_id IN ({marks})", part))
        return found

    def touching(self, entity_ids: Sequence[str]) -> tuple[set[str], set[str]]:
        """名字可能变了的实体：连着它的关系（内容里有名字）、它的别名（是不是标准名那个写法、歧义）。"""
        relations: set[str] = set()
        aliases: set[str] = set()
        for part in chunks(sorted(set(entity_ids))):
            marks = _marks(len(part))
            for column in ("subject_entity_id", "object_entity_id", "org_entity_id"):
                relations.update(row[0] for row in self.conn.execute(
                    f"SELECT relation_id FROM relations WHERE {column} IN ({marks})", part))
            aliases.update(row[0] for row in self.conn.execute(f"SELECT alias_id FROM aliases WHERE entity_id IN ({marks})", part))
        return relations, aliases

    def all_ids(self, kind: str) -> list[str]:
        table, column = {"fact": ("facts", "fact_id"), "relation": ("relations", "relation_id"),
                         "alias": ("aliases", "alias_id"), "entity": ("entities", "entity_id")}[kind]
        return [row[0] for row in self.conn.execute(f"SELECT {column} FROM {table} ORDER BY {column}")]
