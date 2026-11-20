"""javmem regress 审阅查出来的第三批（U2 甲，11-19）：过没过要判得对。

只差判断 C 的窗口、主图那一路悄悄退回账本视图、基准里少了线、套件写错了键、题变了、有付钱的那一遍存成基准、
基准写坏了、相对路径的工作目录、输出落进仓库、锁定区、“无”算进出处、问话向量缓存里还没并进去的 -wal。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from test_regress import GOOD, suite_file

from javmem.regress import evidence
from javmem.regress import run as regress
from javmem.regress.suite import GraphSpec, RetrievalLine, Suite


def bare_suite(tmp_path: Path, mode: str = "managed") -> Suite:
    return Suite(path=tmp_path / "suite.yaml", name="x", config={}, gold=(), retrieval=(), graph=GraphSpec(mode=mode),
                 baseline=tmp_path / "baseline.json")


def fake_javmem(summary: dict) -> object:
    def run(env, log, *args, module="javmem"):  # noqa: ANN001, ANN202
        if "--summary" in args:
            Path(args[args.index("--summary") + 1]).write_text(json.dumps(summary), encoding="utf-8")
        return 0, ""

    return run


def test_windows_left_without_a_judgment_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只差判断 C 的窗口 javmem write 照样退出码 0、可没写进账：算“跑不了，要重抽”，不往下量。"""
    monkeypatch.setattr(regress, "javmem", fake_javmem({"this_run": {"windows_without_judgment": 2}}))
    with pytest.raises(regress.CannotRun, match="2 个窗口"):
        regress._write({}, tmp_path, fake_embeddings=True)
    monkeypatch.setattr(regress, "javmem", fake_javmem({"this_run": {"packets_waiting": 1}}))
    with pytest.raises(regress.CannotRun, match="排队"):
        regress._write({}, tmp_path, fake_embeddings=True)
    monkeypatch.setattr(regress, "javmem", fake_javmem({"this_run": {}}))
    assert regress._write({}, tmp_path, fake_embeddings=True) == {"this_run": {}}


def test_a_graph_route_that_fell_back_is_noticed(monkeypatch: pytest.MonkeyPatch) -> None:
    """主图那一路悄悄退回了账本视图（连不上、超时、问话的向量对不上）：量出来的数不能当主图的报。"""
    from javmem.query import tools

    pages = iter([{"items": [{"kind": "fact", "id": "F1", "content": "x"}], "degraded": ["graph"], "next_cursor": None}])
    monkeypatch.setattr(tools, "memory_search", lambda arguments, settings: next(pages))
    items, degraded = evidence._pages("问话", None, True)
    assert len(items) == 1 and degraded
    pages = iter([{"items": [], "degraded": [], "next_cursor": None}])
    assert evidence._pages("问话", None, True) == ([], False)


def test_a_fallen_back_graph_route_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(env, log, *args, module="javmem"):  # noqa: ANN001, ANN202
        if module == "javmem.regress.evidence":
            out = Path(args[args.index("--out") + 1])
            out.write_text(json.dumps({"totals": {}, "resolved": 1, "sources": 1, "routes": ["graph"], "questions": 1,
                                       "questions_sha256": "x", "graph_degraded": 3, "queries": {}}), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(regress, "javmem", fake)
    monkeypatch.setattr(regress, "_prepare", lambda *args: tmp_path / "data")
    monkeypatch.setattr(regress, "_write", lambda *args, **kwargs: {})
    line = RetrievalLine(line="a", env={}, packets=(), store=tmp_path, questions=tmp_path / "q.json", caller="x")
    with pytest.raises(regress.CannotRun, match="3 道题"):
        regress.run_retrieval(bare_suite(tmp_path, "given"), line, tmp_path / "work", object(), lambda text: None)


def gold_result(line: str = "a") -> dict[str, object]:
    return {line: {"scores": {"format": "f", "gold": {}, "sides": {"javmem": {"facts": {"precision": 0.5}}}}}}


def test_lines_missing_on_either_side_are_said_and_fail_under_exact(tmp_path: Path) -> None:
    suite = bare_suite(tmp_path)
    result = regress.Result(gold=gold_result("new"))
    baseline = {"gold": gold_result("old")}
    said: list[str] = []
    assert regress.report(suite, result, baseline, exact=False, graph_mode="off", log=said.append) == 0
    assert any("基准里没有" in line for line in said) and any("这次没跑" in line for line in said)
    assert regress.report(suite, result, baseline, exact=True, graph_mode="off", log=lambda text: None) == 1


def test_a_typo_in_the_suite_is_an_error(tmp_path: Path) -> None:
    from javmem.config import ConfigError
    from javmem.regress.suite import load_suite

    (tmp_path / "suite.yaml").write_text("format: javmem-regress/1\nretrival: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="retrival"):
        load_suite(tmp_path / "suite.yaml")
    (tmp_path / "suite.yaml").write_text("format: javmem-regress/1\nname: 空的\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="一条线都没有"):
        load_suite(tmp_path / "suite.yaml")


def test_suite_paths_are_normalised_and_the_locked_area_is_refused(tmp_path: Path) -> None:
    from javmem.config import ConfigError
    from javmem.regress.suite import load_suite

    (tmp_path / "q.json").write_text("{}", encoding="utf-8")
    (tmp_path / "p.md").write_text("x", encoding="utf-8")
    text = ("format: javmem-regress/1\nretrieval:\n  - {line: a, packets: [p.md], store: q.json, questions: q.json, caller: x}\n"
            "graph: {mode: managed, data: sub/../graph}\n")
    (tmp_path / "suite.yaml").write_text(text, encoding="utf-8")
    assert load_suite(tmp_path / "suite.yaml").graph.data == (tmp_path / "graph").resolve()
    (tmp_path / "锁定").mkdir()
    (tmp_path / "锁定" / "q.json").write_text("{}", encoding="utf-8")
    (tmp_path / "suite.yaml").write_text(text.replace("questions: q.json", "questions: 锁定/q.json"), encoding="utf-8")
    with pytest.raises(ConfigError, match="锁定"):
        load_suite(tmp_path / "suite.yaml")


def retrieval_now(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {"routes": ["ledger"], "resolved": 3, "sources": 3, "questions": 2, "questions_sha256": "q1",
                               "totals": {"ledger": {"5000": 2, "1500": 1}}}
    base.update(changes)
    return base


def test_changed_questions_are_flagged() -> None:
    _, dropped, _ = regress.compare_retrieval(retrieval_now(questions_sha256="q2", questions=3), retrieval_now())
    assert any("题变了" in line for line in dropped)
    state, dropped, _ = regress.compare_retrieval(retrieval_now(routes=["graph", "ledger"], totals={
        "graph": {"5000": 9, "1500": 9}, "ledger": {"5000": 2, "1500": 1}}), retrieval_now())
    assert state == "变了" and dropped == []  # 基准里没有主图那一路：不算降，可也不算“一样”


def test_baselines_with_paid_rows_or_without_the_graph_are_not_saved() -> None:
    assert regress.may_save(regress.Result(), graph_mode="managed", suite_mode="managed") is None
    assert "付钱" in regress.may_save(regress.Result(paid_rows=1), graph_mode="managed", suite_mode="managed")
    assert "--no-graph" in regress.may_save(regress.Result(), graph_mode="off", suite_mode="managed")
    assert regress.may_save(regress.Result(), graph_mode="off", suite_mode="off") is None


def test_a_broken_baseline_cannot_run(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    for broken in ({"format": regress.BASELINE_FORMAT, "gold": {"a": {}}},
                   {"format": regress.BASELINE_FORMAT, "retrieval": {"a": {"totals": {"ledger": {"5000": 1}}}}}):
        path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(regress.CannotRun, match="基准"):
            regress._load_baseline(path)


def test_work_dir_is_absolute_and_outputs_stay_outside_the_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    from javmem.cli import main

    path = suite_file(tmp_path, GOOD)
    repo = Path(__file__).resolve().parents[1]
    assert main(["regress", "--suite", str(path), "--work", str(tmp_path / "w"), "--json", str(repo / "x.json")]) == 2
    assert not (repo / "x.json").exists() and "仓库" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    assert regress.work_dir("relative-work", path) == (tmp_path / "relative-work").resolve()
    fake_repo = tmp_path / "repo"
    (fake_repo / ".git").mkdir(parents=True)
    (fake_repo / "src" / "javmem").mkdir(parents=True)
    (fake_repo / "baseline.json").write_text("{}", encoding="utf-8")
    suite = Suite(path=fake_repo / "suite.yaml", name="x", config={}, gold=(), retrieval=(), graph=GraphSpec(),
                  baseline=fake_repo / "baseline.json")
    regress.save_baseline(suite, regress.Result(), "测试", lambda text: None)
    assert sorted(one.name for one in fake_repo.iterdir()) == [".git", "baseline.json", "src"]  # 仓库里不留备份（git 里有历史）


def test_sources_marked_none_are_not_counted() -> None:
    assert evidence.real_sources({"sources": ["包 ｜ 话", " ", "无", " 无 "]}) == ["包 ｜ 话"]


def test_the_query_vector_cache_reads_what_is_still_in_the_wal(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE vectors (key TEXT PRIMARY KEY, vector BLOB)")
    key = hashlib.sha256(("m\n" + "问话").encode("utf-8")).hexdigest()
    writer.execute("INSERT INTO vectors VALUES (?, ?)", (key, b"\x01\x02"))
    writer.commit()  # 还在 -wal 里，没并进主文件
    try:
        assert evidence.CachedQueries("m", path, None)._cached("问话") == b"\x01\x02"
    finally:
        writer.close()
