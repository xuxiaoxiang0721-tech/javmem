"""javmem versions 和探针（U2 甲，D205 ⑩⑪）：列全部版本、--save 存基准、--check 和基准比。都不连网：探针用假的向量服务、
假的本机模型；服务商实际给的模型名从编的结果库、编的 token 账读。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from javmem import probes, versions
from javmem.cli import main
from javmem.embedding import EmbeddingError, FakeEmbeddings
from javmem.llm import Usage
from javmem.pipeline.extract import PROMPTS
from javmem.pipeline.merge import L5_VERSION
from javmem.pipeline.reljudge import FakeLocal
from javmem.pipeline.store import ResultStore
from javmem.usage import TokenLedger

REPO = Path(__file__).resolve().parents[1]


def snap(env: dict[str, str] | None = None, **kwargs: object) -> dict[str, object]:
    return versions.snapshot({} if env is None else env, **kwargs)  # type: ignore[arg-type]


def test_lists_rules_prompts_and_ledger_version() -> None:
    found = snap(probe=False)
    assert found["format"] == versions.FORMAT
    assert found["rules"]["L5"] == L5_VERSION  # type: ignore[index]
    for version, digest in PROMPTS.items():  # 照现在的文件算的指纹，和代码里钉着的一样
        assert found["prompts"][f"l3:{version}"] == digest  # type: ignore[index]
    from javmem.ledger import latest_version

    assert found["ledger"] == {"code": latest_version(), "prod": None, "test": None}
    assert found["code"]["javmem"]  # type: ignore[index]
    assert "pytest" in found["packages"] and "javmem" not in found["packages"]  # type: ignore[operator]
    assert found["probes"] == {"skipped": "--no-probe"} and found["neo4j"] == {"skipped": "没加 --graph"}


def test_models_say_what_is_wanted_and_flag_relays() -> None:
    env = {"JAVMEM_LLM_MODEL": "model-x", "JAVMEM_LLM_API_KEY": "sk-not-a-real-key-123456", "JAVMEM_LLM_SERVICE_TIER": "flex"}
    found = versions.models(env)
    assert found["l3"] == {"model": "model-x", "effort": "auto", "output": "json_schema", "tier": "flex",
                           "base_url": "https://api.openai.com/v1", "official": True, "key_set": True}
    assert versions.unofficial(found) == []
    relay = versions.models({"JAVMEM_LLM_BASE_URL": "https://relay.example.com/v1"})
    assert relay["l3"]["official"] is False  # type: ignore[index]
    assert versions.unofficial(relay) == ["l3 的模型地址不是官方的：https://relay.example.com/v1"]
    broken = versions.models({"JAVMEM_LLM_BASE_URL": "ftp://x"})
    assert "error" in broken["l3"]  # type: ignore[operator]  # 写错了照实写，不报配置错误


def test_no_secret_ever_shows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "sk-not-a-real-key-ABCDEF123456"
    for name in ("JAVMEM_LLM_API_KEY", "JAVMEM_SOFT_API_KEY", "JAVMEM_EMBEDDING_API_KEY"):
        monkeypatch.setenv(name, secret)
    monkeypatch.setenv("JAVMEM_GRAPH_NEO4J_PASSWORD", "not-a-real-password-99")
    assert main(["versions", "--json", "--no-probe", "--save", str(tmp_path / "base.json")]) == 0
    text = capsys.readouterr().out + (tmp_path / "base.json").read_text(encoding="utf-8")
    assert secret not in text and "not-a-real-password-99" not in text


def test_served_names_come_from_the_store_and_the_ledger(tmp_path: Path) -> None:
    with ResultStore(tmp_path / "l3") as store:
        for key, name, at in (("k1", "m-2026-10-01", "2026-11-01T10:00:00+08:00"), ("k2", "m-2026-11-01", "2026-11-10T10:00:00+08:00")):
            store.save_result(cache_key=key, window_sha="s", prompt_version="l3-v17", model="m@auto/json_schema", output="{}",
                              parsed={}, response_model=name, usage=Usage(1, 1), seconds=0.1, created_at=at)
    ledger = tmp_path / "ledger.csv"
    TokenLedger(ledger).append({"at": "2026-11-12T09:00:00+08:00", "model": "soft-model", "response_model": "soft-model-0101"})
    found = versions.served([tmp_path / "l3" / "l3.db"], [], ledger)
    assert found["models"]["m@auto/json_schema"]["name"] == "m-2026-11-01"  # type: ignore[index]
    assert found["models"]["m@auto/json_schema"]["names"] == ["m-2026-10-01", "m-2026-11-01"]  # type: ignore[index]
    assert found["models"]["soft-model"]["from"] == "ledger.csv"  # type: ignore[index]
    assert not (tmp_path / "l3" / "l3.db-shm").exists()  # 只读打开，不在旁边留东西


class Shifted(FakeEmbeddings):
    """模型悄悄换了：每个向量的第一维挪一点（在 float32 能看出来的范围里）。"""

    def __init__(self, shift: float) -> None:
        super().__init__(name="fake-embedding")
        self.shift = shift

    def embed(self, texts):  # noqa: ANN001
        from javmem.embedding import EmbeddingReply, pack_vector, unpack_vector

        reply = super().embed(texts)
        moved = []
        for blob in reply.vectors:
            values = list(unpack_vector(blob))
            values[0] += self.shift
            moved.append(pack_vector(values))
        return EmbeddingReply(moved, reply.tokens, reply.seconds)


def test_embedding_probe_same_and_moved() -> None:
    base = probes.embed_probe(FakeEmbeddings())
    assert len(base["vectors"]) == len(probes.EMBED_SENTENCES) == 20  # type: ignore[arg-type]
    assert probes.compare_embed(base, probes.embed_probe(FakeEmbeddings())) == []
    assert probes.compare_embed(base, probes.embed_probe(Shifted(1e-6))) == []  # 万分之一以内算没变
    moved = probes.compare_embed(base, probes.embed_probe(Shifted(1e-3)))
    assert len(moved) == 1 and "20 / 20 句" in moved[0]
    renamed = probes.compare_embed(base, probes.embed_probe(FakeEmbeddings(name="other")))
    assert renamed == ["向量模型的名字变了：fake-embedding → other"]


def test_qwen_probe_same_and_moved() -> None:
    base = probes.qwen_probe(FakeLocal([0.9] * 20))  # 每道连问两遍（D229 ⑫）
    assert [one["first"] for one in base["answers"]] == ["是"] * 10  # type: ignore[index, union-attr]
    assert probes.compare_qwen(base, probes.qwen_probe(FakeLocal([0.905] * 20))) == []  # 0.05 以内算没变（D229 ⑫）
    moved = probes.compare_qwen(base, probes.qwen_probe(FakeLocal([0.9] * 18 + [0.3] * 2)))
    assert len(moved) == 1 and "第 10 题" in moved[0] and "'是' → '否'" in moved[0]


def test_probe_questions_use_the_judge_c_question() -> None:
    """探针照判断 C 本机那一档的问法问：同一句问话，最后一个字要回“是”或“否”。"""
    from javmem.pipeline.reljudge import LOCAL_ASK, load_judge_prompt

    local = FakeLocal()
    probes.qwen_probe(local)
    assert len(local.calls) == 20 and local.calls[0] == local.calls[1]  # 每道连问两遍（D229 ⑫）
    system, user = local.calls[0]
    assert system == load_judge_prompt() and user.startswith("【关系】") and user.endswith(LOCAL_ASK)


def test_check_finds_changes_and_missing_probes() -> None:
    base = snap(embedder=FakeEmbeddings(), local=FakeLocal([0.9] * 20))
    same = snap(embedder=FakeEmbeddings(), local=FakeLocal([0.9] * 20))
    assert versions.compare(base, same) == ([], [])
    moved = snap(embedder=Shifted(1e-2), local=FakeLocal([0.9] * 20))
    different, _ = versions.compare(base, moved)
    assert len(different) == 1 and different[0].startswith("向量探针")

    class Down(FakeEmbeddings):
        def embed(self, texts):  # noqa: ANN001
            raise EmbeddingError("transport", "连不上")

    down = snap(embedder=Down(), local=FakeLocal([0.9] * 20))
    different, _ = versions.compare(base, down)
    assert different == ["探针 embedding：这次没量到（transport），说不清变没变"]
    assert versions.compare(base, down, only=["rules", "prompts"]) == ([], [])


def test_check_from_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "base.json"
    assert main(["versions", "--no-probe", "--save", str(base)]) == 0
    assert main(["versions", "--no-probe", "--check", str(base)]) == 0
    assert "一样" in capsys.readouterr().out
    saved = json.loads(base.read_text(encoding="utf-8"))
    saved["rules"]["L5"] = "l5-v0"
    saved["prompts"]["l3:l3-v17"] = "0" * 64
    base.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    assert main(["versions", "--no-probe", "--check", str(base)]) == 1
    out = capsys.readouterr().out
    assert "不一样的 2 处" in out and f"规则：L5 l5-v0 → {L5_VERSION}" in out
    assert main(["versions", "--no-probe", "--check", str(base), "--only", "code,packages"]) == 0
    monkeypatch.setenv("JAVMEM_LLM_BASE_URL", "https://relay.example.com/v1")  # 地址不是官方的：--check 一定算不一样
    assert main(["versions", "--no-probe", "--check", str(base), "--only", "models"]) == 1
    assert "不是官方的" in capsys.readouterr().out


def test_bad_arguments_and_saving_inside_the_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["versions", "--no-probe", "--save", str(REPO / "base.json")]) == 2
    assert not (REPO / "base.json").exists()
    assert main(["versions", "--no-probe", "--only", "rules"]) == 2  # --only 只配 --check
    base = tmp_path / "base.json"
    assert main(["versions", "--no-probe", "--save", str(base)]) == 0
    assert main(["versions", "--no-probe", "--check", str(base), "--only", "nonsense"]) == 2
    assert main(["versions", "--no-probe", "--check", str(tmp_path / "missing.json")]) == 2
    (tmp_path / "other.json").write_text('{"format": "something/9"}', encoding="utf-8")
    assert main(["versions", "--no-probe", "--check", str(tmp_path / "other.json")]) == 1


def test_probe_calls_go_into_the_token_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.csv"
    found = snap({"JAVMEM_TOKEN_LEDGER": str(ledger)}, embedder=FakeEmbeddings(), local=FakeLocal())
    assert versions._measured(found["probes"]["embedding"]) and versions._measured(found["probes"]["qwen"])  # type: ignore[index]
    rows = list(TokenLedger(ledger).rows())
    assert [row["purpose"] for row in rows] == ["versions_probe_embed"] * 2 + ["versions_probe_qwen"] * 20  # 每道两遍
    assert {row["tag"] for row in rows} == {versions.DEFAULT_TAG}
    text = ledger.read_text(encoding="utf-8")
    assert "鹿鸣" not in text and "白杨" not in text  # 只记数，不记内容


def test_probes_only_talk_to_this_machine() -> None:
    found = versions.run_probes({"JAVMEM_EMBEDDING_BASE_URL": "https://embed.example.com/v1",
                                 "JAVMEM_JUDGE_URL": "http://10.0.0.5:8080/v1/chat/completions"}, None)
    assert found == {"embedding": {"skipped": "向量服务不是本机的：探针只连本机"},
                     "qwen": {"skipped": "本机模型的地址不是本机的：探针只连本机"}}
    assert versions.run_probes({}, None)["qwen"]["skipped"].startswith("没配")  # type: ignore[index]
