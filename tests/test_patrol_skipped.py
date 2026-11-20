"""巡检这一回没看的几种（巡检要 javmem 改的第 7 样）：javmem patrol --json 照旧是一串提醒，没看的每个原因多一条
checks_skipped（reason、skipped）。它不算要看的：退出码、“几件要看”都不算它，run_patrol 也不给。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import World

from javmem.cli import main
from javmem.intake import intake_lock
from javmem.patrol import run_patrol

SETTLED = ["failed_without_note", "archive_missing", "archive_orphan", "registration_stuck", "inbox_stuck"]
INBOX = ["inbox_stray", "inbox_unknown_dir", "inbox_stuck"]
# 测试的数据目录没存过版本的基准（javmem versions --save）：和基准比的那几样每回都没看，这一条跟在最后
NO_BASELINE = ("no_versions_baseline", ["served_model_changed", "probe_changed", "probe_stale"])


def patrol_json(capsys: pytest.CaptureFixture[str]) -> tuple[int, list[dict]]:
    code = main(["patrol", "--json"])
    return code, json.loads(capsys.readouterr().out)


def skipped_of(items: list[dict]) -> list[tuple[str, list[str]]]:
    return [(item["reason"], item["skipped"]) for item in items if item["kind"] == "checks_skipped"]


def test_json_says_what_was_skipped_while_an_intake_is_running(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.intake()
    with intake_lock(world.data):
        code, items = patrol_json(capsys)
    assert code == 0  # 只有没看的：不算要看的
    assert skipped_of(items) == [("intake_busy", SETTLED), NO_BASELINE]
    assert [item["kind"] for item in items] == ["checks_skipped", "checks_skipped"]
    assert "library" not in items[0] and "ref" not in items[0]  # 每个库都没看；不能 --ack
    assert "收件锁被占着" in items[0]["message"]  # 撤回、合并这类也拿收件锁，不光是收件（审阅第 6 条）
    code, items = patrol_json(capsys)
    assert code == 0 and skipped_of(items) == [NO_BASELINE]  # 放开以后对账那几样都看了，没有这一条


def test_text_output_says_it_and_does_not_count_it(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.intake()
    with intake_lock(world.data):
        assert main(["patrol"]) == 0
    out = capsys.readouterr().out
    assert "checks_skipped：收件锁被占着" in out and "这几样这回没看" in out and "巡检：没有要看的" in out


def test_a_broken_registry_skips_the_inbox_checks(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.intake()
    world.settings.sources_file.write_text("[不合法", encoding="utf-8")
    code, items = patrol_json(capsys)
    assert code == 1 and [item["kind"] for item in items] == ["sources_unreadable", "checks_skipped", "checks_skipped"]
    assert skipped_of(items) == [("sources_unreadable", INBOX), NO_BASELINE]  # 没看的跟在后面


def test_a_broken_lexicon_skips_the_lexicon_check(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world.intake()
    broken = tmp_path / "lexicon-broken.yaml"
    broken.write_text("[不合法", encoding="utf-8")
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(broken))
    code, items = patrol_json(capsys)
    assert code == 1 and [item["kind"] for item in items] == ["lexicon_unreadable", "checks_skipped", "checks_skipped"]
    assert skipped_of(items) == [("lexicon_unreadable", ["lexicon_changed"]), NO_BASELINE]


def test_two_reasons_at_once_and_run_patrol_leaves_them_out(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.intake()
    world.settings.sources_file.write_text("[不合法", encoding="utf-8")
    with intake_lock(world.data):
        code, items = patrol_json(capsys)
        assert [item.kind for item in run_patrol(world.settings)] == ["sources_unreadable"]  # 只给要看的
    assert code == 1
    assert skipped_of(items) == [("sources_unreadable", INBOX), ("intake_busy", SETTLED), NO_BASELINE]
