"""Bot 一类（J3.6，D13、D92、D100）：账本的实体类型加上 Bot（迁移 0014 重建实体表，数据和外键照旧），
Bot 名单里的写法 L5 改成 Bot，普通词的写法只在它是说话人时才认，来源登记表里出包的 Bot 都要在名单里。
名字和句子都是编的，从头写的。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import World
from test_lexicon_l5 import lexicon_path, subjects, write_lex
from test_write import day_packet, deliver, l3_fact

from javmem.config import ConfigError
from javmem.ledger import connect, latest_version, load_migrations, migrate
from javmem.pipeline.merge import entity_id_of, name_key
from javmem.pipeline.write import writer_options

BOTS = """format: javmem-lexicon/2
bots:
  - names: [desk-bot, Desk]
    plain: [Desk]
  - names: [管家]
    plain: [管家]
  - names: [books-bot]
  - names: [trial-bot]
"""


def test_migration_adds_the_bot_type_and_keeps_rows_and_foreign_keys(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    migrations = load_migrations()
    before = [migration for migration in migrations if migration.version < 14]
    migrate(conn, before)
    conn.execute(
        "INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at, name_key) VALUES"
        " ('E1', '鹭汀纺织', '公司或机构', 't', 't', '鹭汀纺织')"
    )
    conn.execute("INSERT INTO aliases (alias_id, entity_id, alias, status, created_at, alias_key) VALUES ('A1', 'E1', '鹭汀', 'verified', 't', '鹭汀')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at) VALUES ('E2', 'x', 'Bot', 't', 't')")
    assert migrate(conn) == [version for version in range(14, latest_version() + 1)]
    rows = [tuple(row) for row in conn.execute("SELECT entity_id, canonical_name, type, name_key FROM entities")]
    assert rows == [("E1", "鹭汀纺织", "公司或机构", "鹭汀纺织")]
    conn.execute("INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at) VALUES ('E2', 'desk-bot', 'Bot', 't', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at) VALUES ('E3', 'x', '机器', 't', 't')")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):  # 外键照旧管着
        conn.execute("INSERT INTO aliases (alias_id, entity_id, alias, status, created_at) VALUES ('A2', 'E9', 'y', 'verified', 't')")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'entities' AND type = 'index'")}
    assert {"entities_by_name", "entities_by_type_and_key"} <= names
    conn.close()


def bot_extract(window):
    """窗口里 desk-bot 说话；L3 把它写成人、把“Desk”写成项目、把“管家”写成人（这几种 L3 现在都可能写错）。"""
    text = window.targets[0].text.rstrip("。")
    if "Desk" in text and "desk-bot" not in text:
        entity = {"key": "e1", "mention": "Desk", "name": "Desk", "type": "项目"}
    elif "管家" in text:
        entity = {"key": "e1", "mention": "管家", "name": "管家", "type": "人"}
    else:
        entity = {"key": "e1", "mention": "desk-bot", "name": "desk-bot", "type": "人"}
    return {"entities": [entity], "facts": [l3_fact(window, text, text, subject="e1")]}


def test_bot_names_become_bot_entities(world: World, tmp_path: Path) -> None:
    deliver(
        world,
        day_packet("2026-10-02", "我：desk-bot 每天七点出早报。", domain="[investment]"),
        day_packet("2026-10-03", "desk-bot：Desk 的提醒改到八点。", domain="[investment]"),
        day_packet("2026-10-04", "我：Desk 那个看板先别动。", domain="[investment]"),
        day_packet("2026-10-05", "我：管家下周换个模型。", domain="[investment]"),
    )
    conn = write_lex(world, bot_extract, lexicon_path(tmp_path, BOTS))
    found = subjects(conn)
    bot = entity_id_of("Bot", name_key("desk-bot"))
    assert found["desk-bot 每天七点出早报"] == ("desk-bot", "Bot")  # L3 写成人：名单里的写法照样是 Bot
    assert found["Desk 的提醒改到八点"] == ("desk-bot", "Bot")  # 普通词的写法：它就是说话人，算
    assert found["Desk 那个看板先别动"] == ("Desk", "项目")  # 这个窗口里它没说话、L3 也没标成 Bot：不算
    assert found["管家下周换个模型"] == ("管家", "人")  # 管家没说话：照旧是 L3 给的类型
    assert conn.execute("SELECT type FROM entities WHERE entity_id = ?", (bot,)).fetchone()[0] == "Bot"


def test_every_chat_source_must_be_in_the_bot_list(world: World, tmp_path: Path) -> None:
    settings = replace(world.settings, lexicon_file=lexicon_path(tmp_path, BOTS))
    assert writer_options(settings)["lexicon"].bots  # type: ignore[union-attr]
    short = BOTS.replace("  - names: [trial-bot]\n", "")
    with pytest.raises(ConfigError) as error:
        writer_options(replace(world.settings, lexicon_file=lexicon_path(tmp_path, short)))
    assert "trial-bot" in str(error.value) and "管家" not in str(error.value)
    no_bots = "format: javmem-lexicon/2\naliases:\n  - {type: 人, names: [柏庭舟, 柏廷舟]}\n"
    assert not writer_options(replace(world.settings, lexicon_file=lexicon_path(tmp_path, no_bots)))["lexicon"].bots  # type: ignore[union-attr]
