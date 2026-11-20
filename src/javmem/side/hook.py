"""撤回、彻底删除接上支线（J6.4）：`javmem withdraw`、`javmem purge` 做完以后调 after_withdraw。

- 第二证人的待确认（witness_mismatch）先按账本的现状删（javmem.side.witness.drop_stale：包不是现行的了、不是比的时候
  那一版、证据撤回了、进了待确认以后包里又有句子撤回了、主线那一边的关系没了），不拿支线的锁：支线正在跑、体检库连
  不上、这个库根本没跑过支线（只比过）的时候，撤回了的名字也不留在待确认里。账本里没有这一种待确认的不碰账本。
- 这个库没用过支线（没有 side.db）：别的什么都不做、什么都不打印。
- 再扫一遍（javmem.side.sweep）：撤回了的、彻底删除了的、换了版本的包，有句子单条撤回了的包，体检库里整组删、支线的
  库里跟着删，这些包的第二证人待确认也删。先只读算一遍有没有要删的组，没有就不去连体检库。
- 撤回本身已经做完了，这里出什么错都不让撤回失败、不把它卡住，只打印一句（不带原文）：
  - 连体检库给短的超时（QUICK_TIMEOUT），连上加核标记一共最多等 OPEN_DEADLINE 秒，等之前先说一声；连上以后整个扫一遍
    最多 SWEEP_DEADLINE 秒。连不上（容器停着、不回话、没配密码、没装 Graphiti 那一套）、过了时限：支线库里这几组的原文
    先删、记成待清，下次支线的命令连上了先整组删完；删到一半出了错的，删了的照样说、剩下的照连不上办；
  - 支线正在跑（拿不到 .side.lock）：它跑完那一遍会扫，撤回了的到时整组删；
  - 写账、收件在跑，账本被别的程序锁着，或者账本出了别的错：第二证人的待确认这一回没清，下一回（支线的命令、下一次撤回）
    再清，按账本的现状清，哪一回都清得掉；
  - 按了 Ctrl+C：扫到一半的，先把要删的原文删了、记待清（只动本机的库，很快）；说一句（还没扫的照实说原文可能还在），
    不冒出来。
- 彻底删除的那几版进过体检库的，提示一句：Neo4j 社区版删了的东西磁盘上可能还留着，要干净就把体检库整个重建。
  已经删了的、还在待清的、支线正在跑（不知道）的，三种说法分开；这一次一版都没删的（早就删过了）不说。
  彻底删除以后支线的库整个重写（scrub）、账本的 WAL 写回去截掉：删了的字（secure_delete 抹成 0）不留在文件里。
- 驱动自己的提示压掉；闸拦下的连接记进支线的库、打印一句。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

from javmem.config import ConfigError, Settings
from javmem.side.store import SideBusy, SideStore, side_dir, side_lock
from javmem.side.sweep import SweepResult, count_todo, sweep
from javmem.side.witness import LedgerBusy, checkpoint, clean, has_rows

QUICK_TIMEOUT = 5.0  # 秒：撤回时连体检库，连不上、不回话、事务重试最多等这么久
OPEN_DEADLINE = 15.0  # 秒：连上加核标记一共最多等这么久，过了就当连不上
SWEEP_DEADLINE = 60.0  # 秒：连上以后整个扫一遍（删组）最多这么久，过了剩下的照连不上办（整组删两遍不要紧）

_REBUILD = "（停容器、删它的数据目录、重起，javmem side reset --yes，再 init、run）"
PURGE_DONE = ("彻底删除的这几版进过体检库，那一组已经删了；可 Neo4j 社区版删了的东西磁盘上可能还留着，"
              "要干净就把体检库整个重建" + _REBUILD)
PURGE_PENDING = ("彻底删除的这几版进过体检库，那一组还没从体检库里删掉（记了待清：体检库起来以后跑一遍 javmem side sweep）；"
                 "删完以后 Neo4j 社区版删了的东西磁盘上可能还留着，要干净就把体检库整个重建" + _REBUILD)
PURGE_BUSY = ("支线正在跑：彻底删除的包要是进过体检库，它跑完那一遍会删那一组；Neo4j 社区版删了的东西磁盘上可能还留着，"
              "要干净就把体检库整个重建" + _REBUILD)

Purged = Mapping[str, Collection[int] | None] | Collection[str]


def _wanted(purged: Purged) -> dict[str, set[int] | None]:
    """彻底删除的：包号 → 这一次删了哪几版（None 是全部版本）。只给包号的算全部版本；一版都没删的（早就删过了）不算。"""
    if isinstance(purged, Mapping):
        out = {str(packet): None if revisions is None else {int(one) for one in revisions}
               for packet, revisions in purged.items()}
        return {packet: revisions for packet, revisions in out.items() if revisions is None or revisions}
    return {str(packet): None for packet in purged}


def _label(packet: str, revisions: Collection[int] | None) -> str:
    return packet if not revisions else f"{packet} 第 {'、'.join(map(str, sorted(revisions)))} 版"


async def _online(ledger: Path, store: SideStore, result: SweepResult) -> tuple[str | None, list[Any]]:
    """连真的体检库扫。返回（连不上的原因，没连不上是 None；闸拦下的）。连不上、过了时限、中途出错：剩下的先删原文、
    记待清。"""
    reason: str | None = None
    blocked: list[Any] = []
    try:
        from javmem.side.config import SideSettings
        from javmem.side.graph import SideGraph
        from javmem.side.netguard import NetGuard

        settings = SideSettings.from_env()
        settings.require_password()
        guard = NetGuard([settings.neo4j_endpoint()[1]])
        try:
            with guard:
                real = SideGraph(settings, store, timeouts=QUICK_TIMEOUT)
                try:
                    await asyncio.wait_for(real.open(), OPEN_DEADLINE)
                    await asyncio.wait_for(sweep(ledger, store, real, result=result), SWEEP_DEADLINE)
                finally:
                    try:
                        await real.close()
                    except Exception:  # noqa: BLE001 - 关不上不算事，删了的已经删了
                        pass
        finally:
            blocked = list(guard.blocked)
        return None, blocked
    except ConfigError:
        reason = "没配体检库"
    except TimeoutError:
        reason = "等了太久"
    except Exception as error:  # noqa: BLE001 - 连不上、不回话、没装、删到一半出错：剩下的都按连不上办
        reason = type(error).__name__
    await sweep(ledger, store, None, result=result)
    return reason, blocked


def _purge_groups(store: SideStore, wanted: Mapping[str, set[int] | None]) -> tuple[list[str], list[str]]:
    """彻底删除的这几版里进过体检库的：已经删了的、还没删的（记了待清，或者还在组里）。"""
    current = {(group.packet_id, group.revision) for group in store.groups()}
    cleared = {(str(row["packet_id"]), int(row["revision"])) for row in store.cleared()}
    done: list[str] = []
    left: list[str] = []
    for packet, revisions in sorted(wanted.items()):
        seen = {revision for one, revision in current | cleared if one == packet}
        for revision in sorted(seen if revisions is None else seen & revisions):
            (left if (packet, revision) in current else done).append(_label(packet, [revision]))
    return done, left


def after_withdraw(settings: Settings, library: str, *, purged: Purged = (), out: Callable[[str], None] = print,
                   graph: Any = None) -> None:
    """graph 给了就用它（测试用假的），不连真的体检库。purged：彻底删除的包号 → 这一次删了哪几版。"""
    data_dir = settings.require_data_dir()
    driver_log = logging.getLogger("neo4j")
    level = driver_log.level
    driver_log.setLevel(logging.ERROR)  # 驱动的提示（“这个标签还不存在”这类）不打出来
    state = _State()
    try:
        _after_withdraw(settings.library(library).db_path, data_dir, side_dir(data_dir, library), library,
                        _wanted(purged), out, graph, state)
    except KeyboardInterrupt:
        if state.offline_swept:
            out("按了 Ctrl+C：撤回已经做完了，支线没扫完（要删的组，支线库里的原文删了、记了待清）；之后跑一遍 javmem side sweep")
        else:
            out("按了 Ctrl+C：撤回已经做完了，支线还没扫（撤回了的原文可能还在支线的库里）；之后跑一遍 javmem side sweep")
    finally:
        driver_log.setLevel(level)


class _State:
    """按了 Ctrl+C 时说的话要对得上：本机这边删原文、记待清那一步做完了没有。"""

    offline_swept = False


class _Cleaner:
    """第二证人的待确认：清了几条、没清成的为什么（出过一回错，这一次就不再试，说一句）。"""

    def __init__(self, ledger: Path, data_dir: Path) -> None:
        self.ledger = ledger
        self.data_dir = data_dir
        self.dropped = 0
        self.problem: str | None = None

    def run(self, packets: Collection[str] = ()) -> None:
        if self.problem is not None:
            return
        try:
            self.dropped += clean(self.ledger, self.data_dir, packets)
        except LedgerBusy as busy:
            self.problem = str(busy)
        except Exception as error:  # noqa: BLE001 - 撤回已经做完了：账本出了别的错也只提示
            self.problem = type(error).__name__

    def say(self, out: Callable[[str], None]) -> None:
        if self.dropped:
            out(f"第二证人的待确认删了 {self.dropped} 条（包撤回了、换了版本、彻底删除了，或者证据撤回了）")
        if self.problem is not None:
            out(f"第二证人的待确认没清（{self.problem}）：之后跑一遍 javmem side sweep，或者下一次撤回时再清")


def _after_withdraw(ledger: Path, data_dir: Path, directory: Path, library: str, wanted: Mapping[str, set[int] | None],
                    out: Callable[[str], None], graph: Any, state: _State) -> None:
    cleaner = _Cleaner(ledger, data_dir)
    try:
        if has_rows(ledger):  # 第二证人的待确认先按账本的现状删：不等支线的锁，没跑过支线（只比过）的库也删
            cleaner.run()
        if not (directory / "side.db").exists():
            return
        try:
            with side_lock(directory), SideStore.open(data_dir, library) as store:
                result = SweepResult()
                offline: str | None = None
                blocked: list[Any] = []
                todo = count_todo(ledger, store)
                try:
                    if todo and graph is not None:
                        asyncio.run(sweep(ledger, store, graph, result=result))
                    elif todo:
                        out(f"体检库里有 {todo} 组要删：连体检库（连不上最多等 {OPEN_DEADLINE:.0f} 秒）……")
                        offline, blocked = asyncio.run(_online(ledger, store, result))
                except KeyboardInterrupt:  # 按了 Ctrl+C：本机这边先删原文、记待清（很快），再停
                    asyncio.run(sweep(ledger, store, None, result=result))
                    state.offline_swept = True
                    raise
                for item in blocked:
                    store.record_blocked(item.at, item.host, item.port, item.where)
                done, left = _purge_groups(store, wanted)
                if wanted:  # 彻底删除：删了的字不留在支线的库的文件里
                    store.scrub()
        except SideBusy:
            out("支线正在跑：它跑完那一遍会扫，撤回了的到时在体检库里整组删")
            if wanted:
                out(PURGE_BUSY + "：" + "、".join(_label(packet, revisions) for packet, revisions in sorted(wanted.items())))
            return
        except KeyboardInterrupt:
            raise
        except Exception as error:  # noqa: BLE001 - 撤回已经做完了，支线没扫成只提示
            out(f"支线没扫成（{type(error).__name__}）：之后跑一遍 javmem side sweep")
            return
        for group_id, reason in result.cleared:
            out(f"体检库里删了 {group_id}（{reason}）")
        if result.pending:
            out(f"体检库连不上（{offline}）：记了待清 {len(result.pending)} 组，支线库里这几组的原文已经删了，"
                "体检库起来以后跑一遍 javmem side sweep")
        if blocked:
            out(f"闸拦下了 {len(blocked)} 次往外的连接：" +
                "，".join(sorted({f"{item.host}:{item.port}（{item.where}）" for item in blocked})))
        if result.packets:  # 这几个包的组删了（或者记了待清）：它们的待确认跟着删
            cleaner.run(result.packets)
        if done:
            out(PURGE_DONE + "：" + "、".join(done))
        if left:
            out(PURGE_PENDING + "：" + "、".join(left))
    finally:
        if wanted and cleaner.dropped:  # 彻底删除：删掉的待确认（secure_delete 抹成 0）落到账本文件里，WAL 截掉
            checkpoint(ledger)
        cleaner.say(out)
