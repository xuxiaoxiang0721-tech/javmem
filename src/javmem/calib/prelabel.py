"""`javmem gold prelabel`（U3，D141 ③）：照账本里现在抽出来的东西，出一份金标准的草稿（预标），给人审。

- 读法和打分一样（javmem.scoring.ledger.side_from_ledger，账本只读）：点名的包里有证据的事实、关系，被它们替代的旧事实
  也带上（变化的前一头），用到的实体和别名。
- 写成 `javmem-gold-prelabel/1`（docs/gold-format.md）：事实 F、关系 R（同一条关系的几处说法合成一条，证据合在一起）、
  变化 S（事实被替代的、关系被替代的）、别名 A（出处是这几个包里第一句写着这个叫法的）。出处只写句子编号，原话片段
  空着；日子照账本的，来由写“javmem 抽的”；事实的分类空着（要人定，打分不看它）。实体表只放用到了的实体。
- 这是 javmem 自己抽的，不是标准答案：审的时候每一条都要看，漏了的要补（审阅页面的“补一条”）。拿它直接打分，
  分数一定好看，没有意义。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from javmem.scoring.gold import ENTITY_SCHEMA, GOLD_SCHEMA, split_sid

BASIS = "javmem 抽的"


def _when(when: object) -> dict[str, object]:
    found = when.as_dict()  # type: ignore[attr-defined]
    return {**found, "basis": BASIS}


def _common(item_id: str, kind: str, content: str, evidence: Sequence[dict[str, str]], when: object) -> dict[str, object]:
    return {"id": item_id, "type": kind, "content": content, "evidence": list(evidence), "when": _when(when),
            "flags": [], "refs": [], "unsure": False, "important": False, "note": ""}


def prelabel(ledger: Path, conn: sqlite3.Connection, packets: Sequence[str], unit: str) -> tuple[dict[str, object], dict[str, object]]:
    """(单元, 实体表)。conn 是同一本账本的只读连接（找别名出处的那一句）。"""
    from javmem.scoring.ledger import side_from_ledger

    side = side_from_ledger(ledger, packets)
    chosen = set(packets)
    used: dict[str, object] = {}

    def pair(entity_id: str | None) -> dict[str, str] | None:
        entity = side.entities.get(entity_id or "")
        if entity is None:
            return None
        used[entity.id] = entity
        return {"name": entity.name, "type": entity.type}

    def mine(sids: Sequence[str]) -> list[str]:
        return [sid for sid in sids if split_sid(sid)[0] in chosen]

    items: list[dict[str, object]] = []
    fact_ids: dict[str, str] = {}
    facts = sorted(side.facts.values(), key=lambda one: (sorted(mine(one.evidence))[:1], one.id))
    for fact in facts:
        evidence = sorted(mine(fact.evidence))
        if not evidence:
            continue
        item_id = f"{unit}-F{len(fact_ids) + 1:03d}"
        fact_ids[fact.id] = item_id
        item = _common(item_id, "fact", fact.text, [{"sid": sid, "quote": ""} for sid in evidence], fact.when)
        item.update(subject=pair(fact.subject), certainty=fact.certainty, speaker=fact.speaker, fact_class=None,
                    attribute=fact.attribute, value=fact.value)
        if fact.state != "现行":
            item["note"] = f"账本里这一条是“{fact.state}”"
        items.append(item)

    merged: dict[tuple[object, ...], dict[str, object]] = {}
    relation_ids: dict[str, dict[str, object]] = {}
    for relation in sorted(side.relations.values(), key=lambda one: (sorted(mine(one.evidence))[:1], one.id)):
        evidence = sorted(mine(relation.evidence))
        subject, obj = pair(relation.subject), pair(relation.object)
        if not evidence or subject is None or obj is None:
            continue
        key = (subject["name"], relation.relation, obj["name"], tuple(sorted(relation.fields.items())))
        item = merged.get(key)
        if item is None:
            item = _common("", "relation", f"{subject['name']} {relation.relation} {obj['name']}", [], relation.when)
            item.update(subject=subject, relation=relation.relation, object=obj, fields=dict(relation.fields),
                        state=relation.state, certainty=relation.certainty)
            merged[key] = item
        else:
            item["state"] = relation.state  # 后说的那一处的状态
            when = item["when"]
            if relation.when.end and (not when.get("to") or str(relation.when.end) > str(when["to"])):  # type: ignore[union-attr]
                when["to"] = relation.when.end  # type: ignore[index]
        seen = {one["sid"] for one in item["evidence"]}  # type: ignore[union-attr]
        item["evidence"] += [{"sid": sid, "quote": ""} for sid in evidence if sid not in seen]  # type: ignore[operator]
        relation_ids[relation.id] = item
    for number, item in enumerate(merged.values(), 1):
        item["id"] = f"{unit}-R{number:03d}"
        item["content"] = f"{item['content']}（{item['state']}）"
        items.append(item)

    changes = 0
    for fact in facts:
        newer = side.facts.get(fact.superseded_by or "")
        if newer is None or fact.id not in fact_ids or newer.id not in fact_ids:
            continue
        before, after = sorted(mine(fact.evidence)), sorted(mine(newer.evidence))
        changes += 1
        item = _common(f"{unit}-S{changes:03d}", "change", f"{fact.text} → {newer.text}",
                       [{"sid": before[-1], "quote": "", "role": "before"}, {"sid": after[0], "quote": "", "role": "after"}],
                       newer.when)
        item.update(subject=pair(newer.subject), links=[fact_ids[fact.id], fact_ids[newer.id]],
                    change={"kind": "变了", "soft": False, "target": newer.attribute or "", "before": fact.value or fact.text,
                            "after": newer.value or newer.text, "is_change": True})
        items.append(item)
    for change in side.changes:
        if change.before is None or change.after is None:
            continue
        old, new = relation_ids.get(change.before.id), relation_ids.get(change.after.id)
        evidence = sorted(mine(change.evidence))
        if old is None or new is None or not evidence or old is new:
            continue
        changes += 1
        item = _common(f"{unit}-S{changes:03d}", "change", f"{old['content']} → {new['content']}",
                       [{"sid": sid, "quote": "", "role": "after"} for sid in evidence], _Plain(new["when"]))
        item.update(subject=new["subject"], links=[str(old["id"]), str(new["id"])],
                    change={"kind": "换了", "soft": False, "target": str(new["relation"]),
                            "before": str(old["object"]["name"]), "after": str(new["object"]["name"]),  # type: ignore[index]
                            "is_change": True})
        items.append(item)

    sentences = conn.execute(
        f"SELECT sentence_id, text FROM raw_sentences WHERE packet_id IN ({', '.join('?' for _ in packets)})"
        " AND withdrawn_at IS NULL ORDER BY packet_id, seq", list(packets)).fetchall()
    aliases = 0
    for entity in sorted(list(used.values()), key=lambda one: one.name):  # type: ignore[attr-defined]
        for alias in sorted(entity.aliases):  # type: ignore[attr-defined]
            where = next((str(row[0]) for row in sentences if alias in str(row[1])), None)
            if where is None:
                continue
            aliases += 1
            item = _common(f"{unit}-A{aliases:03d}", "alias", f"{alias}是{entity.name}。",  # type: ignore[attr-defined]
                           [{"sid": where, "quote": alias}], _Plain({"from": None, "to": None, "precision": "unknown"}))
            item.update(subject={"name": entity.name, "type": entity.type},  # type: ignore[attr-defined]
                        alias={"mention": alias, "entity": entity.name, "entity_type": entity.type, "kind": "别名"})  # type: ignore[attr-defined]
            items.append(item)

    kinds = {str(row[0]) for row in conn.execute(
        f"SELECT DISTINCT channel FROM packets WHERE packet_id IN ({', '.join('?' for _ in packets)})", list(packets))}
    unit_json: dict[str, object] = {
        "schema": GOLD_SCHEMA, "unit": unit, "unit_kind": "mail" if kinds == {"email"} else "dialog",
        "title": f"javmem 预标：{len(packets)} 个包（要人审）", "packets": list(packets),
        "revisions": {packet: side.revisions[packet] for packet in packets if packet in side.revisions},
        "not_labeled": [], "new_entities": [], "items": items,
    }
    table = {"schema": ENTITY_SCHEMA, "entities": [
        {"name": entity.name, "type": entity.type, "aliases": sorted(entity.aliases), "from": BASIS}  # type: ignore[attr-defined]
        for entity in sorted(used.values(), key=lambda one: one.name)]}  # type: ignore[attr-defined]
    return unit_json, table


class _Plain:
    """只给 _when 用：已经是 {from, to, precision} 的字典。"""

    def __init__(self, found: object) -> None:
        self.found = dict(found) if isinstance(found, dict) else {}

    def as_dict(self) -> dict[str, object]:
        return {key: self.found.get(key) for key in ("from", "to", "precision")}
