"""校准工具包（U3，D141 ③）：预标（`javmem gold prelabel`）和审阅页面（`javmem gold review`）。

做法：挑十来个自己的包，先让 javmem 照现在的抽取结果出一份金标准的草稿（预标），本人在审阅页面上一条一条点
对、错、改，再补漏了的，存下来就是打分认的审阅结果。这里拿编的小套件走一遍：照录好的回复建结果库、写账，出草稿，
草稿要过 `javmem gold check`；拿草稿给同一份抽取打分，关系、事实应该都对得上（草稿就是它自己抽的）。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from javmem.cli import main
from javmem.regress.replies import build_store

MINI = Path(__file__).resolve().parent / "fixtures" / "regress-mini"
REPO = Path(__file__).resolve().parents[1]
PACKETS = sorted(path.stem for path in (MINI / "packets" / "demo-bot").glob("*.md"))
ENV = {
    "JAVMEM_SOURCES_FILE": str(MINI / "config" / "sources.yaml"),
    "JAVMEM_ACCESS_FILE": str(MINI / "config" / "access.yaml"),
    "JAVMEM_OWNER_FILE": str(MINI / "config" / "owner.yaml"),
    "JAVMEM_LEXICON_FILE": str(MINI / "config" / "lexicon.yaml"),
    "JAVMEM_TIMEZONE": "+08:00",
    "JAVMEM_L3_PROMPT": "l3-v20",
    "JAVMEM_LLM_MODEL": "gpt-6.1-sol",
}


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """编的小套件照录好的回复抽完、写完账的数据目录（不调模型，假向量）。"""
    root = tmp_path_factory.mktemp("prelabel")
    data = root / "data"
    with pytest.MonkeyPatch.context() as patch:
        for name in [name for name in os.environ if name.startswith("JAVMEM_")]:
            patch.delenv(name)
        for key, value in ENV.items():
            patch.setenv(key, value)
        patch.setenv("JAVMEM_DATA_DIR", str(data))
        patch.setenv("JAVMEM_TOKEN_LEDGER", str(root / "token-ledger.csv"))
        assert main(["init"]) == 0
        inbox = data / "inbox-test" / "demo-bot"
        inbox.mkdir(parents=True, exist_ok=True)
        for packet in sorted((MINI / "packets" / "demo-bot").glob("*.md")):
            shutil.copy2(packet, inbox / packet.name)
        assert main(["intake"]) == 0
        build_store(MINI / "l3-replies.json", data / "l3")
        assert main(["write", "--library", "test", "--fake-embeddings"]) == 0
    return data


@pytest.fixture
def use(written: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(written))
    return written


def prelabel(tmp_path: Path) -> Path:
    out = tmp_path / "draft"
    args = ["gold", "prelabel", "--library", "test", "--unit", "D1", "--out", str(out)]
    for packet in PACKETS:
        args += ["--packet", packet]
    assert main(args) == 0
    return out


def test_the_draft_passes_the_check_and_covers_every_kind(use: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = prelabel(tmp_path)
    unit = json.loads((out / "D1.json").read_text(encoding="utf-8"))
    assert unit["schema"] == "javmem-gold-prelabel/1" and unit["packets"] == PACKETS
    kinds = {one["type"] for one in unit["items"]}
    assert {"fact", "relation", "alias"} <= kinds
    assert all(one["evidence"] for one in unit["items"])
    assert len({one["id"] for one in unit["items"]}) == len(unit["items"])
    capsys.readouterr()
    assert main(["gold", "check", "--gold", str(out), "--library", "test"]) == 0, capsys.readouterr().out


def test_scoring_the_same_extraction_against_its_own_draft_agrees(use: Path, tmp_path: Path) -> None:
    out = prelabel(tmp_path)
    scores_dir = tmp_path / "score"
    assert main(["score", "--gold", str(out), "--side", f"javmem={use}", "--library", "test", "--scores-only",
                 "--out", str(scores_dir)]) == 0
    side = json.loads((scores_dir / "分数.json").read_text(encoding="utf-8"))["sides"]["javmem"]
    assert side["relations"]["precision"] == 1.0 and side["relations"]["recall"] == 1.0
    assert side["facts"]["complete_rate"] == 1.0


def test_the_draft_must_go_outside_the_repo(use: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["gold", "prelabel", "--library", "test", "--unit", "D1", "--out", str(REPO / "draft-here"), "--packet", PACKETS[0]]
    assert main(args) == 2
    assert "仓库外" in capsys.readouterr().out and not (REPO / "draft-here").exists()


def test_unknown_packets_are_refused(use: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["gold", "prelabel", "--library", "test", "--unit", "D1", "--out", str(tmp_path / "d"), "--packet", "2026-01-01_0000_nobody"]
    assert main(args) == 2
    assert "没有这个包" in capsys.readouterr().out


# ---------- 审阅页面 ----------


def test_the_review_page_has_a_card_per_item_and_no_outside_links(use: Path, tmp_path: Path) -> None:
    page = tmp_path / "review.html"
    assert main(["gold", "review", "--gold", str(MINI / "gold"), "--library", "test", "--out", str(page)]) == 0
    html = page.read_text(encoding="utf-8")
    unit = json.loads((MINI / "gold" / "D1.json").read_text(encoding="utf-8"))
    for one in unit["items"]:
        assert one["id"] in html
    assert "http://" not in html and "https://" not in html and "<script src" not in html  # 不连外面、不拉别的文件
    assert "以后给客户的报价单一律先发我看过再外发。" in html  # 有数据目录的，卡片上带着出处那一句原话


def test_the_review_page_starts_from_an_earlier_review(use: Path, tmp_path: Path) -> None:
    earlier = tmp_path / "earlier.json"
    earlier.write_text(json.dumps({"reviews": {"D1-F001": {"v": "wrong", "text": "不对"}}, "additions": []}, ensure_ascii=False),
                       encoding="utf-8")
    page = tmp_path / "review.html"
    assert main(["gold", "review", "--gold", str(MINI / "gold"), "--review", str(earlier), "--out", str(page)]) == 0
    html = page.read_text(encoding="utf-8")
    start = html.index('id="earlier"')
    assert '"D1-F001"' in html[start:start + 4000] and '"wrong"' in html[start:start + 4000]


def test_the_review_page_must_go_outside_the_repo(use: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gold", "review", "--gold", str(MINI / "gold"), "--out", str(REPO / "review.html")]) == 2
    assert "仓库外" in capsys.readouterr().out and not (REPO / "review.html").exists()
