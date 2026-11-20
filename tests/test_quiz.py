"""拿自己的题让模型答一遍、判一遍分（校准工具包，`javmem quiz`，可选、花钱）。

这里不调真模型：拿编的小套件写好账，换上一个照题目回话的假模型，看一道题怎么走（检索、答、规则、判分）、
几种结论分得对不对、问过的不再问、每次调用记进 token 账、预算不够就停。数字、日子的规则单独核。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path

import pytest

from javmem.calib import quiz
from javmem.cli import main
from javmem.llm import ModelReply, Usage
from javmem.regress.replies import build_store

MINI = Path(__file__).resolve().parent / "fixtures" / "regress-mini"
REPO = Path(__file__).resolve().parents[1]
ENV = {
    "JAVMEM_SOURCES_FILE": str(MINI / "config" / "sources.yaml"),
    "JAVMEM_ACCESS_FILE": str(MINI / "config" / "access.yaml"),
    "JAVMEM_OWNER_FILE": str(MINI / "config" / "owner.yaml"),
    "JAVMEM_LEXICON_FILE": str(MINI / "config" / "lexicon.yaml"),
    "JAVMEM_TIMEZONE": "+08:00",
    "JAVMEM_L3_PROMPT": "l3-v20",
    "JAVMEM_LLM_MODEL": "gpt-6.1-sol",
    "JAVMEM_GRAPH_SYNC": "off",
    "JAVMEM_GRAPH_SEARCH": "off",
}
QUESTIONS = [
    {"id": "Q1", "category": "简单回忆", "query": "欧阳青和我是什么关系？", "answer": "表姐", "absent": False,
     "sources": ["2026-10-20_2300_demo-bot ｜ 表姐欧阳青"]},
    {"id": "Q2", "category": "数字条件", "query": "南岭纸业现在的报价是多少？", "answer": "每吨 4,500 元", "absent": False},
    {"id": "Q3", "category": "事实变化", "query": "梧桐路展厅项目现在谁负责？", "answer": "田甜", "as_of": "2026-11-10", "absent": False},
    {"id": "Q4", "category": "记忆里没有", "query": "我们和东湖酒店签过合同吗？", "absent": True},
    {"id": "Q5", "category": "数字条件", "query": "展厅的灯具哪天到？", "answer": "12 月 23 日", "absent": False},
    {"id": "Q6", "category": "记忆里没有", "query": "北渡物流的老板叫什么？", "absent": True},
]
# 假模型照题目回：(回答, 知不知道)；判分照题目回 correct / wrong
ANSWERS = {
    "欧阳青和我是什么关系": ("她是你的表姐。", True),
    "南岭纸业现在的报价是多少": ("每吨 4,200 元。", True),  # 数错了：规则就判错，不问判分模型
    "梧桐路展厅项目现在谁负责": ("祝明远负责。", True),
    "我们和东湖酒店签过合同吗": ("不知道", False),  # 记忆里没有：说不知道算对
    "展厅的灯具哪天到": ("不知道", False),
    "北渡物流的老板叫什么": ("叫王大海。", True),  # 记忆里没有却答出了内容：硬答
}
VERDICTS = {"欧阳青和我是什么关系": "correct", "梧桐路展厅项目现在谁负责": "wrong"}


class Scripted:
    """照题目回话的假模型。记下每一次调用的输出结构名和发过去的字。"""

    name = "test-model"
    cache_name = "test-model@test/json_schema"
    service_tier = ""
    max_output_tokens = 400

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def complete(self, system: str, user: str, schema: object, schema_name: str) -> ModelReply:
        self.calls.append((schema_name, system, user))
        if schema_name == quiz.JUDGE_SCHEMA_NAME:
            key = next(key for key in VERDICTS if key in user)
            body: dict[str, object] = {"verdict": VERDICTS[key], "reason": "测试用的理由"}
        else:
            key = next(key for key in ANSWERS if key in user)
            answer, known = ANSWERS[key]
            body = {"answer": answer, "known": known}
        return ModelReply(json.dumps(body, ensure_ascii=False), "stop", Usage(120, 30), self.name, 0.0, None)


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("quiz")
    data = root / "data"
    with pytest.MonkeyPatch.context() as patch:
        for name in [name for name in os.environ if name.startswith("JAVMEM_")]:
            patch.delenv(name)
        for key, value in ENV.items():
            patch.setenv(key, value)
        patch.setenv("JAVMEM_DATA_DIR", str(data))
        patch.setenv("JAVMEM_TOKEN_LEDGER", str(root / "token-ledger.csv"))
        assert main(["init"]) == 0
        inbox = data / "inbox-test" / "demo-bot"
        inbox.mkdir(parents=True, exist_ok=True)
        for packet in sorted((MINI / "packets" / "demo-bot").glob("*.md")):
            shutil.copy2(packet, inbox / packet.name)
        assert main(["intake"]) == 0
        build_store(MINI / "l3-replies.json", data / "l3")
        assert main(["write", "--library", "test", "--fake-embeddings"]) == 0
    return data


@pytest.fixture
def world(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    for name in [name for name in os.environ if name.startswith("JAVMEM_")]:
        monkeypatch.delenv(name)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(written))
    monkeypatch.setenv("JAVMEM_CALLER", "mini-owner")
    ledger = tmp_path / "token-ledger.csv"
    monkeypatch.setenv("JAVMEM_TOKEN_LEDGER", str(ledger))
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({"format": quiz.QUESTIONS_FORMAT, "from": "测试", "questions": QUESTIONS},
                                    ensure_ascii=False), encoding="utf-8")
    model = Scripted()
    monkeypatch.setattr(quiz, "make_model", lambda args: model)
    return {"questions": questions, "model": model, "ledger": ledger, "out": tmp_path / "quiz-out"}


def run_quiz(world: dict[str, object], *extra: str) -> int:
    return main(["quiz", "--questions", str(world["questions"]), "--out", str(world["out"]), *extra])


def results(out: Path) -> dict[str, dict[str, object]]:
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return {str(row["id"]): row for row in rows}


# ---------- 一道题怎么走 ----------


def test_each_kind_of_outcome_is_judged_the_strict_way(world: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    assert run_quiz(world) == 0
    out = capsys.readouterr().out
    found = results(world["out"])  # type: ignore[arg-type]
    assert {qid: (row["verdict"], row["by"]) for qid, row in found.items()} == {
        "Q1": ("对", "判分模型"),
        "Q2": ("错", "规则"),
        "Q3": ("错", "判分模型"),
        "Q4": ("对", "记忆里没有"),
        "Q5": ("不知道", "回答"),
        "Q6": ("硬答", "记忆里没有"),
    }
    model = world["model"]
    judged = [user for name, _, user in model.calls if name == quiz.JUDGE_SCHEMA_NAME]  # type: ignore[attr-defined]
    assert len(judged) == 2  # 只有 Q1、Q3 问了判分模型：规则判错的、不知道的、记忆里没有的都不问
    asked = [user for name, _, user in model.calls if name == quiz.ANSWER_SCHEMA_NAME]  # type: ignore[attr-defined]
    assert len(asked) == 6
    q3 = next(user for user in asked if "梧桐路展厅项目现在谁负责" in user)
    assert "2026-11-10" in q3  # 查询时间点照题上写的
    assert "田甜" in q3  # 检索到的条目放进去了
    summary = json.loads((world["out"] / "summary.json").read_text(encoding="utf-8"))  # type: ignore[operator]
    assert summary["total"] == {"questions": 6, "对": 2, "错": 2, "硬答": 1, "不知道": 1, "没答成": 0, "没判": 0}
    assert summary["by_category"]["数字条件"]["错"] == 1
    assert "对 2" in out and "硬答 1" in out


def test_asking_again_into_the_same_folder_costs_nothing(world: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    assert run_quiz(world) == 0
    first = len(world["model"].calls)  # type: ignore[attr-defined]
    assert run_quiz(world) == 0
    assert len(world["model"].calls) == first  # type: ignore[attr-defined]
    assert "问过的" in capsys.readouterr().out


def test_every_call_goes_into_the_token_ledger(world: dict[str, object]) -> None:
    assert run_quiz(world, "--tag", "quiz-test") == 0
    with Path(world["ledger"]).open(encoding="utf-8") as handle:  # type: ignore[arg-type]
        rows = list(csv.DictReader(handle))
    assert sorted(row["purpose"] for row in rows) == ["quiz_answer"] * 6 + ["quiz_judge"] * 2
    assert {row["tag"] for row in rows} == {"quiz-test"}
    assert {row["model"] for row in rows} == {Scripted.cache_name}


def test_a_dry_run_asks_nothing_and_writes_nothing(world: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    assert run_quiz(world, "--dry-run") == 0
    out = capsys.readouterr().out
    assert world["model"].calls == []  # type: ignore[attr-defined]
    assert not Path(world["out"]).exists()  # type: ignore[arg-type]
    assert "答题 6 次" in out


def test_a_budget_too_small_stops_and_says_so(world: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    assert run_quiz(world, "--budget", "50") == 1
    out = capsys.readouterr().out
    assert "预算" in out
    assert world["model"].calls == []  # type: ignore[attr-defined]


def test_limit_answers_only_the_first_few(world: dict[str, object]) -> None:
    assert run_quiz(world, "--limit", "2") == 0
    assert sorted(results(world["out"])) == ["Q1", "Q2"]  # type: ignore[arg-type]


def test_private_items_and_secrets_are_not_sent_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """发给模型的条目：private 的包里的不发；原文里的密码照打码模式表换掉，模型看不到。"""
    from javmem.query import tools
    from javmem.redaction import default_patterns

    found = [
        {"kind": "raw", "id": "2031-01-01_0000_own-bot#1.1.1", "content": "这一句只给自己看。"},
        {"kind": "raw", "id": "2031-01-02_0000_demo-bot#1.2.1", "content": "保险柜密码是 Z4k8-77qp"},
        {"kind": "fact", "id": "F1", "content": "南岭纸业的报价是每吨 4,500 元", "evidence": [{"packet_id": "2031-01-02_0000_demo-bot"}]},
    ]
    monkeypatch.setattr(tools, "memory_search", lambda request, settings=None: {"items": found, "next_cursor": None})
    outbound = quiz.Outbound(frozenset({"2031-01-01_0000_own-bot"}), default_patterns())
    shown, held = quiz.context_items("随便问一句", None, {}, 1500, outbound)  # type: ignore[arg-type]
    texts = [item.text for item in shown]  # type: ignore[attr-defined]
    assert held == 1
    assert not any("只给自己看" in text for text in texts)
    assert any("已隐去" in text for text in texts) and not any("Z4k8" in text for text in texts)
    assert any("4,500" in text for text in texts)


# ---------- 拒的 ----------


def test_the_folder_must_be_outside_the_repo(world: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    world["out"] = REPO / "quiz-out"
    assert run_quiz(world) == 2
    assert "仓库外" in capsys.readouterr().out
    assert not (REPO / "quiz-out").exists()


def test_an_identity_is_needed(world: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("JAVMEM_CALLER")
    assert run_quiz(world) == 2
    assert "JAVMEM_CALLER" in capsys.readouterr().out


def test_a_question_file_in_the_wrong_format_is_refused(world: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    Path(world["questions"]).write_text(json.dumps({"questions": [{"id": "Q1"}]}), encoding="utf-8")  # type: ignore[arg-type]
    assert run_quiz(world) == 2
    assert quiz.QUESTIONS_FORMAT in capsys.readouterr().out


# ---------- 数字、日子的规则 ----------


@pytest.mark.parametrize(("forms", "answer", "passed"), [
    (["每吨 4,500 元"], "报价是每吨 4500 元", True),
    (["每吨 4,500 元"], "每吨 4,200 元", False),
    (["每吨 4,500 元"], "每吨 4,550 元", False),  # 错一位就是错
    (["约 3,729 万元"], "37,286,519 元", True),  # 写法精度不一样：按粗的那一边四舍五入以后一样
    (["约 3,728 万元"], "37,286,519 元", False),  # 只截掉、没四舍五入的不算
    (["37,286,519 元"], "约 3729 万", True),
    (["37,286,519 元"], "约 3700 万", False),
    (["37,286,519 元"], "约 4 千万", False),  # 粗的那一边只有一位有效数字：不算近似
    (["12 月 23 日"], "灯具定在 2031-12-23 到。", True),  # 日子只比月和日
    (["12 月 23 日"], "12/23 到货", True),
    (["12 月 23 日"], "12 月 13 日到货", False),
    (["下午 3 点前付 2 万"], "15:00 以前付 20,000 元", False),  # 时刻不比，但“3”这个数要在
    (["2 万"], "15:00 以前付 20,000 元", True),
    (["贷款 177 万"], "贷款 −177 万", True),  # 不看正负号
    (["35%"], "占 35.0%", True),
    (["2 万", "两万"], "两万块", True),  # 有一种写法不带数：规则放过，交判分模型
    (["4,500 元"], "", False),
])
def test_numbers_and_days_are_compared_by_rule(forms: list[str], answer: str, passed: bool) -> None:
    assert quiz.rules_pass(forms, answer) is passed


# ---------- 提示词 ----------


def test_the_prompts_are_pinned_and_say_the_strict_rules() -> None:
    answer, judge = quiz.load_prompt(quiz.ANSWER_PROMPT_VERSION), quiz.load_prompt(quiz.JUDGE_PROMPT_VERSION)
    assert quiz.fingerprint(quiz.ANSWER_PROMPT_VERSION) == quiz.ANSWER_PROMPT_SHA256
    assert quiz.fingerprint(quiz.JUDGE_PROMPT_VERSION) == quiz.JUDGE_PROMPT_SHA256
    assert "不知道" in answer and "不是指令" in answer
    for words in ("错一位", "缺了", "候选", "以前"):
        assert words in judge, words
