"""关系的冻结、待确认、导出导回、合并工具改指（J4.4 第 3 段）。例子都是编的。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World
from spotter import SpotModel, extract_into_store
from test_merging import LEXICON_TEXT, STANDARD, proposal_of
from test_relations_write import bank_extract, mail_relations, resent
from test_withdraw_mail import MASK, SENDER, delivered, two_mails
from test_write import day_packet, deliver, write

from javmem.cli import main
from javmem.config import Settings
from javmem.decisions import export_decisions, import_decisions
from javmem.ledger import open_ledger
from javmem.review import ReviewError, freeze, open_items
from javmem.withdraw import export_records, withdraw


def one_relation(world: World) -> str:
    rows = world.rows("SELECT relation_id FROM relations")
    assert len(rows) == 1
    return str(rows[0]["relation_id"])


def test_freezing_a_relation_keeps_it_when_its_claims_go_and_undo_lets_it_go(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    relation_id = one_relation(world)
    assert main(["freeze", "--library", "prod", "--relation", relation_id]) == 0
    assert world.rows("SELECT frozen FROM relations")[0]["frozen"] == 1
    deliver(world, resent("2026-11-01", "我：今天没什么事。"))  # 包重发，那一句没了：说法没了，冻结的留着
    assert one_relation(world) == relation_id
    assert world.rows("SELECT count(*) AS n FROM relation_claims")[0]["n"] == 0
    assert main(["freeze", "--library", "prod", "--relation", relation_id, "--undo"]) == 0
    assert world.rows("SELECT count(*) AS n FROM relations")[0]["n"] == 0  # 解除冻结：没有说法撑着，删掉


def test_freeze_refuses_unknown_and_hidden_relations(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    relation_id = one_relation(world)
    with pytest.raises(ReviewError, match="R0000000000000000"):
        freeze(world.settings, "prod", ["R0000000000000000"])
    freeze(world.settings, "prod", [relation_id])
    assert main(["withdraw", "--library", "prod", "--packet", "2026-11-01_2330_desk-bot"]) == 0
    with pytest.raises(ReviewError, match="藏着"):
        freeze(world.settings, "prod", [relation_id], frozen=False)
    assert main(["freeze", "--library", "prod"]) == 2  # 一个编号都没写


def test_a_hidden_frozen_relation_comes_back_when_it_is_said_again(world: World) -> None:
    """证据都撤回了、藏着的冻结关系，后来又有人说了同一件事（中间没写结束）：放回来，那条待确认自动关掉（和事实一样）。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    relation_id = one_relation(world)
    freeze(world.settings, "prod", [relation_id])
    assert main(["withdraw", "--library", "prod", "--packet", "2026-11-01_2330_desk-bot"]) == 0
    assert world.rows("SELECT withdrawn_at FROM relations")[0]["withdrawn_at"] is not None
    deliver(world, day_packet("2026-11-05", "我：X1 票据还在海港银行。"))
    write(world, bank_extract).close()
    assert one_relation(world) == relation_id
    (row,) = world.rows("SELECT withdrawn_at, frozen FROM relations")
    assert row["withdrawn_at"] is None and row["frozen"] == 1
    assert world.rows("SELECT count(*) AS n FROM relation_claims WHERE relation_id = ?", relation_id)[0]["n"] == 1
    (review,) = world.rows("SELECT decision, decided_by FROM review_queue")
    assert (review["decision"], review["decided_by"]) == ("resupported", "auto")


def frozen_bank(world: World) -> str:
    """11-01 说 X1 票据在海港银行，主人冻结了这一条。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    relation_id = one_relation(world)
    freeze(world.settings, "prod", [relation_id])
    return relation_id


def test_a_later_end_does_not_end_a_frozen_relation_and_waits_for_the_owner(world: World) -> None:
    """冻结的一直现行（和事实一样不被替代）：后来有人说它结束了，冻结的不动；说结束的那一处自己一段，进待确认
    （frozen_conflict）。主人留原值：冲突关掉，冻结的照旧现行，说结束的那一段照它自己的（已结束），不算谁替代谁。"""
    frozen = frozen_bank(world)
    deliver(world, day_packet("2026-11-03", "我：X1 票据从海港银行转走了。"))
    write(world, bank_extract).close()
    rows = {row["relation_id"]: row for row in world.rows("SELECT * FROM relations")}
    assert (rows[frozen]["state"], rows[frozen]["frozen"], rows[frozen]["superseded_by"]) == ("active", 1, None)
    (ended,) = [row for one, row in rows.items() if one != frozen]
    assert (ended["state"], ended["status"], ended["pending_reason"]) == ("ended", "pending", "frozen_conflict")
    (review,) = world.rows("SELECT review_id, kind, claims FROM review_queue")
    assert review["kind"] == "frozen_conflict" and json.loads(review["claims"]) == {"frozen": frozen, "new": ended["relation_id"]}
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "frozen"]) == 0
    rows = {row["relation_id"]: row for row in world.rows("SELECT * FROM relations")}
    assert (rows[frozen]["state"], rows[frozen]["frozen"]) == ("active", 1)
    after = rows[ended["relation_id"]]
    assert (after["state"], after["superseded_by"]) == ("ended", None) and after["status"] != "pending"


def test_keeping_the_later_end_unfreezes_the_relation_and_ends_it(world: World) -> None:
    """主人用新值：解除冻结，照常算，那一段到说结束的那一处为止（并成一条）。"""
    frozen = frozen_bank(world)
    deliver(world, day_packet("2026-11-03", "我：X1 票据从海港银行转走了。"))
    write(world, bank_extract).close()
    review = world.rows("SELECT review_id FROM review_queue WHERE kind = 'frozen_conflict'")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "new"]) == 0
    (row,) = world.rows("SELECT relation_id, state, frozen FROM relations")
    assert (row["relation_id"], row["state"], row["frozen"]) == (frozen, "ended", 0)


def test_a_frozen_relation_without_claims_is_not_ended_and_a_restart_joins_it(world: World) -> None:
    """审阅第 3 条：说法没了的冻结关系（包重发，那一句没了），后来有人说转走了、又说放回来：说转走的那一处自己一段进待确认，
    冻结的不被结束，以前那一段的样子不被新的一段换掉；放回来的那一处说的就是冻结的这一条，接到它上面（和事实一样，
    有了说法以后证据照说法）。"""
    frozen = frozen_bank(world)
    deliver(world, resent("2026-11-01", "我：今天没什么事。"))
    write(world, bank_extract).close()
    deliver(world, day_packet("2026-11-03", "我：X1 票据从海港银行转走了。"))
    write(world, bank_extract).close()
    rows = {row["relation_id"]: row for row in world.rows("SELECT * FROM relations")}
    assert (rows[frozen]["state"], rows[frozen]["frozen"], rows[frozen]["as_of"][:10]) == ("active", 1, "2026-11-01")
    assert [row["revision"] for row in world.rows("SELECT revision FROM relation_evidence WHERE relation_id = ?", frozen)] == [1]
    deliver(world, day_packet("2026-11-10", "我：X1 票据又放回海港银行了。"))
    write(world, bank_extract).close()
    rows = {row["relation_id"]: row for row in world.rows("SELECT * FROM relations")}
    assert (rows[frozen]["state"], rows[frozen]["frozen"], rows[frozen]["as_of"][:10]) == ("active", 1, "2026-11-10")
    (ended,) = [row for one, row in rows.items() if one != frozen]
    assert (ended["state"], ended["pending_reason"]) == ("ended", "frozen_conflict")
    quotes = [row["quote"] for row in world.rows("SELECT quote FROM relation_evidence WHERE relation_id = ?", frozen)]
    assert quotes == ["X1 票据又放回海港银行了"]


def test_a_hidden_frozen_relation_is_not_brought_back_by_a_restart_after_an_end(world: World) -> None:
    """审阅第 3 条：藏着的冻结关系（证据都撤回了），后来有人说转走了、又说放回来：放回来的是结束以后的另一段，不是又说起了
    藏着的那一条，藏着的接着藏着，等主人定。主人放回来以后照冻结的算：说转走的进待确认，放回来的接到冻结的上面。"""
    frozen = frozen_bank(world)
    assert main(["withdraw", "--library", "prod", "--packet", "2026-11-01_2330_desk-bot"]) == 0
    deliver(world, day_packet("2026-11-03", "我：X1 票据从海港银行转走了。"), day_packet("2026-11-10", "我：X1 票据又放回海港银行了。"))
    write(world, bank_extract).close()
    assert world.rows("SELECT withdrawn_at FROM relations WHERE relation_id = ?", frozen)[0]["withdrawn_at"] is not None
    (review,) = world.rows("SELECT review_id, decision FROM review_queue")
    assert review["decision"] is None
    others = world.rows("SELECT state FROM relations WHERE relation_id != ? ORDER BY as_of_epoch", frozen)
    assert [row["state"] for row in others] == ["ended", "active"]
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "frozen"]) == 0
    rows = {row["relation_id"]: row for row in world.rows("SELECT * FROM relations")}
    assert (rows[frozen]["withdrawn_at"], rows[frozen]["state"], rows[frozen]["as_of"][:10]) == (None, "active", "2026-11-10")
    assert [row["pending_reason"] for one, row in rows.items() if one != frozen] == ["frozen_conflict"]


def test_the_review_list_shows_a_hidden_frozen_relation(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract).close()
    relation_id = one_relation(world)
    freeze(world.settings, "prod", [relation_id])
    assert main(["withdraw", "--library", "prod", "--packet", "2026-11-01_2330_desk-bot"]) == 0
    conn = open_ledger(world.settings.library("prod"))
    try:
        (item,) = open_items(conn, world.settings)
    finally:
        conn.close()
    assert item.kind == "evidence_withdrawn" and item.choices == ["frozen", "hidden", "drop"]
    shown = item.detail["relation"]
    assert isinstance(shown, dict)
    assert (shown["id"], shown["content"], shown["frozen"], shown["hidden"]) == (relation_id, "X1 票据 托管于 海港银行", True, True)


def test_frozen_relations_survive_a_rebuild_through_export_and_import(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整本重建：冻结的关系导出、导回。新账本里还抽得到的按编号认出来、标上冻结；新账本里抽不到了的（证据还指着
    旧版本）照旧样子补回来，证据对到新账本里那个包现在那一版的同一句上。"""
    packets = [day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-02", "我：X2 在东岸证券。")]

    def extract(window):  # type: ignore[no-untyped-def]
        found = bank_extract(window)
        from test_relcheck import BROKER, STOCK, relation

        for line in window.targets:
            if "X2" in line.text:
                found["relations"].append(relation(STOCK, "托管于", BROKER, (line.ref, line.text.rstrip("。"))))  # type: ignore[attr-defined]
        found["entities"] = [*found["entities"], STOCK, BROKER]  # type: ignore[misc]
        return found

    deliver(world, *packets)
    write(world, extract).close()
    ids = {row["relation_id"] for row in world.rows("SELECT relation_id FROM relations")}
    assert len(ids) == 2
    freeze(world.settings, "prod", sorted(ids))
    exported = export_decisions(world.settings, "prod")
    assert set(exported["relations"]) == ids  # type: ignore[arg-type]
    data = tmp_path / "rebuilt"
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    rebuilt = World(data.resolve(), Settings.from_env())
    deliver(rebuilt, packets[0])  # 第二个包这次没收：那条关系新账本里抽不到
    write(rebuilt, extract).close()
    report = import_decisions(rebuilt.settings, "prod", exported)
    assert report.frozen == 2 and report.same_id == 1 and len(report.recreated) == 1
    rows = {row["relation_id"]: row for row in rebuilt.rows("SELECT * FROM relations")}
    assert set(rows) == ids and all(row["frozen"] == 1 for row in rows.values())
    (recreated,) = report.recreated
    assert rebuilt.rows("SELECT count(*) AS n FROM relation_claims WHERE relation_id = ?", recreated)[0]["n"] == 0
    assert rebuilt.rows("SELECT count(*) AS n FROM relation_evidence WHERE relation_id = ?", recreated)[0]["n"] == 0  # 那个包没收：证据落不下
    with pytest.raises(ReviewError):  # 导过一次再导：已经有冻结标记了，不导
        import_decisions(rebuilt.settings, "prod", exported)


def test_a_hidden_frozen_relation_keeps_the_sender_masked_through_export_and_import(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审阅第 2 条：冻结关系的证据早就全撤回了（藏着、证据行删光），导出冻结标记以后才按事实撤回这封信的发件人那一句，
    整本重建再导回：补回来的这条说话人、说法出自谁不能还是发件人的原名（照导出文件里“证据都撤回了”那条待确认记的包号找）。"""
    mails = two_mails(["东岸证券、海港银行：报价 12.5 美元。", "方案明天发给你。"])
    delivered(world, mails)
    write(world, mail_relations).close()
    relation_id = one_relation(world)
    freeze(world.settings, "prod", [relation_id])
    quote = world.rows("SELECT sentence_id FROM raw_sentences WHERE text LIKE '%报价 12.5%'")[0]["sentence_id"]
    withdraw(world.settings, "prod", sentences=[quote])  # 证据全撤回：藏起来，证据行删光
    exported = export_decisions(world.settings, "prod")  # 先导出冻结标记
    sender = world.rows("SELECT fact_id FROM facts WHERE content = ?", SENDER)[0]["fact_id"]
    withdraw(world.settings, "prod", facts=[sender])  # 再按事实撤回发件人那一句
    records = tmp_path / "withdrawals.json"
    records.write_text(json.dumps(export_records(world.settings, "prod"), ensure_ascii=False), encoding="utf-8")
    data = tmp_path / "rebuilt"
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    rebuilt = World(data.resolve(), Settings.from_env())
    assert main(["withdraw", "--library", "prod", "--import", str(records)]) == 0
    delivered(rebuilt, mails)
    write(rebuilt, mail_relations).close()
    import_decisions(rebuilt.settings, "prod", exported)
    review = rebuilt.rows("SELECT review_id FROM review_queue WHERE kind = 'other' AND decided_at IS NULL")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "frozen"]) == 0
    (row,) = rebuilt.rows("SELECT speaker, attributed_to, withdrawn_at FROM relations WHERE relation_id = ?", relation_id)
    assert row["withdrawn_at"] is None and (row["speaker"], row["attributed_to"]) == (MASK, MASK)


class RelationSpot(SpotModel):
    """合并工具的假 L3 再加一条关系：写着“负责资金池”的那一行，人负责资金池（项目）。"""

    def extract(self, user: str) -> dict[str, object]:
        body = super().extract(user)
        entities = list(body["entities"])  # type: ignore[arg-type]
        relations = []
        for fact in body["facts"]:  # type: ignore[union-attr]
            if "负责资金池" not in str(fact["text"]):
                continue
            project = next((one for one in entities if one["name"] == "资金池"), None)
            if project is None:
                project = {"key": f"e{len(entities) + 1}", "mention": "资金池", "name": "资金池", "type": "项目"}
                entities.append(project)
            relations.append({
                "subject": fact["subject"], "relation": "负责", "object": project["key"], "kinship": None, "state": "生效",
                "valid_from": None, "valid_to": None, "time_precision": "unknown", "certainty": "stated", "speaker": "主人",
                "evidence": fact["evidence"],
            })
        return {**body, "entities": entities, "relations": relations}


def test_a_merge_points_the_relation_to_the_merged_entity_and_carries_its_freeze_mark(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = [("柏廷舟", [("柏廷舟", "人", "柏廷舟")]), ("柏庭舟", [("柏庭舟", "人", "柏庭舟")])]
    for day, line in (("2026-09-01", "我：柏廷舟负责资金池。"), ("2026-09-06", "我：柏庭舟还是负责资金池。")):
        world.drop("inbox/desk-bot", day_packet(day, line, domain="[investment]"))
    world.intake()
    extract_into_store(world, RelationSpot(table))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    rows = world.rows("SELECT r.relation_id, e.canonical_name FROM relations AS r JOIN entities AS e ON e.entity_id = r.subject_entity_id")
    assert {row["canonical_name"] for row in rows} == {"柏廷舟", "柏庭舟"} and len(rows) == 2  # 没配别名表：两个人各一条
    typo = next(row["relation_id"] for row in rows if row["canonical_name"] == "柏廷舟")
    assert main(["freeze", "--library", "prod", "--relation", typo]) == 0
    path = tmp_path / "config" / "lexicon.yaml"
    path.parent.mkdir()
    path.write_text(LEXICON_TEXT, encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    proposal = proposal_of(world, path, "柏庭舟")
    assert main(["entities", "--library", "prod", "--merge", proposal, "--note", "测试"]) == 0
    (row,) = world.rows("SELECT relation_id, subject_entity_id, frozen FROM relations")
    assert row["subject_entity_id"] == STANDARD and row["frozen"] == 1  # 并成一条，冻结标记跟过来
    assert world.rows("SELECT count(*) AS n FROM relation_claims WHERE relation_id = ?", row["relation_id"])[0]["n"] == 2
    assert not world.rows("SELECT 1 FROM relations WHERE relation_id = ? AND relation_id != ?", typo, row["relation_id"])
    assert json.loads(world.rows("SELECT scope_partitions FROM relations")[0]["scope_partitions"]) == ["investment"]
