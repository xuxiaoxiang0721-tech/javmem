"""撤回、彻底删除也清软变化判定的 soft.db（U3；以后要修.md“加关系隶属于”第 6 条）。

soft.db 的 verdicts 存着发给模型的题（前后几行原话打过码拼起来，还带着旧事实最后一次被说到的那一句）和模型的回复
（里面引着原话）。撤回以后这些判定不能再留着：带着撤回句子的那几道题整条删；彻底删除以后 soft.db 整理一遍，
删掉的字不留在空页、日志里。没带撤回内容的判定留着（写账最后一步还要照它执行）。

样例都是编的，不调模型：判定直接按 soft.db 的表写进去。
"""

from __future__ import annotations

import sqlite3

import pytest
from helpers import World
from test_write import day_packet, deliver

from javmem.soft.judge import SoftStore, shown_text
from javmem.withdraw import SOFT_MIN_CHARS, purge, withdraw

P1 = "2026-11-01_2330_desk-bot"
P2 = "2026-11-02_2330_desk-bot"
A = "梧桐路那间样板房的墙面改刷浅灰色的漆。"
B = "晨星工作室下周四派人来量窗帘的尺寸。"
SHORT = "好的。"


def stocked(world: World) -> None:
    deliver(world, day_packet("2026-11-01", f"我：{A}", f"desk-bot：{SHORT}"), day_packet("2026-11-02", f"我：{B}"))


def put_verdict(world: World, key: str, *lines: str) -> None:
    """照 render_case 的样子写一道题、一个回复，直接写进 soft.db（不调模型）。"""
    with SoftStore(world.data / "soft"):
        pass  # 照现在的代码建好表
    body = "\n".join(f"T{n} [2026-11-01 10:{30 + n:02d}] 我：{shown_text(line)}" for n, line in enumerate(lines, 1))
    request = f"【包日期】2026-11-01（要判的那几行是 2026-11-01 10:31 说的）\n【旧的】\nO1 事实：样板房的墙面是暖色系。\n【前后文】\n{body}\n【要判的】T1\n"
    output = '{"items": [{"target": "O1", "quote": "' + shown_text(lines[0]) + '"}]}'
    conn = sqlite3.connect(world.data / "soft" / "soft.db")
    try:
        with conn:
            conn.execute(
                "INSERT INTO verdicts (key, case_id, prompt_version, model, request_sha, request, output, parsed, created_at)"
                " VALUES (?, ?, 'soft-v1', 'claude-x', ?, ?, ?, '{}', '2026-11-03T00:00:00+08:00')",
                (key, f"C{key}", key, request, output),
            )
    finally:
        conn.close()


def keys(world: World) -> list[str]:
    conn = sqlite3.connect(world.data / "soft" / "soft.db")
    try:
        return [row[0] for row in conn.execute("SELECT key FROM verdicts ORDER BY key")]
    finally:
        conn.close()


def sentence_of(world: World, text: str) -> str:
    return world.rows("SELECT sentence_id FROM raw_sentences WHERE text = ?", text)[0]["sentence_id"]


def test_withdrawing_a_packet_drops_the_verdicts_that_carry_its_lines(world: World) -> None:
    stocked(world)
    put_verdict(world, "a", A)
    put_verdict(world, "b", B)
    put_verdict(world, "ab", B, A)  # A 是旧事实最后一次被说到的那一句：题里带着它，也算
    report = withdraw(world.settings, "prod", packets=[P1])
    assert report.soft_deleted == 2
    assert keys(world) == ["b"]


def test_withdrawing_one_sentence_drops_only_what_carries_it(world: World) -> None:
    stocked(world)
    put_verdict(world, "a", A)
    put_verdict(world, "b", B)
    report = withdraw(world.settings, "prod", sentences=[sentence_of(world, B)])
    assert report.soft_deleted == 1
    assert keys(world) == ["a"]


def test_short_lines_do_not_take_unrelated_verdicts_with_them(world: World) -> None:
    """撤回的包里“好的。”这种很短的话不拿来比：比了，别的包里一字不差的“好的。”会把一大片判定都带走。"""
    assert len(SHORT.strip()) < SOFT_MIN_CHARS
    stocked(world)
    put_verdict(world, "b-short", B, SHORT)
    withdraw(world.settings, "prod", packets=[P1])
    assert keys(world) == ["b-short"]


def test_purge_leaves_no_bytes_of_the_text_in_soft_db(world: World) -> None:
    stocked(world)
    put_verdict(world, "a", A)
    put_verdict(world, "b", B)
    withdraw(world.settings, "prod", packets=[P1])
    report = purge(world.settings, "prod", P1)
    assert not report.incomplete
    needle = A.rstrip("。").encode("utf-8")
    for path in (world.data / "soft").iterdir():
        if path.is_file():
            assert needle not in path.read_bytes(), path.name
    assert keys(world) == ["b"]


def test_a_running_judgment_is_not_raced(world: World) -> None:
    """软变化判定正开着 soft.db（它拿着自己的锁）：这一次不清、说一句，下一次撤回（不写目标也行）接着清。"""
    stocked(world)
    put_verdict(world, "a", A)
    with SoftStore(world.data / "soft", exclusive=True):
        report = withdraw(world.settings, "prod", packets=[P1])
    assert report.soft_deleted == 0 and any("soft.db" in problem for problem in report.problems)
    assert keys(world) == ["a"]
    again = withdraw(world.settings, "prod")
    assert again.soft_deleted == 1 and keys(world) == []


def test_no_soft_db_nothing_to_do(world: World) -> None:
    stocked(world)
    report = withdraw(world.settings, "prod", packets=[P1])
    assert report.soft_deleted == 0 and not (world.data / "soft").exists()


@pytest.mark.parametrize("text", [A, "〔外部:编的〕方案先这样", "密码是 Abc12345，别外传"])
def test_shown_text_is_what_render_case_writes(text: str) -> None:
    """拿来比的写法要和发题时一模一样（打码、〔〕换成［］）：render_case 改了写法，这里就红。"""
    from javmem.soft.cases import Case, Line, OldItem
    from javmem.soft.judge import render_case

    case = Case("C1", (OldItem("O1", "fact", "编的旧事实"),), (Line("T1", "2026-11-01T10:31:00+08:00", "我", "owner", text),), ("T1",))
    assert render_case(case).shown["T1"] == shown_text(text)
