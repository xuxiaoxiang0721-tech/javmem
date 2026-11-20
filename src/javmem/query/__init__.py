"""查询接口（J5.5，query-api.md）：一个 MCP 服务，五个只读工具。

- `common`：五个工具共用的外层、出错、入参、时间、拒绝的日志。
- `view`：账本视图，事实、关系、别名、实体从账本只读地查，过滤都在 SQL 里。
- `tools`：五个工具（raw_search 就是原文层的 `javmem.raw.raw_search`）。
- `graphhook`：给主图检索（J5.4）留的口子。
- `mcp`：stdio 上的 MCP 服务（`javmem serve`）。

这里不导入下面的模块：原文层（javmem.raw.search）要用 common，导入 tools 会绕回原文层。
"""
