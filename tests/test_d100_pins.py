"""D100 定了、写代码时要钉住的（J3.6）。例子都是编的，从头写的。"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import World
from spotter import SpotModel, extract_into_store
from test_write import day_packet

from javmem.cli import main
from javmem.pipeline.merge import entity_id_of, name_key

LEXICON = """format: javmem-lexicon/2
aliases:
  - type: 资产
    names: [远帆, 远帆 ADR, 6127 HK]
"""


def test_d100_item_3_two_listings_in_one_group_replace_each_others_price(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D100 第 3 条：主人选了照 D94 把一只票的两种证券（美国的 ADR、港股）合成一个实体。这是选的，不是错。

    两边的“现价”是同一个主体、同一个属性，L5 按后说的替代先说的：问“现价多少”，答到的是最后报的那一边，币种可能
    不对（包里主人纠正过一次 Bot 混用两边的币种）。要改成分开，先改别名表（拆这一组，或者合并工具撤销），再改这条测试；
    或者让属性带上币种（“现价（港币）”），那要 J3.9 的提示词配合。
    """
    path = tmp_path / "lexicon.yaml"
    path.write_text(LEXICON, encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    world.drop("inbox/desk-bot", day_packet("2026-10-09", "我：6127 HK 现价 63.7 港币。", "我：远帆 ADR 现价 8.1 美元。", domain="[investment]"))
    world.intake()
    table = [("6127 HK", [("6127 HK", "资产", "6127 HK")]), ("远帆 ADR", [("远帆 ADR", "资产", "远帆 ADR")])]
    values = [("63.7", "现价", "63.7 港币"), ("8.1", "现价", "8.1 美元")]
    extract_into_store(world, SpotModel(table, values))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    rows = {row["value"]: row for row in world.rows("SELECT * FROM facts")}
    hk, adr = rows["63.7 港币"], rows["8.1 美元"]
    one = entity_id_of("资产", name_key("远帆"))
    assert hk["subject_entity_id"] == adr["subject_entity_id"] == one  # 两种证券是一个实体
    assert hk["group_key"] == adr["group_key"] is not None  # 同一个主体、同一个属性：一组
    assert hk["superseded_by"] == adr["fact_id"] and adr["superseded_by"] is None  # 后说的美元价顶掉了先说的港币价
