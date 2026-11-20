"""第二证人的矛盾进待确认（J6.2）：比对出来的两种真矛盾（另一头不一样、一边结束了）写进账本的待确认，种类
`witness_mismatch`（迁移 0001 就有）。撤回、彻底删除、支线的每一遍扫都按账本的现状清（javmem.side.hook、javmem.side.cli）。

- 一条一行：`claims` 写哪个证人、哪一种、哪个包、哪种关系、两边各是哪几条和两头的名字、状态，加一个 `key`（这几样
  算出来的指纹）。主线那一边 `relations` 写账本里的关系编号、`claims` 写说法编号（打分的读法一处说法一条）；支线那一边
  `relations` 写支线导出里的编号。`quotes` 写证据的句子编号（照 L5 进待确认的写法，不抄原话）。
- 同一个 `key` 已经有一行的（开着的、定过的都算）不再进：同样的两份结果比几遍，待确认里只有一行。
- 不算数了的（drop_stale，条件见 stale_condition）：包不是现行的了（撤回了、彻底删除了），或者现行的不是比的时候那一版
  （`claims.revision`；句子编号按位置编，换了版本的包常常还是同样的编号，只看句子认不出；以前的写法没记版本的也算）；
  证据里有撤回了、找不到的句子（主线、支线两边的证据都在里面：主线的关系撤回了，它的证据句撤回了，也算）；进了待确认
  以后包里又有句子撤回了（体检库里那一组作废了，名字可能就出自那一句）。主线那一边写的关系编号对不上不算：关系照常会
  换编号（分段重算），这一种待确认里的不跟着改。这一种待确认不会自己过期，所以撤回、彻底删除、支线的每一遍扫都按账本的现状删一遍，
  哪一遍没删成，下一遍照样删得掉（不靠哪一遍记着删了哪几组）。删的时候 `secure_delete`：删了的字抹成 0，彻底删除以后
  再把 WAL 写回去截掉（checkpoint），文件里不留。
- 写账本照 review.py：先拿收件锁、写账锁（ledger_locks，都不等），账本被别的程序锁着的最多等 LEDGER_TIMEOUT 秒，
  拿不到就报 LedgerBusy（撤回已经做完了，这一步没做成只提示，下一遍再清）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from javmem.intake import IntakeBusy, intake_lock
from javmem.ledger import connect_readonly
from javmem.pipeline.write import WriteBusy, WriteLock
from javmem.scoring.common import Relation, Side
from javmem.side.compare import Comparison, Mismatch

KIND = "witness_mismatch"
LEDGER_TIMEOUT = 5.0  # 秒：账本被别的程序（不是 javmem 的写账、收件，它们有锁）锁着时最多等这么久
_CHUNK = 400

# 不算数了的第二证人待确认：条件见 stale_condition。claims、quotes 写坏了（不是 JSON）的也算
_CLAIMS = "(CASE WHEN json_valid(review_queue.claims) THEN review_queue.claims END)"
_QUOTES = "(CASE WHEN json_valid(review_queue.quotes) THEN review_queue.quotes ELSE '[]' END)"
_LIVE_SENTENCE = (
    "SELECT 1 FROM raw_sentences s JOIN packets p ON p.packet_id = s.packet_id AND p.revision = s.revision"
    " WHERE s.sentence_id = q.value AND s.withdrawn_at IS NULL AND p.state = 'current' AND p.withdrawn_at IS NULL"
)


def stale_condition(conn: sqlite3.Connection) -> str:
    """不算数了的第二证人待确认（见模块说明），写成 review_queue 的 WHERE。账本还没有关系表的，不看主线的关系。"""
    parts = [
        "NOT json_valid(review_queue.claims)",
        "NOT json_valid(review_queue.quotes)",
        "NOT EXISTS (SELECT 1 FROM packets p WHERE p.packet_id = json_extract(" + _CLAIMS + ", '$.packet')"
        " AND p.revision = json_extract(" + _CLAIMS + ", '$.revision') AND p.state = 'current' AND p.withdrawn_at IS NULL)",
        f"EXISTS (SELECT 1 FROM json_each({_QUOTES}) q WHERE NOT EXISTS ({_LIVE_SENTENCE}))",
        "EXISTS (SELECT 1 FROM raw_sentences s WHERE s.packet_id = json_extract(" + _CLAIMS + ", '$.packet')"
        " AND s.withdrawn_at IS NOT NULL AND julianday(s.withdrawn_at) >= julianday(review_queue.created_at))",
    ]
    # 主线那一边的关系编号对不上不算：关系照常会换编号（分段重算），这一种待确认里写的编号不跟着改；撤回了的关系，
    # 它的证据句撤回了，quotes 那一条就清了
    return f"kind = '{KIND}' AND (" + " OR ".join(parts) + ")"


class LedgerBusy(Exception):
    """写账、收件在跑，或者账本被别的程序锁着：这一回没写成、没清成。"""


class Queued(NamedTuple):
    added: int  # 新进了几条
    stale: int  # 没进的：包不是现行的、支线导出的不是现行的那一版（或者没写是哪一版）、证据里有撤回了、找不到的句子


@contextmanager
def ledger_locks(data_dir: Path) -> Iterator[None]:
    """照 review.py：动账本之前拿收件锁和写账锁，都不等，拿不到报 LedgerBusy。读主线（javmem.side.mainline）也拿着，
    读的那两步中间没人写得进来。"""
    try:
        with intake_lock(data_dir), WriteLock(data_dir):
            yield
    except (IntakeBusy, WriteBusy):
        raise LedgerBusy("写账、收件在跑") from None


def _open(ledger: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(ledger, autocommit=True, timeout=LEDGER_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")  # 删了的字抹成 0（彻底删除以后不留在文件里）
    return conn


def _begin(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:  # 等了 LEDGER_TIMEOUT 秒还锁着
        raise LedgerBusy("账本被别的程序锁着") from error


def _end(side: Side, ids: Sequence[str], relation_of: Mapping[str, str] | None = None) -> dict[str, object]:
    relations: list[Relation] = [side.relations[one] for one in ids if one in side.relations]
    if relation_of is not None:  # 主线：账本里的关系编号，加上说法编号
        out: dict[str, object] = {"relations": list(dict.fromkeys(relation_of.get(one, one) for one in ids)),
                                  "claims": list(ids)}
    else:
        out = {"relations": list(ids)}
    if relations:
        first = relations[0]
        subject, obj = side.entities.get(first.subject), side.entities.get(first.object)
        out.update(subject=subject.name if subject else first.subject, object=obj.name if obj else first.object,
                   states=sorted({one.state for one in relations}))
    return out


def key_of(item: Mismatch, witness: str) -> str:
    def key(value: object) -> str:
        return "" if value is None else f"{value.subject}|{value.relation}|{value.object}"  # type: ignore[attr-defined]

    text = "\n".join((witness, item.kind, item.packet, item.relation, key(item.main), key(item.side)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _live_sentences(conn: sqlite3.Connection, sentence_ids: Collection[str]) -> set[str]:
    """这几句里没撤回、在现行的那一版里的。"""
    ids = sorted(set(sentence_ids))
    found: set[str] = set()
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        found |= {row[0] for row in conn.execute(
            "SELECT s.sentence_id FROM raw_sentences s JOIN packets p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            f" WHERE s.sentence_id IN ({marks}) AND s.withdrawn_at IS NULL AND p.state = 'current' AND p.withdrawn_at IS NULL",
            chunk)}
    return found


def _withdrawn_since(conn: sqlite3.Connection, packet: str, started: object) -> bool:
    """这个包有没有句子在 started 以后（没写 started 的：不管哪时候）撤回了。"""
    if started is None:
        row = conn.execute("SELECT 1 FROM raw_sentences WHERE packet_id = ? AND withdrawn_at IS NOT NULL LIMIT 1",
                           (packet,)).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM raw_sentences WHERE packet_id = ? AND withdrawn_at IS NOT NULL"
            " AND (julianday(?) IS NULL OR julianday(withdrawn_at) >= julianday(?)) LIMIT 1",
            (packet, str(started), str(started))).fetchone()
    return row is not None


def queue_contradictions(ledger: Path, comparison: Comparison, main: Side, side: Side, *,
                         relation_of: Mapping[str, str] | None = None) -> Queued:
    """真矛盾写进待确认。调用方要拿着 ledger_locks。

    先按 stale_condition 清一遍以前进过、现在不算数了的（清了再查重：不算数了的同一条不挡着新的）。给的支线结果不全信
    （旧的导出照样拿得来比）：包不是现行的了、导出的不是现行的那一版（没写是哪一版的也算）、证据里有撤回了或者找不到的
    句子、这一组开始抽以后（导出的 `described.groups_started`，没写的算一直以来）包里有句子撤回了（那一组作废了），都不进。
    claims 记上比的是哪一版。relation_of：主线的说法编号 → 账本里的关系编号（javmem.side.mainline 给）。"""
    now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    described = side.described if isinstance(side.described, Mapping) else {}
    started = described.get("groups_started")
    started = started if isinstance(started, Mapping) else {}
    conn = _open(ledger)
    added = stale = 0
    try:
        _begin(conn)
        try:
            conn.execute(f"DELETE FROM review_queue WHERE {stale_condition(conn)}")
            current = {row[0]: int(row[1]) for row in conn.execute(
                "SELECT packet_id, revision FROM packets WHERE state = 'current' AND withdrawn_at IS NULL")}
            known = {row[0] for row in conn.execute(
                "SELECT json_extract(" + _CLAIMS + ", '$.key') FROM review_queue WHERE kind = ?", (KIND,))}
            items = comparison.queue()
            live = _live_sentences(conn, [one for item in items for one in item.evidence])
            for item in items:
                revision = current.get(item.packet)
                if (revision is None or side.revisions.get(item.packet) != revision or not set(item.evidence) <= live
                        or _withdrawn_since(conn, item.packet, started.get(item.packet))):
                    stale += 1
                    continue
                key = key_of(item, side.name)
                if key in known:
                    continue
                claims = {"key": key, "witness": side.name, "kind": item.kind, "packet": item.packet, "revision": revision,
                          "relation": item.relation, "main": _end(main, item.main_ids, relation_of or {}),
                          "side": _end(side, item.side_ids)}
                conn.execute("INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES (?, ?, ?, ?)",
                             (KIND, json.dumps(claims, ensure_ascii=False), json.dumps(list(item.evidence)), now))
                known.add(key)
                added += 1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return Queued(added, stale)


def drop_stale(ledger: Path, packets: Collection[str] = ()) -> int:
    """删不算数了的第二证人待确认（stale_condition），加上点名的这几个包的（它们在体检库里的组刚删了、要重跑），返回删了几条。
    调用方要拿着 ledger_locks；账本被别的程序锁着报 LedgerBusy。"""
    if not ledger.exists():
        return 0
    conn = _open(ledger)
    try:
        _begin(conn)
        try:
            deleted = conn.execute(f"DELETE FROM review_queue WHERE {stale_condition(conn)}").rowcount
            for start in range(0, len(sorted(packets)), _CHUNK):
                chunk = sorted(packets)[start:start + _CHUNK]
                marks = ",".join("?" * len(chunk))
                deleted += conn.execute(
                    f"DELETE FROM review_queue WHERE kind = ? AND json_extract({_CLAIMS}, '$.packet') IN ({marks})",
                    (KIND, *chunk)).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return int(deleted)


def drop_for_packets(ledger: Path, packets: Collection[str]) -> int:
    """撤回、彻底删除、换了版本的包：它们的第二证人待确认（开着的、定过的）都删（连同别的不算数了的）。"""
    return drop_stale(ledger, packets) if packets else 0


def clean(ledger: Path, data_dir: Path, packets: Collection[str] = ()) -> int:
    """拿着 ledger_locks 删（drop_stale）。拿不到锁、账本被锁着：报 LedgerBusy。"""
    if not ledger.exists():
        return 0
    with ledger_locks(data_dir):
        return drop_stale(ledger, packets)


def has_rows(ledger: Path) -> bool:
    """账本里有没有第二证人的待确认（只读）。没有就不用拿锁去清。"""
    if not ledger.exists():
        return False
    try:
        conn = connect_readonly(ledger)
    except sqlite3.Error:
        return False
    try:
        return conn.execute("SELECT 1 FROM review_queue WHERE kind = ? LIMIT 1", (KIND,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def checkpoint(ledger: Path) -> None:
    """彻底删除以后：把 WAL 写回账本、截掉，secure_delete 抹掉的才真的落到文件里。拿不到就算了（下一次自动写回）。"""
    try:
        conn = _open(ledger)
    except sqlite3.Error:
        return
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def clean_and_say(ledger: Path, data_dir: Path, packets: Collection[str], out: Callable[[str], None]) -> int | None:
    """支线的命令扫完以后清第二证人的待确认，说一句；没清成的说一句、返回 None（下一遍再清）。"""
    try:
        dropped = clean(ledger, data_dir, packets)
    except LedgerBusy as busy:
        out(f"第二证人的待确认没清（{busy}）：之后再跑一遍 javmem side sweep")
        return None
    except Exception as error:  # noqa: BLE001 - 账本出了别的错：这一回不清，不让支线的命令停在这里
        out(f"第二证人的待确认没清（{type(error).__name__}）：之后再跑一遍 javmem side sweep")
        return None
    if dropped:
        out(f"第二证人的待确认删了 {dropped} 条（包撤回了、换了版本、彻底删除了，或者证据撤回了）")
    return dropped
