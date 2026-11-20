"""raw_search：query-api 第 3、4.5、5.5、6、7、8、9 节。

文档里的返回样例直接拿来比，一个字段都不能差。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from helpers import (
    CHAT_ID,
    CHAT_PACKET,
    MAIL_ID,
    MAIL_PACKET,
    QUERY_API,
    SHANGHAI,
    World,
    as_json,
    build_packet,
    fenced,
)

from javmem.cli import main
from javmem.raw import raw_search, withdraw_sentence

AS_OF = datetime(2026, 11, 10, 9, 30, tzinfo=SHANGHAI)
AS_OF_TEXT = "2026-11-10T09:30:00+08:00"
NEW_ID = "2026-11-10_0900_desk-bot"
NEW_PACKET = build_packet(
    stamp="2026-11-10_0900",
    created_at="2026-11-10T09:00:00+08:00",
    covers_from="2026-11-09T23:30:00+08:00",
    covers_to="2026-11-10T09:00:00+08:00",
    kind="adhoc",
    body="## 原话 · 08:50\n- [08:50] 我：新的报价单今天发给客户。\n",
)


@pytest.fixture
def stocked(world: World) -> World:
    """规范第 14 节的两个包，加文档 5.5 里刚到的那个包。"""
    world = world.at(AS_OF).as_caller("desk-bot")
    world.drop("inbox/desk-bot", CHAT_PACKET)
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.drop("inbox/desk-bot", NEW_PACKET)
    assert world.intake().count("accepted") == 3
    return world


def search(world: World, caller: str | None = None, **arguments: object) -> dict:
    target = world.as_caller(caller) if caller else world
    return as_json(raw_search(arguments, settings=target.settings))


def ids(result: dict) -> list[str]:
    return [item["id"] for item in result["items"]]


# ---- 文档里的样例 ----


def test_quotation_example_matches_the_doc_exactly(stocked: World) -> None:
    """5.5 “报价单”。抽取状态按文档设：刚到的还在排队，旧的抽完了。"""
    stocked.execute("UPDATE packets SET extraction = 'done' WHERE packet_id IN (?, ?)", CHAT_ID, MAIL_ID)
    call = json.loads(fenced(QUERY_API, "### 5.5", "```json", 0))
    expected = json.loads(fenced(QUERY_API, "### 5.5", "```json", 1))
    assert search(stocked, **call["arguments"]) == expected


def test_forbidden_example_matches_the_doc_exactly(stocked: World) -> None:
    """第 7 节：明着要 finance。"""
    call = json.loads(fenced(QUERY_API, "## 7.", "```json", 0))
    expected = json.loads(fenced(QUERY_API, "## 7.", "```json", 1))
    assert search(stocked, **call["arguments"]) == expected


def test_not_asked_example_matches_the_doc_exactly(stocked: World) -> None:
    """第 7 节：没明着要，当它不存在。"""
    payroll = build_packet("books-bot", domain="[finance]", sensitive="[payroll]", body="## 原话\n- [10:30] 我：工资单发了。\n")
    stocked.drop("inbox/books-bot", payroll)
    stocked.intake()
    call = json.loads(fenced(QUERY_API, "## 7.", "```json", 2))
    expected = json.loads(fenced(QUERY_API, "## 7.", "```json", 3))
    assert search(stocked, **call["arguments"]) == expected

    # 同样的调用换成 books-bot，就能查到
    result = search(stocked, "books-bot", **call["arguments"])
    assert result["result"] == "hit"
    assert result["items"][0]["scope"] == {"partitions": ["finance"], "sensitive": ["payroll"]}


# ---- 验收：两字人名和具体金额都命中 ----


@pytest.mark.parametrize("name", ["小李", "老周"])
def test_two_character_names_hit(stocked: World, name: str) -> None:
    result = search(stocked, query=name)
    assert result["result"] == "hit"
    assert all(name in item["content"] for item in result["items"])


@pytest.mark.parametrize(
    ("amount", "sentence_id"),
    [
        ("12.5", f"{CHAT_ID}#attachment.1.1"),  # 附件说明里的单价
        ("12.5 美元", f"{CHAT_ID}#attachment.1.1"),  # 两个词，两个字的走 LIKE
        ("6688", f"{CHAT_ID}#2.9.1"),  # 尾号
    ],
)
def test_specific_amounts_hit(stocked: World, amount: str, sentence_id: str) -> None:
    assert ids(search(stocked, query=amount)) == [sentence_id]


def test_new_packet_is_searchable_right_after_intake(world: World) -> None:
    world = world.as_caller("desk-bot")
    assert search(world, query="报价单")["ok"] is False  # 还没建库
    world.drop("inbox/desk-bot", NEW_PACKET)
    world.intake()
    result = search(world, query="报价单")
    assert ids(result) == [f"{NEW_ID}#1.1.1"]
    assert result["items"][0]["extraction"] == "queued"


# ---- 匹配 ----


def test_english_is_case_insensitive_in_both_paths(stocked: World) -> None:
    assert ids(search(stocked, query="WAREHOUSE")) != []
    assert ids(search(stocked, query="OK")) != []


def test_several_words_must_hit_the_same_sentence(stocked: World) -> None:
    assert ids(search(stocked, query="报价单 单价")) == [f"{CHAT_ID}#2.7.1", f"{MAIL_ID}#2.3.2"]
    assert ids(search(stocked, query="报价单 没有这个词")) == []
    # 两个字加四个字。第一条是后来引用的那一行
    assert ids(search(stocked, query="小李 越南那批")) == [f"{CHAT_ID}#3.2.2", f"{CHAT_ID}#2.1.1"]
    assert ids(search(stocked, query="小李 老周")) == []  # 不在同一句


def test_full_width_space_separates_words(stocked: World) -> None:
    assert ids(search(stocked, query="报价单　单价")) == [f"{CHAT_ID}#2.7.1", f"{MAIL_ID}#2.3.2"]


@pytest.mark.parametrize(
    "query",
    ['"报价单"', '报价"单', "NOT 报价单", "报价单*", "(报价单)", "报价单 OR 单价", "a:报价单", "^报价单", "%%", "__"],
)
def test_search_syntax_is_treated_as_plain_text(stocked: World, query: str) -> None:
    result = search(stocked, query=query)
    assert result["ok"] is True
    assert result["result"] == "none"


def test_like_wildcards_are_literal(world: World) -> None:
    world = world.as_caller("desk-bot")
    body = "## 原话\n- [10:30] 我：完成了 50% 的工作。\n- [10:31] 我：a_b 这个名字。\n- [10:32] 我：这是我的。\n"
    world.drop("inbox/desk-bot", build_packet(body=body))
    world.intake()
    assert ids(search(world, query="%的")) == [f"{CHAT_ID}#1.1.1"]  # % 是字面的，不然“我的”那句也算
    assert ids(search(world, query="0%")) == []  # 数不查到更长的数里（D77）
    assert ids(search(world, query="a_")) == [f"{CHAT_ID}#1.2.1"]
    assert ids(search(world, query="_b")) == [f"{CHAT_ID}#1.2.1"]


@pytest.mark.parametrize("query", ["", "   ", "我", "报价单 我", "a"])
def test_query_too_short(stocked: World, query: str) -> None:
    result = search(stocked, query=query)
    assert (result["ok"], result["error"]["code"]) == (False, "query_too_short")


def test_no_alias_table_is_used(stocked: World) -> None:
    """别名是抽取的结果。标准名只在包头和摘要里，原文层查不到。"""
    assert search(stocked, query="周建华")["result"] == "none"


# ---- 查什么 ----


def test_summary_is_not_searched(stocked: World) -> None:
    assert search(stocked, query="来自小李转述")["result"] == "none"


def test_unknown_heading_and_bad_lines_are_not_searched(world: World) -> None:
    world = world.as_caller("desk-bot")
    body = "## 原话\n- [10:30] 我：能查到的话。\n- [坏] 坏行里的词。\n\n## 备注\n备注里的词。\n"
    world.drop("inbox/desk-bot", build_packet(body=body))
    world.intake()
    assert search(world, query="能查到")["result"] == "hit"
    assert search(world, query="坏行里")["result"] == "none"
    assert search(world, query="备注里")["result"] == "none"
    assert (world.data / "archive" / "2026" / "11" / f"{CHAT_ID}.md").read_text(encoding="utf-8").count("坏行里") == 1


def test_attachment_note_result(stocked: World) -> None:
    item = search(stocked, query="价格表")["items"][0]
    assert item["id"] == f"{CHAT_ID}#attachment.1.1"
    evidence = item["evidence"][0]
    assert evidence["origin"] == "attachment_note"
    assert evidence["speaker"] == "desk-bot"
    assert evidence["speaker_role"] == "bot"
    assert evidence["said_at"] == "2026-11-08T23:30:00+08:00"
    assert evidence["position"] == {"section": "attachment", "line": 1, "sentence": 1}
    assert item["secondhand"] is True
    assert "email" not in evidence


@pytest.mark.parametrize(
    ("query", "field", "section"),
    [
        ("Shipment date", "subject", 1),
        ("tran.mai@example.org", "from", 1),
        ("li.ming@example.com", "to", 1),
        ("wang.fang@example.com", "cc", 1),
        ("报价单.pdf", "attachments", 2),
    ],
)
def test_mail_header_fields_are_searched(stocked: World, query: str, field: str, section: int) -> None:
    result = search(stocked, query=query, packet_id=MAIL_ID, limit=20)
    positions = [item["evidence"][0]["position"] for item in result["items"]]
    assert {"section": section, "field": field} in positions


def test_mail_header_hit_has_the_full_shape(stocked: World) -> None:
    result = search(stocked, query="报价单.pdf")
    item = result["items"][0]
    assert item["id"] == f"{MAIL_ID}#2.attachments"
    assert item["content"] == "报价单.pdf"
    assert item["evidence"][0]["speaker"] == "李明 <li.ming@example.com>"
    assert item["evidence"][0]["said_at"] == "2026-11-03T14:30:00+08:00"
    assert item["evidence"][0]["email"]["message_id"] == "<ship-0002@example.com>"
    assert item["quoted"] is False


def test_quoted_old_lines_are_marked(stocked: World) -> None:
    result = search(stocked, query="warehouse", packet_id=MAIL_ID)
    flags = {item["id"]: item["quoted"] for item in result["items"]}
    assert flags == {f"{MAIL_ID}#2.9.2": True, f"{MAIL_ID}#1.3.2": False}


def test_injected_instruction_is_just_a_result(stocked: World) -> None:
    item = search(stocked, query="系统提示")["items"][0]
    assert item["status"] == "pending"
    assert item["pending_reason"] == "not_checked"
    assert item["evidence"][0]["origin"] == "external"
    assert item["secondhand"] is True


# ---- 过滤条件 ----


def test_source_filter(stocked: World) -> None:
    assert all(i["evidence"][0]["source"] == "desk-bot" for i in search(stocked, query="报价单", source="desk-bot")["items"])
    assert all(i["evidence"][0]["source"] == "mail-sales-a" for i in search(stocked, query="报价单", source="mail-sales-a")["items"])
    assert search(stocked, query="报价单", source="no-such-bot")["result"] == "none"


def test_packet_filter(stocked: World) -> None:
    assert ids(search(stocked, query="报价单", packet_id=NEW_ID)) == [f"{NEW_ID}#1.1.1"]


def test_thread_filter(stocked: World) -> None:
    assert ids(search(stocked, query="单价", thread_id="<ship-0001@example.org>")) == [f"{MAIL_ID}#2.3.2"]
    assert search(stocked, query="单价", thread_id="<slot-0001@example.com>")["result"] == "none"


def test_speaker_filter(stocked: World) -> None:
    assert ids(search(stocked, query="报价单", speaker="小李")) == [f"{CHAT_ID}#2.7.1", f"{CHAT_ID}#2.6.1"]
    assert ids(search(stocked, query="单价", speaker="李明 <li.ming@example.com>")) == [f"{MAIL_ID}#2.3.2"]


def test_time_filter_takes_dates_in_the_service_timezone(stocked: World) -> None:
    assert ids(search(stocked, query="报价单", time_from="2026-11-10")) == [f"{NEW_ID}#1.1.1"]
    # 终点只写日期时，那一天整天都算
    november_8 = ids(search(stocked, query="报价单", time_from="2026-11-08", time_to="2026-11-08"))
    assert november_8 == [f"{CHAT_ID}#2.7.1", f"{CHAT_ID}#2.6.1"]
    assert search(stocked, query="报价单", time_to="2026-11-02")["result"] == "none"


def test_time_filter_takes_full_times_in_any_timezone(stocked: World) -> None:
    result = search(
        stocked,
        query="报价单",
        time_from="2026-11-08T02:45:00+00:00",
        time_to="2026-11-08T02:45:00+00:00",
    )
    assert ids(result) == [f"{CHAT_ID}#2.6.1"]  # 10:45+08:00，两端都含


def test_mail_time_compares_as_the_real_moment(world: World) -> None:
    """09:12+07:00 是北京时间 10:12，比 10:00+08:00 晚。不能按文本比。"""
    world = world.as_caller("desk-bot").at(AS_OF)
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.drop(
        "inbox/desk-bot",
        build_packet(
            stamp="2026-11-03_1800",
            created_at="2026-11-03T18:00:00+08:00",
            covers_from="2026-11-02T18:00:00+08:00",
            covers_to="2026-11-03T18:00:00+08:00",
            body="## 原话\n- [10:00] 我：Could we 这句在十点。\n",
        ),
    )
    world.intake()
    assert ids(search(world, query="Could we", limit=2)) == [f"{MAIL_ID}#2.9.1", f"{MAIL_ID}#1.3.1"]
    assert ids(search(world, query="Could we", limit=3))[2] == "2026-11-03_1800_desk-bot#1.1.1"


# ---- 新的在前、翻页 ----


def test_newest_first(stocked: World) -> None:
    times = [item["evidence"][0]["said_at"] for item in search(stocked, query="报价单")["items"]]
    assert times == sorted(times, reverse=True)


def test_paging_walks_through_everything_once(stocked: World) -> None:
    everything = ids(search(stocked, query="报价单", limit=100))
    assert len(everything) == 5
    seen = []
    cursor = None
    while True:
        arguments = {"query": "报价单", "limit": 2}
        if cursor:
            arguments["cursor"] = cursor
        page = search(stocked, **arguments)
        seen.extend(ids(page))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == everything


def test_last_page_has_no_cursor(stocked: World) -> None:
    assert search(stocked, query="报价单", limit=5)["next_cursor"] is None
    assert search(stocked, query="报价单", limit=4)["next_cursor"] == "c4"


def test_ties_are_stable_within_a_minute(world: World) -> None:
    world = world.as_caller("desk-bot")
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：词语甲。词语乙。\n"))
    world.intake()
    assert ids(search(world, query="词语")) == [f"{CHAT_ID}#1.1.2", f"{CHAT_ID}#1.1.1"]


# ---- 上下文 ----


def test_context_brings_neighbours_from_the_same_section(stocked: World) -> None:
    item = search(stocked, query="单价没动", context=1)["items"][0]
    assert [row["text"] for row in item["context"]["before"]] == ["[图片:报价单.jpg]"]
    assert [row["text"] for row in item["context"]["after"]] == ["物流后台的令牌是 [已隐去:令牌]，你先别用。"]
    assert item["context"]["before"][0] == {
        "speaker": "小李",
        "said_at": "2026-11-08T10:45:00+08:00",
        "origin": "said",
        "text": "[图片:报价单.jpg]",
    }


def test_context_stops_at_the_section_edge(stocked: World) -> None:
    item = search(stocked, query="明天提醒你", context=5)["items"][0]
    assert [row["text"] for row in item["context"]["before"]] == ["明天记得问下越南那批的出货。"]
    assert item["context"]["after"] == []


def test_context_of_a_mail_body_skips_the_header_fields(stocked: World) -> None:
    item = search(stocked, query="Hi Mai", context=2)["items"][0]
    assert item["context"]["before"] == []
    assert [row["text"] for row in item["context"]["after"]] == ["OK, Nov 15 works for us.", "报价单见附件，单价不变。"]


def test_context_of_a_header_hit_is_empty(stocked: World) -> None:
    item = search(stocked, query="报价单.pdf", context=2)["items"][0]
    assert item["context"] == {"before": [], "after": []}


def test_no_context_unless_asked(stocked: World) -> None:
    assert "context" not in search(stocked, query="单价没动")["items"][0]
    assert "context" not in search(stocked, query="单价没动", context=0)["items"][0]


def test_context_skips_withdrawn_sentences(stocked: World) -> None:
    stocked.execute("UPDATE raw_sentences SET withdrawn_at = ? WHERE sentence_id = ?", AS_OF_TEXT, f"{CHAT_ID}#2.6.1")
    item = search(stocked, query="单价没动", context=1)["items"][0]
    assert [row["text"] for row in item["context"]["before"]] == ["[外部:网页] 海运 11 月中旬运力偏紧。"]


# ---- 身份、分区、敏感标记 ----

# query-api 3.3 的七行：包头 → 看得到这个包的身份
VISIBILITY_TABLE = [
    ("prod", "[business]", None, None, {"desk-bot", "books-bot", "owner-console"}),
    ("prod", "[finance]", "[payroll]", None, {"books-bot", "owner-console"}),
    ("prod", "[business, finance]", None, None, {"desk-bot", "books-bot", "owner-console"}),
    ("prod", "[finance, investment]", None, None, {"books-bot", "owner-console"}),
    ("prod", "[business, finance]", "[payroll]", None, {"books-bot", "owner-console"}),
    ("prod", "[business]", None, "private", {"owner-console"}),
    ("test", "[business]", None, None, {"trial-bot"}),
]
IDENTITIES = ("desk-bot", "books-bot", "trial-bot", "owner-console")


@pytest.mark.parametrize(("env", "domain", "sensitive", "privacy", "visible_to"), VISIBILITY_TABLE)
def test_visibility_table(world: World, env: str, domain: str, sensitive: str | None, privacy: str | None, visible_to: set) -> None:
    fields: dict[str, object] = {"domain": domain, "env": env}
    if sensitive:
        fields["sensitive"] = sensitive
    if privacy:
        fields["privacy"] = privacy
    source = "books-bot" if env == "prod" else "trial-bot"
    folder = "inbox" if env == "prod" else "inbox-test"
    world.drop(f"{folder}/{source}", build_packet(source, body="## 原话\n- [10:30] 我：只有这个包有的词。\n", **fields))
    assert world.intake().count("accepted") == 1

    seen = {caller for caller in IDENTITIES if search(world, caller, query="只有这个包有的词")["result"] == "hit"}
    assert seen == visible_to


def test_scope_shows_all_partitions_on_the_packet(world: World) -> None:
    """D31：能看其中一个就看得到整个包，scope 写的是包上的全部分区。"""
    world.drop("inbox/books-bot", build_packet("books-bot", domain="[business, finance]", body="## 原话\n- [10:30] 我：两个分区的包。\n"))
    world.intake()
    item = search(world, "desk-bot", query="两个分区")["items"][0]
    assert item["scope"] == {"partitions": ["business", "finance"], "sensitive": []}


def test_asking_for_a_partition_you_cannot_see_is_forbidden_even_if_the_packet_has_it(world: World) -> None:
    world.drop("inbox/books-bot", build_packet("books-bot", domain="[business, finance]", body="## 原话\n- [10:30] 我：两个分区的包。\n"))
    world.intake()
    result = search(world, "desk-bot", query="两个分区", partitions=["finance"])
    assert result == {
        "ok": False,
        "tool": "raw_search",
        "error": {"code": "forbidden", "message": "这个身份不能查分区 finance。"},
    }


def test_partitions_argument_keeps_only_those(world: World) -> None:
    world.drop("inbox/books-bot", build_packet("books-bot", domain="[finance]", body="## 原话\n- [10:30] 我：财务的话。\n"))
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：业务的话。\n"))
    world.intake()
    both = search(world, "books-bot", query="的话")
    assert len(both["items"]) == 2
    assert ids(search(world, "books-bot", query="的话", partitions=["finance"])) == ["2026-11-08_2330_books-bot#1.1.1"]
    assert ids(search(world, "books-bot", query="的话", partitions=["business"])) == [f"{CHAT_ID}#1.1.1"]
    assert search(world, "books-bot", query="的话", partitions=[])["result"] == "none"


def test_owner_can_ask_for_any_partition(stocked: World) -> None:
    assert search(stocked, "owner-console", query="报价单", partitions=["no-such-partition"])["result"] == "none"
    assert search(stocked, "owner-console", query="报价单", partitions=["business"])["result"] == "hit"


def test_filter_is_inside_retrieval_not_after(world: World) -> None:
    """看不到的内容更新，也不能占掉 limit，翻页也不能露馅。"""
    for number in range(5):
        world.drop(
            "inbox/books-bot",
            build_packet(
                "books-bot",
                stamp=f"2026-11-08_233{number}",
                domain="[finance]",
                body=f"## 原话\n- [2{number % 2}:0{number}] 我：同样的词。\n",
            ),
        )
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [08:00] 我：同样的词。\n"))
    world.intake()
    result = search(world, "desk-bot", query="同样的词", limit=1)
    assert ids(result) == [f"{CHAT_ID}#1.1.1"]
    assert result["next_cursor"] is None


def test_invisible_and_missing_look_the_same(world: World) -> None:
    world.drop("inbox/books-bot", build_packet("books-bot", domain="[finance]", body="## 原话\n- [10:30] 我：财务的话。\n"))
    world.intake()
    world = world.at(AS_OF)
    hidden = search(world, "desk-bot", query="财务的话")
    missing = search(world, "desk-bot", query="根本没有的话")
    assert hidden == missing
    by_packet = search(world, "desk-bot", query="财务的话", packet_id="2026-11-08_2330_books-bot")
    by_no_packet = search(world, "desk-bot", query="财务的话", packet_id="2026-11-08_2330_no-such-bot")
    assert by_packet == by_no_packet


def test_private_needs_the_tag_or_star(world: World, tmp_path: Path) -> None:
    world.drop("inbox/desk-bot", build_packet(privacy="private", body="## 原话\n- [10:30] 我：私密的话。\n"))
    world.intake()
    assert search(world, "desk-bot", query="私密的话")["result"] == "none"
    owner = search(world, "owner-console", query="私密的话")
    assert owner["items"][0]["scope"]["sensitive"] == ["private"]
    assert owner["items"][0]["extraction"] == "skipped"

    world.settings.access_file.write_text(
        "identities:\n  reader: {env: prod, partitions: [business], sensitive: [private]}\n", encoding="utf-8"
    )
    assert search(world, "reader", query="私密的话")["result"] == "hit"


def test_every_tag_must_be_readable(world: World) -> None:
    world.settings.sources_file.write_text(
        "sources:\n  demo-bot: {env: prod, channel: chat, may_carry: [alpha, beta]}\n", encoding="utf-8"
    )
    world.settings.access_file.write_text(
        "identities:\n"
        "  one-tag: {env: prod, partitions: '*', sensitive: [alpha]}\n"
        "  two-tags: {env: prod, partitions: '*', sensitive: [alpha, beta]}\n"
        "  no-tag: {env: prod, partitions: '*', sensitive: []}\n",
        encoding="utf-8",
    )
    world.drop("inbox/demo-bot", build_packet("demo-bot", sensitive="[alpha, beta]", body="## 原话\n- [10:30] 我：两个标记。\n"))
    world.intake()
    assert search(world, "one-tag", query="两个标记")["result"] == "none"
    assert search(world, "no-tag", query="两个标记")["result"] == "none"
    assert search(world, "two-tags", query="两个标记")["result"] == "hit"


def test_config_changes_take_effect_immediately(stocked: World) -> None:
    assert search(stocked, query="报价单")["result"] == "hit"
    stocked.settings.access_file.write_text(
        "identities:\n  desk-bot: {env: prod, partitions: [research], sensitive: []}\n", encoding="utf-8"
    )
    assert search(stocked, query="报价单")["result"] == "none"


# ---- 测试身份只查测试库 ----


def test_test_identity_only_sees_the_test_library(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：正式库的话。\n"))
    world.drop("inbox-test/trial-bot", build_packet("trial-bot", env="test", body="## 原话\n- [10:30] 我：测试库的话。\n"))
    world.intake()

    prod = search(world, "desk-bot", query="库的话")
    test = search(world, "trial-bot", query="库的话")
    assert prod["library"] == "prod"
    assert test["library"] == "test"
    assert [item["content"] for item in prod["items"]] == ["正式库的话。"]
    assert [item["content"] for item in test["items"]] == ["测试库的话。"]


def test_same_packet_id_in_both_libraries_never_mix(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：正式的内容。\n"))
    world.drop("inbox-test/desk-bot", build_packet(env="test", body="## 原话\n- [10:30] 我：试跑的内容。\n"))
    world.intake()
    world.settings.access_file.write_text(
        "identities:\n"
        "  prod-reader: {env: prod, partitions: '*', sensitive: '*'}\n"
        "  test-reader: {env: test, partitions: '*', sensitive: '*'}\n",
        encoding="utf-8",
    )
    assert search(world, "prod-reader", query="试跑的")["result"] == "none"
    assert search(world, "test-reader", query="正式的")["result"] == "none"
    assert search(world, "test-reader", query="试跑的", packet_id=CHAT_ID)["result"] == "hit"


def test_owner_with_star_still_only_sees_prod(world: World) -> None:
    world.drop("inbox-test/trial-bot", build_packet("trial-bot", env="test", body="## 原话\n- [10:30] 我：测试库的话。\n"))
    world.intake()
    assert search(world, "owner-console", query="测试库")["result"] == "none"


# ---- 撤回和替换 ----


def test_withdrawn_packet_is_gone(stocked: World) -> None:
    stocked.execute("UPDATE packets SET withdrawn_at = ? WHERE packet_id = ?", AS_OF_TEXT, NEW_ID)
    assert f"{NEW_ID}#1.1.1" not in ids(search(stocked, query="报价单"))


def test_withdrawn_sentence_is_gone(stocked: World) -> None:
    stocked.execute("UPDATE raw_sentences SET withdrawn_at = ? WHERE sentence_id = ?", AS_OF_TEXT, f"{CHAT_ID}#2.7.1")
    assert f"{CHAT_ID}#2.7.1" not in ids(search(stocked, query="报价单"))


def test_replaced_revision_is_gone_and_evidence_says_the_new_revision(world: World) -> None:
    world = world.as_caller("desk-bot")
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：第一版的话。\n- [10:31] 我：两版都有的话。\n"))
    world.intake()
    assert search(world, query="第一版")["result"] == "hit"

    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：第二版的话。\n- [10:31] 我：两版都有的话。\n"))
    world.intake()
    assert search(world, query="第一版")["result"] == "none"
    assert search(world, query="第二版")["items"][0]["evidence"][0]["revision"] == 2
    both = search(world, query="两版都有")["items"]
    assert [item["evidence"][0]["revision"] for item in both] == [2]


def withdraw(world: World, sentence_id: str) -> None:
    """单条撤回的命令是 J2.11 的事。这里直接调它要用的那个函数。"""
    conn = world.ledger()
    try:
        assert withdraw_sentence(conn, sentence_id, AS_OF_TEXT)
    finally:
        conn.close()


def replace_body(world: World, revision: int, body: str) -> None:
    world.drop("inbox/desk-bot", build_packet(revision=revision, body=body))
    assert world.intake().count("replaced") == 1


def test_sentence_withdrawn_earlier_does_not_come_back_with_a_new_revision(world: World) -> None:
    """单条撤回的内容不会因为替换又回来（规范第 10 节）。"""
    world = world.as_caller("desk-bot")
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：撤回的那句。\n- [10:31] 我：留着的那句。\n"))
    world.intake()
    withdraw(world, f"{CHAT_ID}#1.1.1")
    assert search(world, query="撤回的那句")["result"] == "none"

    replace_body(world, 2, "## 原话\n- [10:29] 我：新加的。\n- [10:30] 我：撤回的那句。\n- [10:31] 我：留着的那句。\n")

    assert search(world, query="撤回的那句")["result"] == "none"
    assert search(world, query="留着的那句")["result"] == "hit"
    rows = world.rows("SELECT sentence_id, withdrawn_at FROM raw_sentences WHERE text = ?", "撤回的那句。")
    assert [(row["sentence_id"], row["withdrawn_at"]) for row in rows] == [(f"{CHAT_ID}#1.2.1", AS_OF_TEXT)]


def test_withdrawal_survives_a_version_without_that_sentence(world: World) -> None:
    """第二版没有这句，第三版又有了，还是不能回来。"""
    world = world.as_caller("desk-bot")
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：撤回的那句。\n"))
    world.intake()
    withdraw(world, f"{CHAT_ID}#1.1.1")

    replace_body(world, 2, "## 原话\n- [10:30] 我：别的内容。\n")
    replace_body(world, 3, "## 原话\n- [10:30] 我：撤回的那句。\n")

    assert search(world, query="撤回的那句")["result"] == "none"


def test_withdrawing_one_speaker_does_not_hide_the_others(world: World) -> None:
    """别人说的同样的话不受影响。"""
    world = world.as_caller("desk-bot")
    body = "## 原话\n- [10:30] desk-bot：好的。\n- [10:31] 我：好的。\n- [10:32] 小李：好的。\n"
    world.drop("inbox/desk-bot", build_packet(body=body))
    world.intake()
    withdraw(world, f"{CHAT_ID}#1.1.1")

    def speakers() -> list[str]:
        return sorted(item["evidence"][0]["speaker"] for item in search(world, query="好的")["items"])

    assert speakers() == sorted(["我", "小李"])
    replace_body(world, 2, body)
    assert speakers() == sorted(["我", "小李"])


def test_withdrawal_record_does_not_keep_the_text(world: World) -> None:
    """彻底删除原文之后，记录里也不能还留着这句话。"""
    world = world.as_caller("desk-bot")
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：撤回的那句。\n"))
    world.intake()
    withdraw(world, f"{CHAT_ID}#1.1.1")
    dump = json.dumps([dict(row) for row in world.rows("SELECT * FROM withdrawn_sentences")], ensure_ascii=False)
    assert "撤回的那句" not in dump
    assert CHAT_ID in dump


def test_withdrawing_an_unknown_sentence_says_so(world: World) -> None:
    world.drop("inbox/desk-bot", build_packet())
    world.intake()
    conn = world.ledger()
    try:
        assert withdraw_sentence(conn, "no-such-packet#1.1.1", AS_OF_TEXT) is False
    finally:
        conn.close()


def test_old_revision_stays_searchable_until_the_new_one_is_done(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """替换到一半断电：查到的还是旧版本，没有半截。"""
    from javmem.intake import _LibraryIntake

    class PowerCut(BaseException):
        pass

    def crash(*args: object, **kwargs: object) -> None:
        raise PowerCut

    world = world.as_caller("desk-bot")
    world.drop("inbox/desk-bot", build_packet(body="## 原话\n- [10:30] 我：第一版的话。\n"))
    world.intake()
    world.drop("inbox/desk-bot", build_packet(revision=2, body="## 原话\n- [10:30] 我：第二版的话。\n"))

    for method in ("_archive", "_finish"):
        with monkeypatch.context() as patch:
            patch.setattr(_LibraryIntake, method, crash)
            with pytest.raises(PowerCut):
                world.intake()
        assert search(world, query="第一版")["result"] == "hit"
        assert search(world, query="第二版")["result"] == "none"

    world.intake()
    assert search(world, query="第一版")["result"] == "none"
    assert search(world, query="第二版")["result"] == "hit"


def test_context_comes_from_the_same_moment_as_the_hit(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """查询到一半，收件把这个包换成了看不到的新版本。上下文不能把新版本的内容带出去。"""
    import javmem.raw.search as search_module

    world = world.as_caller("desk-bot")
    world.drop(
        "inbox/books-bot",
        build_packet("books-bot", body="## 原话\n- [10:30] 我：公开的话。\n- [10:31] 我：别的公开内容。\n"),
    )
    world.intake()
    secret = build_packet(
        "books-bot",
        revision=2,
        domain="[finance]",
        sensitive="[payroll]",
        body="## 原话\n- [10:30] 我：工资单的话。\n- [10:31] 我：工资明细一万二。\n",
    )
    real = search_module._find_rows

    def replace_after_the_hit(conn: object, identity: object, request: object) -> list:
        rows = real(conn, identity, request)
        world.drop("inbox/books-bot", secret)
        assert world.intake().count("replaced") == 1
        return rows

    monkeypatch.setattr(search_module, "_find_rows", replace_after_the_hit)
    item = search(world, query="公开的话", context=1)["items"][0]

    assert item["content"] == "公开的话。"
    assert item["scope"] == {"partitions": ["business"], "sensitive": []}
    assert [row["text"] for row in item["context"]["after"]] == ["别的公开内容。"]


def test_query_is_not_blocked_while_something_is_writing(stocked: World) -> None:
    """收件写账本的时候照样能查，查到的是提交过的内容。"""
    writer = stocked.ledger()
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE raw_sentences SET text = '还没提交的内容'")
        result = search(stocked, query="报价单")
        assert result["result"] == "hit"
        assert search(stocked, query="还没提交")["result"] == "none"
        writer.execute("ROLLBACK")
    finally:
        writer.close()


@pytest.mark.parametrize("state", ["queued", "running", "done", "failed", "skipped"])
def test_every_extraction_state_is_searchable(stocked: World, state: str) -> None:
    """不管抽到哪了都查得到，每条结果说明抽到哪了（第 9 节）。"""
    stocked.execute("UPDATE packets SET extraction = ? WHERE packet_id = ?", state, NEW_ID)
    item = search(stocked, query="新的报价单")["items"][0]
    assert item["extraction"] == state


# ---- 每条结果都带四样 ----


def test_every_result_has_the_four_things(stocked: World) -> None:
    result = search(stocked, query="单价", limit=100)
    assert result["items"]
    for item in result["items"]:
        assert item["kind"] == "raw"
        assert item["content"]
        assert item["status"] == "pending"
        assert item["pending_reason"] == "not_checked"
        assert item["valid_time"]["from"] == item["evidence"][0]["said_at"]
        assert item["valid_time"]["to"] is None
        assert item["valid_time"]["precision"] == "minute"
        assert len(item["evidence"]) == 1
        assert item["evidence_total"] == 1
        assert item["evidence"][0]["quote"] == item["content"]
        assert item["evidence"][0]["packet_id"]


# ---- 出错 ----


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": 123},
        {"query": "报价单", "limit": 0},
        {"query": "报价单", "limit": 101},
        {"query": "报价单", "limit": "5"},
        {"query": "报价单", "limit": True},
        {"query": "报价单", "context": 6},
        {"query": "报价单", "context": -1},
        {"query": "报价单", "time_from": "10 月以来"},
        {"query": "报价单", "time_from": "2026-13-01"},
        {"query": "报价单", "time_to": "2026-11-08T10:00:00"},  # 没带时区
        {"query": "报价单", "time_from": 20261108},
        {"query": "报价单", "time_from": "2026-11-09", "time_to": "2026-11-08"},
        {"query": "报价单", "partitions": "business"},
        {"query": "报价单", "partitions": [1]},
        {"query": "报价单", "source": 1},
        {"query": "报价单", "cursor": "第二页"},
        {"query": "报价单", "cursor": "c99999999999999999999999"},  # 写进 SQL 会溢出
        {"query": "报价单", "time_to": "9999-12-31"},  # 加一天会溢出
        {"query": "报价单", "no_such_argument": 1},
        {"query": "报\x00价单"},
        {"query": " ".join(["词语"] * 33)},
        # JSON 里写 "\ud800" 是合法转义，所以入参能带进来这种写不进库的字符
        {"query": "\ud800报价单"},
        {"query": "报价单", "speaker": "\ud800"},
        {"query": "报价单", "source": "\udce9"},
        {"query": "报价单", "packet_id": "\ud800"},
        {"query": "报价单", "thread_id": "\ud800"},
        {"query": "报价单", "partitions": ["\ud800"]},
    ],
)
def test_invalid_arguments(stocked: World, arguments: dict) -> None:
    result = raw_search(arguments, settings=stocked.settings)
    assert result["ok"] is False
    assert set(result) == {"ok", "tool", "error"}
    assert result["error"]["code"] == "invalid_argument"


def test_error_messages_have_no_memory_content(stocked: World) -> None:
    for arguments in ({"query": "报价单", "limit": 0}, {"query": "报价单", "partitions": ["finance"]}, {"query": "我"}):
        message = raw_search(arguments, settings=stocked.settings)["error"]["message"]
        assert "报价单" not in message


@pytest.mark.parametrize("caller", [None, "no-such-bot"])
def test_unknown_identity(stocked: World, caller: str | None) -> None:
    from dataclasses import replace

    settings = replace(stocked.settings, caller=caller)
    assert raw_search({"query": "报价单"}, settings=settings) == {
        "ok": False,
        "tool": "raw_search",
        "error": {"code": "unknown_identity", "message": "不认识这个身份。"},
    }


def test_denials_are_logged_without_the_query(stocked: World) -> None:
    from dataclasses import replace

    raw_search({"query": "只查一次的词"}, settings=replace(stocked.settings, caller="no-such-bot"))
    raw_search({"query": "只查一次的词", "partitions": ["finance"]}, settings=stocked.settings)
    search(stocked, query="只查一次的词")  # 普通查询不记

    lines = (stocked.data / "logs" / "query-denied.log").read_text(encoding="utf-8").strip().split("\n")
    records = [json.loads(line) for line in lines]
    assert [(r["caller"], r["tool"], r["code"]) for r in records] == [
        ("no-such-bot", "raw_search", "unknown_identity"),
        ("desk-bot", "raw_search", "forbidden"),
    ]
    assert all(r["time"] == AS_OF_TEXT for r in records)
    assert "只查一次的词" not in "\n".join(lines)


def test_unavailable_when_the_ledger_is_missing(world: World) -> None:
    result = search(world, "desk-bot", query="报价单")
    assert result["error"]["code"] == "unavailable"
    assert not world.settings.library("prod").db_path.exists()  # 查询不建库


def test_unavailable_when_the_ledger_is_not_up_to_date(stocked: World) -> None:
    stocked.execute("PRAGMA user_version = 1")
    assert search(stocked, query="报价单")["error"]["code"] == "unavailable"


# ---- 命令行 ----


def run_cli(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caller: str, *argv: str) -> tuple[int, dict]:
    monkeypatch.setenv("JAVMEM_CALLER", caller)
    code = main(["raw-search", *argv])
    return code, json.loads(capsys.readouterr().out)


def test_cli_finds_and_filters(stocked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, result = run_cli(stocked, monkeypatch, capsys, "desk-bot", "报价单", "--limit", "2", "--source", "desk-bot")
    assert code == 0
    assert result["library"] == "prod"
    assert len(result["items"]) == 2
    assert result["next_cursor"] == "c2"

    code, result = run_cli(stocked, monkeypatch, capsys, "desk-bot", "报价单", "--limit", "2", "--source", "desk-bot", "--cursor", "c2")
    assert code == 0
    assert len(result["items"]) == 1


def test_cli_two_character_name_and_amount(stocked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _, by_name = run_cli(stocked, monkeypatch, capsys, "desk-bot", "老周")
    assert by_name["result"] == "hit"
    _, by_amount = run_cli(stocked, monkeypatch, capsys, "desk-bot", "12.5")
    assert by_amount["result"] == "hit"


def test_cli_context_and_filters_reach_the_query(stocked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _, result = run_cli(
        stocked,
        monkeypatch,
        capsys,
        "desk-bot",
        "单价",
        "--context",
        "1",
        "--partition",
        "business",
        "--from",
        "2026-11-08",
        "--to",
        "2026-11-08",
        "--speaker",
        "小李",
        "--packet-id",
        CHAT_ID,
    )
    assert [item["id"] for item in result["items"]] == [f"{CHAT_ID}#2.7.1"]
    assert "context" in result["items"][0]


def test_cli_thread_filter(stocked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _, result = run_cli(stocked, monkeypatch, capsys, "desk-bot", "单价", "--thread-id", "<ship-0001@example.org>")
    assert [item["id"] for item in result["items"]] == [f"{MAIL_ID}#2.3.2"]


def test_cli_exit_code_when_refused(stocked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, result = run_cli(stocked, monkeypatch, capsys, "desk-bot", "报价单", "--partition", "finance")
    assert code == 1
    assert result["error"]["code"] == "forbidden"


def test_cli_has_no_way_to_pick_the_identity_or_library() -> None:
    from javmem.cli import build_parser

    parser = build_parser()
    subcommands = next(action for action in parser._actions if action.dest == "command")
    options = {option for action in subcommands.choices["raw-search"]._actions for option in action.option_strings}
    assert not {"--caller", "--identity", "--library", "--env"} & options


def test_cli_without_a_caller(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["raw-search", "报价单"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unknown_identity"
