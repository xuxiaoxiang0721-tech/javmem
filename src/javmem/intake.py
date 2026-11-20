"""收件（packet-spec 第 8 到 10 节）：校验、去重、登记、归档、建原文索引。

顺序不能变：写包登记 → 归档 → 收尾。每一步都能重做，中断了不丢包：
启动时先看包登记里停在 registered 的，接着做完（对账），再扫收件目录。

收尾是一个事务：换版本、换原文索引、记警告、排队抽取。替换时旧版本一直是现行的，
直到这个事务才换成新版本，所以中途断了也查不到一半的东西。

这里不调模型。排队抽取就是把包登记的 extraction 写成 queued，原文索引不等它。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from javmem.config import Library, Settings
from javmem.ledger import open_ledger, transaction
from javmem.packet import Packet, Rejected, content_hash, parse_packet, validate_packet
from javmem.packet.errors import BAD_HEADER, UNKNOWN_SOURCE
from javmem.packet.render import TMP_DIR
from javmem.raw import replace_packet_sentences
from javmem.redaction import RedactionPattern, load_patterns, scan_types
from javmem.sources import Registry, load_registry
from javmem.timeutil import epoch, iso

if os.name == "nt":
    import msvcrt
else:
    import fcntl

UNKNOWN_DIR = "_unknown"
SKIPPED_DIR = "skipped"
TASK_INTAKE = "intake"

# 进巡检的警告（解析时的几种在 packet.model）
REDACTION_HIT = "redaction_hit"
SAME_REVISION_DIFFERENT = "same_revision_different"
WITHDRAWN_RESENT = "withdrawn_resent"
REGISTRATION_LOST = "registration_lost"
ARCHIVE_CONFLICT = "archive_conflict"
INDEX_FAILED = "index_failed"

REGISTRATION_PENDING = "registration_pending"

ACCEPTED = "accepted"
REPLACED = "replaced"
SKIPPED = "skipped"
FAILED = "failed"
RECOVERED = "recovered"
ERROR = "error"


class IntakeBusy(Exception):
    """已经有一个收件在跑。"""


@dataclass(frozen=True)
class Outcome:
    library: str
    file: str  # 相对数据目录
    result: str
    code: str | None = None  # 拒收的错误码，或者跳过、出错的原因
    packet_id: str | None = None
    revision: int | None = None


@dataclass
class IntakeReport:
    outcomes: list[Outcome] = field(default_factory=list)

    def count(self, result: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.result == result)


@contextmanager
def intake_lock(data_dir: Path) -> Iterator[None]:
    """同一时间只有一个收件在跑。锁跟着进程走，进程死了系统会放开，不会卡死。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / ".intake.lock", "a+b") as handle:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise IntakeBusy("已经有一个收件在跑") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _numbered(stem: str, suffix: str, number: int) -> str:
    return f"{stem}{suffix}" if number == 1 else f"{stem}_{number}{suffix}"


def free_path(directory: Path, filename: str) -> Path:
    """同名时加 _2、_3。"""
    stem, suffix = os.path.splitext(filename)
    number = 1
    while (directory / _numbered(stem, suffix, number)).exists():
        number += 1
    return directory / _numbered(stem, suffix, number)


def error_file_name(filename: str) -> str:
    """说明文件的名字：原文件名去掉 .md，加 .error.txt。"""
    return f"{filename[:-3] if filename.lower().endswith('.md') else filename}.error.txt"


def _free_failed_pair(directory: Path, filename: str) -> tuple[Path, Path]:
    """失败包和它的说明要一起加编号，两个名字都没被占才用。"""
    stem, suffix = os.path.splitext(filename)
    number = 1
    while True:
        name = _numbered(stem, suffix, number)
        target, error_file = directory / name, directory / error_file_name(name)
        if not target.exists() and not error_file.exists():
            return target, error_file
        number += 1


def _printable(text: str) -> str:
    """文件名也可能写了换行之类的东西，别让它改了说明文件的格式。"""
    return "".join(char if char.isprintable() else "?" for char in text)


def add_warning(
    conn: sqlite3.Connection,
    *,
    kind: str,
    now: str,
    packet_id: str = "",
    revision: int = 0,
    line: int = 0,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO patrol_warnings (kind, packet_id, revision, line, detail, first_seen_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (kind, packet_id, revision, line, detail) DO UPDATE SET"
        " last_seen_at = excluded.last_seen_at, seen_count = seen_count + 1, resolved_at = NULL, acknowledged_at = NULL",
        (kind, packet_id, revision, line, detail, now, now),
    )


def clear_derived_for_replacement(conn: sqlite3.Connection, packet_id: str, old_revision: int) -> None:
    """替换时清掉旧版本派生的事实和关系：证据来自旧版本的说法、旧版本的别名出处和隔离，受影响的事实和组重算，
    冻结的事实留着（pipeline.write.remove_revision）。新版本排队，下次 javmem write 写进来。它和换版本在同一个事务里。

    不动冻结的事实、主人定过的待确认、单条撤回的记录（packet-spec 第 10 节）。撤回（javmem.withdraw）也调
    remove_revision，再多清几样：冻结事实上指着撤回内容的证据、L3 结果库、向量缓存。
    """
    from javmem.pipeline.write import remove_revision  # 处理线引用收件这边的东西，放这里免得互相引用

    remove_revision(conn, packet_id, old_revision)


class _LibraryIntake:
    """一个库的收件：正式库和测试库各跑一遍，互不相通。"""

    def __init__(
        self,
        settings: Settings,
        library: Library,
        conn: sqlite3.Connection,
        registry: Registry,
        patterns: list[RedactionPattern],
        report: IntakeReport,
    ) -> None:
        self.settings = settings
        self.library = library
        self.conn = conn
        self.registry = registry
        self.patterns = patterns
        self.report = report
        self.data_dir = settings.require_data_dir()

    # ---- 小工具 ----

    def _now(self) -> str:
        return iso(self.settings.now())

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.data_dir).as_posix()

    def _record(self, file: str, result: str, **details: object) -> None:
        self.report.outcomes.append(Outcome(self.library.name, file, result, **details))

    def _current_row(self, packet_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM packets WHERE packet_id = ? AND state = 'current'", (packet_id,)
        ).fetchone()

    def _kept_path(self, dest: Path, packet_id: str, revision: int) -> Path:
        return dest.with_name(f"{packet_id}.r{revision}.md")

    # ---- 扫目录 ----

    def scan(self) -> None:
        root = self.library.inbox
        if not root.is_dir():
            return
        for entry in sorted(os.scandir(root), key=lambda item: item.name):
            path = Path(entry.path)
            if entry.is_symlink() or entry.is_junction():
                continue  # 不跟链接，不然数据目录外面的文件会被收走。巡检会提醒
            if entry.is_file():
                # 根目录不管什么文件都拒收，不读内容
                self._guarded(path, lambda p: self._reject_by_position(p, "文件直接放在收件根目录"))
            elif not entry.is_dir():
                continue
            elif entry.name in self.registry.sources:
                self._guarded(path, self._scan_source_dir)
            elif not entry.name.startswith("."):
                self._guarded(path, self._scan_unknown_dir)

    def _scan_source_dir(self, directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            # 只收这一层的 .md。别的文件和子目录不动，巡检会提醒
            if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".md"):
                self._guarded(Path(entry.path), self._process_file)

    def _scan_unknown_dir(self, directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            if entry.is_file(follow_symlinks=False):
                self._guarded(Path(entry.path), lambda p: self._reject_by_position(p, "所在目录不在登记表里"))

    def _guarded(self, path: Path, action: Callable[[Path], None]) -> None:
        """一个文件的问题不影响别的文件，它留在原地下次再试（比如被别的程序占着）。

        账本本身出了问题（锁住、磁盘满）才停下来，那不是这个文件的事。
        """
        try:
            action(path)
        except sqlite3.OperationalError:
            raise
        except Exception as error:  # noqa: BLE001 一个怪文件不能卡住后面所有的包
            self._record(self._relative(path), ERROR, code=type(error).__name__)

    # ---- 一个文件 ----

    def _process_file(self, path: Path) -> None:
        directory = path.parent.name
        raw = path.read_bytes()
        try:
            packet = validate_packet(
                raw,
                filename=path.name,
                directory=directory,
                library=self.library.name,
                registry=self.registry,
            )
        except Rejected as rejection:
            self._reject(path, directory, rejection, raw)
            return
        except Exception as error:  # noqa: BLE001 一个怪文件不能卡住后面所有的包
            self._reject(path, directory, Rejected(BAD_HEADER, f"解析时出错：{type(error).__name__}"), raw)
            return

        header = packet.header
        pending = self.conn.execute(
            "SELECT 1 FROM packets WHERE packet_id = ? AND state = 'registered'", (header.packet_id,)
        ).fetchone()
        if pending is not None:
            # 对账这次也没做完（比如归档目录写不了）。文件留在原地，下次再来
            self._record(self._relative(path), ERROR, code=REGISTRATION_PENDING, packet_id=header.packet_id)
            return

        withdrawn = self.conn.execute(
            "SELECT 1 FROM packets WHERE packet_id = ? AND withdrawn_at IS NOT NULL"
            " UNION ALL SELECT 1 FROM withdrawn_packets WHERE packet_id = ? LIMIT 1",
            (header.packet_id, header.packet_id),
        ).fetchone()
        if withdrawn is not None:
            # 撤回过的包号不再收，不管 revision 多大。要解除走管理命令（J2.11）
            add_warning(
                self.conn,
                kind=WITHDRAWN_RESENT,
                now=self._now(),
                packet_id=header.packet_id,
                revision=header.revision,
            )
            self._skip(path, packet, "withdrawn")
            return

        current = self._current_row(header.packet_id)
        if current is not None:
            if header.revision == current["revision"]:
                if packet.content_hash != current["content_hash"]:
                    # 可能改了内容忘了加 revision，也可能两个不同的包撞了号，要人看一下
                    add_warning(
                        self.conn,
                        kind=SAME_REVISION_DIFFERENT,
                        now=self._now(),
                        packet_id=header.packet_id,
                        revision=header.revision,
                    )
                self._skip(path, packet, "same_revision")
                return
            if header.revision < current["revision"]:
                self._skip(path, packet, "older_revision")
                return

        self._accept(path, packet, replacing=current is not None)

    def _reject_by_position(self, path: Path, detail: str) -> None:
        self._reject(path, UNKNOWN_DIR, Rejected(UNKNOWN_SOURCE, detail))

    def _reject(self, path: Path, folder: str, rejection: Rejected, raw: bytes | None = None) -> None:
        """失败包按文件所在的目录放，不看包头：包头可能写错，也可能是冒充。

        先挪文件再写说明：挪不动（比如被别的程序占着）就什么都不留，下一轮再试，
        不然每轮多一个说明文件。断在两步之间时包还在 failed 里，只是没有说明，巡检会提醒。
        raw 是读过的内容，放错位置的不读内容，就是 None。
        """
        directory = self.settings.failed_dir / folder
        directory.mkdir(parents=True, exist_ok=True)
        target, error_file = _free_failed_pair(directory, path.name)
        relative = self._relative(path)
        inbox = self._relative(path.parent)
        os.replace(path, target)
        error_file.write_text(
            f"code: {rejection.code}\n"
            f"file: {_printable(path.name)}\n"
            f"inbox: {_printable(inbox)}\n"
            f"checked_at: {self._now()}\n"
            f"detail: {rejection.detail}\n",
            encoding="utf-8",
            newline="\n",
        )
        if raw is not None:
            # 明文密钥留在 failed 里也要提醒。没有包号，用文件名
            self._warn_redaction(raw.decode("utf-8", errors="replace"), packet_id=os.path.splitext(path.name)[0])
        self._record(relative, FAILED, code=rejection.code)

    def _skip(self, path: Path, packet: Packet, reason: str) -> None:
        directory = self.library.archive / SKIPPED_DIR
        directory.mkdir(parents=True, exist_ok=True)
        relative = self._relative(path)
        os.replace(path, free_path(directory, path.name))
        header = packet.header
        self._warn_redaction(packet.text, packet_id=header.packet_id, revision=header.revision)
        self._record(relative, SKIPPED, code=reason, packet_id=header.packet_id, revision=header.revision)

    def _warn_redaction(self, text: str, *, packet_id: str, revision: int = 0) -> None:
        """打码模式表扫一遍，命中只提醒，只记类型。"""
        for hit_type in scan_types(text, self.patterns):
            add_warning(
                self.conn, kind=REDACTION_HIT, now=self._now(), packet_id=packet_id, revision=revision, detail=hit_type
            )

    # ---- 收下：登记 → 归档 → 收尾 ----

    def _accept(self, path: Path, packet: Packet, *, replacing: bool) -> None:
        relative = self._relative(path)
        header = packet.header
        self._register(path, packet)
        row = self.conn.execute(
            "SELECT * FROM packets WHERE packet_id = ? AND revision = ?", (header.packet_id, header.revision)
        ).fetchone()
        if self._archive(row):
            self._finish(row)
            self._record(
                relative, REPLACED if replacing else ACCEPTED, packet_id=header.packet_id, revision=header.revision
            )

    def _archive_relative(self, packet_id: str) -> str:
        year, month = packet_id[:4], packet_id[5:7]
        return f"{self.library.archive.name}/{year}/{month}/{packet_id}.md"

    def _register(self, path: Path, packet: Packet) -> None:
        header = packet.header
        now = self._now()
        # 现在没有本地模型，private 的只进原文层，不抽取。排队的从收到这一刻起算（queued_at，迁移 0029）
        extraction = "skipped" if header.privacy == "private" else "queued"
        self.conn.execute(
            "INSERT INTO packets (packet_id, revision, source, env, framework, channel, mailbox, kind, created_at,"
            " covers_from, covers_to, covers_from_epoch, covers_to_epoch, partitions, sensitive, privacy, entities,"
            " continues, content_hash, inbox_path, archive_path, state, extraction, received_at, queued_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, json(?), json(?), ?, json(?), ?, ?, ?, ?,"
            " 'registered', ?, ?, ?)",
            (
                header.packet_id,
                header.revision,
                header.source,
                header.env,
                header.framework,
                header.channel,
                header.mailbox,
                header.kind,
                iso(header.created_at),
                iso(header.covers_from),
                iso(header.covers_to),
                epoch(header.covers_from),
                epoch(header.covers_to),
                _json_list(header.domain),
                _json_list(header.tags),
                header.privacy,
                _json_list(header.entities),
                header.continues,
                packet.content_hash,
                self._relative(path),
                self._archive_relative(header.packet_id),
                extraction,
                now,
                now if extraction == "queued" else None,
            ),
        )

    def _archive(self, row: sqlite3.Row) -> bool:
        """把包写进归档。返回 False 表示文件没了或者变了，这次登记作废。重复调用没事。

        归档是 javmem 自己写的一份，不是把打包方的文件挪过来：这样属主是 javmem，只读才设得上
        （packet-spec 第 11 节每个来源可以是不同的系统用户）；落了盘才删收件目录那份，
        断电也不会两边都没有；写进去的就是核对过指纹的这份内容，不会被打包方中途换掉。
        """
        dest = self.data_dir / row["archive_path"]
        inbox_file = self.data_dir / row["inbox_path"]
        expected = row["content_hash"]
        raw = _read_if_same(inbox_file, expected)
        packet_id = row["packet_id"]
        dest.parent.mkdir(parents=True, exist_ok=True)

        current = self._current_row(packet_id)
        if current is not None:
            kept = self._kept_path(dest, packet_id, current["revision"])
            if kept.exists():
                if dest.exists():  # 旧包留档了，新包也到位了
                    self._clear_inbox(inbox_file, expected)
                    return True
                if raw is None:
                    os.replace(kept, dest)  # 新包没了：把旧包放回去，当没收过
                    return self._void(row)
            else:
                if raw is None:
                    return self._void(row)
                if dest.exists():
                    os.replace(dest, kept)
        elif dest.exists():
            if _hash_file(dest) == expected:  # 已经写过了
                self._clear_inbox(inbox_file, expected)
                return True
            if raw is None:
                return self._void(row)
            # 归档里有个包登记不认识的文件。不覆盖，挪开留着，巡检提醒
            os.replace(dest, free_path(dest.parent, f"{packet_id}.conflict.md"))
            add_warning(self.conn, kind=ARCHIVE_CONFLICT, now=self._now(), packet_id=packet_id)
        elif raw is None:
            return self._void(row)

        self._write_archive(dest, raw)
        self._clear_inbox(inbox_file, expected)
        return True

    def _write_archive(self, dest: Path, raw: bytes) -> None:
        """先写临时文件、落盘、设只读，再改名到位。归档里要么没有，要么是完整的只读文件。"""
        temporary = dest.with_name(f".{dest.name}.incoming")
        _remove_file(temporary)  # 上次断在这里留下的
        with open(temporary, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IREAD)
        os.replace(temporary, dest)
        _sync_directory(dest.parent)

    def _clear_inbox(self, inbox_file: Path, expected: str) -> None:
        """归档到位了，删收件目录那份。

        内容变了说明打包方又发了一份，留给下一轮。删不掉（比如被占着）也不影响这个包，
        下一轮它会按同样的 revision 跳过。
        """
        try:
            if _read_if_same(inbox_file, expected) is not None:
                inbox_file.unlink()
        except OSError:
            pass

    def _void(self, row: sqlite3.Row) -> bool:
        """登记了，文件却不见了。没做完的不算收过，巡检提醒。"""
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM packets WHERE packet_id = ? AND revision = ? AND state = 'registered'",
                (row["packet_id"], row["revision"]),
            )
            add_warning(
                self.conn,
                kind=REGISTRATION_LOST,
                now=self._now(),
                packet_id=row["packet_id"],
                revision=row["revision"],
            )
        self._record(row["inbox_path"], ERROR, code=REGISTRATION_LOST, packet_id=row["packet_id"])
        return False

    def _finish(self, row: sqlite3.Row) -> None:
        """收尾：一个事务里换版本、换原文索引、记警告、排队抽取。从归档读，归档是真相。"""
        packet_id, revision = row["packet_id"], row["revision"]
        archived = self.data_dir / row["archive_path"]
        packet = parse_packet(archived.read_bytes(), filename=f"{packet_id}.md")
        now = self._now()

        with transaction(self.conn):
            current = self._current_row(packet_id)
            if current is not None:
                self._retire(current, archived, now)
            replace_packet_sentences(self.conn, packet)
            # 新来的这一版里，复述、引用撤回内容的句子也标上撤回（指纹只认得出原来那一句）
            from javmem.withdraw import mark_on_arrival  # 撤回那边引用收件这边的东西，放这里免得互相引用

            mark_on_arrival(self.conn, packet, now)
            for warning in packet.warnings:
                add_warning(
                    self.conn,
                    kind=warning.kind,
                    now=now,
                    packet_id=packet_id,
                    revision=revision,
                    line=warning.line,
                    detail=warning.detail,
                )
            self._warn_redaction(packet.text, packet_id=packet_id, revision=revision)
            self.conn.execute(
                "UPDATE packets SET state = 'current', indexed_at = ? WHERE packet_id = ? AND revision = ?",
                (now, packet_id, revision),
            )

    def _retire(self, old: sqlite3.Row, archived: Path, now: str) -> None:
        """旧版本让位：留档的路径写回登记，它的警告不再提醒，派生的东西清掉。"""
        packet_id, revision = old["packet_id"], old["revision"]
        kept = self._kept_path(archived, packet_id, revision)
        self.conn.execute(
            "UPDATE packets SET state = 'superseded', superseded_at = ?, archive_path = ?"
            " WHERE packet_id = ? AND revision = ?",
            (now, self._relative(kept), packet_id, revision),
        )
        self.conn.execute(
            "UPDATE patrol_warnings SET resolved_at = ? WHERE packet_id = ? AND revision = ? AND resolved_at IS NULL",
            (now, packet_id, revision),
        )
        clear_derived_for_replacement(self.conn, packet_id, revision)

    # ---- 启动对账 ----

    def reconcile(self) -> None:
        """上次没做完的接着做。停在 registered 的，要么文件还在收件目录，要么已经进了归档。"""
        rows = self.conn.execute("SELECT * FROM packets WHERE state = 'registered' ORDER BY received_at").fetchall()
        for row in rows:
            try:
                if self._archive(row):
                    self._finish(row)
                    self._record(row["inbox_path"], RECOVERED, packet_id=row["packet_id"], revision=row["revision"])
            except sqlite3.OperationalError:
                raise  # 账本本身出了问题，停下来
            except OSError as error:
                self._record(row["inbox_path"], ERROR, code=type(error).__name__, packet_id=row["packet_id"])
            except Exception as error:  # noqa: BLE001 一个包收不完也不能卡住后面的包
                add_warning(
                    self.conn,
                    kind=INDEX_FAILED,
                    now=self._now(),
                    packet_id=row["packet_id"],
                    revision=row["revision"],
                )
                self._record(row["inbox_path"], ERROR, code=type(error).__name__, packet_id=row["packet_id"])

    # ---- 记一下跑过了 ----

    def mark_started(self) -> None:
        self.conn.execute(
            "INSERT INTO task_runs (task, started_at) VALUES (?, ?)"
            " ON CONFLICT (task) DO UPDATE SET started_at = excluded.started_at",
            (TASK_INTAKE, self._now()),
        )

    def mark_finished(self) -> None:
        self.conn.execute("UPDATE task_runs SET finished_at = ? WHERE task = ?", (self._now(), TASK_INTAKE))


def _json_list(items: tuple[str, ...]) -> str:
    return json.dumps(list(items), ensure_ascii=False)


def _hash_file(path: Path) -> str | None:
    return content_hash(path.read_bytes())


def _read_if_same(path: Path, expected: str) -> bytes | None:
    """文件还是登记时的内容，就返回它。没了或者变了返回 None。读不了照样抛错，下次再试。"""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return raw if content_hash(raw) == expected else None


def _remove_file(path: Path) -> None:
    """删一个可能是只读的文件。Windows 上只读文件删不掉。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)
    except FileNotFoundError:
        pass


def _sync_directory(directory: Path) -> None:
    """让改名落盘。Windows 打不开目录，它的文件系统自己有日志。"""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reconcile_library(settings: Settings, library: Library, conn: sqlite3.Connection) -> IntakeReport:
    """只对账一个库：停在 registered 的接着做完，不扫收件目录。调用方要先拿收件锁（intake_lock）。

    撤回之前跑这个：不然停在 registered 的新版本会在撤回以后转正，撤回的内容又回来了。
    """
    report = IntakeReport()
    nobody = Registry(sources={}, domains=frozenset())  # 对账不看登记表
    worker = _LibraryIntake(settings, library, conn, nobody, load_patterns(settings.redaction_file), report)
    worker.reconcile()
    return report


def run_intake(settings: Settings) -> IntakeReport:
    """跑一遍收件：先对账，再扫目录，正式库和测试库各一遍。

    配置有问题在这里就报错，一个文件都不动。
    """
    data_dir = settings.require_data_dir()
    registry = load_registry(settings.sources_file)
    patterns = load_patterns(settings.redaction_file)

    report = IntakeReport()
    with intake_lock(data_dir):
        for library in settings.libraries():
            conn = open_ledger(library)
            try:
                worker = _LibraryIntake(settings, library, conn, registry, patterns, report)
                worker.mark_started()
                worker.reconcile()
                worker.scan()
                worker.mark_finished()
            finally:
                conn.close()
    return report
