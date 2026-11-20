"""账本和迁移：第一版 11 张表，J4.1 加上关系表和关系证据表一共 13 张；字段对得上 01 p3–4；迁移留好路。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from javmem.ledger import (
    LEDGER_TABLES,
    LedgerError,
    Migration,
    connect,
    connect_readonly,
    load_migrations,
    migrate,
    schema_version,
    table_names,
    transaction,
)

# 01 p3–4 每张表“存什么”，对到列名。少一个就说明和设计稿对不上了。
DESIGN_FIELDS = {
    "packets": {
        "packet_id": "包号",
        "source": "来源 Bot",
        "env": "环境",
        "covers_from": "覆盖时段",
        "covers_to": "覆盖时段",
        "state": "处理状态",
        "withdrawn_at": "是否撤回",
    },
    "raw_sentences": {"text": "原文分句"},
    "entities": {"entity_id": "编号", "canonical_name": "标准名", "type": "类型"},
    "aliases": {"alias": "各种叫法", "entity_id": "对到实体编号"},
    "facts": {
        "content": "事实文字",
        "subject_entity_id": "主体",
        "attribute": "属性",
        "value": "值",
        "occurred_from": "发生时间",
        "time_precision": "精度",
        "status": "确定程度",
        "speaker": "说话人",
        "frozen": "冻结标记",
    },
    "fact_entities": {"fact_id": "每条事实", "entity_id": "涉及哪些实体", "role": "角色"},
    "evidence": {"packet_id": "哪个包", "quote": "哪句原话", "position": "什么位置", "fact_id": "每条事实"},
    "quarantine": {"reason": "没过的原因", "payload": "L4 没过的内容"},
    "review_queue": {
        "kind": "类型",
        "claims": "几种说法",
        "quotes": "原文",
        "decision": "决定",
        "decided_at": "时间",
    },
    "rules": {"rule_id": "规则编号", "version": "版本", "layer": "落在哪一层", "from_questions": "来自哪几道题"},
    "audit_log": {"trusted": "信了谁", "basis": "依据哪段原文", "probability": "概率多少"},
    # J4.1（迁移 0020）
    "relations": {
        "subject_entity_id": "主体",
        "relation_type": "关系",
        "object_entity_id": "客体",
        "valid_from": "生效时间",
        "valid_to": "失效时间",
        "valid_from_precision": "精度",
        "valid_to_precision": "精度",
        "state": "状态",
        "superseded_by": "被谁替代",
        "status": "确定程度",
        "speaker": "说话人",
        "pause_condition": "暂停条件",
        "extracted_by": "抽取模型和提示词版本",
        "rule_version": "规则版本",
    },
    "relation_evidence": {"relation_id": "每条关系", "packet_id": "哪个包", "quote": "哪句原话", "position": "什么位置"},
}

# 01 以外的设计稿多写的（J4.1 加的）：04 p8 每条规则记下的、02 p6 留痕记下的
LATER_FIELDS = {
    "rules": {"description": "规则本身", "from_data": "来自哪份数据", "examples": "正反例原句", "decided_by": "谁定的"},
    "audit_log": {"tier": "哪一级判的", "judge": "哪条规则、哪个模型判的"},
}

FIRST_TABLES = LEDGER_TABLES[:11]  # 第一版（0001）建的


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "ledger.db")
    yield connection
    connection.close()


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_packet(conn: sqlite3.Connection, packet_id: str = "2026-11-08_2330_demo-bot", revision: int = 1) -> None:
    conn.execute(
        "INSERT INTO packets (packet_id, revision, source, env, framework, channel, kind, created_at,"
        " covers_from, covers_to, covers_from_epoch, covers_to_epoch, partitions, privacy, content_hash,"
        " inbox_path, archive_path, state, received_at)"
        " VALUES (?, ?, 'demo-bot', 'prod', 'demo-chat', 'chat', 'daily', 't', 't', 't', 0, 0, '[\"business\"]',"
        " 'normal', 'hash', 'inbox/x.md', 'archive/x.md', 'current', 't')",
        (packet_id, revision),
    )


def add_sentence(conn: sqlite3.Connection, sentence_id: str, text: str) -> None:
    conn.execute(
        "INSERT INTO raw_sentences (sentence_id, packet_id, revision, seq, section_kind, section_no, line_no,"
        " sentence_no, text, speaker, speaker_role, said_at, said_at_epoch, origin)"
        " VALUES (?, '2026-11-08_2330_demo-bot', 1, 1, 'raw', 1, 1, 1, ?, '我', 'owner', 't', 0, 'said')",
        (sentence_id, text),
    )


def test_first_migration_builds_exactly_the_eleven_tables(conn: sqlite3.Connection) -> None:
    assert schema_version(conn) == 0
    assert table_names(conn) == []

    migrate(conn, load_migrations()[:1])

    assert len(FIRST_TABLES) == 11
    assert sorted(table_names(conn)) == sorted(FIRST_TABLES)
    assert schema_version(conn) == 1


def test_empty_ledger_goes_straight_to_the_latest(conn: sqlite3.Connection) -> None:
    migrate(conn)
    assert len(LEDGER_TABLES) == 13
    assert set(LEDGER_TABLES) <= set(table_names(conn))
    assert schema_version(conn) == load_migrations()[-1].version


def test_relation_tables_come_with_j4_1(conn: sqlite3.Connection) -> None:
    """关系表、关系证据表、关系的说法是迁移 0020 加的：升到 0019 还没有，0020 有了。"""
    migrations = load_migrations()
    [j41] = [m for m in migrations if m.name == "0020_relations.sql"]
    migrate(conn, migrations[: j41.version - 1])
    assert not {"relations", "relation_evidence", "relation_claims"} & set(table_names(conn))
    migrate(conn, migrations[: j41.version])
    assert {"relations", "relation_evidence", "relation_claims"} <= set(table_names(conn))


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_fields_match_the_design(conn: sqlite3.Connection, table: str) -> None:
    migrate(conn)
    missing = set(DESIGN_FIELDS[table]) - columns(conn, table)
    assert not missing


@pytest.mark.parametrize("table", sorted(LATER_FIELDS))
def test_rules_and_audit_have_the_later_design_fields(conn: sqlite3.Connection, table: str) -> None:
    migrate(conn)
    assert not set(LATER_FIELDS[table]) - columns(conn, table)


def test_migrate_twice_changes_nothing(conn: sqlite3.Connection) -> None:
    assert migrate(conn)
    version = schema_version(conn)
    assert migrate(conn) == []
    assert schema_version(conn) == version


def test_old_ledger_can_be_upgraded_and_keeps_its_rows(conn: sqlite3.Connection) -> None:
    """以后加表就这样（J4.1 加关系表也是这样加的，真数据的升级见 test_relations.py）：新迁移一跑，旧数据还在。"""
    existing = load_migrations()
    migrate(conn, existing)
    add_packet(conn)

    later = Migration(
        existing[-1].version + 1,
        "later",
        "CREATE TABLE later_items (item_id TEXT PRIMARY KEY);"
        "CREATE TABLE later_evidence (item_id TEXT REFERENCES later_items (item_id));",
    )
    assert migrate(conn, [*existing, later]) == [later.version]

    assert {"later_items", "later_evidence"} <= set(table_names(conn))
    assert conn.execute("SELECT count(*) FROM packets").fetchone()[0] == 1


def test_failed_migration_is_all_or_nothing(conn: sqlite3.Connection) -> None:
    existing = load_migrations()
    migrate(conn, existing)
    version = schema_version(conn)
    broken = Migration(version + 1, "broken", "CREATE TABLE half_done (id INTEGER); THIS IS NOT SQL;")

    with pytest.raises(sqlite3.Error):
        migrate(conn, [*existing, broken])

    assert schema_version(conn) == version
    assert "half_done" not in table_names(conn)
    assert not conn.in_transaction


def test_ledger_newer_than_the_code_is_refused(conn: sqlite3.Connection) -> None:
    migrate(conn)
    conn.execute(f"PRAGMA user_version = {schema_version(conn) + 1}")
    with pytest.raises(LedgerError):
        migrate(conn)


def test_migration_numbers_are_contiguous() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == list(range(1, len(versions) + 1))


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    migrate(conn)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            add_packet(conn)
            raise RuntimeError("中途出错")
    assert conn.execute("SELECT count(*) FROM packets").fetchone()[0] == 0


def test_only_one_current_revision_per_packet(conn: sqlite3.Connection) -> None:
    migrate(conn)
    add_packet(conn, revision=1)
    with pytest.raises(sqlite3.IntegrityError):
        add_packet(conn, revision=2)


def test_foreign_keys_are_on(conn: sqlite3.Connection) -> None:
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_sentence(conn, "no-such-packet#1.1.1", "没有这个包")


def test_full_text_index_follows_the_table(conn: sqlite3.Connection) -> None:
    migrate(conn)
    add_packet(conn)
    add_sentence(conn, "p#1.1.1", "新的报价单今天发给客户")

    def hits(word: str) -> int:
        return conn.execute(
            "SELECT count(*) FROM raw_fts WHERE raw_fts MATCH ?", (f'"{word}"',)
        ).fetchone()[0]

    assert hits("报价单") == 1
    assert hits("报价") == 0  # trigram 至少三个字，所以两个字要走 LIKE

    conn.execute("UPDATE raw_sentences SET text = '改了别的内容' WHERE sentence_id = 'p#1.1.1'")
    assert hits("报价单") == 0
    assert hits("别的内容") == 1

    conn.execute("DELETE FROM raw_sentences WHERE sentence_id = 'p#1.1.1'")
    assert hits("别的内容") == 0


def test_readonly_connection_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "目录 有空格" / "账本.db"
    writer = connect(path)
    migrate(writer)
    add_packet(writer)
    writer.close()

    reader = connect_readonly(path)
    try:
        assert reader.execute("SELECT count(*) FROM packets").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("DELETE FROM packets")
    finally:
        reader.close()


def test_readonly_connection_does_not_create_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(path)
    assert not path.exists()
