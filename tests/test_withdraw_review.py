"""J2.11 到 J2.13、D62 审阅以后补的测试：每一条都是审阅复现出来的情况，修之前是红的。样例都是编的。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import MAIL_ID, World, build_mail_packet, build_packet, mail_header_block
from test_intake_recovery import PowerCut
from test_owner_names import subjects, write_with_owner
from test_review import by_value, frozen_and_contradicted, reviews
from test_withdraw import P1, TWO_DAYS, evidence_of, extract_and_write, facts, freeze, search, stocked, store_text
from test_write import PATTERNS, day_packet, deliver, l3_fact, write

import javmem.cli as cli
import javmem.withdraw as withdraw_module
from javmem.cli import main
from javmem.config import ConfigError, Settings
from javmem.export import facts as export_facts
from javmem.ledger import connect_readonly
from javmem.llm import FakeModel
from javmem.pipeline.batch import load_queued
from javmem.pipeline.render import render_window
from javmem.pipeline.store import StoreBusy
from javmem.pipeline.window import build_windows
from javmem.pipeline.write import LedgerWriter
from javmem.raw import raw_search, sentence_hash
from javmem.review import decide
from javmem.review import freeze as set_frozen
from javmem.withdraw import export_records, unwithdraw, withdraw

P2 = "2026-11-02_2330_desk-bot"
P3 = "2026-11-03_2330_desk-bot"
P4 = "2026-11-04_2330_desk-bot"
P5 = "2026-11-05_2330_desk-bot"
CONTENT = "越南那批订单 11-15 出货。"


def later(stamp: str, *lines: str) -> str:
    day = stamp[:10]
    return build_packet(
        stamp=stamp,
        body=f"## 原话 · {day} 09:00–09:01\n" + "".join(lines),
        created_at=f"{day}T23:30:00+08:00",
        covers_from=f"{day}T00:00:00+08:00",
        covers_to=f"{day}T23:30:00+08:00",
    )


# ---------- 审阅 1：彻底删除一版以后，原文句还在 ----------


def test_purging_the_current_revision_of_a_withdrawn_packet_deletes_its_raw_text(world: World) -> None:
    stocked(world, *TWO_DAYS)
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    assert main(["purge", "--library", "prod", "--packet", P1, "--revision", "1", "--yes"]) == 0
    assert world.rows("SELECT count(*) AS n FROM raw_sentences WHERE packet_id = ?", P1)[0]["n"] == 0
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 0  # 再整包删一次：没东西可删，也不出错
    assert world.rows("SELECT count(*) AS n FROM raw_fts WHERE raw_fts MATCH '\"11-15\"'")[0]["n"] == 0
    assert "11-15 出货".encode() not in (world.data / "ledger" / "prod.db").read_bytes()


# ---------- 审阅 2：邮件的主题、发件人跟着整封信走 ----------


def mail_with(subject: str) -> str:
    body = "## 原话 · 邮件 · 2026-11-03 09:12\n\n" + mail_header_block(subject=f'"{subject}"') + "\n  明天上午开会讨论。\n"
    return build_mail_packet(sections=body)


def test_withdrawing_a_mail_subject_withdraws_the_whole_mail(world: World) -> None:
    world.drop("inbox/mail-sales-a", mail_with("收购价 9.9 亿的保密方案"))
    world.intake()
    report = withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.subject"])
    assert report.mails == [f"{MAIL_ID}#1"]
    owner = world.as_caller("owner-console").settings
    assert raw_search({"query": "保密方案"}, settings=owner)["items"] == []
    assert raw_search({"query": "开会讨论"}, settings=owner)["items"] == []  # 正文那一句带着主题，也撤回了


def test_withdrawing_a_mail_sender_keeps_it_out_of_the_window(world: World) -> None:
    world.drop("inbox/mail-sales-a", mail_with("周会"))
    world.intake()
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.from"])
    batch = load_queued(world.settings, "prod")
    windows = build_windows(batch.packets, context_pool=batch.context)
    assert all("tran.mai@example.org" not in render_window(window, PATTERNS).text for window in windows)


def test_a_reply_subject_carrying_the_withdrawn_subject_is_reported(world: World) -> None:
    second = (
        "## 原话 · 邮件 · 2026-11-03 10:00\n\n"
        + mail_header_block(
            message_id='"<b@example.org>"',
            in_reply_to='"<a@example.org>"',
            date="2026-11-03T10:00:00+07:00",
            subject='"Re: 收购价 9.9 亿的保密方案"',
        )
        + "\n  好的。\n"
    )
    first = "## 原话 · 邮件 · 2026-11-03 09:12\n\n" + mail_header_block(subject='"收购价 9.9 亿的保密方案"') + "\n  明天上午开会讨论。\n"
    world.drop("inbox/mail-sales-a", build_mail_packet(sections=first + "\n" + second))
    world.intake()
    report = withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.subject"])
    assert f"{MAIL_ID}#2.subject" in report.copies_elsewhere  # 回信主题里的“Re: 原主题”报出来，要人看


# ---------- 审阅 3：抽取先拿锁、再取包；撤回要拿结果库的锁 ----------


def test_extraction_holds_the_store_lock_before_cutting_windows(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    stocked(world, *TWO_DAYS)
    deliver(world, day_packet("2026-11-03", "我：卡号尾号 6688 的那笔 3 万先别动。"))
    real = cli.build_windows
    outcome: list[str] = []

    def build_then_withdraw(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        windows = real(*args, **kwargs)
        try:  # 另一个终端里的撤回，正好这时候跑：抽取已经拿着结果库的锁，撤回拿不到
            withdraw(world.settings, "prod", packets=[P3])
            outcome.append("withdrawn")
        except StoreBusy:
            outcome.append("busy")
        return windows

    monkeypatch.setattr(cli, "build_windows", build_then_withdraw)
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert outcome == ["busy"] and world.packet_row(P3)["withdrawn_at"] is None


def test_withdraw_takes_the_store_lock_even_before_the_store_exists(world: World) -> None:
    deliver(world, *TWO_DAYS)  # 还没抽过，结果库还没建
    assert not (world.data / "l3" / "l3.db").exists()
    report = withdraw(world.settings, "prod", packets=[P1])
    assert report.store and (world.data / "l3" / "l3.lock").exists()


def test_write_takes_its_lock_before_loading(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    stocked(world, *TWO_DAYS)
    deliver(world, day_packet("2026-11-03", "我：卡号尾号 6688 的那笔 3 万先别动。"))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    real = cli.load_queued
    order: list[str] = []

    def load(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        from javmem.pipeline.write import WriteBusy, WriteLock

        try:
            WriteLock(world.data).close()
            order.append("lock free")
        except WriteBusy:
            order.append("lock held")
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "load_queued", load)
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert order == ["lock held"]


# ---------- 审阅 4：撤回以后才到的引用、新版本里新加的复述 ----------


def test_a_quote_arriving_after_the_withdrawal_is_withdrawn_on_arrival(world: World) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.1"])
    deliver(world, later("2026-11-05_2330", "- [2026-11-05 09:00] 小李：这个还算数吗？\n", f"  > [引用 2026-11-01 10:30 我] {CONTENT}\n"))
    assert search(world, "11-15 出货") == []
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert "11-15" not in store_text(world)


def test_a_restatement_added_in_a_new_revision_is_withdrawn_on_arrival(world: World) -> None:
    owner = "我：越南那批订单 11 月 15 日出货，老周负责跟进。"
    stocked(world, day_packet("2026-11-01", owner))
    withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.1"])
    again = day_packet("2026-11-01", owner, "desk-bot：收到，越南那批订单 11 月 15 日出货，老周负责跟进。")
    deliver(world, again.replace("daily", "daily\nrevision: 2"))
    assert search(world, "老周负责跟进") == []


# ---------- 审阅 5：整本重建时把撤回的记录带过去 ----------


def test_withdrawal_records_carry_over_to_a_rebuilt_ledger(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", packets=[P1], sentences=[f"{P2}#1.2.1"])  # 一个包、P2 的“报价 12.5 美元。”
    records_file = tmp_path / "withdrawals.json"
    assert main(["withdraw", "--library", "prod", "--export", str(records_file)]) == 0
    records = json.loads(records_file.read_text(encoding="utf-8"))
    assert [item["packet_id"] for item in records["packets"]] == [P1] and len(records["sentences"]) == 1
    assert "12.5" not in records_file.read_text(encoding="utf-8")  # 只有指纹，不带原文

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(fresh))
    rebuilt = World(fresh.resolve(), Settings.from_env())
    assert main(["init"]) == 0
    assert main(["withdraw", "--library", "prod", "--import", str(records_file)]) == 0  # 收包以前导进来
    for packet in TWO_DAYS:
        rebuilt.drop("inbox/desk-bot", packet)
    outcomes = {(o.result, o.code) for o in rebuilt.intake().outcomes}
    assert ("skipped", "withdrawn") in outcomes  # 撤回过的包照样不收
    assert search(rebuilt, "12.5 美元") == []  # 单条撤回的那句按指纹标上了
    extract_and_write()
    assert set(facts(rebuilt)) == {"老周 9 点到仓库。"}


def test_importing_records_into_a_ledger_that_has_them_withdraws_them(world: World, tmp_path: Path) -> None:
    stocked(world, *TWO_DAYS)
    records = {"schema": "javmem-withdrawals/1", "packets": [{"packet_id": P1, "withdrawn_at": "2026-11-09T00:00:00+08:00"}], "sentences": []}
    report = withdraw(world.settings, "prod", records=records)
    assert report.packets == [P1] and search(world, "11-15 出货") == []


# ---------- 审阅 6：藏起来的冻结事实又有了说法 ----------


def test_a_hidden_frozen_fact_comes_back_when_another_packet_says_it(world: World) -> None:
    stocked(world, *TWO_DAYS)
    fact_id = freeze(world, CONTENT)
    withdraw(world.settings, "prod", packets=[P1])
    review_id = int(reviews(world)[0]["review_id"])
    deliver(world, day_packet("2026-11-03", f"我：{CONTENT}"))
    extract_and_write()
    row = world.rows("SELECT withdrawn_at, frozen FROM facts WHERE fact_id = ?", fact_id)[0]
    assert row["withdrawn_at"] is None and row["frozen"] == 1  # 有新的证据，放回来了，还冻结着
    assert evidence_of(world, fact_id) == [f"{P3}#1.1.1"]
    assert reviews(world) == []  # 那条“证据都撤回了”的待确认自动关掉了
    closed = [r for r in reviews(world, open_only=False) if r["review_id"] == review_id]
    assert [(r["decision"], r["decided_by"]) for r in closed] == [("resupported", "auto")]


def test_unwithdrawing_brings_a_hidden_frozen_fact_back(world: World) -> None:
    stocked(world, *TWO_DAYS)
    fact_id = freeze(world, CONTENT)
    withdraw(world.settings, "prod", packets=[P1])
    unwithdraw(world.settings, "prod", [P1])
    extract_and_write()
    assert world.rows("SELECT withdrawn_at FROM facts WHERE fact_id = ?", fact_id)[0]["withdrawn_at"] is None


# ---------- 审阅 7：按包撤回不误标别的包里碰巧像的引用；解除撤回放回来 ----------


def test_packet_withdrawal_leaves_unrelated_quotes_and_undo_releases_its_own(world: World) -> None:
    p1 = day_packet("2026-11-01", f"我：{CONTENT}")
    p3 = day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货，泰国那批不变。")
    p4 = later(
        "2026-11-04_2330",
        "- [2026-11-04 09:00] 小李：收到，我去跟仓库说。\n",
        "  > [引用 2026-11-03 10:30 我] 越南那批订单…\n",  # 截短的引用，引的是 P3，碰巧也是 P1 那句的开头
        f"- [2026-11-04 09:01] 小李：这个还算数吗？\n  > [引用 2026-11-01 10:30 我] {CONTENT}\n",  # 引的就是 P1 那句
    )
    stocked(world, p1, p3, p4)
    report = withdraw(world.settings, "prod", packets=[P1])
    assert f"{P4}#1.1.2" not in report.sentences  # 太短（“越南那批订单”只有 6 个字），不认
    assert f"{P4}#1.2.2" in report.sentences  # 一字不差引的 P1 那句：跟着撤回
    assert search(world, "11-15 出货") == []
    unwithdraw(world.settings, "prod", [P1])
    assert set(search(world, "11-15 出货")) == {f"{P1}#1.1.1", f"{P4}#1.2.2"}  # 跟着标上的一起放回来
    assert world.rows("SELECT count(*) AS n FROM withdrawn_sentences")[0]["n"] == 0


def test_an_explicit_withdrawal_is_not_released_by_undoing_a_packet(world: World) -> None:
    p4 = later("2026-11-04_2330", f"- [2026-11-04 09:01] 小李：这个还算数吗？\n  > [引用 2026-11-01 10:30 我] {CONTENT}\n")
    stocked(world, day_packet("2026-11-01", f"我：{CONTENT}"), p4)
    withdraw(world.settings, "prod", packets=[P1])  # 引用行跟着标上（因为 P1）
    withdraw(world.settings, "prod", sentences=[])  # 什么都不点：只清一遍
    world.execute("UPDATE raw_sentences SET withdrawn_at = NULL WHERE sentence_id = ?", f"{P4}#1.1.2")
    withdraw(world.settings, "prod", sentences=[f"{P4}#1.1.2"])  # 主人又点名撤回了这一行
    unwithdraw(world.settings, "prod", [P1])
    assert search(world, "11-15 出货") == [f"{P1}#1.1.1"]  # 点名撤回的那一行还撤回着


# ---------- 审阅 8：实体有歧义定过以后，包重发 ----------


def test_an_ambiguity_decision_survives_a_resend(world: World) -> None:
    def extract(window):  # noqa: ANN001, ANN202
        text = window.targets[0].text
        name = "周建国" if "周建国" in text else "周立新" if "周立新" in text else "老周"
        entity = {"key": "e1", "mention": "老周", "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}

    third = day_packet("2026-11-03", "我：老周明天回来。")
    deliver(world, day_packet("2026-11-01", "我：周建国就是老周，管越南。"), day_packet("2026-11-02", "我：周立新也叫老周，管泰国。"), third)
    write(world, extract).close()
    decide(world.settings, "prod", int(reviews(world)[0]["review_id"]), "both")
    deliver(world, third.replace("daily", "daily\nrevision: 2"))
    write(world, extract).close()
    row = world.rows("SELECT status, pending_reason FROM facts WHERE content = '老周明天回来'")[0]
    assert (row["status"], row["pending_reason"]) == ("verified", None)
    assert reviews(world) == []


# ---------- 审阅 9：定过的冲突，解除冻结再冻结要重新问 ----------


@pytest.mark.parametrize("choice", ["frozen", "both"])
def test_refreezing_after_a_decision_asks_again(world: World, choice: str) -> None:
    old, new = frozen_and_contradicted(world)
    decide(world.settings, "prod", int(reviews(world)[0]["review_id"]), choice)
    set_frozen(world.settings, "prod", [old], frozen=False)
    set_frozen(world.settings, "prod", [old])
    assert [r["claims"] for r in reviews(world)] == [{"frozen": old, "new": new}]
    assert by_value(world)["11-20"]["pending_reason"] == "frozen_conflict"


# ---------- 审阅 10：冻结事实的旧版本证据按指纹认 ----------


def test_old_revision_evidence_is_matched_by_fingerprint_not_position(world: World) -> None:
    stocked(world, day_packet("2026-11-01", f"我：{CONTENT}", "我：老周 9 点到仓库。"))
    fact_id = freeze(world, CONTENT)
    second = day_packet("2026-11-01", "我：老周 9 点到仓库。", "我：报价 12.5 美元。").replace("daily", "daily\nrevision: 2")
    deliver(world, second)
    extract_and_write()
    report = withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.1"])  # 新版本第 1 句是“老周 9 点到仓库”
    assert report.hidden == [] and evidence_of(world, fact_id) == [f"{P1}#1.1.1"]  # 旧版本那条证据是另一句话，留着
    assert world.rows("SELECT withdrawn_at FROM facts WHERE fact_id = ?", fact_id)[0]["withdrawn_at"] is None


def test_old_revision_evidence_of_the_same_sentence_is_dropped(world: World) -> None:
    stocked(world, day_packet("2026-11-01", f"我：{CONTENT}", "我：老周 9 点到仓库。"))
    fact_id = freeze(world, CONTENT)
    second = day_packet("2026-11-01", "我：老周 9 点到仓库。", f"我：{CONTENT}").replace("daily", "daily\nrevision: 2")
    deliver(world, second)
    extract_and_write()  # 新版本也说了这句：冻结事实有了新说法
    world.execute("DELETE FROM fact_claims WHERE fact_id = ?", fact_id)  # 只剩旧版本的证据，看撤回认不认得出
    report = withdraw(world.settings, "prod", sentences=[f"{P1}#1.2.1"])  # 新版本第 2 句就是那句话
    assert report.hidden == [fact_id]


# ---------- 审阅 11：D62 邮件的发件人、同名的别人 ----------


def mail_from(name: str, addr: str, body_line: str) -> str:
    body = "## 原话 · 邮件 · 2026-11-03 09:12\n\n" + mail_header_block(**{"from": f'{{name: "{name}", addr: "{addr}"}}'}) + f"\n  {body_line}\n"
    return build_packet(
        "mail-sales-a", "2026-11-03_1800", body=body, framework="mail-import", channel="email",
        mailbox="li.ming@example.com", covers_from="2026-11-02T18:00:00+08:00", covers_to="2026-11-03T18:00:00+08:00",
    )


def test_a_mail_sender_using_the_owners_name_is_not_the_owner(world: World) -> None:
    world.drop("inbox/mail-sales-a", mail_from("Ming", "someone@evil.example", "我这边已经把尾款付清了。"))
    world.intake()

    def extract(window):  # noqa: ANN001, ANN202
        line = next(line for line in window.targets if "尾款" in line.text)
        entity = {"key": "e1", "mention": "Ming", "name": "Ming", "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, line.text, "Ming 已经把尾款付清了", subject="e1")]}

    conn = write_with_owner(world, extract)
    assert subjects(conn) == {"Ming 已经把尾款付清了": "Ming"}


def test_a_different_full_name_with_the_owners_short_name_is_not_the_owner(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "Ming：我周五去越南。"))

    def extract(window):  # noqa: ANN001, ANN202
        text = window.targets[0].text.rstrip("。")
        entity = {"key": "e1", "mention": "Ming", "name": "Ming Zhou", "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text, "Ming Zhou 周五去越南", subject="e1")]}

    conn = write_with_owner(world, extract)
    assert subjects(conn) == {"Ming Zhou 周五去越南": "Ming Zhou"}


# ---------- 审阅 12：撤回中途断了 ----------


def test_a_cut_after_the_ledger_is_finished_by_running_again_without_targets(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stocked(world, *TWO_DAYS)
    target = facts(world)["报价 12.5 美元。"]["fact_id"]

    def cut(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise PowerCut

    with monkeypatch.context() as patch:
        patch.setattr(withdraw_module, "_forget_in_store", cut)
        with pytest.raises(PowerCut):
            main(["withdraw", "--library", "prod", "--fact", target])
    assert "12.5" in store_text(world)
    assert main(["withdraw", "--library", "prod", "--fact", target]) == 1  # 事实已经没了：报出来，告诉怎么接着清
    assert "不写目标再跑一次" in capsys.readouterr().err
    assert main(["withdraw", "--library", "prod"]) == 0
    assert "12.5" not in store_text(world)


def test_packets_are_queued_before_the_store_forgets_their_windows(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    stocked(world, *TWO_DAYS)
    from javmem.pipeline.store import ResultStore

    def cut(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise PowerCut

    with monkeypatch.context() as patch:
        patch.setattr(ResultStore, "delete_windows", cut)
        with pytest.raises(PowerCut):
            withdraw(world.settings, "prod", sentences=[f"{P2}#1.2.1"])
    assert world.packet_row(P2)["extraction"] == "queued"  # 先改回排队了：断在删窗口之前也丢不了
    withdraw(world.settings, "prod")
    assert "12.5" not in store_text(world)


# ---------- 审阅 13、14：写完的包，少掉的窗口 ----------


def two_window_packet(extra: str = "") -> str:
    sentence = "第 {} 句数字记录，内容凑够长度用的，没有别的意思，只是让这一句话长一点再长一点。"
    first = "".join(sentence.format(index) for index in range(1, 76))
    second = "".join(sentence.format(index) for index in range(76, 151))
    body = "## 原话 · 2026-11-01 10:30–10:31\n" f"- [2026-11-01 10:30] 我：{first}\n" f"- [2026-11-01 10:31] 我：{extra}{second}\n"
    return build_packet(
        stamp="2026-11-01_2330", body=body, created_at="2026-11-01T23:30:00+08:00",
        covers_from="2026-11-01T00:00:00+08:00", covers_to="2026-11-01T23:30:00+08:00",
    )


def test_a_cut_before_the_stale_cleanup_leaves_nothing_behind(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    stocked(world, two_window_packet())
    withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.{index}" for index in range(1, 13)])
    assert main(["extract", "--library", "prod", "--fake"]) == 0

    def cut(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise PowerCut

    with monkeypatch.context() as patch:
        patch.setattr(LedgerWriter, "drop_stale_windows", cut)
        with pytest.raises(PowerCut):
            main(["write", "--library", "prod", "--fake", "--fake-embeddings"])
    assert world.packet_row(P1)["extraction"] == "queued"  # 和改成 done 在同一个事务里：一起撤销了
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert {row["window_id"] for row in world.rows("SELECT window_id FROM fact_claims")} == {f"{P1}#w1"}


def test_stale_windows_lose_their_quarantine_and_alias_evidence_too(world: World) -> None:
    deliver(world, two_window_packet("周建国说，"))

    def extract(window):  # noqa: ANN001, ANN202
        if not window.window_id.endswith("#w2"):
            return {"entities": [], "facts": []}
        entity = {"key": "e1", "mention": "周建国", "name": "周建国", "type": "人"}
        good = l3_fact(window, "周建国说", "周建国说了话", subject="e1")
        bad = l3_fact(window, "第 80 句数字记录", "第 99999 句数字记录")  # 数对不上，G2 隔离
        return {"entities": [entity], "facts": [good, bad]}

    write(world, extract).close()
    assert world.rows("SELECT window_id FROM quarantine")
    withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.{index}" for index in range(1, 13)])
    write(world, lambda window: {"entities": [], "facts": []}).close()
    assert world.rows("SELECT window_id FROM quarantine") == []
    assert world.rows("SELECT alias FROM aliases") == [] and world.rows("SELECT entity_id FROM entities") == []


# ---------- 审阅 15：只在撤回的包里出现过的标准名 ----------


def test_a_canonical_name_only_the_withdrawn_packet_used_goes_away(world: World) -> None:
    def extract(window):  # noqa: ANN001, ANN202
        text = window.targets[0].text
        name = "周建国" if "周建国" in text else "老周"
        entity = {"key": "e1", "mention": "老周", "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}

    deliver(world, day_packet("2026-11-01", "我：周建国就是老周，管越南。"), day_packet("2026-11-02", "我：老周明天回来。"))
    write(world, extract).close()
    withdraw(world.settings, "prod", packets=[P1])

    def names() -> dict[str, str]:
        conn = world.ledger()
        try:
            return {row["content"]: row["subject"]["name"] for row in export_facts(conn)}
        finally:
            conn.close()

    assert names() == {"老周明天回来": "老周"}  # 马上换成还有出处的叫法
    assert world.packet_row(P2)["extraction"] == "queued"  # 要重新对齐
    write(world, extract).close()  # 用存着的结果重写，不调模型
    assert names() == {"老周明天回来": "老周"}
    assert [row["canonical_name"] for row in world.rows("SELECT canonical_name FROM entities")] == ["老周"]


# ---------- 审阅 16：彻底删除时有查询正读着 ----------


def test_purge_says_so_when_a_reader_keeps_it_from_finishing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    stocked(world, *TWO_DAYS)
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    reader = connect_readonly(world.data / "ledger" / "prod.db")
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM raw_sentences").fetchone()
    try:
        assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 1
        assert "整理没做完" in capsys.readouterr().out
    finally:
        reader.close()
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 0  # 查询结束了，再跑一次整理完
    assert "11-15 出货".encode() not in (world.data / "ledger" / "prod.db").read_bytes()


# ---------- 审阅 17：同一组两条都冻结 ----------


def test_two_frozen_values_in_one_group_both_stay_current(world: World) -> None:
    old, new = frozen_and_contradicted(world)
    set_frozen(world.settings, "prod", [new])
    values = by_value(world)
    for value in ("11-15", "11-20"):
        assert (values[value]["frozen"], values[value]["status"], values[value]["superseded_by"]) == (1, "verified", None)
    assert reviews(world) == []  # 两条主人都冻结了，不再问


# ---------- 审阅 18：放回来的冻结事实，来源不留撤回以前的 ----------


def test_a_frozen_fact_put_back_has_no_stale_provenance(world: World) -> None:
    stocked(world, *TWO_DAYS)
    fact_id = freeze(world, CONTENT)
    withdraw(world.settings, "prod", packets=[P1])
    decide(world.settings, "prod", int(reviews(world)[0]["review_id"]), "frozen")
    row = world.rows("SELECT sources, origins, owner_said, secondhand FROM facts WHERE fact_id = ?", fact_id)[0]
    assert (json.loads(row["sources"]), json.loads(row["origins"]), row["owner_said"], row["secondhand"]) == ([], [], 0, 0)


def test_export_records_has_no_text(world: World) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", sentences=[f"{P2}#1.2.1"])
    records = export_records(world.settings, "prod")
    assert "12.5" not in json.dumps(records, ensure_ascii=False)
    assert records["sentences"][0]["packet_id"] == P2 and len(records["sentences"][0]["sentence_hash"]) == 64


def test_fake_model_is_used_by_name(world: World) -> None:
    """抽取的模型名是假的那个（这几个测试都用 --fake），免得测试里不小心连网。"""
    assert FakeModel().cache_name.startswith("fake-model")


# ========== 第二遍审阅 ==========

ENGLISH = "We will ship 300 boxes on Nov 15."


def quoting(stamp: str, text: str) -> str:
    """stamp 那天小李引用 11-01 我说的 text：引用行是这个包的 #1.1.2。"""
    day = stamp[:10]
    return later(stamp, f"- [{day} 09:01] 小李：这个还算数吗？\n  > [引用 2026-11-01 10:30 我] {text}\n")


def rebuilt_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(fresh))
    rebuilt = World(fresh.resolve(), Settings.from_env())
    assert main(["init"]) == 0
    return rebuilt


def exported(world: World, tmp_path: Path) -> Path:
    records_file = tmp_path / "withdrawals.json"
    assert main(["withdraw", "--library", "prod", "--export", str(records_file)]) == 0
    return records_file


# ---------- 第二遍审阅 1：英文句子带着句号，引用一字不差也要认 ----------


@pytest.mark.parametrize("quote", [ENGLISH, ENGLISH.rstrip("."), "We will ship 300 boxes..."])
def test_quotes_of_an_english_sentence_follow_the_withdrawal(world: World, quote: str) -> None:
    stocked(world, day_packet("2026-11-01", f"我：{ENGLISH}"), quoting("2026-11-04_2330", quote))
    report = withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.1"])
    assert f"{P4}#1.1.2" in report.sentences
    deliver(world, quoting("2026-11-05_2330", quote))  # 撤回以后才到的也标上
    assert search(world, "300 boxes") == []


# ---------- 第二遍审阅 2：彻底删除以后、搬到新账本以后才到的引用 ----------


def test_a_quote_arriving_after_a_purge_is_still_withdrawn(world: World) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", packets=[P1])
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 0
    deliver(world, quoting("2026-11-05_2330", CONTENT))
    assert search(world, "11-15 出货") == []
    assert [row["cause"] for row in world.rows("SELECT cause FROM withdrawn_sentences WHERE packet_id = ?", P5)] == [P1]
    assert all(len(row["key_hash"]) == 64 for row in world.rows("SELECT key_hash FROM withdrawn_texts"))  # 只有指纹


def test_a_quote_of_text_withdrawn_in_the_old_ledger_is_withdrawn_in_the_rebuilt_one(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", packets=[P1])
    records_file = exported(world, tmp_path)
    exported_text = records_file.read_text(encoding="utf-8")
    for piece in (CONTENT, "越南那批订单", "11-15 出货"):  # 只有指纹，没有撤回的原话（比整句：时间戳里会有当天的日子）
        assert piece not in exported_text
    rebuilt = rebuilt_world(tmp_path, monkeypatch)
    assert main(["withdraw", "--library", "prod", "--import", str(records_file)]) == 0
    deliver(rebuilt, *TWO_DAYS)  # P1 跳过，新账本里没有它的原文
    deliver(rebuilt, quoting("2026-11-05_2330", CONTENT))  # 旧账本里没有的、后来才到的引用
    assert search(rebuilt, "11-15 出货") == []


# ---------- 第二遍审阅 3：新账本里导进来的撤回，能解除，跳过的副本能删 ----------


def test_an_imported_withdrawal_can_be_undone_and_the_packet_delivered_again(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", packets=[P1])
    records_file = exported(world, tmp_path)
    rebuilt = rebuilt_world(tmp_path, monkeypatch)
    assert main(["withdraw", "--library", "prod", "--import", str(records_file)]) == 0
    deliver(rebuilt, *TWO_DAYS)
    capsys.readouterr()
    assert main(["withdraw", "--library", "prod", "--undo", "--packet", P1]) == 0
    assert "要再投一次原包" in capsys.readouterr().out
    assert rebuilt.rows("SELECT packet_id FROM withdrawn_packets") == []
    deliver(rebuilt, TWO_DAYS[0])
    assert search(rebuilt, "11-15 出货") == [f"{P1}#1.1.1"]


def test_the_skipped_copy_of_an_imported_withdrawal_can_be_purged(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", packets=[P1])
    records_file = exported(world, tmp_path)
    rebuilt = rebuilt_world(tmp_path, monkeypatch)
    assert main(["withdraw", "--library", "prod", "--import", str(records_file)]) == 0
    deliver(rebuilt, *TWO_DAYS)
    assert rebuilt.files("archive/skipped")
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 0
    assert rebuilt.files("archive/skipped") == []
    assert rebuilt.rows("SELECT purged_at FROM withdrawn_packets WHERE packet_id = ?", P1)[0]["purged_at"] is not None
    assert main(["withdraw", "--library", "prod", "--undo", "--packet", P1]) == 1  # 彻底删除过了，回不来


# ---------- 第二遍审阅 6：点名撤回的盖过跟着标的 ----------


def test_naming_a_quote_already_marked_by_a_packet_withdrawal_sticks(world: World) -> None:
    stocked(world, day_packet("2026-11-01", f"我：{CONTENT}"), quoting("2026-11-04_2330", CONTENT))
    withdraw(world.settings, "prod", packets=[P1])  # 引用行跟着标上（因为 P1）
    report = withdraw(world.settings, "prod", sentences=[f"{P4}#1.1.2"])  # 主人又点名撤回这一行
    assert report.already == [f"{P4}#1.1.2"]
    unwithdraw(world.settings, "prod", [P1])
    assert search(world, "11-15 出货") == [f"{P1}#1.1.1"]


def test_naming_a_sentence_of_a_withdrawn_packet_keeps_it_withdrawn_after_undo(world: World) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", packets=[P1])
    withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.1"])
    unwithdraw(world.settings, "prod", [P1])
    assert search(world, "11-15 出货") == []
    assert set(search(world, "点到仓库")) == {f"{P1}#1.2.1", f"{P2}#1.1.1"}  # 包里别的句子放回来了


def two_mails_from_one_sender() -> str:
    def section(when: str, message: str, subject: str, body: str) -> str:
        header = mail_header_block(
            message_id=f'"<{message}@example.org>"', thread_id=f'"<{message}@example.org>"',
            date=f"2026-11-03T{when}:00+07:00", subject=f'"{subject}"',
        )
        return f"## 原话 · 邮件 · 2026-11-03 {when}\n\n{header}\n  Hi Ming,\n\n  {body}\n\n  Best regards,\n  Tran Mai\n\n"

    return build_mail_packet(sections=section("09:12", "a", "收购价 9.9 亿的保密方案", "方案明天发给你。") + section("10:12", "b", "越南订单出货", CONTENT))


def test_naming_a_sender_line_marked_by_another_mails_withdrawal_withdraws_its_mail(world: World) -> None:
    world.drop("inbox/mail-sales-a", two_mails_from_one_sender())
    world.intake()
    withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#1.subject"])
    # 以前的版本里第 2 封的发件人那一行一字不差，会跟着标上（D73 第 17 条以后只标同一封信的）：照那时的样子标上
    world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-03T12:00:00+08:00' WHERE sentence_id = ?", f"{MAIL_ID}#2.from")
    report = withdraw(world.settings, "prod", sentences=[f"{MAIL_ID}#2.from"])  # 主人要撤第 2 封
    assert report.mails == [f"{MAIL_ID}#2"]
    assert search(world, "11-15 出货") == []


def test_a_quote_stays_withdrawn_while_another_packet_with_the_sentence_is(world: World) -> None:
    stocked(
        world,
        day_packet("2026-11-01", f"我：{CONTENT}"),
        day_packet("2026-11-03", f"我：{CONTENT}"),
        quoting("2026-11-04_2330", CONTENT),
    )
    withdraw(world.settings, "prod", packets=[P1, P3])  # 两个包里都有这句，引用行只记下其中一个
    unwithdraw(world.settings, "prod", [P3])
    assert search(world, "11-15 出货") == [f"{P3}#1.1.1"]  # 另一个还撤着，引用行照样标着
    unwithdraw(world.settings, "prod", [P1])
    assert set(search(world, "11-15 出货")) == {f"{P1}#1.1.1", f"{P3}#1.1.1", f"{P4}#1.1.2"}


def test_undo_marks_again_quotes_of_text_still_withdrawn_elsewhere(world: World) -> None:
    stocked(
        world,
        day_packet("2026-11-01", f"我：{CONTENT}"),
        day_packet("2026-11-03", f"我：{CONTENT}"),
        quoting("2026-11-04_2330", CONTENT),
    )
    withdraw(world.settings, "prod", packets=[P1])  # P4 的引用行跟着标上（因为 P1）
    withdraw(world.settings, "prod", sentences=[f"{P3}#1.1.1"])  # P3 也说了这句，主人点名撤回
    unwithdraw(world.settings, "prod", [P1])
    assert search(world, "11-15 出货") == [f"{P1}#1.1.1"]  # 引用行引的这句在 P3 还撤回着


# ---------- 第二遍审阅 10：别的库的撤回记录不能导 ----------


def test_records_of_another_library_are_refused(world: World) -> None:
    stocked(world, *TWO_DAYS)
    withdraw(world.settings, "prod", packets=[P1])
    records = export_records(world.settings, "prod")
    with pytest.raises(ConfigError):
        withdraw(world.settings, "test", records=records)
    assert world.rows("SELECT packet_id FROM withdrawn_packets", library="test") == []


# ---------- 第二遍修的时候自己查出来的：导进记录以后断了，导进来的跟着标的记录 ----------


def test_a_cut_after_importing_records_is_finished_by_running_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    stocked(world, *TWO_DAYS)
    said = world.rows("SELECT speaker, text FROM raw_sentences WHERE sentence_id = ?", f"{P2}#1.2.1")[0]
    records = {
        "schema": "javmem-withdrawals/1",
        "library": "prod",
        "packets": [{"packet_id": P1, "withdrawn_at": "2026-11-09T00:00:00+08:00"}],
        "sentences": [
            {"packet_id": P2, "sentence_hash": sentence_hash(said["speaker"], said["text"]), "withdrawn_at": "2026-11-09T00:00:00+08:00"}
        ],
    }

    def cut(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise PowerCut

    with monkeypatch.context() as patch:
        patch.setattr(withdraw_module, "_apply", cut)
        with pytest.raises(PowerCut):
            withdraw(world.settings, "prod", records=records)
    assert search(world, "11-15 出货") == [f"{P1}#1.1.1"]  # 记录记进去了，还没撤回
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 1  # 还没真撤回：先不删
    assert f"2026/11/{P1}.md" in world.files("archive")
    report = withdraw(world.settings, "prod")  # 不写目标再跑一次
    assert report.packets == [P1] and report.sentences == [f"{P2}#1.2.1"]
    assert search(world, "11-15 出货") == [] and search(world, "12.5 美元") == []


def test_an_imported_follower_record_keeps_its_cause(world: World) -> None:
    stocked(world, day_packet("2026-11-01", f"我：{CONTENT}"), quoting("2026-11-04_2330", CONTENT))
    withdraw(world.settings, "prod", packets=[P1])
    records = export_records(world.settings, "prod")
    unwithdraw(world.settings, "prod", [P1])
    withdraw(world.settings, "prod", records=records)  # 记录导回来：P1 撤回，P4 的引用行跟着标上
    assert [tuple(row) for row in world.rows("SELECT packet_id, cause FROM withdrawn_sentences")] == [(P4, P1)]
    unwithdraw(world.settings, "prod", [P1])
    assert set(search(world, "11-15 出货")) == {f"{P1}#1.1.1", f"{P4}#1.1.2"}
