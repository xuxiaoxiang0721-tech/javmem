"""许可证（D26：MIT；U3）：仓库里有 MIT 的 LICENSE，pyproject 照它写，装出来的包里带着它；装着的依赖的许可证
都是能和 MIT 一起用的那一类，docs/licenses.md 列着锁文件里的每个包。"""
from __future__ import annotations

import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PUBLIC_REPO = "https://github.com/xuxiaoxiang0721-tech/javmem"  # 公开库（U4，D235）
# 能一起用的（宽松的）许可证、碰到就红的（传染的、限制用法的）。一个包写了几样（“A OR B”）的，有一样不行就红
OK = re.compile(r"\b(?:MIT(?:-0)?|BSD(?:-[23]-Clause)?|Apache(?:-2\.0)?|PSF-2\.0|Python-2\.0|ISC|MPL-2\.0|0BSD|Zlib)\b"
                r"|BSD License|MIT License|Apache Software License|Python Software Foundation License", re.IGNORECASE)
BAD = re.compile(r"\b(?:A?GPL|LGPL|SSPL|BUSL|EUPL|CC-BY-NC)\b|General Public License|Commons Clause", re.IGNORECASE)
LOCKS = ("main.txt", "dev.txt", "claude.txt", "side.txt")


def locked(lock: str) -> list[tuple[str, str, str]]:
    """锁文件里的 (包名, 版本, 只在某些系统上装的条件)。"""
    found = []
    for line in (REPO / "requirements" / lock).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9._-]+)==([^\s;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?$", line)
        if match:
            found.append((match.group(1), match.group(2), (match.group(3) or "").strip()))
    return found


def license_of(name: str) -> str:
    meta = metadata.metadata(name)
    pieces = [meta.get("License-Expression") or "", (meta.get("License") or "").strip().splitlines()[0][:200] if meta.get("License") else ""]
    pieces += [line for line in meta.get_all("Classifier") or [] if line.startswith("License ::")]
    return " | ".join(piece for piece in pieces if piece)


@pytest.mark.parametrize("lock", ["main.txt", "dev.txt"])
def test_every_installed_locked_package_has_a_permissive_license(lock: str) -> None:
    """跑测试的环境照 dev.txt 装（CI、本机都是）：运行时、开发用的包一个个读包自带的许可信息。"""
    from packaging.markers import Marker

    checked = 0
    for name, version, marker in locked(lock):
        if marker and not Marker(marker).evaluate():
            continue  # 这个系统上不装的（比如只在 Windows 上装的 colorama）
        assert metadata.version(name) == version, f"{name}：装的不是锁着的版本，照锁文件重装"
        text = license_of(name)
        assert not BAD.search(text), f"{name}：{text}"
        assert OK.search(text), f"{name}：认不出许可证（{text}），核过以后改 OK 或者 docs/licenses.md"
        checked += 1
    assert checked >= 10


def test_the_license_page_lists_every_locked_package() -> None:
    page = (REPO / "docs" / "licenses.md").read_text(encoding="utf-8")
    missing = sorted({name for lock in LOCKS for name, _, _ in locked(lock) if f"`{name}`" not in page})
    assert missing == []


def test_the_license_page_says_how_neo4j_is_used() -> None:
    page = (REPO / "docs" / "licenses.md").read_text(encoding="utf-8")
    for words in ("社区版", "GPLv3", "不带 Neo4j 的代码", "别发布把 Neo4j 打包进去的镜像"):
        assert words in page, words


def test_the_checks_would_catch_a_bad_license() -> None:
    for text in ("GPL-3.0-only", "GNU General Public License v3 (GPLv3)", "AGPL-3.0", "LGPL-2.1-or-later", "MIT OR GPL-2.0"):
        assert BAD.search(text), text
    for text in ("MIT", "Apache-2.0 OR BSD-3-Clause", "License :: OSI Approved :: BSD License", "PSF-2.0", "MIT-0"):
        assert OK.search(text) and not BAD.search(text), text


def test_the_license_file_is_mit() -> None:
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n\nCopyright (c) ")
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in text
    assert "\r" not in text  # 仓库里一律 LF


def test_pyproject_says_mit_and_ships_the_file() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["license"] == "MIT"
    assert project["project"]["license-files"] == ["LICENSE"]
    # license 写成 SPDX 的一个词（PEP 639）要 setuptools 77 以上；锁着的那个版本要在这个范围里（test_pins 也核）
    assert any(spec.replace(" ", "").startswith("setuptools>=77") for spec in project["build-system"]["requires"])
    # 许可证的写法只用 license，不再写 License :: 那种分类（两样一起写，新的 setuptools 不让装）
    assert not any(c.startswith("License ::") for c in project["project"].get("classifiers", []))


def test_pyproject_points_at_the_public_repo() -> None:
    """包的说明里的地址是公开库（U4，D235）：私有的开发库改名以后，这里照旧指着公开的那一个。"""
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    urls = project["project"]["urls"]
    assert urls == {"Homepage": PUBLIC_REPO, "Repository": PUBLIC_REPO, "Issues": f"{PUBLIC_REPO}/issues"}
