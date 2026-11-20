"""写包的公共工具：转义、编号、投递。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from javmem.packet.render import clean_text, deliver, front_matter, id_stamp, next_packet_id, yaml_list, yaml_text
from javmem.yamlio import load_yaml


@pytest.mark.parametrize(
    "text",
    [
        "普通的话",
        'He said "hi" \\ back',
        "有换行\n和回车\r和制表\t",
        "控制字符\x01\x1f\x7f\x85",
        "行分隔 段分隔 ",
        "BOM﻿在中间",
        "Re: 回复: [外部] #1 - {x} & *y* | z ' ` %",
        "",
    ],
)
def test_yaml_text_reads_back_the_same(text: str) -> None:
    assert load_yaml(f"value: {yaml_text(text)}")["value"] == text
    assert "\n" not in yaml_text(text)


def test_lone_surrogates_become_replacement_characters() -> None:
    text = "坏的\udce9字"
    assert load_yaml(f"value: {yaml_text(text)}")["value"] == "坏的�字"
    assert clean_text(text + "\x00") == "坏的�字�"


def test_clean_text_unifies_line_breaks() -> None:
    assert clean_text("a\r\nb\rc\n") == "a\nb\nc\n"


def test_yaml_list() -> None:
    assert load_yaml(f"v: {yaml_list(['business', 'a b', '标记'])}")["v"] == ["business", "a b", "标记"]
    assert yaml_list([]) == "[]"


def test_packet_id_takes_covers_to_and_adds_suffixes() -> None:
    moment = datetime(2026, 11, 8, 23, 30, 59, tzinfo=timezone(timedelta(hours=7)))
    assert id_stamp(moment) == "2026-11-08_2330"
    assert next_packet_id(moment, "desk-bot", set()) == "2026-11-08_2330_desk-bot"
    used = {"2026-11-08_2330_desk-bot", "2026-11-08_2330_desk-bot_2"}
    assert next_packet_id(moment, "desk-bot", used) == "2026-11-08_2330_desk-bot_3"
    assert id_stamp(datetime(999, 1, 2, 3, 4)) == "0999-01-02_0304"


def test_deliver_goes_through_tmp(tmp_path: Path) -> None:
    target = deliver(tmp_path / "inbox-test" / "desk-bot", "x.md", "内容\n")
    assert target == tmp_path / "inbox-test" / "desk-bot" / "x.md"
    assert target.read_bytes() == "内容\n".encode()
    assert list((tmp_path / "inbox-test" / "desk-bot" / ".tmp").iterdir()) == []


# ---------- 独立审阅找到的问题 ----------


@pytest.mark.parametrize("item", ["on", "yes", "No", "null", "~", "2024", "1e3", "0x1F", "1:20", "a, b", "#x", "- x", "true"])
def test_yaml_list_quotes_what_yaml_would_not_read_back_as_text(item: str) -> None:
    assert load_yaml(f"v: {yaml_list([item, 'business'])}")["v"] == [item, "business"]


def test_front_matter_reads_back_the_same() -> None:
    zone = timezone(timedelta(hours=8))
    text = front_matter(
        packet_id="2026-11-08_2330_desk-bot",
        created_at=datetime(2026, 11, 8, 23, 30, tzinfo=zone),
        source="desk-bot",
        framework="123",
        channel="chat",
        env="test",
        kind="daily",
        covers_from=datetime(2026, 11, 8, 9, 0, tzinfo=zone),
        covers_to=datetime(2026, 11, 8, 23, 30, tzinfo=zone),
        domain=["on", "2024"],
        sensitive=["null"],
    )
    fields = load_yaml(text.strip().strip("-"))
    assert (fields["framework"], fields["domain"], fields["sensitive"]) == ("123", ["on", "2024"], ["null"])
