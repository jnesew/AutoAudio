from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.narration_text import (
    ReplacementRule,
    ReplacementRuleError,
    apply_replacement_rules,
    apply_rules_to_chapters,
    high_confidence_plain_text_toc_indexes,
    load_replacement_rules,
)
from core.pipeline import extract_text_blocks_from_text_file


def test_whole_word_rule_does_not_replace_inside_another_word():
    rule = ReplacementRule(source="IV", spoken="four")

    result = apply_replacement_rules("Chapter IV has vivid detail.", (rule,))

    assert result.text == "Chapter four has vivid detail."
    assert result.count == 1


def test_rules_are_single_pass_and_longest_match_wins():
    rules = (
        ReplacementRule(source="A", spoken="B", match="literal"),
        ReplacementRule(source="B", spoken="C", match="literal"),
        ReplacementRule(source="AB", spoken="long", match="literal"),
    )

    assert apply_replacement_rules("A", rules).text == "B"
    assert apply_replacement_rules("AB", rules).text == "long"


def test_case_scope_and_title_rules_are_respected():
    rules = (
        ReplacementRule(source="dr.", spoken="Doctor", match="literal", case_sensitive=False),
        ReplacementRule(source="IV", spoken="the Fourth", scope="title"),
    )

    chapters, count = apply_rules_to_chapters((("Book IV", "DR. Vale met Dr. Stone."),), rules)

    assert chapters == [("Book the Fourth", "Doctor Vale met Doctor Stone.")]
    assert count == 3


def test_rule_file_and_inline_rules_are_merged_in_order(tmp_path):
    rule_file = tmp_path / "replacements.json"
    rule_file.write_text(
        json.dumps(
            {
                "version": 1,
                "replacements": [
                    {
                        "source": "IV",
                        "spoken": "four",
                        "match": "whole-word",
                        "scope": "body",
                        "case_sensitive": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rules = load_replacement_rules(rule_file, ("Dr.=Doctor",))

    assert [rule.source for rule in rules] == ["IV", "Dr."]
    assert rules[1].to_dict() == {
        "source": "Dr.",
        "spoken": "Doctor",
        "match": "whole-word",
        "scope": "body",
        "case_sensitive": True,
    }


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"version": 2, "replacements": []}, "Unsupported replacement rules version"),
        ({"version": 1, "replacements": [{"source": "", "spoken": "x"}]}, "cannot be empty"),
        (
            {
                "version": 1,
                "replacements": [{"source": "(", "spoken": "x", "match": "regex"}],
            },
            "Invalid replacement regular expression",
        ),
    ],
)
def test_invalid_rule_files_are_rejected(tmp_path, payload, message):
    rule_file = tmp_path / "bad.json"
    rule_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReplacementRuleError, match=message):
        load_replacement_rules(rule_file)


def test_zero_width_regular_expression_is_rejected_at_application():
    rule = ReplacementRule(source="^", spoken="prefix", match="regex")

    with pytest.raises(ReplacementRuleError, match="empty match"):
        apply_replacement_rules("text", (rule,))


def test_plain_text_toc_requires_heading_and_multiple_entries():
    paragraphs = [
        "Contents",
        "CHAPTER I. Arrival ........ 1\nCHAPTER II. Winter ........ 9\nCHAPTER III. Return ........ 20",
        "This is the first long narrative paragraph and it must remain in the audiobook output.",
    ]

    assert high_confidence_plain_text_toc_indexes(paragraphs) == frozenset({0, 1})
    assert high_confidence_plain_text_toc_indexes(("Contents", "A reflective essay.")) == frozenset()


def test_plain_text_extraction_omits_toc_unless_requested(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text(
        "Contents\n\n"
        "CHAPTER I. Arrival ........ 1\nCHAPTER II. Winter ........ 9\nCHAPTER III. Return ........ 20\n\n"
        "This is the first long narrative paragraph and it must remain in the audiobook output.",
        encoding="utf-8",
    )

    default_text = " ".join(text for _title, text in extract_text_blocks_from_text_file(str(source)))
    included_text = " ".join(
        text for _title, text in extract_text_blocks_from_text_file(str(source), narrate_toc=True)
    )

    assert "CHAPTER I" not in default_text
    assert "first long narrative" in default_text
    assert "CHAPTER I" in included_text
