"""包登记的 queued_at（迁移 0029，巡检要 javmem 改的第 1 样）：这个包最近一回进排队的时刻。

进排队写现在：收件登记、写完了（done）又改回排队、解除撤回。本来就在排队的（queued、failed）接着算，不动；
done 的留着上一回的。以前的包空着。样例借别的测试里编好的包和场景，不调模型。
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from helpers import SHANGHAI, World
from test_queue_failed import LINE, PACKET, WRITE, last_calls, mark_failed
from test_quote_only import CRATES, REPLY, mail, mail_packet, written
from test_quote_only import deliver as deliver_mail
from test_withdraw import P1, P2, TWO_DAYS, extract_and_write
from test_write import day_packet, deliver, l3_fact, ref_of, write
from test_write_review import _mail

from javmem.cli import main
from javmem.ledger import load_migrations, migrate
from javmem.llm import SERVER
from javmem.pipeline.batch import load_queued
from javmem.pipeline.window import Window
from javmem.timeutil import iso
from javmem.withdraw import RECORDS_SCHEMA, WithdrawError, requeue_packets, unwithdraw, withdraw

T0 = datetime(2026, 11, 4, 23, 40, tzinfo=SHANGHAI)  # 收件
T1 = T0 + timedelta(days=3)  # 撤回、改回排队
T2 = T1 + timedelta(days=2)  # 解除撤回


def queued(world: World, packet_id: str, revision: int = 1) -> tuple[str, str | None]:
    row = world.packet_row(packet_id, revision)
    assert row is not None
    return row["extraction"], row["queued_at"]


# ---------- 迁移 ----------


def test_an_old_ledger_gets_the_column_and_its_rows_stay_empty(world: World) -> None:
    [mine] = [m for m in load_migrations() if m.name.endswith("_queued_at.sql")]  # 按名字找：合并时改了编号也不用改这里
    deliver(world.at(T0), *TWO_DAYS)
    conn = world.ledger()
    try:
        conn.execute("ALTER TABLE packets DROP COLUMN queued_at")  # 照升以前的样子
        conn.execute(f"PRAGMA user_version = {mine.version - 1}")
        before = [tuple(row) for row in conn.execute("SELECT * FROM packets ORDER BY packet_id, revision")]
        # 只升这一步：以后的迁移这本账里已经做过了，再做一遍会报“已经有了”（审阅第 1 条）
        assert migrate(conn, [m for m in load_migrations() if m.version <= mine.version]) == [mine.version]
        rows = conn.execute("SELECT * FROM packets ORDER BY packet_id, revision").fetchall()
        assert [row["queued_at"] for row in rows] == [None, None]  # 不知道以前是什么时候开始排的，空着
        assert [tuple(row)[:-1] for row in rows] == before  # 别的列一字不差（新列加在最后）
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# ---------- 收包登记 ----------


def test_intake_writes_the_received_time_and_private_packets_stay_empty(world: World) -> None:
    private = day_packet("2026-11-02", LINE).replace("privacy: normal", "privacy: private")
    deliver(world.at(T0), day_packet("2026-11-01", LINE), private)
    assert queued(world, P1) == ("queued", iso(T0)) and world.packet_row(P1)["received_at"] == iso(T0)
    assert queued(world, P2) == ("skipped", None)  # 不进排队


def test_a_resent_revision_queues_from_when_it_arrives(world: World) -> None:
    deliver(world.at(T0), day_packet("2026-11-01", LINE))
    deliver(world.at(T1), day_packet("2026-11-01", LINE).replace("privacy: normal", "privacy: normal\nrevision: 2"))
    assert queued(world, P1, 2) == ("queued", iso(T1))
    assert world.packet_row(P1, 1)["queued_at"] == iso(T0)  # 换下来的那一版留着它自己的


# ---------- 撤回 ----------


def test_a_withdrawal_queues_a_written_packet_again_from_the_withdrawal(world: World) -> None:
    deliver(world.at(T0), *TWO_DAYS)
    extract_and_write()
    assert queued(world, P2) == ("done", iso(T0))  # 写完了的留着上一回的
    withdraw(world.at(T1).settings, "prod", sentences=[f"{P2}#1.2.1"])
    assert queued(world, P2) == ("queued", iso(T1))
    assert queued(world, P1) == ("done", iso(T0))  # 没动的不变


def test_requeue_keeps_the_time_of_a_failed_packet_and_restarts_a_written_one(world: World) -> None:
    deliver(world.at(T0), day_packet("2026-11-01", LINE))
    mark_failed(world)
    assert requeue_packets(world.at(T1).settings, "prod", [PACKET])
    assert queued(world, PACKET) == ("queued", iso(T0))  # failed 的一直在排队，接着算
    world.execute("UPDATE packets SET extraction = 'done' WHERE packet_id = ?", PACKET)
    assert requeue_packets(world.at(T2).settings, "prod", [PACKET])
    assert queued(world, PACKET) == ("queued", iso(T2))


def test_undoing_a_withdrawal_queues_from_the_undo(world: World) -> None:
    deliver(world.at(T0), *TWO_DAYS)  # 还没写：P1 撤回时还在排队
    withdraw(world.at(T1).settings, "prod", packets=[P1])
    unwithdraw(world.at(T2).settings, "prod", [P1])
    assert queued(world, P1) == ("queued", iso(T2))  # 撤回的那一阵不算排队


def test_undoing_the_withdrawal_of_a_written_packet_queues_from_the_undo(world: World) -> None:
    deliver(world.at(T0), *TWO_DAYS)
    extract_and_write()
    withdraw(world.at(T1).settings, "prod", packets=[P1])
    unwithdraw(world.at(T2).settings, "prod", [P1])
    assert queued(world, P1) == ("queued", iso(T2))


def test_undoing_a_record_that_never_withdrew_the_packet_keeps_its_time(world: World) -> None:
    """撤回记录导进来了、包却没撤回（--import 时同一条命令里点名的包号写错了）：包一直在排队，解除撤回不重新计时
    （审阅第 4 条）。"""
    deliver(world.at(T0), day_packet("2026-11-01", LINE))
    records = {"schema": RECORDS_SCHEMA, "library": "prod", "sentences": [], "texts": [],
               "packets": [{"packet_id": PACKET, "withdrawn_at": iso(T0), "purged_at": None}]}
    with pytest.raises(WithdrawError):
        withdraw(world.at(T1).settings, "prod", packets=["2026-11-02_2330_typo-bot"], records=records)
    assert world.packet_row(PACKET)["withdrawn_at"] is None
    assert [row[0] for row in world.rows("SELECT packet_id FROM withdrawn_packets")] == [PACKET]
    unwithdraw(world.at(T2).settings, "prod", [PACKET])
    assert queued(world, PACKET) == ("queued", iso(T0))


# ---------- 写账 ----------


def _both_mails(window: Window) -> dict[str, object]:
    """往来串的两封信一起写：一条说法的证据两封信里各有一句。"""
    texts = [line.text for line in window.targets]
    if not (any("Nov 15" in text for text in texts) and any("12.5 USD" in text for text in texts)):
        return {"entities": [], "facts": []}
    fact = l3_fact(window, "We will move the shipment to Nov 15", "We will move the shipment to Nov 15")
    fact["evidence"].append({"ref": ref_of(window, "Price stays 12.5 USD"), "quote": "Price stays 12.5 USD"})
    return {"entities": [], "facts": [fact]}


TW = T1 + timedelta(hours=1)  # 写账自己的钟（javmem.pipeline.write._now）钉在这一刻（审阅第 8 条：不拿真钟比）


@pytest.fixture
def write_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("javmem.pipeline.write._now", lambda: iso(TW))


def test_a_mail_written_with_a_resent_one_queues_again_from_the_resend(world: World, write_clock: None) -> None:
    span = ("2026-11-02T18:00:00+08:00", "2026-11-03T18:00:00+08:00")
    first = _mail("2026-11-03_1800", "We will move the shipment to Nov 15.", *span, reply=False)
    reply = _mail("2026-11-04_1800", "Price stays 12.5 USD.", "2026-11-03T18:00:00+08:00", "2026-11-04T18:00:00+08:00", reply=True)
    deliver(world.at(T0), first, reply, folder="inbox/mail-sales-a")
    write(world, _both_mails).close()
    answer = "2026-11-04_1800_mail-sales-a"
    assert queued(world, answer) == ("done", iso(T0))
    deliver(world.at(T1), _mail("2026-11-03_1800", "We will move the shipment to Nov 15.", *span, reply=False, revision=2),
            folder="inbox/mail-sales-a")
    assert queued(world, answer) == ("queued", iso(TW))  # 包重发连带的：用写账自己的钟


def test_a_quote_that_stops_being_evidence_queues_its_packet_again(world: World, write_clock: None) -> None:
    [copy] = deliver_mail(world.at(T0), mail_packet(4, mail("<r1@example.org>", "2026-11-04T09:00:00+08:00", REPLY)))
    assert written(world, load_queued(world.settings, "prod")) == []
    assert queued(world, copy) == ("done", iso(T0))
    original = mail("<orig@example.net>", "2026-11-03T09:12:00+08:00", [CRATES, "", "Best regards,", "Lena Ortiz"],
                    subject="Crates", reply_to=None, sender='{name: "Lena Ortiz", addr: "lena.ortiz@example.net"}')
    deliver_mail(world.at(T1), mail_packet(6, original))
    assert written(world, load_queued(world.settings, "prod")) == [copy]
    assert queued(world, copy) == ("queued", iso(TW))


def test_a_failed_packet_back_in_the_queue_keeps_its_time(world: World) -> None:
    deliver(world.at(T0), day_packet("2026-11-01", LINE))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    last_calls(world, "refusal")
    assert main(WRITE) == 1 and queued(world, PACKET) == ("failed", iso(T0))
    last_calls(world, SERVER)  # 一时的毛病：回到 queued
    assert main(WRITE) == 1
    assert queued(world, PACKET) == ("queued", iso(T0))


# ---------- 以后别的地方也一样写 ----------

SRC = Path(__file__).resolve().parents[1] / "src" / "javmem"


def _sql_strings() -> list[tuple[str, str]]:
    """src/javmem 里每一个字符串常量（连在一起写的几段 Python 会合成一个），空白、等号两边的写法统一。"""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = re.sub(r"\s*=\s*", " = ", " ".join(node.value.split()))
                found.append((f"{path.relative_to(SRC).as_posix()}:{node.lineno}", sql))
    return found


def test_every_statement_that_queues_a_packet_writes_queued_at() -> None:
    """改成 queued 的每一句都写 queued_at；只认 failed 改回来的那一句不用（一直在排队，接着算）。收件登记那一句也要写。

    查不到的（审阅第 7 条）：用 + 拼起来的 SQL；不改 extraction、却让包回到排队的（解除撤回清 withdrawn_at 那一句：
    queued_at 写在它后面那一句里）。"""
    queues, missing = [], []
    for where, sql in _sql_strings():
        if re.search(r"(INSERT( OR \w+)?|REPLACE) INTO packets\b", sql) and "queued_at" not in sql:
            missing.append(where)
        if "UPDATE packets" not in sql:
            continue
        head, _, tail = sql.partition(" WHERE ")
        if "extraction = 'queued'" in head or "extraction = ?" in head:
            queues.append(where)
            only_failed = re.search(r"(^| AND )extraction = 'failed'( AND |$)", tail) and " OR " not in tail
            if "queued_at" not in head and not only_failed:
                missing.append(where)
    assert missing == []
    assert len(queues) >= 5  # 写账、撤回那几处都找到了：少了是这一条自己坏了
