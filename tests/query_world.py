"""查询接口（J5.5）测试用的小世界：照 query-api 里编的名字和句子（周建华、老周、李明、小李、越南那批订单、示例贸易、
报价单），走真的收件、假 L3、L4、写账。规范第 14 节的两个包加几包现编的，名字、句子、号码都是编的。

账本写好以后只读地查，所以一个测试文件搭一次（scope="module"），各个测试共用。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from helpers import ACCESS_YAML, CHAT_PACKET, MAIL_PACKET, SHANGHAI, SOURCES_YAML, World, build_packet
from test_relcheck import relation
from test_write import checked_items, l3_fact

from javmem.config import Settings
from javmem.embedding import FakeEmbeddings
from javmem.ledger import open_ledger
from javmem.pipeline.batch import load_queued
from javmem.pipeline.store import ResultStore
from javmem.pipeline.vectors import VectorCache
from javmem.pipeline.window import Window
from javmem.pipeline.write import LedgerWriter, run_write
from javmem.review import freeze
from javmem.zh.lexicon import load_lexicon

AS_OF = datetime(2026, 11, 10, 9, 30, tzinfo=SHANGHAI)
AS_OF_TEXT = "2026-11-10T09:30:00+08:00"

LEXICON = """\
format: javmem-lexicon/2
aliases:
  - {type: 人, names: [李明, 小李]}
words: [越南那批订单, 报价单]
"""

LIMING = {"key": "e1", "mention": "小李", "name": "李明", "type": "人"}
ORDER = {"key": "e2", "mention": "越南那批订单", "name": "越南那批订单", "type": "订单或合同"}
ZHOU = {"key": "e3", "mention": "老周", "name": "周建华", "type": "人"}
TRADE = {"key": "e4", "mention": "示例贸易", "name": "示例贸易", "type": "公司或机构"}
BOOKS = {"key": "e5", "mention": "工资单", "name": "十一月工资单", "type": "项目"}
TRIAL_ORDER = {"key": "e6", "mention": "越南那批", "name": "越南那批订单", "type": "订单或合同"}
ZHOUHAI = {"key": "e7", "mention": "周总", "name": "周海", "type": "人"}
LOGISTICS = {"key": "e8", "mention": "示例物流", "name": "示例物流", "type": "公司或机构"}


def day_body(*sections: tuple[str, list[tuple[str, str, str]]]) -> str:
    """几个原话小节：每个 (标题里的时段, [(时分, 说话人, 原文)])。"""
    out = []
    for title, lines in sections:
        out.append(f"## 原话 · {title}\n" + "".join(f"- [{at}] {who}：{text}\n" for at, who, text in lines))
    return "\n".join(out)


def chat(day: str, body: str, *, source: str = "desk-bot", stamp: str = "2330", **fields: object) -> str:
    return build_packet(
        source=source,
        stamp=f"{day}_{stamp}",
        body=body,
        created_at=f"{day}T23:30:00+08:00",
        covers_from=f"{day}T00:00:00+08:00",
        covers_to=f"{day}T{stamp[:2]}:{stamp[2:]}:00+08:00",
        **fields,
    )


P0920 = chat("2026-09-20", day_body(("09:12", [("09:12", "我", "越南那批订单交给小李跟。")])))
P0925 = chat(
    "2026-09-25",
    day_body(("11:20–11:21", [("11:20", "我", "老周就是周建华，在示例贸易管物流。"), ("11:21", "我", "小李也在示例贸易。")])),
)
P1008 = chat("2026-10-08", day_body(("15:20", [("15:20", "我", "越南订单的定金到账了。")])))
# 两个人都叫“周总”（文档 5.2 对到不止一个实体的例子）；周海“10 月起”在示例物流，只知道到月
P1101 = chat(
    "2026-11-01",
    day_body(("09:00–09:01", [("09:00", "我", "周总就是周建华。"), ("09:01", "我", "周海也叫周总，10 月起在示例物流上班。")])),
)
P1109 = chat(
    "2026-11-09",
    day_body(
        ("14:08–14:10", [("14:08", "我", "下午开会。"), ("14:10", "我", "越南那批的尾款先放放。")]),
        ("17:20", [("17:20", "老周", "舱位 11 月 12 日前订好，越南那批订单不耽误。")]),
    ),
)
# 冻结了“11 月 12 日前”以后又来的新说法：和冻结的冲突，新说法待确认（文档 4.3 最后一句）
P1109B = chat("2026-11-09", day_body(("23:40", [("23:40", "老周", "舱位改到 11 月 14 日前订好，越南那批订单不耽误。")])),
              stamp="2359", kind="adhoc")
# 刚到、还没抽的包（文档 5.5 的第一条结果）
NEW_ID = "2026-11-10_0900_desk-bot"
NEW_PACKET = build_packet(
    stamp="2026-11-10_0900",
    created_at="2026-11-10T09:00:00+08:00",
    covers_from="2026-11-09T23:30:00+08:00",
    covers_to="2026-11-10T09:00:00+08:00",
    kind="adhoc",
    body="## 原话 · 08:50\n- [08:50] 我：新的报价单今天发给客户。\n",
)
# 管账的包：finance 分区、payroll 敏感标记（文档第 7 节的例子）
BOOKS_PACKET = chat(
    "2026-11-05",
    day_body(("10:00", [("10:00", "我", "十一月的工资单月底发。")])),
    source="books-bot",
    domain="[finance]",
    sensitive="[payroll]",
)
# D83 的测试才用（不在 build_world 里）：管账的那一边对越南那批订单的尾款冻结了又改口；一句只说“周总”的话（两个人都叫周总）
FINANCE_FIRST = chat("2026-11-06", day_body(("10:00", [("10:00", "我", "越南那批订单的尾款是 5 万。")])),
                     source="books-bot", domain="[finance]")
FINANCE_SECOND = chat("2026-11-07", day_body(("10:00", [("10:00", "我", "越南那批订单的尾款改成 6 万。")])),
                      source="books-bot", domain="[finance]")
WHO_IS_ZHOU = chat("2026-11-09", day_body(("21:00", [("21:00", "我", "周总明天来对一下。")])), stamp="2101", kind="adhoc")
SOMEONE_ZHOU = {"key": "e9", "mention": "周总", "name": "周总", "type": "人"}
# 测试库：trial-bot（文档第 8 节）
TRIAL_PACKET = chat(
    "2026-11-09",
    day_body(("20:00", [("20:00", "我", "越南那批改到 11 月 20 日出货。")])),
    source="trial-bot",
    env="test",
)


def extract(window: Window) -> dict[str, object]:
    """假 L3：照几句编好的话给事实、关系。"""
    entities: dict[str, dict[str, object]] = {}
    facts: list[dict[str, object]] = []
    relations: list[dict[str, object]] = []

    def use(*items: dict[str, object]) -> None:
        for item in items:
            entities[str(item["key"])] = item

    for line in window.targets:
        text = line.text.rstrip("。")
        ref = line.ref
        if text == "越南那批订单交给小李跟":
            use(LIMING, ORDER)
            relations.append(relation(LIMING, "负责", ORDER, (ref, text), valid_from="2026-09-20", time_precision="day"))
        elif text == "越南那批订单还是小李跟":
            use(LIMING, ORDER)
            relations.append(relation(LIMING, "负责", ORDER, (ref, text)))
        elif text == "老周就是周建华，在示例贸易管物流":
            use(ZHOU, TRADE)
            relations.append(relation(ZHOU, "任职于", TRADE, (ref, text)))
        elif text == "小李也在示例贸易":
            use(LIMING, TRADE)
            relations.append(relation(LIMING, "任职于", TRADE, (ref, text)))
        elif text == "周总就是周建华":
            use({**ZHOU, "mention": "周总"})
            facts.append(l3_fact(window, text, "周建华也叫周总", subject="e3", attribute="称呼", value="周总"))
        elif text == "周海也叫周总，10 月起在示例物流上班":
            use(ZHOUHAI, LOGISTICS)
            relations.append(relation(ZHOUHAI, "任职于", LOGISTICS, (ref, text), valid_from="2026-10", time_precision="month"))
        elif text.startswith("刚才说的改一下"):
            quote = "刚才说的改一下，越南那批从下周一开始交给老周"
            use({**ORDER, "mention": "越南那批"}, ZHOU)
            relations.append(relation(ZHOU, "负责", {**ORDER, "mention": "越南那批"}, (ref, quote),
                                      valid_from="2026-11-09", time_precision="day"))
        elif text == "[转发 自 小李] 客户说出货可以推到 11 月 15 日":
            use({**ORDER, "mention": "越南那批"})
            facts.append(l3_fact(window, text, "客户说越南那批订单的出货可以推到 11 月 15 日", subject="e2",
                                 attribute="出货日期", value="2026-11-15", occurred_at="2026-11-08", time_precision="day"))
        elif text == "Could we move the shipment to Nov 15?":
            use({**ORDER, "mention": "Vietnam order"})
            facts.append(l3_fact(window, text, "客户在邮件里提出把越南那批订单的出货推到 11 月 15 日", subject="e2",
                                 attribute="出货日期", value="2026-11-15", occurred_at="2026-11-03", time_precision="day"))
        elif text == "越南订单的定金到账了":
            order = {**ORDER, "mention": "越南订单"}
            use(order)
            facts.append(l3_fact(window, text, "越南那批订单的定金到账", subject="e2", attribute="定金", value="已到账",
                                 occurred_at="2026-10-08", time_precision="day"))
        elif text == "越南那批的尾款先放放":
            use({**ORDER, "mention": "越南那批"})
            facts.append(l3_fact(window, text, "越南那批订单的尾款可能暂缓", subject="e2", attribute="尾款", value="暂缓",
                                 occurred_at="2026-11-09", time_precision="day", certainty="vague"))
        elif text.startswith(("舱位 11 月 12 日前订好", "舱位改到 11 月 14 日前订好")):
            use(ORDER, ZHOU)
            day = "2026-11-12" if "12 日" in text else "2026-11-14"
            facts.append(l3_fact(window, text, f"周建华说越南那批订单的舱位 {int(day[-2:])} 日前订好", subject="e2",
                                 attribute="订舱期限", value=day, occurred_at="2026-11-09", time_precision="day",
                                 entities=[{"key": "e2", "role": "subject"}, {"key": "e3", "role": "mention"}]))
        elif text == "十一月的工资单月底发":
            use(BOOKS)
            facts.append(l3_fact(window, text, "十一月的工资单月底发", subject="e5", attribute="发放时间", value="月底"))
        elif text in ("越南那批订单的尾款是 5 万", "越南那批订单的尾款改成 6 万"):
            use(ORDER)
            amount = "5万" if "5 万" in text else "6万"
            facts.append(l3_fact(window, text, f"越南那批订单的尾款是 {amount}", subject="e2", attribute="尾款金额", value=amount))
        elif text == "周总明天来对一下":
            use(SOMEONE_ZHOU)
            facts.append(l3_fact(window, text, "周总明天来对一下", subject="e9", attribute="安排", value="明天来对一下"))
        elif text == "越南那批改到 11 月 20 日出货":
            use(TRIAL_ORDER)
            facts.append(l3_fact(window, text, "越南那批订单改到 11 月 20 日出货", subject="e6", attribute="出货日期",
                                 value="2026-11-20", occurred_at="2026-11-20", time_precision="day"))
    return {"entities": list(entities.values()), "facts": facts, "relations": relations}


def settings_for(root: Path, **extra: str) -> Settings:
    env = {
        "JAVMEM_DATA_DIR": str(root / "data"),
        "JAVMEM_SOURCES_FILE": str(root / "sources.yaml"),
        "JAVMEM_ACCESS_FILE": str(root / "access.yaml"),
        "JAVMEM_LEXICON_FILE": str(root / "lexicon.yaml"),
        "JAVMEM_TIMEZONE": "+08:00",
        **extra,
    }
    return Settings.from_env(env)


def write_all(world: World, extract_fn: Callable[[Window], Mapping[str, object] | None], library: str) -> None:
    items = checked_items(world, extract_fn, library)
    conn = open_ledger(world.settings.library(library))
    try:
        embedder = FakeEmbeddings()
        writer = LedgerWriter(conn, extracted_by="l3-v1 fake", embedding_model=embedder.name,
                              lexicon=load_lexicon(world.settings.lexicon_file))
        batch = load_queued(world.settings, library)
        with ResultStore(world.data / "l3") as store, VectorCache(world.data / "vectors") as cache:
            run_write(items, writer, packets=[(p.packet_id, p.header.revision) for p in batch.packets],
                      store=store, cache=cache, embedder=embedder, sleep=lambda _: None)
    finally:
        conn.close()


def build_world(root: Path) -> World:
    """搭好的世界：时钟停在文档的查询时刻（2026-11-10 09:30），身份是 desk-bot。"""
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    (root / "access.yaml").write_text(ACCESS_YAML, encoding="utf-8")
    (root / "lexicon.yaml").write_text(LEXICON, encoding="utf-8")
    world = World((root / "data").resolve(), settings_for(root)).at(AS_OF)
    for packet in (P0920, P0925, P1008, P1101, CHAT_PACKET, P1109):
        world.drop("inbox/desk-bot", packet)
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.drop("inbox/books-bot", BOOKS_PACKET)
    world.drop("inbox-test/trial-bot", TRIAL_PACKET)
    world.intake()
    write_all(world, extract, "prod")
    write_all(world, extract, "test")
    (booking,) = world.rows("SELECT fact_id FROM facts WHERE attribute = '订舱期限'")
    freeze(world.settings, "prod", [booking["fact_id"]])
    world.drop("inbox/desk-bot", P1109B)
    world.intake()
    write_all(world, extract, "prod")
    world.drop("inbox/desk-bot", NEW_PACKET)  # 写账以后才到：排队等抽取
    world.intake()
    return world.as_caller("desk-bot")


def rows(world: World, sql: str, *params: object, library: str = "prod") -> list[sqlite3.Row]:
    return world.rows(sql, *params, library=library)
