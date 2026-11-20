"""只在引用里看到过的话（D101 第一条）。样例都是编的。

邮件引的旧信里新写的话，整本账本里找不到原话、回的那封信也不在的，同一个可见范围里最早的那一份引用当证据
（L1 标 quoted_only，写给模型时标〔转发，原作者 …〕，L4 一律推断）；原信后来到了、更早的一份引用到了，换过去，
以前当证据的那一份所在的包改回排队。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import World, build_mail_packet, build_packet, file_name, mail_header_block

from javmem.ledger import connect, load_migrations, migrate, transaction
from javmem.packet import parse_packet
from javmem.pipeline.batch import _visible, load_queued
from javmem.pipeline.normalize import (
    NEW,
    QUOTED,
    QUOTED_ONLY,
    SIGNATURE,
    TARGET_KINDS,
    Scope,
    classify_mail_body,
    normalize,
    quote_sources,
)
from javmem.pipeline.render import render_window
from javmem.pipeline.verify import INFERRED, VERIFIED, verify_window
from javmem.pipeline.window import build_windows
from javmem.pipeline.write import record_quote_holders, record_quote_roles
from javmem.raw.index import copy_key
from javmem.redaction import default_patterns
from javmem.withdraw import purge, withdraw

LENA = "On Tue, Nov 3, 2026 at 9:12 AM Lena Ortiz <lena.ortiz@example.net> wrote:"
CRATES = "The crates for order 7 leave the yard on Friday."


def sources(body: list[str], subject: str = "Re: Crates") -> dict[int, tuple[str, str | None]]:
    return quote_sources(body, [kind for kind, _ in classify_mail_body(body, subject)])


# ---------- L1：引的旧信里是什么 ----------


def test_a_quoted_old_mail_is_read_as_a_mail() -> None:
    """> 引的旧信再认一遍：新写的话、署名分开，更深一层的引用是更早那封信的话，作者取引用抬头里写的人。"""
    body = [
        "Thanks, noted.",
        "",
        LENA,
        f"> {CRATES}",
        ">",
        "> Best regards,",
        "> Lena Ortiz",
        "> Tel: 000-5555-0100",
        ">",
        "> On Mon, Nov 2, 2026 at 8:00 AM Tom Reed <tom.reed@example.org> wrote:",
        ">> Can the crates go out this week?",
    ]
    found = sources(body)
    lena = "Lena Ortiz <lena.ortiz@example.net>"
    assert found[3] == (NEW, lena)
    assert [found[row][0] for row in (5, 6, 7)] == [SIGNATURE, SIGNATURE, SIGNATURE]
    assert found[10] == (NEW, "Tom Reed <tom.reed@example.org>")
    assert 0 not in found and 4 not in found  # 回信自己写的、引用里的空行不在里面


def test_an_outlook_block_is_one_old_mail_per_header() -> None:
    """Outlook 那种不带 > 的原始邮件块：引用抬头隔开的每一段是一封旧信，作者取 From: 那一行。"""
    body = [
        "Noted.",
        "",
        "-----Original Message-----",
        "From: Lena Ortiz [mailto:lena.ortiz@example.net]",
        "Sent: Tuesday, November 3, 2026 9:12 AM",
        "To: Tom Reed",
        "Subject: RE: Crates",
        "",
        CRATES,
        "",
        "Best regards,",
        "Lena Ortiz",
        "Sales Manager",
        "",
        "From: Tom Reed <tom.reed@example.org>",
        "Sent: Monday, November 2, 2026 8:00 AM",
        "To: Lena Ortiz",
        "Subject: Crates",
        "",
        "Can the crates go out this week?",
    ]
    found = sources(body)
    assert found[8] == (NEW, "Lena Ortiz [mailto:lena.ortiz@example.net]")
    assert [found[row][0] for row in (10, 11, 12)] == [SIGNATURE, SIGNATURE, "signature_info"]
    assert found[19] == (NEW, "Tom Reed <tom.reed@example.org>")


@pytest.mark.parametrize(
    ("header", "author"),
    [
        ("在 2026年11月3日 09:12，林浩 写道：", "林浩"),
        ("林浩 <lin.hao@example.net> 于2026年11月3日周二 09:12写道：", "林浩 <lin.hao@example.net>"),
        ("On Tuesday, November 3, 2026 9:12 AM, Lena Ortiz wrote:", "Lena Ortiz"),
        ("El mar, 3 nov 2026 a las 9:12, Lena Ortiz (<lena.ortiz@example.net>) escribió:", "Lena Ortiz <lena.ortiz@example.net>"),
        ("On 3 Nov 2026, at 09:12, <lena.ortiz@example.net> wrote:", "<lena.ortiz@example.net>"),
    ],
)
def test_the_author_comes_from_the_quote_header(header: str, author: str) -> None:
    body = ["收到。", "", header, "> 这批木箱周五从堆场出发。"]
    assert sources(body)[3] == (NEW, author)


def mail(message: str, date: str, lines: list[str], *, subject: str = "Re: Crates", reply_to: str | None = "<orig@example.net>",
         sender: str = '{name: "Tom Reed", addr: "tom.reed@example.org"}') -> str:
    header = mail_header_block(
        message_id=f'"{message}"', thread_id='"<orig@example.net>"', in_reply_to=f'"{reply_to}"' if reply_to else "null",
        date=date, subject=f'"{subject}"', **{"from": sender},
    )
    body = "".join(f"  {line}\n" if line else "\n" for line in lines)
    return f"## 原话 · 邮件 · {date[:10]} {date[11:16]}\n\n{header}\n{body}\n"


REPLY = ["Thanks, noted.", "", LENA, f"> {CRATES}", ">", "> Best regards,", "> Lena Ortiz"]


def mail_packet(day: int, sections: str, **fields: object) -> str:
    return build_mail_packet(
        sections=sections, packet_id=f"2026-11-{day:02d}_1800_mail-sales-a", covers_from="2026-11-01T00:00:00+08:00",
        covers_to=f"2026-11-{day:02d}T18:00:00+08:00", **fields,
    )


def kinds_of(packet) -> dict[str, str]:  # noqa: ANN001
    return {line.text: line.kind for unit in packet.units for line in unit.lines}


def test_quote_evidence_only_changes_the_old_mails_own_words() -> None:
    raw = mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY))
    packet = parse_packet(raw.encode("utf-8"), filename=file_name(raw))
    plain = normalize(packet)
    ids = {line.text: line.sentence_id for unit in plain.units for line in unit.lines}
    assert kinds_of(plain)[f"> {CRATES}"] == QUOTED
    chosen = normalize(packet, quote_evidence={ids[f"> {CRATES}"], ids["> Lena Ortiz"]})
    assert kinds_of(chosen)[f"> {CRATES}"] == QUOTED_ONLY and QUOTED_ONLY in TARGET_KINDS
    assert kinds_of(chosen)["> Lena Ortiz"] == QUOTED  # 旧信的署名给了也不改
    assert chosen.kinds[QUOTED_ONLY] == 1
    [line] = [line for unit in chosen.units for line in unit.lines if line.kind == QUOTED_ONLY]
    assert line.relayed_from == "Lena Ortiz <lena.ortiz@example.net>" and line.old_kind == NEW


def test_chat_quotes_are_never_quote_evidence() -> None:
    raw = build_packet(body="## 原话 · 10:30–10:31\n- [10:30] 我：木箱周五从堆场出发。\n  > [引用 10:00 小李] 木箱这周能出发吗？\n")
    packet = normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))
    ids = [line.sentence_id for unit in packet.units for line in unit.lines]
    assert QUOTED_ONLY not in kinds_of(normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)), quote_evidence=ids)).values()


# ---------- 写给模型、L4 ----------


def test_quote_evidence_is_a_target_labelled_like_a_forward() -> None:
    raw = mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY))
    packet = parse_packet(raw.encode("utf-8"), filename=file_name(raw))
    ids = {line.text: line.sentence_id for unit in normalize(packet).units for line in unit.lines}
    [window] = build_windows([normalize(packet, quote_evidence={ids[f"> {CRATES}"]})])
    target = [line for line in window.targets if line.kind == QUOTED_ONLY]
    assert len(target) == 1
    rendered = render_window(window, default_patterns()).text
    assert f"{target[0].ref} 〔转发，原作者 Lena Ortiz <lena.ortiz@example.net>〕> {CRATES}" in rendered
    assert "〔引用〕> Best regards," in rendered  # 旧信的署名照旧只帮着理解


def facts_for(quotes: list[tuple[str, str]], certainty: str = "stated") -> dict:
    return {
        "entities": [{"key": "e1", "mention": "order 7", "name": "order 7", "type": "订单或合同"}],
        "facts": [{
            "text": "Order 7 crates leave the yard on Friday", "subject": "e1", "attribute": None, "value": None,
            "occurred_at": None, "time_precision": "unknown", "certainty": certainty,
            "entities": [{"key": "e1", "role": "subject"}],
            "evidence": [{"ref": ref, "quote": quote} for ref, quote in quotes],
        }],
    }


def test_facts_only_seen_in_quotes_are_inferred() -> None:
    """证据全是只在引用里看到过的话：L3 写 stated 也是推断。回信的人自己又说了一遍的照旧是 verified。"""
    lines = ["Yes, the crates for order 7 leave the yard on Friday.", "", LENA, f"> {CRATES}"]
    raw = mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", lines))
    packet = parse_packet(raw.encode("utf-8"), filename=file_name(raw))
    ids = {line.text: line.sentence_id for unit in normalize(packet).units for line in unit.lines}
    [window] = build_windows([normalize(packet, quote_evidence={ids[f"> {CRATES}"]})])
    rendered = render_window(window, default_patterns())
    refs = {line.kind: line.ref for line in window.targets}
    only = verify_window(window, rendered, facts_for([(refs[QUOTED_ONLY], "leave the yard on Friday")]))
    assert only.facts[0].passed and only.facts[0].status == INFERRED
    both = verify_window(window, rendered, facts_for([(refs[QUOTED_ONLY], "leave the yard on Friday"),
                                                       (refs[NEW], "leave the yard on Friday")]))
    assert both.facts[0].passed and both.facts[0].status == VERIFIED


# ---------- 取包：按账本定谁当证据 ----------


def deliver(world: World, *packets: str) -> list[str]:
    for packet in packets:
        world.drop("inbox/mail-sales-a", packet)
    world.intake()
    return [file_name(packet)[:-3] for packet in packets]


def written(world: World, batch) -> list[str]:  # noqa: ANN001
    """当这一批写完了：和 javmem write 一样，记下每一句是原话还是引用、谁当证据，包改成 done。返回改回排队的包。"""
    conn = world.ledger()
    try:
        with transaction(conn):
            record_quote_roles(conn, [*batch.packets, *batch.context])
            requeued = record_quote_holders(conn, batch.quote_holders, batch.requeue)
            for packet in batch.packets:
                conn.execute("UPDATE packets SET extraction = 'done' WHERE packet_id = ?", (packet.packet_id,))
        return requeued
    finally:
        conn.close()


def quote_kind(batch, packet_id: str, text: str = f"> {CRATES}") -> str:  # noqa: ANN001
    packet = next(packet for packet in batch.packets if packet.packet_id == packet_id)
    return kinds_of(packet)[text]


def test_a_quote_whose_original_is_missing_becomes_evidence(world: World) -> None:
    [first] = deliver(world, mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)))
    batch = load_queued(world.settings, "prod")
    assert quote_kind(batch, first) == QUOTED_ONLY
    assert quote_kind(batch, first, "> Lena Ortiz") == QUOTED  # 旧信的署名照旧只当上文
    assert len(batch.quote_holders) == 1 and batch.requeue == []


def test_only_the_earliest_copy_is_evidence(world: World) -> None:
    first, second = deliver(
        world,
        mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)),
        mail_packet(5, mail("<r2@example.org>", "2026-11-05T09:00:00+08:00", REPLY)),
    )
    batch = load_queued(world.settings, "prod")
    assert quote_kind(batch, first) == QUOTED_ONLY
    assert quote_kind(batch, second) == QUOTED


def test_the_original_arriving_later_takes_over(world: World) -> None:
    """原信后来到了：那一份引用回到上文，它的包改回排队重抽；原信照常当证据。"""
    [first] = deliver(world, mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)))
    assert written(world, load_queued(world.settings, "prod")) == []
    original = mail("<orig@example.net>", "2026-11-03T09:12:00+08:00", [CRATES, "", "Best regards,", "Lena Ortiz"],
                    subject="Crates", reply_to=None, sender='{name: "Lena Ortiz", addr: "lena.ortiz@example.net"}')
    [late] = deliver(world, mail_packet(6, original))
    batch = load_queued(world.settings, "prod")
    assert [packet.packet_id for packet in batch.packets] == [late]
    assert batch.requeue == [first]
    assert written(world, batch) == [first]
    assert world.packet_row(first)["extraction"] == "queued"
    again = load_queued(world.settings, "prod")
    assert quote_kind(again, first) == QUOTED
    assert written(world, again) == []
    assert world.rows("SELECT count(*) AS n FROM quote_holders")[0]["n"] == 0


def test_an_earlier_copy_arriving_later_takes_over(world: World) -> None:
    [second] = deliver(world, mail_packet(5, mail("<r2@example.org>", "2026-11-05T09:00:00+08:00", REPLY)))
    written(world, load_queued(world.settings, "prod"))
    [first] = deliver(world, mail_packet(6, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)))
    batch = load_queued(world.settings, "prod")
    assert quote_kind(batch, first) == QUOTED_ONLY
    assert batch.requeue == [second]
    written(world, batch)
    assert quote_kind(load_queued(world.settings, "prod"), second) == QUOTED


def test_the_mail_it_replies_to_being_there_is_enough(world: World) -> None:
    """回的那封信（In-Reply-To）在账本里：引用折了行、字对不上也不当证据。"""
    original = mail("<orig@example.net>", "2026-11-03T09:12:00+08:00", ["The crates for order 7 leave the yard on", "Friday."],
                    subject="Crates", reply_to=None, sender='{name: "Lena Ortiz", addr: "lena.ortiz@example.net"}')
    [_, reply] = deliver(world, mail_packet(4, original), mail_packet(5, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)))
    assert quote_kind(load_queued(world.settings, "prod"), reply) == QUOTED


def test_an_original_in_another_partition_does_not_count(world: World) -> None:
    """原话在别的分区：看得到这份引用的人看不到它，这份引用照样当证据。"""
    original = mail("<other@example.net>", "2026-11-02T09:12:00+08:00", [CRATES], subject="Crates", reply_to=None)
    [_, reply] = deliver(
        world, mail_packet(3, original, domain="[finance]"), mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY))
    )
    assert quote_kind(load_queued(world.settings, "prod"), reply) == QUOTED_ONLY


def test_visible_means_same_partitions_and_fewer_tags() -> None:
    copy = Scope(("business",), ("deal",))
    assert _visible(Scope(("business",), ()), copy)
    assert _visible(Scope(("business",), ("deal",)), copy)
    assert not _visible(Scope(("business",), ("deal", "hr")), copy)
    assert not _visible(Scope(("finance",), ()), copy)


def test_withdrawn_and_truncated_quotes_are_not_evidence(world: World) -> None:
    lines = ["Thanks, noted.", "", LENA, f"> {CRATES}", "> The invoice for order 7 follows next we…"]
    [first] = deliver(world, mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", lines)))
    batch = load_queued(world.settings, "prod")
    assert quote_kind(batch, first, "> The invoice for order 7 follows next we…") == QUOTED
    world.execute("INSERT INTO withdrawn_texts (key_hash, cause, withdrawn_at) VALUES (?, NULL, '2026-11-05T00:00:00+08:00')",
                  _withdrawn_hash(CRATES))
    assert quote_kind(load_queued(world.settings, "prod"), first) == QUOTED


def _withdrawn_hash(text: str) -> str:
    from javmem.withdraw import withdrawn_text_hash

    return withdrawn_text_hash(text.rstrip("."))


@pytest.mark.parametrize("how", ["packet", "sentence"])
def test_withdrawing_one_copy_keeps_the_other_copies_out(world: World, how: str) -> None:
    """撤回了一份引用（整包撤回，或者只撤那一句 > 引用）：同一句话别的引用也不当证据，撤回的话不会换个地方又抽出来。
    > 引用行撤回时不记 withdrawn_texts（引用行只是旧话重说），所以按原文索引里撤回了的句子认。"""
    first, second = deliver(
        world,
        mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)),
        mail_packet(5, mail("<r2@example.org>", "2026-11-05T09:00:00+08:00", REPLY)),
    )
    batch = load_queued(world.settings, "prod")
    assert quote_kind(batch, first) == QUOTED_ONLY
    written(world, batch)
    if how == "packet":
        world.execute("UPDATE packets SET withdrawn_at = '2026-11-06T00:00:00+08:00' WHERE packet_id = ?", first)
    else:
        world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-06T00:00:00+08:00' WHERE packet_id = ? AND text = ?",
                      first, f"> {CRATES}")
    world.execute("UPDATE packets SET extraction = 'queued' WHERE packet_id = ?", second)
    batch = load_queued(world.settings, "prod")
    assert quote_kind(batch, second) == QUOTED


# ---------- 账本：比引用用的指纹 ----------


def test_copy_key_ignores_quote_marks_spaces_and_the_full_stop() -> None:
    assert copy_key(f">  {CRATES}") == copy_key(CRATES.rstrip(".")) == copy_key("The crates for order 7  leave the yard on Friday")
    assert copy_key("Thanks.") is None  # 太短的不比


def test_migration_fills_the_copy_key_of_old_sentences(tmp_path: Path) -> None:
    migrations = load_migrations()
    conn = connect(tmp_path / "ledger.db")
    migrate(conn, [m for m in migrations if m.version <= 11])
    conn.execute("PRAGMA foreign_keys = OFF")  # 只测原文索引，不登记包
    for number, text in enumerate([f"> {CRATES}", "Thanks."], 1):
        conn.execute(
            "INSERT INTO raw_sentences (sentence_id, packet_id, revision, seq, section_kind, section_no, line_no,"
            " sentence_no, text, speaker, speaker_role, said_at, said_at_epoch, origin)"
            " VALUES (?, 'P', 1, ?, 'raw', 1, ?, 1, ?, 'x', 'other', 't', 0, 'external')",
            (f"P#1.{number}.1", number, number, text),
        )
    assert migrate(conn) == [m.version for m in migrations if m.version > 11]
    keys = dict(conn.execute("SELECT sentence_id, copy_key FROM raw_sentences").fetchall())
    assert keys == {"P#1.1.1": copy_key(CRATES), "P#1.2.1": None}
    conn.close()


def test_intake_writes_the_copy_key(world: World) -> None:
    deliver(world, mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)))
    [row] = world.rows("SELECT copy_key FROM raw_sentences WHERE text = ?", f"> {CRATES}")
    assert row["copy_key"] == copy_key(CRATES)


# ---------- 第四轮审阅：撤回 ----------

OUTLOOK = ["Noted.", "", "-----Original Message-----", "From: Lena Ortiz [mailto:lena.ortiz@example.net]",
           "Sent: Tuesday, November 3, 2026 9:12 AM", "To: Tom Reed", "Subject: RE: Crates", "", CRATES]


def lena_original(lines: list[str] | None = None) -> str:
    return mail("<orig@example.net>", "2026-11-03T09:12:00+08:00", lines or [CRATES], subject="Crates", reply_to=None,
                sender='{name: "Lena Ortiz", addr: "lena.ortiz@example.net"}')


def test_withdrawing_the_original_withdraws_an_evidence_copy_without_quote_marks(world: World) -> None:
    """第四轮审阅第 1 条：Outlook 那种不带 > 的引用行原文索引里 quoted 是 0，撤回原话时以前只标 > 行，从这一份抽出来的
    事实留着。正在当证据的那一份（quote_holders 里记着的）也按原话标上撤回，它的包改回排队。"""
    secret, reply = deliver(world, mail_packet(3, lena_original(), domain="[finance]"),
                            mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", OUTLOOK)))
    batch = load_queued(world.settings, "prod", packet_ids=[reply])
    assert quote_kind(batch, reply, CRATES) == QUOTED_ONLY  # 原信在别的分区，看得到这封回信的人看不到它
    written(world, batch)
    withdraw(world.settings, "prod", packets=[secret])
    [row] = world.rows("SELECT withdrawn_at, quoted FROM raw_sentences WHERE packet_id = ? AND text = ?", reply, CRATES)
    assert row["quoted"] == 0 and row["withdrawn_at"] is not None
    assert world.packet_row(reply)["extraction"] == "queued"


SHARP = "The crates for order 7 leave the yard on Friday morning at 9 sharp."
FOLDED = ["Thanks, noted.", "", LENA, "> The crates for order 7 leave the yard on Friday", "> morning at 9 sharp."]


def folded_kinds(batch, packet_id: str) -> set[str]:  # noqa: ANN001
    return {quote_kind(batch, packet_id, line) for line in FOLDED[3:]}


def test_a_folded_quote_of_a_withdrawn_mail_it_replies_to_is_not_evidence(world: World) -> None:
    """第四轮审阅第 2 条：回的那封信撤回了，以前算“不在”，折了行的引用（和撤回的原话一字不差对不上）就当了证据。
    撤回了的信也算在。"""
    original, reply = deliver(world, mail_packet(3, lena_original([SHARP])),
                              mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", FOLDED)))
    withdraw(world.settings, "prod", packets=[original])
    assert folded_kinds(load_queued(world.settings, "prod"), reply) == {QUOTED}


def test_a_folded_quote_of_withdrawn_words_is_withdrawn_and_never_evidence(world: World) -> None:
    """第四轮审阅第 2 条：没写回的哪一封（没有 In-Reply-To）、折了行的引用，是一句撤回了的话的一部分：
    已经当了证据的，撤回原话时一起撤回；没当的，以后也不当。"""
    original, reply = deliver(world, mail_packet(3, lena_original([SHARP])),
                              mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", FOLDED, reply_to=None)))
    batch = load_queued(world.settings, "prod", packet_ids=[reply])
    assert folded_kinds(batch, reply) == {QUOTED_ONLY}  # 字对不上原信：两半各当证据（推断），9.1 里写了
    written(world, batch)
    withdraw(world.settings, "prod", packets=[original])
    rows = world.rows("SELECT withdrawn_at FROM raw_sentences WHERE packet_id = ? AND quoted = 1", reply)
    assert rows and all(row["withdrawn_at"] is not None for row in rows)
    [late] = deliver(world, mail_packet(5, mail("<r2@example.org>", "2026-11-05T09:00:00+08:00", FOLDED, reply_to=None)))
    assert folded_kinds(load_queued(world.settings, "prod", packet_ids=[late]), late) == {QUOTED}


def test_a_folded_quote_of_a_purged_mail_it_replies_to_is_not_evidence(world: World) -> None:
    """彻底删除以后原文没了，比不了：记下删掉的信的 Message-ID，回它的信里的引用照旧不当证据。"""
    [original] = deliver(world, mail_packet(3, lena_original([SHARP])))
    withdraw(world.settings, "prod", packets=[original])
    purge(world.settings, "prod", original)
    [reply] = deliver(world, mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", FOLDED)))
    assert folded_kinds(load_queued(world.settings, "prod"), reply) == {QUOTED}


def test_purging_a_withdrawn_copy_keeps_the_other_copies_out(world: World) -> None:
    """第四轮审阅第 3 条：撤回了一份引用，别的引用不当证据（9c8ed3b）；彻底删除那一份以后原文索引里没了，以前就挡不住了。
    彻底删除时记下这些句子的指纹。"""
    first, second = deliver(
        world,
        mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)),
        mail_packet(5, mail("<r2@example.org>", "2026-11-05T09:00:00+08:00", REPLY)),
    )
    written(world, load_queued(world.settings, "prod"))
    withdraw(world.settings, "prod", packets=[first])
    purge(world.settings, "prod", first)
    world.execute("UPDATE packets SET extraction = 'queued' WHERE packet_id = ?", second)
    [late] = deliver(world, mail_packet(6, mail("<r3@example.org>", "2026-11-06T09:00:00+08:00", REPLY)))
    batch = load_queued(world.settings, "prod")
    assert quote_kind(batch, second) == QUOTED and quote_kind(batch, late) == QUOTED


# ---------- 第五轮（第 53 条）：旧信再认时的作者 ----------


def test_a_forward_inside_a_quoted_old_mail_keeps_its_own_author() -> None:
    """第 53 条之一：旧信里又转发了一封，转发块里是别人的话，以前标的是那封旧信的作者。转发块的作者照它的 From 认。"""
    body = [
        "Thanks.",
        "",
        LENA,
        "> See below.",
        ">",
        "> ---------- Forwarded message ---------",
        "> From: Tom Reed <tom.reed@example.org>",
        "> Date: Mon, Nov 2, 2026 at 8:00 AM",
        "> Subject: Crates",
        ">",
        f"> {CRATES}",
    ]
    found = sources(body)
    assert found[3] == (NEW, "Lena Ortiz <lena.ortiz@example.net>")
    assert found[10] == ("forwarded", "Tom Reed <tom.reed@example.org>")


# ---------- 第五轮（第 51 条）：记“谁当证据”、改回排队，在写窗口以前 ----------


class Crash(Exception):
    pass


def test_holders_are_recorded_before_the_windows_are_written(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """第 51 条：以前写完窗口以后另开一个事务记，中间断了，原信那一包已经写完，那一份引用还记作证据、它的包也不回排队，
    推断的说法和原信的说法一直都在。挪到写窗口以前：断在写完窗口以后，那个包也已经改回排队；再跑也不会再记一遍。"""
    from javmem import cli

    [first] = deliver(world, mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)))
    assert cli.main(["extract", "--library", "prod", "--fake"]) == 0
    assert cli.main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert world.packet_row(first)["extraction"] == "done"
    deliver(world, mail_packet(6, lena_original()))
    assert cli.main(["extract", "--library", "prod", "--fake"]) == 0
    real = cli.run_write

    def crash(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        real(*args, **kwargs)
        raise Crash

    monkeypatch.setattr(cli, "run_write", crash)
    with pytest.raises(Crash):
        cli.main(["write", "--library", "prod", "--fake", "--fake-embeddings"])
    assert world.packet_row(first)["extraction"] == "queued"
    assert world.rows("SELECT count(*) AS n FROM quote_holders")[0]["n"] == 0


# ---------- 第五轮（第 53 条之二）：回信的人插在引用里的话（D103 第 6 条） ----------

TOM = '{name: "Tom Reed", addr: "tom.reed@example.org"}'
LIN = '{name: "林浩", addr: "lin.hao@example.org"}'


def outlook_reply(first: list[str], earlier: list[str] | None = None) -> list[str]:
    """Tom 回 Lena：上面一句新写的，下面接着 Outlook 那种原始邮件块（Lena 那封），再下面是更早的一封（Ann 写的，Tom 上一轮在里面插过话）。"""
    lines = ["Please see my comments below.", "", "-----Original Message-----",
             "From: Lena Ortiz [mailto:lena.ortiz@example.net]", "Sent: Tuesday, November 3, 2026 9:12 AM", "To: Tom Reed",
             "Subject: RE: Crates", "", *first]
    if earlier is not None:
        lines += ["", "From: Ann Lee <ann.lee@example.net>", "Sent: Monday, November 2, 2026 8:00 AM", "To: Lena Ortiz",
                  "Subject: Crates", "", *earlier]
    return lines


def unit_lines(raw: str) -> dict[str, object]:
    packet = normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))
    return {line.text: line for unit in packet.units for line in unit.lines}


MAL = '{name: "Mary Ann Lee", addr: "mary.lee@example.org"}'


@pytest.mark.parametrize(
    ("sender", "comment"),
    [(TOM, "[Tom] Friday works, the yard opens at 7."), (MAL, "[MAL] Friday works, the yard opens at 7."),
     (TOM, "<<Tom Reed>> Friday works, the yard opens at 7."), (LIN, "【林浩】周五可以，堆场 7 点开门。")],
)
def test_the_repliers_comment_inside_the_quote_is_the_repliers_own_words(sender: str, comment: str) -> None:
    """第 53 条之二：回信的人插在对方来信引用里的话，算回信的人新写的话，当证据（原话）。只认写着记号的：行首方括号、
    书名号、双尖括号里写着发件人（名字、名字里的一个词、3 个字母以上的首字母缩写）。"""
    raw = mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", outlook_reply([CRATES, comment]), sender=sender))
    lines = unit_lines(raw)
    assert lines[comment].kind == NEW and lines[comment].relayed_from is None
    assert lines[CRATES].kind == QUOTED


@pytest.mark.parametrize("comment", ["[Note] The yard opens at 7.", ">> The yard opens at 7.", "Tom: the yard opens at 7."])
def test_unmarked_or_other_lines_in_the_quote_stay_quoted(comment: str) -> None:
    """护栏：方括号里不是发件人的、>> 后面隔着空格的（一层一层的引用）、“名字:”这种分不清的，照旧是引用。"""
    raw = mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", outlook_reply([CRATES, comment]), sender=TOM))
    assert unit_lines(raw)[comment].kind == QUOTED


@pytest.mark.parametrize(
    ("sender", "comment"),
    [('{name: "Oliver King", addr: "oliver.king@example.org"}', "[OK] Carton marks match the purchase order."),
     (TOM, "[TR] Friday works, the yard opens at 7."), (MAL, "[mal] Friday works, the yard opens at 7."),
     ('{name: "Lena Ortiz", addr: "sales.desk@example.net"}', "[desk] Friday works, the yard opens at 7."),
     (TOM, ">>Can you ship 20 pallets on Friday?"), (TOM, ">>On Mon, Nov 2, 2026 at 8:00 AM Ann Lee <ann.lee@example.net> wrote:")],
)
def test_short_initials_address_parts_and_glued_quote_marks_are_not_comments(sender: str, comment: str) -> None:
    """第六轮（第 58 条）：两个字母的缩写（[OK] 是“好的”）、大小写对不上的、邮件地址 @ 前面拆出来的段，以前都算发件人，
    把别人的话认成了回信的人的；>> 紧跟着字的，旧信自己用不带空格的 > 引更早的信也是这样写，连引用抬头一起成了回信的人
    新写的。这些照旧是引用。>> 那一条去掉了：三箱里一处都没有。"""
    raw = mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", outlook_reply([CRATES, comment]), sender=sender))
    assert unit_lines(raw)[comment].kind == QUOTED


def test_an_earlier_comment_by_the_replier_stays_quoted_with_the_replier_as_author() -> None:
    """更早那几段里写着这封信的发件人的批注，是以前那一轮插的：照旧是引用，原作者写发件人。"""
    comment = "[Tom] Friday works, the yard opens at 7."
    raw = mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", outlook_reply([CRATES], ["Can the crates go out?", comment]),
                              sender=TOM))
    line = unit_lines(raw)[comment]
    assert line.kind == QUOTED and line.relayed_from == "Tom Reed <tom.reed@example.org>"


# ---------- 第五轮（第 50 条）：写过账的包不再读 ----------


def count_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from javmem.pipeline import batch as batch_module

    read: list[str] = []
    real = batch_module._load_other

    def counted(conn, data_dir, packet_id):  # noqa: ANN001, ANN202
        read.append(packet_id)
        return real(conn, data_dir, packet_id)

    monkeypatch.setattr(batch_module, "_load_other", counted)
    return read


def test_written_packets_are_not_read_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """第 50 条：以前这一批的引用里有一句话，整本账本里有这句话的包都要读进来做一遍 L1。写账时记下每一句是原话还是引用，
    写过账的包查表就够，不再读；算出来的和读包的一样。"""
    first, second = deliver(
        world,
        mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)),
        mail_packet(5, mail("<r2@example.org>", "2026-11-05T09:00:00+08:00", REPLY)),
    )
    batch = load_queued(world.settings, "prod", packet_ids=[first])
    assert quote_kind(batch, first) == QUOTED_ONLY
    written(world, batch)
    read = count_reads(monkeypatch)
    batch = load_queued(world.settings, "prod", packet_ids=[second])
    assert read == []
    assert quote_kind(batch, second) == QUOTED and batch.requeue == []
    late = mail_packet(6, mail("<r0@example.org>", "2026-11-03T20:00:00+08:00", REPLY))  # 更早的一份后来才到
    [late_id] = deliver(world, late)
    batch = load_queued(world.settings, "prod", packet_ids=[late_id])
    assert read == [second]  # 第 2 封没写过账，要读；第 1 封写过，不读
    assert quote_kind(batch, late_id) == QUOTED_ONLY and batch.requeue == [first]


def test_roles_from_an_older_l1_are_read_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """写账时的 L1 不是这一版的，记下的不算数，照旧读包。"""
    first, second = deliver(
        world,
        mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)),
        mail_packet(5, mail("<r2@example.org>", "2026-11-05T09:00:00+08:00", REPLY)),
    )
    written(world, load_queued(world.settings, "prod", packet_ids=[first]))
    world.execute("UPDATE quote_roles SET l1_version = 'l1-v0'")
    read = count_reads(monkeypatch)
    assert quote_kind(load_queued(world.settings, "prod", packet_ids=[second]), second) == QUOTED
    assert read == [first]


def test_one_known_original_is_enough(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """找到一份看得到的原话就停：别的没写过账的包不用读。"""
    original, unwritten, reply = deliver(
        world,
        mail_packet(3, lena_original()),
        mail_packet(4, mail("<x1@example.net>", "2026-11-03T10:00:00+08:00", [CRATES], subject="Crates again", reply_to=None,
                            sender='{name: "Ann Lee", addr: "ann.lee@example.net"}')),
        mail_packet(5, mail("<r1@example.org>", "2026-11-05T09:00:00+08:00", REPLY, reply_to=None)),
    )
    written(world, load_queued(world.settings, "prod", packet_ids=[original]))
    read = count_reads(monkeypatch)
    assert quote_kind(load_queued(world.settings, "prod", packet_ids=[reply]), reply) == QUOTED
    assert unwritten not in read



# ---------- 第五轮审阅第 1 条：认成插话的引用行，撤回照样挡得住 ----------

PI = "[Tom] Please send the revised PI for order 7 by Friday."


def lena_with_tom_line() -> str:
    """Lena 的信里有一行写着 [Tom] 的话（她贴进来的）。"""
    return mail("<orig@example.net>", "2026-11-03T09:12:00+08:00", ["Notes from the call:", PI], subject="Crates", reply_to=None,
                sender='{name: "Lena Ortiz", addr: "lena.ortiz@example.net"}')


def tom_replies() -> str:
    return mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", outlook_reply(["Notes from the call:", PI]), sender=TOM)


def test_a_withdrawn_line_is_not_brought_back_as_an_insertion(world: World) -> None:
    """第五轮审阅第 1 条：引用里认成回信的人插话的行改成了 new，撤回只标 > 行和正在当证据的那一份，挡不住：整包撤回
    Lena 那封以后，Tom 回她（引用不带 >），那一行就成了 Tom 新写的话。取包时插话行和引用一样过一遍撤回，对上的改回引用。"""
    [original] = deliver(world, mail_packet(3, lena_with_tom_line()))
    withdraw(world.settings, "prod", packets=[original])
    [reply] = deliver(world, mail_packet(4, tom_replies()))
    assert quote_kind(load_queued(world.settings, "prod", packet_ids=[reply]), reply, PI) == QUOTED


def test_withdrawing_the_source_withdraws_an_insertion_already_written(world: World) -> None:
    """先写了 Tom 的回信（那一行当插话、当证据），再撤回 Lena 那封：那一行也标上撤回，Tom 的包改回排队。"""
    original, reply = deliver(world, mail_packet(3, lena_with_tom_line()), mail_packet(4, tom_replies()))
    batch = load_queued(world.settings, "prod", packet_ids=[reply])
    assert quote_kind(batch, reply, PI) == NEW
    written(world, batch)
    withdraw(world.settings, "prod", packets=[original])
    [row] = world.rows("SELECT withdrawn_at FROM raw_sentences WHERE packet_id = ? AND text = ?", reply, PI)
    assert row["withdrawn_at"] is not None
    assert world.packet_row(reply)["extraction"] == "queued"
