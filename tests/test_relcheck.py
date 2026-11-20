"""L4 核对关系（J4.3）：R1 到 R6 每条规则各有拦下的例子和放过的例子，加上写账时的隔离表。例子都是编的。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World
from test_verify import window_of
from test_write import day_packet, deliver, write

from javmem.cli import main
from javmem.ledger import transaction
from javmem.pipeline.extract import map_output
from javmem.pipeline.preb import apply_notes
from javmem.pipeline.relcheck import RULES, RelationCheck, window_notes
from javmem.pipeline.verify import INFERRED, VERIFIED, summarize_checks, verify_window
from javmem.pipeline.window import Window
from javmem.pipeline.write import LedgerWriter
from javmem.zh.lexicon import EMPTY, AliasGroup, BotEntry, Lexicon, build

OWNER = {"key": "e1", "mention": "我", "name": "主人", "type": "人"}
NOTE = {"key": "e2", "mention": "X1 票据", "name": "X1 票据", "type": "订单或合同"}
BANK = {"key": "e3", "mention": "海港银行", "name": "海港银行", "type": "公司或机构"}
BOT = {"key": "e4", "mention": "desk-bot", "name": "desk-bot", "type": "人"}
ZHOU = {"key": "e5", "mention": "小周", "name": "小周", "type": "人"}
BROKER = {"key": "e6", "mention": "东岸证券", "name": "东岸证券", "type": "公司或机构"}
STOCK = {"key": "e7", "mention": "X2", "name": "X2", "type": "资产"}
DEAL = {"key": "e8", "mention": "X3 累购", "name": "X3 累购", "type": "订单或合同"}


def relation(subject: dict, kind: str, obj: dict, *evidence: tuple[str, str], **fields: object) -> dict[str, object]:
    item: dict[str, object] = {
        "subject": subject["key"],
        "relation": kind,
        "object": obj["key"],
        "kinship": None,
        "state": "生效",
        "valid_from": None,
        "valid_to": None,
        "time_precision": "unknown",
        "certainty": "stated",
        "speaker": "主人",
        "evidence": [{"ref": ref, "quote": quote} for ref, quote in evidence],
    }
    item.update(fields)
    return item


def checks(
    lines: tuple[str, ...] | list[str],
    relations: list[dict[str, object]],
    entities: list[dict[str, object]],
    *,
    lexicon: Lexicon = EMPTY,
    owner_names: tuple[str, ...] = (),
    notes: dict[str, list[list[str]]] | None = None,
) -> list[RelationCheck]:
    window, rendered = window_of(*lines)
    extraction = map_output({"entities": entities, "facts": [], "relations": relations}, window, rendered.redacted_refs)
    if notes:
        rendered = apply_notes(rendered, notes)
    return verify_window(window, rendered, extraction, lexicon=lexicon, owner_names=owner_names).relations


def one(lines: tuple[str, ...] | list[str], item: dict[str, object], entities: list[dict[str, object]], **options: object) -> RelationCheck:
    return checks(lines, [item], entities, **options)[0]  # type: ignore[arg-type]


def codes(result: RelationCheck) -> list[str]:
    return [finding.as_text() for finding in result.failures]


# ---------- L3、R1 原句逐字可查 ----------


def test_a_quote_found_in_its_line_passes_and_stated_is_verified() -> None:
    result = one(["我：X1 票据在海港银行。"], relation(NOTE, "托管于", BANK, ("T1", "X1 票据在海港银行")), [NOTE, BANK])
    assert result.passed and result.status == VERIFIED
    assert [proof.sentence_id for proof in result.proofs] and result.attributed_to == "主人"


def test_a_quote_not_in_the_window_is_quarantined_by_r1() -> None:
    result = one(["我：X1 票据在海港银行。"], relation(NOTE, "托管于", BANK, ("T1", "X1 票据在东岸证券")), [NOTE, BANK])
    assert codes(result) == ["R1:quote_not_found"] and result.first_rule == "R1"


def test_a_bad_quote_is_dropped_when_another_one_holds() -> None:
    result = one(
        ["我：X1 票据在海港银行。", "desk-bot：好的。"],
        relation(NOTE, "托管于", BANK, ("T1", "X1 票据在海港银行"), ("T2", "X1 票据转走了")),
        [NOTE, BANK],
    )
    assert result.passed
    assert [finding.as_text() for _, finding in result.dropped] == ["R1:quote_not_found"]


def test_problems_from_l3_quarantine_directly_and_bad_shapes_too() -> None:
    unknown = one(["我：X1 票据在海港银行。"], relation(NOTE, "托管于", BANK, ("T9", "X1 票据在海港银行")), [NOTE, BANK])
    assert codes(unknown) == ["L3:no_usable_evidence", "L3:unknown_ref"]
    window, rendered = window_of("我：X1 票据在海港银行。")
    weird = {"entities": [NOTE, BANK], "facts": [], "relations": [
        {"subject": "e2", "relation": "托管于", "object": "e3", "state": "可能", "certainty": "stated",
         "evidence": [{"ref": "T1", "quote": "X1 票据在海港银行"}]},
        "不是对象",
    ]}
    found = verify_window(window, rendered, weird).relations
    assert [codes(item) for item in found] == [["L3:bad_shape"], ["L3:bad_shape"]]


# ---------- R2 两头在原文里出现 ----------


def test_an_end_named_only_in_another_line_passes_with_a_note() -> None:
    result = one(
        ["我：海港银行那边新开了一个户。", "我：X1 票据放那边。"],
        relation(NOTE, "托管于", BANK, ("T2", "X1 票据放那边")),
        [NOTE, BANK],
    )
    assert result.passed and "R2:object_elsewhere" in result.notes


def test_an_end_written_nowhere_is_quarantined_by_r2() -> None:
    ghost = {"key": "e9", "mention": "南湖信托", "name": "南湖信托", "type": "公司或机构"}
    result = one(["我：X1 票据放那边。"], relation(NOTE, "托管于", ghost, ("T1", "X1 票据放那边")), [NOTE, ghost])
    assert codes(result) == ["R2:object_not_in_text"]


def test_the_owner_need_not_be_written_and_owner_names_count_as_owner() -> None:
    result = one(["desk-bot：账上还有 X2 一千股。"], relation(OWNER, "持有", STOCK, ("T1", "账上还有 X2 一千股")), [OWNER, STOCK])
    assert result.passed and not [note for note in result.notes if note.startswith("R2")]
    named = {"key": "e1", "mention": "某甲", "name": "某甲", "type": "人"}
    assert codes(one(["desk-bot：账上还有 X2 一千股。"], relation(named, "持有", STOCK, ("T1", "账上还有 X2 一千股")),
                     [named, STOCK])) == ["R2:subject_not_in_text"]
    assert one(["desk-bot：账上还有 X2 一千股。"], relation(named, "持有", STOCK, ("T1", "账上还有 X2 一千股")),
               [named, STOCK], owner_names=("某甲",)).passed


def test_a_speaker_counts_as_written() -> None:
    result = one(
        ["我：海港银行那边的事以后归你管。", "desk-bot：好的，海港银行这边我来管。"],
        relation(BOT, "负责", BANK, ("T2", "海港银行这边我来管"), speaker="desk-bot"),
        [BOT, BANK],
    )
    assert result.passed and "R2:subject_speaker" in result.notes


def test_names_known_from_the_alias_table_or_a_pre_b_note_count() -> None:
    full = {"key": "e3", "mention": "海港银行总行", "name": "海港银行总行", "type": "公司或机构"}
    lexicon = build([AliasGroup("公司或机构", ("海港银行总行", "海港银行"))])
    lines = ["我：X1 票据放在海港银行。"]
    item = relation(NOTE, "托管于", full, ("T1", "X1 票据放在海港银行"))
    assert codes(one(lines, item, [NOTE, full])) == ["R2:object_not_in_text"]
    result = one(lines, item, [NOTE, full], lexicon=lexicon)
    assert result.passed and "R2:object_alias" in result.notes
    noted = one(["我：X1 票据放在那家。"], relation(NOTE, "托管于", BANK, ("T1", "X1 票据放在那家")), [NOTE, BANK],
                notes={"T1": [["那家", "海港银行"]]})
    assert noted.passed and "R2:object_alias" in noted.notes
    wrong_line = one(["我：X1 票据放在别处。"], relation(NOTE, "托管于", BANK, ("T1", "X1 票据放在别处")), [NOTE, BANK],
                     notes={"T1": [["那家", "海港银行"]]})  # 〔指〕的说法不在那一行原文里：不算
    assert codes(wrong_line) == ["R2:object_not_in_text"]


def test_a_name_put_together_from_pieces_in_the_window_counts() -> None:
    joined = {"key": "e8", "mention": "X3 累购", "name": "X3 累购", "type": "订单或合同"}
    lines = ["我：X3 那张累购还在接货。"]
    result = one(lines, relation(OWNER, "持有", joined, ("T1", "X3 那张累购还在接货")), [OWNER, joined])
    assert result.passed and "R2:object_pieces" in result.notes
    made_up = {"key": "e8", "mention": "X3 盯盘提醒", "name": "X3 盯盘提醒", "type": "项目"}
    assert codes(one(lines, relation(BOT, "负责", made_up, ("T1", "X3 那张累购还在接货")), [BOT, made_up])) == [
        "R2:subject_not_in_text", "R2:object_not_in_text"
    ]


def test_notes_saved_by_pre_b_reach_r2_through_verify_windows(tmp_path: Path) -> None:
    """J4.2 的预处理 B 把〔指〕存在结果库里，verify_windows 照存下的加上（和抽的时候一字不差），R2 就认得。"""
    from test_preb import save_notes

    from javmem.llm import FakeModel
    from javmem.pipeline.extract import prepare, window_row
    from javmem.pipeline.store import ResultStore
    from javmem.pipeline.verify import verify_windows
    from javmem.redaction import default_patterns

    patterns = default_patterns()
    window, _ = window_of("我：X1 票据放在那家。")
    item = relation(NOTE, "托管于", BANK, ("T1", "X1 票据放在那家"))
    with ResultStore(tmp_path / "l3") as store:
        for noted in (False, True):
            if noted:
                save_notes(store, window, {"T1": [["那家", "海港银行"]]})
            _, rendered, key = prepare([window], FakeModel(), patterns, store=store)[0]
            store.save_window(window_row(window, rendered, key, FakeModel().cache_name))
            extraction = map_output({"entities": [NOTE, BANK], "facts": [], "relations": [item]}, window, rendered.redacted_refs)
            store.save_extraction(window.window_id, key, extraction)
            found = verify_windows([window], store, FakeModel(), patterns)[0].check
            assert found is not None
            assert codes(found.relations[0]) == ([] if noted else ["R2:object_not_in_text"])
            assert ("R2:object_alias" in found.relations[0].notes) is noted


def test_notes_written_by_pre_b_read_back() -> None:
    _, rendered = window_of("我：X1 票据放在那家。", "我：那张也是。")
    noted = apply_notes(rendered, {"T1": [["那家", "海港银行"], ["X1 票据", "X1 票据"]], "T2": [["那张", "X1 票据"]]})
    assert window_notes(noted) == {"T1": [("那家", "海港银行"), ("X1 票据", "X1 票据")], "T2": [("那张", "X1 票据")]}
    assert window_notes(rendered) == {}


# ---------- R3 类型 ----------


def test_types_must_be_in_the_allowed_table() -> None:
    lines = ["我：我在海港银行有个户。"]
    custody = one(lines, relation(OWNER, "托管于", BANK, ("T1", "我在海港银行有个户")), [OWNER, BANK])
    assert codes(custody) == ["R3:type_not_allowed"]  # 主人不托管在哪家：托管于的主体是东西
    client = one(lines, relation(OWNER, "客户是", BANK, ("T1", "我在海港银行有个户")), [OWNER, BANK])
    assert client.passed and (client.subject_type, client.object_type) == ("人", "公司或机构")
    other = one(lines, relation(OWNER, "供应商是", BANK, ("T1", "我在海港银行有个户")), [OWNER, BANK])
    assert codes(other) == ["R3:type_not_allowed"]  # 不是六种之一


def test_bots_from_the_bot_list_are_their_own_type() -> None:
    lexicon = build([], bots=[BotEntry(("desk-bot",))])
    project = {"key": "e9", "mention": "每日盯盘", "name": "每日盯盘", "type": "项目"}
    bot_as_project = dict(BOT, type="项目")
    lines = ["我：以后每日盯盘归 desk-bot。"]
    item = relation(bot_as_project, "负责", project, ("T1", "以后每日盯盘归 desk-bot"))
    assert codes(one(lines, item, [bot_as_project, project])) == ["R3:type_not_allowed"]  # 没有名单：项目负责项目
    result = one(lines, item, [bot_as_project, project], lexicon=lexicon)
    assert result.passed and result.subject_type == "Bot"
    other_bot = {"key": "e9", "mention": "desk-bot", "name": "desk-bot", "type": "人"}
    assert "R3:type_not_allowed" in codes(one(lines, relation(bot_as_project, "负责", other_bot, ("T1", "归 desk-bot")),
                                              [bot_as_project, other_bot], lexicon=lexicon))


def test_same_ends_and_kinship_without_a_word_are_quarantined() -> None:
    lines = ["我：小周是我太太。"]
    assert codes(one(lines, relation(ZHOU, "亲属", ZHOU, ("T1", "小周是我太太"), kinship="妻子"), [ZHOU])) == ["R3:same_ends"]
    assert codes(one(lines, relation(ZHOU, "亲属", OWNER, ("T1", "小周是我太太")), [ZHOU, OWNER])) == ["R3:kinship_missing"]
    assert one(lines, relation(ZHOU, "亲属", OWNER, ("T1", "小周是我太太"), kinship="妻子"), [ZHOU, OWNER]).passed


# ---------- 隶属于（D210、D213，卡 7）：编的公司、部门 ----------

TRADE = {"key": "e10", "mention": "示例贸易", "name": "示例贸易", "type": "公司或机构"}
DEPT = {"key": "e11", "mention": "外贸部", "name": "示例贸易 外贸部", "type": "公司或机构"}
BRANCH = {"key": "e12", "mention": "示例贸易华东分公司", "name": "示例贸易华东分公司", "type": "公司或机构"}
KELAN = {"key": "e13", "mention": "柯岚", "name": "柯岚", "type": "人"}


def test_part_of_goes_from_a_unit_to_the_organization_it_belongs_to() -> None:
    """隶属于：（公司或机构，公司或机构）。人不当主体：人在哪个部门记任职于（允许表，D210）。"""
    lines = ["我：外贸部是示例贸易下面的部门，柯岚在外贸部。"]
    found = one(lines, relation(DEPT, "隶属于", TRADE, ("T1", "外贸部是示例贸易下面的部门")), [DEPT, TRADE])
    assert found.passed and (found.subject_type, found.object_type) == ("公司或机构", "公司或机构")
    person = one(lines, relation(KELAN, "隶属于", DEPT, ("T1", "柯岚在外贸部")), [KELAN, DEPT])
    assert codes(person) == ["R3:type_not_allowed"]
    assert one(lines, relation(KELAN, "任职于", DEPT, ("T1", "柯岚在外贸部")), [KELAN, DEPT]).passed  # 人连部门是任职于
    assert codes(one(lines, relation(DEPT, "隶属于", DEPT, ("T1", "外贸部是示例贸易下面的部门")), [DEPT])) == ["R3:same_ends"]


@pytest.mark.parametrize(
    ("line", "unit"),
    [
        ("我：示例贸易华东分公司已经划出去了。", BRANCH),
        ("我：外贸部上个月撤销了。", DEPT),
        ("我：外贸部已经裁撤。", DEPT),
        ("我：外贸部合并掉了。", DEPT),
        ("我：示例贸易华东分公司卖给了别家。", BRANCH),
        ("我：示例贸易华东分公司不再是示例贸易的子公司了。", BRANCH),
        ("我：外贸部不再是示例贸易的了。", DEPT),
    ],
)
def test_part_of_end_words_end_it(line: str, unit: dict) -> None:
    """卡 7 第 5 项的结束说法：划出去、撤销、裁撤、合并掉、卖给了别家、不再是 X 的。说的是主体（部门、分公司），名字在前面。"""
    quote = line.split("：", 1)[1].rstrip("。")
    lines = [line, "我：说的是示例贸易那边。"]  # 客体在窗口别的行里（R2）
    active = one(lines, relation(unit, "隶属于", TRADE, ("T1", quote)), [unit, TRADE])
    assert codes(active) == ["R4:ended_but_active"], line
    assert one(lines, relation(unit, "隶属于", TRADE, ("T1", quote), state="已结束"), [unit, TRADE]).passed


@pytest.mark.parametrize(
    "line",
    [
        "我：示例贸易撤销了外贸部的申请。",  # 名字在结束词后面：不认（宁可漏）
        "我：上周撤销了外贸部的申请。",  # 前面没有名字、后面的也不认
        "我：外贸部不再是主要的部门了。",  # “不再是”说的不是归哪家
        "我：外贸部下个月撤销。",  # 以后的事
        "我：外贸部撤销了吗？",  # 问句
        "我：示例贸易华东分公司划出去了一部分业务。",  # 只结束一部分
        "我：外贸部没撤销。",  # 否定
    ],
)
def test_part_of_end_words_that_do_not_end_it(line: str) -> None:
    quote = line.split("：", 1)[1].rstrip("。？")
    unit = BRANCH if "华东" in line else DEPT
    lines = [line, "我：说的是示例贸易那边。"]
    assert one(lines, relation(unit, "隶属于", TRADE, ("T1", quote)), [unit, TRADE]).passed, line


def test_leaving_the_company_ends_the_job_in_its_department() -> None:
    """任职于的结束词那一小句写着公司的，要是这一家：这条任职于连的是部门的，写的是这个部门往上的那家也算（照这个窗口
    L3 抽的、没结束的隶属于，一层层往上找）。反过来（任职于公司、小句里只写着它的部门）不认：辞去部门的职位不一定离开公司。"""
    lines = ["我：柯岚已经从示例贸易离职了。", "我：外贸部是示例贸易下面的部门。"]
    left = relation(KELAN, "任职于", DEPT, ("T1", "柯岚已经从示例贸易离职了"))
    part = relation(DEPT, "隶属于", TRADE, ("T2", "外贸部是示例贸易下面的部门"))
    found = checks(lines, [left, part], [KELAN, DEPT, TRADE])
    assert [codes(item) for item in found] == [["R4:ended_but_active"], []]
    assert one(lines, left, [KELAN, DEPT, TRADE]).passed  # 窗口里没有隶属于：说不准外贸部是不是示例贸易的，不拦
    ended = relation(DEPT, "隶属于", TRADE, ("T2", "外贸部是示例贸易下面的部门"), state="已结束")
    assert codes(checks(lines, [left, ended], [KELAN, DEPT, TRADE])[0]) == []  # 已经结束的隶属于不往上找
    two_levels = ["我：柯岚已经从示例贸易离职了。", "我：外贸部归示例贸易华东分公司，示例贸易华东分公司是示例贸易的。"]
    chain = [left, relation(DEPT, "隶属于", BRANCH, ("T2", "外贸部归示例贸易华东分公司")),
             relation(BRANCH, "隶属于", TRADE, ("T2", "示例贸易华东分公司是示例贸易的"))]
    assert codes(checks(two_levels, chain, [KELAN, DEPT, BRANCH, TRADE])[0]) == ["R4:ended_but_active"]
    reverse = ["我：柯岚已经辞去外贸部的职位。", "我：外贸部是示例贸易下面的部门。"]
    company_job = relation(KELAN, "任职于", TRADE, ("T1", "柯岚已经辞去外贸部的职位"))
    assert checks(reverse, [company_job, part], [KELAN, DEPT, TRADE])[0].passed


# ---------- R4 结束词 ----------


def test_an_ended_holding_marked_active_is_quarantined_by_r4() -> None:
    lines = ["我：X1 票据已经到期了，钱回到账上。"]
    active = one(lines, relation(OWNER, "持有", NOTE, ("T1", "X1 票据已经到期了")), [OWNER, NOTE])
    assert codes(active) == ["R4:ended_but_active"]
    ended = one(lines, relation(OWNER, "持有", NOTE, ("T1", "X1 票据已经到期了"), state="已结束"), [OWNER, NOTE])
    assert ended.passed


@pytest.mark.parametrize(
    "line",
    [
        "我：X1 票据 2027 年 1 月到期。",  # 以后的事
        "我：X1 票据下周到期。",
        "我：X1 票据到期了吗？",  # 问句
        "我：X1 票据还没到期。",  # 否定
        "我：如果 X1 票据到期了，就转定存。",  # 条件
        "我：X1 票据可能已经到期了。",  # 语气
        "我：X1 票据到期日是 11 月 1 日。",  # 名词
        "我：X1 票据差一步敲出。",
        "我：等海港银行确认 X1 票据是否敲出。",
        "我：X1 票据到期。",  # 没说已经发生了
    ],
)
def test_end_words_that_have_not_happened_do_not_count(line: str) -> None:
    quote = line.split("：", 1)[1].rstrip("。？")
    assert one([line], relation(OWNER, "持有", NOTE, ("T1", quote)), [OWNER, NOTE]).passed


def test_an_end_word_only_ends_the_relation_it_is_about() -> None:
    lines = ["我：X3 累购敲出了，买了 X2 一千股。"]
    entities = [OWNER, DEAL, STOCK]
    found = checks(lines, [
        relation(OWNER, "持有", STOCK, ("T1", "买了 X2 一千股")),
        relation(OWNER, "持有", DEAL, ("T1", "X3 累购敲出了")),
    ], entities)
    assert [codes(item) for item in found] == [[], ["R4:ended_but_active"]]


def test_an_end_word_about_something_without_an_entity_ends_nothing() -> None:
    lines = ["我：不再派三号机做盯盘了，以后每日盯盘归 desk-bot。"]
    project = {"key": "e9", "mention": "每日盯盘", "name": "每日盯盘", "type": "项目"}
    result = one(lines, relation(BOT, "负责", project, ("T1", "以后每日盯盘归 desk-bot")), [BOT, project],
                 lexicon=build([], bots=[BotEntry(("desk-bot",))]))
    assert result.passed


def test_no_longer_responsible_ends_it_but_put_aside_is_a_pause() -> None:
    lexicon = build([], bots=[BotEntry(("desk-bot",))])
    ended = one(["我：desk-bot 不再负责海港银行那边的事。"],
                relation(BOT, "负责", BANK, ("T1", "desk-bot 不再负责海港银行那边的事")), [BOT, BANK], lexicon=lexicon)
    assert codes(ended) == ["R4:ended_but_active"]
    paused = one(["我：海港银行那边 desk-bot 先不管了。"],
                 relation(BOT, "负责", BANK, ("T1", "海港银行那边 desk-bot 先不管了"), state="暂停"), [BOT, BANK], lexicon=lexicon)
    assert paused.passed


def test_leaving_one_company_does_not_end_the_job_at_the_next() -> None:
    lines = ["我：小周已辞去东岸证券的工作，加入海港银行。"]
    found = checks(lines, [
        relation(ZHOU, "任职于", BROKER, ("T1", "小周已辞去东岸证券的工作")),
        relation(ZHOU, "任职于", BANK, ("T1", "加入海港银行")),
    ], [ZHOU, BROKER, BANK])
    assert [codes(item) for item in found] == [["R4:ended_but_active"], []]


def test_a_knock_out_ends_the_deal_not_the_stock_it_delivers() -> None:
    lines = ["desk-bot：挂 X2 的那张 X3 已 KO，交来的一百二十股 X2 记进股票仓。"]
    stock = {"key": "e7", "mention": "X2", "name": "X2", "type": "资产"}
    assert one(lines, relation(OWNER, "持有", stock, ("T1", "挂 X2 的那张 X3 已 KO"), speaker="desk-bot"),
               [OWNER, stock]).passed  # 只有单子才会敲出：KO 说的是那张单子


def test_ko_as_an_event_ends_a_deal_but_a_ko_level_does_not() -> None:
    assert codes(one(["desk-bot：X3 累购已 KO。"], relation(OWNER, "持有", DEAL, ("T1", "X3 累购已 KO")), [OWNER, DEAL])) == [
        "R4:ended_but_active"
    ]
    assert one(["desk-bot：X3 累购 KO 586.37，已经接了两周。"], relation(OWNER, "持有", DEAL, ("T1", "X3 累购 KO 586.37")),
               [OWNER, DEAL]).passed
    level = one(["desk-bot：6/3 那张 X3 累购眼下停在 KO 上头，保护期没过，照接。"],
                relation(OWNER, "持有", DEAL, ("T1", "6/3 那张 X3 累购眼下停在 KO 上头"), speaker="desk-bot"), [OWNER, DEAL])
    assert level.passed  # KO 上头是价位；6/3 是单子的名字，不是已经敲出的根据
    kospi = {"key": "e8", "mention": "X3", "name": "X3", "type": "订单或合同"}
    assert one(["desk-bot：X3 跟着 KOSPI 了。"], relation(OWNER, "持有", kospi, ("T1", "X3 跟着 KOSPI 了")), [OWNER, kospi]).passed


def test_ended_with_an_end_date_after_the_window_is_quarantined() -> None:
    lines = ["我：X1 票据 12 月 1 日到期。"]
    item = relation(OWNER, "持有", NOTE, ("T1", "X1 票据 12 月 1 日到期"), state="已结束", valid_to="2026-12-01")
    assert codes(one(lines, item, [OWNER, NOTE])) == ["R4:ends_in_future"]
    planned = relation(OWNER, "持有", NOTE, ("T1", "X1 票据 12 月 1 日到期"), valid_to="2026-12-01")
    result = one(lines, planned, [OWNER, NOTE])
    assert result.passed and (result.valid_to, result.valid_to_precision) == ("2026-12-01", "day")


# ---------- R5 日子 ----------


def test_dates_with_support_stay_and_get_their_own_precision() -> None:
    lines = ["我：今天在东岸证券买了 X2，11月12日交割。"]
    said = one(lines, relation(OWNER, "持有", STOCK, ("T1", "今天在东岸证券买了 X2"), valid_from="2026-11-08"), [OWNER, STOCK])
    assert (said.valid_from, said.valid_from_precision, said.notes) == ("2026-11-08", "day", [])
    written = one(lines, relation(OWNER, "持有", STOCK, ("T1", "11月12日交割"), valid_from="2026-11-12"), [OWNER, STOCK])
    assert (written.valid_from, written.valid_from_precision) == ("2026-11-12", "day")
    relative = one(["我：上周三买了 X2。"], relation(OWNER, "持有", STOCK, ("T1", "上周三买了 X2"), valid_from="2026-10-28"),
                   [OWNER, STOCK])
    assert relative.valid_from == "2026-10-28" and not relative.notes


def test_a_date_without_support_is_dropped_but_the_relation_stays() -> None:
    result = one(["我：X2 还在东岸证券。"], relation(STOCK, "托管于", BROKER, ("T1", "X2 还在东岸证券"), valid_from="2026-09-01"),
                 [STOCK, BROKER])
    assert result.passed and result.valid_from is None and result.valid_from_precision == "unknown"
    assert result.notes == ["R5:start_dropped"]


def test_a_date_supported_only_by_month_is_lowered() -> None:
    result = one(["我：X2 是 9 月买的。"], relation(OWNER, "持有", STOCK, ("T1", "X2 是 9 月买的"), valid_from="2026-09-15"),
                 [OWNER, STOCK])
    assert (result.valid_from, result.valid_from_precision) == ("2026-09", "month") and result.notes == ["R5:start_lowered"]


def test_a_date_written_in_another_line_counts_with_a_note_and_end_before_start_is_dropped() -> None:
    lines = ["desk-bot：11月2日那份结单到了。", "desk-bot：X2 一千股在东岸证券。"]
    result = one(lines, relation(STOCK, "托管于", BROKER, ("T2", "X2 一千股在东岸证券"), valid_from="2026-11-02",
                                 speaker="desk-bot"), [STOCK, BROKER])
    assert result.valid_from == "2026-11-02" and result.notes == ["R5:start_elsewhere"]
    backwards = one(["我：X2 是9月9日买的，9月2日卖了。"],
                    relation(OWNER, "持有", STOCK, ("T1", "X2 是9月9日买的，9月2日卖了"), state="已结束",
                             valid_from="2026-09-09", valid_to="2026-09-02"), [OWNER, STOCK])
    assert backwards.valid_to is None and "R5:end_before_start" in backwards.notes and backwards.passed


# ---------- R6 说话人 ----------


def test_said_by_the_owner_without_owner_words_is_moved_to_the_speaker_and_inferred() -> None:
    result = one(["desk-bot：海港银行那边你也有户。"], relation(OWNER, "客户是", BANK, ("T1", "海港银行那边你也有户")), [OWNER, BANK])
    assert result.passed and result.attributed_to == "desk-bot" and result.status == INFERRED
    assert "R6:not_owner" in result.notes
    owner = one(["我：海港银行那边我也有户。"], relation(OWNER, "客户是", BANK, ("T1", "海港银行那边我也有户")), [OWNER, BANK])
    assert owner.attributed_to == "主人" and owner.status == VERIFIED and not owner.notes


def test_a_source_named_by_l3_stays_and_inferred_stays_inferred() -> None:
    result = one(["desk-bot：东岸证券周报：X2 一千股。"],
                 relation(STOCK, "托管于", BROKER, ("T1", "X2 一千股"), speaker="东岸证券周报", certainty="inferred"), [STOCK, BROKER])
    assert result.passed and result.attributed_to == "东岸证券周报" and result.status == INFERRED and not result.notes


# ---------- 汇总、写账 ----------


def test_summary_counts_relations_by_rule_without_text() -> None:
    window, rendered = window_of("我：X1 票据已经到期了。", "我：X2 还在东岸证券。")
    extraction = map_output({"entities": [OWNER, NOTE, STOCK, BROKER], "facts": [], "relations": [
        relation(OWNER, "持有", NOTE, ("T1", "X1 票据已经到期了")),
        relation(STOCK, "托管于", BROKER, ("T2", "X2 还在东岸证券"), valid_from="2026-01-01"),
        relation(OWNER, "托管于", BROKER, ("T2", "X2 还在东岸证券")),
    ]}, window, rendered.redacted_refs)
    summary = summarize_checks([verify_window(window, rendered, extraction)])["relations"]
    assert isinstance(summary, dict)
    assert (summary["relations"], summary["passed"], summary["quarantined"]) == (3, 1, 2)
    assert summary["by_rule"] == {"R3": 1, "R4": 1} and summary["by_first_rule"] == {"R3": 1, "R4": 1}
    assert summary["notes_on_passed"] == {"R5:start_dropped": 1} and summary["status"] == {VERIFIED: 1, INFERRED: 0}
    assert "X1" not in json.dumps(summary, ensure_ascii=False)
    assert set(RULES) == {"R1", "R2", "R3", "R4", "R5", "R6", "C"}  # C 是判断（javmem.pipeline.reljudge），配了才判


def _relations_extract(window: Window) -> dict[str, object]:
    first = next(line.ref for line in window.lines if line.target and "到期" in line.text)
    second = next(line.ref for line in window.lines if line.target and "东岸证券" in line.text)
    return {
        "entities": [OWNER, NOTE, STOCK, BROKER],
        "facts": [],
        "relations": [
            relation(OWNER, "持有", NOTE, (first, "X1 票据已经到期了")),
            relation(STOCK, "托管于", BROKER, (second, "X2 还在东岸证券"), (second, "X2 转走了")),
        ],
    }


def test_write_puts_failed_relations_and_dropped_evidence_in_quarantine(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据已经到期了。", "desk-bot：X2 还在东岸证券。"))
    conn = write(world, _relations_extract)
    rows = conn.execute(
        "SELECT item_kind, reason, rule_id, rule_version, window_id, payload FROM quarantine ORDER BY item_kind"
    ).fetchall()
    assert [(row["item_kind"], row["reason"], row["rule_id"]) for row in rows] == [
        ("relation", "R4:ended_but_active", "R4"),
        ("relation_evidence", "R1:quote_not_found", "R1"),
    ]
    assert all(row["window_id"] and row["rule_version"] for row in rows)
    payload = json.loads(rows[0]["payload"])
    assert payload["relation"]["relation"] == "持有" and payload["failures"] == ["R4:ended_but_active"]
    assert json.loads(rows[1]["payload"])["relation"] == {"subject": "e7", "relation": "托管于", "object": "e6"}
    # 过了的那条 J4.4 起写进账本（去掉的那条证据不在它的证据里），没过的那条不写
    written = conn.execute("SELECT relation_type FROM relations").fetchall()
    assert [row[0] for row in written] == ["托管于"]
    assert conn.execute("SELECT count(*) FROM relation_evidence").fetchone()[0] == 1
    conn.close()
    conn = write(world, _relations_extract)  # 重写同一个窗口：先清掉再写，不重复
    assert conn.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 2
    sentence = json.loads(rows[0]["payload"])["relation"]["evidence"][0]
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake", register=False)
    with transaction(conn):  # 单条撤回那一句：引着它的隔离一起去掉
        writer.remove_sentences([(sentence["sentence_id"], sentence["packet_id"], sentence["revision"])])
    assert [row[0] for row in conn.execute("SELECT item_kind FROM quarantine")] == ["relation_evidence"]
    conn.close()


def test_verify_command_reports_relations(world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据已经到期了。"))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    report = tmp_path / "verify.json"
    assert main(["verify", "--library", "prod", "--fake", "--report", str(report)]) == 0
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["relations"]["relations"] == summary["relations"]["passed"] + summary["relations"]["quarantined"]
    assert "关系 " in capsys.readouterr().out


# ---------- 审阅查出来的（11-16）：R4 不该拦的、怪输入、慢、R5 说话那天、撤回 ----------

NOTE_EN = {"key": "e2", "mention": "X1 note", "name": "X1 note", "type": "订单或合同"}
PROJECT = {"key": "e9", "mention": "每日盯盘", "name": "每日盯盘", "type": "项目"}
BOTS = build([], bots=[BotEntry(("desk-bot",))])


@pytest.mark.parametrize(
    ("line", "target"),
    [
        ("我：X1 票据已经到期了？", NOTE),  # 只有问号的问句
        ("我：X2 卖了？", STOCK),
        ("我：X3 累购已经敲出了？", DEAL),
        ("我：X1 票据到期了没有？", NOTE),
        ("我：X1 票据是不是已经到期了？", NOTE),
        ("我：Has the X1 note matured?", NOTE_EN),
        ("我：We have not sold X2 yet.", STOCK),  # 英文的否定
        ("我：The X1 note never matured, it was rolled over.", NOTE_EN),
        ("我：已提交 X1 票据的赎回申请。", NOTE),  # 只是申请
    ],
)
def test_end_words_in_questions_english_negations_and_requests_do_not_count(line: str, target: dict) -> None:
    quote = line.split("：", 1)[1].rstrip("。？?.")
    assert one([line], relation(OWNER, "持有", target, ("T1", quote)), [OWNER, target]).passed


@pytest.mark.parametrize(
    ("line", "item", "entities"),
    [
        # 说的是不建实体的人、东西：离它最近的那个实体不背这个结束
        ("我：不再派三号机而是派 desk-bot 做每日盯盘。", relation(BOT, "负责", PROJECT, ("T1", "派 desk-bot 做每日盯盘")), [BOT, PROJECT]),
        ("我：我在东岸证券上班，我同事昨天离职了。", relation(OWNER, "任职于", BROKER, ("T1", "我在东岸证券上班")), [OWNER, BROKER]),
        ("我：小周的妹妹离婚了。", relation(ZHOU, "亲属", OWNER, ("T1", "小周的妹妹离婚了"), kinship="妻子"), [ZHOU, OWNER]),
        ("desk-bot：小周说海港银行的老王已经离职了，以后由小周对接。",
         relation(ZHOU, "任职于", BANK, ("T1", "小周说海港银行的老王已经离职了"), speaker="desk-bot"), [ZHOU, BANK]),
        ("desk-bot：X3 累购的姊妹单已经敲出了。", relation(OWNER, "持有", DEAL, ("T1", "X3 累购的姊妹单已经敲出了"), speaker="desk-bot"),
         [OWNER, DEAL]),
        # 只结束了一部分、是别人的那一份
        ("我：今天卖了 X2 五百股，还剩五百股。", relation(OWNER, "持有", STOCK, ("T1", "今天卖了 X2 五百股")), [OWNER, STOCK]),
        ("我：今天卖了 X2 五百股还剩五百股。", relation(OWNER, "持有", STOCK, ("T1", "今天卖了 X2 五百股还剩五百股")), [OWNER, STOCK]),
        ("我：我在海港银行销户了一个子账户，主账户还在。", relation(OWNER, "客户是", BANK, ("T1", "我在海港银行销户了一个子账户")),
         [OWNER, BANK]),
        ("我：小周把她那份 X2 卖了，我的还拿着。", relation(OWNER, "持有", STOCK, ("T1", "小周把她那份 X2 卖了")), [OWNER, STOCK, ZHOU]),
    ],
)
def test_end_words_about_someone_else_or_only_a_part_do_not_count(line: str, item: dict, entities: list) -> None:
    assert one([line], item, entities, lexicon=BOTS).passed


X5 = {"key": "e10", "mention": "X5", "name": "X5", "type": "资产"}


@pytest.mark.parametrize(
    ("line", "item", "entities"),
    [
        # 第二遍审阅：收窄以后这些真结束了的不能放过
        ("我：海港银行的 X1 票据已经到期了。", relation(OWNER, "持有", NOTE, ("T1", "海港银行的 X1 票据已经到期了")), [OWNER, NOTE, BANK]),
        ("desk-bot：X1 票据在海港银行已经到期了。",
         relation(OWNER, "持有", NOTE, ("T1", "X1 票据在海港银行已经到期了"), speaker="desk-bot"), [OWNER, NOTE, BANK]),
        ("我：X2 已经清仓了，X5 还在。", relation(OWNER, "持有", STOCK, ("T1", "X2 已经清仓了")), [OWNER, STOCK, X5]),
        ("我：X2 已经卖光了，钱还在东岸证券。", relation(STOCK, "托管于", BROKER, ("T1", "X2 已经卖光了")), [STOCK, BROKER]),
        ("我：剩下的 X2 也卖了。", relation(OWNER, "持有", STOCK, ("T1", "剩下的 X2 也卖了")), [OWNER, STOCK]),
        ("我：We sold the remaining X2 shares last week.",
         relation(OWNER, "持有", STOCK, ("T1", "We sold the remaining X2 shares last week")), [OWNER, STOCK]),
        ("我：X2 的 500 股全部卖掉了。", relation(OWNER, "持有", STOCK, ("T1", "X2 的 500 股全部卖掉了")), [OWNER, STOCK]),
        ("我：小周已经从东岸证券离职了。", relation(ZHOU, "任职于", BROKER, ("T1", "小周已经从东岸证券离职了")), [ZHOU, BROKER]),
        ("我：东岸证券的账户已经销户了。", relation(OWNER, "客户是", BROKER, ("T1", "东岸证券的账户已经销户了")), [OWNER, BROKER]),
        ("我：东岸证券的户已经销了，海港银行的还在。", relation(OWNER, "客户是", BROKER, ("T1", "东岸证券的户已经销了")),
         [OWNER, BROKER, BANK]),
        ("desk-bot：X3 累购在 11 月 2 日敲出了。",
         relation(OWNER, "持有", DEAL, ("T1", "X3 累购在 11 月 2 日敲出了"), speaker="desk-bot"), [OWNER, DEAL]),
        ("我：We sold X2 last week.", relation(OWNER, "持有", STOCK, ("T1", "We sold X2 last week")), [OWNER, STOCK]),
    ],
)
def test_real_endings_in_common_shapes_still_count(line: str, item: dict, entities: list) -> None:
    assert codes(one([line], item, entities)) == ["R4:ended_but_active"]


@pytest.mark.parametrize(
    ("line", "entities"),
    [
        ("我：X2 先放着，" + "卖了" * 8000 + "。", [OWNER, STOCK]),  # 结束词挂不上：每一处都走到还剩着的、多少股那几样
        ("我：X2 stays, " + "sold " * 8000 + ".", [OWNER, STOCK]),  # 英文的结束词：每一处都看前面有没有否定
        ("我：X2 先放着，" + "X5卖了" * 4000 + "。", [OWNER, STOCK, X5]),  # 一小句里几千个别的名字
    ],
)
def test_long_lines_full_of_end_words_stay_fast(line: str, entities: list) -> None:
    import time

    window, rendered = window_of(line)
    extraction = map_output({"entities": entities, "facts": [], "relations": [
        relation(OWNER, "持有", STOCK, ("T1", line.split("：", 1)[1][:8]))]}, window, rendered.redacted_refs)
    started = time.perf_counter()
    verify_window(window, rendered, extraction)
    assert time.perf_counter() - started < 3


def test_odd_values_in_a_stored_relation_do_not_break_the_window() -> None:
    window, rendered = window_of("我：X1 票据在海港银行。")
    odd = {"entities": [NOTE, BANK], "facts": [], "relations": [
        dict(relation(NOTE, "托管于", BANK, ("T1", "X1 票据在海港银行")), state=["生效"]),
        dict(relation(NOTE, "托管于", BANK, ("T1", "X1 票据在海港银行")), certainty={"a": 1}),
        dict(relation(NOTE, "托管于", BANK, ("T1", "X1 票据在海港银行")), relation=["托管于"]),
    ]}
    assert [codes(item) for item in verify_window(window, rendered, odd).relations] == [["L3:bad_shape"]] * 3


def test_a_long_line_with_many_end_words_stays_fast() -> None:
    import time

    window, rendered = window_of("我：X1 票据" + "未到期" * 16000 + "。")
    extraction = map_output({"entities": [OWNER, NOTE], "facts": [], "relations": [
        relation(OWNER, "持有", NOTE, ("T1", "X1 票据未到期"))]}, window, rendered.redacted_refs)
    started = time.perf_counter()
    assert verify_window(window, rendered, extraction).relations[0].passed
    assert time.perf_counter() - started < 2  # 原来 48,000 字要 8 秒多：每一处结束词都从头切一遍前面的字


def test_the_said_day_supports_a_start_only_when_the_sentence_names_no_date() -> None:
    named = one(["我：X2 是上周三在东岸证券买的。"],
                relation(OWNER, "持有", STOCK, ("T1", "X2 是上周三在东岸证券买的"), valid_from="2026-11-08"), [OWNER, STOCK])
    assert named.valid_from != "2026-11-08" and any(note.startswith("R5:start_") for note in named.notes)
    bare = one(["我：刚在东岸证券买了 X2。"], relation(OWNER, "持有", STOCK, ("T1", "刚在东岸证券买了 X2"), valid_from="2026-11-08"),
               [OWNER, STOCK])
    assert (bare.valid_from, bare.notes) == ("2026-11-08", [])


def _fixed_ref_extract(window: Window) -> dict[str, object]:
    first = next(line.ref for line in window.lines if line.target and "海港银行" in line.text)
    return {
        "entities": [OWNER, BANK],
        "facts": [],
        "relations": [relation(OWNER, "托管于", BANK, (first, "我在东岸证券也有户"))],  # 引错了行：R1 改指第二句
    }


def test_withdrawing_the_sentence_a_quarantined_relation_was_moved_to_clears_it(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：海港银行那边先不动。", "我：我在东岸证券也有户。"))
    conn = write(world, _fixed_ref_extract)
    row = conn.execute("SELECT payload FROM quarantine WHERE item_kind = 'relation'").fetchone()
    payload = json.loads(row["payload"])
    assert payload["failures"] == ["R3:type_not_allowed"] and "R1:ref_fixed" in payload["notes"]
    proof = payload["proofs"][0]
    assert proof["sentence_id"] != payload["relation"]["evidence"][0]["sentence_id"]
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake", register=False)
    with transaction(conn):
        writer.remove_sentences([(proof["sentence_id"], proof["packet_id"], proof["revision"])])
    assert conn.execute("SELECT count(*) FROM quarantine WHERE item_kind = 'relation'").fetchone()[0] == 0
    conn.close()
