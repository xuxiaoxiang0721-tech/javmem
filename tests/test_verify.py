"""L4 核对：G1 到 G7 每条规则各有拦下的例子和放过的例子，都是编的。"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from helpers import CHAT_ID, CHAT_PACKET, MAIL_PACKET, SHANGHAI, World, build_packet, file_name

from javmem.cli import main
from javmem.packet import parse_packet
from javmem.pipeline.extract import map_output
from javmem.pipeline.normalize import normalize
from javmem.pipeline.render import RenderedWindow, render_window
from javmem.pipeline.verify import (
    INFERRED,
    L4_VERSION,
    VERIFIED,
    FactCheck,
    cn_number,
    fold_quote,
    parse_occurred,
    sentence_times,
    summarize_checks,
    verify_window,
)
from javmem.pipeline.window import Window, build_windows
from javmem.redaction import default_patterns

PATTERNS = default_patterns()


def window_of(*lines: str, heading: str = "## 原话 · 2026-11-08 10:30–10:40") -> tuple[Window, RenderedWindow]:
    """一个对话包：lines 每个是一句话，写成 `说话人：原文`，从 10:30 起每句隔一分钟。"""
    rows = []
    for index, line in enumerate(lines):
        if line.startswith("  "):  # 续行、引用行原样接上
            rows.append(line)
            continue
        rows.append(f"- [10:{30 + index:02d}] {line}")
    raw = build_packet(body=heading + "\n" + "\n".join(rows) + "\n")
    packet = normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))
    window = build_windows([packet])[0]
    return window, render_window(window, PATTERNS)


def fact(text: str, *evidence: tuple[str, str], **fields: object) -> dict[str, object]:
    item: dict[str, object] = {
        "text": text,
        "subject": None,
        "attribute": None,
        "value": None,
        "occurred_at": None,
        "time_precision": "unknown",
        "certainty": "stated",
        "entities": [],
        "evidence": [{"ref": ref, "quote": quote} for ref, quote in evidence],
    }
    item.update(fields)
    return item


def check(lines: tuple[str, ...] | list[str], item: dict[str, object], entities: list[dict] | None = None) -> FactCheck:
    window, rendered = window_of(*lines)
    extraction = map_output({"entities": entities or [], "facts": [item]}, window, rendered.redacted_refs)
    return verify_window(window, rendered, extraction).facts[0]


def codes(result: FactCheck) -> list[str]:
    return [finding.as_text() for finding in result.failures]


# ---------- 比对的写法 ----------


def test_fold_quote_ignores_spaces_width_case_markdown_and_edge_punctuation() -> None:
    assert fold_quote("  单价 **12.5** 美元。") == fold_quote("单价12.5美元") == "单价12.5美元"
    assert fold_quote("“事实”") == fold_quote('"事实"') == '事实'
    assert fold_quote("ＡＢＣ　１２") == "abc12"
    assert fold_quote("| A | B |") == "ab"
    assert fold_quote("期限 --- ---: 6M") == fold_quote("期限 --- ---: --- 6M") == "期限6m"
    assert fold_quote("。") == ""


def test_cn_number() -> None:
    assert [cn_number(text) for text in ("三", "十五", "三千五百", "两万", "二〇二六", "一亿二千万", "x")] == [
        3, 15, 3500, 20000, 2026, 120000000, None
    ]


# ---------- G1 引文在原文里 ----------


def test_g1_passes_a_verbatim_quote_and_keeps_the_raw_text() -> None:
    result = check(["我：越南那批订单〔加急〕下周出货。"], fact("越南那批订单下周出货", ("T1", "越南那批订单［加急］下周出货")))
    assert result.passed
    assert result.proofs[0].quote == "越南那批订单〔加急〕下周出货"  # 模型看到的是［］，记下的是原句里的字


def test_g1_tolerates_spaces_and_markdown_but_not_other_characters() -> None:
    lines = ["desk-bot：单价 **12.5** 美元，交期 11-20。"]
    assert check(lines, fact("单价 12.5 美元", ("T1", "单价12.5美元"))).passed
    assert codes(check(lines, fact("单价 13.5 美元", ("T1", "单价13.5美元")))) == ["G1:quote_not_found"]


def test_g1_splits_a_quote_that_runs_over_two_sentences_of_one_line() -> None:
    result = check(["我：先关掉那个闹钟。再把这条记下来。"], fact("先关闹钟再记下来", ("T1", "先关掉那个闹钟。再把这条记下来")))
    assert result.passed
    assert [proof.ref for proof in result.proofs] == ["T1", "T2"]
    assert "G1:spans_lines" in result.notes


def test_g1_points_a_wrong_ref_at_the_only_line_that_has_the_quote() -> None:
    result = check(["我：价格不变。", "desk-bot：交期推到 11-20。"], fact("交期推到 11-20", ("T1", "交期推到 11-20")))
    assert result.passed
    assert [proof.ref for proof in result.proofs] == ["T2"]
    assert result.proofs[0].speaker == "desk-bot"
    assert "G1:ref_fixed" in result.notes


def test_g1_fails_a_quote_found_in_several_lines_or_only_in_quoted_text() -> None:
    lines = ["我：好的，按这个办。", "desk-bot：好的，按这个办。", "我：价格不变。"]
    assert codes(check(lines, fact("按这个办", ("T3", "好的，按这个办")))) == ["G1:quote_ambiguous"]
    quoted = ["我：对，就这样。", "  > [引用 10:20 desk-bot] 越南那批订单下周出货"]
    assert codes(check(quoted, fact("越南那批订单下周出货", ("T1", "越南那批订单下周出货")))) == ["G1:quote_in_context"]


def test_g1_drops_a_bad_quote_but_keeps_the_fact_when_another_quote_holds() -> None:
    result = check(["我：价格不变。"], fact("价格不变", ("T1", "价格不变"), ("T1", "价格涨了")))
    assert result.passed
    assert [finding.as_text() for _, finding in result.dropped] == ["G1:quote_not_found"]


def test_g1_empty_quote_after_punctuation() -> None:
    assert codes(check(["我：价格不变。"], fact("价格不变", ("T1", "。")))) == ["G1:empty_quote"]


def test_problems_from_l3_are_quarantined_directly() -> None:
    window, rendered = window_of("我：价格不变。")
    extraction = map_output(
        {"entities": [], "facts": [fact("价格不变", ("T9", "价格不变")), fact("价格不变", ("T1", "价格不变"))]},
        window,
        rendered.redacted_refs,
    )
    first, second = verify_window(window, rendered, extraction).facts
    assert codes(first) == ["L3:no_usable_evidence", "L3:unknown_ref"]
    assert second.passed


def test_g1_redacted_line_keeps_the_mask_not_the_secret() -> None:
    result = check(
        ["我：新邮箱的密码是 Xq7#Lm2pRz9。"],
        fact("新邮箱的密码已改", ("T1", "新邮箱的密码是 [已隐去:密码]")),
    )
    assert result.passed
    assert result.proofs[0].redacted
    assert "Xq7#Lm2pRz9" not in result.proofs[0].quote


# ---------- G2 数字、币种 ----------


def test_g2_fails_a_number_that_is_nowhere_in_the_window() -> None:
    assert codes(check(["desk-bot：单价 12.5 美元。"], fact("单价 13.5 美元", ("T1", "单价 12.5 美元")))) == [
        "G2:number_mismatch"
    ]


@pytest.mark.parametrize(
    ("source", "claim"),
    [
        ("我转了 3200万日元。", "主人转了 32,000,000 日元"),  # 万和千分位是一个数
        ("仅 ≤阈值×0.85 才提醒。", "跌到阈值的 85% 以下才提醒"),  # 0.85 和 85%
        ("三个人去开会。", "3 个人去开会"),  # 中文写的数
        ("七八月份到期的那几张续上。", "7、8 月到期的那几张续上"),
        ("单价 45.0。", "单价 45"),
    ],
)
def test_g2_same_number_written_differently(source: str, claim: str) -> None:
    result = check([f"我：{source}"], fact(claim, ("T1", source.rstrip("。"))))
    assert "G2:number_mismatch" not in codes(result), codes(result)


def test_g2_number_from_another_line_of_the_window_passes_with_a_note() -> None:
    result = check(["desk-bot：账户 A 的余额是 8,000。", "我：那就转过去吧。"], fact("主人同意把账户 A 的 8,000 转过去", ("T2", "那就转过去吧")))
    assert result.passed
    assert "G2:number_elsewhere" in result.notes


def test_g2_currency_added_without_any_source() -> None:
    assert codes(check(["desk-bot：可用额度 50 万。"], fact("可用额度 50 万美元", ("T1", "可用额度 50 万")))) == [
        "G2:currency_added"
    ]
    elsewhere = check(
        ["desk-bot：这个账户是美元账户。", "desk-bot：可用额度 50 万。"],
        fact("可用额度 50 万美元", ("T2", "可用额度 50 万")),
    )
    assert elsewhere.passed and "G2:currency_elsewhere" in elsewhere.notes


def test_g2_does_not_count_dates_as_numbers() -> None:
    result = check(["我：明天发货。"], fact("2026-11-09 发货", ("T1", "明天发货")))
    assert result.passed


# ---------- G3 否定 ----------


def test_g3_negation_dropped() -> None:
    lines = ["我：老周不负责越南那批订单。"]
    assert codes(check(lines, fact("老周负责越南那批订单", ("T1", "老周不负责越南那批订单")))) == ["G3:negation_dropped"]
    assert check(lines, fact("老周不负责越南那批订单", ("T1", "老周不负责越南那批订单"))).passed


@pytest.mark.parametrize(
    ("source", "claim"),
    [
        ("这周都是空跑、无新增。", "这周都是空跑"),  # 只是没写进来，没有说反
        ("收盘≥参考价按 1 份算。", "收盘不低于参考价时按 1 份算"),
        ("更正：是王小明，不是王晓明。", "负责人是王小明"),
        ("还没收到合同。", "尚未收到合同"),  # “没收”不是这里的意思
        ("旅行险（日本除外），保两人。", "旅行险不含日本，保两人"),
        ("仍待发票：付款 12/26。", "尚未开票，预计 12/26 付款"),
    ],
)
def test_g3_passes(source: str, claim: str) -> None:
    result = check([f"desk-bot：{source}"], fact(claim, ("T1", source.rstrip("。"))))
    assert not [code for code in codes(result) if code.startswith("G3")], codes(result)


def test_g3_negation_added() -> None:
    assert codes(check(["desk-bot：订单已发货。"], fact("订单未发货", ("T1", "订单已发货")))) == ["G3:negation_added"]
    elsewhere = check(
        ["desk-bot：这一版不写附录。", "我：好，就按这个发。"],
        fact("主人同意发出，这一版不写附录", ("T2", "好，就按这个发")),
    )
    assert elsewhere.passed and "G3:negation_elsewhere" in elsewhere.notes


# ---------- G4 条件 ----------


def test_g4_condition_dropped() -> None:
    lines = ["我：如果下周没到货，就取消订单。"]
    assert codes(check(lines, fact("主人要取消订单", ("T1", "如果下周没到货，就取消订单")))) == ["G4:condition_dropped"]
    assert check(lines, fact("如果下周没到货，主人要取消订单", ("T1", "如果下周没到货，就取消订单"))).passed


@pytest.mark.parametrize(
    ("source", "claim"),
    [
        ("若通知来自总部，文末写标注。", "来自总部的通知，文末写标注"),  # 条件写成了限定
        ("拖后腿的主要是 B 线。", "B 线是主要拖后腿的"),  # “主要是”不是“要是”
        ("出勤 90% 是底线，否则不发奖金。", "出勤达不到 90% 就不发奖金"),
        ("只要蓝色和原来的字体一样。", "页面只用蓝色，字体和原来一样"),
    ],
)
def test_g4_passes(source: str, claim: str) -> None:
    result = check([f"我：{source}"], fact(claim, ("T1", source.rstrip("。"))))
    assert "G4:condition_dropped" not in codes(result), codes(result)


# ---------- G5 语气 ----------


def test_g5_modality_upgrade() -> None:
    lines = ["desk-bot：可能下周发货。"]
    assert codes(check(lines, fact("下周发货", ("T1", "可能下周发货")))) == ["G5:modality_upgrade"]
    assert check(lines, fact("可能下周发货", ("T1", "可能下周发货"))).passed
    vague = check(lines, fact("下周发货", ("T1", "可能下周发货"), certainty="vague"))
    assert vague.passed and vague.status == INFERRED


def test_g5_question_as_fact_but_not_a_request() -> None:
    assert codes(check(["我：越南那批订单发了吗？"], fact("越南那批订单已发", ("T1", "越南那批订单发了吗")))) == [
        "G5:question_as_fact"
    ]
    request = check(["我：可以帮我把上个月的账先结了吗？"], fact("主人要求先结上个月的账", ("T1", "可以帮我把上个月的账先结了吗")))
    assert request.passed


# ---------- G6 时间 ----------


def test_g6_text_date_must_match_the_evidence() -> None:
    lines = ["我：下周一发货。"]  # 2026-11-08 是星期天，下周一是 11-09
    assert check(lines, fact("2026-11-09 发货", ("T1", "下周一发货"))).passed
    assert codes(check(lines, fact("2026-11-20 发货", ("T1", "下周一发货")))) == ["G6:time_unsupported"]
    assert check(lines, fact("主人在 2026-11-08 说下周一发货", ("T1", "下周一发货"))).passed  # 说话那天总是算的


def test_g6_text_date_from_another_line_passes_with_a_note() -> None:
    result = check(["desk-bot：以下是 10/25 的盘点。", "desk-bot：仓库 A 有 120 箱。"], fact("仓库 A 在 2026-10-25 有 120 箱", ("T2", "仓库 A 有 120 箱")))
    assert result.passed and "G6:time_elsewhere" in result.notes


def test_g6_occurred_at_is_lowered_or_dropped_but_the_fact_stays() -> None:
    lines = ["我：刚下单了。"]
    exact = check(lines, fact("主人下单", ("T1", "刚下单了"), occurred_at="2026-11-08T10:30+08:00", time_precision="minute"))
    assert (exact.occurred_at, exact.time_precision, exact.notes) == ("2026-11-08T10:30+08:00", "minute", [])
    lowered = check(lines, fact("主人下单", ("T1", "刚下单了"), occurred_at="2026-11-08T09:00+08:00", time_precision="minute"))
    assert lowered.passed and (lowered.occurred_at, lowered.time_precision) == ("2026-11-08", "day")
    assert "G6:precision_lowered" in lowered.notes
    dropped = check(lines, fact("主人下单", ("T1", "刚下单了"), occurred_at="2025-03-01", time_precision="day"))
    assert dropped.passed and (dropped.occurred_at, dropped.time_precision) == (None, "unknown")
    assert "G6:time_dropped" in dropped.notes


def test_g6_explicit_date_wins_over_the_day_it_was_said() -> None:
    lines = ["desk-bot：10/24 那批已于 2026-10-28 全部到港。"]
    result = check(lines, fact("那批已全部到港", ("T1", "10/24 那批已于 2026-10-28 全部到港"), occurred_at="2026-11-08"))
    assert (result.occurred_at, result.time_precision) == ("2026", "year")  # 说话那天不算，只对得上年份
    assert check(lines, fact("那批已到港", ("T1", "10/24 那批已于 2026-10-28 全部到港"), occurred_at="2026-10-28")).occurred_at == "2026-10-28"


def test_sentence_times_month_only_and_said_day() -> None:
    said = datetime(2026, 11, 8, 10, 30, tzinfo=SHANGHAI)
    assert (2026, 10) in sentence_times("报价单 Oct 再更新", said).months
    assert (2026, 5) not in sentence_times("You may proceed", said).months
    plain = sentence_times("刚下单了", said)
    assert plain.days == {date(2026, 11, 8)} and (date(2026, 11, 8), 10, 30) in plain.minutes


def test_parse_occurred() -> None:
    assert parse_occurred("2026-Q4").at("quarter") == "2026-Q4"
    assert parse_occurred("2026-11-05T10:30+08:00").at("minute") == "2026-11-05T10:30+08:00"
    assert parse_occurred("2026-13-01") is None
    assert parse_occurred("下周") is None


# ---------- G7 改写 ----------


def test_g7_fails_content_that_is_not_in_the_window() -> None:
    lines = ["我：价格不变。"]
    assert codes(check(lines, fact("董事会批准了收购计划", ("T1", "价格不变")))) == ["G7:paraphrase_unsupported"]
    assert check(lines, fact("主人说价格不变", ("T1", "价格不变"))).passed


def test_g7_empty_text_is_quarantined() -> None:
    assert codes(check(["我：价格不变。"], fact("   ", ("T1", "价格不变")))) == ["G7:empty_text"]


def test_g7_names_and_speakers_from_the_window_count() -> None:
    entities = [{"key": "e1", "mention": "他", "name": "Tran Mai", "type": "人"}]
    lines = ["desk-bot：Tran Mai 是越南那家的对接人。", "我：他同意了。"]
    result = check(lines, fact("Tran Mai 同意了", ("T2", "他同意了"), subject="e1"), entities)
    assert result.passed


# ---------- 一个窗口 ----------


def test_status_from_certainty_and_entities_with_problems() -> None:
    window, rendered = window_of("我：价格不变。")
    entities = [{"key": "e1", "mention": "我", "name": "主人", "type": "人"}] * 2
    extraction = map_output(
        {
            "entities": entities,
            "facts": [
                fact("价格不变", ("T1", "价格不变")),
                fact("价格不变", ("T1", "价格不变"), certainty="inferred"),
            ],
        },
        window,
        rendered.redacted_refs,
    )
    check_result = verify_window(window, rendered, extraction)
    assert [result.status for result in check_result.facts] == [VERIFIED, INFERRED]
    assert len(check_result.entities) == 1
    assert [finding.as_text() for _, finding in check_result.bad_entities] == ["L3:duplicate_key"]


def test_verify_command_counts_without_copying_text(world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.intake()
    assert main(["verify", "--library", "prod", "--fake"]) == 2  # 还没抽：结果库都没有
    assert "没有这个 L3 结果库" in capsys.readouterr().err
    assert main(["extract", "--library", "prod", "--fake", "--packet", CHAT_ID]) == 0
    assert main(["verify", "--library", "prod", "--fake"]) == 1  # 邮件包那个窗口还没结果
    assert "没结果的 1" in capsys.readouterr().out
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    report = tmp_path / "verify.json"
    assert main(["verify", "--library", "prod", "--fake", "--report", str(report)]) == 0
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert (summary["windows"], summary["windows_without_result"]) == (2, 0)
    assert summary["facts"] == summary["passed"] + summary["quarantined"] > 0
    assert "越南" not in report.read_text(encoding="utf-8")


def test_summary_counts_each_rule_and_the_first_rule() -> None:
    window, rendered = window_of("desk-bot：单价 12.5 美元。", "我：订单已发货。")
    extraction = map_output(
        {
            "entities": [],
            "facts": [
                fact("单价 13.5 美元", ("T1", "单价 12.5 美元")),
                fact("订单未发货，单价 99", ("T2", "订单已发货")),
                fact("订单已发货", ("T2", "订单已发货")),
            ],
        },
        window,
        rendered.redacted_refs,
    )
    summary = summarize_checks([verify_window(window, rendered, extraction)])
    assert summary["l4_version"] == L4_VERSION
    assert (summary["facts"], summary["passed"], summary["quarantined"]) == (3, 1, 2)
    assert summary["by_rule"] == {"G2": 2, "G3": 1}
    assert summary["by_first_rule"] == {"G2": 2}
