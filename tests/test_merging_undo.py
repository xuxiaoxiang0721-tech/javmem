"""合并工具（J3.6，D100）第四段：撤销。追加一条撤销记录，受影响的窗口照没有那一条的别名表重写：编号是按名字算的，
旧实体重写出来还是原来的编号；跳转删掉；冻结标记跟着回来；撤销了再整本重建一样；撤过的、没有的不能撤。名字都是编的。"""

from __future__ import annotations

import pytest
from test_merging import (  # noqa: F401
    LEXICON_TEXT,
    STANDARD,
    TYPO,
    facts_by_content,
    ledger_view,
    merged_world,
    proposal_of,
    rebuild_world,
)

from javmem.cli import main
from javmem.entities import find_proposals
from javmem.ledger import open_ledger
from javmem.zh import lexicon as lex

NO_GROUP = LEXICON_TEXT.replace("  - type: 人\n    names: [柏庭舟, 柏廷舟]\n", "")


def typo_merge(world, path) -> list[str]:
    """人写的别名表里没有这一组：照账本里找的提议（中文名只差一个字）合，标准名用后一个写法。"""
    path.write_text(NO_GROUP, encoding="utf-8")
    return ["entities", "--library", "prod", "--merge", proposal_of(world, path, "柏廷舟"), "--name", "柏庭舟"]


def test_undo_splits_the_merge_back(merged_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = merged_world
    before = ledger_view(world)
    command = typo_merge(world, path)
    proposal = command[4]
    old_frozen = facts_by_content(world)["柏廷舟负责资金池"]["fact_id"]
    assert main(["freeze", "--library", "prod", "--fact", old_frozen]) == 0
    assert main([*command, "--note", "测试第 1 条"]) == 0
    assert [row["entity_id"] for row in world.rows("SELECT * FROM entities WHERE type = '人'")] == [STANDARD]
    assert main(["entities", "--library", "prod", "--undo", "M0001", "--note", "测试第 2 条"]) == 0
    out = capsys.readouterr().out
    assert "记下了 M0002（undo）" in out and f"跳转删掉了：{TYPO}" in out
    names = {row["entity_id"]: row["canonical_name"] for row in world.rows("SELECT * FROM entities WHERE type = '人'")}
    assert names == {TYPO: "柏廷舟", STANDARD: "柏庭舟"}  # 按名字算的编号：还是原来那两个
    facts = facts_by_content(world)
    assert facts["柏廷舟负责资金池"]["fact_id"] == old_frozen and facts["柏廷舟负责资金池"]["frozen"] == 1  # 冻结跟着回来
    assert world.rows("SELECT count(*) AS n FROM entity_redirects")[0]["n"] == 0
    assert world.rows("SELECT undone_at FROM entity_merges WHERE merge_id = 'M0001'")[0]["undone_at"] is not None
    conn = open_ledger(world.settings.library("prod"))
    assert proposal in {item.proposal_id for item in find_proposals(conn, lex.load_lexicon(path))}  # 提议又出来了
    conn.close()
    after = ledger_view(world)
    assert after["entities"] == before["entities"] and after["aliases"] == before["aliases"]
    assert main(["entities", "--library", "prod", "--history"]) == 0
    history = capsys.readouterr().out
    assert "M0001 合并" in history and "撤销了" in history and "M0002 撤销" in history


def test_rebuilding_after_an_undo_gives_the_same_ledger(merged_world, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, path = merged_world
    assert main([*typo_merge(world, path), "--note", "测试"]) == 0
    assert main(["entities", "--library", "prod", "--merge", proposal_of(world, path, "KVLT"), "--note", "测试"]) == 0
    assert main(["entities", "--library", "prod", "--undo", "M0001", "--note", "测试"]) == 0
    current = ledger_view(world)
    assert len(world.rows("SELECT * FROM entities WHERE type = '人'")) == 2
    rebuilt = rebuild_world(tmp_path, monkeypatch, world.data / "l3")
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert main(["entities", "--library", "prod", "--sync"]) == 0
    assert ledger_view(rebuilt) == current


def test_only_a_record_still_in_effect_can_be_undone(merged_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = merged_world
    assert main(["entities", "--library", "prod", "--undo", "M0001", "--note", "测试"]) == 1
    assert "没有这条还算数的合并记录" in capsys.readouterr().err
    assert main([*typo_merge(world, path), "--note", "测试"]) == 0
    assert main(["entities", "--library", "prod", "--undo", "M0001", "--note", "测试"]) == 0
    assert main(["entities", "--library", "prod", "--undo", "M0001", "--note", "测试"]) == 1  # 撤过的不能再撤
    assert main(["entities", "--library", "prod", "--undo", "M0001"]) == 2  # 没写 --note
    assert [record["kind"] for record in lex.read_records(lex.merges_path(path))] == ["merge", "undo"]
