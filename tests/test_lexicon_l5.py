"""L5 照别名表第二版认人（J3.6，D100）：组里的名字对到标准名算出来的编号（先后不同、结果一样），from_types 改类型，
where 挡住别的分区，拆开的名字每一段都在组里才算，不建实体的名字照泛称办，“不是同一个”的不按别名连起来，
主人的规矩不变，账本记下别名表的指纹、改了巡检提醒。名字和句子都是编的，从头写的。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import World
from test_write import Extract, checked_items, day_packet, deliver, l3_fact

from javmem.config import ConfigError
from javmem.embedding import FakeEmbeddings
from javmem.ledger import open_ledger
from javmem.patrol import run_patrol
from javmem.pipeline.batch import load_queued
from javmem.pipeline.merge import entity_id_of, name_key
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline import write as write_module
from javmem.pipeline.write import LedgerWriter, run_write, writer_options
from javmem.zh.lexicon import load_lexicon

LEXICON = """format: javmem-lexicon/2
default_where: [investment, personal]
aliases:
  - type: 人
    names: [柏庭舟, 柏廷舟, Tingzhou]
  - type: 资产
    names: [KVLT, Kovalt]
    from_types: [公司或机构]
  - type: 人
    names: [Corbin Vale, Corbin]
    where: [investment]
not_entities:
  names: [9号机]
  path_like: true
apart:
  - {type: 人, names: [Rhea Dunmore, Rhea]}
"""


def lexicon_path(tmp_path: Path, text: str = LEXICON) -> Path:
    path = tmp_path / "lexicon.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def write_lex(
    world: World,
    extract: Extract,
    lexicon: Path | None,
    *,
    order: Callable | None = None,
    owner_names: frozenset[str] = frozenset(),
) -> sqlite3.Connection:
    items = checked_items(world, extract)
    if order is not None:
        items = order(items)
    conn = open_ledger(world.settings.library("prod"))
    embedder = FakeEmbeddings()
    writer = LedgerWriter(
        conn, extracted_by="l3-v1 fake", embedding_model=embedder.name, lexicon=load_lexicon(lexicon), owner_names=owner_names
    )
    batch = load_queued(world.settings, "prod")
    with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
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


def one_entity(name_of: Callable[[str], tuple[str, str]]) -> Extract:
    """每个窗口一个实体、一条事实：name_of(原文) 给 (名字, 类型)。"""

    def extract(window):
        text = window.targets[0].text.rstrip("。")
        name, entity_type = name_of(text)
        entity = {"key": "e1", "mention": name, "name": name, "type": entity_type}
        return {"entities": [entity], "facts": [l3_fact(window, text, text, subject="e1")]}

    return extract


def subjects(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    rows = conn.execute(
        "SELECT f.content, e.canonical_name, e.type FROM facts AS f LEFT JOIN entities AS e ON e.entity_id = f.subject_entity_id"
    ).fetchall()
    return {row["content"]: (row["canonical_name"], row["type"]) for row in rows}


TYPO_FIRST = [
    day_packet("2026-08-03", "我：柏廷舟管资金池的日常。", domain="[investment]"),
    day_packet("2026-09-15", "我：周五约柏庭舟对一下报价。", domain="[investment]"),
]


def name_in(text: str) -> tuple[str, str]:
    return ("柏廷舟" if "柏廷舟" in text else "柏庭舟"), "人"


def test_group_names_go_to_the_standard_id_whatever_comes_first(world: World, tmp_path: Path) -> None:
    deliver(world, *TYPO_FIRST)
    conn = write_lex(world, one_entity(name_in), lexicon_path(tmp_path))
    standard = entity_id_of("人", name_key("柏庭舟"))
    assert [(row["entity_id"], row["canonical_name"]) for row in conn.execute("SELECT * FROM entities")] == [(standard, "柏庭舟")]
    assert set(subjects(conn).values()) == {("柏庭舟", "人")}
    aliases = {row["alias"] for row in conn.execute("SELECT alias FROM aliases WHERE entity_id = ?", (standard,))}
    assert aliases == {"柏廷舟", "柏庭舟"}  # 别名照记，出处是原文那一句


def test_reverse_order_gives_the_same_entity(world: World, tmp_path: Path) -> None:
    deliver(world, *TYPO_FIRST)
    conn = write_lex(world, one_entity(name_in), lexicon_path(tmp_path), order=lambda items: list(reversed(items)))
    assert [row["canonical_name"] for row in conn.execute("SELECT * FROM entities")] == ["柏庭舟"]


def test_without_the_v2_lexicon_the_first_spelling_wins(world: World, tmp_path: Path) -> None:
    deliver(world, *TYPO_FIRST)
    v1 = lexicon_path(tmp_path, "aliases:\n  - {type: 人, names: [柏庭舟, 柏廷舟]}\n")
    conn = write_lex(world, one_entity(name_in), v1)
    # 第一版只用来找：按别名表找到了先建的那个，标准名是先出现的那个写法（笔误的），和以前一样
    assert [row["canonical_name"] for row in conn.execute("SELECT * FROM entities")] == ["柏廷舟"]


def test_from_types_moves_the_type(world: World, tmp_path: Path) -> None:
    deliver(
        world,
        day_packet("2026-10-02", "我：Kovalt 那边的财报下周出。", domain="[investment]"),
        day_packet("2026-10-09", "我：KVLT 收在 41.2。", domain="[investment]"),
    )

    def name_of(text: str) -> tuple[str, str]:
        return ("Kovalt", "公司或机构") if "Kovalt" in text else ("KVLT", "资产")

    conn = write_lex(world, one_entity(name_of), lexicon_path(tmp_path))
    assert set(subjects(conn).values()) == {("KVLT", "资产")}
    assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 1


def test_where_keeps_other_partitions_apart(world: World, tmp_path: Path) -> None:
    deliver(
        world,
        day_packet("2026-10-02", "我：Corbin 把仓位降到三成。", domain="[investment]"),
        day_packet("2026-10-03", "我：Corbin 下午来对账。", domain="[business]"),
    )

    def name_of(text: str) -> tuple[str, str]:
        return "Corbin", "人"

    conn = write_lex(world, one_entity(name_of), lexicon_path(tmp_path))
    found = subjects(conn)
    assert found["Corbin 把仓位降到三成"] == ("Corbin Vale", "人")  # 投资那边算这一组
    assert found["Corbin 下午来对账"] == ("Corbin", "人")  # business 不在 where 里：照旧按名字建


def test_split_names_need_every_piece_in_the_group(world: World, tmp_path: Path) -> None:
    deliver(
        world,
        day_packet("2026-10-02", "我：柏廷舟 / Tingzhou 管资金池。", domain="[investment]"),
        day_packet("2026-10-03", "我：柏廷舟 / 鹿鸣 一起去看铺面。", domain="[investment]"),
    )

    def name_of(text: str) -> tuple[str, str]:
        return ("柏廷舟 / Tingzhou" if "Tingzhou" in text else "柏廷舟 / 鹿鸣"), "人"

    conn = write_lex(world, one_entity(name_of), lexicon_path(tmp_path))
    found = subjects(conn)
    assert found["柏廷舟 / Tingzhou 管资金池"] == ("柏庭舟", "人")
    assert found["柏廷舟 / 鹿鸣 一起去看铺面"][0] != "柏庭舟"  # 有一段不在组里：不算这一组


def test_not_entities_get_no_entity(world: World, tmp_path: Path) -> None:
    deliver(
        world,
        day_packet("2026-10-02", "我：9号机今晚重启。", domain="[investment]"),
        day_packet("2026-10-03", "我：周报放在 notes/weekly_07.md 里。", domain="[investment]"),
    )

    def name_of(text: str) -> tuple[str, str]:
        return ("9号机", "项目") if "9号机" in text else ("notes/weekly_07.md", "项目")

    conn = write_lex(world, one_entity(name_of), lexicon_path(tmp_path))
    assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
    assert set(subjects(conn).values()) == {(None, None)}  # 事实照记，主体空着
    assert conn.execute("SELECT count(*) FROM aliases").fetchone()[0] == 0


def test_apart_names_are_not_joined_by_alias(world: World, tmp_path: Path) -> None:
    packets = [
        day_packet("2026-10-02", "我：Rhea Dunmore（大家叫她 Rhea）接手了物业。", domain="[personal]"),
        day_packet("2026-10-05", "我：Rhea 明天来量窗帘。", domain="[personal]"),
    ]

    def extract(window):
        text = window.targets[0].text.rstrip("。")
        name = "Rhea Dunmore" if "Dunmore" in text else "Rhea"
        entity = {"key": "e1", "mention": "Rhea", "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text, text, subject="e1")]}

    deliver(world, *packets)
    conn = write_lex(world, extract, lexicon_path(tmp_path, LEXICON.replace("[Rhea Dunmore, Rhea]", "[Rhea Dunmore, Rhea Ngata]")))
    assert len(set(subjects(conn).values())) == 1  # 没记“不是同一个”：第二包的 Rhea 按别名对上了


def test_apart_names_are_not_joined_by_alias_when_recorded(world: World, tmp_path: Path) -> None:
    deliver(
        world,
        day_packet("2026-10-02", "我：Rhea Dunmore（大家叫她 Rhea）接手了物业。", domain="[personal]"),
        day_packet("2026-10-05", "我：Rhea 明天来量窗帘。", domain="[personal]"),
    )

    def extract(window):
        text = window.targets[0].text.rstrip("。")
        name = "Rhea Dunmore" if "Dunmore" in text else "Rhea"
        entity = {"key": "e1", "mention": "Rhea", "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text, text, subject="e1")]}

    conn = write_lex(world, extract, lexicon_path(tmp_path))
    found = subjects(conn)
    assert found["Rhea 明天来量窗帘"] == ("Rhea", "人")
    assert conn.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 0


def test_owner_comes_before_the_lexicon(world: World, tmp_path: Path) -> None:
    deliver(world, day_packet("2026-10-02", "我：主人这周不看盘。", domain="[investment]"))
    conn = write_lex(world, one_entity(lambda text: ("主人", "人")), lexicon_path(tmp_path))
    assert set(subjects(conn).values()) == {("主人", "人")}


def test_owner_names_in_the_lexicon_are_a_config_error(world: World, tmp_path: Path) -> None:
    owner = tmp_path / "owner.yaml"
    owner.write_text("names: [柏庭舟]\n", encoding="utf-8")
    settings = replace(world.settings, owner_file=owner, lexicon_file=lexicon_path(tmp_path))
    with pytest.raises(ConfigError) as error:
        writer_options(settings)
    assert "柏庭舟" not in str(error.value)


def test_lexicon_fingerprint_is_recorded_and_patrol_warns_when_it_changes(world: World, tmp_path: Path) -> None:
    deliver(world, *TYPO_FIRST)
    path = lexicon_path(tmp_path)
    conn = write_lex(world, one_entity(name_in), path)
    assert write_module.ledger_lexicon(conn) == load_lexicon(path).fingerprint[:16]
    settings = replace(world.settings, lexicon_file=path)
    assert not [item for item in run_patrol(settings) if item.kind == "lexicon_changed"]
    path.write_text(LEXICON.replace("[柏庭舟, 柏廷舟, Tingzhou]", "[柏庭舟, Tingzhou]"), encoding="utf-8")
    warned = [item for item in run_patrol(settings) if item.kind == "lexicon_changed"]
    assert [item.library for item in warned] == ["prod"]
