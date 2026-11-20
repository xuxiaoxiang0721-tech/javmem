"""Graphiti 支线的第二证人、每周体检（javmem.side.compare、javmem.side.checkup，J6.2、J6.3）：比对是纯函数，
两边都是编的对齐格式；抽包用编的日子。不用装 Graphiti。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World, build_packet
from test_side import BODY, PACKET_ID, _episode

from javmem.config import ConfigError
from javmem.side.store import SideStore



def _side(name: str, relations: list[tuple[str, str, str, str, str]], packet: str = PACKET_ID,
          types: dict[str, str] | None = None) -> object:
    """relations：(id, 主体名, 关系, 客体名, 状态)。实体按名字建，类型照 types，没给的人名是人、别的是公司或机构。"""
    from javmem.scoring.common import Entity, Relation, Side

    entities: dict[str, Entity] = {}

    def entity(label: str) -> str:
        key = f"{name}:{label}"
        kind = (types or {}).get(label) or ("人" if label in ("主人", "我", "小周", "小李") else "公司或机构")
        entities.setdefault(key, Entity(key, label, kind))
        return key

    rels = {
        rid: Relation(rid, entity(s), rel, entity(o), {}, state=state, evidence=(f"{packet}#1.{n}.1",))
        for n, (rid, s, rel, o, state) in enumerate(relations, start=1)
    }
    return Side(name, frozenset({packet}), entities, {}, rels)


def test_compare_four_kinds() -> None:
    from javmem.side.compare import ENDED, MAIN_ONLY, OTHER_END, SIDE_ONLY, checkup_report, compare, totals

    main = _side("main", [
        ("m1", "主人", "客户是", "东岸证券", "生效"),
        ("m2", "X1 FCN", "托管于", "东岸证券", "生效"),
        ("m3", "小周", "负责", "主人", "生效"),
        ("m4", "主人", "持有", "X2", "生效"),
    ])
    side = _side("side", [
        ("s1", "我", "客户是", "东岸证券", "生效"),  # “我”就是主人：两边都有
        ("s2", "X1 FCN", "托管于", "海港银行", "生效"),  # 同一张单子放的地方不一样
        ("s3", "小周", "负责", "主人", "已结束"),  # 一边结束了
        ("s4", "主人", "任职于", "西岭公司", "生效"),  # 只有支线有
    ])
    result = compare(main, side, None)  # type: ignore[arg-type]
    kinds = sorted((item.kind, item.relation) for item in result.mismatches)
    assert kinds == sorted([(OTHER_END, "托管于"), (ENDED, "负责"), (MAIN_ONLY, "持有"), (SIDE_ONLY, "任职于")])
    assert result.by_kind() == {OTHER_END: 1, ENDED: 1, MAIN_ONLY: 1, SIDE_ONLY: 1}
    assert {item.kind for item in result.queue()} == {OTHER_END, ENDED}
    assert totals(result)["客户是"] == {"main": 1, "side": 1, "both": 1}
    report = checkup_report(result, side)  # type: ignore[arg-type]
    assert report["main_missed_by_relation"] == {"任职于": 1}
    assert report["main_missed_by_types"] == {"人 → 公司或机构": 1}
    assert report["main_missed"] == [{"packet": PACKET_ID, "relation": "任职于", "evidence": [f"{PACKET_ID}#1.4.1"]}]
    assert "海港银行" not in json.dumps(report, ensure_ascii=False)  # 报告不抄名字，只给包号、句子编号


def test_pick_is_seeded_and_skips_done_private_and_other_weeks(world: World, tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from javmem.side.checkup import SidePlan, candidates, every_packet, pick

    for stamp, privacy in (("2026-11-02_0900", "normal"), ("2026-11-03_0900", "normal"), ("2026-11-04_0900", "private")):
        world.drop("inbox/desk-bot", build_packet(stamp=stamp, body=BODY, privacy=privacy))
    world.intake()
    world.execute("UPDATE packets SET extraction = 'done'")
    world.execute("UPDATE packets SET received_at = '2026-11-03T10:00:00+08:00'")
    world.execute("UPDATE packets SET received_at = '2026-10-20T10:00:00+08:00' WHERE packet_id LIKE '2026-11-02%'")
    tz = timezone(timedelta(hours=8))
    week = (datetime(2026, 11, 2, tzinfo=tz), datetime(2026, 11, 9, tzinfo=tz))
    with SideStore.open(tmp_path, "prod") as store:
        pool = candidates(world.settings.library("prod").db_path, store, *week)
        assert pool == ["2026-11-03_0900_desk-bot"]  # 别的周的、private 的不抽
        store.start_group(_episode(1, packet="2026-11-03_0900_desk-bot"))
        assert candidates(world.settings.library("prod").db_path, store, *week) == []  # 跑过的不再抽
    many = [f"p{n:02d}" for n in range(30)]
    assert pick(many, 10, 7) == pick(list(reversed(many)), 10, 7)
    assert len(pick(many, 10, 7)) == 10 and pick(many[:3], 10, 7) == many[:3]
    assert every_packet(["investment"], SidePlan()) and not every_packet(["personal"], SidePlan())
    plan_file = tmp_path / "side.yaml"
    plan_file.write_text("every_packet_partitions: [legal]\nweekly_count: 5\n", encoding="utf-8")
    assert SidePlan.from_env({"JAVMEM_SIDE_FILE": str(plan_file)}) == SidePlan(("legal",), 5)
    plan_file.write_text("weekly_count: 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        SidePlan.from_env({"JAVMEM_SIDE_FILE": str(plan_file)})



def test_part_of_is_a_side_type_and_one_parent_at_a_time() -> None:
    """D210：支线也认隶属于（PART_OF，两头都是公司或机构，允许的两头照 relcheck.ALLOWED）；同一个部门两边说归两家，算
    “另一头不一样”，和托管于一样按主体比。"""
    from javmem.side import graph
    from javmem.side.compare import FUNCTIONAL, OTHER_END, compare

    assert graph.RELATION_LABELS["隶属于"] == "PART_OF" and graph.LABEL_RELATION["PART_OF"] == "隶属于"
    assert set(graph.RELATION_DOCS) == set(graph.RELATION_LABELS.values())
    assert graph.RELATION_DOCS["PART_OF"].startswith("隶属于：") and "任职于" in graph.RELATION_DOCS["PART_OF"]
    assert "部门" in graph.RELATION_DOCS["WORKS_AT"] and "七种" in graph.INSTRUCTIONS
    assert FUNCTIONAL["隶属于"] == "subject"
    main = _side("main", [("m1", "外贸部", "隶属于", "示例贸易", "生效")])
    side = _side("side", [("s1", "外贸部", "隶属于", "示例贸易华东分公司", "生效")])
    assert [item.kind for item in compare(main, side, None).mismatches] == [OTHER_END]  # type: ignore[arg-type]


def test_responsible_for_the_owner_is_not_a_contradiction() -> None:
    from javmem.side.compare import MAIN_ONLY, OTHER_END, SIDE_ONLY, compare

    main = _side("main", [("m1", "小周", "负责", "主人", "生效")])
    side = _side("side", [("s1", "小李", "负责", "主人", "生效")])  # 两家各有一个客户经理，不矛盾
    kinds = sorted(item.kind for item in compare(main, side, None).mismatches)  # type: ignore[arg-type]
    assert kinds == sorted([MAIN_ONLY, SIDE_ONLY])
    main = _side("main", [("m1", "小周", "负责", "新店装修", "生效")], types={"新店装修": "项目"})
    side = _side("side", [("s1", "小李", "负责", "新店装修", "生效")], types={"新店装修": "项目"})
    assert [item.kind for item in compare(main, side, None).mismatches] == [OTHER_END]  # type: ignore[arg-type]
