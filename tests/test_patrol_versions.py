"""巡检报模型、版本变了（D205 ⑪，“巡检报模型、版本变了”的做法）：实际模型名照数据目录那一份基准（javmem versions --save
不写文件名存的）比；没存过基准、基准读不了的，和基准比的那几样这回没看（checks_skipped）。都不连网、不调模型：结果库、
soft.db、基准都是编的。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path

import pytest
from helpers import SHANGHAI, World, build_packet
from test_versions import Shifted
from test_versions_default import fake_probes, measured

from javmem import probes, versions
from javmem.cli import main
from javmem.llm import Usage
from javmem.patrol import patrol_report
from javmem.pipeline.store import ResultStore
from javmem.soft.judge import SoftStore

VERSIONS = ["served_model_changed", "probe_changed", "probe_stale"]
CHAT_DEFAULT_ID = "2026-11-08_2330_desk-bot"  # build_packet() 默认的包号
MODEL = "m-x@auto/json_schema"
_keys = count()


def add_result(data: Path, name: str, at: str, model: str = MODEL, times: int = 1) -> None:
    with ResultStore(data / "l3") as store:
        for _ in range(times):
            store.save_result(cache_key=f"k{next(_keys)}", window_sha="s", prompt_version="l3-v20", model=model, output="{}",
                              parsed={}, response_model=name, usage=Usage(1, 1), seconds=0.1, created_at=at)


def add_judgment(data: Path, name: str, at: str, model: str) -> None:
    row = {"judge_key": f"j{next(_keys)}", "tier": "llm", "prompt_version": "c1", "model": model, "probability": None,
           "answer": "是", "reason": "", "output": "{}", "response_model": name, "input_tokens": 1, "output_tokens": 1,
           "reasoning_tokens": 0, "cached_tokens": 0, "seconds": 0.1, "created_at": at}
    with ResultStore(data / "l3") as store:
        store.save_judgment(row, [])


def add_verdict(data: Path, name: str, at: str, model: str) -> None:
    SoftStore(data / "soft").close()  # 建好表
    conn = sqlite3.connect(data / "soft" / "soft.db")
    try:
        conn.execute("INSERT INTO verdicts (key, case_id, prompt_version, model, request_sha, request, output, parsed,"
                     " response_model, created_at) VALUES (?, 'c', 's1', ?, 'r', '', '{}', '{}', ?, ?)",
                     (f"v{next(_keys)}", model, name, at))
        conn.commit()
    finally:
        conn.close()


def save_baseline() -> None:
    assert main(["versions", "--no-probe", "--save"]) == 0


def kinds(world: World) -> list[str]:
    return [item.kind for item in patrol_report(world.settings).items]


def skipped(world: World) -> list[tuple[str | None, list[str]]]:
    return [(item.reason, list(item.skipped or ())) for item in patrol_report(world.settings).skipped]


def test_without_a_baseline_the_comparisons_are_skipped(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.intake()
    assert main(["patrol", "--json"]) == 0  # 不算要看的
    items = json.loads(capsys.readouterr().out)
    assert [(item["kind"], item["reason"], item["skipped"]) for item in items] == [
        ("checks_skipped", "no_versions_baseline", VERSIONS)]
    assert "javmem versions --save" in items[0]["message"] and "library" not in items[0]


def test_a_changed_served_name_is_reported(world: World) -> None:
    world.intake()
    add_result(world.data, "m-x-1101", "2026-11-19T20:00:00+08:00")
    save_baseline()
    assert kinds(world) == [] and skipped(world) == []
    add_result(world.data, "m-x-1201", "2026-11-19T20:30:00+08:00")
    [item] = patrol_report(world.settings).items
    assert item.kind == "served_model_changed" and item.library is None and item.ref is None
    assert "m-x-1101" in item.message and "m-x-1201" in item.message and "javmem versions --save" in item.message
    assert item.stats == {"model": MODEL, "baseline": "m-x-1101", "latest": "m-x-1201", "at": "2026-11-19T20:30:00+08:00",
                          "from": "l3.db:results"}
    save_baseline()  # 认了：重新存基准，就不报了
    assert kinds(world) == []


def test_the_latest_across_tables_and_soft_db(world: World) -> None:
    world.intake()
    add_result(world.data, "m-x-1101", "2026-11-19T20:00:00+08:00")
    add_judgment(world.data, "j-1101", "2026-11-19T20:00:00+08:00", "judge-y")
    add_verdict(world.data, "s-1101", "2026-11-19T20:00:00+08:00", "soft-z")
    save_baseline()
    add_judgment(world.data, "j-1201", "2026-11-19T21:00:00+08:00", "judge-y")
    add_verdict(world.data, "s-1201", "2026-11-19T21:00:00+08:00", "soft-z")
    add_result(world.data, "m-x-1101", "2026-11-19T21:00:00+08:00")  # 没变的不报
    found = {item.stats["model"]: item.stats for item in patrol_report(world.settings).items}  # type: ignore[index]
    assert sorted(found) == ["judge-y", "soft-z"]
    assert found["judge-y"]["from"] == "l3.db:judgments" and found["soft-z"]["from"] == "soft.db:verdicts"


def test_only_the_last_rows_of_each_table_are_read(world: World) -> None:
    """不扫表：每张表照写进去的先后倒着取最后几行。很早写进去的一行时刻写得再晚，也不在里面。"""
    world.intake()
    add_result(world.data, "m-x-1101", "2026-11-19T20:00:00+08:00")
    save_baseline()
    add_result(world.data, "m-x-old", "2026-11-19T23:00:00+08:00")
    add_result(world.data, "m-x-1101", "2026-11-19T21:00:00+08:00", times=200)
    assert kinds(world) == []
    add_result(world.data, "m-x-1201", "2026-11-19T21:00:00+08:00")  # 最后写进去的一行时刻一样：照写进去的先后算它最近
    assert kinds(world) == ["served_model_changed"]


def test_a_broken_baseline_is_reported_and_the_comparisons_skipped(world: World) -> None:
    world.intake()
    path = world.data / "logs" / versions.BASELINE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("{不是 JSON", '{"format": "something/9", "at": "2026-11-19T20:00:00+08:00"}', '["javmem-versions/1"]',
                 json.dumps({"format": versions.FORMAT, "at": "昨天"})):
        path.write_text(text, encoding="utf-8")
        report = patrol_report(world.settings)
        assert [item.kind for item in report.items] == ["versions_unreadable"], text
        assert "javmem versions --save" in report.items[0].message
        assert [(item.reason, list(item.skipped or ())) for item in report.skipped] == [("versions_unreadable", VERSIONS)]


def test_no_result_store_to_read_is_skipped_not_passed(world: World) -> None:
    world.intake()
    add_result(world.data, "m-x-1101", "2026-11-19T20:00:00+08:00")
    save_baseline()
    for name in ("l3.db", "l3.db-wal", "l3.db-shm"):
        (world.data / "l3" / name).unlink(missing_ok=True)
    assert kinds(world) == []
    assert skipped(world) == [("stores_unreadable", ["served_model_changed"])]


# ---------- 探针：巡检不跑，只读 javmem versions --check 记的那一笔 ----------


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def probe_kinds(world: World) -> list[str]:
    return [item.kind for item in patrol_report(world.settings).items if item.kind.startswith("probe_")]


def test_a_check_that_differed_is_reported_until_the_baseline_is_saved_again(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.intake()
    now = {"embedding": probes.embed_probe(Shifted(1e-2)), "qwen": {"error": "transport"}}
    fake_probes(monkeypatch, measured(), now, now)
    assert main(["versions", "--save"]) == 0
    assert probe_kinds(world) == []  # 刚存的：探针存的时候量过
    assert main(["versions", "--check", "--only", "probes,served,models"]) == 1
    [item] = [one for one in patrol_report(world.settings).items if one.kind == "probe_changed"]
    assert item.library is None and item.ref is None
    assert "2 处不一样" in item.message and "向量 1" in item.message and "本机 Qwen 1" in item.message
    assert "没量到的也算" in item.message
    saved = read_json(world.data / "logs" / versions.BASELINE_FILE)
    assert item.stats == {"checked_at": item.stats["checked_at"], "baseline_at": saved["at"], "different": 2,  # type: ignore[index]
                          "probes": {"embedding": 1, "qwen": 1}, "no_baseline": [], "unmeasured": [], "old_version": []}
    # 认了：照现在这样重新存基准（文件的指纹变了），上一回比的是旧的那一份，不算
    assert main(["versions", "--save"]) == 0
    assert probe_kinds(world) == []


def test_a_baseline_without_probes_is_reported_after_a_check(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.intake()
    fake_probes(monkeypatch, measured())
    assert main(["versions", "--no-probe", "--save"]) == 0
    assert main(["versions", "--check", "--only", "probes"]) == 0  # versions 只提一句
    [item] = [one for one in patrol_report(world.settings).items if one.kind == "probe_changed"]
    assert "基准里向量、本机 Qwen 没量到，比不了" in item.message and "javmem versions --save" in item.message
    assert item.stats["no_baseline"] == ["embedding", "qwen"] and item.stats["different"] == 0  # type: ignore[index]


def test_probes_not_checked_for_more_than_eight_days(world: World) -> None:
    world.intake()
    save_baseline()
    base_path = world.data / "logs" / versions.BASELINE_FILE
    saved = read_json(base_path)
    saved["at"] = "2026-11-01T09:00:00+08:00"
    write_json(base_path, saved)
    status_path = world.data / "logs" / versions.STATUS_FILE
    now = datetime(2026, 11, 9, 9, 0, tzinfo=SHANGHAI)
    assert probe_kinds(world.at(now)) == []  # 存基准正好 8 天：不报
    [item] = [one for one in patrol_report(world.at(now + timedelta(hours=1)).settings).items if one.kind == "probe_stale"]
    assert "存了基准（2026-11-01T09:00:00+08:00）以后 8 天没 --check 过探针" in item.message
    assert "javmem versions --check --only probes,served,models" in item.message
    assert item.stats == {"last_check_at": None, "baseline_at": "2026-11-01T09:00:00+08:00", "days": 8}

    digest = hashlib.sha256(base_path.read_bytes()).hexdigest()

    def checked(at: str, baseline_sha256: str = digest) -> None:
        probe = {"at": at, "baseline_at": saved["at"], "baseline_sha256": baseline_sha256, "different": 0,
                 "probes": {"embedding": 0, "qwen": 0}, "no_baseline": [], "unmeasured": []}
        write_json(status_path, {"format": versions.STATUS_FORMAT, "last": {}, "sections": {"probes": probe}})

    checked("2026-11-07T09:00:00+08:00")
    assert probe_kinds(world.at(now + timedelta(days=5))) == []
    [item] = [one for one in patrol_report(world.at(now + timedelta(days=7)).settings).items if one.kind == "probe_stale"]
    assert "版本和探针 9 天没查了（上一次 --check 比探针是 2026-11-07T09:00:00+08:00）" in item.message
    checked("2026-11-08T09:00:00+08:00", baseline_sha256="0" * 64)  # 比的是别的基准（指纹不一样）：不算
    assert probe_kinds(world.at(now + timedelta(days=1))) == ["probe_stale"]


def test_a_check_without_probes_does_not_reset_the_clock(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.intake()
    fake_probes(monkeypatch, measured())
    assert main(["versions", "--save"]) == 0
    assert main(["versions", "--no-probe", "--check", "--only", "rules,served"]) == 0
    status = read_json(world.data / "logs" / versions.STATUS_FILE)
    assert "probes" not in status["sections"]
    later = datetime.now(SHANGHAI) + timedelta(days=9)
    assert probe_kinds(world.at(later)) == ["probe_stale"]


def test_a_broken_status_file_is_reported(world: World) -> None:
    world.intake()
    save_baseline()
    status_path = world.data / "logs" / versions.STATUS_FILE
    for text in ("{坏了", json.dumps({"format": "something/9"})):
        status_path.write_text(text, encoding="utf-8")
        [item] = [one for one in patrol_report(world.settings).items if one.kind.startswith("probe_")]
        assert item.kind == "probe_stale" and "读不了" in item.message and "javmem versions --check" in item.message


# ---------- 模型地址：照这个进程的环境变量看，和抽取读的同一套 ----------


def test_an_unofficial_endpoint_is_reported_with_its_host_only(
        world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    world.intake()
    monkeypatch.setenv("JAVMEM_LLM_BASE_URL", "https://relay.example.com/v1/some-path")
    assert main(["patrol", "--json"]) == 1  # 要看的
    items = json.loads(capsys.readouterr().out)
    [item] = [one for one in items if one["kind"] == "endpoint_unofficial"]
    assert "L3 的模型地址不是官方的（relay.example.com）" in item["message"]
    assert item["stats"] == {"which": "l3", "host": "relay.example.com"} and "library" not in item and "ref" not in item
    assert "some-path" not in json.dumps(items, ensure_ascii=False)  # 只写主机名
    assert main(["patrol", "--ack-kind", "endpoint_unofficial"]) == 0  # 不能标
    assert "标了看过了 0 条" in capsys.readouterr().out


def test_soft_and_local_endpoints_and_the_defaults(world: World) -> None:
    world.intake()
    env = {"JAVMEM_SOFT_BASE_URL": "https://relay.example.org", "JAVMEM_LLM_BASE_URL": "http://127.0.0.1:9/v1"}
    found = {item.stats["which"]: item.stats["host"]  # type: ignore[index]
             for item in patrol_report(world.settings, env).items if item.kind == "endpoint_unofficial"}
    assert found == {"l3": "127.0.0.1", "soft": "relay.example.org"}
    assert patrol_report(world.settings, {}).items == []  # 没配的是官方的默认地址
    official = {"JAVMEM_LLM_BASE_URL": "https://api.openai.com/v1/", "JAVMEM_SOFT_BASE_URL": "https://api.anthropic.com"}
    assert patrol_report(world.settings, official).items == []


def test_broken_model_settings_are_skipped_not_passed(world: World) -> None:
    world.intake()
    report = patrol_report(world.settings, {"JAVMEM_LLM_BASE_URL": "https://[::1", "JAVMEM_SOFT_BASE_URL": "https://relay.example.org"})
    assert [item.kind for item in report.items] == ["endpoint_unofficial"]  # 软变化的照查
    last = report.skipped[-1]
    assert (last.reason, last.skipped) == ("model_settings_unreadable", ("endpoint_unofficial",))
    assert "L3 的模型地址写错了" in last.message and "[::1" not in last.message
    odd = patrol_report(world.settings, {"JAVMEM_LLM_BASE_URL": "ftp://x"}).items  # 认得出主机名的照比：不是官方的
    assert [(item.kind, item.stats) for item in odd] == [("endpoint_unofficial", {"which": "l3", "host": "x"})]


# ---------- 几版规则混着写：一本账一条 ----------


T1, T2 = "2026-11-01T09:00:00+08:00", "2026-11-19T22:00:00+08:00"


def add_rules(world: World, *rows: tuple[str, str, str, str]) -> None:
    for rule, version, layer, at in rows:
        world.execute("INSERT INTO rules (rule_id, version, layer, from_questions, description, created_at)"
                      " VALUES (?, ?, ?, '[]', '', ?)", rule, version, layer, at)


def mixed(world: World) -> list:
    return [item for item in patrol_report(world.settings).items if item.kind == "rules_mixed"]


def test_rules_written_by_two_versions_are_reported(world: World) -> None:
    world.intake()
    add_rules(world, ("G1", "l4-v6", "L4", T1), ("G2", "l4-v6", "L4", T1), ("C", "l4-v6", "L4", T1),
              ("C", "f1", "L4", T1), ("L5-merge", "l5-v11", "L5", T1),
              ("L5-lexicon", "aaaa111122223333", "L5", T1), ("L5-lexicon", "bbbb111122223333", "L5", T2))
    # 一版 L4；关系的 C 跟着 L4 的版本号，进图前检查另登记一行 C（提示词 f1）：本来就有两行；别名表的指纹是 lexicon_changed 管
    assert mixed(world) == []
    add_rules(world, ("G1", "l4-v7", "L4", T2), ("G2", "l4-v7", "L4", T2), ("C", "l4-v7", "L4", T2))
    [item] = mixed(world)
    assert item.library == "prod" and item.ref is None
    assert f"L4 是 l4-v6（从 {T1} 起）、l4-v7（从 {T2} 起）" in item.message and "整本重建" in item.message
    assert item.stats == {"rules": {"L4": ["l4-v6", "l4-v7"]}}
    add_rules(world, ("C", "f2", "L4", T2))
    [item] = mixed(world)
    assert item.stats == {"rules": {"L4": ["l4-v6", "l4-v7"], "进图前检查": ["f1", "f2"]}}
    assert [one.library for one in mixed(world)] == ["prod"]  # 一本账一条，测试库没写过


def test_packets_read_by_two_l1_versions_are_reported(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.drop("inbox/desk-bot", build_packet(stamp="2026-11-09_2330", created_at="2026-11-09T23:30:00+08:00",
                                              covers_from="2026-11-08T23:30:00+08:00", covers_to="2026-11-09T23:30:00+08:00"))
    world.intake()
    first, second = CHAT_DEFAULT_ID, "2026-11-09_2330_desk-bot"
    world.execute("INSERT INTO quote_roles (packet_id, revision, l1_version) VALUES (?, 1, 'l1-v11'), (?, 1, 'l1-v12'),"
                  " ('2026-01-01_0000_gone-bot', 1, 'l1-v10')", first, second)  # 包登记里没有的不算
    [item] = mixed(world)
    assert "包是两版 L1 认的（l1-v11 1 个、l1-v12 1 个）" in item.message
    assert item.stats == {"l1": {"l1-v11": 1, "l1-v12": 1}}
    world.execute("UPDATE packets SET withdrawn_at = ? WHERE packet_id = ?", T2, second)  # 撤回了的不算
    assert mixed(world) == []
    world.execute("UPDATE packets SET withdrawn_at = NULL WHERE packet_id = ?", second)
    world.execute("UPDATE quote_roles SET revision = 2 WHERE packet_id = ?", second)  # 记的是别的版本：这一版还没写，不算
    assert mixed(world) == []
