"""`javmem score` 的参数和跑法（J3.8）。不调模型，只读账本、结果库和金标准，写进 --out（放仓库外）。

    javmem score --gold 金标准目录 [--gold-extra 盲测目录] [--review 审阅结果.json]
                 --side javmem=结果文件夹|账本.db|l3.db [--store javmem=L3结果库] [--extra javmem=另写的.json]
                 [--side 对手=对齐格式.json] [--bridge 别名表.yaml] [--bridge 底表.md] [--library test]
                 [--sentences 邮件的原文账本.db] [--units P01,P04] [--tolerance-days 7] [--scores-only] [--out 输出目录]

- `--side 名字=路径`：`.json` 的当对齐格式读（javmem.scoring.common）；别的当一份抽取结果读（javmem.scoring.results）：
  结果文件夹（数据目录：`ledger/<库>.db` 加 `l3/l3.db`）、账本、L3 的结果库都行，关系从结果库的 L3 结果里读。
  可以写几次，一边一个。
- `--store 名字=路径`：结果库不在账本旁边的时候点名（目录或者 l3.db）。
- `--library`：结果文件夹里用哪个库的账本，默认 test。
- `--scores-only`：只出分数（D111，给写提示词的会话 J3.9 自己打分）：打印各项分数和按错误类型的条数，例子只用这一边
  自己抽错的；不打印、不写金标准的条目、原话、名字。给了 `--out` 只写 `分数.json`（去掉了金标准的编号）和 `给J3.9.md`。
- `--extra 名字=路径`：另写的对齐格式，并进那一边（J3.9 的关系先在 L3 的结果里，转成对齐格式从这里并进来）。
- `--bridge`：桥，名字不一样的对上用。`.yaml`、`.yml` 是别名表（第一版、第二版都认），`.md` 是带“标准名 | 叫法”两列的表格。
- `--sentences`：邮件单元要知道一句是哪个往来串的，给邮件的原文账本（只读）。没给的用这一边自己的账本（里面有原文
  索引）；都没有的，邮件单元不打分。
- 退出码：0 打完了；2 金标准、审阅结果、对齐格式、账本写得不对，什么都没写。
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from javmem.config import ConfigError
from javmem.scoring.common import ScoreInputError, Side, load_side
from javmem.scoring.dates import TOLERANCE_DAYS
from javmem.scoring.gold import SentenceIndex, load_gold, unit_files
from javmem.scoring.ledger import sentences_from_ledger
from javmem.scoring.report import Options, bridge_groups, merge_sides, score, scores_only, write_outputs
from javmem.scoring.results import mail_sentences, side_from_result
from javmem.zh.lexicon import load_lexicon

EXIT_OK = 0
EXIT_INPUT = 2


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gold", required=True, help="金标准的目录（一个单元一个 JSON，实体表也在这里）")
    parser.add_argument("--gold-extra", action="append", default=[], help="另外的单元：目录或文件（比如盲测那两包），可以写几次")
    parser.add_argument("--entities", help="金标准的实体表，默认 --gold 目录下的 实体表.json")
    parser.add_argument("--review", help="审阅结果（JSON，按条目编号：{v: ok|wrong|fix, text}），没给照预标打")
    parser.add_argument("--side", action="append", default=[], required=True,
                        help="名字=路径：.json 是对齐格式；别的是一份抽取结果（结果文件夹、账本或者 L3 的结果库）")
    parser.add_argument("--store", action="append", default=[], help="名字=路径：结果库不在账本旁边时点名（算成本、读关系）")
    parser.add_argument("--library", default="test", choices=("test", "prod"), help="结果文件夹里用哪个库的账本，默认 test")
    parser.add_argument("--extra", action="append", default=[], help="名字=路径：另写的对齐格式，并进那一边")
    parser.add_argument("--bridge", action="append", default=[], help="别名表（.yaml）或者带“标准名 | 叫法”的表格（.md），可以写几次")
    parser.add_argument("--sentences", action="append", default=[], help="邮件的原文账本（只读），邮件单元分得出是哪一串的句子")
    parser.add_argument("--units", help="只打这几个单元，逗号隔开")
    parser.add_argument("--tolerance-days", type=int, default=TOLERANCE_DAYS,
                        help="关系的日子允许差几天（开始、结束都是），默认 7（D120；04 p7 原来是 3）")
    parser.add_argument("--scores-only", action="store_true",
                        help="只出分数和按错误类型的条数，不出金标准的条目、原话、名字（给写提示词的会话 J3.9）")
    parser.add_argument("--out", help="输出目录（放仓库外：里面有金标准的原话）；--scores-only 时可以不给，只打印")


def _pairs(values: Sequence[str], what: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for value in values:
        name, sep, path = value.partition("=")
        if not sep or not name or not path:
            raise ScoreInputError(f"{what} 要写成 名字=路径")
        if name in out:
            raise ScoreInputError(f"{what} 的名字 {name} 写了两次")
        out[name] = Path(path)
    return out


def inside_repo(path: Path) -> bool:
    """输出目录在不在 javmem 的仓库里（有 .git、有 src/javmem 的那一层底下）：输出里有金标准的原话，不能进仓库。"""
    for folder in (path.resolve(), *path.resolve().parents):
        if (folder / ".git").exists() and (folder / "src" / "javmem").is_dir():
            return True
    return False


def run(args: argparse.Namespace, out=print) -> int:  # noqa: A002
    if args.out is None and not args.scores_only:
        out("打不了分：要给 --out（只出分数的 --scores-only 可以不给）")
        return EXIT_INPUT
    if args.out is not None and inside_repo(Path(args.out)):
        out("打不了分：输出目录在仓库里。输出里有金标准的原话和真实的抽取结果，要放仓库外")
        return EXIT_INPUT
    try:
        gold_dir = Path(args.gold)
        entities = Path(args.entities) if args.entities else gold_dir / "实体表.json"
        gold = load_gold(unit_files(gold_dir, [Path(one) for one in args.gold_extra]), entities,
                         Path(args.review) if args.review else None)
        packets = sorted({packet for unit in gold.units.values() for packet in unit.packets})
        sides_paths = _pairs(args.side, "--side")
        stores = _pairs(args.store, "--store")
        extras = _pairs(args.extra, "--extra")
        for name in (*stores, *extras):
            if name not in sides_paths:
                raise ScoreInputError(f"--store、--extra 的 {name} 没有对应的 --side")
        sides: dict[str, Side] = {}
        own_ledgers: dict[str, Path | None] = {}
        for name, path in sides_paths.items():
            if path.suffix.lower() == ".json":
                if name in stores:
                    raise ScoreInputError(f"--store 只给抽取结果那一边用，{name} 是对齐格式")
                side = load_side(path)
                side = replace(side, name=name, described={**side.described, "文件": str(path)})
                own_ledgers[name] = None
            else:
                side, own_ledgers[name] = side_from_result(path, packets, name=name, library=args.library,
                                                           store=stores.get(name))
            if name in extras:
                side = merge_sides(side, load_side(extras[name]))
            sides[name] = side
        lexicons, tables = [], []
        for one in args.bridge:
            path = Path(one)
            if path.suffix.lower() in (".yaml", ".yml"):
                lexicons.append((path.name, load_lexicon(path)))
            else:
                tables.append((path.name, path.read_text(encoding="utf-8")))
        groups = bridge_groups(lexicons, tables)
        sentences: SentenceIndex | None = None
        mail_packets = sorted({p for unit in gold.units.values() if not unit.is_dialog for p in unit.packets})
        if args.sentences:
            message: dict[str, str | None] = {}
            thread: dict[str, str | None] = {}
            for one in args.sentences:
                found = sentences_from_ledger(Path(one), mail_packets)
                message.update(found.message)
                thread.update(found.thread)
            sentences = SentenceIndex(message, thread)
        per_side = {name: sentences if sentences is not None else mail_sentences(own_ledgers[name], mail_packets)
                    for name in sides}
        only = [one.strip() for one in args.units.split(",") if one.strip()] if args.units else None
        if only is not None:
            unknown = sorted(set(only) - set(gold.units))
            if unknown:
                raise ScoreInputError(f"--units 里有金标准没有的单元：{'、'.join(unknown)}")
        options = Options(tolerance_days=args.tolerance_days, only_units=only)
        results = {name: score(side, gold, groups=groups, sentences=per_side[name], options=options)
                   for name, side in sides.items()}
    except (ScoreInputError, ConfigError, sqlite3.Error, OSError, UnicodeDecodeError) as error:
        out(f"打不了分：{error}")
        return EXIT_INPUT
    if args.scores_only:
        out(scores_only(gold, results, Path(args.out) if args.out else None).rstrip("\n"))
        return EXIT_OK
    payload = write_outputs(Path(args.out), gold, results)
    for name, numbers in payload["sides"].items():  # type: ignore[union-attr]
        facts, relations, changes, aliases, cost = (numbers[k] for k in ("facts", "relations", "changes", "aliases", "cost"))
        out(
            f"{name}：单元 {len(numbers['units_scored'])} 个；关系准确率 {_pct(relations['precision'])}、覆盖率 "
            f"{_pct(relations['recall'])}（合成后判、没写开始日子算对；严格的 {_pct(relations['precision_strict'])}、"
            f"{_pct(relations['recall_strict'])}）；变化判对 {_pct(changes['rate'])}；别名对齐 {_pct(aliases['rate'])}；"
            f"每包 {cost['tokens_per_packet'] if cost['tokens_per_packet'] is not None else '—'} token；"
            f"事实完整 {_pct(facts['complete_rate'])}、对得上金标准 {_pct(facts['precision'])}"
        )
    out(f"写到 {args.out}")
    return EXIT_OK


def _pct(value: object) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"  # type: ignore[arg-type]
