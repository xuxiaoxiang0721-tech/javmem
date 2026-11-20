"""打分（J3.8）：金标准的读法、审阅结果、两边的实体对齐、事实、关系、状态变化、别名、成本、报告隐去是哪一边、
给写提示词的那份不带金标准。名字、句子都是编的。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from helpers import MAIL_ID, MAIL_PACKET, World, build_packet

from javmem.cli import main
from javmem.ledger import open_ledger
from javmem.llm import FakeModel
from javmem.pipeline.batch import load_queued
from javmem.pipeline.extract import map_output, prepare
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.verify import Checked, verify_window
from javmem.pipeline.window import build_windows
from javmem.pipeline.write import LedgerWriter, run_write
from javmem.embedding import FakeEmbeddings
from javmem.redaction import default_patterns
from javmem.scoring.align import add_bridges, align, gold_index, lexicon_groups, table_groups
from javmem.scoring.common import FORMAT, Ref, ScoreInputError, When, side_from_dict
from javmem.scoring.dates import close, interval
from javmem.scoring.gold import (
    SentenceIndex,
    apply_review,
    gold_relations,
    is_not_labeled,
    kinship,
    load_gold,
    parse_unit,
    round1_ids,
    unit_files,
)
from javmem.scoring.ledger import packet_costs, sentences_from_ledger, side_from_ledger
from javmem.scoring.match import kin_ok, relation_changes
from javmem.scoring.report import Options, bridge_groups, metrics, score, write_outputs
from javmem.scoring.results import mail_sentences, side_from_result
from javmem.zh.lexicon import load_lexicon

P = "2026-11-01_2330_desk-bot"
Q = "2026-11-05_2330_desk-bot"
MAIL = "2026-11-02_1800_mail-sales-a"


def sid(tail: str, packet: str = P) -> str:
    return f"{packet}#{tail}"


def ev(tail: str, packet: str = P, role: str | None = None, sentence: str = "") -> dict[str, object]:
    out: dict[str, object] = {"sid": sid(tail, packet), "quote": "", "sentence": sentence or f"编的第 {tail} 句。"}
    if role:
        out["role"] = role
    return out


def item(item_id: str, kind: str, subject: tuple[str, str], evidence: list[dict[str, object]], **fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": item_id, "type": kind, "content": fields.pop("content", f"编的条目 {item_id}。"),
        "subject": {"name": subject[0], "type": subject[1]}, "evidence": evidence,
        "when": fields.pop("when", {"from": None, "to": None, "precision": "unknown", "basis": "不清楚"}),
        "certainty": "原话", "speaker": "主人", "flags": fields.pop("flags", []), "refs": [], "unsure": False,
        "important": False, "note": "",
    }
    base.update(fields)
    return base


def unit(name: str, packets: list[str], items: list[dict[str, object]], kind: str = "dialog", **extra: object) -> dict[str, object]:
    return {"schema": "javmem-gold-prelabel/1", "unit": name, "unit_kind": kind, "title": "编的单元", "packets": packets,
            "revisions": {p: 1 for p in packets}, "not_labeled": extra.pop("not_labeled", []),
            "new_entities": extra.pop("new_entities", []), "items": items, **extra}


OWNER = ("主人", "人")
FAR = ("远帆", "公司或机构")
FAR_ADR = ("远帆 ADR", "资产")
HE = ("林小禾", "人")


def gold_units() -> list[dict[str, object]]:
    u1 = unit("U1", [P], [
        item("U1-F001", "fact", FAR, [ev("1.1.1")], fact_class="事实", attribute="出货日期", value="2026-11-15",
             content="远帆的出货日期定在 2026-11-15。"),
        item("U1-F002", "fact", OWNER, [ev("1.4.1")], fact_class="主人的偏好和规矩", attribute="报价格式",
             value="先给草稿再外发", content="主人的规矩：报价先给草稿再外发。"),
        item("U1-F003", "fact", FAR, [ev("1.5.1")], fact_class="事实", attribute="付款方式", value="电汇",
             flags=["only_in_quote"]),
        item("U1-R001", "relation", OWNER, [ev("1.6.1")], relation="持有", object={"name": FAR_ADR[0], "type": FAR_ADR[1]},
             fields={}, state="生效", when={"from": "2026-10-13", "to": None, "precision": "day", "basis": "交割日"}),
        item("U1-R002", "relation", HE, [ev("1.7.1")], relation="亲属", object={"name": "主人", "type": "人"},
             fields={"kinship": "妻子"}, state="生效"),
        item("U1-S101", "change", FAR_ADR, [ev("1.2.1", role="before"), ev("1.3.1", role="after")],
             content="远帆 ADR 的报价从 12.5 改成 13.0。",
             change={"kind": "价格变了", "soft": False, "target": "报价", "before": "12.5", "after": "13.0", "is_change": True},
             when={"from": "2026-11-01 10:32", "to": None, "precision": "minute", "basis": "说话那天"}),
        item("U1-S102", "change", OWNER, [ev("1.8.1", role="after")], content="持有远帆 ADR 只是说明白了。",
             change={"kind": "说明白了（不算变化）", "soft": False, "target": "持有", "before": "持有", "after": "持有",
                     "is_change": False},
             when={"from": "2026-11-01 10:37", "to": None, "precision": "minute", "basis": "说话那天"}, links=["U1-R001"]),
        item("U1-A001", "alias", FAR, [ev("1.1.1")], alias={"mention": "远帆有限公司", "entity": "远帆", "entity_type": "公司或机构", "kind": "别名"}),
        item("U1-A002", "alias", FAR, [ev("1.9.1")], alias={"mention": "远帆物流", "entity": "远帆", "entity_type": "公司或机构", "kind": "不是同一个"}),
        item("U1-A003", "alias", HE, [ev("1.7.1")], alias={"mention": "小禾", "entity": "林小禾", "entity_type": "人", "kind": "简称"}),
    ], not_labeled=[{"range": ["2.1.1", "2.3.1"], "reason": "编的：记忆系统自己的活"}])
    u2 = unit("U2", [Q], [
        item("U2-R001", "relation", OWNER, [ev("1.3.1", Q)], relation="持有", object={"name": FAR_ADR[0], "type": FAR_ADR[1]},
             fields={}, state="生效", flags=["追加证据"], when={"from": "2026-10", "to": None, "precision": "month", "basis": "约"}),
        item("U2-F001", "fact", FAR_ADR, [ev("1.1.1", Q)], fact_class="和主人有关的行情", attribute="收盘价", value="13.2",
             content="远帆 ADR 收盘 13.2。"),
        item("U2-S201", "change", FAR_ADR, [ev("1.2.1", Q, role="after")], content="远帆 ADR 的收盘价只是说明白了。",
             change={"kind": "说明白了（不算变化）", "soft": False, "target": "收盘价", "before": "13.2", "after": "13.2",
                     "is_change": False},
             when={"from": "2026-11-05 10:31", "to": None, "precision": "minute", "basis": "说话那天"}),
    ])
    m9 = unit("M9", [MAIL], [item("M9-F001", "fact", FAR, [ev("1.1.1", MAIL)], fact_class="事实")], kind="mail_thread",
              thread_id="<thread@example.org>")
    return [u1, u2, m9]


ENTITY_TABLE = {
    "schema": "javmem-gold-entities/1",
    "entities": [
        {"name": "主人", "type": "人", "aliases": ["包里说话人“我”"], "from": "底表"},
        {"name": "远帆", "type": "公司或机构", "aliases": ["远帆集团"], "from": "底表"},
        {"name": "远帆 ADR", "type": "资产", "aliases": ["YF"], "from": "底表"},
        {"name": "林小禾", "type": "人", "aliases": [], "from": "底表"},
    ],
}


def side_dict(name: str = "编的一边") -> dict[str, object]:
    return {
        "format": FORMAT,
        "side": name,
        "packets": [P, Q],
        "entities": [
            {"id": "e1", "name": "主人", "type": "人"},
            {"id": "e2", "name": "远帆", "type": "公司或机构", "aliases": ["远帆有限公司"]},
            {"id": "e3", "name": "远帆ADR（YF）", "type": "资产"},
            {"id": "e4", "name": "林小禾", "type": "人"},
            {"id": "e5", "name": "远帆物流", "type": "公司或机构"},
            {"id": "e6", "name": "某个编的基金", "type": "资产"},
        ],
        "facts": [
            {"id": "f1", "text": "远帆的出货日期是 2026-11-15。", "subject": "e2", "attribute": "出货日期", "value": "2026-11-15",
             "evidence": [sid("1.1.1")]},
            {"id": "f2", "text": "主人早上喝了一杯热的豆浆。", "subject": "e1", "evidence": [sid("1.10.1")]},
            {"id": "f3", "text": "不标的那一段里的一句。", "evidence": [sid("2.2.1")]},
            {"id": "f4", "text": "远帆 ADR 的报价是 12.5。", "subject": "e3", "attribute": "报价", "value": "12.5",
             "evidence": [sid("1.2.1")], "state": "被替代", "superseded_by": "f5"},
            {"id": "f5", "text": "远帆 ADR 的报价改成 13.0。", "subject": "e3", "attribute": "报价", "value": "13.0",
             "evidence": [sid("1.3.1")]},
            {"id": "f6", "text": "远帆 ADR 收盘 13.2。", "subject": "e3", "attribute": "收盘价", "value": "13.2",
             "evidence": [sid("1.1.1", Q)]},
            {"id": "f7", "text": "远帆 ADR 的收盘价记的是 13.1。", "subject": "e3", "attribute": "收盘价记录", "value": "13.1",
             "evidence": [sid("1.1.5", Q)], "state": "被替代", "superseded_by": "f8"},
            {"id": "f8", "text": "远帆 ADR 的收盘价记录改成 13.2。", "subject": "e3", "attribute": "收盘价记录", "value": "13.2",
             "evidence": [sid("1.2.1", Q)]},
        ],
        "relations": [
            {"id": "r1", "subject": "e1", "relation": "持有", "object": "e3",
             "when": {"from": "2026-10-14", "to": None, "precision": "day"}, "evidence": [sid("1.6.1")]},
            {"id": "r2", "subject": "e1", "relation": "亲属", "object": "e4", "fields": {"kinship": "丈夫"},
             "evidence": [sid("1.7.1")]},
            {"id": "r3", "subject": "e3", "relation": "持有", "object": "e1", "evidence": [sid("1.6.1")]},
            {"id": "r4", "subject": "e1", "relation": "持有", "object": "e6", "evidence": [sid("1.6.1")]},
            {"id": "r5", "subject": "e1", "relation": "持有", "object": "e3",
             "when": {"from": "2026-12-01", "to": None, "precision": "day"}, "evidence": [sid("1.3.1", Q)]},
        ],
    }


@pytest.fixture
def gold_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "gold"
    folder.mkdir()
    for data in gold_units():
        (folder / f"{data['unit']}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (folder / "实体表.json").write_text(json.dumps(ENTITY_TABLE, ensure_ascii=False), encoding="utf-8")
    return folder


def load(gold_dir: Path, review: Path | None = None):
    return load_gold(unit_files(gold_dir), gold_dir / "实体表.json", review)


# ---------- 日子 ----------


def test_dates_cover_precision_timezone_and_tolerance() -> None:
    assert interval("2026-10-23", "day") == interval("2026-10-23 16:54", "minute")
    first, last = interval("2026-10", "month")  # type: ignore[misc]
    assert (first.day, last.day) == (1, 31)
    assert interval("2026-10-23", "month") == interval("2026-10", "month")  # 精度粗的按精度
    assert interval("2026-Q4", "quarter")[0].month == 10  # type: ignore[index]
    assert interval("2026-10-23T23:30+00:00", "minute")[0].day == 24  # type: ignore[index]  # 换成北京时间
    assert interval(None, "day") is None and interval("2026-10-23", "unknown") is None and interval("不清楚", "day") is None
    day = When("2026-10-13", None, "day")
    assert close(day, When("2026-10-16", None, "day"), 3) is True
    assert close(day, When("2026-10-17", None, "day"), 3) is False
    assert close(day, When("2026-10", None, "month"), 0) is True
    assert close(day, When(None, None, "unknown"), 3) is None


# ---------- 金标准 ----------


def test_round1_takes_asked_unsure_important_and_a_fixed_tenth() -> None:
    raw = [item(f"X-F{n:03d}", "fact", OWNER, [ev("1.1.1")]) for n in range(60)]
    raw[0]["important"] = True
    raw[1]["unsure"] = True
    raw[2]["note"] = "问 主人：两种标法"
    raw[3]["note"] = "问“这张单子现在怎么样”，答的是到期日"  # 不是要问人的
    parsed = parse_unit(unit("X", [P], raw))
    important, sample = round1_ids(parsed.items)
    assert important == {"X-F000", "X-F001", "X-F002"}
    expected = {f"X-F{n:03d}" for n in range(3, 60) if int(hashlib.sha256(f"X-F{n:03d}".encode()).hexdigest(), 16) % 10 == 0}
    assert sample == expected and not (sample & important)


def test_review_overlay_keeps_drops_replaces_and_holds_text_only_fixes(gold_dir: Path) -> None:
    gold = load(gold_dir)
    fixed = item("U1-F002", "fact", OWNER, [ev("1.4.1")], fact_class="主人的偏好和规矩", attribute="报价格式", value="改过的值")
    review = {
        "reviews": {
            "U1-F001": {"v": "ok", "text": ""},
            "U1-A003": {"v": "wrong", "text": "编的：不该有这一条"},
            "U1-F002": {"v": "fix", "text": "编的改法", "item": fixed},
            "U2-F001": {"v": "fix", "text": "只写了一句话"},
            "Z-F999": {"v": "ok"},
        },
        "additions": [{"unit": "U2", "text": "编的漏标"},
                      {"unit": "U2", "item": item("U2-F099", "fact", FAR_ADR, [ev("1.1.1", Q)], fact_class="事实")}],
    }
    units, stats, pending = apply_review(gold.units, review)
    ids = {one.id for u in units.values() for one in u.items}
    assert "U1-A003" not in ids and "U2-F001" not in ids and "U2-F099" in ids
    assert next(one for one in units["U1"].items if one.id == "U1-F002").value == "改过的值"
    assert [one.id for one in pending] == ["U2-F001"]
    assert (stats.reviewed, len(stats.dropped), len(stats.replaced), len(stats.pending_fix)) == (4, 1, 1, 1)
    assert (len(stats.added), stats.pending_additions, stats.unknown_ids) == (1, 1, ["Z-F999"])
    with pytest.raises(ScoreInputError):
        apply_review(gold.units, {"U1-F001": {"v": "maybe"}})


def test_not_labeled_ranges_lists_and_attachment_rule() -> None:
    parsed = parse_unit(unit("X", [P], [], not_labeled=[{"range": ["2.1.1", "2.3.1"]}, {"sids": ["5.1.2"]}]))
    assert is_not_labeled(parsed, sid("2.2.9")) and is_not_labeled(parsed, sid("2.3.1"))
    assert not is_not_labeled(parsed, sid("2.3.2")) and not is_not_labeled(parsed, sid("1.9.9"))
    assert is_not_labeled(parsed, sid("5.1.2")) and not is_not_labeled(parsed, sid("5.1.1"))
    mail = parse_unit(unit("M", [MAIL], [], kind="mail_thread", not_labeled=[{"reason": "附件不标"}]))
    assert is_not_labeled(mail, sid("attachment.3.1", MAIL)) and not is_not_labeled(mail, sid("1.3.1", MAIL))


def test_relations_merge_across_units_with_the_best_start(gold_dir: Path) -> None:
    gold = load(gold_dir)
    merged = {relation.key: relation for relation in gold_relations(gold.units.values())}
    held = merged[("主人", "持有", "远帆 ADR")]
    assert {one.id for one in held.items} == {"U1-R001", "U2-R001"}
    assert held.best == When("2026-10-13", None, "day")  # 精度高的赢，月份那条是追加证据
    assert merged[("林小禾", "亲属", "主人")].kin == "妻子"


def test_a_second_episode_of_the_same_relation_also_counts_as_a_start() -> None:
    first = item("A-R001", "relation", OWNER, [ev("1.1.1")], relation="负责", object={"name": "远帆", "type": "公司或机构"},
                 when={"from": "2026-08-21", "to": None, "precision": "day"})
    again = item("B-R001", "relation", OWNER, [ev("1.1.1", Q)], relation="负责", object={"name": "远帆", "type": "公司或机构"},
                 when={"from": "2026-09-01", "to": None, "precision": "day"})
    merged = gold_relations([parse_unit(unit("A", [P], [first])), parse_unit(unit("B", [Q], [again]))])
    assert {one.start for one in merged[0].starts} == {"2026-08-21", "2026-09-01"}


def test_kinship_reversed_and_synonyms() -> None:
    assert kinship("老婆") == kinship("妻子") == ("配偶", None)
    assert kin_ok("妻子", "太太", reversed_=False)
    assert kin_ok("妻子", "丈夫", reversed_=True)
    assert kin_ok("女儿", "父亲", reversed_=True) and not kin_ok("女儿", "女儿", reversed_=True)
    assert not kin_ok("女儿", "妻子", reversed_=False) and not kin_ok("妻子", None, reversed_=False)
    assert kin_ok("儿子", "子女", reversed_=False)


# ---------- 实体对齐 ----------


def test_bridges_join_names_that_differ_and_skip_groups_that_hit_two(gold_dir: Path, tmp_path: Path) -> None:
    gold = load(gold_dir)
    lexicon = tmp_path / "lexicon.yaml"
    lexicon.write_text(
        "format: javmem-lexicon/2\naliases:\n"
        "  - {type: 公司或机构, names: [远帆集团, 远帆控股（编的）]}\n"
        "  - {type: 公司或机构, names: [远帆, 林小禾的店]}\n"
        "  - {type: 公司或机构, names: [某个编的银行, 编的银行]}\n",
        encoding="utf-8",
    )
    index = gold_index(gold)
    report = add_bridges(index, lexicon_groups(load_lexicon(lexicon), "别名表"))
    assert [g.hits for g in report.used] == [("远帆",), ("远帆",)]
    assert [g.names[0] for g in report.unmatched] == ["某个编的银行"]
    side = side_from_dict({**side_dict(), "entities": [
        {"id": "a", "name": "远帆控股（编的）", "type": "公司或机构"},
        {"id": "b", "name": "林小禾", "type": "人", "aliases": ["远帆"]},
        {"id": "c", "name": "谁也不认识", "type": "人"},
    ], "facts": [], "relations": []})
    alignment = align(side, gold, index)
    assert alignment.to_gold == {"a": "远帆", "b": "林小禾"}
    assert alignment.unaligned == ["c"]
    assert alignment.mixed == {"b": ("林小禾", "远帆")}  # 一个实体的名字、别名对上了两个：可能合错了


def test_table_groups_read_the_name_and_alias_columns() -> None:
    text = "| 标准名 | 常用叫法和别名 | 备注 |\n| --- | --- | --- |\n| 林小禾（小禾） | 林小禾、禾禾；别名：Hollis（编的写法） | 编的 |\n\n| 别的表 | x |\n| --- | --- |\n| 不认 | 不认 |\n"
    groups = table_groups(text, "底表")
    assert len(groups) == 1
    assert {"林小禾（小禾）", "林小禾", "小禾", "禾禾", "Hollis（编的写法）", "Hollis"} <= set(groups[0][1])


def test_split_rows_win_over_the_combined_row_in_the_entity_table() -> None:
    table = {"schema": "javmem-gold-entities/1", "entities": [
        {"name": "编的几张单子（合称）", "type": "订单或合同", "aliases": ["XX0001"], "from": "底表"},
        {"name": "编的单子一", "type": "订单或合同", "aliases": ["XX0001"], "from": "预标统一（一张一个）"},
    ]}
    from javmem.scoring.gold import Gold, ReviewStats, parse_entities

    index = gold_index(Gold({}, parse_entities(table), ReviewStats()))
    assert index.lookup("XX0001") == {"编的单子一": 1, "编的几张单子（合称）": 2}
    assert index.demoted == [("XX0001", "编的几张单子（合称）", ("编的单子一",))]


# ---------- 一边整个打一遍 ----------


def test_side_format_takes_the_seven_relations() -> None:
    """D210：系统那一边认七种，隶属于（部门、分公司 → 它所属的那家）照收；不在七种里的（汇报给）照旧报错（下一条）。"""
    good = side_dict()
    good["entities"].append({"id": "e7", "name": "远帆 外贸部", "type": "公司或机构"})  # type: ignore[union-attr]
    good["relations"] = [{"id": "r9", "subject": "e7", "relation": "隶属于", "object": "e2", "evidence": [sid("1.1.1")]}]
    side = side_from_dict(good)
    assert [(r.subject, r.relation, r.object) for r in side.relations.values()] == [("e7", "隶属于", "e2")]


def test_side_format_rejects_unknown_fields_types_and_dangling_ids() -> None:
    good = side_dict()
    side_from_dict(good)
    for broken in (
        {**good, "extra": 1},
        {**good, "relations": [{"id": "r", "subject": "e1", "relation": "汇报给", "object": "e2"}]},
        {**good, "facts": [{"id": "f", "text": "x", "subject": "nope"}]},
        {**good, "entities": [{"id": "e1", "name": "主人", "type": "机器人"}]},
        {**good, "format": "javmem-score/0"},
    ):
        with pytest.raises(ScoreInputError):
            side_from_dict(broken)


def test_score_one_side_against_the_gold(gold_dir: Path) -> None:
    gold = load(gold_dir)
    result = score(side_from_dict(side_dict()), gold)
    assert result.scored == ["U1", "U2"]
    assert "M9" in result.skipped  # 邮件单元没给原文账本：分不出是哪一串的句子
    grades = {one.gold: one.grade for one in result.facts}
    assert grades == {"U1-F001": "完整", "U1-F002": "没抽到", "U1-F003": "没抽到", "U2-F001": "完整"}
    assert "f3" not in result.side_facts["U1"]  # 不标的句子里的：抽到了不算错
    assert "f2" in result.side_facts["U1"] and "f2" not in result.matched
    verdicts = {one.side: one.verdict for one in result.relation_verdicts}
    assert verdicts == {"r1": "对", "r2": "对", "r3": "方向反了", "r4": "两头对不上实体", "r5": "日子不对"}
    changes = {one.gold: one.verdict for one in result.changes}
    assert changes == {"U1-S101": "判对", "U1-S102": "没判错", "U2-S201": "当成变了"}
    aliases = {one.items[0]: one.verdict for one in result.aliases}
    assert aliases == {"U1-A001": "对", "U1-A002": "没合错", "U1-A003": "没认出"}
    numbers = metrics(result, gold)
    facts = numbers["facts"]
    assert (facts["gold"], facts["complete"], facts["missed"]) == (3, 2, 1)  # 只在引用里的单列
    assert facts["only_in"]["only_in_quote"]["gold"] == 1
    assert facts["by_class"]["主人的偏好和规矩"]["missed"] == 1
    relations = numbers["relations"]
    assert (relations["gold"], relations["side"], relations["each"]["side"]) == (2, 4, 5)  # 合成后 4 条：r1、r5 合成一条
    assert (relations["precision"], relations["precision_strict"], relations["precision_loose"], relations["recall"]) == (
        0.5, 0.5, 0.5, 1.0)
    assert (relations["each"]["precision_strict"], relations["each"]["precision_loose"], relations["each"]["recall_strict"]) == (
        0.4, 0.6, 1.0)
    assert relations["reversed"] == 1
    assert numbers["changes"]["rate"] == round(2 / 3, 4)
    assert numbers["aliases"]["rate"] == 0.5 and numbers["aliases"]["not_same"]["right"] == 1


def test_a_change_about_another_entity_in_the_same_sentence_is_not_this_change() -> None:
    from javmem.scoring.gold import Gold, ReviewStats, parse_entities

    changed = item("V1-S101", "change", FAR, [ev("1.4.1", Q, role="after")], content="远帆换了联系人。",
                   change={"kind": "换人或换渠道", "soft": False, "target": "联系人", "before": "甲", "after": "乙", "is_change": True})
    kept = item("V1-S102", "change", FAR_ADR, [ev("1.5.1", Q, role="after")], content="远帆 ADR 只是说明白了。",
                change={"kind": "说明白了（不算变化）", "soft": False, "target": "x", "before": "x", "after": "x", "is_change": False})
    gold = Gold({"V1": parse_unit(unit("V1", [Q], [changed, kept]))}, parse_entities(ENTITY_TABLE), ReviewStats())
    data = side_dict()
    data["relations"] = []
    data["facts"] = [  # 同一句上被替代的是主人的事，不是远帆的
        {"id": "g1", "text": "主人的早饭是豆浆。", "subject": "e1", "attribute": "早饭", "value": "豆浆", "evidence": [sid("1.1.9", Q)],
         "state": "被替代", "superseded_by": "g2"},
        {"id": "g2", "text": "主人的早饭改成米粥。", "subject": "e1", "attribute": "早饭", "value": "米粥", "evidence": [sid("1.4.1", Q)]},
        {"id": "g3", "text": "主人的午饭是面。", "subject": "e1", "attribute": "午饭", "value": "面", "evidence": [sid("1.1.8", Q)],
         "state": "被替代", "superseded_by": "g4"},
        {"id": "g4", "text": "主人的午饭改成饭。", "subject": "e1", "attribute": "午饭", "value": "饭", "evidence": [sid("1.5.1", Q)]},
    ]
    verdicts = {one.gold: one.verdict for one in score(side_from_dict(data), gold).changes}
    assert verdicts == {"V1-S101": "抽到了新的没判成变化", "V1-S102": "没判错"}


def test_kinship_written_wrong_is_not_a_right_relation(gold_dir: Path) -> None:
    gold = load(gold_dir)
    data = side_dict()
    data["relations"] = [{"id": "r2", "subject": "e4", "relation": "亲属", "object": "e1", "fields": {"kinship": "女儿"}}]
    result = score(side_from_dict(data), gold)
    assert [one.verdict for one in result.relation_verdicts] == ["具体关系不对"]


def test_outputs_hide_the_side_and_keep_gold_out_of_the_prompt_report(gold_dir: Path, tmp_path: Path) -> None:
    gold = load(gold_dir)
    name = "这一边的真名-编的"
    result = score(side_from_dict(side_dict(name)), gold)
    out = tmp_path / "out"
    write_outputs(out, gold, {name: result})
    codes = json.loads((out / "边的代号.json").read_text(encoding="utf-8"))
    assert codes[name] in "甲乙丙丁戊己庚辛"
    blind = (out / "对不上-给审的人.md").read_text(encoding="utf-8")
    assert name not in blind and name not in (out / "对不上.json").read_text(encoding="utf-8")
    assert "U1-F002" in blind and f"**{codes[name]} · 事实 · 抽到了、金标准没有**" in blind
    assert blind.index("抽到了、金标准没有") < blind.index("金标准有、没抽到")  # 先看可能是金标准漏了的
    assert name in (out / "分数.md").read_text(encoding="utf-8")
    prompt = (out / "给J3.9.md").read_text(encoding="utf-8")
    for unit_data in gold_units():
        for one in unit_data["items"]:  # type: ignore[union-attr]
            assert one["id"] not in prompt and one["content"] not in prompt
            for evidence in one["evidence"]:
                assert evidence["sentence"] not in prompt
    assert "主人早上喝了一杯热的豆浆。" in prompt  # 例子是这一边自己抽的、对不上金标准的
    again = side_from_dict(json.loads((out / f"side-{name}.json").read_text(encoding="utf-8")))
    assert set(again.facts) == set(result.side.facts)


def test_side_codes_are_drawn_fresh_and_do_not_run_out() -> None:
    from javmem.scoring.report import side_codes

    names = [f"边{n}" for n in range(10)]
    codes = side_codes(names)
    assert set(codes) == set(names) and len(set(codes.values())) == 10
    assert sorted(code for code in codes.values() if code in "甲乙丙丁戊己庚辛") == sorted("甲乙丙丁戊己庚辛")
    assert {code for code in codes.values() if code not in "甲乙丙丁戊己庚辛"} == {"第9边", "第10边"}


def test_cli_scores_a_side_file_and_reports_bad_input(gold_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    side = tmp_path / "side.json"
    side.write_text(json.dumps(side_dict(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    assert main(["score", "--gold", str(gold_dir), "--side", f"编的={side}", "--out", str(out)]) == 0
    assert (out / "scores.json").exists() and "关系准确率 50.0%" in capsys.readouterr().out
    assert main(["score", "--gold", str(gold_dir), "--side", "坏的", "--out", str(out)]) == 2
    assert main(["score", "--gold", str(gold_dir), "--side", f"编的={side}", "--units", "U7", "--out", str(out)]) == 2
    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    assert main(["score", "--gold", str(gold_dir), "--side", f"编的={broken}", "--out", str(out)]) == 2
    assert "打不了分" in capsys.readouterr().out
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "javmem").mkdir(parents=True)
    assert main(["score", "--gold", str(gold_dir), "--side", f"编的={side}", "--out", str(repo / "out")]) == 2
    assert not (repo / "out").exists()  # 输出里有金标准的原话，不写进仓库


def test_cli_extra_merges_relations_into_a_side(gold_dir: Path, tmp_path: Path) -> None:
    base = side_dict()
    base["relations"] = []
    extra = {"format": FORMAT, "side": "另写的", "packets": [P], "entities": base["entities"][:4],
             "relations": [side_dict()["relations"][0]]}
    (tmp_path / "base.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "extra.json").write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    assert main(["score", "--gold", str(gold_dir), "--side", f"编的={tmp_path / 'base.json'}",
                 "--extra", f"编的={tmp_path / 'extra.json'}", "--out", str(out)]) == 0
    numbers = json.loads((out / "scores.json").read_text(encoding="utf-8"))["sides"]["编的"]
    assert numbers["relations"]["verdicts"] == {"对": 1}


# ---------- javmem 的账本、结果库 ----------


def _day_packet(day: str, *lines: str) -> str:
    body = f"## 原话 · {day} 10:30–10:59\n" + "".join(f"- [10:{30 + n:02d}] {line}\n" for n, line in enumerate(lines))
    return build_packet(stamp=f"{day}_2330", body=body, created_at=f"{day}T23:30:00+08:00",
                        covers_from=f"{day}T00:00:00+08:00", covers_to=f"{day}T23:30:00+08:00")


def _write_ledger(world: World) -> sqlite3.Connection:
    patterns = default_patterns()
    batch = load_queued(world.settings, "prod")
    items = []
    for window, rendered, key in prepare(build_windows(batch.packets), FakeModel(), patterns):
        facts = []
        for line in window.targets:
            if "单价" in line.text:
                value = "12.5" if "12.5" in line.text else "13.0"
                facts.append({"text": f"编的零件单价是 {value}。", "subject": "e1", "attribute": "单价", "value": value,
                              "occurred_at": None, "time_precision": "unknown", "certainty": "stated",
                              "entities": [{"key": "e1", "role": "subject"}],
                              "evidence": [{"ref": line.ref, "quote": line.text.rstrip("。")}]})
        raw = {"entities": [{"key": "e1", "mention": "岱川", "name": "岱川实业", "type": "公司或机构"}], "facts": facts}
        extraction = map_output(raw, window, rendered.redacted_refs)
        items.append(Checked(window, rendered, key, extraction, verify_window(window, rendered, extraction)))
    conn = open_ledger(world.settings.library("prod"))
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model=FakeEmbeddings().name)
    with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
        run_write(items, writer, packets=[(p.packet_id, p.header.revision) for p in batch.packets], store=store,
                  cache=cache, embedder=FakeEmbeddings(), sleep=lambda _: None)
    return conn


def test_ledger_side_reads_facts_entities_aliases_and_supersede(world: World) -> None:
    for packet in (_day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), _day_packet("2026-11-05", "我：岱川的零件单价改成 13.0。")):
        world.drop("inbox/desk-bot", packet)
    world.intake()
    _write_ledger(world).close()
    db = world.settings.library("prod").db_path
    side = side_from_ledger(db, ["2026-11-01_2330_desk-bot", "2026-11-05_2330_desk-bot", "不在库里的包"])
    assert side.packets == {"2026-11-01_2330_desk-bot", "2026-11-05_2330_desk-bot"}
    by_value = {fact.value: fact for fact in side.facts.values()}
    old, new = by_value["12.5"], by_value["13.0"]
    assert old.state == "被替代" and old.superseded_by == new.id and new.state == "现行"
    assert old.certainty == "原话" and old.evidence == ("2026-11-01_2330_desk-bot#1.1.1",)
    assert side.entities[old.subject].name == "岱川实业" and "岱川" in side.entities[old.subject].aliases  # type: ignore[index]
    assert side.relations == {} and side.changes == ()
    only_first = side_from_ledger(db, ["2026-11-01_2330_desk-bot"])
    assert set(only_first.facts) == {old.id}  # 只取证据在点名的包里的
    assert only_first.facts[old.id].superseded_by is None  # 替代它的那条不在这一份里
    only_second = side_from_ledger(db, ["2026-11-05_2330_desk-bot"])
    assert set(only_second.facts) == {old.id, new.id}  # 被替代的旧事实证据在别的包，也带上：变化的前一头认得出来
    assert only_second.facts[old.id].superseded_by == new.id


def test_packet_costs_split_a_window_by_target_characters(tmp_path: Path) -> None:
    store = tmp_path / "l3.db"
    conn = sqlite3.connect(store)
    conn.executescript(
        "CREATE TABLE windows (window_id TEXT, cache_key TEXT, packets TEXT, manifest TEXT, built_at TEXT);"
        "CREATE TABLE calls (cache_key TEXT, input_tokens INT, output_tokens INT, reasoning_tokens INT, cached_tokens INT, seconds REAL);"
    )
    manifest = [{"ref": "C1", "packet_id": P, "shown": "上文" * 50},
                {"ref": "T1", "packet_id": P, "shown": "甲" * 30}, {"ref": "T2", "packet_id": Q, "shown": "乙" * 10}]
    conn.executemany("INSERT INTO windows VALUES (?, ?, ?, ?, ?)", [
        (f"{Q}#w1", "old", json.dumps([[Q, 1]]), json.dumps([{"ref": "T1", "packet_id": Q, "shown": "旧"}]), "2026-11-01T00:00"),
        (f"{Q}#w1", "new", json.dumps([[P, 1], [Q, 1]]), json.dumps(manifest, ensure_ascii=False), "2026-11-02T00:00"),
    ])
    conn.executemany("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)", [
        ("new", 800, 400, 100, 0, 10.0), ("new", 0, 0, 0, 0, 2.0), ("old", 9999, 9999, 0, 0, 99.0)])
    conn.commit()
    conn.close()
    costs = packet_costs(store, {P, Q})
    assert (costs[P].calls, costs[P].input_tokens, costs[P].output_tokens, costs[P].seconds) == (2, 600, 300, 9.0)
    assert (costs[Q].input_tokens, costs[Q].output_tokens, costs[Q].reasoning_tokens) == (200, 100, 25)
    assert costs[P].tokens == 900
    assert packet_costs(store, {Q}).keys() == {Q}


def test_mail_units_need_the_sentence_index_and_count_only_their_thread(gold_dir: Path) -> None:
    gold = load(gold_dir)
    data = side_dict()
    data["packets"] = [P, Q, MAIL]
    data["facts"] = [
        {"id": "m1", "text": "远帆的事，这一串里说的。", "subject": "e2", "evidence": [sid("1.1.1", MAIL)]},
        {"id": "m2", "text": "别的往来串里的一句。", "subject": "e2", "evidence": [sid("2.1.1", MAIL)]},
    ]
    data["relations"] = []
    side = side_from_dict(data)
    assert "M9" in score(side, gold).skipped
    index = SentenceIndex(thread={sid("1.1.1", MAIL): "<thread@example.org>", sid("2.1.1", MAIL): "<other@example.org>"})
    result = score(side, gold, sentences=index)
    assert "M9" in result.scored
    assert result.side_facts["M9"] == ["m1"]  # 别的往来串的句子不算这个单元的


def test_sentence_index_from_a_mail_ledger(world: World) -> None:
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.intake()
    found = sentences_from_ledger(world.settings.library("prod").db_path, [MAIL_ID])
    assert set(found.thread.values()) == {"<ship-0001@example.org>", "<slot-0001@example.com>"}  # 规范的样例有两串
    assert {"<ship-0001@example.org>", "<ship-0002@example.com>"} <= set(found.message.values())
    assert found.thread[f"{MAIL_ID}#1.subject"] == "<ship-0001@example.org>"


def test_bridge_groups_take_lexicons_and_tables(tmp_path: Path) -> None:
    lexicon = tmp_path / "lexicon.yaml"
    lexicon.write_text("aliases:\n  - {type: 人, names: [林小禾, 小禾]}\n", encoding="utf-8")
    groups = bridge_groups([("一版", load_lexicon(lexicon))], [("表", "| 标准名 | 叫法 |\n| --- | --- |\n| 远帆 | 远帆集团 |\n")])
    assert [names for _, names in groups] == [("林小禾", "小禾"), ("远帆", "远帆集团")]


# ---------- 独立审阅那一轮抓到的（每条先红再修） ----------


def _gold(units: list[dict[str, object]], table: dict[str, object] = ENTITY_TABLE):
    from javmem.scoring.gold import Gold, ReviewStats, parse_entities

    return Gold({u["unit"]: parse_unit(u) for u in units}, parse_entities(table), ReviewStats())  # type: ignore[index]


def test_a_restatement_on_the_superseding_fact_is_not_a_recorded_change() -> None:
    changed = item("V2-S101", "change", FAR_ADR, [ev("1.2.1", role="before"), ev("1.3.1", role="after")],
                   content="远帆 ADR 的报价从 12.5 改成 13.0。",
                   change={"kind": "价格变了", "soft": False, "target": "报价", "before": "12.5", "after": "13.0", "is_change": True})
    clarified = item("V2-S102", "change", FAR_ADR, [ev("1.5.1", role="after")], content="远帆 ADR 的报价只是又说了一遍。",
                     change={"kind": "说明白了（不算变化）", "soft": False, "target": "报价", "before": "13.0", "after": "13.0",
                             "is_change": False})
    gold = _gold([unit("V2", [P], [changed, clarified])])
    data = side_dict()
    data["facts"] = [
        {"id": "f4", "text": "远帆 ADR 的报价是 12.5。", "subject": "e3", "attribute": "报价", "value": "12.5",
         "evidence": [sid("1.2.1")], "state": "被替代", "superseded_by": "f5"},
        {"id": "f5", "text": "远帆 ADR 的报价改成 13.0。", "subject": "e3", "attribute": "报价", "value": "13.0",
         "evidence": [sid("1.3.1"), sid("1.5.1")]},  # 1.5.1 是又说了一遍，合到了现行的那条上
    ]
    data["relations"] = [{"id": "r9", "subject": "e1", "relation": "持有", "object": "e3", "state": "已结束",
                          "when": {"from": "2026-10-14", "to": "2026-11-01", "precision": "day"},
                          "evidence": [sid("1.6.1"), sid("1.5.1"), sid("1.9.1")]}]
    verdicts = {one.gold: one.verdict for one in score(side_from_dict(data), gold).changes}
    assert verdicts == {"V2-S101": "判对", "V2-S102": "没判错"}


def test_packets_taken_in_but_not_extracted_are_not_counted_as_extracted(world: World) -> None:
    world.drop("inbox/desk-bot", _day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"))
    world.intake()
    db = world.settings.library("prod").db_path
    open_ledger(world.settings.library("prod")).close()
    assert side_from_ledger(db, ["2026-11-01_2330_desk-bot"]).packets == frozenset()  # 排着队、还没抽


def test_a_text_only_fix_holds_its_sentences_out_of_precision(gold_dir: Path, tmp_path: Path) -> None:
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"U1-F001": {"v": "fix", "text": "编的改法"}, "U1-R002": {"v": "fix", "text": "编的"}},
                                 ensure_ascii=False), encoding="utf-8")
    gold = load(gold_dir, review)
    result = score(side_from_dict(side_dict()), gold)
    assert "f1" in result.held and "f1" not in result.side_facts["U1"]  # 金标准那条在改：不算对也不算错
    verdicts = {one.side: one.verdict for one in result.relation_verdicts}
    assert verdicts["r2"] == "金标准这条在改"
    numbers = metrics(result, gold)
    assert numbers["facts"]["side_held"] == 1  # 在改的不进分母：合成后 4 条去掉 1 条，每条单独判 5 条去掉 1 条
    assert (numbers["relations"]["side"], numbers["relations"]["each"]["side"]) == (3, 4)


def test_prompt_report_drops_examples_that_copy_gold_text(gold_dir: Path, tmp_path: Path) -> None:
    gold = load(gold_dir)
    data = side_dict()
    data["facts"].append({"id": "f9", "text": "主人的规矩：报价先给草稿再外发。", "subject": "e1", "evidence": [sid("1.11.1")]})
    result = score(side_from_dict(data), gold)
    assert "f9" in result.side_facts["U1"] and "f9" not in result.matched
    write_outputs(tmp_path / "out", gold, {"编的": result})
    prompt = (tmp_path / "out" / "给J3.9.md").read_text(encoding="utf-8")
    assert "报价先给草稿再外发" not in prompt and "主人早上喝了一杯热的豆浆。" in prompt


def test_gold_items_inside_not_labeled_sentences_are_not_required() -> None:
    inside = item("V3-F001", "fact", FAR, [ev("2.2.1")], fact_class="事实", attribute="编的", value="一件事")
    outside = item("V3-F002", "fact", FAR, [ev("1.1.1")], fact_class="事实", attribute="出货日期", value="2026-11-15")
    gold = _gold([unit("V3", [P], [inside, outside], not_labeled=[{"range": ["2.1.1", "2.3.1"]}])])
    result = score(side_from_dict(side_dict()), gold)
    assert [one.gold for one in result.facts] == ["V3-F002"] and result.not_required == ["V3-F001"]
    assert metrics(result, gold)["facts"]["gold"] == 1


def test_only_in_changes_do_not_lower_the_main_change_rate() -> None:
    quoted = item("V4-S101", "change", FAR, [ev("1.4.1", role="after")], flags=["only_in_quote"],
                  change={"kind": "计划改了", "soft": False, "target": "x", "before": "a", "after": "b", "is_change": True})
    gold = _gold([unit("V4", [P], [quoted])])
    numbers = metrics(score(side_from_dict(side_dict()), gold), gold)["changes"]
    assert numbers["gold"] == 0 and numbers["rate"] is None and numbers["only_in"]["gold"] == 1


KIN_TABLE = {"schema": "javmem-gold-entities/1", "entities": [
    *ENTITY_TABLE["entities"], {"name": "林大树", "type": "人", "aliases": [], "from": "底表"}]}  # type: ignore[misc]


def test_kinship_written_both_ways_in_two_units_is_one_relation() -> None:
    one = item("V5-R001", "relation", HE, [ev("1.7.1")], relation="亲属", object={"name": "林大树", "type": "人"},
               fields={"kinship": "女儿"})
    two = item("V6-R001", "relation", ("林大树", "人"), [ev("1.7.1", Q)], relation="亲属", object={"name": "林小禾", "type": "人"},
               fields={"kinship": "父亲"})
    gold = _gold([unit("V5", [P], [one]), unit("V6", [Q], [two])], KIN_TABLE)
    assert len(gold_relations(gold.units.values())) == 1
    data = side_dict()
    data["entities"].append({"id": "e7", "name": "林大树", "type": "人"})
    data["relations"] = [{"id": "k1", "subject": "e4", "relation": "亲属", "object": "e7", "fields": {"kinship": "女儿"},
                          "evidence": [sid("1.7.1")]}]
    result = score(side_from_dict(data), gold)
    assert [one.verdict for one in result.relation_verdicts] == ["对"]
    assert metrics(result, gold)["relations"]["recall_strict"] == 1.0


def test_reversed_kinship_needs_the_other_side_of_the_term() -> None:
    assert not kin_ok("妻子", "妻子", reversed_=True) and kin_ok("妻子", "丈夫", reversed_=True)
    assert kin_ok("哥哥", "妹妹", reversed_=True) and not kin_ok("哥哥", "哥哥", reversed_=True)
    assert kin_ok("儿子", "母亲", reversed_=True) and kin_ok("配偶", "妻子", reversed_=True)
    assert not kin_ok("妻子", "丈夫", reversed_=False)


def test_packet_costs_count_a_shared_recheck_once_add_two_and_prefer_the_written_key(tmp_path: Path) -> None:
    store = tmp_path / "l3.db"
    conn = sqlite3.connect(store)
    conn.executescript(
        "CREATE TABLE windows (window_id TEXT, cache_key TEXT, packets TEXT, manifest TEXT, built_at TEXT);"
        "CREATE TABLE calls (cache_key TEXT, input_tokens INT, output_tokens INT, reasoning_tokens INT, cached_tokens INT, seconds REAL);"
        "CREATE TABLE recheck_windows (window_id TEXT, cache_key TEXT, recheck_key TEXT, facts TEXT, linked_at TEXT);"
        "CREATE TABLE recheck_calls (window_id TEXT, cache_key TEXT, input_tokens INT, output_tokens INT, reasoning_tokens INT, "
        "cached_tokens INT, seconds REAL);"
    )
    one = json.dumps([{"ref": "T1", "packet_id": P, "shown": "甲"}])
    two = json.dumps([{"ref": "T1", "packet_id": Q, "shown": "乙"}])
    conn.executemany("INSERT INTO windows VALUES (?, ?, ?, ?, ?)", [
        (f"{P}#w1", "k1", json.dumps([[P, 1]]), one, "2026-11-01"),
        (f"{P}#w1", "k1-newer", json.dumps([[P, 1]]), one, "2026-11-03"),  # 切过一份新的，账本写的还是 k1
        (f"{Q}#w1", "k2", json.dumps([[Q, 1]]), two, "2026-11-01"),
    ])
    conn.executemany("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)", [("k1", 10, 10, 0, 0, 1.0), ("k1-newer", 99, 99, 0, 0, 9.0),
                                                                     ("k2", 20, 20, 0, 0, 2.0)])
    conn.executemany("INSERT INTO recheck_windows VALUES (?, ?, ?, ?, ?)", [
        (f"{P}#w1", "k1", "rk1", "[]", "x"), (f"{Q}#w1", "k2", "rk1", "[]", "x"), (f"{P}#w1", "k1", "rk2", "[]", "x")])
    conn.executemany("INSERT INTO recheck_calls VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (f"{P}#w1", "rk1", 100, 0, 0, 0, 1.0), (f"{P}#w1", "rk2", 50, 0, 0, 0, 1.0), (f"{Q}#w1", "rk3", 30, 0, 0, 0, 1.0)])
    conn.commit()
    conn.close()
    costs = packet_costs(store, {P, Q}, written={f"{P}#w1": "k1"})
    assert costs[P].input_tokens == 10 + 100 + 50  # 账本写的 k1；共用的复核只算一次；两份复核加起来
    assert costs[Q].input_tokens == 20 + 30  # 没挂上窗口的复核调用按它记的窗口算


def test_the_blind_list_does_not_follow_the_side_order(gold_dir: Path, tmp_path: Path) -> None:
    gold = load(gold_dir)
    results = {name: score(side_from_dict(side_dict(name)), gold) for name in ("头一边", "第二边")}
    write_outputs(tmp_path / "out", gold, results)
    codes = json.loads((tmp_path / "out" / "边的代号.json").read_text(encoding="utf-8"))
    order = [entry["边"] for entry in json.loads((tmp_path / "out" / "对不上.json").read_text(encoding="utf-8"))["条目"]]
    first, second = codes["头一边"], codes["第二边"]
    assert order.index(second) < len(order) - 1 - order[::-1].index(first)  # 两边交错着排，不是一边排完再排另一边


def test_an_unmatched_fact_in_two_units_is_listed_once(gold_dir: Path, tmp_path: Path) -> None:
    gold = load(gold_dir)
    data = side_dict()
    data["facts"].append({"id": "f9", "text": "编的一句两包都说了的闲话。", "evidence": [sid("1.12.1"), sid("1.12.1", Q)]})
    result = score(side_from_dict(data), gold)
    write_outputs(tmp_path / "out", gold, {"编的": result})
    entries = json.loads((tmp_path / "out" / "对不上.json").read_text(encoding="utf-8"))["条目"]
    assert sum(1 for entry in entries if entry.get("抽到的") == "编的一句两包都说了的闲话。") == 1
    prompt = (tmp_path / "out" / "给J3.9.md").read_text(encoding="utf-8")
    assert prompt.count("编的一句两包都说了的闲话。") <= 1


def test_reading_a_ledger_leaves_no_shm_or_wal_next_to_it(world: World) -> None:
    world.drop("inbox/desk-bot", _day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"))
    world.intake()
    _write_ledger(world).close()
    db = world.settings.library("prod").db_path
    extra = [db.with_name(db.name + suffix) for suffix in ("-wal", "-shm")]
    assert not any(path.exists() for path in extra)
    side_from_ledger(db, ["2026-11-01_2330_desk-bot"])
    assert not any(path.exists() for path in extra)


def test_dates_infer_precision_read_a_spaced_offset_and_coarse_starts_are_not_exact(gold_dir: Path) -> None:
    assert interval("2026-10-23 16:54 +08:00", "minute") == interval("2026-10-23", "day")
    data = side_dict()
    data["relations"] = [
        {"id": "a", "subject": "e1", "relation": "持有", "object": "e3", "when": {"from": "2026-10-14"}},  # 没写精度
        {"id": "b", "subject": "e1", "relation": "持有", "object": "e3", "when": {"from": "2026", "precision": "year"}},
    ]
    verdicts = {one.side: one.verdict for one in score(side_from_dict(data), load(gold_dir)).relation_verdicts}
    assert verdicts == {"a": "对", "b": "日子太粗"}


def test_unit_files_find_the_schema_anywhere_and_additions_need_unique_ids(gold_dir: Path) -> None:
    late: dict[str, object] = {"note": "很长的说明" * 80}  # schema 不在开头二百个字里
    late.update(unit("V7", [P], []))
    assert json.dumps(late, ensure_ascii=False).index("javmem-gold-prelabel/1") > 200
    (gold_dir / "V7.json").write_text(json.dumps(late, ensure_ascii=False), encoding="utf-8")
    assert "V7" in load(gold_dir).units
    twice = {"reviews": {}, "additions": [
        {"unit": "U1", "item": item("X-F001", "fact", FAR, [ev("1.1.1")], fact_class="事实")},
        {"unit": "U2", "item": item("X-F001", "fact", FAR, [ev("1.1.1", Q)], fact_class="事实")}]}
    with pytest.raises(ScoreInputError):
        apply_review(load(gold_dir).units, twice)


def test_a_unit_labeled_on_another_packet_revision_is_not_scored(gold_dir: Path) -> None:
    data = side_dict()
    data["revisions"] = {P: 2, Q: 1}
    result = score(side_from_dict(data), load(gold_dir))
    assert "U1" in result.skipped and "版本" in result.skipped["U1"] and "U2" in result.scored


def test_picked_units_leave_out_relations_without_evidence_and_extra_does_not_add_packets(gold_dir: Path) -> None:
    from javmem.scoring.report import Options, merge_sides

    data = side_dict()
    data["relations"] = [{"id": "n", "subject": "e1", "relation": "持有", "object": "e3"}]
    result = score(side_from_dict(data), load(gold_dir), options=Options(only_units=["U2"]))
    assert result.relation_verdicts == []
    base = side_dict()
    base["packets"] = [P]
    extra = {"format": FORMAT, "side": "另写的", "packets": [Q], "entities": []}
    merged = merge_sides(side_from_dict(base), side_from_dict(extra))
    assert merged.packets == {P}


def test_best_start_counts_the_same_moment_written_two_ways_as_one_day() -> None:
    def rel(item_id: str, start: str) -> dict[str, object]:
        return item(item_id, "relation", OWNER, [ev("1.1.1")], relation="持有", object={"name": FAR_ADR[0], "type": FAR_ADR[1]},
                    when={"from": start, "to": None, "precision": "minute"})

    units = [parse_unit(unit("A", [P], [rel("A-R001", "2026-10-14 10:00")])),
             parse_unit(unit("B", [Q], [rel("B-R001", "2026-10-13 09:00"), rel("B-R002", "2026-10-13T09:00+08:00")]))]
    best = gold_relations(units)[0].best
    assert best is not None and interval(best.start, best.precision) == interval("2026-10-13", "day")


def test_prompt_report_prints_only_known_kinds(gold_dir: Path, tmp_path: Path) -> None:
    odd = item("V8-S101", "change", FAR, [ev("1.4.1", role="after")],
               change={"kind": "编的不认识的种类", "soft": False, "target": "x", "before": "a", "after": "b", "is_change": True})
    gold = _gold([unit("V8", [P], [odd])])
    write_outputs(tmp_path / "out", gold, {"编的": score(side_from_dict(side_dict()), gold)})
    prompt = (tmp_path / "out" / "给J3.9.md").read_text(encoding="utf-8")
    assert "编的不认识的种类" not in prompt and "别的 0/1" in prompt


# ---------- 审阅复看那一遍抓到的 ----------


def test_prompt_report_drops_examples_holding_a_short_gold_text_and_lists_no_subject_examples(tmp_path: Path) -> None:
    short = item("V9-F001", "fact", HE, [ev("1.3.1")], fact_class="事实", attribute="口味", value="爱吃面条",
                 content="林小禾爱吃面条。")
    gold = _gold([unit("V9", [P], [short])])
    data = side_dict()
    data["facts"] = [
        {"id": "s1", "text": "林小禾爱吃面条。", "subject": "e4", "evidence": [sid("1.13.1")]},  # 对不上（出处不挨着），字照抄
        {"id": "s2", "text": "编的另一句和金标准不相干的话。", "subject": "e1", "evidence": [sid("1.14.1")]},
    ]
    data["relations"] = []
    result = score(side_from_dict(data), gold)
    assert {"s1", "s2"} <= set(result.side_facts["V9"]) and not ({"s1", "s2"} & result.matched)
    write_outputs(tmp_path / "out", gold, {"编的": result})
    prompt = (tmp_path / "out" / "给J3.9.md").read_text(encoding="utf-8")
    assert "爱吃面条" not in prompt and "编的另一句和金标准不相干的话。" in prompt
    assert "主体对不上的例子" not in prompt  # 主体不对的是对上了金标准的，不当例子


def test_packet_costs_leave_out_a_recheck_of_an_older_window_version(tmp_path: Path) -> None:
    store = tmp_path / "l3.db"
    conn = sqlite3.connect(store)
    conn.executescript(
        "CREATE TABLE windows (window_id TEXT, cache_key TEXT, packets TEXT, manifest TEXT, built_at TEXT);"
        "CREATE TABLE calls (cache_key TEXT, input_tokens INT, output_tokens INT, reasoning_tokens INT, cached_tokens INT, seconds REAL);"
        "CREATE TABLE recheck_windows (window_id TEXT, cache_key TEXT, recheck_key TEXT, facts TEXT, linked_at TEXT);"
        "CREATE TABLE recheck_calls (window_id TEXT, cache_key TEXT, input_tokens INT, output_tokens INT, reasoning_tokens INT, "
        "cached_tokens INT, seconds REAL);"
    )
    shown = json.dumps([{"ref": "T1", "packet_id": P, "shown": "甲"}])
    conn.executemany("INSERT INTO windows VALUES (?, ?, ?, ?, ?)", [
        (f"{P}#w1", "old", json.dumps([[P, 1]]), shown, "2026-11-01"),
        (f"{P}#w1", "new", json.dumps([[P, 1]]), shown, "2026-11-02")])
    conn.executemany("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)", [("old", 1, 1, 0, 0, 1.0), ("new", 200, 0, 0, 0, 1.0)])
    conn.execute("INSERT INTO recheck_windows VALUES (?, ?, ?, ?, ?)", (f"{P}#w1", "old", "rk-old", "[]", "x"))
    conn.execute("INSERT INTO recheck_calls VALUES (?, ?, ?, ?, ?, ?, ?)", (f"{P}#w1", "rk-old", 50, 0, 0, 0, 1.0))
    conn.commit()
    conn.close()
    assert packet_costs(store, {P}, written={f"{P}#w1": "new"})[P].input_tokens == 200  # 旧那一份的复核不算


# ---------- 只出分数的用法、接一份抽取结果（D111：J3.9 每跑一遍自己打分） ----------


def _store(path: Path, windows: list[tuple[object, ...]]) -> Path:
    """编一个 L3 的结果库：只建打分读的几列。windows 每个是 (窗口编号, 键, 清单, 结果, 切的时间[, 模型原样的回答])；
    没写模型原样的回答的，和结果一样。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE windows (window_id TEXT, cache_key TEXT, packets TEXT, manifest TEXT, extraction TEXT, built_at TEXT);"
        "CREATE TABLE results (cache_key TEXT, parsed TEXT);"
        "CREATE TABLE calls (cache_key TEXT, input_tokens INT, output_tokens INT, reasoning_tokens INT, cached_tokens INT, seconds REAL);"
    )
    for window_id, key, manifest, extraction, built, *rest in windows:
        packets = sorted({line["packet_id"] for line in manifest})  # type: ignore[attr-defined, index]
        conn.execute("INSERT INTO windows VALUES (?, ?, ?, ?, ?, ?)",
                     (window_id, key, json.dumps([[one, 1] for one in packets]), json.dumps(manifest, ensure_ascii=False),
                      json.dumps(extraction, ensure_ascii=False) if extraction is not None else None, built))
        if extraction is not None:
            parsed = rest[0] if rest else extraction
            conn.execute("INSERT INTO results VALUES (?, ?)", (key, json.dumps(parsed, ensure_ascii=False)))
            conn.execute("INSERT INTO calls VALUES (?, 100, 50, 10, 0, 2.0)", (key,))
    conn.commit()
    conn.close()
    return path


def _line(ref: str, tail: str, packet: str = P) -> dict[str, object]:
    return {"ref": ref, "sentence_id": f"{packet}#{tail}", "packet_id": packet, "revision": 1, "shown": "编的一句。"}


V2_EXTRACTION = {
    "entities": [{"key": "e1", "mention": "我", "name": "主人", "type": "人"},
                 {"key": "e2", "mention": "远帆ADR", "name": "远帆 ADR", "type": "资产"},
                 {"key": "e3", "mention": "小禾", "name": "林小禾", "type": "人"}],
    "facts": [{"text": "远帆的出货日期是 2026-11-15。", "subject": "e2", "attribute": "出货日期", "value": "2026-11-15",
               "occurred_at": None, "time_precision": "unknown", "certainty": "stated", "entities": [{"key": "e2", "role": "subject"}],
               "evidence": [{"ref": "T1", "quote": "编的", "sentence_id": sid("1.1.1")}]}],
    "relations": [
        {"subject": "e1", "relation": "持有", "object": "e2", "valid_from": "2026-10-14", "time_precision": "day",
         "certainty": "stated", "evidence": [{"ref": "T2", "quote": "编的"}]},  # 只有 T 编号：照清单换
        {"subject": "e3", "relation": "亲属", "object": "e1", "kinship": "妻子", "certainty": "stated",
         "evidence": [{"ref": "T3", "quote": "编的", "sentence_id": sid("1.7.1")}]},
        {"subject": "e1", "relation": "汇报给", "object": "e3", "evidence": []},  # 不是七种之一：不收，记个数
    ],
}


def test_relations_are_read_from_the_l3_results_of_a_store_only_result(tmp_path: Path, gold_dir: Path) -> None:
    kept = {"entities": [{"key": "e1", "mention": "远帆ADR", "name": "远帆 ADR", "type": "资产"},
                         {"key": "e2", "mention": "某编的银行", "name": "某编的银行", "type": "公司或机构"}], "facts": []}
    answer = {**kept, "relations": [{"subject": "e1", "relation": "托管于", "object": "e2",
                                     "evidence": [{"ref": "T1", "quote": "编的"}]}]}
    store = _store(tmp_path / "l3" / "l3.db", [
        (f"{P}#w1", "k-old", [_line("T1", "1.1.1")], {"entities": [], "facts": []}, "2026-11-01"),
        (f"{P}#w1", "k-new", [_line("T1", "1.1.1"), _line("T2", "1.6.1"), _line("T3", "1.7.1")], V2_EXTRACTION, "2026-11-02"),
        (f"{P}#w2", "k-w2", [_line("T1", "1.9.1")], None, "2026-11-02"),  # 这个包还有一个窗口没抽
        (f"{P}#w3", "k-w3", [_line("T1", "1.12.1")], kept, "2026-11-02", answer),  # 存下来的没有 relations，模型的回答里有
        (f"{Q}#w1", "k-none", [_line("T1", "1.1.1", Q)], None, "2026-11-02"),  # 切了还没抽：这个包不算抽过
    ])
    side, ledger = side_from_result(tmp_path, [P, Q])
    assert ledger is None and side.packets == {P} and side.revisions == {P: 1}
    relations = sorted(side.relations.values(), key=lambda r: r.relation)
    assert [(r.relation, r.evidence) for r in relations] == [
        ("亲属", (sid("1.7.1"),)), ("托管于", (sid("1.12.1"),)), ("持有", (sid("1.6.1"),))]
    held = next(r for r in relations if r.relation == "持有")  # 证据只有 T 编号的，照清单换成了句子
    assert side.entities[held.subject].name == "主人" and held.when == When("2026-10-14", None, "day")
    assert next(r for r in relations if r.relation == "亲属").fields == {"kinship": "妻子"}
    assert side.described["没收的关系"] == {"不是七种关系之一": 1}
    assert "1 个窗口存下来的结果里没有 relations" in str(side.described["关系"])
    assert [fact.text for fact in side.facts.values()] == ["远帆的出货日期是 2026-11-15。"]  # 没有账本：L3 原样的事实
    assert side.cost[P].input_tokens == 200
    assert side.pending == {sid("1.9.1"), sid("1.1.1", Q)} and str(side.described["还没抽完的窗口"]).startswith("2 个")
    result = score(side, load(gold_dir))
    assert "还没抽完" in result.skipped["U1"]  # U1 有一条金标准的出处在没抽的那个窗口里
    assert side_from_result(store, [P, Q])[0].relations.keys() == side.relations.keys()  # 直接给结果库也一样


def test_a_result_folder_reads_the_ledger_and_relations_and_counts_packets_still_queued(world: World, tmp_path: Path) -> None:
    for packet in (_day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), _day_packet("2026-11-05", "我：岱川的零件单价改成 13.0。")):
        world.drop("inbox/desk-bot", packet)
    world.intake()
    _write_ledger(world).close()
    # 照 J4.4 以前的账本：规则表里没有 L5 写关系的规则，关系照旧从结果库读（J4.4 起写的账本从账本读，见下面另一个测试）
    world.execute("DELETE FROM rules WHERE rule_id LIKE 'L5-relation%'")
    first, second, third = "2026-11-01_2330_desk-bot", "2026-11-05_2330_desk-bot", "2026-11-07_2330_desk-bot"
    world.drop("inbox/desk-bot", _day_packet("2026-11-07", "我：岱川的零件单价还是 13.0。"))  # 收下了，L3 跑了，账本还没写
    world.intake()
    world.execute("UPDATE packets SET extraction = 'queued' WHERE packet_id = ?", second)  # 只抽了几个窗口的包，还在排队
    ledger_path = world.settings.library("prod").db_path
    assert side_from_ledger(ledger_path, [first, second, third]).packets == {first}  # 只看账本，它们都不算抽过
    (written,) = {row[0] for row in world.rows("SELECT window_id FROM evidence WHERE packet_id = ?", second)}
    extraction = {"entities": [{"key": "e1", "mention": "岱川", "name": "岱川实业", "type": "公司或机构"},
                               {"key": "e2", "mention": "我", "name": "主人", "type": "人"}],
                  "facts": [], "relations": [{"subject": "e2", "relation": "客户是", "object": "e1",
                                              "evidence": [{"ref": "T1", "quote": "编的"}]}]}
    store = _store(world.data / "l3x" / "l3.db", [
        (written, "k2", [_line("T1", "1.1.1", second)], extraction, "2026-11-05"),
        (f"{third}#w1", "k3", [_line("T1", "1.1.1", third)], extraction, "2026-11-07")])  # 有结果，账本没写
    side, ledger = side_from_result(world.data, [first, second, third], library="prod", store=store)
    assert ledger == ledger_path and side.packets == {first, second}  # 账本写过它一个窗口的也算抽过
    assert side.pending == {sid("1.1.1", third)}  # 第三包：算了就成了一条都没抽到
    assert str(side.described["还没抽完的窗口"]).startswith("1 个")
    assert {fact.value for fact in side.facts.values()} == {"12.5", "13.0"}  # 事实照账本
    (relation,) = side.relations.values()  # 第三包那个窗口的关系不读：它的事实也还没写进账本
    assert side.entities[relation.object].name == "岱川实业"
    assert not relation.object.startswith("L3:") and relation.subject.startswith("L3:")  # 账本里有的认到账本的实体上
    only_ledger, _ = side_from_result(ledger_path, [first, second])  # 只给账本：数据目录里它旁边的结果库一起读
    assert only_ledger.described["结果库"] == str(world.data / "l3" / "l3.db")
    elsewhere = tmp_path / "别处" / "prod.db"  # 不在数据目录的 ledger 里的账本：旁边的结果库不认
    elsewhere.parent.mkdir(parents=True)
    _store(elsewhere.parent.parent / "l3" / "l3.db", [])
    source, target = sqlite3.connect(ledger_path), sqlite3.connect(elsewhere)
    source.backup(target)
    source.close()
    target.close()
    assert side_from_result(elsewhere, [first])[0].described["结果库"] == "没有"
    with pytest.raises(ScoreInputError, match="--library"):
        side_from_result(world.data, [first], library="test" if not (world.data / "ledger" / "test.db").exists() else "nope")
    with pytest.raises(ScoreInputError):
        side_from_result(world.data / "inbox", [first])  # 里面既没有账本也没有结果库


def test_a_ledger_with_windows_written_before_j44_keeps_reading_relations_from_the_result_store(world: World) -> None:
    """审阅第 5 条：J4.4 以前写的账本，新代码开一次写账（一开头就登记了写关系的规则）、哪个窗口都没重写：以前那些窗口的
    关系还只在结果库里，照旧从结果库读，不然一条都打不到。窗口都是 J4.4 起写的（写关系的规则登记以后）才从账本读。"""
    for packet in (_day_packet("2026-11-01", "我：岱川的零件单价是 12.5。"), _day_packet("2026-11-05", "我：岱川的零件单价改成 13.0。")):
        world.drop("inbox/desk-bot", packet)
    world.intake()
    _write_ledger(world).close()
    # 照 J4.4 以前的账本：没有写关系的规则，窗口是早先写的
    world.execute("DELETE FROM rules WHERE rule_id LIKE 'L5-relation%'")
    world.execute("UPDATE fact_claims SET created_at = '2026-01-01T00:00:00+08:00'")
    world.execute("UPDATE quarantine SET created_at = '2026-01-01T00:00:00+08:00'")
    first, second = "2026-11-01_2330_desk-bot", "2026-11-05_2330_desk-bot"
    (written,) = {row[0] for row in world.rows("SELECT window_id FROM evidence WHERE packet_id = ?", second)}
    extraction = {"entities": [{"key": "e1", "mention": "岱川", "name": "岱川实业", "type": "公司或机构"},
                               {"key": "e2", "mention": "我", "name": "主人", "type": "人"}],
                  "facts": [], "relations": [{"subject": "e2", "relation": "客户是", "object": "e1",
                                              "evidence": [{"ref": "T1", "quote": "编的"}]}]}
    store = _store(world.data / "l3x" / "l3.db", [(written, "k2", [_line("T1", "1.1.1", second)], extraction, "2026-11-05")])
    assert len(side_from_result(world.data, [first, second], library="prod", store=store)[0].relations) == 1
    conn = open_ledger(world.settings.library("prod"))
    try:
        LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")  # javmem write 一开头就登记规则
    finally:
        conn.close()
    side, _ = side_from_result(world.data, [first, second], library="prod", store=store)
    assert len(side.relations) == 1 and "结果库" in str(side.described["关系"])


def test_scores_only_scores_mail_units_with_sentences_from_the_sides_own_ledger(world: World, tmp_path: Path,
                                                                               capsys: pytest.CaptureFixture[str]) -> None:
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.intake()
    world.execute("UPDATE packets SET extraction = 'done' WHERE packet_id = ?", MAIL_ID)
    found = mail_sentences(world.settings.library("prod").db_path, [MAIL_ID])
    assert found is not None and mail_sentences(None, [MAIL_ID]) is None
    one = sorted(s for s, thread in found.thread.items() if thread == "<ship-0001@example.org>")[0]
    gold = tmp_path / "gold"
    gold.mkdir()
    mail = unit("M1", [MAIL_ID], [item("M1-F001", "fact", FAR, [{"sid": one, "quote": "", "sentence": "编的一句。"}],
                                       fact_class="事实")], kind="mail_thread", thread_id="<ship-0001@example.org>")
    (gold / "M1.json").write_text(json.dumps(mail, ensure_ascii=False), encoding="utf-8")
    (gold / "实体表.json").write_text(json.dumps(ENTITY_TABLE, ensure_ascii=False), encoding="utf-8")
    assert main(["score", "--gold", str(gold), "--side", f"javmem={world.data}", "--library", "prod", "--scores-only"]) == 0
    assert "- 打了分的单元：M1\n" in capsys.readouterr().out  # 没给 --sentences：用这一边自己的账本分出往来串


def test_scores_only_prints_numbers_and_the_sides_own_mistakes_but_no_gold(gold_dir: Path, tmp_path: Path,
                                                                         capsys: pytest.CaptureFixture[str]) -> None:
    side = tmp_path / "side.json"
    data = side_dict()
    data["relations"].append({"id": "r6", "subject": "e5", "relation": "客户是", "object": "e1", "evidence": [sid("1.6.1")]})
    data["facts"] += [  # 金标准说只是说明白了的那一句上，这一边记成了变化
        {"id": "f10", "text": "主人说那只编的基金先放着。", "subject": "e1", "evidence": [sid("1.8.1")], "state": "被替代",
         "superseded_by": "f11"},
        {"id": "f11", "text": "主人说那只编的基金还是先放着。", "subject": "e1", "evidence": [sid("1.8.2")]},
        # 照抄了审过、还在改的那一条（U1-F002）的话：它不算分，可还是金标准的话，不能当例子
        {"id": "f12", "text": "主人的规矩：报价先给草稿再外发。", "subject": "e1", "evidence": [sid("1.12.1")]},
    ]
    side.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"U1-F002": {"v": "fix", "text": "编的"}}, ensure_ascii=False), encoding="utf-8")
    base = ["score", "--gold", str(gold_dir), "--review", str(review), "--side", f"编的={side}", "--scores-only"]
    assert main(base) == 0
    printed = capsys.readouterr().out
    assert "| 关系准确率 | 40.0%（合成后 5 条、没写开始日子算对；严格 40.0%、不看日子 40.0%） |" in printed
    assert "| 关系准确率（每条单独判） | 33.3%（6 条；严格 33.3%、不看日子 50.0%） |" in printed
    for row in ("| 关系覆盖率 |", "| 变化判对率 | 33.3%", "| 别名对齐率 |", "| 事实完整抽到 |", "| 每包成本 |"):
        assert row in printed
    for klass in ("事实", "主人的偏好和规矩", "主人的决定和计划", "Bot 的建议和判断", "和主人有关的行情"):
        assert f"| {klass} |" in printed  # 事实五类
    assert "'两头对不上实体': 2" in printed and "'当成变了': 2" in printed  # 按错误类型的条数
    assert "  - 两头对不上实体：远帆物流 —客户是→ 主人（开始 没写）" in printed  # 例子是这一边自己抽错的
    for verdict in ("方向反了", "日子不对"):  # 对上了金标准一条的（三头对上了）只给数：例子会把金标准那一条说出来
        assert f"  - {verdict}" not in printed and f"'{verdict}': 1" in printed
    assert "  - 主人说那只编的基金先放着。 → 主人说那只编的基金还是先放着。" in printed
    assert "收盘价记的是 13.1。 →" not in printed  # 带着金标准的值（13.2）的例子不放
    for unit_data in gold_units():
        for one in unit_data["items"]:  # type: ignore[union-attr]
            assert one["id"] not in printed and one["content"] not in printed
            for evidence in one["evidence"]:
                assert evidence["sentence"] not in printed
    assert "远帆集团" not in printed and "包里说话人" not in printed  # 只在金标准实体表里的叫法也不出
    out = tmp_path / "out"
    assert main([*base, "--out", str(out)]) == 0
    assert (out / "给J3.9.md").read_text(encoding="utf-8").rstrip("\n") == capsys.readouterr().out.rstrip("\n")
    assert sorted(path.name for path in out.iterdir()) == ["分数.json", "给J3.9.md"]
    saved = (out / "分数.json").read_text(encoding="utf-8")
    assert "pending_not_scored" not in saved and "远帆集团" not in saved
    for unit_data in gold_units():
        for one in unit_data["items"]:  # type: ignore[union-attr]
            assert one["id"] not in saved
    saved_relations = json.loads(saved)["sides"]["编的"]["relations"]
    assert saved_relations["precision"] == 0.4 and saved_relations["each"]["precision_strict"] == 0.3333
    assert main(["score", "--gold", str(gold_dir), "--side", f"编的={side}"]) == 2  # 不是只出分数的要给 --out
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "javmem").mkdir(parents=True)
    assert main([*base, "--out", str(repo / "out")]) == 2  # 只出分数的也不能写进仓库
    assert not (repo / "out").exists()


def test_part_of_is_a_known_relation_kind_in_the_report() -> None:
    """D210：分种类的数里隶属于单列（以前金标准里有隶属于的，合进“别的”）；不在七种里的照旧写“别的”。"""
    from javmem.scoring.report import RELATION_KINDS, _known

    assert RELATION_KINDS == ("持有", "托管于", "负责", "任职于", "客户是", "亲属", "隶属于")
    assert _known("隶属于", RELATION_KINDS) == "隶属于" and _known("合伙做生意", RELATION_KINDS) == "别的"


def test_scores_only_writes_other_for_gold_labels_it_does_not_know(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    units = gold_units()
    units[0]["items"] += [  # type: ignore[operator]
        item("U1-R009", "relation", OWNER, [ev("1.13.1")], relation="合伙做生意", object={"name": "远帆", "type": "公司或机构"},
             fields={}, state="生效"),
        item("U1-A009", "alias", ("编的外星人", "外星人"), [ev("1.13.1")],
             alias={"mention": "小外", "entity": "编的外星人", "entity_type": "外星人", "kind": "简称"}),
    ]
    odd = unit("X1", [P], [item("X1-F001", "fact", FAR, [ev("1.14.1")], fact_class="事实")], kind="演示单元")
    folder = tmp_path / "gold"
    folder.mkdir()
    for data in [*units, odd]:
        (folder / f"{data['unit']}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    table = {**ENTITY_TABLE, "entities": [*ENTITY_TABLE["entities"],  # type: ignore[misc]
                                          {"name": "编的外星人", "type": "外星人", "aliases": ["小外"], "from": "底表"}]}
    (folder / "实体表.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    side = tmp_path / "side.json"
    side.write_text(json.dumps(side_dict(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    assert main(["score", "--gold", str(folder), "--side", f"编的={side}", "--scores-only", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    saved = (out / "分数.json").read_text(encoding="utf-8")
    for label in ("合伙做生意", "外星人", "演示单元"):  # 金标准里写了别的标签的，只写“别的”
        assert label not in printed and label not in saved
    assert "别的 0/1" in printed


# ---------- J3.9 查出的三处（11-15 夜） ----------


def test_an_ended_relation_is_paired_with_the_one_that_takes_over() -> None:
    relations = side_from_dict({**side_dict(), "relations": [
        {"id": "old", "subject": "e1", "relation": "持有", "object": "e3", "state": "已结束",
         "when": {"from": "2026-10-01", "to": "2026-11-01", "precision": "day"}, "evidence": [sid("1.6.1")]},
        {"id": "new", "subject": "e1", "relation": "持有", "object": "e6",  # 转仓：同一个主体、同一种关系，结束那几天开始的
         "when": {"from": "2026-11-02", "to": None, "precision": "day"}, "evidence": [sid("1.8.1")]},
    ]}).relations
    (change,) = relation_changes(relations)
    assert (change.before, change.after) == (Ref("relation", "old"), Ref("relation", "new"))
    assert change.evidence == (sid("1.8.1"),)  # 变的那一句：后一头在前一头最后一句之后的第一句


def test_relations_l3_marked_with_problems_are_left_out_like_facts_at_l4(tmp_path: Path) -> None:
    extraction = {"entities": [{"key": "e1", "mention": "我", "name": "主人", "type": "人"},
                               {"key": "e2", "mention": "远帆ADR", "name": "远帆 ADR", "type": "资产"}],
                  "facts": [],
                  "relations": [
                      {"subject": "e1", "relation": "持有", "object": "e2", "evidence": [{"ref": "T1", "quote": "编的"}],
                       "problems": [], "notes": ["precision_guessed"]},  # notes 不是问题，照收
                      {"subject": "e1", "relation": "持有", "object": "e2", "evidence": [{"ref": "C1", "quote": "编的"}],
                       "problems": ["no_usable_evidence", "context_ref"]},  # 只引了上文
                      {"subject": "e1", "relation": "客户是", "object": "e2", "evidence": [{"ref": "T1", "quote": "编的"}],
                       "problems": "duplicate_key"},
                  ]}
    _store(tmp_path / "l3" / "l3.db", [(f"{P}#w1", "k1", [_line("C1", "1.5.1"), _line("T1", "1.6.1")], extraction, "2026-11-02")])
    side, _ = side_from_result(tmp_path, [P])
    (relation,) = side.relations.values()
    assert (relation.relation, relation.evidence) == ("持有", (sid("1.6.1"),))
    assert side.described["没收的关系"] == {"L3 标了问题（context_ref、no_usable_evidence）": 1, "L3 标了问题（duplicate_key）": 1}


def test_a_repeated_entity_key_means_the_first_entity_like_l4_and_l5(tmp_path: Path) -> None:
    extraction = {"entities": [{"key": "e1", "mention": "我", "name": "主人", "type": "人"},
                               {"key": "e2", "mention": "远帆ADR", "name": "远帆 ADR", "type": "资产"},
                               {"key": "e2", "mention": "远帆", "name": "远帆", "type": "公司或机构", "problems": ["duplicate_key"]}],
                  "facts": [{"text": "远帆 ADR 收盘 13.2。", "subject": "e2", "attribute": "收盘价", "value": "13.2",
                             "entities": [{"key": "e2", "role": "subject"}], "evidence": [{"ref": "T1", "quote": "编的"}]}],
                  "relations": [{"subject": "e1", "relation": "持有", "object": "e2", "evidence": [{"ref": "T1", "quote": "编的"}]}]}
    _store(tmp_path / "l3" / "l3.db", [(f"{P}#w1", "k1", [_line("T1", "1.6.1")], extraction, "2026-11-02")])
    side, _ = side_from_result(tmp_path, [P])
    (relation,) = side.relations.values()
    (fact,) = side.facts.values()
    assert side.entities[relation.object].name == "远帆 ADR" and side.entities[fact.subject].name == "远帆 ADR"  # type: ignore[index]


# ---------- D112、D115：关系的开始日子没写算对；这一边的先合成一条再判（11-15 定的） ----------


def test_d112_a_relation_without_a_start_date_counts_as_right_in_the_official_number(gold_dir: Path) -> None:
    gold = load(gold_dir)
    data = side_dict()
    data["relations"] = [
        {"id": "a", "subject": "e1", "relation": "持有", "object": "e3", "evidence": [sid("1.6.1")]},  # 金标准知道开始日子，这里没写
        {"id": "b", "subject": "e1", "relation": "亲属", "object": "e4", "fields": {"kinship": "丈夫"}, "evidence": [sid("1.7.1")]},
    ]
    relations = metrics(score(side_from_dict(data), gold), gold)["relations"]
    assert relations["verdicts"] == {"对": 1, "日子没给": 1}
    assert (relations["precision"], relations["precision_strict"], relations["precision_loose"]) == (1.0, 0.5, 1.0)
    assert (relations["recall"], relations["recall_strict"], relations["recall_loose"]) == (1.0, 0.5, 1.0)
    data["relations"][0]["when"] = {"from": "2026-10", "precision": "month"}  # 写到月、和金标准那天对得上：不算差 3 天以上
    relations = metrics(score(side_from_dict(data), gold), gold)["relations"]
    assert relations["verdicts"] == {"对": 1, "日子太粗": 1} and relations["precision"] == 1.0 and relations["precision_strict"] == 0.5
    data["relations"][0]["when"] = {"from": "2026-12-01", "precision": "day"}  # 写了、差 3 天以上：算错
    relations = metrics(score(side_from_dict(data), gold), gold)["relations"]
    assert relations["verdicts"] == {"对": 1, "日子不对": 1} and relations["precision"] == 0.5 and relations["precision_loose"] == 1.0


def test_d115_the_sides_relations_with_the_same_ends_and_kind_are_merged_before_judging(gold_dir: Path) -> None:
    gold = load(gold_dir)
    data = side_dict()
    data["entities"].append({"id": "e7", "name": "YF", "type": "资产"})  # 也对到金标准的远帆 ADR
    data["relations"] = [
        {"id": "a", "subject": "e1", "relation": "持有", "object": "e3", "when": {"from": "2026-10-14", "precision": "day"},
         "evidence": [sid("1.6.1")]},
        {"id": "b", "subject": "e1", "relation": "持有", "object": "e7", "when": {"from": "2026-10", "precision": "month"},
         "evidence": [sid("1.3.1", Q)]},
        {"id": "c", "subject": "e1", "relation": "持有", "object": "e3", "evidence": [sid("1.9.1")]},  # 没写日子
        {"id": "d", "subject": "e1", "relation": "持有", "object": "e6", "evidence": [sid("1.6.1")]},  # 对不上金标准的：按这一边的实体合
        {"id": "e", "subject": "e1", "relation": "持有", "object": "e6", "evidence": [sid("1.8.1")]},
        {"id": "k1", "subject": "e1", "relation": "亲属", "object": "e4", "fields": {"kinship": "丈夫"}, "evidence": [sid("1.7.1")]},
        {"id": "k2", "subject": "e1", "relation": "亲属", "object": "e4", "fields": {"kinship": "丈夫"}, "evidence": [sid("1.10.1")]},
        {"id": "k3", "subject": "e1", "relation": "亲属", "object": "e4", "fields": {"kinship": "哥哥"}, "evidence": [sid("1.11.1")]},
    ]
    result = score(side_from_dict(data), gold)
    merged = {tuple(sorted(ids)): result.merged_relations[mid] for mid, ids in result.merged_members.items()}
    assert set(merged) == {("a", "b", "c"), ("d", "e"), ("k1", "k2"), ("k3",)}  # 亲属照旧比具体说法：说法不一样的不合
    assert merged[("a", "b", "c")].when == When("2026-10", None, "month")  # 有日子的里面最早的（10-01 比 10-14 早）
    assert merged[("a", "b", "c")].evidence == (sid("1.6.1"), sid("1.9.1"), sid("1.3.1", Q))
    relations = metrics(result, gold)["relations"]
    assert relations["verdicts"] == {"两头对不上实体": 1, "具体关系不对": 1, "对": 1, "日子太粗": 1}
    assert (relations["side"], relations["precision"], relations["precision_strict"]) == (4, 0.5, 0.25)
    each = relations["each"]  # 每条单独判的也列着
    assert each["side"] == 8 and each["verdicts"]["日子没给"] == 1 and each["precision_strict"] == 0.375
    data["relations"] = [
        {"id": "s1", "subject": "e1", "relation": "持有", "object": "e3", "state": "已结束",
         "when": {"from": "2026-10-14", "to": "2026-10-20", "precision": "day"}, "evidence": [sid("1.6.1")]},
        {"id": "s2", "subject": "e1", "relation": "持有", "object": "e3", "when": {"from": "2026-11-20", "precision": "day"},
         "evidence": [sid("1.9.1")]},  # 结束以后又有了：另一段
        {"id": "s3", "subject": "e1", "relation": "持有", "object": "e3", "when": {"from": "2026-10-14", "precision": "day"},
         "evidence": [sid("1.10.1")]},  # 后面又说了一遍前一段
        {"id": "s4", "subject": "e1", "relation": "持有", "object": "e3", "evidence": [sid("1.11.1")]},  # 没写日子、说在结束以后
    ]
    result = score(side_from_dict(data), gold)
    groups = {tuple(sorted(ids)) for ids in result.merged_members.values()}
    assert groups == {("s1", "s3"), ("s2", "s4")}
    ended = next(one for mid, one in result.merged_relations.items() if result.merged_members[mid][0] == "s1")
    assert ended.when == When("2026-10-14", "2026-10-20", "day") and ended.state == "已结束"


def test_scores_only_prints_the_official_strict_and_loose_relation_numbers(gold_dir: Path, tmp_path: Path,
                                                                           capsys: pytest.CaptureFixture[str]) -> None:
    side = tmp_path / "side.json"
    side.write_text(json.dumps(side_dict(), ensure_ascii=False), encoding="utf-8")
    assert main(["score", "--gold", str(gold_dir), "--side", f"编的={side}", "--scores-only"]) == 0
    printed = capsys.readouterr().out
    assert "| 关系准确率 | 50.0%（合成后 4 条、没写开始日子算对；严格 50.0%、不看日子 50.0%） |" in printed
    assert "| 关系准确率（每条单独判） | 40.0%（5 条；严格 40.0%、不看日子 60.0%） |" in printed
    assert "| 关系覆盖率 | 100.0%（金标准 2 条；严格 100.0%、不看日子 100.0%） |" in printed
    assert "| 关系覆盖率（每条单独判） | 100.0%（严格 100.0%、不看日子 100.0%） |" in printed


# ---------- D120：关系的日子差 7 天以内算对（原来 3 天），开始、结束日子都照这个 ----------


def test_d120_relation_dates_within_seven_days_count_as_right(gold_dir: Path) -> None:
    gold = load(gold_dir)
    data = side_dict()
    data["relations"] = [{"id": "a", "subject": "e1", "relation": "持有", "object": "e3",
                          "when": {"from": "2026-10-18", "precision": "day"}, "evidence": [sid("1.6.1")]}]  # 金标准 10-13，差 5 天
    assert metrics(score(side_from_dict(data), gold), gold)["relations"]["verdicts"] == {"对": 1}
    narrow = score(side_from_dict(data), gold, options=Options(tolerance_days=3))  # 照原来的 3 天：算错
    assert metrics(narrow, gold)["relations"]["verdicts"] == {"日子不对": 1}
    data["relations"][0]["when"] = {"from": "2026-10-21", "precision": "day"}  # 差 8 天：还是错
    assert metrics(score(side_from_dict(data), gold), gold)["relations"]["verdicts"] == {"日子不对": 1}
    relations = side_from_dict({**side_dict(), "relations": [
        {"id": "old", "subject": "e1", "relation": "持有", "object": "e3", "state": "已结束",
         "when": {"from": "2026-10-01", "to": "2026-11-01", "precision": "day"}, "evidence": [sid("1.6.1")]},
        {"id": "new", "subject": "e1", "relation": "持有", "object": "e6",  # 结束以后 5 天开始的：结束日子也照 7 天
         "when": {"from": "2026-11-06", "precision": "day"}, "evidence": [sid("1.8.1")]},
    ]}).relations
    (change,) = relation_changes(relations)
    assert change.after == Ref("relation", "new")
    assert relation_changes(relations, tolerance_days=3)[0].after is None  # 照原来的 3 天配不上
