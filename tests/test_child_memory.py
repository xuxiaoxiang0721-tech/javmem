"""读附件的子进程在 Windows 上也限内存（D79 第 7 条、D93 第 16 条；U3 卡 8）。

Linux、macOS 上子进程自己设 RLIMIT_AS（attachments.limit_child）；Windows 上没有，以前只能写进部署说明。现在起来以后、
喂文件以前把它放进一个作业对象，每个进程最多用这么多内存：做坏的压缩文件（几 KB 解出几 GB）吃不光内存，分配不出来
就是子进程出错，调用方照旧记成 unreadable。

这里不造真的压缩炸弹：起一个子进程（`python -c`，只分配内存），限得小一点，看它分配不出来；限得大一点照样能跑完。
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from pdfgen import make_pdf

from javmem import attachments

ALLOCATE = "import sys; data = bytearray(200 * 1024 * 1024); sys.stdout.write(str(len(data)))"


def test_run_child_reads_stdin_and_returns_like_subprocess_run() -> None:
    done = attachments.run_child([sys.executable, "-I", "-c", "import sys; sys.stdout.write(sys.stdin.read()[::-1])"],
                                 b"abc", timeout=60)
    assert done.returncode == 0 and done.stdout == b"cba"


def test_run_child_kills_it_on_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        attachments.run_child([sys.executable, "-I", "-c", "import time; time.sleep(30)"], b"", timeout=1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 才用作业对象；别的系统子进程自己设 RLIMIT_AS")
def test_on_windows_a_child_over_the_limit_cannot_get_the_memory() -> None:
    small = attachments.run_child([sys.executable, "-I", "-c", ALLOCATE], b"", timeout=60, memory=64 * 1024**2)
    assert small.returncode != 0 and b"MemoryError" in small.stderr
    big = attachments.run_child([sys.executable, "-I", "-c", ALLOCATE], b"", timeout=60, memory=1024**3)
    assert big.returncode == 0 and big.stdout == str(200 * 1024 * 1024).encode()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 才用作业对象")
def test_the_real_reader_child_runs_inside_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """读附件用的就是这一套：限额照 _CHILD_MEMORY（2 GiB），平常的文件照样读得出来。"""
    seen: list[int] = []
    real = attachments._windows_job

    def spy(pid: int, memory: int) -> object:
        seen.append(memory)
        return real(pid, memory)

    monkeypatch.setattr(attachments, "_windows_job", spy)
    found = attachments._pdf_text_in_child(make_pdf(["Unit price 9.8 USD."]), 1000, 30.0)
    assert seen == [attachments._CHILD_MEMORY]
    assert isinstance(found, tuple) and "Unit price 9.8 USD." in found[0]  # 编的 PDF 照样读得出来
