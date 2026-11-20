"""巡检报模型、版本变了：独立审阅（只读，11-20）查出来的那几处，都先写成测试看它红。都不连网、不调模型。

- 服务商给的名字只比存基准以后写进去的：基准里最近的名字可能来自 token 账（别处的调用也记在这本账上），同一秒里也可能
  有两个名字，拿结果库最后几行整个比会白报，而且重新存基准也消不掉（审阅第 1 条）。
- 模型地址只读那一个变量：别的设置写错了（代理地址写坏了会抛 ValueError）不能让巡检、备份崩，也不能把地址的检查一起带没了
  （第 2、5 条）。恢复核对不比 endpoint_unofficial：看的是跑的那个进程的环境变量（第 3 条）。
- 记的那一笔里几样写得不对的不崩（第 4 条）；soft.db 读不了、l3.db 读得了的也说一声（第 6 条）；基准先写到旁边再换上去
  （第 7 条）；照基准的内容认是哪一份，两边都没量到的探针（这次出错）也说一声（第 8 条）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from helpers import World
from test_backup import backup, restore, stocked  # noqa: F401 - stocked 是 fixture
from test_patrol_versions import MODEL, add_result, add_verdict, read_json, save_baseline, write_json
from test_versions_default import fake_probes

from javmem import probes, versions
from javmem.cli import main
from javmem.patrol import patrol_report
from javmem.pipeline.reljudge import FakeLocal
from javmem.usage import TokenLedger


def served(world: World) -> list:
    return [item for item in patrol_report(world.settings).items if item.kind == "served_model_changed"]


# ---------- 第 1 条 ----------


def test_a_newer_name_only_in_the_token_ledger_is_not_a_change(world: World) -> None:
    world.intake()
    add_result(world.data, "m-x-1101", "2026-11-19T20:00:00+08:00")
    ledger = world.data / "logs" / "token-ledger.csv"  # 数据目录默认的那本账：别处的调用也记在这里
    TokenLedger(ledger).append({"at": "2026-11-19T21:00:00+08:00", "model": MODEL, "response_model": "m-x-1201"})
    save_baseline()
    assert served(world) == []  # 存完马上看：不报
    add_result(world.data, "m-x-1301", "2026-11-19T22:00:00+08:00")  # 存完以后结果库里来了别的名字：报
    [item] = served(world)
    assert (item.stats["baseline"], item.stats["latest"]) == ("m-x-1201", "m-x-1301")  # type: ignore[index]
    save_baseline()  # 认了：重新存基准，就不报
    assert served(world) == []


def test_two_names_in_the_same_second_as_the_baseline_are_not_a_change(world: World) -> None:
    world.intake()
    add_result(world.data, "m-x-1102", "2026-11-19T20:00:00+08:00")
    add_result(world.data, "m-x-1101", "2026-11-19T20:00:00+08:00")  # 同一秒，后写进去的名字排在前面
    save_baseline()
    assert served(world) == []
    add_result(world.data, "m-x-1101", "2026-11-19T20:00:00+08:00")  # 还是那一秒：不比
    assert served(world) == []


# ---------- 第 2、5 条 ----------


def test_other_odd_model_settings_neither_crash_nor_hide_the_address(
        world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    world.intake()
    monkeypatch.setenv("JAVMEM_LLM_PROXY_URL", "http://[::1")  # 代理地址写坏了：读设置会抛 ValueError
    monkeypatch.setenv("JAVMEM_LLM_SERVICE_TIER", "bogus")
    monkeypatch.setenv("JAVMEM_LLM_BASE_URL", "https://relay.example.com/v1")
    assert main(["patrol", "--json"]) == 1
    items = json.loads(capsys.readouterr().out)
    assert [item["stats"]["host"] for item in items if item["kind"] == "endpoint_unofficial"] == ["relay.example.com"]
    assert [item.get("reason") for item in items if item["kind"] == "checks_skipped"] == ["no_versions_baseline"]


def test_an_address_without_a_host_is_skipped_not_crashed(world: World) -> None:
    world.intake()
    report = patrol_report(world.settings, {"JAVMEM_LLM_BASE_URL": "http://[::1", "JAVMEM_SOFT_BASE_URL": "不是地址"})
    assert [item.kind for item in report.items] == []
    last = report.skipped[-1]
    assert (last.reason, last.skipped) == ("model_settings_unreadable", ("endpoint_unofficial",))
    assert "L3、软变化" in last.message and "[::1" not in last.message


def test_backup_survives_odd_model_settings(stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("JAVMEM_LLM_PROXY_URL", "http://[::1")
    assert backup(tmp_path / "staging") == 0  # 备份要跑一遍巡检（恢复时拿来比）：不崩
    assert (stocked.data / "logs" / "backup-status.json").exists()


# ---------- 第 3 条 ----------


def test_restore_check_does_not_compare_the_endpoint(stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    staging, target = tmp_path / "staging", tmp_path / "restored"
    monkeypatch.setenv("JAVMEM_LLM_BASE_URL", "https://relay.example.com/v1")
    assert backup(staging) == 0
    monkeypatch.delenv("JAVMEM_LLM_BASE_URL")  # 恢复核对在另一个环境里跑
    assert restore(staging, target) == 0
    report = json.loads((target / "restore-check.json").read_text(encoding="utf-8"))
    assert report["ok"] and report["patrol"]["same"]


def test_a_real_write_has_no_mixed_rules(stocked: World) -> None:  # noqa: F811
    """真写过账的（照现在的代码写一遍）：每层一版，不报 rules_mixed。"""
    assert [item for item in patrol_report(stocked.settings).items if item.kind == "rules_mixed"] == []


# ---------- 第 4 条 ----------


@pytest.mark.parametrize("odd", [5, [["x"]], "embedding", [None], {"embedding": 1}])
def test_odd_fields_in_the_status_do_not_crash(world: World, odd: object) -> None:
    world.intake()
    save_baseline()
    base = world.data / "logs" / versions.BASELINE_FILE
    saved = read_json(base)
    probe = {"at": saved["at"], "baseline_at": saved["at"], "baseline_sha256": hashlib.sha256(base.read_bytes()).hexdigest(),
             "different": "x", "probes": {"embedding": "1", "qwen": [1]}, "no_baseline": odd, "unmeasured": odd}
    write_json(world.data / "logs" / versions.STATUS_FILE, {"format": versions.STATUS_FORMAT, "sections": {"probes": probe}})
    assert [item.kind for item in patrol_report(world.settings).items if item.kind.startswith("probe_")] == []


# ---------- 第 6 条 ----------


def test_an_unreadable_soft_db_next_to_a_good_l3_db_is_said(world: World) -> None:
    world.intake()
    add_result(world.data, "m-x-1101", "2026-11-19T20:00:00+08:00")
    add_verdict(world.data, "s-1101", "2026-11-19T20:00:00+08:00", "soft-z")
    save_baseline()
    for name in ("soft.db-wal", "soft.db-shm"):
        (world.data / "soft" / name).unlink(missing_ok=True)
    (world.data / "soft" / "soft.db").write_bytes(b"not a database, " * 100)
    [skip] = [item for item in patrol_report(world.settings).skipped if item.reason == "stores_unreadable"]
    assert skip.skipped == ("served_model_changed",) and "soft.db" in skip.message
    add_result(world.data, "m-x-1201", "2026-11-19T21:00:00+08:00")  # 读得了的那个照看
    assert [item.stats["model"] for item in served(world)] == [MODEL]  # type: ignore[index]


# ---------- 第 7 条 ----------


def test_the_baseline_is_written_beside_and_then_swapped_in(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    swaps: list[tuple[str, str]] = []
    real = os.replace

    def replace(source: str | Path, target: str | Path) -> None:
        swaps.append((Path(source).name, Path(target).name))
        real(source, target)

    monkeypatch.setattr(versions.os, "replace", replace)
    assert main(["versions", "--no-probe", "--save"]) == 0
    assert swaps == [(versions.BASELINE_FILE + ".part", versions.BASELINE_FILE)]
    assert sorted(path.name for path in (data_dir / "logs").iterdir()) == [versions.BASELINE_FILE]


def test_a_baseline_that_cannot_be_written_says_so(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (data_dir / "logs" / versions.BASELINE_FILE).mkdir(parents=True)  # 换不上去
    assert main(["versions", "--no-probe", "--save"]) == 2
    assert "没存上" in capsys.readouterr().out
    assert sorted(path.name for path in (data_dir / "logs").iterdir()) == [versions.BASELINE_FILE]  # 旁边那份删了


# ---------- 第 8 条 ----------


def test_the_baseline_is_known_by_its_content(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """照基准文件的内容认是哪一份：同一秒里重新存了一份不一样的，上一回比的照样不算。"""
    world.intake()
    same = {"embedding": probes.embed_probe(FakeLocal_embed()), "qwen": probes.qwen_probe(FakeLocal([0.9] * 20))}
    moved = {"embedding": same["embedding"], "qwen": probes.qwen_probe(FakeLocal([0.9] * 18 + [0.3] * 2))}
    fake_probes(monkeypatch, same, moved, moved)
    assert main(["versions", "--save"]) == 0
    assert main(["versions", "--check", "--only", "probes"]) == 1
    assert [item.kind for item in patrol_report(world.settings).items] == ["probe_changed"]
    assert main(["versions", "--save"]) == 0  # 存的是现在这样（认了），多半和上一份在同一秒里
    assert [item.kind for item in patrol_report(world.settings).items] == []


def test_probes_missing_on_both_sides_this_time_by_error_are_said(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.intake()
    down = {"embedding": {"error": "transport"}, "qwen": probes.qwen_probe(FakeLocal([0.9] * 20))}
    fake_probes(monkeypatch, down, down)
    assert main(["versions", "--save"]) == 0  # 存的时候向量服务就没开
    assert main(["versions", "--check", "--only", "probes"]) == 0  # versions 自己照旧：两边都没量到的不比
    [item] = [one for one in patrol_report(world.settings).items if one.kind == "probe_changed"]
    assert "向量 两边都没量到（这次出错）" in item.message
    assert item.stats["unmeasured"] == ["embedding"] and item.stats["different"] == 0  # type: ignore[index]
    status = read_json(world.data / "logs" / versions.STATUS_FILE)
    assert status["sections"]["probes"]["unmeasured"] == ["embedding"]


def FakeLocal_embed():  # noqa: N802 - 和 FakeLocal 放一起好认
    from javmem.embedding import FakeEmbeddings

    return FakeEmbeddings()
