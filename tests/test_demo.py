"""编的演示（U3）：`javmem demo --to 新的空目录`。数据是 GitHub 那一层回归的编的小套件，照录好的回复走，不调模型。

- 走一整圈的那一条只在 ubuntu 跑（和 test_regress_mini 一样：Windows 那一路一分钟不加，D205 ①）；本机要看就直接跑命令。
- 别的几条（目录不对、数据不全、子进程拿到的环境）两个系统都跑，不起子进程。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from javmem import demo
from javmem.cli import main

REPO = Path(__file__).resolve().parents[1]
MINI = REPO / "tests" / "fixtures" / "regress-mini"


def test_the_default_data_is_the_made_up_ci_suite() -> None:
    assert demo.default_source() == MINI
    plan = demo.load_plan(MINI)
    packets = {path.stem for path in (MINI / "packets").glob("*/*.md")}
    assert plan["withdraw"]["packet"] in packets
    access = yaml.safe_load((MINI / "config" / "access.yaml").read_text(encoding="utf-8"))
    assert plan["caller"] in access["identities"]
    assert {ask["tool"] for ask in plan["ask"]} == {"memory_search", "entity_card", "timeline", "relation_at", "raw_search"}


def test_the_target_must_be_outside_the_repo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo", "--to", str(REPO / "demo-out")]) == demo.EXIT_CANNOT
    assert "仓库外" in capsys.readouterr().out
    assert not (REPO / "demo-out").exists()


def test_the_target_must_be_new_or_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "old.txt").write_text("x", encoding="utf-8")
    assert main(["demo", "--to", str(tmp_path)]) == demo.EXIT_CANNOT
    assert "空目录" in capsys.readouterr().out
    assert sorted(path.name for path in tmp_path.iterdir()) == ["old.txt"]


def test_a_folder_without_demo_data_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo", "--from", str(tmp_path), "--to", str(tmp_path / "out")]) == demo.EXIT_CANNOT
    assert "demo.json" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_the_children_get_no_keys_no_addresses(tmp_path: Path) -> None:
    """子进程的环境：外面的 JAVMEM_* 一个都不带过去（密钥、模型和向量服务的地址、图的密码），只给演示的配置。"""
    outside = {"JAVMEM_LLM_API_KEY": "sk-not-a-real-key-0000000000", "JAVMEM_LLM_BASE_URL": "https://api.example.com/v1",
               "JAVMEM_EMBEDDING_BASE_URL": "http://127.0.0.1:9", "JAVMEM_GRAPH_TEST_NEO4J_PASSWORD": "not-a-password",
               "JAVMEM_DATA_DIR": str(tmp_path / "someone-elses"), "PATH": "kept"}
    env = demo.demo_env(tmp_path / "config", tmp_path / "data", tmp_path / "ledger.csv",
                        {"JAVMEM_L3_PROMPT": "l3-v20", "JAVMEM_LLM_MODEL": "gpt-6.1-sol"}, "+08:00", base=outside)
    javmem_keys = {key for key in env if key.startswith("JAVMEM_")}
    assert javmem_keys == set(demo.DEMO_KEYS) | {"JAVMEM_L3_PROMPT", "JAVMEM_LLM_MODEL"}
    assert env["JAVMEM_DATA_DIR"] == str(tmp_path / "data") and env["PATH"] == "kept"
    assert env["JAVMEM_GRAPH_SYNC"] == "off" and env["JAVMEM_GRAPH_SEARCH"] == "off"
    with pytest.raises(demo.CannotRun, match="只收"):
        demo.demo_env(tmp_path / "config", tmp_path / "data", tmp_path / "ledger.csv",
                      {"JAVMEM_LLM_BASE_URL": "https://api.example.com/v1"}, "+08:00", base=outside)


def test_ci_runs_the_demo_on_both_machines() -> None:
    """CI 两种机器都照 README 跑一遍演示（U3 戊）：装好 javmem 以后、跑测试以前，输出放 runner 的临时目录，不带密钥。
    这样空的 Windows 上 README 那一圈也走一遍（下面那条走一整圈的测试只在 ubuntu 跑）。"""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    names = [str(step.get("name") or step.get("uses")) for step in steps]
    demo = next((step for step in steps if "javmem demo" in str(step.get("run") or "")), None)
    assert demo is not None, "ci.yml 里没有跑 javmem demo 的那一步"
    assert "if" not in demo, "演示那一步要两种机器都跑"
    assert "RUNNER_TEMP" in demo["run"] and "secrets" not in str(demo)
    assert names.index("安装") < steps.index(demo) < names.index("测试")


@pytest.mark.skipif(sys.platform == "win32", reason="走一整圈的只在 ubuntu 跑（D205 ①：Windows 那一路一分钟不加）")
def test_the_demo_runs_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    to = tmp_path / "demo"
    code = main(["demo", "--to", str(to)])
    out = capsys.readouterr().out
    assert code == demo.EXIT_OK, out
    for words in ("收进来 5 个", "写账", "memory_search", "entity_card", "timeline", "relation_at", "raw_search",
                  "关系准确率", "不用金标准的四个数", "撤回", "彻底删除", "付钱的 0 行", "演示做完"):
        assert words in out, words
    plan = demo.load_plan(MINI)
    packet = plan["withdraw"]["packet"]
    assert not list((to / "data").rglob(f"{packet}.md"))  # 彻底删除了：归档里没有这个包
    assert not (to / "second-run").exists()  # 第二遍的结果库里有原文：算完就删
    assert (to / "steps.log").exists()
    found = json.loads((to / "demo.json").read_text(encoding="utf-8"))
    assert found["wrong"] == 0 and found["paid_rows"] == 0
    assert found["selfcheck"] == {"runs_same": 1.0, "evidence_found": 1.0, "merged_flagged": 0, "entities_in_proposals": 0}
