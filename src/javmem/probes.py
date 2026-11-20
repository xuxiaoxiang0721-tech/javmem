"""探针（U2 甲，D205 ⑪；Qwen 那一样 D229 ⑫ 改过）：本机的两个模型（向量服务、Qwen）用编的句子查有没有悄悄变。
`javmem versions` 跑它们。

不花钱：只连本机的服务。句子、题都是编的（人名、店名、数、日子都是编的），不碰真数据。

- **向量：** 20 句编的话过向量服务（一批最多 16 条：两批），每句的向量存下来（float32，base64）。下一次每一维都比，
  差都不到 `EMBED_TOLERANCE`（万分之一）才算没变。模型名、维数不一样也算变了。线是在这台机器上照同一个服务连着跑几遍量出来的
  （报告 `U2-甲.md`）：同一个模型文件、同一个服务程序，几遍之间一点不差；换了模型、换了量化、换了服务程序，会超过线。
- **Qwen：** 10 道编的“光看这几句原话，能不能看出上面这条关系”，照判断 C 本机那一档的问法（同一个系统提示、同一句问话、
  温度 0、只要一个字、前十个候选字），照它的算法记 P(是)。下一次比，差不到 `QWEN_TOLERANCE`（0.05）才算没变；回的第一个字
  也比，变了就算变了。线原来是 0.01（11-19 空着连跑三遍一模一样）；U1 那一夜有人在用时差到 0.10，空下来再量也差到 0.04，
  回的字都没变，D229 ⑫ 放到 0.05。
- **量的时候核有没有别人在用**（D229 ⑫，`pq2`）：本机 Qwen 和别的程序共用，llama.cpp 几路并着算，别人的请求挤在同一批里，
  数会抖；本机的 llama.cpp 关着 /slots（它会露出最近的提示词）、/metrics，看不到忙不忙。所以不靠它：关掉提示缓存（有人在用时
  算歪的那段系统提示不会留着接着用），每道连问两遍，两遍 P 差不到 `QWEN_AGAIN_TOLERANCE`（0.005）、回的字一样才算量到。
  一对不上就收掉这一回（不把 10 道问完，免得接着挤别人），当有人在用，隔 `QWEN_RETRY_SECONDS`（30 秒）再量；
  `QWEN_WAIT_SECONDS`（5 分钟）都对不上，这回没量到（记成 busy），不算变了。
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from javmem.embedding import EMBEDDING_DIM, MAX_BATCH, Embedder, unpack_vector

EMBED_TOLERANCE = 1e-4
QWEN_TOLERANCE = 0.05  # D229 ⑫：原来 0.01，U1 那一夜空着再量也差到 0.04（回的字没变），放到 0.05
QWEN_AGAIN_TOLERANCE = 0.005  # 同一道连问两遍，P 差不到这个、回的字一样，才算量到（D229 ⑫）
QWEN_RETRY_SECONDS = 30.0  # 两遍对不上：当有人在用，隔这么久再量
QWEN_WAIT_SECONDS = 300.0  # 一直对不上，等到这么久就不量了：这回没量到

# 编的 20 句：中文、英文、数、日子、口语都有一点。改了句子要换 EMBED_PROBE_VERSION（存下的向量就对不上了）
EMBED_PROBE_VERSION = "pe1"
EMBED_SENTENCES: tuple[str, ...] = (
    "鹿鸣书店周三下午进了一批旧版地图册，店员说先放在二楼靠窗的架子上。",
    "青禾农场今年的番茄比去年早熟了十来天。",
    "The ferry to the lighthouse leaves at 7:40 on weekdays and 9:10 on Sundays.",
    "秋水说她下个月要去山里的民宿住两个星期，手机信号不太好。",
    "石桥茶社把会员卡的积分规则改成了每消费二十元记一分。",
    "Please move the pottery class from Thursday to Saturday morning.",
    "仓库里还剩三百四十二箱柚子罐头，保质期到明年五月。",
    "老周觉得那台二手投影仪太吵了，想退回去换一台静音的。",
    "雨下得太大，蓝湾灯具的样品要推迟到二十八号才能寄出。",
    "她把自行车停在图书馆后门，结果被管理员贴了一张提醒条。",
    "We agreed that the mural on the east wall should stay mostly blue.",
    "白杨在电话里说，那批松木椅子的扶手要再打磨一遍。",
    "社区合唱团每周二晚上七点半在小学礼堂排练。",
    "冰箱的冷藏室温度显示四度，可里面的酸奶还是有点结冰。",
    "The invoice number for the garden lights is GL-0417, total 1,260 dollars.",
    "如果周末不下雨，我们就去河边的草地放风筝。",
    "猫咪最近只肯吃鸡肉味的罐头，鱼味的一口都不碰。",
    "邻居送来一篮自家种的枇杷，说是今年第一批。",
    "那家面馆的招牌换成了木头的，字是老板自己写的。",
    "明早八点的会改到线上开，链接稍后发到群里。",
)

# 编的 10 道“能不能看出这条关系”（判断 C 的问法）。前 5 道原话说得清楚，后 5 道原话里看不出来。
# 改了题、改了问法要换 QWEN_PROBE_VERSION。pq2（D229 ⑫）：关掉提示缓存、每道连问两遍；pq1 存的基准比不了，要重存
QWEN_PROBE_VERSION = "pq2"
QWEN_QUESTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("白杨 —任职于→ 蓝湾灯具（状态：生效）", ("T1 我：白杨上个月起在蓝湾灯具做采购，样品都是他寄的。",)),
    ("秋水 —负责→ 石桥茶社的会员活动（状态：生效）", ("T1 我：石桥茶社的会员活动现在归秋水管。", "T2 我：有事直接找她。")),
    ("老周 —持有→ 二手投影仪（状态：生效）", ("T1 我：老周上周买了一台二手投影仪，放在他家客厅。",)),
    ("鹿鸣书店 —客户是→ 青禾农场（状态：生效）", ("T1 我：鹿鸣书店的咖啡角一直从青禾农场订番茄和生菜。",)),
    ("白杨 —亲属→ 秋水（主体是客体的哥哥）（状态：生效）", ("T1 我：秋水的哥哥白杨下周回来过节。",)),
    ("秋水 —任职于→ 鹿鸣书店（状态：生效）", ("T1 我：秋水周末常去鹿鸣书店看书。",)),
    ("老周 —负责→ 社区合唱团（状态：生效）", ("T1 我：社区合唱团每周二晚上在小学礼堂排练。", "T2 我：老周偶尔去听。")),
    ("青禾农场 —托管于→ 蓝湾灯具（状态：生效）", ("T1 我：青禾农场今年的番茄比去年早熟了十来天。",)),
    ("白杨 —持有→ 石桥茶社的会员卡（状态：生效）", ("T1 我：石桥茶社把会员卡的积分规则改了。",)),
    ("鹿鸣书店 —任职于→ 老周（状态：已结束）", ("T1 我：老周说那台投影仪太吵了，想退回去。",)),
)


@dataclass(frozen=True)
class ProbeCall:
    """一次探针调用，记 token 账用：用途、模型、第几批（Qwen 是第几道）、结果、用了多少 token、几秒、第几回（Qwen 两遍对不上
    要隔一会再量，D229 ⑫）。"""

    purpose: str
    model: str
    index: int
    status: str
    input_tokens: int
    output_tokens: int
    seconds: float
    attempt: int = 1


Record = Callable[[ProbeCall], None]


def _digest(blobs: Sequence[bytes]) -> str:
    return hashlib.sha256(b"".join(blobs)).hexdigest()


def embed_probe(embedder: Embedder, record: Record | None = None) -> dict[str, object]:
    """20 句过一遍向量服务。出错的照样抛（EmbeddingError），调的人决定怎么报。"""
    blobs: list[bytes] = []
    for index, start in enumerate(range(0, len(EMBED_SENTENCES), MAX_BATCH)):
        batch = EMBED_SENTENCES[start : start + MAX_BATCH]
        reply = embedder.embed(batch)
        if record is not None:
            record(ProbeCall("versions_probe_embed", embedder.name, index, "ok_estimated" if reply.estimated else "ok",
                             reply.tokens, 0, reply.seconds))
        blobs.extend(reply.vectors)
    return {
        "version": EMBED_PROBE_VERSION,
        "model": embedder.name,
        "dim": EMBEDDING_DIM,
        "sha256": _digest(blobs),
        "vectors": [base64.b64encode(blob).decode("ascii") for blob in blobs],
    }


def compare_embed(saved: Mapping[str, object], now: Mapping[str, object]) -> list[str]:
    """不一样的地方，一条一句；一样的是空的。"""
    if saved.get("version") != now.get("version"):
        return [f"向量探针换了版本（{saved.get('version')} → {now.get('version')}），没法比，要重新 --save"]
    out = []
    if saved.get("model") != now.get("model"):
        out.append(f"向量模型的名字变了：{saved.get('model')} → {now.get('model')}")
    if saved.get("dim") != now.get("dim"):
        out.append(f"向量的维数变了：{saved.get('dim')} → {now.get('dim')}")
        return out
    if saved.get("sha256") == now.get("sha256"):
        return out
    worst = 0.0
    moved = 0
    for one, two in zip(saved.get("vectors") or [], now.get("vectors") or []):  # type: ignore[call-overload]
        a = unpack_vector(base64.b64decode(one))
        b = unpack_vector(base64.b64decode(two))
        gap = max(abs(x - y) for x, y in zip(a, b))
        worst = max(worst, gap)
        moved += gap >= EMBED_TOLERANCE
    if moved:
        out.append(f"向量探针：{moved} / {len(EMBED_SENTENCES)} 句的向量变了，最大差 {worst:.2e}（线是 {EMBED_TOLERANCE:.0e}）")
    return out


def qwen_question(relation: str, lines: Sequence[str]) -> str:
    """照判断 C 本机那一档的样子写一道题：【关系】……【原话】…… 加上那一句问话。"""
    from javmem.pipeline.reljudge import LOCAL_ASK

    return f"【关系】{relation}\n【原话】\n" + "\n".join(lines) + "\n" + LOCAL_ASK


def _ask_qwen(local: object, system: str, question: str, record: Record | None, index: int, attempt: int) -> dict[str, object]:
    """问一遍：{p: P(是)（6 位小数，回的不是“是 / 否”的是 None）, first: 回的第一个字}。每问一遍记一行账。"""
    from javmem.pipeline.reljudge import local_answer

    reply, seconds = local.chat(system, question, max_tokens=1, logprobs=True)  # type: ignore[attr-defined]
    p, first, _ = local_answer(reply)
    usage = reply.get("usage") if isinstance(reply, Mapping) else None
    usage = usage if isinstance(usage, Mapping) else {}
    if record is not None:
        record(ProbeCall("versions_probe_qwen", str(getattr(local, "name", "")), index, "ok" if p is not None else "no_answer",
                         int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), seconds, attempt))
    return {"p": None if p is None else round(p, 6), "first": first}


def qwen_steady(one: Mapping[str, object], two: Mapping[str, object]) -> bool:
    """同一道连问的两遍对不对得上（D229 ⑫）：回的字一样，P 差不到 QWEN_AGAIN_TOLERANCE（照 6 位小数比，正好差到的算对不上）；
    两遍回的都不是“是 / 否”（P 是 None）的，回的字一样也算对得上。"""
    if one.get("first") != two.get("first"):
        return False
    p1, p2 = one.get("p"), two.get("p")
    if p1 is None or p2 is None:
        return p1 is None and p2 is None
    return round(abs(p1 - p2), 6) < QWEN_AGAIN_TOLERANCE  # type: ignore[operator]


def qwen_probe(
    local: object,
    record: Record | None = None,
    *,
    wait: float = QWEN_WAIT_SECONDS,
    every: float = QWEN_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    say: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """10 道题过本机 Qwen（判断 C 的系统提示，温度 0，一个字，前十个候选字），每道连问两遍（D229 ⑫）。local 是
    preb.LocalModel 那样的（chat(system, user, max_tokens=, logprobs=) -> (回复, 秒数)），要关掉提示缓存（run_probes 照这样建）。

    每道两遍对得上（qwen_steady）才问下一道；有一道对不上就收掉这一回（当有人在用：别的程序的请求挤进了同一批），隔 every 秒
    从头再量。10 道都对得上的是量到了：{version, model, answers（每道第一遍的）, tries（量了几回）, spread（对上的两遍最大差）}。
    再等一回就过 wait 秒的，不量了：{busy: 一句话, waited: 等了几秒, tries}，这回没量到，不算变了。连不上的照样抛，调的人决定
    怎么报。sleep、clock 给测试换成假的钟；say 给了的，每回要等之前说一句（javmem versions 打到 stderr：最多等 5 分钟，别当它卡住了）。"""
    from javmem.pipeline.reljudge import load_judge_prompt

    system = load_judge_prompt()
    questions = [qwen_question(relation, lines) for relation, lines in QWEN_QUESTIONS]
    started = clock()
    tries = 0
    while True:
        tries += 1
        answers: list[dict[str, object]] = []
        spread = 0.0
        for index, question in enumerate(questions):
            one = _ask_qwen(local, system, question, record, index, tries)
            two = _ask_qwen(local, system, question, record, index, tries)
            if not qwen_steady(one, two):
                break  # 一对不上就收掉这一回：有人在用时不接着挤人家
            answers.append(one)
            if one["p"] is not None:
                spread = max(spread, round(abs(one["p"] - two["p"]), 6))  # type: ignore[operator]
        else:
            return {"version": QWEN_PROBE_VERSION, "model": str(getattr(local, "name", "")), "answers": answers,
                    "tries": tries, "spread": spread}
        waited = clock() - started
        if waited + every > wait:
            return {"busy": f"量了 {tries} 回、等了 {int(waited)} 秒，同一道连问两遍总对不上，像是一直有别人在用",
                    "waited": int(waited), "tries": tries}
        if say is not None:
            say(f"本机 Qwen 探针第 {len(answers) + 1} 道问两遍对不上，像是有别人在用：{int(every)} 秒后从头再量"
                f"（第 {tries + 1} 回，最多等 {int(wait)} 秒）")
        sleep(every)


def compare_qwen(saved: Mapping[str, object], now: Mapping[str, object]) -> list[str]:
    if saved.get("version") != now.get("version"):
        return [f"Qwen 探针换了版本（{saved.get('version')} → {now.get('version')}），没法比，要重新 --save"]
    out = []
    if saved.get("model") != now.get("model"):
        out.append(f"Qwen 的名字变了：{saved.get('model')} → {now.get('model')}")
    moved: list[str] = []
    for index, (one, two) in enumerate(zip(saved.get("answers") or [], now.get("answers") or []), start=1):  # type: ignore[call-overload]
        p1, p2 = one.get("p"), two.get("p")
        # 差不到线才算没变，正好差到线上的算变了；P 存的是 6 位小数，差也照 6 位比（不让浮点的尾巴定输赢）
        if (p1 is None) != (p2 is None) or (p1 is not None and round(abs(p1 - p2), 6) >= QWEN_TOLERANCE) \
                or one.get("first") != two.get("first"):
            moved.append(f"第 {index} 题 P(是) {p1} → {p2}（回 {one.get('first')!r} → {two.get('first')!r}）")
    if moved:
        out.append(f"Qwen 探针：{len(moved)} / {len(QWEN_QUESTIONS)} 道题变了（线是 {QWEN_TOLERANCE}）：" + "；".join(moved))
    return out
