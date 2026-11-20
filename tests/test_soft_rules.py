"""软变化的结论 → L5 的动作（J4.5，javmem.soft.rules）：六种结论各做什么，哪些进待确认。"""

from __future__ import annotations

import pytest

from javmem.soft import rules

SAID = "2026-11-07T10:30:00+08:00"


def item(verdict: str, **fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "target": "O1", "about": True, "verdict": verdict, "condition": None, "effective_at": None,
        "effective_precision": "unknown", "new_value": None, "sure": True, "finance": False, "quote": "先停一下",
        "reason": "", "quote_ok": True, "consistent": True, "date_ok": True,
    }
    base.update(fields)
    return base


def test_still_valid_and_hesitation_both_keep() -> None:
    assert rules.action(item("A"), said_at=SAID).kind == rules.KEEP
    assert rules.action(item("E", condition="对方降价就换"), said_at=SAID).kind == rules.KEEP


def test_pause_keeps_the_condition_and_defaults_to_when_it_was_said() -> None:
    action = rules.action(item("B", condition="等客户回复"), said_at=SAID)
    assert (action.kind, action.at, action.precision, action.condition) == (rules.PAUSE, SAID, "minute", "等客户回复")


def test_end_and_replace_use_the_effective_day_when_given() -> None:
    ended = rules.action(item("C", effective_at="2026-11-09", effective_precision="day"), said_at=SAID)
    assert (ended.kind, ended.at, ended.precision) == (rules.END, "2026-11-09", "day")
    replaced = rules.action(item("D", effective_at="2026-11-09", effective_precision="day", new_value="小严负责"), said_at=SAID)
    assert (replaced.kind, replaced.at, replaced.needs_new) == (rules.REPLACE, "2026-11-09", True)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"verdict": "F"}, rules.UNCLEAR),
        ({"sure": False}, rules.NOT_SURE),
        ({"quote_ok": False}, rules.QUOTE_MISMATCH),
        ({"about": False, "consistent": False}, rules.INCONSISTENT),
    ],
)
def test_unclear_unsure_bad_quote_and_inconsistent_go_to_review(fields: dict[str, object], reason: str) -> None:
    fields = dict(fields)
    verdict = str(fields.pop("verdict", "B"))
    action = rules.action(item(verdict, condition="等客户回复", **fields), said_at=SAID)
    assert action.kind == rules.REVIEW and action.review_kind == "soft_change"
    assert reason in action.reasons
    assert action.suggestion == verdict  # 模型的建议留给主人看


def test_finance_alone_does_not_send_it_to_review() -> None:
    """主人 11-16 定的：碰不碰钱不单独分流，只记下来。"""
    assert rules.action(item("C", finance=True), said_at=SAID).kind == rules.END


def test_not_judged_and_expiry() -> None:
    pending = rules.not_judged()
    assert pending.kind == rules.REVIEW and pending.reasons == (rules.NOT_JUDGED,)
    assert rules.EXPIRE_ACTION == rules.KEEP  # 30 天没人管：当成不改


def test_an_unreadable_date_on_a_change_goes_to_review() -> None:
    """主人 11-16 审阅后定的：模型写的哪天认不出（date_ok 是 false），暂停、结束、换新的不拿说话那天顶上，进待确认问他。
    不改旧的（A、E）用不着日子，照旧不动。"""
    for verdict in ("B", "C", "D"):
        action = rules.action(item(verdict, date_ok=False), said_at=SAID)
        assert action.kind == rules.REVIEW and rules.DATE_UNREADABLE in action.reasons
        assert action.suggestion == verdict  # 模型的建议留给主人看
    for verdict in ("A", "E"):
        assert rules.action(item(verdict, date_ok=False), said_at=SAID).kind == rules.KEEP


def test_a_bad_quote_sends_every_verdict_to_review() -> None:
    """主人 11-16 定的：引文对不上的一律进待确认，A、E 也一样（E 会把这句话抽的记成打算，不是什么都不做）。"""
    for verdict in "ABCDE":
        action = rules.action(item(verdict, quote_ok=False), said_at=SAID)
        assert action.kind == rules.REVIEW and rules.QUOTE_MISMATCH in action.reasons, verdict


def test_still_valid_and_not_about_it_needs_no_quote() -> None:
    """主人 11-16 审阅后定的：判 A、又说没说到这一条（about 是 false）的，不要求引文，引文空着不进待确认。
    写了引文却对不上的照旧进；说到了它的 A、判 E 的，引文空着也照旧进。"""
    for empty in ("", "  ", None):
        action = rules.action(item("A", about=False, quote=empty, quote_ok=False), said_at=SAID)
        assert action.kind == rules.KEEP, repr(empty)
    assert rules.action(item("A", about=False, quote="别处抄来的一句", quote_ok=False), said_at=SAID).kind == rules.REVIEW
    assert rules.action(item("A", about=True, quote="", quote_ok=False), said_at=SAID).kind == rules.REVIEW
    assert rules.action(item("E", about=False, quote="", quote_ok=False), said_at=SAID).kind == rules.REVIEW
    assert rules.action(item("A", about=False, quote="", quote_ok=False, sure=False), said_at=SAID).kind == rules.REVIEW


def test_expected_action_merges_still_valid_and_hesitation() -> None:
    assert rules.expected_action("A") == rules.expected_action("E") == rules.KEEP
    assert rules.expected_action("B") == rules.PAUSE
    assert rules.expected_action("F") == rules.REVIEW
    assert rules.expected_action("D", review=True) == rules.REVIEW
    with pytest.raises(ValueError):
        rules.expected_action("G")
    with pytest.raises(ValueError):
        rules.action(item("G"), said_at=SAID)
