"""软变化判定的那一次调用（J4.5，javmem.soft.judge）：发什么、回复的校验、存法、一批怎么调。假模型，例子都是编的。"""

from __future__ import annotations

import csv
import json
import re
import threading
import time
from pathlib import Path

import pytest

from javmem.llm import ModelReply, Usage
from javmem.pipeline.extract import CACHED, DONE, FAILED, ExtractOptions, FormatError
from javmem.redaction import RedactionPattern, default_patterns
from javmem.soft import rules
from javmem.soft.cases import Case, Line, OldItem
from javmem.soft.claude import FakeSoftModel, fake_soft
from javmem.soft.judge import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    PURPOSE,
    SCHEMA,
    UNSENT,
    SoftStore,
    case_key,
    load_prompt,
    normal_date,
    parse_reply,
    plan,
    prompt_fingerprint,
    render_case,
    run_judge,
    verdict_records,
)
from javmem.usage import TokenLedger

QUICK = ExtractOptions(tag="soft-test", sleep=lambda _: None, backoff_base=0.0)


def pause_case(case_id: str = "Q01", *, text: str = "东湖那批先停一下，等客户回复再说。", private: bool = False) -> Case:
    return Case(
        case_id,
        (
            OldItem("O1", "relation", "林工 负责 东湖那批订单", "生效", "2026-10-12", "2026-11-02"),
            OldItem("O2", "relation", "林工 任职于 北岸纺织", "生效"),
        ),
        (
            Line("T1", "2026-11-07T10:28:00+08:00", "我", "owner", "东湖那批客户还没回样品意见。"),
            Line("T2", "2026-11-07T10:30:00+08:00", "我", "owner", text),
            Line("T3", "2026-11-07T10:31:00+08:00", "desk-bot", "bot", "好的，东湖那批先停〔已记下〕。", quoted=True),
        ),
        ("T2",),
        packet_date="2026-11-07",
        private=private,
    )


def reply(**overrides: object) -> dict[str, object]:
    items = [
        {"target": "O1", "about": True, "verdict": "B", "condition": "等客户回复", "effective_at": None,
         "effective_precision": "unknown", "new_value": None, "sure": True, "finance": False,
         "quote": "东湖那批先停一下，等客户回复", "reason": "先停下来，说了等什么"},
        {"target": "O2", "about": False, "verdict": "A", "condition": None, "effective_at": None,
         "effective_precision": "unknown", "new_value": None, "sure": True, "finance": False,
         "quote": "东湖那批先停一下", "reason": "没说林工在哪家"},
    ]
    for ref, changes in overrides.items():
        for item in items:
            if item["target"] == ref:
                item.update(changes)  # type: ignore[arg-type]
    return {"items": items}


# ---------- 提示词 ----------


def test_prompt_fingerprint_is_pinned() -> None:
    """改了提示词或输出结构，要换 PROMPT_VERSION、改 PROMPT_SHA256：不然存下的旧结论会被当成新提示词的。"""
    assert prompt_fingerprint() == PROMPT_SHA256, f"{PROMPT_VERSION} 的指纹变了，换版本号并更新 PROMPT_SHA256"


def test_prompt_lists_six_verdicts_and_every_field() -> None:
    prompt = load_prompt()
    for letter, text in rules.VERDICT_TEXT.items():
        assert f"**{letter} {text}**" in prompt
    for name in SCHEMA["properties"]["items"]["items"]["properties"]:  # type: ignore[index]
        assert f"`{name}`" in prompt


# ---------- 发什么 ----------


def test_rendering_shows_old_items_lines_and_targets() -> None:
    request = render_case(pause_case())
    lines = request.text.splitlines()
    assert lines[0] == "【包日期】2026-11-07（要判的那几行是 2026-11-07 10:30 说的）"
    assert "O1 关系：林工 负责 东湖那批订单。生效；2026-10-12 起；最后一次说到 2026-11-02。" in lines
    assert "O2 关系：林工 任职于 北岸纺织。生效。" in lines
    assert "T2 [2026-11-07 10:30] 我：东湖那批先停一下，等客户回复再说。" in lines
    assert "T3 [2026-11-07 10:31] desk-bot〔引用〕：好的，东湖那批先停［已记下］。" in lines  # 原文的〔〕换成［］
    assert lines[-1] == "【要判的】T2"
    assert request.olds == ("O1", "O2") and request.targets == ("T2",)


def test_secrets_are_masked_and_leftovers_are_not_sent(tmp_path: Path) -> None:
    case = pause_case(text="东湖那批先停一下，后台密码是 Hx9k2mQ7pz 记一下。")
    request = render_case(case, default_patterns())
    assert "Hx9k2mQ7pz" not in request.text and request.redactions and not request.leftover
    stubborn = [RedactionPattern("密钥", re.compile("已隐去|Hx9k2mQ7pz"))]  # 打完还扫得出来
    leftover = render_case(case, stubborn)
    assert leftover.leftover == ("密钥",)
    model = FakeSoftModel()
    with SoftStore(tmp_path) as store:
        result = run_judge([case], model, store, patterns=stubborn, options=QUICK)
    assert result.outcomes["Q01"].status == UNSENT and result.outcomes["Q01"].detail == "leftover"
    assert model.calls == []


def test_keys_depend_on_the_text_model_and_repeat() -> None:
    request = render_case(pause_case())
    assert case_key(request, "m@high") == case_key(render_case(pause_case()), "m@high")
    assert case_key(request, "m@high") != case_key(request, "m@medium")
    assert case_key(request, "m@high") != case_key(render_case(pause_case(text="东湖那批先放放。")), "m@high")
    assert case_key(request, "m@high", repeat=1) != case_key(request, "m@high")


# ---------- 回什么 ----------


def test_a_good_reply_comes_back_in_old_order_with_checks() -> None:
    request = render_case(pause_case())
    data = reply()
    data["items"].reverse()  # type: ignore[union-attr]
    parsed = parse_reply(json.dumps(data, ensure_ascii=False), request)
    first, second = parsed["items"]  # type: ignore[misc]
    assert (first["target"], first["verdict"], first["quote_ok"], first["consistent"]) == ("O1", "B", True, True)
    assert (second["target"], second["about"], second["consistent"]) == ("O2", False, True)


@pytest.mark.parametrize(
    "content",
    [
        "{不是 JSON",
        json.dumps({"items": [reply()["items"][0]]}, ensure_ascii=False),  # type: ignore[index]  漏了 O2
        json.dumps({"items": reply()["items"] * 2}, ensure_ascii=False),  # type: ignore[operator]  重复
        json.dumps(reply(O1={"target": "O9"}), ensure_ascii=False),  # 多了一条没有的
        json.dumps(reply(O1={"verdict": "G"}), ensure_ascii=False),
        json.dumps(reply(O1={"sure": "yes"}), ensure_ascii=False),
        json.dumps({"items": reply()["items"], "extra": 1}, ensure_ascii=False),
    ],
)
def test_bad_replies_are_format_errors(content: str) -> None:
    with pytest.raises(FormatError):
        parse_reply(content, render_case(pause_case()))


def test_quote_must_come_from_the_judged_lines() -> None:
    request = render_case(pause_case())
    parsed = parse_reply(json.dumps(reply(O1={"quote": "客户还没回样品意见"}), ensure_ascii=False), request)  # 那是 T1
    assert parsed["items"][0]["quote_ok"] is False  # type: ignore[index]
    spaced = parse_reply(json.dumps(reply(O1={"quote": "东湖那批 先停一下，等客户回复。"}), ensure_ascii=False), request)
    assert spaced["items"][0]["quote_ok"] is True  # type: ignore[index]  空白、两头的标点不算差别
    assert rules.action(parsed["items"][0], said_at=request.said_at).kind == rules.REVIEW  # type: ignore[index]


def test_saying_it_is_not_about_this_one_but_changing_it_is_inconsistent() -> None:
    request = render_case(pause_case())
    parsed = parse_reply(json.dumps(reply(O2={"verdict": "C"}), ensure_ascii=False), request)
    assert parsed["items"][1]["consistent"] is False  # type: ignore[index]


def test_effective_dates_are_normalised() -> None:
    said = "2026-11-07T10:30:00+08:00"
    assert normal_date("2026-11-09", said) == ("2026-11-09", "day")
    assert normal_date("2026-11", said) == ("2026-11", "month")
    assert normal_date("2026-Q4", said) == ("2026-Q4", "quarter")
    assert normal_date("2026-11-09T10:30", said) == ("2026-11-09T10:30+08:00", "minute")  # 没写时区用说话的时区
    assert normal_date("2026-11-09T02:30Z", said) == ("2026-11-09T02:30+00:00", "minute")
    assert normal_date("2026-13", said) is None and normal_date("下周一", said) is None
    assert normal_date("2026-02-30", said) is None
    request = render_case(pause_case())
    parsed = parse_reply(json.dumps(reply(O1={"effective_at": "2026-11", "effective_precision": "day"}), ensure_ascii=False), request)
    assert (parsed["items"][0]["effective_at"], parsed["items"][0]["effective_precision"]) == ("2026-11", "month")  # type: ignore[index]
    bad = parse_reply(json.dumps(reply(O1={"effective_at": "下周一", "effective_precision": "day"}), ensure_ascii=False), request)
    assert (bad["items"][0]["effective_at"], bad["items"][0]["date_ok"]) == (None, False)  # type: ignore[index]


def test_the_fake_model_answers_every_old_item() -> None:
    answer = fake_soft(render_case(pause_case()).text)
    assert [item["target"] for item in answer["items"]] == ["O1", "O2"]  # type: ignore[index]
    assert parse_reply(json.dumps(answer, ensure_ascii=False), render_case(pause_case()))


# ---------- 一批怎么调 ----------


def test_verdicts_are_stored_and_not_called_again(tmp_path: Path) -> None:
    model = FakeSoftModel()
    with SoftStore(tmp_path, exclusive=True) as store:
        first = run_judge([pause_case()], model, store, options=QUICK)
        assert first.outcomes["Q01"].status == DONE
        second = run_judge([pause_case()], model, store, options=QUICK)
        assert second.outcomes["Q01"].status == CACHED
        again = run_judge([pause_case()], model, store, options=QUICK, repeat=1)  # 再判一遍：另算键
        assert again.outcomes["Q01"].status == DONE
        records = verdict_records([pause_case()], first, store)
    assert len(model.calls) == 2
    system, user = model.calls[0]
    assert system == load_prompt() and user == render_case(pause_case()).text
    (record,) = records
    assert record["status"] == DONE and record["not_judged"] is None
    assert [item["action"]["kind"] for item in record["items"]] == [rules.PAUSE, rules.PAUSE]  # type: ignore[index]


def test_at_most_two_calls_at_a_time(tmp_path: Path) -> None:
    gate = threading.Lock()

    def slow(system: str, user: str) -> ModelReply:
        time.sleep(0.05)
        with gate:
            content = json.dumps(fake_soft(user), ensure_ascii=False)
        return ModelReply(content, "stop", Usage(10, 5), "fake-claude", 0.05)

    model = FakeSoftModel([slow] * 6)
    cases = [pause_case(f"Q{n:02d}", text=f"东湖那批第 {n} 箱先停一下。") for n in range(6)]
    with SoftStore(tmp_path, exclusive=True) as store:
        result = run_judge(cases, model, store, options=QUICK)
    assert result.count(DONE) == 6 and model.peak <= 2


def test_rate_limits_are_retried_and_every_call_is_booked(tmp_path: Path) -> None:
    ledger = TokenLedger(tmp_path / "ledger.csv")
    model = FakeSoftModel(["rate_limit", "ok"])
    with SoftStore(tmp_path / "soft", exclusive=True) as store:
        result = run_judge([pause_case()], model, store, ledger=ledger, options=QUICK)
        calls = store.calls()
    assert result.outcomes["Q01"].status == DONE and result.outcomes["Q01"].attempts == 2
    rows = list(csv.DictReader((tmp_path / "ledger.csv").open(encoding="utf-8")))
    assert [row["status"] for row in rows] == ["rate_limit", "ok"]
    assert {row["purpose"] for row in rows} == {PURPOSE} and {row["tag"] for row in rows} == {"soft-test"}
    assert {row["model"] for row in rows} == {"fake-claude@fake"}
    assert [row["status"] for row in calls] == ["rate_limit", "ok"] and calls[0]["case_id"] == "Q01"


def test_refusals_and_format_errors_are_not_called_again_unless_asked(tmp_path: Path) -> None:
    cases = [pause_case("Q01"), pause_case("Q02", text="东湖那批先放放。")]
    model = FakeSoftModel(["refusal", "bad_json", "bad_json"])
    with SoftStore(tmp_path, exclusive=True) as store:
        first = run_judge(cases, model, store, options=ExtractOptions(tag="t", sleep=lambda _: None, concurrency=1))
        assert first.outcomes["Q01"].status == FAILED and first.outcomes["Q01"].detail == "refusal"
        assert first.outcomes["Q02"].status == FAILED and first.outcomes["Q02"].detail == "format_error"
        assert plan(cases, model.cache_name, store, [])["failed_before"] == 2
        skipped = run_judge(cases, model, store, options=QUICK)
        assert all(outcome.status == FAILED and "以前失败过" in outcome.detail for outcome in skipped.outcomes.values())
        calls_before = len(model.calls)
        retried = run_judge(cases, model, store, options=ExtractOptions(tag="t", sleep=lambda _: None, retry_failed=True))
        assert retried.count(DONE) == 2 and len(model.calls) == calls_before + 2
        records = verdict_records(cases, first, store)
    assert all(record["items"] for record in records)  # 重调以后有结论了


def test_fatal_errors_stop_the_batch(tmp_path: Path) -> None:
    model = FakeSoftModel(["auth"])
    cases = [pause_case("Q01"), pause_case("Q02", text="东湖那批先放放。")]
    with SoftStore(tmp_path, exclusive=True) as store:
        result = run_judge(cases, model, store, options=ExtractOptions(tag="t", sleep=lambda _: None, concurrency=1))
        records = verdict_records(cases, result, store)
    assert result.stopped is not None and result.stopped.startswith("auth")
    assert len(model.calls) == 1
    assert all(record["items"] == [] and record["not_judged"]["kind"] == rules.REVIEW for record in records)  # type: ignore[index]


def test_budget_stops_before_spending(tmp_path: Path) -> None:
    ledger = TokenLedger(tmp_path / "ledger.csv")
    model = FakeSoftModel()
    with SoftStore(tmp_path / "soft", exclusive=True) as store:
        result = run_judge([pause_case()], model, store, ledger=ledger, options=ExtractOptions(tag="t", budget=100))
    assert result.stopped == "budget" and model.calls == []


def test_private_and_too_long_cases_are_not_sent(tmp_path: Path) -> None:
    model = FakeSoftModel()
    cases = [pause_case("Q01", private=True), pause_case("Q02", text="先停一下。" * 100)]
    with SoftStore(tmp_path, exclusive=True) as store:
        result = run_judge(cases, model, store, options=ExtractOptions(tag="t", max_input_chars=400))
        counts = plan(cases, model.cache_name, store, [], max_input_chars=400)
        records = verdict_records(cases, result, store)
    assert [result.outcomes[case.id].detail for case in cases] == ["private", "too_big"]
    assert model.calls == [] and counts["unsent"] == 2
    assert [record["status"] for record in records] == [UNSENT, UNSENT]


def test_limit_leaves_the_rest_for_later(tmp_path: Path) -> None:
    model = FakeSoftModel()
    cases = [pause_case(f"Q{n}", text=f"第 {n} 批先停一下。") for n in range(3)]
    with SoftStore(tmp_path, exclusive=True) as store:
        result = run_judge(cases, model, store, options=ExtractOptions(tag="t", limit=1))
    assert result.count(DONE) == 1 and len(model.calls) == 1
    assert sorted(outcome.detail for outcome in result.outcomes.values() if outcome.status != DONE) == ["limit", "limit"]


def test_only_one_judge_per_store(tmp_path: Path) -> None:
    from javmem.soft.judge import StoreBusy

    with SoftStore(tmp_path, exclusive=True):
        with pytest.raises(StoreBusy):
            SoftStore(tmp_path, exclusive=True)


# ---------- 审阅以后补的（先红后修） ----------


def test_ctrl_c_stops_the_cases_still_waiting(tmp_path: Path) -> None:
    """Ctrl+C（这里在打印进度时抛）：排着队的不再调模型，整批停下，照样往外抛。"""
    model = FakeSoftModel()
    cases = [pause_case(f"Q{n:02d}", text=f"第 {n} 批先停一下。") for n in range(10)]

    def log(line: str) -> None:
        raise KeyboardInterrupt

    with SoftStore(tmp_path, exclusive=True) as store:
        with pytest.raises(KeyboardInterrupt):
            run_judge(cases, model, store, options=ExtractOptions(tag="t", concurrency=1, sleep=lambda _: None), log=log)
    assert len(model.calls) <= 2


def test_secrets_across_speaker_and_text_are_not_sent() -> None:
    """每一段单独打码扫不出来，拼成一行“说话人：原文”就是一个验证码：照 L3，写出来的每一行再扫一遍，扫得出来的不发。"""
    case = Case("Q01", (OldItem("O1", "fact", "南港那张合同在走签字流程"),),
                (Line("T1", "2026-11-07T10:30:00+08:00", "招商银行动态码", "other", "835201"),), ("T1",))
    assert "验证码" in render_case(case, default_patterns()).leftover


def test_the_packet_date_is_masked_too() -> None:
    case = Case("Q01", (OldItem("O1", "fact", "南港那张合同在走签字流程"),),
                (Line("T1", "2026-11-07T10:30:00+08:00", "我", "owner", "南港那张合同先放放。"),), ("T1",),
                packet_date="后台密码是 Hx9k2mQ7pz")
    request = render_case(case, default_patterns())
    assert "Hx9k2mQ7pz" not in request.text


def test_more_date_shapes_are_read() -> None:
    said = "2026-11-07T10:30:00+08:00"
    assert normal_date("2026-11-09 10:30", said) == ("2026-11-09T10:30+08:00", "minute")
    assert normal_date("2026-11-09T10:30:00.250+08:00", said) == ("2026-11-09T10:30+08:00", "minute")
    assert normal_date("2026-11-09T10:30+0800", said) == ("2026-11-09T10:30+08:00", "minute")
