"""J4.4：判断 C 拿不准的关系挪进待确认（主会话 11-17：第二档拿不准 llm_unsure、回不了 llm_failed 的，原因用 manual_review，
只改 L5）。没判过的（not_judged）、有把握说不能的（local_unsupported、llm_unsupported）照旧进隔离表。例子都是编的；判断 C
的结论直接写在 RelationCheck 上（和 reljudge.apply_judgments 填的一样的样子），不调模型。"""

from __future__ import annotations

import json
from dataclasses import replace

from helpers import World
from test_relations_write import bank_extract
from test_write import checked_items, day_packet, deliver, write

from javmem.cli import main
from javmem.ledger import open_ledger
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.verify import Checked, Finding
from javmem.pipeline.write import LedgerWriter
from javmem.review import remind


def judged(code: str):  # type: ignore[no-untyped-def]
    """把过了 R1 到 R6 的关系都记成判断 C 判的 code（apply_judgments 那样：加一条 C 的原因、记上判的经过）。"""

    def order(items: list[Checked]) -> list[Checked]:
        out = []
        for item in items:
            if item.check is None:
                out.append(item)
                continue
            relations = [replace(one, failures=[*one.failures, Finding("C", code)], judge={"by": "llm", "verdict": "unsure"})
                         if one.passed else one for one in item.check.relations]
            out.append(replace(item, check=replace(item.check, relations=relations)))
        return out

    return order


def test_a_relation_judge_c_is_unsure_about_waits_for_the_owner(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract, order=judged("llm_unsure")).close()
    (row,) = world.rows("SELECT relation_id, status, pending_reason, state FROM relations")
    assert (row["status"], row["pending_reason"], row["state"]) == ("pending", "manual_review", "active")
    assert world.rows("SELECT count(*) AS n FROM quarantine WHERE item_kind = 'relation'")[0]["n"] == 0  # 挪出来了
    (review,) = world.rows("SELECT review_id, kind, claims, decided_at FROM review_queue")
    claims = json.loads(review["claims"])
    assert (review["kind"], claims["relation"], claims["reason"], review["decided_at"]) == (
        "manual_review", row["relation_id"], "C:llm_unsure", None)
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0  # 主人说留
    (row,) = world.rows("SELECT status, pending_reason FROM relations")
    assert (row["status"], row["pending_reason"]) == ("verified", None)


def test_dropping_an_unsure_relation_sends_it_back_to_quarantine(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    items = judged("llm_failed")(checked_items(world, bank_extract))
    write(world, bank_extract, order=judged("llm_failed")).close()
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--drop"]) == 0  # 主人说不要
    assert world.rows("SELECT count(*) AS n FROM relations")[0]["n"] == 0
    conn = open_ledger(world.settings.library("prod"))
    writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")
    with VectorCache(world.data / "vectors") as cache:  # 窗口重写（重抽、换了核的方式）：定过不要的照旧进隔离表，不再进待确认
        for item in items:
            writer.write_window(item, cache.get("fake-embedding", writer.texts_needed(item)))
    conn.close()
    assert world.rows("SELECT count(*) AS n FROM relations")[0]["n"] == 0
    (held,) = world.rows("SELECT reason, rule_id FROM quarantine WHERE item_kind = 'relation'")
    assert (held["reason"], held["rule_id"]) == ("C:llm_failed", "C")
    assert [one["decision"] for one in world.rows("SELECT decision FROM review_queue")] == ["drop"]


def test_relations_judge_c_did_not_judge_or_rejected_stay_in_quarantine(world: World) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"), day_packet("2026-11-02", "我：X1 票据还在海港银行。"))

    def mixed(items: list[Checked]) -> list[Checked]:  # 一个窗口没判过（合并工具重写时那样），一个本机有把握说不能
        first, second = items
        return [*judged("not_judged")([first]), *judged("local_unsupported")([second])]

    write(world, bank_extract, order=mixed).close()
    assert world.rows("SELECT count(*) AS n FROM relations")[0]["n"] == 0
    reasons = sorted(row["reason"] for row in world.rows("SELECT reason FROM quarantine WHERE item_kind = 'relation'"))
    assert reasons == ["C:local_unsupported", "C:not_judged"]
    assert world.rows("SELECT count(*) AS n FROM review_queue")[0]["n"] == 0


def test_an_unsure_relation_left_alone_for_30_days_stays_pending(world: World) -> None:
    """30 天没人管：接着搁着（不现行、不再提醒，D125：没把握的不进），不自动留下也不删。"""
    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract, order=judged("llm_unsure")).close()
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    world.execute("UPDATE review_queue SET created_at = '2026-01-01T00:00:00+08:00'")
    assert remind(world.settings, "prod", force=True).expired == [review]
    (row,) = world.rows("SELECT status, pending_reason FROM relations")
    assert (row["status"], row["pending_reason"]) == ("pending", "manual_review")
    assert world.rows("SELECT decision FROM review_queue")[0]["decision"] == "keep_hidden"


def test_scoring_skips_relations_judge_c_was_unsure_about(world: World) -> None:
    """打分照判断 C：拿不准的不算（以前在隔离表里，也不算）；主人说留了才算。"""
    from javmem.scoring.results import side_from_result

    deliver(world, day_packet("2026-11-01", "我：X1 票据在海港银行。"))
    write(world, bank_extract, order=judged("llm_unsure")).close()
    side, _ = side_from_result(world.data, ["2026-11-01_2330_desk-bot"], library="prod")
    assert side.relations == {}
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "new"]) == 0
    side, _ = side_from_result(world.data, ["2026-11-01_2330_desk-bot"], library="prod")
    assert len(side.relations) == 1
