"""本机 Qwen 探针（D229 ⑫）：
- 线：P(是) 差不到 0.05 算没变（原来 0.01）；回的第一个字变了照样报。
- 量的时候核有没有别人在用（8180 的 llama.cpp 关着 /slots、/metrics，不靠它）：关掉提示缓存，每道连问两遍，两遍 P 差不到
  0.005、回的字一样才算量到；一对不上马上收掉这一回（不把 10 道跑完），当有人在用，隔 30 秒再量；5 分钟都对不上，这回没量到。
- 这回没量到的：--check 只提一句、不算不一样，记一笔里写上，巡检记 checks_skipped（probe_busy），不报 probe_changed；
  --save 不存。
- 探针换成 pq2：拿它比 pq1 存的旧基准，报“基准是旧版探针存的，要重存”，不报成变了。
都用假的本机模型、假的钟，不连网、不真等。"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import World
from test_versions_default import base_of, fake_probes, measured, status_of

from javmem import probes, versions
from javmem.cli import main
from javmem.embedding import FakeEmbeddings
from javmem.patrol import patrol_report
from javmem.pipeline import preb
from javmem.pipeline.reljudge import FakeLocal
from javmem.usage import TokenLedger

BUSY = {"busy": "等了 300 秒，一直有别人在用", "waited": 300, "tries": 10}


def twice(*ps: float) -> list[float]:
    """假的本机模型照这一串回：每道连问两遍，一样的数写两次。"""
    return [p for p in ps for _ in (0, 1)]


def measure(*ps: float) -> dict[str, object]:
    return probes.qwen_probe(FakeLocal(twice(*ps)))


# ---------- 线 ----------


def test_the_line_is_five_hundredths() -> None:
    assert probes.QWEN_TOLERANCE == 0.05
    base = measure(*[0.9] * 10)
    assert probes.compare_qwen(base, measure(*[0.94] * 10)) == []  # 差 0.04：照新的线算没变
    assert probes.compare_qwen(base, measure(*[0.86] * 10)) == []  # 往下差 0.04 也一样
    moved = probes.compare_qwen(base, measure(*[0.9] * 9, 0.96))
    assert len(moved) == 1 and "1 / 10 道题变了（线是 0.05）" in moved[0] and "第 10 题 P(是) 0.9 → 0.96" in moved[0]


def test_exactly_five_hundredths_counts_as_moved() -> None:
    """“差不到 0.05 才算没变”：正好差 0.05 的算变了（和向量那一样，差到线上就算）。P 存的是 6 位小数，照 6 位比。"""
    base = measure(*[0.9] * 10)
    moved = probes.compare_qwen(base, measure(0.95, *[0.9] * 9))
    assert len(moved) == 1 and "第 1 题 P(是) 0.9 → 0.95" in moved[0]
    assert probes.compare_qwen(base, measure(0.949999, *[0.9] * 9)) == []


def test_the_first_character_still_counts_inside_the_line() -> None:
    """P 只差 0.02（在线以内），可回的第一个字从“是”变成了“否”：照样算变了。"""
    base = measure(0.51, *[0.9] * 9)
    moved = probes.compare_qwen(base, measure(0.49, *[0.9] * 9))
    assert len(moved) == 1 and "第 1 题 P(是) 0.51 → 0.49（回 '是' → '否'）" in moved[0]


def test_the_u1_night_readings_are_inside_the_new_line() -> None:
    """U1 那一夜空下来再量的两道（0.431 → 0.392、0.112 → 0.097，回的字没变）：照新的线算没变；
    有人在用时的那一道（0.431 → 0.334）还是算变了，所以量的时候要核有没有别人在用。"""
    others = [0.9] * 8
    base = measure(0.431, 0.112, *others)
    assert probes.compare_qwen(base, measure(0.392, 0.097, *others)) == []
    busy = probes.compare_qwen(base, measure(0.334, 0.112, *others))
    assert len(busy) == 1 and "第 1 题 P(是) 0.431 → 0.334" in busy[0]


# ---------- 每道问两遍：对得上才算量到 ----------


class Clock:
    """假的钟：sleep 只把钟往前拨，不真等。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


QUESTION = {probes.qwen_question(relation, lines): index for index, (relation, lines) in enumerate(probes.QWEN_QUESTIONS)}
Noise = Callable[[int, int, float], tuple[float, bool]]


class SharedQwen(FakeLocal):
    """假的本机 Qwen，和别人共用。noise(第几次调用（从 1 数）, 第几道（从 0 数）, 钟) -> (P 挪多少, 回的字变不变)：
    有人在用时 P 带点抖动、回的字偶尔变一变；(0, False) 是空着，每道每次回的一模一样（P 照 ps）。每问一次，钟走 1 秒。"""

    def __init__(self, clock: Clock, noise: Noise, ps: tuple[float, ...] = (0.9,) * 10) -> None:
        super().__init__(name="qwen-shared")
        self.clock = clock
        self.noise = noise
        self.ps = ps
        self.asked: list[tuple[float, int]] = []  # 每问一次：钟、第几道

    def chat(self, system, user, *, max_tokens, schema=None, logprobs=False):  # noqa: ANN001
        index = QUESTION[user]
        self.asked.append((self.clock.now, index))
        shift, flip = self.noise(len(self.asked), index, self.clock.now)
        p = min(0.999, max(0.001, self.ps[index] + shift)) if shift else self.ps[index]
        yes, no = math.log(p), math.log(1 - p)
        self.script.appendleft({"是": yes, "否": no} if (p >= 0.5) != flip else {"否": no, "是": yes})
        self.clock.now += 1.0
        return super().chat(system, user, max_tokens=max_tokens, schema=schema, logprobs=logprobs)


def jitter(seed: int, until: float = math.inf) -> Noise:
    """钟在 until 以前有人在用：P 在 ±0.06 里抖，五次里一次回的字变一变。"""
    rng = random.Random(seed)

    def noise(call: int, index: int, now: float) -> tuple[float, bool]:
        return (rng.uniform(-0.06, 0.06), rng.random() < 0.2) if now < until else (0.0, False)

    return noise


def probe(local: SharedQwen, clock: Clock) -> dict[str, object]:
    return probes.qwen_probe(local, sleep=clock.sleep, clock=clock)


def test_an_idle_qwen_is_measured_on_the_first_try() -> None:
    clock = Clock()
    ps = (0.99958, 0.998565, 0.858749, 0.403043, 0.999604, 0.000359, 0.001543, 0.000262, 0.131102, 0.001863)
    local = SharedQwen(clock, lambda call, index, now: (0.0, False), ps)
    found = probe(local, clock)
    assert found["version"] == probes.QWEN_PROBE_VERSION == "pq2" and found["model"] == "qwen-shared"
    assert found["tries"] == 1 and found["spread"] == 0.0 and clock.slept == []
    assert [one["p"] for one in found["answers"]] == [round(p, 6) for p in ps]  # type: ignore[index, union-attr]
    assert [index for _, index in local.asked] == [index for index in range(10) for _ in (0, 1)]  # 每道连问两遍


def test_a_try_stops_at_the_first_pair_that_does_not_agree() -> None:
    """有人在用时只第 4 道的第二遍挪了 0.01：每回问到那儿就收掉（8 次），不把 10 道跑完；隔 30 秒再量，空了就量到。"""
    clock = Clock()
    local = SharedQwen(clock, lambda call, index, now: (0.01, False) if now < 50 and index == 3 and call % 2 == 0
                       else (0.0, False))
    found = probe(local, clock)
    assert found["tries"] == 3 and clock.slept == [30.0, 30.0]
    assert len(local.asked) == 8 + 8 + 20
    assert [one["p"] for one in found["answers"]] == [0.9] * 10  # type: ignore[index, union-attr]


def test_a_changed_first_character_alone_does_not_agree() -> None:
    clock = Clock()
    local = SharedQwen(clock, lambda call, index, now: (0.0, call == 2))  # 只第一回第 1 道的第二遍回的字变了
    found = probe(local, clock)
    assert found["tries"] == 2 and len(local.asked) == 2 + 20 and clock.slept == [30.0]


def test_two_asks_agree_only_under_five_thousandths_with_the_same_character() -> None:
    assert probes.QWEN_AGAIN_TOLERANCE == 0.005
    one = {"p": 0.9, "first": "是"}
    assert probes.qwen_steady(one, {"p": 0.9049, "first": "是"})
    assert probes.qwen_steady(one, {"p": 0.8951, "first": "是"})
    assert not probes.qwen_steady(one, {"p": 0.905, "first": "是"})  # 正好差 0.005：对不上
    assert not probes.qwen_steady(one, {"p": 0.9, "first": "否"})
    assert probes.qwen_steady({"p": None, "first": "嗯"}, {"p": None, "first": "嗯"})
    assert not probes.qwen_steady({"p": None, "first": "嗯"}, {"p": 0.9, "first": "嗯"})


def test_busy_then_idle_waits_and_measures_the_idle_numbers() -> None:
    clock = Clock()
    ps = (0.9, 0.85, 0.6, 0.45, 0.97, 0.02, 0.01, 0.003, 0.13, 0.004)
    local = SharedQwen(clock, jitter(3, until=100), ps)
    found = probe(local, clock)
    assert found["tries"] > 1 and found["spread"] == 0.0 and set(clock.slept) == {30.0}
    assert [one["p"] for one in found["answers"]] == list(ps)  # type: ignore[index, union-attr]
    assert len(local.asked) < 20 * found["tries"]  # type: ignore[operator]  # 有人在用的那几回都没把 10 道跑完


def test_always_busy_is_not_measured_after_five_minutes() -> None:
    clock = Clock()
    local = SharedQwen(clock, jitter(7))
    found = probe(local, clock)
    assert not versions._measured(found) and "busy" in found
    assert "像是一直有别人在用" in found["busy"]  # type: ignore[operator]
    assert found["tries"] == len(clock.slept) + 1 and set(clock.slept) == {30.0}
    assert found["waited"] <= 300 < found["waited"] + 30  # type: ignore[operator]  # 再等一回就过 5 分钟了，才停
    assert local.asked[-1][0] < found["waited"]  # type: ignore[operator]  # 停了就不再问
    assert len(local.asked) < 20 * found["tries"]  # type: ignore[operator]


def test_waiting_is_said_before_each_retry() -> None:
    clock = Clock()
    local = SharedQwen(clock, lambda call, index, now: (0.01, False) if now < 50 and index == 3 and call % 2 == 0
                       else (0.0, False))
    said: list[str] = []
    probes.qwen_probe(local, sleep=clock.sleep, clock=clock, say=said.append)
    assert len(said) == 2 and all("第 4 道问两遍对不上" in line and "30 秒后从头再量" in line for line in said)
    assert "第 2 回" in said[0] and "第 3 回" in said[1]


def test_the_command_says_it_is_waiting_on_stderr_only(data_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    """要等的时候 javmem versions 打一句到 stderr（最多等 5 分钟，别当它卡住了）；--json 的输出照旧只有 JSON。"""

    def run_probes(env, record, embedder=None, local=None, say=None):  # noqa: ANN001, ANN202
        say("本机 Qwen 探针第 1 道问两遍对不上，像是有别人在用：30 秒后从头再量（第 2 回，最多等 300 秒）")
        return measured()

    monkeypatch.setattr(versions, "run_probes", run_probes)
    assert main(["versions", "--json"]) == 0
    captured = capsys.readouterr()
    assert "30 秒后从头再量" in captured.err and "30 秒后从头再量" not in captured.out
    assert json.loads(captured.out)["probes"]["qwen"]["version"] == "pq2"


def test_run_probes_asks_qwen_without_the_prompt_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[dict[str, object]] = []

    class Local(FakeLocal):
        def __init__(self, url: str, model: str, *, prompt_cache: bool = True, timeout: float = 600.0) -> None:
            super().__init__(name=model)
            made.append({"url": url, "prompt_cache": prompt_cache})

    monkeypatch.setattr(preb, "LocalModel", Local)
    url = "http://127.0.0.1:8180/v1/chat/completions"
    found = versions.run_probes({"JAVMEM_JUDGE_URL": url}, None, embedder=FakeEmbeddings())
    assert made == [{"url": url, "prompt_cache": False}]  # 关掉提示缓存：缓存里有人在用时算歪的那段不会接着用
    assert found["qwen"]["version"] == "pq2" and found["qwen"]["tries"] == 1  # type: ignore[index]


def test_each_ask_goes_into_the_token_ledger_with_its_try(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.csv"
    clock = Clock()
    local = SharedQwen(clock, lambda call, index, now: (0.01, False) if call == 2 else (0.0, False))
    rows: list[object] = []
    probes.qwen_probe(local, rows.append, sleep=clock.sleep, clock=clock)
    record = versions._probe_recorder(ledger, "t")
    for row in rows:
        record(row)  # type: ignore[misc]
    written = list(TokenLedger(ledger).rows())
    assert [(row["window_id"], row["attempt"]) for row in written] == [("probe#0", "1")] * 2 + [
        (f"probe#{index}", "2") for index in range(10) for _ in (0, 1)]


# ---------- 这回没量到：不算变了 ----------


def with_busy_qwen() -> dict[str, object]:
    return {**measured(), "qwen": dict(BUSY)}


def test_a_busy_qwen_is_not_measured_and_not_a_difference() -> None:
    assert not versions._measured(BUSY)
    detail = versions.probe_changes({"probes": measured()}, {"probes": with_busy_qwen()})
    different, notes = detail["qwen"]
    assert different == [] and len(notes) == 1
    assert "探针 qwen：这回没量到（等了 300 秒，一直有别人在用）" in notes[0] and "不算不一样" in notes[0]
    assert detail["embedding"] == ([], [])
    # 基准里也没量到的，照样只说这回没量到
    assert versions.probe_changes({"probes": with_busy_qwen()}, {"probes": with_busy_qwen()})["qwen"][0] == []


def test_check_with_a_busy_qwen_exits_zero_and_records_it(data_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    fake_probes(monkeypatch, measured(), with_busy_qwen())
    assert main(["versions", "--save"]) == 0
    assert main(["versions", "--check", "--only", "probes,served,models"]) == 0
    out = capsys.readouterr().out
    assert "探针 qwen：这回没量到（等了 300 秒，一直有别人在用）" in out and "不一样的" not in out
    probe_entry = status_of(data_dir)["sections"]["probes"]
    assert probe_entry["different"] == 0 and probe_entry["probes"] == {"embedding": 0, "qwen": 0}
    assert probe_entry["busy"] == ["qwen"] and probe_entry["unmeasured"] == [] and probe_entry["no_baseline"] == []


def test_saving_while_busy_saves_nothing(data_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    """重存基准也照“两遍对得上”：对不上就不存，报出来（退出码 2），原来那一份不动。"""
    fake_probes(monkeypatch, with_busy_qwen(), measured(), with_busy_qwen())
    assert main(["versions", "--save"]) == 2
    out = capsys.readouterr().out
    assert "探针 qwen：这回没量到（等了 300 秒，一直有别人在用）" in out and "没存" in out
    assert not base_of(data_dir).exists()
    assert main(["versions", "--save"]) == 0  # 空的时候存上了
    before = base_of(data_dir).read_bytes()
    assert main(["versions", "--save"]) == 2  # 又有人在用：旧的那一份不动
    assert base_of(data_dir).read_bytes() == before


def test_patrol_says_not_measured_this_time_not_changed(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.intake()
    fake_probes(monkeypatch, measured(), with_busy_qwen(), measured())
    assert main(["versions", "--save"]) == 0
    assert main(["versions", "--check", "--only", "probes,served,models"]) == 0
    report = patrol_report(world.settings)
    assert [item.kind for item in report.items if item.kind.startswith("probe_")] == []  # 不当成变了
    [skip] = [item for item in report.skipped if item.reason == "probe_busy"]
    assert skip.kind == "checks_skipped" and skip.skipped == ("probe_changed",) and skip.library is None
    assert "本机 Qwen" in skip.message and "这回没量到" in skip.message
    assert "javmem versions --check --only probes,served,models" in skip.message
    assert main(["versions", "--check", "--only", "probes,served,models"]) == 0  # 空的时候再量：量到了就不说了
    assert [item.reason for item in patrol_report(world.settings).skipped if item.reason == "probe_busy"] == []


# ---------- 旧版探针存的基准：要重存，不是变了 ----------


def test_an_old_probe_baseline_says_save_again_not_changed(world: World, monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    world.intake()
    fake_probes(monkeypatch, measured(), measured(), measured())
    assert main(["versions", "--save"]) == 0
    path = base_of(world.data)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["probes"]["qwen"]["version"] = "pq1"  # 合进来以前存的那一份
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert main(["versions", "--check", "--only", "probes,served,models"]) == 0
    out = capsys.readouterr().out
    assert "探针 qwen：基准是旧版探针（pq1）存的，这次是 pq2，比不了：要重存" in out and "不一样的" not in out
    probe_entry = status_of(world.data)["sections"]["probes"]
    assert probe_entry["old_version"] == ["qwen"] and probe_entry["different"] == 0
    [item] = [one for one in patrol_report(world.settings).items if one.kind == "probe_changed"]
    assert "基准里本机 Qwen 是旧版探针存的，要重存" in item.message and "javmem versions --save" in item.message
    assert "不一样" not in item.message and "变" not in item.message  # 不报成变了
    assert item.stats["old_version"] == ["qwen"] and item.stats["different"] == 0  # type: ignore[index]
    assert main(["versions", "--save"]) == 0  # 重存了就不报了
    assert [one.kind for one in patrol_report(world.settings).items if one.kind.startswith("probe_")] == []


def test_an_old_baseline_is_noted_by_the_version_not_the_numbers() -> None:
    old = measured()
    old["qwen"] = {**old["qwen"], "version": "pq1", "answers": [{"p": 0.1, "first": "否"}] * 10}  # type: ignore[dict-item]
    different, notes = versions.probe_changes({"probes": old}, {"probes": measured()})["qwen"]
    assert different == [] and notes == ["探针 qwen：基准是旧版探针（pq1）存的，这次是 pq2，比不了：要重存（javmem versions --save）"]
