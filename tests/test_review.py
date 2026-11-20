"""冻结和待确认队列（J2.12）：冻结、解除冻结、给冻结事实写入矛盾信息、处理待确认、每天一条数量提醒、
30 天没人处理就两边都留、巡检的警告标“看过了”。样例都是编的，抽取用手写的 L3 结果，向量用假的。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from helpers import CHAT_ID, World, build_packet, warnings_of
from test_write import day_packet, deliver, facts, l3_fact, ship_extract, write

from javmem.cli import main
from javmem.intake import intake_lock
from javmem.patrol import AckError, acknowledge, run_patrol
from javmem.review import EXPIRE_DAYS, ReviewError, decide, freeze, remind
from javmem.withdraw import withdraw

SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 9, 8, 30, tzinfo=SHANGHAI)


@pytest.fixture
def clocked(world: World) -> World:
    return world.at(NOW)


def by_value(world: World) -> dict[str, object]:
    conn = world.ledger()
    try:
        return {row["value"]: dict(row) for row in facts(conn)}
    finally:
        conn.close()


def reviews(world: World, *, open_only: bool = True) -> list[dict[str, object]]:
    where = " WHERE decided_at IS NULL" if open_only else ""
    rows = world.rows(f"SELECT review_id, kind, claims, decision, decided_by FROM review_queue{where} ORDER BY review_id")
    return [{**dict(row), "claims": json.loads(row["claims"])} for row in rows]


def frozen_and_contradicted(world: World) -> tuple[str, str]:
    """11-01 说 11-15 出货，冻结它；11-03 又说改到 11-20。返回 (冻结的, 新说法)。"""
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship_extract).close()
    old = by_value(world)["11-15"]["fact_id"]
    assert main(["freeze", "--library", "prod", "--fact", str(old)]) == 0
    deliver(world, day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    write(world, ship_extract).close()
    return str(old), str(by_value(world)["11-20"]["fact_id"])


# ---------- 冻结 ----------


def test_writing_a_contradiction_keeps_the_frozen_value_and_queues_it(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    """导航 J2.12 的验收：给冻结事实写入矛盾信息，原值不变，队列出现待确认项。"""
    old, new = frozen_and_contradicted(world)
    values = by_value(world)
    assert (values["11-15"]["frozen"], values["11-15"]["status"], values["11-15"]["superseded_by"]) == (1, "verified", None)
    assert (values["11-20"]["status"], values["11-20"]["pending_reason"], values["11-20"]["superseded_by"]) == (
        "pending",
        "frozen_conflict",
        None,
    )
    assert [(r["kind"], r["claims"]) for r in reviews(world)] == [("frozen_conflict", {"frozen": old, "new": new})]
    capsys.readouterr()
    assert main(["review", "--library", "prod"]) == 0
    out = capsys.readouterr().out
    assert "和冻结的冲突" in out and old in out and new in out and "待确认 1 条" in out
    assert main(["review", "--library", "prod", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["choices"] == ["frozen", "new", "both"] and listed[0]["detail"]["frozen"]["frozen"] is True


def test_freezing_an_old_value_brings_it_back_and_the_newer_one_waits(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    write(world, ship_extract).close()
    assert by_value(world)["11-15"]["superseded_by"] is not None
    old = str(by_value(world)["11-15"]["fact_id"])
    assert freeze(world.settings, "prod", [old]) == [old]
    values = by_value(world)
    assert values["11-15"]["superseded_by"] is None and values["11-20"]["pending_reason"] == "frozen_conflict"
    assert freeze(world.settings, "prod", [old]) == []  # 本来就冻结了


def test_unfreezing_lets_the_newer_value_replace_it(world: World) -> None:
    old, new = frozen_and_contradicted(world)
    assert main(["freeze", "--library", "prod", "--fact", old, "--undo"]) == 0
    values = by_value(world)
    assert values["11-15"]["frozen"] == 0 and values["11-15"]["superseded_by"] == new
    assert values["11-20"]["status"] == "verified" and values["11-20"]["pending_reason"] is None
    assert [(r["decision"], r["decided_by"]) for r in reviews(world, open_only=False)] == [("unfrozen", "owner")]
    # 再冻结：冲突重新问
    freeze(world.settings, "prod", [old])
    assert [r["claims"] for r in reviews(world)] == [{"frozen": old, "new": new}]


def test_freeze_refuses_unknown_and_hidden_facts(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship_extract).close()
    assert main(["freeze", "--library", "prod", "--fact", "F0000000000000000"]) == 1
    fact_id = str(by_value(world)["11-15"]["fact_id"])
    freeze(world.settings, "prod", [fact_id])
    withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])  # 证据都撤回了：藏起来
    with pytest.raises(ReviewError):
        freeze(world.settings, "prod", [fact_id], frozen=False)


def test_freeze_is_busy_while_writing(world: World) -> None:
    from javmem.pipeline.write import WriteLock

    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship_extract).close()
    fact_id = str(by_value(world)["11-15"]["fact_id"])
    with WriteLock(world.data):
        assert main(["freeze", "--library", "prod", "--fact", fact_id]) == 3
    with intake_lock(world.data):
        assert main(["freeze", "--library", "prod", "--fact", fact_id]) == 3
    assert by_value(world)["11-15"]["frozen"] == 0


# ---------- 处理待确认 ----------


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        # 留原值：新说法算被冻结的那条替代了
        ("frozen", {"11-15": (1, "verified", None), "11-20": (0, "verified", "old")}),
        # 用新值：解除冻结，照常按新旧
        ("new", {"11-15": (0, "verified", "new"), "11-20": (0, "verified", None)}),
        # 两边都留：都现行
        ("both", {"11-15": (1, "verified", None), "11-20": (0, "verified", None)}),
    ],
)
def test_deciding_a_frozen_conflict(world: World, choice: str, expected: dict[str, tuple[int, str, str | None]]) -> None:
    old, new = frozen_and_contradicted(world)
    review_id = int(reviews(world)[0]["review_id"])
    assert main(["review", "--library", "prod", "--decide", str(review_id), "--keep", choice]) == 0
    names = {"old": old, "new": new, None: None}
    values = by_value(world)
    for value, (frozen, status, superseded_by) in expected.items():
        assert (values[value]["frozen"], values[value]["status"], values[value]["superseded_by"]) == (frozen, status, names[superseded_by])
    assert reviews(world) == []
    # 再写一遍同样的矛盾，不再进队列，状态也不回到待确认
    deliver(world, day_packet("2026-11-05", "我：越南那批订单还是 11-20 出货。"))
    write(world, ship_extract).close()
    assert reviews(world) == []
    assert by_value(world)["11-20"]["status"] == "verified"


def test_decide_refuses_wrong_choices_and_decided_items(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    frozen_and_contradicted(world)
    review_id = int(reviews(world)[0]["review_id"])
    assert main(["review", "--library", "prod", "--decide", str(review_id), "--drop"]) == 1
    assert "只能定成" in capsys.readouterr().err
    assert main(["review", "--library", "prod", "--decide", str(review_id)]) == 2  # 没写怎么定
    decide(world.settings, "prod", review_id, "both")
    with pytest.raises(ReviewError):
        decide(world.settings, "prod", review_id, "both")
    with pytest.raises(ReviewError):
        decide(world.settings, "prod", 999, "both")


def test_ambiguous_entity_kept_apart(world: World) -> None:
    def extract(window):  # noqa: ANN001, ANN202
        text = window.targets[0].text
        name = "周建国" if "周建国" in text else "周立新" if "周立新" in text else "老周"
        entity = {"key": "e1", "mention": "老周", "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}

    deliver(
        world,
        day_packet("2026-11-01", "我：周建国就是老周，管越南。"),
        day_packet("2026-11-02", "我：周立新也叫老周，管泰国。"),
        day_packet("2026-11-03", "我：老周明天回来。"),
    )
    write(world, extract).close()
    item = reviews(world)[0]
    assert item["kind"] == "ambiguous_entity"
    assert world.rows("SELECT status FROM facts WHERE content = '老周明天回来'")[0]["status"] == "pending"
    with pytest.raises(ReviewError):  # 只能两边都留，合并是 J3.6 的事
        decide(world.settings, "prod", int(item["review_id"]), "frozen")
    decide(world.settings, "prod", int(item["review_id"]), "both")
    row = world.rows("SELECT status, pending_reason FROM facts WHERE content = '老周明天回来'")[0]
    assert (row["status"], row["pending_reason"]) == ("verified", None)


@pytest.mark.parametrize("choice", ["frozen", "drop", "hidden"])
def test_frozen_fact_whose_evidence_was_withdrawn(world: World, choice: str) -> None:
    """冻结事实的证据都撤回了，先藏着；主人定：放回来（没有证据也看得到）、删掉、接着藏着。"""
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship_extract).close()
    fact_id = str(by_value(world)["11-15"]["fact_id"])
    freeze(world.settings, "prod", [fact_id])
    withdraw(world.settings, "prod", packets=["2026-11-01_2330_desk-bot"])
    item = reviews(world)[0]
    assert item["claims"]["reason"] == "evidence_withdrawn"
    decide(world.settings, "prod", int(item["review_id"]), choice)
    rows = world.rows("SELECT withdrawn_at FROM facts WHERE fact_id = ?", fact_id)
    if choice == "frozen":
        assert rows[0]["withdrawn_at"] is None
    elif choice == "drop":
        assert rows == []
    else:
        assert rows[0]["withdrawn_at"] is not None
    assert reviews(world) == []


# ---------- 每天一条数量提醒，30 天两边都留 ----------


def test_daily_reminder_is_one_line_a_day(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    frozen_and_contradicted(world)
    capsys.readouterr()
    assert main(["review", "--remind"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and "[prod] 待确认 1 条" in out[0] and "和冻结的冲突 1" in out[0]
    assert main(["review", "--remind"]) == 0
    assert capsys.readouterr().out == ""  # 今天提醒过了
    assert main(["review", "--remind", "--force"]) == 0
    assert "待确认 1 条" in capsys.readouterr().out
    later = world.at(datetime.now(SHANGHAI) + timedelta(days=1))
    assert remind(later.settings, "prod").shown


def test_open_items_older_than_30_days_keep_both(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    old, new = frozen_and_contradicted(world)
    created = datetime.fromisoformat(str(world.rows("SELECT created_at FROM review_queue")[0]["created_at"]))
    soon = world.at(created + timedelta(days=EXPIRE_DAYS - 1))
    assert remind(soon.settings, "prod", force=True).expired == []
    late = world.at(created + timedelta(days=EXPIRE_DAYS, minutes=1))
    reminder = remind(late.settings, "prod", force=True)
    assert len(reminder.expired) == 1 and reminder.open == 0
    assert "照规矩关掉了" in reminder.line() and "两边都留" in reminder.line()
    assert [(r["decision"], r["decided_by"]) for r in reviews(world, open_only=False)] == [("keep_both", "auto")]
    values = by_value(world)
    assert values["11-15"]["frozen"] == 1 and values["11-15"]["superseded_by"] is None  # 冻结的原值不变
    assert values["11-20"]["status"] == "verified" and values["11-20"]["superseded_by"] is None  # 新说法也留着


def test_reminder_counts_but_does_not_expire_while_writing(world: World) -> None:
    from javmem.pipeline.write import WriteLock

    frozen_and_contradicted(world)
    created = datetime.fromisoformat(str(world.rows("SELECT created_at FROM review_queue")[0]["created_at"]))
    late = world.at(created + timedelta(days=EXPIRE_DAYS + 1))
    with WriteLock(world.data):
        reminder = remind(late.settings, "prod", force=True)
    assert reminder.skipped_expiry and reminder.expired == [] and reminder.open == 1


# ---------- 巡检的警告标“看过了” ----------


def test_acknowledged_warnings_stop_reporting_until_they_come_again(clocked: World, capsys: pytest.CaptureFixture[str]) -> None:
    clocked.drop("inbox/desk-bot", build_packet(body="## 原话 · 10:30–10:31\n- [10:30] 我：第一句话。\n## 别的\n随便写点。\n"))
    clocked.intake()
    clocked.drop("inbox/desk-bot", build_packet(body="## 原话 · 10:30–10:31\n- [10:30] 我：内容不一样了。\n"))
    clocked.intake()  # 同一个 revision、内容不一样：same_revision_different
    items = [item for item in run_patrol(clocked.settings) if item.ref]
    kinds = {item.kind: item.ref for item in items}
    assert {"unknown_heading", "same_revision_different"} <= set(kinds)
    capsys.readouterr()
    assert main(["patrol", "--ack", str(kinds["unknown_heading"])]) == 0
    remaining = {item.kind for item in run_patrol(clocked.settings) if item.ref}
    assert "unknown_heading" not in remaining and "same_revision_different" in remaining
    acknowledge(clocked.settings, kinds=["same_revision_different"])
    assert not any(item.kind == "same_revision_different" for item in run_patrol(clocked.settings))
    # 又来一次：重新报
    clocked.drop("inbox/desk-bot", build_packet(body="## 原话 · 10:30–10:31\n- [10:30] 我：又不一样了。\n"))
    clocked.intake()
    assert any(item.kind == "same_revision_different" for item in run_patrol(clocked.settings))
    assert "unknown_heading" in {kind for kind, _ in warnings_of(clocked, CHAT_ID)}  # 记录还在，只是不报
    with pytest.raises(AckError):
        acknowledge(clocked.settings, ["prod/W9999"])
    assert main(["patrol", "--ack", "prod/W9999"]) == 1


def test_acknowledged_daily_gap_stops_reporting_until_it_changes(clocked: World) -> None:
    first = build_packet(stamp="2026-11-06_2330", covers_from="2026-11-05T23:30:00+08:00", covers_to="2026-11-06T23:30:00+08:00",
                         created_at="2026-11-06T23:30:00+08:00", body="## 原话 · 2026-11-06 10:30–10:31\n- [2026-11-06 10:30] 我：第一天。\n")
    third = build_packet(body="## 原话\n- [10:30] 我：第三天。\n")
    clocked.drop("inbox/desk-bot", first)
    clocked.drop("inbox/desk-bot", third)
    clocked.intake()
    gaps = [item for item in run_patrol(clocked.settings) if item.kind == "daily_gap"]
    assert len(gaps) == 1 and gaps[0].ref.startswith("prod/G")
    acknowledge(clocked.settings, [gaps[0].ref])
    assert not any(item.kind == "daily_gap" for item in run_patrol(clocked.settings))
    # 补进来一部分，缺口变了：重新报
    middle = build_packet(stamp="2026-11-07_1200", covers_from="2026-11-06T23:30:00+08:00", covers_to="2026-11-07T12:00:00+08:00",
                          created_at="2026-11-07T12:00:00+08:00", body="## 原话 · 2026-11-07 10:30–10:31\n- [2026-11-07 10:30] 我：第二天。\n")
    clocked.drop("inbox/desk-bot", middle)
    clocked.intake()
    assert [item.kind for item in run_patrol(clocked.settings) if item.kind == "daily_gap"] == ["daily_gap"]
