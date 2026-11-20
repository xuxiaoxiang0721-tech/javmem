"""撤回时要重抽的窗口当场清（J5.6；J5.5 报告第 4 节第 9 条，判断 C 报告第 4 节第 5、8 条）。

撤回一个包以后，别的包里拿它当上文的、往来串一起写的、标注用了从它学来的名字的窗口，结果库里删掉、正文的包改回排队，
下一次 extract、write 照剩下的原文重抽。这些窗口以前写进账本的：隔离行当场删；整包撤回时别的包写的说法也当场删
（写它们的时候模型看得到撤回的那一包），重抽回来；单条撤回时别的说法留着等重抽（证据是别的句子）。

样例都是编的，模型和向量都用假的（javmem extract --fake、javmem write --fake --fake-embeddings）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World, build_packet
from test_preb import FakeLocal, chat_packet, learn_entry, lexicon, save_notes, store, windows_of  # noqa: F401
from test_withdraw import P1, P2, TWO_DAYS, extract_and_write, facts, stocked, store_rows
from test_write import day_packet

from javmem.cli import main
from javmem.ledger import transaction
from javmem.llm import FakeModel
from javmem.pipeline.extract import window_row
from javmem.pipeline.preb import PRE_B_VERSION, PreB
from javmem.pipeline.render import render_window
from javmem.pipeline.store import ResultStore
from javmem.redaction import default_patterns
from javmem.withdraw import withdraw

PATTERNS = default_patterns()
FIRST = day_packet("2026-11-01", "我：越南那批订单 11-15 出货。")
SECOND = build_packet(  # 接着上一包：第一个窗口带着上一包的末尾当上文
    stamp="2026-11-02_2330",
    body="## 原话 · 2026-11-02 09:00–09:01\n- [2026-11-02 09:00] 我：老周 9 点到仓库。\n",
    created_at="2026-11-02T23:30:00+08:00",
    covers_from="2026-11-01T23:30:00+08:00",
    covers_to="2026-11-02T23:30:00+08:00",
    continues=P1,
)
ZHOU_AT_NINE = "老周 9 点到仓库。"


def context_world(world: World) -> tuple[str, list[str]]:
    """两个包写进账本；返回 P2 那个拿 P1 当上文的窗口，和它上文那几句的编号。"""
    stocked(world, FIRST, SECOND)
    windows, _ = store_rows(world)
    row = next(row for row in windows if P1 in row["manifest"] and json.loads(row["manifest"])[-1]["packet_id"] == P2)
    manifest = json.loads(row["manifest"])
    return row["window_id"], [item["sentence_id"] for item in manifest if not str(item["ref"]).startswith("T")]


def add_quarantine(world: World, packet_id: str, window_id: str, payload: dict[str, object]) -> None:
    """隔离表里加一行（像事实复核 G7、判断 C 第二档那样照着原话写的理由），挂在 packet_id 下面。"""
    conn = world.ledger()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO quarantine (packet_id, revision, item_kind, payload, reason, rule_id, rule_version, created_at,"
                " window_id) VALUES (?, 1, 'fact', ?, 'G7:不支持', 'G7', 'test', '2026-11-02T23:40:00+08:00', ?)",
                (packet_id, json.dumps(payload, ensure_ascii=False), window_id),
            )
    finally:
        conn.close()


def quarantined(world: World, window_id: str) -> int:
    return int(world.rows("SELECT count(*) AS n FROM quarantine WHERE window_id = ?", window_id)[0]["n"])


def test_a_whole_packet_withdrawal_clears_the_window_that_used_it_as_context(world: World,
                                                                              capsys: pytest.CaptureFixture[str]) -> None:
    window_id, _ = context_world(world)
    add_quarantine(world, P2, window_id, {"window_id": window_id, "recheck": {"verdict": "不支持", "problem": "照上文那一句写的"}})
    assert ZHOU_AT_NINE in facts(world)
    capsys.readouterr()
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    out = capsys.readouterr().out
    assert "要重抽的窗口：别的包在里面写的说法先删了 1 处" in out and "隔离行删了 1 行" in out
    assert ZHOU_AT_NINE not in facts(world)  # 重抽以前先没了：写它的时候模型看得到撤回的那一包
    assert quarantined(world, window_id) == 0
    assert world.packet_row(P2)["extraction"] == "queued"
    extract_and_write()
    assert set(facts(world)) == {ZHOU_AT_NINE}  # 照剩下的原文重抽、重写回来


def test_withdrawing_the_context_sentence_alone_keeps_the_claims_and_clears_the_quarantine(world: World) -> None:
    """判断 C 报告第 4 节第 5 条：单条撤回上文那一句，复核照它写的 problem 里没有它的编号，按窗口删。别的说法留着等重抽。"""
    window_id, context = context_world(world)
    assert context  # P1 的末尾当了上文
    add_quarantine(world, P2, window_id, {"window_id": window_id, "recheck": {"verdict": "不支持", "problem": "照上文那一句写的"}})
    assert main(["withdraw", "--library", "prod", "--sentence", context[0]]) == 0
    assert ZHOU_AT_NINE in facts(world)  # 单条撤回：证据是别的句子，留着等重抽
    assert quarantined(world, window_id) == 0
    assert world.packet_row(P2)["extraction"] == "queued"
    extract_and_write()
    assert ZHOU_AT_NINE in facts(world)


QUOTED = build_packet(  # 不接着 P1，只是引用了 P1 的原话
    stamp="2026-11-03_2330",
    body=(
        "## 原话 · 2026-11-03 09:00–09:01\n"
        "- [2026-11-03 09:00] 我：这个还算数吗？\n"
        "  > [引用 2026-11-01 10:30 我] 越南那批订单 11-15 出货。\n"
        "- [2026-11-03 09:01] 我：那就照这个安排装 3 个柜。\n"
    ),
    created_at="2026-11-03T23:30:00+08:00",
    covers_from="2026-11-03T00:00:00+08:00",
    covers_to="2026-11-03T23:30:00+08:00",
)
P3 = "2026-11-03_2330_desk-bot"


def test_a_window_quoting_the_withdrawn_packet_counts_as_brought_in_by_it(world: World) -> None:
    """审阅第 3 条：整包撤回 P1，别的包里引用 P1 原话的行跟着标上撤回（cause 是 P1）；那个窗口里没有 P1 这个包，
    可写它的时候模型看得到 P1 的原话，照样算整包带出来的：别的说法先删、重抽回来。"""
    stocked(world, FIRST, QUOTED)
    before = set(facts(world))
    assert "那就照这个安排装 3 个柜。" in before
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    quote = world.rows("SELECT withdrawn_at FROM raw_sentences WHERE packet_id = ? AND text LIKE '%11-15%'", P3)
    assert quote and all(row["withdrawn_at"] for row in quote)  # 引用行跟着标上了
    assert not {text for text in facts(world)} & {"那就照这个安排装 3 个柜。"}  # 重抽以前先没了
    assert world.packet_row(P3)["extraction"] == "queued"
    extract_and_write()
    assert "那就照这个安排装 3 个柜。" in facts(world)  # 照剩下的原文重抽回来
    assert not any("11-15" in text for text in facts(world))


def test_naming_the_quote_line_in_the_same_command_still_counts_as_brought_in(world: World) -> None:
    """复审第 3 条：同一条命令里既整包撤回 P1、又点名 P3 里引用它的那一行（记录的 cause 被点名改成空），照引的话认，
    P3 那个窗口照样算整包带出来的。"""
    stocked(world, FIRST, QUOTED)
    quote = world.rows("SELECT sentence_id FROM raw_sentences WHERE packet_id = ? AND text LIKE '%11-15%'", P3)[0]["sentence_id"]
    assert main(["withdraw", "--library", "prod", "--packet", P1, "--sentence", quote]) == 0
    assert "那就照这个安排装 3 个柜。" not in facts(world)  # 重抽以前先没了
    extract_and_write()
    assert "那就照这个安排装 3 个柜。" in facts(world)


SPLIT_SECOND = build_packet(  # 接着上一包，两句：后面单条撤回其中一句
    stamp="2026-11-02_2330",
    body=("## 原话 · 2026-11-02 09:00–09:01\n- [2026-11-02 09:00] 我：老周 9 点到仓库。\n"
          "- [2026-11-02 09:01] 我：报价 12.5 美元。\n"),
    created_at="2026-11-02T23:30:00+08:00",
    covers_from="2026-11-01T23:30:00+08:00",
    covers_to="2026-11-02T23:30:00+08:00",
    continues=P1,
)


def test_a_context_window_already_gone_from_the_store_is_still_cleared(world: World) -> None:
    """复审 N1：先单条撤回 P2 里的一句（那个窗口从结果库删了，别的说法留着等重抽），重抽以前再整包撤回 P2 拿来当上文的
    P1：结果库里已经没有那个窗口，照账本认（接着 P1 的后一包的第一个窗口），别的说法照样先删。"""
    stocked(world, FIRST, SPLIT_SECOND)
    assert main(["withdraw", "--library", "prod", "--sentence", f"{P2}#1.2.1"]) == 0  # 报价 12.5 美元。
    assert ZHOU_AT_NINE in facts(world)  # 单条撤回：留着等重抽
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    assert ZHOU_AT_NINE not in facts(world)
    extract_and_write()
    assert set(facts(world)) == {ZHOU_AT_NINE}


def test_quarantine_rows_quoting_the_withdrawn_packet_elsewhere_go_too(world: World) -> None:
    """判断 C 报告第 4 节第 8 条的兜底：结果库里没有那个窗口，隔离行挂在别的包下面、payload 里记着撤回那包的句子编号。"""
    stocked(world, *TWO_DAYS)
    elsewhere = f"{P2}#w9"
    add_quarantine(world, P2, elsewhere, {"judge": {"lines": [f"{P1}#1.1.1"]}, "reason": "照发过去的那一句写的"})
    add_quarantine(world, P2, elsewhere, {"judge": {"lines": [f"{P2}#1.1.1"]}, "reason": "只照自己这一包"})
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    rows = world.rows("SELECT payload FROM quarantine WHERE window_id = ?", elsewhere)
    assert [json.loads(row["payload"])["judge"]["lines"] for row in rows] == [[f"{P2}#1.1.1"]]


def test_an_interrupted_withdrawal_picks_up_the_window_cleanup(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """账本那边清完了、结果库还没删就断了：不写目标再跑一次，结果库里还有那个窗口，照样认得出、接着清。"""
    window_id, _ = context_world(world)
    real = ResultStore.delete_windows

    def broken(self: ResultStore, doomed: object) -> int:
        raise RuntimeError("断了")

    monkeypatch.setattr(ResultStore, "delete_windows", broken)
    with pytest.raises(RuntimeError):
        withdraw(world.settings, "prod", packets=[P1])
    monkeypatch.setattr(ResultStore, "delete_windows", real)
    assert ZHOU_AT_NINE not in facts(world) and world.packet_row(P2)["extraction"] == "queued"
    assert any(row["window_id"] == window_id for row in store_rows(world)[0])
    assert main(["withdraw", "--library", "prod"]) == 0
    assert all(P1 not in row["manifest"] for row in store_rows(world)[0])
    extract_and_write()
    assert set(facts(world)) == {ZHOU_AT_NINE}


def test_whole_marks_the_windows_a_packet_withdrawal_brings_in(tmp_path: Path, store: ResultStore) -> None:  # noqa: F811
    """doomed_windows：预处理 B 标注用了从撤回的包学来的名字的窗口，整包撤回时算“整包带出来的”；只撤回那一句时不算。"""
    source, user, other = windows_of(chat_packet("2026-11-06_2330", "韦明下午到。"), chat_packet("2026-11-07_2330", "他说定价了。"),
                                     chat_packet("2026-11-08_2330", "罗某某也到了。"))
    lex = lexicon(tmp_path)
    PreB(FakeLocal(learns=[learn_entry(source.targets[0].ref, "韦明", "韦明", "人")]), lex).run_packet([source], store, PATTERNS)
    weiming = next(row for row in store.preb_items(PRE_B_VERSION) if row["name"] == "韦明")
    noted = save_notes(store, user, {user.targets[0].ref: [["他", "韦明"]]}, [int(weiming["item_id"])])
    model = FakeModel()
    for window in (source, other):
        store.save_window(window_row(window, render_window(window, PATTERNS), "k-" + window.window_id, model.cache_name))
    store.save_window(window_row(user, noted, "k-" + user.window_id, model.cache_name))
    whole = {one.window_id: one for one in store.doomed_windows(packets={source.targets[0].packet_id})}
    assert set(whole) == {source.window_id, user.window_id}
    assert whole[user.window_id].whole and whole[user.window_id].bodies == {user.targets[0].packet_id}
    single = {one.window_id: one for one in store.doomed_windows(sentences={source.targets[0].sentence_id})}
    assert set(single) == {source.window_id, user.window_id} and not any(one.whole for one in single.values())
    assert store.doomed(sentences={source.targets[0].sentence_id})[0] == [(one.window_id, one.cache_key) for one in single.values()]
