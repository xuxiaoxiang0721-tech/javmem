"""校准工具包的命令（U3）：`javmem gold …`。"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from javmem.config import Settings

EXIT_OK, EXIT_PROBLEMS, EXIT_INPUT = 0, 1, 2
Out = Callable[[str], None]
ENTITY_FILE = "实体表.json"


def add_gold_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="gold_command", required=True)
    check = commands.add_parser("check", help="核一份金标准写得对不对：只报条目编号和哪一种不对，不抄内容")
    check.add_argument("--gold", required=True, help="金标准的目录（每个单元一个 JSON，加一份 实体表.json），或者一个单元的文件")
    check.add_argument("--entities", help=f"实体表，默认金标准目录里的 {ENTITY_FILE}")
    check.add_argument("--library", choices=("prod", "test"), default="test", help="出处的句子对着哪个库的账本核，默认 test")

    pre = commands.add_parser("prelabel", help="照账本里现在抽出来的，出一份金标准的草稿（预标），给人审")
    pre.add_argument("--library", choices=("prod", "test"), default="test", help="哪个库，默认 test")
    pre.add_argument("--packet", action="append", required=True, help="挑哪几个包（十来个就够），可以写几次")
    pre.add_argument("--unit", default="D1", help="单元的编号（字母、数字、连字符），默认 D1")
    pre.add_argument("--out", required=True, help="写到这个目录（一个单元一个文件，加一份实体表）；要在仓库外、是新的或空的")

    review = commands.add_parser("review", help="把一份金标准做成一页本机打开的审阅页面：对、错、改，补一条")
    review.add_argument("--gold", required=True, help="金标准的目录，或者一个单元的文件")
    review.add_argument("--entities", help=f"实体表，默认金标准目录里的 {ENTITY_FILE}")
    review.add_argument("--library", choices=("prod", "test"), default="test", help="卡片上的原话从哪个库的账本取，默认 test")
    review.add_argument("--review", help="上一次存下的审阅结果：页面照它先选好，接着审")
    review.add_argument("--out", required=True, help="审阅页面（.html），要在仓库外")


def _outside_repo(path: Path, out: Out, what: str) -> bool:
    from javmem.scoring.run import inside_repo

    if inside_repo(path):
        out(f"{what}在仓库里：里面有原话，要放仓库外")
        return False
    return True


def _ledger(settings: Settings, library_name: str):  # noqa: ANN202 - (路径, 连接) 或者 (None, None)
    from javmem.ledger import connect_readonly

    if settings.data_dir is None:
        return None, None
    library = settings.library(library_name)
    if not library.db_path.exists():
        return None, None
    return library.db_path, connect_readonly(library.db_path)


def _prelabel(args: argparse.Namespace, settings: Settings, out: Out) -> int:
    import json
    import re

    from javmem.calib.prelabel import prelabel

    unit = str(args.unit)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,19}", unit):
        out("单元的编号只能是字母、数字、连字符，最多 20 个字")
        return EXIT_INPUT
    target = Path(args.out)
    if not _outside_repo(target, out, "草稿的目录"):
        return EXIT_INPUT
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        out(f"{target} 不是新的空目录")
        return EXIT_INPUT
    path, conn = _ledger(settings, args.library)
    if conn is None:
        out(f"{args.library} 库还没有账本（或者没设 JAVMEM_DATA_DIR）：先收包、抽取、写账")
        return EXIT_INPUT
    try:
        packets = list(dict.fromkeys(args.packet))
        current = {str(row[0]) for row in conn.execute(
            f"SELECT packet_id FROM packets WHERE state = 'current' AND withdrawn_at IS NULL AND packet_id IN ({', '.join('?' for _ in packets)})",
            packets)}
        missing = [packet for packet in packets if packet not in current]
        if missing:
            out(f"库里没有这个包（或者撤回了）：{'、'.join(missing)}")
            return EXIT_INPUT
        unit_json, table = prelabel(path, conn, packets, unit)
    finally:
        conn.close()
    target.mkdir(parents=True, exist_ok=True)
    for name, value in ((f"{unit}.json", unit_json), (ENTITY_FILE, table)):
        (target / name).write_bytes((json.dumps(value, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    items = unit_json["items"]  # type: ignore[index]
    counts: dict[str, int] = {}
    for item in items:  # type: ignore[union-attr]
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    out(f"草稿写好了：{target}（{len(packets)} 个包，" + "、".join(f"{kind} {count} 条" for kind, count in sorted(counts.items()))
        + f"，实体 {len(table['entities'])} 个）")  # type: ignore[arg-type]
    out("这是 javmem 自己抽的，不是标准答案：用 javmem gold review 一条一条审，漏了的补上，审完再拿来打分")
    return EXIT_OK


def _review(args: argparse.Namespace, settings: Settings, out: Out) -> int:
    import json

    from javmem.calib.review import build_page
    from javmem.scoring.common import ScoreInputError
    from javmem.scoring.gold import load_gold

    target = Path(args.out)
    if not _outside_repo(target, out, "审阅页面"):
        return EXIT_INPUT
    files, table = _gold_files(Path(args.gold), args.entities)
    if not files:
        out(f"金标准读不了：{args.gold} 里没有单元的文件")
        return EXIT_INPUT
    try:
        load_gold(files, table)  # 结构先照打分的读法核一遍
        units = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        earlier = json.loads(Path(args.review).read_text(encoding="utf-8")) if args.review else None
    except (ScoreInputError, OSError, ValueError) as error:
        out(f"金标准、审阅结果读不了：{error}")
        return EXIT_INPUT
    if earlier is not None and not isinstance(earlier, dict):
        out("审阅结果要是一个对象（reviews、additions）")
        return EXIT_INPUT
    sentences: dict[str, str] = {}
    _, conn = _ledger(settings, args.library)
    if conn is not None:
        try:
            sids = sorted({str(one.get("sid")) for unit in units for item in unit.get("items") or []
                           for one in item.get("evidence") or [] if isinstance(one, dict)})
            for start in range(0, len(sids), 400):
                chunk = sids[start : start + 400]
                for row in conn.execute(f"SELECT sentence_id, text FROM raw_sentences WHERE sentence_id IN ({', '.join('?' for _ in chunk)})", chunk):
                    sentences[str(row[0])] = str(row[1])
        finally:
            conn.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_page(units, sentences, earlier).encode("utf-8"))
    count = sum(len(unit.get("items") or []) for unit in units)
    out(f"审阅页面写好了：{target}（{len(units)} 个单元、{count} 条{'' if sentences else '；没有数据目录，卡片上只有句子编号'}）")
    out("用浏览器打开它（双击就行），审完点“存下来”；下载的 审阅.json 交给 javmem score --review")
    return EXIT_OK


def _gold_files(gold: Path, entities: str | None) -> tuple[list[Path], Path]:
    from javmem.scoring.gold import unit_files

    files = unit_files(gold) if gold.is_dir() else [gold]
    table = Path(entities) if entities else (gold if gold.is_dir() else gold.parent) / ENTITY_FILE
    return files, table


def _check(args: argparse.Namespace, settings: Settings, out: Out) -> int:
    from javmem.calib.gold_check import check
    from javmem.ledger import connect_readonly
    from javmem.scoring.common import ScoreInputError
    from javmem.scoring.gold import load_gold

    files, table = _gold_files(Path(args.gold), args.entities)
    if not files:
        out(f"金标准读不了：{args.gold} 里没有单元的文件")
        return EXIT_INPUT
    try:
        gold = load_gold(files, table)
    except ScoreInputError as error:
        out(f"金标准读不了：{error}")
        return EXIT_INPUT
    conn = None
    note = ""
    if settings.data_dir is None:
        note = "没有数据目录（JAVMEM_DATA_DIR）：出处的句子账本里有没有、原话片段对不对这两样没核"
    else:
        library = settings.library(args.library)
        if library.db_path.exists():
            conn = connect_readonly(library.db_path)
        else:
            note = f"{args.library} 库还没有账本：出处的句子账本里有没有、原话片段对不对这两样没核"
    try:
        result = check(gold, conn)
    finally:
        if conn is not None:
            conn.close()
    out(f"金标准：{result.units} 个单元、{result.items} 条；有问题的 {result.bad_items()} 条")
    for item_id, kind in result.problems:
        out(f"  {item_id}：{kind}")
    if result.problems:
        out("几种各几条：" + "，".join(f"{kind} {count}" for kind, count in sorted(result.kinds().items())))
    if note:
        out(note)
    return EXIT_PROBLEMS if result.problems else EXIT_OK


def run_gold(args: argparse.Namespace, settings: Settings, out: Out = print) -> int:
    if args.gold_command == "check":
        return _check(args, settings, out)
    if args.gold_command == "prelabel":
        return _prelabel(args, settings, out)
    if args.gold_command == "review":
        return _review(args, settings, out)
    out(f"不认得：javmem gold {args.gold_command}")
    return EXIT_INPUT
