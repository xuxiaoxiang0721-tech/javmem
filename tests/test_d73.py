"""D73 第 18、19 条（撤回、冻结、来源那一段第二遍审阅留下的第 7、8 条），11-10 定了放进附件正文那一段顺带修。
每一条先写测试、看着红了再修。样例都是编的，模型和向量用假的。"""

from __future__ import annotations

from helpers import World
from test_review import by_value, frozen_and_contradicted, reviews
from test_withdraw import P1
from test_write import day_packet, deliver, l3_fact, write

from javmem.review import decide
from javmem.review import freeze as set_frozen
from javmem.withdraw import withdraw

P2 = "2026-11-02_2330_desk-bot"


# ---------- 第 18 条：撤回以后重新对齐，只看这次删掉的出处 ----------


def full_name_extract(window):  # noqa: ANN001, ANN201
    """原文只写“老周”，L3 写了全名“周建华”：全名在窗口里找不到，本来就不记别名、没有出处。"""
    text = window.targets[0].text
    entity = {"key": "e1", "mention": "老周", "name": "周建华", "type": "人"}
    return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}


def canonical_names(world: World) -> list[str]:
    return [row["canonical_name"] for row in world.rows("SELECT canonical_name FROM entities ORDER BY canonical_name")]


def test_a_full_name_l3_filled_in_is_not_realigned_after_a_withdrawal(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：老周管越南。"), day_packet("2026-11-02", "我：老周明天回来。"))
    write(world, full_name_extract).close()
    assert canonical_names(world) == ["周建华"]
    report = withdraw(world.settings, "prod", packets=[P1])
    # 撤回只碰到了这个实体，删掉的出处里没有“周建华”：标准名不换，提到它的包也不用重写
    assert canonical_names(world) == ["周建华"]
    assert P2 not in report.requeued
    assert world.packet_row(P2)["extraction"] == "done"
    claims = world.rows("SELECT cache_key FROM fact_claims")
    assert claims and all(row["cache_key"] for row in claims)  # 说法没记成“没写过”


def test_a_name_whose_last_source_was_withdrawn_is_still_realigned(world: World) -> None:
    """改了以后，标准名的出处真的被这次撤回删光了的，照旧重新对齐（审阅 15 那条测试也还在）。"""

    def extract(window):  # noqa: ANN001, ANN202
        text = window.targets[0].text
        name = "周建国" if "周建国" in text else "老周"
        entity = {"key": "e1", "mention": "老周", "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}

    deliver(world, day_packet("2026-11-01", "我：周建国就是老周，管越南。"), day_packet("2026-11-02", "我：老周明天回来。"))
    write(world, extract).close()
    report = withdraw(world.settings, "prod", packets=[P1])
    assert P2 in report.requeued and canonical_names(world) == ["老周"]


# ---------- 第 19 条：冻结新说法时自动关掉的冲突，解除冻结时也补 unfrozen ----------


def test_unfreezing_the_newer_value_asks_about_the_conflict_again(world: World) -> None:
    old, new = frozen_and_contradicted(world)
    set_frozen(world.settings, "prod", [new])
    assert reviews(world) == []  # 两条主人都冻结了，那条冲突自动关掉
    set_frozen(world.settings, "prod", [new], frozen=False)
    values = by_value(world)
    assert (values["11-15"]["frozen"], values["11-15"]["status"]) == (1, "verified")
    assert (values["11-20"]["frozen"], values["11-20"]["status"], values["11-20"]["pending_reason"]) == (
        0,
        "pending",
        "frozen_conflict",
    )
    [item] = reviews(world)
    assert (item["kind"], item["claims"]) == ("frozen_conflict", {"frozen": old, "new": new})
    set_frozen(world.settings, "prod", [new])  # 再冻结它：又是两条都冻结，又关掉
    assert reviews(world) == []


def test_an_explicit_keep_both_survives_freezing_and_unfreezing_the_newer_value(world: World) -> None:
    """主人自己定的“两边都留”不是冻结时自动关掉的，解除冻结新说法时不作废。"""
    old, new = frozen_and_contradicted(world)
    decide(world.settings, "prod", int(reviews(world)[0]["review_id"]), "both")
    set_frozen(world.settings, "prod", [new])
    set_frozen(world.settings, "prod", [new], frozen=False)
    assert reviews(world) == []
    values = by_value(world)
    assert (values["11-20"]["status"], values["11-20"]["superseded_by"]) == ("verified", None)
