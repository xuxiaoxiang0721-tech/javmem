"""备份和恢复（U2 丙，D205 ⑥⑦⑧⑨）：javmem backup、javmem restore-check，巡检里“备份太久没成功”那一条。

样例都是编的，模型和向量都用假的（javmem extract --fake、javmem write --fake --fake-embeddings）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from helpers import CHAT_ID, REPO_ROOT, SHANGHAI, World, build_packet
from test_admin_graph import graphed  # noqa: F401  夹具：接着内存里的图、整张同步过的账本（撤回清向量那几条测试的那一份）
from test_write import day_packet, deliver

from javmem.backup import MANIFEST, STATUS_FILE
from javmem.cli import main
from javmem.graph.sync import enable
from javmem.intake import intake_lock
from javmem.ledger import open_ledger
from javmem.patrol import run_patrol
from javmem.pipeline.store import ResultStore
from javmem.pipeline.write import WriteLock

P1 = "2026-11-01_2330_desk-bot"
P2 = "2026-11-02_2330_desk-bot"
P3 = "2026-11-03_2330_desk-bot"
SAID = ("越南那批订单 11-15 出货", "老周 9 点到仓库", "报价 12.5 美元", "样品下周寄出")
PACKETS = [
    day_packet("2026-11-01", "我：越南那批订单 11-15 出货。", "我：老周 9 点到仓库。"),
    day_packet("2026-11-02", "我：老周 9 点到仓库。", "我：报价 12.5 美元。"),
]
THIRD = day_packet("2026-11-03", "我：样品下周寄出。")
LEDGER_FILES = ("data/ledger/prod.db", "data/ledger/test.db", "data/l3/l3.db", "data/vectors/vectors.db")


def extract_and_write() -> None:
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0


@pytest.fixture
def stocked(world: World) -> World:
    deliver(world, *PACKETS)
    extract_and_write()
    return world


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                " AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name"
            )
        ]
        return {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in names}
    finally:
        conn.close()


def manifest_of(staging: Path) -> dict:
    return json.loads((staging / MANIFEST).read_text(encoding="utf-8"))


def status_of(world: World) -> dict:
    return json.loads((world.data / "logs" / STATUS_FILE).read_text(encoding="utf-8"))


def backup(staging: Path, *extra: str) -> int:
    return main(["backup", "--to", str(staging), *extra])


def backup_json(staging: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> dict:
    capsys.readouterr()
    assert backup(staging, "--json", *extra) == 0
    return json.loads(capsys.readouterr().out)


def archive_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for folder in ("archive", "archive-test")
        if (root / folder).is_dir()
        for path in (root / folder).rglob("*")
        if path.is_file()
    )


# ---------- javmem backup ----------


def test_every_sqlite_file_is_copied_with_the_online_backup_and_listed(stocked: World, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    manifest = manifest_of(staging)
    assert manifest["format"] == "javmem-backup/1"
    files = manifest["files"]
    for relative in LEDGER_FILES:
        entry = files[relative]
        assert entry["sqlite"]["integrity"] == "ok", relative
        assert entry["sha256"] == sha256(staging / relative)
        assert entry["size"] == (staging / relative).stat().st_size
    live = table_counts(stocked.data / "ledger" / "prod.db")
    assert live["facts"] > 0
    assert table_counts(staging / "data" / "ledger" / "prod.db") == live == files["data/ledger/prod.db"]["sqlite"]["tables"]
    assert files["data/ledger/prod.db"]["sqlite"]["user_version"] == manifest["ledger_code_version"]
    assert archive_files(staging / "data") == archive_files(stocked.data) != []
    for relative in archive_files(stocked.data):
        assert files[f"data/{relative}"]["sha256"] == sha256(stocked.data / relative)
    # 锁、-wal、-shm 不备：在线备份拷出来的是一个完整的库
    assert not [name for name in files if name.endswith((".lock", "-wal", "-shm", "-journal"))]
    assert not [path for path in staging.rglob("*")  # 暂存目录自己的那把锁不算
                if path.name.endswith((".lock", "-wal", "-shm", "-journal")) and path.name != ".javmem-backup.lock"]
    # 空的目录也记着（收件目录、失败目录、.tmp），恢复时建回来
    assert "data/inbox/desk-bot/.tmp" in manifest["dirs"]


def test_commits_still_in_the_wal_are_in_the_backup(stocked: World, tmp_path: Path) -> None:
    """WAL 模式下直接拷文件会漏掉还在 -wal 里的提交（做法第 0 节）：在线备份不会。"""
    ledger = stocked.data / "ledger" / "prod.db"
    writer = sqlite3.connect(ledger, autocommit=True)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO task_runs (task, started_at) VALUES ('made-up-task', '2026-11-19T10:00:00+08:00')")
        plain = tmp_path / "plain.db"
        shutil.copyfile(ledger, plain)  # 只拷库文件，-wal 里的没拷上
        staging = tmp_path / "staging"
        assert backup(staging) == 0
    finally:
        writer.close()

    def has_row(path: Path) -> bool:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
        try:
            return conn.execute("SELECT count(*) FROM task_runs WHERE task = 'made-up-task'").fetchone()[0] == 1
        finally:
            conn.close()

    assert not has_row(plain)
    assert has_row(staging / "data" / "ledger" / "prod.db")


def _hold(world: World, which: str):  # noqa: ANN202
    if which == "intake":
        return intake_lock(world.data)
    if which == "write":
        return WriteLock(world.data)
    return ResultStore(world.data / "l3", exclusive=True)


@pytest.mark.parametrize("which", ["intake", "write", "store"])
def test_gives_up_when_one_of_the_three_locks_stays_taken(
    stocked: World, tmp_path: Path, which: str, capsys: pytest.CaptureFixture[str]
) -> None:
    staging = tmp_path / "staging"
    with _hold(stocked, which):
        assert backup(staging, "--wait", "0") == 3
    assert not (staging / MANIFEST).exists()
    status = status_of(stocked)
    assert status["last_ok_at"] is None
    assert status["last_error"] == "busy"
    assert "锁" in capsys.readouterr().out


def test_waits_for_a_lock_that_is_let_go(stocked: World, tmp_path: Path) -> None:
    lock = WriteLock(stocked.data)
    timer = threading.Timer(1.0, lock.close)
    timer.start()
    try:
        assert backup(tmp_path / "staging", "--wait", "30") == 0
    finally:
        timer.join()
    assert status_of(stocked)["last_ok_at"] is not None


def test_the_backup_lock_is_the_same_one_extraction_takes(stocked: World, tmp_path: Path) -> None:
    """结果库的锁：备份拿着的时候，抽取（ResultStore(exclusive=True)）拿不到。"""
    from javmem.backup import store_lock
    from javmem.pipeline.store import StoreBusy

    with store_lock(stocked.data / "l3"):
        with pytest.raises(StoreBusy):
            ResultStore(stocked.data / "l3", exclusive=True)
    ResultStore(stocked.data / "l3", exclusive=True).close()


def test_targets_that_could_lose_or_leak_data_are_refused(stocked: World, tmp_path: Path) -> None:
    inside = REPO_ROOT / "backup-made-up-target"
    assert backup(inside) == 2
    assert not inside.exists()
    assert backup(stocked.data / "staging") == 2  # 在数据目录里
    assert backup(stocked.data.parent) == 2  # 数据目录在它里面
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("别人的东西", encoding="utf-8")
    assert backup(foreign) == 2  # 不是空的，又不是备份的暂存目录：里面的东西不动
    assert (foreign / "keep.txt").read_text(encoding="utf-8") == "别人的东西"
    assert sorted(path.name for path in foreign.iterdir()) == ["keep.txt"]


def test_one_staging_dir_belongs_to_one_data_dir(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """暂存里数据目录没了的文件要删：另一个数据目录拿它当暂存，就把这一个的备份删了。"""
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    before = (staging / MANIFEST).read_bytes()
    other = tmp_path / "other-data"
    other.mkdir()
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(other))
    assert main(["init"]) == 0
    assert backup(staging) == 2
    assert (staging / MANIFEST).read_bytes() == before
    assert archive_files(staging / "data") == archive_files(stocked.data)


def test_no_data_dir_is_a_config_error(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JAVMEM_DATA_DIR")
    assert backup(tmp_path / "staging") == 2
    monkeypatch.setenv("JAVMEM_DATA_DIR", str(tmp_path / "nowhere"))
    assert backup(tmp_path / "staging") == 2
    assert not (tmp_path / "nowhere").exists()


def test_the_second_backup_copies_only_new_files_and_drops_purged_ones(
    stocked: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    staging = tmp_path / "staging"
    first = backup_json(staging, capsys)
    assert first["files"]["copied"] == first["files"]["plain"] > 0
    assert first["files"]["archive_copied"] == len(archive_files(stocked.data))
    again = backup_json(staging, capsys)
    assert again["files"]["archive_copied"] == 0 and again["files"]["removed"] == 0
    assert again["files"]["copied"] <= 1  # 只有 logs/ 里上一次备份记的那一笔变了
    deliver(stocked, THIRD)
    extract_and_write()
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 0
    third = backup_json(staging, capsys)
    assert third["files"]["archive_copied"] == 1
    assert third["files"]["removed"] == 1  # 彻底删除了的归档，暂存里也删掉
    names = archive_files(staging / "data")
    assert names == archive_files(stocked.data)
    assert not [name for name in names if P1 in name]
    assert [name for name in names if P3 in name]
    assert set(manifest_of(staging)["files"]) >= {f"data/{name}" for name in names}


class PowerCut(BaseException):
    """断电：不是 Exception，备份自己接不住。"""


def test_a_backup_cut_off_halfway_leaves_no_manifest(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清单写到一半、拷到一半断了的暂存没有清单：推异地、恢复都认得出来，下一次备份照常补好。"""
    import javmem.backup as backup_module

    staging = tmp_path / "staging"
    assert backup(staging) == 0
    real = backup_module.sqlite_details

    def cut(path: Path) -> dict:  # 放了锁、拷完库，算完整性那一步断了
        raise PowerCut

    monkeypatch.setattr(backup_module, "sqlite_details", cut)
    with pytest.raises(PowerCut):
        backup(staging)
    assert not (staging / MANIFEST).exists()
    assert restore(staging, tmp_path / "restored") == 2  # 不是做完了的备份
    monkeypatch.setattr(backup_module, "sqlite_details", real)
    assert backup(staging) == 0
    assert (staging / MANIFEST).exists()
    assert not (staging / ".javmem-backup.previous.json").exists()
    assert restore(staging, tmp_path / "restored") == 0


def test_withdrawals_and_decisions_are_exported_without_raw_text(stocked: World, tmp_path: Path) -> None:
    assert main(["withdraw", "--library", "prod", "--packet", P2]) == 0
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    manifest = manifest_of(staging)
    withdrawals = staging / manifest["exports"]["withdrawals"]["prod"]
    records = json.loads(withdrawals.read_text(encoding="utf-8"))
    assert records["schema"] == "javmem-withdrawals/1"
    assert [item["packet_id"] for item in records["packets"]] == [P2]
    text = withdrawals.read_text(encoding="utf-8")
    assert not [said for said in SAID if said in text]
    decisions = json.loads((staging / manifest["exports"]["decisions"]["prod"]).read_text(encoding="utf-8"))
    assert decisions["schema"] == "javmem-owner-decisions/1"
    assert manifest["files"][manifest["exports"]["withdrawals"]["prod"]]["sha256"] == sha256(withdrawals)


def test_config_files_outside_the_data_dir_are_copied(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "owner.yaml").write_text("names: [编的主人]\n", encoding="utf-8")
    (config / "lexicon.yaml").write_text("words: []\n", encoding="utf-8")
    (config / "lexicon.merges.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("JAVMEM_OWNER_FILE", str(config / "owner.yaml"))
    monkeypatch.setenv("JAVMEM_LEXICON_FILE", str(config / "lexicon.yaml"))
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    files = manifest_of(staging)["files"]
    for env, name, source in (
        ("JAVMEM_OWNER_FILE", "owner.yaml", config / "owner.yaml"),
        ("JAVMEM_LEXICON_FILE", "lexicon.yaml", config / "lexicon.yaml"),
        ("JAVMEM_LEXICON_FILE", "lexicon.merges.jsonl", config / "lexicon.merges.jsonl"),
        ("JAVMEM_SOURCES_FILE", "sources.yaml", tmp_path / "sources.yaml"),
        ("JAVMEM_ACCESS_FILE", "access.yaml", tmp_path / "access.yaml"),
    ):
        relative = f"config/{env}/{name}"
        assert (staging / relative).read_bytes() == source.read_bytes()
        assert files[relative]["sha256"] == sha256(source)


def test_a_token_ledger_that_does_not_exist_yet_is_fine(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """token 账第一次调用模型才建：还没有的不算配置找不到；有了的照拷。别的配置设了却找不到，照样报。"""
    ledger = tmp_path / "elsewhere" / "token-ledger.csv"
    monkeypatch.setenv("JAVMEM_TOKEN_LEDGER", str(ledger))
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    ledger.parent.mkdir()
    ledger.write_text("编的表头\n", encoding="utf-8")
    assert backup(staging) == 0
    assert (staging / "config" / "JAVMEM_TOKEN_LEDGER" / "token-ledger.csv").read_bytes() == ledger.read_bytes()
    monkeypatch.setenv("JAVMEM_OWNER_FILE", str(tmp_path / "missing-owner.yaml"))
    assert backup(staging) == 1


def test_output_has_no_raw_text(stocked: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    assert main(["restore-check", "--from", str(staging), "--to", str(tmp_path / "restored")]) == 0
    printed = capsys.readouterr()
    assert not [said for said in SAID if said in printed.out + printed.err]
    report = (tmp_path / "restored" / "restore-check.json").read_text(encoding="utf-8")
    assert not [said for said in SAID if said in report]


def test_a_broken_sqlite_file_is_not_a_good_backup(stocked: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = stocked.data / "made-up" / "broken.db"
    broken.parent.mkdir()
    conn = sqlite3.connect(broken)
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.execute("CREATE INDEX t_a ON t (a)")
    conn.executemany("INSERT INTO t VALUES (?)", [(f"编的第 {number} 行" * 20,) for number in range(400)])
    conn.commit()
    conn.close()
    raw = bytearray(broken.read_bytes())
    page = 4096
    assert len(raw) > page * 6
    raw[page * 2 : page * 4] = bytes((number * 37 + 11) % 256 for number in range(page * 2))  # 第 3、4 页整页写成乱码
    broken.write_bytes(bytes(raw))
    called = tmp_path / "called"
    script = tmp_path / "push.py"
    script.write_text(f"from pathlib import Path\nPath({str(called)!r}).write_text('推了', encoding='utf-8')\n", encoding="utf-8")
    capsys.readouterr()
    assert backup(tmp_path / "staging", "--push", str(script)) == 1
    out = capsys.readouterr().out
    assert "made-up/broken.db" in out and "没推异地" in out
    assert not called.exists()  # 坏了的不推：不把异地好的旧版本挤掉
    status = status_of(stocked)
    assert status["last_ok_at"] is None and status["last_error"] == "integrity"


def test_never_backed_up_is_not_flagged_and_a_stale_backup_is(stocked: World, tmp_path: Path) -> None:
    def stale(hours: float) -> list:
        moment = datetime.now(SHANGHAI) + timedelta(hours=hours)
        return [item for item in run_patrol(stocked.at(moment).settings) if item.kind == "backup_stale"]

    assert stale(1000) == []  # 没备份过的数据目录不报（测试、开发用的不用备）
    assert backup(tmp_path / "staging") == 0
    assert stale(25) == []
    items = stale(27)  # 默认 26 小时，和“处理任务太久没跑”同一个数
    assert len(items) == 1 and items[0].library is None
    with WriteLock(stocked.data):
        assert backup(tmp_path / "staging", "--wait", "0") == 3
    items = stale(27)  # 没成功的那一次不算
    assert len(items) == 1 and "锁" in items[0].message


def test_push_runs_after_the_staging_without_javmem_settings(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JAVMEM_LLM_API_KEY", "made-up-not-a-key")
    record = tmp_path / "pushed.json"
    script = tmp_path / "push.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "staging = Path(sys.argv[1])\n"
        f"Path({str(record)!r}).write_text(json.dumps({{'staging': str(staging), 'manifest': (staging / {MANIFEST!r}).is_file(),"
        " 'env': sorted(name for name in os.environ if name.startswith('JAVMEM_')), 'cwd': os.getcwd()}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    assert backup(staging, "--push", str(script)) == 0
    pushed = json.loads(record.read_text(encoding="utf-8"))
    assert Path(pushed["staging"]).resolve() == staging.resolve()
    assert Path(pushed["cwd"]).resolve() != staging.resolve()  # 在它自己的临时目录里跑，见下面第二轮那一条
    assert pushed["manifest"] is True  # 清单写完了才推
    assert pushed["env"] == []  # JAVMEM_* 一个都不给：推异地用不着密钥
    status = status_of(stocked)
    assert status["pushed"] is True and status["last_ok_at"] is not None


def test_a_failed_push_is_not_a_good_backup(stocked: World, tmp_path: Path) -> None:
    script = tmp_path / "push.py"
    script.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    assert backup(tmp_path / "staging", "--push", str(script)) == 1
    status = status_of(stocked)
    assert status["last_ok_at"] is None and status["last_error"] == "push_failed"


# ---------- javmem restore-check ----------


def restore(source: Path, target: Path, *extra: str) -> int:
    return main(["restore-check", "--from", str(source), "--to", str(target), *extra])


def test_restore_check_puts_the_backup_back_and_everything_matches(stocked: World, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    target = tmp_path / "restored"
    assert restore(staging, target) == 0
    restored = target / "data"
    for relative in ("ledger/prod.db", "l3/l3.db", "vectors/vectors.db"):
        assert table_counts(restored / relative) == table_counts(stocked.data / relative), relative
    assert archive_files(restored) == archive_files(stocked.data)
    for relative in archive_files(restored):  # 归档照旧是只读的
        assert not os.access(restored / relative, os.W_OK)
    assert (restored / "inbox" / "desk-bot" / ".tmp").is_dir()
    report = json.loads((target / "restore-check.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["files"]["mismatched"] == [] and report["files"]["missing"] == [] and report["files"]["extra"] == []
    assert report["patrol"]["same"] is True
    assert set(report["seconds"]) >= {"verify", "copy", "open", "withdraw", "total"}


@pytest.mark.parametrize("damage", ["flip", "missing", "extra", "ledger"])
def test_a_damaged_backup_is_reported_and_not_restored(
    stocked: World, tmp_path: Path, damage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    copy = tmp_path / "copy"
    shutil.copytree(staging, copy)
    one = next(name for name in archive_files(copy / "data"))
    path = copy / "data" / one
    if damage == "flip":
        os.chmod(path, 0o600)
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0x01  # 大小不变，只改一个字节
        path.write_bytes(bytes(raw))
        expect = (f"data/{one}", "sha256 对不上")
    elif damage == "missing":
        os.chmod(path, 0o600)
        path.unlink()
        expect = (f"data/{one}", "缺")
    elif damage == "extra":
        (path.parent / "made-up-extra.md").write_text("编的", encoding="utf-8")
        expect = ("made-up-extra.md", "多")
    else:
        ledger = copy / "data" / "ledger" / "prod.db"
        raw = bytearray(ledger.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        ledger.write_bytes(bytes(raw))
        expect = ("data/ledger/prod.db", "sha256 对不上")
    capsys.readouterr()
    target = tmp_path / "restored"
    assert restore(copy, target) == 1
    out = capsys.readouterr().out
    for piece in expect:
        assert piece in out
    assert not (target / "data" / "ledger").exists()  # 坏了的不往下恢复
    report = json.loads((target / "restore-check.json").read_text(encoding="utf-8"))
    assert report["ok"] is False


def test_what_was_withdrawn_after_the_backup_is_withdrawn_again(stocked: World, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    assert main(["withdraw", "--library", "prod", "--packet", P1]) == 0  # 备份以后才撤回、删掉
    assert main(["purge", "--library", "prod", "--packet", P1, "--yes"]) == 0
    latest = tmp_path / "withdrawals.json"
    assert main(["withdraw", "--library", "prod", "--export", str(latest)]) == 0

    plain = tmp_path / "plain"
    assert restore(staging, plain) == 0  # 不给最新的撤回清单：备份是撤回以前的，包还在
    assert [name for name in archive_files(plain / "data") if P1 in name]

    target = tmp_path / "restored"
    assert restore(staging, target, "--withdrawals", str(latest)) == 0
    conn = sqlite3.connect(f"{(target / 'data' / 'ledger' / 'prod.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT withdrawn_at, purged_at FROM packets WHERE packet_id = ?", (P1,)).fetchall()
        assert rows and all(row[0] is not None and row[1] is not None for row in rows)
        assert conn.execute("SELECT count(*) FROM raw_sentences WHERE packet_id = ?", (P1,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM withdrawn_packets WHERE packet_id = ?", (P1,)).fetchone()[0] == 1
    finally:
        conn.close()
    assert not [name for name in archive_files(target / "data") if P1 in name]
    report = json.loads((target / "restore-check.json").read_text(encoding="utf-8"))
    assert report["withdrawals"]["prod"]["withdrawn"] == [P1]
    assert report["withdrawals"]["prod"]["purged"] == [P1]


def test_the_restored_copy_does_not_point_at_the_old_graph(stocked: World, tmp_path: Path) -> None:
    conn = open_ledger(stocked.settings.library("prod"))
    try:
        enable(conn, "made-up-marker")
    finally:
        conn.close()
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    target = tmp_path / "restored"
    assert restore(staging, target) == 0
    conn = sqlite3.connect(f"{(target / 'data' / 'ledger' / 'prod.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        enabled, marker = conn.execute("SELECT enabled, marker_id FROM graph_state WHERE id = 1").fetchone()
        triggers = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'graph\\_mark\\_%' ESCAPE '\\'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert (enabled, marker, triggers) == (0, None, 0)


def test_patrol_differences_after_a_restore_are_reported(stocked: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """清单自己对得上（有人连同清单一起改了），恢复出来的巡检比备份那时多了一条，也要报。"""
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    one = archive_files(staging / "data")[0]
    os.chmod(staging / "data" / one, 0o600)
    (staging / "data" / one).unlink()
    manifest = manifest_of(staging)
    del manifest["files"][f"data/{one}"]
    (staging / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert restore(staging, tmp_path / "restored") == 1
    out = capsys.readouterr().out
    assert "巡检" in out and "archive_missing" in out


def test_restore_targets_are_checked(stocked: World, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("别人的东西", encoding="utf-8")
    assert restore(staging, busy) == 2
    assert sorted(path.name for path in busy.iterdir()) == ["keep.txt"]
    inside = REPO_ROOT / "restore-made-up-target"
    assert restore(staging, inside) == 2
    assert not inside.exists()
    assert restore(tmp_path / "not-a-backup", tmp_path / "restored") == 2
    assert restore(staging, staging / "inner") == 2
    assert restore(staging, stocked.data / "restored") == 2  # 恢复到现在用的数据目录里
    assert not (stocked.data / "restored").exists()


def test_cli_help_lists_both_commands(capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("backup", "restore-check"):
        with pytest.raises(SystemExit) as stopped:
            main([command, "--help"])
        assert stopped.value.code == 0
    assert "--to" in capsys.readouterr().out


# ---------- 审阅查出的（11-19）----------


def export_latest(tmp_path: Path, library: str = "prod") -> Path:
    latest = tmp_path / f"latest-{library}.json"
    assert main(["withdraw", "--library", library, "--export", str(latest)]) == 0
    return latest


def readonly_ledger(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def link_dir(link: Path, target: Path) -> None:
    """目录链接：Windows 上是联接（mklink /J，不要管理员），别的系统是符号链接。"""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


def test_an_old_revision_purged_after_the_backup_is_purged_again(world: World, tmp_path: Path) -> None:
    """备份以后单删了替换下来的旧版本（旧版里有没打码的东西）：恢复时照最新的清单再删这一版。"""
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：旧版里编的口令 made-up-1234。\n"))
    world.intake()
    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：新版打过码了。\n"))
    world.intake()
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    old = Path("archive") / "2026" / "11" / f"{CHAT_ID}.r1.md"
    assert (staging / "data" / old).exists()
    assert main(["purge", "--library", "prod", "--packet", CHAT_ID, "--revision", "1", "--yes"]) == 0
    latest = export_latest(tmp_path)
    revisions = json.loads(latest.read_text(encoding="utf-8"))["revisions"]
    assert [(item["packet_id"], item["revision"]) for item in revisions] == [(CHAT_ID, 1)]
    target = tmp_path / "restored"
    assert restore(staging, target, "--withdrawals", str(latest)) == 0
    assert not (target / "data" / old).exists()
    conn = readonly_ledger(target / "data" / "ledger" / "prod.db")
    try:
        assert conn.execute("SELECT purged_at FROM packets WHERE packet_id = ? AND revision = 1", (CHAT_ID,)).fetchone()[0]
        assert conn.execute("SELECT purged_at FROM packets WHERE packet_id = ? AND revision = 2", (CHAT_ID,)).fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM raw_sentences WHERE packet_id = ? AND revision = 1", (CHAT_ID,)).fetchone()[0] == 0
    finally:
        conn.close()
    report = json.loads((target / "restore-check.json").read_text(encoding="utf-8"))
    assert report["withdrawals"]["prod"]["revisions_purged"] == [f"{CHAT_ID}#1"]


def test_an_old_revision_that_is_still_current_in_the_backup_is_reported(world: World, tmp_path: Path) -> None:
    """备份那时第 1 版还是现行的，后来才来了第 2 版、删了第 1 版：恢复出来的那一份里第 1 版是现行的，不能单删，要报。"""
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：旧版里编的口令 made-up-5678。\n"))
    world.intake()
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：新版打过码了。\n"))
    world.intake()
    assert main(["purge", "--library", "prod", "--packet", CHAT_ID, "--revision", "1", "--yes"]) == 0
    latest = export_latest(tmp_path)
    assert restore(staging, tmp_path / "restored", "--withdrawals", str(latest)) == 1
    report = json.loads((tmp_path / "restored" / "restore-check.json").read_text(encoding="utf-8"))
    assert [problem for problem in report["problems"] if CHAT_ID in problem and "现行" in problem]


def test_the_side_store_is_swept_and_forgets_the_checkup_db_after_restore(world: World, tmp_path: Path) -> None:
    """支线的库（side.db）：备份以后彻底删除的原文，恢复时照清单在本机删掉；恢复出来的支线库忘了正在用的体检库的标记，
    不然在它上面扫一遍会连上正在用的体检库，把备份以后跑出来的组当没主的删掉。"""
    from test_side import PACKET_ID as SIDE_PACKET
    from test_side import FakeGraph, _packet, _ran

    from javmem.side.store import SideStore

    _packet(world)
    with SideStore.open(world.data, "prod") as store:
        _ran(world, store, FakeGraph())
        store.set_meta("marker_id", "made-up-checkup-marker")
        assert store.groups()
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    assert main(["withdraw", "--library", "prod", "--packet", SIDE_PACKET]) == 0
    assert main(["purge", "--library", "prod", "--packet", SIDE_PACKET, "--yes"]) == 0
    latest = export_latest(tmp_path)
    target = tmp_path / "restored"
    assert restore(staging, target, "--withdrawals", str(latest)) == 0
    side_db = target / "data" / "side" / "prod" / "side.db"
    conn = sqlite3.connect(f"{side_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT count(*) FROM lines").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM meta WHERE key = 'marker_id'").fetchone()[0] == 0
    finally:
        conn.close()
    report = json.loads((target / "restore-check.json").read_text(encoding="utf-8"))
    assert report["detached"] and "side:prod" in report["detached"]


def test_a_packet_waiting_in_the_inbox_and_purged_later_does_not_come_back(
    stocked: World, tmp_path: Path
) -> None:
    stocked.drop("inbox/desk-bot", THIRD)  # 投进来了，备份那时还没收
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    assert [path for path in (staging / "data" / "inbox").rglob("*.md") if P3 in path.name]
    stocked.intake()
    extract_and_write()
    assert main(["withdraw", "--library", "prod", "--packet", P3]) == 0
    assert main(["purge", "--library", "prod", "--packet", P3, "--yes"]) == 0
    latest = export_latest(tmp_path)
    target = tmp_path / "restored"
    assert restore(staging, target, "--withdrawals", str(latest)) == 0
    left = [path for path in (target / "data").rglob("*.md") if P3 in path.name]
    assert left == []
    for path in (target / "exports").glob("*.json"):  # 导出的那几份照恢复、再删一遍以后的账本重导
        assert "样品下周寄出" not in path.read_text(encoding="utf-8")


def test_a_link_inside_the_data_dir_is_reported_and_keeps_what_was_staged(
    stocked: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    staged = archive_files(staging / "data")
    elsewhere = tmp_path / "elsewhere-archive"
    shutil.move(str(stocked.data / "archive"), str(elsewhere))
    link_dir(stocked.data / "archive", elsewhere)
    capsys.readouterr()
    assert backup(staging) == 1
    assert "链接" in capsys.readouterr().out
    assert archive_files(staging / "data") == staged  # 暂存里早先拷好的不删


def test_a_folder_that_cannot_be_listed_stops_the_backup(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    manifest = (staging / MANIFEST).read_bytes()
    staged = archive_files(staging / "data")
    real = os.scandir

    def flaky(path: object = ".") -> object:
        if Path(str(path)).name == "archive" and Path(str(path)).parent == stocked.data:
            raise PermissionError(13, "made-up: cannot list")
        return real(path)

    monkeypatch.setattr(os, "scandir", flaky)
    assert backup(staging) == 5
    monkeypatch.undo()
    assert archive_files(staging / "data") == staged
    assert (staging / MANIFEST).read_bytes() == manifest  # 还没动暂存就停了：上一份清单照旧能用


def test_a_file_that_cannot_be_copied_is_not_taken_as_gone(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拷不过去（比如暂存那边的路径太长，Windows 报的错和“文件没了”是同一种）：源文件还在就不能当成打包方拿走了。"""
    import javmem.backup as backup_module

    real = backup_module.shutil.copyfile

    def too_long(source: object, target: object, *args: object, **kwargs: object) -> object:
        if "archive" in str(source):
            raise FileNotFoundError(2, "made-up: the path is too long")
        return real(source, target, *args, **kwargs)

    monkeypatch.setattr(backup_module.shutil, "copyfile", too_long)
    assert backup(tmp_path / "staging") == 5
    assert status_of(stocked)["last_ok_at"] is None


def test_a_busy_backup_keeps_the_last_good_manifest(stocked: World, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    before = (staging / MANIFEST).read_bytes()
    with WriteLock(stocked.data):
        assert backup(staging, "--wait", "0") == 3
    assert (staging / MANIFEST).read_bytes() == before
    assert restore(staging, tmp_path / "restored") == 0


def test_config_files_inside_the_data_dir_are_used_after_restore(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inside = stocked.data / "made-up-config" / "sources.yaml"
    inside.parent.mkdir()
    shutil.copyfile(tmp_path / "sources.yaml", inside)
    monkeypatch.setenv("JAVMEM_SOURCES_FILE", str(inside))
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    assert restore(staging, tmp_path / "restored") == 0  # 巡检不会多一条“来源登记表读不了”


def test_a_staged_copy_that_went_bad_is_copied_again(stocked: World, tmp_path: Path) -> None:
    """暂存里的副本在原地坏了（大小、修改时间都没变）：下一次备份照源文件的内容认出来、重拷，清单照源文件的算。"""
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    one = archive_files(staging / "data")[0]
    staged = staging / "data" / one
    info = staged.stat()
    raw = bytearray(staged.read_bytes())
    raw[0] ^= 0x01
    staged.write_bytes(bytes(raw))
    os.utime(staged, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert backup(staging) == 0
    assert staged.read_bytes() == (stocked.data / one).read_bytes()
    assert manifest_of(staging)["files"][f"data/{one}"]["sha256"] == sha256(stocked.data / one)


def test_two_backups_into_one_staging_at_the_same_time(stocked: World, tmp_path: Path) -> None:
    from javmem.backup import staging_lock

    staging = tmp_path / "staging"
    assert backup(staging) == 0
    with staging_lock(staging):
        assert backup(staging, "--wait", "0") == 3


def test_files_are_copied_while_the_three_locks_are_held(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import javmem.backup as backup_module
    from javmem.pipeline.write import WriteBusy

    real = backup_module._copy_plain
    seen: list[str] = []

    def checking(source: Path, target: Path) -> bool:
        try:
            WriteLock(stocked.data).close()
            seen.append("free")
        except WriteBusy:
            seen.append("held")
        return real(source, target)

    monkeypatch.setattr(backup_module, "_copy_plain", checking)
    assert backup(tmp_path / "staging") == 0
    assert seen and set(seen) == {"held"}


def test_patrol_items_that_need_the_intake_lock_are_recorded_too(stocked: World, tmp_path: Path) -> None:
    """对账那几项（要拿收件锁的）也记进清单：备份拿着收件锁，巡检自己会跳过它们。"""
    orphan = stocked.data / "archive" / "2026" / "11" / "2026-11-30_2330_desk-bot.md"
    orphan.write_text("编的，账上没有\n", encoding="utf-8")
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    kinds = [item["kind"] for item in manifest_of(staging)["patrol"]["items"]]
    assert "archive_orphan" in kinds
    assert restore(staging, tmp_path / "restored") == 0


def test_a_staging_whose_data_folder_is_a_link_is_refused(stocked: World, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("别人的东西", encoding="utf-8")

    def writable(function, name, _):  # noqa: ANN001, ANN202
        os.chmod(name, 0o700)
        function(name)

    shutil.rmtree(staging / "data", onexc=writable)
    link_dir(staging / "data", elsewhere)
    assert backup(staging) == 2
    assert (elsewhere / "keep.txt").read_text(encoding="utf-8") == "别人的东西"


def test_a_full_disk_while_copying_a_database_is_not_called_a_broken_database(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import javmem.backup as backup_module

    def full(source: Path, target: Path) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backup_module, "online_backup", full)
    assert backup(tmp_path / "staging") == 5
    assert status_of(stocked)["last_error"] == "error"


def test_a_status_that_cannot_be_written_is_not_a_good_backup(stocked: World, tmp_path: Path) -> None:
    (stocked.data / "logs" / STATUS_FILE).mkdir(parents=True)  # 记那一笔的地方被一个目录占着
    assert backup(tmp_path / "staging") == 1


def test_withdrawal_lists_must_say_which_library(stocked: World, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    bad = tmp_path / "no-library.json"
    bad.write_text(json.dumps({"schema": "javmem-withdrawals/1", "packets": [], "sentences": []}), encoding="utf-8")
    assert restore(staging, tmp_path / "restored", "--withdrawals", str(bad)) == 2


def test_the_restored_copy_forgets_the_graph_even_when_the_restore_stops_halfway(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import javmem.backup as backup_module

    conn = open_ledger(stocked.settings.library("prod"))
    try:
        enable(conn, "made-up-marker")
    finally:
        conn.close()
    staging = tmp_path / "staging"
    assert backup(staging) == 0

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("made-up: the disk went away")

    monkeypatch.setattr(backup_module, "_compare_patrol", broken)
    target = tmp_path / "restored"
    assert restore(staging, target) == 1
    conn = readonly_ledger(target / "data" / "ledger" / "prod.db")
    try:
        assert conn.execute("SELECT enabled, marker_id FROM graph_state WHERE id = 1").fetchone() == (0, None)
    finally:
        conn.close()


def test_a_file_that_became_a_folder_does_not_break_later_backups(stocked: World, tmp_path: Path) -> None:
    odd = stocked.data / "logs" / "made-up-note"
    odd.parent.mkdir(exist_ok=True)
    odd.write_text("编的", encoding="utf-8")
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    odd.unlink()
    odd.mkdir()
    (odd / "inside.txt").write_text("编的", encoding="utf-8")
    assert backup(staging) == 0
    shutil.rmtree(odd)
    odd.write_text("又是文件", encoding="utf-8")
    assert backup(staging) == 0
    assert (staging / "data" / "logs" / "made-up-note").read_text(encoding="utf-8") == "又是文件"


def test_patrol_flags_a_backup_that_stopped_being_pushed(stocked: World, tmp_path: Path) -> None:
    """推过异地的数据目录，后来的备份都没推（比如按时跑的那一行漏了 --push）：照样报。"""
    from javmem.backup import backup as run_backup

    script = tmp_path / "push.py"
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    start = datetime(2026, 11, 19, 1, 0, tzinfo=SHANGHAI)
    staging = tmp_path / "staging"
    assert run_backup(stocked.at(start).settings, staging, push=str(script)).ok
    assert run_backup(stocked.at(start + timedelta(hours=30)).settings, staging).ok  # 没推
    items = [item for item in run_patrol(stocked.at(start + timedelta(hours=31)).settings) if item.kind == "backup_stale"]
    assert len(items) == 1 and "推" in items[0].message
    assert run_backup(stocked.at(start + timedelta(hours=32)).settings, staging, push=str(script)).ok
    assert [item for item in run_patrol(stocked.at(start + timedelta(hours=33)).settings) if item.kind == "backup_stale"] == []


def test_checks_skipped_is_not_compared_after_restore(stocked: World, tmp_path: Path) -> None:
    """巡检“这一回没看的那几种”（checks_skipped）不比：备份拿着收件锁跑巡检，对账那几样一定是“没看”的（intake_busy），
    恢复出来的那一份没人拿着锁、照常看，两边这一条一定对不上。清单里记着它也不算巡检多了少了。"""
    from javmem.backup import PATROL_NOT_COMPARED
    from javmem.patrol import CHECKS_SKIPPED

    assert CHECKS_SKIPPED in PATROL_NOT_COMPARED
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    manifest = manifest_of(staging)
    manifest["patrol"]["items"].append({"kind": CHECKS_SKIPPED, "message": "编的：收件锁被占着，这几样这回没看",
                                        "reason": "intake_busy", "skipped": ["archive_orphan"]})
    (staging / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert restore(staging, tmp_path / "restored") == 0
    report = json.loads((tmp_path / "restored" / "restore-check.json").read_text(encoding="utf-8"))
    assert report["patrol"]["same"] is True


# ---------- 第二轮审阅查出的（11-19 晚）----------


def test_a_push_program_that_reads_the_whole_staging_is_not_stopped_by_the_lock(stocked: World, tmp_path: Path) -> None:
    """暂存目录的锁锁的是锁文件远处的一个字节：推异地的程序把暂存整个拷走、打包，读得了那个锁文件。"""
    script = tmp_path / "push.py"
    bundle = tmp_path / "pushed.zip"
    script.write_text(
        "import sys, zipfile\nfrom pathlib import Path\n"
        f"with zipfile.ZipFile({str(bundle)!r}, 'w') as out:\n"
        "    for path in Path(sys.argv[1]).rglob('*'):\n"
        "        if path.is_file():\n"
        "            out.writestr(str(path.relative_to(sys.argv[1])), path.read_bytes())  # 每个文件都真读一遍\n",
        encoding="utf-8",
    )
    assert backup(tmp_path / "staging", "--push", str(script)) == 0
    with zipfile.ZipFile(bundle) as packed:
        assert MANIFEST in packed.namelist()


def test_the_push_program_runs_in_a_folder_of_its_own(stocked: World, tmp_path: Path) -> None:
    """推异地的程序在一个用完就删的临时目录里跑：它用相对路径写的临时文件不进仓库，也不进暂存（不然下一次跟着推出去）。"""
    record = tmp_path / "cwd.json"
    script = tmp_path / "push.py"
    script.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path('made-up-temp.txt').write_text('编的', encoding='utf-8')\n"
        f"Path({str(record)!r}).write_text(json.dumps(os.getcwd()), encoding='utf-8')\n",
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    assert backup(staging, "--push", str(script)) == 0
    cwd = Path(json.loads(record.read_text(encoding="utf-8"))).resolve()
    assert staging.resolve() not in (cwd, *cwd.parents)
    assert REPO_ROOT.resolve() not in (cwd, *cwd.parents)
    assert not cwd.exists()  # 用完删了
    assert not list(staging.rglob("made-up-temp.txt"))


def test_inbox_packets_are_read_with_the_real_header_parser(world: World, tmp_path: Path) -> None:
    """收件目录里还没收的包照真的包头解析器认第几版：`revision: 2  # 注释` 是第 2 版，清单里删的是第 1 版，它不能删。"""
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：第一版编的话。\n"))
    world.intake()
    world.drop("inbox/desk-bot", build_packet(revision="2  # 编的注释", body="## 原话\n- [10:30] 我：第二版编的话。\n"))
    staging = tmp_path / "staging"
    assert backup(staging) == 0  # 第 2 版还在收件目录里，没收
    world.intake()
    assert main(["purge", "--library", "prod", "--packet", CHAT_ID, "--revision", "1", "--yes"]) == 0
    latest = export_latest(tmp_path)
    target = tmp_path / "restored"
    assert restore(staging, target, "--withdrawals", str(latest)) == 1  # 恢复出来的第 1 版还是现行的：单删不了，报
    assert [path for path in (target / "data" / "inbox").rglob("*.md") if CHAT_ID in path.name]  # 没收的第 2 版留着


def test_an_inbox_packet_written_with_a_bom_is_recognised(stocked: World, tmp_path: Path) -> None:
    stocked.drop("inbox/desk-bot", b"\xef\xbb\xbf" + THIRD.encode("utf-8"), name=f"{P3}.md")
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    stocked.intake()
    extract_and_write()
    assert main(["withdraw", "--library", "prod", "--packet", P3]) == 0
    assert main(["purge", "--library", "prod", "--packet", P3, "--yes"]) == 0
    target = tmp_path / "restored"
    assert restore(staging, target, "--withdrawals", str(export_latest(tmp_path))) == 0
    assert not [path for path in (target / "data").rglob("*.md") if P3 in path.name]


def test_a_restore_that_stops_while_copying_still_forgets_the_graphs(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import javmem.backup as backup_module
    from test_side import FakeGraph, _packet, _ran

    from javmem.side.store import SideStore

    _packet(stocked)
    with SideStore.open(stocked.data, "prod") as store:
        _ran(stocked, store, FakeGraph())
        store.set_meta("marker_id", "made-up-checkup-marker")
    conn = open_ledger(stocked.settings.library("prod"))
    try:
        enable(conn, "made-up-marker")
    finally:
        conn.close()
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    real = backup_module.shutil.copyfile

    def full(source: object, target: object, *args: object, **kwargs: object) -> object:
        if "vectors" in str(source):  # 账本、支线的库拷完了，拷向量缓存时盘满了
            raise OSError(28, "made-up: no space left on device")
        return real(source, target, *args, **kwargs)

    monkeypatch.setattr(backup_module.shutil, "copyfile", full)
    target = tmp_path / "restored"
    assert restore(staging, target) == 1
    monkeypatch.undo()
    conn = readonly_ledger(target / "data" / "ledger" / "prod.db")
    try:
        assert conn.execute("SELECT enabled, marker_id FROM graph_state WHERE id = 1").fetchone() == (0, None)
    finally:
        conn.close()
    side = sqlite3.connect(f"{(target / 'data' / 'side' / 'prod' / 'side.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert side.execute("SELECT count(*) FROM meta WHERE key = 'marker_id'").fetchone()[0] == 0
    finally:
        side.close()


def test_relation_vectors_of_a_graphed_ledger_are_kept_after_restore(graphed: tuple[World, object], tmp_path: Path) -> None:
    """接着主图的账本：恢复时再删一遍（清向量缓存）是在忘了图以前做的，现在每条关系拿来算向量的那一句照样留着（J5.6）。"""
    from dataclasses import replace

    from test_admin_graph import cached, relation_texts

    world, _ = graphed
    before = relation_texts(world)
    assert before and cached(world, before) == before
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    target = tmp_path / "restored"
    assert restore(staging, target) == 0
    restored = World(target / "data", replace(world.settings, data_dir=target / "data"))
    assert cached(restored, before) == before


def test_a_folder_in_the_staging_where_a_database_now_is(stocked: World, tmp_path: Path) -> None:
    odd = stocked.data / "made-up" / "later.db"
    odd.mkdir(parents=True)
    (odd / "inside.txt").write_text("编的", encoding="utf-8")
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    shutil.rmtree(odd)
    conn = sqlite3.connect(odd)
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.execute("INSERT INTO t VALUES ('编的')")
    conn.commit()
    conn.close()
    assert backup(staging) == 0
    assert table_counts(staging / "data" / "made-up" / "later.db") == {"t": 1}


def test_a_side_store_that_cannot_be_opened_is_reported_not_a_crash(stocked: World, tmp_path: Path) -> None:
    side = stocked.data / "side" / "prod" / "side.db"
    side.parent.mkdir(parents=True)
    conn = sqlite3.connect(side)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema', '999'), ('library', 'prod')")  # 别的版本的支线做的
    conn.commit()
    conn.close()
    staging = tmp_path / "staging"
    assert backup(staging) == 0
    target = tmp_path / "restored"
    assert restore(staging, target) == 1
    report = json.loads((target / "restore-check.json").read_text(encoding="utf-8"))
    assert [problem for problem in report["problems"] if "支线" in problem]


def test_the_staging_owner_is_checked_again_once_the_lock_is_held(
    stocked: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拿到暂存目录的锁以后再核一遍是谁的：两个数据目录同时往一个新的暂存目录备份，后拿到锁的那个不能把前一个的删了。"""
    import javmem.backup as backup_module

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / ".javmem-backup-staging").write_text(json.dumps({"data_dir": str(tmp_path / "another-data")}), encoding="utf-8")
    (staging / "data").mkdir()
    (staging / "data" / "keep.txt").write_text("另一个数据目录的", encoding="utf-8")
    monkeypatch.setattr(backup_module, "_empty_or_ours", lambda *args: None)  # 当成开头那一遍核的时候它还是空的
    assert backup(staging) == 2
    assert (staging / "data" / "keep.txt").read_text(encoding="utf-8") == "另一个数据目录的"


def test_wait_is_one_deadline_for_all_the_locks(stocked: World, tmp_path: Path) -> None:
    """--wait 是一共等多久：暂存目录的锁等了一阵，剩下的三把只等剩下的那些。"""
    import time

    from javmem.backup import staging_lock

    staging = tmp_path / "staging"
    assert backup(staging) == 0
    held = staging_lock(staging)
    held.__enter__()
    timer = threading.Timer(1.5, lambda: held.__exit__(None, None, None))
    timer.start()
    try:
        with WriteLock(stocked.data):
            started = time.monotonic()
            assert backup(staging, "--wait", "2") == 3
            seconds = time.monotonic() - started
    finally:
        timer.join()
    assert seconds < 3.5
