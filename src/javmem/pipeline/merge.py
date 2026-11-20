"""L5 合并和替代：同一事实合并证据，规则判新旧。这里是不碰账本的那一半，写账本的在 javmem.pipeline.write。

**同一事实：** 可见范围一样（D30：范围不同的不合并，内容相同也各算一条）、主体一样、属性和值一样
（比对写法：NFKC、不分大小写、去掉空白和标点），就是同一条事实，证据合在一起。没有属性或值的，按事实文字比。
键是 merge_key，事实编号 F 加它的前 16 位：同样的说法不管哪个窗口、什么先后写进来，都是同一条。

**判新旧：** 可见范围、主体、属性一样，值不一样，算同一件事的新旧说法（group_key）。按每一句算：一条事实最后
一次被说之后，同组别的值第一次被说的那一句替代它，替代的时间就是那一句的说话时间；之后再没有别的值被说的
现行（几条同一时刻说的都现行）。晚到的旧消息自然排在前面，只补历史。

- **同一时刻说的不互相替代：** 同一句话、同一张表里列出的几档报价是并列的。
- **多值的属性不判新旧：** 属性结尾的中心词是偏好、要求、安排、活动、字段、报价、清单这类的，一个主体同时可以有
  好几条，新来的不让旧的失效（MULTI_VALUED）。看 36 个测试包，值不一样的 11 组里，替代错了的都是这几种。
- **比对写法里数的标点留着：** 12.5 和 125、11/2 和 1/12、$100 和 ¥100、-5 和 5 不是同一个值（key_text）。
- **冻结的不自动替代（D12，J2.12 管冻结和待确认）：** 冻结的那条一直现行；比它晚说的同组事实写成待确认
  （pending_reason frozen_conflict），每一对进一次待确认队列。
- **外部内容不替代主人原话（D204）：** 主人自己说过的那条（没冻结），后来外部内容（邮件、[外部:…]、[转发 …]、附件说明，
  J2.13 的转述）说了别的值，不替代它；外部说的那条和冻结的一样待确认（frozen_conflict），主人定了才换。按每一刻算：这一刻
  这条事实的几句全不是说话人自己的话才算外部。主人自己后来说的、Bot 和同事在对话里自己说的照旧替代；外部对外部照旧。
  主人说过以后已经被换掉（别的值有过不是外部的一刻）、只是外部内容又说回来的旧值，不再护着。
- **可见范围不同的不判新旧：** 和不合并一样，一个身份看到的新旧只按它看得到的内容算（query-api 3.2）。

**实体对齐：** 名字（L3 给的最完整的叫法）的比对写法加类型，就是实体编号（E 加前 16 位）；没有同名同类型的，
按别名找，只认出处和这个窗口可见范围一样的别名（D30：看不到的内容不能把两个叫法连起来）：只对上一个就是它，
对上好几个就新建一个、进待确认（ambiguous_entity），这个实体当主体的事实写成待确认。名字和原文里的写法都记成别名，
出处是窗口里第一句写着它的话。代词、称呼、泛指（他、我们、对方、客户……）和一个字的名字不建实体、不记别名，
事实的主体是空的；对话里的主人一个库一个实体（L3 叫它“主人”的，以及主人自己说的“我”，见 write._is_owner）。
L3 直接写主人的名字的（D62），名字写在仓库外的配置里（JAVMEM_OWNER_FILE），也挂到主人上。
歧义按可见范围算：别的范围里的歧义不算。

**别名表第二版（J3.6，D100，javmem.zh.lexicon）：** 先后是主人 → 泛称和别名表里不建实体的 → 别名表的组 → 上面那几步。
组里的名字（或者拆开以后每一段都是这一组的）、类型对得上（from_types 也算）、窗口的分区都在这一组的 where 里的，
对到“类型加标准名”算出来的编号，实体的标准名就是组里第一个名字：和写的先后无关，整本重建、一包一包地写都一样。
按别名、拆名字找到的候选里，和这个名字记成“不是同一个”（apart）的去掉。没写 format 的第一版照旧只用来找。

规则版本 L5_VERSION。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from bisect import bisect_right
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

from javmem.config import ConfigError
from javmem.pipeline.normalize import Scope
from javmem.pipeline.verify import fold, parse_occurred
from javmem.yamlio import load_yaml
from javmem.zh import numbers as zh_numbers

# v5：附件的正文不算主人说的（文件里的“我”是写文件的人，D72 审阅）
# v6：J2.14 中文规则：名字里写在一起的几个叫法拆开、公司的法律后缀、仓库外的别名表、值的统一写法、Bot 一对一时的“你”
# v7：发件人那一句撤回了（“[已撤回]”）的邮件窗口里，主人的名字不按名字挂到主人上（D73 第 16 条以后的审阅）
# v8：别名表第二版（J3.6，D100）：组里的名字对到标准名算出来的编号（where、from_types），不建实体的名字，
#     按别名找到的候选里去掉“不是同一个”的
# v9：J4.4 关系写进账本（同一条关系合证据、分段），只出现在关系里的“我”也看关系的证据是谁说的
# v10：J4.6 对话里纠正关系（javmem.pipeline.correct）：纠正掉的结束日子不拿说话那天顶上、记着被纠正，财务投资的先挡住，
#      Bot 当场说错的不记（R05；和纠正给成同一处说它结束了的说法的，Bot 那句不算证据），同一对人纠正成另一种亲属的旧的结束，
#      抽取没抽出来的纠正问主人；没写开始日子又说它结束了的，结束日子写法和已经结束的哪一段一样，归那一段（真调测试题 K25）
# v11：D204 外部内容（邮件、[外部:…]、[转发 …]、附件说明）后来说的不替代主人原话说的，进待确认，主人定了才换
# v12：D210 加了隶属于：同一时间一个、按主体（一个部门同一时间只归一家，新的来了旧的结束）；负责的“代表哪一家”顺着隶属于
#      找到最上面那家（D215，write._top_unit）
L5_VERSION = "l5-v12"

# 多值的属性：一个主体同时可以有好几条，新的不替代旧的。看属性结尾的中心词（比对写法）：
# “投资偏好”“活动安排”是多值的，“计划出货日期”“任务负责人”“报价单价”不是
MULTI_VALUED = re.compile(
    r"(?:偏好|喜好|爱好|兴趣|要求|规矩|规则|纪律|习惯|约定|原则|注意事项|注意|提醒|待办|事项|任务|安排|活动|日程|计划|"
    r"字段|栏目|报价|建议|意见|观点|看法|问题|风险|备注|说明|记录|事件|进展|动态|经历|特点|优点|缺点|标签|清单|列表|"
    r"preferences?|requirements?|rules?|notes?|tasks?|todos?|plans?|events?|quotes?|fields?|tags?|lists?)$"
)
# 不记成别名的：代词、称呼、泛指
NOT_ALIASES = frozenset(
    {
        "我", "我们", "你", "您", "你们", "我自己", "你自己", "您自己", "他", "她", "它", "他们", "她们", "它们", "咱们",
        "大家", "自己", "本人", "对方",
        "主人", "用户", "客户", "老板", "领导", "同事", "朋友", "孩子", "儿子", "女儿", "爸爸", "妈妈", "老公", "老婆",
        "这个", "那个", "这家", "那家", "这位", "那位", "该公司", "本公司", "公司", "对方公司", "银行", "学校",
        "i", "me", "we", "us", "you", "he", "she", "it", "they", "them", "client", "customer", "boss", "the company",
    }
)

_KEEP_SYMBOLS = frozenset("%$¥€£₩")


def key_text(text: str | None) -> str:
    """比对写法：fold 以后去掉标点，数里的标点留着。

    两个数字之间的小数点、斜杠、冒号、连字符留着（12.5 和 125、11/2 和 1/12 不一样），千分位的逗号去掉
    （12,500 和 12500 一样）；货币和百分号留着（$100 和 ¥100 不一样）；数前面的负号留着（-5 和 5 不一样）。
    """
    folded = fold(text or "")
    out = []
    for index, char in enumerate(folded):
        if char.isalnum():
            out.append(char)
            continue
        before = folded[index - 1] if index > 0 else ""
        after = folded[index + 1] if index + 1 < len(folded) else ""
        if char in ".,/:-" and before.isdigit() and after.isdigit():
            if char == "," and folded[index + 1 : index + 4].isdigit() and not folded[index + 4 : index + 5].isdigit():
                continue  # 千分位
            out.append(char)
        elif char in _KEEP_SYMBOLS:
            out.append(char)
        elif char in "-+" and after.isdigit() and not before.isdigit():
            out.append(char)
    return "".join(out)


def name_key(name: str) -> str:
    """实体名字、别名的比对写法：NFKC、不分大小写、去掉空白；两头的标点去掉，中间的留着（A+B、3.0）。"""
    folded = fold(name)
    return folded.strip("。，、；：！？.,;:!?…—–-~·()（）[]［］【】「」『』《》〈〉<>\"'")


def usable_alias(text: str) -> bool:
    """能当别名、能按名字对齐的写法：两个字以上，不是代词、称呼、泛指。"""
    key = name_key(text)
    return len(key) >= 2 and key not in NOT_ALIASES and unicodedata.normalize("NFKC", text).strip().casefold() not in NOT_ALIASES


# ---------- 名字里写在一起的几个叫法（J2.14） ----------

_PAREN = re.compile(r"\(([^()]{1,60})\)")  # NFKC 以后全角括号也是半角的
_SPACES = re.compile(r"\s+")
_SPACED_SLASH = re.compile(r"\s+/\s*|\s*/\s+")  # 两边至少一边有空格的斜杠；KVLT/NRDX 这种连着写的不拆
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9 .\-]{1,14}")  # 证券代码、编号这类：大写字母和数字
_MAX_NAME = 120  # 再长的名字不拆：不是名字，正则也会慢
# 公司的法律后缀（只对“公司或机构”）：岱川实业有限公司、Halvorsen Co. Ltd.。控股、集团、Holdings 不算：
# “岱川控股”和“岱川有限公司”可能是两家
_LEGAL_SUFFIX = re.compile(
    r"(?:\s*+(?:股份有限公司|有限责任公司|有限公司|股份公司)|[\s,]++(?:co\.?,?\s*+ltd\.?|company\s++limited|limited|ltd\.?|"
    r"inc\.?|incorporated|corp\.?|corporation|llc|l\.l\.c\.|plc|pte\.?\s*+ltd\.?|pty\.?\s*+ltd\.?|gmbh|ag|s\.a\.|sa|n\.v\.|"
    r"nv|b\.v\.|bv|k\.k\.|kk))+$",
    re.IGNORECASE,
)
# 括号里含这些的，是在说哪一部分、哪一类、什么身份（“Halvorsen（日本）”是日本那家，“NRDX（债券）”的“债券”是品类，
# “赵六（采购经理）”的“采购经理”是职务），不是另一个叫法：整个名字不拆
_QUALIFIER = re.compile(
    r"中国|大陆|内地|香港|澳门|台湾|日本|韩国|美国|英国|法国|德国|瑞士|新加坡|马来西亚|泰国|越南|印度|澳洲|澳大利亚|"
    r"加拿大|欧洲|亚洲|亚太|北美|全球|国际|海外|上海|北京|深圳|广州|杭州|东京|纽约|伦敦|总部|总行|分行|支行|分公司|"
    r"子公司|母公司|办事处|分部|部门|事业部|团队|集团|控股|中国区|大中华区|本地|当地|已注销|已退市|退市|停牌|"
    r"[一二三四五六七八九十\d]+期|份额|累购|累沽|正股|股票|期权|债券|基金|票据|存款|理财|结构性产品|账户|持仓|组合|"
    r"总监|经理|主任|主管|总裁|董事|老板|老师|律师|医生|司机|秘书|助理|房东|阿姨|保姆|会计|顾问|代表|专员|客服|销售|"
    r"财务|同事|朋友|太太|先生|女士|中介|业主|甲方|乙方|房客|租客|物业|供应商|厂家|商家|师傅|家长|学生|护士|翻译|代理|经纪|"
    r"(?a:(?i:\b(?:japan|china|hong kong|singapore|europe|asia|apac|global|international|group|branch|hq|"
    r"common stock|stock|shares|bond|fund|account|ceo|cfo|cto|coo|vp|director|manager|partner|sales|assistant|"
    r"lawyer|driver|boss|pm|hr|rm|bd|ir|gm|md|ae|"
    r"nasdaq|nyse|hkex|sgx|tse|lse|sse|szse|otc|adr|fcn|etf|reit|ipo|usd|hkd|sgd|cny|rmb|jpy|eur|usa|emea|latam)\b))"
)
_NICKNAME = 3  # 人名括号里两边都是中文的，括号里不超过这么多字才当小名（沈禾苗（苗苗））


@dataclass(frozen=True)
class NameParts:
    """名字里拆出来的叫法（merge.name_parts）。

    outer：括号外面那段、去掉法律后缀的、人名斜杠两边的，按别名找实体；inner：括号里的另一个叫法，只按实体自己的名字找
    （别的实体括号里拆出来的别名不算，免得“职务”“地方”这类把不同的人连起来）；codes：括号里的代码（4421 JT），
    是很强的标识，按别名找。三样都记成别名。
    """

    outer: tuple[str, ...] = ()
    inner: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()

    @property
    def every(self) -> list[str]:
        return list(dict.fromkeys([*self.outer, *self.inner, *self.codes]))


def _has_cjk(text: str) -> bool:
    return any(unicodedata.name(char, "").startswith("CJK UNIFIED") for char in text)


def _has_latin(text: str) -> bool:
    return any(char.isascii() and char.isalpha() for char in text)


def _ticker_like(text: str) -> bool:
    """像证券代码、缩写：大写字母和数字，带数字或者至少三个字母（HK、US 这种两个字母的是地方，不算）。"""
    text = text.strip()
    if not _TICKER.fullmatch(text):
        return False
    return any(char.isdigit() for char in text) or sum(char.isalpha() for char in text) >= 3


def _code(text: str) -> bool:
    """编号、带数字的代码（4421 JT、6127 HK、XS0000000001）：很强的标识，按别名找。全是字母的缩写（KVLT、FCN、NASDAQ）
    不算：交易所、产品类型的缩写会把不同的东西连起来。"""
    return _ticker_like(text) and any(char.isdigit() for char in text)


def _codes(text: str, test=_code) -> list[str]:
    parts = [part.strip() for part in _SPACED_SLASH.split(text)]
    return parts if all(test(part) for part in parts) else []


def _alternative(outer: str, inner: str, entity_type: str) -> bool:
    """括号里外是不是同一个东西的两种叫法。人：一边中文一边是字母（程一舟（Yizhou）），或者两边都是中文、括号里是
    不超过三个字的小名；别的：一边中文一边不是（Corvalis（松岚）、远帆（6127 HK）），或者一边是代码（KVLT（Kovalt））。
    括号里是地方、分支、品类、职务的不算。"""
    if not outer or not inner or _QUALIFIER.search(inner):
        return False
    if entity_type == "人":
        if _has_cjk(outer) and _has_cjk(inner):
            return not _has_latin(inner) and len(inner) <= _NICKNAME
        return _has_cjk(outer) != _has_cjk(inner)
    return _has_cjk(outer) != _has_cjk(inner) or _ticker_like(outer) or bool(_codes(inner, _ticker_like))


def name_parts(name: str, entity_type: str) -> NameParts:
    """名字里写在一起的几个叫法（J2.14）：括号里外（“Corvalis（松岚）”“Morrow (4421 JT)”）；人名两边带空格的斜杠，
    要一边中文一边是字母（“沈禾苗（苗苗）/ Nora”）；公司或机构去掉法律后缀（“岱川实业有限公司”是“岱川实业”）、
    括号里斜杠隔开的几个代码（“(4421 JT / 4421.T)”）。资产、订单里的斜杠是一篮子，不拆。都是能当别名的写法，
    不含名字本身。"""
    text = unicodedata.normalize("NFKC", name).strip()
    if len(text) > _MAX_NAME:
        return NameParts()
    inners = [match.group(1).strip() for match in _PAREN.finditer(text)]
    outer = _SPACES.sub(" ", _PAREN.sub(" ", text)).strip()
    split = bool(inners) and all(_alternative(outer, inner, entity_type) for inner in inners)
    if not split:
        inners, outer = [], text
    outers = [outer] if split else []
    names: list[str] = []
    codes: list[str] = []
    for inner in inners:
        tickers = _codes(inner, _ticker_like) if entity_type != "人" else []
        if len(tickers) > 1 and entity_type != "公司或机构":
            continue  # 资产、订单的括号里几个代码是一篮子
        strong = _codes(inner) if entity_type != "人" else []
        if strong:
            codes += strong  # 带数字的代码：按别名找
        elif len(tickers) > 1:
            names += tickers  # 公司的几个全是字母的缩写：当括号里的叫法
        else:
            names.append(inner)
    if entity_type == "人":
        pieces = [piece.strip() for piece in _SPACED_SLASH.split(outer)]
        if len(pieces) == 2 and _has_cjk(pieces[0]) != _has_cjk(pieces[1]):
            outers = pieces
    if entity_type == "公司或机构":
        stripped = _LEGAL_SUFFIX.sub("", outer).strip(" ,")
        if stripped != outer:
            outers.append(stripped)
    own = name_key(name)

    def usable(parts: list[str]) -> tuple[str, ...]:
        return tuple(part for part in dict.fromkeys(parts) if part and usable_alias(part) and name_key(part) != own)

    return NameParts(outer=usable(outers), inner=usable(names), codes=usable(codes))


def name_variants(name: str, entity_type: str) -> list[str]:
    """name_parts 拆出来的每一个叫法。"""
    return name_parts(name, entity_type).every


# 对话里的主人（规范第 5 节）：几个包、几个 Bot 说的都是同一个人，一个库一个实体。邮件里的“我”是发件人，不算。
# L3 叫主人的名字（渲染给它的窗口里主人标的是“主人”）不管谁说的都是主人；第一人称要看是谁说的（write._is_owner）
OWNER_NAMES = frozenset({"主人", "记忆主人", "记忆的主人"})
FIRST_PERSON = frozenset({"我", "我自己", "本人"})
# D78：Bot 一对一跟主人说话时的“你”“您”是主人（窗口里只有主人和这一个 Bot 说话时，见 write._is_owner）
SECOND_PERSON = frozenset({"你", "您", "你自己", "您自己"})
OWNER_NAME = "主人"

# D62：主人的名字（中文名、英文名）。L3 有时直接写主人的名字，或者拼在一起写（“中文名 / 英文名”“主人（英文名）”），
# 这些也是主人。名字只写在仓库外的配置里，代码里没有。拼写之间认这些分隔：斜杠、竖线、顿号、逗号、分号、括号
_NAME_PARTS = re.compile(r"[/／|｜、,，;；()（）]")


def load_owner_names(path: Path | None) -> frozenset[str]:
    """主人的名字的比对写法。配置文件是 YAML：names: [中文名, 英文名]。没配是空的，只认“主人”“我”这些叫法。
    写错了报 ConfigError，消息里不带名字。"""
    if path is None:
        return frozenset()
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as error:
        raise ConfigError(f"主人的名字的配置读不了：{type(error).__name__}") from None
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list) or not names or not all(isinstance(name, str) and name_key(name) for name in names):
        raise ConfigError("主人的名字的配置要写成 names: [名字, ……]，每个都不能是空的")
    unknown = set(data) - {"names"}
    if unknown:
        raise ConfigError("主人的名字的配置里有认不出的字段")
    return frozenset(name_key(name) for name in names)


def owner_by_name(name: str, owner_keys: Collection[str]) -> bool:
    """这个名字是不是主人（D62）：就是配置里的一个名字，或者只由配置里的名字和“主人”这些叫法拼成，
    而且至少有一个是配置里的名字。“英文名 的同事”“中文名公司”这种不是。"""
    if not owner_keys:
        return False
    parts = [key for key in (name_key(part) for part in _NAME_PARTS.split(name)) if key]
    allowed = set(owner_keys) | {name_key(label) for label in OWNER_NAMES}
    return bool(parts) and all(part in allowed for part in parts) and any(part in owner_keys for part in parts)


def _sha(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def scope_key(scope: Scope) -> str:
    return json.dumps(scope.as_dict(), ensure_ascii=False, sort_keys=True)


def value_text(value: str | None, said: date | None = None, attribute: str | None = None) -> str:
    """值的比对写法：只是一个金额或一个日期的换成统一的写法（J2.14，javmem.zh.numbers.value_key：`5 万` 和 `50000`、
    `USD 384k` 和 `USD 384,000`、`11月18日` 和 `2026-11-18` 是同一个值），别的照 key_text。"""
    if value is None:
        return ""
    return zh_numbers.value_key(value, said, attribute) or key_text(value)


def merge_key(
    scope: Scope, subject_id: str | None, attribute: str | None, value: str | None, content: str, *, said: date | None = None
) -> str:
    """同一事实的键。said 是说法最早的那一句说话的那天：没写年份的日期取它的那一年。"""
    if key_text(attribute) and key_text(value):
        value_key = value_text(value, said, attribute)
        return _sha("javmem-fact", scope_key(scope), subject_id or "", key_text(attribute), value_key)
    return _sha("javmem-fact-text", scope_key(scope), subject_id or "", key_text(content))


def group_key(scope: Scope, subject_id: str | None, attribute: str | None) -> str | None:
    """判新旧的组。没有主体、没有属性、属性是多值的（看结尾的中心词），没有组。"""
    key = key_text(attribute)
    if not subject_id or not key or MULTI_VALUED.search(key):
        return None
    return _sha("javmem-group", scope_key(scope), subject_id, key)


def fact_id_of(key: str) -> str:
    return "F" + key[:16]


def entity_id_of(entity_type: str, key: str) -> str:
    return "E" + _sha("javmem-entity", entity_type, key)[:16]


def alias_id_of(entity_id: str, alias_key: str) -> str:
    return "A" + _sha("javmem-alias", entity_id, alias_key)[:16]


# ---------- 一条事实从它的几处说法算出来 ----------


@dataclass(frozen=True)
class ClaimView:
    """算事实用的一处说法，从 fact_claims 读出来的。"""

    claim_id: str
    content: str
    subject_entity_id: str | None
    attribute: str | None
    value: str | None
    occurred_at: str | None
    time_precision: str
    status: str
    speaker: str
    speaker_role: str
    said_from: str
    said_from_epoch: int
    said_to: str
    said_to_epoch: int
    notes: tuple[str, ...]
    extracted_by: str
    l4_version: str
    attributed_to: str | None = None  # 这条说法出自谁（L3 的 speaker）；speaker 是证据那一句是谁说的


def representative(claims: Sequence[ClaimView]) -> ClaimView:
    """代表这条事实的说法：最早说的那一处，同一时刻按说法编号。事实的文字、时间、说话人、说法出自谁都取它的。"""
    return min(claims, key=lambda claim: (claim.said_from_epoch, claim.claim_id))


def latest(claims: Sequence[ClaimView]) -> ClaimView:
    """最后一次说的那一处：as_of 取它。"""
    return max(claims, key=lambda claim: (claim.said_to_epoch, claim.claim_id))


def base_status(claims: Iterable[ClaimView]) -> str:
    """有一处是核对过、原话直接说的，就是 verified。"""
    return "verified" if any(claim.status == "verified" for claim in claims) else "inferred"


@dataclass(frozen=True)
class GroupFact:
    """组里的一条事实。statements 是它的每一句证据的说话时间 (epoch, ISO)。

    owner：哪几刻（说话时间 epoch）参加判新旧的说法里有主人自己说的（origin said、身份 owner，D204）。external：哪几刻
    这条事实的几句全是外部内容（origin 是 external、relayed、attachment_note；不知道的不算）。"""

    fact_id: str
    statements: tuple[tuple[int, str], ...]
    frozen: bool
    owner: frozenset[int] = frozenset()
    external: frozenset[int] = frozenset()

    @property
    def last(self) -> int:
        return max(epoch for epoch, _ in self.statements)


@dataclass(frozen=True)
class Succession:
    """一组事实的新旧：谁被谁替代、在什么时候；哪些和冻结的、主人原话的冲突，待确认。"""

    superseded: dict[str, tuple[str, str]]  # 事实 → (替代它的, 替代的时间)
    conflicts: list[tuple[str, str]]  # (冻结的或者主人原话的, 比它晚说的)


def succession(facts: Sequence[GroupFact], switched: Collection[tuple[str, str]] = ()) -> Succession:
    """按每一句算：一条事实最后一次被说之后，同组别的值第一次被说的那一句替代它，替代的时间就是那一句的说话时间。

    之后再没有别的值被说的，现行（几条同一时刻说的都现行）。冻结的不替代，它最后一次被说之后又被说过的别的值，
    都和它冲突。

    外部内容不替代主人原话（D204）：没冻结、有主人原话的那条，它最后一次被说之后，按先后看别的值的每一刻：外部的那一刻
    （没冻结的别的事实说的、这一刻全是外部内容、主人没定过照它换：switched 里没有 (它, 那一条)）不替代它，记下冲突，接着
    往后看；碰到第一刻不是外部的，照常替代它，前面记下的都不算了（它已经不现行）；一直没碰到的，它现行，记下的每一条都和
    它冲突（一条只记一次）。主人最后一次说它以后、它最后一次被说以前，别的值有过不是外部的一刻的，不护着：那是主人（或者
    Bot、同事）已经换掉了，后来只是外部内容又说回来（审阅第 1 条）。外部的那一刻往后一句一句看，不切片（组大时不翻倍）。

    组里每一句只排一次序，键和以前每条事实各排一次时一样（说话时间、ISO 写法、事实编号），每条事实从它最后一次被说的
    地方往后二分着找。以前每一条都把组里别的句子挑一遍、各排一次，组一大就是平方地涨（收件变慢第二轮：一组几千条时，
    每写进一条都要比几千万次）。自己的句子都不比自己最后一次被说晚，挑不中；同一个编号传了两份的（写账不会这样传）
    也和以前一样跳过。算出来的和以前一样，连先后。ISO 写法是空的（只有手改过的决定文件导进来才会有）排在同一时刻的
    最前面：以前只排比各自晚的句子，碰不上就照常算，碰上了报错；现在全组一起排，不能拿空的去和字符串比。
    """
    superseded: dict[str, tuple[str, str]] = {}
    conflicts: list[tuple[str, str]] = []
    said = sorted(
        ((moment, at, fact.fact_id) for fact in facts for moment, at in fact.statements),
        key=lambda item: (item[0], item[1] is not None, item[1] or "", item[2]),
    )
    moments = [moment for moment, _, _ in said]
    frozen_ids = {fact.fact_id for fact in facts if fact.frozen}
    external = {fact.fact_id: fact.external for fact in facts}

    def outside(owner_fact: str, moment: int, other: str) -> bool:
        """这一刻对主人原话那条不算数：别的事实说的、没冻结、这一刻全是外部内容、主人也没定过照它换。"""
        return other not in frozen_ids and moment in external[other] and (owner_fact, other) not in switched

    for fact in sorted(facts, key=lambda item: item.fact_id):
        if not fact.statements:
            if any(fact_id != fact.fact_id for _, _, fact_id in said):
                raise ValueError(f"{fact.fact_id} 一句说话时间都没有")  # 以前在这里取最后一次被说，报的也是 ValueError
            continue
        start = bisect_right(moments, fact.last)  # 第一句比它最后一次被说晚的
        while start < len(said) and said[start][2] == fact.fact_id:
            start += 1
        if start == len(said):
            continue
        if fact.frozen:
            # 比它晚说的别的值都和它冲突；也冻结了的不算：两条主人都冻结了，都现行，不进待确认
            later = {fact_id for _, _, fact_id in said[start:] if fact_id != fact.fact_id}
            conflicts.extend((fact.fact_id, other) for other in sorted(later) if other not in frozen_ids)
            continue
        if fact.owner and all(said[index][2] == fact.fact_id or outside(fact.fact_id, said[index][0], said[index][2])
                              for index in range(bisect_right(moments, max(fact.owner)), start)):
            # 主人原话（D204），主人最后一次说它以后没被换掉过：外部的那几刻不替代它
            waiting: dict[str, None] = {}
            index = start
            while index < len(said):
                moment, at, other = said[index]
                index += 1
                if other == fact.fact_id:
                    continue  # 同一个编号传了两份的（写账不会这样传），和上面一样跳过
                if outside(fact.fact_id, moment, other):
                    waiting[other] = None
                    continue
                superseded[fact.fact_id] = (other, at)
                break
            else:
                conflicts.extend((fact.fact_id, other) for other in sorted(waiting))
            continue
        _, at, other = said[start]
        superseded[fact.fact_id] = (other, at)
    return Succession(superseded, conflicts)


# ---------- 关系（J4.4）：不碰账本的那一半，写账本的在 javmem.pipeline.write ----------
#
# 写账的单位是一处说法（relation_claims）：L3 在某个窗口抽出、过了 L4 的一条关系。关系（relations）是同一条关系、
# 同一段的几处说法合起来的（D115，和打分的合法一样），每一列、证据都从它的说法算出来，所以重写一个窗口、几个窗口
# 换个先后写，结果都一样。
#
# - 同一条关系（relation_identity）：可见范围、主体、关系、客体一样（D30：范围不同的不合并），亲属还要具体说法一样，
#   负责以后有了“代表哪一家”也算进去（D118）。
# - 判新旧的组（relation_group_key）：同一时间一个的只有托管于（一样东西同一时间放一家，按主体）、负责（一样东西同一时间
#   一个人管，按客体加代表哪一家，人和 Bot 一组；定义卡卡 2、卡 3，R12）、隶属于（一个部门、一家分公司同一时间只归一家，
#   按主体；卡 7，D210）。别的关系同一时间可以几个，一条关系自己一组。
# - 走一遍（relation_timeline）：一组里所有的说法按说的先后走：
#   - 写了结束的（标已结束，或者结束日子在说这句话那天或以前；还没到的到期日不算），这一段就到它为止，后来又说起是另一段
#     （卡 3 的 D101）；后来说起的开始日子不晚于前面某一段的结束，是又说了一遍那一段，归那一段；说的是现在这一段开始以前
#     就结束了的旧事，另起一段，不结束现在这一段；没写开始日子、又说它结束了的，结束日子的写法和前面已经结束的哪一段
#     一样（最近的那一段，冻结的不算），也是又说了一遍那一段，归那一段（J4.6，真调测试题 K25）。“开始以前”要现在这一段写了开始日子才看得出：没写的照说话的先后，
#     后说的结束就结束这一段（和 D153 第 1 条“有一句没写的照说话的先后”一样；打分的 D115 合法也是这样，先这样定）。
#   - 冻结着、没结束的（主人冻结的那一行上原来的说法，没有说法的冻结关系放进来的那一处）一直现行，和不被顶掉一样：
#     别人后来说它结束了，不结束它，说结束的那一处自己一段、进待确认（frozen_conflict）；它也不归到别人已经结束了的那一段
#     里去。别人又说起它（没写结束），照常接上。
#   - 同一时间一个的组，新开的一段碰上别人正管着：同一句话里说的（两处说法有同一句证据，不看 L3 写的先后，后面还没走到的
#     那一处也算），两个都留着、都进待确认（R12：A 或 B）；两边都写了开始日子、新的开始得更早的，只补历史，现在的不动
#     （D153 第 1 条）；正管着的冻结着，新的进待确认（D12），没冻结的照常被新的顶掉；别的照常顶掉：正管着的那段结束，结束
#     日子取新的那段的开始日子，没写的取说这句话那天、记成“约”（R37、5.10）。暂停着的也照样被顶掉（R36）。
# - 一段的各列（summarize_segment）：开始日子取有日子的里面最早的，同一天的取最准的；写了结束的那一处给结束日子，没有的
#   取最后说的那一处写的结束日子（还没到的到期日）；状态有结束的是已结束，不然取最后说的那一处的（生效、暂停）；暂停那天
#   取最后一段连着的暂停里最早那一处的（D153 第 3 条）；有一处核对过就是核对过；说话人、说法出自谁取最早说的那一处。
# - 编号：R 加 merge_key 的前 16 位，键是“同一条关系 + 这一段最早那一句证据”。一条关系的两段也可能从同一句开始（L3 在
#   一个窗口里把它给了两次，一处写了结束、一处没写）：先到的那段用这个键，后面的键里再加它最早那处说法的编号
#   （javmem.pipeline.write，编号不撞）。

RELATION_RULE_PREFIX = "L5-relation"  # 写关系的规则都以它开头：打分看规则表里有没有，认这本账是不是写关系的代码写的
L3_RELATION_STATES = {"生效": "active", "暂停": "paused", "已结束": "ended"}  # L3 的状态 → 账本的（query-api 4.2）
KINSHIP = "亲属"
# 同一时间一个的：按哪一头分组（定义卡卡 2、卡 3；隶属于是卡 7，D210：一个部门同一时间只归一家）
ONE_AT_A_TIME = {"托管于": "subject", "负责": "object", "隶属于": "subject"}
_PRECISION_RANK = {"minute": 5, "day": 4, "month": 3, "quarter": 2, "year": 1, "unknown": 0}
SHELL_PREFIX = "~"  # 走一遍时没有说法的冻结关系当一处“说法”放进去，编号是 ~ 加关系编号
DIRECTIVE_PREFIX = "!"  # 软变化判了暂停、结束的是同一组里别的那一条：照判的给它加一处“说法”（不存），编号是 ! 加挡住的那一处


def relation_identity(
    scope: Scope, subject_id: str, relation_type: str, object_id: str, *, kinship: str | None = None, org_id: str | None = None
) -> str:
    """同一条关系的键（不分段）。亲属的具体说法按比对写法比；不是亲属的不看 kinship。"""
    kin = key_text(kinship) if relation_type == KINSHIP else ""
    return _sha("javmem-relation-identity", scope_key(scope), subject_id, relation_type, object_id, kin, org_id or "")


def relation_group_key(
    scope: Scope, relation_type: str, subject_id: str, object_id: str, *, org_id: str | None = None, identity: str = ""
) -> str:
    """判新旧的组：同一时间一个的按卡上那一头（负责另加代表哪一家，看不出的是主人这边，D153 第 2 条）；别的关系一条自己一组
    （就是 identity）。软变化找旧的（javmem.soft.find）也按它找同一组。"""
    side = ONE_AT_A_TIME.get(relation_type)
    if side is None:
        return identity
    end = subject_id if side == "subject" else object_id
    return _sha("javmem-relation-group", scope_key(scope), relation_type, end, org_id or "" if relation_type == "负责" else "")


def relation_merge_key(identity: str, anchor: str) -> str:
    """一段的键：同一条关系 + 这一段最早那一句证据的句子编号。"""
    return _sha("javmem-relation", identity, anchor)


def relation_id_of(key: str) -> str:
    return "R" + key[:16]


def date_bounds(value: str | None) -> tuple[date, date] | None:
    """一个日子（ISO 写法，可能只到月、季、年）最早、最晚可能是哪一天。没写、认不出的是 None。"""
    parsed = parse_occurred(value) if value else None
    if parsed is None:
        return None
    try:
        if parsed.precision in ("minute", "day"):
            day = date(parsed.year, parsed.month or 1, parsed.day or 1)
            return day, day
        if parsed.precision == "month":
            month = parsed.month or 1
            first = date(parsed.year, month, 1)
            return first, date(parsed.year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        if parsed.precision == "quarter":
            quarter = parsed.quarter or 1
            first = date(parsed.year, 3 * quarter - 2, 1)
            return first, date(parsed.year + quarter // 4, (3 * quarter) % 12 + 1, 1) - timedelta(days=1)
        return date(parsed.year, 1, 1), date(parsed.year, 12, 31)
    except ValueError:
        return None


@dataclass(frozen=True)
class RelationClaimView:
    """算关系用的一处说法，从 relation_claims 读出来的。anchor 是它最早那一句证据的句子编号，said_day 是那一句说话那天。
    paused_at 是这一处说的暂停那天（只有暂停的才有）。"""

    claim_id: str
    state: str  # active、paused、ended
    valid_from: str | None
    valid_from_precision: str
    valid_from_basis: str | None
    valid_to: str | None
    valid_to_precision: str
    valid_to_basis: str | None
    status: str  # verified、inferred
    speaker: str
    speaker_role: str
    attributed_to: str | None
    said_from: str
    said_from_epoch: int
    said_to: str
    said_to_epoch: int
    anchor: str
    said_day: date
    notes: tuple[str, ...] = ()
    extracted_by: str = ""
    l4_version: str = ""
    paused_at: str | None = None
    paused_at_precision: str = "unknown"
    pause_condition: str | None = None
    sentences: tuple[str, ...] = ()  # 这一处说法的证据句（同一句话里两个的 R12 按它认）
    correction: str | None = None  # 主人在纠正（J4.6）：带纠正词的那一句的编号（javmem.pipeline.correct），不是的是 None

    @property
    def start(self) -> date | None:
        bounds = date_bounds(self.valid_from)
        return bounds[0] if bounds else None

    @property
    def end(self) -> date | None:
        bounds = date_bounds(self.valid_to)
        return bounds[1] if bounds else None

    @property
    def closes(self) -> bool:
        """这一处说的是这一段已经结束了：标了已结束，或者结束日子最晚也在说这句话那天或以前（还没到的到期日不算）。"""
        if self.state == "ended":
            return True
        end = self.end
        return end is not None and end <= self.said_day

    @property
    def shell(self) -> bool:
        """没有说法的冻结关系放进来的那一处（不是真的说法）。"""
        return self.claim_id.startswith(SHELL_PREFIX)

    @property
    def pseudo(self) -> bool:
        """不是真的说法：没有说法的冻结关系放进来的那一处、软变化判出来加给旧的那一处。"""
        return self.claim_id[:1] in (SHELL_PREFIX, DIRECTIVE_PREFIX)


def _said_order(claim: RelationClaimView) -> tuple[int, str, str]:
    return (claim.said_from_epoch, claim.anchor, claim.claim_id)


@dataclass
class TimelineSegment:
    """走一遍以后的一段。superseded_by 是顶掉它的那一段在列表里的下标；cut 是被顶掉时的结束日子
    （写法、精度、basis：新的那段没写开始日子的，取说那句话那天、记成 approx）。"""

    identity: str
    members: list[RelationClaimView] = field(default_factory=list)
    closed: bool = False
    end: date | None = None  # 它自己写的结束（最晚可能那天）
    start: date | None = None
    start_text: str | None = None
    start_precision: str = "unknown"
    superseded_by: int | None = None
    superseded_at: str | None = None
    cut: tuple[str | None, str, str | None] | None = None  # 写法是 None 的：被主人纠正掉，哪天结束的不知道（J4.6）
    pending: str | None = None  # other：同一时刻两个（R12）；frozen_conflict：正管着的冻结着，或者说冻结着的结束了
    conflict_with: int | None = None
    frozen: bool = False
    guarded: bool = False  # 段里有冻结着、没结束的那一行原来的说法：不被别人说的结束结束掉
    held: bool = False  # 软变化的候选先挡住的那一处，自己一段（J4.4 第 4 段）
    corrected_by: RelationClaimView | None = None  # 这一段是被主人纠正掉的（J4.6）：结束它、顶掉它的那一处纠正

    def take_start(self, claim: RelationClaimView) -> None:
        start = claim.start
        if start is None:
            return
        rank = _PRECISION_RANK.get(claim.valid_from_precision, 0)
        if self.start is None or start < self.start or (start == self.start and rank > _PRECISION_RANK.get(self.start_precision, 0)):
            self.start, self.start_text, self.start_precision = start, claim.valid_from, claim.valid_from_precision


def _cut(segment: TimelineSegment, claim: RelationClaimView, index: int) -> None:
    """正管着的这一段被新开的那一段（下标 index，从 claim 起）顶掉：结束日子取新的那段的开始日子，没写的取说这句话那天、
    记成“约”（R37、5.10；卡 1 的 approx）。主人纠正的（J4.6，主人 11-17 第 1 题 A）：说“不对”的那天不是换的那天，
    原话没写哪天就空着，这一段记着是被这一处纠正掉的。"""
    segment.closed = True
    before_start = (claim.correction is not None and claim.start is not None and segment.start is not None
                    and claim.start < segment.start)  # 纠正写的那天比这一段的开始还早：不写出结束早于开始的一段（第一轮审阅第 1 条）
    if claim.start is not None and not before_start:
        segment.cut = (str(claim.valid_from), claim.valid_from_precision, None)
    elif claim.correction is not None:
        segment.cut = (None, "unknown", None)
    else:
        segment.cut = (claim.said_day.isoformat(), "day", "approx")
    segment.superseded_by, segment.superseded_at = index, claim.said_from
    if claim.correction is not None:
        segment.corrected_by = claim


def _written_end(segment: TimelineSegment, claim: RelationClaimView) -> tuple[str | None, str, str | None]:
    """主人纠正时原话写了旧的哪天结束（“宓春生 10 月就不管了”）：照原话；没写、比这一段的开始还早的，空着。"""
    if claim.valid_to is None or (claim.end is not None and segment.start is not None and claim.end < segment.start):
        return (None, "unknown", None)
    return (claim.valid_to, claim.valid_to_precision, claim.valid_to_basis)


def relation_timeline(
    members: Mapping[str, Sequence[RelationClaimView]],
    *,
    one_at_a_time: bool = False,
    frozen_claims: Collection[str] = frozenset(),
    guarded_claims: Collection[str] = frozenset(),
    candidates: Collection[str] = frozenset(),
    holds: Collection[str] = frozenset(),
    forced: Collection[str] = frozenset(),
    finance: Collection[str] = frozenset(),
    cut_only: Collection[str] | None = None,
) -> list[TimelineSegment]:
    """一组的几条关系（identity → 它的说法）按说的先后走一遍（见上面）。frozen_claims：原来在冻结的那几行上的说法
    （没有说法的冻结关系放进来的那一处也算），它们在的那一段是冻结的，不被顶掉。guarded_claims：其中冻结着、没结束的
    那几行上的，它们在的那一段也不被别人说的结束结束掉。candidates：软变化的候选（还没判的、判了不改的），会改到组里
    以前说的（结束、暂停、又开始、换一家）的先挡住，自己一段（held），不碰别的段。holds：判断 C 拿不准的，不管改不改得到
    以前说的，都自己一段挡着（manual_review，主人说留了才放进来）。forced：软变化判了结束、放出来的那几处（判出来加给
    旧的那一处也是）：照判的结束现在这一段，不当成“开始以前就结束了的旧事”（判的那天早于开始的，是取消了还没开始的安排）。
    finance：财务投资的关系被主人纠正、主人还没说照改的那几处（J4.6，D7）：和软变化的候选一样，会改到以前说的才挡住，
    挡住的那一段是 manual_review。cut_only：同一时间一个的组里只有这几处能顶掉别人（J4.6：同一对人的几种亲属放在一起走，
    只有纠正顶掉别的那种；None 是谁都能顶）。说法的 correction 有的（主人在纠正）：被它结束、顶掉的那一段记着它
    （corrected_by），顶掉时原话没写哪天的结束日子空着；纠正不当“只补历史”“开始以前就结束了的旧事”，原话写的那天比
    旧那段的开始还早的，旧那段的结束日子也空着；纠正成正管着的那一条时，同组别的没冻结的那几条也结束（第一轮审阅）。
    返回的各段按段里最早说的那一处排。"""
    # 同一时刻的，没有说法的冻结关系先走（它的时刻是最后一次被说的时候；包重发、同一时刻改了说法的，是后来的话）
    events = sorted(((identity, claim) for identity, claims in members.items() for claim in claims),
                    key=lambda item: (item[1].said_from_epoch, not item[1].shell, *_said_order(item[1])[1:], item[0]))
    sentences_of: dict[str, set[str]] = {}  # 每条关系所有说法的证据句：同一句话里的那一处还没走到也认得出（R12）
    standing_of: dict[str, set[str]] = {}  # 其中不是说它结束了的那几处的证据句（纠正看它，J4.6）
    for identity, claims in members.items():
        for claim in claims:
            sentences_of.setdefault(identity, set()).update(claim.sentences)
            if not claim.closes:
                standing_of.setdefault(identity, set()).update(claim.sentences)
    segments: list[TimelineSegment] = []
    open_of: dict[str, int] = {}
    holders: list[int] = []

    def finished(segment: TimelineSegment) -> date | None:
        """一段结束的那天：它自己写的，或者被顶掉的那天。"""
        if segment.end is not None:
            return segment.end
        bounds = date_bounds(segment.cut[0]) if segment.cut is not None else None
        return bounds[1] if bounds else None

    def written_ends(segment: TimelineSegment) -> set[str]:
        """一段结束日子的写法（又说了一遍那一段的结束，写法一样才认，K25 审阅）：被顶掉的是顶掉那天（记成“约”的、纠正掉
        日子空着的不算），自己写了结束的是写那天的那几处说法的写法。"""
        if segment.cut is not None:
            return {segment.cut[0]} if segment.cut[0] is not None and segment.cut[2] != "approx" else set()
        stop = finished(segment)
        return {str(member.valid_to) for member in segment.members
                if member.closes and member.valid_to is not None and member.end == stop}

    def closed_home(identity: str, start: date | None) -> int | None:
        if start is None:
            return None
        return next((number for number, one in enumerate(segments) if one.identity == identity and one.closed
                     and not one.held and (stop := finished(one)) is not None and start <= stop), None)

    def join(segment: TimelineSegment, claim: RelationClaimView) -> None:
        segment.members.append(claim)
        segment.frozen |= claim.claim_id in frozen_claims
        segment.guarded |= claim.claim_id in guarded_claims

    def earlier_stint(identity: str, end: date | None, current: int | None) -> int | None:
        """主人纠正写的结束日子早于现在这一段的开始：这条关系更早、已经结束、开始不晚于这一天的最近那一段（纠正说的是它）。"""
        if end is None:
            return None
        return next((number for number in reversed(range(len(segments))) if number != current
                     and segments[number].identity == identity and segments[number].closed and not segments[number].held
                     and (segments[number].start is None or segments[number].start <= end)), None)

    def same_fix(one: RelationClaimView, other: RelationClaimView) -> bool:
        """两处是主人同一次纠正说的：有同一句证据，或者是同一句纠正词（同一条消息里分两句说的，第二轮审阅第 7 条）。"""
        return bool(set(one.sentences) & set(other.sentences)) or (one.correction is not None and one.correction == other.correction)

    for identity, claim in events:
        start, end = claim.start, claim.end
        current = open_of.get(identity)
        guarded = claim.claim_id in guarded_claims
        if claim.claim_id.startswith(DIRECTIVE_PREFIX) and (current is None or segments[current].guarded):
            continue  # 软变化判出来加给旧的那一处：旧的那时已经不现行了，或者是主人冻结着的，不改它
        if claim.claim_id in holds:  # 判断 C 拿不准的：自己一段挡着，等主人
            held = TimelineSegment(identity, closed=True, pending="manual_review", held=True)
            held.members.append(claim)
            held.take_start(claim)
            segments.append(held)
            continue
        if claim.claim_id in candidates or claim.claim_id in finance:
            # 软变化的候选（J4.4 第 4 段）：会改到组里这句话以前说的（结束、暂停或者又生效、结束以后又开始、换一家）先挡住，
            # 自己一段，不改旧的、不顶掉旧的，判了再照结论；组里以前没说过的、只是又说了一遍的、同一句话里说的（不是“以前”，
            # 和 javmem.soft.find 配旧的一样）照常。财务投资的纠正（J4.6）一样挡，挡住的是 manual_review，等主人说照改
            def earlier(one: TimelineSegment) -> bool:
                return min(member.said_from_epoch for member in one.members) < claim.said_from_epoch

            live = [one for one in segments if not one.held and earlier(one)]
            change = False
            # 亲属放在一起走时（cut_only），只有纠正那几处顶掉别的；软变化的候选照亲属原来的规矩（第一轮审阅第 14 条）
            one_way = one_at_a_time and (cut_only is None or claim.claim_id in cut_only)
            if live:
                if claim.closes:
                    old_story = (claim.correction is None and end is not None and current is not None  # 纠正不当旧事（审阅第 1 条）
                                 and segments[current].start is not None and end < segments[current].start)  # type: ignore[operator]
                    change = (current is not None and earlier(segments[current]) and closed_home(identity, start) is None
                              and not old_story)
                elif (None if guarded else closed_home(identity, start)) is None:
                    if current is not None:
                        change = earlier(segments[current]) and claim.state != segments[current].members[-1].state
                        if not change and claim.correction is not None and one_way:
                            # 纠正成正管着的这一条：会顶掉同组别的没冻结的那几条（第一轮审阅第 3 条）
                            change = any(segments[number].identity != identity and not segments[number].frozen
                                         and earlier(segments[number]) for number in holders)
                    else:
                        others_now = [number for number in holders
                                      if segments[number].identity != identity and earlier(segments[number])]
                        # 写了开始日子、比正管着的都早的，只补历史（D153 第 1 条），不改现在的：不挡（第三轮审阅 N4）。
                        # 主人纠正的不是补历史，是换掉（第一轮审阅第 1 条）
                        history = claim.correction is None and start is not None and bool(others_now) and all(
                            segments[number].start is not None and start < segments[number].start  # type: ignore[operator]
                            for number in others_now)
                        change = any(one.identity == identity for one in live) or (
                            one_way and bool(others_now) and not history)
            if change and claim.claim_id not in candidates:
                # 财务投资的纠正碰到冻结着的那一条：冻结的优先，照 J4.4 进“和冻结的冲突”，不再另挡一次（只问一次）；
                # 要改的是组里别的没冻结的，照挡（第一轮审阅第 7 条）
                touches_frozen = (current is not None and segments[current].guarded
                                  and (claim.closes or claim.state != segments[current].members[-1].state))
                if not claim.closes and one_way:
                    touches_frozen = touches_frozen or any(
                        segments[number].guarded for number in holders if segments[number].identity != identity)
                change = not touches_frozen
            if change:
                held = TimelineSegment(identity, closed=True, held=True,
                                       pending="soft_change" if claim.claim_id in candidates else "manual_review")
                held.members.append(claim)
                held.take_start(claim)
                segments.append(held)
                continue
        if claim.closes:
            # 判了结束的（放出来的、判出来加给旧的那一处）：结束现在这一段，不看是不是旧事、又说了一遍哪一段（第三轮审阅 S1）
            forced_end = claim.claim_id in forced or claim.claim_id.startswith(DIRECTIVE_PREFIX)
            home = None if forced_end else closed_home(identity, start)
            old_story = (end is not None and current is not None and segments[current].start is not None
                         and end < segments[current].start)  # type: ignore[operator]
            if claim.correction is not None and old_story and home is None:
                stint = earlier_stint(identity, end, current)
                if stint is not None:
                    # 纠正的是这条关系更早那一段的结束（“那次是去年 6 月就走了”）：改那一段，现在这一段照旧（第二轮审阅第 2 条）
                    segment = segments[stint]
                    join(segment, claim)
                    segment.cut, segment.end, segment.corrected_by = _written_end(segment, claim), end, claim
                    continue
            # 主人纠正的不当“开始以前就结束了的旧事”，照结束现在这一段办（第一轮审阅第 1 条）
            if home is None and current is not None and (forced_end or claim.correction is not None or not old_story):
                segment = segments[current]
                if segment.guarded and not guarded:
                    # 冻结着的一直现行（和不被顶掉一样）：别人说它结束了，不结束它；这一处自己一段，进待确认，主人定
                    new = TimelineSegment(identity, closed=True, end=end, pending="frozen_conflict", conflict_with=current)
                    join(new, claim)
                    new.take_start(claim)
                    segments.append(new)
                    continue
                join(segment, claim)
                segment.take_start(claim)
                segment.closed, segment.end = True, end
                if claim.correction is not None:  # 主人说它不对、早就没了（J4.6）：这一段记着是被这一处纠正掉的
                    segment.corrected_by = claim
                    if old_story:  # 写的结束早于这一段的开始（开始多半是 L3 照说话那天写的）：结束那天空着，不写出结束早于开始的一段
                        segment.cut, segment.end = (None, "unknown", None), None
                del open_of[identity]
                if current in holders:
                    holders.remove(current)
                continue
            if home is None and current is None:
                # 主人纠正的同一句话里，新的先走、已经顶掉了它（“是阚星野负责的，不是宓春生”，J4.6）：说它结束的这一处归被顶掉
                # 的那一段，不另起一段；原话写了它哪天结束的，照原话（第一轮审阅第 4 条）
                swapped = next((number for number in reversed(range(len(segments))) if segments[number].identity == identity
                                and (by := segments[number].corrected_by) is not None and by.correction is not None
                                and same_fix(claim, by)), None)
                if swapped is not None:
                    join(segments[swapped], claim)
                    if claim.valid_to is not None:
                        segments[swapped].cut = _written_end(segments[swapped], claim)
                    continue
            if home is None and not guarded and not forced_end and start is None and claim.valid_to is not None:
                # 又说了一遍已经结束了的那一段（真调测试题 K25：D83 回答那一包，Bot 问“你说……上个月就兑付了，去掉吗”、主人
                # “对，改吧”，L3 又抽成一处“已结束、到 10 月”，没写开始日子）：结束日子的写法和已经结束的那一段一样的，归那一段，
                # 不另起一段（J4.6）；几段都一样的归最近的那一段。冻结的那一段不归进去（没有说法的冻结关系会照新说法重算），
                # 写法不一样（写到哪一天的和只写到哪个月的）不算（K25 审阅）
                home = next((number for number in reversed(range(len(segments))) if segments[number].identity == identity
                             and segments[number].closed and not segments[number].held and not segments[number].frozen
                             and claim.valid_to in written_ends(segments[number])), None)
            if home is None:  # 现在这一段开始以前就结束了的旧事（或者这条关系第一次被说起就是已经结束了的）
                segment = TimelineSegment(identity, closed=True, end=end)
                join(segment, claim)
                segment.take_start(claim)
                segments.append(segment)
                continue
            join(segments[home], claim)
            continue
        # 先看是不是又说了一遍前面已经结束了的那一段（和打分的合法一样）；冻结着、没结束的不归到已经结束了的那一段里去
        home = None if guarded else closed_home(identity, start)
        if home is not None:
            join(segments[home], claim)
            continue
        if current is not None:
            segment = segments[current]
            join(segment, claim)
            segment.take_start(claim)
            if claim.correction is not None and one_at_a_time and (cut_only is None or claim.claim_id in cut_only):
                # 纠正成正管着的这一条（两种亲属原来都现行、负责两边都留过、同一句话里两个）：同组以前就现行的、没冻结的那几条
                # 结束，记着被纠正（第一轮审阅第 3 条）。冻结的照旧；和冻结的在冲突里的照旧等主人定；和这一处同一句来的
                # （R12 的另一半，“是阚星野负责，宓春生也在帮忙”）不结束，照 R12 两个都问（第二轮审阅第 1、6 条）
                mine = set(claim.sentences)
                beaten = [number for number in holders if segments[number].identity != identity and not segments[number].frozen
                          and segments[number].pending != "frozen_conflict"
                          and not mine & {sentence for member in segments[number].members for sentence in member.sentences}]
                for number in beaten:
                    _cut(segments[number], claim, current)
                    open_of.pop(segments[number].identity, None)
                    if segments[number].pending == "other":
                        segments[number].pending = segments[number].conflict_with = None
                if beaten:
                    holders = [number for number in holders if number not in beaten]
                    if segment.pending == "other" and segment.conflict_with in beaten:
                        segment.pending = segment.conflict_with = None
            continue
        index = len(segments)
        new = TimelineSegment(identity, frozen=claim.claim_id in frozen_claims, guarded=guarded)
        new.members.append(claim)
        new.take_start(claim)
        segments.append(new)
        open_of[identity] = index
        if claim.correction is not None and one_at_a_time:
            # 同一句纠正里先说了旧的哪天结束、再说新的（“宓春生 10 月就不管了，现在是阚星野”）：旧的那一段指着新的，
            # 和新的先走时一样（第一轮审阅第 4 条）
            for other in segments[:index]:
                by = other.corrected_by
                if (other.identity != identity and other.closed and other.superseded_by is None and by is not None
                        and by.closes and same_fix(by, claim)):
                    other.superseded_by, other.superseded_at = index, claim.said_from
                    other.cut = other.cut or _written_end(other, by)
        if not one_at_a_time:
            continue
        others = [number for number in holders if segments[number].identity != identity]
        if not others:
            holders = [number for number in holders if segments[number].identity == identity] + [index]
            continue
        if cut_only is not None and claim.claim_id not in cut_only:  # 亲属：不是纠正的，几种照旧都留着（J4.6）
            holders.append(index)
            continue
        if new.frozen:  # 冻结的（主人定的）不被顶掉、也不当历史：顶掉没冻结的；两条都冻结的都现行（和事实一样）
            for number in others:
                if not segments[number].frozen:
                    _cut(segments[number], claim, index)
                    open_of.pop(segments[number].identity, None)
            holders = [number for number in holders if segments[number].frozen] + [index]
            continue
        # R12：同一句话里两个，分不出是“A 或 B”还是“A 和 B 一起”：都留着，都进待确认（按证据句认，不看先后，第二轮审阅 S4）。
        # 主人纠正的那一句（J4.6）只看别的那条说它现在还在的几处：同一句里说旧的已结束、新的生效，是换掉，不是 A 或 B
        mine = set(claim.sentences)
        pool = standing_of if claim.correction is not None else sentences_of
        same_moment = [number for number in others if not segments[number].frozen
                       and mine & pool.get(segments[number].identity, set())]
        if same_moment:
            new.pending, new.conflict_with = "other", same_moment[0]
            for number in same_moment:
                segments[number].pending, segments[number].conflict_with = "other", index
            holders.append(index)
            continue
        earlier = start is not None and all(segments[number].start is not None and start < segments[number].start  # type: ignore[operator]
                                            for number in others)
        if earlier and claim.correction is None:  # D153 第 1 条：后说的讲的是更早的事，只补历史；主人纠正的是换掉（审阅第 1 条）
            first = min(others, key=lambda number: segments[number].start)  # type: ignore[arg-type,return-value]
            new.closed = True
            new.cut = (str(segments[first].start_text), segments[first].start_precision, None)
            new.superseded_by, new.superseded_at = first, claim.said_from
            del open_of[identity]
            continue
        frozen = [number for number in others if segments[number].frozen]
        if frozen:  # 正管着的冻结着：不顶掉，新的进待确认（frozen_conflict）；没冻结的照常被新的顶掉（和事实一样，第二轮审阅 S3）
            new.pending, new.conflict_with = "frozen_conflict", frozen[0]
            for number in others:
                if not segments[number].frozen:
                    _cut(segments[number], claim, index)
                    open_of.pop(segments[number].identity, None)
            holders = frozen + [index]
            continue
        for number in others:
            _cut(segments[number], claim, index)
            open_of.pop(segments[number].identity, None)
        holders = [index]
    order = sorted(range(len(segments)), key=lambda number: min(_said_order(claim) for claim in segments[number].members))
    position = {old: new for new, old in enumerate(order)}
    result = [segments[number] for number in order]
    for segment in result:
        if segment.superseded_by is not None:
            segment.superseded_by = position[segment.superseded_by]
        if segment.conflict_with is not None:
            segment.conflict_with = position[segment.conflict_with]
    return result


def relation_segments(claims: Sequence[RelationClaimView]) -> list[list[RelationClaimView]]:
    """一条关系自己的几处说法分段（不看组里别人）。"""
    return [segment.members for segment in relation_timeline({"": claims})]


@dataclass(frozen=True)
class RelationSummary:
    """一段算出来的各列。"""

    anchor: str
    valid_from: str | None
    valid_from_precision: str
    valid_from_basis: str | None
    valid_to: str | None
    valid_to_precision: str
    valid_to_basis: str | None
    state: str
    status: str
    speaker: str
    speaker_role: str
    attributed_to: str | None
    as_of: str
    as_of_epoch: int
    notes: tuple[str, ...]
    extracted_by: str
    l4_version: str
    closed: bool  # 这一段写了结束
    paused_at: str | None = None
    paused_at_precision: str = "unknown"
    pause_condition: str | None = None


def summarize_segment(segment: Sequence[RelationClaimView]) -> RelationSummary:
    """一段的各列（见上面）。没有说法的冻结关系放进来的那一处不算（它自己的各列不动）。软变化判出来加给旧的那一处只算
    状态、日子（暂停、结束），说话人、最后说到、核对过没有只看真的说法（第三轮审阅小的第 1 条）。"""
    stateful = [claim for claim in segment if not claim.shell] or list(segment)
    real = [claim for claim in stateful if not claim.pseudo] or stateful
    ordered = sorted(stateful, key=_said_order)
    first = min(real, key=_said_order)
    last = max(real, key=lambda claim: (claim.said_to_epoch, claim.claim_id))
    dated = [claim for claim in ordered if claim.start is not None]
    best = min(dated, key=lambda claim: (claim.start, -_PRECISION_RANK.get(claim.valid_from_precision, 0),
                                         *_said_order(claim))) if dated else None
    closing = next((claim for claim in reversed(ordered) if claim.closes), None)
    ending = closing if closing is not None else next((claim for claim in reversed(ordered) if claim.valid_to), None)
    state = "ended" if closing is not None else ordered[-1].state  # 标了已结束的那一处自己就 closes
    paused = None
    if state == "paused":  # 最后一段连着的暂停里最早那一处（D153 第 3 条：暂停从那天算）
        for claim in reversed(ordered):
            if claim.state != "paused":
                break
            paused = claim
    anchor = min((claim.said_from_epoch, claim.anchor) for claim in real)[1]
    return RelationSummary(
        anchor=anchor,
        valid_from=best.valid_from if best else None,
        valid_from_precision=best.valid_from_precision if best else "unknown",
        valid_from_basis=best.valid_from_basis if best else None,
        valid_to=ending.valid_to if ending else None,
        valid_to_precision=ending.valid_to_precision if ending and ending.valid_to else "unknown",
        valid_to_basis=ending.valid_to_basis if ending and ending.valid_to else None,
        state=state,
        status="verified" if any(claim.status == "verified" for claim in real) else "inferred",
        speaker=first.speaker,
        speaker_role=first.speaker_role,
        attributed_to=first.attributed_to,
        as_of=last.said_to,
        as_of_epoch=last.said_to_epoch,
        notes=tuple(sorted({note for claim in real for note in claim.notes})),
        extracted_by=first.extracted_by,
        l4_version=first.l4_version,
        closed=closing is not None,
        paused_at=paused.paused_at if paused else None,
        paused_at_precision=paused.paused_at_precision if paused and paused.paused_at else "unknown",
        pause_condition=paused.pause_condition if paused else None,
    )
