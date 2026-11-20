"""D83（J4.6 第 2 段）：编号、问话里的编号、主人的回答照死规矩认。只测纯函数，例子都是编的。"""

from __future__ import annotations

import pytest

from javmem.pipeline import answers
from javmem.pipeline.answers import DROP, KEEP_BOTH, KEEP_FROZEN, KEEP_HIDDEN, KEEP_NEW, code_of, find_code, interpret


def test_the_code_follows_the_content_not_the_row() -> None:
    claims = {"frozen": "R1111111111111111", "new": "R2222222222222222"}
    code = code_of("frozen_conflict", claims)
    assert code.startswith("Q") and len(code) == 7 and code[1:] == code[1:].upper()
    assert code_of("frozen_conflict", dict(reversed(list(claims.items())))) == code  # 先后不算
    assert code_of("frozen_conflict", {**claims, "new": "R3333333333333333"}) != code
    assert code_of("same_time", {"reason": "same_time", "relations": ["R1", "R2"]}) != code


def test_a_fingerprint_wins_over_the_sentence_position() -> None:
    held = {"item": "relation", "reason": "correction", "key": "k", "packet": "p", "fingerprint": "f", "sentence": "p#1.2.1"}
    assert code_of("correction", held) == code_of("correction", {**held, "sentence": "p#1.5.1", "relation": "R9", "claim": "c"})


def test_the_judge_reason_is_not_in_the_code() -> None:
    """第 2 段审阅第 13 条：判断 C 拿不准的原因重建时可能变（llm_failed → llm_unsure），编号不跟着变。"""
    held = {"key": "k", "packet": "p", "fingerprint": "f", "sentence": "p#1.2.1"}
    assert code_of("manual_review", {**held, "reason": "C:llm_failed"}) == code_of("manual_review", {**held, "reason": "C:llm_unsure"})


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("按老周改吗？（待确认 Q3F9A1C）", "Q3F9A1C"),
        ("按老周改吗？(待确认:q3f9a1c)", "Q3F9A1C"),
        ("按老周改吗？（待确认：Q3F9A1C）", "Q3F9A1C"),
        ("按老周改吗？", None),
        ("（待确认 Q3F9A1C）还有（待确认 Q000001）", None),  # 写了两个：看不出问的是哪一条
        ("（待确认 Q3F9A1）", None),
    ],
)
def test_finding_the_code_in_a_question(text: str, code: str | None) -> None:
    assert find_code(text) == code


@pytest.mark.parametrize(
    ("text", "decision"),
    [
        ("是冉北辰。", KEEP_NEW),
        ("对，改吧。", KEEP_NEW),
        ("改吧", KEEP_NEW),
        ("用新的", KEEP_NEW),
        ("不用，留原来的。", KEEP_FROZEN),
        ("还是苗一帆。", KEEP_FROZEN),
        ("不用", KEEP_FROZEN),
        ("两个都对，他俩一起排。", KEEP_BOTH),
        ("这个回头再说。", None),
        ("不知道", None),
        ("嗯？", None),
        ("苗一帆和冉北辰我再想想", None),
        ("别改吧", KEEP_FROZEN),  # “改吧”前面有“别”：不算改，“别改”是留原来的
        ("今天天气不错", None),
        ("是冉北辰，不是苗一帆。", KEEP_NEW),  # “不是”后面的名字不算
    ],
)
def test_answers_to_a_frozen_conflict(text: str, decision: str | None) -> None:
    assert interpret("frozen_conflict", text, new_names=["冉北辰"], old_names=["苗一帆"]) == decision


# 第 2 段审阅第 1 条：否定、条件句、顺口提到的名字不能认成确定的决定。(种类, 回答, 能接受的)：认不出 None，或者意思对的那个
MISREADS = [
    ("frozen_conflict", "不是阚星野。", {None, KEEP_FROZEN}),
    ("frozen_conflict", "不，阚星野不对。", {None, KEEP_FROZEN}),
    ("frozen_conflict", "对，改吧，宓春生早就调走了。", {None, KEEP_NEW}),
    ("frozen_conflict", "两个都不对。", {None}),
    ("frozen_conflict", "不要用新的。", {None, KEEP_FROZEN}),
    ("frozen_conflict", "改吧，都是阚星野在管。", {None, KEEP_NEW}),
    ("frozen_conflict", "别按你说的改。", {None, KEEP_FROZEN}),
    ("frozen_conflict", "要是不对就改。", {None}),
    ("frozen_conflict", "阚星野早就不管了。", {None, KEEP_FROZEN}),
    ("correction", "别照你说的改。", {None, KEEP_HIDDEN}),
    ("correction", "不按新的。", {None, KEEP_HIDDEN}),
    ("manual_review", "确实没有。", {None, DROP}),
    ("evidence_withdrawn", "别删掉。", {None, KEEP_FROZEN}),
    ("evidence_withdrawn", "不用删了。", {None, KEEP_FROZEN, KEEP_HIDDEN}),
    ("evidence_withdrawn", "不用留着。", {None, DROP}),
    ("soft_change", "不取消。", {None, "A"}),
    ("soft_change", "不继续了。", {None, "C"}),
    ("soft_change", "不用暂停。", {None, "A"}),
    ("soft_change", "不要交给阚星野。", {None}),
    ("ambiguous_entity", "不是两个人，是同一个。", {None}),
    ("same_time", "两个都不对。", {None}),
    # 复审第 N1 条：否定跟在词后面、双重否定、“一起”“都要”“都是”、否定在 4 个字以外
    ("frozen_conflict", "原来那个不对。", {None, KEEP_NEW}),
    ("frozen_conflict", "新的那个不对。", {None, KEEP_FROZEN}),
    ("frozen_conflict", "按你说的不行。", {None, KEEP_FROZEN}),
    ("frozen_conflict", "用新的不好。", {None, KEEP_FROZEN}),
    ("frozen_conflict", "还是原来的不行。", {None, KEEP_NEW}),
    ("frozen_conflict", "照旧不行。", {None, KEEP_NEW}),
    ("correction", "新的那个不对。", {None, KEEP_HIDDEN}),
    ("correction", "改过来不对。", {None, KEEP_HIDDEN}),
    ("soft_change", "继续不了。", {None, "C"}),
    ("soft_change", "取消不了。", {None, "A"}),
    ("frozen_conflict", "不改不行。", {None, KEEP_NEW}),
    ("correction", "不改不行。", {None, KEEP_NEW}),
    ("same_time", "都是海港银行的。", {None}),
    ("frozen_conflict", "一起改了吧。", {None, KEEP_NEW}),
    ("frozen_conflict", "都要改。", {None, KEEP_NEW}),
    ("frozen_conflict", "不用这么快就改。", {None}),
]


@pytest.mark.parametrize(("kind", "text", "ok"), MISREADS, ids=[f"{kind}:{text}" for kind, text, _ in MISREADS])
def test_opposite_answers_are_not_misread(kind: str, text: str, ok: set) -> None:
    assert interpret(kind, text, new_names=["阚星野"], old_names=["宓春生"]) in ok


@pytest.mark.parametrize(
    ("kind", "text", "decision"),
    [("correction", "不是这个意思。", KEEP_HIDDEN), ("manual_review", "没有这回事。", DROP)],
)
def test_phrases_from_the_design_table_are_recognised(kind: str, text: str, decision: str) -> None:
    assert interpret(kind, text) == decision


@pytest.mark.parametrize(
    ("text", "decision"),
    [
        ("还在，两边都在做。", KEEP_HIDDEN),
        ("不用改，那边也在。", KEEP_HIDDEN),
        ("改吧，那边早走了。", KEEP_NEW),
        ("已经不在了。", KEEP_NEW),
        ("对。", None),  # 问法是“还算不算”还是“要不要结束”分不清：不定
        ("他还在吗？", None),
    ],
)
def test_answers_about_the_other_one(text: str, decision: str | None) -> None:
    """只说了新的那一条时问旧的（correction_other）：还在、还算是不改；不在了、走了、改吧是结束旧的。光说“对”“不”的不定。"""
    assert interpret("correction_other", text) == decision


@pytest.mark.parametrize("text", ["换了个岗位而已。", "试用期结束了，转正了。", "换了领导。", "走了个流程，人还是那边的。",
                                  "合同到期了，续签了。", "他人不在那，出差了。"])
def test_job_answers_that_do_not_end_the_old_job(text: str) -> None:
    """审阅第 4 条、复审第 4 条：岗位、试用期、领导、流程这些“换了”“结束”“走了”，续签了、出差了，都不是人离开了那一家：
    不能认成结束旧的。"""
    assert interpret("correction_other", text) in (None, KEEP_HIDDEN)


@pytest.mark.parametrize("text", ["做完这个月就结束。", "下个月就不在那边了。", "月底离职了。"])
def test_job_answers_about_a_later_day_are_not_taken(text: str) -> None:
    """复审第 4 条：说的是以后才走（这个月、下个月、月底）：照“那一句的时候结束”会提前，不定。"""
    assert interpret("correction_other", text) is None


@pytest.mark.parametrize(
    ("kind", "text", "decision"),
    [
        ("correction", "对，改吧。", KEEP_NEW),
        ("correction", "是的", KEEP_NEW),
        ("correction", "不用", KEEP_HIDDEN),
        ("correction", "别改，原来的对", KEEP_HIDDEN),
        ("correction", "回头再说", None),
        ("correction_unmatched", "对", KEEP_NEW),
        ("correction_unmatched", "不是", KEEP_HIDDEN),
        ("manual_review", "对，有这回事", KEEP_NEW),
        ("manual_review", "没这回事", DROP),
        ("evidence_withdrawn", "留着吧", KEEP_FROZEN),
        ("evidence_withdrawn", "删掉", DROP),
        ("evidence_withdrawn", "先搁着", KEEP_HIDDEN),
        ("ambiguous_entity", "不是同一个人", KEEP_BOTH),
        ("ambiguous_entity", "是同一个", None),  # 合并照旧用合并工具
        ("same_time", "两个都对", KEEP_BOTH),
        ("same_time", "是前一个", None),
        ("soft_change", "先停，等报价出来", "B"),
        ("soft_change", "不做了", "C"),
        ("soft_change", "改成交给程序", "D"),  # “改成”“交给”都是 D：只算一种
        ("soft_change", "照旧", "A"),
        ("soft_change", "还没定", "E"),
        ("witness_mismatch", "对", None),  # 这一种还不能在对话里答
    ],
)
def test_answers_to_other_kinds(kind: str, text: str, decision: str | None) -> None:
    assert interpret(kind, text) == decision


def test_rules_have_a_version() -> None:
    assert answers.RULES_VERSION.startswith("answers-")


# ---------- 哪一句算回答 ----------

ASKED = "按冉北辰改吗？（待确认 Q3F9A1C）"


def said(role: str, minute: int, *texts: str, quoted: tuple[str, ...] = ()) -> answers.Said:
    parts = tuple((f"p#1.{minute}.{number}", text) for number, text in enumerate(texts, 1))
    return answers.Said(role, minute * 60, parts, tuple((f"p#1.{minute}.q{number}", text) for number, text in enumerate(quoted, 1)))


def test_the_owners_first_message_after_the_question_answers_it() -> None:
    messages = [said("bot", 0, "锦屏仓的盘点记着是苗一帆负责。", ASKED), said("other", 1, "我也不清楚"), said("owner", 2, "改吧。")]
    assert answers.question_for(messages, 2) == answers.Asked("p#1.0.2", "Q3F9A1C", "next", ("p#1.0.1", "p#1.0.2"))


@pytest.mark.parametrize(
    ("messages", "why"),
    [
        ([said("bot", 0, ASKED), said("owner", 1, "嗯"), said("owner", 2, "改吧。")], "不是问完以后的第一条"),
        ([said("bot", 0, ASKED), said("owner", 31, "改吧。")], "隔了 30 分钟以上"),
        ([said("bot", 0, "（待确认 Q3F9A1C）还有（待确认 Q000001）"), said("owner", 1, "改吧。")], "一句问了两条"),
        ([said("bot", 0, ASKED), said("bot", 1, "另外这一条呢？（待确认 Q000001）"), said("other", 2, "改吧。")], "别人答的"),
    ],
)
def test_not_an_answer(messages: list[answers.Said], why: str) -> None:
    assert answers.question_for(messages, len(messages) - 1) is None, why


def test_a_question_in_between_takes_the_answer() -> None:
    messages = [said("bot", 0, ASKED), said("bot", 1, "另外这一条呢？（待确认 Q000001）"), said("owner", 2, "改吧。")]
    assert answers.question_for(messages, 2) == answers.Asked("p#1.1.1", "Q000001", "next", ("p#1.1.1",))


def test_a_quoted_question_counts_any_time_later() -> None:
    messages = [said("owner", 3000, "是冉北辰。", quoted=(ASKED,))]
    assert answers.question_for(messages, 0) == answers.Asked("p#1.3000.q1", "Q3F9A1C", "quoted", ("p#1.3000.q1",))


def test_reaching_the_start_asks_for_the_packet_before() -> None:
    assert answers.question_for([said("other", 0, "早"), said("owner", 1, "改吧。")], 1) == answers.EARLIER
