"""真实模型的 token 记账：每次调用一行 CSV，失败、重试也记，不记内容。

列：时间、标签（一段工作用一个，预算按它算）、用途、模型、窗口、键、第几次、结果、
输入、输出、其中推理、输入里命中缓存的、秒数、接口说它实际用的模型（`response_model`）。几个进程、几段工作可以记在同一个文件里。

`model` 是要的那个（L3 写成 `模型名@推理档位/输出方式`，和结果库的键一样）；`response_model` 是回复里写的（服务商实际给的，
常常带着日子，比如 `某模型-2026-11-01`），U2 甲（D205 ⑪）加的：拿它和上一次比，看服务商有没有悄悄换模型（`javmem versions`）。
没拿到回复的（限流、超时、连不上）是空的。

**11-19 以前的账文件**表头没有最后这一列。表头不改：几个进程同时往一个文件里记，改表头要整个重写，重写的那一下别的进程记的
会丢。新记的行照样在最后多一格；`TokenLedger.rows()` 认得出（表头没有这一列、行多一格的，多的那一格就是它），
`csv.DictReader` 照样读得到每一列（多的那一格在 `row[None]` 里），按位置读的也一样（加在最后）。要表头也有这一列，
换一个新的账文件就是了。
"""

from __future__ import annotations

import csv
import os
import re
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path

OLD_FIELDS = (
    "at",
    "tag",
    "purpose",
    "model",
    "window_id",
    "cache_key",
    "attempt",
    "status",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "seconds",
)
FIELDS = (*OLD_FIELDS, "response_model")

# 回复里的模型名只留看得懂的那几种字：别的（逗号、引号、换行、中文……）不记，CSV 里不会多出一列、多出一行
_MODEL_NAME = re.compile(r"[A-Za-z0-9._:/@+-]{1,100}")


def clean_model(value: object) -> str:
    """接口说它用的模型名，记账、比对用：去掉两头的空白；不是字符串、有别的字、超过 100 个字的不记（空的）。"""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if _MODEL_NAME.fullmatch(value) else ""


class TokenLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, row: Mapping[str, object]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists() or self.path.stat().st_size == 0
            values = {name: row.get(name, "") for name in FIELDS}
            values["response_model"] = clean_model(row.get("response_model"))
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
                if new:
                    writer.writeheader()
                writer.writerow(values)
                handle.flush()
                os.fsync(handle.fileno())

    def rows(self) -> Iterator[dict[str, str]]:
        """一行一个字典，以前的账（表头没有 response_model）也认：多出来的那一格当 response_model，没有的是空的。"""
        if not self.path.exists():
            return
        with self.path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if not header:
                return
            extra = "response_model" not in header
            for values in reader:
                if not values:
                    continue
                out = dict(zip(header, values))
                if extra:
                    out["response_model"] = values[len(header)] if len(values) > len(header) else ""
                yield out

    def used(self, tag: str | None = None) -> int:
        """记下的输入加输出，tag 给了就只算这个标签的。"""
        with self._lock:
            total = 0
            for row in self.rows():
                if tag is not None and row.get("tag") != tag:
                    continue
                for name in ("input_tokens", "output_tokens"):
                    try:
                        total += int(row.get(name) or 0)
                    except ValueError:
                        continue
            return total
