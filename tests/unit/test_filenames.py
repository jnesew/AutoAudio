from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.filenames import safe_filename_component


@pytest.mark.parametrize(
    ("unsafe", "expected"),
    [
        ("../../outside/book", "outside book"),
        ("..\\..\\outside\\book", "outside book"),
        ("CON", "_CON"),
        ("CON .txt", "_CON .txt"),
        (" title. ", "title"),
        ("chapter\x00\nname", "chapter name"),
        ("Ａ／Ｂ", "A B"),
    ],
)
def test_safe_filename_component_blocks_cross_platform_path_hazards(unsafe, expected):
    assert safe_filename_component(unsafe) == expected


def test_safe_filename_component_is_utf8_bounded_without_splitting_codepoint():
    result = safe_filename_component("é" * 200, max_bytes=31)

    assert len(result.encode("utf-8")) <= 31
    assert result == "é" * 15


def test_safe_filename_component_uses_stable_fallback():
    assert safe_filename_component("///", fallback="Chapter 001") == "Chapter 001"
    assert safe_filename_component("///", fallback="../fallback") == "fallback"
    assert safe_filename_component("///", fallback="é", max_bytes=1) == "U"


def test_safe_filename_component_rejects_invalid_limit():
    with pytest.raises(ValueError):
        safe_filename_component("title", max_bytes=0)
