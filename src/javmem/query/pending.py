"""和这个实体有关的待确认（D83、D178 ③）：entity_card 多一个 pending 列表，让 Bot 聊到这个人、这家公司时好顺带问。

接口文档 5.2 加的那一段（“和这个实体有关的待确认”）等主人点头再合：这个文件、entity_card 接它的那几行、文档那一段、
测试，都在 J5.5 分支的最后一个提交里，和别的分开。

- **哪几条：** 没处理的待确认里牵涉到这个实体的：事实的主体或涉及的实体，关系的两头和代表的那一家，实体有歧义的那个
  实体和它的候选。
- **按身份滤：** 牵涉的事实、关系都要还在、这个身份都看得到（账本视图的 fact_conditions、relation_conditions，入参
  partitions 也算）；挡住的那一句也要看得到；实体有歧义的照它记着的可见范围算，候选也都要看得到。证据都撤回了的冻结
  事实、冻结关系那一种不给（内容撤回了，五个工具都不再给出去）。看不到的不计数，也不露出有这么一条。和 J4.6 认主人回答
  时的算法一样：分区有一个能看就过，敏感标记每个都要能看。
- **编号：** review_queue.code；还没补的照 answers.code_of 现算（和 javmem review 一样，整本重建也不变）。查询接口只读，
  不写编号。Bot 问的时候把编号原样写进问话（packet-spec 第 7 节第 10 条），J4.6 照它认主人在答哪一条。
- **最多 5 条，先来的在前：** 30 天没人管就自动处理（D6），先来的先问。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from javmem.pipeline import answers
from javmem.query.view import LedgerView
from javmem.raw.search import SENTENCE_COLUMNS, sentence_item

MAX_PENDING = 5

# 每一种问什么（照 J4.6 认回答的对照表写，javmem.pipeline.answers）。Bot 照这个意思问，用自己的话
ASK = {
    "frozen_conflict": "记着的原值（冻结了）和新说的对不上：要不要照新说的改？",
    "ambiguous_entity": "这个名字对得上好几个：是其中哪一个，还是另一个？",
    "same_time": "同一句话里说了两个：是不是两个都对？",
    "soft_change": "这句话是不是要改：照旧、先停、不做了、改成别的，还是还没定？",
    "manual_review": "这一条拿不准：有没有这回事？",
    "correction": "这是财务、投资的，要不要照纠正改？",
    "correction_unmatched": "是不是在纠正这一条？",
    "correction_other": "只说了新的那一条：旧的那一条还算不算？",
}
# 和冻结的冲突里，原值那条没冻结、是主人原话说的那种（D204：外部内容后来说的不替代主人原话）：种类照旧，换这一句问
OWNER_WORDS_ASK = "你说过的和后来外部内容（邮件、转发、附件）说的对不上：要不要照外部说的改？"
_ABOUT = ("manual_review", "correction", "correction_unmatched", "correction_other")


def _review_type(kind: str, claims: Mapping[str, object]) -> str:
    """细分以后的种类：和写账、javmem review 用的同一个（javmem.pipeline.write._review_type）。用到才导入写账那一层。"""
    from javmem.pipeline.write import _review_type as review_type

    return review_type(kind, claims)


def _referenced(kind: str, claims: Mapping[str, object]) -> list[tuple[object, str]]:
    """这一条牵涉的事实、关系：(编号, 哪一边)。"""
    if kind == "frozen_conflict":
        return [(claims.get("frozen"), "frozen"), (claims.get("new"), "new")]
    if kind == "same_time":
        return [(one, "one") for one in claims.get("relations") or []]  # type: ignore[union-attr]
    if kind == "soft_change":
        name = "relation" if claims.get("item") == "relation" else "fact"
        found: list[tuple[object, str]] = [(claims.get(name), "held")]
        if claims.get("old"):
            found.append((claims.get("old"), "old"))
        return found
    if kind in _ABOUT:
        return [(claims.get("relation") or claims.get("fact"), "about")]
    return []


def _row(view: LedgerView, item_id: object) -> sqlite3.Row | None:
    """一条牵涉的事实、关系，这个身份看得到的才有。"""
    if not isinstance(item_id, str) or not item_id:
        return None
    if item_id.startswith("R"):
        conditions, params = view.relation_conditions("r")
        return view.conn.execute(
            f"SELECT r.* FROM relations AS r WHERE r.relation_id = ? AND {' AND '.join(conditions)}", [item_id, *params]
        ).fetchone()
    conditions, params = view.fact_conditions("f")
    return view.conn.execute(
        f"SELECT f.* FROM facts AS f WHERE f.fact_id = ? AND {' AND '.join(conditions)}", [item_id, *params]
    ).fetchone()


def _entities_of(view: LedgerView, row: sqlite3.Row) -> set[str]:
    if "relation_id" in row.keys():
        return {str(one) for one in (row["subject_entity_id"], row["object_entity_id"], row["org_entity_id"]) if one}
    found = {str(row["subject_entity_id"])} if row["subject_entity_id"] else set()
    found.update(str(one[0]) for one in view.conn.execute("SELECT entity_id FROM fact_entities WHERE fact_id = ?", (row["fact_id"],)))
    return found


def _sentence(view: LedgerView, sentence_id: object) -> sqlite3.Row | None:
    if not isinstance(sentence_id, str) or not sentence_id:
        return None
    conditions, params = view.packet_conditions("p")
    return view.conn.execute(
        f"SELECT {SENTENCE_COLUMNS} FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id"
        f" AND p.revision = s.revision WHERE s.sentence_id = ? AND s.withdrawn_at IS NULL AND {' AND '.join(conditions)}",
        [sentence_id, *params],
    ).fetchone()


def _ambiguity(view: LedgerView, entity_id: str, claims: Mapping[str, object]) -> dict[str, Any] | None:
    """实体有歧义的：它记着的可见范围、那个实体、候选都要看得到。不牵涉这个实体的是 None。"""
    candidates = [str(one) for one in claims.get("candidates") or []]  # type: ignore[union-attr]
    new = str(claims.get("entity_id") or "")
    if entity_id not in {new, *candidates}:
        return None
    try:
        scope = json.loads(str(claims.get("scope")))
        partitions, sensitive = list(scope["partitions"]), list(scope["sensitive"])
    except (ValueError, KeyError, TypeError):
        return None  # 没记范围的旧条目认不出是谁的：不给
    if not view.sees_scope(partitions, sensitive) or not view.entity_visible(new):
        return None
    if not candidates or not all(view.entity_visible(one) for one in candidates):
        return None
    return {
        "entity": {"id": new, "name": view.entity_name(new), "type": view.entity_type(new)},
        "candidates": [{"entity_id": one, "name": view.entity_name(one), "type": view.entity_type(one)} for one in candidates],
    }


def pending_for(view: LedgerView, entity_id: str) -> list[dict[str, Any]]:
    """和这个实体有关、这个身份看得到的没处理的待确认，最多 5 条，先来的在前。"""
    found: list[dict[str, Any]] = []
    for review in view.conn.execute(
        "SELECT review_id, kind, claims, code FROM review_queue WHERE decided_at IS NULL ORDER BY created_at, review_id"
    ):
        if len(found) >= MAX_PENDING:
            break
        try:
            claims = json.loads(review["claims"])
        except ValueError:
            continue
        if not isinstance(claims, dict):
            continue
        kind = _review_type(str(review["kind"]), claims)
        if kind not in ASK:
            continue  # 证据都撤回了的（内容撤回了）、认不出的种类：不给
        entry: dict[str, Any] = {"code": str(review["code"] or answers.code_of(kind, claims)), "kind": kind, "ask": ASK[kind]}
        if kind == "ambiguous_entity":
            ambiguity = _ambiguity(view, entity_id, claims)
            if ambiguity is not None:
                found.append({**entry, "items": [], **ambiguity})
            continue
        rows = [(_row(view, item_id), side) for item_id, side in _referenced(kind, claims)]
        if not rows or any(row is None for row, _side in rows):
            continue  # 牵涉的有一条看不到（或者已经没了）：整条不给
        if not any(entity_id in _entities_of(view, row) for row, _side in rows):  # type: ignore[arg-type]
            continue
        sentence = None
        if claims.get("sentence"):
            sentence = _sentence(view, claims.get("sentence"))
            if sentence is None:
                continue  # 挡住的那一句看不到：整条不给
        items = []
        for row, side in rows:
            item = view.relation_item(row) if "relation_id" in row.keys() else view.fact_item(row)  # type: ignore[union-attr]
            items.append({"id": item["id"], "kind": item["kind"], "side": side,
                          **{key: value for key, value in item.items() if key not in ("id", "kind")}})
        entry["items"] = items
        original = rows[0][0]
        if kind == "frozen_conflict" and "relation_id" not in original.keys() and not original["frozen"]:  # type: ignore[union-attr]
            entry["ask"] = OWNER_WORDS_ASK
        if sentence is not None:
            entry["sentence"] = sentence_item(view.conn, sentence)
        found.append(entry)
    return found
