"""来源登记表（packet-spec 第 11 节）。写在配置里，代码不写死任何 Bot 的名字。

登记表写错了整个收件都停下，不搬任何文件。不然一个错字就会把所有包都当成来路不明的扔进 failed。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from javmem.config import ENV_SOURCES_FILE, ConfigError
from javmem.yamlio import load_yaml

DEFAULT_DOMAINS = ("business", "finance", "investment", "research", "dev", "personal")
RESERVED_TAG = "private"
SOURCE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)


@dataclass(frozen=True)
class Source:
    id: str
    env: str
    channel: str
    mailbox: str | None
    framework: str | None
    note: str | None
    default_domain: tuple[str, ...]
    may_carry: frozenset[str]


@dataclass(frozen=True)
class Registry:
    sources: Mapping[str, Source]
    domains: frozenset[str]


def _string_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{where} 要写成文本列表")
    return tuple(value)


def _optional_text(value: object, where: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ConfigError(f"{where} 要写成文本")


def _parse_source(source_id: object, fields: object, domains: frozenset[str]) -> Source:
    if not isinstance(source_id, str) or SOURCE_ID.fullmatch(source_id) is None:
        raise ConfigError("来源编号只能有小写英文、数字和连字符")
    where = f"来源 {source_id}"
    if not isinstance(fields, Mapping):
        raise ConfigError(f"{where} 的写法不对")

    env = fields.get("env")
    if env not in ("prod", "test"):
        raise ConfigError(f"{where} 的 env 要写 prod 或 test")
    channel = fields.get("channel")
    if channel not in ("chat", "email"):
        raise ConfigError(f"{where} 的 channel 要写 chat 或 email")

    mailbox = _optional_text(fields.get("mailbox"), f"{where} 的 mailbox")
    if channel == "email" and not mailbox:
        raise ConfigError(f"{where} 是邮件来源，要写 mailbox")
    if channel == "chat" and mailbox is not None:
        raise ConfigError(f"{where} 是对话来源，不写 mailbox")

    default_domain = _string_list(fields.get("default_domain", []), f"{where} 的 default_domain")
    unknown = set(default_domain) - domains
    if unknown:
        raise ConfigError(f"{where} 的 default_domain 不在 domains 里")

    may_carry = frozenset(_string_list(fields.get("may_carry", []), f"{where} 的 may_carry"))
    if RESERVED_TAG in may_carry:
        raise ConfigError(f"{where} 的 may_carry 不能写保留字 {RESERVED_TAG}")

    return Source(
        id=source_id,
        env=env,
        channel=channel,
        mailbox=mailbox,
        framework=_optional_text(fields.get("framework"), f"{where} 的 framework"),
        note=_optional_text(fields.get("note"), f"{where} 的 note"),
        default_domain=default_domain,
        may_carry=may_carry,
    )


def load_registry(path: Path | None) -> Registry:
    if path is None:
        raise ConfigError(f"没有设置 {ENV_SOURCES_FILE}")
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise ConfigError("来源登记表读不了") from None
    except (yaml.YAMLError, RecursionError):
        raise ConfigError("来源登记表不是合法的 YAML") from None

    if not isinstance(data, Mapping):
        raise ConfigError("来源登记表的写法不对")
    domains = frozenset(_string_list(data.get("domains", list(DEFAULT_DOMAINS)), "domains"))
    if not domains:
        raise ConfigError("domains 不能是空的")
    entries = data.get("sources")
    if not isinstance(entries, Mapping) or not entries:
        raise ConfigError("来源登记表里一个来源都没有")
    sources = {source_id: _parse_source(source_id, fields, domains) for source_id, fields in entries.items()}
    return Registry(sources=sources, domains=domains)
