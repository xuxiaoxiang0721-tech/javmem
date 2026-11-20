"""J5.3 进图前检查（javmem.pipeline.precheck，D178、D22）：设置、事实问什么、两档怎么判、财务投资判不了的转人工、写账（挡下的进
隔离表、转人工的写成待确认、不参加新旧、谁认的）、主人在待确认里定、留痕和 javmem audit、进图的口子、账本视图、打分、命令行。
例子都是编的；写账那几条把判的结论直接写在 FactCheck 上（和 precheck.apply 填的一样的样子），不调模型。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import World, build_mail_packet, build_packet, file_name
from test_attachments import FILE_PACKET
from test_graph import assert_matches_ledger, connect_graph, run_sync
from test_reljudge import FAKE_LLM, OPTIONS, llm_answer, scripted, two_relations
from test_relcheck import BANK, NOTE, relation
from test_window import mail_section
from test_write import day_packet, deliver, facts, l3_fact, ref_of, write

import javmem.cli as cli
from javmem.cli import main
from javmem.config import ConfigError
from javmem.graph.gate import AdmitAll, GateItem, Verdict
from javmem.ledger import open_ledger
from javmem.llm import FakeModel
from javmem.packet import parse_packet
from javmem.pipeline import precheck, reljudge
from javmem.pipeline.extract import map_output
from javmem.pipeline.normalize import normalize
from javmem.pipeline.precheck import FACT_PROMPT_SHA256, LOCAL_NO_ANSWER, PrecheckGate, PrecheckSettings
from javmem.pipeline.reljudge import CASCADE, FAKE_LOCAL_NAME, LOCAL, NO_JUDGE, FakeLocal, JudgeSpec
from javmem.pipeline.render import render_window
from javmem.pipeline.store import ResultStore
from javmem.pipeline.verify import L4_VERSION, Checked, FactCheck, Finding, verify_window
from javmem.pipeline.window import Window, WindowConfig, build_windows
from javmem.redaction import default_patterns
from javmem.review import remind

PATTERNS = default_patterns()
LOCAL_FACTS = JudgeSpec(LOCAL, FAKE_LOCAL_NAME, 0.1, version="f1")
CASCADE_FACTS = JudgeSpec(CASCADE, FAKE_LOCAL_NAME, 0.2, 0.8, FAKE_LLM, version="f1")
STRICT = frozenset({"finance", "investment"})


def item_of(lines: tuple[str, ...], found: list[dict[str, object]], *, domain: str = "[business]", relations=(),
            entities=(), key: str = "l3-key") -> Checked:
    """一个对话包切出来的窗口，L3 抽出 found 这几条事实（和 relations 这几条关系），过一遍 L4。"""
    body = "## 原话 · 2026-11-08 10:30–10:40\n" + "".join(f"- [10:{30 + index:02d}] {line}\n" for index, line in enumerate(lines))
    raw = build_packet(body=body, domain=domain)
    packet = normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))
    window = build_windows([packet])[0]
    rendered = render_window(window, PATTERNS)
    extraction = map_output({"entities": list(entities), "facts": found, "relations": list(relations)}, window,
                            rendered.redacted_refs)
    return Checked(window, rendered, key, extraction, verify_window(window, rendered, extraction))


def fact_of(window: Window, phrase: str, text: str, **fields: object) -> dict[str, object]:
    return l3_fact(window, phrase, text, **fields)


def two_facts(domain: str = "[business]") -> Checked:
    """两条过了规则的事实：X1 票据的面值 50 万、X1 票据下周到期。"""
    lines = ("我：X1 票据面值 50 万。", "desk-bot：X1 下周到期。")
    probe = item_of(lines, [], domain=domain)
    window = probe.window
    return item_of(lines, [
        fact_of(window, "X1 票据面值 50 万", "X1 票据面值 50 万", attribute="面值", value="50 万"),
        fact_of(window, "X1 下周到期", "X1 票据下周到期", certainty="vague"),
    ], domain=domain)


@pytest.fixture
def store(tmp_path: Path) -> ResultStore:
    with ResultStore(tmp_path / "l3") as opened:
        yield opened


def by_fact(table: dict[str, object], default: object = 0.95) -> Callable[[str, str], object]:
    """假的本机模型：按问的是哪条事实（【事实】那一行里有没有这个词）回。"""

    def answer(system: str, user: str) -> object:
        first = user.split("\n", 1)[0]
        return next((value for word, value in table.items() if word in first), default)

    return answer


def codes(item: Checked) -> list[list[str]]:
    assert item.check is not None
    return [[finding.as_text() for finding in fact.failures] for fact in item.check.facts]


# ---------- 设置 ----------


def test_off_unless_on_and_on_needs_judge_c() -> None:
    assert PrecheckSettings.from_env({}) is None
    assert PrecheckSettings.from_env({"JAVMEM_PRECHECK": "off"}) is None
    with pytest.raises(ConfigError):
        PrecheckSettings.from_env({"JAVMEM_PRECHECK": "yes"})
    with pytest.raises(ConfigError):  # 开了却没配判断 C
        PrecheckSettings.from_env({"JAVMEM_PRECHECK": "on"})
    local = PrecheckSettings.from_env({"JAVMEM_PRECHECK": "on", "JAVMEM_JUDGE_MODE": "local"})
    assert local is not None and (local.low, local.high, local.strict) == (0.1, None, STRICT)
    cascade = PrecheckSettings.from_env({"JAVMEM_PRECHECK": " ON ", "JAVMEM_JUDGE_MODE": "local+llm", "JAVMEM_JUDGE_LOW": "0.3",
                                         "JAVMEM_PRECHECK_HIGH": "0.9", "JAVMEM_STRICT_PARTITIONS": "finance"})
    assert cascade is not None and (cascade.low, cascade.high, cascade.strict) == (0.3, 0.9, frozenset({"finance"}))
    spec = cascade.spec(FAKE_LLM)
    assert (spec.version, spec.token()) == ("f1", f"f1,local+llm,qwen3.8-27b,0.3,0.9,{FAKE_LLM}")
    for bad in ({"JAVMEM_PRECHECK_HIGH": "0.8"}, {"JAVMEM_PRECHECK_LOW": "1"}):  # luna 线只有一条线
        with pytest.raises(ConfigError):
            PrecheckSettings.from_env({"JAVMEM_PRECHECK": "on", "JAVMEM_JUDGE_MODE": "local", **bad})
    with pytest.raises(ConfigError):
        PrecheckSettings.from_env({"JAVMEM_PRECHECK": "on", "JAVMEM_JUDGE_MODE": "local+llm", "JAVMEM_PRECHECK_LOW": "0.9",
                                   "JAVMEM_PRECHECK_HIGH": "0.5"})
    assert precheck.enabled({"JAVMEM_PRECHECK": "On"}) and not precheck.enabled({})


def test_checked_by_carries_the_fact_spec_and_splits_back() -> None:
    judge = JudgeSpec(LOCAL, FAKE_LOCAL_NAME, 0.1)
    text = precheck.checked_by(None, judge, LOCAL_FACTS)
    assert text == f"{L4_VERSION} c1,local,{FAKE_LOCAL_NAME},0.1 f1,local,{FAKE_LOCAL_NAME},0.1"
    assert precheck.split_checked_by(text) == (L4_VERSION, judge, LOCAL_FACTS)
    assert precheck.split_checked_by(precheck.checked_by(None, judge, None)) == (L4_VERSION, judge, None)
    assert precheck.split_checked_by(L4_VERSION) == (L4_VERSION, None, None)
    cascade = precheck.checked_by(None, JudgeSpec(CASCADE, FAKE_LOCAL_NAME, 0.2, 0.8, FAKE_LLM), CASCADE_FACTS)
    assert precheck.split_checked_by(cascade)[2] == CASCADE_FACTS
    with pytest.raises(ValueError):
        precheck.split_checked_by(f"{L4_VERSION} c1,local,{FAKE_LOCAL_NAME},0.1 f9,local,{FAKE_LOCAL_NAME},0.1")


def test_prompt_is_pinned_and_differs_from_judge_c() -> None:
    assert precheck.fact_fingerprint() == FACT_PROMPT_SHA256
    assert precheck.load_fact_prompt() != reljudge.load_judge_prompt()
    assert "事实" in precheck.LOCAL_ASK and "是 或 否" in precheck.LOCAL_ASK


# ---------- 问什么 ----------


def test_the_request_has_the_fact_its_fields_certainty_and_the_evidence() -> None:
    item = two_facts()
    (_, first, request), (_, second, other) = precheck.fact_requests([item], PATTERNS)
    head, said = request.text.split("\n【原话】\n", 1)
    assert head == "【事实】X1 票据面值 50 万 〔属性〕面值 〔值〕50 万（说定了）"
    assert other.text.startswith("【事实】X1 票据下周到期（含糊）\n")
    assert "★" in said and request.prompt_version == "f1" and request.kind == "fact"
    assert request.key("local", FAKE_LOCAL_NAME) != reljudge.judge_key("local", FAKE_LOCAL_NAME, request.text)  # 和 c1 的键分得开
    assert request.basis == tuple((proof.sentence_id, proof.packet_id, proof.revision) for proof in first.proofs)
    assert set(request.context) >= {sentence for sentence, _, _ in request.basis}


def test_markup_in_the_fact_is_escaped_and_only_passed_facts_are_asked() -> None:
    lines = ("我：X1 票据面值 50 万。",)
    window = item_of(lines, []).window
    item = item_of(lines, [
        fact_of(window, "X1 票据面值 50 万", "X1 票据〔面值〕50 万"),
        fact_of(window, "X1 票据面值 50 万", "X1 票据面值 90 万"),  # 数字对不上：G2 拦下，不问
    ])
    requests = precheck.fact_requests([item], PATTERNS)
    assert len(requests) == 1
    line = requests[0][2].text.split("\n", 1)[0]
    assert line == "【事实】X1 票据［面值］50 万（说定了）"


# ---------- 两档怎么判 ----------


def test_local_line_blocks_low_and_keeps_the_rest(store: ResultStore) -> None:
    item = two_facts()
    local = FakeLocal([by_fact({"面值": 0.05, "到期": 0.5})] * 2)
    result = precheck.run_fact_judge([item], LOCAL_FACTS, store, local_model=local, patterns=PATTERNS, options=OPTIONS)
    assert result.relations == 2 and result.local.asked == 2
    assert all(system == precheck.load_fact_prompt() for system, _ in local.calls)
    (applied,) = precheck.apply([item], store, LOCAL_FACTS, PATTERNS, strict=STRICT)
    assert codes(applied) == [["C:local_unsupported"], []]
    kept = applied.check.facts[1]  # type: ignore[union-attr]
    assert kept.judge is not None and kept.judge["by"] == "local" and precheck.judged_tier(kept.judge) == "local"
    assert applied.check.facts[0].first_rule == "C"  # type: ignore[union-attr]
    links = store.fact_judgment_links()
    assert len(links) == 2 and {row["fact"] for row in links} == {0, 1} and store.judgment_links() == []
    again = FakeLocal()
    precheck.run_fact_judge([item], LOCAL_FACTS, store, local_model=again, patterns=PATTERNS, options=OPTIONS)
    assert again.calls == []  # 存过的不再问


def test_luna_line_finance_without_a_local_answer_goes_to_the_owner(tmp_path: Path) -> None:
    """D22（规矩题 1 甲）：luna 线本机没答上来（回的不是“是 / 否”、说太长）的，财务投资的转人工，别的照 C 的 luna 线留下。"""
    hold = [f"C:{LOCAL_NO_ANSWER}"]
    for domain, expected in (("[investment]", [hold, hold]), ("[business]", [[], []])):
        item = two_facts(domain)
        local = FakeLocal([by_fact({"面值": "other", "到期": "http400"})] * 2)  # 一条回的不是“是 / 否”，一条说太长
        with ResultStore(tmp_path / domain.strip("[]")) as own:
            precheck.run_fact_judge([item], LOCAL_FACTS, own, local_model=local, patterns=PATTERNS, options=OPTIONS)
            (applied,) = precheck.apply([item], own, LOCAL_FACTS, PATTERNS, strict=STRICT)
        assert codes(applied) == expected
        if domain == "[investment]":
            assert precheck.fact_hold(applied.check.facts[0]) == LOCAL_NO_ANSWER  # type: ignore[union-attr]


def test_luna_line_finance_relation_without_a_local_answer_goes_to_the_owner(store: ResultStore) -> None:
    lines = ("我：X1 票据放在海港银行。",)
    item = item_of(lines, [], domain="[investment]", relations=[relation(NOTE, "托管于", BANK, ("T1", "X1 票据放在海港银行"))],
                   entities=[NOTE, BANK])
    spec = JudgeSpec(LOCAL, FAKE_LOCAL_NAME, 0.1)
    reljudge.run_judge([item], spec, store, local_model=FakeLocal(["other"]), patterns=PATTERNS, options=OPTIONS)
    (c_only,) = reljudge.apply_judgments([item], store, spec, PATTERNS)
    assert c_only.check.relations[0].passed  # type: ignore[union-attr]  # 判断 C 照 luna 线留下
    (applied,) = precheck.apply([c_only], store, LOCAL_FACTS, PATTERNS, strict=STRICT)
    assert [finding.as_text() for finding in applied.check.relations[0].failures] == [f"C:{LOCAL_NO_ANSWER}"]  # type: ignore[union-attr]
    assert precheck.summarize([applied])["relations_held"] == 1


def test_sol_line_asks_the_second_tier_for_the_middle(store: ResultStore) -> None:
    lines = ("我：X1 票据面值 50 万。", "desk-bot：X1 下周到期。", "我：X2 也是 50 万。")
    window = item_of(lines, []).window
    item = item_of(lines, [
        fact_of(window, "X1 票据面值 50 万", "X1 票据面值 50 万"),
        fact_of(window, "X1 下周到期", "X1 票据下周到期"),
        fact_of(window, "X2 也是 50 万", "X2 票据面值 50 万"),
    ])
    local = FakeLocal([by_fact({"X1 票据面值": 0.9, "到期": 0.5, "X2": 0.4})] * 3)
    llm = FakeModel([llm_answer({"到期": "能", "X2": "拿不准"})] * 2)
    result = precheck.run_fact_judge([item], CASCADE_FACTS, store, local_model=local, llm_model=llm, patterns=PATTERNS,
                                     options=OPTIONS)
    assert result.llm is not None and len(llm.calls) == 2
    assert all(system.startswith(precheck.load_fact_prompt()) for system, _ in llm.calls)
    (applied,) = precheck.apply([item], store, CASCADE_FACTS, PATTERNS, strict=STRICT)
    assert codes(applied) == [[], [], ["C:llm_unsure"]]
    assert [precheck.judged_tier(fact.judge) for fact in applied.check.facts[:2]] == ["local", "llm"]  # type: ignore[union-attr]
    summary = precheck.summarize([applied])
    assert (summary["kept"], summary["held"], summary["held_by_code"]) == (2, 1, {"llm_unsure": 1})


def test_unjudged_facts_hold_the_window_or_block_when_merging(store: ResultStore) -> None:
    item = two_facts()
    (waiting,) = precheck.apply([item], store, LOCAL_FACTS, PATTERNS, strict=STRICT)
    assert waiting.check is None and waiting.skipped == NO_JUDGE
    (blocked,) = precheck.apply([item], store, LOCAL_FACTS, PATTERNS, strict=STRICT, unjudged=precheck.BLOCK)
    assert codes(blocked) == [["C:not_judged"], ["C:not_judged"]]


def test_plan_counts_without_calling(store: ResultStore) -> None:
    item = two_facts()
    plan = precheck.plan_fact_judge([item], store, CASCADE_FACTS, PATTERNS)
    assert (plan["facts"], plan["local_to_call"], plan["llm_unknown"]) == (2, 2, 2)


# ---------- 写账 ----------


def ship(window: Window) -> dict[str, object]:
    found = []
    for line in window.targets:
        for value in ("11-15", "11-20"):
            if value in line.text:
                found.append(l3_fact(window, line.text.rstrip("。"), f"越南那批订单 {value} 出货",
                                     subject="e1", attribute="出货日期", value=value))
    return {"entities": [{"key": "e1", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}], "facts": found}


def judged(code_of: Callable[[FactCheck], str | None], by: str = "local"):  # type: ignore[no-untyped-def]
    """把过了规则的事实都记成进图前检查判的（precheck.apply 那样）：code_of 给原因（None 是留下），判的经过一档本机。"""

    def order(items: list[Checked]) -> list[Checked]:
        out = []
        for item in items:
            if item.check is None:
                out.append(item)
                continue
            found = []
            for fact in item.check.facts:
                if not fact.passed:
                    found.append(fact)
                    continue
                code = code_of(fact)
                info = {"version": "f1", "mode": "local", "by": None if code else by, "verdict": "supported",
                        "steps": [{"tier": "local", "model": FAKE_LOCAL_NAME, "p": 0.5, "answer": "是", "verdict": "supported"}],
                        "context": [proof.sentence_id for proof in fact.proofs]}
                found.append(replace(fact, failures=[Finding("C", code)] if code else [], judge=info))
            out.append(replace(item, check=replace(item.check, facts=found)))
        return out

    return order


def value_code(table: dict[str, str]) -> Callable[[FactCheck], str | None]:
    return lambda fact: table.get(str(fact.raw.get("value")))


def test_a_held_fact_waits_for_the_owner_and_the_trail_shows_each_step(world: World, monkeypatch: pytest.MonkeyPatch,
                                                                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship, order=judged(value_code({"11-15": "llm_unsure"}))).close()
    (row,) = world.rows("SELECT fact_id, status, pending_reason, judged_by FROM facts")
    assert (row["status"], row["pending_reason"], row["judged_by"]) == ("pending", "manual_review", None)
    (claim,) = world.rows("SELECT claim_id, judge_hold, judged_by FROM fact_claims")
    assert (claim["judge_hold"], claim["judged_by"]) == ("llm_unsure", None)
    assert world.rows("SELECT count(*) AS n FROM quarantine WHERE item_kind = 'fact'")[0]["n"] == 0
    (review,) = world.rows("SELECT review_id, kind, claims FROM review_queue")
    claims = json.loads(review["claims"])
    assert (review["kind"], claims["item"], claims["fact"], claims["claim"], claims["reason"]) == (
        "manual_review", "fact", row["fact_id"], claim["claim_id"], "C:llm_unsure")
    kinds = sorted((one["check_kind"], one["tier"], one["trusted"]) for one in world.rows("SELECT * FROM audit_log"))
    assert kinds == [("fact_judge", "judge", "supported"), ("precheck", "rule", "hold")]
    assert main(["review", "--library", "prod"]) == 0
    assert "转人工的那一处：越南那批订单 11-15 出货" in capsys.readouterr().out
    assert main(["review", "--library", "prod", "--decide", str(review["review_id"]), "--keep", "new"]) == 0  # 主人说留下
    (row,) = world.rows("SELECT status, pending_reason, judged_by FROM facts")
    assert (row["status"], row["pending_reason"], row["judged_by"]) == ("verified", None, "owner")
    owner = world.rows("SELECT trusted, judge FROM audit_log WHERE tier = 'owner'")
    assert [(one["trusted"], one["judge"]) for one in owner] == [("keep", "owner")]
    capsys.readouterr()
    assert main(["audit", "--library", "prod", "--item", claim["claim_id"]]) == 0
    out = capsys.readouterr().out
    assert "事实的判断：判断模型（本机）" in out and "进图前检查：规则" in out and "进图前检查：主人" in out


def test_a_held_newer_value_does_not_replace_the_current_one(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    conn = write(world, ship, order=judged(value_code({"11-20": "llm_failed"})))
    old, new = facts(conn)
    conn.close()
    assert (old["value"], old["superseded_by"], old["status"], old["judged_by"]) == ("11-15", None, "verified", "local")
    assert (new["value"], new["status"], new["pending_reason"]) == ("11-20", "pending", "manual_review")
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "new"]) == 0
    old, new = facts(open_ledger(world.settings.library("prod")))
    assert old["superseded_by"] == new["fact_id"] and new["status"] == "verified"


def test_dropping_a_held_fact_sends_it_to_quarantine_on_rewrite(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    order = judged(value_code({"11-15": "llm_unsure"}))
    write(world, ship, order=order).close()
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--drop"]) == 0
    assert world.rows("SELECT count(*) AS n FROM facts")[0]["n"] == 0
    world.execute("UPDATE packets SET extraction = 'queued'")  # 重抽、换了核的方式：窗口重写
    write(world, ship, order=order).close()
    assert world.rows("SELECT count(*) AS n FROM facts")[0]["n"] == 0
    (held,) = world.rows("SELECT reason, rule_id, payload FROM quarantine WHERE item_kind = 'fact'")
    assert (held["reason"], held["rule_id"]) == ("C:llm_unsure", "C")
    assert json.loads(held["payload"])["judge"]["version"] == "f1"
    assert [one["decision"] for one in world.rows("SELECT decision FROM review_queue")] == ["drop"]
    assert world.rows("SELECT trusted FROM audit_log WHERE tier = 'owner'")[0]["trusted"] == "drop"


def test_a_held_fact_left_alone_for_30_days_stays_held(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship, order=judged(value_code({"11-15": LOCAL_NO_ANSWER}))).close()
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    world.execute("UPDATE review_queue SET created_at = '2026-01-01T00:00:00+08:00'")
    assert remind(world.settings, "prod", force=True).expired == [review]
    (row,) = world.rows("SELECT status, pending_reason FROM facts")
    assert (row["status"], row["pending_reason"]) == ("pending", "manual_review")
    (trail,) = world.rows("SELECT tier, judge, trusted FROM audit_log WHERE judge = '30 天没人管'")
    assert (trail["tier"], trail["trusted"]) == ("rule", "hidden")
    assert world.rows("SELECT judge FROM audit_log WHERE trusted = 'hold'")[0]["judge"] == "D22 财务投资"


def test_the_owners_decision_on_a_held_fact_survives_a_rebuild(world: World, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """整本重建：导出待确认（javmem review --export），新账本照 J5.3 重写，再导回：主人定过“留下”的照搬，不再挡住。"""
    from javmem.config import Settings
    from javmem.decisions import export_decisions, import_decisions

    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    packet = day_packet("2026-11-01", "我：越南那批订单 11-15 出货。")
    order = judged(value_code({"11-15": "llm_unsure"}))
    deliver(world, packet)
    write(world, ship, order=order).close()
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "new"]) == 0
    exported = export_decisions(world.settings, "prod")
    data = tmp_path / "rebuilt"
    data.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    rebuilt = World(data.resolve(), Settings.from_env())
    deliver(rebuilt, packet)
    write(rebuilt, ship, order=order).close()
    assert rebuilt.rows("SELECT status FROM facts")[0]["status"] == "pending"  # 新账本写账时又挡住了
    import_decisions(rebuilt.settings, "prod", exported)
    (row,) = rebuilt.rows("SELECT status, pending_reason, judged_by FROM facts")
    assert (row["status"], row["pending_reason"], row["judged_by"]) == ("verified", None, "owner")
    assert [one["decision"] for one in rebuilt.rows("SELECT decision FROM review_queue")] == ["keep_new"]


def test_withdrawing_the_held_sentence_clears_the_fact_and_its_open_review(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.withdraw import withdraw

    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship, order=judged(value_code({"11-15": "llm_unsure"}))).close()
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE decided_at IS NULL")[0]["n"] == 1
    sentence = json.loads(world.rows("SELECT claims FROM review_queue")[0]["claims"])["sentence"]
    withdraw(world.settings, "prod", sentences=[sentence])
    assert world.rows("SELECT count(*) AS n FROM facts")[0]["n"] == 0
    assert world.rows("SELECT count(*) AS n FROM review_queue WHERE decided_at IS NULL")[0]["n"] == 0


def test_the_merging_tool_reads_the_fact_spec_back() -> None:
    import sqlite3

    from javmem.merging import MergeError, _written_by

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE fact_claims (window_id TEXT, extracted_by TEXT, l4_version TEXT)")
    judge = JudgeSpec(CASCADE, FAKE_LOCAL_NAME, 0.2, 0.8, FAKE_LLM)
    conn.execute("INSERT INTO fact_claims VALUES ('w1', ?, ?)", (f"l3-v17 {FAKE_LLM}", precheck.checked_by(None, judge, CASCADE_FACTS)))
    written = _written_by(conn, {"w1"})
    assert (written.judge, written.facts, written.recheck) == (judge, CASCADE_FACTS, None)
    conn.execute("UPDATE fact_claims SET l4_version = ?", (precheck.checked_by(None, judge, None) + " f9,local,x,0.1",))
    with pytest.raises(MergeError, match="进图前检查"):
        _written_by(conn, {"w1"})


def test_a_blocked_fact_is_quarantined_with_its_judgment(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship, order=judged(value_code({"11-15": "local_unsupported"}))).close()
    assert world.rows("SELECT count(*) AS n FROM facts")[0]["n"] == 0
    (row,) = world.rows("SELECT reason, rule_id, payload FROM quarantine WHERE item_kind = 'fact'")
    payload = json.loads(row["payload"])
    assert (row["reason"], row["rule_id"]) == ("C:local_unsupported", "C")
    assert payload["judge"]["context"] and payload["proofs"][0]["sentence_id"] in payload["judge"]["context"]
    assert world.rows("SELECT count(*) AS n FROM review_queue")[0]["n"] == 0


def test_frozen_facts_count_as_the_owners(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship, order=judged(lambda fact: None, by="llm")).close()
    fact_id = world.rows("SELECT fact_id, judged_by FROM facts")[0]
    assert fact_id["judged_by"] == "llm"
    assert main(["freeze", "--library", "prod", "--fact", fact_id["fact_id"]]) == 0
    assert world.rows("SELECT judged_by FROM facts")[0]["judged_by"] == "owner"
    assert main(["freeze", "--library", "prod", "--fact", fact_id["fact_id"], "--undo"]) == 0
    assert world.rows("SELECT judged_by FROM facts")[0]["judged_by"] == "llm"


def test_off_writes_nothing_new(world: World) -> None:
    """没开（JAVMEM_PRECHECK 不配）：说法、事实上谁认的都是空的，没有留痕、待确认（和以前一字不差）。"""
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship).close()
    assert [tuple(row) for row in world.rows("SELECT judge_hold, judged_by FROM fact_claims")] == [(None, None)]
    assert world.rows("SELECT judged_by FROM facts")[0]["judged_by"] is None
    assert world.rows("SELECT count(*) AS n FROM audit_log")[0]["n"] == 0


# ---------- 进图的口子、账本视图、打分 ----------


def test_the_gate_admits_only_what_someone_checked(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"), day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    conn = write(world, ship, order=judged(lambda fact: None))
    ids = [row["fact_id"] for row in facts(conn)]
    conn.execute("UPDATE facts SET judged_by = NULL WHERE fact_id = ?", (ids[0],))  # 没检查过的（开 J5.3 以前写的）
    items = [GateItem("fact", one, ("business",), (), {}, "x") for one in ids] + [GateItem("relation", "R0", (), (), {}, "y")]
    result = PrecheckGate().admit(conn, "prod", items)
    conn.close()
    assert result.verdicts[("fact", ids[0])] is Verdict.HOLD and result.reasons[("fact", ids[0])] == precheck.UNCHECKED
    assert result.verdicts[("fact", ids[1])] is Verdict.ADMIT
    assert result.verdicts[("relation", "R0")] is Verdict.HOLD  # 账本里没有的不放
    assert isinstance(precheck.gate({}), AdmitAll) and isinstance(precheck.gate({"JAVMEM_PRECHECK": "on"}), PrecheckGate)


def test_sync_leaves_held_and_unchecked_facts_out_and_the_owner_brings_them_in(world: World,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship).close()  # 没开 J5.3 写的：没检查过
    graph = connect_graph(world)
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    report = run_sync(world, graph, gate=precheck.gate())
    assert list(graph.iter_items("fact")) == [] and report.gate.held == {precheck.UNCHECKED: 1}
    assert "挡了 1 条（unchecked 1）" in report.text()
    deliver(world, day_packet("2026-11-03", "我：越南那批订单改到 11-20 出货。"))
    world.execute("UPDATE packets SET extraction = 'queued'")
    world.execute("UPDATE fact_claims SET l4_version = 'l4-v6'")  # 以前的核的方式写的：窗口重写（整本照 J5.3 重写）
    write(world, ship, order=judged(value_code({"11-20": "llm_unsure"}))).close()
    run_sync(world, graph, gate=precheck.gate(), mode="all")
    assert [item["props"]["value"] for item in graph.iter_items("fact")] == ["11-15"]  # 转人工的不进图
    assert_matches_ledger(world, graph, gate=precheck.gate())
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "new"]) == 0
    run_sync(world, graph, gate=precheck.gate())
    assert sorted(item["props"]["value"] for item in graph.iter_items("fact")) == ["11-15", "11-20"]
    assert_matches_ledger(world, graph, gate=precheck.gate())


def test_the_ledger_view_uses_the_same_condition(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.query.view import LedgerView

    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship).close()  # 没检查过
    conn = open_ledger(world.settings.library("prod"))
    try:
        def shown() -> int:
            from javmem.access import Identity
            identity = Identity("owner", "prod", None, None)
            view = LedgerView(conn, identity, world.settings, as_of=world.settings.now())
            conditions, params = view.fact_conditions("f")
            return conn.execute(f"SELECT count(*) FROM facts AS f WHERE {' AND '.join(conditions)}", params).fetchone()[0]

        assert shown() == 1
        monkeypatch.setenv("JAVMEM_PRECHECK", "on")
        assert shown() == 0  # 开了：没检查过的不给
        conn.execute("UPDATE facts SET status = 'pending', pending_reason = 'manual_review'")
        assert shown() == 1  # 待确认的照接口文档 4.3 给（标着还没定）
    finally:
        conn.close()


def test_scoring_skips_held_facts_until_the_owner_keeps_them(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.scoring.results import side_from_result

    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    deliver(world, day_packet("2026-11-01", "我：越南那批订单 11-15 出货。"))
    write(world, ship, order=judged(value_code({"11-15": "llm_unsure"}))).close()
    side, _ = side_from_result(world.data, ["2026-11-01_2330_desk-bot"], library="prod")
    assert side.facts == {}
    review = world.rows("SELECT review_id FROM review_queue")[0]["review_id"]
    assert main(["review", "--library", "prod", "--decide", str(review), "--keep", "new"]) == 0
    side, _ = side_from_result(world.data, ["2026-11-01_2330_desk-bot"], library="prod")
    assert len(side.facts) == 1


# ---------- 命令行 ----------


def one_fact(user: str) -> list[dict[str, object]]:
    ref = next(line.split(" ", 1)[0] for line in user.splitlines() if line[:1] == "T" and "海港银行" in line)
    return [{"text": "X1 票据放在海港银行", "subject": None, "attribute": None, "value": None, "occurred_at": None,
             "time_precision": "unknown", "certainty": "stated", "speaker": "", "entities": [],
             "evidence": [{"ref": ref, "quote": "X1 票据放在海港银行"}]}]


def test_cli_extract_judges_facts_after_c_and_write_follows(world: World, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据放在海港银行。", "desk-bot：X2 也在东岸证券。"))
    monkeypatch.setenv("JAVMEM_JUDGE_MODE", "local")
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")

    def respond(system, user):  # type: ignore[no-untyped-def]
        reply = scripted(two_relations).script[0](system, user)  # type: ignore[operator]
        body = json.loads(reply.content)
        body["facts"] = one_fact(user)
        return replace(reply, content=json.dumps(body, ensure_ascii=False))

    monkeypatch.setattr(cli, "FakeModel", lambda: FakeModel([respond] * 5))
    local = FakeLocal([0.95, 0.95, 0.02])  # 两条关系留下，那条事实挡下
    monkeypatch.setattr(cli.reljudge, "FakeLocal", lambda: local)
    assert main(["extract", "--library", "prod", "--fake", "--dry-run"]) == 0
    assert f"事实的判断（f1,local,{FAKE_LOCAL_NAME},0.1）" in capsys.readouterr().out
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    out = capsys.readouterr().out
    assert "事实的判断这次：要判的事实 1 条" in out and "挡下 1（local_unsupported 1）" in out
    assert len(local.calls) == 3 and local.calls[2][1].startswith("【事实】")
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    (row,) = world.rows("SELECT reason, rule_id FROM quarantine WHERE item_kind = 'fact'")
    assert (row["reason"], row["rule_id"]) == ("C:local_unsupported", "C")
    assert {row["judged_by"] for row in world.rows("SELECT judged_by FROM relations")} == {"local"}
    checked = {row["l4_version"] for row in world.rows("SELECT l4_version FROM relation_claims")}
    assert checked == {precheck.checked_by(None, JudgeSpec(LOCAL, FAKE_LOCAL_NAME, 0.1), LOCAL_FACTS)}
    assert main(["verify", "--library", "prod", "--fake"]) == 0
    assert "事实的判断（f1" in capsys.readouterr().out
    assert main(["audit", "--library", "prod", "--summary"]) == 0
    assert "fact_judge / fact / judge / unsupported：1" in capsys.readouterr().out


def test_cli_precheck_with_recheck_or_without_judge_c_is_a_setting_error(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据放在海港银行。"))
    monkeypatch.setenv("JAVMEM_PRECHECK", "on")
    assert main(["extract", "--library", "prod", "--fake"]) == 2  # 没配判断 C
    monkeypatch.setenv("JAVMEM_JUDGE_MODE", "local")
    assert main(["extract", "--library", "prod", "--fake", "--recheck"]) == 2  # 和 G7 复核不一起开
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings", "--recheck"]) == 2
    assert not (world.data / "l3" / "l3.db").exists()


def test_cli_audit_says_when_nothing_is_found(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    deliver(world, day_packet("2026-11-01", "我：X1 票据放在海港银行。"))
    assert main(["audit", "--library", "prod", "--item", "F0000"]) == 1
    assert main(["audit", "--library", "prod", "--item", "x#y#3"]) == 1
    assert main(["audit", "--library", "prod", "--packet", "no-such-packet"]) == 1
    assert main(["audit", "--library", "prod", "--item", "a", "--packet", "b"]) == 2



# ---------- 发过去的原话：抬头写全日期（D184，只给事实） ----------
#
# 窗口文字（L3 看的）跟上一段同一天、同一时区的抬头只写“时:分”（render._time，省字）。判的时候只发证据前后各两句，
# 写着日期的那个抬头常常不在里面，sol 就说“原话看不出是哪天”（J5.3 第 3 步：这种 655 条里挡了 69%）。
# 事实那段话的第一个抬头、和这一段里上一个抬头不是同一天的抬头写全日期；关系那段话照旧（关系那一行不带日子）。

DATED_LINES = ("我：先说别的。", "desk-bot：好的。", "我：再说一件。", "desk-bot：收到。", "我：还有一件。", "desk-bot：明白。",
               "我：X1 票据放在海港银行。", "desk-bot：记下了。")
HEADING = re.compile(r"^\[[^\]]+\][^：\n]*：$", re.M)


def said_headings(text: str) -> list[str]:
    """发过去的那段话里【原话】以后的抬头。"""
    return HEADING.findall(text.split("\n【原话】\n", 1)[1])


def windowed(raw: str, groups: list[tuple[str, ...]], *, config: WindowConfig | None = None, at: int = 0) -> Checked:
    """一个包切出来的第 at 个窗口，每组原话抽一条事实（一句一条证据，事实的文字就是这几句连起来），过一遍 L4。"""
    packet = normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))
    window = (build_windows([packet], config=config) if config is not None else build_windows([packet]))[at]
    rendered = render_window(window, PATTERNS)
    found = [{**l3_fact(window, group[0], "，".join(group)),
              "evidence": [{"ref": ref_of(window, phrase), "quote": phrase} for phrase in group]} for group in groups]
    extraction = map_output({"entities": [], "facts": found, "relations": []}, window, rendered.redacted_refs)
    return Checked(window, rendered, "l3-key", extraction, verify_window(window, rendered, extraction))


def only_request(item: Checked) -> reljudge.JudgeRequest:
    requests = precheck.fact_requests([item], PATTERNS)
    assert len(requests) == 1, [codes(item)]
    return requests[0][2]


def test_the_first_heading_sent_carries_the_full_date() -> None:
    """证据那一句的抬头只写“时:分”，写日期的抬头在前后两句以外：发过去的第一个抬头补上那一天，后面同一天的照旧。"""
    window = item_of(DATED_LINES, []).window
    item = item_of(DATED_LINES, [fact_of(window, "X1 票据放在海港银行", "X1 票据放在海港银行")])
    request = only_request(item)
    assert "\n[10:34] 我：\nT5 还有一件。\n" in item.rendered.text  # L3 看的窗口文字照旧只写时分
    assert said_headings(request.text) == ["[2026-11-08 10:34 +08:00] 我：", "[10:35] desk-bot：", "[10:36] 我：",
                                           "[10:37] desk-bot："]


def test_a_date_heading_inside_the_earlier_talk_still_counts() -> None:
    """写日期的抬头在【上文】里（前一个窗口的末尾），【正文】第一句和它同一天：正文的抬头也补上日期。"""
    body = "## 原话 · 10:00–10:59\n" + "".join(f"- [10:{n:02d}] 我：第{n}句{'话' * 30}。\n" for n in range(30))
    config = WindowConfig(max_chars=300, context_chars=100, context_units=3)
    probe = windowed(build_packet(body=body), [], config=config, at=1)
    first = next(block for block in probe.window.blocks if not block.context)
    item = windowed(build_packet(body=body), [(first.lines[0].text,)], config=config, at=1)
    rows = item.rendered.text.split("\n")
    body_heading = rows[rows.index("【正文】") + 1]
    assert body_heading == f"[{first.said_at:%H:%M}] 我："  # 窗口文字里正文的抬头只写时分（日期在上文的抬头里）
    assert said_headings(only_request(item).text)[0] == f"[{first.said_at:%Y-%m-%d %H:%M} +08:00] 我："


def test_a_heading_after_a_skipped_day_change_carries_its_own_date() -> None:
    """两处证据隔得远、跨了一天：后一处的抬头只写“时:分”（写日期的那个隔在中间、没发过去），补上它自己那一天。"""
    day1 = ("桌子", "香蕉", "橘子", "葡萄", "西瓜", "草莓")
    day2 = ("铅笔", "钢笔", "毛笔", "蜡笔", "粉笔", "画笔")
    body = ("## 原话 · 2026-11-07 10:30–2026-11-08 09:05\n"
            + "".join(f"- [2026-11-07 10:{30 + n}] 我：说到{word}。\n" for n, word in enumerate(day1))
            + "".join(f"- [09:0{n}] desk-bot：说到{word}。\n" for n, word in enumerate(day2)))
    item = windowed(build_packet(body=body, covers_from="2026-11-07T00:00:00+08:00"), [("说到香蕉", "说到粉笔")])
    assert "\n[09:02] desk-bot：\n" in item.rendered.text
    headings = said_headings(only_request(item).text)
    assert headings[0] == "[2026-11-07 10:30 +08:00] 我："  # 本来就写全
    assert "[2026-11-08 09:02 +08:00] desk-bot：" in headings and "[09:02] desk-bot：" not in headings
    assert "[09:03] desk-bot：" in headings  # 同一天接着的照旧


def test_a_later_mail_on_the_same_day_gets_its_date_and_its_own_zone() -> None:
    """邮件的抬头一样补；邮件保留原来的时区，补的是它自己的时区。"""
    first = mail_section("<a@x>", "<a@x>", "2026-11-03T09:12:00+07:00", "  第一封说到桌子。\n  第一封说到香蕉。\n")
    second = mail_section("<b@x>", "<a@x>", "2026-11-03T09:40:00+07:00",
                          "  第二封说到铅笔。\n  第二封说到钢笔。\n  第二封说到毛笔。\n  第二封说到蜡笔。\n", subject="Re: 报价")
    item = windowed(build_mail_packet(sections=first + second), [("第二封说到蜡笔",)])
    assert re.search(r"^\[09:40\] 邮件，发件人 ", item.rendered.text, re.M)
    assert said_headings(only_request(item).text)[0].startswith("[2026-11-03 09:40 +07:00] 邮件，发件人 ")


def test_an_attachment_heading_gets_the_date_too() -> None:
    """附件的抬头（谁发的哪个附件）一样补。"""
    item = windowed(FILE_PACKET, [("违约金按 5% 算",)])
    assert "\n[10:31] 我 发的附件 合同.txt（外部内容）：\n" in item.rendered.text
    first = said_headings(only_request(item).text)[0]
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} 10:31 [+-]\d{2}:\d{2}\] 我 发的附件 合同\.txt（外部内容）：", first)


def test_a_full_first_heading_sends_exactly_the_same_text() -> None:
    """第一个抬头本来就写全、中间没换日子的：发过去的话一个字不变（存下的判定照用，键不变）。"""
    lines = ("我：X1 票据放在海港银行。", "desk-bot：好的。", "我：明白。")
    window = item_of(lines, []).window
    request = only_request(item_of(lines, [fact_of(window, "X1 票据放在海港银行", "X1 票据放在海港银行")]))
    assert request.text == (
        "【事实】X1 票据放在海港银行（说定了）\n【原话】\n来源 desk-bot，对话。“我”是记忆的主人，desk-bot 是这个包的 Bot，"
        "标着“别人”的是群里的人或者别的 Bot。\n[2026-11-08 10:30 +08:00] 我：\n★T1 X1 票据放在海港银行。\n[10:31] desk-bot：\n"
        "T2 好的。\n[10:32] 我：\nT3 明白。\n"
    )


def test_relation_requests_are_sent_exactly_as_before() -> None:
    """只给事实补：关系那一行不带日子，判断 C 发过去的话和改以前一字不差（键不变，不用重判）。"""
    window = item_of(DATED_LINES, []).window
    proof = (ref_of(window, "X1 票据放在海港银行"), "X1 票据放在海港银行")
    item = item_of(DATED_LINES, [fact_of(window, "X1 票据放在海港银行", "X1 票据放在海港银行")],
                   relations=[relation(NOTE, "托管于", BANK, proof)], entities=[NOTE, BANK])
    (_, _, request), = reljudge.judge_requests([item], PATTERNS)
    assert request.text == (
        "【关系】X1 票据 —托管于→ 海港银行（状态：生效）\n【原话】\n来源 desk-bot，对话。“我”是记忆的主人，desk-bot 是这个包的 Bot，"
        "标着“别人”的是群里的人或者别的 Bot。\n[10:34] 我：\nT5 还有一件。\n[10:35] desk-bot：\nT6 明白。\n[10:36] 我：\n"
        "★T7 X1 票据放在海港银行。\n[10:37] desk-bot：\nT8 记下了。\n"
    )
    assert said_headings(only_request(item).text)[0] == "[2026-11-08 10:34 +08:00] 我："  # 同一个窗口，事实那段话补了
