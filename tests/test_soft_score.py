"""软变化判定对答案（J4.5，javmem.soft.score）：按 L5 的动作算（A、E 一样），六选一另列，两种最伤人的错，分 dev、test。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from javmem.soft import rules
from javmem.soft.cases import Case, Line, OldItem, SoftInputError
from javmem.soft.score import ANSWERS_FORMAT, load_answers, load_verdicts, report_lines, score

SAID = "2026-11-07T10:30:00+08:00"


def case(case_id: str, split: str | None = "test") -> Case:
    return Case(case_id, (OldItem("O1", "relation", "林工 负责 东湖那批订单"),),
                (Line("T1", SAID, "我", "owner", "东湖那批先停一下。"),), ("T1",), split=split)


def verdict(letter: str, **fields: object) -> dict[str, object]:
    item: dict[str, object] = {"target": "O1", "about": True, "verdict": letter, "condition": None, "effective_at": None,
                               "effective_precision": "unknown", "new_value": None, "sure": True, "finance": False,
                               "quote": "先停一下", "reason": "", "quote_ok": True, "consistent": True, "date_ok": True}
    item.update(fields)
    item["action"] = rules.action(item, said_at=SAID).as_dict()
    return item


def records(**letters: object) -> dict[str, dict[str, object]]:
    out = {}
    for case_id, value in letters.items():
        items = [] if value is None else [value if isinstance(value, dict) else verdict(str(value))]
        out[case_id] = {"case": case_id, "items": items}
    return out


def answers(**letters: str | tuple[str, bool]) -> dict[str, dict[str, dict[str, object]]]:
    out = {}
    for case_id, value in letters.items():
        letter, review = value if isinstance(value, tuple) else (value, False)
        out[case_id] = {"O1": {"verdict": letter, "review": review}}
    return out


def test_still_valid_and_hesitation_count_as_the_same_action() -> None:
    cases = [case("S1"), case("S2")]
    result = score(cases, answers(S1="A", S2="E"), records(S1="E", S2="A"))
    total = result["groups"]["total"]  # type: ignore[index]
    assert (total["action_right"], total["verdict_right"], total["action_rate"], total["verdict_rate"]) == (2, 0, 100.0, 0.0)


def test_the_two_harmful_mistakes_are_counted() -> None:
    cases = [case("S1"), case("S2"), case("S3")]
    result = score(cases, answers(S1="A", S2="B", S3="B"), records(S1="C", S2="E", S3="C"))
    total = result["groups"]["total"]  # type: ignore[index]
    assert total["wrong_change"] == 2  # 不该改的改了（S1），改错了样子（S3：该暂停的判成结束）
    assert total["missed_change"] == 1  # 该暂停的判成不动（S2）
    assert total["action_right"] == 0


def test_review_and_missing_verdicts() -> None:
    cases = [case("S1"), case("S2"), case("S3")]
    result = score(
        cases,
        answers(S1="F", S2=("B", True), S3="B"),
        records(S1="F", S2=verdict("B", sure=False), S3=None),
    )
    total = result["groups"]["total"]  # type: ignore[index]
    assert (total["review_expected"], total["review_predicted"], total["review_right"]) == (2, 2, 2)
    assert total["not_judged"] == 1 and total["action_right"] == 2  # 没有结论的算错
    rows = {row["case"]: row for row in result["items"]}  # type: ignore[union-attr]
    assert rows["S3"]["predicted"] == "-" and rows["S3"]["predicted_action"] is None


def test_splits_are_counted_apart() -> None:
    cases = [case("S1", "dev"), case("S2", "test"), case("S3", None)]
    result = score(cases, answers(S1="B", S2="B", S3="B"), records(S1="B", S2="A", S3="B"))
    groups = cast("dict[str, dict[str, int]]", result["groups"])
    assert groups["split:dev"]["action_right"] == 1 and groups["split:test"]["action_right"] == 0
    assert groups["split:none"]["items"] == 1 and groups["total"]["items"] == 3
    lines = report_lines(result)
    assert lines[0].startswith("[total] 3 项") and any(line.startswith("  B:") for line in lines)
    assert "东湖" not in "\n".join(lines)  # 不带原文


def test_answers_for_unknown_cases_or_items_are_errors(tmp_path: Path) -> None:
    with pytest.raises(SoftInputError):
        score([case("S1")], answers(S9="B"), records())
    with pytest.raises(SoftInputError):
        score([case("S1")], {"S1": {"O7": {"verdict": "B"}}}, records())
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"format": ANSWERS_FORMAT, "answers": {"S1": {"O1": {"verdict": "Z"}}}}), encoding="utf-8")
    with pytest.raises(SoftInputError):
        load_answers(path)
    path.write_text(json.dumps({"format": ANSWERS_FORMAT, "answers": {"S1": {"O1": {"verdict": "B", "review": False}}}}),
                    encoding="utf-8")
    assert load_answers(path) == {"S1": {"O1": {"verdict": "B", "review": False}}}
    bad = tmp_path / "verdicts.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(SoftInputError):
        load_verdicts(bad)
