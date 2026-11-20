"""合并卡片上模型的看法（J3.6，D100 第 8 条）：只当参考，不算数，合不合都是人点了才算（04 p9）。

- **什么时候调：** `javmem entities --show` 出一条提议的卡片时调一次。结果按“提议里的实体、送出去的文字、提示词版本、
  模型”存在账本里（merge_judgments），同样的再出卡片不重调。列清单（--proposals）时不调。
- **送什么：** 每个实体的名字、类型、别的叫法、事实的条数和起止日子、两句原话。都只用不是 private 的包里的：
  名字、叫法要在不是 private 的包里有出处（标准名没有的用有出处的叫法代替），原话不送 private 的包里的句子；
  主人的名字（主人的名字的配置）换成“主人”；整段按打码模式表打一遍，还扫得出来的不发。有实体一个能送的名字都没有的，不发。
- **要它回什么：** JSON `{"same": "yes" | "no" | "unsure", "reason": "一句话"}`，用结构化输出，回来再校验一遍；
  格式错重试一次，还错就在卡片上写“模型没给出看法”。
- **调用的规矩和 L3 一样**（extract._Run）：用抽取在用的那个模型（JAVMEM_LLM_*），碰到限流退避，超时只再试一次；
  每次调用的输入、输出 token 记进 token 账（标签默认 javmem-merge，用途 merge_judgment），算在总的额度里。
  开调前看 token 账里一共用了多少（各个标签加起来），加上这一次的估计超过额度的不调，卡片上写“没调模型（额度不够）”。
- **撤回、彻底删除：** 存下的看法里理由是模型从原话写的，送过的句子撤回了、包撤回了或者删了，这一条删掉（forget_judgments）。

提示词在 prompts/ 下，带版本号；改了提示词或输出结构要换版本号，也要改 JUDGE_PROMPT_SHA256。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from importlib import resources
from types import SimpleNamespace

from javmem.llm import ModelReply, Usage
from javmem.pipeline.extract import DONE, ExtractOptions, FormatError, Model, _Run
from javmem.redaction import REDACTION_TYPES, RedactionPattern, redact, scan_types
from javmem.timeutil import iso
from javmem.usage import TokenLedger

JUDGE_PROMPT_VERSION = "m1-v1"
JUDGE_PROMPT_SHA256 = "71b41816daf0d759662c67896ebb0a46bf306207b91e62e48e357c99664670a1"
JUDGE_SCHEMA_NAME = "javmem_m1_v1"
PURPOSE = "merge_judgment"
TAG = "javmem-merge"
SAME = ("yes", "no", "unsure")
SAME_TEXT = {"yes": "像同一个", "no": "不像同一个", "unsure": "拿不准"}
QUOTES_PER_ENTITY = 2
OUTPUT_ESTIMATE = 1000  # 开调前估这一次要多少：输入按字数估，输出按这么多
JUDGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["same", "reason"],
    "properties": {"same": {"type": "string", "enum": list(SAME)}, "reason": {"type": "string"}},
}

# 卡片上那一行的几种样子
OK = "ok"
CACHED = "cached"
NOT_SENT = "not_sent"
BUDGET = "budget"
FAILED = "failed"
OFF = "off"


def load_judge_prompt(version: str = JUDGE_PROMPT_VERSION) -> str:
    text = (resources.files("javmem.pipeline") / "prompts" / f"{version}.md").read_text(encoding="utf-8")
    return text.replace("\r\n", "\n")


def judge_fingerprint(version: str = JUDGE_PROMPT_VERSION) -> str:
    """提示词连同输出结构的指纹。"""
    text = load_judge_prompt(version) + "\n" + json.dumps(JUDGE_SCHEMA, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Sendable:
    """一个实体能送出去的：名字、类型、别的叫法、事实的条数和起止日子、原话 (句子编号, 包号, 日子, 说话人, 原句)。"""

    name: str
    type: str
    aliases: tuple[str, ...]
    facts: int
    first: str | None
    last: str | None
    quotes: tuple[tuple[str, str, str, str, str], ...]


@dataclass(frozen=True)
class JudgeRequest:
    proposal_id: str
    entities: tuple[str, ...]
    text: str
    sentences: tuple[str, ...]
    packets: tuple[str, ...]
    leftover: tuple[str, ...] = ()

    def key(self, model: str, prompt_version: str = JUDGE_PROMPT_VERSION) -> str:
        sha = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        parts = ("javmem-merge-judgment", *sorted(self.entities), sha, prompt_version, model)
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass
class Judgment:
    status: str
    same: str | None = None
    reason: str = ""
    detail: str = ""
    usage: Usage = field(default_factory=Usage)

    def line(self) -> str:
        """卡片上那一行：单独一行，标着只当参考。"""
        head = "模型的看法（只当参考，不算数）："
        if self.same is not None:
            return f"{head}{SAME_TEXT[self.same]}。理由：{self.reason}"
        text = {
            BUDGET: "没调模型（额度不够）",
            NOT_SENT: f"没调模型（{self.detail}）",
            OFF: "没调模型（这次不调）",
        }.get(self.status, f"模型没给出看法（{self.detail}）" if self.detail else "模型没给出看法")
        return head + text


# ---------- 送什么 ----------


def _owner_patterns(owner_keys: Collection[str]) -> list[re.Pattern[str]]:
    """主人的名字（比对写法）在文字里的样子：字之间可以有空白，不分大小写。"""
    found = []
    for key in sorted(owner_keys, key=len, reverse=True):
        if not key:
            continue
        body = r"\s*".join(re.escape(char) for char in key)
        if key[0].isascii() and key[0].isalnum():
            body = r"(?<![A-Za-z0-9])" + body
        if key[-1].isascii() and key[-1].isalnum():
            body += r"(?![A-Za-z0-9])"
        found.append(re.compile(body, re.IGNORECASE))
    return found


def _clean(text: str) -> str:
    """一行里不能有换行和我们的标注括号：伪造不了别的行。"""
    return " ".join(unicodedata.normalize("NFKC", text).replace("【", "[").replace("】", "]").split())


def build_request(
    proposal_id: str,
    entities: Mapping[str, Sendable | None],
    patterns: Sequence[RedactionPattern],
    owner_keys: Collection[str],
) -> JudgeRequest | str:
    """这条提议要发去问模型的文字。发不了的返回原因（卡片上写“没调模型（原因）”）。"""
    if any(item is None for item in entities.values()):
        return "有实体的名字只在 private 的包里出现过"
    owners = _owner_patterns(owner_keys)
    blocks = []
    sentences: list[str] = []
    packets: list[str] = []
    for number, (entity_id, item) in enumerate(entities.items(), start=1):
        assert item is not None
        aliases = [alias for alias in item.aliases if alias != item.name]
        days = f"，{item.first} 到 {item.last}" if item.first and item.last and item.first != item.last else (f"，{item.first}" if item.first else "")
        head = f"【实体 {number}】名字：{_clean(item.name)}；类型：{item.type}"
        if aliases:
            head += f"；别的叫法：{'、'.join(_clean(alias) for alias in aliases)}"
        head += f"；事实 {item.facts} 条{days}"
        lines = [head]
        for index, (sentence_id, packet_id, day, speaker, text) in enumerate(item.quotes[:QUOTES_PER_ENTITY], start=1):
            lines.append(f"{number}.{index}（{day}，{_clean(speaker)}）{_clean(text)}")
            sentences.append(sentence_id)
            packets.append(packet_id)
        blocks.append("\n".join(lines))
    text = "下面这几个实体是不是同一个？\n\n" + "\n\n".join(blocks) + "\n"
    for pattern in owners:
        text = pattern.sub("主人", text)
    text, _ = redact(text, patterns)
    found = set(scan_types(text, patterns))
    leftover = tuple(kind for kind in REDACTION_TYPES if kind in found)
    return JudgeRequest(proposal_id, tuple(entities), text, tuple(dict.fromkeys(sentences)), tuple(sorted(set(packets))), leftover)


# ---------- 回来的 ----------


def parse_judgment(content: str) -> dict[str, object]:
    """模型的回复：是 JSON、字段对、same 是三个之一、理由不是空的。不对算格式错误，消息里不抄回复的内容。"""
    try:
        data = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        raise FormatError("回复不是 JSON") from None
    if not isinstance(data, dict) or set(data) != {"same", "reason"}:
        raise FormatError("回复的字段不对")
    if data["same"] not in SAME or not isinstance(data["reason"], str) or not data["reason"].strip():
        raise FormatError("回复的取值不对")
    try:
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise FormatError("回复里有写不进库的字符") from None
    return {"same": data["same"], "reason": data["reason"].strip()[:500]}


def cached(conn: sqlite3.Connection, key: str) -> Judgment | None:
    row = conn.execute("SELECT same, reason FROM merge_judgments WHERE judgment_key = ?", (key,)).fetchone()
    return Judgment(CACHED, row["same"], row["reason"]) if row is not None else None


def _now() -> str:
    return iso(datetime.now(timezone.utc).astimezone())


class _JudgeRun(_Run):
    """L3 那一套调用的规矩，换掉输出结构、解析、存结果、记调用这几处。在调用它的线程里跑，结果写进账本。"""

    purpose = PURPOSE
    schema = JUDGE_SCHEMA
    schema_name = JUDGE_SCHEMA_NAME

    def __init__(self, conn: sqlite3.Connection, model: Model, ledger: TokenLedger | None, options: ExtractOptions) -> None:
        super().__init__(model, None, ledger, options, None, system=load_judge_prompt())  # type: ignore[arg-type]
        self.conn = conn

    def parse(self, content: str, request: object) -> dict[str, object]:
        return parse_judgment(content)

    def save(self, window: object, request: object, key: str, reply: ModelReply, parsed: Mapping[str, object], usage: Usage) -> None:
        assert isinstance(request, JudgeRequest)
        self.conn.execute(
            "INSERT OR REPLACE INTO merge_judgments (judgment_key, proposal_id, entities, sentences, packets, prompt_version, model,"
            " same, reason, input_tokens, output_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                request.proposal_id,
                json.dumps(list(request.entities)),
                json.dumps(list(request.sentences), ensure_ascii=False),
                json.dumps(list(request.packets), ensure_ascii=False),
                JUDGE_PROMPT_VERSION,
                self.model.cache_name,
                parsed["same"],
                parsed["reason"],
                usage.input_tokens,
                usage.output_tokens,
                _now(),
            ),
        )

    def record_call(self, row: Mapping[str, object]) -> None:  # 调用记在 token 账里就够了
        return None


def judge(
    conn: sqlite3.Connection,
    request: JudgeRequest,
    model: Model,
    *,
    ledger: TokenLedger | None,
    budget: int | None,
    tag: str = TAG,
    options: ExtractOptions | None = None,
) -> Judgment:
    """问一次模型（存过的不再问）。模型说什么都只是卡片上的一行，这里不动别的。"""
    key = request.key(model.cache_name)
    found = cached(conn, key)
    if found is not None:
        return found
    if request.leftover:
        return Judgment(NOT_SENT, detail="打码没打干净")
    options = replace(options or ExtractOptions(concurrency=1), tag=tag, budget=None)  # 额度按总数在上面看
    system = load_judge_prompt()
    if ledger is not None and budget is not None:
        estimate = int((len(system) + len(request.text)) * options.tokens_per_char) + OUTPUT_ESTIMATE
        if ledger.used() + estimate > budget:
            return Judgment(BUDGET)
    run = _JudgeRun(conn, model, ledger, options)
    outcome = run.work(SimpleNamespace(window_id=request.proposal_id), request, key)  # type: ignore[arg-type]
    if outcome.status != DONE:
        return Judgment(FAILED, detail=outcome.detail or outcome.status, usage=outcome.usage)
    found = cached(conn, key)
    assert found is not None
    return Judgment(OK, found.same, found.reason, usage=outcome.usage)


def forget_judgments(conn: sqlite3.Connection) -> int:
    """撤回、彻底删除以后：送过的句子撤回了（或者原文索引里已经没有了）、包撤回了或者删了的看法删掉。不自己开事务。"""
    rows = conn.execute("SELECT judgment_key, sentences, packets FROM merge_judgments").fetchall()
    doomed = []
    for row in rows:
        packets = json.loads(row["packets"])
        gone = any(
            conn.execute(
                "SELECT 1 FROM packets WHERE packet_id = ?1 AND (withdrawn_at IS NOT NULL OR purged_at IS NOT NULL)"
                " UNION ALL SELECT 1 FROM withdrawn_packets WHERE packet_id = ?1 LIMIT 1",
                (packet_id,),
            ).fetchone()
            for packet_id in packets
        )
        if not gone:
            for sentence_id in json.loads(row["sentences"]):
                found = conn.execute("SELECT withdrawn_at FROM raw_sentences WHERE sentence_id = ?", (sentence_id,)).fetchone()
                if found is None or found[0] is not None:
                    gone = True
                    break
        if gone:
            doomed.append(row["judgment_key"])
    for key in doomed:
        conn.execute("DELETE FROM merge_judgments WHERE judgment_key = ?", (key,))
    return len(doomed)
