"""L5、L6、向量审阅（J2.9）找出来的问题，一条一个测试。例子都是编的。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from helpers import World, build_mail_packet, build_packet, mail_header_block
from test_write import checked_items, day_packet, deliver, dump, facts, l3_fact, ship_extract, write

from javmem.cli import main
from javmem.config import Settings
from javmem.embedding import (
    BAD_REPLY,
    BAD_REQUEST,
    CONFIG,
    TRANSPORT,
    EmbeddingError,
    EmbeddingSettings,
    EmbedStats,
    FakeEmbeddings,
    FakeEmbeddingServer,
    OpenAIEmbeddings,
    embed_all,
)
from javmem.ledger import open_ledger, transaction
from javmem.pipeline.batch import load_queued
from javmem.pipeline.merge import MULTI_VALUED, group_key, key_text, merge_key
from javmem.pipeline.normalize import Scope
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.window import Window
from javmem.pipeline.write import LedgerWriter, fill_vectors, run_write

SCOPE = Scope(("business",), ())
ORDER = {"key": "e1", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}


# ---------- 1. 数里的标点不能去掉 ----------


def test_numbers_keep_their_punctuation_in_keys() -> None:
    assert key_text("12.5") != key_text("125") != key_text("1.25")
    assert key_text("11/2") != key_text("1/12")
    assert key_text("$100") != key_text("¥100")
    assert key_text("-5") != key_text("5")
    assert key_text("1.5万") != key_text("15万")
    assert key_text("12,500") == key_text("12500")
    assert key_text("单价 12.5 美元。") == key_text("单价12.5美元")
    assert merge_key(SCOPE, "E1", "单价", "12.5 美元", "") != merge_key(SCOPE, "E1", "单价", "125 美元", "")


def test_different_prices_are_not_merged(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        line = window.targets[0].text.rstrip("。")
        value = "12.5 美元" if "12.5" in line else "125 美元"
        return {"entities": [ORDER], "facts": [l3_fact(window, line, f"越南那批订单单价 {value}", subject="e1", attribute="单价", value=value)]}

    deliver(world, day_packet("2026-11-01", "我：越南那批订单单价 12.5 美元。"), day_packet("2026-11-03", "我：越南那批订单单价改成 125 美元。"))
    conn = write(world, extract)
    old, new = facts(conn)
    assert (old["value"], new["value"]) == ("12.5 美元", "125 美元")
    assert old["superseded_by"] == new["fact_id"]


# ---------- 2. 泛称不当全库唯一的实体 ----------


def test_common_nouns_are_not_global_entities(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        line = window.targets[0].text.rstrip("。")
        date_text = "11-15" if "11-15" in line else "11-28"
        entity = {"key": "e1", "mention": "客户", "name": "客户", "type": "公司或机构"}
        return {"entities": [entity], "facts": [l3_fact(window, line, f"客户要求 {date_text} 交货", subject="e1", attribute="交货日期", value=date_text)]}

    deliver(world, day_packet("2026-11-01", "我：越南那家的客户要求 11-15 交货。"), day_packet("2026-11-03", "我：新加坡这边的客户要求 11-28 交货。"))
    conn = write(world, extract)
    assert conn.execute("SELECT count(*) FROM entities WHERE canonical_name = '客户'").fetchone()[0] == 0
    assert [row["superseded_by"] for row in facts(conn)] == [None, None]


def test_the_owner_in_chat_is_one_entity(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        line = window.targets[0].text.rstrip("。")
        entity = {"key": "e1", "mention": "我", "name": "我", "type": "人"}
        value = "9%" if "9%" in line else "12%"
        return {"entities": [entity], "facts": [l3_fact(window, line, f"主人的目标收益是 {value}", subject="e1", attribute="目标收益", value=value)]}

    deliver(world, day_packet("2026-11-01", "我：我的目标收益是 9%。"), day_packet("2026-11-03", "我：我的目标收益改成 12%。"))
    conn = write(world, extract)
    owners = conn.execute("SELECT canonical_name FROM entities").fetchall()
    assert [row["canonical_name"] for row in owners] == ["主人"]
    old, new = facts(conn)
    assert old["superseded_by"] == new["fact_id"]


# ---------- 3. 按别名对齐不跨可见范围 ----------


def _person_extract(window: Window) -> dict[str, object]:
    line = window.targets[0].text.rstrip("。")
    if "周建国" in line and "老周" in line:
        entity = {"key": "e1", "mention": "老周", "name": "周建国", "type": "人"}
    elif "周立新" in line:
        entity = {"key": "e1", "mention": "老周", "name": "周立新", "type": "人"}
    elif "周建国" in line:
        entity = {"key": "e1", "mention": "周建国", "name": "周建国", "type": "人"}
    else:
        entity = {"key": "e1", "mention": "老周", "name": "老周", "type": "人"}
    return {"entities": [entity], "facts": [l3_fact(window, line, line, subject="e1")]}


def test_alias_from_another_scope_does_not_align(world: World) -> None:
    deliver(
        world,
        day_packet("2026-11-01", "我：周建国（大家叫他老周）的年薪是 50 万。", domain="[finance]"),
        day_packet("2026-11-02", "我：老周下周去越南。"),
        day_packet("2026-11-03", "我：周建国是新来的销售总监。"),
    )
    conn = write(world, _person_extract)
    subjects = {
        row["content"]: conn.execute("SELECT canonical_name FROM entities WHERE entity_id = ?", (row["subject_entity_id"],)).fetchone()[0]
        for row in facts(conn)
    }
    assert subjects["老周下周去越南"] == "老周"  # 不靠 finance 里的别名挂到周建国上
    assert subjects["周建国是新来的销售总监"] == "周建国"


def test_ambiguity_only_counts_candidates_in_the_same_scope(world: World) -> None:
    deliver(
        world,
        day_packet("2026-11-01", "我：周立新也叫老周，管工资。", domain="[finance]"),
        day_packet("2026-11-02", "我：周建国就是老周，管越南。"),
        day_packet("2026-11-03", "我：老周明天回来。"),
    )
    conn = write(world, _person_extract)
    row = conn.execute("SELECT status, subject_entity_id FROM facts WHERE content = '老周明天回来'").fetchone()
    assert row["status"] == "verified"
    name = conn.execute("SELECT canonical_name FROM entities WHERE entity_id = ?", (row["subject_entity_id"],)).fetchone()[0]
    assert name == "周建国"
    assert conn.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 0


# ---------- 4. 替代的时间和替代者按每一句算 ----------


def test_superseded_at_is_when_the_other_value_was_first_said(world: World) -> None:
    deliver(
        world,
        day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
        day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"),
        day_packet("2026-11-05", "我：越南那批订单改到 11-25 出货。"),
        day_packet("2026-11-10", "我：越南那批订单再确认一下，还是 11-20 出货。"),
    )
    conn = write(world, ship_extract)
    rows = {row["value"]: row for row in facts(conn)}
    assert rows["11-15"]["superseded_by"] == rows["11-20"]["fact_id"]
    assert rows["11-15"]["superseded_at"].startswith("2026-11-03")
    assert rows["11-25"]["superseded_by"] == rows["11-20"]["fact_id"]
    assert rows["11-25"]["superseded_at"].startswith("2026-11-10")
    assert rows["11-20"]["superseded_by"] is None


# ---------- 5. 多值属性看结尾的中心词 ----------


@pytest.mark.parametrize(
    ("attribute", "multi"),
    [
        ("计划出货日期", False),
        ("任务负责人", False),
        ("活动地点", False),
        ("会议安排时间", False),
        ("报价单价", False),
        ("plan date", False),
        ("Task owner", False),
        ("投资偏好", True),
        ("活动安排", True),
        ("付款要求", True),
        ("task list", True),
        ("task_list", True),
    ],
)
def test_multi_valued_looks_at_the_head_noun(attribute: str, multi: bool) -> None:
    assert (group_key(SCOPE, "E1", attribute) is None) is multi, attribute


# ---------- 6、8. 缺向量不卡住，一条坏文字不卡住整个库 ----------


def test_changing_the_embedding_model_refills_vectors(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship_extract, embedder=FakeEmbeddings(name="m1"))
    deliver(world, day_packet("2026-11-02", "我：越南那批订单还是 11-15 出货，别忘了。"))
    conn = write(world, ship_extract, embedder=FakeEmbeddings(name="m2"))
    assert {row["model"] for row in conn.execute("SELECT model FROM fact_vectors")} == {"m2"}
    assert conn.execute("SELECT count(*) FROM facts WHERE fact_id NOT IN (SELECT fact_id FROM fact_vectors)").fetchone()[0] == 0
    assert {row["extraction"] for row in conn.execute("SELECT extraction FROM packets")} == {"done"}


def test_a_text_the_service_always_rejects_does_not_block_the_library(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-02", "我：越南那批订单改到 11-20 出货。"))
    fake = FakeEmbeddings(bad_texts={"越南那批订单出货日期是 11-20"})
    conn = write(world, ship_extract, embedder=fake)
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM fact_vectors").fetchone()[0] == 1
    assert {row["extraction"] for row in conn.execute("SELECT extraction FROM packets")} == {"done"}


def test_vectors_of_finished_batches_are_kept_when_a_later_batch_fails(tmp_path: Path) -> None:
    texts = [f"第 {index} 句" for index in range(40)]
    fake = FakeEmbeddings(["ok", "ok", "auth"])
    stored: dict[str, bytes] = {}
    with pytest.raises(EmbeddingError):
        embed_all(texts, fake, sleep=lambda _: None, store=stored.update)
    assert len(stored) == 32


# ---------- 7、15. 向量服务回得怪 ----------


@pytest.mark.parametrize(("reply", "kind"), [("bad_status", TRANSPORT), ("short_body", TRANSPORT), ("huge", BAD_REPLY),
                                             ("bool", BAD_REPLY), ("zero", BAD_REPLY), ("redirect", CONFIG)])
def test_odd_replies_are_embedding_errors(reply: str, kind: str) -> None:
    with FakeEmbeddingServer(api_key="k", replies=[reply]) as server:
        client = OpenAIEmbeddings(EmbeddingSettings.from_env({"JAVMEM_EMBEDDING_BASE_URL": server.base_url, "JAVMEM_EMBEDDING_API_KEY": "k"}))
        with pytest.raises(EmbeddingError) as caught:
            client.embed(["一句"])
        assert caught.value.kind == kind
        assert all(request.get("authorization") != "Bearer k" for request in server.redirected)


def test_bad_request_alone_is_a_bad_request() -> None:
    with FakeEmbeddingServer(replies=[400]) as server:
        client = OpenAIEmbeddings(EmbeddingSettings.from_env({"JAVMEM_EMBEDDING_BASE_URL": server.base_url}))
        with pytest.raises(EmbeddingError) as caught:
            client.embed(["一句"])
    assert caught.value.kind == BAD_REQUEST


# ---------- 9. 抽不了的包标成 failed（排队修，以前是留着 queued），--retry-failed 还够得着 ----------


def test_permanently_failed_window_marks_its_packet_failed_and_retry_still_reaches_it(world: World) -> None:
    world.drop("inbox/desk-bot", day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    world.intake()
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    with sqlite3.connect(world.data / "l3" / "l3.db") as store:
        store.execute("DELETE FROM windows")
        store.execute("DELETE FROM results")
        store.execute("UPDATE calls SET status = 'length'")
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 1
    conn = open_ledger(world.settings.library("prod"))
    assert conn.execute("SELECT extraction FROM packets").fetchone()[0] == "failed"
    assert main(["extract", "--library", "prod", "--fake", "--retry-failed"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    assert conn.execute("SELECT extraction FROM packets").fetchone()[0] == "done"


# ---------- 10. 包重发以后，旧版本的事实清掉 ----------


def test_replacement_clears_what_the_old_revision_wrote(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    conn = write(world, ship_extract)
    assert [row["value"] for row in facts(conn)] == ["11-15"]
    replaced = day_packet("2026-11-01", "我：越南那批订单 11-25 出货。").replace("privacy: normal", "privacy: normal\nrevision: 2")
    world.drop("inbox/desk-bot", replaced)
    world.intake()
    conn = open_ledger(world.settings.library("prod"))
    assert facts(conn) == []  # 旧版本的事实清掉了，等新版本写账
    assert conn.execute("SELECT count(*) FROM fact_claims").fetchone()[0] == 0
    conn = write(world, ship_extract)
    assert [row["value"] for row in facts(conn)] == ["11-25"]


def test_replacement_with_private_clears_the_old_facts(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship_extract)
    replaced = day_packet("2026-11-01", "我：越南那批订单 11-15 出货。").replace("privacy: normal", "privacy: private\nrevision: 2")
    world.drop("inbox/desk-bot", replaced)
    world.intake()
    conn = open_ledger(world.settings.library("prod"))
    assert facts(conn) == []


# ---------- 12. 别名编号变了也不撞外键 ----------


def test_alias_with_an_old_id_does_not_break_evidence(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    conn = write(world, ship_extract)
    with transaction(conn):
        conn.execute("PRAGMA defer_foreign_keys = ON")
        conn.execute("UPDATE evidence SET alias_id = 'Aold' WHERE alias_id IS NOT NULL")
        conn.execute("UPDATE aliases SET alias_id = 'Aold'")
    deliver(world, day_packet("2026-11-02", "我：越南那批订单改到 11-20 出货。"))
    conn = write(world, ship_extract)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT count(*) FROM aliases").fetchone()[0] == 1


# ---------- 14. 冻结的事实不随重写删掉，待确认里指着删掉的事实的条目清掉 ----------


def test_rewrite_that_drops_a_fact(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    empty = checked_items(world, lambda window: {"entities": [], "facts": []})  # 包还在排队时取窗口
    conn = write(world, ship_extract)
    old, new = facts(conn)
    with transaction(conn):
        conn.execute("UPDATE facts SET frozen = 1 WHERE fact_id = ?", (old["fact_id"],))
        conn.execute(
            "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('frozen_conflict', ?, '[]', '2026-11-04T00:00:00+08:00')",
            (json.dumps({"frozen": old["fact_id"], "new": new["fact_id"]}),),
        )
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
    with VectorCache(world.data / "vectors") as cache:
        for item in empty:
            writer.write_window(item, cache.get("fake-embedding", writer.texts_needed(item)))
    remaining = {row["fact_id"]: row for row in facts(conn)}
    assert old["fact_id"] in remaining  # 冻结的留着
    assert new["fact_id"] not in remaining
    assert remaining[old["fact_id"]]["superseded_by"] is None
    assert conn.execute("SELECT count(*) FROM review_queue WHERE decided_at IS NULL").fetchone()[0] == 0  # 指着删掉的事实
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_decided_frozen_conflict_is_not_queued_again(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    conn = write(world, ship_extract)
    with transaction(conn):
        conn.execute("UPDATE facts SET frozen = 1")
    deliver(world, day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    conn = write(world, ship_extract)
    with transaction(conn):
        conn.execute("UPDATE review_queue SET decision = 'keep', decided_at = '2026-11-04T00:00:00+08:00'")
    deliver(world, day_packet("2026-11-05", "我：越南那批订单还是 11-20 出货。"))
    conn = write(world, ship_extract)
    assert conn.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 1


# ---------- 测试：缓存里有就不调服务；增量写和整本重建一样 ----------


def test_rebuild_with_the_old_cache_calls_nothing_and_matches_incremental(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packets = [
        day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
        day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"),
    ]
    deliver(world, packets[0])
    write(world, ship_extract)
    deliver(world, packets[1])
    incremental = dump(write(world, ship_extract))

    other = tmp_path / "rebuild"
    other.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(other))
    again = World(other.resolve(), Settings.from_env())
    deliver(again, *packets)
    fake = FakeEmbeddings()
    rebuilt = dump(write(again, ship_extract, embedder=fake, vectors=world.data / "vectors"))
    assert fake.calls == []
    assert rebuilt == incremental


def test_write_command_against_the_fake_service(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    ledger = tmp_path / "tokens.csv"
    with FakeEmbeddingServer(api_key="k") as server:
        monkeypatch.setenv("JAVMEM_EMBEDDING_BASE_URL", server.base_url)
        monkeypatch.setenv("JAVMEM_EMBEDDING_API_KEY", "k")
        monkeypatch.setenv("JAVMEM_TOKEN_LEDGER", str(ledger))
        assert main(["write", "--library", "prod", "--fake", "--tag", "t"]) == 0
    text = ledger.read_text(encoding="utf-8")
    assert "embedding" in text and "越南" not in text and ",k," not in text
    conn = open_ledger(world.settings.library("prod"))
    assert conn.execute("SELECT count(*) FROM fact_vectors").fetchone()[0] >= 1


# ========== 复看（第二轮）找出来的 ==========




def _subjects(conn: sqlite3.Connection) -> dict[str, str | None]:
    result = {}
    for row in facts(conn):
        entity = conn.execute("SELECT canonical_name FROM entities WHERE entity_id = ?", (row["subject_entity_id"],)).fetchone()
        result[row["content"]] = entity[0] if entity else None
    return result


# ---------- N1. 别人、Bot 自己说的“我”不是主人 ----------


def test_someone_elses_i_is_not_the_owner(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        found, entities = [], []
        for index, line in enumerate(window.targets):
            text = line.text.rstrip("。")
            if "泰国" in text:
                entity = {"key": f"e{index}", "mention": "我", "name": "我", "type": "人"}
            elif "越南" in text:
                entity = {"key": f"e{index}", "mention": "我", "name": "小李", "type": "人"}
            elif "单子" in text:
                entity = {"key": f"e{index}", "mention": "我", "name": "desk-bot", "type": "人"}
            else:
                entity = {"key": f"e{index}", "mention": "我", "name": "我", "type": "人"}  # Bot 说的“我”，L3 没认出是谁
            entities.append(entity)
            found.append(l3_fact(window, text, text, subject=f"e{index}", attribute="行程", value=text[-4:]))
        return {"entities": entities, "facts": found}

    deliver(
        world,
        day_packet("2026-11-01", "我：我下周去泰国。"),
        day_packet("2026-11-02", "小李：我明天去越南。"),
        day_packet("2026-11-03", "desk-bot：我已经把单子发了。", "desk-bot：我下午再查一遍。"),
    )
    conn = write(world, extract)
    assert _subjects(conn) == {
        "我下周去泰国": "主人",
        "我明天去越南": "小李",
        "我已经把单子发了": "desk-bot",
        "我下午再查一遍": None,
    }
    assert all(row["superseded_by"] is None for row in facts(conn))


# ---------- N3. 别的范围里的歧义不算 ----------


def test_ambiguity_from_another_scope_does_not_make_facts_pending(world: World) -> None:
    deliver(
        world,
        day_packet("2026-11-01", "我：周建国就是老周，管越南。"),
        day_packet("2026-11-02", "我：周立新也叫老周，管泰国。"),
        day_packet("2026-11-03", "我：老周明天回来。"),
        day_packet("2026-11-04", "我：老周的工资是 5 万。", domain="[finance]"),
    )
    conn = write(world, _person_extract)
    rows = {row["content"]: row for row in facts(conn)}
    assert rows["老周明天回来"]["status"] == "pending"
    assert (rows["老周的工资是 5 万"]["status"], rows["老周的工资是 5 万"]["pending_reason"]) == ("verified", None)


# ---------- N4. 取窗口以后包换了版本：不写回去 ----------


def test_a_window_whose_packet_was_replaced_meanwhile_is_not_written(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    items = checked_items(world, ship_extract)  # javmem write 开头取好的窗口
    packets = [(packet.packet_id, packet.header.revision) for packet in load_queued(world.settings, "prod").packets]
    replaced = day_packet("2026-11-01", "我：越南那批订单 11-15 出货。").replace("privacy: normal", "privacy: private\nrevision: 2")
    deliver(world, replaced)
    conn = open_ledger(world.settings.library("prod"))
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
    with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
        run_write(items, writer, packets=packets, store=store, cache=cache, embedder=FakeEmbeddings(), sleep=lambda _: None)
    assert facts(conn) == [] and writer.stats.windows_stale == 1


# ---------- N5. 同一往来串的两个邮件包：前一包重发，后一包的事实不丢 ----------


def _mail(stamp: str, body: str, covers_from: str, covers_to: str, *, reply: bool, revision: int | None = None) -> str:
    if reply:
        header = mail_header_block(
            message_id='"<b@example.com>"', in_reply_to='"<a@example.org>"', date="2026-11-04T09:00:00+07:00",
            subject='"Re: Shipment"', **{"from": '{name: "李明", addr: "li.ming@example.com"}',
                                         "to": '[{name: "Tran Mai", addr: "tran.mai@example.org"}]'},
        )
        date = "2026-11-04 09:00"
    else:
        header = mail_header_block(subject='"Shipment"')
        date = "2026-11-03 09:12"
    text = build_packet(
        "mail-sales-a", stamp, body=f"## 原话 · 邮件 · {date}\n\n{header}\n  {body}\n", framework="mail-import",
        channel="email", mailbox="li.ming@example.com", covers_from=covers_from, covers_to=covers_to, created_at=covers_to,
    )
    return text if revision is None else text.replace("privacy: normal", f"privacy: normal\nrevision: {revision}")


def _mail_extract(window: Window) -> dict[str, object]:
    found = []
    for line in window.targets:
        if "Nov 15" in line.text:
            found.append(l3_fact(window, "We will move the shipment to Nov 15", "We will move the shipment to Nov 15"))
        if "12.5 USD" in line.text:
            found.append(l3_fact(window, "Price stays 12.5 USD", "Price stays 12.5 USD"))
    return {"entities": [], "facts": found}


def test_replacing_the_first_mail_of_a_thread_keeps_the_facts_of_the_reply(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _mail("2026-11-03_1800", "We will move the shipment to Nov 15.", "2026-11-02T18:00:00+08:00", "2026-11-03T18:00:00+08:00", reply=False)
    again = _mail("2026-11-03_1800", "We will move the shipment to Nov 15.", "2026-11-02T18:00:00+08:00", "2026-11-03T18:00:00+08:00", reply=False, revision=2)
    reply = _mail("2026-11-04_1800", "Price stays 12.5 USD.", "2026-11-03T18:00:00+08:00", "2026-11-04T18:00:00+08:00", reply=True)
    deliver(world, first, reply, folder="inbox/mail-sales-a")
    write(world, _mail_extract)
    deliver(world, again, folder="inbox/mail-sales-a")
    conn = write(world, _mail_extract)
    incremental = sorted(row["content"] for row in facts(conn))
    assert incremental == ["Price stays 12.5 USD", "We will move the shipment to Nov 15"]

    other = tmp_path / "rebuild"
    other.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(other))
    rebuilt = World(other.resolve(), Settings.from_env())
    deliver(rebuilt, again, reply, folder="inbox/mail-sales-a")
    assert sorted(row["content"] for row in facts(write(rebuilt, _mail_extract))) == incremental


# ---------- N6. 包重发以后没用了的实体删掉，后来的对齐和整本重建一样 ----------


def _zhou_or_plain(window: Window) -> dict[str, object]:
    text = window.targets[0].text.rstrip("。")
    if "老周" not in text:
        return {"entities": [], "facts": [l3_fact(window, text, text)]}
    name = "周建国" if "周建国" in text else "老周"
    return {"entities": [{"key": "e1", "mention": "老周", "name": name, "type": "人"}], "facts": [l3_fact(window, text, text, subject="e1")]}


def test_entities_left_without_facts_or_aliases_are_dropped(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = day_packet("2026-11-01", "我：老周下周去越南。")
    again = day_packet("2026-11-01", "我：下周开会。").replace("privacy: normal", "privacy: normal\nrevision: 2")
    later = [day_packet("2026-11-05", "我：周建国就是老周，管越南。"), day_packet("2026-11-06", "我：老周明天回来。")]
    deliver(world, first)
    write(world, _zhou_or_plain)
    deliver(world, again)
    for packet in later:
        deliver(world, packet)
        conn = write(world, _zhou_or_plain)
    assert _subjects(conn)["老周明天回来"] == "周建国"

    other = tmp_path / "rebuild"
    other.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(other))
    rebuilt = World(other.resolve(), Settings.from_env())
    deliver(rebuilt, again, *later)
    assert dump(write(rebuilt, _zhou_or_plain), surrogate=False)["entities"] == dump(conn, surrogate=False)["entities"]


# ---------- N7. 冻结的事实说法没了，状态不变 ----------


def test_a_frozen_fact_without_claims_keeps_its_status(world: World) -> None:
    def extract(window: Window) -> dict[str, object]:
        fact = l3_fact(
            window, "越南那批订单 11-15 出货", "越南那批订单 11-15 出货", subject="e1", attribute="出货日期", value="11-15", certainty="inferred"
        )
        return {"entities": [ORDER], "facts": [fact]}

    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    empty = checked_items(world, lambda window: {"entities": [], "facts": []})
    conn = write(world, extract)
    with transaction(conn):
        conn.execute("UPDATE facts SET frozen = 1")
    before = conn.execute("SELECT status, pending_reason FROM facts").fetchone()
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
    for item in empty:
        writer.write_window(item, {})
    assert tuple(conn.execute("SELECT status, pending_reason FROM facts").fetchone()) == tuple(before) == ("inferred", None)


# ---------- N8. 服务每条都拒：停下；只是几条自己的毛病：跳过 ----------


def test_a_service_that_rejects_everything_stops_the_run(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    deliver(world, *(day_packet(f"2026-11-0{day}", f"我：越南那批订单 11-1{day} 出货。") for day in (1, 2, 3)))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    with FakeEmbeddingServer(api_key="k", replies=[400] * 50) as server:
        monkeypatch.setenv("JAVMEM_EMBEDDING_BASE_URL", server.base_url)
        monkeypatch.setenv("JAVMEM_EMBEDDING_API_KEY", "k")
        assert main(["write", "--library", "prod", "--fake"]) == 4
        assert len(server.requests) <= 5  # 一批、拆开的三条、探一次
    conn = open_ledger(world.settings.library("prod"))
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 3  # 账照写了
    assert conn.execute("SELECT count(*) FROM fact_vectors").fetchone()[0] == 0


def test_texts_that_are_rejected_on_their_own_are_skipped() -> None:
    stats = EmbedStats()
    found = embed_all(["甲", "乙", "丙"], FakeEmbeddings(bad_texts={"甲", "乙"}), sleep=lambda _: None, stats=stats)
    assert set(found) == {"丙"} and stats.skipped == 2
    found = embed_all(["甲", "乙"], FakeEmbeddings(bad_texts={"甲", "乙"}), sleep=lambda _: None)  # 探一下服务是好的
    assert found == {}
    with pytest.raises(EmbeddingError) as caught:  # 连探的那一句也被拒：服务配错了
        embed_all(["甲", "乙"], FakeEmbeddings([BAD_REQUEST] * 5), sleep=lambda _: None)
    assert caught.value.kind == BAD_REQUEST


# ---------- N10. 对话和邮件按时间先后写 ----------


def _tran_mai_extract(window: Window) -> dict[str, object]:
    for line in window.targets:
        if "warehouse is full" in line.text:
            entity = {"key": "e1", "mention": "Mai", "name": "Tran Mai", "type": "人"}
            return {"entities": [entity], "facts": [l3_fact(window, "The warehouse is full until Nov 15", "The warehouse is full until Nov 15", subject="e1")]}
        if "仓库要到" in line.text:
            entity = {"key": "e1", "mention": "Mai", "name": "Mai", "type": "人"}
            text = line.text.rstrip("。")
            return {"entities": [entity], "facts": [l3_fact(window, text, text, subject="e1")]}
    return {"entities": [], "facts": []}


def test_chat_and_mail_are_written_in_time_order(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mail = build_mail_packet(sections="## 原话 · 邮件 · 2026-11-03 09:12\n\n" + mail_header_block() + "\n  Mai here. The warehouse is full until Nov 15.\n")
    chat = day_packet("2026-11-04", "我：Mai 说仓库要到 11-15 才空出来。")
    deliver(world, mail, folder="inbox/mail-sales-a")
    write(world, _tran_mai_extract)
    deliver(world, chat)
    by_day = _subjects(write(world, _tran_mai_extract))
    assert set(by_day.values()) == {"Tran Mai"}

    other = tmp_path / "rebuild"
    other.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(other))
    rebuilt = World(other.resolve(), Settings.from_env())
    deliver(rebuilt, mail, folder="inbox/mail-sales-a")
    deliver(rebuilt, chat)
    assert _subjects(write(rebuilt, _tran_mai_extract)) == by_day


# ---------- N11. 一直排队的包，写过的窗口不再重写 ----------


def test_a_window_already_written_is_not_rewritten(world: World) -> None:
    lines = ["- [08:00] 我：老周明天回来。"]
    for index in range(1, 200):
        minute = 8 * 60 + index
        lines.append(f"- [{minute // 60:02d}:{minute % 60:02d}] 我：第 {index} 条备注，这里是一些编出来凑字数的文字，用来让这个包切成两个窗口。")
    long_packet = build_packet(
        source="desk-bot", stamp="2026-11-02_2330", body="## 原话 · 2026-11-02 08:00–11:19\n" + "\n".join(lines) + "\n",
        created_at="2026-11-02T23:30:00+08:00", covers_from="2026-11-02T00:00:00+08:00", covers_to="2026-11-02T23:30:00+08:00",
    )

    def extract(window: Window) -> dict[str, object] | None:
        if not any("老周" in line.text for line in window.targets):
            return None  # 这个包的第二个窗口一直没结果（截断、拒答这类）
        return _person_extract(window)

    deliver(world, day_packet("2026-11-01", "我：周建国就是老周，管越南。"), long_packet)
    write(world, extract)
    deliver(world, day_packet("2026-11-09", "我：周立新也叫老周，管泰国。"))
    write(world, extract)
    conn = write(world, extract)
    row = conn.execute("SELECT status, subject_entity_id FROM facts WHERE content = '老周明天回来'").fetchone()
    assert row["status"] == "verified" and _subjects(conn)["老周明天回来"] == "周建国"
    assert conn.execute("SELECT extraction FROM packets WHERE packet_id = '2026-11-02_2330_desk-bot'").fetchone()[0] == "queued"


# ---------- N12. 数字之间的各种横线是同一个 ----------


def test_dashes_between_digits_are_the_same() -> None:
    assert key_text("11–15") == key_text("11-15") == key_text("11‐15") != key_text("1115")


# ---------- N13. 补向量时事实被删了，不撞外键 ----------


def test_filling_vectors_skips_facts_deleted_meanwhile(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-02", "我：越南那批订单改到 11-20 出货。"))
    conn = write(world, ship_extract)
    with transaction(conn):
        conn.execute("DELETE FROM fact_vectors")
    victim = facts(conn)[0]["fact_id"]

    class Deleting(FakeEmbeddings):
        def embed(self, texts):  # type: ignore[no-untyped-def]
            other = open_ledger(world.settings.library("prod"))
            with transaction(other):
                other.execute("UPDATE facts SET superseded_by = NULL WHERE superseded_by = ?", (victim,))
                other.execute("DELETE FROM facts WHERE fact_id = ?", (victim,))
            other.close()
            return super().embed(texts)

    with VectorCache(world.data / "fresh-vectors") as cache:
        assert fill_vectors(conn, cache, Deleting(), sleep=lambda _: None) == 1


# ---------- 13. 太大的窗口也算重调没用 ----------


def test_a_window_too_big_to_send_counts_as_permanent(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("javmem.pipeline.write.MAX_INPUT_CHARS", 10)
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    items = checked_items(world, lambda window: None)
    conn = open_ledger(world.settings.library("prod"))
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
    with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
        run = run_write(items, writer, store=store, cache=cache, embedder=FakeEmbeddings(), sleep=lambda _: None)
    assert run.permanent == [items[0].window.window_id]


# ---------- N9. 有真向量的账本不让用假向量写 ----------


def test_fake_embeddings_are_refused_on_a_ledger_with_real_vectors(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    with FakeEmbeddingServer(api_key="k") as server:
        monkeypatch.setenv("JAVMEM_EMBEDDING_BASE_URL", server.base_url)
        monkeypatch.setenv("JAVMEM_EMBEDDING_API_KEY", "k")
        assert main(["write", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 2


# ---------- 包重发时文字没变的事实留着向量 ----------


def test_replacement_keeps_the_vector_of_a_fact_whose_text_did_not_change(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-02", "我：越南那批订单 11-15 出货。"))
    conn = write(world, ship_extract)
    assert conn.execute("SELECT count(*) FROM fact_claims").fetchone()[0] == 2
    replaced = day_packet("2026-11-02", "我：越南那批订单的事明天再说。").replace("privacy: normal", "privacy: normal\nrevision: 2")
    deliver(world, replaced)
    conn = open_ledger(world.settings.library("prod"))
    assert conn.execute("SELECT count(*) FROM fact_claims").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM fact_vectors").fetchone()[0] == 1
