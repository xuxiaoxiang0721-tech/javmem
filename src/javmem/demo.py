"""编的演示（U3）：`javmem demo --to 新的空目录`。不要密钥、不调模型、不连外面，一两分钟走完一整圈：
收包 → 写账 → 五个查询工具各问一句 → 拿编的金标准打分 → 不用金标准的四个数 → 撤回一个包再问 → 彻底删除。

演示数据就是 GitHub 那一层回归用的编的小套件（仓库里的 `tests/fixtures/regress-mini/`，D205 ①）：一个编的主人、一个编的
Bot、5 个对话包（报价改了、开幕推迟、负责人换了，任职、亲属、持有、托管、简称），28 条编的金标准，10 道编的题，加上第一次
用真模型抽这 5 个包时录下的回复（`l3-replies.json`）。照录好的回复建 L3 结果库，所以不调模型。问哪几句、撤回哪个包写在
同一个目录的 `demo.json` 里（回归的套件不读它）。从仓库装的（`pip install -e .`）才有这份数据；别处装的，用 `--from`
指一份同样格式的目录。

- **子进程的环境：** 去掉所有 `JAVMEM_*`，只给演示的配置（来源登记、身份、主人的名字、别名表、时区、录回复时那条线的
  提示词和模型的名字）。模型、向量服务、图的地址和密钥一样不给：用不着，也就不会往外发东西、不会连到这台机器上别的
  Neo4j。写账用假向量，主图那一路关着（`JAVMEM_GRAPH_SYNC=off`、`JAVMEM_GRAPH_SEARCH=off`）。
- **输出：** 每条命令的完整输出写进 `--to/steps.log`；屏幕上打每一步做了什么、几条、主要的分数（演示数据都是编的，照打）。
  最后写一份 `--to/demo.json`（只有数：每一步几条、分数、对不上的几处、付钱的几行）。
- **`--to`：** 要是新的空目录（还没有的也行），在仓库外（数据目录、日志不进仓库）。
- **退出码：** 0 走完；1 走完了，可有一步的结果不对（比如撤回以后还搜得到那一句，或者记账里有付钱的行）；
  2 跑不了（目录不对、演示数据不全、某条命令出错，看 `steps.log`）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

EXIT_OK, EXIT_WRONG, EXIT_CANNOT = 0, 1, 2
FORMAT = "javmem-demo/1"
LIBRARY = "test"  # 演示的来源只投测试库
# 子进程拿到的 JAVMEM_*：只有这几样，再加上演示配置 `line_env` 写着的提示词、模型的名字
DEMO_KEYS = ("JAVMEM_DATA_DIR", "JAVMEM_SOURCES_FILE", "JAVMEM_ACCESS_FILE", "JAVMEM_OWNER_FILE", "JAVMEM_LEXICON_FILE",
             "JAVMEM_TIMEZONE", "JAVMEM_TOKEN_LEDGER", "JAVMEM_GRAPH_SYNC", "JAVMEM_GRAPH_SEARCH")
LINE_KEYS = ("JAVMEM_L3_PROMPT", "JAVMEM_LLM_MODEL")  # 录回复时那条线的名字：结果库照它们认窗口，不是密钥、不是地址
LOCAL_MODELS = ("qwen", "fake")  # 记账里这几种开头的模型不花钱（和 javmem regress 一样）
CONFIG_FILES = ("sources.yaml", "access.yaml", "owner.yaml", "lexicon.yaml")

Out = Callable[[str], None]


class CannotRun(Exception):
    """跑不了：目录不对、演示数据不全、某条命令出错。"""


def default_source() -> Path:
    """从仓库装的：仓库里那份编的小套件。"""
    import javmem

    return Path(javmem.__file__).resolve().parents[2] / "tests" / "fixtures" / "regress-mini"


def _src_dir() -> Path:
    import javmem

    return Path(javmem.__file__).resolve().parents[1]


def load_plan(source: Path) -> dict[str, object]:
    """读演示数据目录里的 demo.json，顺带核要用的几样都在。"""
    path = source / "demo.json"
    if not path.is_file():
        raise CannotRun(f"{source} 里没有 demo.json，不是演示数据的目录（从仓库装的，演示数据在仓库的 "
                        "tests/fixtures/regress-mini/；别处装的用 --from 指一份）")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CannotRun(f"{path} 读不了：{error}") from None
    if not isinstance(plan, dict) or plan.get("format") != FORMAT:
        raise CannotRun(f"{path} 不是 {FORMAT}")
    for name in ("packets", "gold"):
        if not (source / name).is_dir():
            raise CannotRun(f"{source} 里没有 {name}/")
    if not (source / "l3-replies.json").is_file():
        raise CannotRun(f"{source} 里没有 l3-replies.json（录好的模型回复）")
    for name in CONFIG_FILES:
        if not (source / "config" / name).is_file():
            raise CannotRun(f"{source} 里没有 config/{name}")
    if not isinstance(plan.get("ask"), list) or not isinstance(plan.get("withdraw"), dict) or not plan.get("caller"):
        raise CannotRun(f"{path} 少了 ask、withdraw 或者 caller")
    return plan


def demo_env(config: Path, data: Path, ledger: Path, line_env: Mapping[str, str], timezone: str,
             base: Mapping[str, str] | None = None) -> dict[str, str]:
    """子进程的环境：外面的 JAVMEM_* 一个都不带，只给演示的配置。"""
    base = os.environ if base is None else base
    env = {key: value for key, value in base.items() if not key.startswith("JAVMEM_")}
    env.update({
        "JAVMEM_DATA_DIR": str(data),
        "JAVMEM_SOURCES_FILE": str(config / "sources.yaml"),
        "JAVMEM_ACCESS_FILE": str(config / "access.yaml"),
        "JAVMEM_OWNER_FILE": str(config / "owner.yaml"),
        "JAVMEM_LEXICON_FILE": str(config / "lexicon.yaml"),
        "JAVMEM_TIMEZONE": timezone,
        "JAVMEM_TOKEN_LEDGER": str(ledger),
        "JAVMEM_GRAPH_SYNC": "off",
        "JAVMEM_GRAPH_SEARCH": "off",
    })
    for key, value in line_env.items():
        if key not in LINE_KEYS:
            raise CannotRun(f"演示配置里 line_env 只收 {'、'.join(LINE_KEYS)}，不收 {key}（密钥、地址不给演示）")
        env[key] = str(value)
    env.update(PYTHONPATH=str(_src_dir()), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return env


def _javmem(env: Mapping[str, str], log: Path, *args: str) -> tuple[int, str]:
    """跑一条 javmem 命令，完整的输出写进 steps.log，返回 (退出码, 标准输出)。"""
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-X", "utf8", "-m", "javmem", *args], env=dict(env), cwd=str(log.parent),
                          capture_output=True, encoding="utf-8", errors="replace")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ javmem {' '.join(args)} -> {done.returncode}（{time.monotonic() - started:.1f} 秒）\n"
                     f"{done.stdout}\n--- stderr ---\n{done.stderr}\n")
    return done.returncode, done.stdout


def _must(env: Mapping[str, str], log: Path, *args: str) -> str:
    code, text = _javmem(env, log, *args)
    if code != 0:
        raise CannotRun(f"javmem {args[0]} 退出码 {code}（看 {log}）")
    return text


def _count(db: Path, sql: str) -> int:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def _short(text: object, width: int = 60) -> str:
    value = " ".join(str(text).split())
    return value if len(value) <= width else value[: width - 1] + "…"


def _share(rate: object) -> str:
    return f"{float(rate):.0%}" if isinstance(rate, (int, float)) else "-"


def summarize(tool: str, found: Mapping[str, object]) -> str:
    """一个工具的返回 → 一句话：查到几条、第一条是什么。"""
    if not found.get("ok", True):
        error = found.get("error") or {}
        return f"出错：{error.get('code') if isinstance(error, Mapping) else error}"
    result = found.get("result")
    if result == "none":
        return "查不到（result: none）"
    if result == "ambiguous":
        return f"名字对到不止一个实体：{len(found.get('candidates') or [])} 个候选"
    if tool == "entity_card":
        entity = found.get("entity") or {}
        name = entity.get("name") if isinstance(entity, Mapping) else entity
        parts = [f"{name}：别名 {len(found.get('aliases') or [])} 个、当前关系 {len(found.get('relations') or [])} 条、"
                 f"最近的事 {len(found.get('recent') or [])} 条"]
        relations = found.get("relations") or []
        if relations and isinstance(relations[0], Mapping):
            parts.append(f"第一条关系：{_short(relations[0].get('content'), 40)}")
        return "；".join(parts)
    items = found.get("items") or []
    if not items:
        return "查到了，可一条结果都没有"
    first = items[0] if isinstance(items[0], Mapping) else {}
    return f"{len(items)} 条，第一条：{_short(first.get('content'))}"


def _ask(env: Mapping[str, str], log: Path, caller: str, ask: Mapping[str, object]) -> dict[str, object]:
    tool = str(ask["tool"])
    arguments = json.dumps(ask["arguments"], ensure_ascii=False)
    code, text = _javmem({**env, "JAVMEM_CALLER": caller}, log, "query", tool, arguments)
    try:
        found = json.loads(text)
    except ValueError:
        raise CannotRun(f"javmem query {tool} 退出码 {code}，输出不是 JSON（看 {log}）") from None
    if not isinstance(found, dict):
        raise CannotRun(f"javmem query {tool} 回的不是 {{…}} 那样的返回（看 {log}）")
    return found


def _paid_rows(ledger: Path) -> int:
    """记账里付钱的几行（和 javmem regress 一样：模型名是 qwen、fake 开头的算本机、假的）。"""
    from javmem.usage import TokenLedger

    if not ledger.exists():
        return 0
    return sum(1 for row in TokenLedger(ledger).rows() if not (row.get("model") or "").lower().startswith(LOCAL_MODELS))


def _check_target(to: Path) -> None:
    from javmem.scoring.run import inside_repo

    if inside_repo(to):
        raise CannotRun(f"{to} 在仓库里：--to 要在仓库外（数据目录、日志不进仓库）")
    if to.exists() and (not to.is_dir() or any(to.iterdir())):
        raise CannotRun(f"{to} 不是新的空目录：--to 要是新的空目录（还没有的也行）")


def _demo(source: Path, plan: Mapping[str, object], to: Path, out: Out) -> int:
    started = time.monotonic()
    to.mkdir(parents=True, exist_ok=True)
    config = to / "config"
    config.mkdir()
    for name in CONFIG_FILES:
        shutil.copy2(source / "config" / name, config / name)
    data, log, ledger = to / "data", to / "steps.log", to / "token-ledger.csv"
    line_env = plan.get("line_env") or {}
    if not isinstance(line_env, Mapping):
        raise CannotRun("演示配置里 line_env 要写成 {名字: 值}")
    env = demo_env(config, data, ledger, line_env, str(plan.get("timezone") or "+08:00"))
    caller = str(plan["caller"])
    record: dict[str, object] = {"format": FORMAT, "source": str(source), "wrong": 0}
    wrong: list[str] = []
    out(f"演示：数据目录 {data}；每一步的命令和完整输出在 {log}")

    # 1. 收包
    _must(env, log, "init")
    packets = sorted((source / "packets").glob("*/*.md"))
    for packet in packets:
        target = data / f"inbox-{LIBRARY}" / packet.parent.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(packet, target / packet.name)
    _must(env, log, "intake")
    db = data / "ledger" / f"{LIBRARY}.db"
    received = _count(db, "SELECT COUNT(DISTINCT packet_id) FROM packets")
    record["packets"] = received
    out(f"1. 收包：{len(packets)} 个编的对话包投进测试库的收件目录，收进来 {received} 个")
    if received != len(packets):
        wrong.append(f"收进来 {received} 个，投了 {len(packets)} 个")

    # 2. 照录好的回复建结果库（不调模型）
    from javmem.regress.replies import build_store

    try:
        counts = build_store(source / "l3-replies.json", data / "l3")
    except ValueError as error:
        raise CannotRun(f"录好的回复对不上现在的代码：{error}") from None
    record["store_rows"] = sum(counts.values())
    out(f"2. 照录好的模型回复建 L3 结果库：{sum(counts.values())} 行（第一次用真模型抽这 5 个包时录的；这次不调模型）")

    # 3. 写账
    summary = to / "write-summary.json"
    _must(env, log, "write", "--library", LIBRARY, "--fake-embeddings", "--summary", str(summary), "--tag", "javmem-demo")
    facts = _count(db, "SELECT COUNT(*) FROM facts")
    relations = _count(db, "SELECT COUNT(*) FROM relations")
    entities = _count(db, "SELECT COUNT(*) FROM entities")
    record.update(facts=facts, relations=relations, entities=entities)
    out(f"3. 写账（核对、合并新旧、写进账本；向量用假的）：事实 {facts} 条、关系 {relations} 条、实体 {entities} 个")

    # 4. 五个查询工具
    out(f"4. 五个查询工具各问一句（身份 {caller}，看得到全部；主图那一路关着，只用账本视图和原文层）：")
    asked = []
    for ask in plan["ask"]:  # type: ignore[union-attr]
        found = _ask(env, log, caller, ask)
        asked.append({"tool": ask["tool"], "result": found.get("result")})
        out(f"   {ask['tool']}｜{ask.get('say', '')}：{summarize(str(ask['tool']), found)}")
    record["asked"] = asked

    # 5. 打分
    from javmem.regress.run import METRICS, _metric

    score_dir = to / "score"
    _must(env, log, "score", "--gold", str(source / "gold"), "--side", f"javmem={data}", "--library", LIBRARY,
          "--scores-only", "--out", str(score_dir))
    scores = json.loads((score_dir / "分数.json").read_text(encoding="utf-8"))
    shown = []
    record["scores"] = {}
    for path, name in METRICS:
        value = _metric(scores, path)
        record["scores"][name] = value  # type: ignore[index]
        shown.append(f"{name} {value:.0%}" if value is not None else f"{name} -")
    out(f"5. 拿编的金标准打分（28 条）：{'，'.join(shown)}")

    # 6. 不用金标准的四个数。第二遍是录好的回复再建一遍（两遍一字不差），建在数据目录外、算完就删：里面有包的原文
    second = to / "second-run"
    build_store(source / "l3-replies.json", second)
    checked_path = to / "selfcheck.json"
    try:
        _must(env, log, "selfcheck", "--library", LIBRARY, "--runs", str(data / "l3"), str(second), "--json", str(checked_path))
    finally:
        shutil.rmtree(second, ignore_errors=True)
    checked = json.loads(checked_path.read_text(encoding="utf-8"))
    four = {
        "runs_same": checked["runs"]["facts"]["same_rate"],
        "evidence_found": checked["evidence"]["facts"]["rate"],
        "merged_flagged": checked["merged"]["flagged"],
        "entities_in_proposals": checked["names"]["in_proposals"],
    }
    record["selfcheck"] = four
    out(f"6. 不用金标准的四个数（javmem selfcheck）：两遍抽的事实内容一样 {_share(four['runs_same'])}（录好的回复再放一遍，"
        f"一字不差；真模型跑两遍才看得出浮动），证据在原文里找得到 {_share(four['evidence_found'])}，"
        f"合在一起又对不上的事实 {four['merged_flagged']} 条，落在合并提议里的实体 {four['entities_in_proposals']} 个")

    # 7. 撤回一个包，再问
    withdraw = plan["withdraw"]
    packet_id = str(withdraw["packet"])  # type: ignore[index]
    _must(env, log, "withdraw", "--library", LIBRARY, "--packet", packet_id)
    out(f"7. {withdraw.get('say', '撤回 ' + packet_id)}（javmem withdraw；原文只标撤回、派生的事实和关系当场删掉）：")  # type: ignore[union-attr]
    for ask in withdraw.get("ask") or []:  # type: ignore[union-attr]
        found = _ask(env, log, caller, ask)
        out(f"   {ask['tool']}｜{ask.get('say', '')}：{summarize(str(ask['tool']), found)}")
        if ask.get("expect_none") and found.get("result") != "none":
            wrong.append(f"撤回以后 {ask['tool']} 还查得到")

    # 8. 彻底删除
    _must(env, log, "purge", "--library", LIBRARY, "--packet", packet_id, "--yes")
    left = [path for path in data.rglob(f"{packet_id}.md")]
    for ask in withdraw.get("ask") or []:  # type: ignore[union-attr]
        if ask.get("expect_none"):
            found = _ask(env, log, caller, ask)
            if found.get("result") != "none":
                wrong.append(f"彻底删除以后 {ask['tool']} 还查得到")
    if left:
        wrong.append(f"彻底删除以后数据目录里还有 {len(left)} 个这个包的文件")
    out(f"8. 彻底删除那个包（javmem purge）：数据目录里这个包的文件还剩 {len(left)} 个；再搜那一句："
        f"{'查不到' if not any('彻底删除以后' in one for one in wrong) else '还查得到'}")

    paid = _paid_rows(ledger)
    record["paid_rows"] = paid
    if paid:
        wrong.append(f"记账里有 {paid} 行付钱的模型")
    out(f"记账：付钱的 {paid} 行（演示不调付钱的模型）")
    record["wrong"] = len(wrong)
    record["seconds"] = round(time.monotonic() - started, 1)
    (to / "demo.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if wrong:
        out("演示走完了，可有几处不对：" + "；".join(wrong))
        return EXIT_WRONG
    out(f"演示做完（{record['seconds']} 秒）。数据目录、配置都在 {to}；照 README“自己试一试”那一节设好环境变量，"
        "就能接着用 javmem query 问。")
    return EXIT_OK


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", required=True, help="新的空目录（还没有的也行），在仓库外：数据目录、配置、日志都放这里")
    parser.add_argument("--from", dest="source", help="演示数据的目录，默认仓库里的 tests/fixtures/regress-mini/")


def run(args: argparse.Namespace, out: Out = print) -> int:
    try:
        source = Path(args.source) if args.source else default_source()
        plan = load_plan(source)
        to = Path(args.to)
        _check_target(to)
        return _demo(source, plan, to, out)
    except CannotRun as error:
        out(f"演示跑不了：{error}")
        return EXIT_CANNOT
