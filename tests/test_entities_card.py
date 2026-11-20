"""合并工具（J3.6，D100）第二段：确认卡片和模型的看法（第 8 条：只当参考，按条记账）。卡片上有模型那一行、标着只当参考；
存过的不重调；额度不够不调、照样出卡片；送出去的文字打过码、没有 private 包的句子、没有主人的名字；模型说什么都不会自己合；
格式错两次写“模型没给出看法”；撤回以后存下的看法删掉；提示词钉住。名字和句子都是编的，从头写的。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from helpers import World
from spotter import SpotModel, extract_into_store
from test_write import day_packet

from javmem.cli import main
from javmem.entities import describe, find_proposals, quotes_of, sendable_of
from javmem.ledger import open_ledger
from javmem.llm import FakeModel, ModelReply, Usage
from javmem.pipeline.merge import entity_id_of, name_key
from javmem.redaction import default_patterns
from javmem.usage import TokenLedger
from javmem.zh.lexicon import load_lexicon
from javmem import samecheck

TABLE = [
    ("柏廷舟", [("柏廷舟", "人", "柏廷舟")]),
    ("柏松年让柏庭舟", [("柏庭舟", "人", "柏庭舟"), ("柏松年", "人", "柏松年")]),
    ("柏庭舟", [("柏庭舟", "人", "柏庭舟")]),
]
LINES = [
    ("2026-09-01", "我：柏廷舟说保险箱密码：Lk38dhq2，周五换。"),
    ("2026-09-02", "我：周五约柏庭舟对一下报价。"),
    ("2026-09-03", "我：柏松年让柏庭舟下周去看铺面。"),
]
LEXICON_TEXT = "format: javmem-lexicon/2\naliases:\n  - type: 人\n    names: [柏庭舟, 柏廷舟]\n"


@pytest.fixture
def card_world(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[World, Path]:
    owner = tmp_path / "owner.yaml"
    owner.write_text("names: [柏松年]\n", encoding="utf-8")
    monkeypatch.setenv("JAVMEM_OWNER_FILE", str(owner))
    for day, line in LINES:
        world.drop("inbox/desk-bot", day_packet(day, line, domain="[investment]"))
    world.intake()
    extract_into_store(world, SpotModel(TABLE))
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0  # 没配别名表：两个写法各建一个
    path = tmp_path / "lexicon.yaml"
    path.write_text(LEXICON_TEXT, encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(path))
    return world, path


def the_proposal(world: World, path: Path):
    conn = open_ledger(world.settings.library("prod"))
    proposal = next(item for item in find_proposals(conn, load_lexicon(path)) if item.name == "柏庭舟")
    return conn, proposal


def request_of(conn, proposal, owner_keys=frozenset({name_key("柏松年")})):
    infos = describe(conn, proposal)
    return samecheck.build_request(
        proposal.proposal_id, {entity_id: sendable_of(conn, infos[entity_id]) for entity_id in proposal.entities}, default_patterns(), owner_keys
    )


def test_the_card_has_the_reference_line_quotes_and_choices(card_world, capsys: pytest.CaptureFixture[str]) -> None:
    world, path = card_world
    conn, proposal = the_proposal(world, path)
    conn.close()
    assert main(["entities", "--library", "prod", "--show", proposal.proposal_id, "--fake"]) == 0
    out = capsys.readouterr().out
    assert f"合并提议 {proposal.proposal_id}" in out and "建议：并成「柏庭舟」（人）" in out
    assert entity_id_of("人", name_key("柏庭舟")) in out
    assert "模型的看法（只当参考，不算数）：拿不准。理由：假模型，没看原话" in out
    assert "周五约柏庭舟对一下报价" in out  # 原话给主人看
    assert "Lk38dhq2" not in out and "[已隐去:密码]" in out  # 漏打的密码不进会话
    assert "选：1 合并，标准名用「柏庭舟」　2 合并，标准名用「柏廷舟」　3 不是同一个　4 先放着" in out
    assert main(["entities", "--library", "prod", "--show", proposal.proposal_id, "--no-model"]) == 0
    assert "没调模型（这次不调）" in capsys.readouterr().out
    assert main(["entities", "--library", "prod", "--show", "P00000000", "--no-model"]) == 1


def test_what_is_sent_is_redacted_without_owner_names(card_world) -> None:
    world, path = card_world
    conn, proposal = the_proposal(world, path)
    request = request_of(conn, proposal)
    assert isinstance(request, samecheck.JudgeRequest)
    assert "Lk38dhq2" not in request.text and "[已隐去:密码]" in request.text
    assert "柏松年" not in request.text and "主人让柏庭舟" in request.text
    assert "【实体 1】" in request.text and "【实体 2】" in request.text and not request.leftover
    conn.close()


def test_a_name_seen_only_in_private_packets_is_not_sent(card_world) -> None:
    world, path = card_world
    world.execute("UPDATE packets SET privacy = 'private' WHERE packet_id = '2026-09-01_2330_desk-bot'")
    conn, proposal = the_proposal(world, path)
    infos = describe(conn, proposal)
    typo = entity_id_of("人", name_key("柏廷舟"))
    assert sendable_of(conn, infos[typo]) is None
    assert all(quote.sentence_id.startswith("2026-09-0") for quote in quotes_of(conn, typo))  # 卡片上照样给主人看
    assert quotes_of(conn, typo, sendable=True) == []
    assert request_of(conn, proposal) == "有实体的名字只在 private 的包里出现过"
    conn.close()


def test_judgments_are_cached_accounted_and_never_merge(card_world, tmp_path: Path) -> None:
    world, path = card_world
    conn, proposal = the_proposal(world, path)
    request = request_of(conn, proposal)
    ledger = TokenLedger(tmp_path / "tokens.csv")

    def yes(system: str, user: str) -> ModelReply:
        body = {"same": "yes", "reason": "两处都在约人对报价，写法只差一个字（1.1、2.1）"}
        return ModelReply(json.dumps(body, ensure_ascii=False), "stop", Usage(120, 30), "fake-model", 0.0)

    model = FakeModel([yes])
    before = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
    first = samecheck.judge(conn, request, model, ledger=ledger, budget=10_000_000)
    assert (first.status, first.same) == (samecheck.OK, "yes") and "只当参考" in first.line()
    again = samecheck.judge(conn, request, model, ledger=ledger, budget=10_000_000)
    assert again.status == samecheck.CACHED and len(model.calls) == 1  # 存过的不重调
    rows = list(csv.DictReader((tmp_path / "tokens.csv").open(encoding="utf-8")))
    assert [(row["tag"], row["purpose"], row["window_id"], row["input_tokens"]) for row in rows] == [
        ("javmem-merge", "merge_judgment", proposal.proposal_id, "120")
    ]
    assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == before  # 模型说是，也不会自己合
    assert proposal.proposal_id in {item.proposal_id for item in find_proposals(conn, load_lexicon(path))}
    system, user = model.calls[0]
    assert "只给人当参考" in system and user == request.text
    conn.close()


def test_no_call_when_the_budget_would_be_exceeded(card_world, tmp_path: Path) -> None:
    world, path = card_world
    conn, proposal = the_proposal(world, path)
    ledger = TokenLedger(tmp_path / "tokens.csv")
    ledger.append({"tag": "javmem", "purpose": "l3_extract", "input_tokens": 9_999_500, "output_tokens": 0})
    model = FakeModel()
    judgment = samecheck.judge(conn, request_of(conn, proposal), model, ledger=ledger, budget=10_000_000)
    assert judgment.status == samecheck.BUDGET and model.calls == []
    assert judgment.line() == "模型的看法（只当参考，不算数）：没调模型（额度不够）"
    conn.close()


def test_two_format_errors_give_no_judgment(card_world) -> None:
    world, path = card_world
    conn, proposal = the_proposal(world, path)
    model = FakeModel(["bad_json", "bad_json"])
    judgment = samecheck.judge(conn, request_of(conn, proposal), model, ledger=None, budget=None)
    assert judgment.status == samecheck.FAILED and len(model.calls) == 2
    assert judgment.line().startswith("模型的看法（只当参考，不算数）：模型没给出看法")
    assert conn.execute("SELECT count(*) FROM merge_judgments").fetchone()[0] == 0
    conn.close()


def test_withdrawing_a_sent_sentence_forgets_the_judgment(card_world) -> None:
    world, path = card_world
    conn, proposal = the_proposal(world, path)
    samecheck.judge(conn, request_of(conn, proposal), FakeModel(), ledger=None, budget=None)
    assert conn.execute("SELECT count(*) FROM merge_judgments").fetchone()[0] == 1
    conn.close()
    assert main(["withdraw", "--library", "prod", "--packet", "2026-09-02_2330_desk-bot"]) == 0
    assert world.rows("SELECT count(*) AS n FROM merge_judgments")[0]["n"] == 0


def test_the_judge_prompt_is_pinned() -> None:
    assert samecheck.judge_fingerprint() == samecheck.JUDGE_PROMPT_SHA256
    assert samecheck.parse_judgment('{"same": "no", "reason": "身份对不上"}') == {"same": "no", "reason": "身份对不上"}
    for bad in ('{"same": "maybe", "reason": "x"}', '{"same": "yes"}', '{"same": "yes", "reason": " "}', "不是 JSON"):
        with pytest.raises(samecheck.FormatError):
            samecheck.parse_judgment(bad)
