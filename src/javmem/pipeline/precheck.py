"""进图前检查（J5.3，D178、D22；设计稿 01 p4 的留痕表、02 p5 的四级、02 p6）。判断 C（javmem.pipeline.reljudge）之外管四件事：

1. **事实也判“原文支不支持”**，照判断 C 的两档（D178 ②“沿用判断 C 的做法”）：过了 G1 到 G7 的事实，一处说法（一个窗口
   里的一条）问本机模型一次（提示词 f1，prompts/f1.md），只回“是 / 否”，P(能) 照 C 的算法；sol 线（local+llm）没把握的交
   抽取模型答“能 / 不能 / 拿不准”。给的原话、键、存法、记账、线都和判断 C 一样，只换提示词和问话（FACTS）。
   - 有把握说不能的挡下进隔离表：rule_id 是 C，原因 local_unsupported、llm_unsupported（item_kind 是 fact）。
   - 第二档拿不准、答不上（llm_unsure、llm_failed）照判断 C 对关系的办法（J4.4）：写成说法，记成待确认 manual_review。
   - 关系 C 在 L4 已经判过，这里不再问。
2. **财务投资“判不了”的转人工**（D22；规矩题 1 甲，主会话 11-18 照建议先定）：判不了是规则过了、模型也没给准话的。sol 线
   就是上面那两种（不分财务投资）；luna 线（local）是本机没答上来（回的不是“是 / 否”、说这条太长）的：财务投资的（窗口的
   分区里有 JAVMEM_STRICT_PARTITIONS 的）转人工，原因 local_no_answer，事实、关系都这样；别的照 C 的 luna 线留下。
3. **留痕**（01 p4、02 p6）：判事实的每一档一行 audit_log（check_kind fact_judge，和 C 的 relation_judge 一个样子）；转人工的
   一行（check_kind precheck、tier rule、trusted hold）；主人在 javmem review 定的、30 天没人管的一行（javmem.pipeline.write）。
   javmem audit 只读地查（javmem.audit）。
4. **进图的口子**（javmem.graph.gate，J5.2）：账本记着每条事实、关系是谁认的（judged_by：local 本机那一档、llm 第二档、
   owner 主人，冻结的算主人认的，没检查过的是空的；javmem.pipeline.write 照说法算）。PrecheckGate 只放 judged_by 不是空的；
   J5.5 的账本视图照同一个条件挑（view_condition）。挡下的、转人工的本来就不进图（隔离表里的不在事实表；待确认的不进）。

**什么时候调模型：** 只在 javmem extract（L3、判断 C 之后，同一个标签、预算、截止时间；本机那一档另记在“标签-f1”下面，
不算进付钱的预算）。verify、write、合并工具照存下的判定办（apply），同步进图不调模型。过了规则的事实还有没判的窗口不写账，
包留着排队（skipped 是 NO_JUDGE，和 C 一样）。

**开关：** JAVMEM_PRECHECK=on 才开（不配、配 off 和以前一字不差）。开了要先配 JAVMEM_JUDGE_MODE：模式、本机模型、第二档都
照 C；事实挡、留的线可以另配 JAVMEM_PRECHECK_LOW、JAVMEM_PRECHECK_HIGH，不配和 C 的一样（线不在键里，改线不用重问）。
不和 G7 复核（--recheck）一起开：都是问事实“原文支不支持”，一条不判两遍。核的方式（checked_by）在 C 那一段后面再跟一段
`f1,模式,本机模型,下线[,上线,第二档的模型]`：换了线、模型，写过的窗口就重写。

提示词在 prompts/ 下，带版本号；改了提示词、问话或输出结构要换版本号，也要改 FACT_PROMPT_SHA256。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from javmem.config import ConfigError
from javmem.graph.gate import AdmitAll, Gate, GateItem, GateResult, Verdict
from javmem.pipeline import correct
from javmem.pipeline import reljudge
from javmem.pipeline.extract import ExtractOptions, Model
from javmem.pipeline.relcheck import RelationCheck
from javmem.pipeline.reljudge import (
    AUDIT_TIERS,
    BLOCK,
    CASCADE,
    DEFAULT_HIGH,
    HOLD,
    LLM_FAILED,
    LLM_UNSURE,
    LOCAL,
    NO_JUDGE,
    NOT_JUDGED,
    RULE,
    TIER_LLM,
    TIER_LOCAL,
    UNSURE,
    JudgeProfile,
    JudgeRequest,
    JudgeRunResult,
    JudgeSettings,
    JudgeSpec,
    LocalChat,
    _context,
    _decide,
    _parse_window,
    _threshold,
    _unsendable,
    plan_requests,
    run_requests,
)
from javmem.pipeline.render import escape_raw
from javmem.pipeline.store import ResultStore
from javmem.pipeline.verify import NOT_SENT, Checked, FactCheck, Finding
from javmem.redaction import REDACTION_TYPES, RedactionPattern, redact, scan_types
from javmem.usage import TokenLedger

FACT_PROMPT_VERSION = "f1"
FACT_PROMPT_SHA256 = "3b58481e71e3661e3afe1d84253b1b40d5951ac66ae3eac97012055d9280e7d7"  # 提示词连同问话、格式要求的指纹
ENV_PRECHECK = "JAVMEM_PRECHECK"
ENV_LOW = "JAVMEM_PRECHECK_LOW"
ENV_HIGH = "JAVMEM_PRECHECK_HIGH"
ON, OFF = "on", "off"

CHECK_KIND = "fact_judge"  # audit_log.check_kind：判事实的每一档
HOLD_KIND = "precheck"  # audit_log.check_kind：转人工的那一笔
LOCAL_NO_ANSWER = "local_no_answer"  # luna 线、财务投资的：本机没答上来，转人工（D22）
HOLD_CODES = (LLM_UNSURE, LLM_FAILED, LOCAL_NO_ANSWER)  # 这几种不进隔离表，写成说法、记成待确认 manual_review
HOLD_TEXT = {LOCAL_NO_ANSWER: "D22 财务投资", LLM_UNSURE: "C 拿不准", LLM_FAILED: "C 答不上"}  # 留痕的 judge 写为什么
LOCAL_PURPOSE = "precheck_judge_local"  # 记账的用途
LLM_PURPOSE = "precheck_judge"
LLM_SCHEMA_NAME = "javmem_f1_judge"
LOCAL_ASK = "\n光看这几句原话，能不能看出上面这条事实？只回答一个字：是 或 否。"
LLM_ASK = "\n光看这几句原话，能不能看出上面这条事实？"
CERTAINTY_TEXT = {"stated": "说定了", "inferred": "推断", "vague": "含糊"}  # L3 标的确定程度，写进事实那一行
FACTS = JudgeProfile(FACT_PROMPT_VERSION, LOCAL_ASK, LLM_ASK, LOCAL_PURPOSE, LLM_PURPOSE, LLM_SCHEMA_NAME, "fact", "F",
                     "事实的判断")

# judged_by：谁认的。一条事实、关系几处说法谁认的不一样时，取排在前面的
BY_OWNER, BY_LLM, BY_LOCAL = "owner", "llm", "local"
JUDGED_ORDER = (BY_OWNER, BY_LLM, BY_LOCAL)
BY_TIER = {TIER_LOCAL: BY_LOCAL, TIER_LLM: BY_LLM}

# 规则表里另登记一行（rule_id C、version 是提示词版本 f1）：开了才登记；隔离表里事实挡下的照样写 rule_id C
RULE_TEXT = "事实：判断（C，进图前检查 J5.3），本机模型判原话看不看得出这条事实，有把握说不能的不收；没把握的交抽取模型再判，说不能的不收，拿不准的转人工；luna 线财务投资的本机没答上来的转人工"
RULE_DECIDED_BY = "主会话 11-18 照建议先定（D178 ②沿用判断 C 的做法；D22 财务投资判不了的转人工，规矩题 1 甲），等主人点头"

GATE_VERSION = "precheck-v1"
UNCHECKED = "unchecked"  # 口子挡下的原因：没过进图前检查（J5.3 没开时写的账），要整本重建
_CHUNK = 500


def load_fact_prompt() -> str:
    return reljudge.load_judge_prompt(FACT_PROMPT_VERSION)


def fact_fingerprint() -> str:
    return FACTS.fingerprint()


# ---------- 设置 ----------


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """JAVMEM_PRECHECK 是不是 on。只看开关（进图的口子、账本视图用）；别的设置在 PrecheckSettings.from_env 核。"""
    env = os.environ if env is None else env
    return env.get(ENV_PRECHECK, "").strip().lower() == ON


@dataclass(frozen=True)
class PrecheckSettings:
    judge: JudgeSettings  # 判断 C 的设置：模式、本机模型、本机模型的地址
    low: float
    high: float | None
    strict: frozenset[str]  # 哪些分区算财务投资（JAVMEM_STRICT_PARTITIONS）

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PrecheckSettings | None:
        """JAVMEM_PRECHECK 没配、配 off：不开，返回 None。写错了、开了却没配 JAVMEM_JUDGE_MODE 的报配置错误，什么都不动。"""
        env = os.environ if env is None else env
        value = env.get(ENV_PRECHECK, "").strip().lower() or OFF
        if value not in (ON, OFF):
            raise ConfigError(f"{ENV_PRECHECK} 只能写 on 或 off")
        if value == OFF:
            return None
        judge = JudgeSettings.from_env(env)
        if judge is None:
            raise ConfigError(f"开 {ENV_PRECHECK} 要先配 {reljudge.ENV_MODE}（local 或 local+llm）：事实照判断 C 的模式、本机模型判")
        low = _threshold(env, ENV_LOW, judge.low)
        strict = correct.strict_partitions(env)
        if judge.mode == LOCAL:
            if env.get(ENV_HIGH, "").strip():
                raise ConfigError(f"{reljudge.ENV_MODE}={LOCAL} 只用 {ENV_LOW}（P 不高于它的挡），不用 {ENV_HIGH}")
            if low >= 1:
                raise ConfigError(f"{ENV_LOW} 要小于 1")
            return cls(judge, low, None, strict)
        high = _threshold(env, ENV_HIGH, judge.high if judge.high is not None else DEFAULT_HIGH)
        if not low < high:
            raise ConfigError(f"{ENV_LOW} 要小于 {ENV_HIGH}")
        return cls(judge, low, high, strict)

    def spec(self, llm: str | None, *, local: str | None = None) -> JudgeSpec:
        """判事实用的设置（版本 f1）：llm 是第二档的模型在结果键里的名字（local+llm 模式要）；local 换掉本机模型的名字（--fake）。"""
        if self.judge.mode == CASCADE:
            if llm is None:
                raise ValueError("local+llm 模式要第二档的模型")
            return JudgeSpec(CASCADE, local or self.judge.model, self.low, self.high, llm, version=FACT_PROMPT_VERSION)
        return JudgeSpec(LOCAL, local or self.judge.model, self.low, version=FACT_PROMPT_VERSION)


def checked_by(recheck_model: str | None, judge_spec: JudgeSpec | None, fact_spec: JudgeSpec | None) -> str:
    """账本里记的核的方式：规则（加复核的带上复核）、判断 C 那一段，开了进图前检查的再跟一段 f1 的设置。"""
    base = reljudge.checked_by(recheck_model, judge_spec)
    return base if fact_spec is None else f"{base} {fact_spec.token()}"


def split_checked_by(checked: str) -> tuple[str, JudgeSpec | None, JudgeSpec | None]:
    """核的方式拆成（规则和复核那一段，判断 C 的设置，事实的判断的设置）。f1 那一段是最后一个词、以“f 加数字和逗号”开头；
    版本认不得的报 ValueError（合并工具：先整本重建）。"""
    fact = None
    head, sep, tail = checked.rpartition(" ")
    if sep and re.match(r"f\d+,", tail):
        fact = JudgeSpec.parse(tail, FACT_PROMPT_VERSION)
        checked = head
    rules, judge = reljudge.split_checked_by(checked)
    return rules, judge, fact


# ---------- 问什么 ----------


def fact_line(fact: FactCheck, patterns: Sequence[RedactionPattern]) -> str:
    """一条事实写成一行：事实文字，有属性、值的接〔属性〕…〔值〕…（查询时它们也交给 Bot），最后括号里写确定程度。
    〔〕、换行先换掉（伪造不了别的行和这两个标注），再打一遍码（照判断 C）。"""
    raw = fact.raw

    def clean(value: object) -> str:
        text, _ = redact(escape_raw(str(value or "")).strip(), patterns)
        return text

    line = clean(raw.get("text"))
    for label, name in (("属性", "attribute"), ("值", "value")):
        part = clean(raw.get(name))
        if part:
            line += f" 〔{label}〕{part}"
    certainty = CERTAINTY_TEXT.get(str(raw.get("certainty") or ""))
    if certainty:
        line += f"（{certainty}）"
    return line


def build_request(
    item: Checked,
    fact: FactCheck,
    patterns: Sequence[RedactionPattern],
    parsed: tuple[str, list[tuple[str, str, str, str]]] | None = None,
) -> JudgeRequest | None:
    """一条过了规则的事实要问的话（照判断 C：开头那句说明、证据那几句前后各两句，带抬头）。没过规则的、L4 没核的窗口、L3 不发的
    窗口、证据不在正文里拼不出原话的，返回 None。

    第一个抬头和换了日子的抬头写全日期（D184）：事实里常写着“2026 年某月某日”，窗口文字跟上一段同一天的抬头只写“时:分”，
    写日期的那个抬头多半不在前后两句里，判的模型就看不出是哪天。关系那一行不带日子，判断 C 照旧。"""
    if item.check is None or _unsendable(item) or not fact.passed or not fact.proofs:
        return None
    line = fact_line(fact, patterns)
    preamble, entries = parsed if parsed is not None else _parse_window(item.rendered.text)
    refs = list(dict.fromkeys(proof.ref for proof in fact.proofs))
    context, shown = _context(entries, refs, dated=True)
    if not context:
        return None
    found = set(scan_types(line, patterns))
    sentence_of = {window_line.ref: window_line.sentence_id for window_line in item.window.lines}
    return JudgeRequest(
        window_id=item.window.window_id,
        l3_key=item.cache_key,
        index=fact.index,
        text=f"【事实】{line}\n【原话】\n{preamble}\n{context}\n",
        basis=tuple(dict.fromkeys((proof.sentence_id, proof.packet_id, proof.revision) for proof in fact.proofs)),
        leftover=tuple(kind for kind in REDACTION_TYPES if kind in found),
        context=tuple(dict.fromkeys(sentence_of[ref] for ref in shown if ref in sentence_of)),
        prompt_version=FACT_PROMPT_VERSION,
        kind="fact",
    )


def fact_requests(items: Sequence[Checked], patterns: Sequence[RedactionPattern]) -> list[tuple[Checked, FactCheck, JudgeRequest]]:
    """这一批要判的：每个 L4 核过的窗口里过了 G1 到 G7 的事实。"""
    found = []
    for item in items:
        if item.check is None or _unsendable(item):
            continue
        parsed = _parse_window(item.rendered.text)
        for fact in item.check.facts:
            request = build_request(item, fact, patterns, parsed)
            if request is not None:
                found.append((item, fact, request))
    return found


def run_fact_judge(
    items: Sequence[Checked],
    spec: JudgeSpec,
    store: ResultStore,
    *,
    local_model: LocalChat,
    llm_model: Model | None = None,
    patterns: Sequence[RedactionPattern],
    ledger: TokenLedger | None = None,
    options: ExtractOptions = ExtractOptions(),
    log: Callable[[str], None] | None = None,
) -> JudgeRunResult:
    """判这一批过了规则的事实（javmem extract 调）：和 reljudge.run_judge 一样，只换提示词、问话、记账的用途（FACTS）。"""
    return run_requests(fact_requests(items, patterns), spec, store, local_model=local_model, llm_model=llm_model, ledger=ledger,
                        options=options, log=log, profile=FACTS)


def plan_fact_judge(
    items: Sequence[Checked], store: ResultStore, spec: JudgeSpec, patterns: Sequence[RedactionPattern], *,
    retry_failed: bool = False, limit: int | None = None,
) -> dict[str, object]:
    """--dry-run 用：要判几条事实（facts）、本机那一档、第二档存过的、要问的（和 reljudge.plan_judge 一样）。不调模型。"""
    plan = plan_requests(fact_requests(items, patterns), store, spec, retry_failed=retry_failed, limit=limit, profile=FACTS)
    return {"facts": plan.pop("relations"), **plan}


# ---------- 用存下的判定 ----------


def _no_local_answer(info: Mapping[str, object]) -> bool:
    """判的经过里本机那一档没答上来：说太长了，或者回的不是“是 / 否”（P 是空的）。"""
    if info.get("local") == reljudge.TOO_LONG:
        return True
    steps = info.get("steps")
    if not isinstance(steps, list) or not steps or not isinstance(steps[0], Mapping):
        return True
    return steps[0].get("tier") == TIER_LOCAL and steps[0].get("p") is None


def is_strict(item: Checked, strict: Collection[str]) -> bool:
    """这个窗口是不是财务投资的：可见范围的分区里有严格分区。"""
    return bool(set(item.window.scope.partitions) & set(strict))


def apply(
    items: Sequence[Checked],
    store: ResultStore,
    spec: JudgeSpec,
    patterns: Sequence[RedactionPattern],
    *,
    strict: Collection[str],
    unjudged: str = HOLD,
) -> list[Checked]:
    """进图前检查，照存下的判定（不调模型、不改传进来的对象）：
    - 过了 G1 到 G7 的事实：挡下的加上 C 的原因（进隔离表）；第二档拿不准、答不上的加上 llm_unsure、llm_failed（写成待确认）；
      luna 线财务投资的本机没答上来加上 local_no_answer（转人工）。判的经过记在 FactCheck.judge。
    - 关系（判断 C 已经判过，reljudge.apply_judgments 要先用过）：luna 线财务投资的、本机没答上来、C 照 luna 线留下的，加上
      local_no_answer（转人工）。别的不动。
    过了规则的事实还有没判完的：unjudged 是 HOLD（verify、write）时整个窗口 check 换成 None、skipped 是 NO_JUDGE，包留着排队；
    是 BLOCK（合并工具）时记 C:not_judged 挡下（D125：没把握的不进）。"""
    out: list[Checked] = []
    for item in items:
        check = item.check
        if check is None:
            out.append(item)
            continue
        if _unsendable(item):  # L3 不发的窗口带着规则的结果：不写，和 NOT_SENT 一样
            out.append(replace(item, check=None, skipped=NOT_SENT))
            continue
        strict_window = is_strict(item, strict)
        parsed = _parse_window(item.rendered.text)
        facts: list[FactCheck] = []
        waiting = False
        for fact in check.facts:
            request = build_request(item, fact, patterns, parsed)
            if request is None:
                facts.append(fact)
                continue
            code, info = _decide(request, store, spec)
            if info is None:
                if unjudged == HOLD:
                    waiting = True
                    break
                code, info = NOT_JUDGED, {"version": FACT_PROMPT_VERSION, "mode": spec.mode, "steps": [],
                                          "context": list(request.context), "by": None, "verdict": UNSURE}
            if code is None and strict_window and spec.mode == LOCAL and _no_local_answer(info):
                code = LOCAL_NO_ANSWER
            failures = list(fact.failures)
            if code is not None:
                failures.append(Finding(RULE, code))
            facts.append(replace(fact, failures=failures, judge=info))
        if waiting:
            out.append(replace(item, check=None, skipped=NO_JUDGE))
            continue
        relations: list[RelationCheck] = []
        for relation in check.relations:
            info = relation.judge
            if (strict_window and relation.passed and isinstance(info, Mapping) and info.get("mode") == LOCAL
                    and _no_local_answer(info)):
                relation = replace(relation, failures=[*relation.failures, Finding(RULE, LOCAL_NO_ANSWER)])
            relations.append(relation)
        out.append(replace(item, check=replace(check, facts=facts, relations=relations)))
    return out


def fact_hold(fact: FactCheck) -> str | None:
    """进图前检查转人工的事实（过了 G1 到 G7，只有 C 的一条原因，是 HOLD_CODES 之一）：返回那个原因。"""
    failures = list(fact.failures)
    if len(failures) == 1 and failures[0].rule == RULE and failures[0].code in HOLD_CODES:
        return failures[0].code
    return None


def judged_tier(info: object) -> str | None:
    """判的经过 → 谁认的（local、llm）。没判过、没留下的是 None。"""
    if not isinstance(info, Mapping) or info.get("by") not in BY_TIER:
        return None
    return BY_TIER[str(info["by"])]


def strongest(tiers: Collection[str | None]) -> str | None:
    """几处说法谁认的合起来：有一处没认过（None）就是没认过；不然取 JUDGED_ORDER 里排在前面的。没有说法是 None。"""
    if not tiers or any(tier is None for tier in tiers):
        return None
    return next(tier for tier in JUDGED_ORDER if tier in tiers)


# ---------- 留痕 ----------


def claim_id(item: Checked, index: int) -> str:
    """一处事实说法的编号（和 javmem.pipeline.write 写进 fact_claims 的一样）。"""
    return f"{item.window.window_id}#{item.cache_key[:12]}#{index}"


def _basis(proofs: Sequence[object]) -> str:
    return json.dumps(list(dict.fromkeys(str(getattr(proof, "sentence_id")) for proof in proofs)), ensure_ascii=False)


def audit_rows(item: Checked, now: str) -> list[tuple[object, ...]]:
    """这个窗口进图前检查的留痕：判过的事实问过的每一档一行（fact_judge）；转人工的事实、关系一行（precheck）。
    一行的样子和 reljudge.audit_rows 一样。不抄原文、不抄理由。"""
    check = item.check
    if check is None:
        return []
    rows: list[tuple[object, ...]] = []
    for fact in check.facts:
        info = fact.judge
        steps = info.get("steps") if isinstance(info, Mapping) else None
        if not isinstance(steps, list) or not fact.proofs:
            continue
        basis = _basis(fact.proofs)
        first = fact.proofs[0]
        item_id = claim_id(item, fact.index)
        for step in steps:
            if not isinstance(step, Mapping) or step.get("tier") not in AUDIT_TIERS:
                continue
            p = step.get("p")
            rows.append((
                CHECK_KIND, "fact", item_id, str(step.get("verdict") or UNSURE), basis, first.packet_id, first.revision,
                float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None, now,
                AUDIT_TIERS[str(step["tier"])], f"{step.get('model')} {FACT_PROMPT_VERSION}",
            ))
        code = fact_hold(fact)
        if code is not None:
            rows.append((HOLD_KIND, "fact", item_id, "hold", basis, first.packet_id, first.revision, None, now, "rule",
                         HOLD_TEXT[code]))
    for relation in check.relations:  # 转人工的关系（判断 C 拿不准、答不上的；财务投资本机没答上来的）
        failures = list(relation.failures)
        if len(failures) != 1 or failures[0].rule != RULE or failures[0].code not in HOLD_CODES or not relation.proofs:
            continue
        first = relation.proofs[0]
        rows.append((HOLD_KIND, "relation", f"{item.window.window_id}#{item.cache_key[:12]}#R{relation.index}", "hold",
                     _basis(relation.proofs), first.packet_id, first.revision, None, now, "rule", HOLD_TEXT[failures[0].code]))
    return rows


def insert_audit(conn: sqlite3.Connection, rows: Sequence[tuple[object, ...]]) -> int:
    """写留痕（调用方开着事务）：一模一样的一行已经有了就不再写（和 reljudge.write_audit 一样），判的不一样另写一行，旧的
    留着（02 p6）。返回写了几行。"""
    written = 0
    for row in rows:
        kind, item_kind, item_id, trusted, basis, _, _, probability, _, tier, judge = row
        exists = conn.execute(
            "SELECT 1 FROM audit_log WHERE item_kind = ? AND item_id = ? AND check_kind = ? AND trusted = ? AND tier = ?"
            " AND judge = ? AND basis IS ? AND probability IS ? LIMIT 1",
            (item_kind, item_id, kind, trusted, tier, judge, basis, probability),
        ).fetchone()
        if exists is not None:
            continue
        conn.execute(
            "INSERT INTO audit_log (check_kind, item_kind, item_id, trusted, basis, packet_id, revision, probability,"
            " created_at, tier, judge) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        written += 1
    return written


def write_audit(conn: sqlite3.Connection, item: Checked, now: str) -> int:
    return insert_audit(conn, audit_rows(item, now))


# ---------- 数 ----------


def summarize(items: Sequence[Checked]) -> dict[str, object]:
    """apply 以后的数：判了几条事实、留下几条、挡下几条（按原因）、转人工几条（按原因）、本机那一档判成什么、第二档答了什么、
    第二档没判成的、还在等判的窗口；关系转人工（local_no_answer）几条。不抄原文。"""
    judged = kept = 0
    blocked: Counter[str] = Counter()
    held: Counter[str] = Counter()
    local: Counter[str] = Counter()
    llm: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    by: Counter[str] = Counter()
    relations_held = 0
    for item in items:
        if item.check is None:
            continue
        for fact in item.check.facts:
            info = fact.judge
            if not isinstance(info, Mapping):
                continue
            judged += 1
            codes = [finding.code for finding in fact.failures if finding.rule == RULE]
            if codes and codes[0] in HOLD_CODES:
                held[codes[0]] += 1
            elif codes:
                blocked[codes[0]] += 1
            else:
                kept += 1
                by[str(judged_tier(info))] += 1
            for step in info.get("steps") or []:  # type: ignore[union-attr]
                if not isinstance(step, Mapping):
                    continue
                if step.get("tier") == TIER_LOCAL:
                    local[str(step.get("verdict"))] += 1
                else:
                    llm[str(step.get("answer") or "")] += 1
            if info.get("llm"):
                missing[str(info["llm"])] += 1
        relations_held += sum(1 for relation in item.check.relations
                              if any(finding.rule == RULE and finding.code == LOCAL_NO_ANSWER for finding in relation.failures))
    return {
        "judged": judged,
        "kept": kept,
        "kept_by": dict(sorted(by.items())),
        "blocked": sum(blocked.values()),
        "blocked_by_code": dict(sorted(blocked.items())),
        "held": sum(held.values()),
        "held_by_code": dict(sorted(held.items())),
        "local": dict(sorted(local.items())),
        "llm": dict(sorted(llm.items())),
        "llm_not_answered": dict(sorted(missing.items())),
        "relations_held": relations_held,
        "windows_waiting": sum(1 for item in items if item.skipped == NO_JUDGE),
    }


# ---------- 进图的口子（javmem.graph.gate） ----------


class PrecheckGate:
    """进图前检查的口子：只放账本里 judged_by 不是空的事实、关系（谁认过的）。只读账本这一列，不调模型，只回 ADMIT、HOLD。
    挡下的原因都是 unchecked：J5.3 没开时写的账（旧账本），要整本重建才进得了图（不悄悄放进去）。"""

    version = GATE_VERSION

    def admit(self, conn: sqlite3.Connection, library: str, items: Sequence[GateItem]) -> GateResult:
        result = GateResult()
        for kind, table, column in (("fact", "facts", "fact_id"), ("relation", "relations", "relation_id")):
            ids = sorted({item.item_id for item in items if item.kind == kind})
            judged: dict[str, object] = {}
            for start in range(0, len(ids), _CHUNK):
                part = ids[start : start + _CHUNK]
                marks = ", ".join("?" * len(part))
                judged.update(
                    (row[0], row[1])
                    for row in conn.execute(f"SELECT {column}, judged_by FROM {table} WHERE {column} IN ({marks})", part)
                )
            for one in ids:
                if judged.get(one):
                    result.verdicts[(kind, one)] = Verdict.ADMIT
                else:
                    result.verdicts[(kind, one)] = Verdict.HOLD
                    result.reasons[(kind, one)] = UNCHECKED
        return result


def gate(env: Mapping[str, str] | None = None) -> Gate:
    """同步进图用哪道口子：开了进图前检查是 PrecheckGate，没开是全放（和以前一字不差）。"""
    return PrecheckGate() if enabled(env) else AdmitAll()


def view_condition(alias: str) -> str:
    """J5.5 账本视图挑事实、关系时加的条件（开了进图前检查才加）：待确认的照接口文档 4.3 返回（标着还没定）；别的要谁认过，
    和进图的口子一样，主图连不上时答的和主图一样。"""
    return f"({alias}.status = 'pending' OR {alias}.judged_by IS NOT NULL)"


__all__ = [
    "BLOCK",
    "CHECK_KIND",
    "FACTS",
    "FACT_PROMPT_SHA256",
    "FACT_PROMPT_VERSION",
    "HOLD",
    "HOLD_CODES",
    "HOLD_KIND",
    "LOCAL_NO_ANSWER",
    "PrecheckGate",
    "PrecheckSettings",
    "apply",
    "audit_rows",
    "build_request",
    "checked_by",
    "enabled",
    "fact_fingerprint",
    "fact_hold",
    "fact_requests",
    "gate",
    "plan_fact_judge",
    "run_fact_judge",
    "split_checked_by",
    "strongest",
    "summarize",
    "view_condition",
    "write_audit",
]
