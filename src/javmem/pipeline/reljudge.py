"""L4 的判断 C（D127、D131、D133）：过了 R1 到 R6 的关系，再问一遍“光看这几句原话，能不能看出这条关系”。

规则只核得了引文在不在、两头在不在、类型对不对，看不出原话撑不撑得住这条关系（只是打算、说的是别的单子、挂钩的股票
不算持有……）。照主人 11-15 定的（D131 离线试，D133 暂定的做法），J4.3 的 R1 到 R6 后面再判一道，只判关系：

- **本机那一档：** 本机模型（llama.cpp 的 OpenAI 兼容接口，Qwen3.8-27B）只回一个字“是 / 否”，从第一个字的候选字里
  各取第一个“是”“否”，P(能) = P(是) / (P(是) + P(否))，和离线试一模一样（线才和 D133 对得上）。回的不是“是 / 否”的，
  P 是空的，算没把握。
- **两种模式**（JAVMEM_JUDGE_MODE，不配、配 off 就不判，和以前一字不差）：
  - `local`（luna 线）：P ≤ 下线（JAVMEM_JUDGE_LOW，默认 0.1）的挡，别的留。
  - `local+llm`（sol 线）：P ≤ 下线（默认 0.2）的挡，P ≥ 上线（JAVMEM_JUDGE_HIGH，默认 0.8）的留；中间的、P 是空的，
    交给抽取模型（JAVMEM_LLM_*，sol 线就是 sol）用同一个问题再判一次，答“能 / 不能 / 拿不准”加一句理由：能的留，
    不能、拿不准的挡；重调也没用的（截断、拒答、两次格式错）、关系那一行打完码还扫得出来不能发的，也挡（D125：没把握的不进）。
- **问什么：** 提示词 c1（prompts/c1.md，就是离线试的 c1：规矩、六种关系的定义卡要点、编的例子）；L3 选了讲隶属于的
  提示词（extract.PART_OF_PROMPTS）的，用 c2（c1 加隶属于、任职于写上部门，七种，D210），和 L3 认一样的关系种类（judge_version_for）。原话是 L3 发出去的那份
  窗口文字（打过码，带预处理 A、B 的标注，和离线试一样用的是 L3 看到的）里的：开头那句说明，证据那几句（★，过了 R1 的
  证据）前后各两句，带抬头。关系写成“主体 —关系→ 客体（状态：…）”，名字照 L3 的实体，亲属带具体说法；名字里的〔〕、
  换行先换掉，再打一遍码。抬头照窗口文字原样（跟上一段同一天的只写“时:分”）：关系那一行不带日子。进图前检查判事实时
  （precheck）第一个抬头和换了日子的抬头写全日期（_context 的 dated，D184）。
- **挡下的：** 加上 C 的原因进隔离表（rule_id 是 C）：local_unsupported（本机有把握说不能）、llm_unsupported（第二档说不能）、
  llm_unsure（第二档拿不准）、llm_failed（第二档回不了、不能发）。D131 说第二档也拿不准的放待确认：关系 J4.4 才写进账本，
  待确认现在接不了关系，先隔离、原因分开写，J4.4 再把 llm_unsure、llm_failed 挪进待确认。
- **留痕：** 每条判过的关系、问过的每一档写一行 audit_log（write_audit，写窗口的那个事务里写）：哪一级判的（本机是 judge，
  第二档是 model）、哪个模型哪一版提示词、判成什么、P、证据那几句的句子编号。一行里不抄原文、不抄理由；同一个窗口重写、
  判的一样的不重复写，判的不一样了写一行新的，旧的留着（02 p6：输的那条不删，留作审计记录）。只有编号和判定，撤回、
  彻底删除不另清（和规则表的正反例一样）。

**什么时候调、什么时候用**（和 L4 的模型复核一样）：调模型只在 `javmem extract`（L3 之后，同一个标签、预算、截止时间；
本机那一档另记在“标签-c1”下面，不算进付钱的预算）；`javmem verify`、`write`、合并工具只按键取存下的判定（apply_judgments），
不调模型。配了 C、过了规则的关系还有没判的窗口，check 换成 None、skipped 是 NO_JUDGE：L6 不写它，包留着排队，下次 extract
接着判。本机模型连不上的这一次不判（不当成挡），连着几次连不上就停下这一段。

**键和存法：** 判定存在 L3 的结果库里（javmem.pipeline.store 的 judgments、judgment_windows、judgment_calls），键是“哪一档、
提示词版本、模型、发过去的那段文字”（judge_key），同样的输入不再调。挡、留的线不在键里：换了线不用重调本机那一档。
窗口删了（撤回、彻底删除、重发成 private）连着删：第二档的理由里可能写着原文的名字。

**核的方式：** 配了 C 的，核的方式（说法的 l4_version、隔离表的 rule_version）后面再跟一段
`c1,模式,本机模型,下线[,上线,第二档的模型]`（checked_by；c2 的写 c2 开头）：换了开关、线、模型、版本，写过的窗口就重写；
合并工具照它原样重做。

提示词在 prompts/ 下，带版本号；改了提示词、问话或输出结构要换版本号，也要改 JUDGE_PROMPTS 里的指纹。
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from importlib import resources
from typing import Protocol

from javmem.config import ConfigError
from javmem.llm import ModelReply, Usage, _is_loopback
from javmem.pipeline.extract import (
    CACHED,
    DONE,
    FAILED,
    NOT_STARTED,
    PART_OF_PROMPTS,
    ExtractOptions,
    FormatError,
    Model,
    RunResult,
    WindowOutcome,
    _now,
    _Run,
)
from javmem.pipeline.recheck import checked_by as rules_checked_by
from javmem.pipeline.relcheck import RULE_ORDER, RelationCheck
from javmem.pipeline.render import escape_raw
from javmem.pipeline.store import ResultStore
from javmem.pipeline.verify import NOT_SENT, Checked, Finding
from javmem.pipeline.window import Window
from javmem.redaction import REDACTION_TYPES, RedactionPattern, redact, scan_types
from javmem.usage import TokenLedger

JUDGE_PROMPT_VERSION = "c1"
JUDGE_PROMPT_SHA256 = "8081db409af8bfd63b72396ff30eef56ff38aabca1464bb48338652937bc1bc5"
PART_OF_JUDGE_VERSION = "c2"  # D210：讲了隶属于的那一版（七种关系），L3 选了讲隶属于的提示词时用
# 判关系能用的几版：版本 → 提示词连同两种问话、第二档格式要求、输出结构的指纹（judge_fingerprint）
JUDGE_PROMPTS = {
    JUDGE_PROMPT_VERSION: JUDGE_PROMPT_SHA256,
    PART_OF_JUDGE_VERSION: "9adca8155a87a1c3bc046bfd41d4df4d29c4d7b18973c67601d459df1ace6362",
}
RULE = "C"
CHECK_KIND = "relation_judge"  # audit_log.check_kind
LOCAL_PURPOSE = "l4_judge_local"  # 记账的用途
LLM_PURPOSE = "l4_judge"
LLM_SCHEMA_NAME = "javmem_c1_judge"

OFF, LOCAL, CASCADE = "off", "local", "local+llm"
MODES = (OFF, LOCAL, CASCADE)
ENV_MODE = "JAVMEM_JUDGE_MODE"
ENV_URL = "JAVMEM_JUDGE_URL"  # 本机模型的 chat/completions 地址，只能是本机；只有 javmem extract 要
ENV_MODEL = "JAVMEM_JUDGE_MODEL"
ENV_LOW = "JAVMEM_JUDGE_LOW"
ENV_HIGH = "JAVMEM_JUDGE_HIGH"
DEFAULT_MODEL = "qwen3.8-27b"
DEFAULT_LOW = {LOCAL: 0.1, CASCADE: 0.2}  # D133：luna 线只用本机、P ≤ 0.1 挡；sol 线 P ≤ 0.2 挡、P ≥ 0.8 留
DEFAULT_HIGH = 0.8
FAKE_LOCAL_NAME = "fake-local"  # --fake 用的本机模型（FakeLocal）

TIER_LOCAL, TIER_LLM = "local", "llm"
AUDIT_TIERS = {TIER_LOCAL: "judge", TIER_LLM: "model"}  # audit_log.tier（02 p5 的四级：判断模型、大模型读原文）
SUPPORTED, UNSUPPORTED, UNSURE = "supported", "unsupported", "unsure"  # 一档判成什么（audit_log.trusted）
LOCAL_NO, LLM_NO, LLM_UNSURE, LLM_FAILED = "local_unsupported", "llm_unsupported", "llm_unsure", "llm_failed"
YES, NO, NOT_SURE = "能", "不能", "拿不准"
ANSWERS = (YES, NO, NOT_SURE)
LLM_VERDICT = {YES: SUPPORTED, NO: UNSUPPORTED, NOT_SURE: UNSURE}
LLM_CODE = {NO: LLM_NO, NOT_SURE: LLM_UNSURE}

# 问话和第二档的回答结构，照离线试（run\j39d\判断\judge.py）原样：本机只回一个字；第二档带一句理由
LOCAL_ASK = "\n光看这几句原话，能不能看出上面这条关系？只回答一个字：是 或 否。"
LLM_ASK = "\n光看这几句原话，能不能看出上面这条关系？"
LLM_FORMAT = "\n\n回答写成 JSON：答案是“能”“不能”“拿不准”之一；理由一句话，三十个字以内。"
LLM_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["答案", "理由"],
    "properties": {"答案": {"type": "string", "enum": list(ANSWERS)}, "理由": {"type": "string"}},
}

NO_JUDGE = "no_judgment"  # skipped：配了 C，过了规则的关系还有没判的（apply_judgments 标的）
AROUND = 2  # 证据前后各带几句
REASON_CHARS = 200  # 第二档的理由最多存几个字：提示词让它写三十个字以内
ANSWER_CHARS = 8
LOCAL_WORKERS = 2  # 本机模型同时最多 2 路，认人认事、别的程序也在用
LOCAL_STOP_AFTER = 3  # 本机模型连着几次连不上，这一段就停下
_MODEL_NAME = re.compile(r"[A-Za-z0-9._:/-]{1,100}")  # 和 llm 的模型名一样：没有逗号、空白，写得进核的方式
_LOCAL_ERRORS = (urllib.error.URLError, OSError, ValueError, http.client.HTTPException)


TOO_LONG = "too_long"  # 本机模型说这一个请求超了上下文：再问也一样（和抽取的 too_long 一个意思）
CONFIG = "config"  # 本机模型回的 HTTP 错误是设置不对（地址、密钥、参数），每个请求都会这样
TRANSIENT = "transient"  # 一时不行（5xx、408、429），和连不上一样
_TOO_LONG_WORDS = ("context", "too long", "too many tokens", "exceed")


def _http_kind(error: urllib.error.HTTPError) -> str:
    """本机模型回的 HTTP 错误是哪一种。只有 413、或者 400 又说了超了上下文（llama.cpp：exceeds the available context size）
    才算这一个请求的事；别的 4xx（404 地址少了一截、401 要密钥、400 不认请求里的参数）是设置不对，不能当成“这一条没把握”
    记下来，不然 C 就悄悄关了，sol 线还会全交第二档。"""
    code = int(error.code or 0)
    body = ""
    try:
        if code == 400:
            body = error.read(2048).decode("utf-8", "replace").lower()
    except Exception:  # noqa: BLE001 - 读不到回的那一段就当没说
        body = ""
    finally:
        try:
            error.close()  # 回的那一段不要了，连接放掉
        except Exception:  # noqa: BLE001 - 关不上不碍事
            pass
    if code == 413 or (code == 400 and any(word in body for word in _TOO_LONG_WORDS)):
        return TOO_LONG
    if code in (408, 429) or code >= 500:
        return TRANSIENT
    return CONFIG


def load_judge_prompt(version: str = JUDGE_PROMPT_VERSION) -> str:
    text = (resources.files("javmem.pipeline") / "prompts" / f"{version}.md").read_text(encoding="utf-8")
    return text.replace("\r\n", "\n")


def judge_fingerprint(version: str = JUDGE_PROMPT_VERSION) -> str:
    """提示词连同两种问话、第二档的格式要求、输出结构的指纹。"""
    text = "\n".join(
        (load_judge_prompt(version), LOCAL_ASK, LLM_ASK, LLM_FORMAT, json.dumps(LLM_SCHEMA, ensure_ascii=False, sort_keys=True))
    )
    return _sha256(text)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JudgeProfile:
    """判一样东西用的那一套：提示词版本、两种问话、记账的用途、第二档输出结构的名字、判定连到结果库的哪张表（kind）、
    记账时窗口编号后面跟的字母、打印时叫什么。关系是 RELATIONS（判断 C）；事实是进图前检查的 FACTS（javmem.pipeline.precheck）。
    两样的规矩、线、第二档的回答结构都一样。"""

    version: str
    local_ask: str
    llm_ask: str
    local_purpose: str
    llm_purpose: str
    schema_name: str
    kind: str
    letter: str
    label: str

    def system(self) -> str:
        return load_judge_prompt(self.version)

    def fingerprint(self) -> str:
        """提示词连同两种问话、第二档的格式要求、输出结构的指纹。"""
        text = "\n".join(
            (self.system(), self.local_ask, self.llm_ask, LLM_FORMAT, json.dumps(LLM_SCHEMA, ensure_ascii=False, sort_keys=True))
        )
        return _sha256(text)


RELATIONS = JudgeProfile(JUDGE_PROMPT_VERSION, LOCAL_ASK, LLM_ASK, LOCAL_PURPOSE, LLM_PURPOSE, LLM_SCHEMA_NAME, "relation", "R",
                         "判断 C")
RELATIONS_C2 = replace(RELATIONS, version=PART_OF_JUDGE_VERSION, schema_name="javmem_c2_judge")  # 别的照 c1：问话、记账、表都一样


def relation_profile(version: str) -> JudgeProfile:
    """判关系用的那一套（c1、c2）。认不得的版本报 ValueError。"""
    profiles = {RELATIONS.version: RELATIONS, RELATIONS_C2.version: RELATIONS_C2}
    if version not in profiles:
        raise ValueError("认不得的判断 C 的版本")
    return profiles[version]


def judge_version_for(prompt: str) -> str:
    """判断 C 用哪一版：和 L3 认一样的关系种类。L3 选了讲隶属于的提示词（PART_OF_PROMPTS）用 c2，别的照旧 c1（D210）。"""
    return PART_OF_JUDGE_VERSION if prompt in PART_OF_PROMPTS else JUDGE_PROMPT_VERSION


# ---------- 设置 ----------


def _threshold(env: Mapping[str, str], name: str, default: float) -> float:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(f"{name} 要写成 0 到 1 之间的数") from None
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ConfigError(f"{name} 要写成 0 到 1 之间的数")
    return value


@dataclass(frozen=True)
class JudgeSpec:
    """判的设置，算键、定挡留、写进核的方式都用它：模式、本机模型、线、第二档的模型（结果键里的名字）。version 是判哪一样
    用的提示词：关系是 c1（判断 C），事实是 f1（进图前检查，javmem.pipeline.precheck）。"""

    mode: str
    local: str
    low: float
    high: float | None = None
    llm: str | None = None
    version: str = JUDGE_PROMPT_VERSION

    def token(self) -> str:
        """写进核的方式的那一段：版本,模式,本机模型,下线[,上线,第二档的模型]（关系是 c1 开头，事实是 f1）。模型名里没有
        逗号、空白。线用 repr 写（最短又能原样读回来的写法：0.1 还是 0.1，0.1234567 不会被截成 0.123457），合并工具读回来
        定挡留和写账一样。"""
        parts = [self.version, self.mode, self.local, repr(float(self.low))]
        if self.mode == CASCADE:
            parts += [repr(float(self.high if self.high is not None else 1.0)), str(self.llm)]
        return ",".join(parts)

    @classmethod
    def parse(cls, token: str, version: str = JUDGE_PROMPT_VERSION) -> JudgeSpec:
        """token 的反过来。写法不对、版本认不得的报 ValueError。"""
        parts = token.split(",")
        if len(parts) < 4 or parts[0] != version:
            raise ValueError("认不得的判断 C 的设置")
        mode, local, low = parts[1], parts[2], float(parts[3])
        if mode == LOCAL and len(parts) == 4:
            return cls(LOCAL, local, low, version=version)
        if mode == CASCADE and len(parts) == 6:
            return cls(CASCADE, local, low, float(parts[4]), parts[5], version=version)
        raise ValueError("认不得的判断 C 的设置")

    def describe(self) -> dict[str, object]:
        return {"version": self.version, "mode": self.mode, "local": self.local, "low": self.low, "high": self.high,
                "llm": self.llm}


@dataclass(frozen=True)
class JudgeSettings:
    mode: str
    model: str = DEFAULT_MODEL
    low: float = DEFAULT_LOW[LOCAL]
    high: float | None = None
    url: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JudgeSettings | None:
        """JAVMEM_JUDGE_MODE 没配、配 off：不判，返回 None。写错了报配置错误，什么都不动。"""
        env = os.environ if env is None else env
        mode = env.get(ENV_MODE, "").strip().lower() or OFF
        if mode not in MODES:
            raise ConfigError(f"{ENV_MODE} 只能写 {'、'.join(MODES)}")
        if mode == OFF:
            return None
        url = env.get(ENV_URL, "").strip() or None
        if url is not None:
            try:
                parts = urllib.parse.urlsplit(url)
                scheme, host = parts.scheme, parts.hostname
            except ValueError:
                scheme, host = "", None
            if scheme not in ("http", "https") or not _is_loopback(host):
                raise ConfigError(f"{ENV_URL} 只能写本机的地址（127.0.0.1、localhost）：判断要把原文发给它，数据不出这台电脑")
        model = env.get(ENV_MODEL, "").strip() or DEFAULT_MODEL
        if not _MODEL_NAME.fullmatch(model):
            raise ConfigError(f"{ENV_MODEL} 写法不对")
        low = _threshold(env, ENV_LOW, DEFAULT_LOW[mode])
        if mode == LOCAL:
            if env.get(ENV_HIGH, "").strip():
                raise ConfigError(f"{ENV_MODE}={LOCAL} 只用 {ENV_LOW}（P 不高于它的挡），不用 {ENV_HIGH}")
            if low >= 1:
                raise ConfigError(f"{ENV_LOW} 要小于 1")
            return cls(mode, model, low, None, url)
        high = _threshold(env, ENV_HIGH, DEFAULT_HIGH)
        if not low < high:
            raise ConfigError(f"{ENV_LOW} 要小于 {ENV_HIGH}")
        return cls(mode, model, low, high, url)

    def require_url(self) -> str:
        if self.url is None:
            raise ConfigError(f"配了 {ENV_MODE} 就要配 {ENV_URL}（本机模型的地址）")
        return self.url

    def spec(self, llm: str | None, *, local: str | None = None, version: str = JUDGE_PROMPT_VERSION) -> JudgeSpec:
        """llm 是第二档的模型在结果键里的名字（local+llm 模式要）；local 换掉本机模型的名字（--fake 用）；version 是判关系用
        哪一版提示词（judge_version_for，跟着 L3 的提示词）。"""
        relation_profile(version)  # 认不得的版本报 ValueError
        if self.mode == CASCADE:
            if llm is None:
                raise ValueError("local+llm 模式要第二档的模型")
            return JudgeSpec(CASCADE, local or self.model, self.low, self.high, llm, version=version)
        return JudgeSpec(LOCAL, local or self.model, self.low, version=version)


def checked_by(recheck_model: str | None, spec: JudgeSpec | None) -> str:
    """账本里记的核的方式：规则（加了复核的再带上复核的提示词版本和模型，recheck.checked_by），配了 C 的再跟一段。"""
    base = rules_checked_by(recheck_model)
    return base if spec is None else f"{base} {spec.token()}"


def split_checked_by(checked: str) -> tuple[str, JudgeSpec | None]:
    """核的方式拆成（规则和复核那一段，C 的设置）。C 那一段是最后一个词、以“c 加数字和逗号”开头（模型名里没有逗号）；
    版本认不得的报 ValueError（合并工具：先整本重建）。"""
    head, sep, tail = checked.rpartition(" ")
    if sep and re.match(r"c\d+,", tail):
        version = tail.split(",", 1)[0]
        relation_profile(version)  # c1、c2 以外的报 ValueError
        return head, JudgeSpec.parse(tail, version)
    return checked, None


# ---------- 问什么 ----------

_HEAD = re.compile(r"^\[[^\]]+\][^：\n]*：\s*$")  # 一句话、一封邮件、一个附件的抬头（render._block_heading 这些）
_LINE = re.compile(r"^(?P<ref>[TC]\d+)\s+(?P<text>.*)$")
# 抬头的时间（render._time）：换了日子、时区才写全“年-月-日 时:分 时区”，跟上一段同一天、同一时区的只写“时:分”
_STAMP_FULL = re.compile(r"^\[(?P<day>\d{4}-\d{2}-\d{2}) \d{2}:\d{2} (?P<zone>[+-]\d{2}:\d{2})\]")
_STAMP_SHORT = re.compile(r"^\[(?P<time>\d{2}:\d{2})\]")


def _heading_day(heading: str) -> tuple[str, str] | None:
    """写全日期的抬头 → (年-月-日, 时区)；只写时分的、认不出的 → None。"""
    match = _STAMP_FULL.match(heading)
    return (match.group("day"), match.group("zone")) if match else None


def _parse_window(text: str) -> tuple[str, list[tuple[str, str, str, str]]]:
    """窗口文字 → (开头那句说明, [(编号, 抬头, 这一行, 写全日期的抬头)])。照离线试：【正文】前面只取第一行说明，后面的抬头、
    编号行按先后。

    写全日期的抬头（D184）：本来写全的照抄；只写“时:分”的补上最近一个写全的抬头的日期和时区（【正文】以前【上文】里的
    抬头也算）。render._time 只在和上一段同一天、同一时区时才省掉，补出来的就是原来那一刻。"""
    preamble, entries, header, dated, in_body = "", [], "", "", False
    day: tuple[str, str] | None = None  # 最近一个写全日期的抬头的 (年-月-日, 时区)
    for raw in text.splitlines():
        line = raw.rstrip()
        heading = _HEAD.match(line.strip()) is not None
        if heading:
            day = _heading_day(line.strip()) or day
        if not in_body:
            if line.startswith("【正文】"):
                in_body = True
            elif line.strip() and not preamble:
                preamble = line.strip()
            continue
        if heading:
            header = line.strip()
            short = _STAMP_SHORT.match(header)
            dated = f"[{day[0]} {short.group('time')} {day[1]}]{header[short.end():]}" if short and day else header
            continue
        match = _LINE.match(line.strip())
        if match:
            entries.append((match.group("ref"), header, line.strip(), dated))
    return preamble, entries


def _context(
    entries: Sequence[tuple[str, str, str, str]], refs: Sequence[str], around: int = AROUND, *, dated: bool = False
) -> tuple[str, list[str]]:
    """证据那几行前后各带 around 行，抬头换了才写，证据那几行前面标★。→ (那段话, 写进去的编号)。

    dated（进图前检查判事实用，D184）：这一段里第一个抬头、和这一段里上一个抬头不是同一天的抬头，写全日期（几处证据隔得远、
    中间跳过了写日期的那个抬头时会这样）；别的照原样。不给 dated 时和以前一字不差（判断 C 判关系）。"""
    index = {ref: number for number, (ref, *_) in enumerate(entries)}
    picked: set[int] = set()
    for ref in refs:
        if ref in index:
            at = index[ref]
            picked.update(range(max(0, at - around), min(len(entries), at + around + 1)))
    out: list[str] = []
    shown: list[str] = []
    last_header: str | None = None
    last_day: tuple[str, str] | None = None  # dated：这一段里上一个写出去的抬头是哪一天
    for number in sorted(picked):
        ref, header, line, full = entries[number]
        if header != last_header:
            if dated:
                day = _heading_day(full)
                out.append(full if last_header is None or day != last_day else header)
                last_day = day
            else:
                out.append(header)
            last_header = header
        out.append(("★" if ref in refs else "") + line)
        shown.append(ref)
    return "\n".join(out), shown


@dataclass(frozen=True)
class JudgeRequest:
    """要判的一条关系（或者一条事实，进图前检查）：哪个窗口（编号、L3 的键）的第几条，发过去的那段话（不带问话），证据那几句。"""

    window_id: str
    l3_key: str
    index: int  # 在 L3 结果里是第几条关系（事实），从 0 数
    text: str  # 【关系】……【原话】……（事实是【事实】……）
    basis: tuple[tuple[str, str, int], ...]  # 证据那几句：(句子编号, 包号, 版本)
    leftover: tuple[str, ...] = ()  # 关系那一行打完码还扫得出来的类型：本机那一档照判，第二档不发
    context: tuple[str, ...] = ()  # 发过去的每一行（证据和前后文）的句子编号：第二档的理由可能照着哪一句写，单条撤回按它清
    prompt_version: str = JUDGE_PROMPT_VERSION  # 关系 c1；事实 f1（键里有它，两样的判定不会混）
    kind: str = "relation"  # relation 或 fact：结果库里判定连到哪张表

    def key(self, tier: str, model: str) -> str:
        return judge_key(tier, model, self.text, self.prompt_version)

    @property
    def link(self) -> tuple[str, str, int]:
        return (self.window_id, self.l3_key, self.index)


def judge_key(tier: str, model: str, text: str, prompt_version: str = JUDGE_PROMPT_VERSION) -> str:
    """判的键：哪一档、提示词版本、模型（名字，第二档是名字@档位/输出方式）、发过去的那段话。"""
    return _sha256("\n".join(("javmem-judge", tier, prompt_version, model, _sha256(text))))


def _unsendable(item: Checked) -> bool:
    """L3 不发的窗口（private 的、打码没打干净的），C 也不判：不全靠 verify_windows 先标了 NOT_SENT。"""
    return item.window.scope.private or bool(item.rendered.leftover)


def build_request(
    item: Checked,
    relation: RelationCheck,
    patterns: Sequence[RedactionPattern],
    parsed: tuple[str, list[tuple[str, str, str, str]]] | None = None,
    *,
    version: str = JUDGE_PROMPT_VERSION,
) -> JudgeRequest | None:
    """一条过了规则的关系要问的话。没过规则的、L4 没核的窗口、L3 不发的窗口、证据不在正文里拼不出原话的，返回 None。
    parsed 是这个窗口文字拆好的（_parse_window），一个窗口几条关系只拆一次。version 是用哪一版提示词问（键里有它）。"""
    check = item.check
    if check is None or _unsendable(item) or not relation.passed or not relation.proofs:
        return None
    raw = relation.raw
    names: dict[str, str] = {}
    for entity in check.entities:
        key = entity.get("key")
        if isinstance(key, str):
            names.setdefault(key, str(entity.get("name") or key))

    def clean(value: object) -> str:
        text, _ = redact(escape_raw(str(value or "")).strip(), patterns)
        return text

    subject = clean(names.get(str(raw.get("subject")), raw.get("subject")))
    obj = clean(names.get(str(raw.get("object")), raw.get("object")))
    kind = clean(raw.get("relation"))
    kinship = clean(raw.get("kinship")) if kind == "亲属" else ""
    kin = f"（主体是客体的{kinship}）" if kinship else ""
    line = f"{subject} —{kind}→ {obj}{kin}（状态：{clean(raw.get('state'))}）"
    preamble, entries = parsed if parsed is not None else _parse_window(item.rendered.text)
    refs = list(dict.fromkeys(proof.ref for proof in relation.proofs))
    context, shown = _context(entries, refs)
    if not context:
        return None
    found = set(scan_types(line, patterns))
    sentence_of = {window_line.ref: window_line.sentence_id for window_line in item.window.lines}
    return JudgeRequest(
        window_id=item.window.window_id,
        l3_key=item.cache_key,
        index=relation.index,
        text=f"【关系】{line}\n【原话】\n{preamble}\n{context}\n",
        basis=tuple(dict.fromkeys((proof.sentence_id, proof.packet_id, proof.revision) for proof in relation.proofs)),
        leftover=tuple(kind for kind in REDACTION_TYPES if kind in found),
        context=tuple(dict.fromkeys(sentence_of[ref] for ref in shown if ref in sentence_of)),
        prompt_version=version,
    )


def judge_requests(
    items: Sequence[Checked], patterns: Sequence[RedactionPattern], version: str = JUDGE_PROMPT_VERSION
) -> list[tuple[Checked, RelationCheck, JudgeRequest]]:
    """这一批要判的：每个 L4 核过的窗口里过了 R1 到 R6 的关系（照 version 那一版问）。"""
    found = []
    for item in items:
        if item.check is None or _unsendable(item):
            continue
        parsed = _parse_window(item.rendered.text)
        for relation in item.check.relations:
            request = build_request(item, relation, patterns, parsed, version=version)
            if request is not None:
                found.append((item, relation, request))
    return found


# ---------- 本机那一档 ----------


def local_answer(out: object) -> tuple[float | None, str, list[list[object]]]:
    """本机模型的回复 → (P(能), 回的第一个字, 前十个候选字和 logprob)。候选字里各取第一个“是”“否”，
    P = P(是) / (P(是) + P(否))（离线试的算法）；两个都没有、回的样子不对的，P 是 None。logprob 不是有限的数的不算。"""
    try:
        choice = out["choices"][0]  # type: ignore[index]
        answer = str((choice.get("message") or {}).get("content") or "").strip()[:ANSWER_CHARS]
        content = (choice.get("logprobs") or {}).get("content") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return None, "", []
    tops = content[0].get("top_logprobs") if content and isinstance(content[0], Mapping) else None
    yes = no = None
    kept: list[list[object]] = []
    for top in tops if isinstance(tops, list) else []:
        if not isinstance(top, Mapping):
            continue
        logprob = top.get("logprob")
        if isinstance(logprob, bool) or not isinstance(logprob, (int, float)) or not math.isfinite(logprob):
            continue
        token = str(top.get("token") or "").strip()
        kept.append([token[:ANSWER_CHARS], round(float(logprob), 6)])
        if token == "是" and yes is None:
            yes = float(logprob)
        elif token == "否" and no is None:
            no = float(logprob)
    if yes is None and no is None:
        return None, answer, kept
    a = math.exp(min(yes, 0.0)) if yes is not None else 0.0
    b = math.exp(min(no, 0.0)) if no is not None else 0.0
    return (a / (a + b) if a + b > 0 else None), answer, kept


@dataclass
class _Ask:
    """要问的一次：发过去的话（_Run 只看 text）、键、用这个判定的几条关系（窗口编号, L3 的键, 第几条）。"""

    text: str
    request: JudgeRequest
    key: str
    links: list[tuple[str, str, int]] = field(default_factory=list)


class LocalChat(Protocol):
    """本机模型（javmem.pipeline.preb.LocalModel，测试用 FakeLocal）：只连本机，没有密钥。"""

    name: str

    def chat(
        self, system: str, user: str, *, max_tokens: int, schema: Mapping[str, object] | None = None, logprobs: bool = False
    ) -> tuple[dict[str, object], float]: ...


_FAKE_HTTP = {  # FakeLocal 的几种 HTTP 错误：(状态码, 回的那一段)
    "http400": (400, '{"error": {"code": 400, "message": "the request exceeds the available context size"}}'),
    "http400-other": (400, '{"error": {"code": 400, "message": "unknown parameter"}}'),
    "http404": (404, "File Not Found"),
    "http401": (401, '{"error": {"code": 401, "message": "Invalid API Key"}}'),
}


class FakeLocal:
    """测试和 --fake 用的本机模型：不连网。script 按顺序给每次调用怎么回：一个 0 到 1 的数是这一次的 P(是)，
    "error" 是连不上，"http400" 是本机模型说这个请求超了上下文（HTTP 400），"http400-other" 是 400 却没说太长（不认请求里的
    参数这类），"http404"、"http401" 是地址、密钥不对，"other" 是回的不是“是 / 否”，一个 {候选字: logprob} 的字典是原样的前几个
    候选字（要 P 正好落在线上时用），也可以是 (system, user) -> 上面这几种 的函数；用完了一律 0.95。"""

    def __init__(self, script: Iterable[object] = (), *, name: str = FAKE_LOCAL_NAME) -> None:
        self.name = name
        self.script: deque[object] = deque(script)
        self.calls: list[tuple[str, str]] = []
        self.in_flight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def chat(
        self, system: str, user: str, *, max_tokens: int, schema: Mapping[str, object] | None = None, logprobs: bool = False
    ) -> tuple[dict[str, object], float]:
        with self._lock:
            self.calls.append((system, user))
            behaviour = self.script.popleft() if self.script else 0.95
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            if callable(behaviour):
                behaviour = behaviour(system, user)
            if behaviour == "error":
                raise urllib.error.URLError("假的本机模型连不上")
            if isinstance(behaviour, str) and behaviour in _FAKE_HTTP:
                code, body = _FAKE_HTTP[behaviour]
                raise urllib.error.HTTPError(  # type: ignore[arg-type]
                    "http://127.0.0.1/v1/chat/completions", code, "假的", None, io.BytesIO(body.encode("utf-8"))
                )
            if behaviour == "other":
                tops: list[dict[str, object]] = [{"token": "嗯", "logprob": -0.1}]
            elif isinstance(behaviour, Mapping):
                tops = [{"token": token, "logprob": logprob} for token, logprob in behaviour.items()]
            else:
                p = float(behaviour)  # type: ignore[arg-type]
                tops = [{"token": "是", "logprob": math.log(p)}] if p > 0 else []
                if p < 1:
                    tops.append({"token": "否", "logprob": math.log(1 - p)})
                tops.sort(key=lambda top: -float(top["logprob"]))  # type: ignore[arg-type]
            first = str(tops[0]["token"]) if tops else ""
            usage = {"prompt_tokens": (len(system) + len(user)) // 2, "completion_tokens": 1}
            logs = {"content": [{"token": first, "logprob": tops[0]["logprob"] if tops else 0.0, "top_logprobs": tops}]}
            return {"choices": [{"message": {"content": first}, "logprobs": logs}], "usage": usage}, 0.0
        finally:
            with self._lock:
                self.in_flight -= 1


@dataclass
class LocalRunResult:
    asked: int = 0  # 这一次真问了几次（回了的，连同不收的）
    cached: int = 0  # 存过的
    failed: int = 0  # 连不上的（下次接着判）
    no_answer: int = 0  # 回的不是“是 / 否”
    rejected: int = 0  # 本机模型说这个请求超了上下文（too_long）：再问也一样，当没把握；不存成判定，--retry-failed-judge 再问
    too_long_before: int = 0  # 以前说过太长、这次没再问的
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    stopped: str | None = None

    def describe(self) -> dict[str, object]:
        return {"asked": self.asked, "cached": self.cached, "failed": self.failed, "no_answer": self.no_answer,
                "rejected": self.rejected, "too_long_before": self.too_long_before, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "seconds": round(self.seconds, 1), "stopped": self.stopped}


class _LocalRun:
    """本机那一档：两路一起问，每次调用记账（标签是 tag-c1，模型写本机模型的名字），存判定。连着几次连不上就停下。"""

    def __init__(
        self,
        model: LocalChat,
        store: ResultStore,
        ledger: TokenLedger | None,
        options: ExtractOptions,
        log: Callable[[str], None] | None,
        profile: JudgeProfile = RELATIONS,
    ) -> None:
        self.model = model
        self.name = model.name
        self.store = store
        self.ledger = ledger
        self.options = options
        self.log = log
        self.profile = profile
        self.tag = f"{options.tag}-{profile.version}"  # 和认人认事的 -b4 一样：本机的另记一个标签，不算进付钱的预算
        self.system = profile.system()
        self.run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{profile.version[0]}{secrets.token_hex(3)}"  # c 关系、f 事实
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.streak = 0  # 连着连不上的次数
        self.long_streak = 0  # 连着说太长的次数（一次都没答上来以前算：多半是上下文开得太小）
        self.answered = 0  # 这一次答上来的次数
        self.workers = max(1, min(LOCAL_WORKERS, options.concurrency))
        self.result = LocalRunResult()

    def halt(self, reason: str) -> None:
        with self.lock:
            if self.result.stopped is None:
                self.result.stopped = reason
        self.stop.set()

    def _record(self, ask: _Ask, status: str, detail: str, usage: Mapping[str, object], seconds: float) -> None:
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0) if isinstance(details, Mapping) else 0
        tokens = {
            "input_tokens": _int(usage.get("prompt_tokens")),
            "output_tokens": _int(usage.get("completion_tokens")),
            "reasoning_tokens": 0,
            "cached_tokens": cached,
        }
        row = {
            "run_id": self.run_id,
            "window_id": f"{ask.request.window_id}#{self.profile.letter}{ask.request.index}",
            "cache_key": ask.key,
            "attempt": 1,
            "status": status,
            "detail": detail[:200],
            **tokens,
            "seconds": round(seconds, 3),
            "at": _now(),
        }
        if self.ledger is not None:  # 账先记：记不了账就不再问
            self.ledger.append({**row, "tag": self.tag, "purpose": self.profile.local_purpose, "model": self.name})
        self.store.record_judgment_call(row)
        with self.lock:
            self.result.input_tokens += tokens["input_tokens"]
            self.result.output_tokens += tokens["output_tokens"]
            self.result.seconds += seconds

    def work(self, ask: _Ask) -> None:
        if self.stop.is_set():
            return
        if self.options.deadline is not None and time.monotonic() >= self.options.deadline:
            self.halt("deadline")
            return
        started = time.monotonic()
        try:
            try:
                out, seconds = self.model.chat(self.system, ask.text, max_tokens=1, logprobs=True)
            except urllib.error.HTTPError as error:
                kind, seconds = _http_kind(error), time.monotonic() - started
                if kind == TRANSIENT:  # 5xx、408、429：本机模型那边一时不行，和连不上一样
                    self._unreachable(ask, f"http {error.code}", seconds)
                    return
                if kind == CONFIG:  # 地址、密钥、参数不对：每个请求都会这样。马上停下，什么都不存，窗口等着设置修好
                    self._record(ask, "config_error", f"http {error.code}", {}, seconds)
                    self.halt(f"local: 本机模型回 HTTP {error.code}，地址、密钥这些设置可能不对（{ENV_URL}、{ENV_MODEL}）")
                    return
                # 说这一个请求超了上下文：再问也一样。记 too_long（不存成判定），当没把握：local 模式照留，local+llm 交第二档；
                # 不然这个窗口一直等、包一直排着队。一次都没答上来就连着几次这样的，多半是上下文开得太小，停下
                self._record(ask, TOO_LONG, f"http {error.code}", {}, seconds)
                with self.lock:
                    self.streak = 0
                    self.result.asked += 1
                    self.result.rejected += 1
                    self.long_streak += 1
                    suspicious = self.answered == 0 and self.long_streak >= LOCAL_STOP_AFTER
                if suspicious:
                    self.halt(f"local: 一次都没答上来就连着 {LOCAL_STOP_AFTER} 次说太长，本机模型的上下文可能开得太小")
                return
            except _LOCAL_ERRORS as error:  # 连不上、断了、回的不是 JSON：这一次不判，下次接着判
                self._unreachable(ask, type(error).__name__, time.monotonic() - started)
                return
            with self.lock:
                self.streak = 0
                self.long_streak = 0
                self.answered += 1
            reply: Mapping[str, object] = out if isinstance(out, Mapping) else {}
            usage = reply.get("usage")
            usage = usage if isinstance(usage, Mapping) else {}
            p, answer, tops = local_answer(reply)
            self._record(ask, "ok" if p is not None else "no_answer", "", usage, seconds)
            # 前十个候选字和 logprob 存着，查 P 怎么算出来的
            self._save(ask, p, answer, json.dumps(tops, ensure_ascii=False), str(reply.get("model") or "") or None, usage, seconds)
            with self.lock:
                self.result.asked += 1
                if p is None:
                    self.result.no_answer += 1
        except (OSError, sqlite3.Error) as error:  # 账、结果库写不进去：整段停下，不能再问
            self.halt(f"store: 记不下来（{type(error).__name__}）")

    def _unreachable(self, ask: _Ask, detail: str, seconds: float) -> None:
        """连不上、一时不行的一次：记一笔，这一条下次接着判；连着几次就停下这一段。"""
        self._record(ask, "transport", detail, {}, seconds)
        with self.lock:
            self.result.failed += 1
            self.streak += 1
            streak = self.streak
        if streak >= LOCAL_STOP_AFTER:
            self.halt(f"local: 连着 {streak} 次连不上本机模型")

    def _save(
        self, ask: _Ask, p: float | None, answer: str, output: str, response_model: str | None, usage: Mapping[str, object],
        seconds: float,
    ) -> None:
        self.store.save_judgment(
            {
                "judge_key": ask.key,
                "tier": TIER_LOCAL,
                "prompt_version": self.profile.version,
                "model": self.name,
                "probability": p,
                "answer": answer,
                "reason": "",
                "output": output,
                "response_model": response_model,
                "input_tokens": _int(usage.get("prompt_tokens")),
                "output_tokens": _int(usage.get("completion_tokens")),
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "seconds": round(seconds, 3),
                "created_at": _now(),
            },
            ask.links,
            kind=self.profile.kind,
        )

    def run(self, asks: Sequence[_Ask]) -> LocalRunResult:
        self.store.start_run(
            self.run_id,
            _now(),
            {**self.options.describe(), "tag": self.tag, "purpose": self.profile.local_purpose, "model": self.name,
             "prompt": self.profile.version},
        )
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            for done, future in enumerate([pool.submit(self.work, ask) for ask in asks], start=1):
                future.result()
                if self.log is not None and (done % 50 == 0 or done == len(asks)):
                    self.log(f"{self.profile.label} 本机那一档：{done}/{len(asks)}")
        except BaseException:
            self.halt("interrupted")
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True)
            self.store.finish_run(self.run_id, _now(), self.result.describe())
        return self.result


def _int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


# ---------- 第二档 ----------


def parse_answer(content: str) -> dict[str, str]:
    """第二档的回复：是 JSON、只有“答案”“理由”两个字段、答案是三种之一。不对算格式错误，消息里不抄回复的内容。"""
    try:
        data = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        raise FormatError("回复不是 JSON") from None
    if not isinstance(data, dict) or set(data) != {"答案", "理由"}:
        raise FormatError("回复的字段不对")
    answer, reason = data["答案"], data["理由"]
    if not isinstance(answer, str) or not isinstance(reason, str):
        raise FormatError("回复的类型不对")
    if answer.strip() not in ANSWERS:
        raise FormatError("答案的取值不在范围里")
    parsed = {"答案": answer.strip(), "理由": reason.strip()[:REASON_CHARS]}
    try:
        json.dumps(parsed, ensure_ascii=False).encode("utf-8")  # 转义写出来的孤立代理写不进库
    except UnicodeEncodeError:
        raise FormatError("回复里有写不进库的字符") from None
    return parsed


class _JudgeRun(_Run):
    """L3 那一套调用的规矩（同时最多几路、限流退避、预算、记账、超时和格式错的重试），换掉输出结构、解析、存结果、记调用。"""

    purpose = LLM_PURPOSE
    schema = LLM_SCHEMA
    schema_name = LLM_SCHEMA_NAME

    def __init__(
        self,
        model: Model,
        store: ResultStore,
        ledger: TokenLedger | None,
        options: ExtractOptions,
        log: Callable[[str], None] | None,
        profile: JudgeProfile = RELATIONS,
    ) -> None:
        super().__init__(model, store, ledger, options, log, system=profile.system() + LLM_FORMAT)
        self.profile = profile
        self.purpose = profile.llm_purpose
        self.schema_name = profile.schema_name

    def parse(self, content: str, request: object) -> dict[str, object]:
        return dict(parse_answer(content))

    def save(self, window: Window, request: object, key: str, reply: ModelReply, parsed: Mapping[str, object], usage: Usage) -> None:
        assert isinstance(request, _Ask)
        self.store.save_judgment(
            {
                "judge_key": key,
                "tier": TIER_LLM,
                "prompt_version": self.profile.version,
                "model": self.model.cache_name,
                "probability": None,
                "answer": str(parsed["答案"]),
                "reason": str(parsed["理由"]),
                "output": reply.content,
                "response_model": reply.model,
                **usage.as_dict(),
                "seconds": reply.seconds,
                "created_at": _now(),
            },
            request.links,
            kind=self.profile.kind,
        )

    def record_call(self, row: Mapping[str, object]) -> None:
        self.store.record_judgment_call(row)


def _unsure(p: float | None, spec: JudgeSpec) -> bool:
    """local+llm 模式下本机没把握、要交给第二档的：P 在两条线中间，或者是空的。"""
    return spec.mode == CASCADE and (p is None or spec.low < p < (spec.high if spec.high is not None else 1.0))


# ---------- 判一批 ----------


@dataclass
class JudgeRunResult:
    relations: int  # 要判的关系
    local: LocalRunResult
    llm: RunResult | None = None  # 第二档这一次的调用（local 模式、没东西交的是 None）
    llm_cached: int = 0
    llm_failed_before: int = 0  # 以前重调也没用的，这次没再调（--retry-failed-judge 才重调）
    llm_unsent: int = 0  # 关系那一行打完码还扫得出来：不发

    @property
    def stopped(self) -> str | None:
        """这一段停下的原因。第二档停下的（密钥、额度、预算、记不下账）先报：本机连不上停下以后第二档照样跑，
        不能让本机那一句盖住（命令行照它定退出码）。本机那一句在 local.stopped。"""
        if self.llm is not None and self.llm.stopped is not None:
            return self.llm.stopped
        return self.local.stopped

    def describe(self) -> dict[str, object]:
        llm = None
        if self.llm is not None:
            llm = {status: self.llm.count(status) for status in (CACHED, DONE, FAILED, NOT_STARTED)}
            llm["stopped"] = self.llm.stopped  # type: ignore[assignment]
        return {"relations": self.relations, "local": self.local.describe(), "llm_calls": llm, "llm_cached": self.llm_cached,
                "llm_failed_before": self.llm_failed_before, "llm_unsent": self.llm_unsent}


def run_judge(
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
    """items：verify_windows() 跑出来的（关系要按别名表、主人的名字核过，和写账一样）。先本机那一档：存过的直接用，
    别的两路一起问；local+llm 模式再把没把握的交给第二档（llm_model，抽取用的那个模型），存过的、以前重调也没用的不再调。
    这一批里要问的话一样的只问一次。Ctrl+C：不再开始新的调用，正在跑的跑完。"""
    return run_requests(judge_requests(items, patterns, spec.version), spec, store, local_model=local_model, llm_model=llm_model,
                        ledger=ledger, options=options, log=log, profile=relation_profile(spec.version))


def run_requests(
    requests: Sequence[tuple[Checked, object, JudgeRequest]],
    spec: JudgeSpec,
    store: ResultStore,
    *,
    local_model: LocalChat,
    llm_model: Model | None = None,
    ledger: TokenLedger | None = None,
    options: ExtractOptions = ExtractOptions(),
    log: Callable[[str], None] | None = None,
    profile: JudgeProfile = RELATIONS,
) -> JudgeRunResult:
    """判一批要判的（关系、事实一样）：见 run_judge。profile 定用哪一份提示词、问话、记账的用途、判定连哪张表。"""
    now = _now()
    asks: dict[str, _Ask] = {}
    cached = too_long_before = 0
    for _, _, request in requests:
        key = request.key(TIER_LOCAL, spec.local)
        if key in asks:
            asks[key].links.append(request.link)
        elif store.judgment(key) is not None:
            store.link_judgment(key, [request.link], now, kind=profile.kind)
            cached += 1
        elif not options.retry_failed and store.last_judgment_failure(key) == TOO_LONG:
            too_long_before += 1  # 以前说过太长：再问也一样，加 --retry-failed-judge 才再问
        else:
            asks[key] = _Ask(request.text + profile.local_ask, request, key, [request.link])
    local = _LocalRun(local_model, store, ledger, options, log, profile)
    local.result.cached = cached
    local.result.too_long_before = too_long_before
    if asks:
        local.run(list(asks.values()))
    result = JudgeRunResult(len(requests), local.result)
    stopped = local.result.stopped
    # 本机模型连不上停下的，本机已经判过、落在中间的照样交第二档（不然本机一直不行时，新来的永远判不完）；
    # 记不下账、Ctrl+C、过了截止时间的不再开始
    if spec.mode != CASCADE or llm_model is None or (stopped is not None and not stopped.startswith("local:")):
        return result
    todo: dict[str, _Ask] = {}
    for item, _, request in requests:
        local_key = request.key(TIER_LOCAL, spec.local)
        stored = store.judgment(local_key)
        if stored is None:
            if store.last_judgment_failure(local_key) != TOO_LONG:
                continue  # 本机还没判：判了才知道交不交
        elif not _unsure(stored.probability, spec):
            continue
        if request.leftover:
            result.llm_unsent += 1
            continue
        key = request.key(TIER_LLM, spec.llm or llm_model.cache_name)
        if key in todo:
            todo[key].links.append(request.link)
        elif store.judgment(key) is not None:
            store.link_judgment(key, [request.link], now, kind=profile.kind)
            result.llm_cached += 1
        elif not options.retry_failed and store.last_judgment_failure(key) is not None:
            result.llm_failed_before += 1
        elif options.limit is None or len(todo) < options.limit:
            todo[key] = _Ask(request.text + profile.llm_ask, request, key, [request.link])
    if not todo:
        return result
    run = _JudgeRun(llm_model, store, ledger, options, log, profile)
    store.start_run(run.run_id, now, run.run_options(purpose=profile.llm_purpose, model=llm_model.cache_name, prompt=profile.version))
    outcomes: list[WindowOutcome] = []
    try:
        windows = {item.window.window_id: item.window for item, _, _ in requests}
        pool = ThreadPoolExecutor(max_workers=max(1, options.concurrency))
        try:
            jobs = []
            for ask in todo.values():
                # _Run 按窗口编号记账、留预算：一个窗口几条关系一起跑，编号带上第几条，免得互相顶掉
                window = replace(windows[ask.request.window_id],
                                 window_id=f"{ask.request.window_id}#{profile.letter}{ask.request.index}")
                jobs.append(pool.submit(run.work, window, ask, ask.key))
            for future in jobs:
                outcome = future.result()
                outcomes.append(outcome)
                run.progress(outcome, len(jobs))
        except BaseException:
            run.halt("interrupted")
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True)
    finally:
        counts = {status: sum(1 for outcome in outcomes if outcome.status == status) for status in (DONE, FAILED, NOT_STARTED)}
        store.finish_run(run.run_id, _now(), {"stopped": run.stopped, **counts})
    result.llm = RunResult(run.run_id, outcomes, run.stopped)
    return result


# ---------- 用存下的判定 ----------


def _decide(
    request: JudgeRequest, store: ResultStore, spec: JudgeSpec
) -> tuple[str | None, dict[str, object]] | tuple[None, None]:
    """一条关系照存下的判定：(挡下的原因，没挡是 None；判的经过)。还没判完的返回 (None, None)。

    steps 只记真回了判定的那几档（留痕一档一行）；第二档没发出去、回不了的记在 llm 里，不算一档。context 是发过去的每一行
    （证据和前后文）的句子编号：第二档的理由可能照着哪一句写，单条撤回那一句时隔离表那一行要找得到。"""
    local_key = request.key(TIER_LOCAL, spec.local)
    local = store.judgment(local_key)
    steps: list[dict[str, object]] = []
    info: dict[str, object] = {"version": request.prompt_version, "mode": spec.mode, "steps": steps,
                               "context": list(request.context)}
    if local is None:
        if store.last_judgment_failure(local_key) != TOO_LONG:
            return None, None  # 还没问、连不上、设置不对：等下次
        p = None  # 本机说这个请求太长、没答：当没把握（不算一档，不写留痕）
        info["local"] = TOO_LONG
    else:
        p = local.probability
        steps.append({"tier": TIER_LOCAL, "model": spec.local, "p": p, "answer": local.answer})

    def mark(verdict: str) -> None:
        if steps:
            steps[0]["verdict"] = verdict

    if p is not None and p <= spec.low:
        mark(UNSUPPORTED)
        return LOCAL_NO, {**info, "by": TIER_LOCAL, "verdict": UNSUPPORTED}
    if not _unsure(p, spec):
        verdict = SUPPORTED if p is not None else UNSURE  # local 模式：没把握说不能的都留
        mark(verdict)
        return None, {**info, "by": TIER_LOCAL, "verdict": verdict}
    mark(UNSURE)
    llm_name = str(spec.llm)
    if request.leftover:  # 关系那一行打完码还扫得出来：不发，当回不了
        return LLM_FAILED, {**info, "by": TIER_LLM, "verdict": UNSURE, "llm": "unredacted"}
    key = request.key(TIER_LLM, llm_name)
    stored = store.judgment(key)
    if stored is None:
        failure = store.last_judgment_failure(key)
        if failure is None:
            return None, None  # 还没问、这一次没问成（网络、限流、预算、格式错还没重试完）：等下次
        return LLM_FAILED, {**info, "by": TIER_LLM, "verdict": UNSURE, "llm": failure}
    verdict = LLM_VERDICT.get(stored.answer, UNSURE)  # 存下的是校验过的三种之一；手改过的库认不得的当拿不准
    steps.append({"tier": TIER_LLM, "model": llm_name, "verdict": verdict, "answer": stored.answer, "reason": stored.reason})
    code = None if verdict == SUPPORTED else LLM_CODE.get(stored.answer, LLM_UNSURE)
    return code, {**info, "by": TIER_LLM, "verdict": verdict}


HOLD, BLOCK = "hold", "block"  # apply_judgments 碰到还没判完的关系：整个窗口等着（写账）；记 C:not_judged 挡下（合并工具）
NOT_JUDGED = "not_judged"


def apply_judgments(
    items: Sequence[Checked], store: ResultStore, spec: JudgeSpec, patterns: Sequence[RedactionPattern], *, unjudged: str = HOLD
) -> list[Checked]:
    """按键取存下的判定：挡下的关系加上 C 的原因（进隔离表），判过的关系都记上判的经过（RelationCheck.judge）。不调模型。

    过了规则的关系还有没判完的：unjudged 是 HOLD（verify、write）时，这个窗口 check 换成 None、skipped 是 NO_JUDGE，L6 不写，
    包留着排队，等 extract 判完；是 BLOCK（合并工具）时，这一条记 C:not_judged 挡下（D125：没把握的不进）。合并工具照别名表加上
    这条合并记录重核，以前 R2 没过、没判过的关系可能这时才过；合并记录要写成了 extract 才看得到，写过的包也不再抽，等不来。
    不改传进来的对象。"""
    out: list[Checked] = []
    for item in items:
        check = item.check
        if check is None:
            out.append(item)
            continue
        if _unsendable(item):  # L3 不发的窗口带着规则的结果：不写，和 NOT_SENT 一样
            out.append(replace(item, check=None, skipped=NOT_SENT))
            continue
        parsed = _parse_window(item.rendered.text)
        relations: list[RelationCheck] = []
        waiting = False
        for relation in check.relations:
            request = build_request(item, relation, patterns, parsed, version=spec.version)
            if request is None:  # 没过规则的；拼不出原话的（证据不在正文里，R1 管着，碰不上）照原样
                relations.append(relation)
                continue
            code, info = _decide(request, store, spec)
            if info is None:
                if unjudged == HOLD:
                    waiting = True
                    break
                code, info = NOT_JUDGED, {"version": spec.version, "mode": spec.mode, "steps": [],
                                          "context": list(request.context), "by": None, "verdict": UNSURE}
            failures = list(relation.failures)
            if code is not None:
                failures.append(Finding(RULE, code))
                failures.sort(key=lambda finding: RULE_ORDER.index(finding.rule))
            relations.append(replace(relation, failures=failures, judge=info))
        if waiting:
            out.append(replace(item, check=None, skipped=NO_JUDGE))
            continue
        out.append(replace(item, check=replace(check, relations=relations)))  # 只换关系，事实、实体照旧
    return out


# ---------- 留痕 ----------


def audit_rows(item: Checked, now: str) -> list[tuple[object, ...]]:
    """这个窗口判过的关系，问过的每一档一行 audit_log：(check_kind, item_kind, item_id, trusted, basis, packet_id, revision,
    probability, created_at, tier, judge)。item_id 是“窗口编号#L3 键前 12 位#R第几条”（和事实说法的编号一个样子）。
    不抄原文、不抄理由。"""
    check = item.check
    if check is None:
        return []
    rows: list[tuple[object, ...]] = []
    for relation in check.relations:
        info = relation.judge
        steps = info.get("steps") if isinstance(info, Mapping) else None
        if not isinstance(steps, list) or not relation.proofs:
            continue
        basis = json.dumps(list(dict.fromkeys(proof.sentence_id for proof in relation.proofs)), ensure_ascii=False)
        first = relation.proofs[0]
        item_id = f"{item.window.window_id}#{item.cache_key[:12]}#R{relation.index}"
        for step in steps:
            if not isinstance(step, Mapping) or step.get("tier") not in AUDIT_TIERS:
                continue
            p = step.get("p")
            rows.append((
                CHECK_KIND,
                "relation",
                item_id,
                str(step.get("verdict") or UNSURE),
                basis,
                first.packet_id,
                first.revision,
                float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None,
                now,
                AUDIT_TIERS[str(step["tier"])],
                f"{step.get('model')} {JUDGE_PROMPT_VERSION}",
            ))
    return rows


def write_audit(conn: sqlite3.Connection, item: Checked, now: str) -> int:
    """写这个窗口的留痕（调用方开着事务）：一模一样的一行（同一条关系、同一档、同一个模型、判成一样、P 一样、证据一样）
    已经有了就不再写，判的不一样了另写一行，旧的留着。返回写了几行。"""
    written = 0
    for row in audit_rows(item, now):
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


# ---------- 数 ----------


def plan_judge(
    items: Sequence[Checked], store: ResultStore, spec: JudgeSpec, patterns: Sequence[RedactionPattern], *,
    retry_failed: bool = False, limit: int | None = None,
) -> dict[str, object]:
    """--dry-run 用：要判几条、本机那一档存过的（这一批里话一样的也算）、要问的，第二档（只算本机已经判过、落在中间的）
    存过的、要调的（--limit 封顶，和真跑一样）、多少字。不调模型。"""
    return plan_requests(judge_requests(items, patterns, spec.version), store, spec, retry_failed=retry_failed, limit=limit,
                         profile=relation_profile(spec.version))


def plan_requests(
    requests: Sequence[tuple[Checked, object, JudgeRequest]], store: ResultStore, spec: JudgeSpec, *,
    retry_failed: bool = False, limit: int | None = None, profile: JudgeProfile = RELATIONS,
) -> dict[str, object]:
    """数法照 plan_judge，判关系、判事实共用（总条数放在 relations 键下）。"""
    plan = {"relations": 0, "local_cached": 0, "local_to_call": 0, "local_too_long_before": 0, "llm_cached": 0,
            "llm_failed_before": 0, "llm_unsent": 0, "llm_to_call": 0, "llm_chars": 0, "llm_unknown": 0, "llm_over_limit": 0}
    local_seen: set[str] = set()
    llm_seen: set[str] = set()
    for _, _, request in requests:
        plan["relations"] += 1
        key = request.key(TIER_LOCAL, spec.local)
        stored = store.judgment(key)
        too_long = stored is None and store.last_judgment_failure(key) == TOO_LONG
        if stored is not None or key in local_seen:
            plan["local_cached"] += 1
        elif too_long and not retry_failed:
            plan["local_too_long_before"] += 1  # 以前说过太长：当没把握，不再问
        else:
            local_seen.add(key)
            plan["local_to_call"] += 1
        if spec.mode != CASCADE:
            continue
        if stored is None and not (too_long and not retry_failed):
            plan["llm_unknown"] += 1  # 本机还没判，交不交第二档判了才知道
            continue
        if stored is not None and not _unsure(stored.probability, spec):
            continue
        if request.leftover:
            plan["llm_unsent"] += 1
            continue
        second = request.key(TIER_LLM, str(spec.llm))
        if store.judgment(second) is not None or second in llm_seen:
            plan["llm_cached"] += 1
        elif not retry_failed and store.last_judgment_failure(second) is not None:
            plan["llm_failed_before"] += 1
        elif limit is not None and plan["llm_to_call"] >= limit:
            plan["llm_over_limit"] += 1
        else:
            llm_seen.add(second)
            plan["llm_to_call"] += 1
            plan["llm_chars"] += len(request.text) + len(profile.llm_ask)
    return plan


def summarize_judgments(items: Sequence[Checked]) -> dict[str, object]:
    """apply_judgments 以后的数：判了几条关系、留下几条、挡下几条（按原因）、本机那一档判成什么、第二档答了什么、
    第二档没判成的（没发、回不了）、还在等判的窗口。不抄原文。"""
    judged = kept = 0
    by_code: Counter[str] = Counter()
    local: Counter[str] = Counter()
    llm: Counter[str] = Counter()
    llm_missing: Counter[str] = Counter()
    for item in items:
        if item.check is None:
            continue
        for relation in item.check.relations:
            info = relation.judge
            if not isinstance(info, Mapping):
                continue
            judged += 1
            codes = [finding.code for finding in relation.failures if finding.rule == RULE]
            if codes:
                by_code[codes[0]] += 1
            elif relation.passed:
                kept += 1
            for step in info.get("steps") or []:  # type: ignore[union-attr]
                if not isinstance(step, Mapping):
                    continue
                if step.get("tier") == TIER_LOCAL:
                    local[str(step.get("verdict"))] += 1
                else:
                    llm[str(step.get("answer") or "")] += 1
            if info.get("llm"):
                llm_missing[str(info["llm"])] += 1
    return {
        "judged": judged,
        "kept": kept,
        "blocked": sum(by_code.values()),
        "blocked_by_code": dict(sorted(by_code.items())),
        "local": dict(sorted(local.items())),
        "llm": dict(sorted(llm.items())),
        "llm_not_answered": dict(sorted(llm_missing.items())),
        "windows_waiting": sum(1 for item in items if item.skipped == NO_JUDGE),
    }


__all__ = [
    "CASCADE",
    "CHECK_KIND",
    "FAKE_LOCAL_NAME",
    "JUDGE_PROMPT_SHA256",
    "JUDGE_PROMPT_VERSION",
    "JUDGE_PROMPTS",
    "LOCAL",
    "NO_JUDGE",
    "OFF",
    "PART_OF_JUDGE_VERSION",
    "RELATIONS",
    "RELATIONS_C2",
    "FakeLocal",
    "JudgeProfile",
    "JudgeRequest",
    "JudgeRunResult",
    "JudgeSettings",
    "JudgeSpec",
    "apply_judgments",
    "audit_rows",
    "build_request",
    "checked_by",
    "judge_fingerprint",
    "judge_key",
    "judge_requests",
    "judge_version_for",
    "load_judge_prompt",
    "local_answer",
    "parse_answer",
    "plan_judge",
    "plan_requests",
    "relation_profile",
    "run_judge",
    "run_requests",
    "split_checked_by",
    "summarize_judgments",
    "write_audit",
]
