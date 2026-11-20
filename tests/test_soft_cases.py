"""软变化的题目文件（J4.5，javmem-soft-cases/1）：读写一样，写错了报错且不带原文。例子都是编的。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from javmem.soft.cases import FORMAT, Case, Line, OldItem, SoftInputError, cases_from_dict, dump_cases, load_cases


def sample() -> Case:
    return Case(
        "Q01",
        (OldItem("O1", "relation", "林工 负责 东湖那批订单", "生效", "2026-10-12", "2026-11-02"),),
        (
            Line("T1", "2026-11-07T10:28:00+08:00", "我", "owner", "东湖那批客户还没回样品意见。"),
            Line("T2", "2026-11-07T10:30:00+08:00", "我", "owner", "东湖那批先停一下，等客户回复再说。", sid="P#1.2.1"),
        ),
        ("T2",),
        packet_date="2026-11-07",
        split="dev",
        meta={"note": "编的"},
    )


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    dump_cases([sample()], path)
    loaded = load_cases(path)
    assert loaded == [sample()]
    assert loaded[0].said_at == "2026-11-07T10:30:00+08:00"
    assert [line.ref for line in loaded[0].target_lines] == ["T2"]


def raw(**changes: object) -> dict[str, object]:
    case = sample().as_dict()
    case.update(changes)
    return {"format": FORMAT, "cases": [case]}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"target": ["T9"]}, "target"),
        ({"old": []}, "old"),
        ({"lines": [{"ref": "T1", "at": "2026-11-07 10:28", "speaker": "我", "role": "owner", "text": "先停一下"}],
          "target": ["T1"]}, "at"),
        ({"lines": [{"ref": "T1", "at": "2026-11-07T10:28:00+08:00", "speaker": "我", "role": "boss", "text": "先停一下"}],
          "target": ["T1"]}, "role"),
        ({"old": [{"ref": "X1", "kind": "fact", "text": "东湖那批出货日期 11-20"}]}, "ref"),
        ({"old": [{"ref": "O1", "kind": "event", "text": "东湖那批出货日期 11-20"}]}, "kind"),
        ({"split": "train"}, "split"),
        ({"id": "有空格 的题"}, "id"),
        ({"surprise": 1}, "认不出"),
    ],
)
def test_mistakes_are_reported_without_the_text(changes: dict[str, object], message: str) -> None:
    with pytest.raises(SoftInputError) as error:
        cases_from_dict(raw(**changes))
    assert message in str(error.value)
    assert "先停一下" not in str(error.value) and "东湖" not in str(error.value)


def test_duplicate_ids_and_wrong_format(tmp_path: Path) -> None:
    case = sample().as_dict()
    with pytest.raises(SoftInputError):
        cases_from_dict({"format": FORMAT, "cases": [case, case]})
    with pytest.raises(SoftInputError):
        cases_from_dict({"format": "something-else", "cases": []})
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SoftInputError):
        load_cases(path)
    assert json.loads(json.dumps(sample().as_dict(), ensure_ascii=False))["old"][0]["ref"] == "O1"
