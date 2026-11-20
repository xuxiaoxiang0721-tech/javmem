"""身份配置：配置出错一律拒绝，不按默认值放行。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from helpers import ACCESS_YAML, World

from javmem.access import UnknownIdentity, load_identity


def identity_for(world: World, caller: str | None, text: str | None = None):
    if text is not None:
        world.settings.access_file.write_text(text, encoding="utf-8")
    return load_identity(replace(world.settings, caller=caller))


def test_spec_identities_load(world: World) -> None:
    desk = identity_for(world, "desk-bot")
    assert desk.env == "prod"
    assert desk.partitions == {"business", "research", "dev"}
    assert desk.sensitive == frozenset()

    books = identity_for(world, "books-bot")
    assert books.partitions == {"business", "finance", "investment"}
    assert books.sensitive == {"payroll"}

    assert identity_for(world, "trial-bot").env == "test"

    owner = identity_for(world, "owner-console")
    assert owner.partitions is None
    assert owner.sensitive is None


def test_no_caller_is_refused(world: World) -> None:
    with pytest.raises(UnknownIdentity):
        identity_for(world, None)


def test_caller_not_in_the_config_is_refused(world: World) -> None:
    with pytest.raises(UnknownIdentity):
        identity_for(world, "no-such-bot")


def test_no_config_file_is_refused(world: World) -> None:
    with pytest.raises(UnknownIdentity):
        load_identity(replace(world.settings, caller="desk-bot", access_file=None))


def test_missing_config_file_is_refused(world: World, tmp_path: Path) -> None:
    with pytest.raises(UnknownIdentity):
        load_identity(replace(world.settings, caller="desk-bot", access_file=tmp_path / "missing.yaml"))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[不合法",
        "- 列表\n",
        "identities: []\n",
        "identities:\n  desk-bot: 不是字典\n",
        "identities:\n  desk-bot: {partitions: [business], sensitive: []}\n",  # 缺 env
        "identities:\n  desk-bot: {env: prod, sensitive: []}\n",  # 缺 partitions
        "identities:\n  desk-bot: {env: prod, partitions: [business]}\n",  # 缺 sensitive
        "identities:\n  desk-bot: {env: staging, partitions: [business], sensitive: []}\n",
        "identities:\n  desk-bot: {env: [prod], partitions: [business], sensitive: []}\n",
        "identities:\n  desk-bot: {env: prod, partitions: business, sensitive: []}\n",
        "identities:\n  desk-bot: {env: prod, partitions: null, sensitive: []}\n",
        "identities:\n  desk-bot: {env: prod, partitions: ['*'], sensitive: []}\n",
        "identities:\n  desk-bot: {env: prod, partitions: [business], sensitive: payroll}\n",
        "identities:\n  desk-bot: {env: prod, partitions: [1], sensitive: []}\n",
    ],
)
def test_bad_config_is_refused_not_defaulted(world: World, text: str) -> None:
    with pytest.raises(UnknownIdentity):
        identity_for(world, "desk-bot", text)


def test_one_bad_identity_does_not_stop_the_others(world: World) -> None:
    text = "identities:\n  good: {env: prod, partitions: [business], sensitive: []}\n  bad: {env: prod}\n"
    assert identity_for(world, "good", text).partitions == {"business"}
    with pytest.raises(UnknownIdentity):
        identity_for(world, "bad", text)


def test_changes_take_effect_on_the_next_call(world: World) -> None:
    assert identity_for(world, "desk-bot").sensitive == frozenset()
    changed = ACCESS_YAML.replace("sensitive: []", "sensitive: [payroll]", 1)
    assert identity_for(world, "desk-bot", changed).sensitive == {"payroll"}
