"""备份和恢复（U2 丙，D205 ⑥⑦⑧⑨，做法第 4 节）：`javmem backup` 拷一份一致的暂存、记清单；`javmem restore-check`
核过清单以后恢复到一个新目录，照撤回清单再删一遍。

**javmem backup --to 暂存目录 [--wait 秒] [--push 程序]**（数据目录照 JAVMEM_DATA_DIR）：

1. **拿锁：** 先拿暂存目录自己的锁（同一个暂存目录同一时间只有一个备份；锁的是锁文件远处的一个字节，推异地的程序照样读得了
   这个文件），拿到以后再核一遍暂存目录是不是这个数据目录的；再拿和撤回一样的三把（收件、写账、L3 结果库）。拿不到每 2 秒
   再试，--wait（默认 300 秒）是一共等多久，过了还拿不到就退出（退出码 3），暂存一个字不动。结果库的锁只拿锁文件
   （store_lock，和 ResultStore(exclusive=True) 同一把），不打开结果库：打开会建库、升表。
2. **锁着的时候拷：**
   - 先把数据目录整个列一遍（只读）：哪个目录列不出来就停（退出码 5），不当成没了；链接、联接不跟进去，报出来（退出码 1，
     这一次不推异地，暂存里原来拷好的那一块留着）。列完了才动暂存：上一份清单先挪开，写到一半断了的暂存没有清单，推异地、
     恢复都认得出来。
   - 数据目录里的 SQLite 文件（看文件头认，不看名字：以后多了一个库也备得上）用 SQLite 的在线备份接口拷：WAL 模式下
     直接拷文件，会漏掉还在 -wal 里的提交。拷的时候盘满了、读写出错算出错，库本身坏得拷不出来算完整性没过。
   - 别的文件（归档、收件目录、失败目录、logs）照内容比：和暂存里那一份的 sha256 一样就不拷（归档只增不改，平时只拷新的；
     暂存里那一份在盘上坏了也认得出来）；拷不过去而源文件还在的算出错，不当成打包方拿走了。暂存里有、数据目录里没了的删掉，
     彻底删除了的归档不会跟着留在下一份里；文件变成目录、目录变成文件的照新的换。锁文件、-wal、-shm、.tmp 里写到一半的
     不拷，目录都记下来（空的也记，恢复时建回来）。
   - 配置（来源登记、身份、主人的名字、别名表连同旁边的合并记录、打码表，放在数据目录外的 token 账）照环境变量拷进
     `config/<环境变量名>/`，清单记着原来在哪；放在数据目录里的跟着数据目录备，清单记着相对路径。
   - 照这一刻的样子跑一遍巡检，记进清单，恢复时比（“巡检没有新的警告”）。
   归档、配置也在锁着的时候拷：放了锁再拷，中间收进来的包在恢复出来的那份里账上没有、归档里有，巡检就多出一条。
3. **放锁以后：** 拷出来的库旁边留下的 -wal、-shm 收掉；从拷出来的账本导出撤回清单（只有包号、指纹和彻底删除过的每一版，
   和 `withdraw --export` 的一样）、冻结和主人的决定（和 `review --export` 的一样，有原文）放进 `exports/`；每个库做完整性
   检查（integrity_check）、数每张表几行；记每个文件的大小和 sha256，最后写清单 `javmem-backup.json`。拷出来的账本不是
   最新版的，导出在另一份拷贝上做，暂存里的那份一个字不动。
4. **--push：** 清单写完以后跑推到异地的程序（仓库外：地方、密码都是私人的，javmem 不管），参数是暂存目录，在一个用完就删的
   临时目录里跑；`.py` 用跑 javmem 的这个 Python。`JAVMEM_*` 一个都不给（用不着密钥）。退出码不是 0 算这一次没成功。
   有库完整性检查没过、数据目录里有链接没备上的不推：免得新的一份把异地好的旧版本挤掉。
5. **记一笔：** 数据目录 `logs/backup-status.json`（巡检读它，见 `javmem.patrol` 的 backup_stale）：这一次什么时候、成没成、
   为什么没成（只有种类）、上一次推成异地是什么时候。参数、设置不对（退出码 2）的不记；记不上的这一次也算没成功。

主图（Neo4j）不备（D205 ⑧）：坏了从账本整张重建。打出来的只有文件数、大小、编号，不打内容。

**javmem restore-check --from 快照目录 --to 新目录 [--withdrawals 撤回清单 …]**：`--from` 是 `javmem backup` 的暂存（推到
异地以后取回来的那一份）。

1. **核：** 清单里每个文件在不在、大小和 sha256 对不对，有没有清单里没有的文件；每个库做完整性检查、每张表的行数和清单比。
   有一处不对就报出来（文件名、哪一样不对），不往下恢复，退出码 1。
2. **恢复：** 拷到 `--to`：`--to/data` 是恢复出来的数据目录，`config/`、`exports/` 照暂存的样子；归档照旧只读、修改时间照旧。
3. **打开：** javmem 打开每本账本（旧的升到最新）。
4. **比巡检：** 用暂存里的配置（数据目录里的用恢复出来的那一份），钟拨到备份那一刻，跑一遍巡检，和清单里记的比（磁盘还剩
   多少、这一回没看的那几种不比）：多了、少了都报。
5. **再删一遍：** 照撤回清单（`--withdrawals`，要写明是哪个库的；不给就用这一份备份自己导出的）把备份以后才撤回、彻底删除的
   再撤回、再彻底删除一遍，单删过的旧版本再单删（这一份里它还是现行的，删不了，报出来），收件目录里还没收的那几个包也删掉；
   支线的库在本机扫一遍（不连体检库），第二证人的待确认按账本的现状删；`--to/exports` 照删完以后的账本重导。备份以后删掉的
   东西，不能跟着备份回来。
6. **不接图：** 删完以后（拷到一半出了错也一样）让恢复出来的那一份忘了原来的图：账本忘了主图（和 `graph init --reset`
   账本这一边一样），支线的库忘了体检库的标记。不然拿它同步、扫，会动到正在用的图。要接图，照开发说明换一个新图、
   `graph init`、整张同步。
7. 再跑一遍巡检（现在的钟），报几种各几条；量每一步用了多久。结果写进 `--to/restore-check.json`（只有编号和数）。

退出码：0 都对上了；1 有不对的（见上）；2 参数不对（`--from` 不是备份的暂存、`--to` 不是空的、在仓库里、撤回清单不对），
什么都没动。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta, timezone
from pathlib import Path

from javmem.config import (
    ENV_ACCESS_FILE,
    ENV_LEXICON_FILE,
    ENV_OWNER_FILE,
    ENV_REDACTION_FILE,
    ENV_SOURCES_FILE,
    ConfigError,
    Library,
    PatrolLimits,
    Settings,
)
from javmem.intake import IntakeBusy, intake_lock
from javmem.ledger import LedgerError, connect_readonly, latest_version, open_ledger
from javmem.packet.render import TMP_DIR
from javmem.patrol import BACKUP_STATUS_FILE as STATUS_FILE
from javmem.pipeline.store import LOCK_FILE as STORE_LOCK_FILE
from javmem.pipeline.store import StoreBusy
from javmem.pipeline.write import LOCK_FILE as WRITE_LOCK_FILE
from javmem.pipeline.write import WriteBusy, WriteLock
from javmem.timeutil import iso, parse_aware

if os.name == "nt":
    import msvcrt
else:
    import fcntl

FORMAT = "javmem-backup/1"
STATUS_FORMAT = "javmem-backup-status/1"
REPORT_FORMAT = "javmem-restore-check/1"
MANIFEST = "javmem-backup.json"
PREVIOUS = ".javmem-backup.previous.json"  # 上一份清单：这一次一开头挪到这里，写完新的再删
MARKER = ".javmem-backup-staging"  # 这个目录是 javmem backup 的暂存：有它才敢删里面数据目录里已经没有的文件
STAGING_LOCK = ".javmem-backup.lock"  # 暂存目录自己的锁：同一时间只有一个备份在用它
REPORT = "restore-check.json"
DATA, CONFIG, EXPORTS = "data", "config", "exports"
SCRATCH = ".scratch"  # 账本不是最新版时，在这里另拷一份做导出，做完删掉
PART = ".part"  # 拷到一半的文件：先写它，写完再换上
SQLITE_MAGIC = b"SQLite format 3\x00"
SIDECARS = ("-wal", "-shm")  # SQLite 自己的两个辅助文件：在线备份拷出来的是一个完整的库，用不着
LOCK_NAMES = frozenset({".intake.lock", WRITE_LOCK_FILE, STORE_LOCK_FILE, "soft.lock", ".side.lock"})
ENV_TOKEN_LEDGER = "JAVMEM_TOKEN_LEDGER"
CONFIG_ENVS = (ENV_SOURCES_FILE, ENV_ACCESS_FILE, ENV_OWNER_FILE, ENV_LEXICON_FILE, ENV_REDACTION_FILE, ENV_TOKEN_LEDGER)
_SETTINGS_FIELD = {
    ENV_SOURCES_FILE: "sources_file",
    ENV_ACCESS_FILE: "access_file",
    ENV_OWNER_FILE: "owner_file",
    ENV_LEXICON_FILE: "lexicon_file",
    ENV_REDACTION_FILE: "redaction_file",
}
MERGES_SUFFIX = ".merges.jsonl"  # 别名表旁边的合并记录（javmem.zh.lexicon.merges_path）
DEFAULT_WAIT = 300.0
RETRY_SECONDS = 2.0
# 恢复时比巡检，这几种不比：disk_low 看的是恢复到的那块盘还剩多少，和备份那时比不了；checks_skipped（这一回没看的那几种，
# 巡检第 7 样）：备份拿着收件锁跑巡检，对账那几样一定是“没看”的（intake_busy，备份另外补上了），恢复出来的那一份照常看；
# endpoint_unofficial（巡检报模型、版本变了）：看的是跑巡检的那个进程的环境变量，不在数据目录里，恢复核对可能在另一个环境里跑
PATROL_NOT_COMPARED = frozenset({"disk_low", "checks_skipped", "endpoint_unofficial"})
EXIT_OK, EXIT_ATTENTION, EXIT_CONFIG, EXIT_BUSY, EXIT_RUNTIME = 0, 1, 2, 3, 5
_CHUNK = 1 << 20

Out = Callable[[str], None]


class BackupProblem(Exception):
    """备份、恢复做不下去了（读不了、写不进去）。消息里只有文件名和种类。"""


# ---------- 小工具 ----------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def _unlink(path: Path) -> None:
    """删一个文件，只读的也删（Windows 上只读文件删不掉，先改回可写）；不在就算了。"""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        os.unlink(path)
    except FileNotFoundError:
        pass


def _replace(part: Path, target: Path) -> None:
    if target.exists():
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)  # Windows 上换不掉只读的
    os.replace(part, target)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + PART)
    part.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    _replace(part, path)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_lock(name: str) -> bool:
    return name in LOCK_NAMES or (name.startswith(".graph-") and name.endswith(".lock"))


def _size_text(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _raise(error: OSError) -> None:
    raise error


def walk(root: Path, links: list[str] | None = None) -> tuple[list[str], list[str]]:
    """root 下面的目录和文件（相对路径，/ 隔开，排好）。.tmp 目录只记它自己，里面写到一半的不算。哪个目录列不出来就抛
    OSError（不能当成没了：暂存里那一份会被删掉）。链接、联接不跟进去也不算，记进 links（给了的话）：数据目录里有它们的，
    备份要报出来，不能悄悄漏了。"""
    dirs: list[str] = []
    files: list[str] = []
    for current, subdirs, names in os.walk(root, followlinks=False, onerror=_raise):
        here = Path(current)
        kept = []
        for name in subdirs:
            path = here / name
            if path.is_symlink() or path.is_junction():
                if links is not None:
                    links.append(_relative(root, path))
                continue
            kept.append(name)
            dirs.append(_relative(root, path))
        subdirs[:] = sorted(name for name in kept if name != TMP_DIR)
        for name in names:
            path = here / name
            if path.is_symlink():
                if links is not None:
                    links.append(_relative(root, path))
                continue
            if path.is_file():
                files.append(_relative(root, path))
    return sorted(dirs), sorted(files)


def _under(relative: str, folders: Sequence[str]) -> bool:
    return any(relative == folder or relative.startswith(folder + "/") for folder in folders)


def _sidecar_of(relative: str, sqlite_files: set[str]) -> bool:
    return any(relative.endswith(suffix) and relative[: -len(suffix)] in sqlite_files for suffix in (*SIDECARS, "-journal"))


@contextmanager
def store_lock(directory: Path) -> Iterator[None]:
    """L3 结果库的锁，和 ResultStore(exclusive=True) 拿的是同一把（同一个文件、同一个字节、同一种锁）：只拿锁，不打开库
    （打开会建库、升表，备份不改数据）。拿不到抛 StoreBusy。锁跟着进程走，进程死了系统会放开。"""
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / STORE_LOCK_FILE, "a+b")  # noqa: SIM115
    try:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise StoreBusy("这个结果库已经有一个抽取在跑") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


class StagingBusy(Exception):
    """这个暂存目录已经有一个备份在用（拷、算指纹、推异地）。"""


FAR_BYTE = 1 << 30  # 暂存目录的锁锁锁文件远处的这一个字节（SQLite 也这样锁）：这个系统（Windows）上的锁是硬的，锁住第 0 个字节，推异地的
# 程序（restic、打包、拷走）连读这个空文件都读不了，11-19 往 F 盘真备那一遍 restic 就是这样报错的


@contextmanager
def staging_lock(staging: Path) -> Iterator[None]:
    """暂存目录自己的锁：同一个暂存目录同一时间只有一个备份（拷、算指纹、推异地都拿着）。拿不到抛 StagingBusy。"""
    staging.mkdir(parents=True, exist_ok=True)
    handle = open(staging / STAGING_LOCK, "a+b")  # noqa: SIM115
    try:
        try:
            if os.name == "nt":
                handle.seek(FAR_BYTE)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise StagingBusy("这个暂存目录已经有一个备份在用") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(FAR_BYTE)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _waiting(enter: Callable[[ExitStack], None], wait: float, *, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> ExitStack:
    """enter 往 ExitStack 里一把一把拿锁，全拿到才回。拿不到的先把拿到的放了，每 RETRY_SECONDS 秒再试，过了 wait 秒还拿不到，
    抛最后那一次的“正在用”。回的 ExitStack 关掉就放锁。"""
    deadline = clock() + wait
    while True:
        stack = ExitStack()
        try:
            enter(stack)
            return stack
        except (IntakeBusy, WriteBusy, StoreBusy, StagingBusy):
            stack.close()
            left = deadline - clock()
            if left <= 0:
                raise
            sleep(min(RETRY_SECONDS, left))


def three_locks(data_dir: Path, wait: float, **kwargs: Callable) -> ExitStack:
    """收件锁、写账的锁、L3 结果库的锁都拿到才回（和撤回一样）。"""

    def enter(stack: ExitStack) -> None:
        stack.enter_context(intake_lock(data_dir))
        stack.enter_context(WriteLock(data_dir))
        stack.enter_context(store_lock(data_dir / "l3"))

    return _waiting(enter, wait, **kwargs)


def online_backup(source: Path, target: Path) -> None:
    """SQLite 的在线备份接口一步拷完（拷的时候拿着一个读事务，拷出来的是那一刻完整的样子，-wal 里的提交也在）。先拷到
    .part、收掉它旁边的 -wal、-shm，再换上。源库用普通连接打开，只读不写。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + PART)
    for path in (part, *(part.with_name(part.name + suffix) for suffix in SIDECARS)):
        _unlink(path)
    src = sqlite3.connect(source, timeout=60)
    try:
        dst = sqlite3.connect(part)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    settle(part)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target, onexc=_writable)  # 原来是目录、现在是库
    _replace(part, target)


def settle(path: Path) -> None:
    """一个没人在用的库旁边的 -wal、-shm（只读打开过会留下）：-wal 里有东西的先并回库里，再删掉。"""
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    for suffix in SIDECARS:
        _unlink(path.with_name(path.name + suffix))


def sqlite_details(path: Path) -> dict[str, object]:
    """完整性检查（integrity_check）、账本版本（user_version）、每张表几行（全文索引自己那张不数，它底下的几张照数）。
    immutable 打开：没人写它，旁边也不留 -wal、-shm。坏得打不开、数不了的照实写。"""
    out: dict[str, object] = {"integrity": None, "user_version": None, "tables": {}}
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        try:
            rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
            out["integrity"] = "ok" if rows == ["ok"] else "；".join(rows[:5])
            out["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
                    " AND coalesce(sql, '') NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name"
                )
            ]
            out["tables"] = {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in names}
        except sqlite3.DatabaseError as error:
            out["integrity"] = f"读不了（{type(error).__name__}）"
    finally:
        conn.close()
    return out


def _copy_plain(source: Path, target: Path) -> tuple[bool, str]:
    """照内容比：暂存里那一份和源文件的 sha256 一样就不拷（归档只增不改，平时只拷新的），不一样（新的、改了的、暂存里
    那一份在盘上坏了的）就拷：先写 .part 再换上，修改时间照原来的（权限不带过来：暂存里的都是可写的，删、换都方便；只读
    记进清单，恢复时设回去）。回（拷了没有，暂存里那一份的 sha256）：拷的时候源文件又变了的（日志正被追加），清单记的是
    暂存里实际的那一份。"""
    info = source.stat()
    digest = sha256_of(source)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target, onexc=_writable)  # 原来是目录、现在是文件
    try:
        same = target.stat().st_size == info.st_size and sha256_of(target) == digest
    except FileNotFoundError:
        same = False
    if same:
        if target.stat().st_mtime_ns != info.st_mtime_ns:
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
        return False, digest
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + PART)
    _unlink(part)
    shutil.copyfile(source, part)
    staged = sha256_of(part)
    os.utime(part, ns=(info.st_atime_ns, info.st_mtime_ns))
    _replace(part, target)
    return True, staged


def _writable(function: Callable, name: str, _: object) -> None:
    """rmtree 碰到只读的（Windows 上删不掉）：改回可写再删。"""
    os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
    function(name)


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _repo_check(path: Path, what: str) -> None:
    from javmem.scoring.run import inside_repo

    if inside_repo(path):
        raise ConfigError(f"{what}在仓库里：备份里有原文，要放仓库外")


def _empty_or_ours(path: Path, what: str, data_dir: Path) -> None:
    """暂存目录要是新的、空的，或者这个数据目录的 javmem backup 用过的（MARKER 里记着是哪个数据目录）：里面数据目录里没了的
    文件会删掉，别的目录、另一个数据目录的暂存不能这样删。暂存自己、它底下的 data、config、exports 是链接、联接的也不行：
    删的时候会删到链接指着的那个目录里去。"""
    if path.is_symlink() or path.is_junction():
        raise ConfigError(f"{what}是链接、联接")
    if not path.exists():
        return
    if not path.is_dir():
        raise ConfigError(f"{what}不是目录")
    for name in (DATA, CONFIG, EXPORTS, SCRATCH):
        if (path / name).is_symlink() or (path / name).is_junction():
            raise ConfigError(f"{what}底下的 {name} 是链接、联接：删的时候会删到别处去，不用")
    if not any(path.iterdir()):
        return
    marker = path / MARKER
    if not marker.is_file():
        raise ConfigError(f"{what}不是空的，也不是 javmem backup 用过的暂存（没有 {MARKER}）：里面的东西不动")
    try:
        owner = json.loads(marker.read_text(encoding="utf-8")).get("data_dir")
    except (OSError, ValueError, AttributeError):
        owner = None
    if owner is None or Path(str(owner)).resolve() != data_dir:
        raise ConfigError(f"{what}是另一个数据目录的暂存（{MARKER} 里记着）：一个数据目录用一个暂存目录")


# ---------- javmem backup ----------


@dataclass
class BackupReport:
    staging: str
    ok: bool = False
    manifest_id: str | None = None
    sqlite: dict[str, int] = field(default_factory=lambda: {"count": 0, "bytes": 0})
    files: dict[str, int] = field(
        default_factory=lambda: {"plain": 0, "copied": 0, "archive_copied": 0, "removed": 0, "bytes": 0}
    )
    ledgers: dict[str, int | None] = field(default_factory=dict)
    configs: int = 0
    withdrawals: dict[str, dict[str, int]] = field(default_factory=dict)
    patrol_items: int | None = None
    seconds: dict[str, float] = field(default_factory=dict)
    pushed: bool | None = None
    error: str | None = None  # 没成功的种类：busy、integrity、config、export、push_failed、error
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _config_files(settings: Settings, data_dir: Path) -> tuple[dict[str, list[Path]], dict[str, str], list[str]]:
    """配置文件：数据目录外的（环境变量名 → 要拷的文件，别名表连同旁边的合并记录），数据目录里的（环境变量名 → 相对数据
    目录的路径：跟着数据目录备，恢复时指到恢复出来的那一份）。设了却找不到的另回一句。"""
    found: dict[str, list[Path]] = {}
    inside: dict[str, str] = {}
    problems: list[str] = []
    for name in CONFIG_ENVS:
        if name in _SETTINGS_FIELD:
            value = getattr(settings, _SETTINGS_FIELD[name])
        else:
            text = os.environ.get(name, "").strip()
            value = Path(text).resolve() if text else None
        if value is None:
            continue
        path = Path(value).resolve()
        if _inside(path, data_dir):
            inside[name] = _relative(data_dir, path)
            continue
        if not path.is_file():
            if name != ENV_TOKEN_LEDGER:  # token 账第一次调用模型才建，还没有不算找不到
                problems.append(f"配置文件找不到：{name}")
            continue
        paths = [path]
        if name == ENV_LEXICON_FILE:
            merges = path.with_name(path.stem + MERGES_SUFFIX)
            if merges.is_file():
                paths.append(merges)
        found[name] = paths
    return found, inside, problems


def _patrol_record(settings: Settings) -> dict[str, object]:
    """照这一刻的样子跑一遍巡检，记下来给恢复时比。调用方拿着收件锁：run_patrol 自己拿不到，对账那几项会跳过，这里补上。"""
    from javmem.patrol import _settled_items, run_patrol
    from javmem.sources import load_registry

    at = settings.now()
    clocked = replace(settings, clock=lambda: at)
    items = run_patrol(clocked)
    try:
        registry = load_registry(clocked.sources_file)
    except ConfigError:
        registry = None
    items.extend(_settled_items(clocked, registry))
    offset = settings.timezone.utcoffset(None)
    return {
        "at": iso(at),
        "utc_offset_seconds": int(offset.total_seconds()) if offset is not None else None,
        "stale_hours": settings.stale_hours,
        "limits": asdict(settings.patrol),
        "items": [item.to_dict() for item in items],
    }


def _user_version(path: Path) -> int | None:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()


def _exports(settings: Settings, staging: Path, report: BackupReport) -> dict[str, dict[str, str]]:
    """从暂存里拷出来的账本导出撤回清单、冻结和主人的决定。账本是最新版的直接读（javmem 打开它不写）；不是的（代码刚升级、
    还没跑过别的命令）另拷一份做，暂存里的那份一个字不动。"""
    from javmem.decisions import export_decisions
    from javmem.withdraw import export_records

    written: dict[str, dict[str, str]] = {"withdrawals": {}, "decisions": {}}
    staged = replace(settings, data_dir=staging / DATA)
    for library in staged.libraries():
        if not library.db_path.is_file():
            continue
        source = staged
        scratch = staging / SCRATCH
        if _user_version(library.db_path) != latest_version():
            shutil.rmtree(scratch, ignore_errors=True)
            (scratch / "ledger").mkdir(parents=True)
            shutil.copyfile(library.db_path, scratch / "ledger" / library.db_path.name)
            source = replace(settings, data_dir=scratch)
        try:
            records = export_records(source, library.name)
            decisions = export_decisions(source, library.name)
        except (ConfigError, LedgerError, sqlite3.Error) as error:
            report.problems.append(f"{library.name} 库的撤回清单、主人的决定导不出来（{type(error).__name__}）")
            report.error = report.error or "export"
            continue
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            settle(library.db_path)
        for kind, value in (("withdrawals", records), ("decisions", decisions)):
            relative = f"{EXPORTS}/{kind}-{library.name}.json"
            _write_json(staging / relative, value)
            written[kind][library.name] = relative
        report.withdrawals[library.name] = {"packets": len(records["packets"]), "sentences": len(records["sentences"])}
    return written


def _manifest_entry(path: Path, *, source_mode: int | None = None, digest: str | None = None) -> dict[str, object]:
    info = path.stat()
    entry: dict[str, object] = {"size": info.st_size, "sha256": digest or sha256_of(path), "mtime_ns": info.st_mtime_ns}
    if source_mode is not None and not source_mode & stat.S_IWRITE:
        entry["readonly"] = True
    return entry


def backup(settings: Settings, staging: Path, *, wait: float = DEFAULT_WAIT, push: str | None = None,
           json_out: bool = False) -> BackupReport:
    """见模块开头。参数、设置不对抛 ConfigError（什么都没动、不记）；别的情况都回 BackupReport（ok、error、problems）。"""
    started = time.monotonic()
    data_dir = settings.require_data_dir()
    if not data_dir.is_dir():
        raise ConfigError("数据目录不在（JAVMEM_DATA_DIR）")
    data_dir = data_dir.resolve()
    staging = Path(staging).absolute()
    _empty_or_ours(staging, "暂存目录", data_dir)  # 链接、联接先看没 resolve 的那一层
    staging = staging.resolve()
    _repo_check(staging, "暂存目录")
    if _inside(staging, data_dir):
        raise ConfigError("暂存目录在数据目录里")
    if _inside(data_dir, staging):
        raise ConfigError("数据目录在暂存目录里")
    if push is not None and not Path(push).is_file():
        raise ConfigError("--push 给的程序找不到")
    if not (wait >= 0 and wait < float("inf")):
        raise ConfigError("--wait 要写成 0 或更大的秒数")

    attempt_at = iso(settings.now())
    report = BackupReport(str(staging))
    try:
        _run_backup(settings, data_dir, staging, report, attempt_at, wait=wait, push=push, json_out=json_out)
    except (IntakeBusy, WriteBusy, StoreBusy, StagingBusy) as busy:
        report.error = "busy"
        report.problems.append(f"拿不到锁：{busy}（等了 {wait:g} 秒）")
    except (OSError, sqlite3.Error, BackupProblem) as error:
        report.error = "error"
        report.problems.append(f"跑到一半出错（{type(error).__name__}）：{error}")
    report.ok = report.error is None and not report.problems
    report.seconds["total"] = round(time.monotonic() - started, 2)
    _record_status(settings, data_dir, attempt_at, report)
    report.ok = report.error is None and not report.problems  # 那一笔记不上（巡检看不到这一次）也不算好了
    return report


def _run_backup(settings: Settings, data_dir: Path, staging: Path, report: BackupReport, attempt_at: str, *,
                wait: float, push: str | None, json_out: bool) -> None:
    deadline = time.monotonic() + wait  # --wait 是一共等多久：暂存目录的锁等了一阵，三把锁只等剩下的
    with _waiting(lambda stack: stack.enter_context(staging_lock(staging)), wait):  # 拷、算指纹、推异地都拿着
        _check_owner(staging, data_dir)  # 拿到锁以后再核一遍：开头核的时候是空的，等锁的这一阵可能被另一个数据目录用上了
        if not (staging / MARKER).is_file():
            _write_json(staging / MARKER, {"format": FORMAT, "data_dir": str(data_dir)})
        _run_locked(settings, data_dir, staging, report, attempt_at, wait=max(0.0, deadline - time.monotonic()),
                    push=push, json_out=json_out)


def _check_owner(staging: Path, data_dir: Path) -> None:
    """拿着暂存目录的锁核：有标记的要是这个数据目录的；没有标记的，除了锁文件要是空的。不对抛 ConfigError，什么都不动。"""
    marker = staging / MARKER
    if marker.is_file():
        try:
            owner = json.loads(marker.read_text(encoding="utf-8")).get("data_dir")
        except (OSError, ValueError, AttributeError):
            owner = None
        if owner is None or Path(str(owner)).resolve() != data_dir:
            raise ConfigError("暂存目录是另一个数据目录的暂存（.javmem-backup-staging 里记着）：一个数据目录用一个暂存目录")
    elif any(entry.name != STAGING_LOCK for entry in staging.iterdir()):
        raise ConfigError("暂存目录不是空的，也不是 javmem backup 用过的暂存：里面的东西不动")


def _run_locked(settings: Settings, data_dir: Path, staging: Path, report: BackupReport, attempt_at: str, *,
                wait: float, push: str | None, json_out: bool) -> None:
    target_root = staging / DATA
    waited = time.monotonic()
    with three_locks(data_dir, wait):
        locked = time.monotonic()
        report.seconds["waited"] = round(locked - waited, 2)
        links: list[str] = []
        dirs, files = walk(data_dir, links)  # 先只读：哪个目录列不出来就停在这里，暂存一个字没动、上一份清单照旧能用
        sqlite_files = {relative for relative in files if is_sqlite(data_dir / relative)}
        plain = [
            relative for relative in files
            if relative not in sqlite_files and not _is_lock(relative.rsplit("/", 1)[-1])
            and not _sidecar_of(relative, sqlite_files)
        ]
        # 从这里起改暂存：上一份清单先挪开，写到一半断了的暂存没有清单
        if (staging / MANIFEST).exists():
            _replace(staging / MANIFEST, staging / PREVIOUS)
        target_root.mkdir(exist_ok=True)
        if links:
            shown = "、".join(links[:5]) + (f" 等 {len(links)} 个" if len(links) > 5 else "")
            report.problems.append(f"数据目录里有链接、联接，没跟进去、没备：{shown}（暂存里原来拷好的留着）")
            report.error = report.error or "link"
        for relative in dirs:  # 原来是文件、现在是目录的
            folder = target_root / relative
            if folder.is_symlink() or folder.is_file():
                _unlink(folder)
        for relative in sorted(sqlite_files):
            try:
                online_backup(data_dir / relative, target_root / relative)
            except sqlite3.OperationalError:
                raise  # 盘满了、读写出错、锁住了：不是库坏了，这一次照出错办
            except sqlite3.DatabaseError as error:  # 坏得拷不出来：多半是数据目录里那个就坏了
                report.problems.append(f"{relative} 拷不出来（{type(error).__name__}）")
                report.error = "integrity"
                _unlink(target_root / relative)
        sqlite_files = {relative for relative in sqlite_files if (target_root / relative).is_file()}
        kept_plain: list[str] = []
        modes: dict[str, int] = {}
        digests: dict[str, str] = {}
        for relative in plain:
            source = data_dir / relative
            try:
                mode = source.stat().st_mode
                copied, digest = _copy_plain(source, target_root / relative)
            except FileNotFoundError:
                if source.exists():
                    raise  # 源文件还在：是拷不过去（比如暂存那边的路径太长），不是打包方拿走了
                continue  # 收件目录里刚被打包方拿走、换掉的
            kept_plain.append(relative)
            modes[relative] = mode
            digests[relative] = digest
            if copied:
                report.files["copied"] += 1
                if relative.startswith(("archive/", "archive-test/")):
                    report.files["archive_copied"] += 1
        # 暂存里有、数据目录里没了的删掉（彻底删除了的归档、收走了的收件）；链接底下的不删：数据目录里那一份还在，只是这一次没备
        wanted = set(kept_plain) | sqlite_files
        staged_dirs, staged_files = walk(target_root)
        for relative in staged_files:
            if relative not in wanted and not _under(relative, links):
                _unlink(target_root / relative)
                if not relative.endswith(PART):
                    report.files["removed"] += 1
        for relative in sorted(set(staged_dirs) - set(dirs), reverse=True):
            if not _under(relative, links):
                shutil.rmtree(target_root / relative, onexc=_writable)
        for relative in dirs:
            (target_root / relative).mkdir(parents=True, exist_ok=True)
        configs, inside, missing = _config_files(settings, data_dir)
        report.problems.extend(missing)
        if missing:
            report.error = report.error or "config"
        if (staging / CONFIG).exists():
            shutil.rmtree(staging / CONFIG, onexc=_writable)
        config_names: dict[str, dict[str, object]] = {}
        for name, paths in configs.items():
            for path in paths:
                target = staging / CONFIG / name / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                os.utime(target, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
            config_names[name] = {"file": paths[0].name, "also": [path.name for path in paths[1:]], "path": str(paths[0])}
            report.configs += len(paths)
        for name, relative in inside.items():  # 放在数据目录里的：跟着数据目录备，恢复时指到恢复出来的那一份
            config_names[name] = {"data": relative}
        versions = {name: _user_version(target_root / "ledger" / f"{name}.db") for name in ("prod", "test")
                    if (target_root / "ledger" / f"{name}.db").is_file()}
        if all(version == latest_version() for version in versions.values()):
            patrol = _patrol_record(settings)
        else:  # 打开旧账本会把它升到最新：备份不改数据，这一次不记巡检
            patrol = {"skipped": "账本不是最新版：恢复时不比巡检"}
        report.seconds["locked"] = round(time.monotonic() - locked, 2)

    report.ledgers = dict(versions)
    if (staging / EXPORTS).exists():
        shutil.rmtree(staging / EXPORTS, onexc=_writable)
    exports = _exports(settings, staging, report)
    entries: dict[str, dict[str, object]] = {}
    for relative in sorted(sqlite_files):
        path = target_root / relative
        settle(path)
        entry = _manifest_entry(path)
        details = sqlite_details(path)
        entry["sqlite"] = details
        if details["integrity"] != "ok":
            report.problems.append(f"{relative} 完整性检查没过：{details['integrity']}")
            report.error = report.error or "integrity"
        entries[f"{DATA}/{relative}"] = entry
        report.sqlite["count"] += 1
        report.sqlite["bytes"] += int(entry["size"])
    for relative in kept_plain:
        entry = _manifest_entry(target_root / relative, source_mode=modes.get(relative), digest=digests.get(relative))
        entries[f"{DATA}/{relative}"] = entry
        report.files["plain"] += 1
        report.files["bytes"] += int(entry["size"])
    for folder in (CONFIG, EXPORTS):
        if (staging / folder).is_dir():
            _, extra = walk(staging / folder)
            for relative in extra:
                entries[f"{folder}/{relative}"] = _manifest_entry(staging / folder / relative)
    report.patrol_items = len(patrol["items"]) if "items" in patrol else None

    from javmem import __version__
    from javmem.versions import code_info

    manifest = {
        "format": FORMAT,
        "id": uuid.uuid4().hex,
        "started_at": attempt_at,
        "finished_at": iso(settings.now()),
        "data_dir": str(data_dir),
        "javmem": __version__,
        "code": code_info(),
        "ledger_code_version": latest_version(),
        "ledgers": versions,
        "dirs": [f"{DATA}/{relative}" for relative in dirs],
        "files": entries,
        "configs": config_names,
        "exports": exports,
        "patrol": patrol,
        "problems": list(report.problems),
    }
    _write_json(staging / MANIFEST, manifest)
    _unlink(staging / PREVIOUS)
    report.manifest_id = str(manifest["id"])

    if push is None:
        report.pushed = False
    elif report.error in ("integrity", "link"):
        # 拷出来的库坏了（多半是数据目录里那个就坏了）、有一块没备上：不推，免得新的一份把异地好的旧版本挤掉
        report.pushed = False
        report.problems.append("有库完整性检查没过，没推异地" if report.error == "integrity" else "有一块没备上，没推异地")
    else:  # 配置找不到、导不出来这类，数据本身是好的：照推，这一次照样算没成功
        code = _push(push, staging, json_out=json_out)
        report.pushed = code == 0
        if code != 0:
            report.error = "push_failed"
            report.problems.append(f"推异地没成功（退出码 {code}）")


def _push(program: str, staging: Path, *, json_out: bool) -> int:
    """跑推异地的程序：参数是暂存目录，在一个用完就删的临时目录里跑（它用相对路径写的临时文件不进仓库，也不进暂存：
    不然下一次跟着推出去），环境去掉所有 JAVMEM_*。--json 时它的输出转到标准错误，标准输出只留 JSON。"""
    import tempfile

    path = Path(program).resolve()
    argv = [sys.executable, str(path), str(staging)] if path.suffix.lower() == ".py" else [str(path), str(staging)]
    env = {name: value for name, value in os.environ.items() if not name.startswith("JAVMEM_")}
    workdir = tempfile.mkdtemp(prefix="javmem-push-")
    try:
        if not json_out:
            return subprocess.run(argv, env=env, cwd=workdir, check=False).returncode
        done = subprocess.run(argv, env=env, cwd=workdir, check=False, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        return -1
    finally:
        try:
            shutil.rmtree(workdir, onexc=_writable)
        except OSError:
            pass  # 删不掉的临时目录留着，不算推异地出错
    for text in (done.stdout, done.stderr):
        if text:
            print(text, end="" if text.endswith("\n") else "\n", file=sys.stderr, flush=True)
    return done.returncode


def _record_status(settings: Settings, data_dir: Path, attempt_at: str, report: BackupReport) -> None:
    """数据目录 logs/backup-status.json 记一笔（巡检读它）。记不上不算备份出错，只多一句。"""
    path = data_dir / "logs" / STATUS_FILE
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        old = old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        old = {}
    status = {
        "format": STATUS_FORMAT,
        "last_attempt_at": attempt_at,
        "last_ok_at": attempt_at if report.ok else old.get("last_ok_at"),
        "last_error": None if report.ok else report.error or "error",
        "last_pushed_at": attempt_at if report.pushed else old.get("last_pushed_at"),
        "staging": report.staging if report.ok else old.get("staging"),
        "manifest": report.manifest_id if report.ok else old.get("manifest"),
        "pushed": report.pushed if report.ok else old.get("pushed"),
    }
    try:
        _write_json(path, status)
    except OSError as error:
        report.problems.append(f"logs/{STATUS_FILE} 记不上（{type(error).__name__}）：巡检看不到这一次")


def describe_backup(report: BackupReport) -> list[str]:
    lines = [f"{'备份好了' if report.ok else '备份没成功'}：{report.staging}"]
    if report.manifest_id:
        ledgers = "、".join(f"{name} 第 {version} 版" for name, version in report.ledgers.items()) or "没有账本"
        lines.append(f"  SQLite {report.sqlite['count']} 个（{_size_text(report.sqlite['bytes'])}），在线备份拷的；账本 {ledgers}")
        files = report.files
        lines.append(
            f"  别的文件 {files['plain']} 个（{_size_text(files['bytes'])}；这次新拷 {files['copied']} 个，其中归档 "
            f"{files['archive_copied']} 个，删了 {files['removed']} 个）；配置 {report.configs} 个"
        )
        for name, counts in report.withdrawals.items():
            lines.append(f"  撤回清单：{name} 库包 {counts['packets']} 个、单条 {counts['sentences']} 句（只有编号和指纹）")
        if report.patrol_items is not None:
            lines.append(f"  巡检记下了 {report.patrol_items} 条，恢复时比")
        seconds = report.seconds
        lines.append(
            f"  等锁 {seconds.get('waited', 0):g} 秒，锁着 {seconds.get('locked', 0):g} 秒，一共 {seconds.get('total', 0):g} 秒；"
            f"清单 {MANIFEST}（编号 {report.manifest_id[:12]}）"
        )
        if report.pushed is False and report.error is None:
            lines.append("  没给 --push：只暂存了，没推到异地")
        elif report.pushed:
            lines.append("  推到异地了")
    for problem in report.problems:
        lines.append(f"  ！{problem}")
    return lines


# ---------- javmem restore-check ----------


@dataclass
class RestoreReport:
    source: str
    target: str
    ok: bool = False
    manifest_id: str | None = None
    backup_at: str | None = None
    files: dict[str, object] = field(
        default_factory=lambda: {"checked": 0, "bytes": 0, "mismatched": [], "missing": [], "extra": []}
    )
    sqlite: dict[str, object] = field(default_factory=lambda: {"checked": 0, "problems": []})
    ledgers: dict[str, dict[str, int | None]] = field(default_factory=dict)
    patrol: dict[str, object] = field(default_factory=dict)
    withdrawals: dict[str, dict[str, object]] = field(default_factory=dict)
    side: dict[str, dict[str, int]] = field(default_factory=dict)  # 支线：本机删了几组的原文（记成待清）、几条第二证人的待确认
    detached: list[str] = field(default_factory=list)  # 忘了原来那个图的：库名（主图），side:库名（支线的体检库）
    patrol_now: dict[str, int] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"format": REPORT_FORMAT, **asdict(self)}


def _patrol_key(item: Mapping[str, object]) -> str:
    return json.dumps({key: value for key, value in item.items() if key != "stats"}, ensure_ascii=False, sort_keys=True)


def _restored_settings(settings: Settings, target: Path, manifest: Mapping[str, object]) -> Settings:
    """恢复出来的那一份用的设置：数据目录是 --to/data，配置用暂存里拷的那几份（备份那时没配的就没有），时区、多久算太久、
    巡检的数照备份那时记的。"""
    values: dict[str, object] = {"data_dir": target / DATA, "clock": None}
    configs = manifest.get("configs") if isinstance(manifest.get("configs"), Mapping) else {}
    for name, attribute in _SETTINGS_FIELD.items():
        found = configs.get(name) if isinstance(configs, Mapping) else None
        if not isinstance(found, Mapping):
            values[attribute] = None
        elif "data" in found:  # 放在数据目录里的：用恢复出来的那一份
            values[attribute] = target / DATA / str(found["data"])
        else:
            values[attribute] = target / CONFIG / name / str(found["file"])
    patrol = manifest.get("patrol") if isinstance(manifest.get("patrol"), Mapping) else {}
    if isinstance(patrol, Mapping) and "items" in patrol:
        offset = patrol.get("utc_offset_seconds")
        if isinstance(offset, int):
            values["timezone"] = timezone(timedelta(seconds=offset))
        if isinstance(patrol.get("stale_hours"), (int, float)):
            values["stale_hours"] = float(patrol["stale_hours"])
        if isinstance(patrol.get("limits"), Mapping):
            try:
                values["patrol"] = PatrolLimits(**patrol["limits"])
            except TypeError:  # 别的版本的 javmem 记的，数的名字对不上：用现在的
                pass
    return replace(settings, **values)


def _verify(source: Path, manifest: Mapping[str, object], report: RestoreReport) -> None:
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise BackupProblem("清单里没有 files")
    listed: set[str] = set()
    for relative, entry in sorted(files.items()):
        parts = relative.split("/")
        if (parts[0] not in (DATA, CONFIG, EXPORTS) or len(parts) < 2 or ".." in parts or "" in parts
                or "\\" in relative or ":" in relative or not isinstance(entry, Mapping)):
            report.problems.append(f"清单里的文件名不对：{relative}")
            continue
        listed.add(relative)
        path = source / relative
        if not path.is_file():
            report.files["missing"].append(relative)
            continue
        size = path.stat().st_size
        if size != entry.get("size") or sha256_of(path) != entry.get("sha256"):
            report.files["mismatched"].append(relative)
            continue
        report.files["checked"] += 1
        report.files["bytes"] += size
        if isinstance(entry.get("sqlite"), Mapping):
            details = sqlite_details(path)
            report.sqlite["checked"] += 1
            if details["integrity"] != "ok":
                report.sqlite["problems"].append(f"{relative} 完整性检查没过：{details['integrity']}")
            elif details["tables"] != entry["sqlite"].get("tables"):
                before = entry["sqlite"].get("tables") or {}
                changed = sorted(set(before) ^ set(details["tables"]) | {
                    name for name in set(before) & set(details["tables"]) if before[name] != details["tables"][name]})
                report.sqlite["problems"].append(f"{relative} 行数和清单对不上：{'、'.join(changed[:10])}")
    for folder in (DATA, CONFIG, EXPORTS):
        if not (source / folder).is_dir():
            continue
        _, found = walk(source / folder)
        sqlite_files = {relative for relative in listed if relative.startswith(f"{folder}/")}
        for relative in found:
            name = f"{folder}/{relative}"
            if name not in listed and not _sidecar_of(name, sqlite_files):
                report.files["extra"].append(name)
    for name, items in (("mismatched", "sha256 对不上"), ("missing", "缺"), ("extra", "多出来的")):
        for relative in report.files[name]:
            report.problems.append(f"{relative}：{items}")
    report.problems.extend(report.sqlite["problems"])


def _restore_files(source: Path, target: Path, manifest: Mapping[str, object]) -> None:
    (target / DATA).mkdir(parents=True, exist_ok=True)
    for relative in manifest.get("dirs") or []:
        folder = (target / str(relative)).resolve()
        if not _inside(folder, (target / DATA).resolve()):
            raise BackupProblem(f"清单里的目录不在 {DATA}/ 底下：{relative}")
        folder.mkdir(parents=True, exist_ok=True)
    for relative, entry in sorted(manifest["files"].items()):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
        mtime = entry.get("mtime_ns")
        if isinstance(mtime, int):
            os.utime(destination, ns=(mtime, mtime))
        if entry.get("readonly"):
            os.chmod(destination, stat.S_IREAD)
    shutil.copyfile(source / MANIFEST, target / MANIFEST)


def _packet_header(path: Path) -> tuple[str, int] | None:
    """一个文件是哪个包、第几版：照收件用的那个包头解析器读（带 BOM 的、revision 后面带注释的都认得），packet_id 要是文本，
    revision 没写是第 1 版。读不出来、写得不对的回 None（不认就不动它）。"""
    from javmem.packet import Rejected
    from javmem.packet.header import decode, split_front_matter

    try:
        fields, _, _ = split_front_matter(decode(path.read_bytes()))
    except (OSError, Rejected, ValueError):
        return None
    packet_id, revision = fields.get("packet_id"), fields.get("revision", 1)
    if not isinstance(packet_id, str) or not packet_id or type(revision) is not int or revision < 1:
        return None
    return packet_id, revision


def _revision_state(db_path: Path, packet_id: str, revision: int) -> str | None:
    """恢复出来的账本里这一版：None 是没有这一版，purged 早就删了，current 是现行的（没撤回），other 是替换下来的或者撤回了的。"""
    conn = connect_readonly(db_path)
    try:
        row = conn.execute(
            "SELECT state, withdrawn_at, purged_at FROM packets WHERE packet_id = ? AND revision = ?", (packet_id, revision)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row["purged_at"] is not None:
        return "purged"
    return "current" if row["state"] == "current" and row["withdrawn_at"] is None else "other"


def _drop_from_inbox(library: Library, gone: Collection[tuple[str, int | None]]) -> int:
    """收件目录里还没收的包（备份那时投进来、还没收）：清单里彻底删除过的（整包，或者那一版）删掉。不然下一次收件又把它
    收进来（撤回过的包号放进跳过的副本，内容照样在盘上）。回删了几个。"""
    if not gone or not library.inbox.is_dir():
        return 0
    whole = {packet_id for packet_id, revision in gone if revision is None}
    removed = 0
    for path in sorted(library.inbox.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found = _packet_header(path)
        if found is not None and (found[0] in whole or found in gone):
            _unlink(path)
            removed += 1
    return removed


def _withdraw_again(settings: Settings, records_list: Sequence[Mapping[str, object]], report: RestoreReport) -> None:
    """照撤回清单把备份以后才撤回、彻底删除的再做一遍（库里早就是这样的不动）：撤回记录导进去、照它撤回；整包彻底删除过的
    再彻底删除；单删过的旧版本（revisions）再单删这一版（这一份里它还是现行的，删不了，报出来）；收件目录里还没收的那几个
    包也删掉。"""
    from javmem.withdraw import WithdrawError, purge, withdraw

    for records in records_list:
        name = str(records["library"])
        library = settings.library(name)
        if not library.db_path.is_file():
            report.withdrawals[name] = {"skipped": "恢复出来的没有这个库的账本"}
            continue
        result: dict[str, object] = {"withdrawn": [], "sentences": 0, "purged": [], "revisions_purged": [], "inbox_removed": 0}
        report.withdrawals[name] = result
        try:
            done = withdraw(settings, name, records=records)
            result["withdrawn"] = list(done.packets)
            result["sentences"] = len(done.sentences)
            whole = sorted({str(item["packet_id"]) for item in records.get("packets") or []
                            if isinstance(item, Mapping) and item.get("purged_at")})
            for packet_id in whole:
                if _needs_purge(library.db_path, packet_id):
                    outcome = purge(settings, name, packet_id)
                    result["purged"].append(packet_id)
                    if outcome.incomplete:
                        report.problems.append(f"{name} 库彻底删除 {packet_id} 以后整理没做完：{'、'.join(outcome.incomplete)}")
            gone: set[tuple[str, int | None]] = {(packet_id, None) for packet_id in whole}
            for item in records.get("revisions") or []:
                if not isinstance(item, Mapping) or not isinstance(item.get("revision"), int):
                    continue
                packet_id, revision = str(item.get("packet_id")), int(item["revision"])
                if packet_id in whole:
                    continue
                gone.add((packet_id, revision))
                state = _revision_state(library.db_path, packet_id, revision)
                if state == "current":
                    report.problems.append(
                        f"{name} 库 {packet_id} 第 {revision} 版：最新的清单里彻底删除了，恢复出来的这一份里它还是现行的"
                        "（备份以后才来了新版本），单删不了：先撤回这个包，或者等打包方重发新版本，再删这一版"
                    )
                elif state == "other":
                    outcome = purge(settings, name, packet_id, revision=revision)
                    result["revisions_purged"].append(f"{packet_id}#{revision}")
                    if outcome.incomplete:
                        report.problems.append(
                            f"{name} 库彻底删除 {packet_id} 第 {revision} 版以后整理没做完：{'、'.join(outcome.incomplete)}")
            result["inbox_removed"] = _drop_from_inbox(library, gone)
        except (WithdrawError, ConfigError, LedgerError, sqlite3.Error) as error:
            report.problems.append(f"{name} 库照撤回清单再删一遍没做成（{type(error).__name__}）：{error}")


def _needs_purge(db_path: Path, packet_id: str) -> bool:
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute("SELECT purged_at FROM packets WHERE packet_id = ?", (packet_id,)).fetchall()
        if rows:
            return any(row[0] is None for row in rows)
        record = conn.execute("SELECT purged_at FROM withdrawn_packets WHERE packet_id = ?", (packet_id,)).fetchone()
        return record is not None and record[0] is None
    finally:
        conn.close()


def _side_again(restored: Settings, report: RestoreReport) -> None:
    """支线（javmem.side）：照恢复出来、再删一遍以后的账本，第二证人的待确认按账本的现状删；支线的库在本机扫一遍（不连
    体检库：撤回了的、彻底删除了的、换了版本的那几组，原文删掉、记成待清），删了的不留在文件里（scrub）。"""
    from javmem.side.store import SideStore, SideStoreError, side_dir, side_lock
    from javmem.side.sweep import sweep
    from javmem.side.witness import clean, has_rows

    data_dir = restored.require_data_dir()
    for library in restored.libraries():
        if not library.db_path.is_file():
            continue
        dropped = clean(library.db_path, data_dir) if has_rows(library.db_path) else 0
        directory = side_dir(data_dir, library.name)
        swept = 0
        if (directory / "side.db").is_file():
            try:
                with side_lock(directory), SideStore.open(data_dir, library.name) as store:
                    result = asyncio.run(sweep(library.db_path, store, None))
                    swept = len(result.pending)
                    if swept:
                        store.scrub()
            except SideStoreError as error:
                report.problems.append(f"{library.name} 库的支线的库打不开（{error}）：没扫，备份以后删掉的原文可能还在里面")
        if dropped or swept:
            report.side[library.name] = {"groups_cleared": swept, "witness_dropped": dropped}


def _detach(restored: Settings, report: RestoreReport) -> None:
    """恢复出来的那一份忘了原来的图：账本忘了主图（graph init --reset 账本这一边），支线的库忘了体检库的标记。不然拿它们去
    同步、去扫，会动到正在用的图：标记对得上，就照这一份旧的写进去，还会把图里有、它这里没有的（备份以后才有的）当成
    没主的删掉。忘了以后再连，标记对不上，一个字不写。做过的不再做。"""
    from javmem.graph.sync import forget
    from javmem.side.store import SideStore, SideStoreError, side_dir

    data_dir = restored.require_data_dir()
    for library in restored.libraries():
        if library.db_path.is_file():
            conn = open_ledger(library)
            try:
                row = conn.execute("SELECT enabled, marker_id FROM graph_state WHERE id = 1").fetchone()
                if row is not None and (row[0] or row[1]):
                    forget(conn)
                    report.detached.append(library.name)
            except sqlite3.OperationalError:
                pass  # 旧账本没有 graph_state（还没升到第 26 版）：本来就没接过图
            finally:
                conn.close()
        if (side_dir(data_dir, library.name) / "side.db").is_file():
            try:
                with SideStore.open(data_dir, library.name) as store:
                    if store.meta("marker_id"):
                        store.conn.execute("DELETE FROM meta WHERE key = 'marker_id'")
                        report.detached.append(f"side:{library.name}")
            except SideStoreError as error:
                report.problems.append(f"{library.name} 库的支线的库打不开（{error}）：没让它忘了体检库，别拿它连体检库")


def _export_again(restored: Settings, target: Path) -> None:
    """--to/exports 照恢复出来、再删一遍以后的账本重导：从暂存拷来的那几份可能还带着备份以后才撤回、删掉的（冻结和主人的决定
    那一份有原文）。"""
    from javmem.decisions import export_decisions
    from javmem.withdraw import export_records

    folder = target / EXPORTS
    if folder.exists():
        shutil.rmtree(folder, onexc=_writable)
    for library in restored.libraries():
        if library.db_path.is_file():
            _write_json(folder / f"withdrawals-{library.name}.json", export_records(restored, library.name))
            _write_json(folder / f"decisions-{library.name}.json", export_decisions(restored, library.name))


def _records(value: object, where: str) -> Mapping[str, object]:
    """一份撤回清单（javmem withdraw --export 的格式）：要写明是哪个库的。"""
    from javmem.withdraw import RECORDS_SCHEMA

    if not isinstance(value, Mapping) or value.get("schema") != RECORDS_SCHEMA:
        raise ConfigError(f"撤回清单不是 {RECORDS_SCHEMA}：{where}")
    if value.get("library") not in ("prod", "test"):
        raise ConfigError(f"撤回清单没写是哪个库的（library 要是 prod 或 test）：{where}")
    return value


def restore_check(settings: Settings, source: Path, target: Path, *, withdrawals: Sequence[Path] = ()) -> RestoreReport:
    """见模块开头。参数不对抛 ConfigError（什么都没动）。"""
    started = time.monotonic()
    source = source.resolve()
    target = target.resolve()
    if not (source / MANIFEST).is_file():
        raise ConfigError(f"--from 里没有 {MANIFEST}：不是 javmem backup 的暂存，或者那一次备份没做完")
    _repo_check(target, "--to")
    if _inside(target, source) or _inside(source, target):
        raise ConfigError("--to 和 --from 不能套在一起")
    if settings.data_dir is not None and _inside(target, settings.data_dir.resolve()):
        raise ConfigError("--to 在现在用的数据目录（JAVMEM_DATA_DIR）里")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ConfigError("--to 要是一个新的（或者空的）目录")
    lists: list[Mapping[str, object]] = []
    for path in withdrawals:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigError(f"撤回清单读不了：{Path(path).name}（{type(error).__name__}）") from None
        lists.append(_records(value, Path(path).name))

    report = RestoreReport(str(source), str(target))
    target.mkdir(parents=True, exist_ok=True)
    try:
        manifest = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BackupProblem(f"清单不是 {FORMAT}")
    except (OSError, ValueError, BackupProblem) as error:
        report.problems.append(f"清单读不了（{type(error).__name__}）")
        return _finish(report, target, started)
    report.manifest_id = str(manifest.get("id"))
    report.backup_at = str(manifest.get("started_at"))

    try:
        moment = time.monotonic()
        _verify(source, manifest, report)
        report.seconds["verify"] = round(time.monotonic() - moment, 2)
        if report.problems:
            return _finish(report, target, started)  # 坏了的不往下恢复

        restored = _restored_settings(settings, target, manifest)  # 只是拼路径：先算出来，拷到一半出错也照样忘了图
        try:
            moment = time.monotonic()
            _restore_files(source, target, manifest)
            report.seconds["copy"] = round(time.monotonic() - moment, 2)
            moment = time.monotonic()
            for library in restored.libraries():
                if library.db_path.is_file():
                    before = _user_version(library.db_path)
                    open_ledger(library).close()
                    report.ledgers[library.name] = {"backup": before, "now": _user_version(library.db_path)}
            report.seconds["open"] = round(time.monotonic() - moment, 2)

            moment = time.monotonic()
            _compare_patrol(restored, manifest, report)
            report.seconds["patrol"] = round(time.monotonic() - moment, 2)

            # 再删一遍走的是库函数（不调图的钩子），支线扫的时候不给图：这一段连不到哪个图；忘了图放在 finally 里，删完再忘：
            # 先忘了，清向量缓存就照“没接图”清，关系拿来算向量的那一句也删了（J5.6 要留着）
            moment = time.monotonic()
            if not lists:
                exports = manifest.get("exports") if isinstance(manifest.get("exports"), Mapping) else {}
                exported = exports.get("withdrawals") if isinstance(exports.get("withdrawals"), Mapping) else {}
                for relative in exported.values():
                    value = json.loads((target / str(relative)).read_text(encoding="utf-8"))
                    lists.append(_records(value, str(relative)))
            _withdraw_again(restored, lists, report)
            _side_again(restored, report)
            report.seconds["withdraw"] = round(time.monotonic() - moment, 2)
            _export_again(restored, target)
        finally:
            try:
                _detach(restored, report)  # 中途出了错（拷到一半盘满了也算）也要忘了图：--to/data 里不能留一本还接着正在用的图的账本
            except (OSError, sqlite3.Error, LedgerError) as error:
                report.problems.append(f"恢复出来的没让它忘了原来的图（{type(error).__name__}）：别拿它连图")
        from javmem.patrol import run_patrol

        report.patrol_now = dict(sorted(Counter(item.kind for item in run_patrol(restored)).items()))
    except (OSError, ValueError, sqlite3.Error, ConfigError, LedgerError, BackupProblem) as error:
        report.problems.append(f"跑到一半出错（{type(error).__name__}）：{error}")
    return _finish(report, target, started)


def _compare_patrol(restored: Settings, manifest: Mapping[str, object], report: RestoreReport) -> None:
    from javmem.patrol import run_patrol

    recorded = manifest.get("patrol")
    if not isinstance(recorded, Mapping) or not isinstance(recorded.get("items"), list):
        report.patrol = {"same": None, "skipped": str((recorded or {}).get("skipped", "清单里没有巡检"))}
        return
    at = parse_aware(recorded.get("at"))
    if at is None:
        report.patrol = {"same": None, "skipped": "清单里巡检的时间读不了"}
        return
    items = run_patrol(replace(restored, clock=lambda: at))
    then = Counter(_patrol_key(item) for item in recorded["items"] if item.get("kind") not in PATROL_NOT_COMPARED)
    now = Counter(_patrol_key(item.to_dict()) for item in items if item.kind not in PATROL_NOT_COMPARED)
    added = sorted((now - then).elements())
    missing = sorted((then - now).elements())
    report.patrol = {"same": not added and not missing, "at": iso(at), "items": sum(now.values()),
                     "added": [json.loads(key) for key in added], "missing": [json.loads(key) for key in missing]}
    for key in added:
        report.problems.append(f"巡检比备份那时多了一条：{json.loads(key).get('kind')}（{json.loads(key).get('message')}）")
    for key in missing:
        report.problems.append(f"巡检比备份那时少了一条：{json.loads(key).get('kind')}（{json.loads(key).get('message')}）")


def _finish(report: RestoreReport, target: Path, started: float) -> RestoreReport:
    report.ok = not report.problems
    report.seconds["total"] = round(time.monotonic() - started, 2)
    try:
        _write_json(target / REPORT, report.to_dict())
    except OSError as error:
        report.problems.append(f"{REPORT} 写不进去（{type(error).__name__}）")
        report.ok = False
    return report


def describe_restore(report: RestoreReport) -> list[str]:
    lines = [f"{'恢复核对过了，都对上了' if report.ok else '恢复核对有不对的'}：{report.source} → {report.target}"]
    files = report.files
    lines.append(
        f"  清单（编号 {(report.manifest_id or '?')[:12]}，备份于 {report.backup_at}）：文件对上 {files['checked']} 个"
        f"（{_size_text(int(files['bytes']))}），sha256 对不上 {len(files['mismatched'])} 个、缺 {len(files['missing'])} 个、"
        f"多出来 {len(files['extra'])} 个；SQLite 核了 {report.sqlite['checked']} 个"
    )
    for name, versions in report.ledgers.items():
        same = "对得上" if versions["backup"] == versions["now"] else f"升到了第 {versions['now']} 版"
        lines.append(f"  {name} 库账本第 {versions['backup']} 版，{same}")
    if report.patrol:
        if report.patrol.get("same") is None:
            lines.append(f"  巡检没比：{report.patrol.get('skipped')}")
        else:
            lines.append(
                f"  巡检和备份那时（{report.patrol.get('at')}）比：{'一样' if report.patrol['same'] else '不一样'}，"
                f"{report.patrol.get('items')} 条"
            )
    for name, result in report.withdrawals.items():
        if "skipped" in result:
            lines.append(f"  撤回清单（{name} 库）没做：{result['skipped']}")
        else:
            lines.append(
                f"  照撤回清单再删一遍（{name} 库）：又撤回了 {len(result['withdrawn'])} 个包、{result['sentences']} 句，"
                f"彻底删除了 {len(result['purged'])} 个包、{len(result['revisions_purged'])} 个旧版本，"
                f"收件目录里删了 {result['inbox_removed']} 个还没收的"
            )
    for name, result in report.side.items():
        lines.append(f"  支线（{name} 库）：本机删了 {result['groups_cleared']} 组的原文（记成待清），"
                     f"第二证人的待确认删了 {result['witness_dropped']} 条")
    if report.detached:
        lines.append(f"  恢复出来的不接图（{'、'.join(report.detached)}）：要接图先换一个新图、graph init、整张同步")
    if report.patrol_now:
        lines.append("  现在的巡检：" + "、".join(f"{kind} {count} 条" for kind, count in report.patrol_now.items()))
    seconds = report.seconds
    if "withdraw" in seconds:
        ready = sum(seconds.get(step, 0) for step in ("verify", "copy", "open", "patrol", "withdraw"))
        lines.append(
            f"  用时：核 {seconds.get('verify', 0):g} 秒、拷 {seconds.get('copy', 0):g} 秒、打开账本 {seconds.get('open', 0):g} 秒、"
            f"比巡检 {seconds.get('patrol', 0):g} 秒、再删一遍 {seconds.get('withdraw', 0):g} 秒；从开始核到能查 {ready:.2f} 秒"
        )
    for problem in report.problems:
        lines.append(f"  ！{problem}")
    lines.append(f"  结果写在 {Path(report.target) / REPORT}")
    return lines


# ---------- 命令行 ----------


def add_backup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", required=True, metavar="暂存目录",
                        help="拷到这里（仓库外；新的，或者上一次备份用过的：只拷新的、删掉数据目录里没了的）")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT, metavar="秒",
                        help=f"收件、写账、抽取的锁拿不到最多等几秒，默认 {DEFAULT_WAIT:g}")
    parser.add_argument("--push", metavar="程序", help="暂存好了以后跑它推到异地，参数是暂存目录；是 .py 的，交给跑 javmem 的那个解释器")
    parser.add_argument("--json", action="store_true", help="输出 JSON（只有编号和数）")


def add_restore_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="source", required=True, metavar="快照目录", help="javmem backup 的暂存（取回来的那一份）")
    parser.add_argument("--to", required=True, metavar="新目录", help="恢复到这里（新的或者空的，仓库外）；数据目录是它底下的 data")
    parser.add_argument("--withdrawals", action="append", default=[], metavar="撤回清单",
                        help="最新的撤回清单（javmem withdraw --export 的，或者更新的那份备份 exports 里的），可以写几次；"
                        "不写用这份备份自己导出的")
    parser.add_argument("--json", action="store_true", help="输出 JSON（只有编号和数）")


def run_backup(args: argparse.Namespace, settings: Settings, out: Out = print) -> int:
    try:
        report = backup(settings, Path(args.to), wait=args.wait, push=args.push, json_out=args.json)
    except ConfigError as error:
        out(f"备份不了：{error}")
        return EXIT_CONFIG
    if args.json:
        out(json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
    else:
        for line in describe_backup(report):
            out(line)
    if report.ok:
        return EXIT_OK
    return {"busy": EXIT_BUSY, "error": EXIT_RUNTIME}.get(report.error or "", EXIT_ATTENTION)


def run_restore_check(args: argparse.Namespace, settings: Settings, out: Out = print) -> int:
    try:
        report = restore_check(settings, Path(args.source), Path(args.to), withdrawals=[Path(one) for one in args.withdrawals])
    except ConfigError as error:
        out(f"恢复核对做不了：{error}")
        return EXIT_CONFIG
    if args.json:
        out(json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
    else:
        for line in describe_restore(report):
            out(line)
    return EXIT_OK if report.ok else EXIT_ATTENTION
