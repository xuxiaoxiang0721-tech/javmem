"""合并工具的测试用的假 L3（J3.6）：照一张表认窗口里的每一行 T，认出来的实体和一条事实（主体是第一个实体）存进 L3 的
结果库，`javmem write --fake` 照常从结果库读、写账，合并工具也从结果库重写窗口。名字和句子都是编的。

表的每一项是 (这一行里有的字, [(名字, 类型, 原文里的写法), ……])，按先后找，一行只用第一项对上的。values 是
(这一行里有的字, 属性, 值)：对上的事实拆成属性和值。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from helpers import World

from javmem.llm import FakeModel, ModelReply, Usage
from javmem.pipeline.batch import load_queued
from javmem.pipeline.extract import run_extraction
from javmem.pipeline.store import ResultStore
from javmem.pipeline.window import build_windows
from javmem.redaction import default_patterns

_TARGET_LINE = re.compile(r"^(T\d+) (?:〔[^〕]*〕)?(.*)$", re.MULTILINE)
Spot = tuple[str, list[tuple[str, str, str]]]


class SpotModel:
    """名字和 FakeModel 一样：`javmem write --fake` 算出来的键对得上。"""

    def __init__(self, table: Sequence[Spot], values: Sequence[tuple[str, str, str]] = ()) -> None:
        self.table = list(table)
        self.values = list(values)
        self.calls = 0

    @property
    def name(self) -> str:
        return FakeModel().name

    @property
    def cache_name(self) -> str:
        return FakeModel().cache_name

    def complete(self, system: str, user: str, schema: object, schema_name: str) -> ModelReply:
        self.calls += 1
        body = self.extract(user)
        return ModelReply(json.dumps(body, ensure_ascii=False), "stop", Usage(10, 10), self.name, 0.0)

    def extract(self, user: str) -> dict[str, object]:
        entities: list[dict[str, str]] = []
        facts = []
        for ref, text in _TARGET_LINE.findall(user):
            found = next((items for needle, items in self.table if needle in text), None)
            if not found:
                continue
            keys = []
            for name, entity_type, mention in found:
                key = next((item["key"] for item in entities if item["name"] == name and item["type"] == entity_type), None)
                if key is None:
                    key = f"e{len(entities) + 1}"
                    entities.append({"key": key, "mention": mention, "name": name, "type": entity_type})
                keys.append(key)
            attribute, value = next(((a, v) for needle, a, v in self.values if needle in text), (None, None))
            quote = text.rstrip("。")
            facts.append(
                {
                    "text": quote,
                    "subject": keys[0],
                    "attribute": attribute,
                    "value": value,
                    "occurred_at": None,
                    "time_precision": "unknown",
                    "certainty": "stated",
                    "speaker": "",
                    "entities": [{"key": key, "role": "subject" if index == 0 else "mention"} for index, key in enumerate(keys)],
                    "evidence": [{"ref": ref, "quote": quote}],
                }
            )
        return {"entities": entities, "facts": facts, "relations": []}


def extract_into_store(world: World, model: SpotModel, library: str = "prod") -> None:
    """排着队的包切窗、用假 L3 抽、存进数据目录下的结果库（l3）。"""
    batch = load_queued(world.settings, library)
    windows = build_windows(batch.packets, context_pool=batch.context)
    with ResultStore(world.data / "l3") as store:
        result = run_extraction(windows, model, store, patterns=default_patterns())
    assert result.stopped is None
