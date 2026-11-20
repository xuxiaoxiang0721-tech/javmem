"""撤回（J2.11）：按包或单条撤回，彻底删除，解除撤回，把撤回的记录导出、导进新账本。都是管理命令，只给主人和 CC，
不是查询工具。

撤回默认只标记原文：包登记的 withdrawn_at、原文索引的 withdrawn_at，归档不动。查询（raw_search，以后的五个工具）
都跳过撤回了的包和句子。派生的东西删掉：

- 账本：证据来自它的说法删掉，事实按剩下的说法重算，没有说法了的删掉（向量跟着删）；别名的出处、隔离、
  没用了的实体、只因为它挂着的“实体有歧义”。其他来源也支持的事实留着，证据只剩别的；窗口里少了内容的包改回 queued，
  用剩下的原文重抽、重写（packet-spec 第 10 节：其他来源也支持的用剩余原文重建）。标准名只在撤回的内容里出现过的实体，
  用到它的说法记成没写过，下次写账重新对齐（不调模型）。
- 冻结的事实（说法没了也留着）上指着撤回内容的证据删掉，一条证据都不剩的先藏起来、进待确认，等主人定；
  后来又有别的包说了，自动放回来。
- L3 结果库：窗口里有撤回内容的那些窗口，以及只被它们用到的结果（原文和模型的回复都在里面）。
- 向量缓存：账本里没有事实、说法再用的文字。

单条撤回点一条事实（query-api 3.2：这条事实的证据原句都标已撤回；别的事实也靠这些原句的，去掉这条证据，没有证据了
就一起撤回），也可以直接点一句原文。点到的句子之外，这些也一起标：同一个包里同一个人一字不差的话（原文层原来的规矩；
邮件包只算同一封信里的，D73 第 17 条）、同一个包里 Bot 对它的复述和引用它的行（L1 的规矩），以及别的包里引用它的行
（引用行只是旧话重说；别的包里的只认引的就是这一句、或者是截短了的这一句，8 个字以上）。撤回以后才到的包、重发的新版本，
收件时也这样标（mark_on_arrival）。邮件的主题、发件人那一句跟着这封信的每一句走（查询结果、发给模型的抬头里都有）：
点名它们就连同整封信一起撤回；按事实撤回时只撤证据那几句（D73 第 16 条），这封信别的句子留着，查询结果、窗口、
账本里的主题、说话人写成“[已撤回]”（报告的 masked）。
别的包里一字不差、却不是引用的（转发、自己又说了一遍、回信主题里的“Re: 原主题”），不动，列在报告的 copies_elsewhere 里；
同一个包里同一个人别的信里一字不差的也是。

彻底删除（purge）是单独的命令：删归档文件（各个版本、跳过的副本）和原文索引里的句子，整理库文件，不留空页。
解除撤回（undo）只按包：包重新排队，要重抽；撤回它时别的包里跟着标上的引用行也解除。彻底删除过的回不来。

撤回的记录（撤回过的包号、单条撤回的指纹、撤回了的话的指纹）只在账本里。撤回了的话的指纹（withdrawn_texts）是给
别的包里的引用行比的：原文彻底删除了、搬到新账本了，一字不差引用它的行照样认得出。整本重建时先从旧账本导出
（--export），新账本收包之前导进去（--import），不然撤回过的东西会跟着原包回来，下次抽取又发上云。导进来以后
中途断了，不写目标再跑一次 javmem withdraw 就接着撤回。

做之前都先拿收件锁、写账的锁和 L3 结果库的锁（结果库还没有也先建出来拿它的锁），再对账一遍：不然停在 registered 的
新版本会在撤回以后转正，正在跑的写账、抽取会把撤回的内容又写回来。中途断了（账本改完了、结果库没清完），不写目标
再跑一次就接着清。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from javmem.config import ConfigError, Library, Settings
from javmem.intake import RECOVERED, intake_lock, reconcile_library
from javmem.ledger import connect_readonly, open_ledger, transaction
from javmem.packet import Packet, Rejected, parse_packet
from javmem.pipeline import correct, prer
from javmem.pipeline.normalize import WITHDRAWN, normal_text, normalize, quote_body
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VECTOR_FILE, VectorCache
from javmem.pipeline.write import LedgerWriter, WriteBusy, WriteLock
from javmem.raw import covered, mark_withdrawn, sentence_hash
from javmem.raw.sentences import MAIL_WIDE_FIELDS
from javmem.samecheck import forget_judgments
from javmem.timeutil import iso

MIN_COPY_CHARS = 8  # 别的包里的引用、一字不差的句子，这么长以上的才认、才报：太短的话（“好的，收到。”）谁都说过
RECORDS_SCHEMA = "javmem-withdrawals/1"
ON_SPOT_REASON = f"L5:{correct.ON_SPOT}"  # 隔离表里 R05 从关系证据里去掉的 Bot 那句（J4.6，见 _left_out）
_TRAILING = " \u3000。．.!！?？;；"  # 比引用时去掉的结尾标点


class WithdrawError(Exception):
    """点名的包、事实、句子不在库里，或者不能这样做。什么都没动。"""


@dataclass
class WithdrawReport:
    """只有编号和数，不抄原文。"""

    library: str
    packets: list[str] = field(default_factory=list)  # 这次撤回的包
    facts: list[str] = field(default_factory=list)  # 点名撤回、删掉的事实
    sentences: list[str] = field(default_factory=list)  # 这次标上撤回的原文句（连同一起标的）
    mails: list[str] = field(default_factory=list)  # 点到主题、发件人，连同整封信一起撤回的：包号#第几封
    # 按事实撤回了主题、发件人那一句、信里别的句子还在的：包号#第几封。主题、说话人写成“[已撤回]”（D73 第 16 条）
    masked: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)  # 点名的里面早就撤回了的
    recovered: int = 0  # 对账时接着做完的包
    facts_before: int = 0
    facts_after: int = 0
    claims_before: int = 0
    claims_after: int = 0
    hidden: list[str] = field(default_factory=list)  # 证据都撤回了的冻结事实：先藏起来，进了待确认
    requeued: list[str] = field(default_factory=list)  # 窗口少了内容、要重新对齐，改回 queued 的包
    store_windows: int = 0  # L3 结果库删了几个窗口、几个结果
    store_results: int = 0
    store: bool = False  # 有没有清 L3 结果库
    # 要重抽的窗口（J5.6）：整包撤回带出来的窗口里别的包写的说法先删了几处（下一次 extract、write 照剩下的原文重抽回来），
    # 这些窗口和单条撤回带出来的窗口的隔离行删了几行
    cleared_claims: int = 0
    cleared_quarantine: int = 0
    vectors_deleted: int = 0
    soft_deleted: int = 0  # 软变化判定的 soft.db 里带着撤回句子的判定删了几条（U3）
    copies_elsewhere: list[str] = field(default_factory=list)  # 别的包里一字不差、或者带着它的句子，没动
    problems: list[str] = field(default_factory=list)  # 没做成的小事：比如归档读不了，没找复述

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class PurgeReport:
    library: str
    packet_id: str
    revisions: list[int] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # 删掉的归档文件，相对数据目录
    sentences: int = 0  # 原文索引里删掉的句子
    hidden: list[str] = field(default_factory=list)
    store_windows: int = 0
    store_results: int = 0
    vectors_deleted: int = 0
    soft_deleted: int = 0  # 软变化判定的 soft.db 里带着这个包的句子的判定删了几条（U3）
    already: bool = False  # 早就彻底删除过了
    requeued: list[str] = field(default_factory=list)  # 标注里用了删掉的版本里学来的名字的别的包，改回 queued（预处理 B）
    incomplete: list[str] = field(default_factory=list)  # 没整理完的库（有查询正读着），删掉的内容可能还在空页、日志里

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class UndoReport:
    packets: list[str] = field(default_factory=list)  # 解除了的包
    resend: list[str] = field(default_factory=list)  # 其中这本账里没有包登记的（整本重建时导进来的）：要再投一次原包


@dataclass
class _Plan:
    packets: list[tuple[str, int]] = field(default_factory=list)  # (包号, 现行版本)
    sentences: list[str] = field(default_factory=list)  # 点名的原文句
    facts: list[str] = field(default_factory=list)
    fact_sentences: list[str] = field(default_factory=list)  # 点名的事实在现行版本里的证据句
    reassert: list[str] = field(default_factory=list)  # 点名的、早就撤回了的句子：记录改成“点名撤回的”
    recorded: list[str] = field(default_factory=list)  # 撤回记录里早就有、原文还没标的句子（导进来的）：只标原文


# ---------- 锁和目录 ----------


def default_dirs(settings: Settings, store_dir: Path | None, vectors_dir: Path | None) -> tuple[Path, Path]:
    data_dir = settings.require_data_dir()
    return (store_dir or data_dir / "l3", vectors_dir or data_dir / "vectors")


@contextmanager
def _locked(settings: Settings, store_dir: Path) -> Iterator[ResultStore]:
    """收件锁、写账的锁、L3 结果库的锁都拿到才动，拿不到抛 IntakeBusy、WriteBusy、StoreBusy。结果库还没有也先建出来、
    拿它的锁：不然撤回的同时开始的抽取会建库，用撤回以前切好的窗口调模型。"""
    data_dir = settings.require_data_dir()
    with intake_lock(data_dir), WriteLock(data_dir), ResultStore(store_dir, exclusive=True) as store:
        yield store


# ---------- 撤回 ----------


def withdraw(
    settings: Settings,
    library_name: str,
    *,
    packets: Sequence[str] = (),
    facts: Sequence[str] = (),
    sentences: Sequence[str] = (),
    store_dir: Path | None = None,
    vectors_dir: Path | None = None,
    records: Mapping[str, object] | None = None,
) -> WithdrawReport:
    """撤回点名的包、事实、原文句。点名的有一个不在库里，抛 WithdrawError，什么都不动。

    一个都没点名（都是空的）：只清一遍（结果库、向量缓存、跟着标的引用），撤回中途断了这样接着清。
    records 是 export_records 导出的撤回记录：先记进账本，再撤回这个库里已经有的那些包和句子。
    """
    library = settings.library(library_name)
    store_dir, vectors_dir = default_dirs(settings, store_dir, vectors_dir)
    report = WithdrawReport(library_name)
    with _locked(settings, store_dir) as store:
        conn = open_ledger(library)
        try:
            report.recovered = reconcile_library(settings, library, conn).count(RECOVERED)
            now = iso(settings.now())
            if records is not None:
                _import_records(conn, records, now, library_name)
            # 撤回记录里有、这本账里还没撤回的（刚导进来的，或者导进来以后中途断了的）：接着撤回
            pending, recorded = _pending_records(conn)
            plan = _resolve(conn, [*packets, *pending], facts, sentences, report)
            plan.recorded = [sentence_id for sentence_id in recorded if sentence_id not in plan.sentences]
            report.facts_before, report.claims_before = _count(conn, "facts"), _count(conn, "fact_claims")
            with transaction(conn):
                _apply(conn, settings, plan, now, report)
            _forget_in_store(conn, store, report, now)
            _clean_soft(conn, settings, report)
            report.facts_after, report.claims_after = _count(conn, "facts"), _count(conn, "fact_claims")
        finally:
            conn.close()
        report.vectors_deleted = _clean_vectors(settings, vectors_dir)
    return report


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _resolve(
    conn: sqlite3.Connection, packets: Sequence[str], facts: Sequence[str], sentences: Sequence[str], report: WithdrawReport
) -> _Plan:
    plan = _Plan()
    unknown: list[str] = []
    for packet_id in dict.fromkeys(packets):
        row = conn.execute(
            "SELECT revision, withdrawn_at FROM packets WHERE packet_id = ? AND state = 'current'", (packet_id,)
        ).fetchone()
        if row is None:
            withdrawn = conn.execute(
                "SELECT 1 FROM packets WHERE packet_id = ? AND withdrawn_at IS NOT NULL"
                " UNION ALL SELECT 1 FROM withdrawn_packets WHERE packet_id = ? LIMIT 1",
                (packet_id, packet_id),
            ).fetchone()
            (report.already if withdrawn else unknown).append(packet_id)
        elif row["withdrawn_at"] is not None:
            report.already.append(packet_id)
        else:
            plan.packets.append((packet_id, int(row["revision"])))
    for sentence_id in dict.fromkeys(sentences):
        row = _raw_row(conn, sentence_id)
        if row is None:
            unknown.append(sentence_id)
        elif row["withdrawn_at"] is not None or row["packet_withdrawn"] is not None:
            report.already.append(sentence_id)
            # 以前因为撤回别的包才跟着标上的，或者它的包撤回了：主人点名了，解除那个包时不跟着放回来
            plan.reassert.append(sentence_id)
        else:
            plan.sentences.append(sentence_id)
    for fact_id in dict.fromkeys(facts):
        # 点名的也可以是一条关系（R 开头，J4.4）：照事实一样撤回它的证据句，再删掉它
        relation = conn.execute("SELECT 1 FROM relations WHERE relation_id = ?", (fact_id,)).fetchone() is not None
        if not relation and conn.execute("SELECT 1 FROM facts WHERE fact_id = ?", (fact_id,)).fetchone() is None:
            unknown.append(fact_id)
            continue
        plan.facts.append(fact_id)
        found = [dict(row) for row in conn.execute(
            "SELECT sentence_id, packet_id, revision FROM relation_evidence WHERE relation_id = ? ORDER BY evidence_id"
            if relation else "SELECT sentence_id, packet_id, revision FROM evidence WHERE fact_id = ? ORDER BY evidence_id",
            (fact_id,),
        )]
        if relation:
            found += _left_out(conn, fact_id)
        for evidence in found:
            row = _raw_row(conn, evidence["sentence_id"])
            if (
                row is not None
                and (row["packet_id"], row["revision"]) == (evidence["packet_id"], evidence["revision"])
                and row["withdrawn_at"] is None
                and row["packet_withdrawn"] is None
            ):
                plan.fact_sentences.append(evidence["sentence_id"])
    if unknown:
        raise WithdrawError(
            f"库里没有这些：{'、'.join(unknown)}（撤回中途断了的话，不写目标再跑一次 javmem withdraw 就接着清）"
        )
    return plan


def _left_out(conn: sqlite3.Connection, relation_id: str) -> list[dict[str, object]]:
    """这条关系的说法里 R05 从证据里去掉的 Bot 那几句（J4.6：L3 把 Bot 当场说错的那句和主人的纠正给成同一处，Bot 那句
    记在隔离表、payload 里有 claim_id）。按关系撤回时它们也算它的证据句：不然纠正那句撤了、Bot 那句留着，重抽时照 Bot
    那句又记回来。"""
    out: list[dict[str, object]] = []
    for (claim_id,) in conn.execute("SELECT claim_id FROM relation_claims WHERE relation_id = ? ORDER BY claim_id", (relation_id,)):
        for (payload,) in conn.execute(
            "SELECT payload FROM quarantine WHERE item_kind = 'relation_evidence' AND reason = ? AND instr(payload, ?) > 0"
            " ORDER BY quarantine_id",
            (ON_SPOT_REASON, json.dumps(claim_id, ensure_ascii=False)),
        ):
            data = json.loads(payload)
            evidence = data.get("evidence")
            if data.get("claim_id") == claim_id and isinstance(evidence, dict):
                out.append({name: evidence.get(name) for name in ("sentence_id", "packet_id", "revision")})
    return out


def _raw_row(conn: sqlite3.Connection, sentence_id: str) -> sqlite3.Row | None:
    """原文索引里的一句，连同它的包撤回了没有。原文索引里只有现行版本的句子。"""
    return conn.execute(
        "SELECT s.sentence_id, s.packet_id, s.revision, s.section_no, s.field, s.text, s.message_id, s.withdrawn_at,"
        " p.withdrawn_at AS packet_withdrawn"
        " FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
        " WHERE s.sentence_id = ?",
        (sentence_id,),
    ).fetchone()


def _apply(conn: sqlite3.Connection, settings: Settings, plan: _Plan, now: str, report: WithdrawReport) -> None:
    """一个事务：标记、清账本里派生的东西、改回排队。"""
    writer = LedgerWriter(conn, extracted_by="", embedding_model="", register=False)
    requeue: set[str] = set()
    changed: set[str] = set()  # 内容少了的包：拿它当上文的后一包也要重抽

    # 和撤回的包在同一个窗口里写过说法的包（邮件往来串一起写过）：窗口少了内容，要重抽。没有 L3 结果库也认得出
    requeue |= _window_mates(conn, packets={packet_id for packet_id, _ in plan.packets})
    # 接着它的后一包的第一个窗口照账本也认一遍（J5.6 复审 N1）：结果库里已经没有的（先单条撤回删过了、结果库删过），
    # _forget_in_store 认不出来
    mates = _ledger_windows(conn, {packet_id for packet_id, _ in plan.packets})
    for packet_id, revision in plan.packets:
        conn.execute("UPDATE packets SET withdrawn_at = ? WHERE packet_id = ? AND withdrawn_at IS NULL", (now, packet_id))
        conn.execute("INSERT OR IGNORE INTO withdrawn_packets (packet_id, withdrawn_at) VALUES (?, ?)", (packet_id, now))
        # 这个包的警告不用再提醒了；撤回以后又来的（withdrawn_resent）是以后的事
        conn.execute("UPDATE patrol_warnings SET resolved_at = ? WHERE packet_id = ? AND resolved_at IS NULL", (now, packet_id))
        writer.remove_revision(packet_id, revision)
        report.packets.append(packet_id)
        changed.add(packet_id)
    if mates:
        cleared, quarantined = writer.forget_windows(mates, claims=set(mates))
        report.cleared_claims += cleared
        report.cleared_quarantine += quarantined
    # 挂在别的包下面、payload 里写着这些包的句子编号的隔离行也删（判断 C 的判的经过记着发过去的每一行，L3 原样里有
    # 证据的编号）：结果库里没有那个窗口时的兜底，有窗口的 _forget_in_store 按窗口删（J5.6，判断 C 报告第 4 节第 8 条）。
    # 一批包合成一条，隔离表只扫一遍（--import 几十个包时不扫几十遍）
    for start in range(0, len(plan.packets), 50):
        part = [f'"{packet_id}#' for packet_id, _ in plan.packets[start:start + 50]]
        cursor = conn.execute(
            "DELETE FROM quarantine WHERE " + " OR ".join("instr(payload, ?) > 0" for _ in part), part
        )
        report.cleared_quarantine += max(cursor.rowcount, 0)

    # 点名的主题、发件人那一句：整封信一起撤回（D73 第 11 条）。早就撤回了的、主人又点名的也走一遍：记录的 cause 改成空
    # （点名撤回的）；是主题、发件人的，整封信照样一起撤回（以前的版本里同一个发件人别的信的邮件头会跟着标上，
    # 点它就是要撤那封信）。按事实撤回只撤证据那几句，不整封撤（D73 第 16 条）：证据里有主题、发件人那一句的，
    # 这封信别的句子留着，查询结果、窗口、账本里写成“[已撤回]”（raw_search、L1、mask_senders）
    base, report.mails = _whole_mails(conn, list(dict.fromkeys([*plan.sentences, *plan.reassert])))
    base = list(dict.fromkeys([*base, *plan.fact_sentences]))
    marked: list[str] = []
    for sentence_id in base:
        marked += mark_withdrawn(conn, sentence_id, now)
    for sentence_id in plan.recorded:  # 记录（连同它的 cause）早就有了：只标原文
        cursor = conn.execute(
            "UPDATE raw_sentences SET withdrawn_at = ? WHERE sentence_id = ? AND withdrawn_at IS NULL", (now, sentence_id)
        )
        if cursor.rowcount:
            marked.append(sentence_id)
    if marked:
        marked += _mark_followers(conn, settings, {sentence_id.split("#", 1)[0] for sentence_id in marked}, now, report)
    marked += _mark_quotes(conn, now)
    report.sentences = sorted(set(marked))
    rows = [_raw_row(conn, sentence_id) for sentence_id in report.sentences]
    proofs = [(row["sentence_id"], row["packet_id"], int(row["revision"])) for row in rows if row is not None]
    requeue |= _window_mates(conn, sentences=set(report.sentences))
    requeue |= writer.remove_sentences(proofs)
    masked = _masked_mails([row for row in rows if row is not None], report.mails)
    report.masked = sorted({f"{packet_id}#{section_no}" for packet_id, _, _, section_no, _ in masked})
    senders = {(packet_id, revision, mail) for packet_id, revision, mail, _, field in masked if field == "from"}
    requeue |= writer.mask_senders(senders)
    # 结果库里这封信的窗口（抬头写着发件人）要删：记下来，_forget_in_store 删完了才勾掉，中途断了再跑接着删
    conn.executemany(
        "INSERT OR IGNORE INTO masked_mails (packet_id, revision, message_id, masked_at) VALUES (?, ?, ?, ?)",
        [(packet_id, revision, mail, now) for packet_id, revision, mail in sorted(senders)],
    )
    changed |= {packet_id for _, packet_id, _ in proofs}
    requeue |= changed

    for fact_id in plan.facts:  # 点名的那条：证据句都撤回了，说法没了已经删了；冻结的也删，是主人点名的
        writer.delete_fact(fact_id)
    report.facts = [
        fact_id for fact_id in plan.facts
        if conn.execute("SELECT 1 FROM facts WHERE fact_id = ?1 UNION ALL SELECT 1 FROM relations WHERE relation_id = ?1",
                        (fact_id,)).fetchone() is None
    ]
    report.hidden = writer.hide_withdrawn(now)
    writer.drop_dead_ambiguities()
    requeue |= writer.realign_after_withdrawal()
    if changed:
        requeue |= {
            row[0]
            for row in conn.execute(
                f"SELECT packet_id FROM packets WHERE state = 'current' AND continues IN ({_marks(changed)})", sorted(changed)
            )
        }
    report.requeued = _requeue(conn, requeue, now)
    report.copies_elsewhere = _copies_elsewhere(conn, base)
    forget_judgments(conn)  # 合并卡片上模型的看法，送过撤回了的句子的删掉（J3.6）


def _marks(items: Collection[object]) -> str:
    return ", ".join("?" for _ in items)


def _ledger_windows(conn: sqlite3.Connection, packets: Collection[str]) -> dict[str, set[str]]:
    """整包撤回这些包时，不靠结果库也认得出的要重抽的窗口（J5.6 复审 N1）：接着它们的后一包的第一个窗口（L2 拿上一包的
    末尾当上文的就是第一个），窗口编号 → 正文的包（就是后一包自己）。结果库里有这个窗口的，_forget_in_store 照结果库
    也会清一遍，清两遍不要紧。往来串一起写的、标注用了学来名字的、引用它原话的窗口，只认得出结果库里还有的。"""
    if not packets:
        return {}
    return {
        f"{packet_id}#w1": {packet_id}
        for (packet_id,) in conn.execute(
            f"SELECT packet_id FROM packets WHERE state = 'current' AND withdrawn_at IS NULL AND continues IN ({_marks(packets)})",
            sorted(packets),
        )
    }


def _window_mates(conn: sqlite3.Connection, *, packets: Collection[str] = (), sentences: Collection[str] = ()) -> set[str]:
    """证据里有这些包、这些句子的说法是在哪几个窗口写的，这几个窗口里别的说法的证据都在哪些包。
    要在删说法之前算。"""
    if not packets and not sentences:
        return set()
    claims = [(row["window_id"], json.loads(row["proofs"])) for row in conn.execute("SELECT window_id, proofs FROM fact_claims")]
    windows = {
        window_id
        for window_id, proofs in claims
        if any(proof["packet_id"] in packets or proof["sentence_id"] in sentences for proof in proofs)
    }
    return {str(proof["packet_id"]) for window_id, proofs in claims if window_id in windows for proof in proofs}


def _masked_mails(rows: Iterable[sqlite3.Row], whole: Collection[str]) -> set[tuple[str, int, str, int, str]]:
    """这次撤回了主题、发件人那一句、又没有整封撤回的信：(包号, 版本, message_id, 第几封, 哪个字段)。
    查询结果、窗口里这封信的主题、说话人写成“[已撤回]”，账本里的说话人跟着换（D73 第 16 条）。"""
    return {
        (row["packet_id"], int(row["revision"]), row["message_id"], int(row["section_no"]), row["field"])
        for row in rows
        if row["field"] in MAIL_WIDE_FIELDS
        and row["message_id"] is not None
        and f"{row['packet_id']}#{row['section_no']}" not in whole
    }


def _whole_mails(conn: sqlite3.Connection, sentence_ids: list[str]) -> tuple[list[str], list[str]]:
    """邮件的主题、发件人那一句跟着整封信走（查询结果的 email.subject、speaker，发给模型的“发件人”抬头），
    点到它们就连同这封信的每一句一起撤回。返回 (要标的句子, 整封撤回的信：包号#第几封)。"""
    expanded: list[str] = []
    mails: list[str] = []
    for sentence_id in sentence_ids:
        expanded.append(sentence_id)
        row = _raw_row(conn, sentence_id)
        if row is None or row["field"] not in MAIL_WIDE_FIELDS:
            continue
        mail = f"{row['packet_id']}#{row['section_no']}"
        if mail in mails:
            continue
        mails.append(mail)
        expanded += [
            found["sentence_id"]
            for found in conn.execute(
                "SELECT sentence_id FROM raw_sentences WHERE packet_id = ? AND revision = ? AND section_kind = 'raw'"
                " AND section_no = ? ORDER BY seq",
                (row["packet_id"], row["revision"], row["section_no"]),
            )
        ]
    return list(dict.fromkeys(expanded)), mails


def _followers_in(conn: sqlite3.Connection, packet: Packet, now: str) -> list[str]:
    """一个包里，撤回了的句子的复述、引用它的行（L1 认的那些）也标上撤回，一直标到没有新的。"""
    packet_id = packet.header.packet_id
    marked: list[str] = []
    for _ in range(5):  # 复述的复述、引用复述的行；几轮就到头了
        withdrawn = {
            found["sentence_id"]
            for found in conn.execute(
                "SELECT sentence_id FROM raw_sentences WHERE packet_id = ? AND withdrawn_at IS NOT NULL", (packet_id,)
            )
        }
        if not withdrawn:
            break
        more = [
            line.sentence_id
            for unit in normalize(packet, withdrawn=withdrawn).units
            for line in unit.lines
            if line.kind == WITHDRAWN and line.sentence_id not in withdrawn
        ]
        if not more:
            break
        for sentence_id in more:
            marked += mark_withdrawn(conn, sentence_id, now)
    return marked


def _mark_followers(
    conn: sqlite3.Connection, settings: Settings, packet_ids: Iterable[str], now: str, report: WithdrawReport
) -> list[str]:
    data_dir = settings.require_data_dir()
    marked: list[str] = []
    for packet_id in sorted(packet_ids):
        row = conn.execute(
            "SELECT archive_path FROM packets WHERE packet_id = ? AND state = 'current'", (packet_id,)
        ).fetchone()
        if row is None:
            continue
        try:
            packet = parse_packet((data_dir / row["archive_path"]).read_bytes(), filename=f"{packet_id}.md")
        except (OSError, Rejected):
            report.problems.append(f"{packet_id}：归档读不了，没找复述和引用")
            continue
        marked += _followers_in(conn, packet, now)
    return marked


def _quote_key(text: str) -> str:
    """比引用用的写法：压空白，去掉结尾的句末标点（“…ship on Nov 15.”和引用里的“…ship on Nov 15”是一句）。"""
    return normal_text(text).rstrip(_TRAILING)


def _key_hash(key: str) -> str:
    return hashlib.sha256(f"javmem-withdrawn-text\n{key}".encode("utf-8")).hexdigest()


def withdrawn_text_hash(key: str) -> str:
    """撤回了的话的指纹（withdrawn_texts.key_hash），key 是比引用用的写法。取包时拿它挡住撤回了的话：只在引用里
    看到过的话不能是撤回了的（D101）。"""
    return _key_hash(key)


def _remember_texts(conn: sqlite3.Connection, texts: Mapping[str, str | None], now: str) -> None:
    """撤回了的话记下指纹（withdrawn_texts），原文彻底删除了、搬到新账本了也认得出引用它的行。点名撤回的盖过跟着标的。"""
    conn.executemany(
        "INSERT INTO withdrawn_texts (key_hash, cause, withdrawn_at) VALUES (?, ?, ?)"
        " ON CONFLICT (key_hash) DO UPDATE SET cause = CASE WHEN excluded.cause IS NULL THEN NULL ELSE cause END",
        [(_key_hash(key), cause, now) for key, cause in texts.items()],
    )


# 撤回了的句子从两处找：单条撤回了的、撤回了的包里的（都不算引用行）。分两段，各走迁移 0018 的小索引：收件每收一个包
# 都要找一遍（mark_on_arrival），原来一条 OR 用不上索引，整张原文索引从头扫，库越大越慢（邮件导入报告第 22 条）。
# 第二段写 CROSS JOIN，先找撤回了的包、再按包号取句子，不然 SQLite 又会整张扫原文索引。第二段只要没单条撤回的，两段不重。
_WITHDRAWN_ROWS = (
    "SELECT s.id, s.packet_id, s.text, s.withdrawn_at, p.withdrawn_at AS packet_withdrawn FROM raw_sentences AS s"
    " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
    " WHERE s.withdrawn_at IS NOT NULL AND s.quoted = 0"
    " UNION ALL"
    " SELECT s.id, s.packet_id, s.text, s.withdrawn_at, p.withdrawn_at FROM packets AS p"
    " CROSS JOIN raw_sentences AS s ON s.packet_id = p.packet_id AND s.revision = p.revision"
    " WHERE p.withdrawn_at IS NOT NULL AND s.withdrawn_at IS NULL AND s.quoted = 0"
)


def _withdrawn_texts(conn: sqlite3.Connection) -> dict[str, str | None]:
    """撤回了的话（不算引用行），按比引用用的写法：写法 → 是因为撤回哪个包才算撤回的（单条撤回的是 None，
    两样都有的算单条）。按原文索引的先后（id）排，和以前整张扫的时候一样（以前的先后是 SQLite 没有统计表时给的，
    javmem 不建统计表；现在自己排，有了统计表也不变）：同一句话记哪个包、截短了的引用对上哪一句，都看先后。"""
    texts: dict[str, str | None] = {}
    for row in sorted(conn.execute(_WITHDRAWN_ROWS), key=lambda found: found["id"]):
        text = _quote_key(row["text"])
        if len(text) < MIN_COPY_CHARS:
            continue
        cause = row["packet_id"] if row["packet_withdrawn"] is not None and row["withdrawn_at"] is None else None
        texts[text] = None if (text in texts and texts[text] is None) or cause is None else cause
    return texts


def _quoted_cause(
    text: str, withdrawn: Mapping[str, str | None], hashes: Mapping[str, str | None]
) -> tuple[bool, str | None]:
    """别的包里的一行引用，引的是不是撤回了的话：引的就是那一句（结尾的句末标点不算），或者是截短了的那一句
    （末尾 … 或者 ...），8 个字以上。比 L1 在同一个包里认得严：不然撤回一个大包，别的包里碰巧是它某一句一部分的
    引用都被标上。原文已经彻底删除了的、从别的账本导进来的，只有指纹（hashes），只认一字不差的。"""
    body = quote_body(text)
    truncated = False
    if body.endswith("…"):
        body, truncated = body[:-1], True
    elif body.endswith("..."):
        body, truncated = body[:-3], True
    key = _quote_key(body)
    if len(key) < MIN_COPY_CHARS:
        return False, None
    if key in withdrawn:
        return True, withdrawn[key]
    digest = _key_hash(key)
    if digest in hashes:
        return True, hashes[digest]
    if truncated:
        for whole, cause in withdrawn.items():
            if whole.startswith(key):
                return True, cause
    return False, None


def _mark_quotes(conn: sqlite3.Connection, now: str, *, packet_id: str | None = None, revision: int | None = None) -> list[str]:
    """别的包里引用撤回内容的行也标上撤回：引用行只是旧话重说。packet_id 给了就只看这个包这一版（收件时）。
    因为撤回一个包才标上的，记下是哪个包，解除撤回那个包时一起解除。"""
    withdrawn = _withdrawn_texts(conn)
    _remember_texts(conn, withdrawn, now)
    hashes = {row[0]: row[1] for row in conn.execute("SELECT key_hash, cause FROM withdrawn_texts")}
    if not withdrawn and not hashes:
        return []
    holders: set[str] = set()
    if packet_id is None:
        candidates = conn.execute(
            "SELECT s.sentence_id, s.text FROM raw_sentences AS s JOIN packets AS p"
            " ON p.packet_id = s.packet_id AND p.revision = s.revision"
            " WHERE s.quoted = 1 AND s.withdrawn_at IS NULL AND p.withdrawn_at IS NULL AND p.state = 'current' ORDER BY s.id"
        ).fetchall()
        # 正在当证据的引用行（只在引用里看到过的话，D101）：Outlook、网易那种不带 > 的、“>>”连写的，原文索引里 quoted
        # 是 0，也按原话标上撤回，不然从它抽出来的事实留着（第四轮审阅第 1 条）；引用时折了行、和原话一字不差对不上的，
        # 是撤回了的话的一部分也算（第 2 条）。别的不带 > 的引用行照旧不看（当不了证据：撤回了的话取包时挡着）
        holder_rows = conn.execute(
            "SELECT s.sentence_id, s.text FROM quote_holders AS h JOIN raw_sentences AS s"
            " ON s.sentence_id = h.sentence_id AND s.revision = h.revision JOIN packets AS p"
            " ON p.packet_id = s.packet_id AND p.revision = s.revision"
            " WHERE s.withdrawn_at IS NULL AND p.withdrawn_at IS NULL AND p.state = 'current' ORDER BY s.id"
        ).fetchall()
        # 写账时记下的回信的人插在引用里的话（第五轮第 53 条，copy_role 是 3）：是从引用里认出来的，撤回原话时一样标上
        # （第五轮审阅第 1 条）。和正在当证据的那一份一样，原话的一部分也算
        holder_rows += conn.execute(
            "SELECT s.sentence_id, s.text FROM raw_sentences AS s JOIN packets AS p"
            " ON p.packet_id = s.packet_id AND p.revision = s.revision"
            " WHERE s.copy_role = 3 AND s.withdrawn_at IS NULL AND p.withdrawn_at IS NULL AND p.state = 'current' ORDER BY s.id"
        ).fetchall()
        holders = {row["sentence_id"] for row in holder_rows}
        listed = {row["sentence_id"] for row in candidates}
        candidates += [row for row in holder_rows if row["sentence_id"] not in listed]
    else:
        candidates = conn.execute(
            "SELECT sentence_id, text FROM raw_sentences WHERE packet_id = ? AND revision = ? AND quoted = 1"
            " AND withdrawn_at IS NULL ORDER BY seq",
            (packet_id, revision),
        ).fetchall()
    blob = "\n".join(withdrawn) if holders else ""
    marked: list[str] = []
    for row in candidates:
        quoted, cause = _quoted_cause(row["text"], withdrawn, hashes)
        if not quoted and row["sentence_id"] in holders:
            quoted, cause = _inside_withdrawn(row["text"], withdrawn, blob)
        if quoted:
            marked += mark_withdrawn(conn, row["sentence_id"], now, cause=cause)
    return marked


def _inside_withdrawn(text: str, withdrawn: Mapping[str, str | None], blob: str) -> tuple[bool, str | None]:
    """正在当证据的引用行（D101）是一句撤回了的话的一部分（引用时折了行），8 个字以上。原文彻底删除了的只有指纹，
    比不了（取包时按记下的 Message-ID、指纹挡）。blob 是撤回了的话用换行连起来的，先整个比一遍。"""
    key = _quote_key(quote_body(text))
    if len(key) < MIN_COPY_CHARS or key not in blob:
        return False, None
    for whole, cause in withdrawn.items():
        if key in whole:
            return True, cause
    return False, None


def mark_on_arrival(conn: sqlite3.Connection, packet: Packet, now: str) -> list[str]:
    """收件收尾的那个事务里（intake._finish 调）：新来的这一版里，复述、引用撤回内容的句子标上撤回。

    换版本时单条撤回的句子按指纹标上了，可新版本里新加的复述、引用指纹对不上；撤回以后才到的包里引用撤回的话也是。
    不标的话原文层查得到，抽取时也会当上文发给模型。
    """
    marked = _followers_in(conn, packet, now)
    marked += _mark_quotes(conn, now, packet_id=packet.header.packet_id, revision=packet.header.revision)
    return marked


def _copies_elsewhere(conn: sqlite3.Connection, sentence_ids: Sequence[str]) -> list[str]:
    """点名的句子在别的地方一字不差的、或者整句带着它的（不是引用）：转发、又说了一遍、回信主题里的“Re: 原主题”。
    8 个字以上的才报。不动，报出来让人看，要撤再点它们。"""
    found: set[str] = set()
    for sentence_id in dict.fromkeys(sentence_ids):
        row = conn.execute("SELECT text FROM raw_sentences WHERE sentence_id = ?", (sentence_id,)).fetchone()
        if row is None or len(row["text"]) < MIN_COPY_CHARS:
            continue
        found |= {
            copy["sentence_id"]
            for copy in conn.execute(
                "SELECT s.sentence_id FROM raw_sentences AS s JOIN packets AS p"
                " ON p.packet_id = s.packet_id AND p.revision = s.revision"
                " WHERE instr(s.text, ?) > 0 AND s.sentence_id != ? AND s.withdrawn_at IS NULL AND p.withdrawn_at IS NULL"
                " AND p.state = 'current'",
                (row["text"], sentence_id),
            )
        }
    return sorted(found)


def requeue_packets(settings: Settings, library: str, packet_ids: Collection[str]) -> bool:
    """别的命令要把几个写完了的包改回排队重抽（javmem extract 清包重发成 private 的旧版本时，预处理 B 用过旧版本里学来的
    名字的包）：拿写账的锁，拿不到（写账正在跑）返回 False，什么都不动；拿到了在一个事务里改，返回 True。"""
    try:
        lock = WriteLock(settings.require_data_dir())
    except WriteBusy:
        return False
    with lock:
        conn = open_ledger(settings.library(library))
        try:
            with transaction(conn):
                _requeue(conn, packet_ids, iso(settings.now()))
        finally:
            conn.close()
    return True


def _requeue(conn: sqlite3.Connection, packet_ids: Iterable[str], now: str) -> list[str]:
    """写完了的、标成 failed 的包改回排队（窗口少了内容，要用剩下的原文重抽、重写；failed 的原因跟着清掉，排队修）。
    撤回了的、private 不抽的不动。开始排队的时刻（queued_at，迁移 0029）：写完了的从 now 起算；failed 的取包时照样取，
    一直在排队，接着算。"""
    done = []
    for packet_id in sorted(set(packet_ids)):
        cursor = conn.execute(
            "UPDATE packets SET extraction = 'queued', extraction_note = NULL,"
            " queued_at = CASE WHEN extraction = 'failed' THEN queued_at ELSE ? END"
            " WHERE packet_id = ? AND state = 'current' AND withdrawn_at IS NULL AND extraction IN ('done', 'failed')",
            (now, packet_id),
        )
        if cursor.rowcount:
            done.append(packet_id)
    return done


def _caused_by_packets(conn: sqlite3.Connection) -> set[str]:
    """跟着撤回一个包才标上撤回的句子（别的包里引用它原话的行：withdrawn_sentences.cause 是那个包；主人点名撤回的
    cause 是空的）。这些句子所在的窗口算整包撤回带出来的（J5.6 审阅第 3 条）：写那几个窗口的时候，模型看得到撤回
    那包的原话。"""
    wanted: dict[str, set[tuple[str, str]]] = {}
    for row in conn.execute("SELECT packet_id, message_id, sentence_hash FROM withdrawn_sentences WHERE cause IS NOT NULL"):
        wanted.setdefault(row[0], set()).add((row[1], row[2]))
    found: set[str] = set()
    for packet_id in sorted(wanted):
        for row in conn.execute(
            "SELECT sentence_id, speaker, text, message_id FROM raw_sentences WHERE packet_id = ? AND withdrawn_at IS NOT NULL",
            (packet_id,),
        ):
            if covered(wanted[packet_id], row["message_id"], sentence_hash(row["speaker"], row["text"])):
                found.add(row["sentence_id"])
    # 同一条命令里主人又点名了这一行（记录的 cause 被点名改成了空）：照它引的话认，引的是撤回了的包里的话也算（复审第 3 条）
    quoted_rows = conn.execute(
        "SELECT sentence_id, text FROM raw_sentences WHERE withdrawn_at IS NOT NULL AND quoted = 1"
    ).fetchall()
    if quoted_rows:
        withdrawn = _withdrawn_texts(conn)
        hashes = {row[0]: row[1] for row in conn.execute("SELECT key_hash, cause FROM withdrawn_texts")}
        for row in quoted_rows:
            quoted, cause = _quoted_cause(row["text"], withdrawn, hashes)
            if quoted and cause is not None:
                found.add(row["sentence_id"])
    return found


def _withdrawn_in(conn: sqlite3.Connection) -> tuple[set[str], set[str], list[tuple[str, int, str]]]:
    """这个库里撤回了的包号、撤回了的句子。L3 结果库按这些清，所以上一次没清完的，这一次也清掉。

    第三样：发件人那一句撤回了、结果库还没清过的信（masked_mails，D73 第 16 条）。这封信的每一句也算：撤回以前存的
    窗口，抬头里写着发件人（长信后面几段的窗口清单里没有发件人那一句）。清过一次就勾掉，重抽以后的窗口写的是
    “[已撤回]”，以后不再删。"""
    packets = {row[0] for row in conn.execute("SELECT DISTINCT packet_id FROM packets WHERE withdrawn_at IS NOT NULL")}
    packets |= {row[0] for row in conn.execute("SELECT packet_id FROM withdrawn_packets")}
    sentences = {row[0] for row in conn.execute("SELECT sentence_id FROM raw_sentences WHERE withdrawn_at IS NOT NULL")}
    mails = [
        (row[0], int(row[1]), row[2])
        for row in conn.execute(
            "SELECT packet_id, revision, message_id FROM masked_mails WHERE store_cleaned_at IS NULL ORDER BY packet_id, message_id"
        )
    ]
    for packet_id, _revision, message_id in mails:  # 按信认，不按那一版：再跑以前包又重发了一版也找得到
        sentences |= {
            row[0]
            for row in conn.execute(
                "SELECT sentence_id FROM raw_sentences WHERE packet_id = ? AND message_id = ?", (packet_id, message_id)
            )
        }
    return packets, sentences, mails


def _forget_in_store(conn: sqlite3.Connection, store: ResultStore, report: WithdrawReport, now: str) -> None:
    """先把要删的窗口的正文所在的包改回排队，再删结果库里的窗口：反过来的话，中间断了，再跑时结果库里已经没有这些窗口，
    只靠结果库才认得出的包就再也不会改回排队。发件人撤回了的信删完了才勾掉（masked_mails.store_cleaned_at）。

    改回排队的同一个事务里，账本里这些窗口写进来的也先清（J5.6）：隔离行都删；整包撤回带出来的窗口（拿撤回的包当上文的、
    往来串一起写的、标注用了从它学来的名字的、有跟着它标上撤回的引用行的）连别的包写的说法也删，下一次 extract、write
    照剩下的原文重抽回来。单条撤回带出来的窗口，别的说法留着等重抽（证据是别的句子）。"""
    report.store = True
    packets, sentences, mails = _withdrawn_in(conn)
    # 放进去的现有关系：账本已经按剩下的内容改了名、删了别名，写着的名字、认出一头的别名对不上了的，用过它的窗口也算
    stale = prer.stale_keys(conn, store)
    found = store.doomed_windows(packets=packets, sentences=sentences, extra={(key[0], key[1]) for key in stale},
                                 whole_sentences=_caused_by_packets(conn))
    doomed = [(one.window_id, one.cache_key) for one in found]
    targets = {packet_id for one in found for packet_id in one.bodies}
    bodies: dict[str, set[str]] = {}
    for one in found:
        bodies.setdefault(one.window_id, set()).update(one.bodies)
    with transaction(conn):
        writer = LedgerWriter(conn, extracted_by="", embedding_model="", register=False)
        cleared, quarantined = writer.forget_windows(bodies, claims={one.window_id for one in found if one.whole})
        report.cleared_claims += cleared
        report.cleared_quarantine += quarantined
        more = _requeue(conn, targets, now)
    report.requeued = sorted(set(report.requeued) | set(more))
    report.store_windows = len(doomed)
    report.store_results = store.delete_windows(doomed)
    # 预处理 B：撤回的内容里学来的名字、“也叫”，没有窗口的标注也删（窗口的标注 delete_windows 已经连着删了）
    store.forget_preb(packets=packets, sentences=sentences)
    store.forget_prea(packets=packets, sentences=sentences)  # 预处理 A：没有窗口的标注也删
    store.forget_prer(packets=packets, sentences=sentences)  # 放进去的现有关系：出自撤回内容的、没有窗口的也删
    store.forget_prer_keys(stale)  # 对不上账本的那几行（没有窗口的也算）
    if mails:
        with transaction(conn):
            conn.executemany(
                "UPDATE masked_mails SET store_cleaned_at = ? WHERE packet_id = ? AND revision = ? AND message_id = ?"
                " AND store_cleaned_at IS NULL",
                [(now, *mail) for mail in mails],
            )


# ---------- 软变化判定的 soft.db（U3；以后要修.md“加关系隶属于”第 6 条） ----------
#
# verdicts 存着发给模型的题（前后几行原话打过码拼起来，还带着旧事实最后一次被说到的那一句）和原样的回复（引着原话）。
# 撤回以后，带着撤回句子的判定整条删：拿撤回的句子照发题时的写法（javmem.soft.judge.shown_text）写出来，在题和回复里
# 找。宁可多删：删了的判定，写账最后一步找不到就留在待确认里，下次 javmem soft judge 重判（花钱）。只认数据目录的
# soft/soft.db：写账最后一步只照它执行；javmem soft judge --store 指到别处的不清，用完自己删。

SOFT_MIN_CHARS = 6  # 撤回的句子去掉空白不到这么多字的不拿来比：“好的。”这类，比了会把别处一字不差的那几道题都带走


def _soft_dir(settings: Settings) -> Path:
    return settings.require_data_dir() / "soft"


def _soft_texts(conn: sqlite3.Connection, settings: Settings) -> set[str]:
    """这个库里撤回了的句子（单条撤回的，加上撤回了的包的全部句子），照发题时的写法写出来的样子。按库里所有撤回了的
    东西算：上一次没清成的（判定正在跑），这一次接着清。"""
    from javmem.redaction import load_patterns
    from javmem.soft.judge import shown_text

    rows = conn.execute(
        "SELECT text FROM raw_sentences WHERE withdrawn_at IS NOT NULL"
        " UNION SELECT r.text FROM raw_sentences r JOIN packets p ON p.packet_id = r.packet_id AND p.revision = r.revision"
        " WHERE p.withdrawn_at IS NOT NULL"
    ).fetchall()
    patterns = load_patterns(settings.redaction_file)
    return {shown_text(str(row[0]), patterns) for row in rows if len("".join(str(row[0]).split())) >= SOFT_MIN_CHARS}


def _soft_doomed(store: object, texts: Collection[str]) -> list[str]:
    """带着撤回句子的判定的键（题里有，或者回复里引着）。"""
    if not texts:
        return []
    return [key for key, request, output in store.verdict_texts()  # type: ignore[attr-defined]
            if any(text in request or text in output for text in texts)]


def _clean_soft(conn: sqlite3.Connection, settings: Settings, report: WithdrawReport) -> None:
    """撤回：删 soft.db 里带着撤回句子的判定。软变化判定正在跑（拿着 soft.db 的锁）的不去抢，说一句，下一次接着清。"""
    from javmem.soft.judge import STORE_FILE, SoftStore, StoreBusy

    soft_dir = _soft_dir(settings)
    if not (soft_dir / STORE_FILE).is_file():
        return
    texts = _soft_texts(conn, settings)
    if not texts:
        return
    try:
        soft = SoftStore(soft_dir, exclusive=True)
    except StoreBusy:
        report.problems.append("软变化判定正在跑（它拿着 soft.db 的锁）：这一次没清 soft.db，等它跑完再撤回一次（不写目标也行）接着清")
        return
    with soft:
        doomed = _soft_doomed(soft, texts)
        if doomed:
            report.soft_deleted += soft.delete_verdicts(doomed)


def _open_soft(settings: Settings) -> object | None:
    """彻底删除：先拿住 soft.db 的锁（没有 soft.db 的回 None）。判定正在跑的，什么都不删，抛 WithdrawError：
    原文删掉以后就比不出来哪几道题带着它了。"""
    from javmem.soft.judge import STORE_FILE, SoftStore, StoreBusy

    soft_dir = _soft_dir(settings)
    if not (soft_dir / STORE_FILE).is_file():
        return None
    try:
        return SoftStore(soft_dir, exclusive=True)
    except StoreBusy:
        raise WithdrawError("软变化判定正在跑（它拿着 soft.db 的锁）：等它跑完再彻底删除（这一次什么都没删）") from None


def _fact_texts(settings: Settings) -> set[str]:
    """这个数据目录两个账本里所有事实和说法的文字：向量缓存两个库共用，只删两边都不再用的。
    说法的文字也留着：整本重建时每处说法的文字都要向量，留着就不用再调向量服务。"""
    texts: set[str] = set()
    for library in settings.libraries():
        if not library.db_path.exists():
            continue
        conn = connect_readonly(library.db_path)
        try:
            texts |= {row[0] for row in conn.execute("SELECT content FROM facts UNION SELECT content FROM fact_claims")}
        finally:
            conn.close()
    return texts


def _relation_texts(settings: Settings) -> set[str]:
    """接着主图的库里，现在每条关系拿来算向量的那一句（javmem.graph.project 的 vector_text）：主图同步要用，清向量缓存时
    留着（J5.6；以前连关系的向量一起删了，以后整张重建、关系又变了都要重算）。撤回了的、名字变了的关系，旧的那一句
    不在里面，照样删。没接主图的库没算过关系的向量，不看。"""
    from javmem.graph.project import Projector

    texts: set[str] = set()
    for library in settings.libraries():
        if not library.db_path.exists():
            continue
        conn = connect_readonly(library.db_path)
        try:
            try:
                state = conn.execute("SELECT enabled FROM graph_state WHERE id = 1").fetchone()
            except sqlite3.OperationalError as error:
                if "no such table" in str(error):  # 旧账本（迁移 0026 以前）：没接过主图
                    continue
                raise  # 别的（锁着、坏了）照实往外报，和读事实的文字那一步一样，不当成没有关系把向量删了
            if state is None or not state[0]:
                continue
            projector = Projector(conn, settings.timezone)
            texts |= {item.vector_text for item in projector.relations(projector.all_ids("relation")).values() if item.vector_text}
        finally:
            conn.close()
    return texts


def _clean_vectors(settings: Settings, vectors_dir: Path, *, vacuum: bool = False) -> int:
    """返回删了几条。vacuum 时整理没做完（有人正读着）抛 _Incomplete。"""
    if not (vectors_dir / VECTOR_FILE).is_file():
        return 0
    keep = _fact_texts(settings) | _relation_texts(settings)
    deleted = 0
    with VectorCache(vectors_dir) as cache:
        for model in cache.models():
            deleted += cache.delete_unused(model, keep)
        if vacuum and not cache.vacuum():
            raise _Incomplete("向量缓存")
    return deleted


# ---------- 撤回的记录：整本重建时搬到新账本 ----------


def export_records(settings: Settings, library_name: str) -> dict[str, object]:
    """撤回的记录：撤回过的包号，单条撤回的句子的指纹（说话人加原句的 sha256，不带原文；邮件带着是哪一封信的
    message_id，对话包是空的），彻底删除过的每一版（revisions：包号、第几版、什么时候删的；单删替换下来的旧版本也在里面）。
    整本重建时导进新账本（revisions 导的时候不用：新账本收不到删掉了的归档）；恢复备份以后照它再删一遍
    （javmem restore-check，U2 丙）。旧账本先升到最新（不然没有 message_id 这一列）；这个库还没有账本的报配置错误，
    不建一本空的。"""
    library = settings.library(library_name)
    if not library.db_path.is_file():
        raise ConfigError(f"{library_name} 库还没有账本")
    open_ledger(library).close()
    conn = connect_readonly(library.db_path)
    try:
        packets = [
            {"packet_id": row["packet_id"], "withdrawn_at": row["withdrawn_at"], "purged_at": row["purged_at"]}
            for row in conn.execute("SELECT packet_id, withdrawn_at, purged_at FROM withdrawn_packets ORDER BY packet_id")
        ]
        stored = {row[0]: row[1] for row in conn.execute("SELECT key_hash, cause FROM withdrawn_texts")}
        for key, cause in _withdrawn_texts(conn).items():  # 还没记下指纹的（以前的账本）现算
            digest = _key_hash(key)
            stored[digest] = None if cause is None or stored.get(digest, cause) is None else cause
        texts = [{"key_hash": digest, "cause": cause} for digest, cause in sorted(stored.items())]
        sentences = [
            {
                "packet_id": row["packet_id"],
                "message_id": row["message_id"],
                "sentence_hash": row["sentence_hash"],
                "withdrawn_at": row["withdrawn_at"],
                "cause": row["cause"],
            }
            for row in conn.execute(
                "SELECT packet_id, message_id, sentence_hash, withdrawn_at, cause FROM withdrawn_sentences"
                " ORDER BY packet_id, message_id, sentence_hash"
            )
        ]
        revisions = [
            {"packet_id": row["packet_id"], "revision": int(row["revision"]), "purged_at": row["purged_at"]}
            for row in conn.execute(
                "SELECT packet_id, revision, purged_at FROM packets WHERE purged_at IS NOT NULL ORDER BY packet_id, revision"
            )
        ]
    finally:
        conn.close()
    return {"schema": RECORDS_SCHEMA, "library": library_name, "packets": packets, "sentences": sentences, "texts": texts,
            "revisions": revisions}


def _import_records(conn: sqlite3.Connection, records: Mapping[str, object], now: str, library_name: str) -> None:
    """撤回的记录记进这本账（一个事务）。这本账里已经有、还没撤回的包和句子，_pending_records 找出来接着撤回。"""
    if records.get("schema") != RECORDS_SCHEMA:
        raise ConfigError(f"撤回记录的格式不对，要 {RECORDS_SCHEMA}")
    if records.get("library") not in (None, library_name):
        raise ConfigError(f"这份撤回记录是 {records.get('library')} 库的，不能导进 {library_name} 库")
    packets = records.get("packets")
    sentences = records.get("sentences")
    if not isinstance(packets, list) or not isinstance(sentences, list):
        raise ConfigError("撤回记录要有 packets、sentences 两个列表")
    texts = records.get("texts", [])
    if not isinstance(texts, list):
        raise ConfigError("撤回记录的 texts 要是列表")
    try:
        packet_rows = [(str(item["packet_id"]), str(item["withdrawn_at"]), item.get("purged_at")) for item in packets]
        text_rows = [(str(item["key_hash"]), item.get("cause"), now) for item in texts]
        # 这一版以前导出的没有 message_id：照旧挡整个包（宁可多挡）
        sentence_rows = [
            (str(item["packet_id"]), str(item.get("message_id") or ""), str(item["sentence_hash"]), str(item["withdrawn_at"]), item.get("cause"))
            for item in sentences
        ]
    except (KeyError, TypeError):
        raise ConfigError("撤回记录里有一项缺字段") from None
    with transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO withdrawn_packets (packet_id, withdrawn_at, purged_at) VALUES (?, ?, ?)", packet_rows
        )
        conn.executemany(
            "INSERT INTO withdrawn_texts (key_hash, cause, withdrawn_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key_hash) DO UPDATE SET cause = CASE WHEN excluded.cause IS NULL THEN NULL ELSE cause END",
            text_rows,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO withdrawn_sentences (packet_id, message_id, sentence_hash, withdrawn_at, cause)"
            " VALUES (?, ?, ?, ?, ?)",
            sentence_rows,
        )


def _pending_records(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """撤回记录里有、这本账里还没撤回的：撤回过的包号还是现行的包，指纹对得上、还没标的原文句。只有导进记录
    （中途断了的也算）才会这样：平常撤回、收件、解除撤回都是记录和标记一起改。返回 (包, 原文句)。"""
    packets = [
        row[0]
        for row in conn.execute(
            "SELECT w.packet_id FROM withdrawn_packets AS w JOIN packets AS p ON p.packet_id = w.packet_id"
            " WHERE p.state = 'current' AND p.withdrawn_at IS NULL ORDER BY w.packet_id"
        )
    ]
    wanted: dict[str, set[tuple[str, str]]] = {}
    for row in conn.execute("SELECT packet_id, message_id, sentence_hash FROM withdrawn_sentences"):
        wanted.setdefault(row[0], set()).add((row[1], row[2]))
    sentences: list[str] = []
    for packet_id in sorted(wanted):
        for row in conn.execute(
            "SELECT s.sentence_id, s.speaker, s.text, s.message_id FROM raw_sentences AS s JOIN packets AS p"
            " ON p.packet_id = s.packet_id AND p.revision = s.revision"
            " WHERE s.packet_id = ? AND s.withdrawn_at IS NULL AND p.withdrawn_at IS NULL AND p.state = 'current'"
            " ORDER BY s.id",
            (packet_id,),
        ):
            if covered(wanted[packet_id], row["message_id"], sentence_hash(row["speaker"], row["text"])):
                sentences.append(row["sentence_id"])
    return packets, sentences


# ---------- 解除撤回 ----------


def unwithdraw(settings: Settings, library_name: str, packets: Sequence[str]) -> UndoReport:
    """解除撤回（packet-spec 第 10 节）：只按包。包重新排队，派生的东西要重抽、重写。撤回它时别的包里跟着标上的
    引用行（单条撤回的记录记着是因为它）一起解除，那些包也改回排队。彻底删除过的回不来。

    单条撤回的句子不解除：点名撤回的是主人定的。整本重建时导进来的撤回（新账本里没有包登记）也能解除，
    解除以后要再投一次原包才收得进来（resend）。
    """
    library = settings.library(library_name)
    data_dir = settings.require_data_dir()
    with intake_lock(data_dir), WriteLock(data_dir):
        conn = open_ledger(library)
        try:
            reconcile_library(settings, library, conn)
            todo, unknown, purged, resend = [], [], [], []
            for packet_id in dict.fromkeys(packets):
                rows = conn.execute("SELECT withdrawn_at, purged_at FROM packets WHERE packet_id = ?", (packet_id,)).fetchall()
                record = conn.execute("SELECT purged_at FROM withdrawn_packets WHERE packet_id = ?", (packet_id,)).fetchone()
                if not rows and record is None:
                    unknown.append(packet_id)
                elif any(row["purged_at"] is not None for row in rows) or (record is not None and record[0] is not None):
                    purged.append(packet_id)
                elif any(row["withdrawn_at"] is not None for row in rows) or record is not None:
                    todo.append(packet_id)
                    if not rows:  # 整本重建时导进来的：新账本里没有包登记，只记在 withdrawn_packets
                        resend.append(packet_id)
            if unknown:
                raise WithdrawError(f"库里没有这些包：{'、'.join(unknown)}")
            if purged:
                raise WithdrawError(f"这些包彻底删除过了，回不来：{'、'.join(purged)}")
            now = iso(settings.now())
            with transaction(conn):
                requeue: set[str] = set()
                for packet_id in todo:
                    # 现行的那一版真撤回过（不是只记在 withdrawn_packets 里：导进来的记录、包还没撤回）
                    current = conn.execute(
                        "SELECT withdrawn_at FROM packets WHERE packet_id = ? AND state = 'current'", (packet_id,)
                    ).fetchone()
                    back = current is not None and current["withdrawn_at"] is not None
                    conn.execute("UPDATE packets SET withdrawn_at = NULL WHERE packet_id = ?", (packet_id,))
                    conn.execute("DELETE FROM withdrawn_packets WHERE packet_id = ?", (packet_id,))
                    # 开始排队的时刻（queued_at，迁移 0029）：真撤回过的从解除这一刻起算，撤回的那一阵不算，撤回时本来就在
                    # 排队的也一样；没真撤回过的照改回排队的规矩（写完了的从现在起算，failed、queued 的一直在排队）
                    conn.execute(
                        "UPDATE packets SET extraction = 'queued', extraction_note = NULL,"
                        " queued_at = CASE WHEN ? OR extraction = 'done' THEN ? ELSE queued_at END"
                        " WHERE packet_id = ? AND state = 'current' AND extraction IN ('done', 'failed', 'queued')",
                        (back, now, packet_id),
                    )
                    requeue |= _release_followers(conn, packet_id)
                    conn.execute("DELETE FROM withdrawn_texts WHERE cause = ?", (packet_id,))
                if todo:  # 拿它当上文的后一包，窗口里又有上文了
                    requeue |= {
                        row[0]
                        for row in conn.execute(
                            f"SELECT packet_id FROM packets WHERE state = 'current' AND continues IN ({_marks(todo)})", todo
                        )
                    }
                # 放回来的引用行里，引的是别的还撤回着的包、句子的，再标上（两个包里有同一句话时）
                requeue |= {sentence_id.split("#", 1)[0] for sentence_id in _mark_quotes(conn, now)}
                _requeue(conn, requeue, now)
        finally:
            conn.close()
    return UndoReport(todo, resend)


def _release_followers(conn: sqlite3.Connection, cause: str) -> set[str]:
    """撤回 cause 这个包时，别的包里跟着标上的引用行：记录删掉，原文索引里指纹对得上、又没有别的记录的句子解除标记。
    返回这些句子所在的包（要重抽）。"""
    rows = conn.execute(
        "SELECT packet_id, message_id, sentence_hash FROM withdrawn_sentences WHERE cause = ?", (cause,)
    ).fetchall()
    conn.execute("DELETE FROM withdrawn_sentences WHERE cause = ?", (cause,))
    released: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        released.setdefault(row["packet_id"], set()).add((row["message_id"], row["sentence_hash"]))
    packets: set[str] = set()
    for packet_id in sorted(released):
        remaining = {
            (row[0], row[1])
            for row in conn.execute("SELECT message_id, sentence_hash FROM withdrawn_sentences WHERE packet_id = ?", (packet_id,))
        }
        for row in conn.execute(
            "SELECT id, speaker, text, message_id FROM raw_sentences WHERE packet_id = ? AND withdrawn_at IS NOT NULL", (packet_id,)
        ).fetchall():
            digest = sentence_hash(row["speaker"], row["text"])
            if covered(released[packet_id], row["message_id"], digest) and not covered(remaining, row["message_id"], digest):
                conn.execute("UPDATE raw_sentences SET withdrawn_at = NULL WHERE id = ?", (row["id"],))
                packets.add(packet_id)
    return packets


# ---------- 彻底删除 ----------


class _Incomplete(Exception):
    """整理没做完：有查询正读着，删掉的内容可能还在空页、日志里。"""


def purge(
    settings: Settings,
    library_name: str,
    packet_id: str,
    *,
    revision: int | None = None,
    store_dir: Path | None = None,
    vectors_dir: Path | None = None,
) -> PurgeReport:
    """彻底删除（02 p2：要求彻底删除时再物理删）。两种：

    - 不写 revision：撤回了的包，所有版本的归档文件、跳过的副本、原文索引里的句子都删掉。
    - 写 revision：替换下来的旧版本（或者撤回了的包的某一版），只删这一版的归档、跳过的副本和这一版在原文索引里的
      句子（packet-spec 第 10 节：旧版里有没打码的密钥，要用管理命令物理删除旧版）。现行的版本不能单独删。

    包登记的行留着（记下 purged_at），撤回过的包号还是不再收。冻结事实上指着删掉内容的证据也删掉。
    最后整理账本、L3 结果库、向量缓存的文件，删掉的内容不留在空页和日志里；有查询正读着、整理没做完的，记在
    incomplete 里，等查询结束再跑一次。再跑一次没事：早就删过的只再找一遍副本、原文句，再整理一遍。
    """
    library = settings.library(library_name)
    store_dir, vectors_dir = default_dirs(settings, store_dir, vectors_dir)
    report = PurgeReport(library_name, packet_id)
    with _locked(settings, store_dir) as store:
        conn = open_ledger(library)
        soft = None
        try:
            reconcile_library(settings, library, conn)
            rows = conn.execute("SELECT * FROM packets WHERE packet_id = ? ORDER BY revision", (packet_id,)).fetchall()
            record = conn.execute("SELECT purged_at FROM withdrawn_packets WHERE packet_id = ?", (packet_id,)).fetchone()
            if not rows and record is None:
                raise WithdrawError(f"库里没有这个包：{packet_id}")
            if not rows and revision is not None:
                raise WithdrawError(f"{packet_id} 在这本账里没有包登记（整本重建时导进来的），只能整包删跳过的副本")
            if revision is None:
                # 有包登记的看包登记：撤回记录导进来了、还没真撤回的（中途断了），也先撤回，不然派生的东西没清就删了原文
                if (rows or record is None) and not any(row["withdrawn_at"] is not None for row in rows):
                    raise WithdrawError(f"{packet_id} 还没撤回，先撤回再彻底删除")
                chosen = list(rows)
            else:
                chosen = [row for row in rows if row["revision"] == revision]
                if not chosen:
                    raise WithdrawError(f"{packet_id} 没有第 {revision} 版")
                if chosen[0]["state"] == "current" and chosen[0]["withdrawn_at"] is None:
                    raise WithdrawError("现行的版本不能单独删：先撤回整个包，或者等打包方重发新版本")
            targets = [row for row in chosen if row["purged_at"] is None]
            report.revisions = [int(row["revision"]) for row in targets]
            report.already = not targets and not (record is not None and record[0] is None and not rows)
            soft = _open_soft(settings)  # 软变化判定正在跑：抛 WithdrawError，这时候还什么都没删
            # 跳过的副本每次都找：删过以后打包方又重发的，收件照样跳过、放进 skipped/，再删一次就清掉
            report.files = _delete_archives(settings, library, packet_id, targets, revision=revision)
            if soft is not None:  # 原文句删掉以前：撤回时没清成的（判定正在跑），这里接着清
                report.soft_deleted = soft.delete_verdicts(_soft_doomed(soft, _soft_texts(conn, settings)))
            now = iso(settings.now())
            conn.execute("PRAGMA secure_delete = ON")  # 删的时候就把内容抹成 0
            with transaction(conn):
                _remember_texts(conn, _withdrawn_texts(conn), now)  # 原文句删掉以前记下指纹：以后到的引用还认得出
                if revision is None:
                    conn.execute(
                        "INSERT INTO withdrawn_packets (packet_id, withdrawn_at, purged_at) VALUES (?, ?, ?)"
                        " ON CONFLICT (packet_id) DO UPDATE SET purged_at = coalesce(purged_at, excluded.purged_at)",
                        (packet_id, now, now),
                    )
                # 只在引用里看到过的话（D101，第四轮审阅第 2、3 条）：删以前记下这些信的 Message-ID 和句子的指纹，
                # 回这些信的、引同一句话的引用，以后照样不当证据
                scope, params = ("packet_id = ?", (packet_id,)) if revision is None else ("packet_id = ? AND revision = ?", (packet_id, revision))
                conn.execute(
                    "INSERT OR IGNORE INTO purged_mails (message_id, purged_at) SELECT DISTINCT message_id, ? FROM raw_sentences"
                    f" WHERE {scope} AND message_id IS NOT NULL",
                    (now, *params),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO purged_copies (copy_key, purged_at) SELECT DISTINCT copy_key, ? FROM raw_sentences"
                    f" WHERE {scope} AND copy_key IS NOT NULL",
                    (now, *params),
                )
                # 原文句每次都删：整包删的删这个包的全部，单删一版的删这一版的（现行那一版在原文索引里）
                if revision is None:
                    cursor = conn.execute("DELETE FROM raw_sentences WHERE packet_id = ?", (packet_id,))
                else:
                    cursor = conn.execute("DELETE FROM raw_sentences WHERE packet_id = ? AND revision = ?", (packet_id, revision))
                report.sentences = max(cursor.rowcount, 0)
                for row in targets:
                    conn.execute(
                        "UPDATE packets SET purged_at = ?, entities = '[]' WHERE packet_id = ? AND revision = ?",
                        (now, packet_id, row["revision"]),
                    )
                for row in chosen:
                    conn.execute("DELETE FROM quarantine WHERE packet_id = ? AND revision = ?", (packet_id, row["revision"]))
                    conn.execute(
                        "DELETE FROM evidence WHERE alias_id IS NOT NULL AND packet_id = ? AND revision = ?",
                        (packet_id, row["revision"]),
                    )
                writer = LedgerWriter(conn, extracted_by="", embedding_model="", register=False)
                report.hidden = writer.hide_withdrawn(now)
                forget_judgments(conn)
            conn.execute("INSERT INTO raw_fts (raw_fts) VALUES ('optimize')")  # 全文索引里删掉的词条合并掉
            conn.execute("INSERT INTO raw_norm_fts (raw_norm_fts) VALUES ('optimize')")
            if not _vacuum(conn):
                report.incomplete.append("账本")
            purged = {(packet_id, int(row["revision"])) for row in chosen}
            # 放进去的现有关系：账本删完了以后写着的名字、认出一头的别名对不上了的，用过它的窗口也删、也改回排队
            stale = prer.stale_keys(conn, store)
            extra = {(key[0], key[1]) for key in stale}
            doomed, _ = (
                store.doomed(packets={packet_id}, extra=extra) if revision is None else store.doomed(revisions=purged, extra=extra)
            )
            # 预处理 B：别的包的标注里用了这几个版本里学来的名字的，先改回排队重抽，再删窗口（和撤回一样的先后）
            _, noted = (
                store.doomed_by_notes(packets={packet_id}, extra=extra) if revision is None
                else store.doomed_by_notes(revisions=purged, extra=extra)
            )
            if noted:
                with transaction(conn):
                    report.requeued = _requeue(conn, noted, now)
            report.store_windows = len(doomed)
            report.store_results = store.delete_windows(doomed)
            # 预处理 B：从这个包（这几个版本）学来的名字、“也叫”，没有窗口的标注也删
            if revision is None:
                store.forget_preb(packets={packet_id})
                store.forget_prea(packets={packet_id})
                store.forget_prer(packets={packet_id})
            else:
                store.forget_preb(revisions=purged)
                store.forget_prea(revisions=purged)
                store.forget_prer(revisions=purged)
            store.forget_prer_keys(stale)
            if not store.vacuum():
                report.incomplete.append("L3 结果库")
            if soft is not None and not soft.scrub():  # 删过的判定不留在空页、日志里
                report.incomplete.append("soft.db")
        finally:
            if soft is not None:
                soft.close()
            conn.close()
        try:
            report.vectors_deleted = _clean_vectors(settings, vectors_dir, vacuum=True)
        except _Incomplete as error:
            report.incomplete.append(str(error))
    return report


def _vacuum(conn: sqlite3.Connection) -> bool:
    """整理库文件、清日志。有查询正读着（还开着读事务）时做不完，返回 False。"""
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError:
        return False
    busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    return busy == 0


def _delete_archives(
    settings: Settings,
    library: Library,
    packet_id: str,
    targets: Sequence[sqlite3.Row],
    *,
    revision: int | None,
) -> list[str]:
    """删归档文件：要删的每个版本的，以及 skipped/ 里这个包（整包删时每一版，单删一版时这一版）的副本。
    跳过的副本按包头认，名字可能加了 _2。"""
    data_dir = settings.require_data_dir()
    paths = [data_dir / row["archive_path"] for row in targets]
    skipped = library.archive / "skipped"
    if skipped.is_dir():
        for entry in sorted(os.scandir(skipped), key=lambda item: item.name):
            if not entry.is_file(follow_symlinks=False) or not entry.name.startswith(packet_id):
                continue
            try:
                header = parse_packet(Path(entry.path).read_bytes(), filename=f"{packet_id}.md").header
            except (OSError, Rejected, ValueError):
                continue  # 读不出包头的不认，不删
            if header.packet_id == packet_id and (revision is None or header.revision == revision):
                paths.append(Path(entry.path))
    removed = []
    for path in paths:
        if path.exists():
            _remove_file(path)
            removed.append(path.relative_to(data_dir).as_posix())
    return removed


def _remove_file(path: Path) -> None:
    """归档是只读的，Windows 上只读文件删不掉，先改回可写。"""
    try:
        os.chmod(path, 0o600)
        os.unlink(path)
    except FileNotFoundError:
        pass
