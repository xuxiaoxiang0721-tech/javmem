"""8 类成对小句（J2.14）：tests/fixtures/zh_pairs/ 下一类一个 YAML，句子、名字和数都是编的，从头写的。

每组一对（或几个）判断，expect 是规则应该给的结论：
- l4：窗口加一条事实，走 L3 换编号和 L4，pass / block；block 要是 by 里写的规则拦的（别的规则碰巧拦下不算对）；
- entity：两个包各提到一次，L5 是不是对到同一个实体，same / different；
- merge：同一个主体、同一个属性，值换了写法，是不是同一条事实，same / different；
- owner：L3 给的主体写进账本以后是不是“主人”，owner / not_owner；
- search：raw_search 只查这一组的包，hit / miss；
- words：问话按 javmem.zh.query_words 切词、任一词命中（query-api 5.1 回退原文层的办法），hit / miss。

按设计这一段不管的标 bucket: beyond，断言它还是错的：哪天修好了这里会红，提醒把标记去掉。
仓库外有一份用真实句子的（组号和要测的现象一样，句子各写各的，这里的不是拿那份换名字换数），跑法一样，数在 J2.14 的报告里。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from helpers import World, build_packet, file_name

from javmem.embedding import FakeEmbeddings
from javmem.ledger import open_ledger
from javmem.llm import FakeModel
from javmem.packet import parse_packet
from javmem.pipeline import write as write_module
from javmem.pipeline.batch import load_queued
from javmem.pipeline.extract import map_output, prepare
from javmem.pipeline.merge import OWNER_NAME, entity_id_of
from javmem.pipeline.normalize import normalize
from javmem.pipeline.render import render_window
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.verify import Checked, fold, verify_window
from javmem.pipeline.window import build_windows
from javmem.raw.search import raw_search
from javmem.redaction import default_patterns
from javmem.zh import query_words
from javmem.zh.lexicon import load_lexicon

PATTERNS = default_patterns()
FIXTURES = Path(__file__).parent / "fixtures" / "zh_pairs"
LEXICON = Path(__file__).parent / "fixtures" / "zh_lexicon.yaml"
DEFAULT_DAY = "2026-11-10"  # 星期二
OWNER_ID = entity_id_of("人", OWNER_NAME)
EDGES = "。，！？；：.,!?;:"
DATA_KINDS = ("entity", "merge", "owner", "search", "words")  # 要收包、写账的


def _load() -> list[tuple[str, dict, int, dict]]:
    cases = []
    for path in sorted(FIXTURES.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for group in data["groups"]:
            for number, case in enumerate(group["cases"], 1):
                cases.append((data["category"], group, number, case))
    return cases


CASES = _load()


def _as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _packet(day: str, lines: list[str], *, stamp: str | None = None) -> str:
    body = f"## 原话 · {day} 10:30–10:59\n" + "".join(f"- [10:{30 + index:02d}] {line}\n" for index, line in enumerate(lines))
    return build_packet(
        stamp=stamp or f"{day}_2330",
        body=body,
        created_at=f"{day}T23:30:00+08:00",
        covers_from=f"{day}T00:00:00+08:00",
        covers_to=f"{day}T23:30:00+08:00",
    )


def _ref(window, rendered, quote: str) -> str:
    """整句就是引文的那一句优先（“能。”不要找到“周三能交货吗？”上），再找包含它的第一句。"""
    needle = fold(quote).strip(EDGES)
    targets = [line for line in window.lines if line.target]
    for line in targets:
        if fold(rendered.shown.get(line.ref, "")).strip(EDGES) == needle:
            return line.ref
    for line in targets:
        if needle in fold(rendered.shown.get(line.ref, "")):
            return line.ref
    raise AssertionError(f"测试集写错了：引文在窗口里找不到 {quote}")


def _l4(group: dict, case: dict) -> tuple[str, list[str]]:
    raw = _packet(case.get("day") or group.get("day") or DEFAULT_DAY, case.get("lines") or group["lines"])
    packet = normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))
    window = build_windows([packet])[0]
    rendered = render_window(window, PATTERNS)
    quotes = _as_list(case.get("quote")) or [case["text"]]
    item = {
        "text": case["text"],
        "subject": None,
        "attribute": case.get("attribute"),
        "value": case.get("value"),
        "occurred_at": None,
        "time_precision": "unknown",
        "certainty": case.get("certainty", "stated"),
        "entities": [],
        "evidence": [{"ref": _ref(window, rendered, quote), "quote": quote} for quote in quotes],
    }
    extraction = map_output({"entities": [], "facts": [item]}, window, rendered.redacted_refs)
    result = verify_window(window, rendered, extraction).facts[0]
    codes = [finding.as_text() for finding in result.failures]
    assert not any(code.startswith(("G1", "L3")) for code in codes), f"测试集写错了：{codes}"
    return ("pass" if not codes else "block"), codes


def _one_fact(window, rendered, spec: dict, entity: dict | None) -> dict:
    line = next(line for line in window.lines if line.target)
    quotes = _as_list(spec.get("quote")) or [line.text.rstrip("。.!！")]
    fact = {
        "text": spec.get("text") or line.text.rstrip("。.!！"),
        "subject": "e1" if entity else None,
        "attribute": spec.get("attribute"),
        "value": spec.get("value"),
        "occurred_at": None,
        "time_precision": "unknown",
        "certainty": spec.get("certainty", "stated"),
        "entities": [{"key": "e1", "role": "subject"}] if entity else [],
        "evidence": [{"ref": _ref(window, rendered, quote), "quote": quote} for quote in quotes],
    }
    return {"entities": [{"key": "e1", **entity}] if entity else [], "facts": [fact]}


def _write(world: World, tmp: Path, extract) -> object:
    batch = load_queued(world.settings, "prod")
    items = []
    for window, rendered, key in prepare(build_windows(batch.packets, context_pool=batch.context), FakeModel(), PATTERNS):
        extraction = map_output(extract(window, rendered), window, rendered.redacted_refs)
        items.append(Checked(window, rendered, key, extraction, verify_window(window, rendered, extraction)))
    conn = open_ledger(world.settings.library("prod"))
    options = write_module.writer_options(world.settings)
    writer = write_module.LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model=FakeEmbeddings().name, **options)
    with ResultStore(tmp / "l3") as store, VectorCache(tmp / "vectors") as cache:
        write_module.run_write(
            items,
            writer,
            packets=[(packet.packet_id, packet.header.revision) for packet in batch.packets],
            store=store,
            cache=cache,
            embedder=FakeEmbeddings(),
            sleep=lambda _: None,
        )
    return conn


def _claim(conn, packet_id: str) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT fact_id, subject_entity_id FROM fact_claims WHERE window_id LIKE ? ORDER BY claim_id", (packet_id + "#%",)
    ).fetchone()
    assert row is not None, f"测试集写错了：{packet_id} 的事实没写进账本"
    return row["fact_id"], row["subject_entity_id"]


def _pair(world: World, tmp: Path, group: dict, case: dict, kind: str) -> str:
    first = group["first"]
    first_day, second_day = first.get("day", "2026-11-02"), case.get("day", "2026-11-06")
    for day, spec in ((first_day, first), (second_day, case)):
        world.drop("inbox/desk-bot", _packet(day, [spec["line"]]))
    world.intake()
    specs = {f"{first_day}_2330_desk-bot": first, f"{second_day}_2330_desk-bot": case}

    def extract(window, rendered):
        spec = dict(specs[window.lines[0].packet_id])
        if kind == "merge":
            subject = group["subject"]
            spec.setdefault("attribute", group.get("attribute"))
            entity = {"name": subject["name"], "mention": subject.get("mention", subject["name"]), "type": subject["type"]}
        else:
            entity = {"name": spec["name"], "mention": spec.get("mention", spec["name"]), "type": group["type"]}
        return _one_fact(window, rendered, spec, entity)

    conn = _write(world, tmp, extract)
    try:
        fact_a, subject_a = _claim(conn, f"{first_day}_2330_desk-bot")
        fact_b, subject_b = _claim(conn, f"{second_day}_2330_desk-bot")
    finally:
        conn.close()
    if kind == "entity":
        return "same" if subject_a is not None and subject_a == subject_b else "different"
    return "same" if fact_a == fact_b else "different"


def _owner(world: World, tmp: Path, group: dict, case: dict) -> str:
    day = case.get("day") or group.get("day") or DEFAULT_DAY
    world.drop("inbox/desk-bot", _packet(day, case.get("lines") or group["lines"]))
    world.intake()
    conn = _write(world, tmp, lambda window, rendered: _one_fact(window, rendered, case, dict(case["entity"])))
    try:
        _, subject = _claim(conn, f"{day}_2330_desk-bot")
    finally:
        conn.close()
    return "owner" if subject == OWNER_ID else "not_owner"


def _lines_hit(world: World, packet_id: str, word: str) -> set[int]:
    settings = replace(world.settings, caller="owner-console")
    result = raw_search({"query": word, "packet_id": packet_id, "limit": 100}, settings=settings)
    if not result["ok"]:
        return set()
    return {int(match.group(1)) for item in result["items"] if (match := re.search(r"#\d+\.(\d+)\.\d+$", item["id"]))}


def _search(world: World, tmp: Path, group: dict, case: dict, kind: str) -> list[tuple[int, str, str]]:
    day = group.get("day") or DEFAULT_DAY
    world.drop("inbox/desk-bot", _packet(day, group["lines"]))
    world.intake()
    packet_id = f"{day}_2330_desk-bot"
    if kind == "search":
        words = [case["query"]]
    else:
        words = query_words(case["query"], load_lexicon(LEXICON).words, cache_dir=tmp / "jieba")
    found: set[int] = set()
    for word in words:
        found |= _lines_hit(world, packet_id, word)
    expected = [(line, "hit") for line in _as_list(case.get("hit"))] + [(line, "miss") for line in _as_list(case.get("miss"))]
    return [(line, expect, "hit" if line in found else "miss") for line, expect in expected]


@pytest.mark.parametrize(
    ("category", "group", "number", "case"),
    CASES,
    ids=[f"{group['id']}-{number}" for _, group, number, _ in CASES],
)
def test_pair(request: pytest.FixtureRequest, tmp_path: Path, category: str, group: dict, number: int, case: dict) -> None:
    kind = group["kind"]
    if kind == "l4":
        actual, codes = _l4(group, case)
        if actual == "block" and case["expect"] == "block" and (case.get("by") or group.get("by")):
            allowed = _as_list(case.get("by") or group.get("by"))
            if not any(code.split(":")[0] in allowed for code in codes):
                actual = f"block by {codes}"  # 不是对口的规则拦的：不算对
        outcomes = [(None, case["expect"], actual)]
    else:
        world: World = request.getfixturevalue("world")
        world = replace(world, settings=replace(world.settings, lexicon_file=LEXICON))
        if kind in ("entity", "merge"):
            outcomes = [(None, case["expect"], _pair(world, tmp_path, group, case, kind))]
        elif kind == "owner":
            outcomes = [(None, case["expect"], _owner(world, tmp_path, group, case))]
        else:
            outcomes = _search(world, tmp_path, group, case, kind)
    wrong = [(line, expect, actual) for line, expect, actual in outcomes if expect != actual]
    if (case.get("bucket") or group.get("bucket")) == "beyond":
        assert wrong, f"{category} {group['id']}-{number} 标了 beyond，现在却对了：把标记去掉"
    else:
        assert not wrong, f"{category} {group['id']}-{number}（{group.get('why')}）：{wrong}"


def test_every_category_is_here() -> None:
    categories = {category for category, _, _, _ in CASES}
    assert len(categories) == 8
    assert {group["kind"] for _, group, _, _ in CASES} == {"l4", *DATA_KINDS}
