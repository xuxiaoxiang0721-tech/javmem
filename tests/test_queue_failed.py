"""抽取重调也没用的包：写账标成 failed、记下原因，巡检报出来；一时的毛病不标；--retry-failed、撤回让它回来（排队修）。
样例都是编的，向量用假的，不调模型。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import World, build_packet
from test_write import checked_items, day_packet, deliver
from test_write_review import _mail

from javmem.cli import main
from javmem.embedding import FakeEmbeddings
from javmem.ledger import connect, load_migrations, migrate, open_ledger, schema_version
from javmem.llm import AUTH, PROXY, QUOTA, RATE_LIMIT, SERVER, FakeModel
from javmem.patrol import PatrolItem, extraction_message, run_patrol
from javmem.pipeline.batch import load_queued
from javmem.pipeline.normalize import PRIVATE_TAG, Scope
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.verify import NO_RECHECK, NOT_SENT, Checked
from javmem.pipeline.write import LedgerWriter, WriteRun, run_write
from javmem.withdraw import requeue_packets

PACKET = "2026-11-01_2330_desk-bot"
LINE = "我：样品箱周五寄出。"
WRITE = ["write", "--library", "prod", "--fake", "--fake-embeddings"]


def state(world: World) -> tuple[str, str | None]:
    row = world.packet_row(PACKET)
    assert row is not None
    return row["extraction"], row["extraction_note"]


def last_calls(world: World, status: str) -> None:
    """抽过以后把结果库改成：这个窗口没有结果，最后一次调用是 status（和 test_write_review 第 9 条一样的做法）。"""
    store = sqlite3.connect(world.data / "l3" / "l3.db")
    try:
        store.execute("DELETE FROM windows")
        store.execute("DELETE FROM results")
        store.execute("UPDATE calls SET status = ?", (status,))
        store.commit()
    finally:
        store.close()


def extract_then(world: World, status: str) -> None:
    deliver(world, day_packet("2026-11-01", LINE))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    last_calls(world, status)


def failed_items(world: World) -> list[PatrolItem]:
    return [item for item in run_patrol(world.settings) if item.kind == "extraction_failed"]


def mark_failed(world: World) -> None:
    world.execute("UPDATE packets SET extraction = 'failed', extraction_note = 'format_error=1' WHERE packet_id = ?", PACKET)


def run_items(world: World, items: list[Checked]) -> WriteRun:
    """拿造好的窗口直接跑一遍写账（不发的、太大的、复核发不出去的，用命令造不出来）。"""
    conn = open_ledger(world.settings.library("prod"))
    try:
        writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
        batch = load_queued(world.settings, "prod")
        with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
            return run_write(
                items,
                writer,
                packets=[(packet.packet_id, packet.header.revision) for packet in batch.packets],
                store=store,
                cache=cache,
                embedder=FakeEmbeddings(),
                sleep=lambda _: None,
            )
    finally:
        conn.close()


def unsent(world: World) -> Checked:
    """这个包的那一个窗口，没有结果。"""
    deliver(world, day_packet("2026-11-01", LINE))
    return checked_items(world, lambda window: None)[0]


# ---------- 验收 1：格式错的，写账以后是 failed，巡检报出来 ----------


def test_a_format_error_marks_the_packet_failed_and_patrol_says_why(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    extract_then(world, "format_error")
    capsys.readouterr()
    summary = tmp_path / "summary.json"
    assert main([*WRITE, "--summary", str(summary)]) == 1  # 退出码照旧：有窗口没结果就是 1
    assert "其中标成 failed 1 个" in capsys.readouterr().out  # 说的是真标了几个（审阅第 7 条）
    assert json.loads(summary.read_text(encoding="utf-8"))["this_run"]["packets_failed"] == 1
    assert state(world) == ("failed", "format_error=1")
    items = failed_items(world)
    assert [(item.packet_id, item.revision) for item in items] == [(PACKET, 1)]
    assert "格式错 1 段" in items[0].message and "javmem extract --retry-failed" in items[0].message


@pytest.mark.parametrize(
    "status, label", [("length", "截断"), ("refusal", "拒答"), ("too_long", "太长"), ("content_rejected", "内容被挡")]
)
def test_every_kind_that_is_not_called_again_marks_the_packet_failed(world: World, status: str, label: str) -> None:
    extract_then(world, status)
    assert main(WRITE) == 1
    assert state(world) == ("failed", f"{status}=1")
    assert label in failed_items(world)[0].message


# ---------- 验收 3：一时的毛病不标 failed ----------


@pytest.mark.parametrize("status", [SERVER, RATE_LIMIT, QUOTA, AUTH, PROXY])
def test_a_transient_failure_keeps_the_packet_queued(world: World, status: str) -> None:
    extract_then(world, status)
    assert main(WRITE) == 1
    assert state(world) == ("queued", None)
    assert failed_items(world) == []


# ---------- 验收 2：--retry-failed 以后再跑 ----------


def test_retry_failed_reaches_the_packet_and_write_finishes_it(world: World) -> None:
    extract_then(world, "format_error")
    assert main(WRITE) == 1 and state(world)[0] == "failed"
    assert main(["extract", "--library", "prod", "--fake"]) == 1  # 取得到它，不加 --retry-failed 照旧不重调
    assert state(world)[0] == "failed"
    assert main(["extract", "--library", "prod", "--fake", "--retry-failed"]) == 0
    assert main(WRITE) == 0
    assert state(world) == ("done", None)
    assert failed_items(world) == []


def test_a_retry_that_hits_a_transient_error_goes_back_to_the_queue(world: World) -> None:
    extract_then(world, "refusal")
    assert main(WRITE) == 1 and state(world)[0] == "failed"
    last_calls(world, SERVER)  # --retry-failed 重调时碰上服务出错：最后一次调用是一时的毛病
    assert main(WRITE) == 1
    assert state(world) == ("queued", None)


# ---------- 不发的窗口：打码没打干净、太大、带着 private 的，复核发不出去的 ----------


def test_a_window_with_a_leftover_secret_marks_the_packet_failed(world: World) -> None:
    item = unsent(world)
    leaky = Checked(item.window, replace(item.rendered, leftover=("密钥",)), item.cache_key, None, None, NOT_SENT)
    run = run_items(world, [leaky])
    assert run.failed == [(PACKET, 1)]
    assert state(world) == ("failed", "unredacted=1")
    assert "打码没打干净" in failed_items(world)[0].message


def test_a_window_too_big_to_send_marks_the_packet_failed(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("javmem.pipeline.write.MAX_INPUT_CHARS", 10)
    run_items(world, [unsent(world)])
    assert state(world) == ("failed", "too_big=1")
    assert "窗口太大" in failed_items(world)[0].message


def test_a_private_window_is_failed_by_the_rules_not_by_an_error(world: World) -> None:
    """先这样定（主会话 11-17）：带着 private 内容、不发的窗口也标 failed，原因写成按规矩不发，巡检看得出不是出错。"""
    item = unsent(world)
    window = replace(item.window, scope=Scope(item.window.scope.partitions, (PRIVATE_TAG,)))
    run_items(world, [Checked(window, item.rendered, item.cache_key, None, None, NOT_SENT)])
    assert state(world) == ("failed", "private=1")
    message = failed_items(world)[0].message
    assert "按规矩不发（private）" in message and "不是出错" in message and "--retry-failed" not in message


def test_a_recheck_that_cannot_be_redone_also_marks_the_packet_failed(world: World) -> None:
    """先这样定：加了 --recheck、复核重调也没用的（这里是事实那一段打码没打干净）也标 failed，原因前面写复核。"""
    item = unsent(world)
    run_items(world, [replace(item, skipped=NO_RECHECK, recheck_key=None)])
    assert state(world) == ("failed", "recheck_unredacted=1")
    assert "复核" in failed_items(world)[0].message


def test_one_window_that_cannot_be_redone_is_enough(world: World) -> None:
    """先这样定：一个包里一段重调也没用、另一段还没抽，包也是 failed（要人管）；还没抽的那一段下次照样抽。"""
    item = unsent(world)
    leaky = Checked(item.window, replace(item.rendered, leftover=("密钥",)), item.cache_key, None, None, NOT_SENT)
    later = replace(item, window=replace(item.window, window_id=f"{item.window.window_id}-later"))
    run = run_items(world, [leaky, later])
    assert run.waiting == [(PACKET, 1)] and run.failed == [(PACKET, 1)]
    assert state(world) == ("failed", "unredacted=1")


def test_a_failed_packet_left_with_no_windows_is_done(world: World) -> None:
    deliver(world, day_packet("2026-11-01", LINE))
    mark_failed(world)
    run_items(world, [])
    assert state(world) == ("done", None)


def test_patrol_without_a_note_still_reports_the_packet(world: World) -> None:
    deliver(world, day_packet("2026-11-01", LINE))
    world.execute("UPDATE packets SET extraction = 'failed' WHERE packet_id = ?", PACKET)
    items = failed_items(world)
    assert [item.packet_id for item in items] == [PACKET] and "javmem extract --retry-failed" in items[0].message


# ---------- 撤回、解除撤回让它回到排队 ----------


def test_requeue_after_a_withdrawal_takes_a_failed_packet_back(world: World) -> None:
    deliver(world, day_packet("2026-11-01", LINE))
    mark_failed(world)
    assert requeue_packets(world.settings, "prod", [PACKET])
    assert state(world) == ("queued", None)


def test_undoing_a_withdrawal_puts_a_failed_packet_back_in_the_queue(world: World) -> None:
    deliver(world, day_packet("2026-11-01", LINE))
    mark_failed(world)
    assert main(["withdraw", "--library", "prod", "--packet", PACKET]) == 0
    assert failed_items(world) == []  # 撤回了的不报
    assert main(["withdraw", "--library", "prod", "--undo", "--packet", PACKET]) == 0
    assert state(world) == ("queued", None)


# ---------- 取包、迁移 ----------


def test_failed_packets_are_still_taken_for_extract_and_write(world: World) -> None:
    deliver(world, day_packet("2026-11-01", LINE))
    mark_failed(world)
    assert [packet.packet_id for packet in load_queued(world.settings, "prod").packets] == [PACKET]


def test_an_old_ledger_gets_the_note_column(tmp_path: Path) -> None:
    migrations = load_migrations()
    assert "extraction_note" in migrations[23].sql  # 0024
    conn = connect(tmp_path / "old.db")
    try:
        migrate(conn, migrations[:23])
        assert schema_version(conn) == 23
        migrate(conn)
        assert schema_version(conn) == len(migrations)
        assert "extraction_note" in {row[1] for row in conn.execute("PRAGMA table_info(packets)")}
    finally:
        conn.close()


# ---------- 审阅（7c8ae0a）查出来的 ----------


def state_of(world: World, packet_id: str, revision: int = 1) -> tuple[str, str | None]:
    row = world.packet_row(packet_id, revision)
    assert row is not None
    return row["extraction"], row["extraction_note"]


NUMBERS = "我：样品箱 3 号寄出，运费 120 元。"


def test_a_recheck_too_big_to_send_marks_the_packet_failed(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 1 条：复核要发的那一段太大，再跑 extract --recheck 也发不出去，是重调也没用的，不是“还没复核”。
    真实里是快到 6 万字的窗口加上事实那一段；这里把上限压低，走的是同一条路。"""
    deliver(world, day_packet("2026-11-01", NUMBERS))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    monkeypatch.setattr("javmem.pipeline.recheck.MAX_RECHECK_CHARS", 10)
    assert main([*WRITE, "--recheck"]) == 1
    assert state(world) == ("failed", "recheck_too_big=1")
    message = failed_items(world)[0].message
    assert "复核：窗口太大 1 段" in message and "--retry-failed-recheck" not in message


def test_the_reason_is_read_once(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 2 条：写账读一次“上一次调用怎么了”就用这一次的；读完以后另一个 extract --retry-failed 又记了一次
    连不上，原因也不会写成 None。"""
    extract_then(world, "format_error")
    real = ResultStore.last_failure
    reads: list[str] = []

    def racing(self: ResultStore, cache_key: str) -> str | None:
        found = real(self, cache_key)
        if not reads:
            other = sqlite3.connect(world.data / "l3" / "l3.db")
            try:
                other.execute(
                    "INSERT INTO calls (run_id, window_id, cache_key, attempt, status, detail, input_tokens, output_tokens,"
                    " reasoning_tokens, cached_tokens, seconds, at) VALUES ('r', 'w', ?, 1, ?, NULL, 0, 0, 0, 0, 0, 'x')",
                    (cache_key, SERVER),
                )
                other.commit()
            finally:
                other.close()
        reads.append(cache_key)
        return found

    monkeypatch.setattr(ResultStore, "last_failure", racing)
    assert main(WRITE) == 1
    assert state(world) == ("failed", "format_error=1")


MAIL_FIRST = "2026-11-03_1800_mail-sales-a"
MAIL_REPLY = "2026-11-04_1800_mail-sales-a"


def test_a_mail_thread_window_marks_both_packets_and_packet_runs_keep_them(world: World) -> None:
    """跨包的窗口挡住的两个包都标；审阅第 3 条：write --packet 只取点名的包，切出来的窗口和平时不一样，不往回改。"""
    first = _mail("2026-11-03_1800", "Samples leave on Friday.", "2026-11-02T18:00:00+08:00", "2026-11-03T18:00:00+08:00", reply=False)
    reply = _mail("2026-11-04_1800", "Thanks, noted.", "2026-11-03T18:00:00+08:00", "2026-11-04T18:00:00+08:00", reply=True)
    deliver(world, first, reply, folder="inbox/mail-sales-a")
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    last_calls(world, "format_error")
    assert main(WRITE) == 1
    assert state_of(world, MAIL_FIRST) == state_of(world, MAIL_REPLY) == ("failed", "format_error=1")
    assert main([*WRITE, "--packet", MAIL_FIRST]) == 1
    assert state_of(world, MAIL_FIRST) == ("failed", "format_error=1")


def test_hints_name_retry_failed_only_where_it_helps() -> None:
    """审阅第 4 条：打码没打干净、太大的不用 --retry-failed（每次都重看，改好了照常抽就发）；--retry-failed 管
    整批，叫人加了会把别的截断、拒答过的也重调一遍。"""
    plain = extraction_message("p", "too_big=1;unredacted=1")
    assert "javmem extract --retry-failed" not in plain and "照常跑 javmem extract，不用 --retry-failed" in plain
    assert "javmem extract --retry-failed" in extraction_message("p", "format_error=1;unredacted=1")
    rechecked = extraction_message("p", "recheck_unredacted=1")
    assert "javmem extract --recheck" in rechecked and "--retry-failed" not in rechecked
    assert "--retry-failed-recheck" in extraction_message("p", "recheck_length=1")
    assert "None" not in extraction_message("p", "None=1")


def test_a_real_recheck_truncation_marks_recheck_length(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.cli as cli

    model = FakeModel(["ok", "length"])  # 第一次是 L3，正常回；第二次是复核，截断
    monkeypatch.setattr(cli, "FakeModel", lambda: model)
    deliver(world, day_packet("2026-11-01", NUMBERS))
    main(["extract", "--library", "prod", "--fake", "--recheck"])
    assert main([*WRITE, "--recheck"]) == 1
    assert state(world) == ("failed", "recheck_length=1")
    assert "javmem extract --recheck --retry-failed-recheck" in failed_items(world)[0].message


def test_a_prompt_change_takes_a_failed_packet_back_to_the_queue(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    extract_then(world, "content_rejected")
    assert main(WRITE) == 1 and state(world) == ("failed", "content_rejected=1")
    monkeypatch.setenv("JAVMEM_L3_PROMPT", "l3-v16")  # 键跟着变：这个键还没抽过，不是重调也没用的
    assert main(WRITE) == 1
    assert state(world) == ("queued", None)


def test_a_verify_error_takes_a_failed_packet_back_to_the_queue(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.pipeline import verify as verify_module

    deliver(world, day_packet("2026-11-01", LINE))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    mark_failed(world)

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("核对的代码出了毛病")

    monkeypatch.setattr(verify_module, "verify_window", broken)
    main(WRITE)
    assert state(world) == ("queued", None)  # 核对出错是代码的毛病，修好重跑，不算重调也没用


def test_a_written_window_is_skipped_and_the_packet_finishes_after_the_retry(world: World) -> None:
    """一个包几个窗口、第二个截断：写过的下次跳过，包仍是 failed；--retry-failed 以后从跳过的那条路改成 done。"""
    packet = "2026-11-02_2330_desk-bot"
    lines = [f"- [{(480 + index) // 60:02d}:{(480 + index) % 60:02d}] 我：第 {index} 件样品的尺寸记下了。" for index in range(1, 200)]
    heading = "## " + "原话" + " · " + "2026-11-02 08:01–11:19\n"  # 拼出来，免得和真实包的小节标题连着 8 个字一样（片段检查）
    deliver(world, build_packet(stamp="2026-11-02_2330", body=heading + "\n".join(lines) + "\n",
                                created_at="2026-11-02T23:30:00+08:00", covers_from="2026-11-02T00:00:00+08:00",
                                covers_to="2026-11-02T23:30:00+08:00"))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    store = sqlite3.connect(world.data / "l3" / "l3.db")
    try:
        windows = [row[0] for row in store.execute("SELECT window_id FROM windows ORDER BY window_id")]
        assert len(windows) >= 2
        key = store.execute("SELECT cache_key FROM windows WHERE window_id = ?", (windows[1],)).fetchone()[0]
        store.execute("DELETE FROM windows WHERE cache_key = ?", (key,))
        store.execute("DELETE FROM results WHERE cache_key = ?", (key,))
        store.execute("UPDATE calls SET status = 'length' WHERE cache_key = ?", (key,))
        store.commit()
    finally:
        store.close()
    assert main(WRITE) == 1 and state_of(world, packet) == ("failed", "length=1")
    assert main(WRITE) == 1 and state_of(world, packet) == ("failed", "length=1")  # 写过的窗口跳过，还是 failed
    assert main(["extract", "--library", "prod", "--fake", "--retry-failed"]) == 0
    assert main(WRITE) == 0
    assert state_of(world, packet) == ("done", None)


def test_a_replacement_during_write_leaves_the_new_revision_queued(world: World) -> None:
    """写账中间收件换了版本：旧版本不动、不算标了，新版本留着 queued。"""
    item = unsent(world)
    leaky = Checked(item.window, replace(item.rendered, leftover=("密钥",)), item.cache_key, None, None, NOT_SENT)
    newer = day_packet("2026-11-01", "我：样品箱周六寄出。").replace("privacy: normal", "privacy: normal\nrevision: 2")
    deliver(world, newer)
    conn = open_ledger(world.settings.library("prod"))
    try:
        writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
        with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
            run = run_write([leaky], writer, packets=[(PACKET, 1)], store=store, cache=cache,
                            embedder=FakeEmbeddings(), sleep=lambda _: None)
    finally:
        conn.close()
    assert run.failed == []
    assert state_of(world, PACKET, 2) == ("queued", None)
    assert failed_items(world) == []
