"""别名表第二版（J3.6，D100）：格式、核对、按名字找组、Bot 名单、不建实体、不是同一个、指纹。

名字都是编的，从头写的。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from javmem.config import ConfigError
from javmem.pipeline.merge import name_key
from javmem.zh import lexicon as lex

BOT = "Bot"
FORMAT_V2 = "javmem-lexicon/2"

V2 = f"""format: {FORMAT_V2}
default_where: [investment, personal]
aliases:
  - type: 人
    names: [柏庭舟, 柏廷舟, Tingzhou, "柏庭舟（Tingzhou）"]
  - type: 人
    names: [Corbin Vale, Corbin]
    where: [investment]
  - type: 资产
    names: [KVLT, Kovalt]
    from_types: [公司或机构]
  - type: 公司或机构
    names: [鹭汀纺织]
    from_types: [人]
bots:
  - names: [desk-bot, Desk]
    plain: [Desk]
  - names: [管家]
    plain: [管家]
not_entities:
  names: [9号机]
  path_like: true
apart:
  - {{type: 人, names: [Corbin Vale, Corbin Ashe]}}
words: [青苇路]
"""


def lexicon_of(tmp_path: Path, text: str):
    path = tmp_path / "lexicon.yaml"
    path.write_text(text, encoding="utf-8")
    return lex.load_lexicon(path)


def test_v2_groups_are_found_by_name_type_and_partition(tmp_path: Path) -> None:
    lexicon = lexicon_of(tmp_path, V2)
    assert lexicon.version == 2
    group = lexicon.group_of("柏廷舟", "人", ["investment"])
    assert group is not None and group.standard == "柏庭舟"
    assert lexicon.group_of("柏 廷 舟", "人", ["personal"]) is group  # 比对写法去掉空白
    assert lexicon.group_of("柏廷舟", "公司或机构", ["investment"]) is None  # 类型不对
    assert lexicon.group_of("柏廷舟", "人", ["business"]) is None  # default_where 里没有这个分区
    assert lexicon.group_of("柏廷舟", "人", ["business", "investment"]) is None  # 窗口的分区都要在里面
    # 拆开以后每一段都是这一组的才算
    assert lexicon.group_of("柏廷舟 / Tingzhou", "人", ["investment"]) is group
    assert lexicon.group_of("柏廷舟（Tingzhou）", "人", ["investment"]) is group
    assert lexicon.group_of("柏廷舟 / 青苇路", "人", ["investment"]) is None
    # where 只认写着的分区
    assert lexicon.group_of("Corbin", "人", ["investment"]) is not None
    assert lexicon.group_of("Corbin", "人", ["personal"]) is None
    # from_types：写成公司或机构的也对到资产这一组
    kvlt = lexicon.group_of("Kovalt", "公司或机构", ["investment"])
    assert kvlt is not None and kvlt.type == "资产" and kvlt.standard == "KVLT"
    assert lexicon.group_of("Kovalt", "地点", ["investment"]) is None
    # 只改类型的一组可以只有一个名字
    assert lexicon.group_of("鹭汀纺织", "人", ["personal"]).type == "公司或机构"  # type: ignore[union-attr]
    assert set(lexicon.words) >= {"青苇路", "柏庭舟", "KVLT", "desk-bot", "管家"}


def test_dictionary_for_an_identity_only_has_names_it_can_see(tmp_path: Path) -> None:
    """J5.5（J5.4 复审顺带看到的，审阅 R5、R6）：查询接口切问话的词典照身份，只放规矩题 1 下它看得到的配置里的名字：它算数的
    那些组（组算数的分区和它看得到的有交集）的标准名、Bot 名单的标准名；组里、Bot 名单里别的写法要有看得到的出处（账本里的
    别名，查询那一边另加）。words 那一项照放，和看不到的名字写法一样的除外。第一版的组不算主人配置里写的名字，都不放。"""
    lexicon = lexicon_of(tmp_path, V2)
    assert lexicon.words_for(None) == ("青苇路", "柏庭舟", "Corbin Vale", "KVLT", "鹭汀纺织", "desk-bot", "管家")
    assert lexicon.words_for({"business", "dev"}) == ("青苇路", "desk-bot", "管家")
    assert lexicon.words_for({"personal"}) == ("青苇路", "柏庭舟", "KVLT", "鹭汀纺织", "desk-bot", "管家")  # 照 default_where
    assert lexicon.words_for({"investment"}) == lexicon.words_for(None)
    corbin = lexicon.group_by_key(name_key("Corbin Vale"), "人")
    assert corbin is not None
    assert lexicon.group_seen_by(corbin, {"business", "investment"}) and not lexicon.group_seen_by(corbin, {"personal"})
    assert lexicon.group_seen_by(corbin, None)
    v1 = lexicon_of(tmp_path, "aliases:\n  - {type: 人, names: [乔一川, 小乔]}\nwords: [青苇路, 小乔]\n")
    assert v1.words_for(None) == v1.words_for({"business"}) == ("青苇路",)
    anywhere = lexicon_of(tmp_path, f"format: {FORMAT_V2}\naliases:\n  - {{type: 人, names: [乔一川, 小乔]}}\nwords: [青苇路]\n")
    assert anywhere.words_for({"business"}) == ("青苇路", "乔一川")  # 没写 where 也没写 default_where：哪个分区都算
    listed = lexicon_of(tmp_path, f"format: {FORMAT_V2}\naliases:\n  - {{type: 人, names: [宋长江, 老宋], where: [finance]}}\n"
                                  "words: [宋长江, 老 宋, 青苇路]\n")
    assert listed.words_for({"business"}) == ("青苇路",)  # words 里也写了：看不到的照样不放（照比对写法比）
    assert listed.words_for({"finance"}) == ("宋长江", "青苇路")


def test_dictionary_for_an_identity_follows_merge_records(tmp_path: Path) -> None:
    """合并记录并进来的组照样看分区：记录里写了 where 的，别的分区的身份词典里没有它的名字。"""
    record = {"kind": "merge", "id": "M0001", "at": "2026-11-18T09:00:00+08:00", "by": "test", "note": "", "type": "人",
              "standard": "宋长江", "names": ["老宋"], "where": ["finance"]}
    (tmp_path / "lexicon.merges.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    lexicon = lexicon_of(tmp_path, f"format: {FORMAT_V2}\naliases:\n  - {{type: 人, names: [乔一川, 小乔]}}\n")
    assert lexicon.words_for({"business"}) == ("乔一川",)
    assert lexicon.words_for({"finance"}) == ("乔一川", "宋长江")


def test_v2_bots_plain_names_need_the_bot_type_or_the_speaker(tmp_path: Path) -> None:
    lexicon = lexicon_of(tmp_path, V2)
    assert lexicon.bot_of("desk-bot", "", "人", []).standard == "desk-bot"  # type: ignore[union-attr]
    assert lexicon.bot_of("Desk", "", "项目", []) is None  # 普通词，L3 没标成 Bot、窗口里它也没说话
    assert lexicon.bot_of("Desk", "", BOT, []).standard == "desk-bot"  # type: ignore[union-attr]
    assert lexicon.bot_of("Desk", "", "人", ["我", "desk-bot"]).standard == "desk-bot"  # type: ignore[union-attr]
    assert lexicon.bot_of("管家", "", "人", ["desk-bot"]) is None
    assert lexicon.bot_of("前台", "desk-bot", "人", []).standard == "desk-bot"  # type: ignore[union-attr]
    assert lexicon.bot_of("前台", "Desk", "人", ["desk-bot"]) is None  # 写法是普通词的不算


def test_v2_not_entities_and_paths(tmp_path: Path) -> None:
    lexicon = lexicon_of(tmp_path, V2)
    assert lexicon.not_entity("9号机") and lexicon.not_entity("9 号机") and lexicon.not_entity("9号机（旧的那台）")
    assert lexicon.not_entity(r"Z:\work\notes\周报.md") and lexicon.not_entity("run/batch_07.log")
    assert lexicon.not_entity("README.md") and lexicon.not_entity("~/drafts/plan")
    assert not lexicon.not_entity("柏庭舟") and not lexicon.not_entity("KVLT/NRDX") and not lexicon.not_entity("v3.0")
    assert lex.looks_like_path("./tools/check.ps1") and not lex.looks_like_path("No. 5")


def test_v2_apart(tmp_path: Path) -> None:
    lexicon = lexicon_of(tmp_path, V2)
    assert lexicon.apart_from(name_key("Corbin Vale"), "人") == {name_key("Corbin Ashe")}
    assert lexicon.apart_from(name_key("Corbin Vale"), "资产") == frozenset()


def test_v1_reads_as_before_and_v2_ignores_its_lookups(tmp_path: Path) -> None:
    v1 = lexicon_of(tmp_path, "aliases:\n  - {type: 人, names: [柏庭舟, 柏廷舟]}\n")
    assert v1.version == 1
    assert v1.same_as(name_key("柏廷舟"), "人", name_key) == {name_key("柏庭舟")}
    assert v1.group_of("柏廷舟", "人", ["investment"]) is None  # 第一版只用来找
    assert v1.bot_of("desk-bot", "", BOT, []) is None and not v1.not_entity("9号机")


def test_fingerprint_follows_what_l5_uses(tmp_path: Path) -> None:
    first = lexicon_of(tmp_path, V2).fingerprint
    assert lexicon_of(tmp_path, V2.replace("words: [青苇路]", "words: [青苇路, 白沙洲]")).fingerprint == first
    assert lexicon_of(tmp_path, V2.replace("[柏庭舟, 柏廷舟,", "[柏庭舟,")).fingerprint != first
    assert lexicon_of(tmp_path, V2.replace("path_like: true", "path_like: false")).fingerprint != first


@pytest.mark.parametrize(
    "change",
    [
        ("format: javmem-lexicon/2", "format: javmem-lexicon/3"),
        ("words: [青苇路]", "others: [青苇路]"),
        ("    names: [Corbin Vale, Corbin]\n", "    names: [Corbin Vale, 柏廷舟]\n"),  # 同一类型两组有同一个写法
        ("    names: [鹭汀纺织]\n    from_types: [人]\n", "    names: [鹭汀纺织, 柏廷舟]\n    from_types: [人]\n"),  # from_types 也算
        ("  - names: [管家]\n", "  - names: [管家, Kovalt]\n"),  # Bot 的写法也在组里
        ("  names: [9号机]\n", "  names: [9号机, Tingzhou]\n"),  # 不建实体的也在组里
        ("names: [Corbin Vale, Corbin Ashe]", "names: [Corbin Vale, Corbin]"),  # 不是同一个的在同一组
        ("    where: [investment]\n", "    where: [Investment]\n"),
        ("    where: [investment]\n", "    where: []\n"),
        ("    plain: [Desk]\n", "    plain: [Front]\n"),
        ("    from_types: [公司或机构]\n", "    from_types: [资产]\n"),
        ("    from_types: [公司或机构]\n", "    from_types: [机构]\n"),
        ("    names: [KVLT, Kovalt]\n", "    names: [KVLT]\n    color: red\n"),
        ("  path_like: true\n", "  path_like: yes please\n"),
        ("{type: 人, names: [Corbin Vale, Corbin Ashe]}", "{type: 人, names: [Corbin Vale]}"),
    ],
)
def test_v2_errors_do_not_show_names(tmp_path: Path, change: tuple[str, str]) -> None:
    old, new = change
    assert old in V2
    with pytest.raises(ConfigError) as error:
        lexicon_of(tmp_path, V2.replace(old, new))
    message = str(error.value)
    assert not any(name in message for name in ("柏庭舟", "柏廷舟", "Tingzhou", "Corbin", "Kovalt", "管家", "9号机", "Desk"))
