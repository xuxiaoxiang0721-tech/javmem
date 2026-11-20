"""J4.2 后一半：L3 抽之前，把账本里已经记着的、两头都在这个窗口里的现行关系写成一段放进窗口文字（导航 D154 第 4 条，
J4.2“调用前放进已知实体和现有关系”的后一半；前一半是预处理 B 的〔指〕）。不调模型，只读账本。

**放哪些（r1）：**

- 只放现行的：没撤回、没被替代、生效或暂停、不是待确认的（J4.4 报告第 6 节的写法）。冻结的照放（主人认过的，说法没了的
  也放）；已结束的不放（放了模型容易把旧的又写一遍），待确认的不放（还没人认，不能借新窗口再进一次账）。
- **两头都在这个窗口里：** 认人认事〔指〕标出来的名字，加上账本里实体的名字、别名（两个字以上）原样出现在窗口的原文里
  （T、C 行）的。实体的名字哪个范围都认（和 L5 按名字对实体一样）；别名只认出处和这个窗口可见范围一样的（D30，和
  write._alias_candidates 一样：看不到的内容不能把两个叫法连起来）。一个写法对上几个实体的不算（看不出是哪一个）。
  全是英文数字的写法按词比（保留词中间的空格，前后不能连着字母数字），别的去掉空白比。主人哪个窗口都算在；Bot 是窗口里
  说话的、或者名字出现在原文里的才算。主人、Bot 不能当唯一点到的那一头：至少一头是窗口里点到的别的实体。
  只有一头在的不放：模型照着写了，另一头不在原文里，L4 的 R2 会挡（要用上得另改 R2 和判断 C 的问法）。
- 只放这个窗口看得见的：分区一样、敏感标记是窗口的子集（D30，和 preb、batch 同一条规矩）。
- **自己不证明自己：** 先把证据里这个窗口的正文包（哪个版本都算）去掉：去掉以后一条都不剩的不放（重发、重抽同一个包时
  不拿它上回写进账本的话当已知）；剩下的证据里最早那一句的说话时间，要早于这个窗口最后一句话（晚到的旧包、改回排队重抽的
  旧包，看不到“以后”才有人说的关系）。证据句的说话时间都没了的、冻结着没有证据的，照最后一次说到（as_of）算。
- 一个窗口最多 MAX_RELATIONS 条、MAX_CHARS 字。先放两头都不是主人、Bot 的，再按最后一次说到（as_of）从近到远；
  到了上限后面的不放，记下没放几条。

**怎么写：** 开头那句说明后面空一行，RELATIONS_HEADER 打头，一条一行（“- 主体 关系 客体（亲属的具体说法；代表哪一家；
状态；开始日子 起；上次说到哪天）”），在【上文】【正文】前面。没有编号，引不了，不能当证据。名字过打码模式表，〔〕【】
换成［］、换行换成空格，打完码还扫得出来的那一条不放。判断 C 取原话时只看【正文】以后的编号行（reljudge._parse_window），
L4 的模型复核发的也去掉这一段（recheck.build_request），它们都看不到。

**存下来、能重放：** 结果库的 prer_notes，按（窗口编号、没加标注的窗口文字的指纹、PRE_R_VERSION、preb.window_context、
这个窗口 B 的〔指〕的指纹 notes_digest）认：B 这一回没标上、下一回标上了的，照新的〔指〕重算，不拿先算的那一段顶着。
先存的为准，后来账本变了也不改；一条都没放的也存一行（空的）。核对、写账、合并工具照存下的加上（extract.prepare），
键和抽的时候一样。

**撤回连带：** sources 里记着放进去的关系当时的全部证据（包、版本、句子）；靠别名认出来的一头，记那个别名的一句出处；
写着的名字不是窗口自己的字的（靠别名、〔指〕认出来的一头，代表的那一家），再记一句名字的出处。撤回、彻底删除时 sources
里有撤回内容的窗口要删、要重抽（store.doomed、doomed_by_notes，只连一层）。出处的快照会因为包重发、窗口重写而对不上，
所以另外记下这一段写着的名字和认出两头用的别名（entities），撤回、彻底删除把账本改完以后照账本现在的样子核一遍
（stale_keys）：名字已经不是实体现在的名字、别名在这个范围里没出处了的，用过它的窗口也删、也重抽。

**开关跟着提示词**（extract.PRE_R_PROMPTS），不另加开关。抽的时候一个包一个包来、抽一个写一个（cli 的 extract 一次只抽
导出最早的那个包）：后面的包才看得到前面的包写进账本的关系。

例子都是编的。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from javmem.ledger import connect_readonly
from javmem.pipeline.preb import PRE_B_VERSION, window_context
from javmem.pipeline.render import RenderedWindow, escape_raw, one_line, render_window
from javmem.pipeline.store import RELATION_MARK, RELATIONS_HEADER, ResultStore
from javmem.pipeline.window import Window
from javmem.redaction import RedactionPattern, redact, scan_types
from javmem.timeutil import iso

PRE_R_VERSION = "r1"
MAX_RELATIONS = 40  # 一个窗口最多放几条
MAX_CHARS = 2000  # 一个窗口最多放多少字（每一条连换行算）
OWNER_NAME = "主人"  # 账本里主人那个实体的名字（和 merge.OWNER_NAME 一样；引 merge 会绕回 extract）
OWNER_TYPE = "人"
BOT_TYPE = "Bot"
STATE_TEXT = {"active": "生效", "paused": "暂停"}
_CURRENT = (
    "withdrawn_at IS NULL AND superseded_by IS NULL AND state IN ('active', 'paused') AND status <> 'pending'"
)
_CHUNK = 400  # IN (…) 里一次放几个
Proof = tuple[str, int, str]  # 一句出处：(包号, 版本, 句子编号)
ScopeKey = tuple[tuple[str, ...], tuple[str, ...]]  # (分区, 敏感标记)，都排好序


def _tools() -> tuple[Callable[[str], str], Callable[[str], str]]:
    """比对写法（verify.fold）和实体名字的键（merge.name_key）。用到时才导入：verify 导入 extract，extract 导入这里。"""
    from javmem.pipeline.merge import name_key
    from javmem.pipeline.verify import fold

    return fold, name_key


def _word_fold(text: str) -> str:
    """英文数字的写法按词比用的：NFKC、引号和横线照 verify.fold 统一、不分大小写，连着的空白并成一个空格（不像 verify.fold
    去掉空白，不然词就连上了）。"""
    from javmem.pipeline.verify import _DASHES, _QUOTE_MARKS

    return " ".join(unicodedata.normalize("NFKC", text).translate(_QUOTE_MARKS).translate(_DASHES).casefold().split())


def _now() -> str:
    return iso(datetime.now(timezone.utc).astimezone())


def notes_digest(notes: Mapping[str, object] | None) -> str:
    """这个窗口 B 的〔指〕的指纹（prer_notes 的键里有它）。没有标注的是空的。"""
    if not notes:
        return ""
    return hashlib.sha256(json.dumps(notes, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _scope_key(partitions: object, sensitive: object) -> ScopeKey:
    return tuple(sorted(set(json.loads(str(partitions))))), tuple(sorted(set(json.loads(str(sensitive or "[]")))))


# ---------- 读账本 ----------


@dataclass(frozen=True)
class Known:
    """账本里一条现行的关系（一段），和它的证据。"""

    relation_id: str
    subject_id: str
    relation_type: str
    object_id: str
    kinship: str | None
    org_id: str | None
    state: str
    valid_from: str | None
    as_of: str | None
    as_of_epoch: float
    frozen: bool
    partitions: tuple[str, ...]
    sensitive: frozenset[str]
    evidence: tuple[tuple[str, int, str, float | None], ...]  # (包号, 版本, 句子编号, 说话时间)，按说的先后

    @property
    def packets(self) -> frozenset[str]:
        return frozenset(packet_id for packet_id, _, _, _ in self.evidence)


def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


def current_relations(conn: sqlite3.Connection, entity_ids: Collection[str] | None = None) -> list[Known]:
    """只读：账本里现行的关系（J4.4 报告第 6 节：没撤回、没被替代、生效或暂停、不是待确认的）。entity_ids 给了就只要一头是
    这几个实体的（按 subject_entity_id、object_entity_id 查，两列都有索引）。证据（包、版本、句子，和那一句的说话时间）
    一起带上。按编号排。"""
    columns = (
        "relation_id, subject_entity_id, relation_type, object_entity_id, kinship, org_entity_id, state, valid_from,"
        " as_of, as_of_epoch, frozen, scope_partitions, scope_sensitive"
    )
    if entity_ids is None:
        rows = [tuple(row) for row in conn.execute(f"SELECT {columns} FROM relations WHERE {_CURRENT} ORDER BY relation_id")]
    else:
        found: dict[str, tuple[object, ...]] = {}
        for part in _chunks(sorted(set(entity_ids))):
            marks = ",".join("?" * len(part))
            for row in conn.execute(
                f"SELECT {columns} FROM relations WHERE {_CURRENT}"
                f" AND (subject_entity_id IN ({marks}) OR object_entity_id IN ({marks}))",
                (*part, *part),
            ):
                found[str(row[0])] = tuple(row)
        rows = [found[key] for key in sorted(found)]
    evidence: dict[str, list[tuple[str, int, str, float | None]]] = {}
    for part in _chunks([str(row[0]) for row in rows]):
        marks = ",".join("?" * len(part))
        for relation_id, packet_id, revision, sentence_id, said in conn.execute(
            "SELECT v.relation_id, v.packet_id, v.revision, v.sentence_id, s.said_at_epoch FROM relation_evidence AS v"
            " LEFT JOIN raw_sentences AS s ON s.sentence_id = v.sentence_id AND s.revision = v.revision"  # 旧版本的句子对不上：没有时间
            f" WHERE v.relation_id IN ({marks}) ORDER BY v.evidence_id",
            tuple(part),
        ):
            evidence.setdefault(str(relation_id), []).append(
                (str(packet_id), int(revision), str(sentence_id), float(said) if said is not None else None)
            )
    out = []
    for row in rows:
        relation_id = str(row[0])
        partitions, sensitive = _scope_key(row[11], row[12])
        out.append(
            Known(
                relation_id=relation_id,
                subject_id=str(row[1]),
                relation_type=str(row[2]),
                object_id=str(row[3]),
                kinship=row[4],  # type: ignore[arg-type]
                org_id=row[5],  # type: ignore[arg-type]
                state=str(row[6]),
                valid_from=row[7],  # type: ignore[arg-type]
                as_of=row[8],  # type: ignore[arg-type]
                as_of_epoch=float(row[9]) if row[9] is not None else 0.0,
                frozen=bool(row[10]),
                partitions=partitions,
                sensitive=frozenset(sensitive),
                evidence=tuple(evidence.get(relation_id, ())),
            )
        )
    return out


# ---------- 写成一段 ----------


def clean_name(text: object) -> str:
    """写进这一段的名字、说法：换行换成空格，〔〕【】换成［］（原文写不出标注、段名，这里也一样），两头的空白去掉。"""
    return escape_raw(one_line(str(text))).replace("【", "［").replace("】", "］").strip()


def relation_line(
    subject: str,
    relation_type: str,
    obj: str,
    *,
    kinship: str | None = None,
    org: str | None = None,
    state: str,
    valid_from: str | None = None,
    as_of: str | None,
) -> str:
    """一条：“- 主体 关系 客体（亲属的具体说法；代表哪一家；状态；开始日子 起；上次说到哪天）”，没有的不写。"""
    details = []
    if kinship:
        details.append(clean_name(kinship))
    if org:
        details.append(f"代表 {clean_name(org)}")
    details.append(STATE_TEXT.get(state, clean_name(state)))
    if valid_from:
        details.append(f"{clean_name(valid_from)} 起")
    if as_of:
        details.append(f"上次说到 {str(as_of)[:10]}")
    return f"{RELATION_MARK}{clean_name(subject)} {clean_name(relation_type)} {clean_name(obj)}（{'；'.join(details)}）"


def apply_block(rendered: RenderedWindow, block: str) -> RenderedWindow:
    """把这一段放进窗口文字：开头那句说明（第一行）后面。别的行一字不变；shown（L4 比引文用的原文）不变。空的原样返回。"""
    if not block:
        return rendered
    rows = rendered.text.split("\n")
    text = "\n".join([rows[0], *block.split("\n"), *rows[1:]])
    return replace(rendered, text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


@dataclass(frozen=True)
class Picked:
    """一个窗口挑出来的：那一段（空的是一条都没有）、放了哪几条关系、撤回连带要看的出处、写着的名字和认出两头用的别名、数。"""

    block: str
    relations: list[str]
    sources: list[list[object]]  # [包号, 版本, 句子编号]
    detail: dict[str, int] = field(default_factory=dict)
    # 撤回以后照账本核一遍用（stale_keys）：{"scope": [分区, 敏感标记], "shown": [[实体, 写着的名字]], "alias": [[实体, 别名的键]]}
    entities: dict[str, object] = field(default_factory=dict)


def _found(text: str, folded: str, words: bool) -> bool:
    """folded 在 text 里。words（全是英文数字的写法）：前后不能连着字母数字（“QZXV”里没有“QZ”）。"""
    start = text.find(folded)
    while start != -1:
        if not words:
            return True
        end = start + len(folded)
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if not (before.isascii() and before.isalnum()) and not (after.isascii() and after.isalnum()):
            return True
        start = text.find(folded, start + 1)
    return False


@dataclass(frozen=True)
class _Match:
    """一个写法认的是哪个实体：是不是它的名字（canonical），是别名的话是哪几个别名的键（aliases，名字和别名同一个写法的
    只算名字），这个写法在账本里的一句出处（proofs，别名的；配置里来的名字可能一句都没有）。"""

    entity_id: str
    canonical: bool
    aliases: tuple[str, ...]
    proofs: tuple[Proof, ...]


@dataclass
class _Hit:
    """这个窗口里认出了一个实体：是不是按它的名字认出来的，靠了哪几个别名，那几个写法的出处。"""

    canonical: bool = False
    aliases: set[str] = field(default_factory=set)
    proofs: list[Proof] = field(default_factory=list)

    def add(self, match: _Match) -> None:
        self.canonical = self.canonical or match.canonical
        if not match.canonical:
            self.aliases |= set(match.aliases)
            self.proofs.extend(match.proofs)


class LedgerView:
    """一次读完账本里要用的：实体的名字、别名和它们的出处、主人、Bot、现行的关系。只读。"""

    def __init__(
        self,
        names: Mapping[str, str],
        name_keys: Mapping[str, str],
        alias_rows: Sequence[tuple[str, str, str, ScopeKey, Proof]],
        owner: str | None,
        bots: Collection[str],
        relations: Sequence[Known],
    ) -> None:
        self.names = dict(names)  # 实体编号 → 名字
        self.name_keys = dict(name_keys)  # 实体编号 → 名字的键
        self.alias_rows = list(alias_rows)  # (实体编号, 写法, 键, 出处的范围, 这个范围里最早那一句出处)
        self.owner = owner
        self.bots = frozenset(bots)
        self.anchorless = self.bots | ({owner} if owner else set())
        self.relations = list(relations)
        self.by_entity: dict[str, list[Known]] = {}
        for known in self.relations:
            self.by_entity.setdefault(known.subject_id, []).append(known)
            if known.object_id != known.subject_id:
                self.by_entity.setdefault(known.object_id, []).append(known)
        # 名字的出处：和名字同一个键的别名（L5 建实体时名字也记成别名）的一句，哪个范围都算
        self.name_proofs: dict[str, Proof] = {}
        for entity_id, _, key, _, proof in self.alias_rows:
            if key == self.name_keys.get(entity_id) and entity_id not in self.name_proofs:
                self.name_proofs[entity_id] = proof
        self._indexes: dict[ScopeKey, tuple[dict[str, _Match], list[tuple[str, bool, _Match]]]] = {}

    @classmethod
    def load(cls, path: Path) -> LedgerView:
        """读账本（只读）。还没有账本的是空的。"""
        if not path.exists():
            return cls({}, {}, [], None, (), [])
        _, name_key = _tools()
        conn = connect_readonly(path)
        try:
            conn.execute("BEGIN")  # 一次读完，读到一半写账也不会前后对不上
            try:
                entities = conn.execute("SELECT entity_id, canonical_name, type, name_key FROM entities").fetchall()
                aliases = conn.execute(
                    "SELECT a.entity_id, a.alias, a.alias_key, p.partitions, p.sensitive, v.packet_id, v.revision, v.sentence_id"
                    " FROM aliases AS a JOIN evidence AS v ON v.alias_id = a.alias_id"
                    " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision"
                    " WHERE a.withdrawn_at IS NULL AND p.state = 'current' AND p.withdrawn_at IS NULL"
                    " ORDER BY v.evidence_id"
                ).fetchall()
                relations = current_relations(conn)
            finally:
                conn.execute("ROLLBACK")
        finally:
            conn.close()
        names, keys, owner, bots = {}, {}, None, set()
        owner_key = name_key(OWNER_NAME)
        for entity_id, canonical, kind, key in entities:
            entity_id = str(entity_id)
            names[entity_id] = str(canonical)
            keys[entity_id] = str(key or name_key(str(canonical)))
            if kind == BOT_TYPE:
                bots.add(entity_id)
            elif kind == OWNER_TYPE and keys[entity_id] == owner_key:
                owner = entity_id
        first: dict[tuple[str, str, str, ScopeKey], Proof] = {}
        for entity_id, alias, key, partitions, sensitive, packet_id, revision, sentence_id in aliases:
            scope = _scope_key(partitions, sensitive)
            spot = (str(entity_id), str(alias), str(key or name_key(str(alias))), scope)
            first.setdefault(spot, (str(packet_id), int(revision), str(sentence_id)))  # 按 evidence_id：最早那一句
        rows = [(entity_id, alias, key, scope, proof) for (entity_id, alias, key, scope), proof in first.items()]
        return cls(names, keys, rows, owner, bots, relations)

    def _index(self, scope: ScopeKey) -> tuple[dict[str, _Match], list[tuple[str, bool, _Match]]]:
        """这个可见范围里能用的写法：按键（认〔指〕的名字用）、按原文里的写法（认原文用）。实体的名字哪个范围都认，别名只认
        出处和这个窗口一样范围的（D30）。一个写法对上几个实体的去掉（看不出是哪一个）；一个字的不认。"""
        if scope in self._indexes:
            return self._indexes[scope]
        fold, _ = _tools()
        spellings: list[tuple[str, str, str, bool, Proof | None]] = [  # (键, 写法, 实体编号, 是不是名字, 别名的出处)
            (key, self.names[entity_id], entity_id, True, None) for entity_id, key in self.name_keys.items()
        ]
        spellings += [
            (key, alias, entity_id, False, proof)
            for entity_id, alias, key, alias_scope, proof in self.alias_rows
            if alias_scope == scope  # D30：别名只认出处和这个窗口一样范围的
        ]

        def matches(groups: Mapping[object, list[tuple[str, str, bool, Proof | None]]]) -> dict[object, _Match]:
            out = {}
            for spot, found in groups.items():
                if len({entity_id for entity_id, _, _, _ in found}) == 1:  # 对上几个实体的不认
                    canonical = any(is_name for _, _, is_name, _ in found)
                    aliases = () if canonical else tuple(sorted({key for _, key, _, _ in found}))
                    proofs = () if canonical else tuple(dict.fromkeys(proof for _, _, _, proof in found if proof is not None))
                    out[spot] = _Match(found[0][0], canonical, aliases, proofs)
            return out

        keyed: dict[object, list[tuple[str, str, bool, Proof | None]]] = {}
        texts: dict[object, list[tuple[str, str, bool, Proof | None]]] = {}
        for key, text, entity_id, is_name, proof in spellings:
            keyed.setdefault(key, []).append((entity_id, key, is_name, proof))
            words = _word_fold(text)
            spot = (words, True) if words.isascii() else (fold(text), False)  # 全是英文数字的按词比
            if len(spot[0].replace(" ", "")) >= 2:
                texts.setdefault(spot, []).append((entity_id, key, is_name, proof))
        by_key = {str(key): match for key, match in matches(keyed).items()}
        literals = [(spot[0], spot[1], match) for spot, match in matches(texts).items()]  # type: ignore[index]
        self._indexes[scope] = (by_key, literals)
        return by_key, literals

    def named(
        self, window: Window, rendered: RenderedWindow, notes: Mapping[str, Sequence[Sequence[str]]]
    ) -> dict[str, _Hit]:
        """这个窗口里点到的实体，和是怎么认出来的：〔指〕标的名字，加上名字、这个范围能用的别名原样出现在窗口原文（T、C 行）里的。"""
        fold, name_key = _tools()
        by_key, literals = self._index((tuple(window.scope.partitions), tuple(window.scope.sensitive)))
        found: dict[str, _Hit] = {}
        for pairs in notes.values():
            for pair in pairs:
                match = by_key.get(name_key(str(pair[1]))) if len(pair) >= 2 else None
                if match is not None:
                    found.setdefault(match.entity_id, _Hit()).add(match)
        shown = [rendered.shown.get(line.ref, "") for line in window.lines]
        folded = "\x00".join(fold(text) for text in shown)
        worded = "\x00".join(_word_fold(text) for text in shown)
        for text, words, match in literals:
            if _found(worded if words else folded, text, words):
                found.setdefault(match.entity_id, _Hit()).add(match)
        return found

    def _in_text(self, window: Window, rendered: RenderedWindow, name: str) -> bool:
        """这个名字原样出现在窗口原文里没有（写着的名字是窗口自己的字，就不是从别处来的）。"""
        fold, _ = _tools()
        words = _word_fold(name)
        shown = [rendered.shown.get(line.ref, "") for line in window.lines]
        if words.isascii():
            return _found("\x00".join(_word_fold(text) for text in shown), words, True)
        return _found("\x00".join(fold(text) for text in shown), fold(name), False)

    def pick(
        self,
        window: Window,
        rendered: RenderedWindow,
        notes: Mapping[str, Sequence[Sequence[str]]],
        patterns: Sequence[RedactionPattern],
    ) -> Picked:
        """照开头说的挑这个窗口放哪几条，写成一段。rendered 是没加标注的那份（render_window 写出来的）。"""
        _, name_key = _tools()
        named = self.named(window, rendered, notes)
        present = set(named)
        if self.owner is not None:
            present.add(self.owner)
        by_key, _ = self._index((tuple(window.scope.partitions), tuple(window.scope.sensitive)))
        for speaker in {block.speaker for block in window.blocks}:
            keys = {name_key(speaker)}
            if " <" in speaker and speaker.endswith(">"):  # 邮件的发件人“名字 <地址>”
                display, _, address = speaker[:-1].rpartition(" <")
                keys |= {name_key(display), name_key(address)}
            for key in keys:
                match = by_key.get(key)
                if match is not None and match.entity_id in self.bots:
                    present.add(match.entity_id)
        anchors = set(named) - self.anchorless
        targets = set(window.target_packets)
        said = [block.said_at.timestamp() for block in window.blocks if not block.context]
        last = max(said) if said else float("-inf")
        partitions = tuple(sorted(window.scope.partitions))
        sensitive = set(window.scope.sensitive)
        seen: set[str] = set()
        candidates: list[Known] = []
        for entity_id in sorted(anchors):
            for known in self.by_entity.get(entity_id, ()):
                if known.relation_id in seen:
                    continue
                seen.add(known.relation_id)
                if known.subject_id not in present or known.object_id not in present:
                    continue  # 两头都要在这个窗口里
                if known.partitions != partitions or not known.sensitive <= sensitive:
                    continue  # 这个窗口看不见（D30）
                others = [one for one in known.evidence if one[0] not in targets]
                if known.evidence and not others:
                    continue  # 证据全在这个窗口的正文包里：自己不证明自己
                if not known.evidence and not known.frozen:
                    continue
                times = [one[3] for one in others if one[3] is not None]
                first = min(times) if times else known.as_of_epoch
                if first >= last:
                    continue  # 去掉这个窗口自己的，这个窗口最后一句话以后才有人说的
                candidates.append(known)
        candidates.sort(
            key=lambda one: (1 if {one.subject_id, one.object_id} & self.anchorless else 0, -one.as_of_epoch, one.relation_id)
        )
        lines: list[str] = []
        used: list[Known] = []
        chars = unredacted = 0
        over = 0
        for index, known in enumerate(candidates):
            line = relation_line(
                self.names.get(known.subject_id, known.subject_id),
                known.relation_type,
                self.names.get(known.object_id, known.object_id),
                kinship=known.kinship if known.relation_type == "亲属" else None,
                org=self.names.get(known.org_id) if known.org_id else None,
                state=known.state,
                valid_from=known.valid_from,
                as_of=known.as_of,
            )
            line, _ = redact(line, patterns)
            if scan_types(line, patterns):
                unredacted += 1  # 打完码还扫得出来：这一条不放
                continue
            if len(used) >= MAX_RELATIONS or chars + len(line) + 1 > MAX_CHARS:
                over = len(candidates) - index
                break
            lines.append(line)
            used.append(known)
            chars += len(line) + 1
        sources: list[list[object]] = []
        shown_names: list[list[str]] = []
        alias_keys: list[list[str]] = []
        for known in used:
            sources.extend([packet_id, revision, sentence_id] for packet_id, revision, sentence_id, _ in known.evidence)
            for entity_id in (known.subject_id, known.object_id, known.org_id):
                if not entity_id:
                    continue
                name = self.names.get(entity_id, entity_id)
                shown_names.append([entity_id, name])
                hit = named.get(entity_id)
                # 写着的名字不是窗口自己的字（靠别名、〔指〕认出来的一头，代表的那一家）：记一句名字的出处，多一道连带。
                # 主人、Bot 的名字是配置里的，不记
                if (entity_id in self.name_proofs and entity_id not in self.anchorless and not (hit is not None and hit.canonical)
                        and not self._in_text(window, rendered, name)):
                    sources.append(list(self.name_proofs[entity_id]))
            for entity_id in (known.subject_id, known.object_id):
                hit = named.get(entity_id)
                if hit is not None and not hit.canonical:  # 靠别名认出来的一头：别名的出处、别名的键（撤回以后核它还在不在）
                    sources.extend(list(proof) for proof in hit.proofs)
                    alias_keys.extend([entity_id, key] for key in sorted(hit.aliases))
        unique: list[list[object]] = []
        for source in sources:
            if source not in unique:
                unique.append(source)
        block = "\n".join(["", RELATIONS_HEADER, *lines]) if lines else ""
        return Picked(
            block=block,
            relations=[known.relation_id for known in used],
            sources=unique,
            detail={"candidates": len(candidates), "used": len(used), "over_cap": over, "unredacted": unredacted},
            entities={
                "scope": [list(partitions), sorted(sensitive)],
                "shown": [list(one) for one in dict.fromkeys(tuple(one) for one in shown_names)],
                "alias": [list(one) for one in dict.fromkeys(tuple(one) for one in alias_keys)],
            },
        )


# ---------- 抽之前算好、存进结果库 ----------


class PreR:
    """一次 extract 用一个：annotate 每次都重读账本（一包一包抽，前面的包刚写进去）；block_for（空跑用）读一次就留着。"""

    def __init__(self, ledger_path: Path, *, log: Callable[[str], None] | None = None) -> None:
        self.ledger_path = ledger_path
        self.log = log
        self.stats: Counter[str] = Counter()
        self._dry: LedgerView | None = None

    def describe(self) -> dict[str, object]:
        return {"version": PRE_R_VERSION, "max_relations": MAX_RELATIONS, "max_chars": MAX_CHARS}

    def block_for(
        self,
        window: Window,
        rendered: RenderedWindow,
        notes: Mapping[str, Sequence[Sequence[str]]],
        patterns: Sequence[RedactionPattern],
    ) -> str:
        """当场算这个窗口放什么，不存（--dry-run 用）。rendered 是没加标注的那份。"""
        if self._dry is None:
            self._dry = LedgerView.load(self.ledger_path)
        return self._dry.pick(window, rendered, notes, patterns).block

    def annotate(self, windows: Sequence[Window], store: ResultStore, patterns: Sequence[RedactionPattern]) -> None:
        """这几个窗口没算过的，照现在的账本和库里这个窗口现在的〔指〕算好、存进结果库。private 的、打码没打干净的不放
        （本来就不发）。"""
        view: LedgerView | None = None
        counts: Counter[str] = Counter()
        for window in windows:
            if window.scope.private:
                continue
            plain = render_window(window, patterns)
            if plain.leftover:
                continue
            context = window_context(window)
            notes = store.preb_notes(window.window_id, plain.sha256, PRE_B_VERSION, context) or {}
            digest = notes_digest(notes)
            if store.prer_notes(window.window_id, plain.sha256, PRE_R_VERSION, context, digest) is not None:
                counts["存过的窗口"] += 1
                continue
            if view is None:
                view = LedgerView.load(self.ledger_path)
            picked = view.pick(window, plain, notes, patterns)
            store.save_prer_notes(
                {
                    "window_id": window.window_id,
                    "window_sha": plain.sha256,
                    "version": PRE_R_VERSION,
                    "context": context,
                    "notes_sha": digest,
                    "packets": json.dumps([list(one) for one in window.packets]),
                    "sentences": json.dumps(sorted({line.sentence_id for line in window.lines}), ensure_ascii=False),
                    "block": picked.block,
                    "relations": json.dumps(picked.relations),
                    "sources": json.dumps(picked.sources, ensure_ascii=False),
                    "entities": json.dumps(picked.entities, ensure_ascii=False, sort_keys=True),
                    "detail": json.dumps(picked.detail, ensure_ascii=False, sort_keys=True),
                    "created_at": _now(),
                }
            )
            counts["算过的窗口"] += 1
            counts["放进去的关系"] += picked.detail["used"]
            counts["超了上限没放的"] += picked.detail["over_cap"]
            counts["打码没打干净没放的"] += picked.detail["unredacted"]
        self.stats.update(counts)
        if self.log is not None and counts:
            self.log(f"[R] 现有关系（{PRE_R_VERSION}）：{json.dumps(dict(sorted(counts.items())), ensure_ascii=False)}")


# ---------- 撤回以后照账本核一遍 ----------


def stale_keys(conn: sqlite3.Connection, store: ResultStore) -> list[tuple[str, str, str, str, str]]:
    """撤回、彻底删除把账本改完以后调（账本已经按剩下的内容改了名、删了别名）：结果库里放过的那一段，写着的名字已经不是
    这个实体现在的名字、这个写法在账本里也没有看得到的出处了（实体没了也算），或者认出一头用的别名在那个可见范围里已经没有
    出处了的，这些行的键（窗口编号, 没加标注的指纹, 版本, context, notes_sha）。用过它们的窗口要删、要重抽：名字可能就出自
    撤回的内容。出处的快照会因为包重发、窗口重写而对不上，这里按账本现在的样子核，不靠快照。名字换了、旧的写法还有看得到的
    出处的（后来对上别名表的标准名、合并）不算：那个写法不是撤回的内容。没记下这些的旧行一律算。只读账本。"""
    _, name_key = _tools()
    names = {str(row[0]): str(row[1]) for row in conn.execute("SELECT entity_id, canonical_name FROM entities")}
    alive: set[tuple[str, str, ScopeKey]] = set()
    for entity_id, key, partitions, sensitive in conn.execute(
        "SELECT DISTINCT a.entity_id, a.alias_key, p.partitions, p.sensitive FROM aliases AS a"
        " JOIN evidence AS v ON v.alias_id = a.alias_id"
        " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision"
        " WHERE a.withdrawn_at IS NULL AND p.state = 'current' AND p.withdrawn_at IS NULL"
    ):
        alive.add((str(entity_id), str(key), _scope_key(partitions, sensitive)))
    alive_anywhere = {(entity_id, key) for entity_id, key, _ in alive}
    stale = []
    for row in store.prer_rows():
        key = (str(row["window_id"]), str(row["window_sha"]), str(row["version"]), str(row["context"]), str(row["notes_sha"]))
        if not row["block"]:
            continue  # 一条都没放：没有名字
        try:
            entities = json.loads(row["entities"]) if row["entities"] else None
        except ValueError:
            entities = None
        if not isinstance(entities, dict):
            stale.append(key)  # 旧行（没记写着的名字）：照撤回连带算
            continue
        partitions, sensitive = entities.get("scope") or [[], []]
        scope: ScopeKey = (tuple(sorted(partitions)), tuple(sorted(sensitive)))
        if any(
            str(entity_id) not in names
            or (names[str(entity_id)] != str(name) and (str(entity_id), name_key(str(name))) not in alive_anywhere)
            for entity_id, name in entities.get("shown") or []
        ):
            stale.append(key)
            continue
        if any((str(entity_id), str(alias_key), scope) not in alive for entity_id, alias_key in entities.get("alias") or []):
            stale.append(key)
    return stale
