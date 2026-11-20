"""J4.2 后一半：L3 抽之前放进账本里的现有关系（javmem.pipeline.prer，版本 r1，提示词 l3-v22、l3-v23）。例子都是编的。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from helpers import World, file_name
from test_relcheck import BANK, OWNER, STOCK, relation
from test_relations_write import bank_extract, resent
from test_write import day_packet, deliver, write

from javmem.cli import main
from javmem.llm import FakeModel
from javmem.packet import parse_packet
from javmem.pipeline import prer, recheck, relcheck
from javmem.pipeline.batch import load_queued, queued_packets
from javmem.pipeline.extract import (
    PRE_A_PROMPTS,
    PRE_R_PROMPTS,
    PROMPTS,
    ExtractOptions,
    load_prompt,
    prepare,
    prompt_fingerprint,
    run_extraction,
)
from javmem.pipeline.normalize import normalize
from javmem.pipeline.preb import PRE_B_VERSION, window_context
from javmem.pipeline.reljudge import _parse_window
from javmem.pipeline.render import render_window
from javmem.pipeline.store import RELATIONS_HEADER, ResultStore, strip_known_relations, strip_pre_notes
from javmem.pipeline.verify import verify_windows
from javmem.pipeline.window import Window, build_windows
from javmem.redaction import default_patterns
from javmem.withdraw import withdraw

PATTERNS = default_patterns()
OPTIONS = ExtractOptions(sleep=lambda seconds: None, concurrency=1)
SOL_R, LUNA_R = "l3-v22", "l3-v23"  # l3-v20、l3-v21 加放进现有关系那一句
DAY1, DAY5 = "2026-11-01_2330_desk-bot", "2026-11-05_2330_desk-bot"


def bank_line(seen: str = "2026-11-01", state: str = "生效") -> str:
    return f"- X1 票据 托管于 海港银行（{state}；上次说到 {seen}）"


def queued_windows(world: World, library: str = "prod") -> list[Window]:
    return build_windows(load_queued(world.settings, library).packets)


def view_of(world: World) -> prer.LedgerView:
    return prer.LedgerView.load(world.settings.library("prod").db_path)


def pick(world: World, window: Window, notes: dict[str, list[list[str]]] | None = None) -> prer.Picked:
    return view_of(world).pick(window, render_window(window, PATTERNS), notes or {}, PATTERNS)


def banked(world: World, day: str = "2026-11-01", line: str = "我：X1 票据在海港银行。") -> None:
    """账本里先有一条：X1 票据托管于海港银行（这个包写完了）。"""
    deliver(world, day_packet(day, line))
    write(world, bank_extract)


def own_extract(window: Window) -> dict[str, object]:
    """写着 X2 的每一行：主人持有 X2。"""
    found = [relation(OWNER, "持有", STOCK, (line.ref, line.text.rstrip("。"))) for line in window.targets if "X2" in line.text]
    return {"entities": [OWNER, STOCK], "facts": [], "relations": found}


NOTES = [{"key": f"n{number}", "mention": f"X{number} 票据", "name": f"X{number} 票据", "type": "订单或合同"} for number in (1, 3, 4)]


def notes_extract(window: Window) -> dict[str, object]:
    """一行里写着哪张票据和海港银行：那张票据托管于海港银行。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        for note in NOTES:
            if note["name"] in text and "海港银行" in text:
                found.append(relation(note, "托管于", BANK, (line.ref, text)))
    return {"entities": [*NOTES, BANK], "facts": [], "relations": found}


# ---------- 挑哪几条 ----------


def test_a_later_window_naming_both_ends_gets_the_known_relation(world: World) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行，下周再看。"))
    (window,) = queued_windows(world)
    got = pick(world, window)
    assert got.block == "\n".join(["", RELATIONS_HEADER, bank_line()])
    assert len(got.relations) == 1 and got.relations[0].startswith("R")
    assert [source[0] for source in got.sources] == [DAY1]  # 当时的证据：包、版本、句子
    assert got.detail == {"candidates": 1, "used": 1, "over_cap": 0, "unredacted": 0}


def test_only_one_end_in_the_window_is_not_put_in(world: World) -> None:
    """一头在的（海港银行不在这个窗口里）不放：模型照着写，R2 会挡（要放得另改 R2、C 的问法）。"""
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据的事下周再说。"))
    (window,) = queued_windows(world)
    assert pick(world, window).block == ""


def test_a_name_marked_by_pre_b_counts_as_in_the_window(world: World) -> None:
    """原文写的是“那家”，认人认事标了〔指：那家 = 海港银行〕：算这个窗口里有海港银行。"""
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还放在那家。"))
    (window,) = queued_windows(world)
    assert pick(world, window).block == ""
    got = pick(world, window, {window.targets[0].ref: [["那家", "海港银行"]]})
    assert got.block.endswith(bank_line())


def test_the_owner_is_always_there_but_cannot_be_the_only_end(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：我买了 X2。"))
    write(world, own_extract)
    deliver(world, day_packet("2026-11-05", "我：今天先不看了。"), day_packet("2026-11-06", "我：X2 再买一点。"))
    quiet, named = queued_windows(world)
    assert pick(world, quiet).block == ""  # 只有主人：哪个窗口都有，不能只靠它
    assert pick(world, named).block.endswith("- 主人 持有 X2（生效；上次说到 2026-11-01）")


def test_another_scope_does_not_see_it(world: World) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。", domain="[personal]"))
    (window,) = queued_windows(world)
    assert pick(world, window).block == ""  # 分区不一样（D30）


def test_pending_ended_and_withdrawn_relations_stay_out(world: World) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    (window,) = queued_windows(world)
    assert pick(world, window).block
    conn = world.ledger("prod")
    try:
        for change in (
            "UPDATE relations SET status = 'pending', pending_reason = 'other'",
            "UPDATE relations SET state = 'ended'",
            "UPDATE relations SET withdrawn_at = '2026-11-06T00:00:00+08:00'",
        ):
            with conn:
                saved = [tuple(row) for row in conn.execute("SELECT relation_id, status, pending_reason, state, withdrawn_at FROM relations")]
                conn.execute(change)
            assert pick(world, window).block == "", change
            with conn:
                for relation_id, status, reason, state, withdrawn in saved:
                    conn.execute(
                        "UPDATE relations SET status = ?, pending_reason = ?, state = ?, withdrawn_at = ? WHERE relation_id = ?",
                        (status, reason, state, withdrawn, relation_id),
                    )
    finally:
        conn.close()
    assert pick(world, window).block  # 改回来又有了：上面每一样都是它自己挡的


def test_a_packet_does_not_get_relations_only_it_supports(world: World) -> None:
    """自己不证明自己：同一个包重发、重抽时，证据全在它自己身上的关系不放。"""
    banked(world)
    deliver(world, resent("2026-11-01", "我：X1 票据在海港银行。"))
    (window,) = queued_windows(world)
    assert window.target_packets == [DAY1]
    assert pick(world, window).block == ""


def test_a_late_older_packet_does_not_see_later_relations(world: World) -> None:
    """晚到的旧包：最早那处说法比这个窗口最后一句话晚的不放。"""
    banked(world, "2026-11-05", "我：X1 票据在海港银行。")
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行，先放着。"))
    (window,) = queued_windows(world)
    assert pick(world, window).block == ""


def test_at_most_so_many_and_the_newest_first(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    for day, number in (("2026-11-01", 1), ("2026-11-02", 3), ("2026-11-03", 4)):
        deliver(world, day_packet(day, f"我：X{number} 票据在海港银行。"))
    write(world, notes_extract)
    deliver(world, day_packet("2026-11-05", "我：X1 票据、X3 票据、X4 票据都在海港银行。"))
    (window,) = queued_windows(world)
    monkeypatch.setattr(prer, "MAX_RELATIONS", 2)
    got = pick(world, window)
    lines = got.block.split("\n")[2:]
    assert [line.split(" ")[1] for line in lines] == ["X4", "X3"]  # 最后一次说到从近到远
    assert got.detail["candidates"] == 3 and got.detail["used"] == 2 and got.detail["over_cap"] == 1
    monkeypatch.setattr(prer, "MAX_RELATIONS", 40)
    monkeypatch.setattr(prer, "MAX_CHARS", len(lines[0]) + 1)  # 字数也有上限
    assert len(pick(world, window).block.split("\n")[2:]) == 1


def test_relations_without_the_owner_or_a_bot_go_first(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-03", "我：我买了 X2。"))
    write(world, own_extract)
    deliver(world, day_packet("2026-11-05", "我：X2、X1 票据都在海港银行。"))
    (window,) = queued_windows(world)
    lines = pick(world, window).block.split("\n")[2:]
    assert lines[0] == bank_line() and lines[1].startswith("- 主人 持有 X2")  # 主人那条说得更近，照样排在后面
    monkeypatch.setattr(prer, "MAX_RELATIONS", 1)
    assert pick(world, window).block.split("\n")[2:] == [bank_line()]


def test_names_are_escaped_and_redacted() -> None:
    assert prer.clean_name("A【正文】〔B〕\nC") == "A［正文］［B］ C"
    line = prer.relation_line("X1 票据", "托管于", "海港银行", state="paused", valid_from="2026-09", as_of="2026-10-13T09:00:00+08:00")
    assert line == "- X1 票据 托管于 海港银行（暂停；2026-09 起；上次说到 2026-10-13）"
    assert prer.relation_line("小周", "负责", "X1 票据", org="东岸证券", state="active", as_of=None) == (
        "- 小周 负责 X1 票据（代表 东岸证券；生效）"
    )
    assert prer.relation_line("老王", "亲属", "主人", kinship="舅舅", state="active", as_of=None) == "- 老王 亲属 主人（舅舅；生效）"


# ---------- 写进窗口 ----------


def test_the_block_goes_after_the_opening_line_and_strips_back(world: World) -> None:
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    (window,) = queued_windows(world)
    plain = render_window(window, PATTERNS)
    block = "\n".join(["", RELATIONS_HEADER, bank_line()])
    put = prer.apply_block(plain, block)
    rows = put.text.split("\n")
    assert rows[0] == plain.text.split("\n")[0] and rows[1] == "" and rows[2] == RELATIONS_HEADER and rows[3] == bank_line()
    assert rows[4] == "" and rows[5] == "【正文】"
    assert strip_known_relations(put.text) == plain.text == strip_pre_notes(put.text)
    assert put.shown == plain.shown and put.sha256 != plain.sha256
    assert prer.apply_block(plain, "") is plain
    assert _parse_window(put.text) == _parse_window(plain.text)  # 判断 C 取原话时看不到这一段
    assert relcheck.window_notes(put) == relcheck.window_notes(plain) == {}


def test_known_relations_go_on_top_of_a_and_b(world: World) -> None:
    """三样一起：A 的〔日子〕、B 的〔指〕在 T 行上，现有关系是开头说明下面另起的一段；去掉三样就是原样。"""
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据昨天还在那家。"))
    (window,) = queued_windows(world)
    ref = window.targets[0].ref
    with ResultStore(world.data / "l3") as store:
        from test_preb import save_notes

        save_notes(store, window, {ref: [["那家", "海港银行"]]})
        pre_r = prer.PreR(world.settings.library("prod").db_path)
        run_extraction([window], FakeModel(), store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R, pre_r=pre_r)
        _, rendered, _ = prepare([window], FakeModel(), PATTERNS, store=store, prompt=SOL_R)[0]
    assert "〔日子：昨天 = 2026-11-04〕〔指：那家 = 海港银行〕" in rendered.text and bank_line() in rendered.text
    assert strip_pre_notes(rendered.text) == render_window(window, PATTERNS).text


# ---------- 存下来、重放 ----------


def test_extraction_stores_the_block_and_later_steps_replay_it(world: World) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行，5 张。"))
    windows = queued_windows(world)
    pre_r = prer.PreR(world.settings.library("prod").db_path)
    fake = FakeModel()
    with ResultStore(world.data / "l3") as store:
        run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R, pre_r=pre_r)
        assert len(fake.calls) == 1 and RELATIONS_HEADER in fake.calls[0][1] and bank_line() in fake.calls[0][1]
        plain = render_window(windows[0], PATTERNS)
        assert store.prer_notes(windows[0].window_id, plain.sha256, prer.PRE_R_VERSION, window_context(windows[0])) == (
            "\n".join(["", RELATIONS_HEADER, bank_line()])
        )
        key = prepare(windows, fake, PATTERNS, store=store, prompt=SOL_R)[0][2]
        conn = world.ledger("prod")
        try:
            with conn:
                conn.execute("UPDATE relations SET withdrawn_at = '2026-11-06T00:00:00+08:00'")  # 后来账本变了
        finally:
            conn.close()
        assert prepare(windows, fake, PATTERNS, store=store, prompt=SOL_R)[0][2] == key  # 照存下的：先存的为准
        assert verify_windows(windows, store, fake, PATTERNS, prompt=SOL_R)[0].extraction is not None
        again = FakeModel()
        run_extraction(windows, again, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R, pre_r=pre_r)
        assert again.calls == []  # 存过的不再调
        assert RELATIONS_HEADER not in prepare(windows, fake, PATTERNS, store=store, prompt="l3-v20")[0][1].text


def test_a_window_with_nothing_to_add_still_stores_a_row(world: World) -> None:
    deliver(world, day_packet("2026-11-05", "我：明天 9 点开会。"))
    windows = queued_windows(world)
    fake = FakeModel()
    with ResultStore(world.data / "l3") as store:
        run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=LUNA_R,
                       pre_r=prer.PreR(world.settings.library("prod").db_path))
        plain = render_window(windows[0], PATTERNS)
        assert store.prer_notes(windows[0].window_id, plain.sha256, prer.PRE_R_VERSION, window_context(windows[0])) == ""
    assert RELATIONS_HEADER not in fake.calls[0][1]


def test_a_relations_prompt_needs_the_ledger(world: World) -> None:
    deliver(world, day_packet("2026-11-05", "我：明天 9 点开会。"))
    with ResultStore(world.data / "l3") as store, pytest.raises(ValueError):
        run_extraction(queued_windows(world), FakeModel(), store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R)


def test_a_dry_run_works_it_out_without_storing(world: World) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    windows = queued_windows(world)
    pre_r = prer.PreR(world.settings.library("prod").db_path)
    with ResultStore(world.data / "l3") as store:
        _, rendered, _ = prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL_R, fresh_r=pre_r)[0]
        assert bank_line() in rendered.text
        plain = render_window(windows[0], PATTERNS)
        assert store.prer_notes(windows[0].window_id, plain.sha256, prer.PRE_R_VERSION, window_context(windows[0])) is None


def test_private_windows_get_nothing(world: World) -> None:
    banked(world)
    raw = day_packet("2026-11-05", "我：X1 票据还在海港银行。").replace("privacy: normal", "privacy: private")
    windows = build_windows([normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))])
    assert windows[0].scope.private
    with ResultStore(world.data / "l3") as store:
        prer.PreR(world.settings.library("prod").db_path).annotate(windows, store, PATTERNS)
        with sqlite3.connect(store.path) as conn:
            assert conn.execute("SELECT count(*) FROM prer_notes").fetchone()[0] == 0


# ---------- 提示词 ----------


def test_the_two_prompts_are_registered_and_add_one_line() -> None:
    for new, old in ((SOL_R, "l3-v20"), (LUNA_R, "l3-v21")):
        assert PROMPTS[new] == prompt_fingerprint(new)
        assert new in PRE_A_PROMPTS and new in PRE_R_PROMPTS and old not in PRE_R_PROMPTS
        old_lines, new_lines = load_prompt(old).split("\n"), load_prompt(new).split("\n")
        added = [at for at, line in enumerate(new_lines) if line not in old_lines]
        assert len(added) == 1
        at = added[0]
        assert new_lines[at].startswith("- 开头说明下面可能有一段【账本里已经记着的关系")
        assert new_lines[at - 1].startswith("- 〔日子：") and new_lines[:at] + new_lines[at + 1 :] == old_lines


# ---------- 命令行：一次一个包 ----------


def test_cli_extracts_one_packet_at_a_time_and_write_says_how_many_wait(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-06", "我：明天 9 点开会。"), day_packet("2026-11-05", "我：X1 票据还在海港银行，5 张。"))
    monkeypatch.setenv("JAVMEM_L3_PROMPT", SOL_R)
    capsys.readouterr()
    assert main(["extract", "--library", "prod", "--fake", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "现有关系：r1" in out and f"这次只抽 {DAY5}" in out
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        assert conn.execute("SELECT count(*) FROM prer_notes").fetchone()[0] == 0  # 空跑不存
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    out = capsys.readouterr().out
    assert f"这次只抽 {DAY5}" in out and "还有 1 个包排着" in out
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        texts = [row[0] for row in conn.execute("SELECT input_text FROM windows")]
    assert len(texts) == 1 and RELATIONS_HEADER in texts[0] and bank_line() in texts[0]
    assert main(["write", "--library", "prod", "--packet", DAY5, "--fake", "--fake-embeddings"]) == 0
    assert "还有 1 个包排着" in capsys.readouterr().out
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert "这次只抽 2026-11-06_2330_desk-bot" in capsys.readouterr().out
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert "排着" not in capsys.readouterr().out


def test_cli_naming_a_later_packet_warns(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行，5 张。"), day_packet("2026-11-06", "我：明天 9 点开会。"))
    monkeypatch.setenv("JAVMEM_L3_PROMPT", SOL_R)
    capsys.readouterr()
    assert main(["extract", "--library", "prod", "--fake", "--packet", "2026-11-06_2330_desk-bot"]) == 0
    out = capsys.readouterr().out
    assert "这次只抽 2026-11-06_2330_desk-bot" in out and "导出更早的 1 个包还没写账" in out


def test_cli_other_prompts_extract_the_whole_queue(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    deliver(world, day_packet("2026-11-05", "我：5 张。"), day_packet("2026-11-06", "我：6 张。"))
    monkeypatch.setenv("JAVMEM_L3_PROMPT", "l3-v20")
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert "这次只抽" not in capsys.readouterr().out
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        assert conn.execute("SELECT count(DISTINCT window_id) FROM windows").fetchone()[0] == 2


def test_queued_packets_come_in_export_order(world: World) -> None:
    deliver(world, day_packet("2026-11-06", "我：6 张。"), day_packet("2026-11-05", "我：5 张。"))
    assert [(packet_id, state) for packet_id, _, state in queued_packets(world.settings, "prod")] == [
        (DAY5, "queued"), ("2026-11-06_2330_desk-bot", "queued")
    ]


# ---------- 撤回、彻底删除 ----------


def extracted_with_block(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """账本里有 DAY1 的关系；DAY5 照 l3-v22 抽了（放进了那条），还没写账。"""
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行，5 张。"))
    monkeypatch.setenv("JAVMEM_L3_PROMPT", SOL_R)
    assert main(["extract", "--library", "prod", "--fake"]) == 0


def test_withdrawing_where_a_known_relation_came_from_requeues_the_window_that_used_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    extracted_with_block(world, monkeypatch)
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert world.rows("SELECT extraction FROM packets WHERE packet_id = ?", DAY5)[0][0] == "done"
    withdraw(world.settings, "prod", packets=[DAY1])
    assert world.rows("SELECT extraction FROM packets WHERE packet_id = ?", DAY5)[0][0] == "queued"  # 改回排队、重抽
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        assert conn.execute("SELECT count(*) FROM windows WHERE window_id LIKE ?", (DAY5 + "%",)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM prer_notes").fetchone()[0] == 0


def test_store_finds_and_forgets_blocks_built_from_withdrawn_content(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    extracted_with_block(world, monkeypatch)
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        (sources,) = conn.execute("SELECT sources FROM prer_notes WHERE window_id = ?", (DAY5 + "#w1",)).fetchone()
    sentence = json.loads(sources)[0][2]
    assert sentence.startswith(DAY1)
    with ResultStore(world.data / "l3") as store:
        doomed, targets = store.doomed(packets={DAY1})
        assert DAY5 + "#w1" in {window_id for window_id, _ in doomed} and DAY5 in targets
        noted, again = store.doomed_by_notes(packets={DAY1})  # 彻底删除、重发成 private 那几处靠它改回排队
        assert [window_id for window_id, _ in noted] == [DAY5 + "#w1"] and again == {DAY5}
        assert DAY5 + "#w1" in {window_id for window_id, _ in store.doomed(sentences={sentence})[0]}  # 单条撤回那一句也算
        assert DAY5 + "#w1" not in {window_id for window_id, _ in store.doomed(packets={"2026-11-09_2330_desk-bot"})[0]}
        assert store.forget_prer(packets={DAY1}) == 1
        assert store.forget_prer(packets={DAY1}) == 0


def test_purging_one_revision_keeps_the_other_revisions_block(world: World) -> None:
    """同一个包重发、文字没变（窗口编号、没加标注的指纹一样），两个版本放进去的不一样：彻底删除旧版本只删旧版本那一行，
    新版本的照旧对得上键。"""
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行，5 张。"))
    pre_r = prer.PreR(world.settings.library("prod").db_path)
    fake = FakeModel()
    with ResultStore(world.data / "l3") as store:
        first = queued_windows(world)
        run_extraction(first, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R, pre_r=pre_r)
        conn = world.ledger("prod")
        try:
            with conn:  # 账本又有人说到了：新版本放进去的那一行“上次说到”不一样
                conn.execute("UPDATE relations SET as_of = '2026-11-03T10:30:00+08:00', as_of_epoch = as_of_epoch + 172800")
        finally:
            conn.close()
        deliver(world, resent("2026-11-05", "我：X1 票据还在海港银行，5 张。"))
        second = queued_windows(world)
        assert second[0].window_id == first[0].window_id and window_context(second[0]) != window_context(first[0])
        run_extraction(second, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R, pre_r=pre_r)
        plain = render_window(second[0], PATTERNS).sha256
        assert render_window(first[0], PATTERNS).sha256 == plain
        new_block = store.prer_notes(second[0].window_id, plain, prer.PRE_R_VERSION, window_context(second[0]))
        assert new_block is not None and bank_line("2026-11-03") in new_block
        doomed, _ = store.doomed(revisions={(DAY5, 1)})
        assert len(doomed) == 1
        store.delete_windows(doomed)
        store.forget_prer(revisions={(DAY5, 1)})
        assert store.prer_notes(second[0].window_id, plain, prer.PRE_R_VERSION, window_context(second[0])) == new_block
        assert store.prer_notes(first[0].window_id, plain, prer.PRE_R_VERSION, window_context(first[0])) is None
        assert verify_windows(second, store, fake, PATTERNS, prompt=SOL_R)[0].extraction is not None


# ---------- 别的地方看不到这一段 ----------


def test_the_fact_recheck_does_not_send_the_block(world: World) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行，5 张。"))
    windows = queued_windows(world)
    fake = FakeModel()
    with ResultStore(world.data / "l3") as store:
        run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R,
                       pre_r=prer.PreR(world.settings.library("prod").db_path))
        (item,) = verify_windows(windows, store, fake, PATTERNS, prompt=SOL_R)
    assert RELATIONS_HEADER in item.rendered.text
    request = recheck.build_request(item, PATTERNS)
    assert request is not None and RELATIONS_HEADER not in request.text and "X1 票据还在海港银行" in request.text


def test_store_context_matches_window_context(world: World) -> None:
    """删窗口时按窗口那一行算回 context，要和 preb.window_context 一样（不然删错别的版本的）。"""
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行，5 张。"), day_packet("2026-11-06", "我：6 张。"))
    windows = queued_windows(world)
    fake = FakeModel()
    with ResultStore(world.data / "l3") as store:
        run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R,
                       pre_r=prer.PreR(world.settings.library("prod").db_path))
        rows = {row["window_id"]: row for row in store.windows()}
        from javmem.pipeline.store import _row_context

        for window in windows:
            assert _row_context(rows[window.window_id]) == window_context(window)


def test_pre_b_version_is_what_the_names_come_from() -> None:
    assert PRE_B_VERSION == "b4"  # 读〔指〕标注用的版本，B 换版本时这里要跟着看


# ---------- 审阅查出来的（db9f431 的审阅，修之前都是红的） ----------

KNOTE = {"key": "e2", "mention": "蓝签 票据", "name": "X1 票据", "type": "订单或合同"}


def personal_extract(window: Window) -> dict[str, object]:
    """personal 分区的包：蓝签 票据（就是 X1 票据）托管于海港银行。"""
    found = [relation(KNOTE, "托管于", BANK, (line.ref, line.text.rstrip("。"))) for line in window.targets
             if "蓝签 票据" in line.text and "海港银行" in line.text]
    return {"entities": [KNOTE, BANK], "facts": [], "relations": found}


def test_review_an_alias_from_another_scope_does_not_count(world: World) -> None:
    """D30（和 write._alias_candidates 一样）：别的可见范围里学来的“蓝签 票据 = X1 票据”，不能拿来认这个窗口里的蓝签 票据。"""
    deliver(world, day_packet("2026-10-20", "我：蓝签 票据在海港银行。", domain="[personal]"))
    write(world, personal_extract)
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：蓝签 票据还在海港银行。"))
    (window,) = queued_windows(world)
    assert pick(world, window).block == ""
    deliver(world, day_packet("2026-11-06", "我：蓝签 票据还在海港银行。", domain="[personal]"))
    (_, personal) = queued_windows(world)
    assert pick(world, personal).block.split("\n")[2:] == [bank_line("2026-10-20")]  # 在它自己的范围里认得，只放这个范围的那条


def test_review_own_earlier_claims_do_not_make_a_later_relation_known(world: World) -> None:
    """重抽旧包（单条撤回了不相干的一句）：去掉这个包自己的说法，这条关系 11-05 才有人说，不放。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。", "我：下午再说。", "我：明天开会。"),
            day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    write(world, bank_extract)
    (sentence_id,) = [row[0] for row in world.rows("SELECT sentence_id FROM raw_sentences WHERE packet_id = ? AND text LIKE ?",
                                                    DAY1, "%开会%")]
    withdraw(world.settings, "prod", sentences=[sentence_id])
    (window,) = queued_windows(world)
    assert window.target_packets == [DAY1]
    assert pick(world, window).block == ""


def test_review_b_labelled_later_gets_a_fresh_block(world: World) -> None:
    """B 这一回没标上（到时间、本机模型出错）：先算的 R 不能存死；下一回 B 标上了，R 照新的〔指〕重算。"""
    from test_preb import save_notes

    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还放在那家。"))
    (window,) = queued_windows(world)
    pre_r = prer.PreR(world.settings.library("prod").db_path)
    with ResultStore(world.data / "l3") as store:
        pre_r.annotate([window], store, PATTERNS)
        save_notes(store, window, {window.targets[0].ref: [["那家", "海港银行"]]})
        pre_r.annotate([window], store, PATTERNS)
        _, rendered, _ = prepare([window], FakeModel(), PATTERNS, store=store, prompt=SOL_R)[0]
    assert "〔指：那家 = 海港银行〕" in rendered.text and bank_line() in rendered.text


KNAME = {"key": "e2", "mention": "X1 票据", "name": "蓝签 票据", "type": "订单或合同"}
XNAME = {"key": "e2", "mention": "X1 票据", "name": "X1 票据", "type": "订单或合同"}


def staged_extract(window: Window) -> dict[str, object]:
    """第一个包把这张票据叫蓝签 票据（只有它这么叫）；后面的包叫 X1 票据。"""
    from test_relcheck import BROKER

    found = []
    entities = [BANK, BROKER]
    for line in window.targets:
        text = line.text.rstrip("。")
        if "蓝签 票据" in text:
            found.append(relation(KNAME, "托管于", BROKER, (line.ref, text)))
            entities.append(KNAME)
        elif "X1 票据" in text and "海港银行" in text:
            found.append(relation(XNAME, "托管于", BANK, (line.ref, text)))
            entities.append(XNAME)
    return {"entities": entities, "facts": [], "relations": found}


def test_review_withdrawing_where_a_shown_name_came_from_dooms_the_window(world: World) -> None:
    """放进去的那一段写着实体的名字：名字只出自撤回了的包的，用了它的窗口要删、要重抽（名字的出处也记进 sources）。"""
    p1, p3 = "2026-11-01_2330_desk-bot", DAY5
    deliver(world, day_packet("2026-11-01", "我：蓝签 票据就是 X1 票据，放在东岸证券。"))
    write(world, staged_extract)
    deliver(world, day_packet("2026-11-03", "我：X1 票据在海港银行。"))
    write(world, staged_extract)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    with ResultStore(world.data / "l3") as store:
        run_extraction(queued_windows(world), FakeModel(), store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R,
                       pre_r=prer.PreR(world.settings.library("prod").db_path))
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        before = [row[0] for row in conn.execute("SELECT input_text FROM windows WHERE window_id LIKE ?", (p3 + "%",))]
    assert any("蓝签 票据" in text for text in before)
    withdraw(world.settings, "prod", packets=[p1])
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        after = [row[0] for row in conn.execute("SELECT input_text FROM windows WHERE window_id LIKE ?", (p3 + "%",))]
    assert not any("蓝签 票据" in text for text in after)


def test_review_english_names_in_english_sentences(world: World) -> None:
    """全是英文数字的名字，在英文句子里也认得出（前后是空格、标点）；连着别的字母的不算。"""
    deliver(world, day_packet("2026-11-01", "我：我买了 X2。"))
    write(world, own_extract)
    deliver(world, day_packet("2026-11-05", "我：bought more X2 today."), day_packet("2026-11-06", "我：bought more X2B today."))
    english, longer = queued_windows(world)
    assert pick(world, english).block.endswith("- 主人 持有 X2（生效；上次说到 2026-11-01）")
    assert pick(world, longer).block == ""


def test_review_a_frozen_relation_without_claims_still_goes_in(world: World) -> None:
    """冻结的照放：说法没了、只剩冻结标记的关系（证据还指着旧版本）也放。"""
    banked(world)
    (relation_id,) = [row["relation_id"] for row in world.rows("SELECT relation_id FROM relations")]
    assert main(["freeze", "--library", "prod", "--relation", relation_id]) == 0
    deliver(world, resent("2026-11-01", "我：今天没什么事。"))
    write(world, bank_extract)
    assert world.rows("SELECT count(*) FROM relation_claims")[0][0] == 0
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    (window,) = queued_windows(world)
    assert "- X1 票据 托管于 海港银行（生效" in pick(world, window).block


def test_review_mail_one_packet_then_plain_write_moves_on(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """邮件包一次抽一个：抽时切出来的窗口，要和写账时切出来的一样（不然写不进去，一直抽同一个）；写账不点名包时也只写最早那个。"""
    from test_quote_only import deliver as deliver_mail
    from test_quote_only import mail, mail_packet

    first = mail_packet(3, mail("<m1@example.org>", "2026-11-03T09:00:00+08:00", ["Crates leave on Friday."]))
    second = mail_packet(4, mail("<m2@example.org>", "2026-11-04T09:00:00+08:00", ["Thanks, noted."], reply_to="<m1@example.org>"))
    ids = deliver_mail(world, first, second)
    monkeypatch.setenv("JAVMEM_L3_PROMPT", SOL_R)
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert f"javmem write --packet {ids[0]}" in capsys.readouterr().out
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) in (0, 1)
    assert world.rows("SELECT extraction FROM packets WHERE packet_id = ?", ids[0])[0][0] == "done"
    assert world.rows("SELECT extraction FROM packets WHERE packet_id = ?", ids[1])[0][0] == "queued"


def test_review_one_packet_windows_match_write_packet_windows(world: World) -> None:
    """拆开的邮件包 M1 ← M2 ← M3，M2 写完了，M1、M3 排着：只抽 M1 时窗口里不能带上 M3 的上一包 M2 当上文。"""
    from helpers import build_mail_packet
    from test_quote_only import deliver as deliver_mail
    from test_quote_only import mail

    from javmem.cli import _one_packet

    def part(day: int, prev: str | None, message: str, reply: str | None, text: str) -> str:
        fields = {"continues": prev} if prev else {}
        return build_mail_packet(
            sections=mail(message, f"2026-11-{day:02d}T09:00:00+08:00", [text], reply_to=reply),
            packet_id=f"2026-11-{day:02d}_1800_mail-sales-a",
            covers_from=f"2026-11-{day - 1:02d}T18:00:00+08:00", covers_to=f"2026-11-{day:02d}T18:00:00+08:00", **fields,
        )

    m1, m2 = "2026-11-03_1800_mail-sales-a", "2026-11-04_1800_mail-sales-a"
    deliver_mail(world, part(3, None, "<m1@example.org>", None, "Crates leave on Friday."),
                 part(4, m1, "<m2@example.org>", "<m1@example.org>", "Thanks, noted."),
                 part(5, m2, "<m3@example.org>", "<m2@example.org>", "Arrived today."))
    world.execute("UPDATE packets SET extraction = 'done' WHERE packet_id = ?", m2)
    one, _ = _one_packet(world.settings, "prod", load_queued(world.settings, "prod"))
    assert [packet.packet_id for packet in one.packets] == [m1]
    named = load_queued(world.settings, "prod", packet_ids=[m1])
    at_extract = {w.window_id: render_window(w, PATTERNS).text for w in build_windows(one.packets, context_pool=one.context)}
    at_write = {w.window_id: render_window(w, PATTERNS).text for w in build_windows(named.packets, context_pool=named.context)}
    assert at_extract == at_write


NOTE_X1 = {"key": "e2", "mention": "X1 票据", "name": "X1 票据", "type": "订单或合同"}


def bot_extract(window: Window) -> dict[str, object]:
    """desk-bot 负责 X1 票据（L3 把 desk-bot 写成人，Bot 名单里有它，L5 改成 Bot）。"""
    bot = {"key": "e8", "mention": "desk-bot", "name": "desk-bot", "type": "人"}
    found = [relation(bot, "负责", NOTE_X1, (line.ref, line.text.rstrip("。")), speaker="desk-bot")
             for line in window.targets if "X1 票据" in line.text]
    return {"entities": [bot, NOTE_X1], "facts": [], "relations": found}


def test_review_a_bot_counts_when_it_speaks_in_the_window(world: World, tmp_path: Path) -> None:
    from test_bots import BOTS
    from test_lexicon_l5 import lexicon_path, write_lex

    deliver(world, day_packet("2026-11-01", "desk-bot：desk-bot 负责盯 X1 票据。"))
    write_lex(world, bot_extract, lexicon_path(tmp_path, BOTS)).close()
    rows = world.rows("SELECT e.type FROM relations AS r JOIN entities AS e ON e.entity_id = r.subject_entity_id")
    assert [row[0] for row in rows] == ["Bot"]  # Bot 名单里有 desk-bot：写成 Bot 负责 X1 票据
    deliver(world, day_packet("2026-11-05", "desk-bot：X1 票据今天没变化。"), day_packet("2026-11-06", "我：X1 票据今天没变化。"))
    spoke, quiet = queued_windows(world)
    assert pick(world, spoke).block  # Bot 在这个窗口里说话：算在
    assert pick(world, quiet).block == ""  # Bot 没说话、名字也没出现：不算在


def test_review_sensitive_marks_must_be_a_subset(world: World) -> None:
    """关系带着敏感标记的，只放进敏感标记包含它的窗口（测试的来源登记表里 books-bot 能带 payroll）。"""

    def books(day: str, line: str, marked: bool) -> str:
        raw = day_packet(day, line, source="books-bot", domain="[finance]")
        return raw.replace("privacy: normal", "privacy: normal\nsensitive: [payroll]") if marked else raw

    deliver(world, books("2026-11-01", "我：X1 票据在海港银行。", True), folder="inbox/books-bot")
    write(world, bank_extract)
    rows = world.rows("SELECT scope_sensitive FROM relations")
    assert rows and json.loads(rows[0][0]) == ["payroll"]
    deliver(world, books("2026-11-05", "我：X1 票据还在海港银行。", False), books("2026-11-06", "我：X1 票据还在海港银行。", True),
            folder="inbox/books-bot")
    plain, marked = queued_windows(world)
    assert pick(world, plain).block == ""
    assert pick(world, marked).block


def test_review_a_line_still_unredacted_is_left_out(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    (window,) = queued_windows(world)
    monkeypatch.setattr(prer, "scan_types", lambda text, patterns: {"secret"} if "海港" in text else set())
    got = pick(world, window)
    assert got.block == "" and got.detail["unredacted"] == 1


def test_review_a_name_shared_by_two_entities_does_not_count(world: World) -> None:
    """一个写法对上两个实体（同名不同类）的不算：看不出是哪一个。"""
    banked(world)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    (window,) = queued_windows(world)
    assert pick(world, window).block  # 只有一个叫海港银行的时候放
    conn = world.ledger("prod")
    try:
        with conn:  # 账本里另有一个同名的“人”
            conn.execute(
                "INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at, name_key)"
                " VALUES ('E-same-name', '海港银行', '人', 'x', 'x', '海港银行')"
            )
    finally:
        conn.close()
    assert pick(world, window).block == ""


# ---------- 复审查出来的（5154f40 的复审，修之前都是红的） ----------

KSAID = {"key": "e2", "mention": "蓝签 票据", "name": "蓝签 票据", "type": "订单或合同"}


def staged_more(window: Window) -> dict[str, object]:
    """staged_extract 再加一种：后来的包也直接叫它蓝签 票据。"""
    from test_relcheck import BROKER

    found = []
    entities = [BANK, BROKER]
    for line in window.targets:
        text = line.text.rstrip("。")
        if "蓝签 票据就是" in text:
            found.append(relation(KNAME, "托管于", BROKER, (line.ref, text)))
            entities.append(KNAME)
        elif "蓝签 票据" in text:
            found.append(relation(KSAID, "托管于", BROKER, (line.ref, text)))
            entities.append(KSAID)
        elif "X1 票据" in text and "海港银行" in text:
            found.append(relation(XNAME, "托管于", BANK, (line.ref, text)))
            entities.append(XNAME)
    return {"entities": entities, "facts": [], "relations": found}


def test_review2_a_name_whose_proof_went_with_a_resend_still_dooms_on_withdrawal(world: World) -> None:
    """记下的那一句出处不只是撤回才会没：P1 重发掉了那一句，名字靠 P4 撑着；后来撤回 P4、账本改了名，P3 写着旧名字的窗口
    照样要删、要重抽（撤回以后照账本现在的样子核一遍，不靠出处的快照）。"""
    p3, p4 = DAY5, "2026-11-07_2330_desk-bot"
    deliver(world, day_packet("2026-11-01", "我：蓝签 票据就是 X1 票据，放在东岸证券。"))
    write(world, staged_more)
    deliver(world, day_packet("2026-11-03", "我：X1 票据在海港银行。"))
    write(world, staged_more)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    with ResultStore(world.data / "l3") as store:
        run_extraction(queued_windows(world), FakeModel(), store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R,
                       pre_r=prer.PreR(world.settings.library("prod").db_path))
    write(world, staged_more)
    deliver(world, day_packet("2026-11-07", "我：蓝签 票据还在东岸证券。"))
    write(world, staged_more)
    deliver(world, resent("2026-11-01", "我：今天没什么事。"))
    write(world, staged_more)
    assert world.rows("SELECT count(*) FROM entities WHERE canonical_name = '蓝签 票据'")[0][0] == 1  # 还靠 P4 撑着
    withdraw(world.settings, "prod", packets=[p4])
    assert world.rows("SELECT count(*) FROM entities WHERE canonical_name = '蓝签 票据'")[0][0] == 0  # 改了名
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        after = [row[0] for row in conn.execute("SELECT input_text FROM windows WHERE window_id LIKE ?", (p3 + "%",))]
        left = conn.execute("SELECT count(*) FROM prer_notes WHERE window_id LIKE ? AND block LIKE ?", (p3 + "%", "%蓝签%")).fetchone()[0]
    assert not any("蓝签 票据" in text for text in after) and left == 0
    assert world.rows("SELECT extraction FROM packets WHERE packet_id = ?", p3)[0][0] == "queued"


def test_review2_withdrawing_the_first_mention_of_a_common_name_leaves_other_windows(world: World) -> None:
    """常见的名字（海港银行）最早那一句在第一个包里：撤回它，名字还有别的出处、没改名，块里只用了后来证据的窗口不重抽。"""
    deliver(world, day_packet("2026-11-01", "我：X3 票据在海港银行。"))
    write(world, notes_extract)
    deliver(world, day_packet("2026-11-03", "我：X1 票据在海港银行。"))
    write(world, notes_extract)
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    with ResultStore(world.data / "l3") as store:
        run_extraction(queued_windows(world), FakeModel(), store, patterns=PATTERNS, options=OPTIONS, prompt=SOL_R,
                       pre_r=prer.PreR(world.settings.library("prod").db_path))
    write(world, notes_extract)
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        (block,) = conn.execute("SELECT block FROM prer_notes WHERE window_id LIKE ? AND block <> ''", (DAY5 + "%",)).fetchone()
    assert block.split("\n")[2:] == ["- X1 票据 托管于 海港银行（生效；上次说到 2026-11-03）"]
    withdraw(world.settings, "prod", packets=[DAY1])
    assert world.rows("SELECT extraction FROM packets WHERE packet_id = ?", DAY5)[0][0] == "done"


OLD_PRER_NOTES = """
CREATE TABLE prer_notes (
    window_id  TEXT NOT NULL,
    window_sha TEXT NOT NULL,
    version    TEXT NOT NULL,
    context    TEXT NOT NULL,
    packets    TEXT NOT NULL,
    sentences  TEXT NOT NULL,
    block      TEXT NOT NULL,
    relations  TEXT NOT NULL,
    sources    TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (window_id, window_sha, version, context)
);
"""


def test_review2_an_older_prer_notes_table_is_upgraded(tmp_path: Path) -> None:
    """db9f431 建的 prer_notes 没有 notes_sha、entities：打开时照新的表结构搬过去，撤回、彻底删除照常能删；
    notes_sha 照库里这个窗口的 B 标注算，已经抽过的窗口键不变。"""
    from test_prea import chat, windows_of
    from test_preb import save_notes

    window = windows_of(chat("2026-11-05 10:00", "我：X1 票据还放在那家。"))[0]
    plain = render_window(window, PATTERNS)
    with ResultStore(tmp_path / "l3") as store:
        save_notes(store, window, {window.targets[0].ref: [["那家", "海港银行"]]})
        notes = store.preb_notes(window.window_id, plain.sha256, PRE_B_VERSION, window_context(window))
    with sqlite3.connect(tmp_path / "l3" / "l3.db") as conn:
        conn.execute("DROP TABLE prer_notes")
        conn.executescript(OLD_PRER_NOTES)
        conn.execute(
            "INSERT INTO prer_notes VALUES (?, ?, 'r1', ?, '[]', '[]', 'the block', '[]', '[[\"P9\", 1, \"P9#1.1.1\"]]', '{}', 't')",
            (window.window_id, plain.sha256, window_context(window)),
        )
    with ResultStore(tmp_path / "l3") as store:
        assert store.prer_notes(window.window_id, plain.sha256, prer.PRE_R_VERSION, window_context(window),
                                prer.notes_digest(notes)) == "the block"
        assert store.forget_prer(packets={"P9"}) == 1


def test_review2_english_names_with_curly_quotes_and_dashes() -> None:
    assert prer._word_fold("Desk–Bot said") == prer._word_fold("desk-bot SAID") == "desk-bot said"
    assert prer._word_fold("O’Neil") == "o'neil"


def test_review2_a_failed_packet_at_the_front_does_not_block_the_queue(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """最早的包有重调也没用的窗口、写账标成了 failed（排队修）：一次一个包时先跳过它、提醒一句，排着的照常往下走；
    只剩 failed 的时候才挑它（修好以后 --retry-failed 重调）。"""
    later = "2026-11-06_2330_desk-bot"
    deliver(world, day_packet("2026-11-05", "我：5 张。"), day_packet("2026-11-06", "我：6 张。"))
    world.execute("UPDATE packets SET extraction = 'failed', extraction_note = 'refusal=1' WHERE packet_id = ?", DAY5)
    monkeypatch.setenv("JAVMEM_L3_PROMPT", SOL_R)
    capsys.readouterr()
    assert main(["extract", "--library", "prod", "--fake", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"这次只抽 {later}" in out and "标成了 failed" in out
    world.execute("UPDATE packets SET extraction = 'done' WHERE packet_id = ?", later)
    assert main(["extract", "--library", "prod", "--fake", "--dry-run"]) == 0
    assert f"这次只抽 {DAY5}" in capsys.readouterr().out
