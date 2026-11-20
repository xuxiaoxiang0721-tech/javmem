"""L4 的模型复核（D63）：规则（G1 到 G7）过了的事实，再让模型看一遍原文支不支持。

规则只能兜底：G7 只看事实文字有多少在窗口里看得到，原文只有“80%/12.5%”、事实写成“屏障 80%”这种一个词没着落的
放过了。所以过了规则的事实，一个窗口发一次，让模型逐条判：

- `supported`：事实说的每一点，正文里都说了，或者照字面就能得出。照常往下走。
- `partial`：主干有，但有一点正文没说、或者对不上。进隔离表，原因 `G7:model_partial`。
- `unsupported`：正文没说这件事，或者说反了。进隔离表，原因 `G7:model_unsupported`。

部分支持也隔离：放过的话，“屏障 80%”这种就挡不住；隔离表留着 L3 的原样、原因和模型说的问题，原文层也都在。

**发什么：** L3 当时看到的窗口文字（`render_window` 加打码模式表，就是 L3 发出去的那段），后面接【要核的事实】：
这个窗口过了规则的事实，按 L3 的顺序编 F1、F2……，一行一条，带上确定程度和过了 G1 的证据行，有属性、值的再接
〔属性〕〔值〕（查询时它们也交给 Bot）。事实文字、属性、值的换行换成空格、〔〕换成［］，伪造不了别的行和标注；
再按打码模式表打一遍，还扫得出来的不发（`unredacted`）。private、打码没打干净的窗口 L4 本来就不核（`NOT_SENT`），
这里也再查一遍，不发、不写；没有过了规则的事实的窗口不调。

**什么时候调、什么时候用：** 调模型只在 `javmem extract --recheck`（L3 之后，同一把结果库的锁、同一个预算标签）。
`javmem write --recheck`、`javmem verify --recheck` 只按键取存下的判定（apply_rechecks），不调模型；
过了规则却还没有判定的窗口，check 换成 None、skipped 是 NO_RECHECK，L6 不写它，包留着排队。
不加 `--recheck` 的和以前一样只用规则。

**键和存法：** 结果存在 L3 的结果库里（javmem.pipeline.store 的 rechecks、recheck_windows、recheck_calls），
键是“窗口文字、事实那一段、提示词版本、模型”（recheck_key），同样的输入不再调。撤回删窗口时一起删。

**调用的规矩和 L3 一样**（extract._Run）：同时最多 2 个请求，限流退避，预算按标签算，每次调用记账
（purpose 是 l4_recheck），超时只再试一次，格式错重试一次，截断、拒答、格式错过的默认不再调
（--retry-failed-recheck 才重调；--retry-failed 只管 L3）。

**核的方式记在账本里**（checked_by）：只用规则是 L4_VERSION；加了复核是 L4_VERSION 加提示词版本加模型。
说法的 l4_version、隔离表的 rule_version 写的就是它，写过的窗口按它判要不要重写。

提示词在 prompts/ 下，带版本号；改了提示词或输出结构要换版本号，也要改 RECHECK_PROMPT_SHA256。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from importlib import resources

from javmem.llm import ModelReply, Usage
from javmem.pipeline.extract import (
    CACHED,
    DONE,
    FAILED,
    NOT_STARTED,
    ExtractOptions,
    FormatError,
    Model,
    RunResult,
    WindowOutcome,
    _now,
    _Run,
)
from javmem.pipeline.render import escape_raw
from javmem.pipeline.store import ResultStore, strip_known_relations
from javmem.pipeline.verify import L4_VERSION, NO_RECHECK, NOT_SENT, RULE_ORDER, Checked, FactCheck, Finding
from javmem.pipeline.window import Window
from javmem.redaction import REDACTION_TYPES, RedactionPattern, redact, scan_types
from javmem.usage import TokenLedger

RECHECK_PROMPT_VERSION = "g7-v2"
RECHECK_PROMPT_SHA256 = "bef041a51e01394723ed0dec52284379751880ba8886cadd8ba0ec83f825d9cf"
RECHECK_SCHEMA_NAME = "javmem_g7_v2"
PURPOSE = "l4_recheck"

SUPPORTED = "supported"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"
VERDICTS = (SUPPORTED, PARTIAL, UNSUPPORTED)
CODES = {PARTIAL: "model_partial", UNSUPPORTED: "model_unsupported"}  # G7 的新原因

NOTHING = "nothing"  # 没有过了规则的事实，不用调
SKIPPED = "skipped"  # L4 核不了这个窗口：还没有 L3 的结果、L3 没发的、核的时候出错的

FACTS_HEADING = "【要核的事实】"
PROBLEM_CHARS = 200  # 模型说的问题最多存几个字：提示词让它写 30 个字以内

RECHECK_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "verdict", "problem"],
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "problem": {"type": "string"},
                },
            },
        }
    },
}


def load_recheck_prompt(version: str = RECHECK_PROMPT_VERSION) -> str:
    text = (resources.files("javmem.pipeline") / "prompts" / f"{version}.md").read_text(encoding="utf-8")
    return text.replace("\r\n", "\n")


def recheck_fingerprint(version: str = RECHECK_PROMPT_VERSION) -> str:
    """提示词连同输出结构的指纹。"""
    text = load_recheck_prompt(version) + "\n" + json.dumps(RECHECK_SCHEMA, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recheck_key(window_sha: str, facts_sha: str, prompt_version: str, model: str) -> str:
    """复核结果的键：窗口文字（L3 发出去的那段）、事实那一段、提示词版本、模型（名字@档位/输出方式）。"""
    return _sha256("\n".join(("javmem-recheck", window_sha, facts_sha, prompt_version, model)))


def checked_by(model: str | None) -> str:
    """账本里记的核的方式：只用规则是 L4_VERSION；加了复核再写上提示词版本和模型。"""
    return L4_VERSION if model is None else f"{L4_VERSION} {RECHECK_PROMPT_VERSION} {model}"


# ---------- 发什么 ----------


@dataclass(frozen=True)
class RecheckRequest:
    window_id: str
    l3_key: str  # 这个窗口 L3 结果的键：撤回按它找到复核的结果
    text: str  # 发给模型的：窗口文字，加上【要核的事实】
    window_sha: str
    facts_sha: str
    ids: tuple[tuple[str, int], ...]  # (F 编号, 这条事实在 L3 结果里是第几条)
    redactions: dict[str, int]  # 事实那一段又打了几处码
    leftover: tuple[str, ...] = ()  # 事实那一段打完码还扫得出来的类型：不发

    def key(self, model: str, prompt_version: str = RECHECK_PROMPT_VERSION) -> str:
        return recheck_key(self.window_sha, self.facts_sha, prompt_version, model)

    @property
    def fact_ids(self) -> list[str]:
        return [fact_id for fact_id, _ in self.ids]


def fact_line(number: int, fact: FactCheck, patterns: Sequence[RedactionPattern]) -> tuple[str, dict[str, int]]:
    """一条事实写成一行：F 编号（确定程度，证据 T…）事实文字，有属性、值的再接〔属性〕…〔值〕…
    （查询时属性和值也交给 Bot，“屏障 80%”写进属性里一样要挡）。换行换成空格、〔〕换成［］（伪造不了别的行和
    这两个标注），再打一遍码。"""
    raw = fact.raw
    refs = "、".join(dict.fromkeys(proof.ref for proof in fact.proofs))
    certainty = escape_raw(str(raw.get("certainty") or ""))[:20]
    counts: dict[str, int] = {}

    def clean(value: object) -> str:
        text, hits = redact(escape_raw(str(value or "")).strip(), patterns)
        for kind, hit in hits.items():
            counts[kind] = counts.get(kind, 0) + hit
        return text

    line = f"F{number}（{certainty}，证据 {refs}）{clean(raw.get('text'))}"
    for label, name in (("属性", "attribute"), ("值", "value")):
        part = clean(raw.get(name))
        if part:
            line += f" 〔{label}〕{part}"
    return line, counts


def _unsendable(item: Checked) -> bool:
    """L3 不发的窗口（private 的、打码没打干净的），复核也不发：不全靠 verify_windows 先标了 NOT_SENT。"""
    return item.window.scope.private or bool(item.rendered.leftover)


def build_request(item: Checked, patterns: Sequence[RedactionPattern]) -> RecheckRequest | None:
    """这个窗口要发去复核的：过了规则的事实。L4 没核这个窗口、L3 不发的窗口、一条都没过的，返回 None。"""
    check = item.check
    if check is None or _unsendable(item):
        return None
    passed = [fact for fact in check.facts if fact.passed]
    if not passed:
        return None
    rows: list[str] = []
    counts: dict[str, int] = {}
    ids: list[tuple[str, int]] = []
    for number, fact in enumerate(passed, start=1):
        row, hits = fact_line(number, fact, patterns)
        rows.append(row)
        ids.append((f"F{number}", fact.index))
        for kind, hit in hits.items():
            counts[kind] = counts.get(kind, 0) + hit
    block = FACTS_HEADING + "\n" + "\n".join(rows) + "\n"
    found = {kind for row in rows for kind in scan_types(row, patterns)}
    return RecheckRequest(
        window_id=item.window.window_id,
        l3_key=item.cache_key,
        # 抽之前放进去的现有关系不是原文：复核只看原文支不支持，不发这一段（没放的窗口一字不变）
        text=strip_known_relations(item.rendered.text) + "\n" + block,
        window_sha=item.rendered.sha256,
        facts_sha=_sha256(block),
        ids=tuple(ids),
        redactions=counts,
        leftover=tuple(kind for kind in REDACTION_TYPES if kind in found),
    )


# ---------- 校验 ----------


def parse_verdicts(content: str, fact_ids: Sequence[str]) -> dict[str, object]:
    """模型的回复：是 JSON、字段对、每个发过去的编号正好判一次。不对算格式错误，消息里不抄回复的内容。"""
    try:
        data = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        raise FormatError("回复不是 JSON") from None
    if not isinstance(data, dict) or set(data) != {"verdicts"} or not isinstance(data["verdicts"], list):
        raise FormatError("回复的字段不对")
    found: dict[str, dict[str, str]] = {}
    for entry in data["verdicts"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "verdict", "problem"}:
            raise FormatError("verdicts 里一项的字段不对")
        if not all(isinstance(entry[name], str) for name in ("id", "verdict", "problem")):
            raise FormatError("verdicts 里一项的类型不对")
        if entry["verdict"] not in VERDICTS:
            raise FormatError("verdict 的取值不在范围里")
        fact_id = entry["id"].strip()
        if fact_id in found:
            raise FormatError("同一个编号判了两次")
        found[fact_id] = {"verdict": entry["verdict"], "problem": entry["problem"].strip()[:PROBLEM_CHARS]}
    if set(found) != set(fact_ids):
        raise FormatError("判的编号和发过去的对不上")
    parsed = {"verdicts": [{"id": fact_id, **found[fact_id]} for fact_id in fact_ids]}
    try:
        json.dumps(parsed, ensure_ascii=False).encode("utf-8")  # 转义写出来的孤立代理写不进库
    except UnicodeEncodeError:
        raise FormatError("回复里有写不进库的字符") from None
    return parsed


# ---------- 用存下的判定 ----------


def _verdicts_of(parsed: object) -> dict[str, tuple[str, str]] | None:
    """存下的判定：F 编号 → (判定, 问题)。结构不对的返回 None。"""
    entries = parsed.get("verdicts") if isinstance(parsed, Mapping) else None
    if not isinstance(entries, list):
        return None
    found: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("verdict") not in VERDICTS:
            return None
        found[str(entry.get("id"))] = (str(entry["verdict"]), str(entry.get("problem") or ""))
    return found


MAX_RECHECK_CHARS = ExtractOptions().max_input_chars  # 复核要发的那一段超过这么多字，run_recheck 不发（too_big）


def apply_rechecks(items: Sequence[Checked], store: ResultStore, model: str, patterns: Sequence[RedactionPattern]) -> list[Checked]:
    """按键取存下的判定，partial、unsupported 的事实加上 G7 的原因（进隔离表）。不调模型。

    过了规则、却还没有判定的窗口（还没复核、复核失败了、事实那一段发不出去），check 换成 None，skipped 是
    NO_RECHECK：L6 不写它，包留着排队。不改传进来的对象。
    """
    out: list[Checked] = []
    for item in items:
        if item.check is not None and _unsendable(item):  # L3 不发的窗口带着规则的结果：不写，和 NOT_SENT 一样
            out.append(replace(item, check=None, skipped=NOT_SENT, recheck_key=None))
            continue
        request = build_request(item, patterns)
        if request is None:
            out.append(item)
            continue
        if request.leftover:
            out.append(replace(item, check=None, skipped=NO_RECHECK, recheck_key=None, recheck_unsent="unredacted"))
            continue
        key = request.key(model)
        stored = store.recheck(key)
        verdicts = _verdicts_of(stored.parsed) if stored is not None else None
        if verdicts is None or any(fact_id not in verdicts for fact_id in request.fact_ids):
            # 还没复核、复核失败了，或者存下的判定对不上（手改过的库）：都当没有判定，不写。太大的再跑也发不出去（排队修）
            unsent = "too_big" if stored is None and len(request.text) > MAX_RECHECK_CHARS else None
            out.append(replace(item, check=None, skipped=NO_RECHECK, recheck_key=key, recheck_unsent=unsent))
            continue
        index_to_id = {index: fact_id for fact_id, index in request.ids}
        assert item.check is not None
        facts: list[FactCheck] = []
        for fact in item.check.facts:
            fact_id = index_to_id.get(fact.index)
            if fact_id is None:  # 没过规则的，没发去复核
                facts.append(fact)
                continue
            verdict, problem = verdicts[fact_id]
            failures = list(fact.failures)
            if verdict in CODES:
                failures.append(Finding("G7", CODES[verdict]))
                failures.sort(key=lambda finding: RULE_ORDER.index(finding.rule))
            facts.append(replace(fact, failures=failures, recheck={"verdict": verdict, "problem": problem}))
        check = replace(item.check, facts=facts)  # 只换事实，实体、关系的核对（J4.3）照旧
        out.append(replace(item, check=check, recheck_key=key))
    return out


# ---------- 调模型 ----------


class _RecheckRun(_Run):
    """L3 那一套调用的规矩，换掉输出结构、解析、存结果、记调用这几处。"""

    purpose = PURPOSE
    schema = RECHECK_SCHEMA
    schema_name = RECHECK_SCHEMA_NAME

    def __init__(
        self,
        model: Model,
        store: ResultStore,
        ledger: TokenLedger | None,
        options: ExtractOptions,
        log: Callable[[str], None] | None,
    ) -> None:
        super().__init__(model, store, ledger, options, log, system=load_recheck_prompt())

    def parse(self, content: str, request: object) -> dict[str, object]:
        assert isinstance(request, RecheckRequest)
        return parse_verdicts(content, request.fact_ids)

    def save(self, window: Window, request: object, key: str, reply: ModelReply, parsed: Mapping[str, object], usage: Usage) -> None:
        assert isinstance(request, RecheckRequest)
        self.store.save_recheck(
            recheck_key=key,
            window_sha=request.window_sha,
            facts_sha=request.facts_sha,
            prompt_version=RECHECK_PROMPT_VERSION,
            model=self.model.cache_name,
            output=reply.content,
            parsed=parsed,
            response_model=reply.model,
            usage=usage,
            seconds=reply.seconds,
            created_at=_now(),
            window_id=window.window_id,
            cache_key=request.l3_key,
            facts=request.ids,
        )

    def record_call(self, row: Mapping[str, object]) -> None:
        self.store.record_recheck_call(row)


def run_recheck(
    items: Sequence[Checked],
    model: Model,
    store: ResultStore,
    *,
    patterns: Sequence[RedactionPattern],
    ledger: TokenLedger | None = None,
    options: ExtractOptions = ExtractOptions(),
    log: Callable[[str], None] | None = None,
) -> RunResult:
    """items 是 verify_windows 的结果（只用规则）。存过的直接用，L4 没核的、没有过了规则的事实的不调，其余的调模型。

    这一批里窗口文字和事实都一样的几个窗口只调一次。Ctrl+C 或者别的意外：不再开始新的调用，正在跑的跑完。
    """
    run = _RecheckRun(model, store, ledger, options, log)
    store.start_run(run.run_id, _now(), run.run_options(purpose=PURPOSE, model=model.cache_name, prompt=RECHECK_PROMPT_VERSION))
    outcomes: dict[str, WindowOutcome] = {}
    todo: list[tuple[Window, RecheckRequest, str]] = []
    followers: dict[str, list[tuple[Window, RecheckRequest]]] = {}
    leaders: set[str] = set()
    try:
        for item in items:
            window_id = item.window.window_id
            if item.check is None or _unsendable(item):
                detail = str(item.skipped or "") if item.check is None else NOT_SENT
                outcomes[window_id] = WindowOutcome(window_id, "", SKIPPED, detail=detail)
                continue
            request = build_request(item, patterns)
            if request is None:
                outcomes[window_id] = WindowOutcome(window_id, "", NOTHING)
                continue
            key = request.key(model.cache_name)
            if request.leftover:  # 事实那一段打完码还扫得出来：不发
                outcomes[window_id] = WindowOutcome(window_id, key, FAILED, detail="unredacted")
                continue
            if store.recheck(key) is not None:
                store.link_recheck(window_id, request.l3_key, key, request.ids, _now())
                outcomes[window_id] = WindowOutcome(window_id, key, CACHED)
                continue
            failed_before = None if options.retry_failed else store.last_recheck_failure(key)
            if failed_before is not None:
                outcomes[window_id] = WindowOutcome(window_id, key, FAILED, detail=f"failed_before: {failed_before}")
                continue
            if key in leaders:
                followers.setdefault(key, []).append((item.window, request))
                outcomes[window_id] = WindowOutcome(window_id, key, NOT_STARTED, detail="same_text")
                continue
            if len(request.text) > options.max_input_chars:
                outcomes[window_id] = WindowOutcome(window_id, key, FAILED, detail="too_big")
                continue
            if options.limit is not None and len(todo) >= options.limit:
                outcomes[window_id] = WindowOutcome(window_id, key, NOT_STARTED, detail="limit")
                continue
            leaders.add(key)
            todo.append((item.window, request, key))

        pool = ThreadPoolExecutor(max_workers=max(1, options.concurrency))
        try:
            futures = [pool.submit(run.work, *job) for job in todo]
            for future in futures:
                outcome = future.result()
                outcomes[outcome.window_id] = outcome
                run.progress(outcome, len(todo))
        except BaseException:
            run.halt("interrupted")
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True)

        for key, waiting in followers.items():
            if store.recheck(key) is None:
                continue
            for window, request in waiting:
                store.link_recheck(window.window_id, request.l3_key, key, request.ids, _now())
                outcomes[window.window_id] = WindowOutcome(window.window_id, key, CACHED)
    finally:
        counts = {
            status: sum(1 for outcome in outcomes.values() if outcome.status == status)
            for status in (CACHED, DONE, FAILED, NOT_STARTED, NOTHING, SKIPPED)
        }
        store.finish_run(run.run_id, _now(), {"stopped": run.stopped, **counts})
    return RunResult(run.run_id, [outcomes[item.window.window_id] for item in items], run.stopped)


# ---------- 数 ----------


def plan_recheck(
    items: Sequence[Checked],
    store: ResultStore,
    model: str,
    patterns: Sequence[RedactionPattern],
    *,
    max_input_chars: int,
    retry_failed: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """--dry-run 用：有几个窗口要复核、存过的几个、要调几个、多少字。不调模型，不要密钥。"""
    plan = {"windows": 0, "cached": 0, "failed_before": 0, "unsent": 0, "to_call": 0, "facts": 0, "chars": 0, "skipped": 0}
    leaders: set[str] = set()
    for item in items:
        if item.check is None or _unsendable(item):
            plan["skipped"] += 1
            continue
        request = build_request(item, patterns)
        if request is None:
            continue
        plan["windows"] += 1
        key = request.key(model)
        if request.leftover or len(request.text) > max_input_chars:
            plan["unsent"] += 1
        elif store.recheck(key) is not None or key in leaders:
            plan["cached"] += 1
        elif not retry_failed and store.last_recheck_failure(key) is not None:
            plan["failed_before"] += 1
        elif limit is None or plan["to_call"] < limit:
            leaders.add(key)
            plan["to_call"] += 1
            plan["facts"] += len(request.ids)
            plan["chars"] += len(request.text)
    return plan


def summarize_recheck(
    items: Sequence[Checked], store: ResultStore, model: str, patterns: Sequence[RedactionPattern]
) -> dict[str, object]:
    """这一批复核的数：要核几条事实、核过几条、判成什么、每个窗口的 token 和秒数、格式错误率。不抄原文。

    items 是 verify_windows 的结果（只用规则）。窗口文字和事实都一样的几个窗口共用一个键，调用和 token 只算在第一个上。
    """
    verdicts = {verdict: 0 for verdict in VERDICTS}
    per_window: dict[str, dict[str, object]] = {}
    totals = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0, "seconds": 0.0}
    requests: list[tuple[str, RecheckRequest, str]] = []
    skipped = nothing = unsent = 0
    for item in items:
        if item.check is None or _unsendable(item):
            skipped += 1
            continue
        request = build_request(item, patterns)
        if request is None:
            nothing += 1
            continue
        if request.leftover:
            unsent += 1
            continue
        requests.append((item.window.window_id, request, request.key(model)))
    calls = store.recheck_calls({key for _, _, key in requests})
    by_key: dict[str, list[Mapping[str, object]]] = {}
    statuses: dict[str, int] = {}
    for row in calls:
        by_key.setdefault(str(row["cache_key"]), []).append(row)
        statuses[str(row["status"])] = statuses.get(str(row["status"]), 0) + 1
    answered = sum(count for status, count in statuses.items() if status.split("_estimated")[0] in ("ok", "format_error", "length"))
    format_errors = statuses.get("format_error", 0) + statuses.get("format_error_estimated", 0)
    done = missing = facts_to_check = facts_checked = 0
    counted: set[str] = set()
    for window_id, request, key in requests:
        stored = store.recheck(key)
        rows = by_key.get(key, []) if key not in counted else []
        counted.add(key)
        usage = {name: sum(row[name] for row in rows) for name in totals}  # type: ignore[misc]
        for name in totals:
            totals[name] += usage[name]  # type: ignore[operator]
        entry: dict[str, object] = {"facts": len(request.ids), "calls": len(rows), **usage}
        facts_to_check += len(request.ids)
        if stored is None:
            missing += 1
            entry["result"] = False
        else:
            done += 1
            facts_checked += len(request.ids)
            entry["result"] = True
            window_verdicts = {verdict: 0 for verdict in VERDICTS}
            for verdict, _ in (_verdicts_of(stored.parsed) or {}).values():
                window_verdicts[verdict] += 1
                verdicts[verdict] += 1
            entry.update(window_verdicts)
        entry["seconds"] = round(float(entry["seconds"]), 1)  # type: ignore[arg-type]
        per_window[window_id] = entry
    return {
        "prompt_version": RECHECK_PROMPT_VERSION,
        "model": model,
        "windows": len(items),
        "windows_rechecked": done,
        "windows_without_recheck": missing,
        "windows_nothing_to_check": nothing,
        "windows_not_checked_by_l4": skipped,
        "windows_not_sent_unredacted": unsent,
        "facts_to_check": facts_to_check,  # 过了规则、要发去复核的
        "facts_checked": facts_checked,  # 其中已经有判定的
        "verdicts": verdicts,
        "calls": len(calls),
        "call_statuses": statuses,
        "format_error_rate": round(format_errors / answered, 4) if answered else None,
        "retries": len(calls) - len(by_key),
        "tokens": {name: round(value, 1) if name == "seconds" else int(value) for name, value in totals.items()},
        "per_window": dict(sorted(per_window.items())),
    }
