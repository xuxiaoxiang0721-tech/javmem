"""冻结标记和主人的决定：整本重建前导出、重建后导回（D73 第 7 条）。样例都是编的，抽取用手写的 L3 结果。

每个测试在旧账本里冻结、定决定，导出；新目录收同样的包、写账（整本重建），导回；再比两本。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World
from test_review import by_value, reviews
from test_withdraw_review import rebuilt_world
from test_write import day_packet, deliver, l3_fact, ship_extract, write

from javmem.cli import main
from javmem.decisions import export_decisions, import_decisions
from javmem.review import ReviewError, decide, freeze
from javmem.withdraw import withdraw

P1 = "2026-11-01_2330_desk-bot"
DAYS = [
    day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
    day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"),
    day_packet("2026-11-05", "我：越南那批订单再改到 11-25 出货。"),
]


def renamed_extract(window):  # noqa: ANN001, ANN201
    """L5 改了以后的样子：同一个订单换了个标准名（实体编号跟着变，事实编号也变），证据、属性、值不变。"""
    found = ship_extract(window)
    found["entities"] = [{"key": "e1", "mention": "越南那批订单", "name": "越南订单（那批）", "type": "订单或合同"}]
    return found


def exported_to(world: World, tmp_path: Path) -> Path:
    path = tmp_path / "decisions.json"
    assert main(["review", "--library", "prod", "--export", str(path)]) == 0
    return path


def rebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extract=ship_extract, packets=DAYS) -> World:  # noqa: ANN001
    world = rebuilt_world(tmp_path, monkeypatch)
    deliver(world, *packets)
    write(world, extract).close()
    return world


def state(world: World) -> dict[str, tuple[object, ...]]:
    """按值看每一条：冻结没有、状态、待确认的原因、被哪个值替代了。"""
    values = by_value(world)
    by_id = {row["fact_id"]: value for value, row in values.items()}
    return {
        value: (row["frozen"], row["status"], row["pending_reason"], by_id.get(row["superseded_by"]))
        for value, row in values.items()
    }


def decided(world: World) -> list[tuple[object, ...]]:
    """定过的待确认：种类、决定、谁定的、什么时候定的，两边按值写。"""
    values = {row["fact_id"]: value for value, row in by_value(world).items()}
    out = []
    for row in reviews(world, open_only=False):
        if row["decision"] is None:
            continue
        claims = row["claims"]
        sides = tuple(values.get(claims.get(name), claims.get(name)) for name in ("frozen", "new", "fact") if claims.get(name))
        at = world.rows("SELECT decided_at FROM review_queue WHERE review_id = ?", row["review_id"])[0]["decided_at"]
        out.append((row["kind"], row["decision"], row["decided_by"], at, sides))
    return out


def opened(world: World) -> list[tuple[object, ...]]:
    values = {row["fact_id"]: value for value, row in by_value(world).items()}
    rows = world.rows("SELECT kind, claims, created_at FROM review_queue WHERE decided_at IS NULL ORDER BY review_id")
    out = []
    for row in rows:
        claims = json.loads(row["claims"])
        sides = tuple(values.get(claims.get(name), claims.get(name)) for name in ("frozen", "new", "fact") if claims.get(name))
        out.append((row["kind"], sides, row["created_at"]))
    return out


# ---------- 冻结、和冻结的冲突 ----------


@pytest.mark.parametrize("extract", [ship_extract, renamed_extract], ids=["same-ids", "ids-changed"])
def test_frozen_marks_and_decided_conflicts_come_back_after_a_rebuild(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extract
) -> None:  # noqa: ANN001
    deliver(world, *DAYS)
    write(world, ship_extract).close()
    values = by_value(world)
    freeze(world.settings, "prod", [str(values["11-15"]["fact_id"])])  # 11-20、11-25 都和它冲突
    pending = sorted(reviews(world), key=lambda row: row["review_id"])
    assert len(pending) == 2
    decide(world.settings, "prod", int(pending[0]["review_id"]), "both")  # 11-20：两边都留
    world.execute("UPDATE review_queue SET created_at = '2026-11-06T09:00:00+08:00' WHERE decided_at IS NULL")  # 建了几天了
    before = (state(world), decided(world), opened(world))
    assert before[0]["11-15"][0] == 1 and before[2] and before[1]

    path = exported_to(world, tmp_path)
    new = rebuilt(tmp_path, monkeypatch, extract)
    assert state(new) != before[0]  # 重建完：冻结标记、决定都没了
    assert main(["review", "--library", "prod", "--import", str(path)]) == 0
    assert (state(new), decided(new), opened(new)) == before  # 冻结、新旧、定过的、没处理的（连建的时间）都一样
    assert new.rows("SELECT count(*) AS n FROM facts")[0]["n"] == world.rows("SELECT count(*) AS n FROM facts")[0]["n"]


def test_import_reports_matches_and_refuses_a_ledger_that_already_has_decisions(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliver(world, *DAYS[:2])
    write(world, ship_extract).close()
    freeze(world.settings, "prod", [str(by_value(world)["11-15"]["fact_id"])])
    data = export_decisions(world.settings, "prod")
    assert {fact["frozen"] for fact in data["facts"].values()} == {True, False}  # 冻结的和冲突的那一方都导出来
    new = rebuilt(tmp_path, monkeypatch, renamed_extract, DAYS[:2])
    report = import_decisions(new.settings, "prod", data)
    assert (report.frozen, report.same_id, len(report.by_evidence), report.recreated) == (1, 0, 2, [])
    with pytest.raises(ReviewError):  # 再导一次：已经有冻结标记了，不导
        import_decisions(new.settings, "prod", data)


def test_records_of_another_library_are_refused(world: World, tmp_path: Path) -> None:
    deliver(world, *DAYS[:1])
    write(world, ship_extract).close()
    data = export_decisions(world.settings, "prod")
    from javmem.config import ConfigError

    with pytest.raises(ConfigError):
        import_decisions(world.settings, "test", data)


def test_export_does_not_make_an_empty_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(empty))
    assert main(["review", "--library", "prod", "--export", str(tmp_path / "x.json")]) == 2
    assert not (empty / "ledger" / "prod.db").exists()


# ---------- 新账本里认不出来的冻结事实：照旧样子补回来 ----------


def test_a_frozen_fact_without_claims_is_brought_back_as_it_was(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """包重发以后说法没了、只剩冻结标记撑着的事实（证据指着旧版本），重建时收的是新版本，L3 抽不出它：照旧补回来。"""
    deliver(world, DAYS[0])
    write(world, ship_extract).close()
    old = str(by_value(world)["11-15"]["fact_id"])
    freeze(world.settings, "prod", [old])
    changed = DAYS[0].replace("11-15 出货", "下周出货").replace("privacy: normal", "privacy: normal\nrevision: 2")
    deliver(world, changed)
    write(world, ship_extract).close()
    assert world.rows("SELECT count(*) AS n FROM fact_claims WHERE fact_id = ?", old)[0]["n"] == 0
    path = exported_to(world, tmp_path)

    new = rebuilt(tmp_path, monkeypatch, ship_extract, [changed])
    assert by_value(new) == {}
    assert main(["review", "--library", "prod", "--import", str(path)]) == 1  # 有补回来的，要人看一下
    [row] = new.rows("SELECT * FROM facts")
    assert (row["fact_id"], row["frozen"], row["value"], row["withdrawn_at"]) == (old, 1, "11-15", None)
    assert new.rows("SELECT count(*) AS n FROM evidence WHERE fact_id = ?", old)[0]["n"] == 0  # 旧版本不在新账本里


def test_who_a_brought_back_fact_is_from_comes_back_too(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """J4.1：L3 写的“这条说法出自谁”跟着冻结的事实导出、补回来；以前导出的文件里没有这一项，补回来是空的。"""

    def extract(window):  # noqa: ANN001, ANN202
        found = ship_extract(window)
        for fact in found["facts"]:
            fact["speaker"] = "主人"
        return found

    deliver(world, DAYS[0])
    write(world, extract).close()
    old = str(by_value(world)["11-15"]["fact_id"])
    freeze(world.settings, "prod", [old])
    data = export_decisions(world.settings, "prod")
    assert data["facts"][old]["attributed_to"] == "主人"
    older = json.loads(json.dumps(data))
    del older["facts"][old]["attributed_to"]

    changed = DAYS[0].replace("11-15 出货", "下周出货")
    new = rebuilt(tmp_path, monkeypatch, extract, [changed])  # L3 抽不出它：照旧样子补回来
    assert import_decisions(new.settings, "prod", data).recreated == [old]
    assert new.rows("SELECT attributed_to FROM facts WHERE fact_id = ?", old)[0]["attributed_to"] == "主人"
    new.execute("DELETE FROM facts WHERE fact_id = ?", old)
    assert import_decisions(new.settings, "prod", older).recreated == [old]
    assert new.rows("SELECT attributed_to FROM facts WHERE fact_id = ?", old)[0]["attributed_to"] is None


def test_owner_choices_on_hidden_frozen_facts_come_back(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """证据都撤回了的冻结事实：放回来的（主人担保）、接着藏着的、还没定的，重建以后都照旧。"""
    packets = [
        day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"),
        day_packet("2026-11-02", "我：泰国那批订单 11-18 出货。", source="desk-bot"),
    ]

    def extract(window):  # noqa: ANN001, ANN202
        found = []
        for line in window.targets:
            for date_text in ("11-15", "11-18"):
                if date_text in line.text:
                    found.append(l3_fact(window, line.text.rstrip("。"), f"出货日期是 {date_text}", attribute=f"出货日期{date_text}", value=date_text))
        return {"entities": [], "facts": found}

    deliver(world, *packets)
    write(world, extract).close()
    values = by_value(world)
    freeze(world.settings, "prod", [str(values["11-15"]["fact_id"]), str(values["11-18"]["fact_id"])])
    withdraw(world.settings, "prod", packets=[P1, "2026-11-02_2330_desk-bot"])
    hidden = sorted(reviews(world), key=lambda row: row["review_id"])
    assert len(hidden) == 2
    decide(world.settings, "prod", int(hidden[0]["review_id"]), "frozen")  # 放回来：没有证据也看得到
    before_facts = [dict(row) for row in world.rows("SELECT fact_id, value, frozen, withdrawn_at IS NULL AS shown FROM facts ORDER BY fact_id")]
    before = (decided(world), opened(world))

    records = tmp_path / "withdrawals.json"
    assert main(["withdraw", "--library", "prod", "--export", str(records)]) == 0
    path = exported_to(world, tmp_path)
    new = rebuilt_world(tmp_path, monkeypatch)
    assert main(["withdraw", "--library", "prod", "--import", str(records)]) == 0
    deliver(new, *packets)  # 撤回过的包不收
    assert main(["review", "--library", "prod", "--import", str(path)]) == 1
    after_facts = [dict(row) for row in new.rows("SELECT fact_id, value, frozen, withdrawn_at IS NULL AS shown FROM facts ORDER BY fact_id")]
    assert after_facts == before_facts
    assert (decided(new), opened(new)) == before


def test_a_brought_back_fact_stores_its_scope_like_the_writer(world: World) -> None:
    """真实数据的演练里查出来的：补回来的事实，可见范围里的中文写成了 \\uXXXX 这样的转义，和写账时写的不一样。"""
    from javmem.decisions import SCHEMA

    fact = {
        "fact_id": "F0000000000000001", "content": "库存 300 件", "attribute": None, "value": None, "frozen": True,
        "scope": {"partitions": ["finance"], "sensitive": ["薪资", "AB"]}, "evidence": [], "entities": [], "subject": None,
        "status": "verified", "time_precision": "unknown", "notes": [], "sources": [], "origins": [], "said_from": None,
    }
    deliver(world, DAYS[0])
    write(world, ship_extract).close()
    report = import_decisions(world.settings, "prod", {"schema": SCHEMA, "library": "prod", "facts": {fact["fact_id"]: fact}, "reviews": []})
    assert report.recreated == ["F0000000000000001"]
    [row] = world.rows("SELECT scope_partitions, scope_sensitive FROM facts WHERE fact_id = 'F0000000000000001'")
    assert (row["scope_partitions"], row["scope_sensitive"]) == ('["finance"]', '["AB", "薪资"]')


# ---------- 实体有歧义的决定 ----------


def test_a_decided_ambiguity_stays_decided_after_a_rebuild(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def extract(window):  # noqa: ANN001, ANN202
        text = window.targets[0].text
        name = "周建国" if "周建国" in text else "周立新" if "周立新" in text else "老周"
        entity = {"key": "e1", "mention": "老周", "name": name, "type": "人"}
        return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}

    packets = [
        day_packet("2026-11-01", "我：周建国就是老周，管越南。"),
        day_packet("2026-11-02", "我：周立新也叫老周，管泰国。"),
        day_packet("2026-11-03", "我：老周明天回来。"),
    ]
    deliver(world, *packets)
    write(world, extract).close()
    [item] = reviews(world)
    decide(world.settings, "prod", int(item["review_id"]), "both")
    status = world.rows("SELECT status FROM facts WHERE content = '老周明天回来'")[0]["status"]
    path = exported_to(world, tmp_path)

    new = rebuilt(tmp_path, monkeypatch, extract, packets)
    assert new.rows("SELECT status FROM facts WHERE content = '老周明天回来'")[0]["status"] == "pending"
    assert main(["review", "--library", "prod", "--import", str(path)]) == 0
    assert new.rows("SELECT status FROM facts WHERE content = '老周明天回来'")[0]["status"] == status == "verified"
    assert reviews(new) == []
    assert decided(new) == decided(world)


def zhou_extract(window):  # noqa: ANN001, ANN201
    text = window.targets[0].text
    name = "周建国" if "周建国" in text else "周立新" if "周立新" in text else "老周"
    entity = {"key": "e1", "mention": "老周", "name": name, "type": "人"}
    return {"entities": [entity], "facts": [l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1")]}


ZHOU = [
    day_packet("2026-11-01", "我：周建国就是老周，管越南。"),
    day_packet("2026-11-02", "我：周立新也叫老周，管泰国。"),
    day_packet("2026-11-03", "我：老周明天回来。"),
]


def test_a_decided_ambiguity_written_before_the_scope_was_recorded_is_applied(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审阅第 4 条：记下可见范围以前写的“实体有歧义”（claims 里没有 scope，写账照旧当哪个范围都算），导回时对不上
    新账本里那条没处理的，事实一直待确认。"""
    deliver(world, *ZHOU)
    write(world, zhou_extract).close()
    [item] = reviews(world)
    decide(world.settings, "prod", int(item["review_id"]), "both")
    world.execute("UPDATE review_queue SET claims = json_remove(claims, '$.scope') WHERE kind = 'ambiguous_entity'")
    path = exported_to(world, tmp_path)
    new = rebuilt(tmp_path, monkeypatch, zhou_extract, ZHOU)
    assert main(["review", "--library", "prod", "--import", str(path)]) == 0
    assert new.rows("SELECT status FROM facts WHERE content = '老周明天回来'")[0]["status"] == "verified"
    assert reviews(new) == []


# ---------- 审阅第 3 条：主体换了的那一档，不能认到同一句话说的别的主体上 ----------

BOTH = "我：越南那批订单和泰国那批订单都是 11-15 出货。"
VN = {"key": "e1", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}
TH = {"key": "e2", "mention": "泰国那批订单", "name": "泰国那批订单", "type": "订单或合同"}


def both_extract(window):  # noqa: ANN001, ANN201
    line = window.targets[0].text.rstrip("。")
    if "2000" in line:
        return {"entities": [VN], "facts": [l3_fact(window, line, "越南那批订单数量 2000 件", subject="e1", attribute="数量", value="2000 件")]}
    return {"entities": [VN, TH], "facts": [
        l3_fact(window, line, "越南那批订单出货日期是 11-15", subject="e1", attribute="出货日期", value="11-15"),
        l3_fact(window, line, "泰国那批订单出货日期是 11-15", subject="e2", attribute="出货日期", value="11-15"),
    ]}


def thailand_only(window):  # noqa: ANN001, ANN201
    found = both_extract(window)
    found["facts"] = [fact for fact in found["facts"] if "越南那批订单出货" not in fact["text"]]
    found["entities"] = [entity for entity in found["entities"] if entity["key"] == "e2"] or found["entities"]
    return found


@pytest.mark.parametrize("packets", [[day_packet("2026-11-01", BOTH)],
                                     [day_packet("2026-11-01", BOTH), day_packet("2026-11-02", "我：越南那批订单的数量是 2000 件。")]],
                         ids=["subject-gone", "subject-still-there"])
def test_a_frozen_fact_is_not_moved_onto_another_subject_of_the_same_sentence(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packets: list[str]
) -> None:
    deliver(world, *packets)
    write(world, both_extract).close()
    [vn] = [row["fact_id"] for row in world.rows("SELECT fact_id FROM facts WHERE content = '越南那批订单出货日期是 11-15'")]
    freeze(world.settings, "prod", [vn])
    path = exported_to(world, tmp_path)
    new = rebuilt(tmp_path, monkeypatch, thailand_only, packets)
    assert main(["review", "--library", "prod", "--import", str(path)]) == 1  # 认不出来、补回来了：要人看一下
    frozen = [row["content"] for row in new.rows("SELECT content FROM facts WHERE frozen = 1")]
    assert frozen == ["越南那批订单出货日期是 11-15"]  # 泰国那条不冻结


# ---------- 审阅第 7 条：补回来的事实要看新账本里的撤回 ----------


@pytest.mark.parametrize("how", ["packet", "sentence"])
def test_a_brought_back_fact_whose_evidence_was_withdrawn_later_stays_hidden(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """先导出了冻结标记和决定，后来又撤回了它的证据、再导出撤回记录：新账本里这条事实补回来时要藏着、进待确认。"""
    deliver(world, DAYS[0])
    write(world, ship_extract).close()
    [fact] = [row["fact_id"] for row in world.rows("SELECT fact_id FROM facts")]
    freeze(world.settings, "prod", [fact])
    path = exported_to(world, tmp_path)
    if how == "packet":
        withdraw(world.settings, "prod", packets=[P1])
    else:
        withdraw(world.settings, "prod", sentences=[f"{P1}#1.1.1"])
    records = tmp_path / "withdrawals.json"
    assert main(["withdraw", "--library", "prod", "--export", str(records)]) == 0
    new = rebuilt_world(tmp_path, monkeypatch)
    assert main(["withdraw", "--library", "prod", "--import", str(records)]) == 0
    deliver(new, DAYS[0])
    write(new, ship_extract).close()
    assert main(["review", "--library", "prod", "--import", str(path)]) == 1
    [row] = new.rows("SELECT fact_id, frozen, withdrawn_at FROM facts")
    assert (row["fact_id"], row["frozen"]) == (fact, 1) and row["withdrawn_at"] is not None
    assert new.rows("SELECT count(*) AS n FROM evidence WHERE fact_id = ?", fact)[0]["n"] == 0
    [item] = reviews(new)
    assert item["claims"]["reason"] == "evidence_withdrawn" and item["claims"]["fact"] == fact


def test_a_fact_hidden_on_import_gets_an_open_review_even_after_an_old_decided_one(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二遍审阅 N5：导回时藏起来的事实，补“证据都撤回了”的待确认时看的是有没有这一种，连定过的（自动关掉的
    resupported）也算，结果藏着、却没有一条能定的待确认。"""
    second = day_packet("2026-11-02", "我：越南那批订单 11-15 出货。")
    deliver(world, DAYS[0])
    write(world, ship_extract).close()
    [fact] = [row["fact_id"] for row in world.rows("SELECT fact_id FROM facts")]
    freeze(world.settings, "prod", [fact])
    withdraw(world.settings, "prod", packets=[P1])  # 藏起来，进待确认
    deliver(world, second)
    write(world, ship_extract).close()  # 别的包又说了：放出来，那条待确认自动关掉（resupported）
    path = exported_to(world, tmp_path)
    withdraw(world.settings, "prod", packets=["2026-11-02_2330_desk-bot"])  # 导出冻结标记以后才撤回
    records = tmp_path / "withdrawals.json"
    assert main(["withdraw", "--library", "prod", "--export", str(records)]) == 0
    new = rebuilt_world(tmp_path, monkeypatch)
    assert main(["withdraw", "--library", "prod", "--import", str(records)]) == 0
    deliver(new, DAYS[0], second)
    write(new, ship_extract).close()
    assert main(["review", "--library", "prod", "--import", str(path)]) == 1
    assert new.rows("SELECT withdrawn_at IS NOT NULL AS hidden FROM facts WHERE fact_id = ?", fact)[0]["hidden"] == 1
    [item] = reviews(new)  # 和旧账本一样，有一条没处理的
    assert item["claims"]["reason"] == "evidence_withdrawn" and item["claims"]["fact"] == fact


# ---------- 命令行 ----------


def test_import_command_refuses_a_broken_file_and_mixed_options(world: World, tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{不是 JSON", encoding="utf-8")
    assert main(["review", "--library", "prod", "--import", str(broken)]) == 2
    assert main(["review", "--library", "prod", "--remind", "--export", str(tmp_path / "x.json")]) == 2
