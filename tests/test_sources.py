"""来源登记表写错了要报错，不能悄悄按默认值放行。"""

from __future__ import annotations

from pathlib import Path

import pytest

from javmem.config import ConfigError
from javmem.sources import load_registry


def load(tmp_path: Path, text: str):
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return load_registry(path)


GOOD = "sources:\n  demo-bot: {env: prod, channel: chat}\n"


def test_minimal_registry(tmp_path: Path) -> None:
    registry = load(tmp_path, GOOD)
    source = registry.sources["demo-bot"]
    assert (source.env, source.channel, source.mailbox) == ("prod", "chat", None)
    assert source.may_carry == frozenset()
    assert registry.domains == {"business", "finance", "investment", "research", "dev", "personal"}


def test_domains_can_be_extended(tmp_path: Path) -> None:
    registry = load(tmp_path, "domains: [business, gardening]\n" + GOOD)
    assert registry.domains == {"business", "gardening"}


def test_no_file_configured() -> None:
    with pytest.raises(ConfigError, match="JAVMEM_SOURCES_FILE"):
        load_registry(None)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_registry(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[不合法",
        "- 列表\n",
        "sources: []\n",
        "sources: {}\n",
        "sources:\n  Demo_Bot: {env: prod, channel: chat}\n",
        "sources:\n  demo-bot: 不是字典\n",
        "sources:\n  demo-bot: {channel: chat}\n",
        "sources:\n  demo-bot: {env: staging, channel: chat}\n",
        "sources:\n  demo-bot: {env: prod}\n",
        "sources:\n  demo-bot: {env: prod, channel: fax}\n",
        "sources:\n  demo-bot: {env: prod, channel: email}\n",
        "sources:\n  demo-bot: {env: prod, channel: chat, mailbox: a@example.com}\n",
        "sources:\n  demo-bot: {env: prod, channel: chat, may_carry: payroll}\n",
        "sources:\n  demo-bot: {env: prod, channel: chat, may_carry: [private]}\n",
        "sources:\n  demo-bot: {env: prod, channel: chat, default_domain: [gardening]}\n",
        "domains: []\n" + GOOD,
        "domains: business\n" + GOOD,
        "sources:\n  demo-bot: {env: prod, channel: chat}\n  demo-bot: {env: test, channel: chat}\n",
    ],
)
def test_bad_registry_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, text)
