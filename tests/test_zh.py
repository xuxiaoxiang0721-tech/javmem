"""J2.14 中文规则的单测：javmem.zh 的几个模块、L5 拆名字，以及审阅时抓到过的几种误拦（句子和名字都是编的，从头写的）。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from javmem.config import ConfigError
from javmem.ledger import connect, load_migrations, migrate
from javmem.pipeline.extract import ENTITY_TYPES
from javmem.pipeline.merge import merge_key, name_key, name_parts, name_variants, value_text
from javmem.pipeline.normalize import Scope
from javmem.zh import query_words
from javmem.zh.clock import clocks
from javmem.zh.lexicon import ENTITY_TYPES as LEXICON_TYPES
from javmem.zh.lexicon import EMPTY, load_lexicon
from javmem.zh.numbers import cn_number, cn_values, value_key
from javmem.zh.search import LANE, search_key, searchable

# ---------- 中文的数 ----------


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("两千五", 2500), ("一万二", 12000), ("三百二", 320), ("一万零二", 10002), ("十五", 15), ("二十五", 25),
        ("一百零五", 105), ("十万五", 105000), ("三千万", 30_000_000), ("一亿五", 150_000_000), ("二〇二六", 2026),
        ("两千五一", None), ("x", None), ("一" * 30, None),
    ],
)
def test_cn_number_colloquial(text: str, number: int | None) -> None:
    assert cn_number(text) == number


def test_cn_values_of_a_sentence() -> None:
    assert cn_values("这批轮胎两千五一条") == {"2500", "1"}  # 两千五一条：两千五，一条
    assert cn_values("分成比例百分之三十") == {"30", ".3"}  # 不再一个字一个字算，3% 不会被放过
    assert cn_values("这趟旅行花了三点五万") == {"3.5", "35000"}
    assert cn_values("九十月份") >= {"9", "10"}  # 后面跟着“月”又不是一个月份：一个字一个数
    assert cn_values("三四天") >= {"3", "4"}
    assert cn_values("万家灯火那家店的报价到了") == set()  # 单写的“万”不是数
    assert cn_values("要准备几万块学费") == set()
    assert cn_values("场地费 5 万") == set()  # “万”是 5 的单位，阿拉伯数字那边算


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ("5 万", "50000"), ("5万", "50000"), ("50000", "50000"), ("约 5 万", "50000"), ("5 千", "5000"),
        ("USD 46k", "usd46000"), ("USD 46,000", "usd46000"), ("$100", "usd100"), ("5万元", "cny50000"),
        ("0.85", "0.85"), ("五万", "50000"), ("三点五万", "35000"), ("2万5", "25000"), ("−5万", "-50000"),
        ("approximately 5000", "5000"),
        ("2026-11-18", "2026-11-18"), ("2026/11/18", "2026-11-18"), ("2026年11月18日", "2026-11-18"),
        ("11月18日", "2026-11-18"), ("十一月十八号", "2026-11-18"),
        ("615 本", None), ("5%", None), ("USD 5 / HKD 40", None), ("11月18", None), ("5万多", None),
        ("11/18", None), ("3/4", None),  # 属性不像日期：斜杠是分数
    ],
)
def test_value_key(value: str, key: str | None) -> None:
    assert value_key(value, date(2026, 11, 6), "金额") == key


def test_value_key_dates() -> None:
    assert value_key("11/18", date(2026, 11, 6), "交货日期") == "2026-11-18"
    assert value_key("11-18", date(2026, 11, 6), "截止时间") == "2026-11-18"
    # 没写年份的往后看：说话那天前 30 天到后 335 天里的那一次，说话那天差几天的总是同一个键，跨年也一样
    assert value_key("11月18日", date(2026, 5, 17)) == value_key("11月18日", date(2026, 5, 21)) == "2026-11-18"
    assert value_key("1月5日", date(2026, 12, 28)) == value_key("1月5日", date(2027, 1, 3)) == "2027-01-05"
    assert value_key("1月15日", date(2026, 11, 10)) == "2027-01-15"
    assert value_key("3-5", date(2026, 11, 10), "期限") is None  # 期限可能是 3 到 5 年
    assert value_key("11月18日") is None  # 不知道哪一年
    assert value_text("11月18日") == "11月18日"  # 照原来的比对写法
    assert value_text("1/2", date(2026, 6, 1), "完成比例") == "1/2"


def test_merge_key_uses_the_unified_value() -> None:
    scope = Scope(("work",), ())
    keys = {merge_key(scope, "E1", "报价单价", value, "x") for value in ("5 万", "50000", "5万")}
    assert len(keys) == 1
    assert merge_key(scope, "E1", "报价单价", "5 千", "x") not in keys
    said = date(2026, 11, 6)
    dates = {merge_key(scope, "E1", "交货日期", value, "x", said=said) for value in ("2026-11-18", "11月18日", "2026/11/18")}
    assert len(dates) == 1


# ---------- 时刻 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("明天下午三点一刻开会", {(15, 15)}),  # 写了下午：只算 15:15
        ("明天 3 点 15 到", {(3, 15), (15, 15)}),
        ("三点三刻", {(3, 45), (15, 45)}),
        ("六点半", {(6, 30), (18, 30)}),
        ("两点", {(2, 0), (14, 0)}),
        ("9/20 7:29 PM PT 起飞", {(7, 29), (19, 29)}),
        ("后天下午2:00打疫苗", {(2, 0), (14, 0)}),
        ("10:00-12:00 开会", {(10, 0), (12, 0)}),
        ("12:30 am", {(12, 30), (0, 30)}),
        ("花了三点五万", {(3, 0), (15, 0)}),  # 三点五万不是三点零五分
        ("早上8点出门", {(8, 0)}),  # 早上的只是 8 点
        ("晚上8点到", {(20, 0)}),
        ("一点也不急", set()),
        ("差一点就赶上了", set()),
        ("一点半到", {(1, 30), (13, 30)}),
        ("明天最晚9点前到", {(9, 0), (21, 0)}),  # 一个字的“晚”不算晚上
        ("明天最早4点到", {(4, 0), (16, 0)}),
        ("每天早晚9点各喂一次", {(9, 0), (21, 0)}),
    ],
)
def test_clocks(text: str, expected: set[tuple[int, int]]) -> None:
    assert clocks(text) == expected


# ---------- 原文检索的统一写法 ----------


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("远帆ADR", "远帆 ADR"),
        ("ＫＶＬＴ", "KVLT"),
        ("宽带+电视", "宽带 + 电视"),
        ("900箱", "900 箱"),
        ("3600万", "3,600 万"),
        ("260万", "2,600,000"),
        ("10月15日", "10/15"),
        ("十月十五日", "10 月 15 日"),
        ("18号", "18 日"),
        ("你好,世界", "你好，世界"),
        ("2026-10", "2026年10月"),
    ],
)
def test_search_key_same(one: str, other: str) -> None:
    assert search_key(one) == search_key(other)


@pytest.mark.parametrize(
    ("query", "text", "found"),
    [
        ("5万", "灯光 15万", False),
        ("5万", "舞台 5 万", True),
        ("5%", "服务费加收 15%", False),
        ("3/1", "3/17 的音乐会", False),
        ("10月", "10月15日的体检", True),
        ("10月15日", "2026-10-15 的体检", True),
        ("2026年", "2026-10-15 的体检", True),
        ("100", "100，200 两档", True),  # 全角逗号不当千分位
        ("10/15", "体检在 10/15.", True),
        ("10月15日", "on 10/15, we ship", True),
        ("2026-10", "2026-10-15 交货", True),
        ("10月", "2026-10 的月报", True),
        ("15人", "10-15人参加", True),  # 原句另一份写法里斜杠、横线照写
        ("5天", "3-5天到", True),
        ("10-15", "10-15人", True),
        ("10-15人", "10-15人参加", True),
        ("10月", "10-15人参加", True),  # 代价：范围也当成了日期
        ("号线", "5号线", True),  # 另一份写法里“号”照写
        ("5号", "5号线", True),
        ("18号", "18 号交货", True),
        ("18号", "18号去北京", True),
        ("18日", "18号和19号都行", True),
        ("11/18", "11/18 周三开会", True),  # 查询词和原句换出来的样子不看后面跟着什么
        ("11月18日", "11/18 周三开会", True),
        ("10/15", "10/15 小林来", True),
        ("10/15", "10/15 韩总来访", True),
        ("0571", "电话 0571-12", True),  # 0571 不是年份
        ("2026年", "电话 0571-12", False),
    ],
)
def test_search_key_in_searchable(query: str, text: str, found: bool) -> None:
    assert (search_key(query) in searchable(text)) is found


def test_query_keys_never_hold_the_lane_mark() -> None:
    assert LANE not in search_key("a" + LANE + "b") and LANE in searchable("10-15人")


def test_searchable_strips_its_own_marks() -> None:
    assert "⟦" not in searchable("⟦") and searchable("a⟦b⟧c") == "abc"


# 统一写法存在原文索引里（raw_sentences.norm）：改了写法要换 zh.search.VERSION、加一个账本迁移重算，再改这里
PINNED = {
    "远帆 ADR 成交了 900 股。": "远帆adr成交了⟦900⟧股。",
    "3,600 万日元到了大阪，手续费约 2 万。": "⟦36000000⟧日元到了大阪、手续费约⟦20000⟧。",
    "10 月 15 日的体检，2026-10-15 出结果。": "⟦10⟧月⟦15⟧日的体检、⟦2026⟧年⟦10⟧月⟦15⟧日出结果。",
    "服务费加收 5%，3-5天到，5号线。": "服务费加收⟦5⟧%、⟦3⟧月⟦5⟧日天到、⟦5⟧日线。" + LANE + "服务费加收⟦5⟧%、⟦3⟧-⟦5⟧天到、⟦5⟧号线。",
    "ＫＶＬＴ + NRDX 的组合，USD 2.4m。": "kvlt+nrdx的组合、usd⟦2400000⟧。",
}


def test_searchable_is_pinned() -> None:
    assert {text: searchable(text) for text in PINNED} == PINNED


# ---------- 别名表 ----------


def test_lexicon_types_match_l3() -> None:
    assert tuple(LEXICON_TYPES) == tuple(ENTITY_TYPES)


def test_lexicon_loads(tmp_path: Path) -> None:
    path = tmp_path / "lexicon.yaml"
    path.write_text("aliases:\n  - {type: 公司或机构, names: [河谷, 河谷车行]}\nwords: [乔一川]\n", encoding="utf-8")
    lexicon = load_lexicon(path)
    assert lexicon.words == ("乔一川", "河谷", "河谷车行")
    assert lexicon.same_as(name_key("河谷"), "公司或机构", name_key) == {name_key("河谷车行")}
    assert lexicon.same_as(name_key("河谷"), "人", name_key) == frozenset()  # 只认写着的类型
    assert load_lexicon(None) is EMPTY


@pytest.mark.parametrize(
    "content",
    [
        "aliases: {type: 人}\n",
        "aliases:\n  - {type: 公司, names: [河谷, 河谷车行]}\n",
        "aliases:\n  - {type: 人, names: [乔一川]}\n",
        "aliases:\n  - {type: 人, names: [乔一川, '']}\n",
        "words: 乔一川\n",
        "others: [乔一川]\n",
        "aliases: [\n",
    ],
)
def test_lexicon_errors_do_not_show_names(tmp_path: Path, content: str) -> None:
    path = tmp_path / "lexicon.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_lexicon(path)
    assert "乔一川" not in str(error.value) and "河谷" not in str(error.value)


# ---------- 问话切词 ----------


def test_query_words(tmp_path: Path) -> None:
    cache = tmp_path / "jieba"
    assert query_words("乔一川签了吗", ["乔一川"], cache_dir=cache) == ["乔一川"]
    assert query_words("韩东林的报价是多少", ["韩东林"], cache_dir=cache) == ["韩东林", "报价"]
    assert query_words("韩东林的报价是多少", ["韩东林"], cache_dir=cache) == ["韩东林", "报价"]  # 第二次用缓存的
    assert query_words("5万场地费什么时候付", cache_dir=cache) == ["场地"]  # 金额切碎了丢掉，问话词丢掉


def test_tokenizers_are_kept_one_per_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """查询接口照身份切词（J5.5）：一个 slot（身份）一个分词器，第一次查时建、记在进程里；几个身份轮着查不互相挤掉，同一个
    身份的词典变了换一个新的、旧的不留；最多留 MAX_SLOTS 个，丢最久没用的。"""
    from collections import OrderedDict

    import jieba

    from javmem.zh import segment

    built: list[int] = []
    real = jieba.Tokenizer

    def counting(*args, **kwargs):
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(jieba, "Tokenizer", counting)
    monkeypatch.setattr(segment, "_slots", OrderedDict())
    cache = tmp_path / "jieba"
    people = {"desk": ["乔一川"], "books": ["韩东林"], "trial": ["岑小舟"]}
    for _ in range(2):
        for slot, words in people.items():
            assert query_words(f"{words[0]}签了吗", words, cache_dir=cache, slot=slot) == words
    assert len(built) == 3  # 三个身份轮着问两遍：一个身份只建一次
    assert query_words("乔一川和柏庭舟签了吗", ["乔一川", "柏庭舟"], cache_dir=cache, slot="desk") == ["乔一川", "柏庭舟"]
    assert len(built) == 4 and list(segment._slots) == ["books", "trial", "desk"]  # 词典变了：换一个新的，旧的不留
    monkeypatch.setattr(segment, "MAX_SLOTS", 2)
    query_words("乔一川签了吗", ["乔一川"], cache_dir=cache, slot="owner")
    assert list(segment._slots) == ["desk", "owner"]


# ---------- L5 拆名字 ----------


@pytest.mark.parametrize(
    ("name", "entity_type", "variants"),
    [
        ("Corvalis（松岚）", "公司或机构", ["Corvalis", "松岚"]),
        ("KVLT（Kovalt）", "资产", ["KVLT", "Kovalt"]),
        ("远帆（6127 HK）", "资产", ["远帆", "6127 HK"]),
        ("Morrow Holdings (4421 JT / 4421.T)", "公司或机构", ["Morrow Holdings", "4421 JT", "4421.T"]),
        ("岱川实业有限公司", "公司或机构", ["岱川实业"]),
        ("Halvorsen Co. Ltd.", "公司或机构", ["Halvorsen"]),
        ("沈禾苗（苗苗）/ Nora", "人", ["沈禾苗", "Nora", "苗苗"]),
        ("程一舟（Yizhou）", "人", ["程一舟", "Yizhou"]),
        ("岱川（香港）", "公司或机构", []),  # 两边都是中文
        ("Halvorsen（日本）", "公司或机构", []),  # 括号里是地方：是日本那家
        ("Morrow (广州分公司)", "公司或机构", []),  # 括号里含分支
        ("Harbor Lane (三期)", "项目", []),  # 括号里是期数
        ("NRDX（债券）", "资产", []),  # 括号里是品类
        ("赵六（采购经理）", "人", []),  # 括号里是职务
        ("Ivy (CFO)", "人", []),
        ("程一舟（北京）", "人", []),
        ("KVLT / NRDX 篮子", "资产", []),  # 资产里的斜杠是一篮子
        ("篮子（KVLT / NRDX）", "资产", ["篮子"]),  # 括号里几个代码是一篮子，不拆成代码
        ("KVLT/NRDX", "人", []),  # 连着写的斜杠不拆
        ("王五 / 孙八", "人", []),  # 两边都是中文：两个人
        ("Iris Wen / Iris Wu", "人", []),
        ("岱川控股", "公司或机构", []),  # 控股不算法律后缀
        ("Corvalis Korea", "公司或机构", []),
        ("Halvorsen (HK)", "公司或机构", []),  # 两个字母的不算代码
        ("x" * 200 + "（Yizhou）", "人", []),  # 太长的不拆
    ],
)
def test_name_variants(name: str, entity_type: str, variants: list[str]) -> None:
    assert name_variants(name, entity_type) == variants


def test_name_parts_kinds() -> None:
    parts = name_parts("Corvalis（松岚）", "公司或机构")
    assert (parts.outer, parts.inner, parts.codes) == (("Corvalis",), ("松岚",), ())
    parts = name_parts("Morrow Holdings (4421 JT / 4421.T)", "公司或机构")
    assert (parts.outer, parts.inner, parts.codes) == (("Morrow Holdings",), (), ("4421 JT", "4421.T"))
    parts = name_parts("沈禾苗（苗苗）/ Nora", "人")
    assert (parts.outer, parts.inner) == (("沈禾苗", "Nora"), ("苗苗",))


# ---------- 原文层的迁移（0010） ----------


def test_migration_fills_the_unified_text_of_old_sentences(tmp_path: Path) -> None:
    migrations = load_migrations()
    conn = connect(tmp_path / "ledger.db")
    migrate(conn, [m for m in migrations if m.version <= 9])
    conn.execute("PRAGMA foreign_keys = OFF")  # 只测原文索引，不登记包
    for number, text in enumerate(["场地费 5 万", "灯光 15万", "  "], 1):
        conn.execute(
            "INSERT INTO raw_sentences (sentence_id, packet_id, revision, seq, section_kind, section_no, line_no,"
            " sentence_no, text, speaker, speaker_role, said_at, said_at_epoch, origin)"
            " VALUES (?, 'P', 1, ?, 'raw', 1, ?, 1, ?, '我', 'owner', 't', 0, 'said')",
            (f"P#1.{number}.1", number, number, text),
        )
    assert migrate(conn) == [m.version for m in migrations if m.version > 9]

    def hits(word: str) -> list[str]:
        return [
            row[0]
            for row in conn.execute(
                "SELECT s.sentence_id FROM raw_sentences s WHERE s.id IN"
                " (SELECT rowid FROM raw_norm_fts WHERE raw_norm_fts MATCH ?) ORDER BY s.id",
                (f'"{search_key(word)}"',),
            )
        ]

    assert hits("5万") == ["P#1.1.1"]
    assert hits("15万") == ["P#1.2.1"]
    conn.execute("DELETE FROM raw_sentences WHERE sentence_id = 'P#1.1.1'")  # 补上的那一份也删得掉，索引不坏
    assert hits("5万") == []
    conn.execute("INSERT INTO raw_norm_fts (raw_norm_fts, rank) VALUES ('integrity-check', 1)")  # 连内容一起核
    conn.execute(
        "INSERT INTO raw_sentences (sentence_id, packet_id, revision, seq, section_kind, section_no, line_no,"
        " sentence_no, text, speaker, speaker_role, said_at, said_at_epoch, origin, norm)"
        " VALUES ('P#1.4.1', 'P', 1, 4, 'raw', 1, 4, 1, '   ', '我', 'owner', 't', 0, 'said', '')"
    )
    conn.execute("DELETE FROM raw_sentences WHERE sentence_id IN ('P#1.3.1', 'P#1.4.1')")  # norm 是空的也删得掉
    conn.execute("INSERT INTO raw_norm_fts (raw_norm_fts, rank) VALUES ('integrity-check', 1)")
    conn.close()


def test_new_sentences_get_the_unified_text_on_intake(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    migrate(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(raw_sentences)")}
    assert "norm" in columns
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO raw_sentences (sentence_id) VALUES ('x')")  # 别的列照旧必填
    conn.close()


# ---------- 审阅时抓到的误拦、误并（tests/fixtures/zh_review.yaml） ----------

REVIEW = Path(__file__).parent / "fixtures" / "zh_review.yaml"


def _review_cases() -> list[tuple[dict, int, dict]]:
    import yaml

    data = yaml.safe_load(REVIEW.read_text(encoding="utf-8"))
    return [(group, number, case) for group in data["groups"] for number, case in enumerate(group["cases"], 1)]


REVIEW_CASES = _review_cases()


@pytest.mark.parametrize(
    ("group", "number", "case"), REVIEW_CASES, ids=[f"{group['id']}-{number}" for group, number, _ in REVIEW_CASES]
)
def test_review_cases(request: pytest.FixtureRequest, tmp_path: Path, group: dict, number: int, case: dict) -> None:
    import test_zh_pairs as pairs  # 和 8 类成对小句同一套跑法

    kind = group["kind"]
    if kind == "l4":
        actual, codes = pairs._l4(group, case)
        if actual == "block" and case.get("by") and not any(code.split(":")[0] in case["by"] for code in codes):
            actual = f"block by {codes}"
    else:
        world = request.getfixturevalue("world")
        actual = pairs._owner(world, tmp_path, group, case) if kind == "owner" else pairs._pair(world, tmp_path, group, case, kind)
    assert actual == case["expect"], f"{group['id']}-{number}（{group['why']}）"
