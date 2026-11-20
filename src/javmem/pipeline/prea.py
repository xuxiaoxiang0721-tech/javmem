"""预处理 A：相对的日子换成具体的日子（L3 之前；导航 D127、D129、D150，报告《预处理A》）。

L2 切好窗口以后、L3 抽之前，用规则把“昨天”“上周三”“下个月 3 号”这类相对的说法，照那句话自己的时间抬头换算成具体的
日子，写成〔日子：说法 = 2026-10-13〕，放在那一行编号后面（已有的〔引用〕这类后面），交给 L3 参考。原文一个字不改，
证据照旧只抄原文。不调模型：规则换不了、拿不准的不标。L3 的提示词选了带 A 那一句的（extract.PRE_A_PROMPTS：l3-v20、
l3-v21）才做，选别的不做、也不加。

标哪些（a2）：

- 只看能当证据的 T 行里新写的话（NEW）。附件的正文、转发的、引的旧信、邮件头、签名不标：写的时间不一定是抬头上的时间。
  对话里转述的、外部的（[转发 自 某人]、[外部:网页] 这种，origin 不是 said）也不标；邮件新写的正文照这封邮件的时间标。
- 天：昨天、前天、大前天、明天、后天、大后天（昨晚、明早这些也算），英文的 yesterday、tomorrow。
- 星期几：上周三、下周三、这周三、本周日（星期、礼拜一样）。一周从周一算到周日，和 L4 的 G6 一样：
  周五说“下周三”是 5 天以后，周一说“下周三”是 9 天以后，周日说“下周三”是 3 天以后。
- 月：上个月、下个月（写到月）；带几号的写到日（下个月 3 号）；带底、初、月中、上中下旬的写成“2026-09 月底”这样。
- 年：去年、前年、明年、后年（写到年）；带月、日的写到月、日（去年 3 月、明年 1 月 5 号）；带底、初的写成“2025 年底”。
- 最后过 L4 这一关：算出来的日子拿 L4 的 G6 自己的算法（verify.sentence_times）对这一句再算一遍，L4 算不出的不标
  （上上周三、下下个月、昨儿、下月三号、大前年、the day before yesterday……）：模型照着写了日子，L4 核不上会挡掉事实。

不标的（存下来的 detail 里记着为什么）：

- 跟抬头同一天、同一个月、同一年、同一个季度的：今天、今晚、本月、本月底、今年、本季度（抬头上就写着）。这个月、本月带了几号
  的照标（本月 20 号）。
- 光写星期几（周三、星期五、每周日）：分不清是哪一周，也常是“每周日”这种。只说了哪一周、周末（上周、下周、上周末）：不是一天。
- 当天、当日、次日、第二天、前一天：说的是别的那一天，不是说话那天。
- 只写了几号（26 号）、几天前、几个月后：不知道是哪个月、从哪天算。光写月底、年底：没说哪个月、哪一年。
- 季度（上个季度、下个季度）：算得出日历上的季度，可是投资聊天里多半说的是公司的财季，不标。
- 半夜（0 点到 NIGHT_END 点以前）说的：拿这一天和前一天各算一遍，不一样的不标（半夜说的“明天”可能是睡醒以后的今天）。
- 紧挨着就写了日子、星期几的（“明天 8/25”“下周二（25 号）”“明天周二”）：日子一样的不用标；日子、星期几对不上的不标，记下来。
- 前面一个字和“上”“下”“本”连成词的（马上、一下、早上、日本……），后面跟着量词、连成别的词的（这周三天、这周一定、
  上周日本、上周三星级、说明天数、以后天天、雨后天晴、减去年化、税后年化）：拿不准，不标。“记一下下周四”是“一下”加“下周四”，
  照“下周四”标。尾巴连成词的去掉尾巴再认：明年初中毕业是明年，上个月底仓是上个月，下个月 5 号线是下个月。
- 转述里的：“昨天他说明天发货”“10/12 他说明天到”，说的人在另一天说的，“明天”照哪天算拿不准（再说、据说、听说不算转述）。
- 算出来的日子不存在的（下个月 31 号、明年 2 月 29 号）。

存：L3 结果库的 prea_notes（javmem.pipeline.store），按（窗口编号、没加标注的窗口文字的指纹、版本）存，连同这个窗口有哪些包、
句子（撤回时按内容删），和认出来没标的那些、为什么；只存说法、日子、行号、抬头时间，不存原文的句子。存过的不再算，也不改
（先存的为准）；规则改了要换版本号。标注不进账本。写窗口时先加 A、再加 B（extract.prepare），核对、写账、合并工具算出来的
键和抽的时候一样。A 只用这个窗口自己的字和抬头，不用别的窗口学来的东西：不按可见范围分，撤回时也不连带别的窗口。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from javmem.packet.model import ORIGIN_SAID
from javmem.pipeline.normalize import ATTACHMENT, FORWARDED, HEADER, NEW, QUOTED_ONLY, SIGNATURE_INFO
from javmem.pipeline.render import RenderedWindow, render_window
from javmem.pipeline.window import Window, WindowBlock, WindowLine
from javmem.redaction import RedactionPattern
from javmem.timeutil import iso
from javmem.zh.numbers import cn_number

if TYPE_CHECKING:
    from javmem.pipeline.store import ResultStore

PRE_A_VERSION = "a2"
NOTE_LABEL = "日子"
NIGHT_END = 5  # 0 点到这个钟点以前说的，拿这一天和前一天各算一遍
MAX_PER_LINE = 6  # 一行最多标几处

_ESCAPE = str.maketrans({"〔": "［", "〕": "］", "\n": " ", "\r": " "})
_SP = "[ 　]?"
_NUM = r"\d{1,2}|[一二三四五六七八九十]{1,3}"
_PREFIX = "上上个?|下下个?|上个?|下个?|这个?|本"

# 一个大的正则，几种写法从具体到笼统排：同一个地方先认长的（“下个月 3 号”不会只认成“下个月”）
_PATTERN = re.compile(
    "|".join(
        (
            r"(?P<en_day>(?<![A-Za-z])(?ai:the day before yesterday|the day after tomorrow|yesterday|tomorrow)(?![A-Za-z]))",
            r"(?P<en_same>(?<![A-Za-z])(?ai:today|tonight)(?![A-Za-z]))",
            r"(?P<en_span>(?<![A-Za-z])(?ai:(?P<en_rel>last|next|this) (?P<en_unit>week|month|year))(?![A-Za-z]))",
            rf"(?P<yw>大前年|大后年|前年|去年|今年|明年|后年)(?:{_SP}(?:的{_SP})?(?:(?P<ym>{_NUM}){_SP}月份?(?:{_SP}(?:(?P<yd>{_NUM}){_SP}[号日]"
            rf"|(?P<ymedge>月?底|月?末|月?初|中旬|上旬|下旬)))?|(?P<yedge>年底|年末|年初|年中|上半年|下半年|底|末|初)))?",
            rf"(?P<qp>{_PREFIX}){_SP}季度",
            rf"(?P<wp>{_PREFIX}){_SP}(?:周|星期|礼拜){_SP}(?P<wd>[一二三四五六日天])",
            rf"(?P<mp>{_PREFIX}){_SP}月份?(?:{_SP}(?:的{_SP})?(?:(?P<md>{_NUM}){_SP}[号日]"
            rf"|(?P<medge>月底|月末|月初|月中|中旬|上旬|下旬|底|末|初)))?",
            rf"(?P<wkp>{_PREFIX}){_SP}(?:周|星期|礼拜)(?P<wkend>末)?",
            r"(?P<day>大前天|大后天|前天|昨天|昨日|昨晚|昨夜|昨儿|明天|明日|明早|明晚|明儿|后天)",
            r"(?P<same>今天|今日|今早|今晚|今晨|今夜|今儿)",
            r"(?P<other>当天|当日|次日|翌日|隔天|第二天|前一天|后一天)",
            rf"(?P<bare>(?:周|星期|礼拜){_SP}[一二三四五六日天])",
            r"(?P<weekend>周末)",
            r"(?P<bare_medge>(?<![\d一二三四五六七八九十])(?<![\d一二三四五六七八九十][ 　])月(?:底|末|初|中))",
            r"(?P<bare_yedge>(?<![\d一二三四五六七八九十两几半财学])(?<!\d[ 　])年(?:底|末|初))",
            r"(?P<ago>(?<![\d月])(?<!月[ 　])(?:\d{1,3}|[一二两三四五六七八九十半几]{1,3})[ 　]?个?[ 　]?"
            r"(?P<ago_unit>天|日|周|星期|礼拜|月|年)[ 　]?(?:以前|以后|之前|之后|前|后))",
            r"(?P<dayonly>(?<![\d./:\-月])(?<!月[ 　])(?<!\d[ 　])(?P<dn>\d{1,2})[ 　]?[号日]"
            r"(?![A-Za-z0-9元圆币楼机线位馆门房厅桌车台柜窗口仓库箱店床码件文人期内常均报志记程子间]))",
        )
    )
)

_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_SHIFT = {"上上": -2, "上": -1, "下下": 2, "下": 1, "这": 0, "本": 0}  # 周、月、季度的前缀，往前往后几个
_DAYS = {"大前天": -3, "前天": -2, "昨天": -1, "昨日": -1, "昨晚": -1, "昨夜": -1, "昨儿": -1,
         "明天": 1, "明日": 1, "明早": 1, "明晚": 1, "明儿": 1, "后天": 2, "大后天": 3}
_EN_DAYS = {"the day before yesterday": -2, "yesterday": -1, "tomorrow": 1, "the day after tomorrow": 2}
_YEARS = {"大前年": -3, "前年": -2, "去年": -1, "今年": 0, "明年": 1, "后年": 2, "大后年": 3}
_EN_SHIFT = {"last": -1, "next": 1, "this": 0}
_MONTH_EDGE = {"底": "月底", "末": "月底", "月底": "月底", "月末": "月底", "初": "月初", "月初": "月初",
               "中": "月中", "月中": "月中", "上旬": "上旬", "中旬": "中旬", "下旬": "下旬"}
_YEAR_EDGE = {"底": "年底", "末": "年底", "年底": "年底", "年末": "年底", "初": "年初", "年初": "年初",
              "年中": "年中", "上半年": "上半年", "下半年": "下半年"}

# 前面一个字和“上”“下”“本”连成词的：马上周一、一下周三、日本周日、成本月报……这几个字后面的不标。
# 跟、比、看、用、按、在、等不放进来：“跟上周三比”“按下周三的安排”“在下周三之前”里是日子
_UP_BEFORE = frozenset("马早晚以之线网身手路楼地加算赶碰遇写填标贴带配补换穿关锁合爱考排摆搭凑轮挂附送递交列印盖扣套绑连追登冲顶台场面桌床车船岸山海街墙头背纸书世站")
_DOWN_BEFORE = frozenset("一以之楼地底私当乡剩留记写放定拿买签压落坐躺停扔丢撂搁降沉掉手部眼目上往向朝南北城林树窗桌床灯脚膝阁殿陛名麾门檐山台线卸吞咽抄存攒省拖推撤卧跪蹲趴立")
_BEN_BEFORE = frozenset("日基成资版根样课剧账笔书文范脚读台原副抄画标蓝底拓话绘稿口手记简刊译正校存合摹孤善珍残足够赔保亏回还老血工")
# 星期几后面跟着这些的是数、连成了别的词：这周三天、上周五笔、这周一定、下周日程、下周天气。
# 股、分、时、前、夜、出不放进来：“上周五股价”“下周三分红”“下周三时”“下周日前”“周日夜里”“下周日出发”里是日子
_COUNT_AFTER = frozenset("次回遍趟个天年月份批项张只条笔家名位件套倍成层级号万千百十亿季")
_SUNDAY_AFTER = frozenset("均内常程期报志记线间子后历化益里落天本元经股韩方企系产立用")
_WEEKDAY_AFTER = {
    "一": _COUNT_AFTER | frozenset("直共样般些点致定律半旦同切连概线手度向再周汽"),
    "二": _COUNT_AFTER | frozenset("手"),
    "三": _COUNT_AFTER | frozenset("星菱井峡国"),  # 上周三星级……（三亚看后面第二个字，见 _not_weekday）
    "四": _COUNT_AFTER | frozenset("川"),
    "五": _COUNT_AFTER | frozenset("粮星矿菱险"),  # 这周五险一金、上周五星级（五金、五道看后面第二个字）
    "六": _COUNT_AFTER | frozenset("福"),
    "日": _SUNDAY_AFTER,
    "天": _SUNDAY_AFTER | frozenset("气数然空使堂生才价赋文"),
}
# “说明天数”“表明年度”：前一个字和“明”连成词、后一个字和“天”“年”连成词
_MING_BEFORE = frozenset("说表证声标注阐查发光文透英聪鲜分简辨失清严申详开精高")
_MING_AFTER = {"明天": frozenset("数气然空地使堂生才价线赋文"), "明日": frozenset("期志记历常"), "明早": frozenset("期"),
               "明年": frozenset("龄份度纪轻级华限内")}
_HOU_BEFORE = frozenset("以之然最此往落背先今前明雨饭课随事午日战婚产术节赛盘班")  # 雨后天晴、盘后、下班后
_QIAN_BEFORE = frozenset("之以提目眼当面事空史年")  # 跟不放进来：“跟前天比”；年前天气
# 后面一个字连成了别的词的（frozenset：空字符串不算在里面）
_HOUNIAN_AFTER = frozenset("纪龄")
_QU_BEFORE = frozenset("减除失过离死出进回上下拿扣省抹删略划")  # 和“去”连成词的：减去、除去、失去、过去……
_YUE_AFTER = frozenset("饼亮球光牙色台嫂老线")
_HOUTIAN_AFTER = frozenset("天性养形")
_QIANTIAN_AFTER = frozenset("天数线气")
# 紧挨着写的日子、星期几（“明天 8/25”“8/25（明天）”“下周二（25 号）”“明天周二”）
_BESIDE_AFTER = re.compile(
    r"[ 　]?[（(：:，,]?[ 　]?"
    r"(?:(?P<y>\d{4})[ 　]?[-/.年][ 　]?(?P<m>\d{1,2})[ 　]?[-/.月][ 　]?(?P<d>\d{1,2})(?!\d)"
    r"|(?P<m2>\d{1,2})[ 　]?(?:/|月[ 　]?)(?P<d2>\d{1,2})(?![\d.])"
    r"|(?P<m4>\d{1,2})-(?P<d4>\d{1,2})(?![\d.\-%点时])(?![ 　]?[点时])"
    r"|(?P<d3>\d{1,2})[ 　]?[号日](?![A-Za-z0-9])"
    r"|(?:周|星期|礼拜)(?P<w>[一二三四五六日天]))"
)
_BESIDE_BEFORE = re.compile(
    r"(?:(?P<y>\d{4})[ 　]?[-/.年][ 　]?(?P<m>\d{1,2})[ 　]?[-/.月][ 　]?(?P<d>\d{1,2})[ 　]?[日号]?"
    r"|(?<![\d./])(?P<m2>\d{1,2})[ 　]?(?:/|月[ 　]?)(?P<d2>\d{1,2})[ 　]?[日号]?)"
    r"[ 　]?[（(]?[ 　]?$"
)
_MONTH_BESIDE_AFTER = re.compile(r"[ 　]?[（(]?[ 　]?(?:(?P<y>\d{4})[ 　]?年[ 　]?)?(?P<m>\d{1,2})[ 　]?月(?![ 　]?\d)")
_NOT_WEEK = frozenset("期年岁边围转全到密折刊")  # 周期、周年、周岁……不是“哪一周”
_CN_DIGITS = frozenset("一二三四五六七八九十两")
# 星期几后面跟着数：只有数后面跟着“点”“时”的才是钟点（下周三两点、十点），别的是数（上周三十万、三四五个）
_CLOCK = re.compile(r"[一二三四五六七八九十两]{1,3}[点时]")
_ASIA = frozenset("洲太")  # 下周三亚洲、亚太：不是三亚
_HARDWARE = frozenset("店件厂行城")  # 上周五金店：是五金
# “会后天”：会前面是这些的才是“会 + 后天”（我会后天联系你），别的是“会后 + 天”（开完会后天黑了、散会后天津）
_WILL_SUBJECT = frozenset("我你他她它们咱也还就都才再又")
# 底、初、末后面又连成了词的，不当月底、年初：明年初中毕业、上个月底仓、末班车
_EDGE_WORDS = {"初": frozenset("中一二三步次期级心始稿审选赛值价"), "底": frozenset("仓价线薪牌部层下座盘气细子稿"),
               "末": frozenset("班尾日端期位")}
# 几号后面跟着这些的不是日子：5 号线、3 号楼（和 dayonly 的一样）
_NOT_DATE_AFTER = frozenset("元圆币楼机线位馆门房厅桌车台柜窗口仓库箱店床码件文人期内常均报志记程子间")
# 转述：前面有别的日子、又紧跟着“说”“告诉”这类的（昨天他说明天发货），“明天”照哪天算拿不准。
# 再说、别说、据说、听说、对我来说、小说、名称这些不是“某人说”
_REPORTED = re.compile(r"(?:(?<![再别不就虽据听来话小难好要且])说|讲|(?<![名简号职统昵俗尊爱])称|表示|提到|告诉|通知|回复|答复|写道|写着|问)(?:过|了|道|的)?[ 　]?[，,：:]?[ 　]?(?:[我你他她它们]{1,2}[ 　]?)?$")
_EXPLICIT_DATE = re.compile(r"\d{1,2}[ 　]?[/月][ 　]?\d{1,2}|\d{4}-\d{1,2}-\d{1,2}")
L4_UNSURE = "L4 认不出这个写法：照着写日子会被 L4 挡"
REPORTED = "别人在另一天说的话里的日子：拿不准照哪天算"


@dataclass(frozen=True)
class Found:
    """一句里认出来的一处。skip 是空的就标，不是空的写着为什么不标。"""

    start: int
    end: int
    text: str  # 窗口里写出来的那几个字
    value: str  # 换算出来的：2026-10-13、2026-09、2025、2026-Q3、2026-09 月底；换不了的是空的
    kind: str  # 哪一种说法，统计用
    skip: str = ""


def _clean(text: str) -> str:
    """写进标注的：〔〕换成［］、换行换成空格，伪造不了别的标注和行。"""
    return str(text).translate(_ESCAPE).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return iso(datetime.now(timezone.utc).astimezone())


def _number(text: str | None) -> int | None:
    if not text:
        return None
    return int(text) if text.isdigit() else cn_number(text)


def _day(year: int, month: int, number: int | None) -> date | None:
    try:
        return date(year, month, number) if number is not None else None
    except (ValueError, OverflowError):
        return None


def _add_days(day: date, days: int) -> date | None:
    try:
        return day + timedelta(days=days)
    except OverflowError:
        return None


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def _prefix(raw: str) -> str:
    return raw.replace("个", "")


def _compound(text: str, start: int, prefix: str) -> bool:
    """前缀“上”“下”“本”和前面一个字连成了词（马上、一下、日本）。"""
    before = text[start - 1] if start > 0 else ""
    head = prefix[:1]
    if head == "上":
        return before in _UP_BEFORE
    if head == "下":
        return before in _DOWN_BEFORE
    if head == "本":
        return before in _BEN_BEFORE
    return False


def _not_weekday(text: str, end: int, weekday: str) -> bool:
    """星期几那个字后面连着量词、连成了别的词：这周三天、这周一定、下周日程、三分之一。“一起”只有“起息”算星期一。"""
    after, two = text[end : end + 1], text[end : end + 2]
    if weekday == "一" and after == "起":
        return two != "起息"
    if weekday not in "日天" and after in _CN_DIGITS:  # 上周三十万、三四五个是数；下周三两点、十点是钟点，照标
        return _CLOCK.match(text, end) is None
    third = text[end + 1 : end + 2]
    if weekday == "三" and after == "亚":  # 上周三亚的房子（三亚）挡；下周三亚洲开盘、亚太照标
        return third not in _ASIA
    if weekday == "五" and after == "金":  # 上周五金店（五金）挡；上周五金价照标
        return third in _HARDWARE
    if weekday == "五" and after == "道":  # 下周五道口（五道口）挡；上周五道路施工照标
        return third == "口"
    return after in _WEEKDAY_AFTER.get(weekday, _COUNT_AFTER) or two == "分之"


# 大正则里每一种写法的组名，按排的先后。match.lastgroup 是最后闭合的那个组（“下周三”是 wd），不能拿它认是哪一种
_KINDS = ("en_day", "en_same", "en_span", "yw", "qp", "wp", "mp", "wkp", "day", "same", "other", "bare", "weekend",
          "bare_medge", "bare_yedge", "ago", "dayonly")


def _kind_of(match: re.Match[str]) -> str:
    return next(name for name in _KINDS if match.group(name) is not None)


def _value_of(match: re.Match[str], text: str, day0: date) -> tuple[str, str, str]:
    """一处的 (日子, 种类, 不标的原因)。"""
    group = _kind_of(match)
    start, end = match.span()
    after = text[end] if end < len(text) else ""
    before = text[start - 1] if start > 0 else ""
    if group == "en_day":
        day = _add_days(day0, _EN_DAYS[match.group(0).lower()])
        return (day.isoformat() if day else "", "天（英文）", "" if day else "日子不存在")
    if group == "en_same":
        return "", "当天（英文）", "跟抬头同一天"
    if group == "en_span":
        shift, unit = _EN_SHIFT[match["en_rel"].lower()], match["en_unit"].lower()
        if unit == "week":
            return "", "一周（英文）", "只说了哪一周，不是一天"
        if shift == 0:
            return "", f"{'月' if unit == 'month' else '年'}（英文）", "跟抬头同一个月" if unit == "month" else "跟抬头同一年"
        if unit == "month":
            year, month = _add_months(day0.year, day0.month, shift)
            return f"{year:04d}-{month:02d}", "月（英文）", ""
        return f"{day0.year + shift:04d}", "年（英文）", ""
    if group == "yw":
        word = match["yw"]
        shift = _YEARS[word]
        year = day0.year + shift
        if text[start + 2 : start + 3] == "化":  # 减去年化、税后年化、说明年化：“年”是“年化”的
            return "", "年", "前后连成了别的词"
        if word == "去年" and before in _QU_BEFORE:  # 除去年费、失去年轻人
            return "", "年", "前后连成了别的词"
        if word == "明年" and before in _MING_BEFORE and after in _MING_AFTER["明年"]:
            return "", "年", "前后连成了别的词"
        if word == "后年" and (before in _HOU_BEFORE or after in _HOUNIAN_AFTER):
            return "", "年", "前后连成了别的词"
        if word == "前年" and (before in _QIAN_BEFORE or after == "度"):
            return "", "年", "前后连成了别的词"
        if match["ym"]:
            month = _number(match["ym"])
            kind = "年和月日" if match["yd"] else "年和月"
            if shift == 0:
                return "", kind, "跟抬头同一年"
            if not month or not 1 <= month <= 12:
                return "", kind, "日子不存在"
            if match["yd"]:
                day = _day(year, month, _number(match["yd"]))
                return (day.isoformat() if day else "", kind, "" if day else "日子不存在")
            if match["ymedge"]:
                return f"{year:04d}-{month:02d} {_MONTH_EDGE[match['ymedge']]}", "年和月", ""
            return f"{year:04d}-{month:02d}", kind, ""
        if match["yedge"]:
            if shift == 0:
                return "", "年头年底", "跟抬头同一年"
            return f"{year:04d} {_YEAR_EDGE[match['yedge']]}", "年头年底", ""
        if shift == 0:
            return "", "年", "跟抬头同一年"
        return f"{year:04d}", "年", ""
    if group == "qp":
        prefix = _prefix(match["qp"])
        if _compound(text, start, prefix):
            return "", "季度", "前面一个字和上、下、本连成了词"
        if _SHIFT[prefix] == 0:
            return "", "季度", "跟抬头同一个季度"
        index = day0.year * 4 + (day0.month - 1) // 3 + _SHIFT[prefix]
        # 算得出日历上的哪个季度，可是投资聊天里“下个季度的数字”多半是公司的财季：拿不准，不标（detail 里留着算出来的）
        return f"{index // 4:04d}-Q{index % 4 + 1}", "季度", "季度：多半说的是公司的财季，不一定是日历上的季度"
    if group == "wp":
        prefix, weekday = _prefix(match["wp"]), match["wd"]
        if _compound(text, start, prefix):
            return "", "星期几", "前面一个字和上、下、本连成了词"
        if _not_weekday(text, end, weekday):
            return "", "星期几", "后面连着量词、连成了别的词"
        monday = _add_days(day0, -day0.weekday())
        day = _add_days(monday, _SHIFT[prefix] * 7 + _WEEKDAYS[weekday]) if monday else None
        return (day.isoformat() if day else "", "星期几", "" if day else "日子不存在")
    if group == "mp":
        prefix = _prefix(match["mp"])
        if _compound(text, start, prefix):
            return "", "月", "前面一个字和上、下、本连成了词"
        if after in _YUE_AFTER and not match["md"] and not match["medge"]:
            return "", "月", "前后连成了别的词"
        year, month = _add_months(day0.year, day0.month, _SHIFT[prefix])
        if not 1 <= year <= 9999:
            return "", "月", "日子不存在"
        if match["md"]:
            day = _day(year, month, _number(match["md"]))
            return (day.isoformat() if day else "", "月和日", "" if day else "日子不存在")
        if _SHIFT[prefix] == 0:
            return "", "月头月底" if match["medge"] else "月", "跟抬头同一个月"
        if match["medge"]:
            return f"{year:04d}-{month:02d} {_MONTH_EDGE[match['medge']]}", "月头月底", ""
        return f"{year:04d}-{month:02d}", "月", ""
    if group == "wkp":
        if match["wkend"]:
            return "", "周末", "只说了哪一周的周末，不是一天"
        if after in _NOT_WEEK:
            return "", "", ""  # 周期、周年：不是日子，不记
        return "", "一周", "只说了哪一周，不是一天"
    if group == "day":
        word = match["day"]
        if word.startswith("明") and before in _MING_BEFORE and after in _MING_AFTER.get(word, frozenset()):
            return "", "天", "前后连成了别的词"
        if word == "后天" and (before in _HOU_BEFORE or after in _HOUTIAN_AFTER):
            return "", "天", "明后天、以后天天这种：拿不准"
        if word == "后天" and before == "会" and text[max(0, start - 2) : start - 1] not in _WILL_SUBJECT:
            return "", "天", "前后连成了别的词"  # 开完会后天黑了、散会后天津：是“会后”
        if word == "前天" and (before in _QIAN_BEFORE or after in _QIANTIAN_AFTER):
            return "", "天", "前后连成了别的词"
        day = _add_days(day0, _DAYS[word])
        return (day.isoformat() if day else "", "天", "" if day else "日子不存在")
    if group == "same":
        return "", "当天", "跟抬头同一天"
    if group == "other":
        return "", "说的是别的那一天", "当天、次日这种说的是别的那一天"
    if group == "bare":
        if _not_weekday(text, end, match.group(0)[-1]):
            return "", "", ""  # 一周一次、周一起：不是日子，不记
        return "", "光写星期几", "每周几" if before == "每" else "光写星期几，分不清是哪一周"
    if group == "weekend":
        return "", "周末", "光写周末，不是一天"
    if group == "bare_medge":
        return "", "光写月底", "没写哪个月"
    if group == "bare_yedge":
        return "", "光写年底", "没写哪一年"
    if group == "ago":
        if match["ago_unit"] == "日" and match.group(0)[0].isdigit():  # “24 日前交”是 24 号以前，不是 24 天以前
            return "", "光写几号", "只写了几号，不知道是哪个月"
        return "", "几天前后", "几天前、几个月后：从哪天算拿不准"
    if group == "dayonly":
        return "", "光写几号", "只写了几号，不知道是哪个月"
    return "", "", ""


_GAP = re.compile(r"[ 　]?[（(，,：:]?[ 　]?")


def _split_doubled(match: re.Match[str], text: str) -> re.Match[str]:
    """“记一下下周四”：前一个“下”和“一”连成了“一下”，后面是“下周四”，不是“下下周四”。从第二个字重新认。"""
    for name in ("wp", "mp", "qp", "wkp"):
        prefix = match.group(name)
        if prefix and prefix[:2] in ("上上", "下下") and _compound(text, match.start(), prefix):
            again = _PATTERN.match(text, match.start() + 1)
            return again if again is not None else match
    return match


def _trim(match: re.Match[str], text: str) -> re.Match[str]:
    """尾巴连成了别的词的去掉重新认：“明年初中毕业”是明年，“上个月底仓”是上个月，“下个月 5 号线”是下个月。"""
    after = text[match.end() : match.end() + 1]
    for name in ("yedge", "medge", "ymedge"):
        edge = match.group(name)
        if edge and after in _EDGE_WORDS.get(edge[-1], frozenset()):
            again = _PATTERN.match(text, match.start(), match.start(name))
            return again if again is not None else match
    for name in ("md", "yd"):
        if match.group(name) and (after in _NOT_DATE_AFTER or (after.isascii() and after.isalnum())):
            again = _PATTERN.match(text, match.start(), match.start(name))
            return again if again is not None else match
    return match


def _scan(text: str, day0: date) -> list[Found]:
    found: list[Found] = []
    for match in _PATTERN.finditer(text):
        match = _trim(_split_doubled(match, text), text)
        value, kind, skip = _value_of(match, text, day0)
        if not kind:
            continue
        if kind in ("光写星期几", "光写几号") and found and _GAP.fullmatch(text[found[-1].end : match.start()]):
            continue  # “明天周二”“下周二（20 号）”：紧挨着前一处写的，前一处拿它核对（_beside），不另记
        found.append(Found(match.start(), match.end(), match.group(0), value, kind, skip))
    return found


def _beside(text: str, one: Found) -> str:
    """紧挨着写了日子、星期几的：不标。返回原因，没有就是空的。"""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", one.value):  # 到日的（“2026-09 月底”也是 10 个字，不能按长短认）
        day = date.fromisoformat(one.value)
        hits = []
        after = _BESIDE_AFTER.match(text, one.end)
        before = _BESIDE_BEFORE.search(text[: one.start])
        for hit in (after, before):
            if hit is None:
                continue
            groups = hit.groupdict()
            if groups.get("w"):
                if _WEEKDAYS[groups["w"]] != day.weekday():
                    return "旁边写的星期几对不上"
                continue
            year = int(groups["y"]) if groups.get("y") else None
            month = groups.get("m") or groups.get("m2") or groups.get("m4")
            number = groups.get("d") or groups.get("d2") or groups.get("d3") or groups.get("d4")
            if (month and not 1 <= int(month) <= 12) or (number and not 1 <= int(number) <= 31):
                continue  # 3-50 这种不是日子
            hits.append((year, int(month) if month else None, int(number) if number else None))
        for year, month, number in hits:
            same = number == day.day and (month is None or month == day.month) and (year is None or year == day.year)
            return "旁边写着同一天" if same else "旁边写的日子对不上"
        return ""
    if re.fullmatch(r"\d{4}-\d{2}", one.value):  # 到月的
        hit = _MONTH_BESIDE_AFTER.match(text, one.end)
        if hit is None:
            return ""
        year, month = int(one.value[:4]), int(one.value[5:7])
        same = int(hit["m"]) == month and (hit["y"] is None or int(hit["y"]) == year)
        return "旁边写着同一个月" if same else "旁边写的月份对不上"
    return ""


def find_dates(text: str, said: datetime) -> list[Found]:
    """一句话（窗口里写出来的样子）里认出来的相对的日子，按出现的先后。标的 skip 是空的；认出来不标的，skip 写着为什么。
    said 是这句话抬头上的时间（带时区，按它自己的时区算哪一天）。"""
    day0 = said.date()
    found = _scan(text, day0)
    if said.hour < NIGHT_END:  # 半夜说的：拿前一天再算一遍，不一样的不标
        earlier = _add_days(day0, -1)
        other = {(one.start, one.end): one for one in _scan(text, earlier)} if earlier else {}
        found = [
            replace(one, skip="半夜说的，拿不准是哪一天")
            if not one.skip and (other.get((one.start, one.end)) is None or other[(one.start, one.end)].value != one.value)
            else one
            for one in found
        ]
    out: list[Found] = []
    for index, one in enumerate(found):
        why = "" if one.skip else _beside(text, one) or _reported(text, one, found[:index])
        out.append(replace(one, skip=why) if why else one)
    if any(not one.skip for one in out):
        times = _l4_times(text, said)
        out = [replace(one, skip=L4_UNSURE) if not one.skip and not _l4_accepts(one.value, times) else one for one in out]
    return out


def _reported(text: str, one: Found, earlier: Sequence[Found]) -> str:
    """紧跟在“说”“告诉”这类后面，前面又有别的日子（不是今天）的：说的人是在另一天说的，“明天”照哪天算拿不准。"""
    before = text[: one.start]
    hit = _REPORTED.search(before)
    if hit is None:
        return ""
    head = before[: hit.start()]
    other = any(e.kind not in ("当天", "当天（英文）") for e in earlier) or _EXPLICIT_DATE.search(head) is not None
    return REPORTED if other else ""


def _l4_times(text: str, said: datetime) -> object:
    """L4 的 G6 拿这一句能对上的时间（javmem.pipeline.verify.sentence_times，说话那天也算）。用到时才导入：verify 导入 extract，
    extract 导入这里。"""
    from javmem.pipeline.verify import sentence_times

    return sentence_times(text, said)


def _l4_accepts(value: str, times: object) -> bool:
    """模型照这个日子写，L4 核得上吗：到日的要在 L4 认出的那几天里，到月的在那几个月里，到年的在那几年里。
    A 和 L4 认的写法不一样的时候（上上周三、下下个月、昨儿、下月三号、大前年……），不标，免得事实被 L4 挡掉。"""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value) in times.days  # type: ignore[attr-defined]
    month = re.fullmatch(r"(\d{4})-(\d{2})(?: \S+)?", value)
    if month:
        return (int(month[1]), int(month[2])) in times.months  # type: ignore[attr-defined]
    year = re.fullmatch(r"(\d{4})(?: \S+)?", value)
    if year:
        return int(year[1]) in times.years  # type: ignore[attr-defined]
    return False


def line_reason(block: WindowBlock, line: WindowLine) -> str:
    """这一行能不能照抬头的时间标：能标的返回空的。"""
    if line.attachment is not None or line.kind == ATTACHMENT:
        return "附件的正文：写的时间不一定是抬头上的时间"
    if line.kind in (FORWARDED, QUOTED_ONLY):
        return "转发的、引的旧信：写的时间不一定是抬头上的时间"
    if line.kind == HEADER:
        return "邮件头"
    if line.kind == SIGNATURE_INFO:
        return "签名"
    if line.kind != NEW or line.quoted:
        return "不是新写的话"
    if block.mail is None and (line.origin != ORIGIN_SAID or block.origin != ORIGIN_SAID):
        return "转述的、外部的：写的时间不一定是抬头上的时间"
    return ""


def window_notes(window: Window, rendered: RenderedWindow) -> tuple[dict[str, list[list[str]]], dict[str, object]]:
    """一个窗口的标注：{编号: [[说法, 日子], …]}，和 detail（标了的、认出来没标的，各写在哪一行、为什么）。
    只看 T 行，按窗口里写出来的样子（rendered.shown）找，说法逐字在那一行里。"""
    notes: dict[str, list[list[str]]] = {}
    labeled: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for block in window.blocks:
        if block.context:
            continue
        for line in block.lines:
            if not line.target:
                continue
            shown = rendered.shown.get(line.ref, "")
            if not shown:
                continue
            reason = line_reason(block, line)
            for one in find_dates(shown, block.said_at):
                row = {"行": line.ref, "说法": one.text, "日子": one.value, "种类": one.kind, "抬头": iso(block.said_at)}
                skip = one.skip or reason
                pairs = notes.setdefault(line.ref, [])
                if not skip and len(pairs) >= MAX_PER_LINE:
                    skip = "这一行标得太多"
                if skip:
                    skipped.append({**row, "为什么不标": skip})
                    continue
                labeled.append(row)
                pair = [_clean(one.text), _clean(one.value)]
                if pair not in pairs:
                    pairs.append(pair)
    notes = {ref: pairs for ref, pairs in notes.items() if pairs}
    return notes, {"version": PRE_A_VERSION, "labeled": labeled, "skipped": skipped}


def apply_notes(rendered: RenderedWindow, notes: Mapping[str, Sequence[Sequence[str]]]) -> RenderedWindow:
    """把标注写进窗口：那一行编号后面、已有的〔〕标注后面加〔日子：说法 = 日子；…〕。别的行一字不变；
    shown（L4 比引文用的原文）不变。"""
    if not notes:
        return rendered
    out = []
    for row in rendered.text.split("\n"):
        ref, space, rest = row.partition(" ")
        pairs = notes.get(ref) if space else None
        if pairs and ref in rendered.shown:
            note = f"〔{NOTE_LABEL}：" + "；".join(f"{_clean(text)} = {_clean(value)}" for text, value in pairs) + "〕"
            at = 0
            while rest.startswith("〔", at) and "〕" in rest[at:]:  # 原文里的〔〕都换成了［］，开头的〔只能是我们的标注
                at = rest.index("〕", at) + 1
            row = f"{ref} {rest[:at]}{note}{rest[at:]}"
        out.append(row)
    text = "\n".join(out)
    return replace(rendered, text=text, sha256=_sha(text))


# ---------- 抽之前标好、存进结果库 ----------


def contents(window: Window) -> dict[str, str]:
    """存进标注的这个窗口有哪些包和句子（撤回时按内容找它们，和预处理 B 一样）。"""
    return {
        "packets": json.dumps([list(one) for one in window.packets], ensure_ascii=False),
        "sentences": json.dumps(sorted({line.sentence_id for line in window.lines}), ensure_ascii=False),
    }


class PreA:
    """一遍里的预处理 A。extract.run_extraction 抽之前调 annotate：没标过的窗口照规则算好，存进结果库（prea_notes）。"""

    def __init__(self, *, log: Callable[[str], None] | None = None) -> None:
        self.log = log
        self.stats: Counter[str] = Counter()

    def describe(self) -> dict[str, object]:
        return {"version": PRE_A_VERSION, "night_end": NIGHT_END}

    def annotate(self, windows: Iterable[Window], store: ResultStore, patterns: Sequence[RedactionPattern]) -> None:
        """没标过的窗口标上、存起来（标了 0 处的也存，下回知道标过）。private 的、打码没打干净的本来就不发，不标。"""
        for window in windows:
            if window.scope.private:
                continue
            rendered = render_window(window, patterns)
            if rendered.leftover:
                continue
            if store.prea_notes(window.window_id, rendered.sha256, PRE_A_VERSION) is not None:
                self.stats["存过的窗口"] += 1
                continue
            notes, detail = window_notes(window, rendered)
            store.save_prea_notes(
                {"window_id": window.window_id, "window_sha": rendered.sha256, "version": PRE_A_VERSION,
                 "noted_sha": apply_notes(rendered, notes).sha256, **contents(window),
                 "notes": json.dumps(notes, ensure_ascii=False), "detail": json.dumps(detail, ensure_ascii=False),
                 "created_at": _now()}
            )
            self.stats["标过的窗口"] += 1
            self.stats["标上的"] += len(detail["labeled"])  # type: ignore[arg-type]
            self.stats["认出来没标的"] += len(detail["skipped"])  # type: ignore[arg-type]
        if self.log is not None:
            self.log(f"[A] 预处理 A（{PRE_A_VERSION}）：{dict(self.stats)}")
