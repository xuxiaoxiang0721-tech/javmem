"""在写过账的账本上找软变化的候选（J4.5，javmem.soft.find）：哪些句子算候选、配上哪条旧的、可见范围不同的不配。
包和 L3 的结果都是编的，向量用假的。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from helpers import World, build_packet

from javmem.embedding import FakeEmbeddings
from javmem.ledger import open_ledger
from javmem.llm import FakeModel
from javmem.pipeline.batch import load_queued
from javmem.pipeline.extract import map_output, prepare
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.verify import NO_RESULT, Checked, verify_window
from javmem.pipeline.window import Window, build_windows
from javmem.pipeline.write import LedgerWriter, run_write
from javmem.redaction import default_patterns
from javmem.soft.find import find_cases

Extract = Callable[[Window], Mapping[str, object] | None]
EAST = {"key": "e1", "mention": "东湖那批订单", "name": "东湖那批订单", "type": "订单或合同"}
SOUTH = {"key": "e2", "mention": "南港那张合同", "name": "南港那张合同", "type": "订单或合同"}


def day_packet(day: str, *lines: str, domain: str = "[business]", **fields: Any) -> str:
    body = f"## 原话 · {day} 10:30–10:59\n" + "".join(f"- [10:{30 + n:02d}] {line}\n" for n, line in enumerate(lines))
    return build_packet(source="desk-bot", stamp=f"{day}_2330", body=body, created_at=f"{day}T23:30:00+08:00",
                        covers_from=f"{day}T00:00:00+08:00", covers_to=f"{day}T23:30:00+08:00", domain=domain, **fields)


def fact(window: Window, phrase: str, text: str, **fields: object) -> dict[str, object]:
    ref = next(line.ref for line in window.lines if line.target and phrase in line.text)
    item: dict[str, object] = {"text": text, "subject": None, "attribute": None, "value": None, "occurred_at": None,
                               "time_precision": "unknown", "certainty": "stated", "entities": [],
                               "evidence": [{"ref": ref, "quote": phrase}]}
    item.update(fields)
    if item["subject"] is not None and not item["entities"]:
        item["entities"] = [{"key": item["subject"], "role": "subject"}]
    return item


def extract(window: Window) -> dict[str, object]:
    """编的 L3：认得几句话，别的不抽。"""
    facts = []
    for line in window.targets:
        if "11-20 出货" in line.text:
            facts.append(fact(window, "东湖那批订单 11-20 出货", "东湖那批订单出货日期是 11-20", subject="e1",
                              attribute="出货日期", value="11-20"))
        if "可能改到 11-25" in line.text:
            facts.append(fact(window, "东湖那批订单可能改到 11-25 出货", "东湖那批订单出货日期可能改到 11-25", subject="e1",
                              attribute="出货日期", value="11-25", certainty="vague"))
        if "在走签字流程" in line.text:
            facts.append(fact(window, "南港那张合同在走签字流程", "南港那张合同在走签字流程", subject="e2",
                              attribute="状态", value="在走签字流程"))
    return {"entities": [EAST, SOUTH], "facts": facts}


def write(world: World, extract: Extract) -> sqlite3.Connection:
    patterns = default_patterns()
    batch = load_queued(world.settings, "prod")
    items = []
    for window, rendered, key in prepare(build_windows(batch.packets), FakeModel(), patterns):
        raw = extract(window)
        if raw is None:
            items.append(Checked(window, rendered, key, None, None, NO_RESULT))
            continue
        extraction = map_output(raw, window, rendered.redacted_refs)
        items.append(Checked(window, rendered, key, extraction, verify_window(window, rendered, extraction)))
    conn = open_ledger(world.settings.library("prod"))
    embedder = FakeEmbeddings()
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model=embedder.name)
    with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
        run_write(items, writer, packets=[(p.packet_id, p.header.revision) for p in batch.packets], store=store, cache=cache,
                  embedder=embedder, sleep=lambda _: None)
    return conn


def deliver(world: World, *packets: str) -> None:
    for packet in packets:
        world.drop("inbox/desk-bot", packet)
    world.intake()


def build(world: World) -> sqlite3.Connection:
    deliver(
        world,
        day_packet("2026-11-01", "我：东湖那批订单 11-20 出货。", "我：南港那张合同在走签字流程。"),
        day_packet("2026-11-05", "我：东湖那批订单可能改到 11-25 出货。", "我：南港那张合同先放放。",
                   "我：如果客户同意，北岸那批就提前。", "desk-bot：好的。"),
    )
    return write(world, extract)


def by_sentence(cases) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {case.target_lines[0].text: case for case in cases}


def test_vague_claims_pair_with_the_older_fact_in_the_same_group(world: World) -> None:
    conn = build(world)
    result = find_cases(conn)
    case = by_sentence(result.cases)["东湖那批订单可能改到 11-25 出货。"]
    (old,) = case.old  # type: ignore[attr-defined]
    assert old.kind == "fact" and "11-20" in old.text and old.last_said == "2026-11-01"
    assert case.meta["olds_by"] == {"O1": "group"}  # type: ignore[attr-defined]
    assert "可能" in case.meta["groups"]  # type: ignore[attr-defined]
    texts = [line.text for line in case.lines]  # type: ignore[attr-defined]
    assert "东湖那批订单 11-20 出货。" in texts  # 旧的那条最后一次被说的那一行
    assert all(line.sid for line in case.lines)  # type: ignore[attr-defined]


def test_strong_hedges_pair_by_the_entity_they_mention(world: World) -> None:
    conn = build(world)
    case = by_sentence(find_cases(conn).cases)["南港那张合同先放放。"]
    (old,) = case.old  # type: ignore[attr-defined]
    assert "签字流程" in old.text and case.meta["olds_by"] == {"O1": "entity"}  # type: ignore[attr-defined]
    assert case.packet_date == "2026-11-05" and not case.private  # type: ignore[attr-defined]


def test_weak_words_alone_with_nothing_to_change_make_no_case(world: World) -> None:
    conn = build(world)
    result = find_cases(conn)
    assert "我：如果客户同意，北岸那批就提前。" not in by_sentence(result.cases)
    assert "如果客户同意，北岸那批就提前。" not in by_sentence(result.cases)
    assert result.stats["no_old"] >= 1 and result.stats["cases"] == 2


def test_finding_twice_gives_the_same_cases(world: World) -> None:
    conn = build(world)
    first, second = find_cases(conn), find_cases(conn)
    assert [case.as_dict() for case in first.cases] == [case.as_dict() for case in second.cases]
    assert find_cases(conn, limit=1).stats["cases"] == 1


def test_other_scopes_are_not_paired(world: World) -> None:
    """可见范围不同的不配（D30）：生活分区里说的“先放放”，配不上生意分区的那张合同。"""
    deliver(
        world,
        day_packet("2026-11-01", "我：南港那张合同在走签字流程。"),
        day_packet("2026-11-06", "我：南港那张合同先放放。", domain="[personal]"),
    )
    conn = write(world, extract)
    assert find_cases(conn).cases == []


def test_private_packets_do_not_pair_with_normal_ones(world: World) -> None:
    deliver(
        world,
        day_packet("2026-11-01", "我：南港那张合同在走签字流程。"),
        day_packet("2026-11-06", "我：南港那张合同先放放。", privacy="private"),
    )
    conn = write(world, extract)
    assert find_cases(conn).cases == []


def test_only_the_named_packets_are_scanned(world: World) -> None:
    conn = build(world)
    assert find_cases(conn, packets=["2026-11-01_2330_desk-bot"]).cases == []
    assert len(find_cases(conn, packets=["2026-11-05_2330_desk-bot"]).cases) == 2


def test_relations_are_paired_and_come_first(world: World) -> None:
    """J4.4 写了关系以后：按实体配的旧东西里，关系排在事实前面，带着状态。这里直接往账本里插一条关系（编的）。"""
    conn = build(world)
    south = conn.execute("SELECT entity_id FROM entities WHERE canonical_name = '南港那张合同'").fetchone()[0]
    owner = conn.execute("SELECT entity_id FROM entities WHERE canonical_name = '主人'").fetchone()
    owner_id = owner[0] if owner else south
    sentence = conn.execute("SELECT sentence_id, packet_id, revision FROM raw_sentences WHERE text LIKE '南港那张合同在走%'").fetchone()
    scope = conn.execute("SELECT scope_partitions, scope_sensitive FROM facts LIMIT 1").fetchone()
    conn.execute(
        "INSERT INTO relations (relation_id, subject_entity_id, relation_type, object_entity_id, state, status, scope_partitions,"
        " scope_sensitive, merge_key, created_at, updated_at) VALUES ('R1', ?, '持有', ?, 'active', 'verified', ?, ?, 'k1', 'x', 'x')",
        (owner_id, south, scope[0], scope[1]),
    )
    conn.execute(
        "INSERT INTO relation_evidence (relation_id, packet_id, revision, sentence_id, quote, position, created_at)"
        " VALUES ('R1', ?, ?, ?, '南港那张合同', '{}', 'x')",
        (sentence["packet_id"], sentence["revision"], sentence["sentence_id"]),
    )
    conn.commit()
    case = by_sentence(find_cases(conn).cases)["南港那张合同先放放。"]
    assert [item.kind for item in case.old][:1] == ["relation"]  # type: ignore[attr-defined]
    assert case.old[0].state == "生效" and case.old[0].id == "R1" and case.old[0].key == "k1"  # type: ignore[attr-defined]
    assert "持有" in case.old[0].text and "南港那张合同" in case.old[0].text  # type: ignore[attr-defined]


# ---------- 审阅以后补的（先红后修） ----------


def test_withdrawn_mail_senders_are_masked(world: World) -> None:
    """按事实撤回了发件人那一句（D73 第 16 条），信里别的句子还在：说话人照 L1 写成“[已撤回]”，不发给模型。"""
    from helpers import build_mail_packet, mail_header_block

    from javmem.ledger import connect_readonly
    from javmem.soft.judge import render_case

    first = mail_header_block(message_id='"<m1@example.org>"', thread_id='"<m1@example.org>"',
                              **{"from": '{name: "Tran Mai", addr: "tran.mai@example.org"}'},
                              date="2026-11-03T09:00:00+08:00", subject='"Contract"')
    second = mail_header_block(message_id='"<m2@example.org>"', thread_id='"<m1@example.org>"',
                               in_reply_to='"<m1@example.org>"',
                               **{"from": '{name: "Bob Lin", addr: "bob.lin@example.org"}'},
                               date="2026-11-03T15:00:00+08:00", subject='"Re: Contract"')
    sections = (f"## 原话 · 邮件 · 2026-11-03 09:00\n\n{first}\n  南港那张合同在走签字流程。\n\n"
                f"## 原话 · 邮件 · 2026-11-03 15:00\n\n{second}\n  南港那张合同先放放。\n")
    world.drop("inbox/mail-sales-a", build_mail_packet(sections=sections))
    world.intake()
    conn = write(world, extract)
    # 只撤回了第二封的发件人那一句、信的正文还在（按事实撤回一条拿发件人那一句当证据的事实时就是这样）
    conn.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-16T10:00:00+08:00'"
                 " WHERE field = 'from' AND message_id = '<m2@example.org>'")
    conn.commit()
    conn.close()  # （按句子撤回发件人那一句，撤回命令会把整封信撤掉；这里要的是“发件人撤了、正文还在”的样子）
    ledger = world.settings.library("prod").db_path
    conn = connect_readonly(ledger)
    try:
        case = by_sentence(find_cases(conn).cases)["南港那张合同先放放。"]
    finally:
        conn.close()
    assert case.target_lines[0].speaker == "[已撤回]"  # type: ignore[attr-defined]
    assert "Bob Lin" not in render_case(case).text  # type: ignore[arg-type]
    assert "Tran Mai" in render_case(case).text  # type: ignore[arg-type]  没撤回的那封照常写发件人


def test_case_ids_do_not_depend_on_the_hash_seed(world: World) -> None:
    """一条旧东西从两个实体都配得上时，题号、旧的先后不能跟着 Python 的哈希种子变（不然同一道题换个进程就要重新付钱判）。"""
    import json
    import os
    import subprocess
    import sys

    person = {"key": "e3", "mention": "林工", "name": "林工", "type": "人"}

    def two_entities(window: Window) -> dict[str, object]:
        facts = []
        for line in window.targets:
            if "负责东湖那批订单的跟单" in line.text:
                facts.append(fact(window, "林工负责东湖那批订单的跟单", "林工负责东湖那批订单的跟单", subject="e3",
                                  attribute="负责", value="东湖那批订单的跟单",
                                  entities=[{"key": "e3", "role": "subject"}, {"key": "e1", "role": "object"}]))
            if "要先放样" in line.text:
                facts.append(fact(window, "东湖那批订单那边要先放样", "东湖那批订单那边要先放样", subject="e1",
                                  attribute="要求", value="先放样"))
        return {"entities": [EAST, person], "facts": facts}

    deliver(world, day_packet("2026-11-01", "我：林工负责东湖那批订单的跟单。", "我：东湖那批订单那边要先放样。"),
            day_packet("2026-11-05", "我：林工那边东湖那批订单先放放。"))
    write(world, two_entities).close()
    script = ("import json, sys\nfrom javmem.ledger import connect_readonly\nfrom javmem.soft.find import find_cases\n"
              "conn = connect_readonly(__import__('pathlib').Path(sys.argv[1]))\n"
              "print(json.dumps([[c.id, [o.id for o in c.old]] for c in find_cases(conn).cases]))\n")
    ledger = str(world.settings.library("prod").db_path)
    runs = [subprocess.Popen([sys.executable, "-c", script, ledger], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONHASHSEED": str(seed)}) for seed in range(8)]
    results = {runs[n].communicate(timeout=120)[0] for n in range(8)}
    assert len(results) == 1, results
    assert json.loads(results.pop())  # 有题


def test_latin_names_with_spaces_are_found() -> None:
    """英文名字中间有空格（“Owen Lee”）：句子里照样认得；两个字母的整词认，不算进长词里。"""
    from javmem.soft.find import _Names

    names = _Names([("Owen Lee", "E1"), ("AQ", "E2"), ("南港那张合同", "E3")])
    assert names.find("Owen Lee 那边的报价先放放。") == {"E1"}
    assert names.find("owen  lee 先不管。") == {"E1"}
    assert names.find("AQUA 那张先放放") == set() and names.find("AQ 先放放") == {"E2"}
    assert names.find("南港那张合同先放放。") == {"E3"}


def test_a_ledger_copy_opened_read_only(world: World, tmp_path: Path) -> None:
    from javmem.ledger import connect_readonly

    conn = build(world)
    conn.close()
    readonly = connect_readonly(world.settings.library("prod").db_path)
    try:
        assert len(find_cases(readonly).cases) == 2
    finally:
        readonly.close()
