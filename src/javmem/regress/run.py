"""javmem regress：回归跑分（U2 甲，D205 ①）。一条命令，照套件把三样做一遍，和基准比：

1. **金标准：** 每条线拷一份结果库，照现在的代码重建账本（init、收包、写账；假向量，不调模型），`javmem score --scores-only`
   打分。和基准一字不差（金标准、分数那几节）的算“一样”；不一样的看六个主要的数（关系准确率、覆盖率、变化判对率、
   别名对齐率、事实完整抽到、事实对得上）降没降。
2. **调试题只检索：** 每条线拷一份结果库、向量缓存，照现在的代码重建账本（向量走缓存，缺的找本机的向量服务），
   同步进测试图（`graph.mode`），每道题三路量证据命中率（`javmem.regress.evidence`）。三路各两档（5,000 字、1,500 字）
   不比基准少才算没降；对上原句的出处少了也算降了。
3. **记账：** 这一遍每一步的 token 账都在工作目录里，付钱的（不是本机的 Qwen、不是假的）一行都不许有。起子进程时
   付钱的那几个密钥根本不给（`JAVMEM_LLM_API_KEY`、`JAVMEM_SOFT_API_KEY`），这一条是兜底。

退出码：0 过了（每一样都一样或者没降）；1 降了（加了 `--exact` 的，变了就算）；2 跑不了：套件、基准不对，测试图被占着，
代码改了窗口的样子、结果库里对不上要重抽（只报几个窗口、估多少字，不调模型），向量拿不全。

工作目录（`--work`，默认套件旁边的 `work/<时间>`）在仓库外，里面是这一遍的数据目录、日志、分数（有真数据的东西）。
屏幕上只打数，不打原文、名字、问话。`--save-baseline --note 决策号`：这一遍过了（或者主会话认了它是真改进），存成新的基准。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from javmem.config import ConfigError
from javmem.regress.suite import GoldLine, RetrievalLine, Suite, load_suite

EXIT_OK, EXIT_DROPPED, EXIT_CANNOT = 0, 1, 2
BASELINE_FORMAT = "javmem-regress-baseline/1"
# 金标准不降的六个数：(分数.json 中的路径, 叫法)
METRICS = (
    (("relations", "precision"), "关系准确率"),
    (("relations", "recall"), "关系覆盖率"),
    (("changes", "rate"), "变化判对率"),
    (("aliases", "rate"), "别名对齐率"),
    (("facts", "complete_rate"), "事实完整抽到"),
    (("facts", "precision"), "事实对得上"),
)
ROUTE_NAMES = {"graph": "主图", "ledger": "账本视图", "raw": "原文层"}
# 起子进程时从这里的环境变量里接着给的（付钱的密钥一律不给）：
# - 向量服务：只在地址是本机的时候给（外面的可能要钱，真数据的事实、问话也就发出去了）；模型名照这条线写的，不照外面配的
# - 测试图：密码、账号照给；地址只在 given 模式给（managed 连回归自己起的那一个，就是默认的测试图口）
EMBEDDING_PASS = ("JAVMEM_EMBEDDING_BASE_URL", "JAVMEM_EMBEDDING_API_KEY", "JAVMEM_EMBEDDING_TIMEOUT_SECONDS")
GRAPH_PASS = ("JAVMEM_GRAPH_TEST_NEO4J_USER", "JAVMEM_GRAPH_TEST_NEO4J_PASSWORD")
GRAPH_URI = "JAVMEM_GRAPH_TEST_NEO4J_URI"
LOCAL_MODELS = ("qwen", "fake")

Out = Callable[[str], None]


class CannotRun(Exception):
    """这一遍跑不了（退出码 2）。消息里只有数和原因，没有原文。"""


@dataclass
class Result:
    gold: dict[str, dict[str, object]] = field(default_factory=dict)
    retrieval: dict[str, dict[str, object]] = field(default_factory=dict)
    paid_rows: int = 0
    local_rows: int = 0
    ledgers: list[str] = field(default_factory=list)


# ---------- 子进程 ----------


def _src_dir() -> Path:
    import javmem

    return Path(javmem.__file__).resolve().parents[1]


def _local_url(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url.strip()).hostname or "") in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


def child_env(suite: Suite, extra: Mapping[str, str], data: Path, ledger: Path) -> dict[str, str]:
    """子进程的环境：这里的环境变量去掉所有 JAVMEM_*，再加上套件的设置、这条线的设置、本机的向量服务和测试图的那几样。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("JAVMEM_")}
    if _local_url(os.environ.get("JAVMEM_EMBEDDING_BASE_URL", "")):
        for key in EMBEDDING_PASS:
            if os.environ.get(key):
                env[key] = os.environ[key]
    for key in GRAPH_PASS + ((GRAPH_URI,) if suite.graph.mode == "given" else ()):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update(suite.config)
    env.update(extra)
    # 合好以后再核一遍（套件已经不收地址，这一道是兜底）：向量服务不是本机的，那几样都不给；managed 不照别处写的测试图地址
    if not _local_url(env.get("JAVMEM_EMBEDDING_BASE_URL", "")):
        for key in EMBEDDING_PASS:
            env.pop(key, None)
    if suite.graph.mode != "given":
        env.pop(GRAPH_URI, None)
    env.update(JAVMEM_DATA_DIR=str(data), JAVMEM_TOKEN_LEDGER=str(ledger), PYTHONPATH=str(_src_dir()),
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return env


def javmem(env: Mapping[str, str], log: Path, *args: str, module: str = "javmem") -> tuple[int, str]:
    """跑一条 javmem 命令，输出整个写进日志，返回 (退出码, 输出)。屏幕上不打它的输出（里面可能有原文）。"""
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-X", "utf8", "-m", module, *args], env=dict(env), cwd=str(log.parent),
                          capture_output=True, encoding="utf-8", errors="replace")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {module} {' '.join(args)} -> {done.returncode}（{time.monotonic() - started:.1f} 秒）\n"
                     f"{done.stdout}\n--- stderr ---\n{done.stderr}\n")
    return done.returncode, done.stdout


def _copy_db(source: Path, target: Path) -> None:
    """SQLite 照在线备份拷（-wal 里没并进去的也带上）；原件只读打开，一个字不改。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _store_into(source: Path, data: Path) -> None:
    target = data / "l3" / "l3.db"
    if source.suffix.lower() == ".json":
        from javmem.regress.replies import build_store

        build_store(source, target.parent)
    else:
        _copy_db(source, target)


def _prepare(suite: Suite, env: Mapping[str, str], root: Path, packets: Sequence[Path], store: Path,
             vectors: Path | None) -> Path:
    """新的数据目录：init、收包、放好结果库（和向量缓存）。返回数据目录。"""
    data = root / "data"
    log = root / "steps.log"
    code, _ = javmem(env, log, "init")
    if code != 0:
        raise CannotRun(f"{root.name}：javmem init 退出码 {code}（看 {log}）")
    for packet in packets:
        target = data / "inbox-test" / packet.parent.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(packet, target / packet.name)
    code, text = javmem(env, log, "intake")
    if code != 0:
        raise CannotRun(f"{root.name}：javmem intake 退出码 {code}（看 {log}）")
    _store_into(store, data)
    if vectors is not None:
        _copy_db(vectors, data / "vectors" / "vectors.db")
    return data


def _write(env: Mapping[str, str], root: Path, *, fake_embeddings: bool) -> dict[str, object]:
    log = root / "steps.log"
    summary = root / "write-summary.json"
    args = ["write", "--library", "test", "--summary", str(summary), "--tag", "javmem-regress"]
    if fake_embeddings:
        args.append("--fake-embeddings")
    code, _ = javmem(env, log, *args)
    found = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else {}
    this_run = found.get("this_run") or {}
    # 结果库里对不上的（没有 L3 的结果、判断 C 的判定、模型复核的判定）：没写进账本，往下量出来的数就不对了。
    # 只差判断 C 的，javmem write 照样退出码 0，所以不看退出码、看这几个数
    missing = sum(int(this_run.get(key) or 0) for key in
                  ("windows_without_result", "windows_without_judgment", "windows_without_recheck"))
    if missing:
        estimate = _estimate(env, root)
        raise CannotRun(f"{root.name}：有 {missing} 个窗口在结果库里对不上（代码改了窗口的样子、提示词、判定的键，要重抽）"
                        f"{estimate}；没调模型")
    waiting = int(this_run.get("packets_waiting") or 0)
    if waiting:
        raise CannotRun(f"{root.name}：写完账还有 {waiting} 个包留着排队（没全写进账本，看 {log}）")
    if code == 4:
        raise CannotRun(f"{root.name}：向量服务调不通，缺的向量没补上（javmem write 退出码 4）：起好本机的向量服务再跑")
    if code != 0:
        raise CannotRun(f"{root.name}：javmem write 退出码 {code}（看 {log}）")
    return found


def _estimate(env: Mapping[str, str], root: Path) -> str:
    """要重抽的话要调几个窗口、多少字：javmem extract --dry-run（不调模型、不要密钥）。"""
    code, text = javmem(env, root / "steps.log", "extract", "--library", "test", "--dry-run")
    if code != 0:
        return ""
    lines = [line for line in text.splitlines() if "要调" in line or "一共" in line]
    return "：" + "；".join(line.strip() for line in lines[:3]) if lines else ""


# ---------- 一样一样跑 ----------


def run_gold(suite: Suite, line: GoldLine, work: Path) -> dict[str, object]:
    root = work / f"gold-{line.line}"
    root.mkdir(parents=True)
    env = child_env(suite, line.env, root / "data", root / "token-ledger.csv")
    data = _prepare(suite, env, root, line.packets, line.store, None)
    _write(env, root, fake_embeddings=True)
    out = root / "score"
    args = ["score", "--gold", str(line.score.gold), "--side", f"javmem={data}", "--library", line.score.library,
            "--scores-only", "--out", str(out)]
    for extra in line.score.extra:
        args += ["--gold-extra", str(extra)]
    for bridge in line.score.bridges:
        args += ["--bridge", str(bridge)]
    if line.score.review is not None:
        args += ["--review", str(line.score.review)]
    code, _ = javmem(env, root / "steps.log", *args)
    scores_file = out / "分数.json"
    if code != 0 or not scores_file.exists():
        raise CannotRun(f"金标准 {line.line}：javmem score 退出码 {code}（看 {root / 'steps.log'}）")
    return {"scores": comparable_scores(json.loads(scores_file.read_text(encoding="utf-8")))}


def comparable_scores(scores: Mapping[str, object]) -> dict[str, object]:
    """分数.json 里拿来比的那几节：金标准那一节、这一边的分数；打分时间、这一边写在哪（路径）不比。"""
    sides = {name: {key: value for key, value in side.items() if key != "described"}  # type: ignore[union-attr]
             for name, side in (scores.get("sides") or {}).items()}  # type: ignore[union-attr]
    return {"format": scores.get("format"), "gold": scores.get("gold"), "sides": sides}


def line_env(line: RetrievalLine) -> dict[str, str]:
    """调试题这条线的设置：套件里写的，加上向量的模型名（写账、同步、量的时候是同一个，问话的向量才对得上图里的）。"""
    return {**line.env, "JAVMEM_EMBEDDING_MODEL": line.embedding_model}


def run_retrieval(suite: Suite, line: RetrievalLine, work: Path, graph: object | None, log: Out) -> dict[str, object]:
    root = work / f"retrieval-{line.line}"
    root.mkdir(parents=True)
    env = child_env(suite, line_env(line), root / "data", root / "token-ledger.csv")
    data = _prepare(suite, env, root, line.packets, line.store, line.vectors)
    written = _write(env, root, fake_embeddings=line.fake_embeddings)
    steps = root / "steps.log"
    with_graph = graph is not None
    if with_graph:
        if hasattr(graph, "up"):
            seconds = graph.up()  # type: ignore[union-attr]
            log(f"  测试图起好了（{seconds:.0f} 秒），挂的是回归自己的目录")
        code, _ = javmem(env, steps, "graph", "init", "--library", "test")
        if code != 0:
            raise CannotRun(f"调试题 {line.line}：javmem graph init 退出码 {code}（看 {steps}）")
        sync = ["graph", "sync", "--library", "test"] + (["--fake-embeddings"] if line.fake_embeddings else [])
        code, _ = javmem(env, steps, *sync)
        if code != 0:
            raise CannotRun(f"调试题 {line.line}：javmem graph sync 退出码 {code}（看 {steps}）")
    measure_file = root / "measure.json"
    args = ["--questions", str(line.questions), "--out", str(measure_file), "--embedding-model", line.embedding_model]
    if with_graph:
        args.append("--graph")
    if line.fake_embeddings:
        args.append("--fake-embeddings")
    if line.query_vectors is not None:
        args += ["--query-vectors", str(line.query_vectors)]
    ask_env = {**env, "JAVMEM_CALLER": line.caller}
    code, _ = javmem(ask_env, steps, *args, module="javmem.regress.evidence")
    if code != 0 or not measure_file.exists():
        raise CannotRun(f"调试题 {line.line}：量证据命中率出错了（退出码 {code}，看 {steps}）")
    found = json.loads(measure_file.read_text(encoding="utf-8"))
    queries = found.get("queries") or {}
    if with_graph and queries.get("missing"):
        raise CannotRun(f"调试题 {line.line}：{queries['missing']} 句问话的向量拿不到（缓存里没有、向量服务也没问到），"
                        "主图那一路的向量量不全")
    if with_graph and found.get("graph_vectors") is False:
        raise CannotRun(f"调试题 {line.line}：主图的向量那一路没开（问话的向量和图里的不是同一个模型，或者问话的向量拿不到），"
                        "量出来的不能当主图的数")
    if with_graph and found.get("graph_degraded"):
        raise CannotRun(f"调试题 {line.line}：主图那一路有 {found['graph_degraded']} 道题退回了账本视图（图连不上、超时、"
                        f"问话的向量和图里的不是一个模型……），量出来的不能当主图的数（看 {steps}）")
    return {"totals": found["totals"], "resolved": found["resolved"], "sources": found["sources"],
            "routes": found["routes"], "questions": found["questions"], "questions_sha256": found.get("questions_sha256"),
            "facts": written.get("facts"), "queries": queries}


def count_ledgers(work: Path) -> tuple[int, int, list[str]]:
    """(付钱的行, 本机和假的行, 账文件)。模型名是 qwen、fake 开头的算本机、假的（向量服务的名字是 Qwen/…）。"""
    from javmem.usage import TokenLedger

    paid = local = 0
    files = []
    for path in sorted(work.rglob("token-ledger*.csv")):
        files.append(str(path))
        for row in TokenLedger(path).rows():
            if (row.get("model") or "").lower().startswith(LOCAL_MODELS):
                local += 1
            else:
                paid += 1
    return paid, local, files


# ---------- 比 ----------


def _metric(scores: Mapping[str, object], path: Sequence[str]) -> float | None:
    node: object = (scores.get("sides") or {}).get("javmem")  # type: ignore[union-attr]
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return float(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else None


def compare_gold(now: Mapping[str, object], before: Mapping[str, object] | None) -> tuple[str, list[str], list[str]]:
    """(一样 / 变了 / 没有基准, 降了的, 主要的数)。"""
    scores = now["scores"]
    shown = []
    for path, name in METRICS:
        value = _metric(scores, path)  # type: ignore[arg-type]
        shown.append(f"{name} {value:.1%}" if value is not None else f"{name} ?")
    if before is None:
        return "没有基准", [], shown
    old = before.get("scores") or {}
    if old == scores:
        return "一样", [], shown
    dropped = []
    for path, name in METRICS:
        a, b = _metric(old, path), _metric(scores, path)  # type: ignore[arg-type]
        if a is not None and (b is None or b < a - 1e-9):
            dropped.append(f"{name} {a:.1%} → {'?' if b is None else f'{b:.1%}'}")
    if (old.get("gold") if isinstance(old, Mapping) else None) != scores.get("gold"):  # type: ignore[union-attr]
        dropped.append("金标准本身变了（单元、条目、审阅结果和基准不一样）：先看是不是换了金标准")
    return "变了", dropped, shown


def compare_retrieval(now: Mapping[str, object], before: Mapping[str, object] | None) -> tuple[str, list[str], str]:
    totals = now["totals"]
    old = (before or {}).get("totals") or {}
    parts = []
    for route in now["routes"]:  # type: ignore[union-attr]
        got = totals[route]  # type: ignore[index]
        was = old.get(route)  # type: ignore[union-attr]
        parts.append(f"{ROUTE_NAMES.get(route, route)} {got['5000']} / {got['1500']}"
                     + (f"（基准 {was['5000']} / {was['1500']}）" if was else ""))
    text = "，".join(parts)
    if before is None:
        return "没有基准", [], text
    dropped = []
    for route in now["routes"]:  # type: ignore[union-attr]
        was = old.get(route)  # type: ignore[union-attr]
        if not was:
            continue
        for budget in ("5000", "1500"):
            if totals[route][budget] < was[budget]:  # type: ignore[index]
                dropped.append(f"{ROUTE_NAMES.get(route, route)} {budget} 字 {was[budget]} → {totals[route][budget]}")  # type: ignore[index]
    if now["resolved"] < (before.get("resolved") or 0):  # type: ignore[operator]
        dropped.append(f"对上原句的出处 {before.get('resolved')} → {now['resolved']}")
    if before.get("questions_sha256") and now.get("questions_sha256") != before.get("questions_sha256"):
        dropped.append(f"题变了（题文件和基准时的不一样：{before.get('questions')} 道 → {now.get('questions')} 道）："
                       "命中数没法比，先看是不是换了题，换了就重存基准")
    same = (all(totals.get(route) == old.get(route) for route in now["routes"])  # type: ignore[union-attr]
            and all(now.get(key) == before.get(key) for key in ("resolved", "sources")))
    return ("一样" if same else "变了"), dropped, text


# ---------- 一遍 ----------


def _code() -> str:
    from javmem.versions import code_info

    info = code_info()
    commit = info.get("commit")
    return (str(commit)[:12] if commit else "?") + ("+改动" if info.get("dirty") else "")


def _load_baseline(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CannotRun(f"基准 {path} 读不了（{type(error).__name__}）") from None
    if not isinstance(data, dict) or data.get("format") != BASELINE_FORMAT:
        raise CannotRun(f"基准 {path} 不是 {BASELINE_FORMAT}")
    for name, one in (data.get("gold") or {}).items():
        if not isinstance(one, dict) or not isinstance(one.get("scores"), dict):
            raise CannotRun(f"基准 {path} 写得不对：金标准 {name} 没有 scores")
    for name, one in (data.get("retrieval") or {}).items():
        totals = one.get("totals") if isinstance(one, dict) else None
        if not isinstance(totals, dict) or not all(
                isinstance(got, dict) and all(isinstance(got.get(budget), int) for budget in ("5000", "1500"))
                for got in totals.values()):
            raise CannotRun(f"基准 {path} 写得不对：调试题 {name} 的命中数不全")
    return data


def _check_given(suite: Suite, graph_mode: str) -> None:
    """given 的图：环境变量里要写明地址，而且不能是默认的测试图口（测试库的图：本机放着测试库的真数据，几个会话共用）。"""
    if not suite.retrieval or graph_mode != "given":
        return
    from javmem.graph.config import DEFAULT_TEST_URI

    uri = os.environ.get(GRAPH_URI, "").strip()
    if not uri:
        raise CannotRun(f"given 的图要在环境变量 {GRAPH_URI} 里写明地址（不写就是默认的测试图口，"
                        "本机几个会话共用的那一个：不去占）")
    if uri.rstrip("/").lower() == DEFAULT_TEST_URI.lower():
        raise CannotRun(f"given 的图不能是默认的测试图口 {DEFAULT_TEST_URI}（测试库的图，放着测试库的真数据）："
                        "起一个空的、只给这一遍用的图，地址写进 " + GRAPH_URI)


def run_suite(suite: Suite, work: Path, *, graph_mode: str, log: Out = print, who: str = "javmem regress") -> Result:
    result = Result()
    _check_given(suite, graph_mode)  # 一开头就核：不然金标准那几条白跑
    for line in suite.gold:
        started = time.monotonic()
        result.gold[line.line] = run_gold(suite, line, work)
        log(f"金标准 {line.line} 重建、打分做完（{time.monotonic() - started:.0f} 秒）")
    if suite.retrieval:
        graph: object | None = None
        if graph_mode == "managed":
            from javmem.regress.graph import ManagedGraph

            spec = suite.graph
            graph = ManagedGraph(spec.compose, spec.data, spec.container, lock=spec.lock, who=who, log=log)  # type: ignore[arg-type]
        elif graph_mode == "given":
            graph = object()  # 环境变量里配好的那个图（开头核过地址）：不起、不停
        try:
            if graph is not None and hasattr(graph, "take_lock"):
                graph.take_lock(15)  # type: ignore[union-attr]
            for line in suite.retrieval:
                started = time.monotonic()
                result.retrieval[line.line] = run_retrieval(suite, line, work, graph, log)
                log(f"调试题 {line.line} 重建、只检索做完（{time.monotonic() - started:.0f} 秒）")
        finally:
            if graph is not None and hasattr(graph, "down"):
                try:
                    graph.down()  # type: ignore[union-attr]
                finally:
                    graph.drop_lock()  # type: ignore[union-attr]
    result.paid_rows, result.local_rows, result.ledgers = count_ledgers(work)
    return result


def report(suite: Suite, result: Result, baseline: Mapping[str, object] | None, *, exact: bool, graph_mode: str,
           log: Out) -> int:
    worst = EXIT_OK
    if baseline is not None:  # 线改了名、新加的、这次没跑的：照实说；--exact 的算没过（和基准对不上就不是“一样”）
        for kind, label in (("gold", "金标准"), ("retrieval", "调试题")):
            have = set(getattr(result, kind))
            had = set((baseline.get(kind) or {}))  # type: ignore[arg-type]
            for name in sorted(have - had):
                log(f"（基准里没有{label} {name} 这条线：这一遍只出数）")
            for name in sorted(had - have):
                log(f"（基准里有{label} {name}，这次没跑）")
            if exact and have != had:
                worst = EXIT_DROPPED
    for name, now in result.gold.items():
        before = ((baseline or {}).get("gold") or {}).get(name)  # type: ignore[union-attr]
        state, dropped, shown = compare_gold(now, before)
        bad = bool(dropped) or (exact and state == "变了")
        log(f"金标准 {name}：{state}（{'、'.join(shown)}）" + (" ✗" if bad else " ✓"))
        for one in dropped:
            log(f"    降了：{one}")
        if exact and state == "变了" and not dropped:
            log("    变了（加了 --exact：这一改本来不该动抽取和写账）")
        worst = max(worst, EXIT_DROPPED if bad else EXIT_OK)
    for name, now in result.retrieval.items():
        before = ((baseline or {}).get("retrieval") or {}).get(name)  # type: ignore[union-attr]
        state, dropped, text = compare_retrieval(now, before)
        bad = bool(dropped) or (exact and state == "变了")
        log(f"调试题 {name}（{now['questions']} 道题，{now['resolved']} / {now['sources']} 条出处对上原句）：{text}；{state}"
            + (" ✗" if bad else " ✓"))
        for one in dropped:
            log(f"    降了：{one}")
        worst = max(worst, EXIT_DROPPED if bad else EXIT_OK)
    if suite.retrieval and graph_mode == "off":
        log("（没量主图那一路：加了 --no-graph，或者套件里 graph.mode 是 off）")
    log(f"记账：付钱的 {result.paid_rows} 行" + ("（应该是 0）✗" if result.paid_rows else " ✓")
        + f"；本机的、假的 {result.local_rows} 行")
    if result.paid_rows:
        worst = max(worst, EXIT_DROPPED)
    return worst


def save_baseline(suite: Suite, result: Result, note: str, log: Out) -> None:
    data = {"format": BASELINE_FORMAT, "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "commit": _code(), "note": note, "suite": suite.name,
            "gold": result.gold,
            "retrieval": {name: {key: value for key, value in one.items()
                                 if key in ("totals", "resolved", "sources", "routes", "questions", "questions_sha256")}
                          for name, one in result.retrieval.items()}}
    from javmem.scoring.run import inside_repo

    old = None
    if suite.baseline.exists() and not inside_repo(suite.baseline):  # 仓库里的（编的小套件）不留备份：git 里有历史
        old = suite.baseline.with_name(suite.baseline.stem + f"-{datetime.now():%Y%m%d-%H%M%S}.json")
        shutil.copy2(suite.baseline, old)
    suite.baseline.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"存成新的基准：{suite.baseline}（{note}）" + (f"；原来的那份留在 {old.name}" if old else ""))


def may_save(result: Result, *, graph_mode: str, suite_mode: str) -> str | None:
    """这一遍能不能存成基准：不能的回原因。有付钱的行的不存；套件要量主图、这一遍没量的不存（以后跑全的比不了主图那一路）。"""
    if result.paid_rows:
        return f"这一遍有 {result.paid_rows} 行付钱的记账，不存成基准"
    if graph_mode == "off" and suite_mode != "off":
        return "加了 --no-graph 的这一遍没量主图那一路，不存成基准：去掉 --no-graph 跑全的再存"
    return None


def work_dir(text: str | None, suite_path: Path) -> Path:
    """这一遍的工作目录，照绝对路径（子进程的工作目录不一样，相对路径会被再拼一遍）。默认套件旁边的 work/<时间>。"""
    if text:
        return Path(text).resolve()
    return (suite_path.parent / "work" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()


# ---------- 命令行 ----------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--suite", required=True, help="套件文件（javmem-regress/1 的 YAML）")
    parser.add_argument("--work", help="这一遍的工作目录（要在仓库外、还不存在），默认套件旁边的 work/<时间>")
    parser.add_argument("--no-graph", action="store_true", help="不量主图那一路（测试图被占着、子会话交活之前用）")
    parser.add_argument("--exact", action="store_true", help="变了就算没过（这一改本来不该动抽取和写账）")
    parser.add_argument("--save-baseline", action="store_true", help="这一遍存成新的基准（要写 --note）")
    parser.add_argument("--note", help="为什么换基准（决策号）")
    parser.add_argument("--who", default="javmem regress", help="拿测试图的锁时写的会话名")
    parser.add_argument("--json", metavar="文件", help="这一遍的结果另写一份 JSON（只有数）")


def run(args: argparse.Namespace, out: Out = print) -> int:
    from javmem.scoring.run import inside_repo

    started = time.monotonic()
    try:
        suite = load_suite(Path(args.suite))
    except ConfigError as error:
        out(f"跑不了：{error}")
        return EXIT_CANNOT
    if args.save_baseline and not args.note:
        out("跑不了：--save-baseline 要写 --note（为什么换基准，决策号）")
        return EXIT_CANNOT
    work = work_dir(args.work, suite.path)
    if args.json and inside_repo(Path(args.json)):
        out("跑不了：--json 的文件在仓库里（里面有包号、单元号）。换一个仓库外的")
        return EXIT_CANNOT
    graph_mode = "off" if args.no_graph else suite.graph.mode
    if args.save_baseline:
        refused = may_save(Result(), graph_mode=graph_mode, suite_mode=suite.graph.mode)
        if refused:
            out(f"跑不了：{refused}")
            return EXIT_CANNOT
    if inside_repo(work):
        out("跑不了：工作目录在仓库里（里面有重建的账本、分数，真数据的套件还有原文）。换一个仓库外的目录")
        return EXIT_CANNOT
    if work.exists() and any(work.iterdir()):
        out(f"跑不了：工作目录 {work} 不是空的，每一遍用一个新的")
        return EXIT_CANNOT
    work.mkdir(parents=True, exist_ok=True)
    out(f"javmem regress：套件「{suite.name}」，代码 {_code()}，工作目录 {work}")
    try:
        baseline = _load_baseline(suite.baseline)
        result = run_suite(suite, work, graph_mode=graph_mode, log=out, who=args.who)
    except CannotRun as error:
        out(f"跑不了：{error}")
        return EXIT_CANNOT
    except Exception as error:  # noqa: BLE001 - 测试图、docker 那一层的错（GraphBusy、GraphRefused……），细节在日志里
        out(f"跑不了：{type(error).__name__}：{error}")
        return EXIT_CANNOT
    if baseline is None:
        out(f"（还没有基准 {suite.baseline}：这一遍只出数，过了可以 --save-baseline 存成基准）")
    code = report(suite, result, baseline, exact=args.exact, graph_mode=graph_mode, log=out)
    if args.json:
        Path(args.json).write_text(json.dumps({"exit": code, "gold": result.gold, "retrieval": result.retrieval,
                                               "paid_rows": result.paid_rows, "local_rows": result.local_rows},
                                              ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.save_baseline:
        refused = may_save(result, graph_mode=graph_mode, suite_mode=suite.graph.mode)
        if refused:
            out(f"没存成基准：{refused}")
        else:
            if code != EXIT_OK:
                out(f"（这一遍有降了的；照 --note「{args.note}」照样存成了基准）")
            save_baseline(suite, result, args.note, out)
    minutes, seconds = divmod(int(time.monotonic() - started), 60)
    out(f"用时 {minutes} 分 {seconds} 秒。结论：" + {EXIT_OK: "过了", EXIT_DROPPED: "没过（有降了的）"}.get(code, "跑不了")
        + f"（退出码 {code}）")
    return code


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(prog="javmem regress")
    add_arguments(parser)
    sys.exit(run(parser.parse_args()))
