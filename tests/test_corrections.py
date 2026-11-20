"""J4.6 对话里纠正关系（第 1 段：认法、L5、恢复、待确认）。例子都是编的，L3 的结果写死在这里，不调模型。

照 主人 11-17 定的：纠正以后原话没说哪天的，旧的结束、新的开始都空着，旧的记着被纠正（第 1 题 A）；投资、财务分区里的
关系被纠正先挡住问主人（第 2 题 A），30 天没人管接着挡着（第 3 题 B）；抽取没抽出来、对得上唯一一条现行关系的进待确认
（第 4 题 B）；只管关系（第 12 节第 1 条）。做法 J4.6-做法.md 第 11 节先这样定的：只认主人自己的话、软变化优先、R05 放在
L5、同一对人纠正成另一种亲属的旧的结束、只说了新的旧的不猜。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import World
from test_relations_judge import judged
from test_relations_write import bank_extract, resent
from test_relcheck import BANK, BROKER, OWNER, relation
from test_withdraw_review import rebuilt_world
from test_write import day_packet, deliver, write

from javmem.cli import main
from javmem.decisions import export_decisions, import_decisions
from javmem.ledger import open_ledger
from javmem.pipeline import correct
from javmem.pipeline.verify import Finding
from javmem.pipeline.window import Window
from javmem.pipeline.write import LedgerWriter
from javmem.review import freeze, open_items, remind
from javmem.withdraw import requeue_packets

MI = {"key": "e21", "mention": "宓春生", "name": "宓春生", "type": "人"}
KAN = {"key": "e22", "mention": "阚星野", "name": "阚星野", "type": "人"}
TEA = {"key": "e23", "mention": "锡兰那批茶具订单", "name": "锡兰那批茶具订单", "type": "订单或合同"}
QI = {"key": "e24", "mention": "祁望舒", "name": "祁望舒", "type": "人"}
GANTANG = {"key": "e25", "mention": "甘棠物流", "name": "甘棠物流", "type": "公司或机构"}
K7 = {"key": "e26", "mention": "K7 票据", "name": "K7 票据", "type": "订单或合同"}
CHENG = {"key": "e27", "mention": "澄江银行", "name": "澄江银行", "type": "公司或机构"}
BEIYU = {"key": "e28", "mention": "北屿证券", "name": "北屿证券", "type": "公司或机构"}
YAN = {"key": "e29", "mention": "晏青禾", "name": "晏青禾", "type": "人"}
GAO = {"key": "e30", "mention": "郜立人", "name": "郜立人", "type": "人"}
PO = {"key": "e31", "mention": "PO-7731", "name": "PO-7731", "type": "订单或合同"}
PO2 = {"key": "e32", "mention": "PO-8102", "name": "PO-8102", "type": "订单或合同"}
Z3 = {"key": "e33", "mention": "Z3 累购", "name": "Z3 累购", "type": "订单或合同"}

MI_TEA = ("宓春生", "负责", "锡兰那批茶具订单")
KAN_TEA = ("阚星野", "负责", "锡兰那批茶具订单")


def rules(*specs: tuple[str, dict, str, dict, dict]):  # type: ignore[no-untyped-def]
    """写死的 L3：specs 每一条是 (这一行里有的字, 主体, 关系, 客体, 别的字段)；正文里一行碰上一条就抽一条，证据是这一行
    （去掉句号）。"""

    def extract(window: Window) -> dict[str, object]:
        entities: dict[str, dict] = {}
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            for phrase, subject, kind, obj, fields in specs:
                if phrase in line.text:
                    entities[str(subject["key"])] = subject
                    entities[str(obj["key"])] = obj
                    found.append(relation(subject, kind, obj, (line.ref, text), **fields))
        return {"entities": list(entities.values()), "facts": [], "relations": found}

    return extract


TEA_RULES = rules(
    ("交给宓春生负责", MI, "负责", TEA, {"valid_from": "2026-11-01", "time_precision": "day"}),
    ("是阚星野负责", KAN, "负责", TEA, {}),
)


def rel_rows(world: World, *ends: str) -> list[dict]:
    """两头的名字和关系对得上的那几行（一条关系可以有几段），按说的先后。"""
    rows = world.rows(
        "SELECT r.*, s.canonical_name AS subject, o.canonical_name AS object FROM relations AS r"
        " JOIN entities AS s ON s.entity_id = r.subject_entity_id JOIN entities AS o ON o.entity_id = r.object_entity_id"
        " ORDER BY r.as_of_epoch, r.relation_id"
    )
    subject, kind, obj = ends
    return [dict(row) for row in rows if (row["subject"], row["relation_type"], row["object"]) == (subject, kind, obj)]


def one(world: World, *ends: str) -> dict:
    (row,) = rel_rows(world, *ends)
    return row


def current(row: dict) -> bool:
    return (row["withdrawn_at"] is None and row["superseded_by"] is None and row["state"] in ("active", "paused")
            and row["status"] != "pending")


def sid(day: str, line: int, sentence: int = 1, source: str = "desk-bot") -> str:
    """day_packet 那一天第 line 句话的第 sentence 句的编号。"""
    return f"{day}_2330_{source}#1.{line}.{sentence}"


def reviews(world: World) -> list[dict]:
    return [{**dict(row), "claims": json.loads(row["claims"])} for row in world.rows("SELECT * FROM review_queue ORDER BY review_id")]


# ---------- 认法 ----------


@pytest.mark.parametrize(
    "text",
    [
        "不对，是阚星野负责。",
        "你记错了，祁望舒早就不在甘棠物流了。",
        "搞错了，那单不是宓春生的。",
        "不是宓春生，是阚星野。",
        "更正一下：K7 票据在北屿证券。",
        "其实是阚星野在跟。",
        "我是说阚星野。",
        "No, that's wrong — he left in September.",
        "Actually he moved to another bank.",
    ],
)
def test_correction_words_are_recognised(text: str) -> None:
    assert correct.is_cue(text)


@pytest.mark.parametrize(
    "text",
    [
        "这个报价有点不对劲。",
        "对不对？",
        "上次的样册还不错了。",
        "错过了那班车。",
        "应该是阚星野吧。",
        "是不是阚星野负责？",
        "不对吗？",
        "其实那家的包装也凑合。",
        "阚星野负责锡兰那批。",
    ],
)
def test_other_words_are_not_corrections(text: str) -> None:
    assert not correct.is_cue(text)


# ---------- L5：换一个、说没有 ----------


def test_owner_correction_replaces_without_a_day_and_marks_the_old_one(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，锡兰那批茶具订单是阚星野负责的。"))
    write(world, TEA_RULES).close()
    old, new = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert (old["state"], old["valid_to"], old["valid_to_precision"], old["superseded_by"]) == (
        "ended", None, "unknown", new["relation_id"])
    assert old["corrected_at"].startswith("2026-11-12") and old["correction_sentence"] == sid("2026-11-12", 1)
    assert current(new) and new["valid_from"] is None and new["corrected_at"] is None
    (claim,) = world.rows("SELECT correction FROM relation_claims WHERE relation_id = ?", new["relation_id"])
    assert claim["correction"] == sid("2026-11-12", 1)
    assert [row for row in reviews(world)] == []


@pytest.mark.parametrize("new_first", [True, False])
def test_one_sentence_that_starts_the_new_and_ends_the_old_is_a_replacement(world: World, new_first: bool) -> None:
    """L3 照“换人”那一条，同一句里抽出新的（生效）和旧的（已结束）：是换掉，不是“同一句话里两个（A 或 B）”，不管哪条在前。"""
    new = ("不是宓春生", KAN, "负责", TEA, {})
    old = ("不是宓春生", MI, "负责", TEA, {"state": "已结束"})
    extract = rules(("交给宓春生负责", MI, "负责", TEA, {}), *((new, old) if new_first else (old, new)))
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，锡兰那批茶具订单是阚星野负责的，不是宓春生。"))
    write(world, extract).close()
    assert reviews(world) == []
    mi, kan = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert current(kan) and (mi["state"], mi["valid_to"]) == ("ended", None)
    assert mi["correction_sentence"] == sid("2026-11-12", 1)


def test_said_day_start_written_by_l3_on_a_correction_is_dropped(world: World) -> None:
    """L3 有时把说话那天写成开始那天（当场交代那种）：纠正的那一处，原话又没写日子的，去掉。"""
    extract = rules(
        ("交给宓春生负责", MI, "负责", TEA, {"valid_from": "2026-11-01", "time_precision": "day"}),
        ("是阚星野负责", KAN, "负责", TEA, {"valid_from": "2026-11-12", "time_precision": "day"}),
    )
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，锡兰那批茶具订单是阚星野负责的。"))
    write(world, extract).close()
    assert one(world, *KAN_TEA)["valid_from"] is None
    assert one(world, *MI_TEA)["valid_to"] is None


def test_a_day_written_in_the_correction_is_kept(world: World) -> None:
    extract = rules(
        ("交给宓春生负责", MI, "负责", TEA, {"valid_from": "2026-09-20", "time_precision": "day"}),
        ("就交给阚星野", KAN, "负责", TEA, {"valid_from": "2026-10-01", "time_precision": "day"}),
    )
    deliver(world, day_packet("2026-09-20", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-10", "我：你记错了，锡兰那批茶具订单 10 月 1 日起就交给阚星野了。"))
    write(world, extract).close()
    old, new = one(world, *MI_TEA), one(world, "阚星野", "负责", "锡兰那批茶具订单")
    assert (new["valid_from"], old["valid_to"], old["superseded_by"]) == ("2026-10-01", "2026-10-01", new["relation_id"])
    assert old["correction_sentence"] == sid("2026-11-10", 1)


def test_an_ended_correction_ends_the_segment_without_a_day_and_marks_it(world: World) -> None:
    extract = rules(
        ("在甘棠物流做", QI, "任职于", GANTANG, {}),
        ("早就不在甘棠物流", QI, "任职于", GANTANG, {"state": "已结束", "valid_to": "2026-11-12", "time_precision": "day"}),
    )
    deliver(world, day_packet("2026-09-28", "我：祁望舒在甘棠物流做运营经理。"),
            day_packet("2026-11-12", "我：你记错了，祁望舒早就不在甘棠物流了。"))
    write(world, extract).close()
    row = one(world, "祁望舒", "任职于", "甘棠物流")
    assert (row["state"], row["valid_to"], row["superseded_by"]) == ("ended", None, None)  # 说话那天不顶上（第 1 题 A）
    assert row["corrected_at"].startswith("2026-11-12") and row["correction_sentence"] == sid("2026-11-12", 1)


def test_a_short_correction_just_before_counts(world: World) -> None:
    """主人紧挨着的上一条只有“不对”这种两三个字、两分钟以内：也算纠正，记的是那一句。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对。", "我：锡兰那批茶具订单是阚星野负责的。"))
    write(world, TEA_RULES).close()
    old = one(world, *MI_TEA)
    assert (old["valid_to"], old["correction_sentence"]) == (None, sid("2026-11-12", 1))


@pytest.mark.parametrize(
    "line",
    ["鞠望川：不对，锡兰那批茶具订单是阚星野负责的。", "我：[转发 自 鞠望川] 不对，锡兰那批茶具订单是阚星野负责的。"],
)
def test_a_colleague_or_a_forward_is_an_ordinary_change(world: World, line: str) -> None:
    """只认主人自己的话：同事说的、转发的照普通新说法，说话那天算换的那天、记成约，不记纠正。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"), day_packet("2026-11-12", line))
    write(world, TEA_RULES).close()
    old = one(world, *MI_TEA)
    assert (old["valid_to"], old["valid_to_basis"], old["corrected_at"]) == ("2026-11-12", "approx", None)


def test_soft_words_win_over_a_correction(world: World) -> None:
    extract = rules(
        ("交给宓春生负责", MI, "负责", TEA, {}),
        ("先别让宓春生管", MI, "负责", TEA, {"state": "暂停"}),
    )
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，锡兰那批茶具订单先别让宓春生管了，等客户定了再说。"))
    write(world, extract).close()
    assert world.rows("SELECT correction FROM relation_claims WHERE correction IS NOT NULL") == []
    assert [row["kind"] for row in reviews(world)] == ["soft_change"]
    assert any(current(row) for row in rel_rows(world, *MI_TEA))


# ---------- 冻结的、财务投资的 ----------


def test_a_frozen_one_is_kept_and_the_correction_waits(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"))
    write(world, TEA_RULES).close()
    freeze(world.settings, "prod", [one(world, *MI_TEA)["relation_id"]])
    deliver(world, day_packet("2026-11-12", "我：不对，锡兰那批茶具订单是阚星野负责的。"))
    write(world, TEA_RULES).close()
    old, new = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert current(old) and old["frozen"] == 1 and old["corrected_at"] is None
    assert (new["status"], new["pending_reason"]) == ("pending", "frozen_conflict")
    assert [row["kind"] for row in reviews(world)] == ["frozen_conflict"]


K7_RULES = rules(
    ("K7 票据放在澄江银行", K7, "托管于", CHENG, {}),
    ("转到北屿证券", K7, "托管于", BEIYU, {}),
)


def held_finance(world: World) -> dict:
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
            day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。", domain="[investment]"))
    write(world, K7_RULES).close()
    (review,) = reviews(world)
    return review


def test_a_finance_correction_waits_for_the_owner(world: World) -> None:
    review = held_finance(world)
    old, new = one(world, "K7 票据", "托管于", "澄江银行"), one(world, "K7 票据", "托管于", "北屿证券")
    assert current(old) and old["corrected_at"] is None
    assert (new["status"], new["pending_reason"]) == ("pending", "manual_review")
    claims = review["claims"]
    assert (review["kind"], claims["reason"], claims["item"], claims["relation"], claims["sentence"]) == (
        "manual_review", "correction", "relation", new["relation_id"], sid("2026-11-11", 1))
    conn = open_ledger(world.settings.library("prod"))
    try:
        (item,) = open_items(conn, world.settings)
    finally:
        conn.close()
    assert (item.kind, item.choices) == ("correction", ["new", "hidden"])


def test_the_owner_keeping_a_finance_correction_applies_it(world: World) -> None:
    review = held_finance(world)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0
    old, new = one(world, "K7 票据", "托管于", "澄江银行"), one(world, "K7 票据", "托管于", "北屿证券")
    assert (old["state"], old["valid_to"], old["superseded_by"]) == ("ended", None, new["relation_id"])
    assert old["correction_sentence"] == sid("2026-11-11", 1) and current(new)


def test_the_owner_refusing_a_finance_correction_keeps_it_held(world: World) -> None:
    review = held_finance(world)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "hidden"]) == 0
    assert current(one(world, "K7 票据", "托管于", "澄江银行"))
    assert not current(one(world, "K7 票据", "托管于", "北屿证券"))
    assert [row["decision"] for row in reviews(world)] == ["keep_hidden"]


def test_a_finance_correction_left_30_days_stays_held(world: World) -> None:
    """第 3 题 B：30 天没人管，接着挡着，旧的照旧，不再提醒。"""
    review = held_finance(world)
    world.execute("UPDATE review_queue SET created_at = '2026-01-01T00:00:00+08:00'")
    assert remind(world.settings, "prod", force=True).expired == [review["review_id"]]
    assert current(one(world, "K7 票据", "托管于", "澄江银行"))
    assert [(row["decision"], row["decided_by"]) for row in reviews(world)] == [("keep_hidden", "auto")]


def test_one_question_for_a_sentence_that_ends_the_old_and_starts_the_new(world: World) -> None:
    extract = rules(
        ("K7 票据放在澄江银行", K7, "托管于", CHENG, {}),
        ("从澄江银行转到北屿证券", K7, "托管于", CHENG, {"state": "已结束"}),
        ("转到北屿证券", K7, "托管于", BEIYU, {}),
    )
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
            day_packet("2026-11-11", "我：不对，K7 票据早就从澄江银行转到北屿证券了。", domain="[investment]"))
    write(world, extract).close()
    assert [(row["kind"], row["claims"]["reason"]) for row in reviews(world)] == [("manual_review", "correction")]
    assert any(current(row) for row in rel_rows(world, "K7 票据", "托管于", "澄江银行"))


def test_a_frozen_finance_relation_is_asked_once_as_a_frozen_conflict(world: World) -> None:
    """冻结的优先：财务投资分区里冻结着的那条被纠正，只进“和冻结的冲突”，不再另挡一次。"""
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"))
    write(world, K7_RULES).close()
    freeze(world.settings, "prod", [one(world, "K7 票据", "托管于", "澄江银行")["relation_id"]])
    deliver(world, day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。", domain="[investment]"))
    write(world, K7_RULES).close()
    assert [row["kind"] for row in reviews(world)] == ["frozen_conflict"]
    new = one(world, "K7 票据", "托管于", "北屿证券")
    assert (new["status"], new["pending_reason"]) == ("pending", "frozen_conflict")


def test_a_business_correction_is_not_held(world: World) -> None:
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。"), day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。"))
    write(world, K7_RULES).close()
    assert reviews(world) == [] and current(one(world, "K7 票据", "托管于", "北屿证券"))


def test_strict_partitions_come_from_the_setting(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_STRICT_PARTITIONS", "business")
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。"), day_packet("2026-11-11", "我：不对，K7 票据早就转到北屿证券了。"))
    write(world, K7_RULES).close()
    assert [row["claims"]["reason"] for row in reviews(world)] == ["correction"]


# ---------- 恢复 ----------


def test_withdrawing_the_correction_brings_the_old_one_back(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，锡兰那批茶具订单是阚星野负责的。"))
    write(world, TEA_RULES).close()
    assert main(["withdraw", "--library", "prod", "--sentence", sid("2026-11-12", 1)]) == 0
    old = one(world, *MI_TEA)
    assert current(old) and (old["valid_to"], old["corrected_at"], old["correction_sentence"]) == (None, None, None)
    assert rel_rows(world, *KAN_TEA) == []


def test_withdrawing_only_the_short_correction_word_drops_the_mark(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对。", "我：锡兰那批茶具订单是阚星野负责的。"))
    write(world, TEA_RULES).close()
    assert main(["withdraw", "--library", "prod", "--sentence", sid("2026-11-12", 1)]) == 0
    old = one(world, *MI_TEA)
    assert old["corrected_at"] is None and old["valid_to_basis"] == "approx"  # 照普通的换人
    assert world.rows("SELECT correction FROM relation_claims WHERE correction IS NOT NULL") == []


def test_withdrawing_a_held_finance_correction_drops_its_review(world: World) -> None:
    held_finance(world)
    assert main(["withdraw", "--library", "prod", "--sentence", sid("2026-11-11", 1)]) == 0
    assert reviews(world) == [] and current(one(world, "K7 票据", "托管于", "澄江银行"))


# ---------- R05：Bot 当场说错、主人当场纠正 ----------

ECHO_RULES = rules(
    ("交给宓春生负责", MI, "负责", TEA, {}),
    ("是宓春生在跟", MI, "负责", TEA, {"speaker": "desk-bot"}),
    ("是阚星野负责", KAN, "负责", TEA, {}),
)


def echoed(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，是阚星野负责。"))
    write(world, ECHO_RULES).close()


def test_a_bot_echo_right_before_the_owner_correction_is_not_recorded(world: World) -> None:
    echoed(world)
    old = one(world, *MI_TEA)
    evidence = [row["sentence_id"] for row in world.rows("SELECT sentence_id FROM relation_evidence WHERE relation_id = ?",
                                                         old["relation_id"])]
    assert sid("2026-11-12", 1) not in evidence and old["correction_sentence"] == sid("2026-11-12", 2)
    (held,) = world.rows("SELECT reason, rule_id, payload FROM quarantine WHERE item_kind = 'relation'")
    assert (held["reason"], held["rule_id"]) == ("L5:corrected_on_spot", "L5")
    assert json.loads(held["payload"])["corrected_by"] == sid("2026-11-12", 2)


def test_a_bot_line_about_another_group_is_kept(world: World) -> None:
    extract = rules(
        ("交给宓春生负责", MI, "负责", TEA, {}),
        ("祁望舒在甘棠物流", QI, "任职于", GANTANG, {"speaker": "desk-bot"}),
        ("是阚星野负责", KAN, "负责", TEA, {}),
    )
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：祁望舒在甘棠物流上班，锡兰那批茶具订单也在他那边过。", "我：不对，是阚星野负责。"))
    write(world, extract).close()
    assert world.rows("SELECT 1 FROM quarantine WHERE item_kind = 'relation'") == []
    assert current(one(world, "祁望舒", "任职于", "甘棠物流"))


def test_a_bot_echo_comes_back_after_the_correction_is_withdrawn(world: World) -> None:
    echoed(world)
    assert main(["withdraw", "--library", "prod", "--sentence", sid("2026-11-12", 2)]) == 0
    assert world.rows("SELECT 1 FROM quarantine WHERE item_kind = 'relation'") == []  # 纠正那句撤回了，挡掉的那一行跟着清
    write(world, ECHO_RULES).close()  # 包改回排队，重抽重写：Bot 那句照常记
    old = one(world, *MI_TEA)
    evidence = [row["sentence_id"] for row in world.rows("SELECT sentence_id FROM relation_evidence WHERE relation_id = ?",
                                                         old["relation_id"])]
    assert sid("2026-11-12", 1) in evidence and current(old)


# R05 的另一半（真调测试题 K05、K02）：L3 把 Bot 当场说错的那句和主人紧接着的纠正给成了同一处说法（证据两句）。
# Bot 那句从这一处的证据里去掉、记进隔离表（item_kind relation_evidence），这一处照主人的那句记。


def together(*specs: tuple[tuple[str, ...], dict, str, dict, dict]):  # type: ignore[no-untyped-def]
    """写死的 L3：specs 每一条是 (几行里各要有的字, 主体, 关系, 客体, 别的字段)；几行都碰上了就抽一处，证据是这几行。"""

    def extract(window: Window) -> dict[str, object]:
        entities: dict[str, dict] = {}
        found = []
        for phrases, subject, kind, obj, fields in specs:
            lines = [next((line for line in window.targets if phrase in line.text), None) for phrase in phrases]
            if any(line is None for line in lines):
                continue
            entities[str(subject["key"])] = subject
            entities[str(obj["key"])] = obj
            found.append(relation(subject, kind, obj, *[(line.ref, line.text.rstrip("。")) for line in lines], **fields))
        return {"entities": list(entities.values()), "facts": [], "relations": found}

    return extract


K7_SPOT = together(
    (("K7 票据放在澄江银行",), K7, "托管于", CHENG, {}),
    (("K7 票据在澄江银行", "早就转到北屿证券"), K7, "托管于", CHENG, {"state": "已结束"}),
    (("早就转到北屿证券",), K7, "托管于", BEIYU, {}),
)
TEA_SPOT_SPECS = (
    (("交给宓春生负责",), MI, "负责", TEA, {}),
    (("是宓春生在跟", "早就换成阚星野"), MI, "负责", TEA, {"state": "已结束"}),
    (("早就换成阚星野",), KAN, "负责", TEA, {}),
)


def evidence_of(world: World, *ends: str) -> list[str]:
    """两头对得上的那几行（待确认的也算）证据里的句子。"""
    return [row["sentence_id"] for one_row in rel_rows(world, *ends) for row in world.rows(
        "SELECT sentence_id FROM relation_evidence WHERE relation_id = ? ORDER BY sentence_id", one_row["relation_id"])]


def spot_rows(world: World) -> list[dict]:
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in world.rows(
        "SELECT * FROM quarantine WHERE item_kind = 'relation_evidence' AND reason = 'L5:corrected_on_spot'")]


def test_a_bot_line_inside_the_held_finance_correction_is_left_out(world: World) -> None:
    """K05：投资的那一处照规矩挡住；Bot 那句（“K7 票据在澄江银行”）不进挡住那一处的证据，主人认了改以后也不进旧的那条。"""
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
            day_packet("2026-11-11", "desk-bot：K7 票据在澄江银行，下周付息。", "我：不对，K7 票据早就转到北屿证券了。",
                       domain="[investment]"))
    write(world, K7_SPOT).close()
    bot, owner = sid("2026-11-11", 1), sid("2026-11-11", 2)
    assert bot not in evidence_of(world, "K7 票据", "托管于", "澄江银行")
    (row,) = spot_rows(world)
    assert (row["rule_id"], row["payload"]["evidence"]["sentence_id"], row["payload"]["corrected_by"]) == ("L5", bot, owner)
    assert row["payload"]["relation"] == {"subject": K7["key"], "relation": "托管于", "object": CHENG["key"]}
    (review,) = reviews(world)
    assert (review["kind"], review["claims"]["reason"]) == ("manual_review", "correction")
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0
    assert bot not in evidence_of(world, "K7 票据", "托管于", "澄江银行")
    assert not any(current(one_row) for one_row in rel_rows(world, "K7 票据", "托管于", "澄江银行"))


def test_a_bot_line_inside_the_owner_correction_is_left_out(world: World) -> None:
    """K02：不是投资的照常改；旧的那条结束、记着被主人那句纠正，证据里没有 Bot 那句；一处说法只记一行。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，早就换成阚星野了。"))
    write(world, together(*TEA_SPOT_SPECS)).close()
    bot, owner = sid("2026-11-12", 1), sid("2026-11-12", 2)
    old, new = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert (old["state"], old["correction_sentence"], old["superseded_by"]) == ("ended", owner, new["relation_id"])
    assert bot not in evidence_of(world, *MI_TEA) and owner in evidence_of(world, *MI_TEA) and current(new)
    (row,) = spot_rows(world)
    assert (row["payload"]["evidence"]["sentence_id"], row["payload"]["corrected_by"]) == (bot, owner)
    claims = world.rows("SELECT speaker_role, proofs FROM relation_claims WHERE correction IS NOT NULL AND state = 'ended'")
    assert [(one_claim["speaker_role"], [proof["sentence_id"] for proof in json.loads(one_claim["proofs"])])
            for one_claim in claims] == [("owner", [owner])]


def test_a_bot_line_inside_the_new_one_is_kept(world: World) -> None:
    """审阅第 3 条：只去说它结束了的那一处里的（Bot 说的正是被纠正掉的那一条）；新的那一处、还生效的那一处带着 Bot 那句的
    不去——Bot 那句可能说对了（主人纠正的是别的），去掉了剩下的证据常常两头都没提到。"""
    specs = (*TEA_SPOT_SPECS[:2], (("是宓春生在跟", "早就换成阚星野"), KAN, "负责", TEA, {}))
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，早就换成阚星野了。"))
    write(world, together(*specs)).close()
    bot = sid("2026-11-12", 1)
    assert bot not in evidence_of(world, *MI_TEA) and bot in evidence_of(world, *KAN_TEA)
    assert len(spot_rows(world)) == 1 and current(one(world, *KAN_TEA))


def test_a_right_bot_line_is_kept_when_the_owner_corrects_something_else(world: World) -> None:
    """审阅第 3 条：Bot 说对了这一条，主人纠正的是发货的日子：这一处还生效，Bot 那句照旧是证据。"""
    specs = ((("交给阚星野负责",), KAN, "负责", TEA, {}), (("是阚星野在跟", "阚星野那边改到下周一"), KAN, "负责", TEA, {}))
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给阚星野负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是阚星野在跟，周五发货。", "我：不对，阚星野那边改到下周一发货了。"))
    write(world, together(*specs)).close()
    assert spot_rows(world) == [] and sid("2026-11-12", 1) in evidence_of(world, *KAN_TEA)


def test_a_bot_question_is_not_taken_as_said_wrong(world: World) -> None:
    """审阅第 3 条：Bot 是问（“还是宓春生在跟吗？”），不是说错：不去、不进隔离表。"""
    specs = (
        (("交给宓春生负责",), MI, "负责", TEA, {}),
        (("还是宓春生在跟吗", "早就换成阚星野"), MI, "负责", TEA, {"state": "已结束"}),
        (("早就换成阚星野",), KAN, "负责", TEA, {}),
    )
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单还是宓春生在跟吗？", "我：不是，早就换成阚星野了。"))
    write(world, together(*specs)).close()
    assert spot_rows(world) == [] and sid("2026-11-12", 1) in evidence_of(world, *MI_TEA)
    assert one(world, *MI_TEA)["state"] == "ended"


def test_a_bot_line_after_the_correction_is_kept(world: World) -> None:
    """审阅第 2 条：Bot 那条后面紧接着的要是主人纠正的那一条（带纠正词那一句所在的消息）；主人纠正 → Bot 照着复述 →
    主人“对”，Bot 那条后面是主人的“对”，不是纠正，不去。"""
    specs = (
        (("交给宓春生负责",), MI, "负责", TEA, {}),
        (("早就不管锡兰那批茶具订单", "宓春生不再负责", "对，宓春生不管了"), MI, "负责", TEA, {"state": "已结束"}),
    )
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，宓春生早就不管锡兰那批茶具订单了。", "desk-bot：好的，宓春生不再负责锡兰那批茶具订单。",
                       "我：对，宓春生不管了。"))
    write(world, together(*specs)).close()
    assert spot_rows(world) == [] and sid("2026-11-12", 2) in evidence_of(world, *MI_TEA)
    assert one(world, *MI_TEA)["correction_sentence"] == sid("2026-11-12", 1)


def bot_or_combined(window: Window) -> dict[str, object]:
    """像真 L3：Bot 那句和主人的纠正都在，给成一处（证据两句）再加新的那一处；纠正撤回了、只剩 Bot 那句，照 Bot 那句抽一处。"""
    def find(phrase: str):  # type: ignore[no-untyped-def]
        return next((line for line in window.targets if phrase in line.text), None)

    def quote(line):  # type: ignore[no-untyped-def]
        return (line.ref, line.text.rstrip("。"))

    first, bot, fix = find("交给宓春生负责"), find("是宓春生在跟"), find("早就换成阚星野")
    found = [relation(MI, "负责", TEA, quote(first))] if first is not None else []
    if bot is not None and fix is not None:
        found += [relation(MI, "负责", TEA, quote(bot), quote(fix), state="已结束"), relation(KAN, "负责", TEA, quote(fix))]
    elif bot is not None:
        found.append(relation(MI, "负责", TEA, quote(bot), speaker="desk-bot"))
    return {"entities": [MI, KAN, TEA], "facts": [], "relations": found}


def test_withdrawing_the_corrected_relation_also_withdraws_the_left_out_bot_line(world: World) -> None:
    """审阅第 1 条（严重）：按关系撤回撤的是它的证据句；Bot 那句不在证据里了，也要跟着撤（它是这一处 L3 给的证据），
    不然纠正那句撤了、Bot 那句留着，重抽时照 Bot 那句又记回来。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，早就换成阚星野了。"))
    write(world, bot_or_combined).close()
    assert main(["withdraw", "--library", "prod", "--fact", one(world, *MI_TEA)["relation_id"]]) == 0
    withdrawn = [row["sentence_id"] for row in world.rows("SELECT sentence_id FROM raw_sentences WHERE withdrawn_at IS NOT NULL")]
    assert sid("2026-11-12", 1) in withdrawn
    write(world, bot_or_combined).close()
    assert not any(current(row) for row in rel_rows(world, *MI_TEA))


def test_a_claim_the_owner_dropped_leaves_no_bot_line_row(world: World) -> None:
    """审阅第 4 条：判断 C 拿不准、主人定了不要的那一处没写成说法，Bot 那句也不另记一行（只有那一处整条进隔离表）。"""
    lines = ("desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，早就换成阚星野了。")
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"))
    write(world, together(*TEA_SPOT_SPECS)).close()
    deliver(world, day_packet("2026-11-12", *lines))
    write(world, together(*TEA_SPOT_SPECS), order=judged("llm_unsure")).close()
    mine = {row["relation_id"] for row in rel_rows(world, *MI_TEA)}
    (review,) = [row for row in reviews(world) if row["claims"].get("relation") in mine]
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--drop"]) == 0
    deliver(world, resent("2026-11-12", *lines))
    write(world, together(*TEA_SPOT_SPECS), order=judged("llm_unsure")).close()
    assert spot_rows(world) == []


def test_a_day_only_the_left_out_bot_line_gave_is_dropped(world: World) -> None:
    """审阅第 5 条：Bot 那句不算证据，它写的日子也不算：剩下的证据里一个日子都没写的，L3 给的开始、结束日子去掉。"""
    specs = (
        (("交给宓春生负责",), MI, "负责", TEA, {"valid_from": "2026-11-08", "time_precision": "day"}),
        (("11 月 5 日起是宓春生在跟", "早就换成阚星野"), MI, "负责", TEA,
         {"state": "已结束", "valid_from": "2026-11-05", "time_precision": "day"}),
        (("早就换成阚星野",), KAN, "负责", TEA, {}),
    )
    deliver(world, day_packet("2026-11-08", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单 11 月 5 日起是宓春生在跟。", "我：不对，早就换成阚星野了。"))
    write(world, together(*specs)).close()
    old = one(world, *MI_TEA)
    assert (old["valid_from"], old["state"]) == ("2026-11-08", "ended") and len(spot_rows(world)) == 1


def test_a_bot_end_day_that_closed_the_claim_keeps_it_ended(world: World) -> None:
    """复审：L3 标生效、结束日子（已经过了）只有 Bot 那句写了：照日子算它结束了，Bot 那句去掉、日子也去掉，这一处照旧算
    结束（已结束、结束日子空着），不变成“纠正成宓春生还管着”、和阚星野一起进待确认。"""
    specs = (
        (("交给宓春生负责",), MI, "负责", TEA, {}),
        (("宓春生跟到 11 月 10 日", "早就换成阚星野"), MI, "负责", TEA, {"valid_to": "2026-11-10"}),
        (("早就换成阚星野",), KAN, "负责", TEA, {}),
    )
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单宓春生跟到 11 月 10 日。", "我：不对，早就换成阚星野了。"))
    write(world, together(*specs)).close()
    old, new = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert (old["state"], old["valid_to"], old["superseded_by"]) == ("ended", None, new["relation_id"]) and current(new)
    assert reviews(world) == [] and len(spot_rows(world)) == 1


def test_an_owner_day_said_elsewhere_is_kept(world: World) -> None:
    """复审：去日子只管 Bot 那句写了日子的；Bot 那句没写，日子是主人在同一个窗口别的一条里说的（“11 月 10 日就交接了”），照留。"""
    specs = (
        (("交给宓春生负责",), MI, "负责", TEA, {}),
        (("是宓春生在跟", "早就换成阚星野"), MI, "负责", TEA, {"state": "已结束", "valid_to": "2026-11-10"}),
        (("早就换成阚星野",), KAN, "负责", TEA, {}),
    )
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：宓春生 11 月 10 日就交接了。", "desk-bot：锡兰那批茶具订单是宓春生在跟。",
                       "我：不对，早就换成阚星野了。"))
    write(world, together(*specs)).close()
    (claim,) = world.rows("SELECT valid_to FROM relation_claims WHERE state = 'ended'")
    assert claim["valid_to"] == "2026-11-10" and len(spot_rows(world)) == 1


def test_the_soft_question_is_the_owners_own_hedge(world: World) -> None:
    """审阅第 6 条：主人自己的话有犹豫词、照软变化办（Bot 那句照旧在证据里）：候选句挑主人那句，不挑 Bot 的（“先暂缓”）。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单先暂缓，是宓春生在跟。", "我：不对，可能早就换成阚星野了。"))
    write(world, together(*TEA_SPOT_SPECS)).close()
    (claim,) = world.rows("SELECT soft_sentence FROM relation_claims WHERE state = 'ended'")
    assert claim["soft_sentence"] == sid("2026-11-12", 2) and spot_rows(world) == []


def test_a_bot_line_that_is_not_right_before_the_correction_is_kept(world: World) -> None:
    """只管 Bot 那条消息后面紧接着就是主人纠正的那一条（和 R05 一样）；中间主人还说了别的，Bot 那句照旧是证据。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：好的，我问问。", "我：不对，早就换成阚星野了。"))
    write(world, together(*TEA_SPOT_SPECS)).close()
    assert sid("2026-11-12", 1) in evidence_of(world, *MI_TEA) and spot_rows(world) == []


def test_a_hedging_bot_line_does_not_make_the_owner_correction_soft(world: World) -> None:
    """Bot 那句不算证据，也不拿来认软变化：Bot 说得含糊（“可能”），主人紧接着干脆地纠正的，照纠正办。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单可能是宓春生在跟。", "我：不对，早就换成阚星野了。"))
    write(world, together(*TEA_SPOT_SPECS)).close()
    old = one(world, *MI_TEA)
    assert (old["state"], old["correction_sentence"]) == ("ended", sid("2026-11-12", 2))
    assert len(spot_rows(world)) == 1 and current(one(world, *KAN_TEA))


def test_an_owner_correction_with_hedges_keeps_the_bot_line(world: World) -> None:
    """主人自己的话有犹豫词、照软变化办的，照旧：Bot 那句不去，不进隔离表。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，可能早就换成阚星野了。"))
    write(world, together(*TEA_SPOT_SPECS)).close()
    assert spot_rows(world) == [] and sid("2026-11-12", 1) in evidence_of(world, *MI_TEA)


def test_withdrawing_the_correction_clears_the_left_out_bot_line(world: World) -> None:
    """纠正那句撤回了：隔离表里那一行跟着清（按 payload 里的句子编号认），旧的回来。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，早就换成阚星野了。"))
    write(world, together(*TEA_SPOT_SPECS)).close()
    assert len(spot_rows(world)) == 1
    assert main(["withdraw", "--library", "prod", "--sentence", sid("2026-11-12", 2)]) == 0
    assert spot_rows(world) == [] and current(one(world, *MI_TEA))


def test_a_rebuild_leaves_out_the_same_bot_line(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packets = (day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
               day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，早就换成阚星野了。"))
    deliver(world, *packets)
    write(world, together(*TEA_SPOT_SPECS)).close()
    before = (evidence_of(world, *MI_TEA), [row["payload"] for row in spot_rows(world)])
    new = rebuilt_world(tmp_path, monkeypatch)
    deliver(new, *packets)
    write(new, together(*TEA_SPOT_SPECS)).close()
    assert (evidence_of(new, *MI_TEA), [row["payload"] for row in spot_rows(new)]) == before


# ---------- 亲属 ----------

KIN_SPECS = (
    ("晏青禾是我妹妹", YAN, "亲属", OWNER, {"kinship": "妹妹"}),
    ("晏青禾是我表妹", YAN, "亲属", OWNER, {"kinship": "表妹"}),
)
KIN_RULES = rules(*KIN_SPECS)


def kin_rows(world: World) -> dict[str, dict]:
    rows = world.rows("SELECT r.*, s.canonical_name AS subject FROM relations AS r"
                      " JOIN entities AS s ON s.entity_id = r.subject_entity_id WHERE r.relation_type = '亲属'")
    return {row["kinship"]: dict(row) for row in rows}


def test_a_kinship_correction_ends_the_other_kinship_of_the_same_pair(world: World) -> None:
    deliver(world, day_packet("2026-09-15", "我：晏青禾是我妹妹，下个月来上海。", domain="[personal]"),
            day_packet("2026-11-09", "我：记错了，晏青禾是我表妹，不是亲妹妹。", domain="[personal]"))
    write(world, KIN_RULES).close()
    rows = kin_rows(world)
    assert current(rows["表妹"])
    assert (rows["妹妹"]["state"], rows["妹妹"]["valid_to"], rows["妹妹"]["superseded_by"]) == (
        "ended", None, rows["表妹"]["relation_id"])
    assert rows["妹妹"]["correction_sentence"] == sid("2026-11-09", 1)


def test_two_kinships_without_a_correction_both_stay(world: World) -> None:
    deliver(world, day_packet("2026-09-15", "我：晏青禾是我妹妹，下个月来上海。", domain="[personal]"),
            day_packet("2026-11-09", "我：晏青禾是我表妹。", domain="[personal]"))
    write(world, KIN_RULES).close()
    rows = kin_rows(world)
    assert set(rows) == {"妹妹", "表妹"} and all(current(row) for row in rows.values())


def test_withdrawing_a_kinship_correction_brings_the_old_kinship_back(world: World) -> None:
    deliver(world, day_packet("2026-09-15", "我：晏青禾是我妹妹，下个月来上海。", domain="[personal]"),
            day_packet("2026-11-09", "我：记错了，晏青禾是我表妹，不是亲妹妹。", domain="[personal]"))
    write(world, KIN_RULES).close()
    assert set(kin_rows(world)) == {"妹妹", "表妹"}
    assert main(["withdraw", "--library", "prod", "--sentence", sid("2026-11-09", 1)]) == 0
    rows = kin_rows(world)
    assert set(rows) == {"妹妹"} and current(rows["妹妹"]) and rows["妹妹"]["corrected_at"] is None


# ---------- 抽取没抽出来的纠正（第 4 题 B） ----------

GAO_RULES = rules(("那单让郜立人跟", GAO, "负责", PO, {}), ("那单也让郜立人跟", GAO, "负责", PO2, {}))
ASKED = ("desk-bot：PO-7731 的出货单要不要先发给郜立人看？", "我：记错了，郜立人不负责这个单子。")


def test_an_unmatched_negation_asks_the_owner(world: World) -> None:
    deliver(world, day_packet("2026-10-20", "我：PO-7731 那单让郜立人跟。"), day_packet("2026-11-13", *ASKED))
    write(world, GAO_RULES).close()
    target = one(world, "郜立人", "负责", "PO-7731")
    assert current(target)
    (review,) = reviews(world)
    claims = review["claims"]
    assert (review["kind"], claims["reason"], claims["relation"], claims["sentence"]) == (
        "manual_review", "correction_unmatched", target["relation_id"], sid("2026-11-13", 2))


def test_keeping_an_unmatched_correction_ends_the_relation_and_marks_it(world: World) -> None:
    deliver(world, day_packet("2026-10-20", "我：PO-7731 那单让郜立人跟。"), day_packet("2026-11-13", *ASKED))
    write(world, GAO_RULES).close()
    (review,) = reviews(world)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0
    target = one(world, "郜立人", "负责", "PO-7731")
    assert (target["state"], target["valid_to"]) == ("ended", None)
    assert target["correction_sentence"] == sid("2026-11-13", 2)
    write(world, GAO_RULES).close()  # 再写一遍账（没有新包）：照定的，不再问
    assert [row["decision"] for row in reviews(world)] == ["keep_new"]


def test_an_unmatched_negation_with_two_candidates_does_nothing(world: World) -> None:
    deliver(world, day_packet("2026-10-20", "我：PO-7731 那单让郜立人跟。", "我：PO-8102 那单也让郜立人跟。"),
            day_packet("2026-11-13", "我：记错了，郜立人不负责 PO-7731，也不负责 PO-8102 的唛头。"))
    write(world, GAO_RULES).close()
    assert reviews(world) == []


def test_an_unmatched_negation_with_soft_words_is_not_asked(world: World) -> None:
    """软变化优先：带犹豫词的（“等客户定了再说”）不当纠正问。"""
    deliver(world, day_packet("2026-10-20", "我：PO-7731 那单让郜立人跟。"),
            day_packet("2026-11-13", ASKED[0], "我：不对，郜立人先不负责这个单子了，等客户定了再说。"))
    write(world, GAO_RULES).close()
    assert reviews(world) == [] and current(one(world, "郜立人", "负责", "PO-7731"))


@pytest.mark.parametrize(
    ("text", "negated"),
    [
        ("记错了，外贸部早就不归示例贸易华东分公司管了。", True),
        ("不对，外贸部不是示例贸易下面的。", True),
        ("搞错了，华东分公司已经不属于示例贸易旗下。", True),
        ("不对，那家不是示例贸易的子公司。", True),
        ("不对，那家不是示例贸易的客户。", False),  # 只说“不是 X 的”不认：客户是也会对上
        ("记错了，两个人部门不一样。", False),
    ],
)
def test_part_of_has_its_own_negation_wording(text: str, negated: bool) -> None:
    """D210：抽取没抽出来的纠正也认隶属于：不是、不属于、不归……旗下、下面、下属、子公司、分公司、部门，或者不归……管。"""
    assert ("隶属于" in correct.negated_types(text)) is negated
    assert correct.WORDS_VERSION == "correct-words-v5"


def test_an_unmatched_negation_of_a_part_of_asks_the_owner(world: World) -> None:
    """主人纠正一个部门归哪家、L3 没抽出来：对得上唯一一条现行的隶属于，进待确认问主人（和负责、任职于一样）。"""
    dept = {"key": "e34", "mention": "外贸部", "name": "外贸部", "type": "公司或机构"}
    branch = {"key": "e35", "mention": "示例贸易华东分公司", "name": "示例贸易华东分公司", "type": "公司或机构"}
    extract = rules(("外贸部归示例贸易华东分公司", dept, "隶属于", branch, {}))
    deliver(world, day_packet("2026-10-20", "我：外贸部归示例贸易华东分公司。"),
            day_packet("2026-11-13", "desk-bot：外贸部的报表发给示例贸易华东分公司吗？",
                       "我：记错了，外贸部早就不归示例贸易华东分公司管了。"))
    write(world, extract).close()
    target = one(world, "外贸部", "隶属于", "示例贸易华东分公司")
    assert current(target)
    (review,) = reviews(world)
    claims = review["claims"]
    assert (review["kind"], claims["reason"], claims["relation"], claims["sentence"]) == (
        "manual_review", "correction_unmatched", target["relation_id"], sid("2026-11-13", 2))


def test_withdrawing_an_unmatched_correction_drops_its_review(world: World) -> None:
    deliver(world, day_packet("2026-10-20", "我：PO-7731 那单让郜立人跟。"), day_packet("2026-11-13", *ASKED))
    write(world, GAO_RULES).close()
    assert main(["withdraw", "--library", "prod", "--sentence", sid("2026-11-13", 2)]) == 0
    assert reviews(world) == []


# ---------- 第一轮审阅（例子照审阅的复现脚本改的，都是编的） ----------


def test_a_dated_correction_earlier_than_the_old_start_replaces_it(world: World) -> None:
    """审阅第 1 条：原话写了哪天的照原话（第 1 题 A）；比旧那段的开始（L3 照当场交代写的说话那天）还早，也是换掉，不是补历史。"""
    extract = rules(
        ("交给宓春生负责", MI, "负责", TEA, {"valid_from": "2026-11-01", "time_precision": "day"}),
        ("就交给阚星野", KAN, "负责", TEA, {"valid_from": "2026-10-01", "time_precision": "day"}),
    )
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-10", "我：你记错了，锡兰那批茶具订单 10 月 1 日起就交给阚星野了。"))
    write(world, extract).close()
    old, new = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert current(new) and new["valid_from"] == "2026-10-01"
    assert (old["state"], old["valid_to"], old["superseded_by"]) == ("ended", None, new["relation_id"])  # 结束不早于开始：空着
    assert old["correction_sentence"] == sid("2026-11-10", 1)


def test_a_dated_ending_earlier_than_the_old_start_ends_it(world: World) -> None:
    extract = rules(
        ("在甘棠物流做", QI, "任职于", GANTANG, {"valid_from": "2026-10-05", "time_precision": "day"}),
        ("9 月就不在甘棠物流", QI, "任职于", GANTANG, {"state": "已结束", "valid_to": "2026-09", "time_precision": "month"}),
    )
    deliver(world, day_packet("2026-10-05", "我：祁望舒在甘棠物流做运营经理。"),
            day_packet("2026-11-12", "我：你记错了，祁望舒 9 月就不在甘棠物流了。"))
    write(world, extract).close()
    (row,) = rel_rows(world, "祁望舒", "任职于", "甘棠物流")  # 不另起一段“旧事”
    assert (row["state"], row["valid_to"], row["correction_sentence"]) == ("ended", None, sid("2026-11-12", 1))


@pytest.mark.parametrize("new_first", [True, False])
def test_a_day_written_for_the_old_end_is_kept_in_either_order(world: World, new_first: bool) -> None:
    """审阅第 4 条：同一句里“旧的哪天不管了 + 新的”，L3 哪个列在前结果都一样：旧的照原话那天结束、指着新的。"""
    new = ("现在是阚星野负责", KAN, "负责", TEA, {})
    old = ("现在是阚星野负责", MI, "负责", TEA, {"state": "已结束", "valid_to": "2026-10", "time_precision": "month"})
    extract = rules(("交给宓春生负责", MI, "负责", TEA, {"valid_from": "2026-09-01", "time_precision": "day"}),
                    *((new, old) if new_first else (old, new)))
    deliver(world, day_packet("2026-09-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，宓春生 10 月就不管了，锡兰那批茶具订单现在是阚星野负责。"))
    write(world, extract).close()
    mi, kan = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert current(kan)
    assert (mi["state"], mi["valid_to"], mi["superseded_by"]) == ("ended", "2026-10", kan["relation_id"])
    assert mi["correction_sentence"] == sid("2026-11-12", 1)


def test_kinship_correction_when_both_kinds_were_current(world: World) -> None:
    """审阅第 3 条：两种原来都现行，主人纠正成其中一种，另一种结束、记着被纠正。"""
    deliver(world, day_packet("2026-09-15", "我：晏青禾是我妹妹，下个月来上海。", domain="[personal]"),
            day_packet("2026-10-01", "我：晏青禾是我表妹。", domain="[personal]"),
            day_packet("2026-11-09", "我：记错了，晏青禾是我表妹，不是亲妹妹。", domain="[personal]"))
    write(world, KIN_RULES).close()
    rows = kin_rows(world)
    assert current(rows["表妹"])
    assert (rows["妹妹"]["state"], rows["妹妹"]["correction_sentence"]) == ("ended", sid("2026-11-09", 1))


def test_a_vague_third_kinship_is_not_held_because_of_another_correction(world: World) -> None:
    """审阅第 14 条：这一对有过纠正，别的亲属说法照亲属原来的规矩（各算各的），软变化的候选不因此挡住。"""
    extract = rules(*KIN_SPECS, ("晏青禾可能是我堂妹", YAN, "亲属", OWNER, {"kinship": "堂妹"}))
    deliver(world, day_packet("2026-09-15", "我：晏青禾是我妹妹，下个月来上海。", domain="[personal]"),
            day_packet("2026-11-09", "我：记错了，晏青禾是我表妹，不是亲妹妹。", domain="[personal]"),
            day_packet("2026-11-10", "我：晏青禾可能是我堂妹，回头问问我妈。", domain="[personal]"))
    write(world, extract).close()
    assert kin_rows(world)["堂妹"]["status"] != "pending"


def unmatched_kept(world: World, *later: str) -> None:
    deliver(world, day_packet("2026-10-20", "我：PO-7731 那单让郜立人跟。"), day_packet("2026-11-13", *(later or ASKED)))
    write(world, GAO_RULES).close()
    (review,) = reviews(world)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0
    assert one(world, "郜立人", "负责", "PO-7731")["state"] == "ended"


def test_withdrawing_the_whole_packet_of_a_kept_unmatched_correction(world: World) -> None:
    """审阅第 2 条：整包撤回也是撤回那一句：旧的回来、记号没了。"""
    unmatched_kept(world)
    assert main(["withdraw", "--library", "prod", "--packet", "2026-11-13_2330_desk-bot"]) == 0
    target = one(world, "郜立人", "负责", "PO-7731")
    assert current(target) and target["corrected_at"] is None


def test_resending_the_packet_without_the_unmatched_correction(world: World) -> None:
    unmatched_kept(world)
    deliver(world, resent("2026-11-13", "desk-bot：PO-7731 的出货单要不要先发给郜立人看？", "我：好的，先发给他。"))
    write(world, GAO_RULES).close()
    target = one(world, "郜立人", "负责", "PO-7731")
    assert current(target) and target["corrected_at"] is None


def test_resending_the_packet_with_the_same_unmatched_correction_keeps_it(world: World) -> None:
    unmatched_kept(world)
    deliver(world, resent("2026-11-13", *ASKED))
    write(world, GAO_RULES).close()
    target = one(world, "郜立人", "负责", "PO-7731")
    assert target["state"] == "ended" and target["correction_sentence"] == sid("2026-11-13", 2)


def test_a_kept_unmatched_correction_that_l3_later_extracts(world: World) -> None:
    """审阅第 5 条：重抽抽出来了的，不再另加判出来的那一处：同一条关系只有一段。"""
    unmatched_kept(world, "我：记错了，郜立人不负责 PO-7731 这单。")
    requeue_packets(world.settings, "prod", ["2026-11-13_2330_desk-bot"])
    write(world, rules(("那单让郜立人跟", GAO, "负责", PO, {}), ("郜立人不负责 PO-7731", GAO, "负责", PO, {"state": "已结束"}))).close()
    (row,) = rel_rows(world, "郜立人", "负责", "PO-7731")
    assert row["state"] == "ended" and row["correction_sentence"] == sid("2026-11-13", 1)


def test_a_finance_correction_is_held_even_when_another_relation_in_the_group_is_frozen(world: World) -> None:
    """审阅第 7 条：纠正要改的是没冻结的那一条，照挡（D7）；碰到冻结的那一条才只进“和冻结的冲突”。"""
    extract = rules(
        ("K7 票据放在澄江银行", K7, "托管于", CHENG, {}),
        ("也转到北屿证券", K7, "托管于", BEIYU, {}),
        ("不在北屿证券", K7, "托管于", BEIYU, {"state": "已结束"}),
    )
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"))
    write(world, extract).close()
    freeze(world.settings, "prod", [one(world, "K7 票据", "托管于", "澄江银行")["relation_id"]])
    deliver(world, day_packet("2026-11-01", "我：K7 票据有一部分也转到北屿证券了。", domain="[investment]"))
    write(world, extract).close()
    (conflict,) = reviews(world)
    assert main(["review", "--library", "prod", "--decide", str(conflict["review_id"]), "--keep", "both"]) == 0
    deliver(world, day_packet("2026-11-11", "我：不对，K7 票据早就不在北屿证券了。", domain="[investment]"))
    write(world, extract).close()
    found = [(row["kind"], row["claims"].get("reason"), row["decision"]) for row in reviews(world)]
    assert ("manual_review", "correction", None) in found
    assert any(current(row) for row in rel_rows(world, "K7 票据", "托管于", "北屿证券"))


def test_two_short_corrections_in_one_packet_are_asked_separately(world: World) -> None:
    """审阅第 13 条：纠正词那一句是“不对。”这种短句时，两处纠正不认成同一条待确认。"""
    dongan = {"key": "e40", "mention": "东岸证券", "name": "东岸证券", "type": "公司或机构"}
    extract = rules(("K7 票据放在澄江银行", K7, "托管于", CHENG, {}), ("转到北屿证券", K7, "托管于", BEIYU, {}),
                    ("转到东岸证券", K7, "托管于", dongan, {}))
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
            day_packet("2026-11-11", "我：不对。", "我：K7 票据转到北屿证券了。", "desk-bot：好的，记下了。",
                       "我：不对。", "我：K7 票据转到东岸证券了。", domain="[investment]"))
    write(world, extract).close()
    assert len([row for row in reviews(world) if row["claims"].get("reason") == "correction"]) == 2


def test_an_echo_judge_c_was_unsure_about_is_quarantined_once(world: World) -> None:
    """审阅第 11 条：R05 不记的那句判断 C 也拿不准，隔离表里只进 R05 那一行。"""

    def unsure_bot(items):  # type: ignore[no-untyped-def]
        out = []
        for item in items:
            if item.check is None:
                out.append(item)
                continue
            relations = [replace(one, failures=[*one.failures, Finding("C", "llm_unsure")], judge={"by": "llm", "verdict": "unsure"})
                         if one.passed and one.raw.get("speaker") == "desk-bot" else one for one in item.check.relations]
            out.append(replace(item, check=replace(item.check, relations=relations)))
        return out

    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "desk-bot：锡兰那批茶具订单是宓春生在跟。", "我：不对，是阚星野负责。"))
    write(world, ECHO_RULES, order=unsure_bot).close()
    assert [row["reason"] for row in world.rows("SELECT reason FROM quarantine WHERE item_kind = 'relation'")] == [
        "L5:corrected_on_spot"]


@pytest.mark.parametrize("text", ["不是的。", "不是的"])
def test_bu_shi_de_alone_is_a_correction_word(text: str) -> None:
    """审阅第 9 条：做法 1.2 第 2 条写了“不是的”。"""
    assert correct.is_cue(text)


def test_bu_shi_de_just_before_is_a_correction(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不是的。", "我：锡兰那批茶具订单是阚星野负责的。"))
    write(world, TEA_RULES).close()
    old = one(world, *MI_TEA)
    assert old["valid_to"] is None and old["correction_sentence"] == sid("2026-11-12", 1)


@pytest.mark.parametrize("before", ["我：可能不对。", "我：不对吧，我再想想。"])
def test_a_hedged_cue_before_is_not_a_correction(world: World, before: str) -> None:
    """审阅第 9 条：犹豫词在纠正词那一句里（“可能不对”），拿不准的不替主人改。"""
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", before, "我：锡兰那批茶具订单是阚星野负责的。"))
    write(world, TEA_RULES).close()
    assert one(world, *MI_TEA)["corrected_at"] is None


@pytest.mark.parametrize("text", [
    "这批报价不对外，锡兰那批茶具订单是阚星野负责的。",
    "这单并不急，锡兰那批茶具订单是阚星野负责的。",
    "He is not only on PO-7731 but also on PO-8102.",
])
def test_ordinary_words_are_not_corrections(text: str) -> None:
    """审阅第 10 条：“不对外”“并不急”“not only … but also”不是在说记的不对（做法的词表里也没有“并不”）。"""
    assert not correct.is_cue(text)


def test_a_judge_c_decision_still_replaces_the_rebuilt_open_one(world: World, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 12 条：判断 C 拿不准的（J4.4），重建时原因变了（llm_failed → llm_unsure），导回照旧盖过新账本里没定的那条。"""
    packet = day_packet("2026-11-01", "我：X1 票据在海港银行。")
    deliver(world, packet)
    write(world, bank_extract, order=judged("llm_failed")).close()
    (review,) = reviews(world)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "hidden"]) == 0
    exported = export_decisions(world.settings, "prod")
    new = rebuilt_world(tmp_path, monkeypatch)
    deliver(new, packet)
    write(new, bank_extract, order=judged("llm_unsure")).close()
    import_decisions(new.settings, "prod", exported)
    assert [row["decision"] for row in reviews(new)] == ["keep_hidden"]


def test_spellings_are_looked_up_once_per_entity(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 8 条：找“抽取没抽出来的纠正”对的是哪一条时，同一个实体的写法只查一次。"""

    def orders(window):  # type: ignore[no-untyped-def]
        found, entities = [], {"e30": GAO}
        for line in window.targets:
            for number in range(30):
                if f"PO-{7700 + number} 那单让郜立人跟" in line.text:
                    order = {"key": f"po{number}", "mention": f"PO-{7700 + number}", "name": f"PO-{7700 + number}",
                             "type": "订单或合同"}
                    entities[order["key"]] = order
                    found.append(relation(GAO, "负责", order, (line.ref, line.text.rstrip("。"))))
        return {"entities": list(entities.values()), "facts": [], "relations": found}

    deliver(world, day_packet("2026-10-20", *[f"我：PO-{7700 + number} 那单让郜立人跟。" for number in range(30)]))
    write(world, orders).close()
    calls: list[str] = []
    original = LedgerWriter._spellings

    def counted(self, entity_id, scope):  # type: ignore[no-untyped-def]  # noqa: ANN001
        calls.append(entity_id)
        return original(self, entity_id, scope)

    monkeypatch.setattr(LedgerWriter, "_spellings", counted)
    deliver(world, day_packet("2026-11-13", "我：记错了，郜立人不负责 PO-7731 这单。"))
    write(world, orders).close()
    assert calls and len(calls) == len(set(calls))


# ---------- 第二轮审阅（例子照审阅的复现脚本改的，都是编的） ----------


@pytest.mark.parametrize("kan_first", [True, False])
def test_a_correction_naming_two_people_in_one_sentence_is_asked(world: World, kan_first: bool) -> None:
    """第二轮第 1 条：“不对，是阚星野负责，宓春生也在帮忙”，L3 两个都抽成生效：同一句话里两个照 R12 都问，
    和 L3 列的先后无关；纠正并进宓春生那一段时，不把同一句里的阚星野当成以前的结束掉。"""
    kan = ("宓春生也在帮忙", KAN, "负责", TEA, {})
    mi = ("宓春生也在帮忙", MI, "负责", TEA, {})
    extract = rules(("交给宓春生负责", MI, "负责", TEA, {}), *((kan, mi) if kan_first else (mi, kan)))
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，锡兰那批茶具订单是阚星野负责，宓春生也在帮忙。"))
    write(world, extract).close()
    kan_rows = rel_rows(world, *KAN_TEA)
    assert [(row["state"], row["status"], row["pending_reason"]) for row in kan_rows] == [("active", "pending", "other")]
    assert ("other", "same_time") in [(row["kind"], row["claims"].get("reason")) for row in reviews(world)]
    assert all(row["corrected_at"] is None for row in rel_rows(world, *MI_TEA) + kan_rows)


def test_a_correction_about_an_earlier_stint_does_not_end_the_current_one(world: World) -> None:
    """第二轮第 2 条：纠正写的结束日子落在这条关系更早、已经结束的那一段里：改那一段，现在这一段照旧。"""
    extract = rules(
        ("在甘棠物流做运营", QI, "任职于", GANTANG, {"valid_from": "2025-03-01", "time_precision": "day"}),
        ("从甘棠物流离职", QI, "任职于", GANTANG, {"state": "已结束", "valid_to": "2025-08-01", "time_precision": "day"}),
        ("又回甘棠物流", QI, "任职于", GANTANG, {"valid_from": "2026-10-05", "time_precision": "day"}),
        ("去年 6 月就从甘棠物流走", QI, "任职于", GANTANG, {"state": "已结束", "valid_to": "2025-06", "time_precision": "month"}),
    )
    deliver(world, day_packet("2025-03-01", "我：祁望舒在甘棠物流做运营。"),
            day_packet("2025-08-01", "我：祁望舒从甘棠物流离职了。"),
            day_packet("2026-10-05", "我：祁望舒又回甘棠物流了。"),
            day_packet("2026-11-12", "我：你记错了，祁望舒那次是去年 6 月就从甘棠物流走了。"))
    write(world, extract).close()
    rows = {row["valid_from"]: row for row in rel_rows(world, "祁望舒", "任职于", "甘棠物流")}
    assert set(rows) == {"2025-03-01", "2026-10-05"}
    assert current(rows["2026-10-05"]) and rows["2026-10-05"]["corrected_at"] is None
    assert (rows["2025-03-01"]["state"], rows["2025-03-01"]["valid_to"]) == ("ended", "2025-06")
    assert rows["2025-03-01"]["correction_sentence"] == sid("2026-11-12", 1)


FINANCE_TWO = rules(
    ("K7 票据放在澄江银行", K7, "托管于", CHENG, {}),
    ("早就不在澄江银行", K7, "托管于", CHENG, {"state": "已结束"}),
    ("现在在北屿证券", K7, "托管于", BEIYU, {}),
)


def test_one_finance_correction_message_in_two_sentences_is_asked_once(world: World) -> None:
    """第二轮第 3 条：一条消息里两句的纠正（纠正词在前一句），还是一句话、一组只问一次；主人照改，两处一起放出来。"""
    deliver(world, day_packet("2026-10-09", "我：K7 票据放在澄江银行。", domain="[investment]"),
            day_packet("2026-11-11", "我：不对，K7 票据早就不在澄江银行了。K7 票据现在在北屿证券。", domain="[investment]"))
    write(world, FINANCE_TWO).close()
    (review,) = [row for row in reviews(world) if row["claims"].get("reason") == "correction"]
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0
    assert current(one(world, "K7 票据", "托管于", "北屿证券"))
    assert not any(current(row) for row in rel_rows(world, "K7 票据", "托管于", "澄江银行"))
    assert [row["decision"] for row in reviews(world)] == ["keep_new"]


@pytest.mark.parametrize("new_first", [True, False])
def test_old_end_and_new_in_two_sentences_of_one_message(world: World, new_first: bool) -> None:
    """第二轮第 7 条：旧的哪天不管了、新的是谁分在同一条消息的两句里：旧的照原话那天结束、指着新的。"""
    new = ("现在是阚星野负责", KAN, "负责", TEA, {})
    old = ("宓春生 10 月就不管了", MI, "负责", TEA, {"state": "已结束", "valid_to": "2026-10", "time_precision": "month"})
    extract = rules(("交给宓春生负责", MI, "负责", TEA, {"valid_from": "2026-09-01", "time_precision": "day"}),
                    *((new, old) if new_first else (old, new)))
    deliver(world, day_packet("2026-09-01", "我：锡兰那批茶具订单交给宓春生负责。"),
            day_packet("2026-11-12", "我：不对，锡兰那批茶具订单宓春生 10 月就不管了。锡兰那批茶具订单现在是阚星野负责。"))
    write(world, extract).close()
    mi, kan = one(world, *MI_TEA), one(world, *KAN_TEA)
    assert current(kan)
    assert (mi["state"], mi["valid_to"], mi["superseded_by"]) == ("ended", "2026-10", kan["relation_id"])


def test_a_correction_back_to_the_frozen_one_leaves_the_conflict_consistent(world: World) -> None:
    """第二轮第 6 条：冻结的宓春生和后说的阚星野在“和冻结的冲突”里，主人又说“不对，还是宓春生”：不把阚星野结束成
    “已结束又待确认”的样子，冲突照旧等主人定。"""
    extract = rules(("交给宓春生负责", MI, "负责", TEA, {}), ("改由阚星野负责", KAN, "负责", TEA, {}),
                    ("还是宓春生负责", MI, "负责", TEA, {}))
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单交给宓春生负责。"))
    write(world, extract).close()
    freeze(world.settings, "prod", [one(world, *MI_TEA)["relation_id"]])
    deliver(world, day_packet("2026-11-05", "我：锡兰那批茶具订单改由阚星野负责。"))
    write(world, extract).close()
    deliver(world, day_packet("2026-11-12", "我：不对，锡兰那批茶具订单还是宓春生负责。"))
    write(world, extract).close()
    kan = one(world, *KAN_TEA)
    assert (kan["state"], kan["status"], kan["pending_reason"]) == ("active", "pending", "frozen_conflict")
    assert [(row["kind"], row["decision"]) for row in reviews(world)] == [("frozen_conflict", None)]


def test_a_correction_that_settles_a_same_moment_pair_closes_its_review(world: World) -> None:
    """第二轮第 5 条：同一句话里两个（R12）以后，主人纠正成其中一个：那条“A 或 B”不再问。"""
    extract = rules(("宓春生和阚星野在跟", MI, "负责", TEA, {}), ("宓春生和阚星野在跟", KAN, "负责", TEA, {}),
                    ("是阚星野负责", KAN, "负责", TEA, {}))
    deliver(world, day_packet("2026-11-01", "我：锡兰那批茶具订单宓春生和阚星野在跟。"))
    write(world, extract).close()
    assert [row["claims"].get("reason") for row in reviews(world)] == ["same_time"]
    deliver(world, day_packet("2026-11-12", "我：不对，锡兰那批茶具订单是阚星野负责的。"))
    write(world, extract).close()
    assert current(one(world, *KAN_TEA)) and one(world, *MI_TEA)["state"] == "ended"
    assert reviews(world) == []


@pytest.mark.parametrize("text", ["不是。", "不是"])
def test_a_bare_no_is_not_a_correction_word(text: str) -> None:
    """第二轮拿不准的第 1 条：单独一句“不是”多半是在答 Bot 的是不是，做法写的是“不是的”。"""
    assert not correct.is_cue(text)


def test_spellings_stop_at_the_first_end_not_mentioned(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """第二轮第 9 条：候选关系的一头没被提到就不再查另一头的写法。"""

    def orders(window):  # type: ignore[no-untyped-def]
        found, entities = [], {}
        for line in window.targets:
            for number in range(30):
                if f"单子{number:02d}让员工{number:02d}号跟" in line.text:
                    person = {"key": f"p{number}", "mention": f"员工{number:02d}号", "name": f"员工{number:02d}号", "type": "人"}
                    order = {"key": f"o{number}", "mention": f"单子{number:02d}", "name": f"单子{number:02d}", "type": "订单或合同"}
                    entities[person["key"]], entities[order["key"]] = person, order
                    found.append(relation(person, "负责", order, (line.ref, line.text.rstrip("。"))))
            if "PO-7731 那单让郜立人跟" in line.text:
                entities["e30"], entities["e31"] = GAO, PO
                found.append(relation(GAO, "负责", PO, (line.ref, line.text.rstrip("。"))))
        return {"entities": list(entities.values()), "facts": [], "relations": found}

    deliver(world, day_packet("2026-10-20", "我：PO-7731 那单让郜立人跟。", *[f"我：单子{n:02d}让员工{n:02d}号跟。" for n in range(30)]))
    write(world, orders).close()
    calls: list[str] = []
    original = LedgerWriter._spellings

    def counted(self, entity_id, scope):  # type: ignore[no-untyped-def]  # noqa: ANN001
        calls.append(entity_id)
        return original(self, entity_id, scope)

    monkeypatch.setattr(LedgerWriter, "_spellings", counted)
    deliver(world, day_packet("2026-11-13", "我：记错了，郜立人不负责 PO-7731 这单。"))
    write(world, orders).close()
    assert len(calls) == len(set(calls)) <= 32  # 30 个没提到的主体各一次，加上郜立人、PO-7731


# ---------- 只说了新的（主人 11-17 复核测试题 K12 定的） ----------

LANCHUAN = {"key": "e41", "mention": "澜川酒店", "name": "澜川酒店", "type": "公司或机构"}
JOB_RULES = rules(
    ("祁望舒在甘棠物流上班", QI, "任职于", GANTANG, {}),
    ("在澜川酒店做销售", QI, "任职于", LANCHUAN, {}),
    ("不在甘棠物流", QI, "任职于", GANTANG, {"state": "已结束"}),
)


def new_job(world: World, said: str = "我：其实祁望舒在澜川酒店做销售。") -> None:
    deliver(world, day_packet("2026-10-06", "我：祁望舒在甘棠物流上班。"), day_packet("2026-11-11", said))
    write(world, JOB_RULES).close()


def test_a_new_job_said_with_a_cue_asks_about_the_old_one(world: World) -> None:
    """可以同时有几条的关系（任职于），主人带着纠正词（“其实”）只说了新的、没说旧的不对：新的照记，旧的不动、不记纠正，
    另进待确认问一句旧的还算不算（原因 correction_other）。"""
    new_job(world)
    old, new = one(world, "祁望舒", "任职于", "甘棠物流"), one(world, "祁望舒", "任职于", "澜川酒店")
    assert current(new) and current(old) and old["corrected_at"] is None
    (review,) = reviews(world)
    claims = review["claims"]
    assert (review["kind"], claims["reason"], claims["relation"], claims["sentence"]) == (
        "manual_review", "correction_other", old["relation_id"], sid("2026-11-11", 1))
    conn = open_ledger(world.settings.library("prod"))
    try:
        (item,) = open_items(conn, world.settings)
    finally:
        conn.close()
    assert (item.kind, item.choices) == ("correction_other", ["new", "hidden"])


def test_the_owner_saying_the_old_job_is_over_ends_it(world: World) -> None:
    new_job(world)
    (review,) = reviews(world)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0
    old = one(world, "祁望舒", "任职于", "甘棠物流")
    assert (old["state"], old["valid_to"], old["correction_sentence"]) == ("ended", None, sid("2026-11-11", 1))
    assert current(one(world, "祁望舒", "任职于", "澜川酒店"))


def test_the_owner_keeping_the_old_job_changes_nothing(world: World) -> None:
    new_job(world)
    (review,) = reviews(world)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "hidden"]) == 0
    assert current(one(world, "祁望舒", "任职于", "甘棠物流")) and [row["decision"] for row in reviews(world)] == ["keep_hidden"]


def test_an_old_job_question_left_30_days_keeps_the_old_one(world: World) -> None:
    """照第 3 题 B：30 天没人管，接着放着、不改。"""
    new_job(world)
    world.execute("UPDATE review_queue SET created_at = '2026-01-01T00:00:00+08:00'")
    remind(world.settings, "prod", force=True)
    assert current(one(world, "祁望舒", "任职于", "甘棠物流"))
    assert [(row["decision"], row["decided_by"]) for row in reviews(world)] == [("keep_hidden", "auto")]


@pytest.mark.parametrize("said", [
    "我：祁望舒在澜川酒店做销售。",  # 没有纠正词：多了一家，普通的新说法
    "我：其实祁望舒早就不在甘棠物流了，现在在澜川酒店做销售。",  # 说了旧的不在了：照结束，不用问
    "我：其实祁望舒可能在澜川酒店做销售。",  # 带犹豫词：软变化优先
])
def test_no_question_about_the_old_job(world: World, said: str) -> None:
    new_job(world, said)
    assert [row["claims"].get("reason") for row in reviews(world) if row["claims"].get("reason") == "correction_other"] == []


def test_withdrawing_the_new_job_sentence_drops_the_question(world: World) -> None:
    new_job(world)
    assert main(["withdraw", "--library", "prod", "--sentence", sid("2026-11-11", 1)]) == 0
    assert reviews(world) == [] and current(one(world, "祁望舒", "任职于", "甘棠物流"))


THREE_JOBS = rules(
    ("祁望舒在甘棠物流上班", QI, "任职于", GANTANG, {}),
    ("祁望舒在东岸证券兼职", QI, "任职于", BROKER, {}),
    ("在澜川酒店做销售", QI, "任职于", LANCHUAN, {}),
    ("不在甘棠物流", QI, "任职于", GANTANG, {"state": "已结束"}),
)


def other_reviews(world: World, *, open_only: bool = False) -> list[dict]:
    return [row for row in reviews(world) if row["claims"].get("reason") == "correction_other"
            and (not open_only or row["decided_at"] is None)]


def about(world: World, row: dict) -> str:
    (found,) = world.rows("SELECT e.canonical_name AS name FROM relations AS r JOIN entities AS e"
                          " ON e.entity_id = r.object_entity_id WHERE r.relation_id = ?", row["claims"]["relation"])
    return str(found["name"])


def test_an_answer_to_a_review_does_not_open_new_questions(world: World) -> None:
    """审阅第 1 条：答待确认的那一句（“改吧，其实她现在就在澜川酒店做销售”）整句不再问，别的那几条也不问。"""
    deliver(world, day_packet("2026-10-05", "我：祁望舒在甘棠物流上班。"), day_packet("2026-10-06", "我：祁望舒在东岸证券兼职。"),
            day_packet("2026-11-11", "我：其实祁望舒在澜川酒店做销售。"))
    write(world, THREE_JOBS).close()
    assert sorted(about(world, row) for row in other_reviews(world)) == ["东岸证券", "甘棠物流"]
    (gantang,) = [row for row in other_reviews(world) if about(world, row) == "甘棠物流"]
    deliver(world, day_packet("2026-11-12", f"desk-bot：祁望舒甘棠物流那边那条要不要结束？（待确认 {gantang['code']}）",
                              "我：改吧，其实她现在就在澜川酒店做销售。"))
    write(world, THREE_JOBS).close()
    assert sorted((about(world, row), row["decision"]) for row in other_reviews(world)) == [
        ("东岸证券", None), ("甘棠物流", "keep_new")]


def test_restating_a_job_already_recorded_is_not_a_new_value(world: World) -> None:
    """审阅第 2 条：说的是早就记着的那一家（“其实她在东岸证券兼职很久了”），不是新的一条，不问别的。"""
    deliver(world, day_packet("2026-10-05", "我：祁望舒在甘棠物流上班。"), day_packet("2026-10-06", "我：祁望舒在东岸证券兼职。"))
    write(world, THREE_JOBS).close()
    deliver(world, day_packet("2026-11-11", "我：其实祁望舒在东岸证券兼职很久了。"))
    write(world, THREE_JOBS).close()
    assert other_reviews(world) == []


def test_ending_the_old_job_survives_a_resend_that_moves_the_sentence(world: World) -> None:
    """审阅第 3 条：主人说了不算了，包重发、那一句挪了位置：照它结束，旧的不再现行。"""
    new_job(world)
    (review,) = reviews(world)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0
    deliver(world, resent("2026-11-11", "desk-bot：早。", "我：其实祁望舒在澜川酒店做销售。"))
    write(world, JOB_RULES).close()
    old = one(world, "祁望舒", "任职于", "甘棠物流")
    assert not current(old) and old["correction_sentence"] == "2026-11-11_2330_desk-bot#1.2.1"


def test_no_second_question_after_the_owner_said_the_old_one_still_holds(world: World) -> None:
    """审阅第 5 条：主人说了旧的那一条还算，以后又带着“其实”说了另一家：那一条不再问一遍，只问这回多出来的旧的那一条。"""
    extract = rules(("祁望舒在甘棠物流上班", QI, "任职于", GANTANG, {}), ("在澜川酒店做销售", QI, "任职于", LANCHUAN, {}),
                    ("在东岸证券上班", QI, "任职于", BROKER, {}))
    deliver(world, day_packet("2026-10-06", "我：祁望舒在甘棠物流上班。"), day_packet("2026-11-11", "我：其实祁望舒在澜川酒店做销售。"))
    write(world, extract).close()
    (first,) = other_reviews(world)
    assert main(["review", "--library", "prod", "--decide", str(first["review_id"]), "--keep", "hidden"]) == 0
    deliver(world, day_packet("2026-11-20", "我：其实祁望舒在东岸证券上班。"))
    write(world, extract).close()
    assert [about(world, row) for row in other_reviews(world, open_only=True)] == ["澜川酒店"]


def test_one_question_about_the_same_old_job(world: World) -> None:
    """审阅第 6 条：上一条刚问过“是不是说它”（抽取没抽出来的纠正）的那一条，不再问“还算不算”。"""
    extract = rules(("祁望舒在甘棠物流上班", QI, "任职于", GANTANG, {}), ("在澜川酒店做销售", QI, "任职于", LANCHUAN, {}))
    deliver(world, day_packet("2026-10-06", "我：祁望舒在甘棠物流上班。"),
            day_packet("2026-11-11", "我：记错了，祁望舒不在甘棠物流了。", "我：其实她在澜川酒店做销售。"))
    write(world, extract).close()
    old = one(world, "祁望舒", "任职于", "甘棠物流")["relation_id"]
    assert [row["claims"]["reason"] for row in reviews(world) if row["claims"].get("relation") == old] == ["correction_unmatched"]


def test_also_means_the_old_one_stays(world: World) -> None:
    """审阅拿不准的：“其实她也在澜川酒店做销售”的“也”说的就是旧的还在，不问。"""
    new_job(world, "我：其实祁望舒也在澜川酒店做销售。")
    assert other_reviews(world) == []


def test_a_new_job_that_is_itself_waiting_does_not_ask(world: World) -> None:
    """审阅拿不准的：新的那一条自己还在待确认（判断 C 拿不准），先不问旧的。"""
    deliver(world, day_packet("2026-10-06", "我：祁望舒在甘棠物流上班。"))
    write(world, JOB_RULES).close()
    deliver(world, day_packet("2026-11-11", "我：其实祁望舒在澜川酒店做销售。"))
    write(world, JOB_RULES, order=judged("llm_unsure")).close()
    assert other_reviews(world) == []


def open_about(world: World) -> list[str]:
    return sorted(about(world, row) for row in other_reviews(world, open_only=True))


@pytest.mark.parametrize(("earlier", "phrase", "fields"), [
    ("我：祁望舒以前在澜川酒店做过。", "以前在澜川酒店做过", {"state": "已结束"}),  # 早就结束了的那一段
    ("我：祁望舒可能要去澜川酒店做销售。", "可能要去澜川酒店做销售", {}),  # 说得不干脆的
    ("鞠望川：祁望舒在澜川酒店当销售。", "在澜川酒店当销售", {}),  # 同事先说的
])
def test_an_earlier_mention_that_was_not_the_owners_firm_word_still_asks(world: World, earlier: str, phrase: str,
                                                                          fields: dict) -> None:
    """复审第 1 条：以前那一处是已经结束的那一段、带犹豫词的、别人说的，都不算“主人说过这一家”：照问甘棠还算不算。"""
    extract = rules(("祁望舒在甘棠物流上班", QI, "任职于", GANTANG, {}), (phrase, QI, "任职于", LANCHUAN, fields),
                    ("其实祁望舒现在在澜川酒店做销售", QI, "任职于", LANCHUAN, {}))
    deliver(world, day_packet("2026-09-01", earlier), day_packet("2026-10-06", "我：祁望舒在甘棠物流上班。"),
            day_packet("2026-11-11", "我：其实祁望舒现在在澜川酒店做销售。"))
    write(world, extract).close()
    assert open_about(world) == ["甘棠物流"]


def test_an_unmatched_correction_does_not_ask_again_about_an_old_job_already_asked(world: World) -> None:
    """复审第 2 条：先问了甘棠还算不算，第二天主人说“记错了，祁望舒不在甘棠物流了”（抽取没抽出来）：同一条旧的不再挂第二条。"""
    extract = rules(("祁望舒在甘棠物流上班", QI, "任职于", GANTANG, {}), ("在澜川酒店做销售", QI, "任职于", LANCHUAN, {}))
    deliver(world, day_packet("2026-10-06", "我：祁望舒在甘棠物流上班。"), day_packet("2026-11-11", "我：其实祁望舒在澜川酒店做销售。"))
    write(world, extract).close()
    deliver(world, day_packet("2026-11-12", "我：记错了，祁望舒不在甘棠物流了。"))
    write(world, extract).close()
    old = one(world, "祁望舒", "任职于", "甘棠物流")["relation_id"]
    assert [row["claims"]["reason"] for row in reviews(world) if row["claims"].get("relation") == old] == ["correction_other"]


def test_a_resend_that_moves_and_rewords_the_sentence_asks_again(world: World) -> None:
    """包重发，说新的那一句挪了位置、又改了几个字：旧的那一问删掉，照新的那一句再问（要删的那一条不挡着）。"""
    new_job(world)
    deliver(world, resent("2026-11-11", "desk-bot：早。", "我：其实祁望舒现在在澜川酒店做销售。"))
    write(world, JOB_RULES).close()
    (review,) = other_reviews(world)
    assert review["claims"]["sentence"] == "2026-11-11_2330_desk-bot#1.2.1"


@pytest.mark.parametrize("text", [
    "其实她在兼程物流做销售。",  # 公司名里有“兼”
    "其实她在又一村餐厅做服务员。",  # 公司名里有“又”
    "其实她早就在澜川酒店做销售了，还升了主管。",  # “还”说的是升了主管
])
def test_names_and_other_things_are_not_the_old_one_still_holding(text: str) -> None:
    """复审第 6 条：“也、还、又、兼……”要和“在、是、做”连着说才算旧的也还在；公司名里的、说别的事的不算。"""
    assert correct.asks_about_others(text)


def test_customers_are_not_asked_about(world: World) -> None:
    """审阅拿不准的：是几家的客户很常见（“其实我在东岸证券也开了户”），客户是不问。"""
    extract = rules(("在海港银行开了户", OWNER, "客户是", BANK, {}), ("在东岸证券开了户", OWNER, "客户是", BROKER, {}))
    deliver(world, day_packet("2026-10-06", "我：我在海港银行开了户。"), day_packet("2026-11-11", "我：其实我在东岸证券开了户。"))
    write(world, extract).close()
    assert reviews(world) == []


def test_holdings_are_not_asked_about(world: World) -> None:
    """持有本来就是好几样：“其实我还持有……”不问别的那几样还算不算。"""
    n4 = {"key": "e42", "mention": "N4 票据", "name": "N4 票据", "type": "订单或合同"}
    extract = rules(("我持有 Z3 累购", OWNER, "持有", Z3, {}), ("还持有 N4 票据", OWNER, "持有", n4, {}))
    deliver(world, day_packet("2026-10-06", "我：我持有 Z3 累购。"), day_packet("2026-11-11", "我：其实我还持有 N4 票据。"))
    write(world, extract).close()
    assert reviews(world) == []


# ---------- 账本 ----------


def test_the_ledger_has_the_new_columns(world: World) -> None:
    conn = world.ledger()
    try:
        columns = {table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                   for table in ("relations", "relation_claims", "review_queue")}
    finally:
        conn.close()
    assert {"corrected_at", "correction_sentence"} <= columns["relations"]
    assert "correction" in columns["relation_claims"]
    assert {"code", "answer"} <= columns["review_queue"]
