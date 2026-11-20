"""录好的模型回复（U2 甲，D205 ①，GitHub 那一层）：把一个 L3 结果库整个导成 JSON 文本（进仓库），跑的时候再建回结果库。

编的小套件第一次用真模型抽一遍（`javmem extract`，花一次钱），`dump_store` 把结果库里每张表的每一行导出来
（`format: javmem-l3-replies/1`）；CI 上 `build_store` 照这份 JSON 建一个新的结果库（表结构照现在的代码建），
`javmem write` 照它写账，不调模型。代码改了窗口的样子（L1、L2、渲染）、提示词、键的算法，结果库里就对不上，
`javmem regress` 报“跑不了：要重抽”——这时候照“录回复”的说明重录一遍（花钱，照 D205 ② 批）。

只导出、导入，不改任何一行；字节（BLOB）写成 `{"$b64": …}`。导出的文件里有窗口文字和模型的回复：只给编的包用。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

FORMAT = "javmem-l3-replies/1"


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _plain(value: object) -> object:
    return {"$b64": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value


def _back(value: object) -> object:
    if isinstance(value, dict) and set(value) == {"$b64"}:
        return base64.b64decode(value["$b64"])
    return value


def dump_store(store: Path, out: Path, *, note: str = "") -> dict[str, int]:
    """结果库（l3.db 或者它所在的目录）→ JSON。返回每张表几行。"""
    path = store / "l3.db" if store.is_dir() else store
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = {}
        for name in _tables(conn):
            cursor = conn.execute(f"SELECT * FROM {name} ORDER BY rowid")  # noqa: S608 - 表名是 sqlite_master 里的
            columns = [one[0] for one in cursor.description]
            tables[name] = {"columns": columns, "rows": [[_plain(value) for value in row] for row in cursor]}
    finally:
        conn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"format": FORMAT, "note": note, "tables": tables}, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    return {name: len(table["rows"]) for name, table in tables.items()}


def build_store(replies: Path, target: Path) -> dict[str, int]:
    """JSON → 一个新的结果库（target 目录下的 l3.db，表结构照现在的代码建）。表、列对不上的报 ValueError（要重录）。"""
    from javmem.pipeline.store import ResultStore

    data = json.loads(replies.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(f"{replies.name} 不是 {FORMAT}")
    if (target / "l3.db").exists():
        raise ValueError(f"{target / 'l3.db'} 已经有了")
    with ResultStore(target):  # 照现在的代码建好表
        pass
    conn = sqlite3.connect(target / "l3.db")
    counts = {}
    try:
        known = set(_tables(conn))
        with conn:
            for name, table in data["tables"].items():
                if name not in known:
                    raise ValueError(f"录的回复里有 {name} 这张表，现在的结果库没有：要重录")
                have = {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}
                columns = table["columns"]
                missing = [column for column in columns if column not in have]
                if missing:
                    raise ValueError(f"{name} 表现在没有 {', '.join(missing)} 这几列：要重录")
                marks = ", ".join("?" for _ in columns)
                conn.executemany(f"INSERT INTO {name} ({', '.join(columns)}) VALUES ({marks})",  # noqa: S608
                                 [[_back(value) for value in row] for row in table["rows"]])
                counts[name] = len(table["rows"])
    finally:
        conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    """用法 `python -m javmem.regress.replies dump <结果库的目录或 l3.db> <输出.json> [--note 文字]`（只导出；建回是回归自己做的）。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m javmem.regress.replies")
    commands = parser.add_subparsers(dest="command", required=True)
    dump = commands.add_parser("dump", help="结果库整个导成 JSON（只给编的包用：里面有窗口文字和模型的回复）")
    dump.add_argument("store")
    dump.add_argument("out")
    dump.add_argument("--note", default="")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    counts = dump_store(Path(args.store), Path(args.out), note=args.note)
    print("导出了：" + "，".join(f"{name} {count} 行" for name, count in counts.items() if count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
