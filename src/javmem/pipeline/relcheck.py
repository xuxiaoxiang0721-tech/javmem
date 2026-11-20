"""L4 核对关系（J4.3）：L3 抽出来的每条关系，按 R1 到 R6 对着原文核一遍。全用规则，不调模型，不碰账本。

关系是“主体、关系、客体”加证据（javmem.pipeline.extract.map_output），没有文字可比，所以不像事实那样拿文字比原文
（G2 到 G7），只核下面这几样（设计稿 04 p4、01 p2；R2、R4 到 R6 的做法主人 11-16 用选择卡定的）：

| 规则 | 看什么 | 没过的原因 |
| --- | --- | --- |
| L3 | L3 换编号时标过问题的、结构不对的，直接隔离，和事实一样 | 照抄 L3 的：unknown_ref、context_ref、empty_quote、no_usable_evidence、unknown_subject、unknown_object、duplicate_key；bad_shape |
| R1 | 原句逐字可查：每条证据照事实的 G1 核 | quote_not_found、quote_in_context、quote_ambiguous、empty_quote |
| R2 | 两头在原文里出现 | subject_not_in_text、object_not_in_text |
| R3 | 关系是七种之一（D210 加了隶属于），两头的类型对得上允许表；两头不是同一个；亲属写了具体关系 | type_not_allowed、same_ends、kinship_missing |
| R4 | 结束词和状态对得上 | ended_but_active、ends_in_future |
| R5 | 开始、结束日子在原文里有根据。**不拦**：对不上的降精度或者去掉，关系留着 | （只记 notes）start_lowered、start_elsewhere、start_dropped，end_ 开头的三个，end_before_start |
| R6 | 说法出自谁。**不拦**：写着出自主人、证据里却没有主人的话的，改成证据那一句的说话人，标推断 | （只记 notes）not_owner |

- **R1：** 每条证据照事实的 G1 核（verify.check_evidence：同样的比法，同一句话里连着几句拼起来、改指别的能当证据的句子
  都照旧）。没过的证据去掉（记在 dropped，写账时进隔离表 `relation_evidence`），一条都不剩才算这条关系没过。
- **R2：** 两头各看一次，下面任何一样都算出现了（主人 11-16 选的宽的那种）：①名字或原文的说法（mention）在证据那几句里；
  ②在窗口别的行里，上文也算（定义卡允许表“给 J4.3 的两条”第 2 条：对面那家常在上一句点名）；③是主人（主人常不写出来）；
  ④就是窗口里的一个说话人（Bot 说“我来管”）；⑤别名表、Bot 名单、认人认事的〔指〕认得这个名字，它的别的叫法在窗口里；
  ⑥ L3 拼出来的名字（“某票 累购”），按空格、斜杠、加号、括号这些拆开以后每一段窗口里都有。②到⑥记一笔。
- **R3：** 允许表第三节的组合（ALLOWED）。类型照 L3 写的；名字在 Bot 名单里的（Lexicon.bot_of）算 Bot。还有两头是同一个的、
  亲属没写具体关系（妻子、儿子……）的，也不收。负责的 org、role L3 还没出（D118），出了以后这里照允许表补上。
- **R4：** 证据里有这种关系的结束词（每张卡第 5 项“结束”的说法，加 04 p4 的取消、终止、不再……，见 _ENDS），只有同时满足下面
  几样才算数（定义卡已定“结束词只管它说的那一条”，D90）：
  - 说的就是这一条：同一小句里，结束词那一边离它最近的、类型对得上的名字，就是这条关系结束的那一头（持有的客体、托管于的
    主体、任职于和负责的主体、客户是的客体、隶属于的主体）；名字和结束词中间只能夹着“已经、了、的、我”、介词（在、从、于）、日子、数量
    这种（_FILLER，介词后面跟着的实体名字先拿掉：“从东岸证券离职了”），夹着别的名词（“我同事离职了”“小周的妹妹离婚了”
    “不再派某台机器而是派某 Bot”）就说不准是谁结束了，不算；介词后面的名字是地方、对象，不算结束的那个（“X1 票据在
    海港银行已经到期了”到期的是票据）；说的是不建实体的东西，什么都不结束。任职于另要求那一小句里写着公司的话，就是这一家
    （“甲已辞去 A 的工作，加入 B”只结束甲在 A 的那条）；连的是部门的，写着这个部门往上的那家也算（D210：照这个窗口 L3 抽的、
    没结束的隶属于一层层往上找，“甲从 A 离职了”结束甲在“A 外贸部”的那条；反过来，连的是公司、只写着它的部门的不算）。
    隶属于的“不再是”要后面写着那一家或者“子公司、分公司、旗下、下属”（“不再是主要的部门”说的不是归哪家）；持有另要求那一小句里没写着别的人，客户是没写着别的人、别的机构
    （“小周把她那份卖了”）。敲出、KO 只说单子（FCN、累购），不说股票。
  - 都结束了：持有、托管于、客户是的结束词后面（同一小句里，或者紧跟着的、没写别的实体的下一小句）写着还剩着的
    （“卖了五百股，还剩五百股”“销户了一个子账户，主账户还在”），不算；“剩下的 X2 也卖了”“sold the remaining X2”说的是
    剩下的也卖了，“，X5 还在”说的是 X5，都照样算。说了卖掉多少、又没说全部的（“卖了五百股”）不算，卖光、清仓、
    “500 股全部卖掉了”才是全部。
  - 已经发生了：结束词带着“了”“已经”“成交”，或者本身就是说已经结束了的（“卖了”“不再负责”）；那一小句写的日子晚于
    说话那天的（“2027 年 1 月到期”“下周到期”）是以后的事。只写着一个以前的日子不算（“6/19 那张累购在 KO 上”）。
  - 不是问句（问号结尾的也算）、条件、语气（“是否敲出”“如果到期”“可能卖掉”“差一步敲出”“等某银行确认”），前面紧挨着的
    不是否定（“没卖”“未到期”，英文的 not、never 在前面这一小句里就不算），也不是暂时的（“先不管了”是暂停，D101）；
    “到期日”“敲出价”“KO 586.37”“在 KO 上”“赎回申请”这种是名词、价位，不算。
  宁可漏，不错拦：拦下的一条就少记一条对的关系。
  这样的结束词碰上 L3 标“生效”“暂停”的，算对不上（ended_but_active）。L3 标“已结束”、结束的日子却晚于这个窗口最后一句
  话的那天，也算对不上（ends_in_future）。对不上的进隔离表，不猜谁对（主人 11-16 选的）。
- **R5：** 开始、结束日子各看一次，照事实 G6 管 occurred_at 的办法：证据那几句写明的日子、按说话时间换算的相对说法（上周三、
  下个月）；一句里一个日子都没写的，加上说话那天（当场就是开始），对得上就留；只对得上粗一级的降精度（start_lowered）；
  证据里没有、窗口别的行写明了的也算（start_elsewhere）；都对不上的去掉这个日子（start_dropped），关系留着（主人 11-16
  选的，和 D123“宁可空着”一样）。说话那天只算那一天、那一刻，降到那个月、那一年的不算有根据。
  结束早于开始的去掉结束日子。开始、结束各算各的精度（账本里本来就是两列），L3 只给一个 time_precision，这里不用它。
  日子的换算用仓库自己的中文规则（verify.sentence_times，J2.14 起事实的 G6 在用），不装 JioNLP。
- **R6：** L3 的 speaker 是“这条说法出自谁”（J4.1 进账本的 attributed_to）。写的是主人，可证据里没有一句是主人自己说的
  （说话人的身份是主人），也不是引用、转发里写明出自主人的，就改成证据里最早那一句的说话人，确定程度标推断，记一笔
  （主人 11-16 选的）。Bot 复述主人的话 L1 标成复述，只当上文，本来就不能当证据。写的是一份文件、一封信的出处的（“某银行
  周报”“某某的邮件”）照收：提示词就是这么要的。
- **确定程度：** L3 的 stated 是 verified，inferred 是 inferred；证据全是只在引用里看到过的话（quoted_only，D101）的一律推断。
  推断的照常过（D19：推断的关系进图，标成推断）。

过了的关系留在 RelationCheck 里（去掉的证据、改过的日子和精度、确定程度、说法出自谁、notes），J4.4 拿去对齐两头、写说法、
合成、判新旧；这一步不写账本的 relations、relation_claims。规则的细节见 docs/development.md 的“L4 核对”。

R1 到 R6 后面还有判断 C（javmem.pipeline.reljudge，配了 JAVMEM_JUDGE_MODE 才判）：过了 R1 到 R6 的关系，本机模型判原话看不看得出
这条关系，没把握的交抽取模型再判；不过的原因写 C:…，进隔离表，判的经过在 RelationCheck.judge。这里只登记 C 这条规则。
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from javmem.packet.model import ROLE_OWNER
from javmem.pipeline.merge import OWNER_NAMES, name_key, owner_by_name
from javmem.pipeline.normalize import QUOTED_ONLY
from javmem.pipeline.preb import NOTE_LABEL
from javmem.pipeline.render import RenderedWindow
from javmem.pipeline.verify import (
    _CLAUSE_MARK,
    _COARSER,
    INFERRED,
    VERIFIED,
    Finding,
    Occurred,
    Proof,
    Times,
    _has_condition,
    _has_hedge,
    _is_question,
    _matches,
    _Ref,
    check_evidence,
    fold,
    fold_map,
    parse_occurred,
    said_times,
    sentence_times,
    soft,
)
from javmem.pipeline.window import Window
from javmem.zh.lexicon import BOT, EMPTY, Lexicon

RULES: dict[str, str] = {
    "R1": "关系：原句逐字可查（每条证据照事实的 G1 核，没过的证据去掉，一条都不剩才不收）",
    "R2": "关系：两头在原文里出现（证据、窗口别的行、主人、说话人自己、别名表和〔指〕认得、拼起来的每一段都在都算）",
    "R3": "关系：七种之一、两头的类型对得上允许表；两头不是同一个；亲属写了具体关系",
    "R4": "关系：结束词说的是这一条、已经发生了，L3 却标生效或暂停的不收；标已结束、结束日子还没到的也不收",
    "R5": "关系：开始、结束日子在原文里有根据（证据写明的、按说话时间换算的、说话那天），没有的降精度或去掉，关系留着",
    "R6": "关系：写着出自主人、证据里没有主人的话的，改成证据那一句的说话人、标推断，关系留着",
    # 判断 C（javmem.pipeline.reljudge）：配了 JAVMEM_JUDGE_MODE 才判，排在 R1 到 R6 后面，只判过了它们的
    "C": "关系：判断（C），本机模型判原话看不看得出这条关系，有把握说不能的不收；没把握的交抽取模型再判，说不能、拿不准的不收",
}
RULE_ORDER = ("L3", *RULES)
# 规则表的 decided_by（谁定的）：R2、R4 到 R6 的做法主人 11-16 用选择卡定的（D148）；R3 照定义卡的允许表（CC 起草，
# 主人 D13、D90、D92 定的）；R1 是设计稿的规则，照事实的 G1
DECIDED_BY: dict[str, str] = {
    "R1": "设计稿 04 p4（照事实的 G1）",
    "R2": "主人 11-16 选择卡（D148）",
    "R3": "定义卡允许表：CC 起草，主人定的（D13、D90、D92；隶属于 D210、D213）",
    "R4": "主人 11-16 选择卡（D148）",
    "R5": "主人 11-16 选择卡（D148）",
    "R6": "主人 11-16 选择卡（D148）",
    "C": "主人 11-15、11-16 选择卡（D127、D131、D133）",
}

ACTIVE, ENDED, PAUSED = "生效", "已结束", "暂停"  # L3 的三种状态（extract.RELATION_STATES）
PERSON, ORG, PROJECT, DEAL, ASSET = "人", "公司或机构", "项目", "订单或合同", "资产"
KINSHIP = "亲属"
PART_OF = "隶属于"  # D210、D213（卡 7）：部门、分公司、子公司 → 它所属的那家
# 定义卡允许表第三节：哪种关系允许哪些（主体，客体）的类型
ALLOWED: dict[str, frozenset[tuple[str, str]]] = {
    "持有": frozenset({(PERSON, ASSET), (PERSON, DEAL), (PERSON, ORG), (ORG, ASSET), (ORG, DEAL), (ORG, ORG)}),
    "托管于": frozenset({(ASSET, ORG), (DEAL, ORG)}),
    "负责": frozenset({(PERSON, PROJECT), (PERSON, DEAL), (PERSON, PERSON), (PERSON, ORG), (BOT, PROJECT), (BOT, ORG)}),
    "任职于": frozenset({(PERSON, ORG)}),
    "客户是": frozenset({(PERSON, ORG), (ORG, ORG)}),
    KINSHIP: frozenset({(PERSON, PERSON)}),
    PART_OF: frozenset({(ORG, ORG)}),  # 人不当主体：人在哪个部门记任职于（允许表，D210）
}
_STATES = frozenset({ACTIVE, ENDED, PAUSED})
_CERTAINTIES = frozenset({"stated", "inferred"})  # 含糊的不抽成关系（04 p3），L3 的输出结构里没有 vague

# ---------- 结果 ----------


@dataclass
class RelationCheck:
    index: int  # 在 L3 结果里是第几条关系，从 0 数
    raw: Mapping[str, object]  # L3 的这条关系，原样
    proofs: list[Proof] = field(default_factory=list)  # 过了 R1 的证据
    dropped: list[tuple[Mapping[str, object], Finding]] = field(default_factory=list)  # 没过 R1 的证据
    failures: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    valid_from: str | None = None  # R5 核过的开始日子（降过精度的写成那一级）
    valid_from_precision: str = "unknown"
    valid_to: str | None = None
    valid_to_precision: str = "unknown"
    status: str = VERIFIED  # verified、inferred
    attributed_to: str | None = None  # 说法出自谁：L3 的 speaker，R6 改过的是证据那一句的说话人
    subject_type: str | None = None  # 两头的类型（名字在 Bot 名单里的是 Bot）
    object_type: str | None = None
    judge: Mapping[str, object] | None = None  # 判断 C 判的经过（reljudge.apply_judgments 填）；没判的是 None

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def first_rule(self) -> str | None:
        """没过的规则里排在最前面的。"""
        rules = {finding.rule for finding in self.failures}
        return next((rule for rule in RULE_ORDER if rule in rules), None)


# ---------- 窗口：几条关系共用的 ----------


@dataclass(frozen=True)
class _Mention:
    key: str  # L3 的实体 key
    start: int  # 在一小句的比对写法里的位置
    end: int


@dataclass(frozen=True)
class _Window:
    """一个窗口里核关系要用的：每一行的比对写法、〔指〕认得的名字、说话人、日子、实体。"""

    lines: tuple[str, ...]  # 每一行 fold(shown)
    noted: frozenset[str]  # 〔指〕标的名字（比对写法），只算说法在那一行原文里的
    speakers: tuple[str, ...]  # 说话人（原样，Lexicon.bot_of 要）
    speaker_keys: frozenset[str]  # 说话人的比对写法；邮件的“名字 <地址>”名字、地址都算
    times: Times  # 每一行写明了的日子、相对的说法（不算说话那天）
    last_said: datetime  # 窗口里最晚的一句话
    partitions: tuple[str, ...]
    entities: Mapping[str, Mapping[str, object]]
    types: Mapping[str, str]  # 实体 key → 类型（名字在 Bot 名单里的是 Bot）
    names: Mapping[str, tuple[str, ...]]  # 实体 key → 名字和说法的比对写法（长的在前）
    # 这个窗口 L3 抽的、没结束、没标问题的隶属于：部门 key → 它归的那几家的 key（R4 的任职于认部门用，D210）
    parents: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    cache: dict[object, object] = field(default_factory=dict, compare=False, repr=False)  # R4 的小句、名字，一个窗口算一次

    def anywhere(self, folded: str) -> bool:
        return bool(folded) and any(folded in line for line in self.lines)


_NOTE = re.compile(rf"〔{NOTE_LABEL}：([^〕]*)〕")


def window_notes(rendered: RenderedWindow) -> dict[str, list[tuple[str, str]]]:
    """预处理 B 写进窗口的〔指：说法 = 名字；…〕（javmem.pipeline.preb.apply_notes），按行的编号。原文里的〔〕写进窗口时
    都换成了［］，所以〔指：…〕只可能是标注；shown（原文）里没有标注。"""
    notes: dict[str, list[tuple[str, str]]] = {}
    for row in rendered.text.split("\n"):
        ref, space, rest = row.partition(" ")
        if not space or ref not in rendered.shown:
            continue
        for match in _NOTE.finditer(rest):
            for pair in match.group(1).split("；"):
                mention, sep, name = pair.partition(" = ")
                if sep and mention.strip() and name.strip():
                    notes.setdefault(ref, []).append((mention.strip(), name.strip()))
    return notes


def _speaker_keys(speakers: Iterable[str]) -> frozenset[str]:
    keys: set[str] = set()
    for speaker in speakers:
        keys.add(name_key(speaker))
        if " <" in speaker and speaker.endswith(">"):  # 邮件的发件人“名字 <地址>”
            display, _, address = speaker[:-1].rpartition(" <")
            keys |= {name_key(display), name_key(address)}
    return frozenset(key for key in keys if key)


def _entity_type(entity: Mapping[str, object], lexicon: Lexicon, speakers: Sequence[str]) -> str:
    kind = str(entity.get("type") or "")
    if kind == BOT:
        return BOT
    name, mention = str(entity.get("name") or ""), str(entity.get("mention") or "")
    if lexicon.bot_of(name, mention, kind, speakers) is not None:
        return BOT
    return kind


def _context(
    window: Window,
    rendered: RenderedWindow,
    refs: Mapping[str, _Ref],
    entities: Mapping[str, Mapping[str, object]],
    lexicon: Lexicon,
    parents: Mapping[str, tuple[str, ...]] | None = None,
) -> _Window:
    lines = tuple(ref.folded for ref in refs.values())
    noted: set[str] = set()
    for ref_id, pairs in window_notes(rendered).items():
        ref = refs.get(ref_id)
        if ref is None:
            continue
        for mention, name in pairs:
            if fold(mention) and fold(mention) in ref.folded:
                noted.add(fold(name))
    speakers = tuple(sorted({block.speaker for block in window.blocks}))
    times = Times()
    seen: set[tuple[str, datetime]] = set()
    for ref in refs.values():
        if (ref.shown, ref.block.said_at) not in seen:
            seen.add((ref.shown, ref.block.said_at))
            times.update(sentence_times(ref.shown, ref.block.said_at, said_day=False))
    types = {key: _entity_type(entity, lexicon, speakers) for key, entity in entities.items()}
    names = {
        key: tuple(sorted({fold(str(entity.get(part) or "")) for part in ("name", "mention")} - {""}, key=len, reverse=True))
        for key, entity in entities.items()
    }
    return _Window(
        lines=lines,
        noted=frozenset(noted - {""}),
        speakers=speakers,
        speaker_keys=_speaker_keys(speakers),
        times=times,
        last_said=max(block.said_at for block in window.blocks),
        partitions=tuple(window.scope.partitions),
        entities=entities,
        types=types,
        names=names,
        parents=dict(parents or {}),
    )


def _parents(raw_relations: Sequence[object]) -> dict[str, tuple[str, ...]]:
    """这个窗口 L3 抽的隶属于里，没结束、没标问题的：主体 key → 客体 key（一个部门写了几家的都留着）。"""
    found: dict[str, list[str]] = {}
    for raw in raw_relations:
        if not isinstance(raw, Mapping) or raw.get("relation") != PART_OF or raw.get("state") == ENDED or _problems(raw):
            continue
        subject, obj = raw.get("subject"), raw.get("object")
        if isinstance(subject, str) and isinstance(obj, str) and subject != obj and obj not in found.get(subject, []):
            found.setdefault(subject, []).append(obj)
    return {key: tuple(values) for key, values in found.items()}


def _ancestors(key: str, ctx: _Window) -> list[str]:
    """从 key 顺着这个窗口的隶属于一层层往上找到的那几家（不含它自己；绕回来的停）。"""
    found: list[str] = []
    todo = list(ctx.parents.get(key, ()))
    while todo:
        one = todo.pop(0)
        if one == key or one in found:
            continue
        found.append(one)
        todo.extend(ctx.parents.get(one, ()))
    return found


# ---------- L3 的问题、结构 ----------


def _problems(raw: Mapping[str, object]) -> list[str]:
    value = raw.get("problems") or []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(problem) for problem in value if problem]
    return ["bad_shape"]


def _shape_problem(raw: Mapping[str, object]) -> str | None:
    """L3 的结果结构不对（手写的、旧库里的）：不往下核，按 L3 的问题隔离。"""
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(item, Mapping) for item in evidence):
        return "bad_shape"
    if not all(isinstance(raw.get(name), str) for name in ("subject", "relation", "object")):
        return "bad_shape"
    state, certainty = raw.get("state"), raw.get("certainty")
    if not isinstance(state, str) or state not in _STATES or not isinstance(certainty, str) or certainty not in _CERTAINTIES:
        return "bad_shape"
    if not all(isinstance(raw.get(name), (str, type(None))) for name in ("valid_from", "valid_to", "kinship", "speaker")):
        return "bad_shape"
    return None


# ---------- R1 原句逐字可查 ----------


def _check_proofs(result: RelationCheck, refs: Mapping[str, _Ref]) -> None:
    seen: set[str] = set()
    for proof in result.raw.get("evidence") or []:  # type: ignore[union-attr]
        assert isinstance(proof, Mapping)
        found, failure = check_evidence(dict(refs), proof)
        if failure is not None:
            result.dropped.append((proof, Finding("R1", failure.code) if failure.rule == "G1" else failure))
            continue
        for item in found:
            if item.sentence_id in seen:
                continue
            seen.add(item.sentence_id)
            result.proofs.append(item)
            if item.note:
                result.notes.append(f"R1:{item.note}")
    if not result.proofs:
        failures = [finding for _, finding in result.dropped] or [Finding("R1", "quote_not_found")]
        result.failures.append(next((one for one in failures if one.rule == "R1"), failures[0]))


# ---------- R2 两头在原文里出现 ----------

# 拼起来的名字按这些拆开（“某票 累购”“A/B/C FCN（30 万）”）
_PIECES = re.compile(r"[\s/／|｜+＋、,，;；()（）\[\]［］【】<>《》@&＆·・]+")


def _owner(name: str, owner_keys: Collection[str]) -> bool:
    """这个名字是不是主人：L3 写“主人”“记忆主人”这些，或者配置里主人的名字（D62，merge.owner_by_name）。"""
    key = name_key(name)
    return bool(key) and (key in {name_key(label) for label in OWNER_NAMES} or owner_by_name(name, owner_keys))


def _aliases_present(entity: Mapping[str, object], kind: str, ctx: _Window, lexicon: Lexicon) -> bool:
    """别名表、Bot 名单认得这个名字，它的别的叫法在窗口里（或者那个 Bot 就是说话人）；〔指〕标过这个名字。"""
    texts = [str(entity.get(part) or "").strip() for part in ("name", "mention")]
    texts = [text for text in texts if text]
    if any(fold(text) in ctx.noted for text in texts):
        return True
    for text in texts:
        group = lexicon.group_of(text, kind, ctx.partitions)
        if group is not None and any(ctx.anywhere(fold(name)) for name in group.names):
            return True
        if any(ctx.anywhere(key) for key in lexicon.same_as(name_key(text), kind, name_key)):  # 第一版别名表
            return True
    bot = lexicon.bot_of(str(entity.get("name") or ""), str(entity.get("mention") or ""), kind, ctx.speakers)
    if bot is not None:
        return any(ctx.anywhere(fold(name)) or name_key(name) in ctx.speaker_keys for name in bot.names)
    return False


def _pieces_present(name: str, ctx: _Window) -> bool:
    pieces = [fold(piece) for piece in _PIECES.split(name)]
    pieces = [piece for piece in pieces if piece]
    return len(pieces) >= 2 and all(ctx.anywhere(piece) for piece in pieces)


def _end_present(
    entity: Mapping[str, object], kind: str, proofs: Sequence[Proof], ctx: _Window, lexicon: Lexicon,
    owner_keys: Collection[str],
) -> tuple[bool, str | None]:
    """这一头算不算在原文里出现。返回 (算不算, 记一笔的写法)。"""
    name = str(entity.get("name") or "").strip()
    mention = str(entity.get("mention") or "").strip()
    folded = [fold(text) for text in (mention, name) if text]
    folded = [text for text in folded if text]
    if _owner(name, owner_keys):
        return True, None  # ③ 主人常不写出来
    evidence = [fold(proof.shown) for proof in proofs]
    if any(text in line for text in folded for line in evidence):
        return True, None  # ① 证据那几句里
    if any(ctx.anywhere(text) for text in folded):
        return True, "elsewhere"  # ② 窗口别的行里（上文也算）
    if any(name_key(text) in ctx.speaker_keys for text in (mention, name) if text):
        return True, "speaker"  # ④ 就是说话人
    if _aliases_present(entity, kind, ctx, lexicon):
        return True, "alias"  # ⑤ 别名表、Bot 名单、〔指〕认得
    if name and _pieces_present(name, ctx):
        return True, "pieces"  # ⑥ 拼出来的名字每一段都在
    return False, None


# ---------- R3 类型 ----------


def _same_entity(one: str, other: str, ctx: _Window) -> bool:
    if one == other:
        return True
    first, second = ctx.entities.get(one), ctx.entities.get(other)
    if first is None or second is None:
        return False
    return ctx.types.get(one) == ctx.types.get(other) and bool(name_key(str(first.get("name") or ""))) and (
        name_key(str(first.get("name") or "")) == name_key(str(second.get("name") or ""))
    )


def _check_types(result: RelationCheck, ctx: _Window) -> None:
    raw = result.raw
    kind = str(raw.get("relation"))
    subject, obj = str(raw.get("subject")), str(raw.get("object"))
    result.subject_type, result.object_type = ctx.types.get(subject), ctx.types.get(obj)
    if (result.subject_type, result.object_type) not in ALLOWED.get(kind, frozenset()):
        result.failures.append(Finding("R3", "type_not_allowed"))
    if _same_entity(subject, obj, ctx):
        result.failures.append(Finding("R3", "same_ends"))
    if kind == KINSHIP and not str(raw.get("kinship") or "").strip():
        result.failures.append(Finding("R3", "kinship_missing"))


# ---------- R4 结束词 ----------

SUBJECT, OBJECT, EITHER_END = "subject", "object", "either"
BEFORE, AFTER, EITHER_SIDE = "before", "after", "either"
# 每种关系的结束词：(说法, 说的是关系的哪一头, 那一头的名字写在结束词的哪一边)。卡 1 到卡 6 第 5 项的“结束”，加 04 p4 的
# 取消、终止、不再。换人、转仓（“改由某某做”“从 A 转到 B”）是新旧替代，J4.4 判，不在这里
_ENDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "持有": (
        ("卖光", OBJECT, EITHER_SIDE), ("卖掉", OBJECT, EITHER_SIDE), ("卖出", OBJECT, EITHER_SIDE), ("卖了", OBJECT, EITHER_SIDE),
        ("清仓", OBJECT, EITHER_SIDE), ("清掉", OBJECT, EITHER_SIDE), ("平仓", OBJECT, EITHER_SIDE), ("赎回", OBJECT, EITHER_SIDE),
        ("兑付", OBJECT, EITHER_SIDE), ("取消", OBJECT, EITHER_SIDE), ("终止", OBJECT, EITHER_SIDE), ("到期", OBJECT, BEFORE),
        ("敲出", OBJECT, BEFORE), ("KO", OBJECT, BEFORE), ("不再持有", OBJECT, AFTER),
        ("sold", OBJECT, EITHER_SIDE), ("redeemed", OBJECT, EITHER_SIDE), ("matured", OBJECT, BEFORE),
        ("knocked out", OBJECT, BEFORE),
    ),
    "托管于": (
        ("卖光", SUBJECT, EITHER_SIDE), ("卖掉", SUBJECT, EITHER_SIDE), ("卖出", SUBJECT, EITHER_SIDE), ("卖了", SUBJECT, EITHER_SIDE),
        ("清仓", SUBJECT, EITHER_SIDE), ("赎回", SUBJECT, EITHER_SIDE), ("兑付", SUBJECT, EITHER_SIDE), ("到期", SUBJECT, BEFORE),
        ("敲出", SUBJECT, BEFORE), ("KO", SUBJECT, BEFORE), ("sold", SUBJECT, EITHER_SIDE), ("redeemed", SUBJECT, EITHER_SIDE),
        ("matured", SUBJECT, BEFORE), ("knocked out", SUBJECT, BEFORE),
    ),
    "负责": (
        ("不再负责", SUBJECT, BEFORE), ("不再管", SUBJECT, BEFORE), ("不管了", SUBJECT, BEFORE), ("交出去", SUBJECT, BEFORE),
        ("不再派", SUBJECT, AFTER),
    ),
    "任职于": (
        ("离职", SUBJECT, BEFORE), ("辞职", SUBJECT, BEFORE), ("辞去", SUBJECT, BEFORE), ("退休", SUBJECT, BEFORE),
        ("卸任", SUBJECT, BEFORE), ("resigned", SUBJECT, BEFORE), ("retired", SUBJECT, BEFORE),
    ),
    "客户是": (
        ("销户", OBJECT, EITHER_SIDE), ("关户", OBJECT, EITHER_SIDE), ("终止合作", OBJECT, EITHER_SIDE),
        ("不再合作", OBJECT, EITHER_SIDE), ("结束合作", OBJECT, EITHER_SIDE), ("退保", OBJECT, EITHER_SIDE), ("销了", OBJECT, EITHER_SIDE),
        ("closed the account", OBJECT, EITHER_SIDE), ("terminated", OBJECT, EITHER_SIDE),
    ),
    KINSHIP: (("离婚", EITHER_END, EITHER_SIDE), ("divorced", EITHER_END, EITHER_SIDE)),
    # 卡 7 第 5 项：划出去、撤销、合并掉、卖给了别家、不再是 X 的子公司。说的是主体（部门、分公司），只认名字在前面的
    # （“外贸部撤销了”；“撤销了外贸部的申请”不认，宁可漏）
    PART_OF: (
        ("划出去", SUBJECT, BEFORE), ("撤销", SUBJECT, BEFORE), ("裁撤", SUBJECT, BEFORE), ("合并掉", SUBJECT, BEFORE),
        ("卖给", SUBJECT, BEFORE), ("不再是", SUBJECT, BEFORE), ("spun off", SUBJECT, BEFORE), ("divested", SUBJECT, BEFORE),
    ),
}
# 自己就说了已经结束的：“卖了”“不管了”“销了”，说“不再……”就是从现在起不了，英文的过去式
_DONE_WORDS = frozenset({
    "卖了", "不管了", "销了", "不再负责", "不再管", "不再派", "不再持有", "不再合作", "交出去",
    "sold", "redeemed", "matured", "knocked out", "resigned", "retired", "terminated", "divorced",
    "不再是", "spun off", "divested",
})
_DONE_AFTER = re.compile(r"(?:了|掉了|光了|完了|成交|完成|完毕)")  # 结束词后面紧跟着的（比对写法，没有空白）
_DONE_BEFORE = re.compile(r"(?:已经|已于|已|刚刚|刚|早已|均已|都已)[^，,；;。]{0,4}$")
_NOT_BEFORE = re.compile(r"(?:[没未不别无非]|没有|还没|尚未|并未)$")  # 紧挨在前面的否定（“没卖”“未到期”“不会到期”）
_PAUSE_BEFORE = re.compile(r"(?:先|暂时|暂且|暂)$")  # “先不管了”是暂停，不是结束（D101）
# 结束词前面不远的这些：以后的事、还没定的事（比对写法里找，没有空白）
_FUTURE = re.compile(r"将|会|要|即将|准备|打算|计划|预计|预期|下周|下个月|下月|明天|明年|后天|年底|月底|届时|到时|差一步|接近|还差")
_FUTURE_EN = re.compile(r"(?a:\b(?:will|going to|expected|scheduled|due|may|might|would)\b)")  # 英文的，整小句里找
_WAITING = re.compile(r"等[^，,；;。]{0,12}确认|是否|有没有|会不会")
# 结束词后面跟着这些的是名词、价位、条件（到期日、敲出价、KO 586.37、在 KO 上、赎回申请），不是说结束了
_NOUN_AFTER = re.compile(
    r"(?:日|时|前|后|价|线|位|水平|条款|条件|机制|观察|事件|概率|风险|申请|手续|流程|的话|上|下|附近|左右|边|\d|[.:：])"
)
# 以下在原文上找（英文要按词），一小句算一次
_NOT_EN = re.compile(r"(?ai)\b(?:not|never|no|yet to|without)\b|n't\b")  # 英文的否定：结束词前面这一小句里有就不算
# 还剩着的（“还剩五百股”“主账户还在”“一部分”）：持有、托管于、客户是的结束词不算
_REMAINING = re.compile(
    r"还剩|剩下|剩余|余下|还有|仍有|仍持有|还持有|还拿着|拿着|保留|留着|其余|还在|仍在|一部分|部分|一半|一些|减仓|减持|"
    r"(?ai:\b(?:remaining|still holds?|the rest|partially|part of)\b)"
)
# 说了卖掉多少的（“卖了五百股”），不一定卖光：卖光、清仓才是全部；写着“全部”的（“500 股全部卖掉了”）照样是全部
_QUANTITY = re.compile(
    r"\d[\d,.]*\s?(?:股|张|手|份)|[一二两三四五六七八九十百千万]+\s?(?:股|张|手|份)|(?ai:\b\d[\d,.]*\s?(?:shares|units)\b)"
)
_WHOLE = re.compile(r"全部|全都|所有|统统|都|(?ai:\ball\b|\bentire)")
_SELL = frozenset({"卖掉", "卖出", "卖了", "sold"})
# 名字和结束词中间只能夹着这些：副词、日子、“了”“的”“我”、介词（在、从、于）、户头这种。夹着别的（“我同事昨天离职了”的
# “同事”、“小周的妹妹”的“妹妹”、“不再派三号机而是派 某 Bot”的“三号机而是派”），结束的是不是这个实体就说不准，不算。
# 介词后面跟着的实体名字（“从东岸证券离职了”“在海港银行已经到期了”）先拿掉再看
_FILLER = re.compile(
    r"(?:已经|已于|已|均已|都已|早已|刚刚|刚|也|都|又|均|全部|全都|全|就|被|给|了|的|我|在|于|从|自|账户|户头|账号|户|"
    r"今天|昨天|前天|上周|本周|这周|上个月|这个月|上月|本月|今早|刚才|上午|下午|晚上|早上|\d{1,4}(?:[年月日号/.\-]\d{0,4})*|"
    r"\d[\d,.]*(?:股|张|手|份)|[一二两三四五六七八九十百千万]+(?:股|张|手|份)|"
    r"has|have|had|was|were|is|are|been|already|just|the|our|my|all|now|remaining|rest|of)*"
)
_PREPOSITIONS = frozenset("在于从自向跟和与对")
_FUTURE_REACH = 8  # 以后的事的说法，最远在结束词前面几个字
_BEFORE_REACH = 30  # 结束词前后只看紧挨着的这么多字：长句子里每一处都从头切，会变成平方
_MENTION_GAP = 20  # 名字离结束词最远几个字（比对写法）
# 只有单子（FCN、累购）才会敲出：一张累购敲出、接来的股票照样拿着，KO 说的是那张单子，不是那只股票（资产）
_DEAL_ONLY = frozenset({"敲出", "KO", "knocked out"})
# 这一小句另写着别的人（客户是还有别的机构）的，结束的可能是他的（“小周把她那份卖了”）。持有只看人：
# 一句持有里写着的银行多半是放在那家、那家发的（“海港银行的 X1 票据到期了”）
_OTHER_ACTORS: dict[str, frozenset[str]] = {"持有": frozenset({PERSON}), "客户是": frozenset({PERSON, ORG})}
_PARTIAL_KINDS = frozenset({"持有", "托管于", "客户是", PART_OF})
# 隶属于的“不再是”后面要写着这些（或者那一家的名字），才是说不归它了：“不再是主要的部门”不算
_PART_OF_AFTER = re.compile(r"子公司|分公司|旗下|下属|(?ai:\bsubsidiary\b|\bpart of\b)")
_ALL_TYPES = frozenset({PERSON, ORG, PROJECT, DEAL, ASSET, BOT, "地点"})
# 找结束词的小句：带着句末的标点（问句要看问号）
_CLAUSE_PIECE = re.compile(r"[^，,；;：:。！？!?|｜\n]+[，,；;：:。！？!?|｜\n]*")


@dataclass(frozen=True)
class _Clause:
    text: str  # 原样，带着句末的标点
    folded: str
    where: tuple[int, ...]  # fold_map 给的位置
    usable: bool  # 找不找结束词：问句、条件、带语气词的、等确认的、写着以后的日子的不找
    negation_at: int  # 英文的否定最早在原文的哪儿，没有是 -1
    remaining: tuple[tuple[int, int], ...]  # 还剩着的说法在原文里的 [起, 止)
    partial_quantity: bool  # 写了多少股、多少张，又没说全部


@dataclass(frozen=True)
class _Index:
    """一小句里某几类实体的名字：按起点、按终点排好，找最近的用二分。"""

    mentions: tuple[_Mention, ...]
    starts: tuple[int, ...]  # 按起点排的
    by_end: tuple[_Mention, ...]
    ends: tuple[int, ...]
    keys: frozenset[str]


def _end_words(kind: str) -> tuple[tuple[str, str, str, str], ...]:
    """(说法, 比对写法, 哪一头, 哪一边)。"""
    return tuple((word, fold(word), end, side) for word, end, side in _ENDS.get(kind, ()))


def _word_char(char: str) -> bool:
    return char.isascii() and char.isalnum()  # 汉字的 isalnum() 也是真的，只算英文字母和数字


def _bounded(clause: str, where: Sequence[int], start: int, stop: int) -> bool:
    """比对写法里的 [start, stop)，两头是英文字母、数字的，在原文里不能紧挨着别的英文字母、数字（英文的词要整个：
    KO 不算 KOSPI 里的，NOW 不算 KNOWN 里的）。where 是 fold_map 给的位置。"""
    first, last = where[start], where[stop - 1]
    if _word_char(clause[first]) and first > 0 and _word_char(clause[first - 1]):
        return False
    return not (_word_char(clause[last]) and last + 1 < len(clause) and _word_char(clause[last + 1]))


def _later(text: str, said: datetime) -> bool:
    """这一小句写明的日子有晚于说话那天的：说的是以后的事（“2027 年 1 月到期”“8/25 到期”）。早于说话那天的日子不算
    已经发生了的根据：“6/19 那张累购在 KO 上”的 6/19 是单子的名字。"""
    times = sentence_times(text, said, said_day=False)
    today = said.date()
    return (
        any(day > today for day in times.days)
        or any((year, month) > (today.year, today.month) for year, month in times.months)
        or any(year > today.year for year in times.years)
    )


def _clauses(ctx: _Window, shown: str, said: datetime) -> list[_Clause]:
    """一句话切成小句（带着句末的标点），每一小句要看的几样先算好。同一句话几条关系共用，一个窗口里只算一次。"""
    key = ("clauses", shown, said)
    cached = ctx.cache.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    found: list[_Clause] = []
    for match in _CLAUSE_PIECE.finditer(shown):
        text = match.group(0)
        folded, where = fold_map(text)
        if not folded:
            continue
        usable = not (
            _is_question(text) or _has_condition(text) or _has_hedge(text) or _WAITING.search(text)
            or _FUTURE_EN.search(soft(text)) or _later(text, said)
        )
        negation = _NOT_EN.search(text)
        found.append(_Clause(
            text, folded, tuple(where), usable,
            negation.start() if negation is not None else -1,
            tuple(one.span() for one in _REMAINING.finditer(text)),
            bool(_QUANTITY.search(text)) and not _WHOLE.search(text),
        ))
    ctx.cache[key] = found
    return found


def _index(clause: _Clause, ctx: _Window, wanted: frozenset[str]) -> _Index:
    """这一小句里写着的、类型在 wanted 里的实体（位置是比对写法里的）。一样的地方几个实体都写着的（“甲”和“甲累购”），
    长的盖住短的。"""
    key = ("index", clause.text, wanted)
    cached = ctx.cache.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    found: list[_Mention] = []
    for entity, names in ctx.names.items():
        if ctx.types.get(entity) not in wanted:
            continue
        for name in names:
            position = clause.folded.find(name)
            while position >= 0:
                if _bounded(clause.text, clause.where, position, position + len(name)):
                    found.append(_Mention(entity, position, position + len(name)))
                position = clause.folded.find(name, position + 1)
    found.sort(key=lambda one: (one.start, -(one.end - one.start)))
    kept: list[_Mention] = []
    reach = -1  # 前面留下的名字最远到哪：按起点排好以后，被盖住的就是终点没超过它的
    for one in found:
        if one.end <= reach:
            continue
        kept.append(one)
        reach = one.end
    by_end = sorted(kept, key=lambda one: one.end)
    index = _Index(tuple(kept), tuple(one.start for one in kept), tuple(by_end), tuple(one.end for one in by_end),
                   frozenset(one.key for one in kept))
    ctx.cache[key] = index
    return index


def _gap_ok(clause: _Clause, ctx: _Window, low: int, high: int) -> bool:
    """名字和结束词中间这一段只夹着 _FILLER；介词后面跟着的实体名字（“从东岸证券”）先拿掉。"""
    if high - low > _MENTION_GAP:
        return False
    gap = clause.folded[low:high]
    if _FILLER.fullmatch(gap):
        return True
    every = _index(clause, ctx, _ALL_TYPES)
    pieces, cursor = [], low
    for position in range(bisect_left(every.starts, low), bisect_left(every.starts, high)):
        one = every.mentions[position]
        if one.end > high or one.start < cursor or one.start == 0 or clause.folded[one.start - 1] not in _PREPOSITIONS:
            continue
        pieces.append(clause.folded[cursor:one.start])
        cursor = one.end
    pieces.append(clause.folded[cursor:high])
    return bool(_FILLER.fullmatch("".join(pieces)))


def _nearest(clause: _Clause, ctx: _Window, wanted: frozenset[str], start: int, stop: int, side: str) -> _Mention | None:
    """结束词那一边离它最近的名字，中间只夹着副词、日子这些（_gap_ok）。"""
    index = _index(clause, ctx, wanted)
    best: tuple[int, int, _Mention] | None = None
    if side in (BEFORE, EITHER_SIDE):  # 终点在 [start - 20, start] 的
        for position in range(bisect_left(index.ends, start - _MENTION_GAP), bisect_right(index.ends, start)):
            one = index.by_end[position]
            if one.start > 0 and clause.folded[one.start - 1] in _PREPOSITIONS:
                continue  # 介词后面的名字是地方、对象（“X1 票据在海港银行已经到期了”到期的是票据）
            if _gap_ok(clause, ctx, one.end, start) and (best is None or (start - one.end, 0) < best[:2]):
                best = (start - one.end, 0, one)
    if side in (AFTER, EITHER_SIDE):  # 起点在 [stop, stop + 20] 的
        for position in range(bisect_left(index.starts, stop), bisect_right(index.starts, stop + _MENTION_GAP)):
            one = index.mentions[position]
            if _gap_ok(clause, ctx, stop, one.start) and (best is None or (one.start - stop, 1) < best[:2]):
                best = (one.start - stop, 1, one)
    return best[2] if best is not None else None


def _types_at(kind: str, end: str) -> frozenset[str]:
    pairs = ALLOWED.get(kind, frozenset())
    if end == SUBJECT:
        return frozenset(subject for subject, _ in pairs)
    if end == OBJECT:
        return frozenset(obj for _, obj in pairs)
    return frozenset(subject for subject, _ in pairs) | frozenset(obj for _, obj in pairs)


def _partial(pieces: Sequence[_Clause], at: int, ctx: _Window, stop: int, attached: _Mention) -> bool:
    """只结束了一部分：这一小句结束词后面写着还剩着的（“卖了五百股还剩五百股”；“sold the remaining X2”“卖了剩下的 X2”
    这种紧挨在名字前面的不算），或者紧跟着的下一小句写着还剩着的、又没写别的实体（“，还剩五百股”“，主账户还在”；
    “，X5 还在”说的是 X5）。结束词前面的“剩下的 X2 也卖了”是剩下的也卖了，不算。"""
    clause = pieces[at]
    after_word = clause.where[stop - 1] + 1
    mention_at = clause.where[attached.start] if attached.start >= stop else -1
    for low, high in clause.remaining:
        if low < after_word:
            continue
        if mention_at >= 0 and high <= mention_at and mention_at - high <= 4:
            continue
        return True
    if at + 1 < len(pieces):
        following = pieces[at + 1]
        if following.remaining and not _index(following, ctx, _ALL_TYPES).mentions:
            return True
    return False


def _ended_here(result: RelationCheck, ctx: _Window) -> bool:
    """证据里有说的就是这一条、都结束了、已经发生了的结束词。"""
    raw = result.raw
    kind = str(raw.get("relation"))
    words = _end_words(kind)
    if not words:
        return False
    ends = {SUBJECT: str(raw.get("subject")), OBJECT: str(raw.get("object"))}
    seen: set[tuple[str, datetime]] = set()
    for proof in result.proofs:
        if (proof.shown, proof.said_at) in seen:
            continue
        seen.add((proof.shown, proof.said_at))
        pieces = _clauses(ctx, proof.shown, proof.said_at)
        for at, clause in enumerate(pieces):
            if not clause.usable:
                continue
            for word, needle, end, side in words:
                position = clause.folded.find(needle)
                while position >= 0:
                    if _applies(kind, word, pieces, at, position, position + len(needle), end, side, ends, ctx):
                        return True
                    position = clause.folded.find(needle, position + 1)
    return False


def _applies(
    kind: str, word: str, pieces: Sequence[_Clause], at: int, start: int, stop: int, end: str, side: str,
    ends: Mapping[str, str], ctx: _Window,
) -> bool:
    """第 at 小句里 folded[start:stop] 这个结束词，说的是不是这一条、是不是都结束了、是不是已经发生了。"""
    clause = pieces[at]
    folded = clause.folded
    before, after = folded[max(0, start - _BEFORE_REACH):start], folded[stop:stop + _BEFORE_REACH]
    if word.isascii():
        if not _bounded(clause.text, clause.where, start, stop):
            return False  # 英文的结束词要整个词：KOSPI、sold-out 这种不算
        if 0 <= clause.negation_at < clause.where[start]:
            return False  # “have not sold”“never matured”：前面这一小句里有否定
    if _NOT_BEFORE.search(before) or _PAUSE_BEFORE.search(before) or _NOUN_AFTER.match(after):
        return False
    if _FUTURE.search(before[-_FUTURE_REACH:]):
        return False
    if not (word in _DONE_WORDS or _DONE_AFTER.match(after) or _DONE_BEFORE.search(before)):
        return False
    if word in _SELL and clause.partial_quantity:
        return False  # 卖了多少，不一定卖光
    wanted = frozenset({DEAL}) if word in _DEAL_ONLY else _types_at(kind, end)
    attached = _nearest(clause, ctx, wanted, start, stop, side)
    if attached is None:
        return False  # 说的是不建实体的东西，或者这一小句里说不准是哪个：什么都不结束
    targets = [ends[SUBJECT], ends[OBJECT]] if end == EITHER_END else [ends[end]]
    if not any(_same_entity(attached.key, target, ctx) for target in targets):
        return False
    if kind in _PARTIAL_KINDS and _partial(pieces, at, ctx, stop, attached):
        return False
    if kind == "任职于":  # 这一小句写着公司的，要是这一家（“甲辞去 A 的职位，加入 B”）；连的是部门的，它往上的那家也算（D210）
        companies = _index(clause, ctx, frozenset({ORG})).keys
        employers = (ends[OBJECT], *_ancestors(ends[OBJECT], ctx))
        if companies and not any(_same_entity(key, one, ctx) for key in companies for one in employers):
            return False
    if kind == PART_OF and word == "不再是":  # 要说的是归哪家：后面写着那一家，或者“子公司、分公司、旗下、下属”
        named = any(one.start >= stop and _same_entity(one.key, ends[OBJECT], ctx)
                    for one in _index(clause, ctx, frozenset({ORG})).mentions)
        if not named and not _PART_OF_AFTER.search(clause.text[clause.where[stop - 1] + 1:]):
            return False
    actors = _OTHER_ACTORS.get(kind)
    if actors is not None:  # 另写着别的人（客户是还有别的机构）：卖掉、销户的可能是他
        known = (ends[SUBJECT], ends[OBJECT], attached.key)
        for key in _index(clause, ctx, actors).keys:
            if not any(_same_entity(key, one, ctx) for one in known):
                return False
    return True


def _bounds(value: Occurred) -> tuple[date, date] | None:
    """这个日子最早、最晚可能是哪一天。"""
    try:
        if value.precision in ("minute", "day"):
            day = date(value.year, value.month or 1, value.day or 1)
            return day, day
        if value.precision == "month":
            first = date(value.year, value.month or 1, 1)
            following = date(value.year + (value.month or 1) // 12, (value.month or 1) % 12 + 1, 1)
            return first, following - timedelta(days=1)
        if value.precision == "quarter":
            quarter = value.quarter or 1
            first = date(value.year, 3 * quarter - 2, 1)
            following = date(value.year + quarter // 4, (3 * quarter) % 12 + 1, 1)
            return first, following - timedelta(days=1)
        return date(value.year, 1, 1), date(value.year, 12, 31)
    except ValueError:
        return None


def _check_ending(result: RelationCheck, ctx: _Window) -> None:
    state = result.raw.get("state")
    if state in (ACTIVE, PAUSED) and _ended_here(result, ctx):
        result.failures.append(Finding("R4", "ended_but_active"))
    if state == ENDED and result.valid_to is not None:
        parsed = parse_occurred(result.valid_to)
        bounds = _bounds(parsed) if parsed is not None else None
        if bounds is not None and bounds[0] > ctx.last_said.date():
            result.failures.append(Finding("R4", "ends_in_future"))


# ---------- R5 日子 ----------


def _evidence_times(proofs: Sequence[Proof]) -> Times:
    """证据那几句对得上的日子：写明的、按说话时间换算的相对说法（细的对上了粗的也算）；一句里一个日子都没写的，加上
    说话那天、那一刻（当场就是开始），和 G6 一样。写了日子的那句（“上周三买的”）说话那天就不算。说话那天只算那一天、
    那一刻：降精度降到说话那个月、那一年的不算有根据（11 月说起的仓位，不一定是 11 月、今年买的）。"""
    times = Times()
    seen: set[tuple[str, datetime]] = set()
    for proof in proofs:
        if (proof.shown, proof.said_at) not in seen:
            seen.add((proof.shown, proof.said_at))
            written = sentence_times(proof.shown, proof.said_at, said_day=False)
            times.update(written)
            if written.empty:
                times.update(said_times([proof.said_at]))  # 只到天、那一刻，不带月、年
    return times


def _check_date(
    value: object, which: str, evidence: Times, ctx: _Window, offset: timedelta | None
) -> tuple[str | None, str, list[str]]:
    """一个日子（开始或结束）：返回 (留下的写法, 精度, notes)。照 verify.check_time 管 occurred_at 的办法。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "unknown", []
    parsed = parse_occurred(value)
    local = parsed.local(offset) if parsed is not None else None
    if local is None:
        return None, "unknown", [f"R5:{which}_dropped"]
    precision: str | None = local.precision
    while precision is not None:
        lowered = [f"R5:{which}_lowered"] if precision != local.precision else []
        if _matches(local, precision, evidence):
            return local.at(precision), precision, lowered
        if _matches(local, precision, ctx.times):
            return local.at(precision), precision, [f"R5:{which}_elsewhere", *lowered]
        precision = _COARSER[precision]
    return None, "unknown", [f"R5:{which}_dropped"]


def _check_dates(result: RelationCheck, ctx: _Window) -> None:
    evidence = _evidence_times(result.proofs)
    offset = result.proofs[0].said_at.utcoffset()
    result.valid_from, result.valid_from_precision, notes = _check_date(result.raw.get("valid_from"), "start", evidence, ctx, offset)
    result.notes.extend(notes)
    result.valid_to, result.valid_to_precision, notes = _check_date(result.raw.get("valid_to"), "end", evidence, ctx, offset)
    result.notes.extend(notes)
    if result.valid_from is not None and result.valid_to is not None:
        start, stop = parse_occurred(result.valid_from), parse_occurred(result.valid_to)
        first, last = (_bounds(start) if start else None), (_bounds(stop) if stop else None)
        if first is not None and last is not None and last[1] < first[0]:
            result.valid_to, result.valid_to_precision = None, "unknown"
            result.notes.append("R5:end_before_start")


# ---------- R6 说话人、确定程度 ----------


def _check_speaker(result: RelationCheck, refs: Mapping[str, _Ref], owner_keys: Collection[str]) -> None:
    raw = result.raw
    result.status = VERIFIED if raw.get("certainty") == "stated" else INFERRED
    if all(proof.kind == QUOTED_ONLY for proof in result.proofs):  # 证据全是只在引用里看到过的话（D101）
        result.status = INFERRED
    speaker = str(raw.get("speaker") or "").strip()
    result.attributed_to = speaker or None
    if not speaker or not _owner(speaker, owner_keys):
        return
    owner_said = any(proof.speaker_role == ROLE_OWNER for proof in result.proofs) or any(
        _owner(str(refs[proof.ref].line.relayed_from or ""), owner_keys) for proof in result.proofs if proof.ref in refs
    )
    if owner_said:
        return
    first = min(result.proofs, key=lambda proof: (proof.said_at, proof.sentence_id))
    result.attributed_to = first.speaker
    result.status = INFERRED
    result.notes.append("R6:not_owner")


# ---------- 一条、一个窗口 ----------


def check_relation(
    index: int,
    raw: Mapping[str, object],
    refs: Mapping[str, _Ref],
    ctx: _Window,
    *,
    lexicon: Lexicon = EMPTY,
    owner_keys: Collection[str] = (),
) -> RelationCheck:
    result = RelationCheck(index=index, raw=raw)
    problems = _problems(raw)
    if problems:  # L3 换编号时就标了问题的，直接隔离（和事实一样）
        result.failures.extend(Finding("L3", problem) for problem in problems)
        return result
    shape = _shape_problem(raw)
    if shape is not None:
        result.failures.append(Finding("L3", shape))
        return result
    if str(raw.get("relation")) not in ALLOWED:
        result.failures.append(Finding("R3", "type_not_allowed"))
        return result
    _check_proofs(result, refs)
    if not result.proofs:
        return result
    _check_dates(result, ctx)  # R5 在 R4 前面：R4 看的结束日子是核过的
    for end in ("subject", "object"):
        key = str(raw.get(end))
        entity = ctx.entities.get(key, {})
        present, note = _end_present(entity, ctx.types.get(key, ""), result.proofs, ctx, lexicon, owner_keys)
        if not present:
            result.failures.append(Finding("R2", f"{end}_not_in_text"))
        elif note is not None:
            result.notes.append(f"R2:{end}_{note}")
    _check_types(result, ctx)
    _check_ending(result, ctx)
    _check_speaker(result, refs, owner_keys)
    result.failures.sort(key=lambda finding: RULE_ORDER.index(finding.rule))
    result.notes = sorted(set(result.notes))
    return result


def check_relations(
    window: Window,
    rendered: RenderedWindow,
    extraction: Mapping[str, object],
    refs: Mapping[str, _Ref],
    entities: Mapping[str, Mapping[str, object]],
    *,
    lexicon: Lexicon = EMPTY,
    owner_names: Collection[str] = (),
) -> list[RelationCheck]:
    """一个窗口里 L3 的每条关系。entities 是 verify_window 留下的实体（key 重复的、有问题的去掉了），refs 是窗口的每一行。
    owner_names 是主人的名字的比对写法（merge.load_owner_names，D62）。"""
    raw_relations = extraction.get("relations")
    if not isinstance(raw_relations, list) or not raw_relations:
        return []
    ctx = _context(window, rendered, refs, entities, lexicon, _parents(raw_relations))
    return [
        check_relation(index, raw, refs, ctx, lexicon=lexicon, owner_keys=owner_names)
        if isinstance(raw, Mapping)
        else RelationCheck(index=index, raw={}, failures=[Finding("L3", "bad_shape")])
        for index, raw in enumerate(raw_relations)
    ]


def summarize_relations(checks: Iterable[Sequence[RelationCheck]]) -> dict[str, object]:
    """这一批关系的数：多少条、过了几条、每条规则拦下多少（一条没过几条规则就各算一次）、按第一条没过的规则、原因、
    过了的关系上的记号（改了日子、说法出自谁这些）、去掉的证据、确定程度。不抄原文。"""
    relations = [relation for window in checks for relation in window]
    by_rule: dict[str, int] = {}
    by_code: dict[str, int] = {}
    first: dict[str, int] = {}
    notes: dict[str, int] = {}
    dropped: dict[str, int] = {}
    for relation in relations:
        for rule in sorted({finding.rule for finding in relation.failures}, key=RULE_ORDER.index):
            by_rule[rule] = by_rule.get(rule, 0) + 1
        for code in sorted({finding.as_text() for finding in relation.failures}):
            by_code[code] = by_code.get(code, 0) + 1
        if relation.first_rule is not None:
            first[relation.first_rule] = first.get(relation.first_rule, 0) + 1
        if relation.passed:
            for note in relation.notes:
                notes[note] = notes.get(note, 0) + 1
            for _, finding in relation.dropped:
                dropped[finding.as_text()] = dropped.get(finding.as_text(), 0) + 1
    passed = [relation for relation in relations if relation.passed]
    return {
        "relations": len(relations),
        "passed": len(passed),
        "quarantined": len(relations) - len(passed),
        "by_rule": {rule: by_rule[rule] for rule in RULE_ORDER if rule in by_rule},
        "by_first_rule": {rule: first[rule] for rule in RULE_ORDER if rule in first},
        "by_code": dict(sorted(by_code.items())),
        "evidence_dropped_from_passed": dict(sorted(dropped.items())),
        "notes_on_passed": dict(sorted(notes.items())),
        "status": {
            VERIFIED: sum(1 for relation in passed if relation.status == VERIFIED),
            INFERRED: sum(1 for relation in passed if relation.status == INFERRED),
        },
    }
