"""javmem regress 审阅查出来的那几处（U2 甲，11-19）：测试图的保险、不花钱不往外发、过没过判得对不对。

测试图那几条不起 docker：把 ManagedGraph 跑 docker 的几个方法换成只记命令的假的，看回归在别人的图上一个 docker 命令都不跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from javmem.regress import graph as regress_graph
from javmem.regress import run as regress
from javmem.regress.suite import GraphSpec, RetrievalLine, Suite

SOMEONE = ["/run/desktop/mnt/host/q/someone/big/data", "/run/desktop/mnt/host/q/someone/big/logs"]


class Done:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """ManagedGraph 跑 docker 的地方都换成假的：只记 compose 的命令。容器挂的是别人的目录、正开着。"""
    calls: list[tuple[str, ...]] = []

    def fake_run(self, *args: str, data: Path | None = None, check: bool = True) -> Done:  # noqa: ANN001
        calls.append(args)
        return Done()

    monkeypatch.setattr(regress_graph.ManagedGraph, "_run", fake_run)
    monkeypatch.setattr(regress_graph.ManagedGraph, "mounts", lambda self: list(SOMEONE))
    monkeypatch.setattr(regress_graph.ManagedGraph, "running", lambda self: True)
    return calls


def managed_suite(root: Path, *, lock: bool = True) -> Suite:
    line = RetrievalLine(line="a", env={}, packets=(root / "p.md",), store=root / "l3.db", questions=root / "q.json",
                         caller="someone")
    spec = GraphSpec(mode="managed", compose=root / "compose.yaml", data=root / "graph", container="c",
                     lock=root / "locks" / "7689.txt" if lock else None)
    return Suite(path=root / "suite.yaml", name="x", config={}, gold=(), retrieval=(line,), graph=spec,
                 baseline=root / "baseline.json")


def test_someone_elses_running_graph_is_left_alone(tmp_path: Path, docker: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(regress, "run_retrieval", lambda suite, line, work, graph, log: graph.up())
    suite = managed_suite(tmp_path)
    with pytest.raises(regress_graph.GraphRefused):
        regress.run_suite(suite, tmp_path / "work", graph_mode="managed", log=lambda text: None)
    assert docker == []  # 一个 compose 命令都没跑：没停它，也没照它的目录重建容器
    assert not suite.graph.lock.exists()  # 自己拿的锁放了


def test_a_taken_lock_leaves_the_graph_and_the_lock_alone(tmp_path: Path, docker: list) -> None:
    suite = managed_suite(tmp_path)
    suite.graph.lock.parent.mkdir(parents=True)
    suite.graph.lock.write_text("会话：别人\n做什么：javmem regress，……\n", encoding="utf-8")
    with pytest.raises(regress_graph.GraphBusy):
        regress.run_suite(suite, tmp_path / "work", graph_mode="managed", log=lambda text: None)
    assert docker == []
    assert suite.graph.lock.read_text(encoding="utf-8").startswith("会话：别人")


@pytest.mark.parametrize("lock", [True, False])
def test_failing_before_the_graph_is_up_touches_nothing(tmp_path: Path, docker: list, monkeypatch: pytest.MonkeyPatch,
                                                        lock: bool) -> None:
    def cannot(suite, line, work, graph, log):  # noqa: ANN001, ANN202
        raise regress.CannotRun("要重抽")

    monkeypatch.setattr(regress, "run_retrieval", cannot)
    with pytest.raises(regress.CannotRun):
        regress.run_suite(managed_suite(tmp_path, lock=lock), tmp_path / "work", graph_mode="managed", log=lambda text: None)
    assert docker == []


def test_the_lock_is_released_only_by_its_own_holder(tmp_path: Path) -> None:
    """释放锁时认的是这一遍自己写进去的编号：别的回归换上来的锁（里面也写着 javmem regress）不删。"""
    lock = tmp_path / "7689.txt"
    mine = regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "graph", "c", lock=lock)
    mine.take_lock(10)
    lock.write_text("会话：javmem regress\n做什么：javmem regress，别的那一遍\n", encoding="utf-8")  # 中途被换了
    mine.drop_lock()
    assert lock.exists()


def test_wsl_mounts_are_not_taken_for_a_drive() -> None:
    """Docker Desktop 的 WSL 挂载（/run/desktop/mnt/host/wsl/…）不是盘符：认不出原来的目录，就不去还原。"""
    assert regress_graph._host_dir("/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/abc") is None
    assert regress_graph._host_dir("/run/desktop/mnt/host/q/x/data") == Path("Q:/x/data")
    assert not regress_graph._same_dir("/run/desktop/mnt/host/wsl/x/data", Path("w:sl/x/data"))


def test_restoring_says_so_when_it_did_not_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[str] = []
    graph = regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "graph", "c", log=said.append)

    class Failed(Done):
        returncode = 1

    monkeypatch.setattr(graph, "_run", lambda *args, **kwargs: Failed())
    graph.touched, graph.had_container, graph.original = True, True, tmp_path / "big"
    graph.down()
    assert any("没建成" in line for line in said) and not any("建好了" in line for line in said)


def test_docker_and_git_never_get_javmem_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """起 docker、git 的子进程，环境里去掉所有 JAVMEM_*（数据目录那一个照给），付钱的密钥、图的密码都不给。"""
    seen: list[dict[str, str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN202
        seen.append(dict(kwargs.get("env") or {}))
        return Done()

    monkeypatch.setenv("JAVMEM_LLM_API_KEY", "sk-not-real")
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "not-real-password")
    monkeypatch.setattr(regress_graph.subprocess, "run", fake_run)
    graph = regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "graph", "c")
    graph._run("down")
    graph.mounts()
    graph.running()
    assert seen and all(not any(key.startswith("JAVMEM_") and key != regress_graph.DATA_ENV for key in env) for env in seen)
    from javmem import versions

    monkeypatch.setattr(versions.subprocess, "run", fake_run)
    seen.clear()
    versions.code_info()
    assert all("JAVMEM_LLM_API_KEY" not in env for env in seen)


# ---------- 不花钱、不往外发 ----------


def suite_for_env(tmp_path: Path, mode: str = "managed") -> Suite:
    return Suite(path=tmp_path / "suite.yaml", name="x", config={}, gold=(), retrieval=(),
                 graph=GraphSpec(mode=mode), baseline=tmp_path / "baseline.json")


def test_only_a_local_embedding_service_reaches_the_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_EMBEDDING_BASE_URL", "https://embeddings.example.com/v1")
    monkeypatch.setenv("JAVMEM_EMBEDDING_API_KEY", "not-real-key")
    env = regress.child_env(suite_for_env(tmp_path), {}, tmp_path / "data", tmp_path / "ledger.csv")
    assert not any(key.startswith("JAVMEM_EMBEDDING_") for key in env)  # 外面的（可能付钱、真数据会发出去）不给
    monkeypatch.setenv("JAVMEM_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    env = regress.child_env(suite_for_env(tmp_path), {}, tmp_path / "data", tmp_path / "ledger.csv")
    assert env["JAVMEM_EMBEDDING_BASE_URL"] == "http://127.0.0.1:9/v1" and env["JAVMEM_EMBEDDING_API_KEY"] == "not-real-key"


def test_children_use_the_lines_embedding_model_and_the_managed_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_EMBEDDING_MODEL", "some-other-model")
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_URI", "bolt://127.0.0.1:7699")
    line = RetrievalLine(line="a", env={}, packets=(), store=tmp_path, questions=tmp_path, caller="x",
                         embedding_model="Qwen/Qwen3-Embedding-4B")
    env = regress.child_env(suite_for_env(tmp_path), regress.line_env(line), tmp_path / "data", tmp_path / "ledger.csv")
    assert env["JAVMEM_EMBEDDING_MODEL"] == "Qwen/Qwen3-Embedding-4B"  # 写账、同步、量的时候是同一个模型名
    assert "JAVMEM_GRAPH_TEST_NEO4J_URI" not in env  # managed：连的是回归自己起的那个（默认的口），不照外面配的
    given = regress.child_env(suite_for_env(tmp_path, "given"), {}, tmp_path / "data", tmp_path / "ledger.csv")
    assert given["JAVMEM_GRAPH_TEST_NEO4J_URI"] == "bolt://127.0.0.1:7699"


def test_a_given_graph_needs_its_address_written_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """given 的图要在环境变量里写明地址：不写就是默认的测试图口，本机几个会话共用的那一个，不去占。"""
    line = RetrievalLine(line="a", env={}, packets=(), store=tmp_path, questions=tmp_path, caller="x")
    suite = Suite(path=tmp_path / "suite.yaml", name="x", config={}, gold=(), retrieval=(line,),
                  graph=GraphSpec(mode="given"), baseline=tmp_path / "baseline.json")
    monkeypatch.setattr(regress, "run_retrieval", lambda *args: {})
    with pytest.raises(regress.CannotRun, match="写明"):
        regress.run_suite(suite, tmp_path / "work", graph_mode="given", log=lambda text: None)
