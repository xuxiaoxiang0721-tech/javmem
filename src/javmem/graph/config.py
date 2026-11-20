"""主图的设置：环境变量 JAVMEM_GRAPH_*（J5.1）。地址都只许本机。

- JAVMEM_GRAPH_NEO4J_URI：主图（正式库的账本投影到这里），默认 bolt://127.0.0.1:7688（deploy/graph-neo4j/compose.yaml）。
- JAVMEM_GRAPH_NEO4J_USER：默认 neo4j。
- JAVMEM_GRAPH_NEO4J_PASSWORD：主图的密码，只从环境变量读，不打印、不写文件。
- JAVMEM_GRAPH_TEST_NEO4J_URI、JAVMEM_GRAPH_TEST_NEO4J_USER、JAVMEM_GRAPH_TEST_NEO4J_PASSWORD：测试图（测试库的账本投影到
  这里，D37 单独一个实例），默认 bolt://127.0.0.1:7689（deploy/graph-test-neo4j/compose.yaml）。
- JAVMEM_GRAPH_SYNC：写成 off，javmem write 写完账不接着同步（欠着的照样记，下次 javmem graph sync 补）。默认 on。
- JAVMEM_GRAPH_TIMEOUT_SECONDS：写账以后接着同步时，连接、取连接最多等几秒（图卡着不回话也不让写账卡住），默认 5。
  查询接口查主图（J5.4）连接、取连接也最多等这么久。
- JAVMEM_GRAPH_SEARCH：写成 off，memory_search 不查主图（比较时、出事时用；只用账本视图，degraded 写 graph）。默认 on。
  写错了当 off、打一行提醒，不报配置错误（不连累写账以后的同步）。

没设密码就当这个库的图没配：写账以后不去连，只记欠着（测试、CI、核分支时都是这样）。

防串库在设置这一层先拦一道：主图和测试图不能是同一个地址，哪个都不能是体检库的地址（JAVMEM_SIDE_NEO4J_URI，没设就是
它的默认 bolt://127.0.0.1:7690）。都在本机，所以比端口。连上以后再核图里的标记（javmem.graph.neo）。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from javmem.config import PROD, TEST, ConfigError
from javmem.llm import _is_loopback

ENV_URI = "JAVMEM_GRAPH_NEO4J_URI"
ENV_USER = "JAVMEM_GRAPH_NEO4J_USER"
ENV_PASSWORD = "JAVMEM_GRAPH_NEO4J_PASSWORD"
ENV_TEST_URI = "JAVMEM_GRAPH_TEST_NEO4J_URI"
ENV_TEST_USER = "JAVMEM_GRAPH_TEST_NEO4J_USER"
ENV_TEST_PASSWORD = "JAVMEM_GRAPH_TEST_NEO4J_PASSWORD"
ENV_SYNC = "JAVMEM_GRAPH_SYNC"
ENV_TIMEOUT = "JAVMEM_GRAPH_TIMEOUT_SECONDS"
ENV_SEARCH = "JAVMEM_GRAPH_SEARCH"
ENV_SIDE_URI = "JAVMEM_SIDE_NEO4J_URI"  # 体检库的地址（javmem.side.config），这里只拿来比，不连

DEFAULT_URI = "bolt://127.0.0.1:7688"
DEFAULT_TEST_URI = "bolt://127.0.0.1:7689"
DEFAULT_SIDE_URI = "bolt://127.0.0.1:7690"
DEFAULT_USER = "neo4j"
DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 600.0

_USER = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_PASSWORD = re.compile(r"[\x21-\x7e]{8,256}")


def search_switch(env: Mapping[str, str] | None = None) -> tuple[bool, str | None]:
    """JAVMEM_GRAPH_SEARCH：(查不查主图, 写法不对时的一句提醒)。写错了当 off（宁可不查主图），也不报配置错误：
    不连累写账以后的同步、javmem graph 命令（J5.4 审阅第 4 条）。"""
    env = os.environ if env is None else env
    text = env.get(ENV_SEARCH, "").strip().lower()
    if text in ("", "on", "1", "true", "yes"):
        return True, None
    if text in ("off", "0", "false", "no"):
        return False, None
    return False, f"{ENV_SEARCH} 要写成 on 或 off，这次当 off"


def _port(value: str, name: str) -> int:
    """地址要写成 bolt://127.0.0.1:端口：只许本机、只许 bolt（neo4j:// 会照路由表去连别的机器），不许带账号、参数。
    返回端口。写错了报配置错误，消息里只有变量名。"""
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ConfigError(f"{name} 写法不对") from None
    if parts.scheme != "bolt" or not host or port is None:
        raise ConfigError(f"{name} 要写成 bolt://127.0.0.1:端口 这样的地址")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ConfigError(f"{name} 里不能带用户名、密码、路径、? 后面的参数和 #")
    if not _is_loopback(host):
        raise ConfigError(f"{name} 只能是本机的地址")
    return port


@dataclass(frozen=True)
class GraphTarget:
    """一个库的图：哪个库、连哪、用哪个账号。密码不进 repr，不打印。"""

    library: str
    uri: str
    user: str
    password: str = field(default="", repr=False)
    password_name: str = ENV_PASSWORD

    def require_password(self) -> str:
        if not self.password:
            raise ConfigError(f"没有设置 {self.password_name}（这个库的图的密码，只放在用户级环境变量里）")
        return self.password

    def describe(self) -> dict[str, object]:
        """不是密钥的设置，可以打印、写进报告。"""
        return {"library": self.library, "uri": self.uri, "user": self.user, "password_set": bool(self.password)}


@dataclass(frozen=True)
class GraphSettings:
    prod: GraphTarget = field(default_factory=lambda: GraphTarget(PROD, DEFAULT_URI, DEFAULT_USER))
    test: GraphTarget = field(
        default_factory=lambda: GraphTarget(TEST, DEFAULT_TEST_URI, DEFAULT_USER, password_name=ENV_TEST_PASSWORD)
    )
    sync: bool = True
    timeout: float = DEFAULT_TIMEOUT
    search: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, check_sync: bool = True) -> GraphSettings:
        """check_sync=False：不核 JAVMEM_GRAPH_SYNC 的写法（查询那一边用：写账的开关写错了不连累查主图，J5.4 审阅第 4 条）。"""
        env = os.environ if env is None else env
        targets = {}
        ports = {}
        for library, uri_name, user_name, password_name, default in (
            (PROD, ENV_URI, ENV_USER, ENV_PASSWORD, DEFAULT_URI),
            (TEST, ENV_TEST_URI, ENV_TEST_USER, ENV_TEST_PASSWORD, DEFAULT_TEST_URI),
        ):
            uri = env.get(uri_name, "").strip() or default
            ports[library] = _port(uri, uri_name)
            user = env.get(user_name, "").strip() or DEFAULT_USER
            if not _USER.fullmatch(user):
                raise ConfigError(f"{user_name} 写法不对")
            password = env.get(password_name, "")
            if password and not _PASSWORD.fullmatch(password):
                raise ConfigError(f"{password_name} 要是 8 到 256 个可见的英文字母、数字和符号")
            targets[library] = GraphTarget(library, uri, user, password, password_name)
        if ports[PROD] == ports[TEST]:
            raise ConfigError(f"{ENV_URI} 和 {ENV_TEST_URI} 不能是同一个图（正式库和测试库互不相通，D37）")
        side_text = env.get(ENV_SIDE_URI, "").strip() or DEFAULT_SIDE_URI
        try:
            side_port = urlsplit(side_text).port
        except ValueError:
            side_port = None
        for library, name in ((PROD, ENV_URI), (TEST, ENV_TEST_URI)):
            if side_port is not None and ports[library] == side_port:
                raise ConfigError(f"{name} 是体检库的地址：主图、测试图不能连体检库")
        sync_text = env.get(ENV_SYNC, "").strip().lower() if check_sync else ""
        if sync_text not in ("", "on", "off", "1", "0", "true", "false", "yes", "no"):
            raise ConfigError(f"{ENV_SYNC} 要写成 on 或 off")
        timeout_text = env.get(ENV_TIMEOUT, "").strip()
        try:
            timeout = float(timeout_text) if timeout_text else DEFAULT_TIMEOUT
        except ValueError:
            raise ConfigError(f"{ENV_TIMEOUT} 要写成秒数") from None
        if not math.isfinite(timeout) or not 1 <= timeout <= MAX_TIMEOUT:
            raise ConfigError(f"{ENV_TIMEOUT} 要在 1 到 {MAX_TIMEOUT:.0f} 秒之间")
        return cls(targets[PROD], targets[TEST], sync_text not in ("off", "0", "false", "no"), timeout, search_switch(env)[0])

    def target(self, library: str) -> GraphTarget:
        if library == PROD:
            return self.prod
        if library == TEST:
            return self.test
        raise ValueError(f"没有这个库：{library}")

    def describe(self) -> dict[str, object]:
        return {"prod": self.prod.describe(), "test": self.test.describe(), "sync": self.sync, "timeout": self.timeout,
                "search": self.search}
