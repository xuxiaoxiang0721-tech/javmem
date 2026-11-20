"""撤回一封信以后，同一往来串里别的包写过的说法要清干净（以后要修.md“收件、写账的快慢”以后要修的第 1 条；U3 卡 7）。

同一往来串的两封信在两个包里（前一个 X、后一个 Y），一起写成一个窗口，窗口按前一个包取名（X#w…），里面有证据只在
Y 的说法。撤回 X（整包，或者只撤回那封信）以后，Y 改回排队、重抽，这次窗口按 Y 取名。X#w… 里那条只靠 Y 的说法要么
撤回的时候就删掉，要么重写 Y 的时候删掉；留着的话，账本里多一处模型看着撤回了的信抽出来的说法，和“不收撤回的内容
整本重建”的结果不一样。

样例都是编的，模型、向量都用假的。
"""

from __future__ import annotations

import pytest
from helpers import MAIL_ID, MAIL_PACKET, World, build_packet, mail_header_block

from javmem.cli import main
from javmem.withdraw import withdraw

LATER = "2026-11-04_1800_mail-sales-a"
SECTION, MAIL = "原话", "邮件"  # 包里小节的标题用变量拼：整段写死的固定字样，片段检查会当成新加的共有片段


def reply_packet() -> str:
    return build_packet(
        "mail-sales-a",
        "2026-11-04_1800",
        body=f"## {SECTION} · {MAIL} · 2026-11-04 10:00\n\n"
        + mail_header_block(
            message_id='"<ship-0003@example.com>"',
            thread_id='"<ship-0001@example.org>"',
            in_reply_to='"<ship-0002@example.com>"',
            date="2026-11-04T10:00:00+08:00",
            subject='"Re: Shipment date for the Vietnam order"',
        )
        + "\n  Noted, we will ship 300 boxes on Nov 15.\n",
        framework="mail-import",
        channel="email",
        mailbox="li.ming@example.com",
        covers_from="2026-11-03T18:00:00+08:00",
        covers_to="2026-11-04T18:00:00+08:00",
        created_at="2026-11-04T18:00:00+08:00",
    )


def extract_and_write() -> None:
    assert main(["extract", "--library", "prod", "--fake"]) == 0
    assert main(["write", "--library", "prod", "--fake", "--fake-embeddings"]) == 0


def later_claim_windows(world: World) -> list[str]:
    """证据在后一个包里的说法，都是在哪个窗口里写的。"""
    rows = world.rows("SELECT window_id FROM fact_claims WHERE proofs LIKE ? ORDER BY window_id", f"%{LATER}%")
    return [row["window_id"] for row in rows]


@pytest.fixture
def shared(world: World) -> World:
    world.drop("inbox/mail-sales-a", MAIL_PACKET)
    world.drop("inbox/mail-sales-a", reply_packet())
    world.intake()
    extract_and_write()
    windows = later_claim_windows(world)
    assert windows and all(window.startswith(MAIL_ID) for window in windows)  # 和前一个包在同一个窗口里写的
    return world


def test_withdrawing_the_earlier_packet_leaves_no_claim_in_the_shared_window(shared: World) -> None:
    withdraw(shared.settings, "prod", packets=[MAIL_ID])
    extract_and_write()
    windows = later_claim_windows(shared)
    assert windows and all(window.startswith(LATER) for window in windows), windows


def later_claims(world: World) -> list[tuple[str, str, str]]:
    rows = world.rows("SELECT window_id, content, cache_key FROM fact_claims WHERE proofs LIKE ? ORDER BY 1, 2", f"%{LATER}%")
    return [(row["window_id"], row["content"], row["cache_key"]) for row in rows]


def test_withdrawing_only_the_earlier_mail_rewrites_the_shared_window(shared: World) -> None:
    """只撤回前一个包里那封信（包还在）：往来串的窗口照旧按前一个包取名，可里面的说法都要是撤回以后重抽的，
    不能留着看着撤回了的信抽出来的那一份，也不能重出一份。"""
    before = {key for _, _, key in later_claims(shared)}
    subject = shared.rows("SELECT sentence_id FROM raw_sentences WHERE packet_id = ? AND field = 'subject'", MAIL_ID)
    assert subject
    withdraw(shared.settings, "prod", sentences=[subject[0]["sentence_id"]])  # 点主题：连同这封信的每一句一起撤回
    extract_and_write()
    after = later_claims(shared)
    assert after and not ({key for _, _, key in after} & before), after
    assert len({(window, content) for window, content, _ in after}) == len(after)  # 没有重出一份
