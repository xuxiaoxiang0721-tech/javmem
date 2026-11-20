"""删除演练（U2 乙，D205 ③④，做法第 2 节）：`javmem drill delete` 在编的小世界上跑一遍，撤回、彻底删除以后逐层查、
逐字节搜都是 0；故意不清一样（向量缓存、结果库、归档、支线库），演练要报“没过”，报在那一层。

- 小世界一：借 GitHub 那一层回归的编的小套件（tests/fixtures/regress-mini/：5 个编的包、录好的模型回复，有实体、别名、关系），
  照它建结果库、写账（假向量），不调模型。
- 小世界二：两个编的包，后一包接着前一包（`continues`），后一包的第一个窗口拿前一包当上文；假模型抽（`extract --fake`）。
- 有测试用的 Neo4j（CI 的 ubuntu 那一路；本机借 7688 的也算）再跑一遍接图的（`--graph given`）；没有的跳过。
名字、句子、号码都是编的。

GitHub 的 Windows 那一路整个跳过（U2 甲定的“Windows 那一路一分钟不加”）：ubuntu 那一路、本机（Windows）照跑。
"""

from __future__ import annotations

import hashlib
import os
import json
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_graph_neo4j import PASSWORD, URI, neo_ready, real  # noqa: F401 - 接图那一条借这两个 fixture 清空那个图

from javmem.cli import main
from javmem.drill.common import LOCK_NAMES, SIDECARS
from javmem.regress.replies import build_store

MINI = Path(__file__).resolve().parent / "fixtures" / "regress-mini"
GRAPH = bool(URI and PASSWORD)

pytestmark = pytest.mark.skipif(sys.platform == "win32" and os.environ.get("GITHUB_ACTIONS") == "true",
                                reason="GitHub 的 Windows 那一路不加时长（ubuntu 那一路、本机照跑）")
FIRST = "2026-10-20_2300_demo-bot"  # 小世界一里“欧阳青”（表姐）只有这一包说过；别的名字别的包里都有

BASE_ENV = {
    "JAVMEM_SOURCES_FILE": str(MINI / "config" / "sources.yaml"),
    "JAVMEM_ACCESS_FILE": str(MINI / "config" / "access.yaml"),
    "JAVMEM_OWNER_FILE": str(MINI / "config" / "owner.yaml"),
    "JAVMEM_LEXICON_FILE": str(MINI / "config" / "lexicon.yaml"),
    "JAVMEM_TIMEZONE": "+08:00",
    "JAVMEM_L3_PROMPT": "l3-v20",
    "JAVMEM_LLM_MODEL": "gpt-6.1-sol",
}


def _env(monkeypatch: pytest.MonkeyPatch, data: Path, ledger: Path) -> None:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    monkeypatch.setenv("JAVMEM_TOKEN_LEDGER", str(ledger))


@pytest.fixture(scope="module")
def mini(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """小世界一的数据目录（建一次，各个测试只拿它当 --from：演练一个字不改它）。"""
    root = tmp_path_factory.mktemp("mini")
    data = root / "data"
    with pytest.MonkeyPatch.context() as patch:
        for name in [name for name in os.environ if name.startswith("JAVMEM_")]:
            patch.delenv(name)
        _env(patch, data, root / "token-ledger.csv")
        assert main(["init"]) == 0
        inbox = data / "inbox-test" / "demo-bot"
        inbox.mkdir(parents=True, exist_ok=True)
        for packet in sorted((MINI / "packets" / "demo-bot").glob("*.md")):
            shutil.copy2(packet, inbox / packet.name)
        assert main(["intake"]) == 0
        build_store(MINI / "l3-replies.json", data / "l3")
        assert main(["write", "--library", "test", "--fake-embeddings"]) == 0
    yield data


def _digest(root: Path) -> dict[str, str]:
    """一个目录里每个文件的 sha256（锁文件、-wal、-shm 不算：SQLite 读的时候也会建、会动这几个）。"""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in LOCK_NAMES and not path.name.endswith(SIDECARS):
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _drill(tmp_path: Path, source: Path, *extra: str, name: str = "drill") -> tuple[int, dict[str, object], Path]:
    target = tmp_path / name
    code = main(["drill", "delete", "--from", str(source), "--to", str(target), "--library", "test", *extra])
    result_file = target / "drill-delete.json"
    result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
    return code, result, target


def _bad(result: dict[str, object], stage: str) -> dict[str, int]:
    stages = result["stages"]  # type: ignore[index]
    return {name: layer["bad"] for name, layer in stages[stage].items()}  # type: ignore[index]


@pytest.fixture
def drill_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """跑演练的这个进程的环境：本机配置照小世界的；数据目录指到别处（演练给子命令的是拷贝）。"""
    _env(monkeypatch, tmp_path / "unused", tmp_path / "unused-ledger.csv")


def test_the_drill_passes_on_the_made_up_world_and_never_touches_the_source(
        mini: Path, tmp_path: Path, drill_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    before = _digest(mini)
    code, result, target = _drill(tmp_path, mini, "--packet", FIRST, "--purge")
    out = capsys.readouterr().out
    assert code == 0, out
    assert result["verdict"] == "过"
    assert "删除演练：过" in out.splitlines()[-1]
    assert _digest(mini) == before  # 原来那个数据目录一个字没改
    counts = result["before"]
    assert counts["sample_sentences"] >= 3 and counts["names_only_it"] >= 1  # type: ignore[index]
    assert counts["facts_only_it"] >= 3 and counts["relations_only_it"] >= 1  # type: ignore[index]
    assert sum(counts["queries"]["leaks"].values()) > 0  # type: ignore[index]  # 撤回以前问得到
    assert all(value == 0 for value in _bad(result, "withdraw").values())
    assert all(value == 0 for value in _bad(result, "purge").values())
    purge = result["stages"]["purge"]  # type: ignore[index]
    assert purge["raw"]["counts"]["raw_sentences"] == 0 and purge["bytes"]["counts"]["sqlite_bytes"] == 0
    assert result["token_rows"] == {"paid": 0, "local": 0}
    # 结果文件、屏幕上都只有编号和数：样本句、名字一个都不写
    text = (target / "drill-delete.json").read_text(encoding="utf-8") + out + (target / "commands.log").read_text(encoding="utf-8")
    for secret in ("欧阳青", "表姐", "改到 11 月 28 日", "净值是 1.032"):
        assert secret not in text
    # 撤回那一遍：空页里还剩的照实列出来，不算没过
    assert result["stages"]["withdraw"]["bytes"]["counts"]["sqlite_bytes"] > 0  # type: ignore[index]


def test_without_purge_the_raw_layer_is_only_marked(mini: Path, tmp_path: Path, drill_env: None) -> None:
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST)
    assert code == 0
    assert "purge" not in result["stages"]  # type: ignore[operator]
    raw = result["stages"]["withdraw"]["raw"]["counts"]  # type: ignore[index]
    assert raw["archives_kept"] == 1 and raw["raw_sentences_kept"] > 0 and raw["packets_not_withdrawn"] == 0


def test_a_vector_cache_left_behind_is_caught(mini: Path, tmp_path: Path, drill_env: None, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """做法第 2 节验收：故意不清一样（不删向量缓存），演练要报“没过：向量 N 处”。"""
    import javmem.withdraw as withdraw_module

    monkeypatch.setattr(withdraw_module, "_clean_vectors", lambda settings, vectors_dir, vacuum=False: 0)
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST, "--purge")
    out = capsys.readouterr().out
    assert code == 1
    assert result["verdict"].startswith("没过：向量")  # type: ignore[union-attr]
    assert _bad(result, "withdraw")["vectors"] > 0 and _bad(result, "purge")["vectors"] > 0
    assert "删除演练：没过：向量" in out


def test_store_windows_left_behind_are_caught(mini: Path, tmp_path: Path, drill_env: None,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from javmem.pipeline.store import ResultStore

    monkeypatch.setattr(ResultStore, "delete_windows", lambda self, doomed: 0)
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST)
    assert code == 1
    bad = _bad(result, "withdraw")
    assert bad["store"] > 0 and bad["bytes"] > 0  # 结果库的表里还有样本句：逐字节搜那一层也报
    assert "结果库" in result["verdict"]  # type: ignore[operator]


def test_archives_left_behind_after_purge_are_caught(mini: Path, tmp_path: Path, drill_env: None,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.withdraw as withdraw_module

    monkeypatch.setattr(withdraw_module, "_delete_archives", lambda *args, **kwargs: [])
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST, "--purge")
    assert code == 1
    assert _bad(result, "withdraw")["raw"] == 0  # 撤回那一遍归档本来就留着
    purge = _bad(result, "purge")
    assert purge["raw"] > 0 and purge["bytes"] > 0


def test_a_withdraw_that_forgets_the_ledger_is_caught(mini: Path, tmp_path: Path, drill_env: None,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """撤回只标了包登记、账本里派生的一样没删：查询接口、账本、向量都要报。撤回清账本有两道（按包删说法
    remove_revision；结果库那一步按窗口再删一遍 forget_windows），两道都换掉才漏得下来。"""
    from javmem.pipeline.write import LedgerWriter

    monkeypatch.setattr(LedgerWriter, "remove_revision", lambda self, packet_id, revision: set())
    monkeypatch.setattr(LedgerWriter, "forget_windows", lambda self, *args, **kwargs: (0, 0))
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST)
    assert code == 1
    bad = _bad(result, "withdraw")
    assert bad["ledger"] > 0 and bad["query"] > 0


def test_the_pick_options(mini: Path, tmp_path: Path, drill_env: None) -> None:
    code, result, _ = _drill(tmp_path, mini, name="most")
    assert code == 0 and result["picked"]["by"] == "most"  # type: ignore[index]
    code, result, _ = _drill(tmp_path, mini, "--pick", "random", "--seed", "7", name="random")
    assert code == 0 and result["picked"] == {"by": "random", "seed": 7, "candidates": 5}
    first = result["before"]["packet"]  # type: ignore[index]
    code, again, _ = _drill(tmp_path, mini, "--pick", "random", "--seed", "7", name="random-again")
    assert again["before"]["packet"] == first  # type: ignore[index]  # 同一个种子挑同一个
    code, result, _ = _drill(tmp_path, mini, "--pick", "month", name="month")
    assert code == 0 and result["picked"]["by"] == "month" and result["picked"]["candidates"] == 5  # type: ignore[index]


@pytest.mark.parametrize("bad_args", [
    ["--packet", "2026-01-01_0000_nobody"],
    ["--packet", FIRST, "--pick", "most"],
    ["--seed", "3"],
    ["--graph", "given"],  # 没写明那个空图的地址
    ["--graph", "managed"],  # 没写 --graph-data
    ["--graph-data", "x"],
])
def test_bad_arguments_cannot_run(mini: Path, tmp_path: Path, drill_env: None, bad_args: list[str]) -> None:
    code, _, _ = _drill(tmp_path, mini, *bad_args)
    assert code == 2


def test_the_target_must_be_new_and_apart(mini: Path, tmp_path: Path, drill_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x.txt").write_text("x", encoding="utf-8")
    assert main(["drill", "delete", "--from", str(mini), "--to", str(busy), "--library", "test"]) == 2
    assert main(["drill", "delete", "--from", str(mini), "--to", str(mini / "inside"), "--library", "test"]) == 2
    assert not (mini / "inside").exists()
    assert main(["drill", "delete", "--from", str(tmp_path / "nothing"), "--to", str(tmp_path / "t"), "--library", "test"]) == 2
    assert "跑不了" in capsys.readouterr().out


def test_mail_packets_are_drilled_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """邮件包也演（U3）：U2 乙那时候先不演，怕撤回一封信以后同一往来串里别的包写过的说法清不掉；J5.6 以后撤回时
    就清掉了（tests/test_withdraw_thread.py）。演一个邮件包、连彻底删除，每一层都要过。"""
    from helpers import MAIL_ID
    from query_world import build_world

    world = build_world(tmp_path / "world")
    monkeypatch.setenv("JAVMEM_SOURCES_FILE", str(tmp_path / "world" / "sources.yaml"))
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(tmp_path / "world" / "lexicon.yaml"))
    code = main(["drill", "delete", "--from", str(world.data), "--to", str(tmp_path / "drill"), "--library", "prod",
                 "--packet", MAIL_ID, "--purge"])
    result = json.loads((tmp_path / "drill" / "drill-delete.json").read_text(encoding="utf-8"))
    assert code == 0, result
    assert result["verdict"] == "过"
    assert result["before"]["sample_sentences"] >= 1  # type: ignore[index]  # 真演了：撤回以前问得到它的样本句
    assert all(value == 0 for value in _bad(result, "withdraw").values())
    assert all(value == 0 for value in _bad(result, "purge").values())


def test_busy_source_waits_then_gives_up(mini: Path, tmp_path: Path, drill_env: None) -> None:
    from javmem.intake import intake_lock

    with intake_lock(mini):  # 一个收件正在跑
        code, result, _ = _drill(tmp_path, mini, "--packet", FIRST, "--wait", "0")
    assert code == 2 and "正忙" in result["cannot"]  # type: ignore[operator]


# ---------- 小世界二：后一包拿前一包当上文 ----------

CONT_A = "2026-10-01_2300_demo-bot"
CONT_B = "2026-10-02_2300_demo-bot"


def _chat(packet_id: str, day: str, lines: list[tuple[str, str]], *, continues: str | None = None) -> str:
    head = [
        "---", "schema: javmem-packet/1", f"packet_id: {packet_id}", f"created_at: {day}T23:00:00+08:00", "source: demo-bot",
        "framework: demo-chat", "channel: chat", "env: test", "kind: daily", f"covers_from: {day}T00:00:00+08:00",
        f"covers_to: {day}T23:00:00+08:00", "domain: [business]", "privacy: normal", "sensitive: []", "entities: []",
    ]
    if continues:
        head.append(f"continues: {continues}")
    body = "\n".join(f"- [{at}] 我：{text}" for at, text in lines)
    return "\n".join(head) + f"\n---\n\n## 原话 · {lines[0][0]}–{lines[-1][0]}\n{body}\n"


@pytest.fixture(scope="module")
def chained(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("chained")
    data = root / "data"
    with pytest.MonkeyPatch.context() as patch:
        for name in [name for name in os.environ if name.startswith("JAVMEM_")]:
            patch.delenv(name)
        _env(patch, data, root / "token-ledger.csv")
        patch.setenv("JAVMEM_L3_PROMPT", "l3-v17")
        assert main(["init"]) == 0
        inbox = data / "inbox-test" / "demo-bot"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{CONT_A}.md").write_text(_chat(CONT_A, "2026-10-01", [
            ("21:10", "锦屏路那间仓库的租约签了三年，月租 18,600 元。"),
            ("21:12", "仓库钥匙一共 3 把，备用那把放在前台抽屉里。"),
            ("21:15", "下周二上午 10 点去看第二间备选仓库，在望江南路。"),
        ]), encoding="utf-8")
        (inbox / f"{CONT_B}.md").write_text(_chat(CONT_B, "2026-10-02", [
            ("08:05", "接着昨天说的，备选仓库的报价是月租 21,000 元。"),
            ("08:07", "两间都看完以后 10 月 9 日前定下来。"),
        ], continues=CONT_A), encoding="utf-8")
        assert main(["intake"]) == 0
        assert main(["extract", "--library", "test", "--fake"]) == 0
        assert main(["write", "--library", "test", "--fake", "--fake-embeddings"]) == 0
    yield data


def test_windows_that_used_the_packet_as_context_are_cleared(chained: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             drill_env: None) -> None:
    monkeypatch.setenv("JAVMEM_L3_PROMPT", "l3-v17")
    code, result, _ = _drill(tmp_path, chained, "--purge")
    assert code == 0, result
    assert result["before"]["packet"] == CONT_A and result["picked"]["context_windows"] >= 1  # type: ignore[index]
    assert result["before"]["context_windows"] >= 1 and result["before"]["context_claims"] >= 1  # type: ignore[index]
    assert result["withdraw"]["requeued"] == [CONT_B]  # type: ignore[index]
    assert result["verdict"] == "过"


# ---------- 支线库：跑过支线的库，体检库那一层 ----------


def _side_db(data: Path, packet: str) -> Path:
    """照支线库的表造一组：这个包的一个窗口、每一句原文一行（跑过支线的库就是这样）。"""
    from javmem.side.store import SideStore

    with SideStore.open(data, "test"):
        pass
    path = data / "side" / "test" / "side.db"
    ledger = sqlite3.connect(data / "ledger" / "test.db")
    raw = ledger.execute("SELECT sentence_id, text FROM raw_sentences WHERE packet_id = ? ORDER BY id", (packet,)).fetchall()
    ledger.close()
    conn = sqlite3.connect(path)
    group, window = f"{packet}_r1", f"{packet}_r1#1"
    conn.execute("INSERT INTO groups (group_id, packet_id, revision, partitions, sensitive, state, started_at)"
                 " VALUES (?, ?, 1, '[\"business\"]', '[]', 'done', '2026-11-01T00:00:00+08:00')", (group, packet))
    conn.execute("INSERT INTO windows (window_id, group_id, number, status, reference_time) VALUES (?, ?, 1, 'done', ?)",
                 (window, group, "2026-10-05T23:00:00+08:00"))
    for seq, (sentence_id, text) in enumerate(raw, start=1):
        conn.execute("INSERT INTO lines (window_id, seq, ref, sentence_id, block, text) VALUES (?, ?, ?, ?, 0, ?)",
                     (window, seq, f"T{seq}", sentence_id, text))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def mini_with_side(mini: Path, tmp_path: Path) -> Path:
    source = tmp_path / "with-side"
    shutil.copytree(mini, source, ignore=shutil.ignore_patterns(*LOCK_NAMES))
    _side_db(source, FIRST)
    return source


def test_the_side_store_layer_is_checked(mini_with_side: Path, tmp_path: Path, drill_env: None) -> None:
    code, result, _ = _drill(tmp_path, mini_with_side, "--packet", FIRST, "--purge")
    assert code == 0, result
    side = result["stages"]["withdraw"]["side"]  # type: ignore[index]
    assert side["bad"] == 0 and side["counts"]["groups_before"] == 1 and side["counts"]["pending_clears"] == 1


def test_a_side_store_left_behind_is_caught(mini_with_side: Path, tmp_path: Path, drill_env: None,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import javmem.side.hook as side_hook

    monkeypatch.setattr(side_hook, "after_withdraw", lambda *args, **kwargs: None)
    code, result, _ = _drill(tmp_path, mini_with_side, "--packet", FIRST, "--purge")
    assert code == 1
    assert _bad(result, "withdraw")["side"] > 0 and _bad(result, "purge")["bytes"] > 0


# ---------- 接着主图的库：关系的向量撤回以后要一条不少 ----------


@pytest.fixture
def mini_on_graph(mini: Path, tmp_path: Path) -> Path:
    """小世界一的一份：账本记着接过主图（graph_state.enabled），向量缓存里有每条关系拿来算向量的那一句（像整张同步过的）。"""
    from datetime import timedelta, timezone

    from javmem.embedding import EMBEDDING_DIM
    from javmem.graph.project import Projector
    from javmem.ledger import connect_readonly
    from javmem.pipeline.vectors import VectorCache

    source = tmp_path / "on-graph"
    shutil.copytree(mini, source, ignore=shutil.ignore_patterns(*LOCK_NAMES))
    conn = sqlite3.connect(source / "ledger" / "test.db")
    conn.execute("UPDATE graph_state SET enabled = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    conn = connect_readonly(source / "ledger" / "test.db")
    projector = Projector(conn, timezone(timedelta(hours=8)))
    texts = {item.vector_text for item in projector.relations(projector.all_ids("relation")).values() if item.vector_text}
    conn.close()
    assert texts
    with VectorCache(source / "vectors") as cache:
        cache.put("fake-embedding", {text: bytes(EMBEDDING_DIM * 4) for text in texts})
    return source


def test_relation_vectors_survive_on_a_ledger_on_the_graph(mini_on_graph: Path, tmp_path: Path, drill_env: None) -> None:
    code, result, _ = _drill(tmp_path, mini_on_graph, "--packet", FIRST, "--purge")
    assert code == 0, result
    assert result["before"]["relation_vectors"] >= 3  # type: ignore[index]
    vectors = result["stages"]["purge"]["vectors"]  # type: ignore[index]
    assert vectors["counts"]["relation_vectors"] >= 3 and vectors["counts"]["relation_vectors_lost"] == 0


def test_relation_vectors_lost_are_caught(mini_on_graph: Path, tmp_path: Path, drill_env: None,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """撤回清向量缓存时把还在的关系的向量也删了（J5.6 以前那样）：演练要报“向量”。"""
    import javmem.withdraw as withdraw_module

    monkeypatch.setattr(withdraw_module, "_relation_texts", lambda settings: set())
    code, result, _ = _drill(tmp_path, mini_on_graph, "--packet", FIRST)
    assert code == 1
    assert result["stages"]["withdraw"]["vectors"]["counts"]["relation_vectors_lost"] > 0  # type: ignore[index]


# ---------- 接图：有测试用的 Neo4j 才跑（CI 的 ubuntu 那一路；本机借 7688 的也跑） ----------


@pytest.mark.skipif(not GRAPH, reason="没给测试用的 Neo4j（JAVMEM_TEST_NEO4J_URI、_PASSWORD）")
@pytest.mark.skipif(URI.rstrip("/").endswith(":7689"), reason="借的是本机共用的测试图口 7689：演练的 given 不接它（要用它走 managed）")
def test_the_drill_with_the_graph(mini: Path, tmp_path: Path, drill_env: None, monkeypatch: pytest.MonkeyPatch,
                                  real: object) -> None:  # noqa: F811
    """借的图正好在主图默认的口上（本机照“两个图怎么轮着用”借 7688）：演练给子命令的环境里不给主图的设置，子命令的主图
    就是默认的 7688，和借来当测试图的同一个口，设置那一层先拦（D37），一步都演不了。所以这时给子命令另设一个没人用的
    主图地址（和 test_graph_neo4j.py 的 env 一样用 7699）：子命令只演测试库、没有主图的密码，主图一次都不连，这个地址
    只拿来比口。CI 的测试图在 7687，不换，照原样跑。演练本身不改。"""
    from urllib.parse import urlsplit

    import javmem.drill.delete as delete_module
    from javmem.graph.config import DEFAULT_URI

    if urlsplit(URI).port == urlsplit(DEFAULT_URI).port:
        graph_env = delete_module._graph_env
        monkeypatch.setattr(delete_module, "_graph_env",
                            lambda plan, base: {**graph_env(plan, base), "JAVMEM_GRAPH_NEO4J_URI": "bolt://127.0.0.1:7699"})
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_URI", URI)
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_PASSWORD", PASSWORD)
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST, "--purge", "--graph", "given")
    assert code == 0, result
    graph = result["stages"]["withdraw"]["graph"]  # type: ignore[index]
    assert graph["bad"] == 0 and graph["counts"]["before"]["ids"] > 0
    assert result["withdraw"]["graph"] == "ok"  # type: ignore[index]


# ---------- 审阅查出来的（11-19，先写测试看它红，再改） ----------


@pytest.fixture
def original_graph(mini: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, object]:
    """小世界一的一份接上“原来那个图”（内存图顶替 Neo4j）、整张同步过：演练的 --from。返回 (数据目录, 那个图)。"""
    from javmem.graph import neo
    from javmem.graph.memory import MemoryGraph

    source = tmp_path / "attached"
    shutil.copytree(mini, source, ignore=shutil.ignore_patterns(*LOCK_NAMES))
    graph = MemoryGraph()
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: graph)
    monkeypatch.setattr(neo, "set_initial_password", lambda target, timeout=None: False)
    with pytest.MonkeyPatch.context() as patch:
        _env(patch, source, tmp_path / "attach-ledger.csv")
        patch.setenv("JAVMEM_GRAPH_TEST_NEO4J_PASSWORD", "made-up-pass-for-the-test")
        assert main(["graph", "init", "--library", "test"]) == 0
        assert main(["graph", "sync", "--library", "test", "--fake-embeddings"]) == 0
    return source, graph


def _facts_in(graph: object, packet: str, source: Path) -> int:
    conn = sqlite3.connect(source / "ledger" / "test.db")
    ids = {row[0] for row in conn.execute("SELECT DISTINCT fact_id FROM evidence WHERE packet_id = ? AND fact_id IS NOT NULL",
                                          (packet,))}
    conn.close()
    return sum(1 for item in graph.iter_items("fact", vectors=False) if item["id"] in ids)  # type: ignore[attr-defined]


def test_a_given_graph_that_is_not_empty_is_refused(original_graph: tuple[Path, object], tmp_path: Path, drill_env: None,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 1 条：--graph given 指到 --from 那份账本接着的图（标记一样），graph init --reset 只核一遍，撤回就删到原来那个图里。
    接图以前先核那个图是空的：不空就不演，原来那个图一个字不动。"""
    source, graph = original_graph
    before = _facts_in(graph, FIRST, source)
    assert before > 0
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_URI", "bolt://127.0.0.1:17999")
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_PASSWORD", "made-up-pass-for-the-test")
    code, result, _ = _drill(tmp_path, source, "--packet", FIRST, "--purge", "--graph", "given")
    assert code == 2 and "不是空的" in result["cannot"]  # type: ignore[operator]
    assert _facts_in(graph, FIRST, source) == before


@pytest.mark.parametrize("uri", ["bolt://localhost:7689", "bolt://[::1]:7689", "bolt://127.0.0.1:07689", "bolt://127.0.0.2:7689/"])
def test_other_spellings_of_the_shared_test_port_are_refused(mini: Path, tmp_path: Path, drill_env: None,
                                                             monkeypatch: pytest.MonkeyPatch, uri: str) -> None:
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_URI", uri)
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_PASSWORD", "made-up-pass-for-the-test")
    code, _, target = _drill(tmp_path, mini, "--packet", FIRST, "--graph", "given")
    assert code == 2 and not (target / "data").exists()  # 一开头就拒，连拷都不拷


def test_vectors_of_deleted_relations_left_in_the_cache_are_caught(mini_on_graph: Path, tmp_path: Path, drill_env: None,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 2 条：清向量缓存时把只出自它、已经删了的关系的那一句也留下了（多留了），演练要报“向量”。"""
    from datetime import timedelta, timezone

    import javmem.withdraw as withdraw_module
    from javmem.graph.project import Projector
    from javmem.ledger import connect_readonly

    conn = connect_readonly(mini_on_graph / "ledger" / "test.db")
    projector = Projector(conn, timezone(timedelta(hours=8)))
    before = {item.vector_text for item in projector.relations(projector.all_ids("relation")).values() if item.vector_text}
    conn.close()
    monkeypatch.setattr(withdraw_module, "_relation_texts", lambda settings: set(before))
    code, result, _ = _drill(tmp_path, mini_on_graph, "--packet", FIRST, "--purge")
    assert code == 1
    for stage in ("withdraw", "purge"):
        assert result["stages"][stage]["vectors"]["counts"]["deleted_relation_vectors_cached"] > 0  # type: ignore[index]


def test_a_text_index_left_behind_after_purge_is_caught(mini: Path, tmp_path: Path, drill_env: None,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 3 条：彻底删除删了原文句、全文索引没跟着删（触发器没了），逐字节、逐表都搜不出：要核全文索引对得上原文表。"""
    import javmem.cli as cli_module

    real_purge = cli_module.purge

    def purge_without_triggers(settings, library_name, packet_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
        conn = sqlite3.connect(settings.library(library_name).db_path)
        conn.execute("DROP TRIGGER IF EXISTS raw_sentences_after_delete")
        conn.execute("DROP TRIGGER IF EXISTS raw_sentences_norm_after_delete")
        conn.commit()
        conn.close()
        return real_purge(settings, library_name, packet_id, **kwargs)

    monkeypatch.setattr(cli_module, "purge", purge_without_triggers)
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST, "--purge")
    assert code == 1
    assert result["stages"]["purge"]["raw"]["counts"]["text_index_out_of_step"] > 0  # type: ignore[index]
    assert result["stages"]["withdraw"]["raw"]["counts"]["text_index_out_of_step"] == 0  # type: ignore[index]


def test_the_copy_is_cut_off_from_the_original_graph_and_side_store(original_graph: tuple[Path, object], tmp_path: Path,
                                                                    drill_env: None) -> None:
    """审阅第 4 条：演完的拷贝还记着原来那个图、体检库的标记，以后谁在拷贝上跑 graph sync、side sweep 就删到原来那一份。
    演完（不管过没过）把拷贝和它们断开。"""
    source, graph = original_graph
    _side_db(source, FIRST)
    with sqlite3.connect(source / "side" / "test" / "side.db") as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('marker_id', 'made-up-side-marker')")
    code, _, target = _drill(tmp_path, source, "--packet", FIRST)
    assert code == 0
    conn = sqlite3.connect(target / "data" / "ledger" / "test.db")
    assert conn.execute("SELECT enabled, marker_id FROM graph_state WHERE id = 1").fetchone() == (0, None)
    conn.close()
    conn = sqlite3.connect(target / "data" / "side" / "test" / "side.db")
    assert conn.execute("SELECT count(*) FROM meta WHERE key = 'marker_id'").fetchone()[0] == 0
    conn.close()
    assert _facts_in(graph, FIRST, source) > 0  # 原来那个图没动


@pytest.mark.parametrize("uri", ["bolt://[0:0:0:0:0:0:0:1]:7689", "bolt://[::0:1]:7689", "bolt://[0::1]:7689", "bolt://127.9.9.9:7689"])
def test_every_loopback_spelling_of_the_shared_port_counts(uri: str) -> None:
    """复审第 3 条：IPv6 回环的别的写法也算本机。"""
    from javmem.drill.delete import _shared_test_port

    assert _shared_test_port(uri)
    assert not _shared_test_port(uri.replace("7689", "17999"))


def test_a_given_graph_keeps_its_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """复审第 4 条：given 的图不去改默认密码（不空、又还是默认密码的图，核出来不演了，密码却被改了）；managed 新起的才改。"""
    from javmem.drill.delete import _require_empty_graph
    from javmem.graph import neo
    from javmem.graph.memory import MemoryGraph

    calls: list[str] = []
    monkeypatch.setattr(neo, "set_initial_password", lambda target, timeout=None: calls.append("changed") or True)
    monkeypatch.setattr(neo, "NeoGraph", lambda target, timeout=None: MemoryGraph())
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_URI", "bolt://127.0.0.1:17999")
    monkeypatch.setenv("JAVMEM_GRAPH_TEST_NEO4J_PASSWORD", "made-up-pass-for-the-test")
    _require_empty_graph("test", fresh=False)
    assert calls == []
    _require_empty_graph("test", fresh=True)
    assert calls == ["changed"]


def test_cut_off_goes_on_past_a_broken_ledger(mini: Path, tmp_path: Path) -> None:
    """复审第 5 条：一个账本打不开，后面的账本、支线库照样断开，打不开的照实报出来。"""
    from javmem.drill.delete import cut_off

    data = tmp_path / "copy"
    shutil.copytree(mini, data, ignore=shutil.ignore_patterns(*LOCK_NAMES))
    with sqlite3.connect(data / "ledger" / "test.db") as conn:
        conn.execute("UPDATE graph_state SET enabled = 1, marker_id = 'made-up-marker' WHERE id = 1")
    (data / "ledger" / "prod.db").write_bytes(b"not a database at all" * 100)
    problems = cut_off(data)
    assert problems == ["ledger/prod.db"]
    with sqlite3.connect(data / "ledger" / "test.db") as conn:
        assert conn.execute("SELECT enabled, marker_id FROM graph_state WHERE id = 1").fetchone() == (0, None)


def test_the_managed_container_comes_from_the_compose_file(tmp_path: Path) -> None:
    """审阅第 6 条：容器名让人写，写错了就当成“没有这个容器”，把别人开着的测试图停掉。现在照 compose 文件认，不让写。"""
    from javmem.drill.common import CannotRun
    from javmem.drill.delete import _repo_compose, compose_container

    assert compose_container(_repo_compose()) == "javmem-graph-test-neo4j"
    two = tmp_path / "two.yaml"
    two.write_text("services:\n  a: {container_name: x}\n  b: {container_name: y}\n", encoding="utf-8")
    with pytest.raises(CannotRun):
        compose_container(two)


def test_a_crash_is_reported_as_cannot_run(mini: Path, tmp_path: Path, drill_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 12 条：中途出了想不到的错，退出码是 2（跑不了），不和“没过”混在一起；结果文件照样写。"""
    from javmem.drill import delete as drill_delete

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("坏了")

    monkeypatch.setattr(drill_delete, "record_before", broken)
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST)
    assert code == 2 and "RuntimeError" in result["cannot"]  # type: ignore[operator]


def test_nothing_found_before_the_withdrawal_cannot_run(mini: Path, tmp_path: Path, drill_env: None,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 11 条：撤回以前问查询接口就问不到这个包的东西，撤回以后问不到证明不了什么：不演。"""
    from javmem.drill import delete as drill_delete

    real_probe = drill_delete.probe_queries

    def nothing(*args: object, **kwargs: object) -> dict[str, object]:
        found = real_probe(*args, **kwargs)  # type: ignore[arg-type]
        return {**found, "leaks": {key: 0 for key in found["leaks"]}}

    monkeypatch.setattr(drill_delete, "probe_queries", nothing)
    code, result, _ = _drill(tmp_path, mini, "--packet", FIRST)
    assert code == 2 and "问不到" in result["cannot"]  # type: ignore[operator]
