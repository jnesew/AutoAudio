from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Mock the uninstalled network client dependency.
if "websocket" not in sys.modules:
    websocket_module = types.ModuleType("websocket")
    websocket_module.WebSocket = object
    sys.modules["websocket"] = websocket_module


from core.audio_assembly import sanitize_metadata_value
from core.pipeline import build_qwen_book_plan, resolve_metadata
from core.segmentation import SegmentPolicy
from metadata.epub_parser import ParsedEpub
from metadata.models import BookMetadata


def test_sanitize_ffmpeg_metadata_value_removes_newlines():
    assert sanitize_metadata_value("Chapter 2: I.\nIntroduction") == "Chapter 2: I. Introduction"
    assert sanitize_metadata_value("\n\n") is None


def test_book_plan_does_not_skip_chapter_for_gutenberg_words_alone():
    plan = build_qwen_book_plan(
        [("Preface", "Project Gutenberg publishes this sentence, but it is legitimate narration text.")],
        input_hash="input",
        settings_hash="settings",
        workflow_hash="workflow",
        segment_policy=SegmentPolicy(target_words=20, max_words=30),
    )

    assert plan.chapters[0].skipped_reason is None
    assert [segment.text for segment in plan.chapters[0].segments] == [
        "Project Gutenberg publishes this sentence, but it is legitimate narration text."
    ]


def test_resolve_metadata_reuses_parsed_epub_without_reopening(tmp_path):
    parsed = ParsedEpub(
        metadata=BookMetadata(title="Embedded title", author="Embedded author"),
        text_blocks=(("Chapter One", "Long enough chapter text for the already parsed EPUB snapshot."),),
        cover=None,
        diagnostics=(),
        gutenberg_detected=False,
        gutenberg_changed=False,
    )
    args = types.SimpleNamespace(fetch_metadata=False, gutenberg_id="", title="", author="")

    with patch("core.pipeline.parse_epub", side_effect=AssertionError("EPUB was reopened")):
        metadata = resolve_metadata(
            args,
            str(tmp_path / "book.epub"),
            "epub",
            str(tmp_path),
            parsed_epub=parsed,
        )

    assert metadata.title == "Embedded title"
    assert metadata.author == "Embedded author"


def test_resolve_metadata_treats_cover_write_failure_as_nonfatal(tmp_path):
    parsed = ParsedEpub(
        metadata=BookMetadata(title="Embedded title", author="Embedded author"),
        text_blocks=(),
        cover=None,
        diagnostics=(),
        gutenberg_detected=False,
        gutenberg_changed=False,
    )
    args = types.SimpleNamespace(fetch_metadata=False, gutenberg_id="", title="", author="")

    with patch("core.pipeline.write_cover_art", side_effect=OSError("read-only output")):
        metadata = resolve_metadata(
            args,
            str(tmp_path / "book.epub"),
            "epub",
            str(tmp_path),
            parsed_epub=parsed,
        )

    assert metadata.title == "Embedded title"
    assert metadata.cover_image_path is None
