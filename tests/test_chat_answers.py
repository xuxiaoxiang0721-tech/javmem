"""J4.6 第 2 段：D83 认出主人在对话里答的是哪一条待确认（主人 11-17 选择卡第 5 题 A）。例子都是编的，L3 的结果写死在这里。

Bot 问的那句话里写着这一条的编号“（待确认 Q……）”，问法是“要不要照新说的改”。问完以后主人的第一条消息（同一包或者
接着的下一包、30 分钟以内、中间 Bot 没问别的待确认），或者主人引用了那句问话回的（隔多久都算），就是回答。照
javmem.pipeline.answers 的死规矩认：认得出就照答案定（谁定的是主人，记着问话、回答是哪一句），认不出不猜。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World
from test_corrections import (
    ASKED, BEIYU, CHENG, GAO_RULES, JOB_RULES, K7, K7_RULES, KAN, MI, TEA, current, new_job, one, rel_rows, reviews, rules, sid,
    together,
)
from test_relations_frozen import frozen_bank
from test_relations_newold import moved_extract
from test_relations_soft import bank_moves, paused_later
from test_relations_write import bank_extract, resent
from test_relcheck import BANK, BROKER, NOTE, OWNER
from test_withdraw_review import rebuilt_world
from test_write import day_packet, deliver, l3_fact, write

from javmem.cli import main
from javmem.decisions import SCHEMA, export_decisions, import_decisions
from javmem.ledger import open_ledger
from javmem.pipeline import answers
from javmem.review import ReviewError, freeze, open_items, remind

FROZEN_RULES = rules(
    ("交给宓春生负责", MI, "负责", TEA, {}),
    ("是阚星野负责", KAN, "负责", TEA, {}),
    ("还是宓春生", MI, "负责", TEA, {}),
    ("他俩一起", MI, "负责", TEA, {}),
    ("他俩一起", KAN, "负责", TEA, {}),
)
ASK = "desk-bot：锡兰那批茶具订单记着是宓春生负责（你冻结过），前天你说是阚星野。按阚星野改吗？（待确认 {code}）"
TWO_NOTES = rules(  # K7、X1 两张票据都在投资分区里
    ("K7 票据放在澄江银行", K7, "托管于", CHENG, {}), ("转到北屿证券", K7, "托管于", BEIYU, {}),
    ("X1 票据在海港银行。", NOTE, "托管于", BANK, {}), ("转到东岸证券", NOTE, "托管于", BROKER, {}),
    ("海港银行那边先停一下", NOTE, "托管于", BANK, {"state": "暂停"}),
)


def code_of_open(world: World) -> str:
    conn = open_ledger(world.settings.library("prod"))
    try:
        (item,) = open_items(conn, world.settings)
    finally:
        conn.close()
    assert item.code.startswith("Q") and len(item.code) == 7
    return item.code


def said(world: World, words: str) -> str:
    """原文里就是这一句（问话按“待确认”认）的那一句的编号。"""
    if words == "待确认":
        (row,) = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%待确认%' AND quoted = 0")
    else:
        (row,) = world.rows("SELECT sentence_id FROM raw_sentences WHERE text = ? AND quoted = 0", words)
    return str(row["sentence_id"])


def frozen_conflict(world: World) -> str:
    """宓春生负责锡兰那批（冻结着），11-12 主人纠正成阚星野：进和冻结的冲突。返回这一条的编号。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"))
    write(world, FROZEN_RULES).close()
    freeze(world.settings, "prod", [one(world, "宓春生", "负责", "锡兰那批茶具订单")["relation_id"]])
    deliver(world, day_packet("2026-11-12", "我：不对，锡兰那批茶具订单是阚星野负责的。"))
    write(world, FROZEN_RULES).close()
    return code_of_open(world)


def ask(world: World, code: str, *lines: str, day: str = "2026-11-13") -> None:
    deliver(world, day_packet(day, ASK.format(code=code), *lines))
    write(world, FROZEN_RULES).close()


MI_TEA = ("宓春生", "负责", "锡兰那批茶具订单")
KAN_TEA = ("阚星野", "负责", "锡兰那批茶具订单")


# ---------- 编号 ----------


def test_the_code_is_stored_listed_and_made_from_the_content(world: World) -> None:
    code = frozen_conflict(world)
    (review,) = reviews(world)
    assert review["code"] == code == answers.code_of("frozen_conflict", review["claims"])


def test_decide_accepts_the_code(world: World) -> None:
    code = frozen_conflict(world)
    assert main(["review", "--library", "prod", "--decide", code.lower(), "--keep", "new"]) == 0
    (review,) = reviews(world)
    assert (review["decision"], review["answer"]) == ("keep_new", None)
    assert main(["review", "--library", "prod", "--decide", "Q000000", "--keep", "new"]) != 0  # 没有这一条


# ---------- 认得出的 ----------


def test_answering_with_the_new_name_keeps_the_new_one(world: World) -> None:
    code = frozen_conflict(world)
    ask(world, code, "我：是阚星野。")
    (review,) = reviews(world)
    assert (review["decision"], review["decided_by"]) == ("keep_new", "owner")
    answer = json.loads(review["answer"])
    assert (answer["question"], answer["answer"], answer["decision"]) == (said(world, "待确认"), said(world, "是阚星野。"), "keep_new")
    old, new = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert current(new) and old["frozen"] == 0 and (old["state"], old["valid_to"]) == ("ended", None)
    assert old["correction_sentence"] == sid("2026-11-12", 1)
    (audit,) = world.rows("SELECT * FROM audit_log WHERE check_kind = 'chat_answer'")
    assert (audit["tier"], audit["trusted"], audit["basis"], audit["item_id"]) == ("owner", "keep_new", said(world, "是阚星野。"), code)


def test_keep_the_old_one(world: World) -> None:
    code = frozen_conflict(world)
    ask(world, code, "我：不用，留原来的。")
    (review,) = reviews(world)
    assert review["decision"] == "keep_frozen"
    assert current(one(world, *MI_TEA)) and not current(one(world, *KAN_TEA))


def test_both_are_right(world: World) -> None:
    code = frozen_conflict(world)
    ask(world, code, "我：两个都对，他俩一起管。")
    assert [(row["kind"], row["decision"]) for row in reviews(world)] == [("frozen_conflict", "keep_both")]
    assert current(one(world, *MI_TEA)) and current(one(world, *KAN_TEA))


def test_an_answer_at_the_start_of_the_next_packet_counts(world: World) -> None:
    code = frozen_conflict(world)
    asked = day_packet("2026-11-13", ASK.format(code=code), hour=23)  # 小节标题里的起止时间只给人看，不用跟着改
    answered = day_packet("2026-11-14", "我：改吧。", hour=0)
    deliver(world, asked.replace("[23:30]", "[23:50]"), answered.replace("[00:30]", "[00:05]"))  # 隔了 15 分钟、跨了包
    write(world, FROZEN_RULES).close()
    (review,) = reviews(world)
    assert review["decision"] == "keep_new"


def test_a_quoted_reply_counts_even_days_later(world: World) -> None:
    code = frozen_conflict(world)
    question = ASK.format(code=code).split("：", 1)[1]
    ask(world, code, "鞠望川：这事我不清楚。")
    reply = day_packet("2026-11-15", "我：是阚星野。", hour=9)
    deliver(world, reply.replace("我：是阚星野。\n", "我：是阚星野。\n  > [引用 2026-11-13 10:30 desk-bot] " + question + "\n"))
    write(world, FROZEN_RULES).close()
    (review,) = reviews(world)
    assert review["decision"] == "keep_new"
    assert json.loads(review["answer"])["answer"] == "2026-11-15_2330_desk-bot#1.1.1"


def test_a_soft_change_answered_in_chat_follows_the_verdict_until_the_answer_is_withdrawn(world: World) -> None:
    """软变化（J4.4 第 4 段）在对话里答“先停”：照 B 办（谁定的是主人）；撤回回答，照定之前的样子放回去。"""
    paused_later(world)  # 11-03“X1 票据在海港银行那边先停一下”：挡住、进待确认
    before = reviews(world)[0]["claims"]
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-04", f"desk-bot：海港银行那边的票据是要先停吗？照判的改吗？（待确认 {code}）",
                              "我：先停，等报价出来。"))
    write(world, bank_moves).close()
    (review,) = reviews(world)
    assert (review["decision"], review["decided_by"], review["claims"]["verdict"]) == ("pause", "owner", "B")
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "先停，等报价出来。")]) == 0
    (review,) = reviews(world)
    assert (review["decision"], review["answer"], review["claims"]) == (None, None, before)
    rows = world.rows("SELECT state, status, pending_reason FROM relations ORDER BY as_of_epoch")
    assert [(row["state"], row["status"], row["pending_reason"]) for row in rows] == [
        ("active", "verified", None), ("paused", "pending", "soft_change")]


def test_a_finance_correction_answered_yes_is_applied_and_not_held_again(world: World) -> None:
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
            day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。", domain="[investment]"))
    write(world, K7_RULES).close()
    code = code_of_open(world)
    question = f"desk-bot：你说 K7 票据早就转到北屿证券了，要按这个改吗？（待确认 {code}）"
    # 回答那一句自己又是一处纠正（“早就”）：照主人定的算，不再挡一遍
    deliver(world, day_packet("2026-11-12", question, "我：对，改吧，早就转到北屿证券了。", domain="[investment]"))
    write(world, K7_RULES).close()
    assert [(row["claims"]["reason"], row["decision"]) for row in reviews(world)] == [("correction", "keep_new")]
    assert current(one(world, "K7 票据", "托管于", "北屿证券"))
    assert not current(one(world, "K7 票据", "托管于", "澄江银行"))


M6 = {"key": "e43", "mention": "M6 票据", "name": "M6 票据", "type": "订单或合同"}
M6_SPECS = (
    (("M6 票据我还拿着",), OWNER, "持有", M6, {}),
    (("不对，M6 票据上个月",), OWNER, "持有", M6, {"state": "已结束", "valid_to": "2026-10"}),
    (("从持仓里去掉吗", "对，改吧"), OWNER, "持有", M6, {"state": "已结束", "valid_to": "2026-10"}),
)


def test_the_answer_saying_the_ended_one_again_does_not_start_another_segment(world: World) -> None:
    """真调测试题 K25：财务投资的纠正，主人在对话里答“对，改吧”；Bot 的问话和主人的回答 L3 又抽成一处“已结束、到
    2026-10”（没写开始日子）：归已经结束的那一段，不另起一段——同一条关系只一行，记着被 P2 那句纠正。"""
    deliver(world, day_packet("2026-10-03", "我：M6 票据我还拿着。", domain="[investment]"),
            day_packet("2026-11-10", "我：不对，M6 票据上个月就到期兑付了。", domain="[investment]"))
    write(world, together(*M6_SPECS)).close()
    code = code_of_open(world)
    question = f"desk-bot：你说 M6 票据上个月就到期兑付了，按这个从持仓里去掉吗？（待确认 {code}）"
    deliver(world, day_packet("2026-11-11", question, "我：对，改吧。", domain="[investment]"))
    write(world, together(*M6_SPECS)).close()
    assert [(row["claims"]["reason"], row["decision"]) for row in reviews(world)] == [("correction", "keep_new")]
    (row,) = rel_rows(world, "主人", "持有", "M6 票据")
    assert (row["state"], row["valid_to"], row["correction_sentence"]) == ("ended", "2026-10", sid("2026-11-10", 1))


# ---------- 认不出、不算的 ----------


def test_later_does_not_decide_and_is_counted(world: World) -> None:
    code = frozen_conflict(world)
    ask(world, code, "我：这个回头再说。")
    (review,) = reviews(world)
    assert review["decided_at"] is None and json.loads(review["answer"])["decision"] is None
    assert "答了没认出来的 1 条" in remind(world.settings, "prod", force=True).line()


def test_a_wrong_code_does_not_decide(world: World) -> None:
    frozen_conflict(world)
    ask(world, "Q000000", "我：改吧。")
    (review,) = reviews(world)
    assert (review["decided_at"], review["answer"]) == (None, None)


def test_a_colleague_answering_does_not_decide(world: World) -> None:
    code = frozen_conflict(world)
    ask(world, code, "鞠望川：改吧，我知道这事。")
    (review,) = reviews(world)
    assert (review["decided_at"], review["answer"]) == (None, None)


def test_another_question_in_between_takes_the_answer(world: World) -> None:
    code = frozen_conflict(world)
    ask(world, code, "desk-bot：另外，那批的包装要按新的改吗？（待确认 Q000000）", "我：改吧。")
    (review,) = reviews(world)
    assert (review["decided_at"], review["answer"]) == (None, None)


def test_an_answer_more_than_30_minutes_later_does_not_count(world: World) -> None:
    code = frozen_conflict(world)
    late = day_packet("2026-11-13", ASK.format(code=code), "我：改吧。")
    deliver(world, late.replace("[10:31]", "[11:05]"))  # 隔了 35 分钟
    write(world, FROZEN_RULES).close()
    (review,) = reviews(world)
    assert (review["decided_at"], review["answer"]) == (None, None)


def test_a_question_in_another_scope_does_not_decide(world: World) -> None:
    """问话那一包的可见范围看不到这一条（分区没有交集）：不算回答。"""
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
            day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。", domain="[investment]"))
    write(world, K7_RULES).close()
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-12", f"desk-bot：K7 票据要改吗？（待确认 {code}）", "我：对，改吧。"))
    write(world, K7_RULES).close()
    (review,) = reviews(world)
    assert (review["decided_at"], review["answer"]) == (None, None)


# ---------- 第 2 段审阅（例子照审阅的复现脚本改的，都是编的） ----------


def test_a_negated_name_is_not_taken(world: World) -> None:
    """第 1 条：“不是阚星野。”不是用新值：认不出，冻结的照旧。"""
    code = frozen_conflict(world)
    ask(world, code, "我：不是阚星野。")
    (review,) = reviews(world)
    assert review["decided_at"] is None and json.loads(review["answer"])["decision"] is None
    assert one(world, *MI_TEA)["frozen"] == 1


def withdrawn_frozen_bank(world: World) -> str:
    """X1 票据在海港银行（冻结着），那一包撤回了：证据都撤回了的冻结关系，进待确认。返回关系编号。"""
    frozen = frozen_bank(world)
    assert main(["withdraw", "--library", "prod", "--packet", "2026-11-01_2330_desk-bot"]) == 0
    return frozen


@pytest.mark.parametrize("answer", ["我：别删掉。", "我：删掉吧。"])
def test_a_delete_is_never_taken_from_chat(world: World, answer: str) -> None:
    """第 1 条：“别删掉”不是删掉；删掉的回不来，对话里答“删掉”也不收（记一笔，要主人用 --decide 定）。"""
    frozen = withdrawn_frozen_bank(world)
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-05", f"desk-bot：那条原话撤回了的冻结记录，要删掉吗？（待确认 {code}）", answer))
    write(world, bank_extract).close()
    (review,) = reviews(world)
    assert review["decided_at"] is None and json.loads(review["answer"])["decision"] is None
    assert world.rows("SELECT 1 FROM relations WHERE relation_id = ?", frozen)


def finance_two(world: World) -> str:
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", "我：X1 票据在海港银行。", domain="[investment]"),
            day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。", domain="[investment]"))
    write(world, TWO_NOTES).close()
    return code_of_open(world)


def test_an_unrelated_finance_correction_in_the_answer_is_still_held(world: World) -> None:
    """第 2 条：答 K7 的那条消息里顺口纠正了 X1 的托管：X1 那一处照样先挡住（不是这一条待确认那一组）。"""
    code = finance_two(world)
    deliver(world, day_packet("2026-11-12", f"desk-bot：K7 票据按北屿证券改吗？（待确认 {code}）",
                              "我：对，改吧。X1 票据也早就转到东岸证券了。", domain="[investment]"))
    write(world, TWO_NOTES).close()
    assert current(one(world, "X1 票据", "托管于", "海港银行"))
    assert [(row["claims"].get("reason"), row["decision"]) for row in reviews(world)] == [
        ("correction", "keep_new"), ("correction", None)]


def test_an_unrelated_soft_change_in_the_answer_is_still_held(world: World) -> None:
    code = finance_two(world)
    deliver(world, day_packet("2026-11-12", f"desk-bot：K7 票据按北屿证券改吗？（待确认 {code}）",
                              "我：对，改吧。X1 票据在海港银行那边先停一下。", domain="[investment]"))
    write(world, TWO_NOTES).close()
    assert any(row["kind"] == "soft_change" and row["decided_at"] is None for row in reviews(world))


def test_the_answer_does_not_open_a_new_unmatched_review(world: World) -> None:
    """第 3 条：答“抽取没抽出来的纠正”那一条的话里又是一句否定，不再多问一条。"""
    deliver(world, day_packet("2026-10-20", "我：PO-7731 那单让郜立人跟。"), day_packet("2026-11-13", *ASKED))
    write(world, GAO_RULES).close()
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-14", f"desk-bot：你是说郜立人不负责 PO-7731 了吗？要按这个改吗？（待确认 {code}）",
                              "我：对，改吧，郜立人根本没负责过这个单子。"))
    write(world, GAO_RULES).close()
    assert [row["decision"] for row in reviews(world)] == ["keep_new"]


def test_withdrawing_an_answer_that_a_resend_moved_reverts(world: World) -> None:
    """第 4 条：包重发，回答那一句挪了位置：照指纹认出还是那一句，撤回它照样撤掉决定。"""
    code = frozen_conflict(world)
    ask(world, code, "我：是阚星野。")
    deliver(world, resent("2026-11-13", ASK.format(code=code), "desk-bot：我先查一下。", "我：是阚星野。"))
    write(world, FROZEN_RULES).close()
    assert reviews(world)[0]["decision"] == "keep_new"  # 还是那一问、那一答
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "是阚星野。")]) == 0
    (review,) = reviews(world)
    assert review["decided_at"] is None and one(world, *MI_TEA)["frozen"] == 1


def test_a_resend_without_the_question_and_answer_reopens_it_right_away(world: World) -> None:
    """第 4 条：包重发，问话、回答都没了：重写那一包时就照新版本撤掉决定，不等以后撤回别的话。"""
    code = frozen_conflict(world)
    ask(world, code, "我：是阚星野。")
    deliver(world, resent("2026-11-13", "desk-bot：今天没别的事。"))
    write(world, FROZEN_RULES).close()
    (review,) = reviews(world)
    assert review["decided_at"] is None and one(world, *MI_TEA)["frozen"] == 1
    deliver(world, day_packet("2026-11-20", "我：今天吃了面。"))
    write(world, FROZEN_RULES).close()
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "今天吃了面。")]) == 0
    assert reviews(world)[0]["decided_at"] is None


def test_withdrawing_the_question_text_reverts(world: World) -> None:
    """第 5 条：问话那条消息里任何一句撤回了（“按阚星野改吗？”），这一问就撤回了。"""
    code = frozen_conflict(world)
    ask(world, code, "我：是阚星野。")
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "按阚星野改吗？")]) == 0
    assert reviews(world)[0]["decided_at"] is None


def test_a_soft_pause_keeps_the_condition_from_the_answer(world: World) -> None:
    """第 7 条：软变化答“先停，等报价出来”：照 B 办，等什么照话里记。"""
    paused_later(world)
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-04", f"desk-bot：海港银行那边的票据是要先停吗？照判的改吗？（待确认 {code}）",
                              "我：先停，等报价出来。"))
    write(world, bank_moves).close()
    (review,) = reviews(world)
    assert review["decision"] == "pause" and review["claims"]["action"]["condition"] == "报价出来"


@pytest.mark.parametrize("answer", ["我：先停，12 月 1 日起再恢复。", "我：月底取消吧。", "我：改成交给别人吧。"])
def test_a_soft_answer_with_a_day_or_a_name_it_cannot_place_is_not_taken(world: World, answer: str) -> None:
    """第 7 条：哪天、交给谁认不出（或者对不上）的，不收，记一笔。"""
    paused_later(world)
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-04", f"desk-bot：海港银行那边的票据是要先停吗？照判的改吗？（待确认 {code}）", answer))
    write(world, bank_moves).close()
    (review,) = reviews(world)
    assert review["decided_at"] is None and json.loads(review["answer"])["decision"] is None


def test_a_soft_replace_naming_another_place_is_not_taken(world: World) -> None:
    """复审第 N2 条：候选是“X1 票据可能转到东岸证券”，主人答“换成 X1 票据放到北屿证券”：说的是另一家，不收。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据可能转到东岸证券。"))
    write(world, bank_moves).close()
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-04", f"desk-bot：那张票据要转走吗？照判的改吗？（待确认 {code}）", "我：换成 X1 票据放到北屿证券。"))
    write(world, bank_moves).close()
    (review,) = reviews(world)
    assert review["decided_at"] is None


def test_a_january_day_said_in_december_is_next_year(world: World) -> None:
    """复审第 N3 条：12 月说的“1 月 5 日”是下一年的。"""
    deliver(world, day_packet("2026-12-01", "我：X1 票据在海港银行。"), day_packet("2026-12-20", "我：X1 票据在海港银行那边先停一下。"))
    write(world, bank_moves).close()
    code = code_of_open(world)
    deliver(world, day_packet("2026-12-21", f"desk-bot：海港银行那边的票据是要先停吗？照判的改吗？（待确认 {code}）",
                              "我：取消吧，1 月 5 日起不做了。"))
    write(world, bank_moves).close()
    (review,) = reviews(world)
    assert review["decided_at"] is None or str(review["claims"]["action"]["at"]).startswith("2027-01-05")


@pytest.mark.parametrize("answer", ["我：取消吧，十二月一号起就不做了。", "我：取消吧，元旦以后不做了。"])
def test_a_day_it_cannot_read_is_not_taken(world: World, answer: str) -> None:
    """复审第 N3 条：话里像是说了哪天、可认不出来（中文数字、节日），不收（做法 3.4“认不出的不收”）。"""
    paused_later(world)
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-04", f"desk-bot：海港银行那边的票据是要先停吗？照判的改吗？（待确认 {code}）", answer))
    write(world, bank_moves).close()
    (review,) = reviews(world)
    assert review["decided_at"] is None


@pytest.mark.parametrize(("answer", "decision"), [("我：还在，两边都在做。", "keep_hidden"), ("我：改吧，那边早走了。", "keep_new")])
def test_the_old_job_question_answered_in_chat(world: World, answer: str, decision: str) -> None:
    """只说了新的那一家时问的“旧的那一条还算不算”（correction_other），在对话里答了照答案改（D83）。"""
    new_job(world)
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-12", f"desk-bot：祁望舒甘棠物流那边那条要不要结束？（待确认 {code}）", answer))
    write(world, JOB_RULES).close()
    (review,) = reviews(world)
    assert (review["decision"], review["decided_by"]) == (decision, "owner")
    assert current(one(world, "祁望舒", "任职于", "甘棠物流")) == (decision == "keep_hidden")


def test_reverting_after_the_owner_unfroze_leaves_no_stale_conflict(world: World) -> None:
    """第 8 条：对话里定了留原值，主人后来自己解除了冻结，再撤回回答：冲突已经没了，不再打开。"""
    code = frozen_conflict(world)
    ask(world, code, "我：不用，留原来的。")
    assert main(["freeze", "--library", "prod", "--relation", one(world, *MI_TEA)["relation_id"], "--undo"]) == 0
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "不用，留原来的。")]) == 0
    assert not any(row["kind"] == "frozen_conflict" and row["decided_at"] is None for row in reviews(world))


def test_a_quoted_reply_records_the_bots_own_question(world: World) -> None:
    """第 9 条：引用了问话回的：记下的问话是 Bot 原来那一句（不是回答那一包里的引用行）。"""
    code = frozen_conflict(world)
    question = ASK.format(code=code).split("：", 1)[1]
    deliver(world, day_packet("2026-11-13", ASK.format(code=code)))
    write(world, FROZEN_RULES).close()
    reply = day_packet("2026-11-15", "我：是阚星野。", hour=9)
    deliver(world, reply.replace("我：是阚星野。\n", "我：是阚星野。\n  > [引用 2026-11-13 10:30 desk-bot] " + question + "\n"))
    write(world, FROZEN_RULES).close()
    (review,) = reviews(world)
    assert review["decision"] == "keep_new" and json.loads(review["answer"])["question"].startswith("2026-11-13")


def test_quoting_a_colleague_who_pasted_the_code_does_not_count(world: World) -> None:
    code = frozen_conflict(world)
    deliver(world, day_packet("2026-11-13", f"鞠望川：（待确认 {code}）这条我看了，应该改。"))
    write(world, FROZEN_RULES).close()
    reply = day_packet("2026-11-15", "我：对。", hour=9)
    deliver(world, reply.replace("我：对。\n", f"我：对。\n  > [引用 2026-11-13 10:30 鞠望川] （待确认 {code}）这条我看了，应该改。\n"))
    write(world, FROZEN_RULES).close()
    assert reviews(world)[0]["decided_at"] is None


def test_codes_are_stored_when_a_review_is_made_outside_writing(world: World) -> None:
    """第 12 条：冻结、撤回这些命令新进的待确认，也当时就记下编号。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：锡兰那批茶具订单是阚星野负责。"))
    write(world, FROZEN_RULES).close()
    freeze(world.settings, "prod", [one(world, *MI_TEA)["relation_id"]])  # 冻结被替代了的旧的：替代它的那条进待确认
    assert [row["code"] is not None for row in reviews(world)] == [True]


# ---------- 撤回、整本重建 ----------


def test_withdrawing_the_answer_reopens_the_question(world: World) -> None:
    code = frozen_conflict(world)
    ask(world, code, "我：是阚星野。")
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "是阚星野。")]) == 0
    (review,) = reviews(world)
    assert (review["decided_at"], review["decision"], review["answer"]) == (None, None, None)
    old = one(world, *MI_TEA)
    assert old["frozen"] == 1 and current(old) and not current(one(world, *KAN_TEA))


def test_withdrawing_the_question_reopens_it_too(world: World) -> None:
    code = frozen_conflict(world)
    ask(world, code, "我：不用，留原来的。")
    assert main(["withdraw", "--library", "prod", "--sentence", said(world, "待确认")]) == 0
    (review,) = reviews(world)
    assert (review["decided_at"], review["answer"]) == (None, None)
    assert one(world, *KAN_TEA)["status"] == "pending"


def test_a_chat_answer_comes_back_after_a_rebuild(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """财务投资的纠正在对话里答了：整本重建时照包里的回答再认一遍（编号不变）；导回不拒收、不重复。"""
    packets = [day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
               day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。", domain="[investment]")]
    deliver(world, *packets)
    write(world, K7_RULES).close()
    code = code_of_open(world)
    packets.append(day_packet("2026-11-12", f"desk-bot：K7 票据按北屿证券改吗？（待确认 {code}）", "我：对，改吧。",
                              domain="[investment]"))
    deliver(world, packets[-1])
    write(world, K7_RULES).close()

    def seen(one_world: World) -> list[tuple[object, ...]]:
        return [(row["kind"], row["code"], row["decision"], row["decided_by"], row["answer"]) for row in reviews(one_world)]

    before = seen(world)
    assert before[0][1:4] == (code, "keep_new", "owner")
    data = export_decisions(world.settings, "prod")
    assert data["reviews"][0]["code"] == code and data["reviews"][0]["answer"]["decision"] == "keep_new"
    new = rebuilt_world(tmp_path, monkeypatch)
    deliver(new, *packets)
    write(new, K7_RULES).close()
    assert seen(new) == before  # 重建时又认了一遍
    import_decisions(new.settings, "prod", data)  # 不当成“新账本里已经有定过的决定”拒收
    assert seen(new) == before  # 也不重复
    assert current(one(new, "K7 票据", "托管于", "北屿证券"))


def same_time_rows(one_world: World) -> list[tuple[object, ...]]:
    return [(row["decision"], row["decided_by"], row["code"]) for row in one_world.rows(
        "SELECT * FROM review_queue WHERE kind = 'other' AND json_extract(claims, '$.reason') = 'same_time' ORDER BY review_id")]


def test_a_same_time_answer_is_not_duplicated_on_import(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第 2 段审阅第 6 条：同一句话里两个（R12）在对话里答了“两个都对”：重建又认了一遍，导回不再多一行。"""
    packets = [day_packet("2026-11-01", "我：X1 票据在海港银行，也可能在东岸证券。")]
    deliver(world, *packets)
    write(world, moved_extract).close()
    code = code_of_open(world)
    packets.append(day_packet("2026-11-02", f"desk-bot：那张票据两家都记着，要都留吗？（待确认 {code}）", "我：两个都对。"))
    deliver(world, packets[-1])
    write(world, moved_extract).close()
    before = same_time_rows(world)
    assert [row[:2] for row in before] == [("keep_both", "owner")]
    data = export_decisions(world.settings, "prod")
    new = rebuilt_world(tmp_path, monkeypatch)
    deliver(new, *packets)
    write(new, moved_extract).close()
    import_decisions(new.settings, "prod", data)
    assert same_time_rows(new) == before


def test_import_refuses_a_ledger_where_the_owner_decided_after_an_unclear_answer(world: World) -> None:
    """第 2 段审阅第 11 条：账本里主人自己用 --decide 定过的（那一条记着一笔没认出来的回答），照样算“已经有定过的决定”，
    导回不收。"""
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
            day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。", domain="[investment]"))
    write(world, K7_RULES).close()
    code = code_of_open(world)
    deliver(world, day_packet("2026-11-12", f"desk-bot：K7 票据按北屿证券改吗？（待确认 {code}）", "我：这个回头再说。",
                              domain="[investment]"))
    write(world, K7_RULES).close()
    assert main(["review", "--library", "prod", "--decide", code, "--keep", "new"]) == 0
    with pytest.raises(ReviewError):
        import_decisions(world.settings, "prod", {"schema": SCHEMA, "library": "prod", "facts": {}, "reviews": []})


def zhou_facts(window):  # type: ignore[no-untyped-def]
    """老周：写着哪个名字就是哪个人（周建国、周立新，只写老周的是老周）。"""
    for line in window.targets:
        text = line.text
        if not any(name in text for name in ("老周", "周建国", "周立新")):
            continue
        name = "周建国" if "周建国" in text else "周立新" if "周立新" in text else "老周"
        entity = {"key": "e1", "mention": "老周", "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}
    return {"entities": [], "facts": []}


def test_an_ambiguity_answer_is_not_duplicated_on_import(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第 2 段审阅第 6 条：实体有歧义在对话里答了“不是同一个人”：重建又认了一遍，导回不再多一行。"""
    packets = [day_packet("2026-11-01", "我：周建国就是老周，管越南。"), day_packet("2026-11-02", "我：周立新也叫老周，管泰国。"),
               day_packet("2026-11-03", "我：老周明天回来。")]
    deliver(world, *packets)
    write(world, zhou_facts).close()
    code = code_of_open(world)
    packets.append(day_packet("2026-11-04", f"desk-bot：明天回来的那位，和前两位是一个人吗？（待确认 {code}）", "我：不是同一个人。"))
    deliver(world, packets[-1])
    write(world, zhou_facts).close()

    def rows(one_world: World) -> list[tuple[object, ...]]:
        return [(row["decision"], row["decided_by"]) for row in one_world.rows(
            "SELECT * FROM review_queue WHERE kind = 'ambiguous_entity' ORDER BY review_id")]

    assert rows(world) == [("keep_both", "owner")]
    data = export_decisions(world.settings, "prod")
    new = rebuilt_world(tmp_path, monkeypatch)
    deliver(new, *packets)
    write(new, zhou_facts).close()
    import_decisions(new.settings, "prod", data)
    assert rows(new) == [("keep_both", "owner")]
