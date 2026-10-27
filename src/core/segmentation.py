from __future__ import annotations

import re
from dataclasses import dataclass


QWEN_SEGMENT_PLANNER_VERSION = "qwen-unicode-limits-v2"
_CJK_CHARACTERS = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af\U00020000-\U000323af]")


@dataclass(frozen=True)
class SegmentPolicy:
    target_words: int
    max_words: int
    max_chars: int = 2400
    max_bytes: int = 7200

    def __post_init__(self) -> None:
        if self.target_words <= 0 or self.max_words <= 0:
            raise ValueError("Segment word limits must be positive.")
        if self.target_words > self.max_words:
            raise ValueError("Segment target_words cannot exceed max_words.")
        if self.max_chars < 1 or self.max_bytes < 4:
            raise ValueError("Segment max_chars must be positive and max_bytes must be at least 4.")

    def accepts(self, text: str) -> bool:
        return (
            len(text.split()) <= self.max_words
            and len(text) <= self.max_chars
            and len(text.encode("utf-8")) <= self.max_bytes
            # A whitespace word can contain an entire CJK chapter. Keep a
            # conservative character budget tied to the requested word budget.
            and len(_CJK_CHARACTERS.findall(text)) <= self.max_words * 2
        )


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


def _bounded_units(text: str, policy: SegmentPolicy) -> list[str]:
    """Split oversized runs without dropping Unicode code points or punctuation."""
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + policy.max_chars)
        # Length, UTF-8 size, word count, and CJK count are monotone for prefixes.
        # Search the longest safe prefix without repeatedly trimming the tail.
        if not policy.accepts(text[start:end]):
            low, high = start + 1, end
            while low < high:
                middle = (low + high + 1) // 2
                if policy.accepts(text[start:middle]):
                    low = middle
                else:
                    high = middle - 1
            end = low
        if end < len(text):
            boundary = text.rfind(" ", start, end + 1)
            if boundary > start:
                end = boundary
        pieces.append(text[start:end].strip())
        start = end
        while start < len(text) and text[start].isspace():
            start += 1
    return pieces


def segment_text_for_qwen(text: str, policy: SegmentPolicy) -> list[str]:
    """Create sentence-aware calls with word, character, and UTF-8 ceilings."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []

    sentences = re.split(r"(?<=[.!?…])\s+|(?<=[。！？])(?=\S)", normalized)
    units: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        word_units = (
            [sentence] if len(sentence.split()) <= policy.max_words
            else _split_oversized_sentence(sentence, policy.max_words)
        )
        for unit in word_units:
            units.extend(_bounded_units(unit, policy))

    segments: list[str] = []
    current: list[str] = []
    current_words = 0
    for unit in units:
        unit_words = len(unit.split())
        candidate = " ".join([*current, unit])
        if current and (current_words >= policy.target_words or not policy.accepts(candidate)):
            segments.append(" ".join(current))
            current = []
            current_words = 0
        current.append(unit)
        current_words += unit_words

    if current:
        segments.append(" ".join(current))

    return segments
