"""给主图检索（J5.4）留的口子（J5.5 做法第六节）。

J5.4 只换了一个函数：`open_searcher` 回 javmem.graph.search 的检索器，服务每次 memory_search 叫它一次
`search(GraphRequest)`，拿回最后的名次（编号和分数）。主图没配好（JAVMEM_GRAPH_SEARCH=off、没设这个库的图的密码、账本没接上
图、要先整张同步）它回 None，memory_search 就只用账本视图，`degraded: ["graph"]`。

**规矩（J5.4 照着接）：**

1. **过滤在检索里面**（query-api 3.2）：只在 `scopes` 里的、和 `partitions` 有交集的、`time_from`/`time_to` 和
   `include_history` 允许的里面找、排、截断（J5.2 图里每个节点、每条边写着范围）。不许先取前 N 条再滤。
2. **只回编号和名次，不回内容。** 服务拿编号回账本取内容、状态、证据（原句不在图里，D177），在同一个读事务里；账本里
   已经撤回、被替代（没开 include_history）、看不到的那几条丢掉（图慢一拍时会有），其余照回来的名次排、翻页。
3. **待确认的图里没有**（J5.1 定的），所以服务把账本视图自己找到的（`ledger_hits`，里面有待确认的）交给检索器，由它合成
   最后的名次。没定怎么合之前的默认见 `merge_default`：图里的在前，账本视图里图没有的接在后面。
4. 连不上、超时要快（J5.1 的 `JAVMEM_GRAPH_TIMEOUT_SECONDS`），抛 `GraphUnavailable`：服务当主图没接，退回账本视图，
   `degraded: ["graph"]`。检索器自己的连接可以缓存在进程里。
5. 接上以后 `searched` 写 `["graph", "ledger"]`（再查了原文层加 `"raw"`），`degraded` 是 `[]`。
6. **信不过的字，不拿去比、去排**（J5.4 审阅第 1 条、复审第 1、2、5 条）：图里事实的文字是 L3 写的，可能写着这个身份
   看不到的标准名（只在别的范围有出处的、撤回以后改了名的旧名字）；图慢一拍时图里的字还是旧的。拿这样的名字、词去搜，全文、
   向量都会把看得到的事实带出来。服务给 `hidden_in`（必填）：交来几条（哪一样、编号、图里拿去比字、算向量的那段字），照账本
   现在的样子回信不过的那几条：字里写着这个身份看不到的叫法、图里事实的字和账本现在的不一样、账本里撤回了或者没了。检索器
   三路都不要这几条。撤回、换版本以后还没同步进图的那一阵，`open_searcher` 回 None（只用账本视图）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from javmem.config import Settings

FACT = "fact"
RELATION = "relation"


@dataclass(frozen=True)
class Hit:
    kind: str  # "fact" 或 "relation"
    id: str  # 和账本一样的编号（F…、R…）
    score: float  # 越大越相关，只用来看名次


Scope = tuple[tuple[str, ...], tuple[str, ...]]  # (分区, 敏感标记)，都排好序、去了重，和账本 scope_* 两列一样


@dataclass(frozen=True)
class GraphRequest:
    library: str  # "prod" / "test"：连哪个图（D37 两个实例），身份的 env 定的
    question: str  # 原样的问话
    words: tuple[str, ...]  # 切好的词（和回退原文层用的一样）
    seeds: tuple[str, ...]  # 账本视图在问话里认出来的实体编号（只认这个身份看得到的叫法）
    scopes: tuple[Scope, ...]  # 这个身份看得到的可见范围（账本里有的那几种，入参 partitions 也算进去了）
    partitions: tuple[str, ...] | None  # 入参 partitions（已经核过不越权）
    time_from: int | None  # 秒，含
    time_to: int | None  # 秒，不含
    include_history: bool
    as_of: datetime
    want: int  # 最多要几条（偏移 + 这一页 + 1，再多留一些给图里过时的）
    ledger_hits: tuple[Hit, ...]  # 账本视图自己找到的、排好的（里面有待确认的：图里没有待确认的）
    # 规矩第 6 条（必填）：交来 (哪一样, 编号, 那段字)，回信不过的 (哪一样, 编号)。只在服务这一次的读事务里能用
    hidden_in: Callable[[Sequence[tuple[str, str, str]]], set[tuple[str, str]]]


class GraphUnavailable(Exception):
    """主图连不上、超时：服务当主图没接，degraded: ["graph"]。消息里不放查询词和内容。"""


class GraphSearcher(Protocol):
    def search(self, request: GraphRequest) -> Sequence[Hit]:
        """最后的名次（已经把 ledger_hits 合进去了）。连不上抛 GraphUnavailable。"""
        ...


def open_searcher(settings: Settings, library: str) -> GraphSearcher | None:
    """这个库的主图检索器（J5.4，javmem.graph.search.open_graph_searcher）；主图没配好就回 None。这里不连图。"""
    from javmem.graph.search import open_graph_searcher  # 用到才导入（neo4j 驱动也是用到才导入）

    return open_graph_searcher(settings, library)


def merge_default(graph_hits: Sequence[Hit], ledger_hits: Sequence[Hit]) -> list[Hit]:
    """没定怎么合之前的默认：图里的在前（照图的名次），账本视图里图没有的接在后面（照账本视图的名次）。"""
    seen = {(hit.kind, hit.id) for hit in graph_hits}
    return [*graph_hits, *(hit for hit in ledger_hits if (hit.kind, hit.id) not in seen)]
