"""命令行入口。管理操作都走这里，不直接改库。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from javmem import __version__
from javmem.config import TEST, ConfigError, Settings
from javmem.convert12 import ConvertReport, convert_directory, load_convert_config
from javmem.intake import (
    ACCEPTED,
    ERROR,
    FAILED,
    RECOVERED,
    REPLACED,
    SKIPPED,
    TMP_DIR,
    UNKNOWN_DIR,
    IntakeBusy,
    IntakeReport,
    run_intake,
)
from javmem.ledger import (
    LEDGER_TABLES,
    LedgerError,
    connect_readonly,
    open_ledger,
    schema_version,
    table_names,
    transaction,
)
from javmem.mailimport import KINDS as MAIL_KINDS
from javmem.mailimport import ImportBusy, ImportReport, import_mail, load_mail_config
from javmem.packet.render import MAX_RAW_CHARS
from javmem.embedding import EmbeddingSettings, FakeEmbeddings, OpenAIEmbeddings
from javmem.export import facts as export_facts
from javmem.export import packets as export_packets
from javmem.graph import hook as graph_hook
from javmem.graph.hook import after_write
from javmem.graph.sync import SyncReport
from javmem.llm import FATAL_KINDS, FakeModel, LLMSettings, ModelName, OpenAIChat
from javmem.patrol import AckError, WriteTimer, acknowledge, patrol_report
from javmem.decisions import export_decisions, import_decisions
from javmem.entities import SOURCE_TEXT as ENTITY_SOURCE_TEXT
from javmem.entities import (
    MERGE,
    SOURCES,
    Proposal,
    card_lines,
    describe,
    find_proposals,
    new_entity_id,
    proposal_by_id,
    proposal_line,
    quotes_of,
    sendable_of,
)
from javmem.pipeline.merge import load_owner_names
from javmem.samecheck import NOT_SENT as JUDGE_NOT_SENT
from javmem.samecheck import OFF as JUDGE_OFF
from javmem.samecheck import JudgeRequest, Judgment, judge
from javmem.samecheck import build_request as build_judge_request
from javmem.merging import MergeError, apart_record, detach_record, merge_record, undo_record
from javmem.merging import history as merge_history
from javmem.merging import run as run_merges
from javmem.zh.lexicon import ENTITY_TYPES as ENTITY_TYPES_L3
from javmem.zh.lexicon import load_lexicon, merges_path, read_records
from javmem.review import (
    KIND_TEXT,
    OWNER_WORDS_TEXT,
    ReviewError,
    ReviewItem,
    counts,
    decide,
    freeze,
    open_items,
    owner_words,
    remind,
)
from javmem.pipeline.batch import Batch, load_queued, queued_packets
from javmem.pipeline.extract import (
    PRE_A_PROMPTS,
    PRE_R_PROMPTS,
    ExtractOptions,
    forget_private_revisions,
    prepare,
    prompt_from_env,
    run_extraction,
    summarize,
)
from javmem.pipeline import precheck, prer, reljudge
from javmem.pipeline.prea import PRE_A_VERSION
from javmem.pipeline.preb import LocalModel, PreB, PreBSettings
from javmem.pipeline.store import STORE_FILE, ResultStore, StoreBusy
from javmem.pipeline.normalize import L1_VERSION
from javmem.pipeline.recheck import (
    VERDICTS,
    apply_rechecks,
    plan_recheck,
    run_recheck,
    summarize_recheck,
)
from javmem.pipeline.render import render_window
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.verify import ERROR, NO_RECHECK, NO_RESULT, NOT_SENT, summarize_checks, verify_windows
from javmem.pipeline.write import (
    LedgerWriter,
    WriteBusy,
    WriteLock,
    ledger_summary,
    record_quote_holders,
    record_quote_roles,
    run_write,
    writer_options,
)
from javmem.pipeline.window import L2_VERSION, Window, build_windows
from javmem.raw import raw_search
from javmem.redaction import RedactionPattern, load_patterns, redact
from javmem.sources import load_registry
from javmem.timeutil import iso
from javmem.usage import TokenLedger
from javmem.withdraw import WithdrawError, export_records, purge, requeue_packets, unwithdraw, withdraw

EXIT_OK = 0
EXIT_ATTENTION = 1  # 有文件出错、巡检有要看的
EXIT_CONFIG = 2  # 配置或账本有问题，什么都没动
EXIT_BUSY = 3  # 已经有一个收件在跑
EXIT_STOPPED = 4  # 模型调不通（密钥、额度、请求本身、代理），停下了
EXIT_RUNTIME = 5  # 跑到一半出错：token 账、结果库、数据目录写不进去，已经调了的都记了账

ENV_TOKEN_LEDGER = "JAVMEM_TOKEN_LEDGER"

_RESULT_TEXT = {
    ACCEPTED: "收下",
    REPLACED: "替换",
    SKIPPED: "跳过",
    FAILED: "拒收",
    RECOVERED: "接着做完",
    ERROR: "出错",
}


def _print(text: str = "") -> None:
    print(text, flush=True)


def _print_error(error: Exception) -> None:
    print(f"错误：{error}", file=sys.stderr, flush=True)


def _cmd_version(args: argparse.Namespace, settings: Settings) -> int:
    _print(f"javmem {__version__}")
    return EXIT_OK


def _cmd_versions(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.versions import run as run_versions

    return run_versions(args, out=_print)


def _cmd_regress(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.regress.run import run as run_regress

    return run_regress(args, out=_print)


def _cmd_backup(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.backup import run_backup

    return run_backup(args, settings, out=_print)


def _cmd_restore_check(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.backup import run_restore_check

    return run_restore_check(args, settings, out=_print)


def _cmd_drill(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.drill.cli import run as run_drill

    return run_drill(args, out=_print)


def _cmd_demo(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.demo import run as run_demo

    return run_demo(args, out=_print)


def _cmd_gold(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.calib.cli import run_gold

    return run_gold(args, settings, out=_print)


def _cmd_selfcheck(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.calib.selfcheck import run_selfcheck

    return run_selfcheck(args, settings, out=_print)


def _cmd_quiz(args: argparse.Namespace, settings: Settings) -> int:
    from javmem.calib.quiz import run_quiz

    return run_quiz(args, settings, out=_print)


def _make_source_dirs(settings: Settings) -> int:
    """按登记表建来源目录（packet-spec 第 11 节）。test 的来源只有测试目录。系统权限是部署的事。"""
    registry = load_registry(settings.sources_file)
    (settings.failed_dir / UNKNOWN_DIR).mkdir(parents=True, exist_ok=True)
    for source in registry.sources.values():
        (settings.failed_dir / source.id).mkdir(parents=True, exist_ok=True)
        for library in settings.libraries():
            if library.name == TEST or source.env != TEST:
                (library.inbox / source.id / TMP_DIR).mkdir(parents=True, exist_ok=True)
    return len(registry.sources)


def _cmd_init(args: argparse.Namespace, settings: Settings) -> int:
    """建数据目录和两个账本，配了登记表再建来源目录。重复跑没事，旧账本会升到最新。"""
    data = settings.require_data_dir()
    settings.failed_dir.mkdir(parents=True, exist_ok=True)
    for library in settings.libraries():
        library.inbox.mkdir(parents=True, exist_ok=True)
        library.archive.mkdir(parents=True, exist_ok=True)
        conn = open_ledger(library)
        try:
            version = schema_version(conn)
            tables = table_names(conn)
        finally:
            conn.close()
        relative = library.db_path.relative_to(data).as_posix()
        ledger_tables = len(set(tables) & set(LEDGER_TABLES))
        _print(
            f"{library.name}: {relative}，账本版本 {version}，"
            f"账本表 {ledger_tables} 张，辅助表 {len(tables) - ledger_tables} 张"
        )
    if settings.sources_file is not None:
        _print(f"来源目录：按登记表建好了 {_make_source_dirs(settings)} 个来源")
    return EXIT_OK


def _print_intake(report: IntakeReport) -> None:
    for outcome in report.outcomes:
        note = f"（{outcome.code}）" if outcome.code else ""
        _print(f"{_RESULT_TEXT[outcome.result]} [{outcome.library}] {outcome.file}{note}")
    _print("，".join(f"{text} {report.count(result)}" for result, text in _RESULT_TEXT.items()))


def _cmd_intake(args: argparse.Namespace, settings: Settings) -> int:
    """跑一遍收件。--watch 时一直跑，每隔几秒一遍，Ctrl+C 停。"""
    if args.watch is None:
        try:
            report = run_intake(settings)
        except IntakeBusy as busy:
            _print(str(busy))
            return EXIT_BUSY
        _print_intake(report)
        return EXIT_ATTENTION if report.count(ERROR) else EXIT_OK

    if args.watch <= 0:
        raise ConfigError("--watch 要大于 0")
    try:
        while True:
            try:
                report = run_intake(settings)
            except IntakeBusy:
                report = IntakeReport()
            except (ConfigError, LedgerError, sqlite3.Error, OSError) as error:
                # 一直跑的时候，配置写错或者账本一时锁住都不算完，改好了接着收
                _print_error(error)
                report = IntakeReport()
            if report.outcomes:
                _print_intake(report)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return EXIT_OK


def _cmd_patrol(args: argparse.Namespace, settings: Settings) -> int:
    """巡检。有要看的事退出码是 1，方便部署方接提醒。--ack 把看过了的标上，以后不再报。"""
    if args.ack or args.ack_kind:
        try:
            marked = acknowledge(settings, args.ack, args.ack_kind)
        except AckError as error:
            _print_error(error)
            return EXIT_ATTENTION
        _print(f"标了看过了 {len(marked)} 条：{'、'.join(marked) or '没有'}")
        return EXIT_OK
    report = patrol_report(settings)
    items = report.items
    if args.json:  # 这一回没看的几种（checks_skipped）跟在后面
        _print(json.dumps([item.to_dict() for item in report.everything()], ensure_ascii=False, indent=2))
    else:
        for item in items:
            prefix = f"[{item.library}] " if item.library else ""
            repeated = f"（来了 {item.count} 次）" if item.count and not item.kind.startswith("failed_") else ""
            ref = f"  〔{item.ref}〕" if item.ref else ""
            _print(f"{prefix}{item.kind}：{item.message}{repeated}{ref}")
        for item in report.skipped:  # 不算要看的
            _print(f"{item.kind}：{item.message}")
        _print(f"巡检：{len(items)} 件要看" if items else "巡检：没有要看的")
        if any(item.ref for item in items):
            _print("看过了不用再报的：javmem patrol --ack 〔〕里的编号，或者 --ack-kind 种类")
    return EXIT_ATTENTION if items else EXIT_OK


def _cmd_freeze(args: argparse.Namespace, settings: Settings) -> int:
    """冻结（D12：主人手动标）。冻结的事实、关系（J4.4）L5 不自动替代，和它冲突的进待确认。--undo 解除冻结。"""
    if not args.fact and not args.relation:
        raise ConfigError("要写 --fact 事实编号或者 --relation 关系编号")
    try:
        changed = freeze(settings, args.library, [*args.fact, *args.relation], frozen=not args.undo)
    except (IntakeBusy, WriteBusy) as busy:
        _print_error(busy)
        return EXIT_BUSY
    except ReviewError as error:
        _print_error(error)
        return EXIT_ATTENTION
    what = "解除冻结" if args.undo else "冻结"
    _print(f"{what} {len(changed)} 条：{'、'.join(changed) or '没有（本来就是这样）'}")
    _say_graph(args, _graph_after_admin(settings, args.library))  # 主图（J5.6）
    return EXIT_OK


def _print_review_item(item: ReviewItem) -> None:
    detail = item.detail
    owner = owner_words(item)  # 和主人原话的冲突（D204）：种类照旧，换个写法
    label = OWNER_WORDS_TEXT if owner else KIND_TEXT.get(item.kind, item.kind)
    _print(f"#{item.review_id}（{item.code}）{label}，{item.age_days} 天前（{item.created_at}），"
           f"可以定成：{'、'.join(item.choices)}")
    if item.answered is not None:  # D83：在对话里答了、没认出来
        _print(f"    你在对话里答过（{item.answered.get('answer')}），没认出来是哪种，照答的意思用 --decide 定")

    def fact_line(label: str, fact: object) -> None:
        if not isinstance(fact, dict):
            _print(f"    {label}：（这条事实已经不在了）")
            return
        flags = "冻结，" if fact.get("frozen") else ""
        flags += "藏着，" if fact.get("hidden") else ""
        _print(f"    {label}：{fact['id']}（{flags}{fact['status']}，{fact.get('as_of') or '时间不知道'}）{fact['content']}")

    if item.kind == "frozen_conflict":
        fact_line("主人原话" if owner else "冻结的", detail.get("frozen"))
        fact_line("外部说的" if owner else "新说法", detail.get("new"))
    elif item.kind == "manual_review" and "relation" in detail:  # 判断 C 拿不准的关系（J4.4）
        fact_line("拿不准的", detail.get("relation"))
        _print(f"    判断 C：{detail.get('reason')}；--keep new 留下，--drop 不要，--keep hidden 接着搁着")
    elif item.kind == "manual_review" and "fact" in detail:  # 进图前检查转人工的事实（J5.3）
        fact_line("这条事实", detail.get("fact"))
        claim = detail.get("claim")
        if isinstance(claim, dict):
            extra = "".join(f" 〔{label}〕{claim[name]}" for label, name in (("属性", "attribute"), ("值", "value")) if claim.get(name))
            _print(f"    转人工的那一处：{claim.get('content')}{extra}")
        _print(f"    为什么：{precheck.HOLD_TEXT.get(str(detail.get('reason') or '').removeprefix('C:'), detail.get('reason'))}"
               f"（证据那一句 {detail.get('sentence')}）；--keep new 留下，--drop 不要，--keep hidden 接着搁着")
    elif item.kind == "correction" and "relation" in detail:  # 财务投资的关系被纠正，先挡着（J4.6）
        fact_line("挡着的", detail.get("relation"))
        _print(f"    你纠正的那一句 {detail.get('sentence')}；--keep new 照纠正改，--keep hidden 不改（旧的照旧）")
    elif item.kind == "correction_unmatched" and "relation" in detail:  # 抽取没抽出来的纠正（J4.6）
        fact_line("是不是说这一条", detail.get("relation"))
        _print(f"    你纠正的那一句 {detail.get('sentence')}；--keep new 是，这一条结束；--keep hidden 不是，不改")
    elif item.kind == "correction_other" and "relation" in detail:  # 只说了新的那一条（J4.6，复核 K12）
        fact_line("这一条还算不算", detail.get("relation"))
        _print(f"    你只说了新的那一条（{detail.get('sentence')}）；--keep new 这一条不算了、结束；--keep hidden 还算，不改")
    elif item.kind == "soft_change":  # J4.4 第 4 段
        fact_line("挡住的", detail.get(str(detail.get("item"))))
        if detail.get("old"):
            fact_line("旧的", detail.get("old"))
        hint = f"；模型的建议 {detail['suggestion']}（{'、'.join(detail.get('reasons') or [])}）" if detail.get("suggestion") else ""  # type: ignore[arg-type]
        _print(f"    候选句 {detail.get('sentence')}{hint}；答 --verdict A 到 E（A 仍然有效、B 暂停、C 结束、D 改成新的、E 只是犹豫），"
               "B、C、D 可以加 --at 哪天")
    elif item.kind == "ambiguous_entity":
        candidates = "、".join(f"{c['id']}（{c['name']}）" for c in detail.get("candidates") or [])  # type: ignore[union-attr]
        _print(f"    {detail.get('type')}「{detail.get('name')}」{detail.get('entity_id')}，像这几个：{candidates}；待确认的事实 {detail.get('pending_facts')} 条")
    elif item.kind == "evidence_withdrawn":
        fact_line("冻结的", detail.get("fact") or detail.get("relation"))
        _print(f"    证据所在的包撤回了：{'、'.join(detail.get('packets') or [])}")  # type: ignore[arg-type]
    else:
        _print(f"    {json.dumps(detail, ensure_ascii=False)}")


def _cmd_audit(args: argparse.Namespace, settings: Settings) -> int:
    """javmem audit（J5.3，01 p4 的留痕表）：只读地查一条从抽出来到进图每一级谁判的、判成什么、P 多少、依据哪几句（--item），
    一个包的（--packet），整本账的数（--summary，什么都不写也是它）。--text 才打依据那几句的原文（在本机看）。不改账本。"""
    from javmem import audit
    from javmem.ledger import latest_version

    if sum(bool(one) for one in (args.item, args.packet, args.summary)) > 1:
        raise ConfigError("--item、--packet、--summary 只能写一个")
    conn = connect_readonly(settings.library(args.library).db_path)
    try:
        if schema_version(conn) != latest_version():
            raise LedgerError("账本不是最新的版本：先跑一次 javmem init（或者收件）把账本升上去")
        conn.execute("BEGIN")  # 一次读完：读到一半写账改了也前后对得上
        if args.item:
            data = audit.item_trail(conn, args.item, with_text=args.text)
        elif args.packet:
            data = audit.packet_trail(conn, args.packet)
        else:
            data = audit.summary(conn)
    except audit.AuditError as error:
        _print_error(error)
        return EXIT_ATTENTION
    finally:
        conn.close()
    if args.json:
        _print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.item:
        for line in audit.lines(data):
            _print(line)
    elif args.packet:
        _print(f"{args.packet}：留痕 {len(data['audit'])} 行（{_count_text(data['audit_counts'])}）")
        for row in data["quarantine"]:
            _print(f"  隔离表 {row['item_kind']} {row['rule_id']}（{row['reason']}）{row['count']} 行")
        for row in data["audit"]:
            _print(f"  {row['item_id']}：{audit.describe(row)}")
    else:
        for name, title in (("audit", "留痕（种类、判的是什么、哪一级、判成什么）"), ("quarantine", "隔离表（种类、规则）"),
                            ("facts", "事实（状态、待确认的原因、谁认的）"), ("relations", "关系（状态、待确认的原因、谁认的）")):
            _print(f"{title}：")
            for row in data[name]:
                _print("  " + " / ".join(str(one) if one != "" else "-" for one in row[:-1]) + f"：{row[-1]}")
    return EXIT_OK


def _cmd_review(args: argparse.Namespace, settings: Settings) -> int:
    """待确认队列（D6）：看、处理一条、每天一条数量提醒（30 天没人处理就两边都留）。"""
    if args.remind and (args.export is not None or args.import_file is not None or args.decide is not None):
        raise ConfigError("--remind 单独用，不和 --export、--import、--decide 一起写")
    if args.remind:
        libraries = [args.library] if args.library else [library.name for library in settings.libraries()]
        lines = []
        for name in libraries:
            if not settings.library(name).db_path.exists():
                continue
            reminder = remind(settings, name, force=args.force)
            if reminder.shown and (reminder.open or reminder.expired or args.force):
                lines.append(reminder.line())
            if reminder.expired:  # 照 30 天的规矩关掉了几条：主图跟着同步，不打那一行（这是每天给主人看的提醒，J5.6）
                _graph_after_admin(settings, name)
        for line in lines:
            _print(line)
        return EXIT_OK
    if args.library is None:
        raise ConfigError("要写 --library")
    if args.export is not None or args.import_file is not None:
        return _review_carry(args, settings)
    if args.decide is not None:
        choice = "drop" if args.drop else args.verdict or args.keep
        if choice is None:
            raise ConfigError("--decide 要配 --keep frozen|new|both|hidden，或者 --drop；软变化的配 --verdict A 到 E")
        try:
            number = decide(settings, args.library, args.decide, choice, at=args.at, condition=args.condition)
        except (IntakeBusy, WriteBusy) as busy:
            _print_error(busy)
            return EXIT_BUSY
        except ReviewError as error:
            _print_error(error)
            return EXIT_ATTENTION
        _print(f"#{number} 定了：{choice}")
        _say_graph(args, _graph_after_admin(settings, args.library))  # 主图（J5.6）
        return EXIT_OK
    conn = open_ledger(settings.library(args.library))
    try:
        items = open_items(conn, settings)
    finally:
        conn.close()
    if args.json:
        _print(json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2))
        return EXIT_OK
    for item in items:
        _print_review_item(item)
    by_kind = "，".join(f"{KIND_TEXT.get(kind, kind)} {count}" for kind, count in counts(items).items())
    _print(f"待确认 {len(items)} 条" + (f"：{by_kind}" if items else ""))
    if items:
        _print("处理一条：javmem review --library 库 --decide 编号 --keep frozen|new|both|hidden，或者 --drop；"
               "软变化的答 --verdict A 到 E（可以加 --at 哪天、--condition 等什么）")
    return EXIT_OK


def _review_carry(args: argparse.Namespace, settings: Settings) -> int:
    """冻结标记和主人的决定搬到整本重建的新账本（D73 第 7 条）：旧账本 --export，新账本写完账以后 --import。"""
    if args.export is not None and args.import_file is not None or args.decide is not None or args.remind:
        raise ConfigError("--export、--import 都单独用")
    if args.export is not None:
        data = export_decisions(settings, args.library)
        Path(args.export).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        reviews = data["reviews"]
        assert isinstance(reviews, list)
        frozen = sum(1 for fact in data["facts"].values() if fact["frozen"])  # type: ignore[union-attr]
        decided = sum(1 for review in reviews if review["decided_at"] is not None)
        _print(f"冻结标记和决定：冻结的事实 {frozen} 条，待确认 {len(reviews)} 条（定过的 {decided} 条），写到 {args.export}（有原文，放在仓库外）")
        return EXIT_OK
    try:
        data = json.loads(Path(args.import_file).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"读不了导出文件：{type(error).__name__}") from None
    if not isinstance(data, dict):
        raise ConfigError("导出文件的格式不对")
    try:
        report = import_decisions(settings, args.library, data)
    except (IntakeBusy, WriteBusy) as busy:
        _print_error(busy)
        return EXIT_BUSY
    except ReviewError as error:
        _print_error(error)
        return EXIT_ATTENTION
    if args.json:
        _print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print(
            f"导回：冻结标记 {report.frozen} 条（编号一样认出来的 {report.same_id} 条，按证据认出来的 {len(report.by_evidence)} 条，"
            f"认不出来照旧补回来的 {len(report.recreated)} 条），照搬定过的决定 {report.decisions} 条，"
            f"没处理的照旧 {report.open_kept} 条，重算了 {report.groups} 个组"
        )
        if report.recreated:
            _print(f"补回来的冻结事实、关系（新账本里认不出来，没有说法，证据对不上的 {report.evidence_dropped} 条没要）：{'、'.join(report.recreated)}")
        if report.hidden:
            _print(f"补回来时证据在新账本里都撤回了、藏起来进了待确认的：{'、'.join(report.hidden)}")
        if report.resupported:
            _print(f"旧账本里藏着、新账本里又有说法撑着、放出来的：{'、'.join(report.resupported)}")
        if report.unmatched:
            _print(f"决定里提到、新账本里认不出来的事实（不冻结，没补）：{'、'.join(report.unmatched)}")
        for skipped in report.skipped:
            _print(f"没搬：#{skipped['review_id']} {skipped['kind']}，{skipped['why']}")
        for gone in report.open_gone:
            _print(f"没处理的、新账本里没再出现：#{gone['review_id']} {gone['kind']}，{gone['why']}")
    _say_graph(args, _graph_after_admin(settings, args.library))  # 主图（J5.6）
    attention = report.recreated or report.unmatched or report.skipped or report.open_gone or report.hidden
    return EXIT_ATTENTION if attention else EXIT_OK


def _entities_ledger(settings: Settings, library: str) -> sqlite3.Connection:
    """合并工具读的账本：库要已经有账本，不然 open_ledger 会建一本空的。"""
    if not settings.library(library).db_path.is_file():
        raise ConfigError(f"{library} 库还没有账本")
    return open_ledger(settings.library(library))


def _cmd_entities(args: argparse.Namespace, settings: Settings) -> int:
    """认人认事（J3.6）：合并的提议、确认卡片、合并、不是同一个、撤销、拆开、历史。管理命令，只给主人和 CC；
    名字、原话是给主人在会话里看的，不进日志。"""
    actions = [name for name in ("show", "merge", "apart", "undo", "detach") if getattr(args, name)]
    actions += [name for name in ("sync", "history") if getattr(args, name)]
    if len(actions) > 1:
        raise ConfigError(f"--{'、--'.join(actions)} 一次只能写一个")
    if args.show is not None:
        return _entities_show(args, settings)
    if args.history:
        return _entities_history(args, settings)
    if actions:
        return _entities_change(args, settings)
    lexicon = load_lexicon(settings.lexicon_file)
    conn = _entities_ledger(settings, args.library)
    try:
        proposals = find_proposals(conn, lexicon, args.source or SOURCES)
        infos = {proposal.proposal_id: describe(conn, proposal) for proposal in proposals}
    finally:
        conn.close()
    if args.json:
        rows = [
            {**proposal.to_dict(), "infos": [asdict(info) for info in infos[proposal.proposal_id].values()]} for proposal in proposals
        ]
        _print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    for proposal in proposals:
        _print(proposal_line(proposal, infos[proposal.proposal_id]))
    by_source = "，".join(
        f"{ENTITY_SOURCE_TEXT[source]} {count}"
        for source in SOURCES
        if (count := sum(1 for proposal in proposals if proposal.source == source))
    )
    _print(f"提议 {len(proposals)} 条" + (f"：{by_source}" if proposals else ""))
    if proposals:
        _print("看一条：javmem entities --library 库 --show 编号")
    return EXIT_OK


def _judge_model(args: argparse.Namespace, settings: Settings) -> tuple[object | None, TokenLedger | None]:
    """卡片上模型的看法用哪个模型、记在哪本 token 账里。--no-model 不调；--fake 用假模型、不记账；不然用抽取在用的那个
    （JAVMEM_LLM_*，没配好报配置错误，什么都不动）。"""
    if args.no_model:
        return None, None
    if args.fake:
        return FakeModel(), None
    return OpenAIChat(LLMSettings.from_env()), TokenLedger(_ledger_path(settings))


def _after_lines(args: argparse.Namespace, settings: Settings, proposal: Proposal) -> list[str]:
    """卡片上“合并以后”那几行：照默认勾上的在一个事务里先合一遍，数完撤回来，账本和合并记录都不动。"""
    if proposal.kind != MERGE:
        return []
    try:
        path = settings.lexicon_file
        records = read_records(merges_path(path)) if path is not None else []
        conn = _entities_ledger(settings, args.library)
        try:
            record = merge_record(conn, proposal, records, note="预演", at=iso(settings.now()))
        finally:
            conn.close()
        report = run_merges(settings, args.library, record, store_dir=_opt_path(args.store), vectors_dir=_opt_path(args.vectors), dry_run=True)
    except MergeError as error:
        return [f"算不了：{error}"]
    except (IntakeBusy, WriteBusy):
        return ["算不了：收件或者写账正在跑，等它跑完再看"]
    return report.lines()


def _opt_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _entities_change(args: argparse.Namespace, settings: Settings) -> int:
    """合并、记成不是同一个、撤销、拆开、把还没生效的补上：写一条合并记录，在这本账上当场生效（javmem.merging）。"""
    record: dict[str, object] | None = None
    if not args.sync:
        if not (args.note or "").strip():
            raise ConfigError("要写 --note（决策号，比如“D97 第 2 条”）")
        path = settings.lexicon_file
        if path is None:
            raise ConfigError("没配别名表（JAVMEM_LEXICON_FILE）：合并记录写在别名表旁边")
        records = read_records(merges_path(path))
        now = iso(settings.now())
        conn = _entities_ledger(settings, args.library)
        try:
            if args.undo:
                record = undo_record(records, args.undo, note=args.note, at=now)
            elif args.detach:
                if not args.alias:
                    raise ConfigError("--detach 要配 --alias 叫法")
                record = detach_record(conn, args.detach, args.alias, records, note=args.note, at=now)
            else:
                proposal = proposal_by_id(conn, load_lexicon(path), args.merge or args.apart)
                if proposal is None:
                    raise MergeError(f"没有这条提议：{args.merge or args.apart}（实体变了编号也会变，先 --proposals 看一遍）")
                if args.merge:
                    record = merge_record(
                        conn, proposal, records, note=args.note, at=now, name=args.name, entity_type=args.type, only=args.only or ()
                    )
                else:
                    record = apart_record(conn, proposal, records, note=args.note, at=now)
        except MergeError as error:
            _print_error(error)
            return EXIT_ATTENTION
        finally:
            conn.close()
    try:
        report = run_merges(settings, args.library, record, store_dir=_opt_path(args.store), vectors_dir=_opt_path(args.vectors))
    except (IntakeBusy, WriteBusy) as busy:
        _print_error(busy)
        return EXIT_BUSY
    except MergeError as error:
        _print_error(error)
        return EXIT_ATTENTION
    if args.json:
        _print(json.dumps({"record": record, **report.to_dict()}, ensure_ascii=False, indent=2))
    else:
        if record is not None:
            _print(f"记下了 {record['id']}（{record['kind']}），写在别名表旁边的合并记录里")
        if not report.records:
            _print("这本账上没有要生效的合并记录")
        else:
            _print(f"在 {args.library} 库上生效：{'、'.join(report.records)}")
            for line in report.lines():
                _print(f"  {line}")
    _say_graph(args, _graph_after_admin(settings, args.library, vectors=_opt_path(args.vectors)))  # 主图（J5.6）
    return EXIT_ATTENTION if report.unmatched or report.others_changed else EXIT_OK


def _entities_history(args: argparse.Namespace, settings: Settings) -> int:
    try:
        rows = merge_history(settings, args.library)
    except MergeError as error:
        _print_error(error)
        return EXIT_ATTENTION
    if args.json:
        _print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    kinds = {"merge": "合并", "apart": "不是同一个", "undo": "撤销", "copied": "誊进人写的那份"}
    for row in rows:
        detail = ""
        if row["kind"] == "merge":
            detail = f"「{row['standard']}」（{row['type']}）← {'、'.join(row['names']) or '只改编号'}"  # type: ignore[arg-type]
        elif row["kind"] == "apart":
            detail = "、".join(row["names"])  # type: ignore[arg-type]
        elif row["kind"] == "undo":
            detail = str(row["undoes"])
        _print(f"{row['id']} {kinds[str(row['kind'])]} {str(row['at'])[:16]} {detail}（{row['note']}）{row['state']}")
    _print(f"合并记录 {len(rows)} 条")
    return EXIT_OK


def _entities_show(args: argparse.Namespace, settings: Settings) -> int:
    """一条提议的确认卡片：有原话，在会话里看，不进日志。模型的看法只当参考（D100 第 8 条），存过的不重调。"""
    lexicon = load_lexicon(settings.lexicon_file)
    owner_keys = load_owner_names(settings.owner_file)
    patterns = load_patterns(settings.redaction_file)
    model, ledger = _judge_model(args, settings)
    conn = _entities_ledger(settings, args.library)
    try:
        proposal = proposal_by_id(conn, lexicon, args.show)
        if proposal is None:
            _print_error(ConfigError(f"没有这条提议：{args.show}（实体变了编号也会变，先 --proposals 看一遍）"))
            return EXIT_ATTENTION
        infos = describe(conn, proposal)
        # 卡片在会话里看：原话也按打码模式表打一遍，漏打的密码、密钥不进会话
        quotes = {
            entity_id: [replace(quote, text=redact(quote.text, patterns)[0]) for quote in quotes_of(conn, entity_id)]
            for entity_id in proposal.entities
        }
        if model is None:
            judgment = Judgment(JUDGE_OFF)
        else:
            request = build_judge_request(
                proposal.proposal_id, {entity_id: sendable_of(conn, infos[entity_id]) for entity_id in proposal.entities}, patterns, owner_keys
            )
            judgment = (
                judge(conn, request, model, ledger=ledger, budget=args.budget, tag=args.tag)  # type: ignore[arg-type]
                if isinstance(request, JudgeRequest)
                else Judgment(JUDGE_NOT_SENT, detail=request)
            )
    finally:
        conn.close()
    lines = card_lines(proposal, infos, quotes, judgment.line(), _after_lines(args, settings, proposal))
    if args.json:
        payload = {
            **proposal.to_dict(),
            "new_id": new_entity_id(proposal),
            "infos": [asdict(info) for info in infos.values()],
            "quotes": {entity_id: [asdict(quote) for quote in items] for entity_id, items in quotes.items()},
            "judgment": {"status": judgment.status, "same": judgment.same, "reason": judgment.reason, "detail": judgment.detail},
            "card": lines,
        }
        _print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            _print(line)
    return EXIT_OK


def _cmd_raw_search(args: argparse.Namespace, settings: Settings) -> int:
    """命令行查原文。身份只认环境变量 JAVMEM_CALLER，没有选身份、选库的参数。"""
    arguments: dict[str, object] = {"query": args.query}
    for name in ("limit", "time_from", "time_to", "source", "packet_id", "thread_id", "speaker", "context", "cursor"):
        value = getattr(args, name)
        if value is not None:
            arguments[name] = value
    if args.partition is not None:
        arguments["partitions"] = args.partition
    result = raw_search(arguments, settings=settings)
    _print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["ok"] else EXIT_ATTENTION


def _cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    """查询服务（J5.5）：stdio 上的 MCP 服务。身份只认环境变量 JAVMEM_CALLER，标准输入关了就退出。"""
    from javmem.query.mcp import serve_stdio  # 用到才导入

    return serve_stdio(settings)


def _cmd_query(args: argparse.Namespace, settings: Settings) -> int:
    """命令行问一次五个工具里的一个（J5.5），输出和工具一样的 JSON。身份只认 JAVMEM_CALLER。"""
    from javmem.query.tools import call_tool

    text = args.arguments if args.arguments is not None else sys.stdin.read()
    try:
        arguments = json.loads(text) if text.strip() else {}
    except ValueError:
        raise ConfigError("参数要写成 JSON，比如 {\"query\": \"报价单\"}") from None
    if not isinstance(arguments, dict):
        raise ConfigError("参数只认大括号括起来的 JSON")
    result = call_tool(args.tool, arguments, settings=settings)
    _print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["ok"] else EXIT_ATTENTION


def windows_report(
    library: str, batch: Batch, windows: Sequence[Window], patterns: Sequence[RedactionPattern]
) -> dict[str, object]:
    """每包几个窗口、每个窗口多大。只有数，不抄原文。"""
    windows_of: dict[str, list[str]] = {packet.packet_id: [] for packet in batch.packets}
    chars_of: dict[str, int] = {packet.packet_id: 0 for packet in batch.packets}
    rows = []
    context_chars = rendered_chars = 0
    for window in windows:
        stats = window.stats()
        stats["rendered_chars"] = len(render_window(window, patterns).text)
        context_chars += sum(len(line.text) for line in window.lines if not line.target)
        rendered_chars += int(stats["rendered_chars"])
        rows.append(stats)
        for line in window.targets:
            chars_of[line.packet_id] += len(line.text)
            if window.window_id not in windows_of[line.packet_id]:
                windows_of[line.packet_id].append(window.window_id)
    packets = [
        {
            "packet_id": packet.packet_id,
            "channel": packet.header.channel,
            "scope": packet.scope.as_dict(),
            "kinds": dict(sorted(packet.kinds.items())),
            "attachment_notes": packet.attachment_notes,
            "windows": windows_of[packet.packet_id],
            "target_chars": chars_of[packet.packet_id],
        }
        for packet in batch.packets
    ]
    totals = {
        "packets": len(batch.packets),
        "windows": len(rows),
        "target_chars": sum(chars_of.values()),
        "context_chars": context_chars,
        "rendered_chars": rendered_chars,
    }
    return {
        "library": library,
        "l1": L1_VERSION,
        "l2": L2_VERSION,
        "packets": packets,
        "windows": rows,
        "problems": [{"packet_id": packet_id, "reason": reason} for packet_id, reason in batch.problems],
        "totals": totals,
    }


def _cmd_windows(args: argparse.Namespace, settings: Settings) -> int:
    """L1 规整、L2 切窗，看每包几个窗口、每个窗口多大。不调模型，不写账本。"""
    patterns = load_patterns(settings.redaction_file)
    batch = load_queued(settings, args.library, packet_ids=args.packet)
    windows = build_windows(batch.packets, context_pool=batch.context)
    if args.show is not None:
        window = next((window for window in windows if window.window_id == args.show), None)
        if window is None:
            _print_error(ConfigError(f"没有这个窗口：{args.show}"))
            return EXIT_ATTENTION
        _print(render_window(window, patterns).text)
        return EXIT_OK
    report = windows_report(args.library, batch, windows, patterns)
    if args.report is not None:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        _print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for packet in batch.packets:
            count = sum(1 for window in windows if packet.packet_id in window.target_packets)
            _print(f"{packet.packet_id}：{count} 个窗口")
        for packet_id, reason in batch.problems:
            _print(f"没用上 {packet_id}：{reason}")
        totals = report["totals"]
        assert isinstance(totals, dict)
        _print(
            f"一共 {totals['packets']} 个包，{totals['windows']} 个窗口，"
            f"正文 {totals['target_chars']} 字，上文 {totals['context_chars']} 字"
        )
    return EXIT_ATTENTION if batch.problems else EXIT_OK


def _ledger_path(settings: Settings) -> Path:
    value = os.environ.get(ENV_TOKEN_LEDGER, "").strip()
    return Path(value) if value else settings.require_data_dir() / "logs" / "token-ledger.csv"


def _select_windows(windows: list[Window], wanted: Sequence[str] | None) -> list[Window]:
    """--window 点名的几个窗口。点了这一批里没有的，报配置错误，一个都不调。"""
    if not wanted:
        return windows
    known = {window.window_id for window in windows}
    unknown = [window_id for window_id in dict.fromkeys(wanted) if window_id not in known]
    if unknown:
        raise ConfigError(f"这一批切出来的窗口里没有：{'、'.join(unknown)}")
    chosen = set(wanted)
    return [window for window in windows if window.window_id in chosen]


def _cmd_extract(args: argparse.Namespace, settings: Settings) -> int:
    """L3 抽取。存过的不再调；--max-minutes 到了不再开始新的调用，重跑接着来。

    --recheck：L3 之后，过了规则的事实再让模型看一遍原文支不支持（L4 的模型复核），同一个标签、预算、截止时间。

    配了 JAVMEM_PRE_B_URL（本机模型）的，L3 之前先做预处理 B（认人认事），要配好 JAVMEM_EMBEDDING_*（挑候选）。
    L3 用哪个提示词照 JAVMEM_L3_PROMPT。设置不对的报配置错误，什么都不动。

    配了 JAVMEM_JUDGE_MODE 的，L3（和复核）之后再做判断 C（javmem.pipeline.reljudge）：过了 R1 到 R6 的关系问本机模型
    （JAVMEM_JUDGE_URL），local+llm 模式把没把握的交给抽取模型，同一个标签、预算、截止时间；判定存进结果库。

    提示词带“放进账本里的现有关系”的（PRE_R_PROMPTS）：一次只抽一个包，这一批里导出最早的那个（--packet 点了几个也只抽
    其中最早的）。抽完先 javmem write 写这个包，再接着抽下一个：后面的包才看得到前面的包写进账本的关系。
    """
    patterns = load_patterns(settings.redaction_file)
    store_dir = Path(args.store) if args.store else settings.require_data_dir() / "l3"
    prompt = prompt_from_env()
    relations_in = prompt in PRE_R_PROMPTS
    judge = reljudge.JudgeSettings.from_env()  # 判断 C 的设置写错了：报配置错误，什么都不动
    prechecks = _prechecks(args.recheck)  # 进图前检查（J5.3）：写错了、开了却没配判断 C、又加了 --recheck 的报配置错误
    judge_url = judge.require_url() if judge is not None and not args.fake and not args.dry_run else None
    judge_names = writer_options(settings) if judge is not None else None  # 关系要照写账时一样核（别名表、主人的名字）
    ledger: TokenLedger | None = None
    model: FakeModel | OpenAIChat | ModelName
    checker: FakeModel | OpenAIChat | ModelName | None = None
    pre_b_settings: PreBSettings | None = None
    pre_b_embedder: OpenAIEmbeddings | None = None
    if args.fake:
        model = FakeModel()
        checker = FakeModel() if args.recheck else None
    elif args.dry_run:
        model = ModelName(LLMSettings.from_env())  # 只算键，不要密钥
        checker = ModelName(LLMSettings.recheck_from_env()) if args.recheck else None
    else:
        llm = LLMSettings.from_env()
        model = OpenAIChat(llm)
        checker = OpenAIChat(LLMSettings.recheck_from_env()) if args.recheck else None
        ledger = TokenLedger(_ledger_path(settings))
        pre_b_settings = PreBSettings.from_env()
        if pre_b_settings is not None:  # 挑候选要本机的向量服务：没配好报配置错误，不悄悄退回不挑候选的做法
            pre_b_embedder = OpenAIEmbeddings(EmbeddingSettings.from_env())
    pre_b_lexicon = load_lexicon(settings.lexicon_file) if pre_b_settings is not None else None
    judge_spec = None
    if judge is not None:  # 第二档就是抽取模型；--fake 用假的本机模型；版本跟着 L3 的提示词（讲了隶属于的用 c2，D210）
        judge_spec = judge.spec(
            model.cache_name if judge.mode == reljudge.CASCADE else None,
            local=reljudge.FAKE_LOCAL_NAME if args.fake else None,
            version=reljudge.judge_version_for(prompt),
        )
    fact_spec = _fact_spec(prechecks, args, model.cache_name)  # 事实的判断：模式、本机模型、第二档照判断 C
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes is not None else None
    options = ExtractOptions(
        tag=args.tag, budget=args.budget, deadline=deadline, limit=args.limit, retry_failed=args.retry_failed
    )
    try:
        store = ResultStore(store_dir, exclusive=not args.dry_run)
    except StoreBusy as busy:
        _print_error(busy)
        return EXIT_BUSY
    with store:
        # 拿到结果库的锁以后才取包、切窗：撤回要拿这把锁，切好窗口以后才撤回的内容就不会再发给模型
        batch = load_queued(settings, args.library, packet_ids=args.packet)
        if not args.dry_run:  # 重发成 private 的包，旧版本留在结果库里的窗口、结果、判定清掉
            dropped = forget_private_revisions(  # 预处理 B 用过旧版本里学来的名字的别的包，改回排队重抽
                settings.library(args.library).db_path, store,
                requeue=lambda packets: requeue_packets(settings, args.library, packets),
            )
            if dropped:
                _print(f"清掉了 {dropped} 个窗口：包重发成了 private，旧版本的窗口文字、结果、复核判定都删了")
        pre_r: prer.PreR | None = None
        if relations_in:  # 一次只抽一个包：抽完写账，后面的包才看得到它写的关系
            batch, notes = _one_packet(settings, args.library, batch)
            pre_r = prer.PreR(settings.library(args.library).db_path, log=_print)
        windows = _select_windows(build_windows(batch.packets, context_pool=batch.context), args.window)
        for packet_id, reason in batch.problems:
            _print(f"没用上 {packet_id}：{reason}")
        if prompt in PRE_A_PROMPTS:  # 提示词带 A 的才做（不另加开关）
            _print(f"预处理 A：{PRE_A_VERSION}（提示词 {prompt}；相对的日子照规则换成具体的日子，不调模型）")
        if relations_in:
            _print(f"现有关系：{prer.PRE_R_VERSION}（提示词 {prompt}；抽之前放进账本里两头都在这个窗口里的现行关系，不调模型）")
            for line in notes:
                _print(line)
        if args.dry_run:
            # 标过的照存下的算；B 没标过的照原样；A（提示词带 A 的）、现有关系没存过的当场算、不存，和真跑一样
            prepared = prepare(windows, model, patterns, store=store, prompt=prompt, fresh_a=True, fresh_r=pre_r)
            private = sum(1 for w, _, _ in prepared if w.scope.private)
            unsent = [w for w, r, _ in prepared if not w.scope.private and (r.leftover or len(r.text) > options.max_input_chars)]
            todo = [
                (w, r)
                for w, r, key in prepared
                if not w.scope.private and not r.leftover and len(r.text) <= options.max_input_chars
                and store.result(key) is None
            ]
            if args.limit is not None:
                todo = todo[: args.limit]
            chars = sum(len(r.text) for _, r in todo)
            _print(
                f"{len(prepared)} 个窗口：存过的 {len(prepared) - len(todo) - private - len(unsent)}，"
                f"不发的 private {private}，打码没打干净或者太长不发的 {len(unsent)}，"
                f"要调的 {len(todo)}，一共 {chars} 字（不算提示词）"
            )
            if checker is not None:
                plan = plan_recheck(
                    verify_windows(windows, store, model, patterns, prompt=prompt),
                    store,
                    checker.cache_name,
                    patterns,
                    max_input_chars=options.max_input_chars,
                    retry_failed=args.retry_failed_recheck,
                    limit=args.limit,
                )
                _print(
                    f"复核：有过了规则的事实的窗口 {plan['windows']} 个，存过的 {plan['cached']}，"
                    f"以前失败过的 {plan['failed_before']}，发不出去的 {plan['unsent']}，要调的 {plan['to_call']}"
                    f"（事实 {plan['facts']} 条，一共 {plan['chars']} 字，不算提示词）；"
                    f"L3 还没结果、核不了的窗口 {plan['skipped']} 个，抽完才知道"
                )
            if judge_spec is not None and judge_names is not None:
                plan_items = verify_windows(
                    windows, store, model, patterns, prompt=prompt,
                    lexicon=judge_names["lexicon"], owner_names=judge_names["owner_names"],  # type: ignore[arg-type]
                )
                judged = reljudge.plan_judge(
                    plan_items, store, judge_spec, patterns, retry_failed=args.retry_failed_judge, limit=args.limit,
                )
                _print(
                    f"判断 C（{judge_spec.token()}）：过了规则要判的关系 {judged['relations']} 条；本机那一档存过的 "
                    f"{judged['local_cached']}，以前说过太长不再问的 {judged['local_too_long_before']}，要问的 {judged['local_to_call']}"
                    + (
                        f"；第二档存过的 {judged['llm_cached']}，以前重调也没用的 {judged['llm_failed_before']}，"
                        f"打码没打干净不发的 {judged['llm_unsent']}，要调的 {judged['llm_to_call']}（一共 {judged['llm_chars']} 字，"
                        f"不算提示词），超了 --limit 这次不调的 {judged['llm_over_limit']}，本机还没判、判了才知道的 {judged['llm_unknown']}"
                        if judge_spec.mode == reljudge.CASCADE else ""
                    )
                    + "；L3 还没结果的窗口抽完才知道"
                )
                if fact_spec is not None:  # 进图前检查（J5.3）：过了规则的事实，和判断 C 一样数
                    facts = precheck.plan_fact_judge(
                        plan_items, store, fact_spec, patterns, retry_failed=args.retry_failed_judge, limit=args.limit,
                    )
                    _print(
                        f"事实的判断（{fact_spec.token()}）：过了规则要判的事实 {facts['facts']} 条；本机那一档存过的 "
                        f"{facts['local_cached']}，以前说过太长不再问的 {facts['local_too_long_before']}，要问的 {facts['local_to_call']}"
                        + (
                            f"；第二档存过的 {facts['llm_cached']}，以前重调也没用的 {facts['llm_failed_before']}，"
                            f"打码没打干净不发的 {facts['llm_unsent']}，要调的 {facts['llm_to_call']}（一共 {facts['llm_chars']} 字，"
                            f"不算提示词），超了 --limit 这次不调的 {facts['llm_over_limit']}，本机还没判、判了才知道的 {facts['llm_unknown']}"
                            if fact_spec.mode == reljudge.CASCADE else ""
                        )
                        + "；L3 还没结果的窗口抽完才知道"
                    )
            return EXIT_OK
        if ledger is not None:
            _print(f"模型设置：{json.dumps(model.settings.describe(), ensure_ascii=False)}")  # type: ignore[union-attr]
            if checker is not None:
                _print(f"复核的模型设置：{json.dumps(checker.settings.describe(), ensure_ascii=False)}")  # type: ignore[union-attr]
            _print(f"记账 {ledger.path}，标签 {args.tag} 已经用了 {ledger.used(args.tag)} token")
            _print(f"L3 提示词：{prompt}")
        if judge_spec is not None:
            _print(f"判断 C：{json.dumps(judge_spec.describe(), ensure_ascii=False)}")
        if fact_spec is not None:
            _print(f"事实的判断（进图前检查）：{json.dumps(fact_spec.describe(), ensure_ascii=False)}")
        pre_b: PreB | None = None
        if pre_b_settings is not None and pre_b_lexicon is not None:
            pre_b = PreB(
                LocalModel(pre_b_settings.url, pre_b_settings.model, prompt_cache=pre_b_settings.prompt_cache),
                pre_b_lexicon,
                ledger=ledger,
                tag=args.tag,
                log=_print,
                embedder=pre_b_embedder,
                # 按收包的先后：导出的时间（包头的 covers_to）；只用现行版本的包里学来的名字
                order={packet.packet_id: packet.header.covers_to.timestamp() for packet in batch.packets},
                current=_current_revisions(settings.library(args.library).db_path),
            )
            _print(f"预处理 B：{json.dumps(pre_b.describe(), ensure_ascii=False)}")
        recheck_result = None
        judge_result: reljudge.JudgeRunResult | None = None
        fact_result: reljudge.JudgeRunResult | None = None
        try:
            result = run_extraction(
                windows, model, store, patterns=patterns, ledger=ledger, options=options, log=_print, prompt=prompt,
                pre_b=pre_b, pre_r=pre_r,
            )
            summary = summarize(windows, store, model, patterns, prompt=prompt)
            if pre_b is not None:
                summary["pre_b"] = {**pre_b.describe(), "stats": dict(sorted(pre_b.stats.items()))}
                _print(f"预处理 B 这一遍：{json.dumps(dict(sorted(pre_b.stats.items())), ensure_ascii=False)}")
            if pre_r is not None:
                summary["pre_r"] = {**pre_r.describe(), "stats": dict(sorted(pre_r.stats.items()))}
            if checker is not None and result.stopped is None:
                checked = verify_windows(windows, store, model, patterns, prompt=prompt)
                recheck_result = run_recheck(
                    checked,
                    checker,
                    store,
                    patterns=patterns,
                    ledger=ledger,
                    # 同一个标签、预算、截止时间、--limit；重调失败过的另有一个开关，不连着 L3 截断、拒答过的一起重发
                    options=replace(options, retry_failed=args.retry_failed_recheck),
                    log=_print,
                )
                summary["recheck"] = summarize_recheck(checked, store, checker.cache_name, patterns)
            if (
                judge is not None and judge_spec is not None and judge_names is not None and result.stopped is None
                and (recheck_result is None or recheck_result.stopped is None)
            ):
                judged_items = verify_windows(  # 关系照写账时一样核：过了 R1 到 R6 的才判
                    windows, store, model, patterns, prompt=prompt,
                    lexicon=judge_names["lexicon"], owner_names=judge_names["owner_names"],  # type: ignore[arg-type]
                )
                local_judge = reljudge.FakeLocal() if args.fake else LocalModel(str(judge_url), judge.model)
                judge_result = reljudge.run_judge(
                    judged_items,
                    judge_spec,
                    store,
                    local_model=local_judge,
                    llm_model=model if judge_spec.mode == reljudge.CASCADE else None,
                    patterns=patterns,
                    ledger=ledger,
                    # 同一个标签、预算、截止时间、--limit（第二档的调用数）；重调失败过的另有一个开关
                    options=replace(options, retry_failed=args.retry_failed_judge),
                    log=_print,
                )
                judged_now = reljudge.apply_judgments(judged_items, store, judge_spec, patterns)
                summary["judge"] = {
                    **judge_spec.describe(),
                    **reljudge.summarize_judgments(judged_now),
                    "this_run": judge_result.describe(),
                }
                if prechecks is not None and fact_spec is not None and judge_result.stopped is None:
                    # 进图前检查（J5.3）：过了规则的事实也照判断 C 的两档判，同一个标签、预算、截止时间
                    fact_result = precheck.run_fact_judge(
                        judged_items,
                        fact_spec,
                        store,
                        local_model=local_judge,
                        llm_model=model if fact_spec.mode == reljudge.CASCADE else None,
                        patterns=patterns,
                        ledger=ledger,
                        options=replace(options, retry_failed=args.retry_failed_judge),
                        log=_print,
                    )
                    facts_now = precheck.summarize(precheck.apply(judged_now, store, fact_spec, patterns, strict=prechecks.strict))
                    # 还没判完的窗口只算事实的：判断 C 还没判完的另算（和 verify 一样，审阅第 11 条）
                    facts_now["windows_waiting"] = int(facts_now["windows_waiting"]) - int(summary["judge"]["windows_waiting"])  # type: ignore[call-overload, index]
                    summary["precheck"] = {**fact_spec.describe(), **facts_now, "this_run": fact_result.describe()}
        except (sqlite3.Error, OSError) as error:  # 跑到一半库或目录出错，不是配置错
            _print_error(error)
            return EXIT_RUNTIME
    stopped = result.stopped or (recheck_result.stopped if recheck_result is not None else None)
    if stopped is None and judge_result is not None:
        stopped = judge_result.stopped
    if stopped is None and fact_result is not None:
        stopped = fact_result.stopped
    summary["problems"] = [{"packet_id": packet_id, "reason": reason} for packet_id, reason in batch.problems]
    summary["stopped"] = stopped
    if args.summary is not None:
        Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tokens = summary["tokens"]
    assert isinstance(tokens, dict)
    _print(
        f"这次：新抽 {result.count('done')}，存过的 {result.count('cached')}，失败 {result.count('failed')}，"
        f"没开始 {result.count('not_started')}，不发的 private {result.count('private')}"
    )
    _print(
        f"这批一整遍：{summary['windows']} 个窗口，有结果的 {summary['done']}，还没有的 {summary['missing']}；"
        f"调用 {summary['calls']} 次，格式错误率 {summary['format_error_rate']}，"
        f"输入 {tokens['input_tokens']}，输出 {tokens['output_tokens']} token"
    )
    recheck_missing = 0
    if checker is not None and recheck_result is None:
        _print("复核没跑：L3 那一段停下了")
    if recheck_result is not None:
        recheck = summary["recheck"]
        assert isinstance(recheck, dict)
        verdicts, recheck_tokens = recheck["verdicts"], recheck["tokens"]
        recheck_missing = int(recheck["windows_without_recheck"]) + int(recheck["windows_not_sent_unredacted"])
        _print(
            f"复核这次：新核 {recheck_result.count('done')}，存过的 {recheck_result.count('cached')}，"
            f"失败 {recheck_result.count('failed')}，没开始 {recheck_result.count('not_started')}，"
            f"没有过了规则的事实 {recheck_result.count('nothing')}，L4 核不了 {recheck_result.count('skipped')}"
        )
        _print(
            f"复核一整遍：有判定的窗口 {recheck['windows_rechecked']}，还没有的 {recheck_missing}；"
            f"要核的事实 {recheck['facts_to_check']} 条，核过的 {recheck['facts_checked']} 条，判定 {_count_text(verdicts)}；"  # type: ignore[arg-type]
            f"调用 {recheck['calls']} 次，格式错误率 {recheck['format_error_rate']}，"
            f"输入 {recheck_tokens['input_tokens']}，输出 {recheck_tokens['output_tokens']} token"  # type: ignore[index]
        )
    judge_waiting = 0
    if judge is not None and judge_result is None:
        _print("判断 C 没跑：前面那一段停下了")
    if judge_result is not None:
        judged = summary["judge"]
        assert isinstance(judged, dict)
        local_run = judge_result.local
        judge_waiting = int(judged["windows_waiting"])
        llm_text = ""
        if judge_result.llm is not None:
            llm_text = (
                f"；第二档新判 {judge_result.llm.count('done')}，失败 {judge_result.llm.count('failed')}，"
                f"没开始 {judge_result.llm.count('not_started')}"
            )
        _print(
            f"判断 C 这次：要判的关系 {judge_result.relations} 条；本机那一档新问 {local_run.asked}，存过的 {local_run.cached}，"
            f"连不上的 {local_run.failed}，回的不是“是 / 否”的 {local_run.no_answer}，说太长、不收的 {local_run.rejected}"
            f"（以前说过太长、这次没再问的 {local_run.too_long_before}；输入 {local_run.input_tokens}，"
            f"输出 {local_run.output_tokens} token）{llm_text}；第二档存过的 {judge_result.llm_cached}，"
            f"以前重调也没用的 {judge_result.llm_failed_before}，打码没打干净不发的 {judge_result.llm_unsent}"
        )
        _print(
            f"判断 C 一整遍：判过的 {judged['judged']} 条，留下 {judged['kept']}，挡下 {judged['blocked']}"
            f"（{_count_text(judged['blocked_by_code'])}），还没判完的窗口 {judge_waiting}"  # type: ignore[arg-type]
        )
    fact_waiting = 0
    if fact_spec is not None and fact_result is None:
        _print("事实的判断没跑：前面那一段停下了")
    if fact_result is not None:
        facts = summary["precheck"]
        assert isinstance(facts, dict)
        fact_local = fact_result.local
        fact_waiting = int(facts["windows_waiting"])
        fact_llm = ""
        if fact_result.llm is not None:
            fact_llm = (
                f"；第二档新判 {fact_result.llm.count('done')}，失败 {fact_result.llm.count('failed')}，"
                f"没开始 {fact_result.llm.count('not_started')}"
            )
        _print(
            f"事实的判断这次：要判的事实 {fact_result.relations} 条；本机那一档新问 {fact_local.asked}，存过的 {fact_local.cached}，"
            f"连不上的 {fact_local.failed}，回的不是“是 / 否”的 {fact_local.no_answer}，说太长、不收的 {fact_local.rejected}"
            f"（以前说过太长、这次没再问的 {fact_local.too_long_before}；输入 {fact_local.input_tokens}，"
            f"输出 {fact_local.output_tokens} token）{fact_llm}；第二档存过的 {fact_result.llm_cached}，"
            f"以前重调也没用的 {fact_result.llm_failed_before}，打码没打干净不发的 {fact_result.llm_unsent}"
        )
        _print(
            f"事实的判断一整遍：判过的 {facts['judged']} 条，留下 {facts['kept']}，挡下 {facts['blocked']}"
            f"（{_count_text(facts['blocked_by_code'])}），转人工 {facts['held']}（{_count_text(facts['held_by_code'])}），"  # type: ignore[arg-type]
            f"财务投资转人工的关系 {facts['relations_held']} 条，还没判完的窗口 {fact_waiting}"
        )
    if stopped is not None:
        _print(f"停下了：{stopped}")
    if judge_result is not None and judge_result.local.stopped is not None and judge_result.local.stopped != stopped:
        _print(f"判断 C 本机那一档停下了：{judge_result.local.stopped}")
    if fact_result is not None and fact_result.local.stopped is not None and fact_result.local.stopped != stopped:
        _print(f"事实的判断本机那一档停下了：{fact_result.local.stopped}")
    if stopped is not None:
        if stopped.split(":")[0] in FATAL_KINDS:
            return EXIT_STOPPED
        if stopped.split(":")[0] in ("ledger", "store"):
            return EXIT_RUNTIME
    if (checker is not None and recheck_result is None) or (judge is not None and judge_result is None):
        return EXIT_ATTENTION
    if fact_spec is not None and fact_result is None:
        return EXIT_ATTENTION
    # 本机模型说太长、没判上的：当没把握照规矩办了，可还是让人看一眼（退出码 1）
    rejected = judge_result.local.rejected if judge_result is not None else 0
    rejected += fact_result.local.rejected if fact_result is not None else 0
    ok = (summary["missing"] == 0 and not batch.problems and recheck_missing == 0 and judge_waiting == 0 and rejected == 0
          and fact_waiting == 0)
    return EXIT_OK if ok else EXIT_ATTENTION


def _one_packet(settings: Settings, library: str, batch: Batch, *, verb: str = "抽") -> tuple[Batch, list[str]]:
    """提示词带现有关系时一次只处理一个包：这一批里导出最早的那个（包头的 covers_to，一样的按包号，和预处理 B 的先后一样）。
    选定以后照“只点名这一个包”重新取一遍（上文、邮件往来串里以前的信都只照这个包算），切出来的窗口和 javmem write --packet
    这个包时一样，键才对得上。返回只剩它的一批，和要打出来的话：还有几个排着；点了名、导出更早的还没写账的，提醒一句。"""
    if not batch.packets:
        return batch, []
    queued = queued_packets(settings, library)
    state = {packet_id: extraction for packet_id, _, extraction in queued}

    def order(packet: object) -> tuple[float, str]:
        return (packet.header.covers_to.timestamp(), packet.packet_id)  # type: ignore[attr-defined]

    # 写账标成 failed 的（有重调也没用的窗口，排队修）先跳过：不然它排在最前面，一次一个包就一直挑它；只剩它们的时候才挑
    waiting = [packet for packet in batch.packets if state.get(packet.packet_id) != "failed"]
    first = min(waiting or batch.packets, key=order)
    when = order(first)
    passed = [packet.packet_id for packet in batch.packets if state.get(packet.packet_id) == "failed" and order(packet) < when]
    left = [packet_id for packet_id, _, extraction in queued if packet_id != first.packet_id and extraction != "failed"]
    earlier = [
        packet_id for packet_id, epoch, extraction in queued
        if packet_id != first.packet_id and extraction != "failed" and (epoch, packet_id) < when
    ]
    if verb == "抽":
        lines = [
            f"这次只抽 {first.packet_id}（一次一个包，这一批里导出最早的那个）；"
            + (f"还有 {len(left)} 个包排着：先 javmem write --packet {first.packet_id} 写完这个包，再接着抽" if left else "没有别的包排着")
        ]
    else:
        lines = [f"这次只{verb} {first.packet_id}（提示词带现有关系：一次一个包，这一批里导出最早的那个）"]
    if passed:
        lines.append(
            f"导出更早的 {len(passed)} 个包标成了 failed（有重调也没用的窗口），先跳过；修好以后 javmem extract --retry-failed --packet 包号"
        )
    if earlier and verb == "抽":
        lines.append(f"注意：导出更早的 {len(earlier)} 个包还没写账，这个包看不到它们写进账本的关系")
    alone = load_queued(settings, library, packet_ids=[first.packet_id])
    problems = [*batch.problems, *(one for one in alone.problems if one not in batch.problems)]
    return replace(alone, problems=problems), lines


def _require_store(store_dir: Path) -> None:
    """读结果库的命令：库要已经在。写错了 --store 不能悄悄建一个空库，再报“没结果”。"""
    if not (store_dir / STORE_FILE).is_file():
        raise ConfigError(f"没有这个 L3 结果库：{store_dir}")


def _results_model(args: argparse.Namespace) -> FakeModel | ModelName:
    """读结果库时算键用的模型名：要和抽的时候一样。--fake 是假模型抽的那些。不要密钥。"""
    return FakeModel() if args.fake else ModelName(LLMSettings.from_env())


def _current_revisions(ledger_path: Path) -> set[tuple[str, int]]:
    """账本里现行、没撤回的（包号, 版本）：预处理 B 只用这些包里学来的名字（旧版本、撤回了的不用）。"""
    if not ledger_path.exists():
        return set()
    conn = connect_readonly(ledger_path)
    try:
        rows = conn.execute("SELECT packet_id, revision FROM packets WHERE state = 'current' AND withdrawn_at IS NULL").fetchall()
    finally:
        conn.close()
    return {(str(row[0]), int(row[1])) for row in rows}


def _recheck_name(args: argparse.Namespace) -> str | None:
    """加了 --recheck：算复核键用的模型名（名字@档位/输出方式），要和 extract --recheck 时一样。不要密钥。"""
    if not args.recheck:
        return None
    return FakeModel().cache_name if args.fake else ModelName(LLMSettings.recheck_from_env()).cache_name


def _judge_spec(args: argparse.Namespace, llm_name: str, prompt: str) -> reljudge.JudgeSpec | None:
    """配了判断 C（JAVMEM_JUDGE_MODE）：算键、定挡留用的设置，要和 extract 时一样（第二档就是抽取模型，llm_name 是它在
    结果键里的名字；--fake 用假的本机模型；prompt 是 L3 的提示词，判断 C 的版本跟着它）。不要密钥，不要本机模型的地址。
    没配是 None。"""
    judge = reljudge.JudgeSettings.from_env()
    if judge is None:
        return None
    return judge.spec(llm_name if judge.mode == reljudge.CASCADE else None, local=reljudge.FAKE_LOCAL_NAME if args.fake else None,
                      version=reljudge.judge_version_for(prompt))


def _prechecks(recheck: bool) -> precheck.PrecheckSettings | None:
    """进图前检查（J5.3，JAVMEM_PRECHECK）的设置：没开是 None。写错了、开了却没配判断 C 的报配置错误；和 G7 复核（--recheck）
    不一起开（都是问事实原文支不支持，一条不判两遍）。"""
    found = precheck.PrecheckSettings.from_env()
    if found is not None and recheck:
        raise ConfigError(f"开了 {precheck.ENV_PRECHECK} 不能再加 --recheck：都是问事实原文支不支持，一条不判两遍")
    return found


def _fact_spec(found: precheck.PrecheckSettings | None, args: argparse.Namespace, llm_name: str) -> reljudge.JudgeSpec | None:
    """判事实用的设置（f1），要和 extract 时一样：第二档就是抽取模型（llm_name 是它在结果键里的名字）；--fake 用假的本机模型。"""
    if found is None:
        return None
    return found.spec(llm_name if found.judge.mode == reljudge.CASCADE else None,
                      local=reljudge.FAKE_LOCAL_NAME if getattr(args, "fake", False) else None)


def _recheck_counts(checked: Sequence[object]) -> dict[str, int]:
    """有判定的事实各判成了什么。"""
    found = {verdict: 0 for verdict in VERDICTS}
    for item in checked:
        check = getattr(item, "check", None)
        for fact in check.facts if check is not None else []:
            if fact.recheck is not None:
                found[str(fact.recheck["verdict"])] += 1
    return found


def _cmd_verify(args: argparse.Namespace, settings: Settings) -> int:
    """L4 核对：拿结果库里 L3 的结果，事实按 G1 到 G7、关系按 R1 到 R6 核一遍，只出数。不调模型，不写账本。

    --recheck：再用结果库里模型复核的判定（extract --recheck 存的），部分支持、不支持的算 G7 没过。
    配了 JAVMEM_JUDGE_MODE 的，再用结果库里判断 C 的判定（extract 存的），挡下的关系算 C 没过。
    关系的核对要用主人的名字、别名表（和 javmem write 一样），写错了报配置错误。
    """
    patterns = load_patterns(settings.redaction_file)
    options = writer_options(settings)  # 主人的名字、别名表：关系的 R2、R3、R6 用，和写账用的一样
    prompt = prompt_from_env()  # 抽的时候用的 L3 提示词：键里有它
    recheck_name = _recheck_name(args)
    model = _results_model(args)
    judge_spec = _judge_spec(args, model.cache_name, prompt)
    prechecks = _prechecks(recheck_name is not None)  # 进图前检查（J5.3）：设置写错了也是这里报
    fact_spec = _fact_spec(prechecks, args, model.cache_name)
    batch = load_queued(settings, args.library, packet_ids=args.packet)
    if prompt in PRE_R_PROMPTS and args.packet is None:  # 一次一个包：和 extract 切出来的窗口一样
        batch, notes = _one_packet(settings, args.library, batch, verb="核")
        for line in notes:
            _print(line)
    windows = build_windows(batch.packets, context_pool=batch.context)
    store_dir = Path(args.store) if args.store else settings.require_data_dir() / "l3"
    _require_store(store_dir)
    with ResultStore(store_dir) as store:
        checked = verify_windows(
            windows, store, model, patterns, prompt=prompt,
            lexicon=options["lexicon"], owner_names=options["owner_names"],  # type: ignore[arg-type]
        )
        if recheck_name is not None:
            checked = apply_rechecks(checked, store, recheck_name, patterns)
        judged_c: dict[str, object] | None = None
        if judge_spec is not None:
            checked = reljudge.apply_judgments(checked, store, judge_spec, patterns)
            judged_c = reljudge.summarize_judgments(checked)  # 事实的判断以前数：还没判完的窗口只算判断 C 的
        if prechecks is not None and fact_spec is not None:  # 进图前检查（J5.3）：过了规则的事实照存下的判定
            checked = precheck.apply(checked, store, fact_spec, patterns, strict=prechecks.strict)
    summary = summarize_checks([item.check for item in checked if item.check is not None])
    summary["windows_without_result"] = sum(1 for item in checked if item.skipped == NO_RESULT)
    summary["windows_not_sent"] = sum(1 for item in checked if item.skipped == NOT_SENT)
    without_recheck = 0
    if recheck_name is not None:
        without_recheck = sum(1 for item in checked if item.skipped == NO_RECHECK)
        summary["windows_without_recheck"] = without_recheck
        summary["recheck_verdicts"] = _recheck_counts(checked)
    without_judgment = 0
    if judge_spec is not None and judged_c is not None:
        without_judgment = int(judged_c["windows_waiting"])  # type: ignore[call-overload]
        summary["judge"] = {**judge_spec.describe(), **judged_c}
    without_fact_judgment = 0
    if fact_spec is not None:
        facts = precheck.summarize(checked)
        without_fact_judgment = int(facts["windows_waiting"]) - without_judgment  # type: ignore[call-overload]
        summary["precheck"] = {**fact_spec.describe(), **facts, "windows_waiting": without_fact_judgment}
    if recheck_name is not None or judge_spec is not None:
        summary["checked_by"] = precheck.checked_by(recheck_name, judge_spec, fact_spec)
    errors: dict[str, int] = {}
    for item in checked:
        if item.skipped == ERROR:
            errors[str(item.error)] = errors.get(str(item.error), 0) + 1
    summary["windows_with_errors"] = errors  # 只有异常的类型，不带内容
    summary["problems"] = [{"packet_id": packet_id, "reason": reason} for packet_id, reason in batch.problems]
    if args.report is not None:
        Path(args.report).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        _print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for packet_id, reason in batch.problems:
            _print(f"没用上 {packet_id}：{reason}")
        _print(
            f"{len(checked)} 个窗口，有结果的 {summary['windows']}，没结果的 {summary['windows_without_result']}，"
            f"不发的 {summary['windows_not_sent']}"
        )
        if recheck_name is not None:
            _print(
                f"模型复核：还没有判定的窗口 {without_recheck}（这些不算在下面的数里），"
                f"判定 {_count_text(summary['recheck_verdicts'])}"  # type: ignore[arg-type]
            )
        if judge_spec is not None:
            judged = summary["judge"]
            assert isinstance(judged, dict)
            _print(
                f"判断 C（{judge_spec.token()}）：还没判完的窗口 {without_judgment}（这些不算在下面的数里），"
                f"判过的关系 {judged['judged']} 条，留下 {judged['kept']}，挡下 {judged['blocked']}"
                f"（{_count_text(judged['blocked_by_code'])}）"  # type: ignore[arg-type]
            )
        if fact_spec is not None:
            facts = summary["precheck"]
            assert isinstance(facts, dict)
            _print(
                f"事实的判断（{fact_spec.token()}）：还没判完的窗口 {without_fact_judgment}（这些不算在下面的数里），"
                f"判过的事实 {facts['judged']} 条，留下 {facts['kept']}，挡下 {facts['blocked']}"
                f"（{_count_text(facts['blocked_by_code'])}），转人工 {facts['held']}（{_count_text(facts['held_by_code'])}），"  # type: ignore[arg-type]
                f"财务投资转人工的关系 {facts['relations_held']} 条"
            )
        _print(f"事实 {summary['facts']} 条：过了 {summary['passed']}，隔离 {summary['quarantined']}")
        _print(f"每条规则拦下：{_count_text(summary['by_rule'])}")  # type: ignore[arg-type]
        _print(f"原因：{_count_text(summary['by_code'])}")  # type: ignore[arg-type]
        _print(f"过了的事实上的记号：{_count_text(summary['notes_on_passed_facts'])}")  # type: ignore[arg-type]
        relations = summary["relations"]
        assert isinstance(relations, dict)
        _print(f"关系 {relations['relations']} 条：过了 {relations['passed']}，隔离 {relations['quarantined']}")
        _print(f"关系每条规则拦下：{_count_text(relations['by_rule'])}")
        _print(f"关系的原因：{_count_text(relations['by_code'])}")
        _print(f"过了的关系上的记号：{_count_text(relations['notes_on_passed'])}")
    missing = summary["windows_without_result"]
    if errors:
        _print(f"核对时出错的窗口：{_count_text(errors)}")
        return EXIT_RUNTIME
    return EXIT_ATTENTION if batch.problems or missing or without_recheck or without_judgment or without_fact_judgment else EXIT_OK


def _cmd_write(args: argparse.Namespace, settings: Settings) -> int:
    """L4 核对、L5 合并和对齐、L6 写账、事实的向量。读结果库里 L3 的结果，不调抽取模型。"""
    patterns = load_patterns(settings.redaction_file)
    prompt = prompt_from_env()  # 抽的时候用的 L3 提示词：键里有它，账本的 extracted_by 也写它
    options = writer_options(settings)  # 主人的名字、别名表：写错了报配置错误，什么都不动
    recheck_name = _recheck_name(args)  # 加了 --recheck：用模型复核的判定；复核的设置写错了也是这里报
    data_dir = settings.require_data_dir()
    library = settings.library(args.library)
    token_ledger: TokenLedger | None = None
    embedder: FakeEmbeddings | OpenAIEmbeddings
    if args.fake_embeddings:
        embedder = FakeEmbeddings()
    else:
        embedder = OpenAIEmbeddings(EmbeddingSettings.from_env())  # 没配好就报配置错误，什么都不动
        token_ledger = TokenLedger(_ledger_path(settings))
    store_dir = Path(args.store) if args.store else data_dir / "l3"
    vectors_dir = Path(args.vectors) if args.vectors else data_dir / "vectors"
    _require_store(store_dir)
    model = _results_model(args)
    judge_spec = _judge_spec(args, model.cache_name, prompt)  # 配了判断 C：用存下的判定；设置写错了也是这里报
    prechecks = _prechecks(recheck_name is not None)  # 进图前检查（J5.3）：用存下的判定；设置写错了也是这里报
    fact_spec = _fact_spec(prechecks, args, model.cache_name)
    checked_by = precheck.checked_by(recheck_name, judge_spec, fact_spec)
    try:
        lock = WriteLock(data_dir)
    except WriteBusy as busy:
        _print_error(busy)
        return EXIT_BUSY
    timer = WriteTimer(iso(settings.now()))  # 巡检看写账变慢：从拿到锁起用了多久，其中等向量服务多久
    # 拿到写账的锁以后才取包、切窗：撤回要拿这把锁
    try:
        batch = load_queued(settings, args.library, packet_ids=args.packet)
        if prompt in PRE_R_PROMPTS and args.packet is None:  # 一次一个包：和 extract 切出来的窗口一样，键才对得上
            batch, notes = _one_packet(settings, args.library, batch, verb="写")
            for line in notes:
                _print(line)
        windows = build_windows(batch.packets, context_pool=batch.context)
    except BaseException:
        lock.close()
        raise
    for packet_id, reason in batch.problems:
        _print(f"没用上 {packet_id}：{reason}")

    def record(digest: str, attempt: int, status: str, tokens: int, seconds: float, estimated: bool) -> None:
        assert token_ledger is not None
        token_ledger.append(
            {
                "at": iso(datetime.now(timezone.utc).astimezone()),
                "tag": args.tag,
                "purpose": "embedding",
                "model": embedder.name,
                "window_id": "",
                "cache_key": digest,
                "attempt": attempt,
                "status": status,
                "input_tokens": tokens,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "seconds": round(seconds, 3),
            }
        )

    with lock, ResultStore(store_dir) as store, VectorCache(vectors_dir) as cache:
        checked = verify_windows(
            windows, store, model, patterns, prompt=prompt,
            lexicon=options["lexicon"], owner_names=options["owner_names"],  # type: ignore[arg-type]
        )
        if recheck_name is not None:
            checked = apply_rechecks(checked, store, recheck_name, patterns)
        judged_c: dict[str, object] | None = None
        if judge_spec is not None:  # 过了规则的关系还没判完的窗口不写，包留着排队
            checked = reljudge.apply_judgments(checked, store, judge_spec, patterns)
            judged_c = reljudge.summarize_judgments(checked)
        if prechecks is not None and fact_spec is not None:  # 进图前检查（J5.3）：过了规则的事实还没判完的窗口也不写
            checked = precheck.apply(checked, store, fact_spec, patterns, strict=prechecks.strict)
        conn = open_ledger(library)
        try:
            if args.fake_embeddings:
                real = conn.execute("SELECT count(*) FROM fact_vectors WHERE model != ?", (embedder.name,)).fetchone()[0]
                if real:  # 不然这本账的真向量全被换成假的
                    _print_error(ConfigError(f"这本账里已经有 {real} 条真向量，不能用 --fake-embeddings 写；试流程换一个数据目录"))
                    return EXIT_CONFIG
            writer = LedgerWriter(
                conn,
                extracted_by=f"{prompt} {model.cache_name}",
                embedding_model=embedder.name,
                owner_names=options["owner_names"],  # type: ignore[arg-type]
                lexicon=options["lexicon"],  # type: ignore[arg-type]
                checked_by=checked_by,
                precheck_on=fact_spec is not None,
            )
            # 只在引用里看到过的话，谁当证据（D101）：写窗口以前记下来，换了的包改回排队（第五轮第 51 条）。
            # 断在写窗口当中、写完以后都没事：记的是按账本算出来的，再跑一遍算出来一样，不会再改一遍
            with transaction(conn):
                # 每一句是原话还是引用也记下来，以后取包时查表、不再读这些包（第五轮第 50 条）
                record_quote_roles(conn, [*batch.packets, *batch.context])
                quote_requeued = record_quote_holders(conn, batch.quote_holders, batch.requeue)
            run = run_write(
                checked,
                writer,
                packets=[(packet.packet_id, packet.header.revision) for packet in batch.packets],
                store=store,
                cache=cache,
                embedder=timer.timed(embedder),  # 等向量服务的时间另记，比快慢时扣掉
                sleep=timer.sleep,
                record=record if token_ledger is not None else None,
                log=_print if args.verbose else None,
                partial=args.packet is not None,  # --packet：标了 failed 的不往回改（排队修）
            )
            with transaction(conn):  # 最后一步：照软变化的结论执行（J4.4 第 4 段；soft.db 没有、还没判的不动）
                writer.apply_soft_verdicts(settings.require_data_dir() / "soft", iso(settings.now()))
            summary = ledger_summary(conn)
            timer.finish(conn, run, facts=int(summary["facts"]), warn=_print)  # type: ignore[call-overload]  # 记一行 write_runs
        except (sqlite3.Error, OSError) as error:  # 跑到一半账本、向量缓存、token 账写不进去
            _print_error(error)
            return EXIT_RUNTIME
        finally:
            conn.close()
        # 主图（J5.2）：写完账接着同步进图。图没开、连不上只记欠着，写账照常成功、退出码不变
        graph_report = after_write(settings, args.library, embedder=embedder, cache=cache, tag=args.tag,
                                   gate=precheck.gate())  # 开了进图前检查（J5.3）只放谁认过的
    stats = run.stats
    summary["this_run"] = {
        "windows": len(checked),
        "windows_written": stats.windows,
        "windows_skipped": stats.windows_skipped,
        "windows_stale": stats.windows_stale,
        "windows_without_result": len(run.without_result),
        "claims": stats.claims,
        "quarantined_facts": stats.quarantined_facts,
        "quarantined_by_rule": dict(sorted(stats.by_rule.items())),
        "quarantined_evidence": stats.quarantined_evidence,
        "quarantined_entities": stats.quarantined_entities,
        "quarantined_relations": stats.quarantined_relations,  # J4.3：没过 L4 的关系，J4.4 起加上 L5 写不进去的
        "quarantined_relations_by_rule": dict(sorted(stats.relation_by_rule.items())),
        "quarantined_relation_evidence": stats.quarantined_relation_evidence,
        "relation_claims": stats.relation_claims,  # J4.4：写进账本的关系的说法
        "soft_held": stats.soft_held,  # J4.4 第 4 段：软变化的候选先挡住、新进了待确认的
        "soft_applied": stats.soft_applied,  # 照软变化的结论执行了的
        "corrections": stats.corrections,  # J4.6：主人在纠正的关系的说法
        "corrections_held": stats.corrections_held,  # 财务投资的纠正先挡住、新进了待确认的
        "corrected_on_spot": stats.corrected_on_spot,  # Bot 当场说错、主人紧接着纠正的，Bot 那句不记（R05，在隔离表里）
        "corrections_unmatched": stats.corrections_unmatched,  # 抽取没抽出来、对上了唯一一条的纠正，新进了待确认的
        "corrections_other": stats.corrections_other,  # 只说了新的那一条，问旧的那一条还算不算、新进了待确认的
        "chat_answers": stats.chat_answers,  # D83：主人在对话里答了 Bot 问的待确认、认出来照答的定了的
        "chat_answers_unclear": stats.chat_answers_unclear,  # 答了、编号对得上，答的话认不出来的（待确认照旧）
        "entities_created": stats.entities_created,
        "entities_dropped": stats.entities_dropped,
        "aliases_created": stats.aliases_created,
        "ambiguous_entities": stats.ambiguous_entities,
        "frozen_conflicts": stats.frozen_conflicts,
        "packets_done": stats.packets_done,
        "packets_waiting": len(run.waiting),
        "packets_failed": len(run.failed),  # 其中标成 failed 的（排队修）
        "windows_needing_retry_failed": len(run.permanent),
        "checked_by": checked_by,
        "windows_without_recheck": len(run.recheck_missing) + len(run.recheck_permanent),
        "windows_needing_recheck_retry_failed": len(run.recheck_permanent),
        "windows_without_judgment": sum(1 for item in checked if item.skipped == reljudge.NO_JUDGE),  # 判断 C、事实的判断还没判完的
        "vectors_filled": stats.vectors_filled,
        "stale_claims": stats.stale_claims,
        "embedding_calls": run.embedding.calls,
        "embedding_texts": run.embedding.texts,
        "embedding_tokens": run.embedding.tokens,
        "embedding_retries": run.embedding.retries,
        "embedding_skipped": run.embedding.skipped,
        "quote_requeued": len(quote_requeued),
        "stopped": run.stopped,
    }
    summary["embedding_model"] = embedder.name
    if judge_spec is not None and judged_c is not None:
        summary["judge"] = {**judge_spec.describe(), **judged_c}
    if fact_spec is not None:  # 进图前检查（J5.3）：这一批判过的事实、挡下的、转人工的
        summary["precheck"] = {**fact_spec.describe(), **precheck.summarize(checked)}
    errors: dict[str, int] = {}
    for item in checked:
        if item.skipped == ERROR:
            errors[str(item.error)] = errors.get(str(item.error), 0) + 1
    summary["this_run"]["windows_with_errors"] = errors  # type: ignore[index]
    summary["problems"] = [{"packet_id": packet_id, "reason": reason} for packet_id, reason in batch.problems]
    summary["graph"] = graph_report.as_dict()  # 主图（J5.2）
    if args.summary is not None:
        Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    this = summary["this_run"]
    assert isinstance(this, dict)
    failed_part = f"（其中标成 failed {this['packets_failed']} 个）" if this["packets_failed"] else ""  # 真标了的（排队修）
    _print(
        f"这次：窗口 {this['windows']} 个，写了 {this['windows_written']}，没结果的 {this['windows_without_result']}；"
        f"说法 {this['claims']}，关系的说法 {this['relation_claims']}，隔离事实 {this['quarantined_facts']}、关系 {this['quarantined_relations']}，"
        f"包 done {this['packets_done']}、"
        f"留着排队 {this['packets_waiting']}{failed_part}；"
        f"向量调用 {this['embedding_calls']} 次、{this['embedding_texts']} 条，"
        f"补上 {this['vectors_filled']} 条"
    )
    if quote_requeued:
        _print(f"只在引用里看到过的话换了一份当证据，改回排队的包 {len(quote_requeued)} 个，下次 extract、write 重抽")
    if run.permanent:
        _print(
            f"重调也没用、按规矩不发的窗口 {len(run.permanent)} 个（打码没打干净、太大、截断、拒答、private 这类），"
            "挡住的包标成 failed（巡检会报，原因记在包登记里）：截断、拒答、格式错这类修好以后（提示词、模型设置）"
            "跑 javmem extract --retry-failed；打码没打干净、太大的每次都重看，改好打码模式表、拆小以后照常跑 javmem extract"
        )
    if run.recheck_missing:
        _print(f"过了规则、还没有模型复核判定的窗口 {len(run.recheck_missing)} 个，包留着排队：先跑 javmem extract --recheck")
    if run.recheck_permanent:
        _print(
            f"模型复核重调也没用的窗口 {len(run.recheck_permanent)} 个（截断、拒答、格式错这类，或者事实那一段打码没打干净、太大），"
            "挡住的包标成 failed（巡检会报）：截断、拒答这类修好以后跑 javmem extract --recheck --retry-failed-recheck；"
            "打码没打干净、太大的改好以后照常跑 javmem extract --recheck"
        )
    if this["windows_without_judgment"]:
        _print(
            f"过了规则的关系还没判完（判断 C）的窗口 {this['windows_without_judgment']} 个，包留着排队："
            "先跑 javmem extract（要配好 JAVMEM_JUDGE_URL）"
            if fact_spec is None else
            f"过了规则的关系、事实还没判完（判断 C、进图前检查）的窗口 {this['windows_without_judgment']} 个，包留着排队："
            "先跑 javmem extract（要配好 JAVMEM_JUDGE_URL）"
        )
    _print(
        f"账本：事实 {summary['facts']}（现行 {summary['facts_current']}，被替代 {summary['facts_superseded']}），"
        f"说法 {summary['claims']}（合到已有事实上的 {summary['claims_merged']}），实体 {summary['entities']}，"
        f"关系 {summary['relations']}（被替代 {summary['relations_superseded']}），"
        f"别名 {summary['aliases']}，向量 {summary['vectors']}，隔离 {_count_text(summary['quarantine'])}"  # type: ignore[arg-type]
    )
    _print(graph_report.text())  # 主图（J5.2）
    if prompt in PRE_R_PROMPTS:  # 放现有关系是一次抽一个包：写完提醒还有几个排着（标成 failed 的另说）
        found = queued_packets(settings, args.library)
        left = [packet_id for packet_id, _, extraction in found if extraction != "failed"]
        failed = len(found) - len(left)
        if left:
            _print(
                f"还有 {len(left)} 个包排着：提示词带现有关系，一次抽一个包，接着 javmem extract"
                + (f"（另有 {failed} 个标成了 failed）" if failed else "")
            )
    if run.stopped is not None:
        _print(f"停下了：{run.stopped}")
        return EXIT_STOPPED
    if errors:  # 核对时出错的窗口没写，它们的包留着排队，修好以后重跑
        _print(f"核对时出错的窗口：{_count_text(errors)}")
        return EXIT_RUNTIME
    if summary["facts_without_vector"]:
        _print(f"还有 {summary['facts_without_vector']} 条事实没有向量（向量服务拒了这几段文字），下次 javmem write 再补")
        return EXIT_ATTENTION
    return EXIT_ATTENTION if batch.problems or run.without_result else EXIT_OK


def _admin_dirs(args: argparse.Namespace, settings: Settings) -> tuple[Path | None, Path | None]:
    """管理命令的 L3 结果库和向量缓存：写了就要在，不写用数据目录下的（没有就不清）。"""
    store = Path(args.store) if args.store else None
    if store is not None:
        _require_store(store)
    vectors = Path(args.vectors) if args.vectors else None
    if vectors is not None and not (vectors / "vectors.db").is_file():
        raise ConfigError(f"没有这个向量缓存：{vectors}")
    return store, vectors


def _write_report(args: argparse.Namespace, report: dict[str, object]) -> None:
    if args.report is not None:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        _print(json.dumps(report, ensure_ascii=False, indent=2))


def _cmd_withdraw(args: argparse.Namespace, settings: Settings) -> int:
    """撤回（J2.11）：按包或单条（事实、原文句）。默认只标记原文，派生的删掉。--undo 解除撤回，只按包。"""
    if args.undo:
        if args.fact or args.sentence or not args.packet:
            raise ConfigError("解除撤回只能按包：--undo --packet 包号")
        try:
            undone = unwithdraw(settings, args.library, args.packet)
        except (IntakeBusy, WriteBusy) as busy:
            _print_error(busy)
            return EXIT_BUSY
        except WithdrawError as error:
            _print_error(error)
            return EXIT_ATTENTION
        queued = [packet_id for packet_id in undone.packets if packet_id not in undone.resend]
        _print(f"解除撤回 {len(undone.packets)} 个包，重新排队：{'、'.join(queued) or '没有'}")
        if undone.resend:
            _print(f"这本账里没有这些包的登记（撤回记录是整本重建时导进来的），要再投一次原包：{'、'.join(undone.resend)}")
        _say_graph(args, _graph_after_admin(settings, args.library))  # 主图（J5.6）
        return EXIT_OK
    if args.export is not None:
        if args.packet or args.fact or args.sentence or args.import_file:
            raise ConfigError("--export 不和别的一起写")
        records = export_records(settings, args.library)
        Path(args.export).write_text(json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        _print(f"撤回的记录：包 {len(records['packets'])} 个，单条 {len(records['sentences'])} 句，写到 {args.export}")
        return EXIT_OK
    records = None
    if args.import_file is not None:
        if args.packet or args.fact or args.sentence:
            raise ConfigError("--import 不和 --packet、--fact、--sentence 一起写")
        try:
            records = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigError(f"撤回记录读不了：{type(error).__name__}") from None
        if not isinstance(records, dict):
            raise ConfigError("撤回记录的格式不对")
    store, vectors = _admin_dirs(args, settings)
    try:
        report = withdraw(
            settings,
            args.library,
            packets=args.packet,
            facts=args.fact,
            sentences=args.sentence,
            store_dir=store,
            vectors_dir=vectors,
            records=records,
        )
    except (IntakeBusy, WriteBusy, StoreBusy) as busy:
        _print_error(busy)
        return EXIT_BUSY
    except WithdrawError as error:
        _print_error(error)
        return EXIT_ATTENTION
    graph = _graph_after_admin(settings, args.library, vectors=vectors)  # 主图（J5.6）：撤回做完、锁都放了以后
    _write_report(args, {**report.to_dict(), "graph": graph.as_dict()})
    if not args.json:
        if not (args.packet or args.fact or args.sentence or records is not None):
            _print("没有点名的：只把结果库、向量缓存、跟着标的引用清了一遍")
        _print(
            f"撤回：包 {len(report.packets)} 个，事实 {len(report.facts)} 条，原文句 {len(report.sentences)} 句"
            f"（早就撤回了的 {len(report.already)} 个）"
        )
        if report.mails:
            _print(f"点到了邮件的主题、发件人，这几封信整封撤回了：{'、'.join(report.mails)}")
        if report.masked:
            _print(f"按事实撤回了这几封信的主题、发件人，信里别的句子还在，主题、说话人写成“[已撤回]”：{'、'.join(report.masked)}")
        _print(
            f"账本：事实 {report.facts_before} → {report.facts_after}，说法 {report.claims_before} → {report.claims_after}；"
            f"改回排队的包 {len(report.requeued)} 个，要重抽"
        )
        if report.hidden:
            _print(f"证据都撤回了的冻结事实、关系 {len(report.hidden)} 条：先藏起来，进了待确认，等主人定")
        store_text = f"窗口 {report.store_windows} 个、结果 {report.store_results} 个"
        _print(f"L3 结果库：{store_text}；向量缓存删了 {report.vectors_deleted} 条；软变化判定（soft.db）删了 {report.soft_deleted} 条")
        if report.cleared_claims or report.cleared_quarantine:  # J5.6
            _print(
                f"要重抽的窗口：别的包在里面写的说法先删了 {report.cleared_claims} 处（下一次 extract、write 照剩下的原文"
                f"重抽回来），隔离行删了 {report.cleared_quarantine} 行"
            )
        if report.copies_elsewhere:
            _print(f"别的地方还有一字不差、或者带着它的句子 {len(report.copies_elsewhere)} 句（转发、又说了一遍、回信的主题），没动，要看一下")
        for problem in report.problems:
            _print(f"没做成：{problem}")
    _say_graph(args, graph, withdrawn=True)
    _side_after_withdraw(args, settings)
    return EXIT_OK if graph.status in graph_hook.FINE else EXIT_ATTENTION


def _graph_after_admin(settings: Settings, library: str, *, vectors: Path | None = None) -> SyncReport:
    """管理命令改完账本接着同步主图（J5.6，javmem.graph.hook.after_admin）：什么都不抛，结果在 SyncReport 里。"""
    return graph_hook.after_admin(settings, library, vectors_dir=vectors)


def _say_graph(args: argparse.Namespace, report: SyncReport, *, withdrawn: bool = False) -> None:
    """主图那一行：这本账没接上图、没有要同步的不打；--json 的打到 stderr（标准输出只放 JSON）。withdrawn（撤回、
    彻底删除）：没同步成的再说一句撤回的内容可能还在主图里、怎么补。"""
    if report.status in ("idle", "not_set_up"):
        return
    lines = [report.text()]
    if withdrawn and report.status not in graph_hook.FINE:
        first = _GRAPH_FIX.get(report.status, "弄好以后")
        lines.append(
            "主图还没清：撤回、彻底删除的内容可能还在主图里（查询接口只拿图里的编号回账本取内容，答不出去），"
            f"{first}跑 javmem graph sync --library {args.library}"
        )
    for line in lines:
        _say(args, line)


# 主图没同步成时，先做什么再补（J5.6）
_GRAPH_FIX = {
    "unavailable": "起图以后",
    "busy": "等正在跑的那个同步完了再",
    "disabled": "",  # javmem graph sync 不看 JAVMEM_GRAPH_SYNC，关着也能跑
    "no_password": "设好密码以后",
    "auth": "密码对上以后",
    "config": "设置改好以后",
    "refused": "先看连的是不是这本账的图（标记对不上），对了以后",
    "untracked": "",
    "unclean": "",
}


def _say(args: argparse.Namespace, line: str) -> None:
    """--json 的打到 stderr（标准输出只放 JSON），不然照常打。"""
    if getattr(args, "json", False):
        print(line, file=sys.stderr)
    else:
        _print(line)


def _side_after_withdraw(args: argparse.Namespace, settings: Settings,
                         purged: Mapping[str, Sequence[int]] | None = None) -> None:
    """撤回、彻底删除做完以后，支线的体检库也扫一遍（J6.4，javmem.side.hook）。没用过支线的库什么都不做。
    purged：彻底删除的包号 → 这一次删了哪几版（提示“体检库里可能还留着”只对进过体检库的那几版说）。
    要 JSON 输出的，支线的几句打到 stderr。"""
    from javmem.side.hook import after_withdraw

    def say(text: str) -> None:
        if args.json:
            print(text, file=sys.stderr)
        else:
            _print(text)

    after_withdraw(settings, args.library, purged=purged or {}, out=say)


def _cmd_purge(args: argparse.Namespace, settings: Settings) -> int:
    """彻底删除：撤回了的包（全部版本），或者替换下来的旧版本。删了就回不来，要加 --yes。"""
    if not args.yes:
        what = f"{args.packet} 第 {args.revision} 版" if args.revision is not None else f"{args.packet} 的全部版本"
        _print(f"会删掉 {what} 的归档文件和原文，回不来。确定的话加 --yes")
        return EXIT_ATTENTION
    store, vectors = _admin_dirs(args, settings)
    try:
        report = purge(settings, args.library, args.packet, revision=args.revision, store_dir=store, vectors_dir=vectors)
    except (IntakeBusy, WriteBusy, StoreBusy) as busy:
        _print_error(busy)
        return EXIT_BUSY
    except WithdrawError as error:
        _print_error(error)
        return EXIT_ATTENTION
    graph = _graph_after_admin(settings, args.library, vectors=vectors)  # 主图（J5.6）
    _write_report(args, {**report.to_dict(), "graph": graph.as_dict()})
    if not args.json:
        if report.already:
            _print(f"{args.packet} 早就彻底删除过了，又整理了一遍库文件")
        else:
            _print(
                f"彻底删除 {args.packet} 第 {'、'.join(map(str, report.revisions))} 版：归档文件 {len(report.files)} 个，"
                f"原文句 {report.sentences} 句；L3 结果库窗口 {report.store_windows} 个、结果 {report.store_results} 个；"
                f"向量缓存 {report.vectors_deleted} 条；软变化判定（soft.db）{report.soft_deleted} 条"
            )
        if report.hidden:
            _print(f"证据都删掉了的冻结事实、关系 {len(report.hidden)} 条：先藏起来，进了待确认")
    _say_graph(args, graph, withdrawn=True)
    if graph.status != "not_set_up" and report.revisions:  # 这本账接着主图、这一次真删了：社区版磁盘上可能还留着
        _say(args, graph_hook.purge_note(args.library, synced=graph.status in graph_hook.FINE))
    _side_after_withdraw(args, settings, purged={args.packet: report.revisions})
    if report.incomplete:
        _say(args, f"{'、'.join(report.incomplete)}有查询正读着，整理没做完，删掉的内容可能还在空页、日志里：等查询结束再跑一次 purge")
        return EXIT_ATTENTION
    return EXIT_OK if graph.status in graph_hook.FINE else EXIT_ATTENTION


def _cmd_export_facts(args: argparse.Namespace, settings: Settings) -> int:
    """事实导出：一行一条 JSON，带来源和信任的标记。有原文，写到仓库外。"""
    conn = open_ledger(settings.library(args.library))
    try:
        written = 0
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            for item in export_facts(conn, hidden=args.hidden):
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                written += 1
        packet_rows = 0
        if args.packets is not None:
            with open(args.packets, "w", encoding="utf-8", newline="\n") as handle:
                for item in export_packets(conn):
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    packet_rows += 1
    finally:
        conn.close()
    _print(f"事实 {written} 条写到 {args.out}" + (f"，包 {packet_rows} 个写到 {args.packets}" if args.packets else ""))
    return EXIT_OK


def _cmd_score(args: argparse.Namespace, settings: Settings) -> int:
    """打分（J3.8）：不碰数据目录，只读点名的账本、结果库和金标准，写进 --out。"""
    del settings
    from javmem.scoring.run import run

    return run(args, out=_print)


def _cmd_soft(args: argparse.Namespace, settings: Settings) -> int:
    """软变化判定（J4.5）：find 找候选、judge 调 Claude 判、score 对答案，见 javmem.soft.cli。"""
    from javmem.soft.cli import run

    return run(args, settings, out=_print)


def _cmd_side(args: argparse.Namespace, settings: Settings) -> int:
    """Graphiti 支线（第 6 步）：init、run、sweep、status、export，见 javmem.side.cli。"""
    from javmem.side.cli import run

    return run(args, settings, out=_print)


def _cmd_graph(args: argparse.Namespace, settings: Settings) -> int:
    """主图（第 5 步 J5.1、J5.2）：init、sync、rebuild、status、export、check，见 javmem.graph.cli。"""
    from javmem.graph.cli import run

    return run(args, settings, out=_print)


def _count_text(counts: dict[str, int]) -> str:
    return "，".join(f"{name} {count}" for name, count in sorted(counts.items())) or "没有"


def _sum_counts(items: Iterable[dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for counts in items:
        for name, count in counts.items():
            total[name] = total.get(name, 0) + count
    return total


def _print_convert(report: ConvertReport) -> None:
    for package in report.packages:
        if package.error is not None:
            _print(f"没转 {package.file}（{package.error}）")
        else:
            _print(f"转 {package.file}：{package.records} 条，写进 {package.written} 条，{len(package.packets)} 个新包")
    for rejected in report.rejected:
        _print(f"没过校验 {rejected['packet_id']}（{rejected['code']}）：{rejected['detail']}")
    _print(
        f"1.2 包 {len(report.packages)} 个，记录 {sum(p.records for p in report.packages)} 条，"
        f"写进 {sum(p.written for p in report.packages)} 条，新包 {len(report.packets)} 个"
    )
    _print(f"没进原话的：{_count_text(_sum_counts(p.skipped for p in report.packages))}")
    _print(f"要留意的：{_count_text(_sum_counts(p.notes for p in report.packages))}")
    _print(f"附件：{_count_text(_sum_counts(p.attachments for p in report.packages))}")
    _print(f"打码：{_count_text(_sum_counts(p.redactions for p in report.packages))}")
    _print(f"警告：{_count_text(_sum_counts(p.warnings for p in report.packets))}")


def _cmd_convert(args: argparse.Namespace, settings: Settings) -> int:
    """把 1.2 包转成新格式。配置写错了什么都不转。没过校验的包不写。"""
    registry = load_registry(settings.sources_file)
    config = load_convert_config(Path(args.config), registry)
    if args.revision < 1:
        raise ConfigError("--revision 要从 1 起")
    if args.max_chars < 0:
        raise ConfigError("--max-chars 不能是负数")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)  # 先建好，不然包都写了才发现报告写不了
    report = convert_directory(
        Path(args.input),
        Path(args.out),
        config=config,
        registry=registry,
        patterns=load_patterns(settings.redaction_file),
        env=args.env,
        revision=args.revision,
        max_chars=args.max_chars,
        attachments_dir=Path(args.attachments) if args.attachments else None,
    )
    if args.report:
        Path(args.report).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    _print_convert(report)
    return EXIT_OK if report.ok else EXIT_ATTENTION


def _print_mail_import(report: ImportReport) -> None:
    for bad in report.bad:
        _print(f"没导 {bad['location']}（{bad['reason']}）")
    for rejected in report.rejected:
        _print(f"没过校验 {rejected['packet_id']}（{rejected['code']}）：{rejected['detail']}")
    for item in report.resent:
        if "kept_packet" in item:  # 以前的批次已经导过（D93）
            _print(f"同一个编号改了以后重发，没导 {item['location']}（{item['date']}）：以前导过，在包 {item['kept_packet']} 里")
        else:
            _print(f"同一个编号改了以后重发，没导 {item['location']}（{item['date']}），导的是 {item['kept']}（{item['kept_date']}）")
    for packet in report.packets:
        _print(f"写了 {packet['path']}：{packet['mails']} 封，{packet['threads']} 个往来串")
    _print(
        f"读到 {report.seen} 封，导入 {report.imported} 封，以前导过 {report.already_imported} 封，"
        f"这一批里重复 {report.duplicates} 封，坏的 {len(report.bad)} 封，新包 {len(report.packets)} 个"
    )
    _print(f"要留意的：{_count_text(report.notes)}")
    _print(f"附件：{_count_text(report.attachments)}")
    _print(f"打码：{_count_text(report.redactions)}")
    _print(f"报告：{report.report_path}")


def _cmd_mail_import(args: argparse.Namespace, settings: Settings) -> int:
    """把 .eml、.mbox 转成邮件包。配置错了什么都不导。坏邮件不进包，写进报告。"""
    registry = load_registry(settings.sources_file)
    config = load_mail_config(Path(args.rules) if args.rules else None, registry, args.source)
    if args.max_chars < 0:
        raise ConfigError("--max-chars 不能是负数")
    try:
        report = import_mail(
            [Path(item) for item in args.inputs],
            source=args.source,
            registry=registry,
            config=config,
            patterns=load_patterns(settings.redaction_file),
            out_dir=Path(args.out),
            work_dir=Path(args.work_dir),
            zone=settings.timezone,
            now=settings.now(),
            env=args.env,
            kind=args.kind,
            max_chars=args.max_chars,
        )
    except ImportBusy as busy:
        _print(str(busy))
        return EXIT_BUSY
    _print_mail_import(report)
    return EXIT_OK if report.ok else EXIT_ATTENTION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="javmem", description="独立的记忆模块")
    commands = parser.add_subparsers(dest="command", required=True)

    version = commands.add_parser("version", help="看版本")
    version.set_defaults(func=_cmd_version)

    versions_cmd = commands.add_parser(
        "versions", help="这一套现在用的全部版本（U2）：代码、账本、规则、提示词、模型、探针；--save 存基准（仓库外），--check 和基准比"
    )
    from javmem.versions import add_arguments as _versions_arguments

    _versions_arguments(versions_cmd)
    versions_cmd.set_defaults(func=_cmd_versions)

    regress_cmd = commands.add_parser(
        "regress", help="回归跑分（U2）：照套件重建金标准、调试题只检索、记账核一遍，和基准比（不调付钱的模型）"
    )
    from javmem.regress.run import add_arguments as _regress_arguments

    _regress_arguments(regress_cmd)
    regress_cmd.set_defaults(func=_cmd_regress)

    from javmem.backup import add_backup_arguments, add_restore_arguments

    backup_cmd = commands.add_parser("backup", help="备份（U2）：拿锁、在线备份 SQLite、拷归档和配置、记清单，--push 再推到异地")
    add_backup_arguments(backup_cmd)
    backup_cmd.set_defaults(func=_cmd_backup)
    restore_cmd = commands.add_parser("restore-check", help="恢复核对（U2）：核清单、恢复到新目录、照撤回清单再删一遍、比巡检")
    add_restore_arguments(restore_cmd)
    restore_cmd.set_defaults(func=_cmd_restore_check)

    drill_cmd = commands.add_parser("drill", help="演练（U2）：delete 删除演练、poison 投毒演练（只在拷贝、临时数据目录上做）")
    from javmem.drill.cli import add_arguments as _drill_arguments

    _drill_arguments(drill_cmd)
    drill_cmd.set_defaults(func=_cmd_drill)

    demo_cmd = commands.add_parser("demo", help="编的演示（U3）：不要密钥、不调模型，收包、写账、查询、打分、撤回走一整圈")
    from javmem.demo import add_arguments as _demo_arguments

    _demo_arguments(demo_cmd)
    demo_cmd.set_defaults(func=_cmd_demo)

    gold_cmd = commands.add_parser("gold", help="自己做金标准（校准工具包）：prelabel 出草稿、review 审阅页面、check 核写得对不对")
    from javmem.calib.cli import add_gold_arguments

    add_gold_arguments(gold_cmd)
    gold_cmd.set_defaults(func=_cmd_gold)

    selfcheck_cmd = commands.add_parser(
        "selfcheck", help="不用金标准的四个数（校准工具包）：两遍一样不一样、证据找得到的比例、合在一起对不上的、一样东西几个名字")
    from javmem.calib.selfcheck import add_selfcheck_arguments

    add_selfcheck_arguments(selfcheck_cmd)
    selfcheck_cmd.set_defaults(func=_cmd_selfcheck)

    quiz_cmd = commands.add_parser(
        "quiz", help="拿自己的题让模型答一遍、判一遍分（校准工具包，可选）：调 JAVMEM_LLM_* 配的模型、花钱，先 --dry-run")
    from javmem.calib.quiz import add_quiz_arguments

    add_quiz_arguments(quiz_cmd)
    quiz_cmd.set_defaults(func=_cmd_quiz)

    init = commands.add_parser("init", help="建数据目录、账本和来源目录")
    init.set_defaults(func=_cmd_init)

    intake = commands.add_parser("intake", help="收件：校验、去重、登记、归档")
    intake.add_argument("--watch", type=float, metavar="秒", help="一直跑，每隔这么多秒收一遍")
    intake.set_defaults(func=_cmd_intake)

    patrol = commands.add_parser("patrol", help="巡检")
    patrol.add_argument("--json", action="store_true", help="输出 JSON")
    patrol.add_argument("--ack", action="append", default=[], metavar="编号", help="这条看过了，以后不再报（巡检〔〕里的编号），可以写几次")
    patrol.add_argument("--ack-kind", action="append", default=[], metavar="种类", help="这一种现在的提醒都标看过了，可以写几次")
    patrol.set_defaults(func=_cmd_patrol)

    raw = commands.add_parser("raw-search", help="查原文，身份看环境变量 JAVMEM_CALLER")
    raw.add_argument("query", help="关键词。空格分开的几个词要命中同一句，每个词至少两个字")
    raw.add_argument("--limit", type=int, help="这一页最多几条，1 到 100，默认 20")
    raw.add_argument("--partition", action="append", help="只留这个分区的，可以写几次")
    raw.add_argument("--from", dest="time_from", metavar="时间", help="YYYY-MM-DD 或者带时区的完整时间")
    raw.add_argument("--to", dest="time_to", metavar="时间", help="同上，只写日期时那一天整天都算")
    raw.add_argument("--source", help="只查这个来源的包")
    raw.add_argument("--packet-id", help="只查这个包")
    raw.add_argument("--thread-id", help="只查这个邮件往来串")
    raw.add_argument("--speaker", help="只查这个说话人说的")
    raw.add_argument("--context", type=int, help="前后各带几句，0 到 5")
    raw.add_argument("--cursor", help="翻页，填上一页的 next_cursor")
    raw.set_defaults(func=_cmd_raw_search)

    serve = commands.add_parser("serve", help="查询服务（MCP，stdio），身份看环境变量 JAVMEM_CALLER")
    serve.set_defaults(func=_cmd_serve)

    query = commands.add_parser("query", help="问一次查询工具，输出和工具一样的 JSON，身份看环境变量 JAVMEM_CALLER")
    query.add_argument("tool", choices=["memory_search", "entity_card", "timeline", "relation_at", "raw_search"], help="工具名")
    query.add_argument("arguments", nargs="?", help="参数 JSON，比如 {\"query\": \"报价单\"}；不写就从标准输入读")
    query.set_defaults(func=_cmd_query)

    convert = commands.add_parser("convert-1.2", help="把 1.2 包转成新格式")
    convert.add_argument("input", help="放 1.2 包的目录，只读")
    convert.add_argument("--config", required=True, help="转换配置：哪个包算哪个来源、什么分区")
    convert.add_argument("--out", required=True, help="写到这个目录下的 <来源>/，可以直接是数据目录下的 inbox-test")
    convert.add_argument("--env", choices=("prod", "test"), default="test", help="包头的 env，默认 test")
    convert.add_argument("--revision", type=int, default=1, help="内容改了要重发时加一")
    convert.add_argument("--max-chars", type=int, default=MAX_RAW_CHARS, help="原话超过这么多字就拆，0 是不拆")
    convert.add_argument(
        "--attachments",
        metavar="目录",
        help="附件的文件在 目录/<1.2 包号>/ 下，按 relative_path 找。不写就在 1.2 包所在的目录找。只读",
    )
    convert.add_argument("--report", help="把逐包、逐条的结果写成 JSON。不要放在收件目录里")
    convert.set_defaults(func=_cmd_convert)

    mail = commands.add_parser("mail-import", help="把 .eml、.mbox 转成邮件包")
    mail.add_argument("inputs", nargs="+", help=".eml、.mbox 文件，或者放它们的目录")
    mail.add_argument("--source", required=True, help="来源编号，要是登记过的邮件来源。一个邮箱一个来源")
    mail.add_argument("--work-dir", required=True, help="导入工具自己的目录，放状态和报告，不能放在收件目录里")
    mail.add_argument("--out", required=True, help="包写到这个目录下的 <来源>/，可以直接是数据目录下的 inbox-test")
    mail.add_argument("--rules", help="分包规则：按发件人、主题分到不同的分区和敏感标记")
    mail.add_argument("--env", choices=("prod", "test"), default="test", help="包头的 env，默认 test")
    mail.add_argument("--kind", choices=MAIL_KINDS, default="backfill", help="backfill 按天分包，daily 从上一包接到现在")
    mail.add_argument("--max-chars", type=int, default=MAX_RAW_CHARS, help="一个包的正文超过这么多字就在两封邮件之间切，0 是不切")
    mail.set_defaults(func=_cmd_mail_import)

    windows = commands.add_parser("windows", help="L1 规整、L2 切窗，看每包几个窗口、每个窗口多大")
    windows.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    windows.add_argument("--packet", action="append", help="只看这个包，可以写几次")
    windows.add_argument("--json", action="store_true", help="输出 JSON")
    windows.add_argument("--report", help="把 JSON 写到这个文件")
    windows.add_argument("--show", metavar="窗口", help="打印这个窗口发给模型的文字（打过码），会有原文")
    windows.set_defaults(func=_cmd_windows)

    extract = commands.add_parser("extract", help="L3 抽取：一个窗口调一次模型，结果存进结果库")
    extract.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    extract.add_argument("--packet", action="append", help="只抽这个包，可以写几次")
    extract.add_argument("--store", help="结果库的目录，默认数据目录下的 l3")
    extract.add_argument("--tag", default="javmem", help="记账的标签，预算按它算")
    extract.add_argument("--budget", type=int, help="这个标签一共最多用多少 token，记账里已经有的也算")
    extract.add_argument("--max-minutes", type=float, help="过了这么多分钟不再开始新的调用")
    extract.add_argument("--limit", type=int, help="这次最多新调几个窗口")
    extract.add_argument("--summary", help="把这批一整遍的数写成 JSON")
    extract.add_argument("--dry-run", action="store_true", help="只看要调几个窗口、多少字，不调模型")
    extract.add_argument("--fake", action="store_true", help="用假模型，不连网，试流程用")
    extract.add_argument(
        "--retry-failed", action="store_true", help="以前截断、拒答、太长、内容被挡、格式错过的窗口也重调，默认不调（只管 L3）"
    )
    extract.add_argument(
        "--recheck",
        action="store_true",
        help="L3 之后，过了规则的事实再让模型看一遍原文支不支持（L4 的模型复核），判定存进结果库；同一个标签和预算",
    )
    extract.add_argument(
        "--retry-failed-recheck",
        action="store_true",
        help="复核以前截断、拒答、格式错过的窗口也重调（--retry-failed 只管 L3，两个各管各的）",
    )
    extract.add_argument(
        "--retry-failed-judge",
        action="store_true",
        help="判断 C 第二档以前截断、拒答、格式错过的关系也重调（配了 JAVMEM_JUDGE_MODE=local+llm 才有第二档）",
    )
    extract.add_argument("--window", action="append", help="只处理这个窗口，可以写几次；这一批里没有的报错，一个都不调")
    extract.set_defaults(func=_cmd_extract)

    verify = commands.add_parser("verify", help="L4 核对：结果库里 L3 的结果按 G1 到 G7 核一遍，只出数")
    verify.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    verify.add_argument("--packet", action="append", help="只看这个包，可以写几次")
    verify.add_argument("--store", help="L3 结果库的目录，默认数据目录下的 l3")
    verify.add_argument("--fake", action="store_true", help="读 javmem extract --fake 抽的结果")
    verify.add_argument("--json", action="store_true", help="输出 JSON")
    verify.add_argument("--report", help="把 JSON 写到这个文件")
    verify.add_argument(
        "--recheck", action="store_true", help="用模型复核的判定（extract --recheck 存的）：部分支持、不支持的算 G7 没过"
    )
    verify.set_defaults(func=_cmd_verify)

    write = commands.add_parser("write", help="L4 核对、L5 合并、L6 写账、事实的向量：结果库里 L3 的结果写进账本")
    write.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    write.add_argument("--packet", action="append", help="只写这个包，可以写几次")
    write.add_argument("--store", help="L3 结果库的目录，默认数据目录下的 l3")
    write.add_argument("--vectors", help="向量缓存的目录，默认数据目录下的 vectors")
    write.add_argument("--fake", action="store_true", help="读 javmem extract --fake 抽的结果")
    write.add_argument("--fake-embeddings", action="store_true", help="用假的向量，不连向量服务，试流程用")
    write.add_argument("--tag", default="javmem", help="向量服务调用记账的标签")
    write.add_argument("--summary", help="把这一次和账本的数写成 JSON")
    write.add_argument("--verbose", action="store_true", help="每写完一个窗口打印一行")
    write.add_argument(
        "--recheck",
        action="store_true",
        help="用模型复核的判定：部分支持、不支持的进隔离表；过了规则却还没有判定的窗口不写，包留着排队",
    )
    write.set_defaults(func=_cmd_write)

    withdraw_cmd = commands.add_parser("withdraw", help="撤回：按包或单条（事实、原文句）。默认只标记原文，派生的删掉")
    withdraw_cmd.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    withdraw_cmd.add_argument("--packet", action="append", default=[], help="撤回这个包，可以写几次")
    withdraw_cmd.add_argument("--fact", action="append", default=[], help="撤回这条事实：它的证据原句都标撤回，可以写几次")
    withdraw_cmd.add_argument("--sentence", action="append", default=[], help="撤回这一句原文（句子编号），可以写几次")
    withdraw_cmd.add_argument("--undo", action="store_true", help="解除撤回，只能按包；包重新排队，要重抽")
    withdraw_cmd.add_argument("--export", metavar="文件", help="把撤回的记录（包号、单条的指纹，不带原文）写成 JSON，整本重建时用")
    withdraw_cmd.add_argument("--import", dest="import_file", metavar="文件", help="导进 --export 写的撤回记录：收包以前导，撤回过的照样不收")
    withdraw_cmd.add_argument("--store", help="L3 结果库的目录，默认数据目录下的 l3（没有就不清）")
    withdraw_cmd.add_argument("--vectors", help="向量缓存的目录，默认数据目录下的 vectors（没有就不清）")
    withdraw_cmd.add_argument("--report", help="把结果写成 JSON（只有编号和数）")
    withdraw_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    withdraw_cmd.set_defaults(func=_cmd_withdraw)

    purge_cmd = commands.add_parser("purge", help="彻底删除：撤回了的包，或者替换下来的旧版本。删归档文件和原文")
    purge_cmd.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    purge_cmd.add_argument("--packet", required=True, help="包号")
    purge_cmd.add_argument("--revision", type=int, help="只删这一版（替换下来的旧版本），不写是撤回了的包的全部版本")
    purge_cmd.add_argument("--store", help="L3 结果库的目录，默认数据目录下的 l3（没有就不清）")
    purge_cmd.add_argument("--vectors", help="向量缓存的目录，默认数据目录下的 vectors（没有就不清）")
    purge_cmd.add_argument("--yes", action="store_true", help="确定要删：删了就回不来")
    purge_cmd.add_argument("--report", help="把结果写成 JSON（只有编号和数）")
    purge_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    purge_cmd.set_defaults(func=_cmd_purge)

    freeze_cmd = commands.add_parser("freeze", help="冻结事实、关系：L5 不自动替代，和它冲突的进待确认")
    freeze_cmd.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    freeze_cmd.add_argument("--fact", action="append", default=[], help="事实编号（F 开头），可以写几次")
    freeze_cmd.add_argument("--relation", action="append", default=[], help="关系编号（R 开头，J4.4），可以写几次")
    freeze_cmd.add_argument("--undo", action="store_true", help="解除冻结，照常按新旧")
    freeze_cmd.set_defaults(func=_cmd_freeze)

    review = commands.add_parser("review", help="待确认队列：看、处理一条、每天一条数量提醒")
    review.add_argument("--library", choices=("prod", "test"), help="哪个库（--remind 不写是两个库）")
    review.add_argument("--json", action="store_true", help="输出 JSON")
    review.add_argument("--decide", metavar="编号", help="处理这一条：# 后面的数字，或者 Q 开头的编号（D83）")
    review.add_argument("--keep", choices=("frozen", "new", "both", "hidden"), help="留原值、用新值、两边都留、接着藏着")
    review.add_argument("--drop", action="store_true", help="删掉（证据都撤回了的冻结事实、冻结关系）")
    review.add_argument("--verdict", choices=("A", "B", "C", "D", "E"),
                        help="软变化的六选一（J4.4）：A 仍然有效、B 暂停、C 结束或取消、D 改成新的、E 只是犹豫或条件")
    review.add_argument("--at", metavar="日子", help="软变化判的从哪天起（B、C、D），没写的取说那句话的时候")
    review.add_argument("--condition", metavar="文字", help="软变化判暂停的，等什么")
    review.add_argument("--remind", action="store_true", help="数量提醒，每天一条；先把 30 天没人处理的按两边都留关掉")
    review.add_argument("--force", action="store_true", help="今天提醒过了也再打印一次")
    review.add_argument("--export", metavar="文件", help="把冻结标记和待确认（定过的、没处理的）连同提到的事实写成 JSON，整本重建前用（有原文，写到仓库外）")
    review.add_argument("--import", dest="import_file", metavar="文件", help="导进 --export 写的冻结标记和决定：整本重建、写完账以后导")
    review.set_defaults(func=_cmd_review)

    audit_cmd = commands.add_parser("audit", help="留痕（J5.3）：一条从抽出来到进图每一级谁判的、P 多少、依据哪几句；只读")
    audit_cmd.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    audit_cmd.add_argument("--item", metavar="编号", help="事实（F 开头）、关系（R 开头）或者说法的编号（窗口编号#键#第几条）")
    audit_cmd.add_argument("--packet", metavar="包号", help="这个包的留痕和隔离表里拦下的")
    audit_cmd.add_argument("--summary", action="store_true", help="整本账按种类、哪一级、判成什么出数（什么都不写也是它）")
    audit_cmd.add_argument("--text", action="store_true", help="--item 的依据那几句也打原文（有原文，在本机看）")
    audit_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    audit_cmd.set_defaults(func=_cmd_audit)

    entities = commands.add_parser("entities", help="认人认事：合并的提议、确认卡片、合并、不是同一个、撤销、拆开、历史")
    entities.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    entities.add_argument("--proposals", action="store_true", help="列出合并的提议，一条一行（默认就是这个）")
    entities.add_argument("--source", action="append", choices=SOURCES, help="只要这一处来的提议，可以写几次")
    entities.add_argument("--show", metavar="提议", help="出这一条提议的确认卡片（有原话，在会话里看）")
    entities.add_argument("--fake", action="store_true", help="卡片上模型的看法用假模型，不连网，试流程用")
    entities.add_argument("--no-model", action="store_true", help="卡片上不要模型的看法，不调模型")
    entities.add_argument(
        "--budget", type=int, default=10_000_000, help="token 账里一共（各个标签加起来）最多用到多少，超了不调模型，默认 1000 万"
    )
    entities.add_argument("--tag", default="javmem-merge", help="调模型记账的标签")
    entities.add_argument("--merge", metavar="提议", help="照这一条提议合并：写一条合并记录，当场生效")
    entities.add_argument("--name", help="合并以后的标准名，不写用提议建议的")
    entities.add_argument("--type", choices=ENTITY_TYPES_L3, help="合并以后的类型，不写用提议建议的")
    entities.add_argument("--only", action="append", metavar="实体", help="只并这几个（默认是提议里勾上的），可以写几次")
    entities.add_argument("--apart", metavar="提议", help="记成不是同一个：以后不再提，L5 也不按别名把它们连起来")
    entities.add_argument("--undo", metavar="记录", help="撤销一条合并记录（M 开头），当场生效")
    entities.add_argument("--detach", metavar="实体", help="拆开：这个实体的一个叫法不是它（配 --alias），当场生效")
    entities.add_argument("--alias", help="--detach 的那个叫法，要一字不差")
    entities.add_argument("--note", help="为什么（决策号，比如“D97 第 2 条”），合并、不是同一个、撤销、拆开都要写")
    entities.add_argument("--sync", action="store_true", help="把合并记录里还没在这本账上生效的补上（整本重建以后、中途断了）")
    entities.add_argument("--history", action="store_true", help="合并记录，按先后，带上在这本账上生效了没有")
    entities.add_argument("--store", help="L3 结果库的目录，默认数据目录下的 l3（合并从这里重写窗口）")
    entities.add_argument("--vectors", help="向量缓存的目录，默认数据目录下的 vectors（没有就只用账本里原来的向量）")
    entities.add_argument("--json", action="store_true", help="输出 JSON")
    entities.set_defaults(func=_cmd_entities)

    export = commands.add_parser("export-facts", help="事实导出：一行一条 JSON，带来源和信任的标记（有原文，写到仓库外）")
    export.add_argument("--library", choices=("prod", "test"), required=True, help="哪个库")
    export.add_argument("--out", required=True, help="写到这个文件（JSONL）")
    export.add_argument("--packets", help="每个包的来源、原文句按 origin 和说话人的数，写到这个文件（JSONL）")
    export.add_argument("--hidden", action="store_true", help="藏起来的事实（证据都撤回了）也导出")
    export.set_defaults(func=_cmd_export_facts)

    score_cmd = commands.add_parser("score", help="打分：拿金标准给一边的抽取结果打分（J3.8，不调模型，输出放仓库外）")
    from javmem.scoring.run import add_arguments as _score_arguments

    _score_arguments(score_cmd)
    score_cmd.set_defaults(func=_cmd_score)

    soft_cmd = commands.add_parser("soft", help="软变化判定（J4.5）：find 找候选、judge 调 Claude 判、score 对答案")
    from javmem.soft.cli import add_arguments as _soft_arguments

    _soft_arguments(soft_cmd)
    soft_cmd.set_defaults(func=_cmd_soft)

    side_cmd = commands.add_parser("side", help="Graphiti 支线（第 6 步）：本机 Qwen 抽、结果只进体检库，导出打分、扫一遍")
    from javmem.side.cli import add_arguments as _side_arguments

    _side_arguments(side_cmd)
    side_cmd.set_defaults(func=_cmd_side)

    graph_cmd = commands.add_parser("graph", help="主图（第 5 步）：接上图、同步、整张重建、导出、比对")
    from javmem.graph.cli import add_arguments as _graph_arguments

    _graph_arguments(graph_cmd)
    graph_cmd.set_defaults(func=_cmd_graph)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台默认不是 UTF-8，打印中文前先改
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    try:
        return args.func(args, Settings.from_env())
    except (ConfigError, LedgerError, sqlite3.Error, OSError) as error:  # OSError：数据目录用不了
        _print_error(error)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
