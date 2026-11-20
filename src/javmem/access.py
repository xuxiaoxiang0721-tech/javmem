"""谁能看什么（query-api 第 2、3 节）。

身份跟着连接走，不是工具参数。谁能看什么写在配置里，每次调用都读最新的，改完马上生效。
配置读不了，或者这个身份缺字段，一律拒绝，不按默认值放行。

分区和敏感标记的算法不一样：分区有一个能看就过，敏感标记每个都要能看。
过滤要写进检索的 SQL 里，不能先取前 N 条再滤，所以这里给的是 SQL 片段。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import yaml

from javmem.config import LIBRARY_NAMES, Settings
from javmem.yamlio import load_yaml

ALL = "*"


class UnknownIdentity(Exception):
    """没有身份，身份不在配置里或缺字段，或者配置读不了。不说具体是哪种。"""


@dataclass(frozen=True)
class Identity:
    name: str
    env: str
    partitions: frozenset[str] | None  # None 是全部
    sensitive: frozenset[str] | None  # None 是全部，包括保留标记 private

    def forbidden_partitions(self, requested: Iterable[str]) -> list[str]:
        """明着要的分区里，身份不能看的。"""
        if self.partitions is None:
            return []
        return [name for name in requested if name not in self.partitions]


def _scope(value: object) -> frozenset[str] | None:
    """字段的取值：'*' 或者文本列表。别的都算写错。"""
    if value == ALL:
        return None
    if isinstance(value, list) and all(isinstance(item, str) and item and item != ALL for item in value):
        return frozenset(value)
    raise UnknownIdentity


def load_identity(settings: Settings) -> Identity:
    caller = settings.caller
    if caller is None or settings.access_file is None:
        raise UnknownIdentity
    try:
        data = load_yaml(settings.access_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
        raise UnknownIdentity from None

    identities = data.get("identities") if isinstance(data, Mapping) else None
    fields = identities.get(caller) if isinstance(identities, Mapping) else None
    if not isinstance(fields, Mapping):
        raise UnknownIdentity
    env = fields.get("env")
    if not isinstance(env, str) or env not in LIBRARY_NAMES:
        raise UnknownIdentity
    if "partitions" not in fields or "sensitive" not in fields:
        raise UnknownIdentity
    return Identity(caller, env, _scope(fields["partitions"]), _scope(fields["sensitive"]))


def _placeholders(values: Iterable[object]) -> str:
    return ", ".join("?" for _ in values)


def has_any_in(column: str, names: Iterable[str]) -> tuple[str, list[str]]:
    """column（JSON 列表）和 names 有交集。"""
    names = sorted(names)
    return (
        f"EXISTS (SELECT 1 FROM json_each({column}) WHERE json_each.value IN ({_placeholders(names)}))",
        names,
    )


def has_any_partition(names: Iterable[str], packet: str = "p") -> tuple[str, list[str]]:
    """包的分区和 names 有交集。"""
    return has_any_in(f"{packet}.partitions", names)


def visible_scope(identity: Identity, partitions: str, sensitive: str) -> tuple[list[str], list[object]]:
    """一条事实、关系自己的可见范围看得到（J5.5）：partitions、sensitive 是它的两列（JSON 列表），算法和包一样，
    分区有一个能看就过，敏感标记每个都要能看（query-api 3.2）。库、撤回不在这里，调用的地方自己加。"""
    conditions: list[str] = []
    params: list[object] = []
    if identity.partitions is not None:
        sql, names = has_any_in(partitions, identity.partitions)
        conditions.append(sql)
        params.extend(names)
    if identity.sensitive is not None:
        tags = sorted(identity.sensitive)
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM json_each({sensitive}) WHERE json_each.value NOT IN ({_placeholders(tags)}))"
        )
        params.extend(tags)
    return conditions, params


def sees(identity: Identity, partitions: Iterable[str], sensitive: Iterable[str]) -> bool:
    """同一个算法，在 Python 里算：这个可见范围这个身份看不看得到。"""
    if identity.partitions is not None and not set(partitions) & identity.partitions:
        return False
    return identity.sensitive is None or set(sensitive) <= identity.sensitive


def visible_packet(identity: Identity, packet: str = "p") -> tuple[list[str], list[object]]:
    """query-api 3.2 的判定规则，变成 SQL 条件和参数。packet 是 packets 表在查询里的别名。"""
    conditions = [f"{packet}.env = ?", f"{packet}.state = 'current'", f"{packet}.withdrawn_at IS NULL"]
    params: list[object] = [identity.env]
    if identity.partitions is not None:
        sql, names = has_any_partition(identity.partitions, packet)
        conditions.append(sql)
        params.extend(names)
    if identity.sensitive is not None:
        tags = sorted(identity.sensitive)
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM json_each({packet}.sensitive)"
            f" WHERE json_each.value NOT IN ({_placeholders(tags)}))"
        )
        params.extend(tags)
    return conditions, params
