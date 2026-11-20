"""写原文索引。全文索引 raw_fts、raw_norm_fts 靠触发器跟着 raw_sentences 走，这里不用管。

每一句多存一份统一写法（norm，javmem.zh.search.searchable，D77），raw_search 查的是它。
还存一份比引用用的写法的指纹（copy_key，D101）：取包时按它找账本里同样的话，看引用的旧信在不在。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Collection

from javmem.packet.model import Packet
from javmem.raw.sentences import iter_sentences
from javmem.timeutil import epoch, iso
from javmem.zh.search import searchable

_INSERT = (
    "INSERT INTO raw_sentences (sentence_id, packet_id, revision, seq, section_kind, section_no, line_no,"
    " sentence_no, field, text, speaker, speaker_role, said_at, said_at_epoch, origin, quoted,"
    " message_id, thread_id, subject, withdrawn_at, norm, copy_key)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_FILL_BATCH = 2000

# 比引用用的写法（D101）：去掉开头的 >、压空白、去掉结尾的句末标点，和撤回认引用时一样（withdraw._quote_key）
_COPY_PREFIX = re.compile(r"^(?:>\s?)+")
_COPY_SPACES = re.compile(r"\s+")
COPY_TRAILING = " 　。．.!！?？;；"
COPY_MIN_CHARS = 8  # 太短的话（“好的，收到。”）谁都说过，不比


def copy_form(text: str) -> str:
    """比引用用的写法：“> We ship on Nov 15.”和原话“We ship on Nov 15”是一句。"""
    return _COPY_SPACES.sub(" ", _COPY_PREFIX.sub("", text.strip())).strip().rstrip(COPY_TRAILING)


def copy_key(text: str) -> str | None:
    """原文索引的 copy_key：比引用用的写法的指纹，8 个字以上才有。"""
    form = copy_form(text)
    if len(form) < COPY_MIN_CHARS:
        return None
    return hashlib.sha256(f"javmem-copy\n{form}".encode("utf-8")).hexdigest()


def fill_copy_key(conn: sqlite3.Connection) -> int:
    """按比引用用的写法补上每一句的 copy_key，返回改了几句。账本迁移（0012）在它的事务里调。"""
    total = 0
    last = 0
    while True:
        rows = conn.execute(
            "SELECT id, text, copy_key FROM raw_sentences WHERE id > ? ORDER BY id LIMIT ?", (last, _FILL_BATCH)
        ).fetchall()
        if not rows:
            break
        changed = [(key, row[0]) for row in rows if (key := copy_key(row[1])) != row[2]]
        conn.executemany("UPDATE raw_sentences SET copy_key = ? WHERE id = ?", changed)
        total += len(changed)
        last = rows[-1][0]
    return total


def fill_norm(conn: sqlite3.Connection) -> int:
    """按现在的统一写法重算每一句的 norm，返回改了几句。全文索引靠触发器跟着改（旧的删掉、新的加上）。
    账本迁移（0010）在它的事务里调；以后统一写法改了，新的迁移也调它。"""
    total = 0
    last = 0
    while True:
        rows = conn.execute(
            "SELECT id, text, norm FROM raw_sentences WHERE id > ? ORDER BY id LIMIT ?", (last, _FILL_BATCH)
        ).fetchall()
        if not rows:
            break
        changed = [(norm, row[0]) for row in rows if (norm := searchable(row[1])) != row[2]]
        conn.executemany("UPDATE raw_sentences SET norm = ? WHERE id = ?", changed)
        total += len(changed)
        last = rows[-1][0]
    return total


def _fingerprint(speaker: str, text: str) -> str:
    return hashlib.sha256(f"{speaker}\n{text}".encode("utf-8")).hexdigest()


def sentence_hash(speaker: str, text: str) -> str:
    """一句原文的指纹：说话人加原句。单条撤回的记录、证据都按它认，不按句子编号（编号只是位置）。"""
    return _fingerprint(speaker, text)


def covered(records: Collection[tuple[str, str]], message_id: str | None, digest: str) -> bool:
    """单条撤回的记录 (message_id, 指纹) 里有没有这一句：同一封信的（邮件），或者整个包的（对话包、
    迁移 0011 以前没记是哪一封的邮件记录）。"""
    return (message_id or "", digest) in records or ("", digest) in records


def mark_withdrawn(conn: sqlite3.Connection, sentence_id: str, now: str, *, cause: str | None = None) -> list[str]:
    """把一句标成已撤回，返回这次新标上的句子编号（同一封信、同一个包里同一个人一字不差的话一起标）。
    要在调用方的事务里跑。

    记录按“这个包、这封信、这个人、这句话”记：换版本之后位置可能变了，只能靠这个认出它。对话包没有“这封信”，
    同一个包里同一个人一字不差的话会一起挡住，宁可多挡；邮件包只挡同一封信里的（D73 第 17 条）：同一个人别的信的
    发件人、称呼、署名、附件名那一行一字不差，不跟着标。别人说的同样的话不受影响。
    cause 是因为撤回哪个包才跟着标的（别的包里引用它的行），解除撤回那个包时按它解除；点名撤回的是空的。
    句子不存在，或者早就撤回了，返回空列表。
    """
    row = conn.execute(
        "SELECT packet_id, speaker, text, message_id FROM raw_sentences WHERE sentence_id = ?", (sentence_id,)
    ).fetchone()
    if row is None:
        return []
    mail = row["message_id"] or ""
    conn.execute(
        "INSERT INTO withdrawn_sentences (packet_id, message_id, sentence_hash, withdrawn_at, cause) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (packet_id, message_id, sentence_hash)"
        " DO UPDATE SET cause = CASE WHEN excluded.cause IS NULL THEN NULL ELSE cause END",
        (row["packet_id"], mail, _fingerprint(row["speaker"], row["text"]), now, cause),
    )
    same = "packet_id = ? AND coalesce(message_id, '') = ? AND speaker = ? AND text = ? AND withdrawn_at IS NULL"
    params = (row["packet_id"], mail, row["speaker"], row["text"])
    marked = [
        found["sentence_id"] for found in conn.execute(f"SELECT sentence_id FROM raw_sentences WHERE {same} ORDER BY seq", params)
    ]
    conn.execute(f"UPDATE raw_sentences SET withdrawn_at = ? WHERE {same}", (now, *params))
    return marked


def withdraw_sentence(conn: sqlite3.Connection, sentence_id: str, now: str) -> bool:
    """把一句标成已撤回（见 mark_withdrawn）。句子不存在时返回 False。"""
    exists = conn.execute("SELECT 1 FROM raw_sentences WHERE sentence_id = ?", (sentence_id,)).fetchone()
    if exists is None:
        return False
    mark_withdrawn(conn, sentence_id, now)
    return True


def replace_packet_sentences(conn: sqlite3.Connection, packet: Packet) -> int:
    """把这个包的原文索引换成这一版的，返回有几行。要在调用方的事务里跑。

    单条撤回的内容不会因为替换又回来：按 withdrawn_sentences 的记录，新版本里的这些句子也标上（邮件只标同一封信的）。
    """
    header = packet.header
    withdrawn = {
        (row["message_id"], row["sentence_hash"]): row["withdrawn_at"]
        for row in conn.execute(
            "SELECT message_id, sentence_hash, withdrawn_at FROM withdrawn_sentences WHERE packet_id = ?", (header.packet_id,)
        )
    }

    def withdrawn_at(message_id: str | None, digest: str) -> str | None:
        found = withdrawn.get((message_id or "", digest))
        return found if found is not None else withdrawn.get(("", digest))

    conn.execute("DELETE FROM raw_sentences WHERE packet_id = ?", (header.packet_id,))

    rows = [
        (
            sentence.sentence_id,
            header.packet_id,
            header.revision,
            seq,
            sentence.section_kind,
            sentence.section_no,
            sentence.line_no,
            sentence.sentence_no,
            sentence.field,
            sentence.text,
            sentence.speaker,
            sentence.speaker_role,
            iso(sentence.said_at),
            epoch(sentence.said_at),
            sentence.origin,
            int(sentence.quoted),
            sentence.message_id,
            sentence.thread_id,
            sentence.subject,
            withdrawn_at(sentence.message_id, _fingerprint(sentence.speaker, sentence.text)),
            searchable(sentence.text),
            copy_key(sentence.text),
        )
        for seq, sentence in enumerate(iter_sentences(packet), 1)
    ]
    conn.executemany(_INSERT, rows)
    return len(rows)
