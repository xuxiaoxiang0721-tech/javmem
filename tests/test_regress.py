"""javmem regress（U2 甲，D205 ①）里不要真数据、不要 Neo4j、不要 docker 的那几块：套件写错了怎么报、证据命中率的算法
（照 J2.15）、和基准比的规矩、记账核一遍、测试图那几道保险。两个系统都跑，很快。

GitHub 那一层（编的小套件整个跑一遍）在 tests/test_regress_mini.py，只在 ubuntu 跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import CHAT_ID, CHAT_PACKET, World

from javmem.config import ConfigError
from javmem.regress import evidence, graph as regress_graph, run as regress
from javmem.regress.suite import load_suite
from javmem.usage import TokenLedger


def suite_file(tmp_path: Path, text: str) -> Path:
    (tmp_path / "packets" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "packets" / "src" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "l3.db").write_bytes(b"")
    (tmp_path / "gold").mkdir(exist_ok=True)
    (tmp_path / "questions.json").write_text('{"questions": []}', encoding="utf-8")
    path = tmp_path / "suite.yaml"
    path.write_text(text, encoding="utf-8")
    return path


GOOD = """\
format: javmem-regress/1
name: 编的
config: {JAVMEM_TIMEZONE: "+08:00"}
gold:
  - line: a
    env: {JAVMEM_L3_PROMPT: l3-v17}
    packets: [packets/*/*.md]
    store: l3.db
    score: {gold: gold}
retrieval:
  - line: a
    packets: [packets/src/a.md]
    store: l3.db
    questions: questions.json
    caller: someone
graph: {mode: given}
"""


def test_a_good_suite_is_read(tmp_path: Path) -> None:
    suite = load_suite(suite_file(tmp_path, GOOD))
    assert suite.name == "编的" and suite.config == {"JAVMEM_TIMEZONE": "+08:00"}
    assert [one.line for one in suite.gold] == ["a"] and suite.gold[0].packets == (tmp_path / "packets" / "src" / "a.md",)
    assert suite.retrieval[0].caller == "someone" and suite.graph.mode == "given"
    assert suite.baseline == tmp_path / "baseline.json"
    assert load_suite(suite_file(tmp_path, GOOD.replace("graph: {mode: given}", ""))).graph.mode == "off"


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (("format: javmem-regress/1", "format: something"), "format"),
        (("store: l3.db\n    score", "store: missing.db\n    score"), "missing.db 不在"),
        (("packets: [packets/*/*.md]", "packets: [packets/*/*.txt]"), "一个都没找到"),
        (("{JAVMEM_L3_PROMPT: l3-v17}", "{JAVMEM_LLM_API_KEY: x}"), "是密钥"),
        (("{JAVMEM_L3_PROMPT: l3-v17}", "{OTHER: x}"), "不是 JAVMEM_ 开头"),
        (("caller: someone", "caller: ''"), "身份"),
        (("graph: {mode: given}", "graph: {mode: sometimes}"), "graph.mode"),
    ],
)
def test_bad_suites_say_what_is_wrong(tmp_path: Path, change: tuple[str, str], says: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_suite(suite_file(tmp_path, GOOD.replace(*change)))
    assert says in str(caught.value)


def test_a_given_graph_takes_one_line_only(tmp_path: Path) -> None:
    two = GOOD.replace("graph: {mode: given}", """\
  - line: b
    packets: [packets/src/a.md]
    store: l3.db
    questions: questions.json
    caller: someone
graph: {mode: given}""")
    with pytest.raises(ConfigError, match="一遍只接一条线"):
        load_suite(suite_file(tmp_path, two))


# ---------- 证据命中率（照 J2.15） ----------


def item(key: str, kind: str, text: str, *ids: str) -> evidence.Item:
    return evidence.Item(key, kind, text, ids)


def test_fill_keeps_to_the_budget_and_skips_covered_sentences() -> None:
    items = [item("F1", "fact", "a" * 30, "s1"), item("s1", "raw", "b" * 30, "s1"), item("s2", "raw", "c" * 30, "s2"),
             item("F2", "fact", "d" * 100, "s3")]
    placed = evidence.fill(items, 100)
    assert [one.key for one in placed] == ["F1", "s2", "F2"]  # s1 已经是 F1 的证据，不再放
    assert placed[-1].text == "d" * 39 + "…"  # 最后一条放不下、剩 40 个字：截一段放进去就停
    assert [one.key for one in evidence.fill(items, 69)] == ["F1", "s2"]  # 剩不到 40 个字：不截
    usable = [{"s1"}, {"s3"}, {"s9"}]
    assert evidence.hits_at(usable, items) == {"5000": 2, "1500": 2}
    assert evidence.hits_at(usable, [item("F9", "fact", "x" * 2000, "s3"), *items]) == {"5000": 2, "1500": 1}


def test_window_cuts_long_sentences_around_the_word() -> None:
    assert evidence.window("短句", "") == "短句"
    text = "前" * 100 + "关键" + "后" * 300
    cut = evidence.window(text, "关键")  # 从那个词前面 50 个字开始，最多 160 个字（两头加“…”）
    assert cut.startswith("…" + "前" * 50 + "关键") and cut.endswith("…") and len(cut) == 160
    assert evidence.window(text, "").startswith("前") and len(evidence.window(text, "")) == 159
    tail = "前" * 200 + "关键"  # 截到了句末：后面不加“…”
    assert evidence.window(tail, "关键") == "…" + tail[150:]


def test_sources_resolve_to_the_sentences_this_caller_can_see(world: World) -> None:
    from javmem.access import load_identity
    from javmem.config import Settings
    from javmem.ledger import connect_readonly

    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    settings = Settings.from_env({**_env(world), "JAVMEM_CALLER": "desk-bot"})
    conn = connect_readonly(settings.library("prod").db_path)
    try:
        sentences = evidence.Sentences(conn, load_identity(settings))
        ids: dict[str, list[str]] = {}
        for text, sentence_id in conn.execute("SELECT text, sentence_id FROM raw_sentences ORDER BY seq, id"):
            ids.setdefault(text, []).append(sentence_id)
    finally:
        conn.close()
    one = sentences.resolve(f"{CHAT_ID} ｜ - [10:33] 我：先不用，下周再说。")  # 行首的时间、说话人去掉再对
    assert one == ids["先不用，下周再说。"] and len(one) == 1
    twice = sentences.resolve(f"{CHAT_ID} ｜ 越南那批订单还是小李跟。")  # 原话那一行、16:06 那一行的引用里各一次：照 J2.15，两句都算
    assert len(twice) == 2 and twice[0] == ids["越南那批订单还是小李跟。"][0] and twice[1].startswith(f"{CHAT_ID}#3.")
    both = sentences.resolve(f"{CHAT_ID} | 交给老周。 小李去跟新加坡")  # 一行（连同续行）的几句拼起来找，取和原话重叠的
    assert both == [*ids["刚才说的改一下，越南那批从下周一开始交给老周。"], *ids["小李去跟新加坡的单子。"]]
    assert sentences.resolve(f"{CHAT_ID} ｜ 没有这句话") == []
    assert sentences.resolve(f"{CHAT_ID} ｜ 好的") == []  # 去掉空白以后不到 4 个字
    assert sentences.resolve("2026-01-01_0000_other ｜ 越南那批订单还是小李跟") == []  # 看不到的包
    assert sentences.resolve("没写成包号和原话") == []


def _env(world: World) -> dict[str, str]:
    import os

    return {key: value for key, value in os.environ.items() if key.startswith("JAVMEM_")}


# ---------- 和基准比 ----------


def scores(**metrics: float) -> dict[str, object]:
    side = {"relations": {"precision": 0.6, "recall": 0.5}, "changes": {"rate": 0.1}, "aliases": {"rate": 0.3},
            "facts": {"complete_rate": 0.6, "precision": 0.8}}
    for key, value in metrics.items():
        group, name = key.split("__")
        side[group][name] = value
    return {"scores": {"format": "javmem-score-result/1", "gold": {"items": 10}, "sides": {"javmem": side}}}


def test_gold_same_changed_and_dropped() -> None:
    base = scores()
    assert regress.compare_gold(scores(), base)[:2] == ("一样", [])
    assert regress.compare_gold(scores(relations__precision=0.7), base)[:2] == ("变了", [])  # 涨了不算降
    state, dropped, shown = regress.compare_gold(scores(changes__rate=0.05), base)
    assert state == "变了" and dropped == ["变化判对率 10.0% → 5.0%"] and "关系准确率 60.0%" in shown
    other_gold = scores()
    other_gold["scores"]["gold"] = {"items": 11}  # type: ignore[index]
    assert "金标准本身变了" in regress.compare_gold(other_gold, base)[1][0]
    assert regress.compare_gold(scores(), None)[0] == "没有基准"


def test_described_paths_and_times_are_not_compared() -> None:
    raw = {"format": "f", "made_at": "今天", "gold": {"g": 1}, "sides": {"javmem": {"described": {"账本": "某处"}, "facts": {}}}}
    other = json.loads(json.dumps(raw))
    other["made_at"], other["sides"]["javmem"]["described"] = "明天", {"账本": "别处"}
    assert regress.comparable_scores(raw) == regress.comparable_scores(other)


def totals(graph: tuple[int, int] = (45, 35), ledger: tuple[int, int] = (27, 21), resolved: int = 75) -> dict[str, object]:
    return {"routes": ["graph", "ledger"], "resolved": resolved,
            "totals": {"graph": {"5000": graph[0], "1500": graph[1]}, "ledger": {"5000": ledger[0], "1500": ledger[1]}}}


def test_retrieval_same_changed_and_dropped() -> None:
    base = totals()
    assert regress.compare_retrieval(totals(), base)[:2] == ("一样", [])
    assert regress.compare_retrieval(totals(graph=(46, 35)), base)[:2] == ("变了", [])
    state, dropped, text = regress.compare_retrieval(totals(ledger=(27, 20), resolved=74), base)
    assert dropped == ["账本视图 1500 字 21 → 20", "对上原句的出处 75 → 74"]
    assert "主图 45 / 35（基准 45 / 35）" in text
    no_graph = {**totals(), "routes": ["ledger"]}  # --no-graph：主图那一路不比
    assert regress.compare_retrieval(no_graph, base)[:2] == ("一样", [])


def test_exact_turns_a_change_into_a_fail(tmp_path: Path) -> None:
    suite = load_suite(suite_file(tmp_path, GOOD))
    result = regress.Result(gold={"a": scores(relations__precision=0.7)})
    baseline = {"gold": {"a": scores()}}
    lines: list[str] = []
    assert regress.report(suite, result, baseline, exact=False, graph_mode="off", log=lines.append) == 0
    assert regress.report(suite, result, baseline, exact=True, graph_mode="off", log=lines.append) == 1


def test_paid_rows_in_any_ledger_fail_the_run(tmp_path: Path) -> None:
    TokenLedger(tmp_path / "a" / "token-ledger.csv").append({"model": "Qwen/Qwen3-Embedding-4B", "purpose": "embedding"})
    TokenLedger(tmp_path / "b" / "token-ledger.csv").append({"model": "fake-model@fake/json_schema"})
    assert regress.count_ledgers(tmp_path)[:2] == (0, 2)
    TokenLedger(tmp_path / "b" / "token-ledger.csv").append({"model": "vendor-model@auto/json_schema"})
    assert regress.count_ledgers(tmp_path)[:2] == (1, 2)
    suite = load_suite(suite_file(tmp_path / "s", GOOD))
    result = regress.Result(paid_rows=1)
    assert regress.report(suite, result, None, exact=False, graph_mode="off", log=lambda line: None) == 1


def test_children_never_get_the_paid_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVMEM_LLM_API_KEY", "sk-not-real")
    monkeypatch.setenv("JAVMEM_SOFT_API_KEY", "sk-not-real")
    monkeypatch.setenv("JAVMEM_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("JAVMEM_DATA_DIR", "somewhere-else")
    suite = load_suite(suite_file(tmp_path, GOOD))
    env = regress.child_env(suite, {"JAVMEM_L3_PROMPT": "l3-v17"}, tmp_path / "data", tmp_path / "ledger.csv")
    assert "JAVMEM_LLM_API_KEY" not in env and "JAVMEM_SOFT_API_KEY" not in env
    assert env["JAVMEM_EMBEDDING_BASE_URL"] == "http://127.0.0.1:9/v1"  # 本机的向量服务接着给
    assert env["JAVMEM_DATA_DIR"] == str(tmp_path / "data") and env["JAVMEM_L3_PROMPT"] == "l3-v17"


def test_the_work_dir_must_be_new_and_outside_the_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from javmem.cli import main

    path = suite_file(tmp_path, GOOD)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x", encoding="utf-8")
    assert main(["regress", "--suite", str(path), "--work", str(tmp_path / "busy")]) == 2
    assert "不是空的" in capsys.readouterr().out
    repo = Path(__file__).resolve().parents[1]
    assert main(["regress", "--suite", str(path), "--work", str(repo / "src" / "work")]) == 2
    assert not (repo / "src" / "work").exists()
    assert main(["regress", "--suite", str(path), "--save-baseline"]) == 2  # 换基准要写为什么


# ---------- 测试图的保险（不起 docker） ----------


def test_the_test_graph_lock(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "7689.txt"
    one = regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "graph", "c", lock=lock, who="甲")
    one.take_lock(10)
    assert "会话：甲" in lock.read_text(encoding="utf-8")
    other = regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "graph", "c", lock=lock, who="乙")
    with pytest.raises(regress_graph.GraphBusy):
        other.take_lock(10)
    other.drop_lock()  # 不是自己建的，不删
    assert lock.exists()
    one.drop_lock()
    assert not lock.exists()


def test_never_wipes_a_directory_that_is_not_its_own(tmp_path: Path) -> None:
    graph = regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "big", "c")
    (tmp_path / "big" / "data").mkdir(parents=True)  # 别人的图（没有回归的标记）
    with pytest.raises(regress_graph.GraphRefused, match="不是回归自己的目录"):
        graph._check_data_dir()
    (tmp_path / "big" / regress_graph.MARKER).write_text("x", encoding="utf-8")
    graph._check_data_dir()
    graph.original = tmp_path / "big"
    with pytest.raises(regress_graph.GraphRefused, match="同一个"):
        graph._check_data_dir()
    regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "new", "c")._check_data_dir()  # 还没有的：可以


def test_docker_desktop_mount_paths() -> None:
    assert regress_graph._same_dir("/run/desktop/mnt/host/q/demo/regress/graph/data",
                                   Path("Q:/demo/regress/graph/data"))
    assert not regress_graph._same_dir("/run/desktop/mnt/host/q/demo/graph-test-neo4j/data",
                                       Path("Q:/demo/regress/graph/data"))
    assert regress_graph._host_dir("/run/desktop/mnt/host/q/x/y/data") == Path("Q:/x/y/data")
    assert regress_graph._same_dir("/home/runner/graph/data/", Path("/home/runner/graph/data"))


# ---------- 录好的回复 ----------


def test_replies_round_trip(tmp_path: Path) -> None:
    """结果库 → JSON → 新的结果库：每张表每一行都一样（编的小套件靠它在 CI 上不调模型）。"""
    import sqlite3

    from javmem.llm import Usage
    from javmem.pipeline.store import ResultStore
    from javmem.regress.replies import build_store, dump_store

    with ResultStore(tmp_path / "l3") as store:
        store.save_result(cache_key="k1", window_sha="s1", prompt_version="l3-v20", model="m@auto/json_schema",
                          output='{"facts": []}', parsed={"facts": []}, response_model="m-2026-11-01", usage=Usage(10, 5),
                          seconds=1.5, created_at="2026-11-19T10:00:00+08:00")
    counts = dump_store(tmp_path / "l3", tmp_path / "replies.json", note="编的")
    assert counts["results"] == 1
    assert build_store(tmp_path / "replies.json", tmp_path / "again") == counts

    def table(path: Path) -> list[tuple[object, ...]]:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT * FROM results").fetchall()
        finally:
            conn.close()

    assert table(tmp_path / "again" / "l3.db") == table(tmp_path / "l3" / "l3.db")
    with pytest.raises(ValueError, match="已经有了"):
        build_store(tmp_path / "replies.json", tmp_path / "again")
    data = json.loads((tmp_path / "replies.json").read_text(encoding="utf-8"))
    data["tables"]["results"]["columns"][0] = "no_such_column"
    (tmp_path / "old.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="要重录"):
        build_store(tmp_path / "old.json", tmp_path / "third")


def test_replies_dump_from_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """重录以后照开发说明导出：python -m javmem.regress.replies dump <结果库> <输出.json> --note …。"""
    from javmem.llm import Usage
    from javmem.pipeline.store import ResultStore
    from javmem.regress import replies

    with ResultStore(tmp_path / "l3") as store:
        store.save_result(cache_key="k1", window_sha="s1", prompt_version="l3-v20", model="m@auto/json_schema",
                          output='{"facts": []}', parsed={"facts": []}, response_model="m-2026-11-01", usage=Usage(10, 5),
                          seconds=1.5, created_at="2026-11-19T10:00:00+08:00")
    assert replies.main(["dump", str(tmp_path / "l3"), str(tmp_path / "out" / "replies.json"), "--note", "编的一遍"]) == 0
    assert "results 1 行" in capsys.readouterr().out
    data = json.loads((tmp_path / "out" / "replies.json").read_text(encoding="utf-8"))
    assert data["format"] == replies.FORMAT and data["note"] == "编的一遍"
    assert len(data["tables"]["results"]["rows"]) == 1
    with pytest.raises(SystemExit):
        replies.main(["load", str(tmp_path / "l3"), str(tmp_path / "x.json")])  # 只有 dump：建回是 regress 自己做的


def test_never_stops_a_test_graph_someone_is_using(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """容器正开着、挂的不是回归自己的目录（有人在用，或者忘了停）：不去停它，一个 docker 命令都不跑。"""
    graph = regress_graph.ManagedGraph(Path("compose.yaml"), tmp_path / "graph", "c")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(graph, "_run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(graph, "mounts", lambda: ["/run/desktop/mnt/host/q/someone/big/data",
                                                  "/run/desktop/mnt/host/q/someone/big/logs"])
    monkeypatch.setattr(graph, "running", lambda: True)
    with pytest.raises(regress_graph.GraphRefused, match="正开着"):
        graph.up()
    assert calls == []
    assert graph.original == Path("Q:/someone/big")
