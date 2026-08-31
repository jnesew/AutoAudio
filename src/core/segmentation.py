from __future__ import annotations

import re
from dataclasses import dataclass


QWEN_SEGMENT_PLANNER_VERSION = "qwen-semantic-v1"


@dataclass(frozen=True)
class SegmentPolicy:
    target_words: int
    max_words: int

    def __post_init__(self) -> None:
        if self.target_words <= 0 or self.max_words <= 0:
            raise ValueError("Segment word limits must be positive.")
        if self.target_words > self.max_words:
            raise ValueError("Segment target_words cannot exceed max_words.")


def default_segment_policy(voice_mode: str) -> SegmentPolicy:
    if voice_mode == "preset":
        return SegmentPolicy(target_words=150, max_words=220)
    if voice_mode == "design":
        return SegmentPolicy(target_words=190, max_words=240)
    raise ValueError(f"Unsupported Qwen voice mode: {voice_mode!r}")


def _word_windows(text: str, max_words: int) -> list[str]:
    words = text.split()
    return [" ".join(words[index : index + max_words]) for index in range(0, len(words), max_words)]


def _split_oversized_sentence(sentence: str, max_words: int) -> list[str]:
    clauses = re.split(r"(?<=[,;:—–])\s+", sentence)
    units: list[str] = []
    current: list[str] = []
    current_words = 0

    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        clause_words = len(clause.split())
        if clause_words > max_words:
            if current:
                units.append(" ".join(current))
                current = []
                current_words = 0
            units.extend(_word_windows(clause, max_words))
        elif current and current_words + clause_words > max_words:
            units.append(" ".join(current))
            current = [clause]
            current_words = clause_words
        else:
            current.append(clause)
            current_words += clause_words

    if current:
        units.append(" ".join(current))
    return units


def segment_text_for_qwen(text: str, policy: SegmentPolicy) -> list[str]:
    """Create sentence-aware Qwen calls with a strict word-count ceiling."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []

    sentences = re.split(r"(?<=[.!?…])\s+", normalized)
    units: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence.split()) <= policy.max_words:
            units.append(sentence)
        else:
            units.extend(_split_oversized_sentence(sentence, policy.max_words))

    segments: list[str] = []
    current: list[str] = []
    current_words = 0
    for unit in units:
        unit_words = len(unit.split())
        if current and (current_words >= policy.target_words or current_words + unit_words > policy.max_words):
            segments.append(" ".join(current))
            current = []
            current_words = 0
        current.append(unit)
        current_words += unit_words

    if current:
        segments.append(" ".join(current))

    return segments
