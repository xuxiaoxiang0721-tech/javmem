"""支线的设置：环境变量 JAVMEM_SIDE_*。地址都只许本机（D156：数据不出这台电脑）。

- JAVMEM_SIDE_NEO4J_URI：体检库，默认 bolt://127.0.0.1:7690（deploy/side-neo4j/compose.yaml 起的那个）。
- JAVMEM_SIDE_NEO4J_USER：默认 neo4j。
- JAVMEM_SIDE_NEO4J_PASSWORD：体检库的密码，只从环境变量读，不打印、不写文件。真跑才要。
- JAVMEM_SIDE_QWEN_URL：本机 Qwen 的 OpenAI 兼容接口（…/v1/chat/completions），真跑才要。
- JAVMEM_SIDE_QWEN_MODEL：记账写的模型名，默认 qwen3.8-27b。
- JAVMEM_SIDE_LANES：同时最多几个请求发给 Qwen，1 或 2，默认 2（本机 Qwen 只有两路，别人要用时降到 1）。
- 向量服务用现成的 JAVMEM_EMBEDDING_*（javmem.embedding），也只许本机。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from javmem.config import ConfigError
from javmem.llm import _is_loopback

ENV_NEO4J_URI = "JAVMEM_SIDE_NEO4J_URI"
ENV_NEO4J_USER = "JAVMEM_SIDE_NEO4J_USER"
ENV_NEO4J_PASSWORD = "JAVMEM_SIDE_NEO4J_PASSWORD"
ENV_QWEN_URL = "JAVMEM_SIDE_QWEN_URL"
ENV_QWEN_MODEL = "JAVMEM_SIDE_QWEN_MODEL"
ENV_LANES = "JAVMEM_SIDE_LANES"

DEFAULT_NEO4J_URI = "bolt://127.0.0.1:7690"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_QWEN_MODEL = "qwen3.8-27b"
DEFAULT_LANES = 2
MAX_LANES = 2


def _endpoint(value: str, name: str, schemes: tuple[str, ...]) -> tuple[str, int]:
    """地址要是本机的：返回 (主机, 端口)。写错了报配置错误，消息里只有变量名。"""
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ConfigError(f"{name} 写法不对") from None
    if parts.scheme not in schemes or not host or port is None:
        raise ConfigError(f"{name} 要写成 {schemes[0]}://127.0.0.1:端口 这样的地址")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(f"{name} 里不能带用户名、密码、? 后面的参数和 #")
    if not _is_loopback(host):
        raise ConfigError(f"{name} 只能是本机的地址（支线的数据不出这台电脑）")
    return host, port


@dataclass(frozen=True)
class SideSettings:
    neo4j_uri: str = DEFAULT_NEO4J_URI
    neo4j_user: str = DEFAULT_NEO4J_USER
    neo4j_password: str = field(default="", repr=False)
    qwen_url: str = ""
    qwen_model: str = DEFAULT_QWEN_MODEL
    lanes: int = DEFAULT_LANES

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SideSettings:
        env = os.environ if env is None else env
        uri = env.get(ENV_NEO4J_URI, "").strip() or DEFAULT_NEO4J_URI
        _endpoint(uri, ENV_NEO4J_URI, ("bolt", "neo4j"))
        if urlsplit(uri).scheme == "neo4j":  # neo4j:// 会按路由表去连别的机器
            raise ConfigError(f"{ENV_NEO4J_URI} 要用 bolt://（单机的体检库）")
        user = env.get(ENV_NEO4J_USER, "").strip() or DEFAULT_NEO4J_USER
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user):
            raise ConfigError(f"{ENV_NEO4J_USER} 写法不对")
        password = env.get(ENV_NEO4J_PASSWORD, "")
        if password and not re.fullmatch(r"[\x21-\x7e]{8,256}", password):
            raise ConfigError(f"{ENV_NEO4J_PASSWORD} 要是 8 个以上可见的英文字母、数字和符号")
        qwen_url = env.get(ENV_QWEN_URL, "").strip()
        if qwen_url:
            _endpoint(qwen_url, ENV_QWEN_URL, ("http",))
        model = env.get(ENV_QWEN_MODEL, "").strip() or DEFAULT_QWEN_MODEL
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
            raise ConfigError(f"{ENV_QWEN_MODEL} 写法不对")
        lanes_text = env.get(ENV_LANES, "").strip()
        try:
            lanes = int(lanes_text) if lanes_text else DEFAULT_LANES
        except ValueError:
            raise ConfigError(f"{ENV_LANES} 要写成 1 或 2") from None
        if not 1 <= lanes <= MAX_LANES:
            raise ConfigError(f"{ENV_LANES} 要写成 1 或 2（本机 Qwen 只有两路）")
        return cls(uri, user, password, qwen_url, model, lanes)

    def neo4j_endpoint(self) -> tuple[str, int]:
        return _endpoint(self.neo4j_uri, ENV_NEO4J_URI, ("bolt",))

    def qwen_endpoint(self) -> tuple[str, int]:
        if not self.qwen_url:
            raise ConfigError(f"没有设置 {ENV_QWEN_URL}（本机 Qwen 的地址）")
        return _endpoint(self.qwen_url, ENV_QWEN_URL, ("http",))

    def require_password(self) -> str:
        if not self.neo4j_password:
            raise ConfigError(f"没有设置 {ENV_NEO4J_PASSWORD}（体检库的密码，只放在用户级环境变量里）")
        return self.neo4j_password

    def describe(self) -> dict[str, object]:
        """不是密钥的设置，可以打印、写进报告。"""
        return {
            "neo4j_uri": self.neo4j_uri,
            "neo4j_user": self.neo4j_user,
            "neo4j_password_set": bool(self.neo4j_password),
            "qwen_url": self.qwen_url,
            "qwen_model": self.qwen_model,
            "lanes": self.lanes,
        }


def embedding_endpoint(base_url: str) -> tuple[str, int]:
    """向量服务也只许本机：JAVMEM_EMBEDDING_BASE_URL 不是本机的，支线不用。"""
    return _endpoint(base_url, "JAVMEM_EMBEDDING_BASE_URL", ("http",))
