"""软变化的结论交给 L5：每种结论 L5 做什么（04 p4“结论交回 L5 由规则执行，模型不直接改库”）。

这里只是纯函数，不碰账本。J4.4 写账（L5 判新旧）时照 action() 的结果改：

| 结论 | 动作 | 旧的是关系 | 旧的是事实 |
| --- | --- | --- | --- |
| A 仍然有效 | keep | 不动 | 不动 |
| B 暂停 | pause | 状态改暂停，写暂停条件、暂停那天；不结束、不失效（D101）。暂停着被别人接手照常失效，接回另起一段 | 旧的照旧现行；这句话另记的“暂停（等……）”和旧的同时现行 |
| C 结束或取消 | end | 状态改已结束，结束那天取 at | 旧的被这句话抽出的那条替代；L3 一条都没抽出来的，进待确认 |
| D 改成新的 | replace | 旧的在 at 那天结束，新的照 L3 抽的那条从那天生效 | 照常新旧，替代时间取 at |
| E 只是犹豫或条件 | keep | 不动；这句话抽出的关系不起 | 不动；这句话抽出的事实记成打算，不参加新旧 |
| F 判断不了 | review | 进待确认（soft_change），旧的不动 | 同左 |

- 判不了（F）、没把握（sure 是 false）、引文在要判的那几行里找不到、前后说不通（说“没说到它”却判了暂停、结束、
  换新）的，一律进待确认（soft_change），带上模型的建议；待着的时候旧的照旧，新说法是待确认、不替代旧的。引文这一条
  六种结论都算（主人 11-16 定的；E 会把这句话抽的记成打算，不是什么都不做），只有一种不要求引文：判 A、又说没说到
  这一条（about 是 false）的，引文空着不进待确认；写了引文却对不上的照旧进（主人 11-16 审阅后定的）。
- 会改旧的（暂停、结束、换新）却写的哪天认不出的，也进待确认问主人，不拿说话那天顶上（主人 11-16 审阅后定的）。
- 碰不碰钱不单独分流（主人 11-16 选的，没按 04 p4 字面）：finance 只记下来当参考。
- D 的新东西只认 L3 抽出来的（needs_new）：L3 没抽出来、只有模型写的 new_value 的，J4.4 送待确认。
- 还没判的候选（还没调、调失败了、不能发的）也是待确认 soft_change：旧的照旧。
- 待确认 30 天没人管：当成不改，旧的照旧，新说法留成打算（主人 11-16 定的，EXPIRE_ACTION）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

VERDICTS = ("A", "B", "C", "D", "E", "F")
VERDICT_TEXT = {
    "A": "仍然有效",
    "B": "暂停",
    "C": "结束或取消",
    "D": "改成新的",
    "E": "只是犹豫或条件，不改",
    "F": "判断不了",
}
KEEP = "keep"
PAUSE = "pause"
END = "end"
REPLACE = "replace"
REVIEW = "review"
ACTIONS = (KEEP, PAUSE, END, REPLACE, REVIEW)
CHANGES = (PAUSE, END, REPLACE)  # 会改旧的状态的
ACTION_OF = {"A": KEEP, "B": PAUSE, "C": END, "D": REPLACE, "E": KEEP, "F": REVIEW}
REVIEW_KIND = "soft_change"  # 待确认的种类（review_queue.kind、pending_reason）
EXPIRE_ACTION = KEEP  # 30 天没人管：当成不改

# 进待确认的原因
UNCLEAR = "unclear"  # 模型判了 F
NOT_SURE = "not_sure"
QUOTE_MISMATCH = "quote_mismatch"
INCONSISTENT = "inconsistent"  # 说没说到它，却判了改它
DATE_UNREADABLE = "date_unreadable"  # 要改旧的，写的哪天认不出
NOT_JUDGED = "not_judged"  # 还没有结论


@dataclass(frozen=True)
class Action:
    kind: str
    at: str | None = None  # 从哪天起（暂停、结束、替代）
    precision: str = "unknown"
    condition: str | None = None  # 暂停等什么；进待确认时是模型写的条件
    needs_new: bool = False  # 替代：新的那条要 L3 抽出来的
    review_kind: str | None = None
    suggestion: str | None = None  # 进待确认时模型的结论（A 到 F），给主人参考
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind, "at": self.at, "precision": self.precision, "condition": self.condition,
            "needs_new": self.needs_new, "review_kind": self.review_kind, "suggestion": self.suggestion,
            "reasons": list(self.reasons),
        }


def _needs_no_quote(verdict: str, item: Mapping[str, object]) -> bool:
    """主人 11-16 审阅后定的：判 A、又说没说到这一条（about 是 false）的不要求引文，引文空着不算对不上。
    写了引文却对不上的不在这里面，照旧算对不上。"""
    quote = item.get("quote")
    empty = quote is None or (isinstance(quote, str) and not quote.strip())
    return verdict == "A" and item.get("about") is False and empty


def action(item: Mapping[str, object], *, said_at: str) -> Action:
    """一条旧的的结论（judge 校验过的那一项）→ L5 的动作。said_at：要判那几行最早的说话时间，结论没写哪天时用它。"""
    verdict = str(item.get("verdict"))
    if verdict not in VERDICTS:
        raise ValueError("结论只能是 A 到 F")
    changes = ACTION_OF[verdict] in CHANGES
    reasons = []
    if verdict == "F":
        reasons.append(UNCLEAR)
    if item.get("sure") is not True:
        reasons.append(NOT_SURE)
    if item.get("quote_ok") is not True and not _needs_no_quote(verdict, item):
        reasons.append(QUOTE_MISMATCH)  # 六种结论都算，只免“A、没说到它、引文空着”这一种
    if item.get("consistent") is False:
        reasons.append(INCONSISTENT)
    if changes and item.get("date_ok") is False:
        reasons.append(DATE_UNREADABLE)  # 不能拿说话那天顶上：“下周一”这类没换算出来的会记错哪天
    raw_condition = item.get("condition")
    condition = raw_condition if isinstance(raw_condition, str) else None
    if reasons:
        return Action(REVIEW, condition=condition, review_kind=REVIEW_KIND, suggestion=verdict, reasons=tuple(reasons))
    kind = ACTION_OF[verdict]
    if kind == KEEP:
        return Action(KEEP)
    at = item.get("effective_at")
    if isinstance(at, str) and at:
        precision = str(item.get("effective_precision") or "unknown")
    else:
        at, precision = said_at, "minute"
    if kind == PAUSE:
        return Action(PAUSE, at, precision, condition=condition)
    if kind == END:
        return Action(END, at, precision)
    return Action(REPLACE, at, precision, needs_new=True)


def not_judged() -> Action:
    """还没有结论的候选：进待确认，旧的照旧。"""
    return Action(REVIEW, review_kind=REVIEW_KIND, reasons=(NOT_JUDGED,))


def expected_action(verdict: str, *, review: bool = False) -> str:
    """复核过的答案应该是哪个动作：该进待确认的是 review，别的照 ACTION_OF（A、E 都是 keep）。"""
    if verdict not in VERDICTS:
        raise ValueError("答案只能是 A 到 F")
    return REVIEW if review else ACTION_OF[verdict]
