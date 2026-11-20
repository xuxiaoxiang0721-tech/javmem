"""给外人看的文档（README 和 docs/ 下除了开发说明的几份）：不带内部的编号和线的叫法，里面的链接都指得到文件。

内部的编号（决策号、步骤号这些）和两条线的叫法只留在 docs/development.md（它开头说了一句是什么）；对外的文档用平常的话说。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INTERNAL = re.compile(r"(?<![A-Za-z0-9])(?:D\d{2,3}|J\d+(?:\.\d+)?|U\d)(?![0-9])|(?<![A-Za-z0-9-])(?:sol|luna)(?![A-Za-z0-9])")
LINK = re.compile(r"\]\(([^)\s]+)\)")


def outward() -> list[Path]:
    return [REPO / "README.md", *sorted(path for path in (REPO / "docs").glob("*.md") if path.name != "development.md")]


def internal_ids(text: str) -> list[str]:
    return [match.group(0) for match in INTERNAL.finditer(text)]


def broken_links(path: Path, text: str) -> list[str]:
    out = []
    for target in LINK.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue  # 外面的网址、页内的锚
        if not (path.parent / target.split("#", 1)[0]).exists():
            out.append(target)
    return out


def test_the_outward_docs_carry_no_internal_ids() -> None:
    found = {path.name: hits for path in outward() if (hits := internal_ids(path.read_text(encoding="utf-8")))}
    assert found == {}


def test_every_relative_link_in_the_outward_docs_points_at_a_file() -> None:
    found = {path.name: bad for path in outward() if (bad := broken_links(path, path.read_text(encoding="utf-8")))}
    assert found == {}


def test_the_readme_lists_every_outward_doc() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    missing = [path.name for path in outward()[1:] if f"(docs/{path.name})" not in readme]
    assert missing == []


def test_the_checks_here_would_catch_an_id_a_line_name_and_a_broken_link(tmp_path: Path) -> None:
    assert internal_ids("见 D141 和 J3.8、U3；sol 线") == ["D141", "J3.8", "U3", "sol"]
    assert internal_ids("单元 D1-F008，模型 gpt-6.1-sol，盘符 Q:") == []
    page = tmp_path / "page.md"
    (tmp_path / "there.md").write_text("x", encoding="utf-8")
    text = "[有](there.md#一节) [没有](missing.md) [网址](https://example.com/a) [锚](#上面)"
    assert broken_links(page, text) == ["missing.md"]
