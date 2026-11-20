"""token 账（javmem.usage）：U2 甲（D205 ⑪）加的一列 response_model，记接口说它实际用的是哪个模型。

- 新的账文件，表头最后多一列 response_model；
- 11-19 以前的账文件表头没有这一列：表头不改（几个进程同时往里记，改表头要整个重写），新记的行照样在最后多一格，
  TokenLedger.rows() 认得出；used() 照旧只数输入、输出；
- 记的名字只留看得懂的那几种字（字母、数字和 ._:/@+-），别的不记，CSV 里不会多出一列、多出一行。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from helpers import build_packet, file_name

from javmem.llm import FakeModel, ModelError, ModelReply, Usage, fake_extract
from javmem.packet import parse_packet
from javmem.pipeline.extract import DONE, ExtractOptions, run_extraction
from javmem.pipeline.normalize import normalize
from javmem.pipeline.store import ResultStore
from javmem.pipeline.window import build_windows
from javmem.redaction import default_patterns
from javmem.usage import FIELDS, OLD_FIELDS, TokenLedger, clean_model

OPTIONS = ExtractOptions(sleep=lambda seconds: None, concurrency=1)


def one_window():
    raw = build_packet(stamp="2026-10-12_1030", body="## 原话 · 10:30–10:31\n- [10:30] 我：越南那批订单 3 号出货\n")
    return build_windows([normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))])


def read(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_new_ledger_has_the_column_last(tmp_path: Path) -> None:
    ledger = TokenLedger(tmp_path / "ledger.csv")
    ledger.append({"tag": "t", "model": "m@auto/json_schema", "input_tokens": 10, "output_tokens": 2,
                   "response_model": "m-2026-11-01"})
    rows = read(tmp_path / "ledger.csv")
    assert rows[0] == list(FIELDS) and FIELDS[-1] == "response_model" and FIELDS[:-1] == OLD_FIELDS
    assert rows[1][-1] == "m-2026-11-01"
    assert [row["response_model"] for row in ledger.rows()] == ["m-2026-11-01"]
    assert ledger.used("t") == 12


def test_old_ledger_keeps_its_header_and_new_rows_carry_one_more_field(tmp_path: Path) -> None:
    """以前的账：表头不动，以前的行不动；新的行最后多一格，rows() 认作 response_model，used() 照旧。"""
    path = tmp_path / "ledger.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(OLD_FIELDS)
        writer.writerow(["2026-11-10T03:26:02+08:00", "old", "l3_extract", "m@auto/json_schema", "w1", "k", "1", "ok",
                         "100", "20", "5", "0", "1.5"])
    before = path.read_bytes()
    ledger = TokenLedger(path)
    ledger.append({"tag": "new", "model": "m@auto/json_schema", "input_tokens": 7, "output_tokens": 3,
                   "response_model": "m-2026-11-01"})
    after = path.read_bytes()
    assert after.startswith(before)  # 以前写的一个字节都没动
    rows = read(path)
    assert rows[0] == list(OLD_FIELDS) and len(rows[1]) == len(OLD_FIELDS) and len(rows[2]) == len(FIELDS)
    got = list(ledger.rows())
    assert [row["tag"] for row in got] == ["old", "new"]
    assert [row["response_model"] for row in got] == ["", "m-2026-11-01"]
    assert ledger.used() == 130 and ledger.used("new") == 10
    with path.open(encoding="utf-8", newline="") as handle:  # 别的脚本照 DictReader 读，照样读得到每一列
        plain = list(csv.DictReader(handle))
    assert plain[1]["input_tokens"] == "7" and plain[1][None] == ["m-2026-11-01"]


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("vendor-model-2026-11-01", "vendor-model-2026-11-01"),
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("Qwen/Qwen3-Embedding-4B", "Qwen/Qwen3-Embedding-4B"),
        ("models/qwen3.8-27b.gguf", "models/qwen3.8-27b.gguf"),
        ("  model-x  ", "model-x"),
        ("a,b", ""),
        ("a\nb", ""),
        ('a"b', ""),
        ("模型", ""),
        ("x" * 101, ""),
        ("", ""),
        (None, ""),
        (42, ""),
        (["model"], ""),
    ],
)
def test_names_are_cleaned(value: object, kept: str) -> None:
    assert clean_model(value) == kept


def test_odd_names_never_break_the_csv(tmp_path: Path) -> None:
    ledger = TokenLedger(tmp_path / "ledger.csv")
    ledger.append({"tag": "t", "response_model": 'bad,"name"\nwith lines'})
    rows = read(tmp_path / "ledger.csv")
    assert len(rows) == 2 and len(rows[1]) == len(FIELDS) and rows[1][-1] == ""


def test_extraction_books_what_the_provider_said(tmp_path: Path) -> None:
    """L3（还有走同一套调用的复核、判断 C 第二档、合并卡片、软变化）：成功的记回复里的模型名。"""

    def reply(system: str, user: str) -> ModelReply:
        return ModelReply(json.dumps(fake_extract(user), ensure_ascii=False), "stop", Usage(100, 50), "model-x-2026-11-01", 0.0)

    ledger = TokenLedger(tmp_path / "ledger.csv")
    with ResultStore(tmp_path / "l3") as store:
        result = run_extraction(one_window(), FakeModel([reply]), store, patterns=default_patterns(), ledger=ledger,
                                options=OPTIONS)
    assert result.count(DONE) == 1
    rows = list(ledger.rows())
    assert [(row["status"], row["model"], row["response_model"]) for row in rows] == [
        ("ok", "fake-model@fake/json_schema", "model-x-2026-11-01")]


def test_failed_calls_book_the_name_when_the_reply_had_one(tmp_path: Path) -> None:
    """回了 200 却拒答、结构认不出的（照样算了钱）：回复里有模型名就记上；没拿到回复的（限流、超时）是空的。"""

    def refused(system: str, user: str) -> ModelReply:
        raise ModelError("refusal", "模型拒绝回答", usage=Usage(30, 2), response_model="model-x-2026-11-01")

    ledger = TokenLedger(tmp_path / "ledger.csv")
    with ResultStore(tmp_path / "l3") as store:
        run_extraction(one_window(), FakeModel(["rate_limit", refused]), store, patterns=default_patterns(), ledger=ledger,
                       options=OPTIONS)
    rows = list(ledger.rows())
    assert [(row["status"], row["response_model"]) for row in rows] == [("rate_limit", ""), ("refusal", "model-x-2026-11-01")]


def test_the_real_client_passes_the_name_on_errors() -> None:
    """OpenAIChat：回了 200、拒答或者结构认不出的，ModelError 带着回复里的模型名（照 service_tier 那样）。"""
    import javmem.llm as llm

    chat = llm.OpenAIChat(llm.LLMSettings(api_key="k" * 20))
    body = {"model": "model-x-2026-11-01", "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            "choices": [{"message": {"refusal": "no"}, "finish_reason": "stop"}]}
    chat._post = lambda data: (200, {}, json.dumps(body).encode("utf-8"))  # type: ignore[method-assign]
    with pytest.raises(ModelError) as caught:
        chat.complete("s", "u", {"type": "object"}, "x")
    assert caught.value.response_model == "model-x-2026-11-01"
    body["choices"] = [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}]
    assert chat.complete("s", "u", {"type": "object"}, "x").model == "model-x-2026-11-01"
