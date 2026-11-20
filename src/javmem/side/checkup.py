"""每周体检（J6.3）：从一段时间里收进来、主线写完了的包里随机抽几个（默认 10，D24），跑支线，和主线比，出体检报告。

- 抽：账本里现行的、没撤回的、主线写完了的（extraction = done）、不是 private 的包，收件时间（received_at）落在
  [since, until) 里，减去支线已经跑过的。按包号排好再用种子抽，种子写进报告，能重来。
- 跑、导出用 `javmem side run`、`javmem side export`；比、出报告用 `javmem side checkup`（javmem.side.compare）。
- 不注册计划任务：谁来每周跑，等接 Bot、巡检时再定。
- 哪些分区每包都跑（第二证人，J6.2）、每周抽几包：仓库外的 YAML（JAVMEM_SIDE_FILE），没配用默认的。

    every_packet_partitions: [finance, investment]
    weekly_count: 10
"""

from __future__ import annotations

import os
import random
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from javmem.config import ConfigError
from javmem.ledger import connect_readonly
from javmem.side.store import SideStore
from javmem.yamlio import load_yaml

ENV_SIDE_FILE = "JAVMEM_SIDE_FILE"
DEFAULT_PARTITIONS = ("finance", "investment")
DEFAULT_WEEKLY = 10


@dataclass(frozen=True)
class SidePlan:
    every_packet_partitions: tuple[str, ...] = DEFAULT_PARTITIONS
    weekly_count: int = DEFAULT_WEEKLY

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SidePlan:
        env = os.environ if env is None else env
        value = env.get(ENV_SIDE_FILE, "").strip()
        if not value:
            return cls()
        try:
            data = load_yaml(Path(value).read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as error:
            raise ConfigError(f"{ENV_SIDE_FILE} 读不了：{type(error).__name__}") from None
        if not isinstance(data, dict) or set(data) - {"every_packet_partitions", "weekly_count"}:
            raise ConfigError(f"{ENV_SIDE_FILE} 只认 every_packet_partitions、weekly_count")
        partitions = data.get("every_packet_partitions", list(DEFAULT_PARTITIONS))
        count = data.get("weekly_count", DEFAULT_WEEKLY)
        if not isinstance(partitions, list) or not all(isinstance(one, str) and one for one in partitions):
            raise ConfigError(f"{ENV_SIDE_FILE} 的 every_packet_partitions 要写成分区的列表")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 200:
            raise ConfigError(f"{ENV_SIDE_FILE} 的 weekly_count 要在 1 到 200 之间")
        return cls(tuple(partitions), count)


def every_packet(partitions: Collection[str], plan: SidePlan) -> bool:
    """第二证人：这个包的分区里有要每包都跑的。"""
    return bool(set(partitions) & set(plan.every_packet_partitions))


def candidates(ledger: Path, store: SideStore, since: datetime, until: datetime) -> list[str]:
    done = {group.packet_id for group in store.groups()}
    conn = connect_readonly(ledger)
    try:
        rows = conn.execute(
            "SELECT packet_id, received_at FROM packets WHERE state = 'current' AND withdrawn_at IS NULL"
            " AND extraction = 'done' AND privacy = 'normal' ORDER BY packet_id"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            received = datetime.fromisoformat(row["received_at"])
        except (TypeError, ValueError):
            continue
        if received.tzinfo is None:
            continue
        if since <= received < until and row["packet_id"] not in done:
            out.append(row["packet_id"])
    return out


def pick(pool: list[str], count: int, seed: int) -> list[str]:
    """按包号排好再用种子抽：同样的包、同样的种子抽出来一样。"""
    ordered = sorted(set(pool))
    if len(ordered) <= count:
        return ordered
    return sorted(random.Random(seed).sample(ordered, count))
