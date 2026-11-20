"""校准工具包（U3，D141 ③）：自己做金标准的工具。这一份管 `javmem gold check`：核一份金标准写得对不对。

用 GitHub 那一层回归的编的小套件（tests/fixtures/regress-mini/）：它的金标准本来就是对的；改坏一处，就要报出那一条、
哪一种不对。输出只有条目编号和种类，不抄内容。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from javmem.cli import main

MINI = Path(__file__).resolve().parent / "fixtures" / "regress-mini"


@pytest.fixture(scope="module")
def mini_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """编的小套件的 5 个包收进测试库（不抽取：核金标准只要原文句）。"""
    root = tmp_path_factory.mktemp("gold-check")
    data = root / "data"
    with pytest.MonkeyPatch.context() as patch:
        for name in [name for name in os.environ if name.startswith("JAVMEM_")]:
            patch.delenv(name)
        patch.setenv("JAVMEM_DATA_DIR", str(data))
        patch.setenv("JAVMEM_SOURCES_FILE", str(MINI / "config" / "sources.yaml"))
        patch.setenv("JAVMEM_TIMEZONE", "+08:00")
        assert main(["init"]) == 0
        inbox = data / "inbox-test" / "demo-bot"
        inbox.mkdir(parents=True, exist_ok=True)
        for packet in sorted((MINI / "packets" / "demo-bot").glob("*.md")):
            shutil.copy2(packet, inbox / packet.name)
        assert main(["intake"]) == 0
    return data


@pytest.fixture
def use(mini_data: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(mini_data))
    monkeypatch.setenv("JAVMEM_SOURCES_FILE", str(MINI / "config" / "sources.yaml"))
    return mini_data


def broken(tmp_path: Path, change) -> Path:  # noqa: ANN001 - change 改一份金标准
    folder = tmp_path / "gold"
    shutil.copytree(MINI / "gold", folder)
    unit = json.loads((folder / "D1.json").read_text(encoding="utf-8"))
    change(unit)
    (folder / "D1.json").write_text(json.dumps(unit, ensure_ascii=False, indent=1), encoding="utf-8")
    return folder


def item(unit: dict, item_id: str) -> dict:
    return next(one for one in unit["items"] if one["id"] == item_id)


def test_the_made_up_gold_passes(use: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gold", "check", "--gold", str(MINI / "gold"), "--library", "test"]) == 0
    out = capsys.readouterr().out
    assert "1 个单元、28 条" in out and "有问题的 0 条" in out


@pytest.mark.parametrize(("item_id", "change", "kind"), [
    ("D1-F001", lambda one: one["evidence"][0].update(sid="2026-10-05_2300_demo-bot#9.9.9"), "出处的句子账本里没有"),
    ("D1-F001", lambda one: one["evidence"][0].update(quote="不是这一句里的字"), "原话片段不是那一句的一段"),
    ("D1-R001", lambda one: one.update(relation="管着"), "关系不是那七种"),
    ("D1-R001", lambda one: one["object"].update(type="地点"), "关系两头的类型不对"),
    ("D1-R001", lambda one: one["object"].update(name="没有这个项目"), "名字不在实体表里"),
    ("D1-F001", lambda one: one.update(fact_class="随便写的"), "事实的分类不对"),
    ("D1-F001", lambda one: one["when"].update(precision="半天"), "日子的精度不对"),
    ("D1-F001", lambda one: one["subject"].update(type="动物"), "实体的类型不对"),
])
def test_each_kind_of_mistake_is_named(use: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                       item_id: str, change, kind: str) -> None:  # noqa: ANN001
    folder = broken(tmp_path, lambda unit: change(item(unit, item_id)))
    assert main(["gold", "check", "--gold", str(folder), "--library", "test"]) == 1
    out = capsys.readouterr().out
    assert f"{item_id}：{kind}" in out
    assert "南岭纸业" not in out and "梧桐路展厅项目" not in out  # 只打编号和种类，不抄内容


def test_evidence_outside_the_unit_is_named(use: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def change(unit: dict) -> None:
        unit["packets"] = unit["packets"][1:]  # 出处那一包不算这个单元的了
        unit["revisions"].pop("2026-10-05_2300_demo-bot", None)
    folder = broken(tmp_path, change)
    assert main(["gold", "check", "--gold", str(folder), "--library", "test"]) == 1
    assert "D1-F001：出处不在这个单元的包里" in capsys.readouterr().out


def test_a_structural_mistake_cannot_be_read(use: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = broken(tmp_path, lambda unit: item(unit, "D1-R001").pop("relation"))
    assert main(["gold", "check", "--gold", str(folder), "--library", "test"]) == 2
    assert "读不了" in capsys.readouterr().out


def test_without_a_data_dir_only_the_ledger_checks_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("JAVMEM_DATA_DIR", raising=False)
    folder = broken(tmp_path, lambda unit: item(unit, "D1-R001").update(relation="管着"))
    assert main(["gold", "check", "--gold", str(folder), "--library", "test"]) == 1
    out = capsys.readouterr().out
    assert "D1-R001：关系不是那七种" in out and "没有数据目录" in out
