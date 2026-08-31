from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.checkpoint import sha256_bytes
from core.segmentation import QWEN_SEGMENT_PLANNER_VERSION


BOOK_PLAN_SCHEMA_VERSION = 1


class BookPlanError(ValueError):
    """Raised when a persisted book plan cannot be trusted."""


class UnsupportedBookPlanVersion(BookPlanError):
    def __init__(self, version: Any):
        super().__init__(
            f"Unsupported book plan schema version {version!r}; "
            f"expected {BOOK_PLAN_SCHEMA_VERSION}. Start a new run to rebuild the plan."
        )


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class PlannedSegment:
    id: str
    index: int
    text: str
    text_sha256: str

    @classmethod
    def from_text(cls, *, chapter_index: int, segment_index: int, text: str) -> "PlannedSegment":
        normalized_text = text.strip()
        return cls(
            id=f"{chapter_index}:{segment_index}",
            index=segment_index,
            text=normalized_text,
            text_sha256=sha256_bytes(normalized_text.encode("utf-8")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "text": self.text,
            "text_sha256": self.text_sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlannedSegment":
        try:
            segment = cls(
                id=str(payload["id"]),
                index=int(payload["index"]),
                text=str(payload["text"]),
                text_sha256=str(payload["text_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BookPlanError("Book plan contains an invalid segment record.") from exc

        expected_hash = sha256_bytes(segment.text.encode("utf-8"))
        if segment.text_sha256 != expected_hash:
            raise BookPlanError(f"Segment {segment.id!r} text hash does not match its persisted text.")
        return segment


@dataclass(frozen=True)
class PlannedChapter:
    index: int
    title: str
    segments: tuple[PlannedSegment, ...]
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "segments": [segment.to_dict() for segment in self.segments],
            "skipped_reason": self.skipped_reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlannedChapter":
        try:
            raw_segments = payload["segments"]
            if not isinstance(raw_segments, list):
                raise TypeError("segments must be a list")
            chapter = cls(
                index=int(payload["index"]),
                title=str(payload["title"]),
                segments=tuple(PlannedSegment.from_dict(segment) for segment in raw_segments),
                skipped_reason=(str(payload["skipped_reason"]) if payload.get("skipped_reason") else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, BookPlanError):
                raise
            raise BookPlanError("Book plan contains an invalid chapter record.") from exc

        expected_ids = [f"{chapter.index}:{index}" for index in range(len(chapter.segments))]
        actual_ids = [segment.id for segment in chapter.segments]
        actual_indices = [segment.index for segment in chapter.segments]
        if actual_ids != expected_ids or actual_indices != list(range(len(chapter.segments))):
            raise BookPlanError(f"Chapter {chapter.index} segment identifiers are not contiguous.")
        return chapter


@dataclass(frozen=True)
class BookPlan:
    input_sha256: str
    settings_sha256: str
    workflow_sha256: str
    chapters: tuple[PlannedChapter, ...]
    planner_version: str = QWEN_SEGMENT_PLANNER_VERSION
    schema_version: int = BOOK_PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "planner_version": self.planner_version,
            "input_sha256": self.input_sha256,
            "settings_sha256": self.settings_sha256,
            "workflow_sha256": self.workflow_sha256,
            "chapters": [chapter.to_dict() for chapter in self.chapters],
        }

    @property
    def sha256(self) -> str:
        return sha256_bytes(_canonical_json(self.to_dict()))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BookPlan":
        if not isinstance(payload, dict):
            raise BookPlanError("Book plan root must be a JSON object.")
        version = payload.get("schema_version")
        if version != BOOK_PLAN_SCHEMA_VERSION:
            raise UnsupportedBookPlanVersion(version)
        try:
            raw_chapters = payload["chapters"]
            if not isinstance(raw_chapters, list):
                raise TypeError("chapters must be a list")
            plan = cls(
                schema_version=int(version),
                planner_version=str(payload["planner_version"]),
                input_sha256=str(payload["input_sha256"]),
                settings_sha256=str(payload["settings_sha256"]),
                workflow_sha256=str(payload["workflow_sha256"]),
                chapters=tuple(PlannedChapter.from_dict(chapter) for chapter in raw_chapters),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, BookPlanError):
                raise
            raise BookPlanError("Book plan is missing required fields or contains invalid values.") from exc

        indices = [chapter.index for chapter in plan.chapters]
        if indices != list(range(len(plan.chapters))):
            raise BookPlanError("Book plan chapter indices are not contiguous.")
        return plan


@dataclass(frozen=True)
class BookPlanStore:
    state_dir: Path
    plan_name: str = "book_plan.json"

    @property
    def path(self) -> Path:
        return self.state_dir / self.plan_name

    def load(self, *, expected_sha256: str | None = None) -> BookPlan:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise BookPlanError(f"Could not read book plan at {self.path}: {exc}") from exc

        plan = BookPlan.from_dict(payload)
        if expected_sha256 and plan.sha256 != expected_sha256:
            raise BookPlanError("Book plan hash does not match the checkpoint.")
        return plan

    def save(self, plan: BookPlan) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".book-plan.", suffix=".tmp", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(plan.to_dict(), file, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
