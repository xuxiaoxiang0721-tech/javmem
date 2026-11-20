"""软变化的犹豫词表（J4.5）：哪些说法碰上哪几组，哪几组单独出现就算候选。句子都是编的。"""

from __future__ import annotations

import pytest

from javmem.soft.words import GROUPS, STRONG, WEAK, hedges, is_strong


@pytest.mark.parametrize(
    ("text", "group"),
    [
        ("东湖那批先放放。", "暂停"),
        ("南港那张合同先不管了。", "暂停"),
        ("报价的事暂缓一下。", "暂停"),
        ("先停一下，等客户回复。", "暂停"),
        ("We will put the sample order on hold.", "暂停"),
        ("Let's hold off on the second batch.", "暂停"),
        ("这个回头再说。", "往后推"),
        ("等客户回复再定。", "往后推"),
        ("Shipment is postponed to next month.", "往后推"),
        ("Keep the old supplier for now.", "往后推"),
        ("交期看情况。", "拿不准"),
        ("原则上同意。", "拿不准"),
        ("The launch date is TBD.", "拿不准"),
        ("We will ship tentatively in March.", "拿不准"),
        ("可能换小严跟。", "可能"),
        ("如果对方降价就换供应商。", "条件"),
        ("Unless the buyer confirms, keep it.", "条件"),
        ("我在考虑换一家工厂。", "打算"),
        ("南港那张合同取消了吗？", "问"),
    ],
)
def test_each_group_is_found(text: str, group: str) -> None:
    assert group in hedges(text)


def test_plain_sentences_hit_nothing() -> None:
    assert hedges("东湖那批订单 11 月 20 日出货。") == ()
    assert hedges("Holding company structure was signed.") == ()  # hold 不算 on hold
    assert hedges("The ifs and buts.") == ()  # if 按整词认


def test_groups_come_back_in_a_fixed_order() -> None:
    assert hedges("如果来不及就先放放，回头再说？") == ("暂停", "往后推", "条件", "问")
    assert set(GROUPS) == set(STRONG) | set(WEAK)


def test_only_the_first_three_groups_are_strong() -> None:
    assert is_strong(("暂停",)) and is_strong(("拿不准", "条件"))
    assert not is_strong(("可能", "条件", "打算", "问"))
    assert not is_strong(())


def test_full_width_and_case_do_not_matter() -> None:
    assert "暂停" in hedges("ON HOLD until further notice")
    assert "问" in hedges("取消了吗？")
