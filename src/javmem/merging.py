"""合并工具改账本的那一半（J3.6，D100）：合并、记成不是同一个、撤销、拆开，当场生效。管理命令，只给主人和 CC。

**一次的样子**（先拿收件锁和写账的锁，和 review 一样）：

1. 照提议写一条合并记录（别名表旁边的 lexicon.merges.jsonl，javmem.zh.lexicon）。写以前先核一遍：和人写的别名表、
   别的记录对不上的，要重写的窗口结果库里没有结果、重新切不出来的，都不写，什么都不动。
2. 一个事务：**受影响的窗口从结果库重写**，不调模型（提到这几个实体的说法所在的窗口、这几个实体的别名出处所在的窗口、
   组里的名字当别名的出处所在的窗口）。重写时 L5 照新的别名表对，组里的名字都对到标准名的编号上：事实的主体、涉及的
   实体换成新编号，事实编号跟着变；同一个范围、属性和值一样的几条合成一条，证据合在一起；新旧重算。证据还是原来那几句。
   旧实体没有事实、没有别名了，照现在的规矩删掉。
3. **冻结标记和主人的决定跟着走**：说法的编号重写前后不变（窗口、结果库的键、第几条），所以按说法认：一条旧事实的
   说法都到了同一条新事实上，就是它。冻结的，新的那条冻结；两条合成一条、其中一条冻结的，合成的那条冻结。待确认里
   提到旧事实的照新编号改，成了同一条的冲突关掉（决定记 merged）。认不出来的不猜，报出来。没有说法的冻结事实
   （以前替换、撤回以后留下的），主体、涉及的是并掉的实体的，改指到新实体上。这几个实体没处理的“实体有歧义”关掉（merged）。
4. **跳转：** 旧编号 → 新编号记进 entity_redirects，查询接口拿旧编号来查照合并以后的实体答（query-api 第 5 节）。
   这本账上生效了哪几条合并记录记在 entity_merges。
5. 向量：事实文字没变的跟着搬（账本里原来的、向量缓存里的），都没有的先空着，下次 javmem write 补。

**不是同一个**（--apart）只管以后：合并工具不再提这一对，L5 按别名找到的候选里去掉它们，账本不重写（它们本来就是分开的）。
**拆开**（--detach 实体 --alias 叫法）：记成“这个叫法不是这个实体”，再把这个实体的说法、这个叫法的出处所在的窗口重写。
**撤销**（--undo 记录）：追加一条撤销记录，受影响的窗口照没有那一条的别名表重写；编号是按名字算的，旧实体重写出来还是
原来的编号；跳转表里那几条删掉。

**整本重建时：** L5 从一开始就读人写的别名表加合并记录，合并不用重放，重建出来就是合好的；写完账以后跑一次
`javmem entities --sync`，按合并记录补上这本新账的 entity_merges、entity_redirects（受影响的窗口照样重写一遍，结果不变）。
合并记录写了、账本没改成的（中途断了），--sync 也接着做。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from javmem.config import ConfigError, Settings
from javmem.entities import MERGE, Proposal, entity_info
from javmem.intake import intake_lock
from javmem.ledger import open_ledger, transaction
from javmem.pipeline.batch import load_queued
from javmem.pipeline.extract import PROMPTS
from javmem.pipeline.merge import entity_id_of, group_key, load_owner_names, merge_key, name_key, relation_identity
from javmem.pipeline.recheck import RECHECK_PROMPT_VERSION, apply_rechecks
from javmem.pipeline import correct, precheck
from javmem.pipeline.reljudge import BLOCK, JudgeSpec, apply_judgments
from javmem.pipeline.store import STORE_FILE, ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.verify import L4_VERSION, Checked, verify_windows
from javmem.pipeline.window import build_windows
from javmem.pipeline.write import LedgerWriter, WriteLock, _json, _scope_of_row, _window_order, register_lexicon
from javmem.redaction import load_patterns
from javmem.timeutil import iso
from javmem.zh import lexicon as lex

MERGED = "merged"  # 待确认的决定：合并时关掉的（成了同一条的冲突、并掉的实体的歧义）


class MergeError(Exception):
    """点名的提议、实体、记录不对，或者现在合不了（要重写的窗口结果库里没有、重新切不出来）。"""


@dataclass
class MergeReport:
    """只有编号和数，不抄原文。"""

    library: str
    records: list[str] = field(default_factory=list)  # 这次在这本账上生效的合并记录
    windows: int = 0  # 重写了几个窗口
    facts_before: int = 0  # 这几个实体当主体、涉及的事实，合以前
    facts_after: int = 0  # 新实体当主体、涉及的事实，合以后
    frozen_moved: list[str] = field(default_factory=list)  # 冻结标记跟着走到的新事实
    unmatched: list[str] = field(default_factory=list)  # 认不出来、冻结标记没跟着走的旧事实
    repointed: list[str] = field(default_factory=list)  # 没有说法的冻结事实，改指到新实体上的
    reviews_moved: int = 0
    reviews_closed: int = 0
    redirects: dict[str, str] = field(default_factory=dict)  # 旧编号 → 新编号（这次加的）
    redirects_dropped: list[str] = field(default_factory=list)  # 撤销时删掉的跳转
    others_changed: list[str] = field(default_factory=list)  # 别的实体跟着变了的（按理说不会，变了要看）
    vectors_missing: int = 0  # 新事实里还没有向量的，下次 javmem write 补
    dry_run: bool = False
    merging: bool = False  # 这次有合并（“挂到新编号上”那一行这样说）；撤销、拆开的照“以前、现在”说

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def lines(self) -> list[str]:
        """卡片上“合并以后”那几行，命令行也打这几行。"""
        joined = max(self.facts_before - self.facts_after, 0)
        lines = [f"重写 {self.windows} 个窗口（从结果库，不调模型）"]
        if self.merging:
            lines.append(f"事实 {self.facts_after} 条挂到新编号上" + (f"，其中 {joined} 条和另一条合成了一条（同一个属性和值）" if joined else ""))
        else:
            lines.append(f"这几个实体当主体、涉及的事实：以前 {self.facts_before} 条，现在 {self.facts_after} 条")
        lines.append(f"冻结的跟着走 {len(self.frozen_moved)} 条，没有说法的冻结事实改指 {len(self.repointed)} 条；待确认改了 {self.reviews_moved} 条、关掉 {self.reviews_closed} 条")
        if self.unmatched:
            lines.append(f"认不出来、冻结标记没跟着走的：{'、'.join(self.unmatched)}")
        if self.redirects:
            lines.append(f"旧编号 {'、'.join(sorted(self.redirects))} 以后查到，跳到 {'、'.join(sorted(set(self.redirects.values())))}")
        if self.redirects_dropped:
            lines.append(f"跳转删掉了：{'、'.join(self.redirects_dropped)}")
        if self.others_changed:
            lines.append(f"别的实体也跟着变了（要看）：{'、'.join(self.others_changed)}")
        if self.vectors_missing:
            lines.append(f"新事实里 {self.vectors_missing} 条还没有向量，下次 javmem write 补")
        return lines


class _DryRun(Exception):
    def __init__(self, report: MergeReport) -> None:
        super().__init__("dry run")
        self.report = report


# ---------- 合并记录 ----------


def _now(settings: Settings) -> str:
    return iso(settings.now())


def _lexicon_path(settings: Settings) -> Path:
    if settings.lexicon_file is None:
        raise MergeError("没配别名表（JAVMEM_LEXICON_FILE）：合并记录写在别名表旁边")
    return settings.lexicon_file


def merge_record(
    conn: sqlite3.Connection,
    proposal: Proposal,
    records: Sequence[Mapping[str, object]],
    *,
    note: str,
    at: str,
    name: str | None = None,
    entity_type: str | None = None,
    only: Sequence[str] = (),
) -> dict[str, object]:
    """照一条提议写的合并记录：标准名、类型、并进来的实体的名字、分区、别的类型，旧编号和新编号。"""
    if proposal.kind != MERGE:
        raise MergeError("这条提议是“记成不是同一个”，用 --apart")
    chosen = list(dict.fromkeys(only)) if only else list(proposal.default)
    unknown = [entity_id for entity_id in chosen if entity_id not in proposal.entities]
    if unknown:
        raise MergeError(f"--only 点的不在这条提议里：{'、'.join(unknown)}")
    if not chosen:
        raise MergeError("这条提议默认一个都不并：用 --only 点名要并的")
    standard = (name or proposal.name).strip()
    target_type = entity_type or proposal.type
    if not standard or target_type not in lex.ENTITY_TYPES:
        raise MergeError(f"标准名不能是空的，类型要是 {'、'.join(lex.ENTITY_TYPES)} 之一")
    new_id = entity_id_of(target_type, name_key(standard))
    infos = {entity_id: entity_info(conn, entity_id) for entity_id in chosen}
    if any(info is None for info in infos.values()):
        raise MergeError("点名的实体有的已经不在账本里了，先 --proposals 看一遍")
    old_ids = [entity_id for entity_id in chosen if entity_id != new_id]
    if not old_ids:
        raise MergeError("点名的就是合并以后的那个实体，没什么可并的")
    names = list(dict.fromkeys(info.name for info in infos.values() if info is not None and name_key(info.name) != name_key(standard)))
    partitions = sorted({partition for info in infos.values() if info is not None for partition in info.partitions})
    types = {info.type for info in infos.values() if info is not None}
    if lex.BOT in types:
        raise MergeError("Bot 不用合并工具合：写进别名表的 Bot 名单")
    return {
        "id": lex.next_record_id(records),
        "kind": "merge",
        "at": at,
        "by": "owner",
        "note": note,
        "type": target_type,
        "standard": standard,
        "names": names,
        "where": partitions or None,
        "from_types": sorted(types - {target_type}, key=lex.ENTITY_TYPES.index),
        "old_ids": old_ids,
        "new_id": new_id,
        "proposal": proposal.proposal_id,
    }


def apart_record(
    conn: sqlite3.Connection, proposal: Proposal, records: Sequence[Mapping[str, object]], *, note: str, at: str
) -> dict[str, object]:
    """照一条提议记“不是同一个”：同一类型的那几个实体的名字（类型不一样的本来就不会按别名连起来）。"""
    infos = [entity_info(conn, entity_id) for entity_id in proposal.entities]
    entity_type = proposal.type
    names = list(dict.fromkeys(info.name for info in infos if info is not None and info.type == entity_type))
    if len({name_key(name) for name in names}) < 2:
        raise MergeError("这条提议里同一类型的不到两个：类型不一样的本来就不会按别名连起来")
    return {"id": lex.next_record_id(records), "kind": "apart", "at": at, "by": "owner", "note": note, "type": entity_type,
            "names": names, "proposal": proposal.proposal_id}





def undo_record(records: Sequence[Mapping[str, object]], merge_id: str, *, note: str, at: str) -> dict[str, object]:
    active = {str(record["id"]) for record in lex.active_records(records)}
    if merge_id not in active:
        raise MergeError(f"没有这条还算数的合并记录：{merge_id}（撤销过的、誊进人写的那份的不能再撤）")
    return {"id": lex.next_record_id(records), "kind": "undo", "at": at, "by": "owner", "note": note, "undoes": merge_id}


def detach_record(
    conn: sqlite3.Connection, entity_id: str, alias: str, records: Sequence[Mapping[str, object]], *, note: str, at: str
) -> dict[str, object]:
    """拆开：这个叫法不是这个实体（L5 按别名并错了的）。"""
    info = entity_info(conn, entity_id)
    if info is None:
        raise MergeError(f"账本里没有这个实体：{entity_id}")
    if lex.BOT == info.type:
        raise MergeError("Bot 不用合并工具拆：改别名表的 Bot 名单")
    if alias not in info.aliases:
        raise MergeError("这个实体没有这个叫法（要一字不差，--show 卡片上有它的叫法）")
    if name_key(alias) == name_key(info.name):
        raise MergeError("这是它的标准名，不能拆：要合到别的实体上用 --merge")
    return {"id": lex.next_record_id(records), "kind": "apart", "at": at, "by": "owner", "note": note, "type": info.type,
            "names": [info.name, alias], "entity": entity_id, "alias": alias}


# ---------- 这本账上哪几条还没生效 ----------


def pending(conn: sqlite3.Connection, records: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """合并记录里还没在这本账上生效的：merge、apart 没记进 entity_merges 的（撤销了的不算）；撤销记录的那一条
    在这本账上还算数的。"""
    applied = {row["merge_id"]: row for row in conn.execute("SELECT merge_id, undone_at FROM entity_merges")}
    undone = {str(record["undoes"]) for record in records if record["kind"] == "undo"}
    found = []
    for record in records:
        if record["kind"] in ("merge", "apart") and record["id"] not in applied and record["id"] not in undone:
            found.append(record)
        elif record["kind"] == "undo":
            row = applied.get(str(record["undoes"]))
            if row is not None and row["undone_at"] is None:
                found.append(record)
    return found


# ---------- 重写哪几个窗口 ----------


@dataclass
class _Plan:
    records: list[Mapping[str, object]]
    entities: set[str] = field(default_factory=set)  # 这几条记录碰到的实体
    keys: set[str] = field(default_factory=set)  # 组里的名字、拆开的叫法（比对写法）
    windows: set[str] = field(default_factory=set)
    merged: dict[str, str] = field(default_factory=dict)  # 并掉的旧编号 → 新编号（合并的）
    undone: list[Mapping[str, object]] = field(default_factory=list)  # 撤销的是哪几条
    collapsed: set[str] = field(default_factory=set)  # 像的几个都并成了一个的“实体有歧义”：关掉，那个实体的窗口一起重写


def _chunks(items: Sequence[str], size: int = 400) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _marks(items: Sequence[object]) -> str:
    return ", ".join("?" for _ in items)


def _named(conn: sqlite3.Connection, types: Collection[str], keys: Collection[str]) -> set[str]:
    found: set[str] = set()
    for chunk in _chunks(sorted(keys)):
        found |= {
            row[0]
            for row in conn.execute(
                f"SELECT entity_id FROM entities WHERE type IN ({_marks(list(types))}) AND name_key IN ({_marks(chunk)})",
                (*types, *chunk),
            )
        }
    return found


def _plan(conn: sqlite3.Connection, todo: Sequence[Mapping[str, object]], records: Sequence[Mapping[str, object]]) -> _Plan:
    by_id = {str(record["id"]): record for record in records}
    plan = _Plan(list(todo))
    for record in todo:
        target = by_id[str(record["undoes"])] if record["kind"] == "undo" else record
        if record["kind"] == "undo":
            plan.undone.append(target)
        if target["kind"] == "merge":
            keys = {name_key(str(name)) for name in [target["standard"], *target["names"]]}  # type: ignore[misc]
            types = [str(target["type"]), *target.get("from_types", [])]  # type: ignore[misc]
            plan.entities |= {*target.get("old_ids", []), str(target["new_id"])} | _named(conn, types, keys)  # type: ignore[misc]
            plan.keys |= keys
            if record["kind"] == "merge":
                plan.merged.update({str(old): str(target["new_id"]) for old in target.get("old_ids", [])})  # type: ignore[union-attr]
        elif target.get("entity"):  # 拆开：这个实体，和那个叫法
            plan.entities.add(str(target["entity"]))
            plan.entities.add(entity_id_of(str(target["type"]), name_key(str(target["alias"]))))  # 拆出来的那个叫法自己的实体
            plan.keys.add(name_key(str(target["alias"])))
    if plan.merged:  # 一条没处理的“实体有歧义”，像的几个这次都并成了一个：歧义没了。整本重建时这个名字直接对上合好的那个
        for row in conn.execute("SELECT claims FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL"):
            claims = json.loads(row["claims"])
            entity, candidates = claims.get("entity_id"), [str(item) for item in claims.get("candidates") or []]
            if not entity or entity in plan.merged or not any(item in plan.merged for item in candidates):
                continue
            if len({plan.merged.get(item, item) for item in candidates}) <= 1:
                plan.collapsed.add(str(entity))
                plan.entities.add(str(entity))
    plan.entities = {entity_id for entity_id in plan.entities if entity_id}
    ids = sorted(plan.entities)
    for chunk in _chunks(ids):
        plan.windows |= {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT window_id FROM fact_claims WHERE subject_entity_id IN ({0}) OR EXISTS (SELECT 1 FROM"
                " json_each(fact_claims.entities) WHERE json_extract(value, '$.entity_id') IN ({0}))".format(_marks(chunk)),
                (*chunk, *chunk),
            )
        }
        plan.windows |= {  # 关系的两头、代表哪一家是这几个实体的（J4.4）
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT window_id FROM relation_claims WHERE subject_entity_id IN ({0}) OR object_entity_id IN ({0})"
                " OR org_entity_id IN ({0})".format(_marks(chunk)),
                (*chunk, *chunk, *chunk),
            )
        }
        plan.windows |= {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT v.window_id FROM evidence AS v JOIN aliases AS a USING (alias_id)"
                f" WHERE a.entity_id IN ({_marks(chunk)}) AND v.window_id IS NOT NULL",
                chunk,
            )
        }
    for chunk in _chunks(sorted(plan.keys)):
        plan.windows |= {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT v.window_id FROM evidence AS v JOIN aliases AS a USING (alias_id)"
                f" WHERE a.alias_key IN ({_marks(chunk)}) AND v.window_id IS NOT NULL",
                chunk,
            )
        }
    plan.windows.discard(None)  # type: ignore[arg-type]
    return plan


@dataclass(frozen=True)
class _Written:
    cache_name: str  # 抽取的模型（名字@档位/输出方式）：算结果库的键
    recheck: str | None  # 复核的模型，没复核是 None
    extracted_by: str
    checked_by: str
    prompt: str = ""  # 抽的时候用的 L3 提示词版本：也算结果库的键
    judge: JudgeSpec | None = None  # 判断 C 的设置（javmem.pipeline.reljudge），没配的是 None
    facts: JudgeSpec | None = None  # 进图前检查判事实的设置（javmem.pipeline.precheck，J5.3），没开的是 None


def _written_by(conn: sqlite3.Connection, windows: Collection[str]) -> _Written:
    """这几个窗口当初是怎么写的（抽取的模型、核法）：重写要一模一样，不然结果库的键对不上、核的结果也会变。"""
    rows: list[sqlite3.Row] = []
    for chunk in _chunks(sorted(windows)):
        rows += conn.execute(
            f"SELECT DISTINCT extracted_by, l4_version FROM fact_claims WHERE window_id IN ({_marks(chunk)})", chunk
        ).fetchall()
    found = {(row["extracted_by"], row["l4_version"]) for row in rows}
    if not found:  # 只有别名出处的窗口：照这本账里写得最多的那一种
        row = conn.execute(
            "SELECT extracted_by, l4_version FROM fact_claims GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise MergeError("这本账里还没有说法，没什么可重写的")
        found = {(row["extracted_by"], row["l4_version"])}
    if len(found) != 1:
        raise MergeError("要重写的窗口是用不同的抽取、核法写的：先整本重建再合并")
    extracted_by, checked = found.pop()
    prompt, _, cache_name = str(extracted_by).partition(" ")
    if prompt not in PROMPTS or not cache_name:  # 照当初的提示词版本算键（J4.2 起版本用配置选）
        raise MergeError(f"要重写的窗口是认不得的提示词版本抽的（认得的：{'、'.join(PROMPTS)}）：先整本重建再合并")
    try:  # 配了判断 C 的，核的方式后面还跟着 C 的设置（reljudge.checked_by），开了进图前检查的再跟着 f1 的：照原样重做
        rules, judge, facts = precheck.split_checked_by(str(checked))
    except ValueError:
        raise MergeError("要重写的窗口是别的版本的判断 C（或者进图前检查）判的：先整本重建再合并") from None
    prefix = f"{L4_VERSION} {RECHECK_PROMPT_VERSION} "
    if rules == L4_VERSION:
        recheck = None
    elif rules.startswith(prefix):
        recheck = rules[len(prefix) :]
    else:
        raise MergeError(f"要重写的窗口是别的版本的 L4 核的（现在是 {L4_VERSION}）：先整本重建再合并")
    return _Written(cache_name, recheck, str(extracted_by), str(checked), prompt, judge, facts)


def _packets_of(conn: sqlite3.Connection, store: ResultStore, windows: Collection[str]) -> set[str]:
    """这几个窗口是哪几个包切出来的：结果库里记着；结果库里没有的，按说法的证据、别名的出处补。"""
    packets: set[str] = set()
    for row in store.windows():
        if row["window_id"] in windows:
            packets |= {str(packet_id) for packet_id, _ in json.loads(row["packets"])}
    for chunk in _chunks(sorted(windows)):
        for row in conn.execute(f"SELECT proofs FROM fact_claims WHERE window_id IN ({_marks(chunk)})", chunk):
            packets |= {str(proof["packet_id"]) for proof in json.loads(row["proofs"])}
        packets |= {row[0] for row in conn.execute(f"SELECT DISTINCT packet_id FROM evidence WHERE window_id IN ({_marks(chunk)})", chunk)}
    return packets


def _items(
    settings: Settings, library: str, store: ResultStore, conn: sqlite3.Connection, plan: _Plan, lexicon: lex.Lexicon
) -> tuple[list[Checked], _Written]:
    """要重写的窗口：照当初的包重新切、按当初的抽取和核法从结果库取结果跑 L4。缺一个都不重写。关系的核对用这一次的
    别名表（加上这条合并记录）和主人的名字，和接着写账用的一样（J4.3）。"""
    if not plan.windows:
        return [], _Written("", None, "", "")
    written = _written_by(conn, plan.windows)
    batch = load_queued(settings, library, packet_ids=_packets_of(conn, store, plan.windows), any_state=True)
    built = [window for window in build_windows(batch.packets, context_pool=batch.context) if window.window_id in plan.windows]
    missing = sorted(plan.windows - {window.window_id for window in built})
    if missing:
        raise MergeError(f"这几个窗口重新切不出来了（包换了版本、撤回了，或者切窗的规矩改了）：{'、'.join(missing)}；先整本重建")
    patterns = load_patterns(settings.redaction_file)
    model = SimpleNamespace(cache_name=written.cache_name, name=written.cache_name.split("@")[0])
    checked = verify_windows(  # type: ignore[arg-type]
        built, store, model, patterns, prompt=written.prompt, lexicon=lexicon,
        owner_names=load_owner_names(settings.owner_file),
    )
    if written.recheck is not None:
        checked = apply_rechecks(checked, store, written.recheck, patterns)
    if written.judge is not None:  # 判断 C：照当初的设置用存下的判定
        # 照别名表加上这条合并记录重核，以前 R2 没过、没判过的关系可能这时才过：这条记录写成了 extract 才看得到，
        # 写完的包也不再抽，等不来。不让整个合并停下，这几条记 C:not_judged 挡下（D125：没把握的不进）
        checked = apply_judgments(checked, store, written.judge, patterns, unjudged=BLOCK)
    if written.facts is not None:  # 进图前检查（J5.3）：照当初的设置用存下的判定，没判过的事实一样记 C:not_judged 挡下
        checked = precheck.apply(checked, store, written.facts, patterns, strict=correct.strict_partitions(), unjudged=BLOCK)
    bad = sorted(item.window.window_id for item in checked if item.check is None)
    if bad:
        raise MergeError(f"这几个窗口结果库里没有能用的结果（没抽过、打码没打干净、复核没判）：{'、'.join(bad)}；合并要从结果库重写")
    return checked, written


# ---------- 改账本 ----------


def _facts_of(conn: sqlite3.Connection, entity_ids: Collection[str]) -> set[str]:
    found: set[str] = set()
    for chunk in _chunks(sorted(entity_ids)):
        found |= {
            row[0]
            for row in conn.execute(
                "SELECT fact_id FROM facts WHERE withdrawn_at IS NULL AND (subject_entity_id IN ({0})"
                " OR fact_id IN (SELECT fact_id FROM fact_entities WHERE entity_id IN ({0})))".format(_marks(chunk)),
                (*chunk, *chunk),
            )
        }
    return found


def _claims_in(conn: sqlite3.Connection, windows: Collection[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for chunk in _chunks(sorted(windows)):
        found.update(
            {row[0]: row[1] for row in conn.execute(f"SELECT claim_id, fact_id FROM fact_claims WHERE window_id IN ({_marks(chunk)})", chunk)}
        )
    return found


def _relation_claims_in(conn: sqlite3.Connection, windows: Collection[str]) -> dict[str, str]:
    """这几个窗口里关系的说法 → 它在哪一条关系上（J4.4）。"""
    found: dict[str, str] = {}
    for chunk in _chunks(sorted(windows)):
        found.update({row[0]: row[1] for row in conn.execute(
            f"SELECT claim_id, relation_id FROM relation_claims WHERE window_id IN ({_marks(chunk)})", chunk)})
    return found


def _move_frozen_relations(
    conn: sqlite3.Connection, writer: LedgerWriter, before: Mapping[str, str], frozen: Collection[str], report: MergeReport
) -> None:
    """冻结的关系跟着说法走（J4.4，照事实）：重写以后它的说法都落在另一条关系上的（两头并成了新的实体），冻结标记、
    待确认改指过去，旧的那一行没有说法了就删掉。说法落在几条上、一条都没落下的，报出来，不猜。"""
    after = _relation_claims_in(conn, set(_window_of(claim) for claim in before))
    for old in sorted(frozen):
        news = {after[claim] for claim, relation in before.items() if relation == old and claim in after}
        if len(news) != 1:
            report.unmatched.append(old)
            continue
        new = next(iter(news))
        if new == old:
            continue
        conn.execute("UPDATE relations SET frozen = 1 WHERE relation_id = ?", (new,))
        report.frozen_moved.append(new)
        writer._rename_relation(old, new)
        if conn.execute("SELECT 1 FROM relation_claims WHERE relation_id = ?", (old,)).fetchone() is None:
            writer._forget_relation(old)


def _repoint_claimless_relations(
    conn: sqlite3.Connection, writer: LedgerWriter, merged: Mapping[str, str], now: str
) -> None:
    """没有说法的冻结关系（以前替换、撤回以后留下的），一头是并掉的旧实体的：改指到新实体上（J4.4，照事实）。"""
    olds = sorted(merged)
    if not olds:
        return
    rows = conn.execute(
        "SELECT * FROM relations AS r WHERE r.frozen = 1"
        " AND NOT EXISTS (SELECT 1 FROM relation_claims AS c WHERE c.relation_id = r.relation_id)"
        " AND (r.subject_entity_id IN ({0}) OR r.object_entity_id IN ({0}) OR r.org_entity_id IN ({0}))"
        " ORDER BY r.relation_id".format(_marks(olds)),
        (*olds, *olds, *olds),
    ).fetchall()
    identities = set()
    for row in rows:
        subject = merged.get(row["subject_entity_id"], row["subject_entity_id"])
        target = merged.get(row["object_entity_id"], row["object_entity_id"])
        org = merged.get(row["org_entity_id"], row["org_entity_id"]) if row["org_entity_id"] else None
        if subject == target:
            continue  # 两头并成了同一个：留着原样，等主人看
        scope = _scope_of_row(row["scope_partitions"], row["scope_sensitive"])
        identity = relation_identity(scope, subject, row["relation_type"], target, kinship=row["kinship"], org_id=org)
        conn.execute(
            "UPDATE relations SET subject_entity_id = ?, object_entity_id = ?, org_entity_id = ?, identity_key = ?, updated_at = ?"
            " WHERE relation_id = ?",
            (subject, target, org, identity, now, row["relation_id"]),
        )
        identities.add(identity)
    writer._refresh_relations(identities, set(), now)


def _window_of(claim_id: str) -> str:
    """说法编号是“窗口编号#键的前 12 位#第几条”：取窗口编号。"""
    return claim_id.rsplit("#", 2)[0]


def _vectors(conn: sqlite3.Connection, vectors_dir: Path | None, windows: Collection[str]) -> tuple[dict[str, bytes], str]:
    """重写要用的向量：账本里原来的（事实文字没变的跟着搬），加上向量缓存里有的。都按这本账里用得最多的那个模型。"""
    row = conn.execute("SELECT model FROM fact_vectors GROUP BY model ORDER BY count(*) DESC LIMIT 1").fetchone()
    model = str(row[0]) if row is not None else ""
    texts: set[str] = set()
    for chunk in _chunks(sorted(windows)):
        texts |= {r[0] for r in conn.execute(f"SELECT content FROM fact_claims WHERE window_id IN ({_marks(chunk)})", chunk)}
    vectors: dict[str, bytes] = {}
    if model:
        for found in conn.execute(
            "SELECT f.content, v.vector FROM fact_vectors AS v JOIN facts AS f USING (fact_id) WHERE v.model = ?", (model,)
        ):
            if found[0] in texts:
                vectors[found[0]] = found[1]
        if vectors_dir is not None and (vectors_dir / "vectors.db").is_file():
            with VectorCache(vectors_dir) as cache:
                vectors.update(cache.get(model, sorted(texts - set(vectors))))
    return vectors, model


def _remap_reviews(conn: sqlite3.Connection, moved: Mapping[str, str], now: str, report: MergeReport) -> None:
    """待确认里提到旧事实的照新编号改（定过的、没处理的都改）；和冻结的冲突成了同一条的关掉，记 merged。"""
    if not moved:
        return
    for row in conn.execute("SELECT review_id, kind, claims, decided_at FROM review_queue WHERE kind IN ('frozen_conflict', 'other')").fetchall():
        claims = json.loads(row["claims"])
        if not isinstance(claims, dict) or not any(claims.get(name) in moved for name in ("frozen", "new", "fact")):
            continue
        for name in ("frozen", "new", "fact"):
            if claims.get(name) in moved:
                claims[name] = moved[claims[name]]
        if row["kind"] == "frozen_conflict" and claims.get("frozen") == claims.get("new"):
            if row["decided_at"] is None:
                conn.execute(
                    "UPDATE review_queue SET claims = ?, decision = ?, decided_at = ?, decided_by = 'owner' WHERE review_id = ?",
                    (_json(claims), MERGED, now, row["review_id"]),
                )
                report.reviews_closed += 1
                continue
        conn.execute("UPDATE review_queue SET claims = ? WHERE review_id = ?", (_json(claims), row["review_id"]))
        report.reviews_moved += 1


def _repoint_claimless(conn: sqlite3.Connection, writer: LedgerWriter, merged: Mapping[str, str], now: str, report: MergeReport) -> set[str]:
    """没有说法的冻结事实（以前替换、撤回以后留下的），主体、涉及的是并掉的旧实体的：改指到新实体上。改完和一条有说法的
    成了同一条的，冻结标记给那一条，这一条的待确认改指过去，这一条删掉。返回要重算新旧的组。"""
    groups: set[str] = set()
    olds = sorted(merged)
    if not olds:
        return groups
    rows = conn.execute(
        "SELECT * FROM facts AS f WHERE f.frozen = 1 AND NOT EXISTS (SELECT 1 FROM fact_claims AS c WHERE c.fact_id = f.fact_id)"
        " AND (f.subject_entity_id IN ({0}) OR EXISTS (SELECT 1 FROM fact_entities AS x WHERE x.fact_id = f.fact_id AND x.entity_id IN ({0})))"
        " ORDER BY f.fact_id".format(_marks(olds)),
        (*olds, *olds),
    ).fetchall()
    for row in rows:
        subject = merged.get(row["subject_entity_id"], row["subject_entity_id"])
        scope = _scope_of_row(row["scope_partitions"], row["scope_sensitive"])
        said = date.fromisoformat(str(row["as_of"])[:10]) if row["as_of"] else None
        key = merge_key(scope, subject, row["attribute"], row["value"], row["content"], said=said)
        other = conn.execute("SELECT fact_id, group_key FROM facts WHERE merge_key = ? AND fact_id != ?", (key, row["fact_id"])).fetchone()
        if other is not None:
            conn.execute("UPDATE facts SET frozen = 1 WHERE fact_id = ?", (other["fact_id"],))
            _remap_reviews(conn, {row["fact_id"]: other["fact_id"]}, now, report)
            writer._forget_fact(row["fact_id"])
            report.frozen_moved.append(other["fact_id"])
            if other["group_key"] is not None:
                groups.add(other["group_key"])
        else:
            group = group_key(scope, subject, row["attribute"])
            conn.execute(
                "UPDATE facts SET subject_entity_id = ?, merge_key = ?, group_key = ?, updated_at = ? WHERE fact_id = ?",
                (subject, key, group, now, row["fact_id"]),
            )
            for role in conn.execute("SELECT entity_id, role FROM fact_entities WHERE fact_id = ?", (row["fact_id"],)).fetchall():
                if role["entity_id"] in merged:
                    conn.execute(
                        "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id, role) VALUES (?, ?, ?)",
                        (row["fact_id"], merged[role["entity_id"]], role["role"]),
                    )
                    conn.execute(
                        "DELETE FROM fact_entities WHERE fact_id = ? AND entity_id = ? AND role = ?", (row["fact_id"], role["entity_id"], role["role"])
                    )
            for old_group in (row["group_key"], group):
                if old_group is not None:
                    groups.add(old_group)
        report.repointed.append(row["fact_id"])
    return groups


def _restore_conflicts(
    conn: sqlite3.Connection, before: Sequence[sqlite3.Row], mapping: Mapping[str, str], now: str, report: MergeReport
) -> None:
    """重写时，没处理的冲突跟着新说法那一边的事实删了、又按新编号重算出来：建的时间照原来的（30 天的钟不重新算）；
    两边成了同一条的，补一条关掉的（merged），留着历史。"""
    for row in before:  # 按冲突的两边认，不按编号：编号是自增的行号，删掉的那条的号会被重新进队列的用上
        claims = json.loads(row["claims"])
        frozen, new = mapping.get(claims.get("frozen"), claims.get("frozen")), mapping.get(claims.get("new"), claims.get("new"))
        if frozen == new:
            if conn.execute(
                "SELECT 1 FROM review_queue WHERE kind = 'frozen_conflict' AND decision = ? AND created_at = ?"
                " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ?",
                (MERGED, row["created_at"], frozen, new),
            ).fetchone() is not None:
                continue  # _remap_reviews 已经关掉了
            conn.execute(
                "INSERT INTO review_queue (kind, claims, quotes, decision, decided_at, decided_by, created_at)"
                " VALUES ('frozen_conflict', ?, '[]', ?, ?, 'owner', ?)",
                (_json({**claims, "frozen": frozen, "new": new}), MERGED, now, row["created_at"]),
            )
            report.reviews_closed += 1
            continue
        conn.execute(
            "UPDATE review_queue SET created_at = ? WHERE kind = 'frozen_conflict' AND decided_at IS NULL"
            " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ?",
            (row["created_at"], frozen, new),
        )


def _close_ambiguities(
    conn: sqlite3.Connection,
    entity_ids: Collection[str],
    targets: Collection[str],
    lexicon: lex.Lexicon,
    now: str,
    report: MergeReport,
) -> None:
    """没处理的“实体有歧义”，主人合并了就是定了，记 merged 关掉：实体是并掉的（entity_ids），或者是合并以后的那个
    （targets）、它的名字现在在别名表的组里、组在这条歧义的分区里算数（L5 直接对到组上，不再按别名找，不会再有歧义）。"""
    for row in conn.execute(
        "SELECT review_id, claims FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL ORDER BY review_id"
    ).fetchall():
        claims = json.loads(row["claims"])
        entity = claims.get("entity_id")
        settled = entity in entity_ids
        if not settled and entity in targets:
            found = conn.execute("SELECT canonical_name, type FROM entities WHERE entity_id = ?", (entity,)).fetchone()
            scope = json.loads(claims["scope"]) if isinstance(claims.get("scope"), str) else {}
            settled = found is not None and lexicon.group_of(found[0], found[1], scope.get("partitions") or []) is not None
        if settled:
            conn.execute(
                "UPDATE review_queue SET decision = ?, decided_at = ?, decided_by = 'owner' WHERE review_id = ?", (MERGED, now, row["review_id"])
            )
            report.reviews_closed += 1


def _bookkeeping(conn: sqlite3.Connection, plan: _Plan, now: str, report: MergeReport) -> None:
    for record in plan.records:
        if record["kind"] == "merge":
            new_id, olds = str(record["new_id"]), [str(old) for old in record.get("old_ids", [])]  # type: ignore[union-attr]
            conn.execute(
                "INSERT OR REPLACE INTO entity_merges (merge_id, kind, new_id, old_ids, applied_at, undone_at) VALUES (?, 'merge', ?, ?, ?, NULL)",
                (record["id"], new_id, json.dumps(olds), now),
            )
            for old in olds:
                if old == new_id:
                    continue
                conn.execute("UPDATE entity_redirects SET new_id = ? WHERE new_id = ?", (new_id, old))  # 以前跳到它的接着跳
                conn.execute(
                    "INSERT OR REPLACE INTO entity_redirects (old_id, new_id, merge_id) VALUES (?, ?, ?)", (old, new_id, record["id"])
                )
                report.redirects[old] = new_id
            conn.execute("DELETE FROM entity_redirects WHERE old_id = new_id")
        elif record["kind"] == "apart":
            entity = [str(record["entity"])] if record.get("entity") else []
            conn.execute(
                "INSERT OR REPLACE INTO entity_merges (merge_id, kind, new_id, old_ids, applied_at, undone_at) VALUES (?, 'apart', NULL, ?, ?, NULL)",
                (record["id"], json.dumps(entity), now),
            )
        else:
            target = str(record["undoes"])
            conn.execute("UPDATE entity_merges SET undone_at = ? WHERE merge_id = ?", (now, target))
            report.redirects_dropped += [row[0] for row in conn.execute("SELECT old_id FROM entity_redirects WHERE merge_id = ? ORDER BY old_id", (target,))]
            conn.execute("DELETE FROM entity_redirects WHERE merge_id = ?", (target,))
        report.records.append(str(record["id"]))


def _apply(
    conn: sqlite3.Connection,
    settings: Settings,
    lexicon: lex.Lexicon,
    plan: _Plan,
    items: Sequence[Checked],
    written: _Written,
    vectors_dir: Path | None,
    report: MergeReport,
) -> None:
    """一个事务里做完（调用方开事务）。"""
    now = _now(settings)
    touched_entities = set(plan.entities) | set(plan.merged.values())
    report.facts_before = len(_facts_of(conn, touched_entities))
    entities_before = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT entity_id, canonical_name, type FROM entities")}
    claims_before = _claims_in(conn, plan.windows)
    relation_claims_before = _relation_claims_in(conn, plan.windows)  # J4.4：冻结的关系跟着说法走
    frozen_relations_before = {
        relation_id for relation_id in set(relation_claims_before.values())
        if conn.execute("SELECT frozen FROM relations WHERE relation_id = ?", (relation_id,)).fetchone()[0]
    }
    frozen_before = {
        fact_id for fact_id in set(claims_before.values())
        if conn.execute("SELECT frozen FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()[0]
    }
    _close_ambiguities(conn, set(plan.merged) | plan.collapsed, set(plan.merged.values()), lexicon, now, report)
    vectors, model = _vectors(conn, vectors_dir, plan.windows)
    writer = LedgerWriter(
        conn,
        extracted_by=written.extracted_by,
        embedding_model=model,
        register=False,
        owner_names=load_owner_names(settings.owner_file),
        checked_by=written.checked_by,
        lexicon=lexicon,
        precheck_on=written.facts is not None,
    )
    # 先把这几个窗口以前写的一起清掉，再按时间一个一个写：不然还没轮到的窗口里旧的别名出处还在，先写的窗口会按它们
    # 对回去（撤销时“笔误的写法”又对到合并以后的实体上）。清掉的说法撑着的事实、碰过的实体，写完再重算、删没用了的
    # 清完先重算：说法清光了的事实删掉（冻结的留着），没用了的实体删掉，后面按名字对齐不会再对上它（撤销拆开时拆出来的那个）
    open_conflicts = conn.execute(
        "SELECT review_id, claims, created_at FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL"
    ).fetchall()
    cleared_facts: set[str] = set()
    cleared_entities: set[str] = set()
    cleared_relations: set[str] = set()
    for item in items:
        facts, entities, identities = writer._clear_window(item)
        cleared_facts |= facts
        cleared_entities |= entities
        cleared_relations |= identities
    writer._refresh(cleared_facts, vectors, set(), now)
    writer._refresh_relations(cleared_relations, set(), now)
    writer._drop_orphan_entities(cleared_entities)
    for item in sorted(items, key=_window_order):
        if not writer.write_window(item, vectors):
            raise MergeError(f"{item.window.window_id} 的包换了版本：这一次撤销了，再跑一次")
    writer._drop_orphan_entities(cleared_entities)
    report.windows = len(items)
    # 冻结标记、决定按说法认回来
    claims_after = _claims_in(conn, plan.windows)
    news: dict[str, set[str]] = {}
    lost: set[str] = set()
    for claim_id, old in claims_before.items():
        if claim_id in claims_after:
            news.setdefault(old, set()).add(claims_after[claim_id])
        else:
            lost.add(old)
    mapping = {old: next(iter(found)) for old, found in news.items() if len(found) == 1 and old not in lost}
    groups: set[str] = set()
    for old in sorted(frozen_before):
        new = mapping.get(old)
        if new is None:
            report.unmatched.append(old)
            continue
        if new == old:
            continue
        conn.execute("UPDATE facts SET frozen = 1 WHERE fact_id = ?", (new,))
        report.frozen_moved.append(new)
    moved = {old: new for old, new in mapping.items() if old != new}
    _remap_reviews(conn, moved, now, report)
    for old in sorted(moved):  # 说法都搬走了还留着的（冻结的空壳）：标记、决定已经跟着走了，删掉
        if old in frozen_before and conn.execute("SELECT 1 FROM fact_claims WHERE fact_id = ?", (old,)).fetchone() is None:
            writer._forget_fact(old)
    groups |= _repoint_claimless(conn, writer, plan.merged, now, report)
    _move_frozen_relations(conn, writer, relation_claims_before, frozen_relations_before, report)
    _repoint_claimless_relations(conn, writer, plan.merged, now)
    # 冻结标记搬过去的、待确认改指过的事实所在的组都重算：主人原话那种冲突（D204）定过的，重写窗口时决定还指着旧编号，
    # 又进了一条没处理的，要照改指以后的决定再算一遍（审阅第 2 条）
    for fact_id in set(report.frozen_moved) | set(moved.values()):
        row = conn.execute("SELECT group_key FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
        if row is not None and row[0] is not None:
            groups.add(row[0])
    for group in sorted(groups):
        writer._supersede(group, now)
    _restore_conflicts(conn, open_conflicts, mapping, now, report)  # 冲突是上面重算新旧时才重新进队列的
    writer._drop_orphan_entities(touched_entities)
    _bookkeeping(conn, plan, now, report)
    register_lexicon(conn, lexicon, now)
    after_ids = set(plan.merged.values()) or touched_entities
    report.facts_after = len(_facts_of(conn, after_ids))
    report.merging = bool(plan.merged)
    entities_after = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT entity_id, canonical_name, type FROM entities")}
    report.others_changed = sorted(
        entity_id
        for entity_id in set(entities_before) ^ set(entities_after) | {e for e in entities_before if e in entities_after and entities_before[e] != entities_after[e]}
        if entity_id not in touched_entities
    )
    report.vectors_missing = conn.execute(
        "SELECT count(*) FROM facts AS f WHERE NOT EXISTS (SELECT 1 FROM fact_vectors AS v WHERE v.fact_id = f.fact_id)"
        " AND f.fact_id IN (SELECT fact_id FROM fact_claims WHERE window_id IN (SELECT value FROM json_each(?)))",
        (json.dumps(sorted(plan.windows)),),
    ).fetchone()[0] if model else 0


def run(
    settings: Settings,
    library: str,
    record: Mapping[str, object] | None = None,
    *,
    store_dir: Path | None = None,
    vectors_dir: Path | None = None,
    dry_run: bool = False,
) -> MergeReport:
    """写一条合并记录（record 是 None：只把还没生效的补上，--sync），在这本账上当场生效。dry_run：一个事务里做完再撤回，
    账本、合并记录都不动，只要那几个数（卡片上“合并以后”）。点名的不对、现在合不了的抛 MergeError，什么都没动。
    拿不到锁抛 IntakeBusy、WriteBusy。"""
    path = _lexicon_path(settings)
    data_dir = settings.require_data_dir()
    store_dir = store_dir or data_dir / "l3"
    vectors_dir = vectors_dir or data_dir / "vectors"
    library_cfg = settings.library(library)
    if not library_cfg.db_path.is_file():
        raise MergeError(f"{library} 库还没有账本")
    try:
        lexicon = lex.lexicon_with(path, [record] if record is not None else [])
    except ConfigError as error:
        raise MergeError(f"这一条和别名表、别的合并记录对不上，没写：{error}") from None
    report = MergeReport(library, dry_run=dry_run)
    with intake_lock(data_dir), WriteLock(data_dir):
        conn = open_ledger(library_cfg)
        try:
            todo = pending(conn, lexicon.records)
            if not todo:
                return report
            plan = _plan(conn, todo, lexicon.records)
            needs_store = bool(plan.windows)
            if needs_store and not (store_dir / STORE_FILE).is_file():
                raise MergeError(f"没有 L3 结果库（{store_dir}）：合并要从结果库重写窗口")
            if needs_store:
                with ResultStore(store_dir) as store:
                    items, written = _items(settings, library, store, conn, plan, lexicon)
            else:
                items, written = [], _Written("", None, "", "")
            if not dry_run and record is not None:
                lex.append_record(path, record)
            try:
                with transaction(conn):
                    _apply(conn, settings, lexicon, plan, items, written, vectors_dir, report)
                    if dry_run:
                        raise _DryRun(report)
            except _DryRun:
                pass
        finally:
            conn.close()
    return report


def history(settings: Settings, library: str) -> list[dict[str, object]]:
    """合并记录，按先后，带上在这本账上生效了没有。只有编号、名字、时间和决策号，没有原话。"""
    path = _lexicon_path(settings)
    records = lex.read_records(lex.merges_path(path))
    library_cfg = settings.library(library)
    applied: dict[str, sqlite3.Row] = {}
    if library_cfg.db_path.is_file():
        conn = open_ledger(library_cfg)
        try:
            applied = {row["merge_id"]: row for row in conn.execute("SELECT * FROM entity_merges")}
        finally:
            conn.close()
    undone = {str(record["undoes"]) for record in records if record["kind"] == "undo"}
    found = []
    for record in records:
        row = applied.get(str(record["id"]))
        if record["kind"] == "undo":
            target = applied.get(str(record["undoes"]))
            state = "已生效" if target is not None and target["undone_at"] is not None else "还没生效"
        elif record["id"] in undone:
            state = "撤销了" if row is None or row["undone_at"] is not None else "撤销还没生效"
        else:
            state = "已生效" if row is not None else "还没生效"
        found.append({**record, "state": state})
    return found
