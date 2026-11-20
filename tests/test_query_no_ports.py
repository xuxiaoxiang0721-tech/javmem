"""查询服务不开端口（U3，docs/security.md“查询服务和身份”）：`javmem serve` 只走 stdio。

身份靠部署（JAVMEM_CALLER 写在 Bot 的 MCP 配置里），没有网络上的鉴权，所以查询服务一定不能自己听端口：
javmem.query 下面的模块里不许出现开端口、起网络服务的写法；`javmem serve` 只交给 serve_stdio。
以后真要开 HTTP，得先加鉴权、改这里和 docs/security.md，不能悄悄开。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from javmem import cli

QUERY = Path(__file__).resolve().parents[1] / "src" / "javmem" / "query"
# 开端口、起网络服务的写法：这几样一个都不许有
FORBIDDEN = re.compile(
    r"^\s*(?:import|from)\s+(?:socket|socketserver|http\.server|asyncio\.(?:base_events|streams)|aiohttp|uvicorn|"
    r"starlette|fastapi|websockets|tornado|flask)\b"
    r"|\bstart_server\s*\(|\bcreate_server\s*\(|\bHTTPServer\b|\bTCPServer\b|\.listen\s*\(|\.bind\s*\(\s*\(",
    re.MULTILINE,
)


def test_the_query_modules_never_open_a_port() -> None:
    found = []
    for path in sorted(QUERY.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        found += [f"{path.name}:{text.count(chr(10), 0, match.start()) + 1}" for match in FORBIDDEN.finditer(text)]
    assert sorted(path.name for path in QUERY.glob("*.py"))  # 目录找对了
    assert found == []


def test_serve_only_hands_over_to_stdio() -> None:
    source = inspect.getsource(cli._cmd_serve)
    assert "serve_stdio" in source and not FORBIDDEN.search(source)


def test_the_check_would_catch_a_server() -> None:
    """这道核自己管用：常见的几种开端口的写法都认得出。"""
    for text in ("import socket\n", "from http.server import HTTPServer\n", "await asyncio.start_server(handle, '0.0.0.0', 80)\n",
                 "server.listen(5)\n", "sock.bind(('127.0.0.1', 8080))\n", "import uvicorn\n"):
        assert FORBIDDEN.search(text), text
