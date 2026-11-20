"""只许连本机那几个端口的闸：支线的命令一开始就装上，Graphiti、Neo4j 的驱动、向量和 Qwen 的客户端往别处连都拒绝。

- 同步的连接（socket.connect、connect_ex、sendto）和 asyncio 的连接（两种事件循环的 sock_connect、sock_sendto）都拦：
  Graphiti、Neo4j 的驱动是 asyncio 的，Windows 上 asyncio 连接不走 socket.connect（走系统的 ConnectEx），只拦一处会漏。
- 查域名也拦：getaddrinfo、gethostbyname、getnameinfo 这些只放过本机的名字和地址。
- 本进程自己 listen 的本机端口也放过：Windows 上 socket.socketpair 是自己连自己的一个临时端口（asyncio 的事件循环要用）。
- 拒绝的记一行：时间、地址、端口、在哪一层拦的；不记内容。拒绝时抛 NetBlocked（是 OSError，库会当连不上处理）。

闸是防的；验靠系统那一层（Get-NetTCPConnection 看这个进程连过哪里），两边对得上才算过。
"""

from __future__ import annotations

import asyncio
import asyncio.proactor_events
import asyncio.selector_events
import errno
import ipaddress
import socket
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_LOCAL_NAMES = frozenset({"localhost"})


class NetBlocked(OSError):
    """闸拒绝了这次连接。"""

    def __init__(self, host: object, port: object, where: str) -> None:
        super().__init__(errno.EACCES, f"支线只许连本机的服务，拒绝了 {host}:{port}（{where}）")
        self.host, self.port, self.where = host, port, where


@dataclass(frozen=True)
class Blocked:
    at: str
    host: str
    port: int | None
    where: str


def _is_local_host(host: object) -> bool:
    if host is None or host == "":
        return True  # 绑本机、没给主机名
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


class NetGuard:
    """allowed：放过的 (本机地址, 端口)，只看端口（地址必须是本机的）。"""

    def __init__(self, ports: Iterable[int], *, on_block: Callable[[Blocked], None] | None = None) -> None:
        self.ports = frozenset(int(port) for port in ports)
        self.blocked: list[Blocked] = []
        self.on_block = on_block
        self._own: set[int] = set()  # 本进程自己 listen 的本机端口
        self._lock = threading.Lock()
        self._saved: list[tuple[object, str, object]] = []

    # ---------- 判 ----------

    def _record(self, host: object, port: object, where: str) -> NetBlocked:
        item = Blocked(datetime.now(UTC).isoformat(timespec="seconds"), str(host), port if isinstance(port, int) else None, where)
        with self._lock:
            self.blocked.append(item)
        if self.on_block is not None:
            try:
                self.on_block(item)
            except Exception:  # noqa: BLE001 - 记不下来也照样拒绝
                pass
        return NetBlocked(host, port, where)

    def check_address(self, address: object, where: str) -> None:
        if not isinstance(address, tuple) or len(address) < 2:  # AF_UNIX 这类：Windows 上不会有，一律拒绝
            raise self._record(address, None, where)
        host, port = address[0], address[1]
        if not _is_local_host(host) or not isinstance(port, int):
            raise self._record(host, port, where)
        with self._lock:
            mine = port in self._own
        if port not in self.ports and not mine:
            raise self._record(host, port, where)

    def check_name(self, host: object, where: str) -> None:
        if not _is_local_host(host):
            raise self._record(host, None, where)

    # ---------- 装、拆 ----------

    def _patch(self, owner: object, name: str, make: Callable[[Any], Any]) -> None:
        original = getattr(owner, name)
        self._saved.append((owner, name, original))
        setattr(owner, name, make(original))

    def install(self) -> NetGuard:
        if self._saved:
            return self
        guard = self

        def wrap_connect(original: Any) -> Any:
            def connect(sock: socket.socket, address: Any) -> Any:
                if sock.family in (socket.AF_INET, socket.AF_INET6):
                    guard.check_address(address, "socket.connect")
                return original(sock, address)

            return connect

        def wrap_connect_ex(original: Any) -> Any:
            def connect_ex(sock: socket.socket, address: Any) -> Any:
                if sock.family in (socket.AF_INET, socket.AF_INET6):
                    guard.check_address(address, "socket.connect_ex")
                return original(sock, address)

            return connect_ex

        def wrap_sendto(original: Any) -> Any:
            def sendto(sock: socket.socket, data: Any, *args: Any) -> Any:
                address = args[-1] if args else None
                if sock.family in (socket.AF_INET, socket.AF_INET6) and address is not None:
                    guard.check_address(address, "socket.sendto")
                return original(sock, data, *args)

            return sendto

        def wrap_listen(original: Any) -> Any:
            def listen(sock: socket.socket, *args: Any) -> Any:
                result = original(sock, *args)
                try:
                    host, port = sock.getsockname()[:2]
                except OSError:
                    return result
                if _is_local_host(host):
                    with guard._lock:
                        guard._own.add(port)
                return result

            return listen

        def wrap_getaddrinfo(original: Any) -> Any:
            def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
                guard.check_name(host, "getaddrinfo")
                return original(host, port, *args, **kwargs)

            return getaddrinfo

        def wrap_by_name(original: Any, where: str) -> Any:
            def lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
                guard.check_name(host, where)
                return original(host, *args, **kwargs)

            return lookup

        def wrap_getnameinfo(original: Any) -> Any:
            def getnameinfo(sockaddr: Any, *args: Any, **kwargs: Any) -> Any:
                host = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else sockaddr
                guard.check_name(host, "getnameinfo")
                return original(sockaddr, *args, **kwargs)

            return getnameinfo

        def wrap_sock_connect(original: Any) -> Any:
            async def sock_connect(loop: Any, sock: socket.socket, address: Any) -> Any:
                if sock.family in (socket.AF_INET, socket.AF_INET6):
                    guard.check_address(address, "asyncio.sock_connect")
                return await original(loop, sock, address)

            return sock_connect

        def wrap_sock_sendto(original: Any) -> Any:
            async def sock_sendto(loop: Any, sock: socket.socket, data: Any, address: Any) -> Any:
                if sock.family in (socket.AF_INET, socket.AF_INET6):
                    guard.check_address(address, "asyncio.sock_sendto")
                return await original(loop, sock, data, address)

            return sock_sendto

        self._patch(socket.socket, "connect", wrap_connect)
        self._patch(socket.socket, "connect_ex", wrap_connect_ex)
        self._patch(socket.socket, "sendto", wrap_sendto)
        self._patch(socket.socket, "listen", wrap_listen)
        self._patch(socket, "getaddrinfo", wrap_getaddrinfo)
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            self._patch(socket, name, lambda original, where=name: wrap_by_name(original, where))
        self._patch(socket, "getnameinfo", wrap_getnameinfo)
        loops: list[type] = [asyncio.selector_events.BaseSelectorEventLoop, asyncio.proactor_events.BaseProactorEventLoop]
        for loop_class in loops:
            if "sock_connect" in vars(loop_class):
                self._patch(loop_class, "sock_connect", wrap_sock_connect)
            if "sock_sendto" in vars(loop_class):  # Proactor 的 UDP 也不走 socket.sendto
                self._patch(loop_class, "sock_sendto", wrap_sock_sendto)
        return self

    def uninstall(self) -> None:
        while self._saved:
            owner, name, original = self._saved.pop()
            setattr(owner, name, original)

    def __enter__(self) -> NetGuard:
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.uninstall()
