"""L1 规整：认出原文里哪些不是新的一手内容。

原文不动，句子不重切。L1 给每一句原文句（javmem.raw.iter_sentences 切出来的，编号和原文索引一样）
贴一个标签，说它是什么、能不能当抽取的证据。L2 按标签切窗，L3 只从能当证据的句子里抽。

标签：

| 标签 | 是什么 | 在窗口里 |
| --- | --- | --- |
| new | 新说的话、新写的内容 | 能当证据 |
| header | 邮件头：主题、发件人、收件人、抄送、密送、附件名 | 能当证据 |
| forwarded | 邮件里转发的内容，按转述处理 | 能当证据 |
| attachment | 附件的正文，接在它属于的那句话、那封信后面 | 能当证据 |
| signature_info | 发件人自己的签名里写公司、职位、部门的那几行（D101） | 能当证据 |
| quoted_only | 邮件引的旧信里新写的话，整本账本里找不到原话的，最早的那一份（D101，哪几句由 load_queued 定） | 能当证据 |
| quoted | 引用的旧话：对话的 > [引用 …]，邮件的 > 行和原始邮件块 | 只帮着理解 |
| quote_header | 引用上面那句 “On … wrote:”，“-----Original Message-----”、网易的“---- Replied Message ----”和它下面的邮件头 | 只帮着理解 |
| signature | 邮件签名：-- 之后，结尾的 Best,/谢谢 到引用抬头之间的署名，“发自我的 iPhone”，免责声明，[cid:…] 图片占位 | 只帮着理解 |
| forward_header | 转发块的分隔线和它下面的邮件头 | 只帮着理解 |
| restatement | Bot 把前面别人刚说的话又说了一遍 | 只帮着理解 |
| attachment_ref | 只有附件写法、没有别的话：[user attachment] 名字、[图片:名字]、[文件:名字] | 只帮着理解 |
| protocol | Bot 之间同步用的消息：整句话以 [全大写_带下划线] 的标记开头 | 不进窗口 |
| machine_event | 整句话是一段 {"type": …} 的 JSON：系统事件、工具的审批记录 | 不进窗口 |
| withdrawn | 单条撤回了的句子 | 不进窗口 |

附件小节的说明不进窗口：它是 Bot 写的，查询接口里它的 origin 只有原文句会有。附件的正文进窗口，
跟着它属于的那句话（对话）、那封信（邮件）：接在那个 Unit 的后面，同进同出，太长了一起按句子拆开。
那句话、那封信整个撤回了（每一句都是 withdrawn），附件的正文也是 withdrawn。
摘要小节解析时就不在 Packet 里，它不算证据。

原文句的 origin（said、relayed、external）原样往后传，不在这里改。邮件整封都是 external，
附件的正文也是 external，转发块里的内容另外标 forwarded，并记下转发块里写的原作者。

邮件的主题、发件人那一句撤回了、信里别的句子还在的（按事实撤回，D73 第 16 条），这封信的 Unit 的说话人、
往后传的主题写成“[已撤回]”：窗口里这封信和它的附件的抬头写的是说话人。
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from datetime import datetime, timedelta

from javmem.packet.model import ROLE_BOT, Address, ChatSection, MailSection, Packet, PacketHeader
from javmem.raw.index import copy_form, copy_key
from javmem.raw.sentences import (
    MAIL_WIDE_FIELDS,
    MASKED,
    SECTION_ATTACHMENT,
    Sentence,
    iter_sentences,
    split_sentences,
)

# l1-v7：认网易邮箱的回复抬头、转发抬头（旧信头不带冒号），原始邮件块里套着的转发块也是旧信；
# 签名放宽（结尾问候到引用抬头之间的几行，中间可以有空行，邮编门牌号不算），免责声明、[cid:…] 图片占位当签名
# l1-v8（D93）：签名收紧又补回几种（一句话带着邮箱、地址的是正文，短的决定不是名字，系统通知只标那一行；
# 聊天账号、带小写词的名字、中文的职位地址认得出），两种免责声明不吞正文，几种回复抬头，几处长行线性
# l1-v9（D96）：电话的字样写着业务字段的才不算电话（l1-v8 卡得太死，第 1 箱的签名少认了一大半）；
# 带着发、送、往的地名，会、要、将开头的短行，用 | 隔开的一串数字，都不算签名；
# 折成两行的引用抬头，上一行要写着年份和时刻才并；公司名、地址、名字加职位、名单里的会、要、将不算说话；
# 课长、店长、线长、秘书长，长的地名；结尾的“总”整行要短；写明邮件是自动发的那种通知，后面有数字的是正文；
# 签名里的字样补繁体（電話、手機、聯絡人……）
# l1-v10（D99）：折成两行的引用抬头，上一行要以尖括号邮箱、逗号、名字结尾才并；字样里认 #，日在前的日期也不当电话；
# 签名里写公司、职位、部门的那几行能当证据（signature_info，D101）；邮件引的旧信按一封信再认一遍（old_kind），
# 整本账本里找不到原话的，最早的那一份引用当证据（quoted_only，D101，哪几句由 load_queued 按账本定、传进来）
# l1-v11（第五轮）：签名里写公司、职位的那一行，混着电话、聊天账号、地名邮编、分机号的照旧只当上文（第 52 条）
# 引的旧信里又转发了一封的，转发块的作者照它的 From 认（第 53 条）；回信的人插在直接引的那一封里、写着记号的话
# （[名字]、【名字】、<<名字>>），算回信的人新写的（D103 第 6 条，第 53 条）
# l1-v12（第六轮）：认插话的记号收紧（3 个字母以上的缩写、大小写照原样、不用 @ 前面拆出来的段，>> 紧跟着字的不认，
# 第 58 条）；签名里公司职位那一条放宽（第 59 条）
L1_VERSION = "l1-v12"

NEW = "new"
HEADER = "header"
FORWARDED = "forwarded"
ATTACHMENT = "attachment"
QUOTED = "quoted"
QUOTE_HEADER = "quote_header"
SIGNATURE = "signature"
# 签名里写公司、职位、部门的那几行（D101）：能当证据，是发件人自己写的。问候、名字、电话、邮箱、网址、地址照旧是 signature
SIGNATURE_INFO = "signature_info"
FORWARD_HEADER = "forward_header"
RESTATEMENT = "restatement"
ATTACHMENT_REF = "attachment_ref"
PROTOCOL = "protocol"
MACHINE_EVENT = "machine_event"
WITHDRAWN = "withdrawn"
# 邮件引的旧信里新写的话，整本账本里找不到原话的，最早的那一份（D101）：能当证据，写给模型时按转发的写法标出原作者，
# L4 一律算推断。哪几句由 load_queued 按账本定，normalize 的 quote_evidence 传进来
QUOTED_ONLY = "quoted_only"

TARGET_KINDS = frozenset({NEW, HEADER, FORWARDED, ATTACHMENT, SIGNATURE_INFO, QUOTED_ONLY})
# 引的旧信里这几种能当证据（D101）：旧信新写的话、转发的内容、签名里写公司职位部门的行。旧信的问候、署名、
# 电话地址、免责声明、引用抬头不能
QUOTE_CONTENT = frozenset({NEW, FORWARDED, SIGNATURE_INFO})
# 当原话的（D101）：能当证据的话，邮件头不算（“Re: 主题”谁都抄），只在引用里看到过的那一份也不算
ORIGINAL_KINDS = frozenset({NEW, FORWARDED, ATTACHMENT, SIGNATURE_INFO})
CONTEXT_KINDS = frozenset({QUOTED, QUOTE_HEADER, SIGNATURE, FORWARD_HEADER, RESTATEMENT, ATTACHMENT_REF})
DROPPED_KINDS = frozenset({PROTOCOL, MACHINE_EVENT, WITHDRAWN})
ALL_KINDS = TARGET_KINDS | CONTEXT_KINDS | DROPPED_KINDS

PRIVATE_TAG = "private"


@dataclass(frozen=True, order=True)
class Scope:
    """可见范围：包的分区加包的敏感标记（query-api 3.2）。范围不同的不进同一个窗口（D30）。"""

    partitions: tuple[str, ...]
    sensitive: tuple[str, ...]  # 敏感标记，privacy 是 private 的包多一个 private

    @classmethod
    def of(cls, header: PacketHeader) -> Scope:
        return cls(tuple(sorted(set(header.domain))), tuple(sorted(set(header.tags))))

    @property
    def private(self) -> bool:
        return PRIVATE_TAG in self.sensitive

    def as_dict(self) -> dict[str, list[str]]:
        return {"partitions": list(self.partitions), "sensitive": list(self.sensitive)}


@dataclass(frozen=True)
class Line:
    """一句原文句和 L1 给它的标签。文字逐字，和原文索引里的一样。"""

    sentence_id: str
    text: str
    kind: str
    origin: str  # 原文句的 origin，原样
    quoted: bool  # 原文句的 quoted，原样
    field: str | None = None  # 邮件头字段
    line_no: int | None = None
    sentence_no: int | None = None
    relayed_from: str | None = None  # 转发块的邮件头里写的原作者；邮件的引用行是引用抬头里写的人（D101）
    restates: tuple[str, ...] = ()  # 复述的是前面哪几句
    old_kind: str | None = None  # 邮件的引用行在它引的那封旧信里是什么（new、signature……，D101）
    inserted: bool = False  # 回信的人插在引用里的话：从引用里认出来、改成 new 的（第五轮第 53 条），取包时还要过一遍撤回
    span: tuple[int, int] | None = None  # 在这句话（这封邮件）的 Unit.source_text 里的位置，打码用
    attachment: int | None = None  # 附件的正文：第几块附件（原文索引的 section_no）
    attachment_name: str | None = None  # 附件名，写给模型的抬头用

    @property
    def target(self) -> bool:
        return self.kind in TARGET_KINDS

    @property
    def dropped(self) -> bool:
        return self.kind in DROPPED_KINDS


@dataclass(frozen=True)
class MailInfo:
    message_id: str
    thread_id: str
    in_reply_to: str | None
    subject: str


@dataclass(frozen=True)
class Unit:
    """对话的一句话（连同续行和引用行），或者一封邮件（邮件头加正文）。"""

    unit_id: str  # 对话 包号#小节.第几句话，邮件 包号#第几封
    packet_id: str
    revision: int
    source: str
    channel: str
    section_no: int
    line_no: int | None  # 对话是第几句话，邮件是空的
    speaker: str
    speaker_role: str
    said_at: datetime
    origin: str
    lines: tuple[Line, ...]
    mail: MailInfo | None = None
    part: int = 0  # L2 把太长的一句话拆开时，第几段
    # 原样的全文：对话是这句话的每一行（引用行也算），邮件是邮件头的每个字段加正文的每一行，用换行连着。
    # 发给模型之前在它上面打码：密码切在两句之间、关键词在行尾而值在下一行，一句一句打认不出来
    source_text: str = ""

    @property
    def dropped(self) -> bool:
        """整句都不进窗口。"""
        return all(line.dropped for line in self.lines)


@dataclass(frozen=True)
class NormalizedPacket:
    header: PacketHeader
    scope: Scope
    units: tuple[Unit, ...]
    attachment_notes: int = 0  # 附件小节有几句，不进窗口
    kinds: dict[str, int] = field(default_factory=dict)  # 每种标签几句

    @property
    def packet_id(self) -> str:
        return self.header.packet_id


# ---------- 对话 ----------

# Bot 之间同步用的消息：整句话以 [全大写、下划线连着、_V 加版本号结尾的标记] 开头，比如 [ABC_SYNC_V1]。
# 只认这种写法，不认 Bot 的名字。[Max 的日记]、[转发 自 小李]、[MEETING_NOTES_1109] 这种都不算
_PROTOCOL = re.compile(r"\[[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+_V\d+\]")
# 系统事件、工具的审批记录：整句话就是一段 {"type": …} 的 JSON。type 是 text、message 这类的是内容，不算
_MACHINE = re.compile(r'\{\s*"type"\s*:\s*"([^"]*)"')
_CONTENT_TYPES = frozenset({"text", "message", "content", "output_text", "input_text", "markdown"})
# 只有附件、没有别的话。1.2 的写法是 [user attachment] 文件名，规范的写法是 [图片:名字]、[文件:名字]。
# 文件名里不能有中文的标点，扩展名要以字母开头：“金额是 3.5”“v2.0”不是文件名
_ATTACHMENT_ONLY = re.compile(
    r"\[(?:user attachment|attachment)\](?:\s*[^\s，。：；！？、][^\n，。：；！？、]{0,200}?\.[A-Za-z][A-Za-z0-9]{0,7})?\s*"
    r"|\[(?:图片|文件|附件)[:：][^\]\n]*\]\s*",
    re.IGNORECASE,
)
# 行首的标记（规范第 5 节）。认上面这几种之前先去掉：[外部:工具] 后面的 JSON 也是系统消息
_LEADING_MARKERS = re.compile(r"(?:\[(?:转发[^\]]*|外部[^\]]*|语音)\]\s*)+")

# 复述：Bot 的一句话和前面别人刚说的某一句差不多一样。按词对（中文一个字一个词，英文和数字一串一个词）算，
# Bot 这句里有多少词对在那一句里出现过。开头是“收到、好的”这类应答的，门槛低一点。
# Bot 这句里有那一句没有的数字或英文（算出来的总价、改了的行权价），就不是复述。
# 宁可少认：认错了，Bot 说的新内容就只剩上文、抽不到了；漏认了，只是多一条 Bot 说的事实
_TOKEN = re.compile(r"[0-9A-Za-z]+|[㐀-䶿一-鿿豈-﫿]")
_ACK = re.compile(r"(?:收到|好的|好|明白|了解|知道了|记下了|记住了|ok|okay|嗯)(?:[，,。！!：:\s]|$)", re.IGNORECASE)
RESTATE_MIN_PAIRS = 6  # 太短的不判，“直接看邮箱。”这种和哪句话都容易撞上
RESTATE_RATIO = 0.8
RESTATE_RATIO_AFTER_ACK = 0.6
RESTATE_LOOKBACK = 6  # 往前看别人的几句话
RESTATE_HORIZON = timedelta(hours=2)

_QUOTE_PREFIX = re.compile(r"^(?:>\s?)+")
_QUOTE_TAG = re.compile(r"^\[引用[^\]]*\]\s*")
_SPACES = re.compile(r"\s+")


def normal_text(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def quote_body(text: str) -> str:
    """引用行去掉开头的 > 和 [引用 时间 说话人]，剩下的是引用的原话。"""
    return normal_text(_QUOTE_TAG.sub("", _QUOTE_PREFIX.sub("", text)))


def _strip_markers(text: str) -> str:
    match = _LEADING_MARKERS.match(text)
    return text[match.end() :] if match else text


def _pairs(text: str) -> frozenset[tuple[str, str]]:
    tokens = [token.lower() for token in _TOKEN.findall(text)]
    return frozenset(zip(tokens, tokens[1:], strict=False))


def _ascii_tokens(text: str) -> frozenset[str]:
    return frozenset(token.lower() for token in _TOKEN.findall(text) if token.isascii())


@dataclass(frozen=True)
class _Own:
    """别人说过的一句，复述拿它比。撤回了的也留着：Bot 复述撤回了的话，要一起挡住。"""

    sentence_id: str
    pairs: frozenset[tuple[str, str]]
    ascii: frozenset[str]
    withdrawn: bool


@dataclass
class _Said:
    said_at: datetime
    sentences: list[_Own]


def _restates(text: str, recent: deque[_Said], said_at: datetime, after_ack: bool) -> _Own | None:
    """复述的是前面哪一句；不是复述返回 None。一句一句比，几句拼起来比的话什么都像。"""
    pairs = _pairs(text)
    if len(pairs) < RESTATE_MIN_PAIRS:
        return None
    ascii_tokens = _ascii_tokens(text)
    threshold = RESTATE_RATIO_AFTER_ACK if after_ack else RESTATE_RATIO
    best: tuple[float, _Own | None] = (0.0, None)
    for said in recent:
        if not timedelta(0) <= said_at - said.said_at <= RESTATE_HORIZON:
            continue
        for own in said.sentences:
            ratio = len(pairs & own.pairs) / len(pairs)
            if ratio >= threshold and ratio > best[0] and ascii_tokens <= own.ascii:
                best = (ratio, own)
    return best[1]


def _utterance_kind(texts: list[str]) -> str | None:
    """整句话都不进窗口的两种。texts 是不算引用行的每一行，行首的标记去掉以后再看。"""
    if not texts:
        return None
    first = _strip_markers(texts[0])
    if _PROTOCOL.match(first):
        return PROTOCOL
    whole = "\n".join([first, *texts[1:]]).strip()
    match = _MACHINE.match(whole)
    if whole.startswith("{") and whole.endswith("}") and match and match.group(1).lower() not in _CONTENT_TYPES:
        return MACHINE_EVENT
    return None


def withdrawn_quote(text: str, withdrawn_texts: Collection[str]) -> bool:
    """引用的是撤回了的话：一样的、被截短了的（末尾 … 或者 ...）都算。"""
    body = quote_body(text)
    if body.endswith("…"):
        body = body[:-1].rstrip()
    elif body.endswith("..."):
        body = body[:-3].rstrip()
    return len(body) >= 4 and any(body in withdrawn_text for withdrawn_text in withdrawn_texts)


def _locate(texts: list[str], sentences: list[Sentence]) -> list[tuple[int, int]] | None:
    """每一句在 texts 用换行连起来的全文里的位置。原文句是 split_sentences 从各行切出来的，按顺序找。"""
    spans: list[tuple[int, int]] = []
    offset = 0
    index = 0
    for text in texts:
        cursor = 0
        for part in split_sentences(text):
            if index >= len(sentences) or sentences[index].text != part:
                return None
            start = text.find(part, cursor)
            if start < 0:
                return None
            spans.append((offset + start, offset + start + len(part)))
            cursor = start + len(part)
            index += 1
        offset += len(text) + 1
    return spans if index == len(sentences) else None


def _chat_units(
    packet: Packet, groups: dict[tuple[int, int], list[Sentence]], withdrawn: Collection[str]
) -> Iterator[Unit]:
    header = packet.header
    recent: deque[_Said] = deque(maxlen=RESTATE_LOOKBACK)
    withdrawn_texts = {
        normal_text(sentence.text)
        for sentences in groups.values()
        for sentence in sentences
        if sentence.sentence_id in withdrawn
    }
    for section in packet.sections:
        assert isinstance(section, ChatSection)
        for utterance in section.utterances:
            sentences = groups.get((section.number, utterance.number), [])
            if not sentences:
                continue
            whole_kind = _utterance_kind([line.text for line in utterance.lines if line.text and not line.quoted])
            first = next((sentence.text for sentence in sentences if not sentence.quoted), "")
            after_ack = bool(_ACK.match(first))
            lines = []
            own: list[_Own] = []
            for sentence in sentences:
                restates: tuple[str, ...] = ()
                is_withdrawn = sentence.sentence_id in withdrawn
                if is_withdrawn:
                    kind = WITHDRAWN
                elif whole_kind is not None:
                    kind = whole_kind
                elif sentence.quoted:
                    # 引用的正好是撤回了的话：一起挡住，上文里也不出现
                    kind = WITHDRAWN if withdrawn_quote(sentence.text, withdrawn_texts) else QUOTED
                elif _ATTACHMENT_ONLY.fullmatch(_strip_markers(sentence.text)):
                    kind = ATTACHMENT_REF
                elif utterance.speaker_role == ROLE_BOT and (
                    match := _restates(sentence.text, recent, utterance.said_at, after_ack)
                ):
                    kind = WITHDRAWN if match.withdrawn else RESTATEMENT
                    restates = (match.sentence_id,)
                else:
                    kind = NEW
                if utterance.speaker_role != ROLE_BOT and (kind == NEW or is_withdrawn):
                    text = sentence.text
                    own.append(_Own(sentence.sentence_id, _pairs(text), _ascii_tokens(text), is_withdrawn))
                lines.append(
                    Line(
                        sentence_id=sentence.sentence_id,
                        text=sentence.text,
                        kind=kind,
                        origin=sentence.origin,
                        quoted=sentence.quoted,
                        line_no=sentence.line_no,
                        sentence_no=sentence.sentence_no,
                        restates=restates,
                    )
                )
            if own:
                recent.append(_Said(utterance.said_at, own))
            texts = [text_line.text for text_line in utterance.lines]
            spans = _locate(texts, sentences)
            if spans is not None:
                lines = [replace(line, span=span) for line, span in zip(lines, spans, strict=True)]
            yield Unit(
                unit_id=f"{header.packet_id}#{section.number}.{utterance.number}",
                packet_id=header.packet_id,
                revision=header.revision,
                source=header.source,
                channel=header.channel,
                section_no=section.number,
                line_no=utterance.number,
                speaker=utterance.speaker,
                speaker_role=utterance.speaker_role,
                said_at=utterance.said_at,
                origin=utterance.origin,
                lines=tuple(lines),
                source_text="\n".join(texts),
            )


# ---------- 邮件 ----------

# 引用行：> 后面跟空格、另一个 >，或者什么都没有。“>5% 的增长”不算
_QUOTE_LINE = re.compile(r">(?:[ >]|$)")
# 引用上面那句：On … wrote:，… 写道：。繁体、越南文的客户端也常见；西班牙文、法文、德文、意大利文、葡萄牙文的（D93）
_ATTRIBUTION = re.compile(
    r".{0,300}?(?:wrote|写道|寫道|đã viết|escribió|a écrit|schrieb|ha scritto|escreveu)\s*[:：]", re.IGNORECASE
)
# 折成两行的引用抬头，上一行的开头：On、在，西班牙文的 El、法文的 Le、德文的 Am、意大利文的 Il、葡萄牙文的 Em
_ATTRIBUTION_START = re.compile(r"(?:on|在|el|le|am|il|em)\s", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[。！？.!?；;：:]$")
# 折成两行的引用抬头，上一行写着日期：年份或者几点几分（D93 审阅第 7 条：“Il prezzo finale è 13 EUR”紧挨着
# “Mario <…> ha scritto:”时，以前被并成了引用抬头）
# 年份、时刻前后都不能挨着数字（D96 第 36 条：12000 里夹着的 2000 也算年份，“Am Montag liefern wir 12000 Kisten”并了进去）
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")
_CLOCK = re.compile(r"(?<!\d)\d{1,2}[:：]\d{2}(?!\d)")


_SHORT_DATE = re.compile(r"(?<!\d)\d{1,2}/\d{1,2}/\d{2}(?!\d)")  # Thunderbird 那种两位年份：11/3/26
_ANGLE_ADDRESS = re.compile(r"<[^<>@\s]+@[^<>\s]+>")


def _date_hint(line: str) -> bool:
    """折成两行的引用抬头，上一行写着日期：年份（短日期 11/3/26 也算）和时刻都要有（D96 第 36 条：只写着时刻、
    只写着一个数的是正文）；写着尖括号里的邮箱的，有年份就够（D96 审阅第 7 条：“On Tue, Nov 3, 2026, 名字 <…>”）。"""
    if not (_YEAR.search(line) or _SHORT_DATE.search(line)):
        return False
    return bool(_CLOCK.search(line)) or bool(_ANGLE_ADDRESS.search(line))


# 折成两行的引用抬头，上一行的结尾（D99 第 46 条）：尖括号里的邮箱（Gmail 的西班牙文写成 (<…>)）、逗号、折开的“<”，
# 或者一个名字（一到四个大写开头的词，或者两到四个汉字）。“On 11/3/26 the truck comes at 9:30 to Dock 2”
# “On 3 Nov 2026 at 10:30 we ship the crates”紧挨着下一行的“Tom wrote:”，以前并进了抬头
_ATTRIBUTION_TAIL = re.compile(
    r"(?:\(?<[^<>\s]*@[^<>\s]*>\)?|[,，<]"
    r"|(?<![\w'’.-])[A-Z][\w'’.-]*(?:[ \t]+(?:[A-Z][\w'’.-]*|van|de|da|del|der|von|bin|binti)){0,3}"
    r"|(?<![\u4e00-\u9fff])[\u4e00-\u9fff]{2,4})[ \t]*$"
)
# 只靠邮箱认出日期的（没写时刻），中间不能有长一点的小写词：星期、月份的缩写和几个小词除外
# （“On 2026 orders please copy Lena <…>”是正文）
_ATTRIBUTION_WORDS = frozenset("giorno alle janv févr juil août sept".split())


def _folded_attribution_start(line: str) -> bool:
    """折成两行的引用抬头的上一行：On、在、El 这些开头，没说完（不是句号、冒号结尾），写着日期（_date_hint），
    结尾像抬头（_ATTRIBUTION_TAIL）；只靠邮箱认日期的，中间没有长一点的小写词。"""
    if not (_ATTRIBUTION_START.match(line) and not _SENTENCE_END.search(line) and _date_hint(line)):
        return False
    if not _ATTRIBUTION_TAIL.search(line):
        return False
    if _CLOCK.search(line):
        return True
    rest = _ANGLE_ADDRESS.sub(" ", line)
    return not any(
        len(word) >= 4 and word[0].islower() and word not in _ATTRIBUTION_WORDS for word in _plain_words(rest)
    )
# 原始邮件块：这一行以下都是旧信。下划线那种要紧跟 From:/发件人：才算
# 有的客户端写成“回复的邮件信息”，下面是“X 在 … 写道：”和没加 > 的旧信（D93）
_REPLY_SEPARATOR = re.compile(
    r"-{2,}\s*(?:original message|原始邮件|原始郵件|原邮件|回复的邮件信息)\s*-{2,}", re.IGNORECASE
)
# 网易邮箱的回复抬头：一行 “---- Replied Message ----”（中文界面是“回复的原邮件”），以下都是被回的旧信，
# 旧信头不带冒号（见 _bare_header）。它只用在回复里，转发写的是 Forwarded Message，所以主题是转发的也当旧信。
# 有的信分隔线后面没换行，直接接着旧信头，所以只看行首
_REPLIED_SEPARATOR = re.compile(r"-{2,}\s*(?:replied message|回复的原邮件)\s*-{2,}", re.IGNORECASE)
_UNDERSCORE_RULE = re.compile(r"_{8,}")
# 转发块
_FORWARD_SEPARATOR = re.compile(
    r"-{2,}\s*(?:forwarded message|转发的原邮件|转发的邮件|转发邮件信息|转发邮件|已转发邮件|轉寄的郵件)\s*-{2,}"
    r"|begin forwarded message\s*[:：]?",
    re.IGNORECASE,
)
# 网易的转发抬头后面没换行、直接接着信头的（---- Forwarded Message ----From 王芳<…> Date …），见 _glued_forward
_GLUED_FORWARD = re.compile(r"-{2,}\s*(?:forwarded message|转发的原邮件|转发的邮件)\s*-{2,}(?=\s*\S)", re.IGNORECASE)
# 网易这类客户端写的旧信头、转发信头：字段名后面不带冒号，空一格写值（From 王芳<…>、发件人 王芳<…>）；
# 也有字段名单独一行、值在下一行，中间还隔着空行的；几个字段挤在一行里的（From … Date … To … Subject …）；
# 中文的字段名和值连着写的（发送日期后面紧跟着日期、抄送人后面紧跟着名字）
_BARE_FIELD = re.compile(
    r"(?P<name>from|sent|date|to|cc|bcc|subject|发件人|发送日期|发送时间|收件人|抄送人|抄送|密送|主题)"
    r"(?:(?<=[一-鿿])|(?=[\s:：]|$)|(?<=date)(?=\d))[ \t:：]*",
    re.IGNORECASE,
)
_BARE_FIELD_NAMES = {
    "发件人": "from", "sent": "date", "发送日期": "date", "发送时间": "date", "收件人": "to",
    "抄送人": "cc", "抄送": "cc", "密送": "bcc", "主题": "subject",
}
# 几个字段挤在一行里时，下一个字段从哪开始（只从一段空白的开头找，长空白也是线性的）；主题那一段在不在这一行
_BARE_NEXT_FIELD = re.compile(
    r"(?<!\s)\s+(?:date|sent|to|cc|bcc|subject)(?=\s|$)|(?<!\s)\s*(?:发送日期|发送时间|收件人|抄送人|抄送|密送|主题)", re.IGNORECASE
)
_BARE_SUBJECT = re.compile(r"(?:^|\s)(?:subject|主题)(?=[\s:：]|$)", re.IGNORECASE)
# 邮件地址：尖括号里的、[mailto:…]、裸写的。裸写的只从一串字的开头找，长串也是线性的
_ADDRESS_PART = re.compile(r"<[^<>]*>|\[mailto:[^\[\]]*\]|(?<![\w.+'-])[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_FIRST_ADDRESS = re.compile(r"<[^<>@]*@[^<>]*>|(?<![\w.+'-])[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")  # 尖括号里第一个 @ 就够
_STOP = re.compile(r"[。！？!?]")
_STOP_END = re.compile(r"[。！？.!?；;]$")
_FROM_WORDS = r"from|发件人|寄件者|từ"
_SENT_WORDS = r"sent|date|发送时间|发送日期|日期|时间|寄件日期|傳送時間|đã gửi|ngày|gửi"  # 发送日期：网易带冒号的信头（D93）
_HEADER_FIELD = re.compile(
    rf"(?:{_FROM_WORDS}|{_SENT_WORDS}|to|cc|subject|收件人|抄送|主题|收件者|副本|主旨|đến|chủ đề)\s*[:：]", re.IGNORECASE
)
_FROM_FIELD = re.compile(rf"(?:{_FROM_WORDS})\s*[:：]\s*(.+)", re.IGNORECASE)
_SENT_FIELD = re.compile(rf"(?:{_SENT_WORDS})\s*[:：]", re.IGNORECASE)
_SUBJECT_FIELD = re.compile(r"(?:subject|主题|主旨|chủ đề)\s*[:：]", re.IGNORECASE)
_ADDRESS = re.compile(r"@|<[^<>]+>|\[mailto:", re.IGNORECASE)
_FORWARD_SUBJECT = re.compile(r"\s*(?:fwd?|转发|轉寄)\s*[:：]", re.IGNORECASE)
_REPLY_SUBJECT = re.compile(r"\s*(?:re|aw|sv|回复|回覆|答复)\s*[:：]", re.IGNORECASE)
# 签名：手机客户端加的那一行，只认整行是这几种写法的
_MOBILE_SIGNATURE = re.compile(
    r"(?:sent from my [a-z0-9 .'-]{1,30}|sent from (?:mail|outlook|yahoo mail|gmail)(?: (?:for|on) [a-z0-9 ]{1,20})?"
    r"|get outlook for [a-z0-9 ]{1,20}"
    r"|(?:发自|来自|從|从)我的\s*(?:iphone|ipad|android|手机|手機|华为|小米|荣耀|oppo|vivo|三星|galaxy|移动设备|mac|电脑)"
    r"[\w ]{0,12}(?:发送)?)",
    re.IGNORECASE,
)
# 署名里不会有的：一句完整的话（句号、问号结尾；句点结尾的，除了 Co. Ltd. 这类缩写），PS、附言
_NOT_A_NAME = re.compile(r"[。！？!?；;]$|^(?:p\.?\s?s\.?|附|补充)[\s:：.]", re.IGNORECASE)
_ABBREVIATION_END = re.compile(
    r"(?:co|ltd|inc|corp|llc|plc|pte|bhd|gmbh|jr|sr|dept|no|rd|st|ave|blvd|bldg|dist|fl|rm|tel|mob|ext)\.$", re.IGNORECASE
)
# 签名（_closing_block）：结尾问候下面第一段（前面可以先空几行）是名字、职位、公司这几行，按 _name_line 看；
# 再往下隔着空行的几段，每一行都要明摆着是签名（_signature_line）：电话、邮箱、网址、地址、公司、职位。
# 一直要到引用抬头、分隔线、--（或者信的末尾）；中间有一行不像的，整个不算，问候照旧是正文。宁可少认：
# 认错了，问候下面的正文就只剩上文、抽不到；漏认了，只是多抽一条“某人的电话是……”
_PHONE_WORD = re.compile(
    r"\b(?:tel|telephone|phone|mobile|mob|cell|fax|whatsapp|wechat|skype|viber)\b|电话|手机|传真|微信|座机|電話|手機|傳真|座機",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"(?<![\w.+'-])[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_DOMAIN = re.compile(r"(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?", re.IGNORECASE)
# 地址：写着 Add:、Address:、地址的；写着邮编（P.C.、Zip、邮编）的；门牌加路名（88 Example Road、Example Rd. 88）、
# 楼层房间（Room 1203、12/F、Building 3）；中文的“路 88 号”“3 栋”“1203 室”。光写“No. 8”“3 号”“3 楼”的不算，
# “Container No. 8 is full”“下周 3 号前付款”“在 3 楼开会”是正文
_ADDRESS_LABEL = re.compile(
    r"(?i:\b(?:add|addr|address|adr)\b\s*[:：.])|地址|邮编|邮政编码|郵編|郵政編碼|郵遞區號"
    r"|(?i:\b(?:p\.?\s?c\.?|zip(?:\s?code)?|post(?:al)?\s?code)\s*(?:[:：.]\s*)?\d{4,6}(?!\d))"
)
_STREET = re.compile(
    r"(?i:\b\d+[a-z]?(?:-\d+)?,?\s+(?:\w[\w'.-]*\s+){0,4}(?:road|rd|street|st|avenue|ave|lane|ln|boulevard|blvd|drive|highway|hwy)\b)"
    r"|(?i:\b(?:road|rd|street|avenue|ave|lane|ln|blvd|highway|hwy)\.?,?\s*(?:no\.?\s*)?\d)"
    r"|(?i:\b(?:room|rm|unit|suite|ste|floor|fl|building|bldg|block|blk|tower)\.?\s*(?:#\s*)?\d)"
    r"|(?i:\b\d+\s*/\s*f\b|\b\d+(?:st|nd|rd|th)\s+floor\b)"
    r"|(?:路|街|大道|巷|弄)\s*\d+\s*号|(?<!\d)\d+\s*(?:号楼|栋|幢|座|室)"  # 只从一串数字的开头找（D93）
)
_COUNTRY = (
    r"(?:P\.?\s?R\.?\s?China|China|Viet\s?Nam|Vietnam|Thailand|Malaysia|Singapore|Indonesia|Philippines|India|Bangladesh"
    r"|Pakistan|Turkey|USA|U\.S\.A\.?|United States|Canada|Mexico|Brazil|Chile|UK|U\.K\.|United Kingdom|England|Germany"
    r"|France|Italy|Spain|Portugal|Poland|Netherlands|Belgium|Sweden|Denmark|Norway|Finland|Japan|Korea|Australia"
    r"|New Zealand|Hong Kong|Taiwan|中国)"
)
# 邮编：挨着国名、City、Province 这些字的 5、6 位数，或者“地名 邮编”这种只有两三个大写开头的词的短行
_CODE_DIGITS = r"(?<!\d)(?<!\d[.,])\d{5,6}(?!\d)(?![.,]\d)"  # 单独的 5、6 位数，不是 1,200,000 里的一段
_POSTCODE = re.compile(
    rf"{_CODE_DIGITS}[^\n]{{0,40}}\b(?:{_COUNTRY}|City|Province|District|County|State)\b"
    rf"|\b(?:{_COUNTRY}|City|Province|District|County|State)\b[^\n]{{0,40}}{_CODE_DIGITS}"
)
_PLACE_CODE = re.compile(r"(?:[A-Z][a-z]{2,}[ \t,]+){1,2}\d{5,6}|\d{5,6}(?:[ \t,]+[A-Z][a-z]{2,}){1,2}|[\u4e00-\u9fff]{2,8}\s*\d{6}")
_COUNTRY_LINE = re.compile(rf"(?:{_COUNTRY})\.?", re.IGNORECASE)
_COMPANY = re.compile(
    r"(?i:\b(?:co|ltd|inc|llc|gmbh|corp|corporation|limited|company|group|plc|pte|bhd|s\.?a|s\.?r\.?l|b\.?v|jsc|tnhh)\b\.?)"
    r"|有限公司|股份|集团|公司|工厂|厂$|贸易行|工作室"
)
# 职位、部门里的词：这些词小写也不算一句话（“Sales manager, export department”“Head of sales and marketing”）
_TITLE_WORDS = frozenset(
    "manager director president vice ceo cfo coo cto founder owner partner chairman head chief officer executive sales"
    " purchasing purchase procurement buyer buying sourcing merchandiser merchandising marketing export import exports"
    " imports trade trading business development account accounts customer service services support logistics shipping"
    " operations operation quality qc qa assurance engineer engineering designer design product products production"
    " technical assistant secretary coordinator specialist representative rep consultant agent supervisor team leader"
    " lead senior junior general department dept division office branch international overseas global regional area"
    " country key admin administration finance financial accounting accountant hr human resources legal"
    # D93 第 6 条：职位里常见的小写词（“Head of supply chain”“Founder and managing director”）
    " supply chain managing planning project projects program programme category retail wholesale factory warehouse"
    " inventory materials packaging compliance audit research digital ecommerce e-commerce communications relations"
    " brand creative analyst strategy planner deputy associate principal co-founder cofounder board member chairwoman"
    " vp svp evp gm md controller treasurer counsel attorney receptionist".split()
)
# 公司名里的词：小写写的 co.、ltd. 也不算一句话
_COMPANY_WORDS = frozenset("co ltd inc llc gmbh corp corporation limited company group plc pte bhd jsc tnhh".split())
# 电话、地址里常见的小写词（D93 审阅第 8 条）：“… ext 801 / mobile …”“… Science park, Pudong new area, …”不是一句话
_CONTACT_WORDS = frozenset(
    "ext extension mobile mob tel telephone phone fax cell office hotline direct whatsapp wechat skype viber email mail"
    " web website add addr address room floor building block road street district city province area zone park new"
    " science industrial town village county state zip code postcode tower center centre plaza avenue lane no unit suite"
    " level sec section ln rd ave blvd bldg dist fl rm".split()
)
_SIGNATURE_WORDS = _TITLE_WORDS | _COMPANY_WORDS | _CONTACT_WORDS
_ZH_TITLE = re.compile(
    r"(?:部|处|科|组|中心|经理|主管|总监|助理|专员|业务员|业务|跟单|客服|外贸|采购|销售|总"
    r"|(?:[部厂科处组班局校院所行社会队站室馆课店线]|董事|理事|秘书)长|主任|工程师|设计师|会计|出纳|老板|董事"
    r"|代表|顾问|专家|总裁|合伙人|创始人|秘书|文员|翻译|跟单员)$"
)
# 署名里不会有的一句话：英文里有小写开头的实词（“Please confirm the count by Friday”“Friday works”；
# 连接词、职位里的词不算），中文带说话、时间的字眼（请、吗、了、我们、您、已经、明天、下周……）或者中间有逗号
_WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|['’-])*")
_CONNECTIVES = frozenset(
    {"of", "and", "the", "for", "de", "du", "da", "la", "le", "van", "von", "der", "den", "y", "e", "at", "in", "on", "to"}
    # 名字里小写的那几个（D93 第 6 条）：Ahmad bin Ismail、Maria dos Santos、Paolo di Marco
    | {"bin", "binti", "di", "dos", "das", "del", "della", "al", "el", "ibn", "ter", "ten", "zu"}
    # 序数词的尾巴（5th Floor 里的 th）、越南文地址里的小写词（đường、số、phường、quận、thành phố 这些）：地址里常见，
    # 算成一句话的字眼的话，整个签名都认不出（D93，真实邮件上查出来的）
    | {"st", "nd", "rd", "th"}
    | {"đường", "số", "phường", "quận", "huyện", "tỉnh", "thành", "phố", "xã", "ấp", "thôn", "khu", "tầng", "tòa", "lầu",
       "ngõ", "ngách", "hẻm", "kiệt", "lô", "phòng"}
)
_ZH_SPEECH = re.compile(
    r"请|吗|呢|吧|了|我们|我|你|您|麻烦|已经|已|需要|可以|是否|能否|明天|今天|昨天|后天|下周|本周|上周|星期|周[一二三四五六日天]"
)
# 会、要、将：三个字以内的一行（名字：王会芳）不算，职位、组织里的（会计、协会、会长）不算（D93 第 6 条）；
# 长一点的一行里照样算一句话的字眼（“交期会延长”“大货价格要再问一下李总”，D93 审阅第 6 条）
_ZH_MAYBE_SPEECH = re.compile(r"[会要将]")
# 会展后面跟着示、出、开的不算（D96 审阅第 4 条：“样品会展示给采购”“这批会展出”是一句话）
_ZH_NOT_SPEECH = re.compile(r"会计|会长|协会|商会|学会|工会|委员会|董事会|展会|博览会|交易会|会展(?![示出开])|将军")
# 名字加职位（“陈会林 销售经理”）：会、要、将在前面的名字里才不算说话（D96 第 37 条；审阅第 4 条：“这批 会转给采购”是一句话）
_ZH_NAME_TITLE = re.compile(r"[\u4e00-\u9fff]{2,3}[ \t]+[\u4e00-\u9fff]{1,8}")
# 写着地址字样的那一段：会、将、要后面一两个字内就是路、街、道的，是路名（将台路、会元路），不算说话（D99 第 47 条）
_ZH_STREET_SPEECH = re.compile(r"[会要将][\u4e00-\u9fff]{0,2}?[路街道巷弄]")
# 只写着地名的一行（D93 第 6 条）：省、市、区、县、开发区、园区这些字结尾的
# 每一段都是几个字加一个省、市、区（D93 审阅第 6 条：“下一批发往深圳市”不是地名）
# 只写着地名的一行：1 到 _PLACE_PARTS 段，每段 1 到 _PLACE_SEGMENT 个汉字加一个这些结尾（_zh_place）。
# 每一段最多 10 个字（D96 第 38 条：以前 6 个，“……高新技术产业开发区”这类长的认不出）。整行超过 _ZH_PLACE_WIDTH 的不用比
_PLACE_SUFFIXES = ("开发区", "工业区", "工业园", "新区", "园区", "街道", "省", "市", "区", "县", "镇", "乡")
_PLACE_SEGMENT = 10
_PLACE_PARTS = 5
_ZH_PLACE_WIDTH = _PLACE_PARTS * (_PLACE_SEGMENT + 3)
# 只写地名的一行里不能有的动词（D96 第 35 条：“这批发往深圳市”“送到浦东新区”是正文）。“开发区”里的“发”先去掉。
# D96 审阅第 2 条补了改、换、至、在、去（“交货地点改为宁波市”“货在南山区”），D99 第 47 条补了为、是（“目的港为宁波市”）；
# 从、运、回、出、于不加：有从化区、运城市、回民区、于洪区
_ZH_PLACE_VERB = re.compile(r"[发送寄往到改换至在去为是]")
_ZH_COMMA = re.compile(r"[，、；]")
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# 职位前面带着问、找、给、向、让、叫、告诉、通知的是一句话（D96 审阅第 6 条：“这事问店长”“再问一下经理”“库存的事找线长”）；
# D99 第 47 条补了联系、转、交、寄、上报、抄送（“这事转经理”“发票寄财务部”）。顾问的“问”、交易部、转运中心、寄售部不算
_ZH_TITLE_VERB = re.compile(r"(?<!顾)问|找|给|向|让|叫|告诉|通知|联系|转(?!运)|交(?![易通流])|寄(?!售)|上报|抄送")
_ZH_ZONG_CHARS = 4  # 结尾是“总”的一行最多几个字才算职位（D96 第 42 条）
_ZH_NAME_CHARS = 3  # 只有中文的一行署名（名字）最多几个字；更长的要是公司、职位、地址（四个字的“同意付款”是正文，D93）
# 问候下面一行短的决定、回答（D93 第 4 条）不是名字：“同意付款”“取消订单”“同意”“Confirmed for Friday”“Approved”
_ZH_DECISION = re.compile(
    r"同意|收到|好的|确认|取消|付款|发货|明白|了解|知道|没问题|是的|不行|可以|已付|已发|已收|退回|拒绝|批准|通过|暂停|安排"
)
_EN_DECISION = frozenset(
    "confirmed confirm approved approve agreed agree noted received done ok okay yes no sure fine cancelled canceled"
    " cancel accepted accept rejected reject declined decline paid shipped delivered sent attached please kindly urgent"
    " asap understood acknowledged pending postponed monday tuesday wednesday thursday friday saturday sunday tomorrow"
    " today".split()
)
_TITLE_CASE_WORDS = 4  # 后面几段里，大写开头的短行（公司名、部门名）最多几个词
# 开头的称呼：“Hi Mai,”“王总：”“小李，”。它不算正文，后面紧跟的“谢谢”不是结尾的问候
_GREETING = re.compile(r".{1,20}[,，:：]")


_URL_TOKEN = re.compile(r"(?:https?://|www\.)\S*", re.IGNORECASE)


def _plain_words(line: str) -> list[str]:
    """一行里的词，邮件地址、尖括号里的链接、网址（http://…、www.…，D93）不算。"""
    return _WORD.findall(_URL_TOKEN.sub(" ", _LINK.sub(" ", _ADDRESS_PART.sub(" ", line))))


def _phone_line(line: str) -> bool:
    """电话那一行：写着 Tel、电话这些字、带着号码的，整行差不多都是号码的（+86 21 0000 0000），
    或者名字加号码（Lin Hao/13800000000：一串 7 位以上的数，别的只有两三个大写开头的词）。"""
    digits = sum(char.isdigit() for char in line)
    if digits < 6:
        return False
    label = _CONTACT_PREFIX.match(line)
    if label and not _phone_label(label.group(0)):  # 写着别的字样的（PI No.: 20261109、Amount: 52,300.00）不是电话（D93）
        return False
    if _figure_value(line):  # 金额、日期不是电话（D96 审阅第 1 条）
        return False
    if _PHONE_WORD.search(line):
        return True
    compact = "".join(line.split())
    if digits >= 0.6 * len(compact) or _PLUS_NUMBER.search(line):
        return True
    words = _plain_words(line)
    return bool(_LONG_NUMBER.search(line)) and 0 < len(words) <= 3 and all(word[0].isupper() for word in words)


_LONG_NUMBER = re.compile(r"(?<!\d)\d{7,}(?!\d)")
_PLUS_NUMBER = re.compile(r"\+\s?\(?\d[\d \t().-]{6,}\d")  # 加号开头的号码：+1-234-567-8901
# 写着“联系人：”“电话：”这类字样的一行
_CONTACT_LABEL = re.compile(
    r"^(?:联系人|联系方式|电话|手机|座机|传真|邮箱|网址|官网|地址|微信|公司|职位|部门"
    r"|聯繫人|聯絡人|聯繫方式|聯絡方式|電話|手機|座機|傳真|郵箱|電郵|網址|官網|職位|部門)\s*[:：]"
    r"|^(?i:contact|attn|e-?mail|web(?:site)?)\s*[:：]"
)


def _sentence_like(line: str, *, comma: bool = True, loose: bool = False, speech: str = "full") -> bool:
    """像一句话：英文里有小写开头的实词（两个词以上的行；loose 是 True 时要两个以上，写着字样开头的地址里常夹着一个
    小写的地名），中文带说话、时间的字眼或者中间有逗号（comma 是 False 时逗号不算：地址里也常有逗号）。
    会、要、将（speech）：full 是长一点的一行里有、或者开头就是都算；street 也是这样，不过路名里的不算（写着地址字样的
    那一段：将台路、会元路是路名，“地址：下月将搬到新厂”是一句话，D99 第 47 条）；off 都不算（信头名单里的名字，D96 第 37 条）。"""
    if _ZH_SPEECH.search(line) or (comma and _CJK.search(line) and _ZH_COMMA.search(line)):
        return True
    spoken = _ZH_NOT_SPEECH.sub("", line)
    if speech == "street":
        spoken = _ZH_STREET_SPEECH.sub("", spoken)
    # 会、要、将：长一点的一行里有，或者开头就是（D96 第 35 条：“要退货”不是名字，名字很少这样开头）。名字加职位不看这一条
    if speech != "off" and _ZH_MAYBE_SPEECH.search(spoken) and not _zh_name_and_title(line):
        if _ZH_MAYBE_SPEECH.match(spoken.lstrip()) or len(_CJK.findall(line)) > 3:
            return True
    words = _plain_words(line)
    content = sum(
        1 for word in words if word[0].islower() and word not in _CONNECTIVES and word.lower() not in _SIGNATURE_WORDS
    )
    return len(words) >= 2 and content >= (2 if loose else 1)


# 写着公司、职位、部门字样的一行（D101）
_ORG_LABEL = re.compile(r"(?:公司|职位|職位|部门|部門|(?i:company|title|position|dept\.?|department))\s*[:：]\s*")
# 一行里写几样时的隔开符号：逗号、竖线、圆点、括号，两边有空白的横线（“Lena Ortiz – Sales Manager”；Co-founder、
# Mary-Jane 里的不算）。两边的空白另外去掉，不写进正则（一大串空格时平方级）
_ORG_PIECE = re.compile(r"[,，|｜•·()（）]|(?<=\s)[-–—](?=\s)")
# 看有没有混着联系方式时的拆法（第五轮第 52 条）：和上面一样，只是括号不拆（“示例贸易（示例市）有限公司”是公司名），
# 拆完每一段再按空白拆
_CONTACT_SPLIT = re.compile(r"[,，|｜•·]|(?<=\s)[-–—](?=\s)")
# 分机号、聊天软件的字样：“ext. 8012”“Skype lenaortiz88”“WeChat …”（不带冒号的也算）。LINE 不认：Production Line、
# Line Manager 是职位。分机要后面跟着号码才算（第六轮第 59 条：“Example Hair Extension Co., Ltd.”是公司名）
_CONTACT_WORD = re.compile(
    r"\b(?:ext|extn|extension)\b\.?\s*(?:no\.?\s*)?[:：#]?\s*\d|\b(?:skype|wechat|whatsapp|viber|telegram|qq)\b"
    r"|(?:分机|分機)(?:号|號)?\s*[:：]?\s*\d|微信",
    re.IGNORECASE,
)
_SIGNATURE_DIGITS = 6  # 一行里数字有这么多的，是电话、邮编、分机这类（公司名、职位、部门不会有）


def _signature_info_line(line: str) -> bool:
    """签名里写公司、职位、部门的一行（D101）：能当证据。问候、名字、电话、邮箱、网址、地址不算，
    一行里混着电话、邮箱、网址、地址的也不算（这些照旧只当上文）。名字和职位写在一行的算（“Lena Ortiz, Sales Manager”
    “林浩（采购经理）”）；中文的按空白再拆一次（“销售经理 林浩”）。
    混着联系方式的（第五轮第 52 条）：拆开的每一段、每一段按空白拆开的每一个词都要看，数字有 6 个以上的、写着分机号、
    聊天软件的、中文的地名，整行照旧只当上文（“Sales Manager 0000 2345 6789”“示例贸易有限公司 示例市示例区”）。
    第六轮（第 59 条）放宽：按词查时不看国名（“Example Vietnam Co., Ltd.”；整段只写国名的，拆段那一步已经挡了），
    地名后面紧跟着职位、公司的不挡（“亚太区 销售总监”），分机要后面跟着号码才算。"""
    text = _LEADING_MARKS.sub("", line).strip()
    if not text or len(text) > _CONTACT_WIDTH or _VALEDICTION.fullmatch(text) or _MOBILE_SIGNATURE.fullmatch(text):
        return False
    pieces = [piece.strip() for piece in _ORG_PIECE.split(text)]
    if any(_contact_piece(piece) for piece in [text, *pieces] if piece):
        return False
    if sum(char.isdigit() for char in text) >= _SIGNATURE_DIGITS or _CONTACT_WORD.search(text):
        return False
    for piece in _CONTACT_SPLIT.split(text):
        words = piece.split()
        for index, word in enumerate(words):
            if _contact_word(word):
                return False
            if _CJK.search(word) and _zh_place(word):  # 地名：后面紧跟着职位、公司的是大区、分公司，不挡
                following = words[index + 1] if index + 1 < len(words) else ""
                if not (following and (_title_line(following) or _COMPANY.search(following))):
                    return False
    labeled = _ORG_LABEL.match(text)
    if labeled:
        return bool(text[labeled.end() :].strip())
    parts = [part for piece in pieces if _CJK.search(piece) for part in piece.split()]
    return any(piece and (_COMPANY.search(piece) or _title_line(piece)) for piece in [*pieces, *parts])


def _contact_word(word: str) -> bool:
    """签名那一行按空白拆开的一个词是联系方式（第五轮第 52 条）。和 _contact_piece 一样，只是不看国名（第六轮第 59 条）。"""
    if _contact_only(word) or _phone_line(word) or _EMAIL.search(word) or _URL.search(word) or _DOMAIN.search(word):
        return True
    return bool(_ADDRESS_LABEL.search(word) or _STREET.search(word) or _POSTCODE.search(word))


def _contact_piece(text: str) -> bool:
    """一段字是电话、邮箱、网址、域名、地址（签名里只当上文的那几样，D101）。"""
    if _contact_only(text) or _phone_line(text) or _EMAIL.search(text) or _URL.search(text) or _DOMAIN.search(text):
        return True
    return bool(_ADDRESS_LABEL.search(text) or _STREET.search(text) or _POSTCODE.search(text) or _COUNTRY_LINE.fullmatch(text))


def _zh_place(line: str) -> bool:
    """整行只写着地名：1 到 _PLACE_PARTS 段，每段 1 到 _PLACE_SEGMENT 个汉字加一个省、市、区、开发区这类结尾。
    按位置往前推（best[i] 是走到第 i 个字最少用了几段），每个位置最多看 10 个字、12 种结尾：写成正则的时候，
    一行几十个“市”每一种分法都要试，用 | 隔开的一千段要七八秒（D96 审阅第 5 条）。"""
    size = len(line)
    if not 0 < size <= _ZH_PLACE_WIDTH or not all("\u4e00" <= char <= "\u9fff" for char in line):
        return False
    suffixes_at = [[len(suffix) for suffix in _PLACE_SUFFIXES if line.startswith(suffix, index)] for index in range(size)]
    best = [_PLACE_PARTS + 1] * (size + 1)
    best[0] = 0
    for start in range(size):
        if best[start] >= _PLACE_PARTS:
            continue
        for middle in range(start + 1, min(size, start + _PLACE_SEGMENT + 1)):
            for length in suffixes_at[middle]:
                best[middle + length] = min(best[middle + length], best[start] + 1)
    return best[size] <= _PLACE_PARTS


def _zh_name_and_title(line: str) -> bool:
    """名字加职位（“陈会林 销售经理”“周将 总经理”），会、要、将在前面的名字里，后面是职位。
    以前公司名（有限公司结尾）也不看会、要、将，“发票会开给示例贸易有限公司”这种一句话也算成了签名（D96 审阅第 4 条），去掉了：
    “示例会展服务有限公司”靠“会展”就能过。"""
    text = line.strip()
    if not _ZH_NAME_TITLE.fullmatch(text):
        return False
    name, title = text.split(None, 1)
    return (
        bool(_ZH_MAYBE_SPEECH.search(name))
        and not _ZH_MAYBE_SPEECH.search(title)
        and not _ZH_TITLE_VERB.search(title)
        and bool(_ZH_TITLE.search(title))
    )


def _signature_piece(piece: str) -> bool:
    """用 | 隔开的一段：自己是签名的，还不能光是数字多（D96 第 35 条：“PO 4500012345 | qty 120000 | ETD …”不是签名）。
    有字母又有 6 个以上数字的，要写着字样（M:、Tel:）、电话的字、邮箱或者网址；光是号码、没有字母的不用
    （D96 审阅第 3 条：“Sales Manager | 138 0000 0000”是签名）。"""
    if not _signature_line(piece):
        return False
    if sum(char.isdigit() for char in piece) < 6 or not any(char.isalpha() for char in piece):
        return True
    return bool(_CONTACT_PREFIX.match(piece) or _PHONE_WORD.search(piece) or _EMAIL.search(piece) or _URL.search(piece))


def _title_line(line: str) -> bool:
    """职位、部门：英文的词都是职位里的词（Sales Manager、Head of Sales），中文的以经理、部这些字结尾。"""
    zh = _CJK.findall(line)
    if zh:
        head = _without_tail(line)
        title = _ZH_TITLE.search(head)
        if title is None or len(zh) > 12 or _ZH_TITLE_VERB.search(head):
            return False
        # 结尾只是一个“总”的（李总、销售副总），整行要短（D96 第 42 条：“大货价格再问一下李总”是正文）
        return title.group(0) != "总" or len(zh) <= _ZH_ZONG_CHARS
    words = [word.lower() for word in _plain_words(line)]
    return 0 < len(words) <= 8 and all(word in _TITLE_WORDS or word in _CONNECTIVES for word in words)


def _without_tail(line: str) -> str:
    """去掉末尾不是汉字的那一截（“销售经理 (Sales)”看的是“销售经理”）。从后往前数，长串也是线性的（D93）。"""
    end = len(line)
    while end and not _CJK.match(line, end - 1):
        end -= 1
    return line[:end]


# 业务字段的字样（D93 审阅第 6 条）：PI No.:、Amount:、Qty:、ETD:、订单号：后面的数字不是电话。
# 反过来写（D96 第 31 条）：以前要字样里每个词都在电话那张表里，Work Tel、Fx、HP NO、手机号这些常见的写法都不认，
# 第 1 箱的签名少认了一大半
# D96 审阅第 1 条补了 S/C、B/L、Container、Booking、Delivery、Due、Pcs、N.W.、Sub-total，价格、运费、交期、柜号这些
_NOT_PHONE_WORDS = frozenset(
    "pi po sc bl invoice inv order amount amt total subtotal sub deposit balance due qty quantity pcs ctn ctns carton cartons"
    " etd eta date price unit weight nw gw cbm size ref contract payment sum container booking delivery shipment lot batch"
    " model item sku".split()
)
_NOT_PHONE_ZH = re.compile(
    r"金额|订单|单号|数量|日期|发票|定金|余款|总价|单价|合同|货款|价格|合计|总额|尾款|运费|交期|柜号|批号|货号|件数|箱数|毛重|净重"
    r"|体积|报价|型号|編號|编号"
    r"|金額|訂單|單號|數量|發票|餘款|總價|單價|貨款|價格|合計|總額|運費|櫃號|批號|貨號|件數|箱數|淨重|體積|報價|型號"
)
_LABEL_SPLIT = re.compile(r"[\s/&,.+_#-]+")
# P.I.、S/C、B/L、N.W. 这种单个字母加点、斜杠的，先并成一个词（pi、sc、bl、nw），再拆（D96 审阅第 1 条）
_LABEL_INITIALS = re.compile(r"(?<![a-z])([a-z])[./](?=[a-z](?![a-z]))")
# 值是金额（千分位、两位小数）、日期（2026-12-05）的不是电话（D96 审阅第 1 条）
# 不从一串逗号数字的中间起头（D96 第二遍审阅第 1 条：“1,111,…,1111”这种长串，每个起点都要往后试一遍，平方级）
_AMOUNT = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])|(?<![\d.,])\d+\.\d{2}(?![\d.])")
# 年在前的（2026-12-05）、日或者月在前的（05/12/2026，D99 第 47 条）
_DATE_VALUE = re.compile(
    r"(?<!\d)(?:19|20)\d\d[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])(?!\d)"
    r"|(?<!\d)(?:0?[1-9]|[12]\d|3[01])[-/.](?:0?[1-9]|[12]\d|3[01])[-/.](?:19|20)\d\d(?!\d)"
)


def _phone_label(label: str) -> bool:
    """这个字样（冒号前面那一截）可不可以是电话的：写着业务字段（PI No.、Amount、订单号……）的不可以，别的都可以。"""
    raw = _LABEL_INITIALS.sub(r"\1", label.strip(" \t:：").lower())
    words = [word for word in _LABEL_SPLIT.split(raw) if word]
    return bool(words) and not any(word in _NOT_PHONE_WORDS for word in words) and not _NOT_PHONE_ZH.search(raw)


def _figure_value(text: str) -> bool:
    """一段字是金额、日期：不是电话。"""
    return bool(_AMOUNT.search(text) or _DATE_VALUE.search(text))


# 只写着一样联系方式的一行（_contact_only）：前面可以有“Tel:”“E-mail:”“网址：”这类字样
# 字样里可以有 #（PO#:、Invoice #:，D99 第 47 条）
_CONTACT_PREFIX = re.compile(r"[^\W\d_][\w .&/+#-]{0,24}?[:：]\s*")
# 聊天软件、社交网站的账号：字样后面一个词（“Skype: lin.hao”“WeChat: linhao”“微信：tom_88”）
_HANDLE_LINE = re.compile(
    r"(?:skype|wechat|whatsapp|line|qq|viber|telegram|signal|linkedin|facebook|instagram|twitter|web|website|homepage"
    r"|微信|网址|官网|主页|網址|官網|主頁)\s*[:：]\s*\S+",
    re.IGNORECASE,
)
# 签名里一行写几样时的隔开符号
_PIECE_SEPARATOR = re.compile(r"[|｜•·]")  # 两边的空白另外去掉：写成 \s*[|]\s* 的话，一大串空格时是平方级
# 一行开头的符号（📍 Add:、☎ Tel:、* 网址：）
_LEADING_MARKS = re.compile(r"^[^\w\u3400-\u9fff+(<\[]+")
# 写着地址、联系人这类字样开头的一行：字样后面不是一句话就是签名（中文的逗号不算一句话：地址里也有逗号）
_LABELED = re.compile(
    r"(?:联系人|联系方式|电话|手机|座机|传真|邮箱|网址|官网|地址|微信|公司|职位|部门|邮编"
    r"|聯繫人|聯絡人|聯繫方式|聯絡方式|電話|手機|座機|傳真|郵箱|電郵|網址|官網|職位|部門|郵編)\s*[:：]"  # 繁体（D96 第 42 条）
    r"|(?i:(?:contact|attn|e-?mail|web(?:site)?|tel|phone|mobile|fax|add|addr|address)\s*[:：])"
    r"|(?i:(?:địa chỉ|điện thoại|di động|đt|sđt)\s*[:：])"  # 越南文的地址、电话（D93）
)


def _contact_only(line: str) -> bool:
    """一行只写着一样联系方式：电话号码、邮件地址、网址、域名（前面可以有字样），或者聊天软件的账号。
    整行就是它们的不用看像不像一句话（D93 第 3、6 条）。"""
    if _HANDLE_LINE.fullmatch(line) or (_URL.match(line) and not any(char.isspace() for char in line)):
        return True
    label = _CONTACT_PREFIX.match(line)
    rest = (line[label.end() :] if label else line).strip()
    if not rest:
        return False
    if _EMAIL.fullmatch(rest) or _DOMAIN.fullmatch(rest) or (_URL.match(rest) and not any(char.isspace() for char in rest)):
        return True
    if label and not _phone_label(label.group(0)):  # 写着别的字样的数字（PI No.:、Amount:）不是电话
        return False
    if _figure_value(rest):  # 金额、日期不是电话（D96 审阅第 1 条）
        return False
    compact = "".join(rest.split())
    digits = sum(char.isdigit() for char in compact)
    letters = sum(char.isalpha() for char in compact)  # ext、转这类，最多几个：“明天打我手机 139…”不是一行号码
    return digits >= 6 and digits >= 0.6 * len(compact) and letters <= 4


def _signature_line(line: str) -> bool:
    """明摆着是签名的一行：电话、邮箱、网址、只写着域名的、地址、国名、公司、职位。

    先看像不像一句话（D93 第 3 条）：“Please send the invoice to billing@example.org”“Room 2201 is booked for …”
    “请把发票寄到新地址”带着邮箱、地址，也是正文。只写着一样联系方式的（_contact_only）不看这个；写着地址、联系人
    这类字样开头的，只看字样后面那一段像不像一句话（“电话：明天打给你”是正文，“地址：…，3 栋 1203 室”是签名）。
    """
    line = _LEADING_MARKS.sub("", line)  # 📍、☎、* 这类开头的符号不算（D93）
    if _contact_only(line):
        return True
    pieces = [piece.strip() for piece in _PIECE_SEPARATOR.split(line)]
    # “M: … | E: … | W: …”：用 | 隔开的几样各自是签名的，整行是签名（D93 审阅第 8 条）；不是的照下面整行看
    if len(pieces) > 1 and all(pieces) and all(_signature_piece(piece) for piece in pieces):
        return True
    labeled = _LABELED.match(line)
    if labeled:  # 写着地址字样的，路名里的会、要、将不算（D96 审阅第 4 条、D99 第 47 条：“地址：下月将搬到新厂”是一句话）
        speech = "street" if _ADDRESS_LABEL.match(line) else "full"
        return not _sentence_like(line[labeled.end() :], comma=False, loose=True, speech=speech)
    address = _ADDRESS_LABEL.search(line) or _STREET.search(line) or _POSTCODE.search(line)
    if _sentence_like(line, comma=not address):  # 地址那几行中文的逗号不算一句话
        return False
    if _phone_line(line) or _EMAIL.search(line) or _URL.search(line) or _DOMAIN.fullmatch(line):
        return True
    if _CONTACT_LABEL.search(line):  # “联系人：Tom Lee”
        return True
    if _ADDRESS_LABEL.search(line) or _STREET.search(line) or _POSTCODE.search(line) or _COUNTRY_LINE.fullmatch(line):
        return True
    if _PLACE_CODE.fullmatch(line) or (
        _zh_place(line) and not _ZH_PLACE_VERB.search(line.replace("开发区", ""))
    ):
        return True
    if any(char.isdigit() for char in line):
        return False
    return bool(_COMPANY.search(line)) or _title_line(line)


def _name_line(line: str) -> bool:
    """问候下面第一段的一行：名字、职位、公司、电话。不能有一句话，数字只能在电话、地址那几行里。名字（D93 第 4 条）：
    只有中文的 2 到 _ZH_NAME_CHARS 个字，都不能是短的决定、回答（_ZH_DECISION、_EN_DECISION：“同意付款”“Confirmed
    for Friday”）。英文的小写写的照认（“tom”“Lin hao”）：一开始要每个词都大写开头，真实邮件里有人一直小写署名，少认了。"""
    if _signature_line(line):
        return True
    if any(char.isdigit() for char in line) or _sentence_like(line) or _ZH_DECISION.search(line):
        return False
    words = _plain_words(line)
    if any(word.lower() in _EN_DECISION for word in words):
        return False
    zh = len(_CJK.findall(line))
    latin = [word for word in words if not _CJK.match(word)]
    if zh > _ZH_NAME_CHARS or (zh and not latin and zh < 2):
        return False
    return len(words) <= 6


def _title_case_line(line: str) -> bool:
    """后面几段里大写开头的短行（公司名、部门名、名字）：几个词都是大写开头的，没有数字，没有中文。"""
    if any(char.isdigit() for char in line) or _CJK.search(line):
        return False
    words = _plain_words(line)
    return 0 < len(words) <= _TITLE_CASE_WORDS and all(word[0].isupper() or word in _CONNECTIVES for word in words)


def _bracketed_line(line: str) -> bool:
    """方括号括起来的一小段（[Logo]、[Mobile:]、图片的说明）。括号里是一句话的不算（D93）：“[Ship 20 pallets on Friday]”是正文。"""
    if not _BRACKETED.fullmatch(line):
        return False
    return not _sentence_like(line[1 : line.index("]")])


def _image_line(line: str) -> bool:
    """图片占位的一行：只有 [cid:…]，或者后面只跟着电话、邮箱、网址（签名里的小图标）。

    占位后面跟着话的（“[cid:…] 箱子压坏了”“[cid:…] Please call … before delivery”）是正文里插的图，不算。
    """
    if "[" not in line or not _IMAGE_PLACEHOLDER.search(line):
        return False
    rest = _LINK.sub(" ", _IMAGE_PLACEHOLDER.sub(" ", line)).strip(" \t|·•/-")
    if not rest:
        return True
    if _DOMAIN.fullmatch(rest) or _EMAIL.fullmatch(rest):  # “example.com”会被当成两个小写的词、算成一句话（D93）
        return True
    if len(rest) > _CONTACT_WIDTH or _sentence_like(rest):
        return False
    return _phone_line(rest) or bool(_URL.match(rest))


_VALEDICTION = re.compile(
    r"(?:best(?:\s+(?:regards|wishes))?|(?:kind|warm|warmest|best)\s+regards|regards|cheers|sincerely|br"
    r"|yours\s+(?:sincerely|faithfully|truly)|with\s+(?:best|kind)\s+regards"
    r"|(?:thanks|thank\s+you|many\s+thanks)(?:\s+(?:and|&)\s+(?:(?:best|kind|warm)\s+)?regards|\s+(?:and|&)\s+best"
    r"|\s+a\s+lot|\s+in\s+advance(?:\s+(?:and|&)\s+(?:(?:best|kind)\s+)?regards)?|\s+again)?"
    r"|谢谢|多谢|此致|敬礼|祝好|顺祝商祺|顺颂商祺|祝商祺|顺颂时祺|顺祝商安)[\s,，.。!！~～]*",
    re.IGNORECASE,
)
_CLOSING_LINES = 8  # 结尾问候下面最多几行署名（空行、分隔的横线、[cid:…] 图片占位、免责声明不算）
_CLOSING_WIDTH = 60  # 署名一行最多多宽；明摆着是签名的那几行（电话、网址、地址）可以到 _CONTACT_WIDTH
_CONTACT_WIDTH = 200
_CLOSING_REACH = 200  # 问候下面最多往下看几行
_INVISIBLE = "﻿​‌‍⁠"
_RULE_LINE = re.compile(r"[\s_\-=*~·•—–]+")
# 内嵌图片的占位 [cid:…]（后面可以跟着图片的链接）；Gmail 写成 [image: 图片名]。见 _image_line。
# 占位里不会再有 [：没闭合的一大串 [cid: 也是线性的
_IMAGE_PLACEHOLDER = re.compile(r"\[(?:cid:|image:\s)[^\[\]\n]*\](?:\s*<[^<>\s]*>)?", re.IGNORECASE)
_LINK = re.compile(r"<[^<>\s]*>")
# 署名里方括号括起来的一小段（[Logo]、[Mobile:]、图片的说明），不算一句话
_BRACKETED = re.compile(r"\[[^\[\]\n]{1,80}\](?:\s*<[^<>\s]*>)?")
# 免责声明、保密声明：从认得出的开头那一行起，到这一段末尾（空行为止），照签名处理，只帮着理解。
# 只认常见的写法，宁可少认：“If you are not the intended recipient”要写明 intended（或者 addressee），
# “This email … is confidential”后面要说到收件人、用途，中间没有数字；“Disclaimer:”后面要接着这类套话；
# 中文的要写明保密（“本邮件所附报价仅供参考”是正文）；“请勿转发”要说的是这封邮件、而且整行就是这一句
_DISCLAIMER = re.compile(
    r"[\s*>_\-]*(?:"
    r"(?:confidentiality|privacy|legal)\s+(?:notice|statement|disclaimer)\s*(?:[:：.]\s*)?"
    r"(?:$|this\s+(?:e-?mail|message|note|communication)|the\s+(?:information|contents?)\b)"
    r"|disclaimer\s*(?:[:：.]\s*)?(?:$|this\s+(?:e-?mail|message)|the\s+(?:information|contents?)\b|all\s+(?:business|transactions|services)\b)"
    # “intended recipient of this parcel”说的是包裹（D93）：后面跟着 of 的，要是 of this e-mail、message 这些
    r"|if\s+you\s+are\s+not\s+(?:the|an)\s+(?:(?:intended|named|designated)\s+(?:recipient|addressee)|addressee)\b"
    r"(?!\s+of\s+(?!this\s+(?:e-?mail|mail|message|note|communication|transmission)\b))"
    r"|if\s+you\s+have\s+received\s+this\s+(?:e-?mail|message|communication|transmission)\s+(?:in\s+error|by\s+mistake)"
    r"|(?:note\s*[:：]\s*)?this\s+(?:e-?mail|message|communication|transmission)\b[^\d\n]{0,60}?"
    r"\b(?:is|are|may\s+be|may\s+contain|contains?)\s+(?:strictly\s+)?(?:confidential|privileged)"
    r"[^\d\n]{0,60}?\b(?:intended\s+(?:solely\s+|only\s+|exclusively\s+)?(?:for\s+(?:the\s+)?(?:(?:named|designated|intended)\s+)?"
    r"(?:use|recipients?|addressees?|readers?|persons?|individuals?|entity|entities)|recipients?|addressees?)|recipients?|addressees?|use\s+of)\b"
    r"|the\s+(?:information|contents?)\s+(?:contained\s+)?(?:in|of)\s+this\s+(?:e-?mail|message)\b[^\d\n]{0,80}?\b(?:confidential|privileged)"
    r"|information\s+in\s+this\s+(?:e-?mail|message)\b[^\d\n]{0,80}?\b(?:confidential|privileged)"
    r"|neither\s+the\s+confidentiality"
    r"|(?:免责声明|保密声明|保密提示)(?:[:：]|$)"
    r"|(?:本|此)邮件(?:及其?附件)?(?:含有|包含|可能含有|可能包含|所含|的内容)?[^\d\n]{0,10}?(?:保密|机密)(?:信息|资料|内容)"
    r"|如果?您?不是(?:本|此|这封)?邮件的?(?:指定|预期|目标)?收件人"
    r")",
    re.IGNORECASE,
)
# 系统通知那一行（D93 第 5 条）：照签名标，但只标它自己，不往下连。下面紧跟着的“您的订单……已经装车”是正文
_NOTICE = re.compile(
    r"[\s*>_\-]*(?:[^\d\n]{0,30}?请勿(?:直接)?(?:转发|回复)(?:这|此|本)封?邮件[。.！!]?$"
    # 第二种后面有数字的不算（D96 第 42 条：写明是自动发的、后面接着付款金额的那一行是正文）
    r"|(?:此|本|这封)邮件(?:为|由|是)(?:系统)?自动(?:发送|发出|生成)[^\d\n]*$)"
)

_BODY = "body"
_SIGNATURE_STATE = "signature"
_QUOTED_TAIL = "quoted_tail"  # 原始邮件块里，以下都是旧信
_FORWARD_HEADER_STATE = "forward_header"
_FORWARDED_STATE = "forwarded"


def _is_quote_line(line: str) -> bool:
    return bool(_QUOTE_LINE.match(line))


def _is_signature_delimiter(raw: str) -> bool:
    """规范的签名分隔线是 “-- ”，很多客户端会把后面的空格去掉。"""
    return raw.strip() == "--"


def _next_nonblank(body: list[str], index: int) -> int | None:
    for position in range(index + 1, len(body)):
        if body[position].strip():
            return position
    return None


def _is_reply_separator(body: list[str], index: int, line: str, reply: bool = False) -> bool:
    if _REPLY_SEPARATOR.fullmatch(line):
        return True
    following = _next_nonblank(body, index)
    if _UNDERSCORE_RULE.fullmatch(line):
        return following is not None and bool(_FROM_FIELD.match(body[following].strip()))
    # Outlook 没有分隔线时，直接是 From: 一行，下面三行里有 Sent:。还要再有一样：From 后面是个邮件地址，
    # 或者下面四行里有 Subject:，或者这封信本身是回信（主题 Re:、回复：）。
    # “From: Shanghai PVG / Date: 11-15” 这种行程单、“发件人：行政部 / 时间：…”这种通知都不是旧信
    match = _FROM_FIELD.match(line)
    if match is None:
        return False
    following_lines = [body[position].strip() for position in range(index + 1, min(index + 5, len(body)))]
    if not any(_SENT_FIELD.match(text) for text in following_lines[:3]):
        return False
    return bool(_ADDRESS.search(match.group(1))) or reply or any(_SUBJECT_FIELD.match(text) for text in following_lines)


def _address_line(text: str) -> bool:
    """一行只有名字和邮件地址：收件人、抄送的名单接着写到了下一行。每个地址前后最多三个词，不能是一句话。"""
    found = _ADDRESS_PART.findall(text)
    if not found:
        return False
    rest = _ADDRESS_PART.sub(" ", text).strip()
    return (
        not _STOP.search(rest) and len(_WORD.findall(rest)) <= 3 * len(found) and not _sentence_like(rest, speech="off")
    )


def _bare_value_ok(name: str, value: str) -> bool:
    """不带冒号的字段的值像不像：发件人、收件人、抄送要有邮件地址，日期要有数字，主题不能是一句话的结尾。"""
    if name in ("from", "to", "cc", "bcc"):
        return bool(_ADDRESS.search(value))
    if name == "date":
        return any(char.isdigit() for char in value)
    return not _STOP_END.search(value)


def _bare_sender(value: str) -> str | None:
    """From 后面写的发件人：到第一个邮件地址为止（名字里有 To、Date 这些词也不切开），没有地址就到下一个字段为止。"""
    match = _FIRST_ADDRESS.search(value)
    if match:
        return value[: match.end()].strip() or None
    return _BARE_NEXT_FIELD.split(value, maxsplit=1)[0].strip() or None


def _bare_header(body: list[str], start: int, *, forward: bool = False, first: str = "") -> tuple[list[int], str | None]:
    """第 start 行的分隔线下面，不带冒号的旧信头（转发信头）占哪几行，和它写的发件人。

    字段名后面跟着值，或者字段名单独一行、值在下一行，中间可以隔着空行；收件人、抄送的名单接着写好几行。
    每个字段的值要像这个字段的（_bare_value_ok）：“From now on we ship …”“To be safe …”不是信头，是正文。
    主题那一行（单独一行的，连同下一行的值）是最后一行；下一个不像信头的行就是旧信的正文，到它为止。
    forward 是转发块：下面先是带冒号的字段（Gmail 那种）的，照带冒号的认，这里不管。
    first 是分隔线同一行后面接着的信头（分隔线后面没换行的）。
    """
    rows: list[int] = []
    sender: str | None = None
    pending: str | None = None  # 上一行只有字段名，值在下面
    last: str | None = None  # 上一个字段，收件人、抄送的名单可以接着写
    lines = [(None, first.strip())] if first.strip() else []
    lines += [(position, body[position].strip()) for position in range(start + 1, len(body))]
    for position, text in lines:
        if not text:
            continue
        if forward and not rows and not first and _HEADER_FIELD.match(text):
            return [], None
        match = _BARE_FIELD.match(text)
        rest = text[match.end() :].strip() if match else ""
        if pending is not None and not (match and not rest):
            if not _bare_value_ok(pending, text):
                break
            if position is not None:
                rows.append(position)
            if pending == "from" and sender is None:
                sender = _bare_sender(text)
            if pending == "subject":
                return rows, sender
            last, pending = pending, None
            continue
        if match:
            word = match.group("name").lower()
            name = _BARE_FIELD_NAMES.get(word, word)
            if rest and not _bare_value_ok(name, rest):
                break
            if position is not None:
                rows.append(position)
            if not rest:
                pending = name
                continue
            if name == "from" and sender is None:
                sender = _bare_sender(rest)
            if name == "subject" or _BARE_SUBJECT.search(rest):
                return rows, sender
            last = name
            continue
        if last in (None, "from", "to", "cc", "bcc") and _address_line(text):
            if position is not None:
                rows.append(position)
            continue
        if rows and _ADDRESS.search(text) and _BARE_SUBJECT.search(text) and last in ("from", "to", "cc", "bcc", "date"):
            if position is not None:
                rows.append(position)
            return rows, sender
        break
    return rows, sender


def _glued_forward(line: str) -> str:
    """网易的转发抬头后面没换行、直接接着信头的：返回后面那段信头；不是这样的返回空的。"""
    match = _GLUED_FORWARD.match(line)
    if match is None:
        return ""
    rest = line[match.end() :].strip()
    field = _BARE_FIELD.match(rest)
    if field:
        word = field.group("name").lower()
        if _bare_value_ok(_BARE_FIELD_NAMES.get(word, word), rest[field.end() :].strip()):
            return rest
    return rest if _address_line(rest) else ""


def _is_attribution(body: list[str], index: int, line: str) -> bool:
    """“On … wrote:”。普通的话也可能以“写道：”结尾，所以要么紧跟引用行，要么带日期或地址。"""
    if not _ATTRIBUTION.fullmatch(line):
        return False
    following = _next_nonblank(body, index)
    if following is not None and _is_quote_line(body[following].strip()):
        return True
    return "@" in line or any(char.isdigit() for char in line)


def _wrapped_attribution(body: list[str], index: int, line: str) -> bool:
    """“On Tue, …, Li Ming <” 折到了下一行，下一行是 “li.ming@… wrote:”。"""
    following = index + 1
    return (
        _folded_attribution_start(line)
        and following < len(body)
        and _is_attribution(body, following, body[following].strip())
    )


def _breaks_block(body: list[str], index: int) -> bool:
    """这一行开始的是引用、分隔线或签名，结尾的问候和署名到这里为止。"""
    line = body[index].strip()
    return (
        _is_quote_line(line)
        or bool(_FORWARD_SEPARATOR.fullmatch(line))
        or bool(_REPLIED_SEPARATOR.match(line))
        or _is_reply_separator(body, index, line)
        or _is_attribution(body, index, line)
        or _wrapped_attribution(body, index, line)
        or _is_signature_delimiter(body[index])
        or bool(_MOBILE_SIGNATURE.fullmatch(line))
    )


def _closing_block(body: list[str], index: int) -> list[int] | None:
    """结尾的问候（Best,、谢谢）下面的署名：一直到引用抬头、分隔线、签名（或者信的末尾）的那几行。

    问候下面第一段（前面可以先空几行）每一行都要像名字、职位、公司、电话（_name_line）；再往下隔着空行的几段，
    每一行都要明摆着是签名（_signature_line），或者是大写开头的短行（公司名、部门名）。分隔的横线、图片占位、
    方括号里的一小段、免责声明（那一段由 classify_mail_body 标）不算行数；署名最多 _CLOSING_LINES 行。
    不是这样的（后面还有正文），返回 None。
    """
    block: list[int] = []
    names = 0
    paragraph = 0  # 第几段有字的署名：1 是问候下面第一段
    blank = True  # 上一行是空行（问候本身之后也算）
    position = index + 1
    end = min(len(body), index + 1 + _CLOSING_REACH)
    while position < end:
        name = body[position].strip()
        if not name.strip(_INVISIBLE):  # 只有零宽字符、BOM 的一行当空行
            blank = True
            position += 1
            continue
        if _breaks_block(body, position):
            return block
        if _DISCLAIMER.match(name):
            while position < len(body) and body[position].strip():
                position += 1
            continue
        if _NOTICE.match(name):  # 系统通知只是这一行（classify_mail_body 标）
            position += 1
            continue
        if _RULE_LINE.fullmatch(name) or _image_line(name) or _bracketed_line(name):
            block.append(position)
            position += 1
            continue
        if _VALEDICTION.fullmatch(name):  # 两句问候（“Thanks.”空一行“Best regards,”）：后一句也是署名的一部分
            block.append(position)
            position += 1
            continue
        if blank:
            paragraph += 1
            blank = False
        if len(_LINK.sub("", name)) > _CONTACT_WIDTH:  # 比签名的行还宽：不用先比一遍是不是签名，长行只看到这里（D96 第二遍审阅第 1 条）
            return None
        signature = _signature_line(name)
        width = _CONTACT_WIDTH if signature else _CLOSING_WIDTH
        fits = _name_line(name) if paragraph == 1 else signature or _title_case_line(name)
        if (
            names >= _CLOSING_LINES
            or len(_LINK.sub("", name)) > width  # 尖括号里的链接不算宽
            or _NOT_A_NAME.search(name)
            or (name.endswith(".") and not _ABBREVIATION_END.search(name))
            or not fits
        ):
            return None
        block.append(position)
        names += 1
        position += 1
    return block if position >= len(body) else None


def classify_mail_body(body: list[str], subject: str, *, reply: bool | None = None) -> list[tuple[str | None, str | None]]:
    """每一行正文是什么：(标签, 转发块里写的原作者)。空行是 (None, None)。reply 是这封信本身是不是回信。"""
    forward_subject = bool(_FORWARD_SUBJECT.match(subject))
    if reply is None:
        reply = bool(_REPLY_SUBJECT.match(subject))
    result: list[tuple[str | None, str | None]] = [(None, None)] * len(body)
    state = _BODY
    tail_header = False  # 原始邮件块的分隔线下面、第一个空行之前，是旧信的邮件头
    bare_rows: set[int] = set()  # 网易那种不带冒号的旧信头、转发信头占的行（_bare_header 认的）
    forward_bare = False  # 这个转发块的信头不带冒号：只认 bare_rows，信头里的空行不算信头结束
    forward_end = -1  # 不带冒号的转发信头的最后一行
    forward_from: str | None = None
    closing: set[int] = set()
    disclaimer_line = -2  # 免责声明那一段最后一行：紧挨着的下一行也是
    said_something = False  # 这一段（正文、转发的内容）里已经有新内容了：开头就是“谢谢”的不是结尾
    signature_in_forward = False  # 这一段签名（-- 之后）是在转发进来的信里的（D101）
    for index, raw in enumerate(body):
        line = raw.strip()
        if not line:
            if state == _FORWARD_HEADER_STATE and not (forward_bare and index < forward_end):
                state, said_something = _FORWARDED_STATE, False
            tail_header = False
            continue
        if _is_quote_line(line):
            result[index] = (QUOTED, None)
            continue
        forward_separator = bool(_FORWARD_SEPARATOR.fullmatch(line))
        glued = "" if forward_separator else _glued_forward(line)  # 分隔线后面没换行、直接接着的信头
        forward_separator = forward_separator or bool(glued)
        replied_separator = bool(_REPLIED_SEPARATOR.match(line))
        if state == _QUOTED_TAIL and (forward_separator or replied_separator):
            # 旧信里套着的转发块、回复块也是旧信：原始邮件块以下都是旧信
            bare_rows.update(_bare_header(body, index, forward=forward_separator, first=glued)[0])
            tail_header = True
            result[index] = (QUOTE_HEADER, None)
            continue
        if forward_separator:
            rows, forward_from = _bare_header(body, index, forward=True, first=glued)
            forward_bare = bool(rows) or bool(glued)
            state, forward_end = _FORWARD_HEADER_STATE, rows[-1] if rows else -1
            bare_rows.update(rows)
            result[index] = (FORWARD_HEADER, None)
            continue
        if replied_separator:
            bare_rows.update(_bare_header(body, index)[0])
            state, tail_header = _QUOTED_TAIL, True
            result[index] = (QUOTE_HEADER, None)
            continue
        if state != _FORWARD_HEADER_STATE and _is_reply_separator(body, index, line, reply):
            # 主题是转发的，第一个原始邮件块就是转发的内容；转发的内容里再出现的，是那封信自己引用的旧信
            if forward_subject and state in (_BODY, _SIGNATURE_STATE):
                state, forward_from, forward_bare = _FORWARD_HEADER_STATE, None, False
                match = _FROM_FIELD.match(line)
                if match:
                    forward_from = match.group(1).strip()
                result[index] = (FORWARD_HEADER, None)
            else:
                state, tail_header = _QUOTED_TAIL, True
                result[index] = (QUOTE_HEADER, None)
            continue
        if state == _FORWARD_HEADER_STATE:
            if index in bare_rows if forward_bare else _HEADER_FIELD.match(line):
                match = None if forward_bare else _FROM_FIELD.match(line)
                if match:
                    forward_from = match.group(1).strip()
                result[index] = (FORWARD_HEADER, None)
                continue
            state, said_something = _FORWARDED_STATE, False
        if state == _QUOTED_TAIL:
            # 分隔线下面的“X 在 … 写道：”也是旧信的抬头（D93）
            header = index in bare_rows or (tail_header and (_HEADER_FIELD.match(line) or _ATTRIBUTION.fullmatch(line)))
            result[index] = (QUOTE_HEADER if header else QUOTED, None)
            continue
        if _is_attribution(body, index, line):
            result[index] = (QUOTE_HEADER, None)
            # “On Tue, …, Li Ming <” 折到了上一行：上一行没说完（不是句号、冒号结尾），日期也在上一行
            previous = index - 1
            before = body[previous].strip() if previous >= 0 else ""
            if (
                before
                and _folded_attribution_start(before)
                and not any(char.isdigit() for char in line)
                and result[previous][0] in (NEW, FORWARDED)
            ):
                result[previous] = (QUOTE_HEADER, None)
            continue
        if _is_signature_delimiter(raw):
            signature_in_forward = state == _FORWARDED_STATE  # 转发进来的信里的签名是转述，不算发件人自己写的（D101）
            state = _SIGNATURE_STATE
            result[index] = (SIGNATURE, None)
            continue
        if index == disclaimer_line + 1 or _DISCLAIMER.match(line):
            disclaimer_line = index
            result[index] = (SIGNATURE, None)
            continue
        if _NOTICE.match(line):
            result[index] = (SIGNATURE, None)
            continue
        if state == _SIGNATURE_STATE or index in closing:
            # 发件人自己的签名里写公司、职位、部门的那几行能当证据（D101）；转发进来的信里的不算
            own = not signature_in_forward if state == _SIGNATURE_STATE else state != _FORWARDED_STATE
            result[index] = (SIGNATURE_INFO if own and _signature_info_line(line) else SIGNATURE, None)
            continue
        if _MOBILE_SIGNATURE.fullmatch(line) or _image_line(line):
            result[index] = (SIGNATURE, None)
            continue
        if said_something and _VALEDICTION.fullmatch(line):
            block = _closing_block(body, index)
            if block is not None:
                closing.update(block)
                result[index] = (SIGNATURE, None)
                continue
        if said_something or not _GREETING.fullmatch(line):  # 开头的称呼不算说了话
            said_something = True
        if state == _FORWARDED_STATE:
            result[index] = (FORWARDED, forward_from)
        else:
            result[index] = (NEW, None)
    return result


# ---------- 邮件引的旧信（D101） ----------

_ONE_QUOTE_LEVEL = re.compile(r"^\s*>[ \t]?")
_QUOTE_DEPTH = 8  # 引用最多拆几层
_ATTRIBUTION_VERB = re.compile(
    r"\s*(?:wrote|写道|寫道|đã viết|escribió|a écrit|schrieb|ha scritto|escreveu)\s*[:：]\s*$", re.IGNORECASE
)
_BARE_FROM = re.compile(rf"(?:{_FROM_WORDS})\s+(\S.*@.*)", re.IGNORECASE)  # 网易不带冒号的旧信头
_NAME_SPLIT = re.compile(r"[\s,，]+")
_NOT_AUTHOR_WORDS = frozenset({"am", "pm", "a.m.", "p.m.", "上午", "下午", "at", "à", "um", "alle", "às", "lúc"})
_AUTHOR_WIDTH = 80


@lru_cache(maxsize=2048)
def _classify_quoted(lines: tuple[str, ...]) -> tuple[tuple[str | None, str | None], ...]:
    """引的旧信的一段按一封信认一遍。按这一段的原文记住（一个进程里最多 2,048 段）：同一往来串里后来的信又引一遍
    同样的一段（回信一层套一层地引），不再认（第五轮第 50 条）。classify_mail_body 只看原文，记住的和重认的一样。"""
    return tuple(classify_mail_body(list(lines), "", reply=True))


def quote_sources(body: list[str], kinds: Sequence[str | None], *, depth: int = 0) -> dict[int, tuple[str, str | None]]:
    """邮件正文里标成 quoted 的行，在它引的那封旧信里是什么（D101）：{第几行（从 0 数）: (旧信里的标签, 旧信的作者)}。

    引用块拆开：整段都是 > 开头的去掉一层 >；Outlook、网易那种不带 > 的原始邮件块，引用抬头隔开的每一段是一封旧信
    （旧信自己的 > 行是更深一层）。每一段按 classify_mail_body 再认一遍（旧信里新写的话、问候、署名、免责声明……），
    更深一层的引用再拆，最多 8 层。作者取这一段上面紧挨着的引用抬头里写的人，认不出是 None。空行不在里面。"""
    result: dict[int, tuple[str, str | None]] = {}
    index, total = 0, len(body)
    while index < total:
        if kinds[index] != QUOTED:
            index += 1
            continue
        run = []
        cursor = index
        while cursor < total and kinds[cursor] in (QUOTED, None):
            run.append(cursor)
            cursor += 1
        while kinds[run[-1]] is None:
            run.pop()
        author = _quote_author(body, kinds, index)
        angled = all(_is_quote_line(body[row].strip()) for row in run if body[row].strip())
        lines = [_ONE_QUOTE_LEVEL.sub("", body[row], count=1) if angled else body[row] for row in run]
        classified = _classify_quoted(tuple(lines))
        inner = [kind for kind, _ in classified]
        deeper = quote_sources(lines, inner, depth=depth + 1) if depth + 1 < _QUOTE_DEPTH and QUOTED in inner else {}
        for position, row in enumerate(run):
            if position in deeper:
                result[row] = deeper[position]
            elif inner[position] is not None:
                # 旧信里又转发了一封：转发块的作者照它的 From 认（第五轮第 53 条），不是这封旧信的作者
                relayed = classified[position][1]
                result[row] = (inner[position], relayed if inner[position] == FORWARDED and relayed else author)
        index = cursor
    return result


# 回信的人插在引用里的话（D103 第 6 条，第五轮第 53 条）。行首 >> 紧跟着字的那一条第六轮去掉了（第 58 条）：三箱里
# 一处都没有，旧信自己用不带空格的 > 引更早的信也这样写，连引用抬头一起算成了回信的人新写的
_NAME_WORDS = re.compile(r"[\s,.()（）\"]+")
_INITIALS_MIN = 3  # 首字母缩写至少几个字母（第六轮第 58 条：两个字母的 [OK] 常是“好的”）


@lru_cache(maxsize=1024)  # 同一个发件人的信很多：一个人编一次
def _sender_mark(sender: Address) -> re.Pattern[str] | None:
    """行首写着这封信的发件人：方括号、书名号、双尖括号里写着名字、名字里的一个词（2 个字以上）、3 个字母以上的首字母
    缩写，或者整个邮件地址 @ 前面那一段（`[Tom]`、`[MAL]`、`【林浩】`、`<<Tom Reed>>`），后面跟着话。大小写照原样。
    第六轮（第 58 条）收紧：两个字母的缩写（[OK]）、不分大小写、@ 前面拆出来的段（sales.desk 的 desk），以前都算，
    别人的话认成了回信的人的。"""
    names: set[str] = set()
    display = (sender.name or "").strip()
    words = [word for word in _NAME_WORDS.split(display) if word]
    if display:
        names |= {display, *(word for word in words if len(word) >= 2)}
    if len(words) >= _INITIALS_MIN and all(word[0].isascii() and word[0].isalpha() for word in words):
        names.add("".join(word[0].upper() for word in words))
    local = sender.addr.split("@", 1)[0]
    if len(local) >= 3:
        names.add(local)
    if not names:
        return None
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?:\[\s*(?:{alternatives})\s*\]|【\s*(?:{alternatives})\s*】|<<\s*(?:{alternatives})\s*>>)\s*\S")


def quote_insertions(body: list[str], kinds: Sequence[str | None], sender: Address) -> tuple[set[int], set[int]]:
    """邮件引用里回信的人插进去的话（D103 第 6 条，第五轮第 53 条）：(插话的行, 更早那一轮插的行)，行号从 0 数。

    只看这封信直接引的那一封：第一段引用（不带 > 的，Outlook、网易那种原始邮件块里第一个引用抬头下面那一段）。
    行首写着这封信的发件人的（_sender_mark）是插话：算发件人新写的话。再往下几段是更早的信，
    写着这个发件人的批注是以前那一轮插的，照旧是引用，原作者写这个发件人。带 > 的逐段回复本来就是新写的话，不在这里。"""
    mark = _sender_mark(sender)
    insertions: set[int] = set()
    earlier: set[int] = set()
    first = True
    index, total = 0, len(body)
    while index < total:
        if kinds[index] != QUOTED:
            index += 1
            continue
        run = []
        cursor = index
        while cursor < total and kinds[cursor] in (QUOTED, None):
            run.append(cursor)
            cursor += 1
        angled = all(_is_quote_line(body[row].strip()) for row in run if body[row].strip())
        for row in run:
            text = body[row].strip()
            if not text:
                continue
            marked = bool(mark and mark.match(text))
            if first and not angled and marked:
                insertions.add(row)
            elif marked:
                earlier.add(row)
        first = False
        index = cursor
    return insertions, earlier


def _quote_author(body: list[str], kinds: Sequence[str | None], start: int) -> str | None:
    """引用块上面紧挨着的引用抬头（中间可以隔着空行）里写的人。"""
    position = start - 1
    while position >= 0 and kinds[position] in (QUOTE_HEADER, None):
        line = body[position].strip()
        found = _header_author(line) if line else None
        if found:
            return found[:_AUTHOR_WIDTH]
        position -= 1
    return None


def _header_author(line: str) -> str | None:
    """引用抬头里写的人：From: 那一行（网易的不带冒号），“… wrote:” 里尖括号的邮箱和它前面的名字（最多 4 个词，
    带数字的、AM、PM 不算），没有尖括号的取 wrote 前面、最后一个逗号后面的那几个词（“…, 10:30 AM, Lena Ortiz wrote:”）。"""
    match = _FROM_FIELD.match(line) or _BARE_FROM.match(line)
    if match:
        return match.group(1).strip() or None
    if not _ATTRIBUTION.fullmatch(line):
        return None
    head = _ATTRIBUTION_VERB.sub("", line)
    addresses = list(_ANGLE_ADDRESS.finditer(head))
    if addresses:
        address = addresses[-1]
        words: list[str] = []
        for word in reversed([word for word in _NAME_SPLIT.split(head[: address.start()].rstrip(" \t(（\"'“")) if word]):
            if len(words) == 4 or any(char.isdigit() for char in word) or word.lower() in _NOT_AUTHOR_WORDS:
                break
            words.insert(0, word)
        return " ".join([*words, address.group(0)])
    tail = re.split(r"[,，]", head)[-1].strip()
    if tail and len(tail) <= 40 and not any(char.isdigit() for char in tail) and not _ATTRIBUTION_START.match(f"{tail} "):
        return tail
    return None


@dataclass(frozen=True)
class QuoteCopy:
    """一行可以当证据的引用（D101）：引的旧信里新写的话。当不当证据由 load_queued 按账本定。"""

    sentence_id: str
    key: str  # 原文索引的 copy_key
    form: str  # 比引用用的写法，挡撤回了的话用
    order: tuple[int, str, int, int, int]  # 谁更早：这封信的时间、包号、第几封、第几行、第几句
    message_id: str
    in_reply_to: str | None


def quote_copies(packet: NormalizedPacket) -> list[QuoteCopy]:
    """这个包里可以当证据的引用行（D101）：邮件引的旧信里新写的话（old_kind 是 new、forwarded、signature_info），
    比引用用的写法 8 个字以上，没截短（末尾 … 或者 ...）。"""
    found = []
    for unit in packet.units:
        if unit.mail is None:
            continue
        for line in unit.lines:
            if line.kind not in (QUOTED, QUOTED_ONLY) or line.old_kind not in QUOTE_CONTENT or line.attachment is not None:
                continue
            if line.text.rstrip().endswith(("…", "...")):
                continue
            key = copy_key(line.text)
            if key is None:
                continue
            order = (int(unit.said_at.timestamp()), unit.packet_id, unit.section_no, line.line_no or 0, line.sentence_no or 0)
            found.append(QuoteCopy(line.sentence_id, key, copy_form(line.text), order, unit.mail.message_id, unit.mail.in_reply_to))
    return found


def apply_quote_evidence(packet: NormalizedPacket, evidence: Collection[str]) -> NormalizedPacket:
    """evidence 里的引用行（旧信里新写的话）改标 quoted_only，能当证据（D101）。别的不动。"""
    chosen = set(evidence)
    if not chosen:
        return packet
    kinds = dict(packet.kinds)
    units = []
    for unit in packet.units:
        lines = []
        changed = False
        for line in unit.lines:
            if line.sentence_id in chosen and line.kind == QUOTED and line.old_kind in QUOTE_CONTENT:
                line = replace(line, kind=QUOTED_ONLY)
                kinds[QUOTED] -= 1
                kinds[QUOTED_ONLY] = kinds.get(QUOTED_ONLY, 0) + 1
                changed = True
            lines.append(line)
        units.append(replace(unit, lines=tuple(lines)) if changed else unit)
    if kinds.get(QUOTED) == 0:
        del kinds[QUOTED]
    return replace(packet, units=tuple(units), kinds=kinds)


def demote_insertions(packet: NormalizedPacket, demoted: Collection[str]) -> NormalizedPacket:
    """认成插话的这几行改回引用（第五轮审阅第 1 条）：取包时对上了撤回了的话。改回的不再当证据，也不当只在引用里
    看到过的话（old_kind 是空的）。"""
    chosen = set(demoted)
    if not chosen:
        return packet
    kinds = dict(packet.kinds)
    units = []
    for unit in packet.units:
        lines = []
        changed = False
        for line in unit.lines:
            if line.sentence_id in chosen and line.inserted:
                line = replace(line, kind=QUOTED, inserted=False)
                kinds[NEW] -= 1
                kinds[QUOTED] = kinds.get(QUOTED, 0) + 1
                changed = True
            lines.append(line)
        units.append(replace(unit, lines=tuple(lines)) if changed else unit)
    if kinds.get(NEW) == 0:
        del kinds[NEW]
    return replace(packet, units=tuple(units), kinds=kinds)


def _mail_units(
    packet: Packet, groups: dict[int, list[Sentence]], withdrawn: Collection[str]
) -> Iterator[Unit]:
    header = packet.header
    withdrawn_texts = {
        normal_text(sentence.text)
        for sentences in groups.values()
        for sentence in sentences
        if sentence.sentence_id in withdrawn
    }
    for section in packet.sections:
        assert isinstance(section, MailSection)
        sentences = groups.get(section.number, [])
        if not sentences:
            continue
        reply = bool(_REPLY_SUBJECT.match(section.headers.subject))
        body_kinds = classify_mail_body(section.body, section.headers.subject, reply=reply)
        plain_kinds = [kind for kind, _ in body_kinds]
        sources = quote_sources(section.body, plain_kinds)  # 引的旧信里是什么（D101）
        # 回信的人插在引用里的话（D103 第 6 条，第五轮第 53 条）
        insertions, earlier = quote_insertions(section.body, plain_kinds, section.headers.sender)
        lines = []
        for sentence in sentences:
            relayed_from = old_kind = None
            inserted = False
            if sentence.sentence_id in withdrawn:
                kind = WITHDRAWN
            elif sentence.field is not None:
                kind = HEADER
            else:
                assert sentence.line_no is not None
                row = sentence.line_no - 1
                kind, relayed_from = body_kinds[row]
                if sentence.quoted or kind == QUOTED:
                    if withdrawn_quote(sentence.text, withdrawn_texts):
                        kind, relayed_from = WITHDRAWN, None
                    elif row in insertions:  # 发件人自己插的话：算发件人新写的
                        kind, relayed_from, inserted = NEW, None, True
                    else:
                        kind = QUOTED
                        old_kind, relayed_from = sources.get(row, (None, None))
                        if row in earlier:  # 以前那一轮发件人插的话：原作者是发件人
                            relayed_from = str(section.headers.sender)
                elif kind is None:
                    kind = NEW
            lines.append(
                Line(
                    sentence_id=sentence.sentence_id,
                    text=sentence.text,
                    kind=kind,
                    origin=sentence.origin,
                    quoted=sentence.quoted,
                    field=sentence.field,
                    line_no=sentence.line_no,
                    sentence_no=sentence.sentence_no,
                    relayed_from=relayed_from,
                    old_kind=old_kind,
                    inserted=inserted,
                )
            )
        # 全文：邮件头每个字段一行，后面是正文每一行（去掉首尾空白，和分句时一样）
        heads = [sentence for sentence in sentences if sentence.field is not None]
        body_texts = [line.strip() for line in section.body]
        texts = [sentence.text for sentence in heads]
        spans: list[tuple[int, int]] | None = []
        offset = 0
        for text in texts:
            spans.append((offset, offset + len(text)))
            offset += len(text) + 1
        body_spans = _locate(body_texts, [sentence for sentence in sentences if sentence.field is None])
        if body_spans is None or len(heads) + len(body_spans) != len(sentences):
            spans = None
        else:
            spans.extend((start + offset, end + offset) for start, end in body_spans)
        if spans is not None:
            lines = [replace(line, span=span) for line, span in zip(lines, spans, strict=True)]
        first = sentences[0]
        headers = section.headers
        # 撤回了主题、发件人那一句（按事实撤回，信里别的句子还在，D73 第 16 条）：抬头的说话人、往后传的主题写成“[已撤回]”
        masked = {sentence.field for sentence in sentences if sentence.field in MAIL_WIDE_FIELDS and sentence.sentence_id in withdrawn}
        subject = MASKED if "subject" in masked else headers.subject
        yield Unit(
            unit_id=f"{header.packet_id}#{section.number}",
            packet_id=header.packet_id,
            revision=header.revision,
            source=header.source,
            channel=header.channel,
            section_no=section.number,
            line_no=None,
            speaker=MASKED if "from" in masked else first.speaker,
            speaker_role=first.speaker_role,
            said_at=headers.date,
            origin=first.origin,
            lines=tuple(lines),
            mail=MailInfo(headers.message_id, headers.thread_id, headers.in_reply_to, subject),
            source_text="\n".join([*texts, *body_texts]),
        )


def _with_attachments(
    packet: Packet, units: list[Unit], groups: dict[int, list[Sentence]], withdrawn: Collection[str]
) -> list[Unit]:
    """附件的正文接在它属于的那句话、那封信的 Unit 后面（packet-spec 第 3 节“附件小节”）。

    全文（source_text）也接上附件的每一行，打码时跨行的密码照样认得出。那句话、那封信每一句都撤回了的
    （引用行不算），附件也算撤回了：主人撤回的是这条消息，不该留下它带的文件。写着附件名的那一句撤回了的也是。
    找不到那个 Unit 的（那句话没有一句原文）不进窗口。
    """
    by_owner: dict[tuple[int, int | None], int] = {
        (unit.section_no, unit.line_no): index for index, unit in enumerate(units)
    }
    for body in packet.attachments:
        sentences = groups.get(body.number, [])
        index = by_owner.get((body.of_section, body.of_line))
        if not sentences or index is None:
            continue
        unit = units[index]
        own = [line for line in unit.lines if line.attachment is None]
        said = [line for line in own if not line.quoted] or own  # 引用行不算新说的（规范第 5 节）
        owner_withdrawn = bool(said) and all(line.kind == WITHDRAWN for line in said)
        # 写着附件名的那一句撤回了，附件也撤回：写给模型的抬头里有附件名。对话是这句话里写着名字的一句；
        # 邮件只认邮件头的附件名那一句（它列着这封信的所有附件），正文里提到附件名的不算（规范第 3 节，D79 第 14 条）
        named_withdrawn = any(
            line.kind == WITHDRAWN
            and (line.field == "attachments" if unit.mail is not None else line.field is None and body.name in line.text)
            for line in own
        )
        lines = [
            Line(
                sentence_id=sentence.sentence_id,
                text=sentence.text,
                kind=WITHDRAWN if owner_withdrawn or named_withdrawn or sentence.sentence_id in withdrawn else ATTACHMENT,
                origin=sentence.origin,
                quoted=sentence.quoted,
                line_no=sentence.line_no,
                sentence_no=sentence.sentence_no,
                attachment=body.number,
                attachment_name=body.name,
            )
            for sentence in sentences
        ]
        texts = [line.text.strip() for line in body.lines]
        offset = len(unit.source_text) + 1
        spans = _locate(texts, sentences)
        if spans is not None:
            lines = [
                replace(line, span=(start + offset, end + offset)) for line, (start, end) in zip(lines, spans, strict=True)
            ]
        units[index] = replace(
            unit, lines=(*unit.lines, *lines), source_text="\n".join([unit.source_text, *texts])
        )
    return units


def normalize(packet: Packet, *, withdrawn: Collection[str] = (), quote_evidence: Collection[str] = ()) -> NormalizedPacket:
    """L1。withdrawn 是单条撤回了的句子编号（原文索引里 withdrawn_at 不是空的）。quote_evidence 是当证据的引用行
    （D101，load_queued 按账本定：整本账本里找不到原话的，最早的那一份），这些行标 quoted_only。"""
    chat_groups: dict[tuple[int, int], list[Sentence]] = {}
    mail_groups: dict[int, list[Sentence]] = {}
    attachment_groups: dict[int, list[Sentence]] = {}
    notes = 0
    for sentence in iter_sentences(packet):
        if sentence.section_kind == SECTION_ATTACHMENT:
            if sentence.section_no is None:  # 附件小节的说明
                notes += 1
            else:  # 附件的正文：section_no 是第几块附件
                attachment_groups.setdefault(sentence.section_no, []).append(sentence)
            continue
        assert sentence.section_no is not None
        if packet.header.channel == "email":
            mail_groups.setdefault(sentence.section_no, []).append(sentence)
        else:
            assert sentence.line_no is not None
            chat_groups.setdefault((sentence.section_no, sentence.line_no), []).append(sentence)
    built = list(
        _mail_units(packet, mail_groups, withdrawn)
        if packet.header.channel == "email"
        else _chat_units(packet, chat_groups, withdrawn)
    )
    units = tuple(_with_attachments(packet, built, attachment_groups, withdrawn))
    kinds: dict[str, int] = {}
    for unit in units:
        for line in unit.lines:
            kinds[line.kind] = kinds.get(line.kind, 0) + 1
    return apply_quote_evidence(NormalizedPacket(packet.header, Scope.of(packet.header), units, notes, kinds), quote_evidence)
