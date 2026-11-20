"""javmem regress 用的测试图：照 compose 起一个只给回归用的 Neo4j，数据目录单独一个，用完停掉、挂回原来的目录。

- **拿锁：** 套件写了锁文件的（本机几个会话共用一个测试图，`两个图怎么轮着用` 那份约定），先建它（已经有了就不跑，
  说一句“测试图被占着，先做别的，或者加 --no-graph”），写上谁、什么时候、做什么、估多久，再写一个这一遍自己的编号；
  用完删掉：只删写着自己那个编号的（中途被别人换掉的不删）。
- **不碰别人的图：** 容器正开着、挂的又不是回归的目录（有人在用，或者用完忘了停），不去停它，一个 compose 命令都不跑。
  拿不到锁、还没起图就跑不了的，也一个 compose 命令都不跑：只有自己真的停过容器（`touched`），最后才停、才去还原。
- **不碰别人的数据：** 回归用的数据目录每一遍都清空重来，所以只认两种：不存在（或者是空的），或者里面有回归自己放的标记文件
  `.javmem-regress-graph`。别的目录（比如放着放大图的那个）一个字不删，直接不跑。和容器原来挂的是同一个目录的，也不跑。
- **起、停：** `docker compose down`（先记下容器原来挂的是哪个目录），清空回归的目录，照 compose 起（数据目录换成回归的），
  等日志里出现 `Started.`，核一遍挂上的确实是回归的目录；用完 `down`，原来有容器的照原来的目录 `up --no-start`（建好、不起），
  下一个用的人照旧起他的图。原来挂的认不出是哪个本机目录的（比如 Docker Desktop 的 WSL 挂载），不还原，照实说一句。
- 起 docker 的子进程，环境里去掉所有 `JAVMEM_*`（只给 compose 要的那个数据目录）：付钱的密钥、图的密码都不给。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MARKER = ".javmem-regress-graph"
DATA_ENV = "JAVMEM_GRAPH_TEST_NEO4J_DATA"
START_SECONDS = 300
_DESKTOP = "/run/desktop/mnt/host/"
_DRIVE = re.compile(r"([a-zA-Z])(/.*)?$")  # /run/desktop/mnt/host/ 后面一个字母就是盘符（e/…）；wsl/… 这种不是


class GraphBusy(Exception):
    """测试图被别人占着（锁文件在）。"""


class GraphRefused(Exception):
    """不敢用：容器正被别人用着、数据目录不是回归自己的、挂的不对、起不来。"""


def _host_dir(source: str) -> Path | None:
    """容器挂载的来源 → 本机的目录。Docker Desktop 写成 /run/desktop/mnt/host/q/… 的换成 Q:/…；
    /run/desktop/mnt/host/ 后面不是盘符的（WSL 的挂载）认不出，回 None。别的照原样。"""
    text = source.replace("\\", "/")
    if text.lower().startswith(_DESKTOP):
        found = _DRIVE.match(text[len(_DESKTOP) :])
        if found is None:
            return None
        return Path(found.group(1).upper() + ":" + (found.group(2) or "/"))
    return Path(text)


def _same_dir(source: str, want: Path) -> bool:
    found = _host_dir(source)
    if found is None:
        return False
    return str(found).replace("\\", "/").lower().rstrip("/") == str(want).replace("\\", "/").lower().rstrip("/")


def _env(data: Path | None = None) -> dict[str, str]:
    """docker 子进程的环境：去掉所有 JAVMEM_*；compose 文件里数据目录那个变量是必填的，给上。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("JAVMEM_")}
    if data is not None:
        env[DATA_ENV] = str(data)
    return env


@dataclass
class ManagedGraph:
    compose: Path
    data: Path
    container: str
    lock: Path | None = None
    who: str = "javmem regress"
    log: object = print
    original: Path | None = None  # 容器原来挂的数据目录（data、logs 的上一层）
    had_container: bool = False
    touched: bool = False  # 自己停过容器：最后才要停、还原
    _locked: bool = False
    _token: str = ""

    def _say(self, text: str) -> None:
        self.log(text)  # type: ignore[operator]

    def _run(self, *args: str, data: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", "compose", "-f", str(self.compose), *args], env=_env(data or self.data),
                              capture_output=True, encoding="utf-8", errors="replace", check=check, timeout=600)

    def _docker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *args], env=_env(), capture_output=True, encoding="utf-8", errors="replace")

    def mounts(self) -> list[str]:
        out = self._docker("inspect", "-f", "{{json .Mounts}}", self.container)
        if out.returncode:
            return []
        return [str(one.get("Source", "")) for one in json.loads(out.stdout or "[]")]

    def running(self) -> bool:
        """容器正开着（没有这个容器的算没开）。"""
        out = self._docker("inspect", "-f", "{{.State.Running}}", self.container)
        return out.returncode == 0 and out.stdout.strip().lower() == "true"

    def logs(self) -> str:
        out = self._docker("logs", self.container)
        return (out.stdout or "") + (out.stderr or "")

    # ---------- 锁 ----------

    def take_lock(self, minutes: int) -> None:
        if self.lock is None:
            return
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        try:
            with self.lock.open("x", encoding="utf-8") as handle:
                handle.write(f"会话：{self.who}\n开始：{datetime.now():%Y-%m-%d %H:%M}\n"
                             f"做什么：javmem regress，测试图挂回归自己的目录（{self.data}），用完停掉、挂回原来的目录\n"
                             f"估多久：{minutes} 分钟\n锁的编号：{token}\n")
        except FileExistsError:
            raise GraphBusy(f"测试图被占着（{self.lock} 在）：先做别的，过一会儿再跑；或者加 --no-graph 不量主图那一路") from None
        self._locked, self._token = True, token

    def drop_lock(self) -> None:
        """只删写着这一遍自己那个编号的锁。"""
        if self._locked and self.lock is not None and self.lock.exists():
            if f"锁的编号：{self._token}" in self.lock.read_text(encoding="utf-8"):
                self.lock.unlink()
        self._locked = False

    # ---------- 起、停 ----------

    def _check_data_dir(self) -> None:
        if self.data.exists() and any(self.data.iterdir()) and not (self.data / MARKER).exists():
            raise GraphRefused(f"{self.data} 不是空的，也不是回归自己的目录（没有 {MARKER}）：不清它，不跑")
        if self.original is not None and self.original.resolve() == self.data.resolve():
            raise GraphRefused("回归的目录和测试图原来挂的是同一个：不清它，不跑")

    def up(self) -> float:
        """清空回归的目录，起图，等起好；返回几秒。容器正被别人用着、目录不是自己的：一个 compose 命令都不跑就退。"""
        sources = self.mounts()
        ours = False
        if sources:
            self.had_container = True
            data_mount = next((one for one in sources if one.replace("\\", "/").rstrip("/").lower().endswith("/data")), None)
            ours = data_mount is not None and _same_dir(data_mount, self.data / "data")
            if data_mount is not None and not ours and self.original is None:  # 上一遍回归没挂回去的，不算原来的
                found = _host_dir(data_mount)
                self.original = None if found is None else found.parent
        if sources and not ours and self.running():
            self.touched = False  # 那个容器是别人的了（上一条线起的那个早被换掉）：最后也不去停它
            raise GraphRefused("测试图正开着、挂的不是回归的目录（有人在用，或者用完忘了停）：不去停它。先问是谁开的、停了再跑，"
                               "或者加 --no-graph")
        self._check_data_dir()
        self.touched = True
        self._run("down")
        for part in ("data", "logs"):
            if (self.data / part).exists():
                shutil.rmtree(self.data / part)
        (self.data / "data").mkdir(parents=True, exist_ok=True)
        (self.data / "logs").mkdir(parents=True, exist_ok=True)
        (self.data / MARKER).write_text("javmem regress 的测试图目录：每一遍都清空重来\n", encoding="utf-8")
        started = time.monotonic()
        self._run("up", "-d", data=self.data)
        while time.monotonic() - started < START_SECONDS:
            if "Started." in self.logs():
                break
            time.sleep(2)
        else:
            raise GraphRefused(f"测试图 {START_SECONDS} 秒没起来")
        mounted = self.mounts()
        if not all(any(_same_dir(source, self.data / part) for source in mounted) for part in ("data", "logs")):
            raise GraphRefused(f"挂上的目录不对：{mounted}")
        return time.monotonic() - started

    def down(self) -> None:
        """停掉回归的图；原来有容器的，照原来的目录建好（不起）。没动过容器的（没起成、拿不到锁、别人在用）什么都不做。"""
        if not self.touched:
            return
        self._run("down", check=False)
        if not self.had_container:
            self._say("测试图停了")
        elif self.original is None:
            self._say("测试图停了；原来挂的目录认不出是本机哪个（或者上一遍回归没挂回去），没还原：要用原来的图，照它的说明重新起")
        elif self._run("up", "--no-start", data=self.original, check=False).returncode == 0:
            self._say(f"测试图停了，容器照原来的目录建好了、没起（{self.original}）")
        else:
            self._say(f"测试图停了，照原来的目录（{self.original}）重建容器没建成：要用原来的图，照它的说明重新起")
