"""memory_search 和给主图检索（J5.4）留的口子（J5.5，query-api 5.1、第 6 到 9 节）。样例都是编的（tests/query_world.py）。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from helpers import ACCESS_YAML, SOURCES_YAML, World, as_json
from query_world import NEW_ID, build_world
from test_write import day_packet, deliver, l3_fact, write

from javmem.config import Settings
from javmem.query import graphhook
from javmem.query.graphhook import GraphRequest, GraphUnavailable, Hit, merge_default
from javmem.query.tools import memory_search
from javmem.query.view import NO_NAME

FOUR = ("content", "status", "valid_time", "evidence")


@pytest.fixture(scope="module")
def qworld(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_world(tmp_path_factory.mktemp("query-search"))


def search(world: World, caller: str | None = "desk-bot", **arguments: object) -> dict:
    return as_json(memory_search(arguments, settings=replace(world.settings, caller=caller)))


def contents(answer: dict) -> list[str]:
    return [item["content"] for item in answer["items"]]


def ids_of(world: World, sql: str, *params: object) -> list[str]:
    return [row[0] for row in world.rows(sql, *params)]


# ---------- 账本视图答（主图还没接） ----------


def test_the_vietnam_order_question(qworld: World) -> None:
    """01 p4 的例子“越南那批订单最近怎么样”：主图没接，从账本视图答。"""
    answer = search(qworld, query="越南那批订单最近怎么样", limit=5)
    assert (answer["ok"], answer["tool"], answer["library"], answer["result"]) == (True, "memory_search", "prod", "hit")
    assert (answer["answered_from"], answer["degraded"]) == ("graph", ["graph"])
    assert "周建华 负责 越南那批订单" in contents(answer)
    assert "李明 负责 越南那批订单" not in contents(answer)  # 已经结束，默认只要现在有效的
    statuses = {item["content"]: (item["status"], item.get("pending_reason")) for item in answer["items"]}
    assert statuses["周建华说越南那批订单的舱位 14 日前订好"] == ("pending", "frozen_conflict")
    for item in answer["items"]:
        assert all(name in item for name in FOUR)
        assert item["kind"] in ("fact", "relation")
    assert "not_found" not in answer


def test_history_brings_back_ended_relations_and_replaced_facts(tmp_path) -> None:
    """include_history：结束了的关系、被替代的事实（D66，另带 superseded_by、superseded_at）。"""
    world = build_world(tmp_path / "w")
    world.execute("UPDATE facts SET superseded_by = (SELECT fact_id FROM facts WHERE attribute = '尾款'),"
                  " superseded_at = '2026-11-09T14:10:00+08:00' WHERE attribute = '定金'")
    now = search(world, query="越南那批订单最近怎么样", limit=50)
    old = search(world, query="越南那批订单最近怎么样", limit=50, include_history=True)
    assert "越南那批订单的定金到账" not in contents(now) and "李明 负责 越南那批订单" not in contents(now)
    assert "越南那批订单的定金到账" in contents(old) and "李明 负责 越南那批订单" in contents(old)
    (deposit,) = [item for item in old["items"] if item["content"] == "越南那批订单的定金到账"]
    assert deposit["fact"]["superseded_at"] == "2026-11-09T14:10:00+08:00"
    assert deposit["fact"]["superseded_by"] == ids_of(world, "SELECT fact_id FROM facts WHERE attribute = '尾款'")[0]
    assert all("superseded_by" not in item.get("fact", {}) for item in now["items"])


def test_without_an_entity_words_are_matched_against_fact_text(qworld: World) -> None:
    answer = search(qworld, query="出货")
    assert answer["answered_from"] == "graph"
    assert contents(answer) == ["客户说越南那批订单的出货可以推到 11 月 15 日"]
    assert answer["items"][0]["secondhand"] is True


def test_without_an_entity_at_least_half_of_the_words_must_match(qworld: World) -> None:
    """一句话里只有一个常见词对得上的，不拿事实凑（真数据上“没有”这种词会对上一大堆）；照文档回退原文层，任一词命中就算。"""
    loose = search(qworld, query="定金 完全 东西")
    assert loose["answered_from"] == "raw" and "越南订单的定金到账了。" in contents(loose)
    tight = search(qworld, query="定金 到账")
    assert (tight["answered_from"], contents(tight)) == ("graph", ["越南那批订单的定金到账"])


def test_time_range_and_partitions_filter_inside_the_search(qworld: World) -> None:
    november = search(qworld, query="越南那批订单最近怎么样", limit=50, time_from="2026-11-01")
    assert "越南那批订单的定金到账" not in contents(november)
    october = search(qworld, query="越南那批订单最近怎么样", limit=50, time_from="2026-10-01", time_to="2026-10-31")
    assert contents(october) == ["越南那批订单的定金到账"]
    narrowed = search(qworld, "owner-console", query="工资单", partitions=["business"])
    assert narrowed["result"] == "none"
    payroll = search(qworld, "owner-console", query="工资单", partitions=["finance"])
    assert contents(payroll) == ["十一月的工资单月底发"]


def test_time_filters_follow_the_valid_range_of_relations(qworld: World) -> None:
    """审阅第 4、5 条：结束日子那一天不算（4.4：含 from 不含 to）；开始日子不知道的，从第一次有人说起算。"""
    handover = search(qworld, query="越南那批订单", limit=50, include_history=True, time_from="2026-11-09")
    assert "李明 负责 越南那批订单" not in contents(handover)
    assert "周建华 负责 越南那批订单" in contents(handover)
    before = search(qworld, query="越南那批订单", limit=50, include_history=True, time_from="2026-11-01", time_to="2026-11-08")
    assert "李明 负责 越南那批订单" in contents(before) and "周建华 负责 越南那批订单" not in contents(before)
    long_ago = search(qworld, query="老周", limit=50, time_from="2020-01-01", time_to="2020-12-31")
    assert "周建华 任职于 示例贸易" not in contents(long_ago)  # 没写开始日子，9 月 25 日才第一次说起
    since = search(qworld, query="老周", limit=50, time_from="2026-09-25")
    assert "周建华 任职于 示例贸易" in contents(since)


def test_filters_are_applied_before_the_candidate_cap(qworld: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 7 条：先取前 N 条候选、再在 Python 里按时间、对上几个词筛，后面对得上的就悄悄丢了。上限调到 1 来试。"""
    from javmem.query import tools

    monkeypatch.setattr(tools, "MAX_CANDIDATES", 1)
    october = search(qworld, query="越南那批订单最近怎么样", limit=50, time_from="2026-10-01", time_to="2026-10-31")
    assert contents(october) == ["越南那批订单的定金到账"]  # 最早的那一条，按时间筛时一次多取
    both = search(qworld, query="定金 到账")
    assert contents(both) == ["越南那批订单的定金到账"]
    ended = search(qworld, query="越南那批订单", limit=50)
    assert "李明 负责 越南那批订单" not in contents(ended) and "周建华 负责 越南那批订单" in contents(ended)


# ---------- 看不到的叫法挤不动看得到的（J5.4 复审顺带看到的第 1 处，以后要修 J5.5 第 5 条） ----------

XIAOLI = {"key": "e1", "mention": "小李", "name": "李明", "type": "人"}
CUSTOMS = ["小李说报关单据交了。", "小李说报关费用付了。", "小李说报关时间定了。", "小李说报关行换了。",
           "小李说一号舱位订好了。", "小李说二号舱位订好了。"]


def customs_facts(window) -> dict:
    """一句一条事实（没写属性：不合并、不替代）。舱位那两条 L3 补全了全名，文字写成“李明……”：没配别名表、原文里只说
    “小李”，“李明”是 desk-bot 看不到的叫法。"""
    found = []
    for line in window.targets:
        text = line.text.rstrip("。")
        found.append(l3_fact(window, text, "李明" + text[2:] if "舱位" in text else text, subject="e1"))
    return {"entities": [XIAOLI], "facts": found}


def customs_world(world: World) -> World:
    """11 月 1 日到 4 日每天一条报关的，5 日、6 日两条舱位的（更新，文字里写着看不到的“李明”）。"""
    deliver(world, *(day_packet(f"2026-11-0{day}", f"我：{text}") for day, text in enumerate(CUSTOMS, 1)))
    write(world, customs_facts).close()
    return world.as_caller("desk-bot")


@pytest.mark.parametrize("dated", [False, True], ids=["no-time", "time"])
@pytest.mark.parametrize("cap", [1, 2, 3])
def test_hidden_names_do_not_push_visible_facts_out_of_the_ledger_pool(
    world: World, monkeypatch: pytest.MonkeyPatch, cap: int, dated: bool
) -> None:
    """没认出实体时，账本视图按原样的字对上一半的词、最近有人说的先拿上限那么多条，再换掉看不到的叫法数词。舱位那两条
    更新，原样的字里有“李明”：拿“李明”问，它们先占了名额、换掉以后又对不上，报关那几条就进不来了，和拿一个没有的名字
    问不一样（看不到的改变了看得到的名次和条数）。上限调小来试；按时间筛时的上限是 MAX_SCAN。"""
    from javmem.query import tools

    world = customs_world(world)
    monkeypatch.setattr(tools, "MAX_SCAN" if dated else "MAX_CANDIDATES", cap)
    extra = {"time_from": "2026-10-01"} if dated else {}

    def ask(query: str) -> tuple[object, list[str], object]:
        answer = search(world, query=query, limit=50, **extra)
        return answer["answered_from"], contents(answer), answer["next_cursor"]

    newest = ["小李说报关行换了", "小李说报关时间定了", "小李说报关费用付了"]
    assert ask("王小二 报关") == ("graph", newest[:cap], None)  # 没有的名字：最近有人说的那几条报关
    assert ask("李明 报关") == ask("王小二 报关")  # 看不到的叫法和没有的名字一样


def test_the_ledger_pool_takes_at_most_max_pages(world: World, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """往下取有个头（MAX_PAGES 页）：一页一条、最多两页，两页都是带着“李明”的，报关那几条就够不着了（和 J5.4 全文那一路
    一样，取到头还不够的少几条），标准错误记一行，不带问话。"""
    from javmem.query import tools

    world = customs_world(world)
    monkeypatch.setattr(tools, "MAX_CANDIDATES", 1)
    monkeypatch.setattr(tools, "MAX_PAGES", 2)
    assert search(world, query="李明 报关", limit=50)["answered_from"] == "raw"
    err = capsys.readouterr().err
    assert "取到 2 页还不够" in err and "李明" not in err and "报关" not in err
    monkeypatch.setattr(tools, "MAX_PAGES", 3)
    assert contents(search(world, query="李明 报关", limit=50)) == ["小李说报关行换了"]


@pytest.mark.parametrize("cap", [2, 3, 5])
def test_with_a_time_range_facts_out_of_it_still_take_their_places(world: World, monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    """按时间筛时名额照旧是“要看的条数”：时间段对不上的也占（和以前一样），只靠看不到的叫法对上的不占（审阅低第 3 条：
    原来的测试时间段都对得上）。到 11 月 2 日（只写日期的那一整天都算）只有报关那四条里最早的两条。"""
    from javmem.query import tools

    world = customs_world(world)
    monkeypatch.setattr(tools, "MAX_SCAN", cap)

    def ask(query: str) -> tuple[object, list[str], object]:
        answer = search(world, query=query, limit=50, time_to="2026-11-02")
        return answer["answered_from"], contents(answer), answer["next_cursor"]

    early = {3: ["小李说报关费用付了"], 5: ["小李说报关费用付了", "小李说报关单据交了"]}
    if cap in early:  # 最近的 cap 条报关里在时间段里的那几条
        assert ask("王小二 报关") == ("graph", early[cap], None)
    else:  # 最近的两条都在时间段以后：账本视图没有，回退原文层
        assert ask("王小二 报关")[0] == "raw"
    assert ask("李明 报关") == ask("王小二 报关")


def bare_world(root) -> World:
    """一个空的世界：文档里的登记表、身份配置，没有别名表。"""
    (root / "data").mkdir(parents=True)
    (root / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    (root / "access.yaml").write_text(ACCESS_YAML, encoding="utf-8")
    settings = Settings.from_env({"JAVMEM_DATA_DIR": str(root / "data"), "JAVMEM_SOURCES_FILE": str(root / "sources.yaml"),
                                  "JAVMEM_ACCESS_FILE": str(root / "access.yaml"), "JAVMEM_TIMEZONE": "+08:00"})
    return World((root / "data").resolve(), settings)


def answers(world: World, query: str, **arguments: object) -> tuple[object, list[str], object]:
    answer = search(world, query=query, limit=50, **arguments)
    return answer["answered_from"], contents(answer), answer["next_cursor"]


def trade_world(root, hidden_name: str, text: str) -> World:
    """原话只说“青苇贸易”；L3 给的标准名是 hidden_name（原文里没有，desk-bot 看不到），事实的字写成 text。"""
    world = bare_world(root)
    deliver(world, day_packet("2026-11-01", "我：青苇贸易的合同签了。"))
    trade = {"key": "e1", "mention": "青苇贸易", "name": hidden_name, "type": "公司或机构"}
    write(world, lambda window: {"entities": [trade], "facts": [
        l3_fact(window, line.text.rstrip("。"), text, subject="e1") for line in window.targets]}).close()
    return world.as_caller("desk-bot")


@pytest.mark.parametrize("hidden_name", ["苇塘商行", "Qingwei Trading"])
def test_words_that_only_the_replaced_name_has_count_too(tmp_path, hidden_name: str) -> None:
    """审阅 R1（反过来的那一种）：原话只说“青苇贸易”，L3 在事实的字里写了看不到的全名，换掉以后是“青苇贸易的合同签了”，和 L3
    照原话写的那一条看起来一字不差。可 SQL 原来只按原样的字数词，拿“贸易”问，前一种漏了（退回原文层）、后一种查得到：L3 字里
    写的是哪个名字（看不到的）改变了看得到的结果。换上去的名字里有、原样的字里没有的词也要算。拉丁字母的带空格也认得。"""
    masked = trade_world(tmp_path / "masked", hidden_name, f"{hidden_name}的合同签了")
    plain = trade_world(tmp_path / "plain", hidden_name, "青苇贸易的合同签了")
    assert answers(masked, "合同") == answers(plain, "合同") == ("graph", ["青苇贸易的合同签了"], None)
    for query in ("贸易", "贸易 合同 价格"):
        assert answers(masked, query) == answers(plain, query) == ("graph", ["青苇贸易的合同签了"], None), query


def test_many_entities_whose_shown_name_has_the_word_do_not_break_the_search(tmp_path) -> None:
    """复审 N1：“换掉以后才对上”那一关原来每个实体拼一段 OR，名字里带这个词、又有看不到的叫法的实体一多，SQL 就慢得厉害，
    过了一千段 SQLite 报表达式太深，memory_search 答“账本连不上”（慢不慢、错不错还跟着看不到的叫法有多少变）。现在先按实体的
    索引挑出事实的编号，SQL 里一个词只有一个 IN。照上面那个世界直接在账本里复制 1,200 个这样的实体（标准名看不到，看得到的
    别名里有“贸易”），问“贸易”照样答得出原来那一条。"""
    world = trade_world(tmp_path / "w", "Qingwei Trading", "Qingwei Trading的合同签了")
    conn = world.ledger()
    try:
        entity = dict(conn.execute("SELECT * FROM entities WHERE canonical_name = 'Qingwei Trading'").fetchone())
        alias = dict(conn.execute("SELECT * FROM aliases WHERE entity_id = ? AND alias = '青苇贸易'", (entity["entity_id"],)).fetchone())
        evidence = dict(conn.execute("SELECT * FROM evidence WHERE alias_id = ?", (alias["alias_id"],)).fetchone())

        def insert(table: str, row: dict) -> None:
            conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))

        for number in range(1200):
            entity_id, alias_id = f"EZ{number:014d}", f"AZ{number:014d}"
            insert("entities", {**entity, "entity_id": entity_id, "canonical_name": f"Qingwei Trading {number}",
                                "name_key": f"qingweitrading{number}"})
            insert("aliases", {**alias, "alias_id": alias_id, "entity_id": entity_id, "alias": f"青苇{number}号贸易",
                               "alias_key": f"青苇{number}号贸易"})
            insert("evidence", {**evidence, "evidence_id": None, "alias_id": alias_id})
        conn.commit()
    finally:
        conn.close()
    assert answers(world, "贸易") == ("graph", ["青苇贸易的合同签了"], None)
    assert answers(world, "贸易 合同") == ("graph", ["青苇贸易的合同签了"], None)


def test_the_no_visible_name_placeholder_is_not_counted_as_words(world: World) -> None:
    """审阅 R1b：一个看得到的叫法都没有的实体，字里换成“[没有看得到的叫法]”。这几个字不是记忆的内容，不拿去数词：问“没有 报关”
    和问一个没有的名字加“报关”排的一样（原来占位符里的“没有”多算一个词，旧的那条排到了前面）。"""
    deliver(world, day_packet("2026-11-01", "我：他说报关单据交了。"), day_packet("2026-11-02", "我：小王说报关费用付了。"))
    nobody = {"key": "e1", "mention": "李明", "name": "李明", "type": "人"}  # 原话里没有这个名字：别名记不下来

    def extract(window) -> dict:
        facts, entities = [], []
        for line in window.targets:
            text = line.text.rstrip("。")
            if text.startswith("他"):
                entities = [nobody]
                facts.append(l3_fact(window, text, "李明" + text[1:], subject="e1"))
            else:
                facts.append(l3_fact(window, text, text))
        return {"entities": entities, "facts": facts}

    write(world, extract).close()
    world = world.as_caller("desk-bot")
    baseline = answers(world, "王小二 报关")
    assert baseline == ("graph", ["小王说报关费用付了", f"{NO_NAME}说报关单据交了"], None)  # 新的在前
    assert answers(world, "没有 报关") == baseline


# ---------- 切词的词典照身份（J5.4 复审顺带看到的第 2 处，以后要修 J5.5 第 6 条） ----------

RIVER_LEXICON = """\
format: javmem-lexicon/2
aliases:
  - {type: 人, names: [宋长江, 老宋], where: [finance]}
"""
NO_GROUP_LEXICON = "format: javmem-lexicon/2\n"


def river_facts(window) -> dict:
    return {"entities": [], "facts": [l3_fact(window, line.text.rstrip("。"), line.text.rstrip("。")) for line in window.targets]}


def test_names_of_groups_an_identity_cannot_see_stay_out_of_its_dictionary(world: World, tmp_path) -> None:
    """切词的词典原来把别名表里所有组的名字都放进去：“宋长江”在词典里是一个词，不在就切成“长江”。那一组只在 finance 算数，
    desk-bot 看不到 finance，可拿“宋长江”问，答的跟着别名表里有没有它变，就能试出别名表里有没有这个它看不到的名字。照身份
    只放它看得到的那些组的名字；看得到 finance 的 books-bot 照样当一个词切。"""
    deliver(world, day_packet("2026-11-01", "我：货走长江航线。"))
    write(world, river_facts).close()
    with_group, without = tmp_path / "with.yaml", tmp_path / "without.yaml"
    with_group.write_text(RIVER_LEXICON, encoding="utf-8")
    without.write_text(NO_GROUP_LEXICON, encoding="utf-8")

    def ask(caller: str, lexicon) -> tuple[object, list[str]]:
        settings = replace(world.settings, caller=caller, lexicon_file=lexicon)
        answer = as_json(memory_search({"query": "宋长江"}, settings=settings))
        return answer["answered_from"], contents(answer)

    assert ask("desk-bot", without) == ("graph", ["货走长江航线"])  # 不在词典里：切成“长江”
    assert ask("desk-bot", with_group) == ask("desk-bot", without)  # 看不到的那一组的名字不进 desk-bot 的词典
    assert ask("books-bot", with_group) == (None, [])  # books-bot 看得到 finance：“宋长江”是一个词，对不上“长江”
    assert ask("books-bot", without) == ("graph", ["货走长江航线"])


def test_other_names_of_a_group_need_visible_evidence_to_be_in_the_dictionary(world: World, tmp_path) -> None:
    """审阅 R6：desk-bot 看得到这一组（business），规矩题 1 只把组的标准名“宋立国”当主人配置里写的名字；“宋长江”只在管账那一包
    （desk-bot 看不到）里说过，对 desk-bot 是看不到的叫法（事实的字里换掉了），不能因为写在别名表的组里就进它的词典。"""
    deliver(world, day_packet("2026-11-01", "我：宋立国说货走长江航线。"))
    deliver(world, day_packet("2026-11-02", "我：宋长江下周来核一下账。", source="books-bot", domain="[finance]"),
            folder="inbox/books-bot")

    def extract(window) -> dict:
        entities, facts = [], []
        for line in window.targets:
            text = line.text.rstrip("。")
            mention = "宋立国" if "宋立国" in text else "宋长江"
            entities.append({"key": "e1", "mention": mention, "name": "宋立国", "type": "人"})
            facts.append(l3_fact(window, text, "宋长江说货走长江航线" if mention == "宋立国" else text, subject="e1"))
        return {"entities": entities, "facts": facts}

    write(world, extract).close()
    seen, without = tmp_path / "seen.yaml", tmp_path / "without.yaml"
    seen.write_text("format: javmem-lexicon/2\naliases:\n  - {type: 人, names: [宋立国, 宋长江], where: [business, finance]}\n",
                    encoding="utf-8")
    without.write_text(NO_GROUP_LEXICON, encoding="utf-8")

    def ask(caller: str, lexicon, query: str) -> tuple[object, list[str]]:
        answer = as_json(memory_search({"query": query}, settings=replace(world.settings, caller=caller, lexicon_file=lexicon)))
        return answer["answered_from"], contents(answer)

    assert ask("desk-bot", seen, "航线") == ("graph", ["宋立国说货走长江航线"])  # 对 desk-bot 看不到：字里换掉了
    assert ask("desk-bot", seen, "宋长江") == ask("desk-bot", without, "宋长江")


def test_first_version_groups_need_visible_evidence_too(world: World, tmp_path) -> None:
    """审阅 R5：第一版别名表的组没有分区，名字也不算主人配置里写的名字（规矩题 1 只认第二版的组）：和别的叫法一样，要有看得到
    的出处才进词典。"""
    deliver(world, day_packet("2026-11-01", "我：货走长江航线。"))
    write(world, river_facts).close()
    v1, without = tmp_path / "v1.yaml", tmp_path / "without.yaml"
    v1.write_text("aliases:\n  - {type: 人, names: [宋长江, 老宋]}\n", encoding="utf-8")
    without.write_text(NO_GROUP_LEXICON, encoding="utf-8")
    for caller in ("desk-bot", "owner-console"):
        settings = replace(world.settings, caller=caller)
        found = [as_json(memory_search({"query": "宋长江"}, settings=replace(settings, lexicon_file=path))) for path in (v1, without)]
        assert [(one["answered_from"], contents(one)) for one in found] == [("graph", ["货走长江航线"])] * 2, caller


def test_the_dictionary_is_one_per_identity(qworld: World) -> None:
    """词典只看身份：入参 partitions 只筛结果，不改词典（一个身份一份，分词器照身份记在进程里）；别名照身份看得到的放。"""
    from javmem.access import load_identity
    from javmem.ledger import connect_readonly
    from javmem.query.tools import _dictionary
    from javmem.query.view import LedgerView

    def dictionary(caller: str, partitions: list[str] | None = None) -> list[str]:
        settings = replace(qworld.settings, caller=caller)
        identity = load_identity(settings)
        conn = connect_readonly(settings.library(identity.env).db_path)
        try:
            conn.execute("BEGIN")
            return _dictionary(LedgerView(conn, identity, settings, as_of=settings.now(), partitions=partitions))
        finally:
            conn.close()

    assert dictionary("owner-console", ["finance"]) == dictionary("owner-console")
    assert "工资单" in dictionary("books-bot") and "工资单" not in dictionary("desk-bot")  # 管账那一包里的别名


def test_each_identity_keeps_its_own_tokenizer(qworld: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """分词器照身份记在进程里：两个身份轮着问两遍，一个身份只建一次（词典不一样；不分身份的话一轮一建，一次半秒）。"""
    from collections import OrderedDict

    import jieba

    from javmem.zh import segment

    built: list[int] = []
    real = jieba.Tokenizer
    monkeypatch.setattr(jieba, "Tokenizer", lambda *args, **kwargs: built.append(1) or real(*args, **kwargs))
    monkeypatch.setattr(segment, "_slots", OrderedDict())
    for _ in range(2):
        for caller in ("desk-bot", "books-bot"):
            search(qworld, caller, query="报价单 定金")
    assert len(built) == 2


def test_paging(qworld: World) -> None:
    everything = search(qworld, query="越南那批订单最近怎么样", limit=50)
    first = search(qworld, query="越南那批订单最近怎么样", limit=2)
    second = search(qworld, query="越南那批订单最近怎么样", limit=2, cursor=first["next_cursor"])
    assert first["next_cursor"] == "c2"
    assert [i["id"] for i in first["items"] + second["items"]] == [i["id"] for i in everything["items"][:4]]
    assert everything["next_cursor"] is None
    past = search(qworld, query="越南那批订单最近怎么样", cursor="c40")  # 审阅第 6 条：翻过头了
    assert (past["result"], past["answered_from"], past["items"], past["next_cursor"]) == ("none", None, [], None)
    assert past["not_found"]["reason"] == "no_match"


# ---------- 回退原文层 ----------


def test_falls_back_to_raw_for_packets_not_extracted_yet(qworld: World) -> None:
    """第 9 节：还没抽完的时候问“新的报价单”，memory_search 从原文层答；两个词都命中的排在最前面。"""
    answer = search(qworld, query="新的报价单", limit=1)
    assert (answer["result"], answer["answered_from"], answer["degraded"]) == ("hit", "raw", ["graph"])
    (item,) = answer["items"]
    assert item["id"] == f"{NEW_ID}#1.1.1" and item["kind"] == "raw"
    assert (item["content"], item["extraction"], item["quoted"]) == ("新的报价单今天发给客户。", "queued", False)
    assert (item["status"], item["pending_reason"]) == ("pending", "not_checked")
    assert answer["next_cursor"] == "c1"
    rest = search(qworld, query="新的报价单", limit=20, cursor="c1")
    assert rest["items"] and all("报价单" in text for text in contents(rest))


def test_nothing_anywhere(qworld: World) -> None:
    for query in ("完全没有的东西", "的了吗", "？"):
        answer = search(qworld, query=query)
        assert (answer["result"], answer["answered_from"], answer["items"]) == ("none", None, [])
        assert answer["not_found"] == {"reason": "no_match", "searched": ["ledger", "raw"], "try": [], "message": "记忆里没有查到。"}
        assert answer["next_cursor"] is None


# ---------- 越权、测试库 ----------


def test_what_you_cannot_see_looks_like_nothing(qworld: World) -> None:
    hidden = search(qworld, query="工资单")
    assert hidden["result"] == "none" and hidden["not_found"]["searched"] == ["ledger", "raw"]
    seen = search(qworld, "books-bot", query="工资单")
    assert contents(seen) == ["十一月的工资单月底发"]
    assert seen["items"][0]["scope"] == {"partitions": ["finance"], "sensitive": ["payroll"]}


def test_errors(qworld: World) -> None:
    assert search(qworld, None, query="老周")["error"]["code"] == "unknown_identity"
    assert search(qworld, query="老周", partitions=["finance"])["error"] == {"code": "forbidden", "message": "这个身份不能查分区 finance。"}


def test_a_test_identity_only_sees_the_test_library(qworld: World) -> None:
    answer = search(qworld, "trial-bot", query="越南那批最近怎么样")
    assert answer["library"] == "test"
    assert contents(answer) == ["越南那批改到 11 月 20 日出货"]  # L3 补全的全名在测试库里没出处，照看得到的叫法写
    # 问话里含着看得到的叫法“越南那批”，认得出实体；回的文字照样不带看不到的全名
    assert contents(search(qworld, "trial-bot", query="越南那批订单最近怎么样")) == ["越南那批改到 11 月 20 日出货"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"query": "字" * 501},
        {"query": 5},
        {"query": "老周", "limit": 0},
        {"query": "老周", "limit": 51},
        {"query": "老周", "include_history": "yes"},
        {"query": "老周", "no_such_argument": 1},
        {"query": "老周", "time_from": "上周"},
        {"query": "老\x00周"},
    ],
)
def test_invalid_arguments(qworld: World, arguments: dict) -> None:
    answer = memory_search(arguments, settings=replace(qworld.settings, caller="desk-bot"))
    assert answer["ok"] is False and answer["error"]["code"] == "invalid_argument"


# ---------- 给 J5.4 的口子 ----------


class FakeSearcher:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.requests: list[GraphRequest] = []

    def search(self, request: GraphRequest) -> list[Hit]:
        self.requests.append(request)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return list(self.answer(request)) if callable(self.answer) else list(self.answer)  # type: ignore[arg-type]


@pytest.fixture
def plugged(monkeypatch: pytest.MonkeyPatch):
    """把 graphhook.open_searcher 换成给一个假的检索器，回传那个检索器。"""

    def plug(answer: object) -> FakeSearcher:
        searcher = FakeSearcher(answer)
        monkeypatch.setattr(graphhook, "open_searcher", lambda settings, library: searcher)
        return searcher

    return plug


def test_the_graph_order_wins_and_stale_or_hidden_hits_are_dropped(qworld: World, plugged) -> None:
    zhou = ids_of(qworld, "SELECT r.relation_id FROM relations AS r JOIN entities AS e ON e.entity_id = r.subject_entity_id"
                          " WHERE e.canonical_name = '周建华' AND r.relation_type = '负责'")[0]
    deposit = ids_of(qworld, "SELECT fact_id FROM facts WHERE attribute = '定金'")[0]
    payroll = ids_of(qworld, "SELECT fact_id FROM facts WHERE attribute = '发放时间'")[0]
    ended = ids_of(qworld, "SELECT relation_id FROM relations WHERE state = 'ended'")[0]
    searcher = plugged([Hit("fact", "F000000000000gone", 9), Hit("fact", deposit, 8), Hit("fact", payroll, 7),
                        Hit("relation", ended, 6), Hit("relation", zhou, 5), Hit("fact", deposit, 4)])
    answer = search(qworld, query="越南那批订单最近怎么样", limit=10)
    assert (answer["answered_from"], answer["degraded"]) == ("graph", [])
    assert [item["id"] for item in answer["items"]] == [deposit, zhou]  # 没了的、看不到的、已经结束的、重复的都丢掉


def test_the_request_carries_what_the_searcher_needs(qworld: World, plugged) -> None:
    without = search(qworld, query="越南那批订单最近怎么样", limit=3, cursor="c2", time_from="2026-10-01")  # 还没接上
    searcher = plugged(lambda request: merge_default([], request.ledger_hits))
    answer = search(qworld, query="越南那批订单最近怎么样", limit=3, cursor="c2", time_from="2026-10-01")
    (request,) = searcher.requests
    order = ids_of(qworld, "SELECT entity_id FROM entities WHERE canonical_name = '越南那批订单'")[0]
    assert (request.library, request.question, request.seeds) == ("prod", "越南那批订单最近怎么样", (order,))
    assert request.scopes == ((("business",), ()),)  # desk-bot 看得到的只有 business、没有敏感标记
    assert request.partitions is None and request.include_history is False and request.time_from is not None
    assert request.want == 2 + 3 + 1 + 20
    booking = ids_of(qworld, "SELECT fact_id FROM facts WHERE status = 'pending'")
    assert set(booking) <= {hit.id for hit in request.ledger_hits}  # 待确认的在账本视图找到的那一份里（图里没有待确认的）
    # 检索器只照账本视图的名次回：和没接上时一字不差，只差 degraded
    assert [item["id"] for item in answer["items"]] == [item["id"] for item in without["items"]]
    assert (answer["degraded"], without["degraded"]) == ([], ["graph"])
    books = plugged(lambda request: [])
    search(qworld, "owner-console", query="工资单", partitions=["finance"])
    assert books.requests[0].scopes == ((("finance",), ("payroll",)),)
    trial = plugged(lambda request: [])
    search(qworld, "trial-bot", query="越南那批")
    assert trial.requests[0].library == "test"


@pytest.mark.parametrize("failure", [GraphUnavailable("连不上"), RuntimeError("坏了")])
def test_a_graph_that_fails_falls_back_to_the_ledger_view(qworld: World, plugged, failure: BaseException, capsys) -> None:
    plugged(failure)
    answer = search(qworld, query="越南那批订单最近怎么样", limit=5)
    assert (answer["answered_from"], answer["degraded"]) == ("graph", ["graph"])
    assert "周建华 负责 越南那批订单" in contents(answer)
    if isinstance(failure, RuntimeError):
        assert "RuntimeError" in capsys.readouterr().err


def test_nothing_from_the_graph_falls_back_to_raw(qworld: World, plugged) -> None:
    plugged([])
    answer = search(qworld, query="新的报价单", limit=1)
    assert answer["answered_from"] == "raw" and answer["degraded"] == []
    empty = search(qworld, query="完全没有的东西")
    assert empty["not_found"]["searched"] == ["graph", "ledger", "raw"]


def test_merge_default_puts_graph_hits_first() -> None:
    graph = [Hit("fact", "F2", 3.0), Hit("relation", "R1", 2.0)]
    ledger = [Hit("relation", "R1", 9.0), Hit("fact", "F9", 1.0)]
    assert merge_default(graph, ledger) == [Hit("fact", "F2", 3.0), Hit("relation", "R1", 2.0), Hit("fact", "F9", 1.0)]


def test_the_hook_is_off_while_the_graph_is_not_set_up(qworld: World) -> None:
    """J5.4 接上以后：这本账没接上图、没设图的密码，照旧回 None（检索器那一边的测试在 test_graph_search.py）。"""
    assert graphhook.open_searcher(qworld.settings, "prod") is None
