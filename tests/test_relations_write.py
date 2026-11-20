"""L5 把关系写进账本（J4.4 第 1 段）：说法、合成、分段、两头对齐，和撤回、重发、改指这些清理的地方。例子都是编的。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from helpers import World, build_packet
from test_relcheck import BANK, BROKER, NOTE, OWNER, STOCK, ZHOU, relation
from test_withdraw_mail import MASK, SENDER, delivered, two_mails
from test_write import checked_items, day_packet, deliver, dump, l3_fact, write

from javmem.cli import main
from javmem.decisions import export_decisions, import_decisions
from javmem.ledger import open_ledger, transaction
from javmem.pipeline.merge import (
    L5_VERSION,
    RelationClaimView,
    date_bounds,
    relation_segments,
    relation_timeline,
    summarize_segment,
)
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.window import Window
from javmem.pipeline.write import LedgerWriter
from javmem.review import freeze
from javmem.withdraw import export_records, withdraw

ME = {"key": "e9", "mention": "我", "name": "我", "type": "人"}
OTHER = {"key": "e10", "mention": "对方", "name": "对方", "type": "公司或机构"}


def relations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM relations ORDER BY as_of_epoch, relation_id").fetchall()


def names(conn: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in conn.execute("SELECT entity_id, canonical_name FROM entities")}


def names_of(world: World) -> dict[str, str]:
    return {row["entity_id"]: row["canonical_name"] for row in world.rows("SELECT entity_id, canonical_name FROM entities")}


def resent(day: str, *lines: str, revision: int = 2) -> str:
    """同一个包重发的新版本（包号一样，revision 加一）。"""
    body = f"## 原话 · {day} 10:30–10:59\n" + "".join(f"- [10:{30 + index:02d}] {line}\n" for index, line in enumerate(lines))
    return build_packet(source="desk-bot", stamp=f"{day}_2330", body=body, created_at=f"{day}T23:30:00+08:00",
                        covers_from=f"{day}T00:00:00+08:00", covers_to=f"{day}T23:30:00+08:00", domain="[business]",
                        revision=revision)


def bank_extract(window: Window) -> dict[str, object]:
    """写着“X1 票据”的每一行：X1 票据托管于海港银行；写着“转走了”的标已结束；写着“起”的照那句写开始日子。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        if "X1 票据" not in text:
            continue
        fields: dict[str, object] = {"state": "已结束" if "转走" in text else "生效"}
        if "11 月 1 日起" in text:
            fields.update(valid_from="2026-11-01", time_precision="day")
        if "11 月 5 日转走" in text:
            fields.update(valid_to="2026-11-05", time_precision="day")
        found.append(relation(NOTE, "托管于", BANK, (line.ref, text), **fields))
    return {"entities": [NOTE, BANK], "facts": [], "relations": found}


# ---------- 写进来 ----------


def test_a_relation_that_passed_l4_is_written_with_its_claim_and_evidence(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    conn = write(world, bank_extract)
    (row,) = relations(conn)
    assert row["relation_id"].startswith("R") and row["relation_type"] == "托管于"
    assert (names(conn)[row["subject_entity_id"]], names(conn)[row["object_entity_id"]]) == ("X1 票据", "海港银行")
    assert (row["state"], row["status"], row["pending_reason"], row["frozen"]) == ("active", "verified", None, 0)
    assert (row["speaker"], row["speaker_role"], row["attributed_to"]) == ("我", "owner", "主人")
    assert row["as_of"].startswith("2026-11-01") and row["rule_version"].endswith(L5_VERSION)
    assert json.loads(row["scope_partitions"]) == ["business"] and row["superseded_by"] is None
    assert (row["owner_said"], row["stated"], row["secondhand"], json.loads(row["sources"])) == (1, 1, 0, ["desk-bot"])
    (claim,) = conn.execute("SELECT * FROM relation_claims").fetchall()
    assert (claim["relation_id"], claim["identity_key"]) == (row["relation_id"], row["identity_key"])
    assert claim["claim_id"].endswith("#0") and claim["state"] == "active" and claim["certainty"] == "stated"
    (evidence,) = conn.execute("SELECT * FROM relation_evidence").fetchall()
    assert evidence["quote"] == "X1 票据在海港银行"
    assert json.loads(evidence["position"]) == {"section": 1, "line": 1, "sentence": 1}
    assert (evidence["source"], evidence["origin"], evidence["speaker_role"]) == ("desk-bot", "said", "owner")
    assert evidence["sentence_hash"] and evidence["window_id"] == claim["window_id"]
    rules = {one[0] for one in conn.execute("SELECT rule_id FROM rules WHERE layer = 'L5'")}
    assert {"L5-relation-merge", "L5-relation-segment"} <= rules


def test_the_same_relation_said_again_only_adds_evidence(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    conn = write(world, bank_extract)
    (row,) = relations(conn)
    assert row["as_of"].startswith("2026-11-05")
    assert conn.execute("SELECT count(*) FROM relation_claims WHERE relation_id = ?", (row["relation_id"],)).fetchone()[0] == 2
    quotes = [one[0] for one in conn.execute("SELECT quote FROM relation_evidence ORDER BY evidence_id")]
    assert quotes == ["X1 票据在海港银行", "X1 票据还在海港银行"]


def test_different_scopes_are_kept_apart(world: World) -> None:
    """D30：范围不同的不合并。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"),
            day_packet("2026-11-02", "我：X1 票据在海港银行。", domain="[business, research]"))
    conn = write(world, bank_extract)
    assert sorted(row["scope_partitions"] for row in relations(conn)) == ['["business", "research"]', '["business"]']


def test_an_end_and_a_new_start_make_two_segments(world: World) -> None:
    """卡 3 的 D101：中间写了结束，后来又说起，是另一段。"""
    deliver(
        world,
        day_packet("2026-11-01", "我：X1 票据在海港银行。"),
        day_packet("2026-11-03", "我：X1 票据从海港银行转走了。"),
        day_packet("2026-11-10", "我：X1 票据又放回海港银行了。"),
    )
    conn = write(world, bank_extract)
    first, second = relations(conn)
    assert (first["state"], second["state"]) == ("ended", "active")
    assert first["identity_key"] == second["identity_key"] and first["relation_id"] != second["relation_id"]
    counts = [conn.execute("SELECT count(*) FROM relation_claims WHERE relation_id = ?", (one["relation_id"],)).fetchone()[0]
              for one in (first, second)]
    assert counts == [2, 1]
    assert [one[0] for one in conn.execute(
        "SELECT quote FROM relation_evidence WHERE relation_id = ? ORDER BY evidence_id", (second["relation_id"],))] == [
        "X1 票据又放回海港银行了"]


def test_ids_and_rows_do_not_depend_on_the_writing_order(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.config import Settings

    packets = [
        day_packet("2026-11-01", "我：X1 票据在海港银行。"),
        day_packet("2026-11-03", "我：X1 票据从海港银行转走了。"),
        day_packet("2026-11-10", "我：X1 票据又放回海港银行了。"),
    ]
    tables = []
    for name, chosen in (("forward", packets), ("backward", list(reversed(packets)))):
        data = tmp_path / name
        data.mkdir()
        monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
        other = World(data.resolve(), Settings.from_env())
        for packet in chosen:  # 一包一包地收、一包一包地写：晚到的旧包后写
            deliver(other, packet)
            write(other, bank_extract).close()
        conn = open_ledger(other.settings.library("prod"))
        tables.append({table: dump(conn)[table] for table in ("relations", "relation_evidence")})
        conn.close()
    assert tables[0]["relations"] == tables[1]["relations"]

    def by_relation(rows: list[tuple[object, ...]]) -> dict[object, list[tuple[object, ...]]]:
        """每条关系的证据，按它自己的先后（证据又说了一遍只往后加，几条关系之间的行号先后跟着写的先后，不算结果）。"""
        grouped: dict[object, list[tuple[object, ...]]] = {}
        for row in rows:
            grouped.setdefault(row[1], []).append(row[2:])
        return grouped

    assert by_relation(tables[0]["relation_evidence"]) == by_relation(tables[1]["relation_evidence"])


def test_the_kinship_wording_comes_from_the_earliest_claim_whatever_the_writing_order(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审阅第 6 条：亲属的具体说法按比对写法认（大小写不同的是同一条），关系那一行存的写法取这一段最早说的那一处，
    和写的先后无关。"""
    from javmem.config import Settings

    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            text = line.text.rstrip("。")
            if "小周是我" in text:
                found.append(relation(ZHOU, "亲属", OWNER, (line.ref, text), kinship="Cousin" if "Cousin" in text else "cousin"))
        return {"entities": [ZHOU, OWNER], "facts": [], "relations": found}

    packets = [day_packet("2026-11-01", "我：小周是我 Cousin。"), day_packet("2026-11-05", "我：小周是我 cousin。")]
    found = []
    for name, chosen in (("forward", packets), ("backward", list(reversed(packets)))):
        data = tmp_path / name
        data.mkdir()
        monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
        other = World(data.resolve(), Settings.from_env())
        for packet in chosen:  # 一包一包地收、一包一包地写
            deliver(other, packet)
            write(other, extract).close()
        found.append([(row["relation_id"], row["kinship"]) for row in other.rows("SELECT relation_id, kinship FROM relations")])
    assert found[0] == found[1] and [kinship for _, kinship in found[0]] == ["Cousin"]


def test_two_segments_that_start_from_the_same_sentence_get_their_own_rows(world: World) -> None:
    """审阅第 1 条：一个窗口里 L3 把同一条关系给了两次，一处写了已结束、一处没写，最早那一句是同一句：分成两段，各一行
    （编号不撞），每一处说法的证据都在它那一行的证据里；撤回其中一句，说法里也清干净。"""

    def extract(window: Window) -> dict[str, object]:
        lines = {line.text.rstrip("。"): line.ref for line in window.targets}
        before, now, between = (next(text for text in lines if word in text) for word in ("以前在", "现在还在", "中间转走过"))
        return {"entities": [NOTE, BANK], "facts": [], "relations": [
            relation(NOTE, "托管于", BANK, (lines[before], before), (lines[between], between), state="已结束"),
            relation(NOTE, "托管于", BANK, (lines[before], before), (lines[now], now)),
        ]}

    deliver(world, day_packet("2026-11-01", "我：X1 票据以前在海港银行。", "我：X1 票据现在还在海港银行。", "我：X1 票据中间转走过一次。"))
    conn = write(world, extract)
    rows = relations(conn)
    assert sorted(row["state"] for row in rows) == ["active", "ended"] and len({row["relation_id"] for row in rows}) == 2
    for claim in conn.execute("SELECT relation_id, proofs FROM relation_claims").fetchall():
        held = {one[0] for one in conn.execute("SELECT sentence_id FROM relation_evidence WHERE relation_id = ?", (claim["relation_id"],))}
        assert {proof["sentence_id"] for proof in json.loads(claim["proofs"])} <= held
    conn.close()
    between = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%中间转走过%'")[0]["sentence_id"]
    withdraw(world.settings, "prod", sentences=[between])
    assert not [row for row in world.rows("SELECT proofs FROM relation_claims") if "中间转走过" in row["proofs"]]
    assert not world.rows("SELECT 1 FROM relation_evidence WHERE quote LIKE '%中间转走过%'")


def test_saying_a_relation_again_only_adds_its_new_evidence_rows(world: World) -> None:
    """审阅第 7 条：一条关系又说了一遍，以前那几句证据不删了重插（不然说得越多写得越慢），只加新的那一句。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    write(world, bank_extract).close()
    world.execute("UPDATE relation_evidence SET created_at = '2026-01-01T00:00:00+08:00'")  # 记个号：删了重插的这一列会变
    deliver(world, day_packet("2026-11-09", "我：X1 票据仍在海港银行。"))
    write(world, bank_extract).close()
    after = world.rows("SELECT created_at, quote FROM relation_evidence ORDER BY evidence_id")
    assert [row["quote"] for row in after] == ["X1 票据在海港银行", "X1 票据还在海港银行", "X1 票据仍在海港银行"]
    assert [row["created_at"] for row in after[:2]] == ["2026-01-01T00:00:00+08:00"] * 2


def test_rewriting_a_window_changes_nothing(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据从海港银行转走了。"))
    items = checked_items(world, bank_extract)
    conn = write(world, bank_extract)
    before = dump(conn, surrogate=False)
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
    with VectorCache(world.data / "vectors") as cache:
        for item in items:
            assert writer.already_written(item)
            writer.write_window(item, cache.get("fake-embedding", writer.texts_needed(item)))
    assert dump(conn, surrogate=False) == before


def test_an_end_that_is_not_an_entity_goes_to_quarantine(world: World) -> None:
    """一头 L5 不建实体（泛称“对方”）：关系表两头都要是实体，写不进去，进隔离表（规则 L5）。"""

    def extract(window: Window) -> dict[str, object]:
        line = window.targets[0]
        return {"entities": [OWNER, OTHER], "facts": [],
                "relations": [relation(OWNER, "客户是", OTHER, (line.ref, line.text.rstrip("。")))]}

    deliver(world, day_packet("2026-11-01", "我：对方那边我开了户。"))
    conn = write(world, extract)
    assert relations(conn) == []
    (row,) = conn.execute("SELECT item_kind, reason, rule_id, rule_version, payload FROM quarantine").fetchall()
    assert (row["item_kind"], row["reason"], row["rule_id"], row["rule_version"]) == ("relation", "L5:end_not_entity", "L5", L5_VERSION)
    assert json.loads(row["payload"])["proofs"]  # 证据在哪几句：单条撤回清得掉


def test_a_first_person_end_said_by_the_owner_is_the_owner(world: World) -> None:
    """只出现在关系里的“我”也看证据是谁说的（J4.4，规矩照事实）：主人自己说的是主人。"""

    def extract(window: Window) -> dict[str, object]:
        line = window.targets[0]
        return {"entities": [ME, STOCK], "facts": [], "relations": [relation(ME, "持有", STOCK, (line.ref, line.text.rstrip("。")))]}

    deliver(world, day_packet("2026-11-01", "我：我还拿着 X2。"))
    conn = write(world, extract)
    (row,) = relations(conn)
    assert names(conn)[row["subject_entity_id"]] == "主人"


def test_an_ambiguous_end_makes_the_relation_wait_for_review(world: World) -> None:
    """一头对上两个实体的（实体有歧义）：这条关系是待确认（和事实一样）；主人定了两边都留，就不是了。"""
    first = {"key": "e2", "mention": "X1", "name": "X1 票据", "type": "订单或合同"}
    second = {"key": "e2", "mention": "X1", "name": "X1 期权", "type": "订单或合同"}
    plain = {"key": "e2", "mention": "X1", "name": "X1", "type": "订单或合同"}

    def extract(window: Window) -> dict[str, object]:
        text = " ".join(line.text for line in window.targets)
        line = window.targets[0]
        entity = first if "票据" in text else second if "期权" in text else plain
        return {"entities": [entity, BANK], "facts": [],
                "relations": [relation(entity, "托管于", BANK, (line.ref, line.text.rstrip("。")))]}

    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-02", "我：X1 期权在海港银行。"))
    write(world, extract).close()
    deliver(world, day_packet("2026-11-03", "我：X1 在海港银行。"))
    conn = write(world, extract)
    (pending,) = [row for row in relations(conn) if row["status"] == "pending"]
    assert pending["pending_reason"] == "ambiguous_entity" and names(conn)[pending["subject_entity_id"]] == "X1"
    review = conn.execute("SELECT review_id FROM review_queue WHERE kind = 'ambiguous_entity'").fetchone()[0]
    conn.close()
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "both"]) == 0
    status = world.rows("SELECT status FROM relations WHERE relation_id = ?", pending["relation_id"])[0]["status"]
    assert status == "verified"


# ---------- 分段（纯函数） ----------


def view(claim_id: str, said: str, state: str = "active", *, start: str | None = None, end: str | None = None,
         precision: str = "day", status: str = "verified") -> RelationClaimView:
    day = date.fromisoformat(said)
    moment = day.toordinal() * 86400  # 只拿来排先后
    return RelationClaimView(
        claim_id=claim_id, state=state, valid_from=start, valid_from_precision=precision if start else "unknown",
        valid_from_basis=None, valid_to=end, valid_to_precision=precision if end else "unknown", valid_to_basis=None,
        status=status, speaker="我", speaker_role="owner", attributed_to="主人", said_from=f"{said}T10:00:00+08:00",
        said_from_epoch=moment, said_to=f"{said}T10:00:00+08:00", said_to_epoch=moment, anchor=f"S-{claim_id}", said_day=day,
    )


def ids(segments: list[list[RelationClaimView]]) -> list[list[str]]:
    return [[claim.claim_id for claim in segment] for segment in segments]


def test_segments_follow_the_ends() -> None:
    claims = [view("a", "2026-11-01"), view("b", "2026-11-03", "ended"), view("c", "2026-11-10")]
    assert ids(relation_segments(claims)) == [["a", "b"], ["c"]]
    # 结束日子在说这句话那天或以前的，也算这一段结束了；还没到的（到期日）不算
    assert ids(relation_segments([view("a", "2026-11-03", end="2026-11-02"), view("b", "2026-11-05")])) == [["a"], ["b"]]
    assert ids(relation_segments([view("a", "2026-11-01", end="2027-01-15"), view("b", "2026-11-05")])) == [["a", "b"]]


def test_a_later_mention_of_a_closed_segment_goes_back_to_it() -> None:
    """后来说起的开始日子不晚于前一段的结束：是又说了一遍那一段（D115）。"""
    claims = [
        view("a", "2026-11-01", start="2026-11-01"),
        view("b", "2026-11-06", "ended", end="2026-11-05"),
        view("c", "2026-11-10"),
        view("d", "2026-11-20", start="2026-11-01"),
    ]
    assert ids(relation_segments(claims)) == [["a", "b", "d"], ["c"]]


def test_an_old_story_that_ended_before_the_current_segment_does_not_end_it() -> None:
    """晚到的旧事：说的是现在这一段开始以前就结束了的，另起一段，现在这一段照旧。"""
    claims = [view("a", "2026-11-10", start="2026-11-10"), view("b", "2026-11-20", "ended", start="2026-09-01", end="2026-09-30")]
    segments = relation_segments(claims)
    assert ids(segments) == [["a"], ["b"]]
    assert [summarize_segment(segment).state for segment in segments] == ["active", "ended"]


def test_without_a_written_start_a_later_end_closes_the_current_segment() -> None:
    """审阅第 4 条（先这样定，报告第 4 节）：现在这一段没写开始日子，后说的说它早就结束了：照说的先后，这一段到那天结束
    （D153 第 1 条“有一句没写的照说话的先后”；打分的 D115 合法也是这样）。写了开始日子的才看得出是不是晚到的旧事。"""
    segments = relation_segments([view("a", "2026-11-10"), view("b", "2026-11-20", "ended", end="2026-09-30")])
    assert ids(segments) == [["a", "b"]]
    assert (summarize_segment(segments[0]).state, summarize_segment(segments[0]).valid_to) == ("ended", "2026-09-30")


def test_an_end_said_again_without_a_start_goes_back_to_the_segment_that_ended_then() -> None:
    """真调测试题 K25（J4.6）：已经结束了的那一段（结束日子写到月），后来又说了一遍“那个月就结束了”、没写开始日子：结束日子
    对得上那一段的，归那一段，不另起一段（D83 回答那一包常这样：Bot 问“你说……上个月就兑付了，去掉吗”，主人“对，改吧”）。"""
    claims = [view("a", "2026-10-03"), view("b", "2026-11-10", "ended", end="2026-10", precision="month"),
              view("c", "2026-11-11", "ended", end="2026-10", precision="month")]
    segments = relation_segments(claims)
    assert ids(segments) == [["a", "b", "c"]]
    assert (summarize_segment(segments[0]).state, summarize_segment(segments[0]).valid_to) == ("ended", "2026-10")


def test_an_end_said_again_for_an_earlier_stint_goes_back_to_that_stint() -> None:
    """现在又是另一段了，后来说的是以前那一段的结束（结束日子对得上、没写开始日子）：归以前那一段，现在这一段照旧。"""
    claims = [view("a", "2025-01-05", start="2025-01-05"), view("b", "2025-06-30", "ended", end="2025-06-30"),
              view("c", "2026-01-05", start="2026-01-05"), view("d", "2026-02-01", "ended", end="2025-06-30")]
    segments = relation_segments(claims)
    assert ids(segments) == [["a", "b", "d"], ["c"]]
    assert [summarize_segment(segment).state for segment in segments] == ["ended", "active"]


def test_an_end_that_matches_no_ended_segment_still_starts_its_own() -> None:
    """结束日子对不上已经结束的那几段的，照旧另起一段（旧事）。"""
    claims = [view("a", "2026-10-03"), view("b", "2026-11-10", "ended", end="2026-10", precision="month"),
              view("c", "2026-11-11", "ended", end="2025-06", precision="month")]
    assert ids(relation_segments(claims)) == [["a", "b"], ["c"]]


def test_a_vaguer_end_said_again_does_not_join_the_segment() -> None:
    """K25 审阅：只认写法一样的结束日子；结束日子写到哪一天的那一段，后来只说到那个月的不归进去（归进去会把这一段的结束日子
    换成只到月）。"""
    claims = [view("a", "2026-10-03"), view("b", "2026-11-01", "ended", end="2026-10-31"),
              view("c", "2026-11-11", "ended", end="2026-10", precision="month")]
    segments = relation_segments(claims)
    assert ids(segments) == [["a", "b"], ["c"]]
    assert summarize_segment(segments[0]).valid_to == "2026-10-31"


def test_an_end_said_again_with_a_start_date_is_left_to_the_start_rule() -> None:
    """K25 审阅：写了开始日子的照 J4.4 按开始日子认（开始日子晚于那一段的结束、对不上的照旧另起一段），新的这条只管没写
    开始日子的。"""
    claims = [view("a", "2026-10-03"), view("b", "2026-11-10", "ended", end="2026-10", precision="month"),
              view("c", "2026-11-12", "ended", start="2026-11-11", end="2026-10", precision="month")]
    assert ids(relation_segments(claims)) == [["a", "b"], ["c"]]


def test_a_judged_end_does_not_go_back_to_an_ended_segment() -> None:
    """K25 审阅：软变化判了结束的那一处照判的办（第三轮审阅 S1：不看又说了一遍哪一段），不归到结束日子一样的那一段。"""
    claims = [view("a", "2026-10-03"), view("b", "2026-11-10", "ended", end="2026-10", precision="month"),
              view("c", "2026-11-11", "ended", end="2026-10", precision="month")]
    segments = relation_timeline({"": claims}, forced={"c"})
    assert [[claim.claim_id for claim in segment.members] for segment in segments] == [["a", "b"], ["c"]]


def test_a_frozen_ended_relation_without_claims_is_not_rewritten_by_an_end_said_again(world: World) -> None:
    """K25 审阅：冻结的、已结束的那一行包重发以后没有说法了（空壳），后来又有人说“11 月 5 日转走了”（没写开始）：不归进
    冻结的那一段（不然冻结那行的各列、证据照新说法重算），照旧另起一段。"""
    deliver(world, day_packet("2026-11-06", "我：X1 票据 11 月 1 日起在海港银行，11 月 5 日转走了。"))
    write(world, bank_extract).close()
    (row,) = world.rows("SELECT relation_id FROM relations")
    freeze(world.settings, "prod", [row["relation_id"]])
    deliver(world, resent("2026-11-06", "我：今天没什么事。"))
    write(world, bank_extract).close()
    deliver(world, day_packet("2026-11-20", "我：海港银行那边，X1 票据 11 月 5 日转走了。"))
    write(world, bank_extract).close()
    (frozen,) = world.rows("SELECT * FROM relations WHERE relation_id = ?", row["relation_id"])
    assert (frozen["frozen"], frozen["state"], frozen["valid_from"], frozen["valid_to"]) == (1, "ended", "2026-11-01", "2026-11-05")
    assert len(world.rows("SELECT 1 FROM relations")) == 2


def test_a_segment_summary_takes_the_earliest_and_most_precise_start() -> None:
    segment = [
        view("a", "2026-11-03", start="2026-11", precision="month", status="inferred"),
        view("b", "2026-11-04", start="2026-11-01"),
        view("c", "2026-11-08", "paused", status="inferred"),
    ]
    summary = summarize_segment(segment)
    assert (summary.valid_from, summary.valid_from_precision) == ("2026-11-01", "day")
    assert (summary.state, summary.status, summary.anchor) == ("paused", "verified", "S-a")
    assert summary.as_of.startswith("2026-11-08") and summary.attributed_to == "主人"
    assert date_bounds("2026-Q3") == (date(2026, 7, 1), date(2026, 9, 30)) and date_bounds("下周") is None


# ---------- 清理的地方 ----------


def test_withdrawing_the_sentence_takes_the_relation_away(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    write(world, bank_extract).close()
    sentence = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%还在%'")[0]["sentence_id"]
    assert main(["withdraw", "--library", "prod", "--sentence", sentence]) == 0
    (row,) = world.rows("SELECT relation_id, as_of FROM relations")
    assert row["as_of"].startswith("2026-11-01")
    assert [one["quote"] for one in world.rows("SELECT quote FROM relation_evidence")] == ["X1 票据在海港银行"]
    first = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%在海港银行。'")[0]["sentence_id"]
    assert main(["withdraw", "--library", "prod", "--sentence", first]) == 0
    assert world.rows("SELECT count(*) AS n FROM relations")[0]["n"] == 0
    assert world.rows("SELECT count(*) AS n FROM relation_claims")[0]["n"] == 0


def test_withdrawing_a_relation_by_its_id(world: World) -> None:
    """javmem withdraw --fact 也收关系的编号（R 开头）：撤回它的证据句，冻结的也删（主人点名的）。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    relation_id = world.rows("SELECT relation_id FROM relations")[0]["relation_id"]
    world.execute("UPDATE relations SET frozen = 1 WHERE relation_id = ?", relation_id)
    assert main(["withdraw", "--library", "prod", "--fact", relation_id]) == 0
    assert world.rows("SELECT count(*) AS n FROM relations")[0]["n"] == 0
    assert world.rows("SELECT count(*) AS n FROM raw_sentences WHERE withdrawn_at IS NOT NULL")[0]["n"] == 1
    assert main(["withdraw", "--library", "prod", "--fact", "R0000000000000000"]) == 1  # 没有这一条：什么都不动


def test_a_resent_packet_replaces_its_relation_claims_and_a_frozen_one_stays(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    old = world.rows("SELECT relation_id FROM relations")[0]["relation_id"]
    deliver(world, resent("2026-11-01", "我：今天没什么事。"))  # 同一个包重发（第 2 版），那一句没了
    assert world.rows("SELECT count(*) AS n FROM relations")[0]["n"] == 0
    write(world, bank_extract).close()
    deliver(world, day_packet("2026-11-02", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    kept = world.rows("SELECT relation_id FROM relations")[0]["relation_id"]
    assert kept != old  # 这一段最早那一句换了：编号跟着换
    world.execute("UPDATE relations SET frozen = 1 WHERE relation_id = ?", kept)
    deliver(world, resent("2026-11-02", "我：今天也没什么事。"))
    (row,) = world.rows("SELECT relation_id, frozen FROM relations")
    assert (row["relation_id"], row["frozen"]) == (kept, 1)  # 冻结的：说法没了也留着，证据还指着旧版本
    assert world.rows("SELECT count(*) AS n FROM relation_claims")[0]["n"] == 0
    assert world.rows("SELECT count(*) AS n FROM relation_evidence WHERE relation_id = ?", kept)[0]["n"] == 1


def test_a_frozen_relation_whose_evidence_is_withdrawn_is_hidden_until_the_owner_decides(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    relation_id = world.rows("SELECT relation_id FROM relations")[0]["relation_id"]
    world.execute("UPDATE relations SET frozen = 1 WHERE relation_id = ?", relation_id)
    packet = "2026-11-01_2330_desk-bot"
    assert main(["withdraw", "--library", "prod", "--packet", packet]) == 0
    (row,) = world.rows("SELECT withdrawn_at, sources FROM relations WHERE relation_id = ?", relation_id)
    assert row["withdrawn_at"] is not None and json.loads(row["sources"]) == []
    (review,) = world.rows("SELECT review_id, kind, claims FROM review_queue")
    assert review["kind"] == "other"
    assert json.loads(review["claims"]) == {"relation": relation_id, "reason": "evidence_withdrawn", "packets": [packet]}
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "frozen"]) == 0
    assert world.rows("SELECT withdrawn_at FROM relations WHERE relation_id = ?", relation_id)[0]["withdrawn_at"] is None


def test_dropping_a_hidden_frozen_relation(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    relation_id = world.rows("SELECT relation_id FROM relations")[0]["relation_id"]
    world.execute("UPDATE relations SET frozen = 1 WHERE relation_id = ?", relation_id)
    assert main(["withdraw", "--library", "prod", "--packet", "2026-11-01_2330_desk-bot"]) == 0
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--drop"]) == 0
    assert world.rows("SELECT count(*) AS n FROM relations")[0]["n"] == 0


# ---------- 撤回邮件的发件人 ----------


def mail_relations(window: Window) -> dict[str, object]:
    """发件人那一句撑着一条事实；正文“报价 12.5”那一句撑着一条关系（说法出自谁写的是发件人）。"""
    found_facts, found = [], []
    for line in window.targets:
        if "tran.mai@example.org" in line.text:
            found_facts.append(l3_fact(window, "tran.mai@example.org", SENDER))
        if "报价 12.5" in line.text:
            found.append(relation(BROKER, "客户是", BANK, (line.ref, "报价 12.5"), speaker="Tran Mai"))
    return {"entities": [BROKER, BANK], "facts": found_facts, "relations": found}


def test_withdrawing_the_sender_masks_the_relations_backed_by_that_mail(world: World) -> None:
    delivered(world, two_mails(["东岸证券、海港银行：报价 12.5 美元。", "方案明天发给你。"]))
    write(world, mail_relations).close()
    (row,) = world.rows("SELECT relation_id, speaker, attributed_to FROM relations")
    assert row["attributed_to"] == "Tran Mai" and "tran.mai" in row["speaker"]
    sender_fact = world.rows("SELECT fact_id FROM facts WHERE content = ?", SENDER)[0]["fact_id"]
    withdraw(world.settings, "prod", facts=[sender_fact])
    (row,) = world.rows("SELECT relation_id, speaker, attributed_to FROM relations")
    assert (row["speaker"], row["attributed_to"]) == (MASK, MASK)
    (claim,) = world.rows("SELECT speaker, attributed_to, proofs FROM relation_claims")
    assert (claim["speaker"], claim["attributed_to"]) == (MASK, MASK) and "tran.mai" not in claim["proofs"]


def test_a_hidden_frozen_fact_gets_the_sender_masked_before_it_is_brought_back(world: World) -> None:
    """J4.1 报告第 4 节第 5 条：冻结事实的证据早就全撤回了（藏起来、证据行删光了），之后才按事实撤回这封信的发件人那一句，
    主人再“放回来”：说话人、说法出自谁不能还是发件人的原名。照“证据都撤回了”那条待确认记的包号找到它。"""

    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            if "tran.mai@example.org" in line.text:
                found.append(l3_fact(window, "tran.mai@example.org", SENDER))
            if "报价 12.5" in line.text:
                found.append(l3_fact(window, "报价 12.5", "报价 12.5 美元。", speaker="Tran Mai 的信"))
        return {"entities": [], "facts": found}

    delivered(world)
    write(world, extract).close()
    frozen = world.rows("SELECT fact_id, speaker FROM facts WHERE content = '报价 12.5 美元。'")[0]
    assert "tran.mai" in frozen["speaker"]
    freeze(world.settings, "prod", [frozen["fact_id"]])
    quote = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%报价 12.5%'")[0]["sentence_id"]
    withdraw(world.settings, "prod", sentences=[quote])  # 冻结那条的证据全撤回了：藏起来，证据行删光
    assert world.rows("SELECT count(*) AS n FROM evidence WHERE fact_id = ?", frozen["fact_id"])[0]["n"] == 0
    sender_fact = world.rows("SELECT fact_id FROM facts WHERE content = ?", SENDER)[0]["fact_id"]
    withdraw(world.settings, "prod", facts=[sender_fact])
    review = world.rows("SELECT review_id FROM review_queue WHERE kind = 'other' AND decided_at IS NULL")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "frozen"]) == 0
    (row,) = world.rows("SELECT speaker, attributed_to, withdrawn_at FROM facts WHERE fact_id = ?", frozen["fact_id"])
    assert row["withdrawn_at"] is None and (row["speaker"], row["attributed_to"]) == (MASK, MASK)


def hidden_fact_extract(window: Window) -> dict[str, object]:
    found = []
    for line in window.targets:
        if "tran.mai@example.org" in line.text:
            found.append(l3_fact(window, "tran.mai@example.org", SENDER))
        if "报价 12.5" in line.text:
            found.append(l3_fact(window, "报价 12.5", "报价 12.5 美元。", speaker="Tran Mai 的信"))
    return {"entities": [], "facts": found}


def test_a_hidden_frozen_fact_keeps_the_sender_masked_through_export_and_import(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审阅第 2 条（J4.1 留下的另一半）：冻结事实的证据早就全撤回了（藏着、证据行删光），导出冻结标记以后才按事实撤回
    这封信的发件人那一句，整本重建再导回：补回来的这条说话人、说法出自谁不能还是发件人的原名（照导出文件里“证据都撤回了”
    那条待确认记的包号找，和写账那边一样）。"""
    from javmem.config import Settings

    delivered(world)
    write(world, hidden_fact_extract).close()
    frozen = world.rows("SELECT fact_id FROM facts WHERE content = '报价 12.5 美元。'")[0]["fact_id"]
    freeze(world.settings, "prod", [frozen])
    quote = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%报价 12.5%'")[0]["sentence_id"]
    withdraw(world.settings, "prod", sentences=[quote])
    exported = export_decisions(world.settings, "prod")
    sender_fact = world.rows("SELECT fact_id FROM facts WHERE content = ?", SENDER)[0]["fact_id"]
    withdraw(world.settings, "prod", facts=[sender_fact])
    records = tmp_path / "withdrawals.json"
    records.write_text(json.dumps(export_records(world.settings, "prod"), ensure_ascii=False), encoding="utf-8")
    data = tmp_path / "rebuilt"
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    rebuilt = World(data.resolve(), Settings.from_env())
    assert main(["withdraw", "--library", "prod", "--import", str(records)]) == 0
    delivered(rebuilt)
    write(rebuilt, hidden_fact_extract).close()
    import_decisions(rebuilt.settings, "prod", exported)
    review = rebuilt.rows("SELECT review_id FROM review_queue WHERE kind = 'other' AND decided_at IS NULL")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "frozen"]) == 0
    (row,) = rebuilt.rows("SELECT speaker, attributed_to, withdrawn_at FROM facts WHERE fact_id = ?", frozen)
    assert row["withdrawn_at"] is None and (row["speaker"], row["attributed_to"]) == (MASK, MASK)


# ---------- 打分从账本读关系 ----------


def test_scoring_reads_relations_from_a_ledger_that_writes_them(world: World) -> None:
    from javmem.scoring.results import side_from_result

    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-03", "我：X1 票据从海港银行转走了。"))
    write(world, bank_extract).close()
    side, _ = side_from_result(world.data, ["2026-11-01_2330_desk-bot", "2026-11-03_2330_desk-bot"], library="prod")
    # 一处说法一条（和以前从结果库读的一样细，合成是打分自己合）：两处说法、一段
    first, second = sorted(side.relations.values(), key=lambda one: one.evidence)
    assert (first.relation, first.state, first.certainty, second.state) == ("托管于", "生效", "原话", "已结束")
    assert side.entities[first.subject].name == "X1 票据" and side.entities[first.object].name == "海港银行"
    assert len(first.evidence) == len(second.evidence) == 1 and "从账本读" in str(side.described["关系"])
    claims = {row["claim_id"] for row in world.rows("SELECT claim_id FROM relation_claims")}
    assert set(side.relations) == claims
    # 被替代的记成这一边写的变化：手改一行试（真的替代见 test_relations_newold.py）
    deliver(world, day_packet("2026-11-05", "我：X1 票据在东岸证券。"))

    def two_banks(window: Window) -> dict[str, object]:
        found = bank_extract(window)
        for line in window.targets:
            if "东岸证券" in line.text:
                found["relations"].append(relation(NOTE, "托管于", BROKER, (line.ref, line.text.rstrip("。"))))  # type: ignore[attr-defined]
        found["entities"] = [NOTE, BANK, BROKER]
        return found

    write(world, two_banks).close()
    by_object = {names_of(world)[row["object_entity_id"]]: row["relation_id"]
                 for row in world.rows("SELECT relation_id, object_entity_id FROM relations")}
    old, new = by_object["海港银行"], by_object["东岸证券"]
    world.execute("UPDATE relations SET superseded_by = ?, superseded_at = '2026-11-05T10:30:00+08:00' WHERE relation_id = ?", new, old)
    side, _ = side_from_result(world.data, ["2026-11-01_2330_desk-bot", "2026-11-05_2330_desk-bot"], library="prod")
    claim_of = {row["relation_id"]: row["claim_id"] for row in world.rows(
        "SELECT relation_id, claim_id FROM relation_claims ORDER BY said_from_epoch")}  # 每条关系最后那一处
    newest = world.rows("SELECT claim_id FROM relation_claims WHERE relation_id = ?", new)[0]["claim_id"]
    (change,) = side.changes
    assert (change.before.id, change.after.id, change.at) == (claim_of[old], newest, "2026-11-05T10:30:00+08:00")  # type: ignore[union-attr]
    assert change.evidence == side.relations[newest].evidence[:1]
