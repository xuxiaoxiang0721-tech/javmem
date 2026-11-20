"""写完账、做完管理命令接着同步进图（J5.2、J5.6）。图没开、连不上、标记对不上，写账、管理命令照常成功，欠着的记在账本里，
下次补上。

不抛出来（写账、管理命令的活是账本，账本照常写好了）：结果写进 SyncReport，打一行。没接上图的账本（金标准重建、测试）、
没设密码、JAVMEM_GRAPH_SYNC=off 的，都不去连图。连接给短的超时（JAVMEM_GRAPH_TIMEOUT_SECONDS，默认 5 秒）。

- `after_write`（javmem write 写完账）：拿不到图的锁不等；该整张同步的不做（交给 javmem graph sync）。
- `after_admin`（撤回、彻底删除、解除撤回、冻结、处理待确认、合并拆开，J5.6）：图正在同步就等一会儿（ADMIN_WAIT_SECONDS）；
  该整张同步的时候只同步这次改到的（照现在的样子写、该删的删，整张同步照旧欠着）；图里一条都还没有的不去同步；
  没有欠着的不去连图（`status` 是 `idle`）。只同步欠着的会漏的两种不同步、照实说（要整张同步一遍）：记欠账的触发器
  不全或者是旧的（`untracked`：这次改的不一定都记上了），上一回整张同步断在半路（`unclean`：账本这边记的清了，图里
  可能还有以前的东西）。
- 进图前检查两边都从 `default_gate` 取（J5.3：照 JAVMEM_PRECHECK 选口子）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from javmem.config import ConfigError, Settings
from javmem.embedding import DEFAULT_MODEL, ENV_MODEL, Embedder, EmbeddingSettings, FakeEmbeddings, OpenAIEmbeddings
from javmem.graph import dirty
from javmem.graph.config import GraphSettings
from javmem.graph.gate import Gate
from javmem.graph.sync import (
    GraphBusy,
    GraphNotSetUp,
    GraphState,
    RelationVectors,
    SyncReport,
    graph_lock,
    needs_full,
    read_state,
    sync,
)
from javmem.ledger import open_ledger
from javmem.pipeline.vectors import VectorCache
from javmem.timeutil import iso
from javmem.usage import TokenLedger

ENV_TOKEN_LEDGER = "JAVMEM_TOKEN_LEDGER"
ADMIN_WAIT_SECONDS = 30.0  # 管理命令接着同步：图正在同步（锁被占着）最多等这么久；整张同步要几个小时，等不起
# 管理命令接着同步时，这几种不算没同步成：同步了；没有要同步的；这本账没接图；该整张同步、图里还什么都没有（没有要清的）。
# 别的（图停着、连不上、标记对不上、密码不对、锁等过了、关了同步、记欠账的不全、上一回整张同步断在半路）撤回、彻底删除时
# 要人看（J5.6）
FINE = frozenset({"ok", "idle", "not_set_up", "full_needed"})


def token_ledger_path(settings: Settings) -> Path:
    """和写账记账的是同一份（javmem.cli）。"""
    value = os.environ.get(ENV_TOKEN_LEDGER, "").strip()
    return Path(value) if value else settings.require_data_dir() / "logs" / "token-ledger.csv"


def default_gate() -> Gate:
    """同步进图用哪道口子（J5.3）：开了进图前检查（JAVMEM_PRECHECK=on）只放谁认过的（javmem.pipeline.precheck.PrecheckGate），
    没开是全放（AdmitAll，和以前一样）。javmem write 接着同步、管理命令接着同步（J5.6）、javmem graph sync、check 都用它。"""
    from javmem.pipeline import precheck  # 这里才导：图这一层不用一开始就带上抽取那一套

    return precheck.gate()


def embedding_recorder(settings: Settings, embedder: Embedder | None, tag: str) -> Callable[..., None] | None:
    """关系的向量调本机向量服务也记账（purpose 是 graph_embedding；本机的，不算进上限）。假向量不记。"""
    if embedder is None or isinstance(embedder, FakeEmbeddings):
        return None
    ledger = TokenLedger(token_ledger_path(settings))

    def record(digest: str, attempt: int, status: str, tokens: int, seconds: float, estimated: bool) -> None:
        ledger.append({
            "at": iso(datetime.now(timezone.utc).astimezone()), "tag": tag, "purpose": "graph_embedding",
            "model": embedder.name, "window_id": "", "cache_key": digest, "attempt": attempt, "status": status,
            "input_tokens": tokens, "output_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0, "seconds": round(seconds, 3),
        })

    return record


def after_write(
    settings: Settings,
    library: str,
    *,
    embedder: Embedder | None,
    cache: VectorCache | None,
    gate: Gate | None = None,
    tag: str = "javmem",
    env_graph: GraphSettings | None = None,
) -> SyncReport:
    """写完账接着同步一次。什么情况都不抛出来（Ctrl+C 这类照样往外走），结果在 SyncReport 里（status、message、还欠着几条）。
    gate 不给是 default_gate()（开没开进图前检查照配置）。"""
    try:
        return _sync_now(settings, library, embedder=embedder, cache=cache, gate=gate, tag=tag, env_graph=env_graph)
    except Exception as error:  # noqa: BLE001 - 账本已经写好了：图这一层（连同又打开账本读状态）出什么错都不让写账失败
        report = SyncReport()
        report.status, report.message = "error", f"同步出错（{type(error).__name__}），没同步"
        return report


def after_admin(
    settings: Settings,
    library: str,
    *,
    vectors_dir: Path | None = None,
    gate: Gate | None = None,
    tag: str = "javmem-admin",
    env_graph: GraphSettings | None = None,
    wait: float = ADMIN_WAIT_SECONDS,
) -> SyncReport:
    """管理命令改完账本接着同步一次（J5.6）。和 after_write 一样什么都不抛；vectors_dir 是关系的向量用的向量缓存
    （默认数据目录下的 vectors）。关系的向量：上一次整张同步用的是假向量（试流程的账本）就用假的；不然用向量服务
    （JAVMEM_EMBEDDING_*），没配就照上一次整张同步的那个模型只查缓存、没有的欠着。"""
    try:
        return _sync_now(settings, library, embedder=None, cache=None, gate=gate, tag=tag, env_graph=env_graph,
                         admin=True, vectors_dir=vectors_dir, wait=wait)
    except Exception as error:  # noqa: BLE001 - 管理命令已经做完了：图这一层出什么错都不让它失败，照实说
        report = SyncReport()
        report.status, report.message = "error", f"同步出错（{type(error).__name__}），没同步"
        return report


def purge_note(library: str, *, synced: bool) -> str:
    """彻底删除以后、这本账接着主图时说的那一句（J5.6，和体检库那一句一个说法）。synced：这一次同步成了没有
    （FINE 里的那几种：同步了、没有要同步的、图里还什么都没有）。"""
    first = ("主图：彻底删除的这几版写过的东西，图里已经没有了。" if synced
             else "主图：这一次没同步成（见上一行），同步以后图里才删干净。")
    return (
        first + "可 Neo4j 社区版删了的东西，磁盘上可能还留着（存储文件里空出来的地方、事务日志）；要干净，就把主图整个换一个："
        f"停容器、删它的数据目录、重起，javmem graph init --reset --library {library}，再 javmem graph sync --library {library}。"
        "javmem graph rebuild 只是清空重装，磁盘上照样可能留着"
    )


def vector_model_name() -> str:
    """没配向量服务时关系的向量照哪个模型查缓存（和 javmem graph sync 一样）。"""
    return os.environ.get(ENV_MODEL, "").strip() or DEFAULT_MODEL


def _graph_vector_model(state: GraphState) -> str | None:
    """上一次整张同步时关系的向量是哪个模型算的（graph_state.projection 里 # 后面那一段）；没整张同步过是 None。"""
    model = (state.projection or "").partition("#")[2]
    return model if model and model != "-" else None


def _admin_embedder(state: GraphState) -> Embedder | None:
    """管理命令接着同步时关系的向量用哪个：上一次整张同步用的是假向量（试流程的账本）就用假的；不然用向量服务。
    没配（JAVMEM_EMBEDDING_*），或者服务的模型和图里的不是一个（换了模型、还没整张同步），都是 None：只照图的模型
    查缓存，图里不混两种模型算的向量（复审第 4 条）。"""
    graph_model = _graph_vector_model(state)
    if graph_model == FakeEmbeddings().name:
        return FakeEmbeddings()
    try:
        embedder = OpenAIEmbeddings(EmbeddingSettings.from_env())
    except ConfigError:
        return None
    if graph_model is not None and embedder.name != graph_model:
        return None
    return embedder


def _sync_now(
    settings: Settings,
    library: str,
    *,
    embedder: Embedder | None,
    cache: VectorCache | None,
    gate: Gate | None,
    tag: str,
    env_graph: GraphSettings | None,
    admin: bool = False,
    vectors_dir: Path | None = None,
    wait: float = 0.0,
) -> SyncReport:
    report = SyncReport()
    graph_settings: GraphSettings | None = None
    if admin:
        if not settings.library(library).db_path.is_file():  # 管理命令碰上还没有账本的库：不建一本空的
            report.status, report.message = "not_set_up", "这个库还没有账本，没同步"
            return report
    else:  # 写账：照 J5.2 原样，先读图的设置
        try:
            graph_settings = env_graph if env_graph is not None else GraphSettings.from_env()
        except ConfigError as error:
            report.status, report.message = "config", f"设置不对，没同步：{error}"
            return report
    conn = open_ledger(settings.library(library))
    try:
        state = read_state(conn)
        report.owed_after = int(conn.execute("SELECT count(*) FROM graph_dirty").fetchone()[0])
        if not state.enabled:
            report.status, report.message = "not_set_up", "这本账还没接上图（javmem graph init），没同步"
            return report
        if graph_settings is None:  # 管理命令：接着图的账本才看图的设置，没接图的设置写错了也不说（审阅第 5 条）
            try:
                graph_settings = env_graph if env_graph is not None else GraphSettings.from_env()
            except ConfigError as error:
                report.status, report.message = "config", f"设置不对，没同步：{error}"
                return report
        target = graph_settings.target(library)
        if admin:
            if state.triggers != dirty.TRIGGERS_VERSION or not dirty.triggers_ok(conn):
                # 记欠账的不全：这次改的不一定都记上了，只同步欠着的会漏（审阅第 1 条），要整张同步一遍。图里还什么都
                # 没有的（接上以后还没同步过）没有要清的，不算没同步成（复审第 1 条）
                if state.synced_at is None and conn.execute("SELECT 1 FROM graph_items LIMIT 1").fetchone() is None:
                    report.status = "full_needed"
                    report.message = (f"记欠账的触发器不全或者是旧的，图里还没有东西，这次没同步：跑 javmem graph sync "
                                      f"--library {library}（整张同步一遍）")
                else:
                    report.status = "untracked"
                    report.message = (f"记欠账的触发器不全或者是旧的，这次改的不一定都记上了，没同步：跑 javmem graph sync "
                                      f"--library {library}（会整张同步一遍）")
                return report
            if not report.owed_after:
                report.status, report.message = "idle", "没有要同步的"
                return report
        if not graph_settings.sync:
            report.status, report.message = "disabled", "JAVMEM_GRAPH_SYNC=off，没同步"
            return report
        if not target.password:
            report.status, report.message = "no_password", f"没设 {target.password_name}，没连图"
            return report
        if admin:
            embedder = _admin_embedder(state)
        vector_model = embedder.name if embedder is not None else (
            (_graph_vector_model(state) or vector_model_name()) if admin else None)
        gate = gate if gate is not None else default_gate()
        if not admin:
            reason = needs_full(conn, gate.version, settings.timezone, vector_model)
            if reason is not None:  # 整张的不在写账里装（一直拿着写账的锁，撤回都要等）：交给 javmem graph sync（审阅第 9 条）
                report.status = "full_needed"
                report.message = f"要整张同步（{reason}），写账里不整张装：跑 javmem graph sync --library {library}"
                return report
        try:
            from javmem.graph.neo import GraphAuthError, GraphRefused, GraphUnavailable, NeoGraph
        except ImportError:  # pragma: no cover - neo4j 驱动是主依赖，正常都装着
            report.status, report.message = "no_driver", "没装 neo4j 驱动，没连图"
            return report
        try:
            with graph_lock(settings.require_data_dir(), library, wait=wait):
                if admin:  # 拿到锁再看：等锁的时候别的同步可能刚做完、或者整张重建正做到一半（复审第 2 条）
                    state = read_state(conn)
                    reason = needs_full(conn, gate.version, settings.timezone, vector_model)
                    if reason is not None and conn.execute("SELECT 1 FROM graph_items LIMIT 1").fetchone() is None:
                        if state.rebuilt_at is None and state.synced_at is not None:
                            # 上一回整张同步（清空重装）断在半路：账本这边记的清了，图里可能还有以前的东西，认不出要删什么
                            # （审阅第 2 条）
                            report.status = "unclean"
                            report.message = (f"上一回整张同步没做完，图里可能还有以前的东西，这次没同步：跑 javmem graph sync "
                                              f"--library {library}")
                        else:  # 接上以后还没同步过，或者上一回整张同步完图里就是空的：没有要清的
                            report.status = "full_needed"
                            report.message = (f"要整张同步（{reason}），图里还没有东西，这次没同步：跑 javmem graph sync "
                                              f"--library {library}")
                        report.owed_after, report.owed_since = _owed_counts(conn)
                        return report
                graph = NeoGraph(target, timeout=graph_settings.timeout)
                try:
                    graph.connect()
                    if not admin:
                        vectors = RelationVectors(cache, embedder, record=embedding_recorder(settings, embedder, tag))
                        return sync(conn, graph, library=library, tz=settings.timezone, gate=gate, vectors=vectors)
                    with VectorCache(vectors_dir or settings.require_data_dir() / "vectors") as admin_cache:
                        vectors = RelationVectors(admin_cache, embedder, record=embedding_recorder(settings, embedder, tag),
                                                  model=vector_model)
                        return sync(conn, graph, library=library, tz=settings.timezone, gate=gate, vectors=vectors,
                                    allow_full=False)
                finally:
                    graph.close()
        except GraphBusy:
            report.status = "busy"
            report.message = (f"这个库已经有一个同步在跑（等了 {wait:.0f} 秒），这次没同步" if wait
                              else "这个库已经有一个同步在跑，这次没同步")
        except GraphUnavailable as error:
            report.status, report.message = "unavailable", f"图连不上（{error}），没同步"
        except GraphRefused as error:
            report.status, report.message = "refused", f"一个字没写：{error}"
        except GraphAuthError as error:
            report.status, report.message = "auth", str(error)
        except GraphNotSetUp as error:  # pragma: no cover - 上面看过了
            report.status, report.message = "not_set_up", str(error)
        except Exception as error:  # noqa: BLE001 - 图这一层出什么错都不让写账、管理命令失败，照实说
            report.status, report.message = "error", f"同步出错（{type(error).__name__}），没写完"
        report.owed_after, report.owed_since = _owed_counts(conn)
        return report
    finally:
        conn.close()


def _owed_counts(conn: object) -> tuple[int, str | None]:
    row = conn.execute("SELECT count(*), min(since) FROM graph_dirty").fetchone()  # type: ignore[attr-defined]
    return int(row[0]), row[1]
