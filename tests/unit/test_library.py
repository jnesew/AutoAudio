from __future__ import annotations

from pathlib import Path

from core.checkpoint import CheckpointStore, create_initial_checkpoint, sha256_file
from core.config import AppConfig
from core.library import checkpoint_progress_percent, job_output_dir, scan_library
from core.plan import BookPlan, BookPlanStore, PlannedChapter, PlannedSegment


def _plan(input_hash: str) -> BookPlan:
    return BookPlan(
        input_sha256=input_hash,
        settings_sha256="settings",
        workflow_sha256="workflow",
        chapters=(
            PlannedChapter(
                index=0,
                title="One",
                segments=(
                    PlannedSegment.from_text(chapter_index=0, segment_index=0, text="one two three"),
                    PlannedSegment.from_text(chapter_index=0, segment_index=1, text="four five six"),
                ),
            ),
        ),
    )


def _checkpoint(source: Path, output_dir: Path, status: str = "cancelled") -> dict:
    input_hash = sha256_file(source)
    state_dir = AppConfig.state_dir_for(output_dir)
    plan = _plan(input_hash)
    BookPlanStore(state_dir).save(plan)
    checkpoint = create_initial_checkpoint(
        input_path=str(source),
        input_hash=input_hash,
        settings_hash="settings",
        workflow_hash="workflow",
        plan_path=str(BookPlanStore(state_dir).path),
        plan_hash=plan.sha256,
        output_dir=str(output_dir.resolve()),
        output_format="flac",
        ui_state={"chapters_per_part": 5},
    )
    checkpoint["status"] = status
    return checkpoint


def test_scan_library_finds_supported_titles_and_assigns_stable_job_directory(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    source = books / "story.txt"
    source.write_text("Title: A Story\nAuthor: Example Writer\n\nText", encoding="utf-8")
    (books / "source.json").write_text("{}", encoding="utf-8")

    entries = scan_library(books, tmp_path / "output")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.title == "A Story"
    assert entry.author == "Example Writer"
    assert entry.status == "Ready"
    assert entry.output_dir == job_output_dir(tmp_path / "output", sha256_file(source))


def test_scan_library_collapses_identical_duplicate_sources_into_one_title(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    contents = "Title: Same Story\n\nText"
    (books / "first.txt").write_text(contents, encoding="utf-8")
    (books / "second.txt").write_text(contents, encoding="utf-8")

    entries = scan_library(books, tmp_path / "output")

    assert len(entries) == 1
    assert entries[0].source_path.name == "first.txt"


def test_scan_library_reports_paused_per_title_progress(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    source = books / "story.txt"
    source.write_text("Title: A Story\n\nText", encoding="utf-8")
    output_dir = job_output_dir(tmp_path / "output", sha256_file(source))
    checkpoint = _checkpoint(source, output_dir)
    segment_path = output_dir / ".segments" / "segment.flac"
    segment_path.parent.mkdir(parents=True)
    segment_path.write_bytes(b"audio")
    checkpoint["progress"]["completed_segments"] = {"0": [0]}
    checkpoint["artifacts"]["segments"] = {
        "0:0": {"path": str(segment_path), "sha256": sha256_file(segment_path)}
    }
    CheckpointStore(AppConfig.state_dir_for(output_dir)).save(checkpoint)

    entry = scan_library(books, tmp_path / "output")[0]

    assert entry.status == "Paused"
    assert entry.resumable is True
    assert 0 < entry.progress_percent < 100
    assert checkpoint_progress_percent(checkpoint, AppConfig.state_dir_for(output_dir)) == entry.progress_percent


def test_scan_library_preserves_incomplete_legacy_global_job(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    source = books / "legacy.txt"
    source.write_text("legacy title", encoding="utf-8")
    output_root = tmp_path / "output"
    checkpoint = _checkpoint(source, output_root, status="failed")
    CheckpointStore(AppConfig.state_dir_for(output_root)).save(checkpoint)

    entry = scan_library(books, output_root)[0]

    assert entry.output_dir == output_root.resolve()
    assert entry.status == "Failed"
    assert entry.resumable is True


def test_completed_checkpoint_with_missing_publishable_files_is_not_reported_complete(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    source = books / "completed.txt"
    source.write_text("completed title", encoding="utf-8")
    output_dir = job_output_dir(tmp_path / "output", sha256_file(source))
    checkpoint = _checkpoint(source, output_dir, status="completed")
    checkpoint["artifacts"]["parts"] = {
        "1": {"path": str(output_dir / "missing.flac"), "sha256": "abc"}
    }
    CheckpointStore(AppConfig.state_dir_for(output_dir)).save(checkpoint)

    entry = scan_library(books, tmp_path / "output")[0]

    assert entry.status == "Output missing"
    assert entry.progress_percent == 0


def test_scan_library_does_not_follow_source_symlinks(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (books / "linked.txt").symlink_to(outside)

    assert scan_library(books, tmp_path / "output") == ()
