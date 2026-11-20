"""邮件包的单条撤回只管那一封信（D73 第 16、17 条，D79 第 10 条）。样例都是编的。

- 单条撤回的记录带上是哪一封信（message_id）：同一个包里同一个发件人别的信里一字不差的话（称呼、署名、发件人、
  收件人、附件名那一行）不跟着标，那封信的附件也不跟着撤。
- 按事实撤回只撤证据那几句，不整封撤：证据里有主题、发件人那一句的，查询结果、发给模型的窗口、账本里的说话人
  换成“[已撤回]”，这封信别的句子照常看得到。点名撤回主题、发件人那一句还是整封撤（D73 第 11 条）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import MAIL_ID, World, build_mail_packet, mail_header_block
from test_withdraw_review import exported, rebuilt_world
from test_write import l3_fact, write

from javmem.cli import main
from javmem.decisions import export_decisions, import_decisions
from javmem.export import facts as export_facts
from javmem.ledger import connect, connect_readonly, load_migrations, migrate
from javmem.packet.render import attachment_block, attachment_section
from javmem.pipeline.batch import load_queued
from javmem.pipeline.render import render_window
from javmem.pipeline.window import Window, build_windows
from javmem.raw import raw_search
from javmem.redaction import default_patterns
from javmem.review import freeze
from javmem.withdraw import export_records, withdraw

PATTERNS = default_patterns()
SENDER = "Tran Mai <tran.mai@example.org>"
MASK = "[已撤回]"
ATTACHMENTS = '[{name: "报价单.pdf", type: "application/pdf", size: 100}]'


def mail_section(when: str, message: str, subject: str, lines: list[str], *, attachments: bool = True) -> str:
    """一封 Tran Mai 发的信：称呼、正文、署名，和另一封一字不差的只有正文。"""
    header = mail_header_block(
        message_id=f'"<{message}@example.org>"',
        thread_id=f'"<{message}@example.org>"',
        date=f"2026-11-03T{when}:00+07:00",
        subject=f'"{subject}"',
        attachments=ATTACHMENTS if attachments else None,
    )
    body = "".join(f"  {line}\n" for line in lines)
    return f"## 原话 · 邮件 · 2026-11-03 {when}\n\n{header}\n  Hi Ming,\n\n{body}\n  Best regards,\n  Tran Mai\n\n"


def two_mails(first: list[str] | None = None, second: list[str] | None = None, *, revision: int = 1) -> str:
    """同一个发件人的两封信，各带一个同名的附件。第 1 封正文是第 3、4 行，第 2 封也是。"""
    first = first or ["方案明天发给你。", "报价 12.5 美元。"]
    second = second or ["越南那批订单 11-15 出货。", "运费 3.5 美元。"]
    blocks = [
        attachment_block(name="报价单.pdf", media_type="application/pdf", size=100, of_section=number, of_line=None,
                         state="full", reason=None, text=text)
        for number, text in ((1, "甲方报价 9.8 美元。"), (2, "乙方报价 7.6 美元。"))
    ]
    sections = (
        mail_section("09:12", "a", "收购价 9.9 亿的保密方案", first)
        + mail_section("10:12", "b", "越南订单出货", second)
        + attachment_section([], blocks)
    )
    fields = {"revision": revision} if revision > 1 else {}
    return build_mail_packet(sections=sections, **fields)


def delivered(world: World, packet: str | None = None) -> None:
    world.drop("inbox/mail-sales-a", packet or two_mails())
    world.intake()


def withdrawn(world: World) -> set[str]:
    rows = world.rows("SELECT sentence_id FROM raw_sentences WHERE withdrawn_at IS NOT NULL")
    return {row["sentence_id"] for row in rows}


def search(world: World, query: str, **arguments: object) -> list[dict]:
    result = raw_search({"query": query, **arguments}, settings=world.as_caller("owner-console").settings)
    assert result["ok"], result
    return result["items"]


def ids(items: list[dict]) -> set[str]:
    return {item["id"] for item in items}


def rendered(world: World) -> str:
    batch = load_queued(world.settings, "prod")
    return "\n".join(render_window(window, PATTERNS).text for window in build_windows(batch.packets, context_pool=batch.context))


# ---------- D73 第 17 条：单条撤回只挡同一封信里的 ----------


def test_one_sentence_does_not_spill_into_the_same_senders_other_mail(world: World) -> None:
    delivered(world)
    report = withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.1.1"])  # 第 1 封的“Hi Ming,”
    assert report.sentences == [f"{MAIL_ID}#1.1.1"]
    assert withdrawn(world) == {f"{MAIL_ID}#1.1.1"}
    assert ids(search(world, "Hi Ming")) == {f"{MAIL_ID}#2.1.1"}
    assert f"{MAIL_ID}#2.1.1" in report.copies_elsewhere  # 另一封里一字不差的报出来，要人看


def test_withdrawing_a_whole_mail_leaves_the_same_senders_other_mail_alone(world: World) -> None:
    delivered(world)
    report = withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.subject"])  # 第 11 条：点名主题，整封撤
    assert report.mails == [f"{MAIL_ID}#1"]
    assert all(sentence.startswith((f"{MAIL_ID}#1.", f"{MAIL_ID}#attachment.1.")) for sentence in withdrawn(world))
    assert {f"{MAIL_ID}#1.from", f"{MAIL_ID}#1.attachments", f"{MAIL_ID}#attachment.1.1"} <= withdrawn(world)
    # 第 2 封的发件人、收件人、附件名、称呼、署名都一字不差，不跟着标
    for sentence in ("2.from", "2.to", "2.attachments", "2.1.1", "2.6.1", "2.7.1"):
        assert f"{MAIL_ID}#{sentence}" not in withdrawn(world)


# ---------- D79 第 10 条：同一个人别的信的附件不跟着撤 ----------


def test_the_other_mails_attachment_stays(world: World) -> None:
    delivered(world)
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.from"])  # 整封撤第 1 封
    assert search(world, "甲方报价") == []
    assert ids(search(world, "乙方报价")) == {f"{MAIL_ID}#attachment.2.1"}
    assert "乙方报价 7.6 美元。" in rendered(world)


def test_withdrawing_one_attachment_line_keeps_the_other_mails_identical_line(world: World) -> None:
    same = "价格表有效期 30 天。"
    blocks = [
        attachment_block(name="报价单.pdf", media_type="application/pdf", size=100, of_section=number, of_line=None,
                         state="full", reason=None, text=same)
        for number in (1, 2)
    ]
    sections = (
        mail_section("09:12", "a", "第一封", ["方案明天发给你。"])
        + mail_section("10:12", "b", "第二封", ["运费 3.5 美元。"])
        + attachment_section([], blocks)
    )
    delivered(world, build_mail_packet(sections=sections))
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#attachment.1.1"])
    assert ids(search(world, "有效期")) == {f"{MAIL_ID}#attachment.2.1"}


# ---------- 记录带着是哪一封信：新版本、整本重建 ----------


def test_a_new_revision_marks_only_the_same_mail(world: World) -> None:
    delivered(world)
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.1.1"])
    delivered(world, two_mails(revision=2))
    assert world.packet_row(MAIL_ID, 2)["state"] == "current"
    assert withdrawn(world) == {f"{MAIL_ID}#1.1.1"}


def test_exported_records_say_which_mail_and_a_rebuild_keeps_to_it(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered(world)
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.1.1"])
    [record] = export_records(world.settings, "prod")["sentences"]
    assert record["message_id"] == "<a@example.org>"
    records_file = exported(world, tmp_path)
    rebuilt = rebuilt_world(tmp_path, monkeypatch)
    assert main(["withdraw", "--library", "prod", "--import", str(records_file)]) == 0
    delivered(rebuilt)
    assert withdrawn(rebuilt) == {f"{MAIL_ID}#1.1.1"}


def test_records_imported_after_the_packet_arrived_keep_to_their_mail(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered(world)
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.1.1"])
    records_file = exported(world, tmp_path)
    rebuilt = rebuilt_world(tmp_path, monkeypatch)
    delivered(rebuilt)  # 先收了包，再导记录：导进来以后接着撤回这本账里已经有的
    assert main(["withdraw", "--library", "prod", "--import", str(records_file)]) == 0
    assert withdrawn(rebuilt) == {f"{MAIL_ID}#1.1.1"}


def test_undoing_a_packet_releases_quotes_mail_by_mail(world: World) -> None:
    """撤回一个对话包时，邮件包里两封信引用它那句的行各记各的；解除撤回时两封都放回来。"""
    from test_write import day_packet

    said = "越南那批订单 11-15 出货，别再改了。"
    world.drop("inbox/desk-bot", day_packet("2026-11-01", f"我：{said}"))
    quoting = [f"> {said}", "收到。"]
    delivered(world, two_mails(quoting, quoting))
    report = withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
    assert {f"{MAIL_ID}#1.3.1", f"{MAIL_ID}#2.3.1"} <= set(report.sentences)
    from javmem.withdraw import unwithdraw

    unwithdraw(world.settings, "prod", ["2026-11-01_2330_desk-bot"])
    assert withdrawn(world) == set()


def test_migration_keeps_old_records_packet_wide_and_fills_in_which_mail_the_evidence_is(tmp_path: Path) -> None:
    migrations = load_migrations()
    conn = connect(tmp_path / "ledger.db")
    migrate(conn, [m for m in migrations if m.version <= 10])
    conn.execute("PRAGMA foreign_keys = OFF")  # 只测这两张表，不登记包
    conn.execute("INSERT INTO withdrawn_sentences (packet_id, sentence_hash, withdrawn_at, cause) VALUES ('P', 'h', 't', 'Q')")
    conn.execute(
        "INSERT INTO raw_sentences (sentence_id, packet_id, revision, seq, section_kind, section_no, line_no, sentence_no,"
        " text, speaker, speaker_role, said_at, said_at_epoch, origin, message_id)"
        " VALUES ('P#1.3.1', 'P', 2, 1, 'raw', 1, 3, 1, 't', 's', 'other', 't', 0, 'external', '<a@example.org>')"
    )
    for fact_id, sentence_id, revision in (("F1", "P#1.3.1", 2), ("F2", "P#1.3.1", 1), ("F3", "P#2.3.1", 2)):
        conn.execute(  # 第 1 版的那句、第 2 封那句不在原文索引里
            "INSERT INTO evidence (fact_id, packet_id, revision, sentence_id, quote, position, created_at)"
            " VALUES (?, 'P', ?, ?, 'q', '{}', 't')",
            (fact_id, revision, sentence_id),
        )
    assert migrate(conn) == [m.version for m in migrations if m.version > 10]
    rows = conn.execute("SELECT packet_id, message_id, sentence_hash, cause FROM withdrawn_sentences").fetchall()
    assert [tuple(row) for row in rows] == [("P", "", "h", "Q")]
    found = conn.execute("SELECT sentence_id, revision, message_id FROM evidence ORDER BY evidence_id").fetchall()
    assert [tuple(row) for row in found] == [("P#1.3.1", 2, "<a@example.org>"), ("P#1.3.1", 1, None), ("P#2.3.1", 2, None)]
    # 同一个指纹，两封信各记一条
    conn.execute("INSERT INTO withdrawn_sentences (packet_id, message_id, sentence_hash, withdrawn_at) VALUES ('P', '<a@example.org>', 'h', 't')")
    conn.execute("INSERT INTO withdrawn_sentences (packet_id, message_id, sentence_hash, withdrawn_at) VALUES ('P', '<b@example.org>', 'h', 't')")
    assert conn.execute("SELECT count(*) FROM withdrawn_sentences").fetchone()[0] == 3
    conn.close()


def test_records_from_before_the_mail_was_recorded_still_cover_the_whole_packet(world: World) -> None:
    """迁移以前的记录（没有 message_id）照旧挡整个包里同一个人一字不差的话：宁可多挡，不会漏。"""
    delivered(world)
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.1.1"])
    world.execute("UPDATE withdrawn_sentences SET message_id = ''")
    delivered(world, two_mails(revision=2))
    assert withdrawn(world) == {f"{MAIL_ID}#1.1.1", f"{MAIL_ID}#2.1.1"}


# ---------- D73 第 16 条：按事实撤回只撤证据那几句，主题、发件人换成“[已撤回]” ----------


def line_of(window: Window, phrase: str) -> tuple[str, str]:
    line = next(line for line in window.lines if line.target and phrase in line.text)
    return line.ref, line.text


def evidence(window: Window, *phrases: str) -> list[dict[str, str]]:
    return [{"ref": ref, "quote": text} for ref, text in (line_of(window, phrase) for phrase in phrases)]


def mail_extract(window: Window) -> dict[str, object]:
    """主题加正文撑着的一条、发件人撑着的一条，正文、附件各撑一条。"""
    shown = " ".join(line.text for line in window.targets)
    found = []
    if "保密方案" in shown:
        found.append(l3_fact(window, "保密方案", "收购价 9.9 亿的保密方案", evidence=evidence(window, "保密方案", "方案明天发给你")))
        found.append(l3_fact(window, "tran.mai@example.org", SENDER))
        found.append(l3_fact(window, "报价 12.5", "报价 12.5 美元。"))
        found.append(l3_fact(window, "甲方报价", "甲方报价 9.8 美元。"))
    if "11-15" in shown:
        found.append(l3_fact(window, "11-15", "越南那批订单 11-15 出货。"))
    return {"entities": [], "facts": found}


def stocked(world: World) -> dict[str, str]:
    delivered(world)
    write(world, mail_extract).close()
    return {row["content"]: row["fact_id"] for row in world.rows("SELECT content, fact_id FROM facts")}


def exported_speakers(world: World) -> dict[str, str]:
    conn = connect_readonly(world.settings.library("prod").db_path)
    try:
        return {str(fact["content"]): str(fact["speaker"]) for fact in export_facts(conn)}
    finally:
        conn.close()


def test_withdrawing_a_fact_backed_by_the_subject_keeps_the_rest_of_the_mail(world: World) -> None:
    facts = stocked(world)
    assert set(facts) >= {"收购价 9.9 亿的保密方案", "报价 12.5 美元。", "甲方报价 9.8 美元。"}
    report = withdraw(world.settings, "prod", facts=[facts["收购价 9.9 亿的保密方案"]])
    assert report.mails == []  # 不整封撤
    assert withdrawn(world) == {f"{MAIL_ID}#1.subject", f"{MAIL_ID}#1.3.1"}
    remaining = {row["content"] for row in world.rows("SELECT content FROM facts")}
    assert {"报价 12.5 美元。", "甲方报价 9.8 美元。", SENDER, "越南那批订单 11-15 出货。"} <= remaining
    # 这封信别的句子还查得到，带的主题换成了“[已撤回]”；另一封的主题不动
    [item] = search(world, "报价 12.5")
    assert item["evidence"][0]["email"]["subject"] == MASK
    [attached] = search(world, "甲方报价")
    assert attached["evidence"][0]["email"]["subject"] == MASK
    [other] = search(world, "11-15")
    assert other["evidence"][0]["email"]["subject"] == "越南订单出货"
    text = rendered(world)
    assert "保密方案" not in text and "报价 12.5 美元。" in text


def test_withdrawing_a_fact_backed_by_the_sender_masks_the_sender_of_that_mail(world: World) -> None:
    facts = stocked(world)
    report = withdraw(world.settings, "prod", facts=[facts[SENDER]])
    assert report.mails == []
    assert withdrawn(world) == {f"{MAIL_ID}#1.from"}
    [item] = search(world, "报价 12.5", context=2)
    assert item["evidence"][0]["speaker"] == MASK
    assert all(row["speaker"] == MASK for row in item["context"]["before"] + item["context"]["after"])
    [attached] = search(world, "甲方报价")
    assert attached["evidence"][0]["speaker"] == MASK
    [other] = search(world, "11-15")
    assert other["evidence"][0]["speaker"] == SENDER  # 同一个人另一封信的发件人不动
    # 按发件人查，查不到这封信的句子
    assert ids(search(world, "美元", speaker=SENDER)) == {f"{MAIL_ID}#2.4.1", f"{MAIL_ID}#attachment.2.1"}
    # 发给模型的窗口里，这封信和它的附件的抬头写“[已撤回]”
    text = rendered(world)
    assert f"邮件，发件人 {MASK}：" in text
    assert f"邮件附件 报价单.pdf，发件人 {MASK}（外部内容）：" in text
    # 账本里这封信撑着的事实，说话人马上换掉，不等重抽
    speakers = exported_speakers(world)
    assert speakers["报价 12.5 美元。"] == MASK and speakers["甲方报价 9.8 美元。"] == MASK
    assert speakers["越南那批订单 11-15 出货。"] == SENDER
    assert "tran.mai" not in json.dumps(
        [json.loads(row["proofs"]) for row in world.rows("SELECT proofs FROM fact_claims WHERE fact_id = ?", facts["报价 12.5 美元。"])]
    )


def test_who_a_claim_is_from_is_masked_along_with_the_sender(world: World) -> None:
    """J4.1：L3 写的“这条说法出自谁”是看着信的抬头写的，多半就是发件人。发件人那一句按事实撤回了，这封信撑着的说法、
    事实的这一列也马上换成“[已撤回]”，不等重抽；另一封信的不动。"""

    def extract(window: Window) -> dict[str, object]:
        found = mail_extract(window)
        for fact in found["facts"]:  # type: ignore[union-attr]
            fact["speaker"] = "Tran Mai"
        return found

    delivered(world)
    write(world, extract).close()
    facts = {row["content"]: row["fact_id"] for row in world.rows("SELECT content, fact_id FROM facts")}
    withdraw(world.settings, "prod", facts=[facts[SENDER]])
    shown = {row["content"]: row["attributed_to"] for row in world.rows("SELECT content, attributed_to FROM facts")}
    assert shown["报价 12.5 美元。"] == MASK and shown["甲方报价 9.8 美元。"] == MASK
    assert shown["越南那批订单 11-15 出货。"] == "Tran Mai"
    claims = world.rows("SELECT attributed_to FROM fact_claims WHERE fact_id IN (?, ?)", facts["报价 12.5 美元。"], facts["甲方报价 9.8 美元。"])
    assert {row["attributed_to"] for row in claims} == {MASK}
    assert "Tran Mai" not in json.dumps([dict(row) for row in world.rows(
        "SELECT * FROM fact_claims WHERE fact_id IN (?, ?)", facts["报价 12.5 美元。"], facts["甲方报价 9.8 美元。"])], ensure_ascii=False)


SECOND_MAILBOX = (
    "  mail-sales-b:\n"
    "    note: 业务员 B 的邮箱（编的）\n"
    "    framework: mail-import\n"
    "    env: prod\n"
    "    channel: email\n"
    "    mailbox: chen.jing@example.com\n"
    "    default_domain: [business]\n"
    "    may_carry: []\n"
)


def test_the_same_mail_in_another_mailboxs_packet_keeps_its_sender(world: World) -> None:
    """query-api 3.2：[已撤回] 只换撤回了那一句的那个包（D87 审阅）。同一封信（同一个 message_id）也进了另一个邮箱的包，
    那边的发件人那一句没撤回，照旧写原来的发件人，按 speaker 也查得到；撤回的报告把它列进 copies_elsewhere。"""
    sources = world.settings.sources_file
    sources.write_text(sources.read_text(encoding="utf-8") + SECOND_MAILBOX, encoding="utf-8")
    other = "2026-11-03_1800_mail-sales-b"
    delivered(world)
    world.drop("inbox/mail-sales-b", two_mails().replace("mail-sales-a", "mail-sales-b").replace(
        "mailbox: li.ming@example.com", "mailbox: chen.jing@example.com"))
    world.intake()

    def extract(window: Window) -> dict[str, object]:
        """“发件人是谁”这条只在 A 邮箱的窗口里抽到。"""
        shown = " ".join(line.text for line in window.targets)
        found = [l3_fact(window, "报价 12.5", "报价 12.5 美元。")] if "报价 12.5" in shown else []
        if window.source == "mail-sales-a" and "tran.mai@example.org" in shown:
            found.append(l3_fact(window, "tran.mai@example.org", SENDER))
        return {"entities": [], "facts": found}

    write(world, extract).close()
    facts = {row["content"]: row["fact_id"] for row in world.rows("SELECT content, fact_id FROM facts")}
    report = withdraw(world.settings, "prod", facts=[facts[SENDER]])
    assert report.masked == [f"{MAIL_ID}#1"]
    assert f"{other}#1.from" in report.copies_elsewhere
    shown = {item["evidence"][0]["packet_id"]: item["evidence"][0] for item in search(world, "报价 12.5")}
    assert shown[MAIL_ID]["speaker"] == MASK and shown[other]["speaker"] == SENDER
    assert shown[MAIL_ID]["email"]["message_id"] == shown[other]["email"]["message_id"] == "<a@example.org>"
    by_sender = ids(search(world, "美元", speaker=SENDER))
    assert f"{other}#1.4.1" in by_sender and f"{MAIL_ID}#1.4.1" not in by_sender


def test_naming_the_subject_still_withdraws_the_whole_mail(world: World) -> None:
    stocked(world)
    report = withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.subject"])
    assert report.mails == [f"{MAIL_ID}#1"]
    remaining = {row["content"] for row in world.rows("SELECT content FROM facts")}
    assert "报价 12.5 美元。" not in remaining and "越南那批订单 11-15 出货。" in remaining


@pytest.mark.parametrize("legacy", [False, True], ids=["evidence-knows-its-mail", "evidence-from-before-0011"])
def test_a_frozen_fact_left_without_claims_gets_the_sender_masked_too(world: World, legacy: bool) -> None:
    """包重发以后只剩冻结标记撑着的事实（证据指着旧版本那封信）：这封信的发件人按事实撤回了，它的说话人也换掉。
    迁移 0011 补不上是哪封信的旧证据（旧版本已经不在原文索引里）也一样（第二遍审阅 N3）。"""

    def extract(window: Window) -> dict[str, object]:
        shown = " ".join(line.text for line in window.targets)
        found = [l3_fact(window, "tran.mai@example.org", SENDER)] if "保密方案" in shown else []
        for price in ("报价 12.5", "报价 13"):
            if price in shown:
                found.append(l3_fact(window, price, f"{price} 美元。", speaker="Tran Mai 的信"))  # J4.1：说法出自谁
        return {"entities": [], "facts": found}

    delivered(world)
    write(world, extract).close()
    frozen = world.rows("SELECT fact_id FROM facts WHERE content = '报价 12.5 美元。'")[0]["fact_id"]
    freeze(world.settings, "prod", [frozen])
    delivered(world, two_mails(["方案明天发给你。", "报价 13 美元。"], revision=2))  # 那一句改了：说法没了
    write(world, extract).close()
    assert world.rows("SELECT count(*) AS n FROM fact_claims WHERE fact_id = ?", frozen)[0]["n"] == 0
    assert world.rows("SELECT attributed_to FROM facts WHERE fact_id = ?", frozen)[0]["attributed_to"] == "Tran Mai 的信"
    if legacy:
        world.execute("UPDATE evidence SET message_id = NULL WHERE fact_id = ?", frozen)
    sender_fact = world.rows("SELECT fact_id FROM facts WHERE content = ?", SENDER)[0]["fact_id"]
    withdraw(world.settings, "prod", facts=[sender_fact])
    assert world.rows("SELECT speaker FROM facts WHERE fact_id = ?", frozen)[0]["speaker"] == MASK
    assert exported_speakers(world)["报价 12.5 美元。"] == MASK
    # 说法出自谁不一定是发件人的原样（这里写的是“某某的信”），证据在这封信里的也换掉（J4.1）
    assert world.rows("SELECT attributed_to FROM facts WHERE fact_id = ?", frozen)[0]["attributed_to"] == MASK


def test_a_brought_back_frozen_fact_does_not_bring_back_the_withdrawn_sender(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冻结标记先导出，后按事实撤回了发件人那一句，再导出撤回记录、整本重建：L3 这次没抽到那条冻结的，照导出时的样子
    补回来。新账本里这封信的发件人那一句是撤回了的，补回来的说话人、说法出自谁也写“[已撤回]”（J4.1 审阅第 1 条；
    说话人那一半以前就会这样）。"""

    def extract(window: Window) -> dict[str, object]:
        shown = " ".join(line.text for line in window.targets)
        found = []
        if "保密方案" in shown:
            found.append(l3_fact(window, "tran.mai@example.org", SENDER))
            found.append(l3_fact(window, "报价 12.5", "报价 12.5 美元。", speaker="Tran Mai 的信"))
        return {"entities": [], "facts": found}

    delivered(world)
    write(world, extract).close()
    price = world.rows("SELECT fact_id FROM facts WHERE content = '报价 12.5 美元。'")[0]["fact_id"]
    sender = world.rows("SELECT fact_id FROM facts WHERE content = ?", SENDER)[0]["fact_id"]
    freeze(world.settings, "prod", [price])
    decisions = export_decisions(world.settings, "prod")  # 先导出冻结标记
    withdraw(world.settings, "prod", facts=[sender])  # 再撤回发件人那一句
    records = exported(world, tmp_path)

    new = rebuilt_world(tmp_path, monkeypatch)
    assert main(["withdraw", "--library", "prod", "--import", str(records)]) == 0
    delivered(new)
    write(new, lambda window: {"entities": [], "facts": []}).close()  # 这次 L3 没抽到那条
    assert import_decisions(new.settings, "prod", decisions).recreated == [price]
    row = new.rows("SELECT speaker, attributed_to, frozen, withdrawn_at FROM facts WHERE fact_id = ?", price)[0]
    assert (row["speaker"], row["attributed_to"], row["frozen"], row["withdrawn_at"]) == (MASK, MASK, 1, None)
    [hit] = search(new, "报价 12.5")
    assert hit["evidence"][0]["speaker"] == MASK


# ---------- D79 第 14 条：邮件只认邮件头里写附件名的那一句 ----------


def test_a_mail_body_line_naming_the_attachment_does_not_take_it_along(world: World) -> None:
    """规范第 3 节：写着附件名的那一句撤回了附件也撤回，邮件是邮件头的附件名那一句。正文里提到附件名的不算。"""
    delivered(world, two_mails(["附件是报价单.pdf，请查收。", "报价 12.5 美元。"]))
    report = withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.3.1"])
    assert report.sentences == [f"{MAIL_ID}#1.3.1"]
    assert ids(search(world, "甲方报价")) == {f"{MAIL_ID}#attachment.1.1"}
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.attachments"])  # 邮件头的那一句撤回了，附件跟着撤
    assert search(world, "甲方报价") == []
    assert ids(search(world, "乙方报价")) == {f"{MAIL_ID}#attachment.2.1"}


def test_a_chat_line_naming_the_attachment_still_takes_it_along() -> None:
    from javmem.packet import parse_packet
    from javmem.pipeline.normalize import WITHDRAWN, normalize
    from test_attachments import CHAT_ID, FILE_PACKET

    packet = parse_packet(FILE_PACKET.encode("utf-8"), filename=f"{CHAT_ID}.md")
    units = normalize(packet, withdrawn={f"{CHAT_ID}#1.2.1"}).units  # 对话里写着名字的那一句
    assert all(line.kind == WITHDRAWN for unit in units for line in unit.lines if line.attachment is not None)


# ---------- 冻结事实的证据按指纹认撤回，也只认同一封信 ----------


def test_a_frozen_facts_evidence_in_another_mail_is_not_taken_for_the_withdrawn_one(world: World) -> None:
    same = "价格表见附件，有效期 30 天。"
    delivered(world, two_mails([same], [same]))

    def second_only(window: Window) -> dict[str, object]:
        line = next((line for line in window.lines if line.target and line.sentence_id == f"{MAIL_ID}#2.3.1"), None)
        if line is None:
            return {"entities": [], "facts": []}
        return {"entities": [], "facts": [l3_fact(window, same, "有效期 30 天", evidence=[{"ref": line.ref, "quote": line.text}])]}

    write(world, second_only).close()
    [fact_id] = [row["fact_id"] for row in world.rows("SELECT fact_id FROM facts")]
    freeze(world.settings, "prod", [fact_id])
    # 新版本里第 2 封那一句改了：说法没了，冻结的事实还留着，证据指着第 1 版第 2 封
    delivered(world, two_mails([same], ["价格表见附件，有效期 60 天。"], revision=2))
    assert world.rows("SELECT count(*) AS n FROM fact_claims")[0]["n"] == 0
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.3.1"])  # 撤回第 1 封里一字不差的那一句
    [row] = world.rows("SELECT withdrawn_at FROM facts WHERE fact_id = ?", fact_id)
    assert row["withdrawn_at"] is None  # 第 2 封的证据不算撤回了，不藏


# ---------- 审阅提的（D73 第 16 条以后）：发件人换成“[已撤回]”以后别的地方 ----------


def long_mail(lines: int = 380) -> str:
    """一封很长的信：L2 把它切成好几段，第 4 段以后的窗口里，上文只够带前面几段的末尾，带不到发件人那一句，
    可写给模型的抬头里有发件人。发件人的地址带数字，假模型会把发件人那一句抽成一条事实。"""
    header = mail_header_block(
        message_id='"<a@example.org>"', thread_id='"<a@example.org>"',
        **{"from": '{name: "Tran Mai", addr: "tm2@example.org"}'}, subject='"Quote"',
    )
    body = "  Hi Ming,\n\n" + "".join(
        f"  第 {n} 项 价格 {n}.5 美元，数量 {n * 3} 件，交货期 {n % 28 + 1} 天，产地越南，包装纸箱，付款方式电汇，备注无特别要求。\n"
        for n in range(1, lines)
    )
    return build_mail_packet(sections=f"## 原话 · 邮件 · 2026-11-03 09:12\n\n{header}\n{body}\n")


def stored_texts(world: World) -> list[str]:
    from javmem.pipeline.store import ResultStore

    with ResultStore(world.data / "l3") as store:
        return [row["input_text"] for row in store.windows()]


def test_the_result_store_keeps_no_window_with_the_masked_sender(world: World) -> None:
    """审阅第 1 条：结果库只删清单里有撤回的句子的窗口；长信后面几段的窗口清单里没有发件人那一句，抬头里却有发件人。"""
    delivered(world, long_mail())
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert sum("tm2@example.org" in text for text in stored_texts(world)) >= 4
    [sender] = [row["fact_id"] for row in world.rows(
        "SELECT DISTINCT fact_id FROM evidence WHERE sentence_id = ? AND fact_id IS NOT NULL", f"{MAIL_ID}#1.from")]
    report = withdraw(world.settings, "prod", facts=[sender])
    assert report.masked == [f"{MAIL_ID}#1"]
    assert not any("tm2@example.org" in text for text in stored_texts(world))
    # 重抽以后的窗口（抬头写“[已撤回]”）以后再撤回别的也不删：不会每撤回一次就重抽一遍
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    kept = stored_texts(world)
    assert kept and all("tm2@example.org" not in text for text in kept)
    withdraw(world.settings, "prod")  # 不写目标：只清一遍
    assert stored_texts(world) == kept


@pytest.mark.parametrize("resent", [False, True], ids=["rerun", "resent-before-rerun"])
def test_cleaning_the_store_for_a_masked_sender_picks_up_after_an_interruption(
    world: World, monkeypatch: pytest.MonkeyPatch, resent: bool
) -> None:
    """撤回的账本那个事务提交了、结果库还没删完就断了：不写目标再跑一次，接着把这封信的窗口删掉。再跑以前这个包
    又重发了一版的也一样（第二遍审阅 N2：按那一版找这封信的句子，新版本来了就找不到了）。"""
    from javmem.pipeline.store import ResultStore

    delivered(world, long_mail())
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    [sender] = [row["fact_id"] for row in world.rows(
        "SELECT DISTINCT fact_id FROM evidence WHERE sentence_id = ? AND fact_id IS NOT NULL", f"{MAIL_ID}#1.from")]

    def cut(*args: object, **kwargs: object) -> int:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(ResultStore, "delete_windows", cut)
        with pytest.raises(KeyboardInterrupt):
            withdraw(world.settings, "prod", facts=[sender])
    assert any("tm2@example.org" in text for text in stored_texts(world))
    assert world.rows("SELECT store_cleaned_at FROM masked_mails")[0]["store_cleaned_at"] is None
    if resent:
        delivered(world, long_mail().replace("privacy: normal", "privacy: normal\nrevision: 2"))
        assert world.packet_row(MAIL_ID, 2)["state"] == "current"
    withdraw(world.settings, "prod")
    assert not any("tm2@example.org" in text for text in stored_texts(world))
    assert world.rows("SELECT store_cleaned_at FROM masked_mails")[0]["store_cleaned_at"] is not None


def test_records_export_upgrades_an_old_ledger_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 5 条：代码升级了、账本还没打开过（还是第 10 版），整本重建的第一步导出撤回记录就报没有 message_id 这一列。"""
    data = tmp_path / "data"
    (data / "ledger").mkdir(parents=True)
    conn = connect(data / "ledger" / "prod.db")
    migrate(conn, [m for m in load_migrations() if m.version <= 10])
    conn.execute("INSERT INTO withdrawn_sentences (packet_id, sentence_hash, withdrawn_at, cause) VALUES ('P', 'h', 't', NULL)")
    conn.close()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    out = tmp_path / "records.json"
    assert main(["withdraw", "--library", "prod", "--export", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["sentences"][0]["message_id"] == ""
    assert main(["withdraw", "--library", "test", "--export", str(out)]) == 2  # 没有账本的库：不建一本空的
    assert not (data / "ledger" / "test.db").exists()


def test_a_masked_sender_with_the_owners_name_is_still_not_the_owner(world: World) -> None:
    """审阅（只读出来的）：发件人换成“[已撤回]”以后，窗口里认不出发件人叫什么，发件人就叫主人的名字时，
    L3 写的这个名字会挂到主人上。D62：邮件的发件人不挂到主人上，看不出发件人是谁的窗口也一样不按名字挂。"""
    from test_owner_names import NAMES, subjects, write_with_owner

    header = mail_header_block(**{"from": '{name: "王小明", addr: "xm9@example.org"}'})
    body = "  报价 12.5 美元。\n\n  王小明\n"
    delivered(world, build_mail_packet(sections=f"## 原话 · 邮件 · 2026-11-03 09:12\n\n{header}\n{body}\n"))

    def extract(window: Window) -> dict[str, object]:
        shown = " ".join(line.text for line in window.targets)
        entity = {"key": "e1", "mention": "王小明", "name": "王小明", "type": "人"}
        found = [l3_fact(window, "12.5", "王小明报价 12.5 美元", subject="e1")]
        if "xm9@example.org" in shown:
            found.append(l3_fact(window, "xm9@example.org", "王小明 <xm9@example.org>"))
        return {"entities": [entity], "facts": found}

    write_with_owner(world, extract, NAMES).close()
    assert subjects(world.ledger())["王小明报价 12.5 美元"] == "王小明"  # 发件人叫这个名字：不是主人
    sender = world.rows("SELECT fact_id FROM facts WHERE content = '王小明 <xm9@example.org>'")[0]["fact_id"]
    withdraw(world.settings, "prod", facts=[sender])
    write_with_owner(world, extract, NAMES).close()  # 改回排队的包重写：窗口里的发件人是“[已撤回]”
    assert subjects(world.ledger())["王小明报价 12.5 美元"] != "主人"
