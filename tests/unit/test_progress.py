from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.plan import BookPlan, PlannedChapter, PlannedSegment
from core.progress import ProgressTracker, ProgressUpdate, format_eta, format_progress_text


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _plan() -> BookPlan:
    first_segments = (
        PlannedSegment.from_text(chapter_index=0, segment_index=0, text="one two three four"),
        PlannedSegment.from_text(chapter_index=0, segment_index=1, text="five six seven eight nine"),
    )
    second_segments = (
        PlannedSegment.from_text(chapter_index=1, segment_index=0, text="ten eleven twelve"),
    )
    return BookPlan(
        input_sha256="input",
        settings_sha256="settings",
        workflow_sha256="workflow",
        chapters=(
            PlannedChapter(index=0, title="One", segments=first_segments),
            PlannedChapter(index=1, title="Two", segments=second_segments),
            PlannedChapter(index=2, title="Skipped", segments=(), skipped_reason="front matter"),
        ),
    )


def test_progress_is_weighted_monotonic_and_gains_eta_after_observations():
    clock = FakeClock()
    updates: list[ProgressUpdate] = []
    tracker = ProgressTracker(
        book_plan=_plan(),
        chapters_per_part=2,
        callback=updates.append,
        clock=clock,
    )

    tracker.report("Preparing")
    clock.advance(10)
    tracker.complete(ProgressTracker.disclosure_key(), "Disclosure ready")
    assert updates[-1].eta_seconds is None

    clock.advance(20)
    tracker.complete(
        ProgressTracker.segment_key(0, 0),
        "Narrating",
        chapter_index=0,
        segment_index=0,
        total_segments=2,
    )

    assert updates[-1].eta_seconds is not None
    assert updates[-1].chapter_number == 1
    assert updates[-1].segment_number == 1
    assert [update.percent for update in updates] == sorted(update.percent for update in updates)

    finished = tracker.finish()
    assert finished.percent == 100
    assert finished.phase == "Completed"
    assert finished.eta_seconds is None


def test_resume_progress_starts_from_reusable_work_without_polluting_eta():
    clock = FakeClock()
    completed = {
        ProgressTracker.disclosure_key(),
        ProgressTracker.segment_key(0, 0),
    }
    tracker = ProgressTracker(
        book_plan=_plan(),
        chapters_per_part=2,
        completed_keys=completed,
        clock=clock,
    )

    initial = tracker.report("Preparing")
    assert initial.percent > 0
    clock.advance(100)
    repeated = tracker.complete(ProgressTracker.segment_key(0, 0), "Resume segment")
    assert repeated.eta_seconds is None

    clock.advance(20)
    first_new = tracker.complete(ProgressTracker.segment_key(0, 1), "Narrating")
    assert first_new.eta_seconds is None
    clock.advance(10)
    second_new = tracker.complete(ProgressTracker.chapter_key(0), "Chapter complete")
    assert second_new.eta_seconds is not None


def test_skipped_chapters_do_not_add_work_and_active_chapters_share_one_part():
    tracker = ProgressTracker(book_plan=_plan(), chapters_per_part=2)

    assert ProgressTracker.chapter_key(2) not in tracker.weights
    assert ProgressTracker.part_key(1) in tracker.weights
    assert ProgressTracker.part_key(2) not in tracker.weights


def test_progress_text_and_eta_are_compact():
    update = ProgressUpdate(
        phase="Narrating",
        percent=42,
        completed_units=420,
        total_units=1000,
        eta_seconds=4_620,
        chapter_number=8,
        total_chapters=26,
        segment_number=2,
        total_segments=5,
    )

    assert format_eta(20) == "less than a minute"
    assert format_eta(125) == "3m"
    assert format_eta(4_620) == "1h 17m"
    assert format_progress_text(update) == (
        "Narrating · Chapter 8/26 · Segment 2/5 · 42% · about 1h 17m remaining"
    )
