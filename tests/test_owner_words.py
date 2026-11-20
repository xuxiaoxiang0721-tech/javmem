"""D204：外部内容后来说的，不替代主人原话说的（主人 11-19 选择卡；做法放行以后 D207 定了四件）。

同一组里，主人自己说过、现在还现行的那个值，后来外部内容（邮件、对话里的 [外部:…]、[转发 …]、附件说明，J2.13 的转述）
说了别的值：不替代它。外部说的那条待确认（借冻结冲突那一套：frozen_conflict，claims 是 {"frozen": 原话那条, "new": 外部那条}），
主人定了才换。主人自己后来说的、Bot 和同事在对话里自己说的，照旧替代；外部对外部照旧；冻结的照冻结。

例子都是编的（青柚那批瓷砖哪天到港），抽取用手写的 L3 结果，向量用假的。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from helpers import World, as_json, build_packet, mail_header_block
from spotter import SpotModel, extract_into_store
from test_chat_answers import code_of_open, said
from test_merging import LEXICON_TEXT as MERGE_LEXICON
from test_merging import TABLE as MERGE_TABLE
from test_merging import VALUES as MERGE_VALUES
from test_merging import proposal_of
from test_withdraw_review import rebuilt_world
from test_write import day_packet, l3_fact, write

from javmem.cli import main
from javmem.decisions import export_decisions, import_decisions
from javmem.pipeline.merge import GroupFact, succession
from javmem.pipeline.window import Window
from javmem.query.pending import ASK
from javmem.query.tools import entity_card
from javmem.review import EXPIRE_DAYS, decide, freeze, remind

MAIL_RAW = "## 原话" + " · 邮件"  # 邮件包里一封信那一节的标题（packet-spec），拼出来用
DAYS = ("12-03", "12-09", "12-11", "12-15")
TILES = {"key": "e1", "mention": "青柚那批瓷砖", "name": "青柚那批瓷砖", "type": "订单或合同"}

OWNER = day_packet("2026-11-01", "我：青柚那批瓷砖 12-03 到港。")
PASTED = day_packet("2026-11-05", "desk-bot：[外部:邮件] 货代来信，青柚那批瓷砖改到 12-09 到港。")
ASK_LINE = "desk-bot：青柚那批瓷砖你说过哪天到港，货代邮件里写的是另一天。按邮件改吗？（待确认 {code}）"


def tile_extract(window: Window) -> dict[str, object]:
    """一句里写着哪个日子，就抽一条“青柚那批瓷砖到港日期是 ……”。Bot 问待确认的那一句不抽。"""
    found = []
    for line in window.targets:
        if "待确认" in line.text:
            continue
        for day in DAYS:
            if day in line.text:
                found.append(l3_fact(window, line.text.rstrip("。"), f"青柚那批瓷砖到港日期是 {day}",
                                     subject="e1", attribute="到港日期", value=day))
    return {"entities": [TILES], "facts": found}


def mail_packet(day: str, text: str) -> str:
    """货代发来的一封邮件（整个邮件包都是外部内容），可见范围和对话包一样。"""
    return build_packet(
        "mail-sales-a",
        f"{day}_1800",
        body=f"{MAIL_RAW} · {day} 09:00\n\n"
        + mail_header_block(message_id='"<tile-0001@example.org>"', thread_id='"<tile-0001@example.org>"',
                            date=f"{day}T09:00:00+08:00", subject='"到港通知"')
        + f"\n  {text}\n",
        framework="mail-import",
        channel="email",
        mailbox="li.ming@example.com",
        created_at=f"{day}T18:00:00+08:00",
        covers_from=f"{day}T00:00:00+08:00",
        covers_to=f"{day}T18:00:00+08:00",
    )


def run(world: World, *packets: str) -> None:
    for packet in packets:
        world.drop("inbox/mail-sales-a" if "channel: email" in packet else "inbox/desk-bot", packet)
    world.intake()
    write(world, tile_extract).close()


def state(world: World) -> dict[str, tuple[object, ...]]:
    """每个日子：(状态, 待确认的理由, 被哪个日子替代, 是不是转述)。"""
    rows = world.rows("SELECT * FROM facts WHERE withdrawn_at IS NULL")
    value = {row["fact_id"]: row["value"] for row in rows}
    return {row["value"]: (row["status"], row["pending_reason"], value.get(row["superseded_by"]), row["secondhand"]) for row in rows}


def fact_id(world: World, day: str) -> str:
    (row,) = world.rows("SELECT fact_id FROM facts WHERE value = ?", day)
    return str(row["fact_id"])


def open_pairs(world: World) -> list[tuple[str, str]]:
    """没处理的冲突，两边写成日子：(原值, 新说法)。"""
    rows = world.rows("SELECT claims FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL ORDER BY review_id")
    value = {row["fact_id"]: row["value"] for row in world.rows("SELECT fact_id, value FROM facts")}
    return [(value[json.loads(row["claims"])["frozen"]], value[json.loads(row["claims"])["new"]]) for row in rows]


def decided(world: World) -> list[tuple[object, object]]:
    return [(row["decision"], row["decided_by"]) for row in world.rows(
        "SELECT decision, decided_by FROM review_queue WHERE decided_at IS NOT NULL ORDER BY review_id")]


KEPT = {"12-03": ("verified", None, None, 0), "12-09": ("pending", "frozen_conflict", None, 1)}  # 原话现行，外部的待确认


# ---------- 外部说的不替代主人原话 ----------


def test_pasted_external_words_do_not_replace_what_the_owner_said(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    """复现：主人 11-01 说 12-03 到港，11-05 Bot 贴的邮件说改到 12-09。原话现行；12-09 待确认，进队列一对。"""
    run(world, OWNER, PASTED)
    assert state(world) == KEPT
    assert open_pairs(world) == [("12-03", "12-09")]
    (row,) = world.rows("SELECT claims, quotes FROM review_queue")
    assert json.loads(row["claims"]) == {"frozen": fact_id(world, "12-03"), "new": fact_id(world, "12-09")}
    assert json.loads(row["quotes"]) == [r["sentence_id"] for r in world.rows("SELECT sentence_id FROM evidence WHERE fact_id = ?",
                                                                               fact_id(world, "12-09"))]
    capsys.readouterr()
    assert main(["review", "--library", "prod"]) == 0
    out = capsys.readouterr().out
    assert "和主人原话的冲突（外部内容说的）" in out and "主人原话：" in out and "外部说的：" in out
    assert "和冻结的冲突" not in out.split("待确认 1 条")[0]  # 这一条不写成冻结的（最后那行汇总照旧按种类数）


@pytest.mark.parametrize(
    "later",
    [
        mail_packet("2026-11-05", "青柚那批瓷砖改到 12-09 到港。"),  # 整个邮件包
        day_packet("2026-11-05", "我：[外部:网页] 港口公告，青柚那批瓷砖 12-09 到港。"),  # 主人自己贴的外部内容
        day_packet("2026-11-05", "我：[转发 自 苗小禾] 青柚那批瓷砖 12-09 到港。"),  # 主人转发别人的话
    ],
    ids=["mail", "owner-pasted", "relayed"],
)
def test_every_kind_of_secondhand_words_waits_for_the_owner(world: World, later: str) -> None:
    run(world, OWNER, later)
    assert state(world) == KEPT
    assert open_pairs(world) == [("12-03", "12-09")]


def test_the_owner_saying_it_at_the_same_moment_is_not_external(world: World) -> None:
    """同一刻主人自己也说了 12-09：这一刻不全是外部内容，照旧替代。"""
    same = day_packet("2026-11-05", "desk-bot：[外部:邮件] 货代来信，青柚那批瓷砖改到 12-09 到港。",
                      "我：知道了，青柚那批瓷砖 12-09 到港。").replace("[10:31]", "[10:30]")  # 两句同一分钟
    run(world, OWNER, same)
    assert state(world) == {"12-03": ("verified", None, "12-09", 0), "12-09": ("verified", None, None, 0)}
    assert open_pairs(world) == []


def test_the_owner_changing_it_later_replaces_as_before_and_drops_the_question(world: World) -> None:
    run(world, OWNER, PASTED)
    assert open_pairs(world) == [("12-03", "12-09")]
    run(world, day_packet("2026-11-07", "我：青柚那批瓷砖定了 12-11 到港。"))
    assert state(world) == {
        "12-03": ("verified", None, "12-11", 0),  # 外部那一刻不算，主人自己 11-07 换掉的
        "12-09": ("verified", None, "12-11", 1),
        "12-11": ("verified", None, None, 0),
    }
    assert open_pairs(world) == [] and decided(world) == []  # 不再冲突的、没处理的那一对删掉


@pytest.mark.parametrize(
    ("packets", "expected"),
    [
        # 外部对外部：照旧
        ((day_packet("2026-11-01", "desk-bot：[外部:邮件] 货代来信，青柚那批瓷砖 12-03 到港。"), PASTED),
         {"12-03": ("verified", None, "12-09", 1), "12-09": ("verified", None, None, 1)}),
        # 外部在前、主人在后：照旧
        ((day_packet("2026-11-01", "desk-bot：[外部:邮件] 货代来信，青柚那批瓷砖 12-09 到港。"),
          day_packet("2026-11-05", "我：青柚那批瓷砖 12-03 到港。")),
         {"12-09": ("verified", None, "12-03", 1), "12-03": ("verified", None, None, 0)}),
        # Bot 自己说的、同事在对话里自己说的：不算外部，照旧
        ((OWNER, day_packet("2026-11-05", "desk-bot：查过了，青柚那批瓷砖 12-09 到港。")),
         {"12-03": ("verified", None, "12-09", 0), "12-09": ("verified", None, None, 0)}),
        ((OWNER, day_packet("2026-11-05", "苗小禾：青柚那批瓷砖 12-09 到港。")),
         {"12-03": ("verified", None, "12-09", 0), "12-09": ("verified", None, None, 0)}),
    ],
    ids=["external-then-external", "external-then-owner", "bot-itself", "colleague-itself"],
)
def test_what_stays_the_same(world: World, packets: tuple[str, ...], expected: dict[str, tuple[object, ...]]) -> None:
    run(world, *packets)
    assert state(world) == expected
    assert open_pairs(world) == []


def test_external_words_bringing_back_the_old_value_wait_for_the_owner(world: World) -> None:
    """主人 12-03 改成 12-11 以后，外部又说回 12-03：12-11 现行，外部说的 12-03 待确认（和 12-11 冲突）。"""
    run(world, OWNER, day_packet("2026-11-03", "我：青柚那批瓷砖改成 12-11 到港。"),
        day_packet("2026-11-05", "desk-bot：[外部:邮件] 货代来信，青柚那批瓷砖还是 12-03 到港。"))
    assert state(world) == {"12-03": ("pending", "frozen_conflict", None, 0), "12-11": ("verified", None, None, 0)}
    assert open_pairs(world) == [("12-11", "12-03")]


def test_an_old_value_the_owner_replaced_is_not_owner_words_when_external_brings_it_back(world: World) -> None:
    """主人 12-03 改成 12-11 以后，邮件说回 12-03、又说 12-15：12-03 是主人自己换掉的，只是外部又说回来，不再当主人原话护着
    （它和 12-15 不成一对，照常被 12-15 替代）；主人定了 12-11 照 12-15 改，答的就是 12-15（审阅第 1 条）。"""
    run(world, OWNER, day_packet("2026-11-03", "我：青柚那批瓷砖改成 12-11 到港。"),
        day_packet("2026-11-05", "desk-bot：[外部:邮件] 货代来信，青柚那批瓷砖还是 12-03 到港。"),
        day_packet("2026-11-07", "desk-bot：[外部:邮件] 货代又来信，青柚那批瓷砖改到 12-15 到港。"))
    assert sorted(open_pairs(world)) == [("12-11", "12-03"), ("12-11", "12-15")]
    assert state(world) == {
        "12-03": ("pending", "frozen_conflict", "12-15", 0),
        "12-11": ("verified", None, None, 0),
        "12-15": ("pending", "frozen_conflict", None, 1),
    }
    (row,) = [one for one in world.rows("SELECT review_id, claims FROM review_queue")
              if json.loads(one["claims"])["new"] == fact_id(world, "12-15")]
    decide(world.settings, "prod", int(row["review_id"]), "new")
    assert state(world) == {
        "12-03": ("verified", None, "12-15", 0),
        "12-11": ("verified", None, "12-15", 0),
        "12-15": ("verified", None, None, 1),
    }
    assert open_pairs(world) == []


def test_two_external_values_both_wait(world: World) -> None:
    run(world, OWNER, PASTED, day_packet("2026-11-07", "desk-bot：[外部:邮件] 货代又来信，青柚那批瓷砖改到 12-15 到港。"))
    assert state(world) == {
        "12-03": ("verified", None, None, 0),
        "12-09": ("pending", "frozen_conflict", "12-15", 1),  # 外部对外部照旧被替代，也和原话冲突
        "12-15": ("pending", "frozen_conflict", None, 1),
    }
    assert open_pairs(world) == [("12-03", "12-09"), ("12-03", "12-15")]


def test_writing_the_old_packet_late_gives_the_same(world: World) -> None:
    run(world, PASTED)  # 外部那一包先写
    assert state(world)["12-09"] == ("verified", None, None, 1)
    run(world, OWNER)  # 主人 11-01 那一包晚到
    assert state(world) == KEPT
    assert open_pairs(world) == [("12-03", "12-09")]


# ---------- 冻结 ----------


def test_freezing_the_external_one_lets_it_replace_and_unfreezing_asks_again(world: World) -> None:
    run(world, OWNER, PASTED)
    freeze(world.settings, "prod", [fact_id(world, "12-09")])  # 主人担保了外部说的
    assert state(world) == {"12-03": ("verified", None, "12-09", 0), "12-09": ("verified", None, None, 1)}
    assert open_pairs(world) == [] and decided(world) == [("keep_both", "owner")]  # 冻结时关掉的
    freeze(world.settings, "prod", [fact_id(world, "12-09")], frozen=False)
    assert state(world) == KEPT and open_pairs(world) == [("12-03", "12-09")]


def test_freezing_the_owners_one_keeps_the_same_question(world: World) -> None:
    run(world, OWNER, PASTED)
    (before,) = world.rows("SELECT review_id FROM review_queue")
    freeze(world.settings, "prod", [fact_id(world, "12-03")])
    assert state(world) == KEPT
    assert [row["review_id"] for row in world.rows("SELECT review_id FROM review_queue")] == [before["review_id"]]


# ---------- 主人定了 ----------


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("frozen", {"12-03": ("verified", None, None, 0), "12-09": ("verified", None, "12-03", 1)}),  # 留原话
        ("new", {"12-03": ("verified", None, "12-09", 0), "12-09": ("verified", None, None, 1)}),  # 照外部换
        ("both", {"12-03": ("verified", None, None, 0), "12-09": ("verified", None, None, 1)}),  # 两边都留
    ],
)
def test_deciding_follows_the_owner(world: World, choice: str, expected: dict[str, tuple[object, ...]]) -> None:
    run(world, OWNER, PASTED)
    (row,) = world.rows("SELECT review_id FROM review_queue")
    decide(world.settings, "prod", int(row["review_id"]), choice)
    assert state(world) == expected
    assert [r["frozen"] for r in world.rows("SELECT frozen FROM facts")] == [0, 0]  # 谁都没被冻结，也不去解除冻结
    assert len(decided(world)) == 1 and open_pairs(world) == []
    if choice == "new":
        (old,) = world.rows("SELECT superseded_at FROM facts WHERE value = '12-03'")
        assert old["superseded_at"].startswith("2026-11-05")  # 从外部说的那一刻起被替代
    # 再写一遍同样的矛盾：不再问，照定的
    run(world, day_packet("2026-11-07", "desk-bot：[外部:邮件] 货代又来信，青柚那批瓷砖是 12-09 到港。"))
    assert state(world) == expected and open_pairs(world) == []


def test_switching_to_one_external_value_does_not_touch_freezing(world: World) -> None:
    """两个外部的值都在问：先定 12-09 留原话，再定 12-15 照外部换。原话那条本来就没冻结，不去“解除冻结”（那会给定过的
    那一对补一行 unfrozen、重新问）。"""
    run(world, OWNER, PASTED, day_packet("2026-11-07", "desk-bot：[外部:邮件] 货代又来信，青柚那批瓷砖改到 12-15 到港。"))
    first, second = (int(row["review_id"]) for row in world.rows("SELECT review_id FROM review_queue ORDER BY review_id"))
    decide(world.settings, "prod", first, "frozen")
    decide(world.settings, "prod", second, "new")
    assert decided(world) == [("keep_frozen", "owner"), ("keep_new", "owner")] and open_pairs(world) == []
    assert state(world) == {
        "12-03": ("verified", None, "12-15", 0),
        "12-09": ("verified", None, "12-15", 1),
        "12-15": ("verified", None, None, 1),
    }


@pytest.mark.parametrize("choice", ["frozen", "new", "both"])
def test_the_owners_decision_comes_back_after_a_rebuild(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        choice: str) -> None:
    """整本重建：导出主人定的，新账本写完（这一对又进了队列）再导回：照定的，不再问。"""
    run(world, OWNER, PASTED)
    (row,) = world.rows("SELECT review_id FROM review_queue")
    decide(world.settings, "prod", int(row["review_id"]), choice)
    before = state(world)
    data = export_decisions(world.settings, "prod")
    fresh = rebuilt_world(tmp_path, monkeypatch)
    run(fresh, OWNER, PASTED)
    assert open_pairs(fresh) == [("12-03", "12-09")]
    import_decisions(fresh.settings, "prod", data)
    assert state(fresh) == before and open_pairs(fresh) == []
    assert [decision for decision, _ in decided(fresh)] == [{"frozen": "keep_frozen", "new": "keep_new", "both": "keep_both"}[choice]]


def test_a_chat_answer_is_not_doubled_when_decisions_come_back_after_a_rebuild(world: World, tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """整本重建时这一对重新进队列，包里主人在对话里的回答又认了一遍；导回时那一行删掉，照导进来的，同一对只一行（审阅第 4 条）。"""
    run(world, OWNER, PASTED)
    answer = day_packet("2026-11-06", ASK_LINE.format(code=code_of_open(world)), "我：不用改，留原来的。")
    run(world, answer)
    data = export_decisions(world.settings, "prod")
    fresh = rebuilt_world(tmp_path, monkeypatch)
    run(fresh, OWNER, PASTED, answer)
    assert decided(fresh) == [("keep_frozen", "owner")]
    import_decisions(fresh.settings, "prod", data)
    assert decided(fresh) == [("keep_frozen", "owner")]
    assert state(fresh) == {"12-03": ("verified", None, None, 0), "12-09": ("verified", None, "12-03", 1)}


@pytest.mark.parametrize("choice", ["frozen", "both"])
def test_a_frozen_owners_decision_comes_back_after_a_rebuild(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             choice: str) -> None:
    """原话那条冻结着、主人定过的：整本重建时它还没冻上，按主人原话这一对又进了队列；导回冻上、插回定过的以后，那一条要删掉
    （复审第 1 条）。"""
    run(world, OWNER, PASTED)
    freeze(world.settings, "prod", [fact_id(world, "12-03")])
    (row,) = world.rows("SELECT review_id FROM review_queue WHERE decided_at IS NULL")
    decide(world.settings, "prod", int(row["review_id"]), choice)
    before = state(world), decided(world)
    data = export_decisions(world.settings, "prod")
    fresh = rebuilt_world(tmp_path, monkeypatch)
    run(fresh, OWNER, PASTED)
    import_decisions(fresh.settings, "prod", data)
    assert (state(fresh), decided(fresh)) == before and open_pairs(fresh) == []


def test_every_decided_row_comes_back_after_a_rebuild(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """旧账本里同一对有好几行定过的（对话里答了留原话；冻结又解除，补了一行 unfrozen；又问一遍，答了两边都留）：导回以后一行
    不少。以前每导一行，都把这次先导进来的、对话里定的那几行当成重建时认的删掉（复审第 2 条）。"""
    run(world, OWNER, PASTED)
    first = day_packet("2026-11-06", ASK_LINE.format(code=code_of_open(world)), "我：不用改，留原来的。")
    run(world, first)
    freeze(world.settings, "prod", [fact_id(world, "12-03")])
    freeze(world.settings, "prod", [fact_id(world, "12-03")], frozen=False)
    second = day_packet("2026-11-08", ASK_LINE.format(code=code_of_open(world)), "我：两个都留着。")
    run(world, second)
    assert decided(world) == [("keep_frozen", "owner"), ("unfrozen", "owner"), ("keep_both", "owner")]
    before = state(world), decided(world)
    data = export_decisions(world.settings, "prod")
    fresh = rebuilt_world(tmp_path, monkeypatch)
    run(fresh, OWNER, PASTED, first, second)
    import_decisions(fresh.settings, "prod", data)
    assert (state(fresh), decided(fresh)) == before and open_pairs(fresh) == []


MERGE_LINES = (("2026-09-01", "我：柏廷舟负责资金池。"), ("2026-09-03", "desk-bot：[外部:邮件] 货代来信，柏廷舟改成负责对账。"),
               ("2026-09-06", "我：柏庭舟周五请假。"))


def merge_world(world: World) -> None:
    """柏廷舟（写错的）负责资金池，邮件说改成对账；柏庭舟（标准的写法）另有一句。还没配别名表，两个写法各一个实体。"""
    for day, line in MERGE_LINES:
        world.drop("inbox/desk-bot", day_packet(day, line, domain="[investment]"))
    world.intake()
    extract_into_store(world, SpotModel(MERGE_TABLE, MERGE_VALUES))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0


def merge_spellings(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配上别名表，把两个写法并成一个（J3.6）：事实换了编号，冻结标记、待确认跟着改指过去。"""
    path = tmp_path / "config" / "lexicon.yaml"
    path.parent.mkdir()
    path.write_text(MERGE_LEXICON, encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    assert main(["entities", "--library", "prod", "--merge", proposal_of(world, path, "柏庭舟"), "--note", "测试"]) == 0


@pytest.mark.parametrize(("frozen", "choice"), [(False, "new"), (False, "frozen"), (True, "frozen"), (True, "both")])
def test_the_owners_decision_holds_after_two_spellings_are_merged(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                   frozen: bool, choice: str) -> None:
    """合并实体：主人定过的照定的，不再多一条没处理的（审阅第 2 条）。原话那条冻结着的也一样：重写窗口时新编号那条还没冻上，
    按主人原话又开了一条，冻结标记、决定搬过去以后那一条要删掉，不然 30 天没人管，自动两边都留会盖掉主人定的（复审第 1 条）。"""
    merge_world(world)
    if frozen:
        freeze(world.settings, "prod", [world.rows("SELECT fact_id FROM facts WHERE value = '资金池'")[0]["fact_id"]])
    (row,) = world.rows("SELECT review_id FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL")
    decide(world.settings, "prod", int(row["review_id"]), choice)
    before = state(world), decided(world)
    merge_spellings(world, tmp_path, monkeypatch)
    assert (state(world), decided(world)) == before and open_pairs(world) == []


def test_nobody_deciding_for_30_days_keeps_both(world: World) -> None:
    run(world, OWNER, PASTED)
    (row,) = world.rows("SELECT created_at FROM review_queue")
    late = world.at(datetime.fromisoformat(str(row["created_at"])) + timedelta(days=EXPIRE_DAYS, minutes=1))
    assert len(remind(late.settings, "prod", force=True).expired) == 1
    assert decided(world) == [("keep_both", "auto")]
    assert state(world) == {"12-03": ("verified", None, None, 0), "12-09": ("verified", None, None, 1)}


@pytest.mark.parametrize("frozen", [False, True])
def test_two_open_rows_for_one_pair_expire_without_breaking_the_daily_step(world: World, frozen: bool) -> None:
    """同一对有两条没处理的（合并实体时两对旧冲突改指成同一对会留下这样的，这里用 SQL 造），都过了 30 天：关第一条时重算把
    第二条删了，第二条跳过，每天关过期的那一步不整步报错（终查第 1 条）。"""
    run(world, OWNER, PASTED)
    if frozen:
        freeze(world.settings, "prod", [fact_id(world, "12-03")])
    world.execute("INSERT INTO review_queue (kind, claims, quotes, created_at) SELECT kind, claims, quotes, created_at FROM review_queue")
    (row,) = world.rows("SELECT min(created_at) AS at FROM review_queue")
    late = world.at(datetime.fromisoformat(str(row["at"])) + timedelta(days=EXPIRE_DAYS, minutes=1))
    assert len(remind(late.settings, "prod", force=True).expired) == 1
    assert decided(world) == [("keep_both", "auto")] and open_pairs(world) == []
    assert state(world) == {"12-03": ("verified", None, None, 0), "12-09": ("verified", None, None, 1)}


def test_answering_in_chat_switches_and_withdrawing_the_answer_asks_again(world: World) -> None:
    run(world, OWNER, PASTED)
    code = code_of_open(world)
    run(world, day_packet("2026-11-06", ASK_LINE.format(code=code), "我：按新的改。"))
    assert decided(world) == [("keep_new", "owner")]
    (row,) = world.rows("SELECT answer FROM review_queue")
    assert "unfroze" not in json.loads(row["answer"])  # 本来就没冻结，撤回时不去冻上
    assert state(world) == {"12-03": ("verified", None, "12-09", 0), "12-09": ("verified", None, None, 1)}
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "按新的改。")]) == 0
    assert decided(world) == [] and open_pairs(world) == [("12-03", "12-09")]
    assert state(world) == KEPT and [r["frozen"] for r in world.rows("SELECT frozen FROM facts")] == [0, 0]


@pytest.mark.parametrize("words", ["不用改，留原来的。", "两个都留着。"])
def test_withdrawing_an_answer_after_unfreezing_does_not_open_the_question_twice(world: World, words: str) -> None:
    """原话那条冻结着时对话里答了，主人后来解除了冻结（照主人原话又问了一遍），再撤回那句回答：只去掉回答的记录，旧的那一行
    不再打开。不然同一对两条没处理的、编号一样，对话里答、--decide 编号都认不出是哪一条（审阅第 3 条）。"""
    run(world, OWNER, PASTED)
    freeze(world.settings, "prod", [fact_id(world, "12-03")])
    code = code_of_open(world)
    run(world, day_packet("2026-11-06", ASK_LINE.format(code=code), f"我：{words}"))
    freeze(world.settings, "prod", [fact_id(world, "12-03")], frozen=False)
    assert open_pairs(world) == [("12-03", "12-09")]
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, words)]) == 0
    assert open_pairs(world) == [("12-03", "12-09")] and state(world) == KEPT
    decide(world.settings, "prod", code_of_open(world), "new")
    assert state(world) == {"12-03": ("verified", None, "12-09", 0), "12-09": ("verified", None, None, 1)}
    assert open_pairs(world) == []


def test_withdrawing_a_keep_the_owners_answer_asks_again(world: World) -> None:
    """对话里答了留原话，又撤回了那一句回答：原话那条没冻结，可冲突还在，重新问（不是只去掉回答的记录）。"""
    run(world, OWNER, PASTED)
    code = code_of_open(world)
    run(world, day_packet("2026-11-06", ASK_LINE.format(code=code), "我：不用改，留原来的。"))
    assert decided(world) == [("keep_frozen", "owner")]
    assert state(world)["12-09"] == ("verified", None, "12-03", 1)
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "不用改，留原来的。")]) == 0
    assert decided(world) == [] and open_pairs(world) == [("12-03", "12-09")]
    assert state(world) == KEPT


# ---------- 查询接口的问法 ----------


def test_entity_card_asks_in_the_owners_words_and_frozen_ones_keep_theirs(world: World) -> None:
    from javmem.query.pending import OWNER_WORDS_ASK

    run(world, OWNER, PASTED)
    caller = replace(world.settings, caller="desk-bot")
    (entry,) = as_json(entity_card({"entity": "青柚那批瓷砖"}, settings=caller))["pending"]
    assert (entry["kind"], entry["ask"]) == ("frozen_conflict", OWNER_WORDS_ASK)
    assert [(item["side"], item["fact"]["value"]) for item in entry["items"]] == [("frozen", "12-03"), ("new", "12-09")]
    freeze(world.settings, "prod", [fact_id(world, "12-03")])  # 冻结了：照冻结的问法，一字不改
    (entry,) = as_json(entity_card({"entity": "青柚那批瓷砖"}, settings=caller))["pending"]
    assert entry["ask"] == ASK["frozen_conflict"] == "记着的原值（冻结了）和新说的对不上：要不要照新说的改？"


# ---------- 纯函数：merge.succession ----------


def _said(fact_id: str, *moments: int, frozen: bool = False, owner: tuple[int, ...] = (),
          external: tuple[int, ...] = ()) -> GroupFact:
    return GroupFact(fact_id, tuple((moment, f"t{moment}") for moment in moments), frozen, owner=frozenset(owner),
                     external=frozenset(external))


def test_succession_with_owner_words() -> None:
    a = _said("FA", 100, owner=(100,))
    # 主人说 → 外部说：原话现行，外部的和它冲突
    result = succession([a, _said("FB", 200, external=(200,))])
    assert (result.superseded, result.conflicts) == ({}, [("FA", "FB")])
    # 主人说 → 外部说 → 主人又说：照旧替代，前面那一对不算了
    result = succession([a, _said("FB", 200, external=(200,)), _said("FC", 300, owner=(300,))])
    assert (result.superseded, result.conflicts) == ({"FA": ("FC", "t300"), "FB": ("FC", "t300")}, [])
    # 主人说 → 外部说 → 外部又说：两对都冲突，外部对外部照旧替代
    result = succession([a, _said("FB", 200, external=(200,)), _said("FC", 300, external=(300,))])
    assert (result.superseded, result.conflicts) == ({"FB": ("FC", "t300")}, [("FA", "FB"), ("FA", "FC")])
    # 外部在前、主人在后；外部对外部；Bot 自己说的（不算外部）：照旧
    assert succession([_said("FB", 100, external=(100,)), _said("FA", 200, owner=(200,))]).superseded == {"FB": ("FA", "t200")}
    assert succession([_said("FB", 100, external=(100,)), _said("FC", 200, external=(200,))]).superseded == {"FB": ("FC", "t200")}
    assert succession([a, _said("FB", 200)]).superseded == {"FA": ("FB", "t200")}
    # 主人改了以后外部又说回旧值：新的现行，外部说回来的和它冲突
    result = succession([_said("FA", 100, 300, owner=(100,), external=(300,)), _said("FC", 200, owner=(200,))])
    assert (result.superseded, result.conflicts) == ({}, [("FC", "FA")])
    # 外部说回来的那个旧值不再当主人原话护着：再来一个外部的，它照常被替代，只和主人后来说的成对（审阅第 1 条）
    result = succession([_said("FA", 100, 300, owner=(100,), external=(300,)), _said("FC", 200, owner=(200,)),
                         _said("FD", 400, external=(400,))])
    assert (result.superseded, result.conflicts) == ({"FA": ("FD", "t400")}, [("FC", "FA"), ("FC", "FD")])
    # 主人定过照 FB 换，后来外部又说回 FA：FA 也不再护着
    result = succession([_said("FA", 100, 300, owner=(100,), external=(300,)), _said("FB", 200, external=(200,)),
                         _said("FD", 400, external=(400,))], switched={("FA", "FB")})
    assert (result.superseded, result.conflicts) == ({"FA": ("FD", "t400"), "FB": ("FA", "t300")}, [])
    # 冻结的原话照冻结；冻结的外部那条是主人担保的，照常替代
    assert succession([_said("FA", 100, owner=(100,), frozen=True), _said("FB", 200, external=(200,))]).conflicts == [("FA", "FB")]
    result = succession([a, _said("FB", 200, frozen=True, external=(200,))])
    assert (result.superseded, result.conflicts) == ({"FA": ("FB", "t200")}, [])
    # 主人定过“照外部换”的那一对：照常替代
    result = succession([a, _said("FB", 200, external=(200,))], switched={("FA", "FB")})
    assert (result.superseded, result.conflicts) == ({"FA": ("FB", "t200")}, [])
    # 同一刻外部的和不是外部的都说了：不是外部的那条替代
    result = succession([a, _said("FB", 200, external=(200,)), _said("FC", 200)])
    assert (result.superseded, result.conflicts) == ({"FA": ("FC", "t200")}, [])
    # 不是主人原话的：外部说的照旧替代（附件说明、转发都一样，按每一刻有没有说话人自己的话算）
    assert succession([_said("FA", 100), _said("FB", 200, external=(200,))]).superseded == {"FA": ("FB", "t200")}
