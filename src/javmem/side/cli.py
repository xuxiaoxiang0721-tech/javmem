"""`javmem side …`：Graphiti 支线的命令（第 6 步）。

- `init`：体检库第一次用。默认密码还在的先改成 JAVMEM_SIDE_NEO4J_PASSWORD 那个（bolt 上改，密码当参数传。默认密码连得上、
  又要先改密码才能用的库一定是没人用过的新库）；核往外连的几项设置关没关；核过了才放标记（防串库）；建 Graphiti 的
  索引，核全文索引的分析器是不是 cjk。
- `run`：跑点名的包（`--packet` 写几次，重了的只跑一遍），每个包一组。开头、结尾各“扫一遍”。只用本机 Qwen 和本机的向量服务。
- `sweep`：只扫一遍（撤回了的、换了版本的整组删）。体检库连不上的（这几个命令都一样）：要删的组先把支线库里的原文删了、
  记成待清，退出码 4，体检库起来以后再扫。
- `status`：支线的库里每一组跑到哪了（不连体检库）。
- `export`：先扫一遍，再导出成对齐格式（javmem-score/1），给 j39_score.py 打分；`--out` 要放仓库外（里面有原话）。
- `reset`：体检库整个重建以后（停了容器、删了它的数据目录重起，比如要换密码），体检库是空的、没有标记的，清支线的库
  （`--yes`），接着 init、run。
- `pick`：每周体检抽包（J6.3）：一段时间里收进来、主线写完了的包随机抽几个，种子能重来。只读账本。
- `compare`：第二证人（J6.2）：两份对齐格式（主线、支线）一包一包比，分四种对不上；`checkup` 另出体检报告
  （主线漏了什么）。都只读文件，不连体检库。

每个命令一开始就装上“只许连本机那几个端口”的闸（javmem.side.netguard），结束时把拦下的记进支线的库、打印出来。
同一个库的支线同时只有一个命令在跑（锁）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from javmem.config import ConfigError, Settings
from javmem.embedding import EmbeddingSettings, OpenAIEmbeddings
from javmem.ledger import LedgerError
from javmem.scoring.run import inside_repo
from javmem.side.config import SideSettings, embedding_endpoint
from javmem.side.netguard import NetGuard
from javmem.side.store import SideBusy, SideStore, SideStoreError, side_dir, side_lock
from javmem.usage import TokenLedger

EXIT_OK = 0
EXIT_ATTENTION = 1  # 有抽不成的窗口、停下了、拦下了往外的连接
EXIT_INPUT = 2  # 设置、参数不对，什么都没动
EXIT_BUSY = 3  # 这个库的支线已经有一个命令在跑
EXIT_STOPPED = 4  # 体检库、Qwen、向量服务连不上
ENV_TOKEN_LEDGER = "JAVMEM_TOKEN_LEDGER"
DEFAULT_TAG = "j6-side"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="side_command", required=True)
    for name, text in (("init", "体检库第一次用：改默认密码、核设置、放标记、建索引"),
                       ("sweep", "扫一遍：撤回了的、换了版本的包整组删"),
                       ("status", "支线的库里每一组跑到哪了（不连体检库）"),
                       ("reset", "体检库整个重建以后：清支线的库（体检库要是空的、没有标记）")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--library", required=True, help="数据目录里的库：prod 或 test")
        if name == "reset":
            sub.add_argument("--yes", action="store_true", help="确定要清")
    run = commands.add_parser("run", help="跑点名的包（本机 Qwen 抽，结果只进体检库）")
    run.add_argument("--library", required=True, help="数据目录里的库：prod 或 test")
    run.add_argument("--packet", action="append", required=True, help="包号，可以写几次")
    run.add_argument("--parallel", type=int, default=1, help="同时跑几个包（Qwen 的并发另由 JAVMEM_SIDE_LANES 卡着）")
    run.add_argument("--retry-failed", action="store_true", help="抽不成的窗口重来")
    run.add_argument("--max-minutes", type=float, help="到了就不再开始新的窗口，重跑接着来")
    run.add_argument("--tag", default=DEFAULT_TAG, help="记账的标签")
    export = commands.add_parser("export", help="导出成对齐格式（javmem-score/1）")
    export.add_argument("--library", required=True, help="数据目录里的库：prod 或 test")
    export.add_argument("--packet", action="append", help="只导这几个包（默认支线的库里都导）")
    export.add_argument("--out", required=True, help="对齐格式的文件（放仓库外：里面有原话）")
    export.add_argument("--edges-out", help="另写一份每条边怎么换的（JSONL，放仓库外）")
    pick_cmd = commands.add_parser("pick", help="每周体检抽包：一段时间里主线写完了的包随机抽几个（只读账本）")
    pick_cmd.add_argument("--library", required=True, help="数据目录里的库：prod 或 test")
    pick_cmd.add_argument("--since", required=True, help="从这个时候起收进来的（ISO 8601，带时区）")
    pick_cmd.add_argument("--until", required=True, help="到这个时候以前（不含）")
    pick_cmd.add_argument("--count", type=int, help="抽几个，默认照 JAVMEM_SIDE_FILE（没配是 10）")
    pick_cmd.add_argument("--seed", type=int, required=True, help="随机种子（写进报告，能重来）")
    for name, text in (("compare", "第二证人：主线、支线两份对齐格式一包一包比"),
                       ("checkup", "体检报告：主线漏了什么（按关系种类、两头的类型数）")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--library", required=True, help="数据目录里的库：prod 或 test（别名表、主人的名字照设置）")
        sub.add_argument("--main", help="主线的对齐格式（JSON）；不给就从这个库的账本读（J4.4 起从关系表）")
        if name == "compare":
            sub.add_argument("--queue", action="store_true", help="两种真矛盾写进待确认（witness_mismatch），只和账本里的主线比")
        sub.add_argument("--side", required=True, help="支线的对齐格式（JSON，javmem side export 导的）")
        sub.add_argument("--packet", action="append", help="只比这几个包（默认两边都抽过的）")
        sub.add_argument("--out", required=True, help="结果（JSON，放仓库外：里面有名字和句子编号）")


def _ledger_path(settings: Settings) -> Path:
    value = os.environ.get(ENV_TOKEN_LEDGER, "").strip()
    return Path(value) if value else settings.require_data_dir() / "logs" / "token-ledger.csv"


def run(args: argparse.Namespace, settings: Settings, out: Callable[[str], None] = print) -> int:
    logging.getLogger("neo4j").setLevel(logging.ERROR)  # 驱动的提示（“这个标签还不存在”这类）不打出来
    try:
        side = SideSettings.from_env()
        data_dir = settings.require_data_dir()
        settings.library(args.library)
        if args.side_command == "export" and inside_repo(Path(args.out)):
            out("导不了：--out 在仓库里。导出里有原话，要放仓库外")
            return EXIT_INPUT
        if args.side_command == "export" and args.edges_out and inside_repo(Path(args.edges_out)):
            out("导不了：--edges-out 在仓库里")
            return EXIT_INPUT
        if args.side_command in ("compare", "checkup") and inside_repo(Path(args.out)):
            out("比不了：--out 在仓库里。结果里有名字和句子编号，要放仓库外")
            return EXIT_INPUT
        if args.side_command == "run" and not 1 <= args.parallel <= 4:
            out("--parallel 要在 1 到 4 之间")
            return EXIT_INPUT
    except ConfigError as error:
        out(f"配置不对：{error}")
        return EXIT_INPUT
    if args.side_command in ("compare", "checkup"):
        return _compare(args, settings, out)
    try:
        with side_lock(side_dir(data_dir, args.library)), SideStore.open(data_dir, args.library) as store:
            if args.side_command == "pick":
                return _pick(args, settings, store, out)
            if args.side_command == "status":
                return _status(store, out)
            ports = [side.neo4j_endpoint()[1]]
            embeddings = None
            if args.side_command == "run":
                ports.append(side.qwen_endpoint()[1])
                embedding = EmbeddingSettings.from_env()
                ports.append(embedding_endpoint(embedding.base_url)[1])
                embeddings = OpenAIEmbeddings(embedding)
            guard = NetGuard(ports)
            with guard:
                try:
                    code = asyncio.run(_async_main(args, settings, side, store, embeddings, out))
                finally:
                    for item in guard.blocked:
                        store.record_blocked(item.at, item.host, item.port, item.where)
            if guard.blocked:
                out(f"闸拦下了 {len(guard.blocked)} 次往外的连接：" +
                    "，".join(sorted({f'{item.host}:{item.port}（{item.where}）' for item in guard.blocked})))
                return max(code, EXIT_ATTENTION)
            return code
    except SideBusy as busy:
        out(str(busy))
        return EXIT_BUSY
    except (ConfigError, SideStoreError, LedgerError) as error:
        out(f"配置不对：{error}")
        return EXIT_INPUT


async def _async_main(args: argparse.Namespace, settings: Settings, side: SideSettings, store: SideStore,
                      embeddings: OpenAIEmbeddings | None, out: Callable[[str], None]) -> int:
    from neo4j.exceptions import AuthError, ServiceUnavailable

    from javmem.side.graph import Recorder, SideGraph, SideGraphError, set_initial_password

    try:
        if args.side_command == "init":
            changed = await set_initial_password(side)
            out("体检库的默认密码改好了" if changed else "体检库的默认密码早就改过了")
        recorder = None
        if args.side_command == "run":
            recorder = Recorder(store, TokenLedger(_ledger_path(settings)), args.tag, side.qwen_model)
        graph = SideGraph(side, store, recorder=recorder, embeddings=embeddings)
        try:
            try:
                await graph.connect()
            except (ServiceUnavailable, OSError) as error:
                out(f"体检库连不上（{type(error).__name__}）：容器起了没有？")
                if args.side_command not in ("init", "reset"):  # 连不上也先把撤回了的原文从支线的库里删掉
                    await _sweep_offline(settings, args.library, store, out)
                return EXIT_STOPPED
            if args.side_command == "init":
                return await _init(graph, out)
            if args.side_command == "reset":
                return await _reset(graph, args, store, out)
            try:
                await graph.check_marker()
            except SideGraphError:  # 标记对不上（重建了还没 reset、配到了别的库）：体检库不碰，撤回了的原文照样先删
                await _sweep_offline(settings, args.library, store, out)
                raise
            if args.side_command == "sweep":
                return await _sweep(graph, settings, args.library, store, out)
            if args.side_command == "run":
                return await _run(graph, settings, args, store, out)
            if args.side_command == "export":
                from javmem.side.export import export_side

                return await export_side(graph, settings, args, store, out)
        finally:
            await graph.close()
    except (ServiceUnavailable, OSError) as error:
        out(f"体检库连不上（{type(error).__name__}）：容器起了没有？")
        return EXIT_STOPPED
    except AuthError:
        out("体检库的账号、密码不对（JAVMEM_SIDE_NEO4J_PASSWORD）")
        return EXIT_INPUT
    except SideGraphError as error:
        out(f"体检库用不了：{error}")
        return EXIT_INPUT
    return EXIT_INPUT


async def _init(graph: object, out: Callable[[str], None]) -> int:
    from javmem.side.graph import EXPECTED_CONFIG, SideGraph, kit

    assert isinstance(graph, SideGraph)
    found = await graph.config()  # 先核往外连的设置，核过了才放标记、建索引
    wrong = {name: found.get(name) for name, want in EXPECTED_CONFIG.items() if str(found.get(name)).lower() != want}
    for name, want in EXPECTED_CONFIG.items():
        out(f"  {name} = {found.get(name)}（要 {want}）")
    for name in ("server.memory.heap.max_size", "server.memory.pagecache.size", "dbms.memory.transaction.total.max"):
        out(f"  {name} = {found.get(name)}")
    if wrong:
        out("体检库的设置不对：" + "、".join(sorted(wrong)))
        return EXIT_INPUT
    await graph.check_marker(create=True)
    k = kit(graph.settings.lanes)
    driver = k.Neo4jDriver(graph.settings.neo4j_uri, graph.settings.neo4j_user, graph.settings.require_password())
    try:
        task = getattr(driver, "_init_task", None)
        if task is not None:
            await task
        else:
            await driver.build_indices_and_constraints()
    finally:
        await driver.close()
    analyzers = await graph.fulltext_analyzers()
    out(f"  全文索引 {len(analyzers)} 个，分析器：" + "、".join(sorted(set(analyzers.values()))))
    if any(value != "cjk" for value in analyzers.values()):
        out("有全文索引不是 cjk 分析器")
        return EXIT_ATTENTION
    out("体检库好了：标记放好了，设置核过了，索引建好了")
    return EXIT_OK


async def _sweep_offline(settings: Settings, library: str, store: SideStore, out: Callable[[str], None]) -> None:
    """体检库连不上：要删的组先把原文（支线库里的行）删了、记成待清，体检库起来以后再整组删。"""
    from javmem.side.sweep import sweep

    from javmem.side.witness import clean_and_say

    result = await sweep(settings.library(library).db_path, store, None)
    if result.pending:
        out(f"记了待清 {len(result.pending)} 组（支线的库里这几组的原文已经删了），体检库起来以后再扫一遍：" +
            "、".join(f"{group_id}（{reason}）" for group_id, reason in result.pending))
    clean_and_say(settings.library(library).db_path, settings.require_data_dir(), result.packets, out)
    _scrub_if_purged(store, result)


def _scrub_if_purged(store: SideStore, result: object) -> None:
    """扫到彻底删除了的包：支线的库整个重写一遍，删了的字不留在文件里（撤回时支线正在跑、彻底删除时没扫成的，这里补上）。"""
    from javmem.side.sweep import PURGED

    reasons = [reason for _, reason in (*result.cleared, *result.pending)]  # type: ignore[attr-defined]
    if PURGED in reasons:
        store.scrub()


async def _reset(graph: object, args: argparse.Namespace, store: SideStore, out: Callable[[str], None]) -> int:
    """体检库整个重建以后（停了容器、删了它的数据目录重起）：清支线的库，接着 init、run。"""
    from javmem.side.graph import SideGraph

    assert isinstance(graph, SideGraph)
    if not args.yes:
        out("reset 会清掉支线的库里所有的组（体检库重建以后才用）：确定的话加 --yes")
        return EXIT_INPUT
    if not await graph.is_empty_without_marker():
        out("体检库里还有东西或者标记：只有整个重建过（空的、没有标记）的体检库才 reset；要删撤回了的用 sweep")
        return EXIT_INPUT
    count = store.reset()
    out(f"支线的库清好了（清掉 {count} 组）：接着 javmem side init，再 run")
    return EXIT_OK


async def _sweep(graph: object, settings: Settings, library: str, store: SideStore, out: Callable[[str], None]) -> int:
    from javmem.side.sweep import sweep
    from javmem.side.witness import clean_and_say

    result = await sweep(settings.library(library).db_path, store, graph)  # type: ignore[arg-type]
    for group_id, reason in result.cleared:
        out(f"删了 {group_id}：{reason}")
    if not result.cleared:
        out("扫了一遍：没有要删的组")
    if result.rerun:
        out("这几个包有句子单条撤回了，要重跑：" + "、".join(sorted(set(result.rerun))))
    # 第二证人的待确认按账本的现状清（撤回时支线正在跑、账本锁着没清成的，这里补上）
    clean_and_say(settings.library(library).db_path, settings.require_data_dir(), result.packets, out)
    _scrub_if_purged(store, result)
    return EXIT_OK


async def _run(graph: object, settings: Settings, args: argparse.Namespace, store: SideStore,
               out: Callable[[str], None]) -> int:
    from javmem.side.episodes import load_episodes
    from javmem.side.run import run_packets

    await _sweep(graph, settings, args.library, store, out)
    args.packet = list(dict.fromkeys(args.packet))  # 同一个包写了两遍的只跑一遍（同一组不能同时跑两份）
    episodes, problems = load_episodes(settings, args.library, args.packet)
    for packet_id, reason in problems:
        out(f"没用上 {packet_id}：{reason}")
    skipped = [one for one in episodes if one.skip]
    out(f"{len({one.packet_id for one in episodes})} 个包，{len(episodes)} 个窗口，不发的 {len(skipped)} 个"
        + (f"（{'、'.join(sorted({one.skip or '' for one in skipped}))}）" if skipped else ""))
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    started = time.monotonic()
    result = await run_packets(episodes, args.packet, store, graph, parallel=args.parallel,  # type: ignore[arg-type]
                               retry_failed=args.retry_failed, deadline=deadline, out=out)
    await _sweep(graph, settings, args.library, store, out)
    for packet in result.packets:
        out(f"{packet.packet_id}：{packet.windows} 个窗口，好了 {packet.done}（认回来的 {packet.adopted}），抽不成 {packet.failed}，"
            f"不发 {packet.skipped}，{packet.seconds:.0f} 秒，{packet.state}")
    out(f"一共 {time.monotonic() - started:.0f} 秒")
    if result.stopped:
        out(f"停下了：{result.stopped}")
        return EXIT_STOPPED
    if problems or any(packet.failed or packet.state != "done" for packet in result.packets):
        return EXIT_ATTENTION
    return EXIT_OK


def _pick(args: argparse.Namespace, settings: Settings, store: SideStore, out: Callable[[str], None]) -> int:
    from datetime import datetime

    from javmem.side.checkup import SidePlan, candidates, pick

    try:
        since, until = datetime.fromisoformat(args.since), datetime.fromisoformat(args.until)
    except ValueError:
        out("--since、--until 要写成 ISO 8601 的时间")
        return EXIT_INPUT
    if since.tzinfo is None or until.tzinfo is None or since >= until:
        out("--since、--until 要带时区，--since 要早于 --until")
        return EXIT_INPUT
    count = args.count if args.count is not None else SidePlan.from_env().weekly_count
    if not 1 <= count <= 200:
        out("--count 要在 1 到 200 之间")
        return EXIT_INPUT
    pool = candidates(settings.library(args.library).db_path, store, since, until)
    chosen = pick(pool, count, args.seed)
    out(f"能抽的 {len(pool)} 个，抽了 {len(chosen)} 个（种子 {args.seed}）：")
    for packet_id in chosen:
        out(f"  {packet_id}")
    return EXIT_OK


def _compare(args: argparse.Namespace, settings: Settings, out: Callable[[str], None]) -> int:
    import json

    from javmem.pipeline.merge import load_owner_names
    from javmem.scoring.common import ScoreInputError, load_side
    from javmem.side.compare import as_dict, checkup_report, compare, totals
    from javmem.zh.lexicon import load_lexicon

    from javmem.side.mainline import read_main_line
    from javmem.side.witness import LedgerBusy, ledger_locks, queue_contradictions

    queue = bool(getattr(args, "queue", False))
    if queue and args.main:
        out("--queue 只和账本里的主线比（不给 --main）：待确认里记的是账本里的关系编号")
        return EXIT_INPUT
    try:
        side = load_side(Path(args.side))
        given = load_side(Path(args.main)) if args.main else None
    except ScoreInputError as error:
        out(f"对齐格式读不了：{error}")
        return EXIT_INPUT
    lexicon = load_lexicon(settings.lexicon_file) if settings.lexicon_file else None
    owners = load_owner_names(settings.owner_file)

    def finish(main: object, relation_of: dict[str, str] | None) -> int:
        result = compare(main, side, lexicon, packets=args.packet, owner_keys=owners)  # type: ignore[arg-type]
        data: dict[str, object] = as_dict(result)
        if args.side_command == "checkup":
            data = {"checkup": checkup_report(result, side), "comparison": data}
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        out(f"比了 {len(result.packets)} 个包：" + "，".join(f"{kind} {count}" for kind, count in result.by_kind().items()))
        for relation, numbers in sorted(totals(result).items()):
            out(f"  {relation}：主线 {numbers['main']}，支线 {numbers['side']}，两边都有 {numbers['both']}")
        if not queue:
            return EXIT_OK
        queued = queue_contradictions(settings.library(args.library).db_path, result, main, side,  # type: ignore[arg-type]
                                      relation_of=relation_of)
        out(f"真矛盾 {len(result.queue())} 条，进了待确认 {queued.added} 条（以前进过的不重复进）")
        if queued.stale:
            out(f"没进的 {queued.stale} 条：包不是现行的了、支线导出的不是现行的那一版（或者没写是哪一版，要重新"
                " javmem side export），或者证据里有撤回了、找不到的句子")
            return EXIT_ATTENTION
        return EXIT_OK

    if given is not None:  # 两份都是文件：不碰账本
        return finish(given, None)
    packets = sorted(set(args.packet) if args.packet else side.packets)
    try:  # 主线从这个库的账本读（J4.4 起从关系表，以前的从结果库），读的两步是同一个样子（javmem.side.mainline）
        if not queue:  # 只出报告：不拿锁，两步中间有人写就重读
            line = read_main_line(settings, args.library, packets)
            return finish(line.side, line.relation_of)
        with ledger_locks(settings.require_data_dir()):  # 要写待确认：拿着写账、收件的锁，一直拿到写完
            line = read_main_line(settings, args.library, packets, locked=True)
            if not line.from_ledger:
                out("这个库的账本还没有关系表（J4.4 以前写的，主线的关系只在 L3 结果库里，没有关系编号）："
                    "--queue 用不了，整本重建以后再比")
                return EXIT_INPUT
            return finish(line.side, line.relation_of)
    except LedgerBusy as busy:
        out(f"比不了：{busy}，等它跑完再比")
        return EXIT_BUSY


def _status(store: SideStore, out: Callable[[str], None]) -> int:
    groups = store.groups()
    if not groups:
        out("支线的库里还没有组")
    for group in groups:
        rows = store.windows(group.group_id)
        counts = {status: sum(1 for row in rows if row.status == status) for status in ("done", "failed", "skipped", "todo")}
        out(f"{group.packet_id} 第 {group.revision} 版：{group.state}，窗口 {len(rows)}（好了 {counts['done']}，"
            f"抽不成 {counts['failed']}，不发 {counts['skipped']}，没跑 {counts['todo']}）")
    for group_id, reason in store.pending_clears():
        out(f"待清：{group_id}（{reason}）")
    return EXIT_OK
