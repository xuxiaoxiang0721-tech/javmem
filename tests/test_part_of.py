"""D210、D213：第七种关系隶属于从头走一遍（L3 写死在这里，不调模型）：核对、写账、查询接口。例子都是编的。

照卡 7、卡 4：说了部门的，人任职于部门（不再另连公司），部门再隶属于公司；问“这个部门是哪家的”查隶属于，问“他是哪家的”
先查到部门、再顺着部门的隶属于往上找（查询接口不自己往上找，D210 第 5 件）。
"""

from __future__ import annotations

from dataclasses import replace

from helpers import World, as_json
from test_relcheck import relation
from test_write import day_packet, deliver, write

from javmem.pipeline.window import Window
from javmem.query.tools import entity_card, relation_at

KELAN = {"key": "e1", "mention": "柯岚", "name": "柯岚", "type": "人"}
WEALTH = {"key": "e2", "mention": "财富管理部", "name": "东岸证券 财富管理部", "type": "公司或机构"}
BROKER = {"key": "e3", "mention": "东岸证券", "name": "东岸证券", "type": "公司或机构"}
SIGNATURE = "柯岚 | 财富管理部 | 东岸证券"


def signature_extract(window: Window) -> dict[str, object]:
    """写着签名的那一行：柯岚任职于东岸证券财富管理部；财富管理部隶属于东岸证券（签名里部门和公司写在一起，原话）。"""
    found = []
    for line in window.targets:
        if SIGNATURE in line.text:
            found += [relation(KELAN, "任职于", WEALTH, (line.ref, SIGNATURE), speaker="desk-bot"),
                      relation(WEALTH, "隶属于", BROKER, (line.ref, SIGNATURE), speaker="desk-bot")]
    return {"entities": [KELAN, WEALTH, BROKER], "facts": [], "relations": found}


def test_a_signature_gives_a_department_job_and_a_part_of_the_query_tools_can_answer(world: World) -> None:
    deliver(world, day_packet("2026-11-01", f"desk-bot：东岸证券那封邮件的签名：{SIGNATURE}。"))
    write(world, signature_extract).close()
    rows = world.rows(
        "SELECT r.relation_type, s.canonical_name AS subject, o.canonical_name AS object, r.state, r.status FROM relations AS r"
        " JOIN entities AS s ON s.entity_id = r.subject_entity_id JOIN entities AS o ON o.entity_id = r.object_entity_id"
        " ORDER BY r.relation_type"
    )
    assert [tuple(row) for row in rows] == [
        ("任职于", "柯岚", "东岸证券 财富管理部", "active", "verified"),
        ("隶属于", "东岸证券 财富管理部", "东岸证券", "active", "verified"),
    ]
    # 查询接口：拼出来的标准名（“东岸证券 财富管理部”）原文里没有这个写法，照 J5.5 的规矩不算看得到的名字，对外用原文的
    # 叫法“财富管理部”，按全名查不到；账本里照旧是东岸证券的那一个（别家的财富管理部是另一个实体）
    caller = replace(world.settings, caller="desk-bot")
    whole = as_json(relation_at({"entity": "东岸证券 财富管理部", "relation": "隶属于", "at": "2026-11-02"}, settings=caller))
    assert whole["result"] == "none" and whole["not_found"]["reason"] == "no_entity"
    answer = as_json(relation_at({"entity": "财富管理部", "relation": "隶属于", "at": "2026-11-02", "role": "subject"},
                                 settings=caller))
    assert answer["result"] == "hit"  # 这个部门是哪家的
    assert [item["relation"]["object"]["name"] for item in answer["items"]] == ["东岸证券"]
    assert answer["items"][0]["content"] == "财富管理部 隶属于 东岸证券"
    reverse = as_json(relation_at({"entity": "东岸证券", "relation": "隶属于", "at": "2026-11-02", "role": "object"}, settings=caller))
    assert [item["relation"]["subject"]["name"] for item in reverse["items"]] == ["财富管理部"]  # 这家有哪些部门
    card = as_json(entity_card({"entity": "柯岚"}, settings=caller))
    jobs = [item for item in card["relations"] if item["relation"]["type"] == "任职于"]
    assert [item["relation"]["object"]["name"] for item in jobs] == ["财富管理部"]  # 不再另连东岸证券


def test_two_firms_with_a_department_of_the_same_name_stay_apart(world: World) -> None:
    """名字带上是哪家的（l3-v24、l3-v25 第一步那一条）：两家的“财富管理部”是两个实体，各归各家，谁也不顶掉谁；
    要是并成一个，同一时间只归一家，后说的就把先说的顶掉了。按“财富管理部”查，两个都给出来让人挑。"""
    shen = {"key": "e4", "mention": "沈岚", "name": "沈岚", "type": "人"}
    harbor_wealth = {"key": "e5", "mention": "财富管理部", "name": "海港银行 财富管理部", "type": "公司或机构"}
    harbor = {"key": "e6", "mention": "海港银行", "name": "海港银行", "type": "公司或机构"}
    other = "沈岚 | 财富管理部 | 海港银行"

    def extract(window: Window) -> dict[str, object]:
        found = signature_extract(window)["relations"]
        for line in window.targets:
            if other in line.text:
                found += [relation(shen, "任职于", harbor_wealth, (line.ref, other), speaker="desk-bot"),  # type: ignore[operator]
                          relation(harbor_wealth, "隶属于", harbor, (line.ref, other), speaker="desk-bot")]
        return {"entities": [KELAN, WEALTH, BROKER, shen, harbor_wealth, harbor], "facts": [], "relations": found}

    deliver(world, day_packet("2026-11-01", f"desk-bot：东岸证券那封邮件的签名：{SIGNATURE}。"),
            day_packet("2026-11-03", f"desk-bot：海港银行那封邮件的签名：{other}。"))
    write(world, extract).close()
    rows = world.rows(
        "SELECT s.canonical_name AS subject, o.canonical_name AS object, r.state, r.superseded_by FROM relations AS r"
        " JOIN entities AS s ON s.entity_id = r.subject_entity_id JOIN entities AS o ON o.entity_id = r.object_entity_id"
        " WHERE r.relation_type = '隶属于' ORDER BY o.canonical_name"
    )
    assert [tuple(row) for row in rows] == [("东岸证券 财富管理部", "东岸证券", "active", None),
                                            ("海港银行 财富管理部", "海港银行", "active", None)]
    caller = replace(world.settings, caller="desk-bot")
    answer = as_json(relation_at({"entity": "财富管理部", "relation": "隶属于", "at": "2026-11-04"}, settings=caller))
    assert answer["result"] == "ambiguous" and len(answer["candidates"]) == 2
