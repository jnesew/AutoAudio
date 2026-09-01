from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _requirement_names() -> set[str]:
    names: set[str] = set()
    for raw_line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"\s+@\s+|[<>=!~\[]", line, maxsplit=1)[0]
        names.add(name.strip().lower())
    return names


def test_every_direct_requirement_is_in_dependency_inventory():
    inventory = (PROJECT_ROOT / "THIRD_PARTY_DEPENDENCIES.md").read_text(encoding="utf-8").lower()

    assert all(f"| {name} |" in inventory for name in _requirement_names())


def test_notice_set_covers_every_direct_requirement():
    snapshot = (PROJECT_ROOT / "LICENSES" / "third-party-licenses.md").read_text(encoding="utf-8").lower()
    supplemental = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (PROJECT_ROOT / "LICENSES").glob("*.txt")
    )
    notice_corpus = f"{snapshot}\n{supplemental}"

    assert all(name in notice_corpus for name in _requirement_names())


def test_removed_v1_parser_dependencies_are_not_declared_or_inventoried():
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    inventory = (PROJECT_ROOT / "THIRD_PARTY_DEPENDENCIES.md").read_text(encoding="utf-8").lower()

    assert "ebooklib" not in requirements
    assert "beautifulsoup" not in requirements
    assert "ebooklib" not in inventory
    assert "beautifulsoup" not in inventory
