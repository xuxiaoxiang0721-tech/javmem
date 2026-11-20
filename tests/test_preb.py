"""预处理 B（认人认事，b4）：名单、代码核一遍、标注写进窗口、存进结果库能重放、收名字、可见范围、撤回连着删、
一个包一个包先标再交给 L3（L3 两路）。只用假模型，例子都是编的。"""

from __future__ import annotations

import csv
import json
import math
import re
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import build_packet, file_name

from javmem.config import ConfigError
from javmem.llm import FakeModel, ModelReply, Usage
from javmem.packet import parse_packet
from javmem.pipeline.extract import DONE, ExtractOptions, map_output, prepare, run_extraction, window_row
from javmem.pipeline.normalize import Scope, normalize
from javmem.pipeline.preb import (
    PRE_B_VERSION,
    Rosters,
    Item,
    LocalModel,
    PreB,
    PreBSettings,
    Roster,
    apply_notes,
    looks_like_account,
    packet_groups,
    window_context,
)
from javmem.pipeline.render import RenderedWindow, render_window
from javmem.pipeline.store import ResultStore
from javmem.pipeline.verify import verify_windows
from javmem.pipeline.window import Window, build_windows
from javmem.redaction import default_patterns
from javmem.usage import TokenLedger
from javmem.zh.lexicon import Lexicon, load_lexicon

PATTERNS = default_patterns()
OPTIONS = ExtractOptions(sleep=lambda seconds: None, concurrency=1)
LEXICON_YAML = """format: javmem-lexicon/2
aliases:
  - type: 人
    names: ["岑小燕", "小岑"]
  - type: 订单或合同
    names: ["X1 FCN（东岸证券）", "X1 那张"]
not_entities:
  names: ["累购"]
"""


def windows_of(*raws: str) -> list[Window]:
    return build_windows([normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw))) for raw in raws])


def chat_packet(stamp: str, text: str, **fields: object) -> str:
    return build_packet(stamp=stamp, body=f"## 原话 · 10:30–10:31\n- [10:30] 我：{text}\n", **fields)


def lexicon_from(tmp_path: Path, text: str, name: str = "lexicon.yaml") -> Lexicon:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return load_lexicon(path)


def lexicon(tmp_path: Path) -> Lexicon:
    return lexicon_from(tmp_path, LEXICON_YAML)


@pytest.fixture
def store(tmp_path: Path) -> ResultStore:
    with ResultStore(tmp_path / "l3") as opened:
        yield opened


Script = list[dict[str, str]] | Callable[[str], list[dict[str, str]]]


class FakeLocal:
    """假的本机模型。提的时候回 proposals，收名字的时候回 learns（列表，或者按发过来的文字给的函数）。问“选哪一项”的时候：
    picks 里写了这个说法的照 picks 选（写 X 就是选“都不是”），没写的选它提过的那一项，都没有的选 X；把握 p，剩下的给别的。
    calls 记着每次的 (发过来的文字, 哪一种：propose、learn、choose)，asked 记着每次问的说法和给的几项。"""

    name = "fake-qwen"

    def __init__(self, proposals: Script = (), p: float = 0.95, picks: dict[str, str] | None = None,
                 learns: Script = (), events: list[str] | None = None) -> None:
        self.proposals = proposals
        self.learns = learns
        self.p = p
        self.picks = dict(picks or {})
        self.calls: list[tuple[str, str]] = []
        self.asked: list[tuple[str, list[str]]] = []
        self.events = events
        self.lock = threading.Lock()

    def _script(self, script: Script, user: str) -> list[dict[str, str]]:
        return list(script(user) if callable(script) else script)

    def chat(self, system: str, user: str, *, max_tokens: int, schema: object = None, logprobs: bool = False):
        if schema is not None:
            key = schema["required"][0]  # type: ignore[index]
            kind = "propose" if key == "标注" else "learn"
            with self.lock:
                self.calls.append((user, kind))
                if self.events is not None:
                    self.events.append(f"{kind}:{user.rsplit('【窗口】', 1)[-1][:200]}")
            found = self._script(self.proposals if kind == "propose" else self.learns, user)
            content = json.dumps({key: found}, ensure_ascii=False)
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20}}, 0.01
        with self.lock:
            self.calls.append((user, "choose"))
        tail = user.rsplit("【要核的】", 1)[-1]
        mention = tail.split("“", 1)[1].split("”", 1)[0]
        options = dict(re.findall(r"^([A-G])\. (.+?)〔", tail, re.MULTILINE))
        self.asked.append((mention, list(options.values())))
        proposed = self._script(self.proposals, user)
        wanted = self.picks.get(mention) or next((one["名字"] for one in proposed if one["说法"] == mention), None)
        letter = "X" if wanted == "X" else next((label for label, name in options.items() if name == wanted), "X")
        top = [{"token": letter, "logprob": math.log(self.p)},
               {"token": "A" if letter == "X" else "X", "logprob": math.log(max(1e-9, 1 - self.p))}]
        return {"choices": [{"message": {"content": letter}, "logprobs": {"content": [{"token": letter, "top_logprobs": top}]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 1}}, 0.01

    def kinds(self) -> list[str]:
        return [kind for _, kind in self.calls]


def fact_reply(ref: str, quote: str, name: str, mention: str):
    """假 L3 模型的一次回复（FakeModel 的剧本里放可调用的）：一个订单或合同实体、一条用到它的事实。"""
    body = {
        "entities": [{"key": "e1", "mention": mention, "name": name, "type": "订单或合同"}],
        "facts": [{"text": f"{name} 下周付息", "subject": "e1", "attribute": None, "value": None, "occurred_at": None,
                   "time_precision": "unknown", "certainty": "stated", "speaker": "主人",
                   "entities": [{"key": "e1", "role": "subject"}], "evidence": [{"ref": ref, "quote": quote}]}],
        "relations": [],
    }
    reply = ModelReply(json.dumps(body, ensure_ascii=False), "stop", Usage(100, 50), "demo", 0.1)
    return lambda system, user: reply


def view_of(lex: Lexicon, window: Window, store: ResultStore | None = None) -> Roster:
    if store is None:
        return Roster(lex, partitions=window.scope.partitions)
    return Rosters(store, lex).view(window.scope)


def learn_entry(ref: str, mention: str, name: str, cls: str) -> dict[str, str]:
    return {"行": ref, "说法": mention, "名字": name, "类": cls}


# ---------- 标注写进窗口、存下来重放 ----------


def test_apply_notes_goes_after_the_number_and_keeps_shown() -> None:
    window = windows_of(chat_packet("2026-11-08_2330", "小岑说那张下个月到期。"))[0]
    rendered = render_window(window, PATTERNS)
    ref = window.targets[0].ref
    noted = apply_notes(rendered, {ref: [["小岑", "岑小燕"], ["那张", "X1 FCN（东岸证券）"]]})
    assert f"{ref} 〔指：小岑 = 岑小燕；那张 = X1 FCN（东岸证券）〕小岑说那张下个月到期。" in noted.text
    assert noted.shown == rendered.shown  # L4 比引文用的原文不变
    assert noted.sha256 != rendered.sha256
    assert apply_notes(rendered, {}) is rendered


def test_apply_notes_after_an_existing_label_and_escaped() -> None:
    rendered = RenderedWindow("来源 x\n【正文】\nT1 〔引用〕原文一句\nT2 别的\n", "", {}, (), (), {"T1": "原文一句", "T2": "别的"})
    noted = apply_notes(rendered, {"T1": [["原〔文", "名〕字"]]})
    assert "T1 〔引用〕〔指：原［文 = 名］字〕原文一句\nT2 别的\n" in noted.text  # 〔〕换成［］，伪造不了别的标注


def save_notes(store: ResultStore, window: Window, notes: dict[str, list[list[str]]], item_ids=()) -> RenderedWindow:
    rendered = render_window(window, PATTERNS)
    noted = apply_notes(rendered, notes)
    store.save_preb_notes(
        {"window_id": window.window_id, "window_sha": rendered.sha256, "version": PRE_B_VERSION,
         "context": window_context(window), "noted_sha": noted.sha256,
         "packets": json.dumps([list(one) for one in window.packets]),
         "sentences": json.dumps(sorted({line.sentence_id for line in window.lines})),
         "notes": json.dumps(notes, ensure_ascii=False), "detail": "{}", "seconds": 0.0, "created_at": "2026-11-16T00:00:00+08:00"},
        item_ids,
    )
    return noted


def test_prepare_replays_saved_notes(store: ResultStore) -> None:
    windows = windows_of(chat_packet("2026-11-08_2330", "小岑说那张下个月到期。"))
    plain = prepare(windows, FakeModel(), PATTERNS, store=store)
    assert plain[0][2] == prepare(windows, FakeModel(), PATTERNS)[0][2]  # 没标过：和不给结果库一样
    save_notes(store, windows[0], {windows[0].targets[0].ref: [["小岑", "岑小燕"]]})
    noted = prepare(windows, FakeModel(), PATTERNS, store=store)
    assert "〔指：小岑 = 岑小燕〕" in noted[0][1].text
    assert noted[0][2] != plain[0][2]  # 键跟着发出去的文字
    assert prepare(windows, FakeModel(), PATTERNS, store=store)[0][2] == noted[0][2]  # 重放一字不差
    assert prepare(windows, FakeModel(), PATTERNS, store=store, prompt="l3-v16")[0][2] != noted[0][2]  # 提示词也在键里


def test_quotes_lose_stacked_labels() -> None:
    window = windows_of(chat_packet("2026-11-08_2330", "小岑说那张下个月到期。"))[0]
    ref = window.targets[0].ref
    parsed = {
        "entities": [{"key": "e1", "mention": "小岑", "name": "岑小燕", "type": "人"}],
        "facts": [{"text": "x", "subject": "e1", "attribute": None, "value": None, "occurred_at": None,
                   "time_precision": "unknown", "certainty": "stated", "speaker": "主人",
                   "entities": [{"key": "e1", "role": "subject"}],
                   "evidence": [{"ref": ref, "quote": f"{ref} 〔引用〕〔指：小岑 = 岑小燕〕小岑说那张下个月到期"}]}],
        "relations": [],
    }
    assert map_output(parsed, window)["facts"][0]["evidence"][0]["quote"] == "小岑说那张下个月到期"


# ---------- 代码核一遍（b2、b2.1、b3） ----------


def test_validate_keeps_only_checkable_candidates(tmp_path: Path) -> None:
    window = windows_of(chat_packet("2026-11-08_2330", "小岑说那张和累购下个月都到期。"))[0]
    rendered = render_window(window, PATTERNS)
    ref = window.targets[0].ref
    lex = lexicon(tmp_path)
    pre_b = PreB(FakeLocal(), lex)
    proposed = [
        {"行": ref, "说法": "小岑说", "名字": "岑小燕"},
        {"行": ref, "说法": "那张", "名字": "X1 那张"},  # 写成了别名：换成名字
        {"行": ref, "说法": "小岑", "名字": "岑小燕"},  # 名单上原样有的叫法，系统认得
        {"行": ref, "说法": "老吴", "名字": "岑小燕"},
        {"行": ref, "说法": "累购", "名字": "X1 FCN（东岸证券）"},
        {"行": ref, "说法": "小岑说", "名字": "岑小燕"},
        {"行": "T99", "说法": "小岑说", "名字": "岑小燕"},
        {"行": ref, "说法": "那张", "名字": "不认识的"},
    ]
    kept, dropped = pre_b.validate(view_of(lex, window), window, rendered, proposed)
    assert [(one["mention"], one["name"], one["auto"]) for one in kept] == [
        ("小岑说", "岑小燕", False), ("那张", "X1 FCN（东岸证券）", False),
        ("小岑", "岑小燕", True),  # b2：名单上原样有的叫法、只对得上一项，放过去、不用再问
    ]
    assert [one["为什么不要"] for one in dropped] == ["说法不在那一行原文里", "泛称", "重复", "不是 T 行", "名字不在名单上"]


def test_run_extraction_with_pre_b_marks_then_extracts(tmp_path: Path, store: ResultStore) -> None:
    windows = windows_of(chat_packet("2026-11-08_2330", "岑姐说那张下个月到期。"))
    ref = windows[0].targets[0].ref
    local = FakeLocal([{"行": ref, "说法": "岑姐", "名字": "岑小燕"}, {"行": ref, "说法": "那张", "名字": "X1 FCN（东岸证券）"}])
    ledger = TokenLedger(tmp_path / "ledger.csv")
    pre_b = PreB(local, lexicon(tmp_path), ledger=ledger, tag="t")
    fake = FakeModel()
    result = run_extraction(windows, fake, store, patterns=PATTERNS, ledger=ledger, options=OPTIONS, pre_b=pre_b)
    assert result.outcomes[0].status == DONE
    assert "〔指：岑姐 = 岑小燕；那张 = X1 FCN（东岸证券）〕岑姐说那张下个月到期。" in fake.calls[0][1]
    checked = verify_windows(windows, store, fake, PATTERNS)
    assert checked[0].extraction is not None  # 核对、写账算出来的键和抽的时候一样
    with ledger.path.open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["tag"] == "t-b4"]
    assert sorted(row["purpose"] for row in rows) == ["pre_b_choose", "pre_b_choose", "pre_b_learn", "pre_b_propose"]
    assert {row["model"] for row in rows} == {"fake-qwen"}
    assert local.kinds() == ["learn", "propose", "choose", "choose"]
    # 再跑一遍：收过名字、标注存过、结果存过，本机模型和 L3 都不再调
    again = run_extraction(windows, fake, store, patterns=PATTERNS, ledger=ledger, options=OPTIONS,
                           pre_b=PreB(local, lexicon(tmp_path), ledger=ledger, tag="t"))
    assert again.outcomes[0].status != DONE and len(local.calls) == 4 and len(fake.calls) == 1


def test_unsure_candidates_are_not_marked(tmp_path: Path, store: ResultStore) -> None:
    windows = windows_of(chat_packet("2026-11-08_2330", "小岑说那张下个月到期。"))
    ref = windows[0].targets[0].ref
    pre_b = PreB(FakeLocal([{"行": ref, "说法": "那张", "名字": "X1 FCN（东岸证券）"}], p=0.6), lexicon(tmp_path))
    fake = FakeModel()
    run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=pre_b)
    assert "〔指：" not in fake.calls[0][1]  # 把握不到 0.9 不标


def test_listed_alias_is_marked_without_asking(tmp_path: Path, store: ResultStore) -> None:
    """b2：原话里原样写着名单上的叫法（小岑）、只对得上一项的，直接标，不再问本机模型。"""
    windows = windows_of(chat_packet("2026-11-08_2330", "小岑说那张下个月到期。"))
    ref = windows[0].targets[0].ref
    local = FakeLocal([{"行": ref, "说法": "小岑", "名字": "岑小燕"}, {"行": ref, "说法": "那张", "名字": "X1 FCN（东岸证券）"}])
    fake = FakeModel()
    run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(local, lexicon(tmp_path)))
    assert "〔指：小岑 = 岑小燕；那张 = X1 FCN（东岸证券）〕小岑说那张下个月到期。" in fake.calls[0][1]
    assert local.kinds() == ["learn", "propose", "choose"]  # 只问了“那张”那一条


# ---------- b2 防合错：日子、编号、金额对不上的，名字再像也不是同一样东西 ----------

TWINS_YAML = """format: javmem-lexicon/2
aliases:
  - type: 订单或合同
    names: ["X3 累购（7/14）", "X3 7/14 AQ", "X3 累购"]
  - type: 订单或合同
    names: ["X3 累购（7/19）", "X3 7/19 AQ"]
  - type: 订单或合同
    names: ["Y1 FCN（XS1111111111）", "XS1111111111"]
  - type: 订单或合同
    names: ["Y1 FCN（XS2222222222）", "XS2222222222"]
  - type: 订单或合同
    names: ["Z1 单子（36 万）", "Z1 36 万那张"]
  - type: 订单或合同
    names: ["Z1 单子（58 万）", "Z1 58 万那张"]
"""


def twins(tmp_path: Path) -> Lexicon:
    return lexicon_from(tmp_path, TWINS_YAML, "twins.yaml")


def checked_one(tmp_path: Path, text: str, proposed: list[dict[str, str]]):
    window = windows_of(chat_packet("2026-11-08_2330", text))[0]
    lex = twins(tmp_path)
    pre_b = PreB(FakeLocal(), lex)
    ref = window.targets[0].ref
    blocked: list[dict[str, object]] = []
    kept, dropped = pre_b.validate(view_of(lex, window), window, render_window(window, PATTERNS),
                                   [{"行": ref, **one} for one in proposed], blocked)
    return blocked, kept, dropped


def test_values_read_dates_codes_amounts_the_same_way() -> None:
    from javmem.pipeline.preb_keys import values_of

    for text in ("7/14", "07-14", "7月14日", "2026-07-14", "2026年7月14日", "14 Jul", "Jul 14"):
        assert values_of(f"X3 累购 {text}")["日子"] == {"07-14"}, text
    assert values_of("Z1 单子 470 万")["金额"] == values_of("Z1 4.7M 的单子")["金额"] == {"4700000"}
    assert values_of("Y1（XS1111111111）")["编号"] == {"XS1111111111"}
    assert not any(values_of("Marvelous 25%，屏障 50%/12.86%").values())  # 百分比、Mar-velous 都不算


def test_only_values_stuck_to_the_mention_count() -> None:
    from javmem.pipeline.preb_keys import values_near

    assert values_near("那张 7/19 的 X3 累购今天敲出了。", "X3 累购")["日子"] == {"07-19"}
    assert not any(values_near("X3 累购 9/15 到期，付 1.2 万。", "X3 累购").values())  # 事件的日子、金额不算
    assert values_near("Z1 那张 58 万的单子到期了。", "Z1 那张")["金额"] == {"580000"}
    assert values_near("Y1 那张（XS2222222222）下周付息。", "Y1 那张")["编号"] == {"XS2222222222"}
    assert not any(values_near("X3 累购（7/14）和 Y1 那张都要看。", "Y1 那张").values())  # 隔着别的字的不算


def test_mark_with_a_different_date_is_blocked(tmp_path: Path) -> None:
    blocked, kept, dropped = checked_one(tmp_path, "那张 7/19 的 X3 累购今天敲出了。", [
        {"说法": "X3 累购", "名字": "X3 累购（7/14）"},
        {"说法": "X3 累购", "名字": "X3 累购（7/19）"},
    ])
    assert [(one["name"], one["auto"]) for one in kept] == [("X3 累购（7/19）", False)]
    assert [one["为什么不要"] for one in dropped] == ["日子、编号、金额对不上"]
    row = blocked[0]
    assert (row["mention"], row["item"], row["kind"], row["mention_values"], row["item_values"], row["stage"]) == (
        "X3 累购", "X3 累购（7/14）", "日子", '["07-19"]', '["07-14"]', "标注")


def test_mark_with_a_different_code_or_amount_is_blocked(tmp_path: Path) -> None:
    _, kept, dropped = checked_one(tmp_path, "Y1 那张（XS2222222222）下周付息。", [
        {"说法": "Y1 那张", "名字": "Y1 FCN（XS1111111111）"}, {"说法": "Y1 那张", "名字": "Y1 FCN（XS2222222222）"}])
    assert [one["name"] for one in kept] == ["Y1 FCN（XS2222222222）"] and len(dropped) == 1
    blocked, kept, dropped = checked_one(tmp_path, "Z1 那张 58 万的单子到期了。", [
        {"说法": "Z1 那张", "名字": "Z1 单子（36 万）"}, {"说法": "Z1 那张", "名字": "Z1 单子（58 万）"}])
    assert [one["name"] for one in kept] == ["Z1 单子（58 万）"]
    assert [(row["kind"], row["mention_values"], row["item_values"]) for row in blocked] == [("金额", '["580000"]', '["360000"]')]


def test_same_date_written_differently_is_not_blocked(tmp_path: Path) -> None:
    for text in ("X3 累购（07-14）今天敲出了。", "2026年7月14日的 X3 累购今天敲出了。", "X3 累购（14 Jul）今天敲出了。"):
        blocked, kept, dropped = checked_one(tmp_path, text, [{"说法": "X3 累购", "名字": "X3 累购（7/14）"}])
        assert [one["name"] for one in kept] == ["X3 累购（7/14）"] and not dropped and not blocked, text
        assert kept[0]["auto"]  # 说法带着分得开两张的日子：只对得上一项，直接标


def test_dateless_name_of_dated_twins_is_asked_not_marked_directly(tmp_path: Path) -> None:
    """防合错 c：“X3 累购”名单上只是 7/14 那张的叫法，可 7/14、7/19 两张只差日子，不带日子分不出：照旧问。"""
    blocked, kept, _ = checked_one(tmp_path, "X3 累购今天敲出了。", [{"说法": "X3 累购", "名字": "X3 累购（7/14）"}])
    assert [(one["name"], one["auto"]) for one in kept] == [("X3 累购（7/14）", False)]
    assert blocked[0]["kind"] == "几项只差日子、编号、金额"
    assert json.loads(str(blocked[0]["item_values"])) == ["X3 累购（7/14）", "X3 累购（7/19）"]


def test_blocked_rows_land_in_the_store(tmp_path: Path, store: ResultStore) -> None:
    windows = windows_of(chat_packet("2026-11-08_2330", "那张 7/19 的 X3 累购今天敲出了。"))
    ref = windows[0].targets[0].ref
    local = FakeLocal([{"行": ref, "说法": "X3 累购", "名字": "X3 累购（7/14）"}])
    run_extraction(windows, FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(local, twins(tmp_path)))
    rows = [(row["ref"], row["mention"], row["item"], row["kind"], row["mention_values"], row["item_values"], row["stage"],
             row["version"]) for row in store.preb_blocked(PRE_B_VERSION)]
    assert rows == [(ref, "X3 累购", "X3 累购（7/14）", "日子", '["07-19"]', '["07-14"]', "标注", "b4")]
    # 拦下的不再问本机模型：“那张 7/19 的 X3 累购”这个单子说法照样拿去选，可 7/14 那张不在给它选的几项里
    assert local.asked and all("X3 累购（7/14）" not in options for _, options in local.asked)
    assert "X3 累购（7/19）" in local.asked[0][1]


def test_short_whole_number_with_m_is_not_an_amount(tmp_path: Path) -> None:
    """“6M”“12m”多半是期限（6 个月）或者 3M 这家公司，不算金额；1.2M、USD 5M、5 million、500k、470 万照旧算。"""
    from javmem.pipeline.preb_keys import values_of

    for text in ("X1 FCN 6M", "12m 期限", "3M 公司"):
        assert not values_of(text)["金额"], text
    assert values_of("1.2M")["金额"] == {"1200000"}
    assert values_of("USD 5M")["金额"] == values_of("5 million")["金额"] == {"5000000"}
    assert values_of("500k")["金额"] == {"500000"} and values_of("470 万")["金额"] == {"4700000"}
    lex = lexicon_from(tmp_path, 'format: javmem-lexicon/2\naliases:\n  - type: 订单或合同\n    names: ["X1 FCN 6M", "X1 6M FCN"]\n')
    window = windows_of(chat_packet("2026-11-08_2330", "X1 FCN 47万 下单了。"))[0]
    blocked: list[dict[str, object]] = []
    kept, dropped = PreB(FakeLocal(), lex).validate(view_of(lex, window), window, render_window(window, PATTERNS),
                                                    [{"行": window.targets[0].ref, "说法": "X1 FCN", "名字": "X1 FCN 6M"}], blocked)
    assert [one["name"] for one in kept] == ["X1 FCN 6M"] and not dropped and not blocked  # 不拦


# ---------- b2.1：不标邮件地址、账号 ----------


def test_email_and_account_mentions_are_not_marked(tmp_path: Path) -> None:
    window = windows_of(chat_packet("2026-11-08_2330", "抄送 cen.xiaoyan@east-bank.example 和 cen.xy，另外 cen_xy2 也加上。"))[0]
    lex = lexicon(tmp_path)
    ref = window.targets[0].ref
    kept, dropped = PreB(FakeLocal(), lex).validate(view_of(lex, window), window, render_window(window, PATTERNS), [
        {"行": ref, "说法": mention, "名字": "岑小燕"} for mention in ("cen.xiaoyan@east-bank.example", "cen.xy", "cen_xy2")])
    assert not kept
    assert [one["为什么不要"] for one in dropped] == ["邮箱、账号"] * 3


def test_tickers_abbreviations_and_chat_mentions_are_not_accounts(tmp_path: Path) -> None:
    assert not any(looks_like_account(text) for text in ("EBK.B", "e.g.", "U.S.", "@小岑", "小岑", "Sally Cen", "east-notes.html"))
    lex = lexicon_from(tmp_path, 'format: javmem-lexicon/2\naliases:\n  - type: 资产\n    names: ["东岸 B 股", "EBK.B"]\n')
    window = windows_of(chat_packet("2026-11-08_2330", "EBK.B 今天涨了，@小岑 看一下。"))[0]
    kept, dropped = PreB(FakeLocal(), lex).validate(view_of(lex, window), window, render_window(window, PATTERNS),
                                                    [{"行": window.targets[0].ref, "说法": "EBK.B", "名字": "东岸 B 股"}])
    assert [one["name"] for one in kept] == ["东岸 B 股"] and not dropped


# ---------- b3：先挑候选、只跟同类比 ----------

DEALS_YAML = """format: javmem-lexicon/2
aliases:
  - type: 资产
    names: ["甲科技", "JKJ", "Jiake"]
  - type: 资产
    names: ["乙数据", "YSJ", "Yishu"]
  - type: 资产
    names: ["丙能源", "BNY", "Bingyuan"]
  - type: 公司或机构
    names: ["东岸证券", "东岸", "East Bank"]
  - type: 订单或合同
    names: ["JKJ+YSJ FCN（东岸证券）", "JKJ+YSJ"]
    from_types: ["资产"]
  - type: 订单或合同
    names: ["JKJ+BNY FCN（东岸证券）", "JKJ+BNY"]
  - type: 订单或合同
    names: ["YSJ 累购（9/03）", "YSJ 9/03 AQ"]
  - type: 订单或合同
    names: ["YSJ 累购（9/07）", "YSJ 9/07 AQ"]
  - type: 订单或合同
    names: ["BNY 累购", "BNY AQ"]
  - type: 订单或合同
    names: ["X1 FCN（东岸证券）", "X1 那张"]
  - type: 人
    names: ["岑小燕", "小岑"]
"""


def deals(tmp_path: Path) -> Lexicon:
    return lexicon_from(tmp_path, DEALS_YAML, "deals.yaml")


class BagEmbeddings:
    """假的向量服务：每两个相邻的字（大小写不分）散到一维上，字越像向量越像。不连网。"""

    name = "fake-bag"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        import hashlib

        from javmem.embedding import EMBEDDING_DIM, EmbeddingReply, pack_vector

        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            values = [0.0] * EMBEDDING_DIM
            folded = text.casefold()
            for at in range(len(folded) - 1):
                values[int(hashlib.md5(folded[at : at + 2].encode("utf-8")).hexdigest(), 16) % EMBEDDING_DIM] += 1.0
            values[0] += 1e-3  # 不能全是 0
            vectors.append(pack_vector(values))
        return EmbeddingReply(vectors, len(texts), 0.0)


def check(pre_b: PreB, lex: Lexicon, text: str, proposed: list[dict[str, str]]):
    window = windows_of(chat_packet("2026-11-08_2330", text))[0]
    ref = window.targets[0].ref
    return pre_b.validate(view_of(lex, window), window, render_window(window, PATTERNS), [{"行": ref, **one} for one in proposed])


def test_deal_phrases_are_found_by_rules(tmp_path: Path) -> None:
    finder = Roster(deals(tmp_path)).deal_finder()

    def found(text: str) -> list[tuple[str, bool]]:
        return [(one.text, one.plural) for one in finder.phrases(text)]

    assert found("篮子是 Jiake + Yishu / Jiake + Bingyuan，明年再看。") == [("Jiake + Yishu", False), ("Jiake + Bingyuan", False)]
    assert found("我们还有 Jiake+YSJ 的 FCN，十月再加 45 万。") == [("Jiake+YSJ 的 FCN", False)]
    assert found("麻烦把 YSJ 这张 TERM SHEET 转给我。") == [("YSJ 这张 TERM SHEET", False)]
    assert found("YSJ 累购 9/15 到期。") == [("YSJ 累购", False)]  # 后面接着“到期”的日子是事件的，不算单子的
    assert found("9/03 的 YSJ 累购今天敲出了。") == [("9/03 的 YSJ 累购", False)]
    assert found("两张东岸固定票息票据都在观察期。") == [("东岸固定票息票据", True)]  # 说的是两张
    assert found("JKJ/YSJ 累购都停了。") == [("JKJ/YSJ 累购", True)]  # 累购一张只有一只股票：是两张
    assert found("JKJ/YSJ FCN 下周观察。") == [("JKJ/YSJ FCN", False)]  # FCN 是一篮子：一张
    for text in ("先发东岸 9/03 那张图给我。", "FCN 不算坏东西。", "Yishu 和 Jiake 两只都拿着。", "这张先放着。", "东岸的 FCN 文件还没到。",
                 "东岸 9/03 那份我还没看。", "条款看东岸的 TERM SHEET 就行。", "图上写着 FCN · YSJ 两块"):
        assert found(text) == [], text  # 图、文件、“那份”、只带公司的文件、“·”隔开的几样都不算


def test_stock_inside_a_deal_is_not_marked_and_the_whole_deal_is_chosen(tmp_path: Path, store: ResultStore) -> None:
    """“底层 Jiake + Yishu 那张”：整个说法对到单子上，里头的股票不单独标，Qwen 只在单子里选。"""
    windows = windows_of(chat_packet("2026-11-08_2330", "底层 Jiake + Yishu 那张下周观察。"))
    ref = windows[0].targets[0].ref
    local = FakeLocal([{"行": ref, "说法": "Jiake", "名字": "甲科技"}, {"行": ref, "说法": "Yishu", "名字": "乙数据"}],
                      picks={"Jiake + Yishu 那张": "JKJ+YSJ FCN（东岸证券）"})
    fake = FakeModel()
    lex = deals(tmp_path)
    pre_b = PreB(local, lex)
    run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=pre_b)
    assert "〔指：Jiake + Yishu 那张 = JKJ+YSJ FCN（东岸证券）〕底层 Jiake + Yishu 那张下周观察。" in fake.calls[0][1]
    assert [mention for mention, _ in local.asked] == ["Jiake + Yishu 那张"]
    deal_names = {item.name for item in Roster(lex).of_class("单子")}
    assert set(local.asked[0][1]) <= deal_names and "JKJ+YSJ FCN（东岸证券）" in local.asked[0][1]  # 只跟单子比
    assert pre_b.stats["单子里的股票没标"] == 2 and pre_b.stats["代码找的单子"] == 1


def test_a_deal_mention_proposed_as_a_stock_is_only_compared_with_deals(tmp_path: Path) -> None:
    window = windows_of(chat_packet("2026-11-08_2330", "Yishu 那张怎么样了？"))[0]
    ref = window.targets[0].ref
    local = FakeLocal([{"行": ref, "说法": "Yishu 那张", "名字": "乙数据"}])  # 单子的说法，Qwen 提成了股票
    lex = deals(tmp_path)
    pre_b = PreB(local, lex)
    rendered = render_window(window, PATTERNS)
    view = view_of(lex, window)
    kept, _ = pre_b.validate(view, window, rendered, local.proposals)  # type: ignore[arg-type]
    assert [(one["mention"], one["class"], one["proposed"]) for one in kept] == [("Yishu 那张", "单子", None)]
    assert pre_b._choose(view, window, rendered, "名单和窗口", kept[0]) is None
    assert local.asked[0][0] == "Yishu 那张" and "乙数据" not in local.asked[0][1]  # 股票不在给它选的几项里
    assert kept[0]["why"] == "都不是"


def test_candidates_are_the_most_alike_of_the_same_class(tmp_path: Path) -> None:
    """同一类多于 5 项的，用向量挑最像的 5 项；Qwen 提的那一项、和它只差日子的几项一定在里面。向量服务的调用记账。"""
    window = windows_of(chat_packet("2026-11-08_2330", "我在看 YSJ 累购，还有 JKJ+BNY 那张。"))[0]
    ref = window.targets[0].ref
    ledger = TokenLedger(tmp_path / "ledger.csv")
    local = FakeLocal([{"行": ref, "说法": "YSJ 累购", "名字": "YSJ 累购（9/03）"}], picks={"JKJ+BNY 那张": "JKJ+BNY FCN（东岸证券）"})
    embedder = BagEmbeddings()
    lex = deals(tmp_path)
    pre_b = PreB(local, lex, ledger=ledger, tag="t", embedder=embedder)
    rendered = render_window(window, PATTERNS)
    view = view_of(lex, window)
    kept, _ = pre_b.validate(view, window, rendered, local.proposals)  # type: ignore[arg-type]
    assert [one["mention"] for one in kept] == ["YSJ 累购", "JKJ+BNY 那张"]
    both, _ = pre_b.pick(view, window, rendered, kept[0])
    assert len(both) == 5 and {"YSJ 累购（9/03）", "YSJ 累购（9/07）"} <= {item.name for item in both}
    basket, _ = pre_b.pick(view, window, rendered, kept[1])
    assert len(basket) == 5 and basket[0].name == "JKJ+BNY FCN（东岸证券）"  # 最像的排第一
    assert pre_b._choose(view, window, rendered, "名单和窗口", kept[1]) is not None and kept[1]["name"] == "JKJ+BNY FCN（东岸证券）"
    with ledger.path.open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["purpose"] == "pre_b_embed"]
    assert rows and {row["tag"] for row in rows} == {"t-b4"} and {row["model"] for row in rows} == {"fake-bag"}
    assert all(len(batch) <= 16 for batch in embedder.calls)  # 一批最多 16 条


def test_choice_probs_keep_room_for_other_answers() -> None:
    from javmem.pipeline.preb_pick import choice_probs

    def out(*tops: tuple[str, float]):
        return {"choices": [{"logprobs": {"content": [{"token": tops[0][0], "top_logprobs": [
            {"token": token, "logprob": math.log(p)} for token, p in tops]}]}}]}

    probs = choice_probs(out(("A", 0.5), ("都", 0.4), ("B", 0.1)), ("A", "B", "X"))
    assert probs is not None and abs(probs["A"] - 0.5) < 1e-9 and abs(probs["B"] - 0.1) < 1e-9  # 答“都不是”的四成也在分母里
    assert choice_probs(out(("A", 0.6), (" A", 0.35), ("X", 0.05)), ("A", "X"))["A"] > 0.9  # 写法不同的同一个字母加起来
    assert choice_probs(out(("是", 0.9), ("否", 0.1)), ("A", "X")) is None


def test_several_deals_are_not_marked(tmp_path: Path) -> None:
    lex = deals(tmp_path)
    kept, dropped = check(PreB(FakeLocal(), lex), lex, "两张东岸固定票息票据都在观察期，JKJ/YSJ 累购都停了。",
                          [{"说法": "东岸固定票息票据", "名字": "X1 FCN（东岸证券）"}])
    assert not kept and [one["为什么不要"] for one in dropped] == ["说的是几张单子"]


def test_one_mention_with_two_deals_is_split_into_two(tmp_path: Path) -> None:
    lex = deals(tmp_path)
    kept, dropped = check(PreB(FakeLocal(), lex), lex, "篮子是 Jiake + Yishu / Jiake + Bingyuan，明年再看。",
                          [{"说法": "Jiake + Yishu / Jiake + Bingyuan", "名字": "JKJ+YSJ FCN（东岸证券）"}])
    assert [one["为什么不要"] for one in dropped] == ["说的是几张单子"]
    assert [(one["mention"], one["origin"]) for one in kept] == [("Jiake + Yishu", "代码找的单子"), ("Jiake + Bingyuan", "代码找的单子")]


def test_a_listed_deal_mention_found_by_rules_is_marked_without_asking(tmp_path: Path, store: ResultStore) -> None:
    windows = windows_of(chat_packet("2026-11-08_2330", "底层 JKJ+BNY，下周观察。"))
    local = FakeLocal()
    fake = FakeModel()
    run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(local, deals(tmp_path)))
    assert "〔指：JKJ+BNY = JKJ+BNY FCN（东岸证券）〕底层 JKJ+BNY，下周观察。" in fake.calls[0][1]
    assert local.kinds() == ["learn", "propose"]  # 名单上原样有、只对得上一张：不再问


SHAPES_YAML = """format: javmem-lexicon/2
aliases:
  - type: 资产
    names: ["阿岚科技", "ALK"]
  - type: 资产
    names: ["海港银行股票", "HGB"]
  - type: 人
    names: ["岑小燕", "小岑"]
bots:
  - names: ["小助", "Helper bot"]
"""


def shapes(tmp_path: Path) -> Lexicon:
    return lexicon_from(tmp_path, SHAPES_YAML, "shapes.yaml")


def test_shape_of_a_mention_does_not_override_the_proposed_class(tmp_path: Path) -> None:
    """“阿岚”看着像人名、“海港银行”看着像公司、“她”看着像人，Qwen 提的是股票、股票、Bot：照 Qwen 提的那一项的类比。"""
    lex = shapes(tmp_path)
    kept, _ = check(PreB(FakeLocal(), lex), lex, "阿岚今天又涨了，海港银行也涨了，她说先别动。", [
        {"说法": "阿岚", "名字": "阿岚科技"}, {"说法": "海港银行", "名字": "海港银行股票"}, {"说法": "她", "名字": "小助"}])
    assert [(one["mention"], one["class"], one["proposed"]) for one in kept] == [
        ("阿岚", "股票", "阿岚科技"), ("海港银行", "股票", "海港银行股票"), ("她", "Bot", "小助")]


def test_a_stock_name_inside_a_deal_is_dropped_whatever_qwen_proposed(tmp_path: Path) -> None:
    lex = deals(tmp_path)
    kept, dropped = check(PreB(FakeLocal(), lex), lex, "底层 Jiake + Yishu 那张下周观察。", [
        {"说法": "Jiake", "名字": "JKJ+YSJ FCN（东岸证券）"}, {"说法": "Yishu", "名字": "东岸证券"}])
    assert [one["为什么不要"] for one in dropped] == ["单子里的股票", "单子里的股票"]
    assert [one["mention"] for one in kept] == ["Jiake + Yishu 那张"]


def test_plural_words_only_count_right_before_the_mention(tmp_path: Path) -> None:
    lex = deals(tmp_path)
    finder = Roster(lex).deal_finder()
    assert [(one.text, one.plural) for one in finder.phrases("上周买了两张，JKJ+BNY 那张下周观察。")] == [("JKJ+BNY 那张", False)]
    kept, dropped = check(PreB(FakeLocal(), lex), lex, "两张 FCN 下周一起到期，那两张也是，还有三笔累购。", [
        {"说法": "两张 FCN", "名字": "X1 FCN（东岸证券）"}, {"说法": "那两张", "名字": "X1 FCN（东岸证券）"},
        {"说法": "三笔累购", "名字": "BNY 累购"}])
    assert not kept and [one["为什么不要"] for one in dropped] == ["说的是几张单子"] * 3


def test_a_pointer_that_occurs_twice_does_not_hide_the_deals(tmp_path: Path) -> None:
    lex = deals(tmp_path)
    kept, dropped = check(PreB(FakeLocal(), lex), lex, "X1 那张下周观察，JKJ+BNY 那张也是。",
                          [{"说法": "那张", "名字": "JKJ+BNY FCN（东岸证券）"}])
    assert [one["为什么不要"] for one in dropped] == ["一行里几处，分不清是哪一张"]
    assert [(one["mention"], one["auto"]) for one in kept] == [("X1 那张", True), ("JKJ+BNY 那张", False)]


def test_a_deal_phrase_written_twice_in_a_line_is_not_marked(tmp_path: Path) -> None:
    lex = deals(tmp_path)
    kept, dropped = check(PreB(FakeLocal(), lex), lex, "两张 YSJ 累购都停了，YSJ 累购（9月7日）还在。",
                          [{"说法": "YSJ 累购", "名字": "YSJ 累购（9/03）"}])
    assert not kept and [one["为什么不要"] for one in dropped] == ["说的是几张单子"]


def test_embedding_outage_leaves_the_window_unmarked(tmp_path: Path, store: ResultStore) -> None:
    """向量服务出错：不悄悄换成名单的先后挑，这个窗口不标、不存，照原样交给 L3（下回还会标）。"""
    from javmem.embedding import FakeEmbeddings

    windows = windows_of(chat_packet("2026-11-08_2330", "我在看 JKJ+BNY 那张。"))
    ref = windows[0].targets[0].ref
    local = FakeLocal([{"行": ref, "说法": "小岑", "名字": "岑小燕"}], picks={"JKJ+BNY 那张": "JKJ+BNY FCN（东岸证券）"})
    pre_b = PreB(local, deals(tmp_path), embedder=FakeEmbeddings(script=["auth"]))
    fake = FakeModel()
    run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=pre_b)
    assert "〔指：" not in fake.calls[0][1]
    assert pre_b.stats["向量服务出错的窗口"] == 1 and pre_b.stats["标过的窗口"] == 0
    assert store.preb_notes(windows[0].window_id, render_window(windows[0], PATTERNS).sha256, PRE_B_VERSION,
                            window_context(windows[0])) is None


def test_odd_logprobs_never_pass() -> None:
    from javmem.pipeline.preb_pick import choice_probs

    def out(*tops: tuple[str, float]):
        return {"choices": [{"logprobs": {"content": [{"token": tops[0][0], "top_logprobs": [
            {"token": token, "logprob": lp} for token, lp in tops]}]}}]}

    assert choice_probs(out(("A", float("nan")), ("X", math.log(0.5))), ("A", "X")) == {"X": 1.0}
    assert choice_probs(out(("A", 1000.0), ("X", math.log(0.5))), ("A", "X")) == {"X": 1.0}  # 不会溢出报错
    assert choice_probs(out(("A", float("inf"))), ("A", "X")) is None


def test_a_bare_stock_name_is_never_marked_as_a_deal(tmp_path: Path) -> None:
    lex = deals(tmp_path)
    kept, _ = check(PreB(FakeLocal(), lex), lex, "YSJ 今天跌了，ZKX 也跌了，EBX 说再等等。", [
        {"说法": "YSJ", "名字": "YSJ 累购（9/03）"}, {"说法": "ZKX", "名字": "BNY 累购"}, {"说法": "EBX", "名字": "东岸证券"}])
    assert [(one["mention"], one["class"], one["name"], one["auto"]) for one in kept] == [
        ("YSJ", "股票", "乙数据", True), ("ZKX", "股票", None, False),
        ("EBX", "公司", "东岸证券", False)]  # 大写的缩写 Qwen 提成了公司：照公司（也可能是银行的缩写）


def test_two_runs_never_send_the_embedding_service_two_requests_at_once(tmp_path: Path) -> None:
    import time

    from javmem.embedding import EMBEDDING_DIM, EmbeddingReply, pack_vector
    from javmem.pipeline.preb_pick import Picker

    busy = [0, 0]  # 正在调的、最多的时候同时调的
    guard = threading.Lock()

    class Slow:
        name = "fake-slow"

        def embed(self, texts):
            with guard:
                busy[0] += 1
                busy[1] = max(busy[1], busy[0])
            time.sleep(0.05)
            with guard:
                busy[0] -= 1
            return EmbeddingReply([pack_vector([1.0] + [0.0] * (EMBEDDING_DIM - 1)) for _ in texts], len(texts), 0.0)

    lock = tmp_path / "embedding.lock"
    pickers = [Picker(Slow(), lock_file=lock) for _ in range(2)]
    threads = [threading.Thread(target=lambda p=p, n=n: [p.rank(f"说法 {n}-{i}", [f"一项 {n}-{i}"]) for i in range(5)])
               for n, p in enumerate(pickers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert busy[1] == 1


# ---------- b4：收名字 ----------


def test_new_names_found_in_a_packet_are_on_the_roster_for_later_packets(tmp_path: Path, store: ResultStore) -> None:
    """b4：不等 L3。头一个包收到的新名字，后一个包标的时候名单上就有，同一个包自己标的时候也有。"""
    first, second = windows_of(chat_packet("2026-11-07_2330", "东岸的韦明说 X2 FCN 下周定价。"),
                               chat_packet("2026-11-08_2330", "韦总说那张定价了。"))
    ref = first.targets[0].ref

    def learns(user: str) -> list[dict[str, str]]:
        return [learn_entry(ref, "韦明", "韦明", "人"), learn_entry(ref, "X2 FCN", "X2 FCN", "单子")] if "韦明说" in user else []

    local = FakeLocal(learns=learns)
    run_extraction([second, first], FakeModel(), store, patterns=PATTERNS, options=OPTIONS,
                   pre_b=PreB(local, lexicon(tmp_path)))
    proposals = [user for user, kind in local.calls if kind == "propose"]
    assert len(proposals) == 2
    assert "（下面几项是前面的窗口认出来的，不一定全对）" in proposals[0] and "- 韦明〔人〕" in proposals[0]  # 自己这个包收的也在
    assert "- X2 FCN〔订单或合同〕" in proposals[1]  # 后一个包也看得到
    items = [(row["name"], row["kind"], row["packet_id"], row["sentence_id"]) for row in store.preb_items(PRE_B_VERSION)]
    assert items == [("韦明", "人", first.targets[0].packet_id, first.targets[0].sentence_id),
                     ("X2 FCN", "订单或合同", first.targets[0].packet_id, first.targets[0].sentence_id)]
    assert store.preb_learned(first.window_id, render_window(first, PATTERNS).sha256, PRE_B_VERSION, window_context(first))


def test_learned_names_are_checked_like_marks(tmp_path: Path) -> None:
    """收来的名字也要逐字在原文里（名字不在的拿说法当名字），代称、泛称、邮箱账号、Bot、几样东西、看不出是单子的不收。"""
    lex = lexicon_from(tmp_path, LEXICON_YAML + 'bots:\n  - names: ["小助"]\n')
    window = windows_of(chat_packet("2026-11-08_2330", "东岸的韦总说那张和累购都行，小助也说两张 FCN 不急，抄送 wei.ming2。"))[0]
    rendered = render_window(window, PATTERNS)
    ref = window.targets[0].ref
    pre_b = PreB(FakeLocal(), lex)
    view = view_of(lex, window)
    cases = [
        (learn_entry(ref, "韦总", "韦明总经理", "人"), ("韦总", "")),  # 名字不在原文里：拿说法当名字
        (learn_entry(ref, "那张", "那张", "单子"), (None, "代称")),
        (learn_entry(ref, "累购", "累购", "单子"), (None, "泛称")),
        (learn_entry(ref, "wei.ming2", "wei.ming2", "人"), (None, "邮箱、账号")),
        (learn_entry(ref, "小助", "小助", "人"), (None, "Bot")),
        (learn_entry(ref, "两张 FCN", "两张 FCN", "单子"), (None, "说的是几样")),
        (learn_entry(ref, "东岸", "东岸", "单子"), (None, "看不出是单子")),
        (learn_entry(ref, "老吴", "老吴", "人"), (None, "说法不在那一行原文里")),
        (learn_entry("T99", "韦总", "韦总", "人"), (None, "不是 T 行")),
        (learn_entry(ref, "韦总", "韦总", "神仙"), (None, "类不对")),
    ]
    for entry, (name, why) in cases:
        checked, reason = pre_b._check_new(view, window, rendered, entry)
        assert (checked["name"] if checked else None, reason) == (name, why), entry


def test_a_list_of_things_is_not_learned_as_one_name(tmp_path: Path) -> None:
    """J4.2 在 P01 上空试看到的：几只股票用“/”“、”连着写、几个名字连写在一起，收来时是一条，其实是几样东西，不收；
    单子不看这两样（篮子单子本来就是几只股票拼的）。例子是编的。"""
    lex = deals(tmp_path)
    window = windows_of(chat_packet("2026-11-08_2330", "JKJ/YSJ/BNY 都跌了，甲科技、乙数据也是，JiakeYishu 一起看，JKJ/YSJ FCN 下周观察，"
                                                     "Rowan Bank 也来了。"))[0]
    rendered = render_window(window, PATTERNS)
    ref = window.targets[0].ref
    pre_b = PreB(FakeLocal(), lex)
    view = view_of(lex, window)
    for entry, why in ((learn_entry(ref, "JKJ/YSJ/BNY", "JKJ/YSJ/BNY", "股票"), "说的是几样"),
                       (learn_entry(ref, "甲科技、乙数据", "甲科技、乙数据", "股票"), "说的是几样"),
                       (learn_entry(ref, "JiakeYishu", "JiakeYishu", "股票"), "说的是几样"),
                       (learn_entry(ref, "JKJ/YSJ FCN", "JKJ/YSJ FCN", "单子"), ""),
                       (learn_entry(ref, "Rowan Bank", "Rowan Bank", "公司"), "")):
        assert pre_b._check_new(view, window, rendered, entry)[1] == why, entry


def test_a_learned_name_goes_to_the_listed_item_a_chosen_item_or_a_new_one(tmp_path: Path, store: ResultStore) -> None:
    """名单上原样有的：记成那一项的“也叫”；同一类最像的几项里选中的：也叫；选“名单上没有”的：另起一项；没把握的：不收。"""
    windows = windows_of(chat_packet("2026-11-08_2330", "岑总和小岑说，韦明下午到，罗某某也来。"))
    ref = windows[0].targets[0].ref
    learns = [learn_entry(ref, "小岑", "小岑", "人"), learn_entry(ref, "岑总", "岑总", "人"),
              learn_entry(ref, "韦明", "韦明", "人"), learn_entry(ref, "罗某某", "罗某某", "人")]
    local = FakeLocal(learns=learns, picks={"岑总": "岑小燕", "韦明": "X", "罗某某": "X"})
    lex = lexicon(tmp_path)
    pre_b = PreB(local, lex)
    pre_b.run_packet(windows, store, PATTERNS)
    view = Rosters(store, lex).view(windows[0].scope)
    assert "岑总" in view.resolve("岑小燕").aliases  # type: ignore[union-attr]
    assert [item.name for item in view.learned] == ["韦明", "罗某某"]
    local2 = FakeLocal(learns=[learn_entry(ref, "韦明", "韦明", "人")], p=0.6)
    other = windows_of(chat_packet("2026-11-09_2330", "岑总和小岑说，韦明下午到，罗某某也来。"))
    PreB(local2, lex).run_packet(other, store, PATTERNS)  # 名单上原样有了：不再问；也不重复另起
    assert [item.name for item in Rosters(store, lex).view(windows[0].scope).learned] == ["韦明", "罗某某"]
    unsure = windows_of(chat_packet("2026-11-10_2330", "老吴下午到。"))
    local3 = FakeLocal(learns=[learn_entry(unsure[0].targets[0].ref, "老吴", "老吴", "人")], p=0.6, picks={"老吴": "X"})
    PreB(local3, lex).run_packet(unsure, store, PATTERNS)
    assert "老吴" not in [item.name for item in Rosters(store, lex).view(windows[0].scope).learned]  # 没把握的不收


def test_learning_keeps_twins_apart(tmp_path: Path, store: ResultStore) -> None:
    """防合错：日子不同的另起一项；不带日子的“X3 AQ”不记成其中一张的“也叫”，也不另起一项盖住两张。"""
    windows = windows_of(chat_packet("2026-11-08_2330", "X3 累购（9/02）和 X3 AQ 都在跑。"))
    ref = windows[0].targets[0].ref
    lex = twins(tmp_path)
    local = FakeLocal(learns=[learn_entry(ref, "X3 累购（9/02）", "X3 累购（9/02）", "单子"),
                              learn_entry(ref, "X3 AQ", "X3 AQ", "单子")],
                      picks={"X3 累购（9/02）": "X", "X3 AQ": "X3 累购（7/14）"})
    PreB(local, lex).run_packet(windows, store, PATTERNS)
    view = Rosters(store, lex).view(windows[0].scope)
    assert [item.name for item in view.learned] == ["X3 累购（9/02）"]
    assert "X3 AQ" not in view.resolve("X3 累购（7/14）").aliases  # type: ignore[union-attr]
    stages = [(row["mention"], row["kind"], row["stage"]) for row in store.preb_blocked(PRE_B_VERSION)]
    assert ("X3 AQ", "几项只差日子、编号、金额", "收名字") in stages
    asked = dict(local.asked)
    assert "X3 累购（7/14）" not in asked["X3 累购（9/02）"]  # 日子对不上的不当候选（防合错 a）


def test_learning_never_files_names_across_classes(tmp_path: Path, store: ResultStore) -> None:
    """股票的叫法不记成单子的“也叫”（另起的单子也不带光是股票代号的写法），单子的说法不记成股票的“也叫”。"""
    windows = windows_of(chat_packet("2026-11-08_2330", "ZKX AQ 那边，YSJ 也在看，Jiake 的 FCN 也问了。"))
    ref = windows[0].targets[0].ref
    lex = deals(tmp_path)
    local = FakeLocal(learns=[learn_entry(ref, "ZKX", "ZKX AQ", "单子"), learn_entry(ref, "Jiake 的 FCN", "Jiake 的 FCN", "股票")],
                      picks={"ZKX": "X", "Jiake 的 FCN": "甲科技"})
    pre_b = PreB(local, lex)
    pre_b.run_packet(windows, store, PATTERNS)
    view = Rosters(store, lex).view(windows[0].scope)
    assert [(item.name, item.aliases) for item in view.learned] == [("ZKX AQ", ())]  # 光是代号的写法不带
    assert "Jiake 的 FCN" not in view.resolve("甲科技").aliases  # type: ignore[union-attr]
    assert pre_b.stats["别的类的叫法没记"] >= 2


def test_marked_mentions_become_aliases_for_later_packets(tmp_path: Path, store: ResultStore) -> None:
    """标上的说法记成那一项的“也叫”：后面的包原样碰到就直接标，不用再问（b2 从 L3 结果里学的那一半）。代称不记。"""
    first, second = windows_of(chat_packet("2026-11-07_2330", "岑姐说那张下个月到期。"),
                               chat_packet("2026-11-08_2330", "岑姐又打电话来了。"))

    def proposals(user: str) -> list[dict[str, str]]:
        if "岑姐说那张" in user.rsplit("【窗口】", 1)[-1]:
            return [{"行": first.targets[0].ref, "说法": "岑姐", "名字": "岑小燕"},
                    {"行": first.targets[0].ref, "说法": "那张", "名字": "X1 FCN（东岸证券）"}]
        return [{"行": second.targets[0].ref, "说法": "岑姐", "名字": "岑小燕"}]

    local = FakeLocal(proposals)
    fake = FakeModel()
    lex = lexicon(tmp_path)
    run_extraction([first, second], fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(local, lex))
    assert local.kinds() == ["learn", "propose", "choose", "choose", "learn", "propose"]  # 后一个包不再问
    assert "〔指：岑姐 = 岑小燕〕岑姐又打电话来了。" in fake.calls[1][1]
    view = Rosters(store, lex).view(first.scope)
    assert "岑姐" in view.resolve("岑小燕").aliases and "那张" not in view.resolve("X1 FCN（东岸证券）").aliases  # type: ignore[union-attr]


def test_bots_learn_names_from_marks(tmp_path: Path, store: ResultStore) -> None:
    windows = windows_of(chat_packet("2026-11-08_2330", "H 助手说先别动。"))
    ref = windows[0].targets[0].ref
    lex = shapes(tmp_path)
    run_extraction(windows, FakeModel(), store, patterns=PATTERNS, options=OPTIONS,
                   pre_b=PreB(FakeLocal([{"行": ref, "说法": "H 助手", "名字": "小助"}]), lex))
    assert "H 助手" in Rosters(store, lex).view(windows[0].scope).resolve("小助").aliases  # type: ignore[union-attr]


# ---------- b4：按收包的先后、两路一起抽 ----------


def test_packets_go_in_the_order_they_were_exported() -> None:
    """按收包的先后：给了导出时间（包头的 covers_to）的照它，没给的照包号（包号里的日子就是 covers_to）。"""
    windows = windows_of(chat_packet("2026-11-08_2330", "后导出的。"), chat_packet("2026-11-07_2330", "先导出的。"))
    late = next(window for window in windows if window.target_packets[0].startswith("2026-11-08"))
    early = next(window for window in windows if window.target_packets[0].startswith("2026-11-07"))
    assert [group[0].window_id for group in packet_groups([late, early])] == [early.window_id, late.window_id]
    order = {late.target_packets[0]: 1.0, early.target_packets[0]: 2.0}  # 导出时间说 late 在前
    assert [group[0].window_id for group in packet_groups([early, late], order)] == [late.window_id, early.window_id]


class TwoAtOnce:
    """假的 L3 模型：两路同时在抽才过得去（一路一路抽的话等 5 秒报错）。events 记着每次开始抽的是哪个窗口的文字。"""

    name = "fake-two"
    cache_name = "fake-two@test"

    def __init__(self, events: list[str]) -> None:
        self.barrier = threading.Barrier(2, timeout=5)
        self.events = events

    def complete(self, system: str, user: str, schema: object, schema_name: str) -> ModelReply:
        self.events.append(f"l3:{user[-200:]}")
        self.barrier.wait()
        return ModelReply(json.dumps({"entities": [], "facts": [], "relations": []}), "stop", Usage(10, 5), "demo", 0.01)


def test_l3_runs_two_ways_while_later_packets_are_still_being_marked(tmp_path: Path, store: ResultStore) -> None:
    """b4：一个包标完就交给 L3，不等后面的包；L3 两路一起抽（b2、b3 一次只抽一个窗口）。"""
    events: list[str] = []
    first, second = windows_of(chat_packet("2026-11-07_2330", "甲包这句。"), chat_packet("2026-11-08_2330", "乙包这句。"))
    local = FakeLocal(events=events)
    model = TwoAtOnce(events)
    options = ExtractOptions(sleep=lambda seconds: None, concurrency=2)
    result = run_extraction([first, second], model, store, patterns=PATTERNS, options=options, pre_b=PreB(local, lexicon(tmp_path)))
    assert [outcome.status for outcome in result.outcomes] == [DONE, DONE]  # 两路同时在跑：栅栏过得去
    first_l3 = next(at for at, event in enumerate(events) if event.startswith("l3:") and "甲包" in event)
    second_marked = next(at for at, event in enumerate(events) if event.startswith("learn:") and "乙包" in event)
    assert first_l3 < second_marked  # 甲包的 L3 在乙包收名字之前就开始了


# ---------- b4：可见范围、现行版本 ----------


def test_learned_names_stay_inside_their_scope(tmp_path: Path, store: ResultStore) -> None:
    """一个分区学来的名字不给别的分区的窗口用；敏感标记多的窗口里学来的，标记少的窗口看不到，反过来看得到。"""
    business = windows_of(chat_packet("2026-11-07_2330", "韦明下午到。", sensitive="[报价]"))[0]
    lex = lexicon(tmp_path)
    PreB(FakeLocal(learns=[learn_entry(business.targets[0].ref, "韦明", "韦明", "人")]), lex).run_packet(
        [business], store, PATTERNS)
    rosters = Rosters(store, lex)
    assert [item.name for item in rosters.view(business.scope).learned] == ["韦明"]
    assert rosters.view(Scope(business.scope.partitions, ())).learned == []  # 少了“报价”这个标记：看不到
    assert [item.name for item in rosters.view(Scope(business.scope.partitions, ("报价", "客户"))).learned] == ["韦明"]
    assert rosters.view(Scope(("personal",), ("报价",))).learned == []  # 别的分区：看不到
    personal = windows_of(chat_packet("2026-11-08_2330", "韦明下午到。", domain="[personal]"))
    local = FakeLocal()
    PreB(local, lex).run_packet(personal, store, PATTERNS)
    assert all("韦明〔人〕" not in user for user, _ in local.calls)  # 别的分区的窗口：名单里没有它


def test_lexicon_groups_count_only_where_they_say(tmp_path: Path) -> None:
    lex = lexicon_from(tmp_path, """format: javmem-lexicon/2
aliases:
  - type: 人
    names: ["岑小燕", "小岑"]
    where: ["business"]
  - type: 人
    names: ["罗某某", "老罗"]
""")
    assert [item.name for item in Roster(lex, partitions=("business",)).fixed] == ["岑小燕", "罗某某"]
    assert [item.name for item in Roster(lex, partitions=("personal",)).fixed] == ["罗某某"]


def test_private_windows_are_neither_learned_from_nor_marked(tmp_path: Path, store: ResultStore) -> None:
    windows = windows_of(chat_packet("2026-11-08_2330", "韦明下午到。", privacy="private"))
    local = FakeLocal(learns=[learn_entry("T1", "韦明", "韦明", "人")])
    run_extraction(windows, FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(local, lexicon(tmp_path)))
    assert local.calls == [] and store.preb_items(PRE_B_VERSION) == []


def test_only_names_from_current_revisions_are_used(tmp_path: Path, store: ResultStore) -> None:
    window = windows_of(chat_packet("2026-11-08_2330", "韦明下午到。"))[0]
    lex = lexicon(tmp_path)
    PreB(FakeLocal(learns=[learn_entry(window.targets[0].ref, "韦明", "韦明", "人")]), lex).run_packet([window], store, PATTERNS)
    line = window.targets[0]
    assert [item.name for item in Rosters(store, lex, current={(line.packet_id, line.revision)}).view(window.scope).learned] == ["韦明"]
    assert Rosters(store, lex, current={(line.packet_id, line.revision + 1)}).view(window.scope).learned == []  # 换了版本


# ---------- b4：撤回、彻底删除连着删 ----------


def test_withdrawal_drops_learned_names_and_the_windows_that_used_them(tmp_path: Path, store: ResultStore) -> None:
    """撤回一句话：从它学来的名字删掉；标注里写着这个名字的窗口也要删、重抽（它的包改回排队）；只连一层：
    别的包自己学来的名字不跟着删。"""
    source, user, other = windows_of(chat_packet("2026-11-06_2330", "韦明下午到。"), chat_packet("2026-11-07_2330", "他说定价了。"),
                                     chat_packet("2026-11-08_2330", "罗某某也到了。"))
    lex = lexicon(tmp_path)
    PreB(FakeLocal(learns=[learn_entry(source.targets[0].ref, "韦明", "韦明", "人")]), lex).run_packet([source], store, PATTERNS)
    PreB(FakeLocal(learns=[learn_entry(other.targets[0].ref, "罗某某", "罗某某", "人")]), lex).run_packet([other], store, PATTERNS)
    weiming = next(row for row in store.preb_items(PRE_B_VERSION) if row["name"] == "韦明")
    noted = save_notes(store, user, {user.targets[0].ref: [["他", "韦明"]]}, [int(weiming["item_id"])])
    model = FakeModel()
    for window in (source, other):
        rendered = render_window(window, PATTERNS)
        store.save_window(window_row(window, rendered, "k-" + window.window_id, model.cache_name))
    store.save_window(window_row(user, noted, "k-" + user.window_id, model.cache_name))
    doomed, targets = store.doomed(sentences={source.targets[0].sentence_id})
    assert sorted(window_id for window_id, _ in doomed) == sorted([source.window_id, user.window_id])
    assert user.targets[0].packet_id in targets  # 用了这个名字的包改回排队、重抽
    store.delete_windows(doomed)
    assert store.preb_notes(user.window_id, render_window(user, PATTERNS).sha256, PRE_B_VERSION, window_context(user)) is None
    assert not store.preb_learned(source.window_id, render_window(source, PATTERNS).sha256, PRE_B_VERSION, window_context(source))
    assert store.forget_preb(sentences={source.targets[0].sentence_id}) == 1
    assert [row["name"] for row in store.preb_items(PRE_B_VERSION)] == ["罗某某"]  # 只连一层


def test_forget_preb_also_drops_notes_that_never_reached_l3(tmp_path: Path, store: ResultStore) -> None:
    """标完了、还没抽就停下的：结果库里没有窗口这一行，撤回时照样按包删掉它的标注和收名字的记号。"""
    window = windows_of(chat_packet("2026-11-08_2330", "小岑说那张下个月到期。"))[0]
    save_notes(store, window, {window.targets[0].ref: [["小岑", "岑小燕"]]})
    assert store.forget_preb(packets={window.targets[0].packet_id}) == 0
    assert store.preb_notes(window.window_id, render_window(window, PATTERNS).sha256, PRE_B_VERSION, window_context(window)) is None


def test_details_never_carry_names_learned_elsewhere(tmp_path: Path, store: ResultStore) -> None:
    """存下的细节、拦下的那几行里，别的窗口学来的项只写编号：撤回了它的出处，别处的记录里不留它的名字。"""
    first, second = windows_of(chat_packet("2026-11-07_2330", "韦明下午到。"), chat_packet("2026-11-08_2330", "韦总说他也到。"))
    lex = lexicon(tmp_path)
    run_extraction([first, second], FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(
        FakeLocal(lambda user: [{"行": second.targets[0].ref, "说法": "韦总", "名字": "韦明"}] if "韦总" in user.rsplit("【窗口】", 1)[-1] else [],
                  picks={"韦明": "X", "韦总": "X"},  # 收名字：名单上没有，另起；标的时候：都不是，不标
                  learns=lambda user: [learn_entry(first.targets[0].ref, "韦明", "韦明", "人")] if "韦明下午" in user else []),
        lex))
    import sqlite3

    con = sqlite3.connect(store.path)
    try:
        detail = con.execute("SELECT detail FROM preb_notes WHERE window_id = ?", (second.window_id,)).fetchone()[0]
    finally:
        con.close()
    assert "韦明" not in detail and "#" in detail  # 给它选的几项写的是编号


# ---------- 审阅查出来的（例子都是编的） ----------


def two_line_packet(stamp: str, first: str, second: str, **fields: object) -> str:
    body = f"## 原话 · 10:30–10:31\n- [10:30] 我：{first}\n- [10:31] 我：{second}\n"
    return build_packet(stamp=stamp, body=body, **fields)


def withdraw_in(store: ResultStore, *, sentences=(), packets=()):
    doomed, targets = store.doomed(packets=packets, sentences=sentences)
    store.delete_windows(doomed)
    store.forget_preb(packets=packets, sentences=sentences)
    return doomed, targets


def he_said(first: Window, second: Window, **picks: str) -> FakeLocal:
    """头一个包收“韦明”，后一个包把“他”标成韦明。"""
    ref = second.targets[0].ref
    return FakeLocal(lambda user: [{"行": ref, "说法": "他", "名字": "韦明"}] if "他说定价" in user.rsplit("【窗口】", 1)[-1] else [],
                     learns=lambda user: [learn_entry(first.targets[0].ref, "韦明", "韦明", "人")] if "韦明下午" in user else [],
                     picks={"韦明": "X", "他": "韦明", **picks})


def test_a_resent_packet_with_another_scope_does_not_replay_old_notes(tmp_path: Path, store: ResultStore) -> None:
    """同一个包换了可见范围重发（文字一样，窗口编号也一样）：以前的标注是照以前的名单标的，不能照样重放。"""
    first, second = windows_of(chat_packet("2026-11-07_2330", "韦明下午到。", sensitive="[报价]"),
                               chat_packet("2026-11-08_2330", "他说定价了。", sensitive="[报价]"))
    lex = lexicon(tmp_path)
    run_extraction([first, second], FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(he_said(first, second), lex))
    resent = windows_of(chat_packet("2026-11-08_2330", "他说定价了。", revision=2))[0]  # 少了“报价”这个标记
    assert resent.window_id == second.window_id and render_window(resent, PATTERNS).sha256 == render_window(second, PATTERNS).sha256
    assert "〔指：" not in prepare([resent], FakeModel(), PATTERNS, store=store)[0][1].text
    fake = FakeModel()
    run_extraction([resent], fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(FakeLocal(), lex))
    assert len(fake.calls) == 1 and "韦明" not in fake.calls[0][1]  # 照它自己的范围重新标、重新抽


def test_purging_the_old_revision_keeps_the_current_revisions_notes(tmp_path: Path, store: ResultStore) -> None:
    first, second = windows_of(chat_packet("2026-11-07_2330", "韦明下午到。"), chat_packet("2026-11-08_2330", "他说定价了。"))
    lex = lexicon(tmp_path)
    run_extraction([first, second], FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(he_said(first, second), lex))
    resent = windows_of(chat_packet("2026-11-08_2330", "他说定价了。", revision=2))[0]
    resent_local = FakeLocal([{"行": resent.targets[0].ref, "说法": "他", "名字": "韦明"}], picks={"他": "韦明"})
    run_extraction([resent], FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(resent_local, lex))
    assert verify_windows([resent], store, FakeModel(), PATTERNS)[0].extraction is not None
    old = {(resent.targets[0].packet_id, 1)}
    doomed, _ = store.doomed(revisions=old)  # javmem purge --revision 1 做的
    store.delete_windows(doomed)
    store.forget_preb(revisions=old)
    assert verify_windows([resent], store, FakeModel(), PATTERNS)[0].extraction is not None  # 现行的版本照样找得到


def test_a_name_copied_from_another_sentence_goes_with_that_sentence(tmp_path: Path, store: ResultStore) -> None:
    """说法在 T2（“韦总”），名字是照 T1 写的（“韦明”）：撤回 T1 那一句，这个名字也要删，标注里用了它的窗口也要重抽。"""
    first = windows_of(two_line_packet("2026-11-06_2330", "韦明下午到。", "韦总说定价了。"))[0]
    second = windows_of(chat_packet("2026-11-08_2330", "他说定价了。"))[0]
    t1, t2 = first.targets
    ref = second.targets[0].ref
    local = FakeLocal(lambda user: [{"行": ref, "说法": "他", "名字": "韦明"}] if "他说定价" in user.rsplit("【窗口】", 1)[-1] else [],
                      learns=lambda user: [learn_entry(t2.ref, "韦总", "韦明", "人")] if "韦总说" in user else [],
                      picks={"韦总": "X", "他": "韦明"})
    fake = FakeModel()
    run_extraction([first, second], fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(local, lexicon(tmp_path)))
    assert "〔指：他 = 韦明〕" in fake.calls[1][1]
    doomed, targets = withdraw_in(store, sentences={t1.sentence_id})
    assert second.window_id in [window_id for window_id, _ in doomed] and second.targets[0].packet_id in targets
    assert [row["name"] for row in store.preb_items(PRE_B_VERSION)] == []
    later = windows_of(chat_packet("2026-11-09_2330", "他又打电话来了。"))[0]
    again = FakeModel()
    run_extraction([later], again, store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(
        FakeLocal([{"行": later.targets[0].ref, "说法": "他", "名字": "韦明"}], picks={"他": "韦明"}), lexicon(tmp_path)))
    assert "韦明" not in again.calls[0][1]


def test_blocked_rows_go_with_the_aliases_they_were_compared_with(tmp_path: Path, store: ResultStore) -> None:
    """拦下的那一行记着名单上那一项的值，里面有从别的窗口学来的“也叫”的编号：撤回那一句，这一行也删。"""
    lex = lexicon_from(tmp_path, 'format: javmem-lexicon/2\naliases:\n  - type: 订单或合同\n    names: ["Y1 FCN（东岸）", "Y1 FCN"]\n')
    first, second = windows_of(chat_packet("2026-11-07_2330", "XS1111111111 下周付息。"),
                               chat_packet("2026-11-08_2330", "Y1 FCN（XS2222222222）下周付息。"))

    def proposals(user: str) -> list[dict[str, str]]:
        if "XS1111111111 下周" in user.rsplit("【窗口】", 1)[-1]:
            return [{"行": first.targets[0].ref, "说法": "XS1111111111", "名字": "Y1 FCN（东岸）"}]
        return [{"行": second.targets[0].ref, "说法": "Y1 FCN", "名字": "Y1 FCN（东岸）"}]

    run_extraction([first, second], FakeModel(), store, patterns=PATTERNS, options=OPTIONS,
                   pre_b=PreB(FakeLocal(proposals, picks={"XS1111111111": "Y1 FCN（东岸）"}), lex))
    assert any("XS1111111111" in row["item_values"] for row in store.preb_blocked(PRE_B_VERSION))
    withdraw_in(store, sentences={first.targets[0].sentence_id})
    assert not any("XS1111111111" in row["item_values"] for row in store.preb_blocked(PRE_B_VERSION))


def test_details_keep_only_the_windows_own_words(tmp_path: Path, store: ResultStore) -> None:
    """本机模型把名单上的名字当成说法回过来（不在那一行里）：去掉，细节里也不记它写的字。"""
    first, second = windows_of(chat_packet("2026-11-07_2330", "韦明下午到。"), chat_packet("2026-11-08_2330", "他说定价了。"))
    ref = second.targets[0].ref
    local = FakeLocal(lambda user: [{"行": ref, "说法": "韦明", "名字": "韦明"}] if "他说定价" in user.rsplit("【窗口】", 1)[-1] else [],
                      learns=lambda user: ([learn_entry(first.targets[0].ref, "韦明", "韦明", "人")] if "韦明下午" in user
                                           else [learn_entry(ref, "韦明", "韦明", "人")]),
                      picks={"韦明": "X"})
    run_extraction([first, second], FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(local, lexicon(tmp_path)))
    import sqlite3

    con = sqlite3.connect(store.path)
    try:
        details = [row[0] for row in con.execute("SELECT detail FROM preb_notes WHERE window_id = ? UNION ALL "
                                                 "SELECT detail FROM preb_learned WHERE window_id = ?", (second.window_id,) * 2)]
    finally:
        con.close()
    assert details and not any("韦明" in detail for detail in details)


def test_purge_and_private_resends_requeue_the_packets_that_used_old_names(tmp_path: Path, store: ResultStore) -> None:
    """包重发成 private、彻底删除旧版本：标注里用了旧版本里学来的名字的别的包，窗口删掉，包也要改回排队重抽。
    改不回排队的（写账正在跑），这一次先不动它们，下次再来。"""
    import sqlite3

    from javmem.pipeline.extract import forget_private_revisions

    first, second = windows_of(chat_packet("2026-11-07_2330", "韦明下午到。"), chat_packet("2026-11-08_2330", "他说定价了。"))
    run_extraction([first, second], FakeModel(), store, patterns=PATTERNS, options=OPTIONS,
                   pre_b=PreB(he_said(first, second), lexicon(tmp_path)))
    old = {(first.targets[0].packet_id, 1)}
    noted, targets = store.doomed_by_notes(revisions=old)
    assert [window_id for window_id, _ in noted] == [second.window_id] and targets == {second.targets[0].packet_id}
    ledger = tmp_path / "ledger.db"
    con = sqlite3.connect(ledger)
    con.execute("CREATE TABLE packets (packet_id TEXT, revision INTEGER, state TEXT, privacy TEXT)")
    con.executemany("INSERT INTO packets VALUES (?, ?, ?, ?)", [(first.targets[0].packet_id, 1, "superseded", "normal"),
                                                              (first.targets[0].packet_id, 2, "current", "private"),
                                                              (second.targets[0].packet_id, 1, "current", "normal")])
    con.commit()
    con.close()
    asked: list[set[str]] = []
    forget_private_revisions(ledger, store, requeue=lambda packets: asked.append(set(packets)) or False)  # 改不回排队
    assert second.window_id in {row["window_id"] for row in store.windows()}  # 先不动
    assert store.preb_items(PRE_B_VERSION)  # 名字也先留着（不给别的窗口用：旧版本的不算现行的）
    forget_private_revisions(ledger, store, requeue=lambda packets: asked.append(set(packets)) or True)
    assert asked == [targets, targets]
    assert second.window_id not in {row["window_id"] for row in store.windows()} and not store.preb_items(PRE_B_VERSION)


def test_a_name_found_again_in_the_same_packet_keeps_its_class(tmp_path: Path, store: ResultStore) -> None:
    """同一个包里前面的窗口刚收了人“韦明”，后面的窗口收到公司“韦明公司”（名字照它写成了韦明）：不记成人的“也叫”。"""
    filler = "".join(f"- [10:{31 + i // 10:02d}] 我：第{i}句没什么。\n" for i in range(90))
    body = f"## 原话 · 10:30–10:45\n- [10:30] 我：韦明下午到。\n{filler}- [10:45] 我：韦明公司的报价单到了。\n"
    windows = windows_of(build_packet(stamp="2026-11-08_2330", body=body))
    first = next(w for w in windows if any("韦明下午" in line.text for line in w.targets))
    second = next(w for w in windows if any("韦明公司" in line.text for line in w.targets))
    r1 = next(line.ref for line in first.targets if "韦明下午" in line.text)
    r2 = next(line.ref for line in second.targets if "韦明公司" in line.text)

    def learns(user: str) -> list[dict[str, str]]:
        tail = user.rsplit("【窗口】", 1)[-1]
        return ([learn_entry(r1, "韦明", "韦明", "人")] if "韦明下午" in tail else []) + (
            [learn_entry(r2, "韦明公司", "韦明", "公司")] if "韦明公司" in tail else [])

    lex = lexicon(tmp_path)
    PreB(FakeLocal(learns=learns, picks={"韦明": "X", "韦明公司": "X"}), lex).run_packet(windows, store, PATTERNS)
    person = Rosters(store, lex).view(first.scope).resolve("韦明")
    assert person is not None and "韦明公司" not in person.aliases


def test_a_dateless_mention_is_not_filed_under_a_new_dated_twin(tmp_path: Path, store: ResultStore) -> None:
    lex = lexicon_from(tmp_path, """format: javmem-lexicon/2
aliases:
  - type: 订单或合同
    names: ["Y3 累购（7/14）", "Y3 7/14 AQ"]
  - type: 订单或合同
    names: ["Y3 累购（7/19）", "Y3 7/19 AQ"]
""", "twins3.yaml")
    window = windows_of(chat_packet("2026-11-08_2330", "Y3 累购（9/05）今天开始接货了。"))[0]
    ref = window.targets[0].ref
    PreB(FakeLocal(learns=[learn_entry(ref, "Y3 累购", "Y3 累购（9/05）", "单子")]), lex).run_packet([window], store, PATTERNS)
    view = Rosters(store, lex).view(window.scope)
    assert [item.name for item in view.learned] == ["Y3 累购（9/05）"]
    assert not any("Y3 累购" in item.aliases for item in view.learned)  # 不带日子的分不出是哪一张，不记


def test_the_deadline_is_checked_between_local_calls(tmp_path: Path, store: ResultStore) -> None:
    import time

    window = windows_of(chat_packet("2026-11-08_2330", "韦明和罗某某、老吴下午到。"))[0]
    ref = window.targets[0].ref
    stamps: list[float] = []

    class Slow(FakeLocal):
        def chat(self, *args, **kwargs):
            time.sleep(0.3)
            stamps.append(time.monotonic())
            return super().chat(*args, **kwargs)

    local = Slow([{"行": ref, "说法": "韦明", "名字": "岑小燕"}],
                 learns=[learn_entry(ref, "韦明", "韦明", "人"), learn_entry(ref, "罗某某", "罗某某", "人"),
                         learn_entry(ref, "老吴", "老吴", "人")], picks={"韦明": "X", "罗某某": "X", "老吴": "X"})
    deadline = time.monotonic() + 0.2
    options = ExtractOptions(sleep=lambda seconds: None, concurrency=1, deadline=deadline)
    run_extraction([window], FakeModel(), store, patterns=PATTERNS, options=options, pre_b=PreB(local, lexicon(tmp_path)))
    assert sum(1 for at in stamps if at > deadline) <= 1  # 过了时间最多再收一个正在问的回复


def test_odd_local_replies_leave_the_window_unmarked(tmp_path: Path, store: ResultStore) -> None:
    """本机模型回的不是对象、连接中途断了：这个窗口不收、不标，照原样交给 L3，不让整批停下。"""
    import http.client

    windows = windows_of(chat_packet("2026-11-07_2330", "小岑说那张下个月到期。"), chat_packet("2026-11-08_2330", "小岑又说了一遍。"))

    class Odd(FakeLocal):
        def chat(self, system: str, user: str, **kwargs):
            if "下个月到期" in user.rsplit("【窗口】", 1)[-1]:
                return [], 0.01
            raise http.client.IncompleteRead(b"")

    fake = FakeModel()
    result = run_extraction(windows, fake, store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(Odd(), lexicon(tmp_path)))
    assert [one.status for one in result.outcomes] == [DONE, DONE] and all("〔指：" not in call[1] for call in fake.calls)


def test_a_context_packet_resent_with_the_same_text_keeps_the_replay_key(tmp_path: Path, store: ResultStore) -> None:
    """复审查出来的：后一个包接着前一个包（上文是前一个包的末尾），前一个包文字不变、换了版本重发：后一个包的窗口一字没变，
    标注照样重放，键对得上（标注只按正文那几个包的版本认，上文的不算）。"""
    def normalized(raw: str):
        return normalize(parse_packet(raw.encode("utf-8"), filename=file_name(raw)))

    first = normalized(chat_packet("2026-11-07_2330", "韦明下午到。", covers_from="2026-11-06T23:30:00+08:00",
                                   covers_to="2026-11-07T23:30:00+08:00"))
    second = normalized(chat_packet("2026-11-08_2330", "他说定价了。", continues="2026-11-07_2330_desk-bot"))
    windows = build_windows([first, second])
    later = next(window for window in windows if window.target_packets == [second.packet_id])
    earlier = next(window for window in windows if window is not later)
    assert any(line.packet_id == first.packet_id for line in later.lines)  # 带着前一个包的上文
    lex = lexicon(tmp_path)
    run_extraction(windows, FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(he_said(earlier, later), lex))
    resent = normalized(chat_packet("2026-11-07_2330", "韦明下午到。", covers_from="2026-11-06T23:30:00+08:00",
                                    covers_to="2026-11-07T23:30:00+08:00", revision=2))
    again = next(window for window in build_windows([second], context_pool=[resent]) if window.window_id == later.window_id)
    assert render_window(again, PATTERNS).sha256 == render_window(later, PATTERNS).sha256
    assert verify_windows([again], store, FakeModel(), PATTERNS)[0].extraction is not None


def test_the_line_field_of_a_bad_proposal_is_not_kept(tmp_path: Path, store: ResultStore) -> None:
    """复审查出来的：本机模型把名单上的名字写进了“行”：这一条去掉，细节里的“行”也不留它写的字。"""
    first, second = windows_of(chat_packet("2026-11-07_2330", "韦明下午到。"), chat_packet("2026-11-08_2330", "他说定价了。"))
    local = FakeLocal(lambda user: [{"行": "韦明", "说法": "他", "名字": "韦明"}] if "他说定价" in user.rsplit("【窗口】", 1)[-1] else [],
                      learns=lambda user: ([learn_entry(first.targets[0].ref, "韦明", "韦明", "人")] if "韦明下午" in user
                                           else [{"行": "韦明", "说法": "他", "名字": "韦明", "类": "人"}]),
                      picks={"韦明": "X"})
    run_extraction([first, second], FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(local, lexicon(tmp_path)))
    import sqlite3

    con = sqlite3.connect(store.path)
    try:
        details = [row[0] for row in con.execute("SELECT detail FROM preb_notes WHERE window_id = ? UNION ALL "
                                                 "SELECT detail FROM preb_learned WHERE window_id = ?", (second.window_id,) * 2)]
    finally:
        con.close()
    assert details and not any("韦明" in detail for detail in details)


def test_a_new_dated_deal_does_not_take_the_bare_name_when_one_twin_is_listed(tmp_path: Path, store: ResultStore) -> None:
    """复审查出来的：名单上只有 7/14 那一张，新收的是 9/05 那一张：两张一记进去就是只差日子的双胞胎，不带日子的
    “Y3 累购”分不出是哪一张，不记成新那一张的“也叫”。"""
    lex = lexicon_from(tmp_path, 'format: javmem-lexicon/2\naliases:\n  - type: 订单或合同\n    names: ["Y3 累购（7/14）", "Y3 7/14 AQ"]\n',
                       "one-twin.yaml")
    window = windows_of(chat_packet("2026-11-08_2330", "Y3 累购（9/05）今天开始接货了。"))[0]
    ref = window.targets[0].ref
    PreB(FakeLocal(learns=[learn_entry(ref, "Y3 累购", "Y3 累购（9/05）", "单子")]), lex).run_packet([window], store, PATTERNS)
    view = Rosters(store, lex).view(window.scope)
    assert [item.name for item in view.learned] == ["Y3 累购（9/05）"] and not view.learned[0].aliases


# ---------- 设置 ----------


def test_pre_b_settings_only_talk_to_this_machine() -> None:
    assert PreBSettings.from_env({}) is None
    found = PreBSettings.from_env({"JAVMEM_PRE_B_URL": "http://127.0.0.1:8180/v1/chat/completions"})
    assert found is not None and found.model == "qwen3.8-27b" and found.prompt_cache
    off = PreBSettings.from_env({"JAVMEM_PRE_B_URL": "http://localhost:8180/x", "JAVMEM_PRE_B_PROMPT_CACHE": "off",
                                 "JAVMEM_PRE_B_MODEL": "别的本机模型"})
    assert off is not None and not off.prompt_cache and off.model == "别的本机模型"
    for bad in ({"JAVMEM_PRE_B_URL": "https://models.example.com/v1/chat/completions"},
                {"JAVMEM_PRE_B_URL": "http://127.0.0.1:8180/x", "JAVMEM_PRE_B_PROMPT_CACHE": "maybe"}):
        with pytest.raises(ConfigError):
            PreBSettings.from_env(bad)


def test_local_model_turns_the_prompt_cache_off_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"choices": []}'

    for cache in (True, False):
        model = LocalModel("http://127.0.0.1:1/x", "m", prompt_cache=cache)
        monkeypatch.setattr(model._opener, "open", lambda request, timeout: (sent.append(json.loads(request.data)), Reply())[1])
        model.chat("系统", "问", max_tokens=1)
    assert "cache_prompt" not in sent[0] and sent[1]["cache_prompt"] is False


def test_items_label_learned_ones_by_number() -> None:
    assert Item("韦明", "人", item_id=7).label == "#7" and Item("岑小燕", "人").label == "岑小燕"
    assert Item("韦明", "人", item_id=7).target == "item:7" and Item("岑小燕", "人").target == "name:岑小燕"


def test_nothing_is_marked_after_the_deadline(tmp_path: Path, store: ResultStore) -> None:
    """到时间了：后面的包不再标、不再抽（本机模型也不调），记成没开始。"""
    windows = windows_of(chat_packet("2026-11-08_2330", "小岑说那张下个月到期。"))
    local = FakeLocal()
    options = ExtractOptions(sleep=lambda seconds: None, concurrency=1, deadline=0.0)
    result = run_extraction(windows, FakeModel(), store, patterns=PATTERNS, options=options, pre_b=PreB(local, lexicon(tmp_path)))
    assert local.calls == [] and [(one.status, one.detail) for one in result.outcomes] == [("not_started", "deadline")]


def test_limit_stops_marking_but_cached_windows_still_count(tmp_path: Path, store: ResultStore) -> None:
    """--limit 调够了：后面的包不再标；以前标过、抽过的窗口照存下的标注算键，照样记成存过的。"""
    first, second = windows_of(chat_packet("2026-11-07_2330", "甲包这句。"), chat_packet("2026-11-08_2330", "乙包这句。"))
    lex = lexicon(tmp_path)
    run_extraction([second], FakeModel(), store, patterns=PATTERNS, options=OPTIONS, pre_b=PreB(FakeLocal(), lex))  # 乙包先抽过
    local = FakeLocal(events=[])
    limited = ExtractOptions(sleep=lambda seconds: None, concurrency=1, limit=1)
    result = run_extraction([first, second], FakeModel(), store, patterns=PATTERNS, options=limited, pre_b=PreB(local, lex))
    assert [one.status for one in result.outcomes] == [DONE, "cached"]
    assert all("乙包" not in event for event in local.events or [])  # 乙包这次没再标


# ---------- 命令行：设置不对的报配置错误，什么都不动 ----------


def test_extract_refuses_a_pre_b_model_off_this_machine(world, capsys: pytest.CaptureFixture[str],
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from helpers import CHAT_PACKET

    from javmem.cli import main

    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    monkeypatch.setenv("JAVMEM_LLM_API_KEY", "test-only-not-a-key")
    monkeypatch.setenv("JAVMEM_PRE_B_URL", "https://models.example.com/v1/chat/completions")
    assert main(["extract", "--library", "prod"]) == 2
    assert "JAVMEM_PRE_B_URL" in capsys.readouterr().err
    monkeypatch.setenv("JAVMEM_PRE_B_URL", "http://127.0.0.1:9/v1/chat/completions")  # 本机的，可没配向量服务
    assert main(["extract", "--library", "prod"]) == 2
    assert "JAVMEM_EMBEDDING_BASE_URL" in capsys.readouterr().err
    assert not (world.data / "l3").exists()  # 什么都没动


def test_the_l3_prompt_is_part_of_the_key_and_of_extracted_by(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """JAVMEM_L3_PROMPT：抽、核对、写账都照它算键；账本的 extracted_by 写它。写错了的报配置错误。"""
    from helpers import CHAT_PACKET

    from javmem.cli import main

    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.intake()
    monkeypatch.setenv("JAVMEM_L3_PROMPT", "l3-v99")
    for command in (["extract", "--library", "prod", "--fake"], ["verify", "--library", "prod", "--fake"],
                    ["write", "--library", "prod", "--fake", "--fake-embeddings"]):
        assert main(command) == 2, command
    monkeypatch.setenv("JAVMEM_L3_PROMPT", "l3-v16")
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    monkeypatch.setenv("JAVMEM_L3_PROMPT", "l3-v17")
    assert main(["verify", "--library", "prod", "--fake"]) == 1  # 换了提示词：键对不上，结果库里没有
    monkeypatch.setenv("JAVMEM_L3_PROMPT", "l3-v16")
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    rows = world.rows("SELECT DISTINCT extracted_by FROM fact_claims")
    assert [row[0].split(" ")[0] for row in rows] == ["l3-v16"]
