"""不用金标准也能看的四个数（U3 校准工具包，`javmem selfcheck`）。

拿编的小套件照录好的回复写好账（不调模型、假向量），四样各出一个数：两遍一样不一样、证据在原文里找得到的比例、
合在一起的事实里日子编号金额对不上的、同一样东西几个名字。再在数据目录的拷贝上造出对不上的情形，数要跟着变。
输出只有数和编号，不带原文、不带名字。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from javmem.cli import main
from javmem.pipeline.merge import name_key
from javmem.regress.replies import build_store

MINI = Path(__file__).resolve().parent / "fixtures" / "regress-mini"
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
    """编的小套件照录好的回复抽完、写完账的数据目录。只读：要改的测试先拷一份。"""
    root = tmp_path_factory.mktemp("selfcheck")
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


def use(data: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [name for name in os.environ if name.startswith("JAVMEM_")]:
        monkeypatch.delenv(name)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(data))
    return data


def copy_of(written: Path, tmp_path: Path) -> Path:
    target = tmp_path / "data"
    shutil.copytree(written, target, ignore=shutil.ignore_patterns("*.lock"))
    return target


def selfcheck(capsys: pytest.CaptureFixture[str], tmp_path: Path, *args: str) -> tuple[int, str, dict[str, object]]:
    report = tmp_path / "selfcheck.json"
    code = main(["selfcheck", "--library", "test", "--json", str(report), *args])
    out = capsys.readouterr().out
    return code, out, (json.loads(report.read_text(encoding="utf-8")) if report.exists() else {})


def ledger(data: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(data / "ledger" / "test.db")
    conn.row_factory = sqlite3.Row
    return conn


def count(data: Path, sql: str) -> int:
    conn = ledger(data)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def store_counts(store: Path) -> tuple[int, int]:
    """结果库里 L3 原样的事实、关系各几条（每个窗口取最后切的那一份）。"""
    conn = sqlite3.connect(store / "l3.db")
    try:
        rows = conn.execute("SELECT window_id, extraction FROM windows ORDER BY built_at, rowid").fetchall()
    finally:
        conn.close()
    last = {window: json.loads(extraction) for window, extraction in rows if extraction}
    return (sum(len(one.get("facts") or []) for one in last.values()),
            sum(len(one.get("relations") or []) for one in last.values()))


# ---------- 演示数据上四样都出数 ----------


def test_on_the_made_up_data_every_part_gives_a_number(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    use(written, monkeypatch)
    again = tmp_path / "again"
    build_store(MINI / "l3-replies.json", again)  # 录好的回复再建一遍：两遍一字不差
    code, out, report = selfcheck(capsys, tmp_path, "--runs", str(written), str(again))
    assert code == 0
    facts, relations = store_counts(written / "l3")
    assert facts > 0 and relations > 0
    runs = report["runs"]
    assert runs["packets"] == 5
    assert runs["facts"] == {"first": facts, "second": facts, "matched": facts, "same": facts, "matched_rate": 1.0, "same_rate": 1.0}
    assert runs["relations"]["matched"] == runs["relations"]["same"] == relations
    claims = count(written, "SELECT COUNT(*) FROM fact_claims")
    assert report["evidence"]["facts"] == {"checked": claims, "found": claims, "rate": 1.0, "dropped_evidence": 0, "not_checked": 0}
    relation_claims = count(written, "SELECT COUNT(*) FROM relation_claims")
    assert report["evidence"]["relations"]["found"] == report["evidence"]["relations"]["checked"] == relation_claims
    assert report["merged"]["merged"] == count(
        written, "SELECT COUNT(*) FROM (SELECT fact_id FROM fact_claims GROUP BY fact_id HAVING COUNT(*) > 1)")
    assert report["merged"]["flagged"] == 0
    assert report["names"]["entities"] == count(written, "SELECT COUNT(*) FROM entities")
    assert report["names"]["in_proposals"] == 0 and report["names"]["rate"] == 0.0
    for start in ("1. ", "2. ", "3. ", "4. "):
        line = next(line for line in out.splitlines() if line.startswith(start))
        assert any(char.isdigit() for char in line)
    assert "100.0%" in out


def test_without_a_second_run_the_first_part_says_how(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    use(written, monkeypatch)
    code, out, report = selfcheck(capsys, tmp_path)
    assert code == 0
    assert report["runs"] is None
    assert "--runs" in next(line for line in out.splitlines() if line.startswith("1. "))
    assert report["evidence"]["facts"]["rate"] == 1.0


# ---------- 两遍 ----------


def test_a_second_run_that_differs_is_counted(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """第二遍里一条事实的值不一样（还对得上，内容不一样）、一条关系没抽到（对不上）。"""
    use(written, monkeypatch)
    again = tmp_path / "again"
    build_store(MINI / "l3-replies.json", again)
    conn = sqlite3.connect(again / "l3.db")
    try:
        rows = conn.execute("SELECT rowid, extraction FROM windows WHERE extraction IS NOT NULL ORDER BY rowid").fetchall()
        changed_value = dropped_relation = False
        for rowid, text in rows:
            extraction = json.loads(text)
            if not changed_value:
                fact = next((one for one in extraction.get("facts") or [] if one.get("value")), None)
                if fact is not None:
                    fact["value"] = str(fact["value"]) + " 另一个"
                    changed_value = True
            if not dropped_relation and extraction.get("relations"):
                extraction["relations"] = extraction["relations"][1:]
                dropped_relation = True
            conn.execute("UPDATE windows SET extraction = ? WHERE rowid = ?", (json.dumps(extraction, ensure_ascii=False), rowid))
        conn.commit()
    finally:
        conn.close()
    assert changed_value and dropped_relation
    code, _, report = selfcheck(capsys, tmp_path, "--runs", str(written / "l3"), str(again / "l3.db"))
    assert code == 0
    facts, relations = store_counts(written / "l3")
    assert report["runs"]["facts"]["matched"] == facts
    assert report["runs"]["facts"]["same"] == facts - 1
    assert report["runs"]["facts"]["same_rate"] == round(2 * (facts - 1) / (2 * facts), 4)
    assert report["runs"]["relations"]["second"] == relations - 1
    assert report["runs"]["relations"]["matched"] == relations - 1
    assert report["runs"]["relations"]["matched_rate"] == round(2 * (relations - 1) / (2 * relations - 1), 4)


def test_runs_must_be_result_stores(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    use(written, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, _ = selfcheck(capsys, tmp_path, "--runs", str(written), str(empty))
    assert code == 2
    assert "结果库" in out
    code, out, _ = selfcheck(capsys, tmp_path, "--runs", str(written), str(written / "ledger" / "test.db"))
    assert code == 2


# ---------- 证据 ----------


def _packet(conn: sqlite3.Connection) -> tuple[str, int]:
    row = conn.execute("SELECT packet_id, revision FROM packets WHERE state = 'current' AND withdrawn_at IS NULL"
                       " ORDER BY packet_id LIMIT 1").fetchone()
    return str(row[0]), int(row[1])


def _quarantine(conn: sqlite3.Connection, kind: str, reason: str) -> None:
    packet_id, revision = _packet(conn)
    conn.execute(
        "INSERT INTO quarantine (packet_id, revision, item_kind, payload, reason, rule_id, rule_version, created_at, window_id)"
        " VALUES (?, ?, ?, '{}', ?, ?, 'test', '2026-11-20T00:00:00+08:00', 'w-test')",
        (packet_id, revision, kind, reason, reason.split(":", 1)[0]))


def test_evidence_that_was_not_found_lowers_the_rate(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    data = use(copy_of(written, tmp_path), monkeypatch)
    conn = ledger(data)
    try:
        _quarantine(conn, "fact", "G1:quote_not_found")  # 引文找不到
        _quarantine(conn, "fact", "L3:unknown_ref")  # 编号认不出：也算找不到
        _quarantine(conn, "fact", "G3:negation")  # 证据找到了，别的规则没过
        _quarantine(conn, "fact", "L3:bad_shape")  # 结构不对，没核证据
        _quarantine(conn, "evidence", "G1:quote_not_found")  # 去掉了一条证据，事实留着
        _quarantine(conn, "relation", "R1:quote_not_found,R3:type_not_allowed")
        _quarantine(conn, "relation", "L5:same_ends")  # 过了核对、写不进去的：证据找到了
        conn.commit()
    finally:
        conn.close()
    claims = count(data, "SELECT COUNT(*) FROM fact_claims")
    relation_claims = count(data, "SELECT COUNT(*) FROM relation_claims")
    code, out, report = selfcheck(capsys, tmp_path)
    assert code == 0
    assert report["evidence"]["facts"] == {"checked": claims + 3, "found": claims + 1, "rate": round((claims + 1) / (claims + 3), 4),
                                           "dropped_evidence": 1, "not_checked": 1}
    assert report["evidence"]["relations"]["checked"] == relation_claims + 2
    assert report["evidence"]["relations"]["found"] == relation_claims + 1
    assert f"事实 {claims + 1} / {claims + 3}" in out


# ---------- 合在一起的事实 ----------


def _add_claim(conn: sqlite3.Connection, fact_id: str, number: int, **changes: str | None) -> None:
    row = dict(conn.execute("SELECT * FROM fact_claims WHERE fact_id = ? ORDER BY claim_id LIMIT 1", (fact_id,)).fetchone())
    row.update(claim_id=f"{row['claim_id']}-extra{number}", **changes)
    columns = ", ".join(row)
    conn.execute(f"INSERT INTO fact_claims ({columns}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def test_merged_facts_whose_dates_codes_or_amounts_disagree_are_listed(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                      capsys: pytest.CaptureFixture[str]) -> None:
    data = use(copy_of(written, tmp_path), monkeypatch)
    conn = ledger(data)
    try:
        facts = [str(row[0]) for row in conn.execute(
            "SELECT fact_id FROM facts WHERE withdrawn_at IS NULL AND fact_id IN"
            " (SELECT fact_id FROM fact_claims GROUP BY fact_id HAVING COUNT(*) = 1) ORDER BY fact_id LIMIT 5")]
        assert len(facts) == 5
        same_day, other_day, other_code, other_amount, month_only = facts
        for fact_id, content, occurred in (
            (same_day, "NL-2207 单子 2031-03-07 收讫，付 4,500 元。", "2031-03-07"),
            (other_day, "货 2031-03-07 到。", "2031-03-07"),
            (other_code, "编号 NL-2207 那张。", None),
            (other_amount, "这一笔 4,500 元。", None),
            (month_only, "这件事在 2031年3月。", "2031-03"),
        ):
            conn.execute("UPDATE fact_claims SET content = ?, occurred_at = ? WHERE fact_id = ?", (content, occurred, fact_id))
        _add_claim(conn, same_day, 1, content="NL-2207 单子 3月7号收讫，付 4500 元。", occurred_at="2031-03-07")
        _add_claim(conn, other_day, 1, content="货 2031-03-14 到。", occurred_at="2031-03-14")
        _add_claim(conn, other_code, 1, content="编号 NL-2208 那张。", occurred_at=None)
        _add_claim(conn, other_amount, 1, content="这一笔 ¥4,200。", occurred_at=None)
        _add_claim(conn, month_only, 1, content="这件事在 3月7号。", occurred_at="2031-03-07")  # 月和那个月的一天：不冲突
        conn.commit()
    finally:
        conn.close()
    code, out, report = selfcheck(capsys, tmp_path)
    assert code == 0
    merged = report["merged"]
    assert merged["merged"] == 5
    assert merged["ids"] == {"dates": [other_day], "codes": [other_code], "amounts": [other_amount]}
    assert (merged["dates"], merged["codes"], merged["amounts"], merged["flagged"]) == (1, 1, 1, 3)
    assert merged["rate"] == 0.6
    line = next(line for line in out.splitlines() if line.startswith("3. "))
    assert "5 条" in line and "3 条" in line
    assert other_day in out


# ---------- 几个名字 ----------


def test_the_same_thing_under_two_names_is_counted(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """账本里一家公司另有一个带公司后缀的实体：认人认事会提议合并，这里算两个实体落在提议里。"""
    data = use(copy_of(written, tmp_path), monkeypatch)
    conn = ledger(data)
    try:
        name, kind = conn.execute("SELECT canonical_name, type FROM entities WHERE type = '公司或机构' ORDER BY canonical_name LIMIT 1").fetchone()
        twin = f"{name}有限公司"
        conn.execute("INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at, name_key)"
                     " VALUES ('E-test-twin', ?, ?, '2026-11-20T00:00:00+08:00', '2026-11-20T00:00:00+08:00', ?)",
                     (twin, kind, name_key(twin)))
        conn.commit()
        total = int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])
    finally:
        conn.close()
    code, out, report = selfcheck(capsys, tmp_path)
    assert code == 0
    names = report["names"]
    assert names["entities"] == total
    assert names["in_proposals"] == 2 and names["proposals"] == 1
    assert names["rate"] == round(2 / total, 4)
    assert len(names["ids"]) == 1 and names["ids"][0].startswith("P")
    assert names["ids"][0] in out


# ---------- 别的 ----------


def test_the_output_has_numbers_and_ids_but_no_text_or_names(written: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    use(written, monkeypatch)
    again = tmp_path / "again"
    build_store(MINI / "l3-replies.json", again)
    code, out, report = selfcheck(capsys, tmp_path, "--runs", str(written), str(again))
    assert code == 0
    conn = ledger(written)
    try:
        words = {str(row[0]) for row in conn.execute("SELECT canonical_name FROM entities")}
        words |= {str(row[0]) for row in conn.execute("SELECT alias FROM aliases")}
        words |= {str(row[0]) for row in conn.execute("SELECT content FROM facts")}
    finally:
        conn.close()
    dumped = json.dumps(report, ensure_ascii=False)
    for word in words:
        if len(word) >= 2 and word != "主人":
            assert word not in out and word not in dumped, "输出里带了名字或者原文"


def test_without_a_ledger_and_without_runs_there_is_nothing_to_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    use(tmp_path / "no-data", monkeypatch)
    code, out, _ = selfcheck(capsys, tmp_path)
    assert code == 2
    assert "账本" in out
