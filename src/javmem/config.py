"""配置：都从环境变量读，前缀 JAVMEM_。

路径、身份这些放这里；模型密钥不走这个模块，用到的地方自己读环境变量，不打印。
真实的配置和数据都放仓库外。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

ENV_DATA_DIR = "JAVMEM_DATA_DIR"
ENV_SOURCES_FILE = "JAVMEM_SOURCES_FILE"
ENV_ACCESS_FILE = "JAVMEM_ACCESS_FILE"
ENV_CALLER = "JAVMEM_CALLER"
ENV_TIMEZONE = "JAVMEM_TIMEZONE"
ENV_STALE_HOURS = "JAVMEM_STALE_HOURS"
ENV_REDACTION_FILE = "JAVMEM_REDACTION_FILE"
ENV_OWNER_FILE = "JAVMEM_OWNER_FILE"
ENV_LEXICON_FILE = "JAVMEM_LEXICON_FILE"

PROD = "prod"
TEST = "test"
LIBRARY_NAMES = (PROD, TEST)

DEFAULT_STALE_HOURS = 26.0
MAX_STALE_HOURS = 8760.0  # 一年

_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})", re.ASCII)


class ConfigError(Exception):
    """配置缺了或者写错了。消息里不放配置文件的内容。"""


def parse_timezone(value: str) -> tzinfo:
    """时区只认固定偏移（+08:00）和 UTC。这样不用依赖系统的时区库。"""
    text = value.strip()
    if text.upper() == "UTC":
        return timezone.utc
    match = _OFFSET.fullmatch(text)
    if match is None:
        raise ConfigError(f"{ENV_TIMEZONE} 要写成 +08:00 这样的偏移，或者 UTC")
    sign, hours, minutes = match.groups()
    if int(hours) > 14 or int(minutes) > 59:
        raise ConfigError(f"{ENV_TIMEZONE} 的偏移超出范围")
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    return timezone(-delta if sign == "-" else delta)


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    return Path(value).resolve() if value else None


# 巡检盯着的快慢（巡检加快慢检查，11-16）：越攒越多、攒多了写账会慢的几样，到了这些数就提醒，见开发说明“巡检”
ENV_PATROL_REVIEW_MAX = "JAVMEM_PATROL_REVIEW_MAX"
ENV_PATROL_GROUP_MAX = "JAVMEM_PATROL_GROUP_MAX"
ENV_PATROL_ENTITIES_MAX = "JAVMEM_PATROL_ENTITIES_MAX"
ENV_PATROL_DISK_RATIO = "JAVMEM_PATROL_DISK_RATIO"
ENV_PATROL_WRITE_RATIO = "JAVMEM_PATROL_WRITE_RATIO"
ENV_PATROL_WRITE_RUNS = "JAVMEM_PATROL_WRITE_RUNS"
ENV_PATROL_WRITE_MIN_RUNS = "JAVMEM_PATROL_WRITE_MIN_RUNS"
_MAX_COUNT = 10**9
_MAX_RATIO = 100.0


def _env_count(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        raise ConfigError(f"{name} 要写成 {low} 到 {high} 的整数") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} 要写成 {low} 到 {high} 的整数")
    return value


def _env_ratio(env: Mapping[str, str], name: str, default: float, low: float, *, above: bool) -> float:
    """倍数：above 是要大于 low，不然至少 low；最多 _MAX_RATIO。nan、inf 也是 float，一样不认。"""
    text = env.get(name, "").strip()
    if not text:
        return default
    rule = f"{name} 要写成倍数，{'大于' if above else '至少'} {low:g}，最多 {_MAX_RATIO:g}"
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(rule) from None
    if not math.isfinite(value) or not (value > low if above else value >= low) or value > _MAX_RATIO:
        raise ConfigError(rule)
    return value


@dataclass(frozen=True)
class PatrolLimits:
    """巡检盯着的快慢到了这些数就提醒。现在都不慢，可会越攒越多（收件变慢报告第 4 节）。"""

    review_items: int = 2000  # 待确认（没处理的加处理过的）超过这么多条
    group_facts: int = 1000  # 事实最多的那一组超过这么多条
    entities: int = 20000  # 实体、别名任一超过这么多个
    disk_ratio: float = 2.0  # 账本所在的磁盘剩下的不到账本（连同 -wal）的这么多倍
    write_ratio: float = 2.0  # 最近一次写账按窗口算，是前几次（窗口数差不多的）中间数的这么多倍或更多
    write_runs: int = 30  # 和窗口数差不多的前这么多次比
    write_min_runs: int = 5  # 窗口数差不多、能拿来比的写账（连同最近一次）不到这么多次不报

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PatrolLimits:
        return cls(
            review_items=_env_count(env, ENV_PATROL_REVIEW_MAX, cls.review_items, 1, _MAX_COUNT),
            group_facts=_env_count(env, ENV_PATROL_GROUP_MAX, cls.group_facts, 1, _MAX_COUNT),
            entities=_env_count(env, ENV_PATROL_ENTITIES_MAX, cls.entities, 1, _MAX_COUNT),
            disk_ratio=_env_ratio(env, ENV_PATROL_DISK_RATIO, cls.disk_ratio, 1.0, above=False),
            write_ratio=_env_ratio(env, ENV_PATROL_WRITE_RATIO, cls.write_ratio, 1.0, above=True),
            write_runs=_env_count(env, ENV_PATROL_WRITE_RUNS, cls.write_runs, 1, 1000),
            write_min_runs=_env_count(env, ENV_PATROL_WRITE_MIN_RUNS, cls.write_min_runs, 2, 1000),
        )


@dataclass(frozen=True)
class Library:
    """正式库或测试库：收件目录、归档和账本各是各的，互不相通。"""

    name: str
    inbox: Path
    archive: Path
    db_path: Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path | None
    sources_file: Path | None
    access_file: Path | None
    caller: str | None
    timezone: tzinfo
    stale_hours: float = DEFAULT_STALE_HOURS
    redaction_file: Path | None = None
    owner_file: Path | None = None  # D62：主人的名字
    lexicon_file: Path | None = None  # J2.14：别名表和词典
    clock: Callable[[], datetime] | None = None  # 测试用，平时是现在
    patrol: PatrolLimits = field(default_factory=PatrolLimits)  # 巡检盯着的快慢到了多少提醒

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        tz_text = env.get(ENV_TIMEZONE, "").strip()
        # 没配时用这台机器现在的偏移
        tz = parse_timezone(tz_text) if tz_text else datetime.now().astimezone().tzinfo

        stale_text = env.get(ENV_STALE_HOURS, "").strip()
        try:
            stale_hours = float(stale_text) if stale_text else DEFAULT_STALE_HOURS
        except ValueError:
            raise ConfigError(f"{ENV_STALE_HOURS} 要写成小时数") from None
        # nan 和 inf 也是 float，太大的数算时间时会溢出
        if not 0 < stale_hours <= MAX_STALE_HOURS:
            raise ConfigError(f"{ENV_STALE_HOURS} 要大于 0，最多 {MAX_STALE_HOURS:.0f}")
        patrol = PatrolLimits.from_env(env)

        return cls(
            data_dir=_optional_path(env, ENV_DATA_DIR),
            sources_file=_optional_path(env, ENV_SOURCES_FILE),
            access_file=_optional_path(env, ENV_ACCESS_FILE),
            caller=env.get(ENV_CALLER, "").strip() or None,
            timezone=tz,
            stale_hours=stale_hours,
            patrol=patrol,
            redaction_file=_optional_path(env, ENV_REDACTION_FILE),
            owner_file=_optional_path(env, ENV_OWNER_FILE),
            lexicon_file=_optional_path(env, ENV_LEXICON_FILE),
        )

    def require_data_dir(self) -> Path:
        if self.data_dir is None:
            raise ConfigError(f"没有设置 {ENV_DATA_DIR}")
        return self.data_dir

    def library(self, name: str) -> Library:
        data = self.require_data_dir()
        if name == PROD:
            return Library(PROD, data / "inbox", data / "archive", data / "ledger" / "prod.db")
        if name == TEST:
            return Library(TEST, data / "inbox-test", data / "archive-test", data / "ledger" / "test.db")
        raise ValueError(f"没有这个库：{name}")

    def libraries(self) -> list[Library]:
        return [self.library(name) for name in LIBRARY_NAMES]

    @property
    def failed_dir(self) -> Path:
        return self.require_data_dir() / "failed"

    @property
    def logs_dir(self) -> Path:
        return self.require_data_dir() / "logs"

    def now(self) -> datetime:
        current = self.clock() if self.clock is not None else datetime.now(self.timezone)
        return current.replace(microsecond=0)
