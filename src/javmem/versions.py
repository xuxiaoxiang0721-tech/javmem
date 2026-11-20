"""javmem versions：这一套现在用的全部版本（U2 甲，D205 ⑩⑪）。`--save` 存一份当基准（仓库外），`--check` 和基准比。

列的是（`--json` 是全的）：
- **代码：** javmem 的版本、提交号（从仓库里装的才有）、src 底下有没有没提交的改动；Python 的版本；这个环境里装的每个包和版本。
- **账本：** 代码的账本版本（最后一个迁移），数据目录里两个库的账本是几版。
- **规则、提示词：** L1、L2、渲染、L4、L5、预处理 A、B、现有关系、进图前检查、纠正词表、对话里答待确认、图的样子、
  主图检索这些的版本号；每个提示词的指纹（照现在的文件算的，不是代码里写的那个）。
- **模型：** 要的是哪个（照环境变量，和抽、写账时读的同一套），推理档位、输出方式、走不走 Flex；模型地址是不是官方的
  （OpenAI 的 `https://api.openai.com/v1`、Anthropic 的 `https://api.anthropic.com`；11-17 中转站那次以后的规矩）。
- **服务商实际给的：** 每个要的模型，最近一次回复里写的是哪个（L3 的结果库：抽取、模型复核、判断 C 第二档；软变化的
  `soft.db`；token 账最后一列 `response_model`）。只读，不调模型。
- **探针**（`javmem.probes`，不加 `--no-probe` 才跑，只连本机）：编的 20 句过向量服务、编的 10 道题过本机 Qwen，
  记下来给下一次比。调用照样记 token 账（本机的，不算钱）。没配本机服务的那一样不跑。本机 Qwen 每道连问两遍，对不上的
  当有人在用，隔一会再量；一直对不上的这回没量到（记成 `busy`，D229 ⑫，见 `javmem.probes`）。
- **主图**（加 `--graph` 才查）：两个图的 Neo4j 版本、版本类别、全文索引默认的分析器。没设密码、连不上的照实写。

`--check 基准`：照现在的样子再算一遍，和基准比。不一样的一条一行，退出码 1；一样的退出码 0。
- 两边都量到了才比。基准里有、这次没量到的探针也算不一样（量不到就说不清变没变）；本机 Qwen 一直有别人在用、这回没量到的
  只提一句，不算（D229 ⑫：别当成变了）；基准是旧版探针存的（换了探针版本）只提一句“要重存”，不算；主图没加 `--graph` 的
  只提一句，不算。
- `--only 几样` 只比这几样（逗号隔开，名字见 SECTIONS），比如每周查探针：`--only probes,served,models`。
- 模型地址不是官方的，不管基准怎么写，`--check` 都算不一样（退出码 1）；不加 `--check` 的只打一行提醒。

`--save`、`--check` 不写文件名，用数据目录的 `logs/versions-baseline.json`（巡检认这一份，D205 ⑪）。照这一份 `--check` 完，
在 `logs/versions-status.json` 记一笔（`record_status`）：这一回什么时候、和哪一份基准（存的时刻、文件的指纹）、比了哪几样、
各几处不一样、退出码；探针另记每一个几处、基准里没量到的、两边都没量到而这次出错的、这回有别人在用没量到的、基准是旧版探针
存的是哪几个。巡检照它报探针对不上、太久没查、这回没量到、要重存。
比别的文件的不记。基准和这一笔都先写到旁边再换上去：巡检同一时刻读，读不到写了一半的。本机 Qwen 这回没量到的不存基准
（退出码 2，原来那一份不动；D229 ⑫：重存也要两遍对得上）。

只打版本、名字、指纹和数，不打原文、不打密钥（只说设没设）。`--save` 的文件要在仓库外。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

FORMAT = "javmem-versions/1"
SECTIONS = ("code", "python", "packages", "ledger", "rules", "prompts", "models", "served", "probes", "neo4j")
OFFICIAL_LLM = "https://api.openai.com/v1"
OFFICIAL_SOFT = "https://api.anthropic.com"
EXIT_OK, EXIT_DIFFERENT, EXIT_CONFIG = 0, 1, 2
DEFAULT_TAG = "javmem-versions"
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
BASELINE_FILE = "versions-baseline.json"  # 数据目录 logs/ 下：--save、--check 不写文件名用这一份，巡检认这一份
STATUS_FILE = "versions-status.json"  # 数据目录 logs/ 下：照上面那一份 --check 完记一笔，巡检读它
STATUS_FORMAT = "javmem-versions-status/1"

Out = Callable[[str], None]


# ---------- 代码、Python、包 ----------


def code_info() -> dict[str, object]:
    import javmem

    root = Path(javmem.__file__).resolve().parents[2]
    out: dict[str, object] = {"javmem": javmem.__version__, "commit": None, "dirty": None}
    if (root / ".git").exists() and (root / "src" / "javmem").is_dir():
        env = {key: value for key, value in os.environ.items() if not key.startswith("JAVMEM_")}  # git 用不着密钥
        try:
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, encoding="utf-8",
                                  timeout=30, env=env)
            dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "src"], capture_output=True,
                                   encoding="utf-8", timeout=30, env=env)
        except (OSError, subprocess.SubprocessError):
            return out
        if head.returncode == 0:
            out["commit"] = head.stdout.strip() or None
            out["dirty"] = bool(dirty.stdout.strip()) if dirty.returncode == 0 else None
    return out


def python_info() -> dict[str, object]:
    return {"version": platform.python_version(), "implementation": platform.python_implementation()}


def packages() -> dict[str, str]:
    """这个环境里装的每个包（名字小写、`_` 换成 `-`）和版本；javmem 自己不列（版本在“代码”里）。"""
    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip().lower().replace("_", "-")
        if name and name != "javmem":
            found[name] = dist.version
    return dict(sorted(found.items()))


# ---------- 账本 ----------


def open_readonly(path: Path) -> sqlite3.Connection:
    """只读打开（巡检读结果库也用它）。旁边没有 -wal 的用 immutable（不在旁边留 -shm）。"""
    uri = path.resolve().as_uri()
    wal = path.with_name(path.name + "-wal")
    conn = sqlite3.connect(f"{uri}?mode=ro" if wal.exists() else f"{uri}?immutable=1", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def ledger_info(data_dir: Path | None) -> dict[str, object]:
    from javmem.config import LIBRARY_NAMES
    from javmem.ledger import latest_version

    out: dict[str, object] = {"code": latest_version()}
    for name in LIBRARY_NAMES:
        path = data_dir / "ledger" / f"{name}.db" if data_dir is not None else None
        if path is None or not path.exists():
            out[name] = None
            continue
        try:
            conn = open_readonly(path)
            try:
                out[name] = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as error:
            out[name] = f"读不了（{type(error).__name__}）"
    return out


# ---------- 规则、提示词 ----------


def rules() -> dict[str, str]:
    from javmem.graph.dirty import TRIGGERS_VERSION
    from javmem.graph.project import PROJECTION_VERSION
    from javmem.graph.search import SEARCH_VERSION
    from javmem.pipeline.answers import RULES_VERSION as ANSWERS_VERSION
    from javmem.pipeline.correct import WORDS_VERSION as CORRECT_WORDS_VERSION
    from javmem.pipeline.merge import L5_VERSION
    from javmem.pipeline.normalize import L1_VERSION
    from javmem.pipeline.prea import PRE_A_VERSION
    from javmem.pipeline.preb import PRE_B_VERSION
    from javmem.pipeline.precheck import GATE_VERSION
    from javmem.pipeline.prer import PRE_R_VERSION
    from javmem.pipeline.render import RENDER_VERSION
    from javmem.pipeline.verify import L4_VERSION
    from javmem.pipeline.window import L2_VERSION
    from javmem.soft.find import FIND_VERSION
    from javmem.soft.words import WORDS_VERSION as SOFT_WORDS_VERSION
    from javmem.zh.search import VERSION as SEARCH_NORM_VERSION

    return {
        "L1": L1_VERSION,
        "L2": L2_VERSION,
        "render": RENDER_VERSION,
        "L4": L4_VERSION,
        "L5": L5_VERSION,
        "pre_a": PRE_A_VERSION,
        "pre_b": PRE_B_VERSION,
        "pre_r": PRE_R_VERSION,
        "precheck": GATE_VERSION,
        "correct_words": CORRECT_WORDS_VERSION,
        "answers": ANSWERS_VERSION,
        "soft_find": FIND_VERSION,
        "soft_words": SOFT_WORDS_VERSION,
        "search_norm": SEARCH_NORM_VERSION,
        "graph_projection": PROJECTION_VERSION,
        "graph_triggers": TRIGGERS_VERSION,
        "graph_search": SEARCH_VERSION,
    }


def prompts() -> dict[str, str]:
    """每个提示词的指纹（照现在的文件算）。L3 的每一版都列：抽的时候用哪一版照 JAVMEM_L3_PROMPT（见“模型”）。"""
    from javmem.pipeline import precheck, recheck, reljudge
    from javmem.pipeline.extract import PROMPTS, prompt_fingerprint
    from javmem.samecheck import JUDGE_PROMPT_VERSION as MERGE_VERSION
    from javmem.samecheck import judge_fingerprint as merge_fingerprint
    from javmem.soft.judge import PROMPT_VERSION as SOFT_VERSION
    from javmem.soft.judge import prompt_fingerprint as soft_fingerprint

    out = {f"l3:{version}": prompt_fingerprint(version) for version in sorted(PROMPTS)}
    out[f"recheck:{recheck.RECHECK_PROMPT_VERSION}"] = recheck.recheck_fingerprint()
    for version in sorted(reljudge.JUDGE_PROMPTS):  # 判断 C 的每一版都列：用哪一版跟着 L3 的提示词（c2 讲了隶属于，D210）
        out[f"judge:{version}"] = reljudge.judge_fingerprint(version)
    out[f"precheck:{precheck.FACT_PROMPT_VERSION}"] = precheck.fact_fingerprint()
    out[f"merge:{MERGE_VERSION}"] = merge_fingerprint()
    out[f"soft:{SOFT_VERSION}"] = soft_fingerprint()
    return out


# ---------- 模型 ----------


def is_official(url: str, official: str) -> bool:
    return url.rstrip("/").lower() == official.rstrip("/").lower()


def _local(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "") in _LOOPBACK
    except ValueError:
        return False


def models(env: Mapping[str, str]) -> dict[str, object]:
    """要的是哪个：照环境变量（和抽、写账时读的同一套），写错了的照实写“写错了”，不报配置错误。密钥只说设没设。"""
    from javmem import embedding, llm
    from javmem.config import ConfigError
    from javmem.pipeline import preb, reljudge
    from javmem.pipeline.extract import prompt_from_env
    from javmem.soft import claude

    out: dict[str, object] = {}
    try:
        settings = llm.LLMSettings.from_env(env)
        out["l3"] = {"model": settings.model, "effort": settings.reasoning_effort, "output": settings.structured_output,
                     "tier": settings.service_tier or "standard", "base_url": settings.base_url,
                     "official": is_official(settings.base_url, OFFICIAL_LLM), "key_set": bool(settings.api_key)}
        recheck = llm.LLMSettings.recheck_from_env(env)
        out["recheck"] = {"model": recheck.model, "effort": recheck.reasoning_effort}
    except ConfigError as error:
        out["l3"] = {"error": str(error)}
    try:
        out["l3_prompt"] = prompt_from_env(env)
    except ConfigError as error:
        out["l3_prompt"] = f"写错了：{error}"
    try:
        soft = claude.SoftSettings.from_env(env)
        out["soft"] = {"model": soft.model, "effort": soft.effort, "base_url": soft.base_url,
                       "official": is_official(soft.base_url, OFFICIAL_SOFT), "key_set": bool(soft.api_key)}
    except ConfigError as error:
        out["soft"] = {"error": str(error)}
    if env.get(embedding.ENV_BASE_URL, "").strip():
        try:  # 照写账时那样核一遍：写错了（带账号密码、? 后面的参数……）只说写错了，地址不打
            found = embedding.EmbeddingSettings.from_env(env)
            out["embedding"] = {"model": found.model, "base_url": found.base_url, "local": _local(found.base_url)}
        except ConfigError as error:
            out["embedding"] = {"error": str(error)}
    else:
        out["embedding"] = {"model": env.get(embedding.ENV_MODEL, "").strip() or embedding.DEFAULT_MODEL, "base_url": None,
                            "local": None}
    out["pre_b"] = {"model": env.get(preb.ENV_MODEL, "").strip() or preb.DEFAULT_MODEL,
                    "url_set": bool(env.get(preb.ENV_URL, "").strip())}
    out["judge"] = {"mode": env.get(reljudge.ENV_MODE, "").strip() or "off",
                    "model": env.get(reljudge.ENV_MODEL, "").strip() or reljudge.DEFAULT_MODEL,
                    "url_set": bool(env.get(reljudge.ENV_URL, "").strip())}
    out["precheck"] = env.get("JAVMEM_PRECHECK", "").strip() or "off"
    return out


def unofficial(found: Mapping[str, object]) -> list[str]:
    """模型地址不是官方的那几样（11-17 中转站那次以后：跑真的之前要看是不是官方的）。"""
    out = []
    for name in ("l3", "soft"):
        one = found.get(name)
        if isinstance(one, Mapping) and one.get("official") is False:
            out.append(f"{name} 的模型地址不是官方的：{one.get('base_url')}")
    return out


# ---------- 服务商实际给的 ----------


def _when(text: object) -> datetime:
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _seen(found: dict[str, dict[str, object]], wanted: str, name: str | None, at: object, where: str) -> None:
    from javmem.usage import clean_model

    name = clean_model(name)
    if not wanted or not name:
        return
    one = found.setdefault(wanted, {"name": None, "at": None, "from": None, "names": []})
    names: list[str] = one["names"]  # type: ignore[assignment]
    if name not in names:
        names.append(name)
    if one["at"] is None or _when(at) >= _when(one["at"]):
        one.update(name=name, at=str(at), **{"from": where})


def served(stores: Iterable[Path], soft_stores: Iterable[Path], ledger: Path | None) -> dict[str, object]:
    """每个要的模型（L3 写成 名字@档位/输出方式），最近一次回复里写的是哪个、在哪看到的、一共见过哪几个名字。"""
    found: dict[str, dict[str, object]] = {}
    read: list[str] = []
    for path in stores:
        if not path.exists():
            continue
        try:
            conn = open_readonly(path)
        except sqlite3.Error:
            continue
        try:
            readable = False
            for table in ("results", "rechecks", "judgments"):
                try:
                    rows = conn.execute(
                        f"SELECT model, response_model, max(created_at) AS at FROM {table}"  # noqa: S608 - 表名是上面写死的
                        " WHERE response_model IS NOT NULL GROUP BY model, response_model").fetchall()
                except sqlite3.Error:
                    continue
                readable = True
                for row in rows:
                    _seen(found, row["model"], row["response_model"], row["at"], f"{path.name}:{table}")
            if readable:  # 打开了、一张表都没读成的（不是结果库）不算读到了
                read.append(str(path))
        finally:
            conn.close()
    for path in soft_stores:
        if not path.exists():
            continue
        try:
            conn = open_readonly(path)
            try:
                rows = conn.execute("SELECT model, response_model, max(created_at) AS at FROM verdicts"
                                    " WHERE response_model IS NOT NULL GROUP BY model, response_model").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for row in rows:
            _seen(found, row["model"], row["response_model"], row["at"], f"{path.name}:verdicts")
        read.append(str(path))
    if ledger is not None and ledger.exists():
        from javmem.usage import TokenLedger

        for row in TokenLedger(ledger).rows():
            if row.get("response_model"):
                _seen(found, row.get("model") or "", row["response_model"], row.get("at"), ledger.name)
        read.append(str(ledger))
    return {"read": read, "models": dict(sorted(found.items()))}


# ---------- 探针 ----------


def _probe_recorder(ledger: Path | None, tag: str) -> Callable[[object], None] | None:
    if ledger is None:
        return None
    from javmem.usage import TokenLedger

    book = TokenLedger(ledger)

    def record(call: object) -> None:
        book.append({"at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), "tag": tag,
                     "purpose": call.purpose, "model": call.model, "window_id": f"probe#{call.index}",  # type: ignore[attr-defined]
                     "cache_key": "", "attempt": getattr(call, "attempt", 1), "status": call.status,  # type: ignore[attr-defined]
                     "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,  # type: ignore[attr-defined]
                     "reasoning_tokens": 0, "cached_tokens": 0, "seconds": round(call.seconds, 3)})  # type: ignore[attr-defined]

    return record


def run_probes(env: Mapping[str, str], record: Callable[[object], None] | None,
               embedder: object | None = None, local: object | None = None,
               say: Callable[[str], None] | None = None) -> dict[str, object]:
    """两个探针。embedder、local 不给就照环境变量连本机的服务；没配的那一样写 skipped，连不上的写 error（只有种类），本机 Qwen
    一直有别人在用的写 busy（D229 ⑫）。say：Qwen 探针要隔一会再量时说一句。"""
    from javmem import probes
    from javmem.config import ConfigError
    from javmem.embedding import EmbeddingError, EmbeddingSettings, OpenAIEmbeddings
    from javmem.pipeline import preb, reljudge

    out: dict[str, object] = {}
    if embedder is None:
        try:
            settings = EmbeddingSettings.from_env(env)
        except ConfigError as error:
            out["embedding"] = {"skipped": str(error)}
        else:
            if _local(settings.base_url):
                embedder = OpenAIEmbeddings(settings)
            else:
                out["embedding"] = {"skipped": "向量服务不是本机的：探针只连本机"}
    if embedder is not None:
        try:
            out["embedding"] = probes.embed_probe(embedder, record)  # type: ignore[arg-type]
        except EmbeddingError as error:
            out["embedding"] = {"error": error.kind}
        except Exception as error:  # noqa: BLE001 - 连接断了、回的不对……：记成没量到（只有种类），不崩
            out["embedding"] = {"error": type(error).__name__}
    if local is None:
        url = env.get(reljudge.ENV_URL, "").strip() or env.get(preb.ENV_URL, "").strip()
        name = env.get(reljudge.ENV_MODEL, "").strip() or env.get(preb.ENV_MODEL, "").strip() or reljudge.DEFAULT_MODEL
        if not url:
            out["qwen"] = {"skipped": f"没配 {reljudge.ENV_URL}（或 {preb.ENV_URL}）"}
        elif not _local(url):
            out["qwen"] = {"skipped": "本机模型的地址不是本机的：探针只连本机"}
        else:  # 关掉提示缓存（D229 ⑫）：缓存里有人在用时算歪的那段系统提示，不留着给这一回接着用
            local = preb.LocalModel(url, name, prompt_cache=False, timeout=120.0)
    if local is not None:
        try:
            out["qwen"] = probes.qwen_probe(local, record, say=say)
        except Exception as error:  # noqa: BLE001 - 回到一半断了（http.client 那一类）、回的不是 JSON……：记成没量到，不崩
            out["qwen"] = {"error": type(error).__name__}
    return out


# ---------- 主图 ----------


def neo4j_info(env: Mapping[str, str]) -> dict[str, object]:
    from javmem.config import ConfigError, LIBRARY_NAMES
    from javmem.graph.config import GraphSettings
    from javmem.graph.neo import GraphAuthError, GraphUnavailable, NeoGraph

    try:
        settings = GraphSettings.from_env(env, check_sync=False)
    except ConfigError as error:
        return {"error": str(error)}
    out: dict[str, object] = {}
    for name in LIBRARY_NAMES:
        target = settings.target(name)
        if not target.password:
            out[name] = {"skipped": f"没设 {target.password_name}"}
            continue
        try:
            with NeoGraph(target, timeout=5.0) as graph:
                graph.connect()
                rows = graph.query("CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition")
                config = graph.config()
        except GraphAuthError:
            out[name] = {"error": "账号、密码不对"}
            continue
        except GraphUnavailable as error:
            out[name] = {"error": f"连不上（{error}）"}
            continue
        kernel = next((row for row in rows if row.get("name") == "Neo4j Kernel"), rows[0] if rows else {})
        versions = kernel.get("versions") or []
        out[name] = {"uri": target.uri, "version": versions[0] if versions else None, "edition": kernel.get("edition"),
                     "analyzer": config.get("db.index.fulltext.default_analyzer")}
    return out


# ---------- 一份全的 ----------


def _data_dir(env: Mapping[str, str]) -> Path | None:
    from javmem.config import ENV_DATA_DIR

    text = env.get(ENV_DATA_DIR, "").strip()
    return Path(text) if text else None


def data_stores(data: Path) -> tuple[Path, Path]:
    """数据目录里的 L3 结果库、软变化的 soft.db：“服务商实际给的”读这两个，巡检也读这两个。"""
    return data / "l3" / "l3.db", data / "soft" / "soft.db"


def token_ledger(env: Mapping[str, str]) -> Path | None:
    """和 javmem 别的命令记账记在同一个文件：JAVMEM_TOKEN_LEDGER，没写的是数据目录下的 logs/token-ledger.csv。"""
    text = env.get("JAVMEM_TOKEN_LEDGER", "").strip()
    if text:
        return Path(text)
    data = _data_dir(env)
    return data / "logs" / "token-ledger.csv" if data is not None else None


def snapshot(
    env: Mapping[str, str] | None = None,
    *,
    probe: bool = True,
    graph: bool = False,
    stores: Iterable[Path] = (),
    soft_stores: Iterable[Path] = (),
    tag: str = DEFAULT_TAG,
    embedder: object | None = None,
    local: object | None = None,
    say: Callable[[str], None] | None = None,
) -> dict[str, object]:
    env = os.environ if env is None else env
    data = _data_dir(env)
    ledger = token_ledger(env)
    l3_store, soft_store = data_stores(data) if data is not None else (None, None)
    all_stores = [*stores, *([l3_store] if l3_store is not None else [])]
    all_soft = [*soft_stores, *([soft_store] if soft_store is not None else [])]
    found_models = models(env)
    return {
        "format": FORMAT,
        "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "code": code_info(),
        "python": python_info(),
        "packages": packages(),
        "ledger": ledger_info(data),
        "rules": rules(),
        "prompts": prompts(),
        "models": found_models,
        "served": served(dict.fromkeys(all_stores), dict.fromkeys(all_soft), ledger),
        "probes": run_probes(env, _probe_recorder(ledger, tag), embedder, local, say=say) if probe else {"skipped": "--no-probe"},
        "neo4j": neo4j_info(env) if graph else {"skipped": "没加 --graph"},
    }


# ---------- 比 ----------


def _measured(value: object) -> bool:
    return isinstance(value, Mapping) and not ({"skipped", "error", "busy"} & set(value))


def _busy(value: object) -> bool:
    """这回没量到，因为一直有别人在用（本机 Qwen，D229 ⑫）：--check 只提一句，不算不一样。"""
    return isinstance(value, Mapping) and "busy" in value


def _old_version(saved: object, now: object) -> bool:
    """两边都量到了，可基准是旧版探针存的（比如 pq1，这次是 pq2）：比不了，要重存，不是变了（D229 ⑫）。"""
    return _measured(saved) and _measured(now) and saved.get("version") != now.get("version")  # type: ignore[union-attr]


def _dict_changes(label: str, before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    out = []
    for key in sorted(set(before) | set(after)):
        if key not in after:
            out.append(f"{label}：{key} 没了（原来 {before[key]}）")
        elif key not in before:
            out.append(f"{label}：多了 {key}（{after[key]}）")
        elif before[key] != after[key]:
            out.append(f"{label}：{key} {before[key]} → {after[key]}")
    return out


Changes = tuple[list[str], list[str]]  # (不一样的, 只提一句的)


def _dict_section(label: str, key: str) -> Callable[[Mapping[str, object], Mapping[str, object]], Changes]:
    def changes(saved: Mapping[str, object], now: Mapping[str, object]) -> Changes:
        return _dict_changes(label, saved.get(key) or {}, now.get(key) or {}), []  # type: ignore[arg-type]

    return changes


def _models_changes(saved: Mapping[str, object], now: Mapping[str, object]) -> Changes:
    different = []
    before, after = saved.get("models") or {}, now.get("models") or {}
    for key in sorted(set(before) | set(after)):  # type: ignore[arg-type]
        one, two = before.get(key), after.get(key)  # type: ignore[union-attr]
        if one != two:
            different.append(f"模型：{key} {json.dumps(one, ensure_ascii=False)} → {json.dumps(two, ensure_ascii=False)}")
    different += unofficial(after)  # type: ignore[arg-type]
    return different, []


def _served_changes(saved: Mapping[str, object], now: Mapping[str, object]) -> Changes:
    different: list[str] = []
    notes: list[str] = []
    before = (saved.get("served") or {}).get("models") or {}  # type: ignore[union-attr]
    after = (now.get("served") or {}).get("models") or {}  # type: ignore[union-attr]
    if before and not (now.get("served") or {}).get("read"):  # type: ignore[union-attr]
        different.append(f"服务商实际给的：这次一个结果库、账都没读到（基准里有 {len(before)} 个模型），说不清变没变")
    for key in sorted(set(before) - set(after)):
        if (now.get("served") or {}).get("read"):  # type: ignore[union-attr]
            notes.append(f"服务商实际给的：基准里有 {key}，这次没读到")
    for key in sorted(set(before) & set(after)):
        if before[key].get("name") != after[key].get("name"):
            different.append(f"服务商实际给的：要 {key}，最近一次回的从 {before[key].get('name')} 变成 {after[key].get('name')}"
                             f"（{after[key].get('at')}，{after[key].get('from')}）")
    for key in sorted(set(after) - set(before)):
        notes.append(f"服务商实际给的：基准里没有 {key}（现在最近一次回的是 {after[key].get('name')}）")
    return different, notes


def probe_changes(saved: Mapping[str, object], now: Mapping[str, object]) -> dict[str, Changes]:
    """每一个探针（embedding、qwen）各比各的。"""
    from javmem import probes

    before, after = saved.get("probes") or {}, now.get("probes") or {}
    out: dict[str, Changes] = {}
    for key, check in (("embedding", probes.compare_embed), ("qwen", probes.compare_qwen)):
        one, two = before.get(key), after.get(key)  # type: ignore[union-attr]
        different: list[str] = []
        notes: list[str] = []
        if _busy(two):  # 一直有别人在用：说不清变没变，可也不是量不了（D229 ⑫：别当成变了）
            notes.append(f"探针 {key}：这回没量到（{two.get('busy')}），不算不一样：空的时候再 --check")  # type: ignore[union-attr]
        elif not _measured(one):
            if _measured(two):
                notes.append(f"探针 {key}：基准里没量到，这次量到了（重新 --save 才有得比）")
        elif not _measured(two):
            why = (two or {}).get("skipped") or (two or {}).get("error") if isinstance(two, Mapping) else after.get("skipped")  # type: ignore[union-attr]
            different.append(f"探针 {key}：这次没量到（{why}），说不清变没变")
        elif _old_version(one, two):
            notes.append(f"探针 {key}：基准是旧版探针（{one.get('version')}）存的，这次是 {two.get('version')}，"  # type: ignore[union-attr]
                         "比不了：要重存（javmem versions --save）")
        else:
            different += check(one, two)  # type: ignore[arg-type]
        out[key] = (different, notes)
    return out


def _probes_changes(saved: Mapping[str, object], now: Mapping[str, object]) -> Changes:
    found = probe_changes(saved, now).values()
    return [line for different, _ in found for line in different], [line for _, notes in found for line in notes]


def _neo4j_changes(saved: Mapping[str, object], now: Mapping[str, object]) -> Changes:
    different: list[str] = []
    notes: list[str] = []
    before, after = saved.get("neo4j") or {}, now.get("neo4j") or {}
    if not _measured(after) and "skipped" in after:  # type: ignore[operator]
        if any(_measured(value) for value in before.values()):  # type: ignore[union-attr]
            notes.append("主图：这次没查（加 --graph 才查）")
    else:
        for key in sorted(set(before) | set(after)):  # type: ignore[arg-type]
            one, two = before.get(key), after.get(key)  # type: ignore[union-attr]
            if _measured(one) and _measured(two) and one != two:
                different.append(f"主图 {key}：{json.dumps(one, ensure_ascii=False)} → {json.dumps(two, ensure_ascii=False)}")
            elif _measured(one) and not _measured(two):
                different.append(f"主图 {key}：这次没量到（{json.dumps(two, ensure_ascii=False)}）")
    return different, notes


_SECTION_CHANGES: dict[str, Callable[[Mapping[str, object], Mapping[str, object]], Changes]] = {
    "code": _dict_section("代码", "code"),
    "python": _dict_section("Python", "python"),
    "packages": _dict_section("包", "packages"),
    "ledger": _dict_section("账本版本", "ledger"),
    "rules": _dict_section("规则", "rules"),
    "prompts": _dict_section("提示词", "prompts"),
    "models": _models_changes,
    "served": _served_changes,
    "probes": _probes_changes,
    "neo4j": _neo4j_changes,
}


def compare_sections(saved: Mapping[str, object], now: Mapping[str, object], only: Iterable[str] = SECTIONS) -> dict[str, Changes]:
    """比了的每一样（照 SECTIONS 的先后）各自的 (不一样的, 只提一句的)。基准的格式不对的只有一样 format。"""
    only = set(only)
    if saved.get("format") != FORMAT:
        return {"format": ([f"基准的格式不对（{saved.get('format')}），要 {FORMAT}：重新 --save"], [])}
    return {name: _SECTION_CHANGES[name](saved, now) for name in SECTIONS if name in only}


def compare(saved: Mapping[str, object], now: Mapping[str, object], only: Iterable[str] = SECTIONS) -> Changes:
    """(不一样的, 只提一句的)。不一样的有一条就该退出码 1。"""
    found = compare_sections(saved, now, only).values()
    return [line for different, _ in found for line in different], [line for _, notes in found for line in notes]


# ---------- 记一笔（巡检读） ----------


def default_baseline(env: Mapping[str, str]) -> Path | None:
    """--save、--check 不写文件名时用的那一份：数据目录的 logs/versions-baseline.json。没设数据目录的是 None。"""
    data = _data_dir(env)
    return data / "logs" / BASELINE_FILE if data is not None else None


def baseline_digest(raw: bytes) -> str:
    """一份基准文件的指纹（照文件的字节算）：--check 记下比的是哪一份，巡检照它认。重新存一份不一样的，指纹就变了。"""
    return hashlib.sha256(raw).hexdigest()


def _write_beside(path: Path, text: str) -> OSError | None:
    """先写到旁边（.part），再换上去：读的人（巡检）不会读到写了一半的。换不上的删掉旁边那份，返回出的错。"""
    part = path.with_name(path.name + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part.write_text(text, encoding="utf-8")
        os.replace(part, path)
    except OSError as error:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return error
    return None


def record_status(path: Path, saved: Mapping[str, object], digest: str, found: Mapping[str, object],
                  sections: Mapping[str, Changes], only: Iterable[str], measured: Iterable[str], code: int) -> str | None:
    """照数据目录那一份基准 --check 完，在 logs/versions-status.json 记一笔（巡检读它）：这一回什么时候、和哪一份基准
    （存的时刻、文件的指纹 digest）、比了哪几样、各几处不一样、退出码；比了、也量了的那几样（measured：--no-probe 的探针、
    没加 --graph 的主图不算）各记最近一回，这一回没比的留着上一回的。探针另记每一个几处不一样、基准里没量到（这次量到了：
    比不了）的、两边都没量到而这次是出错的（unmeasured：--check 不算它不一样，巡检说一声）、这次一直有别人在用没量到的
    （busy：D229 ⑫，不算不一样，巡检说这回没量到）、两边都量到可基准是旧版探针存的（old_version：比不了，巡检说要重存）
    是哪几个。只有名字和数，不抄 --check 打的那几行。记不上不算比出错：返回一句话给人看，记上了返回 None。"""
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    kept = old.get("sections") if isinstance(old, dict) else None
    entries: dict[str, object] = dict(kept) if isinstance(kept, dict) else {}
    at, base_at = found.get("at"), saved.get("at")
    counts = {name: len(different) for name, (different, _) in sections.items()}
    measured = set(measured)
    for name, count in counts.items():
        if name not in measured:
            continue
        entry: dict[str, object] = {"at": at, "baseline_at": base_at, "baseline_sha256": digest, "different": count}
        if name == "probes":
            before, after = saved.get("probes") or {}, found.get("probes") or {}
            detail = probe_changes(saved, found)
            entry["probes"] = {key: len(different) for key, (different, _) in detail.items()}
            entry["no_baseline"] = [key for key in detail
                                    if not _measured(before.get(key)) and _measured(after.get(key))]  # type: ignore[union-attr]
            entry["unmeasured"] = [key for key in detail if not _measured(before.get(key))  # type: ignore[union-attr]
                                   and isinstance(after.get(key), Mapping) and "error" in after.get(key)]  # type: ignore[union-attr, operator]
            entry["busy"] = [key for key in detail if _busy(after.get(key))]  # type: ignore[union-attr]
            entry["old_version"] = [key for key in detail if _old_version(before.get(key), after.get(key))]  # type: ignore[union-attr]
        entries[name] = entry
    only = set(only)
    status = {
        "format": STATUS_FORMAT,
        "last": {"at": at, "baseline_at": base_at, "baseline_sha256": digest,
                 "only": [name for name in SECTIONS if name in only], "exit": code, "different": counts},
        "sections": entries,
    }
    error = _write_beside(path, json.dumps(status, ensure_ascii=False, indent=1) + "\n")
    if error is not None:
        return f"logs/{STATUS_FILE} 记不上（{type(error).__name__}）：巡检看不到这一回"
    return None


# ---------- 打印 ----------


def _short(digest: object) -> str:
    return str(digest)[:12] if digest else "?"


def describe(found: Mapping[str, object]) -> list[str]:
    code = found["code"]
    lines = [f"javmem versions（{found['at']}）"]
    commit = code.get("commit")  # type: ignore[union-attr]
    dirty = code.get("dirty")  # type: ignore[union-attr]
    lines.append(f"代码：{code.get('javmem')}，提交 {commit[:12] if commit else '（不是从仓库装的，看不到）'}"  # type: ignore[union-attr]
                 + ("（src 有没提交的改动）" if dirty else ""))
    python = found["python"]
    lines.append(f"Python {python['version']}（{python['implementation']}）；装着的包 {len(found['packages'])} 个"  # type: ignore[index, arg-type]
                 "（--json 看全的）")
    ledger = found["ledger"]
    lines.append("账本：代码 {code} 版；".format(code=ledger["code"])  # type: ignore[index]
                 + "、".join(f"{name} {ledger.get(name) if ledger.get(name) is not None else '（没有）'}"  # type: ignore[union-attr]
                            for name in ("prod", "test")))
    lines.append("规则：" + "、".join(f"{key} {value}" for key, value in found["rules"].items()))  # type: ignore[union-attr]
    lines.append("提示词：" + "、".join(f"{key} {_short(value)}" for key, value in found["prompts"].items()))  # type: ignore[union-attr]
    lines.append("模型（要的）：")
    for key, value in found["models"].items():  # type: ignore[union-attr]
        lines.append(f"  {key}：{json.dumps(value, ensure_ascii=False)}")
    for warning in unofficial(found["models"]):  # type: ignore[arg-type]
        lines.append(f"  ！{warning}")
    served_models = found["served"]["models"]  # type: ignore[index]
    lines.append(f"服务商实际给的（最近一次，读了 {len(found['served']['read'])} 个文件）：" + ("" if served_models else "没有"))  # type: ignore[index, arg-type]
    for key, value in served_models.items():
        others = [name for name in value["names"] if name != value["name"]]
        lines.append(f"  {key} → {value['name']}（{value['at']}，{value['from']}）"
                     + (f"；以前还回过 {'、'.join(others)}" if others else ""))
    probes_found = found["probes"]
    if "skipped" in probes_found:  # type: ignore[operator]
        lines.append(f"探针：没跑（{probes_found['skipped']}）")  # type: ignore[index]
    else:
        for key in ("embedding", "qwen"):
            one = probes_found.get(key) or {}  # type: ignore[union-attr]
            if _measured(one):
                count = len(one.get("vectors") or one.get("answers") or [])
                lines.append(f"探针 {key}：{one.get('model')}，{count} 条，版本 {one.get('version')}"
                             + (f"，指纹 {_short(one.get('sha256'))}" if one.get("sha256") else "")
                             + (f"，每道问两遍最大差 {one.get('spread')}（量了 {one.get('tries')} 回）" if "tries" in one else ""))
            elif _busy(one):
                lines.append(f"探针 {key}：这回没量到（{one.get('busy')}）")
            else:
                lines.append(f"探针 {key}：没量到（{one.get('skipped') or one.get('error')}）")
    neo = found["neo4j"]
    if "skipped" in neo:  # type: ignore[operator]
        lines.append(f"主图：没查（{neo['skipped']}）")  # type: ignore[index]
    else:
        for key, value in neo.items():  # type: ignore[union-attr]
            lines.append(f"主图 {key}：{json.dumps(value, ensure_ascii=False)}")
    return lines


# ---------- 命令行 ----------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="输出 JSON（全的）")
    parser.add_argument("--save", nargs="?", const="", metavar="文件",
                        help=f"存一份当基准（仓库外）；不写文件名存到数据目录的 logs/{BASELINE_FILE}（巡检认这一份）")
    parser.add_argument("--check", nargs="?", const="", metavar="文件",
                        help="和 --save 存的基准比：不一样的一条一行，退出码 1；不写文件名比数据目录那一份，"
                             f"比完记一笔 logs/{STATUS_FILE}（巡检看）")
    parser.add_argument("--only", metavar="几样", help=f"--check 只比这几样，逗号隔开：{','.join(SECTIONS)}")
    parser.add_argument("--no-probe", action="store_true", help="不跑探针（不连本机的向量服务、Qwen）")
    parser.add_argument("--graph", action="store_true", help="也查主图、测试图的 Neo4j 版本和分析器（要设了密码、图开着）")
    parser.add_argument("--store", action="append", default=[], metavar="l3.db",
                        help="另外读这个 L3 结果库里服务商实际给的模型名（数据目录里的那个自己会读），可以写几次")
    parser.add_argument("--soft-store", action="append", default=[], metavar="soft.db", help="另外读这个软变化的 soft.db，可以写几次")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="探针的调用记进 token 账用的标签")


def _store_path(text: str) -> Path:
    path = Path(text)
    return path / "l3.db" if path.is_dir() else path


def _to_stderr(text: str) -> None:
    """等着的时候说一句（本机 Qwen 有人在用，最多等 5 分钟）：打到 stderr，--json 的输出照旧只有 JSON。"""
    print(f"（{text}）", file=sys.stderr, flush=True)


def _same_file(one: Path, two: Path) -> bool:
    try:
        return os.path.samefile(one, two)
    except OSError:
        return False


def run(args: argparse.Namespace, out: Out = print) -> int:
    from javmem.config import ENV_DATA_DIR
    from javmem.scoring.run import inside_repo

    if args.save is not None and args.check is not None:
        out("--save 和 --check 一次只做一样")
        return EXIT_CONFIG
    default = default_baseline(os.environ)
    given = args.save if args.save is not None else args.check
    target = None
    if given is not None:  # 不写文件名（""）的是数据目录那一份
        target = Path(given) if given else default
        if target is None:
            out(f"没写文件名：要设 {ENV_DATA_DIR}（基准放数据目录的 logs/{BASELINE_FILE}，巡检认这一份），或者写一个仓库外的文件")
            return EXIT_CONFIG
    if args.save is not None and inside_repo(target):  # type: ignore[arg-type]
        out("没存：基准要放仓库外（里面有本机的设置和模型名）")
        return EXIT_CONFIG
    only = set(SECTIONS)
    if args.only:
        only = {part.strip() for part in args.only.split(",") if part.strip()}
        unknown = only - set(SECTIONS)
        if not only:
            out(f"--only 没写要比哪几样（认得的是：{','.join(SECTIONS)}）")
            return EXIT_CONFIG
        if unknown or args.check is None:
            out(f"--only 只配 --check 用，认得的是：{','.join(SECTIONS)}" + (f"（不认得：{','.join(sorted(unknown))}）" if unknown else ""))
            return EXIT_CONFIG
    saved = None
    digest = ""
    if args.check is not None:
        try:
            raw = target.read_bytes()  # type: ignore[union-attr]
            digest = baseline_digest(raw)
            saved = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            out(f"数据目录里还没有基准（logs/{BASELINE_FILE}）：先 javmem versions --save" if not args.check
                else "基准读不了：FileNotFoundError")
            return EXIT_CONFIG
        except (OSError, ValueError) as error:
            out(f"基准读不了：{type(error).__name__}")
            return EXIT_CONFIG
        if not isinstance(saved, dict):
            out("基准不是 javmem versions --save 存的")
            return EXIT_CONFIG
    probe = not args.no_probe and (saved is None or "probes" in only)
    found = snapshot(probe=probe, graph=args.graph, stores=[_store_path(one) for one in args.store],
                     soft_stores=[Path(one) for one in args.soft_store], tag=args.tag, say=_to_stderr)
    if args.json:
        out(json.dumps(found, ensure_ascii=False, indent=1))
    else:
        for line in describe(found):
            out(line)
    if args.save is not None:  # 先写到旁边再换上去：巡检同一时刻读，不会读到写了一半的
        busy = [key for key, value in found["probes"].items() if _busy(value)]  # type: ignore[union-attr]
        if busy:  # 重存基准也照“两遍对得上”：对不上就不存，原来那一份不动（D229 ⑫）
            out(f"没存：探针 {'、'.join(busy)} 这回没量到（一直有别人在用），存下的基准里就没有它；空的时候再 --save")
            return EXIT_CONFIG
        error = _write_beside(target, json.dumps(found, ensure_ascii=False, indent=1) + "\n")  # type: ignore[arg-type]
        if error is not None:
            out(f"没存上（{type(error).__name__}）：{target}")
            return EXIT_CONFIG
        out(f"存好了：{target}")
        return EXIT_OK
    if saved is None:
        return EXIT_OK
    sections = compare_sections(saved, found, only)
    different = [line for lines, _ in sections.values() for line in lines]
    for note in (note for _, notes in sections.values() for note in notes):
        out(f"（{note}）")
    if different:
        out(f"和基准（{saved.get('at')}）不一样的 {len(different)} 处：")
        for line in different:
            out(f"  - {line}")
    else:
        out(f"和基准（{saved.get('at')}）一样" + (f"（只比了 {','.join(s for s in SECTIONS if s in only)}）" if args.only else ""))
    code = EXIT_DIFFERENT if different else EXIT_OK
    if default is not None and _same_file(target, default):  # type: ignore[arg-type]  # 巡检只认数据目录那一份
        measured = set(SECTIONS) - ({"probes"} if not probe else set()) - ({"neo4j"} if not args.graph else set())
        problem = record_status(default.with_name(STATUS_FILE), saved, digest, found, sections, only, measured, code)
        if problem is not None:
            out(problem)
    return code


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(prog="javmem versions")
    add_arguments(parser)
    sys.exit(run(parser.parse_args()))
