from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.segmentation import SegmentPolicy, default_segment_policy, segment_text_for_qwen


def test_short_chapter_remains_one_segment():
    text = "A short opening sentence. A second short sentence closes the chapter."

    assert segment_text_for_qwen(text, SegmentPolicy(target_words=20, max_words=30)) == [text]


def test_segments_prefer_sentence_boundaries_and_never_include_pause_tokens():
    text = "One two three four. Five six seven eight. Nine ten eleven twelve."

    segments = segment_text_for_qwen(text, SegmentPolicy(target_words=6, max_words=8))

    assert segments == ["One two three four. Five six seven eight.", "Nine ten eleven twelve."]
    assert all("[pause]" not in segment for segment in segments)


def test_oversized_unpunctuated_sentence_is_hard_split():
    text = " ".join(f"word{index}" for index in range(23))

    segments = segment_text_for_qwen(text, SegmentPolicy(target_words=8, max_words=10))

    assert [len(segment.split()) for segment in segments] == [10, 10, 3]
    assert " ".join(segments) == text


def test_oversized_sentence_prefers_clause_boundaries():
    text = "one two three four, five six seven eight; nine ten eleven twelve."

    segments = segment_text_for_qwen(text, SegmentPolicy(target_words=4, max_words=5))

    assert segments == ["one two three four,", "five six seven eight;", "nine ten eleven twelve."]


def test_mode_defaults_favor_larger_voice_design_segments():
    preset = default_segment_policy("preset")
    design = default_segment_policy("design")

    assert preset.target_words < design.target_words
    assert preset.max_words < design.max_words


def test_invalid_segment_policy_is_rejected():
    with pytest.raises(ValueError, match="cannot exceed"):
        SegmentPolicy(target_words=201, max_words=200)
