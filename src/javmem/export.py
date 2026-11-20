"""事实导出（J2.13）：账本里的事实一行一条 JSON，带上来源和信任的标记，给对照、跑分和人看。

查询接口（J5.5）以前，这是看“哪个 Bot、原话还是推断、是不是外部内容”的地方：

- 每条事实：sources（证据来自哪些来源）、stated（原话直接说的，不是推断）、owner_said（证据里有主人自己的话）、
  secondhand（证据全都不是说话人自己的话：外部内容、转发、附件说明，当转述）、origins；
  说话人两列：speaker 是证据那一句是谁说的，attributed_to 是 L3 写的“这条说法出自谁”（J4.1）；
  每条证据：source、origin、speaker_role。
- 每个包：来源、框架、渠道，原文句按 origin、说话人分别有几句。邮件包整包都是外部内容。

输出里有原文（事实文字、引文），只写到命令行给的文件，放仓库外。藏起来的事实（证据都撤回了）默认不导出。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator


def facts(conn: sqlite3.Connection, *, hidden: bool = False) -> Iterator[dict[str, object]]:
    names = {row["entity_id"]: row["canonical_name"] for row in conn.execute("SELECT entity_id, canonical_name FROM entities")}
    claims = dict(conn.execute("SELECT fact_id, count(*) FROM fact_claims GROUP BY fact_id").fetchall())
    evidence: dict[str, list[dict[str, object]]] = {}
    for row in conn.execute("SELECT * FROM evidence WHERE fact_id IS NOT NULL ORDER BY evidence_id"):
        evidence.setdefault(row["fact_id"], []).append(
            {
                "sentence_id": row["sentence_id"],
                "packet_id": row["packet_id"],
                "revision": row["revision"],
                "quote": row["quote"],
                "position": json.loads(row["position"]),
                "source": row["source"],
                "origin": row["origin"],
                "speaker_role": row["speaker_role"],
            }
        )
    for row in conn.execute("SELECT * FROM facts ORDER BY as_of_epoch, fact_id"):
        if row["withdrawn_at"] is not None and not hidden:
            continue
        subject = row["subject_entity_id"]
        yield {
            "fact_id": row["fact_id"],
            "content": row["content"],
            "status": row["status"],
            "pending_reason": row["pending_reason"],
            "stated": bool(row["stated"]),
            "owner_said": bool(row["owner_said"]),
            "secondhand": bool(row["secondhand"]),
            "sources": json.loads(row["sources"]),
            "origins": json.loads(row["origins"]),
            "subject": {"id": subject, "name": names.get(subject)} if subject else None,
            "attribute": row["attribute"],
            "value": row["value"],
            "occurred_from": row["occurred_from"],
            "time_precision": row["time_precision"],
            "as_of": row["as_of"],
            "current": row["superseded_by"] is None,
            "superseded_by": row["superseded_by"],
            "superseded_at": row["superseded_at"],
            "frozen": bool(row["frozen"]),
            "hidden": row["withdrawn_at"] is not None,
            "scope": {"partitions": json.loads(row["scope_partitions"]), "sensitive": json.loads(row["scope_sensitive"])},
            "speaker": row["speaker"],  # 证据那一句是谁说的
            "speaker_role": row["speaker_role"],
            "attributed_to": row["attributed_to"],  # 这条说法出自谁（L3 写的，J4.1），以前写的是空的
            "claims": claims.get(row["fact_id"], 0),
            "notes": json.loads(row["notes"]),
            "evidence": evidence.get(row["fact_id"], []),
        }


def packets(conn: sqlite3.Connection) -> Iterator[dict[str, object]]:
    """每个包（现行的版本）：来源和原文句按 origin、说话人分别有几句。只有数，不抄原文。"""
    counts: dict[str, dict[str, dict[str, int]]] = {}
    for row in conn.execute(
        "SELECT packet_id, origin, speaker_role, count(*) AS n FROM raw_sentences GROUP BY packet_id, origin, speaker_role"
    ):
        mine = counts.setdefault(row["packet_id"], {"origins": {}, "speaker_roles": {}})
        mine["origins"][row["origin"]] = mine["origins"].get(row["origin"], 0) + row["n"]
        mine["speaker_roles"][row["speaker_role"]] = mine["speaker_roles"].get(row["speaker_role"], 0) + row["n"]
    for row in conn.execute("SELECT * FROM packets WHERE state = 'current' ORDER BY covers_from_epoch, packet_id"):
        mine = counts.get(row["packet_id"], {"origins": {}, "speaker_roles": {}})
        yield {
            "packet_id": row["packet_id"],
            "revision": row["revision"],
            "source": row["source"],
            "framework": row["framework"],
            "channel": row["channel"],
            "external": row["channel"] == "email",  # 邮件包整包都是外部内容（规范第 6 节）
            "scope": {"partitions": json.loads(row["partitions"]), "sensitive": json.loads(row["sensitive"])},
            "extraction": row["extraction"],
            "withdrawn": row["withdrawn_at"] is not None,
            "sentences_by_origin": dict(sorted(mine["origins"].items())),
            "sentences_by_speaker_role": dict(sorted(mine["speaker_roles"].items())),
        }
