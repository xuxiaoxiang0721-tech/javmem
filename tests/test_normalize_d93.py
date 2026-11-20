"""L1 第二轮（D93）：报告第 4 节第 3、4、5、6、8、12 条。句子都是编的。"""

from __future__ import annotations

import time

import pytest

from javmem.pipeline.normalize import (
    FORWARD_HEADER,
    FORWARDED,
    NEW,
    QUOTE_HEADER,
    QUOTED,
    SIGNATURE,
    SIGNATURE_INFO,
    classify_mail_body,
)

S = SIGNATURE
# 计时测试的线：这几条防的是平方级变慢（没修以前要好几秒到半分钟以上）。原来是 2 秒，CI 的 Windows 机器忙时跑过
# 2.06 秒（11-18），放到 4 秒，照样抓得住变慢
SLOW_LINE = 4


def body_kinds(text: str, subject: str = "Hello", *, info: bool = False) -> list[str | None]:
    """正文每一行的标签，空行是 None。签名里写公司、职位、部门的那几行（signature_info，D101）还是签名段的一部分：
    看签名段认得对不对的测试照旧算 signature，info=True 才分出来。"""
    kinds = [kind for kind, _ in classify_mail_body(text.split("\n"), subject)]
    return kinds if info else [SIGNATURE if kind == SIGNATURE_INFO else kind for kind in kinds]


# ---------- 第 3 条：问候下面隔着空行的一句话，带着邮箱、网址、电话、地址，不是签名 ----------


@pytest.mark.parametrize(
    "line",
    [
        "Please send the invoice to billing@example.org",
        "The new price list is on www.example.org/prices",
        "Please call me at +86 139 0000 1111 tomorrow",
        "Room 2201 is booked for the Monday meeting",
        "The truck is waiting at 66 Sample Road",
        "PO 778899 ships to Germany on Nov 20",
        "明天打我手机 13900001111",
        "请把发票寄到新地址",
        "货已经送到 5 栋",
    ],
)
def test_a_sentence_with_contact_details_below_the_closing_is_body(line: str) -> None:
    assert body_kinds(f"Pallets are ready.\n\nThanks,\nMing\n\n{line}") == [NEW, None, NEW, NEW, None, NEW]


def test_a_real_signature_with_contact_lines_is_still_a_signature() -> None:
    text = (
        "Pallets are ready.\n\nBest regards,\nLin Hao\nSales Manager\n\nExample Home Co., Ltd.\n"
        "Room 1203, Building 3, No. 88 Example Road\nTel: +86 21 0000 0000\nwww.example.com\nlin.hao@example.net"
    )
    assert body_kinds(text) == [NEW, None, S, S, S, None, S, S, S, S, S]


# ---------- 第 4 条：问候下面短的决定不是名字 ----------


@pytest.mark.parametrize(
    "text",
    [
        "样品看过了。\n\n谢谢\n\n同意付款",
        "样品看过了。\n谢谢\n取消订单",
        "样品看过了。\n\n谢谢\n\n同意",
        "Samples look good.\n\nThanks,\n\nConfirmed for Friday",
        "Samples look good.\n\nThanks,\n\nApproved",
    ],
)
def test_a_short_decision_below_the_closing_is_not_a_name(text: str) -> None:
    assert all(kind in (NEW, None) for kind in body_kinds(text))


@pytest.mark.parametrize("name", ["Lin Hao", "林浩", "王芳"])
def test_names_below_the_closing_are_still_names(name: str) -> None:
    assert body_kinds(f"Samples look good.\n\nThanks,\n\n{name}") == [NEW, None, S, None, S]


# ---------- 第 5 条：系统通知那一行只标它自己 ----------


def test_a_system_notice_marks_only_its_own_line() -> None:
    text = "这封邮件是自动发出的。\n您的订单 778899 已经装车，周五前送到二号仓。"
    assert body_kinds(text) == [S, NEW]


# ---------- 第 6 条：几种以前认得出的签名 ----------


@pytest.mark.parametrize(
    "lines",
    [
        ["Web: example.org"],
        ["Skype: lin.hao"],
        ["WeChat: linhao"],
        ["LinkedIn: linkedin.com/in/linhao"],
        ["Office: 021-6000-0000"],
        ["Hotline: 400-000-0000"],
        ["Cellphone: 138-0000-0000"],
        ["Head of supply chain"],
        ["Founder and managing director"],
    ],
)
def test_contact_and_title_lines_below_the_name_are_signature(lines: list[str]) -> None:
    text = "\n".join(["The samples ship on Monday.", "", "Best regards,", "Lin Hao", *lines])
    assert body_kinds(text) == [NEW, None, S, S, *[S] * len(lines)]


@pytest.mark.parametrize("name", ["Ahmad bin Ismail", "Maria dos Santos", "Paolo di Marco"])
def test_names_with_lowercase_particles_are_names(name: str) -> None:
    assert body_kinds(f"The samples ship on Monday.\n\nBest regards,\n{name}\nSales Manager") == [NEW, None, S, S, S]


@pytest.mark.parametrize("lines", [["王会芳"], ["林浩", "会计"], ["林浩", "高级销售代表"], ["林浩", "上海市浦东新区"], ["林浩", "微信：tom_88"]])
def test_chinese_names_titles_and_places_below_the_closing_are_signature(lines: list[str]) -> None:
    text = "\n".join(["样品周一寄出。", "", "谢谢", *lines])
    assert body_kinds(text) == [NEW, None, S, *[S] * len(lines)]


# ---------- 第 8 条：平方级的正则、两种会吞正文的免责声明、方括号的一行、[cid:] 加域名 ----------


@pytest.mark.parametrize(
    ("lines", "subject"),
    [
        (["Pallets are ready.", "Thanks,", "1" * 20000 + "a" * 20000], "Hello"),  # _STREET 的“几号楼”
        (["Pallets are ready.", "Thanks,", "a" * 40000 + "中"], "Hello"),  # _CJK_TAIL
        (["Hello.", "Confidentiality notice" + " " * 40000 + "x"], "Hello"),  # _DISCLAIMER 的 \s*[:：.]?\s*
        (["Hello.", "Disclaimer" + " " * 40000 + "x"], "Hello"),
        (["OK.", "---- Replied Message ----", "From <" + "a@" * 20000], "Re: x"),  # _FIRST_ADDRESS
    ],
)
def test_long_lines_are_linear(lines: list[str], subject: str) -> None:
    started = time.perf_counter()
    classify_mail_body(lines, subject)
    assert time.perf_counter() - started < SLOW_LINE


@pytest.mark.parametrize(
    "lines",
    [
        ["If you are not the intended recipient of this box, please send it back to Dock 2.", "The crates must be back by Friday."],
        ["This message is confidential and only for the intended group: prices change in March.", "Please keep it internal."],
    ],
)
def test_sentences_that_only_sound_like_a_disclaimer_are_body(lines: list[str]) -> None:
    assert body_kinds("\n".join(["Hi Mai,", *lines])) == [NEW, NEW, NEW]


@pytest.mark.parametrize(
    "line",
    [
        "If you are not the intended recipient of this email, please notify the sender and delete it.",
        "This email is confidential and intended solely for the use of the addressee.",
    ],
)
def test_real_disclaimers_are_still_signature(line: str) -> None:
    assert body_kinds(f"Hi Mai,\nPallets are ready.\n\n{line}") == [NEW, NEW, None, S]


def test_a_bracketed_sentence_below_the_closing_is_body() -> None:
    assert body_kinds("Pallets are ready.\n\nThanks,\n[Ship 20 pallets on Friday]") == [NEW, None, NEW, NEW]
    assert body_kinds("Pallets are ready.\n\nThanks,\nLin Hao\n[Logo]") == [NEW, None, S, S, S]


def test_an_image_placeholder_followed_by_a_domain_is_signature() -> None:
    assert body_kinds("Pallets are ready.\n\nBest regards,\nLin Hao\n[cid:logo.png] example.com") == [NEW, None, S, S, S]
    assert body_kinds("Pallets are ready.\n[cid:logo.png] example.com") == [NEW, S]


# ---------- 第 12 条：别的客户端的几种回复抬头 ----------


def test_the_reply_info_separator_starts_the_old_mail() -> None:
    text = "收到，谢谢。\n\n----------回复的邮件信息----------\n林浩 在 2026-11-03 09:12 写道：\n托盘改十八个。\n周五装车。"
    assert body_kinds(text, "回复：托盘") == [NEW, None, QUOTE_HEADER, QUOTE_HEADER, QUOTED, QUOTED]


def test_a_spanish_attribution_folded_onto_two_lines() -> None:
    text = "Gracias.\n\nEl mar., 3 nov. 2026 a las 09:12, Lin Hao (<lin.hao@example.net>)\nescribió:\n> Pallets ready."
    assert body_kinds(text, "Re: Pallets") == [NEW, None, QUOTE_HEADER, QUOTE_HEADER, QUOTED]


def test_a_colon_header_with_the_send_date_field() -> None:
    text = (
        "收到。\n\n发件人：林浩 <lin.hao@example.net>\n发送日期：11/03/2026 09:12\n收件人：陈楠 <chen.nan@example.com>\n"
        "主题：托盘\n托盘改十八个。"
    )
    assert body_kinds(text, "回复：托盘") == [NEW, None, QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, QUOTE_HEADER, QUOTED]


# ---------- 真实邮件上查出来的（D93，l1-v8 第一版少认的几种签名，句子都是编的） ----------


@pytest.mark.parametrize(
    "line",
    [
        "Add:Room 501, 5th Floor, Block 3, Sample Road, Pudong District, Shanghai",
        "2nd Floor, Sample Tower, 18 Example Road",
        "http://www.sample-co.example",
        "Website: www.sample.example <http://www.sample.example/>",
        "+84 28 0000 0000 | www.SAMPLE.example<http://www.sample.example/>",
        "📍 Địa chỉ: 12 Mẫu, phường Một, quận Hai, thành phố Mẫu",
        "上海市样例区示例路88号3栋，邮编：200000",
    ],
)
def test_signature_lines_with_ordinals_links_and_addresses_are_signature(line: str) -> None:
    text = "\n".join(["The samples ship on Monday.", "", "Best regards,", "Lin Hao", line])
    assert body_kinds(text) == [NEW, None, S, S, S]


@pytest.mark.parametrize("name", ["tom", "mike"])
def test_lowercase_names_below_the_closing_are_still_names(name: str) -> None:
    assert body_kinds(f"The samples ship on Monday.\n\nThanks,\n{name}") == [NEW, None, S, S]


@pytest.mark.parametrize(
    "text",
    [
        "Hi Ming,\n\nPlease find the updated PI below.\n\nThanks,\nPI No.: 20261109\nAmount: 52,300.00\nDeposit: 15,690.00",
        "Hi Ming,\n\nPlease confirm.\n\nBest regards,\nLena Ortiz\n\nPO No.: 4500012346\nQty: 130000 pcs\nETD: 2026-12-06",
        "李经理：\n\n订单已收到。\n\n谢谢！\n\n交期会延长",
        "李经理：\n\n样品收到了。\n\n谢谢\n\n大货价格要再问一下李总",
        "李经理：\n\n这批已经装柜。\n\n谢谢\n\n下一批发往深圳市",
        "李经理：\n\n这批已经装柜。\n\n谢谢\n\n时间延长",
    ],
)
def test_figures_and_short_sentences_below_the_closing_are_body(text: str) -> None:
    """D93 审阅第 6 条：写着 PI No.:、Amount: 这类字样的数字不是电话；会、要、将在长一点的一行里照样算一句话；
    “……发往深圳市”“时间延长”不是地名、职位。"""
    assert all(kind in (NEW, None) for kind in body_kinds(text))


def test_a_notice_line_that_carries_figures_is_body() -> None:
    """D93 审阅第 13 条：“请勿回复此邮件”前面写着金额、单号的，这一行是正文。"""
    assert body_kinds("您的付款 USD 52,300.00 已经到账，请勿回复此邮件。") == [NEW]
    assert body_kinds("账单已出。\n为了账号安全，请勿转发此邮件。") == [NEW, S]


@pytest.mark.parametrize(
    ("lines", "subject"),
    [
        (["Ciao Mario,", "Il prezzo finale è 13 EUR", "Mario <mario@example.it> ha scritto:", "> Qual è il prezzo?"], "Re: prezzo"),
        (["Bonjour Jean,", "Le prix final est 13 EUR", "Jean <jean@example.fr> a écrit :", "> Quel est le prix ?"], "Re: prix"),
        (["Hallo Max,", "Am Montag liefern wir 41 Kisten", "Max <max@example.de> schrieb:", "> Wann liefert ihr?"], "Re: Lieferung"),
        (["Hi Tom,", "On Monday we ship 41 crates", "Tom <tom@example.com> wrote:", "> When do you ship?"], "Re: crates"),
    ],
)
def test_a_body_line_just_above_a_one_line_attribution_stays_body(lines: list[str], subject: str) -> None:
    """D93 审阅第 7 条：上一行要写着日期（年份、几点几分）才和下面的引用抬头算一行。"""
    assert body_kinds("\n".join(lines), subject) == [NEW, NEW, QUOTE_HEADER, QUOTED]


@pytest.mark.parametrize(
    "line",
    [
        "Tel: 0574-8888 0000 ext 801 / mobile 138 0000 0000",
        "M: +84 90 000 0000 | E: mai@example.vn | W: example.vn",
        "Room 1203, Building 3, Example Science park, Pudong new area, Shanghai",
        "E-mail: lena.ortiz@example.net | Web: www.example.net",
        "Example Trading Co., Ltd. | www.example.net",
    ],
)
def test_contact_lines_with_extensions_separators_and_area_words_are_signature(line: str) -> None:
    """D93 审阅第 8 条：ext、mobile、park、new area 这类签名里的小写词不算一句话；用 | 隔开的几样各自是签名的，整行是签名。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", line])
    assert body_kinds(text) == [NEW, None, S, S, S]


# ---------- D96（第三轮）：报告第 4 节第 31、35 到 38、42 条 ----------


@pytest.mark.parametrize(
    "line",
    [
        "Work Tel:21-555-0100",
        "Fx: 21-555-0101",
        "HP NO: 0812-0000-0000",
        "Telp: (021) 5550 0100",
        "Main Line: +1 555 0100 200",
        "Contact No.: 555-0100-200",
        "手机号：139 0000 0000",
        "售后热线：400 0000 000",
    ],
)
def test_phone_labels_outside_the_old_list_are_still_phones(line: str) -> None:
    """D96 第 31 条：电话的字样不再要每个词都在一张表里（第 1 箱签名认出来的从 70% 掉到 28%）；
    写着业务字段（PI、PO、Amount、Qty……）的才不算电话。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", "Example Trading Co., Ltd.", line])
    assert body_kinds(text) == [NEW, None, S, S, S, S]


@pytest.mark.parametrize(
    "line", ["Invoice No.: 20261109", "Order Total: 52,300.00", "Balance Due: 36,610.00", "订单号：4500012346", "合同金额：52,300.00"]
)
def test_business_fields_below_the_closing_are_still_body(line: str) -> None:
    """D96 第 31 条：反过来写以后，写着业务字段的数字照样不当电话（D93 审阅第 6 条）。"""
    text = "\n".join(["Hi Ming,", "", "Please confirm.", "", "Thanks,", line])
    assert all(kind in (NEW, None) for kind in body_kinds(text))


@pytest.mark.parametrize("line", ["这批发往深圳市", "改发宁波市", "送到浦东新区", "要退货", "将另行通知"])
def test_short_shipping_lines_and_speech_words_below_the_closing_are_body(line: str) -> None:
    """D96 第 35 条：只写地名的一行不能带着发、送、寄、往、到（“这批发往深圳市”能过，以前只是因为前面正好 7 个字）；
    会、要、将开头的短行是一句话，不是名字。"""
    assert all(kind in (NEW, None) for kind in body_kinds("李经理：\n\n样品已经寄出。\n\n谢谢\n\n" + line))


@pytest.mark.parametrize("line", ["深圳市南山区", "宁波经济技术开发区", "Tel 021-5555-0100 | Mobile 138 0000 0000"])
def test_places_and_separated_phones_are_still_signature(line: str) -> None:
    """护栏：只写地名的（开发区里的“发”不算）、用 | 隔开的两个电话照样是签名。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", "", line])
    assert body_kinds(text) == [NEW, None, S, S, None, S]


def test_figures_separated_by_bars_are_not_contact_details() -> None:
    """D96 第 35 条（c94bcff 带出来的）：用 | 拆开以后，每一段光是数字多的不算联系方式，要有字样、电话的字、邮箱、网址。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", "", "PO 4500012345 | qty 120000 | ETD 2026-12-05"])
    assert body_kinds(text)[-1] == NEW


@pytest.mark.parametrize(
    ("lines", "subject"),
    [
        (["Ciao Mario,", "Il prezzo finale è 2000 EUR", "Mario <mario@example.it> ha scritto:", "> Qual è il prezzo?"], "Re: prezzo"),
        (["Hallo Max,", "Am Montag liefern wir 12000 Kisten", "Max <max@example.de> schrieb:", "> Wann liefert ihr?"], "Re: Lieferung"),
        (["Hallo Max,", "Am Montag um 10:30 liefern wir", "Max <max@example.de> schrieb:", "> Wann liefert ihr?"], "Re: Lieferung"),
        (["Hi Tom,", "On Monday at 9:30 we ship", "Tom <tom@example.com> wrote:", "> When do you ship?"], "Re: crates"),
    ],
)
def test_a_line_with_only_a_number_or_only_a_time_is_not_merged_into_the_attribution(lines: list[str], subject: str) -> None:
    """D96 第 36 条：12000 里夹着的 2000 也算年份，只写着时刻的也并了。现在上一行要写着年份（前后不挨着数字）和时刻。"""
    assert body_kinds("\n".join(lines), subject) == [NEW, NEW, QUOTE_HEADER, QUOTED]


def test_a_folded_attribution_with_a_year_and_a_time_is_still_one_header() -> None:
    """护栏：真的折成两行的抬头，年份、时刻都在上一行，照样并。"""
    text = "Thanks.\n\nOn Tue, 3 Nov 2026 at 09:12, Lin Hao <lin.hao@example.net>\nwrote:\n> Pallets ready."
    assert body_kinds(text, "Re: Pallets") == [NEW, None, QUOTE_HEADER, QUOTE_HEADER, QUOTED]


@pytest.mark.parametrize("line", ["示例会展服务有限公司", "地址：示例市会展路 18 号", "陈会林 销售经理", "周将 总经理"])
def test_signature_lines_with_hui_yao_jiang_are_still_signature(line: str) -> None:
    """D96 第 37 条：公司名（有限公司结尾）、写着地址字样的、名字加职位的一行先认，再看会、要、将；“会展”不算说话。"""
    text = "\n".join(["样品已经寄出。", "", "谢谢", "", line])
    assert body_kinds(text) == [NEW, None, S, None, S]


def test_names_with_hui_in_a_netease_forward_header_stay_in_the_header() -> None:
    """D96 第 37 条：网易不带冒号的转发信头，收件人接着写的那一行是两个带“会”字的名字，以前那一行起成了转发的正文。"""
    lines = [
        "---- 转发的原邮件 ----",
        "发件人 林浩<lin.hao@example.net>",
        "发送日期2027年3月4日",
        "收件人 陈楠<chen.nan@example.com>,",
        "陈会林<chen.huilin@example.com>, 王会芳<wang.huifang@example.com>",
        "主题托盘数",
        "",
        "托盘改十八个。",
    ]
    result = classify_mail_body(lines, "转发：托盘数")
    assert [kind for kind, _ in result] == [FORWARD_HEADER] * 6 + [None, FORWARDED]


@pytest.mark.parametrize("line", ["采购课长", "示例商会秘书长", "门店店长", "生产部线长", "示例国家高新技术产业开发区"])
def test_more_titles_ending_in_zhang_and_long_place_names_are_signature(line: str) -> None:
    """D96 第 38 条：“长”前面补上课、店、线、秘书；地名每一段最多 10 个字（以前 6 个，长的开发区名认不出）。"""
    text = "\n".join(["样品已经寄出。", "", "谢谢", "", "林浩", line])
    assert body_kinds(text) == [NEW, None, S, None, S, S]


def test_a_long_run_of_place_suffixes_stays_fast() -> None:
    """地名那一条放宽到 10 个字以后，一大串“市市市……”照样很快。"""
    started = time.perf_counter()
    body_kinds("谢谢\n\n林浩\n" + "市" * 50000)
    assert time.perf_counter() - started < SLOW_LINE


def test_a_long_line_ending_in_zong_is_not_a_title() -> None:
    """D96 第 42 条：结尾的“总”整行要短才算职位（“大货价格再问一下李总”没有“要”字，以前三个版本都标成签名）。"""
    assert all(kind in (NEW, None) for kind in body_kinds("李经理：\n\n样品收到了。\n\n谢谢\n\n大货价格再问一下李总"))
    for title in ("李总", "销售副总"):
        assert body_kinds("样品已经寄出。\n\n谢谢\n\n林浩\n" + title) == [NEW, None, S, None, S, S], title


def test_an_auto_sent_notice_that_carries_figures_is_body() -> None:
    """D96 第 42 条：通知的第二种写法（写明这封邮件是自动发的）后面写着金额的那一行是正文（以前整行标成签名）；
    后面没有数字的照样是通知。"""
    assert body_kinds("这封邮件为自动生成，货款 USD 52,300.00 已经入账") == [NEW]
    assert body_kinds("账单已出。\n这封邮件为自动生成，无需回信。") == [NEW, S]


@pytest.mark.parametrize(
    "line", ["電話：0571-0000-0000", "手機：0912 000 000", "聯絡人：林浩", "郵箱：lin.hao@example.net", "地址：示例市中山路 18 號 3 樓"]
)
def test_traditional_labels_are_signature(line: str) -> None:
    """D96 第 42 条：签名里的字样（電話、手機、聯絡人、郵箱……）补繁体，以前三个版本都认不出“電話：”。"""
    text = "\n".join(["样品已经寄出。", "", "谢谢", "", "林浩", line])
    assert body_kinds(text) == [NEW, None, S, None, S, S]


def test_traditional_business_fields_are_not_phones() -> None:
    """繁体的业务字段（訂單、金額）后面的数字也不是电话。"""
    for line in ("訂單號：4500012346", "合同金額：52,300.00"):
        assert all(kind in (NEW, None) for kind in body_kinds("Hi Ming,\n\nPlease confirm.\n\nThanks,\n" + line)), line


# ---------- D96 第一遍审阅后 ----------


@pytest.mark.parametrize(
    "line",
    [
        "P.I. No.: 20261109",
        "S/C No.: 26110901",
        "B/L No.: 73581246",
        "Container No.: 4501234",
        "Booking No.: 73584136",
        "Delivery: 2026-12-05",
        "Due: 2026-12-05",
        "Pcs: 120000",
        "N.W.: 1250.50",
        "Sub-total: 52,300.00",
        "PO-No: 4500012345",
        "价格：52,300.00",
        "合计：52,300.00",
        "总额：52,300.00",
        "尾款：36,610.00",
        "运费：1,250.00",
        "交期：2031-03-07",
        "柜号：7358124",
        "批号：20261109",
        "Note: 2026-12-05",
        "Remark: 52,300.00",
    ],
)
def test_more_business_fields_and_figure_values_are_not_phones(line: str) -> None:
    """D96 第一遍审阅第 1 条：P.I.、S/C 这种带点、斜杠的，Sub-total 这种带连字符的，价格、运费这些中文的字段，
    以前都当成电话，问候下面那一行连问候都成了签名；值是金额、日期的也不是电话。"""
    text = "\n".join(["Hi Ming,", "", "Please confirm.", "", "Thanks,", line])
    assert all(kind in (NEW, None) for kind in body_kinds(text))


@pytest.mark.parametrize("line", ["Tel: 0571.8888.0000", "Tel: 021-5555-0100, 0101", "Mobile/WhatsApp: +84 90 000 0000"])
def test_phone_numbers_with_dots_and_commas_are_still_phones(line: str) -> None:
    """护栏：用点、逗号隔开的电话号码不是金额。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", "Example Trading Co., Ltd.", line])
    assert body_kinds(text) == [NEW, None, S, S, S, S]


@pytest.mark.parametrize(
    "line",
    ["交货地点改为宁波市", "目的港改为宁波市", "这批货改去宁波市", "下一票改从宁波市", "收货人在苏州工业园区", "运至深圳市", "货在南山区", "仓库换成嘉兴市"],
)
def test_place_lines_with_more_verbs_below_the_closing_are_body(line: str) -> None:
    """D96 第一遍审阅第 2 条：地名放宽到 10 个字以后，带着改、换、至、在、去的一句话也当成了地名。"""
    assert all(kind in (NEW, None) for kind in body_kinds("李经理：\n\n样品已经寄出。\n\n谢谢\n\n" + line))


@pytest.mark.parametrize("line", ["示例市从化区", "运城市", "示例市回民区", "示例出口加工区"])
def test_place_names_with_cong_yun_hui_chu_are_still_places(line: str) -> None:
    """护栏：从、运、回、出不算动词，有从化区、运城市、回民区、出口加工区这些地名。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", "", line])
    assert body_kinds(text) == [NEW, None, S, S, None, S]


@pytest.mark.parametrize(
    "line",
    [
        "Sales Manager | 138 0000 0000",
        "Purchasing Manager | 0571 8888 0000",
        "Example Trading Co., Ltd. | 021-5555-0100",
        "Export Dept. | 021 5555 0100",
        "深圳市南山区 | 0755-8888-0000",
    ],
)
def test_a_bare_number_after_a_bar_is_still_a_phone(line: str) -> None:
    """D96 第一遍审阅第 3 条：用 | 隔开的，光是号码、没有字母的一段不用写字样；有字母又有 6 个以上数字的
    （PO 4500012345、qty 120000）才要。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", line])
    assert body_kinds(text) == [NEW, None, S, S, S]


@pytest.mark.parametrize(
    "line",
    [
        "样品会展示给采购",
        "新款会展示在深圳市",
        "这批会展出在示例集团",
        "发票会开给示例贸易有限公司",
        "此事要上报集团",
        "货款将汇给示例有限公司",
        "这批货要退回示例有限公司",
        "这批 会转给采购",
        "样品 要寄给业务",
        "电话：要换新号",
        "地址：将搬到新厂",
        "联系人：要换成王经理",
    ],
)
def test_sentences_with_hui_yao_jiang_are_not_exempted(line: str) -> None:
    """D96 第一遍审阅第 4 条：会展示、会展出不是“会展”；公司名、名字加职位、写着字样的那一段的豁免太宽，
    “发票会开给……有限公司”“这批 会转给采购”“电话：要换新号”这类一句话都标成了签名。"""
    assert all(kind in (NEW, None) for kind in body_kinds("李经理：\n\n样品已经寄出。\n\n谢谢\n\n" + line))


def test_an_address_with_jiang_inside_a_street_name_is_still_signature() -> None:
    """护栏：写着地址字样的，会、要、将只看是不是开头就是（“地址：将搬到新厂”是一句话），路名里的不算。"""
    text = "\n".join(["样品已经寄出。", "", "谢谢", "", "地址：示例市将台路 8 号"])
    assert body_kinds(text) == [NEW, None, S, None, S]


@pytest.mark.parametrize(
    "body",
    [
        "谢谢\n\n林浩\n" + " | ".join(["市" * 55] * 1000),
        "\n\n".join(["谢谢\n\n林浩\n" + "\n".join(["市" * 55] * 8)] * 300),
    ],
)
def test_many_short_runs_of_place_suffixes_stay_fast(body: str) -> None:
    """D96 第一遍审阅第 5 条：地名放宽到 10 个字以后，一行几十个“市”每一段都要比，一千段要七八秒。"""
    started = time.perf_counter()
    body_kinds("样品已经寄出。\n\n" + body)
    assert time.perf_counter() - started < SLOW_LINE


@pytest.mark.parametrize(
    ("line", "place"),
    [
        ("深圳市南山区", True),
        ("示例国家高新技术产业开发区", True),
        ("示例省示例市示例区示例镇示例乡", True),
        ("示例市示例区示例县示例镇示例乡示例街道", True),  # 6 个结尾，两段并成一段（一段里也可以有“市”）就是 5 段
        ("一二三四五六市" * 6, False),  # 6 段，相邻两段并起来超过 10 个字
        ("示例一二三四五六七八九十一市", False),  # 一段 11 个字
        ("市" * 55, True),
        ("市" * 56, False),
        ("深圳", False),
        ("深圳 市", False),
    ],
)
def test_the_place_check_keeps_its_shape(line: str, place: bool) -> None:
    """改成小循环以后认的东西不变：1 到 5 段，每段 1 到 10 个汉字加省、市、区、开发区这类结尾。"""
    from javmem.pipeline import normalize

    assert normalize._zh_place(line) is place


def test_the_place_loop_agrees_with_the_old_pattern() -> None:
    """拿改以前那条正则当对照，随机拼几千行比一遍：认的完全一样。"""
    import random
    import re

    from javmem.pipeline import normalize

    old = re.compile(r"(?:[一-鿿]{1,10}?(?:省|市|区|县|镇|乡|新区|开发区|工业区|工业园|园区|街道)){1,5}")
    pieces = ["示", "例", "市", "区", "县", "开发区", "工业园", "园区", "新区", "街道", "省"]
    rng = random.Random(96)
    for _ in range(5000):
        line = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 24)))
        if len(line) <= normalize._ZH_PLACE_WIDTH:
            assert normalize._zh_place(line) == bool(old.fullmatch(line)), line


@pytest.mark.parametrize("line", ["这事问店长", "大货价格再问一下店长", "库存的事找线长", "大货价格再问一下经理", "这件事告诉采购经理", "样品给业务"])
def test_a_line_asking_someone_with_a_title_is_body(line: str) -> None:
    """D96 第一遍审阅第 6 条：以 12 个字以内、结尾是职位的就算职位，“这事问店长”“再问一下经理”也标成了签名。
    职位前面带着问、找、给、向、让、叫、告诉、通知的是一句话。"""
    assert all(kind in (NEW, None) for kind in body_kinds("李经理：\n\n样品已经寄出。\n\n谢谢\n\n" + line))


@pytest.mark.parametrize("line", ["销售顾问", "高级顾问"])
def test_a_consultant_title_is_still_a_title(line: str) -> None:
    """护栏：顾问的“问”不算。"""
    text = "\n".join(["样品已经寄出。", "", "谢谢", "", "林浩", line])
    assert body_kinds(text) == [NEW, None, S, None, S, S]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("On 11/3/26 9:12 AM, Lin Hao <lin.hao@example.net>", "wrote:"),
        ("On Tue, Nov 3, 2026, Lin Hao <lin.hao@example.net>", "wrote:"),
        ("Le mar. 3 nov. 2026, Jean Moreau <jean.moreau@example.fr>", "a écrit :"),
    ],
)
def test_a_folded_attribution_with_a_short_date_or_an_address_is_one_header(first: str, second: str) -> None:
    """D96 第一遍审阅第 7 条：第 36 条要年份和时刻都有以后，Thunderbird 的两位年份（11/3/26）、只写日期不写时刻的
    折行抬头不并了，上一行成了正文。短日期也算年份；上一行写着尖括号里的邮箱的，有年份就并。"""
    text = "\n".join(["Thanks.", "", first, second, "> Pallets ready."])
    assert body_kinds(text, "Re: Pallets") == [NEW, None, QUOTE_HEADER, QUOTE_HEADER, QUOTED]


# ---------- D96 第二遍审阅后 ----------


@pytest.mark.parametrize("closings", [1, 3])
def test_a_long_run_of_comma_groups_below_the_closing_is_fast(closings: int) -> None:
    """D96 第二遍审阅第 1 条：问候下面一行 5 万多字的“1,111,…,1111”，比一次是不是签名要好几秒（千分位金额那条正则
    是平方级），问候连写几句，每一句都要再比一遍。"""
    figures = ",".join(["1"] + ["111"] * 16000) + "1"
    started = time.perf_counter()
    body_kinds("样品已经寄出。\n\n" + "谢谢\n" * closings + figures)
    assert time.perf_counter() - started < SLOW_LINE



def test_many_closings_above_one_long_line_are_fast() -> None:
    """D96 第二遍审阅（老问题）：问候连写一两百句时，下面那一行很长的，每一句都要再比一遍是不是签名，以前要半分钟以上。
    比签名的行还宽的，先比宽度就停下。"""
    started = time.perf_counter()
    body_kinds("样品已经寄出。\n\n" + "谢谢\n" * 199 + "问" * 50000)
    assert time.perf_counter() - started < SLOW_LINE


# ---------- D99（第四轮）：第 46、47 条 ----------


@pytest.mark.parametrize(
    "lines",
    [
        ["Hi Tom,", "On 11/3/26 the truck comes at 9:30 to Dock 2", "Tom wrote:", "> When?"],
        ["Hi Tom,", "On 2026 orders please copy Lena <lena@example.com>", "Tom wrote:", "> Who?"],
        ["Hi Tom,", "On 3 Nov 2026 at 10:30 we ship the crates", "Tom <tom@example.com> wrote:", "> When?"],
    ],
)
def test_a_body_line_starting_with_on_is_not_merged_into_the_attribution(lines: list[str]) -> None:
    """D99 第 46 条：On 开头、写着日期的正文，紧挨着下一行的“Tom wrote:”，以前并进了引用抬头（丢证据）。
    上一行要以尖括号邮箱、逗号、名字结尾（或者折开的“<”）；只靠邮箱认日期的，中间不能有长一点的小写词。"""
    assert body_kinds("\n".join(lines), "Re: crates") == [NEW, NEW, QUOTE_HEADER, QUOTED]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("On Tue, Nov 3, 2026 at 9:12 AM,", "Lin Hao <lin.hao@example.net> wrote:"),
        ("On Tue, Nov 3, 2026 at 9:12 AM Lin Hao", "<lin.hao@example.net> wrote:"),
        ("On Tue, Nov 3, 2026 at 9:12 AM Lin Hao <", "lin.hao@example.net> wrote:"),
        ("Il giorno mar 3 nov 2026, Mario Rossi <mario.rossi@example.it>", "ha scritto:"),
    ],
)
def test_real_folded_attributions_are_still_one_header(first: str, second: str) -> None:
    """护栏：真的折成两行的抬头（结尾是逗号、名字、折开的“<”、尖括号邮箱）照样并。"""
    text = "\n".join(["Thanks.", "", first, second, "> Pallets ready."])
    assert body_kinds(text, "Re: Pallets") == [NEW, None, QUOTE_HEADER, QUOTE_HEADER, QUOTED]


@pytest.mark.parametrize(
    "line",
    ["PO#: 4500012345", "PO #: 4500012345", "Invoice #: 20261109", "Order#: 73581246", "Ref#: 26110901", "Note: 05/12/2031", "ETA 05/12/2031"],
)
def test_hash_labels_and_day_first_dates_are_not_phones(line: str) -> None:
    """D99 第 47 条：字样里不认 #，PO#:、Invoice #: 后面的数字当成了电话；日在前的日期（05/12/2031）也当成了电话，
    问候连这一行一起标成签名（丢证据）。"""
    text = "\n".join(["Hi Ming,", "", "Please confirm.", "", "Thanks,", line])
    assert all(kind in (NEW, None) for kind in body_kinds(text))


def test_a_phone_label_with_a_hash_is_still_a_phone() -> None:
    """护栏：Tel #: 后面的照样是电话。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", "Example Trading Co., Ltd.", "Tel #: 021-5555-0100"])
    assert body_kinds(text) == [NEW, None, S, S, S, S]


@pytest.mark.parametrize("line", ["目的港为宁波市", "交货地是宁波市", "这批也是宁波市"])
def test_place_lines_with_wei_or_shi_below_the_closing_are_body(line: str) -> None:
    """D99 第 47 条：只写地名的那一条，表外的动词“为”“是”的一句话也当成了地名（丢证据）。"""
    assert all(kind in (NEW, None) for kind in body_kinds("李经理：\n\n样品已经寄出。\n\n谢谢\n\n" + line))


@pytest.mark.parametrize("line", ["示例市于洪区", "于都县"])
def test_place_names_with_yu_are_still_places(line: str) -> None:
    """护栏：“于”不当动词，有于洪区、于都县。"""
    text = "\n".join(["The samples left today.", "", "Best regards,", "Lena Ortiz", "", line])
    assert body_kinds(text) == [NEW, None, S, S, None, S]


@pytest.mark.parametrize("line", ["联系采购经理", "这事转经理", "样品交采购", "发票寄财务部", "此事上报经理", "抄送销售经理"])
def test_more_verbs_before_a_title_make_it_a_sentence(line: str) -> None:
    """D99 第 47 条：职位前面带着联系、转、交、寄、上报、抄送的，以前照旧当职位，标成签名（丢证据）。"""
    assert all(kind in (NEW, None) for kind in body_kinds("李经理：\n\n样品已经寄出。\n\n谢谢\n\n" + line))


@pytest.mark.parametrize("line", ["交易部", "转运中心", "寄售部"])
def test_department_names_with_those_characters_are_still_titles(line: str) -> None:
    """护栏：交易部、转运中心、寄售部是部门名。"""
    text = "\n".join(["样品已经寄出。", "", "谢谢", "", "林浩", line])
    assert body_kinds(text) == [NEW, None, S, None, S, S]


@pytest.mark.parametrize("line", ["地址：下月将搬到新厂", "地址：新厂下月会启用", "Add: 新厂下月会启用"])
def test_an_address_label_followed_by_a_sentence_with_hui_or_jiang_is_body(line: str) -> None:
    """D99 第 47 条：写着地址字样的，以前只看开头是不是会、要、将，“地址：下月将搬到新厂”这种一句话标成了签名（丢证据）。"""
    assert all(kind in (NEW, None) for kind in body_kinds("李经理：\n\n样品已经寄出。\n\n谢谢\n\n" + line))


@pytest.mark.parametrize("line", ["地址：示例市将台路 8 号", "地址：示例市会元路 18 号", "地址：将台路 8 号"])
def test_street_names_with_hui_or_jiang_are_still_addresses(line: str) -> None:
    """护栏：会、将后面一两个字内就是路、街、道的是路名（将台路、会元路），不算说话。"""
    text = "\n".join(["样品已经寄出。", "", "谢谢", "", line])
    assert body_kinds(text) == [NEW, None, S, None, S]


# ---------- D101：签名里写公司、职位、部门的那几行能当证据 ----------


@pytest.mark.parametrize(
    ("lines", "kinds"),
    [
        (
            ["The samples left today.", "", "Best regards,", "Lena Ortiz", "Sales Manager", "Example Trading Co., Ltd.",
             "Tel: 021-5555-0100", "Room 1203, Building 3, Example Road"],
            ["new", None, "signature", "signature", "signature_info", "signature_info", "signature", "signature"],
        ),
        (
            ["样品已经寄出。", "", "谢谢", "", "林浩", "销售经理", "示例贸易有限公司", "电话：021-5555-0100", "地址：示例市示例路 8 号"],
            ["new", None, "signature", None, "signature", "signature_info", "signature_info", "signature", "signature"],
        ),
        (
            ["样品已经寄出。", "", "谢谢", "", "公司：示例贸易有限公司", "职位：采购经理", "部门：外贸部"],
            ["new", None, "signature", None, "signature_info", "signature_info", "signature_info"],
        ),
        (
            ["The samples left today.", "", "Best regards,", "Lena Ortiz, Sales Manager", "Sales Manager | 138 0000 0000",
             "lena.ortiz@example.net"],
            ["new", None, "signature", "signature_info", "signature", "signature"],
        ),
        (
            ["The samples left today.", "--", "Lena Ortiz", "Export Department", "www.example.net"],
            ["new", "signature", "signature", "signature_info", "signature"],
        ),
    ],
)
def test_company_title_and_department_lines_in_a_signature_are_evidence(lines: list[str], kinds: list[str | None]) -> None:
    """D101：签名里写公司、职位、部门的那几行能当证据（signature_info）；问候、名字、电话、邮箱、网址、地址照旧只当上文，
    一行里混着电话这些的也照旧。"""
    assert body_kinds("\n".join(lines), info=True) == kinds


def test_a_forwarded_mails_signature_stays_context() -> None:
    """转发进来的信里的签名不算：那是转述，不是发件人自己的话。"""
    lines = ["Please see below.", "", "---- Forwarded Message ----", "From Lin Hao<lin.hao@example.net>",
             "Date 11/03/2026 09:12", "Subject Pallet count", "Pickup moved to the 15th.", "", "Best regards,", "Lin Hao",
             "Sales Manager"]
    kinds = body_kinds("\n".join(lines), "Fwd: Pallet count", info=True)
    assert "signature_info" not in kinds


@pytest.mark.parametrize(
    "line",
    ["Lena Ortiz – Sales Manager", "Lena Ortiz - Export Department", "Lena Ortiz (Sales Manager)", "林浩（采购经理）",
     "销售经理 林浩", "外贸部 林浩"],
)
def test_a_name_and_a_title_on_one_line_in_a_signature_is_evidence(line: str) -> None:
    """D101：名字和职位、部门写在一行的（横线隔开、职位写在括号里、中文职位在前名字在后）也写着职位、部门，能当证据。"""
    kinds = body_kinds("\n".join(["The samples left today.", "--", line, "Tel: 021-5555-0100"]), info=True)
    assert kinds == ["new", "signature", "signature_info", "signature"]


@pytest.mark.parametrize(
    "line", ["Lena Ortiz", "Mary-Jane Ortiz", "林浩", "Lena Ortiz – 138 0000 0000", "林浩 示例路 8 号", "林浩（138 0000 0000）"]
)
def test_names_and_contact_lines_in_a_signature_stay_context(line: str) -> None:
    """护栏：只写名字的、名字连着电话地址的照旧只当上文；Mary-Jane 这种名字里的横线不拆。"""
    kinds = body_kinds("\n".join(["The samples left today.", "--", line]), info=True)
    assert kinds == ["new", "signature", "signature"]



# ---------- 第五轮（第 52 条）：签名里写公司、职位的那一行，混着电话、账号、地址、分机的照旧只当上文 ----------


@pytest.mark.parametrize(
    "line",
    ["Sales Manager 0000 2345 6789", "Head of Sales · WeChat lenaortiz88", "示例贸易有限公司 示例市示例区 000000",
     "示例贸易有限公司 示例市示例区", "Title: Sales Manager, ext. 8012", "Export Department, Skype lenaortiz88"],
)
def test_a_title_or_company_line_with_contact_details_stays_context(line: str) -> None:
    """第 52 条：名字、职位、公司后面直接跟着电话、聊天账号、地名邮编、分机号的，以前整行当了证据（按空白拆开的那几段
    没过一遍是不是电话、地址）。整行照旧只当上文。"""
    kinds = body_kinds("\n".join(["The samples left today.", "--", line]), info=True)
    assert kinds == ["new", "signature", "signature"]


@pytest.mark.parametrize(
    "line", ["示例贸易（示例市）有限公司", "Sales Manager, Example Trading Co., Ltd.", "Est. 1998, Example Trading Co.", "Line Manager, Example Trading Co."]
)
def test_company_lines_with_a_place_in_brackets_or_a_year_are_still_evidence(line: str) -> None:
    """护栏：公司名的括号里写着地名的、写着成立年份（数字少）的，照旧能当证据。"""
    kinds = body_kinds("\n".join(["The samples left today.", "--", line]), info=True)
    assert kinds == ["new", "signature", "signature_info"]


# ---------- 第六轮（第 59 条）：签名那一行按词查时收得太宽 ----------


@pytest.mark.parametrize(
    "line", ["Example Vietnam Co., Ltd.", "Example UK Ltd", "亚太区 销售总监", "华东区 区域经理", "Example Hair Extension Co., Ltd."]
)
def test_countries_regions_and_the_word_extension_in_company_and_title_lines_are_evidence(line: str) -> None:
    """第 59 条：第 52 条按词查时，公司名里带着国名、“…区”结尾的大区后面跟着职位、公司名里写着 Extension 的，整行变回了
    签名（丢证据）。按词查时不看国名；地名后面跟着职位、公司的不挡；ext、extension 后面跟着号码才算分机。"""
    kinds = body_kinds("\n".join(["The samples left today.", "--", line]), info=True)
    assert kinds == ["new", "signature", "signature_info"]


@pytest.mark.parametrize(
    "line", ["Title: Sales Manager, ext. 8012", "Sales Manager, Extension 8012", "示例贸易有限公司 示例市示例区", "China",
             "销售经理，分机 801"]
)
def test_extension_numbers_addresses_and_bare_countries_still_stay_context(line: str) -> None:
    """护栏：分机后面跟着号码的、公司名后面跟着地名的、整段只写国名的，照旧只当上文。"""
    kinds = body_kinds("\n".join(["The samples left today.", "--", line]), info=True)
    assert kinds == ["new", "signature", "signature"]
