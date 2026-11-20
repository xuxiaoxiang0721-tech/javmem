"""`javmem graph …`：主图的命令（J5.1、J5.2）。

- `init`：一个图第一次用：默认密码还在的先改成环境变量里的（bolt 上改，密码当参数传）；核往外连的几项设置关没关、全文索引
  默认是不是 cjk、有没有 cjk；核过了才放标记（防串库）、建约束和索引，再核每个全文索引是 cjk、向量索引是 2560 维；
  账本记下标记、接上图（下一次同步是整张的）。已经接上的再跑一遍只核、补建索引。`--reset`：图整个重建过（空的、没有标记），
  让账本忘了上一个图，再接上这个新的。
- `sync`：把欠着的同步进去（`--all` 账本里每一条都算一遍，指纹一样的照样不写）。
- `rebuild`：清空重来，从账本整张重建（要 `--yes`），打出每一段的耗时。
- `status`：欠着多少（按种类）、最早欠的是什么时候、上次同步、图里有多少（不连图）。
- `export`：按编号排好导出、打出摘要（`--out` 要在仓库外：里面有内容）。
- `check`：账本照现在的样子该有的和图里实际的一条条比，对不上的列编号。

退出码：0 好了；1 有对不上的、还欠着；2 设置、参数不对，什么都没动；3 这个库已经有一个同步在跑；4 图连不上（欠着的留着）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from javmem.config import ConfigError, Settings
from javmem.embedding import DEFAULT_MODEL as DEFAULT_EMBEDDING_MODEL
from javmem.embedding import ENV_MODEL as ENV_EMBEDDING_MODEL
from javmem.embedding import Embedder, EmbeddingSettings, FakeEmbeddings, OpenAIEmbeddings
from javmem.graph import dirty, neo
from javmem.graph.config import ENV_SYNC, GraphSettings, GraphTarget
from javmem.graph.hook import default_gate, embedding_recorder
from javmem.graph.project import PROJECTION_VERSION
from javmem.graph.sync import (
    GraphBusy,
    GraphNotSetUp,
    RelationVectors,
    compare,
    enable,
    expected_lines,
    export,
    export_lines,
    forget,
    graph_lock,
    read_state,
    sync,
)
from javmem.ledger import LedgerError, open_ledger
from javmem.pipeline.vectors import VectorCache
from javmem.scoring.run import inside_repo

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_INPUT = 2
EXIT_BUSY = 3
EXIT_STOPPED = 4
DEFAULT_TAG = "j5-graph"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="graph_command", required=True)
    init = commands.add_parser("init", help="一个图第一次用：改默认密码、核设置、放标记、建索引，账本接上这个图")
    init.add_argument("--reset", action="store_true", help="图整个重建过（空的、没有标记）：账本忘了上一个图，接上这个新的")
    for name, text in (("sync", "把欠着的同步进图"), ("rebuild", "清空重来，从账本整张重建")):
        sub = commands.add_parser(name, help=text)
        if name == "sync":
            sub.add_argument("--all", action="store_true", help="账本里每一条都算一遍（指纹一样的照样不写）")
        else:
            sub.add_argument("--yes", action="store_true", help="确定要清空重来")
        sub.add_argument("--fake-embeddings", action="store_true", help="关系的向量用假的（试流程、测试的账本）")
        sub.add_argument("--vectors", help="向量缓存的目录，默认数据目录下的 vectors")
        sub.add_argument("--tag", default=DEFAULT_TAG, help="向量服务调用记账的标签")
        sub.add_argument("--chunk", type=int, default=500, help="一批写多少条（一批一个事务）")
        sub.add_argument("--verbose", action="store_true", help="每一批打一行（整张重建量快慢用）")
    commands.add_parser("status", help="欠着多少、上次同步、图里有多少（不连图）")
    export_cmd = commands.add_parser("export", help="按编号排好导出，打出摘要")
    export_cmd.add_argument("--out", help="写到这个文件（放仓库外：里面有内容）；不给只打摘要")
    export_cmd.add_argument("--no-vectors", action="store_true", help="不读向量（大的图导得快些，摘要和带向量的不一样）")
    check = commands.add_parser("check", help="账本该有的和图里实际的一条条比")
    check.add_argument("--fake-embeddings", action="store_true", help="关系的向量照假的算（测试的账本）")
    check.add_argument("--vectors", help="向量缓存的目录，默认数据目录下的 vectors")
    for sub in commands.choices.values():
        sub.add_argument("--library", required=True, choices=("prod", "test"), help="哪个库")


def run(args: argparse.Namespace, settings: Settings, out: Callable[[str], None] = print) -> int:
    logging.getLogger("neo4j").setLevel(logging.ERROR)  # 驱动的提示不打出来
    try:
        graph_settings = GraphSettings.from_env()
        data_dir = settings.require_data_dir()
        target = graph_settings.target(args.library)
        if args.graph_command == "export" and args.out and inside_repo(Path(args.out)):
            out("导不了：--out 在仓库里。导出里有内容，要放仓库外")
            return EXIT_INPUT
        if args.graph_command in ("sync", "rebuild") and not 1 <= args.chunk <= 5000:
            out("--chunk 要在 1 到 5000 之间")
            return EXIT_INPUT
        if args.graph_command == "rebuild" and not args.yes:
            out("整张重建会先把图清空：确定的话加 --yes")
            return EXIT_INPUT
    except ConfigError as error:
        out(f"配置不对：{error}")
        return EXIT_INPUT
    if args.graph_command == "status":
        return _status(settings, args.library, out)
    try:
        target.require_password()
    except ConfigError as error:
        out(f"配置不对：{error}")
        return EXIT_INPUT
    try:
        with graph_lock(data_dir, args.library):
            if args.graph_command == "init":
                return _init(settings, args, target, out)
            graph = neo.NeoGraph(target)
            graph.connect()
            try:
                if args.graph_command in ("sync", "rebuild"):
                    return _sync(settings, args, graph, out)
                if args.graph_command == "export":
                    return _export(args, graph, out)
                return _check(settings, args, graph, out)
            finally:
                graph.close()
    except GraphBusy as busy:
        out(str(busy))
        return EXIT_BUSY
    except neo.GraphUnavailable as error:
        out(f"图连不上（{error}）：容器起了没有？欠着的都留着")
        return EXIT_STOPPED
    except neo.GraphAuthError as error:
        out(str(error))
        return EXIT_INPUT
    except (neo.GraphRefused, GraphNotSetUp) as error:
        out(f"图用不了：{error}")
        return EXIT_INPUT
    except (ConfigError, LedgerError) as error:
        out(f"配置不对：{error}")
        return EXIT_INPUT


# ---------- init ----------


def _init(settings: Settings, args: argparse.Namespace, target: GraphTarget, out: Callable[[str], None]) -> int:
    changed = neo.set_initial_password(target)
    out("图的默认密码改好了" if changed else "图的默认密码早就改过了")
    graph = neo.NeoGraph(target)
    graph.connect()
    conn = open_ledger(settings.library(args.library))
    try:
        found = graph.config()  # 先核往外连的设置，核过了才放标记、建索引
        for name, want in neo.EXPECTED_CONFIG.items():
            out(f"  {name} = {found.get(name)}（要 {want}）")
        for name in neo.MEMORY_SETTINGS:
            out(f"  {name} = {found.get(name)}")
        wrong = sorted(name for name, want in neo.EXPECTED_CONFIG.items() if str(found.get(name)).lower() != want)
        if wrong:
            out("图的设置不对：" + "、".join(wrong))
            return EXIT_INPUT
        if "cjk" not in graph.analyzers():
            out("这个 Neo4j 没有 cjk 分析器")
            return EXIT_INPUT
        state = read_state(conn)
        markers = graph.markers()
        if markers:
            graph.check_marker(state.marker_id, args.library)  # 对不上抛 GraphRefused：一个字不写
            out("这个图早就接上这本账了：只核一遍、补建索引")
        else:
            if graph.has_other_content():
                raise neo.GraphRefused("图不是空的，又没有 javmem 主图的标记：一个字不写（是不是配到了别的 Neo4j？）")
            if state.marker_id and not args.reset:
                raise neo.GraphRefused("图是空的，可账本记着上一个图：图整个重建过的话加 --reset")
            if args.reset:
                forget(conn)
            marker_id = graph.create_marker(args.library, PROJECTION_VERSION)
            try:
                enable(conn, marker_id)
            except BaseException:  # 账本没接上（比如账本写不进去）：刚放的标记拿掉，不然再 init 就对不上了
                graph.remove_marker(marker_id)
                raise
            out("标记放好了，装好了记欠账的触发器，账本接上了这个图：下一次同步是整张的（javmem graph sync）")
        graph.ensure_schema()
        graph.await_indexes(600)
        return _report_indexes(graph, out)
    finally:
        conn.close()
        graph.close()


def _report_indexes(graph: neo.NeoGraph, out: Callable[[str], None]) -> int:
    report = graph.index_report()
    problems = []
    for name in neo.FULLTEXT_INDEXES:
        analyzer = (report.get(name) or {}).get("analyzer")
        out(f"  全文索引 {name}：{analyzer}")
        if analyzer != "cjk":
            problems.append(name)
    for name in neo.VECTOR_INDEXES:
        dims = (report.get(name) or {}).get("dimensions")
        out(f"  向量索引 {name}：{dims} 维")
        if dims != neo.EMBEDDING_DIM:
            problems.append(name)
    missing = sorted(set(neo.CONSTRAINTS) - set(graph.constraint_names()))
    if missing or problems:
        out("索引、约束不对：" + "、".join([*missing, *problems]))
        return EXIT_ATTENTION
    out("图好了：设置核过了，约束、索引建好了")
    return EXIT_OK


# ---------- sync、rebuild ----------


def _embedder(args: argparse.Namespace) -> Embedder | None:
    if args.fake_embeddings:
        return FakeEmbeddings()
    try:
        return OpenAIEmbeddings(EmbeddingSettings.from_env())
    except ConfigError:
        return None  # 没配向量服务：关系的向量只查缓存，没有的欠着


def _sync(settings: Settings, args: argparse.Namespace, graph: neo.NeoGraph, out: Callable[[str], None]) -> int:
    embedder = _embedder(args)
    vectors_dir = Path(args.vectors) if args.vectors else settings.require_data_dir() / "vectors"
    mode = "rebuild" if args.graph_command == "rebuild" else "all" if args.all else "incremental"
    conn = open_ledger(settings.library(args.library))
    started = time.perf_counter()
    try:
        if args.fake_embeddings:  # 和写账那边一样：账本里有真向量的，图里的关系不换成假向量（审阅第 7 条）
            real = conn.execute("SELECT count(*) FROM fact_vectors WHERE model != ?", (FakeEmbeddings().name,)).fetchone()[0]
            if real:
                out(f"这本账里已经有 {real} 条真向量，不能用 --fake-embeddings 同步：关系的向量会是假的")
                return EXIT_INPUT
        with VectorCache(vectors_dir) as cache:
            vectors = RelationVectors(cache, embedder, record=embedding_recorder(settings, embedder, args.tag),
                                      model=_model_name())
            log = (lambda line: out(f"[{time.perf_counter() - started:7.1f}s] {line}")) if args.verbose else None
            report = sync(conn, graph, library=args.library, tz=settings.timezone, gate=default_gate(), vectors=vectors,
                          mode=mode, log=log, chunk=args.chunk)
    finally:
        conn.close()
    out(report.text())
    if args.verbose:
        out("每一段的秒数：" + json.dumps({key: round(value, 1) for key, value in report.seconds.items()}, ensure_ascii=False))
    if embedder is None and report.vectors_owed:
        out("没配向量服务（JAVMEM_EMBEDDING_*），关系的向量只查了缓存：配好以后再同步一遍补上")
    return EXIT_ATTENTION if report.owed_after else EXIT_OK


# ---------- status、export、check ----------


def _status(settings: Settings, library: str, out: Callable[[str], None]) -> int:
    conn = open_ledger(settings.library(library))
    try:
        state = read_state(conn)
        counts, oldest = dirty.owed(conn)
        items = dict(conn.execute("SELECT kind, count(*) FROM graph_items GROUP BY kind").fetchall())
    finally:
        conn.close()
    if not state.enabled:
        out("这本账还没接上图（javmem graph init）")
        return EXIT_ATTENTION
    out(f"接上的图：标记 {str(state.marker_id)[:8]}…，图的样子 {state.projection or '还没整张同步过'}，检查 {state.gate or '-'}")
    out(f"上次同步：{state.synced_at or '还没有'}；上次整张同步：{state.rebuilt_at or '还没有（下一次同步是整张的）'}")
    names = (("fact", "事"), ("relation", "关系"), ("alias", "别名"), ("entity", "实体"), ("scope", "范围"))
    out("图里（上次写进去的）：" + "、".join(f"{label} {items.get(kind, 0)}" for kind, label in names))
    if counts:
        out("欠着：" + "、".join(f"{kind} {count}" for kind, count in sorted(counts.items())) + f"，最早从 {oldest}（UTC）")
    else:
        out("欠着：没有")
    sync_off = GraphSettings.from_env().sync is False
    if sync_off:
        out(f"{ENV_SYNC}=off：写账以后不接着同步")
    return EXIT_ATTENTION if counts else EXIT_OK


def _export(args: argparse.Namespace, graph: neo.NeoGraph, out: Callable[[str], None]) -> int:
    digest, counts = export(graph, Path(args.out) if args.out else None, vectors=not args.no_vectors)
    out("导出：" + "、".join(f"{kind} {count}" for kind, count in sorted(counts.items())))
    out(f"摘要 {digest}")
    return EXIT_OK


def _check(settings: Settings, args: argparse.Namespace, graph: neo.NeoGraph, out: Callable[[str], None]) -> int:
    conn = open_ledger(settings.library(args.library))
    vectors_dir = Path(args.vectors) if args.vectors else settings.require_data_dir() / "vectors"
    try:
        state = read_state(conn)
        if not state.enabled:
            raise GraphNotSetUp("这本账还没接上图：先跑 javmem graph init")
        graph.check_marker(state.marker_id, args.library)
        embedder = FakeEmbeddings() if args.fake_embeddings else None  # 假向量照算；真的只查缓存，不调服务
        with VectorCache(vectors_dir) as cache:
            vectors = RelationVectors(cache, embedder, model=_model_name())
            found = compare(export_lines(graph), expected_lines(conn, tz=settings.timezone, gate=default_gate(), vectors=vectors,
                                                               library=args.library))
    finally:
        conn.close()
    for key, label in (("only_graph", "图里多出来的"), ("only_ledger", "图里少了的"), ("different", "两边不一样的")):
        ids = found[key]
        out(f"{label}：{len(ids)}" + (f"（{'、'.join(ids[:20])}{'……' if len(ids) > 20 else ''}）" if ids else ""))
    return EXIT_ATTENTION if any(found.values()) else EXIT_OK


def _model_name() -> str:
    """向量模型的名字（缓存的键里有它）：照 JAVMEM_EMBEDDING_MODEL，没写是默认的那个。没配向量服务也知道查缓存用哪个。"""
    return os.environ.get(ENV_EMBEDDING_MODEL, "").strip() or DEFAULT_EMBEDDING_MODEL
