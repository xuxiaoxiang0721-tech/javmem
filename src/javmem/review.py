"""冻结和待确认队列（J2.12）：管理命令，只给主人和 CC，不是查询工具。

- **冻结**（D12）：主人用管理命令手动标，模块不自动冻结。冻结的事实 L5 不自动替代；同组比它晚说的别的值写成待确认
  （pending_reason frozen_conflict），每一对进一次待确认队列。解除冻结以后照常按新旧。外部内容后来说的不替代主人原话
  （D204）：外部那条也这样待确认，列出来写成“和主人原话的冲突（外部内容说的）”，用新值不碰冻结。
- **待确认队列**（D6）：管理命令看，在会话里看细节；每天一条数量提醒（javmem review --remind，部署方每天跑）；
  30 天没人处理就两边都留（decided_by 是 auto）。
- 处理一条：和冻结的冲突可以留原值、用新值、两边都留；实体有歧义现在只能两边都留（合并到 J3.6）；
  证据都撤回了的冻结事实可以放回来、删掉，或者接着藏着。

改账本的都先拿收件锁和写账的锁：L5 重算一组的新旧时，不能有写账、收件替换在改同一组。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from javmem.config import Settings
from javmem.intake import IntakeBusy, intake_lock
from javmem.ledger import open_ledger, transaction
from javmem.pipeline import answers
from javmem.pipeline.write import (
    _DECISIONS,
    DROP,
    EVIDENCE_WITHDRAWN,
    KEEP_BOTH,
    KEEP_FROZEN,
    KEEP_HIDDEN,
    KEEP_NEW,
    SAME_TIME,
    SOFT_VERDICTS,
    LedgerWriter,
    WriteBusy,
    WriteLock,
    _review_type,
)
from javmem.timeutil import iso, parse_aware

EXPIRE_DAYS = 30  # D6：30 天没人处理就两边都留
TASK_REMIND = "review_remind"

KIND_TEXT = {
    "frozen_conflict": "和冻结的冲突",
    "ambiguous_entity": "实体有歧义",
    EVIDENCE_WITHDRAWN: "证据都撤回了的冻结事实、冻结关系",
    SAME_TIME: "同一时刻两个（A 或 B）",
    "soft_change": "软变化（还没判、判不了的）",
    "witness_mismatch": "两个证人对不上",
    "manual_review": "转人工（财务投资的；判断 C 拿不准的关系，J4.4；进图前检查拿不准、财务投资判不了的事实，J5.3）",
    "correction": "纠正（财务投资的关系被纠正，先挡着等你说照改，J4.6）",
    "correction_unmatched": "纠正（抽取没抽出来、对上了一条现行关系，问你是不是说它，J4.6）",
    "correction_other": "纠正（只说了新的那一条，问你旧的那一条还算不算，J4.6）",
    "other": "其他",
}
OWNER_WORDS_TEXT = "和主人原话的冲突（外部内容说的）"  # D204：种类照旧是 frozen_conflict，列出来时这样写
CHOICES = {KEEP_FROZEN: "frozen", KEEP_NEW: "new", KEEP_BOTH: "both", KEEP_HIDDEN: "hidden"}


class ReviewError(Exception):
    """点名的事实、待确认不在库里，或者不能这样定。什么都没动。"""


@dataclass
class ReviewItem:
    """一条没处理的待确认。detail 里有事实的文字，是给主人在会话里看的，不进日志。"""

    review_id: int
    kind: str  # 细分以后的种类（other 里分出 evidence_withdrawn）
    created_at: str
    age_days: int
    choices: list[str]
    detail: dict[str, object] = field(default_factory=dict)
    code: str = ""  # 这一条的编号（D83：Bot 问的时候写在问话里；--decide 也认它）
    answered: dict[str, object] | None = None  # 主人在对话里答了、没认出来的那一笔（问话、回答是哪一句）

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def owner_words(item: ReviewItem) -> bool:
    """和冻结的冲突里，原值那条是没冻结的事实：主人原话说的那种（D204，外部内容后来说的不替代它）。"""
    original = item.detail.get("frozen")
    return (item.kind == "frozen_conflict" and isinstance(original, dict) and not original.get("frozen")
            and not str(original.get("id") or "").startswith("R"))


def _fact(conn: sqlite3.Connection, fact_id: object) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT f.fact_id, f.content, f.attribute, f.value, f.status, f.frozen, f.as_of, f.superseded_by, f.withdrawn_at,"
        " e.canonical_name AS subject FROM facts AS f LEFT JOIN entities AS e ON e.entity_id = f.subject_entity_id"
        " WHERE f.fact_id = ?",
        (fact_id,),
    ).fetchone()
    if row is None:
        return None
    evidence = [r[0] for r in conn.execute("SELECT sentence_id FROM evidence WHERE fact_id = ? ORDER BY evidence_id", (fact_id,))]
    return {
        "id": row["fact_id"],
        "content": row["content"],
        "subject": row["subject"],
        "attribute": row["attribute"],
        "value": row["value"],
        "status": row["status"],
        "frozen": bool(row["frozen"]),
        "as_of": row["as_of"],
        "hidden": row["withdrawn_at"] is not None,
        "evidence": evidence,
    }


def _relation(conn: sqlite3.Connection, relation_id: object) -> dict[str, object] | None:
    """一条关系给主人看的样子（J4.4）：主体、关系、客体写成一句，加上状态、日子、证据句。"""
    row = conn.execute(
        "SELECT r.*, s.canonical_name AS subject, o.canonical_name AS object FROM relations AS r"
        " JOIN entities AS s ON s.entity_id = r.subject_entity_id JOIN entities AS o ON o.entity_id = r.object_entity_id"
        " WHERE r.relation_id = ?",
        (relation_id,),
    ).fetchone()
    if row is None:
        return None
    evidence = [r[0] for r in conn.execute(
        "SELECT sentence_id FROM relation_evidence WHERE relation_id = ? ORDER BY evidence_id", (relation_id,))]
    kind = f"{row['relation_type']}（{row['kinship']}）" if row["kinship"] else row["relation_type"]
    return {
        "id": row["relation_id"],
        "content": f"{row['subject']} {kind} {row['object']}",
        "state": row["state"],
        "pause_condition": row["pause_condition"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "status": row["status"],
        "frozen": bool(row["frozen"]),
        "as_of": row["as_of"],
        "superseded_by": row["superseded_by"],
        "hidden": row["withdrawn_at"] is not None,
        "evidence": evidence,
    }


def _item(conn: sqlite3.Connection, item_id: object) -> dict[str, object] | None:
    """待确认里提到的一条：关系（R 开头）或者事实。"""
    return _relation(conn, item_id) if isinstance(item_id, str) and item_id.startswith("R") else _fact(conn, item_id)


def _detail(conn: sqlite3.Connection, kind: str, claims: dict[str, object]) -> dict[str, object]:
    if kind == "frozen_conflict":
        return {"frozen": _item(conn, claims.get("frozen")), "new": _item(conn, claims.get("new"))}
    if kind == "ambiguous_entity":
        names = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT entity_id, canonical_name FROM entities WHERE entity_id IN (SELECT value FROM json_each(?))",
                (json.dumps(claims.get("candidates") or []),),
            )
        }
        pending = conn.execute(
            "SELECT count(*) FROM facts WHERE subject_entity_id = ? AND pending_reason = 'ambiguous_entity'", (claims.get("entity_id"),)
        ).fetchone()[0]
        relations = conn.execute(  # 一头是这个实体的关系也是待确认（J4.4）
            "SELECT count(*) FROM relations WHERE pending_reason = 'ambiguous_entity'"
            " AND (subject_entity_id = ?1 OR object_entity_id = ?1)",
            (claims.get("entity_id"),),
        ).fetchone()[0]
        return {
            "entity_id": claims.get("entity_id"),
            "name": claims.get("name"),
            "type": claims.get("type"),
            "candidates": [{"id": cid, "name": names.get(cid)} for cid in claims.get("candidates") or []],
            "pending_facts": pending,
            "pending_relations": relations,
        }
    if kind == SAME_TIME:  # 同一时间一个的关系，同一句话里说了两个（R12，J4.4）
        return {"relations": [_relation(conn, item) for item in claims.get("relations") or []]}  # type: ignore[union-attr]
    if kind == "manual_review" and claims.get("item") == "fact":
        # 进图前检查转人工的事实（J5.3）：那一条事实、转人工的那一处说法（文字、属性、值）、为什么、哪一句
        claim = conn.execute("SELECT content, attribute, value, judge_hold FROM fact_claims WHERE claim_id = ?",
                             (claims.get("claim"),)).fetchone()
        return {
            "fact": _fact(conn, claims.get("fact")),
            "claim": dict(claim) if claim is not None else None,
            "reason": claims.get("reason"),
            "sentence": claims.get("sentence"),
        }
    if kind in ("manual_review", "correction", "correction_unmatched", "correction_other") and "relation" in claims:
        # 判断 C 拿不准的关系（J4.4）；纠正挡着的、抽取没抽出来的纠正（J4.6）：那一条和纠正那一句
        return {"relation": _relation(conn, claims.get("relation")), "reason": claims.get("reason"), "sentence": claims.get("sentence")}
    if kind == "soft_change":  # 软变化先挡住的那一处（J4.4 第 4 段）：挡住的、旧的、模型的建议
        name = "relation" if claims.get("item") == "relation" else "fact"
        return {
            "item": name,
            name: _item(conn, claims.get(name)),
            "sentence": claims.get("sentence"),
            "old": _item(conn, claims.get("old")) if claims.get("old") else None,
            "suggestion": claims.get("suggestion"),
            "reasons": claims.get("reasons") or [],
        }
    if kind == EVIDENCE_WITHDRAWN:
        if "relation" in claims:  # 证据都撤回了的冻结关系（J4.4）
            return {"relation": _relation(conn, claims.get("relation")), "packets": claims.get("packets") or []}
        return {"fact": _fact(conn, claims.get("fact")), "packets": claims.get("packets") or []}
    return {"claims": claims}


def open_items(conn: sqlite3.Connection, settings: Settings) -> list[ReviewItem]:
    """没处理的待确认，先来的在前。"""
    now = settings.now()
    items = []
    for row in conn.execute(
        "SELECT review_id, kind, claims, created_at, code, answer FROM review_queue WHERE decided_at IS NULL"
        " ORDER BY created_at, review_id"
    ):
        claims = json.loads(row["claims"])
        claims = claims if isinstance(claims, dict) else {}
        kind = _review_type(row["kind"], claims)
        created = parse_aware(row["created_at"])
        age = max((now - created).days, 0) if created is not None else 0
        choices = [CHOICES.get(decision, decision) for decision in _DECISIONS.get(kind, ()) if decision != DROP]
        if DROP in _DECISIONS.get(kind, ()):
            choices.append("drop")
        answered = json.loads(row["answer"]) if row["answer"] else None
        items.append(
            ReviewItem(int(row["review_id"]), kind, row["created_at"], age, choices, _detail(conn, kind, claims),
                       code=str(row["code"] or answers.code_of(kind, claims)),  # 写账时补；还没补的现算（一样的算法）
                       answered=answered if isinstance(answered, dict) else None)
        )
    return items


def resolve(conn: sqlite3.Connection, reference: int | str) -> int:
    """--decide 点名的那一条：编号（数字）或者 D83 的编号（Q 开头）。Q 开头的只认没处理的那几条；没有、几条撞了的抛
    ReviewError。"""
    if isinstance(reference, int) or str(reference).strip().isdigit():
        return int(reference)
    code = str(reference).strip().upper()
    found = []
    for row in conn.execute("SELECT review_id, kind, claims, code FROM review_queue WHERE decided_at IS NULL ORDER BY review_id"):
        claims = json.loads(row["claims"])
        claims = claims if isinstance(claims, dict) else {}
        if str(row["code"] or answers.code_of(_review_type(row["kind"], claims), claims)) == code:
            found.append(int(row["review_id"]))
    if len(found) != 1:
        raise ReviewError(f"没处理的待确认里没有编号是 {code} 的" if not found else f"几条待确认的编号都是 {code}，用 # 后面的数字点名")
    return found[0]


def counts(items: Sequence[ReviewItem]) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in items:
        found[item.kind] = found.get(item.kind, 0) + 1
    return dict(sorted(found.items()))


# ---------- 改账本的 ----------


def _writer(conn: sqlite3.Connection) -> LedgerWriter:
    return LedgerWriter(conn, extracted_by="", embedding_model="", register=False)


def freeze(settings: Settings, library_name: str, fact_ids: Sequence[str], *, frozen: bool = True) -> list[str]:
    """冻结或者解除冻结。点名的可以是事实（F 开头），也可以是关系（R 开头，J4.4）。点名的有一个不在库里、或者藏着的
    （证据都撤回了，先处理那条待确认），什么都不动。返回改了的。拿不到锁抛 IntakeBusy、WriteBusy。"""
    data_dir = settings.require_data_dir()
    with intake_lock(data_dir), WriteLock(data_dir):
        conn = open_ledger(settings.library(library_name))
        try:
            rows = {
                fact_id: conn.execute(
                    "SELECT frozen, withdrawn_at FROM facts WHERE fact_id = ?1"
                    " UNION ALL SELECT frozen, withdrawn_at FROM relations WHERE relation_id = ?1",
                    (fact_id,),
                ).fetchone()
                for fact_id in dict.fromkeys(fact_ids)
            }
            unknown = [fact_id for fact_id, row in rows.items() if row is None]
            if unknown:
                raise ReviewError(f"库里没有这些事实、关系：{'、'.join(unknown)}")
            hidden = [fact_id for fact_id, row in rows.items() if row["withdrawn_at"] is not None]
            if hidden:
                raise ReviewError(f"这些藏着（证据都撤回了），先用 javmem review 处理那条待确认：{'、'.join(hidden)}")
            changed = [fact_id for fact_id, row in rows.items() if bool(row["frozen"]) != frozen]
            now = iso(settings.now())
            with transaction(conn):
                writer = _writer(conn)
                for fact_id in changed:
                    writer.set_frozen(fact_id, frozen, now)
            return changed
        finally:
            conn.close()


def decide(
    settings: Settings, library_name: str, review_id: int | str, choice: str, *, at: str | None = None,
    condition: str | None = None,
) -> int:
    """处理一条待确认。review_id 是编号，或者 D83 的编号（Q 开头）。choice 是 frozen、new、both、hidden、drop（命令行的
    写法）；软变化的（J4.4 第 4 段）答六选一的 A 到 E（at 是从哪天起，condition 是暂停等什么）。返回定的是哪一条。"""
    decision = {"frozen": KEEP_FROZEN, "new": KEEP_NEW, "both": KEEP_BOTH, "hidden": KEEP_HIDDEN, "drop": DROP}.get(choice)
    if decision is None and choice in SOFT_VERDICTS:
        decision = choice
    if decision is None:
        raise ReviewError(f"不认识的决定：{choice}")
    data_dir = settings.require_data_dir()
    with intake_lock(data_dir), WriteLock(data_dir):
        conn = open_ledger(settings.library(library_name))
        try:
            with transaction(conn):
                number = resolve(conn, review_id)
                try:
                    _writer(conn).decide_review(number, decision, iso(settings.now()), at=at, condition=condition)
                except ValueError as error:
                    raise ReviewError(str(error)) from None
        finally:
            conn.close()
    return number


@dataclass
class Reminder:
    library: str
    open: int
    by_kind: dict[str, int]
    oldest_days: int
    expired: list[int]  # 这次按 30 天的规矩关掉的
    skipped_expiry: bool = False  # 写账或者收件在跑，这次没关过期的
    shown: bool = True  # 今天还没提醒过（或者 --force），要打印这一条
    unclear: int = 0  # 主人在对话里答了、没认出来的（D83）

    def line(self) -> str:
        kinds = "，".join(f"{KIND_TEXT.get(kind, kind)} {count}" for kind, count in self.by_kind.items())
        text = f"[{self.library}] 待确认 {self.open} 条"
        if self.open:
            text += f"：{kinds}；最早的已经 {self.oldest_days} 天，javmem review --library {self.library} 看细节"
        if self.unclear:
            text += f"；你在对话里答了没认出来的 {self.unclear} 条，照答的意思用 --decide 定"
        if self.expired:
            text += (f"。超过 {EXPIRE_DAYS} 天没人处理的 {len(self.expired)} 条照规矩关掉了（冲突、同一句话里两个是两边都留，"
                     "软变化当成不改，证据都撤回了的、判断 C 拿不准的、纠正挡着的接着搁着）")
        if self.skipped_expiry:
            text += "。写账或者收件正在跑，这次没关过期的，明天再关"
        return text

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def remind(settings: Settings, library_name: str, *, force: bool = False) -> Reminder:
    """每天一条数量提醒（D6）：先把建了 30 天还没人管的按两边都留关掉，再数还剩几条。

    今天（JAVMEM_TIMEZONE 的日期）已经提醒过的，shown 是 False，命令行不打印；force 照样打印。
    写账、收件在跑的时候不关过期的（要改账本），只数。
    """
    library = settings.library(library_name)
    data_dir = settings.require_data_dir()
    now = settings.now()
    expired: list[int] = []
    skipped = False
    try:
        with intake_lock(data_dir), WriteLock(data_dir):
            conn = open_ledger(library)
            try:
                with transaction(conn):
                    expired = _writer(conn).expire_reviews(iso(now), iso(now - timedelta(days=EXPIRE_DAYS)))
            finally:
                conn.close()
    except (IntakeBusy, WriteBusy):
        skipped = True
    conn = open_ledger(library)
    try:
        items = open_items(conn, settings)
        last = conn.execute("SELECT started_at FROM task_runs WHERE task = ?", (TASK_REMIND,)).fetchone()
        last_day = parse_aware(last["started_at"]).astimezone(settings.timezone).date() if last is not None else None
        shown = force or last_day != now.date()
        if shown:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO task_runs (task, started_at, finished_at) VALUES (?, ?, ?)"
                    " ON CONFLICT (task) DO UPDATE SET started_at = excluded.started_at, finished_at = excluded.finished_at",
                    (TASK_REMIND, iso(now), iso(now)),
                )
    finally:
        conn.close()
    return Reminder(
        library_name,
        len(items),
        counts(items),
        max((item.age_days for item in items), default=0),
        expired,
        skipped,
        shown,
        sum(1 for item in items if item.answered is not None),
    )
