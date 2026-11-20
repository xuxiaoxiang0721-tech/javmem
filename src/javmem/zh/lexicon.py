"""别名表和词典（J2.14、J3.6）：仓库外的配置，环境变量 JAVMEM_LEXICON_FILE 指过去。真名只写在那里，代码和测试里没有。

两种格式。**文件头没写 format 的是第一版（J2.14），意思不变：**

    aliases:                      # 同一个东西的几种叫法，类型是 L3 的六种实体类型之一
      - {type: 公司或机构, names: [甲, 甲银行, Jia Bank]}
      - {type: 人, names: [全名, 小名]}
    words: [人名, 公司名]          # 只进切词的词典

- **别名表只用来找：** L5 按名字对齐实体时，同一组的名字互相算数（名字找不到实体、别名也找不到时，按同组的别的名字找）；
  不写进账本的别名表，账本里的别名都要有原文的出处。
- **词典：** words 加上别名表里的名字，问话切词（`javmem.zh.segment`）时当一个词。查询接口照身份切（`words_for`）：只放
  名字规矩题 1 下这个身份看得到的配置里的名字（第二版里它算数的那些组的标准名、Bot 名单的标准名），别的写法要有看得到的
  出处，由查询那一边从账本的别名里加；words 照放，和看不到的名字写法一样的除外；第一版的组都不放。

**写着 `format: javmem-lexicon/2` 的是第二版（J3.6，D100）：**

    format: javmem-lexicon/2
    default_where: [investment, personal]   # 不写 where 的组在这几个分区的窗口里算数；这一项也不写，哪个分区都算
    aliases:
      - type: 人
        names: [标准名, 别的写法]            # 第一个是标准名
        where: [investment]                  # 可以不写：只在这些分区的窗口里算数
        from_types: [公司或机构]             # 可以不写：L3 写成这几种类型的，也对到这一组、改成这一组的类型
    bots:                                    # Bot，第七类实体（D13、D92）
      - names: [标准名, 别的写法]
        plain: [别的写法]                    # 也是普通词：只在 L3 标成 Bot、或者窗口里它就是说话人时才认
    not_entities:                            # 不建实体（D92）：照泛称办
      names: [名字]
      path_like: true                        # 像文件路径、带扩展名的文件名的也不建（组里、Bot 名单里写了的让路）
    apart:                                   # 不是同一个：合并工具不提，L5 也不按别名把它们连起来
      - {type: 人, names: [甲, 乙]}
    words: [人名]

- **组里的名字一律对到标准名算出来的编号**（L5，javmem.pipeline.write），实体的标准名就是组里第一个名字，和写的先后无关。
  名字是组里的一个，或者按斜杠、竖线、括号拆开以后每一段都是这一组的，才算这一组的。
- **同一类型里一个写法只能在一组**（from_types 也算那一类型）；Bot 名单、不建实体的名字不能在组里；“不是同一个”的两个
  名字不能在同一组。对不上的报配置错误，什么都不动，等人改。
- 别名表旁边的合并记录（合并工具写的，见 javmem.entities）也是别名表的一部分，读的时候接在人写的这份后面。
- 写错了报配置错误，消息里不带名字（只说第几组、第几条）。没配就是空的。
"""

from __future__ import annotations

import hashlib
import os
import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from javmem.config import ConfigError
from javmem.yamlio import load_yaml

ENTITY_TYPES = ("人", "公司或机构", "项目", "订单或合同", "地点", "资产")  # 和 L3 的一样（extract.ENTITY_TYPES）
BOT = "Bot"  # 第七类实体（D13）：L3 现在的提示词还给不出这一类，名单里的写法 L5 改成它
LEDGER_TYPES = (*ENTITY_TYPES, BOT)  # 账本里实体的类型
FORMAT_V2 = "javmem-lexicon/2"
_PARTITION = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
_PIECES = re.compile(r"\s*[/／|｜]\s*|[()（）]")  # 名字里拼在一起的几个叫法：斜杠、竖线、括号
_PARENS = re.compile(r"[(（][^()（）]*[)）]")
# 像文件路径、带扩展名的文件名：盘符、~/、./、/ 开头的，有反斜杠的，结尾是常见扩展名的
_PATH_LIKE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|\.{1,2}[\\/]|/)|\\"
    r"|[\w\-.]\.(?:md|txt|py|json|ya?ml|csv|tsv|xlsx?|docx?|pptx?|pdf|png|jpe?g|gif|html?|sql|db|log|zip|eml|mbox|toml|ini|cfg|"
    r"sh|ps1|bat|ipynb)$",
    re.IGNORECASE,
)


def _name_key(text: str) -> str:
    """L5 的比对写法（merge.name_key）。用到时才 import：别名表是中文规则这一层的，不在导入时拉上处理线。"""
    from javmem.pipeline.merge import name_key

    return name_key(text)


def looks_like_path(name: str) -> bool:
    return bool(_PATH_LIKE.search(name.strip()))


@dataclass(frozen=True)
class AliasGroup:
    """一组叫法。names[0] 是标准名；where 是哪几个分区的窗口里算数（None：哪个分区都算）。origin 是它从哪来的：
    人写的第几组，或者合并记录的编号，报错用。"""

    type: str
    names: tuple[str, ...]
    where: tuple[str, ...] | None = None
    from_types: tuple[str, ...] = ()
    origin: str = ""

    @property
    def standard(self) -> str:
        return self.names[0]

    @property
    def types(self) -> tuple[str, ...]:
        return (self.type, *self.from_types)


@dataclass(frozen=True)
class BotEntry:
    """一个 Bot。names[0] 是标准名；plain 里的写法也是普通词，只在 L3 标成 Bot、或者窗口里它是说话人时才认。"""

    names: tuple[str, ...]
    plain: tuple[str, ...] = ()

    @property
    def standard(self) -> str:
        return self.names[0]


@dataclass(frozen=True)
class Apart:
    """不是同一个：这几个名字说的是不同的东西。"""

    type: str
    names: tuple[str, ...]
    origin: str = ""


@dataclass(frozen=True)
class Lexicon:
    """aliases：(类型, 这一组的名字)，第一版的找法用；words：切词的词典（words 加上别名表里的名字）。
    第二版另有 groups、bots、not_entities、apart、default_where；version 是 1 或 2。"""

    aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    words: tuple[str, ...] = ()
    version: int = 1
    listed_words: tuple[str, ...] = ()  # 配置里 words 那一项写的（只进切词的词典，没有分区）
    groups: tuple[AliasGroup, ...] = ()
    bots: tuple[BotEntry, ...] = ()
    not_entities: tuple[str, ...] = ()
    path_like: bool = False
    apart: tuple[Apart, ...] = ()
    default_where: tuple[str, ...] | None = None
    records: tuple[dict[str, object], ...] = ()  # 合并记录，原样，按先后（撤销了的也在）
    _index: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict, compare=False, repr=False)
    _groups: dict[tuple[str, str], int] = field(default_factory=dict, compare=False, repr=False)
    _bots: dict[str, int] = field(default_factory=dict, compare=False, repr=False)
    _not: frozenset[str] = field(default_factory=frozenset, compare=False, repr=False)
    _named: frozenset[str] = field(default_factory=frozenset, compare=False, repr=False)  # 组里、Bot 名单里的写法
    _apart: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict, compare=False, repr=False)

    # ---------- 第一版的找法 ----------

    def same_as(self, key: str, entity_type: str, name_key: Callable[[str], str]) -> frozenset[str]:
        """和这个名字（比对写法）同组的别的名字的比对写法。name_key 是 L5 的比对写法。"""
        if not self.aliases:
            return frozenset()
        if not self._index:
            index: dict[tuple[str, str], set[str]] = {}
            for group_type, names in self.aliases:
                keys = {name_key(name) for name in names} - {""}
                for one in keys:
                    index.setdefault((group_type, one), set()).update(keys - {one})
            self._index.update({where: frozenset(names) for where, names in index.items()})
        return self._index.get((entity_type, key), frozenset())

    # ---------- 第二版 ----------

    def group_by_key(self, key: str, entity_type: str) -> AliasGroup | None:
        """这个写法（比对写法）在哪一组：组的类型或者 from_types 是这个类型。不看分区。"""
        number = self._groups.get((entity_type, key))
        return self.groups[number] if number is not None else None

    def group_of(self, name: str, entity_type: str, partitions: Collection[str]) -> AliasGroup | None:
        """L3 给的名字对到哪一组（第二版才有）：名字是组里的一个，或者按斜杠、竖线、括号拆开以后每一段都是这一组的；
        L3 给的类型是组的类型或者 from_types；窗口的分区都在这一组算数的分区里。"""
        if self.version < 2:
            return None
        found = self.group_by_key(_name_key(name), entity_type)
        if found is None:
            keys = [key for key in (_name_key(piece) for piece in _PIECES.split(name)) if key]
            if len(keys) >= 2:
                every = {self._groups.get((entity_type, key)) for key in keys}
                if len(every) == 1 and None not in every:
                    found = self.groups[every.pop()]  # type: ignore[index]
        if found is None:
            return None
        where = found.where if found.where is not None else self.default_where
        if where is not None and not set(partitions) <= set(where):
            return None
        return found

    def bot_of(self, name: str, mention: str, entity_type: str, speakers: Iterable[str]) -> BotEntry | None:
        """L3 给的名字是不是名单里的 Bot（第二版才有）。名单里的写法 L3 给什么类型都算；plain 的写法只在 L3 标成 Bot、
        或者窗口里这个 Bot 就是说话人时才算。名字对不上的，写法（mention）不是 plain 的也算。"""
        if self.version < 2 or not self.bots:
            return None
        speaker_keys = {_name_key(speaker) for speaker in speakers}
        for text, plain_ok in ((name, True), (mention, False)):
            key = _name_key(text) if text else ""
            number = self._bots.get(key) if key else None
            if number is None:
                continue
            bot = self.bots[number]
            if key in {_name_key(plain) for plain in bot.plain}:
                present = bool(speaker_keys & {_name_key(one) for one in bot.names})
                if not plain_ok or not (entity_type == BOT or present):
                    continue
            return bot
        return None

    def not_entity(self, name: str) -> bool:
        """不建实体的名字（第二版才有）：比对写法一样的，去掉括号里的说明以后一样的；path_like 的话，像文件路径、
        带扩展名的文件名的（组里、Bot 名单里写了的让路）。"""
        if self.version < 2:
            return False
        key = _name_key(name)
        if key in self._not or _name_key(_PARENS.sub(" ", name)) in self._not:
            return True
        if self.path_like and looks_like_path(name):
            return key not in self._named
        return False

    @property
    def known_keys(self) -> frozenset[str]:
        """组里、Bot 名单里的写法（比对写法）。"""
        return self._named

    def apart_from(self, key: str, entity_type: str) -> frozenset[str]:
        """和这个名字记成“不是同一个”的名字（比对写法）。"""
        return self._apart.get((entity_type, key), frozenset())

    # ---------- 查询接口：照身份 ----------

    def group_seen_by(self, group: AliasGroup, partitions: Collection[str] | None) -> bool:
        """这一组对看得到 partitions 这几个分区的身份算不算数（None 是全部）：组算数的分区（没写 where 的照 default_where，
        也没写是哪个分区都算）和它看得到的有交集。查询接口的名字（规矩题 1）、切词的词典都照这一条。"""
        where = group.where if group.where is not None else self.default_where
        return where is None or partitions is None or bool(set(where) & set(partitions))

    def words_for(self, partitions: Collection[str] | None) -> tuple[str, ...]:
        """看得到 partitions 这几个分区的身份（None 是全部）切问话用的词典里，别名表出的那一份（J5.5，J5.4 复审顺带看到的；
        审阅 R5、R6）。只放规矩题 1 下它看得到的、主人配置里写的名字：它算数的那些组（group_seen_by）的标准名、Bot 名单的
        标准名。组里、Bot 名单里别的写法和别的叫法一样，要有这个身份看得到的出处才算看得到（账本里的别名，查询那一边另加），
        这里不放：不然一个它看不到的名字在不在别名表里，切出来不一样、答的跟着变，就能试出别名表里有没有它。words 那一项没有
        分区，照放，和这些不放的名字比对写法一样的除外。第一版的组不算主人配置里写的名字（规矩题 1 只认第二版），都不放。"""
        shown: list[str] = []
        if self.version >= 2:
            shown += [group.standard for group in self.groups if self.group_seen_by(group, partitions)]
            shown += [bot.standard for bot in self.bots]
            named = [*(name for group in self.groups for name in group.names), *(name for bot in self.bots for name in bot.names)]
        else:
            named = [name for _, names in self.aliases for name in names]
        unseen = {_name_key(name) for name in named} - {_name_key(name) for name in shown}
        listed = [word for word in self.listed_words if _name_key(word) not in unseen]
        return tuple(dict.fromkeys([*listed, *shown]))

    @property
    def fingerprint(self) -> str:
        """L5 用到的那些（不算 words）的指纹：账本里记着写账时用的是哪一份，巡检比对。"""
        body = {
            "version": self.version,
            "aliases": [[group_type, list(names)] for group_type, names in self.aliases] if self.version < 2 else [],
            "groups": [[g.type, list(g.names), list(g.where) if g.where is not None else None, list(g.from_types)] for g in self.groups],
            "bots": [[list(b.names), list(b.plain)] for b in self.bots],
            "not_entities": list(self.not_entities),
            "path_like": self.path_like,
            "apart": [[a.type, list(a.names)] for a in self.apart],
            "default_where": list(self.default_where) if self.default_where is not None else None,
        }
        return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


EMPTY = Lexicon()


def _names(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(name, str) and name.strip() for name in value):
        raise ConfigError(f"别名表的配置里 {where} 要写成名字的列表，每个都不能是空的")
    return tuple(name.strip() for name in value)


def _partitions(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and _PARTITION.fullmatch(item) for item in value):
        raise ConfigError(f"别名表的配置里 {where} 要写成分区名的列表（小写英文、数字和连字符）")
    return tuple(sorted(set(value)))


def _types(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(item in ENTITY_TYPES for item in value):
        raise ConfigError(f"别名表的配置里 {where} 要写成类型的列表，每个是 {'、'.join(ENTITY_TYPES)} 之一")
    return tuple(dict.fromkeys(value))


def _dedupe(names: Sequence[str]) -> tuple[str, ...]:
    """同一组里比对写法一样的只留第一个（“X 1”和“X1”是一个写法）。"""
    seen: set[str] = set()
    kept = []
    for name in names:
        key = _name_key(name)
        if key and key not in seen:
            seen.add(key)
            kept.append(name)
    return tuple(kept)


def _v1(data: Mapping[str, object]) -> Lexicon:
    if set(data) - {"aliases", "words"}:
        raise ConfigError("别名表的配置只能有 aliases 和 words 两项（第二版要在开头写 format: javmem-lexicon/2）")
    if not isinstance(data.get("aliases") or [], list):
        raise ConfigError("别名表的配置里 aliases 要写成列表")
    groups = []
    for number, group in enumerate(data.get("aliases") or [], 1):
        if not isinstance(group, dict) or set(group) != {"type", "names"}:
            raise ConfigError(f"别名表的第 {number} 组要写成 {{type: 类型, names: [名字, ……]}}")
        if group["type"] not in ENTITY_TYPES:
            raise ConfigError(f"别名表的第 {number} 组的类型要是 {'、'.join(ENTITY_TYPES)} 之一")
        names = _names(group["names"], f"第 {number} 组的 names")
        if len(names) < 2:
            raise ConfigError(f"别名表的第 {number} 组至少要有两个名字")
        groups.append((group["type"], names))
    words = _names(data.get("words") or [], "words")
    every = dict.fromkeys([*words, *(name for _, names in groups for name in names)])
    return Lexicon(aliases=tuple(groups), words=tuple(every), listed_words=tuple(dict.fromkeys(words)))


def _parse_groups(value: object) -> list[AliasGroup]:
    if not isinstance(value or [], list):
        raise ConfigError("别名表的配置里 aliases 要写成列表")
    groups = []
    for number, group in enumerate(value or [], 1):
        where = f"第 {number} 组"
        if not isinstance(group, dict) or not {"type", "names"} <= set(group) or set(group) - {"type", "names", "where", "from_types"}:
            raise ConfigError(f"别名表的{where}要写成 {{type: 类型, names: [名字, ……]}}，可以加 where、from_types")
        if group["type"] not in ENTITY_TYPES:
            raise ConfigError(f"别名表的{where}的类型要是 {'、'.join(ENTITY_TYPES)} 之一")
        from_types = _types(group["from_types"], f"{where}的 from_types") if "from_types" in group else ()
        if group["type"] in from_types:
            raise ConfigError(f"别名表的{where}的 from_types 不能有这一组自己的类型")
        names = _dedupe(_names(group["names"], f"{where}的 names"))
        if len(names) < (1 if from_types else 2):
            raise ConfigError(f"别名表的{where}至少要有两个名字（只改类型的、写了 from_types 的可以一个）")
        partitions = _partitions(group["where"], f"{where}的 where") if "where" in group else None
        groups.append(AliasGroup(group["type"], names, partitions, from_types, where))
    return groups


def _parse_bots(value: object) -> list[BotEntry]:
    if not isinstance(value or [], list):
        raise ConfigError("别名表的配置里 bots 要写成列表")
    bots = []
    for number, bot in enumerate(value or [], 1):
        where = f"第 {number} 个 Bot"
        if not isinstance(bot, dict) or "names" not in bot or set(bot) - {"names", "plain"}:
            raise ConfigError(f"别名表的{where}要写成 {{names: [名字, ……]}}，可以加 plain")
        names = _dedupe(_names(bot["names"], f"{where}的 names"))
        plain = _dedupe(_names(bot["plain"], f"{where}的 plain")) if "plain" in bot else ()
        if not {_name_key(one) for one in plain} <= {_name_key(one) for one in names}:
            raise ConfigError(f"别名表的{where}的 plain 要是 names 里的写法")
        bots.append(BotEntry(names, plain))
    return bots


def _parse_apart(value: object) -> list[Apart]:
    if not isinstance(value or [], list):
        raise ConfigError("别名表的配置里 apart 要写成列表")
    found = []
    for number, item in enumerate(value or [], 1):
        where = f"第 {number} 条“不是同一个”"
        if not isinstance(item, dict) or set(item) != {"type", "names"} or item["type"] not in ENTITY_TYPES:
            raise ConfigError(f"别名表的{where}要写成 {{type: 类型, names: [名字, 名字]}}")
        names = _dedupe(_names(item["names"], f"{where}的 names"))
        if len(names) < 2:
            raise ConfigError(f"别名表的{where}至少要有两个名字")
        found.append(Apart(item["type"], names, where))
    return found


def build(
    groups: Sequence[AliasGroup],
    bots: Sequence[BotEntry] = (),
    not_entities: Sequence[str] = (),
    path_like: bool = False,
    apart: Sequence[Apart] = (),
    words: Sequence[str] = (),
    default_where: tuple[str, ...] | None = None,
    records: Sequence[dict[str, object]] = (),
) -> Lexicon:
    """第二版：核对以后建索引。对不上的报 ConfigError，消息里不带名字。"""
    index: dict[tuple[str, str], int] = {}
    for number, group in enumerate(groups):
        for name in group.names:
            key = _name_key(name)
            for group_type in group.types:
                other = index.setdefault((group_type, key), number)
                if other != number:
                    raise ConfigError(
                        f"别名表的{groups[other].origin}和{group.origin}有同一个写法（类型 {group_type}，from_types 也算）：一个写法只能在一组"
                    )
    bot_index: dict[str, int] = {}
    for number, bot in enumerate(bots):
        for name in bot.names:
            other = bot_index.setdefault(_name_key(name), number)
            if other != number:
                raise ConfigError(f"别名表的第 {other + 1} 个和第 {number + 1} 个 Bot 有同一个写法")
    group_keys = {key for _, key in index}
    for number in sorted({bot_index[key] for key in bot_index if key in group_keys}):
        raise ConfigError(f"别名表的第 {number + 1} 个 Bot 的写法也在别名表的组里：Bot 名单和组只能写在一处")
    not_keys = frozenset(key for key in (_name_key(name) for name in not_entities) if key)
    if not_keys & group_keys or not_keys & set(bot_index):
        raise ConfigError("别名表的 not_entities 里有写法也在组里或者 Bot 名单里")
    apart_index: dict[tuple[str, str], set[str]] = {}
    for item in apart:
        keys = [_name_key(name) for name in item.names]
        numbers = [index.get((item.type, key)) for key in keys]
        hit = [number for number in numbers if number is not None]
        if len(hit) != len(set(hit)):
            number = next(number for number in hit if hit.count(number) > 1)
            raise ConfigError(f"别名表的{item.origin}里有两个名字在{groups[number].origin}里：一份说是同一个、一份说不是，先改别名表")
        for key in keys:
            apart_index.setdefault((item.type, key), set()).update(set(keys) - {key})
    every = dict.fromkeys(
        [*words, *(name for group in groups for name in group.names), *(name for bot in bots for name in bot.names)]
    )
    return Lexicon(
        aliases=tuple((group.type, group.names) for group in groups),
        words=tuple(every),
        version=2,
        listed_words=tuple(dict.fromkeys(words)),
        groups=tuple(groups),
        bots=tuple(bots),
        not_entities=tuple(not_entities),
        path_like=path_like,
        apart=tuple(apart),
        default_where=default_where,
        records=tuple(records),
        _groups=index,
        _bots=bot_index,
        _not=not_keys,
        _named=frozenset(group_keys | set(bot_index)),
        _apart={where: frozenset(keys) for where, keys in apart_index.items()},
    )


def _v2(data: Mapping[str, object], records: Sequence[dict[str, object]] = ()) -> Lexicon:
    allowed = {"format", "default_where", "aliases", "bots", "not_entities", "apart", "words"}
    if set(data) - allowed:
        raise ConfigError(f"别名表第二版的配置只能有 {'、'.join(sorted(allowed - {'format'}))} 这几项")
    default_where = _partitions(data["default_where"], "default_where") if "default_where" in data else None
    not_entities = data.get("not_entities") or {}
    if not isinstance(not_entities, dict) or set(not_entities) - {"names", "path_like"}:
        raise ConfigError("别名表的配置里 not_entities 要写成 {names: [名字, ……], path_like: true|false}")
    path_like = not_entities.get("path_like", False)
    if not isinstance(path_like, bool):
        raise ConfigError("别名表的配置里 not_entities 的 path_like 要写成 true 或 false")
    groups = _parse_groups(data.get("aliases"))
    apart = _parse_apart(data.get("apart"))
    groups, apart = apply_records(groups, apart, records, default_where)
    return build(
        groups,
        _parse_bots(data.get("bots")),
        _names(not_entities.get("names") or [], "not_entities 的 names"),
        path_like,
        apart,
        _names(data.get("words") or [], "words"),
        default_where,
        records,
    )


def load_lexicon(path: Path | None) -> Lexicon:
    """读仓库外的别名表和词典。没配返回空的；写错了报 ConfigError，消息里不带名字。"""
    if path is None:
        return EMPTY
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as error:
        raise ConfigError(f"别名表的配置读不了：{type(error).__name__}") from None
    if data is None:
        if merges_path(path).exists() and read_records(merges_path(path)):
            raise ConfigError("别名表旁边有合并记录，别名表要是第二版（开头写 format: javmem-lexicon/2）")
        return EMPTY
    if not isinstance(data, dict):
        raise ConfigError("别名表的配置只能有 aliases 和 words 两项")
    records = read_records(merges_path(path))
    if "format" not in data:
        if records:
            raise ConfigError("别名表旁边有合并记录，别名表要是第二版（开头写 format: javmem-lexicon/2）")
        return _v1(data)
    if data["format"] != FORMAT_V2:
        raise ConfigError(f"别名表的 format 只认 {FORMAT_V2}")
    return _v2(data, records)


# ---------- 合并记录（合并工具写的，javmem.entities，J3.6） ----------
#
# 别名表旁边一个只追加的文件（lexicon.yaml 旁边的 lexicon.merges.jsonl），一行一条，只有编号、名字、时间，不带原话：
#   merge：这几个名字是同一个（type、standard 标准名、names、where、from_types，另记 old_ids、new_id、proposal 备查）
#   apart：这几个名字不是同一个（type、names；拆开一个叫法的另记 entity、alias）
#   undo：撤销前面的一条 merge 或 apart（undoes）
#   copied：人写的那份已经誊进去了的几条（records），读的时候跳过，记录不删
# 读别名表时，先读人写的那份，再按先后接上这一份（撤销了的、誊过的跳过）：合并记录在账本外面，整本重建也丢不了。
# merge 和人写的组有同一个写法的并成一组，标准名用记录里的；和“不是同一个”对不上的报配置错误，什么都不动。

MERGES_SUFFIX = ".merges.jsonl"
RECORD_KINDS = ("merge", "apart", "undo", "copied")
_RECORD_ID = re.compile(r"M\d{4,}", re.ASCII)


def merges_path(path: Path) -> Path:
    """别名表旁边的合并记录：lexicon.yaml 旁边的 lexicon.merges.jsonl。"""
    return path.with_name(path.stem + MERGES_SUFFIX)


def _record_names(value: object, where: str, minimum: int) -> None:
    if not isinstance(value, list) or len(value) < minimum or not all(isinstance(name, str) and name.strip() for name in value):
        raise ConfigError(f"{where}的 names 要是至少 {minimum} 个名字的列表")


def _check_record(record: object, number: int, seen: Mapping[str, Mapping[str, object]]) -> None:
    where = f"合并记录第 {number} 行"
    if not isinstance(record, dict):
        raise ConfigError(f"{where}要是一个对象")
    kind, record_id = record.get("kind"), record.get("id")
    if kind not in RECORD_KINDS:
        raise ConfigError(f"{where}的 kind 要是 {'、'.join(RECORD_KINDS)} 之一")
    if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
        raise ConfigError(f"{where}的编号要写成 M 加四位数")
    if record_id in seen:
        raise ConfigError(f"{where}的编号 {record_id} 和前面的重了")
    if not all(isinstance(record.get(name), str) for name in ("at", "by", "note")):
        raise ConfigError(f"{where}要有 at、by、note，都是文字")
    if kind == "merge":
        if record.get("type") not in ENTITY_TYPES or not isinstance(record.get("standard"), str) or not str(record["standard"]).strip():
            raise ConfigError(f"{where}要有 type（六种类型之一）和 standard（标准名）")
        _record_names(record.get("names"), where, 0)
        if record.get("where") is not None:
            _partitions(record["where"], f"{where}的 where")
        from_types = record.get("from_types") or []
        if from_types:
            _types(from_types, f"{where}的 from_types")
        if record["type"] in from_types:
            raise ConfigError(f"{where}的 from_types 不能有它自己的类型")
        if not isinstance(record.get("old_ids", []), list) or not isinstance(record.get("new_id", ""), str):
            raise ConfigError(f"{where}的 old_ids 要是列表、new_id 要是文字")
    elif kind == "apart":
        if record.get("type") not in ENTITY_TYPES:
            raise ConfigError(f"{where}要有 type（六种类型之一）")
        _record_names(record.get("names"), where, 2)
    elif kind == "undo":
        target = record.get("undoes")
        if not isinstance(target, str) or target not in seen or seen[target].get("kind") not in ("merge", "apart"):
            raise ConfigError(f"{where}撤销的要是前面的一条 merge 或 apart")
        if any(other.get("kind") == "undo" and other.get("undoes") == target for other in seen.values()):
            raise ConfigError(f"{where}撤销的那一条前面已经撤销过了")
    else:
        targets = record.get("records")
        if not isinstance(targets, list) or not targets or not all(
            isinstance(item, str) and item in seen and seen[item].get("kind") in ("merge", "apart") for item in targets
        ):
            raise ConfigError(f"{where}的 records 要是前面的 merge、apart 的编号")


def read_records(path: Path) -> list[dict[str, object]]:
    """读合并记录，一行一条 JSON，按先后。没有这个文件是空的。写错了报 ConfigError：第几行、哪里不对，不带名字。"""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"合并记录读不了：{type(error).__name__}") from None
    records: list[dict[str, object]] = []
    seen: dict[str, dict[str, object]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            raise ConfigError(f"合并记录第 {number} 行不是 JSON") from None
        _check_record(record, number, seen)
        seen[str(record["id"])] = record
        records.append(record)
    return records


def active_records(records: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """算数的 merge、apart：没撤销、没誊进人写的那份的，按先后。"""
    undone = {record["undoes"] for record in records if record["kind"] == "undo"}
    copied = {item for record in records if record["kind"] == "copied" for item in record["records"]}  # type: ignore[union-attr]
    return [record for record in records if record["kind"] in ("merge", "apart") and record["id"] not in undone | copied]


def _effective_where(where: object, default_where: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if where is None:
        return default_where
    return tuple(sorted(set(where)))  # type: ignore[arg-type]


def apply_records(
    groups: Sequence[AliasGroup],
    apart: Sequence[Apart],
    records: Sequence[Mapping[str, object]],
    default_where: tuple[str, ...] | None,
) -> tuple[list[AliasGroup], list[Apart]]:
    """人写的组和“不是同一个”接上合并记录。merge 和已有的组（类型对得上、有同一个写法）并成一组：标准名、类型用记录里的，
    别的类型进 from_types，分区取并集（有一边是哪个分区都算的，就哪个分区都算）。"""
    groups, apart = list(groups), list(apart)
    for record in active_records(records):
        if record["kind"] == "apart":
            apart.append(Apart(str(record["type"]), _dedupe(tuple(record["names"])), str(record["id"])))  # type: ignore[arg-type]
            continue
        group_type = str(record["type"])
        types = {group_type, *record.get("from_types", [])}  # type: ignore[misc]
        names = list(_dedupe((str(record["standard"]), *record["names"])))  # type: ignore[misc]
        keys = {_name_key(name) for name in names}
        touching = [g for g in groups if set(g.types) & types and keys & {_name_key(name) for name in g.names}]
        wheres = [_effective_where(record.get("where"), default_where)]
        wheres += [g.where if g.where is not None else default_where for g in touching]
        for group in touching:
            names += group.names
            types |= set(group.types)
        where = None if any(item is None for item in wheres) else tuple(sorted({p for item in wheres for p in item}))  # type: ignore[union-attr]
        merged = AliasGroup(
            group_type,
            _dedupe(names),
            where,
            tuple(sorted(types - {group_type}, key=ENTITY_TYPES.index)),
            str(record["id"]),
        )
        groups = [g for g in groups if not any(g is other for other in touching)] + [merged]
    return groups, apart


def next_record_id(records: Sequence[Mapping[str, object]]) -> str:
    numbers = [int(str(record["id"])[1:]) for record in records]
    return f"M{max(numbers, default=0) + 1:04d}"


def append_record(path: Path, record: Mapping[str, object]) -> None:
    """往合并记录后面加一行，写完落盘。只追加，不改前面的。"""
    line = json.dumps(dict(record), ensure_ascii=False, sort_keys=False)
    with open(merges_path(path), "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def lexicon_with(path: Path, extra: Sequence[Mapping[str, object]]) -> Lexicon:
    """人写的那份、已有的合并记录，再加上 extra 这几条（还没写进文件的），读出来的别名表：对不上的报 ConfigError。
    合并工具写记录以前先用它核一遍。"""
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as error:
        raise ConfigError(f"别名表的配置读不了：{type(error).__name__}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT_V2:
        raise ConfigError("合并工具要别名表是第二版（开头写 format: javmem-lexicon/2）")
    records = read_records(merges_path(path))
    seen = {str(record["id"]): record for record in records}
    for offset, record in enumerate(extra, 1):
        _check_record(dict(record), len(records) + offset, seen)
        seen[str(record["id"])] = dict(record)
    return _v2(data, [*records, *(dict(record) for record in extra)])
