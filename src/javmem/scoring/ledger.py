"""javmem 的账本转成对齐格式（J3.8）。只读：账本、L3 的结果库都只读打开，不升级、不写。

- **事实：** 证据落在点名的包里的、没撤回的，加上被它们替代的旧事实（证据在别的包也带上，变化的前一头要认得出来）。
  主体、属性、值、时间、说话人照账本；确定程度取最早那一处说法的
  （stated → 原话，inferred → 推断，vague → 含糊）；被替代的 state 写“被替代”，待确认的写“待确认”。
- **实体：** 账本里全部的实体，带没撤回的别名（别名对不对齐要看整本账，不只看这几包）。
- **关系（J4.4）：** L5 写关系的账本（规则表里登记了 `L5-relation` 开头的规则）从账本读：证据落在点名的包里的、没撤回的
  关系，加上被它们替代的旧关系（和事实一样，变化的前一头要认得出来）。**一处说法一条**（`relation_claims`：过了 L4、两头
  对好了实体的），和以前从结果库读的一样细：同一条关系合成一条、分段是打分自己合（D115，match.merge_relations），
  “当成新开始”这类看每一条日子的比法也照以前的样子看。每一条的两头照账本的实体，日子、状态照这一处说法（精度取开始的，
  没写开始的取结束的，和 L3 的 `time_precision` 一样），确定程度照这一处的 L3 写法。没有说法的冻结关系照关系那一行读。
  没登记这几条规则的账本（J4.4 以前写的）一条都不读，关系照旧从结果库读（javmem.scoring.results），打出来和以前一样。
- **状态变化：** 事实被替代的（superseded_by）打分时自己算成“前后变了”（javmem.scoring.match.fact_changes）。关系被 L5
  替代的（同一时间一个的新来旧失效，J4.4）照对齐格式记成这一边自己写的变化：前一头是被替代那一段最后说的那一处，后一头是
  替代它的那一段最早说的那一处，出处是变的那一句（和事实一样用 match.first_after）；关系结束、暂停照旧由打分自己算
  （match.relation_changes）。
- **成本：** 给了 L3 的结果库的，算每包的调用次数、token、秒数：每个窗口编号取账本写的那一份（账本的说法记着窗口
  和键；这个窗口一条说法都没有的，取最后切的那一份），这个键的每一次调用都算（失败、重试也算），一个窗口跨几个包时
  按要抽的句子的字数分摊，和 `javmem extract --summary` 的算法一样；同一个键只算在第一个窗口上。L4 的模型复核也记在
  结果库里的，一起算（见 packet_costs）。
- **只读、不留东西：** 账本、结果库旁边没有 -wal 的用 immutable 打开，不建 -shm、-wal（open_quietly）。
- **抽过哪些包：** 现行的版本、抽取写完了（done）、没撤回的。收下了还在排队的不算抽过；版本号也记下，打分时和金标准标的那一版比。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping
from pathlib import Path

from javmem.ledger import connect_readonly
from javmem.pipeline.merge import RELATION_RULE_PREFIX
from javmem.scoring.common import Change, Cost, Entity, Fact, Ref, Relation, Side, When
from javmem.scoring.gold import SentenceIndex

_CERTAINTY = {"stated": "原话", "inferred": "推断", "vague": "含糊"}
_TYPES = ("人", "公司或机构", "项目", "订单或合同", "地点", "资产", "Bot")
_RELATION_STATES = {"active": "生效", "paused": "暂停", "ended": "已结束"}
_PRECISIONS = ("minute", "day", "month", "quarter", "year", "unknown")


def open_quietly(path: Path) -> sqlite3.Connection:
    """只读打开，旁边不留东西：WAL 的库照常只读打开时，SQLite 会在旁边建 -shm（有时还有 -wal），关了也还在。
    旁边没有 -wal（或者是空的）的，说明没人在写，用 immutable 打开，什么都不建；有 -wal 的（有人在写，或者没合进去），
    只能照常只读打开，读得到 -wal 里的东西。"""
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        return connect_readonly(path)
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True, autocommit=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _chunks(values: list[str], size: int = 400) -> list[list[str]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def writes_relations(conn: sqlite3.Connection) -> bool:
    """这本账的窗口是不是都是 L5 写关系的代码（J4.4 起）写的：规则表里登记了 L5 的关系规则，而且没有比登记还早写的
    窗口（事实的说法、隔离表都没有更早的）。J4.4 以前的账本关系表是空的，关系只在结果库里，打分照旧从结果库读；
    以前的账本用新代码接着写（写账一开头就登记规则），以前那些窗口的关系还只在结果库里，也照旧从结果库读（审阅第 5 条），
    整本重建以后才从账本读。"""
    if not _has_table(conn, "relations") or not _has_table(conn, "rules"):
        return False
    since = conn.execute(
        "SELECT min(julianday(created_at)) FROM rules WHERE layer = 'L5' AND substr(rule_id, 1, ?) = ?",
        (len(RELATION_RULE_PREFIX), RELATION_RULE_PREFIX),
    ).fetchone()[0]
    if since is None:
        return False
    for table in ("fact_claims", "quarantine"):
        if _has_table(conn, table) and conn.execute(
            f"SELECT 1 FROM {table} WHERE julianday(created_at) < ? LIMIT 1", (since,)
        ).fetchone() is not None:
            return False
    return True


_CLAIM_STATES = {"active": "生效", "paused": "暂停", "ended": "已结束"}


def _ledger_relations(
    conn: sqlite3.Connection, wanted: list[str], entities: Mapping[str, Entity]
) -> tuple[dict[str, Relation], tuple[Change, ...]]:
    """证据落在点名的包里的、没撤回的关系，加上被它们替代的旧关系（见模块说明），一处说法一条；被 L5 替代的另记成变化。"""
    from javmem.scoring.match import first_after  # match 要用这个模块的东西，放这里免得互相导入

    relation_ids: set[str] = set()
    for chunk in _chunks(wanted):
        marks = ",".join("?" * len(chunk))
        relation_ids |= {
            row[0] for row in conn.execute(f"SELECT DISTINCT relation_id FROM relation_evidence WHERE packet_id IN ({marks})", chunk)
        }
    rows: dict[str, sqlite3.Row] = {}
    for chunk in _chunks(sorted(relation_ids)):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(  # 判断 C 拿不准、主人还没说留的不算（以前在隔离表里，也不算）
            f"SELECT * FROM relations WHERE relation_id IN ({marks}) AND withdrawn_at IS NULL"
            " AND (pending_reason IS NULL OR pending_reason <> 'manual_review')", chunk
        ):
            rows[row["relation_id"]] = row
    for chunk in _chunks(sorted(rows)):  # 被这几包里的关系替代的旧关系（证据可能在别的包）
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT * FROM relations WHERE superseded_by IN ({marks}) AND withdrawn_at IS NULL", chunk):
            rows.setdefault(row["relation_id"], row)
    claims_of: dict[str, list[sqlite3.Row]] = {}
    evidence_of: dict[str, list[str]] = {}
    for chunk in _chunks(sorted(rows)):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT * FROM relation_claims WHERE relation_id IN ({marks}) ORDER BY said_from_epoch, claim_id", chunk
        ):
            claims_of.setdefault(row["relation_id"], []).append(row)
        for row in conn.execute(
            f"SELECT relation_id, sentence_id FROM relation_evidence WHERE relation_id IN ({marks}) ORDER BY evidence_id", chunk
        ):
            listed = evidence_of.setdefault(row[0], [])
            if row[1] not in listed:
                listed.append(row[1])
    relations: dict[str, Relation] = {}
    first_of: dict[str, str] = {}  # 关系 → 它最早说的那一处（变化的后一头）
    last_of: dict[str, str] = {}  # 关系 → 它最后说的那一处（变化的前一头）
    for relation_id, row in sorted(rows.items()):
        if row["subject_entity_id"] not in entities or row["object_entity_id"] not in entities:
            continue
        claims = claims_of.get(relation_id, [])
        fields = {"kinship": row["kinship"]} if row["kinship"] else {}
        if not claims:  # 没有说法的冻结关系：照关系那一行
            precision = row["valid_from_precision"] if row["valid_from"] else row["valid_to_precision"]
            relations[relation_id] = Relation(
                relation_id, row["subject_entity_id"], row["relation_type"], row["object_entity_id"], fields,
                When(row["valid_from"], row["valid_to"], precision if precision in _PRECISIONS else "unknown"),
                _RELATION_STATES.get(row["state"], "生效"), None, tuple(evidence_of.get(relation_id, [])),
            )
            first_of[relation_id] = last_of[relation_id] = relation_id
            continue
        for claim in claims:
            precision = claim["valid_from_precision"] if claim["valid_from"] else claim["valid_to_precision"]
            if claim["kinship"]:  # 亲属照这一处自己的写法（一处说法一条，第二轮审阅小的第 4 条）
                fields = {"kinship": claim["kinship"]}
            evidence = tuple(dict.fromkeys(str(proof["sentence_id"]) for proof in json.loads(claim["proofs"])))
            relations[claim["claim_id"]] = Relation(
                claim["claim_id"], row["subject_entity_id"], row["relation_type"], row["object_entity_id"], fields,
                When(claim["valid_from"], claim["valid_to"], precision if precision in _PRECISIONS else "unknown"),
                _CLAIM_STATES.get(claim["state"], "生效"), _CERTAINTY.get(claim["certainty"]), evidence,
            )
        first_of[relation_id] = claims[0]["claim_id"]
        last_of[relation_id] = max(claims, key=lambda claim: (claim["said_to_epoch"], claim["claim_id"]))["claim_id"]
    changes = tuple(
        Change(f"rs-{relation_id}", Ref("relation", last_of[relation_id]), Ref("relation", first_of[row["superseded_by"]]),
               row["superseded_at"],
               first_after(evidence_of.get(row["superseded_by"], []), evidence_of.get(relation_id, [])))
        for relation_id, row in sorted(rows.items())
        if relation_id in last_of and row["superseded_by"] in first_of
    )
    return relations, changes


def side_from_ledger(
    ledger: Path,
    packets: Collection[str],
    *,
    store: Path | None = None,
    name: str = "javmem",
    described: Mapping[str, object] | None = None,
    also: Collection[str] = (),
) -> Side:
    """also：另外算抽过的包（结果库里它的窗口出了结果、写了账，可这个包别的窗口还没抽，账本里还在排队；
    javmem.scoring.results 给）。"""
    conn = open_quietly(ledger)
    try:
        # 抽过的包：现行的版本、抽取写完了（done）、没撤回。收下了还在排队的不算抽过（also 点名的除外）
        current: dict[str, tuple[int, str]] = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT packet_id, revision, extraction FROM packets WHERE state = 'current' AND withdrawn_at IS NULL"
            )
        }
        extra = set(also)
        revisions = {packet: revision for packet, (revision, extraction) in current.items()
                     if extraction == "done" or packet in extra}
        wanted = sorted(set(packets) & set(revisions))
        fact_ids: set[str] = set()
        for chunk in _chunks(wanted):
            marks = ",".join("?" * len(chunk))
            fact_ids |= {
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT fact_id FROM evidence WHERE fact_id IS NOT NULL AND packet_id IN ({marks})", chunk
                )
            }
        facts_rows = {}
        for chunk in _chunks(sorted(fact_ids)):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(  # 进图前检查转人工、主人还没说留的不算（和关系一样，J5.3；以前的账本没有这种事实）
                f"SELECT * FROM facts WHERE fact_id IN ({marks}) AND withdrawn_at IS NULL"
                " AND (pending_reason IS NULL OR pending_reason <> 'manual_review')", chunk
            ):
                facts_rows[row["fact_id"]] = row
        # 被这几包里的事实替代的旧事实也带上（证据可能在别的包）：变化的后一头在这几包里，前一头要认得出来
        for chunk in _chunks(sorted(facts_rows)):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(f"SELECT * FROM facts WHERE superseded_by IN ({marks}) AND withdrawn_at IS NULL", chunk):
                facts_rows.setdefault(row["fact_id"], row)
        evidence: dict[str, list[str]] = {}
        roles: dict[str, list[str]] = {}
        certainty: dict[str, str] = {}
        for chunk in _chunks(sorted(facts_rows)):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT fact_id, sentence_id FROM evidence WHERE fact_id IN ({marks}) ORDER BY evidence_id", chunk
            ):
                listed = evidence.setdefault(row[0], [])
                if row[1] not in listed:
                    listed.append(row[1])
            for row in conn.execute(
                f"SELECT fact_id, entity_id, role FROM fact_entities WHERE fact_id IN ({marks}) ORDER BY rowid", chunk
            ):
                roles.setdefault(row[0], []).append(row[1])
            for row in conn.execute(
                f"SELECT fact_id, certainty FROM fact_claims WHERE fact_id IN ({marks}) ORDER BY said_from_epoch, claim_id",
                chunk,
            ):
                certainty.setdefault(row[0], row[1])
        entities: dict[str, Entity] = {}
        aliases: dict[str, list[str]] = {}
        for row in conn.execute("SELECT entity_id, alias FROM aliases WHERE withdrawn_at IS NULL ORDER BY alias_id"):
            aliases.setdefault(row[0], []).append(row[1])
        for row in conn.execute("SELECT entity_id, canonical_name, type FROM entities ORDER BY entity_id"):
            entity_type = row[2] if row[2] in _TYPES else "项目"
            names = [one for one in aliases.get(row[0], []) if one != row[1]]
            entities[row[0]] = Entity(row[0], row[1], entity_type, tuple(dict.fromkeys(names)))
        facts: dict[str, Fact] = {}
        for fact_id, row in facts_rows.items():
            state = "被替代" if row["superseded_by"] else ("待确认" if row["status"] == "pending" else "现行")
            subject = row["subject_entity_id"] if row["subject_entity_id"] in entities else None
            facts[fact_id] = Fact(
                id=fact_id,
                text=row["content"],
                subject=subject,
                attribute=row["attribute"],
                value=row["value"],
                when=When(row["occurred_from"], row["occurred_to"], row["time_precision"] or "unknown"),
                certainty=_CERTAINTY.get(certainty.get(fact_id, "")),
                speaker=row["speaker"],
                entities=tuple(one for one in dict.fromkeys(roles.get(fact_id, [])) if one in entities),
                evidence=tuple(evidence.get(fact_id, [])),
                state=state,
                superseded_by=row["superseded_by"] if row["superseded_by"] in facts_rows else None,
            )
        written = {row[0]: row[1] for row in conn.execute("SELECT DISTINCT window_id, cache_key FROM fact_claims")}
        versions = sorted({row["extracted_by"] for row in facts_rows.values() if row["extracted_by"]})
        rules = sorted({row["rule_version"] for row in facts_rows.values() if row["rule_version"]})
        relations: dict[str, Relation] = {}
        changes: tuple[Change, ...] = ()
        from_ledger = writes_relations(conn)
        if from_ledger:  # J4.4 起：关系从账本读（过了 L4、L5 的）；以前的账本关系表是空的，照旧从结果库读
            relations, changes = _ledger_relations(conn, wanted, entities)
            for row in conn.execute("SELECT DISTINCT window_id, cache_key FROM relation_claims"):
                written.setdefault(row[0], row[1])
    finally:
        conn.close()
    info = {"账本": str(ledger), "抽取": versions, "规则": rules, **(described or {})}
    if from_ledger:
        info["关系"] = f"{len(relations)} 条（从账本读：过了 L4、L5 的），被替代的 {len(changes)} 条记成变化"
    cost = packet_costs(store, set(wanted), written=written) if store is not None else {}
    return Side(name, frozenset(wanted), entities, facts, relations, changes, cost, info,
                {packet: revisions[packet] for packet in wanted})


def sentences_from_ledger(ledger: Path, packets: Collection[str]) -> SentenceIndex:
    """邮件的句子是哪一封信、哪个往来串的（原文索引，只读）。"""
    message: dict[str, str | None] = {}
    thread: dict[str, str | None] = {}
    conn = open_quietly(ledger)
    try:
        for chunk in _chunks(sorted(packets)):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT sentence_id, message_id, thread_id FROM raw_sentences WHERE packet_id IN ({marks})", chunk
            ):
                message[row[0]] = row[1]
                thread[row[0]] = row[2]
    finally:
        conn.close()
    return SentenceIndex(message, thread)


def packet_costs(store: Path, packets: Collection[str], *, written: Mapping[str, str] | None = None) -> dict[str, Cost]:
    """每包的成本（见模块说明）。written：账本写的是哪一份（窗口编号 → 结果库的键），有的照它，没有的取最后切的那一份。
    L4 的复核：每个复核的键只算一次（几个窗口共用一个键的，算在编号排头的那个窗口上），一个窗口几个复核的键加起来；
    没挂上窗口的复核调用（比如失败了的）按调用自己记的窗口算。结果库里没有这个包的，不写。"""
    conn = open_quietly(store / "l3.db" if store.is_dir() else store)
    try:
        rows: dict[str, list[sqlite3.Row]] = {}
        for row in conn.execute("SELECT window_id, cache_key, packets, manifest, built_at FROM windows ORDER BY built_at, rowid"):
            rows.setdefault(row["window_id"], []).append(row)
        chosen: dict[str, sqlite3.Row] = {}
        for window_id, versions in rows.items():
            want = (written or {}).get(window_id)
            chosen[window_id] = next((row for row in versions if row["cache_key"] == want), versions[-1])
        usage: dict[str, Cost] = {}
        for row in conn.execute(
            "SELECT cache_key, input_tokens, output_tokens, reasoning_tokens, cached_tokens, seconds FROM calls"
        ):
            usage[row[0]] = usage.get(row[0], Cost()).plus(Cost(1, *(int(value or 0) for value in row[1:5]), float(row[5] or 0)))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        rechecks: dict[str, Cost] = {}  # 窗口编号 → 这个窗口的复核花的
        if {"recheck_windows", "recheck_calls"} <= tables:
            by_key: dict[str, Cost] = {}
            called_from: dict[str, str] = {}
            for row in conn.execute(
                "SELECT window_id, cache_key, input_tokens, output_tokens, reasoning_tokens, cached_tokens, seconds "
                "FROM recheck_calls ORDER BY window_id"
            ):
                by_key[row[1]] = by_key.get(row[1], Cost()).plus(Cost(1, *(int(v or 0) for v in row[2:6]), float(row[6] or 0)))
                called_from.setdefault(row[1], row[0])
            linked: dict[str, str] = {}
            ever_linked: set[str] = set()  # 挂过窗口的复核键（挂的可能是旧的那一份窗口）
            for row in conn.execute("SELECT window_id, cache_key, recheck_key FROM recheck_windows ORDER BY window_id"):
                ever_linked.add(row[2])
                window = chosen.get(row[0])
                if window is not None and window["cache_key"] == row[1]:
                    linked.setdefault(row[2], row[0])
            for key, spent in by_key.items():
                # 挂在算的这一份窗口上的算；一个窗口都没挂过的（比如失败了的）按调用记的窗口算；只挂在旧的那一份上的不算
                window_id = linked.get(key) if key in ever_linked else called_from.get(key)
                if window_id is not None:
                    rechecks[window_id] = rechecks.get(window_id, Cost()).plus(spent)
    finally:
        conn.close()
    out: dict[str, Cost] = {}
    counted: set[str] = set()
    for window_id, row in sorted(chosen.items()):
        listed = {one[0] for one in json.loads(row["packets"] or "[]")}
        if not listed & set(packets):
            continue
        spent = usage.get(row["cache_key"], Cost()) if row["cache_key"] not in counted else Cost()
        counted.add(row["cache_key"])
        spent = spent.plus(rechecks.get(window_id, Cost()))
        chars: dict[str, int] = {}
        for line in json.loads(row["manifest"] or "[]"):
            if str(line.get("ref", "")).startswith("T"):
                chars[line["packet_id"]] = chars.get(line["packet_id"], 0) + len(line.get("shown") or "")
        whole = sum(chars.values()) or 1
        for packet_id, count in chars.items():
            if packet_id not in packets:
                continue
            share = count / whole
            part = Cost(
                spent.calls,  # 调用次数不分摊：这个包的窗口调了几次（跨两个包的窗口，两个包各算一次）
                round(spent.input_tokens * share), round(spent.output_tokens * share),
                round(spent.reasoning_tokens * share), round(spent.cached_tokens * share),
                round(spent.seconds * share, 3),
            )
            out[packet_id] = out.get(packet_id, Cost()).plus(part)
    return out
