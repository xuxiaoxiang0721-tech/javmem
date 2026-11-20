"""锁定版本（U2 甲，D205 ⑩）：CI 里的 Actions 钉提交号，Neo4j 镜像四处（三份 compose、CI）钉同一个指纹。

只读仓库里的文件，不连网。升级照“一次只改一项”：改提交号（和后面写着是哪一版的注释）、改指纹（四处一起改），
推一份到 ci/ 开头的分支看 CI；Neo4j 换了版本，主图要整张重建、再跑一遍检索的回归（javmem regress）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github" / "workflows" / "ci.yml"
COMPOSE = sorted((REPO / "deploy").glob("*/compose.yaml"))
_USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.MULTILINE)
# CI 里 docker run 的镜像：[仓库/…/]neo4j:标签[@sha256:指纹]，前后是空白
_CI_IMAGE = re.compile(r"(?<!\S)((?:[\w.-]+/)*neo4j:[\w.-]+(?:@sha256:[0-9a-f]{64})?)(?!\S)")


def test_actions_are_pinned_to_commits() -> None:
    """标签能被人改（2025 年 tj-actions 那次，照标签引用的两万多个仓库都拉到了改过的代码），提交号改不了。"""
    found = _USES.findall(CI.read_text(encoding="utf-8"))
    assert found, "ci.yml 里没找到 uses"
    for action, rest in found:
        name, _, ref = action.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{name} 没钉提交号：{ref}"
        assert re.search(r"#\s*v\d+\.\d+\.\d+", rest), f"{name} 后面要写注释说是哪一版（# v1.2.3）"


def test_neo4j_image_is_one_digest_everywhere() -> None:
    """三份 compose 和 CI 起的 Neo4j 是同一个镜像：都钉指纹（标签会跟着小版本走），四处一字不差。"""
    images = {}
    for path in COMPOSE:
        images[str(path.relative_to(REPO))] = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["neo4j"]["image"]
    lines = [line for line in CI.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]  # 注释不算
    found = _CI_IMAGE.findall("\n".join(lines))
    assert len(found) == 1, f"ci.yml 里找到 {len(found)} 个 Neo4j 镜像"
    images["ci.yml"] = found[0]
    assert len(COMPOSE) == 3
    assert len(set(images.values())) == 1, images
    image = next(iter(images.values()))
    assert re.search(r"@sha256:[0-9a-f]{64}$", image), f"没钉指纹：{image}"
    assert ":5.26-community@" in image  # 标签留着给人看是哪一版（钉了指纹以后拉的时候只认指纹）


# ---------- 锁文件（requirements/*.txt，uv pip compile --universal --generate-hashes 生成的） ----------

LOCKS = {"main.txt": (), "dev.txt": ("dev",), "claude.txt": ("claude",), "side.txt": ("side", "dev")}


def _entries(path: Path) -> dict[str, tuple[str, list[str]]]:
    """锁文件里每个包：(版本, 指纹)。续行（行尾的 \）拼起来；注释不算。"""
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    found: dict[str, tuple[str, list[str]]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip() if line.lstrip().startswith("#") else line.strip()
        if not line:
            continue
        head = line.split(";", 1)[0].split("--hash", 1)[0].strip()
        name, sep, version = head.partition("==")
        assert sep and version.strip(), f"{path.name} 里有没钉版本的一行：{line[:80]}"
        hashes = re.findall(r"--hash=sha256:([0-9a-f]+)", line)
        found[name.strip().lower().replace("_", "-").replace(".", "-")] = (version.strip(), hashes)
    return found


def test_lock_files_pin_every_package_with_hashes() -> None:
    for name in LOCKS:
        entries = _entries(REPO / "requirements" / name)
        assert entries, name
        for package, (version, hashes) in entries.items():
            assert hashes and all(re.fullmatch(r"[0-9a-f]{64}", one) for one in hashes), f"{name}：{package} 没带指纹"
        assert "setuptools" in entries, f"{name}：装 javmem 自己要的 setuptools 也要照锁文件装"


def test_lock_files_cover_what_pyproject_asks_for() -> None:
    """pyproject 里写的每个依赖（连同这一份的那几组可选依赖、装的时候要的 setuptools）锁文件里都有、版本在范围里。
    改了 pyproject 忘了重新生成锁文件，这里就红。"""
    import tomllib

    from packaging.requirements import Requirement

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    for name, extras in LOCKS.items():
        entries = _entries(REPO / "requirements" / name)
        wanted = [*project["project"]["dependencies"], *project["build-system"]["requires"]]
        for extra in extras:
            wanted += project["project"]["optional-dependencies"][extra]
        for text in wanted:
            requirement = Requirement(text)
            key = requirement.name.lower().replace("_", "-").replace(".", "-")
            assert key in entries, f"{name} 里没有 {requirement.name}：重新生成锁文件"
            assert requirement.specifier.contains(entries[key][0], prereleases=True), \
                f"{name} 里 {requirement.name} 是 {entries[key][0]}，pyproject 要 {requirement.specifier}：重新生成锁文件"


def test_lock_files_carry_no_local_paths() -> None:
    """锁文件的注释（# via）里会写约束文件的路径：只许写仓库里的相对路径，不许有本机的路径（盘符、用户目录）。"""
    for path in sorted((REPO / "requirements").glob("*")):
        text = path.read_text(encoding="utf-8")
        found = re.findall(r"\b[A-Za-z]:[/\\]|[/\\]U[s]ers[/\\]|/home/|/tmp/|AppData", text)  # 写成 U[s]ers：这一行自己不算本机路径
        assert not found, f"{path.name} 里有本机的路径：{found[:3]}"
