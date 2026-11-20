"""在写过账的账本上找软变化的候选，配上它可能改到的旧东西，写成题（J4.5，04 p4）。只读账本，不调模型。

**候选句**（原文索引里新写的话，不是引用，没撤回）：

1. 有前三组犹豫词（暂停、往后推、拿不准，javmem.soft.words）的句子；
2. L3 标了含糊的说法（事实说法 `certainty = vague`；关系说法 `state = paused`、`certainty = vague`，J4.4 写了关系才有）
   的证据句；
3. 只有后四组词（可能、条件、打算、问）的句子，要它抽出的说法和一条更早说的旧东西对得上（见下面第 1 步）才算。

**配旧的：**

1. 这句话是哪些说法的证据：说法所在的那一组（事实：可见范围、主体、属性，`group_key`；关系：J4.4 定的 `group_key`）里，
   在这句话之前说过、那时还现行的别的事实或关系，取最后说的那一条。
2. 第 1 步没配上、又是第 1 种候选句的：这句话提到的实体（这句话当证据的说法里的实体、按名字和别名在句子里找到的；
   主人不算，主人的事实太多），它们在这句话之前现行的事实和关系，关系排前面，再按和这句话字面挨得多近、最后一次说到的
   先后，最多取 MAX_BY_ENTITY 条。
3. 只配同一个可见范围的（分区、敏感标记一样，D30）。一条都没配上的不出题：没有旧的就没东西可改，L3 照常记成打算。

**题的样子：** 要判的就是这一句；前后文是同一个包里它前面 BEFORE_LINES 行、后面 AFTER_LINES 行，再加上每条旧的
最后一次被说的那一行（不在这几行里的）。发件人那一句撤回了的信，说话人照 L1 写成“[已撤回]”。private 的包照样出题、
标上 private，judge 不发。题号是句子编号和旧的编号算出来的，同一本账本找几次都一样（换个进程、换个哈希种子也一样）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from javmem.pipeline.merge import OWNER_NAME, name_key, usable_alias
from javmem.pipeline.verify import fold
from javmem.raw.sentences import MASKED
from javmem.soft import words
from javmem.soft.cases import Case, Line, OldItem
from javmem.timeutil import parse_aware

FIND_VERSION = "soft-find-v1"
_HELD = ("soft_change", "manual_review")  # L5 先挡住的待确认（J4.4）：不当旧的
BEFORE_LINES = 8
AFTER_LINES = 3
MAX_BY_ENTITY = 3
_MAX_OLD = 5  # 一道题最多几条旧的


@dataclass(frozen=True)
class _Sentence:
    sentence_id: str
    packet_id: str
    revision: int
    seq: int
    text: str
    speaker: str
    role: str
    at: str
    epoch: int
    quoted: bool


@dataclass
class _Item:
    """一条事实或关系，配旧的用。statements：它的每一句证据的说话时间（epoch, 句子编号）。"""

    kind: str
    id: str
    key: str | None
    group: str | None
    subject: str | None
    entities: set[str]
    text: str
    since: str | None
    state: str | None
    scope: tuple[str, str]
    superseded_at: int | None
    statements: list[tuple[int, str]] = field(default_factory=list)
    held: bool = False  # L5 先挡住、待确认的（软变化、判断 C 拿不准的）：不现行，不拿来当“旧的”（J4.4 第三轮审阅 N5）

    def last_before(self, epoch: int) -> tuple[int, str] | None:
        earlier = [one for one in self.statements if one[0] < epoch]
        return max(earlier) if earlier else None

    def current_at(self, epoch: int) -> bool:
        """这一刻之前说过，那时还没被替代。"""
        return self.last_before(epoch) is not None and (self.superseded_at is None or self.superseded_at >= epoch)


@dataclass
class FindResult:
    cases: list[Case]
    stats: dict[str, int]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _scope(partitions: str, sensitive: str) -> tuple[str, str]:
    return (json.dumps(sorted(set(json.loads(partitions or "[]"))), ensure_ascii=False),
            json.dumps(sorted(set(json.loads(sensitive or "[]"))), ensure_ascii=False))


def _epoch(value: str | None) -> int | None:
    moment = parse_aware(value) if value else None
    return int(moment.timestamp()) if moment is not None else None


def _proof_ids(proofs: str) -> list[str]:
    try:
        data = json.loads(proofs)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(one.get("sentence_id")) for one in data if isinstance(one, dict) and one.get("sentence_id")]


_CJK = re.compile(r"[㐀-鿿]")
_WORD = re.compile(r"[a-z0-9]{2,}")


def _terms(text: str) -> set[str]:
    """字面挨得多近用的：中文两个字一段、英文和数字一个词一段。"""
    folded = unicodedata.normalize("NFKC", text).casefold()
    cjk = "".join(char if _CJK.match(char) else " " for char in folded)
    out = {cjk[n : n + 2] for n in range(len(cjk) - 1) if " " not in cjk[n : n + 2]}
    return out | set(_WORD.findall(folded))


_NAME_EDGES = "。，、；：！？.,;:!?…—–-~·()（）[]［］【】「」『』《》〈〉<>\"'"  # 和 name_key 去掉的两头标点一样
_SPACE = re.compile(r"\s+")


class _Names:
    """按名字、别名（原样写的，不是比对写法）在句子里找实体。有中文的：比对写法（name_key）在句子的比对写法里按字串找；
    全是英文字母、数字的：按整词找，名字中间的空白有几个都行（Owen Lee、owen  lee），AQ 不算 AQUA 里的。"""

    def __init__(self, pairs: Iterable[tuple[str, str]]) -> None:
        self.cjk: list[tuple[str, str]] = []
        self.ascii: dict[str, set[str]] = {}  # 去掉空白的写法 → 实体
        pieces: dict[str, str] = {}  # 去掉空白的写法 → 正则里的一段
        for name, entity_id in sorted(set(pairs)):
            key = name_key(name)
            if not key:
                continue
            if not key.isascii():
                self.cjk.append((key, entity_id))
                continue
            parts = unicodedata.normalize("NFKC", name).casefold().strip().strip(_NAME_EDGES).split()
            joined = "".join(parts)
            if not joined:
                continue
            self.ascii.setdefault(joined, set()).add(entity_id)
            pieces.setdefault(joined, r"\s*".join(re.escape(part) for part in parts))
        alternation = "|".join(pieces[joined] for joined in sorted(pieces, key=lambda one: (-len(one), one)))
        self.pattern = re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])") if alternation else None

    def find(self, text: str) -> set[str]:
        folded = fold(text)
        found = {entity_id for key, entity_id in self.cjk if key in folded}
        if self.pattern is not None:
            spaced = unicodedata.normalize("NFKC", text).casefold()
            for match in self.pattern.finditer(spaced):
                found |= self.ascii.get(_SPACE.sub("", match.group(0)), set())
        return found


class _Ledger:
    """找候选要的那几张表，一次读进来（只读）。scan：只在这几个包里找候选（旧的哪个包的都算）。"""

    def __init__(self, conn: sqlite3.Connection, scan: Collection[str] | None) -> None:
        self.conn = conn
        rows = conn.execute(
            "SELECT packet_id, revision, partitions, sensitive, privacy FROM packets WHERE state = 'current' AND withdrawn_at IS NULL"
        ).fetchall()
        self.packets = {row["packet_id"]: row for row in rows}
        self.scan = set(self.packets) if scan is None else set(scan) & set(self.packets)
        self.sentences: dict[str, _Sentence] = {}
        self.by_packet: dict[str, list[_Sentence]] = {}
        # 发件人那一句撤回了的信（按事实撤回，信里别的句子还在，D73 第 16 条）：这封信的说话人照 L1 写成“[已撤回]”
        masked = {(row[0], row[1], row[2]) for row in conn.execute(
            "SELECT packet_id, revision, message_id FROM raw_sentences"
            " WHERE field = 'from' AND withdrawn_at IS NOT NULL AND message_id IS NOT NULL")}
        if _has_table(conn, "masked_mails"):
            masked |= {(row[0], row[1], row[2]) for row in conn.execute("SELECT packet_id, revision, message_id FROM masked_mails")}
        for row in conn.execute(
            "SELECT sentence_id, packet_id, revision, message_id, seq, text, speaker, speaker_role, said_at, said_at_epoch, quoted"
            " FROM raw_sentences WHERE section_kind = 'raw' AND field IS NULL AND withdrawn_at IS NULL ORDER BY packet_id, seq"
        ):
            packet = self.packets.get(row["packet_id"])
            if packet is None or packet["revision"] != row["revision"]:
                continue
            speaker = MASKED if (row["packet_id"], row["revision"], row["message_id"]) in masked else row["speaker"]
            sentence = _Sentence(row["sentence_id"], row["packet_id"], row["revision"], row["seq"], row["text"], speaker,
                                 row["speaker_role"], row["said_at"], row["said_at_epoch"], bool(row["quoted"]))
            self.sentences[sentence.sentence_id] = sentence
            self.by_packet.setdefault(sentence.packet_id, []).append(sentence)
        self.entities = {row["entity_id"]: row["canonical_name"] for row in conn.execute("SELECT entity_id, canonical_name FROM entities")}
        self.owner = {entity_id for entity_id, name in self.entities.items() if name_key(name) == name_key(OWNER_NAME)}
        self.items: dict[str, _Item] = {}
        self.claims_of: dict[str, list[tuple[str, bool]]] = {}  # 句子 → [(事实或关系的编号, 这一处是不是含糊的)]
        self._facts()
        if _has_table(conn, "relations"):
            self._relations()
        self.groups: dict[str, list[_Item]] = {}
        for item in self.items.values():
            if item.group:
                self.groups.setdefault(item.group, []).append(item)
        self.by_entity: dict[str, list[_Item]] = {}
        for item in self.items.values():
            for entity_id in item.entities:
                self.by_entity.setdefault(entity_id, []).append(item)
        pairs = [(name, entity_id) for entity_id, name in self.entities.items()
                 if entity_id not in self.owner and entity_id in self.by_entity and usable_alias(name)]
        pairs += [(row["alias"], row["entity_id"])
                  for row in conn.execute("SELECT entity_id, alias FROM aliases WHERE withdrawn_at IS NULL")
                  if row["entity_id"] not in self.owner and row["entity_id"] in self.by_entity and usable_alias(row["alias"])]
        self.names = _Names(pairs)

    def _facts(self) -> None:
        involved: dict[str, set[str]] = {}
        for row in self.conn.execute("SELECT fact_id, entity_id FROM fact_entities"):
            involved.setdefault(row["fact_id"], set()).add(row["entity_id"])
        for row in self.conn.execute(
            "SELECT fact_id, merge_key, group_key, subject_entity_id, content, attribute, value, occurred_from,"
            " scope_partitions, scope_sensitive, superseded_at, pending_reason FROM facts WHERE withdrawn_at IS NULL"
        ):
            text = row["content"]
            if row["attribute"] and row["value"] and str(row["value"]) not in text:
                text = f"{text}（{row['attribute']}：{row['value']}）"
            entities = set(involved.get(row["fact_id"], set()))
            if row["subject_entity_id"]:
                entities.add(row["subject_entity_id"])
            self.items[row["fact_id"]] = _Item(
                "fact", row["fact_id"], row["merge_key"], row["group_key"], row["subject_entity_id"], entities - self.owner, text,
                row["occurred_from"], None, _scope(row["scope_partitions"], row["scope_sensitive"]), _epoch(row["superseded_at"]),
                held=row["pending_reason"] in _HELD,
            )
        for row in self.conn.execute(
            "SELECT e.fact_id, e.sentence_id, s.said_at_epoch FROM evidence AS e JOIN raw_sentences AS s"
            " ON s.sentence_id = e.sentence_id WHERE e.fact_id IS NOT NULL"
        ):
            item = self.items.get(row["fact_id"])
            if item is not None:
                item.statements.append((row["said_at_epoch"], row["sentence_id"]))
        for row in self.conn.execute("SELECT fact_id, certainty, proofs FROM fact_claims"):
            for sentence_id in _proof_ids(row["proofs"]):
                self.claims_of.setdefault(sentence_id, []).append((row["fact_id"], row["certainty"] == "vague"))

    def _relations(self) -> None:
        states = {"active": "生效", "paused": "暂停", "ended": "已结束"}
        for row in self.conn.execute(
            "SELECT relation_id, merge_key, group_key, subject_entity_id, relation_type, object_entity_id, kinship, state,"
            " valid_from, scope_partitions, scope_sensitive, superseded_at, pending_reason FROM relations WHERE withdrawn_at IS NULL"
        ):
            subject = self.entities.get(row["subject_entity_id"], row["subject_entity_id"])
            target = self.entities.get(row["object_entity_id"], row["object_entity_id"])
            relation = f"{row['relation_type']}（{row['kinship']}）" if row["kinship"] else row["relation_type"]
            self.items[row["relation_id"]] = _Item(
                "relation", row["relation_id"], row["merge_key"], row["group_key"], row["subject_entity_id"],
                {row["subject_entity_id"], row["object_entity_id"]} - self.owner, f"{subject} {relation} {target}",
                row["valid_from"], states.get(row["state"], row["state"]), _scope(row["scope_partitions"], row["scope_sensitive"]),
                _epoch(row["superseded_at"]), held=row["pending_reason"] in _HELD,
            )
        for row in self.conn.execute(
            "SELECT e.relation_id, e.sentence_id, s.said_at_epoch FROM relation_evidence AS e JOIN raw_sentences AS s"
            " ON s.sentence_id = e.sentence_id"
        ):
            item = self.items.get(row["relation_id"])
            if item is not None:
                item.statements.append((row["said_at_epoch"], row["sentence_id"]))
        for row in self.conn.execute("SELECT relation_id, state, certainty, proofs FROM relation_claims"):
            soft = row["state"] == "paused" or row["certainty"] == "vague"
            for sentence_id in _proof_ids(row["proofs"]):
                self.claims_of.setdefault(sentence_id, []).append((row["relation_id"], soft))


def _by_group(ledger: _Ledger, sentence: _Sentence, scope: tuple[str, str]) -> list[_Item]:
    """第 1 步：这句话当证据的说法，同一组里这句话之前现行的别的事实或关系，各组取最后说的那一条。"""
    found: dict[str, _Item] = {}
    for item_id, _ in ledger.claims_of.get(sentence.sentence_id, []):
        item = ledger.items.get(item_id)
        if item is None or not item.group:
            continue
        olds = [other for other in ledger.groups.get(item.group, []) if other.id != item.id and other.scope == scope
                and not other.held and other.current_at(sentence.epoch)]
        if olds:
            latest = max(olds, key=lambda other: (other.last_before(sentence.epoch), other.id))
            found[latest.id] = latest
    return list(found.values())


def _by_entity(ledger: _Ledger, sentence: _Sentence, scope: tuple[str, str], taken: Collection[str]) -> list[_Item]:
    """第 2 步：这句话提到的实体，它们在这句话之前现行的事实和关系：关系排前面，再按字面挨得多近、最后说到的先后。"""
    entities = set()
    for item_id, _ in ledger.claims_of.get(sentence.sentence_id, []):
        item = ledger.items.get(item_id)
        if item is not None:
            entities |= item.entities
    entities |= ledger.names.find(sentence.text)
    terms = _terms(sentence.text)
    names = {entity_id: _terms(ledger.entities.get(entity_id, "")) for entity_id in entities}
    pool: dict[str, tuple[int, tuple[int, str], _Item]] = {}
    for entity_id in sorted(entities - ledger.owner):  # 排好序：集合的先后跟着哈希种子变，题号不能跟着变
        for item in ledger.by_entity.get(entity_id, []):
            if item.id in taken or item.scope != scope or item.held or not item.current_at(sentence.epoch):
                continue
            if any(sentence.sentence_id == sid for _, sid in item.statements):
                continue  # 就是这句话说的
            shared = len((_terms(item.text) - names.get(entity_id, set())) & terms)
            last = item.last_before(sentence.epoch)
            assert last is not None
            if item.id not in pool or shared > pool[item.id][0]:  # 从几个实体都配得上的：取挨得最近的那个算法
                pool[item.id] = (shared, last, item)
    # 关系排前面（暂停、换人多半说的是关系），再按字面挨得多近、最后说到的先后
    ranked = sorted(pool.values(), key=lambda one: (one[2].kind != "relation", -one[0], -one[1][0], one[2].id))
    return [item for _, _, item in ranked[:MAX_BY_ENTITY]]


def _line(sentence: _Sentence, ref: str) -> Line:
    return Line(ref, sentence.at, sentence.speaker, sentence.role, sentence.text, sentence.sentence_id, sentence.quoted)


def _case(ledger: _Ledger, sentence: _Sentence, olds: Sequence[_Item], groups: Sequence[str], how: Mapping[str, str]) -> Case:
    packet = ledger.packets[sentence.packet_id]
    rows = ledger.by_packet[sentence.packet_id]
    index = next(n for n, one in enumerate(rows) if one.sentence_id == sentence.sentence_id)
    chosen = {one.sentence_id: one for one in rows[max(0, index - BEFORE_LINES) : index + AFTER_LINES + 1]}
    for item in olds:
        last = item.last_before(sentence.epoch)
        if last is not None and last[1] in ledger.sentences:
            chosen.setdefault(last[1], ledger.sentences[last[1]])
    ordered = sorted(chosen.values(), key=lambda one: (one.epoch, one.packet_id, one.seq))
    lines = [_line(one, f"T{n}") for n, one in enumerate(ordered, 1)]
    target = next(line.ref for line in lines if line.sid == sentence.sentence_id)
    old_items = []
    for n, item in enumerate(olds, 1):
        last = item.last_before(sentence.epoch)
        last_said = ledger.sentences[last[1]].at[:10] if last is not None and last[1] in ledger.sentences else None
        old_items.append(OldItem(f"O{n}", item.kind, item.text, item.state, item.since, last_said, item.id, item.key))
    digest = hashlib.sha256(json.dumps([sentence.sentence_id, [item.id for item in olds]]).encode("utf-8")).hexdigest()
    return Case(
        f"C{digest[:12]}", tuple(old_items), tuple(lines), (target,), sentence.at[:10], None, packet["privacy"] == "private",
        tuple(sorted(set(json.loads(packet["partitions"] or "[]")))),
        {"find": FIND_VERSION, "words": words.WORDS_VERSION, "sentence_id": sentence.sentence_id, "packet_id": sentence.packet_id,
         "groups": list(groups), "olds_by": dict(how)},
    )


def find_cases(conn: sqlite3.Connection, *, packets: Iterable[str] | None = None, limit: int | None = None) -> FindResult:
    """在账本上找候选，写成题。packets：只看这几个包（默认所有现行的）。"""
    ledger = _Ledger(conn, set(packets) if packets is not None else None)
    stats = {"sentences": 0, "strong": 0, "vague_claims": 0, "weak": 0, "no_old": 0, "cases": 0, "private": 0}
    cases: list[Case] = []
    for packet_id in sorted(ledger.scan & set(ledger.by_packet)):
        packet = ledger.packets[packet_id]
        scope = _scope(packet["partitions"], packet["sensitive"])
        for sentence in ledger.by_packet[packet_id]:
            if sentence.quoted:
                continue
            stats["sentences"] += 1
            groups = words.hedges(sentence.text)
            claims = ledger.claims_of.get(sentence.sentence_id, [])
            strong = words.is_strong(groups)
            vague = any(soft for _, soft in claims)
            if not (strong or vague or groups):
                continue
            stats["strong" if strong else "vague_claims" if vague else "weak"] += 1
            olds = _by_group(ledger, sentence, scope)
            how = {item.id: "group" for item in olds}
            if strong and not olds:
                olds = _by_entity(ledger, sentence, scope, how)
                how.update({item.id: "entity" for item in olds})
            if not olds:
                stats["no_old"] += 1
                continue
            olds = olds[:_MAX_OLD]
            case = _case(ledger, sentence, olds, groups, {f"O{n}": how[item.id] for n, item in enumerate(olds, 1)})
            cases.append(case)
            stats["cases"] += 1
            stats["private"] += case.private
            if limit is not None and len(cases) >= limit:
                return FindResult(cases, stats)
    return FindResult(cases, stats)
