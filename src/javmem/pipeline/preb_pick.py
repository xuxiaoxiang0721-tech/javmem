"""预处理 B：先挑候选、只跟同类比（导航 D136 第 ② 步，b3 起）。

只问本机 Qwen 一句“是不是名单上的这一项”，它只看得到自己提的那一项，别的像的看不到。所以改成：

1. **先看说法是哪一类，只和名单上同一类的比**（class_of、DealFinder、preb.Roster.mention_class）：
   - 单子（订单或合同）：后面跟着 FCN、累购、AQ、票据、合约、那张、这单这类字的（“YSJ 这张”“JKJ+YSJ 的 FCN”），
     或者几只股票用 + 连着写的（“底层 Jiake + Yishu”）。这种说法整个当成一张单子的叫法：里头的股票不再单独标，
     Qwen 没提的，代码也把它当一个说法拿去对。说的是几张的（“两张 FCN”；“JKJ/YSJ 累购”：累购一张只有一只股票）不标。
   - 别的：说法原样是名单上的叫法的，就是那一项的类；不是的，照 Qwen 提的那一项的类（“小岑”“X 总”像人、“X 银行”
     像公司，这种看样子的只在没有 Qwen 提的那一项时才用：看样子会认错，“阿某”也可能是股票）。
2. **再挑候选**（Picker）：同一类的项多于 TOP_K 的，用本机的向量模型（Qwen3-Embedding-4B）挑最像的 TOP_K 项。
   Qwen 提的那一项、和它只差日子编号金额的几项一定放进去；日子、编号、金额和说法对不上的项不放（防合错 a）。
3. **Qwen 只在这几项里选一个，或者选“都不是”**（choice_probs）：从回答第一个字的概率算每一项的把握，
   最高的那一项到 preb.KEEP_AT 才算。

例子都是编的。
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from javmem.embedding import TRANSPORT, Embedder, EmbeddingError, embed_all, unpack_vector
from javmem.pipeline.preb_keys import EVENT, spans

TOP_K = 5  # 最多挑几项给 Qwen 选
NONE_LABEL = "X"  # “都不是”
LABELS = ("A", "B", "C", "D", "E", "F", "G")

# ---------- 类 ----------

DEAL, STOCK, PERSON, COMPANY, PROJECT, BOT, PLACE = "单子", "股票", "人", "公司", "项目", "Bot", "地点"
CLASS_OF_KIND = {"订单或合同": DEAL, "资产": STOCK, "人": PERSON, "公司或机构": COMPANY, "项目": PROJECT, "Bot": BOT,
                 "地点": PLACE}
KIND_OF_CLASS = {cls: kind for kind, cls in CLASS_OF_KIND.items() if cls != BOT}  # 收名字时 Qwen 写的类 → L3 的实体类型


def class_of(kind: str) -> str:
    """名单上一项（或者 L3 的实体类型）是哪一类。资产这一类叫“股票”（基金、债券也在里面）。"""
    kind = (kind or "").strip()
    return CLASS_OF_KIND.get(kind, kind or "别的")


_PERSON = re.compile(r"^(?:小|老|阿)[一-鿿]{1,2}$|^[一-鿿]{1,3}(?:总|姐|哥|叔|姨|老师|先生|小姐|女士|太太|经理|律师|医生)$")
_COMPANY = re.compile(r"(?:银行|证券|公司|集团|保险|信托|(?<![A-Za-z])(?:Bank|Securities))$", re.IGNORECASE)
_HE = ("他", "她", "他们", "她们")


def class_by_shape(mention: str) -> str | None:
    """看说法本身的样子猜的类：“小岑”“岑总”“他”像人，“东岸银行”像公司。认不出的 None。只在没有 Qwen 提的那一项时用。"""
    text = mention.strip()
    if text in _HE or _PERSON.match(text):
        return PERSON
    if _COMPANY.search(text):
        return COMPANY
    return None


# ---------- 单子的说法 ----------

_LATIN_MARKERS = r"FCN|ELN|AQ|accumulators?|term\s?sheets?|termsheets?"
MARKER = re.compile(rf"(?<![A-Za-z])(?:{_LATIN_MARKERS})(?![A-Za-z])|固定票息票据|固定票息|累购器|累购合约|累购|票据|合约|合同|订单|单子",
                    re.IGNORECASE)
_AQ_MARKER = re.compile(r"(?<![A-Za-z])(?:AQ|accumulators?)(?![A-Za-z])|累购", re.IGNORECASE)
_NOTE_MARKER = re.compile(r"(?<![A-Za-z])(?:FCN|ELN)(?![A-Za-z])|票据|固定票息", re.IGNORECASE)
# 单子的代称：那张、这单、那笔、另一张……（“那份”“这份”多半说的是文件、周报，不算）
POINTER = re.compile(r"(?:那|这|另外|另)(?:一|两|几|三|四|五|六|七|八|九|十)?(?:张|单|笔)")
_DOC_MARKER = re.compile(r"term\s?sheets?|termsheets?", re.IGNORECASE)  # 单子的文件：只带着公司名的不算哪一张
_TICKER = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,5}(?:\.[A-Z]{1,2})?(?![A-Za-z0-9])")
_CAPWORD = re.compile(r"(?<![A-Za-z0-9])[A-Z][a-z]{2,}(?![A-Za-z0-9])")
# 大写的、不是股票代号的词（结构票据的字眼、币种、常见缩写）
_NOT_TICKER = frozenset({"FCN", "ELN", "AQ", "TS", "KO", "KI", "ISIN", "OTC", "PDF", "CTA", "NAV", "ETF", "AI", "API", "OK",
                         "UTC", "GMT", "USD", "SGD", "HKD", "JPY", "CNY", "RMB", "EUR", "GBP", "AUD", "CEO", "CFO", "PM"})
_JOIN = re.compile(r"\s*(?:[+＋/／&、·\-]|和|与|及|的)?\s*")  # 一个说法里两个字块之间能隔的（AQ-YSJ、X+Y、X 的 FCN）
_PLUS = re.compile(r"[+＋]")
_SPLIT = re.compile(r"、|·|和|与|及|\s/|/\s|\s／|／\s")  # 隔着这些的是几样东西（“JKJ、YSJ”“X+Y / X+Z”“FCN · YSJ”）
_PLURAL = re.compile(r"[两三四五六七八九十几多]\s*[张份笔单只个]|\d+\s*[张份笔单]|各")
_PLURAL_BEFORE = re.compile(r"(?:[两三四五六七八九十几多]|\d+)\s*[张份笔单只个]\s*(?:的\s*)?$")  # 紧贴在说法前面的“两张”
_CLAUSE_STOPS = "，。；！？、,;!?\n"
# “那张图”“这张 9/03 图”“东岸的 FCN 文件”：说的是图、表、文件，不是单子
_NOT_DEAL_NOUN = re.compile(r"\s*(?:图|表|截|照|片|纸|卡|屏|周报|月报|日报|报告|文件|邮件|账单|清单|名单|PDF)", re.IGNORECASE)
_IDENT = frozenset({"stock", "company", "ticker", "word", "日子", "编号", "金额"})
_STOCKISH = frozenset({"stock", "ticker", "word"})  # 拼篮子的（Jiake + Rowan 里的 Rowan 名单上没有，也算）
_LISTED_STOCK = frozenset({"stock", "ticker"})  # 认得出是股票的（大写开头的英文词也可能是人名，不算）


def bare_ticker(text: str) -> bool:
    """整个说法就是一个股票代号样子的写法（“YSJ”“ZKX”）：大写字母开头、两到六个大写字母或数字，不是 FCN、AQ 这类字。
    光是这样的说法是股票，不标成单子，也不记成单子的“也叫”（累购单子不能把股票名收进去）。"""
    text = text.strip()
    return bool(_TICKER.fullmatch(text)) and text not in _NOT_TICKER


def plural_at(text: str, start: int, end: int) -> bool:
    """text[start:end] 这个说法说的是几张：说法里有“两张”“三笔”“各”这类字，或者同一小句里紧贴在它前面的是“两张”这类字
    （“两张 YSJ 票据”；逗号前面的“买了两张，”不管后面）。"""
    if _PLURAL.search(text[start:end]):
        return True
    before = text[max(0, start - 8) : start]
    cut = max(before.rfind(stop) for stop in _CLAUSE_STOPS)
    return bool(_PLURAL_BEFORE.search(before[cut + 1 :]))


def _product(marker: str) -> str | None:
    """单子的种类：累购、FCN、ELN。票据、合约、term sheet 这类字不分种类，None。"""
    text = marker.casefold()
    if "累购" in text or text in ("aq", "accumulator", "accumulators"):
        return "aq"
    if "固定票息" in text or text == "fcn":
        return "fcn"
    return "eln" if text == "eln" else None


@dataclass(frozen=True)
class DealPhrase:
    """一行里一张单子的说法：start、end 是在这一行里的位置，text 是原文（逐字）。plural：说的是几张。"""

    start: int
    end: int
    text: str
    plural: bool


def _names_pattern(names: Iterable[str], ignore_case: bool) -> re.Pattern[str] | None:
    parts = []
    for name in sorted({n for n in names if len(n.strip()) >= 2}, key=len, reverse=True):
        body = re.escape(name.strip())
        head = r"(?<![A-Za-z0-9])" if name.strip()[0].isascii() and name.strip()[0].isalnum() else ""
        tail = r"(?![A-Za-z0-9])" if name.strip()[-1].isascii() and name.strip()[-1].isalnum() else ""
        parts.append(head + body + tail)
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE if ignore_case else 0)


class DealFinder:
    """按规则在一行原文里找单子的说法。stock_names、company_names 是名单上股票、公司的叫法（拼出单子名字的字块）。"""

    def __init__(self, stock_names: Iterable[str], company_names: Iterable[str]) -> None:
        stock_names, company_names = list(stock_names), list(company_names)
        self._kinds = {name.strip().casefold(): "company" for name in company_names}
        self._kinds.update({name.strip().casefold(): "stock" for name in stock_names})  # 两边都有的算股票
        names = [*stock_names, *company_names]
        # 四个字以上的不分大小写（jiake 这种小写的也认）；短的（YSJ、DX 这种）分大小写，免得认成普通的英文词
        self._long = _names_pattern([n for n in names if len(n.strip()) >= 4], ignore_case=True)
        self._short = _names_pattern([n for n in names if len(n.strip()) < 4], ignore_case=False)

    def _units(self, text: str) -> list[tuple[int, int, str]]:
        """一行里拼单子名字用得上的字块 (起, 止, 哪种)：股票、公司的叫法，股票代号样子的，大写开头的英文词，
        日子、编号、金额，单子的字眼（FCN、累购……），“那张”“这单”。重叠的留长的、靠前的。"""
        found: list[tuple[int, int, str]] = []
        for pattern in (self._long, self._short):
            if pattern is not None:
                for match in pattern.finditer(text):
                    found.append((match.start(), match.end(), self._kinds.get(match.group(0).strip().casefold(), "stock")))
        for match in _TICKER.finditer(text):
            if match.group(0) not in _NOT_TICKER:
                found.append((match.start(), match.end(), "ticker"))
        for match in _CAPWORD.finditer(text):
            found.append((match.start(), match.end(), "word"))
        for start, end, kind, _ in spans(text):
            found.append((start, end, kind))
        for match in MARKER.finditer(text):
            found.append((match.start(), match.end(), "marker"))
        for match in POINTER.finditer(text):
            found.append((match.start(), match.end(), "pointer"))
        found.sort(key=lambda unit: (unit[0], -(unit[1] - unit[0])))
        units: list[tuple[int, int, str]] = []
        for unit in found:
            if units and unit[0] < units[-1][1]:
                continue
            units.append(unit)
        return units

    def phrases(self, text: str) -> list[DealPhrase]:
        units = self._units(text)
        chains: list[list[tuple[int, int, str]]] = []
        for unit in units:
            if chains:
                gap = text[chains[-1][-1][1] : unit[0]]
                if _JOIN.fullmatch(gap) and not _SPLIT.search(gap):
                    chains[-1].append(unit)
                    continue
            chains.append([unit])
        out = []
        for chain in chains:
            phrase = self._phrase(text, chain)
            if phrase is not None:
                out.append(phrase)
        return out

    def _phrase(self, text: str, chain: list[tuple[int, int, str]]) -> DealPhrase | None:
        # 后面接着“到期、付息”的日子、金额是事件的（“那张 9/15 到期”），不算单子本身的
        while chain and chain[-1][2] in ("日子", "金额") and EVENT.match(text[chain[-1][1] :]):
            chain = chain[:-1]
        if not chain:
            return None
        kinds = [kind for _, _, kind in chain]
        for start, end, kind in chain:  # “那张图”“这张 9/03 图”：那张说的不是单子
            if kind == "pointer" and _NOT_DEAL_NOUN.match(text[end:]):
                return None
        if _NOT_DEAL_NOUN.match(text[chain[-1][1] :]):  # “这张东岸图”“东岸的 FCN 文件”
            return None
        basket = any(
            a[2] in _STOCKISH and b[2] in _STOCKISH and {a[2], b[2]} & _LISTED_STOCK and _PLUS.search(text[a[1] : b[0]])
            for a, b in zip(chain, chain[1:])
        )
        marked = "marker" in kinds or "pointer" in kinds
        if not basket and not (marked and any(kind in _IDENT for kind in kinds)):
            return None
        markers = [text[s:e] for s, e, kind in chain if kind == "marker"]
        if (not basket and "pointer" not in kinds and markers and all(_DOC_MARKER.fullmatch(one) for one in markers)
                and {kind for kind in kinds if kind in _IDENT} <= {"company"}):
            return None  # “东岸 term sheet”：哪家的文件，不是哪一张单子
        start, end = chain[0][0], chain[-1][1]
        phrase = text[start:end]
        stocks = {text[s:e].casefold() for s, e, kind in chain if kind in _LISTED_STOCK}
        products = {_product(text[s:e]) for s, e, kind in chain if kind == "marker"} - {None}
        plural = plural_at(text, start, end)
        if _AQ_MARKER.search(phrase) and not _NOTE_MARKER.search(phrase) and len(stocks) >= 2:
            plural = True  # 累购一张只有一只股票：“JKJ/YSJ 累购”是两张
        if len(products) >= 2:
            plural = True  # “东岸 AQ/FCN 的条款”：说的是几种单子
        return DealPhrase(start, end, phrase, plural)


def occurrences(text: str, mention: str) -> list[tuple[int, int]]:
    out, at = [], text.find(mention) if mention else -1
    while at >= 0:
        out.append((at, at + len(mention)))
        at = text.find(mention, at + 1)
    return out


def inside(span: tuple[int, int], phrase: DealPhrase) -> bool:
    return phrase.start <= span[0] and span[1] <= phrase.end


def overlaps(span: tuple[int, int], phrase: DealPhrase) -> bool:
    return span[0] < phrase.end and phrase.start < span[1]


# ---------- 挑候选（向量） ----------

INSTRUCT = "Instruct: 给一段记忆原文里说到的一样东西（人、公司、股票、单子……），找出已知名单上指的就是它的那一项\nQuery: "
RecordEmbed = Callable[[str, str, int, str, int, float, bool], None]  # (窗口, 批的指纹, 第几次, 结果, token, 秒, 估的)


LOCK_FILE = Path(tempfile.gettempdir()) / "javmem-embedding.lock"
LOCK_LIMIT = 600.0  # 等这么久还拿不到锁，当向量服务出错（这个窗口不标）


@contextmanager
def one_request_at_a_time(path: Path | None = None, *, wait: float = 0.05, limit: float = LOCK_LIMIT) -> Iterator[None]:
    """本机的一把文件锁：几个进程一起跑也只让一个请求同时发给向量服务（工作约定：线上也在用，一次一个请求）。
    拿不到就隔一会儿再试，等太久的抛 EmbeddingError。"""
    path = path or LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - 锁要一直拿着，到 finally 才关
    started = time.monotonic()
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - started > limit:
                    raise EmbeddingError(TRANSPORT, "等向量服务的锁等太久") from None
                time.sleep(wait)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # 没拿到锁就出来的，没什么可放
        handle.close()


def _unit(blob: bytes) -> tuple[float, ...]:
    values = unpack_vector(blob)
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return tuple(value / norm for value in values)


class Picker:
    """用本机的向量模型给同一类的几项按“像不像”排个序。向量按文字记在内存里；一次只发一个请求（线上也在用这个服务），
    一批最多 16 条（embed_all）。没有向量服务（测试）的 rank 返回 None；服务出错的抛 EmbeddingError（调的人整个窗口不标，
    不悄悄换成别的挑法）。"""

    def __init__(self, embedder: Embedder | None, record: RecordEmbed | None = None, *, lock_file: Path | None = None) -> None:
        self.embedder = embedder
        self.record = record
        self.lock_file = lock_file  # None：本机临时目录里那一把（几个进程共用）
        self._cache: dict[str, tuple[float, ...]] = {}
        self._lock = threading.Lock()
        self.errors = 0

    @property
    def name(self) -> str | None:
        return self.embedder.name if self.embedder is not None else None

    def rank(self, query: str, docs: Sequence[str], window_id: str = "") -> list[float] | None:
        """每篇和 query 有多像（余弦，和 docs 一样的顺序）。没有向量服务的 None；服务出错的抛 EmbeddingError。"""
        if self.embedder is None:
            return None
        texts = [INSTRUCT + query, *docs]
        with self._lock:
            missing = [text for text in dict.fromkeys(texts) if text not in self._cache]
            if missing:
                record = None
                if self.record is not None:
                    hook = self.record

                    def record(digest: str, attempt: int, status: str, tokens: int, seconds: float, estimated: bool) -> None:
                        hook(window_id, digest, attempt, status, tokens, seconds, estimated)

                try:
                    with one_request_at_a_time(self.lock_file):  # 两条线一起跑也一次一个请求
                        found = embed_all(missing, self.embedder, record=record)
                except EmbeddingError:
                    self.errors += 1
                    raise
                for text, blob in found.items():
                    self._cache[text] = _unit(blob)
            vectors = [self._cache.get(text) for text in texts]
        head = vectors[0]
        if head is None:
            return None
        return [sum(a * b for a, b in zip(head, vector)) if vector is not None else -1.0 for vector in vectors[1:]]


# ---------- Qwen 选哪一项 ----------


def choice_probs(out: Mapping[str, object], labels: Sequence[str]) -> dict[str, float] | None:
    """回答第一个字是哪个选项的概率。分母是前十个候选字的概率加起来：模型把把握放在别的字上的（比如“都”），
    也算进分母，选项的把握就跟着小。一个选项都不在前十个里的，None。logprob 不是有限的数、或者大于 0 的（不可能是对的）
    那一个不算。"""
    try:
        content = (out["choices"][0].get("logprobs") or {}).get("content") or []  # type: ignore[index]
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if not content or not isinstance(content[0], Mapping):
        return None
    wanted = set(labels)
    found: dict[str, float] = {}
    total = 0.0
    for top in content[0].get("top_logprobs") or []:
        if not isinstance(top, Mapping) or not isinstance(top.get("logprob"), (int, float)):
            continue
        logprob = float(top["logprob"])
        if not math.isfinite(logprob) or logprob > 1e-6:
            continue
        p = math.exp(min(logprob, 0.0))
        total += p
        token = str(top.get("token") or "").strip().rstrip(".．、:：)）").upper()
        if token in wanted:
            found[token] = found.get(token, 0.0) + p
    if not found or total <= 0:
        return None
    return {label: p / total for label, p in found.items()}
