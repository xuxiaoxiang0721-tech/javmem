"""软变化判定对答案（J4.5 的验收）：拿复核过的答案（`javmem-soft-answers/1`）对 judge 写出的结论（`javmem-soft-verdicts/1`）。

答案文件（仓库外，主人复核过的）：

    {"format": "javmem-soft-answers/1",
     "answers": {"C01": {"O1": {"verdict": "B", "review": false, "condition": "…", "effective_at": null,
                                "alternatives": ["E"], "reason": "…"}}}}

`review` 是 true 的：这一条应该进待确认（判不了、该拿不准的）。别的字段只给人看，不打分。

**算法（主人 11-16 定的第 2 题）：** 验收按 L5 的动作算：A、E 都是“不动”，混了不算错；六选一判对几题另列着。

- `action_right`：结论换成的动作（rules.action）和答案的动作一样。没有结论的（不发、调失败了）算错，另数 `not_judged`。
- `verdict_right`：六选一一样（答案该进待确认、结论也进了待确认的，按字母比）。
- 两种最伤人的错：`wrong_change`：动作是暂停、结束、替代，可答案不是这个（不该改的改了，或者改错了样子）；
  `missed_change`：答案是暂停、结束、替代，结论却是不动。
- 按 split 分开数（dev 调试、test 考试），验收看 test 那份。

输出不带原文，只有题号、编号、字母和动作。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from javmem.soft import rules
from javmem.soft.cases import Case, SoftInputError

ANSWERS_FORMAT = "javmem-soft-answers/1"
NO_VERDICT = "-"  # 没有结论


def load_answers(path: Path) -> dict[str, dict[str, dict[str, object]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SoftInputError(f"答案文件读不了：{type(error).__name__}") from None
    if not isinstance(data, dict) or data.get("format") != ANSWERS_FORMAT or not isinstance(data.get("answers"), dict):
        raise SoftInputError(f"答案文件要写成 {{format: {ANSWERS_FORMAT}, answers: {{题号: {{O1: {{verdict, review}}}}}}}}")
    out: dict[str, dict[str, dict[str, object]]] = {}
    for case_id, items in data["answers"].items():
        if not isinstance(items, dict) or not items:
            raise SoftInputError("答案里每道题要写成 {O1: {...}}")
        for ref, answer in items.items():
            if not isinstance(answer, dict) or answer.get("verdict") not in rules.VERDICTS:
                raise SoftInputError("答案的 verdict 只能是 A 到 F")
            if not isinstance(answer.get("review", False), bool):
                raise SoftInputError("答案的 review 要是 true 或 false")
        out[str(case_id)] = {str(ref): dict(answer) for ref, answer in items.items()}
    return out


def load_verdicts(path: Path) -> dict[str, Mapping[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SoftInputError(f"结论文件读不了：{type(error).__name__}") from None
    if not isinstance(data, dict) or data.get("format") != "javmem-soft-verdicts/1" or not isinstance(data.get("cases"), list):
        raise SoftInputError("结论文件要是 javmem soft judge 写出来的（javmem-soft-verdicts/1）")
    return {str(record["case"]): record for record in data["cases"] if isinstance(record, dict) and "case" in record}


def _empty() -> dict[str, object]:
    return {
        "items": 0, "judged": 0, "not_judged": 0, "action_right": 0, "verdict_right": 0, "wrong_change": 0,
        "missed_change": 0, "review_expected": 0, "review_predicted": 0, "review_right": 0, "confusion": {},
    }


def _rate(right: int, total: int) -> float | None:
    return round(100 * right / total, 1) if total else None


def score(cases: Sequence[Case], answers: Mapping[str, Mapping[str, Mapping[str, object]]],
          verdicts: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    """对答案。只算答案里有的（题、旧的那条）；题目里没有的题号报错。"""
    known = {case.id: case for case in cases}
    unknown = sorted(set(answers) - set(known))
    if unknown:
        raise SoftInputError(f"答案里有题目文件没有的题：{'、'.join(unknown)}")
    groups: dict[str, dict[str, object]] = {"total": _empty()}
    rows = []
    for case_id in sorted(answers, key=lambda one: list(known).index(one)):
        case = known[case_id]
        record = verdicts.get(case_id) or {}
        predicted_items = {str(item.get("target")): item for item in record.get("items", []) or [] if isinstance(item, dict)}  # type: ignore[union-attr]
        refs = {item.ref for item in case.old}
        for ref, answer in answers[case_id].items():
            if ref not in refs:
                raise SoftInputError(f"答案里 {case_id} 的 {ref} 不是这道题的旧的")
            expected_letter = str(answer["verdict"])
            expected = rules.expected_action(expected_letter, review=bool(answer.get("review", False)))
            item = predicted_items.get(ref)
            if item is None:
                predicted_letter, predicted = NO_VERDICT, None
            else:
                predicted_letter = str(item.get("verdict"))
                action = item.get("action") if isinstance(item.get("action"), dict) else rules.action(item, said_at=case.said_at).as_dict()
                predicted = str(action["kind"])  # type: ignore[index]
            row = {
                "case": case_id, "target": ref, "split": case.split, "expected": expected_letter, "expected_action": expected,
                "predicted": predicted_letter, "predicted_action": predicted,
                "action_right": predicted == expected, "verdict_right": predicted_letter == expected_letter,
            }
            rows.append(row)
            for name in ("total", f"split:{case.split or 'none'}"):
                group = groups.setdefault(name, _empty())
                group["items"] += 1  # type: ignore[operator]
                group["judged" if predicted is not None else "not_judged"] += 1  # type: ignore[operator]
                group["action_right"] += row["action_right"]  # type: ignore[operator]
                group["verdict_right"] += row["verdict_right"]  # type: ignore[operator]
                group["wrong_change"] += predicted in rules.CHANGES and predicted != expected  # type: ignore[operator]
                group["missed_change"] += expected in rules.CHANGES and predicted == rules.KEEP  # type: ignore[operator]
                group["review_expected"] += expected == rules.REVIEW  # type: ignore[operator]
                group["review_predicted"] += predicted == rules.REVIEW  # type: ignore[operator]
                group["review_right"] += expected == predicted == rules.REVIEW  # type: ignore[operator]
                confusion = group["confusion"]
                assert isinstance(confusion, dict)
                confusion.setdefault(expected_letter, {})
                confusion[expected_letter][predicted_letter] = confusion[expected_letter].get(predicted_letter, 0) + 1
    for group in groups.values():
        group["action_rate"] = _rate(group["action_right"], group["items"])  # type: ignore[arg-type]
        group["verdict_rate"] = _rate(group["verdict_right"], group["items"])  # type: ignore[arg-type]
    return {"groups": groups, "items": rows}


def report_lines(result: Mapping[str, object]) -> list[str]:
    """给人看的几行：每一组的数，加六选一的混淆表。"""
    lines = []
    groups = result["groups"]
    assert isinstance(groups, dict)
    for name, group in groups.items():
        lines.append(
            f"[{name}] {group['items']} 项：动作判对 {group['action_right']}（{group['action_rate']}%），"
            f"六选一判对 {group['verdict_right']}（{group['verdict_rate']}%）；不该改的改了 {group['wrong_change']}，"
            f"该改的没改 {group['missed_change']}；该进待确认 {group['review_expected']}、进了 {group['review_predicted']}、"
            f"对上 {group['review_right']}；没有结论 {group['not_judged']}"
        )
    total = groups["total"]
    letters = list(rules.VERDICTS) + [NO_VERDICT]
    lines.append("混淆表（行是答案，列是结论）：" + " ".join(letters))
    for expected in rules.VERDICTS:
        row = total["confusion"].get(expected, {})
        if row:
            lines.append(f"  {expected}: " + " ".join(str(row.get(letter, 0)) for letter in letters))
    return lines
