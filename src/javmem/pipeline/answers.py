"""D83：认出主人在对话里的回答是在答哪一条待确认（J4.6 第 2 段；主人 11-17 选择卡第 5 题 A）。

Bot 问主人一条待确认时，把这一条的编号原样写在问的那句话里，写成“（待确认 Q3F9A1C）”，问法是“要不要照新说的改”
（packet-spec 第 5 节、第 7 节第 10 条）。主人答的照常录。javmem 写账时照这里的几条死规矩认，不调模型：

- **编号**（code_of）：`Q` 加 6 位大写十六进制，按这一条的内容算（哪一种、两边各是哪一条、哪一句），不按表里的行号：整本
  重建导回时是新插的行，行号会变。第一次给出去（列出来、查询接口给 Bot）时记进 review_queue.code，以后照记下的。
- **哪一句是问话**（find_code）：Bot 自己说的、新写的原话里，带着上面那种写法的编号。
- **哪一句算回答**（question_for，做法第 3.3 节）：Bot 那句问话以后主人的第一条消息（同一包，或者接着它的下一包；30 分钟
  以内；中间 Bot 没问别的待确认），或者主人引用了那句问话回的（隔多久都算）。别人答的、Bot 自己答的、转发的不算。
- **怎么认答案**（interpret）：几种待确认各一张对照表（做法第 3.4 节）：
  - 和冻结的冲突：说到新的那一边才有的名字、“用新的”“改吧”这些 → 用新值；说到旧的那一边才有的名字、“留原来的”
    “不用”这些 → 留原值；“都对”“两个都留”→ 两边都留。
  - 纠正挡着的（财务投资的、抽取没抽出来的）：“对”“改吧” → 照纠正改；“不用”“别改” → 不改。
  - 只说了新的那一条时问旧的那一条（correction_other）：“还在”“还算”“两边都在” → 不改；“不在了”“走了”“改吧” → 结束旧的。
    光答“对”“不”的分不清问法，不定。
  - 判断 C 拿不准的：“对”“有这回事” → 留下；“没有”“不对” → 不要。
  - 证据都撤回了的冻结的：“留着”“放回来” → 放回来；“删掉” → 删掉；“先搁着” → 接着藏着。
  - 实体有歧义：“不是同一个”“两个人” → 两边都留（是同一个要合并，照旧用合并工具）。
  - 同一句话里两个：“都对”“两个都” → 两边都留。
  - 软变化：“照旧”→ A，“先停（等……）”→ B，“不做了”“取消”→ C，“改成……”“交给……”→ D，“还没定”“看情况”→ E。
  对上两种的（名字和词放在一起比）、“回头再说”“不知道”这种、反问回来的、条件句（“要是不对就改”）、双重否定（“不改
  不行”）不定（返回 None），待确认照旧。一处词、一个名字所在的那一小段（逗号、句号隔开）里，它自己以外还有否定字（不、
  别、没……）的，这一处不算：“别按你说的改”不是改，“不是阚星野”不是说阚星野，“新的那个不对”“阚星野不对”也不是。
  两边都留要说得明白（“都对”“两个都留”“他俩一起”），光说“都是”“一起”“都要”的不算。认错比认不出坏：拿不准的都不定。

规矩是写死在代码里的普通说法，改了要换 RULES_VERSION。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

RULES_VERSION = "answers-v4"  # v2：第 2 段审阅（否定、条件句、名字和词一起比）；v3：复审（同一小段里有否定的都不算、双重否定）
# v4：K12 那条的审阅（旧的那一条还算不算：光说“换了”“结束”“走了”的不算结束，要说人离开了那一家；续签了、出差了、
# 以后才走的不定）
ANSWER_SECONDS = 30 * 60  # Bot 问完以后主人的第一条消息，这么久以内才算回答（引用了那句问话回的不限）

# 编号的写法：“（待确认 Q3F9A1C）”，全角半角括号、冒号、空白都认，大小写都认
_CODE = re.compile(r"[（(]\s*待确认\s*[:：]?\s*(Q[0-9A-F]{6})\s*[)）]", re.IGNORECASE)

# 编号按哪几样算（细分以后的种类 → claims 里的几项）。指纹有的不看句子编号（句子编号按位置算，包重发会挪）；挡住的那几种
# 不看原因（种类里已经分开了；判断 C 拿不准的原因重建时可能从 llm_failed 变成 llm_unsure，编号不跟着变）
_CODE_FIELDS: dict[str, tuple[str, ...]] = {
    "frozen_conflict": ("frozen", "new"),
    "ambiguous_entity": ("entity_id", "scope"),
    "evidence_withdrawn": ("reason", "fact", "relation"),
    "same_time": ("reason", "relations"),
    "soft_change": ("item", "key", "packet", "fingerprint", "sentence"),
    "manual_review": ("item", "key", "packet", "fingerprint", "sentence"),
    "correction": ("item", "key", "packet", "fingerprint", "sentence"),
    "correction_unmatched": ("item", "key", "packet", "fingerprint", "sentence"),
    "correction_other": ("item", "key", "packet", "fingerprint", "sentence"),
}

# 定成什么（和 javmem.pipeline.write 的待确认决定一样的写法）
KEEP_FROZEN = "keep_frozen"
KEEP_NEW = "keep_new"
KEEP_BOTH = "keep_both"
KEEP_HIDDEN = "keep_hidden"
DROP = "drop"

_LATER = re.compile(r"回头|再说|以后再|过两天|过几天|等等|等一下|不知道|不清楚|不确定|说不准|想想|我问问|问一下")
_BOTH = re.compile(r"都对|都没错|两个都留|两边都留|都留|两个都要|他俩一起|她俩一起|他们俩一起|两个人一起|两人一起")
_NEW = re.compile(r"用新的|按新的|照新的|新的对|新的那个|改成新的|就按你说的|照你说的|按你说的|改过来|改吧|就改|改掉")
_OLD = re.compile(r"留原来的|还是原来|原来的对|原来那个|照原来的|用原来的|没变|不用改|别改|不改|照旧|保持不变")
_YES = re.compile(r"^(?:对|对的|对呀|对啊|是|是的|是啊|好|好的|行|可以|嗯|嗯对|没错|就是|改|改吧|ok|okay|yes|yep|sure)$",
                  re.IGNORECASE)
_NO = re.compile(r"^(?:不|不用|不要|不对|不是|别|别改|不改|算了|先别|没有|no|nope)$", re.IGNORECASE)
_PIECES = re.compile(r"[\s，。,.；;！!？?、~～…]+")
_CLAUSES = re.compile(r"[，。,.；;！!？?、~～…]+")  # 认否定按小段（不按空白切：名字里可能有空格）
_ASKING = re.compile(r"[?？]\s*$|[吗呢]\s*[。！!]?\s*$")  # 回答本身是问句（“嗯？”“是老周吗”）
_COND = re.compile(r"要是|如果|假如|假设|万一|的话|若是")  # 条件句：“要是不对就改”不是定了
_DOUBLE = re.compile(r"不.{0,6}不(?:行|可|成|会)|非.{0,6}不可|没有不")  # 双重否定：“不改不行”是非改不可，不猜
_NEG = frozenset("不别没未非莫勿甭")  # 否定字：一处词、一个名字所在的小段里它自己以外有这些字的，这一处不算
_KEEP_HELD = re.compile(r"先搁着|先藏着|先放着|接着藏着|别动")
_RESTORE = re.compile(r"留着|放回来|恢复|还要")
_REMOVE = re.compile(r"删掉|删了|不要了|去掉")
_APART = re.compile(r"不是同一个|不是一个人|两个人|两家|不是一家|不一样的|是两个")
_SAME = re.compile(r"是同一个|同一个人|是一个人")
_SURE = re.compile(r"有这回事|确实")
_NONE = re.compile(r"没这回事|没有这回事|没有这事")
_NOT_MEANT = re.compile(r"不是这个意思|不是这意思")
# 只说了新的那一条时问旧的那一条（correction_other）：还在、还算是不改；人离开了那一家、改吧是结束旧的。光说“换了”“结束”
# “走了”的不算（“换了个岗位”“试用期结束了”“走了个流程”说的不是人走了，K12 审阅第 4 条）
_STILL = re.compile(r"还在|还算|也在|都在|两边都|没走|没换|照旧|留着|不用改|别改|不改")
_GONE = re.compile(r"改吧|就改|结束吧|就结束|可以结束|不在了|不在那|离职了|离开了|辞职了|不干了|不算了|跳槽了|换工作了"
                   r"|换公司了|换了家|换了一家|人走了|早走了|已经走了|早就走了|合同到期")
# 说了结束旧的、可人其实还在那边（续签了、出差了、转正了），或者是以后才走（这个月、下个月、月底）：不定（照“那一句的时候
# 结束”会认反、会提前，K12 复审第 4 条）
_STAYS = re.compile(r"续签|续约|续了|转正|出差|休假|请假|放假|外派|借调|培训|调岗|换岗|升职|升了|调到")
_LATER_END = re.compile(r"月底|月初|这个月|下个月|下月|这周|下周|下礼拜|年底|年后|明年|过几天|过两天|到时候|以后|之后")
_SOFT = (
    ("B", re.compile(r"先停|暂停|停一下|先放|先放放|缓一缓|暂缓|先不")),
    ("C", re.compile(r"不做了|取消|结束了|不要了|不弄了|停掉|作罢")),
    ("D", re.compile(r"改成|换成|交给|改由")),
    ("E", re.compile(r"还没定|不一定|看情况|只是说说|只是想想|再看看")),
    ("A", re.compile(r"照旧|还有效|没变|不变|继续")),
)


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def find_codes(text: str) -> frozenset[str]:
    """话里写着的几个编号（大写）。"""
    return frozenset(match.group(1).upper() for match in _CODE.finditer(_clean(text)))


def find_code(text: str) -> str | None:
    """问话里写着的编号（大写）；没有、写了几个不一样的返回 None（看不出问的是哪一条，不猜）。"""
    found = find_codes(text)
    return next(iter(found)) if len(found) == 1 else None


@dataclass(frozen=True)
class Said:
    """一条消息（原文索引里同一行的几句）。parts：新写的原话那几句（不是转发、外部内容、引用，没撤回），(编号, 原文)；
    quoted：引用的那几句。role 是说话人的身份（owner、bot、other）。"""

    role: str
    epoch: int
    parts: tuple[tuple[str, str], ...]
    quoted: tuple[tuple[str, str], ...] = ()

    @property
    def text(self) -> str:
        return "".join(text for _, text in self.parts)

    @property
    def sentences(self) -> tuple[str, ...]:
        return tuple(sentence for sentence, _ in self.parts)


EARLIER = "earlier"  # question_for：往前找到这一包开头还没有结果，要接着看上一包


@dataclass(frozen=True)
class Asked:
    """主人这一条答的是哪一句问话：问话那一句（带编号的那一句）、写着的编号、怎么认的（next 紧跟着的，quoted 引用了
    问话的）、问话那条消息的几句（哪一句撤回了这一问都算撤回，第 2 段审阅第 5 条）。"""

    sentence: str
    code: str
    how: str
    sentences: tuple[str, ...] = ()


def _asked(parts: Sequence[tuple[str, str]], code: str, how: str) -> Asked:
    sentence = next((one for one, text in parts if code in find_codes(text)), parts[-1][0])
    return Asked(sentence, code, how, tuple(one for one, _ in parts))


def question_for(messages: Sequence[Said], index: int) -> Asked | str | None:
    """messages[index] 是主人新写的一条消息（messages 按先后）：它是在答哪一句问话。不算回答的返回 None；往前找到
    messages 开头还没有结果的返回 EARLIER（调用的接上上一包的消息再问一遍）。

    - 引用了带编号的问话（引用的那几句里只有一个编号）：答的就是它，隔多久都算。
    - 不然往前找：碰到主人自己新写的消息（这一条不是问完以后的第一条）、隔了 ANSWER_SECONDS 以上，不算；碰到 Bot 新写的、
      带编号的消息：只带一个编号的就是答它，带了几个不一样的不算（看不出答的是哪一条）。别人的话、Bot 没带编号的话、
      转发的跳过。"""
    mine = messages[index]
    if mine.role != "owner" or not mine.parts:
        return None
    quoted = find_codes("".join(text for _, text in mine.quoted))
    if quoted:
        return _asked(mine.quoted, next(iter(quoted)), "quoted") if len(quoted) == 1 else None
    for earlier in reversed(messages[:index]):
        if mine.epoch - earlier.epoch > ANSWER_SECONDS:
            return None
        if not earlier.parts:
            continue
        if earlier.role == "owner":
            return None
        if earlier.role != "bot":
            continue
        codes = find_codes(earlier.text)
        if not codes:
            continue
        return _asked(earlier.parts, next(iter(codes)), "next") if len(codes) == 1 else None
    return EARLIER


def code_of(kind: str, claims: Mapping[str, object]) -> str:
    """一条待确认的编号：Q 加 6 位大写十六进制，按细分以后的种类（javmem.pipeline.write._review_type）和内容算。"""
    fields = _CODE_FIELDS.get(kind)
    picked = {name: claims.get(name) for name in fields} if fields else dict(claims)
    if picked.get("fingerprint"):
        picked.pop("sentence", None)
    text = json.dumps({"kind": kind, **picked}, ensure_ascii=False, sort_keys=True, default=str)
    return "Q" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:6].upper()


def _pieces(text: str) -> list[str]:
    return [piece for piece in _PIECES.split(_clean(text)) if piece]


def _only(pattern: re.Pattern[str], text: str) -> bool:
    """整句只是这几个字（“对，改吧”“不用”）：几段都对得上。"""
    pieces = _pieces(text)
    return bool(pieces) and all(pattern.match(piece) for piece in pieces)


def _fold(text: str) -> str:
    return re.sub(r"\s+", "", _clean(text).casefold())


@lru_cache(maxsize=256)
def _name_pattern(names: tuple[str, ...]) -> re.Pattern[str] | None:
    keys = sorted({key for key in (_fold(name) for name in names) if len(key) >= 2}, key=len, reverse=True)
    return re.compile("|".join(re.escape(key) for key in keys)) if keys else None


def _one(found: set[str]) -> str | None:
    return next(iter(found)) if len(found) == 1 else None


class _Reading:
    """一句回答按小段（逗号、句号这些隔开）认：一处词、一个名字算不算（它所在的小段里它自己以外有否定字的不算）。"""

    def __init__(self, text: str) -> None:
        self.clauses = [clause for clause in (_fold(part) for part in _CLAUSES.split(text)) if clause]

    def has(self, pattern: re.Pattern[str] | None) -> bool:
        """有没有一处不带否定的命中：命中那几个字以外，同一小段前前后后都没有否定字（“新的那个不对”“不用这么快就改”
        都不算；“别改”“不用改”这种词自己带着否定的照算）。"""
        if pattern is None:
            return False
        for clause in self.clauses:
            for match in pattern.finditer(clause):
                if not (_NEG & set(clause[:match.start()] + clause[match.end():])):
                    return True
        return False


def interpret(kind: str, text: str, *, new_names: Collection[str] = (), old_names: Collection[str] = ()) -> str | None:
    """主人这句回答定成什么（见开头）；认不出、对上两种的返回 None，待确认照旧。kind 是细分以后的种类。
    new_names、old_names：和冻结的冲突里，只有新的那一边、只有旧的那一边才有的名字（两头的实体名和别名）。
    软变化返回 A 到 E。返回的是回答的意思；照不照着办（比如删掉的不从对话里收）是写账那边的事。"""
    cleaned = _clean(text)
    if (not cleaned or _LATER.search(cleaned) or _ASKING.search(cleaned) or _COND.search(cleaned)
            or _DOUBLE.search(_fold(cleaned))):
        return None  # 回头再说、反问回来的、条件句、双重否定：不定
    said = _Reading(cleaned)
    if kind == "frozen_conflict":
        both = said.has(_BOTH)
        new = said.has(_name_pattern(tuple(new_names)))
        old = said.has(_name_pattern(tuple(old_names)))
        words_new, words_old = said.has(_NEW), said.has(_OLD)
        if both and new and old and not words_new and not words_old:
            return KEEP_BOTH  # “都对，阚星野和宓春生一起管”：两边的名字都说了
        found = {decision for decision, hit in ((KEEP_BOTH, both), (KEEP_NEW, new or words_new), (KEEP_FROZEN, old or words_old))
                 if hit}
        if found:
            return _one(found)
        if _only(_YES, cleaned):
            return KEEP_NEW
        if _only(_NO, cleaned):
            return KEEP_FROZEN
        return None
    if kind == "correction_other":  # 问的是旧的那一条还算不算：光答“对”“不”分不清问法，只认说明白了的
        found = {decision for decision, pattern in ((KEEP_HIDDEN, _STILL), (KEEP_NEW, _GONE)) if said.has(pattern)}
        if KEEP_NEW in found and (_STAYS.search(cleaned) or _LATER_END.search(cleaned)):
            return None  # “合同到期了，续签了”“做完这个月就结束”：不定
        return _one(found)
    if kind in ("correction", "correction_unmatched"):
        found = set()
        if said.has(_NEW) or _only(_YES, cleaned):
            found.add(KEEP_NEW)
        if said.has(_OLD) or said.has(_NOT_MEANT) or _only(_NO, cleaned):
            found.add(KEEP_HIDDEN)
        return _one(found)
    if kind == "manual_review":  # 判断 C 拿不准的关系：有这回事就留下，没有就不要
        found = set()
        if _only(_YES, cleaned) or said.has(_SURE):
            found.add(KEEP_NEW)
        if _only(_NO, cleaned) or said.has(_NONE):
            found.add(DROP)
        return _one(found)
    if kind == "evidence_withdrawn":
        found = {decision for decision, pattern in ((KEEP_HIDDEN, _KEEP_HELD), (KEEP_FROZEN, _RESTORE), (DROP, _REMOVE))
                 if said.has(pattern)}
        return _one(found)
    if kind == "ambiguous_entity":
        return KEEP_BOTH if said.has(_APART) and not said.has(_SAME) else None
    if kind == "same_time":
        return KEEP_BOTH if said.has(_BOTH) else None
    if kind == "soft_change":
        return _one({verdict for verdict, pattern in _SOFT if said.has(pattern)})
    return None
