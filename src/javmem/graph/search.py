"""主图检索（J5.4，版本 gs2）：memory_search 的主图那一步（子会话 J5.4-做法.md，主会话 11-18 放行）。

gs2（D196、D201）：排名的参数和 gs1 一样，只换了时限：gs1 每一路 1.5 秒、超了扔掉这一路（route_seconds），gs2 整个这一步
30 秒、超了这次不用主图（search_seconds）。参数里那一项真换了，所以换了版本号。

口子在 javmem.query.graphhook：服务每次 memory_search 叫一次 search(GraphRequest)，这里分四路找候选，按名次合起来，回
编号和分数（Hit）；服务拿编号回账本取内容、按身份再核一遍（图慢一拍时对不上的丢掉）。entity_card、timeline、
relation_at 只查账本视图，不经过这里。

**四路**（每路最多 80 条；过滤都写在查询里面，滤完再截：看不到的东西不占名次、不影响条数，query-api 3.2）：

1. 实体：问话里认出来的实体（账本视图认的，只认这个身份看得到的叫法）是主体、对象、提及的事（主体的排前），一头是它、
   或者“代表哪一家”是它的关系；顺着看得到的、时间段和现行允许的关系到旁边的实体（最多 10 个，最近说起的先），拿它们
   当主体的事和它们的关系；再往外一层只拿关系（最多 20 条）。往外扩绕开枢纽（主人、Bot：一头是主人的关系占了一半，
   顺着主人扩一跳就到了主人所有的东西）；枢纽自己是认出来的实体时，只拿它直接连着的、和问话最像的，不往外扩。每个
   实体最多 10 条事、10 条关系。排：和问话的余弦，一样像的跳数少的在前，再新的在前；没有问话的向量时照跳数、新旧。
2. 全文（cjk 两字一组）：事的内容，关系的内容和另带的字段。范围编号写进 Lucene 查询里，在索引里面就滤掉；时间、现行
   写在后面的条件里。名次不用 Lucene 的分：它的词频统计里算着看不到的内容（“排序只在看得到的内容上算”），改用对上
   几个词、新旧。
3. 向量：事、关系两个向量索引。5.26 的向量检索不能先滤再取：先取 200 条，滤完不够 80 条又还没取完就取 4 倍再来，最多
   10,000 条（还不够的记一行日志、数进 stats）；余弦低于 θ 的不要，问记忆里没有的事时这一路是空的，不硬凑。
4. 账本：账本视图自己找到的（ledger_hits，里面有图里没有的待确认、图慢一拍还没进图的），照它的名次。

**合：** RRF，每一路第 r 名得 1 / (60 + r)，几路都有的加起来；一样的最近有人说起的在前（as_of_epoch），再按编号，每次
一样。截到 want 条。四路都空就回空，服务接着查原文层。

**现在有效、时间段**和账本视图一样（javmem.query.tools 的 _fact_ok、_relation_ok，view.relation_state）：

- 事实：没被替代，或者替代它的那条这个身份看不到（状态只认看得到的内容）。
- 关系：不是已结束、没被看得到的那条替代、结束日子（按精度的整段）没过 as_of；暂停的、开始日子在将来的照样算。
- include_history 打开的，这两条都不管。
- 时间段和 time_from、time_to 有交叠：事实看发生时间（照精度换成的那一段，不知道的按最早一句证据）；关系看生效的那一段
  （开始不知道的从最早一句证据算，结束写到天或者更细的那一天不算）。结束了又不知道哪天结束的（只在 include_history 时
  有），这里当一直到现在：比账本视图宽，服务回账本核的时候照账本的算。

**枢纽：** 主人（账本里主人那个实体，和 L5 一样）和类型是 Bot 的实体。

**问话的向量：** 本机向量服务，照 Qwen3-Embedding 查询的写法（Instruct: …\\nQuery:…），说明那一句和 J2.15 跑分用的
一样。进程里记住最近 256 句。向量服务没配、没开、2 秒没回，这一次不走向量那一路（实体那一路照跳数、新旧排），别的照走，
记一行日志，不报 degraded（接口文档里 degraded 只有 graph）。

**快慢、出错：** 一次检索一个只读的会话，每一句查询一个只读事务。每一路都等它跑完，不因为慢就扔掉一路：扔不扔看那一刻
机器忙不忙，同一句话几遍答的就不一样（D196）。整个这一步（核标记、几路、取时刻）的查询加起来最多 search_seconds 秒（只算
查询的时间，Budget），这是出事才用的保险：过了就到点掐掉，这次整个主图不用，抛 graphhook.GraphUnavailable，服务退回账本
视图（degraded: ["graph"]），30 秒里不再去查。掐掉靠的是事务的时限：每一句是一个带时限（这一步剩下的那么多）的只读事务，
Neo4j 到点把它停下（大约两秒看一次），这边一直等着它回话，会话随后关掉，图那边不留着接着跑的查询，不会越堆越多（D201）。
只有向量索引第一次用（大图刚起来）时 Neo4j 要等它载完才停得下来（D179 量过 15.5 秒），那一次这一步可能比 30 秒长一些。图那边报别的错（不是超时）只丢那一路，别的照回，记一行。连不上、标记对不上、
账号密码不对，抛 GraphUnavailable，30 秒里不再去连。日志只写种类和条数，不写问话、内容。

**参数**都在 Params 里（gs2），不做成环境变量，量的时候跑分脚本直接传另一组。只有一个开关 JAVMEM_GRAPH_SEARCH=off
（javmem.graph.config）：比较时、出事时关掉主图检索。

**信不过的字**（graphhook 规矩第 6 条，J5.4 审阅第 1 条、复审第 1、2、5 条）：图里事实的文字是 L3 写的，可能写着这个身份
看不到的名字（只在别的范围有出处的标准名、撤回以后改了名的旧名字）；拿这个名字去搜，全文、向量都会把这条看得到的事实带出来，
“查不到”变成“查到”。图慢一拍的时候更多：图里的字还是旧的，旧字里撤回了的名字、词账本已经不认识了。所以：
- 撤回、换版本以后还没同步进图的（javmem.graph.dirty.withdrawn_owed），这一阵 open_graph_searcher 回 None，只用账本视图。
- 服务给 hidden_in（必填）：候选拿去比字、算向量的那段字交过去，照账本现在的样子回信不过的那几条（字里有看不到的叫法、图里
  事实的字和账本现在的不一样、账本里撤回了或者没了）。这几条三路都不要：全文、向量两路和看不到的一样多取再滤，不占名额；
  实体那一路也不要，每个实体先按新旧挑、再按余弦挑，不借问话。万一没给，全都当信不过。
- 全文那一路的池子满了、干净的又不够，接着往下取一页（最多 fulltext_pages 页），挑出来的和前面有多少信不过的无关。
"""

from __future__ import annotations

import atexit
import sqlite3
import sys
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from typing import Any, NoReturn, TypeVar

from javmem.config import ConfigError, Settings
from javmem.embedding import Embedder, EmbeddingSettings, OpenAIEmbeddings, unpack_vector
from javmem.graph import neo
from javmem.graph.config import GraphSettings, GraphTarget, search_switch
from javmem.graph.project import scope_of
from javmem.pipeline.merge import OWNER_NAME, entity_id_of
from javmem.pipeline.normalize import Scope
from javmem.query.graphhook import GraphRequest, GraphUnavailable, Hit
from javmem.query.view import name_key_of
from javmem.timeutil import epoch

SEARCH_VERSION = "gs2"
# 和 J2.15 跑分用的同一句（调试题问题的向量在它的缓存里都有）；事实本身入库时不加前缀
QUERY_INSTRUCTION = "Given a question the user asks about their own past conversations, retrieve memory facts that help answer it"
OWNER_ID = entity_id_of("人", OWNER_NAME)  # 账本里主人那个实体（javmem.pipeline.write 建的）
BOT_TYPE = "Bot"
NEVER = 2**62  # 没有结束：比哪个时间都晚
MAX_LUCENE_SCOPES = 256  # 看得到的范围比这还多，就不写进 Lucene 查询里（只在后面的条件里滤，照样先滤再截）
LOG_PREFIX = "javmem 主图检索："


@dataclass(frozen=True)
class Params:
    """检索的参数（gs2：排名的参数和 gs1 一样，只换了时限）。量的时候跑分脚本直接传另一组。"""

    version: str = SEARCH_VERSION
    per_route: int = 80  # 每一路最多几条（做法写 50；照做法第 9 节在调试题上试了 10 组，80 命中的最多）
    rrf_k: int = 60  # RRF 的常数
    theta: float = 0.27  # 向量那一路的最低余弦（-1 到 1），低于它的不要：调试题标准出处上的事实和问话的余弦，第 5 百分位
    hops: int = 2  # 实体那一路往外扩几跳（0、1、2）
    neighbors: int = 10  # 一跳最多到几个实体
    entity_facts: int = 10  # 每个实体最多几条事
    entity_relations: int = 10  # 每个实体最多几条关系
    entity_scan: int = 50  # 每个实体先按主体在前、新旧挑几条（不看问话），再从里面按余弦挑上面那几条
    far_entities: int = 20  # 两跳那一层最多看几个实体
    far_relations: int = 20  # 两跳那一层最多几条关系
    vector_first: int = 200  # 向量那一路先取几条
    vector_growth: int = 4  # 滤完不够再取几倍
    vector_max: int = 10_000  # 最多取几条
    fulltext_pool: int = 2_000  # 全文那一路一页拿几条（按原样的文字对上几个词、新旧挑），去掉信不过的以后不够再拿一页
    fulltext_pages: int = 10  # 最多拿几页
    max_terms: int = 128  # Lucene 查询里最多几个词（照 Graphiti）
    # 整个主图这一步的查询加起来最多几秒，过了这次不用主图。只是出事时的保险，不是快慢的目标：大图刚起来的第一句，向量
    # 索引第一次用要 15.5 秒（D179），平常 36 包一句 0.1 秒（D196）
    search_seconds: float = 30.0
    embed_seconds: float = 2.0  # 算问话的向量最多等几秒
    query_cache: int = 256  # 进程里记住最近几句问话的向量
    cooldown_seconds: float = 30.0  # 图、向量服务连不上以后，这么久里不再去连（每一句都等满超时太慢）

    def describe(self) -> dict[str, object]:
        return asdict(self)


GS2 = Params()


def query_text(question: str) -> str:
    """Qwen3-Embedding 查询的写法（官方样例 f'Instruct: {task}\\nQuery:{query}'，和 J2.15 一样）。"""
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery:{question}"


def log(text: str) -> None:
    """一行日志（标准错误）：只写种类、条数，不写问话和内容。"""
    print(LOG_PREFIX + text, file=sys.stderr, flush=True)


def is_hub(entity_id: str, entity_type: str | None) -> bool:
    return entity_id == OWNER_ID or entity_type == BOT_TYPE


# ---------- 看得到的范围、过滤 ----------


def visible_scope_ids(request: GraphRequest) -> list[str]:
    """这个身份看得到的范围（服务算好的分区、敏感标记），再按入参 partitions 筛，换成 J5.2 写进图里的范围编号
    （javmem.graph.project.scope_of，同一个函数）。"""
    wanted = None if request.partitions is None else set(request.partitions)
    found: set[str] = set()
    for partitions, sensitive in request.scopes:
        if wanted is not None and not wanted & set(partitions):
            continue
        found.add(scope_of(Scope(tuple(sorted(set(partitions))), tuple(sorted(set(sensitive))))).scope_id)
    return sorted(found)


@dataclass(frozen=True)
class Filters:
    """一次检索的过滤：看得到的范围编号、时间段（秒，含起不含止）、要不要以前的、查询时刻。"""

    scopes: tuple[str, ...]
    time_from: int | None
    time_to: int | None
    history: bool
    as_of: int

    @classmethod
    def of(cls, request: GraphRequest) -> Filters:
        return cls(tuple(visible_scope_ids(request)), request.time_from, request.time_to, request.include_history,
                   epoch(request.as_of))

    def params(self) -> dict[str, object]:
        return {"scopes": list(self.scopes), "t_from": self.time_from, "t_to": self.time_to, "history": self.history,
                "as_of": self.as_of}


def fact_filter(f: str = "f") -> str:
    """Cypher：一条事（变量 f）看得到、现在有效（没开 include_history 时）、和时间段有交叠。参数见 Filters.params。"""
    return (
        f"{f}.scope_id IN $scopes"
        f" AND ($history OR {f}.superseded_by IS NULL"
        f" OR NOT EXISTS {{ MATCH (g:{neo.FACT} {{id: {f}.superseded_by}}) WHERE g.scope_id IN $scopes }})"
        f" AND ($t_to IS NULL OR coalesce({f}.from_lo, {f}.first_said_epoch) < $t_to)"
        f" AND ($t_from IS NULL OR coalesce({f}.from_hi, {f}.first_said_epoch + 1) > $t_from)"
    )


def relation_filter(r: str = "r") -> str:
    """Cypher：一条关系（变量 r）看得到、现在有效（没开 include_history 时）、生效的那一段和时间段有交叠。"""
    low = f"coalesce({r}.from_lo, {r}.first_said_epoch)"
    high = (f"CASE WHEN {r}.to_lo IS NULL THEN {NEVER} WHEN {r}.to_hi - {r}.to_lo <= 86400 THEN {r}.to_lo"
            f" ELSE {r}.to_hi END")
    return (
        f"{r}.scope_id IN $scopes"
        f" AND ($history OR ({r}.state <> 'ended'"
        f" AND ({r}.superseded_by IS NULL"
        f" OR NOT EXISTS {{ MATCH ()-[g:{neo.RELATION} {{id: {r}.superseded_by}}]->() WHERE g.scope_id IN $scopes }})"
        f" AND ({r}.to_hi IS NULL OR {r}.to_hi > $as_of)))"
        f" AND ($t_to IS NULL OR {low} < $t_to)"
        f" AND ($t_from IS NULL OR $t_from < CASE WHEN {high} > {low} + 1 THEN {high} ELSE {low} + 1 END)"
    )


# ---------- 全文：Lucene 查询、看不到的叫法换掉以后数词 ----------


def lucene_query(fields: Sequence[str], words: Iterable[str], scopes: Sequence[str], *, max_terms: int = 128) -> str | None:
    """词都当整句（加引号，里面的 \\ 和 " 转义），任一个对上就算；看得到的范围编号写成必须对上的一项。没有词或者
    一个看得到的范围都没有，回 None（这一路不查）。"""
    terms: list[str] = []
    for word in words:
        text = " ".join(str(word).split())
        if text and text not in terms:
            terms.append(text)
    terms = terms[:max_terms]
    if not terms or not scopes:
        return None
    phrases = ['"' + term.replace("\\", "\\\\").replace('"', '\\"') + '"' for term in terms]
    query = "+(" + " ".join(f"{name}:{phrase}" for phrase in phrases for name in fields) + ")"
    if len(scopes) <= MAX_LUCENE_SCOPES:
        query = "+scope_text:(" + " ".join(scope.lower() for scope in scopes) + ") " + query
    return query


def words_in(text: str, words: Iterable[str]) -> int:
    """对上了几个词（比对写法，和账本视图的 _contains 一样）。"""
    folded = name_key_of(text)
    count = 0
    for word in dict.fromkeys(words):
        key = name_key_of(word)
        if key and key in folded:
            count += 1
    return count


# ---------- 合起来排 ----------


@dataclass(frozen=True)
class Found:
    """一路里的一条候选：哪一样、编号、最近有人说起的时刻（排一样分时用，不知道是 None）。"""

    kind: str
    id: str
    said: int | None = None


def merge_routes(routes: Sequence[Sequence[Found]], *, k: int, want: int,
                 said: Mapping[tuple[str, str], int | None] | None = None) -> list[Hit]:
    """RRF：每一路第 r 名得 1 / (k + r)（一路里同一条只算第一次），几路都有的加起来。分数用分数算（加的先后不影响一样不
    一样）；一样的最近有人说起的在前，再按哪一样、编号。截到 want 条。"""
    scores: dict[tuple[str, str], Fraction] = {}
    newest: dict[tuple[str, str], int] = {key: value for key, value in (said or {}).items() if value is not None}
    for route in routes:
        seen: set[tuple[str, str]] = set()
        for one in route:
            key = (one.kind, one.id)
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, Fraction(0)) + Fraction(1, k + len(seen))
            if one.said is not None:
                newest[key] = max(newest.get(key, one.said), one.said)
    order = sorted(scores, key=lambda key: (-scores[key], -newest.get(key, -NEVER), key[0], key[1]))
    return [Hit(kind, item_id, float(scores[(kind, item_id)])) for kind, item_id in order[: max(want, 0)]]


# ---------- 向量那一路：多取再滤 ----------


@dataclass(frozen=True)
class Scored:
    """向量、实体那两路的一条：哪一样、编号、余弦（-1 到 1，没有向量是 None）、跳数、最近有人说起的时刻。"""

    kind: str
    id: str
    score: float | None
    said: int | None
    hop: int = 0

    def found(self) -> Found:
        return Found(self.kind, self.id, self.said)


VectorFetch = Callable[[int], tuple[int, "float | None", list[Scored]]]


def gather(fetch: VectorFetch, *, theta: float, params: Params = GS2) -> tuple[list[Scored], bool]:
    """5.26 的向量检索不能先滤再取：fetch(k) 从索引取最像的 k 条、在查询里滤掉看不到的、时间段和现行不对的、余弦低于 θ 的，
    回 (取到几条, 取到的里面最低的余弦, 留下的)。不够 per_route 条、又还没取完（取到的和要的一样多）、最低的也还不低于 θ，
    就取 vector_growth 倍再来，最多 vector_max 条。回 (留下的, 是不是取到上限还不够)。"""
    k = min(params.vector_first, params.vector_max)
    while True:
        fetched, lowest, kept = fetch(k)
        enough = len(kept) >= params.per_route
        exhausted = fetched < k
        below = lowest is not None and lowest < theta
        if enough or exhausted or below or k >= params.vector_max:
            return kept, not (enough or exhausted or below)
        k = min(k * params.vector_growth, params.vector_max)


def rank_scored(items: Iterable[Scored], limit: int) -> list[Scored]:
    """实体、向量两路的排法：余弦高的在前（没有的排后面），一样的跳数少的在前，再新的在前，再按哪一样、编号。
    同一条出现几次的留跳数最少的那次。"""
    best: dict[tuple[str, str], Scored] = {}
    for one in items:
        key = (one.kind, one.id)
        old = best.get(key)
        if old is None or (one.hop, -(one.score if one.score is not None else -2.0)) < (
                old.hop, -(old.score if old.score is not None else -2.0)):
            best[key] = one
    order = sorted(best.values(), key=lambda one: (
        one.score is None, -(one.score or 0.0), one.hop, -(one.said if one.said is not None else -NEVER), one.kind, one.id))
    return order[: max(limit, 0)]


# ---------- 问话的向量 ----------


class QueryVectors:
    """问话的向量：本机向量服务，进程里记住最近几句。服务没配（embedder 是 None）、出错，回 None，这一次不走向量那一路。"""

    def __init__(self, embedder: Embedder | None, *, size: int = 256, say: Callable[[str], None] = log,
                 cooldown: float = 30.0) -> None:
        self.embedder = embedder
        self.size = size
        self.say = say
        self.cooldown = cooldown
        self.failures = 0
        self._cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._skip_until = 0.0

    def vector(self, question: str) -> list[float] | None:
        if self.embedder is None:
            return None
        text = query_text(question)
        key = (self.embedder.name, text)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            if time.monotonic() < self._skip_until:  # 刚连不上过：这一阵不再等它
                return None
        try:
            reply = self.embedder.embed([text])
            values = list(unpack_vector(reply.vectors[0]))
        except Exception as error:  # noqa: BLE001 - 向量服务出什么错都只是这一次不走向量那一路
            self.failures += 1
            with self._lock:
                self._skip_until = time.monotonic() + self.cooldown
            self.say(f"问话的向量没算出来（{type(error).__name__}），{self.cooldown:g} 秒里不走向量那一路")
            return None
        with self._lock:
            self._cache[key] = values
            while len(self._cache) > self.size:
                self._cache.popitem(last=False)
        return values


# ---------- 图里的四路（只读） ----------

_ALL_ROLES = ["subject", "object", "mention"]
_ROLE = "CASE x.role WHEN 'subject' THEN 0 WHEN 'object' THEN 1 ELSE 2 END"


def _cosine(var: str) -> str:
    """Neo4j 的余弦函数、向量索引回的都是 (1 + 余弦) / 2：换回 -1 到 1。没有向量的是 null。"""
    return (f"CASE WHEN $vector IS NULL OR {var}.vector IS NULL THEN null"
            f" ELSE 2 * vector.similarity.cosine({var}.vector, $vector) - 1 END")


_MARKER = f"MATCH (m:{neo.MARKER}) RETURN m.marker_id AS id, m.library AS library, m.kind AS kind"
_ENTITY_TYPES = f"MATCH (e:{neo.ENTITY}) WHERE e.id IN $ids RETURN e.id AS id, e.type AS type"
# 关系拿去比字、算向量的那一段（和 javmem.graph.project 算关系向量的那一句一样）
_RELATION_TEXT_OF = "r.content + CASE WHEN r.details IS NULL THEN '' ELSE '（' + r.details + '）' END"
# 一个实体的事：它是主体（subject_id，或者“事 → 实体”上写着 subject），它是对象、提及的（roles 里有的才要）。滤完先按主体在前、
# 新旧挑 scan 条：不看问话（字里写着看不到的叫法的，不能借问话占名额），每个实体几条在外面按余弦挑
_FACTS_OF = f"""
UNWIND $ids AS one
MATCH (e:{neo.ENTITY} {{id: one}})
CALL (e) {{
  CALL (e) {{
    MATCH (f:{neo.FACT}) WHERE f.subject_id = e.id RETURN f, 0 AS role
    UNION ALL
    MATCH (f:{neo.FACT})-[x:{neo.INVOLVES}]->(e) WHERE x.role IN $roles RETURN f, {_ROLE} AS role
  }}
  WITH f, min(role) AS role
  WHERE {fact_filter('f')}
  WITH f, role ORDER BY role, f.as_of_epoch DESC, f.id LIMIT $scan
  RETURN f.id AS id, role, f.as_of_epoch AS said, f.content AS text, {_cosine('f')} AS score
}}
RETURN one AS entity, id, role, said, text, score
"""
# 一个实体的关系：一头是它，或者“代表哪一家”是它。滤完先按新旧挑 scan 条
_RELATIONS_OF = f"""
UNWIND $ids AS one
MATCH (e:{neo.ENTITY} {{id: one}})
CALL (e) {{
  CALL (e) {{
    MATCH (e)-[r:{neo.RELATION}]-() RETURN r
    UNION
    MATCH ()-[r:{neo.RELATION}]->() WHERE r.org_id = e.id RETURN r
  }}
  WITH r WHERE {relation_filter('r')}
  WITH r ORDER BY r.as_of_epoch DESC, r.id LIMIT $scan
  RETURN r.id AS id, 0 AS role, r.as_of_epoch AS said, {_RELATION_TEXT_OF} AS text, {_cosine('r')} AS score
}}
RETURN one AS entity, id, role, said, text, score
"""
# 顺着看得到的、现行（时间段允许）的关系到旁边的实体：不要 skip 里的、不要枢纽；最近说起的先，最多 limit 个
_NEIGHBORS = f"""
CALL () {{
  MATCH (e:{neo.ENTITY})-[r:{neo.RELATION}]-(n:{neo.ENTITY}) WHERE e.id IN $ids RETURN r, n
  UNION
  MATCH (a:{neo.ENTITY})-[r:{neo.RELATION}]->(b:{neo.ENTITY}) WHERE r.org_id IN $ids UNWIND [a, b] AS n RETURN r, n
}}
WITH r, n WHERE {relation_filter('r')} AND NOT n.id IN $skip AND n.id <> $owner AND coalesce(n.type, '') <> $bot
WITH n, max(r.as_of_epoch) AS said
ORDER BY said DESC, n.id
LIMIT $limit
RETURN n.id AS id
"""
# 全文：Lucene 在索引里滤掉看不到的范围；后面的条件滤时间段、现行；按原样的文字对上几个词、新旧排，一页 pool 条（SKIP 翻页）
_FACT_TEXT = f"""
CALL db.index.fulltext.queryNodes('jm_fact_text', $lucene) YIELD node AS f
WITH f WHERE {fact_filter('f')}
WITH f, size([w IN $keys WHERE toLower(f.content) CONTAINS w]) AS hits
ORDER BY hits DESC, f.as_of_epoch DESC, f.id
SKIP $skip LIMIT $pool
RETURN f.id AS id, f.content AS text, f.as_of_epoch AS said
"""
_RELATION_TEXT = f"""
CALL db.index.fulltext.queryRelationships('jm_relation_text', $lucene) YIELD relationship AS r
WITH r WHERE {relation_filter('r')}
WITH r, toLower(r.content + ' ' + coalesce(r.details, '')) AS plain
WITH r, size([w IN $keys WHERE plain CONTAINS w]) AS hits
ORDER BY hits DESC, r.as_of_epoch DESC, r.id
SKIP $skip LIMIT $pool
RETURN r.id AS id, {_RELATION_TEXT_OF} AS text, r.as_of_epoch AS said
"""
# 向量：索引取最像的 k 条（5.26 不能先滤再取），在这里滤，回取到几条、最低的余弦、留下的（编号、余弦、新旧、那段字）。
# 索引给的分是量化过的近似值（vector.quantization.enabled 默认开），θ 和名次用余弦函数现算的（和实体那一路一样，审阅第 3 条）
_FACT_VECTOR = f"""
CALL db.index.vector.queryNodes('jm_fact_vector', $k, $vector) YIELD node AS f
WITH f, 2 * vector.similarity.cosine(f.vector, $vector) - 1 AS cos
WITH f, cos, cos >= $theta AND {fact_filter('f')} AS keep
RETURN count(*) AS fetched, min(cos) AS lowest,
       collect(CASE WHEN keep THEN [f.id, cos, f.as_of_epoch, f.content] END) AS kept
"""
_RELATION_VECTOR = f"""
CALL db.index.vector.queryRelationships('jm_relation_vector', $k, $vector) YIELD relationship AS r
WITH r, 2 * vector.similarity.cosine(r.vector, $vector) - 1 AS cos
WITH r, cos, cos >= $theta AND {relation_filter('r')} AS keep
RETURN count(*) AS fetched, min(cos) AS lowest,
       collect(CASE WHEN keep THEN [r.id, cos, r.as_of_epoch, {_RELATION_TEXT_OF}] END) AS kept
"""
# 账本那一路里图也有的：最近有人说起的时刻（排一样分时用）
_SAID_FACTS = (f"UNWIND $ids AS one MATCH (f:{neo.FACT} {{id: one}}) WHERE f.scope_id IN $scopes"
               " RETURN one AS id, f.as_of_epoch AS said")
_SAID_RELATIONS = (f"UNWIND $ids AS one MATCH ()-[r:{neo.RELATION} {{id: one}}]->() WHERE r.scope_id IN $scopes"
                   " RETURN one AS id, r.as_of_epoch AS said")

T = TypeVar("T")


def _unavailable() -> tuple[type[BaseException], ...]:
    """连不上、断了、账号密码不对（驱动这一边的错都算）：整个主图这一步不要了，服务退回账本视图。"""
    from neo4j.exceptions import AuthError, DriverError

    return (DriverError, AuthError, OSError)


Key = tuple[str, str]
Text = tuple[str, str, str]  # (哪一样, 编号, 图里拿去比字、算向量的那段字)
CHECK_BATCH = 100  # 问服务“字里有没有看不到的叫法”时一次问几条


class OutOfTime(Exception):
    """这一步给查询的时间用完了。"""


class Budget:
    """整个主图这一步给查询的时间（search_seconds）：每一句查询是一个只读的自动提交事务（会话是只读的），超时给剩下的那么多；
    只算查询自己花的时间。问服务“字里有没有看不到的叫法”的时间不算：看不到的名字多、问得久，这一步就超时，结果就跟着看不到
    的名字变了（复审第 4 条）。"""

    def __init__(self, session: Any, seconds: float) -> None:
        self.session = session
        self.left = seconds

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        import neo4j

        if self.left <= 0:
            raise OutOfTime
        started = time.monotonic()
        try:
            return list(self.session.run(neo4j.Query(query, timeout=max(self.left, 0.001)), params).data())
        finally:
            self.left -= time.monotonic() - started


class Hidden:
    """这一次检索里，哪几条的字信不过（graphhook 规矩第 6 条，服务照账本现在的样子算：字里写着这个身份看不到的叫法、图里的字
    和账本现在的不一样、账本里撤回了或者没了）。问过的记着。这几条三路都不要。
    服务没给（request.hidden_in 是 None，正常不会）就全都当信不过：宁可不比字、不比向量（复审第 5 条）。"""

    def __init__(self, check: Callable[[Sequence[Text]], set[Key]] | None) -> None:
        self.check = check
        self.known: dict[Key, bool] = {}

    def __call__(self, items: Sequence[Text]) -> set[Key]:
        if not items:
            return set()
        if self.check is None:
            return {(kind, item_id) for kind, item_id, _ in items}
        todo = list({(kind, item_id): (kind, item_id, text) for kind, item_id, text in items
                     if (kind, item_id) not in self.known}.values())
        if todo:
            found = self.check(todo)
            for kind, item_id, _ in todo:
                self.known[(kind, item_id)] = (kind, item_id) in found
        return {(kind, item_id) for kind, item_id, _ in items if self.known.get((kind, item_id))}

    def first_clean(self, ordered: Sequence[Text], want: int) -> list[Key]:
        """照先后一批批问，凑够 want 条干净的就停。回干净的那几条（照原来的先后）。"""
        kept: list[Key] = []
        for start in range(0, len(ordered), CHECK_BATCH):
            batch = ordered[start : start + CHECK_BATCH]
            bad = self(batch)
            kept.extend((kind, item_id) for kind, item_id, _ in batch if (kind, item_id) not in bad)
            if len(kept) >= want:
                break
        return kept[:want]


class GraphSearch:
    """一个库的主图检索器（graphhook.GraphSearcher 的样子）。驱动在进程里留着；一次检索一个只读会话，整个这一步的查询有一个
    总时限（Budget）。marker_id 是账本记着的那个图的标记：图里的对不上（不是这本账的图），一个字不查。连不上、超过总时限以后
    cooldown_seconds 秒里不再去查，直接抛 GraphUnavailable（不然 Neo4j 卡住时每一句都要等满超时）。"""

    def __init__(self, target: GraphTarget, *, marker_id: str, embedder: Embedder | None = None, timeout: float = 5.0,
                 params: Params = GS2, say: Callable[[str], None] = log) -> None:
        self.target = target
        self.marker_id = marker_id
        self.timeout = timeout
        self.params = params
        self.say = say
        self.vectors = QueryVectors(embedder, size=params.query_cache, say=say, cooldown=params.cooldown_seconds)
        self.stats: Counter[str] = Counter()
        self._driver: Any = None
        self._lock = threading.Lock()
        self._down_until = 0.0

    # ---------- 连接 ----------

    def _session(self) -> Any:
        import neo4j
        from neo4j import GraphDatabase

        with self._lock:
            if self._driver is None:
                self._driver = GraphDatabase.driver(
                    self.target.uri, auth=(self.target.user, self.target.require_password()), **neo._timeouts(self.timeout)
                )
            driver = self._driver
        return driver.session(database=neo.DATABASE, default_access_mode=neo4j.READ_ACCESS)

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None

    # ---------- 一次检索 ----------

    def search(self, request: GraphRequest) -> list[Hit]:
        p = self.params
        filters = Filters.of(request)
        ledger = [Found(hit.kind, hit.id) for hit in request.ledger_hits]
        if not filters.scopes:  # 一个看得到的范围都没有：图里没有这个身份的东西
            return merge_routes([ledger], k=p.rrf_k, want=request.want)
        if time.monotonic() < self._down_until:  # 刚连不上过：这一阵直接当主图没接
            self.stats["cooldown"] += 1
            raise GraphUnavailable("cooldown")
        vector = self.vectors.vector(request.question)
        hidden = Hidden(request.hidden_in)
        try:
            with self._session() as session:
                budget = Budget(session, p.search_seconds)  # 整个这一步一个总时限，几路共用
                self._check_marker(budget, request.library)
                routes = [
                    self._route(budget, "entity", lambda tx: self._entity_route(tx, request, filters, vector, hidden), []),
                    self._route(budget, "fulltext", lambda tx: self._fulltext_route(tx, request, filters, hidden), []),
                    self._route(budget, "vector", lambda tx: self._vector_route(tx, filters, vector, hidden), [])
                    if vector else [],
                ]
                known = {(one.kind, one.id) for route in routes for one in route}
                missing = [one for one in ledger if (one.kind, one.id) not in known]
                said = self._route(budget, "said", lambda tx: self._said(tx, filters, missing), {}) if missing else {}
        except _unavailable() as error:
            self._went_down(type(error).__name__)
            raise GraphUnavailable(type(error).__name__) from None
        return merge_routes([*routes, ledger], k=p.rrf_k, want=request.want, said=said)

    def _went_down(self, what: str) -> None:
        self.stats["unavailable"] += 1
        self._down_until = time.monotonic() + self.params.cooldown_seconds
        self.say(f"主图连不上（{what}），{self.params.cooldown_seconds:g} 秒里不再去连，只用账本视图")

    def _check_marker(self, budget: Budget, library: str) -> None:
        """图里的标记要正好一个，种类、库、编号都和账本记着的一样。核的时候图那边报错（库没起来、超时）也当连不上。"""
        from neo4j.exceptions import AuthError, Neo4jError

        try:
            rows = budget.run(_MARKER)
        except AuthError:
            raise
        except Neo4jError as error:
            code = str(getattr(error, "code", "") or type(error).__name__)
            self._went_down(code)
            raise GraphUnavailable(code) from None
        if len(rows) != 1 or rows[0]["kind"] != neo.KIND or rows[0]["library"] != library or rows[0]["id"] != self.marker_id:
            self.stats["marker"] += 1
            self.say("主图的标记和账本记的对不上，这次不查主图")
            raise GraphUnavailable("marker")

    def _route(self, budget: Budget, name: str, work: Callable[[Budget], T], default: T) -> T:
        """一路的查询，用整个这一步的总时限（Budget），慢也等它跑完。图那边报错（不是超时）的这一路不要，别的照回，记一行：
        同样的问话每次都一样错，不会时有时无。超时（这一步的查询加起来过了 search_seconds 秒）整个主图这一步不要（_timed_out）。"""
        from neo4j.exceptions import AuthError, Neo4jError

        try:
            return work(budget)
        except AuthError:
            raise
        except (Neo4jError, OutOfTime) as error:
            code = "OutOfTime" if isinstance(error, OutOfTime) else str(getattr(error, "code", "") or type(error).__name__)
            if code == "OutOfTime" or "TimedOut" in code:
                self._timed_out(name)
            self.stats[f"{name}_error"] += 1
            self.say(f"{name} 那一路出错（{code}），这次不要它")
            return default

    def _timed_out(self, name: str) -> NoReturn:
        """这一步的查询加起来过了 search_seconds 秒（出事了：图卡住、太忙）：这次整个主图不用，服务照账本视图答
        （degraded: ["graph"]），不悄悄少一路；cooldown_seconds 秒里不再去查，不然图卡住时每一句都要等满。"""
        p = self.params
        self.stats["timeout"] += 1
        self._down_until = time.monotonic() + p.cooldown_seconds
        self.say(f"主图这一步的查询超过 {p.search_seconds:g} 秒（停在 {name} 那一路），这次只用账本视图；"
                 f"{p.cooldown_seconds:g} 秒里不再去查")
        raise GraphUnavailable("timeout")

    # ---------- 实体那一路 ----------

    def _entity_route(self, budget: Budget, request: GraphRequest, filters: Filters, vector: list[float] | None,
                      hidden: Hidden) -> list[Found]:
        """信不过的那几条（规矩第 6 条）整条不要；每个实体挑哪几条也不借问话：先按主体在前、新旧挑 scan 条，再按余弦挑。"""
        p = self.params
        seeds = list(dict.fromkeys(request.seeds))
        if not seeds:
            return []
        base = {**filters.params(), "vector": vector, "owner": OWNER_ID, "bot": BOT_TYPE, "scan": p.entity_scan}
        types = {row["id"]: row["type"] for row in budget.run(_ENTITY_TYPES, ids=seeds)}
        seeds = [one for one in seeds if one in types]
        normal = [one for one in seeds if not is_hub(one, types[one])]
        found: list[Scored] = []

        def pull(ids: list[str], hop: int, roles: list[str] | None, facts: int, relations: int) -> list[Scored]:
            rows = [("relation", row) for row in budget.run(_RELATIONS_OF, ids=ids, **base)] if relations else []
            if roles is not None and facts:
                rows += [("fact", row) for row in budget.run(_FACTS_OF, ids=ids, roles=roles, **base)]
            bad = hidden([(kind, str(row["id"]), str(row["text"] or "")) for kind, row in rows])
            groups: dict[tuple[str, str], list[tuple[float | None, int, int, str]]] = {}
            for kind, row in rows:
                if (kind, row["id"]) in bad:
                    continue
                score = None if row["score"] is None else float(row["score"])
                said = row["said"] if row["said"] is not None else -NEVER
                groups.setdefault((str(row["entity"]), kind), []).append((score, int(row["role"]), said, str(row["id"])))
            out: list[Scored] = []
            for (_entity, kind), group in sorted(groups.items()):
                group.sort(key=lambda one: (one[0] is None, -(one[0] or 0.0), one[1], -one[2], one[3]))
                for score, _role, said, item_id in group[: facts if kind == "fact" else relations]:
                    out.append(Scored(kind, item_id, score, None if said == -NEVER else said, hop))
            return out

        found.extend(pull(seeds, 0, _ALL_ROLES, p.entity_facts, p.entity_relations))  # 枢纽也拿它直接连着的，只是不往外扩
        if p.hops >= 1 and normal:
            near = [row["id"] for row in budget.run(_NEIGHBORS, ids=normal, skip=seeds, limit=p.neighbors, **base)]
            if near:
                found.extend(pull(near, 1, ["subject"], p.entity_facts, p.entity_relations))
                if p.hops >= 2:
                    far = [row["id"] for row in budget.run(_NEIGHBORS, ids=near, skip=[*seeds, *near], limit=p.far_entities,
                                                           **base)]
                    if far:
                        seen = {(one.kind, one.id) for one in found}
                        rows = [one for one in pull(far, 2, None, 0, p.far_relations) if (one.kind, one.id) not in seen]
                        found.extend(rank_scored(rows, p.far_relations))
        return [one.found() for one in rank_scored(found, p.per_route)]

    # ---------- 全文那一路 ----------

    def _fulltext_route(self, budget: Budget, request: GraphRequest, filters: Filters, hidden: Hidden) -> list[Found]:
        """信不过的那几条（规矩第 6 条）整条不要；干净的那些原样就是换过名字的样子，照原样数词。每一样（事、关系）一页按
        原样的字对上几个词、新旧挑 fulltext_pool 条；去掉信不过的以后干净的不够 per_route 条、又还没取完，接着往下取一页，
        最多 fulltext_pages 页：挑出来的干净的那几条，和前面有多少信不过的无关（复审第 3 条）。"""
        p = self.params
        words = [word for word in dict.fromkeys(" ".join(str(one).split()) for one in request.words) if word][: p.max_terms]
        queries = {"fact": (_FACT_TEXT, lucene_query(["content"], words, filters.scopes, max_terms=p.max_terms)),
                   "relation": (_RELATION_TEXT, lucene_query(["content", "details"], words, filters.scopes, max_terms=p.max_terms))}
        if any(lucene is None for _, lucene in queries.values()):
            return []
        base = {**filters.params(), "keys": [word.lower() for word in words], "pool": p.fulltext_pool}
        scored: list[tuple[int, int, str, str]] = []
        for kind, (query, lucene) in queries.items():
            clean: list[tuple[int, int, str, str]] = []
            skip, full = 0, True
            for _page in range(p.fulltext_pages):
                rows = budget.run(query, lucene=lucene, skip=skip, **base)
                skip += len(rows)
                page: list[tuple[int, int, str, str, str]] = []
                for row in rows:
                    text = str(row["text"] or "")
                    hits = words_in(text, words)
                    if hits:
                        page.append((hits, row["said"] if row["said"] is not None else -NEVER, kind, str(row["id"]), text))
                bad = hidden([(one_kind, item_id, text) for _, _, one_kind, item_id, text in page])
                clean.extend((hits, said, one_kind, item_id) for hits, said, one_kind, item_id, _ in page
                             if (one_kind, item_id) not in bad)
                full = len(rows) >= p.fulltext_pool
                if not full or len(clean) >= p.per_route:
                    break
            if full and len(clean) < p.per_route:
                self.stats["fulltext_pool"] += 1  # 取到 fulltext_pages 页还不够
            scored.extend(clean)
        scored.sort(key=lambda one: (-one[0], -one[1], one[2], one[3]))
        return [Found(kind, item_id, None if said == -NEVER else said) for _, said, kind, item_id in scored[: p.per_route]]

    # ---------- 向量那一路 ----------

    def _vector_route(self, budget: Budget, filters: Filters, vector: list[float] | None, hidden: Hidden) -> list[Found]:
        """信不过的那几条整条不要：它的向量是照那段字算的（规矩第 6 条）。和看不到的一样多取再滤。"""
        if vector is None:
            return []
        p = self.params
        base = {**filters.params(), "vector": vector, "theta": p.theta}
        kept: list[Scored] = []
        for kind, query in (("fact", _FACT_VECTOR), ("relation", _RELATION_VECTOR)):

            def fetch(k: int, kind: str = kind, query: str = query) -> tuple[int, float | None, list[Scored]]:
                row = budget.run(query, k=k, **base)[0]
                rows = sorted(((str(one[0]), float(one[1]), one[2], str(one[3] or "")) for one in (row["kept"] or [])),
                              key=lambda one: (-one[1], -(one[2] if one[2] is not None else -NEVER), one[0]))
                clean = set(hidden.first_clean([(kind, item_id, text) for item_id, _, _, text in rows], p.per_route))
                found = [Scored(kind, item_id, cos, said) for item_id, cos, said, _ in rows if (kind, item_id) in clean]
                lowest = float(row["lowest"]) if row["lowest"] is not None else None
                return int(row["fetched"] or 0), lowest, found

            rows, short = gather(fetch, theta=p.theta, params=p)
            if short:
                self.stats["vector_short"] += 1
                self.say(f"向量那一路取到 {p.vector_max} 条还不够 {p.per_route} 条看得到的（{kind}），这次少几条")
            kept.extend(rows)
        return [one.found() for one in rank_scored(kept, p.per_route)]

    # ---------- 账本那一路里图也有的：最近有人说起的时刻 ----------

    def _said(self, budget: Budget, filters: Filters, missing: Sequence[Found]) -> dict[tuple[str, str], int | None]:
        out: dict[tuple[str, str], int | None] = {}
        for kind, query in (("fact", _SAID_FACTS), ("relation", _SAID_RELATIONS)):
            ids = [one.id for one in missing if one.kind == kind]
            if ids:
                for row in budget.run(query, ids=ids, scopes=list(filters.scopes)):
                    out[(kind, str(row["id"]))] = row["said"]
        return out


# ---------- 接进查询接口（graphhook.open_searcher） ----------

_SEARCHERS: dict[tuple[object, ...], GraphSearch] = {}
_SEARCHERS_LOCK = threading.Lock()
_SAID_ONCE: set[str] = set()


def _say_once(text: str) -> None:
    """设置这一类的提醒一个进程只打一次（每次查询都要看一遍设置）。"""
    if text not in _SAID_ONCE:
        _SAID_ONCE.add(text)
        log(text)


def ledger_graph(settings: Settings, library: str) -> tuple[str, str | None] | None:
    """这本账接上的图：(标记, 关系的向量是哪个模型算的；不知道是 None)。没接上图（javmem graph init）、上一回整张同步没做完
    （整张同步时先清掉的）、图的样子（版本、时区）和这一版对不上的（要先 javmem graph sync），都回 None。
    撤回、换版本以后还没同步进图的（主图里还留着撤回了的东西，javmem.graph.dirty.withdrawn_owed）也回 None：图里那时的字
    还是旧的，旧字里撤回了的名字、词照样能把看得到的东西搜出来（复审第 1、2 条），这一阵只用账本视图。只读账本。"""
    from javmem.graph.dirty import withdrawn_owed
    from javmem.graph.sync import projection_key, read_state
    from javmem.ledger import connect_readonly

    path = settings.library(library).db_path
    if not path.exists():
        return None
    try:
        conn = connect_readonly(path)
        try:
            state = read_state(conn)
            stale = state.enabled and withdrawn_owed(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not state.enabled or not state.marker_id or state.rebuilt_at is None or not state.projection:
        return None
    if stale:
        _say_once("撤回了的还在主图里（撤回、换版本以后还没同步），这一阵只用账本视图；跑 javmem graph sync")
        return None
    shape, _, model = state.projection.partition("#")
    if shape != projection_key(settings.timezone).partition("#")[0]:
        return None
    return state.marker_id, (None if model in ("", "-") else model)


def query_embedder(env: Mapping[str, str] | None = None, *, seconds: float = GS2.embed_seconds) -> Embedder | None:
    """算问话向量的本机向量服务（JAVMEM_EMBEDDING_*，和写账同一个），最多等 seconds 秒。没配回 None。"""
    try:
        embedding = EmbeddingSettings.from_env(env)
    except ConfigError:
        return None
    return OpenAIEmbeddings(replace(embedding, timeout=min(embedding.timeout, seconds)))


def open_graph_searcher(settings: Settings, library: str, *, env: Mapping[str, str] | None = None,
                        params: Params = GS2) -> GraphSearch | None:
    """graphhook.open_searcher 回的：主图配好了（JAVMEM_GRAPH_SEARCH 没关、这个库的图设了密码、账本接上了图、整张同步过、
    图的样子对得上）回这个库的检索器，进程里留着（驱动、问话的向量都在里面）；不然回 None，memory_search 只用账本视图。
    这里不连图：连不上、标记对不上，检索的时候抛 GraphUnavailable。"""
    on, problem = search_switch(env)
    if problem:
        _say_once(problem)
    if not on:
        return None
    try:
        graph = GraphSettings.from_env(env, check_sync=False)  # 写账的开关写错了不连累查主图
    except ConfigError as error:
        _say_once(f"主图的设置不对（{error}），只用账本视图")
        return None
    target = graph.target(library)
    if not target.password:
        return None
    found = ledger_graph(settings, library)
    if found is None:
        return None
    marker_id, model = found
    embedder = query_embedder(env, seconds=params.embed_seconds)
    if embedder is not None and model is not None and embedder.name != model:
        _say_once("向量服务的模型和图里的向量不是同一个，不走向量那一路")
        embedder = None
    key: tuple[object, ...] = (target, marker_id, graph.timeout, getattr(embedder, "settings", None), params)
    with _SEARCHERS_LOCK:
        searcher = _SEARCHERS.get(key)
        if searcher is None:
            searcher = GraphSearch(target, marker_id=marker_id, embedder=embedder, timeout=graph.timeout, params=params)
            _SEARCHERS[key] = searcher
    return searcher


def close_searchers() -> None:
    """关掉进程里留着的检索器（驱动）。退出时自己调。"""
    with _SEARCHERS_LOCK:
        for searcher in _SEARCHERS.values():
            searcher.close()
        _SEARCHERS.clear()


atexit.register(close_searchers)
