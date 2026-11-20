"""预处理 B：认人认事（导航 D127、D129、D136、D146；J4.2 正式接进仓库，带 b4）。

L2 切好窗口以后、L3 抽之前：用本机的模型（llama.cpp 的 OpenAI 兼容接口，Qwen3.8-27B）看一个窗口，把“那张”“X 累购”
“小岑”这类说法对到已知名单上的一项，写成〔指：说法 = 名字〕，交给 L3 参考。原文一个字不改，证据照旧只抄原文。

**名单（Roster）：** 别名表里这个分区算数的组（第一个名字当名字，别的当“也叫”）、Bot 名单，加上系统自己认出来的项。
别名表的 apart 列成“不是同一个”，not_entities 列成“泛称”。

**b4：名单单独维护，不等 L3 的结果。** 一个包一个包按收包的先后来（导出的先后：包头的 covers_to，包号里的日子就是它）：

1. **收名字**（LEARN_FILE）：本机模型列出这个包每个窗口 T 行里有名字的东西（名单上有没有都列：叫它只列名单上没有的，它就一条都不列，
   b1 也碰过）。代码核一遍（说法、名字都要
   逐字在原文里，不收代称、泛称、我和主人、邮箱账号、Bot、说的是几样的），名单上原样有的不是新的；别的先挑候选、只跟同类比
   （preb_pick），问它“是这几项里的哪一项，还是名单上没有”：选中一项、把握到 KEEP_AT 的记成那一项的“也叫”，选“没有”、
   把握到 KEEP_AT 的另起一项，没把握的不收。
2. **标〔指〕**（PROPOSE_FILE，b3 原样）：先让模型提，代码核一遍；说法原样是名单上的叫法、只对得上一项的直接标；别的先认
   是哪一类，只在同一类最像的几项里选，最高的那一项到 KEEP_AT 才标。用的名单已经带上这个包自己收的名字。
3. **标上的说法记成“也叫”**：后面的包原样碰到就直接标。

L3 不等这些：extract.run_extraction 一个包标完，就把它的窗口交给 L3（两路），本机模型接着做下一个包。

**防合错**（preb_keys，b2 起）：日子、编号、金额对不上的，名字再像也不标成同一样、不记成“也叫”；几项只差日子编号金额、
说法分不出是哪一项的，不直接标、不记、不另起一个盖住几项的名字。拦下的都记下来。**股票和单子不合**（b3）。

**可见范围：** 系统认出来的项、学来的“也叫”，只给分区一样、敏感标记是它的子集的窗口用（和 batch._visible 同一条规矩），
不然一个分区学来的名字会被标进别的分区的窗口、发给 L3、写进账本。别名表的组照它写的 where 算。private 的窗口不收名字、
不标。只用现行版本的包里学来的。

**存在结果库**（javmem.pipeline.store 的 preb_* 表）：标注按（窗口编号、没加标注的窗口文字的指纹、预处理版本）存，
extract.prepare 写窗口时照原样加上：核对、写账、合并工具算出来的键和抽的时候一样。标注不进账本。撤回、彻底删除连着删
（store.doomed、delete_windows、forget_preb）。没标成的窗口（本机模型出错、太长、向量服务出错）不存，照原样交给 L3。

每次调本机模型记一行账：标签是抽取的标签加 -b4（不算进抽取的预算），用途 pre_b_learn、pre_b_learn_choose、pre_b_propose、
pre_b_choose；向量服务的调用也记（pre_b_embed）。

例子都是编的。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from importlib import resources

from javmem.config import ConfigError
from javmem.embedding import Embedder, EmbeddingError
from javmem.llm import _is_loopback
from javmem.pipeline.normalize import Scope
from javmem.pipeline.preb_keys import Values, base_key, clash, values_near, values_of, values_of_names
from javmem.pipeline.preb_pick import (
    BOT,
    COMPANY,
    DEAL,
    KIND_OF_CLASS,
    LABELS,
    NONE_LABEL,
    POINTER,
    STOCK,
    TOP_K,
    DealFinder,
    DealPhrase,
    Picker,
    bare_ticker,
    choice_probs,
    class_by_shape,
    class_of,
    inside,
    occurrences,
    overlaps,
    plural_at,
)
from javmem.pipeline.render import RenderedWindow, render_window
from javmem.pipeline.store import ResultStore
from javmem.pipeline.window import Window
from javmem.redaction import RedactionPattern
from javmem.timeutil import iso
from javmem.usage import TokenLedger
from javmem.zh.lexicon import Lexicon

PRE_B_VERSION = "b4"  # b3 + 名单单独维护（收名字、标上的说法记成也叫）、可见范围、按导出先后（J4.2）
PROPOSE_FILE = "pre-b2.md"  # “提”的提示词：b2.1、b3 原样（加“先看是哪一类”那一节 Qwen 就少提，b3 试过）
LEARN_FILE = "pre-b4.md"  # “收名字”的提示词（b4）
NOTE_LABEL = "指"
KEEP_AT = 0.9  # 选的那一项的把握到这么多才算
MAX_PER_LINE = 4
MAX_PER_WINDOW = 80  # 代码核过的，一个窗口最多留几条去问
CHUNK_LINES = 40  # 一次只问这么多行 T（整个窗口照样给它看），回答不会太长被截断
MAX_PER_CHUNK = 60
MAX_LEARNED = 150  # 名单里系统自己认出来的最多放几项（放后来的）
MAX_ALIASES = 12  # 一项的“也叫”最多几个，多了不再学
MAX_PROMPT_CHARS = 45000  # 提示词、名单、窗口加起来超过这么多字的不收、不标（本机模型的上下文）
ASK_TOKENS = 4000
OPTION_ALIASES = 6  # 选项里每一项最多列几个“也叫”

ENV_URL = "JAVMEM_PRE_B_URL"  # 本机模型的 chat/completions 地址；不配就不做预处理 B
ENV_MODEL = "JAVMEM_PRE_B_MODEL"
ENV_PROMPT_CACHE = "JAVMEM_PRE_B_PROMPT_CACHE"  # on（默认，和 b3 一样）/ off：llama.cpp 的提示词缓存（认人认事.md 7.4）
DEFAULT_MODEL = "qwen3.8-27b"

_EMAIL = re.compile(r"[^\s@]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")  # 带 @、@ 后面像域名的
_HANDLE = re.compile(r"(?=[a-z0-9._]*[a-z])[a-z0-9]+(?:[._][a-z0-9]+)+")  # 全是小写字母、数字，中间连着点或下划线
_ESCAPE = str.maketrans({"〔": "［", "〕": "］", "\n": " ", "\r": " "})
_LIST_SPLIT = re.compile(r"\s*(?:[/／、，,;；]|\s&\s|\sand\s)\s*")  # 几样东西列在一起（“和”“与”不算：名字里也有）
_CLASSES = tuple(KIND_OF_CLASS)  # 收名字时 Qwen 写的类：人、公司、股票、单子、项目、地点


def _array_schema(key: str, fields: Mapping[str, dict[str, object]]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [key],
        "properties": {
            key: {
                "type": "array",
                "maxItems": MAX_PER_CHUNK,
                "items": {"type": "object", "additionalProperties": False, "required": list(fields), "properties": dict(fields)},
            }
        },
    }


PROPOSE_SCHEMA = _array_schema("标注", {"行": {"type": "string"}, "说法": {"type": "string"}, "名字": {"type": "string"}})
LEARN_SCHEMA = _array_schema(
    "新名字",
    {"行": {"type": "string"}, "说法": {"type": "string"}, "名字": {"type": "string"},
     "类": {"type": "string", "enum": list(_CLASSES)}},
)
PROPOSE_TAIL = (
    "\n\n【要做的】只看 {first} 到 {last} 这几行（别的行只帮着理解），把该对的说法都找出来，照规矩只对有把握的。只输出 JSON："
    '{{"标注": [{{"行": "T 编号", "说法": "原文里的那几个字", "名字": "名单上那一项的名字"}}]}}；没有就给空的。'
)
CHOOSE_TAIL = (
    "\n\n【要核的】{ref} 里的“{mention}”，指的是下面哪一项？下面几项是名单上和这个说法同一类（{cls}）、最像的几项。"
    "照规矩，有把握才选；名单上没有、看不出是哪一项、说的是几样东西的，选 X。\n{options}\nX. 都不是\n只回答一个字母。"
)
LEARN_TAIL = (
    "\n\n【要做的】只看 {first} 到 {last} 这几行（别的行只帮着理解），把有名字的东西都列出来（名单上有没有都列）。只输出 JSON："
    '{{"新名字": [{{"行": "T 编号", "说法": "原文里的那几个字", "名字": "这个窗口原文里写得最全的写法", "类": "人"}}]}}；'
    "没有就给空的。"
)
LEARN_CHOOSE_TAIL = (
    "\n\n【要核的】{ref} 里的“{mention}”（这个窗口里写得最全的是“{name}”），是下面哪一项？下面几项是名单上和它同一类"
    "（{cls}）、最像的几项。照规矩，看得出就是其中一项的才选它；名单上没有的、是另一样东西的，选 X。\n{options}\n"
    "X. 名单上没有\n只回答一个字母。"
)


def load_prompt(name: str) -> str:
    text = (resources.files("javmem.pipeline") / "prompts" / name).read_text(encoding="utf-8")
    return text.replace("\r\n", "\n")


def looks_like_account(mention: str) -> bool:
    """邮件地址（cen.xiaoyan@east-bank.example）、账号（cen.xy、wei_ming2）：不标〔指〕，不收名字（b2.1）。
    大写的股票代号（EBK.B）、普通英文缩写（e.g.、U.S.）、带空格的、聊天里的 @某人（没有域名）都不算。"""
    text = mention.strip()
    return bool(_EMAIL.search(text)) or bool(_HANDLE.fullmatch(text))


def _clean(text: object) -> str:
    """写进名单和标注的名字：〔〕换成［］、换行换成空格，伪造不了别的标注和行。"""
    return str(text).translate(_ESCAPE).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return iso(datetime.now(timezone.utc).astimezone())


def window_context(window: Window) -> str:
    """标注、收名字的记号除了按窗口编号和文字的指纹，还按这个存：窗口的可见范围和正文那几个包（T 行的包）的版本。同一个包
    换了范围、换了版本重发，窗口编号、文字都可能一样，可以前的标注是照以前那个范围的名单标的，不能照样用。上文那几行的包
    不算：上文的包换了版本、文字没变的，后一个包的窗口一字没变，标注照样用（复审查出来的：算上它，键就对不上了）。"""
    packets = sorted({(line.packet_id, line.revision) for line in window.targets})
    return _sha(json.dumps({"scope": window.scope.as_dict(), "packets": [list(one) for one in packets]},
                           ensure_ascii=False, sort_keys=True))


def _own(rendered: RenderedWindow, ref: str, text: object) -> str:
    """存进细节的说法：逐字在那一行原文里的才留（窗口自己的字），本机模型自己写的（可能是名单上别处学来的名字）不留。"""
    text = str(text or "")
    return text if text and text in rendered.shown.get(ref, "") else ""


def _own_ref(rendered: RenderedWindow, ref: object) -> str:
    """存进细节的“行”：这个窗口里真有的编号才留，本机模型写在这里的别的字不留。"""
    ref = str(ref or "")
    return ref if ref in rendered.shown else ""


def _visible(source: Scope, window: Scope) -> bool:
    """看得到这个窗口的人都看得到 source：分区一样，source 的敏感标记是窗口的子集（batch._visible 同一条规矩）。"""
    return tuple(source.partitions) == tuple(window.partitions) and set(source.sensitive) <= set(window.sensitive)


# ---------- 设置 ----------


@dataclass(frozen=True)
class PreBSettings:
    url: str
    model: str
    prompt_cache: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PreBSettings | None:
        """JAVMEM_PRE_B_URL 没配：不做预处理 B，返回 None。地址只能是本机的：认人认事要把整个窗口发给它（设计稿第 2 节：
        数据不出这台电脑）。"""
        env = os.environ if env is None else env
        url = env.get(ENV_URL, "").strip()
        if not url:
            return None
        try:
            parts = urllib.parse.urlsplit(url)
            scheme, host = parts.scheme, parts.hostname
        except ValueError:
            scheme, host = "", None
        if scheme not in ("http", "https") or not _is_loopback(host):
            raise ConfigError(f"{ENV_URL} 只能写本机的地址（127.0.0.1、localhost）：认人认事要把原文发给它，数据不出这台电脑")
        cache = env.get(ENV_PROMPT_CACHE, "").strip().lower() or "on"
        if cache not in ("on", "off"):
            raise ConfigError(f"{ENV_PROMPT_CACHE} 只能写 on 或 off")
        return cls(url, env.get(ENV_MODEL, "").strip() or DEFAULT_MODEL, cache == "on")


# ---------- 名单 ----------


@dataclass
class Item:
    name: str
    kind: str  # 别名表的类型、Bot，或者 L3 的实体类型
    aliases: tuple[str, ...] = ()
    source: str = "别名表"  # 别名表、Bot 名单，或者学来的那个窗口的编号
    also_kinds: tuple[str, ...] = ()  # 别名表的 from_types：L3 写成这几种类型的也算这一项
    item_id: int | None = None  # 系统认出来的：preb_items 的编号
    depends: tuple[str, ...] = ()  # 这一项用到的学来的东西（"item:编号"、"alias:编号"）：拦下的那几行记着，撤回时连着删

    @property
    def learned(self) -> bool:
        return self.item_id is not None

    @property
    def target(self) -> str:
        """学来的“也叫”记在谁名下：系统认出来的按编号，别名表、Bot 名单的按名字。"""
        return f"item:{self.item_id}" if self.item_id is not None else f"name:{self.name}"

    @property
    def label(self) -> str:
        """存进细节、拦下的那一行时写的：系统认出来的写编号（撤回了它的出处，别的窗口的记录里不留它的名字）。"""
        return f"#{self.item_id}" if self.item_id is not None else self.name

    def line(self) -> str:
        also = f"也叫：{'；'.join(self.aliases)}" if self.aliases else ""
        return f"- {self.name}〔{self.kind}〕{also}"

    def option(self) -> str:
        """给 Qwen 选的那一行：名字、类别、最多几个“也叫”。"""
        aliases = self.aliases[:OPTION_ALIASES]
        also = f"也叫：{'；'.join(aliases)}" if aliases else ""
        return f"{self.name}〔{self.kind}〕{also}"

    def doc(self) -> str:
        """挑候选时拿去算向量的字：名字和“也叫”（最多 8 个）。"""
        return "；".join((self.name, *self.aliases[:8]))


def _group_usable(lexicon: Lexicon, where: tuple[str, ...] | None, partitions: Sequence[str]) -> bool:
    """别名表这一组在这些分区的窗口里算不算数（和 Lexicon.group_of 一样）。"""
    where = where if where is not None else lexicon.default_where
    return where is None or set(partitions) <= set(where)


class Roster:
    """一个可见范围看得到的名单：别名表（这个分区算数的组）+ Bot 名单 + 看得到的系统认出来的项（最近 MAX_LEARNED 项），
    “也叫”里加上看得到的学来的写法。只读：学到的东西由 Rosters 存进结果库，再给新的 Roster。"""

    def __init__(
        self,
        lexicon: Lexicon,
        *,
        partitions: Sequence[str] = (),
        learned: Sequence[Item] = (),
        extra_aliases: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    ) -> None:
        from javmem.pipeline.merge import name_key  # 别名表、比对写法和 L5 用同一个

        self._key = name_key
        self.lexicon = lexicon
        extra = extra_aliases or {}
        self.fixed: list[Item] = []
        for group in lexicon.groups:
            if not _group_usable(lexicon, group.where, partitions):
                continue
            names = [_clean(name) for name in group.names if _clean(name)]
            if names:
                item = Item(names[0], group.type, tuple(dict.fromkeys(names[1:])), "别名表", tuple(group.from_types))
                self.fixed.append(item)
        for bot in lexicon.bots:
            names = [_clean(name) for name in (*bot.names, *bot.plain) if _clean(name)]
            if names:
                self.fixed.append(Item(names[0], BOT, tuple(n for n in dict.fromkeys(names[1:]) if n != names[0]), "Bot 名单"))
        for item in self.fixed:
            more = [(alias, label) for alias, label in extra.get(item.target, ()) if alias not in item.aliases and alias != item.name]
            item.aliases = (*item.aliases, *(alias for alias, _ in more))
            item.depends = tuple(label for _, label in more)
        self.learned: list[Item] = list(learned)[-MAX_LEARNED:]
        self.apart = [tuple(_clean(name) for name in one.names) for one in lexicon.apart]
        self.vague = tuple(_clean(name) for name in lexicon.not_entities)
        self._finder: tuple[object, DealFinder] | None = None

    def items(self) -> list[Item]:
        return [*self.fixed, *self.learned]

    def text(self) -> str:
        rows = ["【已知名单】", *(item.line() for item in self.fixed)]
        if self.learned:
            rows.append("（下面几项是前面的窗口认出来的，不一定全对）")
            rows.extend(item.line() for item in self.learned)
        if self.apart:
            rows.append("不是同一个：" + "；".join(" 和 ".join(names) for names in self.apart))
        if self.vague:
            rows.append("泛称（单独出现时不指哪一项）：" + "、".join(self.vague))
        return "\n".join(rows)

    def key(self, text: str) -> str:
        return self._key(text)

    def resolve(self, name: str) -> Item | None:
        """模型写的名字 → 名单上的一项。写成了别名的，别名只对得上一项才认。"""
        wanted = _clean(name)
        items = self.items()
        exact = [item for item in items if item.name == wanted]
        if exact:
            return exact[0]
        hits = [item for item in items if wanted in item.aliases]
        return hits[0] if len(hits) == 1 else None

    def by_label(self, label: str) -> Item | None:
        return next((item for item in self.items() if item.label == label), None)

    def vague_word(self, text: str) -> bool:
        return self.lexicon.not_entity(text) or text.strip() in ("我", "主人", "我们")

    @staticmethod
    def pointer_word(text: str) -> bool:
        """“那张”“这单”“它”这类代称：能标〔指〕，不能当“也叫”（不是名字）。"""
        text = text.strip()
        return not text or text in ("它", "他", "她", "它们", "他们") or (len(text) <= 4 and text[0] in "那这该此")

    def listed(self, mention: str, item: Item) -> bool:
        """说法就是这一项的名字或“也叫”里的一个（比对写法一样）。"""
        key = self._key(mention)
        return any(self._key(name) == key for name in (item.name, *item.aliases))

    def only_item(self, mention: str) -> Item | None:
        """说法原样是名单上某一项的名字或“也叫”（比对写法一样），而且只对得上这一项：返回它。"""
        hits = [item for item in self.items() if self.listed(mention, item)]
        return hits[0] if len(hits) == 1 else None

    def values(self, item: Item) -> Values:
        """这一项的名字和“也叫”里带着的日子、编号、金额。"""
        return values_of_names((item.name, *item.aliases))

    def lookalikes(self, text: str) -> list[Item]:
        """名单上底子一样（去掉日子、编号、金额以后一样）的几项。"""
        base = base_key(text)
        if not base:
            return []
        return [item for item in self.items() if any(base_key(name) == base for name in (item.name, *item.aliases))]

    def ambiguous(self, text: str, near: Values | None = None, extra: Sequence[Item] = ()) -> list[Item]:
        """防合错 c：名单上有几项只差日子、编号、金额（9/03、9/07 两张 X3 累购），说法（连同紧挨着的字）却一个能分开
        它们的日子、编号、金额都没带（“X3 累购”）→ 返回那几项；分得出、或者只有一项的 → 空。说法带着值、
        却和哪一项对不上的，归防合错 a 拦。"""
        looks = self.lookalikes(text)
        base = base_key(text)
        looks += [item for item in extra if base and any(base_key(name) == base for name in (item.name, *item.aliases))]
        if len(looks) < 2:
            return []
        own = near if near is not None else values_of(text)
        kinds = {kind for item in looks for kind, found in self.values(item).items() if found}
        if not kinds or any(own.get(kind) for kind in kinds):
            return []
        return looks

    # ---------- b3：类 ----------

    def of_class(self, cls: str) -> list[Item]:
        """名单上这一类的几项（按名单的先后）。"""
        return [item for item in self.items() if class_of(item.kind) == cls]

    def same_class(self, kind: str, item: Item) -> bool:
        """一个类型和名单上这一项是不是一类（别名表的 from_types 也算）。没写类型的算；名单上这一项是 Bot 的算。"""
        kind = (kind or "").strip()
        return (not kind or class_of(item.kind) == BOT or class_of(kind) == class_of(item.kind)
                or kind in item.also_kinds)

    def deal_finder(self) -> DealFinder:
        """认单子说法用的：名单上股票、公司的叫法（名单变了就重建）。"""
        stocks = tuple(name for item in self.items() if class_of(item.kind) == STOCK for name in (item.name, *item.aliases))
        companies = tuple(name for item in self.items() if class_of(item.kind) == COMPANY for name in (item.name, *item.aliases))
        if self._finder is None or self._finder[0] != (stocks, companies):
            self._finder = ((stocks, companies), DealFinder(stocks, companies))
        return self._finder[1]

    def mention_class(self, mention: str, item: Item | None) -> str | None:
        """说法是哪一类，按先后：说法本身就是单子的说法（FCN、累购、X+Y……）或者“那张”“这单”→ 单子；原样是名单上的叫法
        → 那几项的类（几项不是一类的不算）；Qwen 提的那一项的类，可光是一个股票代号样子的写法（“ZKX”）Qwen 提成了单子的，
        当股票；都没有的，代号样子的当股票，别的看样子猜（小岑、X 总像人，X 银行像公司）。看样子的不压过 Qwen 提的那一项：
        “阿某”也可能是股票，“她”也可能是 Bot。"""
        if self.deal_finder().phrases(mention) or POINTER.fullmatch(mention.strip()):
            return DEAL
        hits = {class_of(one.kind) for one in self.items() if self.listed(mention, one)}
        if len(hits) == 1:
            return hits.pop()
        if item is not None:  # 光是代号的写法 Qwen 提成了单子：当股票（提成公司、人的照 Qwen 的，代号也可能是银行的缩写）
            return STOCK if bare_ticker(mention) and class_of(item.kind) == DEAL else class_of(item.kind)
        if bare_ticker(mention):
            return STOCK
        return class_by_shape(mention)

    def crosses_class(self, alias: str, target: Item) -> bool:
        """学名字用：这个写法是别的类的叫法，不记成 target 的“也叫”。名单上原样是别的类某一项的叫法（光“YSJ”是股票，
        不记到 YSJ 累购底下）；单子收不进光是股票代号的写法；股票、公司收不进单子的说法。Bot 不管。"""
        cls = class_of(target.kind)
        if cls == BOT:
            return False
        if any(one is not target and class_of(one.kind) != cls and self.listed(alias, one) for one in self.items()):
            return True
        if cls == DEAL and bare_ticker(alias):
            return True
        return cls in (STOCK, COMPANY) and bool(self.deal_finder().phrases(alias))


class Rosters:
    """这个结果库里系统认出来的项和学来的“也叫”（store 的 preb_items、preb_aliases），按可见范围给名单（Roster）。

    current：现行的（包号, 版本）；给了的，旧版本、撤回了的包里学来的不用（L2 也不拿旧版本当上文）。None 不查。"""

    def __init__(self, store: ResultStore, lexicon: Lexicon, *, current: set[tuple[str, int]] | None = None) -> None:
        self.store = store
        self.lexicon = lexicon
        self.current = current
        self._lock = threading.Lock()
        self._views: dict[tuple[tuple[str, ...], tuple[str, ...]], Roster] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._items = [dict(row) for row in self.store.preb_items(PRE_B_VERSION)]
            self._aliases = [dict(row) for row in self.store.preb_aliases(PRE_B_VERSION)]
            self._views = {}

    def _usable(self, row: Mapping[str, object], scope: Scope) -> bool:
        """看得到出处的窗口才用；出处的每一句都要是现行版本的包里的。"""
        source = Scope(tuple(json.loads(str(row["partitions"]))), tuple(json.loads(str(row["sensitive"]))))
        if not _visible(source, scope):
            return False
        if self.current is None:
            return True
        return all((str(packet_id), int(revision)) in self.current for packet_id, revision, _ in json.loads(str(row["sources"])))

    def view(self, scope: Scope) -> Roster:
        key = (tuple(scope.partitions), tuple(scope.sensitive))
        with self._lock:
            found = self._views.get(key)
            if found is not None:
                return found
            extra: dict[str, list[tuple[str, str]]] = {}
            for row in self._aliases:
                if self._usable(row, scope):
                    pairs = extra.setdefault(str(row["target"]), [])
                    if all(alias != row["alias"] for alias, _ in pairs):
                        pairs.append((str(row["alias"]), f"alias:{row['alias_id']}"))
            learned = []
            for row in self._items:
                if self._usable(row, scope):
                    item = Item(str(row["name"]), str(row["kind"]), (), str(row["window_id"]), (), int(row["item_id"]))
                    pairs = [(alias, label) for alias, label in extra.get(item.target, ()) if alias != item.name]
                    item.aliases = tuple(alias for alias, _ in pairs)
                    item.depends = (item.target, *(label for _, label in pairs))
                    learned.append(item)
            roster = Roster(self.lexicon, partitions=scope.partitions, learned=learned, extra_aliases=extra)
            self._views[key] = roster
            return roster


# ---------- 标注写进窗口 ----------


def apply_notes(rendered: RenderedWindow, notes: Mapping[str, Sequence[Sequence[str]]]) -> RenderedWindow:
    """把标注写进窗口：那一行编号后面（开头已经有〔引用〕、预处理 A 的〔日子〕这类的，接在它们后面）加〔指：说法 = 名字；…〕。
    别的行一字不变；shown（L4 比引文用的原文）不变。"""
    if not notes:
        return rendered
    rows = rendered.text.split("\n")
    out = []
    for row in rows:
        ref, space, rest = row.partition(" ")
        pairs = notes.get(ref) if space else None
        if pairs and ref in rendered.shown:
            note = f"〔{NOTE_LABEL}：" + "；".join(f"{_clean(mention)} = {_clean(name)}" for mention, name in pairs) + "〕"
            at = 0  # 原文里的〔〕都换成了［］，开头连着的〔…〕只能是标注：接在它们后面
            while rest.startswith("〔", at) and "〕" in rest[at:]:
                at = rest.index("〕", at) + 1
            row = f"{ref} {rest[:at]}{note}{rest[at:]}"
        out.append(row)
    text = "\n".join(out)
    return replace(rendered, text=text, sha256=_sha(text))


def packet_groups(windows: Sequence[Window], order: Mapping[str, float] | None = None) -> list[list[Window]]:
    """按包分组（窗口里能当证据的句子在哪个包），包按收包的先后：order 给了的按它（包号 → 导出的时间，包头的 covers_to），
    没给的按包号（包号里的日子、钟点就是 covers_to）；一样的按包号。包里的窗口按时间（最早一句能当证据的话）。"""

    def start(window: Window) -> str:
        times = [block.said_at.astimezone(timezone.utc).isoformat() for block in window.blocks if not block.context]
        return min(times) if times else "9999"

    groups: dict[str, list[Window]] = {}
    for window in windows:
        targets = window.target_packets
        groups.setdefault(targets[0] if targets else window.window_id, []).append(window)
    order = order or {}

    def when(packet_id: str) -> tuple[float, str]:
        return (order.get(packet_id, math.inf) if order else 0.0, packet_id)

    return [sorted(groups[packet_id], key=lambda w: (start(w), w.window_id)) for packet_id in sorted(groups, key=when)]


# ---------- 本机模型 ----------


class LocalModel:
    """llama.cpp 的 OpenAI 兼容接口。只连本机，不走代理；没有密钥。prompt_cache：关掉的请求里写 cache_prompt: false。"""

    def __init__(self, url: str, model: str, *, prompt_cache: bool = True, timeout: float = 600.0) -> None:
        self.url = url
        self.name = model
        self.prompt_cache = prompt_cache
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def chat(
        self, system: str, user: str, *, max_tokens: int, schema: Mapping[str, object] | None = None, logprobs: bool = False
    ) -> tuple[dict[str, object], float]:
        body: dict[str, object] = {
            "model": self.name,
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if not self.prompt_cache:
            body["cache_prompt"] = False
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "javmem_pre_b", "strict": True, "schema": schema}}
        if logprobs:
            body["logprobs"] = True
            body["top_logprobs"] = 10
        request = urllib.request.Request(
            self.url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"}
        )
        started = time.monotonic()
        with self._opener.open(request, timeout=self.timeout) as response:
            out = json.loads(response.read().decode("utf-8"))
        return out, time.monotonic() - started


@dataclass
class _Learning:
    """一个窗口收名字的结果（还没记进名单）。"""

    decisions: list[dict[str, object]] = field(default_factory=list)
    dropped: list[dict[str, object]] = field(default_factory=list)
    found: int = 0
    status: str = "ok"
    seconds: float = 0.0


_LOCAL_ERRORS = (urllib.error.URLError, OSError, ValueError, http.client.HTTPException)


class _Late(Exception):
    """过了截止时间：这个窗口不收、不标，照原样交给 L3（L3 那边过了时间也不再开始新的调用）。"""


class PreB:
    """一遍里的认人认事：本机模型、向量模型（挑候选）、名单（Rosters，存在结果库里）、记账。
    extract.run_extraction 一个包一个包调 run_packet。"""

    def __init__(
        self,
        model: LocalModel,
        lexicon: Lexicon,
        *,
        ledger: TokenLedger | None = None,
        tag: str | None = None,
        log: Callable[[str], None] | None = None,
        workers: int = 2,
        embedder: Embedder | None = None,
        order: Mapping[str, float] | None = None,
        current: set[tuple[str, int]] | None = None,
    ) -> None:
        self.model = model
        self.lexicon = lexicon
        self.ledger = ledger
        self.tag = f"{tag}-{PRE_B_VERSION}" if tag else f"javmem-{PRE_B_VERSION}"
        self.log = log
        self.workers = max(1, min(2, workers))  # 本机模型同时最多 2 路，别的程序也在用
        self.system = load_prompt(PROPOSE_FILE)
        self.learn_system = load_prompt(LEARN_FILE)
        self.order = dict(order or {})
        self.current = current
        self.stats: Counter[str] = Counter()
        self._lock = threading.Lock()
        self.picker = Picker(embedder, self._record_embed)  # 没有向量服务（测试）：同一类的按名单的先后挑
        self.rosters: Rosters | None = None
        self.deadline: float | None = None  # time.monotonic() 的时刻：过了就不再问本机模型（run_extraction 照 --max-minutes 给）

    def describe(self) -> dict[str, object]:
        return {"version": PRE_B_VERSION, "model": self.model.name, "keep_at": KEEP_AT, "tag": self.tag,
                "embedding": self.picker.name, "top_k": TOP_K, "prompt_cache": getattr(self.model, "prompt_cache", True)}

    def groups(self, windows: Sequence[Window]) -> list[list[Window]]:
        return packet_groups(windows, self.order)

    def _rosters(self, store: ResultStore) -> Rosters:
        if self.rosters is None or self.rosters.store is not store:
            self.rosters = Rosters(store, self.lexicon, current=self.current)
        return self.rosters

    def _say(self, text: str) -> None:
        if self.log is not None:
            self.log(text)

    def _count(self, **counts: int) -> None:
        with self._lock:
            for name, value in counts.items():
                self.stats[name.replace("_", "")] += value

    # ---------- 记账 ----------

    def _record(self, window: Window, purpose: str, status: str, usage: Mapping[str, object], seconds: float) -> None:
        if self.ledger is None:
            return
        details = usage.get("prompt_tokens_details") or {}
        self.ledger.append(
            {
                "at": _now(),
                "tag": self.tag,
                "purpose": purpose,
                "model": self.model.name,
                "window_id": window.window_id,
                "cache_key": f"{PRE_B_VERSION}:{window.window_id}",
                "attempt": 1,
                "status": status,
                "input_tokens": int(usage.get("prompt_tokens") or 0),  # type: ignore[call-overload]
                "output_tokens": int(usage.get("completion_tokens") or 0),  # type: ignore[call-overload]
                "reasoning_tokens": 0,
                "cached_tokens": int(details.get("cached_tokens") or 0) if isinstance(details, Mapping) else 0,
                "seconds": round(seconds, 3),
            }
        )

    def _record_embed(self, window_id: str, digest: str, attempt: int, status: str, tokens: int, seconds: float,
                      estimated: bool) -> None:
        if self.ledger is None:
            return
        self.ledger.append(
            {
                "at": _now(),
                "tag": self.tag,
                "purpose": "pre_b_embed",
                "model": self.picker.name or "",
                "window_id": window_id,
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

    # ---------- 一个包 ----------

    def run_packet(self, windows: Sequence[Window], store: ResultStore, patterns: Sequence[RedactionPattern]) -> None:
        """一个包的窗口：收名字（没收过的）→ 记进名单 → 标〔指〕（没标过的）→ 标上的说法记成“也叫”。
        本机模型、向量服务出错，太长的窗口，照原样交给 L3。"""
        rosters = self._rosters(store)
        todo = []
        for window in windows:
            if window.scope.private:
                continue
            rendered = render_window(window, patterns)
            if rendered.leftover:
                continue
            todo.append((window, rendered))
        learn = [(w, r) for w, r in todo if not store.preb_learned(w.window_id, r.sha256, PRE_B_VERSION, window_context(w))]
        views = {w.window_id: rosters.view(w.scope) for w, _ in learn}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            found = list(pool.map(lambda item: self._learn_one(item[0], item[1], views[item[0].window_id]), learn))
        for (window, rendered), learning in zip(learn, found):  # 按窗口的先后记，结果不随哪一路先回来变
            if learning is not None:
                self._commit_learning(rosters, store, window, rendered, learning)
        mark = []
        for window, rendered in todo:
            if store.preb_notes(window.window_id, rendered.sha256, PRE_B_VERSION, window_context(window)) is not None:
                self._count(存过的窗口=1)
                continue
            mark.append((window, rendered))
        views = {w.window_id: rosters.view(w.scope) for w, _ in mark}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            kept = list(pool.map(lambda item: self._mark_one(item[0], item[1], views[item[0].window_id], store), mark))
        for (window, rendered), marked in zip(mark, kept):
            if marked:
                self._learn_aliases(rosters, store, window, rendered, marked)

    # ---------- 问本机模型 ----------

    def _ask_json(self, window: Window, system: str, user: str, schema: Mapping[str, object], key: str,
                  purpose: str) -> tuple[list[object], str]:
        """问一段，要 JSON：→ (列出来的, 状态)。连不上、出错的抛出去；过了截止时间的抛 _Late。"""
        self._check_time()
        out, seconds = self.model.chat(system, user, max_tokens=ASK_TOKENS, schema=schema)
        try:
            usage = out.get("usage") or {}  # type: ignore[union-attr]
            choice = (out.get("choices") or [{}])[0]  # type: ignore[index, union-attr]
            status = "length" if choice.get("finish_reason") == "length" else "ok"
            content = str((choice.get("message") or {}).get("content") or "")
        except (AttributeError, IndexError, KeyError, TypeError):  # 回的不是该有的样子：当格式错，这一段不算
            self._record(window, purpose, "format_error", {}, seconds)
            return [], "format_error"
        self._record(window, purpose, status, usage if isinstance(usage, Mapping) else {}, seconds)
        try:
            found = json.loads(content).get(key) or []
        except (ValueError, AttributeError):
            return [], "format_error" if status == "ok" else status
        if not isinstance(found, list):
            return [], "format_error"
        return found, status

    def _ask_choice(self, window: Window, system: str, user: str, labels: Sequence[str],
                    purpose: str) -> dict[str, float] | None:
        """问一个字母：→ 每个选项的把握；回答的不是选项的 None。连不上、出错的抛出去；过了截止时间的抛 _Late。"""
        self._check_time()
        answer, asked = self.model.chat(system, user, max_tokens=1, logprobs=True)
        if not isinstance(answer, Mapping):
            self._record(window, purpose, "format_error", {}, asked)
            return None
        used = answer.get("usage") or {}
        self._record(window, purpose, "ok", used if isinstance(used, Mapping) else {}, asked)
        return choice_probs(answer, (*labels, NONE_LABEL))

    def _check_time(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise _Late

    def _chunks(self, window: Window) -> list[list[str]]:
        refs = [line.ref for line in window.targets]
        return [refs[at : at + CHUNK_LINES] for at in range(0, len(refs), CHUNK_LINES)]

    def _source(self, window: Window, rendered: RenderedWindow, ref: str, text: str) -> dict[str, object]:
        """学来的一项、一个“也叫”记着的出处：可见范围、包和版本、窗口、哪一句（说法那一句），sources 是 text 这段字在这个
        窗口里出现过的每一句（名字可能是照别的句子写的：撤回哪一句，这个名字都跟着删）。"""
        line = window.line(ref)
        lines = [one for one in window.targets if one.ref == ref or (text and text in rendered.shown.get(one.ref, ""))]
        sources = sorted({(one.packet_id, one.revision, one.sentence_id) for one in lines})
        return {
            "version": PRE_B_VERSION,
            "partitions": json.dumps(list(window.scope.partitions), ensure_ascii=False),
            "sensitive": json.dumps(list(window.scope.sensitive), ensure_ascii=False),
            "packet_id": line.packet_id if line is not None else "",
            "revision": line.revision if line is not None else 0,
            "window_id": window.window_id,
            "window_sha": rendered.sha256,
            "ref": ref,
            "sentence_id": line.sentence_id if line is not None else "",
            "sources": json.dumps([list(one) for one in sources], ensure_ascii=False),
            "created_at": _now(),
        }

    @staticmethod
    def _contents(window: Window) -> dict[str, str]:
        """存进标注、收名字记号的这个窗口有哪些包和句子（撤回时找它们）。"""
        return {
            "context": window_context(window),
            "packets": json.dumps([list(one) for one in window.packets], ensure_ascii=False),
            "sentences": json.dumps(sorted({line.sentence_id for line in window.lines}), ensure_ascii=False),
        }

    def _blocked_row(self, window: Window, rendered: RenderedWindow, ref: str, mention: str, item: Item | str, kind: str,
                     mine: Sequence[str], theirs: Sequence[str], stage: str,
                     others: Sequence[Item] = ()) -> dict[str, object]:
        """拦下的一行。depends：名单上那一项（和 others，“几项只差日子编号金额”的那几项）用到的学来的东西，撤回时连着删。"""
        depends = sorted({label for one in (item, *others) if isinstance(one, Item) for label in one.depends})
        return {"window_id": window.window_id, "window_sha": rendered.sha256, "version": PRE_B_VERSION,
                "context": window_context(window), "ref": ref, "mention": mention,
                "item": item.label if isinstance(item, Item) else item, "kind": kind,
                "mention_values": json.dumps(list(mine), ensure_ascii=False),
                "item_values": json.dumps(list(theirs), ensure_ascii=False),
                "depends": json.dumps(depends, ensure_ascii=False), "stage": stage, "created_at": _now()}

    # ---------- 1. 收名字 ----------

    def _learn_one(self, window: Window, rendered: RenderedWindow, view: Roster) -> _Learning | None:
        """问本机模型这个窗口里名单上还没有的名字，代码核过，再一个一个定：记成名单上哪一项的“也叫”、另起一项，还是不收。
        还不写进名单（_commit_learning 按窗口的先后写）。出错、太长的 None。"""
        head = view.text() + "\n\n【窗口】\n" + rendered.text.rstrip("\n")
        if len(self.learn_system) + len(head) + len(LEARN_TAIL) > MAX_PROMPT_CHARS:
            self._count(太长没收的窗口=1)
            self._say(f"[B] {window.window_id}：太长，不收名字")
            return None
        started = time.monotonic()
        learning = _Learning()
        entries: list[object] = []
        statuses = []
        for chunk in self._chunks(window):
            try:
                found, status = self._ask_json(window, self.learn_system, head + LEARN_TAIL.format(first=chunk[0], last=chunk[-1]),
                                               LEARN_SCHEMA, "新名字", "pre_b_learn")
            except _Late:
                self._count(到时间没收的窗口=1)
                return None
            except _LOCAL_ERRORS as error:
                self._record(window, "pre_b_learn", f"error: {type(error).__name__}", {}, time.monotonic() - started)
                self._count(收名字出错的窗口=1)
                self._say(f"[B] {window.window_id}：收名字时本机模型出错（{type(error).__name__}）")
                return None
            entries.extend(found)
            statuses.append(status)
        learning.found = len(entries)
        learning.status = "ok" if all(one == "ok" for one in statuses) else ",".join(statuses)
        seen: set[str] = set()
        try:
            for raw in entries:
                checked, why = self._check_new(view, window, rendered, raw)
                if checked is None:
                    entry = raw if isinstance(raw, Mapping) else {}
                    ref = str(entry.get("行") or "")
                    learning.dropped.append({"行": _own_ref(rendered, ref), "说法": _own(rendered, ref, entry.get("说法")),
                                             "为什么不要": why})
                    continue
                if view.key(str(checked["name"])) in seen:
                    continue
                seen.add(view.key(str(checked["name"])))
                learning.decisions.append(self._place(view, window, rendered, head, checked))
        except EmbeddingError as error:  # 向量服务出错：这个窗口不收，下回再收（不悄悄换成别的挑法）
            self._count(向量服务出错的窗口=1)
            self._say(f"[B] {window.window_id}：收名字时向量服务出错（{error.kind}）")
            return None
        except _Late:
            self._count(到时间没收的窗口=1)
            return None
        learning.seconds = time.monotonic() - started
        return learning

    def _check_new(self, view: Roster, window: Window, rendered: RenderedWindow,
                   raw: object) -> tuple[dict[str, object] | None, str]:
        """代码核一条收来的名字。说法逐字在那一行原文里；名字逐字在这个窗口的 T 行里（不在的拿说法当名字）；不收代称、
        泛称、我和主人、邮箱账号、Bot、说的是几样的；光是股票代号的写法当股票；单子要看得出是单子。"""
        entry = raw if isinstance(raw, Mapping) else {}
        ref = str(entry.get("行") or "").strip()
        mention = _clean(entry.get("说法") or "")
        name = _clean(entry.get("名字") or "")
        cls = str(entry.get("类") or "").strip()
        line = window.line(ref)
        shown = rendered.shown.get(ref, "")
        if line is None or not line.target:
            return None, "不是 T 行"
        if not mention or mention not in shown:
            return None, "说法不在那一行原文里"
        if not name or not any(name in rendered.shown.get(one.ref, "") for one in window.targets):
            name = mention
        if cls not in _CLASSES:
            return None, "类不对"
        for text in dict.fromkeys((mention, name)):
            if looks_like_account(text):
                return None, "邮箱、账号"
            if view.vague_word(text):
                return None, "泛称"
        if view.pointer_word(name) or POINTER.fullmatch(name):
            return None, "代称"
        if len(view.key(name)) < 2:
            return None, "太短"
        if any(class_of(item.kind) == BOT and (view.listed(name, item) or view.listed(mention, item)) for item in view.fixed):
            return None, "Bot"
        if bare_ticker(name) and cls == DEAL:
            cls = STOCK  # 光是股票代号的写法是股票，不是单子（b3）
        deals = view.deal_finder().phrases(shown)
        span = occurrences(shown, mention)[0]
        if (plural_at(shown, *span) or any(phrase.plural and overlaps(span, phrase) for phrase in deals)
                or any(self._several(view, text, cls) for text in dict.fromkeys((mention, name)))):
            return None, "说的是几样"
        if cls == DEAL and not (view.deal_finder().phrases(name) or view.deal_finder().phrases(mention)):
            return None, "看不出是单子"
        return {"ref": ref, "mention": mention, "name": name, "class": cls, "kind": KIND_OF_CLASS[cls]}, ""

    @staticmethod
    def _several(view: Roster, text: str, cls: str) -> bool:
        """收来的一个“名字”其实是几样东西：用“/”“、”“，”这类隔开的（“JKJ/YSJ/BNY”“甲科技、乙数据”），或者名单上两项以上的
        名字连写在一起的（“甲科技乙数据”）。单子不看这两样：篮子单子本来就是几只股票拼的（“JKJ+YSJ FCN”“JKJ/YSJ FCN”是一张），
        几张的照 DealFinder 认（“JKJ/YSJ 累购”是两张）。"""
        if cls == DEAL:
            return False
        if len([piece for piece in _LIST_SPLIT.split(text) if piece.strip()]) >= 2:
            return True
        folded = text.casefold()
        hits = set()
        for at, item in enumerate(view.items()):
            for one in (item.name, *item.aliases):
                one = one.strip().casefold()
                if len(one) >= 4 or (len(one) >= 2 and not one.isascii()):
                    found = one in folded
                elif len(one) == 3:
                    found = bool(re.search(rf"(?<![a-z0-9]){re.escape(one)}(?![a-z0-9])", folded))
                else:
                    found = False
                if found:
                    hits.add(at)
                    break
        return len(hits) >= 2

    def _place(self, view: Roster, window: Window, rendered: RenderedWindow, head: str,
               checked: dict[str, object]) -> dict[str, object]:
        """一条收来的名字记成谁：名单上原样有的 → 那一项的“也叫”（只对得上一项、又是一类的）；不然在同一类最像的几项里
        问本机模型。→ checked 加上 decision（alias、new、skip）、target（Item）、why。"""
        ref, mention, name, cls = (str(checked[key]) for key in ("ref", "mention", "name", "class"))
        hits = {id(item): item for item in view.items() if view.listed(name, item) or view.listed(mention, item)}
        if hits:
            found = list(hits.values())
            if len(found) == 1 and class_of(found[0].kind) == cls:
                return {**checked, "decision": "alias", "target": found[0], "why": "名单上原样有"}
            return {**checked, "decision": "skip", "why": "名单上已经有"}
        around, own = values_near(rendered.shown.get(ref, ""), mention), values_of(name)
        near = {kind: around.get(kind, frozenset()) | own.get(kind, frozenset()) for kind in own}
        same = view.of_class(cls)
        pool = [item for item in same if not clash(near, view.values(item))]
        checked = {**checked, "values_out": len(same) - len(pool)}
        if not pool:
            return {**checked, "decision": "new", "why": "同一类里没有像的"}
        options = pool
        if len(pool) > TOP_K:
            scores = self.picker.rank(name, [item.doc() for item in pool], window.window_id)  # 出错的抛出去，整个窗口不收
            if scores is not None:
                options = [pool[at] for at in sorted(range(len(pool)), key=lambda at: -scores[at])]
            options = options[:TOP_K]
        labels = LABELS[: len(options)]
        lines = "\n".join(f"{label}. {item.option()}" for label, item in zip(labels, options))
        question = LEARN_CHOOSE_TAIL.format(ref=ref, mention=mention, name=name, cls=cls, options=lines)
        checked["options"] = [item.label for item in options]
        try:
            probs = self._ask_choice(window, self.learn_system, head + question, labels, "pre_b_learn_choose")
        except _LOCAL_ERRORS as error:
            self._record(window, "pre_b_learn_choose", f"error: {type(error).__name__}", {}, 0.0)
            return {**checked, "decision": "skip", "why": "本机模型出错"}
        if not probs:
            return {**checked, "decision": "skip", "why": "回答的不是选项"}
        best = max(probs, key=lambda label: probs[label])
        checked["p"] = round(probs[best], 4)
        if not probs[best] >= KEEP_AT:  # 这么写，不是数的（NaN）也过不了
            return {**checked, "decision": "skip", "why": "没把握"}
        if best == NONE_LABEL:
            return {**checked, "decision": "new", "why": "名单上没有"}
        return {**checked, "decision": "alias", "target": options[labels.index(best)], "why": "选的"}

    def _fold(self, view: Roster, window: Window, rendered: RenderedWindow, ref: str, target: Item, alias: str,
              aliases: list[dict[str, object]], blocked: list[dict[str, object]], stage: str) -> str:
        """这个写法记成 target 的“也叫”：先过别的类的叫法、防合错 b、c。→ 记了 "ok"；为什么没记。"""
        if not alias or view.pointer_word(alias) or view.vague_word(alias) or looks_like_account(alias):
            return "不是名字"
        if view.listed(alias, target) or any(row["alias"] == alias and row.get("target") == target.target for row in aliases):
            return "已经有"
        if len(target.aliases) >= MAX_ALIASES:
            return "也叫太多"
        if view.crosses_class(alias, target):
            self._count(别的类的叫法没记=1)
            return "别的类的叫法"
        clashes = clash(values_of(alias), view.values(target))
        if clashes:  # 防合错 b：带着的日子、编号、金额和那一项的对不上
            for what, mine, theirs in clashes:
                blocked.append(self._blocked_row(window, rendered, ref, alias, target, what, mine, theirs, stage))
            return "日子、编号、金额对不上"
        looks = view.ambiguous(alias)
        if len(looks) > 1:  # 防合错 c：不带能分开几项的日子、编号，不记成其中一项的“也叫”
            blocked.append(self._blocked_row(window, rendered, ref, alias, target, "几项只差日子、编号、金额", [],
                                             [item.label for item in looks], stage, looks))
            return "几项只差日子、编号、金额"
        aliases.append({"target": target.target, "alias": alias, **self._source(window, rendered, ref, alias)})
        return "ok"

    def _commit_learning(self, rosters: Rosters, store: ResultStore, window: Window, rendered: RenderedWindow,
                         learning: _Learning) -> None:
        """一个窗口收来的名字写进名单（一个事务），连同“收过名字”的记号。看的是现在的名单：同一个包前面的窗口刚记进去的也算。"""
        view = rosters.view(window.scope)
        items: list[dict[str, object]] = []
        aliases: list[dict[str, object]] = []
        blocked: list[dict[str, object]] = []
        new_keys: dict[str, int] = {}
        outcome: Counter[str] = Counter()
        kept: list[dict[str, object]] = []
        for decision in learning.decisions:
            ref, mention, name = str(decision["ref"]), str(decision["mention"]), str(decision["name"])
            what = str(decision["decision"])
            target = decision.get("target")
            if isinstance(target, Item):
                target = view.by_label(target.label)  # 看现在的名单（同一个包前面的窗口可能改过它的“也叫”）
            if what == "new":
                existing = view.only_item(name)
                if existing is not None and class_of(existing.kind) != class_of(str(decision["kind"])):
                    outcome["名单上有别的类的同名项"] += 1  # 同一个包前面的窗口刚记了别的类的同名项：不并进去
                    kept.append({"ref": ref, "mention": mention, "decision": "skip", "why": "名单上有别的类的同名项"})
                    continue
                if existing is not None or view.key(name) in new_keys:
                    what, target = ("alias", existing) if existing is not None else ("alias-new", None)
            if what == "new":
                looks = view.ambiguous(name)
                if looks:  # 防合错 c：不另起一个盖住几项的名字（“X3 累购”盖住 9/03、9/07 两张）
                    blocked.append(self._blocked_row(window, rendered, ref, name, "", "几项只差日子、编号、金额", [],
                                                     [item.label for item in looks], "收名字（不另起）", looks))
                    outcome["不另起"] += 1
                    continue
                new_keys[view.key(name)] = len(items)
                items.append({"name": name, "kind": decision["kind"], **self._source(window, rendered, ref, name)})
                outcome["新的"] += 1
                self._new_alias(view, window, rendered, ref, len(items) - 1, name, mention, str(decision["kind"]),
                                aliases, blocked)
                kept.append({"ref": ref, "mention": mention, "decision": "new", "why": decision.get("why")})
                continue
            if what == "alias-new":
                index = new_keys[view.key(name)]
                if not any(row.get("new_item") == index and row["alias"] == mention for row in aliases):
                    self._new_alias(view, window, rendered, ref, index, name, mention, str(decision["kind"]), aliases, blocked)
                continue
            if what != "alias" or not isinstance(target, Item):
                outcome["不收"] += 1
                kept.append({"ref": ref, "mention": mention, "decision": "skip", "why": decision.get("why"),
                             "p": decision.get("p")})
                continue
            results = []
            for alias in dict.fromkeys((mention, name)):
                if decision.get("why") == "名单上原样有" and view.listed(alias, target):
                    continue
                results.append(self._fold(view, window, rendered, ref, target, alias, aliases, blocked, "收名字"))
            stage = "也叫" if "ok" in results else "没记"
            outcome[stage] += 1
            kept.append({"ref": ref, "mention": mention, "decision": stage, "target": target.label, "why": decision.get("why"),
                         "p": decision.get("p")})
        detail = {"status": learning.status, "found": learning.found, "dropped": learning.dropped, "kept": kept}
        learned = {"window_id": window.window_id, "window_sha": rendered.sha256, "version": PRE_B_VERSION,
                   **self._contents(window), "detail": json.dumps(detail, ensure_ascii=False),
                   "seconds": round(learning.seconds, 3), "created_at": _now()}
        store.save_preb_learning(items=items, aliases=aliases, blocked=blocked, learned=learned)
        rosters.reload()
        self._count(收过名字的窗口=1, 收来的=learning.found, 新的项=len(items), 学来的也叫=len(aliases),
                    防合错拦下的=len(blocked))
        self._say(f"[B] {window.window_id}：收名字 {learning.found} 条，新的 {len(items)} 项，也叫 {len(aliases)} 个，"
                  f"{learning.seconds:.0f} 秒")

    def _new_alias(self, view: Roster, window: Window, rendered: RenderedWindow, ref: str, index: int, name: str,
                   mention: str, kind: str, aliases: list[dict[str, object]], blocked: list[dict[str, object]]) -> None:
        """另起的一项的说法写法和名字不一样的，记成它的“也叫”。先过和 _fold 一样的几条：代称、泛称、邮箱账号不记；日子编号
        金额和名字对不上的不记；名单上已经是别的哪一项的叫法的不记；别的类的叫法不记（另起的单子也不带光是股票代号的写法）；
        防合错 c：几项只差日子编号金额、说法分不出是哪一项的不记。"""
        if (mention == name or view.pointer_word(mention) or view.vague_word(mention) or looks_like_account(mention)
                or clash(values_of(mention), values_of(name)) or view.only_item(mention) is not None):
            return
        if view.crosses_class(mention, Item(name, kind)):
            self._count(别的类的叫法没记=1)
            return
        looks = view.ambiguous(mention, extra=[Item(name, kind)])  # 新的这一项也算：记进去以后它就是双胞胎里的一张
        if len(looks) > 1:
            blocked.append(self._blocked_row(window, rendered, ref, mention, "", "几项只差日子、编号、金额", [],
                                             [item.label for item in looks], "收名字", looks))
            return
        aliases.append({"new_item": index, "alias": mention, **self._source(window, rendered, ref, mention)})

    # ---------- 2. 标〔指〕（b3） ----------

    def _mark_one(self, window: Window, rendered: RenderedWindow, view: Roster, store: ResultStore) -> list[dict[str, object]] | None:
        """标一个窗口、存进结果库。→ 标上的几条（_learn_aliases 用）；没标成的（出错、太长）None。"""
        head = view.text() + "\n\n【窗口】\n" + rendered.text.rstrip("\n")
        if len(self.system) + len(head) + len(PROPOSE_TAIL) > MAX_PROMPT_CHARS:
            self._count(太长没标的窗口=1)
            self._say(f"[B] {window.window_id}：太长，不标")
            return None
        started = time.monotonic()
        proposed: list[object] = []
        statuses: list[str] = []
        for chunk in self._chunks(window):  # 整个窗口都给它看（前面一样，本机模型的缓存接着用），一次只问一段
            try:
                found, status = self._ask_json(window, self.system, head + PROPOSE_TAIL.format(first=chunk[0], last=chunk[-1]),
                                               PROPOSE_SCHEMA, "标注", "pre_b_propose")
            except _Late:
                self._count(到时间没标的窗口=1)
                return None
            except _LOCAL_ERRORS as error:
                self._record(window, "pre_b_propose", f"error: {type(error).__name__}", {}, time.monotonic() - started)
                self._count(没标成的窗口=1)
                self._say(f"[B] {window.window_id}：本机模型出错（{type(error).__name__}），不标")
                return None
            proposed.extend(found)
            statuses.append(status)
        status = "ok" if all(one == "ok" for one in statuses) else ",".join(statuses)
        blocked: list[dict[str, object]] = []
        candidates, dropped = self.validate(view, window, rendered, proposed, blocked)
        kept = []
        try:
            for candidate in candidates:
                if candidate.get("auto"):  # b2：原话里原样写着名单上的叫法、只对得上这一项，不用再问
                    candidate["p"] = None
                    kept.append(candidate)
                    continue
                if self._choose(view, window, rendered, head, candidate) is not None:  # b3：在同一类最像的几项里选
                    kept.append(candidate)
        except EmbeddingError as error:  # 向量服务出错：这个窗口不标、不存，照原样交给 L3（不悄悄换成别的挑法）
            self._count(向量服务出错的窗口=1)
            self._say(f"[B] {window.window_id}：向量服务出错（{error.kind}），不标")
            return None
        except _Late:
            self._count(到时间没标的窗口=1)
            return None
        notes: dict[str, list[list[str]]] = {}
        for candidate in kept:
            notes.setdefault(str(candidate["ref"]), []).append([str(candidate["mention"]), str(candidate["name"])])
        noted = apply_notes(rendered, notes)
        seconds = time.monotonic() - started
        detail = {
            "status": status,
            "proposed": len(proposed),
            "candidates": [
                {key: (value.label if isinstance(value, Item) else value) for key, value in one.items()
                 if key in ("ref", "mention", "origin", "class", "auto", "p", "why", "chosen", "options", "values_out")}
                for one in candidates
            ],
            "dropped": dropped,
        }
        item_ids = {int(one["item"].item_id) for one in kept if isinstance(one.get("item"), Item) and one["item"].learned}
        store.save_preb_notes(
            {"window_id": window.window_id, "window_sha": rendered.sha256, "version": PRE_B_VERSION, "noted_sha": noted.sha256,
             **self._contents(window), "notes": json.dumps(notes, ensure_ascii=False),
             "detail": json.dumps(detail, ensure_ascii=False), "seconds": round(seconds, 3), "created_at": _now()},
            item_ids,
            blocked,
        )
        self._count(标过的窗口=1, 提的=len(proposed), 核过的=len(candidates), 标上的=len(kept), 防合错拦下的=len(blocked),
                    代码找的单子=sum(1 for one in candidates if one.get("origin") == "代码找的单子"),
                    选了别的项的=sum(1 for one in kept if one.get("proposed") and one["proposed"] != one["name"]),
                    单子里的股票没标=sum(1 for one in dropped if one["为什么不要"] == "单子里的股票"))
        self._say(f"[B] {window.window_id}：提了 {len(proposed)} 条，代码核过 {len(candidates)} 条，标上 {len(kept)} 条，"
                  f"{seconds:.0f} 秒")
        return kept

    def _context(self, window: Window, rendered: RenderedWindow, ref: str) -> str:
        """“那张”“它”这种说法本身没字可比：拿这一行和前面两行 T 去比。"""
        refs = [line.ref for line in window.targets]
        at = refs.index(ref) if ref in refs else len(refs)
        picked = refs[max(0, at - 2) : at + 1] or [ref]
        return " ".join(rendered.shown.get(one, "") for one in picked)[-400:]

    def pick(self, view: Roster, window: Window, rendered: RenderedWindow,
             candidate: Mapping[str, object]) -> tuple[list[Item], int]:
        """挑候选：名单上和说法同一类的；日子、编号、金额和说法明摆着对不上的不要（防合错 a）。多于 TOP_K 项的，用向量
        挑最像的 TOP_K 项；Qwen 提的那一项、和它只差日子编号金额的几项一定放进去。按像的先后排（没有向量的按名单的先后）。
        → (挑出来的几项, 日子编号金额对不上、没放进去的有几项)。"""
        cls = candidate.get("class")
        if not cls:
            return [], 0
        ref, mention = str(candidate["ref"]), str(candidate["mention"])
        near = values_near(rendered.shown.get(ref, ""), mention)
        same = view.of_class(str(cls))
        pool = [item for item in same if not clash(near, view.values(item))]
        removed = len(same) - len(pool)
        proposed = view.resolve(str(candidate["proposed"])) if candidate.get("proposed") else None
        must: list[Item] = []
        if proposed is not None and proposed in pool:
            must.append(proposed)
            must.extend(one for one in view.lookalikes(proposed.name) if one in pool and one not in must)
        if len(pool) <= TOP_K:
            return pool, removed
        bare = POINTER.fullmatch(mention.strip()) or view.pointer_word(mention)
        query = self._context(window, rendered, ref) if bare else mention
        scores = self.picker.rank(query, [item.doc() for item in pool], window.window_id)  # 服务出错的抛出去，整个窗口不标
        if scores is None:
            ranked = [*must, *(item for item in pool if item not in must)]  # 没有向量服务（测试）：Qwen 提的、和它像的在前
        else:
            ranked = [pool[at] for at in sorted(range(len(pool)), key=lambda at: -scores[at])]
        top = ranked[:TOP_K]
        for item in must[:TOP_K]:
            if item not in top:  # 换掉排得最靠后的、不是一定要放的那一项
                for at in range(len(top) - 1, -1, -1):
                    if top[at] not in must:
                        top[at] = item
                        break
        order = {id(item): at for at, item in enumerate(ranked)}
        return sorted(top, key=lambda item: order[id(item)]), removed

    def _choose(self, view: Roster, window: Window, rendered: RenderedWindow, head: str,
                candidate: dict[str, object]) -> Item | None:
        """问 Qwen：这个说法是挑出来的几项里的哪一项，还是都不是。最高的那一项到 KEEP_AT、那一行又没写着它的名字，
        candidate 的 name 换成它，返回它；不然 None（candidate 里记着为什么）。"""
        ref, mention = str(candidate["ref"]), str(candidate["mention"])
        options, removed = self.pick(view, window, rendered, candidate)
        candidate["options"] = [item.label for item in options]
        candidate["values_out"] = removed  # 同一类里日子、编号、金额和说法对不上、没放进候选的几项（防合错 a）
        candidate["p"] = None
        if not options:
            candidate["why"] = "没有同类的候选"
            return None
        labels = LABELS[: len(options)]
        lines = "\n".join(f"{label}. {item.option()}" for label, item in zip(labels, options))
        question = CHOOSE_TAIL.format(ref=ref, mention=_clean(mention), cls=candidate.get("class"), options=lines)
        try:
            probs = self._ask_choice(window, self.system, head + question, labels, "pre_b_choose")
        except _LOCAL_ERRORS as error:
            self._record(window, "pre_b_choose", f"error: {type(error).__name__}", {}, 0.0)
            candidate["why"] = "本机模型出错"
            return None
        if not probs:
            candidate["why"] = "回答的不是选项"
            return None
        best = max(probs, key=lambda label: probs[label])
        candidate["p"] = round(probs[best], 4)
        if best == NONE_LABEL:
            candidate["why"] = "都不是"
            return None
        item = options[labels.index(best)]
        candidate["chosen"] = item.label
        if not probs[best] >= KEEP_AT:  # 这么写，不是数的（NaN）也过不了
            candidate["why"] = "没把握"
            return None
        if item.name in rendered.shown.get(ref, ""):
            candidate["why"] = "那一行已经写着这个名字"
            return None
        candidate["name"] = item.name
        candidate["item"] = item
        return item

    def validate(
        self,
        view: Roster,
        window: Window,
        rendered: RenderedWindow,
        proposed: Sequence[object],
        blocked: list[dict[str, object]] | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """代码核一遍模型提的：说法逐字在那一行（T 行）原文里；名字是名单上的（写成别名的换成名字）；
        那一行没原样写着这个名字；不是泛称；同一行同一个说法一条；一行最多几条、一个窗口最多几条。

        b2：说法原样是名单上的叫法（“也叫”里的）也放过去；只对得上这一项的标 auto，不用再问本机模型。
        b3：先认说法是哪一类（class），后面只在这一类里挑候选、选。股票的叫法在单子的说法里头的（“Jiake + Yishu”里的
        Jiake）不单独标，Qwen 提成了什么都一样；说的是几张单子的不标；单子的说法一行里有几处、说的又不是同一张的不标
        （分不清〔指〕说的是哪一处）。Qwen 没提、也没被 Qwen 提的单子说法盖住的单子说法，代码补上（origin 是“代码找的单子”，
        名单上原样有、只对得上一张的直接标，别的拿去选；同一个写法一行里有两处的不补）。防合错拦下的记进 blocked。"""
        blocked = blocked if blocked is not None else []
        lines = {line.ref: line for line in window.lines}
        kept: list[dict[str, object]] = []
        dropped: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        per_line: Counter[str] = Counter()
        finder = view.deal_finder()
        found: dict[str, list[DealPhrase]] = {}

        def deals(ref: str) -> list[DealPhrase]:
            if ref not in found:
                found[ref] = finder.phrases(rendered.shown.get(ref, ""))
            return found[ref]

        for raw in proposed:
            entry = raw if isinstance(raw, Mapping) else {}
            ref = str(entry.get("行") or "").strip()
            mention = str(entry.get("说法") or "").strip()
            name = str(entry.get("名字") or "").strip()
            line = lines.get(ref)
            shown = rendered.shown.get(ref, "")
            item = view.resolve(name) if name else None
            spans = occurrences(shown, mention)
            reason = ""
            cls: str | None = None
            if line is None or not line.target:
                reason = "不是 T 行"
            elif not mention or mention not in shown:
                reason = "说法不在那一行原文里"
            elif looks_like_account(mention):  # b2.1：邮件地址、账号不标（别名表不动，账本里照旧靠别名表合到人上）
                reason = "邮箱、账号"
            elif item is None:
                reason = "名字不在名单上"
            if not reason:
                assert item is not None
                cls = view.mention_class(mention, item)
                if class_of(item.kind) != cls:  # b3：Qwen 提的那一项不是这一类（光“YSJ”提成了 YSJ 累购）
                    alt = view.only_item(mention)
                    if alt is not None and class_of(alt.kind) == cls:
                        item = alt  # 说法原样是这一类里一项的叫法：就是那一项（股票 YSJ）
            if reason:
                pass
            elif item.name in shown:
                reason = "那一行已经写着这个名字"
            elif view.vague_word(mention):
                reason = "泛称"
            else:
                hit = {phrase.text for phrase in deals(ref) if any(overlaps(span, phrase) for span in spans)}
                if (cls == STOCK or class_of(item.kind) == STOCK) and cls != DEAL and all(
                    any(inside(span, phrase) for phrase in deals(ref)) for span in spans
                ):
                    reason = "单子里的股票"  # b3：股票的叫法在单子的说法里头，不单独标（单子的说法整个拿去对）
                elif cls == DEAL and (plural_at(shown, *spans[0])
                                      or any(phrase.plural and overlaps(spans[0], phrase) for phrase in deals(ref))
                                      or sum(spans[0][0] <= one.start and one.end <= spans[0][1] for one in deals(ref)) >= 2):
                    reason = "说的是几张单子"  # “两张 FCN”“那两张”；一个说法里有两个单子说法（“X+Y / X+Z”），代码分开拿去对
                elif cls == DEAL and len(spans) > 1 and len(hit) >= 2:
                    reason = "一行里几处，分不清是哪一张"  # “X1 那张……JKJ+BNY 那张”：光“那张”不标，代码找的两个照样对
                elif (ref, mention) in seen:
                    reason = "重复"
                elif per_line[ref] >= MAX_PER_LINE or len(kept) >= MAX_PER_WINDOW:
                    reason = "太多"
            near: Values = {}
            if not reason:  # 防合错 a：说法（连同紧挨着的字）带着的日子、编号、金额和那一项的明摆着对不上
                assert item is not None
                near = values_near(shown, mention)
                clashes = clash(near, view.values(item))
                if clashes:
                    reason = "日子、编号、金额对不上"
                    for what, mine, theirs in clashes:
                        blocked.append(self._blocked_row(window, rendered, ref, mention, item, what, mine, theirs, "标注"))
            if reason:
                dropped.append({"行": _own_ref(rendered, ref), "说法": _own(rendered, ref, mention), "为什么不要": reason})
                continue
            assert item is not None
            seen.add((ref, mention))
            per_line[ref] += 1
            same = class_of(item.kind) == cls  # Qwen 提的那一项和说法不是一类的（单子的说法提成了股票）：那一项不算数
            auto = same and view.only_item(mention) is item
            if auto:  # 防合错 c：几项只差日子、编号、金额，说法分不出是哪一项的，不直接标，照旧问
                looks = view.ambiguous(mention, near)
                if looks:
                    auto = False
                    blocked.append(self._blocked_row(window, rendered, ref, mention, item, "几项只差日子、编号、金额", [],
                                                     [one.label for one in looks], "标注（不直接标，问本机模型）", looks))
            kept.append({"ref": ref, "mention": mention, "name": item.name if same else None,
                         "proposed": item.name if same else None, "item": item if auto else None, "auto": auto,
                         "class": cls, "origin": "Qwen 提的"})
        for line in window.targets:  # b3：Qwen 没提的单子说法，代码补上
            ref = line.ref
            shown = rendered.shown.get(ref, "")
            for phrase in deals(ref):
                if phrase.plural or (ref, phrase.text) in seen:
                    continue
                if len(occurrences(shown, phrase.text)) > 1:
                    continue  # 同一个写法一行里两处：〔指〕分不出说的是哪一处，日子金额也不知道看哪一处的
                if any(one["ref"] == ref and one["class"] == DEAL
                       and any(overlaps(at, phrase) for at in occurrences(shown, str(one["mention"]))) for one in kept):
                    continue  # Qwen 提的单子说法已经盖住了
                if looks_like_account(phrase.text) or view.vague_word(phrase.text):
                    continue
                if per_line[ref] >= MAX_PER_LINE or len(kept) >= MAX_PER_WINDOW:
                    dropped.append({"行": ref, "说法": phrase.text, "为什么不要": "太多"})
                    continue
                item = view.only_item(phrase.text)
                if item is not None and class_of(item.kind) != DEAL:
                    item = None
                if item is not None and item.name in shown:
                    continue  # 那一行已经写着这个名字
                near = values_near(shown, phrase.text)
                auto = item is not None and not clash(near, view.values(item)) and not view.ambiguous(phrase.text, near)
                seen.add((ref, phrase.text))
                per_line[ref] += 1
                kept.append({"ref": ref, "mention": phrase.text, "name": item.name if auto and item else None,
                             "proposed": None, "item": item if auto else None, "auto": auto, "class": DEAL,
                             "origin": "代码找的单子"})
        return kept, dropped

    # ---------- 3. 标上的说法记成“也叫” ----------

    def _learn_aliases(self, rosters: Rosters, store: ResultStore, window: Window, rendered: RenderedWindow,
                       kept: Sequence[Mapping[str, object]]) -> None:
        """标上了的说法记成那一项的“也叫”：后面的包原样碰到就直接标（b2 从 L3 结果里学的“原文的写法记成也叫”）。
        代称不记，防合错 b、c、别的类的照 b3。"""
        view = rosters.view(window.scope)
        aliases: list[dict[str, object]] = []
        blocked: list[dict[str, object]] = []
        for one in kept:
            item = one.get("item")
            target = view.by_label(item.label) if isinstance(item, Item) else view.resolve(str(one.get("name") or ""))
            if target is None:
                continue
            self._fold(view, window, rendered, str(one["ref"]), target, str(one["mention"]), aliases, blocked, "标上的说法")
        if aliases or blocked:
            store.save_preb_learning(aliases=aliases, blocked=blocked)
            rosters.reload()
            self._count(学来的也叫=len(aliases), 防合错拦下的=len(blocked))
