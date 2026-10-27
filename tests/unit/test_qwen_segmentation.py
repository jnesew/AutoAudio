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


@pytest.mark.parametrize("text", (
    "这是一个长篇中文测试句子。" * 1500,
    "句読点なしの日本語テキスト" * 1600,
    "https://example.test/" + "a" * 20_000,
    "😀" * 5000,
    ("a" * 120 + " ") * 200,
))
def test_character_and_utf8_limits_bound_every_segment_without_losing_content(text):
    policy = SegmentPolicy(target_words=150, max_words=220, max_chars=600, max_bytes=1000)
    segments = segment_text_for_qwen(text, policy)
    assert len(segments) > 1
    assert all(policy.accepts(segment) for segment in segments)
    assert "".join("".join(segment.split()) for segment in segments) == "".join(text.split())


def test_cjk_sentence_boundaries_are_preferred():
    text = "第一句。第二句！第三句？第四句。"
    segments = segment_text_for_qwen(text, SegmentPolicy(10, 20, max_chars=9))
    assert segments == ["第一句。 第二句！", "第三句？ 第四句。"]


def test_default_cjk_budget_is_tied_to_word_limit_instead_of_a_whole_chapter():
    policy = default_segment_policy("preset")
    segments = segment_text_for_qwen("長" * 20_000, policy)
    assert max(map(len, segments)) <= policy.max_words * 2
    assert "".join(segments) == "長" * 20_000


@pytest.mark.parametrize("kwargs", ({"max_chars": 0}, {"max_bytes": 3}))
def test_invalid_character_or_byte_limit_is_rejected(kwargs):
    with pytest.raises(ValueError, match="max_chars"):
        SegmentPolicy(10, 20, **kwargs)
