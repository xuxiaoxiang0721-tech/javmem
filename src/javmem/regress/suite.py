"""javmem regress 的套件文件（YAML，`format: javmem-regress/1`）：拿哪些包、哪份结果库、哪份金标准、哪些题，基准记在哪。

真数据的套件放仓库外（本机一层）；编的小套件在仓库里（`tests/fixtures/regress-mini/`，GitHub 一层）。路径写相对的，
照套件文件所在的目录算；`repo:` 开头的照这份代码的仓库算（比如 `repo:deploy/graph-test-neo4j/compose.yaml`）。

```yaml
format: javmem-regress/1
name: 本机真数据
config:                      # 每一步都用的设置（来源登记表、身份、主人的名字、别名表、时区……），照环境变量的名字写
  JAVMEM_SOURCES_FILE: config/sources.yaml
  JAVMEM_TIMEZONE: "+08:00"
gold:                        # 金标准：拷一份结果库、照现在的代码重建账本（假向量，不调模型）、打分
  - line: sol                # 线的名字
    env: {JAVMEM_L3_PROMPT: l3-v20, JAVMEM_LLM_MODEL: 某模型}   # 结果库的键照它算；不调模型
    packets: [packets/某个来源/*.md]      # 包（可以写通配），照来源目录的名字放进收件目录
    store: l3/sol.db         # L3 的结果库（只读，拷一份用）；写 .json 的是录好的回复（javmem.regress.replies）
    score:                   # javmem score 的那几样
      gold: 金标准目录
      extra: [盲测目录]
      bridges: [别名表.yaml]
      review: 审阅结果.json   # 可以不写
retrieval:                   # 调试题只检索：照现在的代码重建账本、同步进测试图、每道题三路量证据命中率
  - line: sol
    env: {JAVMEM_L3_PROMPT: l3-v20, JAVMEM_LLM_MODEL: 某模型, JAVMEM_JUDGE_MODE: local+llm}
    packets: [packets/*/*.md]
    store: l3/sol-36.db
    vectors: vectors/sol-36.db     # 向量缓存（只读，拷一份用）；缺的才找本机的向量服务
    fake_embeddings: false         # true：假向量（编的小套件，CI 上没有向量服务）
    questions: questions.json      # javmem.regress.evidence 的题（format javmem-regress-questions/1）
    query_vectors: cache/calls.db  # 问话的向量缓存（可以不写）
    caller: test-owner             # 用哪个身份问
    embedding_model: Qwen/Qwen3-Embedding-4B
graph:                       # 测试图：managed 照 compose 起一个只给回归用的（数据目录每遍清空）；given 用环境变量里配好的那个
  mode: managed              # （空的图，CI 上那一个）；off 不量主图那一路
  compose: repo:deploy/graph-test-neo4j/compose.yaml
  data: graph               # 回归自己的数据目录：不存在、空的，或者有 .javmem-regress-graph 的才用
  container: javmem-graph-test-neo4j
  lock: 锁文件              # 可以不写
baseline: baseline.json      # 基准：上一次过关的分数和命中数（--save-baseline 写）
```
"""

from __future__ import annotations

import glob
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from javmem.config import ConfigError
from javmem.yamlio import load_yaml

FORMAT = "javmem-regress/1"
GRAPH_MODES = ("managed", "given", "off")
# 认得的键：写错了（retrival、golds……）报错，不当成没有这一段
TOP_KEYS = frozenset({"format", "name", "config", "gold", "retrieval", "graph", "baseline"})
GOLD_KEYS = frozenset({"line", "env", "packets", "store", "score"})
SCORE_KEYS = frozenset({"gold", "extra", "bridges", "review", "library"})
RETRIEVAL_KEYS = frozenset({"line", "env", "packets", "store", "questions", "caller", "vectors", "fake_embeddings",
                            "query_vectors", "embedding_model"})
GRAPH_KEYS = frozenset({"mode", "compose", "data", "container", "lock"})
LOCKED = "锁定"  # 题库的锁定区（03 p3：到 U1 才跑一次）：套件里哪一样都不许指到它底下
DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-4B"
FAKE_EMBEDDING_MODEL = "fake-embedding"  # javmem write、graph sync 加 --fake-embeddings 时向量的名字


def repo_root() -> Path:
    import javmem

    return Path(javmem.__file__).resolve().parents[2]


@dataclass(frozen=True)
class Score:
    gold: Path
    extra: tuple[Path, ...] = ()
    bridges: tuple[Path, ...] = ()
    review: Path | None = None
    library: str = "test"


@dataclass(frozen=True)
class GoldLine:
    line: str
    env: Mapping[str, str]
    packets: tuple[Path, ...]
    store: Path
    score: Score


@dataclass(frozen=True)
class RetrievalLine:
    line: str
    env: Mapping[str, str]
    packets: tuple[Path, ...]
    store: Path
    questions: Path
    caller: str
    vectors: Path | None = None
    fake_embeddings: bool = False
    query_vectors: Path | None = None
    embedding_model: str = DEFAULT_EMBEDDING_MODEL


@dataclass(frozen=True)
class GraphSpec:
    mode: str = "off"
    compose: Path | None = None
    data: Path | None = None
    container: str = "javmem-graph-test-neo4j"
    lock: Path | None = None


@dataclass(frozen=True)
class Suite:
    path: Path
    name: str
    config: Mapping[str, str]
    gold: tuple[GoldLine, ...]
    retrieval: tuple[RetrievalLine, ...]
    graph: GraphSpec
    baseline: Path
    notes: tuple[str, ...] = field(default_factory=tuple)


class _Reader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.base = path.parent

    def fail(self, where: str, why: str) -> ConfigError:
        return ConfigError(f"套件 {self.path.name} 的 {where}：{why}")

    def keys(self, value: Mapping[str, object], known: frozenset[str], where: str) -> None:
        unknown = sorted(str(key) for key in value if key not in known)
        if unknown:
            raise self.fail(where, f"不认得 {'、'.join(unknown)}（认得的是 {'、'.join(sorted(known))}）")

    def path_of(self, value: object, where: str, *, must_exist: bool = True) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise self.fail(where, "要写一个路径")
        text = value.strip()
        found = repo_root() / text[5:] if text.startswith("repo:") else Path(text)
        if not found.is_absolute():
            found = self.base / found
        found = found.resolve()  # 带 .. 的照规范的写法（和容器挂载的比得上）
        if LOCKED in found.parts:
            raise self.fail(where, f"指到了{LOCKED}区底下：{LOCKED}区的题到 U1 才跑一次，回归哪一层都不跑")
        if must_exist and not found.exists():
            raise self.fail(where, f"{found} 不在")
        return found

    def optional_path(self, value: object, where: str) -> Path | None:
        return None if value in (None, "") else self.path_of(value, where)

    def paths(self, value: object, where: str) -> tuple[Path, ...]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value:
            raise self.fail(where, "要写一个或几个路径")
        found: list[Path] = []
        for one in value:
            if isinstance(one, str) and any(char in one for char in "*?["):
                pattern = one if Path(one).is_absolute() else str(self.base / one)
                if LOCKED in Path(pattern).parts:  # 先拦，不去列锁定区里的文件
                    raise self.fail(where, f"指到了{LOCKED}区底下")
                matched = sorted(Path(item).resolve() for item in glob.glob(pattern))
                if not matched:
                    raise self.fail(where, f"{one} 一个都没找到")
                if any(LOCKED in item.parts for item in matched):
                    raise self.fail(where, f"指到了{LOCKED}区底下")
                found += matched
            else:
                found.append(self.path_of(one, where))
        return tuple(dict.fromkeys(found))

    def env(self, value: object, where: str) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise self.fail(where, "要写成 名字: 值")
        out = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.startswith("JAVMEM_"):
                raise self.fail(where, f"{key} 不是 JAVMEM_ 开头的设置")
            if key.endswith(("_API_KEY", "_PASSWORD")):
                raise self.fail(where, f"{key} 是密钥：不写进套件，从环境变量里拿")
            if key.endswith(("_URL", "_URI")):
                raise self.fail(where, f"{key} 是地址：不写进套件（本机的服务、测试图照外面的环境变量，回归自己核过才交给子进程）")
            text = "" if item is None else str(item)
            if key.endswith("_FILE") and text:
                text = str(self.path_of(text, f"{where}.{key}"))
            out[key] = text
        return out


def load_suite(path: Path) -> Suite:
    """读套件文件。写错了报 ConfigError（说哪一项、为什么），什么都不跑。"""
    reader = _Reader(path.resolve())
    import yaml

    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"套件 {path} 读不了（{type(error).__name__}）") from None
    except (yaml.YAMLError, ValueError):
        raise ConfigError(f"套件 {path.name} 不是好的 YAML") from None
    if not isinstance(data, Mapping) or data.get("format") != FORMAT:
        raise ConfigError(f"套件 {path.name} 开头要写 format: {FORMAT}")
    reader.keys(data, TOP_KEYS, "开头那一层")
    config = reader.env(data.get("config"), "config")
    gold = []
    for index, one in enumerate(data.get("gold") or []):
        where = f"gold[{index}]"
        if not isinstance(one, Mapping):
            raise reader.fail(where, "要写成一段")
        reader.keys(one, GOLD_KEYS, where)
        score = one.get("score")
        if not isinstance(score, Mapping):
            raise reader.fail(f"{where}.score", "要写 gold、extra、bridges 这几样")
        reader.keys(score, SCORE_KEYS, f"{where}.score")
        gold.append(GoldLine(
            line=str(one.get("line") or f"gold{index + 1}"),
            env=reader.env(one.get("env"), f"{where}.env"),
            packets=reader.paths(one.get("packets"), f"{where}.packets"),
            store=reader.path_of(one.get("store"), f"{where}.store"),
            score=Score(
                gold=reader.path_of(score.get("gold"), f"{where}.score.gold"),
                extra=tuple(reader.path_of(item, f"{where}.score.extra") for item in score.get("extra") or []),
                bridges=tuple(reader.path_of(item, f"{where}.score.bridges") for item in score.get("bridges") or []),
                review=reader.optional_path(score.get("review"), f"{where}.score.review"),
                library=str(score.get("library") or "test"),
            ),
        ))
    retrieval = []
    for index, one in enumerate(data.get("retrieval") or []):
        where = f"retrieval[{index}]"
        if not isinstance(one, Mapping):
            raise reader.fail(where, "要写成一段")
        reader.keys(one, RETRIEVAL_KEYS, where)
        caller = one.get("caller")
        if not isinstance(caller, str) or not caller.strip():
            raise reader.fail(f"{where}.caller", "要写用哪个身份问")
        fake = bool(one.get("fake_embeddings"))
        model = str(one.get("embedding_model") or (FAKE_EMBEDDING_MODEL if fake else DEFAULT_EMBEDDING_MODEL))
        if fake and model != FAKE_EMBEDDING_MODEL:  # 假向量写账、同步时用的就是这个名字：别的名字问话的向量对不上图里的
            raise reader.fail(f"{where}.embedding_model", f"假向量的线要写 {FAKE_EMBEDDING_MODEL}（或者不写）")
        retrieval.append(RetrievalLine(
            line=str(one.get("line") or f"retrieval{index + 1}"),
            env=reader.env(one.get("env"), f"{where}.env"),
            packets=reader.paths(one.get("packets"), f"{where}.packets"),
            store=reader.path_of(one.get("store"), f"{where}.store"),
            questions=reader.path_of(one.get("questions"), f"{where}.questions"),
            caller=caller.strip(),
            vectors=reader.optional_path(one.get("vectors"), f"{where}.vectors"),
            fake_embeddings=fake,
            query_vectors=reader.optional_path(one.get("query_vectors"), f"{where}.query_vectors"),
            embedding_model=model,
        ))
    from javmem.pipeline.extract import PRE_R_PROMPTS

    for one in [*gold, *retrieval]:
        if one.env.get("JAVMEM_L3_PROMPT", "").strip() in PRE_R_PROMPTS:
            raise reader.fail(f"{one.line} 的 env", f"{one.env['JAVMEM_L3_PROMPT']} 写账一次只写一个包，回归还不支持（只写一次，"
                              "量的是一本只有一个包的账）")
    lines = [one.line for one in gold] + [one.line for one in retrieval]
    if not lines:
        raise reader.fail("gold、retrieval", "一条线都没有：跑了也是白跑")
    if len(set(one.line for one in gold)) != len(gold) or len(set(one.line for one in retrieval)) != len(retrieval):
        raise reader.fail("gold、retrieval", f"线的名字重了：{lines}")
    raw_graph = data.get("graph") or {}
    if not isinstance(raw_graph, Mapping):
        raise reader.fail("graph", "要写成一段")
    reader.keys(raw_graph, GRAPH_KEYS, "graph")
    mode = str(raw_graph.get("mode") or "off")
    if mode not in GRAPH_MODES:
        raise reader.fail("graph.mode", f"要写 {'、'.join(GRAPH_MODES)} 之一")
    graph = GraphSpec(mode=mode)
    if mode == "managed":
        graph = GraphSpec(
            mode=mode,
            compose=reader.path_of(raw_graph.get("compose") or "repo:deploy/graph-test-neo4j/compose.yaml", "graph.compose"),
            data=reader.path_of(raw_graph.get("data"), "graph.data", must_exist=False),
            container=str(raw_graph.get("container") or "javmem-graph-test-neo4j"),
            lock=reader.path_of(raw_graph.get("lock"), "graph.lock", must_exist=False) if raw_graph.get("lock") else None,
        )
    if mode == "given" and len(retrieval) > 1:
        raise reader.fail("graph", "given 的图一遍只接一条线（换一本账本要先清空那个图）")
    baseline = reader.path_of(data.get("baseline") or "baseline.json", "baseline", must_exist=False)
    return Suite(path=reader.path, name=str(data.get("name") or path.stem), config=config, gold=tuple(gold),
                 retrieval=tuple(retrieval), graph=graph, baseline=baseline)
