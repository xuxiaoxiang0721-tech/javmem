"""从账本和归档里取要处理的包，做完 L1。

按 docs/development.md “写处理线”：只认 current、没撤回、extraction 是 queued 的包（failed 的也取：重调也没用的
窗口照旧不调，--retry-failed 重调、写账照结果改回来）；
同一个包号还有一行 registered 的（替换做到一半）先跳过。内容从归档读，和包登记的指纹对得上才用。
账本只读，这里什么都不写。

拆开的包（continues 指着上一包）的上一包不在这一批里、已经处理过的，也读进来放在 Batch.context，
L2 拿它当上文：按包一批一批处理和整本一起处理，切出来的窗口一样，L3 存下的结果对得上。

只在引用里看到过的话（D101）：邮件引的旧信里新写的话，整本账本里找不到原话、回的那封信也不在的，
同一个可见范围里最早的那一份引用当证据（L1 标 quoted_only）。这一步跨包，在这里按账本算，算完传给 L1；
算出来的“谁当证据”放在 Batch.quote_holders，写账时（write）记进账本，换了的包放在 Batch.requeue。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple
from pathlib import Path

from javmem.config import Settings
from javmem.ledger import LedgerError, connect_readonly, latest_version, schema_version
from javmem.packet import Rejected, parse_packet
from javmem.pipeline.normalize import (
    L1_VERSION,
    ORIGINAL_KINDS,
    NormalizedPacket,
    QuoteCopy,
    Scope,
    apply_quote_evidence,
    demote_insertions,
    normalize,
    quote_copies,
)
from javmem.raw.index import COPY_MIN_CHARS, copy_form, copy_key
from javmem.raw.sentences import iter_sentences

_USABLE = (
    "state = 'current' AND withdrawn_at IS NULL"
    " AND NOT EXISTS (SELECT 1 FROM packets AS r WHERE r.packet_id = p.packet_id AND r.state = 'registered')"
)
_QUEUED = (
    "SELECT packet_id, revision, archive_path, content_hash, continues FROM packets AS p"
    f" WHERE {_USABLE} AND extraction IN ('queued', 'failed') ORDER BY covers_from_epoch, packet_id"
)
_ANY_STATE = (  # 合并工具重写窗口用（J3.6）：点名的包，写完了的也取
    "SELECT packet_id, revision, archive_path, content_hash, continues FROM packets AS p"
    f" WHERE {_USABLE} ORDER BY covers_from_epoch, packet_id"
)
_PREVIOUS = "SELECT packet_id, revision, archive_path, content_hash, continues FROM packets AS p WHERE {} AND packet_id = ?".format(
    _USABLE
)
_CURRENT = (
    "SELECT packet_id, revision, archive_path, content_hash, continues FROM packets AS p"
    " WHERE state = 'current' AND withdrawn_at IS NULL AND packet_id = ?"
)
_CHUNK = 500  # IN (…) 里一次放几个

Holder = tuple[str, str, int, str | None]  # 当证据的那一份：句子编号、包号、版本、它那封信回的哪一封（In-Reply-To）


@dataclass
class Batch:
    packets: list[NormalizedPacket] = field(default_factory=list)
    problems: list[tuple[str, str]] = field(default_factory=list)  # (包号, 为什么没用上)，不抄原文
    context: list[NormalizedPacket] = field(default_factory=list)  # 拆开的包的上一包，已经处理过的，只当上文
    # D101：这一批碰到的只在引用里看到过的话，现在谁当证据。(copy_key, 可见范围) → 那一份，None 是没有了。写账时记进 quote_holders
    quote_holders: dict[tuple[str, str], Holder | None] = field(default_factory=dict)
    requeue: list[str] = field(default_factory=list)  # 当证据的那一份换了、在这一批以外的包（写完了的写账时改回排队）


def load_queued(
    settings: Settings, library: str, *, packet_ids: Collection[str] | None = None, any_state: bool = False
) -> Batch:
    """取这个库里排着队要抽的包。packet_ids 给了就只取这些。any_state 的话不管排没排队（写完了的也取），要和 packet_ids
    一起用：合并工具从结果库重写几个窗口时（javmem.merging），切出来的窗口和当初写的时候一样。"""
    if any_state and packet_ids is None:
        raise ValueError("any_state 要点名包")
    data_dir = settings.require_data_dir()
    conn = connect_readonly(settings.library(library).db_path)
    try:
        if schema_version(conn) != latest_version():  # 少了 D101 的列和表，算不了引用
            raise LedgerError("账本不是最新的版本：先跑一次 javmem init（或者收件）把账本升上去")
        conn.execute("BEGIN")  # 一次读完，读到一半收件换了版本也不会前后对不上
        try:
            rows = conn.execute(_ANY_STATE if any_state else _QUEUED).fetchall()
            wanted = set(packet_ids) if packet_ids is not None else None
            rows = [row for row in rows if wanted is None or row["packet_id"] in wanted]
            in_batch = {row["packet_id"] for row in rows}
            previous = []
            for packet_id in sorted({row["continues"] for row in rows if row["continues"]} - in_batch):
                found = conn.execute(_PREVIOUS, (packet_id,)).fetchone()
                if found is not None:
                    previous.append(found)
            withdrawn: dict[str, set[str]] = {}
            indexed: dict[str, str] = {}  # 包号 → 原文索引里 (句子编号, 原文) 的指纹
            for row in [*rows, *previous]:
                indexed[row["packet_id"]], withdrawn[row["packet_id"]] = _indexed(conn, row["packet_id"])
        finally:
            conn.execute("ROLLBACK")

        batch = Batch()
        for row in rows:
            loaded = _load(data_dir, row, indexed[row["packet_id"]], withdrawn.get(row["packet_id"], set()))
            if isinstance(loaded, str):
                batch.problems.append((row["packet_id"], loaded))
            else:
                batch.packets.append(loaded)
        for row in previous:  # 上一包有问题就不当上文，窗口少几句上文，不算这一批的问题
            loaded = _load(data_dir, row, indexed[row["packet_id"]], withdrawn.get(row["packet_id"], set()))
            if not isinstance(loaded, str):
                batch.context.append(loaded)
        if wanted is not None:
            found = {packet.packet_id for packet in batch.packets} | {packet_id for packet_id, _ in batch.problems}
            missing = "不在账本里，或者撤回了、换了版本" if any_state else "不在排队的包里"
            batch.problems.extend((packet_id, missing) for packet_id in sorted(wanted - found))
        _withdrawn_insertions(conn, batch)
        _choose_quote_evidence(conn, data_dir, batch)
    finally:
        conn.close()
    return batch


def queued_packets(settings: Settings, library: str) -> list[tuple[str, float, str]]:
    """排着队要抽的包（和 load_queued 取的一样，failed 的也在里面）：(包号, 导出的时间（包头的 covers_to）, extraction)，
    按导出的先后、一样的按包号。只读账本，不读归档（放现有关系时一次只抽一个包，命令行拿它挑、数还有几个排着）。"""
    conn = connect_readonly(settings.library(library).db_path)
    try:
        queued = {str(row["packet_id"]) for row in conn.execute(_QUEUED)}
        rows = conn.execute("SELECT packet_id, covers_to_epoch, extraction FROM packets AS p WHERE " + _USABLE).fetchall()
    finally:
        conn.close()
    found = {
        str(row["packet_id"]): (float(row["covers_to_epoch"] or 0), str(row["extraction"]))
        for row in rows if str(row["packet_id"]) in queued
    }
    return sorted(((packet_id, when, state) for packet_id, (when, state) in found.items()), key=lambda one: (one[1], one[0]))


def _indexed(conn: sqlite3.Connection, packet_id: str) -> tuple[str, set[str]]:
    """原文索引里这个包 (句子编号, 原文) 的指纹，和单条撤回了的句子。"""
    digest = hashlib.sha256()
    withdrawn: set[str] = set()
    for sentence in conn.execute(
        "SELECT sentence_id, text, withdrawn_at FROM raw_sentences WHERE packet_id = ? ORDER BY seq", (packet_id,)
    ):
        digest.update(f"{sentence['sentence_id']}\t{sentence['text']}\n".encode("utf-8"))
        if sentence["withdrawn_at"] is not None:
            withdrawn.add(sentence["sentence_id"])
    return digest.hexdigest(), withdrawn


def _load(data_dir: Path, row: sqlite3.Row, indexed: str, withdrawn: set[str]) -> NormalizedPacket | str:
    """读归档、核对、做 L1。用不上的返回原因。"""
    packet_id = row["packet_id"]
    try:
        raw = (data_dir / row["archive_path"]).read_bytes()
    except OSError:
        return "归档读不了"
    try:
        packet = parse_packet(raw, filename=f"{packet_id}.md")
    except Rejected as rejected:
        return f"归档解析不了：{rejected.code}"
    if packet.content_hash != row["content_hash"] or packet.header.revision != row["revision"]:
        return "归档和包登记对不上"
    # 句子编号和原文要和原文索引一样：分句的规矩改了却没重建索引，证据就对不上原文了
    digest = hashlib.sha256()
    for sentence in iter_sentences(packet):
        digest.update(f"{sentence.sentence_id}\t{sentence.text}\n".encode("utf-8"))
    if digest.hexdigest() != indexed:
        return "原文索引和归档对不上"
    return normalize(packet, withdrawn=withdrawn)


# ---------- 只在引用里看到过的话（D101） ----------


# 原文索引的 copy_role（写账时 L1 认的，第五轮第 50 条）：能当原话、可以当证据的引用、都不是；
# 回信的人插在引用里的话（能当原话，撤回原话时也要标上，第五轮审阅第 1 条）
ROLE_OTHER, ROLE_ORIGINAL, ROLE_COPY, ROLE_INSERTED = 0, 1, 2, 3


def scope_text(scope: Scope) -> str:
    """quote_holders 里记的可见范围。"""
    return json.dumps(scope.as_dict(), ensure_ascii=False, sort_keys=True)


def _scope_from_text(text: str) -> Scope:
    found = json.loads(text)
    return Scope(tuple(found["partitions"]), tuple(found["sensitive"]))


@lru_cache(maxsize=256)
def _scope_of_columns(partitions: str, sensitive: str) -> Scope:
    """包登记的 partitions、sensitive（JSON 列表）换成可见范围，和 Scope.of(包头) 一样。"""
    return Scope(tuple(sorted(set(json.loads(partitions)))), tuple(sorted(set(json.loads(sensitive)))))


class _Where(NamedTuple):
    """一份引用在哪：包号、版本、可见范围。"""

    packet_id: str
    revision: int
    scope: Scope


class _Row(NamedTuple):
    """原文索引里有这一批碰到的话的一句（_sentences_with）。recorded：这个包这一版写过账、是这一版 L1 认的，role 算数。"""

    sentence_id: str
    packet_id: str
    revision: int
    key: str
    role: int | None
    reply_to: str | None
    epoch: int
    section_no: int
    line_no: int
    sentence_no: int
    text: str | None  # 可以当证据的引用才取原文
    message_id: str | None
    scope: Scope
    recorded: bool

    def copy(self) -> QuoteCopy:
        assert self.text is not None
        order = (self.epoch, self.packet_id, self.section_no, self.line_no, self.sentence_no)
        return QuoteCopy(self.sentence_id, self.key, copy_form(self.text), order, self.message_id or "", self.reply_to)


def _sort_in_memory(
    packets: Mapping[str, NormalizedPacket],
    copies: Mapping[str, Mapping[str, QuoteCopy]],
    wanted: Mapping[str, Mapping[str, tuple[int, str]]],
    originals: dict[str, list[Scope]],
    candidates: dict[str, list[tuple[QuoteCopy, _Where]]],
) -> None:
    """读进来了的包（这一批、上一包、读了的别的包）：按刚做的 L1 分原话、可以当证据的引用。wanted 是要看的句子。"""
    for packet_id, needed in wanted.items():
        packet = packets[packet_id]
        where = _Where(packet_id, packet.header.revision, packet.scope)
        for unit in packet.units:
            for line in unit.lines:
                hit = needed.get(line.sentence_id)
                if hit is None or hit[0] != packet.header.revision:
                    continue
                if line.kind in ORIGINAL_KINDS:
                    originals.setdefault(hit[1], []).append(packet.scope)
                elif line.sentence_id in copies.get(packet_id, {}):
                    candidates.setdefault(hit[1], []).append((copies[packet_id][line.sentence_id], where))


def _visible(other: Scope, copy: Scope) -> bool:
    """看得到这份引用的人都看得到 other：分区一样，other 的敏感标记是这份引用的子集（回信从往来串里带上标记，D49）。"""
    return other.partitions == copy.partitions and set(other.sensitive) <= set(copy.sensitive)


def _chunks(items: Iterable[str]) -> Iterable[list[str]]:
    ordered = sorted(set(items))
    for start in range(0, len(ordered), _CHUNK):
        yield ordered[start : start + _CHUNK]


def _marks(count: int) -> str:
    return ",".join("?" * count)


def _choose_quote_evidence(conn: sqlite3.Connection, data_dir: Path, batch: Batch) -> None:
    """只在引用里看到过的话，谁当证据（docs/development.md “L1 规整”，D101）。

    这一批碰到的话：这一批里可以当证据的引用行（normalize.quote_copies）；quote_holders 里记着的、这一批的包里也有的
    （来了原话、更早的引用，或者这一批的包换了版本）；记着的那一份所在的包不是 current、撤回了、换了版本的。每一句
    按账本重新算：账本里有一句原话（ORIGINAL_KINDS，看得到这份引用的人都看得到）一字不差的，谁都不当；回的那封信
    （In-Reply-To）在账本里的，那封信里的引用不算；撤回了的话不算；剩下的按可见范围分开，最早的一份当证据。
    这一批的包标上 quoted_only；和 quote_holders 比，换了的、在这一批以外的包放进 requeue。"""
    loaded = {packet.packet_id: packet for packet in [*batch.packets, *batch.context]}
    in_batch = {packet.packet_id for packet in batch.packets}
    copies = {packet_id: {copy.sentence_id: copy for copy in quote_copies(packet)} for packet_id, packet in loaded.items()}
    mails = {unit.mail.message_id for packet in batch.packets for unit in packet.units if unit.mail is not None}
    conn.execute("BEGIN")
    try:
        touched = {copy.key for packet_id in in_batch for copy in copies[packet_id].values()}
        touched |= _holder_keys(conn, in_batch, mails)
        if not touched:
            return
        originals: dict[str, list[Scope]] = {}
        candidates: dict[str, list[tuple[QuoteCopy, _Where]]] = {}
        in_memory: dict[str, dict[str, tuple[int, str]]] = {}  # 包号 → 句子编号 → (版本, 指纹)：读进来了的包
        unknown: dict[str, dict[str, list[_Row]]] = {}  # 指纹 → 包号 → 句子：没写过账、写的时候 L1 是旧版本的包
        for row in _sentences_with(conn, touched):
            if row.packet_id in loaded:
                in_memory.setdefault(row.packet_id, {})[row.sentence_id] = (row.revision, row.key)
            elif row.recorded:  # 写账时记下了是原话还是引用（第五轮第 50 条）：查表，不读包
                if row.role in (ROLE_ORIGINAL, ROLE_INSERTED):
                    originals.setdefault(row.key, []).append(row.scope)
                elif row.role == ROLE_COPY:
                    candidates.setdefault(row.key, []).append((row.copy(), _Where(row.packet_id, row.revision, row.scope)))
            else:
                unknown.setdefault(row.key, {}).setdefault(row.packet_id, []).append(row)
        _sort_in_memory(loaded, copies, in_memory, originals, candidates)
        stored = _stored_holders(conn, touched)
        # 找到一份原话就停：这句话在每个可见范围里都有看得到的原话，没记过的包不用读；定不下来的才读那几个包
        reading: set[str] = set()
        for key, by_packet in unknown.items():
            scopes = [where.scope for _, where in candidates.get(key, [])]
            scopes += [rows[0].scope for rows in by_packet.values()]
            scopes += [_scope_from_text(scope) for stored_key, scope in stored if stored_key == key]
            if not all(any(_visible(original, scope) for original in originals.get(key, [])) for scope in scopes):
                reading |= set(by_packet)
        others: dict[str, NormalizedPacket] = {}
        for packet_id in sorted(reading):
            packet = _load_other(conn, data_dir, packet_id)
            if packet is None:  # 归档读不了、对不上的包：不知道它是原话还是引用，当原话（宁可不当证据）
                for key, by_packet in unknown.items():
                    for row in by_packet.get(packet_id, []):
                        originals.setdefault(key, []).append(row.scope)
                continue
            others[packet_id] = packet
            copies[packet_id] = {copy.sentence_id: copy for copy in quote_copies(packet)}
            for key, by_packet in unknown.items():
                for row in by_packet.get(packet_id, []):
                    in_memory.setdefault(packet_id, {})[row.sentence_id] = (row.revision, key)
        _sort_in_memory(others, copies, {packet_id: in_memory[packet_id] for packet_id in others if packet_id in in_memory},
                        originals, candidates)
        replied = {copy.in_reply_to for found in candidates.values() for copy, _ in found if copy.in_reply_to}
        parents = _parent_scopes(conn, replied)
        purged = _purged_mails(conn, replied)
        withdrawn = {row[0] for row in conn.execute("SELECT key_hash FROM withdrawn_texts")}
        blocked = _withdrawn_keys(conn, candidates)
        stored = _stored_holders(conn, touched)
        blob = _withdrawn_blob(conn) if candidates else ""
    finally:
        conn.execute("ROLLBACK")

    from javmem.withdraw import withdrawn_text_hash  # 撤回那边引用这边的东西，用到才导入

    chosen: dict[tuple[str, str], Holder] = {}
    for key, found in candidates.items():
        if key in blocked:  # 有一份撤回了：撤回的话不能换一份引用又当证据
            continue
        groups: dict[str, list[tuple[QuoteCopy, _Where]]] = {}
        for copy, where in found:
            if copy.in_reply_to and (
                copy.in_reply_to in purged or any(_visible(scope, where.scope) for scope in parents.get(copy.in_reply_to, []))
            ):
                continue  # 回的那封信在账本里（撤回了、彻底删除了的也算）：引的是它的话，或者它自己也引着的旧话，轮不到这一份
            if withdrawn_text_hash(copy.form) in withdrawn:
                continue
            groups.setdefault(scope_text(where.scope), []).append((copy, where))
        for scope, group in groups.items():
            copy, where = min(group, key=lambda item: item[0].order)
            if any(_visible(original, where.scope) for original in originals.get(key, [])):
                continue
            if blob and copy.form in blob:  # 是一句撤回了的话的一部分（引用时折了行）：这句话哪一份都不当（第四轮审阅第 2 条）
                continue
            chosen[(key, scope)] = (copy.sentence_id, where.packet_id, where.revision, copy.in_reply_to)
    evidence: dict[str, set[str]] = {}
    for sentence_id, packet_id, _, _ in chosen.values():
        evidence.setdefault(packet_id, set()).add(sentence_id)
    batch.packets = [apply_quote_evidence(packet, evidence.get(packet.packet_id, ())) for packet in batch.packets]
    batch.context = [apply_quote_evidence(packet, evidence.get(packet.packet_id, ())) for packet in batch.context]
    requeue: set[str] = set()
    for slot in set(stored) | set(chosen):
        before, after = stored.get(slot), chosen.get(slot)
        if before == after:
            continue
        batch.quote_holders[slot] = after
        for holder in (before, after):
            if holder is not None and holder[1] not in in_batch:
                requeue.add(holder[1])
    batch.requeue = sorted(requeue)


def _withdrawn_insertions(conn: sqlite3.Connection, batch: Batch) -> None:
    """回信的人插在引用里的话（第五轮第 53 条）是从引用里认出来的：撤回了的话不能这样又当成回信的人说的（第五轮审阅
    第 1 条）。和只在引用里看到过的话一样挡：撤回了的话的指纹、同一句话有一份撤回了或者彻底删除了、是一句撤回了的话的
    一部分。对上的改回引用。"""
    lines = [line for packet in [*batch.packets, *batch.context] for unit in packet.units for line in unit.lines if line.inserted]
    if not lines:
        return
    keys = {key for line in lines if (key := copy_key(line.text)) is not None}
    conn.execute("BEGIN")
    try:
        withdrawn = {row[0] for row in conn.execute("SELECT key_hash FROM withdrawn_texts")}
        blocked = _withdrawn_keys(conn, keys)
        blob = _withdrawn_blob(conn)
    finally:
        conn.execute("ROLLBACK")
    from javmem.withdraw import withdrawn_text_hash  # 撤回那边引用这边的东西，用到才导入

    demoted = set()
    for line in lines:
        form = copy_form(line.text)
        if withdrawn_text_hash(form) in withdrawn or copy_key(line.text) in blocked or (
            blob and len(form) >= COPY_MIN_CHARS and form in blob
        ):
            demoted.add(line.sentence_id)
    if demoted:
        batch.packets = [demote_insertions(packet, demoted) for packet in batch.packets]
        batch.context = [demote_insertions(packet, demoted) for packet in batch.context]


def _holder_keys(conn: sqlite3.Connection, in_batch: Collection[str], mails: Collection[str]) -> set[str]:
    """quote_holders 里要重新算的：那一份所在的包不是 current、撤回了、换了版本的；这一批的包里有同一句话的
    （来了原话、更早的引用），或者那一份就在这一批的包里（换了版本，那一句可能没了）；那一份所在的信回的那一封
    在这一批里（mails 是这一批的信的 Message-ID：引用折了行、和原信一字不差对不上，也要换）。"""
    keys = {
        row[0]
        for row in conn.execute(
            "SELECT h.copy_key FROM quote_holders AS h LEFT JOIN packets AS p ON p.packet_id = h.packet_id"
            " AND p.revision = h.revision AND p.state = 'current' AND p.withdrawn_at IS NULL WHERE p.packet_id IS NULL"
        )
    }
    for chunk in _chunks(mails):
        keys |= {row[0] for row in conn.execute(f"SELECT copy_key FROM quote_holders WHERE in_reply_to IN ({_marks(len(chunk))})", chunk)}
    for chunk in _chunks(in_batch):
        keys |= {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT h.copy_key FROM raw_sentences AS s JOIN quote_holders AS h ON h.copy_key = s.copy_key"
                f" WHERE s.packet_id IN ({_marks(len(chunk))})",
                chunk,
            )
        }
        keys |= {row[0] for row in conn.execute(f"SELECT copy_key FROM quote_holders WHERE packet_id IN ({_marks(len(chunk))})", chunk)}
    return keys


def _sentences_with(conn: sqlite3.Connection, keys: Collection[str]) -> list[_Row]:
    """账本里有这些指纹的句子（current、没撤回的包，句子也没撤回），带着写账时记下的是原话还是引用（第五轮第 50 条）、
    当证据要用的那几样（信的时间、第几封第几行、回的哪一封、可见范围）。"""
    found: list[_Row] = []
    for chunk in _chunks(keys):
        for row in conn.execute(
            "SELECT s.sentence_id, s.packet_id, s.revision, s.copy_key, s.copy_role, s.reply_to, s.said_at_epoch,"
            " s.section_no, s.line_no, s.sentence_no, CASE WHEN s.copy_role = ? THEN s.text END, s.message_id,"
            " p.partitions, p.sensitive, (r.revision IS NOT NULL AND r.revision = s.revision AND r.l1_version = ?)"
            " FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            " LEFT JOIN quote_roles AS r ON r.packet_id = s.packet_id"
            f" WHERE s.copy_key IN ({_marks(len(chunk))}) AND s.withdrawn_at IS NULL"
            " AND p.state = 'current' AND p.withdrawn_at IS NULL",
            (ROLE_COPY, L1_VERSION, *chunk),
        ):
            found.append(
                _Row(
                    row[0], row[1], int(row[2]), row[3], row[4], row[5], int(row[6]), int(row[7] or 0), int(row[8] or 0),
                    int(row[9] or 0), row[10], row[11], _scope_of_columns(row[12], row[13]), bool(row[14]),
                )
            )
    return found


def _withdrawn_keys(conn: sqlite3.Connection, keys: Collection[str]) -> set[str]:
    """撤回过的话：账本里有一份这句话撤回了（单条撤回，或者它的包撤回了），或者彻底删除了（purged_copies，第四轮审阅
    第 3 条）。> 引用行撤回时不记 withdrawn_texts（引用行只是旧话重说），按原文索引里撤回了的句子认。同一句话别的引用
    也不当证据，宁可少抽。"""
    found: set[str] = set()
    for chunk in _chunks(keys):
        found |= {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT s.copy_key FROM raw_sentences AS s JOIN packets AS p"
                " ON p.packet_id = s.packet_id AND p.revision = s.revision"
                f" WHERE s.copy_key IN ({_marks(len(chunk))}) AND (s.withdrawn_at IS NOT NULL OR p.withdrawn_at IS NOT NULL)",
                chunk,
            )
        }
        found |= {row[0] for row in conn.execute(f"SELECT copy_key FROM purged_copies WHERE copy_key IN ({_marks(len(chunk))})", chunk)}
    return found


def _withdrawn_blob(conn: sqlite3.Connection) -> str:
    """撤回了的话（单条撤回的句子、撤回了的包里的句子），按比引用用的写法，用换行连起来：当证据的那一份是其中一句的
    一部分（引用时折了行）就不当（第四轮审阅第 2 条）。没有撤回过的就是空的。彻底删除了的原文没了，比不了。"""
    if conn.execute("SELECT EXISTS (SELECT 1 FROM withdrawn_packets) OR EXISTS (SELECT 1 FROM withdrawn_sentences)").fetchone()[0] == 0:
        return ""
    rows = conn.execute(
        "SELECT s.text FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
        " WHERE s.withdrawn_at IS NOT NULL OR p.withdrawn_at IS NOT NULL"
    )
    return "\n".join(form for (text,) in rows if len(form := copy_form(text)) >= COPY_MIN_CHARS)


def _load_other(conn: sqlite3.Connection, data_dir: Path, packet_id: str) -> NormalizedPacket | None:
    """这一批以外、有同一句话的包，做一遍 L1 看那一句是原话还是引用。用不上的是 None。"""
    row = conn.execute(_CURRENT, (packet_id,)).fetchone()
    if row is None:
        return None
    digest, withdrawn = _indexed(conn, packet_id)
    loaded = _load(data_dir, row, digest, withdrawn)
    return None if isinstance(loaded, str) else loaded


def _parent_scopes(conn: sqlite3.Connection, message_ids: Collection[str]) -> dict[str, list[Scope]]:
    """这些 Message-ID 的信在账本里的（current 的包，撤回了的也算：第四轮审阅第 2 条，回撤回了的信、折了行的引用
    和原话对不上）：Message-ID → 它所在的包的可见范围。"""
    found: dict[str, list[Scope]] = {}
    for chunk in _chunks(message_ids):
        for row in conn.execute(
            "SELECT DISTINCT s.message_id, p.partitions, p.sensitive FROM raw_sentences AS s JOIN packets AS p"
            " ON p.packet_id = s.packet_id AND p.revision = s.revision"
            f" WHERE s.message_id IN ({_marks(len(chunk))}) AND p.state = 'current'",
            chunk,
        ):
            found.setdefault(row[0], []).append(
                Scope(tuple(sorted(set(json.loads(row[1])))), tuple(sorted(set(json.loads(row[2])))))
            )
    return found


def _purged_mails(conn: sqlite3.Connection, message_ids: Collection[str]) -> set[str]:
    """这些 Message-ID 里彻底删除了的信（purged_mails）：原文没了，回它的信里的引用照旧不当证据。"""
    found: set[str] = set()
    for chunk in _chunks(message_ids):
        found |= {row[0] for row in conn.execute(f"SELECT message_id FROM purged_mails WHERE message_id IN ({_marks(len(chunk))})", chunk)}
    return found


def _stored_holders(conn: sqlite3.Connection, keys: Collection[str]) -> dict[tuple[str, str], Holder]:
    stored: dict[tuple[str, str], Holder] = {}
    for chunk in _chunks(keys):
        for row in conn.execute(
            "SELECT copy_key, scope, sentence_id, packet_id, revision, in_reply_to FROM quote_holders"
            f" WHERE copy_key IN ({_marks(len(chunk))})",
            chunk,
        ):
            stored[(row[0], row[1])] = (row[2], row[3], int(row[4]), row[5])
    return stored


def explain(error: sqlite3.Error) -> str:
    return f"账本读不了：{type(error).__name__}"
