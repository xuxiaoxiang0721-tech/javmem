"""javmem versions 审阅查出来的那几处（U2 甲，11-19）：向量服务的地址配错了不原样打、“服务商实际给的”一个都没读到不算一样、
--only 什么都没写不算比过、探针碰上断了的回复照样记成没量到（不崩）。都不连网。"""

from __future__ import annotations

import http.client
import json
from pathlib import Path

import pytest

from javmem import versions
from javmem.cli import main
from javmem.embedding import FakeEmbeddings
from javmem.pipeline.reljudge import FakeLocal


def test_a_bad_embedding_address_is_not_printed() -> None:
    found = versions.models({"JAVMEM_EMBEDDING_BASE_URL": "https://user:secret-word@embed.example.com/v1?api_key=abc"})
    text = json.dumps(found, ensure_ascii=False)
    assert "secret-word" not in text and "api_key=abc" not in text
    assert "error" in found["embedding"]  # type: ignore[operator]
    ok = versions.models({"JAVMEM_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1"})
    assert ok["embedding"]["base_url"] == "http://127.0.0.1:9/v1" and ok["embedding"]["local"] is True  # type: ignore[index]


def test_served_names_read_from_nowhere_are_not_the_same() -> None:
    saved = {"format": versions.FORMAT, "served": {"read": ["l3.db"], "models": {"m": {"name": "m-1"}}}}
    now = {"format": versions.FORMAT, "served": {"read": [], "models": {}}}
    different, notes = versions.compare(saved, now, only=["served"])
    assert different == ["服务商实际给的：这次一个结果库、账都没读到（基准里有 1 个模型），说不清变没变"]
    now = {"format": versions.FORMAT, "served": {"read": ["l3.db"], "models": {}}}
    different, notes = versions.compare(saved, now, only=["served"])
    assert different == [] and notes == ["服务商实际给的：基准里有 m，这次没读到"]


def test_only_needs_a_section(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "base.json"
    assert main(["versions", "--no-probe", "--save", str(base)]) == 0
    assert main(["versions", "--no-probe", "--check", str(base), "--only", ","]) == 2
    assert "没写要比哪几样" in capsys.readouterr().out


class Broken:
    """本机 Qwen 回到一半断了（http.client 的那类错）。"""

    name = "qwen-x"

    def chat(self, system, user, **kwargs):  # noqa: ANN001, ANN003, ANN201
        raise http.client.IncompleteRead(b"")


def test_probes_that_break_halfway_are_recorded_not_raised() -> None:
    found = versions.run_probes({}, None, embedder=FakeEmbeddings(), local=Broken())
    assert found["qwen"] == {"error": "IncompleteRead"}
    assert versions._measured(found["embedding"])  # type: ignore[arg-type]

    class Weird(FakeEmbeddings):
        def embed(self, texts):  # noqa: ANN001
            raise RuntimeError("连接池坏了")

    assert versions.run_probes({}, None, embedder=Weird(), local=FakeLocal())["embedding"] == {"error": "RuntimeError"}
