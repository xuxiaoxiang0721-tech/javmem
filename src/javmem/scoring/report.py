"""算分、写报告（J3.8）。

- score：一边对金标准比一遍（javmem.scoring.match），得到每一条的判定。
- metrics：判定算成数（03 p2 的五个指标，事实另按五类出数）。
- write_outputs：写进输出目录（放仓库外）：
  - `scores.json`、`分数.md`：分数。分数写明是哪一边的（要拿来定插口，D17）。
  - `对不上-给审的人.md`、`对不上.json`：金标准有、这一边没抽到的，和这一边抽到了、金标准没有的。**隐去是哪一边：**
    每一边一个代号（甲、乙……），代号和边的对照另放 `边的代号.json`，不给看的人看（03 p1）。
  - `实体对不上.md`：这一边对不上金标准的实体、有歧义的、一个实体混了几个的；金标准的实体这一边一个都没有的；
    桥（别名表）里对不上、对上几个的组。
  - `给J3.9.md`：给写提示词的会话看的（它不能看金标准）：只有错误类型和数，例子只用这一边自己抽的、对不上的那几条，
    不带金标准的原话、条目和编号。
  - `side-名字.json`：这一边转成的对齐格式，以后不用账本也能重打。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from javmem.scoring.align import (
    TABLE_ALIAS,
    Alignment,
    BridgeReport,
    add_bridges,
    align,
    gold_index,
    gold_names,
    lexicon_groups,
    table_groups,
)
from javmem.scoring.common import ENTITY_TYPES, RELATION_TYPES, Change, Cost, Relation, ScoreInputError, Side, dump_side
from javmem.scoring.dates import TOLERANCE_DAYS
from javmem.scoring.gold import (
    FACT_CLASSES,
    Gold,
    GoldItem,
    GoldRelation,
    GoldUnit,
    SentenceIndex,
    gold_relations,
    in_unit,
    is_not_labeled,
    merged_aliases,
    sentence_text,
    sid_order,
    unit_order,
)
from javmem.scoring.match import (
    HELD,
    PARTIAL,
    AliasVerdict,
    ChangeVerdict,
    EntityView,
    FactMatch,
    RelationVerdict,
    Scope,
    by_sentence,
    candidates,
    cover,
    fact_changes,
    found_back,
    judge_aliases,
    judge_changes,
    judge_relations,
    match_facts,
    merge_relations,
    overlap,
    relation_as_fact,
    relation_changes,
    side_units,
    units,
)
from javmem.pipeline.merge import name_key
from javmem.zh.lexicon import Lexicon

RESULT_FORMAT = "javmem-score-result/1"
LOOSE = ("对", "日子太粗", "日子不对", "日子没给")  # 不看日子的时候算对的
# 正式的（D112）：开始日子没写的不扣分；写了、和金标准对得上（差不过 7 天，D120；或者只写到月、季、年但对得上）的算对；
# 写了、差 7 天以上的（日子不对）才算错。严格的只认“对”（没写、写粗了都算错）。
OFFICIAL = ("对", "日子太粗", "日子没给")
CODES = "甲乙丙丁戊己庚辛"


@dataclass
class Options:
    tolerance_days: int = TOLERANCE_DAYS  # D120：7 天
    only_units: Sequence[str] | None = None


@dataclass
class SideResult:
    side: Side
    scored: list[str]
    skipped: dict[str, str]
    alignment: Alignment
    bridges: BridgeReport
    view: EntityView
    names: Mapping[str, Mapping[str, int]] = field(default_factory=dict)  # 金标准的比对写法 → {实体: 对得多准}
    demoted: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)  # 实体表里让给拆出来的实体的叫法
    facts: list[FactMatch] = field(default_factory=list)
    pairs: list[FactMatch] = field(default_factory=list)
    side_facts: dict[str, list[str]] = field(default_factory=dict)  # 单元 → 这个单元里算准不准的这一边的事实
    matched: set[str] = field(default_factory=set)
    relation_facts: dict[str, str] = field(default_factory=dict)  # 金标准关系条目 → 抽成了的那条事实
    relations: list[GoldRelation] = field(default_factory=list)  # 范围里的金标准关系（合过的）
    relation_verdicts: list[RelationVerdict] = field(default_factory=list)  # 每条单独判的
    relation_hits: dict[tuple[str, str, str], list[RelationVerdict]] = field(default_factory=dict)
    # D115：这一边两头、关系一样的先合成一条再判（正式的数照这个）：合成的关系、合成的 → 原来的、判定
    merged_relations: dict[str, Relation] = field(default_factory=dict)
    merged_members: dict[str, tuple[str, ...]] = field(default_factory=dict)
    merged_verdicts: list[RelationVerdict] = field(default_factory=list)
    merged_hits: dict[tuple[str, str, str], list[RelationVerdict]] = field(default_factory=dict)
    changes: list[ChangeVerdict] = field(default_factory=list)
    aliases: list[AliasVerdict] = field(default_factory=list)
    cost: dict[str, Cost] = field(default_factory=dict)
    side_changes: int = 0  # 这一边记下的变化（自己写的、事实被替代、关系结束或换了一头）
    held: set[str] = field(default_factory=set)  # 这一边的事实落在金标准“在改”的条目上：不算对也不算错
    change_items: dict[str, Change] = field(default_factory=dict)  # 这一边的变化（编号 → 变化），给例子用
    not_required: list[str] = field(default_factory=list)  # 金标准的条目证据全在不标的句子里：不要求抽到


def merge_sides(base: Side, extra: Side) -> Side:
    """把另写的一份（比如 J3.9 先放在 L3 结果里的关系）并进这一边。编号重了的报错。抽过哪些包照原来那一边的。"""
    for kind in ("entities", "facts", "relations"):
        mine, theirs = getattr(base, kind), getattr(extra, kind)
        clash = set(mine) & set(theirs)
        if kind == "entities":
            clash = {one for one in clash if mine[one] != theirs[one]}
        if clash:
            raise ScoreInputError(f"并进来的 {kind} 有 {len(clash)} 个编号和原来的重了")
    return replace(
        base,
        packets=base.packets,  # 并进来的只加内容，不算这一边多抽了哪些包
        entities={**base.entities, **extra.entities},
        facts={**base.facts, **extra.facts},
        relations={**base.relations, **extra.relations},
        changes=base.changes + extra.changes,
        cost={**base.cost, **{k: v for k, v in extra.cost.items() if k not in base.cost}},
        described={**base.described, "并进来的": extra.described},
    )


def _touches_pending(unit: GoldUnit, pending: frozenset[str], sentences: SentenceIndex | None) -> bool:
    """金标准这个单元的条目，有出处落在这一边还没抽完的句子上（不标的句子、不在这个单元里的不算）。
    只看金标准的出处：一个包里别的往来串的窗口没抽完，不耽误这个往来串打分。"""
    return any(
        sid in pending and not is_not_labeled(unit, sid) and in_unit(unit, sid, sentences) is not False
        for item in unit.items for sid in item.sids
    )


def _weights(side: Side) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fact in side.facts.values():
        for one in {fact.subject, *fact.entities} - {None}:
            counts[one] = counts.get(one, 0) + 1  # type: ignore[index]
    for relation in side.relations.values():
        for one in (relation.subject, relation.object):
            counts[one] = counts.get(one, 0) + 1
    return counts


def bridge_groups(lexicons: Sequence[tuple[str, Lexicon]] = (), tables: Sequence[tuple[str, str]] = ()) -> list[tuple[str, tuple[str, ...]]]:
    """桥：别名表（第一版、第二版）的组，加上 Markdown 表格（认人认事的底表）的“标准名 | 叫法”。"""
    groups = [group for origin, lexicon in lexicons for group in lexicon_groups(lexicon, origin)]
    return groups + [group for origin, text in tables for group in table_groups(text, origin)]


def score(side: Side, gold: Gold, *, groups: Sequence[tuple[str, tuple[str, ...]]] = (),
          sentences: SentenceIndex | None = None, options: Options | None = None) -> SideResult:
    options = options or Options()
    index = gold_index(gold)
    bridge_report = add_bridges(index, groups)
    alignment = align(side, gold, index, _weights(side))
    view = EntityView(alignment, gold_names(gold, index))
    scored: list[str] = []
    skipped: dict[str, str] = {}
    for uid, unit in gold.units.items():
        if options.only_units is not None and uid not in options.only_units:
            skipped[uid] = "这次没点它"
        elif not set(unit.packets) <= side.packets:
            skipped[uid] = "这一边没抽过这个单元的包（没收下、还在排队，或者撤回了）"
        elif side.revisions and any(
            packet in unit.revisions and packet in side.revisions and unit.revisions[packet] != side.revisions[packet]
            for packet in unit.packets
        ):
            skipped[uid] = "这一边抽的包的版本和金标准标的那一版不一样"
        elif not unit.is_dialog and sentences is None:
            skipped[uid] = "邮件单元要原文账本（--sentences）才分得出是哪一串的句子"
        elif side.pending and _touches_pending(unit, side.pending, sentences):
            skipped[uid] = "这一边有窗口还没抽完：这个单元的句子落在还没有结果（或者结果还没写进账本）的窗口里"
        else:
            scored.append(uid)
    result = SideResult(side, scored, skipped, alignment, bridge_report, view, names=index.entries, demoted=index.demoted)
    # 金标准的条目，证据全在不标的句子里的不要求抽到（说明第五节：没抽到不扣分），单列
    units_used: dict[str, GoldUnit] = {}
    for uid, unit in gold.units.items():
        kept = []
        for item in unit.items:
            if item.sids and all(is_not_labeled(unit, sid) for sid in item.sids):
                if uid in scored:
                    result.not_required.append(item.id)
            else:
                kept.append(item)
        units_used[uid] = replace(unit, items=tuple(kept))
    pending = [item for item in gold.pending if item.unit in scored]
    held_relations = [(item.subject[0], item.relation or "", item.object[0]) for item in pending
                      if item.type == "relation" and item.subject and item.object]
    scopes = {uid: Scope(units_used[uid], sentences) for uid in scored}
    every_relation = gold_relations(units_used.values(), tolerance_days=options.tolerance_days, order=unit_order(units_used))
    by_item = {item.id: relation for relation in every_relation for item in relation.items}
    in_scope = [relation for relation in every_relation if relation.units & set(scored)]
    result.relations = in_scope
    every_change = (list(side.changes) + fact_changes(side.facts, side.changes)
                    + relation_changes(side.relations, tolerance_days=options.tolerance_days))
    result.side_changes = len(every_change)
    result.change_items = {change.id: change for change in every_change}
    for uid in scored:
        unit, scope = units_used[uid], scopes[uid]
        mine = [fact for fact in side.facts.values() if scope.touched(fact.evidence)]
        countable = [fact.id for fact in mine if scope.labeled(fact.evidence)]
        result.side_facts[uid] = countable
        local = by_sentence(mine)
        facts = [item for item in unit.items if item.type == "fact"]
        best, pairs = match_facts(facts, side, view, local)
        result.facts += best
        result.pairs += pairs
        result.matched |= {pair.side for pair in pairs if pair.side}
        for item in unit.items:
            if item.type == "relation":
                found = relation_as_fact(item, side, view, local)
                if found:
                    result.relation_facts[item.id] = found
                    result.matched.add(found)
            elif item.type == "change" and item.change is not None:
                after = item.role_sids("after") or item.sids
                ref = units(" ".join(str(part) for part in (item.change.get("after"), item.content) if part))
                for fact_id in candidates(after, *local):
                    fact = side.facts[fact_id]
                    if overlap(fact.evidence, after) and (cover(ref, side_units(fact)) or 0) >= PARTIAL:
                        result.matched.add(fact_id)
        for fact_id in countable:
            if fact_id not in result.matched and found_back(side.facts[fact_id], unit.items):
                result.matched.add(fact_id)
        mine_pending = [item for item in pending if item.unit == uid]
        for fact_id in list(countable):  # 金标准这一条审的时候改了、还没写成结构：落在它上面的不算对也不算错
            fact = side.facts[fact_id]
            if fact_id not in result.matched and any(overlap(fact.evidence, item.sids) == 2 for item in mine_pending):
                result.held.add(fact_id)
                countable.remove(fact_id)
        changes = [change for change in every_change if scope.touched(_change_sids(side, change))]
        result.changes += judge_changes([item for item in unit.items if item.type == "change"], side, changes, by_item,
                                        tolerance_days=options.tolerance_days, view=view)
    scored_set = set(scored)
    relations = [relation for relation in side.relations.values()
                 if _relation_in_scope(relation, scopes, picked=options.only_units is not None)]
    result.relation_verdicts, result.relation_hits = judge_relations(relations, in_scope, view,
                                                                     tolerance_days=options.tolerance_days, held=held_relations)
    result.merged_relations, result.merged_members = merge_relations(relations, view)
    result.merged_verdicts, result.merged_hits = judge_relations(result.merged_relations.values(), in_scope, view,
                                                                 tolerance_days=options.tolerance_days, held=held_relations)
    merged = merged_aliases(unit for uid, unit in units_used.items() if uid in scored_set)
    result.aliases = judge_aliases(merged, alignment, index.entries)
    for uid in scored:
        for packet in gold.units[uid].packets:
            if packet in side.cost:
                result.cost[packet] = side.cost[packet]
    return result


def _change_sids(side: Side, change: Change) -> tuple[str, ...]:
    out = list(change.evidence)
    for ref in (change.before, change.after):
        if ref is None:
            continue
        item = side.facts.get(ref.id) if ref.kind == "fact" else side.relations.get(ref.id)
        if item is not None:
            out += list(item.evidence)
    return tuple(out)


def _relation_in_scope(relation, scopes: Mapping[str, Scope], *, picked: bool = False) -> bool:
    """出处落在打分的单元里（不标的句子不算）的关系。没写出处的：整个打的时候算；只点了几个单元的时候不算（分不出是哪个单元的）。"""
    if not relation.evidence:
        return not picked
    return any(scope.labeled(relation.evidence) for scope in scopes.values())


# ---------- 数 ----------


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _fact_numbers(matches: Sequence[FactMatch]) -> dict[str, object]:
    complete = sum(1 for one in matches if one.grade == "完整")
    subject_off = sum(1 for one in matches if one.grade == "主体不对")
    partial = sum(1 for one in matches if one.grade == "部分")
    return {
        "gold": len(matches),
        "complete": complete,
        "subject_off": subject_off,
        "partial": partial,
        "missed": len(matches) - complete - subject_off - partial,
        "complete_rate": _rate(complete, len(matches)),
        "content_rate": _rate(complete + subject_off, len(matches)),
        "found_rate": _rate(complete + subject_off + partial, len(matches)),
    }


def metrics(result: SideResult, gold: Gold) -> dict[str, object]:
    items = {item.id: item for item in gold.items()}
    main = [one for one in result.facts if not items[one.gold].only_in]
    only = [one for one in result.facts if items[one.gold].only_in]
    by_class = {klass: _fact_numbers([one for one in main if items[one.gold].fact_class == klass]) for klass in FACT_CLASSES}
    by_unit = {uid: _fact_numbers([one for one in main if one.unit == uid]) for uid in result.scored}
    countable = sorted({one for ids in result.side_facts.values() for one in ids})
    matched = [one for one in countable if one in result.matched]
    facts = {
        **_fact_numbers(main),
        "by_class": by_class,
        "by_unit": by_unit,
        "only_in": {flag: _fact_numbers([one for one in only if items[one.gold].only_in == flag])
                    for flag in sorted({items[one.gold].only_in for one in only})},
        "side_counted": len(countable),
        "side_matched": len(matched),
        "precision": _rate(len(matched), len(countable)),
        "side_held": len(result.held),
        "not_required": len(result.not_required),
        "side_by_unit": {uid: {"counted": len(ids), "matched": sum(1 for one in ids if one in result.matched)}
                         for uid, ids in result.side_facts.items()},
    }
    # 关系
    main_rel = [relation for relation in result.relations if not relation.only_in]

    def relation_numbers(judged_list: Sequence[RelationVerdict], hits: Mapping[tuple[str, str, str], list[RelationVerdict]]
                         ) -> tuple[dict[str, object], dict[str, list[GoldRelation]]]:
        """准确率、覆盖率：正式的（OFFICIAL，D112）、严格的（只认“对”）、不看日子的（LOOSE）。"""
        counts: dict[str, int] = {}
        for one in judged_list:
            counts[one.verdict] = counts.get(one.verdict, 0) + 1
        judged = len(judged_list) - counts.get(HELD, 0)  # 金标准那条在改的不进分母
        found = {name: [relation for relation in main_rel
                        if any(v.verdict in allowed for v in hits.get(relation.key, []))]
                 for name, allowed in (("official", OFFICIAL), ("strict", ("对",)), ("loose", LOOSE))}
        return {
            "side": judged,
            "verdicts": dict(sorted(counts.items())),
            "precision": _rate(sum(counts.get(k, 0) for k in OFFICIAL), judged),
            "precision_strict": _rate(counts.get("对", 0), judged),
            "precision_loose": _rate(sum(counts.get(k, 0) for k in LOOSE), judged),
            "recall": _rate(len(found["official"]), len(main_rel)),
            "recall_strict": _rate(len(found["strict"]), len(main_rel)),
            "recall_loose": _rate(len(found["loose"]), len(main_rel)),
        }, found

    merged_numbers, found = relation_numbers(result.merged_verdicts, result.merged_hits)  # 正式的：合成后判（D115）
    each_numbers, _ = relation_numbers(result.relation_verdicts, result.relation_hits)  # 每条单独判的也列着
    by_type: dict[str, dict[str, object]] = {}
    for relation in main_rel:
        entry = by_type.setdefault(relation.key[1], {"gold": 0, "found": 0, "found_strict": 0, "found_loose": 0, "as_fact": 0})
        entry["gold"] += 1  # type: ignore[operator]
        entry["found"] += relation in found["official"]  # type: ignore[operator]
        entry["found_strict"] += relation in found["strict"]  # type: ignore[operator]
        entry["found_loose"] += relation in found["loose"]  # type: ignore[operator]
        entry["as_fact"] += any(item.id in result.relation_facts for item in relation.items)  # type: ignore[operator]
    relations = {
        "gold": len(main_rel),
        "gold_only_in": len(result.relations) - len(main_rel),
        **merged_numbers,
        "each": each_numbers,
        "reversed": merged_numbers["verdicts"].get("方向反了", 0),  # type: ignore[attr-defined]
        "by_type": by_type,
        "as_fact": sum(1 for relation in main_rel if any(item.id in result.relation_facts for item in relation.items)),
    }
    # 状态变化（只在引用、签名、附件里的单列，不进主的数）
    main_changes = [one for one in result.changes if not items[one.gold].only_in]
    only_changes = [one for one in result.changes if items[one.gold].only_in]
    change_counts: dict[str, int] = {}
    for one in main_changes:
        change_counts[one.verdict] = change_counts.get(one.verdict, 0) + 1
    real = [one for one in main_changes if items[one.gold].change.get("is_change") is not False]  # type: ignore[union-attr]
    clear = [one for one in main_changes if items[one.gold].change.get("is_change") is False]  # type: ignore[union-attr]
    soft = [one for one in main_changes if items[one.gold].change.get("soft")]  # type: ignore[union-attr]
    by_kind: dict[str, dict[str, int]] = {}
    for one in main_changes:
        kind = str(items[one.gold].change.get("kind"))  # type: ignore[union-attr]
        entry = by_kind.setdefault(kind, {"gold": 0, "right": 0})
        entry["gold"] += 1
        entry["right"] += one.right
    changes = {
        "gold": len(main_changes),
        "right": sum(one.right for one in main_changes),
        "rate": _rate(sum(one.right for one in main_changes), len(main_changes)),
        "only_in": {"gold": len(only_changes), "right": sum(one.right for one in only_changes)},
        "real": len(real),
        "real_right": sum(one.right for one in real),
        "real_rate": _rate(sum(one.right for one in real), len(real)),
        "clarified": len(clear),
        "clarified_right": sum(one.right for one in clear),
        "soft": len(soft),
        "soft_right": sum(one.right for one in soft),
        "verdicts": dict(sorted(change_counts.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "side_changes": result.side_changes,
    }
    # 别名
    counted = [one for one in result.aliases if one.right is not None and one.key[2] not in ("不是同一个", "泛称（不建实体）")
               and not all(items[i].only_in for i in one.items)]
    alias_counts: dict[str, int] = {}
    for one in counted:
        alias_counts[one.verdict] = alias_counts.get(one.verdict, 0) + 1
    by_alias_kind: dict[str, dict[str, int]] = {}
    for one in counted:
        entry = by_alias_kind.setdefault(one.key[2], {"gold": 0, "right": 0})
        entry["gold"] += 1
        entry["right"] += bool(one.right)
    by_entity_type: dict[str, dict[str, int]] = {}
    for one in counted:
        entity_type = gold.entities[one.key[1]].type if one.key[1] in gold.entities else "金标准实体表里没有"
        entry = by_entity_type.setdefault(entity_type, {"gold": 0, "right": 0})
        entry["gold"] += 1
        entry["right"] += bool(one.right)
    apart = [one for one in result.aliases if one.key[2] == "不是同一个"]
    plain = [one for one in result.aliases if one.key[2] == "泛称（不建实体）"]
    aliases = {
        "gold": len(counted),
        "right": sum(1 for one in counted if one.right),
        "rate": _rate(sum(1 for one in counted if one.right), len(counted)),
        "verdicts": dict(sorted(alias_counts.items())),
        "by_kind": dict(sorted(by_alias_kind.items())),
        "by_entity_type": dict(sorted(by_entity_type.items())),
        "people_and_orgs_rate": _rate(
            sum(v["right"] for t, v in by_entity_type.items() if t in ("人", "公司或机构")),
            sum(v["gold"] for t, v in by_entity_type.items() if t in ("人", "公司或机构")),
        ),
        "not_same": {"gold": len(apart), "right": sum(1 for one in apart if one.right),
                     "unchecked": sum(1 for one in apart if one.right is None)},
        "plain_words": {"gold": len(plain), "right": sum(1 for one in plain if one.right)},
        "gold_conflicts": sum(1 for one in result.aliases if one.verdict == "金标准里这个叫法也对着别的实体"),
    }
    alignment = result.alignment
    entities = {
        "side": len(result.side.entities),
        "aligned": len(alignment.to_gold),
        "ambiguous": len(alignment.ambiguous),
        "unaligned": len(alignment.unaligned),
        "mixed": len(alignment.mixed),
        "gold_with_side": len(alignment.members),
        "gold_split": sum(1 for ids in alignment.members.values() if len(ids) > 1),
        "bridges_used": len(result.bridges.used),
        "bridges_ambiguous": len(result.bridges.ambiguous),
        "bridges_unmatched": len(result.bridges.unmatched),
    }
    total = Cost()
    for one in result.cost.values():
        total = total.plus(one)
    packets = len(result.cost)
    cost = {
        "packets": packets,
        "total": total.as_dict(),
        "per_packet_average": {name: round(getattr(total, name) / packets, 1) for name in
                               ("calls", "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "seconds")}
        if packets else None,
        "tokens_per_packet": round(total.tokens / packets) if packets else None,
        "per_packet": {packet: one.as_dict() for packet, one in sorted(result.cost.items())},
    }
    return {
        "described": dict(result.side.described),
        "units_scored": result.scored,
        "units_skipped": result.skipped,
        "facts": facts,
        "relations": relations,
        "changes": changes,
        "aliases": aliases,
        "entities": entities,
        "cost": cost,
    }


def gold_summary(gold: Gold, scored: Iterable[str] | None = None) -> dict[str, object]:
    wanted = set(scored) if scored is not None else set(gold.units)
    counts: dict[str, dict[str, int]] = {}
    for uid, unit in gold.units.items():
        entry = counts.setdefault(uid, {"fact": 0, "relation": 0, "change": 0, "alias": 0})
        for item in unit.items:
            entry[item.type] += 1
    return {
        "units": {uid: {"kind": unit.kind, "packets": list(unit.packets), "items": counts[uid], "scored": uid in wanted}
                  for uid, unit in gold.units.items()},
        "items": sum(sum(entry.values()) for entry in counts.values()),
        "review": gold.review.as_dict(),
        "pending_not_scored": [item.id for item in gold.pending],
    }


# ---------- 写报告 ----------


def _pct(value: object) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"  # type: ignore[arg-type]


def side_codes(names: Sequence[str]) -> dict[str, str]:
    """每一边一个代号（甲、乙……），每打一遍都重新随机（沿用的话，以前只打一边那一遍的代号就把边认出来了）。
    八边以上甲到辛用完了，接着叫“第9边”这样。对照写进输出目录的 边的代号.json，不给看对不上的人看。"""
    order = sorted(names, key=lambda name: secrets.token_hex(8))
    codes: dict[str, str] = {}
    for number, name in enumerate(order):
        codes[name] = CODES[number] if number < len(CODES) else f"第{number + 1}边"
    return codes


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def write_outputs(out: Path, gold: Gold, results: Mapping[str, SideResult]) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    scores = {name: metrics(result, gold) for name, result in results.items()}
    scored_any = sorted({uid for result in results.values() for uid in result.scored})
    payload = {
        "format": RESULT_FORMAT,
        "made_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "gold": gold_summary(gold, scored_any),
        "sides": scores,
    }
    _write(out / "scores.json", json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    _write(out / "分数.md", render_scores(payload))
    codes = side_codes(sorted(results))
    _write(out / "边的代号.json", json.dumps(codes, ensure_ascii=False, indent=1) + "\n")
    blind = mismatches(gold, results, codes)
    _write(out / "对不上.json", json.dumps(blind, ensure_ascii=False, indent=1) + "\n")
    _write(out / "对不上-给审的人.md", render_mismatches(blind))
    _write(out / "实体对不上.md", render_entities(gold, results))
    _write(out / "给J3.9.md", render_for_prompts(gold, results, scores))
    for name, result in results.items():
        dump_side(result.side, out / f"side-{_safe(name)}.json")
    return payload


def _safe(name: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in name) or hashlib.sha256(name.encode()).hexdigest()[:8]


def render_scores(payload: Mapping[str, object]) -> str:
    gold = payload["gold"]  # type: ignore[index]
    lines = ["# J3.8 分数", "", f"打分时间：{payload['made_at']}。指标照 03 p2；比法见仓库 `javmem.scoring`。", ""]
    review = gold["review"]  # type: ignore[index]
    lines += [
        "## 金标准",
        "",
        f"- 单元 {len(gold['units'])} 个，条目 {gold['items']} 条（审阅叠上以后）。"  # type: ignore[index]
        f"这一轮要审的 {review['round1']} 条，审过的 {review['round1_reviewed']} 条；抽查的一成 {review['sample']} 条，"
        f"审过 {review['sample_reviewed']} 条，判错或要改的 {review['sample_bad']} 条（{_pct(review['sample_bad_rate'])}）。",
        f"- 审阅：判错去掉的 {review['dropped']} 条，改好整条换上的 {review['replaced']} 条，改了还没写成结构、先不算分的 "
        f"{review['pending_fix']} 条，补进来的 {review['added']} 条（只有文字、先不算的 {review['pending_additions']} 条）。",
        "",
    ]
    for name, side in payload["sides"].items():  # type: ignore[union-attr]
        facts, relations, changes, aliases, cost = (side[k] for k in ("facts", "relations", "changes", "aliases", "cost"))
        lines += [f"## {name}", ""]
        lines += [f"- {k}：{v}" for k, v in side["described"].items()]
        lines += [f"- 打了分的单元：{'、'.join(side['units_scored']) or '没有'}"]
        if side["units_skipped"]:
            lines += [f"- 没打分的：" + "；".join(f"{uid}（{why}）" for uid, why in side["units_skipped"].items())]
        lines += [
            "",
            "| 指标 | 数 | 怎么算 |",
            "| --- | --- | --- |",
            f"| 关系准确率 | {_pct(relations['precision'])}（严格 {_pct(relations['precision_strict'])}、不看日子 "
            f"{_pct(relations['precision_loose'])}；每条单独判 {_pct(relations['each']['precision'])}，严格 "
            f"{_pct(relations['each']['precision_strict'])}、不看日子 {_pct(relations['each']['precision_loose'])}） | "
            f"这一边两头、关系一样的先合成一条（D115，{relations['each']['side']} 条合成 {relations['side']} 条），三头都对、"
            f"开始日子没写或者差不过 7 天的算对（D112、D120）；严格的没写、写粗了也算错 |",
            f"| 关系覆盖率 | {_pct(relations['recall'])}（严格 {_pct(relations['recall_strict'])}、不看日子 "
            f"{_pct(relations['recall_loose'])}；每条单独判 {_pct(relations['each']['recall'])}，严格 "
            f"{_pct(relations['each']['recall_strict'])}、不看日子 {_pct(relations['each']['recall_loose'])}） | "
            f"金标准 {relations['gold']} 条关系（跨单元合过）被抽到的 |",
            f"| 变化判对率 | {_pct(changes['rate'])} | 金标准 {changes['gold']} 条状态变化：真变了的 {changes['real']} 条判对 "
            f"{changes['real_right']}，说明白了、不算变化的 {changes['clarified']} 条没判错 {changes['clarified_right']} |",
            f"| 别名对齐率 | {_pct(aliases['rate'])}（人和公司 {_pct(aliases['people_and_orgs_rate'])}） | "
            f"金标准 {aliases['gold']} 个叫法对到对的实体的 |",
            f"| 每包成本 | {cost['tokens_per_packet'] if cost['tokens_per_packet'] is not None else '—'} token | "
            f"{cost['packets']} 包平均，输入加输出（推理算在输出里） |",
            f"| 事实完整抽到 | {_pct(facts['complete_rate'])}（内容完整、不管主体 {_pct(facts['content_rate'])}；"
            f"抽到一部分的另有 {facts['partial']} 条） | 金标准 {facts['gold']} 条事实（只在引用、签名、附件里的单列） |",
            f"| 事实对得上金标准 | {_pct(facts['precision'])} | 这一边 {facts['side_counted']} 条事实里对得上金标准的 |",
            "",
            "**事实按五类：**",
            "",
            "| 类 | 金标准 | 完整 | 内容完整、主体不对 | 部分 | 没抽到 | 完整的比例 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for klass, entry in facts["by_class"].items():
            lines.append(f"| {klass} | {entry['gold']} | {entry['complete']} | {entry['subject_off']} | {entry['partial']} | "
                         f"{entry['missed']} | {_pct(entry['complete_rate'])} |")
        lines += ["", "**事实按单元：**", "",
                  "| 单元 | 金标准 | 完整 | 内容完整、主体不对 | 部分 | 没抽到 | 这一边的事实 | 对得上的 |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for uid, entry in facts["by_unit"].items():
            mine = facts["side_by_unit"].get(uid, {"counted": 0, "matched": 0})
            lines.append(f"| {uid} | {entry['gold']} | {entry['complete']} | {entry['subject_off']} | {entry['partial']} | "
                         f"{entry['missed']} | {mine['counted']} | {mine['matched']} |")
        lines += ["", f"**关系：** 判定（合成后）{relations['verdicts'] or '这一边没抽关系'}；每条单独判 "
                  f"{relations['each']['verdicts'] or '—'}；方向反了 {relations['reversed']} 条；"
                  f"金标准的关系抽成了事实的（两头都提到、出处挨得上）{relations['as_fact']} 条。", "",
                  "| 关系 | 金标准 | 抽到（正式） | 抽到（严格） | 抽到（不看日子） | 抽成了事实的 |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for kind, entry in relations["by_type"].items():
            lines.append(f"| {kind} | {entry['gold']} | {entry['found']} | {entry['found_strict']} | {entry['found_loose']} | "
                         f"{entry['as_fact']} |")
        lines += ["", f"**状态变化：** {changes['verdicts']}；软变化 {changes['soft']} 条判对 {changes['soft_right']}；"
                  f"这一边记下的变化（自己写的、事实被替代、关系结束）{changes['side_changes']} 条。", ""]
        lines += [f"**别名：** {aliases['verdicts']}；“不是同一个”的 {aliases['not_same']['gold']} 条没合错 "
                  f"{aliases['not_same']['right']}（查不了 {aliases['not_same']['unchecked']}）；金标准的实体表里这个叫法也对着"
                  f"别的实体、不算分的 {aliases['gold_conflicts']} 个（明细在 `实体对不上.md`）。", "",
                  "| 种类 | 金标准 | 对 |", "| --- | ---: | ---: |"]
        for kind, entry in aliases["by_kind"].items():
            lines.append(f"| {kind} | {entry['gold']} | {entry['right']} |")
        ent = side["entities"]
        lines += ["", f"**实体：** 这一边 {ent['side']} 个，对上金标准的 {ent['aligned']}，有歧义的 {ent['ambiguous']}，"
                  f"对不上的 {ent['unaligned']}，一个实体混了几个金标准实体的 {ent['mixed']}；金标准实体在这一边分成了"
                  f"几个的 {ent['gold_split']} 个。桥（别名表）用上的组 {ent['bridges_used']}，对上几个的 {ent['bridges_ambiguous']}，"
                  f"一个都没对上的 {ent['bridges_unmatched']}（明细在 `实体对不上.md`）。", ""]
        if cost["packets"]:
            lines += ["**每包成本：**", "", "| 包 | 调用 | 输入 | 输出 | 其中推理 | 秒 |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
            for packet, entry in cost["per_packet"].items():
                lines.append(f"| {packet} | {entry['calls']} | {entry['input_tokens']} | {entry['output_tokens']} | "
                             f"{entry['reasoning_tokens']} | {entry['seconds']} |")
            lines.append("")
    return "\n".join(lines) + "\n"


def _sentences(sids: Iterable[str], texts: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"sid": sid, "text": texts.get(sid, "")} for sid in sorted(set(sids))]


def mismatches(gold: Gold, results: Mapping[str, SideResult], codes: Mapping[str, str]) -> dict[str, object]:
    """对不上的，隐去是哪一边：只写代号。金标准的条目照写（给审金标准的人看）。

    “先看”的是这一边说了、金标准里没有或者不一样的（可能是金标准漏了、标错了）；别的是这一边没抽到、没抽完整的，
    给改提示词用，不用逐条看。"""
    items = {item.id: item for item in gold.items()}
    texts = sentence_text(gold.units.values())
    unit_of = {packet: uid for uid, unit in gold.units.items() for packet in unit.packets}
    out: list[dict[str, object]] = []

    def add(code: str, unit: str, kind: str, situation: str, first: bool, sids: Iterable[str], **more: str) -> None:
        out.append({"边": code, "单元": unit, "种类": kind, "情况": situation, "先看": first,
                    **{k: v for k, v in more.items() if v}, "原文": _sentences(sids, texts)})

    for name, result in results.items():
        code = codes[name]
        for one in result.facts:
            if one.grade == "完整":
                continue
            item = items[one.gold]
            situation = {"部分": "金标准有、只抽到一部分", "主体不对": "金标准有、内容抽到了、主体不对"}.get(one.grade, "金标准有、没抽到")
            add(code, item.unit, "事实", situation, one.grade == "主体不对", item.sids, 金标准=f"[{item.id}] {item.content}",
                抽到的=result.side.facts[one.side].text if one.side else "")
        for uid, fact_id in unmatched_facts(result):
            fact = result.side.facts[fact_id]
            add(code, uid, "事实", "抽到了、金标准没有", True, fact.evidence, 抽到的=fact.text)
        hit = set(result.merged_hits)  # 正式的：合成后判的（D115）
        for relation in result.relations:
            if relation.key in hit or relation.only_in:
                continue
            first = relation.items[0]
            add(code, first.unit, "关系", "金标准有、没抽到", False, first.sids,
                金标准=f"[{'、'.join(item.id for item in relation.items)}] {relation.key[0]} —{relation.key[1]}→ {relation.key[2]}")
        for verdict in result.merged_verdicts:
            if verdict.verdict in ("对", HELD):
                continue
            relation = result.merged_relations[verdict.side]
            subject = result.side.entities[relation.subject].name
            obj = result.side.entities[relation.object].name
            unit = next((unit_of[sid.partition("#")[0]] for sid in relation.evidence if sid.partition("#")[0] in unit_of), "")
            add(code, unit, "关系", verdict.verdict, True, relation.evidence,
                抽到的=f"{subject} —{relation.relation}→ {obj}（开始 {relation.when.start or '没写'}）")
        for verdict in result.changes:
            if verdict.right:
                continue
            item = items[verdict.gold]
            add(code, item.unit, "状态变化", verdict.verdict, verdict.verdict in ("当成变了", "当成新开始"), item.sids,
                金标准=f"[{item.id}] {item.content}")
        for verdict in result.aliases:
            if verdict.right is not False:
                continue
            first = items[verdict.items[0]]
            holders = [result.side.entities[one].name for one in verdict.side]
            add(code, first.unit, "别名", verdict.verdict, verdict.verdict not in ("没认出", "没合并"), first.sids,
                金标准=f"[{'、'.join(verdict.items)}] {first.content}", 抽到的="、".join(holders))
    # 不按边排：同一个单元里按种类、出处的先后、内容排，几边交错着出现（照边排，头一段就看得出是哪一边）
    kinds = {"关系": 0, "状态变化": 1, "别名": 2, "事实": 3}

    def order(entry: dict[str, object]) -> tuple[object, ...]:
        sentences = entry.get("原文") or []
        first = min((sid_order(one["sid"]) for one in sentences), default=("", ()))  # type: ignore[index, union-attr]
        text = str(entry.get("金标准") or entry.get("抽到的") or "")
        return (str(entry["单元"]), kinds.get(str(entry["种类"]), 9), first, hashlib.sha256(text.encode()).hexdigest(),
                str(entry["边"]))

    out.sort(key=order)
    return {"说明": "金标准审完、冻结以后再看（03 p1）。代号是哪一边，不在这份里。", "条目": out}


def unmatched_facts(result: SideResult) -> list[tuple[str, str]]:
    """这一边对不上金标准的事实（单元, 事实），一条只算一次：证据落在几个单元的，记在头一个单元。"""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for uid, ids in result.side_facts.items():
        for fact_id in ids:
            if fact_id not in result.matched and fact_id not in seen:
                seen.add(fact_id)
                out.append((uid, fact_id))
    return out


def render_mismatches(blind: Mapping[str, object]) -> str:
    entries: list[dict[str, object]] = blind["条目"]  # type: ignore[assignment]
    first = [entry for entry in entries if entry["先看"]]
    rest = [entry for entry in entries if not entry["先看"]]
    lines = ["# 对不上的（隐去了是哪一边）", "", str(blind["说明"]), "",
             f"一共 {len(entries)} 条。**先看第一部分（{len(first)} 条）：** 这一边说了、金标准里没有或者不一样的，可能是金标准漏了、"
             f"标错了。每一条先看原文，再看金标准写的和抽到的，判哪边对；金标准错了的，在审阅网页上改那一条、漏了的补上。"
             f"第二部分（{len(rest)} 条）是这一边没抽到、没抽完整的，给改提示词用，不用逐条看。", ""]
    for title, chosen in (("第一部分：先看（可能是金标准漏了、标错了）", first), ("第二部分：这一边没抽到、没抽完整的", rest)):
        lines += [f"## {title}（{len(chosen)} 条）", ""]
        units = sorted({str(entry["单元"]) for entry in chosen})
        for unit in units:
            mine = [entry for entry in chosen if entry["单元"] == unit]
            lines += [f"### {unit or '（不在哪个单元）'}（{len(mine)} 条）", ""]
            for kind in ("关系", "状态变化", "别名", "事实"):
                for entry in (one for one in mine if one["种类"] == kind):
                    lines.append(f"- **{entry['边']} · {kind} · {entry['情况']}**")
                    if entry.get("金标准"):
                        lines.append(f"  - 金标准：{entry['金标准']}")
                    if entry.get("抽到的"):
                        lines.append(f"  - 抽到的：{entry['抽到的']}")
                    for sentence in entry.get("原文") or []:  # type: ignore[union-attr]
                        lines.append(f"  - `{sentence['sid']}` {sentence['text']}")
            lines.append("")
    return "\n".join(lines) + "\n"


def render_entities(gold: Gold, results: Mapping[str, SideResult]) -> str:
    lines = ["# 实体对不上的", "", "名字一样、别名表（桥）对得上的已经对上了；下面是对不上的。", ""]
    for name, result in results.items():
        alignment = result.alignment
        side = result.side
        weights = _weights(side)
        lines += [f"## {name}", ""]
        lines += [f"### 这一边对不上金标准的实体（{len(alignment.unaligned)} 个，按挂的事实多少排，只列挂了事实、关系的）", ""]
        listed = sorted((one for one in alignment.unaligned if weights.get(one)), key=lambda one: (-weights.get(one, 0), one))
        for one in listed:
            entity = side.entities[one]
            lines.append(f"- {entity.name}（{entity.type}，{weights.get(one, 0)} 条）" +
                         (f"：别名 {'、'.join(entity.aliases[:6])}" if entity.aliases else ""))
        lines += ["", f"### 有歧义的（{len(alignment.ambiguous)} 个）", ""]
        for one, choices in sorted(alignment.ambiguous.items()):
            lines.append(f"- {side.entities[one].name}（{side.entities[one].type}）→ {' / '.join(choices)}")
        lines += ["", f"### 一个实体混了几个金标准实体的（{len(alignment.mixed)} 个，可能合错了）", ""]
        for one, choices in sorted(alignment.mixed.items()):
            lines.append(f"- {side.entities[one].name} → {' / '.join(choices)}")
        lines += ["", "### 金标准的实体在这一边分成了几个的", ""]
        for gold_name, ids in sorted(alignment.members.items()):
            if len(ids) > 1:
                lines.append(f"- {gold_name}：" + "、".join(side.entities[one].name for one in sorted(ids)))
        used = {uid for uid in result.scored}
        needed: dict[str, int] = {}
        for unit in gold.units.values():
            if unit.id not in used:
                continue
            for item in unit.items:
                for pair in (item.subject, item.object):
                    if pair:
                        needed[pair[0]] = needed.get(pair[0], 0) + 1
        missing = sorted((g for g in needed if g not in alignment.members), key=lambda g: (-needed[g], g))
        lines += ["", f"### 金标准的实体这一边一个都没有的（{len(missing)} 个，按打了分的单元里用到几次排）", ""]
        lines += [f"- {g}（{gold.entities[g].type if g in gold.entities else '?'}，{needed[g]} 次）" for g in missing]
        conflicts = [one for one in result.aliases if one.verdict == "金标准里这个叫法也对着别的实体"]
        lines += ["", f"### 金标准自己两处说法不一的叫法（{len(conflicts)} 个，不算分）", "",
                  "别名条目说这个叫法是 A，金标准的实体表里它又是 B 的标准名或别名。要在金标准里统一。", ""]
        for one in conflicts:
            others = sorted(name for name, rank in result.names.get(one.key[0], {}).items()
                            if rank <= TABLE_ALIAS and name != one.key[1])
            lines.append(f"- [{'、'.join(one.items)}] 叫法“{one.key[0]}”：别名条目对到 {one.key[1]}，实体表里是 {' / '.join(others)}")
        lines += ["", f"### 金标准实体表里底表那一行还挂着、后来拆出来的实体也有的叫法（{len(result.demoted)} 个）", "",
                  "D103 定了一张一个、合称只当泛指：打分时这些叫法算拆出来的那个的；实体表里那一行的别名可以拿掉。", ""]
        lines += [f"- “{text}”：底表那一行 {old} → 让给 {' / '.join(new)}" for text, old, new in result.demoted]
        bridges = result.bridges
        renamed = [group for group in bridges.used if name_key(group.names[0]) != name_key(group.hits[0])]
        lines += ["", f"### 桥：名字不一样、对上了的（{len(renamed)} 组）", "",
                  "别名表、底表里的标准名和金标准实体表的名字不一样，按组里的别的叫法对上了（D103：拆过的底表和金标准实体表对一遍）。", ""]
        lines += [f"- {group.origin}：{group.names[0]} → 金标准 {group.hits[0]}" for group in renamed]
        lines += ["", f"### 桥：别名表里对上金标准几个实体的组（{len(bridges.ambiguous)} 组，没用）", ""]
        lines += [f"- {group.origin}：{'、'.join(group.names)} → {' / '.join(group.hits)}" for group in bridges.ambiguous]
        lines += ["", f"### 桥：别名表里一个金标准实体都没对上的组（{len(bridges.unmatched)} 组）", ""]
        lines += [f"- {group.origin}：{'、'.join(group.names)}" for group in bridges.unmatched]
        lines.append("")
    return "\n".join(lines) + "\n"


def _change_text(result: SideResult, change_id: str) -> str:
    """这一边的一条变化写成一句：前一头 → 后一头（事实写事实的话，关系写两头和关系）。说不出的返回空。"""
    change = result.change_items.get(change_id) or next((one for one in result.side.changes if one.id == change_id), None)
    if change is None:
        return ""

    def say(ref: object) -> str:
        if ref is None:
            return "（没有后一头）"
        if ref.kind == "fact":  # type: ignore[attr-defined]
            fact = result.side.facts.get(ref.id)  # type: ignore[attr-defined]
            return fact.text if fact else ""
        relation = result.side.relations.get(ref.id)  # type: ignore[attr-defined]
        if relation is None:
            return ""
        return f"{result.side.entities[relation.subject].name} —{relation.relation}→ {result.side.entities[relation.object].name}"

    return f"{say(change.before)} → {say(change.after)}"


def public_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """只出分数的那一份（给写提示词的会话）：去掉金标准的编号（审了改了、还没算分的那几条），种类只写已知的。"""
    out = json.loads(json.dumps(payload, ensure_ascii=False))
    out["gold"].pop("pending_not_scored", None)
    for unit in out["gold"]["units"].values():
        unit["kind"] = _known(unit["kind"], UNIT_KINDS)
    for side in out["sides"].values():
        for section, key, allowed in (("changes", "by_kind", CHANGE_KINDS), ("aliases", "by_kind", ALIAS_KINDS),
                                      ("relations", "by_type", RELATION_KINDS), ("aliases", "by_entity_type", TYPE_KINDS)):
            side[section][key] = _merge_known(side[section][key], allowed)
    return out


def render_headline(name: str, side: Mapping[str, object]) -> list[str]:
    """一边的几个主要的数（只有数，不带金标准的东西）。关系的正式的数是合成后判、没写开始日子算对的（D115、D112），
    严格的、不看日子的、每条单独判的一起列着。"""
    facts, relations, changes, aliases, cost = (side[k] for k in ("facts", "relations", "changes", "aliases", "cost"))
    each = relations["each"]  # type: ignore[index]
    lines = [f"## {name}：主要的数", "",
             f"- 打了分的单元：{'、'.join(side['units_scored']) or '没有'}"]  # type: ignore[arg-type]
    if side["units_skipped"]:
        lines.append("- 没打分的：" + "；".join(f"{uid}（{why}）" for uid, why in side["units_skipped"].items()))  # type: ignore[union-attr]
    lines += [
        "",
        "| 指标 | 数 |",
        "| --- | --- |",
        f"| 关系准确率 | {_pct(relations['precision'])}（合成后 {relations['side']} 条、没写开始日子算对；"  # type: ignore[index]
        f"严格 {_pct(relations['precision_strict'])}、不看日子 {_pct(relations['precision_loose'])}） |",  # type: ignore[index]
        f"| 关系准确率（每条单独判） | {_pct(each['precision'])}（{each['side']} 条；"  # type: ignore[index]
        f"严格 {_pct(each['precision_strict'])}、不看日子 {_pct(each['precision_loose'])}） |",  # type: ignore[index]
        f"| 关系覆盖率 | {_pct(relations['recall'])}（金标准 {relations['gold']} 条；"  # type: ignore[index]
        f"严格 {_pct(relations['recall_strict'])}、不看日子 {_pct(relations['recall_loose'])}） |",  # type: ignore[index]
        f"| 关系覆盖率（每条单独判） | {_pct(each['recall'])}（"  # type: ignore[index]
        f"严格 {_pct(each['recall_strict'])}、不看日子 {_pct(each['recall_loose'])}） |",  # type: ignore[index]
        f"| 变化判对率 | {_pct(changes['rate'])}（金标准 {changes['gold']} 条） |",  # type: ignore[index]
        f"| 别名对齐率 | {_pct(aliases['rate'])}（人和公司 {_pct(aliases['people_and_orgs_rate'])}） |",  # type: ignore[index]
        f"| 事实完整抽到 | {_pct(facts['complete_rate'])}（内容完整、不管主体 {_pct(facts['content_rate'])}） |",  # type: ignore[index]
        f"| 事实对得上金标准 | {_pct(facts['precision'])}（这一边 {facts['side_counted']} 条） |",  # type: ignore[index]
        f"| 每包成本 | {cost['tokens_per_packet'] if cost['tokens_per_packet'] is not None else '—'} token（{cost['packets']} 包平均） |",  # type: ignore[index]
        "",
    ]
    return lines


def scores_only(gold: Gold, results: Mapping[str, SideResult], out: Path | None = None) -> str:
    """只出分数（D111，给写提示词的会话 J3.9 自己打分）：主要的数、事实五类、按错误类型的条数，例子只用这一边自己抽错的；
    金标准的条目、原话、名字都不出。给了 out 只写这两样：分数.json（去掉了金标准的编号）、给J3.9.md（就是打印的这些）。"""
    scores = {name: metrics(result, gold) for name, result in results.items()}
    scored_any = sorted({uid for result in results.values() for uid in result.scored})
    payload = {
        "format": RESULT_FORMAT,
        "made_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "gold": gold_summary(gold, scored_any),
        "sides": scores,
    }
    lines = [f"# 分数（只有数，给写提示词的会话）", "", f"打分时间：{payload['made_at']}。金标准的条目、原话、名字都不在这里。", ""]
    for name in results:
        lines += render_headline(name, scores[name])
    text = "\n".join(lines) + "\n" + render_for_prompts(gold, results, scores)
    if out is not None:
        _write(out / "分数.json", json.dumps(public_payload(payload), ensure_ascii=False, indent=1) + "\n")
        _write(out / "给J3.9.md", text)
    return text


CHANGE_KINDS = ("决定翻过来", "计划改了", "价格变了", "换人或换渠道", "规则改了", "状态变了", "数值变了", "名字改了", "外面的事",
                "定下来了", "说明白了（不算变化）")  # 金标准变化的种类（标注规则 2.2），别的写进给 J3.9 那份时一律写“别的”
ALIAS_KINDS = ("别名", "笔误", "改名", "简称", "代码", "邮箱", "不是同一个", "泛称（不建实体）")
GOLD_RUN = 12  # 例子和金标准的内容、原话片段有这么多个字连着一样的，不放进给 J3.9 那份
GOLD_SHORT = 4  # 比 GOLD_RUN 短的金标准内容、值，有这么长的，例子里整个出现了也不放


def _fold(text: str) -> str:
    return text.replace(" ", "").rstrip("。.；;，,")


def _gold_runs(gold: Gold) -> tuple[set[str], set[str]]:
    """金标准每一条的内容、值、原话片段里，每 GOLD_RUN 个字一段；短的内容、值整个留着（比 GOLD_RUN 短的凑不成一段）。
    审过、改了还没写成结构的那几条（不算分）也算：它们还是金标准的话。"""
    runs: set[str] = set()
    shorts: set[str] = set()
    for item in (*gold.items(), *gold.pending):
        for text in (item.content, item.value or "", *(one.quote for one in item.evidence)):
            folded = _fold(text)
            runs.update(folded[start : start + GOLD_RUN] for start in range(max(0, len(folded) - GOLD_RUN + 1)))
            if GOLD_SHORT <= len(folded) < GOLD_RUN and text is not None and text != "" and text in (item.content, item.value):
                shorts.add(folded)
    return runs, shorts


def _copies_gold(text: str, guard: tuple[set[str], set[str]]) -> bool:
    runs, shorts = guard
    folded = _fold(text)
    if any(folded[start : start + GOLD_RUN] in runs for start in range(max(0, len(folded) - GOLD_RUN + 1))):
        return True
    return any(short in folded for short in shorts)


def _known(kind: str, allowed: Sequence[str]) -> str:
    return kind if kind in allowed else "别的"


def _merge_known(entries: Mapping[str, Mapping[str, object]], allowed: Sequence[str]) -> dict[str, dict[str, object]]:
    """按种类的数：种类不是已知的那几种的（金标准里写了别的标签），合进“别的”，数加起来。"""
    merged: dict[str, dict[str, object]] = {}
    for kind, entry in entries.items():
        bucket = merged.setdefault(_known(kind, allowed), {})
        for key, value in entry.items():
            bucket[key] = bucket.get(key, 0) + value  # type: ignore[operator]
    return merged


def _one_line(text: str) -> str:
    """例子写成一行（这一边的话里有换行的，接着的那一行就不当例子查了）。"""
    return " ".join(text.split())


RELATION_KINDS = RELATION_TYPES  # 金标准关系的种类，别的写“别的”
TYPE_KINDS = (*ENTITY_TYPES, "金标准实体表里没有")  # 实体的类型
UNIT_KINDS = ("dialog", "mail_thread", "mail_window")


def _kinds(entries: Mapping[str, Mapping[str, int]], allowed: Sequence[str], right: str = "right") -> str:
    merged: dict[str, list[int]] = {}
    for kind, entry in entries.items():
        bucket = merged.setdefault(_known(kind, allowed), [0, 0])
        bucket[0] += entry[right]
        bucket[1] += entry["gold"]
    return "、".join(f"{kind} {hit}/{total}" for kind, (hit, total) in merged.items())


def render_for_prompts(gold: Gold, results: Mapping[str, SideResult], scores: Mapping[str, Mapping[str, object]],
                       *, examples: int = 4) -> str:
    """给写提示词的会话（J3.9）：只按错误类型出数；例子只用这一边自己抽的、对不上的，不带金标准的原话、条目、编号。
    这一边写的例子和金标准的内容、原话片段有 GOLD_RUN 个字连着一样的、整个带着金标准短的内容或值的，也不放（照抄了金标准
    的话）。对上了金标准一条的只给数、不给例子，例子会把金标准那一条说出来：事实主体对不上的（内容对上了），关系方向反了、
    日子不对、日子太粗、日子没给、具体关系不对的（三头对上了），变化当成新开始的（那条关系金标准里有）。关系的例子只给
    金标准没有、两头对不上实体的。种类、类型只写已知的那几种，别的写“别的”。"""
    lines = ["# 给 J3.9：按错误类型的数", "",
             "写提示词的会话不能看金标准（03 p1、04 p9），这份只有数和这一边自己抽的几条例子。例子是“和金标准对不上”的，"
             "不一定都是错的（也可能是金标准漏了），看个大概。金标准的原话、条目、编号都不在这里。", ""]
    guard = _gold_runs(gold)  # 只用来挡例子，金标准自己的字不写进这一份
    for name, result in results.items():
        side_scores = scores[name]
        facts, relations, changes, aliases = (side_scores[k] for k in ("facts", "relations", "changes", "aliases"))
        lines += [f"## {name}", ""]
        lines += [f"- {k}：{v}" for k, v in side_scores["described"].items()]
        lines += ["", "### 事实", "",
                  f"完整抽到 {_pct(facts['complete_rate'])}，内容完整、主体不对的 {facts['subject_off']} 条，抽到一部分的 "
                  f"{facts['partial']} 条，没抽到 {facts['missed']} 条（金标准 {facts['gold']} 条）。这一边的事实 "
                  f"{facts['side_counted']} 条，对得上金标准的 {_pct(facts['precision'])}。", "",
                  "| 五类 | 完整 | 内容完整、主体不对 | 部分 | 没抽到 | 完整的比例 |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for klass, entry in facts["by_class"].items():
            lines.append(f"| {klass} | {entry['complete']} | {entry['subject_off']} | {entry['partial']} | {entry['missed']} | "
                         f"{_pct(entry['complete_rate'])} |")
        subject_off = sorted({pair.side for pair in result.pairs if pair.grade == "主体不对" and pair.side})
        lines += ["", f"- 内容对得上、主体对不上的这一边的事实：{len(subject_off)} 条。"]
        unmatched = unmatched_facts(result)
        lines += [f"- 和金标准对不上的这一边的事实：{len(unmatched)} 条（不标的句子里的已经去掉了）。各单元的例子（这一边自己写的）："]
        by_unit: dict[str, list[str]] = {}
        for uid, fact_id in unmatched:
            by_unit.setdefault(uid, []).append(fact_id)
        for uid in sorted(by_unit):
            pool = [one for one in by_unit[uid] if not _copies_gold(result.side.facts[one].text, guard)]
            for one in sorted(pool, key=lambda fid: hashlib.sha256(fid.encode()).hexdigest())[:examples]:
                fact = result.side.facts[one]
                subject = result.side.entities[fact.subject].name if fact.subject else "（没有主体）"
                lines.append(f"  - {uid}：{_one_line(fact.text)}（主体：{_one_line(subject)}）")
        lines += ["", "### 关系", ""]
        if relations["side"] == 0:
            lines.append("- 这一边一条关系都没抽（结果库的 L3 结果里没有 relations：l3-v1 不抽关系；提示词出了关系还是 0 条的，"
                         "看抽取那一步把 relations 存没存进结果库）。")
        else:
            each = relations["each"]
            lines += [
                f"- 判定（合成后，{each['side']} 条合成 {relations['side']} 条，D115）：{relations['verdicts']}。准确率 "
                f"{_pct(relations['precision'])}（严格 {_pct(relations['precision_strict'])}、不看日子 "
                f"{_pct(relations['precision_loose'])}），覆盖率 {_pct(relations['recall'])}（严格 {_pct(relations['recall_strict'])}、"
                f"不看日子 {_pct(relations['recall_loose'])}）。开始日子没写的算对（D112）。",
                f"- 每条单独判：{each['verdicts']}。准确率 {_pct(each['precision'])}（严格 {_pct(each['precision_strict'])}、"
                f"不看日子 {_pct(each['precision_loose'])}），覆盖率 {_pct(each['recall'])}。",
            ]
            # 只给金标准里没有对应的那几种：方向反了、日子不对这些是三头对上了金标准一条，例子会把那一条说出来
            wrong = [one for one in result.merged_verdicts if one.gold is None and one.verdict not in ("对", HELD)]
            shown = []
            for one in sorted(wrong, key=lambda v: hashlib.sha256(v.side.encode()).hexdigest()):
                relation = result.merged_relations[one.side]
                text = _one_line(f"{result.side.entities[relation.subject].name} —{relation.relation}→ "
                                 f"{result.side.entities[relation.object].name}（开始 {relation.when.start or '没写'}）")
                if not _copies_gold(text, guard):
                    shown.append(f"  - {one.verdict}：{text}")
                if len(shown) >= examples * 2:
                    break
            if shown:
                lines += ["- 这一边抽了、金标准里没有对应的几条（这一边自己写的；方向反了、日子不对这些对上了金标准一条的只给数）：",
                          *shown]
        lines += [f"- 金标准的关系被这一边抽成了事实的（两头都提到、出处挨得上）：{relations['as_fact']} 条，按关系：" +
                  _kinds(relations["by_type"], RELATION_KINDS, right="as_fact")]
        lines += ["", "### 状态变化", "", f"- 判对 {_pct(changes['rate'])}：{changes['verdicts']}。",
                  "- 按变化的种类（判对/一共）：" + _kinds(changes["by_kind"], CHANGE_KINDS),
                  f"- 这一边记下的变化（自己写的、事实被替代、关系结束）：{changes['side_changes']} 条。"]
        # 当成新开始的只给数：那条关系金标准里有（只是说明白了），例子会把它说出来
        made = sorted({one.side for one in result.changes if one.side and one.verdict == "当成变了"})
        shown = []
        for change_id in made:
            text = _one_line(_change_text(result, change_id))
            if text and not _copies_gold(text, guard):
                shown.append(f"  - {text}")
            if len(shown) >= examples:
                break
        if shown:
            lines += ["- 这一边记成了变化、其实不算变了的几条（这一边自己写的）：", *shown]
        lines += ["", "### 别名", "", f"- 对齐 {_pct(aliases['rate'])}：{aliases['verdicts']}。",
                  "- 按叫法的种类（对/一共）：" + _kinds(aliases["by_kind"], ALIAS_KINDS),
                  "- 按实体的类型（对/一共）：" + _kinds(aliases["by_entity_type"], TYPE_KINDS),
                  f"- 这一边一个实体混了几个金标准实体的：{side_scores['entities']['mixed']} 个；金标准一个实体在这一边分成几个的："
                  f"{side_scores['entities']['gold_split']} 个。"]
        lines.append("")
    return "\n".join(lines) + "\n"
