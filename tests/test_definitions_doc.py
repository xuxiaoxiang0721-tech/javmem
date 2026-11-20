"""定义卡（docs/definitions.md）和代码对得上：七类实体、七种关系和两头允许的类型、关系的状态、事实的五类、
变化和别名的种类。

卡上的表是人照着标金标准的；代码里的清单是抽取、核对、打分、`javmem gold check` 照着的。哪一边改了、另一边没跟上，
这里红。卡上的例句是编的，这里管不了；只看卡上没有内部的编号（对外的文档不带）。
"""

from __future__ import annotations

import re
from pathlib import Path

from javmem.pipeline.extract import RELATION_STATES
from javmem.pipeline.relcheck import ALLOWED
from javmem.scoring.common import ENTITY_TYPES, RELATION_TYPES
from javmem.scoring.gold import FACT_CLASSES, NOT_ALIAS_KINDS
from javmem.scoring.report import ALIAS_KINDS, CHANGE_KINDS

DOC = Path(__file__).resolve().parents[1] / "docs" / "definitions.md"
INTERNAL_ID = re.compile(r"(?<![A-Za-z0-9])(?:D\d{2,3}|J\d+(?:\.\d+)?|U\d)(?![0-9])")
SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$")


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def table_after(heading: str) -> list[list[str]]:
    """标题（任何一级，写着 heading 的那一行）下面第一张表，去掉表头，每行一个格子的列表。"""
    lines = _text().splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("#") and heading in line), None)
    assert start is not None, f"卡上没有“{heading}”这一节"
    rows: list[list[str]] = []
    seen_table = False
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("#"):
            break
        if line.startswith("|"):
            seen_table = True
            if SEPARATOR.match(line) or (i + 1 < len(lines) and SEPARATOR.match(lines[i + 1])):
                continue  # 表头和分隔线
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif seen_table:
            break
    assert rows, f"“{heading}”下面没有表"
    return rows


def _plain(cell: str) -> str:
    return cell.strip().strip("*`").strip()


def test_the_seven_entity_types_are_the_ones_the_code_uses():
    assert [_plain(row[0]) for row in table_after("七类实体")] == list(ENTITY_TYPES)


def test_the_seven_relations_and_their_allowed_ends_match_the_check():
    found: dict[str, set[tuple[str, str]]] = {}
    for row in table_after("两头允许的类型"):
        pairs = set()
        for pair in re.split(r"[、，,；;]", row[1]):
            if pair.strip():
                subject, _, obj = pair.partition("→")
                pairs.add((_plain(subject), _plain(obj)))
        found[_plain(row[0])] = pairs
    assert list(found) == list(RELATION_TYPES)
    assert found == {relation: set(ALLOWED[relation]) for relation in RELATION_TYPES}


def test_every_relation_has_its_own_card():
    text = _text()
    for relation in RELATION_TYPES:
        assert re.search(rf"^### {relation}(?:\s|（|$)", text, re.M), f"{relation}没有自己的卡"


def test_the_relation_states_are_the_ones_the_code_uses():
    assert [_plain(row[0]) for row in table_after("关系的状态")] == list(RELATION_STATES)


def test_the_five_fact_classes_are_the_ones_scoring_reads():
    assert [_plain(row[0]) for row in table_after("事实的五类")] == list(FACT_CLASSES)


def test_the_change_kinds_are_the_ones_scoring_reads():
    assert [_plain(row[0]) for row in table_after("变化的种类")] == list(CHANGE_KINDS)


def test_the_alias_kinds_are_the_ones_scoring_reads_and_say_which_are_not_aliases():
    rows = table_after("别名的种类")
    assert [_plain(row[0]) for row in rows] == list(ALIAS_KINDS)
    for row in rows:
        if _plain(row[0]) in NOT_ALIAS_KINDS:
            assert "不算别名" in row[1], f"{row[0]}要写明不算别名"


def test_the_page_carries_no_internal_ids():
    hits = [match.group(0) for match in INTERNAL_ID.finditer(_text())]
    assert hits == []


def test_the_checks_here_would_catch_a_page_out_of_step(tmp_path, monkeypatch):
    """自己核一下：卡上少写一种关系的一头，上面那条会红。"""
    import sys

    me = sys.modules[__name__]
    broken = _text().replace("人 → 资产、", "", 1)
    assert broken != _text()
    fake = tmp_path / "definitions.md"
    fake.write_text(broken, encoding="utf-8")
    monkeypatch.setattr(me, "DOC", fake)
    try:
        test_the_seven_relations_and_their_allowed_ends_match_the_check()
    except AssertionError:
        return
    raise AssertionError("少写一头也没红")
