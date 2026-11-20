"""合并工具（J3.6，D100）第五段：拆开。L5 按别名并错了的（一个简称挂在全名上，后来又按这个简称对过去），
--detach 实体 --alias 叫法 记成“这个叫法不是这个实体”，再把这个实体的说法、这个叫法的出处所在的窗口重写。
撤销拆开、点错了的不写。名字和句子都是编的，从头写的。"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import World
from spotter import SpotModel, extract_into_store
from test_write import day_packet

from javmem.cli import main
from javmem.pipeline.merge import entity_id_of, name_key
from javmem.zh import lexicon as lex

TABLE = [("Dunmore", [("Rhea Dunmore", "人", "Rhea")]), ("Rhea", [("Rhea", "人", "Rhea")])]
LINES = [("2026-10-02", "我：Rhea Dunmore 接手了物业。"), ("2026-10-05", "我：Rhea 明天来量窗帘。")]
DUNMORE, RHEA = entity_id_of("人", name_key("Rhea Dunmore")), entity_id_of("人", name_key("Rhea"))


@pytest.fixture
def rhea_world(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[World, Path]:
    for day, line in LINES:
        world.drop("inbox/desk-bot", day_packet(day, line, domain="[personal]"))
    world.intake()
    extract_into_store(world, SpotModel(TABLE))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    path = tmp_path / "lexicon.yaml"
    path.write_text("format: javmem-lexicon/2\n", encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    return world, path


def subjects(world: World) -> dict[str, str]:
    return {row["content"]: row["subject_entity_id"] for row in world.rows("SELECT content, subject_entity_id FROM facts")}


def test_detach_takes_an_alias_off_an_entity_and_rewrites(rhea_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = rhea_world
    assert subjects(world) == {"Rhea Dunmore 接手了物业": DUNMORE, "Rhea 明天来量窗帘": DUNMORE}  # 按简称对过去了
    assert main(["entities", "--library", "prod", "--detach", DUNMORE, "--alias", "Rhea", "--note", "测试第 1 条"]) == 0
    assert "记下了 M0001（apart）" in capsys.readouterr().out
    assert subjects(world) == {"Rhea Dunmore 接手了物业": DUNMORE, "Rhea 明天来量窗帘": RHEA}
    record = lex.read_records(lex.merges_path(path))[0]
    assert (record["kind"], record["names"], record["entity"], record["alias"]) == ("apart", ["Rhea Dunmore", "Rhea"], DUNMORE, "Rhea")
    assert [tuple(row) for row in world.rows("SELECT merge_id, kind, old_ids FROM entity_merges")] == [("M0001", "apart", f'["{DUNMORE}"]')]
    # 撤销拆开：又按简称对回全名上
    assert main(["entities", "--library", "prod", "--undo", "M0001", "--note", "测试第 2 条"]) == 0
    assert subjects(world) == {"Rhea Dunmore 接手了物业": DUNMORE, "Rhea 明天来量窗帘": DUNMORE}


def test_a_wrong_detach_writes_nothing(rhea_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = rhea_world
    assert main(["entities", "--library", "prod", "--detach", "E0000000000000000", "--alias", "Rhea", "--note", "测试"]) == 1
    assert main(["entities", "--library", "prod", "--detach", DUNMORE, "--alias", "Rhe", "--note", "测试"]) == 1  # 要一字不差
    assert main(["entities", "--library", "prod", "--detach", DUNMORE, "--alias", "Rhea Dunmore", "--note", "测试"]) == 1  # 标准名
    assert main(["entities", "--library", "prod", "--detach", DUNMORE, "--note", "测试"]) == 2  # 没写 --alias
    err = capsys.readouterr().err
    assert "没有这个实体" in err and "没有这个叫法" in err and "标准名" in err
    assert not lex.merges_path(path).exists()
