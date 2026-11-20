"""`javmem soft` 命令行（J4.5）：假模型从头跑一遍 judge、score；--dry-run 不要密钥；输出不能放仓库里；真调没密钥报配置错误。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import REPO_ROOT

from javmem.cli import main
from javmem.soft.cases import Case, Line, OldItem, dump_cases
from javmem.soft.judge import PROMPT_VERSION
from javmem.soft.score import ANSWERS_FORMAT


def write_cases(path: Path) -> Path:
    cases = [
        Case("Q01", (OldItem("O1", "relation", "林工 负责 东湖那批订单", "生效"),),
             (Line("T1", "2026-11-07T10:30:00+08:00", "我", "owner", "东湖那批先停一下，等客户回复。"),), ("T1",), split="dev"),
        Case("Q02", (OldItem("O1", "fact", "南港那张合同在走签字流程"),),
             (Line("T1", "2026-11-04T17:20:00+08:00", "小严", "other", "南港那张合同是不是取消了？"),), ("T1",), split="test"),
    ]
    dump_cases(cases, path)
    return path


def test_fake_judge_then_score(data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cases = write_cases(tmp_path / "cases.json")
    verdicts = tmp_path / "out" / "verdicts.json"
    assert main(["soft", "judge", "--cases", str(cases), "--out", str(verdicts), "--fake"]) == 0
    data = json.loads(verdicts.read_text(encoding="utf-8"))
    assert data["format"] == "javmem-soft-verdicts/1" and data["prompt"] == PROMPT_VERSION
    assert [record["status"] for record in data["cases"]] == ["done", "done"]
    assert (data_dir / "soft" / "soft.db").exists()
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"format": ANSWERS_FORMAT, "answers": {
        "Q01": {"O1": {"verdict": "B", "review": False}}, "Q02": {"O1": {"verdict": "A", "review": False}}}}), encoding="utf-8")
    capsys.readouterr()
    assert main(["soft", "score", "--cases", str(cases), "--answers", str(answers), "--verdicts", str(verdicts)]) == 0
    printed = capsys.readouterr().out
    assert "[total] 2 项" in printed and "[split:test] 1 项" in printed
    # 只判一份、再判一遍
    assert main(["soft", "judge", "--cases", str(cases), "--fake", "--split", "test", "--repeat", "1"]) == 0


def test_dry_run_needs_no_key(data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cases = write_cases(tmp_path / "cases.json")
    assert main(["soft", "judge", "--cases", str(cases), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "要调的 2" in printed and "claude-sonnet-5-5@high" in printed


def test_a_real_run_without_the_key_is_a_config_error(data_dir: Path, tmp_path: Path) -> None:
    cases = write_cases(tmp_path / "cases.json")
    assert main(["soft", "judge", "--cases", str(cases)]) == 2


def test_outputs_inside_the_repo_are_refused(data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cases = write_cases(tmp_path / "cases.json")
    inside = REPO_ROOT / "tests" / "should-not-exist.json"
    assert main(["soft", "judge", "--cases", str(cases), "--out", str(inside), "--fake"]) == 2
    assert not inside.exists() and "仓库" in capsys.readouterr().out


def test_unknown_case_and_bad_files(data_dir: Path, tmp_path: Path) -> None:
    cases = write_cases(tmp_path / "cases.json")
    assert main(["soft", "judge", "--cases", str(cases), "--fake", "--case", "Q99"]) == 2
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert main(["soft", "judge", "--cases", str(broken), "--fake"]) == 2


def test_a_missing_sdk_is_a_config_error_before_anything_runs(data_dir: Path, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    """没装 SDK：开跑之前就报配置错误（退出码 2，告诉怎么装），不是每道题各记一次失败。"""
    import sys

    cases = write_cases(tmp_path / "cases.json")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic 会报 ImportError
    monkeypatch.setenv("JAVMEM_SOFT_API_KEY", "sk-test-not-a-real-key-0000")
    assert main(["soft", "judge", "--cases", str(cases)]) == 2
    printed = capsys.readouterr()
    assert "pip install" in printed.out + printed.err and "sk-test" not in printed.out + printed.err


def test_a_store_inside_the_repo_is_refused(data_dir: Path, tmp_path: Path) -> None:
    cases = write_cases(tmp_path / "cases.json")
    inside = REPO_ROOT / "tests" / "soft-store-should-not-exist"
    assert main(["soft", "judge", "--cases", str(cases), "--fake", "--store", str(inside)]) == 2
    assert not inside.exists()


@pytest.mark.parametrize("command", [["judge", "--fake", "--limit", "0"], ["judge", "--fake", "--limit", "-1"],
                                     ["find", "--limit", "0"]])
def test_limits_must_be_positive(command: list[str], data_dir: Path, tmp_path: Path) -> None:
    from javmem.config import Settings
    from javmem.ledger import open_ledger

    open_ledger(Settings.from_env().library("prod")).close()  # 一本空账本：找得到才算数，不是因为文件不在
    cases = write_cases(tmp_path / "cases.json")
    where = ["--cases", str(cases)] if command[0] == "judge" else ["--library", "prod", "--out", str(tmp_path / "found.json")]
    assert main(["soft", command[0], *where, *command[1:]]) == 2


def test_find_writes_cases_from_a_ledger(world, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from test_soft_find import build

    build(world).close()
    out = tmp_path / "found.json"
    assert main(["soft", "find", "--library", "prod", "--out", str(out)]) == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))["cases"]) == 2
    ledger = world.settings.library("prod").db_path
    assert main(["soft", "find", "--ledger", str(ledger), "--out", str(tmp_path / "again.json")]) == 0
