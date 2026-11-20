"""认人认事（J3.6，D94、D100）：合并工具。管理命令，只给主人和 CC，和 review、freeze 一样；主人不敲命令，CC 把确认卡片
做成主会话里的选择卡，一张一件事，主人选了 CC 照着跑。

**合并一律人工确认，留历史**（04 p9）：错合比错分伤得多，拿不准的不并。

**提议从哪来**（proposals，找提议不调模型）：

- **别名表**（lexicon）：一组的名字在账本里现在对得上两个以上实体。分两档：实体自己的名字就在组里的“默认并”；
  只是挂着组里的一个叫法当别名的（一只股票的累购也挂着股票代码）“默认不并”，要人勾。
- **L5 的歧义**（ambiguous）：待确认里没处理的“实体有歧义”，那个实体连同它像的几个。
- **账本里找重复**（ledger）：同一类型里共用一个别名的；写法一样、类型不一样的；名字里带同一个编号的（ISIN、
  交易编号、带数字的证券代码，最强）；一个的名字是另一个整词的开头的（同一类型，出处在同一个分区）；去掉公司后缀、
  括号以后一样的；中文名只差一个字的（不是头一个字，标“像笔误”）。一个简称是两个以上全名整词的开头的，不提合并，
  提“记成不是同一个”，标“疑似撞名”。
- 不提的：记成“不是同一个”的；主人（主人走主人的名字的配置，D62）；Bot 名单里的、别名表里不建实体的。

每条提议有个编号：P 加“来源和几个实体编号排好序”的哈希前 8 位，实体没变，编号就不变。建议的标准名、类型：别名表里写了的
用它；不然用事实最多的那个实体的。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field

from javmem.pipeline.merge import OWNER_NAME, entity_id_of, name_key
from javmem.zh.lexicon import BOT, AliasGroup, Lexicon

LEXICON = "lexicon"
AMBIGUOUS = "ambiguous"
LEDGER = "ledger"
SOURCES = (LEXICON, AMBIGUOUS, LEDGER)
SOURCE_TEXT = {LEXICON: "别名表", AMBIGUOUS: "L5 的歧义", LEDGER: "账本里找的"}
MERGE = "merge"
APART = "apart"

# 提议上要标出来的
CROSS_PARTITION = "出处在不同分区"
HAS_FROZEN = "有冻结的事实"
TYPE_CHANGE = "要改类型"
TYPO = "像笔误"
CLASH = "疑似撞名"

# 名字里的编号：ISIN（两个字母、九个字母或数字、一个数字）、带数字的证券代码（6127 HK、4421.T）、长的交易编号
_ISIN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}[A-Z0-9]{9}[0-9](?![A-Z0-9])")
_TICKER = re.compile(r"(?<![A-Za-z0-9])\d{3,6}(?:\s?|\.)(?:HK|JT|JP|T|SS|SZ|SH|US|SI|KS)(?![A-Za-z0-9])")
_DEAL = re.compile(r"(?<![A-Za-z0-9])[A-Z]{0,4}-?\d{7,}(?![A-Za-z0-9])")
_WORDS = re.compile(r"[A-Za-z][A-Za-z.'\-]*")
_PARENS = re.compile(r"\s*[(（][^()（）]*[)）]\s*")


@dataclass
class EntityInfo:
    """卡片、清单上的一个实体。只有编号和数，名字是给主人在会话里看的。"""

    entity_id: str
    name: str
    type: str
    subject_facts: int = 0
    other_facts: int = 0  # 当对象、提及的
    first: str | None = None  # 事实最早、最晚的日子
    last: str | None = None
    partitions: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    frozen: int = 0

    @property
    def facts(self) -> int:
        return self.subject_facts + self.other_facts


@dataclass
class Proposal:
    proposal_id: str
    source: str
    kind: str  # merge 并；apart 记成不是同一个
    entities: list[str]  # 默认并的在前
    default: list[str]  # 默认勾上的
    name: str  # 建议的标准名
    type: str  # 建议的类型
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    group: str | None = None  # 别名表的哪一组（第几组、合并记录的编号）
    hung: dict[str, list[str]] = field(default_factory=dict)  # 默认不并的：挂着组里的哪几个叫法

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def proposal_id(source: str, entity_ids: Iterable[str]) -> str:
    return "P" + hashlib.sha256("\n".join(("javmem-proposal", source, *sorted(set(entity_ids)))).encode("utf-8")).hexdigest()[:8]


# ---------- 实体的样子 ----------


def entity_info(conn: sqlite3.Connection, entity_id: str) -> EntityInfo | None:
    row = conn.execute("SELECT entity_id, canonical_name, type FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
    if row is None:
        return None
    info = EntityInfo(row["entity_id"], row["canonical_name"], row["type"])
    facts = conn.execute(
        "SELECT f.fact_id, f.subject_entity_id, f.as_of, f.scope_partitions, f.frozen FROM facts AS f"
        " WHERE f.withdrawn_at IS NULL AND (f.subject_entity_id = ?1"
        " OR EXISTS (SELECT 1 FROM fact_entities AS x WHERE x.fact_id = f.fact_id AND x.entity_id = ?1))",
        (entity_id,),
    ).fetchall()
    partitions: set[str] = set()
    days = []
    for fact in facts:
        if fact["subject_entity_id"] == entity_id:
            info.subject_facts += 1
        else:
            info.other_facts += 1
        info.frozen += int(bool(fact["frozen"]))
        partitions.update(json.loads(fact["scope_partitions"]))
        if fact["as_of"]:
            days.append(str(fact["as_of"])[:10])
    for alias in conn.execute(
        "SELECT DISTINCT a.alias, p.partitions FROM aliases AS a JOIN evidence AS v ON v.alias_id = a.alias_id"
        " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision WHERE a.entity_id = ? ORDER BY a.alias",
        (entity_id,),
    ):
        if alias["alias"] not in info.aliases:
            info.aliases.append(alias["alias"])
        partitions.update(json.loads(alias["partitions"]))
    info.first, info.last = (min(days), max(days)) if days else (None, None)
    info.partitions = sorted(partitions)
    return info


# ---------- 提议 ----------


@dataclass(frozen=True)
class _Entity:
    entity_id: str
    name: str
    type: str
    key: str


class _Finder:
    def __init__(self, conn: sqlite3.Connection, lexicon: Lexicon) -> None:
        self.conn = conn
        self.lexicon = lexicon
        owner = entity_id_of("人", OWNER_NAME)
        rows = conn.execute("SELECT entity_id, canonical_name, type, name_key FROM entities ORDER BY entity_id").fetchall()
        self.entities = {
            row["entity_id"]: _Entity(row["entity_id"], row["canonical_name"], row["type"], row["name_key"] or name_key(row["canonical_name"]))
            for row in rows
            if row["entity_id"] != owner and not self._left_alone(row["canonical_name"], row["type"])
        }
        self.aliases: dict[str, set[str]] = {}  # 实体 → 别名的比对写法（有出处的）
        for row in conn.execute(
            "SELECT DISTINCT a.entity_id, a.alias_key, a.alias FROM aliases AS a"
            " WHERE EXISTS (SELECT 1 FROM evidence AS v WHERE v.alias_id = a.alias_id)"
        ):
            if row["entity_id"] in self.entities:
                self.aliases.setdefault(row["entity_id"], set()).add(row["alias_key"] or name_key(row["alias"]))
        self.facts = {
            entity_id: count
            for entity_id, count in conn.execute(
                "SELECT subject_entity_id, count(*) FROM facts WHERE subject_entity_id IS NOT NULL AND withdrawn_at IS NULL"
                " GROUP BY subject_entity_id"
            )
        }
        self.partitions: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT x.entity_id, f.scope_partitions FROM fact_entities AS x JOIN facts AS f ON f.fact_id = x.fact_id"
            " WHERE f.withdrawn_at IS NULL"
        ):
            self.partitions.setdefault(row["entity_id"], set()).update(json.loads(row["scope_partitions"]))

    def _left_alone(self, name: str, entity_type: str) -> bool:
        """不提的：Bot（Bot 名单会处理）、别名表里不建实体的。"""
        if entity_type == BOT:
            return True
        return self.lexicon.bot_of(name, "", BOT, ()) is not None or self.lexicon.not_entity(name)

    def _apart(self, entity_ids: Sequence[str]) -> bool:
        """这几个里有两个记成了“不是同一个”。"""
        found = [self.entities[entity_id] for entity_id in entity_ids if entity_id in self.entities]
        for one in found:
            apart = self.lexicon.apart_from(one.key, one.type)
            if any(other.key in apart for other in found if other is not one):
                return True
        return False

    def _suggest(self, entity_ids: Sequence[str]) -> _Entity:
        """事实最多的那个；一样多的按编号。"""
        return max((self.entities[entity_id] for entity_id in entity_ids), key=lambda one: (self.facts.get(one.entity_id, 0), -_order(one.entity_id)))

    def proposal(
        self,
        source: str,
        entity_ids: Iterable[str],
        reasons: Sequence[str],
        *,
        default: Iterable[str] | None = None,
        group: AliasGroup | None = None,
        kind: str = MERGE,
        flags: Sequence[str] = (),
    ) -> Proposal | None:
        every = [entity_id for entity_id in dict.fromkeys(entity_ids) if entity_id in self.entities]
        if len(every) < 2 or (kind == MERGE and self._apart(every)):
            return None
        ticked = list(dict.fromkeys(default if default is not None else every))
        ordered = [*ticked, *(entity_id for entity_id in every if entity_id not in ticked)]
        if group is not None:
            name, entity_type = group.standard, group.type
        else:
            best = self._suggest(ticked or every)
            name, entity_type = best.name, best.type
        found = list(flags)
        types = {self.entities[entity_id].type for entity_id in ticked}
        if types - {entity_type} and kind == MERGE:
            found.append(TYPE_CHANGE)
        partitions = {frozenset(self.partitions.get(entity_id, set())) for entity_id in ticked} - {frozenset()}
        if len(partitions) > 1:
            found.append(CROSS_PARTITION)
        if any(self._frozen(entity_id) for entity_id in ticked):
            found.append(HAS_FROZEN)
        return Proposal(
            proposal_id(source, every),
            source,
            kind,
            ordered,
            ticked,
            name,
            entity_type,
            list(reasons),
            list(dict.fromkeys(found)),
            group.origin if group is not None else None,
        )

    def _frozen(self, entity_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM facts AS f WHERE f.frozen = 1 AND (f.subject_entity_id = ?1"
                " OR EXISTS (SELECT 1 FROM fact_entities AS x WHERE x.fact_id = f.fact_id AND x.entity_id = ?1)) LIMIT 1",
                (entity_id,),
            ).fetchone()
            is not None
        )

    # ---------- 三处来源 ----------

    def from_lexicon(self) -> list[Proposal]:
        found = []
        for group in self.lexicon.groups:
            keys = {name_key(name) for name in group.names}
            named = [
                one.entity_id
                for one in self.entities.values()
                if one.type in group.types
                and (one.key in keys or self.lexicon.group_of(one.name, one.type, ()) is group)
            ]
            hung = [
                one.entity_id
                for one in self.entities.values()
                if one.type in group.types and one.entity_id not in named and self.aliases.get(one.entity_id, set()) & keys
            ]
            if len(named) + len(hung) < 2:
                continue
            reasons = [f"别名表里的一组（{group.origin}），账本里现在分成 {len(named) + len(hung)} 个"]
            if hung:
                reasons.append(f"其中 {len(hung)} 个只是挂着这一组的叫法当别名，默认不并")
            proposal = self.proposal(LEXICON, [*named, *hung], reasons, default=named, group=group)
            if proposal is not None:
                for entity_id in hung:
                    texts = [row[0] for row in self.conn.execute("SELECT alias FROM aliases WHERE entity_id = ? ORDER BY alias", (entity_id,))]
                    proposal.hung[entity_id] = [text for text in texts if name_key(text) in keys]
                found.append(proposal)
        return found

    def from_ambiguities(self) -> list[Proposal]:
        found = []
        for row in self.conn.execute(
            "SELECT review_id, claims FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL ORDER BY review_id"
        ):
            claims = json.loads(row["claims"])
            every = [str(claims.get("entity_id")), *(str(item) for item in claims.get("candidates") or [])]
            reason = f"待确认 #{row['review_id']}：名字同时对上了这几个实体，L5 没并"
            proposal = self.proposal(AMBIGUOUS, every, [reason])
            if proposal is not None:
                found.append(proposal)
        return found

    def from_ledger(self) -> list[Proposal]:
        found: list[Proposal] = []
        add = found.append
        by_type: dict[str, list[_Entity]] = {}
        for one in self.entities.values():
            by_type.setdefault(one.type, []).append(one)
        # 同一类型里共用一个别名
        shared: dict[tuple[str, str], list[str]] = {}
        for entity_id, keys in self.aliases.items():
            for key in keys:
                shared.setdefault((self.entities[entity_id].type, key), []).append(entity_id)
        for (_, key), entity_ids in sorted(shared.items()):
            if len(entity_ids) >= 2:
                proposal = self.proposal(LEDGER, entity_ids, ["同一类型里共用一个别名"])
                if proposal is not None:
                    add(proposal)
        # 写法一样、类型不一样
        same_key: dict[str, list[str]] = {}
        for one in self.entities.values():
            same_key.setdefault(one.key, []).append(one.entity_id)
        for key, entity_ids in sorted(same_key.items()):
            if len({self.entities[entity_id].type for entity_id in entity_ids}) >= 2:
                proposal = self.proposal(LEDGER, entity_ids, ["写法一样、类型不一样"])
                if proposal is not None:
                    add(proposal)
        # 名字里带同一个编号
        codes: dict[str, set[str]] = {}
        for one in self.entities.values():
            texts = [one.name, *self._alias_texts(one.entity_id)]
            for code in {code for text in texts for code in _codes_in(text)}:
                codes.setdefault(code, set()).add(one.entity_id)
        for code, entity_ids in sorted(codes.items()):
            if len(entity_ids) >= 2:
                proposal = self.proposal(LEDGER, sorted(entity_ids), ["名字里带同一个编号"])
                if proposal is not None:
                    add(proposal)
        # 去掉公司后缀、括号以后一样
        stripped: dict[tuple[str, str], list[str]] = {}
        for one in self.entities.values():
            key = _bare_key(one.name, one.type)
            if key and key != one.key:
                stripped.setdefault((one.type, key), []).append(one.entity_id)
        for (entity_type, key), entity_ids in sorted(stripped.items()):
            others = [one.entity_id for one in by_type.get(entity_type, []) if one.key == key]
            proposal = self.proposal(LEDGER, [*others, *entity_ids], ["去掉公司后缀、括号以后一样"])
            if proposal is not None:
                add(proposal)
        for entity_type, items in sorted(by_type.items()):
            found.extend(self._prefixes(entity_type, items))
            found.extend(self._typos(items))
        return found

    def _alias_texts(self, entity_id: str) -> list[str]:
        return [row[0] for row in self.conn.execute("SELECT alias FROM aliases WHERE entity_id = ?", (entity_id,))]

    def _prefixes(self, entity_type: str, items: Sequence[_Entity]) -> list[Proposal]:
        """一个的名字是另一个整词的开头（英文名），同一类型、出处在同一个分区。一个简称是两个以上全名的开头的，
        不提合并，提把那几个全名记成“不是同一个”。"""
        found = []
        latin = [one for one in items if _WORDS.fullmatch(one.name.replace(" ", "")) and " " not in one.name.strip()]
        for short in latin:
            words = short.name.strip().casefold()
            longer = [
                one
                for one in items
                if one is not short
                and one.name.strip().casefold().startswith(words + " ")
                and self.partitions.get(one.entity_id, set()) & self.partitions.get(short.entity_id, set())
            ]
            if len(longer) == 1:
                proposal = self.proposal(LEDGER, [short.entity_id, longer[0].entity_id], ["一个的名字是另一个整词的开头"])
            elif len(longer) >= 2:
                proposal = self.proposal(
                    LEDGER,
                    [one.entity_id for one in longer],
                    [f"一个简称是这 {len(longer)} 个全名整词的开头：不并，记成不是同一个，以后不再按这个简称连起来"],
                    kind=APART,
                    flags=[CLASH],
                )
            else:
                continue
            if proposal is not None:
                found.append(proposal)
        return found

    def _typos(self, items: Sequence[_Entity]) -> list[Proposal]:
        """中文名只差一个字（三个字以上、一样长、头一个字一样）。读音没核，只当提议。"""
        found = []
        cjk = [one for one in items if len(one.key) >= 3 and all(_is_cjk(char) for char in one.key)]
        for index, one in enumerate(cjk):
            for other in cjk[index + 1 :]:
                if len(one.key) != len(other.key) or one.key[0] != other.key[0]:
                    continue
                if sum(a != b for a, b in zip(one.key, other.key)) == 1:
                    proposal = self.proposal(LEDGER, [one.entity_id, other.entity_id], ["中文名只差一个字"], flags=[TYPO])
                    if proposal is not None:
                        found.append(proposal)
        return found


def _order(entity_id: str) -> int:
    return int(hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:8], 16)


def _is_cjk(char: str) -> bool:
    return unicodedata.name(char, "").startswith("CJK UNIFIED")


def _codes_in(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", text)
    found = {match.group(0) for match in _ISIN.finditer(folded)}
    found |= {re.sub(r"[\s.]", "", match.group(0)).upper() for match in _TICKER.finditer(folded)}
    found |= {match.group(0).replace("-", "") for match in _DEAL.finditer(folded)}
    return found


def _bare_key(name: str, entity_type: str) -> str:
    """去掉括号里的说明、公司的法律后缀以后的比对写法。"""
    from javmem.pipeline.merge import _LEGAL_SUFFIX

    text = _PARENS.sub(" ", unicodedata.normalize("NFKC", name)).strip()
    if entity_type == "公司或机构":
        text = _LEGAL_SUFFIX.sub("", text).strip(" ,")
    return name_key(text)


def find_proposals(conn: sqlite3.Connection, lexicon: Lexicon, sources: Sequence[str] = SOURCES) -> list[Proposal]:
    """所有提议，按来源（别名表、L5 的歧义、账本里找的）的先后；同样一组实体只留先来的那一条，理由合在一起。"""
    finder = _Finder(conn, lexicon)
    found: dict[frozenset[str], Proposal] = {}
    for source in SOURCES:
        if source not in sources:
            continue
        items = {LEXICON: finder.from_lexicon, AMBIGUOUS: finder.from_ambiguities, LEDGER: finder.from_ledger}[source]()
        for item in items:
            key = frozenset(item.entities)
            if key in found:
                found[key].reasons.extend(reason for reason in item.reasons if reason not in found[key].reasons)
                found[key].flags.extend(flag for flag in item.flags if flag not in found[key].flags)
                continue
            found[key] = item
    return list(found.values())


def proposal_by_id(conn: sqlite3.Connection, lexicon: Lexicon, wanted: str) -> Proposal | None:
    return next((item for item in find_proposals(conn, lexicon) if item.proposal_id == wanted), None)


def describe(conn: sqlite3.Connection, proposal: Proposal) -> dict[str, EntityInfo]:
    return {entity_id: info for entity_id in proposal.entities if (info := entity_info(conn, entity_id)) is not None}


def proposal_line(proposal: Proposal, infos: Mapping[str, EntityInfo]) -> str:
    """清单上的一行：编号、来源、几个实体、建议的标准名。"""
    what = "记成不是同一个" if proposal.kind == APART else f"并成「{proposal.name}」（{proposal.type}）"

    def one(entity_id: str) -> str:
        info = infos.get(entity_id)
        return f"{entity_id}「{info.name}」{info.type} {info.facts} 条" if info is not None else entity_id

    ticked = "、".join(one(entity_id) for entity_id in proposal.default)
    rest = [entity_id for entity_id in proposal.entities if entity_id not in proposal.default]
    text = f"{proposal.proposal_id} {SOURCE_TEXT[proposal.source]}：{what}：{ticked}"
    if rest:
        text += f"；默认不并：{'、'.join(one(entity_id) for entity_id in rest)}"
    if proposal.flags:
        text += f"（{'，'.join(proposal.flags)}）"
    return text


# ---------- 确认卡片（--show） ----------


@dataclass(frozen=True)
class Quote:
    """一句原话：卡片上给主人看（private 的包里的也给看，标出来），送模型时 private 的不送。"""

    entity_id: str
    sentence_id: str
    packet_id: str
    day: str
    speaker: str
    text: str
    private: bool


def quotes_of(conn: sqlite3.Connection, entity_id: str, limit: int = 2, *, sendable: bool = False) -> list[Quote]:
    """这个实体的几句原话，先说的在前：它的事实的证据、它的别名的出处。撤回了的不要；sendable 的话 private 的包里的也不要。"""
    rows = conn.execute(
        "SELECT DISTINCT v.sentence_id, v.packet_id, s.text, s.speaker, s.said_at, s.said_at_epoch, s.id, p.privacy"
        " FROM evidence AS v JOIN raw_sentences AS s ON s.sentence_id = v.sentence_id AND s.revision = v.revision"
        " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision"
        " WHERE s.withdrawn_at IS NULL AND p.withdrawn_at IS NULL AND p.purged_at IS NULL AND ("
        " v.fact_id IN (SELECT f.fact_id FROM facts AS f WHERE f.withdrawn_at IS NULL AND (f.subject_entity_id = ?1"
        " OR EXISTS (SELECT 1 FROM fact_entities AS x WHERE x.fact_id = f.fact_id AND x.entity_id = ?1)))"
        " OR v.alias_id IN (SELECT alias_id FROM aliases WHERE entity_id = ?1))"
        " ORDER BY s.said_at_epoch, s.id",
        (entity_id,),
    ).fetchall()
    found: list[Quote] = []
    for row in rows:
        private = row["privacy"] == "private"
        if sendable and private or any(item.sentence_id == row["sentence_id"] for item in found):
            continue
        found.append(Quote(entity_id, row["sentence_id"], row["packet_id"], str(row["said_at"])[:10], row["speaker"], row["text"], private))
        if len(found) >= limit:
            break
    return found


def sendable_of(conn: sqlite3.Connection, info: EntityInfo):  # -> samecheck.Sendable | None
    """这个实体能送去问模型的：名字和叫法要在不是 private 的包里有出处（标准名没有的用有出处的叫法代替），原话不送
    private 的。一个能送的名字都没有的返回 None。"""
    from javmem.samecheck import QUOTES_PER_ENTITY, Sendable

    names = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT a.alias FROM aliases AS a JOIN evidence AS v ON v.alias_id = a.alias_id"
            " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision"
            " WHERE a.entity_id = ? AND p.privacy = 'normal' AND p.withdrawn_at IS NULL ORDER BY v.evidence_id",
            (info.entity_id,),
        )
    ]
    if not names:
        return None
    name = next((alias for alias in names if name_key(alias) == name_key(info.name)), names[0])
    quotes = quotes_of(conn, info.entity_id, QUOTES_PER_ENTITY, sendable=True)
    return Sendable(
        name,
        info.type,
        tuple(alias for alias in dict.fromkeys(names) if alias != name),
        info.facts,
        info.first,
        info.last,
        tuple((quote.sentence_id, quote.packet_id, quote.day, quote.speaker, quote.text) for quote in quotes),
    )


def new_entity_id(proposal: Proposal) -> str:
    """合并以后的编号：按类型和标准名算（和 L5 照别名表对出来的一样）。"""
    return entity_id_of(proposal.type, name_key(proposal.name))


def card_lines(
    proposal: Proposal,
    infos: Mapping[str, EntityInfo],
    quotes: Mapping[str, Sequence[Quote]],
    judgment_line: str,
    after: Sequence[str] = (),
) -> list[str]:
    """确认卡片（设计稿 3.2.2）：有原话，是给主人在会话里看的，不进日志。after 是“合并以后”那几行。"""

    def entity_line(entity_id: str, ticked: bool) -> str:
        info = infos[entity_id]
        days = f"{info.first} 到 {info.last}" if info.first and info.last and info.first != info.last else (info.first or "没有日子")
        where = "、".join(info.partitions) or "没有出处"
        text = f"  [{'x' if ticked else ' '}] {entity_id}「{info.name}」　{info.type}　主体 {info.subject_facts} 条，涉及 {info.other_facts} 条　{days}　出处在 {where}"
        if info.frozen:
            text += f"　冻结的 {info.frozen} 条"
        if entity_id in proposal.hung:
            text += f"　别名里有{'、'.join(f'“{alias}”' for alias in proposal.hung[entity_id])}"
        return text

    lines = [f"{'合并提议' if proposal.kind == MERGE else '提议'} {proposal.proposal_id}　来源：{SOURCE_TEXT[proposal.source]}" + (f"（{proposal.group}）" if proposal.group else "")]
    if proposal.kind == MERGE:
        lines.append(f"建议：并成「{proposal.name}」（{proposal.type}），编号 {new_entity_id(proposal)}（按标准名算）")
        lines.append("要并的（默认勾上）：")
        lines += [entity_line(entity_id, True) for entity_id in proposal.default]
        rest = [entity_id for entity_id in proposal.entities if entity_id not in proposal.default]
        if rest:
            lines.append("只是挂着这一组的叫法、默认不并的：" if proposal.hung else "默认不并的：")
            lines += [entity_line(entity_id, False) for entity_id in rest]
    else:
        lines.append("建议：记成不是同一个，以后不再提、L5 也不按别名把它们连起来")
        lines += [entity_line(entity_id, True) for entity_id in proposal.entities]
    lines.append(f"为什么提：{'；'.join(proposal.reasons)}")
    if proposal.flags:
        lines.append(f"要留神：{'；'.join(proposal.flags)}")
    lines.append(judgment_line)
    lines.append("原话（每个实体两句）：")
    for entity_id in proposal.entities:
        for quote in quotes.get(entity_id, ()):
            mark = "〔private〕" if quote.private else ""
            lines.append(f"  {entity_id}　{mark}『{quote.text}』 {quote.sentence_id} · {quote.speaker} · {quote.day}")
    if after:
        lines.append("合并以后：")
        lines += [f"  {line}" for line in after]
    if proposal.kind == MERGE:
        names = list(dict.fromkeys([proposal.name, *(infos[entity_id].name for entity_id in proposal.default if entity_id in infos)]))
        choices = [f"{number} 合并，标准名用「{name}」" for number, name in enumerate(names, start=1)]
        choices += [f"{len(names) + 1} 不是同一个", f"{len(names) + 2} 先放着"]
    else:
        choices = ["1 记成不是同一个", "2 先放着"]
    lines.append("选：" + "　".join(choices))
    return lines
