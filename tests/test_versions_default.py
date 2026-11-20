"""javmem versions 不写文件名时用数据目录的那一份基准（巡检报模型、版本变了，D205 ⑪）：`--save`、`--check` 不写文件名是
`logs/versions-baseline.json`；照这一份 `--check` 完记一笔 `logs/versions-status.json`（比了哪几样、和哪一份基准、
不一样几处，探针每一个几处、基准里没量到的是哪几个），巡检读它。都不连网：探针用假的。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from javmem import probes, versions
from javmem.cli import main
from javmem.embedding import FakeEmbeddings
from javmem.pipeline.reljudge import FakeLocal
from test_versions import Shifted


def base_of(data: Path) -> Path:
    return data / "logs" / versions.BASELINE_FILE


def digest_of(data: Path) -> str:
    return hashlib.sha256(base_of(data).read_bytes()).hexdigest()


def status_of(data: Path) -> dict:
    return json.loads((data / "logs" / versions.STATUS_FILE).read_text(encoding="utf-8"))


def fake_probes(monkeypatch: pytest.MonkeyPatch, *answers: dict[str, object]) -> None:
    """versions 跑探针时一回给一份编的结果（不连本机服务）。"""
    left = list(answers)
    monkeypatch.setattr(versions, "run_probes", lambda env, record, embedder=None, local=None, say=None: left.pop(0))


def measured() -> dict[str, object]:
    return {"embedding": probes.embed_probe(FakeEmbeddings()), "qwen": probes.qwen_probe(FakeLocal([0.9] * 20))}


def test_save_and_check_without_a_file_use_the_data_dir(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["versions", "--no-probe", "--save"]) == 0
    saved = json.loads(base_of(data_dir).read_text(encoding="utf-8"))
    assert saved["format"] == versions.FORMAT and str(base_of(data_dir)) in capsys.readouterr().out
    assert main(["versions", "--no-probe", "--check"]) == 0
    assert "一样" in capsys.readouterr().out
    status = status_of(data_dir)
    assert status["format"] == versions.STATUS_FORMAT
    assert status["last"] == {"at": status["last"]["at"], "baseline_at": saved["at"], "baseline_sha256": digest_of(data_dir),
                              "only": list(versions.SECTIONS), "exit": 0, "different": {name: 0 for name in versions.SECTIONS}}
    # 没跑探针（--no-probe）、没查主图（没加 --graph）的不算查过
    assert set(status["sections"]) == set(versions.SECTIONS) - {"probes", "neo4j"}
    assert status["sections"]["rules"] == {"at": status["last"]["at"], "baseline_at": saved["at"],
                                           "baseline_sha256": digest_of(data_dir), "different": 0}


def test_check_records_how_many_differ_and_each_probe(data_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    fake_probes(monkeypatch, measured(),
                {"embedding": probes.embed_probe(Shifted(1e-2)), "qwen": {"error": "transport"}})
    assert main(["versions", "--save"]) == 0
    saved = json.loads(base_of(data_dir).read_text(encoding="utf-8"))
    saved["rules"]["L5"] = "l5-v0"
    base_of(data_dir).write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    assert main(["versions", "--check"]) == 1
    out = capsys.readouterr().out
    assert "不一样的 3 处" in out
    status = status_of(data_dir)
    assert status["last"]["exit"] == 1 and status["last"]["different"]["probes"] == 2
    assert status["last"]["different"]["rules"] == 1
    probe = status["sections"]["probes"]
    assert probe == {"at": status["last"]["at"], "baseline_at": saved["at"], "baseline_sha256": digest_of(data_dir),
                     "different": 2, "probes": {"embedding": 1, "qwen": 1}, "no_baseline": [], "unmeasured": [], "busy": [],
                     "old_version": []}
    text = json.dumps(status, ensure_ascii=False)
    assert "l5-v0" not in text and "Qwen" not in text  # 只有名字和数，不抄 --check 打的那几行


def test_only_updates_what_it_compared(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_probes(monkeypatch, measured(), measured())
    assert main(["versions", "--save"]) == 0
    assert main(["versions", "--check"]) == 0
    first = status_of(data_dir)
    assert first["sections"]["probes"]["different"] == 0
    assert main(["versions", "--no-probe", "--check", "--only", "rules,served"]) == 0
    second = status_of(data_dir)
    assert second["last"]["only"] == ["rules", "served"] and set(second["last"]["different"]) == {"rules", "served"}
    assert second["sections"]["probes"] == first["sections"]["probes"]  # 这回没比探针：上一回的留着
    assert second["sections"]["code"] == first["sections"]["code"]


def test_a_baseline_without_probes_cannot_be_compared(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_probes(monkeypatch, measured())
    assert main(["versions", "--no-probe", "--save"]) == 0  # 存基准时没跑探针
    assert main(["versions", "--check", "--only", "probes"]) == 0  # 照旧只提一句，退出码不变
    probe = status_of(data_dir)["sections"]["probes"]
    assert probe["different"] == 0 and probe["no_baseline"] == ["embedding", "qwen"]  # 巡检看它：比不了


def test_another_file_does_not_touch_the_status(data_dir: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    assert main(["versions", "--no-probe", "--save", str(other)]) == 0
    assert main(["versions", "--no-probe", "--check", str(other)]) == 0
    assert not (data_dir / "logs" / versions.STATUS_FILE).exists()  # 巡检只认数据目录那一份
    assert main(["versions", "--no-probe", "--save"]) == 0
    assert main(["versions", "--no-probe", "--check", str(base_of(data_dir))]) == 0  # 写全了的也是那一份
    assert status_of(data_dir)["last"]["exit"] == 0


def test_without_a_data_dir_a_file_name_is_needed(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["versions", "--no-probe", "--save"]) == 2
    assert "JAVMEM_DATA_DIR" in capsys.readouterr().out
    assert main(["versions", "--no-probe", "--check"]) == 2
    assert "JAVMEM_DATA_DIR" in capsys.readouterr().out


def test_check_before_any_save_says_so(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["versions", "--no-probe", "--check"]) == 2
    out = capsys.readouterr().out
    assert "还没有基准" in out and "javmem versions --save" in out
    assert not (data_dir / "logs" / versions.STATUS_FILE).exists()


def test_a_status_that_cannot_be_written_does_not_change_the_answer(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["versions", "--no-probe", "--save"]) == 0
    (data_dir / "logs" / versions.STATUS_FILE).mkdir()  # 写不进去
    assert main(["versions", "--no-probe", "--check"]) == 0
    assert "记不上" in capsys.readouterr().out


def test_save_and_check_together_are_refused(data_dir: Path) -> None:
    assert main(["versions", "--no-probe", "--save", "--check"]) == 2
    assert not base_of(data_dir).exists()
