"""javmem regress 的“调试题只检索”：证据命中率，照 J2.15 的算法（D187、D196 量的那一套原样搬进来）。

一道题有几条出处（“包号 ｜ 原话”）。原话对到账本里这个身份看得到的原句（`Sentences.resolve`：这个包看得到的原句按行拼起来、
去掉空白，找到含着原话的那一行，取和原话重叠的那几句）。三路各取一份候选：

- `graph`：`memory_search`，主图开着（JAVMEM_GRAPH_SEARCH=on），每页 50 条，翻到 6,000 字或者没有下一页（最多 10 页）；
- `ledger`：同上，主图关着，只用账本视图；
- `raw`：只回退原文层（`tools._raw_hits`，前 400 句）。

每一路的候选按先后放进上下文（`fill`：5,000 字、1,500 字两档；原句太长截 160 字一段，已经作为某条放进去的事实的证据的原句
不再重复放），出处有一句进了上下文就算这条出处命中。“记忆里没有”的题不算。只打题号和数，不打问话、原话。

这个模块在 `javmem regress` 起的子进程里跑（`python -m javmem.regress.evidence`）：环境变量是子进程自己的（数据目录、
配置、身份、主图开不开），和跑它的那个进程不相通。问话的向量：先查给的缓存（J2.15 的 calls.db 那个样子：
`vectors(key, vector)`，键是 sha256(模型名 + 换行 + 发过去的那段字)），没有的再问本机的向量服务（等得久一点，不照检索时的
2 秒：慢了就扔掉一路，结果就和基准对不上了）；两样都拿不到的记下来，这一遍算跑不全。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

BUDGETS = (5000, 1500)
PAGE_LIMIT = 50
PAGE_CHARS = 6000
MAX_PAGES = 10
RAW_LIMIT = 400
RAW_MAX_CHARS = 160
ROUTES = ("graph", "ledger", "raw")

_WS = re.compile(r"\s+")
# 出处原话前面可能带着行首的“- [时间] 说话人：”，对原句时去掉（J2.15 的 _LINE_PREFIX）
_LINE_PREFIX = re.compile(r"^-?\s*\[[^\]]{4,25}\]\s*[^：:\s]{1,30}[：:]\s*")
MIN_QUOTE_CHARS = 4
_SOURCE_SPLIT = re.compile(r"\s*(?:｜| \| )\s*")


def _no_ws(text: str) -> str:
    return _WS.sub("", text)


def _norm_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class Item:
    """放进上下文的一条：事实、关系（kind 记成 fact），或者一句原文（raw）；sentence_ids 是它的证据句。"""

    key: str
    kind: str
    text: str
    sentence_ids: tuple[str, ...]


def fill(items: Sequence[Item], budget: int) -> list[Item]:
    """按先后放进上下文，文字加起来不超过 budget 个字。最后一条放不下、剩的地方有 40 字以上，截一段放进去就停。"""
    out: list[Item] = []
    used = 0
    covered: set[str] = set()
    for item in items:
        if item.kind == "raw" and item.key in covered:
            continue
        room = budget - used
        if room <= 0:
            break
        text = item.text
        if len(text) > room:
            if room < 40:
                break
            out.append(replace(item, text=text[: room - 1] + "…"))
            break
        out.append(item)
        used += len(text)
        if item.kind == "fact":
            covered.update(item.sentence_ids)
    return out


def window(text: str, term: str) -> str:
    """原句太长时截一段：从最要紧的那个词前面一点开始，最多 160 个字。"""
    if len(text) <= RAW_MAX_CHARS:
        return text
    pos = text.lower().find(term) if term else -1
    start = max(0, pos - 50) if pos >= 0 else 0
    piece = text[start : start + RAW_MAX_CHARS - 2]
    return ("…" if start > 0 else "") + piece + ("…" if start + len(piece) < len(text) else "")


def hits_at(usable: Sequence[set[str]], items: Sequence[Item]) -> dict[str, int]:
    out = {}
    for budget in BUDGETS:
        inside: set[str] = set()
        for item in fill(items, budget):
            inside.update(item.sentence_ids)
        out[str(budget)] = sum(1 for ids in usable if ids & inside)
    return out


class Sentences:
    """这个身份看得到的原句（照 query-api 3.2，和查询接口同一套），按包、按行放好，给出处对原句用。"""

    def __init__(self, conn: sqlite3.Connection, identity: object) -> None:
        from javmem.access import visible_packet

        conditions, params = visible_packet(identity, "p")  # type: ignore[arg-type]
        rows = conn.execute(
            "SELECT s.sentence_id, s.packet_id, s.text, s.section_kind, s.section_no, s.line_no, s.field"
            " FROM raw_sentences s JOIN packets p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            f" WHERE {' AND '.join(conditions)} AND s.withdrawn_at IS NULL ORDER BY s.packet_id, s.seq, s.id",
            params,
        ).fetchall()
        self.by_packet: dict[str, dict[tuple[object, ...], list[tuple[str, str]]]] = {}
        for sentence_id, packet_id, text, kind, number, line_no, field in rows:
            key = (kind, number, line_no if field is None else field)
            self.by_packet.setdefault(packet_id, {}).setdefault(key, []).append((sentence_id, text))

    def resolve(self, source: str) -> list[str]:
        """一条出处 → 对上的原句编号（对不上的是空的）。"""
        parts = _SOURCE_SPLIT.split(source.strip(), maxsplit=1)
        if len(parts) != 2:
            return []
        packet_id = parts[0].strip()
        quote = _no_ws(_LINE_PREFIX.sub("", _norm_ws(parts[1])))
        if len(quote) < MIN_QUOTE_CHARS or packet_id not in self.by_packet:
            return []
        hits: list[str] = []
        for group in self.by_packet[packet_id].values():  # 一行的几句拼起来找原话，取和它重叠的句子；一行里出现几次都算
            joined = "".join(_no_ws(text) for _, text in group)
            start = joined.find(quote)
            while start >= 0:
                end, offset = start + len(quote), 0
                for sentence_id, text in group:
                    length = len(_no_ws(text))
                    if offset < end and offset + length > start and sentence_id not in hits:
                        hits.append(sentence_id)
                    offset += length
                start = joined.find(quote, start + 1)
        return hits


def evidence_of(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, ...]]:
    """账本里每条事实、关系的证据句编号。"""
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for fact_id, sentence_id in conn.execute("SELECT fact_id, sentence_id FROM evidence WHERE fact_id IS NOT NULL"):
        out[("fact", fact_id)] = out.get(("fact", fact_id), ()) + (sentence_id,)
    for relation_id, sentence_id in conn.execute("SELECT relation_id, sentence_id FROM relation_evidence"):
        out[("relation", relation_id)] = out.get(("relation", relation_id), ()) + (sentence_id,)
    return out


# ---------- 问话的向量 ----------


class CachedQueries:
    """问话的向量：先查缓存（只读），没有的问本机的向量服务（给了才问）。名字要和图里关系的向量是同一个模型。"""

    def __init__(self, name: str, cache: Path | None, service: object | None) -> None:
        self.name = name
        self.service = service
        self.conn = None
        if cache is not None:  # 旁边有 -wal 的照只读打开（读得到还没并进主文件的），没有的用 immutable（不在旁边留 -shm）
            wal = cache.with_name(cache.name + "-wal")
            mode = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
            self.conn = sqlite3.connect(f"{cache.resolve().as_uri()}?{mode}", uri=True, check_same_thread=False)
        self.from_cache = 0
        self.from_service = 0
        self.missing = 0

    def _cached(self, text: str) -> bytes | None:
        if self.conn is None:
            return None
        key = hashlib.sha256((self.name + "\n" + text).encode("utf-8")).hexdigest()
        row = self.conn.execute("SELECT vector FROM vectors WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def embed(self, texts: Sequence[str]):  # noqa: ANN201 - javmem.embedding.Embedder 的样子
        from javmem.embedding import EmbeddingError, EmbeddingReply

        found = [self._cached(text) for text in texts]
        if all(blob is not None for blob in found):
            self.from_cache += len(texts)
            return EmbeddingReply(found, 0, 0.0, estimated=True)  # type: ignore[arg-type]
        if self.service is None:
            self.missing += len(texts)
            raise EmbeddingError("transport", "问话的向量不在缓存里，也没配向量服务")
        try:
            reply = self.service.embed(list(texts))  # type: ignore[attr-defined]
        except EmbeddingError:
            self.missing += len(texts)
            raise
        self.from_service += len(texts)
        return reply

    def describe(self) -> dict[str, int]:
        return {"from_cache": self.from_cache, "from_service": self.from_service, "missing": self.missing}


# ---------- 量 ----------


def _to_items(found: Iterable[Mapping[str, object]], evidence: Mapping[tuple[str, str], tuple[str, ...]]) -> list[Item]:
    items = []
    for item in found:
        kind, key = str(item["kind"]), str(item["id"])
        if kind == "raw":
            items.append(Item(key, "raw", window(str(item.get("content") or ""), ""), (key,)))
        else:
            items.append(Item(key, "fact", str(item.get("content") or ""), evidence.get((kind, key), ())))
    return items


def _pages(question: str, settings: object, graph: bool) -> tuple[list[Mapping[str, object]], bool]:
    """(候选, 主图那一路有没有退回账本视图)。graph 是 True 的，哪一页回的 degraded 里有 graph（图连不上、超时、
    出错以后歇着、问话的向量对不上……），这一道题的主图那一路就不算数。"""
    from javmem.query import tools

    os.environ["JAVMEM_GRAPH_SEARCH"] = "on" if graph else "off"
    items: list[Mapping[str, object]] = []
    cursor: object = "c0"
    used = pages = 0
    degraded = False
    while cursor and used < PAGE_CHARS and pages < MAX_PAGES:
        out = tools.memory_search({"query": question, "limit": PAGE_LIMIT, "cursor": cursor}, settings=settings)  # type: ignore[arg-type]
        degraded = degraded or (graph and "graph" in (out.get("degraded") or []))
        for item in out.get("items", []):
            items.append(item)
            used += len(str(item.get("content", "")))
        cursor = out.get("next_cursor")
        pages += 1
    return items, degraded


def real_sources(question: Mapping[str, object]) -> list[str]:
    """一道题算数的出处：空的、写着“无”的不算（照 D196 的 sources_of）。"""
    return [str(one) for one in question.get("sources") or [] if str(one).strip() and str(one).strip() != "无"]  # type: ignore[union-attr]


def _raw(question: str, settings: object) -> list[Item]:
    from javmem.query import tools

    def answer(view, request, out):  # noqa: ANN001, ANN202 - tools.run_tool 的样子
        words = tools.query_words(request.query, tools._dictionary(view), slot=view.identity.name)  # 和 _answer_search 一样
        found, more = tools._raw_hits(view, words, replace(request, limit=RAW_LIMIT, offset=0))
        return {"items": found, "words": words, "more": more}

    out = tools.run_tool("memory_search", {"query": question}, settings, tools._parse_search, answer)  # type: ignore[arg-type]
    words = [word.lower() for word in out.get("words", [])]
    items = []
    for item in out.get("items", []):
        content = str(item["content"])
        term = next((word for word in words if word in content.lower()), "")
        items.append(Item(str(item["id"]), "raw", window(content, term), (str(item["id"]),)))
    return items


def measure(questions: Sequence[Mapping[str, object]], *, graph: bool, queries: CachedQueries | None = None,
            log=print) -> dict[str, object]:  # noqa: ANN001
    """40 道题（或者别的几道）三路各量一遍。graph 是 False 的不量主图那一路。"""
    from javmem.access import load_identity
    from javmem.config import Settings
    from javmem.ledger import connect_readonly

    settings = Settings.from_env()
    if queries is not None:
        import javmem.graph.search as search

        search.query_embedder = lambda env=None, seconds=2.0: queries  # noqa: ARG005 - 检索时只拿这一个
    identity = load_identity(settings)
    conn = connect_readonly(settings.library(identity.env).db_path)
    try:
        sentences = Sentences(conn, identity)
        evidence = evidence_of(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    routes = ROUTES if graph else ROUTES[1:]
    vectors_on: bool | None = None
    if graph:  # 主图的向量那一路开没开：问话的向量和图里的不是同一个模型、没有问话的向量，检索器悄悄关掉这一路
        import javmem.graph.search as search

        searcher = search.open_graph_searcher(settings, identity.env)
        vectors_on = searcher is not None and searcher.vectors.embedder is not None
    started = time.perf_counter()
    graph_items: dict[str, list[Item]] = {}
    degraded: list[str] = []
    if graph:  # 照 D196：先把每道题主图那一路都问一遍，再挨个问另外两路
        for question in questions:
            found, fell_back = _pages(str(question["query"]), settings, True)
            graph_items[str(question["id"])] = _to_items(found, evidence)
            if fell_back:
                degraded.append(str(question["id"]))
    totals = {route: {str(budget): 0 for budget in BUDGETS} for route in routes}
    full = {route: 0 for route in routes}
    rows: dict[str, object] = {}
    sources = resolved = 0
    for question in questions:
        qid = str(question["id"])
        if question.get("absent"):
            continue
        counted = real_sources(question)
        usable = [set(ids) for ids in (sentences.resolve(one) for one in counted) if ids]
        sources += len(counted)
        resolved += len(usable)
        by_route = {"raw": _raw(str(question["query"]), settings),
                    "ledger": _to_items(_pages(str(question["query"]), settings, False)[0], evidence)}
        if graph:
            by_route["graph"] = graph_items[qid]
        row: dict[str, object] = {"of": len(usable)}
        for route in routes:
            got = hits_at(usable, by_route[route])
            row[route] = got
            for budget in got:
                totals[route][budget] += got[budget]
            full[route] += bool(usable) and got[str(BUDGETS[0])] == len(usable)
        rows[qid] = row
        log(f"{qid} {json.dumps(row, ensure_ascii=False)}")
    if graph:
        import javmem.graph.search as search

        search.close_searchers()
    return {"routes": list(routes), "totals": totals, "full_at_5000": full, "sources": sources, "resolved": resolved,
            "questions": len(questions), "graph_degraded": len(degraded), "graph_degraded_ids": degraded,
            "graph_vectors": vectors_on, "rows": rows,
            "ledger_version": version,
            "queries": queries.describe() if queries is not None else None,
            "seconds": round(time.perf_counter() - started, 1)}


def main(argv: Sequence[str] | None = None) -> int:
    """子进程的入口：python -m javmem.regress.evidence --questions 文件 --out 文件 [--graph] [--query-vectors 缓存]。"""
    parser = argparse.ArgumentParser(prog="python -m javmem.regress.evidence")
    parser.add_argument("--questions", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--graph", action="store_true")
    parser.add_argument("--query-vectors")
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--fake-embeddings", action="store_true", help="问话也用假向量（账本、图里是假向量的套件）")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from javmem.config import ConfigError
    from javmem.embedding import EmbeddingSettings, FakeEmbeddings, OpenAIEmbeddings

    if args.fake_embeddings:
        service: object | None = FakeEmbeddings(name=args.embedding_model)
    else:
        try:
            service = OpenAIEmbeddings(EmbeddingSettings.from_env())
        except ConfigError:
            service = None
    queries = CachedQueries(args.embedding_model, Path(args.query_vectors) if args.query_vectors else None, service)
    raw = Path(args.questions).read_bytes()
    questions = json.loads(raw.decode("utf-8"))["questions"]
    found = measure(questions, graph=args.graph, queries=queries)
    found["questions_sha256"] = hashlib.sha256(raw).hexdigest()  # 题文件改了（加题、删题、改出处），命中数就没法和基准比
    Path(args.out).write_text(json.dumps(found, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("证据命中率（5,000 字 / 1,500 字）：" + "，".join(
        f"{route} {found['totals'][route]['5000']} / {found['totals'][route]['1500']}" for route in found["routes"]))  # type: ignore[index]
    return 0


if __name__ == "__main__":
    sys.exit(main())
