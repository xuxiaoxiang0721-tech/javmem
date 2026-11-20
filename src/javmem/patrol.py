"""巡检（packet-spec 第 12 节）：有情况就提醒，多久跑一次由部署方定。

只报包号、行号和类型，不抄原文。巡检只读不写；标“看过了”（acknowledge，J2.12）是管理命令，
只写账本里巡检那两张辅助表：收件记下的警告标了就不再报，同样的警告又来一次重新报；daily 包的缺口按指纹记，
缺口变了重新报。别的几种（失败包、收件目录里多出来的东西、太久没跑……）是现在的样子，处理掉就不报了，不能标。

还盯着越攒越多、攒多了写账会慢的几样（巡检加快慢检查，11-16）：待确认、最大的一组、实体和别名、账本所在的磁盘、
写账的快慢。到了 config.PatrolLimits 的数就提醒，也是现在的样子、不能标，只报数、编号、大小。写账的快慢读
write_runs，那一行是 javmem write 写完时记的（WriteTimer、record_write_run，在这里是因为只有巡检读它）。

这一回没看的几种说一声（巡检要 javmem 改的第 7 样）：拿不到收件锁（收件、撤回、合并这类正在跑）时对账那几样不看；
来源登记表读不了时收件目录那几样不看；别名表读不了时别名表改没改不看。每个原因一条 checks_skipped（reason、skipped），
跟在要看的后面进 --json：外面的巡检拿这一回跟上一回比，没看的那几种不能当“好了”。它不算要看的，退出码不因为它变。

备份太久没成功（backup_stale，U2 丙）：读 `logs/backup-status.json`（javmem backup 每一次记一笔），备份过的数据目录才报。

模型、版本变了（D205 ⑪）：数据目录那一份基准（`logs/versions-baseline.json`，javmem versions --save 不写文件名存的）在，
就拿它比结果库每张表最后几行里服务商实际给的模型名（served_model_changed）；探针不跑，读 javmem versions --check 记的一笔
（`logs/versions-status.json`），上一回查出不一样报 probe_changed，8 天没查报 probe_stale，上一回本机 Qwen 一直有别人在用、
这回没量到的记 checks_skipped（probe_busy，不当成变了，D229 ⑫）；基准读不了报 versions_unreadable。
模型地址不是官方的（照这个进程的环境变量）报 endpoint_unofficial，一本账里几版规则混着写报 rules_mixed：这两样不和基准比。
没存过基准、基准读不了的，和基准比的那几样各记一条 checks_skipped（no_versions_baseline、versions_unreadable），跟在最后；
基准里有模型、结果库一个都读不了的记 stores_unreadable，模型设置写错了的记 model_settings_unreadable。都只读，不连网、不调模型。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from javmem.config import ConfigError, Library, PatrolLimits, Settings
from javmem.embedding import Embedder, EmbeddingReply
from javmem.graph.dirty import WITHDRAWN_CONDITION
from javmem.intake import (
    TASK_INTAKE,
    TMP_DIR,
    UNKNOWN_DIR,
    IntakeBusy,
    error_file_name,
    intake_lock,
)
from javmem.ledger import open_ledger, transaction
from javmem.pipeline.write import LEXICON_RULE, WriteRun, ledger_lexicon
from javmem.sources import Registry, load_registry
from javmem.versions import (
    BASELINE_FILE,
    OFFICIAL_LLM,
    OFFICIAL_SOFT,
    STATUS_FILE,
    STATUS_FORMAT,
    baseline_digest,
    data_stores,
    is_official,
    open_readonly,
)
from javmem.versions import FORMAT as VERSIONS_FORMAT
from javmem.zh.lexicon import load_lexicon
from javmem.timeutil import epoch, iso, parse_aware

_WARNING_TEXT = {
    "unknown_heading": "认不出的标题",
    "bad_attachment": "写错的附件块，这一块没进原文索引",
    "bad_line": "解析不了的行",
    "unrecognized_line": "认不出的行",
    "time_backwards": "时间倒退",
    "unknown_field": "认不出的包头字段，可能打错了字",
    "id_time_mismatch": "packet_id 的日期和时分和 covers_to 对不上",
    "suspect_continuation": "缩进的续行长得像另一个人在说话，现在算在上一句里",
    "odd_speaker": "说话人不像人名，可能漏写了说话人",
    "redaction_hit": "打码模式表命中",
    "same_revision_different": "revision 相同、内容却不一样",
    "withdrawn_resent": "撤回过的包号又来了",
    "registration_lost": "登记了但文件不见了，这次没收下",
    "archive_conflict": "归档里有包登记不认识的同名文件，已挪开",
    "index_failed": "这个包收到一半出错了，还没收完",
}


@dataclass(frozen=True)
class PatrolItem:
    kind: str
    message: str
    library: str | None = None
    source: str | None = None
    packet_id: str | None = None
    revision: int | None = None
    line: int | None = None
    count: int | None = None
    ref: str | None = None  # 能标“看过了”的才有：库/W 警告编号，库/G 缺口的指纹（javmem patrol --ack）
    stats: dict[str, object] | None = None  # 盯着快慢的那几样：数、编号、大小（--json 里也有），不抄原文
    reason: str | None = None  # 只有 checks_skipped 有：这回为什么没看（intake_busy、sources_unreadable、lexicon_unreadable）
    skipped: tuple[str, ...] | None = None  # 只有 checks_skipped 有：这回没看的种类，就是别的提醒的 kind

    def to_dict(self) -> dict:
        return {key: list(value) if isinstance(value, tuple) else value
                for key, value in asdict(self).items() if value is not None}


CHECKS_SKIPPED = "checks_skipped"
# 这一回没看的几种（巡检要 javmem 改的第 7 样）：拿不到收件锁时 _settled_items 那几样，来源登记表读不了时收件目录那几样
# （_inbox_items 两种，_settled_items 里的 inbox_stuck），别名表读不了时 _lexicon_items 那一种
SETTLED_KINDS = ("failed_without_note", "archive_missing", "archive_orphan", "registration_stuck", "inbox_stuck")
INBOX_KINDS = ("inbox_stray", "inbox_unknown_dir", "inbox_stuck")
LEXICON_KINDS = ("lexicon_changed",)


def _skipped(reason: str, kinds: Sequence[str], why: str) -> PatrolItem:
    """一条 checks_skipped：没有 library 就是每个库都没看（failed_without_note 本来就不分库）。"""
    return PatrolItem(CHECKS_SKIPPED, f"{why}：{'、'.join(kinds)}", reason=reason, skipped=tuple(kinds))


@dataclass(frozen=True)
class PatrolReport:
    """一回巡检：要看的（items），和这一回没看的那几种（skipped，每个原因一条 checks_skipped）。"""

    items: list[PatrolItem]
    skipped: list[PatrolItem]

    def everything(self) -> list[PatrolItem]:
        """--json 打的：要看的在前，没看的跟在后面。"""
        return [*self.items, *self.skipped]


def _failed_packets(directory: str) -> list[str]:
    """一个失败目录里的失败包。说明文件不算，除非它本身就是被拒收的文件。"""
    names = {child.name for child in os.scandir(directory) if child.is_file()}
    return sorted(name for name in names if not name.endswith(".error.txt") or f"{name}.error.txt" in names)


def _failed_items(settings: Settings) -> list[PatrolItem]:
    """失败包按来源目录报。"""
    root = settings.failed_dir
    if not root.is_dir():
        return []
    items = []
    for entry in sorted(os.scandir(root), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        count = len(_failed_packets(entry.path))
        if count == 0:
            continue
        if entry.name == UNKNOWN_DIR:
            message = f"failed/{UNKNOWN_DIR} 里有 {count} 个文件，有程序没按来源目录投"
            items.append(PatrolItem("failed_unknown", message, count=count))
        else:
            message = f"failed/{entry.name} 里有 {count} 个失败包"
            items.append(PatrolItem("failed_packets", message, source=entry.name, count=count))
    return items


def _failed_without_note(settings: Settings) -> list[PatrolItem]:
    """收件断在挪文件和写说明之间，失败包就没有说明。重投一次就知道原因了。"""
    root = settings.failed_dir
    if not root.is_dir():
        return []
    items = []
    for entry in sorted(os.scandir(root), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        for name in _failed_packets(entry.path):
            if not os.path.exists(os.path.join(entry.path, error_file_name(name))):
                message = f"failed/{entry.name}/{name} 没有说明，收件上次中断了，重投一次就知道原因"
                source = None if entry.name == UNKNOWN_DIR else entry.name
                items.append(PatrolItem("failed_without_note", message, source=source))
    return items


def _inbox_items(library: Library, registry: Registry) -> list[PatrolItem]:
    """来源目录里不会被收的东西，以及没登记的目录。"""
    root = library.inbox
    if not root.is_dir():
        return []
    items = []
    for entry in sorted(os.scandir(root), key=lambda item: item.name):
        if entry.is_file(follow_symlinks=False):
            continue  # 根目录的文件下次收件就拒收了
        where = f"{root.name}/{entry.name}"
        if entry.is_symlink() or entry.is_junction():
            items.append(PatrolItem("inbox_stray", f"{where} 是链接，不会被收", library=library.name))
        elif entry.name not in registry.sources:
            items.append(PatrolItem("inbox_unknown_dir", f"{where} 不在登记表里", library=library.name))
        else:
            for child in sorted(os.scandir(entry.path), key=lambda item: item.name):
                if child.is_dir(follow_symlinks=False):
                    if child.name == TMP_DIR:
                        continue
                elif child.is_file(follow_symlinks=False) and child.name.lower().endswith(".md"):
                    continue  # 下次收件就收了
                items.append(
                    PatrolItem(
                        "inbox_stray",
                        f"{where}/ 里有不会被收的东西：{child.name}",
                        library=library.name,
                        source=entry.name,
                    )
                )
    return items


def _stuck_items(settings: Settings, library: Library, registry: Registry, conn: sqlite3.Connection) -> list[PatrolItem]:
    """上一轮收件开始之前就在的文件，现在还在，说明它一直收不走（比如读不了、写库出错）。"""
    row = conn.execute("SELECT started_at FROM task_runs WHERE task = ?", (TASK_INTAKE,)).fetchone()
    started = parse_aware(row["started_at"]) if row is not None else None
    root = library.inbox
    if started is None or not root.is_dir():
        return []

    candidates: list[os.DirEntry] = []
    for entry in os.scandir(root):
        if entry.is_symlink() or entry.is_junction():
            continue
        if entry.is_file():
            candidates.append(entry)
        elif entry.is_dir() and not entry.name.startswith("."):
            registered = entry.name in registry.sources
            candidates.extend(
                child
                for child in os.scandir(entry.path)
                if child.is_file(follow_symlinks=False) and (not registered or child.name.lower().endswith(".md"))
            )

    data_dir = settings.require_data_dir()
    items = []
    for candidate in sorted(candidates, key=lambda item: item.path):
        if candidate.stat(follow_symlinks=False).st_mtime < epoch(started):
            relative = os.path.relpath(candidate.path, data_dir).replace(os.sep, "/")
            message = f"{relative} 上一轮收件之前就在了，还没收走，看收件的输出"
            items.append(PatrolItem("inbox_stuck", message, library=library.name))
    return items


def _stale_items(settings: Settings, library: Library, conn: sqlite3.Connection) -> list[PatrolItem]:
    row = conn.execute("SELECT finished_at FROM task_runs WHERE task = ?", (TASK_INTAKE,)).fetchone()
    finished = parse_aware(row["finished_at"]) if row is not None else None
    if finished is None:
        return [PatrolItem("task_stale", "收件还没跑完过一次", library=library.name)]
    if settings.now() - finished > timedelta(hours=settings.stale_hours):
        message = f"收件太久没跑了，上一次是 {iso(finished)}"
        return [PatrolItem("task_stale", message, library=library.name)]
    return []


_FAILURE_TEXT = {  # 写账记在 extraction_note 里的种类（pipeline.write._not_redone），巡检说给人听的
    "length": "截断",
    "refusal": "拒答",
    "too_long": "太长（超过模型能收的）",
    "content_rejected": "内容被挡",
    "format_error": "格式错",
    "unredacted": "打码没打干净",
    "too_big": "窗口太大",
    "private": "按规矩不发（private）",
}
_RETRYABLE = {"length", "refusal", "too_long", "content_rejected", "format_error"}  # --retry-failed 才重调的那几种
_REDONE = {"unredacted", "too_big"}  # 每次都重看的：改好了照常抽就发，--retry-failed 管不着
_RETRY = (
    "重投会被去重跳过：截断、拒答、格式错这类修好以后（提示词、模型设置）跑 javmem extract --retry-failed"
    "（它管这一整批，别的重调也没用的窗口也会重调一遍）"
)


def _failure_label(kind: str) -> str:
    if kind.startswith("recheck_"):
        inner = kind.removeprefix("recheck_")
        return "复核：" + ("事实那一段打码没打干净" if inner == "unredacted" else _FAILURE_TEXT.get(inner, "原因没记下"))
    return _FAILURE_TEXT.get(kind, "原因没记下的")


def extraction_message(packet_id: str, note: str | None) -> str:
    """抽取失败的包（写账标的 failed，排队修）：照 extraction_note 说是哪几种、各几段、怎么办。只有种类和段数。
    只在用得上的时候叫人加 --retry-failed（审阅第 4 条）。"""
    parts = []
    for piece in (note or "").split(";"):
        kind, _, count = piece.partition("=")
        if kind:
            parts.append((kind, int(count) if count.isdigit() else 1))
    if not parts:
        return f"{packet_id} 抽取失败，{_RETRY}"
    kinds = {kind for kind, _ in parts}
    rechecks = {kind.removeprefix("recheck_") for kind in kinds if kind.startswith("recheck_")}
    plain = {kind for kind in kinds if not kind.startswith("recheck_")}
    hints = []
    if plain - _REDONE - {"private"}:  # 截断、拒答这类，连同原因没记下的
        hints.append(_RETRY)
    if plain & _REDONE:
        hints.append("打码没打干净、太大的每次都重看：改好打码模式表、拆小以后照常跑 javmem extract，不用 --retry-failed")
    if rechecks - _REDONE:
        hints.append("复核截断、拒答这类修好以后跑 javmem extract --recheck --retry-failed-recheck")
    if rechecks & _REDONE:
        hints.append("复核那一段打码没打干净、太大的，改好以后照常跑 javmem extract --recheck")
    if "private" in plain:
        hints.append("按规矩不发的那几段不是出错：private 的内容不发给云上的模型，等有了本地模型再抽")
    said = "、".join(f"{_failure_label(kind)} {count} 段" for kind, count in parts)
    return f"{packet_id} {'没抽' if kinds == {'private'} else '抽取失败'}：{said}。{'；'.join(hints)}"


def _extraction_items(library: Library, conn: sqlite3.Connection) -> list[PatrolItem]:
    rows = conn.execute(
        "SELECT packet_id, revision, extraction_note FROM packets WHERE state = 'current' AND extraction = 'failed'"
        " AND withdrawn_at IS NULL ORDER BY packet_id"
    )
    return [
        PatrolItem(
            "extraction_failed",
            extraction_message(row["packet_id"], row["extraction_note"]),
            library=library.name,
            packet_id=row["packet_id"],
            revision=row["revision"],
        )
        for row in rows
    ]


def _gap_items(library: Library, conn: sqlite3.Connection) -> list[PatrolItem]:
    """同一个来源的 daily 包，时间段合起来有缺口。

    按权限拆开的包时间段相同不算问题。撤回了的也算覆盖过，那段内容是收到过的。
    """
    rows = conn.execute(
        "SELECT source, covers_from, covers_to, covers_from_epoch, covers_to_epoch FROM packets"
        " WHERE kind = 'daily' AND state = 'current' ORDER BY source, covers_from_epoch, covers_to_epoch"
    ).fetchall()
    acked = {row[0] for row in conn.execute("SELECT key FROM patrol_acks WHERE kind = 'daily_gap'")}
    items = []
    source = None
    reached = reached_text = None
    for row in rows:
        if row["source"] != source:
            source = row["source"]
            reached, reached_text = row["covers_to_epoch"], row["covers_to"]
            continue
        if row["covers_from_epoch"] > reached:
            key = gap_key(source, reached_text, row["covers_from"])
            if key not in acked:
                message = f"{source} 的 daily 包有缺口：{reached_text} 到 {row['covers_from']}"
                ref = f"{library.name}/G{hashlib.sha256(key.encode('utf-8')).hexdigest()[:10]}"
                items.append(PatrolItem("daily_gap", message, library=library.name, source=source, ref=ref))
        if row["covers_to_epoch"] >= reached:
            reached, reached_text = row["covers_to_epoch"], row["covers_to"]
    return items


def gap_key(source: str, start: str, end: str) -> str:
    """一个缺口的指纹：来源和起止。缺口变了（补进来一部分）就是另一条，重新报。"""
    return f"{source}|{start}|{end}"


def _warning_items(library: Library, conn: sqlite3.Connection) -> list[PatrolItem]:
    rows = conn.execute(
        "SELECT warning_id, kind, packet_id, revision, line, detail, seen_count FROM patrol_warnings"
        " WHERE resolved_at IS NULL AND acknowledged_at IS NULL ORDER BY packet_id, revision, line, kind"
    )
    items = []
    for row in rows:
        text = _WARNING_TEXT.get(row["kind"], row["kind"])
        where = row["packet_id"] or "（没有包号）"
        message = f"{where}：{text}"
        if row["line"]:
            message += f"，第 {row['line']} 行"
        if row["detail"]:
            message += f"，{row['detail']}"
        items.append(
            PatrolItem(
                row["kind"],
                message,
                library=library.name,
                packet_id=row["packet_id"] or None,
                revision=row["revision"] or None,
                line=row["line"] or None,
                count=row["seen_count"] if row["seen_count"] > 1 else None,
                ref=f"{library.name}/W{row['warning_id']}",
            )
        )
    return items


def _archive_items(settings: Settings, library: Library, conn: sqlite3.Connection) -> list[PatrolItem]:
    """对账归档和包登记：账上有的文件要在，归档里的文件账上要有。"""
    data_dir = settings.require_data_dir()
    items = []
    known = set()
    for row in conn.execute("SELECT packet_id, revision, archive_path, state, purged_at FROM packets"):
        known.add(row["archive_path"])
        if row["purged_at"] is not None:
            continue  # 彻底删除了的，归档本来就没了
        if row["state"] != "registered" and not (data_dir / row["archive_path"]).exists():
            items.append(
                PatrolItem(
                    "archive_missing",
                    f"{row['packet_id']} 第 {row['revision']} 版在账上，归档里没有",
                    library=library.name,
                    packet_id=row["packet_id"],
                    revision=row["revision"],
                )
            )
    stuck = conn.execute("SELECT packet_id, revision FROM packets WHERE state = 'registered' ORDER BY packet_id")
    for row in stuck:
        items.append(
            PatrolItem(
                "registration_stuck",
                f"{row['packet_id']} 登记了还没做完，下次收件会接着做",
                library=library.name,
                packet_id=row["packet_id"],
                revision=row["revision"],
            )
        )
    if library.archive.is_dir():
        skipped = library.archive / "skipped"
        for directory, subdirs, files in os.walk(library.archive):
            if directory == str(skipped):
                subdirs[:] = []
                continue
            for name in sorted(files):
                relative = os.path.relpath(os.path.join(directory, name), data_dir).replace(os.sep, "/")
                if name.lower().endswith(".md") and relative not in known and ".conflict" not in name:
                    items.append(
                        PatrolItem("archive_orphan", f"{relative} 在归档里，账上没有", library=library.name)
                    )
    return items


def _settled_items(settings: Settings, registry: Registry | None) -> list[PatrolItem]:
    """这些只在没有收件在跑时才算数：收到一半时，账上和目录本来就对不上。"""
    items = _failed_without_note(settings)
    for library in settings.libraries():
        if not library.db_path.exists():
            continue
        conn = open_ledger(library)
        try:
            items.extend(_archive_items(settings, library, conn))
            if registry is not None:
                items.extend(_stuck_items(settings, library, registry, conn))
        finally:
            conn.close()
    return items


class AckError(Exception):
    """要标的提醒不在了，或者不能标。什么都没动。"""


def acknowledge(settings: Settings, refs: Sequence[str] = (), kinds: Sequence[str] = ()) -> list[str]:
    """标“看过了”：refs 是巡检给的编号（库/W…、库/G…），kinds 是把这一种现在的都标上。返回标了的编号。

    编号对不上现在的提醒（已经没了、写错了），一个都不标，抛 AckError。
    """
    current = [item for item in run_patrol(settings) if item.ref is not None]
    by_ref = {item.ref: item for item in current}
    unknown = [ref for ref in refs if ref not in by_ref]
    if unknown:
        raise AckError(f"巡检里没有这些能标的提醒：{'、'.join(unknown)}")
    chosen = {ref: by_ref[ref] for ref in refs}
    for item in current:
        if item.kind in kinds:
            chosen[str(item.ref)] = item
    now = iso(settings.now())
    for library in settings.libraries():
        mine = [item for item in chosen.values() if item.library == library.name]
        if not mine:
            continue
        conn = open_ledger(library)
        try:
            with transaction(conn):
                for item in mine:
                    _, _, tail = str(item.ref).partition("/")
                    if tail.startswith("W"):
                        conn.execute(
                            "UPDATE patrol_warnings SET acknowledged_at = ? WHERE warning_id = ? AND acknowledged_at IS NULL",
                            (now, int(tail[1:])),
                        )
                    else:  # daily 包的缺口：指纹从这一条现算
                        start, _, end = item.message.partition("：")[2].partition(" 到 ")
                        conn.execute(
                            "INSERT OR IGNORE INTO patrol_acks (kind, key, acknowledged_at) VALUES ('daily_gap', ?, ?)",
                            (gap_key(str(item.source), start, end), now),
                        )
        finally:
            conn.close()
    return sorted(chosen)


def _lexicon_items(library: Library, conn: sqlite3.Connection, fingerprint: str | None) -> list[PatrolItem]:
    """别名表改了（人写的那份，或者合并记录多了一条没当场生效的），这本账还是按旧的写的：要整本重建才全都生效（J3.6）。"""
    used = ledger_lexicon(conn)
    if fingerprint is None or used is None or used == fingerprint[:16]:
        return []
    return [
        PatrolItem(
            "lexicon_changed",
            "别名表改了，这本账还是按旧的写的：新写的照新的对，以前写的要整本重建才改（合并工具当场合的不算）",
            library=library.name,
        )
    ]


# ---------- 越攒越多、攒多了写账会慢的（巡检加快慢检查，11-16）----------
# 现在都不慢，可会越攒越多（收件变慢报告第 4 节）：到了数就提醒，再修。数在 config.PatrolLimits。

# 事实最多的几组：只读 facts_by_group（只有组的编号），不回表——事实表每一行带着事实文字，回表就是把它整张读一遍。
# 写死 INDEXED BY：跑过 ANALYZE 也不会换成整张扫。撤回了的也算在里面（判新旧不看它们，可要分清得回表）
LARGE_GROUPS_SQL = (
    "SELECT group_key, count(*) AS facts FROM facts INDEXED BY facts_by_group WHERE group_key IS NOT NULL"
    " GROUP BY group_key HAVING count(*) > ?"
)


def _review_items(library: Library, conn: sqlite3.Connection, limits: PatrolLimits) -> list[PatrolItem]:
    """待确认太多：写账时每个实体、每条事实都要把待确认看一遍（有没有没处理的歧义、处理过没有），处理过的也在表里、也要看。"""
    total = conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]
    if total <= limits.review_items:
        return []
    waiting = conn.execute("SELECT count(*) FROM review_queue WHERE decided_at IS NULL").fetchone()[0]
    message = (
        f"待确认一共 {total:,} 条（没处理的 {waiting:,}，处理过的 {total - waiting:,}），超过 {limits.review_items:,} 条："
        "写账时每个实体、每条事实都要把待确认看一遍，多了写账会慢"
    )
    stats = {"total": total, "open": waiting, "decided": total - waiting, "limit": limits.review_items}
    return [PatrolItem("review_queue_large", message, library=library.name, stats=stats)]


def _group_items(library: Library, conn: sqlite3.Connection, limits: PatrolLimits) -> list[PatrolItem]:
    """一组太长：每往一组写进一条都要把整组重算一遍。只报组的主体（实体编号）和条数：属性可能就是原文里的字，不报。"""
    over = conn.execute(LARGE_GROUPS_SQL, (limits.group_facts,)).fetchall()
    if not over:
        return []
    largest = min(over, key=lambda row: (-row["facts"], row["group_key"]))
    row = conn.execute(
        "SELECT subject_entity_id FROM facts INDEXED BY facts_by_group WHERE group_key = ? LIMIT 1", (largest["group_key"],)
    ).fetchone()
    subject = row["subject_entity_id"] if row is not None else None
    message = (
        f"最大的一组有 {largest['facts']:,} 条事实（{f'主体 {subject}' if subject else '没有主体'}），"
        f"超过 {limits.group_facts:,} 条的有 {len(over)} 组："
        "每往一组写进一条都要把整组重算一遍，组越大写账越慢"
    )
    stats = {"facts": largest["facts"], "subject_entity_id": subject, "groups_over": len(over), "limit": limits.group_facts}
    return [PatrolItem("group_large", message, library=library.name, stats=stats)]


def _entity_items(library: Library, conn: sqlite3.Connection, limits: PatrolLimits) -> list[PatrolItem]:
    """人和事太多：几万个实体、别名时写账的快慢还没量过（收件变慢报告第 4 节第 6 条）。"""
    entities = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
    aliases = conn.execute("SELECT count(*) FROM aliases").fetchone()[0]
    which = [name for name, count in (("实体", entities), ("别名", aliases)) if count > limits.entities]
    if not which:
        return []
    message = (
        f"实体 {entities:,} 个、别名 {aliases:,} 个，{'和'.join(which)}超过 {limits.entities:,} 个："
        "这么多的时候写账的快慢还没量过，量一下"
    )
    stats = {"entities": entities, "aliases": aliases, "limit": limits.entities}
    return [PatrolItem("entities_many", message, library=library.name, stats=stats)]


def _ledger_bytes(db_path: Path) -> int:
    """账本连同 -wal 一共多大：写到一半、还没并回账本的都在 -wal 里。"""
    total = 0
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def _size_text(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _disk_items(library: Library, limits: PatrolLimits) -> list[PatrolItem]:
    """账本太大、磁盘快满：备份、升级、整本重建都要再放一份账本。"""
    size = _ledger_bytes(library.db_path)
    free = shutil.disk_usage(library.db_path.parent).free
    if free >= limits.disk_ratio * size:
        return []
    message = (
        f"账本 {_size_text(size)}（连同 -wal），所在的磁盘只剩 {_size_text(free)}，不到账本的 {limits.disk_ratio:g} 倍："
        "备份、升级、整本重建都要再放一份"
    )
    stats = {"ledger_bytes": size, "free_bytes": free, "limit": limits.disk_ratio}
    return [PatrolItem("disk_low", message, library=library.name, stats=stats)]


def _per_window(row: sqlite3.Row) -> float:
    """一次写账按窗口算的秒数，扣掉等向量服务的。"""
    return max(row["seconds"] - row["embedding_seconds"], 0.0) / row["windows"]


def _about_as_many(windows: int, other: int) -> bool:
    """窗口数差不多：other 不少于 windows 乘 2/3，也不多于 windows 乘 1.5（反过来也一样）。"""
    return 3 * other >= 2 * windows and 2 * other <= 3 * windows


def _write_speed_items(library: Library, conn: sqlite3.Connection, limits: PatrolLimits) -> list[PatrolItem]:
    """写账变慢（比法是 11-16 定的）：最近一次按窗口算，和前面窗口数差不多（2/3 到 1.5 倍）的那几次比，次数最多是
    limits.write_runs；是它们中间数的 limits.write_ratio 倍或更多就报，连同最近一次凑不够 limits.write_min_runs 次不报。

    - 按窗口算：一个包几个窗口不一样；留着排队的包，写了的窗口照样算；整包没窗口的包不写窗口，不算。
    - 只和窗口数差不多的比：每次写账都有一块和窗口数无关的固定活（补向量两遍、出汇总，跟着账本涨），窗口数差得多的
      两次，固定那块摊到每个窗口上不一样多。
    - 只看写了窗口、没停下的：向量服务调不通、停下了的，最后那一遍补向量没做，用的时间偏少。
    """
    cursor = conn.execute(
        "SELECT started_at, seconds, embedding_seconds, windows FROM write_runs WHERE stopped = 0 AND windows > 0"
        " ORDER BY run_id DESC"
    )
    try:
        latest = cursor.fetchone()
        if latest is None:
            return []
        before: list[float] = []
        for row in cursor:  # 从最近往前一行一行找，凑够了就停，不读整张表
            if len(before) >= limits.write_runs:
                break
            if _about_as_many(latest["windows"], row["windows"]):
                before.append(_per_window(row))
    finally:
        cursor.close()
    if 1 + len(before) < limits.write_min_runs or not before:  # not before：代码里直接造的怪数（比前 0 次）
        return []
    now, middle = _per_window(latest), statistics.median(before)
    if middle <= 0 or now < limits.write_ratio * middle:
        return []
    message = (
        f"写账变慢了：最近一次（{latest['started_at']}，{latest['windows']} 个窗口）每个窗口 {now:.2f} 秒，"
        f"是前 {len(before)} 次窗口数差不多的写账中间数 {middle:.2f} 秒的 {now / middle:.1f} 倍（等向量服务的时间不算）"
    )
    stats = {
        "started_at": latest["started_at"],
        "windows": latest["windows"],
        "seconds_per_window": round(now, 3),
        "median_seconds_per_window": round(middle, 3),
        "runs_compared": len(before),
        "ratio": round(now / middle, 2),
        "limit": limits.write_ratio,
    }
    return [PatrolItem("write_slower", message, library=library.name, stats=stats)]


GRAPH_WITHDRAWN_HOURS = 1.0  # 撤回、换版本以后欠着的（主图里还留着撤回了的东西）过多久就报


def _graph_items(settings: Settings, library: Library, conn: sqlite3.Connection) -> list[PatrolItem]:
    """主图欠着太久（J5.4，J5.1 第〇节第 2 题）：这本账接上了图（javmem graph init），欠着没同步的分两种报。只读账本
    （graph_state、graph_dirty、graph_items 和账本里的几张表），不连图；没接上图的账本不提醒。是现在的样子，同步了就不报了，
    不能标。
    - graph_withdrawn：撤回、换版本以后欠着的（主图里还留着撤回了的东西，javmem.graph.dirty.WITHDRAWN_CONDITION），最早
      那一条过了 1 小时就报，叫人跑 javmem graph sync（主会话 11-18 照 J5.6 的建议定的）。这一阵 memory_search 也不查主图。
    - graph_behind：别的欠着的，最早那一条超过 stale_hours（和“太久没收包”同一个数）才报：图落后了，从主图答的会是旧的。"""
    try:
        state = conn.execute("SELECT enabled FROM graph_state WHERE id = 1").fetchone()
        if state is None or not state[0]:
            return []
        groups = conn.execute(
            f"SELECT CASE WHEN {WITHDRAWN_CONDITION} THEN 1 ELSE 0 END AS withdrawn, count(*), min(d.since)"
            " FROM graph_dirty AS d GROUP BY withdrawn"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # 账本还没有这几张表（还没升到第 26 版）
    items: list[PatrolItem] = []
    for withdrawn, count, oldest in sorted(groups, key=lambda row: -row[0]):
        since = parse_aware(oldest) if count else None
        if since is None:
            continue
        hours = (settings.now() - since).total_seconds() / 3600
        if withdrawn:
            if hours <= GRAPH_WITHDRAWN_HOURS:
                continue
            message = (f"撤回了的还在主图里：撤回、换版本以后有 {count} 条没同步，最早的是 {int(hours)} 小时前，"
                       f"跑 javmem graph sync --library {library.name}")
            items.append(PatrolItem("graph_withdrawn", message, library=library.name, count=int(count)))
        elif hours > settings.stale_hours:
            message = (f"主图欠着 {count} 条没同步，最早的是 {int(hours)} 小时前，看 javmem graph status --library {library.name}"
                       "（图落后了，从主图答的会是旧的）")
            items.append(PatrolItem("graph_behind", message, library=library.name, count=int(count)))
    return items


BACKUP_STATUS_FILE = "backup-status.json"  # 数据目录 logs/ 下，javmem backup 每一次记一笔（U2 丙）
_BACKUP_ERRORS = {
    "busy": "拿不到锁（收件、写账、抽取有一个一直在跑，或者另一个备份在用这个暂存目录）",
    "integrity": "有库完整性检查没过",
    "link": "数据目录里有链接、联接（没跟进去，没备）",
    "config": "有配置文件找不到",
    "export": "撤回清单、主人的决定导不出来",
    "push_failed": "推异地没成功",
    "error": "跑到一半出错",
}


def _backup_items(settings: Settings) -> list[PatrolItem]:
    """备份太久没成功（U2 丙，做法第 4 节第 7 条）：javmem backup 每一次在 logs/backup-status.json 记一笔。备份过的数据目录，
    上一次成功超过 stale_hours（和“太久没收包”同一个数，默认 26 小时）就报，说上一次没成功是为什么（只有种类）；推过异地的，
    上一次推成超过 stale_hours 也报（后来的备份都只暂存、没推：多半是按时跑的那一行漏了 --push）。没备份过的（没有这个文件）
    不报：测试、开发用的数据目录不用备。是现在的样子，备好了就不报，不能标。"""
    path = settings.logs_dir / BACKUP_STATUS_FILE
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        status = None
    if not isinstance(status, dict):
        return [PatrolItem("backup_stale", f"logs/{BACKUP_STATUS_FILE} 读不了，看不出备份成没成：再跑一次 javmem backup")]
    ok = parse_aware(status.get("last_ok_at"))
    limit = timedelta(hours=settings.stale_hours)
    if ok is not None and settings.now() - ok <= limit:
        pushed = parse_aware(status.get("last_pushed_at"))
        if pushed is None or settings.now() - pushed <= limit:
            return []
        hours = int((settings.now() - pushed).total_seconds() // 3600)
        message = (f"推异地 {hours} 小时没成功了，上一次推成是 {iso(pushed)}；后来的备份只暂存了，"
                   "看按时跑的那一行带没带 --push")
        stats = {"last_ok_at": status.get("last_ok_at"), "last_pushed_at": status.get("last_pushed_at")}
        return [PatrolItem("backup_stale", message, stats=stats)]
    if ok is None:
        message = "备份还没成功过一次"
    else:
        message = f"备份 {int((settings.now() - ok).total_seconds() // 3600)} 小时没成功了，上一次成功是 {iso(ok)}"
    attempt, error = parse_aware(status.get("last_attempt_at")), status.get("last_error")
    if attempt is not None and error:
        message += f"；最近一次（{iso(attempt)}）没成功：{_BACKUP_ERRORS.get(str(error), '原因没记下')}"
    stats = {"last_ok_at": status.get("last_ok_at"), "last_attempt_at": status.get("last_attempt_at"), "last_error": error}
    return [PatrolItem("backup_stale", message + "，看 javmem backup 的输出", stats=stats)]


# ---------- 模型、版本变了（巡检报模型、版本变了，D205 ⑪）----------
# 都只读：数据目录那一份基准（logs/versions-baseline.json，javmem versions --save 不写文件名存的）、--check 记的那一笔
# （logs/versions-status.json）、结果库每张表的最后几行、这个进程的两个地址变量、账本的规则表。不连网、不调模型。

VERSIONS_KINDS = ("served_model_changed", "probe_changed", "probe_stale")  # 和基准比的那几种：没基准、基准读不了时这回没看
SERVED_TABLES = ("results", "rechecks", "judgments")  # L3 结果库里记着服务商给的名字的三张表；软变化的在 soft.db 的 verdicts
SERVED_ROWS = 200  # 每张表照写进去的先后倒着取最后几行：不扫表，大结果库上也是一下子
_OLDEST = datetime(1, 1, 1, tzinfo=timezone.utc)


def _versions_baseline(settings: Settings) -> tuple[dict[str, object] | None, str, str | None]:
    """数据目录那一份基准：(基准, 文件的指纹, 读不了的原因)。没存过的是 (None, "", None)。指纹照文件的字节算
    （versions.baseline_digest）：--check 记下比的是哪一份，巡检照它认，重新存了一份不一样的就不算。"""
    path = settings.logs_dir / BASELINE_FILE
    try:
        raw = path.read_bytes()
        saved = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return None, "", None
    except (OSError, ValueError) as error:
        return None, "", type(error).__name__
    if not isinstance(saved, dict) or saved.get("format") != VERSIONS_FORMAT:
        return None, "", "不是 javmem versions --save 存的这一版格式"
    if parse_aware(saved.get("at")) is None:
        return None, "", "没写存的时刻"
    return saved, baseline_digest(raw), None


def _baseline_served(saved: dict[str, object]) -> dict[str, tuple[str, datetime]]:
    """基准里每个要的模型：存的时候服务商最近一次给的名字，和那一次的时刻（读不了的算最早）。"""
    from javmem.usage import clean_model

    served = saved.get("served")
    models = served.get("models") if isinstance(served, dict) else None
    if not isinstance(models, dict):
        return {}
    out = {}
    for model, one in models.items():
        name = clean_model(one.get("name")) if isinstance(one, dict) else ""
        if name:
            out[str(model)] = (name, parse_aware(one.get("at")) or _OLDEST)
    return out


@dataclass(frozen=True)
class _Served:
    """结果库里的一行：服务商给的名字、写进去的时刻（原样的、解析了的）、哪个库哪张表。"""

    name: str
    at: str
    moment: datetime
    where: str


def _recent_served(settings: Settings) -> tuple[dict[str, list[_Served]], bool, list[str]]:
    """数据目录的结果库（l3.db 那三张表）、soft.db（verdicts），每张表倒着取最后 SERVED_ROWS 行，每个要的模型照读到的先后
    （从最后写进去的往前）列出来。(每个模型的, 有没有一张表读到了, 在却一张表都读不了的库)。只读打开。"""
    from javmem.usage import clean_model

    found: dict[str, list[_Served]] = {}
    read = False
    unreadable: list[str] = []
    l3_store, soft_store = data_stores(settings.require_data_dir())
    for path, tables in ((l3_store, SERVED_TABLES), (soft_store, ("verdicts",))):
        if not path.exists():
            continue
        ok = False
        try:
            conn = open_readonly(path)
        except sqlite3.Error:
            unreadable.append(path.name)
            continue
        try:
            for table in tables:
                try:
                    rows = conn.execute(
                        f"SELECT model, response_model, created_at FROM {table} ORDER BY rowid DESC LIMIT ?",  # noqa: S608 - 表名是写死的
                        (SERVED_ROWS,),
                    ).fetchall()
                except sqlite3.Error:
                    continue
                ok = True
                for row in rows:
                    name = clean_model(row["response_model"])
                    if row["model"] and name:
                        found.setdefault(str(row["model"]), []).append(
                            _Served(name, str(row["created_at"]), parse_aware(row["created_at"]) or _OLDEST, f"{path.name}:{table}"))
        finally:
            conn.close()
        if ok:
            read = True
        else:
            unreadable.append(path.name)
    return found, read, unreadable


def _served_items(saved: dict[str, object], recent: dict[str, list[_Served]]) -> list[PatrolItem]:
    """一个要的模型，存基准以后写进去的（时刻比基准里那一次晚的）里最近一次服务商给的名字，和基准里的不一样：一个模型一条。
    存基准那时就有的不比：基准里最近的名字可能来自 token 账（别处的调用也记在这本账上），同一秒里也可能回过两个名字，
    拿那时候的比会白报，重新存基准也消不掉。"""
    items = []
    for model, (before, since) in sorted(_baseline_served(saved).items()):
        newer = [one for one in recent.get(model, []) if one.moment > since]
        if not newer:
            continue
        now = max(newer, key=lambda one: one.moment)  # 时刻一样的，先读到的（后写进去的）算最近
        if now.name == before:
            continue
        message = (f"要 {model}，基准里服务商给的是 {before}，最近一次（{now.at}，{now.where}）给的是 {now.name}："
                   "照“一次只改一项”跑一遍回归，认了以后重新 javmem versions --save")
        stats: dict[str, object] = {"model": model, "baseline": before, "latest": now.name, "at": now.at, "from": now.where}
        items.append(PatrolItem("served_model_changed", message, stats=stats))
    return items


PROBE_STALE_DAYS = 8  # 探针每周查一次（D205 ⑪），宽一天
_PROBE_LABEL = {"embedding": "向量", "qwen": "本机 Qwen"}
_CHECK_COMMAND = "javmem versions --check --only probes,served,models"


def _probe_check(settings: Settings, digest: str) -> tuple[dict[str, object] | None, bool]:
    """javmem versions --check 记的那一笔里，最近一回和这一份基准（照文件的指纹认）比探针的那一条：(那一条, 记的那一笔读不了)。
    没记过、后来重新存过一份不一样的基准（比的是旧的那一份）的是 (None, False)。"""
    try:
        status = json.loads((settings.logs_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True
    if not isinstance(status, dict) or status.get("format") != STATUS_FORMAT:
        return None, True
    sections = status.get("sections")
    entry = sections.get("probes") if isinstance(sections, dict) else None
    if not isinstance(entry, dict) or entry.get("baseline_sha256") != digest or parse_aware(entry.get("at")) is None:
        return None, False
    return entry, False


def _probe_keys(value: object) -> list[str]:
    """记的那一笔里探针的名单：只认写成一串名字的、认得的那几个，写得不对的当没有。"""
    return [key for key in value if isinstance(key, str) and key in _PROBE_LABEL] if isinstance(value, list) else []


def _probe_items(settings: Settings, saved: dict[str, object], digest: str) -> tuple[list[PatrolItem], list[PatrolItem]]:
    """探针（D205 ⑪ 定的是每周一次）：巡检不跑，只读上一回 --check 记的。(要看的, 这回没看的)。查出不一样（没量到的也算）、
    基准里没量到比不了、两边都没量到而这次是出错的、基准是旧版探针存的（要重存，不说成变了，D229 ⑫），报 probe_changed；
    探针超过 PROBE_STALE_DAYS 天没量（从存基准、上一回 --check 比探针里晚的那个算）报 probe_stale。上一回本机 Qwen 一直有
    别人在用、这回没量到的（D229 ⑫：别当成变了），记一条 checks_skipped（probe_busy）。"""
    entry, broken = _probe_check(settings, digest)
    if broken:
        return [PatrolItem("probe_stale", f"logs/{STATUS_FILE} 读不了，看不出探针上一回查得怎么样：再跑一次 {_CHECK_COMMAND}")], []
    items = []
    skipped = []
    if entry is not None:
        busy = _probe_keys(entry.get("busy"))
        if busy:
            skipped.append(_skipped("probe_busy", ("probe_changed",),
                                    f"上一次 javmem versions --check（{entry['at']}）{'、'.join(_PROBE_LABEL[key] for key in busy)} "
                                    f"一直有别人在用，探针这回没量到（不算变了；空的时候再跑 {_CHECK_COMMAND}），这回没看"))
        detail = entry.get("probes") if isinstance(entry.get("probes"), dict) else {}
        counts = {str(key): value for key, value in detail.items()  # type: ignore[union-attr]
                  if type(value) is int and value > 0}
        different = entry["different"] if type(entry.get("different")) is int else sum(counts.values())
        missing, unmeasured = _probe_keys(entry.get("no_baseline")), _probe_keys(entry.get("unmeasured"))
        old = _probe_keys(entry.get("old_version"))
        parts = []
        if different:
            each = "、".join(f"{_PROBE_LABEL.get(key, key)} {value}" for key, value in counts.items())
            parts.append(f"有 {different} 处不一样（{each + '；' if each else ''}没量到的也算：说不清变没变）")
        if missing:
            parts.append(f"基准里{'、'.join(_PROBE_LABEL[key] for key in missing)} 没量到，比不了")
        if unmeasured:
            parts.append(f"{'、'.join(_PROBE_LABEL[key] for key in unmeasured)} 两边都没量到（这次出错），说不清变没变")
        if old:  # 换了探针（pq1 → pq2 这样）：比不了，要重存，不说成变了
            parts.append(f"基准里{'、'.join(_PROBE_LABEL[key] for key in old)} 是旧版探针存的，要重存")
        if parts:
            if different:
                advice = "看 --check 的输出，照“一次只改一项”查，认了以后重新 javmem versions --save"
            elif missing or unmeasured:
                advice = "本机服务开着的时候再 --check；基准里没有的，开着的时候重新 javmem versions --save"
            else:
                advice = "本机服务开着、Qwen 空着的时候重新 javmem versions --save"
            message = f"上一次 javmem versions --check（{entry['at']}）探针{'；'.join(parts)}：{advice}"
            stats: dict[str, object] = {"checked_at": entry["at"], "baseline_at": saved["at"], "different": different,
                                        "probes": detail, "no_baseline": missing, "unmeasured": unmeasured,
                                        "old_version": old}
            items.append(PatrolItem("probe_changed", message, stats=stats))
    saved_at = parse_aware(saved["at"])
    checked_at = parse_aware(entry["at"]) if entry is not None else None
    last = max(saved_at, checked_at) if checked_at is not None else saved_at  # type: ignore[type-var]
    days = (settings.now() - last).total_seconds() / 86400  # type: ignore[operator]
    if days > PROBE_STALE_DAYS:
        if entry is not None:
            message = f"版本和探针 {int(days)} 天没查了（上一次 --check 比探针是 {entry['at']}）：跑 {_CHECK_COMMAND}"
        else:
            message = f"存了基准（{saved['at']}）以后 {int(days)} 天没 --check 过探针：跑 {_CHECK_COMMAND}"
        stats = {"last_check_at": entry["at"] if entry is not None else None, "baseline_at": saved["at"], "days": int(days)}
        items.append(PatrolItem("probe_stale", message, stats=stats))
    return items, skipped


def _endpoint_items(env: Mapping[str, str]) -> tuple[list[PatrolItem], list[PatrolItem]]:
    """模型地址不是官方的（11-17 中转站那次以后的规矩：跑真的以前要是官方的）。结果库每一回抽取记下的选项里没有地址，只能照
    这个进程的环境变量看：只读地址那一个变量（JAVMEM_LLM_BASE_URL、JAVMEM_SOFT_BASE_URL，没写的是官方的默认地址，和抽取
    读的同一个），别的设置写错了不影响它，也不会让巡检崩。巡检和抽取要在同一套环境里跑。只写主机名。地址写得看不出主机名的，
    记一条 checks_skipped（model_settings_unreadable）。"""
    from javmem import llm
    from javmem.soft import claude

    items: list[PatrolItem] = []
    broken: list[str] = []
    for name, label, variable, default, official in (
        ("l3", "L3", llm.ENV_BASE_URL, llm.DEFAULT_BASE_URL, OFFICIAL_LLM),
        ("soft", "软变化", claude.ENV_BASE_URL, claude.DEFAULT_BASE_URL, OFFICIAL_SOFT),
    ):
        url = str(env.get(variable) or "").strip() or default
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if not host:
            broken.append(label)
        elif not is_official(url, official):
            message = f"{label} 的模型地址不是官方的（{host}）：跑真的以前改回官方的（11-17 中转站那次以后的规矩）"
            items.append(PatrolItem("endpoint_unofficial", message, stats={"which": name, "host": host}))
    skipped = [_skipped("model_settings_unreadable", ("endpoint_unofficial",),
                        f"{'、'.join(broken)} 的模型地址写错了（看不出主机名），这回没看它是不是官方的")] if broken else []
    return items, skipped


def _rule_layer(layer: str, version: str) -> str:
    """规则表里一行算哪一层：照层和版本号去掉末尾数字的前缀分。关系的规则（R1 到 R6、判断 C）跟着 L4 的版本号登记，
    进图前检查另登记一行 C、版本是它的提示词（f1）：同一个规则号两版是本来就有的，各算各的。"""
    family = version.rstrip("0123456789")
    if family == f"{layer.lower()}-v":
        return layer
    if layer == "L4" and family == "f":
        return "进图前检查"
    return f"{layer} {family}…"


def _rules_mixed_items(library: Library, conn: sqlite3.Connection) -> list[PatrolItem]:
    """几版规则混着写（D205 ⑪）：一本账里同一层规则登记了两版以上（javmem write 每一回都登记现在的版本），或者现行的包是
    两版以上的 L1 认的（quote_roles）。旧的照旧规矩，整本重建才都照新的：规则升了版当晚重建（重建不花钱），这一条就是提醒。
    别名表的指纹不算（那是 lexicon_changed）。一本账一条；是现在的样子，整本重建了就不报，不能标。只读规则表、一个包一行的
    quote_roles，不碰事实表。"""
    layers: dict[str, dict[str, str]] = {}
    for row in conn.execute("SELECT layer, version, min(created_at) AS since FROM rules WHERE rule_id != ?"
                            " GROUP BY layer, version", (LEXICON_RULE,)):
        layers.setdefault(_rule_layer(str(row["layer"]), str(row["version"])), {})[str(row["version"])] = str(row["since"])
    rules = {layer: dict(sorted(found.items())) for layer, found in sorted(layers.items()) if len(found) > 1}
    l1 = {str(row["version"]): int(row["packets"]) for row in conn.execute(
        "SELECT r.l1_version AS version, count(*) AS packets FROM quote_roles AS r"
        " JOIN packets AS p ON p.packet_id = r.packet_id AND p.revision = r.revision"
        " WHERE p.state = 'current' AND p.withdrawn_at IS NULL GROUP BY r.l1_version ORDER BY r.l1_version")}
    parts = [f"{layer} 是 " + "、".join(f"{version}（从 {since} 起）" for version, since in found.items())
             for layer, found in rules.items()]
    if len(l1) > 1:
        how_many = "两" if len(l1) == 2 else f" {len(l1)} "
        parts.append(f"包是{how_many}版 L1 认的（" + "、".join(f"{version} {count} 个" for version, count in l1.items()) + "）")
    if not parts:
        return []
    stats: dict[str, object] = {}
    if rules:
        stats["rules"] = {layer: list(found) for layer, found in rules.items()}
    if len(l1) > 1:
        stats["l1"] = l1
    message = f"这本账里几版规则混着写：{'；'.join(parts)}：旧的照旧规矩，整本重建才都照新的（重建不花钱）"
    return [PatrolItem("rules_mixed", message, library=library.name, stats=stats)]


def _versions_items(settings: Settings, env: Mapping[str, str]) -> tuple[list[PatrolItem], list[PatrolItem]]:
    """模型、版本变了的那几种（rules_mixed 分库，在每本账那里）：(要看的, 这回没看的)。数据目录一级的，不分库；是现在的样子，
    处理掉就不报，不能标。"""
    items: list[PatrolItem] = []
    skipped: list[PatrolItem] = []
    saved, digest, broken = _versions_baseline(settings)
    if broken is not None:
        items.append(PatrolItem("versions_unreadable", f"logs/{BASELINE_FILE} 读不了（{broken}）：重新 javmem versions --save"))
        skipped.append(_skipped("versions_unreadable", VERSIONS_KINDS, "版本的基准读不了，和基准比的这几样这回没看"))
    elif saved is None:  # 测试、开发用的数据目录不用存基准
        skipped.append(_skipped("no_versions_baseline", VERSIONS_KINDS,
                                f"没存过版本的基准（logs/{BASELINE_FILE}；要盯的数据目录跑一次 javmem versions --save），"
                                "和基准比的这几样这回没看"))
    else:
        recent, read, unreadable = _recent_served(settings)
        if _baseline_served(saved) and (unreadable or not read):  # 不能当“模型名没变”
            if unreadable and read:
                why = f"{'、'.join(unreadable)} 读不了，这回服务商实际给的模型名没看全（读得了的照看）"
            else:
                why = f"{'、'.join(unreadable) or '结果库'} 读不了（或者还没有），这回没看服务商实际给的模型名"
            skipped.append(_skipped("stores_unreadable", ("served_model_changed",), why))
        items.extend(_served_items(saved, recent))
        probe_items, probe_skipped = _probe_items(settings, saved, digest)
        items.extend(probe_items)
        skipped.extend(probe_skipped)
    endpoint_items, endpoint_skipped = _endpoint_items(env)  # 地址不和基准比：没存基准也照查
    return [*items, *endpoint_items], [*skipped, *endpoint_skipped]


def _growth_items(settings: Settings, library: Library, conn: sqlite3.Connection) -> list[PatrolItem]:
    limits = settings.patrol
    return [
        *_review_items(library, conn, limits),
        *_group_items(library, conn, limits),
        *_entity_items(library, conn, limits),
        *_disk_items(library, limits),
        *_write_speed_items(library, conn, limits),
    ]


class _TimedEmbedder(Embedder):
    """交给 run_write 的向量服务：每次调用（连同出错的）用了多久，记到 WriteTimer 的 waiting 上。"""

    def __init__(self, inner: Embedder, timer: WriteTimer) -> None:
        self.name = inner.name
        self._inner = inner
        self._timer = timer

    def embed(self, texts: Sequence[str]) -> EmbeddingReply:
        started = self._timer.clock()
        try:
            return self._inner.embed(texts)
        finally:
            self._timer.waiting += self._timer.clock() - started


class WriteTimer:
    """javmem write 记一笔给巡检：从拿到写账锁起用了多久，其中等向量服务多久。

    交给 run_write 的向量服务用 timed() 包一层、退避用 sleep：调用（连同出错的）、重试前等的都算等向量服务，比快慢时
    扣掉——真实写账大半时间在等向量服务，它快慢和账本多大无关。clock、sleep 是测试用的。"""

    def __init__(
        self, started_at: str, *, clock: Callable[[], float] = time.perf_counter, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.started_at = started_at
        self.clock = clock
        self.waiting = 0.0
        self._sleep = sleep
        self._started = clock()

    def elapsed(self) -> float:
        return self.clock() - self._started

    def timed(self, embedder: Embedder) -> Embedder:
        return _TimedEmbedder(embedder, self)

    def sleep(self, seconds: float) -> None:
        started = self.clock()
        try:
            self._sleep(seconds)
        finally:
            self.waiting += self.clock() - started

    def finish(
        self, conn: sqlite3.Connection, run: WriteRun, *, facts: int, warn: Callable[[str], None] | None = None
    ) -> bool:
        """写完、出完汇总以后记一行（record_write_run）。返回记了没有。

        记下的窗口数是这一次过了一遍的：写了的，加上同样的结果写过、这次跳过的（跳过的也要取包、切窗、核对一遍，
        时间都算在里面；不算进去的话，补抽以后那一次、有包卡着时的定时写账按窗口算会显得慢）。记不记还是看写了窗口、
        写完了包没有：只跳过了写过的不记。

        记不上（账本一时锁住、磁盘满）不算写账出错：账已经写完了，退出码、汇总照旧，只交给 warn 说一句。"""
        stats = run.stats
        if stats.windows <= 0 and stats.packets_done <= 0:
            return False
        seconds = self.elapsed()
        try:
            return record_write_run(
                conn,
                started_at=self.started_at,
                seconds=seconds,
                embedding_seconds=min(self.waiting, seconds),
                packets=stats.packets_done,
                windows=stats.windows + stats.windows_skipped,
                facts=facts,
                stopped=run.stopped is not None,
            )
        except sqlite3.Error as error:
            if warn is not None:
                warn(f"写账的快慢没记上（{type(error).__name__}：{error}），账照写了；巡检比写账快慢时少这一次")
            return False


def record_write_run(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    seconds: float,
    embedding_seconds: float,
    packets: int,
    windows: int,
    facts: int,
    stopped: bool,
) -> bool:
    """写账写完记一行（write_runs，迁移 0021）。一个包、一个窗口都没写的（只跳过了写过的、包都留着排队的也算）不记：
    没什么可量的。自己开一个事务。返回记了没有。"""
    if packets <= 0 and windows <= 0:
        return False
    with transaction(conn):
        conn.execute(
            "INSERT INTO write_runs (started_at, seconds, embedding_seconds, packets, windows, facts, stopped)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (started_at, seconds, embedding_seconds, packets, windows, facts, int(stopped)),
        )
    return True


def run_patrol(settings: Settings) -> list[PatrolItem]:
    """要看的那几条（--ack 和别处用这个）。这一回没看的那几种在 patrol_report 的 skipped 里。"""
    return patrol_report(settings).items


def patrol_report(settings: Settings, env: Mapping[str, str] | None = None) -> PatrolReport:
    """查一回：要看的，连同这一回没看的那几种（每个原因一条 checks_skipped）。env 是看模型地址用的环境变量，不给就是
    这个进程的（和抽取读的同一套）。"""
    data_dir = settings.require_data_dir()
    items = _failed_items(settings)
    items.extend(_backup_items(settings))
    version_items, version_skipped = _versions_items(settings, os.environ if env is None else env)
    items.extend(version_items)
    skipped: list[PatrolItem] = []
    try:
        registry: Registry | None = load_registry(settings.sources_file)
    except ConfigError as error:
        registry = None
        items.append(PatrolItem("sources_unreadable", f"来源登记表有问题：{error}"))
        skipped.append(_skipped("sources_unreadable", INBOX_KINDS, "来源登记表读不了，收件目录那几样这回没看"))
    fingerprint: str | None = None
    try:
        fingerprint = load_lexicon(settings.lexicon_file).fingerprint if settings.lexicon_file is not None else None
    except ConfigError as error:
        items.append(PatrolItem("lexicon_unreadable", f"别名表有问题：{error}"))
        skipped.append(_skipped("lexicon_unreadable", LEXICON_KINDS, "别名表读不了，这回没看别名表改没改"))

    for library in settings.libraries():
        if registry is not None:
            items.extend(_inbox_items(library, registry))
        if not library.db_path.exists():
            items.append(PatrolItem("task_stale", "收件还没跑完过一次", library=library.name))
            continue
        conn = open_ledger(library)
        try:
            items.extend(_stale_items(settings, library, conn))
            items.extend(_extraction_items(library, conn))
            items.extend(_gap_items(library, conn))
            items.extend(_warning_items(library, conn))
            items.extend(_lexicon_items(library, conn, fingerprint))
            items.extend(_rules_mixed_items(library, conn))
            items.extend(_graph_items(settings, library, conn))
            items.extend(_growth_items(settings, library, conn))
        finally:
            conn.close()

    if data_dir.is_dir():
        try:
            with intake_lock(data_dir):
                items.extend(_settled_items(settings, registry))
        except IntakeBusy:  # 收件正在跑：收到一半时账上和目录本来就对不上，这几项下次再看，不误报；说一声这回没看
            # 撤回、合并、处理待确认这类也拿收件锁，不光是收件
            skipped.append(_skipped("intake_busy", SETTLED_KINDS, "收件锁被占着（收件、撤回、合并这类正在跑），这几样这回没看，下次再看"))
    skipped.extend(version_skipped)  # 模型、版本那几样没看的跟在最后
    return PatrolReport(items, skipped)
