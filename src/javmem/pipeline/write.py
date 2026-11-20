"""L6 写账：L4 核过、L5 合并对齐的结果写进账本。一个窗口一个事务，这个窗口的包写完了，extraction 改成 done。

写账的单位是一处说法（fact_claims）：L3 在某个窗口抽出、过了 L4 的一条事实。事实（facts）是同一事实的键
（merge_key）一样的说法合起来的，它的每一列、证据（evidence）、涉及的实体（fact_entities）、向量（fact_vectors）
都从它的说法算出来；判新旧按组（group_key）、按每一句重算。所以：

- **重写一个窗口：** 先删掉这个窗口以前写的、证据全在这个窗口正文的包里的说法，以及这几个包的隔离、别名的出处，
  再写，受影响的事实和组重算。中途断了，这个窗口的事务整个撤销，包还是 queued，下次接着写，结果一样。同一个结果库的键、
  同一版 L4 已经写过的窗口不重写；取窗口以后包换了版本的不写。
- **先后：** 主体定了以后，事实、证据、新旧和写的先后无关。主体靠实体对齐：按名字对齐和先后无关；按别名对齐要看
  写的时候已经有哪些别名，所以先出现昵称、后出现全名的，按日期写是两个实体，倒过来写是一个。一批里的窗口按正文最早
  那句话的时间写（对话和邮件一起排），没用了的实体删掉，所以整本重建几次都一样，和按时间一批一批写的也一样。
- **包重发（新 revision）：** 收件换版本的那个事务里调 remove_revision，删掉证据来自旧版本的说法、旧版本的别名出处
  和隔离，重算受影响的事实和组（intake.clear_derived_for_replacement）；删掉的说法里有别的包的证据的，那些包改回
  queued。撤回（J2.11）也可以照这样做。
- **冻结的事实不随重写、重发删掉：** 说法没了也留着，状态不动，等主人处理（J2.12）。

**向量缺了不卡住：** 写事实时这段文字的向量还没有（换了模型、服务一直拒这一条、服务暂时调不通），先不写向量，
写完这一批再补（fill_vectors）；补不上的下次再补。javmem write 每次开头、结尾都补一遍。

隔离表一个窗口重写时也先清掉：没过 L4 的事实（item_kind fact）、过了的事实上去掉的证据（evidence）、
L3 标了问题的实体（entity）、没过 L4 的关系（relation，J4.3）、过了的关系上去掉的证据（relation_evidence），
payload 是 L3 的原样加上没过的原因。

**关系（J4.4）：** 过了 L4 的关系照事实的样子写：一处说法一行 relation_claims，关系（relations，一段一行）、
关系证据（relation_evidence）从说法算（分段、合成的算法在 javmem.pipeline.merge 的“关系”一节）。两头用这个窗口写事实时
对好的实体；写不进去的（一头不建实体、两头是同一个）进隔离表，规则 L5。重写、重发、撤回、改指这些清理的地方，关系的说法
和事实的说法一起清，受影响的关系按同一条关系的键（identity_key）重算。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path

from javmem.config import ConfigError, Settings
from javmem.embedding import EMBEDDING_DIM, EmbedStats, Embedder, EmbeddingError, RecordCall, embed_all
from javmem.ledger import transaction
from javmem.packet.model import ORIGIN_SAID, ROLE_BOT, ROLE_OWNER
from javmem.pipeline.extract import ExtractOptions
from javmem.pipeline.merge import (
    FIRST_PERSON,
    KINSHIP,
    L3_RELATION_STATES,
    L5_VERSION,
    ONE_AT_A_TIME,
    OWNER_NAME,
    OWNER_NAMES,
    RELATION_RULE_PREFIX,
    SECOND_PERSON,
    DIRECTIVE_PREFIX,
    SHELL_PREFIX,
    TimelineSegment,
    ClaimView,
    GroupFact,
    RelationClaimView,
    RelationSummary,
    alias_id_of,
    base_status,
    date_bounds,
    entity_id_of,
    fact_id_of,
    load_owner_names,
    NameParts,
    name_parts,
    owner_by_name,
    group_key,
    latest,
    merge_key,
    name_key,
    relation_group_key,
    relation_id_of,
    relation_identity,
    relation_merge_key,
    relation_timeline,
    representative,
    scope_key,
    succession,
    summarize_segment,
    usable_alias,
)
from javmem.zh.lexicon import EMPTY as EMPTY_LEXICON
from javmem.zh.lexicon import BOT, Lexicon, load_lexicon
from javmem.pipeline import answers, correct, precheck
from javmem.pipeline.normalize import ATTACHMENT, L1_VERSION, ORIGINAL_KINDS, NormalizedPacket, Scope, quote_copies
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache, text_sha256
from javmem.pipeline.relcheck import DECIDED_BY as RELATION_DECIDED_BY
from javmem.pipeline.relcheck import PART_OF
from javmem.pipeline.relcheck import RULES as RELATION_RULES
from javmem.pipeline.relcheck import RelationCheck
from javmem.pipeline.reljudge import write_audit
from javmem.pipeline.verify import L4_VERSION, NO_RECHECK, NOT_SENT, RULES, Checked, FactCheck, Proof, fold_map, sentence_times
from javmem.pipeline.window import Window
from javmem.raw.index import copy_key, sentence_hash
from javmem.raw.sentences import MASKED
from javmem.soft import rules as soft_rules
from javmem.soft import words as soft_words
from javmem.timeutil import epoch, iso, parse_aware

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOCK_FILE = ".write.lock"

# 待确认的决定（review_queue.decision）
KEEP_FROZEN = "keep_frozen"  # 留原值；证据都撤回了的冻结事实：放回来留着
KEEP_NEW = "keep_new"  # 用新值：解除冻结，照常按新旧；原值是主人原话的（D204，没冻结）：记着这一对照外部换
KEEP_BOTH = "keep_both"  # 两边都留（D6：30 天没人处理也这样）
KEEP_HIDDEN = "keep_hidden"  # 证据都撤回了的冻结事实：接着藏着，不删也不放回来
DROP = "drop"  # 证据都撤回了的冻结事实：删掉
UNFROZEN = "unfrozen"  # 主人解除冻结时，和它有关的冲突这样关掉（定过的也补一行，再冻结时重新问）
CLOSED_BY_FREEZE = "closed_by_freeze"  # 冻结新说法时自动关掉的冲突，claims 里记这一笔（D73 第 19 条）
RESUPPORTED = "resupported"  # 证据都撤回了的冻结事实又有了新说法，放回来了，这条待确认自动关掉
EVIDENCE_WITHDRAWN = "evidence_withdrawn"  # 待确认 other 的一种：冻结事实的证据都撤回了
SAME_TIME = "same_time"  # 待确认 other 的一种：同一时间一个的关系，同一句话里说了两个（R12：A 或 B，J4.4）
PART_OF_DEPTH = 5  # 负责的“代表哪一家”顺着隶属于最多往上（重算时往下）找几层（D215）
SOFT_CHANGE = soft_rules.REVIEW_KIND  # 软变化先挡住、等判的（J4.4 第 4 段）
MANUAL_REVIEW = "manual_review"  # 判断 C 拿不准的关系，等主人说留不留（J4.4，主会话 11-17）；进图前检查转人工的事实（J5.3）
# 判断 C 的这两种原因挪进待确认；没判过的、有把握说不能的照旧进隔离表。local_no_answer 是进图前检查（J5.3）开着时 luna 线
# 财务投资的本机没答上来（D22），只有开了才有
JUDGE_HOLD = ("llm_unsure", "llm_failed", precheck.LOCAL_NO_ANSWER)
REVIEW_TRUSTED = {KEEP_NEW: "keep", DROP: "drop", KEEP_HIDDEN: "hidden"}  # 主人定进图前检查转人工的那几条：留痕的 trusted
SOFT_VERDICTS = ("A", "B", "C", "D", "E")  # 主人能答的：和测试题一样六选一，F（判断不了）不用答
_DECISIONS = {
    "frozen_conflict": (KEEP_FROZEN, KEEP_NEW, KEEP_BOTH),
    "ambiguous_entity": (KEEP_BOTH,),
    EVIDENCE_WITHDRAWN: (KEEP_FROZEN, DROP, KEEP_HIDDEN),
    SAME_TIME: (KEEP_BOTH,),
    SOFT_CHANGE: SOFT_VERDICTS,
    MANUAL_REVIEW: (KEEP_NEW, DROP, KEEP_HIDDEN),  # 留下（照常算）、不要（删掉，以后重写窗口照旧进隔离表）、接着搁着
    # 纠正（J4.6）：照纠正改（用新值）、不改（接着挡着，旧的照旧）
    correct.CORRECTION: (KEEP_NEW, KEEP_HIDDEN),
    correct.UNMATCHED: (KEEP_NEW, KEEP_HIDDEN),
    correct.OTHER: (KEEP_NEW, KEEP_HIDDEN),  # 只说了新的那一条：旧的不算了（结束）、还算（不改）
}
_EXPIRE = {
    "frozen_conflict": KEEP_BOTH, "ambiguous_entity": KEEP_BOTH, EVIDENCE_WITHDRAWN: KEEP_HIDDEN, SAME_TIME: KEEP_BOTH,
    SOFT_CHANGE: soft_rules.EXPIRE_ACTION,  # 30 天没人管当成不改（D151）：旧的照旧，新说法留成打算
    MANUAL_REVIEW: KEEP_HIDDEN,  # 30 天没人管：接着搁着，不现行、不再提醒（D125：没把握的不进），不自动留也不删
    # 纠正挡着的 30 天没人管：接着挡着，旧的照旧，不再提醒（主人 11-17 第 3 题 B）；抽取没抽出来的纠正、只说了新的那一条
    # 一样，不替主人改
    correct.CORRECTION: KEEP_HIDDEN,
    correct.UNMATCHED: KEEP_HIDDEN,
    correct.OTHER: KEEP_HIDDEN,
}


def _claim_id(item: Checked, relation: RelationCheck) -> str:
    """一处关系说法的编号：窗口、这一份抽取结果、第几处（同一个窗口照同一份结果重写，编号不变）。"""
    return f"{item.window.window_id}#{item.cache_key[:12]}#{relation.index}"


def _judge_hold(relation: RelationCheck) -> str | None:
    """判断 C 拿不准的关系（过了 R1 到 R6，只有 C 的一条原因，是 llm_unsure 或者 llm_failed）：返回那个原因。"""
    failures = list(relation.failures)
    if len(failures) == 1 and failures[0].rule == "C" and failures[0].code in JUDGE_HOLD:
        return failures[0].code
    return None


def _review_type(kind: str, claims: object) -> str:
    """待确认的种类，other 里再分一层：证据都撤回了的冻结事实、冻结关系；同一时刻两个的关系（J4.4）。manual_review 这一种再分出
    纠正的两种（J4.6）：财务投资的纠正先挡住的（correction）、抽取没抽出来的纠正（correction_unmatched）。"""
    if kind == "other" and isinstance(claims, Mapping) and claims.get("reason") in (EVIDENCE_WITHDRAWN, SAME_TIME):
        return str(claims["reason"])
    if kind == MANUAL_REVIEW and isinstance(claims, Mapping) and claims.get("reason") in correct.REASONS:
        return str(claims["reason"])
    return kind
L5_RULES = {
    "L5-merge": "同一可见范围、主体、属性和值的说法合成一条事实，证据合在一起；值里数的标点留着；值只是一个金额或日期的按统一写法比",
    "L5-supersede": "同一可见范围、主体、属性，值不同：按每一句，后说的替代先说的；同一时刻的、多值属性的、冻结的不替代；"
    "外部内容后来说的不替代主人原话说的，进待确认（D204）",
    "L5-entity": "实体按名字加类型对齐，对不上的按同一可见范围的别名，再按名字里拆出来的叫法、别名表里同组的名字，"
    "对上几个的进待确认；泛称不建实体；别名表第二版的组对到标准名算出来的编号，不建实体的名字照泛称办，"
    "按别名找到的候选里去掉记成不是同一个的",
    # 关系（J4.4）：规则编号都以 RELATION_RULE_PREFIX 开头，打分认这几条看这本账是不是写关系的代码写的
    f"{RELATION_RULE_PREFIX}-merge": "同一可见范围、主体、关系、客体（亲属还有具体说法）的说法合成一条关系，证据合在一起；"
    "两头照事实的实体对齐；一头不建实体（泛称、一个字的名字、别名表里不建实体的）、两头对成同一个的进隔离表",
    f"{RELATION_RULE_PREFIX}-segment": "中间写了结束的，后来又说起是另一段（D115、卡 3 的 D101）；后来说起的开始日子不晚于前一段的"
    "结束，归那一段；说的是现在这一段开始以前就结束了的，另起一段；没写开始日子又说它结束了的，结束日子写法和已经结束的哪一段一样，"
    "归那一段（J4.6）",
    # J4.6 对话里纠正关系（D7；主人 11-17 选择卡定的五题）
    f"{RELATION_RULE_PREFIX}-correction": "主人在对话里纠正记着的关系（原话、纠正词，软变化优先）：纠正掉的那一段记着是哪一句，原话"
    "没写哪天的结束、开始日子空着；财务投资分区里的先挡住问主人，30 天没人管接着挡着；Bot 当场说错、主人紧接着纠正的不记"
    "（R05；和主人的纠正给成同一处说它结束了的说法的，Bot 那句不算证据）；同一对人纠正成另一种亲属的旧的结束；抽取没抽出来、对得上唯一一条现行关系的问主人",
}
# _relation_claims 先算好的一处关系：(L4 的结果, 判断 C 拿不准的原因, 主体, 客体, 算这一处的证据, 软变化的候选句,
# 主人在纠正的那一句, R05 从证据里去掉的 Bot 那几句)
Prepared = tuple[RelationCheck, str | None, str, str, list[dict[str, object]], str | None, str | None, list[dict[str, object]]]
L5_RELATION_ENDS = "end_not_entity"  # 关系的一头 L5 不建实体：写不进账本（关系表两头都要是实体），进隔离表
L5_RELATION_SAME = "same_ends"  # 两头对齐成了同一个实体
LEXICON_RULE = "L5-lexicon"  # 写账时用的别名表的指纹（版本那一列是指纹的前 16 位），巡检拿它和配置里的比


def register_lexicon(conn: sqlite3.Connection, lexicon: Lexicon, now: str) -> None:
    """记下写账时用的别名表（指纹的前 16 位，登记在规则表里）。同一份又用了一次，时间改成这一次的：巡检看最后一次。
    不自己开事务。"""
    conn.execute(
        "INSERT INTO rules (rule_id, version, layer, from_questions, description, created_at) VALUES (?, ?, 'L5', '[]', ?, ?)"
        " ON CONFLICT (rule_id, version) DO UPDATE SET created_at = excluded.created_at",
        (LEXICON_RULE, lexicon.fingerprint[:16], "写账时用的别名表（人写的那份加合并记录）的指纹", now),
    )


def ledger_lexicon(conn: sqlite3.Connection) -> str | None:
    """这本账最后一次写账时用的别名表的指纹（前 16 位）。没写过账的是 None。"""
    row = conn.execute(
        "SELECT version FROM rules WHERE rule_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (LEXICON_RULE,)
    ).fetchone()
    return str(row[0]) if row is not None else None


def writer_options(settings: Settings) -> dict[str, object]:
    """LedgerWriter 要的仓库外配置：主人的名字（D62）、别名表（J2.14、J3.6）。写错了报配置错误，消息里不带名字。
    主人的名字不能写在别名表第二版的组里、Bot 名单里：主人的叫法只写在主人的名字的配置里（D62）。
    别名表写了 Bot 名单的，来源登记表里出包的 Bot（对话的来源）都要在名单里（D92）。"""
    owner_names = load_owner_names(settings.owner_file)
    lexicon = load_lexicon(settings.lexicon_file)
    if owner_names & lexicon.known_keys:
        raise ConfigError("主人的名字也写在别名表的组里或者 Bot 名单里：主人的叫法只写在主人的名字的配置里（D62）")
    if lexicon.bots and settings.sources_file is not None:
        from javmem.sources import load_registry  # 只有写了 Bot 名单才要来源登记表

        listed = {name_key(name) for bot in lexicon.bots for name in bot.names}
        missing = sorted(
            source.id for source in load_registry(settings.sources_file).sources.values()
            if source.channel == "chat" and name_key(source.id) not in listed
        )
        if missing:
            raise ConfigError(f"来源登记表里出包的 Bot 不在别名表的 Bot 名单里：{'、'.join(missing)}（来源编号）")
    return {"owner_names": owner_names, "lexicon": lexicon}


class WriteBusy(Exception):
    """这个数据目录已经有一个写账在跑。"""


class WriteLock:
    """一个数据目录同一时间只能有一个 javmem write。锁跟着进程走，进程死了系统会放开。"""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._handle = open(directory / LOCK_FILE, "a+b")  # noqa: SIM115
        try:
            if os.name == "nt":
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            raise WriteBusy("这个数据目录已经有一个写账在跑") from None

    def close(self) -> None:
        if os.name == "nt":
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        self._handle.close()

    def __enter__(self) -> WriteLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class WriteStats:
    windows: int = 0
    windows_skipped: int = 0  # 同一个结果库的键、同一版规则已经写过的，不重写
    windows_stale: int = 0  # 取窗口以后包换了版本、撤回了，不写
    claims: int = 0
    quarantined_facts: int = 0
    quarantined_evidence: int = 0
    quarantined_entities: int = 0
    quarantined_relations: int = 0  # 没过 L4 的关系（J4.3），加上 L5 写不进去的（一头不建实体、两头是同一个，J4.4）
    quarantined_relation_evidence: int = 0  # 过了的关系上去掉的证据
    relation_claims: int = 0  # 写进账本的关系的说法（J4.4）
    entities_created: int = 0
    entities_dropped: int = 0  # 没有事实、别名、待确认的实体
    aliases_created: int = 0
    ambiguous_entities: int = 0
    frozen_conflicts: int = 0
    soft_held: int = 0  # 软变化的候选先挡住、进了待确认的（J4.4 第 4 段）
    soft_applied: int = 0  # 照软变化的结论执行了的
    corrections: int = 0  # 主人在纠正的关系的说法（J4.6）
    corrections_held: int = 0  # 财务投资的纠正先挡住、进了待确认的
    corrected_on_spot: int = 0  # Bot 当场说错、主人紧接着纠正的，Bot 那句不记（进隔离表，R05）
    corrections_unmatched: int = 0  # 抽取没抽出来、对上了唯一一条现行关系的纠正，进了待确认的
    corrections_other: int = 0  # 可以有几条的关系只说了新的那一条，问旧的那一条还算不算、进了待确认的
    chat_answers: int = 0  # 主人在对话里答了 Bot 问的待确认、认出来照答的定了的（D83，J4.6 第 2 段）
    chat_answers_unclear: int = 0  # 答了、编号也对得上，可答的话认不出来的（待确认照旧）
    packets_done: int = 0
    vectors_filled: int = 0
    stale_claims: int = 0  # 写完的包里以前的窗口这一批已经没有了，只来自它的说法删掉
    by_rule: dict[str, int] = field(default_factory=dict)
    relation_by_rule: dict[str, int] = field(default_factory=dict)  # 关系每条规则拦下多少（一条没过几条就各算一次）


def _now() -> str:
    return iso(datetime.now(timezone.utc).astimezone())


def _claim_order(claim: RelationClaimView) -> tuple[int, str, str]:
    """关系的说法按说的先后排（和 merge 走一遍的先后一样）。"""
    return (claim.said_from_epoch, claim.anchor, claim.claim_id)


def _moment_of(at: str, like: str) -> str:
    """判的那天写成带时区的时刻（替代时间和别处一样是说话的时刻）：只到天、月、季、年的取那一段第一天零点，时区照 like
    （那一处说法说话的时刻）；认不出的取 like（第三轮审阅小的第 2 条）。"""
    if parse_aware(at) is not None:
        return at
    bounds = date_bounds(at)
    said = parse_aware(like)
    if bounds is None or said is None:
        return like
    return datetime(bounds[0].year, bounds[0].month, bounds[0].day, tzinfo=said.tzinfo).isoformat()


def _soft_view(view: RelationClaimView, action: Mapping[str, object]) -> RelationClaimView:
    """照软变化的结论（javmem.soft.rules.Action.as_dict()）改一处关系说法的样子：暂停的从判的那天停、写上等什么；
    结束的到判的那天；换新的从判的那天开始（L3 写了开始日子的照它）。"""
    at = action.get("at") if isinstance(action.get("at"), str) else None
    precision = str(action.get("precision") or "unknown") if at else "unknown"
    if action.get("kind") == soft_rules.PAUSE:
        condition = action.get("condition")
        return replace(view, state="paused", paused_at=at, paused_at_precision=precision,
                       pause_condition=str(condition) if condition else view.pause_condition)
    if action.get("kind") == soft_rules.END:
        return replace(view, state="ended", valid_to=at or view.valid_to,
                       valid_to_precision=precision if at else view.valid_to_precision)
    if view.valid_from is None and at:
        return replace(view, valid_from=at, valid_from_precision=precision)
    return view


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _position(row: sqlite3.Row) -> dict[str, object]:
    """query-api 4.5 的 position。"""
    if row["field"] is not None:
        return {"section": row["section_no"], "field": row["field"]}
    section: object = "attachment" if row["section_kind"] == "attachment" else row["section_no"]
    return {"section": section, "line": row["line_no"], "sentence": row["sentence_no"]}


def _proof_order(proof: Mapping[str, object]) -> tuple[int, str]:
    return (int(proof["said_epoch"]), str(proof["sentence_id"]))


def _scope_of_row(partitions: str, sensitive: str) -> Scope:
    return Scope(tuple(sorted(set(json.loads(partitions)))), tuple(sorted(set(json.loads(sensitive)))))


# 对话里的引用行（packet-spec 第 5 节）：“> [引用 时间 说话人] 原文”，D83 认引用了谁的问话
_QUOTE_HEAD = re.compile(r"^>\s*\[引用\s+(?:\d{4}-\d{1,2}-\d{1,2}\s+)?\d{1,2}:\d{2}\s+([^\]]+)\]")
# 软变化在对话里答的（D83）：像日子的说法、“等……”、“改成……”（LedgerWriter._soft_details）
_SOFT_DAY = re.compile(r"\d{4}-\d{1,2}(?:-\d{1,2})?|\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?|月底|月初|月中|年底|年初|下周|下个?月"
                       r"|明天|后天|周[一二三四五六日天]|星期[一二三四五六日天]|\d{1,2}\s*[日号]")
_SOFT_WAIT = re.compile(r"等([^，。,.；;！!？?]{1,20})")
_SOFT_TO = re.compile(r"(?:改成|换成|交给|改由)([^，。,.；;！!？?]{1,20})")
# 像是说了哪天的字眼：有这些、又一个日子都没认出来的（中文数字、节日），不收（复审第 N3 条）
_DAYISH = re.compile(r"[月号日]|元旦|春节|节后|年后|年前|年底|年初|下周|上周|周[一二三四五六日天]|星期|礼拜")


def _iso_day(text: str, said: date) -> str | None:
    """软变化在对话里答的那天写成 ISO：2026-12-01、2026-12 照写；“12 月 1 日”“12 月”没写年份的，照说那句话的年份，
    比说话那天早两个月以上的算下一年（12 月说的“1 月 5 日”），推到下一年也离得太远（三百天以上）的认不出；别的（月底、
    下周）认不出。认不出返回 None。"""
    text = re.sub(r"\s+", "", text)
    match = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", text) or re.fullmatch(r"()(\d{1,2})月(?:(\d{1,2})[日号])?", text)
    if match is None:
        return None
    month, day = int(match.group(2)), int(match.group(3)) if match.group(3) else None
    yyyy = int(match.group(1)) if match.group(1) else said.year
    try:
        when = date(yyyy, month, day or 1)
        if not match.group(1) and (said - when).days > 60:
            yyyy += 1
            when = date(yyyy, month, day or 1)
            if (when - said).days > 300:
                return None
    except ValueError:
        return None
    return f"{yyyy:04d}-{month:02d}-{day:02d}" if day else f"{yyyy:04d}-{month:02d}"


def _message_of(sentence_id: str) -> tuple[str, str, str] | None:
    """句子编号（包号#小节.行.句）是哪一条消息的：(包号, 小节, 行)。认不出的 None。"""
    packet, _, position = sentence_id.partition("#")
    parts = position.split(".")
    return (packet, parts[0], parts[1]) if len(parts) >= 3 else None


class LedgerWriter:
    """写一个库的账本。conn 是 ledger.open_ledger 打开的。register：登记规则（自己开一个事务）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        extracted_by: str,
        embedding_model: str,
        register: bool = True,
        owner_names: Collection[str] = (),
        checked_by: str = L4_VERSION,
        lexicon: Lexicon = EMPTY_LEXICON,
        strict_partitions: Collection[str] | None = None,
        precheck_on: bool | None = None,
    ) -> None:
        """owner_names 是主人的名字的比对写法（merge.load_owner_names，D62）。checked_by 是核的方式：只用规则是
        L4_VERSION，加了模型复核是 recheck.checked_by(模型)；说法的 l4_version、隔离表的 rule_version 写它，
        写过的窗口也按它判要不要重写。lexicon 是仓库外的别名表（javmem.zh.lexicon，J2.14），只用来按名字找实体。
        strict_partitions 是哪些分区算财务投资（J4.6：这里的关系被纠正先挡住问主人）；不给就照 JAVMEM_STRICT_PARTITIONS，
        它也空着是 finance、investment（correct.strict_partitions：每个写账本的地方读同一个配置，撤回、处理待确认时重算也一样）。
        precheck_on：写的窗口过了进图前检查（javmem.pipeline.precheck，J5.3）：说法记下谁认的（judged_by），转人工的事实写成
        说法（judge_hold），写判事实、转人工的留痕，转人工的进待确认。不给就照 JAVMEM_PRECHECK（撤回、处理待确认时重算也一样）。
        没开的和以前一字不差。"""
        self.conn = conn
        self.checked_by = checked_by
        self.precheck_on = precheck.enabled() if precheck_on is None else precheck_on
        self.owner_names = frozenset(owner_names)
        self.lexicon = lexicon
        self.strict_partitions = frozenset(strict_partitions) if strict_partitions is not None else correct.strict_partitions()
        self.batch_windows: frozenset[str] | None = None  # 这一批的窗口编号，run_write 设；包写完时清它少掉的窗口
        self.touched_entities: set[str] = set()  # 撤回时说法、别名被删过的实体（javmem.withdraw 要看它们的标准名）
        # 撤回时删掉了出处的别名：(实体, 别名的比对写法)。重新对齐只看这次删掉的出处里有没有标准名（D73 第 18 条）
        self.lost_names: set[tuple[str, str]] = set()
        self.extracted_by = extracted_by
        self.embedding_model = embedding_model
        self.stats = WriteStats()
        self.moved_relations: set[tuple[str, int]] = set()  # 这个窗口里判断 C 拿不准、写成待确认的关系（窗口编号、第几条）
        self.moved_facts: set[tuple[str, int]] = set()  # 这个窗口里进图前检查转人工、写成待确认的事实（窗口编号、第几条）
        # 主人认了的“抽取没抽出来的纠正”，按同一条关系的键（_kept_unmatched_reviews；待确认表改了就作废）
        self._kept_unmatched: dict[str, list[tuple[int, dict[str, object]]]] | None = None
        # 主人在对话里答了、认出来定了的待确认，回答那几句（D83：这几句自己抽出来的说法不再挡一遍；待确认表改了就作废）
        self._answered: dict[str, frozenset[str]] | None = None
        # 主人在对话里答待确认的那几句，认没认出来都算（只说了新的那一条时这几句不问旧的；待确认表改了就作废）
        self._answer_ids: frozenset[str] | None = None
        # 有没有没处理的、进图前检查转人工的事实（J5.3）：关掉以后重写窗口，开着时留下的要清（审阅第 7 条；待确认表改了就作废）
        self._fact_holds_open: bool | None = None
        if register:
            self._register_rules()

    # ---------- 规则表 ----------

    def _register_rules(self) -> None:
        now = _now()
        rows = [(rule, L4_VERSION, "L4", text) for rule, text in RULES.items()]
        rows += [(rule, L5_VERSION, "L5", text) for rule, text in L5_RULES.items()]
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT OR IGNORE INTO rules (rule_id, version, layer, from_questions, description, created_at)"
                " VALUES (?, ?, ?, '[]', ?, ?)",
                [(*row, now) for row in rows],
            )
            # 关系的 R1 到 R6（J4.3，javmem.pipeline.relcheck），带上谁定的（04 p8：规则记下谁定的）
            self.conn.executemany(
                "INSERT OR IGNORE INTO rules (rule_id, version, layer, from_questions, description, created_at, decided_by)"
                " VALUES (?, ?, 'L4', '[]', ?, ?, ?)",
                [(rule, L4_VERSION, text, now, RELATION_DECIDED_BY.get(rule)) for rule, text in RELATION_RULES.items()],
            )
            if self.precheck_on:  # 进图前检查（J5.3）：事实也照 C 判，另登记一行（版本是提示词 f1），关着的一行不多写
                self.conn.execute(
                    "INSERT OR IGNORE INTO rules (rule_id, version, layer, from_questions, description, created_at, decided_by)"
                    " VALUES ('C', ?, 'L4', '[]', ?, ?, ?)",
                    (precheck.FACT_PROMPT_VERSION, precheck.RULE_TEXT, now, precheck.RULE_DECIDED_BY),
                )
            register_lexicon(self.conn, self.lexicon, now)

    # ---------- 写之前：这个窗口可能用到哪些文字的向量 ----------

    def texts_needed(self, item: Checked) -> list[str]:
        """这个窗口写完以后可能成为事实文字的那些：这个窗口过了 L4 的说法，以及会受影响的事实现有的说法。"""
        assert item.check is not None
        texts = [str(fact.raw.get("text") or "").strip() for fact in item.check.facts
                 if fact.passed or precheck.fact_hold(fact) is not None]  # 进图前检查转人工的也写成说法（J5.3）
        rows = self.conn.execute(
            "SELECT content FROM fact_claims WHERE fact_id IN (SELECT fact_id FROM fact_claims WHERE window_id = ?)",
            (item.window.window_id,),
        ).fetchall()
        texts += [row["content"] for row in rows]
        return [text for text in dict.fromkeys(texts) if text]

    # ---------- 一个窗口 ----------

    def write_window(
        self, item: Checked, vectors: Mapping[str, bytes], *, done: Iterable[tuple[str, int]] = ()
    ) -> bool:
        """一个事务：清掉这个窗口以前写的，写实体、别名、说法、隔离，重算受影响的事实和组。done 里的包改成 done。

        窗口正文的包在取窗口以后换了版本或者撤回了，什么都不写，返回 False：收件已经清掉了旧版本写的东西，
        不能再写回去。
        """
        check = item.check
        assert check is not None
        window = item.window
        now = _now()
        with transaction(self.conn):
            if not all(self._current(packet_id, revision) for packet_id, revision in _target_packets(item)):
                self.stats.windows_stale += 1
                return False
            affected, touched, identities = self._clear_window(item)
            keys, ambiguous = self._entities(item, now)
            self.moved_facts = set()
            for fact in check.facts:
                # 进图前检查转人工的（J5.3，只有开了才有）也写成说法，事实记成待确认；主人定过不要的照旧进隔离表
                hold = precheck.fact_hold(fact)
                if not fact.passed and hold is None:
                    continue
                fact_id = self._claim(item, fact, keys, now, judge_hold=hold)
                if fact_id is None:
                    continue
                affected.add(fact_id)
                if hold is not None:
                    self.moved_facts.add((window.window_id, fact.index))
            identities |= self._relation_claims(item, keys, now)
            self._refresh(affected, vectors, ambiguous, now)
            self._refresh_relations(identities, ambiguous, now)
            # D83：主人在对话里答了 Bot 问的哪一条待确认，认得出就照答的定。排在“抽取没抽出来的纠正”前面：回答那一句又是否定
            # 的（“对，改吧，他根本没负责过”），定完那一条已经结束了，不再对上它多问一条（第 2 段审阅第 3 条）
            self._chat_answers(item, now)
            self._unmatched_corrections(item, now)  # J4.6：抽取没抽出来的纠正，对得上唯一一条的进待确认
            self._other_corrections(item, now)  # 可以有几条的关系只说了新的那一条：问旧的那一条还算不算（复核 K12）
            self._reapply_kept(item, now)  # 主人认过“不算了”的、那一句在这个窗口里的：照它结束（排在两种问话改指以后）
            self.fill_codes()  # 这个窗口新进的待确认记下编号
            self._quarantine(item, now)
            write_audit(self.conn, item, now)  # 判断 C 每一次判的留痕（reljudge）；没判过的窗口一行都不写
            if self.precheck_on:  # 进图前检查判事实的每一档、转人工的（J5.3）
                precheck.write_audit(self.conn, item, now)
            self._drop_orphan_entities(touched | {entity_id for entity_id in keys.values() if entity_id is not None})
            self._mark_done(done)
        self.stats.windows += 1
        return True

    def already_written(self, item: Checked) -> bool:
        """这个窗口已经用同一个结果库的键、同一种核法（L4 的版本，加没加模型复核、复核用的哪个模型）、同一个抽取
        写过了：重写也只会让按别名对齐换个结果（看的是重写那一刻的别名），所以不重写。一条说法都没写下的窗口没法认，
        照样重写。"""
        targets = {line.packet_id for line in item.window.targets}
        mine = [row for row in self._window_claims(item.window.window_id) if _proof_packets(row) <= targets]
        mine += [row for row in self._window_relation_claims(item.window.window_id) if _proof_packets(row) <= targets]
        return bool(mine) and all(
            row["cache_key"] == item.cache_key and row["l4_version"] == self.checked_by and row["extracted_by"] == self.extracted_by
            for row in mine
        )

    def mark_done(self, done: Iterable[tuple[str, int]]) -> None:
        with transaction(self.conn):
            self._mark_done(done)

    def _mark_done(self, done: Iterable[tuple[str, int]]) -> None:
        """包改成 done。batch_windows 给了（run_write 给），同一个事务里把这个包以前的、这一批里已经没有的窗口清掉：
        分两个事务的话，中间断了包已经 done、不会再写，旧窗口的说法就一直留着。"""
        for packet_id, revision in done:
            cursor = self.conn.execute(
                "UPDATE packets SET extraction = 'done', extraction_note = NULL"
                " WHERE packet_id = ? AND revision = ? AND state = 'current'",
                (packet_id, revision),
            )
            if cursor.rowcount and self.batch_windows is not None:
                self.stats.stale_claims += self.drop_stale_windows(packet_id, self.batch_windows)
            self.stats.packets_done += cursor.rowcount

    def _current(self, packet_id: str, revision: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM packets WHERE packet_id = ? AND revision = ? AND state = 'current' AND withdrawn_at IS NULL",
                (packet_id, revision),
            ).fetchone()
            is not None
        )

    def _window_claims(self, window_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT claim_id, fact_id, cache_key, l4_version, extracted_by, subject_entity_id, entities, proofs"
            " FROM fact_claims WHERE window_id = ?",
            (window_id,),
        ).fetchall()

    def _window_relation_claims(self, window_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT claim_id, relation_id, identity_key, cache_key, l4_version, extracted_by, subject_entity_id,"
            " object_entity_id, org_entity_id, proofs, correction, state FROM relation_claims WHERE window_id = ?",
            (window_id,),
        ).fetchall()

    def _relation_claims_of(self, relation_ids: Iterable[str]) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        for chunk in _chunks(sorted(set(relation_ids))):
            rows += self.conn.execute(
                f"SELECT * FROM relation_claims WHERE relation_id IN ({_marks(chunk)}) ORDER BY claim_id", chunk
            ).fetchall()
        return rows

    def _relation_claims_by_packet(self, packet_id: str, revision: int | None = None) -> list[sqlite3.Row]:
        """证据里有这个包（这一版）的关系的说法：先按关系的证据找关系（走 relation_evidence_by_packet），再取它们的说法。
        关系的证据就是它几处说法的证据合起来的，所以找得全。"""
        if revision is None:
            found = self.conn.execute("SELECT DISTINCT relation_id FROM relation_evidence WHERE packet_id = ?", (packet_id,))
        else:
            found = self.conn.execute(
                "SELECT DISTINCT relation_id FROM relation_evidence WHERE packet_id = ? AND revision = ?", (packet_id, revision)
            )
        return self._relation_claims_of(row[0] for row in found)

    def _refresh(self, affected: Iterable[str], vectors: Mapping[str, bytes], ambiguous: set[str], now: str) -> None:
        groups: set[str] = set()
        for fact_id in sorted(affected):
            group = self._refresh_fact(fact_id, vectors, ambiguous, now)
            if group is not None:
                groups.add(group)
        for group in sorted(groups):
            self._supersede(group, now)

    def _clear_window(self, item: Checked) -> tuple[set[str], set[str], set[str]]:
        """删掉这个窗口以前写的、证据全在这个窗口正文的包里的说法（事实的、关系的），以及这几个包的隔离、别名的出处。

        同一个编号的窗口以前可能多带着别的包（同一往来串的邮件一起写过）：只来自那些包的说法留着，那些包已经写完，
        不会再写。返回 (受影响的事实, 碰过的实体, 受影响的关系：同一条关系的键)。
        """
        affected, touched, identities, _, _ = self._clear_window_id(
            item.window.window_id, {line.packet_id for line in item.window.targets}
        )
        return affected, touched, identities

    def _clear_window_id(
        self, window_id: str, targets: Collection[str], *, claims: bool = True
    ) -> tuple[set[str], set[str], set[str], int, int]:
        """_clear_window 照窗口编号和正文的包（targets）做。claims=False 只删隔离行（J5.6：单条撤回带出来的窗口，说法
        留着等重抽）。返回 (受影响的事实, 碰过的实体, 受影响的关系, 删了几处说法, 删了几行隔离)。"""
        targets = set(targets)
        affected: set[str] = set()
        touched: set[str] = set()
        identities: set[str] = set()
        removed = 0
        if claims:
            for row in self._window_claims(window_id):
                if _proof_packets(row) <= targets:
                    self.conn.execute("DELETE FROM fact_claims WHERE claim_id = ?", (row["claim_id"],))
                    affected.add(row["fact_id"])
                    touched |= _claim_entities(row)
                    removed += 1
            for row in self._window_relation_claims(window_id):
                if _proof_packets(row) <= targets:
                    self.conn.execute("DELETE FROM relation_claims WHERE claim_id = ?", (row["claim_id"],))
                    identities.add(row["identity_key"])
                    touched |= _relation_claim_entities(row)
                    removed += 1
            rows = self.conn.execute(
                "SELECT v.evidence_id, v.alias_id, v.packet_id, a.entity_id FROM evidence AS v JOIN aliases AS a USING (alias_id)"
                " WHERE v.window_id = ? AND v.alias_id IS NOT NULL",
                (window_id,),
            ).fetchall()
            aliases = []
            for row in rows:
                if row["packet_id"] in targets:
                    self.conn.execute("DELETE FROM evidence WHERE evidence_id = ?", (row["evidence_id"],))
                    aliases.append(row["alias_id"])
                    touched.add(row["entity_id"])
            self._drop_bare_aliases(aliases)
        quarantined = 0
        for row in self.conn.execute("SELECT quarantine_id, packet_id FROM quarantine WHERE window_id = ?", (window_id,)).fetchall():
            if row["packet_id"] in targets:
                self.conn.execute("DELETE FROM quarantine WHERE quarantine_id = ?", (row["quarantine_id"],))
                quarantined += 1
        return affected, touched, identities, removed, quarantined

    def forget_windows(self, windows: Mapping[str, Collection[str]], *, claims: Collection[str] = ()) -> tuple[int, int]:
        """撤回时结果库要删的窗口（J5.6，javmem.withdraw）：窗口编号 → 正文的包。这些窗口、正文那几个包下面的隔离行删掉
        （隔离里是 L3 原样、复核和判断 C 的理由，可能照着撤回的那几句写）。claims 里的窗口（整包撤回带出来的：拿撤回的包
        当上文的、往来串一起写的、标注用了从它学来的名字的）连说法、关系的说法、别名的出处一起删，和重写这个窗口时清的
        一样，事实、关系照剩下的说法重算，没用了的实体删掉；下一次 extract、write 照剩下的原文重抽、重写回来。
        返回 (删了几处说法, 删了几行隔离)。不自己开事务。"""
        affected: set[str] = set()
        touched: set[str] = set()
        identities: set[str] = set()
        removed = quarantined = 0
        for window_id, targets in sorted(windows.items()):
            facts, entities, relations, claims_gone, rows_gone = self._clear_window_id(
                window_id, targets, claims=window_id in claims
            )
            affected |= facts
            touched |= entities
            identities |= relations
            removed += claims_gone
            quarantined += rows_gone
        now = _now()
        if affected:
            self._refresh(affected, {}, set(), now)
        if identities:
            self._refresh_relations(identities, set(), now)
        if touched:
            self._drop_orphan_entities(touched)
            self.touched_entities |= touched
        if affected or identities:
            self.fill_codes()  # 重算以后新进的待确认当时就记下编号（D83）
        return removed, quarantined

    def _drop_bare_aliases(self, aliases: Iterable[str]) -> None:
        for alias_id in aliases:  # 没有出处的别名谁都看不到，也不该再拿来对齐
            self.conn.execute(
                "DELETE FROM aliases WHERE alias_id = ? AND NOT EXISTS (SELECT 1 FROM evidence WHERE alias_id = ?)",
                (alias_id, alias_id),
            )

    def _drop_orphan_entities(self, candidates: Iterable[str]) -> None:
        """没有事实、没有关系、没有别名、没有没处理的待确认的实体删掉：留着的话，后来的按名字对齐会对上它，和整本重建
        不一样。关系、关系的说法指着的实体删不掉（外键），要算用着（J4.4）。"""
        for entity_id in sorted(set(candidates)):
            used = self.conn.execute(
                "SELECT 1 FROM facts WHERE subject_entity_id = ?1"
                " UNION ALL SELECT 1 FROM fact_entities WHERE entity_id = ?1"
                " UNION ALL SELECT 1 FROM aliases WHERE entity_id = ?1"
                " UNION ALL SELECT 1 FROM relations WHERE subject_entity_id = ?1"
                " UNION ALL SELECT 1 FROM relations WHERE object_entity_id = ?1"
                " UNION ALL SELECT 1 FROM relations WHERE org_entity_id = ?1"
                " UNION ALL SELECT 1 FROM relation_claims WHERE subject_entity_id = ?1"
                " UNION ALL SELECT 1 FROM relation_claims WHERE object_entity_id = ?1"
                " UNION ALL SELECT 1 FROM relation_claims WHERE org_entity_id = ?1"
                " UNION ALL SELECT 1 FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL"
                " AND json_extract(claims, '$.entity_id') = ?1 LIMIT 1",
                (entity_id,),
            ).fetchone()
            if used is None:
                cursor = self.conn.execute("DELETE FROM entities WHERE entity_id = ?", (entity_id,))
                self.stats.entities_dropped += cursor.rowcount

    # ---------- 包重发 ----------

    def remove_revision(self, packet_id: str, revision: int) -> None:
        """删掉证据来自这个包这个版本的说法、它的别名出处和隔离，重算受影响的事实和组，没用了的实体删掉。不自己开事务：
        收件换版本的那个事务里调。

        删掉的说法里还有别的包的证据的（同一往来串的邮件一起写过），那些包改回 queued，和新版本一起重新抽、重新写：
        只删一半证据，剩下的那一半不一定还撑得住这条事实。向量这时候不算：事实文字变了的向量先删掉，下次 javmem write 补。
        """
        now = _now()
        candidates = [
            row["fact_id"]
            for row in self.conn.execute(
                "SELECT DISTINCT fact_id FROM evidence WHERE packet_id = ? AND revision = ? AND fact_id IS NOT NULL",
                (packet_id, revision),
            )
        ]
        affected: set[str] = set()
        touched: set[str] = set()
        others: set[str] = set()
        for fact_id in candidates:
            for row in self._claims_of(fact_id):
                proofs = json.loads(row["proofs"])
                if any(proof["packet_id"] == packet_id and proof["revision"] == revision for proof in proofs):
                    self.conn.execute("DELETE FROM fact_claims WHERE claim_id = ?", (row["claim_id"],))
                    affected.add(fact_id)
                    touched |= _claim_entities(row)
                    others |= {proof["packet_id"] for proof in proofs} - {packet_id}
        identities: set[str] = set()  # 关系的说法一样（J4.4）
        for row in self._relation_claims_by_packet(packet_id, revision):
            proofs = json.loads(row["proofs"])
            if any(proof["packet_id"] == packet_id and proof["revision"] == revision for proof in proofs):
                self.conn.execute("DELETE FROM relation_claims WHERE claim_id = ?", (row["claim_id"],))
                identities.add(row["identity_key"])
                touched |= _relation_claim_entities(row)
                others |= {proof["packet_id"] for proof in proofs} - {packet_id}
        rows = self.conn.execute(
            "SELECT DISTINCT v.alias_id, a.entity_id, a.alias_key FROM evidence AS v JOIN aliases AS a USING (alias_id)"
            " WHERE v.packet_id = ? AND v.revision = ? AND v.alias_id IS NOT NULL",
            (packet_id, revision),
        ).fetchall()
        self.conn.execute(
            "DELETE FROM evidence WHERE packet_id = ? AND revision = ? AND alias_id IS NOT NULL", (packet_id, revision)
        )
        self._drop_bare_aliases(row["alias_id"] for row in rows)
        touched |= {row["entity_id"] for row in rows}
        self.lost_names |= {(row["entity_id"], row["alias_key"]) for row in rows}
        self.conn.execute("DELETE FROM quarantine WHERE packet_id = ? AND revision = ?", (packet_id, revision))
        # 抽取没抽出来的纠正（J4.6）那一句在这个包里的：认过的那一条重算——那一句不在了（撤回、换了）就不再照它结束；新版本
        # 还是那一句的，写到那个窗口时再照它结束。没处理的：整包撤回的删掉；包重发的留着，写新版本时照常认（还对得上的
        # 留着、建的时间不变，对不上的删掉）（第一轮审阅第 2 条）
        withdrawn = self.conn.execute("SELECT 1 FROM withdrawn_packets WHERE packet_id = ?", (packet_id,)).fetchone() is not None
        for review in self.conn.execute(  # 只说了新的那一条时问的旧的那一条（correction_other）一样
            "SELECT review_id, claims, decided_at FROM review_queue WHERE kind = ? AND json_extract(claims, '$.reason') IN (?, ?)"
            " AND substr(json_extract(claims, '$.sentence'), 1, instr(json_extract(claims, '$.sentence'), '#') - 1) = ?",
            (MANUAL_REVIEW, *correct.ASKED, packet_id),
        ).fetchall():
            claims = json.loads(review["claims"])
            identities.add(str(claims.get("identity") or claims.get("key")))
            if review["decided_at"] is None and withdrawn:
                self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (review["review_id"],))
        self._refresh(affected, {}, set(), now)
        self._refresh_relations(identities, set(), now)
        self._drop_orphan_entities(touched)
        self.touched_entities |= touched
        self.fill_codes()
        for other in sorted(others):  # 写完了的又进排队：从现在起算（queued_at，迁移 0029）
            self.conn.execute(
                "UPDATE packets SET extraction = 'queued', queued_at = ? WHERE packet_id = ? AND state = 'current'"
                " AND extraction = 'done'",
                (now, other),
            )

    # ---------- 撤回（javmem.withdraw） ----------

    def remove_sentences(self, sentences: Iterable[tuple[str, str, int]]) -> set[str]:
        """单条撤回（query-api 3.2）：这些句子从说法的证据（proofs）里去掉，去光了的说法删掉；别名的出处、
        引着这些句子的隔离也去掉；受影响的事实和组按剩下的说法重算，没用了的实体删掉。不自己开事务。

        sentences 是 (句子编号, 包号, 版本)。返回证据被动过的说法里出现的包：这些窗口少了几句原文，
        要改回 queued，用剩下的原文重抽、重写（其他来源也支持的，用剩余原文重建）。
        """
        wanted = set(sentences)
        if not wanted:
            return set()
        now = _now()
        ids = sorted({sentence_id for sentence_id, _, _ in wanted})
        candidates: set[str] = set()
        for chunk in _chunks(ids):
            candidates |= {
                row[0]
                for row in self.conn.execute(
                    f"SELECT DISTINCT fact_id FROM evidence WHERE fact_id IS NOT NULL AND sentence_id IN ({_marks(chunk)})",
                    chunk,
                )
            }
        affected: set[str] = set()
        touched: set[str] = set()
        packets: set[str] = set()
        for fact_id in sorted(candidates):
            for row in self._claims_of(fact_id):
                proofs = json.loads(row["proofs"])
                kept = [proof for proof in proofs if (proof["sentence_id"], proof["packet_id"], proof["revision"]) not in wanted]
                if len(kept) == len(proofs):
                    continue
                affected.add(fact_id)
                touched |= _claim_entities(row)
                packets |= {str(proof["packet_id"]) for proof in proofs}
                if not kept:
                    self.conn.execute("DELETE FROM fact_claims WHERE claim_id = ?", (row["claim_id"],))
                    continue
                kept.sort(key=_proof_order)
                first, last = kept[0], max(kept, key=lambda proof: proof["said_epoch"])
                self.conn.execute(
                    "UPDATE fact_claims SET proofs = ?, speaker = ?, speaker_role = ?, said_from = ?, said_from_epoch = ?,"
                    " said_to = ?, said_to_epoch = ? WHERE claim_id = ?",
                    (
                        _json(kept),
                        first["speaker"],
                        first["speaker_role"],
                        first["said_at"],
                        first["said_epoch"],
                        last["said_at"],
                        last["said_epoch"],
                        row["claim_id"],
                    ),
                )
        identities: set[str] = set()  # 关系的说法一样（J4.4）
        relation_ids: set[str] = set()
        for chunk in _chunks(ids):
            relation_ids |= {
                row[0]
                for row in self.conn.execute(
                    f"SELECT DISTINCT relation_id FROM relation_evidence WHERE sentence_id IN ({_marks(chunk)})", chunk
                )
            }
        for row in self._relation_claims_of(relation_ids):
            proofs = json.loads(row["proofs"])
            kept = [proof for proof in proofs if (proof["sentence_id"], proof["packet_id"], proof["revision"]) not in wanted]
            if len(kept) == len(proofs):
                continue
            identities.add(row["identity_key"])
            touched |= _relation_claim_entities(row)
            packets |= {str(proof["packet_id"]) for proof in proofs}
            if not kept:
                self.conn.execute("DELETE FROM relation_claims WHERE claim_id = ?", (row["claim_id"],))
                continue
            kept.sort(key=_proof_order)
            first, last = kept[0], max(kept, key=lambda proof: proof["said_epoch"])
            self.conn.execute(
                "UPDATE relation_claims SET proofs = ?, speaker = ?, speaker_role = ?, said_from = ?, said_from_epoch = ?,"
                " said_to = ?, said_to_epoch = ? WHERE claim_id = ?",
                (_json(kept), first["speaker"], first["speaker_role"], first["said_at"], first["said_epoch"],
                 last["said_at"], last["said_epoch"], row["claim_id"]),
            )
        # 纠正那一句撤回了（J4.6）：那一处不再算纠正（只撤回了“不对”那两个字的，剩下的照普通新说法）；抽取没抽出来的纠正、
        # 只说了新的那一条的那一句撤回了的，没处理的待确认删掉，定过的重算时不再照它结束（_live_sentence 认不出撤回了的）
        for chunk in _chunks(ids):
            for row in self.conn.execute(
                f"SELECT claim_id, identity_key FROM relation_claims WHERE correction IN ({_marks(chunk)})", chunk
            ).fetchall():
                self.conn.execute("UPDATE relation_claims SET correction = NULL WHERE claim_id = ?", (row["claim_id"],))
                identities.add(row["identity_key"])
            for review in self.conn.execute(
                f"SELECT review_id, claims, decided_at FROM review_queue WHERE kind = ? AND json_extract(claims, '$.reason') IN (?, ?)"
                f" AND json_extract(claims, '$.sentence') IN ({_marks(chunk)})",
                (MANUAL_REVIEW, *correct.ASKED, *chunk),
            ).fetchall():
                claims = json.loads(review["claims"])
                identities.add(str(claims.get("identity") or claims.get("key")))
                if review["decided_at"] is None:
                    self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (review["review_id"],))
        aliases: list[str] = []
        for chunk in _chunks(ids):
            rows = self.conn.execute(
                "SELECT v.evidence_id, v.alias_id, v.sentence_id, v.packet_id, v.revision, a.entity_id, a.alias_key"
                f" FROM evidence AS v JOIN aliases AS a USING (alias_id) WHERE v.alias_id IS NOT NULL AND v.sentence_id IN ({_marks(chunk)})",
                chunk,
            ).fetchall()
            for row in rows:
                if (row["sentence_id"], row["packet_id"], row["revision"]) in wanted:
                    self.conn.execute("DELETE FROM evidence WHERE evidence_id = ?", (row["evidence_id"],))
                    aliases.append(row["alias_id"])
                    touched.add(row["entity_id"])
                    self.lost_names.add((row["entity_id"], row["alias_key"]))
        self._drop_bare_aliases(aliases)
        for sentence_id in ids:  # 隔离的 payload 是 L3 的原样，引文就在里面
            self.conn.execute("DELETE FROM quarantine WHERE instr(payload, ?) > 0", (json.dumps(sentence_id, ensure_ascii=False),))
        self._refresh(affected, {}, set(), now)
        self._refresh_relations(identities, set(), now)
        self._drop_orphan_entities(touched)
        self.touched_entities |= touched
        self.fill_codes()
        return packets

    def mask_senders(self, mails: Collection[tuple[str, int, str]]) -> set[str]:
        """按事实撤回了邮件的发件人那一句、这封信别的句子还在（D73 第 16 条）：这些句子撑着的说法，证据里的说话人
        换成“[已撤回]”，说法、事实的说话人跟着重算，不等重抽；没有说法了、冻结标记撑着的事实，证据在这封信里、
        说话人是它的发件人的，也换掉。L3 写的“这条说法出自谁”（attributed_to）多半写的就是发件人，这些说法的、
        证据在这封信里的没有说法的事实的，也换成“[已撤回]”（J4.1）。mails 是 (包号, 版本, message_id)。

        句子属于哪封信按原文索引认（证据里只有句子编号）。返回证据被动过的说法里出现的包：这封信的抬头变了，
        要改回排队重抽。不自己开事务。
        """
        wanted = set(mails)
        if not wanted:
            return set()
        mail_of: dict[str, str | None] = {}

        def mail(proof: Mapping[str, object]) -> str | None:
            sentence_id = str(proof["sentence_id"])
            if sentence_id not in mail_of:
                row = self.conn.execute(
                    "SELECT message_id FROM raw_sentences WHERE sentence_id = ? AND revision = ?", (sentence_id, proof["revision"])
                ).fetchone()
                mail_of[sentence_id] = row[0] if row is not None else None
            return mail_of[sentence_id]

        candidates: set[str] = set()
        for packet_id, revision in sorted({(packet_id, revision) for packet_id, revision, _ in wanted}):
            candidates |= {
                row[0]
                for row in self.conn.execute(
                    "SELECT DISTINCT fact_id FROM evidence WHERE fact_id IS NOT NULL AND packet_id = ? AND revision = ?",
                    (packet_id, revision),
                )
            }
        affected: set[str] = set()
        packets: set[str] = set()
        for fact_id in sorted(candidates):
            for row in self._claims_of(fact_id):
                proofs = json.loads(row["proofs"])
                changed = False
                for proof in proofs:
                    if proof.get("speaker") != MASKED and (proof["packet_id"], proof["revision"], mail(proof)) in wanted:
                        proof["speaker"] = MASKED
                        changed = True
                if not changed:
                    continue
                proofs.sort(key=_proof_order)
                # L3 写的“这条说法出自谁”是看着这封信的抬头写的，多半就是这个发件人：一起换掉（J4.1），重抽以后再写上
                attributed_to = MASKED if row["attributed_to"] is not None else None
                self.conn.execute(
                    "UPDATE fact_claims SET proofs = ?, speaker = ?, attributed_to = ? WHERE claim_id = ?",
                    (_json(proofs), proofs[0]["speaker"], attributed_to, row["claim_id"]),
                )
                affected.add(fact_id)
                packets |= {str(proof["packet_id"]) for proof in proofs}
        self._refresh(affected, {}, set(), _now())
        identities: set[str] = set()  # 关系的说法一样（J4.4）
        for packet_id, revision in sorted({(packet_id, revision) for packet_id, revision, _ in wanted}):
            for row in self._relation_claims_by_packet(packet_id, revision):
                proofs = json.loads(row["proofs"])
                changed = False
                for proof in proofs:
                    if proof.get("speaker") != MASKED and (proof["packet_id"], proof["revision"], mail(proof)) in wanted:
                        proof["speaker"] = MASKED
                        changed = True
                if not changed:
                    continue
                proofs.sort(key=_proof_order)
                attributed_to = MASKED if row["attributed_to"] is not None else None
                self.conn.execute(
                    "UPDATE relation_claims SET proofs = ?, speaker = ?, attributed_to = ? WHERE claim_id = ?",
                    (_json(proofs), proofs[0]["speaker"], attributed_to, row["claim_id"]),
                )
                identities.add(row["identity_key"])
                packets |= {str(proof["packet_id"]) for proof in proofs}
        self._refresh_relations(identities, set(), _now())
        for packet_id, revision, message_id in sorted(wanted):  # 没有说法了、冻结标记撑着的关系，照下面事实的办法
            self.conn.execute(
                "UPDATE relations SET attributed_to = ? WHERE relation_id IN (SELECT v.relation_id FROM relation_evidence AS v"
                " WHERE v.packet_id = ? AND (v.message_id = ? OR v.message_id IS NULL))"
                " AND attributed_to IS NOT NULL AND attributed_to <> ?"
                " AND NOT EXISTS (SELECT 1 FROM relation_claims AS c WHERE c.relation_id = relations.relation_id)",
                (MASKED, packet_id, message_id, MASKED),
            )
            sender = self.conn.execute(
                "SELECT speaker FROM raw_sentences WHERE packet_id = ? AND revision = ? AND message_id = ? LIMIT 1",
                (packet_id, revision, message_id),
            ).fetchone()
            if sender is not None:
                self.conn.execute(
                    "UPDATE relations SET speaker = ? WHERE speaker = ?"
                    " AND NOT EXISTS (SELECT 1 FROM relation_claims AS c WHERE c.relation_id = relations.relation_id)"
                    " AND EXISTS (SELECT 1 FROM relation_evidence AS v WHERE v.relation_id = relations.relation_id"
                    " AND v.packet_id = ? AND (v.message_id = ? OR v.message_id IS NULL))",
                    (MASKED, sender[0], packet_id, message_id),
                )
        self._mask_bare(wanted)
        # 没有说法了、冻结标记撑着的事实（证据还指着替换前旧版本的这封信）：说话人是这封信的发件人的，也换掉
        for packet_id, revision, message_id in sorted(wanted):
            # 说法出自谁不一定写成发件人的原样（“某某的邮件”），证据在这封信里的都换：宁可多换。不会再重抽，不换就一直留着。
            # 从这个包的证据找（走 evidence_by_packet），不把事实表从头扫一遍
            self.conn.execute(
                "UPDATE facts SET attributed_to = ? WHERE fact_id IN (SELECT v.fact_id FROM evidence AS v"
                " WHERE v.packet_id = ? AND (v.message_id = ? OR v.message_id IS NULL) AND v.fact_id IS NOT NULL)"
                " AND attributed_to IS NOT NULL AND attributed_to <> ?"
                " AND NOT EXISTS (SELECT 1 FROM fact_claims AS c WHERE c.fact_id = facts.fact_id)",
                (MASKED, packet_id, message_id, MASKED),
            )
            sender = self.conn.execute(
                "SELECT speaker FROM raw_sentences WHERE packet_id = ? AND revision = ? AND message_id = ? LIMIT 1",
                (packet_id, revision, message_id),
            ).fetchone()
            if sender is None:
                continue
            # 证据不知道是哪封信的（迁移 0011 补不上的旧版本），同一个包、说话人就是这个发件人的也换：宁可多换
            self.conn.execute(
                "UPDATE facts SET speaker = ? WHERE speaker = ?"
                " AND NOT EXISTS (SELECT 1 FROM fact_claims AS c WHERE c.fact_id = facts.fact_id)"
                " AND EXISTS (SELECT 1 FROM evidence AS v WHERE v.fact_id = facts.fact_id AND v.packet_id = ?"
                " AND (v.message_id = ? OR v.message_id IS NULL))",
                (MASKED, sender[0], packet_id, message_id),
            )
        return packets

    def _mask_bare(self, wanted: Collection[tuple[str, int, str]]) -> None:
        """证据早就全撤回了、证据行都删了的冻结事实和冻结关系（J4.1 报告第 4 节第 5 条）：按事实撤回邮件发件人那一句时，
        按证据找不到它们。照“证据都撤回了”那条待确认（没处理的、处理过的都算：主人可能已经放回来了）记的包号找：包对得上的，
        说话人就是那封信的发件人的换成“[已撤回]”，说法出自谁有就换（宁可多换）。不然主人把它放回来以后，说话人还是
        发件人的原名。只动没有说法、也没有证据的：还有证据的上面按证据换过了。"""
        senders: dict[str, set[str]] = {}
        for packet_id, revision, message_id in sorted(wanted):
            row = self.conn.execute(
                "SELECT speaker FROM raw_sentences WHERE packet_id = ? AND revision = ? AND message_id = ? LIMIT 1",
                (packet_id, revision, message_id),
            ).fetchone()
            if row is not None:
                senders.setdefault(packet_id, set()).add(str(row[0]))
        if not senders:
            return
        bare = {
            "fact": ("facts", "fact_id", "fact_claims", "evidence"),
            "relation": ("relations", "relation_id", "relation_claims", "relation_evidence"),
        }
        for row in self.conn.execute("SELECT claims FROM review_queue WHERE kind = 'other'").fetchall():
            claims = json.loads(row["claims"])
            if not isinstance(claims, Mapping) or claims.get("reason") != EVIDENCE_WITHDRAWN:
                continue
            hit = {str(one) for one in claims.get("packets") or []} & set(senders)
            for name, (table, column, claim_table, evidence_table) in bare.items():
                item_id = claims.get(name)
                if not hit or not isinstance(item_id, str):
                    continue
                alone = (f" AND NOT EXISTS (SELECT 1 FROM {claim_table} AS c WHERE c.{column} = {table}.{column})"
                         f" AND NOT EXISTS (SELECT 1 FROM {evidence_table} AS v WHERE v.{column} = {table}.{column})")
                self.conn.execute(
                    f"UPDATE {table} SET attributed_to = ? WHERE {column} = ? AND attributed_to IS NOT NULL AND attributed_to <> ?"
                    + alone,
                    (MASKED, item_id, MASKED),
                )
                for sender in sorted(set().union(*(senders[packet] for packet in hit))):
                    self.conn.execute(f"UPDATE {table} SET speaker = ? WHERE {column} = ? AND speaker = ?" + alone,
                                      (MASKED, item_id, sender))

    def delete_fact(self, fact_id: str) -> bool:
        """单条撤回点名的那条事实：说法、证据、向量一起删，冻结的也删（主人点名撤回的）。指着它的替代清空，
        没处理的待确认删掉，它的组重算。不自己开事务。没有这条返回 False。点名的是关系（R 开头，J4.4）的照关系删。"""
        if self.conn.execute("SELECT 1 FROM relations WHERE relation_id = ?", (fact_id,)).fetchone() is not None:
            return self.delete_relation(fact_id)
        row = self.conn.execute("SELECT group_key FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
        if row is None:
            return False
        touched = {entity["entity_id"] for entity in self.conn.execute("SELECT entity_id FROM fact_entities WHERE fact_id = ?", (fact_id,))}
        subject = self.conn.execute("SELECT subject_entity_id FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()[0]
        if subject is not None:
            touched.add(subject)
        self._forget_fact(fact_id)
        if row["group_key"] is not None:
            self._supersede(row["group_key"], _now())
        self._drop_orphan_entities(touched)
        self.touched_entities |= touched
        return True

    def delete_relation(self, relation_id: str) -> bool:
        """单条撤回点名的那条关系（J4.4）：它这一段的说法、证据一起删，冻结的也删（主人点名撤回的）。同一条关系别的几段
        按剩下的说法重算。不自己开事务。没有这条返回 False。"""
        row = self.conn.execute(
            "SELECT identity_key, subject_entity_id, object_entity_id, org_entity_id FROM relations WHERE relation_id = ?",
            (relation_id,),
        ).fetchone()
        if row is None:
            return False
        touched = _relation_claim_entities(row)
        family = self._family_of(relation_id)
        self._forget_relation(relation_id)
        self._refresh_relations(family, set(), _now())  # 同组按剩下的重算（被它顶掉的又现行了，第二轮审阅 S2）
        self._drop_orphan_entities(touched)
        self.touched_entities |= touched
        return True

    def _family_of(self, relation_id: str) -> set[str]:
        """这条关系和谁一起走一遍（_family）：删它、藏它以前先记下，完了按剩下的重算（第二轮审阅 S2、G1）。"""
        row = self.conn.execute(
            "SELECT identity_key, relation_type, subject_entity_id, object_entity_id, scope_partitions, scope_sensitive"
            " FROM relations WHERE relation_id = ?",
            (relation_id,),
        ).fetchone()
        if row is None or not row["identity_key"]:
            return set()
        return self._family(row, str(row["identity_key"]))[1]

    def _forget_fact(self, fact_id: str) -> None:
        self.conn.execute("UPDATE facts SET superseded_by = NULL, superseded_at = NULL WHERE superseded_by = ?", (fact_id,))
        self.conn.execute(
            "DELETE FROM review_queue WHERE decided_at IS NULL AND ("
            "(kind = 'frozen_conflict' AND (json_extract(claims, '$.new') = ?1 OR json_extract(claims, '$.frozen') = ?1))"
            " OR (kind IN ('other', 'soft_change') AND json_extract(claims, '$.fact') = ?1))",
            (fact_id,),
        )
        self.conn.execute("DELETE FROM fact_claims WHERE fact_id = ?", (fact_id,))
        self.conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))

    def hide_withdrawn(self, now: str) -> list[str]:
        """撤回、彻底删除以后：没有说法的事实（冻结的，说法没了也留着）上，指着撤回了的包、撤回了的句子、
        彻底删除了的版本的证据删掉。证据一条都不剩的，先藏起来（withdrawn_at），进待确认等主人定：冻结是主人定的，
        不自动删；可它的文字来自撤回了的内容，不能再给人看。返回这次藏起来的事实。不自己开事务。

        撤回的句子按指纹认（说话人加原句，和单条撤回的记录一样），不按句子编号：冻结事实的证据可能还指着替换前的
        旧版本，句子编号只是位置，新版本同一个位置可能是另一句话。邮件的记录只认同一封信的证据（D73 第 17 条）；
        证据不知道是哪封信的（迁移 0011 以前写的、旧版本已经不在原文索引里）、记录不知道是哪一封的，照旧按整个包认。
        没有指纹的旧证据（J2.11 以前写的）按同一版本的句子编号认。

        先看对话里答待确认的（D83）：问话、回答那一句撤回了的，照它定的跟着撤（revert_withdrawn_answers）。
        """
        self.revert_withdrawn_answers(now)
        rows = self.conn.execute(
            "SELECT v.evidence_id, v.fact_id, v.packet_id FROM evidence AS v JOIN facts AS f ON f.fact_id = v.fact_id"
            " WHERE f.withdrawn_at IS NULL AND NOT EXISTS (SELECT 1 FROM fact_claims AS c WHERE c.fact_id = v.fact_id)"
            " AND (EXISTS (SELECT 1 FROM packets AS p WHERE p.packet_id = v.packet_id AND (p.withdrawn_at IS NOT NULL"
            " OR (p.revision = v.revision AND p.purged_at IS NOT NULL)))"
            " OR EXISTS (SELECT 1 FROM withdrawn_packets AS wp WHERE wp.packet_id = v.packet_id)"
            " OR (v.sentence_hash IS NOT NULL AND EXISTS (SELECT 1 FROM withdrawn_sentences AS w"
            " WHERE w.packet_id = v.packet_id AND w.sentence_hash = v.sentence_hash"
            " AND (w.message_id = '' OR v.message_id IS NULL OR w.message_id = v.message_id)))"
            " OR (v.sentence_hash IS NULL AND EXISTS (SELECT 1 FROM raw_sentences AS s WHERE s.sentence_id = v.sentence_id"
            " AND s.revision = v.revision AND s.withdrawn_at IS NOT NULL)))"
            " ORDER BY v.fact_id, v.evidence_id"
        ).fetchall()
        packets_of: dict[str, set[str]] = {}
        for row in rows:
            self.conn.execute("DELETE FROM evidence WHERE evidence_id = ?", (row["evidence_id"],))
            packets_of.setdefault(row["fact_id"], set()).add(row["packet_id"])
        hidden = []
        groups: set[str] = set()
        for fact_id, packets in sorted(packets_of.items()):
            if self.conn.execute("SELECT 1 FROM evidence WHERE fact_id = ?", (fact_id,)).fetchone() is not None:
                self._provenance(fact_id)  # 还有别的证据，照常看得到；来源按剩下的证据算
                continue
            group = self.conn.execute("SELECT group_key FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()[0]
            self.conn.execute("UPDATE facts SET withdrawn_at = ?, updated_at = ? WHERE fact_id = ?", (now, now, fact_id))
            self._provenance(fact_id)  # 证据都没了：来源、origin 跟着清空，不留撤回以前的样子
            self.conn.execute(  # 和它冲突的待确认先删掉；主人把它放回来时，组重算会再进队列
                "DELETE FROM review_queue WHERE decided_at IS NULL AND kind = 'frozen_conflict'"
                " AND (json_extract(claims, '$.new') = ?1 OR json_extract(claims, '$.frozen') = ?1)",
                (fact_id,),
            )
            claims = {"fact": fact_id, "reason": EVIDENCE_WITHDRAWN, "packets": sorted(packets)}
            self.conn.execute(
                "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('other', ?, '[]', ?)", (_json(claims), now)
            )
            hidden.append(fact_id)
            if group is not None:
                groups.add(group)
        for group in sorted(groups):
            self._supersede(group, now)
        hidden += self._hide_withdrawn_relations(now)
        self.fill_codes()  # 撤回新进的待确认当时就记下编号（D83）
        return hidden

    def _hide_withdrawn_relations(self, now: str) -> list[str]:
        """没有说法的关系（冻结的）照事实的办法（hide_withdrawn）：指着撤回内容的证据删掉，一条不剩的藏起来进待确认
        （claims 里是 relation）。返回藏起来的关系。"""
        rows = self.conn.execute(
            "SELECT v.evidence_id, v.relation_id, v.packet_id FROM relation_evidence AS v"
            " JOIN relations AS r ON r.relation_id = v.relation_id"
            " WHERE r.withdrawn_at IS NULL AND NOT EXISTS (SELECT 1 FROM relation_claims AS c WHERE c.relation_id = v.relation_id)"
            " AND (EXISTS (SELECT 1 FROM packets AS p WHERE p.packet_id = v.packet_id AND (p.withdrawn_at IS NOT NULL"
            " OR (p.revision = v.revision AND p.purged_at IS NOT NULL)))"
            " OR EXISTS (SELECT 1 FROM withdrawn_packets AS wp WHERE wp.packet_id = v.packet_id)"
            " OR (v.sentence_hash IS NOT NULL AND EXISTS (SELECT 1 FROM withdrawn_sentences AS w"
            " WHERE w.packet_id = v.packet_id AND w.sentence_hash = v.sentence_hash"
            " AND (w.message_id = '' OR v.message_id IS NULL OR w.message_id = v.message_id)))"
            " OR (v.sentence_hash IS NULL AND EXISTS (SELECT 1 FROM raw_sentences AS s WHERE s.sentence_id = v.sentence_id"
            " AND s.revision = v.revision AND s.withdrawn_at IS NOT NULL)))"
            " ORDER BY v.relation_id, v.evidence_id"
        ).fetchall()
        packets_of: dict[str, set[str]] = {}
        for row in rows:
            self.conn.execute("DELETE FROM relation_evidence WHERE evidence_id = ?", (row["evidence_id"],))
            packets_of.setdefault(row["relation_id"], set()).add(row["packet_id"])
        hidden = []
        families: set[str] = set()
        for relation_id, packets in sorted(packets_of.items()):
            self._relation_provenance(relation_id)  # 还有别的证据的照常看得到，来源按剩下的算；都没了的跟着清空
            if self.conn.execute("SELECT 1 FROM relation_evidence WHERE relation_id = ?", (relation_id,)).fetchone() is not None:
                continue
            families |= self._family_of(relation_id)
            self.conn.execute("UPDATE relations SET withdrawn_at = ?, updated_at = ? WHERE relation_id = ?", (now, now, relation_id))
            self.conn.execute(
                "DELETE FROM review_queue WHERE decided_at IS NULL AND kind = 'frozen_conflict'"
                " AND (json_extract(claims, '$.new') = ?1 OR json_extract(claims, '$.frozen') = ?1)",
                (relation_id,),
            )
            claims = {"relation": relation_id, "reason": EVIDENCE_WITHDRAWN, "packets": sorted(packets)}
            self.conn.execute(
                "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('other', ?, '[]', ?)", (_json(claims), now)
            )
            hidden.append(relation_id)
        self._refresh_relations(families, set(), now)  # 藏起来的不再走：和它冲突的、被它挡着的按剩下的重算（第二轮审阅 G1）
        return hidden

    def realign_after_withdrawal(self) -> set[str]:
        """撤回以后：碰过的实体里，还在、标准名的出处这次被删光了的（这个名字只在撤回的内容里出现过），用到它的说法
        要重新对齐：把这些说法记成“没写过”（cache_key 清空），它们所在的包改回排队，下次 javmem write 用结果库里
        存着的结果重写一遍（不调模型），按那时看得到的名字、别名对齐，和不收撤回的内容整本重建一样。
        重写以前事实照常看得到。返回要改回排队的包。不自己开事务。主人不算（标准名是定的）。

        只看这次删掉的出处里有没有标准名（D73 第 18 条），不看现在还有没有：L3 补全的标准名（原文只写“老周”，
        L3 写了全名）在窗口里找不到，本来就不记别名、没有出处，按“现在还有没有”算的话，撤回只要碰到这个实体就换名字、
        提到它的包都重写一遍，以后每次碰到都再来一遍，标准名也和整本重建的不一样。"""
        owner = entity_id_of("人", OWNER_NAME)
        packets: set[str] = set()
        for entity_id in sorted(self.touched_entities - {owner}):
            entity = self.conn.execute("SELECT name_key FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
            if entity is None:
                continue
            if (entity_id, entity["name_key"]) not in self.lost_names:
                continue  # 这次删掉的出处里没有标准名
            named = self.conn.execute(
                "SELECT 1 FROM aliases AS a WHERE a.entity_id = ? AND a.alias_key = ?"
                " AND EXISTS (SELECT 1 FROM evidence AS v WHERE v.alias_id = a.alias_id)",
                (entity_id, entity["name_key"]),
            ).fetchone()
            if named is not None:
                continue  # 标准名还有看得到的出处
            # 重写以前先换成还有出处的叫法（最早记下的那个），撤回的名字马上就看不到了；没有别的叫法的等重写
            fallback = self.conn.execute(
                "SELECT a.alias FROM aliases AS a JOIN evidence AS v ON v.alias_id = a.alias_id WHERE a.entity_id = ?"
                " ORDER BY v.evidence_id LIMIT 1",
                (entity_id,),
            ).fetchone()
            if fallback is not None:
                self.conn.execute("UPDATE entities SET canonical_name = ? WHERE entity_id = ?", (fallback[0], entity_id))
            rows = self.conn.execute(
                "SELECT claim_id, proofs FROM fact_claims WHERE subject_entity_id = ?1"
                " OR EXISTS (SELECT 1 FROM json_each(fact_claims.entities) WHERE json_extract(value, '$.entity_id') = ?1)",
                (entity_id,),
            ).fetchall()
            for row in rows:
                self.conn.execute("UPDATE fact_claims SET cache_key = '' WHERE claim_id = ?", (row["claim_id"],))
                packets |= {str(proof["packet_id"]) for proof in json.loads(row["proofs"])}
            for row in self.conn.execute(  # 关系的说法一样（J4.4）
                "SELECT claim_id, proofs FROM relation_claims WHERE subject_entity_id = ?1"
                " UNION SELECT claim_id, proofs FROM relation_claims WHERE object_entity_id = ?1"
                " UNION SELECT claim_id, proofs FROM relation_claims WHERE org_entity_id = ?1",
                (entity_id,),
            ).fetchall():
                self.conn.execute("UPDATE relation_claims SET cache_key = '' WHERE claim_id = ?", (row["claim_id"],))
                packets |= {str(proof["packet_id"]) for proof in json.loads(row["proofs"])}
        return packets

    def drop_dead_ambiguities(self) -> None:
        """没处理的“实体有歧义”，实体已经没有事实、别名了的（撤回以后），连同这个实体删掉：
        不然待确认一直挂着它，后来的按名字对齐还会对上它。不自己开事务。"""
        rows = self.conn.execute(
            "SELECT review_id, json_extract(claims, '$.entity_id') AS entity_id FROM review_queue"
            " WHERE kind = 'ambiguous_entity' AND decided_at IS NULL ORDER BY review_id"
        ).fetchall()
        dead = set()
        for row in rows:
            used = self.conn.execute(
                "SELECT 1 FROM facts WHERE subject_entity_id = ?1 UNION ALL SELECT 1 FROM fact_entities WHERE entity_id = ?1"
                " UNION ALL SELECT 1 FROM aliases WHERE entity_id = ?1"
                " UNION ALL SELECT 1 FROM relations WHERE subject_entity_id = ?1"
                " UNION ALL SELECT 1 FROM relations WHERE object_entity_id = ?1 LIMIT 1",
                (row["entity_id"],),
            ).fetchone()
            if used is None:
                self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (row["review_id"],))
                dead.add(str(row["entity_id"]))
        self._drop_orphan_entities(dead)

    def drop_stale_windows(self, packet_id: str, current_windows: Collection[str]) -> int:
        """这个包写完了：以前按它命名、这一批里已经没有的窗口（比如撤回了几句，窗口少了一个），只来自这个包的
        说法、这个包的隔离和别名的出处删掉（和重写一个窗口时清的一样），受影响的事实和组重算，没用了的实体删掉。
        返回删了几处说法。不自己开事务。"""
        prefix = f"{packet_id}#"
        rows = self.conn.execute(  # 写成区间才走得上 fact_claims_by_window，substr 要整张扫说法表（每写完一个包扫一遍）
            "SELECT claim_id, fact_id, window_id, subject_entity_id, entities, proofs FROM fact_claims"
            " WHERE window_id >= ? AND window_id < ?",
            window_range(packet_id),
        ).fetchall()
        stale = [row for row in rows if row["window_id"] not in current_windows and _proof_packets(row) <= {packet_id}]
        affected: set[str] = set()
        touched: set[str] = set()
        for row in stale:
            self.conn.execute("DELETE FROM fact_claims WHERE claim_id = ?", (row["claim_id"],))
            affected.add(row["fact_id"])
            touched |= _claim_entities(row)
        identities: set[str] = set()  # 关系的说法一样（J4.4）
        relation_rows = self.conn.execute(
            "SELECT claim_id, identity_key, window_id, subject_entity_id, object_entity_id, org_entity_id, proofs"
            " FROM relation_claims WHERE window_id >= ? AND window_id < ?",
            window_range(packet_id),
        ).fetchall()
        for row in relation_rows:
            if row["window_id"] not in current_windows and _proof_packets(row) <= {packet_id}:
                self.conn.execute("DELETE FROM relation_claims WHERE claim_id = ?", (row["claim_id"],))
                identities.add(row["identity_key"])
                touched |= _relation_claim_entities(row)
                stale.append(row)
        aliases = self.conn.execute(
            "SELECT v.evidence_id, v.alias_id, v.window_id, a.entity_id FROM evidence AS v JOIN aliases AS a USING (alias_id)"
            " WHERE v.alias_id IS NOT NULL AND v.packet_id = ? AND substr(v.window_id, 1, ?) = ?",
            (packet_id, len(prefix), prefix),
        ).fetchall()
        dropped = [row for row in aliases if row["window_id"] not in current_windows]
        for row in dropped:
            self.conn.execute("DELETE FROM evidence WHERE evidence_id = ?", (row["evidence_id"],))
            touched.add(row["entity_id"])
        self._drop_bare_aliases(row["alias_id"] for row in dropped)
        for row in self.conn.execute(
            "SELECT quarantine_id, window_id FROM quarantine WHERE packet_id = ? AND substr(window_id, 1, ?) = ?",
            (packet_id, len(prefix), prefix),
        ).fetchall():
            if row["window_id"] not in current_windows:
                self.conn.execute("DELETE FROM quarantine WHERE quarantine_id = ?", (row["quarantine_id"],))
        if affected:
            self._refresh(affected, {}, set(), _now())
        if identities:
            self._refresh_relations(identities, set(), _now())
        if touched:
            self._drop_orphan_entities(touched)
        return len(stale)

    # ---------- 实体和别名 ----------

    def _entities(self, item: Checked, now: str) -> tuple[dict[str, str | None], set[str]]:
        """L3 的实体对到账本里的实体。返回 (key → 实体编号, 有歧义的实体编号)。"""
        assert item.check is not None
        keys: dict[str, str | None] = {}
        ambiguous: set[str] = set()
        roles = _speaker_roles(item)
        said_roles = _speaker_roles(item, said_only=True)
        scope = item.window.scope
        speakers = sorted({block.speaker for block in item.window.blocks})
        for entity in item.check.entities:
            name = str(entity.get("name") or "").strip() or str(entity.get("mention") or "").strip()
            mention = str(entity.get("mention") or "").strip()
            entity_type = str(entity.get("type") or "")
            entity_key = str(entity["key"])
            if _is_owner(
                item, name, mention, entity_type, roles.get(entity_key, set()), self.owner_names, said_roles.get(entity_key, set())
            ):
                keys[str(entity["key"])] = self._owner(now)
                continue
            key = name_key(name)
            if not key or not usable_alias(name) or self.lexicon.not_entity(name):
                keys[str(entity["key"])] = None  # 泛称、一个字的名字、别名表里不建实体的：不建实体，主体是空的
                continue
            bot = self.lexicon.bot_of(name, mention, entity_type, speakers)
            if bot is not None:  # Bot 名单（D13、D92）：L3 给什么类型都改成 Bot，编号按 Bot 加标准名算
                entity_id = self._standard_entity(BOT, bot.standard, now)
                keys[entity_key] = entity_id
                for text in dict.fromkeys((name, mention)):
                    if text and usable_alias(text):
                        self._alias(item, entity_id, text, now)
                continue
            group = self.lexicon.group_of(name, entity_type, scope.partitions)
            if group is not None:  # 别名表第二版的组：对到标准名算出来的编号，和先后无关（J3.6）
                entity_type = group.type
                parts = name_parts(name, entity_type)
                entity_id = self._standard_entity(group.type, group.standard, now)
                if self._open_ambiguity(entity_id, scope):
                    ambiguous.add(entity_id)
                keys[entity_key] = entity_id
                for text in dict.fromkeys((name, mention, *parts.every)):
                    if text and usable_alias(text):
                        self._alias(item, entity_id, text, now)
                continue
            entity_id = entity_id_of(entity_type, key)
            parts = name_parts(name, entity_type)
            if self.conn.execute("SELECT 1 FROM entities WHERE entity_id = ?", (entity_id,)).fetchone() is None:
                candidates = self._alias_candidates(key, entity_type, item.window.scope)
                if not candidates:
                    candidates = self._other_name_candidates(key, parts, entity_type, item.window.scope)
                candidates = self._not_apart(candidates, key, entity_type)
                if len(candidates) == 1:
                    entity_id = candidates[0]
                else:
                    self.conn.execute(
                        "INSERT INTO entities (entity_id, canonical_name, type, created_at, updated_at, name_key)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (entity_id, name, entity_type, now, now, key),
                    )
                    self.stats.entities_created += 1
                    if candidates:
                        self._ambiguous(entity_id, name, entity_type, candidates, item.window, now)
            if self._open_ambiguity(entity_id, scope):
                ambiguous.add(entity_id)
            keys[str(entity["key"])] = entity_id
            for text in dict.fromkeys((name, mention, *parts.every)):  # 名字里拆出来的叫法也记成别名（J2.14）
                if text and usable_alias(text):
                    self._alias(item, entity_id, text, now)
        return keys, ambiguous

    def _other_name_candidates(self, key: str, parts: NameParts, entity_type: str, scope: Scope) -> list[str]:
        """名字、别名都对不上时（J2.14）：按名字里拆出来的叫法（merge.name_parts）、仓库外别名表里同组的名字找，
        也只认同一可见范围的别名。括号外面那段、代码、别名表里的名字按别名找；括号里的另一个叫法只对实体自己的名字，
        或者它名字里括号外面那段（“松岚（Corvalis）”的“松岚”）：别的实体括号里拆出来的别名不算，免得括号里的说明
        把不同的人连起来。"""
        by_alias = {name_key(part) for part in (*parts.outer, *parts.codes)}
        by_name = {name_key(part) for part in parts.inner}
        for one in [key, *sorted(by_alias | by_name)]:
            by_alias |= self.lexicon.same_as(one, entity_type, name_key)
        found: set[str] = set()
        for one in sorted(by_alias - {key, ""}):
            found.update(self._alias_candidates(one, entity_type, scope))
        for one in sorted(by_name - by_alias - {key, ""}):
            for candidate in self._alias_candidates(one, entity_type, scope):
                row = self.conn.execute("SELECT canonical_name FROM entities WHERE entity_id = ?", (candidate,)).fetchone()
                own = {name_key(row["canonical_name"]), *(name_key(part) for part in name_parts(row["canonical_name"], entity_type).outer)}
                if one in own:
                    found.add(candidate)
        return sorted(found)

    def _not_apart(self, candidates: Sequence[str], key: str, entity_type: str) -> list[str]:
        """按别名、拆名字找到的候选里，和这个名字记成“不是同一个”的去掉（别名表的 apart、合并工具记的，J3.6）。"""
        apart = self.lexicon.apart_from(key, entity_type)
        if not apart:
            return list(candidates)
        kept = []
        for candidate in candidates:
            row = self.conn.execute("SELECT name_key FROM entities WHERE entity_id = ?", (candidate,)).fetchone()
            if row is None or row["name_key"] not in apart:
                kept.append(candidate)
        return kept

    def _standard_entity(self, entity_type: str, standard: str, now: str) -> str:
        """别名表里一组（或者一个 Bot）的实体：编号按类型和标准名算，标准名就是组里第一个名字，不看先出现的是哪个写法。"""
        key = name_key(standard)
        entity_id = entity_id_of(entity_type, key)
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO entities (entity_id, canonical_name, type, created_at, updated_at, name_key)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (entity_id, standard, entity_type, now, now, key),
        )
        self.stats.entities_created += cursor.rowcount
        if not cursor.rowcount:  # 以前按名字建过（写法的大小写、空白可能不一样）：标准名照别名表
            self.conn.execute(
                "UPDATE entities SET canonical_name = ?, updated_at = ? WHERE entity_id = ? AND canonical_name != ?",
                (standard, now, entity_id, standard),
            )
        return entity_id

    def _owner(self, now: str) -> str:
        entity_id = entity_id_of("人", OWNER_NAME)
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO entities (entity_id, canonical_name, type, created_at, updated_at, name_key)"
            " VALUES (?, ?, '人', ?, ?, ?)",
            (entity_id, OWNER_NAME, now, now, OWNER_NAME),
        )
        self.stats.entities_created += cursor.rowcount
        return entity_id

    def _alias_candidates(self, key: str, entity_type: str, scope: Scope) -> list[str]:
        """按别名找实体：只认出处和这个窗口可见范围一样的别名（D30：看不到的内容不能把两个叫法连起来）。"""
        rows = self.conn.execute(
            "SELECT DISTINCT a.entity_id, p.partitions, p.sensitive FROM aliases AS a"
            " JOIN entities AS e ON e.entity_id = a.entity_id"
            " JOIN evidence AS v ON v.alias_id = a.alias_id"
            " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision"
            " WHERE a.alias_key = ? AND e.type = ? AND a.withdrawn_at IS NULL AND p.state = 'current'"
            " AND p.withdrawn_at IS NULL ORDER BY a.entity_id",
            (key, entity_type),
        ).fetchall()
        return sorted({row["entity_id"] for row in rows if _scope_of_row(row["partitions"], row["sensitive"]) == scope})

    def _open_ambiguity(self, entity_id: str, scope: Scope, known: dict[tuple[str, str], bool] | None = None) -> bool:
        """这个实体在这个可见范围里有没有处理的歧义（D30：别的范围里的歧义不算）。没记范围的旧条目哪个范围都算。

        known 不是 None 时，查过的（实体、范围）记在里面，再问就不再查：调用的地方要保证这期间待确认没有变（_supersede）。"""
        key = scope_key(scope)
        if known is not None and (entity_id, key) in known:
            return known[(entity_id, key)]
        found = (
            self.conn.execute(
                "SELECT 1 FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NULL"
                " AND json_extract(claims, '$.entity_id') = ? AND coalesce(json_extract(claims, '$.scope'), ?) = ?",
                (entity_id, key, key),
            ).fetchone()
            is not None
        )
        if known is not None:
            known[(entity_id, key)] = found
        return found

    def _decided_ambiguity(self, entity_id: str, scope: Scope) -> bool:
        key = scope_key(scope)
        return (
            self.conn.execute(
                "SELECT 1 FROM review_queue WHERE kind = 'ambiguous_entity' AND decided_at IS NOT NULL"
                " AND json_extract(claims, '$.entity_id') = ? AND coalesce(json_extract(claims, '$.scope'), ?) = ?",
                (entity_id, key, key),
            ).fetchone()
            is not None
        )

    def _ambiguous(
        self, entity_id: str, name: str, entity_type: str, candidates: Sequence[str], window: Window, now: str
    ) -> None:
        if self._open_ambiguity(entity_id, window.scope) or self._decided_ambiguity(entity_id, window.scope):
            return  # 还没处理的不重复进；主人定过的（两边都留）不再问：包重发以后实体删了又建，决定还算数
        claims = {
            "entity_id": entity_id,
            "name": name,
            "type": entity_type,
            "candidates": list(candidates),
            "window_id": window.window_id,
            "scope": scope_key(window.scope),
        }
        self.conn.execute(
            "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('ambiguous_entity', ?, '[]', ?)",
            (_json(claims), now),
        )
        self.stats.ambiguous_entities += 1

    def _alias(self, item: Checked, entity_id: str, text: str, now: str) -> None:
        """别名连同出处：窗口里第一句写着它的话，能当证据的行优先。窗口里找不到的不记。"""
        needle, _ = fold_map(text)
        if not needle:
            return
        redacted = set(item.rendered.redacted_refs)
        for line in sorted(item.window.lines, key=lambda line: not line.target):
            shown = item.rendered.shown.get(line.ref, "")
            folded, where = fold_map(shown)
            position = folded.find(needle)
            if position < 0:
                continue
            start, end = where[position], where[position + len(needle) - 1] + 1
            clean = line.ref not in redacted and not line.clipped and len(line.text) == len(shown)
            quote = line.text[start:end] if clean else shown[start:end]
            raw = self.conn.execute("SELECT * FROM raw_sentences WHERE sentence_id = ?", (line.sentence_id,)).fetchone()
            if raw is None:
                continue
            key = name_key(text)
            alias_id = alias_id_of(entity_id, key)
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO aliases (alias_id, entity_id, alias, status, created_at, alias_key)"
                " VALUES (?, ?, ?, 'verified', ?, ?)",
                (alias_id, entity_id, text, now, key),
            )
            self.stats.aliases_created += cursor.rowcount
            # 同一个实体、同一个写法以前记过（编号可能是旧规则算的），用它实际的编号
            existing = self.conn.execute(
                "SELECT alias_id FROM aliases WHERE alias_id = ? OR (entity_id = ? AND alias = ?) ORDER BY alias_id = ? DESC",
                (alias_id, entity_id, text, alias_id),
            ).fetchone()
            if existing is None:
                return
            self.conn.execute(
                "INSERT OR IGNORE INTO evidence (alias_id, packet_id, revision, sentence_id, quote, position, created_at, window_id,"
                " source, origin, speaker_role, sentence_hash, message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    existing["alias_id"],
                    line.packet_id,
                    line.revision,
                    line.sentence_id,
                    quote,
                    _json(_position(raw)),
                    now,
                    item.window.window_id,
                    *self._evidence_meta(line.packet_id, line.revision, line.sentence_id),
                ),
            )
            return

    # ---------- 说法 ----------

    def _claim(
        self, item: Checked, fact: FactCheck, keys: Mapping[str, str | None], now: str, *, judge_hold: str | None = None
    ) -> str | None:
        """写一处说法，返回它合到的事实编号。事实这一行没有就先占上，列在 _refresh_fact 里算。judge_hold 是进图前检查转人工的
        原因（J5.3）：主人对同一句、同一条事实定过“不要”的不写，返回 None（照旧进隔离表）。"""
        raw = fact.raw
        window = item.window
        content = str(raw.get("text") or "").strip()
        subject = keys.get(str(raw.get("subject"))) if raw.get("subject") is not None else None
        attribute = str(raw.get("attribute") or "").strip() or None
        value = str(raw.get("value") or "").strip() or None
        said = min(proof.said_at for proof in fact.proofs).date() if fact.proofs else None
        key = merge_key(window.scope, subject, attribute, value, content, said=said)
        fact_id = fact_id_of(key)
        proofs = []
        for proof in fact.proofs:
            row = self.conn.execute("SELECT * FROM raw_sentences WHERE sentence_id = ?", (proof.sentence_id,)).fetchone()
            if row is None:  # 原文索引里没有这一句：取包时就核对过，不该出现
                raise sqlite3.IntegrityError("证据的句子不在原文索引里")
            proofs.append(
                {
                    "sentence_id": proof.sentence_id,
                    "packet_id": proof.packet_id,
                    "revision": proof.revision,
                    "quote": proof.quote,
                    "position": _position(row),
                    "said_at": iso(proof.said_at),
                    "said_epoch": epoch(proof.said_at),
                    "speaker": proof.speaker,
                    "speaker_role": proof.speaker_role,
                }
            )
        proofs.sort(key=_proof_order)
        if judge_hold is not None:  # 主人对这一句、这条事实定过“不要”：不写，照旧进隔离表
            pair = (str(proofs[0]["sentence_id"]), fact_id)
            decided = self._held_lookup(MANUAL_REVIEW, "fact", [pair], decided=True).get(pair)
            if decided is not None and decided["decision"] == DROP:
                return None
        roles: dict[tuple[str, str], None] = {}
        if subject is not None:
            roles[(subject, "subject")] = None
        for role in raw.get("entities") or []:
            if isinstance(role, Mapping):
                entity_id = keys.get(str(role.get("key")))
                if entity_id is not None and role.get("role") in ("subject", "object", "mention"):
                    roles[(entity_id, str(role["role"]))] = None
        self.conn.execute(
            "INSERT OR IGNORE INTO facts (fact_id, content, status, scope_partitions, scope_sensitive, created_at, updated_at,"
            " merge_key, group_key) VALUES (?, ?, 'inferred', ?, ?, ?, ?, ?, ?)",
            (
                fact_id,
                content,
                _json(list(window.scope.partitions)),
                _json(list(window.scope.sensitive)),
                now,
                now,
                key,
                group_key(window.scope, subject, attribute),
            ),
        )
        first = proofs[0]
        latest_proof = max(proofs, key=lambda proof: proof["said_epoch"])
        # 说话人分两列（J4.1）：speaker 是证据那一句是谁说的（上面按原文索引取的），attributed_to 是 L3 写的“这条说法出自谁”
        # （Bot 转述的主人的话写主人）。旧样子的结果没有，是空的
        attributed_to = str(raw.get("speaker") or "").strip() or None
        # 软变化的候选句（J4.4 第 4 段）：有属性的才可能改到组里以前的（没有属性的不分组、不参加新旧）
        soft = self._soft_sentence(proofs, str(raw.get("certainty")) == "vague") if attribute else None
        # 进图前检查（J5.3）：谁认的（开了才记；转人工的是空的，主人说留下以后算主人认的）
        judged = precheck.judged_tier(fact.judge) if self.precheck_on and judge_hold is None else None
        self.conn.execute(
            "INSERT INTO fact_claims (claim_id, fact_id, window_id, cache_key, extracted_by, l4_version, content,"
            " subject_entity_id, attribute, value, occurred_at, time_precision, certainty, status, speaker, speaker_role,"
            " said_from, said_from_epoch, said_to, said_to_epoch, proofs, entities, notes, created_at, attributed_to,"
            " soft_sentence, judge_hold, judged_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?)",
            (
                f"{window.window_id}#{item.cache_key[:12]}#{fact.index}",
                fact_id,
                window.window_id,
                item.cache_key,
                self.extracted_by,
                self.checked_by,
                content,
                subject,
                attribute,
                value,
                fact.occurred_at,
                fact.time_precision,
                str(raw.get("certainty")),
                fact.status,
                first["speaker"],
                first["speaker_role"],
                first["said_at"],
                first["said_epoch"],
                latest_proof["said_at"],
                latest_proof["said_epoch"],
                _json(proofs),
                _json([{"entity_id": entity_id, "role": role} for entity_id, role in roles]),
                _json(fact.notes),
                now,
                attributed_to,
                soft,
                judge_hold,
                judged,
            ),
        )
        self.stats.claims += 1
        return fact_id

    # ---------- 事实：从它的说法算 ----------

    def _claims_of(self, fact_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM fact_claims WHERE fact_id = ? ORDER BY claim_id", (fact_id,)).fetchall()

    @staticmethod
    def _view(row: sqlite3.Row) -> ClaimView:
        return ClaimView(
            claim_id=row["claim_id"],
            content=row["content"],
            subject_entity_id=row["subject_entity_id"],
            attribute=row["attribute"],
            value=row["value"],
            occurred_at=row["occurred_at"],
            time_precision=row["time_precision"],
            status=row["status"],
            speaker=row["speaker"],
            speaker_role=row["speaker_role"],
            said_from=row["said_from"],
            said_from_epoch=row["said_from_epoch"],
            said_to=row["said_to"],
            said_to_epoch=row["said_to_epoch"],
            notes=tuple(json.loads(row["notes"])),
            extracted_by=row["extracted_by"],
            l4_version=row["l4_version"],
            attributed_to=row["attributed_to"],
        )

    def _status(
        self,
        claims: Sequence[ClaimView],
        subject: str | None,
        ambiguous: set[str],
        scope: Scope,
        known: dict[tuple[str, str], bool] | None = None,
    ) -> tuple[str, str | None]:
        if subject is not None and (subject in ambiguous or self._open_ambiguity(subject, scope, known)):
            return "pending", "ambiguous_entity"
        return base_status(claims), None

    def _refresh_fact(self, fact_id: str, vectors: Mapping[str, bytes], ambiguous: set[str], now: str) -> str | None:
        """按说法重算一条事实。没有说法了就删掉（冻结的留着）。返回它的组（要重算新旧），没有组是 None。"""
        current = self.conn.execute(
            "SELECT group_key, frozen, scope_partitions, scope_sensitive, withdrawn_at FROM facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if current is None:
            return None
        group = current["group_key"]
        scope = _scope_of_row(current["scope_partitions"], current["scope_sensitive"])
        rows = self._claims_of(fact_id)
        if not rows:
            if current["frozen"]:
                # 冻结的是主人定的，说法没了也不自动删：替换以后证据还指着旧版本；撤回以后 hide_withdrawn 藏起来进待确认
                return group
            self.conn.execute("UPDATE facts SET superseded_by = NULL, superseded_at = NULL WHERE superseded_by = ?", (fact_id,))
            self.conn.execute(  # 进图前检查转人工的（J5.3，manual_review 的 item 是 fact）也跟着删
                "DELETE FROM review_queue WHERE decided_at IS NULL AND ((kind = 'frozen_conflict'"
                " AND (json_extract(claims, '$.new') = ?1 OR json_extract(claims, '$.frozen') = ?1))"
                " OR (kind IN ('soft_change', 'manual_review') AND json_extract(claims, '$.fact') = ?1))",
                (fact_id,),
            )
            self.conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            return group
        if current["withdrawn_at"] is not None:
            # 证据都撤回了、藏起来的冻结事实，后来又有了说法（别的包又说了，或者解除撤回以后重抽）：有看得到的证据撑着，
            # 放回来；那条“证据都撤回了”的待确认关掉。不然主人定“删掉”会连新说法一起删
            self.conn.execute("UPDATE facts SET withdrawn_at = NULL WHERE fact_id = ?", (fact_id,))
            self.conn.execute(
                "UPDATE review_queue SET decision = ?, decided_at = ?, decided_by = 'auto' WHERE decided_at IS NULL"
                " AND kind = 'other' AND json_extract(claims, '$.fact') = ?",
                (RESUPPORTED, now, fact_id),
            )
        # 进图前检查转人工、主人还没说留下的（J5.3）：冻结的是主人担保的，不挡（审阅第 2 条）。挡着的那几处不给这条事实文字、
        # 时间、证据（审阅第 3 条）；只剩这种说法的照全部算（这条本来就是待确认，不进图）
        held = {} if current["frozen"] else self._fact_holds(rows)
        live = [row for row in rows if str(row["claim_id"]) not in held] or rows
        claims = [self._view(row) for row in live]
        first = representative(claims)
        last = latest(claims)
        status, reason = self._status(claims, first.subject_entity_id, ambiguous, scope)
        if held and len(held) == len(rows) and status != "pending":  # 只有这种说法：待确认，等主人
            status, reason = "pending", MANUAL_REVIEW
        notes = sorted({note for claim in claims for note in claim.notes})
        self.conn.execute(
            "UPDATE facts SET content = ?, subject_entity_id = ?, attribute = ?, value = ?, occurred_from = ?,"
            " time_precision = ?, status = ?, pending_reason = ?, speaker = ?, speaker_role = ?, attributed_to = ?, as_of = ?,"
            " as_of_epoch = ?, notes = ?, extracted_by = ?, rule_version = ?, updated_at = ?,"
            " judged_by = CASE WHEN frozen = 1 THEN 'owner' ELSE ? END WHERE fact_id = ?",
            (
                first.content,
                first.subject_entity_id,
                first.attribute,
                first.value,
                first.occurred_at,
                first.time_precision,
                status,
                reason,
                first.speaker,
                first.speaker_role,
                first.attributed_to,
                last.said_to,
                last.said_to_epoch,
                _json(notes),
                first.extracted_by,
                f"{first.l4_version} {L5_VERSION}",
                now,
                self._judged_of(False, rows, held),
                fact_id,
            ),
        )
        if self.precheck_on or any(row["judge_hold"] for row in rows) or self._open_fact_holds():
            self._fact_reviews(fact_id, rows, held, now)  # 转人工的进待确认、不再挡着的删掉（J5.3）
        # 证据：各处说法的证据合起来，一句只记一次，按说法的先后
        self.conn.execute("DELETE FROM evidence WHERE fact_id = ?", (fact_id,))
        self.conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
        seen: set[str] = set()
        roles: dict[tuple[str, str], None] = {}
        for row in sorted(live, key=lambda row: (row["said_from_epoch"], row["claim_id"])):
            for proof in json.loads(row["proofs"]):
                if proof["sentence_id"] in seen:
                    continue
                seen.add(proof["sentence_id"])
                self.conn.execute(
                    "INSERT INTO evidence (fact_id, packet_id, revision, sentence_id, quote, position, created_at, window_id,"
                    " source, origin, speaker_role, sentence_hash, message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fact_id,
                        proof["packet_id"],
                        proof["revision"],
                        proof["sentence_id"],
                        proof["quote"],
                        _json(proof["position"]),
                        now,
                        row["window_id"],
                        *self._evidence_meta(proof["packet_id"], proof["revision"], proof["sentence_id"]),
                    ),
                )
        # 涉及的实体照全部说法算（转人工的那一处也算，J5.3）：只按判过的算的话，只有转人工那一处提到的实体没人用、写完窗口就被
        # 当没用的删掉，主人以后说留下、判过的那一处撤回时再要挂上它，外键对不上、整笔撤销（第二遍审阅第 1 条）
        for row in sorted(rows, key=lambda row: (row["said_from_epoch"], row["claim_id"])):
            for role in json.loads(row["entities"]):
                roles[(role["entity_id"], role["role"])] = None
        self.conn.executemany(
            "INSERT INTO fact_entities (fact_id, entity_id, role) VALUES (?, ?, ?)",
            [(fact_id, entity_id, role) for entity_id, role in sorted(roles)],
        )
        self._provenance(fact_id, stated=base_status(claims) == "verified")
        self._vector(fact_id, first.content, vectors, now)
        return group

    # ---------- 进图前检查转人工的事实（J5.3，javmem.pipeline.precheck） ----------

    def _fact_holds(self, rows: Sequence[sqlite3.Row]) -> dict[str, tuple[str, str]]:
        """这几处说法里进图前检查转人工的（judge_hold 不是空的）、主人还没说留下的：说法编号 → (那一句, 事实编号)。那一句是
        这一处证据里排在最前面的（写说法时排好的）。没有转人工的说法不查待确认表（没开的一句 SQL 都不多跑）。"""
        pairs: dict[str, tuple[str, str]] = {}
        for row in rows:
            if row["judge_hold"]:
                proofs = json.loads(row["proofs"])
                pairs[str(row["claim_id"])] = (str(proofs[0]["sentence_id"]) if proofs else "", str(row["fact_id"]))
        if not pairs:
            return {}
        kept = self._held_lookup(MANUAL_REVIEW, "fact", pairs.values(), decided=True)
        return {claim_id: pair for claim_id, pair in pairs.items() if pair not in kept or kept[pair]["decision"] != KEEP_NEW}

    @staticmethod
    def _judged_of(frozen: bool, rows: Sequence[sqlite3.Row], held: Collection[str]) -> str | None:
        """一条事实是谁认的（facts.judged_by，进图的口子只放不是空的）：冻结的是主人；不然照说法，转人工、主人还没说留下的不算，
        主人说留下的算主人认的，别的照这一处记下的（local、llm）；有一处没认过就是空的（precheck.strongest）。"""
        if frozen:
            return precheck.BY_OWNER
        tiers: list[str | None] = []
        for row in rows:
            if row["judge_hold"]:
                if str(row["claim_id"]) not in held:
                    tiers.append(precheck.BY_OWNER)
                continue
            tiers.append(row["judged_by"])
        return precheck.strongest(tiers)

    def _fact_reviews(self, fact_id: str, rows: Sequence[sqlite3.Row], held: Mapping[str, tuple[str, str]], now: str) -> None:
        """进图前检查转人工的那几处进待确认（manual_review，claims 的 item 是 fact、原因是 C:…）：同一句、同一条只进一次（定过的
        也不再进）；不再挡着的（那一处没了、主人说留下了）没处理的删掉。"""
        reasons = {str(row["claim_id"]): str(row["judge_hold"]) for row in rows if row["judge_hold"]}
        for claim_id, (sentence, _) in sorted(held.items()):
            self._hold_review(MANUAL_REVIEW, "fact", "fact", fact_id, claim_id, sentence, fact_id, {"reason": f"C:{reasons[claim_id]}"},
                              now)
        self._unhold(MANUAL_REVIEW, "fact", [fact_id], set(held.values()))

    def _evidence_meta(self, packet_id: str, revision: int, sentence_id: str) -> tuple[object, ...]:
        """证据那一句的来源、origin、说话人是谁、指纹、是哪封信（邮件才有）：从包登记、原文索引取（这一句所在的
        那个版本），不从模型的回复取。"""
        row = self.conn.execute(
            "SELECT p.source, s.origin, s.speaker_role, s.speaker, s.text, s.message_id FROM packets AS p"
            " LEFT JOIN raw_sentences AS s ON s.sentence_id = ? AND s.packet_id = p.packet_id AND s.revision = p.revision"
            " WHERE p.packet_id = ? AND p.revision = ?",
            (sentence_id, packet_id, revision),
        ).fetchone()
        if row is None:
            return (None, None, None, None, None)
        digest = sentence_hash(row["speaker"], row["text"]) if row["text"] is not None else None
        return (row["source"], row["origin"], row["speaker_role"], digest, row["message_id"])

    def _provenance(self, fact_id: str, *, stated: bool | None = None) -> None:
        """来源和信任（J2.13）：按这条事实的证据算哪几个来源、哪几种 origin、是不是全是转述、有没有主人自己的话。
        stated 是原话还是推断（有一处说法 verified）；None 是没有说法了（冻结的），照原来的不动。

        这几样都从原文和包登记来，不看模型写了什么：邮件里写“我是主人”、外部网页里写“已核对”，都改变不了它们。
        """
        rows = self.conn.execute("SELECT source, origin, speaker_role FROM evidence WHERE fact_id = ?", (fact_id,)).fetchall()
        sources = sorted({row["source"] for row in rows if row["source"]})
        origins = sorted({row["origin"] for row in rows if row["origin"]})
        secondhand = bool(rows) and all(row["origin"] is not None and row["origin"] != ORIGIN_SAID for row in rows)
        owner_said = any(row["origin"] == ORIGIN_SAID and row["speaker_role"] == ROLE_OWNER for row in rows)
        if stated is None:
            self.conn.execute(
                "UPDATE facts SET sources = ?, origins = ?, secondhand = ?, owner_said = ? WHERE fact_id = ?",
                (_json(sources), _json(origins), int(secondhand), int(owner_said), fact_id),
            )
            return
        self.conn.execute(
            "UPDATE facts SET sources = ?, origins = ?, secondhand = ?, stated = ?, owner_said = ? WHERE fact_id = ?",
            (_json(sources), _json(origins), int(secondhand), int(stated), int(owner_said), fact_id),
        )

    def _vector(self, fact_id: str, content: str, vectors: Mapping[str, bytes], now: str) -> None:
        """事实文字的向量。这段文字的向量现在没有，先删掉旧的那行（文字或者模型不一样了），写完再补。

        包重发时（remove_revision）不知道现在配的是哪个模型：文字没变的留着，换没换模型下次 javmem write 再看。
        """
        digest = text_sha256(content)
        row = self.conn.execute("SELECT model, text_sha256 FROM fact_vectors WHERE fact_id = ?", (fact_id,)).fetchone()
        if row is not None and row["text_sha256"] == digest and (row["model"] == self.embedding_model or not self.embedding_model):
            return
        vector = vectors.get(content)
        if vector is None or len(vector) != 4 * EMBEDDING_DIM:
            if row is not None:
                self.conn.execute("DELETE FROM fact_vectors WHERE fact_id = ?", (fact_id,))
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO fact_vectors (fact_id, model, dim, text_sha256, vector, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (fact_id, self.embedding_model, EMBEDDING_DIM, digest, vector, now),
        )

    # ---------- 关系（J4.4）：说法写进来，关系从说法算 ----------

    def _proof_rows(self, proofs: Iterable[Proof]) -> list[dict[str, object]]:
        """核过的证据写成说法里存的样子（和事实的说法一样），按说话时间排。"""
        out = []
        for proof in proofs:
            sentence_id = proof.sentence_id
            row = self.conn.execute("SELECT * FROM raw_sentences WHERE sentence_id = ?", (sentence_id,)).fetchone()
            if row is None:  # 原文索引里没有这一句：取包时就核对过，不该出现
                raise sqlite3.IntegrityError("证据的句子不在原文索引里")
            said_at = proof.said_at
            out.append(
                {
                    "sentence_id": sentence_id,
                    "packet_id": proof.packet_id,
                    "revision": proof.revision,
                    "quote": proof.quote,
                    "position": _position(row),
                    "said_at": iso(said_at),
                    "said_epoch": epoch(said_at),
                    "speaker": proof.speaker,
                    "speaker_role": proof.speaker_role,
                }
            )
        out.sort(key=_proof_order)
        return out

    def _relation_claims(self, item: Checked, keys: Mapping[str, str | None], now: str) -> set[str]:
        """这个窗口过了 L4 的关系，一条写一处说法（relation_claims），返回写了说法的关系（同一条关系的键）。

        两头照这个窗口写事实时对好的实体（keys）。一头 L5 不建实体的（泛称、一个字的名字、别名表里不建实体的）写不进账本
        （关系表两头都要是实体），两头对成了同一个实体的也不写：都进隔离表，规则 L5。"""
        assert item.check is not None
        window = item.window
        revisions = dict(window.packets)
        first_target = window.targets[0]
        identities: set[str] = set()
        held: list[tuple[object, ...]] = []
        self.moved_relations = set()
        entries: list[tuple[RelationCheck, str | None, str, str]] = []
        # 判断 C 拿不准的（llm_unsure、llm_failed）也写成说法，关系记成待确认，原因用 manual_review；这些不再进隔离表（主会话 11-17）
        moved = [(relation, code) for relation in item.check.relations if (code := _judge_hold(relation)) is not None]
        for relation, judge_hold in [(relation, None) for relation in item.check.passed_relations] + moved:
            raw = relation.raw
            subject = keys.get(str(raw.get("subject")))
            target = keys.get(str(raw.get("object")))
            reason = L5_RELATION_ENDS if subject is None or target is None else L5_RELATION_SAME if subject == target else None
            if reason is not None and judge_hold is not None:
                continue  # 两头写不进账本的：照旧进隔离表（C 的原因在那一行里）
            if reason is not None:
                packet = next(((proof.packet_id, revisions[proof.packet_id]) for proof in relation.proofs
                               if proof.packet_id in revisions), (first_target.packet_id, first_target.revision))
                payload = {
                    "window_id": window.window_id,
                    "index": relation.index,
                    "relation": raw,
                    "failures": [f"L5:{reason}"],
                    "notes": relation.notes,
                    "dropped": [],
                    "proofs": [
                        {"sentence_id": proof.sentence_id, "packet_id": proof.packet_id, "revision": proof.revision}
                        for proof in relation.proofs
                    ],
                }
                held.append((*packet, "relation", _json(payload), f"L5:{reason}", "L5", L5_VERSION))
                self.stats.quarantined_relations += 1
                self.stats.relation_by_rule["L5"] = self.stats.relation_by_rule.get("L5", 0) + 1
                continue
            entries.append((relation, judge_hold, str(subject), str(target)))
        # J4.6：先算每一处的证据、软变化的候选句、是不是主人在纠正（软变化优先：有犹豫词的不当纠正），再看 Bot 当场说错的（R05）
        prepared: list[Prepared] = []
        for relation, judge_hold, subject_id, target_id in entries:
            proofs = self._proof_rows(relation.proofs)
            state = L3_RELATION_STATES.get(str(relation.raw.get("state")), "active")
            vague = state == "paused" or str(relation.raw.get("certainty") or "stated") == "vague"
            claimed = correct.correction_sentence(self.conn, proofs)
            # R05 的另一半（真调测试题 K05、K02）：主人纠正的这一处，L3 把 Bot 紧挨着的上一条也放进了证据（_on_spot）。那几句
            # 不拿来认软变化（Bot 问的“……吗？”、Bot 的“先暂缓”不是主人拿不准）；说它结束了的这一处，其中 Bot 说的（不是问话，
            # 当场说错的那句）也不算证据，写成说法以后记进隔离表。主人自己的话有犹豫词、照软变化办的，Bot 那句照旧在证据里，
            # 候选句挑主人的话（审阅第 6 条）
            adjacent = self._on_spot(proofs, claimed)
            soft = self._soft_sentence([proof for number, proof in enumerate(proofs) if number not in adjacent], vague)
            closes = self._closes(state, relation.valid_to, proofs)
            spot = [number for number in adjacent
                    if closes and not correct.is_question(correct.said_text(self.conn, proofs[number]) or "")]
            if soft is not None or len(spot) == len(proofs):
                spot = []
            kept = [proof for number, proof in enumerate(proofs) if number not in spot]
            fix = None if soft else claimed
            prepared.append((relation, judge_hold, subject_id, target_id, kept, soft, fix, [proofs[number] for number in spot]))
        echoes = self._echoes(window, prepared)
        for index, (relation, judge_hold, subject_id, target_id, proofs, soft, fix, dropped) in enumerate(prepared):
            if index in echoes:
                # R05：Bot 上一条说的、主人紧接着说不对的同一组的那一条，不记（会错了意、主人当场纠正的，不记复述那一版）。
                # payload 里记着是哪一句纠正的：那一句撤回了，这一行跟着清（撤回清隔离表按 payload 里的句子编号认）
                packet = next(((proof.packet_id, revisions[proof.packet_id]) for proof in relation.proofs
                               if proof.packet_id in revisions), (first_target.packet_id, first_target.revision))
                payload = {
                    "window_id": window.window_id, "index": relation.index, "relation": relation.raw,
                    "failures": [f"L5:{correct.ON_SPOT}"], "notes": relation.notes, "dropped": [],
                    "proofs": [{"sentence_id": proof.sentence_id, "packet_id": proof.packet_id, "revision": proof.revision}
                               for proof in relation.proofs],
                    "corrected_by": echoes[index],
                }
                held.append((*packet, "relation", _json(payload), f"L5:{correct.ON_SPOT}", "L5", L5_VERSION))
                self.stats.quarantined_relations += 1
                self.stats.corrected_on_spot += 1
                self.stats.relation_by_rule["L5"] = self.stats.relation_by_rule.get("L5", 0) + 1
                if judge_hold is not None:  # 判断 C 也拿不准的：隔离表里只进 R05 这一行，不再按 C 的原因进一行（第一轮审阅第 11 条）
                    self.moved_relations.add((window.window_id, relation.index))
                continue
            identity = self._relation_claim(item, relation, subject_id, target_id, now, judge_hold=judge_hold,
                                            prepared=(proofs, soft, fix, dropped))
            if identity is None:
                continue  # 主人定过不要的：照旧进隔离表（Bot 那句不另记一行，审阅第 4 条）
            for proof in dropped:
                # R05 的另一半：去掉的 Bot 那句一句一行（和 L4 去掉的证据一样是 relation_evidence）。payload 里记着是哪一句纠正的
                # （那一句、Bot 那句撤回了，这一行跟着清：撤回清隔离表按 payload 里的句子编号认），和是哪一处说法的
                # （claim_id：按关系撤回时 Bot 那句跟着撤，不然重抽时照 Bot 那句又记回来，审阅第 1 条）
                packet_id = str(proof["packet_id"])
                payload = {
                    "window_id": window.window_id, "index": relation.index, "claim_id": _claim_id(item, relation),
                    "relation": {name: relation.raw.get(name) for name in ("subject", "relation", "object")},
                    "evidence": {name: proof[name] for name in ("sentence_id", "packet_id", "revision", "quote")},
                    "corrected_by": fix,
                }
                held.append((packet_id, revisions.get(packet_id, proof["revision"]), "relation_evidence", _json(payload),
                             f"L5:{correct.ON_SPOT}", "L5", L5_VERSION))
                self.stats.quarantined_relation_evidence += 1
            if dropped:
                self.stats.corrected_on_spot += 1
            identities.add(identity)
            if judge_hold is not None:
                self.moved_relations.add((window.window_id, relation.index))
        self.conn.executemany(
            "INSERT INTO quarantine (packet_id, revision, item_kind, payload, reason, rule_id, rule_version, created_at, window_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*row, now, window.window_id) for row in held],
        )
        return identities

    def _echo_group(self, window: Window, relation: RelationCheck, subject: str, target: str) -> str:
        """R05 认“同一组”：判新旧的组（负责照代表哪一家分开）；亲属是同一对人，不管哪一种（纠正成另一种亲属也算）。"""
        relation_type = str(relation.raw.get("relation"))
        if relation_type == KINSHIP:
            return "\n".join(("kin", scope_key(window.scope), subject, target))
        identity = relation_identity(window.scope, subject, relation_type, target)
        org = (self._employer(subject, _json(list(window.scope.partitions)), _json(list(window.scope.sensitive)))
               if relation_type == "负责" else None)
        return relation_group_key(window.scope, relation_type, subject, target, org_id=org, identity=identity)

    def _on_spot(self, proofs: Sequence[Mapping[str, object]], fix: str | None) -> list[int]:
        """R05 的另一半（J4.6，真调测试题 K05、K02）：主人纠正的这一处说法（fix 是带纠正词的那一句），证据里 Bot 自己说的、
        所在的那条消息后面紧接着就是带纠正词那一句所在的那条消息的那几句：下标（问话也在里面，去不去由 _relation_claims 定）。
        证据里主人后来说的别的（“对，是他”）不算（审阅第 2 条）。"""
        if fix is None:
            return []
        fixing = correct.message_of(self.conn, fix, proofs)
        if fixing is None:
            return []
        out = []
        for number, proof in enumerate(proofs):
            message = correct.bot_message(self.conn, [proof])
            if message is not None and (message[0], message[1], message[2], message[3] + 1) == fixing:
                out.append(number)
        return out

    @staticmethod
    def _closes(state: str, valid_to: str | None, proofs: Sequence[Mapping[str, object]]) -> bool:
        """这一处说的是这条关系结束了：标了已结束，或者结束日子最晚也在说第一句那天或以前（和 _relation_claim、merge 的一样）。"""
        if state == "ended":
            return True
        bounds = date_bounds(valid_to)
        said = parse_aware(str(proofs[0]["said_at"])) if proofs else None
        return bounds is not None and said is not None and bounds[1] <= said.date()

    def _echoes(self, window: Window, prepared: Sequence[Prepared]) -> dict[int, str]:
        """R05（J4.6）：这个窗口里，证据全在 Bot 的一条消息里、主人紧接着的下一条就是纠正同一组的那几处：下标 → 纠正那一句。"""
        fixes = [(self._echo_group(window, relation, subject, target), correct.owner_messages(self.conn, proofs, fix), fix)
                 for relation, _, subject, target, proofs, _, fix, _ in prepared if fix is not None]
        if not fixes:
            return {}
        out: dict[int, str] = {}
        for index, (relation, _, subject, target, proofs, _, fix, _) in enumerate(prepared):
            if fix is not None:
                continue
            message = correct.bot_message(self.conn, proofs)
            if message is None:
                continue
            following = (message[0], message[1], message[2], message[3] + 1)
            group = self._echo_group(window, relation, subject, target)
            found = next((sentence for other, messages, sentence in fixes if other == group and following in messages), None)
            if found is not None:
                out[index] = found
        return out

    def _undated(self, proofs: Sequence[Mapping[str, object]]) -> bool:
        """证据那几句里一个日子都没写（和 _said_day_only 一样照 sentence_times 认；原文找不到的当写了）。"""
        for proof in proofs:
            said = parse_aware(str(proof["said_at"]))
            row = self.conn.execute("SELECT text FROM raw_sentences WHERE sentence_id = ? AND revision = ?",
                                    (proof["sentence_id"], proof["revision"])).fetchone()
            if said is None or row is None or not sentence_times(str(row["text"]), said, said_day=False).empty:
                return False
        return True

    def _said_day_only(self, value: str | None, precision: str, proofs: Sequence[Mapping[str, object]]) -> bool:
        """纠正的那一处（J4.6，第 1 题 A）：这个日子就是说这句话那天，证据那几句又一个日子都没写——L3 照“当场交代”写的
        说话那天，可说“不对”的那天不是换的那天，去掉。"""
        if not value or precision not in ("day", "minute"):
            return False
        bounds = date_bounds(value)
        if bounds is None or bounds[0] != bounds[1]:
            return False
        days = set()
        for proof in proofs:
            said = parse_aware(str(proof["said_at"]))
            row = self.conn.execute("SELECT text FROM raw_sentences WHERE sentence_id = ? AND revision = ?",
                                    (proof["sentence_id"], proof["revision"])).fetchone()
            if said is None or row is None or not sentence_times(str(row["text"]), said, said_day=False).empty:
                return False
            days.add(said.date())
        return bounds[0] in days

    def _relation_claim(
        self, item: Checked, relation: RelationCheck, subject: str, target: str, now: str, *, judge_hold: str | None = None,
        prepared: tuple[list[dict[str, object]], str | None, str | None, list[dict[str, object]]] | None = None,
    ) -> str | None:
        """写一处关系的说法。它先挂在这条关系现有的哪一行上（没有就先占一行），分到哪一段在 _refresh_family 里算。
        judge_hold 是判断 C 拿不准的原因：主人对同一句、同一条关系定过“不要”的不写，返回 None（照旧进隔离表）。
        prepared 是 _relation_claims 先算好的 (证据, 软变化的候选句, 主人在纠正的那一句, R05 从证据里去掉的 Bot 那几句)
        （J4.6）；没给的这里算。"""
        raw = relation.raw
        window = item.window
        relation_type = str(raw.get("relation"))
        kinship = (str(raw.get("kinship") or "").strip() or None) if relation_type == KINSHIP else None
        identity = relation_identity(window.scope, subject, relation_type, target, kinship=kinship)
        proofs = prepared[0] if prepared is not None else self._proof_rows(relation.proofs)
        first = proofs[0]
        last = max(proofs, key=lambda proof: proof["said_epoch"])
        if judge_hold is not None and (self._held_decided(MANUAL_REVIEW, [(str(first["sentence_id"]), identity)]).get(
                (str(first["sentence_id"]), identity)) or {}).get("decision") == DROP:
            return None
        # 先挂在有说法、没冻结的那几行上；没有说法的冻结关系留给 _refresh_family 认（要不要接上它）。新说法不先挂在冻结的
        # 那一行上：那一行上的说法算它原来的，冻结着的不被别人说的结束结束掉（审阅第 3 条），新说法挂上去就分不出来了
        existing = self.conn.execute(
            "SELECT r.relation_id FROM relations AS r WHERE r.identity_key = ? AND r.frozen = 0"
            " AND EXISTS (SELECT 1 FROM relation_claims AS c WHERE c.relation_id = r.relation_id) ORDER BY r.relation_id LIMIT 1",
            (identity,),
        ).fetchone()
        state = L3_RELATION_STATES.get(str(raw.get("state")), "active")
        claim_id = _claim_id(item, relation)
        if existing is not None:
            relation_id = str(existing[0])
        else:
            key = relation_merge_key(identity, str(first["sentence_id"]))
            relation_id = relation_id_of(key)
            clash = self.conn.execute(
                "SELECT frozen, state, superseded_by, withdrawn_at FROM relations WHERE relation_id = ?", (relation_id,)
            ).fetchone()
            said = parse_aware(str(first["said_at"]))
            bounds = date_bounds(relation.valid_to)
            closes = state == "ended" or (bounds is not None and said is not None and bounds[1] <= said.date())
            guarded = clash is not None and clash["frozen"] and (clash["state"] != "ended" or clash["superseded_by"] is not None)
            if clash is not None and (clash["withdrawn_at"] is not None or (guarded and closes)):
                # 编号撞上了藏着的、冻结着没结束的那一行（包重发时同一个位置的句子编号不变）：说结束的、或者藏着的，不先挂上去，
                # 不然就当成它原来的说法了（第二轮审阅 S1）；挂在自己的一行上，走一遍时再看接不接上
                key = relation_merge_key(identity, f"{first['sentence_id']}\n{claim_id}")
                relation_id = relation_id_of(key)
            self.conn.execute(
                "INSERT OR IGNORE INTO relations (relation_id, subject_entity_id, relation_type, object_entity_id, kinship,"
                " state, status, scope_partitions, scope_sensitive, merge_key, identity_key, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'active', 'inferred', ?, ?, ?, ?, ?, ?)",
                (
                    relation_id, subject, relation_type, target, kinship,
                    _json(list(window.scope.partitions)), _json(list(window.scope.sensitive)), key, identity, now, now,
                ),
            )
        paused_at, paused_precision = (pause_day(relation.proofs) if state == "paused" else (None, "unknown"))
        certainty = str(raw.get("certainty") or "stated")
        if prepared is not None:
            soft, correction = prepared[1], prepared[2]
        else:
            soft = self._soft_sentence(proofs, state == "paused" or certainty == "vague")
            correction = None if soft else correct.correction_sentence(self.conn, proofs)
        valid_from, valid_from_precision = relation.valid_from, relation.valid_from_precision
        valid_to, valid_to_precision = relation.valid_to, relation.valid_to_precision
        notes = list(relation.notes)
        if correction is not None:  # 纠正那一处：L3 拿说话那天顶上的开始、结束日子去掉（第 1 题 A：原话没说哪天就空着）
            self.stats.corrections += 1
            if self._said_day_only(valid_from, valid_from_precision, proofs):
                valid_from, valid_from_precision = None, "unknown"
                notes.append("L5:correction_start_dropped")
            if self._said_day_only(valid_to, valid_to_precision, proofs):
                valid_to, valid_to_precision = None, "unknown"
                notes.append("L5:correction_end_dropped")
            if prepared is not None and prepared[3] and self._undated(proofs) and not self._undated(prepared[3]):
                # R05 的另一半：Bot 那句不算证据，它写的日子也不算——Bot 那句写了日子、剩下的证据（主人的话）里一个都没写的，
                # L3 给的开始、结束日子去掉（审阅第 5 条；Bot 那句没写日子的不去，日子多半是主人在别的一条里说的：复审）。
                # 去掉了 Bot 那几句的都是说它结束了的：结束日子去掉以后照旧算结束（L3 标的生效、照日子算结束的，改成已结束、
                # 结束日子空着；不然成了“纠正成它还在”，复审）
                if valid_from is not None:
                    valid_from, valid_from_precision = None, "unknown"
                    notes.append("L5:on_spot_start_dropped")
                if valid_to is not None:
                    valid_to, valid_to_precision = None, "unknown"
                    notes.append("L5:on_spot_end_dropped")
                    state = "ended"
        self.conn.execute(
            "INSERT INTO relation_claims (claim_id, relation_id, window_id, cache_key, extracted_by, l4_version,"
            " subject_entity_id, relation_type, object_entity_id, kinship, state, valid_from, valid_to,"
            " valid_from_precision, valid_to_precision, certainty, status, speaker, speaker_role, attributed_to,"
            " said_from, said_from_epoch, said_to, said_to_epoch, proofs, notes, created_at, identity_key, paused_at,"
            " paused_at_precision, soft_sentence, judge_hold, correction, judged_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim_id,
                relation_id,
                window.window_id,
                item.cache_key,
                self.extracted_by,
                self.checked_by,
                subject,
                relation_type,
                target,
                kinship,
                state,
                valid_from,
                valid_to,
                valid_from_precision,
                valid_to_precision,
                certainty,
                relation.status,
                first["speaker"],
                first["speaker_role"],
                relation.attributed_to,
                first["said_at"],
                first["said_epoch"],
                last["said_at"],
                last["said_epoch"],
                _json(proofs),
                _json(notes),
                now,
                identity,
                paused_at,
                paused_precision,
                soft,
                judge_hold,
                correction,
                # 谁认的（J5.3，开了进图前检查才记）：判断 C 照哪一档留下的；拿不准、转人工的是空的
                precheck.judged_tier(relation.judge) if self.precheck_on and judge_hold is None else None,
            ),
        )
        self.stats.relation_claims += 1
        return identity

    def _soft_sentence(self, proofs: Sequence[Mapping[str, object]], soft: bool) -> str | None:
        """软变化的候选句（和 javmem.soft.find 同一套）：这一处说法的证据里原文里新写的话（不是引用、没撤回），有犹豫词的
        那一句（javmem.soft.words：前三组的先挑，再挑后四组的）；L3 标了暂停、含糊的（soft）一句有犹豫词的都没有，取第一句
        新写的话。照这一句出的题执行，所以要挑说得不干脆的那一句（第三轮审阅 N3）。没有的是 None。"""
        fresh: list[tuple[str, tuple[str, ...]]] = []
        for proof in proofs:
            row = self.conn.execute(
                "SELECT text, quoted FROM raw_sentences WHERE sentence_id = ? AND revision = ? AND section_kind = 'raw'"
                " AND field IS NULL AND withdrawn_at IS NULL",
                (proof["sentence_id"], proof["revision"]),
            ).fetchone()
            if row is not None and not row["quoted"]:
                fresh.append((str(proof["sentence_id"]), soft_words.hedges(str(row["text"]))))
        for wanted in (lambda groups: soft_words.is_strong(groups), bool):
            chosen = next((sentence for sentence, groups in fresh if wanted(groups)), None)
            if chosen is not None:
                return chosen
        return fresh[0][0] if soft and fresh else None

    @staticmethod
    def _relation_view(row: sqlite3.Row) -> RelationClaimView:
        proofs = json.loads(row["proofs"])
        first = min(proofs, key=_proof_order) if proofs else None
        said = parse_aware(str(first["said_at"])) if first is not None else parse_aware(str(row["said_from"]))
        return RelationClaimView(
            claim_id=row["claim_id"],
            state=row["state"],
            valid_from=row["valid_from"],
            valid_from_precision=row["valid_from_precision"],
            valid_from_basis=row["valid_from_basis"],
            valid_to=row["valid_to"],
            valid_to_precision=row["valid_to_precision"],
            valid_to_basis=row["valid_to_basis"],
            status=row["status"],
            speaker=row["speaker"],
            speaker_role=row["speaker_role"],
            attributed_to=row["attributed_to"],
            said_from=row["said_from"],
            said_from_epoch=row["said_from_epoch"],
            said_to=row["said_to"],
            said_to_epoch=row["said_to_epoch"],
            anchor=str(first["sentence_id"]) if first is not None else str(row["claim_id"]),
            said_day=said.date() if said is not None else datetime.fromtimestamp(row["said_from_epoch"], timezone.utc).date(),
            notes=tuple(json.loads(row["notes"])),
            extracted_by=row["extracted_by"],
            l4_version=row["l4_version"],
            paused_at=row["paused_at"],
            paused_at_precision=row["paused_at_precision"],
            pause_condition=row["pause_condition"],
            sentences=tuple(dict.fromkeys(str(proof["sentence_id"]) for proof in proofs)),
            correction=row["correction"],
        )

    @staticmethod
    def _shell_view(row: sqlite3.Row) -> RelationClaimView:
        """没有说法的冻结关系当一处“说法”放进组里走一遍：说的时间是它最后一次被说的时候，各列照它自己的。"""
        said = parse_aware(str(row["as_of"])) if row["as_of"] else None
        moment = int(row["as_of_epoch"] or 0)
        return RelationClaimView(
            claim_id=SHELL_PREFIX + str(row["relation_id"]),
            state=row["state"],
            valid_from=row["valid_from"],
            valid_from_precision=row["valid_from_precision"],
            valid_from_basis=row["valid_from_basis"],
            valid_to=row["valid_to"],
            valid_to_precision=row["valid_to_precision"],
            valid_to_basis=row["valid_to_basis"],
            status=row["status"] if row["status"] in ("verified", "inferred") else "inferred",
            speaker=row["speaker"] or "",
            speaker_role=row["speaker_role"] or "other",
            attributed_to=row["attributed_to"],
            said_from=str(row["as_of"] or ""),
            said_from_epoch=moment,
            said_to=str(row["as_of"] or ""),
            said_to_epoch=moment,
            anchor=str(row["relation_id"]),
            said_day=said.date() if said is not None else datetime.fromtimestamp(moment, timezone.utc).date(),
            paused_at=row["paused_at"],
            paused_at_precision=row["paused_at_precision"],
            pause_condition=row["pause_condition"],
        )

    def _refresh_relations(self, identities: Iterable[str | None], ambiguous: set[str], now: str) -> None:
        """按说法重算这几条关系所在的组（同一时间一个的整组一起走一遍，见 merge 的“关系”一节）。任职于变了，同一个人负责的
        那几样要重新看代表哪一家（D153 第 2 条），跟着重算（只往下带一层：负责不影响任职于）。隶属于变了也一样（D210、D215）：
        在那个部门、和它下面的部门任职的人，代表哪一家要顺着隶属于重新往上找，他们负责的那几样跟着重算。"""
        families: dict[str, set[str]] = {}
        workers: set[tuple[str, str, str]] = set()
        units: set[tuple[str, str, str]] = set()
        for identity in sorted({one for one in identities if one}):
            row = self.conn.execute(
                "SELECT relation_type, subject_entity_id, object_entity_id, scope_partitions, scope_sensitive FROM relations"
                " WHERE identity_key = ? LIMIT 1",
                (identity,),
            ).fetchone()
            if row is None:
                continue
            key, members = self._family(row, identity)
            families.setdefault(key, set()).update(members)
            if row["relation_type"] == KINSHIP and members == {identity}:
                # 同一对人的别的几种亲属各自重算（J4.6）：纠正撤回了、不再放在一起走的时候，被它顶掉的那种要回来
                for other in sorted(self._kin_pair(row) - {identity}):
                    families.setdefault(other, set()).add(other)
            if row["relation_type"] == "任职于":
                workers.add((row["scope_partitions"], row["scope_sensitive"], row["subject_entity_id"]))
            elif row["relation_type"] == PART_OF:
                units.add((row["scope_partitions"], row["scope_sensitive"], row["subject_entity_id"]))
        for partitions, sensitive, unit in sorted(units):
            workers |= {(partitions, sensitive, person) for person in self._unit_workers(unit, partitions, sensitive)}
        # 负责要看任职于（代表哪一家）：任职于的组先算，负责的后算（第二轮审阅 G4：以前按组名排，负责有时先算、读到旧的）
        for key in sorted(families, key=lambda one: (one.startswith("family\n负责"), one)):
            self._refresh_family(families[key], ambiguous, now)
        extra: dict[str, set[str]] = {}
        for partitions, sensitive, subject in sorted(workers):
            for row in self.conn.execute(
                "SELECT DISTINCT identity_key, relation_type, subject_entity_id, object_entity_id, scope_partitions, scope_sensitive"
                " FROM relations WHERE subject_entity_id = ? AND relation_type = '负责' AND scope_partitions = ?"
                " AND scope_sensitive = ? AND identity_key IS NOT NULL",
                (subject, partitions, sensitive),
            ).fetchall():
                key, members = self._family(row, row["identity_key"])
                if key not in families:
                    extra.setdefault(key, set()).update(members)
        for key in sorted(extra):
            self._refresh_family(extra[key], ambiguous, now)

    def _family(self, row: sqlite3.Row, identity: str) -> tuple[str, set[str]]:
        """这条关系和谁一起走一遍：同一时间一个的（托管于、负责），同一个可见范围里卡上那一头一样的都算（负责再按代表哪一家
        分开，在 _refresh_family 里分）；别的关系自己一个。返回 (组的名字, 里面的几条关系)。"""
        side = ONE_AT_A_TIME.get(row["relation_type"])
        if row["relation_type"] == KINSHIP:
            # 亲属同一时间可以几种（不同的人）；同一对人有纠正的（J4.6：“不是妹妹，是表妹”），这一对的几种放在一起走，
            # 只有纠正顶掉别的那种（relation_timeline 的 cut_only）
            pair = self._kin_pair(row) | {identity}
            if len(pair) > 1 and self.conn.execute(
                f"SELECT 1 FROM relation_claims WHERE identity_key IN ({_marks(sorted(pair))}) AND correction IS NOT NULL LIMIT 1",
                sorted(pair),
            ).fetchone():
                key = "\n".join(("family", KINSHIP, str(row["subject_entity_id"]), str(row["object_entity_id"]),
                                 row["scope_partitions"], row["scope_sensitive"]))
                return key, pair
            return identity, {identity}
        if side is None:
            return identity, {identity}
        column = "subject_entity_id" if side == "subject" else "object_entity_id"
        end = row[column]
        found = {
            str(one[0])
            for one in self.conn.execute(
                f"SELECT DISTINCT identity_key FROM relations WHERE {column} = ? AND relation_type = ? AND scope_partitions = ?"
                " AND scope_sensitive = ? AND identity_key IS NOT NULL",
                (end, row["relation_type"], row["scope_partitions"], row["scope_sensitive"]),
            )
        }
        key = "\n".join(("family", row["relation_type"], str(end), row["scope_partitions"], row["scope_sensitive"]))
        return key, found | {identity}

    def _kin_pair(self, row: sqlite3.Row) -> set[str]:
        """同一个可见范围里、同一对人（主体、客体一样）的几种亲属。"""
        return {
            str(one[0])
            for one in self.conn.execute(
                "SELECT DISTINCT identity_key FROM relations WHERE relation_type = ? AND subject_entity_id = ?"
                " AND object_entity_id = ? AND scope_partitions = ? AND scope_sensitive = ? AND identity_key IS NOT NULL",
                (KINSHIP, row["subject_entity_id"], row["object_entity_id"], row["scope_partitions"], row["scope_sensitive"]),
            )
        }

    def _employer(self, subject: str, partitions: str, sensitive: str) -> str | None:
        """负责的“代表哪一家”还没有（D118）：先看这个人在账本里任职于哪家（同一个可见范围、没结束的），每一家顺着隶属于往上
        找到最上面那家（D210、D215：同一家公司的部门、分公司算一家；_top_unit），去掉重复的只剩一家，就是它；看不出的
        （主人这边的人、Bot、几家都有）当成主人这边（D153 第 2 条）。主人自己记了任职于哪家，也算主人这边。"""
        if subject == entity_id_of("人", OWNER_NAME):
            return None
        rows = {
            self._top_unit(str(one[0]), partitions, sensitive)
            for one in self.conn.execute(
                "SELECT DISTINCT object_entity_id FROM relations WHERE subject_entity_id = ? AND relation_type = '任职于'"
                " AND scope_partitions = ? AND scope_sensitive = ? AND state != 'ended' AND withdrawn_at IS NULL",
                (subject, partitions, sensitive),
            )
        }
        return next(iter(rows)) if len(rows) == 1 else None

    def _parents(self, unit: str, partitions: str, sensitive: str) -> set[str]:
        """这个部门、分公司现在归哪家：同一个可见范围里现行的隶属于（没结束、没撤回、没被替代、不是待确认的）的客体。"""
        return {
            str(one[0])
            for one in self.conn.execute(
                "SELECT DISTINCT object_entity_id FROM relations WHERE subject_entity_id = ? AND relation_type = ?"
                " AND scope_partitions = ? AND scope_sensitive = ? AND state != 'ended' AND withdrawn_at IS NULL"
                " AND superseded_by IS NULL AND status <> 'pending'",
                (unit, PART_OF, partitions, sensitive),
            )
        }

    def _top_unit(self, unit: str, partitions: str, sensitive: str) -> str:
        """顺着现行的隶属于一层层往上，找到最上面那家（D215）：最多走 PART_OF_DEPTH 层；没有上级、同时写着归两家（看不出是
        哪一家）、绕回走过的，就停在这一层。没有隶属于的就是它自己（和以前一样）。"""
        seen = [unit]
        for _ in range(PART_OF_DEPTH):
            parents = self._parents(seen[-1], partitions, sensitive)
            if len(parents) != 1:
                break
            parent = parents.pop()
            if parent in seen:
                break
            seen.append(parent)
        return seen[-1]

    def _unit_workers(self, unit: str, partitions: str, sensitive: str) -> set[str]:
        """在这个部门、和它下面（隶属于它的，往下最多 PART_OF_DEPTH 层）的部门任职的人：隶属于变了，他们代表哪一家可能跟着变。
        往下找时没结束的、结束了的隶属于都算（多重算几组不碍事，漏了才不对）。"""
        below, frontier = {unit}, {unit}
        for _ in range(PART_OF_DEPTH):
            if not frontier:
                break
            found = {
                str(one[0])
                for one in self.conn.execute(
                    f"SELECT DISTINCT subject_entity_id FROM relations WHERE relation_type = ? AND object_entity_id IN"
                    f" ({_marks(sorted(frontier))}) AND scope_partitions = ? AND scope_sensitive = ? AND withdrawn_at IS NULL",
                    (PART_OF, *sorted(frontier), partitions, sensitive),
                )
            }
            frontier = found - below
            below |= found
        return {
            str(one[0])
            for one in self.conn.execute(
                f"SELECT DISTINCT subject_entity_id FROM relations WHERE relation_type = '任职于' AND object_entity_id IN"
                f" ({_marks(sorted(below))}) AND scope_partitions = ? AND scope_sensitive = ? AND withdrawn_at IS NULL",
                (*sorted(below), partitions, sensitive),
            )
        }

    def _refresh_family(self, identities: set[str], ambiguous: set[str], now: str) -> None:
        """一组关系按说法重算（见 merge 的“关系”一节）：走一遍分段、谁顶掉谁，每段一行 relations、证据、来源和信任；
        冻结的不被顶掉，和它冲突的、同一时刻两个的进待确认。说法都没了的删掉（冻结的留着，和事实一样，等主人处理）。
        冻结标记跟着说法走：一段的编号变了，标记、替代、待确认改指到新的那一行。"""
        marks = _marks(sorted(identities))
        existing = {
            row["relation_id"]: row
            for row in self.conn.execute(
                f"SELECT * FROM relations WHERE identity_key IN ({marks}) ORDER BY relation_id", sorted(identities)
            )
        }
        if not existing:
            return
        rows = self.conn.execute(
            f"SELECT * FROM relation_claims WHERE identity_key IN ({marks}) ORDER BY claim_id", sorted(identities)
        ).fetchall()
        templates: dict[str, sqlite3.Row] = {}
        for row in existing.values():
            templates.setdefault(row["identity_key"], row)
        before = {row["claim_id"]: row["relation_id"] for row in rows}
        by_claim = {row["claim_id"]: row for row in rows}
        frozen_claims = {claim_id for claim_id, relation_id in before.items() if existing[relation_id]["frozen"]}
        # 冻结着、没结束的那几行上原来的说法：一直现行，不被别人说的结束结束掉（见 merge 的“关系”一节）
        guarded_claims = {claim_id for claim_id in frozen_claims if existing[before[claim_id]]["state"] != "ended"
                          or existing[before[claim_id]]["superseded_by"] is not None}
        with_claims = set(before.values())
        views: dict[str, list[RelationClaimView]] = {}
        for row in rows:
            views.setdefault(row["identity_key"], []).append(self._relation_view(row))
        hidden_shells: dict[str, list[sqlite3.Row]] = {}
        for row in existing.values():  # 没有说法的冻结关系：看得到的放进去走（冲突、接上）；藏着的等下面看接不接上
            if not row["frozen"] or row["relation_id"] in with_claims:
                continue
            if row["withdrawn_at"] is not None:
                hidden_shells.setdefault(row["identity_key"], []).append(row)
                continue
            shell = self._shell_view(row)
            views.setdefault(row["identity_key"], []).append(shell)
            frozen_claims.add(shell.claim_id)
            if row["state"] != "ended" or row["superseded_by"] is not None:
                guarded_claims.add(shell.claim_id)
        # 软变化（J4.4 第 4 段）：候选那几处，判过要改的照结论放出来（改成判的样子）；没判的、判了不改的（A、E、30 天没人管）、
        # 还要主人看的先挡着（走一遍时会改到组里以前说的才挡）
        # 主人冻结的那几行上的说法不挡（主人担保了，和冻结的事实一样）
        soft_all = {str(row["claim_id"]): str(row["soft_sentence"]) for row in rows if row["soft_sentence"]}
        soft = soft_all  # 判过的照结论（冻结的那几行上的也照判的样子：重建导回以后和原来一样）
        decided = self._soft_decided("relation", [(sentence, by_claim[claim_id]["identity_key"]) for claim_id, sentence in soft.items()])
        candidates = set(soft) - frozen_claims  # 主人冻结的那几行上的不挡
        forced: set[str] = set()  # 判了结束、放出来的：照判的结束现在这一段（merge.relation_timeline 的 forced）
        for claim_id, sentence in sorted(soft.items()):
            identity = str(by_claim[claim_id]["identity_key"])
            record = decided.get((sentence, identity))
            action = record.get("action") if record else None
            if not isinstance(action, Mapping) or action.get("kind") not in soft_rules.CHANGES:
                continue
            old_identity = str(record.get("old_identity") or identity)  # type: ignore[union-attr]
            changed = _soft_view(next(one for one in views[identity] if one.claim_id == claim_id), action)
            if action.get("kind") == soft_rules.REPLACE or old_identity == identity:
                views[identity] = [changed if one.claim_id == claim_id else one for one in views[identity]]
                candidates.discard(claim_id)
                if action.get("kind") == soft_rules.END:
                    forced.add(claim_id)
            elif old_identity in views:  # 暂停、结束的是同一组里别的那一条：给它加一处判出来的（只带状态和那天），这一处照旧挡着
                views[old_identity].append(replace(changed, claim_id=DIRECTIVE_PREFIX + claim_id, valid_from=None,
                                                   valid_from_precision="unknown", valid_from_basis=None, sentences=()))
        # 判断 C 拿不准的：主人说留了的照常，没说的、说接着搁着的挡着（manual_review）
        judged = {str(row["claim_id"]): str(row["judge_hold"]) for row in rows if row["judge_hold"]}
        judged_pairs = {claim_id: (self._relation_view(by_claim[claim_id]).anchor, str(by_claim[claim_id]["identity_key"]))
                        for claim_id in judged}
        kept = self._held_decided(MANUAL_REVIEW, judged_pairs.values())
        holds = {claim_id for claim_id, pair in judged_pairs.items() if (kept.get(pair) or {}).get("decision") != KEEP_NEW}
        kind = next(iter(templates.values()))["relation_type"]
        partitions: dict[str | None, set[str]] = {}
        group_of: dict[str, str] = {}  # 每条关系判新旧的组（纠正挡住的待确认按组认：一句话结束旧的、开始新的只问一次）
        for identity in sorted(views):
            template = templates[identity]
            org = template["org_entity_id"]
            if kind == "负责" and org is None:
                org = self._employer(template["subject_entity_id"], template["scope_partitions"], template["scope_sensitive"])
            partitions.setdefault(org, set()).add(identity)
            group_of[identity] = relation_group_key(
                _scope_of_row(template["scope_partitions"], template["scope_sensitive"]), kind, template["subject_entity_id"],
                template["object_entity_id"], org_id=org, identity=identity)
        # 主人在对话里答待确认的那几句自己抽出来的、和那一条同一条关系（同一组）的说法（D83）：照主人定的算，不再因为软变化、
        # 财务投资挡一遍
        confirmed = self._confirmed_claims(views, group_of)
        candidates -= confirmed
        # 纠正（J4.6）：主人在纠正的那几处（软变化的候选写说法时就分开了）。财务投资的，主人没说照改的先挡住（第 2 题 A）；
        # 同一对人的几种亲属放在一起走、只有纠正顶掉别的那种；主人认了“抽取没抽出来的纠正”的，照它结束那一条
        fixes = {str(row["claim_id"]): str(row["correction"]) for row in rows if row["correction"]}
        # 挡住的那一处按哪一句认（一句话、一组只问一次）：纠正词和它自己的证据在同一条消息里的（同一句、前一句）是纠正词
        # 那一句；纠正词在紧挨着的上一条“不对。”这种短句里的，按它自己最早那一句证据认，不然同一个包里两处这样的纠正指纹
        # 一样、认成一条（第一轮审阅第 13 条、第二轮第 3 条）
        view_of = {claim.claim_id: claim for claims in views.values() for claim in claims}
        hold_at = {claim_id: sentence if _message_of(sentence) in {_message_of(one) for one in view_of[claim_id].sentences}
                   else view_of[claim_id].anchor for claim_id, sentence in fixes.items()}
        # 冻结的优先：纠正那一处要改的是冻结着的那一条，照 J4.4 进“和冻结的冲突”，不再另挡一次（relation_timeline 里认）
        finance = self._finance_holds(hold_at, by_claim, group_of, templates) - frozen_claims - holds - confirmed
        kinship_mode = kind == KINSHIP and len(views) > 1 and bool(fixes)
        used = {sentence for claim in view_of.values() for sentence in claim.sentences} | set(fixes.values())
        for identity, directive in self._unmatched_directives(set(views), used):
            views[identity].append(directive)
        placed: list[tuple[str | None, TimelineSegment, list[TimelineSegment]]] = []
        for org in sorted(partitions, key=lambda one: one or ""):
            timeline = relation_timeline(
                {identity: views[identity] for identity in sorted(partitions[org])},
                one_at_a_time=kind in ONE_AT_A_TIME or kinship_mode,
                frozen_claims=frozen_claims,
                guarded_claims=guarded_claims,
                candidates=candidates - holds,
                holds=holds,
                forced=forced,
                finance=finance,
                cut_only=set(fixes) if kinship_mode else None,
            )
            placed += [(org, segment, timeline) for segment in timeline]
        # 每一段的编号：放进去的冻结关系在哪一段，就用它的编号；别的按“同一条关系 + 这一段最早那一句”
        ids: dict[int, str] = {}
        keys: dict[int, str] = {}
        owned: set[int] = set()  # 编号是冻结关系自己的那几段（放进去走的、接上藏着的）
        summaries: dict[int, RelationSummary] = {}
        for number, (_, segment, _) in enumerate(placed):
            shells = [claim.claim_id[len(SHELL_PREFIX):] for claim in segment.members if claim.shell]
            summaries[number] = summarize_segment(segment.members)
            keys[number] = relation_merge_key(segment.identity, summaries[number].anchor)
            if shells:
                ids[number] = max(shells, key=lambda one: (existing[one]["as_of_epoch"] or 0, one))
                owned.add(number)
            else:
                ids[number] = relation_id_of(keys[number])
        # 藏着的冻结关系（证据都撤回了）后来又有人说起、中间没写结束、也没被顶掉：这一条关系最后那一段就是它，放回来。
        # 它最后被说起以后有人说过结束的：后来又说起的是结束以后的另一段，不是它，接着藏着等主人定（审阅第 3 条）
        for identity, shells in sorted(hidden_shells.items()):
            last = max((number for number, (_, segment, _) in enumerate(placed) if segment.identity == identity),
                       key=lambda number: min((claim.said_from_epoch, claim.anchor) for claim in placed[number][1].members),
                       default=None)
            if last is None:
                continue
            segment = placed[last][1]
            if segment.closed or any(claim.shell for claim in segment.members):
                continue
            living = [row for row in shells if row["state"] != "ended"]
            if not living:
                continue
            chosen = max(living, key=lambda row: (row["as_of_epoch"] or 0, row["relation_id"]))
            since = int(chosen["as_of_epoch"] or 0)
            if any(claim.closes and claim.said_from_epoch > since for claim in views.get(identity, ()) if not claim.shell):
                continue
            ids[last] = chosen["relation_id"]
            owned.add(last)
        # 编号不撞（审阅第 1 条）：同一条关系的两段从同一句开始的（L3 在一个窗口里把它给了两次，一处写了结束、一处没写），
        # 或者撞上了冻结关系自己的编号：冻结关系的那段、早说的那段留着这个编号，后面的键里再加它最早那处说法的编号
        taken: set[str] = set()
        # 冻结的、藏着的那几行，只给原来就挂在它上面的说法（或者放进去走的它自己）那一段用：别的段算出来的编号碰巧一样的
        # （包重发时同一个位置的句子编号不变），另算，不然那一行就被别的说法盖掉、藏着的被放出来（第二轮审阅 S1）
        protected = {one for one, row in existing.items() if row["frozen"] or row["withdrawn_at"] is not None}
        for number in sorted(ids, key=lambda one: (one not in owned, min(_claim_order(claim) for claim in placed[one][1].members))):
            segment = placed[number][1]
            foreign = (ids[number] in protected and number not in owned
                       and not any(before.get(claim.claim_id) == ids[number] for claim in segment.members if not claim.pseudo))
            if ids[number] in taken or foreign:
                first = min((claim for claim in placed[number][1].members if not claim.pseudo), key=_claim_order)
                keys[number] = relation_merge_key(placed[number][1].identity, f"{summaries[number].anchor}\n{first.claim_id}")
                ids[number] = relation_id_of(keys[number])
            if ids[number] in taken:
                raise RuntimeError(f"关系分段的编号撞了：{ids[number]}")
            taken.add(ids[number])
        index_of = {id(segment): number for number, (_, segment, _) in enumerate(placed)}
        after: dict[str, str] = {}
        for number, (org, segment, _) in enumerate(placed):
            template = templates[segment.identity]
            relation_id = ids[number]
            self.conn.execute(
                "INSERT OR IGNORE INTO relations (relation_id, subject_entity_id, relation_type, object_entity_id, kinship,"
                " org_entity_id, state, status, scope_partitions, scope_sensitive, merge_key, identity_key, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', 'inferred', ?, ?, ?, ?, ?, ?)",
                (
                    relation_id, template["subject_entity_id"], template["relation_type"], template["object_entity_id"],
                    template["kinship"], template["org_entity_id"], template["scope_partitions"], template["scope_sensitive"],
                    keys[number], segment.identity, now, now,
                ),
            )
            for claim in segment.members:
                if not claim.pseudo:
                    after[claim.claim_id] = relation_id
        for claim_id, relation_id in sorted(after.items()):
            if before[claim_id] != relation_id:
                self.conn.execute("UPDATE relation_claims SET relation_id = ? WHERE claim_id = ?", (relation_id, claim_id))
        new_ids = set(ids.values())
        renamed: dict[str, str] = {}  # 旧的一行的说法多半去了哪一行，冻结标记、替代、待确认就给哪一行
        for old_id in existing:
            if old_id in new_ids:
                continue
            moved = sorted(after[claim_id] for claim_id, relation_id in before.items() if relation_id == old_id)
            if moved:
                renamed[old_id] = max(set(moved), key=lambda one: (moved.count(one), one))
        hidden_before = {old_id for old_id, row in existing.items() if row["withdrawn_at"] is not None}
        for old_id, new_id in sorted(renamed.items()):
            if existing[old_id]["frozen"]:
                self.conn.execute("UPDATE relations SET frozen = 1 WHERE relation_id = ?", (new_id,))
            self._rename_relation(old_id, new_id)
            if old_id in hidden_before:
                hidden_before.add(new_id)
        known: dict[tuple[str, str], bool] = {}
        conflicts: list[tuple[str, str, str]] = []  # (种类, 一边, 另一边)
        held: list[tuple[str, RelationClaimView]] = []  # 挡住的那几处：(哪一行, 哪一处)，最后进待确认
        for number, (org, segment, timeline) in enumerate(placed):
            relation_id = ids[number]
            if all(claim.pseudo for claim in segment.members):
                continue  # 只有冻结关系自己（没有说法）：各列照它自己的，不动
            template = templates[segment.identity]
            summary = summaries[number]
            scope = _scope_of_row(template["scope_partitions"], template["scope_sensitive"])
            state, valid_to = summary.state, (summary.valid_to, summary.valid_to_precision, summary.valid_to_basis)
            superseded_by = superseded_at = None
            if segment.cut is not None:  # 被顶掉了（或者只补历史）：在那天结束；主人纠正掉、哪天不知道的空着（J4.6）
                state, valid_to = "ended", segment.cut
                if segment.superseded_by is not None:
                    superseded_by = ids[index_of[id(timeline[segment.superseded_by])]]
                    superseded_at = segment.superseded_at
            status, reason = summary.status, None
            if segment.pending is not None and segment.conflict_with is not None:
                other = ids[index_of[id(timeline[segment.conflict_with])]]
                if segment.pending == "frozen_conflict":
                    decided = self._frozen_decision(other, relation_id)
                    if decided is None:
                        status, reason = "pending", "frozen_conflict"
                        conflicts.append(("frozen_conflict", other, relation_id))
                    elif decided[0] == KEEP_FROZEN and timeline[segment.conflict_with].identity != segment.identity:
                        state, superseded_by, superseded_at = "ended", other, decided[1]  # 主人留原值：新的算被冻结的那条替代了
                    # 同一条关系、说冻结的那条结束了的：主人留原值，这一段照它自己的（已结束），不算谁替代谁
                elif not self._same_time_decided(relation_id, other):
                    status, reason = "pending", "other"  # R12：同一句话里两个（A 或 B），都进待确认
                    conflicts.append(("same_time", relation_id, other))  # 哪一边都入队，同一对只进一次
            ends = (template["subject_entity_id"], template["object_entity_id"])
            if any(end in ambiguous or self._open_ambiguity(end, scope, known) for end in ends):
                status, reason = "pending", "ambiguous_entity"
            if segment.held:  # 软变化先挡住的（判了再照结论）、判断 C 拿不准的（等主人）：待确认
                status, reason = "pending", str(segment.pending)
                held.append((relation_id, segment.members[0]))
            paused = state == "paused"
            # 亲属的具体说法按比对写法认（大小写不同的是同一条）：存的写法取这一段最早说的那一处，和写的先后无关（审阅第 6 条）
            first_real = min((claim for claim in segment.members if not claim.pseudo), key=_claim_order)
            kinship = by_claim[first_real.claim_id]["kinship"] if template["relation_type"] == KINSHIP else template["kinship"]
            fixed = segment.corrected_by if state == "ended" else None  # 被主人纠正掉的（J4.6）：说那一句的时间、那一句
            # 谁认的（J5.3 进图的口子看它）：照这一段的说法，判断 C 拿不准、主人还没说留下的不算，说留下的算主人认的；冻结的是主人
            tiers: list[str | None] = []
            for claim in segment.members:
                member = by_claim.get(claim.claim_id) if not claim.pseudo else None
                if member is None:
                    continue
                if member["judge_hold"]:
                    if claim.claim_id not in holds:
                        tiers.append(precheck.BY_OWNER)
                    continue
                tiers.append(member["judged_by"])
            self.conn.execute(
                "UPDATE relations SET kinship = ?, valid_from = ?, valid_from_precision = ?, valid_from_basis = ?, valid_to = ?,"
                " valid_to_precision = ?, valid_to_basis = ?, state = ?, pause_condition = ?, paused_at = ?, paused_at_precision = ?,"
                " superseded_by = ?, superseded_at = ?, status = ?, pending_reason = ?, speaker = ?, speaker_role = ?,"
                " attributed_to = ?, as_of = ?, as_of_epoch = ?, notes = ?, extracted_by = ?, rule_version = ?, group_key = ?,"
                " corrected_at = ?, correction_sentence = ?, withdrawn_at = NULL, updated_at = ?,"
                " judged_by = CASE WHEN frozen = 1 THEN 'owner' ELSE ? END WHERE relation_id = ?",
                (
                    kinship, summary.valid_from, summary.valid_from_precision, summary.valid_from_basis,
                    valid_to[0], valid_to[1] if valid_to[0] else "unknown", valid_to[2] if valid_to[0] else None,
                    state, summary.pause_condition if paused else None, summary.paused_at if paused else None,
                    summary.paused_at_precision if paused and summary.paused_at else "unknown",
                    superseded_by, superseded_at, status, reason, summary.speaker, summary.speaker_role,
                    summary.attributed_to, summary.as_of, summary.as_of_epoch, _json(list(summary.notes)), summary.extracted_by,
                    f"{summary.l4_version} {L5_VERSION}",
                    relation_group_key(scope, template["relation_type"], template["subject_entity_id"],
                                       template["object_entity_id"], org_id=org, identity=segment.identity),
                    fixed.said_from if fixed is not None else None, fixed.correction if fixed is not None else None,
                    now, precheck.strongest(tiers), relation_id,
                ),
            )
            self._relation_evidence(relation_id, [by_claim[claim.claim_id] for claim in segment.members if not claim.pseudo], now)
            self._relation_provenance(relation_id, stated=summary.status == "verified")
            if relation_id in hidden_before:
                # 证据都撤回了、藏起来的冻结关系又有了说法：放回来（上面清了 withdrawn_at），那条待确认关掉（和事实一样）
                self.conn.execute(
                    "UPDATE review_queue SET decision = ?, decided_at = ?, decided_by = 'auto' WHERE decided_at IS NULL"
                    " AND kind = 'other' AND json_extract(claims, '$.relation') = ?",
                    (RESUPPORTED, now, relation_id),
                )
        for old_id, row in existing.items():
            if old_id in new_ids or old_id in renamed:
                if old_id in renamed:
                    self._forget_relation(old_id)
                continue
            if row["frozen"]:
                continue  # 冻结的是主人定的，说法没了也不自动删（和事实一样）：证据还指着原来那几句
            self._forget_relation(old_id)
        self._conflict_reviews(conflicts, now)
        if fixes:
            # 主人纠正成了 R12（同一句话里两个）那一对里的一个：这一对重算以后不再是“A 或 B”，没处理的那条删掉（第二轮
            # 审阅第 5 条）。只在组里有纠正时看，别的照 J4.4
            live = {_json(sorted((one, other))) for what, one, other in conflicts if what == "same_time"}
            for row in self.conn.execute(
                "SELECT review_id, claims FROM review_queue WHERE kind = 'other' AND decided_at IS NULL"
                " AND json_extract(claims, '$.reason') = ?", (SAME_TIME,),
            ).fetchall():
                pair = json.loads(row["claims"]).get("relations") or []
                if len(pair) == 2 and set(pair) <= new_ids and _json(sorted(pair)) not in live:
                    self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (row["review_id"],))
        for relation_id, claim in held:
            key = str(by_claim[claim.claim_id]["identity_key"])
            if claim.claim_id in holds:
                self._judge_review(relation_id, claim.claim_id, claim.anchor, key, f"C:{judged[claim.claim_id]}", now)
            elif claim.claim_id in finance:  # 财务投资的纠正（J4.6）：一句话、一组只问一次
                if self._hold_review(MANUAL_REVIEW, "relation", "relation", relation_id, claim.claim_id, hold_at[claim.claim_id],
                                     group_of[key], {"reason": correct.CORRECTION, "identity": key}, now,
                                     reason=correct.CORRECTION):
                    self.stats.corrections_held += 1
            else:
                self._soft_review("relation", relation_id, claim.claim_id, soft[claim.claim_id], key, now)
        still = {(soft[claim.claim_id], str(by_claim[claim.claim_id]["identity_key"])) for _, claim in held if claim.claim_id in soft}
        still_c = {(claim.anchor, str(by_claim[claim.claim_id]["identity_key"])) for _, claim in held if claim.claim_id in holds}
        still_f = {(hold_at[claim.claim_id], group_of[str(by_claim[claim.claim_id]["identity_key"])])
                   for _, claim in held if claim.claim_id in finance}
        self._unhold(SOFT_CHANGE, "relation", identities, still)
        self._unhold(MANUAL_REVIEW, None, identities, still_c)
        self._unhold(MANUAL_REVIEW, "relation", set(group_of.values()), still_f, reason=correct.CORRECTION)

    def _soft_facts(
        self, claims_of: Mapping[str, Sequence[sqlite3.Row]]
    ) -> tuple[dict[str, tuple[str, str]], set[str], dict[tuple[str, str], str]]:
        """一组事实里软变化的候选（J4.4 第 4 段，javmem.soft.rules 的表）。按说的先后一处一处看：
        - 候选那一处，这句话以前组里说过别的值（别的事实）、这一刻组里最后说的不是它自己的（只是又说了一遍现在的值的不挡，
          第三轮审阅 N1），先挡住：不参加新旧，这条事实只有挡住的说法的是待确认；判了不改（A、E、30 天没人管）的记成打算，
          照旧挡着（不参加新旧，D151）
        - 判了暂停的：这一处另记的事实和旧的同时现行（D153 第 4 条：事实不加“暂停”），不替代别的（后来说的别的值照常替代它）
        - 判了结束、换新的：放出来照常新旧，这一处替代旧的那一次，替代时间取判的那天（只这一次，第三轮审阅 N7）
        返回 (挡住的说法 → (候选句, 事实), 和旧的同时现行的说法, (事实, 那一句说话的时刻) → 判的那天写成的时刻)。"""
        soft = {str(claim["claim_id"]): (str(claim["soft_sentence"]), fact_id)
                for fact_id, claims in claims_of.items() for claim in claims if claim["soft_sentence"]}
        if not soft:
            return {}, set(), {}
        decided = self._soft_decided("fact", soft.values())
        rows = {str(claim["claim_id"]): claim for claims in claims_of.values() for claim in claims}
        seen: dict[str, int] = {}  # 事实 → 它第一处照常参加新旧的说法什么时候说的
        last_fact: str | None = None  # 这一刻组里最后说的（照常参加新旧的）那条
        held: dict[str, tuple[str, str]] = {}
        parallel: set[str] = set()
        judged_at: dict[tuple[str, str], str] = {}
        ordered = sorted(((int(claim["said_from_epoch"]), str(claim["claim_id"]), fact_id)
                          for fact_id, claims in claims_of.items() for claim in claims))
        for said, claim_id, fact_id in ordered:
            if claim_id not in soft:
                seen.setdefault(fact_id, said)
                last_fact = fact_id
                continue
            record = decided.get(soft[claim_id])
            action = record.get("action") if record else None
            kind = action.get("kind") if isinstance(action, Mapping) else None
            if kind in (soft_rules.END, soft_rules.REPLACE):
                seen.setdefault(fact_id, said)
                last_fact = fact_id
                at = action.get("at")  # type: ignore[union-attr]
                if isinstance(at, str) and at:
                    for proof in json.loads(rows[claim_id]["proofs"]):
                        judged_at[(fact_id, str(proof["said_at"]))] = _moment_of(at, str(proof["said_at"]))
                continue
            if kind == soft_rules.PAUSE:
                parallel.add(claim_id)
                continue
            if any(other != fact_id and first < said for other, first in seen.items()) and last_fact != fact_id:
                held[claim_id] = soft[claim_id]
            else:
                seen.setdefault(fact_id, said)
                last_fact = fact_id
        return held, parallel, judged_at

    def _fingerprint(self, sentence_id: str) -> tuple[str, str] | None:
        """候选句的指纹：哪个包、sentence_hash(说话人, 原句)（这个包现在那一版的那一句）。句子编号按位置算，包重发时同一个
        位置可能换了一句话、同一句话也可能挪了位置：定过的按指纹认（第三轮审阅 S3）。"""
        row = self.conn.execute(
            "SELECT s.packet_id, s.speaker, s.text FROM raw_sentences AS s JOIN packets AS p"
            " ON p.packet_id = s.packet_id AND p.revision = s.revision AND p.state = 'current' WHERE s.sentence_id = ? LIMIT 1",
            (sentence_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["packet_id"]), sentence_hash(str(row["speaker"]), str(row["text"]))

    def _held_lookup(
        self, kind: str, item: str | None, pairs: Iterable[tuple[str, str]], *, decided: bool, reason: str | None = None
    ) -> dict[tuple[str, str], sqlite3.Row]:
        """软变化（soft_change）、判断 C 拿不准的（manual_review）的待确认：(那一句, 键) → 那一行。认法：键一样；记了指纹的按
        包和指纹认（包重发时同一句挪了位置也认得出，同一个位置换了一句话的不认），没记的按句子编号。decided：只看定过的
        （后定的盖过先定的，按定的时间），不然只看没处理的。item 是 None 的只看没写 item 的（判断 C 拿不准的）；纠正挡住的
        （J4.6，manual_review 带 item 和 reason）按 reason 分开。"""
        wanted = set(pairs)
        if not wanted:
            return {}
        prints = {sentence: self._fingerprint(sentence) for sentence, _ in wanted}
        by_key: dict[str, list[tuple[str, str]]] = {}
        for pair in sorted(wanted):
            by_key.setdefault(pair[1], []).append(pair)
        condition, order = ("IS NOT NULL", "decided_at, review_id") if decided else ("IS NULL", "review_id")
        out: dict[tuple[str, str], sqlite3.Row] = {}
        for row in self.conn.execute(
            f"SELECT review_id, decision, decided_by, created_at, claims FROM review_queue WHERE kind = ? AND decided_at {condition}"
            f" ORDER BY {order}",
            (kind,),
        ):
            claims = json.loads(row["claims"])
            if not isinstance(claims, dict) or claims.get("item") != item or (reason is not None and claims.get("reason") != reason):
                continue
            for pair in by_key.get(str(claims.get("key")), ()):
                printed = prints.get(pair[0])
                if claims.get("fingerprint"):
                    same = printed is not None and [claims.get("packet"), claims.get("fingerprint")] == list(printed)
                else:
                    same = str(claims.get("sentence")) == pair[0]
                if same:
                    out[pair] = row
        return out

    def _held_decided(self, kind: str, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict[str, object]]:
        """定过的判断 C 拿不准的（manual_review）：(那一句, 同一条关系的键) → {decision, claims}。"""
        return {pair: {"decision": row["decision"], "claims": json.loads(row["claims"])}
                for pair, row in self._held_lookup(kind, None, pairs, decided=True).items()}

    def _soft_decided(self, item: str, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict[str, object]]:
        """定过的软变化（主人、模型、30 天没人管）：(候选句, 键) → 那条待确认记下的 claims（action 是照哪个结论做）。
        键：关系是同一条关系的键（identity_key），事实是事实编号。"""
        return {pair: json.loads(row["claims"]) for pair, row in self._held_lookup(SOFT_CHANGE, item, pairs, decided=True).items()}

    def _hold_review(self, kind: str, item: str | None, owner: str, item_id: str, claim_id: str, sentence: str, key: str,
                     extra: Mapping[str, object], now: str, *, reason: str | None = None) -> bool:
        """挡住的那一处进待确认（软变化 soft_change、判断 C 拿不准的 manual_review、纠正挡住的 manual_review）：同一句（按指纹
        认）、同一条的进过一次就不再进（定过的也不再进），指着的那一行、那一处、那一句换了的改指过去。owner 是 claims 里记
        那一行的名字。新进了一条返回 True。"""
        found = (self._held_lookup(kind, item, [(sentence, key)], decided=True, reason=reason)
                 or self._held_lookup(kind, item, [(sentence, key)], decided=False, reason=reason))
        row = found.get((sentence, key))
        if row is not None:
            claims = json.loads(row["claims"])
            update = {owner: item_id, "claim": claim_id, "sentence": sentence, **{name: extra[name] for name in ("identity",)
                                                                                  if name in extra}}
            if any(claims.get(name) != value for name, value in update.items()):
                claims.update(update)
                self.conn.execute("UPDATE review_queue SET claims = ? WHERE review_id = ?", (_json(claims), row["review_id"]))
                self.reviews_changed()
            return False
        claims = {**({"item": item} if item else {}), owner: item_id, "claim": claim_id, "sentence": sentence, "key": key, **extra}
        printed = self._fingerprint(sentence)
        if printed is not None:
            claims.update(packet=printed[0], fingerprint=printed[1])
        self.conn.execute(
            "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES (?, ?, ?, ?)",
            (kind, _json(claims), _json([sentence]), now),
        )
        if kind == SOFT_CHANGE:
            self.stats.soft_held += 1
        return True

    def _judge_review(self, relation_id: str, claim_id: str, sentence: str, key: str, reason: str, now: str) -> None:
        """判断 C 拿不准的那一处进待确认（manual_review）。开了进图前检查（J5.3）的记一笔 precheck：主人定的时候写留痕。"""
        extra: dict[str, object] = {"reason": reason, **({"precheck": True} if self.precheck_on else {})}
        self._hold_review(MANUAL_REVIEW, None, "relation", relation_id, claim_id, sentence, key, extra, now)

    def _soft_review(self, item: str, item_id: str, claim_id: str, sentence: str, key: str, now: str) -> None:
        """挡住的那一处进待确认（soft_change，还没判）。"""
        self._hold_review(SOFT_CHANGE, item, item, item_id, claim_id, sentence, key, {"reasons": [soft_rules.NOT_JUDGED]}, now)

    def _unhold(self, kind: str, item: str | None, keys: Iterable[str], still: Collection[tuple[str, str]], *,
                reason: str | None = None) -> None:
        """这几条（同一条关系的键、事实编号；纠正挡住的是判新旧的组）上没处理的这一种待确认，那一处不再挡着了的（旧的没了、
        只是又说了一遍、判过了、那一句撤回了、那一处说法没了）删掉（第三轮审阅 N2）。still：现在还挡着的 (那一句, 键)。
        item 是 None 的只管没写 item 的（判断 C 拿不准的）；reason 给了的只管这一种原因的（纠正挡住的，J4.6）。"""
        for chunk in _chunks(sorted(set(keys))):
            for row in self.conn.execute(
                f"SELECT review_id, claims FROM review_queue WHERE kind = ? AND decided_at IS NULL"
                f" AND json_extract(claims, '$.key') IN ({_marks(chunk)})",
                (kind, *chunk),
            ).fetchall():
                claims = json.loads(row["claims"])
                if claims.get("item") != item or (reason is not None and claims.get("reason") != reason):
                    continue
                if (str(claims.get("sentence")), str(claims.get("key"))) not in still:
                    self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (row["review_id"],))

    # ---------- 纠正（J4.6） ----------

    def _finance_holds(self, fixes: Mapping[str, str], by_claim: Mapping[str, sqlite3.Row], group_of: Mapping[str, str],
                       templates: Mapping[str, sqlite3.Row]) -> set[str]:
        """财务投资的关系被主人纠正（可见范围里有严格分区，D7，主人 11-17 第 2 题 A）、主人还没说照改的那几处：先挡住。
        主人说照改（keep_new）的照常；说不改（keep_hidden，30 天没人管也是）的接着挡着。"""
        pairs: dict[str, tuple[str, str]] = {}
        for claim_id, sentence in fixes.items():
            identity = str(by_claim[claim_id]["identity_key"])
            if set(json.loads(str(templates[identity]["scope_partitions"]))) & self.strict_partitions:
                pairs[claim_id] = (sentence, group_of[identity])
        if not pairs:
            return set()
        kept = self._held_lookup(MANUAL_REVIEW, "relation", pairs.values(), decided=True, reason=correct.CORRECTION)
        return {claim_id for claim_id, pair in pairs.items() if (kept[pair]["decision"] if pair in kept else None) != KEEP_NEW}

    def _live_sentence(self, claims: Mapping[str, object]) -> sqlite3.Row | None:
        """待确认里记的那一句还在（这个包现在那一版、没撤回；记了指纹的指纹要对得上）：那一句；不在的 None。"""
        row = self.conn.execute(
            "SELECT s.sentence_id, s.packet_id, s.speaker, s.text, s.said_at, s.said_at_epoch FROM raw_sentences AS s"
            " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision AND p.state = 'current'"
            " WHERE s.sentence_id = ? AND s.withdrawn_at IS NULL AND p.withdrawn_at IS NULL LIMIT 1",
            (str(claims.get("sentence")),),
        ).fetchone()
        if row is None:
            return None
        if claims.get("fingerprint") and [claims.get("packet"), claims.get("fingerprint")] != [
                row["packet_id"], sentence_hash(str(row["speaker"]), str(row["text"]))]:
            return None
        return row

    def reviews_changed(self) -> None:
        """待确认表改了（定了、导回了、删了）：从它算出来、存在这个 writer 上的几样作废，下次用时重读。"""
        self._kept_unmatched = None
        self._answered = None
        self._answer_ids = None
        self._fact_holds_open = None

    def _open_fact_holds(self) -> bool:
        """待确认表里有没有没处理的、进图前检查转人工的事实（J5.3）。一个 writer 查一次（关着、从来没开过的一句 SQL 都不在
        每条事实上多跑），待确认表改了再查。"""
        if self._fact_holds_open is None:
            self._fact_holds_open = self.conn.execute(
                "SELECT 1 FROM review_queue WHERE kind = 'manual_review' AND decided_at IS NULL"
                " AND json_extract(claims, '$.item') = 'fact' LIMIT 1"
            ).fetchone() is not None
        return self._fact_holds_open

    def _kept_unmatched_reviews(self) -> dict[str, list[tuple[int, dict[str, object]]]]:
        """主人认了的“抽取没抽出来的纠正”、只说了新的那一条时问的“旧的那一条不算了”（keep_new），按同一条关系的键：每个
        writer 读一次（重算一组就读一遍待确认表太慢，第一轮审阅第 8 条），定、导回、撤回时作废（reviews_changed）。"""
        if self._kept_unmatched is None:
            found: dict[str, list[tuple[int, dict[str, object]]]] = {}
            for row in self.conn.execute(
                "SELECT review_id, claims FROM review_queue WHERE kind = ? AND decided_at IS NOT NULL AND decision = ?"
                " AND json_extract(claims, '$.reason') IN (?, ?) ORDER BY decided_at, review_id",
                (MANUAL_REVIEW, KEEP_NEW, *correct.ASKED),
            ):
                claims = json.loads(row["claims"])
                found.setdefault(str(claims.get("identity") or claims.get("key")), []).append((int(row["review_id"]), claims))
            self._kept_unmatched = found
        return self._kept_unmatched

    def _unmatched_directives(self, identities: Collection[str], used: Collection[str] = ()) -> list[tuple[str, RelationClaimView]]:
        """主人认了的“抽取没抽出来的纠正”（keep_new）：给那一条加一处判出来的“说法”（不存，编号 ! 开头），在说那一句的时候
        结束它、记着被纠正（结束那天空着，第 1 题 A）。那一句撤回了、换了的不算；后来重抽抽出来了的（那一句已经是这一组
        哪一处说法的证据、纠正那一句，used）也不算，照抽出来的那一处办（第一轮审阅第 5 条）。"""
        kept = self._kept_unmatched_reviews() if identities else {}
        out = []
        for review_id, claims in sorted((one for identity in identities for one in kept.get(identity, ())),
                                        key=lambda one: one[0]):
            identity = str(claims.get("identity") or claims.get("key"))
            if str(claims.get("sentence")) in used:
                continue
            said = self._live_sentence(claims)
            moment = parse_aware(str(said["said_at"])) if said is not None else None
            if said is None or moment is None:
                continue
            out.append((identity, RelationClaimView(
                claim_id=f"{DIRECTIVE_PREFIX}u{review_id}", state="ended", valid_from=None, valid_from_precision="unknown",
                valid_from_basis=None, valid_to=None, valid_to_precision="unknown", valid_to_basis=None, status="verified",
                speaker=str(said["speaker"]), speaker_role=ROLE_OWNER, attributed_to=None, said_from=str(said["said_at"]),
                said_from_epoch=int(said["said_at_epoch"]), said_to=str(said["said_at"]), said_to_epoch=int(said["said_at_epoch"]),
                anchor=str(said["sentence_id"]), said_day=moment.date(), correction=str(said["sentence_id"]),
                sentences=(str(said["sentence_id"]),),
            )))
        return out

    def _spellings(self, entity_id: str, scope: Scope) -> list[str]:
        """一个实体在这个可见范围里认得的写法：名字，加上出处和这个范围一样的别名（D30）。"""
        names = [str(row[0]) for row in self.conn.execute("SELECT canonical_name FROM entities WHERE entity_id = ?", (entity_id,))]
        for row in self.conn.execute(
            "SELECT DISTINCT a.alias, p.partitions, p.sensitive FROM aliases AS a JOIN evidence AS v ON v.alias_id = a.alias_id"
            " JOIN packets AS p ON p.packet_id = v.packet_id AND p.revision = v.revision"
            " WHERE a.entity_id = ? AND a.withdrawn_at IS NULL AND p.state = 'current' AND p.withdrawn_at IS NULL",
            (entity_id,),
        ):
            if _scope_of_row(row["partitions"], row["sensitive"]) == scope:
                names.append(str(row["alias"]))
        return names

    def _unmatched_target(self, window: Window, row: sqlite3.Row, kinds: Collection[str]) -> tuple[str, str] | None:
        """抽取没抽出来的纠正对的是哪一条：这句话（加上紧挨着的上一条消息）里提到了两头、关系又是被否定的那几种之一的
        现行关系，只有一条的就是它（主人自己那一头不用提到：是主人在说）；对不上、对上几条的不猜。返回 (关系编号, 同一条关系的键)。"""
        texts = [str(row["text"])]
        key = correct.message_key(row)
        if key is not None:
            texts += [str(one["text"]) for one in correct.message_rows(self.conn, key) if one["sentence_id"] != row["sentence_id"]
                      and one["withdrawn_at"] is None and not one["quoted"]]
            if key[3] > 1:
                texts += [str(one["text"]) for one in correct.message_rows(self.conn, (key[0], key[1], key[2], key[3] - 1))
                          if one["withdrawn_at"] is None and not one["quoted"]]
        text = "\n".join(texts)
        owner = entity_id_of("人", OWNER_NAME)
        found: list[tuple[str, str]] = []
        spellings: dict[str, list[str]] = {}
        for candidate in self.conn.execute(
            f"SELECT relation_id, identity_key, subject_entity_id, object_entity_id FROM relations WHERE relation_type IN"
            f" ({_marks(sorted(kinds))}) AND scope_partitions = ? AND scope_sensitive = ? AND withdrawn_at IS NULL"
            " AND superseded_by IS NULL AND state IN ('active', 'paused') AND status <> 'pending' ORDER BY relation_id",
            (*sorted(kinds), _json(list(window.scope.partitions)), _json(list(window.scope.sensitive))),
        ).fetchall():
            ends = [str(candidate["subject_entity_id"]), str(candidate["object_entity_id"])]

            def spelled(end: str) -> list[str]:
                # 一个实体的写法只查一次（第一轮审阅第 8 条）；前一头没提到就不查后一头（第二轮第 9 条）
                if end not in spellings:
                    spellings[end] = self._spellings(end, window.scope)
                return spellings[end]

            if all(end == owner or correct.mentioned(text, spelled(end)) for end in ends):
                found.append((str(candidate["relation_id"]), str(candidate["identity_key"])))
        return found[0] if len(found) == 1 else None

    def _unmatched_corrections(self, item: Checked, now: str) -> None:
        """抽取没抽出来的纠正（J4.6，主人 11-17 第 4 题 B）：这个窗口正文里主人在纠正（correct.correction_sentence 的认法）、
        又是“不负责”“不在……了”这种否定的一句，这个窗口写下的关系说法一处都没用到它（证据里没有、也不是哪一处的纠正那一句）：
        对得上唯一一条现行关系的进待确认（manual_review，原因 correction_unmatched），旧的照旧，主人认了再结束它。
        以前问过、现在不再对得上的（重抽抽出来了、那一句换了），没处理的删掉。"""
        window = item.window
        used: set[str] = set()
        for claim in self._window_relation_claims(window.window_id):
            used |= {str(proof["sentence_id"]) for proof in json.loads(claim["proofs"])}
            if claim["correction"]:
                used.add(str(claim["correction"]))
        produced: set[tuple[str, str]] = set()
        if window.channel == "chat":
            for line in window.targets:
                if line.sentence_id in used:
                    continue
                kinds = correct.negated_types(line.text)
                if not kinds or soft_words.hedges(line.text):  # 有犹豫词的照软变化优先，不当纠正问
                    continue
                row = self.conn.execute(
                    "SELECT s.*, p.channel FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id"
                    " AND p.revision = s.revision WHERE s.sentence_id = ? AND s.revision = ?",
                    (line.sentence_id, line.revision),
                ).fetchone()
                if row is None or not correct.owner_said(row):
                    continue
                cue = correct.correction_sentence(self.conn, [{"sentence_id": line.sentence_id, "revision": line.revision,
                                                               "said_epoch": row["said_at_epoch"]}])
                if cue is None:
                    continue
                target = self._unmatched_target(window, row, kinds)
                if target is None:
                    continue
                relation_id, identity = target
                if identity in self._answered_sentences().get(line.sentence_id, frozenset()):
                    continue  # 这一句就是主人答这一条待确认的话（D83）：照主人定的，不再问
                if self._asked_before(identity, line.sentence_id, answered=False):
                    continue  # 这一条别的那一句正问着（还算不算、是不是说它）：同一条旧的只挂一条（K12 复审第 2 条）
                if self._hold_review(MANUAL_REVIEW, "relation", "relation", relation_id, "", line.sentence_id, identity,
                                     {"reason": correct.UNMATCHED, "identity": identity, "cue": cue}, now,
                                     reason=correct.UNMATCHED):
                    self.stats.corrections_unmatched += 1
                produced.add((line.sentence_id, identity))
        targets = {line.sentence_id for line in window.targets}
        packets = sorted({line.packet_id for line in window.targets})
        # 只看这个窗口的包里那几句问的（别的包撤回、重发时由 remove_sentences、remove_revision 管；第一轮审阅第 8 条：
        # 每个窗口把没处理的都读一遍太慢）
        for review in self.conn.execute(
            "SELECT review_id, claims FROM review_queue WHERE kind = ? AND decided_at IS NULL AND json_extract(claims, '$.reason') = ?"
            f" AND substr(json_extract(claims, '$.sentence'), 1, instr(json_extract(claims, '$.sentence'), '#') - 1)"
            f" IN ({_marks(packets)})",
            (MANUAL_REVIEW, correct.UNMATCHED, *packets),
        ).fetchall():
            claims = json.loads(review["claims"])
            pair = (str(claims.get("sentence")), str(claims.get("key")))
            if (pair[0] in targets and pair not in produced) or self._live_sentence(claims) is None:
                self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (review["review_id"],))

    def _reapply_kept(self, item: Checked, now: str) -> None:
        """主人认过的“抽取没抽出来的纠正”“旧的那一条不算了”（keep_new），那一句在这个窗口里的（包重发了同一句、解除撤回又
        写回来）：那一条照它结束，重算（第一轮审阅第 2 条）。排在两种问话后面：包重发挪了位置的，它们先把待确认改指到新的
        位置（K12 审阅第 3 条）。"""
        targets = {line.sentence_id for line in item.window.targets}
        again = {identity for identity, kept in self._kept_unmatched_reviews().items()
                 for _, claims in kept if str(claims.get("sentence")) in targets}
        if again:
            self._refresh_relations(again, set(), now)

    def _other_cue(self, claim: sqlite3.Row) -> str | None:
        """这一处说法是主人带着纠正词说的新的那一条（只说了新的、要不要问旧的）：纠正那一句（correction），或者证据里主人
        自己说的、带着“其实”的那一句（correct.asks_about_others）。说了旧的也还在的（“也”“还”“兼”，见
        correct.keeps_others）都不算，返回 None。"""
        if claim["correction"]:
            row = self.conn.execute(
                "SELECT s.text FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
                " AND p.state = 'current' WHERE s.sentence_id = ? LIMIT 1", (claim["correction"],)
            ).fetchone()
            return None if row is None or correct.keeps_others(str(row["text"])) else str(claim["correction"])
        for proof in json.loads(claim["proofs"]):
            row = self.conn.execute(
                "SELECT s.*, p.channel FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id"
                " AND p.revision = s.revision WHERE s.sentence_id = ? AND s.revision = ?",
                (proof["sentence_id"], proof["revision"]),
            ).fetchone()
            if row is not None and correct.owner_said(row) and correct.asks_about_others(str(row["text"])):
                return str(row["sentence_id"])
        return None

    def _other_corrections(self, item: Checked, now: str) -> None:
        """只说了新的那一条（主人 11-17 复核测试题 K12，做法第 11 节第 5 条改的）：可以同时有几条、平常是一条的关系
        （correct.ASK_OTHERS：任职于），主人带着纠正词（“其实”也算）说了新的一条、没说旧的不对：新的照记，旧的不动，
        同一个主体、同一种关系、同一个可见范围里在这句话以前就现行的别的那几条，各进一条待确认（manual_review，原因
        correction_other）问旧的那一条还算不算。不问的：同一条消息里说到了旧的那一条的（“早就不在那家了”）；说了旧的也还在的
        （“也”“还”“兼”）；主人答待确认的那几句（D83，认没认出来都算）；新的那一家以前就说过的（不是新的一条）；新的那一条
        自己还在待确认的；旧的那一条别的那一句已经问过、还没处理或者定了还算的（_asked_before）。主人说不算了（keep_new）的，
        那一条在说那一句的时候结束、记着被纠正（照“抽取没抽出来的纠正”的办法）；还算（keep_hidden）、30 天没人管的不改。
        以前问过、现在不再这样的（那一句换了、撤回了、重抽说到了旧的），没处理的删掉。"""
        window = item.window
        produced: set[tuple[str, str]] = set()
        if window.channel == "chat":
            claims = self._window_relation_claims(window.window_id)
            touched_in: dict[tuple[str, str, str] | None, set[str]] = {}  # 每条消息里说到了哪几条关系
            for claim in claims:
                for proof in json.loads(claim["proofs"]):
                    touched_in.setdefault(_message_of(str(proof["sentence_id"])), set()).add(str(claim["identity_key"]))
            for claim in claims:
                if claim["state"] == "ended":
                    continue
                new = self.conn.execute(
                    "SELECT relation_type, subject_entity_id, scope_partitions, scope_sensitive, withdrawn_at, status FROM relations"
                    " WHERE relation_id = ?", (claim["relation_id"],)
                ).fetchone()
                if (new is None or new["relation_type"] not in correct.ASK_OTHERS or new["withdrawn_at"] is not None
                        or new["status"] == "pending"):
                    continue  # 新的那一条自己还在待确认的（判断 C 拿不准）先不问旧的（K12 审阅）
                cue = self._other_cue(claim)
                if cue is None or cue in self._answer_sentences():
                    continue  # 主人答待确认的那几句（认没认出来都算）不再问别的（K12 审阅第 1 条）
                said = self.conn.execute("SELECT said_at_epoch FROM raw_sentences WHERE sentence_id = ?", (cue,)).fetchone()
                if said is None:
                    continue
                if self.conn.execute(
                    "SELECT 1 FROM relation_claims WHERE relation_id = ? AND claim_id <> ? AND said_from_epoch < ?"
                    " AND speaker_role = 'owner' AND state <> 'ended' AND soft_sentence IS NULL AND judge_hold IS NULL LIMIT 1",
                    (claim["relation_id"], claim["claim_id"], said["said_at_epoch"]),
                ).fetchone() is not None:
                    # 这一段以前主人就说定了（“其实她在东岸证券兼职很久了”）：不是新的一条，不问（K12 审阅第 2 条）。早就结束了
                    # 的那一段、带犹豫词的、别人说的、判断 C 挡着的不算（复审第 1 条）
                    continue
                mentioned = touched_in.get(_message_of(cue), set())
                for other in self.conn.execute(
                    "SELECT relation_id, identity_key FROM relations WHERE relation_type = ? AND subject_entity_id = ?"
                    " AND scope_partitions = ? AND scope_sensitive = ? AND identity_key IS NOT NULL AND identity_key <> ?"
                    " AND withdrawn_at IS NULL AND superseded_by IS NULL AND state IN ('active', 'paused')"
                    " AND status <> 'pending' AND as_of_epoch < ? ORDER BY relation_id",
                    (new["relation_type"], new["subject_entity_id"], new["scope_partitions"], new["scope_sensitive"],
                     claim["identity_key"], said["said_at_epoch"]),
                ).fetchall():
                    identity = str(other["identity_key"])
                    if identity in mentioned or self._asked_before(identity, cue):
                        continue
                    if self._hold_review(MANUAL_REVIEW, "relation", "relation", str(other["relation_id"]), "", cue, identity,
                                         {"reason": correct.OTHER, "identity": identity, "new": str(claim["identity_key"])},
                                         now, reason=correct.OTHER):
                        self.stats.corrections_other += 1
                    produced.add((cue, identity))
        targets = {line.sentence_id for line in window.targets}
        packets = sorted({line.packet_id for line in window.targets})
        for review in self.conn.execute(
            "SELECT review_id, claims FROM review_queue WHERE kind = ? AND decided_at IS NULL AND json_extract(claims, '$.reason') = ?"
            f" AND substr(json_extract(claims, '$.sentence'), 1, instr(json_extract(claims, '$.sentence'), '#') - 1)"
            f" IN ({_marks(packets)})",
            (MANUAL_REVIEW, correct.OTHER, *packets),
        ).fetchall():
            claims = json.loads(review["claims"])
            pair = (str(claims.get("sentence")), str(claims.get("key")))
            if (pair[0] in targets and pair not in produced) or self._live_sentence(claims) is None:
                self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (review["review_id"],))

    def _asked_before(self, identity: str, cue: str, *, answered: bool = True) -> bool:
        """旧的那一条别的那一句已经问过、不用再问（K12 审阅第 5、6 条，复审第 2 条）：问它的待确认（抽取没抽出来的纠正、只说了
        新的那一条）还没处理；answered 的，问过它还算不算、定了还算（keep_hidden，30 天没人管的也是）的也算（抽取没抽出来的
        纠正不算这个：主人后来明说了不在了，是新的话）。同一句（编号一样或者指纹一样）的不算：那是这一条自己，照 _hold_review
        改指过去；那一句已经不在了的（撤回了、重发换掉了，等着删）也不算。"""
        printed = self._fingerprint(cue)
        for row in self.conn.execute(
            "SELECT decided_at, decision, claims FROM review_queue WHERE kind = ? AND json_extract(claims, '$.key') = ?"
            " AND json_extract(claims, '$.reason') IN (?, ?)",
            (MANUAL_REVIEW, identity, *correct.ASKED),
        ):
            claims = json.loads(row["claims"])
            if claims.get("sentence") == cue or (printed is not None and [claims.get("packet"), claims.get("fingerprint")]
                                                 == list(printed)):
                continue
            if self._live_sentence(claims) is None:
                continue
            if row["decided_at"] is None or (answered and row["decision"] == KEEP_HIDDEN and claims.get("reason") == correct.OTHER):
                return True
        return False

    # ---------- 对话里答待确认（D83，J4.6 第 2 段，主人 11-17 选择卡第 5 题 A） ----------

    def fill_codes(self) -> None:
        """没写编号的待确认补上编号（answers.code_of：按细分以后的种类和内容算，整本重建也不变）。记下以后照记下的（Bot
        拿到的是这个），内容以后变了也不换。不自己开事务。"""
        for row in self.conn.execute("SELECT review_id, kind, claims FROM review_queue WHERE code IS NULL").fetchall():
            claims = json.loads(row["claims"])
            claims = claims if isinstance(claims, dict) else {}
            self.conn.execute("UPDATE review_queue SET code = ? WHERE review_id = ?",
                              (answers.code_of(_review_type(row["kind"], claims), claims), row["review_id"]))

    def _prints_now(self, prints: Iterable[Sequence[object]]) -> dict[tuple[str, str], list[str]]:
        """指纹（包号、sentence_hash）→ 这个包现在那一版里、没撤回、指纹一样的那几句的编号（包重发挪了位置也认得出）。"""
        wanted: dict[str, set[str]] = {}
        for one in prints:
            if len(one) == 2:
                wanted.setdefault(str(one[0]), set()).add(str(one[1]))
        found: dict[tuple[str, str], list[str]] = {}
        for packet_id, hashes in wanted.items():
            for row in self.conn.execute(
                "SELECT s.sentence_id, s.speaker, s.text FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id"
                " AND p.revision = s.revision AND p.state = 'current' AND p.withdrawn_at IS NULL"
                " WHERE s.packet_id = ? AND s.withdrawn_at IS NULL ORDER BY s.seq", (packet_id,)
            ):
                printed = sentence_hash(str(row["speaker"]), str(row["text"]))
                if printed in hashes:
                    found.setdefault((packet_id, printed), []).append(str(row["sentence_id"]))
        return found

    def _answered_sentences(self) -> dict[str, frozenset[str]]:
        """认出来、照答的定了的对话回答：回答那几句现在的编号（按指纹找，包重发挪了位置也认得出）→ 这个决定管的那几条关系
        （同一条关系的键、判新旧的组，answer 的 about）。每个 writer 读一次，待确认表改了作废。"""
        if self._answered is None:
            self._read_answers()
        assert self._answered is not None
        return self._answered

    def _answer_sentences(self) -> frozenset[str]:
        """主人在对话里答待确认的那几句现在的编号，认出来定了的、认不出只记了一笔的都算（只说了新的那一条时，这几句不再问
        旧的那几条，K12 审阅第 1 条）。每个 writer 读一次，待确认表改了作废。"""
        if self._answer_ids is None:
            self._read_answers()
        assert self._answer_ids is not None
        return self._answer_ids

    def _read_answers(self) -> None:
        found: dict[str, set[str]] = {}
        everyone: set[str] = set()
        records = []
        for text, decided_at in self.conn.execute("SELECT answer, decided_at FROM review_queue WHERE answer IS NOT NULL"):
            record = json.loads(text)
            if isinstance(record, dict):
                records.append((record, decided_at is not None and bool(record.get("decision")) and bool(record.get("about"))))
        now_ids = self._prints_now(one for record, _ in records for one in record.get("answer_prints") or ())
        for record, applied in records:
            about = {str(one) for one in record.get("about") or ()}
            for one in record.get("answer_prints") or ():
                for sentence in now_ids.get((str(one[0]), str(one[1])), ()):
                    everyone.add(sentence)
                    if applied:
                        found.setdefault(sentence, set()).update(about)
        self._answered = {sentence: frozenset(keys) for sentence, keys in found.items()}
        self._answer_ids = frozenset(everyone)

    def _confirmed_claims(self, views: Mapping[str, Sequence[RelationClaimView]], group_of: Mapping[str, str]) -> set[str]:
        """回答待确认的那几句（D83）自己抽出来的、和那一条待确认同一条关系或者同一组的说法：照主人定的算，软变化、财务投资
        都不再挡一遍（做法第 3.4 节）。同一条消息里顺口说的别的关系照常挡（第 2 段审阅第 2 条）。"""
        answered = self._answered_sentences()
        if not answered:
            return set()
        out = set()
        for identity, claims in views.items():
            mine = {identity, group_of.get(identity, identity)}
            for claim in claims:
                if claim.pseudo:
                    continue
                keys = set().union(*(answered.get(sentence, frozenset()) for sentence in (*claim.sentences, claim.correction)
                                     if sentence))
                if keys & mine:
                    out.add(claim.claim_id)
        return out

    def _said_messages(self, packet_id: str, revision: int) -> list[answers.Said]:
        """一个包（这一版）原话小节里的消息，按先后：原文索引里同一行的几句是一条消息（新写的那几句、引用的那几句）。"""
        grouped: dict[correct.MessageKey, list[sqlite3.Row]] = {}
        for row in self.conn.execute(
            "SELECT s.*, p.channel FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            " WHERE s.packet_id = ? AND s.revision = ? AND s.section_kind = 'raw' ORDER BY s.seq",
            (packet_id, revision),
        ):
            key = correct.message_key(row)
            if key is not None:
                grouped.setdefault(key, []).append(row)
        out = []
        for members in grouped.values():
            fresh = [row for row in members if correct.is_fresh(row)]
            quoted = [row for row in members if row["quoted"] and row["withdrawn_at"] is None]
            first = (fresh or members)[0]
            out.append(answers.Said(str(first["speaker_role"]), int(first["said_at_epoch"]),
                                    tuple((str(row["sentence_id"]), str(row["text"])) for row in fresh),
                                    tuple((str(row["sentence_id"]), str(row["text"])) for row in quoted)))
        return out

    def _original_question(self, code: str, before_epoch: int, speaker: str | None) -> tuple[str, tuple[str, ...]] | None:
        """引用了问话回的：Bot 原来问的那一句（新写的原话、带着这个编号、在回答以前、包现在那一版没撤回；引用行写了是谁说的，
        还要是他说的），最近的那一条问话消息。返回 (带编号的那一句, 那条消息的几句)；找不到（原来那句撤回了、引的是同事贴的
        编号）的不算回答（第 2 段审阅第 9 条）。"""
        found = None
        for row in self.conn.execute(
            "SELECT s.*, p.channel FROM raw_fts AS f JOIN raw_sentences AS s ON s.id = f.rowid"
            " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            " WHERE raw_fts MATCH ? AND p.state = 'current' AND p.withdrawn_at IS NULL AND p.channel = 'chat'",
            ('"' + code + '"',),
        ):
            if (row["speaker_role"] != ROLE_BOT or not correct.is_fresh(row) or int(row["said_at_epoch"]) > before_epoch
                    or code not in answers.find_codes(str(row["text"])) or (speaker is not None and row["speaker"] != speaker)):
                continue
            if found is None or int(row["said_at_epoch"]) > int(found["said_at_epoch"]):
                found = row
        if found is None:
            return None
        key = correct.message_key(found)
        mine = [one for one in (correct.message_rows(self.conn, key) if key is not None else [found]) if correct.is_fresh(one)]
        return str(found["sentence_id"]), tuple(str(one["sentence_id"]) for one in mine) or (str(found["sentence_id"]),)

    def _packet_before(self, packet_id: str) -> tuple[str, int] | None:
        """同一个来源、接在这一包前面的那一包（现行、没撤回）：写了接续哪一包的照它，不然按覆盖时段的先后。"""
        row = self.conn.execute(
            "SELECT source, continues, covers_from_epoch FROM packets WHERE packet_id = ? AND state = 'current'", (packet_id,)
        ).fetchone()
        if row is None:
            return None
        found = None
        if row["continues"]:
            found = self.conn.execute("SELECT packet_id, revision FROM packets WHERE packet_id = ? AND state = 'current'"
                                      " AND withdrawn_at IS NULL", (row["continues"],)).fetchone()
        if found is None:
            found = self.conn.execute(
                "SELECT packet_id, revision FROM packets WHERE source = ? AND channel = 'chat' AND state = 'current'"
                " AND withdrawn_at IS NULL AND covers_from_epoch < ? ORDER BY covers_from_epoch DESC, packet_id DESC LIMIT 1",
                (row["source"], row["covers_from_epoch"]),
            ).fetchone()
        return (str(found[0]), int(found[1])) if found is not None else None

    def _chat_answers(self, item: Checked, now: str) -> None:
        """D83：这个窗口正文里主人新写的话，是在答 Bot 问的哪一条待确认（answers.question_for：问完以后主人的第一条消息，
        同一包或者接着的下一包、30 分钟以内、中间 Bot 没问别的；或者引用了那句问话回的），认得出就照答的定（_apply_answer）。
        Bot 问的都是写过账的待确认，这时都有编号。"""
        window = item.window
        if window.channel != "chat":
            return
        packets = sorted({(line.packet_id, line.revision) for line in window.targets})
        # 这一包重发了：以前照这一包里的问话、回答定的，问话、回答在新的这一版里没了（按指纹认），先撤掉（第 2 段审阅第 4 条）
        self.revert_withdrawn_answers(now, packets={packet_id for packet_id, _ in packets})
        targets = {line.sentence_id for line in window.targets if line.origin == ORIGIN_SAID and not line.quoted}
        if not targets:
            return
        for packet_id, revision in packets:
            messages = self._said_messages(packet_id, revision)
            earlier: list[answers.Said] | None = None
            for index, message in enumerate(messages):
                if message.role != ROLE_OWNER or not message.parts or message.sentences[0] not in targets:
                    continue  # 一条消息只在它第一句所在的那个窗口认一次
                asked = answers.question_for(messages, index)
                if asked == answers.EARLIER:  # 问话可能在接着的上一包里
                    if earlier is None:
                        before = self._packet_before(packet_id)
                        earlier = self._said_messages(*before) if before is not None else []
                    asked = answers.question_for([*earlier, *messages], len(earlier) + index)
                if not isinstance(asked, answers.Asked):
                    continue
                if asked.how == "quoted":  # 引用了问话回的：照 Bot 原来那一句记、算范围
                    head = _QUOTE_HEAD.match(message.quoted[0][1]) if message.quoted else None
                    original = self._original_question(asked.code, message.epoch, head.group(1).strip() if head else None)
                    if original is None:
                        continue
                    asked = answers.Asked(original[0], asked.code, asked.how, original[1])
                self._apply_answer(asked, message, packet_id, revision, now)

    def _scope_of_sentence(self, sentence_id: str) -> Scope | None:
        row = self.conn.execute(
            "SELECT p.partitions, p.sensitive FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id"
            " AND p.revision = s.revision WHERE s.sentence_id = ?", (sentence_id,)
        ).fetchone()
        return _scope_of_row(row["partitions"], row["sensitive"]) if row is not None else None

    def _review_scopes(self, kind: str, claims: Mapping[str, object]) -> list[Scope]:
        """一条待确认说到的那几条（关系、事实；实体有歧义的是它记着的范围）的可见范围。"""
        ids = [claims.get(name) for name in ("frozen", "new", "relation", "fact")] + list(claims.get("relations") or [])  # type: ignore[call-overload]
        if claims.get("item") in ("relation", "fact"):
            ids.append(claims.get(str(claims["item"])))
        scopes = []
        for item_id in ids:
            if not isinstance(item_id, str) or not item_id:
                continue
            table, column = ("relations", "relation_id") if item_id.startswith("R") else ("facts", "fact_id")
            row = self.conn.execute(f"SELECT scope_partitions, scope_sensitive FROM {table} WHERE {column} = ?", (item_id,)).fetchone()
            if row is not None:
                scopes.append(_scope_of_row(row[0], row[1]))
        if kind == "ambiguous_entity" and isinstance(claims.get("scope"), str):
            try:
                data = json.loads(str(claims["scope"]))
                scopes.append(Scope(tuple(sorted(data["partitions"])), tuple(sorted(data["sensitive"]))))
            except (ValueError, KeyError, TypeError):
                pass
        return scopes

    def _visible_from(self, scopes: Sequence[Scope], seen_from: Scope) -> bool:
        """问话那一包的可见范围看得到这几条：照 query-api 第 3 节的算法（分区有一个能看就过，敏感标记每个都要能看）。
        一条都认不出范围的不算看得到。"""
        return bool(scopes) and all(set(one.partitions) & set(seen_from.partitions) and set(one.sensitive) <= set(seen_from.sensitive)
                                    for one in scopes)

    def _side_names(self, item_id: object) -> set[str]:
        """和冻结的冲突里一边的名字：关系是两头（和代表哪一家）的名字和这个范围的别名；事实是它的实体，加上值。"""
        if not isinstance(item_id, str) or not item_id:
            return set()
        names: set[str] = set()
        if item_id.startswith("R"):
            row = self.conn.execute(
                "SELECT subject_entity_id, object_entity_id, org_entity_id, scope_partitions, scope_sensitive FROM relations"
                " WHERE relation_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return names
            scope = _scope_of_row(row["scope_partitions"], row["scope_sensitive"])
            for end in (row["subject_entity_id"], row["object_entity_id"], row["org_entity_id"]):
                if end:
                    names.update(self._spellings(str(end), scope))
            return names
        row = self.conn.execute("SELECT value, scope_partitions, scope_sensitive FROM facts WHERE fact_id = ?", (item_id,)).fetchone()
        if row is None:
            return names
        if row["value"]:
            names.add(str(row["value"]))
        scope = _scope_of_row(row["scope_partitions"], row["scope_sensitive"])
        for entity_id in sorted(self._fact_entities(item_id)):
            names.update(self._spellings(entity_id, scope))
        return names

    def _apply_answer(self, asked: answers.Asked, message: answers.Said, packet_id: str, revision: int, now: str) -> None:
        """一条认出来是在答 asked 那一句问话的消息：编号对得上的、没处理的待确认只有一条，问话那一包又看得到它，才照答的
        认（answers.interpret）。认得出就照 decide_review 那一套定（谁定的写主人），review_queue.answer 记问话、回答各是
        哪一句，audit_log 写一行（tier 是 owner，依据是回答那一句）；认不出的（回头再说、答非所问）记一笔，待确认照旧。"""
        self.fill_codes()
        rows = self.conn.execute(
            "SELECT review_id, kind, claims, answer FROM review_queue WHERE decided_at IS NULL AND code = ?", (asked.code,)
        ).fetchall()
        if len(rows) != 1:
            return  # 没有这一条、已经定过了、别的库的、几条撞了编号：不算
        review = rows[0]
        claims = json.loads(review["claims"])
        claims = claims if isinstance(claims, dict) else {}
        kind = _review_type(review["kind"], claims)
        seen_from = self._scope_of_sentence(asked.sentence)
        if seen_from is None or not self._visible_from(self._review_scopes(review["kind"], claims), seen_from):
            return  # 问话那一包看不到这一条：不算
        new_names: tuple[str, ...] = ()
        old_names: tuple[str, ...] = ()
        if kind == "frozen_conflict":
            new_side, old_side = self._side_names(claims.get("new")), self._side_names(claims.get("frozen"))
            new_names, old_names = tuple(sorted(new_side - old_side)), tuple(sorted(old_side - new_side))
        decision = answers.interpret(kind, message.text, new_names=new_names, old_names=old_names)
        question_sentences = list(asked.sentences or (asked.sentence,))
        prints = {sentence: self._fingerprint(sentence) for sentence in (*question_sentences, *message.sentences)}
        record: dict[str, object] = {
            "question": asked.sentence, "question_sentences": question_sentences, "answer": message.sentences[0],
            "sentences": list(message.sentences), "packet": packet_id, "revision": revision, "how": asked.how,
            "decision": decision, "rules": answers.RULES_VERSION, "code": asked.code,
            # 指纹（包号、sentence_hash）：包重发挪了位置也认得出是哪几句，撤回、重发照它看还在不在（第 2 段审阅第 4 条）
            "question_prints": [list(prints[one]) for one in question_sentences if prints[one]],
            "answer_prints": [list(prints[one]) for one in message.sentences if prints[one]],
            "about": sorted(self._review_keys(kind, claims)),  # 这个决定管的那几条关系：回答里只有它们不再挡一遍
        }
        at = condition = None
        if decision == DROP:  # 删掉的回不来，对话里不收：记一笔，要主人用 --decide 定（第 2 段审阅第 1 条）
            record.update(decision=None, refused=DROP)
        elif kind == SOFT_CHANGE:
            details = self._soft_details(str(decision), message.text, claims)
            if details is None:  # 哪天、等什么、交给谁认不出（或者对不上）的不收（做法第 3.4 节）
                record.update(decision=None, unread=decision)
            else:
                at, condition = details
        if record["decision"] is None:
            if review["answer"] != _json(record):  # 重写同一个窗口不再多数一次
                self.conn.execute("UPDATE review_queue SET answer = ? WHERE review_id = ?", (_json(record), review["review_id"]))
                self.reviews_changed()
                self.stats.chat_answers_unclear += 1
            return
        if kind == SOFT_CHANGE:
            record["claims_before"] = claims  # 撤回回答时照定之前的样子放回去
        if decision == KEEP_NEW and kind in ("frozen_conflict", *correct.ASKED):
            target = claims.get("frozen") if kind == "frozen_conflict" else claims.get("relation")
            frozen = self.conn.execute("SELECT 1 FROM relations WHERE relation_id = ?1 AND frozen = 1 UNION ALL"
                                       " SELECT 1 FROM facts WHERE fact_id = ?1 AND frozen = 1", (target,)).fetchone()
            if frozen is not None:
                record["unfroze"] = target  # 用新值会解除冻结：撤回回答时再冻上
        if decision == KEEP_FROZEN and kind == EVIDENCE_WITHDRAWN:
            record["restored"] = claims.get("relation") or claims.get("fact")  # 放回来的：撤回回答时再藏起来
        # 定不成的（这一条现在不能这样定）整个退回去，不留半截（第 2 段审阅第 10 条）
        self.conn.execute("SAVEPOINT chat_answer")
        try:
            self.conn.execute("UPDATE review_queue SET answer = ? WHERE review_id = ?", (_json(record), review["review_id"]))
            self.reviews_changed()
            self.decide_review(int(review["review_id"]), str(decision), now, by="owner", at=at, condition=condition)
        except ValueError:  # 比如证据都撤回了的又有了说法、哪天认不出：不算，照旧
            self.conn.execute("ROLLBACK TO chat_answer")
            self.conn.execute("RELEASE chat_answer")
            self.reviews_changed()
            return
        self.conn.execute("RELEASE chat_answer")
        self.conn.execute(
            "INSERT INTO audit_log (check_kind, item_kind, item_id, trusted, basis, packet_id, revision, probability, created_at,"
            " tier, judge) VALUES ('chat_answer', 'review', ?, ?, ?, ?, ?, NULL, ?, 'owner', ?)",
            (asked.code, decision, message.sentences[0], packet_id, revision, now, answers.RULES_VERSION),
        )
        self.stats.chat_answers += 1
        # 回答那几句自己抽出来的说法：照主人定的重算一遍（写这个窗口时它们可能已经挡上了）
        wanted = list(message.sentences)
        identities = {str(row[0]) for row in self.conn.execute(
            f"SELECT DISTINCT r.identity_key FROM relation_evidence AS e JOIN relations AS r ON r.relation_id = e.relation_id"
            f" WHERE e.sentence_id IN ({_marks(wanted)}) AND r.identity_key IS NOT NULL", wanted)}
        if identities:
            self._refresh_relations(identities, set(), now)

    def _review_keys(self, kind: str, claims: Mapping[str, object]) -> set[str]:
        """一条待确认管的那几条关系（同一条关系的键；财务投资的纠正另加判新旧的组）：回答里只有它们的说法不再挡一遍。"""
        keys: set[str] = set()
        if kind in ("frozen_conflict", "same_time", EVIDENCE_WITHDRAWN):
            relation_ids = [value for value in (claims.get(name) for name in ("frozen", "new", "relation"))
                            if isinstance(value, str) and value.startswith("R")]
            relation_ids += [value for value in claims.get("relations") or () if isinstance(value, str)]  # type: ignore[attr-defined]
            keys |= {str(row[0]) for chunk in _chunks(relation_ids) for row in self.conn.execute(
                f"SELECT identity_key FROM relations WHERE relation_id IN ({_marks(chunk)}) AND identity_key IS NOT NULL", chunk)}
        elif kind == correct.CORRECTION:
            keys |= {str(value) for value in (claims.get("identity"), claims.get("key")) if value}
        elif kind in correct.ASKED:
            keys |= {str(claims.get("identity") or claims.get("key"))}
        elif kind == MANUAL_REVIEW or (kind == SOFT_CHANGE and claims.get("item") == "relation"):
            keys |= {str(value) for value in (claims.get("key"), claims.get("old_identity")) if value}
        return keys

    def _soft_details(self, verdict: str, text: str, claims: Mapping[str, object]) -> tuple[str | None, str | None] | None:
        """软变化在对话里答的：哪天（C、D 写了一个认得出的日子才收）、等什么（B 的“等……”）、改成谁（D 要说到挡住的那一处的
        名字）。认不出、对不上的返回 None：不收，记一笔（做法第 3.4 节“哪天、等什么照话里认，认不出的不收”）。"""
        cleaned = unicodedata.normalize("NFKC", text)
        days = _SOFT_DAY.findall(cleaned)
        if not days and _DAYISH.search(cleaned):
            return None  # 像是说了哪天，可一个都没认出来（“十二月一号”“元旦以后”）：不收
        at = None
        if days:
            if verdict not in ("C", "D") or len(days) != 1:
                return None
            said = parse_aware(self._said_at(str(claims.get("sentence"))))
            at = _iso_day(days[0], said.date()) if said is not None else None
            if at is None:
                return None
        condition = None
        wait = _SOFT_WAIT.search(cleaned)
        if wait is not None:
            if verdict != "B":
                return None
            condition = re.split(r"再|就|以后|之后|了", wait.group(1))[0].strip() or None
        if verdict == "D":
            # 改成、交给谁：要说到挡住的那一处换过去的那一头（托管于是放在哪家、负责是谁），说到别家的不收（复审第 N2 条）
            target = _SOFT_TO.search(cleaned)
            names = {re.sub(r"\s+", "", name.casefold()) for name in self._changed_end_names(claims.get(str(claims.get("item"))))}
            said_to = re.sub(r"\s+", "", target.group(1).casefold()) if target is not None else ""
            if not any(len(name) >= 2 and name in said_to for name in names):
                return None
        return at, condition

    def _changed_end_names(self, item_id: object) -> set[str]:
        """挡住的那一处关系里，换的是哪一头的名字和这个范围的别名：同一时间一个的组按卡上那一头分组，另一头是换的
        （托管于换的是放在哪家，负责换的是谁）；别的关系看客体。事实看它的值。"""
        if not isinstance(item_id, str) or not item_id:
            return set()
        if not item_id.startswith("R"):
            row = self.conn.execute("SELECT value FROM facts WHERE fact_id = ?", (item_id,)).fetchone()
            return {str(row[0])} if row is not None and row[0] else set()
        row = self.conn.execute(
            "SELECT relation_type, subject_entity_id, object_entity_id, scope_partitions, scope_sensitive FROM relations"
            " WHERE relation_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return set()
        end = row["subject_entity_id"] if ONE_AT_A_TIME.get(row["relation_type"]) == "object" else row["object_entity_id"]
        return set(self._spellings(str(end), _scope_of_row(row["scope_partitions"], row["scope_sensitive"])))

    def revert_withdrawn_answers(self, now: str, *, packets: Collection[str] | None = None) -> list[int]:
        """D83：认出来的回答、问话那几句不在了（单条撤回、整包撤回；包重发、新的这一版里没了，按指纹认）：照它定的跟着撤，
        待确认重新打开（做法第 3.4 节）。冻结照定之前的样子（用新值解除了的再冻上，放回来的再藏起来），软变化的记录照定之前的
        放回去，再重算；定了留原值、两边都留以后主人自己解除了冻结的，冲突已经没了，只去掉回答的记录（第 2 段审阅第 8 条）：
        原值是主人原话说的（D204）不算，没冻结也照样重新打开（重算时已经不冲突的，组重算会删掉）；这一对后来又有过别的行的
        （解除冻结补的、重新问的），也只去掉回答的记录（审阅第 3 条）。
        认不出、只记了一笔的，那一笔去掉。packets 给了的只看问话、回答在这几个包里的。返回重新打开的编号。不自己开事务。"""
        reopened: list[int] = []
        rows = []
        for row in self.conn.execute(
            "SELECT review_id, kind, claims, answer, decision FROM review_queue WHERE answer IS NOT NULL ORDER BY review_id"
        ).fetchall():
            record = json.loads(row["answer"])
            if not isinstance(record, dict):
                continue
            prints = [one for one in (*(record.get("question_prints") or ()), *(record.get("answer_prints") or ()))
                      if isinstance(one, list) and len(one) == 2]
            if packets is not None and not {str(one[0]) for one in prints} & set(packets):
                continue
            rows.append((row, record, prints))
        now_ids = self._prints_now(prints for _, _, record_prints in rows for prints in record_prints)
        for row, record, prints in rows:
            if prints:
                alive = all(now_ids.get((str(one[0]), str(one[1]))) for one in prints)
            else:  # 以前记的没有指纹：按句子编号看
                said = [str(one) for one in (record.get("question"), *(record.get("sentences") or ())) if one]
                alive = all(self._live_sentence({"sentence": one}) is not None for one in said)
            if alive:
                continue
            if row["decision"] is None or not record.get("decision"):
                self.conn.execute("UPDATE review_queue SET answer = NULL WHERE review_id = ?", (row["review_id"],))
                self.reviews_changed()
                continue
            if row["decision"] == DROP:
                continue  # 删掉了的回不来（对话里也不收删掉，这是以前记的）
            claims = json.loads(row["claims"])
            kind = _review_type(row["kind"], claims)
            # 这一对后来又有过别的行（解除冻结补的 unfrozen、重新问的）：这个决定已经被它们顶了，再打开就是同一对两条没处理的、
            # 编号一样（审阅第 3 条）
            later = kind == "frozen_conflict" and self.conn.execute(
                "SELECT 1 FROM review_queue WHERE kind = 'frozen_conflict' AND review_id > ?"
                " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ? LIMIT 1",
                (row["review_id"], claims.get("frozen"), claims.get("new")),
            ).fetchone() is not None
            if kind == "frozen_conflict" and (later or row["decision"] != KEEP_NEW and self.conn.execute(
                "SELECT 1 FROM relations WHERE relation_id = ?1 AND frozen = 1 UNION ALL SELECT 1 FROM facts WHERE fact_id = ?1"
                " AND (frozen = 1 OR owner_said = 1)", (claims.get("frozen"),)  # 主人原话的那种（D204）没冻结，冲突也还在
            ).fetchone() is None):
                self.conn.execute("UPDATE review_queue SET answer = NULL WHERE review_id = ?", (row["review_id"],))
                self.reviews_changed()
                continue
            before = record.get("claims_before")
            claims = before if isinstance(before, dict) else claims
            self.conn.execute(
                "UPDATE review_queue SET decision = NULL, decided_at = NULL, decided_by = NULL, answer = NULL, claims = ?"
                " WHERE review_id = ?", (_json(claims), row["review_id"]))
            self.reviews_changed()
            if record.get("unfroze"):
                target = claims.get("frozen") if kind == "frozen_conflict" else claims.get("relation")
                if not self.set_frozen(str(target or record["unfroze"]), True, now):
                    self.set_frozen(str(record["unfroze"]), True, now)
            if record.get("restored"):
                restored = str(record["restored"])
                table, column = ("relations", "relation_id") if restored.startswith("R") else ("facts", "fact_id")
                self.conn.execute(f"UPDATE {table} SET withdrawn_at = ?, updated_at = ? WHERE {column} = ?", (now, now, restored))
            self._refresh_review_items(claims, now)
            self.conn.execute(
                "INSERT INTO audit_log (check_kind, item_kind, item_id, trusted, basis, packet_id, revision, probability, created_at,"
                " tier, judge) VALUES ('chat_answer', 'review', ?, 'withdrawn', ?, ?, ?, NULL, ?, 'owner', ?)",
                (str(record.get("code") or self._code_of_review(int(row["review_id"]))), str(record.get("answer")),
                 record.get("packet"), record.get("revision"), now, answers.RULES_VERSION),
            )
            reopened.append(int(row["review_id"]))
        return reopened

    def _code_of_review(self, review_id: int) -> str:
        row = self.conn.execute("SELECT code FROM review_queue WHERE review_id = ?", (review_id,)).fetchone()
        return str(row[0]) if row is not None and row[0] else ""

    def _refresh_review_items(self, claims: Mapping[str, object], now: str) -> None:
        """一条待确认说到的关系、事实、实体照现在的决定重算（撤回回答以后用；重算的和定的时候一样）。"""
        relation_ids = [value for value in (claims.get(name) for name in ("frozen", "new", "relation", "old"))
                        if isinstance(value, str) and value.startswith("R")]
        relation_ids += [value for value in claims.get("relations") or () if isinstance(value, str)]  # type: ignore[attr-defined]
        identities = {str(row[0]) for chunk in _chunks(relation_ids) for row in self.conn.execute(
            f"SELECT identity_key FROM relations WHERE relation_id IN ({_marks(chunk)}) AND identity_key IS NOT NULL", chunk)}
        identities |= {str(value) for value in (claims.get("identity"), claims.get("old_identity")) if value}
        if claims.get("item") == "relation" and claims.get("key"):
            identities.add(str(claims["key"]))
        entity = claims.get("entity_id")
        if isinstance(entity, str):
            identities |= {str(row[0]) for row in self.conn.execute(
                "SELECT identity_key FROM relations WHERE (subject_entity_id = ?1 OR object_entity_id = ?1) AND identity_key IS NOT NULL",
                (entity,))}
            for fact in self.conn.execute("SELECT fact_id FROM facts WHERE subject_entity_id = ?", (entity,)).fetchall():
                group = self._refresh_fact(fact["fact_id"], {}, set(), now)
                if group is not None:
                    self._supersede(group, now)
        self._refresh_relations(identities, set(), now)
        fact_ids = [value for value in (claims.get(name) for name in ("frozen", "new", "fact"))
                    if isinstance(value, str) and not value.startswith("R")]
        for fact_id in fact_ids:  # 进图前检查转人工的事实（J5.3）：照现在的决定重算这一条（文字、证据、状态；有组的下面再重算新旧）
            if self.conn.execute(
                "SELECT 1 FROM fact_claims WHERE fact_id = ? AND judge_hold IS NOT NULL LIMIT 1", (fact_id,)
            ).fetchone():
                self._refresh_fact(fact_id, {}, set(), now)
        for group in sorted({str(row[0]) for chunk in _chunks(fact_ids) for row in self.conn.execute(
                f"SELECT group_key FROM facts WHERE fact_id IN ({_marks(chunk)}) AND group_key IS NOT NULL", chunk)}):
            self._supersede(group, now)

    def _same_time_decided(self, one: str, other: str) -> bool:
        """同一时刻两个（R12）这一对主人定过两边都留没有（30 天没人管也算）。"""
        pair = _json(sorted((one, other)))
        return self.conn.execute(
            "SELECT 1 FROM review_queue WHERE kind = 'other' AND decided_at IS NOT NULL AND json_extract(claims, '$.reason') = ?"
            " AND json_extract(claims, '$.relations') = json(?) LIMIT 1",
            (SAME_TIME, pair),
        ).fetchone() is not None

    def _conflict_reviews(self, conflicts: Sequence[tuple[str, str, str]], now: str) -> None:
        """关系的冲突进待确认（没进过的才进）：和冻结的冲突照事实的写法（{frozen, new}）；同一时刻两个记成 other
        （{relations: [两条], reason: same_time}，主会话定的先用 other，接口文档没有更合适的原因）。"""
        for kind, one, other in conflicts:
            if kind == "frozen_conflict":
                if self.conn.execute(
                    "SELECT 1 FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL"
                    " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ?",
                    (one, other),
                ).fetchone():
                    continue
                quotes = [row[0] for row in self.conn.execute(
                    "SELECT sentence_id FROM relation_evidence WHERE relation_id = ? ORDER BY evidence_id", (other,))]
                self.conn.execute(
                    "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('frozen_conflict', ?, ?, ?)",
                    (_json({"frozen": one, "new": other}), _json(quotes), now),
                )
                self.stats.frozen_conflicts += 1
                continue
            pair = sorted((one, other))
            if self.conn.execute(
                "SELECT 1 FROM review_queue WHERE kind = 'other' AND decided_at IS NULL AND json_extract(claims, '$.reason') = ?"
                " AND json_extract(claims, '$.relations') = json(?)",
                (SAME_TIME, _json(pair)),
            ).fetchone():
                continue
            quotes = [row[0] for row in self.conn.execute(
                "SELECT sentence_id FROM relation_evidence WHERE relation_id IN (?, ?) ORDER BY evidence_id", tuple(pair))]
            self.conn.execute(
                "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('other', ?, ?, ?)",
                (_json({"relations": pair, "reason": SAME_TIME}), _json(sorted(set(quotes))), now),
            )

    def _relation_evidence(self, relation_id: str, members: Sequence[sqlite3.Row], now: str) -> None:
        """一条关系的证据：这一段几处说法的证据合起来，一句只记一次，按说法的先后（和事实一样）。记着的正好是要记的前一截
        （又说了一遍，最常见）的，只加后面新的，不删了重插（审阅第 7 条：不然一条关系说得越多，每次写账越慢）。"""
        wanted: list[tuple[sqlite3.Row, dict[str, object]]] = []
        seen: set[str] = set()
        for row in sorted(members, key=lambda one: (one["said_from_epoch"], one["claim_id"])):
            for proof in json.loads(row["proofs"]):
                if proof["sentence_id"] in seen:
                    continue
                seen.add(proof["sentence_id"])
                wanted.append((row, proof))
        have = [tuple(one) for one in self.conn.execute(
            "SELECT sentence_id, packet_id, revision, quote, position, window_id FROM relation_evidence WHERE relation_id = ?"
            " ORDER BY evidence_id",
            (relation_id,),
        )]
        keys = [(proof["sentence_id"], proof["packet_id"], proof["revision"], proof["quote"], _json(proof["position"]),
                 row["window_id"]) for row, proof in wanted]
        if have == keys[: len(have)]:
            wanted = wanted[len(have):]
        else:
            self.conn.execute("DELETE FROM relation_evidence WHERE relation_id = ?", (relation_id,))
        for row, proof in wanted:
            self.conn.execute(
                "INSERT INTO relation_evidence (relation_id, packet_id, revision, sentence_id, quote, position, window_id,"
                " source, origin, speaker_role, sentence_hash, message_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    relation_id,
                    proof["packet_id"],
                    proof["revision"],
                    proof["sentence_id"],
                    proof["quote"],
                    _json(proof["position"]),
                    row["window_id"],
                    *self._evidence_meta(str(proof["packet_id"]), int(proof["revision"]), str(proof["sentence_id"])),  # type: ignore[arg-type]
                    now,
                ),
            )

    def _relation_provenance(self, relation_id: str, *, stated: bool | None = None) -> None:
        """来源和信任，照事实（_provenance）：按这条关系的证据算，不看模型写了什么。stated 是 None 的（没有说法了）不动。"""
        rows = self.conn.execute(
            "SELECT source, origin, speaker_role FROM relation_evidence WHERE relation_id = ?", (relation_id,)
        ).fetchall()
        sources = sorted({row["source"] for row in rows if row["source"]})
        origins = sorted({row["origin"] for row in rows if row["origin"]})
        secondhand = bool(rows) and all(row["origin"] is not None and row["origin"] != ORIGIN_SAID for row in rows)
        owner_said = any(row["origin"] == ORIGIN_SAID and row["speaker_role"] == ROLE_OWNER for row in rows)
        if stated is None:
            self.conn.execute(
                "UPDATE relations SET sources = ?, origins = ?, secondhand = ?, owner_said = ? WHERE relation_id = ?",
                (_json(sources), _json(origins), int(secondhand), int(owner_said), relation_id),
            )
            return
        self.conn.execute(
            "UPDATE relations SET sources = ?, origins = ?, secondhand = ?, stated = ?, owner_said = ? WHERE relation_id = ?",
            (_json(sources), _json(origins), int(secondhand), int(stated), int(owner_said), relation_id),
        )

    def _rename_relation(self, old: str, new: str) -> None:
        """一段的编号变了：指着旧编号的替代、待确认改指新的。"""
        self.conn.execute("UPDATE relations SET superseded_by = ? WHERE superseded_by = ?", (new, old))
        for row in self.conn.execute("SELECT review_id, claims FROM review_queue").fetchall():
            claims = json.loads(row["claims"])
            if not isinstance(claims, dict):
                continue
            changed = False
            for name in ("relation", "frozen", "new"):
                if claims.get(name) == old:
                    claims[name] = new
                    changed = True
            pair = claims.get("relations")  # 同一句话里两个的（第二轮审阅 G2）
            if isinstance(pair, list) and old in pair:
                claims["relations"] = sorted(new if one == old else one for one in pair)
                changed = True
            if changed:
                self.conn.execute("UPDATE review_queue SET claims = ? WHERE review_id = ?", (_json(claims), row["review_id"]))

    def _forget_relation(self, relation_id: str) -> None:
        """删一行关系：指着它的替代清空、没处理的待确认删掉，它的说法、证据跟着删（外键 ON DELETE CASCADE）。"""
        self.conn.execute("UPDATE relations SET superseded_by = NULL, superseded_at = NULL WHERE superseded_by = ?", (relation_id,))
        self.conn.execute(
            "DELETE FROM review_queue WHERE decided_at IS NULL AND ("
            "(kind = 'frozen_conflict' AND (json_extract(claims, '$.new') = ?1 OR json_extract(claims, '$.frozen') = ?1))"
            " OR (kind IN ('other', 'soft_change', 'manual_review') AND json_extract(claims, '$.relation') = ?1)"
            " OR (kind = 'other' AND EXISTS (SELECT 1 FROM json_each(claims, '$.relations') WHERE value = ?1)))",
            (relation_id,),
        )
        self.conn.execute("DELETE FROM relation_claims WHERE relation_id = ?", (relation_id,))
        self.conn.execute("DELETE FROM relations WHERE relation_id = ?", (relation_id,))

    # ---------- 新旧 ----------

    @staticmethod
    def _statements(claims: Sequence[sqlite3.Row]) -> tuple[tuple[int, str], ...]:
        """这条事实每一句证据的说话时间。claims 是它的说法（_claims_of 读出来的）。"""
        found: dict[int, str] = {}
        for row in claims:
            for proof in json.loads(row["proofs"]):
                found.setdefault(int(proof["said_epoch"]), str(proof["said_at"]))
        return tuple(sorted(found.items()))

    @staticmethod
    def _origins(fact_id: str, claims: Sequence[sqlite3.Row], said_by: Mapping[tuple[str, str], tuple[str | None, str | None]]
                 ) -> tuple[frozenset[int], frozenset[int]]:
        """D204：这条事实的这几处说法里，哪几刻（说话时间 epoch）有主人自己说的（origin said、身份 owner），哪几刻的几句全是
        外部内容（origin 是 external、relayed、attachment_note，J2.13 的转述）。said_by 是 (事实, 句子) → (origin, 说话人身份)，
        照证据表；查不到的、origin 不知道的（旧版本的句子）不算外部。"""
        owner: set[int] = set()
        outside: dict[int, bool] = {}
        for row in claims:
            for proof in json.loads(row["proofs"]):
                origin, role = said_by.get((fact_id, str(proof["sentence_id"])), (None, None))
                moment = int(proof["said_epoch"])
                if origin == ORIGIN_SAID and role == ROLE_OWNER:
                    owner.add(moment)
                outside[moment] = outside.get(moment, True) and origin is not None and origin != ORIGIN_SAID
        return frozenset(owner), frozenset(moment for moment, yes in outside.items() if yes)

    def _switched(self, members: Sequence[GroupFact]) -> set[tuple[str, str]]:
        """D204：主人定过“照外部换”的那几对 (主人原话那条, 外部那条)：这一对最后一次的决定是用新值。组里有没冻结的主人原话、
        又有外部那一刻时才查。"""
        protected = sorted(member.fact_id for member in members if member.owner and not member.frozen)
        if not protected or not any(member.external for member in members):
            return set()
        last: dict[tuple[str, str], str] = {}
        for chunk in _chunks(protected):
            for row in self.conn.execute(
                "SELECT json_extract(claims, '$.frozen') AS frozen, json_extract(claims, '$.new') AS new, decision FROM review_queue"
                f" WHERE kind = 'frozen_conflict' AND decided_at IS NOT NULL AND json_extract(claims, '$.frozen') IN ({_marks(chunk)})"
                " ORDER BY review_id",
                chunk,
            ):
                last[(str(row["frozen"]), str(row["new"]))] = str(row["decision"])
        return {pair for pair, decision in last.items() if decision == KEEP_NEW}

    def _drop_settled(self, rows: Sequence[sqlite3.Row], asking: Collection[tuple[str, str]],
                      settled: Collection[tuple[str, str]]) -> None:
        """D204：没处理的冲突里，这一对已经不用问的删掉，和说法没了时删没处理的待确认一样：
        - 原值那条没冻结（主人原话的那种）、这一对已经不冲突了的（主人自己又说了别的、原话那一句撤回了）；
        - 主人定过、决定还算数的那几对（settled），原值冻没冻结都一样：合并实体、整本重建导回时，原值还没冻上就按主人原话
          又进了一条，冻结标记、决定搬过去以后留着它，30 天没人管会按两边都留盖掉主人定的（复审第 1 条）。
        定过的那几行都留着；原值冻结着、没定过的照旧不碰。"""
        loose = {row["fact_id"] for row in rows if not row["frozen"]}
        stale = [review["review_id"] for review in self.conn.execute(
            "SELECT review_id, json_extract(claims, '$.frozen') AS frozen, json_extract(claims, '$.new') AS new FROM review_queue"
            " WHERE kind = 'frozen_conflict' AND decided_at IS NULL"
        ).fetchall() if (review["frozen"], review["new"]) in settled
            or review["frozen"] in loose and (review["frozen"], review["new"]) not in asking]
        for review_id in stale:
            self.conn.execute("DELETE FROM review_queue WHERE review_id = ?", (review_id,))
        if stale:
            self.reviews_changed()

    def _supersede(self, group: str, now: str) -> None:
        """重算一组的新旧：按每一句算。状态也重算：先按说法，再按歧义、和冻结的冲突（冻结的，或者主人原话说的：外部内容后来
        说的不替代主人原话，D204）。

        每往一组写进一条都要重算整组，组越大越要省（收件变慢第二轮：一组几千条时，写账大半的时间花在这里）：每条事实的
        说法只读一遍；替代、状态都没变的不写回；主体有没有没处理的歧义一组只查一次（这一段只改事实表，待确认在最后才写，
        查到的不会变）。算出来的和以前一样。
        """
        rows = self.conn.execute(
            "SELECT fact_id, frozen, subject_entity_id, as_of_epoch, as_of, status, pending_reason, superseded_by, superseded_at,"
            " scope_partitions, scope_sensitive, judged_by FROM facts WHERE group_key = ? AND withdrawn_at IS NULL",
            (group,),
        ).fetchall()
        claims_of = {row["fact_id"]: self._claims_of(row["fact_id"]) for row in rows}
        frozen_facts = {row["fact_id"] for row in rows if row["frozen"]}  # 主人冻结的不挡（主人担保了）
        # 进图前检查转人工、主人还没说留下的（J5.3）：不参加新旧，也不当软变化的候选（照 J4.4 软变化挡住的那条路）
        judge_held = self._fact_holds([claim for fact_id, claims in claims_of.items() if fact_id not in frozen_facts
                                       for claim in claims])  # 冻结的是主人担保的，不挡（审阅第 2 条）
        held, parallel, judged_at = self._soft_facts({fact_id: [claim for claim in claims if claim["claim_id"] not in judge_held]
                                                     for fact_id, claims in claims_of.items()
                                                     if fact_id not in frozen_facts})  # 软变化（J4.4 第 4 段）
        # 每一句是谁说的、是不是外部内容（D204）：照证据表（写证据时从原文索引抄的），一组一句 SQL；一条的组不判新旧，不查
        said_by = {(row[0], row[1]): (row[2], row[3]) for row in self.conn.execute(
            "SELECT v.fact_id, v.sentence_id, v.origin, v.speaker_role FROM evidence AS v JOIN facts AS f ON f.fact_id = v.fact_id"
            " WHERE f.group_key = ? AND f.withdrawn_at IS NULL", (group,))} if len(rows) > 1 else {}
        members = []
        for row in rows:
            usable = [claim for claim in claims_of[row["fact_id"]] if claim["claim_id"] not in held and claim["claim_id"] not in parallel
                      and claim["claim_id"] not in judge_held]
            statements = self._statements(usable)
            owner, external = self._origins(row["fact_id"], usable, said_by)
            if not claims_of[row["fact_id"]] and row["as_of_epoch"] is not None:  # 冻结的、说法都没了的：按它最后的 as_of
                statements = ((row["as_of_epoch"], row["as_of"]),)
            if statements:
                members.append(GroupFact(row["fact_id"], statements, bool(row["frozen"]), owner, external))
        result = succession(members, self._switched(members))
        superseded = dict(result.superseded)
        for old_id, (newer, at) in list(superseded.items()):
            if (newer, at) in judged_at:  # 软变化判了结束、换新的：那一处替代旧的那一次，替代时间取判的那天
                superseded[old_id] = (newer, judged_at[(newer, at)])
        # 判了暂停、和旧的同时现行的那条：不替代别的，后来说的别的值照常替代它（第三轮审阅 S4）
        for fact_id, claims in claims_of.items():
            mine = [claim for claim in claims if claim["claim_id"] in parallel]
            if not mine or any(claim["claim_id"] not in parallel and claim["claim_id"] not in held for claim in claims):
                continue
            last = max(moment for moment, _ in self._statements(mine))
            later = min(((moment, at or "", member.fact_id) for member in members if member.fact_id != fact_id
                         for moment, at in member.statements if moment > last), default=None)
            if later is not None:
                superseded[fact_id] = (later[2], later[1])
        held_only = {fact_id for fact_id, claims in claims_of.items()
                     if claims and all(claim["claim_id"] in held or claim["claim_id"] in judge_held for claim in claims)}
        decisions = {pair: self._frozen_decision(*pair) for pair in result.conflicts}
        for (frozen, newer), decided in decisions.items():
            if decided is not None and decided[0] == KEEP_FROZEN and newer not in superseded:
                superseded[newer] = (frozen, decided[1])  # 主人留原值：新说法算被冻结的（主人原话的）那条替代了
            # 两边都留（主人定的，或者 30 天没人处理）、别的决定：新说法照常现行，不再进待确认
        # 还没定的那几对：新说法待确认，进待确认队列（按这一对算，别的一对定过了的不再进）。原值是主人原话的、它自己又被主人
        # 定了留冻结的那条替代了的：已经不现行，和外部的那几对不问（D204）
        asking = [(frozen, newer) for (frozen, newer), decided in decisions.items()
                  if decided is None and (frozen in frozen_facts or frozen not in superseded)]
        conflicted = {newer for _, newer in asking}
        known: dict[tuple[str, str], bool] = {}
        for row in rows:
            fact_id = row["fact_id"]
            # 转人工、主人还没说留下的那几处不给状态（和 _refresh_fact 一样；只剩这种说法的照全部算，第二遍审阅第 4 条）
            counted = [claim for claim in claims_of[fact_id] if claim["claim_id"] not in judge_held] or claims_of[fact_id]
            claims = [self._view(claim) for claim in counted]
            if claims:
                scope = _scope_of_row(row["scope_partitions"], row["scope_sensitive"])
                status, reason = self._status(claims, row["subject_entity_id"], set(), scope, known)
            else:  # 冻结的、说法都没了的：状态是主人定的时候的样子，不动
                status, reason = row["status"], row["pending_reason"]
            if fact_id in conflicted and status != "pending":
                status, reason = "pending", "frozen_conflict"
            if fact_id in held_only and status != "pending":  # 只有软变化先挡住的说法：待确认，判了再照结论
                # 有进图前检查转人工的（J5.3）：等主人（manual_review）
                mine = claims_of[fact_id]
                status, reason = "pending", (MANUAL_REVIEW if any(claim["claim_id"] in judge_held for claim in mine)
                                             else soft_rules.REVIEW_KIND)
            judged = row["judged_by"]  # 谁认的（J5.3）：照说法、冻结重算；没有说法又没冻结的（碰不上）照旧
            if claims_of[fact_id] or row["frozen"]:
                judged = self._judged_of(bool(row["frozen"]), claims_of[fact_id], judge_held)
            by, at = superseded.get(fact_id, (None, None))
            if (by, at, status, reason, judged) == (row["superseded_by"], row["superseded_at"], row["status"], row["pending_reason"],
                                                    row["judged_by"]):
                continue  # 都没变，不写回（rows 是这一组开头读的，这一段只改各自那一行，读到的就是现在的）
            self.conn.execute(
                "UPDATE facts SET superseded_by = ?, superseded_at = ?, status = ?, pending_reason = ?, judged_by = ? WHERE fact_id = ?",
                (by, at, status, reason, judged, fact_id),
            )
        if len(rows) > 1:
            self._drop_settled(rows, set(asking), {pair for pair, decided in decisions.items() if decided is not None})
        for frozen, newer in asking:  # 主人定过了的（留原值、两边都留）不在这里，不再进
            if self.conn.execute(  # 同一对还没定的进过一次就不再进
                "SELECT 1 FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NULL"
                " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ?",
                (frozen, newer),
            ).fetchone():
                continue
            quotes = [row["sentence_id"] for row in self.conn.execute("SELECT sentence_id FROM evidence WHERE fact_id = ?", (newer,))]
            self.conn.execute(
                "INSERT INTO review_queue (kind, claims, quotes, created_at) VALUES ('frozen_conflict', ?, ?, ?)",
                (_json({"frozen": frozen, "new": newer}), _json(quotes), now),
            )
            self.stats.frozen_conflicts += 1
        for claim_id, (sentence, fact_id) in sorted(held.items()):  # 软变化先挡住的那几处进待确认（J4.4 第 4 段）
            self._soft_review("fact", fact_id, claim_id, sentence, fact_id, now)
        self._unhold(SOFT_CHANGE, "fact", [row["fact_id"] for row in rows], set(held.values()))

    def _is_frozen(self, item_id: object) -> bool:
        """这条事实或者关系现在冻结着没有。"""
        return self.conn.execute(
            "SELECT 1 FROM relations WHERE relation_id = ?1 AND frozen = 1 UNION ALL SELECT 1 FROM facts WHERE fact_id = ?1 AND frozen = 1",
            (item_id,),
        ).fetchone() is not None

    def _frozen_decision(self, frozen: str, newer: str) -> tuple[str, str] | None:
        """这一对冻结冲突定过没有：最后一次的 (决定, 定的时间)。用新值、解除冻结（keep_new、unfrozen）的不算：
        那时它已经不冻结了，现在又冻结了，冲突要重新问。别的决定都算定过了。"""
        row = self.conn.execute(
            "SELECT decision, decided_at FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NOT NULL"
            " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ? ORDER BY review_id DESC LIMIT 1",
            (frozen, newer),
        ).fetchone()
        if row is None or row["decision"] in (KEEP_NEW, UNFROZEN):
            return None
        return (row["decision"], row["decided_at"])

    # ---------- 冻结和待确认（javmem.review） ----------

    def set_frozen(self, fact_id: str, frozen: bool, now: str) -> bool:
        """冻结或者解除冻结（见 _set_frozen）；新进的待确认当时就记下编号（D83）。"""
        done = self._set_frozen(fact_id, frozen, now)
        self.fill_codes()
        return done

    def _set_frozen(self, fact_id: str, frozen: bool, now: str) -> bool:
        """冻结或者解除冻结（D12：主人用管理命令手动标，模块不自动冻结）。组重算：冻结的不被替代，比它晚说的别的值
        进待确认；解除冻结以后照常按新旧，和它有关的没处理的冲突记成 unfrozen 关掉。说法都没了的事实解除冻结以后
        没有东西撑着，删掉。不自己开事务。返回 False 是没有这条事实。"""
        relation = self.conn.execute("SELECT identity_key FROM relations WHERE relation_id = ?", (fact_id,)).fetchone()
        if relation is not None:  # 关系（R 开头，J4.4）
            return self._set_frozen_relation(fact_id, relation["identity_key"], frozen, now)
        row = self.conn.execute("SELECT group_key FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
        if row is None:
            return False
        self.conn.execute("UPDATE facts SET frozen = ?, updated_at = ? WHERE fact_id = ?", (int(frozen), now, fact_id))
        self._freeze_reviews(fact_id, frozen, now)
        claims = self._claims_of(fact_id)
        if not frozen and not claims:
            touched = self._fact_entities(fact_id)
            self._forget_fact(fact_id)
            self._drop_orphan_entities(touched)
        elif any(row["judge_hold"] for row in claims):
            # 有进图前检查转人工的说法（J5.3）：冻结的是主人担保的，放出来、那条待确认关掉；解除冻结又挡着、再问（审阅第 2 条）
            self._refresh_fact(fact_id, {}, set(), now)
        else:  # 谁认的（J5.3）：冻结的是主人，解除冻结的照说法
            self.conn.execute("UPDATE facts SET judged_by = ? WHERE fact_id = ?", (self._judged_of(frozen, claims, {}), fact_id))
        if row["group_key"] is not None:
            self._supersede(row["group_key"], now)
        return True

    def _set_frozen_relation(self, relation_id: str, identity: str | None, frozen: bool, now: str) -> bool:
        """冻结、解除冻结一条关系（J4.4），和事实一样：冻结的不被别的替代、说法没了也留着；解除冻结以后照常，说法都没了的
        删掉。和它有关的冲突照事实的办法关、补。"""
        # 谁认的（J5.3）：冻结的是主人（没有说法的冻结关系重算时不改这一行）；解除冻结的重算时照说法
        self.conn.execute("UPDATE relations SET frozen = ?, updated_at = ?, judged_by = CASE WHEN ? = 1 THEN 'owner' ELSE judged_by END"
                          " WHERE relation_id = ?", (int(frozen), now, int(frozen), relation_id))
        self._freeze_reviews(relation_id, frozen, now)
        family = self._family_of(relation_id) or ({identity} if identity else set())
        if not frozen and self.conn.execute("SELECT 1 FROM relation_claims WHERE relation_id = ?", (relation_id,)).fetchone() is None:
            row = self.conn.execute(
                "SELECT subject_entity_id, object_entity_id, org_entity_id FROM relations WHERE relation_id = ?", (relation_id,)
            ).fetchone()
            self._forget_relation(relation_id)
            self._drop_orphan_entities(_relation_claim_entities(row))
        self._refresh_relations(family, set(), now)  # 同组一起重算（删掉的那条顶掉的、和它冲突的，第二轮审阅 S2）
        return True

    def _freeze_reviews(self, fact_id: str, frozen: bool, now: str) -> None:
        """冻结、解除冻结时待确认里跟着关的、补的（事实、关系一样：冲突记的是两边的编号）。"""
        if frozen:  # 它是别的冻结事实的“新说法”的话：两条主人都冻结了，那条冲突算两边都留
            # 记一笔是冻结它时自动关掉的（和主人自己定的“两边都留”分开），解除冻结它时这条要重新问（D73 第 19 条）
            self.conn.execute(
                "UPDATE review_queue SET decision = ?, decided_at = ?, decided_by = 'owner',"
                f" claims = json_set(claims, '$.{CLOSED_BY_FREEZE}', json('true'))"
                " WHERE kind = 'frozen_conflict' AND decided_at IS NULL AND json_extract(claims, '$.new') = ?",
                (KEEP_BOTH, now, fact_id),
            )
        if not frozen:
            # 冻结它时自动关掉的冲突（它是新说法那一方）：它不冻结了，补一行 unfrozen，冲突重新问
            olders = {
                str(row[0])
                for row in self.conn.execute(
                    "SELECT json_extract(claims, '$.frozen') FROM review_queue WHERE kind = 'frozen_conflict'"
                    " AND json_extract(claims, '$.new') = ?",
                    (fact_id,),
                )
            }
            for older in sorted(olders):
                last = self.conn.execute(
                    "SELECT claims FROM review_queue WHERE kind = 'frozen_conflict' AND decided_at IS NOT NULL"
                    " AND json_extract(claims, '$.frozen') = ? AND json_extract(claims, '$.new') = ?"
                    " ORDER BY review_id DESC LIMIT 1",
                    (older, fact_id),
                ).fetchone()
                if last is not None and json.loads(last["claims"]).get(CLOSED_BY_FREEZE):
                    self.conn.execute(
                        "INSERT INTO review_queue (kind, claims, quotes, decision, decided_at, decided_by, created_at)"
                        " VALUES ('frozen_conflict', ?, '[]', ?, ?, 'owner', ?)",
                        (_json({"frozen": older, "new": fact_id}), UNFROZEN, now, now),
                    )
            self.conn.execute(
                "UPDATE review_queue SET decision = ?, decided_at = ?, decided_by = 'owner' WHERE kind = 'frozen_conflict'"
                " AND decided_at IS NULL AND json_extract(claims, '$.frozen') = ?",
                (UNFROZEN, now, fact_id),
            )
            # 以前定过的（留原值、两边都留）也补一行：它不冻结了，以前的决定不再算数，再冻结时冲突重新问
            pairs = {
                str(row[0])
                for row in self.conn.execute(
                    "SELECT json_extract(claims, '$.new') FROM review_queue WHERE kind = 'frozen_conflict'"
                    " AND json_extract(claims, '$.frozen') = ?",
                    (fact_id,),
                )
            }
            for newer in sorted(pairs):
                if self._frozen_decision(fact_id, newer) is not None:
                    self.conn.execute(
                        "INSERT INTO review_queue (kind, claims, quotes, decision, decided_at, decided_by, created_at)"
                        " VALUES ('frozen_conflict', ?, '[]', ?, ?, 'owner', ?)",
                        (_json({"frozen": fact_id, "new": newer}), UNFROZEN, now, now),
                    )

    def _fact_entities(self, fact_id: str) -> set[str]:
        found = {row[0] for row in self.conn.execute("SELECT entity_id FROM fact_entities WHERE fact_id = ?", (fact_id,))}
        subject = self.conn.execute("SELECT subject_entity_id FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
        if subject is not None and subject[0] is not None:
            found.add(subject[0])
        return found

    def decide_review(
        self, review_id: int, decision: str, now: str, *, by: str = "owner", at: str | None = None, condition: str | None = None
    ) -> None:
        """处理一条待确认（见 _decide_review）；定了以后重算新进的待确认，当时就记下编号（D83，第 2 段审阅第 12 条）。"""
        self._decide_review(review_id, decision, now, by=by, at=at, condition=condition)
        self.fill_codes()

    def _decide_review(
        self, review_id: int, decision: str, now: str, *, by: str = "owner", at: str | None = None, condition: str | None = None
    ) -> None:
        """处理一条待确认。不自己开事务。不认识的组合抛 ValueError，什么都不动。

        - 和冻结的冲突：留原值（keep_frozen，新说法算被冻结的那条替代了）、用新值（keep_new，解除冻结，照常按新旧）、
          两边都留（keep_both，新说法也现行）。原值是主人原话说的那种（D204，没冻结）一样，只是用新值不碰冻结：这一对
          记着照外部换，重算时外部说的那一刻替代原话那条。
        - 实体有歧义：两边都留（keep_both，新实体单独算，不并到候选上）。合并是认人认事那一步（J3.6）的事。
        - 证据都撤回了的冻结事实：留着（keep_frozen，放回来，没有证据也照常看得到，主人担保）、删掉（drop）；
          没人处理时一直藏着（keep_hidden）。
        """
        row = self.conn.execute("SELECT kind, claims, decided_at FROM review_queue WHERE review_id = ?", (review_id,)).fetchone()
        if row is None or row["decided_at"] is not None:
            raise ValueError("没有这条待确认，或者已经定过了")
        kind, claims = row["kind"], json.loads(row["claims"])
        if kind == SOFT_CHANGE and by == "auto" and decision == soft_rules.EXPIRE_ACTION:  # 30 天没人管（D151）
            self._decide_soft(review_id, claims, soft_rules.Action(soft_rules.KEEP), now, by=by, tier="rule",
                              judge="D151 30 天没人管", verdict=soft_rules.KEEP)
            return
        if kind == SOFT_CHANGE and decision in SOFT_VERDICTS:  # 主人答了六选一（A 到 E），照 rules.action 执行
            from javmem.soft.judge import normal_date  # judge 要用 extract，放这里免得一开始就互相导入

            said_at = self._said_at(str(claims.get("sentence")))
            precision = "unknown"
            if at is not None:  # 和模型写的一样要认得出（第三轮审阅 N6）
                normal = normal_date(at, said_at)
                if normal is None:
                    raise ValueError(f"认不出是哪天：{at}（写成 2026-11-20、2026-11、2026-Q4、2026 这样）")
                at, precision = normal
            item = {"verdict": decision, "about": True, "sure": True, "quote_ok": True, "consistent": True, "date_ok": True,
                    "effective_at": at, "effective_precision": precision, "condition": condition}
            action = soft_rules.action(item, said_at=said_at)
            self._decide_soft(review_id, claims, action, now, by=by, tier="owner", judge="owner", verdict=decision)
            return
        allowed = _DECISIONS.get(_review_type(kind, claims), ())
        if decision not in allowed:
            raise ValueError(f"这条待确认只能定成：{'、'.join(allowed) or '还不能用命令处理'}")
        self.conn.execute(
            "UPDATE review_queue SET decision = ?, decided_at = ?, decided_by = ? WHERE review_id = ?",
            (decision, now, by, review_id),
        )
        self.reviews_changed()
        if kind == MANUAL_REVIEW and (claims.get("item") == "fact" or claims.get("precheck")):
            self._review_audit(claims, decision, now, by)  # 进图前检查转人工的（J5.3）：主人定的、30 天没人管的留一笔
        if kind == "frozen_conflict":
            if decision == KEEP_NEW and self._is_frozen(claims["frozen"]):
                self.set_frozen(str(claims["frozen"]), False, now)
            # 原值是主人原话的那种（D204）本来就没冻结：不碰冻结，这一对记着用新值，重算时外部那一刻替代原话那条
            for fact_id in (claims["frozen"], claims["new"]):
                group = self.conn.execute("SELECT group_key FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
                if group is not None and group[0] is not None:
                    self._supersede(group[0], now)
            self._refresh_relations(  # 冲突的两边是关系的（J4.4）：照决定重算
                [row[0] for row in self.conn.execute(
                    "SELECT identity_key FROM relations WHERE relation_id IN (?, ?)", (claims["frozen"], claims["new"]))],
                set(), now,
            )
        elif kind == "ambiguous_entity":
            groups = set()
            for fact in self.conn.execute(
                "SELECT fact_id FROM facts WHERE subject_entity_id = ? ORDER BY fact_id", (claims["entity_id"],)
            ).fetchall():
                group = self._refresh_fact(fact["fact_id"], {}, set(), now)
                if group is not None:
                    groups.add(group)
            for group in sorted(groups):
                self._supersede(group, now)
            self._refresh_relations(  # 一头是这个实体的关系（J4.4）
                [row[0] for row in self.conn.execute(
                    "SELECT identity_key FROM relations WHERE subject_entity_id = ?1"
                    " UNION SELECT identity_key FROM relations WHERE object_entity_id = ?1",
                    (claims["entity_id"],),
                )],
                set(), now,
            )
        elif kind == MANUAL_REVIEW and claims.get("item") == "fact":
            # 进图前检查转人工的事实（J5.3）：留下照常算（算主人认的）；不要就删掉这一处（以后重写窗口照旧进隔离表）；搁着的照旧挡着
            if decision == DROP:
                self.conn.execute("DELETE FROM fact_claims WHERE claim_id = ? AND judge_hold IS NOT NULL", (claims.get("claim"),))
            group = self._refresh_fact(str(claims.get("fact")), {}, set(), now)
            if group is not None:
                self._supersede(group, now)
        elif kind == MANUAL_REVIEW:  # 判断 C 拿不准的关系（J4.4）：留下照常算；不要就删掉这一处（以后重写窗口照旧进隔离表）
            if decision == DROP:
                self.conn.execute("DELETE FROM relation_claims WHERE claim_id = ? AND judge_hold IS NOT NULL", (claims.get("claim"),))
            if claims.get("reason") in correct.ASKED and decision == KEEP_NEW:
                # 主人认了“抽取没抽出来的纠正”、说了旧的那一条不算了（J4.6）：冻结着的也照主人说的，先解除冻结，重算时照它
                # 结束那一条
                frozen = self.conn.execute("SELECT relation_id FROM relations WHERE relation_id = ? AND frozen = 1",
                                           (claims.get("relation"),)).fetchone()
                if frozen is not None:
                    self.set_frozen(str(frozen[0]), False, now)
            # 纠正挡住的（J4.6）键是判新旧的组，按同一条关系的键（identity）重算
            self._refresh_relations([str(claims.get("identity") or claims.get("key"))], set(), now)
        elif _review_type(kind, claims) == SAME_TIME:  # 同一时刻两个的关系（J4.4）：两边都留，照常重算
            self._refresh_relations(
                [row[0] for row in self.conn.execute(
                    "SELECT identity_key FROM relations WHERE relation_id IN (SELECT value FROM json_each(?))",
                    (_json(list(claims.get("relations") or [])),))],
                set(), now,
            )
        elif "relation" in claims:  # 证据都撤回了的冻结关系（J4.4），照下面事实的办法
            relation_id = str(claims["relation"])
            row = self.conn.execute(
                "SELECT identity_key, subject_entity_id, object_entity_id, org_entity_id FROM relations WHERE relation_id = ?",
                (relation_id,),
            ).fetchone()
            if row is None:
                return
            has_claims = self.conn.execute("SELECT 1 FROM relation_claims WHERE relation_id = ?", (relation_id,)).fetchone()
            if decision == DROP and has_claims is not None:
                raise ValueError("这条关系又有了别的包的说法，已经放回来了，不删")
            if decision == KEEP_FROZEN:
                self.conn.execute("UPDATE relations SET withdrawn_at = NULL, updated_at = ? WHERE relation_id = ?", (now, relation_id))
                # 放回来了，照冻结的算一遍：藏着的时候别人说的结束进待确认，又说起它的接上（审阅第 3 条）
                self._refresh_relations([row["identity_key"]], set(), now)
            elif decision == DROP:
                self._forget_relation(relation_id)
                self._drop_orphan_entities(_relation_claim_entities(row))
        else:  # 证据都撤回了的冻结事实
            fact_id = str(claims["fact"])
            group = self.conn.execute("SELECT group_key FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
            if group is None:
                return
            if decision == DROP and self._claims_of(fact_id):
                raise ValueError("这条事实又有了别的包的说法，已经放回来了，不删")
            if decision == KEEP_FROZEN:
                self.conn.execute("UPDATE facts SET withdrawn_at = NULL, updated_at = ? WHERE fact_id = ?", (now, fact_id))
            elif decision == DROP:
                touched = self._fact_entities(fact_id)
                self._forget_fact(fact_id)
                self._drop_orphan_entities(touched)
            if group[0] is not None and decision != KEEP_HIDDEN:
                self._supersede(group[0], now)

    def _review_audit(self, claims: Mapping[str, object], decision: str, now: str, by: str) -> None:
        """主人定进图前检查转人工的那一条（J5.3）、30 天没人管关掉的，写一行留痕：check_kind 是 precheck，tier 是 owner（没人管的
        是 rule），trusted 是 keep（留下）、drop（不要）、hidden（搁着）。item_id 和判的那几行对得上：事实是说法编号，关系是
        “窗口编号#L3 键前 12 位#R第几条”。"""
        claim = str(claims.get("claim") or "")
        if claims.get("item") == "fact":
            item_kind, item_id = "fact", claim
        else:
            head, _, index = claim.rpartition("#")
            item_kind, item_id = "relation", f"{head}#R{index}"
        sentence = claims.get("sentence")
        where = self.conn.execute(
            "SELECT packet_id, revision FROM raw_sentences WHERE sentence_id = ? ORDER BY revision DESC LIMIT 1", (sentence,)
        ).fetchone()
        tier, judge = ("owner", "owner") if by == "owner" else ("rule", "30 天没人管")
        precheck.insert_audit(self.conn, [(
            precheck.HOLD_KIND, item_kind, item_id, REVIEW_TRUSTED.get(decision, decision),
            _json([sentence]) if sentence else None, where[0] if where else None, where[1] if where else None, None, now, tier,
            judge,
        )])

    def _said_at(self, sentence_id: str) -> str:
        row = self.conn.execute("SELECT said_at FROM raw_sentences WHERE sentence_id = ? ORDER BY revision DESC LIMIT 1",
                                (sentence_id,)).fetchone()
        return str(row[0]) if row is not None and row[0] else ""

    def _soft_case(self, sentence_id: str) -> object | None:
        """这一句的软变化题（javmem.soft.find，只读账本）。出不来（没有旧的、句子撤回了）是 None。"""
        from javmem.soft.find import find_cases  # find 要用 merge、verify，放这里免得一开始就互相导入

        row = self.conn.execute("SELECT packet_id FROM raw_sentences WHERE sentence_id = ? LIMIT 1", (sentence_id,)).fetchone()
        if row is None:
            return None
        return next((case for case in find_cases(self.conn, packets=[str(row[0])]).cases
                     if case.meta.get("sentence_id") == sentence_id), None)

    def _soft_old(self, case: object | None, claims: Mapping[str, object]) -> object | None:
        """题里哪一条旧的是挡住的这一处要改的：按组配上的、和它同一组的那一条（javmem.soft.find 的第 1 步）。"""
        if case is None:
            return None
        item = str(claims.get("item"))
        table, column = ("relations", "relation_id") if item == "relation" else ("facts", "fact_id")
        mine = self.conn.execute(f"SELECT group_key FROM {table} WHERE {column} = ?", (claims.get(item),)).fetchone()
        how = case.meta.get("olds_by") or {}  # type: ignore[attr-defined]
        for old in case.old:  # type: ignore[attr-defined]
            if how.get(old.ref) != "group" or old.kind != item or old.id is None:  # type: ignore[union-attr]
                continue
            theirs = self.conn.execute(f"SELECT group_key FROM {table} WHERE {column} = ?", (old.id,)).fetchone()
            if mine is not None and theirs is not None and theirs[0] == mine[0]:
                return old
        return None

    def _decide_soft(
        self, review_id: int, claims: Mapping[str, object], action: soft_rules.Action, now: str, *, by: str, tier: str,
        judge: str, verdict: str, old: object | None = None, case: object | None = None,
    ) -> None:
        """照一个结论处理一条软变化待确认（J4.4 第 4 段）：记进这条待确认（结论、动作、改的是哪一条旧的、题号），
        写一行 audit_log，再重算挡住的那一处和旧的那一条。要主人看的（F、没把握、引文对不上……）记下模型的建议，
        接着待确认；别的定下来（decision 是动作：keep、pause、end、replace）。以后重写窗口、整本重建都照这条记下的办，
        不再找 soft.db。"""
        if case is None and tier == "owner":  # 主人答的：照这一句现在出的题找旧的那一条
            case = self._soft_case(str(claims.get("sentence")))
        if old is None and tier == "owner":
            old = self._soft_old(case, claims)
        item = str(claims.get("item"))
        old_id = getattr(old, "id", None)
        old_identity = None
        if old_id is not None and item == "relation":
            found = self.conn.execute("SELECT identity_key FROM relations WHERE relation_id = ?", (old_id,)).fetchone()
            old_identity = found[0] if found is not None else None
        record = {**claims, "verdict": verdict, "action": action.as_dict(), "old": old_id, "old_identity": old_identity,
                  "case": getattr(case, "id", None), "judge": judge}
        record.pop("not_done", None)
        if (action.kind in (soft_rules.PAUSE, soft_rules.END) and item == "relation" and old_identity
                and old_identity != claims.get("key")):  # 判给同一组别的那一条的：它冻结着就不改（第三轮审阅小的第 6 条）
            target = self.conn.execute("SELECT frozen, state, superseded_by FROM relations WHERE relation_id = ?", (old_id,)).fetchone()
            if target is not None and target["frozen"] and (target["state"] != "ended" or target["superseded_by"] is not None):
                record["not_done"] = "frozen"
        if action.kind == soft_rules.REVIEW:
            record.update(suggestion=action.suggestion, reasons=list(action.reasons))
            self.conn.execute("UPDATE review_queue SET claims = ? WHERE review_id = ?", (_json(record), review_id))
        else:
            record.pop("reasons", None)
            self.conn.execute(
                "UPDATE review_queue SET claims = ?, decision = ?, decided_at = ?, decided_by = ? WHERE review_id = ?",
                (_json(record), action.kind, now, by, review_id),
            )
        sentence = str(claims.get("sentence"))
        where = self.conn.execute(
            "SELECT packet_id, revision FROM raw_sentences WHERE sentence_id = ? ORDER BY revision DESC LIMIT 1", (sentence,)
        ).fetchone()
        self.conn.execute(
            "INSERT INTO audit_log (check_kind, item_kind, item_id, trusted, basis, packet_id, revision, probability, created_at,"
            " tier, judge) VALUES ('soft_change', ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (item if old_id is None else str(getattr(old, "kind", item)), str(old_id or claims.get(item)),
             f"{verdict} {action.kind}" + ("（冻结着，没执行）" if record.get("not_done") else ""), sentence,
             where[0] if where else None, where[1] if where else None, now, tier, judge),
        )
        if action.kind != soft_rules.REVIEW:
            self.stats.soft_applied += 1
        if item == "relation":
            held = self.conn.execute("SELECT identity_key FROM relations WHERE relation_id = ?", (claims.get("relation"),)).fetchone()
            self._refresh_relations([held[0] if held is not None else None, old_identity, str(claims.get("key"))], set(), now)
        else:
            groups = {row[0] for row in self.conn.execute(
                "SELECT group_key FROM facts WHERE fact_id IN (?, ?) AND group_key IS NOT NULL", (claims.get("fact"), old_id))}
            for group in sorted(groups):
                self._supersede(group, now)

    def apply_soft_verdicts(self, soft_dir: Path, now: str, *, model: str | None = None) -> int:
        """写账最后一步（J4.4 第 4 段）：没处理的软变化待确认，按题号（javmem.soft.find 出的题：句子加旧的编号算的）
        到 soft.db 找结论（javmem.soft.judge.stored_items），照 javmem.soft.rules.action 执行（_decide_soft：
        decided_by 是 auto，表上只有 owner、auto 两种；judge 记下模型和提示词版本，audit_log 的 tier 是 model）。soft.db 没有、题出不来、还没判的不动：旧的照旧。返回照结论
        处理了几条（要主人看的也算）。model 默认照 JAVMEM_SOFT_*（javmem.soft.claude.SoftSettings）。不自己开事务。"""
        from javmem.soft.claude import SoftSettings
        from javmem.soft.find import find_cases
        from javmem.soft.judge import PROMPT_VERSION, STORE_FILE, store_signature, stored_items

        path = soft_dir / STORE_FILE
        if not path.exists():
            return 0
        name = model or SoftSettings.from_env().cache_name
        signature = store_signature(path, name)
        last = self.conn.execute("SELECT signature FROM soft_apply WHERE id = 1").fetchone()
        if signature is None or (last is not None and last[0] == signature):
            return 0  # 没有结论，或者上一次看过以后 soft.db 没变：不再出一遍题（出题要读整本账，第三轮审阅小的第 3 条）
        reviews = []
        for row in self.conn.execute(
            "SELECT review_id, claims FROM review_queue WHERE kind = 'soft_change' AND decided_at IS NULL ORDER BY review_id"
        ).fetchall():
            claims = json.loads(row["claims"])
            if isinstance(claims, dict) and claims.get("sentence") and claims.get("judge") is None:  # 模型判过要主人看的不再找
                reviews.append((int(row["review_id"]), claims))
        if not reviews:
            return 0
        sentences = sorted({str(claims["sentence"]) for _, claims in reviews})
        packets = {str(row[0]) for chunk in _chunks(sentences) for row in self.conn.execute(
            f"SELECT DISTINCT packet_id FROM raw_sentences WHERE sentence_id IN ({_marks(chunk)})", chunk)}
        cases: dict[str, object] = {}
        for case in find_cases(self.conn, packets=sorted(packets)).cases:
            cases.setdefault(str(case.meta.get("sentence_id")), case)
        done = 0
        for review_id, claims in reviews:
            case = cases.get(str(claims["sentence"]))
            if case is None:
                continue
            items = stored_items(path, str(case.id), name)  # type: ignore[attr-defined]
            old = self._soft_old(case, claims)
            item = next((one for one in items or [] if old is not None and one.get("target") == old.ref), None)  # type: ignore[attr-defined]
            if item is None:
                continue  # 还没判（没调、调失败、不能发）：照“还没判”办，旧的照旧
            action = soft_rules.action(item, said_at=str(case.said_at))  # type: ignore[attr-defined]
            if not self.conn.execute("SELECT 1 FROM review_queue WHERE review_id = ? AND decided_at IS NULL", (review_id,)).fetchone():
                continue  # 前面执行的时候重算，这一条已经没了
            # decided_by 照 0001 的取值写 auto（不是主人定的），谁判的看 claims 里的 judge、audit_log 的 tier
            self._decide_soft(review_id, claims, action, now, by="auto", tier="model", judge=f"{name} {PROMPT_VERSION}",
                              verdict=str(item.get("verdict")), old=old, case=case)
            done += 1
        self.conn.execute(
            "INSERT INTO soft_apply (id, signature, applied_at) VALUES (1, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET signature = excluded.signature, applied_at = excluded.applied_at",
            (signature, now),
        )
        return done

    def expire_reviews(self, now: str, before: str) -> list[int]:
        """D6：没处理的待确认，建了 30 天（before 以前建的）还没人管的，按“两边都留”关掉（decided_by 是 auto）：
        和冻结的冲突、实体有歧义的两边都留；证据都撤回了的冻结事实接着藏着，不删也不放回来。还没有命令能处理的那几种
        不动。不自己开事务。返回关掉的编号。"""
        cutoff = parse_aware(before)
        closed = []
        for row in self.conn.execute(
            "SELECT review_id, kind, claims, created_at FROM review_queue WHERE decided_at IS NULL ORDER BY review_id"
        ).fetchall():
            created = parse_aware(row["created_at"])
            if created is None or cutoff is None or created >= cutoff:
                continue
            kind = _review_type(row["kind"], json.loads(row["claims"]))
            decision = _EXPIRE.get(kind)
            if decision is None:
                continue
            if not self.conn.execute("SELECT 1 FROM review_queue WHERE review_id = ? AND decided_at IS NULL", (row["review_id"],)).fetchone():
                continue  # 前面关的时候重算，这一条已经删了（同一对两条没处理的，D204 终查第 1 条）
            self.decide_review(int(row["review_id"]), decision, now, by="auto")
            closed.append(int(row["review_id"]))
        return closed

    # ---------- 隔离 ----------

    def _quarantine(self, item: Checked, now: str) -> None:
        check = item.check
        assert check is not None
        window = item.window
        revisions = dict(window.packets)
        first_target = window.targets[0]
        rows: list[tuple[object, ...]] = []

        def where(evidence: Iterable[Mapping[str, object]]) -> tuple[str, int]:
            for proof in evidence:
                packet_id = proof.get("packet_id")
                if isinstance(packet_id, str) and packet_id in revisions:
                    return packet_id, revisions[packet_id]
            return first_target.packet_id, first_target.revision

        for fact in check.facts:
            raw_evidence = fact.raw.get("evidence")
            evidence = [proof for proof in raw_evidence if isinstance(proof, Mapping)] if isinstance(raw_evidence, list) else []
            # 进图前检查转人工、写成待确认的事实（J5.3）不进隔离表；L4 去掉的证据照过了的事实一样记
            if not fact.passed and (window.window_id, fact.index) not in self.moved_facts:
                packet_id, revision = where(evidence)
                codes = [finding.as_text() for finding in fact.failures]
                payload = {
                    "window_id": window.window_id,
                    "index": fact.index,
                    "fact": fact.raw,
                    "failures": codes,
                    "notes": fact.notes,
                    "dropped": [{"evidence": proof, "finding": finding.as_text()} for proof, finding in fact.dropped],
                }
                if fact.recheck is not None:  # 模型复核判的，和它说的问题
                    payload["recheck"] = dict(fact.recheck)
                if fact.judge is not None:  # 进图前检查判的经过（J5.3）：P、哪一档定的、第二档的回答和理由、发过去的每一行
                    payload["judge"] = fact.judge
                    payload["proofs"] = [
                        {"sentence_id": proof.sentence_id, "packet_id": proof.packet_id, "revision": proof.revision}
                        for proof in fact.proofs
                    ]
                rows.append((packet_id, revision, "fact", _json(payload), ",".join(codes), fact.first_rule, self.checked_by))
                self.stats.quarantined_facts += 1
                for rule in {finding.rule for finding in fact.failures}:
                    self.stats.by_rule[rule] = self.stats.by_rule.get(rule, 0) + 1
                continue
            for proof, finding in fact.dropped:
                packet_id, revision = where([proof])
                payload = {"window_id": window.window_id, "index": fact.index, "fact_text": fact.raw.get("text"), "evidence": proof}
                rows.append((packet_id, revision, "evidence", _json(payload), finding.as_text(), finding.rule, self.checked_by))
                self.stats.quarantined_evidence += 1
        for entity, finding in check.bad_entities:
            payload = {"window_id": window.window_id, "entity": entity}
            rows.append(
                (first_target.packet_id, first_target.revision, "entity", _json(payload), finding.as_text(), finding.rule, self.checked_by)
            )
            self.stats.quarantined_entities += 1
        for relation in check.relations:  # J4.3：没过的关系、过了的关系上去掉的证据（过了的关系 J4.4 再写）
            raw_evidence = relation.raw.get("evidence")
            evidence = [proof for proof in raw_evidence if isinstance(proof, Mapping)] if isinstance(raw_evidence, list) else []
            if not relation.passed and (window.window_id, relation.index) in self.moved_relations:
                continue  # 判断 C 拿不准的：写成待确认的关系了（_relation_claims），不进隔离表
            if not relation.passed:
                packet_id, revision = where(evidence)
                codes = [finding.as_text() for finding in relation.failures]
                payload = {
                    "window_id": window.window_id,
                    "index": relation.index,
                    "relation": relation.raw,
                    "failures": codes,
                    "notes": relation.notes,
                    "dropped": [{"evidence": proof, "finding": finding.as_text()} for proof, finding in relation.dropped],
                    # 过了 R1 的证据在哪几句：R1 改指过的句子 L3 原样里没有，单条撤回按句子编号清隔离表要找得到
                    "proofs": [
                        {"sentence_id": proof.sentence_id, "packet_id": proof.packet_id, "revision": proof.revision}
                        for proof in relation.proofs
                    ],
                }
                if relation.judge is not None:  # 判断 C 判的经过：P、哪一档定的、第二档的回答和理由
                    payload["judge"] = relation.judge
                rows.append((packet_id, revision, "relation", _json(payload), ",".join(codes), relation.first_rule, self.checked_by))
                self.stats.quarantined_relations += 1
                for rule in {finding.rule for finding in relation.failures}:
                    self.stats.relation_by_rule[rule] = self.stats.relation_by_rule.get(rule, 0) + 1
                continue
            for proof, finding in relation.dropped:
                packet_id, revision = where([proof])
                payload = {
                    "window_id": window.window_id,
                    "index": relation.index,
                    "relation": {name: relation.raw.get(name) for name in ("subject", "relation", "object")},
                    "evidence": proof,
                }
                rows.append((packet_id, revision, "relation_evidence", _json(payload), finding.as_text(), finding.rule, self.checked_by))
                self.stats.quarantined_relation_evidence += 1
        self.conn.executemany(
            "INSERT INTO quarantine (packet_id, revision, item_kind, payload, reason, rule_id, rule_version, created_at, window_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*row, now, window.window_id) for row in rows],
        )


_PAUSE_FROM = re.compile(r"(?:从|自)?([^，,。；;！!？?、]{1,20}?)(?:起|开始)")  # 日子里可以有空白（“11 月 9 日起”）
_PAUSE_WORD = re.compile(r"停|暂缓|缓一缓|搁置|先放|放一放|暂时不|hold|paus", re.IGNORECASE)  # 说停的那一小句
_CLAUSE = re.compile(r"[，,。；;！!？?\n]")


def pause_day(proofs: Sequence[Proof]) -> tuple[str | None, str]:
    """一处暂停的关系从哪天起停（D153 第 3 条）：证据那几句里写了“（从）某天起、开始”、换算得出一个日子的，用那天；
    没写的用最早那一句说话那天。返回 (写法, 精度)。"""
    days: set[date] = set()
    for proof in proofs:
        clauses = [clause for clause in _CLAUSE.split(proof.shown) if clause.strip()]
        paused = [clause for clause in clauses if _PAUSE_WORD.search(clause)] or clauses  # 只看说停的那一小句（后面说哪天起恢复的，那天不算）
        for clause in paused:
            for match in _PAUSE_FROM.finditer(clause):
                days |= sentence_times(match.group(1), proof.said_at, said_day=False).days
    if len(days) == 1:
        return next(iter(days)).isoformat(), "day"
    if not proofs:
        return None, "unknown"
    first = min(proofs, key=lambda proof: proof.said_at)
    return first.said_at.date().isoformat(), "day"


def window_range(packet_id: str) -> tuple[str, str]:
    """窗口编号以“包号#”开头的，写成区间 [包号#, 包号$)：账本按字节比（UTF-8），# 后面紧挨着的就是 $，所以以“包号#”
    开头的都在这个区间里，别的都不在。区间能走 window_id 的索引，substr 用不上索引。"""
    return f"{packet_id}#", f"{packet_id}$"


def _proof_packets(row: sqlite3.Row) -> set[str]:
    return {str(proof["packet_id"]) for proof in json.loads(row["proofs"])}


def _chunks(items: Sequence[str], size: int = 500) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _marks(items: Sequence[object]) -> str:
    return ", ".join("?" for _ in items)


def _claim_entities(row: sqlite3.Row) -> set[str]:
    found = {str(role["entity_id"]) for role in json.loads(row["entities"])}
    if row["subject_entity_id"] is not None:
        found.add(str(row["subject_entity_id"]))
    return found


def _speaker_roles(item: Checked, *, said_only: bool = False) -> dict[str, set[str]]:
    """每个 L3 实体：提到它的、过了 L4 的事实和关系（J4.4，关系的两头），证据是谁说的（主人、Bot、别人）。

    附件的正文不算（D72 审阅）：附件的句子的说话人是发它的人，可文件里的“我”是写文件的人。主人转来的供应商回信里
    “我同意……”不是主人。"""
    assert item.check is not None
    roles: dict[str, set[str]] = {}
    mentions: list[tuple[set[str], Sequence[Proof]]] = []
    for fact in item.check.passed:
        keys = {str(fact.raw["subject"])} if fact.raw.get("subject") is not None else set()
        listed = fact.raw.get("entities")
        for role in listed if isinstance(listed, list) else []:
            if isinstance(role, Mapping) and role.get("key") is not None:
                keys.add(str(role["key"]))
        mentions.append((keys, fact.proofs))
    for relation in item.check.passed_relations:  # 只出现在关系里的“我”也看证据是谁说的（J4.4）
        mentions.append(({str(relation.raw[end]) for end in ("subject", "object") if relation.raw.get(end) is not None},
                         relation.proofs))
    for keys, proofs in mentions:
        for key in keys:
            roles.setdefault(key, set()).update(
                proof.speaker_role
                for proof in proofs
                if proof.kind != ATTACHMENT and (not said_only or proof.origin == ORIGIN_SAID)
            )
    return roles


def _relation_claim_entities(row: sqlite3.Row) -> set[str]:
    """一处关系的说法用着的实体：两头，负责另有代表哪一家。"""
    return {str(one) for one in (row["subject_entity_id"], row["object_entity_id"], row["org_entity_id"]) if one}


def _is_owner(
    item: Checked,
    name: str,
    mention: str,
    entity_type: str,
    roles: set[str],
    owner_names: frozenset[str] = frozenset(),
    said_roles: set[str] | None = None,
) -> bool:
    """这个人是不是记忆的主人（规范第 5 节）。

    - L3 认成了窗口里别的说话人（Bot、别人、邮件的发件人）的，不是：那是他自己说的“我”。
    - L3 给的名字（name，最完整的叫法）是主人的名字的（D62，配置里的中文名、英文名，或者只由它们拼成的写法），是，
      对话、邮件都一样。只看写法（mention）不算：“Ming Zhou”写成“Ming”的是别人。邮件的发件人是别的说话人：
      显示名谁都能改，发件人就叫主人的名字也不算（主人自己发的信也一样，认不出来）。
    - 对话里 L3 叫它“主人”“记忆主人”的，是，不管是谁说的。
    - 对话里名字或写法是“我”这类第一人称的，提到它的事实的证据里有主人说的话才是；只有 Bot、别人说的，是他们自己。
      不要求证据全是主人说的：Bot 转述主人的要求、主人自己说的，L3 常常记成同一个“我”。邮件里的“我”是发件人。
    - 对话里名字是“你”“您”的（D78）：窗口里只有主人和一个 Bot 说话、提到它的事实的证据全是这个 Bot 自己的话
      （转发、外部内容里的“你”不算），是主人；有第三个人说话、主人自己说的“你”（说的是 Bot），照旧是泛称。
      只看 name：L3 已经认成具体的人的（name 是名字、写法是“你”），照名字走。这条还是 Bot 说的，来源、原话、
      推断这些标记照旧，只是实体挂到主人上。
    """
    if entity_type != "人":
        return False
    others: set[str] = set()
    for block in item.window.blocks:
        if block.speaker_role != ROLE_OWNER:
            others.add(name_key(block.speaker))
            if " <" in block.speaker:  # 邮件的发件人是“名字 <地址>”，显示名谁都能改，名字和地址都算别人
                display, _, address = block.speaker.rpartition(" <")
                others |= {name_key(display), name_key(address.rstrip(">"))}
    if name_key(name) in others or (mention and name_key(mention) in others):
        return False
    if item.window.channel != "chat" and any(block.speaker == MASKED for block in item.window.blocks):
        # 发件人那一句撤回了的信（“[已撤回]”，D73 第 16 条）：看不出发件人叫什么，他就叫主人的名字也认不出来。
        # 邮件的发件人不挂到主人上（D62），这样的窗口里主人的名字一律不按名字挂
        return False
    if owner_by_name(name, owner_names):
        return True
    if item.window.channel != "chat":
        return False
    if name in OWNER_NAMES:
        return True
    if name in FIRST_PERSON or mention in FIRST_PERSON:
        return ROLE_OWNER in roles
    if name in SECOND_PERSON:  # 只看 name：L3 已经认成具体的人的（name 是名字、mention 是“你”），照名字走
        said = roles if said_roles is None else said_roles
        return roles == {ROLE_BOT} and said == {ROLE_BOT} and _one_on_one(item.window)
    return False


def _one_on_one(window: Window) -> bool:
    """窗口里只有主人和一个 Bot 说话（D78）。"""
    bots = set()
    for block in window.blocks:
        if block.speaker_role == ROLE_OWNER:
            continue
        if block.speaker_role != ROLE_BOT:
            return False
        bots.add(block.speaker)
    return len(bots) == 1


def remove_revision(conn: sqlite3.Connection, packet_id: str, revision: int) -> None:
    """包重发时清掉旧版本写进账本的东西，见 LedgerWriter.remove_revision。收件换版本的那个事务里调。"""
    LedgerWriter(conn, extracted_by="", embedding_model="", register=False).remove_revision(packet_id, revision)


# ---------- 向量补齐 ----------


def missing_vectors(conn: sqlite3.Connection, model: str) -> dict[str, str]:
    """向量没有、或者不是这个模型、不是这段文字算的事实：事实编号 → 事实文字。

    向量表每一行带着 10 KB 的向量：这个模型的编号和指纹从迁移 0019 的索引里顺着读一遍，不连表一条条按编号回表找（事实多了
    每找一条都要读一页，一次写账跑两遍）。事实照原来的先后（rowid）顺着读，找出来的和原来连表查的一样：别的模型的向量不进
    字典，照样算缺；有这个模型的向量才算文字的指纹。两句在同一个读事务里，和原来一条语句一样看到的是同一个时刻。"""
    missing: dict[str, str] = {}
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        have = dict(conn.execute("SELECT fact_id, text_sha256 FROM fact_vectors WHERE model = ? AND fact_id IS NOT NULL", (model,)))
        for fact_id, content in conn.execute("SELECT fact_id, content FROM facts ORDER BY rowid"):
            if content and ((digest := have.get(fact_id)) is None or digest != text_sha256(content)):
                missing[fact_id] = content
    finally:
        if own and conn.in_transaction:  # 出了错 SQLite 可能自己回滚了：再提交会报错，盖掉原来那个错
            conn.execute("COMMIT")
    return missing


def fill_vectors(
    conn: sqlite3.Connection,
    cache: VectorCache,
    embedder: Embedder,
    *,
    record: RecordCall | None = None,
    sleep: Callable[[float], None] = time.sleep,
    stats: EmbedStats | None = None,
    rejected: set[str] | None = None,
) -> int:
    """把缺的向量补上：缓存里有的直接用，没有的调向量服务（每批算完就进缓存）。返回补了几条。

    服务一直拒的那几段文字跳过，记进 rejected，这一次不再发，下次再试；服务调不通抛 EmbeddingError，已经补上的留着。
    """
    rejected = rejected if rejected is not None else set()
    missing = missing_vectors(conn, embedder.name)
    if not missing:
        return 0
    texts = sorted(set(missing.values()))
    found = cache.get(embedder.name, texts)
    todo = [text for text in texts if text not in found and text not in rejected]

    def keep(done: dict[str, bytes]) -> None:
        cache.put(embedder.name, done)
        found.update(done)

    try:
        if todo:
            embed_all(todo, embedder, record=record, sleep=sleep, stats=stats, store=keep)
            rejected.update(text for text in todo if text not in found)
    finally:
        filled = _write_vectors(conn, embedder.name, missing, found)
    return filled


def _write_vectors(conn: sqlite3.Connection, model: str, missing: Mapping[str, str], found: Mapping[str, bytes]) -> int:
    now = _now()
    rows = [
        (fact_id, model, EMBEDDING_DIM, text_sha256(content), found[content], now)
        for fact_id, content in sorted(missing.items())
        if content in found
    ]
    if not rows:
        return 0
    with transaction(conn):  # 调服务的这几秒里收件可能删了、改了事实：只写还在、文字没变的
        cursor = conn.executemany(
            "INSERT OR REPLACE INTO fact_vectors (fact_id, model, dim, text_sha256, vector, created_at)"
            " SELECT ?1, ?2, ?3, ?4, ?5, ?6 WHERE EXISTS (SELECT 1 FROM facts WHERE fact_id = ?1 AND content = ?7)",
            [(*row, missing[row[0]]) for row in rows],
        )
    return max(cursor.rowcount, 0)


# ---------- 一批 ----------


@dataclass
class WriteRun:
    stats: WriteStats
    embedding: EmbedStats
    stopped: str | None = None  # 向量服务调不通：账照写了，向量下次补
    without_result: list[str] = field(default_factory=list)  # 结果库里没有结果的窗口（L3 的，或者加了 --recheck 时复核的）
    waiting: list[tuple[str, int]] = field(default_factory=list)  # 有窗口没结果、这次没写完的包（queued 或者 failed）
    failed: list[tuple[str, int]] = field(default_factory=list)  # 其中被重调也没用的窗口挡住、标成 failed 的包（排队修）
    permanent: list[str] = field(default_factory=list)  # 其中 L3 重调也没用的窗口（不发的、截断拒答这类）：要 --retry-failed
    recheck_missing: list[str] = field(default_factory=list)  # 其中过了规则、还没有模型复核判定的窗口：要 extract --recheck
    recheck_permanent: list[str] = field(default_factory=list)  # 其中复核重调也没用的（截断、拒答、格式错、发不出去）


def _target_packets(item: Checked) -> list[tuple[str, int]]:
    return sorted({(line.packet_id, line.revision) for line in item.window.targets})


def _window_order(item: Checked) -> tuple[int, str]:
    """写的先后：窗口正文最早的那句话的时间，对话和邮件一起排；同一时刻按窗口编号。"""
    said = [epoch(block.said_at) for block in item.window.blocks if not block.context]
    return (min(said) if said else 0, item.window.window_id)


MAX_INPUT_CHARS = ExtractOptions().max_input_chars  # 窗口写出来超过这么多字的，L3 不发（too_big）


def run_write(
    checked: Sequence[Checked],
    writer: LedgerWriter,
    *,
    packets: Sequence[tuple[str, int]] = (),
    store: ResultStore,
    cache: VectorCache,
    embedder: Embedder,
    record: RecordCall | None = None,
    log: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    partial: bool = False,
) -> WriteRun:
    """L5、L6 跑一批：verify_windows 的结果按顺序一个窗口一个事务写进账本。

    partial：只取了点名的包（javmem write --packet）。跨包的邮件往来串切出来的窗口和平时不一样，所以标了 failed 的
    不往回改成 queued，下一次全量写账再看（审阅第 3 条）。

    一个包的窗口都有结果，写完最后一个时这个包改成 done；有窗口没结果的（还没抽、抽失败、不发），这个包这次写不完。
    挡住它的有重调也没用的（不发的、截断拒答这类，记在 permanent；加了 --recheck 时复核重调也没用的），包标成 failed，
    extraction_note 记原因（排队修）：巡检报出来，要 javmem extract --retry-failed。别的（还没抽、一时的毛病）留着、
    或者回到 queued。failed 的包取包时照样取，下次都有结果了就 done。packets 是这一批的包 (包号, 版本)：一个窗口都
    没有的（整包都是同步消息、系统事件），没什么可抽，最后也改成 done。

    向量先按这一批过了 L4 的事实文字一起算好（缓存里有的不再算，每批算完就进缓存）；向量服务调不通，账照写，
    向量先空着，写完这一批补一遍，补不上的下次 javmem write 开头再补。
    """
    run = WriteRun(writer.stats, EmbedStats())
    checked = sorted(checked, key=_window_order)
    writer.batch_windows = frozenset(item.window.window_id for item in checked)
    blocked: set[tuple[str, int]] = set()
    reasons: dict[tuple[str, int], Counter[str]] = {}  # 挡住的包里重调也没用的那几段：为什么、几段
    windows_of: dict[tuple[str, int], list[str]] = {}
    for item in checked:
        for packet in _target_packets(item):
            windows_of.setdefault(packet, []).append(item.window.window_id)
        if item.check is not None:
            continue
        run.without_result.append(item.window.window_id)
        too_big = len(item.rendered.text) > MAX_INPUT_CHARS
        reason: str | None = None
        if item.skipped == NO_RECHECK:
            unsent = item.recheck_unsent or ("unredacted" if item.recheck_key is None else None)  # 没有键是事实那一段打码没打干净
            failure = None if unsent is not None else store.last_recheck_failure(str(item.recheck_key))
            if unsent is not None or failure is not None:
                run.recheck_permanent.append(item.window.window_id)
                reason = f"recheck_{unsent or failure}"
            else:
                run.recheck_missing.append(item.window.window_id)
        else:
            # 只读一次：写账开结果库不独占，这时另一个 extract --retry-failed 可能刚记了一次调用（审阅第 2 条）
            failure = None if item.skipped == NOT_SENT or too_big else store.last_failure(item.cache_key)
            if item.skipped == NOT_SENT or too_big or failure is not None:
                run.permanent.append(item.window.window_id)
                reason = _not_redone(item, too_big, failure)
        blocked.update(_target_packets(item))
        if reason is not None:
            for packet in _target_packets(item):
                reasons.setdefault(packet, Counter())[reason] += 1
    last_window = {packet: windows[-1] for packet, windows in windows_of.items()}

    texts = [str(fact.raw.get("text") or "").strip() for item in checked if item.check is not None for fact in item.check.passed]
    vectors = cache.get(embedder.name, texts)
    rejected: set[str] = set()  # 这一次服务拒了的文字，不再发
    service_ok = True

    def keep(done: dict[str, bytes]) -> None:
        cache.put(embedder.name, done)
        vectors.update(done)

    def ensure(needed: Sequence[str]) -> None:
        nonlocal service_ok
        missing = [text for text in dict.fromkeys(needed) if text and text not in vectors and text not in rejected]
        if not missing or not service_ok:
            return
        try:
            embed_all(missing, embedder, record=record, sleep=sleep, stats=run.embedding, store=keep)
        except EmbeddingError as error:
            service_ok = False  # 账照写，向量先空着
            run.stopped = f"embedding: {error}"
        rejected.update(text for text in missing if text not in vectors)

    def fill() -> None:
        nonlocal service_ok
        try:
            writer.stats.vectors_filled += fill_vectors(
                writer.conn, cache, embedder, record=record, sleep=sleep, stats=run.embedding, rejected=rejected
            )
        except EmbeddingError as error:
            service_ok = False
            run.stopped = f"embedding: {error}"

    fill()
    ensure(texts)
    for item in checked:
        if item.check is None:
            continue
        done = [
            packet for packet in _target_packets(item) if last_window[packet] == item.window.window_id and packet not in blocked
        ]
        if writer.already_written(item):
            writer.mark_done(done)
            writer.stats.windows_skipped += 1
            continue
        ensure(writer.texts_needed(item))
        written = writer.write_window(item, vectors, done=done)
        if log is not None:
            what = f"说法 {len(item.check.passed)}，隔离 {len(item.check.quarantined)}" if written else "包换了版本，没写"
            log(f"{item.window.window_id}：{what}")
    empty = [packet for packet in packets if packet not in windows_of]
    if empty:
        with transaction(writer.conn):
            for packet_id, revision in empty:
                cursor = writer.conn.execute(
                    "UPDATE packets SET extraction = 'done', extraction_note = NULL"
                    " WHERE packet_id = ? AND revision = ? AND state = 'current' AND extraction IN ('queued', 'failed')",
                    (packet_id, revision),
                )
                writer.stats.packets_done += cursor.rowcount
                if cursor.rowcount:  # 一个窗口都没有了（比如句子都撤回了）：以前的窗口都少掉了
                    writer.stats.stale_claims += writer.drop_stale_windows(packet_id, frozenset())
    run.waiting = sorted(blocked)
    run.failed = _settle_blocked(writer.conn, run.waiting, reasons, requeue=not partial)
    if service_ok:
        fill()
    return run


def _not_redone(item: Checked, too_big: bool, failure: str | None) -> str:
    """一个重调也没用的窗口为什么，记进 extraction_note 的种类：带着 private 的内容按规矩不发（private）、打码没打干净
    （unredacted）、太大（too_big）、上一次调用截断、拒答、太长、被挡、格式错（failure，store.last_failure 给的）。"""
    if item.skipped == NOT_SENT:
        return "private" if item.window.scope.private else "unredacted"
    if too_big:
        return "too_big"
    assert failure is not None
    return failure


def note_text(reasons: Mapping[str, int]) -> str:
    """extraction_note 的写法：种类=段数，按种类排，分号隔开（format_error=1;unredacted=2）。"""
    return ";".join(f"{kind}={count}" for kind, count in sorted(reasons.items()))


def _settle_blocked(
    conn: sqlite3.Connection,
    waiting: Sequence[tuple[str, int]],
    reasons: Mapping[tuple[str, int], Mapping[str, int]],
    *,
    requeue: bool = True,
) -> list[tuple[str, int]]:
    """这次没写完的包（排队修）：挡住它的有重调也没用的，标 failed、记原因（原因换成这一次的）；没有的（还没抽、
    一时的毛病、--retry-failed 重调碰上连不上）以前标了 failed 的回到 queued（requeue 是 False 时不改）。
    返回标成 failed 的包。"""
    failed: list[tuple[str, int]] = []
    if not waiting:
        return failed
    with transaction(conn):
        for packet_id, revision in waiting:
            found = reasons.get((packet_id, revision))
            if found:
                cursor = conn.execute(
                    "UPDATE packets SET extraction = 'failed', extraction_note = ? WHERE packet_id = ? AND revision = ?"
                    " AND state = 'current' AND extraction IN ('queued', 'failed')",
                    (note_text(found), packet_id, revision),
                )
                if cursor.rowcount:
                    failed.append((packet_id, revision))
            elif requeue:  # queued_at 不动（迁移 0029）：标了 failed 的取包时照样取，一直在排队，接着算
                conn.execute(
                    "UPDATE packets SET extraction = 'queued', extraction_note = NULL WHERE packet_id = ? AND revision = ?"
                    " AND state = 'current' AND extraction = 'failed'",
                    (packet_id, revision),
                )
    return failed


def record_quote_roles(conn: sqlite3.Connection, packets: Iterable[NormalizedPacket]) -> int:
    """只在引用里看到过的话（D101，第五轮第 50 条）：这几个包的每一句（有 copy_key 的）按刚做的 L1 记下是能当原话、
    可以当证据的引用、还是都不是（raw_sentences.copy_role），可以当证据的引用另记它那封信回的哪一封（reply_to）；
    每个包记下是哪一版、哪一版 L1 认的（quote_roles）。以后取包时查表，不再把这些包读进来。不自己开事务。返回记了几句。"""
    from javmem.pipeline.batch import ROLE_COPY, ROLE_INSERTED, ROLE_ORIGINAL, ROLE_OTHER  # 取包那边读这几个值

    total = 0
    for packet in packets:
        copies = {copy.sentence_id: copy for copy in quote_copies(packet)}
        rows = []
        for unit in packet.units:
            for line in unit.lines:
                if copy_key(line.text) is None:
                    continue
                copy = copies.get(line.sentence_id)
                if line.inserted:  # 回信的人插在引用里的话：能当原话；撤回原话时也要标上（第五轮审阅第 1 条）
                    role = ROLE_INSERTED
                else:
                    role = ROLE_ORIGINAL if line.kind in ORIGINAL_KINDS else ROLE_COPY if copy is not None else ROLE_OTHER
                rows.append((role, copy.in_reply_to if copy is not None else None, line.sentence_id, packet.header.revision))
        conn.executemany("UPDATE raw_sentences SET copy_role = ?, reply_to = ? WHERE sentence_id = ? AND revision = ?", rows)
        conn.execute(
            "INSERT INTO quote_roles (packet_id, revision, l1_version) VALUES (?, ?, ?) ON CONFLICT (packet_id)"
            " DO UPDATE SET revision = excluded.revision, l1_version = excluded.l1_version",
            (packet.packet_id, packet.header.revision, L1_VERSION),
        )
        total += len(rows)
    return total


def record_quote_holders(
    conn: sqlite3.Connection,
    holders: Mapping[tuple[str, str], tuple[str, str, int, str | None] | None],
    requeue: Iterable[str],
) -> list[str]:
    """只在引用里看到过的话（D101）：这一批算出来的“谁当证据”（load_queued 的 Batch.quote_holders）记进 quote_holders，
    当证据的那一份换了的包（Batch.requeue，这一批以外的）改回排队：写完了的、current、没撤回的才改。
    重跑时那一行变回上文（或者变成证据），窗口的字变了，L3 重抽，写账时按重写一个窗口换掉以前的说法。
    不自己开事务。返回改回排队的包。"""
    for (key, scope), holder in holders.items():
        if holder is None:
            conn.execute("DELETE FROM quote_holders WHERE copy_key = ? AND scope = ?", (key, scope))
        else:
            conn.execute(
                "INSERT INTO quote_holders (copy_key, scope, sentence_id, packet_id, revision, in_reply_to)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (copy_key, scope) DO UPDATE SET sentence_id = excluded.sentence_id,"
                " packet_id = excluded.packet_id, revision = excluded.revision, in_reply_to = excluded.in_reply_to",
                (key, scope, *holder),
            )
    done = []
    now = _now()  # 写完了的又进排队：从现在起算（queued_at，迁移 0029）
    for packet_id in sorted(set(requeue)):
        cursor = conn.execute(
            "UPDATE packets SET extraction = 'queued', queued_at = ? WHERE packet_id = ? AND state = 'current'"
            " AND withdrawn_at IS NULL AND extraction = 'done'",
            (now, packet_id),
        )
        if cursor.rowcount:
            done.append(packet_id)
    return done


def ledger_summary(conn: sqlite3.Connection) -> dict[str, object]:
    """账本里现在的数。不抄原文。"""

    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    def grouped(sql: str) -> dict[str, int]:
        return {str(row[0]): int(row[1]) for row in conn.execute(sql)}

    facts = one("SELECT count(*) FROM facts")
    claims = one("SELECT count(*) FROM fact_claims")
    return {
        "facts": facts,
        "facts_current": one("SELECT count(*) FROM facts WHERE superseded_by IS NULL AND withdrawn_at IS NULL"),
        "facts_superseded": one("SELECT count(*) FROM facts WHERE superseded_by IS NOT NULL"),
        "facts_by_status": grouped("SELECT status, count(*) FROM facts GROUP BY status ORDER BY status"),
        "pending_reasons": grouped(
            "SELECT pending_reason, count(*) FROM facts WHERE pending_reason IS NOT NULL GROUP BY pending_reason ORDER BY 1"
        ),
        "facts_in_groups": one("SELECT count(*) FROM facts WHERE group_key IS NOT NULL"),
        "facts_without_subject": one("SELECT count(*) FROM facts WHERE subject_entity_id IS NULL"),
        "claims": claims,
        "claims_merged": claims - facts,  # 合到已经有的事实上的说法（冻结的、没说法的事实不算）
        "facts_with_several_claims": one(
            "SELECT count(*) FROM (SELECT fact_id FROM fact_claims GROUP BY fact_id HAVING count(*) > 1)"
        ),
        "evidence_for_facts": one("SELECT count(*) FROM evidence WHERE fact_id IS NOT NULL"),
        "evidence_for_aliases": one("SELECT count(*) FROM evidence WHERE alias_id IS NOT NULL"),
        "entities": one("SELECT count(*) FROM entities"),
        "entities_by_type": grouped("SELECT type, count(*) FROM entities GROUP BY type ORDER BY type"),
        "aliases": one("SELECT count(*) FROM aliases"),
        "vectors": one("SELECT count(*) FROM fact_vectors"),
        "vector_dims": grouped("SELECT dim, count(*) FROM fact_vectors GROUP BY dim"),
        "facts_without_vector": one(
            "SELECT count(*) FROM facts WHERE NOT EXISTS (SELECT 1 FROM fact_vectors AS v WHERE v.fact_id = facts.fact_id)"
        ),
        "relations": one("SELECT count(*) FROM relations"),  # J4.4：关系（一段一行）
        "relations_by_type": grouped("SELECT relation_type, count(*) FROM relations GROUP BY relation_type ORDER BY 1"),
        "relations_by_state": grouped("SELECT state, count(*) FROM relations GROUP BY state ORDER BY state"),
        "relations_by_status": grouped("SELECT status, count(*) FROM relations GROUP BY status ORDER BY status"),
        "relations_superseded": one("SELECT count(*) FROM relations WHERE superseded_by IS NOT NULL"),
        "relation_pending_reasons": grouped(
            "SELECT pending_reason, count(*) FROM relations WHERE pending_reason IS NOT NULL GROUP BY pending_reason ORDER BY 1"
        ),
        "relation_claims": one("SELECT count(*) FROM relation_claims"),
        "relations_with_several_segments": one(
            "SELECT count(*) FROM (SELECT identity_key FROM relations WHERE identity_key IS NOT NULL"
            " GROUP BY identity_key HAVING count(*) > 1)"
        ),
        "evidence_for_relations": one("SELECT count(*) FROM relation_evidence"),
        "quarantine": grouped("SELECT item_kind, count(*) FROM quarantine GROUP BY item_kind ORDER BY item_kind"),
        "quarantined_facts_by_first_rule": grouped(
            "SELECT rule_id, count(*) FROM quarantine WHERE item_kind = 'fact' GROUP BY rule_id ORDER BY rule_id"
        ),
        "quarantined_relations_by_first_rule": grouped(
            "SELECT rule_id, count(*) FROM quarantine WHERE item_kind = 'relation' GROUP BY rule_id ORDER BY rule_id"
        ),
        "review_queue": grouped("SELECT kind, count(*) FROM review_queue GROUP BY kind ORDER BY kind"),
        "packets_by_extraction": grouped(
            "SELECT extraction, count(*) FROM packets WHERE state = 'current' GROUP BY extraction ORDER BY extraction"
        ),
    }
