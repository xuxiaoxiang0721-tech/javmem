"""`javmem soft` 的参数和跑法（J4.5）：find 找候选、judge 调 Claude 判、score 对答案。

- `find`：只读账本（`--library` 数据目录里的库，或者 `--ledger` 一个账本文件，比如拷贝），写题目文件。不调模型。
- `judge`：判一批题，结论存进 soft.db（默认数据目录的 `soft/`，`--store` 改），`--out` 写结论文件。`--fake` 用假模型，
  `--dry-run` 只数要调几道、多少字（不要密钥）。真调要 JAVMEM_SOFT_API_KEY，token 记进 JAVMEM_TOKEN_LEDGER
  （没配是数据目录的 logs/token-ledger.csv），标签 `--tag`，预算 `--budget`。
- `score`：拿复核过的答案对结论文件，打印每一组的数，`--out` 另写一份 JSON。

题目、结论里有原话，`--out`、`--store` 都不能放在仓库里。`--limit` 要是正整数。真调之前先把客户端建好：没装 SDK、
有不该有的 ANTHROPIC_* 环境变量，开跑之前就报配置错误。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from javmem.config import Settings
from javmem.ledger import connect_readonly
from javmem.pipeline.extract import ExtractOptions
from javmem.redaction import load_patterns
from javmem.scoring.run import inside_repo
from javmem.soft.cases import SPLITS, SoftInputError, dump_cases, load_cases
from javmem.soft.claude import ClaudeModel, FakeSoftModel, SoftSettings
from javmem.soft.find import find_cases
from javmem.soft.judge import (
    PROMPT_VERSION,
    SoftStore,
    StoreBusy,
    dump_verdicts,
    plan,
    run_judge,
    verdict_records,
)
from javmem.soft.score import load_answers, load_verdicts, report_lines, score
from javmem.usage import TokenLedger

EXIT_OK = 0
EXIT_ATTENTION = 1  # 有没判完的（失败、不发、没开始）
EXIT_INPUT = 2  # 文件、参数不对，什么都没动
EXIT_BUSY = 3  # 这个 soft.db 已经有一个判定在跑
EXIT_STOPPED = 4  # 模型调不通、预算用完，停下了
ENV_TOKEN_LEDGER = "JAVMEM_TOKEN_LEDGER"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="soft_command", required=True)
    find = commands.add_parser("find", help="在账本上找软变化的候选，写成题（只读账本，不调模型）")
    where = find.add_mutually_exclusive_group(required=True)
    where.add_argument("--library", help="数据目录里的库：prod 或 test")
    where.add_argument("--ledger", help="一个账本文件（比如拷贝），只读打开")
    find.add_argument("--packet", action="append", help="只在这个包里找，可以写几次")
    find.add_argument("--limit", type=int, help="最多出几道题")
    find.add_argument("--out", required=True, help="题目文件（JSON，放仓库外：里面有原话）")

    judge = commands.add_parser("judge", help="调 Claude 判一批题（--fake 假模型，--dry-run 只数）")
    judge.add_argument("--cases", required=True, help="题目文件（javmem-soft-cases/1）")
    judge.add_argument("--out", help="结论写到这个文件（放仓库外：里面有原话）")
    judge.add_argument("--store", help="soft.db 放在哪个目录，默认数据目录的 soft")
    judge.add_argument("--split", choices=SPLITS, help="只判这一份题")
    judge.add_argument("--case", action="append", help="只判这道题，可以写几次")
    mode = judge.add_mutually_exclusive_group()
    mode.add_argument("--fake", action="store_true", help="用假模型（测试用，不连网）")
    mode.add_argument("--dry-run", action="store_true", help="不调模型，只数要调几道、多少字")
    judge.add_argument("--tag", default="javmem-soft", help="记账的标签，预算按它算")
    judge.add_argument("--budget", type=int, help="这个标签一共最多用多少 token（记账里已经有的也算）")
    judge.add_argument("--limit", type=int, help="这次最多新调几道")
    judge.add_argument("--repeat", type=int, default=0, help="第几遍：大于 0 的另算键，同一批题再判一遍看稳不稳")
    judge.add_argument("--max-minutes", type=float, help="到了这么多分钟就不再开始新的调用")
    judge.add_argument("--retry-failed", action="store_true", help="以前截断、拒答、格式错过的也重调")

    scoring = commands.add_parser("score", help="拿复核过的答案对结论")
    scoring.add_argument("--cases", required=True, help="题目文件")
    scoring.add_argument("--answers", required=True, help="答案文件（javmem-soft-answers/1）")
    scoring.add_argument("--verdicts", required=True, help="judge 写的结论文件")
    scoring.add_argument("--out", help="另写一份 JSON（放仓库外）")


def _outside(path: str | None, out: Callable[[str], None], what: str = "输出") -> bool:
    if path is not None and inside_repo(Path(path)):
        out(f"不写：{what}放在仓库里了。题目、结论里有原话，要放仓库外")
        return False
    return True


def _positive_limit(args: argparse.Namespace, out: Callable[[str], None]) -> bool:
    if args.limit is not None and args.limit < 1:
        out("--limit 要是正整数")
        return False
    return True


def _find(args: argparse.Namespace, settings: Settings, out: Callable[[str], None]) -> int:
    if not _outside(args.out, out) or not _positive_limit(args, out):
        return EXIT_INPUT
    path = Path(args.ledger) if args.ledger else settings.library(args.library).db_path
    conn = connect_readonly(path)
    try:
        result = find_cases(conn, packets=args.packet, limit=args.limit)
    finally:
        conn.close()
    dump_cases(result.cases, Path(args.out))
    out(f"找候选：{json.dumps(result.stats, ensure_ascii=False)}；题 {len(result.cases)} 道写到 {args.out}")
    return EXIT_OK


def _ledger_path(settings: Settings) -> Path:
    value = os.environ.get(ENV_TOKEN_LEDGER, "").strip()
    return Path(value) if value else settings.require_data_dir() / "logs" / "token-ledger.csv"


def _judge(args: argparse.Namespace, settings: Settings, out: Callable[[str], None]) -> int:
    if not _outside(args.out, out) or not _outside(args.store, out, "结论库（--store）") or not _positive_limit(args, out):
        return EXIT_INPUT
    cases = load_cases(Path(args.cases))
    if args.split:
        cases = [case for case in cases if case.split == args.split]
    if args.case:
        wanted = set(args.case)
        unknown = sorted(wanted - {case.id for case in cases})
        if unknown:
            out(f"题目文件里没有这几道题：{'、'.join(unknown)}")
            return EXIT_INPUT
        cases = [case for case in cases if case.id in wanted]
    if args.repeat < 0:
        out("--repeat 不能是负数")
        return EXIT_INPUT
    patterns = load_patterns(settings.redaction_file)
    store_dir = Path(args.store) if args.store else settings.require_data_dir() / "soft"
    soft = SoftSettings.from_env()
    model: FakeSoftModel | ClaudeModel = FakeSoftModel() if args.fake else ClaudeModel(soft)
    if args.dry_run:
        with SoftStore(store_dir) as store:
            counts = plan(cases, model.cache_name, store, patterns, repeat=args.repeat, retry_failed=args.retry_failed)
        out(f"{counts['cases']} 道题：存过的 {counts['cached']}，不发的 {counts['unsent']}，以前失败过的 {counts['failed_before']}，"
            f"要调的 {counts['to_call']}，一共 {counts['chars']} 字（不算提示词）；模型 {model.cache_name}，提示词 {PROMPT_VERSION}")
        return EXIT_OK
    ledger: TokenLedger | None = None
    if not args.fake:
        soft.require_key()
        assert isinstance(model, ClaudeModel)
        model.client()  # 没装 SDK、环境变量不对：现在就报配置错误，不是每道题各记一次失败
        ledger = TokenLedger(_ledger_path(settings))
        out(f"模型设置：{json.dumps(soft.describe(), ensure_ascii=False)}")
        out(f"记账 {ledger.path}，标签 {args.tag} 已经用了 {ledger.used(args.tag)} token")
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes is not None else None
    options = ExtractOptions(tag=args.tag, budget=args.budget, deadline=deadline, limit=args.limit, retry_failed=args.retry_failed)
    try:
        store = SoftStore(store_dir, exclusive=True)
    except StoreBusy as busy:
        out(str(busy))
        return EXIT_BUSY
    with store:
        result = run_judge(cases, model, store, patterns=patterns, ledger=ledger, options=options, log=out, repeat=args.repeat)
        records = verdict_records(cases, result, store)
    if args.out:
        dump_verdicts(records, Path(args.out), model=model.cache_name, repeat=args.repeat)
    summary = result.summary()
    out(f"判完：{json.dumps(summary, ensure_ascii=False)}" + (f"；结论写到 {args.out}" if args.out else ""))
    if result.stopped is not None:
        out(f"停下了：{result.stopped}")
        return EXIT_STOPPED
    unfinished = sum(1 for record in records if not record["items"])
    return EXIT_ATTENTION if unfinished else EXIT_OK


def _score(args: argparse.Namespace, out: Callable[[str], None]) -> int:
    if not _outside(args.out, out):
        return EXIT_INPUT
    result = score(load_cases(Path(args.cases)), load_answers(Path(args.answers)), load_verdicts(Path(args.verdicts)))
    for line in report_lines(result):
        out(line)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(result, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return EXIT_OK


def run(args: argparse.Namespace, settings: Settings, out: Callable[[str], None] = print) -> int:
    try:
        if args.soft_command == "find":
            return _find(args, settings, out)
        if args.soft_command == "judge":
            return _judge(args, settings, out)
        return _score(args, out)
    except SoftInputError as error:
        out(f"文件不对：{error}")
        return EXIT_INPUT
