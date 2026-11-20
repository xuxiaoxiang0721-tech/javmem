from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

import pytest

from javmem.config import ConfigError, Settings, parse_timezone


def test_reads_the_four_documented_names(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "JAVMEM_DATA_DIR": str(tmp_path / "data"),
            "JAVMEM_SOURCES_FILE": str(tmp_path / "sources.yaml"),
            "JAVMEM_ACCESS_FILE": str(tmp_path / "access.yaml"),
            "JAVMEM_CALLER": "desk-bot",
        }
    )
    assert settings.data_dir == (tmp_path / "data").resolve()
    assert settings.sources_file == (tmp_path / "sources.yaml").resolve()
    assert settings.access_file == (tmp_path / "access.yaml").resolve()
    assert settings.caller == "desk-bot"


def test_nothing_is_set_by_default() -> None:
    settings = Settings.from_env({})
    assert settings.data_dir is None
    assert settings.sources_file is None
    assert settings.access_file is None
    assert settings.caller is None
    assert settings.stale_hours == 26
    with pytest.raises(ConfigError, match="JAVMEM_DATA_DIR"):
        settings.require_data_dir()


def test_prod_and_test_never_share_a_path(tmp_path: Path) -> None:
    settings = Settings.from_env({"JAVMEM_DATA_DIR": str(tmp_path)})
    prod, test = settings.library("prod"), settings.library("test")
    assert prod.db_path != test.db_path
    assert prod.inbox == tmp_path.resolve() / "inbox"
    assert test.inbox == tmp_path.resolve() / "inbox-test"
    assert prod.archive == tmp_path.resolve() / "archive"
    assert test.archive == tmp_path.resolve() / "archive-test"
    with pytest.raises(ValueError):
        settings.library("staging")


@pytest.mark.parametrize(
    ("text", "offset"),
    [
        ("+08:00", timedelta(hours=8)),
        ("-05:30", -timedelta(hours=5, minutes=30)),
        ("UTC", timedelta(0)),
    ],
)
def test_timezone_is_a_fixed_offset(text: str, offset: timedelta) -> None:
    assert parse_timezone(text) == timezone(offset)


@pytest.mark.parametrize("text", ["Asia/Shanghai", "8", "+25:00", "+08:99"])
def test_bad_timezone_is_refused(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_timezone(text)


@pytest.mark.parametrize("text", ["soon", "0", "-1", "nan", "inf", "1e400", "1e30"])
def test_bad_stale_hours_is_refused(text: str) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"JAVMEM_STALE_HOURS": text})
