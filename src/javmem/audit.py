"""javmem audit（J5.3，设计稿 01 p4 的留痕表、02 p6）：只读地查一条从抽出来到进图，每一级谁判的、判成什么、P 多少、依据哪几句。

- item_trail：一条事实（F 开头）、关系（R 开头）或者一处说法（“窗口编号#L3 键前 12 位#第几条”，关系的判定那几行写成
  “…#R第几条”）。事实、关系按它现在的几处说法找。列出：留痕表里判断 C（relation_judge）、事实的判断（fact_judge）每一档，
  转人工和主人定的（precheck）；隔离表里规则、判断拦下的；现在的样子（状态、谁认的、在不在图里）。
- packet_trail：一个包的留痕和隔离，按种类数，加上每一行。
- summary：整本账按种类、哪一级、判成什么出数；事实、关系按状态、谁认的出数。
- 都不抄原文：with_text 才从原文层把依据那几句的原文带上（在本机看，不进日志）。

只读：调用方给只读连接（javmem.ledger.connect_readonly），这里一句写的都没有。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any


class AuditError(Exception):
    """点名的编号在这本账里一样都找不到。"""


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _split_claim(claim_id: str) -> tuple[str, str, str] | None:
    """说法编号 → (窗口编号, L3 键前 12 位, 第几条)。窗口编号里本来就有 #：从右边拆。"""
    parts = claim_id.rsplit("#", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    return parts[0], parts[1], parts[2]


def _relation_audit_id(claim_id: str) -> str:
    """关系说法的编号 → 判断 C 留痕里那一行的编号（第几条前面加 R）。"""
    split = _split_claim(claim_id)
    return claim_id if split is None else f"{split[0]}#{split[1]}#R{split[2].removeprefix('R')}"


def _audit(conn: sqlite3.Connection, ids: Iterable[str]) -> list[dict[str, Any]]:
    wanted = sorted(set(ids))
    rows: list[dict[str, Any]] = []
    for start in range(0, len(wanted), 500):
        part = wanted[start : start + 500]
        marks = ", ".join("?" * len(part))
        rows += [dict(row) for row in conn.execute(
            "SELECT audit_id, check_kind, item_kind, item_id, trusted, basis, packet_id, revision, probability, created_at, tier,"
            f" judge FROM audit_log WHERE item_id IN ({marks}) ORDER BY audit_id", part)]
    for row in rows:
        row["basis"] = json.loads(row["basis"]) if row["basis"] else []
    return sorted(rows, key=lambda row: row["audit_id"])


def _quarantined(conn: sqlite3.Connection, kind: str, claim_id: str) -> list[dict[str, Any]]:
    """这一处说法在隔离表里的行（规则、判断拦下的）：按窗口编号和第几条认。payload 里有原文，只回原因、规则、版本。"""
    split = _split_claim(claim_id)
    if split is None:
        return []
    window_id, _, index = split
    rows = conn.execute(
        "SELECT quarantine_id, packet_id, revision, item_kind, reason, rule_id, rule_version, created_at FROM quarantine"
        " WHERE window_id = ? AND item_kind = ? AND json_extract(payload, '$.index') = ? ORDER BY quarantine_id",
        (window_id, kind, int(index.removeprefix("R"))),
    ).fetchall()
    return [dict(row) for row in rows]


def _in_graph(conn: sqlite3.Connection, kind: str, item_id: str) -> bool | None:
    """在不在图里（照账本记的 graph_items，J5.2）。这本账没接上图的是 None。"""
    if not _has_table(conn, "graph_state"):
        return None
    state = conn.execute("SELECT enabled FROM graph_state WHERE id = 1").fetchone()
    if state is None or not state[0]:
        return None
    return conn.execute(
        "SELECT 1 FROM graph_items WHERE kind = ? AND item_id = ? AND fingerprint <> ''", (kind, item_id)
    ).fetchone() is not None


def _texts(conn: sqlite3.Connection, sentence_ids: Iterable[str]) -> dict[str, str | None]:
    """依据那几句的原文（现行版本、没撤回的；撤回了的是 None）。只在 with_text 时用。"""
    found: dict[str, str | None] = {}
    for sentence_id in sorted(set(sentence_ids)):
        row = conn.execute(
            "SELECT s.text FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            " WHERE s.sentence_id = ? AND p.state = 'current' AND s.withdrawn_at IS NULL AND p.withdrawn_at IS NULL LIMIT 1",
            (sentence_id,),
        ).fetchone()
        found[sentence_id] = row[0] if row is not None else None
    return found


def item_trail(conn: sqlite3.Connection, item: str, *, with_text: bool = False) -> dict[str, Any]:
    """一条事实、关系或者一处说法的经过（见模块说明）。一样都找不到抛 AuditError。"""
    item = item.strip()
    out: dict[str, Any] = {"item": item}
    claims: list[tuple[str, str]] = []  # (fact 或 relation, 说法编号)
    if item.startswith("F"):
        row = conn.execute(
            "SELECT fact_id, status, pending_reason, judged_by, frozen, withdrawn_at, superseded_by FROM facts WHERE fact_id = ?",
            (item,),
        ).fetchone()
        if row is not None:
            out["kind"] = "fact"
            out["now"] = {**dict(row), "in_graph": _in_graph(conn, "fact", item)}
            claims = [("fact", str(r[0])) for r in conn.execute(
                "SELECT claim_id FROM fact_claims WHERE fact_id = ? ORDER BY claim_id", (item,))]
    elif item.startswith("R"):
        row = conn.execute(
            "SELECT relation_id, status, pending_reason, judged_by, frozen, withdrawn_at, superseded_by, state FROM relations"
            " WHERE relation_id = ?",
            (item,),
        ).fetchone()
        if row is not None:
            out["kind"] = "relation"
            out["now"] = {**dict(row), "in_graph": _in_graph(conn, "relation", item)}
            claims = [("relation", str(r[0])) for r in conn.execute(
                "SELECT claim_id FROM relation_claims WHERE relation_id = ? ORDER BY claim_id", (item,))]
    if "kind" not in out:  # 说法编号：先当事实的说法、关系的说法找；都没有的（挡下的）照留痕、隔离表找
        split = _split_claim(item)
        if split is None:
            raise AuditError(f"这本账里没有 {item}：写事实（F 开头）、关系（R 开头）或者说法的编号（窗口编号#键#第几条）")
        bare = f"{split[0]}#{split[1]}#{split[2].removeprefix('R')}"
        fact = conn.execute("SELECT fact_id, judge_hold, judged_by FROM fact_claims WHERE claim_id = ?", (bare,)).fetchone()
        relation = conn.execute(
            "SELECT relation_id, judge_hold, judged_by FROM relation_claims WHERE claim_id = ?", (bare,)
        ).fetchone()
        if split[2].startswith("R") or (relation is not None and fact is None):
            out["kind"] = "relation_claim"
            claims = [("relation", bare)]
            if relation is not None:
                out["claim"] = dict(relation)
        else:
            out["kind"] = "fact_claim"
            claims = [("fact", bare)]
            if fact is not None:
                out["claim"] = dict(fact)
    ids = [claim if kind == "fact" else _relation_audit_id(claim) for kind, claim in claims]
    out["claims"] = [claim for _, claim in claims]
    out["audit"] = _audit(conn, ids)
    out["quarantine"] = [row for kind, claim in claims for row in _quarantined(conn, kind, claim)]
    if out["kind"] in ("fact_claim", "relation_claim") and not out["audit"] and not out["quarantine"] and "claim" not in out:
        raise AuditError(f"这本账里没有 {item}：说法、留痕、隔离表里都找不到")
    if with_text:
        out["texts"] = _texts(conn, [sentence for row in out["audit"] for sentence in row["basis"]])
    return out


def packet_trail(conn: sqlite3.Connection, packet: str) -> dict[str, Any]:
    """一个包的留痕（按种类、哪一级、判成什么数，再加上每一行）和隔离表里拦下的（按种类、原因数）。不抄原文。"""
    audit = [dict(row) for row in conn.execute(
        "SELECT audit_id, check_kind, item_kind, item_id, trusted, basis, revision, probability, created_at, tier, judge"
        " FROM audit_log WHERE packet_id = ? ORDER BY audit_id", (packet,))]
    for row in audit:
        row["basis"] = json.loads(row["basis"]) if row["basis"] else []
    quarantine = conn.execute(
        "SELECT item_kind, reason, rule_id, count(*) FROM quarantine WHERE packet_id = ? GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
        (packet,),
    ).fetchall()
    if not audit and not quarantine and conn.execute("SELECT 1 FROM packets WHERE packet_id = ?", (packet,)).fetchone() is None:
        raise AuditError(f"这本账里没有包 {packet}")
    counts: dict[str, int] = {}
    for row in audit:
        key = f"{row['check_kind']}/{row['tier']}/{row['trusted']}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "packet": packet,
        "audit_counts": dict(sorted(counts.items())),
        "quarantine": [{"item_kind": row[0], "reason": row[1], "rule_id": row[2], "count": row[3]} for row in quarantine],
        "audit": audit,
    }


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """整本账：留痕按种类、哪一级、判成什么；隔离表按种类、规则；事实、关系按状态（待确认的再分原因）、谁认的。"""

    def grouped(sql: str) -> list[list[object]]:
        return [list(row) for row in conn.execute(sql)]

    return {
        "audit": grouped("SELECT check_kind, item_kind, tier, trusted, count(*) FROM audit_log GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4"),
        "quarantine": grouped("SELECT item_kind, rule_id, count(*) FROM quarantine GROUP BY 1, 2 ORDER BY 1, 2"),
        "facts": grouped(
            "SELECT status, coalesce(pending_reason, ''), coalesce(judged_by, ''), count(*) FROM facts WHERE withdrawn_at IS NULL"
            " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ),
        "relations": grouped(
            "SELECT status, coalesce(pending_reason, ''), coalesce(judged_by, ''), count(*) FROM relations WHERE withdrawn_at IS NULL"
            " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ),
    }


_TIER_TEXT = {"rule": "规则", "judge": "判断模型（本机）", "model": "大模型（第二档）", "owner": "主人"}
_KIND_TEXT = {"relation_judge": "判断 C（关系）", "fact_judge": "事实的判断", "precheck": "进图前检查"}


def describe(row: dict[str, Any]) -> str:
    """留痕的一行写成一句话（不抄原文）。"""
    p = row.get("probability")
    p_text = f"，P={p:.3f}" if isinstance(p, float) else ""
    basis = row.get("basis") or []
    return (f"{row['created_at']} {_KIND_TEXT.get(row['check_kind'], row['check_kind'])}：{_TIER_TEXT.get(row['tier'], row['tier'])}"
            f"（{row['judge']}）判成 {row['trusted']}{p_text}；依据 {'、'.join(basis) if basis else '-'}")


def lines(trail: dict[str, Any]) -> Sequence[str]:
    """item_trail 写成几行给人看。"""
    out = [f"{trail['item']}（{trail['kind']}）"]
    now = trail.get("now")
    if isinstance(now, dict):
        graph = {True: "在图里", False: "不在图里", None: "这本账没接上图"}[now.get("in_graph")]
        reason = f"（{now['pending_reason']}）" if now.get("pending_reason") else ""
        out.append(f"  现在：{now.get('status')}{reason}，谁认的 {now.get('judged_by') or '没认过'}"
                   f"{'，冻结' if now.get('frozen') else ''}{'，藏着' if now.get('withdrawn_at') else ''}，{graph}")
    claim = trail.get("claim")
    if isinstance(claim, dict):
        out.append(f"  这一处说法：谁认的 {claim.get('judged_by') or '没认过'}"
                   + (f"，转人工（{claim['judge_hold']}）" if claim.get("judge_hold") else ""))
    if trail.get("claims") and trail["kind"] in ("fact", "relation"):
        out.append(f"  说法 {len(trail['claims'])} 处：{'、'.join(trail['claims'])}")
    for row in trail.get("quarantine") or []:
        out.append(f"  {row['created_at']} 隔离表：{row['rule_id']} 没过（{row['reason']}），核的方式 {row['rule_version']}")
    for row in trail.get("audit") or []:
        out.append("  " + describe(row))
    for sentence, text in sorted((trail.get("texts") or {}).items()):
        out.append(f"  〔{sentence}〕{text if text is not None else '（撤回了、不在现行版本里）'}")
    if not trail.get("audit") and not trail.get("quarantine"):
        out.append("  留痕表、隔离表里都没有：这一条写账时没开判断 C、进图前检查，或者是主人定的")
    return out
