"""体检库 → 对齐格式 javmem-score/1（J3.7 导出对手结果的同一套办法），交给 j39_score.py 打分。

- 每条边当一条事实（text 是边的 fact，attribute 写边的名字）；七种关系（Graphiti 里的 HOLDS 这些，D210 加了 PART_OF）另记成
  关系，方向照边（主体 → 客体），亲属带 kinship。不在七种里的边只当事实（“全算”那个分母用得着），另外按名字数。
- 实体的类型照 Graphiti 认的那一类（Person → 人……）；没认成七类的记成“项目”（J3.7 也这么记），另外数有几个。
  主人：名字是“主人”“我”“用户”这些叫法、或者是配置里主人的名字的，加别名“主人”。
- 日子：Graphiti 每条边只存一个时刻。正好是 UTC 零点的当“只知道哪天”，别的照北京时间写到分钟（J3.7 的 when_text）。
  有结束的算已结束，被顶掉的（expired_at）算被取代。
- 出处：边记着它来自哪几个 episode（窗口）。在那个窗口里挑和这条边的事实最像、提到它两头的一两句话，再在这一两句话里
  挑最像的一两个句子（J3.7 的 narrow）；挑不出来的用整个窗口的句子（数有几条）。只挑 T 行（能当证据的），
  C 行（引用、Bot 复述这些）只帮着理解。
- 包：导出之前先扫一遍（撤回了的、换了版本的整组删，不进文件）。支线的库里有的组都算抽过的包；还没跑完的窗口（todo）
  和不发的窗口（private、打码没打干净、太长、邮件）要抽的句子写进 pending（那个单元不打分，主线这种窗口也没有结果）。
  抽不成的窗口（格式错）不算 pending：那是支线漏抽。
- 成本：每包调 Qwen 的次数（重问的也算）、输入、输出、命中缓存的 token，秒数是窗口的时间加起来。向量的另数。
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from javmem.config import Settings
from javmem.pipeline.merge import FIRST_PERSON, OWNER_NAMES, load_owner_names, name_key, owner_by_name
from javmem.scoring.common import Cost, Entity, Fact, Relation, Side, When, dump_side
from javmem.side.store import GroupRow, SideStore, now_text

CN = timezone(timedelta(hours=8))
FALLBACK_TYPE = "项目"
OWNER = "主人"
_NOISE = re.compile(r"[\s`*_#>|~\-–—•·]+")
_FRACTION = re.compile(r"(\.\d{1,6})\d*")
# 主人的叫法：“主人”这些，加上“我”（对话里主人说话的抬头就写“我”），加上 Graphiti 常写的“用户”
_OWNER_KEYS = frozenset(name_key(name) for name in (*OWNER_NAMES, *FIRST_PERSON, "用户", "user"))


def norm(text: str) -> str:
    return _NOISE.sub("", text or "").lower()


def grams(text: str) -> set[str]:
    clean = norm(text)
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"\[[^\]]*\]$", "", text)
    text = _FRACTION.sub(r"\1", text, count=1)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def when_text(moment: datetime | None) -> tuple[str | None, str]:
    """Graphiti 只存一个时刻。正好是 UTC 零点的当“只知道哪天”；别的照北京时间写到分钟。"""
    if moment is None:
        return None, "unknown"
    utc = moment.astimezone(UTC)
    if (utc.hour, utc.minute, utc.second, utc.microsecond) == (0, 0, 0, 0):
        return utc.date().isoformat(), "day"
    return moment.astimezone(CN).strftime("%Y-%m-%dT%H:%M+08:00"), "minute"


def _score(fact: set[str], keys: Sequence[str], text: str) -> tuple[float, float, int]:
    cov = len(fact & grams(text)) / len(fact) if fact else 0.0
    hits = sum(1 for key in keys if key in norm(text))
    return cov + 0.25 * hits, cov, hits


def narrow(fact_text: str, candidates: Sequence[dict[str, Any]], names: Sequence[str],
           sentence_text: dict[str, str]) -> tuple[list[str], bool]:
    """一条边的出处（J3.7 的办法）：候选是窗口里的一块一块（一句话、一封邮件），先挑和事实最像、提到两头的一两块，
    再在每块里挑最像的一两个句子（两句以内的整块给）。挑不出来的用整个窗口（第二个值 True）。"""
    fact = grams(fact_text)
    keys = [norm(name) for name in names if len(norm(name)) >= 2]
    scored = [(*_score(fact, keys, one["text"]), one) for one in candidates]
    if not scored:
        return [], True
    best = max(score for score, _, _, _ in scored)
    keep = [row for row in scored if row[0] >= 0.8 * best and (row[1] >= 0.2 or row[2] >= 1)]
    keep.sort(key=lambda row: -row[0])
    if not keep or best < 0.25:
        return [sid for one in candidates for sid in one["sids"]], True
    out: list[str] = []
    for _, _, _, one in keep[:3]:
        sids = one["sids"]
        if len(sids) <= 2:
            out += sids
            continue
        rows = [(_score(fact, keys, sentence_text.get(sid, ""))[0], sid) for sid in sids]
        top = max(score for score, _ in rows)
        if top <= 0:
            out += sids
            continue
        chosen = {sid for _, sid in sorted((row for row in rows if row[0] >= 0.5 * top), key=lambda row: -row[0])[:2]}
        out += [sid for sid in sids if sid in chosen]
    return list(dict.fromkeys(out)), False


def is_owner(name: str, owner_keys: Collection[str]) -> bool:
    return name_key(name) in _OWNER_KEYS or owner_by_name(name, owner_keys)


def _latest(groups: Iterable[GroupRow], packets: Collection[str] | None) -> list[GroupRow]:
    best: dict[str, GroupRow] = {}
    for group in groups:
        if packets is not None and group.packet_id not in packets:
            continue
        if group.packet_id not in best or group.revision > best[group.packet_id].revision:
            best[group.packet_id] = group
    return [best[packet] for packet in sorted(best)]


def build_side(groups: Sequence[GroupRow], graph_rows: dict[str, dict[str, list[dict[str, Any]]]], store: SideStore,
               owner_keys: Collection[str], *, described: dict[str, object] | None = None
               ) -> tuple[Side, list[dict[str, Any]], dict[str, Any]]:
    """纯函数（体检库那边先读好放进 graph_rows：组 → {entities, edges, episodes}），测试直接用。"""
    from javmem.side.graph import LABEL_ENTITY, LABEL_RELATION

    entities: dict[str, Entity] = {}
    facts: dict[str, Fact] = {}
    relations: dict[str, Relation] = {}
    rows_out: list[dict[str, Any]] = []
    stats: dict[str, Any] = collections.defaultdict(collections.Counter)
    pending: set[str] = set()
    cost: dict[str, Cost] = {}
    for group in groups:
        data = graph_rows.get(group.group_id, {"entities": [], "edges": [], "episodes": []})
        windows = store.windows(group.group_id)
        episode_window = {row.episode_uuid: row.window_id for row in windows if row.status == "done" and row.episode_uuid}
        for row in windows:
            stats["windows"][row.status] += 1
            if row.status == "failed":
                stats["failed_steps"][row.step or "?"] += 1
            if row.status in ("todo", "skipped"):  # 还没跑的、不发的：要抽的句子（T 行）算没跑完，单元不打分
                pending.update(line.sentence_id for line in store.lines(row.window_id) if line.ref.startswith("T"))
        candidates: dict[str, list[dict[str, Any]]] = {}
        sentence_text: dict[str, str] = {}
        for window_id in episode_window.values():
            blocks: dict[int, dict[str, Any]] = {}
            for line in store.lines(window_id):
                if not line.ref.startswith("T"):  # C 行（引用、Bot 复述这些）只帮着理解，不当证据
                    continue
                block = blocks.setdefault(line.block, {"sids": [], "text": ""})
                block["sids"].append(line.sentence_id)
                block["text"] += line.text
                sentence_text[line.sentence_id] = line.text
            candidates[window_id] = [blocks[number] for number in sorted(blocks)]
        names = {row["uuid"]: (row.get("name") or "").strip() for row in data["entities"]}
        labels = {row["uuid"]: row.get("labels") or [] for row in data["entities"]}

        def entity_id(uuid: str) -> str | None:
            name = names.get(uuid, "")
            if not name:
                return None
            if uuid not in entities:
                kinds = [LABEL_ENTITY[label] for label in labels.get(uuid, []) if label in LABEL_ENTITY]
                kind = kinds[0] if kinds else FALLBACK_TYPE
                if not kinds:
                    stats["entities"]["没认成七类，记成项目"] += 1
                aliases = (OWNER,) if is_owner(name, owner_keys) and name != OWNER else ()
                entities[uuid] = Entity(uuid, name, kind, aliases)
                stats["entities"][kind] += 1
            return uuid

        for edge in data["edges"]:
            source, target = entity_id(edge["source"]), entity_id(edge["target"])
            if source is None or target is None:
                stats["edges"]["两头有没名字的实体"] += 1
                continue
            windows_of_edge = [episode_window[uuid] for uuid in edge.get("episodes") or [] if uuid in episode_window]
            evidence: list[str] = []
            whole = False
            for window_id in windows_of_edge:
                found, fallback = narrow(edge.get("fact") or "", candidates.get(window_id, []),
                                         [names[edge["source"]], names[edge["target"]]], sentence_text)
                evidence += found
                whole |= fallback
            evidence = list(dict.fromkeys(evidence))
            if not evidence:
                stats["edges"]["对不上窗口"] += 1
                continue
            if whole:
                stats["edges"]["出处用了整个窗口"] += 1
            start, end = parse_ts(edge.get("valid_at")), parse_ts(edge.get("invalid_at"))
            expired = parse_ts(edge.get("expired_at"))
            start_text, precision = when_text(start)
            end_text, end_precision = when_text(end)
            if start is None and end is not None:
                precision = end_precision
            facts["f:" + edge["uuid"]] = Fact(
                "f:" + edge["uuid"], edge.get("fact") or edge.get("name") or "", source, edge.get("name"), None,
                When(start_text, end_text, precision), None, None, (source, target), tuple(evidence),
                "被替代" if (end or expired) else "现行",
            )
            relation = LABEL_RELATION.get(edge.get("name") or "")
            row_log = {"uuid": edge["uuid"], "group": group.group_id, "name": edge.get("name"), "fact": edge.get("fact"),
                       "source": names[edge["source"]], "target": names[edge["target"]], "relation": relation,
                       "evidence": evidence, "whole_window": whole, "valid_at": edge.get("valid_at"),
                       "invalid_at": edge.get("invalid_at"), "expired_at": edge.get("expired_at")}
            rows_out.append(row_log)
            if relation is None:
                stats["unmapped"][edge.get("name") or ""] += 1
                continue
            fields = {"kinship": str(edge["kinship"])} if relation == "亲属" and edge.get("kinship") else {}
            state = "已结束" if end is not None else ("被取代" if expired is not None else "生效")
            relations["r:" + edge["uuid"]] = Relation("r:" + edge["uuid"], source, relation, target, fields,
                                                      When(start_text, end_text, precision), state, None, tuple(evidence))
            stats["relations"][relation] += 1
        cost[group.packet_id] = _cost(store, group, windows)
        stats["embedding"]["calls"] += sum(1 for row in store.calls([group.group_id]) if row["prompt"] == "side_embed")
    side = Side(
        "graphiti-qwen", frozenset(group.packet_id for group in groups), entities, facts, relations, (), cost,
        dict(described or {}), {group.packet_id: group.revision for group in groups}, frozenset(pending),
    )
    return side, rows_out, {key: dict(value) for key, value in stats.items()}


def _cost(store: SideStore, group: GroupRow, windows: Sequence[Any]) -> Cost:
    # 只算这一组这一回的调用：同一个组名以前整组删了重来的，那几回的调用不算进这一包的成本
    calls = [row for row in store.calls([group.group_id])
             if str(row["prompt"]).startswith("side:") and str(row["at"]) >= group.started_at]
    asked = [row for row in calls if row["status"] != "format_error"]  # 每问一次记一行；格式错另记一行，不算多问一次
    ok = [row for row in calls if row["status"] == "ok"]
    seconds = sum(row.seconds or 0.0 for row in windows)
    return Cost(len(asked), sum(row["input_tokens"] for row in ok), sum(row["output_tokens"] for row in ok), 0,
                sum(row["cached_tokens"] for row in ok), round(seconds, 1))


async def export_side(graph: Any, settings: Settings, args: argparse.Namespace, store: SideStore,
                      out: Callable[[str], None]) -> int:
    from javmem.side.graph import GRAPHITI_VERSION
    from javmem.side.sweep import sweep

    # 先扫一遍：撤回了的、彻底删除了的、换了版本的不能进导出的文件（撤回还没接进 javmem withdraw 之前尤其要紧）
    swept = await sweep(settings.library(args.library).db_path, store, graph)
    for group_id, reason in swept.cleared:
        out(f"导出之前删了 {group_id}：{reason}")
    from javmem.side.witness import clean_and_say

    clean_and_say(settings.library(args.library).db_path, settings.require_data_dir(), swept.packets, out)
    groups = _latest(store.groups(), set(args.packet) if args.packet else None)
    if args.packet:
        missing = sorted(set(args.packet) - {group.packet_id for group in groups})
        if missing:
            out("支线的库里没有：" + "、".join(missing))
            return 2
    graph_rows = {group.group_id: await graph.export_group(group.group_id) for group in groups}
    described = {
        "系统": f"Graphiti {GRAPHITI_VERSION} + 本机 Qwen（{graph.settings.qwen_model}，关思考），向量是本机的 Qwen3-Embedding-4B",
        "做法": "和主线同一套切窗、打码；不带上文、不加预处理 A、B 的标注；每包一组，同一包的前 3 个窗口当上文；"
              "七类实体、七种关系当 Graphiti 的自定义类型（javmem.side.graph）",
        "导出": f"{now_text()}，javmem side export（javmem.side.export，J3.7 的出处挑法）",
        # 每组是哪时候开始抽的：compare --queue 照它认这一组抽完以后包里有没有句子撤回了（撤回了的那一组作废了）
        "groups_started": {group.packet_id: group.started_at for group in groups},
    }
    side, rows, stats = build_side(groups, graph_rows, store, load_owner_names(settings.owner_file), described=described)
    dump_side(side, Path(args.out))
    if args.edges_out:
        path = Path(args.edges_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)).encode("utf-8"))
    out(f"导出了 {len(side.packets)} 个包：实体 {len(side.entities)}，事实 {len(side.facts)}，关系 {len(side.relations)}，"
        f"还没跑完的句子 {len(side.pending)}")
    for key in ("windows", "failed_steps", "relations", "entities", "edges", "embedding"):
        if stats.get(key):
            out(f"  {key}：" + "，".join(f"{name} {count}" for name, count in sorted(stats[key].items())))
    if stats.get("unmapped"):
        top = sorted(stats["unmapped"].items(), key=lambda item: -item[1])
        out(f"  不在七种里的边 {sum(count for _, count in top)} 条，最多的：" + "，".join(f"{n} {c}" for n, c in top[:12]))
    return 0
