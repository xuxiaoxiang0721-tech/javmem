"""收件、写账不随账本变大而变慢（J3.2 邮件导入报告第 4 节第 22 条：三箱邮件，每个包差不多大，收件却越收越慢）。

- 收件收尾时要找撤回了的话（withdraw._withdrawn_texts：新来的包里引用撤回内容的行跟着标上撤回）。原来一条 OR 把整张
  原文索引从头扫一遍，每收一个包扫一次，库越大越慢。现在分两段，各走迁移 0018 的小索引。
- 写账时每写完一个包，drop_stale_windows 按窗口编号的开头找这个包以前的说法。原来用 substr，整张说法表扫一遍；
  现在写成区间，走 fact_claims_by_window。
- 每次写账都要看哪些事实还没有向量（missing_vectors，一次写账两遍）、出汇总时数向量、--fake-embeddings 先查有没有
  真向量：原来要把向量表一行一行读进来（每行带着 10 KB 的向量），库越大越慢。现在只读迁移 0019 的小索引。
- 同一个主体、同一个属性的事实越积越多（一组越来越长），每往这一组写进一条都要重算整组的新旧：原来判新旧是组里
  每一条和每一条比（平方），说法读两遍，每一条都写回。现在排一次序，说法读一遍，没变的不写回。

找出来的和原来一样，先后也一样。样例都是编的。
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import pytest
from helpers import World, build_packet
from test_review import reviews
from test_withdraw import extract_and_write, stocked
from test_write import day_packet, deliver, l3_fact
from test_write import write as write_ledger

from javmem import cli, intake, withdraw
from javmem.ledger import connect, load_migrations, migrate
from javmem.packet import Packet
from javmem.pipeline.merge import GroupFact, Succession, succession
from javmem.review import decide, freeze
from javmem.pipeline.vectors import text_sha256
from javmem.pipeline.window import Window
from javmem.pipeline.write import KEEP_FROZEN, LedgerWriter, _json, _scope_of_row, missing_vectors

FIRST = "2026-11-01_2330_desk-bot"
WITHDRAWN_LINE = "紫藤路仓库那排旧货架周四一早拆掉。"  # FIRST 的第一句，后来单条撤回了

_SCAN = re.compile(r"^SCAN (?:TABLE )?(\w+)(?: AS (\w+))?")  # 老一点的 SQLite 写成“SCAN TABLE 表 AS 别名”
_USING = re.compile(r"USING (?:COVERING )?INDEX (\w+)")
_WRITES = {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "WITH"}


def full_scans(
    conn: sqlite3.Connection, statements: Iterable[str], table: str, *, covering_ok: bool = False
) -> list[tuple[str, str]]:
    """这些语句里整张扫 table 的：查询计划里 SCAN 这张表（或者它在这句里的别名）。从头扫只收撤回了的行的那两个小索引
    （迁移 0018，建的时候写着 WHERE withdrawn_at IS NOT NULL）不算：撤回了的行很少。别的带 WHERE 的索引照样算，
    比如 copy_key、message_id 的，几乎每一句都有，从头扫它们和整张扫一样。covering_ok：只扫索引、不回表读行的也不算
    （向量表的行带着 10 KB 的向量，要躲的是读行）。查询计划不看数据多少（账本里没有统计表），
    小账本上看出来的和大账本上一样。看的是跟踪记下的语句（参数已经换成了字面值），这几条和带参数执行时的计划一样。"""
    partial = {
        row[0]
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")
        if re.search(r"\bWHERE\s+withdrawn_at\s+IS\s+NOT\s+NULL\s*$", row[1], re.IGNORECASE)
    }
    found = []
    for sql in dict.fromkeys(statements):
        words = sql.split(None, 1)
        if sql.startswith("--") or not words or words[0].upper() not in _WRITES:
            continue  # SQLite 自己的（全文索引、触发器里的）以 -- 开头；BEGIN、COMMIT 这些没有查询计划
        names = {table, *re.findall(rf"\b{table}\s+AS\s+(\w+)", sql)}
        for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}"):
            match, index = _SCAN.match(row[3]), _USING.search(row[3])
            if match is None or not names & {match.group(1), match.group(2)} or (index and index.group(1) in partial):
                continue
            if covering_ok and "COVERING INDEX" in row[3]:
                continue
            found.append((sql[:160], row[3]))
    return found


def chat_packet(day: str, lines: list[str], *, section: str = "原话", hour: int = 9, **fields: object) -> str:
    """day 这一天的一个对话包：lines 每个是一句话（“说话人：原文”，可以带续行），从 hour 点起每句隔一分钟。
    section 是小节的标题（packet-spec 第 3 节）。"""
    body = f"## {section} · {day} {hour:02d}:00–{hour:02d}:59\n"
    body += "".join(f"- [{day} {hour:02d}:{at:02d}] {line}\n" for at, line in enumerate(lines))
    return build_packet(
        stamp=f"{day}_2330",
        body=body,
        created_at=f"{day}T23:30:00+08:00",
        covers_from=f"{day}T00:00:00+08:00",
        covers_to=f"{day}T23:30:00+08:00",
        **fields,
    )


def quoting(day: str) -> str:
    """day 这一天的包：问一句，下面引着那句撤回了的话。"""
    return chat_packet(
        day, [f"我：拆货架那件事改期了没有？\n  > [引用 2026-11-01 10:30 我] {WITHDRAWN_LINE}", "desk-bot：我去问一下仓管。"]
    )


def filler(day: str, count: int) -> str:
    """和撤回没关系的包，把账本撑大。"""
    return day_packet(day, *(f"我：第 {n} 号蓝箱里的样品换了新标签，旧标签收进抽屉。" for n in range(count)))


def start_with_a_withdrawn_line(world: World) -> None:
    deliver(world, day_packet("2026-11-01", f"我：{WITHDRAWN_LINE}", "我：顺便把门口的推车也挪走。"))
    withdraw.withdraw(world.settings, "prod", sentences=[f"{FIRST}#1.1.1"])


def quote_marked(world: World, packet_id: str) -> bool:
    rows = world.rows("SELECT withdrawn_at FROM raw_sentences WHERE packet_id = ? AND quoted = 1", packet_id)
    return len(rows) == 1 and rows[0]["withdrawn_at"] is not None


# ========== 收件这一步 ==========


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """收件收尾时 mark_on_arrival 让 SQLite 走了多少步（每走一步叫一次进度回调）：(包号, 步数)。"""
    counts: list[tuple[str, int]] = []
    original = withdraw.mark_on_arrival

    def counting(conn: sqlite3.Connection, packet: Packet, now: str) -> list[str]:
        walked = 0

        def tick() -> int:
            nonlocal walked
            walked += 1
            return 0

        conn.set_progress_handler(tick, 1)
        try:
            return original(conn, packet, now)
        finally:
            conn.set_progress_handler(None, 1)
            counts.append((packet.header.packet_id, walked))

    monkeypatch.setattr(withdraw, "mark_on_arrival", counting)
    return counts


def test_marking_quotes_on_arrival_does_not_grow_with_the_ledger(world: World, steps: list[tuple[str, int]]) -> None:
    """账本里多了 300 句和撤回不相干的话，收一个同样的包，认撤回这一步走的步数不跟着涨（只多走过一行撤回了的引用行
    的几步）。原来每多一句都要多走十来步。"""
    start_with_a_withdrawn_line(world)
    deliver(world, quoting("2026-11-02"))
    small = steps[-1]
    deliver(world, *(filler(f"2026-11-{day:02d}", 15) for day in range(4, 24)))
    deliver(world, quoting("2026-11-27"))
    large = steps[-1]

    assert small[0] == "2026-11-02_2330_desk-bot" and large[0] == "2026-11-27_2330_desk-bot"
    assert small[1] > 0  # 进度回调真的叫到了（编 SQLite 时可以把它去掉）
    assert quote_marked(world, small[0]) and quote_marked(world, large[0])  # 引着撤回的话的那一行照样标上
    extra = 20 * 15
    assert large[1] - small[1] < extra // 10, (small, large)


def test_intake_never_scans_the_whole_raw_index(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """收件的每一条语句都不整张扫原文索引：账本里有单条撤回的句子、撤回了的包，新来的包引着撤回的话，另一个包
    换了版本，都一样。"""
    start_with_a_withdrawn_line(world)
    deliver(world, day_packet("2026-11-02", "我：西门的灯泡下午换成暖光的。"), day_packet("2026-11-03", "我：样品册第二版周一寄出。"))
    withdraw.withdraw(world.settings, "prod", packets=["2026-11-02_2330_desk-bot"])

    seen: list[str] = []
    original = intake.open_ledger

    def traced(library):  # noqa: ANN001, ANN202
        conn = original(library)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(intake, "open_ledger", traced)
    world.drop("inbox/desk-bot", quoting("2026-11-05"))
    world.drop("inbox/desk-bot", chat_packet("2026-11-03", ["我：样品册第二版改到周三寄出。"], revision=2))
    report = world.intake()
    assert sorted(outcome.result for outcome in report.outcomes) == ["accepted", "replaced"]
    assert quote_marked(world, "2026-11-05_2330_desk-bot")

    conn = world.ledger()
    try:
        assert any("raw_sentences" in sql for sql in seen)  # 真的记下了
        assert full_scans(conn, seen, "raw_sentences") == []
    finally:
        conn.close()


# ========== 撤回了的话：找出来的和原来整张扫的一样 ==========

DUP = "灰色帆布包里装着三本旧账簿。"
SINGLE = "北面窗台上的仙人掌每周浇一次水。"
BOTH = "铜钥匙挂在配电箱左边的钉子上。"
LATE = "最后一格抽屉放备用的保险丝。"


def _packet(conn: sqlite3.Connection, packet_id: str, withdrawn_at: str | None) -> None:
    conn.execute(
        "INSERT INTO packets (packet_id, revision, source, env, framework, channel, kind, created_at,"
        " covers_from, covers_to, covers_from_epoch, covers_to_epoch, partitions, privacy, content_hash,"
        " inbox_path, archive_path, state, received_at, withdrawn_at)"
        " VALUES (?, 1, 'demo-bot', 'prod', 'demo-chat', 'chat', 'daily', 't', 't', 't', 0, 0, '[\"business\"]',"
        " 'normal', 'hash', 'inbox/x.md', 'archive/x.md', 'current', 't', ?)",
        (packet_id, withdrawn_at),
    )


def withdrawals(conn: sqlite3.Connection) -> list[tuple[str, str | None]]:
    """一本带着几种撤回的小账本：A、C 整包撤回了（A 先撤），B、D 没撤，B、C、D 里各有单条撤回的句子。
    同一句话在 C、A 里各有一份（C 的那一份 id 小），一句话 D 里单条撤回了、A 里跟着包撤回了。
    返回撤回了的话：(比引用用的写法, 因为撤回哪个包才算撤回的)，按原文索引里的先后。"""
    _packet(conn, "A", "2026-11-01T09:00:00+08:00")
    _packet(conn, "B", None)
    _packet(conn, "C", "2026-11-02T09:00:00+08:00")
    _packet(conn, "D", None)
    rows = [
        (1, "C", DUP, 0, None),
        (2, "B", SINGLE, 0, "t"),
        (3, "A", DUP, 0, None),
        (4, "D", BOTH, 0, "t"),
        (5, "A", BOTH, 0, None),
        (6, "A", "引用里那句旧话不算撤回的原话。", 1, None),  # 引用行不算
        (7, "C", "好的。", 0, None),  # 太短
        (8, "B", "这一句没撤回，也不在撤回的包里。", 0, None),
        (9, "C", LATE, 0, "t"),
    ]
    conn.executemany(
        "INSERT INTO raw_sentences (id, sentence_id, packet_id, revision, seq, section_kind, section_no, line_no,"
        " sentence_no, text, speaker, speaker_role, said_at, said_at_epoch, origin, quoted, withdrawn_at)"
        " VALUES (?, ?, ?, 1, ?, 'raw', 1, ?, 1, ?, '我', 'owner', 't', 0, 'said', ?, ?)",
        [(i, f"{packet}#1.{i}.1", packet, i, i, text, quoted, at) for i, packet, text, quoted, at in rows],
    )
    # DUP 先见到的是 C 的，后来 A 的又盖了一遍；BOTH 有单条撤回的一份，算单条
    return [(DUP.rstrip("。"), "A"), (SINGLE.rstrip("。"), None), (BOTH.rstrip("。"), None), (LATE.rstrip("。"), None)]


def full_scan_withdrawn_texts(conn: sqlite3.Connection) -> dict[str, str | None]:
    """改以前的写法（一条 OR，整张扫原文索引），拿来比。原来没写先后：没有统计表时（javmem 不建）SQLite 整张扫
    就是按 id 出，这里把 ORDER BY 写明，免得对照本身也跟着查询计划变。"""
    texts: dict[str, str | None] = {}
    for row in conn.execute(
        "SELECT s.packet_id, s.text, s.withdrawn_at, p.withdrawn_at AS packet_withdrawn FROM raw_sentences AS s"
        " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
        " WHERE s.quoted = 0 AND (s.withdrawn_at IS NOT NULL OR p.withdrawn_at IS NOT NULL) ORDER BY s.id"
    ):
        text = withdraw._quote_key(row["text"])
        if len(text) < withdraw.MIN_COPY_CHARS:
            continue
        cause = row["packet_id"] if row["packet_withdrawn"] is not None and row["withdrawn_at"] is None else None
        texts[text] = None if (text in texts and texts[text] is None) or cause is None else cause
    return texts


def test_withdrawn_texts_are_the_same_as_a_full_scan(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    try:
        migrate(conn)
        expected = withdrawals(conn)
        assert list(full_scan_withdrawn_texts(conn).items()) == expected
        assert list(withdraw._withdrawn_texts(conn).items()) == expected
    finally:
        conn.close()


def _sentences(conn: sqlite3.Connection, packet_id: str, texts: list[str]) -> None:
    conn.executemany(
        "INSERT INTO raw_sentences (sentence_id, packet_id, revision, seq, section_kind, section_no, line_no,"
        " sentence_no, text, speaker, speaker_role, said_at, said_at_epoch, origin)"
        " VALUES (?, ?, 1, ?, 'raw', 1, ?, 1, ?, '我', 'owner', 't', 0, 'said')",
        [(f"{packet_id}#1.{seq}.1", packet_id, seq, seq, text) for seq, text in enumerate(texts, 1)],
    )


def test_withdrawn_texts_do_not_depend_on_statistics(tmp_path: Path) -> None:
    """跑过 ANALYZE 以后（javmem 自己不跑，有人拿命令行工具跑一下就有了），SQLite 可能换一种查法，行的先后跟着变，
    原来的写法这时记的包就不一样了。现在自己按 id 排，有没有统计表都一样。A 先登记、很晚才建原文索引，同一句话
    C、A 里各有一份，A 的那一份 id 大，记的是 A。"""
    conn = connect(tmp_path / "ledger.db")
    try:
        migrate(conn)
        _packet(conn, "A", "2026-11-20T09:00:00+08:00")
        for n in range(40):
            _packet(conn, f"F{n:02d}", None)
            _sentences(conn, f"F{n:02d}", [f"编号 {n}-{i} 的填充句，只是让账本大一点。" for i in range(50)])
        _packet(conn, "C", "2026-11-21T09:00:00+08:00")
        _sentences(conn, "C", [DUP, "C 包里另外一句话，也够八个字。"])
        _sentences(conn, "A", ["A 包里另外一句话，也够八个字。", DUP])
        expected = list(withdraw._withdrawn_texts(conn).items())
        assert dict(expected)[DUP.rstrip("。")] == "A"
        assert expected == list(full_scan_withdrawn_texts(conn).items())
        conn.execute("ANALYZE")
        assert list(withdraw._withdrawn_texts(conn).items()) == expected
    finally:
        conn.close()


def test_an_old_ledger_gets_the_small_indexes(tmp_path: Path) -> None:
    """0018 以前的账本：升上来以后两个小索引都在、只收撤回了的，找出来的和原来一样，账本没坏。"""
    conn = connect(tmp_path / "old.db")
    try:
        migrations = load_migrations()
        migrate(conn, [m for m in migrations if m.version < 18])
        expected = withdrawals(conn)
        assert 18 in migrate(conn)
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"raw_sentences_withdrawn", "packets_withdrawn"} <= indexes
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        in_index = conn.execute(
            "SELECT count(*) FROM raw_sentences INDEXED BY raw_sentences_withdrawn WHERE withdrawn_at IS NOT NULL"
        ).fetchone()[0]
        assert in_index == 3
        assert list(withdraw._withdrawn_texts(conn).items()) == expected
    finally:
        conn.close()


# ========== 写账这一步 ==========


@pytest.mark.parametrize("packet_id", ["P", "P2", "2026-11-01_2330_desk-bot", "中文包", "Q"])
def test_window_range_picks_the_same_windows_as_the_prefix(packet_id: str) -> None:
    """窗口编号以“包号#”开头的，区间写法和 substr 找出来的一样：包号是别的包号的开头的、带中文的、紧挨着 # 和 $ 的。"""
    from javmem.pipeline.write import window_range  # 这一段加的

    windows = [
        "P#w1", "P#w10", "P#", "P", "P$w1", "P\"#w1", "P2#w1", "P-x#w1", "PP#w1", "O#w9", "Q#w1", "Q#", "P#中文",
        "中文包#w1", "中文包", "中文包$", "中文包2#w1", "2026-11-01_2330_desk-bot#w1", "2026-11-01_2330_desk-bot-2#w1",
        "2026-11-01_2330_desk-bot", "2026-11-01_2330_desk-bot$",
    ]
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (window_id TEXT NOT NULL)")
        conn.executemany("INSERT INTO t VALUES (?)", [(window,) for window in windows])
        prefix = f"{packet_id}#"
        by_prefix = {row[0] for row in conn.execute("SELECT window_id FROM t WHERE substr(window_id, 1, ?) = ?", (len(prefix), prefix))}
        low, high = window_range(packet_id)
        by_range = {row[0] for row in conn.execute("SELECT window_id FROM t WHERE window_id >= ? AND window_id < ?", (low, high))}
        assert by_range == by_prefix
        assert by_prefix == {window for window in windows if window.startswith(prefix)}
    finally:
        conn.close()


def test_dropping_stale_windows_uses_the_window_index(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """撤回几句以后包从两个窗口变成一个：写完这个包，以前第二个窗口的说法删掉，找的时候不整张扫说法表。"""
    sentence = "第 {} 条凑数的备注：青瓷杯垫按批号排好，蓝色标签朝外摆放，凑够长度好切两个窗口。"  # 150 句 6,342 字，两个窗口
    first = "".join(sentence.format(index) for index in range(1, 76))
    second = "".join(sentence.format(index) for index in range(76, 151))
    stocked(world, chat_packet("2026-11-01", [f"我：{first}", f"我：{second}"]))
    windows = {row["window_id"] for row in world.rows("SELECT window_id FROM fact_claims")}
    assert windows == {f"{FIRST}#w1", f"{FIRST}#w2"}

    seen: list[str] = []
    original = LedgerWriter.drop_stale_windows

    def traced(self: LedgerWriter, packet_id: str, current: object) -> int:
        self.conn.set_trace_callback(seen.append)
        try:
            return original(self, packet_id, current)  # type: ignore[arg-type]
        finally:
            self.conn.set_trace_callback(None)

    monkeypatch.setattr(LedgerWriter, "drop_stale_windows", traced)
    withdraw.withdraw(world.settings, "prod", sentences=[f"{FIRST}#1.1.{index}" for index in range(1, 13)])
    extract_and_write()
    windows = {row["window_id"] for row in world.rows("SELECT window_id FROM fact_claims")}
    assert windows == {f"{FIRST}#w1"}

    conn = world.ledger()
    try:
        assert any("fact_claims" in sql for sql in seen)  # 真的跑到了
        assert full_scans(conn, seen, "fact_claims") == []
    finally:
        conn.close()


# ========== 写账：向量 ==========

FOUR_LINES = (  # 假抽取一句带数字的话一条事实，写账时算好假向量
    "我：三号仓的叉车 2 台周四修好。",
    "我：样品柜第 4 层留给新到的瓷杯。",
    "我：门禁卡 7 张周五前发完。",
    "我：月底前把 9 箱旧账簿搬去地下室。",
)


def plan_steps(conn: sqlite3.Connection, statements: Iterable[str]) -> list[str]:
    steps = []
    for sql in dict.fromkeys(statements):
        words = sql.split(None, 1)
        if sql.startswith("--") or not words or words[0].upper() not in _WRITES:
            continue
        steps += [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")]
    return steps


def full_scan_missing_vectors(conn: sqlite3.Connection, model: str) -> dict[str, str]:
    """改以前的写法（连表，一条条按编号回表找），拿来比。原来没写先后（连表时事实表在外层，整张按 rowid 扫），这里写明。"""
    rows = conn.execute(
        "SELECT f.fact_id, f.content, v.model, v.text_sha256 FROM facts AS f LEFT JOIN fact_vectors AS v ON v.fact_id = f.fact_id"
        " ORDER BY f.rowid"
    ).fetchall()
    return {
        row["fact_id"]: row["content"]
        for row in rows
        if row["content"] and (row["model"] != model or row["text_sha256"] != text_sha256(row["content"]))
    }


def test_missing_vectors_finds_the_same_facts(world: World) -> None:
    """没有向量的、模型换了的、文字变了的（指纹对不上）都找得出来，文字是空的不算，先后和原来一样。"""
    stocked(world, day_packet("2026-11-01", *FOUR_LINES), day_packet("2026-11-02", "我：西门的灯泡下午换 3 个暖光的。"))
    conn = world.ledger()
    try:
        facts = [row["fact_id"] for row in conn.execute("SELECT fact_id FROM facts ORDER BY rowid")]
        assert len(facts) == 5
        assert missing_vectors(conn, "fake-embedding") == {}
        conn.execute("DELETE FROM fact_vectors WHERE fact_id = ?", (facts[0],))
        conn.execute("UPDATE fact_vectors SET model = 'another-model' WHERE fact_id = ?", (facts[1],))
        conn.execute("UPDATE fact_vectors SET text_sha256 = ? WHERE fact_id = ?", ("0" * 64, facts[3]))
        conn.execute("UPDATE facts SET content = '' WHERE fact_id = ?", (facts[4],))
        conn.execute("DELETE FROM fact_vectors WHERE fact_id = ?", (facts[4],))
        expected = full_scan_missing_vectors(conn, "fake-embedding")
        assert list(expected) == [facts[0], facts[1], facts[3]]
        assert list(missing_vectors(conn, "fake-embedding").items()) == list(expected.items())
        assert list(missing_vectors(conn, "another-model")) == [facts[0], facts[2], facts[3]]
    finally:
        conn.close()


def test_finding_missing_vectors_only_reads_the_small_index(world: World) -> None:
    """找没有向量的事实时，向量表只读迁移 0019 的小索引，不回表把带着向量的那一行读进来。"""
    stocked(world, day_packet("2026-11-01", *FOUR_LINES))
    conn = world.ledger()
    try:
        seen: list[str] = []
        conn.set_trace_callback(seen.append)
        missing_vectors(conn, "fake-embedding")
        conn.set_trace_callback(None)
        steps = plan_steps(conn, seen)
        touching = [step for step in steps if re.search(r"\b(fact_vectors|v)\b", step)]
        assert touching and all("COVERING INDEX" in step for step in touching), steps
    finally:
        conn.close()


def test_masking_who_a_claim_is_from_finds_facts_by_index(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """J4.1：按事实撤回了邮件的发件人那一句，没有说法的事实的“说法出自谁”跟着打码，从这个包的证据找（evidence_by_packet），
    不把事实表从头扫一遍。旁边那句给说话人打码的（说话人等于发件人的）以前就整张扫，不在这里管。"""
    from test_withdraw_mail import SENDER, delivered, mail_extract
    from test_write import write as write_ledger

    delivered(world)
    write_ledger(world, mail_extract).close()
    sender = world.rows("SELECT fact_id FROM facts WHERE content = ?", SENDER)[0]["fact_id"]
    seen: list[str] = []
    original = LedgerWriter.mask_senders

    def traced(self: LedgerWriter, mails: object) -> set[str]:
        self.conn.set_trace_callback(seen.append)
        try:
            return original(self, mails)  # type: ignore[arg-type]
        finally:
            self.conn.set_trace_callback(None)

    monkeypatch.setattr(LedgerWriter, "mask_senders", traced)
    withdraw.withdraw(world.settings, "prod", facts=[sender])
    masking = [sql for sql in seen if sql.startswith("UPDATE facts") and "attributed_to" in sql]
    conn = world.ledger()
    try:
        assert any("IN (SELECT v.fact_id FROM evidence" in sql for sql in masking)  # 真的跑到了
        assert full_scans(conn, masking, "facts") == []
    finally:
        conn.close()


def test_write_never_reads_the_whole_vector_table(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """写账（连同补向量、出汇总、--fake-embeddings 先查真向量）不整张扫向量表的行：要扫也只扫小索引。"""
    stocked(world, day_packet("2026-11-01", *FOUR_LINES))
    deliver(world, day_packet("2026-11-02", "我：西门的灯泡下午换 3 个暖光的。"))
    assert cli.main(["extract", "--library", "prod", "--fake"]) == 0

    seen: list[str] = []
    original = cli.open_ledger

    def traced(library):  # noqa: ANN001, ANN202
        conn = original(library)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(cli, "open_ledger", traced)
    assert cli.main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0
    conn = world.ledger()
    try:
        assert any("fact_vectors" in sql for sql in seen)  # 真的记下了
        assert full_scans(conn, seen, "fact_vectors", covering_ok=True) == []
    finally:
        conn.close()


def test_missing_vectors_keeps_the_original_error(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """读到一半出了错、SQLite 自己把事务回滚了（磁盘满、IO 错这类会这样）：报出来的是原来那个错，不是“没有事务可提交”。"""
    stocked(world, day_packet("2026-11-01", *FOUR_LINES))
    conn = world.ledger()
    try:
        def broken(text: str) -> str:
            conn.execute("ROLLBACK")  # 冒充 SQLite 自己回滚
            raise ValueError("原来的错")

        monkeypatch.setattr("javmem.pipeline.write.text_sha256", broken)
        with pytest.raises(ValueError, match="原来的错"):
            missing_vectors(conn, "fake-embedding")
        assert not conn.in_transaction
    finally:
        conn.close()


def test_an_old_ledger_gets_the_vector_index(tmp_path: Path) -> None:
    """0019 以前的账本（有事实、有向量）：升上来以后索引在、账本没坏，找没有向量的照旧。"""
    conn = connect(tmp_path / "old.db")
    try:
        migrate(conn, [m for m in load_migrations() if m.version < 19])
        texts = {"F1": "灰色帆布包里装着 3 本旧账簿。", "F2": "北面窗台的 2 盆仙人掌每周浇水。", "F3": "铜钥匙 5 把挂在配电箱左边。",
                 "F4": "最后一格抽屉放 8 个备用保险丝。"}
        for fact_id, content in texts.items():
            conn.execute(
                "INSERT INTO facts (fact_id, content, status, scope_partitions, created_at, updated_at)"
                " VALUES (?, ?, 'verified', '[\"business\"]', 't', 't')",
                (fact_id, content),
            )
        vectors = [("F1", "fake-embedding", text_sha256(texts["F1"])), ("F3", "another-model", text_sha256(texts["F3"])),
                   ("F4", "fake-embedding", "0" * 64)]  # F2 没有向量，F3 模型不对，F4 指纹对不上
        conn.executemany(
            "INSERT INTO fact_vectors (fact_id, model, dim, text_sha256, vector, created_at) VALUES (?, ?, 2560, ?, ?, 't')",
            [(fact_id, model, digest, bytes(4 * 2560)) for fact_id, model, digest in vectors],
        )
        before = full_scan_missing_vectors(conn, "fake-embedding")
        assert list(before) == ["F2", "F3", "F4"]
        assert 19 in migrate(conn)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "fact_vectors_check" in names
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(missing_vectors(conn, "fake-embedding").items()) == list(before.items())
    finally:
        conn.close()


# ========== 写账：一组越来越长 ==========
# 同一个主体、同一个属性的事实是一组（group_key），新值替代旧值。每往一组写进一条，_supersede 重算整组的新旧。原来
# merge.succession 让每一条都把组里别的句子挑一遍、各排一次序（组越大越是平方地涨），整组的说法读两遍，每一条都写回，
# 每一条都去查一遍主体有没有没处理的歧义。现在全组的句子只排一次，说法读一遍，没变的不写回，歧义一组查一次。


def old_succession(facts: Sequence[GroupFact]) -> Succession:
    """改以前的写法，拿来比。"""
    superseded: dict[str, tuple[str, str]] = {}
    conflicts: list[tuple[str, str]] = []
    for fact in sorted(facts, key=lambda item: item.fact_id):
        later = sorted(
            (moment, said, other.fact_id)
            for other in facts
            if other.fact_id != fact.fact_id
            for moment, said in other.statements
            if moment > fact.last
        )
        if not later:
            continue
        if fact.frozen:
            frozen_ids = {other.fact_id for other in facts if other.frozen}
            conflicts.extend(
                (fact.fact_id, other) for other in sorted({fact_id for _, _, fact_id in later}) if other not in frozen_ids
            )
            continue
        _, said, other = later[0]
        superseded[fact.fact_id] = (other, said)
    return Succession(superseded, conflicts)


def random_group(rng: random.Random) -> list[GroupFact]:
    """编一组：一条到三十条事实，每条一到四句，说话时间挤在一小段里（同一时刻说的、同一秒两种写法的都有），两成冻结。
    一成的事实和前面一条用同一个编号（写账不会这样传，算法照样要和以前一样）。"""
    facts: list[GroupFact] = []
    for number in rng.sample(range(100), rng.randint(1, 30)):
        moments = {rng.randint(0, 40) for _ in range(rng.randint(1, 4))}
        statements = tuple(sorted((moment, f"t{moment}{rng.choice('ab')}") for moment in moments))
        fact_id = rng.choice(facts).fact_id if facts and rng.random() < 0.1 else f"F{number:02d}"
        facts.append(GroupFact(fact_id, statements, rng.random() < 0.2))
    return facts


def test_succession_is_the_same_as_before() -> None:
    """随机编 3,000 组，新的写法和原来的写法算出来的一样：谁被谁替代、替代的时间、和冻结的冲突，连先后都一样。"""
    rng = random.Random(20261116)
    for _ in range(3000):
        facts = random_group(rng)
        new, old = succession(facts), old_succession(facts)
        assert list(new.superseded.items()) == list(old.superseded.items())
        assert new.conflicts == old.conflicts


def test_succession_without_statements_behaves_as_before() -> None:
    """一句说话时间都没有的事实（写账不会这样传）：组里还有别的句子时两种写法都报错，组里只有它时都算不出新旧。"""
    lonely = [GroupFact("F1", (), False)]
    assert succession(lonely) == old_succession(lonely) == Succession({}, [])
    mixed = [GroupFact("F1", (), False), GroupFact("F2", ((5, "t5"),), False)]
    with pytest.raises(ValueError):
        old_succession(mixed)
    with pytest.raises(ValueError):
        succession(mixed)


def test_succession_with_an_empty_iso_at_the_same_moment() -> None:
    """一句的 ISO 写法是空的（只有手改过的决定文件导进来才会有：冻结的事实说法没了，按它的 as_of 算），又和别的事实
    同一时刻：以前只排比各自晚的句子，碰不上就照常算；现在全组一起排，也不能拿空的去和字符串比。"""
    facts = [GroupFact("F1", ((100, None),), True), GroupFact("F2", ((100, "t100"),), False)]  # type: ignore[arg-type]
    assert succession(facts) == old_succession(facts) == Succession({}, [])
    earlier = [GroupFact("F0", ((50, "t50"),), False), *facts]
    with pytest.raises(TypeError):
        old_succession(earlier)  # 以前也有排到一起就报错的时候
    assert succession(earlier) == Succession({"F0": ("F1", None)}, [])  # type: ignore[arg-type]


class Counted(int):
    """比较一次记一次的整数，拿来当说话时间。"""

    comparisons = 0

    def __hash__(self) -> int:
        return int.__hash__(self)


def _counting(name: str) -> Callable[[Counted, object], bool]:
    compare = getattr(int, name)

    def counted(self: Counted, other: object) -> bool:
        Counted.comparisons += 1
        return compare(self, other)

    return counted


for _name in ("__lt__", "__le__", "__gt__", "__ge__", "__eq__", "__ne__"):
    setattr(Counted, _name, _counting(_name))


def test_succession_does_not_compare_every_pair() -> None:
    """一组 400 条事实，各说一次：原来每一条都和组里每一句比一遍（十几万次），现在排一次序、各自往后二分着找，比较的
    次数和 400 × log 400 一个量级。"""
    facts = [GroupFact(f"F{number:03d}", ((Counted(1000 + number), f"t{number}"),), False) for number in range(400)]
    random.Random(7).shuffle(facts)
    Counted.comparisons = 0
    result = succession(facts)
    assert len(result.superseded) == 399  # 真的算了：除了最后说的那一条，每一条都被下一条替代
    assert Counted.comparisons < 40 * len(facts)


SAMPLES = {"key": "e1", "mention": "蓝鲸那批样机", "name": "蓝鲸那批样机", "type": "订单或合同"}
_HANDOVER = re.compile(r"(\d\d-\d\d) 交货")


def handover_extract(window: Window) -> dict[str, object]:
    """一句“改到 月-日 交货”一条事实：主体、属性都一样，值是日子，一组越积越长。"""
    found = []
    for line in window.targets:
        if (match := _HANDOVER.search(line.text)) is not None:
            day = match.group(1)
            found.append(
                l3_fact(window, line.text.rstrip("。"), f"蓝鲸那批样机交货日期是 {day}", subject="e1", attribute="交货日期", value=day)
            )
    return {"entities": [SAMPLES], "facts": found}


def handover_packets(days: Iterable[int]) -> list[str]:
    return [day_packet(f"2026-10-{day:02d}", f"我：蓝鲸那批样机改到 12-{day:02d} 交货。") for day in days]


def test_superseding_a_long_group_reads_and_writes_little(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """一组已经有 30 条，再写进一条新的日子：重算这一组时，每条事实的说法只读一遍，只写回变了的那一条（上一条现行的被
    新的替代），主体有没有没处理的歧义只查一次。原来说法读两遍、31 条全写回、每条都查一遍歧义。"""
    deliver(world, *handover_packets(range(1, 31)))
    write_ledger(world, handover_extract).close()
    seen: list[str] = []
    calls: list[str] = []
    original = LedgerWriter._supersede

    def traced(self: LedgerWriter, group: str, now: str) -> None:
        calls.append(group)
        self.conn.set_trace_callback(seen.append)
        try:
            original(self, group, now)
        finally:
            self.conn.set_trace_callback(None)

    monkeypatch.setattr(LedgerWriter, "_supersede", traced)
    deliver(world, *handover_packets([31]))
    conn = write_ledger(world, handover_extract)
    try:
        rows = conn.execute("SELECT fact_id, superseded_by FROM facts ORDER BY as_of_epoch").fetchall()
        assert len(rows) == 31 and rows[-2]["superseded_by"] == rows[-1]["fact_id"]  # 新的那条替代了上一条
        assert len(calls) == 1
        reads = [sql for sql in seen if sql.startswith("SELECT * FROM fact_claims WHERE fact_id")]
        writes = [sql for sql in seen if sql.startswith("UPDATE facts SET superseded_by")]
        asks = [sql for sql in seen if "kind = 'ambiguous_entity' AND decided_at IS NULL" in sql]
        assert (len(reads), len(writes), len(asks)) == (31, 1, 1)
    finally:
        conn.close()


def old_supersede(writer: LedgerWriter, group: str, now: str) -> None:
    """改以前的 LedgerWriter._supersede（连同 old_succession），拿来比。"""
    conn = writer.conn
    rows = conn.execute(
        "SELECT fact_id, frozen, subject_entity_id, as_of_epoch, as_of, status, pending_reason, scope_partitions,"
        " scope_sensitive FROM facts WHERE group_key = ? AND withdrawn_at IS NULL",
        (group,),
    ).fetchall()
    members = []
    for row in rows:
        found: dict[int, str] = {}
        for claim in writer._claims_of(row["fact_id"]):
            for proof in json.loads(claim["proofs"]):
                found.setdefault(int(proof["said_epoch"]), str(proof["said_at"]))
        statements = tuple(sorted(found.items()))
        if not statements and row["as_of_epoch"] is not None:
            statements = ((row["as_of_epoch"], row["as_of"]),)
        if statements:
            members.append(GroupFact(row["fact_id"], statements, bool(row["frozen"])))
    result = old_succession(members)
    superseded = dict(result.superseded)
    conflicted: set[str] = set()
    for frozen, newer in result.conflicts:
        decided = writer._frozen_decision(frozen, newer)
        if decided is None:
            conflicted.add(newer)
        elif decided[0] == KEEP_FROZEN and newer not in superseded:
            superseded[newer] = (frozen, decided[1])
    for row in rows:
        fact_id = row["fact_id"]
        claims = [writer._view(claim) for claim in writer._claims_of(fact_id)]
        if claims:
            scope = _scope_of_row(row["scope_partitions"], row["scope_sensitive"])
            status, reason = writer._status(claims, row["subject_entity_id"], set(), scope)
        else:
            status, reason = row["status"], row["pending_reason"]
        if fact_id in conflicted and status != "pending":
            status, reason = "pending", "frozen_conflict"
        by, at = superseded.get(fact_id, (None, None))
        conn.execute(
            "UPDATE facts SET superseded_by = ?, superseded_at = ?, status = ?, pending_reason = ? WHERE fact_id = ?",
            (by, at, status, reason, fact_id),
        )
    for frozen, newer in result.conflicts:
        if newer not in conflicted:
            continue
        if conn.execute(
            "SELECT 1 FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL"
            " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ?",
            (frozen, newer),
        ).fetchone():
            continue
        quotes = [row["sentence_id"] for row in conn.execute("SELECT sentence_id FROM evidence WHERE fact_id = ?", (newer,))]
        conn.execute(
            "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('frozen_conflict', ?, ?, ?)",
            (_json({"frozen": frozen, "new": newer}), _json(quotes), now),
        )
        writer.stats.frozen_conflicts += 1


def assert_the_old_way_changes_nothing(world: World) -> list[list[tuple[object, ...]]]:
    """每一组拿原来的写法再重算一遍（在保存点里，算完撤回）：事实表、待确认一点不变。返回事实表、待确认现在的样子。"""
    conn = world.ledger()
    try:
        writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model="fake-embedding")

        def snapshot() -> list[list[tuple[object, ...]]]:
            return [
                [tuple(row) for row in conn.execute("SELECT * FROM facts ORDER BY fact_id")],
                [tuple(row) for row in conn.execute("SELECT * FROM review_queue ORDER BY review_id")],
            ]

        before = snapshot()
        groups = [row[0] for row in conn.execute("SELECT DISTINCT group_key FROM facts WHERE group_key IS NOT NULL ORDER BY 1")]
        assert groups
        conn.execute("SAVEPOINT again")
        try:
            for group in groups:
                old_supersede(writer, group, "2026-11-16T00:00:00+08:00")
            after = snapshot()
        finally:
            conn.execute("ROLLBACK TO again")
            conn.execute("RELEASE again")
        assert after == before
        return before
    finally:
        conn.close()


def guessed_extract(window: Window) -> dict[str, object]:
    """和 handover_extract 一样，只是日子是单数的那几条是推断的：组里的事实状态有的 verified、有的 inferred。"""
    result = handover_extract(window)
    for fact in result["facts"]:  # type: ignore[union-attr]
        if int(str(fact["value"])[-1]) % 2:  # type: ignore[index]
            fact["certainty"] = "inferred"  # type: ignore[index]
    return result


def test_superseding_gives_the_same_ledger_as_before(world: World) -> None:
    """写完以后，每一组拿原来的写法再重算一遍，账本一点不变：替代、状态、待确认都和新写法写的一样。一组 12 条，有推断的；
    先冻结一条，后来又来了更晚的值（进待确认），再冻结一条更晚说的（两条冻结的，一条在另一条后面说）。"""
    def fact_of(value: str) -> str:
        return str(world.rows("SELECT fact_id FROM facts WHERE value = ?", value)[0]["fact_id"])

    deliver(world, *handover_packets(range(1, 4)))
    write_ledger(world, guessed_extract).close()
    freeze(world.settings, "prod", [fact_of("12-02")])  # 主人用管理命令冻结（J2.12）
    deliver(world, *handover_packets(range(4, 9)))
    write_ledger(world, guessed_extract).close()
    freeze(world.settings, "prod", [fact_of("12-06")])  # 冻结的 12-02 和它的那一对，冻结时关成两边都留
    deliver(world, *handover_packets(range(9, 13)))
    write_ledger(world, guessed_extract).close()
    facts_now, _ = assert_the_old_way_changes_nothing(world)
    assert len(facts_now) == 12
    value = {row["fact_id"]: row["value"] for row in world.rows("SELECT fact_id, value FROM facts")}
    pairs = [(value[row["claims"]["frozen"]], value[row["claims"]["new"]]) for row in reviews(world) if row["kind"] == "frozen_conflict"]
    expected = [("12-02", f"12-{day:02d}") for day in (3, 4, 5, 7, 8, 9, 10, 11, 12)]
    expected += [("12-06", f"12-{day:02d}") for day in range(7, 13)]
    assert sorted(pairs) == sorted(expected)  # 冻结的那两条以后说的值各一对，每一对只进一次；两条冻结的之间不算
    statuses = {row[0] for row in world.rows("SELECT DISTINCT status FROM facts")}
    assert {"pending", "verified", "inferred"} <= statuses


_STOCKTAKE = re.compile(r"去(.)库盘点")


def stocktake_extract(window: Window) -> dict[str, object]:
    """先说“柯远舟就是老柯”“柯远帆也叫老柯”，以后“老柯下周去某库盘点”的老柯对上两个人：主体有歧义，进待确认。"""
    text = window.targets[0].text
    name = "柯远舟" if "柯远舟" in text else "柯远帆" if "柯远帆" in text else "老柯"
    found = [
        l3_fact(window, line.text.rstrip("。"), f"老柯下周去{match.group(1)}库盘点", subject="e1", attribute="盘点的库房",
                value=match.group(1))
        for line in window.targets
        if (match := _STOCKTAKE.search(line.text)) is not None
    ]
    if not found:
        found.append(l3_fact(window, text.rstrip("。"), text.rstrip("。"), subject="e1"))
    return {"entities": [{"key": "e1", "mention": "老柯", "name": name, "type": "人"}], "facts": found}


def test_superseding_a_group_whose_subject_is_ambiguous(world: World) -> None:
    """一组的主体有没处理的歧义（歧义一组只查一次，查到的要用对）：这一组的事实都是待确认，新旧照算；另一个可见范围里
    同样的叫法不算有歧义（D30）；主人定了两边都留以后，这一组不再待确认。每一步都和原来的写法算的一样。"""
    lines = [("2026-11-01", "我：柯远舟就是老柯，管北仓。", "[business]"), ("2026-11-02", "我：柯远帆也叫老柯，管南仓。", "[business]")]
    lines += [(f"2026-11-0{3 + index}", f"我：老柯下周去{place}库盘点。", "[business]") for index, place in enumerate("东西中东")]
    lines += [(f"2026-11-0{7 + index}", f"我：老柯下周去{place}库盘点。", "[investment]") for index, place in enumerate("南北")]
    deliver(world, *[day_packet(day, line, domain=domain) for day, line, domain in lines])
    write_ledger(world, stocktake_extract).close()
    rows = world.rows("SELECT value, status, pending_reason, superseded_by, scope_partitions FROM facts WHERE attribute = '盘点的库房'")
    business = [row for row in rows if "business" in row["scope_partitions"]]
    other = [row for row in rows if "investment" in row["scope_partitions"]]
    assert len(business) == 3 and len(other) == 2  # “东”说了两次，是同一条
    assert {(row["status"], row["pending_reason"]) for row in business} == {("pending", "ambiguous_entity")}
    assert [row["superseded_by"] is None for row in sorted(business, key=lambda row: row["value"])].count(True) == 1
    assert all(row["status"] != "pending" for row in other)
    assert_the_old_way_changes_nothing(world)
    ambiguity = [row for row in reviews(world) if row["kind"] == "ambiguous_entity"]
    assert ambiguity
    decide(world.settings, "prod", int(ambiguity[0]["review_id"]), "both")
    assert {row["status"] for row in world.rows("SELECT status FROM facts WHERE attribute = '盘点的库房'")} == {"verified"}
    assert_the_old_way_changes_nothing(world)
