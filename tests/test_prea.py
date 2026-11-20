"""预处理 A（相对的日子换成具体的日子）：换算的规矩、不标的那些、标注写进窗口、存进结果库重放、跟着提示词、
抽之前先标、和预处理 B 一起、撤回连着删。

例子都是编的；不调任何模型（L3 用假模型，B 用假的本机模型）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import World, build_packet, file_name
from test_preb import LEXICON_YAML, FakeLocal, learn_entry, lexicon_from, save_notes

from javmem.cli import main
from javmem.llm import FakeModel
from javmem.packet import parse_packet
from javmem.packet.model import ORIGIN_EXTERNAL, ORIGIN_RELAYED
from javmem.pipeline import preb
from javmem.pipeline.extract import (
    DONE,
    PRE_A_PROMPTS,
    ExtractOptions,
    map_output,
    prepare,
    run_extraction,
    window_row,
)
from javmem.pipeline.normalize import ATTACHMENT, FORWARDED, HEADER, NEW, normalize
from javmem.pipeline.prea import (
    L4_UNSURE,
    NIGHT_END,
    PRE_A_VERSION,
    REPORTED,
    PreA,
    apply_notes,
    contents,
    find_dates,
    line_reason,
    window_notes,
)
from javmem.pipeline.preb import PRE_B_VERSION, PreB, window_context
from javmem.pipeline.render import RenderedWindow, render_window
from javmem.pipeline.store import ResultStore, strip_pre_notes
from javmem.pipeline.verify import verify_windows
from javmem.pipeline.window import Window, build_windows
from javmem.redaction import default_patterns

PATTERNS = default_patterns()
OPTIONS = ExtractOptions(sleep=lambda seconds: None, concurrency=1)
TZ = timezone(timedelta(hours=8))
SOL, LUNA = "l3-v20", "l3-v21"  # 带 A 那一句的两个提示词（l3-v16、l3-v17 加一句）


@pytest.fixture
def store(tmp_path: Path) -> ResultStore:
    with ResultStore(tmp_path / "l3") as opened:
        yield opened


def at(text: str) -> datetime:
    """“2026-11-01 10:00”，东八区。"""
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def labeled(text: str, when: str) -> list[tuple[str, str]]:
    """标上的：[(说法, 日子)]。"""
    return [(one.text, one.value) for one in find_dates(text, at(when)) if not one.skip]


def skipped(text: str, when: str) -> list[tuple[str, str]]:
    """认出来没标的：[(说法, 为什么)]。"""
    return [(one.text, one.skip) for one in find_dates(text, at(when)) if one.skip]


def chat(when: str, *lines: str, **fields: object) -> str:
    """几句话的对话包，每句都在 when 那一分钟说（东八区）。lines 写成“我：……”“desk-bot：……”。"""
    day, clock = when.split()
    end = f"{day}T{clock}:00+08:00"
    start = (datetime.fromisoformat(end) - timedelta(days=1)).isoformat()
    body = f"## 原话 · {day} {clock}–{day} {clock}\n" + "".join(f"- [{day} {clock}] {line}\n" for line in lines)
    return build_packet(stamp=f"{day}_{clock.replace(':', '')}", body=body, covers_from=start, covers_to=end, created_at=end,
                        **fields)


def windows_of(*raws: str) -> list[Window]:
    return build_windows([normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw))) for raw in raws])


# ---------- 换算 ----------


def test_days_cross_month_year_and_leap_day() -> None:
    assert labeled("昨天交割的", "2026-11-01 10:00") == [("昨天", "2026-10-31")]  # 跨月
    assert labeled("明天起息", "2026-12-31 10:00") == [("明天", "2027-01-01")]  # 跨年
    assert labeled("昨晚到账，前天下的单，大前天问的价", "2027-01-01 10:00") == [
        ("昨晚", "2026-12-31"), ("前天", "2026-12-30"), ("大前天", "2026-12-29")]
    assert labeled("后天到期，大后天付息", "2026-02-27 10:00") == [("后天", "2026-03-01"), ("大后天", "2026-03-02")]  # 平年
    assert labeled("前天买的", "2028-03-01 10:00") == [("前天", "2028-02-28")]  # 闰年：2 月有 29 号
    assert labeled("明早再看", "2028-02-28 10:00") == [("明早", "2028-02-29")]


@pytest.mark.parametrize(
    ("when", "text", "expected"),
    [
        ("2026-10-16 10:00", "下周三交割", "2026-10-21"),  # 周五说：5 天以后
        ("2026-10-18 10:00", "下周三交割", "2026-10-21"),  # 周日说：一周从周一算，周日是这一周的最后一天
        ("2026-10-19 10:00", "下周三交割", "2026-10-28"),  # 周一说：9 天以后
        ("2026-10-16 10:00", "这周三下的单", "2026-10-14"),
        ("2026-10-16 10:00", "本周日开会", "2026-10-18"),
        ("2026-10-19 10:00", "上周三看过", "2026-10-14"),
        ("2026-10-19 10:00", "上周日看过", "2026-10-18"),  # 周一说“上周日”：昨天
        ("2026-10-14 10:00", "下个星期二再说", "2026-10-20"),
        ("2026-10-14 10:00", "上礼拜天到的", "2026-10-11"),
        ("2026-12-30 10:00", "下周五付款", "2027-01-08"),  # 跨年
        ("2026-03-02 10:00", "上周六交割", "2026-02-28"),  # 跨月
    ],
)
def test_weekdays_go_by_monday_start_weeks(when: str, text: str, expected: str) -> None:
    assert [value for _, value in labeled(text, when)] == [expected]


def test_months_and_month_edges() -> None:
    assert labeled("上个月开的户", "2027-01-10 10:00") == [("上个月", "2026-12")]  # 跨年
    assert labeled("上个月底到账", "2026-10-14 10:00") == [("上个月底", "2026-09 月底")]
    assert labeled("上个月底到账", "2027-01-10 10:00") == [("上个月底", "2026-12 月底")]
    assert labeled("下个月 3 号付款", "2026-12-15 10:00") == [("下个月 3 号", "2027-01-03")]
    assert labeled("下月 3 号付款", "2026-10-14 10:00") == [("下月 3 号", "2026-11-03")]  # 下月三号、上上个月 L4 不认，见审阅那几条
    assert labeled("下个月初再看，下个月中旬到", "2026-10-14 10:00") == [("下个月初", "2026-11 月初"), ("下个月中旬", "2026-11 中旬")]
    assert labeled("这个月 20 号付首款", "2026-10-14 10:00") == [("这个月 20 号", "2026-10-20")]
    assert labeled("下个月 29 号", "2028-01-10 10:00") == [("下个月 29 号", "2028-02-29")]
    assert skipped("下个月 31 号", "2026-10-14 10:00") == [("下个月 31 号", "日子不存在")]
    assert skipped("下个月 29 号", "2027-01-10 10:00") == [("下个月 29 号", "日子不存在")]
    assert skipped("本月底到期，这个月", "2026-10-14 10:00") == [("本月底", "跟抬头同一个月"), ("这个月", "跟抬头同一个月")]


def test_years_and_quarters() -> None:
    assert labeled("去年 3 月开的户，明年 1 月 5 号到期，前年买的", "2026-10-14 10:00") == [
        ("去年 3 月", "2025-03"), ("明年 1 月 5 号", "2027-01-05"), ("前年", "2024")]
    assert labeled("去年底买的", "2027-01-01 10:00") == [("去年底", "2026 年底")]
    assert labeled("明年初再说，去年同期，后年到期", "2026-10-14 10:00") == [
        ("明年初", "2027 年初"), ("去年", "2025"), ("后年", "2028")]
    assert labeled("去年 12 月底", "2026-10-14 10:00") == [("去年 12 月底", "2025-12 月底")]
    assert skipped("今年 3 月，今年，前年度", "2026-10-14 10:00") == [
        ("今年 3 月", "跟抬头同一年"), ("今年", "跟抬头同一年"), ("前年", "前后连成了别的词")]
    # 季度算得出（detail 里留着），可是多半说的是公司的财季：不标
    found = find_dates("下个季度再看，上个季度亏了", at("2026-11-20 10:00"))
    assert [(one.text, one.value) for one in found] == [("下个季度", "2027-Q1"), ("上个季度", "2026-Q3")]
    assert {one.skip for one in found} == {"季度：多半说的是公司的财季，不一定是日历上的季度"}
    assert skipped("本季度", "2026-11-20 10:00") == [("本季度", "跟抬头同一个季度")]


def test_english() -> None:
    assert labeled("yesterday it closed; Tomorrow we buy", "2026-10-14 10:00") == [
        ("yesterday", "2026-10-13"), ("Tomorrow", "2026-10-15")]
    assert skipped("the day after tomorrow", "2026-12-31 10:00") == [("the day after tomorrow", L4_UNSURE)]  # L4 只认 tomorrow
    assert labeled("next month and last year", "2026-12-14 10:00") == [("next month", "2027-01"), ("last year", "2025")]
    assert skipped("today, this week, next week", "2026-10-14 10:00") == [
        ("today", "跟抬头同一天"), ("this week", "只说了哪一周，不是一天"), ("next week", "只说了哪一周，不是一天")]


# ---------- 不标的 ----------


def test_unsure_or_not_relative_are_not_labeled() -> None:
    text = "今天、今晚、周三、每周日、上周、上周末、周末、当天、次日、26 号、三天前、两个月后、月底、年底"
    assert labeled(text, "2026-10-14 10:00") == []
    reasons = dict(skipped(text, "2026-10-14 10:00"))
    assert reasons == {
        "今天": "跟抬头同一天",
        "今晚": "跟抬头同一天",
        "周三": "光写星期几，分不清是哪一周",
        "周日": "每周几",
        "上周": "只说了哪一周，不是一天",
        "上周末": "只说了哪一周的周末，不是一天",
        "周末": "光写周末，不是一天",
        "当天": "当天、次日这种说的是别的那一天",
        "次日": "当天、次日这种说的是别的那一天",
        "26 号": "只写了几号，不知道是哪个月",
        "三天前": "几天前、几个月后：从哪天算拿不准",
        "两个月后": "几天前、几个月后：从哪天算拿不准",
        "月底": "没写哪个月",
        "年底": "没写哪一年",
    }


@pytest.mark.parametrize(
    "text",
    ["马上周一了", "看一下周三的数据", "日本周日开盘", "成本月报", "这周一定要", "这周一直在跑", "下周一起去",
     "这周三天都在", "上周五笔交易", "下周天气", "这周日线", "说明天数", "以后天天看", "明后天到", "表明年龄",
     "减去年化一点五的利息", "税后年化收益", "除去年费", "说明年化怎么算"],
)
def test_words_that_only_look_like_dates(text: str) -> None:
    assert labeled(text, "2026-10-14 10:00") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("跟上周三比涨了", [("上周三", "2026-10-07")]),
        ("上周五股价", [("上周五", "2026-10-09")]),
        ("下周日前交", [("下周日", "2026-10-25")]),
        ("下周三分红", [("下周三", "2026-10-21")]),
        ("下周一起息", [("下周一", "2026-10-19")]),
        ("在下周三之前", [("下周三", "2026-10-21")]),
        ("按下周三的安排", [("下周三", "2026-10-21")]),
        ("我说明天去", [("明天", "2026-10-15")]),
        ("周日夜里到，下周日出发", [("下周日", "2026-10-25")]),
    ],
)
def test_dates_next_to_lookalike_words_still_count(text: str, expected: list[tuple[str, str]]) -> None:
    assert labeled(text, "2026-10-14 10:00") == expected


def test_doubled_prefix_after_a_compound() -> None:
    """“记一下下周四”是“一下”加“下周四”；“马上上个月”是“马上”加“上个月”。真的“下下周四”照旧。"""
    assert labeled("麻烦记一下下周四再提醒我", "2026-09-27 21:48") == [("下周四", "2026-10-01")]  # 周日说
    assert labeled("马上上个月的账就齐了", "2026-10-14 10:00") == [("上个月", "2026-09")]
    assert skipped("下下周四再说", "2026-09-27 21:48") == [("下下周四", L4_UNSURE)]  # 真的“下下周四”：L4 认成下周四，不标
    assert skipped("最晚 19 日前寄出，三日后再看", "2026-08-25 10:00") == [
        ("19 日前", "只写了几号，不知道是哪个月"), ("三日后", "几天前、几个月后：从哪天算拿不准")]


def test_midnight_is_unsure_when_the_two_days_disagree() -> None:
    assert NIGHT_END == 5
    assert skipped("明天交割", "2026-10-15 00:30") == [("明天", "半夜说的，拿不准是哪一天")]
    assert labeled("下个月再说", "2026-10-15 00:30") == [("下个月", "2026-11")]  # 前一天算也是 11 月
    assert skipped("下个月再说", "2026-11-01 00:30") == [("下个月", "半夜说的，拿不准是哪一天")]
    assert skipped("上周三买的", "2026-10-19 00:30") == [("上周三", "半夜说的，拿不准是哪一天")]  # 周一半夜
    assert labeled("上周三买的", "2026-10-20 00:30") == [("上周三", "2026-10-14")]  # 周二半夜：前一天也在这一周
    assert labeled("明天交割", "2026-10-15 05:00") == [("明天", "2026-10-16")]


def test_dates_written_right_beside() -> None:
    when = "2026-08-24 10:00"  # 周一
    assert skipped("明天 8/25 去看房", when) == [("明天", "旁边写着同一天")]
    assert skipped("8/25（明天）交割", when) == [("明天", "旁边写着同一天")]
    assert skipped("明天（8 月 26 日）起息", when) == [("明天", "旁边写的日子对不上")]
    assert skipped("明天 2026-08-25 起息", when) == [("明天", "旁边写着同一天")]
    assert labeled("明天周二", when) == [("明天", "2026-08-25")]  # 星期几对得上：照标
    assert skipped("明天周三", when) == [("明天", "旁边写的星期几对不上")]
    assert skipped("下周二（2 号）开会", when) == [("下周二", "旁边写的日子对不上")]  # 下周二是 9 月 1 日
    assert skipped("下周二（1 号）开会", when) == [("下周二", "旁边写着同一天")]
    assert skipped("下周二（9 月 1 日）开会", when) == [("下周二", "旁边写着同一天")]
    assert skipped("下个月（9 月）到期", when) == [("下个月", "旁边写着同一个月")]
    assert labeled("明天下午 2:00 开会，明天 3.5% 的票息", when) == [("明天", "2026-08-25"), ("明天", "2026-08-25")]


# ---------- 哪些行能标 ----------


def test_only_new_lines_said_at_the_heading_time() -> None:
    window = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))[0]
    block, line = window.blocks[0], window.blocks[0].lines[0]
    assert line_reason(block, line) == ""
    assert line_reason(block, replace(line, attachment=1, kind=ATTACHMENT)).startswith("附件的正文")
    assert line_reason(block, replace(line, kind=FORWARDED)).startswith("转发的")
    assert line_reason(block, replace(line, kind=HEADER)) == "邮件头"
    assert line_reason(block, replace(line, origin=ORIGIN_RELAYED)).startswith("转述的、外部的")
    assert line_reason(replace(block, origin=ORIGIN_EXTERNAL), line).startswith("转述的、外部的")
    mail = object()  # 邮件：整封都是 external，新写的正文照这封邮件的时间标
    assert line_reason(replace(block, mail=mail, origin=ORIGIN_EXTERNAL), replace(line, origin=ORIGIN_EXTERNAL)) == ""
    assert line_reason(block, replace(line, kind=NEW, quoted=True)) == "不是新写的话"


def test_relayed_and_external_lines_in_a_real_packet_are_not_labeled() -> None:
    window = windows_of(chat("2026-11-01 10:00", "我：[转发 自 小李] 昨天到的货", "desk-bot：[外部:网页] 昨天收涨", "我：昨天交割的"))[0]
    notes, detail = window_notes(window, render_window(window, PATTERNS))
    assert sum(len(pairs) for pairs in notes.values()) == 1
    assert [row["为什么不标"] for row in detail["skipped"]] == [  # type: ignore[index]
        "转述的、外部的：写的时间不一定是抬头上的时间", "转述的、外部的：写的时间不一定是抬头上的时间"]


# ---------- 写进窗口 ----------


def test_window_notes_go_after_the_number_and_keep_shown() -> None:
    window = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9，下周三再看，今天先这样。"))[0]
    rendered = render_window(window, PATTERNS)
    notes, detail = window_notes(window, rendered)
    ref = window.targets[0].ref
    assert notes == {ref: [["昨天", "2026-10-31"], ["下周三", "2026-11-04"]]}  # 2026-11-01 是周日
    noted = apply_notes(rendered, notes)
    assert f"{ref} 〔日子：昨天 = 2026-10-31；下周三 = 2026-11-04〕昨天交割的 X9，下周三再看，今天先这样。" in noted.text
    assert noted.shown == rendered.shown  # L4 比引文用的原文不变
    assert noted.sha256 != rendered.sha256
    assert [line for line in noted.text.split("\n") if not line.startswith(ref)] == [
        line for line in rendered.text.split("\n") if not line.startswith(ref)]  # 别的行一字不变
    assert apply_notes(rendered, {}) is rendered
    assert [row["说法"] for row in detail["labeled"]] == ["昨天", "下周三"]  # type: ignore[index]
    assert [row["说法"] for row in detail["skipped"]] == ["今天"]  # type: ignore[index]
    assert "交割" not in json.dumps(detail, ensure_ascii=False)  # 只存说法、日子、行号，不存原文的句子


def test_notes_go_after_existing_labels_and_are_escaped() -> None:
    rendered = RenderedWindow("来源 x\n【正文】\nT1 〔引用〕原文一句\nT2 〔指：那张 = X1〕别的\nT3 第三句\n", "", {}, (), (),
                              {"T1": "原文一句", "T2": "别的", "T3": "第三句"})
    noted = apply_notes(rendered, {"T1": [["昨〔天", "2026-10-13〕"]], "T2": [["昨天", "2026-10-13"]]})
    assert "T1 〔引用〕〔日子：昨［天 = 2026-10-13］〕原文一句\n" in noted.text  # 〔〕换成［］，伪造不了别的标注
    assert "T2 〔指：那张 = X1〕〔日子：昨天 = 2026-10-13〕别的\nT3 第三句\n" in noted.text


def test_one_note_per_phrase_and_date_on_a_line() -> None:
    window = windows_of(chat("2026-11-01 10:00", "我：昨天买的，昨天卖的。"))[0]
    notes, detail = window_notes(window, render_window(window, PATTERNS))
    assert list(notes.values()) == [[["昨天", "2026-10-31"]]]
    assert len(detail["labeled"]) == 2  # type: ignore[arg-type]


def test_quotes_lose_stacked_labels() -> None:
    window = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))[0]
    ref = window.targets[0].ref
    parsed = {
        "entities": [],
        "facts": [{"text": "x", "subject": None, "attribute": None, "value": None, "occurred_at": None,
                   "time_precision": "unknown", "certainty": "stated", "speaker": "主人", "entities": [],
                   "evidence": [{"ref": ref, "quote": f"{ref} 〔引用〕〔日子：昨天 = 2026-10-31〕昨天交割的 X9"}]}],
        "relations": [],
    }
    assert map_output(parsed, window)["facts"][0]["evidence"][0]["quote"] == "昨天交割的 X9"


# ---------- 存进结果库、重放：跟着提示词 ----------


def test_only_the_a_prompts_do_a() -> None:
    """l3-v20、v21 加了 A；l3-v22、v23 是它们再加放进现有关系的那一句（J4.2 后一半），l3-v24、v25 是它们加隶属于（D210），
    照旧带 A。"""
    assert PRE_A_PROMPTS == frozenset({SOL, LUNA, "l3-v22", "l3-v23", "l3-v24", "l3-v25"})


def test_prepare_replays_saved_notes(store: ResultStore) -> None:
    windows = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))
    plain = prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL)
    assert plain[0][2] == prepare(windows, FakeModel(), PATTERNS, prompt=SOL)[0][2]  # 没标过：和不给结果库一样
    pre_a = PreA()
    pre_a.annotate(windows, store, PATTERNS)
    assert pre_a.stats["标过的窗口"] == 1 and pre_a.stats["标上的"] == 1
    noted = prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL)
    assert "〔日子：昨天 = 2026-10-31〕昨天交割的 X9。" in noted[0][1].text
    assert noted[0][2] != plain[0][2]  # 键跟着发出去的文字
    assert prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL)[0][2] == noted[0][2]  # 重放一字不差
    for other in ("l3-v4", "l3-v16", "l3-v17"):  # 不带 A 那一句的提示词：库里有 A 的标注也不加
        assert "〔日子：" not in prepare(windows, FakeModel(), PATTERNS, store=store, prompt=other)[0][1].text
    again = PreA()
    again.annotate(windows, store, PATTERNS)
    assert again.stats["存过的窗口"] == 1 and again.stats["标过的窗口"] == 0


def test_saved_notes_win_over_the_rules(store: ResultStore) -> None:
    """存过的不改、照存下的重放：规则以后变了（同一个版本号），已经抽过的窗口文字也不变。"""
    windows = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))
    ref = windows[0].targets[0].ref
    row = {"window_id": windows[0].window_id, "window_sha": render_window(windows[0], PATTERNS).sha256,
           "version": PRE_A_VERSION, "noted_sha": "", **contents(windows[0]), "detail": "{}",
           "created_at": "2026-11-16T00:00:00+08:00"}
    store.save_prea_notes({**row, "notes": json.dumps({ref: [["昨天", "2026-10-30"]]}, ensure_ascii=False)})
    store.save_prea_notes({**row, "notes": json.dumps({ref: [["昨天", "2026-10-29"]]}, ensure_ascii=False)})  # 不覆盖
    PreA().annotate(windows, store, PATTERNS)
    assert "〔日子：昨天 = 2026-10-30〕" in prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL)[0][1].text
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM prea_notes").fetchone()[0] == 1


def test_dry_run_computes_without_saving(store: ResultStore) -> None:
    windows = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))
    prepared = prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL, fresh_a=True)
    assert "〔日子：昨天 = 2026-10-31〕" in prepared[0][1].text
    assert store.prea_notes(windows[0].window_id, render_window(windows[0], PATTERNS).sha256, PRE_A_VERSION) is None
    PreA().annotate(windows, store, PATTERNS)
    assert prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL)[0][2] == prepared[0][2]  # 空跑的键和真跑一样
    assert "〔日子：" not in prepare(windows, FakeModel(), PATTERNS, store=store, prompt="l3-v16", fresh_a=True)[0][1].text


def test_private_windows_are_not_annotated(store: ResultStore) -> None:
    raw = chat("2026-11-01 10:00", "我：昨天交割的 X9。").replace("privacy: normal", "privacy: private")
    windows = windows_of(raw)
    assert windows[0].scope.private
    pre_a = PreA()
    pre_a.annotate(windows, store, PATTERNS)
    assert pre_a.stats["标过的窗口"] == 0
    assert "〔日子：" not in prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL, fresh_a=True)[0][1].text


def test_run_extraction_with_an_a_prompt_marks_then_extracts(store: ResultStore) -> None:
    windows = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。", "desk-bot：收到，下周三再对一遍。"))
    fake = FakeModel()
    result = run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL)
    assert result.outcomes[0].status == DONE
    sent = fake.calls[0][1]
    assert "〔日子：昨天 = 2026-10-31〕昨天交割的 X9。" in sent
    assert "〔日子：下周三 = 2026-11-04〕收到，下周三再对一遍。" in sent
    checked = verify_windows(windows, store, fake, PATTERNS, prompt=SOL)
    assert checked[0].extraction is not None  # 核对、写账算出来的键和抽的时候一样
    quotes = [proof["quote"] for fact in checked[0].extraction["facts"] for proof in fact["evidence"]]
    assert "昨天交割的 X9。" in quotes  # 引文是原文，不带〔日子：…〕
    again = run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL)
    assert again.outcomes[0].status != DONE and len(fake.calls) == 1  # 标注、结果都存过：不再调
    other = FakeModel()
    run_extraction(windows, other, store, patterns=PATTERNS, options=OPTIONS, prompt="l3-v16")
    assert "〔日子：" not in other.calls[0][1]  # 不带 A 的提示词：不标，也不加


def test_a_notes_travel_with_l3_db(tmp_path: Path, store: ResultStore) -> None:
    """重建账本时只拷 l3.db（tools 里的重建脚本就这样拷）：A 的标注在 l3.db 里，键照样对得上。"""
    windows = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))
    fake = FakeModel()
    run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL)
    copy = tmp_path / "copy"
    copy.mkdir()
    with sqlite3.connect(store.path) as source, sqlite3.connect(copy / "l3.db") as target:
        source.backup(target)
    with ResultStore(copy) as copied:
        assert verify_windows(windows, copied, fake, PATTERNS, prompt=SOL)[0].extraction is not None


def test_cli_extract_and_write_with_an_a_prompt(world: World, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    """命令行：JAVMEM_L3_PROMPT 选了带 A 的提示词就做 A（不另加开关）；write 是另一次运行，照库里存的加上，事实写进账本。"""
    body = "## 原话 · 10:30–10:31\n- [10:30] 我：东岸那边昨天交割了 X9 的 3 张。\n- [10:31] desk-bot：收到。\n"
    world.drop("inbox/desk-bot", build_packet(body=body))
    world.intake()
    monkeypatch.setenv("JAVMEM_L3_PROMPT", SOL)
    assert main(["extract", "--library", "prod", "--fake", "--dry-run"]) == 0
    assert "预处理 A" in capsys.readouterr().out
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert "'标上的': 1" in capsys.readouterr().out
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    conn = world.ledger("prod")
    try:
        texts = [row[0] for row in conn.execute("SELECT content FROM facts")]
    finally:
        conn.close()
    assert any("X9" in text for text in texts)
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        assert conn.execute("SELECT count(*) FROM prea_notes").fetchone()[0] == 1


def test_cli_default_prompt_does_not_do_a(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(body="## 原话 · 10:30–10:31\n- [10:30] 我：昨天交割了 3 张。\n"))
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0  # 默认 l3-v17：不做 A
    with sqlite3.connect(world.data / "l3" / "l3.db") as conn:
        assert conn.execute("SELECT count(*) FROM prea_notes").fetchone()[0] == 0


# ---------- 撤回、彻底删除连着删 ----------


def test_deleting_windows_drops_their_a_notes(store: ResultStore) -> None:
    windows = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))
    run_extraction(windows, FakeModel(), store, patterns=PATTERNS, options=OPTIONS, prompt=SOL)
    plain = render_window(windows[0], PATTERNS).sha256
    assert store.prea_notes(windows[0].window_id, plain, PRE_A_VERSION) is not None
    doomed, _ = store.doomed(sentences={windows[0].targets[0].sentence_id})
    assert [window_id for window_id, _ in doomed] == [windows[0].window_id]
    store.delete_windows(doomed)
    assert store.prea_notes(windows[0].window_id, plain, PRE_A_VERSION) is None


def test_forget_prea_drops_notes_that_never_reached_l3(store: ResultStore) -> None:
    """标完了、还没抽就停下的：结果库里没有窗口这一行，撤回时照样按包、句子删掉它的标注。"""
    first, second = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"), chat("2026-11-02 10:00", "我：明天起息。"))
    PreA().annotate([first, second], store, PATTERNS)
    assert store.forget_prea(packets={first.targets[0].packet_id}) == 1
    assert store.prea_notes(first.window_id, render_window(first, PATTERNS).sha256, PRE_A_VERSION) is None
    assert store.forget_prea(sentences={second.targets[0].sentence_id}) == 1
    assert store.prea_notes(second.window_id, render_window(second, PATTERNS).sha256, PRE_A_VERSION) is None
    assert store.forget_prea() == 0


# ---------- 和预处理 B 一起 ----------


def test_b_notes_go_after_all_leading_labels() -> None:
    rendered = RenderedWindow("来源 x\n【正文】\nT1 〔引用〕原文一句\nT2 别的\n", "", {}, (), (), {"T1": "原文一句", "T2": "别的"})
    both = preb.apply_notes(apply_notes(rendered, {"T1": [["昨天", "2026-10-13"]]}), {"T1": [["那张", "X1"]]})
    assert "T1 〔引用〕〔日子：昨天 = 2026-10-13〕〔指：那张 = X1〕原文一句\nT2 别的\n" in both.text
    only_b = preb.apply_notes(rendered, {"T1": [["那张", "X1"]]})
    assert "T1 〔引用〕〔指：那张 = X1〕原文一句\n" in only_b.text  # 只有 B 的：和原来一字不差


def test_strip_pre_notes_gives_back_the_plain_text() -> None:
    window = windows_of(chat("2026-11-01 10:00", "我：小岑昨天交割的 X9。"))[0]
    plain = render_window(window, PATTERNS)
    ref = window.targets[0].ref
    both = preb.apply_notes(apply_notes(plain, {ref: [["昨天", "2026-10-31"]]}), {ref: [["小岑", "岑小燕"]]})
    assert both.text != plain.text and strip_pre_notes(both.text) == plain.text
    quoted = "来源 x\n【正文】\nT1 〔引用〕〔日子：昨天 = 2026-10-13〕〔指：那张 = X1〕原文［引］\nC1 〔签名〕别的\n"
    assert strip_pre_notes(quoted) == "来源 x\n【正文】\nT1 〔引用〕原文［引］\nC1 〔签名〕别的\n"  # render 自己的标注留着


def test_prepare_adds_a_then_b(store: ResultStore) -> None:
    windows = windows_of(chat("2026-11-01 10:00", "我：小岑昨天交割的 X9。"))
    ref = windows[0].targets[0].ref
    save_notes(store, windows[0], {ref: [["小岑", "岑小燕"]]})
    PreA().annotate(windows, store, PATTERNS)
    text = prepare(windows, FakeModel(), PATTERNS, store=store, prompt=SOL)[0][1].text
    assert f"{ref} 〔日子：昨天 = 2026-10-31〕〔指：小岑 = 岑小燕〕小岑昨天交割的 X9。" in text
    only_b = prepare(windows, FakeModel(), PATTERNS, store=store, prompt="l3-v16")[0][1].text
    assert f"{ref} 〔指：小岑 = 岑小燕〕小岑昨天交割的 X9。" in only_b


def test_withdrawal_reaches_windows_that_carry_both_labels(tmp_path: Path, store: ResultStore) -> None:
    """撤回 B 学名字的那一句：标注里用了这个名字的窗口要删、重抽。那个窗口还带着 A 的〔日子〕时也一样：B 记下的指纹是
    只加了 B 的那份，发出去的是 A、B 都加了的，要按没加标注的那份对。"""
    source = windows_of(chat("2026-11-06 10:00", "我：韦明下午到。"))[0]
    user = windows_of(chat("2026-11-07 10:00", "我：他昨天就定价了。"))[0]
    lex = lexicon_from(tmp_path, LEXICON_YAML)
    PreB(FakeLocal(learns=[learn_entry(source.targets[0].ref, "韦明", "韦明", "人")]), lex).run_packet([source], store, PATTERNS)
    weiming = next(row for row in store.preb_items(PRE_B_VERSION) if row["name"] == "韦明")
    save_notes(store, user, {user.targets[0].ref: [["他", "韦明"]]}, [int(weiming["item_id"])])
    PreA().annotate([user], store, PATTERNS)
    model = FakeModel()
    for window in (source, user):
        _, rendered, key = prepare([window], model, PATTERNS, store=store, prompt=SOL)[0]
        store.save_window(window_row(window, rendered, key, model.cache_name, SOL))
    sent = prepare([user], model, PATTERNS, store=store, prompt=SOL)[0][1].text
    assert "〔日子：昨天 = 2026-11-06〕〔指：他 = 韦明〕" in sent
    doomed, targets = store.doomed(sentences={source.targets[0].sentence_id})
    assert user.window_id in {window_id for window_id, _ in doomed}
    assert user.targets[0].packet_id in targets  # 用了这个名字的包改回排队、重抽
    _, noted = store.doomed_by_notes(sentences={source.targets[0].sentence_id})
    assert user.targets[0].packet_id in noted
    store.delete_windows(doomed)
    plain = render_window(user, PATTERNS).sha256
    assert store.preb_notes(user.window_id, plain, PRE_B_VERSION, window_context(user)) is None
    assert store.prea_notes(user.window_id, plain, PRE_A_VERSION) is None


def test_deleting_an_a_labeled_window_drops_b_marks_too(tmp_path: Path, store: ResultStore) -> None:
    """B 收过名字、没标〔指〕的窗口，发出去的只带 A 的标注：删窗口时 B 收名字的记号也要删（里面有这个窗口的原文）。"""
    window = windows_of(chat("2026-11-07 10:00", "我：韦明昨天到。"))[0]
    lex = lexicon_from(tmp_path, LEXICON_YAML)
    PreB(FakeLocal(learns=[learn_entry(window.targets[0].ref, "韦明", "韦明", "人")]), lex).run_packet([window], store, PATTERNS)
    PreA().annotate([window], store, PATTERNS)
    model = FakeModel()
    _, rendered, key = prepare([window], model, PATTERNS, store=store, prompt=SOL)[0]
    assert "〔日子：" in rendered.text and "〔指：" not in rendered.text
    store.save_window(window_row(window, rendered, key, model.cache_name, SOL))
    plain = render_window(window, PATTERNS).sha256
    assert store.preb_learned(window.window_id, plain, PRE_B_VERSION, window_context(window))
    store.delete_windows([(window.window_id, key)])
    assert not store.preb_learned(window.window_id, plain, PRE_B_VERSION, window_context(window))
    assert store.prea_notes(window.window_id, plain, PRE_A_VERSION) is None


def test_relation_check_reads_both_labels_and_keeps_the_date_from_a(store: ResultStore) -> None:
    """J4.3 的关系核对和 A 一起：〔指〕接在〔日子〕后面，R2 照样认得；模型照 A 的日子写的开始日子，R5 用的是同一套
    换算（verify.sentence_times），核得上，不降精度、不去掉。"""
    from test_relcheck import BANK, NOTE, codes, relation

    window = windows_of(chat("2026-11-07 10:00", "我：X1 票据昨天放在那家。"))[0]
    ref = window.targets[0].ref
    save_notes(store, window, {ref: [["那家", "海港银行"]]})
    PreA().annotate([window], store, PATTERNS)
    model = FakeModel()
    _, rendered, key = prepare([window], model, PATTERNS, store=store, prompt=SOL)[0]
    assert "〔日子：昨天 = 2026-11-06〕〔指：那家 = 海港银行〕" in rendered.text
    store.save_window(window_row(window, rendered, key, model.cache_name, SOL))
    item = relation(NOTE, "托管于", BANK, (ref, "X1 票据昨天放在那家"), valid_from="2026-11-06", time_precision="day")
    extraction = map_output({"entities": [NOTE, BANK], "facts": [], "relations": [item]}, window, rendered.redacted_refs)
    store.save_extraction(window.window_id, key, extraction)
    check = verify_windows([window], store, model, PATTERNS, prompt=SOL)[0].check
    assert check is not None
    result = check.relations[0]
    assert codes(result) == [] and "R2:object_alias" in result.notes
    assert (result.valid_from, result.valid_from_precision) == ("2026-11-06", "day")
    assert not [note for note in result.notes if note.startswith("R5:")]


def test_run_extraction_with_b_and_an_a_prompt(tmp_path: Path, store: ResultStore) -> None:
    """一个包一个包：先标 A（整批开头）、再标 B、再抽。发出去的带着两样，核对的键和抽的时候一样。"""
    windows = windows_of(chat("2026-11-07 10:00", "我：岑姐说那张昨天到期。"))
    ref = windows[0].targets[0].ref
    local = FakeLocal([{"行": ref, "说法": "岑姐", "名字": "岑小燕"}, {"行": ref, "说法": "那张", "名字": "X1 FCN（东岸证券）"}])
    fake = FakeModel()
    run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=LUNA,
                   pre_b=PreB(local, lexicon_from(tmp_path, LEXICON_YAML)))
    assert "〔日子：昨天 = 2026-11-06〕〔指：岑姐 = 岑小燕；那张 = X1 FCN（东岸证券）〕岑姐说那张昨天到期。" in fake.calls[0][1]
    assert verify_windows(windows, store, fake, PATTERNS, prompt=LUNA)[0].extraction is not None


# ---------- 审阅提的（先写测试看它红，再修） ----------

WHEN = "2026-10-14 10:00"  # 周三


@pytest.mark.parametrize(
    "text",
    ["上周日本那边加息", "上周日元走弱", "上周日经济数据出来了", "上周五星级酒店涨价", "上周三亚的房子卖了", "上周三星级酒店",
     "下周四川那边开会", "雨后天晴", "饭后天天散步", "上周三四个客户", "下周五六天都在外面"],
)
def test_review_words_after_the_weekday_or_before_houtian(text: str) -> None:
    assert labeled(text, WHEN) == []


def test_review_edges_and_day_numbers_that_are_words() -> None:
    assert labeled("上个月中签了", WHEN) == [("上个月", "2026-09")]
    assert labeled("明年初中毕业", WHEN) == [("明年", "2027")]
    assert labeled("上个月底仓加了一点", WHEN) == [("上个月", "2026-09")]
    assert labeled("下个月 5 号线开通", WHEN) == [("下个月", "2026-11")]
    assert labeled("明年 1 月 5 号线通车", WHEN) == [("明年 1 月", "2027-01")]


def test_review_forms_l4_cannot_check_are_not_labeled() -> None:
    """L4 的 G6 不认的写法不标：模型照着写了日子，L4 核不出来，事实会被挡掉。"""
    for text in ("上上周三买的", "下下周四再说", "下下个月到期", "上上个月买的", "昨儿到的", "下月三号付款",
                 "the day before yesterday", "大前年买的", "大后年到期"):
        assert labeled(text, WHEN) == [], text
        assert [reason for _, reason in skipped(text, WHEN)] == ["L4 认不出这个写法：照着写日子会被 L4 挡"], text
    assert labeled("下周三交割，下个月 3 号付款，上个月底到账，去年 3 月开户", WHEN) == [
        ("下周三", "2026-10-21"), ("下个月 3 号", "2026-11-03"), ("上个月底", "2026-09 月底"), ("去年 3 月", "2025-03")]


def test_review_dates_inside_reported_speech_are_unsure() -> None:
    assert labeled("昨天他说明天发货", WHEN) == [("昨天", "2026-10-13")]
    assert skipped("昨天他说明天发货", WHEN) == [("明天", "别人在另一天说的话里的日子：拿不准照哪天算")]
    assert labeled("10/12 他说明天到", WHEN) == []
    assert labeled("他说明天发货", WHEN) == [("明天", "2026-10-15")]  # 没有别的日子：就是今天说的
    assert labeled("明年初再说，去年同期", WHEN) == [("明年初", "2027 年初"), ("去年", "2025")]  # “再说”不是某人说
    assert labeled("昨天据说明天到", WHEN) == [("昨天", "2026-10-13"), ("明天", "2026-10-15")]  # 据说：照今天算


def test_review_non_ascii_lookalike_letters_do_not_crash() -> None:
    assert find_dates("laſt month thıs year yeſterday", at(WHEN)) == []


def test_review_hyphen_date_beside() -> None:
    assert skipped("明天 10-30 截止", "2026-10-30 10:00") == [("明天", "旁边写的日子对不上")]
    assert skipped("交表就在明天 10-30，别忘了", "2026-10-29 10:00") == [("明天", "旁边写着同一天")]


# ---------- a2：第 4.2 节留下的（复审以后提的），先写测试看它红，再修 ----------


@pytest.mark.parametrize("text", ["这周日用品打折", "这周五险一金交了", "站上月线了", "下班后天都黑了", "后天养成的习惯", "年前天气冷"])
def test_a2_lookalikes_are_not_labeled(text: str) -> None:
    assert labeled(text, WHEN) == []


def test_a2_reported_speech_with_spaced_dates() -> None:
    assert skipped("3 月 5 号他说明天到", WHEN) == [("明天", REPORTED)]
    assert skipped("3 月 5 日他说明天到", WHEN) == [("明天", REPORTED)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("下周三两点开会", [("下周三", "2026-10-21")]),
        ("下周五九点", [("下周五", "2026-10-23")]),
        ("上周五金价涨了", [("上周五", "2026-10-09")]),
        ("上周五道路施工", [("上周五", "2026-10-09")]),
        ("下周三亚洲开盘", [("下周三", "2026-10-21")]),
        ("我会后天联系你", [("后天", "2026-10-16")]),
        ("明天 2-3 点开会", [("明天", "2026-10-15")]),
    ],
)
def test_a2_dates_that_a1_missed(text: str, expected: list[tuple[str, str]]) -> None:
    assert labeled(text, WHEN) == expected


def test_a2_keeps_what_a1_blocked() -> None:
    """修的时候别把原来挡住的放出来。"""
    for text in ("上周三四个客户", "下周五六天都在外面", "上周五金店买的", "下周五道口见", "上周三亚的房子卖了", "上周三星级酒店",
                 "会后天天加班", "上周五星级酒店涨价"):
        assert labeled(text, WHEN) == [], text


def test_a2_day_with_a_clock_stays_at_the_month() -> None:
    """“下个月3号9点”：L4 不认紧跟着数字的“3号”（怕是 3 号楼这种），A 跟着 L4，只标到月。"""
    assert labeled("下个月3号9点", WHEN) == [("下个月", "2026-11")]


# ---------- 第二遍审阅查出来的（先写测试看它红，再修） ----------


@pytest.mark.parametrize("text", ["上周三十万的单子成交了", "上周五十多个人", "这周四五十万", "上周三四五个客户",
                                  "开完会后天都黑了", "散会后天津那边发了材料", "会后天津那边发了材料"])
def test_second_review_lookalikes(text: str) -> None:
    assert labeled(text, WHEN) == []


def test_second_review_dates_still_labeled() -> None:
    assert labeled("下周三十点开会", WHEN) == [("下周三", "2026-10-21")]  # 十点：数后面跟着“点”，是钟点
    assert labeled("下周三两点开会", WHEN) == [("下周三", "2026-10-21")]
    assert labeled("他们会后天到", WHEN) == [("后天", "2026-10-16")]  # 会 = 将要
    assert labeled("我会后天联系你", WHEN) == [("后天", "2026-10-16")]


def test_purging_the_old_revision_keeps_the_current_windows_a_notes(store: ResultStore) -> None:
    """包重发第 2 版、文字没变（窗口编号、文字都一样，照用第 1 版时存的 A 标注），再彻底删除第 1 版：这条标注第 2 版的
    窗口还在用，不能删；记的包、句子换成第 2 版的，以后撤回第 2 版的内容时照样删得掉。"""
    first = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))
    fake = FakeModel()
    run_extraction(first, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL)
    resent = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。", revision=2))
    assert resent[0].window_id == first[0].window_id
    assert render_window(resent[0], PATTERNS).sha256 == render_window(first[0], PATTERNS).sha256
    run_extraction(resent, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL)
    old = {(resent[0].targets[0].packet_id, 1)}
    doomed, _ = store.doomed(revisions=old)  # javmem purge --revision 1 做的
    store.delete_windows(doomed)
    store.forget_prea(revisions=old)
    assert verify_windows(resent, store, fake, PATTERNS, prompt=SOL)[0].extraction is not None  # 现行的版本照样对得上
    sentence = resent[0].targets[0].sentence_id
    doomed, _ = store.doomed(sentences={sentence})  # 以后撤回第 2 版的这一句
    store.delete_windows(doomed)
    store.forget_prea(sentences={sentence})
    assert store.prea_notes(resent[0].window_id, render_window(resent[0], PATTERNS).sha256, PRE_A_VERSION) is None


def test_resent_as_private_drops_the_old_a_notes(store: ResultStore) -> None:
    """重发成 private、文字一样：private 的窗口不加 A（结果库里那一行不存文字），旧版本的 A 标注没人用了，删旧版本时要删掉，
    不能当成还在用（复审查出来的）。"""
    first = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。"))
    fake = FakeModel()
    run_extraction(first, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL)
    private = windows_of(chat("2026-11-01 10:00", "我：昨天交割的 X9。", revision=2, privacy="private"))
    assert private[0].scope.private and private[0].window_id == first[0].window_id
    run_extraction(private, fake, store, patterns=PATTERNS, options=OPTIONS, prompt=SOL)
    old = {(private[0].targets[0].packet_id, 1)}
    doomed, _ = store.doomed(revisions=old)  # 包重发成 private 时 forget_private_revisions 做的
    store.delete_windows(doomed)
    store.forget_prea(revisions=old)
    assert store.prea_notes(first[0].window_id, render_window(first[0], PATTERNS).sha256, PRE_A_VERSION) is None


def test_withdrawal_reaches_b_labeled_windows_without_stored_text(tmp_path: Path, store: ResultStore) -> None:
    """窗口这一行没存发出去的文字（打码模式表改了以后成了打码没打干净的那种），可是指纹是带着〔指〕的：撤回 B 学名字的那一句，
    照 B 记下的 noted_sha 照样认得出来（0a33b45 就认得）。"""
    source = windows_of(chat("2026-11-06 10:00", "我：韦明下午到。"))[0]
    user = windows_of(chat("2026-11-07 10:00", "我：他就定价了。"))[0]
    lex = lexicon_from(tmp_path, LEXICON_YAML)
    PreB(FakeLocal(learns=[learn_entry(source.targets[0].ref, "韦明", "韦明", "人")]), lex).run_packet([source], store, PATTERNS)
    weiming = next(row for row in store.preb_items(PRE_B_VERSION) if row["name"] == "韦明")
    noted = save_notes(store, user, {user.targets[0].ref: [["他", "韦明"]]}, [int(weiming["item_id"])])
    model = FakeModel()
    row = window_row(user, noted, "k-" + user.window_id, model.cache_name, SOL)
    store.save_window({**row, "input_text": ""})
    doomed, _ = store.doomed(sentences={source.targets[0].sentence_id})
    assert user.window_id in {window_id for window_id, _ in doomed}
    _, noted_targets = store.doomed_by_notes(sentences={source.targets[0].sentence_id})
    assert user.targets[0].packet_id in noted_targets
