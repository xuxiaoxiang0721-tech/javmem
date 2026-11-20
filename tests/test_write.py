"""L5 合并和替代、L6 写账、向量：合并、新旧、晚到的旧消息、同一时刻、多值属性、可见范围、冻结、实体对齐、
重写不变、隔离、包的状态、整本重建两次逐表比。样例都是编的，向量用假的。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from helpers import CHAT_PACKET, MAIL_PACKET, World, build_packet

from javmem.cli import main
from javmem.embedding import EMBEDDING_DIM, FakeEmbeddings, unpack_vector
from javmem.ledger import open_ledger, table_names, transaction
from javmem.llm import FakeModel
from javmem.pipeline.batch import load_queued
from javmem.pipeline.extract import map_output, prepare
from javmem.pipeline.merge import L5_VERSION, MULTI_VALUED, GroupFact, key_text, succession, usable_alias
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.verify import NO_RESULT, Checked, verify_window
from javmem.pipeline.window import Window, build_windows
from javmem.pipeline.write import LedgerWriter, ledger_summary, run_write
from javmem.redaction import default_patterns

PATTERNS = default_patterns()
Extract = Callable[[Window], Mapping[str, object] | None]


def day_packet(day: str, *lines: str, source: str = "desk-bot", domain: str = "[business]", hour: int = 10) -> str:
    """某一天的一个对话包：lines 每个是一句话 `说话人：原文`，从 hour:30 起每句隔一分钟。"""
    body = f"## 原话 · {day} {hour:02d}:30–{hour:02d}:59\n"
    body += "".join(f"- [{hour:02d}:{30 + index:02d}] {line}\n" for index, line in enumerate(lines))
    return build_packet(
        source=source,
        stamp=f"{day}_2330",
        body=body,
        created_at=f"{day}T23:30:00+08:00",
        covers_from=f"{day}T00:00:00+08:00",
        covers_to=f"{day}T23:30:00+08:00",
        domain=domain,
    )


def ref_of(window: Window, phrase: str) -> str:
    return next(line.ref for line in window.lines if line.target and phrase in line.text)


def l3_fact(window: Window, phrase: str, text: str, **fields: object) -> dict[str, object]:
    item: dict[str, object] = {
        "text": text,
        "subject": None,
        "attribute": None,
        "value": None,
        "occurred_at": None,
        "time_precision": "unknown",
        "certainty": "stated",
        "entities": [],
        "evidence": [{"ref": ref_of(window, phrase), "quote": phrase}],
    }
    item.update(fields)
    if item["subject"] is not None and not item["entities"]:
        item["entities"] = [{"key": item["subject"], "role": "subject"}]
    return item


ZHOU = {"key": "e1", "mention": "老周", "name": "周建国", "type": "人"}


def checked_items(world: World, extract: Extract, library: str = "prod") -> list[Checked]:
    batch = load_queued(world.settings, library)
    items = []
    for window, rendered, key in prepare(build_windows(batch.packets), FakeModel(), PATTERNS):
        raw = extract(window)
        if raw is None:
            items.append(Checked(window, rendered, key, None, None, NO_RESULT))
            continue
        extraction = map_output(raw, window, rendered.redacted_refs)
        items.append(Checked(window, rendered, key, extraction, verify_window(window, rendered, extraction)))
    return items


def write(
    world: World,
    extract: Extract,
    *,
    library: str = "prod",
    tmp: Path | None = None,
    order=None,
    embedder: FakeEmbeddings | None = None,
    vectors: Path | None = None,
) -> sqlite3.Connection:
    """取包、按 extract 造 L3 的结果、跑 L4、写账。返回账本的连接。"""
    items = checked_items(world, extract, library)
    if order is not None:
        items = order(items)
    conn = open_ledger(world.settings.library(library))
    embedder = embedder or FakeEmbeddings()
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model=embedder.name)
    batch = load_queued(world.settings, library)
    vector_dir = vectors or (tmp or world.data) / "vectors"
    with ResultStore((tmp or world.data) / "l3") as store, VectorCache(vector_dir) as cache:
        run_write(
            items,
            writer,
            packets=[(packet.packet_id, packet.header.revision) for packet in batch.packets],
            store=store,
            cache=cache,
            embedder=embedder,
            sleep=lambda _: None,
        )
    return conn


def facts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM facts ORDER BY as_of_epoch, fact_id").fetchall()


# ---------- 合并 ----------


def deliver(world: World, *packets: str, folder: str = "inbox/desk-bot") -> None:
    for packet in packets:
        world.drop(folder, packet)
    world.intake()


def zhou_extract(window: Window) -> dict[str, object]:
    found = []
    for line in window.targets:
        if "越南" in line.text:
            value = "越南那批订单"
            found.append(l3_fact(window, line.text.rstrip("。"), "周建国负责越南那批订单", subject="e1", attribute="负责的订单", value=value))
    return {"entities": [ZHOU], "facts": found}


def test_same_fact_from_two_packets_is_merged_with_both_evidence(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：老周负责越南那批订单。"), day_packet("2026-11-05", "我：还是老周负责越南那批订单。"))
    conn = write(world, zhou_extract)
    rows = facts(conn)
    assert len(rows) == 1
    fact = rows[0]
    assert fact["content"] == "周建国负责越南那批订单"
    assert fact["status"] == "verified" and fact["superseded_by"] is None
    assert fact["as_of"].startswith("2026-11-05")  # 最后一次有人说
    assert fact["speaker"] == "我" and fact["speaker_role"] == "owner"
    assert json.loads(fact["scope_partitions"]) == ["business"]
    assert conn.execute("SELECT count(*) FROM fact_claims").fetchone()[0] == 2
    evidence = conn.execute("SELECT sentence_id, quote, position FROM evidence WHERE fact_id = ? ORDER BY evidence_id", (fact["fact_id"],)).fetchall()
    assert [row["sentence_id"].split("#")[0] for row in evidence] == ["2026-11-01_2330_desk-bot", "2026-11-05_2330_desk-bot"]
    assert json.loads(evidence[0]["position"]) == {"section": 1, "line": 1, "sentence": 1}
    assert evidence[1]["quote"] == "还是老周负责越南那批订单"
    summary = ledger_summary(conn)
    assert (summary["facts"], summary["claims"], summary["claims_merged"]) == (1, 2, 1)


def test_different_scopes_are_not_merged(world: World) -> None:
    deliver(
        world,
        day_packet("2026-11-01", "我：老周负责越南那批订单。"),
        day_packet("2026-11-02", "我：老周负责越南那批订单。", domain="[business, research]"),
    )
    conn = write(world, zhou_extract)
    rows = facts(conn)
    assert len(rows) == 2
    assert {row["scope_partitions"] for row in rows} == {'["business"]', '["business", "research"]'}
    assert all(row["superseded_by"] is None for row in rows)


# ---------- 说话人分两列（J4.1） ----------


def test_who_the_claim_is_from_sits_next_to_who_said_the_evidence(world: World) -> None:
    """Bot 转述主人的话：证据那一句是 Bot 说的（speaker），L3 写这条说法出自主人（attributed_to）。事实取最早的那一处，
    后来的另一处写的出自谁不同，也不改。"""

    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            if "越南" in line.text:
                who = "desk-bot" if "还在" in line.text else "主人"
                found.append(l3_fact(window, line.text.rstrip("。"), "周建国负责越南那批订单", subject="e1",
                                     attribute="负责的订单", value="越南那批订单", speaker=who))
        return {"entities": [ZHOU], "facts": found}

    deliver(world, day_packet("2026-11-01", "desk-bot：你说过老周负责越南那批订单。"),
            day_packet("2026-11-05", "desk-bot：老周还在负责越南那批订单。"))
    conn = write(world, extract)
    [fact] = facts(conn)
    assert (fact["speaker"], fact["speaker_role"], fact["attributed_to"]) == ("desk-bot", "bot", "主人")
    claims = conn.execute("SELECT speaker, attributed_to FROM fact_claims ORDER BY said_from_epoch").fetchall()
    assert [tuple(row) for row in claims] == [("desk-bot", "主人"), ("desk-bot", "desk-bot")]
    from javmem.export import facts as export_facts

    assert [(item["speaker"], item["attributed_to"]) for item in export_facts(conn)] == [("desk-bot", "主人")]


@pytest.mark.parametrize("speaker", [None, "", "  "], ids=["old-shape", "empty", "blank"])
def test_results_without_who_it_is_from_leave_it_empty(world: World, speaker: str | None) -> None:
    """旧样子的结果（l3-v1，事实没有 speaker）、写了空的：说法出自谁是空的，说话人照旧是证据那一句的。"""

    def extract(window: Window) -> dict[str, object]:
        found = ship_extract(window)
        for fact in found["facts"]:  # type: ignore[union-attr]
            if speaker is not None:
                fact["speaker"] = speaker
        return found

    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    conn = write(world, extract)
    [fact] = facts(conn)
    assert (fact["speaker"], fact["attributed_to"]) == ("我", None)
    assert conn.execute("SELECT attributed_to FROM fact_claims").fetchone()[0] is None


# ---------- 新旧 ----------


def ship_extract(window: Window) -> dict[str, object]:
    found = []
    for line in window.targets:
        for date_text in ("11-15", "11-20", "11-25"):
            if date_text in line.text:
                found.append(
                    l3_fact(window, line.text.rstrip("。"), f"越南那批订单出货日期是 {date_text}",
                            subject="e1", attribute="出货日期", value=date_text)
                )
    entity = {"key": "e1", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}
    return {"entities": [entity], "facts": found}


def test_newer_value_supersedes_the_older_one(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    conn = write(world, ship_extract)
    old, new = facts(conn)
    assert old["value"] == "11-15" and new["value"] == "11-20"
    assert old["superseded_by"] == new["fact_id"] and old["superseded_at"] == new["as_of"]
    assert new["superseded_by"] is None
    assert old["group_key"] == new["group_key"] is not None


def test_late_old_message_only_adds_history_and_order_does_not_matter(world: World, tmp_path: Path) -> None:
    deliver(world, day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    write(world, ship_extract)
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))  # 旧的晚到，下一批才写
    conn = write(world, ship_extract)
    old, new = facts(conn)
    assert (old["value"], old["superseded_by"]) == ("11-15", new["fact_id"])
    assert new["superseded_by"] is None


def test_restating_an_old_value_makes_it_current_again(world: World) -> None:
    deliver(
        world,
        day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
        day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"),
        day_packet("2026-11-05", "我：算了，越南那批订单还是 11-15 出货。"),
    )
    conn = write(world, ship_extract)
    rows = {row["value"]: row for row in facts(conn)}
    assert rows["11-20"]["superseded_by"] == rows["11-15"]["fact_id"]
    assert rows["11-15"]["superseded_by"] is None
    assert rows["11-15"]["as_of"].startswith("2026-11-05")


def test_values_said_at_the_same_moment_do_not_replace_each_other(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "desk-bot：越南那批订单两个方案：11-15 出货，或者 11-20 出货。"))
    conn = write(world, ship_extract)
    assert [row["superseded_by"] for row in facts(conn)] == [None, None]


def test_multi_valued_attribute_is_not_superseded(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        found = []
        for line in window.targets:
            for wish in ("不喝咖啡", "早上跑步"):
                if wish in line.text:
                    found.append(l3_fact(window, wish, f"周建国的偏好：{wish}", subject="e1", attribute="生活偏好", value=wish))
        return {"entities": [ZHOU], "facts": found}

    deliver(world, day_packet("2026-11-01", "我：老周不喝咖啡。"), day_packet("2026-11-03", "我：老周早上跑步。"))
    conn = write(world, extract)
    rows = facts(conn)
    assert len(rows) == 2 and all(row["superseded_by"] is None and row["group_key"] is None for row in rows)
    assert MULTI_VALUED.search("报价") and not MULTI_VALUED.search("出货日期")


def test_frozen_fact_is_not_replaced_and_the_newer_one_waits_for_review(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    conn = write(world, ship_extract)
    with transaction(conn):
        conn.execute("UPDATE facts SET frozen = 1")  # J2.12 的管理命令以后做这一步
    deliver(world, day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    conn = write(world, ship_extract)
    old, new = facts(conn)
    assert old["frozen"] == 1 and old["superseded_by"] is None and old["status"] == "verified"
    assert (new["status"], new["pending_reason"], new["superseded_by"]) == ("pending", "frozen_conflict", None)
    reviews = conn.execute("SELECT kind, claims FROM review_queue").fetchall()
    assert [(row["kind"], json.loads(row["claims"])) for row in reviews] == [
        ("frozen_conflict", {"frozen": old["fact_id"], "new": new["fact_id"]})
    ]


def _said(fact_id: str, *moments: int, frozen: bool = False) -> GroupFact:
    return GroupFact(fact_id, tuple((moment, f"t{moment}") for moment in moments), frozen)


def test_succession_rules() -> None:
    result = succession([_said("F1", 100), _said("F2", 200), _said("F3", 200)])
    assert result.superseded == {"F1": ("F2", "t200")} and result.conflicts == []  # 同一时刻说的两条都现行
    frozen = succession([_said("F1", 100, frozen=True), _said("F2", 200), _said("F3", 300)])
    assert frozen.superseded == {"F2": ("F3", "t300")}
    assert frozen.conflicts == [("F1", "F2"), ("F1", "F3")]
    # 先说 F1，再说 F2，后来又说回 F1：F2 被 F1 替代，替代的时间是又说 F1 的那一句
    again = succession([_said("F1", 100, 300), _said("F2", 200)])
    assert again.superseded == {"F2": ("F1", "t300")}


# ---------- 实体 ----------


def test_entities_are_aligned_by_name_and_alias(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        text = window.targets[0].text.rstrip("。")
        # 第一包 L3 认出“老周”就是周建国；第二包只看得到“老周”
        name = "周建国" if "周建国" in text else "老周"
        entities = [{"key": "e1", "mention": "老周", "name": name, "type": "人"}]
        return {"entities": entities, "facts": [l3_fact(window, text, text, subject="e1")]}

    deliver(
        world,
        day_packet("2026-11-01", "我：周建国，大家叫他老周，下周去越南。"),
        day_packet("2026-11-03", "我：老周已经到越南了。"),
    )
    conn = write(world, extract)
    entities = conn.execute("SELECT * FROM entities").fetchall()
    assert [row["canonical_name"] for row in entities] == ["周建国"]  # 第二包的“老周”按别名对上了
    aliases = {row["alias"] for row in conn.execute("SELECT alias FROM aliases")}
    assert aliases == {"周建国", "老周"}
    assert all(row["subject_entity_id"] == entities[0]["entity_id"] for row in facts(conn))
    assert conn.execute("SELECT count(*) FROM evidence WHERE alias_id IS NOT NULL").fetchone()[0] >= 2


def test_alias_matching_two_entities_creates_a_new_one_for_review(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        text = window.targets[0].text
        if "周建国" in text:
            entity = {"key": "e1", "mention": "老周", "name": "周建国", "type": "人"}
        elif "周立新" in text:
            entity = {"key": "e1", "mention": "老周", "name": "周立新", "type": "人"}
        else:
            entity = {"key": "e1", "mention": "老周", "name": "老周", "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}

    deliver(
        world,
        day_packet("2026-11-01", "我：周建国就是老周，管越南。"),
        day_packet("2026-11-02", "我：周立新也叫老周，管泰国。"),
        day_packet("2026-11-03", "我：老周明天回来。"),
    )
    conn = write(world, extract)
    assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 3
    review = conn.execute("SELECT kind, claims FROM review_queue").fetchone()
    claims = json.loads(review["claims"])
    assert review["kind"] == "ambiguous_entity" and claims["name"] == "老周" and len(claims["candidates"]) == 2
    pending = conn.execute("SELECT content, status, pending_reason FROM facts WHERE status = 'pending'").fetchall()
    assert [(row["content"], row["pending_reason"]) for row in pending] == [("老周明天回来", "ambiguous_entity")]


def test_pronouns_and_one_character_names_are_not_aliases() -> None:
    assert not usable_alias("他") and not usable_alias("我们") and not usable_alias("客户") and not usable_alias("周")
    assert usable_alias("老周") and usable_alias("Tran Mai")
    assert key_text("Tran  Mai。") == key_text("tran mai") == "tranmai"


# ---------- 隔离、重写、包的状态 ----------


def test_quarantine_rows_for_failed_facts_dropped_evidence_and_bad_entities(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        good = l3_fact(window, "单价 12.5 美元", "单价 12.5 美元")
        good["evidence"].append({"ref": good["evidence"][0]["ref"], "quote": "单价涨了"})
        return {
            "entities": [ZHOU, dict(ZHOU)],
            "facts": [good, l3_fact(window, "单价 12.5 美元", "单价 13.5 美元")],
        }

    deliver(world, day_packet("2026-11-01", "desk-bot：单价 12.5 美元。"))
    conn = write(world, extract)
    rows = conn.execute("SELECT item_kind, reason, rule_id, rule_version, window_id, payload FROM quarantine ORDER BY item_kind").fetchall()
    assert [(row["item_kind"], row["reason"], row["rule_id"]) for row in rows] == [
        ("entity", "L3:duplicate_key", "L3"),
        ("evidence", "G1:quote_not_found", "G1"),
        ("fact", "G2:number_mismatch", "G2"),
    ]
    assert all(row["window_id"] and row["rule_version"] for row in rows)
    assert json.loads(rows[2]["payload"])["fact"]["text"] == "单价 13.5 美元"
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
    # 事实的 L3、G1 到 G7，加上关系的 R1 到 R6（J4.3）和判断 C；关系的带着谁定的
    assert conn.execute("SELECT count(*) FROM rules WHERE layer = 'L4'").fetchone()[0] == 8 + 6 + 1
    decided = dict(conn.execute(
        "SELECT rule_id, decided_by FROM rules WHERE layer = 'L4' AND (rule_id LIKE 'R%' OR rule_id = 'C')").fetchall())
    assert sorted(decided) == ["C", "R1", "R2", "R3", "R4", "R5", "R6"] and all(decided.values())
    assert conn.execute("SELECT version FROM rules WHERE rule_id = 'L5-merge'").fetchone()[0] == L5_VERSION


WALL_CLOCK = {"created_at", "updated_at", "received_at", "indexed_at", "first_seen_at", "last_seen_at", "started_at", "finished_at",
              "queued_at"}


SURROGATE = {"evidence_id", "quarantine_id", "review_id"}  # 自增的编号，没有别处引用；重写时证据删了重插，编号会变


def dump(conn: sqlite3.Connection, *, surrogate: bool = True) -> dict[str, list[tuple[object, ...]]]:
    """每张表的内容，按写进去的时间的那几列不算。packets 的 superseded_at 也是写的时间。surrogate 是 False 时
    自增的编号也不算。"""
    tables = {}
    for table in table_names(conn):
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        skip = WALL_CLOCK | (set() if surrogate else SURROGATE)
        keep = [name for name in columns if name not in skip and not (table == "packets" and name == "superseded_at")]
        rows = conn.execute(f"SELECT {', '.join(keep)} FROM {table}").fetchall()
        tables[table] = sorted((tuple(row) for row in rows), key=repr)  # 有空值，按写法排
    return tables


def test_rewriting_a_window_changes_nothing(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    items = checked_items(world, ship_extract)
    conn = write(world, ship_extract)
    before = dump(conn, surrogate=False)
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
    with VectorCache(world.data / "vectors") as cache:
        for item in items:
            writer.write_window(item, cache.get("fake-embedding", writer.texts_needed(item)))
    assert dump(conn, surrogate=False) == before


def test_packets_are_done_or_waiting(world: World) -> None:
    deliver(
        world,
        day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
        day_packet("2026-11-02", "我：越南那批订单改到 11-20 出货。"),
        day_packet("2026-11-03", 'desk-bot：{"type": "system_event", "name": "rename"}'),  # 整包不进窗口
    )
    conn = write(world, lambda window: ship_extract(window) if "11-15" in window.targets[0].text else None)
    states = dict(conn.execute("SELECT packet_id, extraction FROM packets"))
    assert states == {
        "2026-11-01_2330_desk-bot": "done",
        "2026-11-02_2330_desk-bot": "queued",  # 还没结果，下次接着来
        "2026-11-03_2330_desk-bot": "done",  # 没有窗口，没什么可抽
    }


def test_vectors_are_2560_float32_and_cached(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    conn = write(world, ship_extract)
    row = conn.execute("SELECT f.content, v.* FROM fact_vectors AS v JOIN facts AS f USING (fact_id)").fetchone()
    assert (row["dim"], len(row["vector"]), row["model"]) == (EMBEDDING_DIM, 4 * EMBEDDING_DIM, "fake-embedding")
    values = unpack_vector(row["vector"])
    assert abs(sum(value * value for value in values) - 1) < 1e-3
    with VectorCache(world.data / "vectors") as cache:
        assert cache.get("fake-embedding", [row["content"]])[row["content"]] == row["vector"]


def rebuild(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch, packets: list[str]) -> dict:
    from javmem.config import Settings

    data = tmp_path / name
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    world = World(data.resolve(), Settings.from_env())
    deliver(world, *packets)
    conn = write(world, ship_extract)
    return dump(conn)


def test_rebuilding_the_whole_ledger_twice_gives_the_same_tables(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packets = [
        day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
        day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"),
        day_packet("2026-11-04", "desk-bot：越南那批订单两个方案：11-20 出货，或者 11-25 出货。"),
    ]
    first = rebuild(tmp_path, "one", monkeypatch, packets)
    second = rebuild(tmp_path, "two", monkeypatch, packets)
    assert first == second
    assert first["facts"] and first["fact_vectors"] and first["evidence"]


def test_write_command_end_to_end(world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.intake()
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 2  # 还没抽，结果库都没有
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    summary_file = tmp_path / "write.json"
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings", "--summary", str(summary_file)]) == 0
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    assert summary["this_run"]["windows_written"] == 2
    assert summary["packets_by_extraction"] == {"done": 2}
    assert summary["facts"] == summary["vectors"] > 0 and summary["vector_dims"] == {"2560": summary["vectors"]}
    assert "越南" not in summary_file.read_text(encoding="utf-8")
    # 包都 done 了，再跑一遍什么都不写
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert "写了 0" in capsys.readouterr().out
