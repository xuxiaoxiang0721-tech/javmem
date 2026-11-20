"""测试的公共部分。样例都是编的，数据都放临时目录，不碰任何真实目录。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from helpers import ACCESS_YAML, SOURCES_YAML, World

from javmem.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试都从干净的环境开始，机器上的 JAVMEM_* 影响不到测试，测试也碰不到真实的库。"""
    for name in list(os.environ):
        if name.startswith("JAVMEM_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("JAVMEM_TIMEZONE", "+08:00")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(path))
    return path


@pytest.fixture
def world(tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """数据目录加文档里的来源登记表和身份配置。"""
    sources = tmp_path / "sources.yaml"
    sources.write_text(SOURCES_YAML, encoding="utf-8")
    access = tmp_path / "access.yaml"
    access.write_text(ACCESS_YAML, encoding="utf-8")
    monkeypatch.setenv("JAVMEM_SOURCES_FILE", str(sources))
    monkeypatch.setenv("JAVMEM_ACCESS_FILE", str(access))
    return World(data_dir.resolve(), Settings.from_env())
