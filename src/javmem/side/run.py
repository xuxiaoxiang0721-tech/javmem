"""跑一批包：每个包（每一版）一组，窗口按先后一个一个加进体检库（Graphiti 要求同一组按顺序加）。

- 每个窗口一个 episode，上文是同一个包的前 PREVIOUS_EPISODES 个跑成了的窗口。
- 跳过的窗口（private、打码没打干净、太长、邮件）只记数和句子编号。
- Qwen 重问以后格式还不对（SideFormatError）：这个窗口记“抽不成”（failed，记错在 Graphiti 的哪一步），接着跑下一个窗口；
  连着 3 个窗口抽不成就停（多半是哪里配错了）。
- 本机 Qwen、向量服务连不上（SideTransportError），或者别的没想到的出错：整批停下，没跑的窗口留着 todo，下次接着跑。
- 一个窗口我们的客户端出了错（格式错、连不上）、体检库里却已经有了它的 episode（Graphiti 哪一步把错吞了，存进去的
  那一份不可信）：整组删了，下次整个包重跑，不当跑成了的认回来。别的出错（体检库断了、Ctrl+C）不删：Graphiti
  一个窗口在一个事务里存，存进去了就是完整的。
- 跑到一半进程没了：体检库里已经有、支线的库里还是 todo 的窗口，下次按 episode 的名字认回来，不重抽。
- 窗口的文字和上次记的不一样了（切窗、写法改了），整组删了重来。
- parallel：同时跑几个包（不同的组），Qwen 那边的并发另由客户端卡着。同一个包点了两遍的只跑一遍。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from javmem.side.episodes import Episode
from javmem.side.graph import SideFormatError, SideTransportError
from javmem.side.store import SideStore

PREVIOUS_EPISODES = 3
STOP_AFTER_FAILURES = 3


class WindowGraph(Protocol):
    async def add_window(self, episode: Episode, previous: Sequence[str]) -> str: ...

    async def delete_group(self, group_id: str) -> tuple[int, int]: ...

    async def find_episode(self, group_id: str, window_id: str) -> str | None: ...


@dataclass
class PacketRun:
    packet_id: str
    group_id: str
    windows: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    adopted: int = 0  # 体检库里已经有、认回来的
    seconds: float = 0.0
    state: str = ""
    failures: list[tuple[str, str]] = field(default_factory=list)  # (窗口, 错在哪一步)


@dataclass
class RunResult:
    packets: list[PacketRun] = field(default_factory=list)
    stopped: str | None = None


class _Stop(Exception):
    pass


def _by_packet(episodes: Sequence[Episode], order: Sequence[str]) -> list[list[Episode]]:
    groups: dict[str, list[Episode]] = {}
    for episode in episodes:
        groups.setdefault(episode.packet_id, []).append(episode)
    return [sorted(groups[packet], key=lambda one: one.number) for packet in dict.fromkeys(order) if packet in groups]


async def _restart_if_changed(episodes: Sequence[Episode], store: SideStore, graph: WindowGraph,
                              out: Callable[[str], None]) -> None:
    group_id = episodes[0].group_id
    old = {row.window_id: row for row in store.windows(group_id)}
    if not old:
        return
    changed = any(
        (episode.window_id not in old) or (old[episode.window_id].text_sha != (episode.sha256 or None))
        for episode in episodes
    ) or len(old) != len(episodes)
    if changed:
        nodes, edges = await graph.delete_group(group_id)
        store.drop_group(group_id, "窗口变了，整组重来", nodes, edges)
        out(f"{episodes[0].packet_id}：窗口和上次的不一样了，整组删了重来")


async def _discard_if_saved(episode: Episode, store: SideStore, graph: WindowGraph) -> bool:
    """出了错的窗口，体检库里却有了它的 episode：整组删（存进去的那一份不可信）。删了返回 True。"""
    if await graph.find_episode(episode.group_id, episode.window_id) is None:
        return False
    nodes, edges = await graph.delete_group(episode.group_id)
    store.drop_group(episode.group_id, "窗口出了错却已经存进体检库，整组重来", nodes, edges)
    return True


async def run_packet(episodes: Sequence[Episode], store: SideStore, graph: WindowGraph, *, retry_failed: bool = False,
                     deadline: float | None = None, stop: asyncio.Event | None = None,
                     out: Callable[[str], None] = print) -> PacketRun:
    first = episodes[0]
    report = PacketRun(first.packet_id, first.group_id, windows=len(episodes))
    for episode in episodes:  # 窗口编号里没有版本：还记在上一版的组里的，先扫一遍，不建空的新组
        found = store.window(episode.window_id)
        if found is not None and found.group_id != episode.group_id:
            raise _Stop(f"{episode.window_id} 还记在上一版的组里：先 javmem side sweep 把上一版删了")
    await _restart_if_changed(episodes, store, graph, out)
    store.start_group(first)
    for episode in episodes:
        store.put_window(episode)
        if retry_failed:
            store.reset_window(episode.window_id)
    failures_in_a_row = 0
    dropped = False
    try:
        for episode in episodes:
            row = store.window(episode.window_id)
            if row is None or row.status != "todo":
                continue
            if deadline is not None and time.monotonic() > deadline:
                raise _Stop("到了 --max-minutes，不再开始新的窗口")
            if stop is not None and stop.is_set():
                raise _Stop("别的包停下了，这个包也不再开始新的窗口")
            found = await graph.find_episode(episode.group_id, episode.window_id)
            if found is not None:
                store.finish_window(episode.window_id, "done", episode_uuid=found)
                report.adopted += 1
                continue
            done = [one for one in store.windows(episode.group_id) if one.status == "done" and one.number < episode.number]
            previous = [one.episode_uuid for one in done[-PREVIOUS_EPISODES:] if one.episode_uuid]
            started = time.monotonic()
            try:
                uuid = await graph.add_window(episode, previous)
            except SideFormatError as error:
                if await _discard_if_saved(episode, store, graph):
                    dropped = True
                    raise _Stop(f"{episode.window_id} 出了错却已经存进体检库：这一组整组删了，下次重跑") from None
                seconds = time.monotonic() - started
                store.finish_window(episode.window_id, "failed", reason="format", step=error.prompt, seconds=seconds)
                report.failures.append((episode.window_id, error.prompt))
                report.seconds += seconds
                out(f"{episode.window_id}：抽不成（{error.prompt}，重问以后格式还是不对），{seconds:.0f} 秒")
                failures_in_a_row += 1
                if failures_in_a_row >= STOP_AFTER_FAILURES:
                    raise _Stop(f"连着 {STOP_AFTER_FAILURES} 个窗口抽不成，停下") from None
                continue
            except SideTransportError:
                # 我们的客户端连不上、Graphiti 却存了（它哪一步吞了错）：存进去的不可信，整组删
                if await _discard_if_saved(episode, store, graph):
                    dropped = True
                raise
            # 别的出错（体检库断了、Ctrl+C）不删：Graphiti 一个窗口在一个事务里存，存进去了就是完整的，下次认回来
            seconds = time.monotonic() - started
            failures_in_a_row = 0
            store.finish_window(episode.window_id, "done", episode_uuid=uuid, seconds=seconds)
            report.seconds += seconds
            out(f"{episode.window_id}：好了，{seconds:.0f} 秒")
    finally:
        if dropped:
            report.state = "dropped"
        else:
            report.state = store.finish_group(first.group_id)
            rows = store.windows(first.group_id)
            report.done = sum(1 for row in rows if row.status == "done")
            report.failed = sum(1 for row in rows if row.status == "failed")
            report.skipped = sum(1 for row in rows if row.status == "skipped")
    return report


async def run_packets(episodes: Sequence[Episode], order: Sequence[str], store: SideStore, graph: WindowGraph, *,
                      parallel: int = 1, retry_failed: bool = False, deadline: float | None = None,
                      out: Callable[[str], None] = print) -> RunResult:
    result = RunResult()
    order = list(dict.fromkeys(order))
    queue = _by_packet(episodes, order)
    gate = asyncio.Semaphore(max(1, parallel))
    stop = asyncio.Event()

    async def one(packet: list[Episode]) -> None:
        async with gate:
            if stop.is_set():
                return
            try:
                result.packets.append(await run_packet(packet, store, graph, retry_failed=retry_failed, deadline=deadline,
                                                       stop=stop, out=out))
            except _Stop as error:
                result.stopped = result.stopped or str(error)
                stop.set()
            except SideTransportError as error:
                result.stopped = str(error)
                stop.set()
            except Exception as error:  # noqa: BLE001 - 没想到的出错：只记种类（不带原文），整批停下
                result.stopped = result.stopped or f"出错停下：{type(error).__name__}"
                stop.set()

    await asyncio.gather(*(one(packet) for packet in queue))
    result.packets.sort(key=lambda run: order.index(run.packet_id) if run.packet_id in order else 0)
    return result
