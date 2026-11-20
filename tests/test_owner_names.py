"""D62：主人的名字写在仓库外的配置里，L3 直接写主人的名字（或者“中文名 / 英文名”这样拼着写）的，也挂到“主人”上。
样例里的名字都是编的。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from helpers import World, build_packet, mail_header_block
from test_write import checked_items, day_packet, deliver, l3_fact

from javmem.cli import main
from javmem.config import ConfigError
from javmem.embedding import FakeEmbeddings
from javmem.ledger import open_ledger
from javmem.pipeline.batch import load_queued
from javmem.pipeline.merge import load_owner_names, name_key, owner_by_name
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.write import LedgerWriter, run_write

NAMES = frozenset({name_key("王小明"), name_key("Ming")})


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("王小明", True),
        ("ming", True),
        ("王小明 / Ming", True),
        ("王小明（Ming）", True),
        ("主人（Ming）", True),
        ("Ming 的同事", False),
        ("王小明公司", False),
        ("Minghua", False),
        ("主人", False),  # “主人”这些叫法另有规矩，这里只管配置里的名字
        ("", False),
    ],
)
def test_owner_by_name(name: str, expected: bool) -> None:
    assert owner_by_name(name, NAMES) is expected
    assert owner_by_name(name, frozenset()) is False  # 没配名字：只认“主人”“我”这些


def test_load_owner_names(tmp_path: Path) -> None:
    good = tmp_path / "owner.yaml"
    good.write_text("names: [王小明, Ming]\n", encoding="utf-8")
    assert load_owner_names(good) == NAMES
    assert load_owner_names(None) == frozenset()
    for text in ("names: []\n", "names: 王小明\n", "名字: [王小明]\n", "names: [王小明]\nextra: 1\n", "names: [' ']\n", ": : :\n"):
        bad = tmp_path / "bad.yaml"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError) as caught:
            load_owner_names(bad)
        assert "王小明" not in str(caught.value)


def write_with_owner(world: World, extract, names=NAMES) -> sqlite3.Connection:  # noqa: ANN001
    items = checked_items(world, extract)
    conn = open_ledger(world.settings.library("prod"))
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding", owner_names=names)
    batch = load_queued(world.settings, "prod")
    with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
        run_write(
            items,
            writer,
            packets=[(packet.packet_id, packet.header.revision) for packet in batch.packets],
            store=store,
            cache=cache,
            embedder=FakeEmbeddings(),
            sleep=lambda _: None,
        )
    return conn


def subjects(conn: sqlite3.Connection) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT f.content, e.canonical_name FROM facts AS f LEFT JOIN entities AS e ON e.entity_id = f.subject_entity_id"
    ).fetchall()
    return {row["content"]: row["canonical_name"] for row in rows}


def named(name: str):  # noqa: ANN201
    def extract(window):  # noqa: ANN001, ANN202
        text = window.targets[0].text.rstrip("。")
        entity = {"key": "e1", "mention": name, "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text, text, subject="e1")]}

    return extract


def test_facts_about_the_owner_by_name_join_the_owner(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "desk-bot：王小明 / Ming 周五去越南。"))
    conn = write_with_owner(world, named("王小明 / Ming"))
    assert subjects(conn) == {"王小明 / Ming 周五去越南": "主人"}
    assert [row["canonical_name"] for row in conn.execute("SELECT canonical_name FROM entities")] == ["主人"]


def test_without_the_config_the_name_stays_its_own_entity(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "desk-bot：王小明 / Ming 周五去越南。"))
    conn = write_with_owner(world, named("王小明 / Ming"), names=frozenset())
    assert subjects(conn) == {"王小明 / Ming 周五去越南": "王小明 / Ming"}


def test_someone_else_in_the_window_with_that_name_is_not_the_owner(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "Ming：我周五去越南。"))
    conn = write_with_owner(world, named("Ming"))
    assert subjects(conn) == {"我周五去越南": "Ming"}


def test_a_mail_about_the_owner_by_name_joins_the_owner(world: World) -> None:
    """邮件里的“我”是发件人；可写的是主人的名字，就是主人。"""
    body = "## 原话 · 邮件 · 2026-11-03 09:12\n\n" + mail_header_block() + "\n  王小明 下周一到河内。\n"
    packet = build_packet(
        "mail-sales-a",
        "2026-11-03_1800",
        body=body,
        framework="mail-import",
        channel="email",
        mailbox="li.ming@example.com",
        covers_from="2026-11-02T18:00:00+08:00",
        covers_to="2026-11-03T18:00:00+08:00",
    )
    world.drop("inbox/mail-sales-a", packet)
    world.intake()

    def extract(window):  # noqa: ANN001, ANN202
        line = next(line for line in window.targets if "河内" in line.text)
        entity = {"key": "e1", "mention": "王小明", "name": "王小明", "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, line.text, "王小明下周一到河内", subject="e1")]}

    conn = write_with_owner(world, extract)
    assert subjects(conn) == {"王小明下周一到河内": "主人"}


def test_a_bad_owner_file_stops_write_before_touching_anything(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "owner.yaml"
    bad.write_text("names: []\n", encoding="utf-8")
    monkeypatch.setenv("JAVMEM_OWNER_FILE", str(bad))
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 2
    assert world.rows("SELECT count(*) AS n FROM facts")[0]["n"] == 0
