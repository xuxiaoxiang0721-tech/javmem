"""合并工具（J3.6）：合并以后，一条“实体有歧义”像的几个都并成了一个，歧义就没了：关掉（merged），那个名字的窗口
一起重写，对上合好的实体，和整本重建一样。名字和句子都是编的，从头写的（在真实数据的副本上试跑时看出来的）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from helpers import World
from spotter import SpotModel, extract_into_store
from test_write import day_packet

from javmem.cli import main
from javmem.config import Settings
from javmem.entities import AMBIGUOUS, find_proposals
from javmem.ledger import open_ledger
from javmem.pipeline.merge import entity_id_of, name_key
from javmem.zh import lexicon as lex

TABLE = [
    ("周建国", [("周建国", "人", "老周")]),
    ("周立新", [("周立新", "人", "老周")]),
    ("老周", [("老周", "人", "老周")]),
]
LINES = [
    ("2026-09-06", "我：周建国就是老周，管越南。"),
    ("2026-09-07", "我：周立新也叫老周，管泰国。"),
    ("2026-09-08", "我：老周明天回来。"),
]
JIANGUO, LIXIN, LAOZHOU = (entity_id_of("人", name_key(name)) for name in ("周建国", "周立新", "老周"))


def deliver(world: World) -> None:
    for day, line in LINES:
        world.drop("inbox/desk-bot", day_packet(day, line, domain="[investment]"))
    world.intake()


@pytest.fixture
def ambiguous_world(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[World, Path]:
    deliver(world)
    extract_into_store(world, SpotModel(TABLE))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    path = tmp_path / "config" / "lexicon.yaml"
    path.parent.mkdir()
    path.write_text("format: javmem-lexicon/2\n", encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    return world, path


def test_merging_the_candidates_settles_the_ambiguity(ambiguous_world, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, path = ambiguous_world
    subject = {row["content"]: row for row in world.rows("SELECT * FROM facts")}
    assert subject["老周明天回来"]["status"] == "pending" and subject["老周明天回来"]["subject_entity_id"] == LAOZHOU
    conn = open_ledger(world.settings.library("prod"))
    proposal = next(item for item in find_proposals(conn, lex.load_lexicon(path)) if item.source == AMBIGUOUS)
    conn.close()
    # 只并两个候选（编的：这两个其实是同一个人），歧义里那个“老周”不点名
    assert main(["entities", "--library", "prod", "--merge", proposal.proposal_id, "--only", JIANGUO, "--only", LIXIN,
                 "--name", "周建国", "--note", "测试"]) == 0
    facts = {row["content"]: row for row in world.rows("SELECT * FROM facts")}
    assert facts["老周明天回来"]["subject_entity_id"] == JIANGUO and facts["老周明天回来"]["status"] == "verified"
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL")[0]["n"] == 0
    closed = [json.loads(row["claims"])["entity_id"] for row in world.rows("SELECT claims FROM review_queue WHERE decision = 'merged'")]
    assert closed == [LAOZHOU]
    assert not world.rows("SELECT 1 FROM entities WHERE entity_id = ?", LAOZHOU)
    merged = {row["content"]: (row["subject_entity_id"], row["status"]) for row in world.rows("SELECT * FROM facts")}
    # 整本重建（别名表加合并记录从一开始就读）：一样
    data = tmp_path / "rebuilt"
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    rebuilt = World(data.resolve(), Settings.from_env())
    deliver(rebuilt)
    shutil.copytree(world.data / "l3", data / "l3")
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert {row["content"]: (row["subject_entity_id"], row["status"]) for row in rebuilt.rows("SELECT * FROM facts")} == merged


def test_the_ambiguous_entity_itself_as_the_merge_target_settles_it(ambiguous_world) -> None:
    world, path = ambiguous_world
    conn = open_ledger(world.settings.library("prod"))
    proposal = next(item for item in find_proposals(conn, lex.load_lexicon(path)) if item.source == AMBIGUOUS)
    conn.close()
    # 有歧义的那个自己当合并以后的那个，只并进一个候选：它的名字进了组，L5 直接对上，不会再有歧义
    assert main(["entities", "--library", "prod", "--merge", proposal.proposal_id, "--only", LAOZHOU, "--only", JIANGUO,
                 "--name", "老周", "--note", "测试"]) == 0
    facts = {row["content"]: row for row in world.rows("SELECT * FROM facts")}
    assert facts["老周明天回来"]["subject_entity_id"] == LAOZHOU and facts["老周明天回来"]["status"] == "verified"
    assert facts["周立新也叫老周，管泰国"]["subject_entity_id"] == LIXIN  # 没点名的候选照旧
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL")[0]["n"] == 0
