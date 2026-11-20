"""J4.4 第二轮审阅（审 2ca3d01、9795bd0）查出来的那几种情况：冻结的和同一个位置重发、删掉冻结的以后同组重算、冻结的
后面又说了几个、同一句话里两个、待确认的编号跟着改、负责重新分组的先后、暂停那天的写法。例子都是编的。"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import World
from test_relations_newold import by_bank, moved_extract
from test_relations_write import bank_extract, resent
from test_relcheck import BANK, BROKER, NOTE, OWNER, ZHOU, relation
from test_write import day_packet, deliver, write

from javmem.cli import main
from javmem.config import Settings
from javmem.pipeline.merge import entity_id_of, name_key, relation_identity
from javmem.pipeline.normalize import Scope
from javmem.pipeline.verify import Proof
from javmem.pipeline.window import Window
from javmem.pipeline.write import pause_day
from javmem.review import freeze
from javmem.withdraw import withdraw

WEST = {"key": "e14", "mention": "西湾信托", "name": "西湾信托", "type": "公司或机构"}
BANKS = (BANK, BROKER, WEST)


def three_banks(window: Window) -> dict[str, object]:
    """X1 票据：一行里写着哪几家，就托管于哪几家（照写的先后）；写着“转走”的标已结束。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        if "X1 票据" not in text:
            continue
        named = sorted((bank for bank in BANKS if str(bank["name"]) in text), key=lambda bank: text.index(str(bank["name"])))
        for bank in named:
            found.append(relation(NOTE, "托管于", bank, (line.ref, text), state="已结束" if "转走" in text else "生效"))
    return {"entities": [NOTE, *BANKS], "facts": [], "relations": found}


def other_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> World:
    data = tmp_path / name
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    return World(data.resolve(), Settings.from_env())


def open_pairs(world: World) -> list[list[str]]:
    return [json.loads(row["claims"])["relations"] for row in world.rows(
        "SELECT claims FROM review_queue WHERE kind = 'other' AND decided_at IS NULL"
        " AND json_extract(claims, '$.reason') = 'same_time'")]


# ---------- S1：冻结的和同一个位置重发 ----------


def test_a_resent_sentence_saying_the_frozen_one_ended_does_not_end_it(world: World) -> None:
    """包重发，同一个位置那一句改成“转走了”（句子编号按位置算，重发不变）：新说法不挂到冻结的那一行上，照别人说的
    结束办：冻结的照旧现行，说结束的那一处自己一段进待确认。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    frozen = world.rows("SELECT relation_id FROM relations")[0]["relation_id"]
    freeze(world.settings, "prod", [frozen])
    deliver(world, resent("2026-11-01", "我：X1 票据从海港银行转走了。"))
    write(world, bank_extract).close()
    rows = {row["relation_id"]: row for row in world.rows("SELECT * FROM relations")}
    assert (rows[frozen]["state"], rows[frozen]["frozen"]) == ("active", 1)
    (ended,) = [row for one, row in rows.items() if one != frozen]
    assert (ended["state"], ended["pending_reason"]) == ("ended", "frozen_conflict")


def test_a_hidden_frozen_relation_is_not_brought_back_by_a_resent_end(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。", "我：今天就这些。"))
    write(world, bank_extract).close()
    frozen = world.rows("SELECT relation_id FROM relations")[0]["relation_id"]
    freeze(world.settings, "prod", [frozen])
    first = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%在海港银行%'")[0]["sentence_id"]
    withdraw(world.settings, "prod", sentences=[first])
    deliver(world, resent("2026-11-01", "我：X1 票据从海港银行转走了。", "我：今天就这些。"))
    write(world, bank_extract).close()
    (row,) = world.rows("SELECT withdrawn_at, state FROM relations WHERE relation_id = ?", frozen)
    assert row["withdrawn_at"] is not None and row["state"] == "active"
    assert [one["decision"] for one in world.rows("SELECT decision FROM review_queue WHERE kind = 'other'")] == [None]


# ---------- S2、G1：删掉、藏起冻结的以后同组重算 ----------


def test_unfreezing_a_bare_holder_lets_the_one_it_replaced_come_back(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-07", "我：X1 票据转到东岸证券了。"))
    write(world, moved_extract).close()
    broker = by_bank(world)["东岸证券"]["relation_id"]
    freeze(world.settings, "prod", [broker])
    deliver(world, resent("2026-11-07", "我：今天没什么事。"))  # 东岸那一句没了，只剩冻结标记撑着
    write(world, moved_extract).close()
    freeze(world.settings, "prod", [broker], frozen=False)  # 解除冻结：没有说法撑着，删掉
    rows = by_bank(world)
    assert set(rows) == {"海港银行"}
    assert (rows["海港银行"]["state"], rows["海港银行"]["valid_to"], rows["海港银行"]["superseded_by"]) == ("active", None, None)


def test_withdrawing_a_frozen_holder_by_id_lets_the_one_it_replaced_come_back(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-07", "我：X1 票据转到东岸证券了。"))
    write(world, moved_extract).close()
    broker = by_bank(world)["东岸证券"]["relation_id"]
    freeze(world.settings, "prod", [broker])
    assert main(["withdraw", "--library", "prod", "--fact", broker]) == 0
    rows = by_bank(world)
    assert set(rows) == {"海港银行"} and (rows["海港银行"]["state"], rows["海港银行"]["valid_to"]) == ("active", None)


def test_unfreezing_a_bare_frozen_holder_clears_the_new_one(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, moved_extract).close()
    bank = by_bank(world)["海港银行"]["relation_id"]
    freeze(world.settings, "prod", [bank])
    deliver(world, resent("2026-11-01", "我：今天没什么事。"))
    write(world, moved_extract).close()
    deliver(world, day_packet("2026-11-07", "我：X1 票据转到东岸证券了。"))
    write(world, moved_extract).close()
    assert by_bank(world)["东岸证券"]["pending_reason"] == "frozen_conflict"
    freeze(world.settings, "prod", [bank], frozen=False)
    rows = by_bank(world)
    assert set(rows) == {"东岸证券"} and (rows["东岸证券"]["status"], rows["东岸证券"]["pending_reason"]) == ("verified", None)


def test_hiding_a_frozen_holder_clears_the_new_one(world: World) -> None:
    """冻结的那条证据都撤回了、藏起来：和它冲突的待确认删掉，同组重算，新的不再是待确认（和事实一样）。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, moved_extract).close()
    freeze(world.settings, "prod", [by_bank(world)["海港银行"]["relation_id"]])
    deliver(world, day_packet("2026-11-07", "我：X1 票据转到东岸证券了。"))
    write(world, moved_extract).close()
    assert main(["withdraw", "--library", "prod", "--packet", "2026-11-01_2330_desk-bot"]) == 0
    broker = by_bank(world)["东岸证券"]
    assert (broker["status"], broker["pending_reason"], broker["state"]) == ("verified", None, "active")


# ---------- S3：冻结的正管着，后面又说了两个 ----------


def test_after_a_frozen_holder_the_later_new_one_replaces_the_earlier_new_one(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, three_banks).close()
    freeze(world.settings, "prod", [by_bank(world)["海港银行"]["relation_id"]])
    deliver(world, day_packet("2026-11-03", "我：X1 票据转到东岸证券了。"), day_packet("2026-11-05", "我：X1 票据转到西湾信托了。"))
    write(world, three_banks).close()
    rows = by_bank(world)
    assert (rows["海港银行"]["state"], rows["海港银行"]["frozen"]) == ("active", 1)
    assert (rows["东岸证券"]["state"], rows["东岸证券"]["superseded_by"]) == ("ended", rows["西湾信托"]["relation_id"])
    assert (rows["西湾信托"]["state"], rows["西湾信托"]["pending_reason"]) == ("active", "frozen_conflict")


# ---------- G3：冻结一条被顶掉过的 ----------


def test_freezing_a_replaced_one_guards_it_against_a_later_end(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据在东岸证券。"),
            day_packet("2026-11-10", "我：X1 票据从海港银行转走了。"))
    write(world, three_banks).close()
    bank = world.rows("SELECT r.relation_id FROM relations AS r JOIN entities AS e ON e.entity_id = r.object_entity_id"
                      " WHERE e.canonical_name = '海港银行' ORDER BY r.as_of_epoch")[0]["relation_id"]
    freeze(world.settings, "prod", [bank])  # 冻结一条以前被替代了的旧值：它又现行了
    (row,) = world.rows("SELECT state, frozen FROM relations WHERE relation_id = ?", bank)
    assert (row["state"], row["frozen"]) == ("active", 1)
    pending = sorted(row["pending_reason"] or "" for row in world.rows("SELECT pending_reason FROM relations WHERE relation_id != ?", bank))
    assert pending == ["frozen_conflict", "frozen_conflict"]  # 东岸证券、说转走的那一处各进一条


# ---------- S4、G1：同一句话里两个、三个 ----------


@pytest.mark.parametrize("order", [(BANK, BROKER), (BROKER, BANK)])
def test_a_and_b_in_one_sentence_after_a_holder_both_wait_for_review(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: tuple[dict, dict]
) -> None:
    """11-01 在海港银行；11-05 同一句话说海港银行和东岸证券：同一句话里两个（R12），都进待确认，和 L3 写的先后无关。"""

    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            for bank in (order if "和" in text else (BANK,)):
                found.append(relation(NOTE, "托管于", bank, (line.ref, text)))
        return {"entities": [NOTE, BANK, BROKER], "facts": [], "relations": found}

    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据在海港银行和东岸证券。"))
    write(world, extract).close()
    rows = by_bank(world)
    assert {(name, row["state"], row["pending_reason"]) for name, row in rows.items()} == {
        ("海港银行", "active", "other"), ("东岸证券", "active", "other")}
    assert open_pairs(world) == [sorted(row["relation_id"] for row in rows.values())]


def test_three_in_one_sentence_all_wait_for_review_whatever_the_order(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number, order in enumerate(itertools.permutations(BANKS)):
        other = other_world(tmp_path, monkeypatch, f"order{number}")
        text = "、".join(str(bank["name"]) for bank in order)
        deliver(other, day_packet("2026-11-01", f"我：X1 票据在{text}三家里的一家。"))
        write(other, three_banks).close()
        pending = {row["relation_id"] for row in other.rows("SELECT relation_id FROM relations WHERE pending_reason = 'other'")}
        assert len(pending) == 3
        assert pending <= {one for pair in open_pairs(other) for one in pair}, order  # 每一条待确认的都有它的那一条


def test_a_resend_that_moves_a_bare_frozen_one_is_a_frozen_conflict(world: World) -> None:
    """冻结的只剩冻结标记，包重发、同一个时刻改成了别家：不是同一句话（R12），是和冻结的冲突。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, moved_extract).close()
    bank = by_bank(world)["海港银行"]["relation_id"]
    freeze(world.settings, "prod", [bank])
    deliver(world, resent("2026-11-01", "我：X1 票据在东岸证券。"))
    write(world, moved_extract).close()
    broker = by_bank(world)["东岸证券"]
    assert broker["pending_reason"] == "frozen_conflict"
    (review,) = world.rows("SELECT claims FROM review_queue WHERE decided_at IS NULL")
    assert json.loads(review["claims"]) == {"frozen": bank, "new": broker["relation_id"]}


# ---------- G2：同一句话里两个的待确认，编号跟着改、跟着删 ----------


def test_the_same_time_review_follows_a_deleted_relation(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行和东岸证券。"))
    write(world, moved_extract).close()
    assert len(open_pairs(world)) == 1
    deliver(world, resent("2026-11-01", "我：X1 票据在海港银行。"))  # 东岸那一处没了
    write(world, moved_extract).close()
    rows = by_bank(world)
    assert set(rows) == {"海港银行"} and rows["海港银行"]["pending_reason"] is None
    assert open_pairs(world) == []


def test_the_same_time_decision_follows_a_renamed_relation(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。", "我：X1 票据在海港银行和东岸证券。"))
    write(world, moved_extract).close()
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "both"]) == 0
    first = world.rows("SELECT sentence_id FROM raw_sentences WHERE text = '我：X1 票据在海港银行。'"
                       " OR text = 'X1 票据在海港银行。'")[0]["sentence_id"]
    before = by_bank(world)["海港银行"]["relation_id"]
    withdraw(world.settings, "prod", sentences=[first])  # 海港银行那一段最早那一句撤回了：编号跟着变
    rows = by_bank(world)
    assert rows["海港银行"]["relation_id"] != before
    assert {row["pending_reason"] for row in rows.values()} == {None}  # 主人定过两边都留，跟着新编号
    assert open_pairs(world) == []


# ---------- G4、拿不准的第 1 条：负责按代表哪一家分组 ----------


LI = {"key": "e11", "mention": "小李", "name": "李一舟", "type": "人"}
LAO = {"key": "e12", "mention": "老周", "name": "周建宇", "type": "人"}
ORDER = {"key": "e13", "mention": "越南那批", "name": "越南那批订单", "type": "订单或合同"}


def late_company() -> str:
    """任职于的键排在负责那一组的名字（family…）后面的公司名（以前按名字排先后，负责会先算）。"""
    scope = Scope(("business",), ())
    for number in range(1, 5000):
        company = f"港湾{number}号银行"
        if relation_identity(scope, entity_id_of("人", name_key("李一舟")), "任职于", entity_id_of("公司或机构", name_key(company))) > "family":
            return company
    raise AssertionError("没找到")


def test_responsibility_regroups_after_the_employer_changes_in_the_same_window(world: World) -> None:
    company = late_company()
    firm = {"key": "e14", "mention": company, "name": company, "type": "公司或机构"}

    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            for person in (LI, LAO):
                name = str(person["mention"])
                if f"{name}在{company}" in text:
                    found.append(relation(person, "任职于", firm, (line.ref, text)))
                if f"{name}从{company}离职" in text:
                    found.append(relation(person, "任职于", firm, (line.ref, text), state="已结束"))
                if f"{name}负责越南那批" in text:
                    found.append(relation(person, "负责", ORDER, (line.ref, text)))
        return {"entities": [LI, LAO, ORDER, firm], "facts": [], "relations": found}

    deliver(world, day_packet("2026-11-01", f"我：小李在{company}。", f"我：老周在{company}。"),
            day_packet("2026-11-02", "我：小李负责越南那批。"))
    write(world, extract).close()
    deliver(world, day_packet("2026-11-10", f"我：小李从{company}离职了。", "我：老周负责越南那批。"))
    write(world, extract).close()
    rows = {row["who"]: row for row in world.rows(
        "SELECT e.canonical_name AS who, r.state, r.superseded_by FROM relations AS r JOIN entities AS e"
        " ON e.entity_id = r.subject_entity_id WHERE r.relation_type = '负责'")}
    assert (rows["李一舟"]["state"], rows["李一舟"]["superseded_by"]) == ("active", None)  # 离职了，看不出哪家，和老周不同组


def test_the_owner_side_is_one_group_even_if_the_owner_has_an_employer(world: World) -> None:
    """D153 第 2 条：主人这边算“看不出的”那一组：主人记了任职于哪家，也照主人这边分组。"""
    project = {"key": "e9", "mention": "每日盯盘", "name": "每日盯盘", "type": "项目"}
    helper = {"key": "e15", "mention": "小周", "name": "周小舟", "type": "人"}

    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            if "我在东岸证券上班" in text:
                found.append(relation(OWNER, "任职于", BROKER, (line.ref, text)))
            if "每日盯盘我来做" in text:
                found.append(relation(OWNER, "负责", project, (line.ref, text)))
            if "每日盯盘交给小周" in text:
                found.append(relation(helper, "负责", project, (line.ref, text)))
        return {"entities": [OWNER, BROKER, project, helper], "facts": [], "relations": found}

    deliver(world, day_packet("2026-10-30", "我：我在东岸证券上班。"), day_packet("2026-11-01", "我：每日盯盘我来做。"),
            day_packet("2026-11-05", "我：每日盯盘交给小周了。"))
    write(world, extract).close()
    rows = {row["who"]: row for row in world.rows(
        "SELECT e.canonical_name AS who, r.state, r.superseded_by FROM relations AS r JOIN entities AS e"
        " ON e.entity_id = r.subject_entity_id WHERE r.relation_type = '负责'")}
    assert rows["主人"]["state"] == "ended" and rows["主人"]["superseded_by"] is not None


# ---------- G5：暂停那天的写法 ----------


def test_the_pause_day_reads_spaced_dates_and_only_the_pause_clause() -> None:
    said = datetime(2026, 11, 5, 10, 30, tzinfo=timezone(timedelta(hours=8)))  # 星期四

    def proof(text: str) -> Proof:
        return Proof(ref="T1", sentence_id="p#1.1.1", packet_id="p", revision=1, quote=text, shown=text, kind="said",
                     origin="said", field=None, redacted=False, speaker="我", speaker_role="owner", said_at=said)

    assert pause_day([proof("X1 票据 11 月 9 日起先停")]) == ("2026-11-09", "day")
    assert pause_day([proof("X1 票据11月9日起先停")]) == ("2026-11-09", "day")
    assert pause_day([proof("X1 票据先停，11月20日起恢复")]) == ("2026-11-05", "day")  # 那是恢复的那天，不是停的那天


# ---------- 小的第 4 条：打分的亲属写法 ----------


def test_scoring_takes_the_kinship_of_each_claim(world: World) -> None:
    from javmem.scoring.results import side_from_result

    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            if "小周是我" in text:
                found.append(relation(ZHOU, "亲属", OWNER, (line.ref, text), kinship="Cousin" if "Cousin" in text else "cousin"))
        return {"entities": [ZHOU, OWNER], "facts": [], "relations": found}

    deliver(world, day_packet("2026-11-01", "我：小周是我 Cousin。"), day_packet("2026-11-05", "我：小周是我 cousin。"))
    write(world, extract).close()
    side, _ = side_from_result(world.data, ["2026-11-01_2330_desk-bot", "2026-11-05_2330_desk-bot"], library="prod")
    assert sorted(one.fields["kinship"] for one in side.relations.values()) == ["Cousin", "cousin"]
