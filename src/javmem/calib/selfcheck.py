"""`javmem selfcheck`（U3 校准工具包）：不用金标准也能看的四个数。

还没有金标准、或者金标准只有十来个包的时候，这四个数看得出抽取稳不稳、证据实不实、合得对不对、认人认得齐不齐：

1. **两遍一样不一样**（`--runs 第一遍 第二遍`）：同一版提示词、同一个模型把同样的包抽两遍，各进一个结果库
   （第二遍：另建一个数据目录，同样的包收一遍、`javmem extract` 抽一遍；写完账的包不再排队，在原来的数据目录里
   抽不了第二遍）。两遍 L3 原样的结果（没过核对、合并）按出处句和主体配对：
   事实是主体一样、出处至少有一句一样的配成一对；关系是主体、关系、客体都一样、出处至少有一句一样的配成一对。
   一对一，内容一样的先配，出处重得多的先配。报两个比例，都是“配成对的条数 × 2 ÷ 两遍的条数之和”：
   - 对得上：配成了对的；
   - 内容也一样：事实的值一样（没有值的比文字里的数；文字里没有数的，字面要有六成一样），日子不冲突；关系的状态、
     具体的亲属关系一样。
   只比两遍都抽过的包。
2. **证据在原文里找得到的比例**：账本里 L3 抽出的事实、关系（过了核对写进去的说法，加上隔离表里没过的），核对那一步
   引文在原文里逐字找得到的占多少。没过的原因里有引文找不到、引文只在上文里、对不上唯一一处、引文是空的、编号认不出的，
   算找不到；别的规则没过的（数字、否定、类型……）证据是找到了的。另报两样：一条事实（关系）有几条证据、去掉了找不到
   的几条、别的留着的，去掉了几条证据；结构不对、没走到核证据的几条（不进分母）。
3. **合在一起的事实里对不上的**：几处说法合成的一条事实（同一个主体、属性、值），各处说法写的日子、编号、金额对不上的
   有几条。日子是说法的发生时间和文字里写明的日子（年月日、年月、月日；只写到月的和那个月的哪一天不冲突），编号是
   字母和数字混写的代码（四个字以上）、六位以上的长数字，金额是带着币种或者“元”的数。两处都写了、又一样都对不上，
   才算对不上：多写了一个、少写了一个不算。
4. **同一样东西几个名字**：认人认事的合并提议（和 `javmem entities` 同一套找法，不调模型）落在几个实体上，占实体的多少；
   另报几条“疑似撞名”（提议记成不是同一个的）。

只读：账本、结果库都只读，不调模型、不连外面。输出只有数和编号（事实、提议的编号），不带原文、不带名字：
要看是哪几条，`javmem audit --library 库 --item 事实编号`、`javmem entities --library 库 --show 提议编号`。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from javmem.config import Settings

EXIT_OK, EXIT_INPUT = 0, 2
FORMAT = "javmem-selfcheck/1"
LIST_CAP = 50  # 编号最多列这么多个
CLOSE_TEXT = 0.6  # 没有值、文字里也没有数的两条事实，字面（两个字一段）要有这么多一样，才算内容一样
Out = Callable[[str], None]
T = TypeVar("T")


class SelfcheckInputError(ValueError):
    """给的东西用不了。消息里只有路径、库名，不带内容。"""


def add_selfcheck_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--library", choices=("prod", "test"), default="test", help="看哪个库的账本，默认 test")
    parser.add_argument("--runs", nargs=2, metavar=("第一遍", "第二遍"),
                        help="比两遍抽取：两份 L3 结果库（数据目录、l3 目录或者 l3.db），同一版提示词、同一个模型各抽一遍")
    parser.add_argument("--json", help="把四个数和编号写成 JSON（只有数和编号，没有原文）")


# ---------- 小工具 ----------


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _pct(rate: object) -> str:
    return f"{float(rate) * 100:.1f}%" if isinstance(rate, (int, float)) else "—"


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(path: Path) -> set[str]:
    try:
        conn = _open(path)
    except sqlite3.Error:
        return set()
    try:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError:
        return set()
    finally:
        conn.close()


_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_PUNCT = re.compile(r"[\s\W_]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _nfkc(text: object) -> str:
    return _THOUSANDS.sub("", unicodedata.normalize("NFKC", str(text or "")))


def _fold(text: object) -> str:
    return _PUNCT.sub("", _nfkc(text).lower())


def _numbers(text: object) -> Counter[str]:
    return Counter(_NUMBER.findall(_nfkc(text)))


def _grams(text: object) -> set[str]:
    folded = _fold(text)
    return {folded[i : i + 2] for i in range(len(folded) - 1)} or ({folded} if folded else set())


def _close(first: object, second: object) -> bool:
    a, b = _grams(first), _grams(second)
    return bool(a and b) and 2 * len(a & b) / (len(a) + len(b)) >= CLOSE_TEXT


# ---------- 1. 两遍一样不一样 ----------


def store_file(path: Path) -> Path:
    """数据目录、l3 目录或者 l3.db → l3.db。不是结果库的报 SelfcheckInputError。"""
    candidates = [path / "l3" / "l3.db", path / "l3.db"] if path.is_dir() else [path]
    for candidate in candidates:
        if candidate.is_file() and {"windows", "results"} <= _tables(candidate):
            return candidate
    raise SelfcheckInputError(f"{path} 里找不到 L3 的结果库（l3.db）")


def store_packets(store: Path) -> set[str]:
    """结果库里的窗口要抽的句子落在哪些包。"""
    conn = _open(store)
    try:
        rows = conn.execute("SELECT manifest FROM windows").fetchall()
    finally:
        conn.close()
    found: set[str] = set()
    for row in rows:
        try:
            manifest = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            continue
        for line in manifest if isinstance(manifest, list) else []:
            if isinstance(line, dict) and str(line.get("ref", "")).startswith("T") and line.get("packet_id"):
                found.add(str(line["packet_id"]))
    return found


@dataclass(frozen=True)
class Pairing:
    first: int
    second: int
    matched: int
    same: int

    def as_dict(self) -> dict[str, object]:
        whole = self.first + self.second
        return {"first": self.first, "second": self.second, "matched": self.matched, "same": self.same,
                "matched_rate": _rate(2 * self.matched, whole), "same_rate": _rate(2 * self.same, whole)}


def pair_up(first: Sequence[T], second: Sequence[T], key: Callable[[T], object], evidence: Callable[[T], Collection[str]],
            same: Callable[[T, T], bool]) -> Pairing:
    """一对一配对：key 一样、出处至少有一句一样的才配；内容一样的先配，出处重得多的先配。"""
    index: dict[tuple[object, str], list[int]] = {}
    for j, item in enumerate(second):
        for sid in evidence(item):
            index.setdefault((key(item), sid), []).append(j)
    candidates: list[tuple[bool, int, int, int, bool]] = []
    for i, item in enumerate(first):
        mine = set(evidence(item))
        seen: set[int] = set()
        for sid in mine:
            for j in index.get((key(item), sid), []):
                if j in seen:
                    continue
                seen.add(j)
                alike = same(item, second[j])
                candidates.append((not alike, -len(mine & set(evidence(second[j]))), i, j, alike))
    candidates.sort()
    used_first: set[int] = set()
    used_second: set[int] = set()
    matched = alike_count = 0
    for _, _, i, j, alike in candidates:
        if i in used_first or j in used_second:
            continue
        used_first.add(i)
        used_second.add(j)
        matched += 1
        alike_count += alike
    return Pairing(len(first), len(second), matched, alike_count)


def _day_parts(value: object) -> tuple[int | None, int | None, int | None] | None:
    """ISO 的日子（2026-10-12、2026-10、2026、2026-Q4、带时刻的）→ (年, 月, 日)；季度只留年。认不出的 None。"""
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", text)
    if not match:
        return None
    if re.match(r"^\d{4}-Q", text):
        return (int(match.group(1)), None, None)
    return (int(match.group(1)), int(match.group(2)) if match.group(2) else None, int(match.group(3)) if match.group(3) else None)


def _days_agree(first: object, second: object) -> bool:
    a, b = _day_parts(first), _day_parts(second)
    if a is None or b is None:
        return True
    return all(x is None or y is None or x == y for x, y in zip(a, b, strict=True))


def _same_fact(a: object, b: object) -> bool:
    if not _days_agree(a.when.start, b.when.start):  # type: ignore[attr-defined]
        return False
    if a.value and b.value:  # type: ignore[attr-defined]
        return _fold(a.value) == _fold(b.value)  # type: ignore[attr-defined]
    numbers = _numbers(a.value or a.text)  # type: ignore[attr-defined]
    if numbers != _numbers(b.value or b.text):  # type: ignore[attr-defined]
        return False
    return bool(numbers) or _close(a.text, b.text)  # type: ignore[attr-defined]


def _same_relation(a: object, b: object) -> bool:
    return a.state == b.state and dict(a.fields).get("kinship") == dict(b.fields).get("kinship")  # type: ignore[attr-defined]


def compare_runs(first: Path, second: Path) -> dict[str, object]:
    """两份结果库里 L3 原样的事实、关系配对。"""
    from javmem.pipeline.merge import name_key
    from javmem.scoring.common import ScoreInputError
    from javmem.scoring.gold import split_sid
    from javmem.scoring.results import side_from_result

    stores = [store_file(first), store_file(second)]
    sides = []
    for number, store in enumerate(stores, 1):
        try:
            side, _ = side_from_result(store, store_packets(store), name=f"第{number}遍")
        except ScoreInputError as error:
            raise SelfcheckInputError(f"第{number}遍的结果库读不了：{error}") from error
        sides.append(side)
    common = sides[0].packets & sides[1].packets

    def inside(evidence: Iterable[str]) -> bool:
        sids = list(evidence)
        return bool(sids) and all(split_sid(sid)[0] in common for sid in sids)

    def facts(side: object) -> list[object]:
        return [fact for fact in side.facts.values() if inside(fact.evidence)]  # type: ignore[attr-defined]

    def relations(side: object) -> list[object]:
        return [one for one in side.relations.values() if inside(one.evidence)]  # type: ignore[attr-defined]

    def named(side: object, entity_id: object) -> str:
        entity = side.entities.get(str(entity_id or ""))  # type: ignore[attr-defined]
        return name_key(entity.name) if entity is not None else ""

    first_side, second_side = sides
    fact_pairs = pair_up(
        [(first_side, one) for one in facts(first_side)], [(second_side, one) for one in facts(second_side)],
        key=lambda pair: named(pair[0], pair[1].subject), evidence=lambda pair: pair[1].evidence,
        same=lambda x, y: _same_fact(x[1], y[1]))
    relation_pairs = pair_up(
        [(first_side, one) for one in relations(first_side)], [(second_side, one) for one in relations(second_side)],
        key=lambda pair: (named(pair[0], pair[1].subject), pair[1].relation, named(pair[0], pair[1].object)),
        evidence=lambda pair: pair[1].evidence, same=lambda x, y: _same_relation(x[1], y[1]))
    return {"packets": len(common), "facts": fact_pairs.as_dict(), "relations": relation_pairs.as_dict()}


# ---------- 2. 证据在原文里找得到 ----------

# 核对那一步“引文找不到”的几种原因（事实的 G1、关系的 R1，加上 L3 换编号时认不出、只引了上文、没有能用的证据）
NOT_FOUND = frozenset({"quote_not_found", "quote_in_context", "quote_ambiguous", "empty_quote", "unknown_ref", "context_ref",
                       "no_usable_evidence"})
_KINDS = (("facts", "fact", "evidence", "G1"), ("relations", "relation", "relation_evidence", "R1"))


def _live(alias: str = "q") -> str:
    """隔离表只算现在的包：当前版本、没撤回。"""
    return (f"EXISTS (SELECT 1 FROM packets p WHERE p.packet_id = {alias}.packet_id AND p.revision = {alias}.revision"
            " AND p.state = 'current' AND p.withdrawn_at IS NULL)")


def evidence_found(conn: sqlite3.Connection) -> dict[str, object]:
    passed = {
        "facts": int(conn.execute("SELECT COUNT(*) FROM fact_claims c JOIN facts f ON f.fact_id = c.fact_id"
                                  " WHERE f.withdrawn_at IS NULL").fetchone()[0]),
        "relations": int(conn.execute("SELECT COUNT(*) FROM relation_claims c JOIN relations r ON r.relation_id = c.relation_id"
                                      " WHERE r.withdrawn_at IS NULL").fetchone()[0]),
    }
    out: dict[str, object] = {}
    for name, kind, dropped_kind, rule in _KINDS:
        not_found = unchecked = other = 0
        for row in conn.execute(f"SELECT reason FROM quarantine q WHERE item_kind = ? AND {_live()}", (kind,)):
            codes = [part.split(":", 1) for part in str(row[0] or "").split(",") if part.strip()]
            rules = {part[0].strip() for part in codes}
            if any(part[0].strip() == rule or (len(part) > 1 and part[1].strip() in NOT_FOUND) for part in codes):
                not_found += 1
            elif rules <= {"L3"}:
                unchecked += 1  # L3 换编号时结构就不对，没走到核证据
            else:
                other += 1  # 证据找到了，别的规则没过（或者写不进去）
        dropped = int(conn.execute(f"SELECT COUNT(*) FROM quarantine q WHERE item_kind = ? AND {_live()}", (dropped_kind,)).fetchone()[0])
        checked = passed[name] + not_found + other
        out[name] = {"checked": checked, "found": checked - not_found, "rate": _rate(checked - not_found, checked),
                     "dropped_evidence": dropped, "not_checked": unchecked}
    return out


# ---------- 3. 合在一起的事实 ----------

_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?(?!\d)")
_YEAR_MONTH = re.compile(r"(?<!\d)(\d{4})\s*[-/年]\s*(\d{1,2})(?:\s*月)?(?![\d./-])")
_MONTH_DAY = re.compile(r"(?<![\d年/.-])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_ISO_TIME = re.compile(r"\d{4}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?")
_CODE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
_LONG_NUMBER = re.compile(r"(?<![\d.])\d{6,}(?![\d.])")
_MONEY = "元|块钱|人民币|美元|港元|港币|欧元|英镑|日元|RMB|CNY|USD|HKD|EUR|GBP|JPY"  # 光一个“块”常是量词，不算
_AMOUNT_AFTER = re.compile(rf"(\d+(?:\.\d+)?)\s*(万|亿)?\s*({_MONEY})", re.IGNORECASE)
_AMOUNT_BEFORE = re.compile(r"([¥￥$€£]|RMB|CNY|USD|HKD|EUR|GBP|JPY)\s*(\d+(?:\.\d+)?)\s*(万|亿)?", re.IGNORECASE)
_CURRENCY = {"元": "CNY", "块钱": "CNY", "人民币": "CNY", "rmb": "CNY", "cny": "CNY", "¥": "CNY", "￥": "CNY",
             "美元": "USD", "usd": "USD", "$": "USD", "港元": "HKD", "港币": "HKD", "hkd": "HKD", "欧元": "EUR", "eur": "EUR",
             "€": "EUR", "英镑": "GBP", "gbp": "GBP", "£": "GBP", "日元": "JPY", "jpy": "JPY"}
_TIMES = {"万": 10_000, "亿": 100_000_000}


def claim_dates(content: object, occurred: object) -> set[tuple[int | None, int | None, int | None]]:
    found: set[tuple[int | None, int | None, int | None]] = set()
    parts = _day_parts(occurred)
    if parts is not None:
        found.add(parts)
    text = _ISO_TIME.sub(lambda match: match.group(0)[:10], _nfkc(content))
    for pattern, build in (
        (_FULL_DATE, lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        (_YEAR_MONTH, lambda m: (int(m.group(1)), int(m.group(2)), None)),
        (_MONTH_DAY, lambda m: (None, int(m.group(1)), int(m.group(2)))),
    ):
        for match in pattern.finditer(text):
            found.add(build(match))
        text = pattern.sub(" ", text)  # 认过的去掉，短的写法不再认一遍
    return {one for one in found if (one[1] is None or 1 <= one[1] <= 12) and (one[2] is None or 1 <= one[2] <= 31)}


def claim_codes(content: object) -> set[str]:
    text = _ISO_TIME.sub(" ", _nfkc(content))
    found = {match.group(0).upper().replace("-", "") for match in _CODE.finditer(text)
             if sum(char.isalnum() for char in match.group(0)) >= 4}
    found |= set(_LONG_NUMBER.findall(text))
    return found


def claim_amounts(content: object) -> set[str]:
    text = _nfkc(content)
    found: set[str] = set()
    for match in _AMOUNT_AFTER.finditer(text):
        value = float(match.group(1)) * _TIMES.get(match.group(2) or "", 1)
        found.add(f"{_CURRENCY.get(match.group(3).lower(), match.group(3).upper())}:{value:g}")
    for match in _AMOUNT_BEFORE.finditer(text):
        value = float(match.group(2)) * _TIMES.get(match.group(3) or "", 1)
        found.add(f"{_CURRENCY.get(match.group(1).lower(), match.group(1).upper())}:{value:g}")
    return found


def _dates_clash(first: set[tuple[int | None, ...]], second: set[tuple[int | None, ...]]) -> bool:
    if not first or not second:
        return False
    return not any(all(x is None or y is None or x == y for x, y in zip(a, b, strict=True)) for a in first for b in second)


def _sets_clash(first: set[str], second: set[str]) -> bool:
    return bool(first) and bool(second) and not first & second


def _any_pair(values: Sequence[T], clash: Callable[[T, T], bool]) -> bool:
    return any(clash(values[i], values[j]) for i in range(len(values)) for j in range(i + 1, len(values)))


def merged_facts(conn: sqlite3.Connection) -> dict[str, object]:
    rows = conn.execute(
        "SELECT c.fact_id, c.content, c.occurred_at FROM fact_claims c JOIN facts f ON f.fact_id = c.fact_id"
        " WHERE f.withdrawn_at IS NULL AND c.fact_id IN (SELECT fact_id FROM fact_claims GROUP BY fact_id HAVING COUNT(*) > 1)"
        " ORDER BY c.fact_id, c.claim_id").fetchall()
    claims: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        claims.setdefault(str(row["fact_id"]), []).append(row)
    ids: dict[str, list[str]] = {"dates": [], "codes": [], "amounts": []}
    flagged: set[str] = set()
    for fact_id, group in sorted(claims.items()):
        checks = {
            "dates": _any_pair([claim_dates(row["content"], row["occurred_at"]) for row in group], _dates_clash),
            "codes": _any_pair([claim_codes(row["content"]) for row in group], _sets_clash),
            "amounts": _any_pair([claim_amounts(row["content"]) for row in group], _sets_clash),
        }
        for name, clash in checks.items():
            if clash:
                ids[name].append(fact_id)
                flagged.add(fact_id)
    return {"merged": len(claims), "dates": len(ids["dates"]), "codes": len(ids["codes"]), "amounts": len(ids["amounts"]),
            "flagged": len(flagged), "rate": _rate(len(flagged), len(claims)),
            "ids": {name: values[:LIST_CAP] for name, values in ids.items()}}


# ---------- 4. 同一样东西几个名字 ----------


def several_names(conn: sqlite3.Connection, lexicon: object) -> dict[str, object]:
    from javmem.entities import APART, MERGE, SOURCES, find_proposals

    proposals = find_proposals(conn, lexicon, SOURCES)  # type: ignore[arg-type]
    merges = [one for one in proposals if one.kind == MERGE]
    clashes = [one for one in proposals if one.kind == APART]
    involved = {entity for one in merges for entity in one.entities}
    redirected = "entity_redirects" in {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    total = int(conn.execute(
        "SELECT COUNT(*) FROM entities" + (" WHERE entity_id NOT IN (SELECT old_id FROM entity_redirects)" if redirected else "")
    ).fetchone()[0])
    return {"entities": total, "in_proposals": len(involved), "rate": _rate(len(involved), total), "proposals": len(merges),
            "clashes": len(clashes), "ids": sorted(one.proposal_id for one in merges)[:LIST_CAP],
            "clash_ids": sorted(one.proposal_id for one in clashes)[:LIST_CAP]}


# ---------- 合起来 ----------


def selfcheck(settings: Settings, library: str, runs: Sequence[Path] | None) -> dict[str, object]:
    from javmem.zh.lexicon import load_lexicon

    report: dict[str, object] = {"format": FORMAT, "library": library, "runs": None, "evidence": None, "merged": None,
                                 "names": None}
    if runs:
        report["runs"] = compare_runs(Path(runs[0]), Path(runs[1]))
    path = settings.library(library).db_path if settings.data_dir is not None else None
    if path is not None and path.is_file():
        conn = _open(path)
        try:
            report["evidence"] = evidence_found(conn)
            report["merged"] = merged_facts(conn)
            report["names"] = several_names(conn, load_lexicon(settings.lexicon_file))
        finally:
            conn.close()
    elif not runs:
        raise SelfcheckInputError(f"{library} 库还没有账本（或者没设 JAVMEM_DATA_DIR），也没给 --runs：什么都算不了")
    return report


def lines(report: Mapping[str, object]) -> list[str]:
    library = report["library"]
    out = [f"不用金标准的四个数（{library} 库）"]
    runs = report.get("runs")
    if isinstance(runs, Mapping):
        facts, relations = runs["facts"], runs["relations"]
        out.append(
            f"1. 两遍一样不一样：两遍都抽过的包 {runs['packets']} 个。事实 {facts['first']} / {facts['second']} 条，"  # type: ignore[index]
            f"对得上 {facts['matched']} 对（{_pct(facts['matched_rate'])}），内容也一样 {facts['same']} 对（{_pct(facts['same_rate'])}）；"  # type: ignore[index]
            f"关系 {relations['first']} / {relations['second']} 条，对得上 {relations['matched']} 对（{_pct(relations['matched_rate'])}），"  # type: ignore[index]
            f"状态也一样 {relations['same']} 对（{_pct(relations['same_rate'])}）")  # type: ignore[index]
    else:
        out.append("1. 两遍一样不一样：没比。另建一个数据目录，同样的包收一遍、用同一版提示词和同一个模型抽一遍，"
                   "再用 --runs 给这两个数据目录")
    evidence = report.get("evidence")
    if isinstance(evidence, Mapping):
        facts, relations = evidence["facts"], evidence["relations"]
        out.append(
            f"2. 证据在原文里找得到：事实 {facts['found']} / {facts['checked']}（{_pct(facts['rate'])}），"  # type: ignore[index]
            f"关系 {relations['found']} / {relations['checked']}（{_pct(relations['rate'])}）；"  # type: ignore[index]
            f"另外去掉了找不到的证据 {facts['dropped_evidence'] + relations['dropped_evidence']} 条（那一条别的证据找到了，留着）；"  # type: ignore[index]
            f"结构不对、没核证据的 {facts['not_checked'] + relations['not_checked']} 条（不算在里面）")  # type: ignore[index]
    merged = report.get("merged")
    if isinstance(merged, Mapping):
        out.append(
            f"3. 合在一起的事实（几处说法合成一条）：{merged['merged']} 条，对不上的 {merged['flagged']} 条（{_pct(merged['rate'])}）："
            f"日子 {merged['dates']}、编号 {merged['codes']}、金额 {merged['amounts']}")
        for name, label in (("dates", "日子"), ("codes", "编号"), ("amounts", "金额")):
            ids = merged["ids"][name]  # type: ignore[index]
            if ids:
                out.append(f"   {label}对不上：{'、'.join(ids)}")
        if merged["flagged"]:
            out.append(f"   看依据：javmem audit --library {library} --item 事实编号")
    names = report.get("names")
    if isinstance(names, Mapping):
        out.append(
            f"4. 同一样东西几个名字：实体 {names['entities']} 个，落在合并提议里的 {names['in_proposals']} 个（{_pct(names['rate'])}，"
            f"提议 {names['proposals']} 条）；疑似撞名 {names['clashes']} 条")
        if names["ids"] or names["clash_ids"]:
            out.append(f"   提议：{'、'.join([*names['ids'], *names['clash_ids']])}"  # type: ignore[misc]
                       f"（看一条：javmem entities --library {library} --show 提议编号）")
    if not isinstance(evidence, Mapping):
        out.append(f"2. 到 4. 没算：{library} 库还没有账本（或者没设 JAVMEM_DATA_DIR）")
    return out


def run_selfcheck(args: argparse.Namespace, settings: Settings, out: Out = print) -> int:
    try:
        report = selfcheck(settings, args.library, args.runs)
    except SelfcheckInputError as error:
        out(str(error))
        return EXIT_INPUT
    for line in lines(report):
        out(line)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((json.dumps(report, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return EXIT_OK
