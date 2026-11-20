"""Graphiti 支线（javmem.side）不用装 Graphiti 的那几层：设置、切 episode、支线的库、扫一遍、跑一批、导出。
体检库用假的（FakeGraph），样例都是编的。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from helpers import World, build_packet

from javmem.config import ConfigError
from javmem.scoring.common import side_from_dict
from javmem.side.config import SideSettings
from javmem.side.episodes import SKIP_PRIVATE, Episode, group_id_of, load_episodes
from javmem.side.export import build_side, narrow, when_text
from javmem.side.graph import SideFormatError
from javmem.side.run import STOP_AFTER_FAILURES, run_packets
from javmem.side.store import SideBusy, SideStore, side_dir, side_lock
from javmem.side.sweep import ORPHAN, PURGED, SENTENCES, SUPERSEDED, WITHDRAWN, sweep

BODY = (
    "## 原话 · 10:30–10:40\n"
    "- [10:30] 我：我在东岸证券开了户，买了 X2。小周是我的客户经理。\n"
    "- [10:31] desk-bot：好的，记下了。\n"
    "- [10:35] 我：X1 FCN 也放在东岸证券。\n"
    "- [10:40] desk-bot：明白。\n"
)
PACKET_ID = "2026-11-08_2330_desk-bot"


def _packet(world: World, **fields: object) -> None:
    world.drop("inbox/desk-bot", build_packet(body=BODY, **fields))
    report = world.intake()
    assert report.count("accepted") == 1, report.outcomes


# ---------- 设置 ----------


def test_settings_only_local_addresses() -> None:
    side = SideSettings.from_env({"JAVMEM_SIDE_QWEN_URL": "http://127.0.0.1:8180/v1/chat/completions"})
    assert side.neo4j_endpoint() == ("127.0.0.1", 7690)
    assert side.qwen_endpoint() == ("127.0.0.1", 8180)
    assert side.lanes == 2
    assert "neo4j_password" not in repr(side)
    for env in ({"JAVMEM_SIDE_QWEN_URL": "http://192.0.2.1:8180/v1/chat/completions"},
                {"JAVMEM_SIDE_NEO4J_URI": "bolt://example.invalid:7687"},
                {"JAVMEM_SIDE_NEO4J_URI": "neo4j://127.0.0.1:7690"},
                {"JAVMEM_SIDE_LANES": "3"},
                {"JAVMEM_SIDE_NEO4J_PASSWORD": "short"}):
        with pytest.raises(ConfigError):
            SideSettings.from_env(env)
    with pytest.raises(ConfigError):
        SideSettings.from_env({}).require_password()


# ---------- episode ----------


def test_episodes_strip_refs_and_keep_sentence_map(world: World) -> None:
    _packet(world)
    episodes, problems = load_episodes(world.settings, "prod", [PACKET_ID])
    assert problems == []
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.skip is None and episode.number == 1
    assert episode.group_id == f"{PACKET_ID}_r1" == group_id_of(PACKET_ID, 1)
    assert "T1 " not in episode.text and "【正文】" not in episode.text and "【上文" not in episode.text
    assert "东岸证券开了户" in episode.text and "我是记忆的主人" not in episode.text  # 开头那句照 render_window 的写法
    assert "“我”是记忆的主人" in episode.text
    assert [line.ref for line in episode.lines][:2] == ["T1", "T2"]
    assert all(line.sentence_id.startswith(PACKET_ID + "#") for line in episode.lines)
    assert episode.reference_time.utcoffset().total_seconds() == 0


def test_private_windows_are_skipped_without_text(world: World) -> None:
    _packet(world, privacy="private")
    episodes, _ = load_episodes(world.settings, "prod", [PACKET_ID])
    assert [one.skip for one in episodes] == [SKIP_PRIVATE]
    assert episodes[0].text == "" and episodes[0].lines and all(line.text == "" for line in episodes[0].lines)


def test_group_id_falls_back_to_a_digest() -> None:
    assert group_id_of("2026-11-08_2330_desk-bot", 2) == "2026-11-08_2330_desk-bot_r2"
    odd = group_id_of("包 号", 1)
    assert odd.startswith("p_") and len(odd) == 34


# ---------- 支线的库 ----------


def test_store_lock_and_library(tmp_path: Path) -> None:
    directory = side_dir(tmp_path, "test")
    with side_lock(directory):
        with pytest.raises(SideBusy):
            with side_lock(directory):
                pass
    SideStore.open(tmp_path, "test").close()
    from javmem.side.store import SideStoreError

    with pytest.raises(SideStoreError):
        SideStore(directory / "side.db", "prod")


# ---------- 假的体检库 ----------


class FakeGraph:
    def __init__(self, fail: Sequence[str] = ()) -> None:
        self.groups: dict[str, list[dict[str, object]]] = {}
        self.fail = set(fail)
        self.calls: list[tuple[str, list[str]]] = []

    async def add_window(self, episode: Episode, previous: Sequence[str]) -> str:
        self.calls.append((episode.window_id, list(previous)))
        await asyncio.sleep(0)  # 真的体检库要等：别的包这时候可以插进来
        if episode.window_id in self.fail:
            raise SideFormatError("extract_edges.edge", "edges.0.fact missing")
        uuid = f"ep-{episode.window_id}"
        self.groups.setdefault(episode.group_id, []).append({"uuid": uuid, "name": episode.window_id})
        return uuid

    async def find_episode(self, group_id: str, window_id: str) -> str | None:
        return next((str(one["uuid"]) for one in self.groups.get(group_id, []) if one["name"] == window_id), None)

    async def delete_group(self, group_id: str) -> tuple[int, int]:
        return len(self.groups.pop(group_id, [])), 0

    async def group_ids(self) -> set[str]:
        return set(self.groups)


def _episode(number: int, *, packet: str = PACKET_ID, skip: str | None = None) -> Episode:
    from datetime import UTC, datetime

    from javmem.pipeline.normalize import Scope
    from javmem.side.episodes import EpisodeLine

    window = f"{packet}#w{number}"
    lines = (EpisodeLine("T1", f"{packet}#1.{number}.1", "" if skip else f"第 {number} 句", 0),)
    return Episode(window, packet, 1, number, Scope(("business",), ()), datetime(2026, 11, 8, 2, 30, tzinfo=UTC),
                   "" if skip else f"文字 {number}\n", "" if skip else f"sha{number}", lines, skip)


def test_run_passes_previous_three_and_records_failures(tmp_path: Path) -> None:
    episodes = [_episode(number) for number in range(1, 6)] + [_episode(6, skip="private")]
    graph = FakeGraph(fail=[f"{PACKET_ID}#w2"])
    with SideStore.open(tmp_path, "test") as store:
        result = asyncio.run(run_packets(episodes, [PACKET_ID], store, graph, out=lambda _: None))
        assert result.stopped is None
        run = result.packets[0]
        assert (run.windows, run.done, run.failed, run.skipped, run.state) == (6, 4, 1, 1, "done")
        assert run.failures == [(f"{PACKET_ID}#w2", "extract_edges.edge")]
        previous = dict(graph.calls)
        assert previous[f"{PACKET_ID}#w5"] == [f"ep-{PACKET_ID}#w1", f"ep-{PACKET_ID}#w3", f"ep-{PACKET_ID}#w4"]
        assert store.window(f"{PACKET_ID}#w2").step == "extract_edges.edge"
        assert [line.text for line in store.lines(f"{PACKET_ID}#w6")] == [""]  # 跳过的只记句子编号，不存文字
        # 再跑一遍：跑过的不再跑；--retry-failed 只重来抽不成的那个
        graph.calls.clear()
        asyncio.run(run_packets(episodes, [PACKET_ID], store, graph, out=lambda _: None))
        assert graph.calls == []
        graph.fail.clear()
        asyncio.run(run_packets(episodes, [PACKET_ID], store, graph, retry_failed=True, out=lambda _: None))
        assert [window for window, _ in graph.calls] == [f"{PACKET_ID}#w2"]


def test_run_stops_after_failures_in_a_row_and_adopts_orphans(tmp_path: Path) -> None:
    episodes = [_episode(number) for number in range(1, 6)]
    graph = FakeGraph(fail=[f"{PACKET_ID}#w{n}" for n in range(1, STOP_AFTER_FAILURES + 1)])
    with SideStore.open(tmp_path, "test") as store:
        result = asyncio.run(run_packets(episodes, [PACKET_ID], store, graph, out=lambda _: None))
        assert result.stopped and "停下" in result.stopped
        assert result.packets == [] or result.packets[0].state == "partial"
        assert store.group(f"{PACKET_ID}_r1").state == "partial"
        # 体检库里已经有、支线的库里还是 todo 的：认回来，不重抽
        graph.fail.clear()
        graph.groups.setdefault(f"{PACKET_ID}_r1", []).append({"uuid": "ep-old", "name": f"{PACKET_ID}#w4"})
        graph.calls.clear()
        asyncio.run(run_packets(episodes, [PACKET_ID], store, graph, out=lambda _: None))
        assert [window for window, _ in graph.calls] == [f"{PACKET_ID}#w5"]
        assert store.window(f"{PACKET_ID}#w4").episode_uuid == "ep-old"


def test_run_restarts_group_when_windows_change(tmp_path: Path) -> None:
    graph = FakeGraph()
    with SideStore.open(tmp_path, "test") as store:
        asyncio.run(run_packets([_episode(1), _episode(2)], [PACKET_ID], store, graph, out=lambda _: None))
        changed = [_episode(1), Episode(**{**_episode(2).__dict__, "sha256": "other"})]
        graph.calls.clear()
        asyncio.run(run_packets(changed, [PACKET_ID], store, graph, out=lambda _: None))
        assert [window for window, _ in graph.calls] == [f"{PACKET_ID}#w1", f"{PACKET_ID}#w2"]
        assert [row["reason"] for row in store.cleared()] == ["窗口变了，整组重来"]


# ---------- 扫一遍 ----------


def _ran(world: World, store: SideStore, graph: FakeGraph) -> None:
    episodes, _ = load_episodes(world.settings, "prod", [PACKET_ID])
    asyncio.run(run_packets(episodes, [PACKET_ID], store, graph, out=lambda _: None))


@pytest.mark.parametrize("change, reason", [
    ("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'", WITHDRAWN),
    ("UPDATE packets SET state = 'superseded'", SUPERSEDED),
    ("UPDATE raw_sentences SET withdrawn_at = '2026-11-09T00:00:00+08:00' WHERE id = (SELECT min(id) FROM raw_sentences)",
     SENTENCES),
])
def test_sweep_clears_whole_group(world: World, change: str, reason: str) -> None:
    _packet(world)
    graph = FakeGraph()
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, graph)
        assert set(graph.groups) == {f"{PACKET_ID}_r1"}
        assert asyncio.run(sweep(world.settings.library("prod").db_path, store, graph)).cleared == []
        world.execute(change)
        result = asyncio.run(sweep(world.settings.library("prod").db_path, store, graph))
        assert result.cleared == [(f"{PACKET_ID}_r1", reason)]
        assert result.rerun == ([PACKET_ID] if reason == SENTENCES else [])
        assert graph.groups == {} and store.groups() == []
        assert store.conn.execute("SELECT count(*) FROM lines").fetchone()[0] == 0  # 原文跟着删了
        assert [row["reason"] for row in store.cleared()] == [reason]


def test_sweep_purged_pending_and_orphans(world: World) -> None:
    _packet(world)
    graph = FakeGraph()
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, graph)
        world.execute("PRAGMA foreign_keys = OFF")
        world.execute("DELETE FROM raw_sentences")
        world.execute("DELETE FROM packets")
        ledger = world.settings.library("prod").db_path
        result = asyncio.run(sweep(ledger, store, None))  # 体检库没开：记成待清
        assert result.pending == [(f"{PACKET_ID}_r1", PURGED)] and graph.groups
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", PURGED)]
        graph.groups["leftover_r1"] = [{"uuid": "x", "name": "x"}]
        result = asyncio.run(sweep(ledger, store, graph))
        assert sorted(result.cleared) == [(f"{PACKET_ID}_r1", PURGED), ("leftover_r1", ORPHAN)]
        assert graph.groups == {} and store.pending_clears() == []


# ---------- 导出 ----------


def test_narrow_picks_the_matching_block() -> None:
    blocks = [{"sids": ["p#1.1.1"], "text": "我在东岸证券开了户，买了 X2。"},
              {"sids": ["p#1.2.1"], "text": "好的，记下了。"}]
    found, whole = narrow("主人在东岸证券开户", blocks, ["主人", "东岸证券"], {})
    assert (found, whole) == (["p#1.1.1"], False)
    found, whole = narrow("完全不相干", blocks, ["某某"], {})
    assert whole and found == ["p#1.1.1", "p#1.2.1"]


def test_when_text_day_or_minute() -> None:
    from datetime import UTC, datetime

    assert when_text(datetime(2026, 10, 23, tzinfo=UTC)) == ("2026-10-23", "day")
    assert when_text(datetime(2026, 10, 23, 8, 54, tzinfo=UTC)) == ("2026-10-23T16:54+08:00", "minute")
    assert when_text(None) == (None, "unknown")


def test_build_side_round_trips_through_the_scoring_format(world: World, tmp_path: Path) -> None:
    _packet(world)
    graph = FakeGraph()
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, graph)
        group = store.groups()[0]
        window = store.windows(group.group_id)[0]
        store.record_call(group_id=group.group_id, window_id=window.window_id, prompt="side:extract_edges.edge", attempt=1,
                          status="ok", input_tokens=100, output_tokens=20, cached_tokens=5, seconds=3.0)
        store.record_call(group_id=group.group_id, window_id=window.window_id, prompt="side:extract_edges.edge", attempt=1,
                          status="format_error", input_tokens=0, output_tokens=0, cached_tokens=0, seconds=0.0)
        rows = {group.group_id: {
            "entities": [
                {"uuid": "n1", "name": "主人", "labels": ["Entity", "Person"]},
                {"uuid": "n2", "name": "东岸证券", "labels": ["Entity", "Organization"]},
                {"uuid": "n3", "name": "X2", "labels": ["Entity", "Asset"]},
                {"uuid": "n4", "name": "我", "labels": ["Entity"]},
            ],
            "edges": [
                {"uuid": "e1", "name": "CLIENT_OF", "fact": "主人在东岸证券开了户", "source": "n1", "target": "n2",
                 "valid_at": "2026-11-08T02:30:00Z", "invalid_at": None, "expired_at": None,
                 "episodes": [window.episode_uuid]},
                {"uuid": "e2", "name": "BOUGHT", "fact": "主人买了 X2", "source": "n4", "target": "n3",
                 "valid_at": None, "invalid_at": None, "expired_at": None, "episodes": [window.episode_uuid]},
                {"uuid": "e3", "name": "HOLDS", "fact": "主人持有 X2", "source": "n1", "target": "n3",
                 "valid_at": "2026-11-08T00:00:00Z", "invalid_at": "2026-11-09T00:00:00Z", "expired_at": None,
                 "episodes": ["别的窗口"]},
            ],
            "episodes": [],
        }}
        side, edge_rows, stats = build_side([group], rows, store, frozenset(), described={"系统": "测试"})
    data = json.loads(json.dumps(side.as_dict(), ensure_ascii=False))
    again = side_from_dict(data)
    assert again.packets == frozenset({PACKET_ID}) and again.revisions == {PACKET_ID: 1}
    assert set(again.relations) == {"r:e1"}  # e3 的 episode 对不上窗口，没有出处，不算
    relation = again.relations["r:e1"]
    assert (relation.subject, relation.relation, relation.object, relation.state) == ("n1", "客户是", "n2", "生效")
    assert relation.when.precision == "minute"
    assert relation.evidence and relation.evidence[0].startswith(PACKET_ID + "#")
    assert set(again.facts) == {"f:e1", "f:e2"}
    assert again.entities["n4"].type == "项目" and again.entities["n4"].aliases == ("主人",)
    assert again.entities["n1"].aliases == ()
    assert stats["unmapped"] == {"BOUGHT": 1} and stats["edges"]["对不上窗口"] == 1
    cost = again.cost[PACKET_ID]
    assert (cost.calls, cost.input_tokens, cost.output_tokens, cost.cached_tokens) == (1, 100, 20, 5)
    assert {row["uuid"] for row in edge_rows} == {"e1", "e2"}


# ---------- 审阅查出来的（11-17） ----------


class ExportGraph(FakeGraph):
    settings = SideSettings()

    async def export_group(self, group_id: str) -> dict[str, list[dict[str, object]]]:
        return {"entities": [], "edges": [], "episodes": []}


def test_export_sweeps_first_so_withdrawn_packets_never_reach_the_file(world: World, tmp_path: Path) -> None:
    import argparse

    from javmem.side.export import export_side

    _packet(world)
    graph = ExportGraph()
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, graph)
        world.execute("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'")
        out_file = tmp_path / "out" / "side.json"
        args = argparse.Namespace(library="prod", packet=None, out=str(out_file), edges_out=None)
        assert asyncio.run(export_side(graph, world.settings, args, store, lambda _: None)) == 0
        assert graph.groups == {} and store.groups() == []
    assert json.loads(out_file.read_text(encoding="utf-8"))["packets"] == []


def test_sweep_without_checkup_db_still_drops_the_text(world: World) -> None:
    _packet(world)
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, FakeGraph())
        world.execute("UPDATE raw_sentences SET withdrawn_at = '2026-11-09T00:00:00+08:00'"
                      " WHERE id = (SELECT min(id) FROM raw_sentences)")
        result = asyncio.run(sweep(world.settings.library("prod").db_path, store, None))
        assert result.pending == [(f"{PACKET_ID}_r1", SENTENCES)]
        assert store.conn.execute("SELECT count(*) FROM lines").fetchone()[0] == 0  # 撤回的句子不留在支线的库里
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", SENTENCES)]
        graph = FakeGraph()
        graph.groups[f"{PACKET_ID}_r1"] = [{"uuid": "x", "name": "x"}]
        result = asyncio.run(sweep(world.settings.library("prod").db_path, store, graph))
        assert result.cleared == [(f"{PACKET_ID}_r1", SENTENCES)] and graph.groups == {} and store.groups() == []


def test_cli_sweep_with_checkup_db_down_drops_text_and_records_pending(world: World,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("neo4j")
    import argparse
    import socket

    from javmem.side.cli import EXIT_STOPPED
    from javmem.side.cli import run as side_run

    _packet(world)
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, FakeGraph())
    world.execute("UPDATE packets SET withdrawn_at = '2026-11-09T00:00:00+08:00'")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # 没人在听的本机端口：体检库连不上
    monkeypatch.setenv("JAVMEM_SIDE_NEO4J_URI", f"bolt://127.0.0.1:{port}")
    monkeypatch.setenv("JAVMEM_SIDE_NEO4J_PASSWORD", "made-up-password-123")
    lines: list[str] = []
    args = argparse.Namespace(side_command="sweep", library="prod")
    assert side_run(args, world.settings, out=lines.append) == EXIT_STOPPED
    with SideStore.open(world.data, "prod") as store:
        assert store.conn.execute("SELECT count(*) FROM lines").fetchone()[0] == 0
        assert store.pending_clears() == [(f"{PACKET_ID}_r1", WITHDRAWN)]
    assert any("待清" in line for line in lines)


def test_mail_windows_are_not_sent_yet(world: World) -> None:
    from helpers import MAIL_ID, MAIL_PACKET

    from javmem.side.episodes import SKIP_MAIL

    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.intake()
    episodes, _ = load_episodes(world.settings, "prod", [MAIL_ID])
    assert episodes and all(one.skip == SKIP_MAIL and one.text == "" for one in episodes)


def _restated_window() -> object:
    from datetime import UTC, datetime

    from javmem.packet.model import ORIGIN_SAID, ROLE_BOT, ROLE_OWNER
    from javmem.pipeline.normalize import NEW, RESTATEMENT, Scope
    from javmem.pipeline.window import Window, WindowBlock, WindowLine

    when = datetime(2026, 11, 8, 2, 30, tzinfo=UTC)
    owner = (WindowLine("T1", f"{PACKET_ID}#1.1.1", PACKET_ID, 1, "我在东岸证券开了户。", NEW, ORIGIN_SAID, False),)
    bot = (WindowLine("C1", f"{PACKET_ID}#1.2.1", PACKET_ID, 1, "你在东岸证券开了户。", RESTATEMENT, ORIGIN_SAID, False),
           WindowLine("T2", f"{PACKET_ID}#1.2.2", PACKET_ID, 1, "记下了。", NEW, ORIGIN_SAID, False))
    blocks = (WindowBlock("u1", PACKET_ID, "我", ROLE_OWNER, when, ORIGIN_SAID, False, owner),
              WindowBlock("u2", PACKET_ID, "desk-bot", ROLE_BOT, when, ORIGIN_SAID, False, bot))
    return Window(f"{PACKET_ID}#w1", "desk-bot", "chat", Scope(("business",), ()), blocks)


def test_c_numbers_are_stripped_and_never_evidence(tmp_path: Path) -> None:
    from javmem.side.episodes import episode_of

    episode = episode_of(_restated_window(), 1, None)  # type: ignore[arg-type]
    assert "C1 " not in episode.text and "T1 " not in episode.text and "〔Bot 复述〕你在东岸证券" in episode.text
    assert [line.ref for line in episode.lines] == ["T1", "C1", "T2"]
    with SideStore.open(tmp_path, "test") as store:
        group = store.start_group(episode)
        store.put_window(episode)
        store.finish_window(episode.window_id, "done", episode_uuid="ep1")
        rows = {group.group_id: {
            "entities": [{"uuid": "n1", "name": "主人", "labels": ["Entity", "Person"]},
                         {"uuid": "n2", "name": "东岸证券", "labels": ["Entity", "Organization"]}],
            "edges": [{"uuid": "e1", "name": "CLIENT_OF", "fact": "你在东岸证券开了户", "source": "n1", "target": "n2",
                       "valid_at": None, "invalid_at": None, "expired_at": None, "episodes": ["ep1"]}],
            "episodes": []}}
        side, _, _ = build_side([group], rows, store, frozenset())
    assert side.relations["r:e1"].evidence == (f"{PACKET_ID}#1.1.1",)  # C1（Bot 复述）不当证据


def test_run_dedupes_repeated_packets(tmp_path: Path) -> None:
    graph = FakeGraph()
    with SideStore.open(tmp_path, "test") as store:
        asyncio.run(run_packets([_episode(1), _episode(2)], [PACKET_ID, PACKET_ID], store, graph, parallel=2,
                                out=lambda _: None))
    assert [window for window, _ in graph.calls] == [f"{PACKET_ID}#w1", f"{PACKET_ID}#w2"]


def test_window_of_an_older_revision_is_not_reused(tmp_path: Path) -> None:
    import dataclasses

    from javmem.side.store import SideStoreError

    with SideStore.open(tmp_path, "test") as store:
        old = _episode(1)
        store.start_group(old)
        store.put_window(old)
        new = dataclasses.replace(old, revision=2)
        store.start_group(new)
        with pytest.raises(SideStoreError):
            store.put_window(new)


def test_cost_counts_only_the_current_run_of_a_group(tmp_path: Path) -> None:
    with SideStore.open(tmp_path, "test") as store:
        episode = _episode(1)
        store.start_group(episode)
        store.conn.execute("INSERT INTO calls (at, group_id, window_id, prompt, attempt, status, input_tokens,"
                           " output_tokens, cached_tokens, seconds) VALUES ('2000-01-01T00:00:00+08:00', ?, ?,"
                           " 'side:x', 1, 'ok', 7, 7, 0, 1.0)", (episode.group_id, episode.window_id))
        store.drop_group(episode.group_id, "测试", 0, 0)
        assert len(store.calls([episode.group_id])) == 1  # 调用的记录留着
        group = store.start_group(episode)  # 重来：新的一回
        store.put_window(episode)
        store.record_call(group_id=episode.group_id, window_id=episode.window_id, prompt="side:x", attempt=1,
                          status="ok", input_tokens=100, output_tokens=10, cached_tokens=0, seconds=1.0)
        side, _, _ = build_side([group], {}, store, frozenset())
        assert (side.cost[PACKET_ID].input_tokens, side.cost[PACKET_ID].calls) == (100, 1)


def test_skipped_windows_are_pending_not_misses(tmp_path: Path) -> None:
    with SideStore.open(tmp_path, "test") as store:
        done, skipped = _episode(1), _episode(2, skip="leftover")
        group = store.start_group(done)
        store.put_window(done)
        store.finish_window(done.window_id, "done", episode_uuid="ep1")
        store.put_window(skipped)
        assert [line.text for line in store.lines(skipped.window_id)] == [""]  # 只记句子编号，不存文字
        side, _, _ = build_side([group], {}, store, frozenset())
    assert side.pending == frozenset({f"{PACKET_ID}#1.2.1"})


def test_a_saved_window_survives_a_database_hiccup(tmp_path: Path) -> None:
    """存进去以后才断的（体检库回提交结果时断了、Ctrl+C）：存进去的是完整的一个事务，不整组删，下次认回来。"""

    class Hiccup(FakeGraph):
        async def add_window(self, episode: Episode, previous: Sequence[str]) -> str:
            uuid = await super().add_window(episode, previous)
            if episode.number == 2:
                raise ConnectionError("提交以后断了")
            return uuid

    graph = Hiccup()
    with SideStore.open(tmp_path, "test") as store:
        result = asyncio.run(run_packets([_episode(1), _episode(2)], [PACKET_ID], store, graph, out=lambda _: None))
        assert result.stopped and "ConnectionError" in result.stopped
        assert store.window(f"{PACKET_ID}#w2").status == "todo" and len(graph.groups[f"{PACKET_ID}_r1"]) == 2
        again = asyncio.run(run_packets([_episode(1), _episode(2)], [PACKET_ID], store, graph, out=lambda _: None))
        assert again.packets[0].adopted == 1 and store.window(f"{PACKET_ID}#w2").status == "done"


def test_a_window_still_under_an_older_revision_stops_before_making_a_group(tmp_path: Path) -> None:
    import dataclasses

    with SideStore.open(tmp_path, "test") as store:
        old = _episode(1)
        store.start_group(old)
        store.put_window(old)
        new = dataclasses.replace(old, revision=2)
        result = asyncio.run(run_packets([new], [PACKET_ID], store, FakeGraph(), out=lambda _: None))
        assert result.stopped and "sweep" in result.stopped
        assert store.group(new.group_id) is None  # 不留空的新组


def test_an_outage_wins_over_a_format_error() -> None:
    from javmem.side.graph import SideTransportError, _worst

    assert isinstance(_worst([SideTransportError("连不上"), SideFormatError("x", "y")]), SideTransportError)
    assert isinstance(_worst([SideFormatError("x", "y")]), SideFormatError)


def test_unexpected_errors_stop_the_batch_cleanly(tmp_path: Path) -> None:
    class Broken(FakeGraph):
        async def add_window(self, episode: Episode, previous: Sequence[str]) -> str:
            raise RuntimeError("坏了")

    with SideStore.open(tmp_path, "test") as store:
        result = asyncio.run(run_packets([_episode(1)], [PACKET_ID], store, Broken(), out=lambda _: None))
        assert result.stopped and "RuntimeError" in result.stopped
        assert store.window(f"{PACKET_ID}#w1").status == "todo"
