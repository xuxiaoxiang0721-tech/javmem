"""L4 审阅（J2.8）找出来的问题，一条一个测试。例子都是编的。"""

from __future__ import annotations

import time
from datetime import date, datetime

import pytest
from helpers import SHANGHAI, World, build_packet
from test_verify import check, codes, fact, window_of

from javmem.cli import main
from javmem.pipeline.extract import map_output
from javmem.pipeline.verify import L4_VERSION, cn_number, fold_quote, sentence_times, verify_window

SAID = datetime(2026, 11, 8, 10, 30, tzinfo=SHANGHAI)


def failed(lines: list[str], text: str, quote: str, **fields: object) -> list[str]:
    return codes(check(lines, fact(text, ("T1", quote), **fields)))


# ---------- 1. 英文 ----------


@pytest.mark.parametrize(
    ("source", "claim", "expected"),
    [
        ("We will not pay the invoice this month.", "We will pay the invoice this month", "G3:negation_dropped"),
        ("They cannot ship before Friday.", "They can ship before Friday", "G3:negation_dropped"),
        ("We might ship the order next week if the deposit arrives.", "They will ship the order next week", "G4:condition_dropped"),
        ("We might ship the order next week if the deposit arrives.", "They will ship the order next week", "G5:modality_upgrade"),
        ("We might ship the order next week if the deposit arrives.", "Tran Mai cancelled the whole contract", "G7:paraphrase_unsupported"),
    ],
)
def test_english_rules_work(source: str, claim: str, expected: str) -> None:
    assert expected in failed([f"desk-bot：{source}"], claim, source.rstrip(".")), claim


def test_english_faithful_facts_pass() -> None:
    assert failed(["desk-bot：We will not pay the invoice this month."], "We will not pay the invoice this month",
                  "We will not pay the invoice this month") == []
    assert failed(["desk-bot：Sales fell 10% this quarter."], "Sales decrease 10% this quarter", "Sales fell 10% this quarter") == []


# ---------- 2. 相对的年、月和没写年月的日期合起来 ----------


@pytest.mark.parametrize(
    ("source", "good", "bad"),
    [
        ("下个月 3 号交货。", "2026-12-03 交货", "2026-11-03 交货"),
        ("今年3月签的合同。", "2026年3月签的合同", "2027年3月签的合同"),
        ("今年 3 月签的合同。", "2026年3月签的合同", "2027年3月签的合同"),
        ("明年11月交货。", "2027年11月交货", "2026年11月交货"),
        ("明年年底交付。", "2027 年 12 月交付", "2026 年 12 月交付"),
    ],
)
def test_relative_year_and_month_anchor_partial_dates(source: str, good: str, bad: str) -> None:
    lines = [f"desk-bot：{source}"]
    assert failed(lines, good, source.rstrip("。")) == [], good
    assert failed(lines, bad, source.rstrip("。")) == ["G6:time_unsupported"], bad


def test_occurred_at_keeps_the_anchored_month() -> None:
    result = check(["desk-bot：明年11月交货。"], fact("明年 11 月交货", ("T1", "明年11月交货"), occurred_at="2027-11", time_precision="month"))
    assert (result.occurred_at, result.time_precision) == ("2027-11", "month")


# ---------- 3. 事实文字里的 11-20、20号、Q4、第三季度、15:00 ----------


@pytest.mark.parametrize(
    ("source", "claim", "expected"),
    [
        ("交期推到 11-20。", "交期推到 11-25", "G2:number_mismatch"),
        ("交期推到 11/20。", "交期推到 11/25", "G2:number_mismatch"),
        ("20号交货。", "25号交货", "G2:number_mismatch"),
        ("Q4 上线。", "Q3 上线", "G2:number_mismatch"),
        ("第四季度上线。", "第三季度上线", "G2:number_mismatch"),
        ("明天 10:30 开会。", "主人明天 15:00 开会", "G6:time_unsupported"),
        ("明天 10:30 开会。", "主人 2026-11-09 15:00 开会", "G6:time_unsupported"),
        ("比分 2-1。", "比分 3-1", "G2:number_mismatch"),
        ("会议在 6号楼。", "会议在 5号楼", "G2:number_mismatch"),
    ],
)
def test_dates_written_like_numbers_are_checked(source: str, claim: str, expected: str) -> None:
    assert expected in failed([f"我：{source}"], claim, source.rstrip("。")), claim


def test_the_same_numbers_and_times_pass() -> None:
    assert failed(["我：交期推到 11-20。"], "交期推到 11-20", "交期推到 11-20") == []
    assert failed(["我：明天 10:30 开会。"], "主人 2026-11-09 10:30 开会", "明天 10:30 开会") == []
    assert failed(["我：第四季度上线。"], "第四季度上线", "第四季度上线") == []


# ---------- 4. “不同意”“不同步”“主要不是” ----------


@pytest.mark.parametrize(
    ("source", "claim", "expected"),
    [
        ("我不同意这个方案。", "主人同意这个方案", "G3:negation_dropped"),
        ("我同意这个方案。", "主人不同意这个方案", "G3:negation_added"),
        ("两边的数据不同步。", "两边的数据同步", "G3:negation_dropped"),
        ("主要不是价格问题。", "主要是价格问题", "G3:negation_dropped"),
    ],
)
def test_real_negations_inside_words_that_look_harmless(source: str, claim: str, expected: str) -> None:
    assert expected in failed([f"我：{source}"], claim, source.rstrip("。")), claim


def test_harmless_words_still_pass() -> None:
    assert failed(["我：两个方案不同，各有好处。"], "两个方案不同", "两个方案不同") == []
    assert failed(["我：要不然就下周再说。"], "主人说下周再说", "要不然就下周再说") == []


# ---------- 5. 只引了半句 ----------


@pytest.mark.parametrize(
    ("source", "quote", "claim", "expected"),
    [
        ("老周不负责越南那批订单。", "负责越南那批订单", "老周负责越南那批订单", "G3:negation_dropped"),
        ("如果下周没到货，就取消订单。", "就取消订单", "主人要取消订单", "G4:condition_dropped"),
        ("可能下周发货。", "下周发货", "下周发货", "G5:modality_upgrade"),
        ("越南那批订单发了吗？", "越南那批订单发了", "越南那批订单已发", "G5:question_as_fact"),
    ],
)
def test_rules_look_at_the_whole_clause_not_just_the_quote(source: str, quote: str, claim: str, expected: str) -> None:
    assert expected in failed([f"我：{source}"], claim, quote), claim


# ---------- 6. 单位、数量级、正负号、中文数字 ----------


@pytest.mark.parametrize(
    ("source", "claim"),
    [
        ("预算 3 千。", "预算 3 万"),
        ("转了 3200 美元。", "转了 3200万美元"),
        ("Budget is 5 million USD.", "Budget is 5 billion USD"),
        ("利润 5%。", "利润 -5%"),
        ("三个人去开会。", "两个人去开会"),
        ("预算 3 万。", "预算五万"),
    ],
)
def test_units_signs_and_chinese_numbers(source: str, claim: str) -> None:
    assert "G2:number_mismatch" in failed([f"我：{source}"], claim, source.rstrip("。.")), claim


def test_weekday_changed_in_the_fact() -> None:
    assert failed(["我：下周一发货。"], "下周三发货", "下周一发货") == ["G6:time_unsupported"]
    assert failed(["我：下周一发货。"], "下周一发货", "下周一发货") == []


def test_chinese_words_with_numerals_are_not_numbers() -> None:
    assert failed(["我：价格高了。"], "价格高了一点", "价格高了") == []
    assert failed(["我：大家一起去。"], "大家一起去", "大家一起去") == []


# ---------- 7. “年”后面有空格 ----------


def test_year_with_spaces_is_not_read_twice() -> None:
    lines = ["desk-bot：越南那批订单 2026 年 11 月 9 日发货，数量和上次一样。"]
    assert failed(lines, "越南那批订单 2025 年 11 月 9 日发货", "越南那批订单 2026 年 11 月 9 日发货") == ["G6:time_unsupported"]
    assert failed(lines, "越南那批订单 2026 年 11 月 9 日发货", "越南那批订单 2026 年 11 月 9 日发货") == []
    assert failed(["我：明天发货。"], "2030 年 11 月发货", "明天发货") == ["G6:time_unsupported"]


# ---------- 8、9. 否定的两道总闸、谓语 ----------


@pytest.mark.parametrize(
    ("source", "quote", "claim", "expected"),
    [
        ("我不去接待客户。", "我不去接待客户", "主人去接待客户", "G3:negation_dropped"),
        ("老周不负责越南那批订单，月底才发货。", "老周不负责越南那批订单，月底才发货", "老周负责越南那批订单，月底才发货", "G3:negation_dropped"),
        ("订单已发货，没有问题。", "订单已发货", "订单未发货", "G3:negation_added"),
        ("订单已发货，明天才到。", "订单已发货", "订单未发货", "G3:negation_added"),
        ("老周不负责越南那批订单。", "老周不负责越南那批订单", "越南那批订单由老周负责", "G3:negation_dropped"),
        ("老周说：不去。", "老周说：不去", "老周说去", "G3:negation_dropped"),
    ],
)
def test_negation_gates_and_predicates(source: str, quote: str, claim: str, expected: str) -> None:
    assert expected in failed([f"我：{source}"], claim, quote), claim


# ---------- 10. 条件 ----------


@pytest.mark.parametrize(
    ("source", "claim"),
    [
        ("如果下周没到货，就取消订单。", "主人要取消订单，然后重新下单"),
        ("如果下周没到货，就取消订单。", "主人要及时取消订单"),
        ("如果下周没到货就取消订单。", "主人要取消订单"),
        ("若是下周没到货，就取消订单。", "主人要取消订单"),
        ("只要发票齐全，就能报销。", "主人说能报销"),
    ],
)
def test_condition_is_harder_to_lose(source: str, claim: str) -> None:
    assert "G4:condition_dropped" in failed([f"我：{source}"], claim, source.rstrip("。")), claim


def test_conditions_that_are_not_conditions() -> None:
    assert failed(["我：这周必须交，否则罚款。"], "这周必须交", "这周必须交，否则罚款") == []  # “否则”不当条件
    assert failed(["我：只要蓝色，和原来的字体一样。"], "页面只用蓝色", "只要蓝色，和原来的字体一样") == []  # 只要 = 只想要
    assert failed(["我：主要是 B 线拖后腿，就砍掉 B 线。"], "主人要砍掉 B 线", "主要是 B 线拖后腿，就砍掉 B 线") == []
    assert "G4:condition_dropped" in failed(
        ["我：要是 B 线拖后腿，就砍掉 B 线。"], "主人要砍掉 B 线", "要是 B 线拖后腿，就砍掉 B 线"
    )


# ---------- 11. 实体的名字要在窗口里看得到 ----------


@pytest.mark.parametrize(
    ("name", "claim", "expected"),
    [
        ("500 箱货", "500 箱货到了", "G2:number_mismatch"),
        ("美元那批订单", "那批订单用美元结算", "G2:currency_added"),
        ("未付款的那批订单", "那批订单未付款", "G3:negation_added"),
    ],
)
def test_entity_names_not_in_the_window_do_not_count(name: str, claim: str, expected: str) -> None:
    entities = [{"key": "e1", "mention": name, "name": name, "type": "订单或合同"}]
    result = check(["我：那批订单到了。"], fact(claim, ("T1", "那批订单到了"), subject="e1", entities=[{"key": "e1", "role": "subject"}]), entities)
    assert expected in codes(result), codes(result)


# ---------- 12、13. 误拦：别名、莫总、呢、嘛；请问 ----------


def test_words_that_are_not_negations_or_questions() -> None:
    assert failed(["我：Tran Mai 也叫小陈。"], "Tran Mai 的别名是小陈", "Tran Mai 也叫小陈") == []
    assert failed(["我：总经理同意了报价。"], "莫总同意了报价", "总经理同意了报价") == []
    assert failed(["desk-bot：货还在路上呢。"], "货还在路上", "货还在路上呢") == []
    assert failed(["我：就按原来的价格嘛。"], "主人要按原来的价格", "就按原来的价格嘛") == []
    assert failed(["我：请问越南那批订单发了吗？"], "越南那批订单已发", "请问越南那批订单发了吗") == ["G5:question_as_fact"]


# ---------- 14. G2 的误拦 ----------


@pytest.mark.parametrize(
    ("source", "claim"),
    [
        ("单价 5 美元。", "单价 US$5"),
        ("融资 3千万。", "融资 30,000,000"),
        ("|数量|12|5|", "数量是 12"),
        ("运费 500 元。", "运费 500 元人民币"),
    ],
)
def test_numbers_and_currencies_written_differently_pass(source: str, claim: str) -> None:
    assert failed([f"desk-bot：{source}"], claim, source.rstrip("。")) == [], claim


# ---------- 15. 英文单词不是月份 ----------


def test_english_words_starting_like_months_are_not_dates() -> None:
    for text in ("decrease 10%", "marketing 2", "junior 3", "novel 2"):
        times = sentence_times(text, SAID)
        assert times.days == {date(2026, 11, 8)}, text  # 只有说话那天


# ---------- 16、17. 不让整批停下，也不慢 ----------


@pytest.mark.parametrize("occurred", ["9999-12-31T23:59Z", "9999-12-31T23:59-12:00", "0001-01-01T00:00+14:00"])
def test_occurred_at_near_the_ends_of_the_calendar(occurred: str) -> None:
    result = check(["我：刚下单了。"], fact("主人下单", ("T1", "刚下单了"), occurred_at=occurred, time_precision="minute"))
    assert result.passed and result.occurred_at is None


def test_odd_text_does_not_raise() -> None:
    assert sentence_times("tonıght 见", SAID) is not None
    assert sentence_times("明天见", datetime(9999, 12, 31, 10, 0, tzinfo=SHANGHAI)) is not None
    assert cn_number("一" * 5000) is None
    check(["我：价格不变。"], fact("价格 " + "9" * 400 + "万", ("T1", "价格不变")))  # 太长的不当数比，不出错就行


def test_long_runs_of_spaces_are_fast() -> None:
    started = time.monotonic()
    sentence_times("上" + " " * 20000 + "好", SAID)
    assert time.monotonic() - started < 1.0


# ---------- 18、19. 存下来的引文、拼起来的几句 ----------


def test_stored_quote_keeps_its_brackets() -> None:
    assert check(["我：旅行险（日本除外）。"], fact("旅行险不含日本", ("T1", "旅行险（日本除外）"))).proofs[0].quote == "旅行险（日本除外）"
    assert check(["我：〔加急〕越南那批订单下周出货。"], fact("越南那批订单下周出货", ("T1", "［加急］越南那批订单下周出货"))).proofs[0].quote == "〔加急〕越南那批订单下周出货"
    secret = check(["我：新邮箱的密码是 Xq7#Lm2pRz9。"], fact("新邮箱的密码改了", ("T1", "新邮箱的密码是 [已隐去:密码]")))
    assert secret.proofs[0].quote == "新邮箱的密码是 [已隐去:密码]"
    assert fold_quote("-5") == "-5"
    assert codes(check(["我：一共 15 箱。"], fact("一共 -5 箱", ("T1", "-5")))) == ["G1:quote_not_found"]


def test_joined_quote_that_misses_the_cited_line_is_a_fixed_ref() -> None:
    result = check(["我：价格不变。交期推到下周。数量减半。"], fact("交期推到下周，数量减半", ("T1", "交期推到下周。数量减半")))
    assert [proof.ref for proof in result.proofs] == ["T2", "T3"]
    assert "G1:ref_fixed" in result.notes


# ---------- 20. G6 的几处 ----------


def test_time_zone_is_converted_before_lowering_the_precision() -> None:
    result = check(["我：刚下单了。"], fact("主人下单", ("T1", "刚下单了"), occurred_at="2026-11-07T19:30-05:00", time_precision="minute"))
    assert (result.occurred_at, result.time_precision) == ("2026-11-08", "day")


def test_impossible_dates_in_the_fact_are_unsupported() -> None:
    assert failed(["我：月底交货。"], "2026年2月30日交货", "月底交货") == ["G6:time_unsupported"]


# ---------- 21. 怪类型 ----------


def test_odd_shapes_from_old_results_do_not_raise() -> None:
    window, rendered = window_of("我：None of them came.")
    weird = {
        "entities": [{"mention": "x", "name": "x", "type": "人"}],
        "facts": [
            {"text": "None of them came", "evidence": [{"ref": "T1", "quote": None}], "certainty": "stated"},
            {"text": "None of them came", "evidence": "T1", "certainty": "stated"},
            {"text": "None of them came", "evidence": 3, "certainty": "stated"},
            {"text": "None of them came", "evidence": [], "problems": "unknown_ref"},
        ],
    }
    result = verify_window(window, rendered, weird)
    assert [item.passed for item in result.facts] == [False, False, False, False]
    assert codes(result.facts[0]) == ["G1:empty_quote"]
    assert codes(result.facts[3]) == ["L3:unknown_ref"]
    assert len(result.bad_entities) == 1


# ---------- 23. verify 不新建结果库 ----------


def test_verify_with_a_wrong_store_is_a_config_error(world: World, tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    missing = tmp_path / "no-such-store"
    assert main(["verify", "--library", "prod", "--fake", "--store", str(missing)]) == 2
    assert not missing.exists()


# ---------- 24. 每种日期写法各自认得出来 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("门诊记录写的是 15.07.2026", date(2026, 7, 15)),
        ("Start 17-Sep-26 / End 01-Oct-26", date(2026, 10, 1)),
        ("8-20：你随即订了 40 张", date(2026, 8, 20)),
        ("两次回访定在 2027-4-13 和 10-13", date(2027, 10, 13)),
        ("十一月五日开会", date(2026, 11, 5)),
        ("Nov 15 works for us", date(2026, 11, 15)),
        ("下个月 3 号", date(2026, 12, 3)),
        ("月底交", date(2026, 11, 30)),
        ("昨天到的", date(2026, 11, 7)),
    ],
)
def test_sentence_times_formats_exactly(text: str, expected: date) -> None:
    assert expected in sentence_times(text, SAID).days


def test_map_output_shapes_still_work() -> None:
    window, rendered = window_of("我：价格不变。")
    extraction = map_output({"entities": [], "facts": [fact("价格不变", ("T1", "价格不变"))]}, window, rendered.redacted_refs)
    assert verify_window(window, rendered, extraction).facts[0].passed


# ---------- 25、26. 对照和只测了一边的 ----------


def test_comparative_negation_needs_the_not() -> None:
    lines = ["我：收盘不低于参考价按 1 份算。"]
    assert failed(lines, "收盘不低于参考价时按 1 份算", "收盘不低于参考价按 1 份算") == []
    assert "G3:negation_dropped" in failed(lines, "收盘低于参考价时按 1 份算", "收盘不低于参考价按 1 份算")


def test_cited_line_wins_over_other_lines_with_the_same_quote() -> None:
    result = check(["我：好的，按这个办。", "desk-bot：好的，按这个办。"], fact("按这个办", ("T2", "好的，按这个办")))
    assert result.passed and [proof.ref for proof in result.proofs] == ["T2"] and result.notes == []


def test_a_quote_running_into_quoted_text_is_not_split() -> None:
    lines = ["我：对，就这样。", "  > [引用 10:20 desk-bot] 越南那批订单下周出货"]
    assert codes(check(lines, fact("就这样", ("T1", "对，就这样。越南那批订单下周出货")))) == ["G1:quote_not_found"]


def test_precision_written_wrong_is_fixed() -> None:
    result = check(["我：刚下单了。"], fact("主人下单", ("T1", "刚下单了"), occurred_at="2026-11-08", time_precision="minute"))
    assert (result.occurred_at, result.time_precision) == ("2026-11-08", "day") and "G6:precision_fixed" in result.notes


def test_verify_command_report_has_only_counts(world: World, tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from helpers import CHAT_PACKET

    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    report = tmp_path / "verify.json"
    assert main(["verify", "--library", "prod", "--fake", "--report", str(report)]) == 0
    summary = json.loads(report.read_text(encoding="utf-8"))

    def leaves(node: object) -> list[object]:
        if isinstance(node, dict):
            return [leaf for value in node.values() for leaf in leaves(value)]
        if isinstance(node, list):
            return [leaf for value in node for leaf in leaves(value)]
        return [node]

    texts = [leaf for leaf in leaves(summary) if isinstance(leaf, str)]
    assert texts == [L4_VERSION]  # 除了规则版本，只有数


# ========== 复看（第二轮）找出来的 ==========


# ---------- 新 1. 写在后面的条件管前面那一段 ----------


@pytest.mark.parametrize(
    ("source", "claim", "quote"),
    [
        ("我：周五发货，除非定金没到。", "主人周五发货", "周五发货，除非定金没到"),
        ("我：周五发货，除非定金没到。", "主人周五发货", "周五发货"),
        ("我：可以降价，前提是订量过千。", "主人同意降价", "可以降价，前提是订量过千"),
        ("desk-bot：We will ship next week, if the deposit arrives.", "They will ship next week", "We will ship next week, if the deposit arrives"),
        ("desk-bot：We will ship on Friday, unless you cancel.", "They will ship on Friday", "We will ship on Friday"),
    ],
)
def test_trailing_conditions_govern_the_clause_before_them(source: str, claim: str, quote: str) -> None:
    assert "G4:condition_dropped" in failed([source], claim, quote), claim


def test_a_trailing_ruo_starts_another_thing() -> None:
    assert failed(["我：周五发货，若有变化再通知。"], "主人周五发货", "周五发货") == []
    assert failed(["我：周五发货，除非定金没到。"], "主人周五发货，除非定金没到", "周五发货，除非定金没到") == []


# ---------- 新 2. 一小句里几件事各有否定，一件一件查 ----------


@pytest.mark.parametrize(
    ("source", "claim", "quote"),
    [
        ("我：我们不发货也不退款。", "主人会发货也会退款", "我们不发货也不退款"),
        ("desk-bot：这批货不能打折也不能退货。", "这批货能打折也能退货", "这批货不能打折也不能退货"),
        ("desk-bot：We will not ship and will not refund.", "We will ship and will refund", "We will not ship and will not refund"),
    ],
)
def test_two_negations_joined_by_ye_or_and_are_checked_one_by_one(source: str, claim: str, quote: str) -> None:
    assert "G3:negation_dropped" in failed([source], claim, quote), claim


def test_two_negations_kept_pass() -> None:
    assert failed(["我：我们不发货也不退款。"], "主人不发货也不退款", "我们不发货也不退款") == []
    assert failed(["我：没有发票的报销不算。"], "没有发票的报销不算数", "没有发票的报销不算") == []


# ---------- 新 3、旧 2①. 每个日期用离它最近的那个相对的年月 ----------


@pytest.mark.parametrize(
    ("source", "good", "quote"),
    [
        ("我：这个月 20 号付首款，下个月 20 号付尾款。", "尾款 2026-12-20 付", "下个月 20 号付尾款"),
        ("desk-bot：今年 3 月签约，明年 3 月交付。", "2027年3月交付", "明年 3 月交付"),
        ("desk-bot：上个月 25 号到的货，下个月 5 号付款。", "2026-12-05 付款", "下个月 5 号付款"),
    ],
)
def test_each_date_uses_the_nearest_relative_month_or_year(source: str, good: str, quote: str) -> None:
    assert failed([source], good, quote) == [], good


def test_relative_month_right_before_the_day() -> None:
    assert failed(["desk-bot：下个月3号交货。"], "2026-12-03 交货", "下个月3号交货") == []
    assert failed(["desk-bot：下个月3号交货。"], "2026-11-03 交货", "下个月3号交货") == ["G6:time_unsupported"]
    assert failed(["desk-bot：上个月 25 号到的货，下个月 5 号付款。"], "2026-10-05 付款", "下个月 5 号付款") == ["G6:time_unsupported"]


# ---------- 新 4. 时间段 ----------


def test_time_ranges() -> None:
    assert failed(["我：明天 10:00-12:00 开会。"], "主人明天 10:00-12:00 开会", "明天 10:00-12:00 开会") == []
    assert failed(["我：仓库工作时间 9:00-18:00。"], "仓库工作时间 9:00-18:00", "仓库工作时间 9:00-18:00") == []
    assert failed(["我：明天下午 3 点到 5 点开会。"], "主人明天 15:00-17:00 开会", "明天下午 3 点到 5 点开会") == []
    assert failed(["我：明天 10:00-12:00 开会。"], "主人明天 10:00-13:00 开会", "明天 10:00-12:00 开会") == ["G6:time_unsupported"]


# ---------- 新 7. “不同部门”不是否定；“不同意”是 ----------


def test_butong_with_a_noun_is_not_negation() -> None:
    assert failed(["desk-bot：各部门都要交报告。"], "不同部门都要交报告", "各部门都要交报告") == []
    assert failed(["desk-bot：老周同意这个方案。"], "老周不同意这个方案", "老周同意这个方案") == ["G3:negation_added"]


# ---------- 新 11. 量词“块” ----------


def test_kuai_as_a_measure_word_is_not_money() -> None:
    assert failed(["我：买了 3 个硬盘。"], "主人买了 3 块硬盘", "买了 3 个硬盘") == []
    assert failed(["我：买了 3 个硬盘。"], "主人花了 3 块钱", "买了 3 个硬盘") != []


# ---------- 新 13. bot 按整词 ----------


def test_framing_bot_is_a_whole_word() -> None:
    from javmem.pipeline.verify import _units

    assert "both" in _units("both sides agreed")
    assert "bot" not in _units("the bot said both sides agreed")


# ---------- 新 15. 各种减号 ----------


def test_unicode_minus_in_the_source_matches_a_hyphen_in_the_quote() -> None:
    assert failed(["desk-bot：冷库温度 −12 度。"], "冷库温度 -12 度", "-12 度") == []


# ---------- 新 16. 每周一次 ----------


def test_zhou_yi_ci_is_not_monday() -> None:
    assert failed(["我：例会每周开一次。"], "例会每周一次", "例会每周开一次") == []
    assert failed(["我：例会两周开一次。"], "例会两周一次", "例会两周开一次") == []


# ---------- 旧 6. “三千万”是数，“千万不要”不是 ----------


def test_qianwan_in_amounts_is_a_number() -> None:
    assert failed(["desk-bot：融资三千万。"], "融资五千万", "融资三千万") == ["G2:number_mismatch"]
    assert failed(["desk-bot：融资 3000 万。"], "融资五千万", "融资 3000 万") == ["G2:number_mismatch"]
    assert failed(["desk-bot：融资三千万。"], "融资三千万", "融资三千万") == []
    assert failed(["我：千万不要发货。"], "主人说千万不要发货", "千万不要发货") == []


# ---------- 旧 12. 请求写成问句 ----------


@pytest.mark.parametrize(("source", "claim"), [("能不能先发货？", "主人要求先发货"), ("请尽快发货可以吗？", "主人要求尽快发货")])
def test_requests_phrased_as_questions_are_requests(source: str, claim: str) -> None:
    assert failed([f"我：{source}"], claim, source.rstrip("？")) == []


def test_a_plain_question_is_still_a_question() -> None:
    assert failed(["我：越南那批订单发了吗？"], "越南那批订单发了", "越南那批订单发了吗") == ["G5:question_as_fact"]


# ---------- 旧 21. problems、entities 不是列表 ----------


@pytest.mark.parametrize("field", ["problems", "entities"])
def test_odd_types_are_bad_shape_not_an_error(field: str) -> None:
    window, rendered = window_of("desk-bot：单价 12.5 美元。")
    extraction = map_output({"entities": [], "facts": [fact("单价 12.5 美元", ("T1", "单价 12.5 美元"))]}, window, rendered.redacted_refs)
    extraction["facts"][0][field] = 5  # 手写的、旧库里的结果
    assert codes(verify_window(window, rendered, extraction).facts[0]) == ["L3:bad_shape"]
