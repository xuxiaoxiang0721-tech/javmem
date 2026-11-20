"""GitHub 那一层的回归跑分（U2 甲，D205 ①）：编的小套件 tests/fixtures/regress-mini/ 整个跑一遍 `javmem regress --exact`，
分数、命中数要和钉着的 baseline.json 一字不差。

- 包、金标准、题都是编的；模型的回复是录好的（l3-replies.json），不调模型。
- 只在 ubuntu 那一路跑：Windows 那一路一分钟不加（D205 ①，Windows 已经 30 多分钟，上限 45 分钟）。
- 给了测试用的 Neo4j（CI 的 ubuntu 那一路有，JAVMEM_TEST_NEO4J_URI、_PASSWORD）就量主图那一路（清空那个图再用，和
  test_graph_neo4j.py 同一个 fixture）；没给的不量主图那一路（只比账本视图、原文层和金标准）。
- 分数变了：照“一次只改一项”看是哪一改动的，该变的话换基准（起一个空的、只给这一遍用的 Neo4j，照开发说明“回归跑分”
  GitHub 这一层那一段跑 `javmem regress … --save-baseline --note 为什么`），提交说明里写为什么。代码改了窗口的样子（L1、L2、
  渲染）、提示词，录好的回复对不上，这里报“跑不了：要重抽”：照同一段重录（花钱，照 D205 ② 批）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_graph_neo4j import PASSWORD, URI, neo_ready, real  # noqa: F401 - 量主图那一条借这两个 fixture 清空那个图

from javmem.cli import main

MINI = Path(__file__).resolve().parent / "fixtures" / "regress-mini"
GRAPH = bool(URI and PASSWORD)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="GitHub 那一层只在 ubuntu 跑（D205 ①：Windows 那一路一分钟不加）")


def _run(tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str]:
    code = main(["regress", "--suite", str(MINI / "suite.yaml"), "--work", str(tmp_path / "work"), "--exact", *extra])
    return code, capsys.readouterr().out


@pytest.mark.skipif(GRAPH, reason="有测试用的 Neo4j：走量主图的那一条")
def test_the_made_up_suite_without_the_graph(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(tmp_path, capsys, "--no-graph")
    assert code == 0, out
    assert "金标准 sol：一样" in out and "调试题 sol" in out and "付钱的 0 行" in out


@pytest.mark.skipif(not GRAPH, reason="没给测试用的 Neo4j（JAVMEM_TEST_NEO4J_URI、_PASSWORD）")
def test_the_made_up_suite_with_the_graph(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                         monkeypatch: pytest.MonkeyPatch, real: object) -> None:  # noqa: F811
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_URI", URI)
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_PASSWORD", PASSWORD)
    code, out = _run(tmp_path, capsys)
    assert code == 0, out
    assert "金标准 sol：一样" in out and "主图 " in out and "付钱的 0 行" in out
