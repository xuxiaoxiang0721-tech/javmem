"""一份抽取结果转成对齐格式（J3.8，D111：J3.9 每跑一遍自己打分用）。只读，不调模型。

给的可以是：
- **结果文件夹**（javmem 的数据目录）：里面有 `ledger/<库>.db`（账本）和 `l3/l3.db`（L3 的结果库），有哪个读哪个；
- **账本**（`.db`，有 facts 表；在数据目录里的，旁边的 `l3/l3.db` 一起读）；
- **结果库**（`l3.db`，或者装着它的目录）。

读法：
- **事实、实体：** 有账本的照账本（过了 L4、L5 的，javmem.scoring.ledger）；只有结果库的，取 L3 原样的事实（没过 L4、L5，
  报告里写着），主体按名字认。
- **关系：** L5 写关系的账本（J4.4 起，规则表里登记了 L5 的关系规则）从账本读（javmem.scoring.ledger），不读结果库的。
  J4.4 以前的账本关系表是空的，关系从结果库的 L3 结果里读：每个窗口的结果（`windows.extraction`）里
  `relations` 那一列。照这个样子写（J3.9 的提示词 v2 照这个出，打分就能直接读）：

      "relations": [{"subject": "e1", "relation": "持有", "object": "e2",      # 两头写顶层 entities 里的 key
                     "kinship": null,                                       # 亲属才写：主体是客体的什么人
                     "valid_from": "2026-10-13", "valid_to": null,          # 开始、结束，ISO 8601，不知道写 null
                     "time_precision": "day",                               # minute、day、month、quarter、year、unknown
                     "state": "生效",                                       # 生效、已结束、暂停、被取代；不写的，写了结束当已结束，没写当生效
                     "certainty": "stated",                                 # stated、inferred、vague
                     "evidence": [{"ref": "T3", "quote": "…"}]}]            # 和事实一样；换过编号的有 sentence_id

  宽一点：时间也认 `from`/`to`、`start`/`end`、`occurred_at`，类型也认 `type`，亲属也认 `fields.kinship`；证据有
  `sentence_id` 的用它，只有 T 编号的照这个窗口的清单换。关系不是七种之一的不算（D210 加了隶属于）；L3 换编号时
  标了问题的（`problems` 不是空的：只引了上文、编号认不出、两头落在重复的 key 上……）也不算、不进分母，和 L4 看事实一样；各有几条、为什么，
  记在报告里。同一个 key 写了几个实体的，认头一个（和 L4、L5 一样）。存下来的结果里没有
  `relations` 这一列的（抽取那一步没存），读模型原样的回答（`results.parsed`）里的，报告里记着几个窗口是这样读的。
- **用哪一份窗口：** 一个窗口编号在结果库里可能有几份（窗口的字变过）：账本写的是哪一份就用哪一份（账本的说法记着），
  账本没有的取最后切的那一份（同一个结果库里来回换着切，“最后切的”可能认错：一遍用一个新的数据目录）。
- **哪些窗口用得上：** 有结果的；有账本的，还要账本写过这个窗口（有说法、出处或者隔离的条目），或者它要抽的句子所在的
  包都抽完了（done）。L3 跑完了、还没写进账本的不用：关系、事实都从用得上的窗口来，不会关系有了、事实没有。
- **还没抽完的句子：** 用不上的窗口里要抽的句子。一个单元，金标准的出处有落在这些句子上的，不打分（一个包里别的往来串
  没抽完，不耽误这一串）。几个都没写的窗口记在报告里。
- **抽过哪些包：** 账本里抽完（done）的，加上有用得上的窗口的（只抽了几个窗口的邮件包也算：一个往来串的窗口抽完、
  写进了账本，那个包别的窗口还没抽，账本里它还在排队）。只给结果库的，有结果的都算。
- **成本：** 照结果库算（javmem.scoring.ledger.packet_costs）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from javmem.pipeline.merge import name_key
from javmem.scoring.common import RELATION_STATES, RELATION_TYPES, Entity, Fact, Relation, ScoreInputError, Side, When
from javmem.scoring.dates import infer_precision
from javmem.scoring.gold import SentenceIndex
from javmem.scoring.ledger import open_quietly, packet_costs, sentences_from_ledger, side_from_ledger, writes_relations

_CERTAINTY = {"stated": "原话", "inferred": "推断", "vague": "含糊", "原话": "原话", "推断": "推断", "含糊": "含糊"}
_STATES = {"active": "生效", "ended": "已结束", "paused": "暂停", "superseded": "被取代", **{s: s for s in RELATION_STATES}}
_TYPES = ("人", "公司或机构", "项目", "订单或合同", "地点", "资产", "Bot")


def _tables(path: Path) -> set[str]:
    conn = open_quietly(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def locate(path: Path, library: str = "test") -> tuple[Path | None, Path | None]:
    """(账本, 结果库)：给的是结果文件夹、账本还是结果库，认出来。都没有报 ScoreInputError。
    结果文件夹里有账本、只是没有点名的那个库的，也报错（不然悄悄地只用了结果库）。"""
    ledger = store = None
    if path.is_dir():
        books = path / "ledger"
        if (books / f"{library}.db").is_file():
            ledger = books / f"{library}.db"
        elif books.is_dir() and any(books.glob("*.db")):
            others = "、".join(sorted(one.stem for one in books.glob("*.db")))
            raise ScoreInputError(f"{path.name} 里没有 ledger/{library}.db（有 {others}：要换 --library 吗）")
        if (path / "l3" / "l3.db").is_file():
            store = path / "l3" / "l3.db"
        elif (path / "l3.db").is_file():
            store = path / "l3.db"
    elif path.is_file():
        tables = _tables(path)
        if "facts" in tables:
            ledger = path
            beside = path.parent.parent / "l3" / "l3.db"  # 数据目录里账本旁边的结果库：ledger\<库>.db 和 l3\l3.db
            if path.parent.name == "ledger" and beside.is_file():
                store = beside
        elif {"windows", "results"} <= tables:
            store = path
    if ledger is None and store is None:
        raise ScoreInputError(f"{path.name} 里找不到账本（ledger/{library}.db）或者 L3 的结果库（l3.db）")
    return ledger, store


@dataclass(frozen=True)
class LedgerView:
    """账本那边知道的：写过的窗口用的是结果库里哪一份（键）；写过哪些窗口（有说法、出处或者隔离的条目）；哪些包抽完了。"""

    keys: Mapping[str, str]
    touched: frozenset[str]
    done: frozenset[str]


def ledger_relations(ledger: Path) -> bool:
    """这本账是不是 L5 写关系的代码（J4.4 起）写的：是的话关系从账本读，不读结果库的（javmem.scoring.ledger）。"""
    conn = open_quietly(ledger)
    try:
        return writes_relations(conn)
    finally:
        conn.close()


def ledger_view(ledger: Path) -> LedgerView:
    conn = open_quietly(ledger)
    try:
        keys = {row[0]: row[1] for row in conn.execute(  # 同一个窗口写过几份的，取最后写的那一份
            "SELECT window_id, cache_key FROM fact_claims WHERE window_id IS NOT NULL ORDER BY created_at, rowid")}
        if writes_relations(conn):  # J4.4 起只写了关系、没有事实的窗口也算写过（J4.4 以前的账本关系表是空的）
            for row in conn.execute("SELECT window_id, cache_key FROM relation_claims ORDER BY created_at, rowid"):
                keys.setdefault(row[0], row[1])
        touched = set(keys)
        for table in ("evidence", "quarantine"):
            touched.update(row[0] for row in conn.execute(f"SELECT DISTINCT window_id FROM {table} WHERE window_id IS NOT NULL"))
        done = {row[0] for row in conn.execute(
            "SELECT packet_id FROM packets WHERE state = 'current' AND extraction = 'done' AND withdrawn_at IS NULL")}
    finally:
        conn.close()
    return LedgerView(keys, frozenset(touched), frozenset(done))


def _json_or_none(text: object) -> object:
    try:
        return json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        return None


def store_windows(store: Path, packets: Collection[str],
                  view: LedgerView | None = None) -> tuple[list[dict[str, object]], set[str], list[str]]:
    """结果库里的窗口（见模块说明“用哪一份窗口”“抽过哪些包”），只看要抽的句子落在点名的包里的。返回三样：
    - 用得上的：有结果的；有账本的（view），还要账本写过这个窗口、或者它要抽的句子所在的包都抽完了。
      每个是 {window_id, cache_key, extraction, parsed, manifest}，parsed 是模型原样的回答。
    - 还没抽完的句子：用不上的窗口里、落在点名的包里的要抽的句子（单元的金标准出处碰上了就不打分）。
    - 用不上的窗口的编号。"""
    conn = open_quietly(store)
    try:
        versions: dict[str, list[sqlite3.Row]] = {}
        for row in conn.execute(
            "SELECT window_id, cache_key, manifest, extraction, built_at FROM windows ORDER BY built_at, rowid"
        ):
            versions.setdefault(row["window_id"], []).append(row)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
        answers = {row[0]: row[1] for row in conn.execute(
            "SELECT cache_key, parsed FROM results" if "parsed" in columns else "SELECT cache_key, NULL FROM results")}
    finally:
        conn.close()
    out: list[dict[str, object]] = []
    pending: set[str] = set()
    unusable: list[str] = []
    wanted = set(packets)
    for window_id, rows in sorted(versions.items()):
        want = view.keys.get(window_id) if view is not None else None
        row = next((one for one in rows if one["cache_key"] == want), rows[-1])
        manifest = json.loads(row["manifest"] or "[]")
        targets = [line for line in manifest if str(line.get("ref", "")).startswith("T") and line.get("packet_id") in wanted]
        if not targets:
            continue
        usable = row["cache_key"] in answers and bool(row["extraction"])
        if usable and view is not None:
            usable = window_id in view.touched or all(line.get("packet_id") in view.done for line in targets)
        if not usable:
            pending.update(str(line["sentence_id"]) for line in targets if line.get("sentence_id"))
            unusable.append(window_id)
            continue
        out.append({"window_id": window_id, "cache_key": row["cache_key"], "extraction": json.loads(row["extraction"]),
                    "parsed": _json_or_none(answers[row["cache_key"]]), "manifest": manifest})
    return out, pending, unusable


def _sentence_ids(proofs: object, refs: Mapping[str, str]) -> tuple[str, ...]:
    out: list[str] = []
    for proof in proofs if isinstance(proofs, list) else []:
        if not isinstance(proof, dict):
            continue
        sid = proof.get("sentence_id")
        if not isinstance(sid, str):
            sid = refs.get(str(proof.get("ref", "")).strip())
        if isinstance(sid, str) and sid not in out:
            out.append(sid)
    return tuple(out)


class _Names:
    """L3 给的实体（窗口里的 key、名字、类型）认到这一边的实体上：账本里同名同类型的、别名只对上一个的，用账本的；
    别的按“类型加名字”新建一个。"""

    def __init__(self, entities: Mapping[str, Entity]) -> None:
        self.entities: dict[str, Entity] = dict(entities)
        self.by_name: dict[tuple[str, str], str] = {}
        self.by_alias: dict[str, set[str]] = {}
        for entity in entities.values():
            self.by_name.setdefault((entity.type, name_key(entity.name)), entity.id)
            for text in (entity.name, *entity.aliases):
                self.by_alias.setdefault(name_key(text), set()).add(entity.id)

    def resolve(self, raw: Mapping[str, object]) -> str | None:
        name = str(raw.get("name") or raw.get("mention") or "").strip()
        if not name:
            return None
        kind = str(raw.get("type") or "项目")
        kind = kind if kind in _TYPES else "项目"
        found = self.by_name.get((kind, name_key(name)))
        if found is None:
            hits = self.by_alias.get(name_key(name), set()) | self.by_alias.get(name_key(str(raw.get("mention") or "")), set())
            if len(hits) == 1:
                found = next(iter(hits))
        if found is not None:
            return found
        entity_id = f"L3:{kind}:{name_key(name)}"
        mention = str(raw.get("mention") or "").strip()
        known = self.entities.get(entity_id)
        aliases = tuple(dict.fromkeys([*(known.aliases if known else ()), *([mention] if mention and mention != name else [])]))
        self.entities[entity_id] = Entity(entity_id, name, kind, aliases)
        self.by_name[(kind, name_key(name))] = entity_id
        for text in (name, mention):
            if text:
                self.by_alias.setdefault(name_key(text), set()).add(entity_id)
        return entity_id


def _first(raw: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _entity_keys(extraction: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """窗口里的实体，按 key 找：同一个 key 写了几个的，认头一个（和 L4、L5 一样；后面那几个 map_output 标了 duplicate_key）。"""
    keys: dict[str, Mapping[str, object]] = {}
    for one in extraction.get("entities") or []:  # type: ignore[union-attr]
        if isinstance(one, dict):
            keys.setdefault(str(one.get("key")), one)
    return keys


def _problems(raw: object) -> list[str]:
    """L3 换编号时标的问题（只引了上文、编号认不出、两头落在重复的 key 上……），写法照 L4 认：字符串、列表都行。"""
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return sorted({str(one) for one in raw if one})
    return ["bad_shape"]


def read_relations(windows: Iterable[Mapping[str, object]], names: _Names) -> tuple[dict[str, Relation], dict[str, int], int]:
    """结果库里每个窗口 L3 结果的 relations（见模块说明）。返回 (关系, 没收的各有几条, 从模型原样的回答里读的窗口数)：
    存下来的结果里没有 relations 这一列的（抽取那一步没存），读模型原样的回答（results.parsed）里的。"""
    relations: dict[str, Relation] = {}
    dropped: dict[str, int] = {}
    from_parsed = 0

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for window in windows:
        extraction = window["extraction"]
        assert isinstance(extraction, dict)
        raw_relations = extraction.get("relations")
        parsed = window.get("parsed")
        if raw_relations is None and isinstance(parsed, dict) and parsed.get("relations") is not None:
            raw_relations = parsed["relations"]
            from_parsed += 1
        raw_relations = raw_relations or []
        if not isinstance(raw_relations, list):
            drop("relations 不是列表")
            continue
        keys = _entity_keys(extraction)
        refs = {str(line.get("ref")): str(line.get("sentence_id")) for line in window["manifest"]  # type: ignore[union-attr]
                if isinstance(line, dict) and line.get("sentence_id")}
        for number, raw in enumerate(raw_relations):
            if not isinstance(raw, dict):
                drop("不是对象")
                continue
            problems = _problems(raw.get("problems"))
            if problems:  # 和 L4 看事实一样：L3 换编号时标了问题的，不收、不进分母
                drop(f"L3 标了问题（{'、'.join(problems)}）")
                continue
            kind = _first(raw, "relation", "type")
            if kind not in RELATION_TYPES:
                drop("不是七种关系之一")
                continue
            ends = []
            for side_name in ("subject", "object"):
                value = raw.get(side_name)
                entity = keys.get(str(value)) if not isinstance(value, dict) else value
                ends.append(names.resolve(entity) if isinstance(entity, dict) else None)
            if None in ends:
                drop("两头的实体没写清")
                continue
            fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
            kinship = _first(raw, "kinship") or _first(fields, "kinship")  # type: ignore[arg-type]
            start = _first(raw, "valid_from", "from", "start", "occurred_at")
            end = _first(raw, "valid_to", "to", "end")
            precision = _first(raw, "time_precision", "precision") or infer_precision(start)
            state = _STATES.get(_first(raw, "state") or "", "已结束" if end else "生效")
            relation_id = f"{window['window_id']}#r{number}"
            relations[relation_id] = Relation(
                relation_id, ends[0], kind, ends[1], {"kinship": kinship} if kinship else {},  # type: ignore[arg-type]
                When(start, end, precision if precision in ("minute", "day", "month", "quarter", "year", "unknown") else "unknown"),
                state, _CERTAINTY.get(_first(raw, "certainty") or ""), _sentence_ids(raw.get("evidence"), refs),
            )
    return relations, dropped, from_parsed


def read_facts(windows: Iterable[Mapping[str, object]], names: _Names) -> dict[str, Fact]:
    """只有结果库的时候：L3 原样的事实（没过 L4、L5）。"""
    facts: dict[str, Fact] = {}
    for window in windows:
        extraction = window["extraction"]
        assert isinstance(extraction, dict)
        keys = _entity_keys(extraction)
        refs = {str(line.get("ref")): str(line.get("sentence_id")) for line in window["manifest"]  # type: ignore[union-attr]
                if isinstance(line, dict) and line.get("sentence_id")}
        for number, raw in enumerate(extraction.get("facts") or []):
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                continue
            subject = keys.get(str(raw.get("subject")))
            subject_id = names.resolve(subject) if subject else None
            involved = []
            for one in raw.get("entities") or []:
                entity = keys.get(str(one.get("key"))) if isinstance(one, dict) else None
                resolved = names.resolve(entity) if entity else None
                if resolved and resolved not in involved:
                    involved.append(resolved)
            occurred = raw.get("occurred_at") if isinstance(raw.get("occurred_at"), str) else None
            precision = raw.get("time_precision") if isinstance(raw.get("time_precision"), str) else infer_precision(occurred)
            fact_id = f"{window['window_id']}#f{number}"
            facts[fact_id] = Fact(
                fact_id, raw["text"], subject_id,
                raw.get("attribute") if isinstance(raw.get("attribute"), str) else None,
                raw.get("value") if isinstance(raw.get("value"), str) else None,
                When(occurred, None, precision if precision in ("minute", "day", "month", "quarter", "year", "unknown") else "unknown"),
                _CERTAINTY.get(str(raw.get("certainty") or "")), None, tuple(involved),
                _sentence_ids(raw.get("evidence"), refs),
            )
    return facts


def side_from_result(path: Path, packets: Collection[str], *, name: str = "javmem", library: str = "test",
                     store: Path | None = None) -> tuple[Side, Path | None]:
    """一份抽取结果（见模块说明）转成对齐格式。返回 (这一边, 账本)：账本给邮件单元分句子用，没有的是 None。
    store：另外点名的结果库（给的是账本、结果库不在它旁边的时候）。"""
    ledger, found = locate(path, library)
    if store is not None:
        store = store / "l3.db" if store.is_dir() else store
    else:
        store = found
    view = ledger_view(ledger) if ledger is not None else None
    windows, pending, unusable = store_windows(store, packets, view) if store is not None else ([], set(), [])
    # 用得上的窗口要抽的句子落在哪几个包：账本里还在排队的，有一个这样的窗口就算抽过（单元碰上还没抽完的句子另外不打分）
    covered = {line.get("packet_id") for window in windows for line in window["manifest"]  # type: ignore[union-attr]
               if str(line.get("ref", "")).startswith("T")} & set(packets)
    revisions: dict[str, int] = {}
    for window in windows:
        for line in window["manifest"]:  # type: ignore[union-attr]
            if str(line.get("ref", "")).startswith("T") and line.get("packet_id") in covered:
                revisions[line["packet_id"]] = max(revisions.get(line["packet_id"], 0), int(line.get("revision") or 0))
    notes: dict[str, object] = {}
    if unusable:
        notes["还没抽完的窗口"] = f"{len(unusable)} 个（没有结果，或者结果还没写进账本）；金标准的出处碰上它们的单元不打分"
    if ledger is not None:
        base = side_from_ledger(ledger, packets, store=store, name=name, also=covered)
        if ledger_relations(ledger):  # J4.4 起 L5 写关系的账本：关系从账本读（side_from_ledger 读好了），不读结果库的
            side = replace(base, revisions={**revisions, **base.revisions}, pending=frozenset(pending),
                           described={**base.described, "结果库": str(store) if store else "没有", **notes})
            return side, ledger
        names = _Names(base.entities)
        relations, dropped, from_parsed = read_relations(windows, names)
        side = replace(base, entities=names.entities, relations=relations, revisions={**revisions, **base.revisions},
                       pending=frozenset(pending),
                       described={**base.described, "结果库": str(store) if store else "没有",
                                  "关系": _relations_note(relations, from_parsed), **({"没收的关系": dropped} if dropped else {}),
                                  **notes})
        return side, ledger
    names = _Names({})
    relations, dropped, from_parsed = read_relations(windows, names)
    facts = read_facts(windows, names)
    cost = packet_costs(store, covered) if store is not None else {}  # type: ignore[arg-type]
    described = {"结果库": str(store), "事实": "L3 原样（没过 L4、L5：没有账本）",
                 "关系": _relations_note(relations, from_parsed), **({"没收的关系": dropped} if dropped else {}), **notes}
    return Side(name, frozenset(covered), names.entities, facts, relations, (), cost, described, revisions,
                frozenset(pending)), None


def _relations_note(relations: Mapping[str, Relation], from_parsed: int) -> str:
    note = f"{len(relations)} 条（从结果库的 L3 结果读）"
    if from_parsed:
        note += f"；{from_parsed} 个窗口存下来的结果里没有 relations，读的是模型原样的回答"
    return note


def mail_sentences(ledger: Path | None, packets: Collection[str]) -> SentenceIndex | None:
    """邮件单元分句子用的索引：这一边自己的账本有原文索引，就从它取。"""
    if ledger is None or not packets:
        return None
    return sentences_from_ledger(ledger, packets)
