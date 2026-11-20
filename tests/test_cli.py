from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from javmem import __version__
from javmem.cli import main
from javmem.ledger import LEDGER_TABLES, connect, table_names


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_init_builds_directories_and_both_ledgers(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == 0

    for name in ("inbox", "inbox-test", "archive", "archive-test", "failed"):
        assert (data_dir / name).is_dir()
    for name in ("prod.db", "test.db"):
        conn = connect(data_dir / "ledger" / name)
        try:
            assert set(LEDGER_TABLES) <= set(table_names(conn))
        finally:
            conn.close()

    output = capsys.readouterr().out
    assert "prod" in output and "test" in output


def test_init_twice_is_fine(data_dir: Path) -> None:
    assert main(["init"]) == 0
    assert main(["init"]) == 0


def test_init_without_data_dir_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == 2
    assert "JAVMEM_DATA_DIR" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["init", "intake"])
def test_unusable_data_dir_is_an_error_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    not_a_directory = tmp_path / "a-file"
    not_a_directory.write_text("这是一个文件", encoding="utf-8")
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources:\n  demo-bot: {env: prod, channel: chat}\n", encoding="utf-8")
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(not_a_directory))
    monkeypatch.setenv("JAVMEM_SOURCES_FILE", str(sources))
    assert main([command]) == 2
    assert "错误" in capsys.readouterr().err


def test_module_entry_point_runs(data_dir: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "javmem", "init"],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "账本版本" in result.stdout
