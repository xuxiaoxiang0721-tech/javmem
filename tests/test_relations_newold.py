"""J4.4 第 2 段：同一时间一个的关系，新的来了旧的结束（加上暂停那天）。例子都是编的。

照定了的：托管于一样东西同一时间一家（卡 2）、负责一样东西同一时间一个（卡 3，R12）；暂停着被接手照常失效（R36）；
转仓按到账那天换（R37、5.10）；后说的讲的是更早的事只补历史（D153 第 1 条）；负责先看这个人任职于哪家（D153 第 2 条）；
暂停那天（D153 第 3 条）；同一时刻两个人两条都记、进待确认（R12）；冻结的不顶掉（D12）；持有这些同一时间可以几个。
"""

from __future__ import annotations

import json

from helpers import World
from test_relcheck import BANK, BRANCH, BROKER, DEPT, NOTE, OWNER, STOCK, TRADE, relation
from test_write import day_packet, deliver, write

from javmem.cli import main
from javmem.pipeline.window import Window
from javmem.pipeline.write import LedgerWriter
from javmem.review import freeze

LI = {"key": "e11", "mention": "小李", "name": "李一舟", "type": "人"}
ZHOU = {"key": "e12", "mention": "老周", "name": "周建宇", "type": "人"}
ORDER = {"key": "e13", "mention": "越南那批", "name": "越南那批订单", "type": "订单或合同"}


def moved_extract(window: Window) -> dict[str, object]:
    """X1 票据：写着哪家的那一行托管于那一家；写着“11 月 9 日起”“10 月 1 日起”的照那句写开始日子。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        fields: dict[str, object] = {}
        for marker, day in (("11 月 9 日起", "2026-11-09"), ("10 月 1 日起", "2026-10-01"), ("11 月 5 日起", "2026-11-05")):
            if marker in text:
                fields.update(valid_from=day, time_precision="day")
        for bank in (BANK, BROKER):
            if str(bank["name"]) in text and "X1 票据" in text:
                found.append(relation(NOTE, "托管于", bank, (line.ref, text), **fields))
    return {"entities": [NOTE, BANK, BROKER], "facts": [], "relations": found}


def by_bank(world: World) -> dict[str, dict]:
    rows = world.rows(
        "SELECT r.*, e.canonical_name AS bank FROM relations AS r JOIN entities AS e ON e.entity_id = r.object_entity_id"
    )
    return {row["bank"]: dict(row) for row in rows}


def test_moving_to_another_bank_ends_the_old_one_from_the_new_start_day(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"),
            day_packet("2026-11-07", "我：X1 票据 11 月 9 日起转到东岸证券。"))
    write(world, moved_extract).close()
    rows = by_bank(world)
    old, new = rows["海港银行"], rows["东岸证券"]
    assert (old["state"], old["valid_to"], old["superseded_by"]) == ("ended", "2026-11-09", new["relation_id"])
    assert old["superseded_at"].startswith("2026-11-07") and old["valid_to_basis"] is None
    assert (new["state"], new["valid_from"], new["superseded_by"]) == ("active", "2026-11-09", None)
    assert old["group_key"] == new["group_key"] and old["group_key"]


def test_without_a_start_day_the_old_one_ends_on_the_day_it_is_said_marked_approx(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-07", "我：X1 票据转到东岸证券了。"))
    write(world, moved_extract).close()
    old = by_bank(world)["海港银行"]
    assert (old["state"], old["valid_to"], old["valid_to_precision"], old["valid_to_basis"]) == ("ended", "2026-11-07", "day", "approx")


def test_a_later_message_about_earlier_days_only_fills_history(world: World) -> None:
    """D153 第 1 条：两句都写了开始日子，后说的那条开始得更早：只补历史，现在的不动。"""
    deliver(world, day_packet("2026-11-05", "我：X1 票据 11 月 5 日起在东岸证券。"))
    write(world, moved_extract).close()
    deliver(world, day_packet("2026-11-10", "我：X1 票据 10 月 1 日起在海港银行。"))
    write(world, moved_extract).close()
    rows = by_bank(world)
    current, history = rows["东岸证券"], rows["海港银行"]
    assert (current["state"], current["superseded_by"]) == ("active", None)
    assert (history["state"], history["valid_from"], history["valid_to"], history["superseded_by"]) == (
        "ended", "2026-10-01", "2026-11-05", current["relation_id"])


def test_without_a_start_day_on_one_side_the_later_one_wins(world: World) -> None:
    """D153 第 1 条：有一句没写开始日子的，照说话的先后算。"""
    deliver(world, day_packet("2026-11-05", "我：X1 票据 11 月 5 日起在东岸证券。"))
    write(world, moved_extract).close()
    deliver(world, day_packet("2026-11-10", "我：X1 票据在海港银行。"))
    write(world, moved_extract).close()
    rows = by_bank(world)
    assert rows["东岸证券"]["superseded_by"] == rows["海港银行"]["relation_id"]
    assert rows["海港银行"]["state"] == "active"


def test_two_banks_in_the_same_sentence_both_wait_for_review_until_the_owner_keeps_both(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行，也可能在东岸证券。"))
    write(world, moved_extract).close()
    rows = by_bank(world)
    assert {(row["status"], row["pending_reason"]) for row in rows.values()} == {("pending", "other")}
    (review,) = world.rows("SELECT review_id, kind, claims FROM review_queue")
    claims = json.loads(review["claims"])
    assert review["kind"] == "other" and claims["reason"] == "same_time"
    assert claims["relations"] == sorted(row["relation_id"] for row in rows.values())
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "both"]) == 0
    assert {row["status"] for row in by_bank(world).values()} == {"verified"}


def test_holding_two_things_at_once_does_not_replace_anything(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            for item in (NOTE, STOCK):
                if str(item["name"]) in text:
                    found.append(relation(OWNER, "持有", item, (line.ref, text)))
        return {"entities": [OWNER, NOTE, STOCK], "facts": [], "relations": found}

    deliver(world, day_packet("2026-11-01", "我：我有 X1 票据。"), day_packet("2026-11-07", "我：我还有 X2。"))
    write(world, extract).close()
    rows = world.rows("SELECT state, superseded_by, group_key, identity_key FROM relations")
    assert {row["state"] for row in rows} == {"active"} and all(row["superseded_by"] is None for row in rows)
    assert all(row["group_key"] == row["identity_key"] for row in rows)  # 同一时间可以几个的：一条自己一组


def test_a_frozen_one_is_not_replaced_and_the_owner_decides(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, moved_extract).close()
    old = by_bank(world)["海港银行"]["relation_id"]
    freeze(world.settings, "prod", [old])
    deliver(world, day_packet("2026-11-07", "我：X1 票据转到东岸证券了。"))
    write(world, moved_extract).close()
    rows = by_bank(world)
    new = rows["东岸证券"]["relation_id"]
    assert (rows["海港银行"]["state"], rows["海港银行"]["superseded_by"]) == ("active", None)
    assert (rows["东岸证券"]["status"], rows["东岸证券"]["pending_reason"]) == ("pending", "frozen_conflict")
    (review,) = world.rows("SELECT review_id, claims FROM review_queue WHERE kind = 'frozen_conflict'")
    assert json.loads(review["claims"]) == {"frozen": old, "new": new}
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "frozen"]) == 0
    rows = by_bank(world)
    assert (rows["东岸证券"]["state"], rows["东岸证券"]["superseded_by"], rows["东岸证券"]["status"]) == ("ended", old, "verified")
    assert rows["海港银行"]["state"] == "active"


def test_keeping_the_new_one_unfreezes_and_replaces(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, moved_extract).close()
    old = by_bank(world)["海港银行"]["relation_id"]
    freeze(world.settings, "prod", [old])
    deliver(world, day_packet("2026-11-07", "我：X1 票据转到东岸证券了。"))
    write(world, moved_extract).close()
    review = world.rows("SELECT review_id FROM review_queue WHERE kind = 'frozen_conflict'")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "new"]) == 0
    rows = by_bank(world)
    assert rows["海港银行"]["frozen"] == 0 and rows["海港银行"]["superseded_by"] == rows["东岸证券"]["relation_id"]
    assert rows["东岸证券"]["status"] == "verified"


def part_of_extract(window: Window) -> dict[str, object]:
    """外贸部：写着归哪家（示例贸易华东分公司、示例贸易）的那一行，隶属于那一家（一行写了两家的两条都起）；
    写着“11 月 9 日起”的照那句写开始日子。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        if "外贸部" not in text:
            continue
        fields: dict[str, object] = {}
        if "11 月 9 日起" in text:
            fields.update(valid_from="2026-11-09", time_precision="day")
        if str(BRANCH["name"]) in text:
            found.append(relation(DEPT, "隶属于", BRANCH, (line.ref, text), **fields))
        if str(TRADE["name"]) in text.replace(str(BRANCH["name"]), ""):
            found.append(relation(DEPT, "隶属于", TRADE, (line.ref, text), **fields))
    return {"entities": [DEPT, BRANCH, TRADE], "facts": [], "relations": found}


def by_parent(world: World) -> dict[str, dict]:
    rows = world.rows(
        "SELECT r.*, e.canonical_name AS parent FROM relations AS r JOIN entities AS e ON e.entity_id = r.object_entity_id"
        " WHERE r.relation_type = '隶属于'"
    )
    return {row["parent"]: dict(row) for row in rows}


def test_a_department_moved_to_another_parent_ends_the_old_one(world: World) -> None:
    """D210、卡 7：一个部门同一时间只归一家（按主体分组，和托管于一样）。改挂到别家的，旧的那条从新的开始那天结束。"""
    deliver(world, day_packet("2026-11-01", "我：外贸部是示例贸易的。"),
            day_packet("2026-11-07", "我：外贸部 11 月 9 日起归示例贸易华东分公司。"))
    write(world, part_of_extract).close()
    rows = by_parent(world)
    old, new = rows["示例贸易"], rows["示例贸易华东分公司"]
    assert (old["state"], old["valid_to"], old["superseded_by"]) == ("ended", "2026-11-09", new["relation_id"])
    assert (new["state"], new["valid_from"], new["superseded_by"]) == ("active", "2026-11-09", None)
    assert old["group_key"] == new["group_key"] and old["group_key"] != old["identity_key"]


def test_two_parents_in_the_same_sentence_both_wait_for_review(world: World) -> None:
    """同一句话里说一个部门归两家：两条都留着、都进待确认（和托管于一样）。"""
    deliver(world, day_packet("2026-11-01", "我：外贸部归示例贸易，也可能归示例贸易华东分公司。"))
    write(world, part_of_extract).close()
    rows = by_parent(world)
    assert len(rows) == 2 and {(row["status"], row["pending_reason"]) for row in rows.values()} == {("pending", "other")}
    (review,) = world.rows("SELECT kind, claims FROM review_queue")
    assert review["kind"] == "other" and json.loads(review["claims"])["reason"] == "same_time"


def test_a_department_and_a_branch_each_have_their_own_parent(world: World) -> None:
    """按主体分组：外贸部归分公司、分公司归公司，是两组，谁也不顶掉谁。"""
    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            if "外贸部" in text:
                found.append(relation(DEPT, "隶属于", BRANCH, (line.ref, text)))
            elif "华东分公司" in text:
                found.append(relation(BRANCH, "隶属于", TRADE, (line.ref, text)))
        return {"entities": [DEPT, BRANCH, TRADE], "facts": [], "relations": found}

    deliver(world, day_packet("2026-11-01", "我：外贸部在示例贸易华东分公司下面。"),
            day_packet("2026-11-07", "我：示例贸易华东分公司是示例贸易的。"))
    write(world, extract).close()
    rows = world.rows("SELECT state, superseded_by, group_key FROM relations WHERE relation_type = '隶属于'")
    assert len(rows) == 2 and {row["state"] for row in rows} == {"active"}
    assert all(row["superseded_by"] is None for row in rows) and rows[0]["group_key"] != rows[1]["group_key"]


def responsible_extract(window: Window) -> dict[str, object]:
    """越南那批：写着小李、老周负责的那一行起负责；写着谁在哪家（任职于）的那一行起任职于。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        for person in (LI, ZHOU):
            name = str(person["mention"])
            if f"{name}负责越南那批" in text or f"{name}来跟越南那批" in text:
                found.append(relation(person, "负责", ORDER, (line.ref, text)))
            for bank in (BANK, BROKER):
                if f"{name}在{bank['name']}" in text:
                    found.append(relation(person, "任职于", bank, (line.ref, text)))
    return {"entities": [LI, ZHOU, ORDER, BANK, BROKER], "facts": [], "relations": found}


def responsible(world: World) -> dict[str, dict]:
    rows = world.rows(
        "SELECT r.*, e.canonical_name AS who FROM relations AS r JOIN entities AS e ON e.entity_id = r.subject_entity_id"
        " WHERE r.relation_type = '负责'"
    )
    return {row["who"]: dict(row) for row in rows}


def test_people_from_different_firms_both_stay_responsible(world: World) -> None:
    """D153 第 2 条：先看这个人任职于哪家，不同家的都留着。"""
    deliver(
        world,
        day_packet("2026-11-01", "我：小李在海港银行。", "我：老周在东岸证券。"),
        day_packet("2026-11-02", "我：小李负责越南那批。"),
        day_packet("2026-11-05", "我：老周负责越南那批。"),
    )
    write(world, responsible_extract).close()
    rows = responsible(world)
    assert {row["state"] for row in rows.values()} == {"active"}
    assert all(row["superseded_by"] is None for row in rows.values())
    assert rows["李一舟"]["group_key"] != rows["周建宇"]["group_key"]


def test_the_same_side_replaces_and_a_later_employer_regroups(world: World) -> None:
    """看不出在哪家的（主人这边的人）：后来的顶掉先来的；后来才知道他们在不同的两家，重新分组，都留着。"""
    deliver(world, day_packet("2026-11-02", "我：小李负责越南那批。"), day_packet("2026-11-05", "我：老周来跟越南那批。"))
    write(world, responsible_extract).close()
    rows = responsible(world)
    assert rows["李一舟"]["superseded_by"] == rows["周建宇"]["relation_id"]
    deliver(world, day_packet("2026-11-06", "我：小李在海港银行。", "我：老周在东岸证券。"))
    write(world, responsible_extract).close()
    rows = responsible(world)
    assert all(row["superseded_by"] is None and row["state"] == "active" for row in rows.values())


SOUTH = {"key": "e16", "mention": "示例贸易华南分公司", "name": "示例贸易华南分公司", "type": "公司或机构"}
# 和小李、老周、越南那批放在同一个窗口里的，key 不能和它们（e11 到 e13）撞
TRADE_K, DEPT_K, BRANCH_K = dict(TRADE, key="e17"), dict(DEPT, key="e18"), dict(BRANCH, key="e19")


def org_rules(*specs: tuple[str, dict, str, dict]):  # type: ignore[no-untyped-def]
    """写死的 L3：一行里写着 specs 的那几个字，就起那一条关系（主体, 关系, 客体），证据是这一行。"""
    def extract(window: Window) -> dict[str, object]:
        entities: dict[str, dict] = {}
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            for phrase, subject, kind, obj in specs:
                if phrase in line.text:
                    entities[str(subject["key"])] = subject
                    entities[str(obj["key"])] = obj
                    found.append(relation(subject, kind, obj, (line.ref, text)))
        return {"entities": list(entities.values()), "facts": [], "relations": found}

    return extract


ORG_RULES = org_rules(
    ("小李在外贸部", LI, "任职于", DEPT_K), ("小李在示例贸易华东分公司", LI, "任职于", BRANCH_K),
    ("小李也在示例贸易", LI, "任职于", TRADE_K), ("老周在示例贸易华南分公司", ZHOU, "任职于", SOUTH),
    ("老周在示例贸易。", ZHOU, "任职于", TRADE_K), ("外贸部是示例贸易的", DEPT_K, "隶属于", TRADE_K),
    ("华东分公司是示例贸易的", BRANCH_K, "隶属于", TRADE_K), ("华南分公司是示例贸易的", SOUTH, "隶属于", TRADE_K),
    ("小李负责越南那批", LI, "负责", ORDER), ("老周负责越南那批", ZHOU, "负责", ORDER),
)


def test_a_department_and_its_company_represent_the_same_firm(world: World) -> None:
    """D210、D215：负责的“代表哪一家”顺着隶属于找到最上面那家：一个在示例贸易外贸部、一个在示例贸易（没说部门），先后负责
    同一张单子，算同一家，后来的顶掉前一个（不改的话，部门和公司是两家，两个都留着）。"""
    deliver(world, day_packet("2026-11-01", "我：小李在外贸部。", "我：老周在示例贸易。", "我：外贸部是示例贸易的。"),
            day_packet("2026-11-02", "我：小李负责越南那批。"), day_packet("2026-11-05", "我：老周负责越南那批。"))
    write(world, ORG_RULES).close()
    rows = responsible(world)
    assert rows["李一舟"]["superseded_by"] == rows["周建宇"]["relation_id"]
    assert rows["李一舟"]["group_key"] == rows["周建宇"]["group_key"] and rows["周建宇"]["state"] == "active"


def test_two_branches_of_one_company_represent_the_same_firm(world: World) -> None:
    """D215（主人定的）：同一家公司的不同分公司算同一家：华东分公司的、华南分公司的先后负责同一张单子，后来的顶掉前一个。"""
    deliver(world, day_packet("2026-11-01", "我：小李在示例贸易华东分公司。", "我：老周在示例贸易华南分公司。",
                              "我：华东分公司是示例贸易的。", "我：华南分公司是示例贸易的。"),
            day_packet("2026-11-02", "我：小李负责越南那批。"), day_packet("2026-11-05", "我：老周负责越南那批。"))
    write(world, ORG_RULES).close()
    rows = responsible(world)
    assert rows["李一舟"]["superseded_by"] == rows["周建宇"]["relation_id"]


def test_a_job_at_the_company_and_at_its_department_is_still_one_firm(world: World) -> None:
    """同一个人记着任职于示例贸易、也任职于它的外贸部（卡 4：说了部门以后原来那条照旧）：往上都是示例贸易，算一家，
    不当成几家都有、算主人这边。"""
    deliver(world, day_packet("2026-11-01", "我：小李在外贸部。", "我：小李也在示例贸易。", "我：老周在示例贸易。",
                              "我：外贸部是示例贸易的。"),
            day_packet("2026-11-02", "我：小李负责越南那批。"), day_packet("2026-11-05", "我：老周负责越南那批。"))
    write(world, ORG_RULES).close()
    rows = responsible(world)
    assert rows["李一舟"]["superseded_by"] == rows["周建宇"]["relation_id"]


def test_a_part_of_written_later_regroups_the_responsibilities(world: World) -> None:
    """隶属于后来才写进账：在那个部门任职的人，他们负责的那几组跟着重算（和任职于变了重算负责一样）。"""
    deliver(world, day_packet("2026-11-01", "我：小李在外贸部。", "我：老周在示例贸易。"),
            day_packet("2026-11-02", "我：小李负责越南那批。"), day_packet("2026-11-05", "我：老周负责越南那批。"))
    write(world, ORG_RULES).close()
    rows = responsible(world)
    assert all(row["superseded_by"] is None for row in rows.values())  # 还不知道外贸部是示例贸易的：两家，都留着
    deliver(world, day_packet("2026-11-06", "我：外贸部是示例贸易的。"))
    write(world, ORG_RULES).close()
    rows = responsible(world)
    assert rows["李一舟"]["superseded_by"] == rows["周建宇"]["relation_id"]


def test_climbing_stops_after_five_levels_and_on_a_loop(world: World) -> None:
    """往上最多走 5 层（D215）：第 0 层的人往上停在第 5 层，和第 6 层的人算两家；绕圈的不会一直找下去。"""
    levels = [{"key": f"e{30 + n}", "mention": f"示例层{n}号", "name": f"示例层{n}号", "type": "公司或机构"} for n in range(7)]
    specs = [(f"示例层{n}号归示例层{n + 1}号", levels[n], "隶属于", levels[n + 1]) for n in range(6)]
    specs += [("小李在示例层0号", LI, "任职于", levels[0]), ("老周在示例层6号", ZHOU, "任职于", levels[6]),
              ("示例层3号又归示例层0号", levels[3], "隶属于", levels[0]),
              ("小李负责越南那批", LI, "负责", ORDER), ("老周负责越南那批", ZHOU, "负责", ORDER)]
    extract = org_rules(*specs)
    chain = [f"我：示例层{n}号归示例层{n + 1}号。" for n in range(6)]
    deliver(world, day_packet("2026-11-01", *chain, "我：小李在示例层0号。", "我：老周在示例层6号。"),
            day_packet("2026-11-02", "我：小李负责越南那批。"), day_packet("2026-11-05", "我：老周负责越南那批。"))
    write(world, extract).close()
    rows = responsible(world)
    assert all(row["superseded_by"] is None for row in rows.values())  # 第 0 层往上停在第 5 层，和第 6 层是两家
    # 第 3 层改归第 0 层（新的顶掉归第 4 层的那条）：0 → 1 → 2 → 3 → 0 绕成一圈。照样写得完，从第 0 层往上停在第 3 层
    deliver(world, day_packet("2026-11-06", "我：示例层3号又归示例层0号。"))
    conn = write(world, extract)
    ids = {row["canonical_name"]: row["entity_id"] for row in conn.execute("SELECT entity_id, canonical_name FROM entities")}
    scope = conn.execute("SELECT scope_partitions, scope_sensitive FROM relations WHERE relation_type = '隶属于' LIMIT 1").fetchone()
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake")
    assert writer._top_unit(ids["示例层0号"], scope[0], scope[1]) == ids["示例层3号"]
    assert writer._top_unit(ids["示例层4号"], scope[0], scope[1]) == ids["示例层6号"]
    conn.close()
    assert len(responsible(world)) == 2


def test_a_paused_holder_is_replaced_and_taking_it_back_is_a_new_segment(world: World) -> None:
    """R36、C05 那种：小李负责 → 先停一下（暂停）→ 老周接手 → 小李又接回来：小李两段。"""

    def extract(window: Window) -> dict[str, object]:
        found = responsible_extract(window)
        for line in window.targets:
            if "先停" in line.text:
                found["relations"].append(relation(LI, "负责", ORDER, (line.ref, line.text.rstrip("。")), state="暂停"))  # type: ignore[attr-defined]
            if "又交回小李" in line.text:
                found["relations"].append(relation(LI, "负责", ORDER, (line.ref, line.text.rstrip("。"))))  # type: ignore[attr-defined]
        return found

    deliver(
        world,
        day_packet("2026-11-01", "我：小李负责越南那批。"),
        day_packet("2026-11-03", "我：越南那批小李先停一下。"),
        day_packet("2026-11-05", "我：老周来跟越南那批。"),
        day_packet("2026-11-20", "我：越南那批又交回小李。"),
    )
    write(world, extract).close()
    # 暂停是软变化（J4.4 第 4 段）：先挡住，判了暂停才算（这里主人答 B）
    review = world.rows("SELECT review_id FROM review_queue WHERE kind = 'soft_change'")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--verdict", "B"]) == 0
    rows = world.rows(
        "SELECT r.*, e.canonical_name AS who FROM relations AS r JOIN entities AS e ON e.entity_id = r.subject_entity_id"
        " WHERE r.relation_type = '负责' ORDER BY r.as_of_epoch"
    )
    assert [(row["who"], row["state"]) for row in rows] == [("李一舟", "ended"), ("周建宇", "ended"), ("李一舟", "active")]
    assert rows[0]["superseded_by"] == rows[1]["relation_id"] and rows[1]["superseded_by"] == rows[2]["relation_id"]
    assert rows[0]["paused_at"] is None  # 被顶掉了，不是暂停着


def test_the_pause_day_is_the_written_start_or_the_day_it_is_said(world: World) -> None:
    """D153 第 3 条：话里写了从哪天起停用那天，没写的用说这句话那天。"""

    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            item = NOTE if "X1" in text else STOCK
            if "停" in text:
                found.append(relation(item, "托管于", BANK, (line.ref, text), state="暂停"))
        return {"entities": [NOTE, STOCK, BANK], "facts": [], "relations": found}

    # 2026-11-05 是星期四，下周一是 11 月 9 日
    deliver(world, day_packet("2026-11-05", "我：X1 票据在海港银行那边下周一起先停。", "我：X2 在海港银行那边先停。"))
    write(world, extract).close()
    rows = {row["name"]: row for row in world.rows(
        "SELECT e.canonical_name AS name, r.state, r.paused_at, r.paused_at_precision FROM relations AS r"
        " JOIN entities AS e ON e.entity_id = r.subject_entity_id")}
    assert (rows["X1 票据"]["state"], rows["X1 票据"]["paused_at"], rows["X1 票据"]["paused_at_precision"]) == ("paused", "2026-11-09", "day")
    assert (rows["X2"]["paused_at"], rows["X2"]["paused_at_precision"]) == ("2026-11-05", "day")


def test_scoring_reads_a_real_replacement_as_a_change(world: World) -> None:
    from javmem.scoring.results import side_from_result

    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-07", "我：X1 票据转到东岸证券了。"))
    write(world, moved_extract).close()
    rows = by_bank(world)
    side, _ = side_from_result(world.data, ["2026-11-01_2330_desk-bot", "2026-11-07_2330_desk-bot"], library="prod")
    claim_of = {row["relation_id"]: row["claim_id"] for row in world.rows("SELECT relation_id, claim_id FROM relation_claims")}
    (change,) = side.changes  # 前一头是被替代那一段最后说的那一处，后一头是替代它的那一段最早说的那一处
    assert (change.before.id, change.after.id) == (claim_of[rows["海港银行"]["relation_id"]], claim_of[rows["东岸证券"]["relation_id"]])  # type: ignore[union-attr]
    assert change.evidence == side.relations[claim_of[rows["东岸证券"]["relation_id"]]].evidence
