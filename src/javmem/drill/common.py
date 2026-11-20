"""删除演练、投毒演练共用的（U2 乙）。

- **拷数据目录**（`copy_data_dir`）：像备份那样先拿原来那个数据目录的三把锁（收件、写账、结果库；拿不到等一会儿，还拿不到
  就不演），SQLite 文件照在线备份拷（只读打开原件；WAL 里没并进去的也带上），别的文件照原样拷（归档还是只读的）。锁文件、
  `-wal`、`-shm` 不拷。原件一个字不改：结果库的锁照 `ResultStore` 的办法单独拿 `l3.lock`，不打开 `l3.db`（打开会建表、升级）。
- **环境**（`drill_env`）：这个进程的环境变量去掉所有 `JAVMEM_*`，只接着给几样本机的配置（来源登记、别名表、主人的名字、
  时区、提示词和模型的名字、判断 C 和进图前检查的开关这类，见 `PASS_THROUGH`）；密钥、密码、外面的地址一样不给（向量服务
  只在地址是本机的时候给）。体检库、主图的设置一律不给：拷贝的账本、支线库记着原来那份的标记，连上了就删到原来那一份里去。
  要接图的由调用的地方另给（只给测试图）。
- **跑命令**（`run_javmem`）：在这个进程里跑 `javmem.cli.main`，环境照上面那一份换掉（`scoped_env`），输出整个写进日志，
  屏幕上不打（里面只有编号和数，也不打）。这样测试里能把哪一步换成故意不清的，看演练报不报。
- **按字搜**（`Needle`、`scan_file`、`scan_tables`）：原样的 UTF-8、JSON 的转义写法（`\\uXXXX`，大小写两种）、原文检索的统一写法，
  三种写法都搜；文件逐字节搜，SQLite 另外逐表、逐列搜（看得出在哪一张表）。只回数，不回字。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from javmem.intake import IntakeBusy, intake_lock
from javmem.pipeline.store import LOCK_FILE as STORE_LOCK_FILE
from javmem.pipeline.store import StoreBusy
from javmem.pipeline.write import WriteBusy, WriteLock

if os.name == "nt":
    import msvcrt
else:
    import fcntl

EXIT_PASS, EXIT_FAIL, EXIT_CANNOT = 0, 1, 2

# 拷数据目录时不拷的：锁文件（拷贝自己会建），SQLite 的 -wal、-shm、-journal（在线备份已经带上了里面提交了的）
LOCK_NAMES = frozenset({".intake.lock", ".write.lock", STORE_LOCK_FILE, ".side.lock", "soft.lock", ".graph-prod.lock",
                        ".graph-test.lock"})
SIDECARS = ("-wal", "-shm", "-journal")
SQLITE_HEADER = b"SQLite format 3\x00"

# 子命令接着给的本机配置（不是密钥、不是地址、不是密码）：拷贝照原来那一份的规矩读
PASS_THROUGH = (
    "JAVMEM_SOURCES_FILE", "JAVMEM_LEXICON_FILE", "JAVMEM_OWNER_FILE", "JAVMEM_REDACTION_FILE", "JAVMEM_TIMEZONE",
    "JAVMEM_STRICT_PARTITIONS", "JAVMEM_STALE_HOURS", "JAVMEM_L3_PROMPT", "JAVMEM_LLM_MODEL", "JAVMEM_LLM_REASONING_EFFORT",
    "JAVMEM_LLM_STRUCTURED_OUTPUT", "JAVMEM_RECHECK_MODEL", "JAVMEM_RECHECK_REASONING_EFFORT", "JAVMEM_SOFT_MODEL",
    "JAVMEM_SOFT_EFFORT", "JAVMEM_EMBEDDING_MODEL", "JAVMEM_JUDGE_MODE", "JAVMEM_JUDGE_MODEL", "JAVMEM_JUDGE_LOW",
    "JAVMEM_JUDGE_HIGH", "JAVMEM_PRECHECK", "JAVMEM_PRECHECK_LOW", "JAVMEM_PRECHECK_HIGH", "JAVMEM_PRE_B_MODEL",
)
# 向量服务：只在地址是本机的时候给（外面的要钱，真数据的字也就发出去了）
EMBEDDING_PASS = ("JAVMEM_EMBEDDING_BASE_URL", "JAVMEM_EMBEDDING_API_KEY", "JAVMEM_EMBEDDING_TIMEOUT_SECONDS")

Out = Callable[[str], None]


class CannotRun(Exception):
    """演练跑不了（退出码 2）。消息里只有原因和数，没有原文、名字。"""


def local_url(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url.strip()).hostname or "") in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


# ---------- 拷数据目录 ----------


@contextmanager
def _store_lock(directory: Path) -> Iterator[None]:
    """结果库的锁，和 ResultStore(exclusive=True) 拿的是同一把（l3.lock 的第一个字节），不打开 l3.db。"""
    with open(directory / STORE_LOCK_FILE, "a+b") as handle:
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


@contextmanager
def source_locks(data_dir: Path, *, wait: float = 60.0, sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    """拿原来那个数据目录的三把锁（像撤回、备份那样：收件、写账、结果库）。拿不到每 2 秒再试，过了 wait 秒还拿不到就不演。"""
    deadline = time.monotonic() + wait
    while True:
        stack = ExitStack()
        try:
            stack.enter_context(intake_lock(data_dir))
            stack.enter_context(WriteLock(data_dir))
            if (data_dir / "l3").is_dir():
                stack.enter_context(_store_lock(data_dir / "l3"))
        except (IntakeBusy, WriteBusy, StoreBusy) as busy:
            stack.close()
            if time.monotonic() >= deadline:
                raise CannotRun(f"原来那个数据目录正忙（{busy}），等了 {wait:.0f} 秒还拿不到锁：等它跑完再演") from None
            sleep(2.0)
            continue
        with stack:
            yield
        return


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def backup_sqlite(source: Path, target: Path) -> None:
    """照在线备份拷一个 SQLite 文件（-wal 里提交了、没并进去的也带上）。原件只读打开，一个字不改。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


@dataclass
class CopyStats:
    files: int = 0  # 照原样拷的文件
    sqlite: int = 0  # 照在线备份拷的 SQLite 文件
    bytes: int = 0
    skipped: int = 0  # 没拷的：锁文件、-wal、-shm、符号链接
    seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {"files": self.files, "sqlite": self.sqlite, "bytes": self.bytes, "skipped": self.skipped,
                "seconds": round(self.seconds, 1)}


def copy_data_dir(source: Path, target: Path, *, wait: float = 60.0) -> CopyStats:
    """把 source 整个拷到 target（target 要还不存在）。拿着 source 的三把锁拷，拷完放锁。"""
    started = time.monotonic()
    stats = CopyStats()
    target.mkdir(parents=True)
    with source_locks(source, wait=wait):
        for root, dirs, files in os.walk(source):
            here = Path(root)
            rel = here.relative_to(source)
            (target / rel).mkdir(parents=True, exist_ok=True)
            dirs[:] = sorted(name for name in dirs if not (here / name).is_symlink())
            for name in sorted(files):
                path = here / name
                if path.is_symlink() or name in LOCK_NAMES:
                    stats.skipped += 1
                    continue
                sidecar = next((suffix for suffix in SIDECARS if name.endswith(suffix)), None)
                if sidecar is not None and (here / name[: -len(sidecar)]).exists():
                    stats.skipped += 1
                    continue
                dest = target / rel / name
                if is_sqlite(path):
                    backup_sqlite(path, dest)
                    stats.sqlite += 1
                else:
                    shutil.copy2(path, dest)
                    stats.files += 1
                stats.bytes += dest.stat().st_size
    stats.seconds = time.monotonic() - started
    return stats


# ---------- 环境、跑命令 ----------


def drill_env(base: Mapping[str, str], *, data: Path, token_ledger: Path, extra: Mapping[str, str] = {}) -> dict[str, str]:  # noqa: B006
    """子命令的环境：base（这个进程的环境）去掉所有 JAVMEM_*，接着给 PASS_THROUGH 里有值的那几样、本机的向量服务，
    再加上 extra 和数据目录、token 账。"""
    env = {key: value for key, value in base.items() if not key.startswith("JAVMEM_")}
    for key in PASS_THROUGH:
        if base.get(key, "").strip():
            env[key] = base[key]
    if local_url(base.get("JAVMEM_EMBEDDING_BASE_URL", "")):
        for key in EMBEDDING_PASS:
            if base.get(key, "").strip():
                env[key] = base[key]
    env.update(extra)
    env.update(JAVMEM_DATA_DIR=str(data), JAVMEM_TOKEN_LEDGER=str(token_ledger))
    return env


@contextmanager
def scoped_env(env: Mapping[str, str]) -> Iterator[None]:
    """这一段里这个进程的环境变量换成 env，出来照原样换回去（出了错也换回去）。"""
    saved = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextmanager
def quiet(log: Path) -> Iterator[None]:
    """这一段打到屏幕上的（查询时主图检索的提示这类）收进日志，屏幕上不打。"""
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            yield
    finally:
        if buffer.getvalue():
            with log.open("a", encoding="utf-8") as handle:
                handle.write(buffer.getvalue())


def run_javmem(log: Path, *args: str) -> tuple[int, str]:
    """在这个进程里跑一条 javmem 命令（照现在的环境），输出整个写进日志，返回 (退出码, 输出)。"""
    from javmem.cli import main

    buffer = io.StringIO()
    started = time.monotonic()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            code = main(list(args))
        except SystemExit as stop:  # argparse 认不得参数
            code = stop.code if isinstance(stop.code, int) else 2
    text = buffer.getvalue()
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ javmem {' '.join(args)} -> {code}（{time.monotonic() - started:.1f} 秒）\n{text}")
    return code, text


def token_rows(path: Path) -> tuple[int, int]:
    """(付钱的行, 本机和假的行)。模型名是 qwen、fake 开头的算本机、假的（向量服务的名字是 Qwen/…）。"""
    if not path.exists():
        return 0, 0
    from javmem.usage import TokenLedger

    paid = local = 0
    for row in TokenLedger(path).rows():
        if (row.get("model") or "").lower().startswith(("qwen", "fake")):
            local += 1
        else:
            paid += 1
    return paid, local


# ---------- 按字搜 ----------


@dataclass(frozen=True)
class Needle:
    """要搜的一段字：kind 是哪一种（样本句、名字），index 是第几个，form 是哪种写法。只在内存里，不写进文件。"""

    kind: str
    index: int
    form: str
    text: str

    @property
    def data(self) -> bytes:
        return self.text.encode("utf-8")


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-f]{4})")


def _escaped(text: str) -> list[str]:
    """JSON 的转义写法（ensure_ascii）：Python 写的是小写的 \\uXXXX，别的程序可能写大写，两种都要。"""
    lower = json.dumps(text)[1:-1]
    if lower == text:
        return []
    upper = _UNICODE_ESCAPE.sub(lambda found: "\\u" + found.group(1).upper(), lower)
    return list(dict.fromkeys((lower, upper)))


def needles_of(kind: str, texts: Sequence[str], *, norm: bool = True) -> list[Needle]:
    """每一段字的三种写法（重复的只留一个）。原文检索的统一写法有两份时（隔着 LANE），两份各搜。"""
    from javmem.zh.search import LANE, searchable

    out: list[Needle] = []
    for index, text in enumerate(texts):
        forms: list[tuple[str, str]] = [("utf8", text)]
        forms += [("json", form) for form in _escaped(text)]
        if norm:
            forms += [("norm", part) for part in searchable(text).split(LANE) if part]
        seen: set[str] = set()
        for form, value in forms:
            if value and value not in seen:
                seen.add(value)
                out.append(Needle(kind, index, form, value))
    return out


def _count_in(chunk: bytes | str, needle: bytes | str, limit: int) -> int:
    """chunk 里从头起到 limit（不含）为止开头的 needle 有几处（下一块会重叠一截，开头在重叠里的留给下一块数）。"""
    found = 0
    at = chunk.find(needle)  # type: ignore[arg-type]
    while at != -1 and at < limit:
        found += 1
        at = chunk.find(needle, at + 1)  # type: ignore[arg-type]
    return found


CHUNK = 8 * 1024 * 1024


def scan_file(path: Path, needles: Sequence[Needle]) -> Counter[Needle]:
    """一个文件逐字节搜：每一段字有几处。按块读，块和块重叠最长那一段字的长度，跨块的也数得到。"""
    found: Counter[Needle] = Counter()
    if not needles:
        return found
    datas = [(needle, needle.data) for needle in needles]
    overlap = max(len(data) for _, data in datas) - 1
    try:
        handle = path.open("rb")
    except OSError:
        return found
    with handle:
        tail = b""
        while True:
            block = handle.read(CHUNK)
            chunk = tail + block
            if not chunk:
                break
            last = not block or len(block) < CHUNK
            limit = len(chunk) if last else max(len(chunk) - overlap, 0)
            for needle, data in datas:
                hits = _count_in(chunk, data, limit)
                if hits:
                    found[needle] += hits
            if last:
                break
            tail = chunk[limit:]
    return found


def scan_tree(root: Path, needles: Sequence[Needle], *, skip: Callable[[Path], bool] = lambda _: False) -> dict[str, Counter[Needle]]:
    """root 底下每个文件逐字节搜，回 {相对路径: 每一段字几处}（一处都没有的不回）。"""
    out: dict[str, Counter[Needle]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or skip(path):
            continue
        found = scan_file(path, needles)
        if found:
            out[path.relative_to(root).as_posix()] = found
    return out


TABLE_CHUNK = 4 * 1024 * 1024


def _tables(conn: sqlite3.Connection) -> list[str]:
    """普通的表（虚表本身不搜：外部内容的全文索引读出来就是原文表；它的影子表照普通的表搜）。"""
    return [str(row[0]) for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name")
            if not str(row[1] or "").upper().startswith("CREATE VIRTUAL")]


def scan_tables(db: Path, needles: Sequence[Needle]) -> dict[str, Counter[Needle]]:
    """一个 SQLite 文件逐表、逐列搜（文字照原样，二进制照 UTF-8 解，解不了的字丢掉）：回 {表: 每一段字几处}。
    只读打开；打不开的回空。"""
    out: dict[str, Counter[Needle]] = {}
    if not needles:
        return out
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        for table in _tables(conn):
            columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
            found: Counter[Needle] = Counter()
            for column in columns:
                for chunk in _column_chunks(conn, table, column):
                    for needle in needles:
                        hits = chunk.count(needle.text)
                        if hits:
                            found[needle] += hits
            if found:
                out[table] = found
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def _column_chunks(conn: sqlite3.Connection, table: str, column: str) -> Iterator[str]:
    """一列的值拼成一块一块（中间隔一个 \\x00，一个值不跨块），每块 TABLE_CHUNK 上下。"""
    parts: list[str] = []
    size = 0
    for (value,) in conn.execute(f'SELECT "{column}" FROM "{table}"'):
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="ignore")
        elif isinstance(value, str):
            text = value
        else:
            continue
        if not text:
            continue
        parts.append(text)
        size += len(text) + 1
        if size >= TABLE_CHUNK:
            yield "\x00".join(parts)
            parts, size = [], 0
    if parts:
        yield "\x00".join(parts)


def contains_any(chunks: Sequence[str], text: str) -> bool:
    return any(text in chunk for chunk in chunks)


def chunked(texts: Iterable[str], size: int = TABLE_CHUNK) -> list[str]:
    """一堆字拼成几大块（中间隔着 \\n\\x00\\n，一段不跨块），拿来查“别处有没有这一句”。"""
    out: list[str] = []
    parts: list[str] = []
    total = 0
    for text in texts:
        if not text:
            continue
        parts.append(text)
        total += len(text) + 3
        if total >= size:
            out.append("\n\x00\n".join(parts))
            parts, total = [], 0
    if parts:
        out.append("\n\x00\n".join(parts))
    return out


def tally(found: Mapping[str, Counter[Needle]]) -> dict[str, dict[str, int]]:
    """{地方: {种类: 几处}}：拿去写结果文件的（只有数）。"""
    out: dict[str, dict[str, int]] = {}
    for where, counts in found.items():
        kinds: Counter[str] = Counter()
        for needle, hits in counts.items():
            kinds[needle.kind] += hits
        out[where] = dict(sorted(kinds.items()))
    return out
