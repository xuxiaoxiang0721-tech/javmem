"""撤回（J2.11）：按包、按事实、按原文句撤回，派生的说法、事实、向量、L3 结果库、向量缓存一起清；
其他来源也支持的留着；冻结的先藏起来进待确认；拿它当上文的后一包重新排队；彻底删除；解除撤回。

样例都是编的，模型和向量都用假的（javmem extract --fake、javmem write --fake --fake-embeddings）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from helpers import CHAT_ID, World, build_packet
from test_intake_recovery import crash_once
from test_write import day_packet, deliver

from javmem.cli import main
from javmem.intake import intake_lock
from javmem.ledger import connect_readonly, transaction
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache, vector_key
from javmem.raw import raw_search
from javmem.withdraw import WithdrawError, withdraw

P1 = "2026-11-01_2330_desk-bot"
P2 = "2026-11-02_2330_desk-bot"
P3 = "2026-11-03_2330_desk-bot"


def extract_and_write(*, expect: int = 0) -> None:
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == expect


def stocked(world: World, *packets: str) -> None:
    deliver(world, *packets)
    extract_and_write()


def facts(world: World) -> dict[str, sqlite3.Row]:
    return {row["content"]: row for row in world.rows("SELECT * FROM facts")}


def evidence_of(world: World, fact_id: str) -> list[str]:
    return [row["sentence_id"] for row in world.rows("SELECT sentence_id FROM evidence WHERE fact_id = ? ORDER BY 1", fact_id)]


def search(world: World, query: str) -> list[str]:
    result = raw_search({"query": query}, settings=world.as_caller("owner-console").settings)
    assert result["ok"], result
    return [item["id"] for item in result["items"]]


def store_rows(world: World) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    with ResultStore(world.data / "l3") as store:
        windows = list(store.windows())
        results = store._conn.execute("SELECT cache_key, output FROM results").fetchall()
    return windows, results


def store_text(world: World) -> str:
    """结果库里所有存着原文、回复的列拼起来，查撤回的话还在不在。"""
    windows, results = store_rows(world)
    parts = [row["input_text"] + row["manifest"] + (row["extraction"] or "") for row in windows]
    parts += [row["output"] for row in results]
    return "\n".join(parts)


def cached_texts(world: World, texts: list[str]) -> set[str]:
    with VectorCache(world.data / "vectors") as cache:
        keys = {row[0] for row in cache._conn.execute("SELECT vector_key FROM vectors")}
    return {text for text in texts if vector_key("fake-embedding", text) in keys}


def ledger_text(world: World) -> str:
    """账本里看得到的事实、证据、说法、隔离拼起来。藏起来的事实（withdrawn_at）不算，查询都跳过它。"""
    rows = world.rows("SELECT content FROM facts WHERE withdrawn_at IS NULL")
    rows += world.rows("SELECT quote AS content FROM evidence")
    rows += world.rows("SELECT content || proofs AS content FROM fact_claims")
    rows += world.rows("SELECT payload AS content FROM quarantine")
    return "\n".join(row["content"] for row in rows)


TWO_DAYS = [
    day_packet("2026-11-01", "我：越南那批订单 11-15 出货。", "我：老周 9 点到仓库。"),
    day_packet("2026-11-02", "我：老周 9 点到仓库。", "我：报价 12.5 美元。"),
]


# ---------- 按包 ----------


def test_withdrawing_a_packet_keeps_facts_other_packets_also_support(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    stocked(world, *TWO_DAYS)
    before = facts(world)
    assert set(before) == {"越南那批订单 11-15 出货。", "老周 9 点到仓库。", "报价 12.5 美元。"}
    assert len(evidence_of(world, before["老周 9 点到仓库。"]["fact_id"])) == 2
    assert search(world, "11-15 出货")

    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    assert "包 1 个" in capsys.readouterr().out

    after = facts(world)
    assert set(after) == {"老周 9 点到仓库。", "报价 12.5 美元。"}  # 只来自 P1 的删了，P2 也说的留着
    assert evidence_of(world, after["老周 9 点到仓库。"]["fact_id"]) == [f"{P2}#1.1.1"]
    assert world.packet_row(P1)["withdrawn_at"] is not None
    # 原文层、账本、向量、结果库都找不到 P1 独有的那句
    assert search(world, "11-15 出货") == []
    assert "11-15" not in ledger_text(world)
    assert world.rows("SELECT count(*) AS n FROM fact_vectors")[0]["n"] == 2
    assert cached_texts(world, ["越南那批订单 11-15 出货。", "老周 9 点到仓库。"]) == {"老周 9 点到仓库。"}
    assert "11-15" not in store_text(world)
    windows, _ = store_rows(world)
    assert {json.loads(row["packets"])[0][0] for row in windows} == {P2}


def test_withdrawn_packet_id_is_not_taken_back_and_patrol_reports_it(world: World) -> None:
    stocked(world, *TWO_DAYS)
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    world.drop("inbox/desk-bot", TWO_DAYS[0])
    report = world.intake()
    assert [(o.result, o.code) for o in report.outcomes] == [("skipped", "withdrawn")]
    # 再撤回一次没事：已经撤回了的照报，结果库、向量缓存再清一遍
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0


def test_the_next_packet_that_used_it_as_context_is_queued_again(world: World) -> None:
    """拆开的包：后一包的第一个窗口带着上一包的末尾当上文。上一包撤回了，后一包的窗口变了，要重抽。"""
    first = day_packet("2026-11-01", "我：越南那批订单 11-15 出货。")
    second = build_packet(
        stamp="2026-11-02_2330",
        body="## 原话 · 2026-11-02 09:00–09:01\n- [2026-11-02 09:00] 我：老周 9 点到仓库。\n",
        created_at="2026-11-02T23:30:00+08:00",
        covers_from="2026-11-01T23:30:00+08:00",
        covers_to="2026-11-02T23:30:00+08:00",
        continues=P1,
    )
    stocked(world, first, second)
    windows, _ = store_rows(world)
    assert any(P1 in row["manifest"] and json.loads(row["manifest"])[-1]["packet_id"] == P2 for row in windows)

    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    assert world.packet_row(P2)["extraction"] == "queued"
    assert "11-15" not in store_text(world)  # 带着 P1 当上文的窗口也删了
    extract_and_write()  # 用剩下的原文重抽、重写
    assert world.packet_row(P2)["extraction"] == "done"
    assert set(facts(world)) == {"老周 9 点到仓库。"}
    windows, _ = store_rows(world)
    assert all(P1 not in row["manifest"] for row in windows)


def test_reconciles_a_replacement_in_flight_before_withdrawing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """替换做到一半（新版本停在 registered）：先对账让它转正，再撤回，不然它会在撤回以后转正。"""
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：第二版的内容。\n"))
    crash_once(world, monkeypatch, "_archive")
    assert world.packet_row(CHAT_ID, 2)["state"] == "registered"

    assert main(["withdraw", "--library", "prod", "--packet", CHAT_ID]) == 0
    rows = world.rows("SELECT revision, state, withdrawn_at FROM packets WHERE packet_id = ? ORDER BY revision", CHAT_ID)
    assert [(row["revision"], row["state"], row["withdrawn_at"] is not None) for row in rows] == [
        (1, "superseded", True),
        (2, "current", True),
    ]
    assert search(world, "第二版") == [] and search(world, "第一句话") == []
    assert world.intake().outcomes == []  # 对账做完了，没有停着的


# ---------- 按句、按事实 ----------


def test_withdrawing_one_sentence_marks_it_and_requeues_its_packet(world: World) -> None:
    stocked(world, *TWO_DAYS)
    sentence = f"{P2}#1.2.1"  # 报价 12.5 美元。
    assert main(["withdraw", "--library", "prod", "--sentence", sentence]) == 0

    assert set(facts(world)) == {"越南那批订单 11-15 出货。", "老周 9 点到仓库。"}
    assert world.rows("SELECT withdrawn_at FROM raw_sentences WHERE sentence_id = ?", sentence)[0]["withdrawn_at"]
    assert search(world, "12.5 美元") == []
    assert "12.5" not in ledger_text(world) and "12.5" not in store_text(world)
    assert world.packet_row(P2)["extraction"] == "queued"
    assert world.packet_row(P2)["withdrawn_at"] is None  # 只撤回这一句，包还在
    extract_and_write()
    assert set(facts(world)) == {"越南那批订单 11-15 出货。", "老周 9 点到仓库。"}  # 重抽也不回来
    assert "12.5" not in store_text(world)


def test_withdrawing_a_fact_withdraws_its_evidence_and_other_facts_lose_that_evidence(world: World) -> None:
    """query-api 3.2：这条事实的证据原句都标已撤回；别的事实也靠这些原句的，去掉这条证据，没有证据了就一起撤回。"""
    stocked(world, *TWO_DAYS)
    target = facts(world)["老周 9 点到仓库。"]["fact_id"]  # 两个包都说了
    assert main(["withdraw", "--library", "prod", "--fact", target]) == 0
    assert "老周 9 点到仓库。" not in facts(world)
    assert search(world, "仓库") == []
    rows = world.rows("SELECT sentence_id FROM raw_sentences WHERE withdrawn_at IS NOT NULL ORDER BY 1")
    assert [row["sentence_id"] for row in rows] == [f"{P1}#1.2.1", f"{P2}#1.1.1"]
    assert {world.packet_row(P1)["extraction"], world.packet_row(P2)["extraction"]} == {"queued"}
    extract_and_write()
    assert set(facts(world)) == {"越南那批订单 11-15 出货。", "报价 12.5 美元。"}


def test_a_fact_keeps_the_evidence_that_was_not_withdrawn(world: World) -> None:
    """一条说法有两句证据，撤回其中一句：说法留着，证据只剩另一句（其他来源也支持的用剩余原文重建）。"""
    from test_write import l3_fact, ref_of, write

    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。", "desk-bot：好的，11-15 出货。"))

    def extract(window):  # noqa: ANN001, ANN202
        fact = l3_fact(window, "越南那批订单 11-15 出货", "越南那批订单 11-15 出货")
        fact["evidence"].append({"ref": ref_of(window, "好的，11-15 出货"), "quote": "好的，11-15 出货"})
        return {"entities": [], "facts": [fact]}

    write(world, extract).close()
    fact_id = world.rows("SELECT fact_id FROM facts")[0]["fact_id"]
    assert evidence_of(world, fact_id) == [f"{P1}#1.1.1", f"{P1}#1.2.1"]

    report = withdraw(world.settings, "prod", sentences=[f"{P1}#1.2.1"])
    assert report.sentences == [f"{P1}#1.2.1"]
    assert evidence_of(world, fact_id) == [f"{P1}#1.1.1"]
    claim = world.rows("SELECT * FROM fact_claims")[0]
    assert [proof["sentence_id"] for proof in json.loads(claim["proofs"])] == [f"{P1}#1.1.1"]
    assert claim["speaker_role"] == "owner" and claim["said_to"] == claim["said_from"]


def test_restatements_and_quotes_of_a_withdrawn_sentence_are_withdrawn_too(world: World) -> None:
    """同一个包里 Bot 的复述、引用它的行，别的包里引用它的行，都是旧话重说，一起标撤回。"""
    first = build_packet(
        stamp="2026-11-01_2330",
        body=(
            "## 原话 · 2026-11-01 10:30–10:32\n"
            "- [2026-11-01 10:30] 我：越南那批订单 11 月 15 日出货，老周负责跟进。\n"
            "- [2026-11-01 10:31] desk-bot：收到，越南那批订单 11 月 15 日出货，老周负责跟进。\n"
            "- [2026-11-01 10:32] desk-bot：记下了。\n"
            "  > [引用 10:30 我] 越南那批订单 11 月 15 日出货，老周负责跟进。\n"
        ),
        created_at="2026-11-01T23:30:00+08:00",
        covers_from="2026-11-01T00:00:00+08:00",
        covers_to="2026-11-01T23:30:00+08:00",
    )
    later = build_packet(
        stamp="2026-11-03_2330",
        body=(
            "## 原话 · 2026-11-03 09:00–09:01\n"
            "- [2026-11-03 09:00] 我：这个还算数吗？\n"
            "  > [引用 2026-11-01 10:30 我] 越南那批订单 11 月 15 日出货，老周负责跟进。\n"
            "- [2026-11-03 09:01] 小李：越南那批订单 11 月 15 日出货，老周负责跟进。\n"
        ),
        created_at="2026-11-03T23:30:00+08:00",
        covers_from="2026-11-03T00:00:00+08:00",
        covers_to="2026-11-03T23:30:00+08:00",
    )
    stocked(world, first, later)

    report = withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.1"])
    assert report.sentences == [f"{P1}#1.1.1", f"{P1}#1.2.1", f"{P1}#1.3.2", f"{P3}#1.1.2"]
    # 小李自己又说了一遍，不是引用：不动，报出来
    assert report.copies_elsewhere == [f"{P3}#1.2.1"]
    assert search(world, "老周负责跟进") == [f"{P3}#1.2.1"]


def test_unknown_targets_change_nothing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    stocked(world, *TWO_DAYS)
    before = world.rows("SELECT count(*) AS n FROM facts")[0]["n"]
    assert main(["withdraw", "--library", "prod", "--packet", P1, "--fact", "F0000000000000000"]) == 1
    assert "F0000000000000000" in capsys.readouterr().err
    assert world.packet_row(P1)["withdrawn_at"] is None
    assert world.rows("SELECT count(*) AS n FROM facts")[0]["n"] == before
    with pytest.raises(WithdrawError):
        withdraw(world.settings, "prod", sentences=["no-such-packet#1.1.1"])


def test_busy_when_intake_is_running(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    stocked(world, *TWO_DAYS)
    with intake_lock(world.data):
        assert main(["withdraw", "--library", "prod", "--packet", P1]) == 3
    assert world.packet_row(P1)["withdrawn_at"] is None


def test_busy_when_extraction_holds_the_store(world: World) -> None:
    stocked(world, *TWO_DAYS)
    with ResultStore(world.data / "l3", exclusive=True):
        assert main(["withdraw", "--library", "prod", "--packet", P1]) == 3
    assert world.packet_row(P1)["withdrawn_at"] is None


# ---------- 冻结的 ----------


def freeze(world: World, content: str) -> str:
    fact_id = facts(world)[content]["fact_id"]
    world.execute("UPDATE facts SET frozen = 1 WHERE fact_id = ?", fact_id)  # J2.12 的管理命令以后做这一步
    return fact_id


def test_a_frozen_fact_whose_evidence_is_all_withdrawn_is_hidden_and_queued_for_review(world: World) -> None:
    stocked(world, *TWO_DAYS)
    fact_id = freeze(world, "越南那批订单 11-15 出货。")
    report = withdraw(world.settings, "prod", packets=[P1])
    assert report.hidden == [fact_id]
    row = world.rows("SELECT frozen, withdrawn_at FROM facts WHERE fact_id = ?", fact_id)[0]
    assert row["frozen"] == 1 and row["withdrawn_at"] is not None  # 没删，藏起来了
    assert evidence_of(world, fact_id) == []  # 指着撤回内容的证据删了
    reviews = world.rows("SELECT kind, claims, quotes FROM review_queue WHERE decided_at IS NULL")
    assert [(r["kind"], json.loads(r["claims"]), r["quotes"]) for r in reviews] == [
        ("other", {"fact": fact_id, "reason": "evidence_withdrawn", "packets": [P1]}, "[]")
    ]
    assert "11-15" not in ledger_text(world)


def test_a_frozen_fact_still_supported_elsewhere_stays_visible(world: World) -> None:
    stocked(world, *TWO_DAYS)
    fact_id = freeze(world, "老周 9 点到仓库。")
    report = withdraw(world.settings, "prod", packets=[P1])
    assert report.hidden == []
    assert evidence_of(world, fact_id) == [f"{P2}#1.1.1"]
    assert world.rows("SELECT withdrawn_at FROM facts WHERE fact_id = ?", fact_id)[0]["withdrawn_at"] is None


def test_withdrawing_a_frozen_fact_by_name_deletes_it(world: World) -> None:
    stocked(world, *TWO_DAYS)
    fact_id = freeze(world, "报价 12.5 美元。")
    report = withdraw(world.settings, "prod", facts=[fact_id])
    assert report.facts == [fact_id] and report.hidden == []
    assert world.rows("SELECT 1 FROM facts WHERE fact_id = ?", fact_id) == []


# ---------- 写完的包，窗口少了一个 ----------


def test_claims_of_a_window_that_no_longer_exists_are_dropped_after_rewriting(world: World) -> None:
    """撤回几句以后包从两个窗口变成一个：重写完，以前第二个窗口的说法不能留着。"""
    sentence = "第 {} 句数字记录，内容凑够长度用的，没有别的意思，只是让这一句话长一点再长一点。"
    first = "".join(sentence.format(index) for index in range(1, 76))
    second = "".join(sentence.format(index) for index in range(76, 151))
    body = "## 原话 · 2026-11-01 10:30–10:31\n" f"- [2026-11-01 10:30] 我：{first}\n" f"- [2026-11-01 10:31] 我：{second}\n"
    packet = build_packet(
        stamp="2026-11-01_2330",
        body=body,
        created_at="2026-11-01T23:30:00+08:00",
        covers_from="2026-11-01T00:00:00+08:00",
        covers_to="2026-11-01T23:30:00+08:00",
    )
    stocked(world, packet)
    windows = {row["window_id"] for row in world.rows("SELECT window_id FROM fact_claims")}
    assert windows == {f"{P1}#w1", f"{P1}#w2"}

    withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.{index}" for index in range(1, 13)])
    extract_and_write()
    windows = {row["window_id"] for row in world.rows("SELECT window_id FROM fact_claims")}
    assert windows == {f"{P1}#w1"}
    assert world.rows("SELECT count(*) AS n FROM facts")[0]["n"] == 150 - 12


# ---------- 彻底删除、解除撤回 ----------


def test_purge_deletes_archive_files_and_raw_text(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    stocked(world, *TWO_DAYS)
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 1  # 还没撤回
    assert "先撤回" in capsys.readouterr().err
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    assert main(["purge", "--library", "prod", "--packet", P1]) == 1  # 没加 --yes
    assert (world.data / "archive" / "2026" / "11" / f"{P1}.md").exists()

    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 0
    assert not (world.data / "archive" / "2026" / "11" / f"{P1}.md").exists()
    assert world.rows("SELECT count(*) AS n FROM raw_sentences WHERE packet_id = ?", P1)[0]["n"] == 0
    assert world.packet_row(P1)["purged_at"] is not None
    ledger_bytes = (world.data / "ledger" / "prod.db").read_bytes()
    assert "11-15 出货".encode("utf-8") not in ledger_bytes  # 空页里也没有
    assert main(["patrol"]) in (0, 1)
    assert "archive_missing" not in capsys.readouterr().out
    world.drop("inbox/desk-bot", TWO_DAYS[0])
    assert [(o.result, o.code) for o in world.intake().outcomes] == [("skipped", "withdrawn")]
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 0  # 再来一次没事
    assert main(["withdraw", "--library", "prod", "--undo", "--packet", P1]) == 1  # 删过了，回不来


def test_purge_an_old_revision_keeps_the_current_one(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：旧版里的密钥 abcd。\n"))
    world.intake()
    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：新版打过码了。\n"))
    world.intake()
    kept = world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.r1.md"
    assert kept.exists()
    assert main(["purge", "--library", "prod", "--packet", CHAT_ID, "--revision", "2", "--yes"]) == 1  # 现行的不能单独删
    assert main(["purge", "--library", "prod", "--packet", CHAT_ID, "--revision", "1", "--yes"]) == 0
    assert not kept.exists()
    assert (world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md").exists()
    assert world.packet_row(CHAT_ID, 1)["purged_at"] is not None
    assert world.packet_row(CHAT_ID, 2)["purged_at"] is None
    assert search(world, "打过码")


def test_undo_brings_a_packet_back_and_queues_it(world: World) -> None:
    stocked(world, *TWO_DAYS)
    assert main(["withdraw", "--library", "prod", "--packet", P2]) == 0
    assert search(world, "12.5 美元") == []
    assert main(["withdraw", "--library", "prod", "--undo", "--packet", P2]) == 0
    assert world.packet_row(P2)["withdrawn_at"] is None and world.packet_row(P2)["extraction"] == "queued"
    assert search(world, "12.5 美元") == [f"{P2}#1.2.1"]
    extract_and_write()
    assert set(facts(world)) == {"越南那批订单 11-15 出货。", "老周 9 点到仓库。", "报价 12.5 美元。"}
    assert main(["withdraw", "--library", "prod", "--undo", "--fact", "F1"]) == 2  # 只能按包


def test_report_has_no_raw_text(world: World, tmp_path: Path) -> None:
    stocked(world, *TWO_DAYS)
    report_file = tmp_path / "withdraw.json"
    assert main(["withdraw", "--library", "prod", "--packet", P1, "--report", str(report_file)]) == 0
    text = report_file.read_text(encoding="utf-8")
    assert "越南" not in text and "老周" not in text
    report = json.loads(text)
    assert report["packets"] == [P1] and report["store"] is True and report["store_windows"] >= 1


def test_replacing_still_leaves_frozen_facts_and_decisions_alone(world: World) -> None:
    """骨架和收件那条：替换不动冻结的事实、定过的待确认。接上撤回的清理以后，替换还是这样。"""
    stocked(world, *TWO_DAYS)
    fact_id = freeze(world, "越南那批订单 11-15 出货。")
    conn = world.ledger()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO review_queue (kind, claims, quotes, decision, decided_at, created_at)"
                " VALUES ('frozen_conflict', '{}', '[]', '留原值', '2026-11-08T00:00:00+08:00', '2026-11-08T00:00:00+08:00')"
            )
    finally:
        conn.close()
    world.drop("inbox/desk-bot", TWO_DAYS[0].replace("daily", "daily\nrevision: 2").replace("11-15", "11-20"))
    assert world.intake().count("replaced") == 1
    row = world.rows("SELECT frozen, withdrawn_at FROM facts WHERE fact_id = ?", fact_id)[0]
    assert row["frozen"] == 1 and row["withdrawn_at"] is None
    assert evidence_of(world, fact_id) == [f"{P1}#1.1.1"]  # 还指着旧版本，替换不动它
    assert world.rows("SELECT decision FROM review_queue WHERE decided_at IS NOT NULL")[0]["decision"] == "留原值"


def test_vectors_the_other_library_still_uses_are_kept(world: World) -> None:
    """向量缓存两个库共用：撤回正式库的包，测试库还在用的向量留着。"""
    stocked(world, *TWO_DAYS)
    world.drop("inbox-test/desk-bot", TWO_DAYS[0].replace("env: prod", "env: test"))
    world.intake()
    assert main(["extract", "--library", "test", "--fake"]) == 0
    assert main(["write", "--library", "test", "--fake", "--fake-embeddings"]) == 0
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    assert cached_texts(world, ["越南那批订单 11-15 出货。"]) == {"越南那批订单 11-15 出货。"}
    conn = connect_readonly(world.settings.library("test").db_path)
    try:
        assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 2
    finally:
        conn.close()


def test_a_mail_packet_that_shared_a_window_is_queued_even_without_the_store(world: World, tmp_path: Path) -> None:
    """同一往来串的信在两个包里、一起写进一个窗口：撤回前一个包，后一个包的窗口少了内容，要重抽。
    没有 L3 结果库（放在别处、没写 --store）也要认得出来。"""
    from helpers import MAIL_ID, MAIL_PACKET, mail_header_block

    reply = build_packet(
        "mail-sales-a",
        "2026-11-04_1800",
        body="## 原话 · 邮件 · 2026-11-04 10:00\n\n"
        + mail_header_block(
            message_id='"<ship-0003@example.com>"',
            thread_id='"<ship-0001@example.org>"',
            in_reply_to='"<ship-0002@example.com>"',
            date="2026-11-04T10:00:00+08:00",
            subject='"Re: Shipment date for the Vietnam order"',
        )
        + "\n  Noted, we will ship 300 boxes on Nov 15.\n",
        framework="mail-import",
        channel="email",
        mailbox="li.ming@example.com",
        covers_from="2026-11-03T18:00:00+08:00",
        covers_to="2026-11-04T18:00:00+08:00",
        created_at="2026-11-04T18:00:00+08:00",
    )
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.drop("inbox/mail-sales-a", reply)
    world.intake()
    extract_and_write()
    later = "2026-11-04_1800_mail-sales-a"
    windows = {row["window_id"] for row in world.rows("SELECT window_id FROM fact_claims WHERE proofs LIKE ?", f"%{later}%")}
    assert windows and all(window.startswith(MAIL_ID) for window in windows)  # 和前一个包在同一个窗口里写的

    report = withdraw(world.settings, "prod", packets=[MAIL_ID], store_dir=tmp_path / "no-store")
    assert report.store_windows == 0 and later in report.requeued  # 新建的空结果库里什么都没有，账本自己认得出
    assert world.packet_row(later)["extraction"] == "queued"
