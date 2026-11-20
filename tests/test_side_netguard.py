"""支线的闸（javmem.side.netguard）：只许连本机那几个端口，同步、asyncio 两种事件循环、查域名都拦。不连外网：
拦下的地址都是编的（文档用的 192.0.2.x、example.invalid），放过的是测试自己在本机起的端口。"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading

import pytest

from javmem.side.netguard import NetBlocked, NetGuard


def _server() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]

    def accept() -> None:
        try:
            conn, _ = server.accept()
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    return server, port


def test_sync_connect_only_to_allowed_local_ports() -> None:
    server, port = _server()
    try:
        with NetGuard([port]) as guard:
            with socket.create_connection(("127.0.0.1", port), timeout=5):
                pass
            with pytest.raises(NetBlocked):
                socket.socket().connect(("192.0.2.10", 443))
            with pytest.raises(NetBlocked):
                socket.socket().connect(("127.0.0.1", 9))  # 本机的、可不在放过的端口里
            with pytest.raises(NetBlocked):
                socket.socket().connect_ex(("192.0.2.11", 80))
        assert [(item.host, item.port) for item in guard.blocked] == [("192.0.2.10", 443), ("127.0.0.1", 9),
                                                                       ("192.0.2.11", 80)]
        assert all(item.where.startswith("socket.") for item in guard.blocked)
    finally:
        server.close()


def test_name_lookups_are_blocked_except_localhost() -> None:
    with NetGuard([]) as guard:
        with pytest.raises(NetBlocked):
            socket.getaddrinfo("example.invalid", 443)
        with pytest.raises(NetBlocked):
            socket.gethostbyname("example.invalid")
        assert socket.getaddrinfo("127.0.0.1", 80)
        assert socket.getaddrinfo("localhost", 80)
    assert [item.where for item in guard.blocked] == ["getaddrinfo", "gethostbyname"]


def test_reverse_lookups_are_checked() -> None:
    with NetGuard([]) as guard:
        with pytest.raises(NetBlocked):
            socket.getnameinfo(("192.0.2.20", 80), 0)
        assert socket.getnameinfo(("127.0.0.1", 80), socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)
    assert [item.where for item in guard.blocked] == ["getnameinfo"]


@pytest.mark.parametrize("loop_class", [asyncio.SelectorEventLoop] + ([asyncio.ProactorEventLoop] if sys.platform == "win32" else []),
                         ids=lambda cls: cls.__name__)
def test_asyncio_udp_sendto_is_checked(loop_class: type[asyncio.AbstractEventLoop]) -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            with pytest.raises(NetBlocked):
                await loop.sock_sendto(sock, b"x", ("192.0.2.21", 53))
        finally:
            sock.close()

    with NetGuard([]) as guard:
        loop = loop_class()
        try:
            loop.run_until_complete(main())
        finally:
            loop.close()
    assert [(item.host, item.port) for item in guard.blocked] == [("192.0.2.21", 53)]


def test_uninstall_restores_everything() -> None:
    before = (socket.socket.connect, socket.getaddrinfo, asyncio.selector_events.BaseSelectorEventLoop.sock_connect)
    with NetGuard([1]):
        assert socket.socket.connect is not before[0]
    assert (socket.socket.connect, socket.getaddrinfo, asyncio.selector_events.BaseSelectorEventLoop.sock_connect) == before


def test_socketpair_still_works() -> None:
    """Windows 上 socketpair 是自己连自己的一个临时端口（asyncio 的事件循环要用）：本进程自己 listen 的放过。"""
    with NetGuard([]) as guard:
        left, right = socket.socketpair()
        left.close()
        right.close()
    assert guard.blocked == []


def _loops() -> list[type[asyncio.AbstractEventLoop]]:
    loops: list[type[asyncio.AbstractEventLoop]] = [asyncio.SelectorEventLoop]
    if sys.platform == "win32":
        loops.append(asyncio.ProactorEventLoop)
    return loops


@pytest.mark.parametrize("loop_class", _loops(), ids=lambda cls: cls.__name__)
def test_asyncio_connections_are_checked_on_both_loops(loop_class: type[asyncio.AbstractEventLoop]) -> None:
    server, port = _server()

    async def main() -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        loop = asyncio.get_running_loop()
        sock = socket.socket()
        sock.setblocking(False)
        with pytest.raises(NetBlocked):
            await loop.sock_connect(sock, ("192.0.2.12", 7687))
        sock.close()
        with pytest.raises(OSError):
            await asyncio.open_connection("example.invalid", 443)

    try:
        with NetGuard([port]) as guard:
            loop = loop_class()
            try:
                loop.run_until_complete(main())
            finally:
                loop.close()
        layers = [item.where for item in guard.blocked]
        assert "asyncio.sock_connect" in layers
        assert "getaddrinfo" in layers
        assert all(item.host != "127.0.0.1" for item in guard.blocked)
    finally:
        server.close()
