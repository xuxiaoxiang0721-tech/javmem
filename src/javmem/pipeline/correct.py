"""对话里纠正关系（J4.6，D7、05 p4 第 8 条“纠正照常录”）：认出“主人在说记着的不对”，交给 L5 照规则改。

纠正照原话进包、照常抽（L3 不改），这里只回答几个问题，L5（javmem.pipeline.write、merge）照答案办：

- **这一处说法是不是纠正**（correction_sentence）：证据里有主人自己的话（对话包、原话，不是转发、外部内容、引用），那一句、
  同一条消息里它前面的一句、或者主人紧挨着的上一条（只有“不对”“错了”这种几个字、两分钟以内）有纠正词。返回带纠正词的
  那一句的编号。会不会改到组里以前说的、是不是软变化（软变化优先），L5 看。
- **纠正词**（is_cue）：说错了（不对、错了、记错、搞错……，单独一句的“不是的”）、改口（不是……是……、其实是、我是说、
  更正）、早就变了（早就、早已、已经不在、根本没……）、英文的 wrong、actually、I meant、not … but …。问句（问号、吗吧呢嘛
  结尾、是不是、对不对、是否）不算：拿不准的不替主人改。“不对劲”“不对外”“对不对”“不错了”“没错了”“没记错”“并不急”
  “not only … but also”这些不算。纠正词在说法自己那一句以外（前一句、上一条）的，那一句带犹豫词（“可能不对”）也不算。
- **Bot 当场说错**（bot_message、message_key）：规则清单 R05“会错了意、主人当场纠正的，不记复述那一版”。证据全在 Bot 的
  一条消息里的那一处，主人紧接着的下一条就是纠正同一组的：L5 不记它。L3 把 Bot 那句和主人的纠正给成同一处说法的
  （Bot 那条后面紧接着就是带纠正词那一句所在的消息：message_of），Bot 那句不拿来认软变化；说它结束了的那一处，Bot 说的
  （不是问话）不算证据（L5 去掉，记进隔离表）。
- **抽取没抽出来的纠正**（negated_types）：主人说了纠正、又是“不负责”“不在……了”这种否定，L3 没抽出能用的说法：
  L5 照这句话和 Bot 的上一句里提到的两头，找对得上的唯一一条现行关系，进待确认问主人（主人 11-17 第 4 题 B）。
- **严格分区**（strict_partitions）：投资、财务的关系被纠正，先挡住问主人（D7，主人 11-17 第 2 题 A）。哪些分区算严格写在
  配置 JAVMEM_STRICT_PARTITIONS（逗号隔开），默认 finance、investment（packet-spec 第 4 节）。

词表是写死在代码里的普通说法，不是原话；改了要换 WORDS_VERSION，认出来的跟着变，要整本重建。
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

from javmem.soft import words as soft_words

WORDS_VERSION = "correct-words-v5"  # v2：第一轮审阅（单独的“不是的”；“不对外”“并不”“not only”不算；带犹豫词的纠正词不算）
# v3：主人 11-17 复核 K12（只说了新的一家时，“其实”也算问一句的纠正词，见 asks_about_others）
# v4：K12 那条的审阅（只问任职于；说了“也在”“还在”“兼职”“另外在”“同时在”“又在”这些的不问，见 keeps_others）
# v5：D210 加了隶属于：抽取没抽出来的纠正也认它的否定说法（_NEGATED）
CORRECTION = "correction"  # 待确认（manual_review）的原因：财务投资的关系被纠正，先挡住问主人
UNMATCHED = "correction_unmatched"  # 待确认（manual_review）的原因：抽取没抽出来、对得上唯一一条现行关系的纠正
OTHER = "correction_other"  # 待确认（manual_review）的原因：可以有几条的关系只说了新的那一条，问旧的那一条还算不算
ON_SPOT = "corrected_on_spot"  # 隔离表的原因（规则 L5）：Bot 当场说错、主人紧接着纠正的那一条（R05）
REASONS = (CORRECTION, UNMATCHED, OTHER)
ASKED = (UNMATCHED, OTHER)  # 问主人“是不是说这一条”的两种：认了（keep_new）那一条在说那一句的时候结束
# 只说了新的那一条要问旧的那一条的关系：可以同时有几条、平常是一条的（一个人在几家上班）。客户是（在几家开户、是几家的
# 客户很常见）、持有（本来就是好几样）不问；同一时间一个的（托管于、负责、隶属于）照换掉办；亲属照亲属的规矩（主人 11-17
# 复核 K12）
ASK_OTHERS = frozenset({"任职于"})
SHORT_CHARS = 6  # 主人紧挨着的上一条只有这么多个字以内（去掉标点和空白）才算“只说了不对”
SHORT_SECONDS = 120  # 而且离这一句不超过这么多秒
ENV_STRICT = "JAVMEM_STRICT_PARTITIONS"
DEFAULT_STRICT = frozenset({"finance", "investment"})

_GROUPS: dict[str, str] = {
    # 说错了：“对不对”“不对劲”“不对称”“不对付”“不对头”“不对外”“不对吗/吧/呢”不算；“不错了”“没错了”“没记错”“没说错”
    # 不算。单独一句的“不是的”也算（做法 1.2 第 2 条）；只有“不是”两个字的多半是在答 Bot 的是不是，不算
    "说错了": r"(?<!对)不对(?![劲称付头吗吧呢外])|(?<![不没])错了|(?<![不没])(?:记|搞|弄|说|写|看|听|算)错"
              r"|^\s*不是的\s*[。！!…~～，,]*\s*$",
    # 改口
    "改口": r"不是[^，。,.；;！!？?、]{1,15}[，,、]?\s*(?:而)?是|其实是|我是说|更正|纠正一下|纠正一点",
    # 早就变了（做法的词表：没有“并不”，“并不急”这种不是纠正）
    "早就变了": r"早就|其实早|早已|已经不在|根本没|从来没|并没有",
    # 英文：“not only … but also”不算
    "英文": r"\bwrong\b|\bactually\b|\bcorrection\b|\bi meant\b|\bnot\b(?!\s+only\b)[^.;!?]{1,40}\bbut\b",
}
_COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in _GROUPS.items()}
# 问句、拿不准的：问号结尾、“吗吧呢嘛”结尾，带“是不是”“对不对”“是否”
_QUESTION = re.compile(r"[?？]\s*[\"”’')）]*\s*$|[吗吧呢嘛]\s*[。！!…~～]*\s*$|是不是|对不对|是否")
_PUNCT = re.compile(r"[\s，。,.；;！!？?、：:…~～\"“”'‘’()（）\[\]［］【】—-]+")

# 抽取没抽出来的纠正：说的是哪种关系不对（否定词紧挨着关系的说法）。亲属不在里面：只说“不是妹妹”看不出是什么
_NOT = r"(?:不|没|没有|不再|并不|早就不|早不|已经不|根本不|从来不|从来没|早就没|已经没)"
_NEGATED: dict[str, str] = {
    "负责": _NOT + r"(?:是|由)?(?:负责|管|跟|对接)",
    "任职于": _NOT + r"在",
    "托管于": _NOT + r"(?:在|放在)",
    "持有": r"(?:没有|没持有|不持有|不再持有|没拿|不拿|没再拿|早就卖|已经卖|卖掉了|卖光了|平掉了|早就平|清仓了)",
    "客户是": r"(?:不是[^，。,.]{0,10}客户|销户|销了|没[^，。,.]{0,6}户|不在[^，。,.]{0,10}开户)",
    # 隶属于（D210）：不是、不属于、不归……旗下、下面、下属、子公司、分公司、部门，或者不归……管。只说“不是 X 的”不认：
    # “不是 X 的客户”也会对上
    "隶属于": r"(?:(?:不是|不属于|不再属于|不归|不再归)[^，。,.；;]{0,10}(?:旗下|下面|下属|子公司|分公司|部门)"
              r"|不再?归[^，。,.；;]{0,10}管)",
}
_NEGATED_COMPILED = {kind: re.compile(pattern, re.IGNORECASE) for kind, pattern in _NEGATED.items()}


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def cue_groups(text: str) -> tuple[str, ...]:
    """这句话碰上了哪几组纠正词（不管是不是问句）。"""
    cleaned = _clean(text)
    return tuple(name for name, pattern in _COMPILED.items() if pattern.search(cleaned))


def is_question(text: str) -> bool:
    return bool(_QUESTION.search(_clean(text)))


def is_cue(text: str) -> bool:
    """这句话是在纠正：有纠正词，又不是问句、拿不准的。"""
    return bool(cue_groups(text)) and not is_question(text)


_ASK_WORDS = re.compile(r"其实")
# “其实她也在……”“还在……兼职”：说的是旧的那几条也还在。要和“在、是、做”连着说（“兼程物流”“又一村”这种名字、
# “还升了主管”这种说别的事的不算，K12 复审第 6 条）
_KEEPS_OTHERS = re.compile(r"也在|也是|也做|也当|还在|还是在|还做|还兼|又在|又做|同时在|同时也|同时做|兼职|兼任|兼着|兼做"
                           r"|另外在|另外还|另外也")


def keeps_others(text: str) -> bool:
    """这句话说的是新的那一条之外、旧的也还在（“也在”“还在”“兼职”“另外在”“同时在”“又在”这些）：只说了新的那一条时不问
    旧的（K12 审阅）。"""
    return bool(_KEEPS_OTHERS.search(_clean(text)))


def asks_about_others(text: str) -> bool:
    """只说了新的那一条时要不要问旧的（主人 11-17 复核 K12）：这句话有纠正词，或者带着“其实”（“其实他在鸥汀酒店做销售”），
    又不是问句、没有犹豫词、没说旧的也还在（keeps_others）。“其实”只在这里算：它不让这一处变成纠正（日子、财务投资挡住这些
    照旧）。"""
    cleaned = _clean(text)
    return ((is_cue(cleaned) or bool(_ASK_WORDS.search(cleaned))) and not is_question(cleaned)
            and not soft_words.hedges(cleaned) and not keeps_others(cleaned))


def _firm_cue(text: str) -> bool:
    """说法自己那一句以外的纠正词（同一条消息里的前一句、紧挨着的上一条）：还不能带犹豫词（“可能不对”是拿不准，
    不替主人改；说法自己那一句的犹豫词由软变化那边认，软变化优先）。"""
    return is_cue(text) and not soft_words.hedges(text)


def negated_types(text: str) -> frozenset[str]:
    """这句话否定了哪几种关系（“不负责”“不在……了”“早就卖了”）。"""
    cleaned = _clean(text)
    return frozenset(kind for kind, pattern in _NEGATED_COMPILED.items() if pattern.search(cleaned))


def strict_partitions(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """哪些分区算严格（财务、投资）：JAVMEM_STRICT_PARTITIONS，逗号隔开；没写的是 finance、investment。"""
    value = (os.environ if env is None else env).get(ENV_STRICT, "")
    parts = frozenset(part.strip() for part in value.replace("，", ",").split(",") if part.strip())
    return parts or DEFAULT_STRICT


# ---------- 原文索引：一句话、一行（一条消息） ----------

MessageKey = tuple[str, int, int, int]  # (包号, 版本, 第几个原话小节, 第几行)：一行就是一条消息


def _sentence(conn: sqlite3.Connection, sentence_id: str, revision: object) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.*, p.channel FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
        " WHERE s.sentence_id = ? AND s.revision = ?",
        (sentence_id, revision),
    ).fetchone()


def _fresh(row: sqlite3.Row) -> bool:
    """原话小节里新写的一句（不是引用、没撤回、不是信头、不是附件）。"""
    return (row["section_kind"] == "raw" and row["field"] is None and not row["quoted"] and row["withdrawn_at"] is None
            and row["origin"] == "said")


def is_fresh(row: sqlite3.Row) -> bool:
    """原文索引里的这一句是原话小节里新写的（D83 认问话、回答也只认这种）。"""
    return _fresh(row)


def owner_said(row: sqlite3.Row) -> bool:
    """主人自己说的：对话包里新写的原话，说话人是主人（转发、外部内容、引用、邮件都不算）。"""
    return _fresh(row) and row["speaker_role"] == "owner" and row["channel"] == "chat"


def message_key(row: sqlite3.Row) -> MessageKey | None:
    if row["section_no"] is None or row["line_no"] is None:
        return None
    return str(row["packet_id"]), int(row["revision"]), int(row["section_no"]), int(row["line_no"])


def message_rows(conn: sqlite3.Connection, key: MessageKey) -> list[sqlite3.Row]:
    """一条消息的几句，按先后。"""
    packet_id, revision, section, line = key
    return conn.execute(
        "SELECT s.*, p.channel FROM raw_sentences AS s JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
        " WHERE s.packet_id = ? AND s.revision = ? AND s.section_kind = 'raw' AND s.section_no = ? AND s.line_no = ?"
        " ORDER BY s.sentence_no, s.seq",
        (packet_id, revision, section, line),
    ).fetchall()


def correction_sentence(conn: sqlite3.Connection, proofs: Sequence[Mapping[str, object]]) -> str | None:
    """这一处说法是主人在纠正的话，返回带纠正词的那一句的编号；不是的返回 None（见开头）。proofs 是说法里存的证据。"""
    for proof in sorted(proofs, key=lambda one: (int(one.get("said_epoch") or 0), str(one.get("sentence_id")))):  # type: ignore[call-overload]
        row = _sentence(conn, str(proof["sentence_id"]), proof["revision"])
        if row is None or not owner_said(row):
            continue
        if is_cue(str(row["text"])):
            return str(row["sentence_id"])
        key = message_key(row)
        if key is None:
            continue
        mine = message_rows(conn, key)
        for earlier in reversed([one for one in mine if (one["sentence_no"] or 0) < (row["sentence_no"] or 0)]):
            if owner_said(earlier) and _firm_cue(str(earlier["text"])):
                return str(earlier["sentence_id"])
        if key[3] <= 1:
            continue
        before = message_rows(conn, (key[0], key[1], key[2], key[3] - 1))
        live = [one for one in before if one["withdrawn_at"] is None]
        if not live or not all(owner_said(one) for one in live):
            continue
        joined = "".join(str(one["text"]) for one in live)
        gap = int(row["said_at_epoch"]) - int(live[-1]["said_at_epoch"])
        if len(_PUNCT.sub("", _clean(joined))) <= SHORT_CHARS and 0 <= gap <= SHORT_SECONDS:
            cue = next((one for one in live if _firm_cue(str(one["text"]))), None)
            if cue is not None:
                return str(cue["sentence_id"])
    return None


def bot_message(conn: sqlite3.Connection, proofs: Sequence[Mapping[str, object]]) -> MessageKey | None:
    """证据全在 Bot 自己说的一条消息里（新写的原话，不是转发、引用）：那一条消息；不是的返回 None。"""
    keys: set[MessageKey] = set()
    for proof in proofs:
        row = _sentence(conn, str(proof["sentence_id"]), proof["revision"])
        if row is None or not _fresh(row) or row["speaker_role"] != "bot":
            return None
        key = message_key(row)
        if key is None:
            return None
        keys.add(key)
    return next(iter(keys)) if len(keys) == 1 else None


def message_of(conn: sqlite3.Connection, sentence_id: str, proofs: Sequence[Mapping[str, object]]) -> MessageKey | None:
    """这一句（带纠正词的那一句，可以不在证据里）所在的那条消息；版本照证据里同一个包的那几句。找不到的 None。"""
    revision = next((proof["revision"] for proof in proofs if str(proof["sentence_id"]).split("#")[0] == sentence_id.split("#")[0]),
                    None)
    row = _sentence(conn, sentence_id, revision) if revision is not None else None
    return message_key(row) if row is not None else None


def said_text(conn: sqlite3.Connection, proof: Mapping[str, object]) -> str | None:
    """证据里这一句的原文（没有的 None）。"""
    row = _sentence(conn, str(proof["sentence_id"]), proof["revision"])
    return str(row["text"]) if row is not None else None


def owner_messages(conn: sqlite3.Connection, proofs: Sequence[Mapping[str, object]], correction: str | None) -> set[MessageKey]:
    """这一处纠正落在主人的哪几条消息里：证据里主人说的那几句、带纠正词的那一句所在的消息。"""
    found: set[MessageKey] = set()
    wanted: list[tuple[str, object]] = [(str(proof["sentence_id"]), proof["revision"]) for proof in proofs]
    if correction is not None:
        revision = next((proof["revision"] for proof in proofs if str(proof["sentence_id"]).split("#")[0] == correction.split("#")[0]),
                        None)
        if revision is not None:
            wanted.append((correction, revision))
    for sentence_id, revision in wanted:
        row = _sentence(conn, sentence_id, revision)
        if row is not None and owner_said(row) and (key := message_key(row)) is not None:
            found.add(key)
    return found


# ---------- 抽取没抽出来的纠正：句子里提到了谁 ----------


def fold(text: str) -> str:
    """比对写法：NFKC、不分大小写、去掉空白。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def mentioned(text: str, spellings: Iterable[str]) -> bool:
    """这几个写法里有一个原样出现在 text 里（两个字以上；全是英文数字的前后不能连着字母数字）。"""
    folded = fold(text)
    for spelling in spellings:
        key = fold(spelling)
        if len(key) < 2:
            continue
        start = folded.find(key)
        while start != -1:
            if not key.isascii():
                return True
            end = start + len(key)
            before = folded[start - 1] if start > 0 else ""
            after = folded[end] if end < len(folded) else ""
            if not (before.isascii() and before.isalnum()) and not (after.isascii() and after.isalnum()):
                return True
            start = folded.find(key, start + 1)
    return False
