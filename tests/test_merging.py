"""合并工具（J3.6，D100）第三段：合并当场生效。受影响的窗口从结果库重写（不调模型），组里的名字对到标准名的编号；
合并以后整本重建和当场合的一样；冻结标记和主人的决定跟着走；挂错了叫法的那一句改回来；旧编号记进跳转表；
卡片上“合并以后”是一个事务里合一遍再撤回、账本不动；合不了的什么都不写。名字和句子都是编的，从头写的。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from helpers import World
from spotter import SpotModel, extract_into_store
from test_write import day_packet, dump

from javmem.cli import main
from javmem.config import Settings
from javmem.entities import find_proposals
from javmem.ledger import open_ledger
from javmem.pipeline.merge import entity_id_of, name_key
from javmem.zh import lexicon as lex

TABLE = [
    ("柏廷舟", [("柏廷舟", "人", "柏廷舟")]),
    ("柏庭舟", [("柏庭舟", "人", "柏庭舟")]),
    ("累购", [("KVLT 累购", "资产", "KVLT")]),
    ("Kovalt", [("Kovalt", "公司或机构", "Kovalt")]),
    ("KVLT", [("KVLT", "资产", "KVLT")]),
]
VALUES = [("资金池", "负责", "资金池"), ("对账", "负责", "对账"), ("报价", "负责", "报价")]
LINES = [
    ("2026-09-01", "我：柏廷舟负责资金池。"),
    ("2026-09-02", "我：KVLT 累购这周敲了两次。"),
    ("2026-09-03", "我：柏廷舟改成负责对账。"),
    ("2026-09-04", "我：Kovalt 的财报下周出。"),
    ("2026-09-05", "我：KVLT 收在 41.2。"),
    ("2026-09-06", "我：柏庭舟以后负责报价。"),
]
LEXICON_TEXT = """format: javmem-lexicon/2
default_where: [investment]
aliases:
  - type: 人
    names: [柏庭舟, 柏廷舟]
  - type: 资产
    names: [KVLT, Kovalt]
    from_types: [公司或机构]
"""
TYPO, STANDARD = entity_id_of("人", name_key("柏廷舟")), entity_id_of("人", name_key("柏庭舟"))


def deliver_all(world: World) -> None:
    for day, line in LINES:
        world.drop("inbox/desk-bot", day_packet(day, line, domain="[investment]"))
    world.intake()
    extract_into_store(world, SpotModel(TABLE, VALUES))


@pytest.fixture
def merged_world(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[World, Path]:
    """没配别名表写的账（两个写法各一个实体），再配上第二版的别名表。"""
    deliver_all(world)
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    path = tmp_path / "config" / "lexicon.yaml"
    path.parent.mkdir()
    path.write_text(LEXICON_TEXT, encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    return world, path


def proposal_of(world: World, path: Path, name: str) -> str:
    conn = open_ledger(world.settings.library("prod"))
    try:
        return next(item.proposal_id for item in find_proposals(conn, lex.load_lexicon(path)) if item.name == name)
    finally:
        conn.close()


def facts_by_content(world: World) -> dict[str, sqlite3.Row]:
    return {row["content"]: row for row in world.rows("SELECT * FROM facts")}


def test_merge_takes_effect_at_once(merged_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = merged_world
    proposal = proposal_of(world, path, "柏庭舟")
    assert main(["entities", "--library", "prod", "--merge", proposal, "--note", "测试第 1 条"]) == 0
    out = capsys.readouterr().out
    assert "记下了 M0001（merge）" in out and "在 prod 库上生效：M0001" in out and "不调模型" in out
    names = {row["entity_id"]: row["canonical_name"] for row in world.rows("SELECT * FROM entities WHERE type = '人'")}
    assert names == {STANDARD: "柏庭舟"}
    facts = facts_by_content(world)
    assert {facts[text]["subject_entity_id"] for text in ("柏廷舟负责资金池", "柏廷舟改成负责对账", "柏庭舟以后负责报价")} == {STANDARD}
    aliases = {row["alias"] for row in world.rows("SELECT alias FROM aliases WHERE entity_id = ?", STANDARD)}
    assert aliases == {"柏廷舟", "柏庭舟"}
    # 同一个主体、同一个属性：后说的替代先说的（新旧重算了）
    assert facts["柏廷舟负责资金池"]["superseded_by"] == facts["柏廷舟改成负责对账"]["fact_id"]
    assert facts["柏廷舟改成负责对账"]["superseded_by"] == facts["柏庭舟以后负责报价"]["fact_id"]
    assert [tuple(row) for row in world.rows("SELECT old_id, new_id, merge_id FROM entity_redirects")] == [(TYPO, STANDARD, "M0001")]
    record = lex.read_records(lex.merges_path(path))[0]
    assert (record["kind"], record["standard"], record["names"], record["old_ids"], record["new_id"]) == (
        "merge", "柏庭舟", ["柏廷舟"], [TYPO], STANDARD
    )
    assert record["where"] == ["investment"] and record["proposal"] == proposal
    assert not (path.parent / "lexicon.yaml").read_text(encoding="utf-8").count("M0001")  # 人写的那份不动
    # 合完了：这一条提议没了；巡检不报别名表改了（当场生效的记了指纹）
    conn = open_ledger(world.settings.library("prod"))
    assert proposal not in {item.proposal_id for item in find_proposals(conn, lex.load_lexicon(path))}
    conn.close()
    settings = Settings.from_env()
    from javmem.patrol import run_patrol

    assert not [item for item in run_patrol(settings) if item.kind == "lexicon_changed"]


def test_a_merge_fixes_the_sentence_aligned_to_the_wrong_entity(merged_world) -> None:
    world, path = merged_world
    proposal = proposal_of(world, path, "KVLT")
    accumulator = entity_id_of("资产", name_key("KVLT 累购"))
    assert facts_by_content(world)["KVLT 收在 41.2"]["subject_entity_id"] == accumulator  # 按挂着的叫法对错了
    assert main(["entities", "--library", "prod", "--merge", proposal, "--note", "测试第 2 条"]) == 0
    kvlt = entity_id_of("资产", name_key("KVLT"))
    facts = facts_by_content(world)
    assert facts["KVLT 收在 41.2"]["subject_entity_id"] == kvlt  # 那个窗口重写了：KVLT 是组里的名字
    assert facts["Kovalt 的财报下周出"]["subject_entity_id"] == kvlt  # 写成公司或机构的也并进来，改成资产
    assert facts["KVLT 累购这周敲了两次"]["subject_entity_id"] == accumulator  # 累购没勾：还是它自己
    row = world.rows("SELECT canonical_name, type FROM entities WHERE entity_id = ?", kvlt)[0]
    assert (row["canonical_name"], row["type"]) == ("KVLT", "资产")


def test_frozen_marks_and_decisions_follow_the_merge(merged_world) -> None:
    world, path = merged_world
    facts = facts_by_content(world)
    old_frozen = facts["柏廷舟负责资金池"]["fact_id"]
    assert main(["freeze", "--library", "prod", "--fact", old_frozen]) == 0
    open_conflicts = world.rows("SELECT claims FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL")
    assert [json.loads(row["claims"])["frozen"] for row in open_conflicts] == [old_frozen]  # 合以前：和后说的对账冲突
    proposal = proposal_of(world, path, "柏庭舟")
    assert main(["entities", "--library", "prod", "--merge", proposal, "--note", "测试第 3 条"]) == 0
    facts = facts_by_content(world)
    new_frozen = facts["柏廷舟负责资金池"]
    assert new_frozen["fact_id"] != old_frozen and new_frozen["frozen"] == 1 and new_frozen["superseded_by"] is None
    assert not world.rows("SELECT 1 FROM facts WHERE fact_id = ?", old_frozen)  # 旧的空壳删了
    conflicts = [json.loads(row["claims"]) for row in world.rows("SELECT claims FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL")]
    expected = {(new_frozen["fact_id"], facts["柏廷舟改成负责对账"]["fact_id"]), (new_frozen["fact_id"], facts["柏庭舟以后负责报价"]["fact_id"])}
    assert {(item["frozen"], item["new"]) for item in conflicts} == expected  # 原来那条改指了，和新并进来的也冲突


def test_the_card_shows_what_a_merge_would_do_without_changing_anything(merged_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = merged_world
    proposal = proposal_of(world, path, "柏庭舟")
    conn = open_ledger(world.settings.library("prod"))
    before = dump(conn, surrogate=False)
    conn.close()
    assert main(["entities", "--library", "prod", "--show", proposal, "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "合并以后：" in out and "重写 3 个窗口（从结果库，不调模型）" in out and "事实 3 条挂到新编号上" in out
    assert f"旧编号 {TYPO} 以后查到，跳到 {STANDARD}" in out
    conn = open_ledger(world.settings.library("prod"))
    assert dump(conn, surrogate=False) == before
    conn.close()
    assert not lex.merges_path(path).exists()


def rebuild_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Path) -> World:
    data = tmp_path / "rebuilt"
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    world = World(data.resolve(), Settings.from_env())
    for day, line in LINES:
        world.drop("inbox/desk-bot", day_packet(day, line, domain="[investment]"))
    world.intake()
    shutil.copytree(store, data / "l3")
    return world


def ledger_view(world: World) -> dict[str, list[tuple[object, ...]]]:
    conn = open_ledger(world.settings.library("prod"))
    try:
        tables = dump(conn, surrogate=False)
    finally:
        conn.close()
    keep = ("entities", "aliases", "facts", "fact_entities", "fact_claims", "evidence", "entity_redirects")
    return {name: tables[name] for name in keep}


def test_rebuilding_after_a_merge_gives_the_same_ledger(merged_world, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, path = merged_world
    for name in ("柏庭舟", "KVLT"):
        assert main(["entities", "--library", "prod", "--merge", proposal_of(world, path, name), "--note", "测试"]) == 0
    merged = ledger_view(world)
    rebuilt = rebuild_world(tmp_path, monkeypatch, world.data / "l3")
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0  # 别名表和合并记录从一开始就读
    assert main(["entities", "--library", "prod", "--sync"]) == 0  # 补上这本新账的跳转、合过的
    assert ledger_view(rebuilt) == merged
    assert [row["merge_id"] for row in rebuilt.rows("SELECT merge_id FROM entity_merges ORDER BY merge_id")] == ["M0001", "M0002"]


def test_nothing_is_written_when_the_windows_cannot_be_rewritten(merged_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = merged_world
    proposal = proposal_of(world, path, "柏庭舟")
    shutil.rmtree(world.data / "l3")
    assert main(["entities", "--library", "prod", "--merge", proposal, "--note", "测试"]) == 1
    assert "没有 L3 结果库" in capsys.readouterr().err
    assert not lex.merges_path(path).exists()
    assert len(world.rows("SELECT * FROM entities WHERE type = '人'")) == 2


def test_a_merge_against_the_human_lexicon_is_refused(merged_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = merged_world
    proposal = proposal_of(world, path, "柏庭舟")
    path.write_text(LEXICON_TEXT + "apart:\n  - {type: 资产, names: [KVLT, KVLT 累购]}\n", encoding="utf-8")
    assert main(["entities", "--library", "prod", "--merge", proposal_of(world, path, "KVLT"), "--only",
                 entity_id_of("资产", name_key("KVLT 累购")), "--note", "测试"]) == 1
    assert "对不上" in capsys.readouterr().err and not lex.merges_path(path).exists()
    assert main(["entities", "--library", "prod", "--merge", proposal]) == 2  # 没写 --note
    assert main(["entities", "--library", "prod", "--merge", "P00000000", "--note", "测试"]) == 1
    assert main(["entities", "--library", "prod", "--merge", proposal, "--show", proposal, "--note", "测试"]) == 2


def test_sync_finishes_a_record_that_was_written_but_not_applied(merged_world) -> None:
    world, path = merged_world
    proposal = proposal_of(world, path, "柏庭舟")
    record = {"id": "M0001", "kind": "merge", "at": "2026-11-14T10:00:00+08:00", "by": "owner", "note": "中途断了",
              "type": "人", "standard": "柏庭舟", "names": ["柏廷舟"], "where": ["investment"], "from_types": [],
              "old_ids": [TYPO], "new_id": STANDARD, "proposal": proposal}
    lex.append_record(path, record)
    assert len(world.rows("SELECT * FROM entities WHERE type = '人'")) == 2
    assert main(["entities", "--library", "prod", "--sync"]) == 0
    assert [row["entity_id"] for row in world.rows("SELECT * FROM entities WHERE type = '人'")] == [STANDARD]
    assert main(["entities", "--library", "prod", "--sync"]) == 0  # 再跑一次什么都不做


def test_apart_is_recorded_without_rewriting_and_the_proposal_goes_away(merged_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = merged_world
    grouped = proposal_of(world, path, "柏庭舟")
    assert main(["entities", "--library", "prod", "--apart", grouped, "--note", "测试"]) == 1  # 人写的别名表说是同一组
    assert "对不上" in capsys.readouterr().err and not lex.merges_path(path).exists()
    path.write_text(LEXICON_TEXT.replace("  - type: 人\n    names: [柏庭舟, 柏廷舟]\n", ""), encoding="utf-8")
    typo = proposal_of(world, path, "柏廷舟")  # 账本里找的：中文名只差一个字，事实多的那个写法当建议
    shutil.rmtree(world.data / "l3")  # 不是同一个不重写窗口，用不着结果库
    assert main(["entities", "--library", "prod", "--apart", typo, "--note", "测试"]) == 0
    record = lex.read_records(lex.merges_path(path))[0]
    assert (record["kind"], record["type"], set(record["names"])) == ("apart", "人", {"柏廷舟", "柏庭舟"})
    assert [tuple(row) for row in world.rows("SELECT merge_id, kind FROM entity_merges")] == [("M0001", "apart")]
    conn = open_ledger(world.settings.library("prod"))
    assert typo not in {item.proposal_id for item in find_proposals(conn, lex.load_lexicon(path))}
    conn.close()
    assert len(world.rows("SELECT * FROM entities WHERE type = '人'")) == 2


def test_history_lists_the_records_and_whether_they_took_effect(merged_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = merged_world
    assert main(["entities", "--library", "prod", "--merge", proposal_of(world, path, "柏庭舟"), "--note", "测试第 1 条"]) == 0
    capsys.readouterr()
    assert main(["entities", "--library", "prod", "--history"]) == 0
    out = capsys.readouterr().out
    assert "M0001 合并" in out and "「柏庭舟」（人）← 柏廷舟（测试第 1 条）已生效" in out and "合并记录 1 条" in out


def test_open_conflicts_keep_their_30_day_clock_through_a_merge(merged_world) -> None:
    world, path = merged_world
    old_frozen = facts_by_content(world)["柏廷舟负责资金池"]["fact_id"]
    assert main(["freeze", "--library", "prod", "--fact", old_frozen]) == 0
    world.execute("UPDATE review_queue SET created_at = '2026-10-01T09:00:00+08:00' WHERE kind = 'frozen_conflict'")
    assert main(["entities", "--library", "prod", "--merge", proposal_of(world, path, "柏庭舟"), "--note", "测试"]) == 0
    facts = facts_by_content(world)
    rows = world.rows("SELECT claims, created_at FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL")
    clock = {json.loads(row["claims"])["new"]: row["created_at"] for row in rows}
    assert clock[facts["柏廷舟改成负责对账"]["fact_id"]] == "2026-10-01T09:00:00+08:00"  # 原来那条：30 天的钟不重新算
    assert clock[facts["柏庭舟以后负责报价"]["fact_id"]] != "2026-10-01T09:00:00+08:00"  # 合并以后才有的：新的
