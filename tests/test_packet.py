"""解析和校验：packet-spec 第 3 到 6 节、第 9 节。纯函数，不碰文件和库。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from helpers import (
    CHAT_ID,
    CHAT_PACKET,
    MAIL_ID,
    MAIL_PACKET,
    SHANGHAI,
    SOURCES_YAML,
    build_mail_packet,
    build_packet,
    file_name,
    mail_header_block,
)

from javmem.packet import Rejected, parse_packet, validate_packet
from javmem.packet.body import origin_of
from javmem.packet.errors import (
    BAD_HEADER,
    ENV_MISMATCH,
    NO_RAW_SECTION,
    NOT_UTF8,
    TIME_OUT_OF_RANGE,
    UNKNOWN_SOURCE,
)
from javmem.packet.model import (
    BAD_LINE,
    ID_TIME_MISMATCH,
    ODD_SPEAKER,
    SUSPECT_CONTINUATION,
    TIME_BACKWARDS,
    UNKNOWN_FIELD,
    UNKNOWN_HEADING,
    UNRECOGNIZED_LINE,
)
from javmem.raw import iter_sentences
from javmem.sources import load_registry


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory):
    path = tmp_path_factory.mktemp("config") / "sources.yaml"
    path.write_text(SOURCES_YAML, encoding="utf-8")
    return load_registry(path)


def parse(packet: str, name: str | None = None):
    return parse_packet(packet.encode("utf-8"), filename=name or file_name(packet))


def validate(packet: str | bytes, registry, *, directory="desk-bot", library="prod", name=None):
    raw = packet if isinstance(packet, bytes) else packet.encode("utf-8")
    filename = name or file_name(packet if isinstance(packet, str) else packet.decode("utf-8", "replace"))
    return validate_packet(raw, filename=filename, directory=directory, library=library, registry=registry)


def rejected_code(packet: str | bytes, registry, **kwargs) -> str:
    with pytest.raises(Rejected) as caught:
        validate(packet, registry, **kwargs)
    return caught.value.code


# ---- 规范第 14 节的两个例子 ----


def test_chat_example_is_accepted_without_warnings(registry) -> None:
    packet = validate(CHAT_PACKET, registry)
    assert packet.warnings == []
    header = packet.header
    assert header.packet_id == CHAT_ID
    assert header.revision == 1
    assert header.source == "desk-bot"
    assert header.channel == "chat"
    assert header.domain == ("business",)
    assert header.sensitive == ()
    assert header.entities == ("越南那批订单", "李明", "周建华")
    assert header.mailbox is None
    assert [len(section.utterances) for section in packet.sections] == [2, 9, 2]
    assert [note.text for note in packet.notes] == ["图片说明：一张价格表，单价 12.5 美元。"]


def test_mail_example_is_accepted_without_warnings(registry) -> None:
    packet = validate(MAIL_PACKET, registry, directory="mail-sales-a")
    assert packet.warnings == []
    assert packet.header.packet_id == MAIL_ID
    assert packet.header.mailbox == "li.ming@example.com"
    assert [section.number for section in packet.sections] == [1, 2, 3]
    first, second, third = packet.sections
    assert first.headers.thread_id == second.headers.thread_id != third.headers.thread_id
    assert second.headers.in_reply_to == "<ship-0001@example.org>"
    assert second.headers.references == ("<ship-0001@example.org>",)
    assert str(first.headers.sender) == "Tran Mai <tran.mai@example.org>"
    assert [str(address) for address in first.headers.cc] == ["王芳 <wang.fang@example.com>"]
    assert first.headers.date == datetime(2026, 11, 3, 9, 12, tzinfo=timezone(timedelta(hours=7)))
    assert second.headers.attachments == ("报价单.pdf",)
    assert first.body[2] == "Could we move the shipment to Nov 15? Our warehouse is full until then."


# ---- 编码和换行 ----


def test_bom_is_fine(registry) -> None:
    packet = validate(b"\xef\xbb\xbf" + CHAT_PACKET.encode("utf-8"), registry, name=f"{CHAT_ID}.md")
    assert packet.header.packet_id == CHAT_ID


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_any_newline_is_fine_and_hashes_the_same(registry, newline: str) -> None:
    other = validate(CHAT_PACKET.replace("\n", newline), registry, name=f"{CHAT_ID}.md")
    same = validate(CHAT_PACKET, registry)
    assert other.warnings == []
    assert other.content_hash == same.content_hash
    assert [len(s.utterances) for s in other.sections] == [2, 9, 2]


def test_not_utf8_says_which_line(registry) -> None:
    raw = CHAT_PACKET.replace("明天记得", "@@@@").encode("utf-8").replace(b"@@@@", b"\xb2\xe2\xca\xd4")
    with pytest.raises(Rejected) as caught:
        validate(raw, registry, name=f"{CHAT_ID}.md")
    assert caught.value.code == NOT_UTF8
    line = CHAT_PACKET.split("明天记得")[0].count("\n") + 1
    assert f"第 {line} 行" in caught.value.detail


def test_only_lf_splits_lines() -> None:
    """换页符和 U+2028 这些不是换行，不能把一句话切开。"""
    packet = build_packet(body="## 原话 · 10:30\n- [10:30] 我：前半句 后半句\x0c还有。\n")
    section = parse(packet).sections[0]
    assert section.utterances[0].lines[0].text == "前半句 后半句\x0c还有。"
    assert parse(packet).warnings == []


# ---- 包头 ----


@pytest.mark.parametrize(
    "field",
    ["schema", "created_at", "framework", "channel", "env", "kind", "covers_from", "covers_to", "domain", "privacy"],
)
def test_missing_required_field(field: str, registry) -> None:
    assert rejected_code(build_packet(drop=(field,)), registry) == BAD_HEADER


def test_missing_source_and_packet_id(registry) -> None:
    packet = build_packet(drop=("source",))
    assert rejected_code(packet, registry) == BAD_HEADER
    packet = build_packet(drop=("packet_id",))
    assert rejected_code(packet, registry, name=f"{CHAT_ID}.md") == BAD_HEADER


@pytest.mark.parametrize(
    "fields",
    [
        {"schema": "javmem-packet/2"},
        {"schema": "memory-packet/1.2"},
        {"env": "staging"},
        {"kind": "weekly"},
        {"channel": "fax"},
        {"privacy": "secret"},
        {"privacy": "[normal]"},
        {"domain": "[]"},
        {"domain": "business"},
        {"domain": "[gardening]"},
        {"domain": "[1]"},
        {"created_at": "2026-11-08T23:30:00"},
        {"created_at": "2026-11-08"},
        {"covers_from": "2026-11-07 23:30"},
        {"covers_to": "yesterday"},
        {"covers_from": "2026-11-09T00:00:00+08:00"},
        {"framework": "Demo_Chat"},
        {"revision": "0"},
        {"revision": "-1"},
        {"revision": "true"},
        {"revision": '"2"'},
        {"revision": "1.5"},
        {"revision": "null"},
        {"revision": "9223372036854775808"},  # 账本放不下
        {"sensitive": "payroll"},
        {"sensitive": "null"},
        {"sensitive": "[private]"},
        {"entities": "老周"},
        {"entities": '["\\uD800"]'},  # YAML 转义出来的孤立代理，写不进账本
        {"framework": '"\\uDCE9"'},
        {"continues": "上一包"},
        {"continues": "null"},
        {"mailbox": "[a, b]"},
    ],
)
def test_bad_header_values(fields: dict, registry) -> None:
    assert rejected_code(build_packet(**fields), registry) == BAD_HEADER


@pytest.mark.parametrize(
    ("packet_id", "name"),
    [
        ("2026-11-08-2330_desk-bot", None),  # 格式不对
        ("2026-13-08_2330_desk-bot", None),  # 没有 13 月
        ("2026-11-08_2561_desk-bot", None),  # 没有 25 点
        ("2026-11-08_2330_DESK-BOT", None),
        ("2026-11-08_2330_desk-bot_0", None),
        ("٢٠٢٦-11-08_2330_desk-bot", None),  # 只认半角数字
        ("2026-11-08_2330_books-bot", None),  # 和 source 对不上
        ("2026-11-08_2330_desk-bot", "2026-11-08_2331_desk-bot.md"),  # 和文件名对不上
        ("2026-11-08_2330_desk-bot", "2026-11-08_2330_desk-bot.MD"),
    ],
)
def test_bad_packet_id(packet_id: str, name: str | None, registry) -> None:
    packet = build_packet(packet_id=packet_id)
    assert rejected_code(packet, registry, name=name or f"{packet_id}.md") == BAD_HEADER


def test_suffix_on_packet_id_is_allowed(registry) -> None:
    packet = build_packet(packet_id="2026-11-08_2330_desk-bot_2")
    assert validate(packet, registry).header.packet_id == "2026-11-08_2330_desk-bot_2"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "没有 YAML 头\n",
        "---\nschema: javmem-packet/1\n",  # 没结束
        "---\nschema: [不合法\n---\n",  # 解析不了
        "---\n- 一个列表\n---\n",  # 不是字典
        "---\n---\n",
    ],
)
def test_bad_front_matter(text: str) -> None:
    with pytest.raises(Rejected) as caught:
        parse_packet(text.encode("utf-8"), filename=f"{CHAT_ID}.md")
    assert caught.value.code == BAD_HEADER


def test_duplicate_key_in_header_is_refused() -> None:
    """写了两行 sensitive，不能让后一行悄悄盖掉前一行。"""
    packet = build_packet(sensitive="[payroll]").replace("---\n\n", "sensitive: []\n---\n\n", 1)
    with pytest.raises(Rejected) as caught:
        parse(packet)
    assert caught.value.code == BAD_HEADER


def test_unwritable_character_in_a_field_name_is_refused() -> None:
    packet = build_packet().replace("---\n\n", '"\\uD800": 1\n---\n\n', 1)
    with pytest.raises(Rejected) as caught:
        parse(packet)
    assert caught.value.code == BAD_HEADER


def test_unknown_field_warns_with_its_name_and_line() -> None:
    """包头打错字，比如 sensitve，这个包就没有标记了。规范没说要拒，先提醒，要不要拒由主人定。"""
    packet = parse(build_packet(sensitve="[payroll]"))
    assert [(w.kind, w.line, w.detail) for w in packet.warnings] == [(UNKNOWN_FIELD, 14, "sensitve")]
    assert packet.header.sensitive == ()


def test_unknown_field_with_a_strange_name_is_not_copied() -> None:
    packet = parse(build_packet().replace("---\n\n", '"这句话不该出现": 1\n---\n\n', 1))
    assert [(w.kind, w.detail) for w in packet.warnings] == [(UNKNOWN_FIELD, "")]


def test_packet_id_time_should_come_from_covers_to() -> None:
    """规范第 4 节这么写，但拒收清单没列，所以只提醒。比如第 3 行是 packet_id。"""
    packet = parse(build_packet(stamp="2020-01-01_0000"))
    assert [(w.kind, w.line) for w in packet.warnings] == [(ID_TIME_MISMATCH, 3)]


def test_packet_id_time_is_compared_in_the_timezone_of_covers_to() -> None:
    packet = parse(build_packet(stamp="2026-11-08_2230", covers_to="2026-11-08T23:30:00+09:00"))
    assert [w.kind for w in packet.warnings] == [ID_TIME_MISMATCH]
    packet = parse(build_packet(stamp="2026-11-08_2330", covers_to="2026-11-08T23:30:00+09:00"))
    assert packet.warnings == []


def test_suffix_on_packet_id_does_not_change_the_time_check() -> None:
    assert parse(build_packet(packet_id="2026-11-08_2330_desk-bot_2")).warnings == []


def test_documented_optional_fields_do_not_warn() -> None:
    packet = parse(build_packet(sensitive="[]", entities="[老周]", continues="2026-11-07_2330_desk-bot", revision=2))
    assert packet.warnings == []


def test_yaml_error_does_not_echo_the_packet() -> None:
    packet = build_packet(note="[这句话不该出现在错误里")
    with pytest.raises(Rejected) as caught:
        parse(packet)
    assert "这句话" not in caught.value.detail
    assert "第" in caught.value.detail


def test_times_keep_their_own_timezone() -> None:
    header = parse(build_packet(covers_to="2026-11-08T23:30:00+08:00", created_at="2026-11-08T15:30:00Z")).header
    assert header.covers_to.utcoffset() == timedelta(hours=8)
    assert header.created_at.utcoffset() == timedelta(0)


def test_private_adds_the_reserved_tag() -> None:
    header = parse(build_packet(privacy="private", sensitive="[payroll]")).header
    assert header.tags == ("payroll", "private")
    assert parse(build_packet()).header.tags == ()


# ---- 对话原话 ----


def utterances(body: str, **fields):
    packet = parse(build_packet(body=body, **fields))
    return [u for section in packet.sections for u in section.utterances], packet.warnings


def test_clock_only_lines_take_the_date_and_zone_of_covers_to() -> None:
    items, _ = utterances(
        "## 原话\n- [10:30] 我：一句。\n",
        covers_from="2026-11-07T00:00:00+07:00",
        covers_to="2026-11-08T23:30:00+07:00",
    )
    assert items[0].said_at == datetime(2026, 11, 8, 10, 30, tzinfo=timezone(timedelta(hours=7)))


def test_full_date_lines_use_their_own_date() -> None:
    items, warnings = utterances("## 原话\n- [2026-11-07 23:58] 我：一句。\n")
    assert items[0].said_at == datetime(2026, 11, 7, 23, 58, tzinfo=SHANGHAI)
    assert warnings == []


def test_speaker_stops_at_the_first_colon_of_either_kind() -> None:
    items, _ = utterances("## 原话\n- [10:30] 我：时间是 11:30：不变。\n- [10:31] 小李: 收到：好。\n")
    assert (items[0].speaker, items[0].lines[0].text) == ("我", "时间是 11:30：不变。")
    assert (items[1].speaker, items[1].lines[0].text) == ("小李", "收到：好。")


def test_speaker_roles() -> None:
    items, _ = utterances("## 原话\n- [10:30] 我：一。\n- [10:31] desk-bot：二。\n- [10:32] 小李：三。\n")
    assert [u.speaker_role for u in items] == ["owner", "bot", "other"]


def test_continuation_and_quote_lines_stay_in_the_utterance() -> None:
    items, warnings = utterances(
        "## 原话\n- [16:05] 我：第一行。\n  第二行。\n\n  > [引用 10:30 我] 旧话。\n- [16:06] 小李：下一句。\n"
    )
    assert warnings == []
    assert [line.text for line in items[0].lines] == ["第一行。", "第二行。", "> [引用 10:30 我] 旧话。"]
    assert [line.quoted for line in items[0].lines] == [False, False, True]
    assert [u.number for u in items] == [1, 2]


@pytest.mark.parametrize(
    ("text", "origin"),
    [
        ("[转发 自 小李] 客户说可以推到 15 号", "relayed"),
        ("[外部:网页] 海运运力偏紧", "external"),
        ("[外部：网页] 全角冒号也认", "external"),
        ("[语音] 转写的文字", "said"),
        ("[语音][转发 自 小李] 连着写", "relayed"),
        ("[图片:报价单.jpg]", "said"),
        ("普通一句 [外部:网页] 标记不在行首", "said"),
        # 标记写得不规范也认。认多了只是当转述说，漏认了外部内容就成了本人的话
        ("[外部] 没写类型", "external"),
        ("[外部 网页] 写错了", "external"),
        ("[转发] 没写谁", "relayed"),
        ("[转发 小李] 少了自", "relayed"),
    ],
)
def test_markers_at_the_start_decide_the_origin(text: str, origin: str) -> None:
    assert origin_of(text) == origin


def test_marker_covers_the_continuation_lines_too() -> None:
    items, _ = utterances("## 原话\n- [10:41] desk-bot：[外部:网页] 第一句。第二句。\n  续行。\n")
    assert items[0].origin == "external"


def test_marker_on_the_first_continuation_counts_when_the_line_is_empty() -> None:
    items, _ = utterances("## 原话\n- [10:41] desk-bot：\n  [外部:网页] 内容在续行。\n")
    assert items[0].origin == "external"
    assert [line.text for line in items[0].lines] == ["[外部:网页] 内容在续行。"]


def test_quote_line_does_not_hide_the_marker_after_it() -> None:
    items, _ = utterances("## 原话\n- [10:41] desk-bot：\n  > [引用 10:30 我] 旧话。\n  [外部:网页] 内容。\n")
    assert items[0].origin == "external"


def test_indented_line_that_looks_like_someone_speaking_warns() -> None:
    """按规范还是续行，所以还是算上一句。但多半是写错了，要提醒。"""
    packet = parse(build_packet(body="## 原话\n- [10:30] 小李：一句。\n  - [10:31] 老周：另一个人说的。\n  - [x] 普通的列表：不提醒\n"))
    assert [line.text for line in packet.sections[0].utterances[0].lines] == [
        "一句。",
        "- [10:31] 老周：另一个人说的。",
        "- [x] 普通的列表：不提醒",
    ]
    assert [(w.kind, w.line) for w in packet.warnings] == [(SUSPECT_CONTINUATION, 18)]


@pytest.mark.parametrize("line", ["- [10:31] https://example.org/x 这个链接。", "- [10:31] 示例/群：一句。"])
def test_speaker_that_looks_like_a_url_only_warns(line: str) -> None:
    """D41：像网址的照旧只提醒，这句话还在。"""
    packet = parse(build_packet(body=f"## 原话\n{line}\n"))
    assert [w.kind for w in packet.warnings] == [ODD_SPEAKER]
    assert len(packet.sections[0].utterances) == 1


@pytest.mark.parametrize(
    "line",
    [
        "- [10:31] [外部:网页] 没写说话人。",  # 漏写了说话人，标记被切开当成了说话人
        "- [10:31] [转发 自 小李]：客户说可以。",
        "- [10:31] 小李[群主]：好。",
        "- [10:31] 小李]：好。",
    ],
)
def test_speaker_with_brackets_is_a_bad_line(line: str) -> None:
    """D41：说话人带方括号的算解析不了：不并进上一句，它的续行也不并，不进原文索引，也不抽取，归档里还在。"""
    raw = build_packet(body=f"## 原话\n- [10:30] 我：上一句。\n{line}\n  它的续行。\n- [10:32] 我：下一句。\n")
    packet = parse(raw)
    utterances = packet.sections[0].utterances
    assert [(u.number, u.speaker, [text.text for text in u.lines]) for u in utterances] == [
        (1, "我", ["上一句。"]),
        (2, "我", ["下一句。"]),
    ]
    assert [w.kind for w in packet.warnings] == [BAD_LINE, UNRECOGNIZED_LINE]
    assert [sentence.text for sentence in iter_sentences(packet)] == ["上一句。", "下一句。"]
    assert line in packet.text  # 归档的就是这份全文，这一行还在


@pytest.mark.parametrize("speaker", ["我", "desk-bot", "小李", "Tran Mai", "王总(示例贸易)"])
def test_ordinary_speakers_do_not_warn(speaker: str) -> None:
    assert parse(build_packet(body=f"## 原话\n- [10:30] {speaker}：一句。\n")).warnings == []


@pytest.mark.parametrize(
    "bad",
    [
        "- [25:61] 我：时间不对",
        "- [昨天] 我：时间不对",
        "- [10:30] 没有冒号",
        "- [10:30] ：没有说话人",
        "- [10:30 我：括号没关",
        "- [2026-13-01 10:30] 我：没有 13 月",
        "-[10:30] 我：少了空格",
    ],
)
def test_unparsable_line_is_not_merged_into_the_previous_one(bad: str) -> None:
    """不能让一句话算到别人头上：它和它的续行都不并进上一句。"""
    body = f"## 原话\n- [10:29] 小李：上一句。\n{bad}\n  这是坏行的续行。\n- [10:40] 我：下一句。\n"
    items, warnings = utterances(body)
    assert [line.text for line in items[0].lines] == ["上一句。"]
    assert [u.speaker for u in items] == ["小李", "我"]
    assert [w.kind for w in warnings] == [BAD_LINE, UNRECOGNIZED_LINE]


def test_other_unrecognized_line_is_a_continuation_with_a_warning() -> None:
    packet = parse(build_packet(body="## 原话\n- [10:29] 我：上一句。\n没有缩进的一行\n"))
    assert [line.text for line in packet.sections[0].utterances[0].lines] == ["上一句。", "没有缩进的一行"]
    assert [(w.kind, w.line) for w in packet.warnings] == [(UNRECOGNIZED_LINE, 18)]


def test_unrecognized_line_at_section_start_is_dropped_with_a_warning() -> None:
    packet = parse(build_packet(body="## 原话\n开头的一行\n  缩进的也一样\n- [10:29] 我：一句。\n"))
    assert [line.text for line in packet.sections[0].utterances[0].lines] == ["一句。"]
    assert [w.kind for w in packet.warnings] == [UNRECOGNIZED_LINE, UNRECOGNIZED_LINE]


def test_time_going_backwards_warns_across_sections() -> None:
    body = "## 原话 · 一\n- [10:30] 我：一。\n- [10:20] 我：二。\n\n## 原话 · 二\n- [10:10] 我：三。\n- [10:15] 我：四。\n"
    packet = parse(build_packet(body=body))
    assert [w.kind for w in packet.warnings] == [TIME_BACKWARDS, TIME_BACKWARDS]


def test_quote_lines_do_not_count_as_time_going_backwards() -> None:
    _, warnings = utterances("## 原话\n- [10:30] 我：一。\n  > [引用 09:00 我] 旧话。\n- [10:31] 我：二。\n")
    assert warnings == []


def test_section_without_any_valid_line_is_not_a_section() -> None:
    body = "## 原话 · 空的\n坏的一行\n\n## 原话 · 真的\n- [10:30] 我：一句。\n"
    packet = parse(build_packet(body=body))
    assert [section.number for section in packet.sections] == [1]
    assert packet.sections[0].utterances[0].lines[0].text == "一句。"


def test_empty_utterance_does_not_make_a_section() -> None:
    packet = parse(build_packet(body="## 原话\n- [10:30] 我：\n"))
    assert packet.sections == []


# ---- 小节 ----


def test_unknown_heading_warns_and_its_content_is_ignored() -> None:
    body = "## 原话\n- [10:30] 我：一句。\n\n## 备注\n- [10:31] 我：不算数。\n"
    packet = parse(build_packet(body=body))
    assert [len(section.utterances) for section in packet.sections] == [1]
    assert [w.kind for w in packet.warnings] == [UNKNOWN_HEADING]


def test_summary_is_not_parsed_and_gives_no_warning() -> None:
    body = "## 原话\n- [10:30] 我：一句。\n\n## 摘要（Bot 写的）\n- 随便写什么\n没有格式的一行\n"
    packet = parse(build_packet(body=body))
    assert packet.warnings == []
    assert packet.notes == []
    assert [len(section.utterances) for section in packet.sections] == [1]


def test_text_before_the_first_heading_warns() -> None:
    packet = parse(build_packet(body="多写的一行\n\n## 原话\n- [10:30] 我：一句。\n"))
    assert [w.kind for w in packet.warnings] == [UNRECOGNIZED_LINE]


def test_third_level_heading_is_just_a_line() -> None:
    packet = parse(build_packet(body="## 原话\n- [10:30] 我：一句。\n### 不是小节\n"))
    assert [w.kind for w in packet.warnings] == [UNRECOGNIZED_LINE]
    assert len(packet.sections) == 1


def test_attachment_notes_are_numbered_without_blank_lines() -> None:
    body = "## 原话\n- [10:30] 我：一句。\n\n## 附件\n\n- 第一条\n\n第二条没有横线\n"
    packet = parse(build_packet(body=body))
    assert [(note.number, note.text) for note in packet.notes] == [(1, "第一条"), (2, "第二条没有横线")]


# ---- 邮件 ----


def mail_section(block: str, *body: str) -> str:
    return "## 原话 · 邮件\n\n" + block + "\n" + "\n".join(body) + "\n"


def test_mail_body_lines_count_blanks_and_lose_their_indent() -> None:
    packet = parse(build_mail_packet(sections=mail_section(mail_header_block(), "  第一行", "", "  第三行", "    多缩进")))
    assert packet.sections[0].body == ["第一行", "", "第三行", "  多缩进"]
    assert packet.warnings == []


def test_structure_inside_a_mail_body_stays_body() -> None:
    packet = parse(build_mail_packet(sections=mail_section(mail_header_block(), "  ## 原话 · 假的", "  - [10:30] 我：假的")))
    assert len(packet.sections) == 1
    assert packet.sections[0].body == ["## 原话 · 假的", "- [10:30] 我：假的"]


def test_unindented_mail_line_is_kept_with_a_warning() -> None:
    packet = parse(build_mail_packet(sections=mail_section(mail_header_block(), "没有缩进")))
    assert packet.sections[0].body == ["没有缩进"]
    assert [w.kind for w in packet.warnings] == [UNRECOGNIZED_LINE]


def test_mail_with_empty_body_counts(registry) -> None:
    packet = validate(
        build_mail_packet(sections=f"## 原话 · 邮件\n\n{mail_header_block()}"), registry, directory="mail-sales-a"
    )
    assert packet.sections[0].body == []


def test_mail_without_display_name_is_just_the_address() -> None:
    block = mail_header_block(**{"from": '{name: null, addr: "a@example.org"}'})
    packet = parse(build_mail_packet(sections=mail_section(block, "  正文")))
    assert str(packet.sections[0].headers.sender) == "a@example.org"


def test_going_backwards_between_threads_is_not_a_warning_for_mail() -> None:
    """邮件按往来串排，时间本来就可能倒着走。"""
    later = mail_header_block(date="2026-11-03T15:00:00+08:00")
    earlier = mail_header_block(date="2026-11-03T09:00:00+08:00")
    packet = parse(build_mail_packet(sections=mail_section(later, "  甲") + "\n" + mail_section(earlier, "  乙")))
    assert packet.warnings == []


@pytest.mark.parametrize("field", ["message_id", "thread_id", "in_reply_to", "from", "to", "date", "subject"])
def test_mail_missing_a_required_header(field: str, registry) -> None:
    block = mail_header_block(**{field: None})
    packet = build_mail_packet(sections=mail_section(block, "  正文"))
    assert rejected_code(packet, registry, directory="mail-sales-a") == BAD_HEADER


@pytest.mark.parametrize(
    "overrides",
    [
        {"date": "2026-11-03T09:12:00"},
        {"date": "2026-11-03"},
        {"subject": "2026"},
        {"from": '"tran.mai@example.org"'},
        {"from": "{name: x}"},
        {"to": '"li.ming@example.com"'},
        {"cc": "[{name: x}]"},
        {"bcc": "[1]"},
        {"references": '"<a>"'},
        {"attachments": "[1]"},
        {"in_reply_to": "[a]"},
        {"message_id": '""'},
        {"thread_id": "1"},
        {"subject": '"bad-\\uDCE9"'},  # 导入工具按 surrogateescape 读 8 位邮件头就会写出这种
    ],
)
def test_mail_with_bad_header_values(overrides: dict, registry) -> None:
    packet = build_mail_packet(sections=mail_section(mail_header_block(**overrides), "  正文"))
    assert rejected_code(packet, registry, directory="mail-sales-a") == BAD_HEADER


@pytest.mark.parametrize(
    "section",
    [
        "## 原话 · 邮件\n\n  正文，没有邮件头\n",
        "## 原话 · 邮件\n\n```yaml\nmessage_id: x\n",  # 邮件头没结束
        "## 原话 · 邮件\n\n```yaml\n[不合法\n```\n",  # 解析不了
        "## 原话 · 邮件\n\n```yaml\n- 列表\n```\n",  # 不是字典
    ],
)
def test_mail_section_with_broken_header_block(section: str, registry) -> None:
    packet = build_mail_packet(sections=section)
    assert rejected_code(packet, registry, directory="mail-sales-a") == BAD_HEADER


def test_mail_packet_without_mailbox(registry) -> None:
    packet = build_mail_packet(drop=("mailbox",))
    assert rejected_code(packet, registry, directory="mail-sales-a") == BAD_HEADER


# ---- 查的顺序（第 9 节） ----


def test_encoding_comes_first(registry) -> None:
    raw = build_packet(env="staging").encode("utf-8") + b"\xff"
    assert rejected_code(raw, registry, name=f"{CHAT_ID}.md") == NOT_UTF8


def test_header_comes_before_source(registry) -> None:
    packet = build_packet("no-such-bot", env="staging")
    assert rejected_code(packet, registry) == BAD_HEADER


def test_source_comes_before_the_registry_items(registry) -> None:
    packet = build_packet("no-such-bot", mailbox="x@example.com", sensitive="[payroll]")
    assert rejected_code(packet, registry) == UNKNOWN_SOURCE


def test_source_must_match_the_directory(registry) -> None:
    assert rejected_code(build_packet("books-bot"), registry, directory="desk-bot") == UNKNOWN_SOURCE


def test_directory_mismatch_comes_before_the_registry_items(registry) -> None:
    """books-bot 登记过，但放错了目录，同时还带了它 may_carry 之外的标记：先报来源。"""
    packet = build_packet("books-bot", sensitive="[not-carried]")
    assert rejected_code(packet, registry, directory="desk-bot") == UNKNOWN_SOURCE


def test_reserved_tag_is_checked_before_source(registry) -> None:
    """保留字和登记表无关，在第 1 条就查，排在来源前面。"""
    packet = build_packet("no-such-bot", sensitive="[private]")
    assert rejected_code(packet, registry, directory="no-such-bot") == BAD_HEADER


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({"mailbox": "li.ming@example.com"}, "mailbox 和登记表不一样"),
        ({"sensitive": "[payroll]"}, "sensitive 里有这个来源 may_carry 之外的标记"),
    ],
)
def test_registry_items_are_bad_header_after_source_passes(fields: dict, detail: str, registry) -> None:
    with pytest.raises(Rejected) as caught:
        validate(build_packet(**fields), registry)
    assert (caught.value.code, caught.value.detail) == (BAD_HEADER, detail)


def test_channel_must_match_the_registry(registry) -> None:
    """desk-bot 登记的是对话来源，写成邮件包，邮件本身没问题也要拒。"""
    section = f"## 原话 · 邮件\n\n{mail_header_block()}\n  正文\n"
    packet = build_packet(channel="email", mailbox="li.ming@example.com", body=section)
    with pytest.raises(Rejected) as caught:
        validate(packet, registry)
    assert (caught.value.code, caught.value.detail) == (BAD_HEADER, "channel 和登记表不一样")


def test_mail_mailbox_must_match_the_registry(registry) -> None:
    packet = build_mail_packet(mailbox="someone.else@example.com")
    assert rejected_code(packet, registry, directory="mail-sales-a") == BAD_HEADER


def test_may_carry_allows_the_listed_tag(registry) -> None:
    packet = build_packet("books-bot", sensitive="[payroll]", domain="[finance]")
    assert validate(packet, registry, directory="books-bot").header.sensitive == ("payroll",)


def test_env_comes_before_raw_section(registry) -> None:
    packet = build_packet(env="test", body="## 摘要（Bot 写的）\n- 只有摘要\n")
    assert rejected_code(packet, registry) == ENV_MISMATCH


def test_raw_section_comes_before_time_range(registry) -> None:
    assert rejected_code(build_packet(body="## 摘要（Bot 写的）\n- 只有摘要\n"), registry) == NO_RAW_SECTION


@pytest.mark.parametrize(
    "body",
    [
        "",
        "## 摘要（Bot 写的）\n- 只有摘要\n",
        "## 原话\n",
        "## 原话\n不合规的一行\n",
        "## 原话\n- [99:99] 我：时间不对\n",
    ],
)
def test_no_raw_section(body: str, registry) -> None:
    assert rejected_code(build_packet(body=body), registry) == NO_RAW_SECTION


def test_mail_packet_without_any_mail(registry) -> None:
    packet = build_mail_packet(sections="## 摘要（Bot 写的）\n- 只有摘要\n")
    assert rejected_code(packet, registry, directory="mail-sales-a") == NO_RAW_SECTION


# ---- 时间范围 ----


@pytest.mark.parametrize(
    ("line", "ok"),
    [
        ("- [2026-11-06 23:30] 我：正好早一天。", True),
        ("- [2026-11-06 23:29] 我：早了一天多一分钟。", False),
        ("- [2026-11-09 23:30] 我：正好晚一天。", True),
        ("- [2026-11-09 23:31] 我：晚了一天多一分钟。", False),
        ("- [2026-11-01 10:42] 我：早很多。", False),
    ],
)
def test_time_range_allows_one_day_either_way(line: str, ok: bool, registry) -> None:
    packet = build_packet(body=f"## 原话\n{line}\n")
    if ok:
        assert validate(packet, registry)
    else:
        assert rejected_code(packet, registry) == TIME_OUT_OF_RANGE


def test_time_range_detail_has_the_line_and_time_but_no_text(registry) -> None:
    packet = build_packet(body="## 原话\n- [2026-11-01 10:42] 我：这句话不该出现在说明里。\n")
    with pytest.raises(Rejected) as caught:
        validate(packet, registry)
    assert caught.value.detail == "第 17 行的时间 2026-11-01 10:42 早于 covers_from 超过一天"


def test_quote_lines_are_not_checked_for_time_range(registry) -> None:
    packet = build_packet(body="## 原话\n- [10:30] 我：一句。\n  > [引用 2020-01-01 10:30 我] 很旧的话。\n")
    assert validate(packet, registry)


@pytest.mark.parametrize(
    ("date", "ok"),
    [
        ("2026-11-03T09:12:00+07:00", True),
        ("2026-11-01T18:00:00+08:00", True),  # 正好早一天
        ("2026-11-01T17:59:00+08:00", False),
        ("2026-10-01T09:12:00+07:00", False),
    ],
)
def test_mail_date_is_checked_for_time_range(date: str, ok: bool, registry) -> None:
    packet = build_mail_packet(sections=mail_section(mail_header_block(date=date), "  正文"))
    if ok:
        assert validate(packet, registry, directory="mail-sales-a")
    else:
        assert rejected_code(packet, registry, directory="mail-sales-a") == TIME_OUT_OF_RANGE


# ---- 环境 ----


@pytest.mark.parametrize(
    ("source", "env", "library", "ok"),
    [
        ("desk-bot", "prod", "prod", True),
        ("desk-bot", "test", "test", True),  # prod 的来源也可以试跑
        ("desk-bot", "test", "prod", False),
        ("desk-bot", "prod", "test", False),
        ("trial-bot", "test", "test", True),
        ("trial-bot", "prod", "prod", False),  # 登记为 test 的来源，包头写 prod
        ("trial-bot", "test", "prod", False),
        ("trial-bot", "prod", "test", False),
    ],
)
def test_env_rules(source: str, env: str, library: str, ok: bool, registry) -> None:
    packet = build_packet(source, env=env)
    if ok:
        assert validate(packet, registry, directory=source, library=library)
    else:
        assert rejected_code(packet, registry, directory=source, library=library) == ENV_MISMATCH


# ---- 登记表 ----


def test_spec_registry_loads(registry) -> None:
    assert set(registry.sources) == {"desk-bot", "books-bot", "trial-bot", "mail-sales-a"}
    assert registry.sources["books-bot"].may_carry == {"payroll"}
    assert registry.sources["mail-sales-a"].mailbox == "li.ming@example.com"
    assert "business" in registry.domains


def test_spec_registry_is_plain_yaml() -> None:
    assert isinstance(yaml.safe_load(SOURCES_YAML), dict)
