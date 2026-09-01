from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from core.plan import BookPlan, PlannedChapter


DISCLOSURE_UNITS = 50
SEGMENT_OVERHEAD_UNITS = 40
MIN_CHAPTER_FINALIZATION_UNITS = 80
MIN_PART_FINALIZATION_UNITS = 120
ETA_OBSERVATIONS_REQUIRED = 2
ETA_EWMA_ALPHA = 0.35


@dataclass(frozen=True)
class ProgressUpdate:
    phase: str
    percent: int
    completed_units: int
    total_units: int
    eta_seconds: float | None = None
    chapter_number: int | None = None
    total_chapters: int | None = None
    segment_number: int | None = None
    total_segments: int | None = None


def format_eta(seconds: float) -> str:
    if seconds < 60:
        return "less than a minute"
    minutes = max(1, math.ceil(seconds / 60))
    hours, remaining_minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}m"
    if not remaining_minutes:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"


def format_progress_text(update: ProgressUpdate) -> str:
    parts = [update.phase]
    if update.chapter_number is not None and update.total_chapters is not None:
        parts.append(f"Chapter {update.chapter_number}/{update.total_chapters}")
    if update.segment_number is not None and update.total_segments is not None:
        parts.append(f"Segment {update.segment_number}/{update.total_segments}")
    parts.append(f"{update.percent}%")
    if update.eta_seconds is not None:
        parts.append(f"about {format_eta(update.eta_seconds)} remaining")
    return " · ".join(parts)


def _word_count(text: str) -> int:
    return max(1, len(text.split()))


class ProgressTracker:
    """Weight BookPlan work and derive a session-local, resume-aware ETA."""

    def __init__(
        self,
        *,
        book_plan: BookPlan,
        chapters_per_part: int,
        completed_keys: set[str] | None = None,
        callback: Callable[[ProgressUpdate], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.book_plan = book_plan
        self.callback = callback
        self.clock = clock
        self.weights = self._build_weights(book_plan, chapters_per_part)
        self.completed_keys = set(completed_keys or ()) & set(self.weights)
        self._last_observation_at = clock()
        self._seconds_per_unit: float | None = None
        self._observations = 0
        self._finished = False

    @staticmethod
    def disclosure_key() -> str:
        return "disclosure"

    @staticmethod
    def segment_key(chapter_index: int, segment_index: int) -> str:
        return f"segment:{chapter_index}:{segment_index}"

    @staticmethod
    def chapter_key(chapter_index: int) -> str:
        return f"chapter:{chapter_index}"

    @staticmethod
    def part_key(part_index: int) -> str:
        return f"part:{part_index}"

    @classmethod
    def _chapter_units(cls, chapter: PlannedChapter) -> int:
        words = sum(_word_count(segment.text) for segment in chapter.segments)
        return max(MIN_CHAPTER_FINALIZATION_UNITS, math.ceil(words * 0.12))

    @classmethod
    def _build_weights(cls, book_plan: BookPlan, chapters_per_part: int) -> dict[str, int]:
        weights = {cls.disclosure_key(): DISCLOSURE_UNITS}
        active_chapters = [chapter for chapter in book_plan.chapters if not chapter.skipped_reason]
        for chapter in active_chapters:
            for segment in chapter.segments:
                weights[cls.segment_key(chapter.index, segment.index)] = (
                    _word_count(segment.text) + SEGMENT_OVERHEAD_UNITS
                )
            weights[cls.chapter_key(chapter.index)] = cls._chapter_units(chapter)

        group_size = max(1, chapters_per_part)
        for offset in range(0, len(active_chapters), group_size):
            part_index = offset // group_size + 1
            part_chapters = active_chapters[offset : offset + group_size]
            part_words = sum(
                _word_count(segment.text)
                for chapter in part_chapters
                for segment in chapter.segments
            )
            weights[cls.part_key(part_index)] = max(
                MIN_PART_FINALIZATION_UNITS,
                math.ceil(part_words * 0.08),
            )
        return weights

    @property
    def total_units(self) -> int:
        return sum(self.weights.values())

    @property
    def completed_units(self) -> int:
        return sum(self.weights[key] for key in self.completed_keys)

    def _eta_seconds(self) -> float | None:
        if self._finished or self._observations < ETA_OBSERVATIONS_REQUIRED or self._seconds_per_unit is None:
            return None
        remaining_units = max(0, self.total_units - self.completed_units)
        return remaining_units * self._seconds_per_unit

    def _update(
        self,
        *,
        phase: str,
        chapter_index: int | None = None,
        segment_index: int | None = None,
        total_segments: int | None = None,
    ) -> ProgressUpdate:
        total = max(1, self.total_units)
        percent = min(100, int(self.completed_units * 100 / total))
        update = ProgressUpdate(
            phase=phase,
            percent=percent,
            completed_units=self.completed_units,
            total_units=total,
            eta_seconds=self._eta_seconds(),
            chapter_number=(chapter_index + 1 if chapter_index is not None else None),
            total_chapters=(len(self.book_plan.chapters) if chapter_index is not None else None),
            segment_number=(segment_index + 1 if segment_index is not None else None),
            total_segments=total_segments,
        )
        if self.callback is not None:
            self.callback(update)
        return update

    def report(
        self,
        phase: str,
        *,
        chapter_index: int | None = None,
        segment_index: int | None = None,
        total_segments: int | None = None,
    ) -> ProgressUpdate:
        return self._update(
            phase=phase,
            chapter_index=chapter_index,
            segment_index=segment_index,
            total_segments=total_segments,
        )

    def complete(
        self,
        key: str,
        phase: str,
        *,
        chapter_index: int | None = None,
        segment_index: int | None = None,
        total_segments: int | None = None,
    ) -> ProgressUpdate:
        if key in self.weights and key not in self.completed_keys:
            now = self.clock()
            elapsed = max(0.0, now - self._last_observation_at)
            units = self.weights[key]
            sample = elapsed / units
            if self._seconds_per_unit is None:
                self._seconds_per_unit = sample
            else:
                self._seconds_per_unit = (
                    ETA_EWMA_ALPHA * sample + (1 - ETA_EWMA_ALPHA) * self._seconds_per_unit
                )
            self._observations += 1
            self._last_observation_at = now
            self.completed_keys.add(key)
        return self._update(
            phase=phase,
            chapter_index=chapter_index,
            segment_index=segment_index,
            total_segments=total_segments,
        )

    def finish(self) -> ProgressUpdate:
        self.completed_keys = set(self.weights)
        self._finished = True
        return self._update(phase="Completed")
