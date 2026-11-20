"""javmem regress 复审查出来的那几处（U2 甲，11-19 第二轮）：套件不许自己写地址、合好环境以后再核一遍；given 不用测试库的图；
主图的向量那一路关着的算跑不了；“读到了”要真读成一张表；一次只写一个包的那几版提示词不收；多条线时 touched 复位；
通配指到锁定区底下先拦、不列文件。都不起 docker、不连网。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from javmem.config import ConfigError
from javmem.regress import graph as regress_graph
from javmem.regress import run as regress
from javmem.regress.suite import GraphSpec, GoldLine, RetrievalLine, Score, Suite, load_suite

LINE = "  - {line: a, packets: [p.md], store: q.json, questions: q.json, caller: x%s}\n"


def write_suite(tmp_path: Path, *, config: str = "", env: str = "", extra: str = "") -> Path:
    (tmp_path / "q.json").write_text("{}", encoding="utf-8")
    (tmp_path / "p.md").write_text("x", encoding="utf-8")
    text = "format: javmem-regress/1\n" + (f"config: {config}\n" if config else "") + "retrieval:\n"
    text += LINE % ((f", env: {env}" if env else "") + extra)
    (tmp_path / "suite.yaml").write_text(text, encoding="utf-8")
    return tmp_path / "suite.yaml"


@pytest.mark.parametrize(
    ("where", "text"),
    [
        ("config", "{JAVMEM_EMBEDDING_BASE_URL: 'https://embed.example.com/v1'}"),
        ("env", "{JAVMEM_EMBEDDING_BASE_URL: 'http://127.0.0.1:9/v1'}"),
        ("env", "{JAVMEM_GRAPH_TEST_NEO4J_URI: 'bolt://127.0.0.1:7699'}"),
        ("config", "{JAVMEM_LLM_BASE_URL: 'https://relay.example.com/v1'}"),
    ],
)
def test_the_suite_may_not_write_addresses(tmp_path: Path, where: str, text: str) -> None:
    """地址（_URL、_URI）不写进套件：本机的服务、测试图照外面的环境变量，回归自己核过才交给子进程。"""
    with pytest.raises(ConfigError, match="地址"):
        load_suite(write_suite(tmp_path, **{where: text}))


def test_child_env_is_checked_again_after_merging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """就算有一样（以后加的设置、手写的线）把地址带了进来，合好以后再核一遍：外面的向量服务、managed 下的测试图地址都去掉。"""
    monkeypatch.setenv("JAVMEM_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("JAVMEM_EMBEDDING_API_KEY", "not-real-key")
    suite = Suite(path=tmp_path / "s.yaml", name="x", config={}, gold=(), retrieval=(), graph=GraphSpec(mode="managed"),
                  baseline=tmp_path / "b.json")
    env = regress.child_env(suite, {"JAVMEM_EMBEDDING_BASE_URL": "https://embed.example.com/v1",
                                    "JAVMEM_GRAPH_TEST_NEO4J_URI": "bolt://127.0.0.1:7699"}, tmp_path / "d", tmp_path / "l.csv")
    assert not any(key.startswith("JAVMEM_EMBEDDING_") and key != "JAVMEM_EMBEDDING_MODEL" for key in env)
    assert "JAVMEM_GRAPH_TEST_NEO4J_URI" not in env


def test_given_never_takes_the_test_librarys_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """given 写明的地址不能是默认的测试图口（测试库的图，本机放着测试库的真数据）；一开头就拦，金标准那几条也不跑。"""
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_URI", "bolt://127.0.0.1:7689")
    ran: list[str] = []
    monkeypatch.setattr(regress, "run_gold", lambda suite, line, work: ran.append(line.line) or {})
    gold = GoldLine(line="g", env={}, packets=(), store=tmp_path, score=Score(gold=tmp_path))
    line = RetrievalLine(line="a", env={}, packets=(), store=tmp_path, questions=tmp_path, caller="x")
    suite = Suite(path=tmp_path / "s.yaml", name="x", config={}, gold=(gold,), retrieval=(line,), graph=GraphSpec(mode="given"),
                  baseline=tmp_path / "b.json")
    with pytest.raises(regress.CannotRun, match="测试库的图"):
        regress.run_suite(suite, tmp_path / "work", graph_mode="given", log=lambda text: None)
    assert ran == []


def test_a_store_that_opens_but_has_no_tables_is_not_read(tmp_path: Path) -> None:
    from javmem import versions

    path = tmp_path / "l3.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE something_else (x)")
    conn.commit()
    conn.close()
    assert versions.served([path], [], None)["read"] == []


def test_prompts_that_write_one_packet_at_a_time_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="一次只写一个包"):
        load_suite(write_suite(tmp_path, env="{JAVMEM_L3_PROMPT: l3-v22}"))


def test_a_later_refusal_resets_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第一条线起过自己的图（touched），第二条线起图前别人换上了自己的容器、开着：拒了，最后也不去停它。"""
    graph = regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "graph", "c")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(graph, "_run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(graph, "mounts", lambda: ["/run/desktop/mnt/host/q/someone/big/data"])
    monkeypatch.setattr(graph, "running", lambda: True)
    graph.touched = True
    with pytest.raises(regress_graph.GraphRefused):
        graph.up()
    graph.down()
    assert calls == [] and not graph.touched


def test_a_glob_into_the_locked_area_is_refused_before_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import glob as glob_module

    write_suite(tmp_path)  # 建好 q.json、p.md
    (tmp_path / "suite.yaml").write_text(
        "format: javmem-regress/1\nretrieval:\n  - {line: a, packets: ['锁定/*.md'], store: q.json, questions: q.json,"
        " caller: x}\n", encoding="utf-8")
    monkeypatch.setattr(glob_module, "glob", lambda *args, **kwargs: pytest.fail("先列了锁定区的文件"))
    with pytest.raises(ConfigError, match="锁定"):
        load_suite(tmp_path / "suite.yaml")


def test_fake_vector_lines_default_to_the_fake_model_name(tmp_path: Path) -> None:
    suite = load_suite(write_suite(tmp_path, extra=", fake_embeddings: true"))
    assert suite.retrieval[0].embedding_model == "fake-embedding"
    with pytest.raises(ConfigError, match="fake-embedding"):
        load_suite(write_suite(tmp_path, extra=", fake_embeddings: true, embedding_model: Qwen/Qwen3-Embedding-4B"))


def test_a_graph_whose_vector_route_is_off_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(env, log, *args, module="javmem"):  # noqa: ANN001, ANN202
        if module == "javmem.regress.evidence":
            Path(args[args.index("--out") + 1]).write_text(json.dumps(
                {"totals": {}, "resolved": 1, "sources": 1, "routes": ["graph"], "questions": 1, "questions_sha256": "x",
                 "graph_degraded": 0, "graph_vectors": False, "queries": {}}), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(regress, "javmem", fake)
    monkeypatch.setattr(regress, "_prepare", lambda *args: tmp_path / "data")
    monkeypatch.setattr(regress, "_write", lambda *args, **kwargs: {})
    line = RetrievalLine(line="a", env={}, packets=(), store=tmp_path, questions=tmp_path / "q.json", caller="x")
    suite = Suite(path=tmp_path / "s.yaml", name="x", config={}, gold=(), retrieval=(line,), graph=GraphSpec(mode="given"),
                  baseline=tmp_path / "b.json")
    with pytest.raises(regress.CannotRun, match="向量那一路"):
        regress.run_retrieval(suite, line, tmp_path / "work", object(), lambda text: None)
