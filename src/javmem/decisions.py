"""冻结标记和主人的决定：整本重建前导出，重建后导回（D73 第 7 条，J4.4 改 L5 之前要有）。管理命令，只给主人和 CC。

冻结标记（facts.frozen）和待确认队列（review_queue）里主人定过的、30 天自动关掉的，都不在包里，整本重建
（改了 L5 的规则、换了抽取的提示词）以后就没了。事实的编号按内容算（可见范围、主体、属性和值，或者事实文字），
L5 改了算法、抽取写的文字变了，编号也会变，所以搬的时候要重新认事实。

导出（export_decisions，`javmem review --library 库 --export 文件`）：冻结的事实、待确认里提到的事实，每一条连同它的
样子（文字、主体、属性、值、可见范围、状态、时间……）和证据的指纹；待确认队列的每一行（定过的、没处理的）原样，
连同提到的实体的名字。文件里有事实的原文，写到仓库外。

导回（import_decisions，`--import 文件`）：新账本整本重建、写完账以后导，一个事务，先拿收件锁和写账的锁。

1. 认事实。编号一样的就是它。编号对不上的按证据认：同一个可见范围、证据里有一句指纹一样（说话人加原句），再看
   属性和值一样（没有属性和值的看事实文字），主体也一样的只剩一条就是它；主体换了（L5 合并了实体）、只剩一条
   属性和值一样的也是它。别的都算认不出来，不猜。
2. 认不出来的冻结事实照旧样子补回来：编号、文字、主体、状态、藏没藏都照旧，没有说法（和以前替换、撤回以后的冻结
   事实一样，算主人担保）；证据按指纹对到新账本里这个包现在那一版的句子上（撤回了的不对），对不上、那一版又不在
   新账本里的证据不要。认不出来的不冻结的事实（冲突的新说法那一方这类）不补。
3. 认出来的冻结事实标上冻结；它在旧账本里藏着（证据都撤回了）、新账本里又有说法撑着的，放出来，那条
   “证据都撤回了”的待确认记成 resupported（和写账时自动放回来一样）。
4. 待确认按原来的先后照搬，定的时间、谁定的、建的时间都不变：和冻结的冲突、证据都撤回了的冻结事实，按认出来的
   编号写回去；实体有歧义的，新账本里同一个实体、同一个可见范围有没处理的就照原来定的定掉（这个实体的事实跟着
   重算），没有的记下这条决定（以后再出现不再问）。两边有一边认不出来的、两边成了同一条的、还没有命令能处理的
   那几种（软变化、第二证人、转人工）不搬，报出来。
5. 这些事实所在的组按冻结和决定重算一遍新旧（_supersede）：没定的冲突重新进队列。旧账本里没处理的、新账本里
   重算出来了的，建的时间照旧（30 天的钟不重新算）。

新账本里已经有冻结标记或者定过的决定，不导：导进来的会和它们搅在一起。导过一次再导也是这样。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field

from javmem.config import ConfigError, Settings
from javmem.intake import intake_lock
from javmem.ledger import open_ledger, transaction
from javmem.pipeline import correct
from javmem.pipeline.merge import entity_id_of, group_key, key_text, name_key, relation_identity
from javmem.pipeline.write import (
    DROP,
    EVIDENCE_WITHDRAWN,
    RESUPPORTED,
    SAME_TIME,
    LedgerWriter,
    WriteLock,
    _json,
    _position,
    _scope_of_row,
)
from javmem.raw.index import covered, sentence_hash
from javmem.raw.sentences import MASKED
from javmem.review import ReviewError
from javmem.timeutil import iso

try:  # J2.14：值只是一个金额或日期的按统一写法比
    from javmem.pipeline.merge import value_text
except ImportError:  # pragma: no cover
    value_text = None  # type: ignore[assignment]

SCHEMA = "javmem-owner-decisions/1"
_FACT_COLUMNS = (
    "content", "attribute", "value", "occurred_from", "occurred_to", "time_precision", "status", "pending_reason",
    "speaker", "speaker_role", "withdrawn_at", "merge_key", "group_key", "as_of", "as_of_epoch", "superseded_by",
    "superseded_at", "extracted_by", "rule_version", "secondhand", "stated", "owner_said", "created_at", "updated_at",
    "attributed_to",  # J4.1：这条说法出自谁（L3 写的）。以前导出的文件里没有，导回时当空的
)
_EVIDENCE_COLUMNS = (
    "packet_id", "revision", "sentence_id", "sentence_hash", "message_id", "quote", "source", "origin", "speaker_role",
)
_RELATION_COLUMNS = (  # 关系（J4.4）
    "relation_type", "kinship", "account", "role", "position", "department", "service", "valid_from", "valid_to",
    "valid_from_precision", "valid_to_precision", "valid_from_basis", "valid_to_basis", "state", "pause_condition",
    "superseded_by", "superseded_at", "status", "pending_reason", "speaker", "speaker_role", "attributed_to", "merge_key",
    "group_key", "identity_key", "as_of", "as_of_epoch", "extracted_by", "rule_version", "secondhand", "stated",
    "owner_said", "withdrawn_at", "created_at", "updated_at",
)


@dataclass
class ImportReport:
    """只有编号和数，不抄原文。"""

    library: str
    frozen: int = 0  # 标上冻结的事实
    same_id: int = 0  # 编号一样、直接认出来的（冻结的和决定里提到的都算）
    by_evidence: dict[str, str] = field(default_factory=dict)  # 按证据认出来的：旧编号 → 新编号
    recreated: list[str] = field(default_factory=list)  # 认不出来、照旧样子补回来的冻结事实
    evidence_dropped: int = 0  # 补回来的事实里，对不上新账本句子的证据（撤回了的也算）
    hidden: list[str] = field(default_factory=list)  # 补回来时证据在新账本里都撤回了：藏起来、进了待确认
    unmatched: list[str] = field(default_factory=list)  # 认不出来、也不补的（不冻结的）
    resupported: list[str] = field(default_factory=list)  # 旧账本里藏着、新账本里又有说法撑着、放出来的
    decisions: int = 0  # 照搬的定过的决定
    skipped: list[dict[str, object]] = field(default_factory=list)  # 没搬的：旧编号、种类、为什么
    open_kept: int = 0  # 没处理的、新账本里也有的：建的时间照旧
    # 没处理的、新账本里没再出现的：旧编号、种类、为什么（L5 改了以后这对事实不在一个组了、实体对齐的结果不一样了）
    open_gone: list[dict[str, object]] = field(default_factory=list)
    groups: int = 0  # 重算了新旧的组

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# ---------- 导出 ----------


def _chat_decided(answer: object) -> bool:
    """这一行是照主人在对话里答的认出来定的（D83：answer 里记着认成什么；只记了一笔没认出来的不算）。"""
    if not answer:
        return False
    record = json.loads(str(answer))
    return isinstance(record, dict) and bool(record.get("decision"))


def _ids_in(claims: Mapping[str, object], *names: str) -> list[str]:
    return [str(claims[name]) for name in names if isinstance(claims.get(name), str)]


def _describe(conn: sqlite3.Connection, fact_id: str) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT f.*, e.canonical_name AS subject_name, e.type AS subject_type FROM facts AS f"
        " LEFT JOIN entities AS e ON e.entity_id = f.subject_entity_id WHERE f.fact_id = ?",
        (fact_id,),
    ).fetchone()
    if row is None:
        return None
    fact: dict[str, object] = {"fact_id": fact_id, **{name: row[name] for name in _FACT_COLUMNS}}
    fact["frozen"] = bool(row["frozen"])
    fact["scope"] = {"partitions": json.loads(row["scope_partitions"]), "sensitive": json.loads(row["scope_sensitive"])}
    fact["notes"] = json.loads(row["notes"])
    fact["sources"], fact["origins"] = json.loads(row["sources"]), json.loads(row["origins"])
    fact["subject"] = (
        {"entity_id": row["subject_entity_id"], "name": row["subject_name"], "type": row["subject_type"]}
        if row["subject_entity_id"] is not None
        else None
    )
    fact["entities"] = [
        {"entity_id": found["entity_id"], "name": found["canonical_name"], "type": found["type"], "role": found["role"]}
        for found in conn.execute(
            "SELECT x.entity_id, x.role, e.canonical_name, e.type FROM fact_entities AS x JOIN entities AS e"
            " ON e.entity_id = x.entity_id WHERE x.fact_id = ? ORDER BY x.entity_id, x.role",
            (fact_id,),
        )
    ]
    fact["evidence"] = [
        {**{name: found[name] for name in _EVIDENCE_COLUMNS}, "position": json.loads(found["position"])}
        for found in conn.execute("SELECT * FROM evidence WHERE fact_id = ? ORDER BY evidence_id", (fact_id,))
    ]
    said = conn.execute("SELECT min(said_from), count(*) FROM fact_claims WHERE fact_id = ?", (fact_id,)).fetchone()
    fact["said_from"], fact["claims"] = said[0] or row["as_of"], int(said[1])
    fact["siblings"] = _siblings(conn, row, fact["evidence"])  # type: ignore[arg-type]
    return fact


def _entity_of(conn: sqlite3.Connection, entity_id: object) -> dict[str, object] | None:
    if entity_id is None:
        return None
    row = conn.execute("SELECT canonical_name, type FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
    return {"entity_id": entity_id, "name": row[0], "type": row[1]} if row is not None else None


def _describe_relation(conn: sqlite3.Connection, relation_id: str) -> dict[str, object] | None:
    """一条关系导出的样子（J4.4）：各列、两头（编号、名字、类型）、证据和它们的指纹，导回时拿来认、拿来补。"""
    row = conn.execute("SELECT * FROM relations WHERE relation_id = ?", (relation_id,)).fetchone()
    if row is None:
        return None
    relation: dict[str, object] = {"relation_id": relation_id, **{name: row[name] for name in _RELATION_COLUMNS}}
    relation["frozen"] = bool(row["frozen"])
    relation["scope"] = {"partitions": json.loads(row["scope_partitions"]), "sensitive": json.loads(row["scope_sensitive"])}
    relation["notes"] = json.loads(row["notes"])
    relation["sources"], relation["origins"] = json.loads(row["sources"]), json.loads(row["origins"])
    relation["subject"] = _entity_of(conn, row["subject_entity_id"])
    relation["object"] = _entity_of(conn, row["object_entity_id"])
    relation["org"] = _entity_of(conn, row["org_entity_id"])
    relation["evidence"] = [
        {**{name: found[name] for name in _EVIDENCE_COLUMNS}, "position": json.loads(found["position"])}
        for found in conn.execute("SELECT * FROM relation_evidence WHERE relation_id = ? ORDER BY evidence_id", (relation_id,))
    ]
    said = conn.execute("SELECT min(said_from), count(*) FROM relation_claims WHERE relation_id = ?", (relation_id,)).fetchone()
    relation["said_from"], relation["claims"] = said[0] or row["as_of"], int(said[1])
    return relation


def _claim_key(attribute: object, value: object, content: object) -> tuple[str, str]:
    """说的是哪件事：属性和值都有的按属性和值，没有的按事实文字（key_text）。"""
    if key_text(attribute) and key_text(value):  # type: ignore[arg-type]
        return ("value", f"{key_text(attribute)}\n{key_text(value)}")  # type: ignore[arg-type]
    return ("text", key_text(str(content or "")))


def _siblings(conn: sqlite3.Connection, row: sqlite3.Row, evidence: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """同一句话撑着的、说的是同一件事（同一个可见范围、属性和值一样）、主体却是别的实体的事实：“越南那批和泰国那批
    都是 11-15 出货”就有两条。导回时有这样的，主体换了的那一档不能用，不然会认到另一个主体那一条上（审阅第 3 条）。"""
    key = _claim_key(row["attribute"], row["value"], row["content"])
    others: set[str] = set()
    for item in evidence:
        if item.get("sentence_hash"):
            found = conn.execute(
                "SELECT DISTINCT fact_id FROM evidence WHERE fact_id IS NOT NULL AND fact_id != ? AND packet_id = ? AND sentence_hash = ?",
                (row["fact_id"], item["packet_id"], item["sentence_hash"]),
            )
        else:
            found = conn.execute(
                "SELECT DISTINCT fact_id FROM evidence WHERE fact_id IS NOT NULL AND fact_id != ? AND packet_id = ? AND sentence_id = ?",
                (row["fact_id"], item["packet_id"], item["sentence_id"]),
            )
        others |= {str(other[0]) for other in found}
    siblings = []
    for other in sorted(others):
        sibling = conn.execute(
            "SELECT f.attribute, f.value, f.content, f.scope_partitions, f.scope_sensitive, f.subject_entity_id, e.canonical_name, e.type"
            " FROM facts AS f LEFT JOIN entities AS e ON e.entity_id = f.subject_entity_id WHERE f.fact_id = ?",
            (other,),
        ).fetchone()
        if (
            sibling is not None
            and sibling["subject_entity_id"] != row["subject_entity_id"]
            and sorted(json.loads(sibling["scope_partitions"])) == sorted(json.loads(row["scope_partitions"]))
            and sorted(json.loads(sibling["scope_sensitive"])) == sorted(json.loads(row["scope_sensitive"]))
            and _claim_key(sibling["attribute"], sibling["value"], sibling["content"]) == key
        ):
            siblings.append({"name": sibling["canonical_name"], "type": sibling["type"]})
    return siblings


def export_decisions(settings: Settings, library_name: str) -> dict[str, object]:
    """冻结标记和待确认队列（定过的、没处理的）连同它们提到的事实、实体。有事实的原文，写到仓库外。"""
    library = settings.library(library_name)
    if not library.db_path.is_file():
        raise ConfigError(f"{library_name} 库还没有账本")  # 不然 open_ledger 会建一本空的
    conn = open_ledger(library)  # 旧账本先升到最新，证据才有指纹、是哪封信
    try:
        reviews = [
            {
                "review_id": int(row["review_id"]),
                "kind": row["kind"],
                "claims": json.loads(row["claims"]),
                "quotes": json.loads(row["quotes"]),
                "decision": row["decision"],
                "decided_at": row["decided_at"],
                "decided_by": row["decided_by"],
                "created_at": row["created_at"],
                "code": row["code"],  # D83：Bot 问的时候用的编号，导回照旧
                "answer": json.loads(row["answer"]) if row["answer"] else None,  # 主人在对话里答的（问话、回答是哪一句）
            }
            for row in conn.execute("SELECT * FROM review_queue ORDER BY review_id")
        ]
        wanted = {row[0] for row in conn.execute("SELECT fact_id FROM facts WHERE frozen = 1")}
        wanted |= {row[0] for row in conn.execute("SELECT relation_id FROM relations WHERE frozen = 1")}  # J4.4
        entity_ids: set[str] = set()
        for review in reviews:
            claims = review["claims"] if isinstance(review["claims"], dict) else {}
            wanted |= set(_ids_in(claims, "frozen", "new", "fact", "relation"))
            wanted |= {str(item) for item in claims.get("relations") or [] if isinstance(item, str)}  # 同一时刻两个（J4.4）
            entity_ids |= set(_ids_in(claims, "entity_id"))
            entity_ids |= {str(item) for item in claims.get("candidates") or [] if isinstance(item, str)}
        facts = {fact_id: fact for fact_id in sorted(wanted) if (fact := _describe(conn, fact_id)) is not None}
        relations = {
            relation_id: relation for relation_id in sorted(wanted)
            if relation_id.startswith("R") and (relation := _describe_relation(conn, relation_id)) is not None
        }
        entities = {
            row["entity_id"]: {"name": row["canonical_name"], "type": row["type"]}
            for entity_id in sorted(entity_ids)
            for row in conn.execute("SELECT entity_id, canonical_name, type FROM entities WHERE entity_id = ?", (entity_id,))
        }
    finally:
        conn.close()
    return {
        "schema": SCHEMA,
        "library": library_name,
        "exported_at": iso(settings.now()),
        "facts": facts,
        "relations": relations,  # J4.4：冻结的关系、待确认里提到的关系；以前导出的文件里没有，导回时当空的
        "entities": entities,
        "reviews": reviews,
    }


# ---------- 导回 ----------


def _check(data: Mapping[str, object], library_name: str) -> None:
    if data.get("schema") != SCHEMA:
        raise ConfigError(f"冻结标记和决定的导出文件格式不对，要 {SCHEMA}")
    if data.get("library") not in (None, library_name):
        raise ConfigError(f"这份导出是 {data.get('library')} 库的，不能导进 {library_name} 库")
    if not isinstance(data.get("facts"), dict) or not isinstance(data.get("reviews"), list):
        raise ConfigError("导出文件要有 facts（按编号）、reviews（列表）")
    if not isinstance(data.get("entities", {}), dict):
        raise ConfigError("导出文件的 entities 要按编号")
    if not isinstance(data.get("relations", {}), dict):
        raise ConfigError("导出文件的 relations 要按编号")


def import_decisions(settings: Settings, library_name: str, data: Mapping[str, object]) -> ImportReport:
    """把 export_decisions 导出的冻结标记和决定导进整本重建、写完账的新账本（见开头）。拿不到锁抛 IntakeBusy、
    WriteBusy；新账本里已经有冻结标记或者定过的决定抛 ReviewError，什么都不动。"""
    _check(data, library_name)
    data_dir = settings.require_data_dir()
    with intake_lock(data_dir), WriteLock(data_dir):
        conn = open_ledger(settings.library(library_name))
        try:
            busy = conn.execute(
                "SELECT (SELECT count(*) FROM facts WHERE frozen = 1) + (SELECT count(*) FROM relations WHERE frozen = 1),"
                " (SELECT count(*) FROM review_queue WHERE decided_at IS NOT NULL"
                " AND NOT (kind = 'soft_change' AND decided_by = 'auto')"  # 写账最后一步照 soft.db 自己定的不算（J4.4）
                # 重建时照包里主人在对话里答的又认了一遍的也不算（D83，做法第 3.5 节）；只记了一笔没认出来、主人自己用
                # --decide 定的照样算（第 2 段审阅第 11 条）
                " AND (answer IS NULL OR json_extract(answer, '$.decision') IS NULL))"
            ).fetchone()
            if busy[0] or busy[1]:
                raise ReviewError(
                    f"这本账里已经有冻结标记 {busy[0]} 条、定过的决定 {busy[1]} 条，不导：导进来的会和它们搅在一起"
                    "（要导就在整本重建、写完账以后先导，再冻结、处理待确认）"
                )
            report = ImportReport(library_name)
            with transaction(conn):
                _Importer(conn, data, iso(settings.now()), report).run()
        finally:
            conn.close()
    return report


def _scope_of(fact: Mapping[str, object]) -> tuple[list[str], list[str]]:
    scope = fact.get("scope") or {}
    assert isinstance(scope, Mapping)
    return sorted(set(scope.get("partitions") or [])), sorted(set(scope.get("sensitive") or []))


def _scope_text(items: Sequence[str]) -> str:
    """可见范围写进 facts 的样子，和写账时一样（write._claim 用 _json：不转义中文）。"""
    return _json(list(items))


class _Importer:
    def __init__(self, conn: sqlite3.Connection, data: Mapping[str, object], now: str, report: ImportReport) -> None:
        self.conn = conn
        self.now = now
        self.report = report
        self.facts: dict[str, dict] = {str(key): value for key, value in dict(data["facts"]).items() if isinstance(value, dict)}  # type: ignore[arg-type]
        self.entities: dict[str, dict] = {str(key): value for key, value in dict(data.get("entities") or {}).items()}  # type: ignore[arg-type]
        self.reviews: list[dict] = [review for review in data["reviews"] if isinstance(review, dict)]  # type: ignore[union-attr]
        self.relations: dict[str, dict] = {  # J4.4：关系和事实共用一张对照表 map（编号 R 开头、F 开头，不会撞）
            str(key): value for key, value in dict(data.get("relations") or {}).items() if isinstance(value, dict)  # type: ignore[arg-type]
        }
        self.writer = LedgerWriter(conn, extracted_by="", embedding_model="", register=False)
        self.map: dict[str, str] = {}  # 旧的事实编号 → 新账本里的
        self.recreated: set[str] = set()
        self.touched: set[str] = set()  # 要重算新旧的事实（新编号）
        self.open_pairs: dict[tuple[str, str], Mapping[str, object]] = {}  # 没处理的冲突：(冻结的, 新说法) → 旧的那一行
        self.hidden_now: dict[str, set[str]] = {}  # 补回来时证据在新账本里都撤回了、藏起来的：编号 → 撤回了的包
        self.keep_age: list[tuple[str, str, str]] = []  # 没处理的、新账本又会进一条的：(种类, 怎么认, 旧的建的时间)
        self.held_facts: set[str] = set()  # 进图前检查转人工的事实（J5.3），导回了主人定的：重算这一条（新编号）
        # 和冻结的冲突里，重建时照对话又认过的已经删过的那几对：一对只删一次，后面导进来的不再被当成重建时认的删掉（复审第 2 条）
        self.rebuilt_dropped: set[tuple[str, str]] = set()

    def run(self) -> None:
        # 先都认一遍，再补：补回来的事实带着证据，先补的话后认的可能认到它身上
        for old, fact in sorted(self.facts.items()):
            found = self._match(fact)
            if found is not None:
                self.map[old] = found
        for old, fact in sorted(self.facts.items()):
            if old in self.map:
                continue
            if fact.get("frozen"):
                self.map[old] = self._recreate(fact)
                self.recreated.add(old)
                self.report.recreated.append(old)
            else:
                self.report.unmatched.append(old)
        for old, relation in sorted(self.relations.items()):  # 关系（J4.4）照事实的办法：先都认，再补冻结的
            found = self._match_relation(relation)
            if found is not None:
                self.map[old] = found
        for old, relation in sorted(self.relations.items()):
            if old in self.map:
                continue
            created = self._recreate_relation(relation) if relation.get("frozen") else None
            if created is not None:
                self.map[old] = created
                self.recreated.add(old)
                self.report.recreated.append(old)
            else:
                self.report.unmatched.append(old)
        self._freeze()
        for review in self.reviews:
            self._replay(review)
        for item_id, packets in sorted(self.hidden_now.items()):  # 还没有一条没处理的（定过的不算，第二遍审阅 N5），补一条
            name = "relation" if item_id.startswith("R") else "fact"
            if not self.conn.execute(
                f"SELECT 1 FROM review_queue WHERE kind = 'other' AND decided_at IS NULL AND json_extract(claims, '$.{name}') = ?",
                (item_id,),
            ).fetchone():
                self._insert(
                    {"created_at": self.now}, "other", {name: item_id, "reason": EVIDENCE_WITHDRAWN, "packets": sorted(packets)}, []
                )
        self._recompute()
        self._keep_ages()
        self.writer.fill_codes()  # 导进来、重算新进的待确认记下编号（导进来的照旧账本记的，D83）

    def _keep_ages(self) -> None:
        """没处理的软变化、判断 C 拿不准的、同一句话里两个不搬，新账本写账时又进了一条：建的时间照旧账本那一条的（30 天不从头
        算，第三轮审阅小的第 5 条）。软变化、判断 C 的按指纹（没有指纹的按句子编号）认，同一句话里两个按那一对关系认。"""
        for kind, match, created in self.keep_age:
            if not created:
                continue
            for row in self.conn.execute(
                "SELECT review_id, kind, claims, created_at FROM review_queue WHERE decided_at IS NULL AND kind = ?",
                ("other" if kind == "same_time" else kind,),
            ).fetchall():
                claims = json.loads(row["claims"])
                if not isinstance(claims, dict):
                    continue
                if kind == "same_time":
                    same = claims.get("reason") == SAME_TIME and _json(claims.get("relations")) == match
                else:
                    same = match in (claims.get("fingerprint"), claims.get("sentence"))
                if same and created < str(row["created_at"]):
                    self.conn.execute("UPDATE review_queue SET created_at = ? WHERE review_id = ?", (created, row["review_id"]))

    # ---------- 认事实 ----------

    def _match(self, fact: Mapping[str, object]) -> str | None:
        old = str(fact["fact_id"])
        if self.conn.execute("SELECT 1 FROM facts WHERE fact_id = ?", (old,)).fetchone() is not None:
            self.report.same_id += 1
            return old
        partitions, sensitive = _scope_of(fact)
        candidates: set[str] = set()
        for evidence in fact.get("evidence") or []:
            if evidence.get("sentence_hash"):  # 邮件的还要同一封信：同一个人两封信里一字不差的一句是两句话
                rows = self.conn.execute(
                    "SELECT DISTINCT fact_id FROM evidence WHERE fact_id IS NOT NULL AND packet_id = ? AND sentence_hash = ?"
                    " AND (? IS NULL OR message_id IS NULL OR message_id = ?)",
                    (evidence["packet_id"], evidence["sentence_hash"], evidence.get("message_id"), evidence.get("message_id")),
                )
            else:  # 没有指纹的旧证据：同一个包、同一个句子编号
                rows = self.conn.execute(
                    "SELECT DISTINCT fact_id FROM evidence WHERE fact_id IS NOT NULL AND packet_id = ? AND sentence_id = ?",
                    (evidence["packet_id"], evidence["sentence_id"]),
                )
            candidates |= {row[0] for row in rows}
        same_scope = []
        for candidate in sorted(candidates):
            row = self.conn.execute(
                "SELECT f.fact_id, f.content, f.attribute, f.value, f.scope_partitions, f.scope_sensitive, e.canonical_name, e.type"
                " FROM facts AS f LEFT JOIN entities AS e ON e.entity_id = f.subject_entity_id WHERE f.fact_id = ?",
                (candidate,),
            ).fetchone()
            if row is not None and (sorted(json.loads(row["scope_partitions"])), sorted(json.loads(row["scope_sensitive"]))) == (
                partitions,
                sensitive,
            ):
                same_scope.append(row)
        said = str(fact.get("said_from") or "")[:10]
        subject = fact.get("subject")
        subject_key = (subject["type"], name_key(str(subject["name"]))) if isinstance(subject, Mapping) and subject.get("name") else None
        same_claim = [row for row in same_scope if self._same_claim(fact, row, said)]
        with_subject = [
            row for row in same_claim
            if ((row["type"], name_key(str(row["canonical_name"]))) if row["canonical_name"] is not None else None) == subject_key
        ]
        # 主体换了的那一档（L5 合并、换了标准名）只在旧账本里这一句没有撑着别的主体的同一件事时才用（审阅第 3 条）
        tiers = (with_subject,) if fact.get("siblings") else (with_subject, same_claim)
        for tier in tiers:
            if len(tier) == 1:
                found = str(tier[0]["fact_id"])
                self.report.by_evidence[old] = found
                return found
        return None

    @staticmethod
    def _same_claim(fact: Mapping[str, object], row: sqlite3.Row, said: str) -> bool:
        """说的是同一件事：属性和值都有的比属性和值（值按 key_text，或者 J2.14 的统一写法），没有的比事实文字。"""
        attribute, value = fact.get("attribute"), fact.get("value")
        if key_text(attribute) and key_text(value):  # type: ignore[arg-type]
            if key_text(row["attribute"]) != key_text(attribute):  # type: ignore[arg-type]
                return False
            if key_text(row["value"]) == key_text(value):  # type: ignore[arg-type]
                return True
            if value_text is None:
                return False
            from datetime import date

            day = date.fromisoformat(said) if len(said) == 10 else None
            return value_text(row["value"], day, str(attribute)) == value_text(str(value), day, str(attribute))
        return key_text(row["content"]) == key_text(str(fact.get("content") or ""))

    # ---------- 认关系（J4.4） ----------

    def _same_end(self, wanted: object, entity_id: str, name: object, kind: object) -> bool:
        """导出时的一头（编号、名字、类型）和新账本里这一头是不是同一个：编号一样，或者同类型、同一个比对写法。"""
        if not isinstance(wanted, Mapping):
            return False
        if wanted.get("entity_id") == entity_id:
            return True
        return wanted.get("type") == kind and name_key(str(wanted.get("name") or "")) == name_key(str(name or ""))

    def _match_relation(self, relation: Mapping[str, object]) -> str | None:
        """编号一样的就是它（新账本里同一条关系、同一段最早那一句一样）。对不上的按证据认：证据里有一句指纹一样
        （邮件还要同一封信）、同一个可见范围、同一种关系（亲属还要具体说法一样）、两头一样（编号一样，或者同类型同一个
        比对写法），只剩一条就是它。别的不猜（和认事实一样）。"""
        old = str(relation["relation_id"])
        if self.conn.execute("SELECT 1 FROM relations WHERE relation_id = ?", (old,)).fetchone() is not None:
            self.report.same_id += 1
            return old
        partitions, sensitive = _scope_of(relation)
        candidates: set[str] = set()
        for evidence in relation.get("evidence") or []:  # type: ignore[union-attr]
            if evidence.get("sentence_hash"):
                rows = self.conn.execute(
                    "SELECT DISTINCT relation_id FROM relation_evidence WHERE packet_id = ? AND sentence_hash = ?"
                    " AND (? IS NULL OR message_id IS NULL OR message_id = ?)",
                    (evidence["packet_id"], evidence["sentence_hash"], evidence.get("message_id"), evidence.get("message_id")),
                )
            else:
                rows = self.conn.execute(
                    "SELECT DISTINCT relation_id FROM relation_evidence WHERE packet_id = ? AND sentence_id = ?",
                    (evidence["packet_id"], evidence["sentence_id"]),
                )
            candidates |= {str(row[0]) for row in rows}
        found = []
        for candidate in sorted(candidates):
            row = self.conn.execute(
                "SELECT r.relation_type, r.kinship, r.scope_partitions, r.scope_sensitive, r.subject_entity_id, r.object_entity_id,"
                " s.canonical_name AS subject_name, s.type AS subject_type, o.canonical_name AS object_name, o.type AS object_type"
                " FROM relations AS r JOIN entities AS s ON s.entity_id = r.subject_entity_id"
                " JOIN entities AS o ON o.entity_id = r.object_entity_id WHERE r.relation_id = ?",
                (candidate,),
            ).fetchone()
            if row is None:
                continue
            if (sorted(json.loads(row["scope_partitions"])), sorted(json.loads(row["scope_sensitive"]))) != (partitions, sensitive):
                continue
            if row["relation_type"] != relation.get("relation_type"):
                continue
            if key_text(row["kinship"]) != key_text(relation.get("kinship")):  # type: ignore[arg-type]
                continue
            if self._same_end(relation.get("subject"), row["subject_entity_id"], row["subject_name"], row["subject_type"]) and \
                    self._same_end(relation.get("object"), row["object_entity_id"], row["object_name"], row["object_type"]):
                found.append(candidate)
        if len(found) == 1:
            self.report.by_evidence[old] = found[0]
            return found[0]
        return None

    def _recreate_relation(self, relation: Mapping[str, object]) -> str | None:
        """认不出来的冻结关系照旧样子补回来（和冻结事实一样，算主人担保）：编号、两头、各列照旧，没有说法；证据按指纹
        对到新账本里这个包现在那一版、同一封信、没撤回的句子上。两头在新账本里补不出来的（导出文件里没写名字），不补。"""
        relation_id = str(relation["relation_id"])
        subject = self._entity(relation.get("subject"))  # type: ignore[arg-type]
        target = self._entity(relation.get("object"))  # type: ignore[arg-type]
        if subject is None or target is None or subject == target:
            return None
        org = self._entity(relation.get("org")) if relation.get("org") else None  # type: ignore[arg-type]
        partitions, sensitive = _scope_of(relation)
        scope = _scope_of_row(json.dumps(partitions), json.dumps(sensitive))
        kind = str(relation.get("relation_type") or "")
        identity = relation_identity(scope, subject, kind, target, kinship=relation.get("kinship"), org_id=org)  # type: ignore[arg-type]
        merge = relation.get("merge_key")
        if merge and self.conn.execute("SELECT 1 FROM relations WHERE merge_key = ?", (merge,)).fetchone():
            merge = None  # 新账本里同一个键已经是别的编号了：补回来的这条不占它
        state = relation.get("state") if relation.get("state") in ("active", "paused", "ended") else "active"
        self.conn.execute(
            "INSERT INTO relations (relation_id, subject_entity_id, relation_type, object_entity_id, kinship, account, role,"
            " org_entity_id, position, department, service, valid_from, valid_to, valid_from_precision, valid_to_precision,"
            " valid_from_basis, valid_to_basis, state, pause_condition, status, pending_reason, speaker, speaker_role,"
            " attributed_to, frozen, scope_partitions, scope_sensitive, merge_key, group_key, identity_key, as_of, as_of_epoch,"
            " notes, extracted_by, rule_version, sources, origins, secondhand, stated, owner_said, withdrawn_at, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?, ?)",
            (
                relation_id, subject, kind, target, relation.get("kinship"), relation.get("account"), relation.get("role"),
                org, relation.get("position"), relation.get("department"), relation.get("service"),
                relation.get("valid_from"), relation.get("valid_to"), relation.get("valid_from_precision") or "unknown",
                relation.get("valid_to_precision") or "unknown", relation.get("valid_from_basis"), relation.get("valid_to_basis"),
                state, relation.get("pause_condition") if state == "paused" else None, relation.get("status") or "verified",
                relation.get("pending_reason"), relation.get("speaker"), relation.get("speaker_role"),
                relation.get("attributed_to"), _scope_text(partitions), _scope_text(sensitive), merge, relation.get("group_key"),
                identity, relation.get("as_of"), relation.get("as_of_epoch"), _json(relation.get("notes") or []),
                relation.get("extracted_by"), relation.get("rule_version"), int(bool(relation.get("secondhand"))),
                int(bool(relation.get("stated"))), int(bool(relation.get("owner_said"))), relation.get("withdrawn_at"),
                relation.get("created_at") or self.now, self.now,
            ),
        )
        kept = 0
        withdrawn: set[str] = set()
        for evidence in relation.get("evidence") or []:  # type: ignore[union-attr]
            anchored, gone = self._anchor(evidence)
            if anchored is None:
                if gone:
                    withdrawn.add(str(evidence["packet_id"]))
                self.report.evidence_dropped += 1
                continue
            sentence_id, revision, position = anchored
            meta = self.writer._evidence_meta(str(evidence["packet_id"]), revision, sentence_id)
            if meta[0] is None:
                self.report.evidence_dropped += 1
                continue
            quote = str(evidence.get("quote") or "")
            row = self.conn.execute("SELECT text FROM raw_sentences WHERE sentence_id = ? AND revision = ?", (sentence_id, revision)).fetchone()
            if row is not None and quote not in row[0]:
                quote = row[0]
            self.conn.execute(
                "INSERT OR IGNORE INTO relation_evidence (relation_id, packet_id, revision, sentence_id, quote, position, created_at,"
                " source, origin, speaker_role, sentence_hash, message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (relation_id, evidence["packet_id"], revision, sentence_id, quote, _json(position), self.now, *meta),
            )
            kept += 1
        if not kept and withdrawn and relation.get("withdrawn_at") is None:
            self.conn.execute("UPDATE relations SET withdrawn_at = ? WHERE relation_id = ?", (self.now, relation_id))
            self.hidden_now[relation_id] = withdrawn
            self.report.hidden.append(relation_id)
        self._mask_withdrawn_senders(relation_id, relation, table="relations", column="relation_id")
        self.writer._relation_provenance(relation_id)
        return relation_id

    # ---------- 补回认不出来的冻结事实 ----------

    def _entity(self, entity: Mapping[str, object] | None) -> str | None:
        """新账本里的这个实体：编号一样的，或者同类型、同一个比对写法的；都没有就照旧建一个。"""
        if not isinstance(entity, Mapping) or not entity.get("name") or not entity.get("type"):
            return None
        if entity.get("entity_id") and self.conn.execute(
            "SELECT 1 FROM entities WHERE entity_id = ?", (entity["entity_id"],)
        ).fetchone():
            return str(entity["entity_id"])
        key = name_key(str(entity["name"]))
        row = self.conn.execute("SELECT entity_id FROM entities WHERE type = ? AND name_key = ?", (entity["type"], key)).fetchone()
        if row is not None:
            return str(row[0])
        entity_id = entity_id_of(str(entity["type"]), key)
        self.conn.execute(
            "INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at, name_key) VALUES (?, ?, ?, ?, ?, ?)",
            (entity_id, entity["name"], entity["type"], self.now, self.now, key),
        )
        return entity_id

    def _anchor(self, evidence: Mapping[str, object]) -> tuple[tuple[str, int, dict[str, object]] | None, bool]:
        """这条证据在新账本里落在哪一句：这个包现在那一版里指纹一样（同一封信）、没撤回的句子；没有的话，
        原来那一版还在新账本里（替换前的旧版本）就照旧指着它。返回 (落在哪, 是不是撤回了)：

        新账本里这个包撤回了、这一句按撤回记录（指纹、同一封信）撤回了的，不落，算撤回了（审阅第 7 条：导出冻结标记
        以后才撤回、再导出撤回记录的）。别的落不下的（那一版不在新账本里了）不算撤回。"""
        packet_id = str(evidence["packet_id"])
        if self.conn.execute(
            "SELECT 1 FROM withdrawn_packets WHERE packet_id = ?1"
            " UNION ALL SELECT 1 FROM packets WHERE packet_id = ?1 AND withdrawn_at IS NOT NULL LIMIT 1",
            (packet_id,),
        ).fetchone():
            return None, True
        digest = evidence.get("sentence_hash")
        if digest:
            records = {
                (row[0], row[1])
                for row in self.conn.execute("SELECT message_id, sentence_hash FROM withdrawn_sentences WHERE packet_id = ?", (packet_id,))
            }
            if covered(records, evidence.get("message_id"), str(digest)):  # type: ignore[arg-type]
                return None, True
            for row in self.conn.execute(
                "SELECT s.* FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
                " WHERE s.packet_id = ? AND p.state = 'current' AND s.withdrawn_at IS NULL ORDER BY s.seq",
                (packet_id,),
            ):
                if sentence_hash(row["speaker"], row["text"]) == digest and (
                    evidence.get("message_id") is None or row["message_id"] == evidence.get("message_id")
                ):
                    return (str(row["sentence_id"]), int(row["revision"]), _position(row)), False
        else:  # 没有指纹的旧证据：同一版的同一句撤回了没有
            row = self.conn.execute(
                "SELECT withdrawn_at FROM raw_sentences WHERE sentence_id = ? AND revision = ?", (evidence["sentence_id"], evidence["revision"])
            ).fetchone()
            if row is not None and row[0] is not None:
                return None, True
        kept = self.conn.execute(
            "SELECT 1 FROM packets WHERE packet_id = ? AND revision = ? AND purged_at IS NULL", (packet_id, evidence["revision"])
        ).fetchone()
        if kept is not None:
            return (str(evidence["sentence_id"]), int(evidence["revision"]), dict(evidence.get("position") or {})), False  # type: ignore[arg-type]
        return None, False

    def _recreate(self, fact: Mapping[str, object]) -> str:
        fact_id = str(fact["fact_id"])
        partitions, sensitive = _scope_of(fact)
        scope = _scope_of_row(json.dumps(partitions), json.dumps(sensitive))
        subject = self._entity(fact.get("subject"))  # type: ignore[arg-type]
        merge = fact.get("merge_key")
        if merge and self.conn.execute("SELECT 1 FROM facts WHERE merge_key = ?", (merge,)).fetchone():
            merge = None  # 新账本里同一个键已经是别的编号了（L5 换了算法）：补回来的这条不占它
        self.conn.execute(
            "INSERT INTO facts (fact_id, content, subject_entity_id, attribute, value, occurred_from, occurred_to, time_precision,"
            " status, pending_reason, speaker, speaker_role, frozen, scope_partitions, scope_sensitive, withdrawn_at, created_at,"
            " updated_at, merge_key, group_key, as_of, as_of_epoch, notes, extracted_by, rule_version, sources, origins,"
            " secondhand, stated, owner_said, attributed_to)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?)",
            (
                fact_id, fact.get("content") or "", subject, fact.get("attribute"), fact.get("value"),
                fact.get("occurred_from"), fact.get("occurred_to"), fact.get("time_precision") or "unknown",
                fact.get("status") or "verified", fact.get("pending_reason"), fact.get("speaker"), fact.get("speaker_role"),
                _scope_text(partitions), _scope_text(sensitive), fact.get("withdrawn_at"), fact.get("created_at") or self.now,
                self.now, merge, group_key(scope, subject, fact.get("attribute")),  # type: ignore[arg-type]
                fact.get("as_of"), fact.get("as_of_epoch"), _json(fact.get("notes") or []), fact.get("extracted_by"),
                fact.get("rule_version"), int(bool(fact.get("secondhand"))), int(bool(fact.get("stated"))),
                int(bool(fact.get("owner_said"))), fact.get("attributed_to"),
            ),
        )
        roles = {(entity_id, str(item.get("role"))) for item in fact.get("entities") or [] if isinstance(item, Mapping)
                 and (entity_id := self._entity(item)) is not None and item.get("role") in ("subject", "object", "mention")}
        self.conn.executemany(
            "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id, role) VALUES (?, ?, ?)",
            [(fact_id, entity_id, role) for entity_id, role in sorted(roles)],
        )
        kept = 0
        withdrawn: set[str] = set()
        for evidence in fact.get("evidence") or []:
            anchored, gone = self._anchor(evidence)
            if anchored is None:
                if gone:
                    withdrawn.add(str(evidence["packet_id"]))
                self.report.evidence_dropped += 1
                continue
            sentence_id, revision, position = anchored
            meta = self.writer._evidence_meta(str(evidence["packet_id"]), revision, sentence_id)
            if meta[0] is None:  # 包登记里没有这一版
                self.report.evidence_dropped += 1
                continue
            quote = str(evidence.get("quote") or "")
            row = self.conn.execute("SELECT text FROM raw_sentences WHERE sentence_id = ? AND revision = ?", (sentence_id, revision)).fetchone()
            if row is not None and quote not in row[0]:
                quote = row[0]
            self.conn.execute(
                "INSERT OR IGNORE INTO evidence (fact_id, packet_id, revision, sentence_id, quote, position, created_at,"
                " source, origin, speaker_role, sentence_hash, message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fact_id, evidence["packet_id"], revision, sentence_id, quote, _json(position), self.now, *meta),
            )
            kept += 1
        if not kept and withdrawn and fact.get("withdrawn_at") is None:
            # 证据在新账本里都撤回了（导出冻结标记以后才撤回的）：和撤回时一样，藏起来、进待确认，等主人定
            self.conn.execute("UPDATE facts SET withdrawn_at = ? WHERE fact_id = ?", (self.now, fact_id))
            self.hidden_now[fact_id] = withdrawn
            self.report.hidden.append(fact_id)
        self._mask_withdrawn_senders(fact_id, fact)
        self.writer._provenance(fact_id)  # 来源、origin 按补回来的证据算；原话还是推断照旧
        return fact_id

    def _mask_withdrawn_senders(
        self, fact_id: str, fact: Mapping[str, object], *, table: str = "facts", column: str = "fact_id"
    ) -> None:
        """补回来的事实照导出时的样子写回说话人和说法出自谁。导出冻结标记以后才按事实撤回了邮件的发件人那一句的
        （D73 第 16 条；再导出撤回记录、整本重建），新账本里这封信的发件人那一句是撤回了的：说话人是这个发件人的换成
        “[已撤回]”，说法出自谁有就换（和 LedgerWriter.mask_senders 对没有说法的事实一样，宁可多换；J4.1 审阅第 1 条）。
        信按导出的证据认：证据不知道是哪封信的（旧证据），这个包里发件人撤回了的信都算。证据早就全撤回了、导出的证据是空的
        （藏着的冻结事实、冻结关系）：照导出文件里“证据都撤回了”那条待确认记的包号找，这些包里发件人撤回了的信都算
        （和写账那边的 LedgerWriter._mask_bare 一样；J4.4 审阅第 2 条）。"""
        mails = {
            (str(item["packet_id"]), item.get("message_id"))
            for item in fact.get("evidence") or []
            if isinstance(item, Mapping) and item.get("packet_id")
        }
        if not mails:
            name = "relation" if table == "relations" else "fact"
            for review in self.reviews:
                claims = review.get("claims")
                if isinstance(claims, Mapping) and claims.get("reason") == EVIDENCE_WITHDRAWN and claims.get(name) == fact_id:
                    mails |= {(str(packet), None) for packet in claims.get("packets") or [] if packet}
        senders: set[str] = set()
        for packet_id, message_id in sorted(mails, key=repr):
            senders |= {
                str(row[0])
                for row in self.conn.execute(
                    "SELECT DISTINCT speaker FROM raw_sentences WHERE packet_id = ? AND field = 'from'"
                    " AND withdrawn_at IS NOT NULL AND (? IS NULL OR message_id = ?)",
                    (packet_id, message_id, message_id),
                )
            }
        if not senders:
            return
        self.conn.execute(
            f"UPDATE {table} SET speaker = ? WHERE {column} = ? AND speaker IN ({', '.join('?' for _ in senders)})",
            (MASKED, fact_id, *sorted(senders)),
        )
        self.conn.execute(f"UPDATE {table} SET attributed_to = ? WHERE {column} = ? AND attributed_to IS NOT NULL", (MASKED, fact_id))

    # ---------- 冻结标记 ----------

    def _freeze(self) -> None:
        for old, fact in sorted(self.facts.items()):
            if not fact.get("frozen") or old not in self.map:
                continue
            new = self.map[old]
            self.report.frozen += 1
            self.touched.add(new)
            if old in self.recreated:
                continue  # 补回来时就是冻结的，藏没藏照旧
            hidden = self.conn.execute("SELECT withdrawn_at FROM facts WHERE fact_id = ?", (new,)).fetchone()[0]
            self.conn.execute("UPDATE facts SET frozen = 1, updated_at = ? WHERE fact_id = ?", (self.now, new))
            if self.conn.execute("SELECT 1 FROM fact_claims WHERE fact_id = ? AND judge_hold IS NOT NULL LIMIT 1", (new,)).fetchone():
                self.held_facts.add(new)  # 有进图前检查转人工的说法（J5.3）：冻结的是主人担保的，最后照冻结重算这一条
            if fact.get("withdrawn_at") is not None and hidden is None:
                self.report.resupported.append(old)  # 旧账本里藏着，新账本里又有说法撑着：放出来
        for old, relation in sorted(self.relations.items()):  # 关系一样（J4.4）
            if not relation.get("frozen") or old not in self.map:
                continue
            new = self.map[old]
            self.report.frozen += 1
            self.touched.add(new)
            if old in self.recreated:
                continue
            hidden = self.conn.execute("SELECT withdrawn_at FROM relations WHERE relation_id = ?", (new,)).fetchone()[0]
            self.conn.execute("UPDATE relations SET frozen = 1, updated_at = ? WHERE relation_id = ?", (self.now, new))
            if relation.get("withdrawn_at") is not None and hidden is None:
                self.report.resupported.append(old)

    # ---------- 待确认 ----------

    def _skip(self, review: Mapping[str, object], why: str) -> None:
        self.report.skipped.append({"review_id": review.get("review_id"), "kind": review.get("kind"), "why": why})

    def _gone(self, review: Mapping[str, object], why: str) -> None:
        self.report.open_gone.append({"review_id": review.get("review_id"), "kind": review.get("kind"), "why": why})

    def _insert(self, review: Mapping[str, object], kind: str, claims: Mapping[str, object], quotes: Sequence[str]) -> None:
        answer = review.get("answer")  # D83：编号、对话里答的照搬（以前导出的文件里没有，当空的）
        self.conn.execute(
            "INSERT INTO review_queue (kind, claims, quotes, decision, decided_at, decided_by, created_at, code, answer)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, _json(dict(claims)), _json(list(quotes)), review.get("decision"), review.get("decided_at"),
             review.get("decided_by"), review.get("created_at") or self.now, review.get("code"),
             _json(answer) if isinstance(answer, dict) else None),
        )
        self.writer.reviews_changed()  # 写账本的那边按待确认表算的几样作废（认了的“抽取没抽出来的纠正”这些）
        if review.get("decided_at") is not None:
            self.report.decisions += 1

    def _quotes(self, fact_id: str) -> list[str]:
        if fact_id.startswith("R"):  # 关系（J4.4）
            return [row[0] for row in self.conn.execute(
                "SELECT sentence_id FROM relation_evidence WHERE relation_id = ? ORDER BY evidence_id", (fact_id,))]
        return [row[0] for row in self.conn.execute("SELECT sentence_id FROM evidence WHERE fact_id = ? ORDER BY evidence_id", (fact_id,))]

    def _replay(self, review: Mapping[str, object]) -> None:
        kind = str(review.get("kind"))
        claims = review.get("claims") if isinstance(review.get("claims"), dict) else {}
        assert isinstance(claims, dict)
        decided = review.get("decided_at") is not None
        if kind == "frozen_conflict":
            frozen, new = self.map.get(str(claims.get("frozen"))), self.map.get(str(claims.get("new")))
            if frozen is None or new is None:
                self._skip(review, "冲突的一边在新账本里认不出来")
                return
            if frozen == new:
                self._skip(review, "两边在新账本里成了同一条事实")
                return
            self.touched |= {frozen, new}
            if decided:
                # 主人原话的冲突（D204）重建时就进了队列，包里主人在对话里答的又认了一遍：删掉，照导进来的（D83，同一条不重复）。
                # 一对只在头一回删：同一对旧账本里定过好几行的，后面的不能把前面刚导进来的（也带着回答）删掉
                if (frozen, new) not in self.rebuilt_dropped:
                    self.rebuilt_dropped.add((frozen, new))
                    self._drop_rebuilt_answered(
                        "frozen_conflict", "json_extract(claims, '$.frozen') || ' ' || json_extract(claims, '$.new')", f"{frozen} {new}")
                self._insert(review, kind, {**claims, "frozen": frozen, "new": new}, self._quotes(new))
            else:
                self.open_pairs[(frozen, new)] = review
            return
        if kind == "ambiguous_entity":
            self._replay_ambiguity(review, claims, decided)
            return
        if kind == "other" and claims.get("reason") == EVIDENCE_WITHDRAWN:
            name = "relation" if "relation" in claims else "fact"  # 证据都撤回了的冻结关系（J4.4）照事实
            old = str(claims.get(name))
            fact = self.map.get(old)
            if fact is None:
                self._skip(review, "那条冻结事实、冻结关系在新账本里没有（旧账本里删掉了，或者认不出来）")
                return
            self.touched.add(fact)
            if old in self.report.resupported and not decided:  # 又有说法撑着、放出来了：和写账时一样自动关掉
                self._insert({**review, "decision": RESUPPORTED, "decided_at": self.now, "decided_by": "auto"}, kind,
                             {**claims, name: fact}, [])
                return
            self._insert(review, kind, {**claims, name: fact}, [])
            if not decided:
                self.report.open_kept += 1
            return
        if kind == "other" and claims.get("reason") == SAME_TIME:  # 同一时刻两个的关系（J4.4）
            pair = [self.map.get(str(item)) for item in claims.get("relations") or []]
            if len(pair) != 2 or None in pair or pair[0] == pair[1]:
                self._skip(review, "那两条关系在新账本里认不出来，或者成了同一条")
                return
            self.touched |= set(pair)  # type: ignore[arg-type]
            if decided:  # 没处理的不搬：重算时同一句话里还是两个的，自己会再进队列（建的时间照旧，30 天不从头算）
                # 重建时照对话又认过的同一对：删掉，照导进来的（D83，同一条不重复）
                self._drop_rebuilt_answered("other", "json_extract(claims, '$.relations')", _json(sorted(pair)),  # type: ignore[type-var]
                                            as_json=True)
                self._insert(review, kind, {**claims, "relations": sorted(pair)}, [])  # type: ignore[type-var]
            else:
                self.keep_age.append(("same_time", _json(sorted(pair)), str(review.get("created_at") or "")))  # type: ignore[type-var]
            return
        if kind == "manual_review" and claims.get("item") == "fact":
            # 进图前检查转人工的事实（J5.3）：和判断 C 拿不准的关系一样，定过的照搬，没定的新账本写账时会再挡住
            if not decided:
                self.keep_age.append(("manual_review", str(claims.get("fingerprint") or claims.get("sentence")),
                                      str(review.get("created_at") or "")))
                return
            moved = dict(claims)
            target = self.map.get(str(claims.get("fact")))
            if target is None and self.conn.execute("SELECT 1 FROM facts WHERE fact_id = ?", (str(claims.get("fact")),)).fetchone():
                # 主人说了不要的那条，旧账本里已经删了、导出文件里没有它：事实编号照内容算，新账本里还是这个编号
                target = str(claims.get("fact"))
            if target:
                moved["fact"] = moved["key"] = target
                self.touched.add(target)
                self.held_facts.add(target)
            self._drop_rebuilt(kind, moved)
            self._insert(review, kind, moved, [str(claims.get("sentence"))] if claims.get("sentence") else [])
            if review.get("decision") == DROP:
                # 主人说不要：新账本写账时这一处又照常写成了说法（那时还没有这条决定），照 javmem review --drop 删掉（审阅第 1 条）
                self.conn.execute(
                    "DELETE FROM fact_claims WHERE judge_hold IS NOT NULL AND (claim_id = ?"
                    " OR (fact_id = ? AND json_extract(proofs, '$[0].sentence_id') = ?))",
                    (moved.get("claim"), moved.get("fact"), moved.get("sentence")),
                )
            # 主人定的那一行留痕照旧补上（新账本的留痕表是重写出来的，审阅第 1 条）
            self.writer._review_audit(moved, str(review.get("decision")), str(review.get("decided_at") or self.now),
                                      str(review.get("decided_by") or "owner"))
            return
        if kind == "manual_review" and "relation" in claims:  # 判断 C 拿不准的关系（J4.4）：定过的照搬，没定的新账本会再挡住
            if not decided:  # 新账本写账时又会进待确认：建的时间照旧
                self.keep_age.append(("manual_review", str(claims.get("fingerprint") or claims.get("sentence")),
                                      str(review.get("created_at") or "")))
                return
            moved = dict(claims)
            if self.map.get(str(claims.get("relation"))):
                moved["relation"] = self.map[str(claims["relation"])]
                row = self.conn.execute("SELECT identity_key, group_key FROM relations WHERE relation_id = ?",
                                        (moved["relation"],)).fetchone()
                if row is not None and row[0]:
                    moved["key"] = row[0]
                    if claims.get("reason") in correct.REASONS:  # 纠正的（J4.6）：另记同一条关系的键；财务投资挡着的按组认
                        moved["identity"] = row[0]
                        if claims.get("reason") == correct.CORRECTION and row[1]:
                            moved["key"] = row[1]
                self.touched.add(str(moved["relation"]))
            self._drop_rebuilt(kind, moved)  # 新账本写账时已经又进了一条没定的：删掉，照搬来的定
            self._insert(review, kind, moved, [str(claims.get("sentence"))] if claims.get("sentence") else [])
            if claims.get("precheck"):  # 开着进图前检查时转人工的（J5.3）：主人定的那一行留痕照旧补上（第二遍审阅第 2 条）
                self.writer._review_audit(moved, str(review.get("decision")), str(review.get("decided_at") or self.now),
                                          str(review.get("decided_by") or "owner"))
            return
        if kind == "soft_change":  # 软变化（J4.4 第 4 段）：定过的照搬，新账本写账时照它办；没定的不搬，新账本自己会再挡住
            if not decided:  # 新账本写账时同一句又会挡住、进待确认：建的时间照旧（第三轮审阅小的第 5 条）
                self.keep_age.append(("soft_change", str(claims.get("fingerprint") or claims.get("sentence")),
                                      str(review.get("created_at") or "")))
                return
            name = "relation" if claims.get("item") == "relation" else "fact"
            moved = dict(claims)
            for field_name in (name, "old"):
                if claims.get(field_name) and self.map.get(str(claims[field_name])):
                    moved[field_name] = self.map[str(claims[field_name])]
            table, column = ("relations", "relation_id") if name == "relation" else ("facts", "fact_id")
            if name == "relation":
                for field_name, key_name in ((name, "key"), ("old", "old_identity")):
                    row = self.conn.execute(f"SELECT identity_key FROM {table} WHERE {column} = ?", (moved.get(field_name),)).fetchone()
                    if row is not None and row[0]:
                        moved[key_name] = row[0]
            elif moved.get(name):
                moved["key"] = moved[name]
            self.touched |= {str(moved[field_name]) for field_name in (name, "old") if moved.get(field_name)}
            # 新账本写账时同一句已经又挡住了（没处理的）、或者照新的 soft.db 自己定了：导进来的定过的盖过它（第三轮审阅 S2、S5）
            self._drop_rebuilt(kind, moved)
            self._insert(review, kind, moved, [str(claims.get("sentence"))] if claims.get("sentence") else [])
            return
        self._skip(review, "这一种还没有命令能处理，不搬")

    def _drop_rebuilt(self, kind: str, claims: Mapping[str, object]) -> None:
        """新账本里同一句（有指纹的按指纹认）、同一条的软变化、判断 C 拿不准的：没处理的，和写账最后一步自己定的、重建时照
        包里主人在对话里答的又认了一遍的（D83），删掉，照导进来的（同一条不重复）。"""
        for row in self.conn.execute(
            "SELECT review_id, claims, decided_at, decided_by, answer FROM review_queue WHERE kind = ?", (kind,)
        ).fetchall():
            mine = json.loads(row["claims"])
            if not isinstance(mine, dict) or str(mine.get("key")) != str(claims.get("key")):
                continue
            if mine.get("item") != claims.get("item"):
                continue
            # 纠正挡住的（J4.6）按原因分开；判断 C 拿不准的（J4.4）重建时原因可能变了（llm_failed → llm_unsure），不比
            # （第一轮审阅第 12 条）
            if {mine.get("reason"), claims.get("reason")} & set(correct.REASONS) and mine.get("reason") != claims.get("reason"):
                continue
            same = (mine.get("fingerprint") == claims.get("fingerprint") if claims.get("fingerprint") and mine.get("fingerprint")
                    else mine.get("sentence") == claims.get("sentence"))
            if same and (row["decided_at"] is None or row["decided_by"] == "auto" or _chat_decided(row["answer"])):
                self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (row["review_id"],))

    def _drop_rebuilt_answered(self, kind: str, match: str, value: str, *, as_json: bool = False) -> None:
        """新账本里同一条（同一对关系、同一个实体）重建时照对话又认过、定了的那一行删掉，照导进来的（D83：同一条不重复，
        第 2 段审阅第 6 条）。as_json：value 是 JSON（一对关系），照 SQLite 的写法比。"""
        for row in self.conn.execute(
            f"SELECT review_id, answer FROM review_queue WHERE kind = ? AND decided_at IS NOT NULL"
            f" AND {match} = {'json(?)' if as_json else '?'}", (kind, value)
        ).fetchall():
            if _chat_decided(row["answer"]):
                self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (row["review_id"],))
        self.writer.reviews_changed()

    def _replay_ambiguity(self, review: Mapping[str, object], claims: dict, decided: bool) -> None:
        old = str(claims.get("entity_id"))
        entity = self._existing_entity(old, claims)
        if entity is None:
            if decided:
                self._skip(review, "那个实体在新账本里没有")
            else:
                self._gone(review, "那个实体在新账本里没有")
            return
        candidates = [found for item in claims.get("candidates") or [] if (found := self._existing_entity(str(item), {})) is not None]
        scope = claims.get("scope")  # 记下可见范围以前写的没有这一项，写账照旧当哪个范围都算（审阅第 4 条）
        if decided:
            self._drop_rebuilt_answered("ambiguous_entity", "json_extract(claims, '$.entity_id')", entity)
        row = self.conn.execute(
            "SELECT review_id FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL"
            " AND json_extract(claims, '$.entity_id') = ? AND (? IS NULL OR coalesce(json_extract(claims, '$.scope'), ?) = ?)"
            " ORDER BY review_id LIMIT 1",
            (entity, scope, scope, scope),
        ).fetchone()
        if not decided:
            if row is None:
                self._gone(review, "新账本里这个实体没有歧义了")
                return
            self.conn.execute("UPDATE review_queue SET created_at = ? WHERE review_id = ?", (review.get("created_at"), row[0]))
            self.report.open_kept += 1
            return
        if row is None:  # 新账本里没再出现：记下这条决定，以后再出现不再问
            self._insert(review, "ambiguous_entity", {**claims, "entity_id": entity, "candidates": candidates}, [])
            return
        try:
            self.writer.decide_review(int(row[0]), str(review.get("decision")), str(review.get("decided_at")), by=str(review.get("decided_by") or "owner"))
        except ValueError as error:
            self._skip(review, str(error))
            return
        answer = review.get("answer")  # D83：编号、对话里答的照搬
        self.conn.execute(
            "UPDATE review_queue SET created_at = ?, code = coalesce(?, code), answer = ? WHERE review_id = ?",
            (review.get("created_at"), review.get("code"), _json(answer) if isinstance(answer, dict) else None, row[0]),
        )
        self.report.decisions += 1

    def _existing_entity(self, entity_id: str, claims: Mapping[str, object]) -> str | None:
        """旧账本的实体在新账本里是哪个：编号一样的，或者同类型、同一个比对写法的。"""
        if self.conn.execute("SELECT 1 FROM entities WHERE entity_id = ?", (entity_id,)).fetchone():
            return entity_id
        known = self.entities.get(entity_id) or {"name": claims.get("name"), "type": claims.get("type")}
        if not known.get("name") or not known.get("type"):
            return None
        row = self.conn.execute(
            "SELECT entity_id FROM entities WHERE type = ? AND name_key = ?", (known["type"], name_key(str(known["name"])))
        ).fetchone()
        return str(row[0]) if row is not None else None

    # ---------- 重算新旧 ----------

    def _recompute(self) -> None:
        for old in sorted(self.recreated):  # 照旧补回来的冻结事实、关系：冻结的算主人认的（J5.3 进图的口子看它，审阅第 5 条）
            new = self.map.get(old)
            if new:
                table, column = ("relations", "relation_id") if new.startswith("R") else ("facts", "fact_id")
                self.conn.execute(f"UPDATE {table} SET judged_by = 'owner' WHERE {column} = ? AND frozen = 1", (new,))
        for fact_id in sorted(self.held_facts):  # 进图前检查转人工的事实（J5.3）：照导进来的决定重算这一条（有组的下面再重算新旧）
            self.writer._refresh_fact(fact_id, {}, set(), self.now)
        groups = {
            row[0]
            for fact_id in sorted(self.touched)
            for row in self.conn.execute("SELECT group_key FROM facts WHERE fact_id = ? AND group_key IS NOT NULL", (fact_id,))
        }
        for group in sorted(groups):
            self.writer._supersede(group, self.now)
        identities = {  # 关系（J4.4）：按冻结标记和决定重算
            row[0]
            for item_id in sorted(self.touched) if item_id.startswith("R")
            for row in self.conn.execute("SELECT identity_key FROM relations WHERE relation_id = ? AND identity_key IS NOT NULL", (item_id,))
        }
        self.writer._refresh_relations(identities, set(), self.now)
        self.report.groups = len(groups) + len(identities)
        for (frozen, new), review in sorted(self.open_pairs.items()):
            cursor = self.conn.execute(
                "UPDATE review_queue SET created_at = ? WHERE kind = 'frozen_conflict' AND decided_at IS NULL"
                " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ?",
                (review.get("created_at") or self.now, frozen, new),
            )
            if cursor.rowcount:
                self.report.open_kept += 1
            else:
                self._gone(review, "新账本里这一对不冲突了（不在一个组，或者新说法被别的替代了）")
