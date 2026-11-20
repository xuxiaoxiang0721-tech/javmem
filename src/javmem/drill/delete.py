"""javmem drill delete：删除演练（U2 乙，D205 ③④，做法第 2 节）。只在拷贝上做，原来那个数据目录一个字不改。

一遍做这几样：

1. **拷一份：** `--from` 拿锁拷到 `--to/data`（`common.copy_data_dir`）。`--to` 要还不存在，或者是空的；不能在仓库里、不能和
   `--from` 套着。
2. **挑一个包：** `--packet 包号`，或者 `--pick most`（牵连最多：别的包的窗口拿它当上文的最多、接着它的包最多、出自它的
   事实最多）、`--pick random`（`--seed`）、`--pick month`（按月轮着撤：第几个月撤排第几的包，10 个包 10 个月一轮，D205 ④）。
   邮件包也演（U3）：U2 乙那时候先不演，是怕“撤回一封信以后，同一往来串里别的包写过的说法清不掉”；这一处 J5.6 以后
   撤回时就清掉了（整包撤回、只撤回那封信两种，tests/test_withdraw_thread.py 钉着）。
3. **撤回以前记下三样**（只在内存里，不写进文件）：
   - 样本句：这个包里 12 个字以上、别的包（两个库的原文句和统一写法、归档文件）、别名表、主人名字配置里都没有的话，
     均匀地取最多 30 句；
   - 只出自它的事实、关系的编号；
   - 只有它说过的名字：证据只在它里面的别名、它包头的名字，去掉别的包里也有的、别名表和主人名字配置里写着的、不到两个字的。
   另外记下别的包拿它当上文的窗口、那些窗口里别的包写的说法、只出自它的文字（向量缓存那一层比）、现在关系的向量。
4. **接图**（`--graph given|managed`，只给测试库）：拷贝的账本接到一个空的测试图上（`graph init --reset`、整张同步）。
   `given` 用环境变量里写明的那个图（不能是默认的测试图口 7689），`managed` 照 compose 起一个演练自己的（照共享笔记拿锁）。
   不加的：主图那一层不查，撤回、彻底删除不去同步（`JAVMEM_GRAPH_SYNC=off`），连拷贝的账本记着的那个原来的图都不连。
5. **撤回以前先问一遍查询接口**（样本句、名字、它碰到的实体）：问得到，撤回以后问不到才算数。
6. **撤回**（`javmem withdraw`），逐层查；加了 `--purge` 再**彻底删除**（`javmem purge`），再逐层查一遍。
7. **输出：** 每一层一行，只有编号和数；结果写进 `--to/drill-delete.json`；最后一行“删除演练：过”或者“没过：哪一层几处”。
   退出码 0 过，1 没过，2 跑不了。

每一层拿什么当“查不到”：

| 层 | 撤回以后 | 彻底删除以后另外还要 |
| --- | --- | --- |
| 查询接口 | 看得到全部的身份拿样本句、名字问 raw_search、memory_search，名字对得上的实体、它碰到的实体问 entity_card、timeline、relation_at：回来的出处没有这个包，字里没有样本句、名字 | 同左 |
| 原文层 | 包登记都标了撤回；归档还在（默认只标记） | 原文句 0 句、归档文件 0 个、跳过的副本 0 个、包登记都记了彻底删除 |
| 账本 | 证据、关系证据、说法、隔离、待确认里指着它的 0；只出自它的事实、关系 0；只有它说过的名字在别名、标准名里 0；拿它当上文的窗口里别的包的说法 0 | 同左 |
| 结果库 | 有它的窗口 0（拿它当上文的也算）；样本句、名字在哪张表里都是 0；它的包号只许在调用记录里 | 同左 |
| 向量 | 不再用的文字在向量缓存里 0；删了的事实的向量 0；还在的关系的向量一条不少 | 同左 |
| 主图（接了图的） | `javmem graph check` 两边对得上；只出自它的编号在图里 0；全文索引按样本句、名字查 0 | 只报不判：社区版磁盘上可能还留着 |
| 体检库（跑过支线的） | 支线库里这一组的原文行 0，组要么删了、要么记了待清（演练不连体检库：拷贝的支线库记着原来那份的标记） | 同左 |
| 逐字节搜 | SQLite 按表里的字判：样本句、名字只许在这个库的原文层（包登记、原文句和它们的全文索引）；别的文件只许在它的归档里。空页、日志里还剩的照实列出来，不算没过（彻底删除时整理掉） | 每个文件逐字节都是 0（账本、结果库、向量、soft.db、side.db、日志、跳过的副本）；只剩指纹和包号 |

收件目录、`failed/` 里碰上的单列出来，不算没过（打包方重发的下次收件跳过、再 purge 删；`failed/` 照现在的规矩不删）。
逐字节搜不出来的：全文索引是拆开存的，靠彻底删除时那一遍合并索引、逐层数句子。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from javmem.config import TEST, ConfigError, Settings
from javmem.drill.common import (
    EMBEDDING_PASS,
    EXIT_CANNOT,
    EXIT_FAIL,
    EXIT_PASS,
    SIDECARS,
    CannotRun,
    Needle,
    Out,
    chunked,
    contains_any,
    copy_data_dir,
    drill_env,
    is_sqlite,
    needles_of,
    quiet,
    run_javmem,
    scan_tables,
    scan_tree,
    scoped_env,
    tally,
    token_rows,
)

FORMAT = "javmem-drill-delete/1"
SAMPLES = 30
MIN_CHARS = 12
CALLER = "javmem-drill"
RESULT_FILE = "drill-delete.json"
LOG_FILE = "commands.log"
PICKS = ("most", "random", "month")
GRAPH_MODES = ("given", "managed")
LAYERS = ("query", "raw", "ledger", "store", "vectors", "graph", "side", "bytes")
LAYER_NAMES = {"query": "查询接口", "raw": "原文层", "ledger": "账本", "store": "结果库", "vectors": "向量", "graph": "主图",
               "side": "体检库", "bytes": "逐字节搜"}
STAGE_NAMES = {"withdraw": "撤回以后", "purge": "彻底删除以后"}
# 撤回以后（还没彻底删除）样本句、名字还许在的表：这个库的原文层（包登记、原文句，加上它们的全文索引的影子表）
RAW_TABLES = frozenset({"packets", "raw_sentences"})
RAW_PREFIXES = ("raw_fts", "raw_norm_fts")
# 结果库里只有编号和数、撤回不删的：调用记录（只有状态和 token）、跑的记录
STORE_ID_TABLES = frozenset({"calls", "recheck_calls", "judgment_calls", "runs"})
TIME_FROM = "1900-01-01"
TIMEOUT = 30


# ---------- 参数 ----------


@dataclass
class Plan:
    source: Path
    root: Path
    library: str
    packet: str | None
    pick: str
    seed: int | None
    purge: bool
    graph: str | None
    graph_data: Path | None
    graph_lock: Path | None
    graph_compose: Path | None
    graph_container: str
    wait: float

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def log(self) -> Path:
        return self.root / LOG_FILE


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="source", required=True, help="原来的数据目录（只读：拿锁拷一份，一个字不改）")
    parser.add_argument("--to", dest="target", required=True, help="新的空目录（仓库外）：拷贝放 data/，结果、日志也放这里")
    parser.add_argument("--library", choices=("prod", "test"), required=True, help="演哪个库")
    parser.add_argument("--packet", help="撤哪个包（对话包、邮件包都行）")
    parser.add_argument("--pick", choices=PICKS, help="不点名时怎么挑：most 牵连最多（默认）、random 随机、month 按月轮着撤")
    parser.add_argument("--seed", type=int, help="--pick random 的种子（不写随便挑一个，结果里记着）")
    parser.add_argument("--purge", action="store_true", help="撤回、查完以后再彻底删除，再查一遍")
    parser.add_argument("--graph", choices=GRAPH_MODES,
                        help="接测试图查主图那一层（只给测试库）：given 用环境变量里写明的那个空图，managed 照 compose 起一个演练自己的")
    parser.add_argument("--graph-data", help="managed：演练自己的测试图数据目录（不存在、空的，或者是演练、回归自己的）")
    parser.add_argument("--graph-lock", help="managed：测试图的锁文件（本机几个会话共用测试图的约定），可以不写")
    parser.add_argument("--graph-compose", help="managed：compose 文件，默认仓库里的 deploy/graph-test-neo4j/compose.yaml（容器名照它认）")
    parser.add_argument("--wait", type=float, default=60.0, help="原来那个数据目录正忙时最多等几秒（拿锁）")


def plan_from(args: argparse.Namespace, env: Mapping[str, str]) -> Plan:
    from javmem.scoring.run import inside_repo

    source = Path(args.source).resolve()
    root = Path(args.target).resolve()
    if not (source / "ledger").is_dir():
        raise CannotRun(f"--from 不是数据目录（没有 ledger）：{source}")
    if not (source / "ledger" / f"{args.library}.db").is_file():
        raise CannotRun(f"--from 里没有 {args.library} 库的账本")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise CannotRun(f"--to 要是新的空目录：{root} 已经有东西了")
    if root == source or source in root.parents or root in source.parents:
        raise CannotRun("--to 和 --from 不能套着")
    if inside_repo(root):
        raise CannotRun("--to 在仓库里：拷贝里有原文，要放仓库外")
    if args.packet and args.pick:
        raise CannotRun("--packet 和 --pick 只写一个")
    if args.seed is not None and args.pick != "random":
        raise CannotRun("--seed 只给 --pick random 用")
    graph_data = graph_lock = compose = None
    container = ""
    if args.graph:
        if args.library != TEST:
            raise CannotRun("主图那一层只在测试库上演（D205 ④：正式库那一份不接图）")
        if args.graph == "given":
            uri = env.get("JAVMEM_GRAPH_TEST_NEO4J_URI", "").strip()
            if not uri:
                raise CannotRun("--graph given 要在环境变量 JAVMEM_GRAPH_TEST_NEO4J_URI 里写明那个空图的地址（不写就是默认的测试图口，"
                                "本机几个会话共用的那一个：要用它就加 --graph managed，照锁的约定起）")
            if _shared_test_port(uri):
                raise CannotRun(f"--graph given 不能是本机共用的测试图口 {SHARED_TEST_PORT}（怎么写都算）：要用它就加 --graph managed")
            if not env.get("JAVMEM_GRAPH_TEST_NEO4J_PASSWORD", "").strip():
                raise CannotRun("--graph given 要设 JAVMEM_GRAPH_TEST_NEO4J_PASSWORD")
        else:
            if not args.graph_data:
                raise CannotRun("--graph managed 要写 --graph-data（演练自己的测试图数据目录）")
            if not env.get("JAVMEM_GRAPH_TEST_NEO4J_PASSWORD", "").strip():
                raise CannotRun("--graph managed 要设 JAVMEM_GRAPH_TEST_NEO4J_PASSWORD（新图的密码，graph init 改成它）")
            graph_data = Path(args.graph_data).resolve()
            graph_lock = Path(args.graph_lock).resolve() if args.graph_lock else None
            compose = Path(args.graph_compose).resolve() if args.graph_compose else _repo_compose()
            if inside_repo(graph_data):
                raise CannotRun("--graph-data 在仓库里：要放仓库外")
            container = compose_container(compose)
    elif args.graph_data or args.graph_lock or args.graph_compose:
        raise CannotRun("--graph-data、--graph-lock、--graph-compose 要和 --graph managed 一起写")
    return Plan(source=source, root=root, library=args.library, packet=args.packet, pick=args.pick or "most",
                seed=args.seed, purge=args.purge, graph=args.graph, graph_data=graph_data, graph_lock=graph_lock,
                graph_compose=compose, graph_container=container, wait=args.wait)


SHARED_TEST_PORT = 7689  # 本机几个会话共用的测试图（测试库的真数据）：given 不接它


def _shared_test_port(uri: str) -> bool:
    """地址是不是本机共用的测试图口：按主机、端口的数认（localhost、[::1]、127.x、07689 这些写法都算）；认不出的也算。"""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(uri.strip())
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return True
    if port is None:
        return True
    try:
        address = ipaddress.ip_address(host)
        local = address.is_loopback or address.is_unspecified  # 127.x、::1 的各种写法（[0::1]、[::0:1]……）、0.0.0.0
    except ValueError:
        local = host == "localhost" or host.endswith(".localhost")
    return local and port == SHARED_TEST_PORT


def compose_container(compose: Path) -> str:
    """compose 文件里那个图的容器名（照它认容器，不让人写：写错了会当成“没有这个容器”，把别人开着的图停掉）。"""
    from javmem.yamlio import load_yaml

    try:
        data = load_yaml(compose.read_text(encoding="utf-8"))
        names = [str(service.get("container_name")) for service in (data.get("services") or {}).values()
                 if isinstance(service, dict) and service.get("container_name")]
    except (OSError, ValueError, AttributeError):
        names = []
    if len(names) != 1:
        raise CannotRun(f"{compose} 里认不出唯一的一个容器名（container_name）：不起图")
    return names[0]


def _repo_compose() -> Path:
    import javmem

    return Path(javmem.__file__).resolve().parents[2] / "deploy" / "graph-test-neo4j" / "compose.yaml"


# ---------- 读拷贝 ----------


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _one(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _marks(items: Iterable[object]) -> str:
    return ",".join("?" for _ in items)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ledgers(data: Path) -> list[Path]:
    return [path for path in (data / "ledger" / "prod.db", data / "ledger" / "test.db") if path.is_file()]


def _manifest_packets(manifest: str) -> tuple[set[str], set[str]]:
    """窗口清单里的包：(全部, 正文那几个)。"""
    try:
        items = json.loads(manifest)
    except ValueError:
        return set(), set()
    every = {str(item.get("packet_id")) for item in items if isinstance(item, dict) and item.get("packet_id")}
    bodies = {str(item.get("packet_id")) for item in items
              if isinstance(item, dict) and str(item.get("ref", "")).startswith("T") and item.get("packet_id")}
    return every, bodies


# ---------- 挑包 ----------


def eligible(conn: sqlite3.Connection) -> list[str]:
    """能演的包：现行、没撤回、没彻底删除、有原文句的包（对话包、邮件包都算）。"""
    return [str(row[0]) for row in conn.execute(
        "SELECT p.packet_id FROM packets AS p WHERE p.state = 'current' AND p.withdrawn_at IS NULL AND p.purged_at IS NULL"
        " AND EXISTS (SELECT 1 FROM raw_sentences AS s WHERE s.packet_id = p.packet_id"
        " AND s.revision = p.revision) ORDER BY p.packet_id")]


def pick_packet(plan: Plan, conn: sqlite3.Connection, store: Path, today: date) -> tuple[str, dict[str, object]]:
    """(包号, 怎么挑的)。"""
    candidates = eligible(conn)
    if plan.packet:
        row = conn.execute("SELECT channel, state, withdrawn_at, purged_at FROM packets WHERE packet_id = ? ORDER BY"
                           " revision DESC LIMIT 1", (plan.packet,)).fetchone()
        if row is None:
            raise CannotRun(f"拷贝的 {plan.library} 库里没有这个包：{plan.packet}")
        if plan.packet not in candidates:
            raise CannotRun(f"{plan.packet} 不能演：不是现行的、撤回了、彻底删除了，或者没有原文句")
        return plan.packet, {"by": "packet"}
    if not candidates:
        raise CannotRun(f"拷贝的 {plan.library} 库里没有能演的对话包")
    if plan.pick == "random":
        seed = plan.seed if plan.seed is not None else random.SystemRandom().randrange(1, 10**6)
        return random.Random(seed).choice(candidates), {"by": "random", "seed": seed, "candidates": len(candidates)}
    if plan.pick == "month":
        index = (today.year * 12 + today.month - 1) % len(candidates)
        return candidates[index], {"by": "month", "month": f"{today:%Y-%m}", "index": index, "candidates": len(candidates)}
    context: Counter[str] = Counter()
    if store.is_file():
        sconn = _ro(store)
        try:
            for (manifest,) in sconn.execute("SELECT manifest FROM windows"):
                every, bodies = _manifest_packets(manifest)
                for packet in every - bodies:
                    context[packet] += 1
        finally:
            sconn.close()
    followers = Counter(str(row[0]) for row in conn.execute(
        "SELECT continues FROM packets WHERE state = 'current' AND continues IS NOT NULL"))
    facts = Counter({str(row[0]): int(row[1]) for row in conn.execute(
        "SELECT packet_id, count(DISTINCT fact_id) FROM evidence WHERE fact_id IS NOT NULL GROUP BY packet_id")})
    best = sorted(candidates, key=lambda packet: (-context[packet], -followers[packet], -facts[packet], packet))[0]
    return best, {"by": "most", "context_windows": context[best], "continued_by": followers[best], "facts": facts[best],
                  "candidates": len(candidates)}


# ---------- 撤回以前记下 ----------


@dataclass
class Before:
    packet: str
    revisions: list[int]
    raw_sentences: int
    sentences: list[str]  # 样本句：只在内存里
    sentence_ids: list[str]
    names: list[str]  # 只有它说过的名字：只在内存里
    facts_only: set[str]
    relations_only: set[str]
    facts_with_it: int
    relations_with_it: int
    named_entities: dict[str, str]  # 名字 → 撤回以前它对得上的实体（只出自它的别名）
    touched_entities: set[str]  # 它的事实、关系、别名碰到的实体
    context_windows: set[tuple[str, str]]  # 别的包拿它当上文的窗口
    context_claims: set[str]  # 那些窗口里别的包写的说法
    store_windows: int  # 结果库里有它的窗口（正文、上文都算）
    texts: set[str]  # 两个库里事实、说法用着的文字（向量缓存那一层比）
    relation_vectors: dict[str, str]  # 关系 → 拿来算向量的那一句的指纹（撤回以前缓存里有的）
    archives: list[str]  # 它的归档、跳过的副本（相对数据目录）
    side_groups: list[str]
    queries: dict[str, Any] = field(default_factory=dict)
    graph: dict[str, Any] = field(default_factory=dict)
    dead_relations: set[str] = field(default_factory=set)  # 只出自它的关系拿来算向量的那一句的指纹（撤回以后不该留在缓存里）

    def counts(self) -> dict[str, object]:
        """写进结果文件的：只有数和编号，没有字。"""
        return {
            "packet": self.packet, "revisions": self.revisions, "raw_sentences": self.raw_sentences,
            "sample_sentences": len(self.sentences), "names_only_it": len(self.names),
            "facts_only_it": len(self.facts_only), "relations_only_it": len(self.relations_only),
            "facts_with_it": self.facts_with_it, "relations_with_it": self.relations_with_it,
            "entities_named": len(self.named_entities), "entities_touched": len(self.touched_entities),
            "context_windows": len(self.context_windows), "context_claims": len(self.context_claims),
            "store_windows": self.store_windows, "relation_vectors": len(self.relation_vectors),
            "archives": len(self.archives), "side_groups": len(self.side_groups),
            "queries": self.queries, "graph": self.graph,
        }


def _own_files(data: Path, conn: sqlite3.Connection, packet: str) -> list[str]:
    """它的归档（每一版）、跳过的副本（按包头认，名字可能加了 _2），相对数据目录。"""
    from javmem.packet import Rejected, parse_packet

    files = {str(row[0]) for row in conn.execute("SELECT archive_path FROM packets WHERE packet_id = ?", (packet,)) if row[0]}
    for folder in ("archive", "archive-test"):
        skipped = data / folder / "skipped"
        if not skipped.is_dir():
            continue
        for path in sorted(skipped.iterdir()):
            if not path.is_file() or not path.name.startswith(packet):
                continue
            try:
                header = parse_packet(path.read_bytes(), filename=f"{packet}.md").header
            except (OSError, Rejected, ValueError):
                continue
            if header.packet_id == packet:
                files.add(path.relative_to(data).as_posix())
    return sorted(files)


def _corpus(data: Path, packet: str, own: Sequence[str]) -> list[str]:
    """别处的字：两个库里别的包的原文句（连统一写法），归档目录里不是它的文件。挑样本句、名字时用（别处有的不算）。"""
    texts: list[str] = []
    for ledger in _ledgers(data):
        conn = _ro(ledger)
        try:
            for text, norm in conn.execute("SELECT text, norm FROM raw_sentences WHERE packet_id != ?", (packet,)):
                texts.append(str(text or ""))
                texts.append(str(norm or ""))
        finally:
            conn.close()
    mine = set(own)
    for folder in ("archive", "archive-test"):
        base = data / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.relative_to(data).as_posix() not in mine:
                texts.append(path.read_bytes().decode("utf-8", errors="replace"))
    return chunked(texts)


def _config_texts(settings: Settings) -> list[str]:
    """别名表、主人名字配置的原文（配了的名字不算“只有它说过的”）。读不了的当没有。"""
    out = []
    for path in (settings.lexicon_file, settings.owner_file):
        if path is not None and path.is_file():
            try:
                out.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                pass
    return out


def _spread(items: Sequence[Any], count: int) -> list[Any]:
    """均匀地取 count 个（头尾都取到）。"""
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[0]]
    return [items[round(index * (len(items) - 1) / (count - 1))] for index in range(count)]


def _elsewhere(text: str, corpus: Sequence[str], config: Sequence[str]) -> bool:
    from javmem.zh.search import LANE, searchable

    if contains_any(corpus, text) or any(text in one for one in config):
        return True
    return any(part and contains_any(corpus, part) for part in searchable(text).split(LANE))


def record_before(data: Path, library: str, packet: str, settings: Settings) -> Before:
    ledger = data / "ledger" / f"{library}.db"
    conn = _ro(ledger)
    try:
        revisions = [int(row[0]) for row in conn.execute("SELECT revision FROM packets WHERE packet_id = ? ORDER BY revision",
                                                          (packet,))]
        raw = [(str(row["sentence_id"]), str(row["text"] or "")) for row in conn.execute(
            "SELECT sentence_id, text FROM raw_sentences WHERE packet_id = ? ORDER BY id", (packet,))]
        own = _own_files(data, conn, packet)
        corpus = _corpus(data, packet, own)
        config = _config_texts(settings)
        seen: set[str] = set()
        picked: list[tuple[str, str]] = []
        for sentence_id, text in raw:
            clean = text.strip()
            if len(clean) < MIN_CHARS or clean in seen:
                continue
            seen.add(clean)
            if not _elsewhere(clean, corpus, config):
                picked.append((sentence_id, clean))
        samples = _spread(picked, SAMPLES)

        def only_it(table: str, key: str) -> set[str]:
            rows = conn.execute(f"SELECT {key}, group_concat(DISTINCT packet_id) FROM {table} WHERE {key} IS NOT NULL GROUP BY {key}")
            return {str(row[0]) for row in rows if set(str(row[1]).split(",")) == {packet}}

        facts_only = only_it("evidence", "fact_id")
        relations_only = only_it("relation_evidence", "relation_id")
        alias_ids = only_it("evidence", "alias_id")
        facts_with_it = _one(conn, "SELECT count(DISTINCT fact_id) FROM evidence WHERE packet_id = ? AND fact_id IS NOT NULL", packet)
        relations_with_it = _one(conn, "SELECT count(DISTINCT relation_id) FROM relation_evidence WHERE packet_id = ?", packet)
        named: dict[str, str] = {}
        names: list[str] = []
        other_aliases = {str(row[0]) for row in conn.execute(
            "SELECT a.alias FROM aliases AS a JOIN evidence AS v ON v.alias_id = a.alias_id WHERE v.packet_id != ?", (packet,))}
        found = []
        if alias_ids:
            found = [(str(row["alias"]), str(row["entity_id"])) for row in conn.execute(
                f"SELECT alias, entity_id FROM aliases WHERE alias_id IN ({_marks(alias_ids)}) ORDER BY alias", sorted(alias_ids))]
        header = conn.execute("SELECT entities FROM packets WHERE packet_id = ? ORDER BY revision DESC LIMIT 1", (packet,)).fetchone()
        try:
            header_names = [str(one) for one in json.loads(header[0] or "[]")] if header is not None else []
        except ValueError:
            header_names = []
        for name, entity in [*found, *((one, "") for one in header_names)]:
            clean = name.strip()
            if len(clean) < 2 or clean in other_aliases or clean in names or _elsewhere(clean, corpus, config):
                continue
            names.append(clean)
            if entity:
                named[clean] = entity
        touched = {str(row[0]) for row in conn.execute(
            "SELECT fe.entity_id FROM fact_entities AS fe JOIN evidence AS v ON v.fact_id = fe.fact_id WHERE v.packet_id = ?"
            " UNION SELECT r.subject_entity_id FROM relations AS r JOIN relation_evidence AS e ON e.relation_id = r.relation_id"
            " WHERE e.packet_id = ? UNION SELECT r.object_entity_id FROM relations AS r JOIN relation_evidence AS e"
            " ON e.relation_id = r.relation_id WHERE e.packet_id = ? UNION SELECT a.entity_id FROM aliases AS a"
            " JOIN evidence AS v ON v.alias_id = a.alias_id WHERE v.packet_id = ?", (packet, packet, packet, packet)) if row[0]}
    finally:
        conn.close()
    store = data / "l3" / "l3.db"
    context: set[tuple[str, str]] = set()
    store_windows = 0
    if store.is_file():
        sconn = _ro(store)
        try:
            for row in sconn.execute("SELECT window_id, cache_key, manifest FROM windows"):
                every, bodies = _manifest_packets(row["manifest"])
                if packet in every:
                    store_windows += 1
                    if packet not in bodies:
                        context.add((str(row["window_id"]), str(row["cache_key"])))
        finally:
            sconn.close()
    context_claims: set[str] = set()
    texts: set[str] = set()
    for path in _ledgers(data):
        lconn = _ro(path)
        try:
            texts |= {str(row[0]) for row in lconn.execute("SELECT content FROM facts UNION SELECT content FROM fact_claims")
                      if row[0]}
            if path == ledger and context:
                windows = sorted({window for window, _ in context})
                for table in ("fact_claims", "relation_claims"):
                    context_claims |= {f"{table}:{row[0]}" for row in lconn.execute(
                        f"SELECT claim_id FROM {table} WHERE window_id IN ({_marks(windows)})", windows)}
        finally:
            lconn.close()
    return Before(
        packet=packet, revisions=revisions, raw_sentences=len(raw), sentences=[text for _, text in samples],
        sentence_ids=[sentence_id for sentence_id, _ in samples], names=names, facts_only=facts_only,
        relations_only=relations_only, facts_with_it=facts_with_it, relations_with_it=relations_with_it,
        named_entities=named, touched_entities=touched, context_windows=context, context_claims=context_claims,
        store_windows=store_windows, texts=texts, relation_vectors=_relation_vectors(data, settings), archives=own,
        side_groups=_side_groups(data, library, packet),
        dead_relations=_dead_relation_vectors(data, library, settings, relations_only),
    )


def _dead_relation_vectors(data: Path, library: str, settings: Settings, relations: set[str]) -> set[str]:
    """只出自它的那几条关系拿来算向量的那一句的指纹（接没接图都算）：撤回以后它们没了，向量缓存里不该还有（审阅第 2 条）。"""
    from javmem.graph.project import Projector

    if not relations:
        return set()
    conn = _ro(data / "ledger" / f"{library}.db")
    try:
        items = Projector(conn, settings.timezone).relations(sorted(relations))
    finally:
        conn.close()
    return {_sha(item.vector_text) for item in items.values() if getattr(item, "vector_text", None)}


def _cached(data: Path) -> set[str]:
    path = data / "vectors" / "vectors.db"
    if not path.is_file():
        return set()
    conn = _ro(path)
    try:
        return {str(row[0]) for row in conn.execute("SELECT text_sha256 FROM vectors")}
    finally:
        conn.close()


def _relation_vectors(data: Path, settings: Settings, *, cached_only: bool = True) -> dict[str, str]:
    """两个库里现在每条关系拿来算向量的那一句的指纹。cached_only：只留缓存里有的（撤回以前记下这些，撤回以后要一条不少）。"""
    from javmem.graph.project import Projector

    cached = _cached(data)
    out: dict[str, str] = {}
    if cached_only and not cached:
        return out
    for path in _ledgers(data):
        conn = _ro(path)
        try:
            # 撤回只给接着主图的库留关系的向量（withdraw._relation_texts）：没接图的本来就不留，不算
            state = conn.execute("SELECT enabled FROM graph_state WHERE id = 1").fetchone()
            if state is None or not state[0]:
                continue
            ids = [str(row[0]) for row in conn.execute("SELECT relation_id FROM relations")]
            if not ids:
                continue
            for relation_id, item in Projector(conn, settings.timezone).relations(ids).items():
                text = getattr(item, "vector_text", None)
                if text and (not cached_only or _sha(text) in cached):
                    out[f"{path.stem}:{relation_id}"] = _sha(text)
        finally:
            conn.close()
    return out


def _side_groups(data: Path, library: str, packet: str) -> list[str]:
    path = data / "side" / library / "side.db"
    if not path.is_file():
        return []
    conn = _ro(path)
    try:
        return [str(row[0]) for row in conn.execute("SELECT group_id FROM groups WHERE packet_id = ? ORDER BY group_id", (packet,))]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


# ---------- 查询接口 ----------


def _leaks(text: str, before: Before) -> dict[str, int]:
    """一问回来的结果里还有几处是它的：它的出处（包号、句子编号）、只出自它的事实和关系（编号：内容是 L3 照它写的，
    证据看不到、名字换掉了也照样是它的）、样本句、只有它说过的名字。"""
    packet = before.packet
    return {
        "refs": text.count(f'"packet_id": "{packet}"') + text.count(f'"{packet}#'),
        "ids": sum(text.count(f'"{item}"') for item in before.facts_only | before.relations_only),
        "sentences": sum(text.count(sentence) for sentence in before.sentences),
        "names": sum(text.count(name) for name in before.names),
    }


def probe_queries(before: Before, settings: Settings, *, today: date) -> dict[str, Any]:
    """拿样本句、名字、它碰到的实体问一遍五个工具（看得到全部的身份）。回 {calls, skipped, errors, leaks, by_tool}：
    leaks 是回来的结果里这个包的出处、样本句、名字一共几处。"""
    from javmem.query.tools import RELATION_TYPES, call_tool

    asks: list[tuple[str, dict[str, object]]] = []
    for sentence in before.sentences:
        asks.append(("raw_search", {"query": "".join(sentence.split())[:40], "limit": 20}))
        asks.append(("memory_search", {"query": sentence[:40], "limit": 20}))
    for name in before.names:
        if len("".join(name.split())) >= 2:
            asks.append(("raw_search", {"query": "".join(name.split()), "limit": 20}))
        asks.append(("memory_search", {"query": name, "limit": 20}))
        asks.append(("entity_card", {"entity": name}))
    entities = sorted(set(before.named_entities.values()) | before.touched_entities)
    for entity in entities:
        asks.append(("entity_card", {"entity_id": entity, "recent_limit": 20}))
        asks.append(("timeline", {"entity_id": entity, "time_from": TIME_FROM, "limit": 100}))
        for relation in RELATION_TYPES:
            asks.append(("relation_at", {"entity_id": entity, "relation": relation, "at": today.isoformat()}))
    for name in before.named_entities:
        asks.append(("timeline", {"entity": name, "time_from": TIME_FROM, "limit": 100}))
    out: dict[str, Any] = {"calls": 0, "skipped": 0, "errors": 0, "leaks": {"refs": 0, "ids": 0, "sentences": 0, "names": 0},
                           "by_tool": {}}
    for tool, arguments in asks:
        out["calls"] += 1
        try:
            result = call_tool(tool, arguments, settings=settings)
        except Exception as error:  # noqa: BLE001 - 一问出错就算这一问没问成，照实数
            out["errors"] += 1
            out.setdefault("error_kinds", Counter())[type(error).__name__] += 1
            continue
        if not result.get("ok", True):
            code = str((result.get("error") or {}).get("code"))
            if code in ("invalid_argument", "query_too_short"):
                out["skipped"] += 1
            else:
                out["errors"] += 1
                out.setdefault("error_kinds", Counter())[code] += 1
            continue
        found = _leaks(json.dumps(result, ensure_ascii=False), before)
        tool_counts = out["by_tool"].setdefault(tool, {"calls": 0, "leaks": 0})
        tool_counts["calls"] += 1
        tool_counts["leaks"] += sum(found.values())
        for key, value in found.items():
            out["leaks"][key] += value
    if "error_kinds" in out:
        out["error_kinds"] = dict(out["error_kinds"])
    return out


# ---------- 主图 ----------


def _neo(library: str) -> Any:
    from javmem.graph.config import GraphSettings
    from javmem.graph.neo import NeoGraph

    graph = NeoGraph(GraphSettings.from_env(check_sync=False).target(library), timeout=TIMEOUT)
    graph.connect()
    return graph


def probe_graph(before: Before, library: str) -> dict[str, Any]:
    """图里还有几样：只出自它的编号；全文索引按名字、样本句查得到、字里真带着它的。"""
    out: dict[str, Any] = {"ids": 0, "names": 0, "sentences": 0, "errors": 0}
    graph = _neo(library)
    try:
        graph.await_indexes(120)
        ids = sorted(before.facts_only | before.relations_only)
        if ids:
            out["ids"] = int(graph.query("MATCH (n) WHERE n.id IN $ids RETURN count(n) AS n", ids=ids)[0]["n"])
            out["ids"] += int(graph.query("MATCH ()-[r]->() WHERE r.id IN $ids RETURN count(r) AS n", ids=ids)[0]["n"])
        node_indexes = ("jm_alias_name", "jm_entity_name", "jm_fact_text")
        for kind, texts, indexes in (("names", before.names, node_indexes), ("sentences", before.sentences, ("jm_fact_text",))):
            for text in texts:
                query = json.dumps(text[:30], ensure_ascii=False)
                for index in indexes:
                    try:
                        rows = graph.query(f"CALL db.index.fulltext.queryNodes('{index}', $q) YIELD node "
                                           "RETURN coalesce(node.name, node.content) AS text", q=query)
                    except Exception:  # noqa: BLE001 - 这一问查不了（全文的写法认不得）：照实数
                        out["errors"] += 1
                        continue
                    out[kind] += sum(1 for row in rows if row["text"] and text in str(row["text"]))
                try:
                    rows = graph.query("CALL db.index.fulltext.queryRelationships('jm_relation_text', $q) YIELD relationship "
                                       "RETURN relationship.content AS text, relationship.details AS details", q=query)
                except Exception:  # noqa: BLE001
                    out["errors"] += 1
                    continue
                out[kind] += sum(1 for row in rows if text in f"{row['text'] or ''}\n{row['details'] or ''}")
    finally:
        graph.close()
    return out


def _marker(data: Path, library: str) -> str | None:
    """拷贝的账本记着的图的标记（接着哪个图）。"""
    conn = _ro(data / "ledger" / f"{library}.db")
    try:
        row = conn.execute("SELECT marker_id FROM graph_state WHERE id = 1").fetchone()
    finally:
        conn.close()
    return None if row is None or not row[0] else str(row[0])


def _require_empty_graph(library: str, *, fresh: bool) -> None:
    """要接的那个图得是空的：一个标记、一样东西都没有。fresh（managed 刚起的图）是默认密码，先照 graph init 那样改成环境
    变量里的；given 的图不去改（不空、又还是默认密码的图，核出来不演了，密码却被改了：复审第 4 条），要先设好密码。
    连不上、密码不对、不空的，都不演。"""
    from javmem.graph import neo
    from javmem.graph.config import GraphSettings

    target = GraphSettings.from_env(check_sync=False).target(library)
    try:
        if fresh:
            neo.set_initial_password(target, timeout=TIMEOUT)
        graph = neo.NeoGraph(target, timeout=TIMEOUT)
        graph.connect()
    except Exception as error:  # noqa: BLE001 - 连不上、密码不对：照实说种类
        raise CannotRun(f"测试图连不上（{type(error).__name__}）：起好、设好密码再演") from None
    try:
        if graph.markers() or graph.has_other_content():
            raise CannotRun("测试图不是空的（有标记，或者有别的东西）：演练只接空的图，不然撤回、彻底删除会删到那个图里去")
    finally:
        graph.close()


def _forget_graph(data: Path, library: str, settings: Settings) -> None:
    """拷贝的这个库的账本忘了它接着的图（graph init --reset 那一步，javmem.graph.sync.forget）。"""
    from javmem.graph.sync import forget
    from javmem.ledger import open_ledger

    conn = open_ledger(settings.library(library))
    try:
        forget(conn)
    finally:
        conn.close()


def cut_off(data: Path) -> list[str]:
    """演完（过没过、跑没跑完都一样）：拷贝的两个账本忘了图（原来那个、演练接的那个），支线库去掉体检库的标记。
    不然以后谁在拷贝上跑 graph sync、side sweep，就删到原来那一份里去（审阅第 4 条）。一个文件打不开的，别的照样断开
    （复审第 5 条），回断不开的那几个（相对数据目录）。"""
    from javmem.config import Library
    from javmem.graph.sync import forget
    from javmem.ledger import open_ledger

    problems: list[str] = []
    for name in ("prod", "test"):
        path = data / "ledger" / f"{name}.db"
        if not path.is_file():
            continue
        try:
            conn = open_ledger(Library(name, data / "unused-inbox", data / "unused-archive", path))
            try:
                forget(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - 坏了、锁着：照实报，接着断别的
            problems.append(path.relative_to(data).as_posix())
    for side in sorted((data / "side").glob("*/side.db")):
        try:
            conn = sqlite3.connect(side)
            try:
                conn.execute("DELETE FROM meta WHERE key = 'marker_id'")
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            problems.append(side.relative_to(data).as_posix())
    return problems


def _graph_check(plan: Plan, fake: bool) -> dict[str, Any]:
    code, text = run_javmem(plan.log, "graph", "check", "--library", plan.library, *(["--fake-embeddings"] if fake else []))
    counts = {}
    for key, label in (("only_graph", "图里多出来的"), ("only_ledger", "图里少了的"), ("different", "两边不一样的")):
        line = next((line for line in text.splitlines() if line.startswith(label + "：")), "")
        number = line.split("：", 1)[1].split("（", 1)[0] if line else ""
        counts[key] = int(number) if number.isdigit() else None
    return {"exit": code, **counts}


def _fake_vectors(data: Path, library: str) -> bool:
    """账本里的事实向量是假的（试流程、金标准的拷贝）：同步、核对主图都照假的算。"""
    conn = _ro(data / "ledger" / f"{library}.db")
    try:
        row = conn.execute("SELECT model FROM fact_vectors LIMIT 1").fetchone()
    finally:
        conn.close()
    return row is None or str(row[0]).lower().startswith("fake")


# ---------- 一层一层查 ----------


@dataclass
class Layer:
    bad: int = 0  # 没过的一共几处
    counts: dict[str, object] = field(default_factory=dict)
    judged: list[str] = field(default_factory=list)  # counts 里算过没过的那几项（别的只报不判）
    notes: list[str] = field(default_factory=list)  # 只报不判的
    skipped: str | None = None  # 没查的原因

    def as_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value not in (None, [], {})} | {"bad": self.bad}


def _bad(layer: Layer, key: str, value: int) -> None:
    layer.counts[key] = value
    layer.judged.append(key)
    layer.bad += value


# 每一项的叫法（屏幕上那一行）
LABELS = {
    "calls": "问了", "before": "撤回以前问到它", "skipped": "问不了的", "refs": "它的出处", "ids": "只出自它的事实、关系",
    "sentences": "样本句", "names": "名字",
    "errors": "没问成", "packets_not_withdrawn": "包登记没标撤回", "archives_kept": "归档还在（默认只标记）",
    "raw_sentences_kept": "原文句还在（只标记）", "raw_sentences": "原文句", "archives": "归档和跳过的副本",
    "packets_not_purged": "包登记没记彻底删除", "evidence": "证据", "relation_evidence": "关系证据", "fact_claims": "说法",
    "relation_claims": "关系的说法", "quarantine": "隔离", "review_queue": "待确认", "facts_only_it": "只出自它的事实",
    "relations_only_it": "只出自它的关系", "names_as_alias": "名字在别名里", "names_as_standard": "名字当标准名",
    "context_claims": "拿它当上文的窗口里别的包的说法", "requeued": "改回排队的包",
    "review_evidence_withdrawn": "证据都撤回了的待确认（只有包号）", "audit_rows_ids_only": "留痕（只有编号）",
    "windows_before": "撤回以前有它的窗口", "windows_with_it": "有它的窗口", "context_windows": "拿它当上文的窗口",
    "texts": "样本句、名字", "packet_id_outside_calls": "调用记录以外写着它的包号", "packet_id_in_calls": "调用记录里的包号（只有编号）",
    "texts_gone": "不再用的文字", "texts_gone_still_cached": "不再用的文字还在缓存里", "fact_vectors_of_gone": "删了的事实的向量",
    "relation_vectors": "还在的关系的向量", "relation_vectors_lost": "还在的关系的向量少了", "not_synced": "没同步成",
    "check_differs": "两边对不上", "ids": "只出自它的编号", "lines": "原文行", "groups_not_cleared": "组没删、也没记待清",
    "groups_before": "撤回以前的组", "pending_clears": "记了待清的组", "raw_layer_tables": "原文层的表里",
    "tables": "别的表里", "other_files": "别的文件里", "own_archive": "它的归档里", "sqlite_bytes": "SQLite 文件逐字节",
    "inbox": "收件目录", "failed": "failed/", "text_index_out_of_step": "全文索引和原文表对不上",
    "deleted_relations": "删掉的关系", "deleted_relation_vectors_cached": "删掉的关系的向量还在缓存里",
}


def _query_layer(before: Before, after: Mapping[str, Any]) -> Layer:
    layer = Layer()
    found = before.queries
    layer.counts.update(calls=after["calls"], before=sum(found["leaks"].values()), skipped=after["skipped"])
    for key, value in after["leaks"].items():
        _bad(layer, key, int(value))
    if after["errors"]:
        _bad(layer, "errors", int(after["errors"]))
        layer.notes.append(f"有 {after['errors']} 问没问成（{after.get('error_kinds')}）")
    if not sum(found["leaks"].values()):
        layer.notes.append("撤回以前也问不到这个包的东西：这一层查得不准")
    return layer


def _raw_layer(plan: Plan, before: Before, stage: str) -> Layer:
    layer = Layer()
    conn = _ro(plan.data / "ledger" / f"{plan.library}.db")
    try:
        packet = before.packet
        _bad(layer, "packets_not_withdrawn", _one(conn, "SELECT count(*) FROM packets WHERE packet_id = ? AND withdrawn_at IS NULL",
                                                  packet))
        present = sum(1 for one in before.archives if (plan.data / one).exists())
        if stage == "withdraw":
            layer.counts["archives_kept"] = present
            layer.counts["raw_sentences_kept"] = _one(conn, "SELECT count(*) FROM raw_sentences WHERE packet_id = ?", packet)
        else:
            _bad(layer, "raw_sentences", _one(conn, "SELECT count(*) FROM raw_sentences WHERE packet_id = ?", packet))
            _bad(layer, "archives", present + len([one for one in _own_files(plan.data, conn, packet) if one not in before.archives
                                                   and (plan.data / one).exists()]))
            _bad(layer, "packets_not_purged", _one(conn, "SELECT count(*) FROM packets WHERE packet_id = ? AND purged_at IS NULL",
                                                   packet))
    finally:
        conn.close()
    # 全文索引对得上原文表：它是三个字一组拆开存的，逐字节、逐表都搜不出，删句子时没跟着删的只有这样看得出（审阅第 3 条）
    _bad(layer, "text_index_out_of_step", text_index_problems(plan.data))
    return layer


def text_index_problems(data: Path) -> int:
    """拷贝的两个账本里，原文的两个全文索引（原句、统一写法）和原文表对不上的有几个（FTS5 的 integrity-check，带上 rank 1
    连外部的原文表一起核）。只在拷贝上做（要可写的连接，不改东西）。"""
    bad = 0
    for ledger in _ledgers(data):
        conn = sqlite3.connect(ledger, isolation_level=None)
        try:
            for table in ("raw_fts", "raw_norm_fts"):
                try:
                    conn.execute(f"INSERT INTO {table} ({table}, rank) VALUES ('integrity-check', 1)")
                except sqlite3.DatabaseError:
                    bad += 1
        finally:
            conn.close()
    return bad


def _ledger_layer(plan: Plan, before: Before) -> Layer:
    layer = Layer()
    packet = before.packet
    quoted, sentence = f'"{packet}"', f'"{packet}#'
    conn = _ro(plan.data / "ledger" / f"{plan.library}.db")
    try:
        _bad(layer, "evidence", _one(conn, "SELECT count(*) FROM evidence WHERE packet_id = ?", packet))
        _bad(layer, "relation_evidence", _one(conn, "SELECT count(*) FROM relation_evidence WHERE packet_id = ?", packet))
        for table in ("fact_claims", "relation_claims"):
            _bad(layer, table, _one(conn, f"SELECT count(*) FROM {table} WHERE instr(proofs, ?) > 0 OR instr(proofs, ?) > 0",
                                    quoted, sentence))
        _bad(layer, "quarantine", _one(conn, "SELECT count(*) FROM quarantine WHERE packet_id = ? OR instr(payload, ?) > 0"
                                             " OR instr(payload, ?) > 0", packet, quoted, sentence))
        allowed = review = 0
        for row in conn.execute("SELECT kind, claims, quotes, answer FROM review_queue WHERE instr(claims, ?) > 0"
                                " OR instr(coalesce(quotes, ''), ?) > 0 OR instr(coalesce(answer, ''), ?) > 0",
                                (packet, packet, packet)):
            try:
                claims = json.loads(row["claims"] or "{}")
            except ValueError:
                claims = {}
            names_only = sentence not in f"{row['claims']}{row['quotes'] or ''}{row['answer'] or ''}"
            if row["kind"] == "other" and isinstance(claims, dict) and claims.get("reason") == "evidence_withdrawn" and names_only:
                allowed += 1  # 证据都撤回了的冻结事实：只记着包号（做法：只剩包号的算查不到）
            else:
                review += 1
        _bad(layer, "review_queue", review)
        if allowed:
            layer.counts["review_evidence_withdrawn"] = allowed
            layer.notes.append(f"证据都撤回了的冻结事实进了待确认 {allowed} 条（只记着包号）")
        ids = sorted(before.facts_only)
        _bad(layer, "facts_only_it", _one(conn, f"SELECT count(*) FROM facts WHERE fact_id IN ({_marks(ids)})", *ids) if ids else 0)
        ids = sorted(before.relations_only)
        _bad(layer, "relations_only_it", _one(conn, f"SELECT count(*) FROM relations WHERE relation_id IN ({_marks(ids)})", *ids)
             if ids else 0)
        names = before.names
        _bad(layer, "names_as_alias", sum(_one(conn, "SELECT count(*) FROM aliases WHERE alias = ?", name) for name in names))
        _bad(layer, "names_as_standard", sum(_one(conn, "SELECT count(*) FROM entities WHERE canonical_name = ?", name)
                                             for name in names))
        windows = sorted({window for window, _ in before.context_windows})
        left = 0
        if windows:
            for table in ("fact_claims", "relation_claims"):
                left += _one(conn, f"SELECT count(*) FROM {table} WHERE window_id IN ({_marks(windows)})", *windows)
        _bad(layer, "context_claims", left)
        layer.counts["requeued"] = _one(conn, "SELECT count(*) FROM packets WHERE state = 'current' AND withdrawn_at IS NULL"
                                              " AND extraction = 'queued'")
        audit = _one(conn, "SELECT count(*) FROM audit_log WHERE packet_id = ?", packet)
        if audit:
            layer.counts["audit_rows_ids_only"] = audit
    finally:
        conn.close()
    return layer


def _store_layer(plan: Plan, before: Before, tables: Mapping[str, Mapping[str, Counter[Needle]]], refs: Mapping[str, int]) -> Layer:
    layer = Layer()
    store = plan.data / "l3" / "l3.db"
    if not store.is_file():
        layer.skipped = "拷贝里没有结果库"
        return layer
    conn = _ro(store)
    try:
        windows = 0
        context = 0
        for row in conn.execute("SELECT window_id, cache_key, manifest FROM windows"):
            every, _ = _manifest_packets(row["manifest"])
            windows += before.packet in every
            context += (str(row["window_id"]), str(row["cache_key"])) in before.context_windows
    finally:
        conn.close()
    layer.counts["windows_before"] = before.store_windows
    _bad(layer, "windows_with_it", windows)
    _bad(layer, "context_windows", context)
    texts = tally(tables.get("l3/l3.db", {}))
    _bad(layer, "texts", sum(sum(kinds.values()) for kinds in texts.values()))
    if texts:
        layer.counts["texts_by_table"] = texts
    stray = {table: hits for table, hits in refs.items() if table not in STORE_ID_TABLES}
    _bad(layer, "packet_id_outside_calls", sum(stray.values()))
    if stray:
        layer.counts["packet_id_by_table"] = stray
    kept = sum(hits for table, hits in refs.items() if table in STORE_ID_TABLES)
    if kept:
        layer.counts["packet_id_in_calls"] = kept
    return layer


def _vectors_layer(plan: Plan, before: Before, settings: Settings) -> Layer:
    layer = Layer()
    path = plan.data / "vectors" / "vectors.db"
    if not path.is_file():
        layer.skipped = "拷贝里没有向量缓存"
        return layer
    cached = _cached(plan.data)
    now: set[str] = set()
    orphans = 0
    for ledger in _ledgers(plan.data):
        conn = _ro(ledger)
        try:
            now |= {str(row[0]) for row in conn.execute("SELECT content FROM facts UNION SELECT content FROM fact_claims") if row[0]}
            orphans += _one(conn, "SELECT count(*) FROM fact_vectors WHERE fact_id NOT IN (SELECT fact_id FROM facts)")
        finally:
            conn.close()
    current = _relation_vectors(plan.data, settings, cached_only=False)  # 现在每条关系的那一句（在不在缓存里都算）
    kept_relation_texts = set(current.values())
    gone = {text for text in before.texts - now if _sha(text) not in kept_relation_texts}
    layer.counts["texts_gone"] = len(gone)
    _bad(layer, "texts_gone_still_cached", sum(1 for text in gone if _sha(text) in cached))
    _bad(layer, "fact_vectors_of_gone", orphans)
    still_used = kept_relation_texts | {_sha(text) for text in now}
    dead = {sha for sha in before.dead_relations if sha not in still_used}
    layer.counts["deleted_relations"] = len(dead)
    _bad(layer, "deleted_relation_vectors_cached", sum(1 for sha in dead if sha in cached))
    surviving = {key: sha for key, sha in before.relation_vectors.items() if current.get(key) == sha}
    layer.counts["relation_vectors"] = len(surviving)
    _bad(layer, "relation_vectors_lost", sum(1 for sha in surviving.values() if sha not in cached))
    return layer


def _graph_layer(plan: Plan, before: Before, stage: str, fake: bool, status: str | None) -> Layer:
    layer = Layer()
    if plan.graph is None:
        layer.skipped = "没接图（加 --graph 才查）"
        return layer
    from javmem.graph.hook import FINE

    if status not in FINE:
        _bad(layer, "not_synced", 1)
        layer.notes.append(f"{'撤回' if stage == 'withdraw' else '彻底删除'}以后主图没同步成（{status}）")
    check = _graph_check(plan, fake)
    layer.counts["check"] = check
    differs = sum(int(check[key] or 0) for key in ("only_graph", "only_ledger", "different"))
    if check["exit"] != 0 and not differs:
        differs = 1  # 没核成
    _bad(layer, "check_differs", differs)
    with quiet(plan.log):
        found = probe_graph(before, plan.library)
    layer.counts["before"] = before.graph
    for key in ("ids", "names", "sentences"):
        _bad(layer, key, int(found[key]))
    if found["errors"]:
        layer.notes.append(f"全文索引有 {found['errors']} 问没问成")
    if stage == "purge":
        layer.notes.append("Neo4j 社区版删了的东西磁盘上可能还留着（只报不判）：要干净就换一个新图，见开发说明“主图”")
    return layer


def _side_layer(plan: Plan, before: Before) -> Layer:
    layer = Layer()
    path = plan.data / "side" / plan.library / "side.db"
    if not path.is_file():
        layer.skipped = "这个库没跑过支线"
        return layer
    conn = _ro(path)
    try:
        packet = before.packet
        pending = {str(row[0]) for row in conn.execute("SELECT group_id FROM pending_clears")}
        groups = [str(row[0]) for row in conn.execute("SELECT group_id FROM groups WHERE packet_id = ?", (packet,))]
        _bad(layer, "lines", _one(conn, "SELECT count(*) FROM lines WHERE substr(sentence_id, 1, ?) = ?", len(packet) + 1,
                                  packet + "#"))
        _bad(layer, "groups_not_cleared", sum(1 for group in groups if group not in pending))
        layer.counts["groups_before"] = len(before.side_groups)
        layer.counts["pending_clears"] = sum(1 for group in pending if group in set(before.side_groups) | set(groups))
    finally:
        conn.close()
    layer.notes.append("演练不连体检库（拷贝的支线库记着原来那份的标记，连上就删到原来的）：体检库那一头记了待清，只查支线库")
    return layer


def _classify(rel: str, before: Before) -> str:
    if rel in before.archives:
        return "own_archive"
    head = rel.split("/", 1)[0]
    if head in ("inbox", "inbox-test"):
        return "inbox"
    if head == "failed":
        return "failed"
    return "other"


def _bytes_layer(plan: Plan, before: Before, stage: str, files: Mapping[str, Counter[Needle]],
                 tables: Mapping[str, Mapping[str, Counter[Needle]]]) -> Layer:
    layer = Layer()
    ledger_rel = f"ledger/{plan.library}.db"
    sqlite_files = {rel for rel in files if _is_db(plan.data / rel)}
    by_kind: Counter[str] = Counter()
    for rel, counts in files.items():
        kind = "sqlite" if rel in sqlite_files else _classify(rel, before)
        by_kind[kind] += sum(counts.values())
    table_hits: Counter[str] = Counter()
    for db, found in tables.items():
        for table, counts in found.items():
            raw_layer = db == ledger_rel and (table in RAW_TABLES or table.startswith(RAW_PREFIXES))
            if stage == "withdraw" and raw_layer:
                layer.counts.setdefault("raw_layer_tables", 0)
                layer.counts["raw_layer_tables"] = int(layer.counts["raw_layer_tables"]) + sum(counts.values())  # type: ignore[call-overload]
                continue
            table_hits[f"{db}:{table}"] += sum(counts.values())
    _bad(layer, "tables", sum(table_hits.values()))
    if table_hits:
        layer.counts["tables_by_name"] = dict(sorted(table_hits.items()))
    _bad(layer, "other_files", by_kind["other"])
    if by_kind["other"]:
        layer.counts["other_files_by_name"] = {rel: sum(counts.values()) for rel, counts in sorted(files.items())
                                               if rel not in sqlite_files and _classify(rel, before) == "other"}
    if stage == "withdraw":
        layer.counts["own_archive"] = by_kind["own_archive"]
        if by_kind["sqlite"]:
            layer.counts["sqlite_bytes"] = by_kind["sqlite"]
            layer.notes.append(f"SQLite 文件的空页、日志里还有 {by_kind['sqlite']} 处（撤回只删表里的，彻底删除时整理掉，不算没过）")
    else:
        _bad(layer, "own_archive", by_kind["own_archive"])
        _bad(layer, "sqlite_bytes", by_kind["sqlite"])
        if by_kind["sqlite"]:
            layer.counts["sqlite_bytes_by_file"] = {rel: sum(counts.values()) for rel, counts in sorted(files.items())
                                                    if rel in sqlite_files}
    for kind, label in (("inbox", "收件目录"), ("failed", "failed/")):
        if by_kind[kind]:
            layer.counts[kind] = by_kind[kind]
            layer.notes.append(f"{label}里碰上 {by_kind[kind]} 处（单列出来，不算没过）")
    return layer


def _is_db(path: Path) -> bool:
    if path.name.endswith(SIDECARS):
        return True
    return is_sqlite(path)


def check_stage(plan: Plan, before: Before, settings: Settings, stage: str, *, today: date, fake: bool,
                graph_status: str | None) -> dict[str, Layer]:
    needles = needles_of("sentence", before.sentences) + needles_of("name", _byte_names(before.names))
    files = scan_tree(plan.data, needles)
    tables: dict[str, dict[str, Counter[Needle]]] = {}
    refs: dict[str, int] = {}
    ref_needle = Needle("ref", 0, "id", before.packet)
    for path in sorted(plan.data.rglob("*")):
        if path.is_file() and not path.name.endswith(SIDECARS) and is_sqlite(path):
            rel = path.relative_to(plan.data).as_posix()
            found = scan_tables(path, needles)
            if found:
                tables[rel] = found
            if rel == "l3/l3.db":
                refs = {table: counts[ref_needle] for table, counts in scan_tables(path, [ref_needle]).items()}
    with quiet(plan.log):
        asked = probe_queries(before, settings, today=today)
    layers = {
        "query": _query_layer(before, asked),
        "raw": _raw_layer(plan, before, stage),
        "ledger": _ledger_layer(plan, before),
        "store": _store_layer(plan, before, tables, refs),
        "vectors": _vectors_layer(plan, before, settings),
        "graph": _graph_layer(plan, before, stage, fake, graph_status),
        "side": _side_layer(plan, before),
        "bytes": _bytes_layer(plan, before, stage, files, tables),
    }
    return layers


def _byte_names(names: Sequence[str]) -> list[str]:
    """拿去逐字节搜的名字：全是英文字母、数字又不到 4 个字的不搜（二进制、JSON 的键里碰上的太多），查询接口那一层照样问。"""
    return [name for name in names if not (name.isascii() and len(name) < 4)]


# ---------- 说一遍 ----------


def describe(name: str, layer: Layer) -> str:
    label = LAYER_NAMES[name]
    if layer.skipped:
        return f"  {label}：不查（{layer.skipped}）"
    judged = "、".join(f"{LABELS.get(key, key)} {layer.counts[key]}" for key in layer.judged)
    other = "、".join(f"{LABELS.get(key, key)} {value}" for key, value in layer.counts.items()
                      if key not in layer.judged and isinstance(value, int))
    mark = "✓" if layer.bad == 0 else f"✗ {layer.bad} 处"
    lines = [f"  {label}：{judged}" + (f"；只报不判：{other}" if other else "") + f" {mark}"]
    lines += [f"    （{note}）" for note in layer.notes]
    return "\n".join(lines)


def verdict(stages: Mapping[str, Mapping[str, Layer]], paid: int) -> tuple[bool, str]:
    bad: Counter[str] = Counter()
    for layers in stages.values():
        for name, layer in layers.items():
            bad[name] += layer.bad
    parts = [f"{LAYER_NAMES[name]} {count} 处" for name, count in bad.items() if count]
    if paid:
        parts.append(f"记账有 {paid} 行付钱的")
    return (not parts), ("过" if not parts else "没过：" + "、".join(parts))


# ---------- 一遍 ----------


class DrillGraph:
    """managed：照 compose 起一个演练自己的测试图（javmem.regress.graph.ManagedGraph 那一套：拿锁、只清自己的目录、
    别人开着不停、用完挂回原来的目录），锁里写的是演练。"""

    def __init__(self, plan: Plan, out: Out) -> None:
        from javmem.regress.graph import ManagedGraph

        class _Drill(ManagedGraph):
            def take_lock(self, minutes: int) -> None:  # noqa: D102 - 锁里写清楚是删除演练
                import secrets

                if self.lock is None:
                    return
                self.lock.parent.mkdir(parents=True, exist_ok=True)
                token = secrets.token_hex(8)
                try:
                    with self.lock.open("x", encoding="utf-8") as handle:
                        handle.write(f"会话：{self.who}\n开始：{datetime.now():%Y-%m-%d %H:%M}\n"
                                     f"做什么：javmem drill delete，测试图挂演练自己的目录（{self.data}），用完停掉、挂回原来的目录\n"
                                     f"估多久：{minutes} 分钟\n锁的编号：{token}\n")
                except FileExistsError:
                    from javmem.regress.graph import GraphBusy

                    raise GraphBusy(f"测试图被占着（{self.lock} 在）：先做别的，过一会儿再演；或者不加 --graph") from None
                self._locked, self._token = True, token

        assert plan.graph_compose is not None and plan.graph_data is not None
        self.graph = _Drill(plan.graph_compose, plan.graph_data, plan.graph_container, lock=plan.graph_lock,
                            who="javmem drill delete", log=out)

    def __enter__(self) -> DrillGraph:
        from javmem.regress.graph import GraphBusy, GraphRefused

        try:
            self.graph.take_lock(30)
            self.graph.up()
        except (GraphBusy, GraphRefused) as error:
            self.__exit__()
            raise CannotRun(str(error)) from None
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.graph.down()
        finally:
            self.graph.drop_lock()


def _code() -> str:
    from javmem.versions import code_info

    info = code_info()
    commit = info.get("commit")
    return (str(commit)[:12] if commit else "?") + ("+改动" if info.get("dirty") else "")


def _access_file(root: Path, library: str) -> Path:
    path = root / "access.yaml"
    path.write_text("# javmem drill delete 写的：只给这一遍演练用，看得到这个库的全部（做法第 2 节：用看得到全部的身份问）\n"
                    f"identities:\n  {CALLER}:\n    env: {library}\n    partitions: \"*\"\n    sensitive: \"*\"\n",
                    encoding="utf-8")
    return path


def _graph_env(plan: Plan, base: Mapping[str, str]) -> dict[str, str]:
    if plan.graph is None:
        return {"JAVMEM_GRAPH_SYNC": "off", "JAVMEM_GRAPH_SEARCH": "off"}
    keys = ["JAVMEM_GRAPH_TEST_NEO4J_USER", "JAVMEM_GRAPH_TEST_NEO4J_PASSWORD"]
    if plan.graph == "given":
        keys.append("JAVMEM_GRAPH_TEST_NEO4J_URI")
    return {key: base[key] for key in keys if base.get(key, "").strip()}


def drill(plan: Plan, base: Mapping[str, str], out: Out, *, today: date | None = None) -> int:
    today = today or date.today()
    started = time.monotonic()
    plan.root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"format": FORMAT, "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                              "commit": _code(), "library": plan.library, "purge": plan.purge, "graph": plan.graph}
    try:
        code = _drill(plan, base, out, today, result)
    except CannotRun as error:
        result["cannot"] = str(error)
        out(f"删除演练：跑不了：{error}")
        code = EXIT_CANNOT
    except Exception as error:  # noqa: BLE001 - 想不到的错（docker、库坏了）：算跑不了，不和“没过”混在一起（审阅第 12 条）
        result["cannot"] = f"出错了：{type(error).__name__}（看 {LOG_FILE}）"
        with plan.log.open("a", encoding="utf-8") as handle:
            import traceback

            handle.write("\n" + traceback.format_exc())
        out(f"删除演练：跑不了：{result['cannot']}")
        code = EXIT_CANNOT
    finally:
        if plan.data.is_dir():
            problems = cut_off(plan.data)  # 演完的拷贝和原来那个图、体检库断开（审阅第 4 条）
            if problems:
                result["cut_off_failed"] = problems
                out(f"注意：拷贝里有 {len(problems)} 个库没能和图、体检库断开（{'、'.join(problems)}）："
                    f"别在 {plan.data} 上跑 graph sync、side sweep")
    result["seconds"] = round(time.monotonic() - started, 1)
    (plan.root / RESULT_FILE).write_text(json.dumps(result, ensure_ascii=False, indent=1, default=_plain) + "\n", encoding="utf-8")
    return code


def _plain(value: object) -> object:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Counter):
        return dict(value)
    raise TypeError(type(value).__name__)


def _drill(plan: Plan, base: Mapping[str, str], out: Out, today: date, result: dict[str, Any]) -> int:
    out(f"拷数据目录：{plan.source} → {plan.data}")
    stats = copy_data_dir(plan.source, plan.data, wait=plan.wait)
    result["copy"] = stats.as_dict()
    out(f"  拷好了：SQLite {stats.sqlite} 个（在线备份）、别的文件 {stats.files} 个，{stats.bytes / 1e6:.1f} MB，{stats.seconds:.0f} 秒")
    token_ledger = plan.root / "token-ledger.csv"
    extra = {"JAVMEM_ACCESS_FILE": str(_access_file(plan.root, plan.library)), "JAVMEM_CALLER": CALLER, **_graph_env(plan, base)}
    env = drill_env(base, data=plan.data, token_ledger=token_ledger, extra=extra)
    if _fake_vectors(plan.data, plan.library):  # 假向量的拷贝（金标准那种）：用不着向量服务，也不去问它
        for key in EMBEDDING_PASS:
            env.pop(key, None)
    managed = DrillGraph(plan, out) if plan.graph == "managed" else None
    with scoped_env(env):
        try:
            settings = Settings.from_env()
        except ConfigError as error:
            raise CannotRun(f"配置不对：{error}") from None
        ledger = plan.data / "ledger" / f"{plan.library}.db"
        conn = _ro(ledger)
        try:
            packet, picked = pick_packet(plan, conn, plan.data / "l3" / "l3.db", today)
        finally:
            conn.close()
        result["picked"] = picked
        out(f"撤哪个包：{packet}（{_picked_text(picked)}）")
        before = record_before(plan.data, plan.library, packet, settings)
        fake = _fake_vectors(plan.data, plan.library)
        if managed is None:
            return _run_stages(plan, before, settings, out, today, result, fake, token_ledger)
        with managed:  # 起不来、拿不到锁：放锁、停图（只停自己起的）以后才出来
            return _run_stages(plan, before, settings, out, today, result, fake, token_ledger)


def _picked_text(picked: Mapping[str, object]) -> str:
    by = picked.get("by")
    if by == "packet":
        return "点名的"
    if by == "random":
        return f"随机挑的，种子 {picked['seed']}，{picked['candidates']} 个里挑一个"
    if by == "month":
        return f"按月轮着撤，{picked['month']} 撤排第 {int(picked['index']) + 1} 的（{picked['candidates']} 个）"  # type: ignore[call-overload]
    return (f"牵连最多：别的包的窗口拿它当上文 {picked['context_windows']} 个、接着它的包 {picked['continued_by']} 个、"
            f"有它的证据的事实 {picked['facts']} 条")


def _run_stages(plan: Plan, before: Before, settings: Settings, out: Out, today: date, result: dict[str, Any], fake: bool,
                token_ledger: Path) -> int:
    if plan.graph is not None:
        # 拷贝的账本记着原来那份接着的图的标记：要接的图不是空的（比如就是原来那个图），graph init 认出“同一本账”只核一遍，
        # 撤回、彻底删除就删到原来那一份里去（审阅第 1 条）。所以先核那个图是空的，拷贝的账本先忘了原来那个图，接上以后核
        # 标记换了新的
        old = _marker(plan.data, plan.library)
        with quiet(plan.log):
            _require_empty_graph(plan.library, fresh=plan.graph == "managed")
        _forget_graph(plan.data, plan.library, settings)
        for args in (("graph", "init", "--reset", "--library", plan.library),
                     ("graph", "sync", "--library", plan.library, *(["--fake-embeddings"] if fake else []))):
            code, _ = run_javmem(plan.log, *args)
            if code != 0:
                raise CannotRun(f"javmem {' '.join(args[:2])} 退出码 {code}（看 {plan.log}）：测试图要是空的、开着")
        new = _marker(plan.data, plan.library)
        if not new or new == old:
            raise CannotRun("接图以后拷贝的账本没换上新的标记：不演（怕连的还是原来那个图）")
        with quiet(plan.log):
            before.graph = probe_graph(before, plan.library)
        before.relation_vectors = _relation_vectors(plan.data, settings)  # 关系的向量整张同步时才算、进缓存
        before.dead_relations = _dead_relation_vectors(plan.data, plan.library, settings, before.relations_only)
    with quiet(plan.log):
        before.queries = probe_queries(before, settings, today=today)
    if not sum(before.queries["leaks"].values()):
        raise CannotRun("撤回以前问查询接口就问不到这个包的东西：撤回以后问不到证明不了什么，换一个包")
    result["before"] = before.counts()
    out(f"撤回以前记下：原文句 {before.raw_sentences} 句；样本句 {len(before.sentences)} 句；只出自它的事实 {len(before.facts_only)} 条、"
        f"关系 {len(before.relations_only)} 条；只有它说过的名字 {len(before.names)} 个；别的包拿它当上文的窗口 "
        f"{len(before.context_windows)} 个、里面别的包的说法 {len(before.context_claims)} 处；"
        f"问查询接口 {before.queries['calls']} 问、问到它 {sum(before.queries['leaks'].values())} 处")
    if not before.sentences and not before.names:
        raise CannotRun("这个包挑不出样本句、名字（12 个字以上、别处没有的话一句都没有）：换一个包")
    stages: dict[str, dict[str, Layer]] = {}
    report = plan.root / "withdraw-report.json"
    code, _ = run_javmem(plan.log, "withdraw", "--library", plan.library, "--packet", before.packet, "--report", str(report))
    withdrawn = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
    if withdrawn is None or before.packet not in withdrawn.get("packets", []):
        raise CannotRun(f"撤回没做成（javmem withdraw 退出码 {code}，看 {plan.log}）")
    result["withdraw"] = {"exit": code, **{key: withdrawn.get(key) for key in (
        "requeued", "store_windows", "store_results", "cleared_claims", "cleared_quarantine", "vectors_deleted", "hidden")},
        "graph": (withdrawn.get("graph") or {}).get("status")}
    stages["withdraw"] = check_stage(plan, before, settings, "withdraw", today=today, fake=fake,
                                     graph_status=(withdrawn.get("graph") or {}).get("status"))
    _say_stage(out, "withdraw", stages["withdraw"])
    if plan.purge:
        report = plan.root / "purge-report.json"
        code, _ = run_javmem(plan.log, "purge", "--library", plan.library, "--packet", before.packet, "--yes", "--report",
                             str(report))
        purged = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
        if purged is None:
            raise CannotRun(f"彻底删除没做成（javmem purge 退出码 {code}，看 {plan.log}）")
        result["purge_report"] = {"exit": code, **{key: purged.get(key) for key in (
            "revisions", "sentences", "store_windows", "store_results", "vectors_deleted", "incomplete")},
            "files": len(purged.get("files") or []), "graph": (purged.get("graph") or {}).get("status")}
        stages["purge"] = check_stage(plan, before, settings, "purge", today=today, fake=fake,
                                      graph_status=(purged.get("graph") or {}).get("status"))
        if purged.get("incomplete"):
            stages["purge"]["bytes"].notes.append(f"彻底删除没整理完：{'、'.join(purged['incomplete'])}")
        _say_stage(out, "purge", stages["purge"])
    paid, local = token_rows(token_ledger)
    result["token_rows"] = {"paid": paid, "local": local}
    result["stages"] = {stage: {name: layer.as_dict() for name, layer in layers.items()} for stage, layers in stages.items()}
    ok, text = verdict(stages, paid)
    result["verdict"] = text
    out(f"记账：付钱的 {paid} 行、本机的和假的 {local} 行")
    out(f"删除演练：{text}（{before.packet}，{'撤回、彻底删除两遍都查了' if plan.purge else '只撤回、没彻底删除'}）")
    return EXIT_PASS if ok else EXIT_FAIL


def _say_stage(out: Out, stage: str, layers: Mapping[str, Layer]) -> None:
    out(f"{STAGE_NAMES[stage]}：")
    for name in LAYERS:
        out(describe(name, layers[name]))


def run(args: argparse.Namespace, out: Out = print) -> int:
    import os

    try:
        plan = plan_from(args, os.environ)
    except CannotRun as error:
        out(f"删除演练：跑不了：{error}")
        return EXIT_CANNOT
    return drill(plan, dict(os.environ), out)
