"""J4.1：账本里的关系表、关系的说法、关系证据，规则表和留痕表的新字段，旧账本升上来一条不丢。样例都是编的。

关系现在还没有人写（核对是 J4.3，合并和判新旧是 J4.4）：这里直接往表里写，看表本身管得住什么（取值、外键、
一句只记一次、删关系连着删）；再拿处理线真写出来的一本旧版本账本升级，逐表逐行比，升完照常往下写。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import World
from test_write import day_packet, deliver, l3_fact, write

from javmem.ledger import connect, load_migrations, migrate, schema_version, table_names, transaction
from javmem.pipeline.window import Window
from javmem.review import freeze
from javmem.withdraw import withdraw

J41 = next(m.version for m in load_migrations() if m.name == "0020_relations.sql")
PACKET = "2026-11-08_2330_demo-bot"
SENTENCE = f"{PACKET}#1.1.1"
NOW = "2026-11-16T09:00:00+08:00"
CARD_FIELDS = ("kinship", "account", "role", "org_entity_id", "position", "department", "service")  # 定义卡上另加的（D118）
NEW_TABLES = ("relations", "relation_claims", "relation_evidence")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """升到最新的空账本，加一个包、一句原话、三个实体：主人、一张单子、一家银行。"""
    connection = connect(tmp_path / "ledger.db")
    migrate(connection)
    connection.execute(
        "INSERT INTO packets (packet_id, revision, source, env, framework, channel, kind, created_at,"
        " covers_from, covers_to, covers_from_epoch, covers_to_epoch, partitions, privacy, content_hash,"
        " inbox_path, archive_path, state, received_at)"
        " VALUES (?, 1, 'demo-bot', 'prod', 'demo-chat', 'chat', 'daily', 't', 't', 't', 0, 0, '[\"finance\"]',"
        " 'normal', 'hash', 'inbox/x.md', 'archive/x.md', 'current', 't')",
        (PACKET,),
    )
    connection.execute(
        "INSERT INTO raw_sentences (sentence_id, packet_id, revision, seq, section_kind, section_no, line_no,"
        " sentence_no, text, speaker, speaker_role, said_at, said_at_epoch, origin)"
        " VALUES (?, ?, 1, 1, 'raw', 1, 1, 1, '东岸银行的小周以后管那张 X1 单子。', 'demo-bot', 'bot', ?, 0, 'said')",
        (SENTENCE, PACKET, NOW),
    )
    for entity_id, name, kind in (("E1", "主人", "人"), ("E2", "X1 单子", "订单或合同"), ("E3", "东岸银行", "公司或机构")):
        connection.execute(
            "INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (entity_id, name, kind, NOW, NOW),
        )
    yield connection
    connection.close()


def insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def relation(conn: sqlite3.Connection, relation_id: str = "R1", **fields: object) -> None:
    row: dict[str, object] = {
        "relation_id": relation_id, "subject_entity_id": "E2", "relation_type": "托管于", "object_entity_id": "E3",
        "state": "active", "status": "verified", "scope_partitions": '["finance"]', "created_at": NOW, "updated_at": NOW,
    }
    insert(conn, "relations", {**row, **fields})


def claim(conn: sqlite3.Connection, claim_id: str = "w1#abc#0", **fields: object) -> None:
    row: dict[str, object] = {
        "claim_id": claim_id, "relation_id": "R1", "window_id": f"{PACKET}#w1", "cache_key": "abc", "extracted_by": "l3-v4 编的模型",
        "l4_version": "l4-v6", "subject_entity_id": "E2", "relation_type": "托管于", "object_entity_id": "E3",
        "state": "active", "valid_from_precision": "unknown", "valid_to_precision": "unknown", "certainty": "stated",
        "status": "verified", "speaker": "demo-bot", "speaker_role": "bot", "said_from": NOW, "said_from_epoch": 0,
        "said_to": NOW, "said_to_epoch": 0, "proofs": "[]", "created_at": NOW,
    }
    insert(conn, "relation_claims", {**row, **fields})


def evidence(conn: sqlite3.Connection, **fields: object) -> None:
    row: dict[str, object] = {
        "relation_id": "R1", "packet_id": PACKET, "revision": 1, "sentence_id": SENTENCE,
        "quote": "东岸银行的小周以后管那张 X1 单子", "position": '{"section": 1, "line": 1, "sentence": 1}', "created_at": NOW,
    }
    insert(conn, "relation_evidence", {**row, **fields})


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ---------- 表和字段 ----------


def test_card_fields_and_both_speakers_have_a_place(conn: sqlite3.Connection) -> None:
    """D118 的字段、两种说话人（J3.9 报告第 4 节第 8 条）：关系和关系的说法都有；事实和事实的说法也加上了说法出自谁。"""
    for table in ("relations", "relation_claims"):
        names = set(columns(conn, table))
        assert set(CARD_FIELDS) <= names
        assert {"speaker", "speaker_role", "attributed_to", "pause_condition", "valid_from_basis", "valid_to_basis"} <= names
    for table in ("facts", "fact_claims"):
        assert {"speaker", "attributed_to"} <= set(columns(conn, table))


def test_a_relation_keeps_what_it_was_given(conn: sqlite3.Connection) -> None:
    relation(
        conn, relation_type="负责", subject_entity_id="E1", object_entity_id="E2", role="客户经理", org_entity_id="E3",
        valid_from="2026-10", valid_from_precision="month", valid_to="2026-11-05", valid_to_precision="day",
        valid_to_basis="trade_date", state="ended", speaker="demo-bot", speaker_role="bot", attributed_to="主人",
        frozen=1, merge_key="k1", group_key="g1", extracted_by="l3-v4 编的模型", rule_version="l4-v6 l5-v8",
    )
    row = conn.execute("SELECT * FROM relations WHERE relation_id = 'R1'").fetchone()
    assert (row["role"], row["org_entity_id"], row["kinship"], row["account"]) == ("客户经理", "E3", None, None)
    assert (row["valid_from"], row["valid_from_precision"], row["valid_from_basis"]) == ("2026-10", "month", None)
    assert (row["valid_to"], row["valid_to_precision"], row["valid_to_basis"]) == ("2026-11-05", "day", "trade_date")
    assert (row["speaker"], row["attributed_to"], row["frozen"]) == ("demo-bot", "主人", 1)
    assert (row["notes"], row["sources"], row["origins"], row["secondhand"], row["stated"]) == ("[]", "[]", "[]", 0, 0)


def test_the_relation_list_lives_in_the_config_not_in_the_table(conn: sqlite3.Connection) -> None:
    """关系清单是配置的（query-api 5.4）：以后加一种（比如“汇报给”）不用重建表。"""
    relation(conn, relation_type="汇报给", subject_entity_id="E1", object_entity_id="E3")
    assert conn.execute("SELECT relation_type FROM relations").fetchone()[0] == "汇报给"


def test_a_paused_relation_says_what_it_waits_for(conn: sqlite3.Connection) -> None:
    relation(conn, state="paused", pause_condition="等结单出来再看")
    claim(conn, state="paused", pause_condition="等结单出来再看")
    assert conn.execute("SELECT pause_condition FROM relations").fetchone()[0] == "等结单出来再看"


BAD_VALUES = [
    ("relations", {"state": "生效"}),  # L3 的中文写进来之前换成 active、paused、ended
    ("relations", {"state": "superseded"}),  # 被替代的看 superseded_by，状态是 ended
    ("relations", {"pause_condition": "等结单出来再看"}),  # 不是暂停的不写暂停条件
    ("relations", {"valid_from_precision": "week"}),
    ("relations", {"valid_to_precision": None}),
    ("relations", {"valid_from_basis": "settlement_date"}),  # 交割日是空的那一种
    ("relations", {"valid_to_basis": "约"}),
    ("relations", {"status": "stated"}),
    ("relations", {"pending_reason": "not_checked"}),
    ("relations", {"speaker_role": "system"}),
    ("relations", {"frozen": 2}),
    ("relations", {"scope_partitions": None}),
    ("relation_claims", {"certainty": "sure"}),
    ("relation_claims", {"status": "pending"}),  # 待确认是关系一级的事，一处说法只有核对过、推断
    ("relation_claims", {"state": "暂停"}),
    ("relation_claims", {"pause_condition": "等结单出来再看"}),
    ("relation_claims", {"valid_from_basis": "approximately"}),
    ("relation_claims", {"speaker": None}),
    ("relation_evidence", {"origin": "quoted"}),
    ("relation_evidence", {"speaker_role": "system"}),
    ("relation_evidence", {"quote": None}),
]


@pytest.mark.parametrize(("table", "fields"), BAD_VALUES, ids=[f"{t}-{next(iter(f))}-{i}" for i, (t, f) in enumerate(BAD_VALUES)])
def test_values_outside_the_lists_are_refused(conn: sqlite3.Connection, table: str, fields: dict[str, object]) -> None:
    if table != "relations":
        relation(conn)
    write_row = {"relations": relation, "relation_claims": claim, "relation_evidence": evidence}[table]
    with pytest.raises(sqlite3.IntegrityError):
        write_row(conn, **fields)


@pytest.mark.parametrize(
    "fields", [{"subject_entity_id": "E9"}, {"object_entity_id": "E9"}, {"org_entity_id": "E9"}, {"superseded_by": "R9"}]
)
def test_a_relation_points_at_things_that_exist(conn: sqlite3.Connection, fields: dict[str, object]) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        relation(conn, **fields)


def test_claims_and_evidence_point_at_things_that_exist(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        evidence(conn)  # 还没有这条关系
    with pytest.raises(sqlite3.IntegrityError):
        claim(conn)
    relation(conn)
    with pytest.raises(sqlite3.IntegrityError):
        evidence(conn, packet_id="2026-11-09_2330_demo-bot")  # 包登记里没有
    for fields in ({"subject_entity_id": "E9"}, {"object_entity_id": "E9"}, {"org_entity_id": "E9"}):
        with pytest.raises(sqlite3.IntegrityError):
            claim(conn, **fields)


def test_one_sentence_counts_once_for_a_relation(conn: sqlite3.Connection) -> None:
    relation(conn, merge_key="k1")
    evidence(conn)
    with pytest.raises(sqlite3.IntegrityError):
        evidence(conn)
    with pytest.raises(sqlite3.IntegrityError):
        relation(conn, "R2", merge_key="k1")  # 同一条关系的键只有一条
    relation(conn, "R2", subject_entity_id="E1", relation_type="持有", object_entity_id="E2")
    evidence(conn, relation_id="R2")  # 同一句撑着另一条关系，照记
    assert count(conn, "relation_evidence") == 2


def test_deleting_a_relation_takes_its_claims_and_evidence_along(conn: sqlite3.Connection) -> None:
    relation(conn)
    claim(conn)
    evidence(conn)
    conn.execute("DELETE FROM relations WHERE relation_id = 'R1'")
    assert (count(conn, "relation_claims"), count(conn, "relation_evidence")) == (0, 0)


def test_what_a_relation_points_at_cannot_be_deleted_from_under_it(conn: sqlite3.Connection) -> None:
    """写关系的（J4.4）删没用了的实体、删关系以前，要先看关系表：两头、org、替代它的关系还被指着的删不掉。"""
    relation(conn, "R2", relation_type="负责", subject_entity_id="E1", object_entity_id="E2", role="客户经理", org_entity_id="E3")
    relation(conn, "R1", state="ended", superseded_by="R2")
    for entity_id in ("E1", "E2", "E3"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM entities WHERE entity_id = ?", (entity_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM relations WHERE relation_id = 'R2'")


def test_rules_and_audit_take_the_new_fields_and_old_inserts_still_work(conn: sqlite3.Connection) -> None:
    positive = {"kind": "positive", "sentence_id": SENTENCE, "packet_id": PACKET, "revision": 1, "sentence_hash": "0" * 64}
    examples = json.dumps([positive, {"kind": "negative", "ref": "编的例句"}], ensure_ascii=False)
    conn.execute(
        "INSERT INTO rules (rule_id, version, layer, from_questions, description, created_at, from_data, examples, decided_by)"
        " VALUES ('R-demo', 'r-v1', 'L4', '[]', '编的规则', ?, '[\"记忆包\"]', ?, '主人')",
        (NOW, examples),
    )
    conn.execute(  # 写账登记规则还是原来的写法：新的三列用默认的
        "INSERT INTO rules (rule_id, version, layer, from_questions, description, created_at) VALUES ('G1', 'l4-v6', 'L4', '[]', '引文', ?)",
        (NOW,),
    )
    rows = {row["rule_id"]: row for row in conn.execute("SELECT * FROM rules")}
    assert json.loads(rows["R-demo"]["examples"])[0] == positive
    assert (rows["G1"]["from_data"], rows["G1"]["examples"], rows["G1"]["decided_by"]) == ("[]", "[]", None)

    conn.execute(
        "INSERT INTO audit_log (check_kind, item_kind, item_id, trusted, basis, probability, created_at, tier, judge)"
        " VALUES ('projection', 'relation', 'R1', 'supported', ?, 0.9, ?, 'model', '编的模型@auto c1')",
        (json.dumps([SENTENCE]), NOW),
    )
    for tier in ("rule", "judge", "owner"):  # 02 p5 的四级：规则、判断模型、大模型读原文、主人
        conn.execute(
            "INSERT INTO audit_log (check_kind, item_kind, item_id, trusted, created_at, tier) VALUES ('projection', 'relation', 'R1', 'x', ?, ?)",
            (NOW, tier),
        )
    with pytest.raises(sqlite3.IntegrityError):  # 判断模型是哪一家写在 judge 里，不当一级
        conn.execute(
            "INSERT INTO audit_log (check_kind, item_kind, item_id, trusted, created_at, tier) VALUES ('projection', 'relation', 'R1', 'x', ?, 'jev')",
            (NOW,),
        )
    assert [tuple(row) for row in conn.execute("SELECT tier, judge FROM audit_log ORDER BY audit_id")] == [
        ("model", "编的模型@auto c1"), ("rule", None), ("judge", None), ("owner", None)
    ]


# ---------- 旧账本升级：处理线真写出来的一本 0019 的账本 ----------

DATES = ("11-15", "11-20", "11-25", "11-28")
OLD_PACKETS = [
    day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
    day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"),
    day_packet("2026-11-05", "desk-bot：你说过越南那批订单再改到 11-25 出货。"),
]


def said_extract(window: Window) -> dict[str, object]:
    """出货日期一条一条抽，L3 写的“这条说法出自谁”都是主人（Bot 转述的也是）；11-15 那句另有一条引文对不上的，L4 隔离。"""
    found = []
    for line in window.targets:
        for date_text in DATES:
            if date_text in line.text:
                found.append(
                    l3_fact(window, line.text.rstrip("。"), f"越南那批订单出货日期是 {date_text}", subject="e1",
                            attribute="出货日期", value=date_text, speaker="主人")
                )
        if "11-15" in line.text:
            found.append(l3_fact(window, line.text.rstrip("。"), "越南那批订单下周一定出货",
                                 evidence=[{"ref": line.ref, "quote": "下周一定出货"}], speaker="主人"))
    entity = {"key": "e1", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}
    return {"entities": [entity], "facts": found}


def transplant(source: Path, target: Path, version: int) -> sqlite3.Connection:
    """在 target 建一本 version 版的账本，把 source 里这一版有的表、有的列原样搬过来（原文的全文索引由触发器跟着建）。"""
    conn = connect(target)
    migrate(conn, load_migrations()[:version])
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ATTACH DATABASE ? AS src", (str(source),))
    with transaction(conn):
        for table in table_names(conn):
            names = ", ".join(columns(conn, table))
            conn.execute(f"INSERT INTO main.{table} ({names}) SELECT {names} FROM src.{table}")
    conn.execute("DETACH DATABASE src")
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    return conn


def snapshot(conn: sqlite3.Connection) -> dict[str, list[dict[str, object]]]:
    return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in table_names(conn)}


def hits(conn: sqlite3.Connection, word: str) -> list[int]:
    return [row[0] for row in conn.execute("SELECT rowid FROM raw_fts WHERE raw_fts MATCH ? ORDER BY rowid", (f'"{word}"',))]


def test_an_old_ledger_written_by_the_pipeline_upgrades_without_losing_a_row(world: World, tmp_path: Path) -> None:
    """包、原文、实体、别名、事实、说法、证据、隔离、待确认、规则、向量、撤回的记录……都在的一本账，退回 0019 的样子，
    再照常打开（自动升级）：旧表每一行每一列都一样，新列是空的或默认的，新表是空的；升完照常收包写账。"""
    deliver(world, *OLD_PACKETS)
    write(world, said_extract).close()
    current = {row["value"]: row["fact_id"] for row in world.rows("SELECT value, fact_id FROM facts WHERE value IS NOT NULL")}
    freeze(world.settings, "prod", [current["11-15"]])  # 11-20、11-25 和它冲突，进待确认
    withdraw(world.settings, "prod", sentences=["2026-11-03_2330_desk-bot#1.1.1"])  # 单条撤回，包改回排队

    db = world.settings.library("prod").db_path
    source = tmp_path / "written.db"
    for suffix in ("", "-wal", "-shm"):
        if db.with_name(db.name + suffix).exists():
            db.with_name(db.name + suffix).rename(source.with_name(source.name + suffix))
    old = transplant(source, db, J41 - 1)
    assert schema_version(old) == J41 - 1 and not set(NEW_TABLES) & set(table_names(old))
    before = snapshot(old)
    old_columns = {table: set(columns(old, table)) for table in before}
    found_before = hits(old, "越南那批订单")
    old.close()
    for table in ("packets", "raw_sentences", "entities", "aliases", "facts", "fact_claims", "evidence", "fact_entities",
                  "quarantine", "review_queue", "rules", "fact_vectors", "withdrawn_sentences", "withdrawn_texts"):
        assert before[table], f"{table} 是空的，这个测试就白比了"

    upgraded = world.ledger()  # 照常打开就升级
    try:
        assert schema_version(upgraded) == load_migrations()[-1].version
        after = snapshot(upgraded)
        for table, rows in before.items():
            kept = [{name: value for name, value in row.items() if name in old_columns[table]} for row in after[table]]
            assert kept == rows, table
        for table in NEW_TABLES:
            assert after[table] == []
        assert {row["attributed_to"] for row in after["facts"] + after["fact_claims"]} == {None}  # 以前写的没有，整本重建才有
        assert {(row["from_data"], row["examples"], row["decided_by"]) for row in after["rules"]} == {("[]", "[]", None)}
        assert hits(upgraded, "越南那批订单") == found_before and found_before
        assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        upgraded.close()

    # 升完照常往下写：新的说法有说法出自谁，以前写的不重写，还是空的
    deliver(world, day_packet("2026-11-07", "desk-bot：你说过越南那批订单最后定在 11-28 出货。"))
    conn = write(world, said_extract)
    try:
        rows = conn.execute("SELECT window_id, speaker, attributed_to FROM fact_claims ORDER BY claim_id").fetchall()
        new = [(row["speaker"], row["attributed_to"]) for row in rows if row["window_id"].startswith("2026-11-07")]
        assert new == [("desk-bot", "主人")]
        assert {row["attributed_to"] for row in rows if row["window_id"].startswith("2026-11-01")} == {None}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
