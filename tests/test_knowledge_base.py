"""The knowledge base must exist, be internally consistent and track every assumption."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "docs" / "kb"

REQUIRED = [
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "docs" / "architecture.md",
    *(
        KB / name
        for name in (
            "README.md",
            "status.md",
            "project-brief.md",
            "roadmap.md",
            "conventions.md",
            "domain.md",
            "clusters.md",
            "assumptions.md",
        )
    ),
]

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
ASSUMPTION_IN_CODE = re.compile(r"#\s*ASSUMPTION(?:\((A\d+)\))?:")


def _markdown_files() -> list[Path]:
    return [ROOT / "AGENTS.md", ROOT / "CLAUDE.md", *sorted((ROOT / "docs").rglob("*.md"))]


@pytest.mark.parametrize("path", REQUIRED, ids=lambda p: str(p.relative_to(ROOT)))
def test_required_file_exists(path: Path) -> None:
    assert path.is_file(), f"missing knowledge base file: {path.relative_to(ROOT)}"


def test_relative_links_resolve() -> None:
    broken = []
    for md in _markdown_files():
        for target in LINK.findall(md.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = (md.parent / target.split("#", 1)[0]).resolve()
            if not path.exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")
    assert not broken, "broken links:\n" + "\n".join(broken)


def test_status_has_last_updated_date() -> None:
    text = (KB / "status.md").read_text(encoding="utf-8")
    assert re.search(r"_Last updated: \d{4}-\d{2}-\d{2}_", text)


def test_every_code_assumption_is_registered() -> None:
    register = (KB / "assumptions.md").read_text(encoding="utf-8")
    known = set(re.findall(r"^\|\s*(A\d+)\s*\|", register, flags=re.MULTILINE))
    problems = []
    for py in sorted((ROOT / "src").rglob("*.py")):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            match = ASSUMPTION_IN_CODE.search(line)
            if not match:
                continue
            where = f"{py.relative_to(ROOT)}:{lineno}"
            if match.group(1) is None:
                problems.append(f"{where}: use '# ASSUMPTION(A<n>): ...'")
            elif match.group(1) not in known:
                problems.append(f"{where}: {match.group(1)} not in docs/kb/assumptions.md")
    assert not problems, "\n".join(problems)
