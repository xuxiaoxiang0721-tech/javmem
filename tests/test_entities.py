"""合并工具（J3.6，D100）第一段：合并的提议。三处来源（别名表、L5 的歧义、账本里找重复）、默认并和默认不并两档、
疑似撞名提“不是同一个”、记成不是同一个的和主人不提、编号稳定、命令行。名字和句子都是编的，从头写的。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World
from spotter import SpotModel, extract_into_store
from test_write import day_packet

from javmem.cli import main
from javmem.entities import AMBIGUOUS, APART, CLASH, LEDGER, LEXICON, TYPE_CHANGE, TYPO, find_proposals
from javmem.ledger import open_ledger
from javmem.pipeline.merge import OWNER_NAME, entity_id_of, name_key
from javmem.zh.lexicon import load_lexicon

TABLE = [
    ("柏廷舟", [("柏廷舟", "人", "柏廷舟")]),
    ("柏庭舟", [("柏庭舟", "人", "柏庭舟")]),
    ("累购", [("KVLT 累购", "资产", "KVLT")]),
    ("Kovalt", [("Kovalt", "公司或机构", "Kovalt")]),
    ("KVLT", [("KVLT", "资产", "KVLT")]),
    ("周建国", [("周建国", "人", "老周")]),
    ("周立新", [("周立新", "人", "老周")]),
    ("老周", [("老周", "人", "老周")]),
    ("Corbin Vale", [("Corbin Vale", "人", "Corbin Vale")]),
    ("Corbin", [("Corbin", "人", "Corbin")]),
    ("Dunmore", [("Rhea Dunmore", "人", "Rhea Dunmore")]),
    ("Ngata", [("Rhea Ngata", "人", "Rhea Ngata")]),
    ("Rhea", [("Rhea", "人", "Rhea")]),
    ("鲁遥舟", [("鲁遥舟", "人", "鲁遥舟")]),
    ("鲁瑶舟", [("鲁瑶舟", "人", "鲁瑶舟")]),
    ("远帆", [("远帆 FCN XS0000000123", "订单或合同", "远帆")]),
    ("XS0000000123", [("XS0000000123", "资产", "XS0000000123")]),
    ("青苇路的铺面", [("青苇路", "项目", "青苇路")]),
    ("青苇路", [("青苇路", "地点", "青苇路")]),
    ("主人", [("主人", "人", "主人")]),
]
LINES = [
    ("2026-09-01", "我：柏廷舟管资金池的日常。", "[investment]"),
    ("2026-09-02", "我：周五约柏庭舟对一下报价。", "[investment]"),
    ("2026-09-03", "我：KVLT 累购这周敲了两次。", "[investment]"),
    ("2026-09-04", "我：Kovalt 的财报下周出。", "[investment]"),
    ("2026-09-05", "我：KVLT 收在 41.2。", "[investment]"),
    ("2026-09-06", "我：周建国就是老周，管越南。", "[investment]"),
    ("2026-09-07", "我：周立新也叫老周，管泰国。", "[investment]"),
    ("2026-09-08", "我：老周明天回来。", "[investment]"),
    ("2026-09-09", "我：Corbin Vale 把仓位降到三成。", "[investment]"),
    ("2026-09-10", "我：Corbin 下午来对账。", "[investment]"),
    ("2026-09-11", "我：Rhea Dunmore 接手了物业。", "[personal]"),
    ("2026-09-12", "我：Rhea Ngata 是新来的家教。", "[personal]"),
    ("2026-09-13", "我：Rhea 明天来量窗帘。", "[personal]"),
    ("2026-09-14", "我：鲁遥舟周六来修水管。", "[personal]"),
    ("2026-09-15", "我：鲁瑶舟说水管要换。", "[personal]"),
    ("2026-09-16", "我：远帆 FCN XS0000000123 下周敲出。", "[investment]"),
    ("2026-09-17", "我：XS0000000123 的票息是 9%。", "[investment]"),
    ("2026-09-18", "我：青苇路的铺面月底交。", "[personal]"),
    ("2026-09-19", "我：青苇路那边在修地铁。", "[personal]"),
    ("2026-09-20", "我：主人这周不看盘。", "[investment]"),
]
LEXICON_TEXT = """format: javmem-lexicon/2
default_where: [investment, personal]
aliases:
  - type: 人
    names: [柏庭舟, 柏廷舟]
  - type: 资产
    names: [KVLT, Kovalt]
    from_types: [公司或机构]
"""


def build(world: World) -> None:
    for day, line, domain in LINES:
        world.drop("inbox/desk-bot", day_packet(day, line, domain=domain))
    world.intake()
    extract_into_store(world, SpotModel(TABLE))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0  # 没配别名表：照旧各建各的


@pytest.fixture
def built(world: World, tmp_path: Path) -> tuple[World, Path]:
    build(world)
    path = tmp_path / "lexicon.yaml"
    path.write_text(LEXICON_TEXT, encoding="utf-8")
    return world, path


def eid(name: str, entity_type: str) -> str:
    return entity_id_of(entity_type, name_key(name))


def test_proposals_from_the_three_sources(built: tuple[World, Path]) -> None:
    world, path = built
    conn = open_ledger(world.settings.library("prod"))
    proposals = find_proposals(conn, load_lexicon(path))
    by_set = {frozenset(item.entities): item for item in proposals}

    names = by_set[frozenset({eid("柏庭舟", "人"), eid("柏廷舟", "人")})]
    assert (names.source, names.kind, names.name, names.type) == (LEXICON, "merge", "柏庭舟", "人")
    assert set(names.default) == set(names.entities)

    # “KVLT 收在 41.2”那一句按别名对到了累购上（累购挂着 KVLT 这个叫法），账本里没有 KVLT 自己
    assert conn.execute("SELECT 1 FROM entities WHERE entity_id = ?", (eid("KVLT", "资产"),)).fetchone() is None
    kvlt = by_set[frozenset({eid("Kovalt", "公司或机构"), eid("KVLT 累购", "资产")})]
    assert kvlt.source == LEXICON and kvlt.default == [eid("Kovalt", "公司或机构")]  # 累购只挂着叫法：默认不并
    assert kvlt.entities[-1] == eid("KVLT 累购", "资产") and (kvlt.name, kvlt.type) == ("KVLT", "资产")
    assert TYPE_CHANGE in kvlt.flags

    ambiguous = next(item for item in proposals if item.source == AMBIGUOUS)
    assert set(ambiguous.entities) == {eid("老周", "人"), eid("周建国", "人"), eid("周立新", "人")}

    corbin = by_set[frozenset({eid("Corbin", "人"), eid("Corbin Vale", "人")})]
    assert corbin.source == LEDGER and corbin.kind == "merge" and corbin.name in ("Corbin", "Corbin Vale")
    rhea = by_set[frozenset({eid("Rhea Dunmore", "人"), eid("Rhea Ngata", "人")})]
    assert rhea.kind == APART and CLASH in rhea.flags  # 一个简称对上两个全名：提不是同一个
    typo = by_set[frozenset({eid("鲁遥舟", "人"), eid("鲁瑶舟", "人")})]
    assert TYPO in typo.flags
    code = by_set[frozenset({eid("远帆 FCN XS0000000123", "订单或合同"), eid("XS0000000123", "资产")})]
    assert code.source == LEDGER
    place = by_set[frozenset({eid("青苇路", "项目"), eid("青苇路", "地点")})]
    assert "写法一样、类型不一样" in place.reasons

    owner = entity_id_of("人", OWNER_NAME)
    assert all(owner not in item.entities for item in proposals)
    # 编号按来源和实体算：再找一遍一样
    assert [item.proposal_id for item in find_proposals(conn, load_lexicon(path))] == [item.proposal_id for item in proposals]
    assert all(item.proposal_id.startswith("P") and len(item.proposal_id) == 9 for item in proposals)
    conn.close()


def test_apart_pairs_are_not_proposed(built: tuple[World, Path]) -> None:
    world, path = built
    path.write_text(LEXICON_TEXT + "apart:\n  - {type: 人, names: [鲁遥舟, 鲁瑶舟]}\n", encoding="utf-8")
    conn = open_ledger(world.settings.library("prod"))
    proposals = find_proposals(conn, load_lexicon(path), [LEDGER])
    assert frozenset({eid("鲁遥舟", "人"), eid("鲁瑶舟", "人")}) not in {frozenset(item.entities) for item in proposals}
    assert all(item.source == LEDGER for item in proposals)
    conn.close()


def test_proposals_command(built: tuple[World, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _, path = built
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    assert main(["entities", "--library", "prod", "--proposals", "--source", "lexicon"]) == 0
    out = capsys.readouterr().out
    assert "并成「柏庭舟」（人）" in out and "默认不并" in out and "提议 2 条" in out
    assert main(["entities", "--library", "prod", "--proposals", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {item["source"] for item in data} == {LEXICON, AMBIGUOUS, LEDGER}
    assert all({"proposal_id", "entities", "default", "infos"} <= set(item) for item in data)
    assert main(["entities", "--library", "test", "--proposals"]) == 0
    assert "提议 0 条" in capsys.readouterr().out


def test_no_ledger_is_a_config_error(data_dir: Path) -> None:
    assert main(["entities", "--library", "prod", "--proposals"]) == 2
    assert not (data_dir / "ledger" / "prod.db").exists()  # 不建一本空的
