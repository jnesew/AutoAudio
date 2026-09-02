from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.checkpoint import CheckpointError, CheckpointStore, sha256_file
from core.config import AppConfig
from core.plan import BookPlanError, BookPlanStore
from core.progress import ProgressTracker
from metadata.extractors import extract_epub_metadata, extract_text_fallback_metadata
from metadata.models import BookMetadata


SUPPORTED_BOOK_EXTENSIONS = frozenset({".epub", ".txt", ".md", ".markdown", ".rst"})
RESUMABLE_CHECKPOINT_STATUSES = frozenset({"running", "failed", "cancelled"})


@dataclass(frozen=True)
class LibraryBook:
    """One local source title and its deterministic conversion job."""

    id: str
    source_path: Path
    title: str
    author: str
    language: str
    output_dir: Path
    status: str
    progress_percent: int
    resumable: bool
    checkpoint_status: str | None = None
    state_error: str | None = None


def job_output_dir(output_root: str | Path, book_id: str) -> Path:
    """Return the stable per-source output directory used by library jobs."""
    return Path(output_root).resolve() / f"book-{book_id[:16]}"


def _metadata_for(path: Path) -> BookMetadata:
    try:
        if path.suffix.lower() == ".epub":
            return extract_epub_metadata(str(path))
        return extract_text_fallback_metadata(str(path))
    except Exception:  # Library discovery must survive one malformed source.
        return BookMetadata(title=path.stem.replace("_", " ").strip() or "Untitled")


def _artifact_present(record: object) -> bool:
    """Use cheap presence checks for display; resume performs full hash validation."""
    return (
        isinstance(record, dict)
        and bool(record.get("path"))
        and bool(record.get("sha256"))
        and Path(str(record["path"])).is_file()
    )


def checkpoint_progress_percent(checkpoint: dict, state_dir: str | Path) -> int:
    """Derive a conservative BookPlan-weighted percentage from persisted work."""
    if checkpoint.get("status") == "completed":
        return 100

    plan_record = checkpoint.get("plan")
    if not isinstance(plan_record, dict):
        return 0
    expected_hash = plan_record.get("sha256")
    try:
        plan = BookPlanStore(Path(state_dir)).load(
            expected_sha256=str(expected_hash) if expected_hash else None
        )
    except BookPlanError:
        return 0

    completed: set[str] = set()
    artifacts = checkpoint.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    if _artifact_present(artifacts.get("disclosure")):
        completed.add(ProgressTracker.disclosure_key())

    completed_part_chapters: set[int] = set()
    part_artifacts = artifacts.get("parts")
    if isinstance(part_artifacts, dict):
        for part_index, record in part_artifacts.items():
            if not _artifact_present(record):
                continue
            try:
                completed.add(ProgressTracker.part_key(int(part_index)))
                completed_part_chapters.update(int(value) for value in record.get("chapter_indexes", []))
            except (AttributeError, TypeError, ValueError):
                continue

    chapter_artifacts = artifacts.get("chapters")
    if not isinstance(chapter_artifacts, dict):
        chapter_artifacts = {}
    chapter_masters = artifacts.get("chapter_masters")
    if not isinstance(chapter_masters, dict):
        chapter_masters = {}
    segment_artifacts = artifacts.get("segments")
    if not isinstance(segment_artifacts, dict):
        segment_artifacts = {}
    for chapter in plan.chapters:
        if chapter.skipped_reason:
            continue
        chapter_key = str(chapter.index)
        chapter_valid = _artifact_present(chapter_artifacts.get(chapter_key))
        master_valid = _artifact_present(chapter_masters.get(chapter_key))
        reusable_chapter = master_valid or (chapter.index in completed_part_chapters and chapter_valid)
        if reusable_chapter:
            completed.update(
                ProgressTracker.segment_key(chapter.index, segment.index)
                for segment in chapter.segments
            )
        else:
            for segment in chapter.segments:
                if _artifact_present(segment_artifacts.get(segment.id)):
                    completed.add(ProgressTracker.segment_key(chapter.index, segment.index))
        if chapter_valid and reusable_chapter:
            completed.add(ProgressTracker.chapter_key(chapter.index))

    ui_state = checkpoint.get("ui_state")
    if not isinstance(ui_state, dict):
        ui_state = {}
    chapters_per_part = ui_state.get("chapters_per_part", 5)
    try:
        group_size = max(1, int(chapters_per_part))
    except (TypeError, ValueError):
        group_size = 5
    tracker = ProgressTracker(
        book_plan=plan,
        chapters_per_part=group_size,
        completed_keys=completed,
    )
    return min(99, int(tracker.completed_units * 100 / max(1, tracker.total_units)))


def _job_summary(output_dir: Path, book_id: str) -> tuple[str, int, bool, str | None, str | None]:
    state_dir = AppConfig.state_dir_for(output_dir)
    store = CheckpointStore(state_dir)
    try:
        checkpoint = store.load()
    except CheckpointError as exc:
        return "State error", 0, False, None, str(exc)
    if checkpoint is None:
        return "Ready", 0, False, None, None
    input_record = checkpoint.get("input")
    if not isinstance(input_record, dict) or input_record.get("sha256") != book_id:
        return "Source changed", 0, False, str(checkpoint.get("status") or ""), None

    raw_status = str(checkpoint.get("status") or "")
    if raw_status == "completed":
        artifacts = checkpoint.get("artifacts")
        if not isinstance(artifacts, dict):
            artifacts = {}
        publishable_records = []
        for key in ("parts", "chapters"):
            records = artifacts.get(key)
            if isinstance(records, dict):
                publishable_records.extend(records.values())
        if not any(_artifact_present(record) for record in publishable_records):
            return "Output missing", 0, False, raw_status, None
    status = {
        "running": "Interrupted",
        "cancelled": "Paused",
        "failed": "Failed",
        "completed": "Complete",
    }.get(raw_status, "Unknown")
    percent = checkpoint_progress_percent(checkpoint, state_dir)
    return status, percent, raw_status in RESUMABLE_CHECKPOINT_STATUSES, raw_status, None


def _legacy_output_for(output_root: Path, book_id: str) -> Path | None:
    """Preserve incomplete jobs created before per-title output directories."""
    store = CheckpointStore(AppConfig.state_dir_for(output_root))
    try:
        checkpoint = store.load()
    except CheckpointError:
        return None
    if not checkpoint or checkpoint.get("status") not in RESUMABLE_CHECKPOINT_STATUSES:
        return None
    input_record = checkpoint.get("input")
    if not isinstance(input_record, dict) or input_record.get("sha256") != book_id:
        return None
    return output_root


def scan_library(books_dir: str | Path, output_root: str | Path) -> tuple[LibraryBook, ...]:
    """Scan supported local sources without following directory symlinks."""
    books_root = Path(books_dir).resolve()
    resolved_output_root = Path(output_root).resolve()
    if not books_root.is_dir():
        return ()

    entries: list[LibraryBook] = []
    seen_book_ids: set[str] = set()
    paths = sorted(
        (
            path
            for path in books_root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in SUPPORTED_BOOK_EXTENSIONS
        ),
        key=lambda path: str(path.relative_to(books_root)).casefold(),
    )
    for source_path in paths:
        try:
            book_id = sha256_file(source_path)
        except OSError:
            continue
        if book_id in seen_book_ids:
            continue
        seen_book_ids.add(book_id)
        metadata = _metadata_for(source_path)
        output_dir = _legacy_output_for(resolved_output_root, book_id) or job_output_dir(
            resolved_output_root, book_id
        )
        status, percent, resumable, checkpoint_status, state_error = _job_summary(output_dir, book_id)
        entries.append(
            LibraryBook(
                id=book_id,
                source_path=source_path.resolve(),
                title=metadata.title or source_path.stem,
                author=metadata.author or "Unknown",
                language=metadata.language or "unknown",
                output_dir=output_dir,
                status=status,
                progress_percent=percent,
                resumable=resumable,
                checkpoint_status=checkpoint_status,
                state_error=state_error,
            )
        )
    return tuple(entries)
