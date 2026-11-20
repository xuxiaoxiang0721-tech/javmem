"""测试的公共工具。

样例尽量直接从两份接口文档里取：规范第 14 节的两个包、第 11 节的登记表、查询接口第 2 节的身份配置。
文档一改，测试跟着变，不会各说各的。文档里的名字都是编的。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from javmem.config import Settings
from javmem.intake import IntakeReport, run_intake
from javmem.ledger import open_ledger

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKET_SPEC = (REPO_ROOT / "docs" / "packet-spec.md").read_text(encoding="utf-8")
QUERY_API = (REPO_ROOT / "docs" / "query-api.md").read_text(encoding="utf-8")


def fenced(document: str, heading: str, opening: str, nth: int = 0) -> str:
    """取 heading 之后第 nth 个代码块的内容。opening 是开头那一行，比如 ```yaml。"""
    section = document.split(f"\n{heading}", 1)[1]
    fence = opening[: len(opening) - len(opening.lstrip("`"))]
    start = -1
    for _ in range(nth + 1):
        start = section.index(f"\n{opening}\n", start + 1)
    begin = start + len(opening) + 2
    return section[begin : section.index(f"\n{fence}", begin)] + "\n"


CHAT_PACKET = fenced(PACKET_SPEC, "### 14.1", "````markdown")
MAIL_PACKET = fenced(PACKET_SPEC, "### 14.2", "````markdown")
CHAT_ID = "2026-11-08_2330_desk-bot"
MAIL_ID = "2026-11-03_1800_mail-sales-a"
SOURCES_YAML = fenced(PACKET_SPEC, "## 11.", "```yaml")
ACCESS_YAML = fenced(QUERY_API, "## 2.", "```yaml")

SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_BODY = "## 原话 · 10:30–10:31\n- [10:30] 我：第一句话。\n- [10:31] desk-bot：好的。\n"


def build_packet(
    source: str = "desk-bot",
    stamp: str = "2026-11-08_2330",
    *,
    body: str = DEFAULT_BODY,
    packet_id: str | None = None,
    drop: tuple[str, ...] = (),
    **fields: object,
) -> str:
    """拼一个合规的对话包。fields 覆盖或增加包头字段，值原样写进 YAML；drop 去掉字段。"""
    header: dict[str, object] = {
        "schema": "javmem-packet/1",
        "packet_id": packet_id or f"{stamp}_{source}",
        "created_at": "2026-11-08T23:30:00+08:00",
        "source": source,
        "framework": "demo-chat",
        "channel": "chat",
        "env": "prod",
        "kind": "daily",
        "covers_from": "2026-11-07T23:30:00+08:00",
        "covers_to": "2026-11-08T23:30:00+08:00",
        "domain": "[business]",
        "privacy": "normal",
    }
    header.update(fields)
    lines = [f"{key}: {value}" for key, value in header.items() if key not in drop]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body


def mail_header_block(**overrides: object) -> str:
    """一封邮件的邮件头围栏，overrides 里值是 None 的字段去掉。"""
    fields: dict[str, object] = {
        "message_id": '"<a@example.org>"',
        "thread_id": '"<a@example.org>"',
        "in_reply_to": "null",
        "from": '{name: "Tran Mai", addr: "tran.mai@example.org"}',
        "to": '[{name: "李明", addr: "li.ming@example.com"}]',
        "date": "2026-11-03T09:12:00+07:00",
        "subject": '"Hello"',
    }
    fields.update(overrides)
    lines = [f"{key}: {value}" for key, value in fields.items() if value is not None]
    return "```yaml\n" + "\n".join(lines) + "\n```\n"


def build_mail_packet(*, sections: str | None = None, **fields: object) -> str:
    """拼一个合规的邮件包。"""
    body = sections if sections is not None else f"## 原话 · 邮件 · 2026-11-03 09:12\n\n{mail_header_block()}\n  正文。\n"
    defaults: dict[str, object] = {
        "framework": "mail-import",
        "channel": "email",
        "mailbox": "li.ming@example.com",
        "covers_from": "2026-11-02T18:00:00+08:00",
        "covers_to": "2026-11-03T18:00:00+08:00",
    }
    defaults.update(fields)
    return build_packet("mail-sales-a", "2026-11-03_1800", body=body, **defaults)


def file_name(packet: str) -> str:
    """从包头取文件名：packet_id 加 .md。"""
    return re.search(r"^packet_id: (.*)$", packet, re.MULTILINE).group(1).strip() + ".md"


@dataclass
class World:
    """一个临时数据目录，配好了文档里的登记表和身份。"""

    data: Path
    settings: Settings

    def drop(self, folder: str, content: str | bytes, name: str | None = None) -> Path:
        """按规范投递：先写 .tmp，再改名移进目录。folder 相对数据目录。"""
        directory = self.data / folder
        directory.mkdir(parents=True, exist_ok=True)
        if name is None:
            name = file_name(content if isinstance(content, str) else content.decode("utf-8", "replace"))
        temporary = directory / ".tmp"
        temporary.mkdir(exist_ok=True)
        path = temporary / name
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        target = directory / name
        os.replace(path, target)
        return target

    def intake(self) -> IntakeReport:
        return run_intake(self.settings)

    def ledger(self, library: str = "prod") -> sqlite3.Connection:
        return open_ledger(self.settings.library(library))

    def rows(self, sql: str, *params: object, library: str = "prod") -> list[sqlite3.Row]:
        conn = self.ledger(library)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def execute(self, sql: str, *params: object, library: str = "prod") -> None:
        conn = self.ledger(library)
        try:
            conn.execute(sql, params)
        finally:
            conn.close()

    def packet_row(self, packet_id: str, revision: int = 1, library: str = "prod") -> sqlite3.Row | None:
        rows = self.rows(
            "SELECT * FROM packets WHERE packet_id = ? AND revision = ?", packet_id, revision, library=library
        )
        return rows[0] if rows else None

    def files(self, folder: str) -> list[str]:
        """某个目录下的全部文件，相对这个目录，不算 .tmp。"""
        root = self.data / folder
        if not root.exists():
            return []
        return sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and ".tmp" not in path.parts
        )

    def at(self, moment: datetime) -> World:
        """让这个世界的时钟停在某一刻。"""
        return replace(self, settings=replace(self.settings, clock=lambda: moment))

    def as_caller(self, caller: str) -> World:
        return replace(self, settings=replace(self.settings, caller=caller))


def warnings_of(world: World, packet_id: str, library: str = "prod") -> list[tuple[str, int]]:
    rows = world.rows(
        "SELECT kind, line FROM patrol_warnings WHERE packet_id = ? AND resolved_at IS NULL ORDER BY line, kind",
        packet_id,
        library=library,
    )
    return [(row["kind"], row["line"]) for row in rows]


def as_json(value: object) -> object:
    """绕一圈 JSON，确认返回值真能序列化。"""
    return json.loads(json.dumps(value, ensure_ascii=False))
