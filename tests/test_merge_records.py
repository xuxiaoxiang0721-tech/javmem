"""合并记录（J3.6，D100）：别名表旁边只追加的 lexicon.merges.jsonl，读别名表时接在人写的那份后面。
合并接上已有的组、撤销跳过、誊过的跳过、和“不是同一个”对不上报错、写错了报第几行、追加、编号。名字都是编的。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from javmem.config import ConfigError
from javmem.pipeline.merge import name_key
from javmem.zh import lexicon as lex

HUMAN = """format: javmem-lexicon/2
default_where: [investment, personal]
aliases:
  - type: 人
    names: [柏庭舟, 柏廷舟]
  - type: 资产
    names: [KVLT, Kovalt]
apart:
  - {type: 人, names: [Corbin Vale, Corbin Ashe]}
"""


def record(number: int, kind: str = "merge", **fields: object) -> dict[str, object]:
    base: dict[str, object] = {"id": f"M{number:04d}", "kind": kind, "at": "2026-11-14T10:00:00+08:00", "by": "owner", "note": "测试"}
    if kind == "merge":
        base.update(type="人", standard="柏庭舟", names=["Tingzhou"], where=["investment"], from_types=[], old_ids=[], new_id="")
    base.update(fields)
    return base


def files(tmp_path: Path, *records: dict[str, object], human: str = HUMAN) -> Path:
    path = tmp_path / "lexicon.yaml"
    path.write_text(human, encoding="utf-8")
    lines = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
    if records:
        (tmp_path / "lexicon.merges.jsonl").write_text(lines, encoding="utf-8")
    return path


def test_merges_file_sits_next_to_the_lexicon(tmp_path: Path) -> None:
    assert lex.merges_path(tmp_path / "lexicon.yaml") == tmp_path / "lexicon.merges.jsonl"


def test_a_merge_record_joins_the_human_group(tmp_path: Path) -> None:
    lexicon = lex.load_lexicon(files(tmp_path, record(1)))
    group = lexicon.group_of("Tingzhou", "人", ["investment"])
    assert group is not None and group.standard == "柏庭舟" and set(group.names) == {"柏庭舟", "柏廷舟", "Tingzhou"}
    assert group.where is None or set(group.where) >= {"investment", "personal"}  # 人写的组哪个默认分区都算：并集
    assert lexicon.group_of("柏廷舟", "人", ["personal"]) is group
    assert [item["id"] for item in lexicon.records] == ["M0001"]


def test_a_merge_record_alone_makes_a_new_group_with_its_where_and_types(tmp_path: Path) -> None:
    item = record(1, type="资产", standard="NRDX", names=["Nordex", "NRDX 正股"], where=["investment"], from_types=["公司或机构"])
    lexicon = lex.load_lexicon(files(tmp_path, item))
    group = lexicon.group_of("Nordex", "公司或机构", ["investment"])
    assert group is not None and (group.type, group.standard, group.where) == ("资产", "NRDX", ("investment",))
    assert lexicon.group_of("Nordex", "公司或机构", ["personal"]) is None


def test_the_record_standard_name_wins(tmp_path: Path) -> None:
    lexicon = lex.load_lexicon(files(tmp_path, record(1, standard="柏廷舟", names=["柏庭舟"])))
    assert lexicon.group_of("柏庭舟", "人", ["investment"]).standard == "柏廷舟"  # type: ignore[union-attr]


def test_undone_and_copied_records_are_skipped(tmp_path: Path) -> None:
    undone = lex.load_lexicon(files(tmp_path, record(1), record(2, "undo", undoes="M0001")))
    assert undone.group_of("Tingzhou", "人", ["investment"]) is None
    copied = lex.load_lexicon(files(tmp_path, record(1), record(2, "copied", records=["M0001"])))
    assert copied.group_of("Tingzhou", "人", ["investment"]) is None  # 誊进人写的那份了：照人写的
    assert [item["id"] for item in lex.active_records(undone.records)] == []


def test_apart_records_and_their_undo(tmp_path: Path) -> None:
    apart = record(1, "apart", type="人", names=["Rhea Dunmore", "Rhea"])
    lexicon = lex.load_lexicon(files(tmp_path, apart))
    assert lexicon.apart_from(name_key("Rhea"), "人") == {name_key("Rhea Dunmore")}
    again = lex.load_lexicon(files(tmp_path, apart, record(2, "undo", undoes="M0001")))
    assert again.apart_from(name_key("Rhea"), "人") == frozenset()


def test_a_merge_against_a_recorded_apart_is_refused(tmp_path: Path) -> None:
    item = record(1, standard="Corbin Vale", names=["Corbin Ashe"])
    with pytest.raises(ConfigError) as error:
        lex.load_lexicon(files(tmp_path, item))
    assert "M0001" in str(error.value) and "Corbin" not in str(error.value)


def test_fingerprint_follows_the_records(tmp_path: Path) -> None:
    before = lex.load_lexicon(files(tmp_path)).fingerprint
    assert lex.load_lexicon(files(tmp_path, record(1))).fingerprint != before
    assert lex.load_lexicon(files(tmp_path, record(1), record(2, "undo", undoes="M0001"))).fingerprint == before


@pytest.mark.parametrize(
    "bad",
    [
        "{不是 JSON\n",
        json.dumps(record(1, kind="rename")) + "\n",
        json.dumps({**record(1), "id": "7"}) + "\n",
        json.dumps(record(1)) + "\n" + json.dumps(record(1)) + "\n",
        json.dumps(record(1, type="公司")) + "\n",
        json.dumps(record(1, where=["Investment"])) + "\n",
        json.dumps(record(1, from_types=["人"])) + "\n",
        json.dumps(record(1, "undo", undoes="M0009")) + "\n",
        json.dumps(record(1, "apart", type="人", names=["Rhea"])) + "\n",
        json.dumps(record(1)) + "\n" + json.dumps(record(2, "undo", undoes="M0001")) + "\n" + json.dumps(record(3, "undo", undoes="M0001")) + "\n",
        json.dumps({key: value for key, value in record(1).items() if key != "note"}) + "\n",
    ],
)
def test_bad_records_are_reported_by_line_without_names(tmp_path: Path, bad: str) -> None:
    path = files(tmp_path)
    (tmp_path / "lexicon.merges.jsonl").write_text(bad, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        lex.load_lexicon(path)
    message = str(error.value)
    assert "合并记录第" in message and "柏庭舟" not in message and "Tingzhou" not in message


def test_records_need_the_second_format(tmp_path: Path) -> None:
    path = files(tmp_path, record(1), human="aliases:\n  - {type: 人, names: [柏庭舟, 柏廷舟]}\n")
    with pytest.raises(ConfigError):
        lex.load_lexicon(path)


def test_append_and_next_id(tmp_path: Path) -> None:
    path = files(tmp_path)
    assert lex.next_record_id([]) == "M0001"
    lex.append_record(path, record(1))
    lex.append_record(path, record(2, "apart", type="人", names=["Rhea Dunmore", "Rhea"]))
    raw = (tmp_path / "lexicon.merges.jsonl").read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw
    records = lex.read_records(lex.merges_path(path))
    assert [item["id"] for item in records] == ["M0001", "M0002"] and lex.next_record_id(records) == "M0003"


def test_lexicon_with_checks_a_record_before_it_is_written(tmp_path: Path) -> None:
    path = files(tmp_path)
    assert lex.lexicon_with(path, [record(1)]).group_of("Tingzhou", "人", ["investment"]) is not None
    assert not lex.merges_path(path).exists()  # 只是核一遍，没写
    with pytest.raises(ConfigError):
        lex.lexicon_with(path, [record(1, standard="Corbin Vale", names=["Corbin Ashe"])])
