"""L1 规整：每种情况一正一反。内容都是编的。"""

from __future__ import annotations

import pytest
from helpers import CHAT_PACKET, MAIL_PACKET, build_mail_packet, build_packet, file_name, mail_header_block

from javmem.packet import parse_packet
from javmem.pipeline.normalize import (
    ATTACHMENT_REF,
    FORWARD_HEADER,
    FORWARDED,
    HEADER,
    MACHINE_EVENT,
    NEW,
    PROTOCOL,
    QUOTE_HEADER,
    QUOTED,
    RESTATEMENT,
    SIGNATURE,
    SIGNATURE_INFO,
    WITHDRAWN,
    NormalizedPacket,
    Scope,
    classify_mail_body,
    normalize,
)
from javmem.raw.sentences import iter_sentences


def parsed(raw: str):
    return parse_packet(raw.encode("utf-8"), filename=file_name(raw))


def chat(body: str, **fields: object) -> NormalizedPacket:
    return normalize(parsed(build_packet(body=body, **fields)))


def kinds(packet: NormalizedPacket) -> dict[str, str]:
    """原文 → 标签。测试里每句原文都不一样。"""
    return {line.text: line.kind for unit in packet.units for line in unit.lines}


def body_kinds(text: str, subject: str = "Hello", *, info: bool = False) -> list[str | None]:
    """正文每一行的标签，空行是 None。签名里写公司、职位、部门的那几行（signature_info，D101）还是签名段的一部分：
    看签名段认得对不对的测试照旧算 signature，info=True 才分出来。"""
    kinds = [kind for kind, _ in classify_mail_body(text.split("\n"), subject)]
    return kinds if info else [SIGNATURE if kind == SIGNATURE_INFO else kind for kind in kinds]


# ---------- 原文不动，编号和原文索引一样 ----------


def test_every_sentence_kept_with_raw_id_and_text() -> None:
    """原话的每一句都在，顺序不变；附件的正文（D72）接在它属于的那句话、那封信后面，见 test_attachments.py。"""
    for raw in (CHAT_PACKET, MAIL_PACKET):
        packet = parsed(raw)
        sentences = list(iter_sentences(packet))
        units = normalize(packet).units
        expected = [
            (sentence.sentence_id, sentence.text, sentence.origin, sentence.quoted)
            for sentence in sentences
            if sentence.section_kind == "raw"
        ]
        got = [
            (line.sentence_id, line.text, line.origin, line.quoted)
            for unit in units
            for line in unit.lines
            if line.attachment is None
        ]
        assert got == expected
        attached = [line.sentence_id for unit in units for line in unit.lines if line.attachment is not None]
        bodies = [s.sentence_id for s in sentences if s.section_kind == "attachment" and s.section_no is not None]
        assert attached == bodies


def test_origin_is_passed_on() -> None:
    lines = {line.text: line for unit in normalize(parsed(CHAT_PACKET)).units for line in unit.lines}
    assert lines["[转发 自 小李] 客户说出货可以推到 11 月 15 日。"].origin == "relayed"
    assert lines["[外部:网页] 海运 11 月中旬运力偏紧。"].origin == "external"
    assert lines["越南那批订单还是小李跟。"].origin == "said"
    mail = normalize(parsed(MAIL_PACKET))
    assert {line.origin for unit in mail.units for line in unit.lines} == {"external"}


def test_scope_is_partitions_plus_tags() -> None:
    packet = chat("## 原话 · 10:30–10:31\n- [10:30] 我：工资的事。\n", domain="[finance, business]",
                  sensitive="[payroll]", privacy="private")
    assert packet.scope == Scope(("business", "finance"), ("payroll", "private"))
    assert packet.scope.private
    assert not chat("## 原话 · 10:30–10:31\n- [10:30] 我：好。\n").scope.private


# ---------- 对话 ----------


def test_chat_quote_line_is_old_talk() -> None:
    packet = chat(
        "## 原话 · 10:30–10:40\n"
        "- [10:30] 我：越南那批订单还是小李跟。\n"
        "- [10:40] desk-bot：记下了，下周三开会。\n"
        "  > [引用 10:30 我] 越南那批订单还是小李跟。\n"
        "  会上要带报价单。\n"
    )
    lines = [(line.text, line.kind) for unit in packet.units for line in unit.lines]
    assert ("> [引用 10:30 我] 越南那批订单还是小李跟。", QUOTED) in lines
    assert ("会上要带报价单。", NEW) in lines  # 续行不是引用


def test_attachment_only_lines() -> None:
    got = kinds(
        chat(
            "## 原话 · 10:30–10:35\n"
            "- [10:30] 我：[user attachment] Statement 2026.pdf\n"
            "- [10:31] 我：[图片:报价单.jpg]\n"
            "- [10:32] 我：[user attachment] 帮我看下里面的数字\n"
            "- [10:33] 我：[文件:合同.pdf] 第三页的金额不对\n"
            "- [10:34] 我：[user attachment]\n"
        )
    )
    assert got["[user attachment] Statement 2026.pdf"] == ATTACHMENT_REF
    assert got["[图片:报价单.jpg]"] == ATTACHMENT_REF
    assert got["[user attachment]"] == ATTACHMENT_REF
    assert got["[user attachment] 帮我看下里面的数字"] == NEW  # 后面是话，不是文件名
    assert got["[文件:合同.pdf] 第三页的金额不对"] == NEW


def test_bot_sync_protocol_messages_are_dropped() -> None:
    packet = chat(
        "## 原话 · 10:30–10:35\n"
        "- [10:30] 我：[DEMO_SYNC_STATUS_V1] 只读对账。\n"
        "  不得执行任何任务。\n"
        "- [10:31] desk-bot：[DEMO_SYNC_SCHEMA_V1] {\"ok\": true}\n"
        "- [10:32] desk-bot：[Max 的日记] 今天学了数字。\n"
        "- [10:33] 我：[TODO_LIST] 买菜。\n"
        "- [10:34] 我：[转发 自 小李] 客户说可以。\n"
    )
    got = kinds(packet)
    assert got["[DEMO_SYNC_STATUS_V1] 只读对账。"] == PROTOCOL
    assert got["不得执行任何任务。"] == PROTOCOL  # 整句话，续行也是
    assert got['[DEMO_SYNC_SCHEMA_V1] {"ok": true}'] == PROTOCOL
    assert got["[Max 的日记] 今天学了数字。"] == NEW
    assert got["[TODO_LIST] 买菜。"] == NEW  # 只有一个下划线
    assert got["[转发 自 小李] 客户说可以。"] == NEW
    assert [unit.dropped for unit in packet.units] == [True, True, False, False, False]


def test_machine_events_are_dropped() -> None:
    got = kinds(
        chat(
            "## 原话 · 10:30–10:34\n"
            '- [10:30] 系统：{"type": "name-changed", "from": "a", "to": "b"}\n'
            '- [10:31] desk-bot：{"type": "auto-review", "status": "expired"}\n'
            '- [10:32] desk-bot：{"price": 12}\n'
            '- [10:33] 我：我说的是 {"type": 1}\n'
        )
    )
    assert got['{"type": "name-changed", "from": "a", "to": "b"}'] == MACHINE_EVENT
    assert got['{"type": "auto-review", "status": "expired"}'] == MACHINE_EVENT
    assert got['{"price": 12}'] == NEW  # 没有 type
    assert got['我说的是 {"type": 1}'] == NEW


def test_bot_restatement() -> None:
    packet = chat(
        "## 原话 · 10:30–13:00\n"
        "- [10:30] 我：越南那批订单还是小李跟，下周三出货。\n"
        "- [10:31] desk-bot：收到，越南那批订单还是小李跟，下周三出货。\n"
        "- [10:32] desk-bot：好的。\n"
        "- [10:33] desk-bot：越南那批订单数量是 300 件，单价 12.5 美元。\n"
        "- [10:34] 我：越南那批订单数量是 300 件，单价 12.5 美元。\n"
        "- [13:00] desk-bot：越南那批订单还是小李跟，下周三出货。\n"
    )
    lines = [line for unit in packet.units for line in unit.lines]
    assert lines[1].kind == RESTATEMENT
    assert lines[1].restates == (lines[0].sentence_id,)
    assert lines[2].kind == NEW  # 太短，不判
    assert lines[3].kind == NEW  # 前面没人说过
    assert lines[4].kind == NEW  # 主人说的不算复述
    assert lines[5].kind == NEW  # 隔了两个小时以上


def test_restatement_needs_one_matching_utterance() -> None:
    """几句话拼起来像，不算复述。"""
    packet = chat(
        "## 原话 · 10:30–10:33\n"
        "- [10:30] 我：越南那批订单下周出货。\n"
        "- [10:31] 我：新加坡的单子小李去跟。\n"
        "- [10:33] desk-bot：越南那批订单小李去跟，新加坡的单子下周出货。\n"
    )
    assert [line.kind for unit in packet.units for line in unit.lines] == [NEW, NEW, NEW]


def test_other_speakers_and_other_bots_are_their_own() -> None:
    packet = chat("## 原话 · 10:30–10:31\n- [10:30] Desk Star：主人让我转告：明天开会。\n")
    unit = packet.units[0]
    assert (unit.speaker, unit.speaker_role, unit.lines[0].kind) == ("Desk Star", "other", NEW)


def test_withdrawn_sentence() -> None:
    packet = parsed(build_packet(body="## 原话 · 10:30–10:31\n- [10:30] 我：第一句。第二句。\n"))
    first = next(iter_sentences(packet)).sentence_id
    got = kinds(normalize(packet, withdrawn={first}))
    assert got == {"第一句。": WITHDRAWN, "第二句。": NEW}


def test_summary_and_attachment_notes_never_enter() -> None:
    packet = chat(
        "## 原话 · 10:30–10:31\n- [10:30] 我：越南那批推迟了。\n\n"
        "## 摘要（Bot 写的）\n- 推断：越南那批订单推迟到 2026-11-15。\n\n"
        "## 附件\n- 附件 报价单.jpg，image/jpeg，1024 字节\n"
    )
    texts = [line.text for unit in packet.units for line in unit.lines]
    assert texts == ["越南那批推迟了。"]
    assert packet.attachment_notes == 1


# ---------- 邮件 ----------


def test_mail_headers_are_their_own_kind() -> None:
    packet = normalize(parsed(MAIL_PACKET))
    first = packet.units[0]
    assert [line.field for line in first.lines if line.kind == HEADER] == ["subject", "from", "to", "cc"]
    assert first.mail is not None and first.mail.thread_id == "<ship-0001@example.org>"
    assert first.speaker == "Tran Mai <tran.mai@example.org>"


def test_mail_quote_lines() -> None:
    assert body_kinds("我们同意。\n> 能推到 15 号吗？\n>> 仓库满了。\n>\n>5% 的增长不算高。") == [
        NEW,
        QUOTED,
        QUOTED,
        QUOTED,
        NEW,
    ]


def test_mail_attribution_line() -> None:
    text = "\n".join(["好的。", "", "On Tue, Nov 3, 2026 at 14:30, Li Ming <li.ming@example.com> wrote:", "> OK, Nov 15 works."])
    assert body_kinds(text) == [NEW, None, QUOTE_HEADER, QUOTED]
    assert body_kinds("在 2026年11月3日 14:30，王芳 写道：\n> 中午吃饭吗？") == [QUOTE_HEADER, QUOTED]
    # 普通的话以“写道：”结尾，后面不是引用、也没有日期和地址，不算
    assert body_kinds("他在信里写道：\n下周开始涨价。") == [NEW, NEW]


def test_mail_attribution_wrapped_over_two_lines() -> None:
    text = "\n".join(["收到。", "On Tue, Nov 3, 2026 at 14:30, Li Ming <", "li.ming@example.com> wrote:", "> OK."])
    assert body_kinds(text) == [NEW, QUOTE_HEADER, QUOTE_HEADER, QUOTED]


def test_mail_signature_delimiter() -> None:
    assert body_kinds("能推到 15 号吗？\n\n-- \nTran Mai\n采购部\n+84 000 000 0000") == [
        NEW,
        None,
        SIGNATURE,
        SIGNATURE,
        SIGNATURE,
        SIGNATURE,
    ]
    assert body_kinds("能推到 15 号吗？\n--\nTran Mai")[1:] == [SIGNATURE, SIGNATURE]
    # 三个横线、横线夹在话里，都不是签名
    assert body_kinds("报价如下\n---\n单价 12.5 美元\n价格 -- 待定") == [NEW, NEW, NEW, NEW]


def test_mail_closing_and_name() -> None:
    assert body_kinds("OK, Nov 15 works.\n\nBest,\nMing\n\n> Could we move it?") == [
        NEW,
        None,
        SIGNATURE,
        SIGNATURE,
        None,
        QUOTED,
    ]
    assert body_kinds("货已经发了。\n谢谢！") == [NEW, SIGNATURE]
    # 问候后面还有正文：不是结尾
    assert body_kinds("谢谢\n\n我们下周见面再谈价格。") == [NEW, None, NEW]
    assert body_kinds("Thanks Ming, Nov 15 confirmed.") == [NEW]


def test_mail_mobile_signature() -> None:
    assert body_kinds("可以。\n发自我的 iPhone") == [NEW, SIGNATURE]
    assert body_kinds("Sent from my phone") == [SIGNATURE]
    assert body_kinds("发自内心地感谢大家。") == [NEW]


def test_mail_forward_block() -> None:
    text = (
        "小李，转给你看下。\n\n"
        "---------- 转发的邮件 ----------\n"
        "发件人：Logistics Desk <desk@example.net>\n"
        "日期：2026年11月3日 15:40\n"
        "主题：仓库排期\n\n"
        "11 月中旬运力偏紧，请提前订舱。\n\n"
        "-- \n"
        "Logistics Desk"
    )
    result = classify_mail_body(text.split("\n"), "Fwd: 仓库排期")
    assert [kind for kind, _ in result] == [
        NEW,
        None,
        FORWARD_HEADER,
        FORWARD_HEADER,
        FORWARD_HEADER,
        FORWARD_HEADER,
        None,
        FORWARDED,
        None,
        SIGNATURE,
        SIGNATURE,
    ]
    assert result[7][1] == "Logistics Desk <desk@example.net>"
    # 长得像分隔线、却不是转发的
    assert body_kinds("---------- 分隔 ----------\n下面是新内容。") == [NEW, NEW]


def test_mail_forward_header_ends_without_blank_line() -> None:
    text = "\n".join(["Begin forwarded message:", "From: Desk <desk@example.net>", "Subject: Slots", "Nov 15 is full."])
    assert body_kinds(text, "Fwd: Slots") == [FORWARD_HEADER, FORWARD_HEADER, FORWARD_HEADER, FORWARDED]


def test_mail_original_message_block() -> None:
    text = (
        "同意，按这个价。\n\n"
        "-----Original Message-----\n"
        "From: Tran Mai <tran.mai@example.org>\n"
        "Sent: Tuesday, November 3, 2026 9:12 AM\n"
        "Subject: 报价\n\n"
        "单价 12.5 美元可以吗？"
    )
    assert body_kinds(text, "RE: 报价") == [NEW, None, QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, None, QUOTED]
    # 主题是转发的：原始邮件块就是转发的内容
    forwarded = classify_mail_body(text.split("\n"), "FW: 报价")
    assert [kind for kind, _ in forwarded][-1] == FORWARDED
    assert forwarded[-1][1] == "Tran Mai <tran.mai@example.org>"


def test_mail_outlook_header_without_separator() -> None:
    text = "\n".join(["好的。", "", "From: 王芳 <wang.fang@example.com>", "Sent: 2026-11-03 09:00", "To: 李明", "", "下午三点开会。"])
    assert body_kinds(text, "回复：开会") == [NEW, None, QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, None, QUOTED]
    # 只有 From:，下面没有 Sent:，是普通的话
    assert body_kinds("From: the warehouse side, Nov 15 is fine.\nWe will book it.") == [NEW, NEW]


def test_mail_underscore_rule() -> None:
    text = "可以。\n________________________________\n发件人: 王芳\n发送时间: 2026年11月3日\n\n开会吗？"
    assert body_kinds(text, "回复：开会")[1:] == [QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, None, QUOTED]
    assert body_kinds("报价\n________________________________\n单价 12.5 美元") == [NEW, NEW, NEW]


def test_mail_sentences_follow_line_kinds() -> None:
    raw = build_mail_packet(
        sections="## 原话 · 邮件 · 2026-11-03 09:12\n\n"
        + mail_header_block(subject='"Fwd: 排期"')
        + "\n  看下。\n\n  ---------- 转发的邮件 ----------\n  发件人：Desk <desk@example.net>\n\n"
        "  运力偏紧。请提前订舱。\n"
    )
    lines = [line for unit in normalize(parsed(raw)).units for line in unit.lines]
    forwarded = [line for line in lines if line.kind == FORWARDED]
    assert [line.text for line in forwarded] == ["运力偏紧。", "请提前订舱。"]
    assert {line.relayed_from for line in forwarded} == {"Desk <desk@example.net>"}
    assert all(line.origin == "external" for line in lines)


# ---------- 审阅找到的：先红后修 ----------


def test_valediction_as_first_line_is_not_a_signature() -> None:
    assert body_kinds("\n".join(["Thanks!", "Nov 15 works for us.", "", "> Could we move it?"])) == [NEW, NEW, None, QUOTED]
    assert body_kinds("\n".join(["谢谢！", "报价收到了，我们接受 12.5 美元。", "下周一安排出货。"])) == [NEW, NEW, NEW]
    assert body_kinds("\n".join(["货已发。", "Thank you.", "The payment of 3000 USD was sent today."])) == [NEW, NEW, NEW]
    assert body_kinds("\n".join(["货已发。", "谢谢", "PS：发票抬头改一下。", "", "> 旧信"])) == [NEW, NEW, NEW, None, QUOTED]
    # 真正的结尾照样认
    assert body_kinds("\n".join(["货已发。", "", "Best regards,", "Tran Mai", "Purchasing, Example Home Co."])) == [
        NEW, None, SIGNATURE, SIGNATURE, SIGNATURE,
    ]


def test_bare_from_and_date_lines_are_not_a_quote_block() -> None:
    text = "\n".join(["行程确认如下：", "From: Shanghai PVG", "To: Ho Chi Minh SGN", "Date: 2026-11-15 08:30", "", "总费用 3200 元。"])
    assert body_kinds(text, "行程") == [NEW, NEW, NEW, NEW, None, NEW]
    text = "\n".join(["发件人：行政部", "时间：11月15日 14:00", "地点：三楼会议室"])
    assert body_kinds(text, "开会") == [NEW, NEW, NEW]


def test_restatement_with_a_new_number_is_not_a_restatement() -> None:
    packet = chat(
        "## 原话 · 10:30–10:31\n"
        "- [10:30] 我：单价 12.5 美元，一共 300 件。\n"
        "- [10:31] desk-bot：好的，单价 12.5 美元，一共 300 件，总价 3750 美元。\n"
    )
    assert [line.kind for unit in packet.units for line in unit.lines] == [NEW, NEW]


def test_restatement_compares_single_sentences() -> None:
    packet = chat(
        "## 原话 · 10:30–10:31\n"
        "- [10:30] 我：越南那批订单下周出货。新加坡的单子小李去跟。\n"
        "- [10:31] desk-bot：越南那批订单小李去跟，新加坡的单子下周出货。\n"
    )
    assert [line.kind for unit in packet.units for line in unit.lines] == [NEW, NEW, NEW]


def test_echo_of_a_withdrawn_sentence_is_dropped_too() -> None:
    packet = parsed(
        build_packet(
            body="## 原话 · 10:30–10:32\n"
            "- [10:30] 我：越南那批订单还是小李跟，下周三出货。\n"
            "- [10:31] desk-bot：收到，越南那批订单还是小李跟，下周三出货。\n"
            "- [10:32] desk-bot：记下了。\n"
            "  > [引用 10:30 我] 越南那批订单还是小李跟，下周三出货。\n"
        )
    )
    first = next(iter_sentences(packet)).sentence_id
    got = [line.kind for unit in normalize(packet, withdrawn={first}).units for line in unit.lines]
    assert got == [WITHDRAWN, WITHDRAWN, NEW, WITHDRAWN]


def test_content_line_above_an_attribution_stays_new() -> None:
    text = "\n".join(["在 11 月 15 日之前必须订舱，费用 3000 美元。", "Li Ming <li.ming@example.com> 于2026年11月3日 14:30写道：", "> 好的"])
    assert body_kinds(text) == [NEW, QUOTE_HEADER, QUOTED]
    text = "\n".join(["On price: we accept 12.5 USD.", "Li Ming <li.ming@example.com> wrote:", "> ok"])
    assert body_kinds(text) == [NEW, QUOTE_HEADER, QUOTED]


def test_mobile_signature_needs_the_whole_form() -> None:
    assert body_kinds("来自我的客户的反馈：价格太高，要降到 11 美元。") == [NEW]
    assert body_kinds("Sent from the Hanoi warehouse today: 300 units.") == [NEW]
    assert body_kinds("\n".join(["可以。", "Sent from my Galaxy"])) == [NEW, SIGNATURE]
    assert body_kinds("\n".join(["可以。", "Get Outlook for iOS"])) == [NEW, SIGNATURE]


def test_attachment_name_must_look_like_a_file() -> None:
    got = kinds(
        chat(
            "## 原话 · 10:30–10:34\n"
            "- [10:30] 我：[user attachment] 合同改好了，金额是 3.5\n"
            "- [10:31] 我：[user attachment] 发票金额 1,280.00\n"
            "- [10:32] 我：[user attachment] 新版本报价 v2.0\n"
            "- [10:33] 我：[user attachment] IMG_0001.png\n"
        )
    )
    assert got["[user attachment] 合同改好了，金额是 3.5"] == NEW
    assert got["[user attachment] 发票金额 1,280.00"] == NEW
    assert got["[user attachment] 新版本报价 v2.0"] == NEW
    assert got["[user attachment] IMG_0001.png"] == ATTACHMENT_REF


def test_traditional_chinese_and_vietnamese_reply_headers() -> None:
    text = "\n".join(["好的。", "", "寄件者: 王芳 <wang.fang@example.com>", "寄件日期: 2026年11月3日", "主旨: 開會", "", "下午三點開會？"])
    assert body_kinds(text, "回覆：開會")[2:] == [QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, None, QUOTED]
    text = "\n".join(["OK.", "", "Từ: Tran Mai <tran.mai@example.org>", "Đã gửi: 03/11/2026", "Chủ đề: Lô hàng", "", "Giao ngày 15?"])
    assert body_kinds(text, "RE: Lô hàng")[2:] == [QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, None, QUOTED]
    assert body_kinds("\n".join(["王芳 於 2026年11月3日 寫道：", "> 開會嗎？"])) == [QUOTE_HEADER, QUOTED]


def test_protocol_tag_needs_a_version_suffix() -> None:
    got = kinds(
        chat(
            "## 原话 · 10:30–10:32\n"
            "- [10:30] 我：[MEETING_NOTES_1109] 今天和老周定了出货。\n"
            "- [10:31] 我：[DEMO_SYNC_STATUS_V2] 对账。\n"
        )
    )
    assert got["[MEETING_NOTES_1109] 今天和老周定了出货。"] == NEW
    assert got["[DEMO_SYNC_STATUS_V2] 对账。"] == PROTOCOL


def test_leading_markers_do_not_hide_machine_events_or_attachments() -> None:
    got = kinds(
        chat(
            "## 原话 · 10:30–10:32\n"
            '- [10:30] desk-bot：[外部:工具] {"type": "auto-review", "status": "expired"}\n'
            "- [10:31] 我：[转发 自 小李] [user attachment] quote.pdf\n"
        )
    )
    assert got['[外部:工具] {"type": "auto-review", "status": "expired"}'] == MACHINE_EVENT
    assert got["[转发 自 小李] [user attachment] quote.pdf"] == ATTACHMENT_REF


# ---------- 复看找到的 ----------


def test_relook_greeting_is_not_content_before_a_closing() -> None:
    assert body_kinds("\n".join(["Hi Mai,", "", "Thanks!", "Nov 15 works for us.", "", "> Could we?"])) == [
        NEW, None, NEW, NEW, None, QUOTED,
    ]
    assert body_kinds("\n".join(["王总：", "谢谢！", "合同明天寄出", "", "> 旧信"])) == [NEW, NEW, NEW, None, QUOTED]


def test_relook_closing_block_rejects_sentences_and_numbers() -> None:
    assert body_kinds("\n".join(["收到。", "谢谢", "明天下午三点在 3 楼开会"])) == [NEW, NEW, NEW]
    assert body_kinds("\n".join(["收到。", "谢谢", "Nov 15 works for us."])) == [NEW, NEW, NEW]
    assert body_kinds("\n".join(["收到。", "", "Regards,", "Tran Mai", "+84 000 000 0000", "Example Home Co."])) == [
        NEW, None, SIGNATURE, SIGNATURE, SIGNATURE, SIGNATURE,
    ]


def test_relook_forwarded_part_starts_fresh() -> None:
    text = "\n".join(
        ["看下。", "", "---------- Forwarded message ---------", "From: Desk <desk@example.net>", "", "谢谢！", "下周一安排出货"]
    )
    assert body_kinds(text, "Fwd: 出货") == [NEW, None, FORWARD_HEADER, FORWARD_HEADER, None, FORWARDED, FORWARDED]


def test_relook_outlook_header_with_names_only() -> None:
    text = "\n".join(
        ["好的。", "", "From: Wang Fang", "Sent: Tuesday, November 3, 2026 9:00 AM", "To: Li Ming", "Subject: RE: 报价", "", "单价 12.5 美元可以吗？"]
    )
    assert body_kinds(text, "RE: 报价")[2:] == [QUOTE_HEADER] * 4 + [None, QUOTED]
    text = "\n".join(["好的。", "", "发件人: 王芳", "发送时间: 2026年11月3日 9:00", "收件人: 李明", "主题: 回复: 报价", "", "可以吗？"])
    assert body_kinds(text, "回复: 报价")[2:] == [QUOTE_HEADER] * 4 + [None, QUOTED]


def test_relook_tool_json_content_is_kept() -> None:
    got = kinds(chat('## 原话 · 10:30–10:31\n- [10:30] desk-bot：[外部:工具] {"type": "text", "text": "海运费是 3200 美元"}\n'))
    assert got['[外部:工具] {"type": "text", "text": "海运费是 3200 美元"}'] == NEW


def test_relook_single_underscore_version_tag_is_not_protocol() -> None:
    assert kinds(chat("## 原话 · 10:30–10:31\n- [10:30] 我：[PLAN_V2] 越南那批改成 11 月 20 日出货。\n")) == {
        "[PLAN_V2] 越南那批改成 11 月 20 日出货。": NEW
    }


def test_relook_withdrawn_quotes_in_mail_and_truncated_quotes() -> None:
    raw = build_mail_packet(
        sections="## 原话 · 邮件 · 2026-11-03 09:12\n\n"
        + mail_header_block()
        + "\n  老周的工资是 2 万。\n\n## 原话 · 邮件 · 2026-11-03 10:00\n\n"
        + mail_header_block(message_id='"<b@example.org>"', in_reply_to='"<a@example.org>"', date="2026-11-03T10:00:00+07:00")
        + "\n  收到。\n\n  > 老周的工资是 2 万。\n"
    )
    packet = parsed(raw)
    salary = next(s.sentence_id for s in iter_sentences(packet) if s.text == "老周的工资是 2 万。")
    got = {line.text: line.kind for unit in normalize(packet, withdrawn={salary}).units for line in unit.lines}
    assert got["> 老周的工资是 2 万。"] == WITHDRAWN
    chat_packet = parsed(
        build_packet(
            body="## 原话 · 10:30–10:31\n- [10:30] 我：越南那批订单还是小李跟，下周三出货。\n"
            "- [10:31] desk-bot：记下了。\n  > [引用 10:30 我] 越南那批订单还是小李跟…\n"
        )
    )
    first = next(iter_sentences(chat_packet)).sentence_id
    got = {line.text: line.kind for unit in normalize(chat_packet, withdrawn={first}).units for line in unit.lines}
    assert got["> [引用 10:30 我] 越南那批订单还是小李跟…"] == WITHDRAWN


def test_relook_more_mobile_signatures() -> None:
    assert body_kinds("\n".join(["可以。", "Sent from Yahoo Mail on Android"])) == [NEW, SIGNATURE]
    assert body_kinds("\n".join(["可以。", "Sent from Outlook"])) == [NEW, SIGNATURE]


def test_spans_point_into_the_source_text() -> None:
    for raw in (CHAT_PACKET, MAIL_PACKET):
        for unit in normalize(parsed(raw)).units:
            for line in unit.lines:
                assert line.span is not None
                assert unit.source_text[line.span[0] : line.span[1]] == line.text


# ---------- 网易邮箱的回复抬头、转发抬头（l1-v7） ----------
# 分隔线下面的旧信头不带冒号：字段名后面空一格写值，或者字段名单独一行、值在下一行（中间还隔着空行），
# 或者几个字段挤在一行里；中文界面的字段名常和值连着写。被回的旧信不能当新写的话


def test_netease_reply_header_one_field_a_line() -> None:
    text = "\n".join(
        [
            "Sure, the 15th works.",
            "",
            "---- Replied Message ----",
            "From Lin Hao<lin.hao@example.net>",
            "Date 11/03/2026 09:12",
            "To Tran Mai<tran.mai@example.org>,",
            "Ana Ruiz<ana.ruiz@example.org>",
            "Cc Desk<desk@example.net>",
            "Subject Re: Pallet count",
            "Hi Mai,",
            "Can we push the pickup to the 15th?",
        ]
    )
    assert body_kinds(text, "Re: Pallet count") == [NEW, None] + [QUOTE_HEADER] * 7 + [QUOTED, QUOTED]


def test_netease_reply_header_names_on_their_own_lines() -> None:
    text = "\n".join(
        ["OK.", "", "---- Replied Message ----", "", "From", "", "Lin Hao<lin.hao@example.net>", "", "Date", "",
         "11/03/2026 09:12", "", "Subject", "", "Pallet count", "", "Can we push it?"]
    )
    got = body_kinds(text, "Re: Pallet count")
    assert [kind for kind in got if kind is not None] == [NEW] + [QUOTE_HEADER] * 7 + [QUOTED]


def test_netease_reply_header_fields_on_one_line() -> None:
    text = "\n".join(
        [
            "Noted.",
            "---- Replied Message ----",
            "From Lin Hao<lin.hao@example.net> Date 11/03/2026 09:12 To Tran Mai<tran.mai@example.org>,",
            "Ana Ruiz<ana.ruiz@example.org> Subject Re: Pallet count",
            "Dear Mai,",
            "Pickup moved to the 15th.",
        ]
    )
    assert body_kinds(text, "Re: Pallet count") == [NEW, QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, QUOTED, QUOTED]


def test_netease_reply_header_chinese_names_glued_to_values() -> None:
    text = "\n".join(
        [
            "好，就照这个顺序装柜。",
            "",
            "---- 回复的原邮件 ----",
            "发件人 林浩<lin.hao@example.net>",
            "发送日期2027年3月4日 09:12",
            "收件人 陈楠<chen.nan@example.com>",
            "抄送人前台<desk@example.net>",
            "主题回复：托盘数",
            "陈楠，",
            "托盘改十八个行不行？",
        ]
    )
    assert body_kinds(text, "回复：托盘数") == [NEW, None] + [QUOTE_HEADER] * 6 + [QUOTED, QUOTED]


def test_netease_reply_is_old_mail_even_under_a_forward_subject() -> None:
    """Replied Message 只用在回复里；Original Message 在转发的主题下才算转发的内容。"""
    text = "\n".join(["See below.", "", "---- Replied Message ----", "From Lin Hao<lin.hao@example.net>", "Subject Slots", "Nov 15 is full."])
    assert body_kinds(text, "Fwd: Slots")[-1] == QUOTED


def test_netease_reply_sentences_are_not_evidence() -> None:
    raw = build_mail_packet(
        sections="## 原话 · 邮件 · 2026-11-03 09:12\n\n"
        + mail_header_block(subject='"Re: Pallet count"')
        + "\n  Sure.\n\n  ---- Replied Message ----\n  From Lin Hao<lin.hao@example.net>\n  Subject Pallet count\n"
        "  The pickup moves to the 15th. Twenty pallets.\n"
    )
    lines = {line.text: line.kind for unit in normalize(parsed(raw)).units for line in unit.lines}
    assert lines["Sure."] == NEW
    assert lines["The pickup moves to the 15th."] == QUOTED
    assert lines["Twenty pallets."] == QUOTED


def test_netease_forward_header_without_colons_is_the_forwarded_mails_header() -> None:
    text = "\n".join(
        [
            "Please see below.",
            "",
            "---- Forwarded Message ----",
            "From Lin Hao<lin.hao@example.net>",
            "Date 11/03/2026 09:12",
            "To Chen Nan<chen.nan@example.com>",
            "Subject Pallet count",
            "Pickup moved to the 15th.",
        ]
    )
    result = classify_mail_body(text.split("\n"), "Fwd: Pallet count")
    assert [kind for kind, _ in result] == [NEW, None] + [FORWARD_HEADER] * 5 + [FORWARDED]
    assert result[-1][1] == "Lin Hao<lin.hao@example.net>"


def test_netease_forward_header_with_blank_lines_and_chinese_names() -> None:
    text = "\n".join(
        ["---- Forwarded Message ----", "", "", "From", "", "Lin Hao<lin.hao@example.net>", "", "", "Date", "",
         "11/03/2026 09:12", "", "", "Subject", "", "Pallet count", "", "", "Pickup moved to the 15th."]
    )
    result = classify_mail_body(text.split("\n"), "Fwd: Pallet count")
    assert [kind for kind, _ in result if kind is not None] == [FORWARD_HEADER] * 7 + [FORWARDED]
    assert result[-1][1] == "Lin Hao<lin.hao@example.net>"
    text = "\n".join(["---- 转发的原邮件 ----", "发件人 林浩<lin.hao@example.net>", "发送日期2027年3月4日", "主题托盘数", "", "托盘改十八个。"])
    result = classify_mail_body(text.split("\n"), "转发：托盘数")
    assert [kind for kind, _ in result] == [FORWARD_HEADER] * 4 + [None, FORWARDED]
    assert result[-1][1] == "林浩<lin.hao@example.net>"


def test_gmail_forward_header_with_colons_is_unchanged() -> None:
    text = "\n".join(["---------- Forwarded message ---------", "From: Desk <desk@example.net>", "To: Lin Hao <lin.hao@example.net>", "Subject: Slots", "", "From the dock side: Nov 15 is full."])
    assert body_kinds(text, "Fwd: Slots") == [FORWARD_HEADER] * 4 + [None, FORWARDED]


def test_forward_block_inside_an_old_mail_stays_old() -> None:
    """原始邮件块以下都是旧信：旧信里套着的转发块不是这封信转发的内容。"""
    text = "\n".join(
        [
            "OK.",
            "",
            "-----Original Message-----",
            "From: Tran Mai <tran.mai@example.org>",
            "Sent: Tuesday, November 3, 2026 9:12 AM",
            "Subject: FW: slots",
            "",
            "---------- Forwarded message ---------",
            "From: Desk <desk@example.net>",
            "Subject: Slots",
            "",
            "Nov 15 is full.",
        ]
    )
    got = body_kinds(text, "RE: slots")
    assert got[7:10] == [QUOTE_HEADER] * 3
    assert got[-1] == QUOTED


def test_netease_lookalikes_are_ordinary_lines() -> None:
    assert body_kinds("We replied message by message.\nFrom now on, Date and To fields are required.") == [NEW, NEW]
    assert body_kinds("---- Replied ----\nFrom Lin Hao the price is fine.") == [NEW, NEW]


def test_closing_right_above_a_netease_header_is_a_signature() -> None:
    text = "\n".join(["Nov 15 works.", "", "Thanks,", "Ming", "", "---- Replied Message ----", "From Lin Hao<lin.hao@example.net>"])
    assert body_kinds(text, "Re: Slots") == [NEW, None, SIGNATURE, SIGNATURE, None, QUOTE_HEADER, QUOTE_HEADER]


def test_netease_separator_glued_to_the_header() -> None:
    """有的信分隔线后面没换行，直接接着旧信头。"""
    text = "\n".join(["OK.", "---- Replied Message ----Lin Hao<lin.hao@example.net> Date11/03/2026 09:12", "Subject Slots", "Nov 15 is full."])
    assert body_kinds(text, "Re: Slots") == [NEW, QUOTE_HEADER, QUOTE_HEADER, QUOTED]


# ---------- 签名、免责声明、图片占位（l1-v7） ----------
# 结尾的问候到引用抬头（或者信的末尾）之间的几行算签名：中间可以有空行，最多 8 行署名，
# 电话、地址（邮编、门牌号）、网址可以带数字；里面有一句话的不算。免责声明、[cid:…] 占位照签名处理


def test_closing_runs_to_the_quote_header_across_blank_lines() -> None:
    text = "\n".join(
        [
            "The samples ship on Monday.",
            "",
            "Best regards,",
            "",
            "Lin Hao",
            "Sales Manager",
            "Example Home Co., Ltd.",
            "",
            "Room 1203, Building 3, No. 88 Example Road",
            "Pudong District, Shanghai 200120, China",
            "Tel: +86 21 0000 0000",
            "www.example.com",
            "",
            "From: Tran Mai <tran.mai@example.org>",
            "Sent: Tuesday, November 3, 2026 9:12 AM",
            "Subject: Samples",
        ]
    )
    assert body_kinds(text, "RE: Samples") == (
        [NEW, None, SIGNATURE, None] + [SIGNATURE] * 3 + [None] + [SIGNATURE] * 4 + [None] + [QUOTE_HEADER] * 3
    )


def test_closing_allows_postcodes_and_house_numbers() -> None:
    text = "\n".join(["Pallets are ready.", "", "Thanks & Best Regards,", "Lin Hao", "No. 18 Example Street", "", "200120 Shanghai", "P.C.: 200120"])
    assert body_kinds(text) == [NEW, None, SIGNATURE, SIGNATURE, SIGNATURE, None, SIGNATURE, SIGNATURE]
    text = "\n".join(["箱子都打好了。", "", "谢谢", "林浩", "上海市浦东新区样例路 88 号 3 栋 1203 室", "邮编：200120"])
    assert body_kinds(text) == [NEW, None, SIGNATURE, SIGNATURE, SIGNATURE, SIGNATURE]


def test_closing_followed_by_more_content_is_not_a_signature() -> None:
    text = "\n".join(["Pallets are ready.", "", "Thanks!", "", "Please confirm the pallet count by Friday", "", "From: Tran Mai <tran.mai@example.org>", "Sent: Tuesday, November 3, 2026 9:12 AM"])
    assert body_kinds(text, "RE: Pallets")[:5] == [NEW, None, NEW, None, NEW]
    assert body_kinds("\n".join(["收到。", "", "谢谢", "", "下周 3 号前付款"])) == [NEW, None, NEW, None, NEW]
    assert body_kinds("\n".join(["收到。", "谢谢", "", "包装纸箱外面要贴两张唛头标签"])) == [NEW, NEW, None, NEW]
    names = ["Lin Hao", "Sales", "Example Home", "Example Group", "Asia Desk", "North Team", "Second Floor", "Main Gate", "Back Office"]
    assert body_kinds("\n".join(["Pallets are ready.", "Regards,", *names]))[1] == NEW  # 署名超过 8 行


def test_valediction_variants() -> None:
    for closing in ("Thanks & Best Regards,", "Thank you in advance.", "Thanks and best regards", "Best  regards,", "Thanks a lot!"):
        assert body_kinds("\n".join(["Pallets are ready.", closing, "Lin Hao"])) == [NEW, SIGNATURE, SIGNATURE], closing


def test_closing_stops_at_an_attribution_wrapped_over_two_lines() -> None:
    text = "\n".join(["Fine.", "Thanks,", "Ming", "", "On Tue, Nov 3, 2026 at 14:30, Li Ming <", "li.ming@example.com> wrote:", "> OK."])
    assert body_kinds(text) == [NEW, SIGNATURE, SIGNATURE, None, QUOTE_HEADER, QUOTE_HEADER, QUOTED]


def test_disclaimers_are_treated_as_signatures() -> None:
    text = "\n".join(
        [
            "Invoice attached.",
            "",
            "Regards,",
            "Lin Hao",
            "",
            "CONFIDENTIALITY NOTICE: The contents of this note are meant for the",
            "named readers only and may be privileged.",
            "",
            "If you are not the addressee of this note, please erase it and let us know.",
            "",
            "From: Tran Mai <tran.mai@example.org>",
            "Sent: Tuesday, November 3, 2026 9:12 AM",
        ]
    )
    assert body_kinds(text, "RE: Invoice")[:9] == [NEW, None, SIGNATURE, SIGNATURE, None, SIGNATURE, SIGNATURE, None, SIGNATURE]
    # 没有结尾问候的也算
    text = "\n".join(["Invoice attached.", "", "This e-mail and its files are confidential and intended for the named reader only."])
    assert body_kinds(text) == [NEW, None, SIGNATURE]
    assert body_kinds("\n".join(["发票在附件里。", "", "保密声明：这封信只写给收件人本人看，旁人别读。"])) == [NEW, None, SIGNATURE]
    assert body_kinds("\n".join(["账单已出。", "为了账号安全，请勿转发此邮件。"])) == [NEW, SIGNATURE]


def test_disclaimer_lookalikes_stay_content() -> None:
    assert body_kinds("这份报价请勿转发给客户。\n请勿回复本邮件给仓库那边") == [NEW, NEW]
    assert body_kinds("Important notice: the dock closes early on Friday.\nThe confidential price list is attached.") == [NEW, NEW]
    assert body_kinds("If you are free, call me after lunch.") == [NEW]


def test_image_placeholders_are_signatures() -> None:
    text = "\n".join(
        [
            "Pallets are ready.",
            "[cid:image001.png@01DA0000.00000000]",
            "[cid:logo.png]<https://www.example.com/>",
            "[cid:icon-phone.png] +86 21 0000 0000",
            "[image: banner.png]",
            "[cid:photo.jpg] The carton was crushed on arrival",
        ]
    )
    assert body_kinds(text) == [NEW, SIGNATURE, SIGNATURE, SIGNATURE, SIGNATURE, NEW]


# ---------- D91 审阅找到的：每条先红再修 ----------


@pytest.mark.parametrize(
    "lines",
    [
        ["Pallets are ready.", "", "Thanks,", "Ming", "", "Please tell the driver we need the pallets before noon"],
        ["Pallets are ready.", "", "Thanks,", "", "Please add 20 more pallets to the Friday load"],
        ["Got them.", "Thanks!", "", "Order 556677 ships on Nov 15", "Container No. 8 is full"],
        ["收到。", "谢谢", "", "订单 123456 我们已经发货了"],
        ["收到。", "谢谢！", "", "另外，样品下周一寄出"],
        ["收到。", "谢谢", "", "市场部周五开会定新价格方案"],
        ["收到。", "Thanks,", "", "Friday works"],
        ["收到。", "谢谢", "", "下周三发货"],
    ],
)
def test_review_content_after_a_closing_is_not_swallowed(lines: list[str]) -> None:
    """问候下面隔着空行的正文不能吞进签名，要能当证据。"""
    assert body_kinds("\n".join(lines))[-1] == NEW


def test_review_titles_with_small_words_are_still_signatures() -> None:
    """l1-v6 认得出的署名：职位里有小写的词（manager、sales）。"""
    text = "\n".join(["The samples ship on Monday.", "", "Best regards,", "Lin Hao", "Sales manager, export department", "Example Home Co., Ltd."])
    assert body_kinds(text) == [NEW, None] + [SIGNATURE] * 4
    assert body_kinds("\n".join(["The samples ship on Monday.", "Thanks,", "Lin Hao", "Head of sales and marketing"]))[1:] == [SIGNATURE] * 3


@pytest.mark.parametrize(
    "line",
    [
        "[cid:photo1.jpg] Carton No. 8 was crushed on arrival",
        "[image: label.png] Please call +86 21 0000 0000 before delivery",
        "[cid:p.jpg] The truck waits at the gate on Example Road",
        "[cid:s.jpg] Invoice 202611 is still unpaid",
        "[cid:x.png] 订单 123456 已发货",
        "[cid:y.png] Please tell the driver to wait",
    ],
)
def test_review_an_image_placeholder_followed_by_words_is_content(line: str) -> None:
    assert body_kinds("\n".join(["Pallets are ready.", line]))[-1] == NEW


@pytest.mark.parametrize(
    ("lines", "content"),
    [
        (
            ["Hi Mai,", "", "If you are not the right recipient for this order, please tell me who is.",
             "We need 500 units delivered by Nov 15 at USD 12.50 each.", "", "Thanks,", "Ming"],
            [2, 3],
        ),
        (["王总：", "", "本邮件所附报价仅供贵司参考，单价 12.5 美元，有效期到月底。", "订金 30%，余款发货前付清。", "", "谢谢"], [2, 3]),
        (["Hi Mai,", "This email is confidential: the new unit price is USD 12.50 from Dec 1.", "Please keep it internal."], [1, 2]),
        (["Warehouse notice:", "Any unauthorized use of the forklift will be fined USD 100.", "Gate 3 closes at 18:00 on Friday."], [0, 1, 2]),
        (["Hi Mai,", "Disclaimer: I have not checked with the factory yet.", "But the ETA should be Nov 15 and the price stays at USD 12.50."], [1, 2]),
        (["王总：", "价格表见邮件附件，仅限内部使用，请勿转发。"], [1]),
    ],
)
def test_review_disclaimer_lookalikes_are_content(lines: list[str], content: list[int]) -> None:
    got = body_kinds("\n".join(lines))
    assert [got[index] for index in content] == [NEW] * len(content)


def test_review_closing_after_a_content_line_that_starts_like_a_disclaimer() -> None:
    lines = ["Hi Mai,", "", "If you are not the right recipient for this order, please tell me who is.", "", "Thanks,", "Ming"]
    assert body_kinds("\n".join(lines))[-2:] == [SIGNATURE, SIGNATURE]


def test_review_bare_forward_header_stops_at_the_forwarded_text() -> None:
    text = "\n".join(
        [
            "FYI.",
            "",
            "---- Forwarded Message ----",
            "From Lin Hao<lin.hao@example.net>",
            "Date 11/03/2026 09:12",
            "To Chen Nan<chen.nan@example.com>",
            "From now on we ship on Mondays",
            "To be safe, book the truck two days ahead",
            "Contact desk@example.net for the new slots",
            "Price stays at USD 12.50.",
        ]
    )
    assert body_kinds(text, "Fwd: Slots") == [NEW, None] + [FORWARD_HEADER] * 4 + [FORWARDED] * 4
    text = "\n".join(["---------- Forwarded message ---------", "To whom it may concern: the dock closes at 18:00 on Friday.", "Date of loading is Nov 15."])
    assert body_kinds(text, "Fwd: Slots") == [FORWARD_HEADER, FORWARDED, FORWARDED]
    text = "\n".join(["---- Forwarded Message ----", "From Lin Hao<lin.hao@example.net>", "Date 11/03/2026 09:12", "Subject", "", "Please load 20 pallets on Friday."])
    assert body_kinds(text, "Fwd: Slots")[-1] == FORWARDED


def test_review_forward_separator_glued_to_the_header() -> None:
    text = "\n".join(
        ["FYI.", "", "---- Forwarded Message ----From Lin Hao<lin.hao@example.net> Date 11/03/2026 09:12", "Subject Slots",
         "Nov 15 is full, we can only load on Nov 16."]
    )
    result = classify_mail_body(text.split("\n"), "Fwd: Slots")
    assert [kind for kind, _ in result] == [NEW, None, FORWARD_HEADER, FORWARD_HEADER, FORWARDED]
    assert result[-1][1] == "Lin Hao<lin.hao@example.net>"


def test_review_relayed_from_keeps_names_with_field_words() -> None:
    for first in ("From Back To Basics Ltd<info@example.net>", "From Back To Basics Ltd<info@example.net> Date 11/03/2026 09:12"):
        text = "\n".join(["---- Forwarded Message ----", first, "Subject Samples", "Samples ship Monday."])
        assert classify_mail_body(text.split("\n"), "Fwd: Samples")[-1] == (FORWARDED, "Back To Basics Ltd<info@example.net>")


@pytest.mark.parametrize(
    ("lines", "subject"),
    [
        (["Pallets are ready.", "Thanks,", "zip" + " " * 3000 + "x"], "Hello"),
        (["Pallets are ready.", "Thanks,", "PC" + " " * 3000 + "x"], "Hello"),
        (["Pallets are ready.", "Thanks,", "No" + " " * 60000 + "x"], "Hello"),
        (["Pallets are ready."] + ["[cid:a.png] zip" + " " * 185 + "x"] * 3000, "Hello"),
        (["OK.", "---- Replied Message ----", "From x" + " " * 30000 + "y"], "Re: Hello"),
        (["OK.", "---- Forwarded Message ----", "From x" + " " * 30000 + "y"], "Fwd: Hello"),
        (["OK.", "---- Replied Message ----", "a" * 150000], "Re: Hello"),
        (["Pallets are ready.", "[cid:" * 30000], "Hello"),
    ],
)
def test_review_long_lines_take_linear_time(lines: list[str], subject: str) -> None:
    import time

    started = time.perf_counter()
    classify_mail_body(lines, subject)
    assert time.perf_counter() - started < 5
