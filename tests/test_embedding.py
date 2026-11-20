"""向量服务：设置、HTTP 客户端（对着本机随机端口上的假服务）、一批一批地调、向量缓存。不连真实服务。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from javmem.config import ConfigError
from javmem.embedding import (
    AUTH,
    BAD_REPLY,
    CONFIG,
    EMBEDDING_DIM,
    MAX_BATCH,
    RATE_LIMIT,
    SERVER,
    TRANSPORT,
    EmbeddingError,
    EmbeddingSettings,
    FakeEmbeddings,
    FakeEmbeddingServer,
    OpenAIEmbeddings,
    embed_all,
    fake_vector,
    pack_vector,
    unpack_vector,
)
from javmem.pipeline.vectors import VectorCache

KEY = "test-key-not-real"


def settings(base_url: str, **env: str) -> EmbeddingSettings:
    return EmbeddingSettings.from_env({"JAVMEM_EMBEDDING_BASE_URL": base_url, "JAVMEM_EMBEDDING_API_KEY": KEY, **env})


# ---------- 设置 ----------


def test_settings_defaults_and_describe_hides_the_key() -> None:
    loaded = settings("http://127.0.0.1:9/v1/")
    assert (loaded.base_url, loaded.model, loaded.timeout) == ("http://127.0.0.1:9/v1", "Qwen/Qwen3-Embedding-4B", 60.0)
    assert KEY not in repr(loaded) and KEY not in str(loaded.describe())
    assert loaded.describe()["api_key_set"] is True


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"JAVMEM_EMBEDDING_BASE_URL": "http://example.com/v1"},  # 不是本机的要 https
        {"JAVMEM_EMBEDDING_BASE_URL": "http://user:pw@127.0.0.1:9/v1"},
        {"JAVMEM_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1?x=1"},
        {"JAVMEM_EMBEDDING_BASE_URL": "ftp://127.0.0.1/v1"},
        {"JAVMEM_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1", "JAVMEM_EMBEDDING_API_KEY": "has space"},
        {"JAVMEM_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1", "JAVMEM_EMBEDDING_MODEL": "bad model"},
        {"JAVMEM_EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1", "JAVMEM_EMBEDDING_TIMEOUT_SECONDS": "nan"},
    ],
)
def test_bad_settings_are_config_errors(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as caught:
        EmbeddingSettings.from_env(env)
    assert "has space" not in str(caught.value)


# ---------- 向量的写法 ----------


def test_pack_and_unpack() -> None:
    values = fake_vector("越南那批订单下周出货")
    blob = pack_vector(values)
    assert len(blob) == 4 * EMBEDDING_DIM
    assert all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7) for a, b in zip(values, unpack_vector(blob)))
    assert abs(sum(value * value for value in values) - 1) < 1e-9
    assert fake_vector("同样的文字") == fake_vector("同样的文字") != fake_vector("别的文字")
    with pytest.raises(EmbeddingError):
        pack_vector(values[:-1])
    with pytest.raises(EmbeddingError):
        pack_vector([math.nan] * EMBEDDING_DIM)


# ---------- HTTP 客户端 ----------


def test_client_against_the_fake_server() -> None:
    with FakeEmbeddingServer(api_key=KEY) as server:
        client = OpenAIEmbeddings(settings(server.base_url))
        reply = client.embed(["第一句", "第二句"])
        assert [unpack_vector(blob)[:3] for blob in reply.vectors] == [
            tuple(pack_and_round(fake_vector(text))[:3]) for text in ("第一句", "第二句")
        ]
        assert reply.tokens > 0 and not reply.estimated
        assert server.requests == [{"model": "Qwen/Qwen3-Embedding-4B", "input": ["第一句", "第二句"]}]
        with pytest.raises(ValueError):
            client.embed([])
        with pytest.raises(ValueError):
            client.embed(["x"] * (MAX_BATCH + 1))


def pack_and_round(values: list[float]) -> tuple[float, ...]:
    return unpack_vector(pack_vector(values))


@pytest.mark.parametrize(
    ("reply", "kind"),
    [(401, AUTH), (404, CONFIG), (429, RATE_LIMIT), (500, SERVER), (400, "bad_request"),
     ("bad_json", BAD_REPLY), ("short", BAD_REPLY), ("wrong_dim", BAD_REPLY)],
)
def test_client_errors_say_what_kind_without_leaking(reply: int | str, kind: str) -> None:
    with FakeEmbeddingServer(api_key=KEY, replies=[reply]) as server:
        client = OpenAIEmbeddings(settings(server.base_url))
        with pytest.raises(EmbeddingError) as caught:
            client.embed(["秘密的原文"])
    assert caught.value.kind == kind
    assert "秘密的原文" not in str(caught.value) and KEY not in str(caught.value)


def test_wrong_key_is_auth() -> None:
    with FakeEmbeddingServer(api_key="another-key") as server:
        with pytest.raises(EmbeddingError) as caught:
            OpenAIEmbeddings(settings(server.base_url)).embed(["x"])
    assert caught.value.kind == AUTH and caught.value.fatal


def test_unreachable_service_is_transport() -> None:
    with FakeEmbeddingServer() as server:
        base = server.base_url
    with pytest.raises(EmbeddingError) as caught:  # 服务关了
        OpenAIEmbeddings(settings(base, JAVMEM_EMBEDDING_TIMEOUT_SECONDS="2")).embed(["x"])
    assert caught.value.kind == TRANSPORT and not caught.value.fatal


# ---------- 一批一批地调 ----------


def test_embed_all_batches_of_16_one_request_at_a_time() -> None:
    texts = [f"第 {index} 句" for index in range(40)] + ["第 0 句"]  # 重复的只算一次
    calls = []
    with FakeEmbeddingServer(api_key=KEY) as server:
        found = embed_all(texts, OpenAIEmbeddings(settings(server.base_url)), record=lambda *args: calls.append(args))
        sizes = [len(request["input"]) for request in server.requests]  # type: ignore[arg-type]
        assert server.peak == 1
    assert sizes == [16, 16, 8]
    assert len(found) == 40 and all(len(blob) == 4 * EMBEDDING_DIM for blob in found.values())
    assert [call[2] for call in calls] == ["ok", "ok", "ok"]


def test_embed_all_retries_transient_errors_and_stops_on_fatal_ones() -> None:
    waits: list[float] = []
    fake = FakeEmbeddings([RATE_LIMIT, SERVER])
    found = embed_all(["一句"], fake, sleep=waits.append)
    assert list(found) == ["一句"] and len(fake.calls) == 3 and waits == [2.0, 4.0]
    with pytest.raises(EmbeddingError):
        embed_all(["一句"], FakeEmbeddings([TRANSPORT] * 4), sleep=waits.append)
    fatal = FakeEmbeddings([AUTH])
    with pytest.raises(EmbeddingError):
        embed_all(["一句"], fatal, sleep=waits.append)
    assert len(fatal.calls) == 1


# ---------- 缓存 ----------


def test_vector_cache_roundtrip_and_cleanup(tmp_path: Path) -> None:
    with VectorCache(tmp_path / "vectors") as cache:
        cache.put("m", {"甲": pack_vector(fake_vector("甲")), "乙": pack_vector(fake_vector("乙"))})
        assert set(cache.get("m", ["甲", "乙", "丙"])) == {"甲", "乙"}
        assert cache.get("另一个模型", ["甲"]) == {}
        assert cache.delete_unused("m", keep=["甲"]) == 1
        assert set(cache.get("m", ["甲", "乙"])) == {"甲"}
        assert cache.count() == 1
