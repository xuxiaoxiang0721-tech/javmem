"""问话切词（J2.14，query-api 5.1）：memory_search 在账本里没查到、回退原文层时，问句不能整句去搜，先切成词。

用 jieba（MIT 许可证），加上词典（`javmem.zh.lexicon`：仓库外配置里的人名、公司名，调用方还可以加账本里看得到的别名），
不然没见过的人名会切成单字、丢掉以后什么都查不到。切完丢掉不到两个字的词、问话的虚词（什么、多少、哪个……）。
金额不拼回去：“5万场地费什么时候付”剩下的是“场地”，不然任一词命中就算，会查到所有写着 5 万的句子。

jieba 第一次切词要建前缀词典（一秒左右），建好的缓存写在 cache_dir（调用方给数据目录下的地方）；不给就写系统的临时目录。

分词器记在进程里，一个 slot 一个（查询接口给身份：词典照身份分，J5.5）：第一次用时建，词典没变就一直用它；这个 slot 的
词典变了（写账以后别名变了、别名表改了）建一个新的、旧的不留。几个身份轮着查不互相挤掉；slot 最多 MAX_SLOTS 个（一个
分词器几十 MB），再多就丢最久没用的。不给 slot 的共用一个。
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Hashable, Iterable
from pathlib import Path

QUESTION_WORDS = frozenset(
    {
        "什么", "怎么", "怎样", "怎么样", "为什么", "为何", "哪个", "哪些", "哪里", "哪儿", "哪位", "哪天", "哪家",
        "多少", "几个", "几时", "何时", "什么时候", "是否", "是不是", "有没有", "能不能", "可不可以", "要不要", "会不会",
        "对不对", "好不好", "行不行", "多久", "多大", "多长", "如何", "请问", "时候",  # “什么时候”切成两个词
    }
)
MIN_WORD = 2
_SPACE_OR_PUNCT = re.compile(r"^[\W_]+$")
MAX_SLOTS = 4  # 进程里最多留几个分词器（一个几十 MB）：查询服务一个身份一个进程，只用一个

_lock = threading.Lock()
# slot → ((词典, 缓存目录), 分词器)，最久没用的在前
_slots: OrderedDict[Hashable, tuple[tuple[tuple[str, ...], str | None], object]] = OrderedDict()


def _tokenizer(words: tuple[str, ...], cache_dir: Path | None, slot: Hashable) -> object:
    key = (words, str(cache_dir) if cache_dir is not None else None)
    with _lock:
        held = _slots.get(slot)
        if held is not None and held[0] == key:
            _slots.move_to_end(slot)
            return held[1]
        _slots.pop(slot, None)  # 这个 slot 的词典变了：旧的不留着
        import jieba  # 用到才加载：建前缀词典要一秒左右

        logging.getLogger("jieba").setLevel(logging.WARNING)
        tokenizer = jieba.Tokenizer()
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tokenizer.cache_file = str(cache_dir / "jieba.cache")
        for word in words:
            tokenizer.add_word(word)
        while len(_slots) >= MAX_SLOTS:
            _slots.popitem(last=False)
        _slots[slot] = (key, tokenizer)
        return tokenizer


def query_words(
    question: str, words: Iterable[str] = (), *, cache_dir: Path | None = None, slot: Hashable = None
) -> list[str]:
    """问句切成拿去原文层查的词：按词典切，丢掉不到两个字的、问话的虚词，去掉重复的，顺序照问句。slot 是分词器记在
    进程里的名字（查询接口给身份，一个身份一份词典），不给的共用一个。"""
    dictionary = tuple(dict.fromkeys(word.strip() for word in words if word.strip()))
    tokenizer = _tokenizer(dictionary, cache_dir, slot)
    found: dict[str, None] = {}
    for word in tokenizer.lcut(question):  # type: ignore[attr-defined]
        word = word.strip()
        if len(word) < MIN_WORD or word in QUESTION_WORDS or _SPACE_OR_PUNCT.match(word):
            continue
        found.setdefault(word, None)
    return list(found)
