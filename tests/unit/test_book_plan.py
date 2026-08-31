from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.plan import BookPlan, BookPlanError, BookPlanStore, PlannedChapter, PlannedSegment
from core.segmentation import QWEN_SEGMENT_PLANNER_VERSION


def _plan() -> BookPlan:
    return BookPlan(
        input_sha256="input-hash",
        settings_sha256="settings-hash",
        workflow_sha256="workflow-hash",
        chapters=(
            PlannedChapter(
                index=0,
                title="Chapter One",
                segments=(PlannedSegment.from_text(chapter_index=0, segment_index=0, text="Hello world."),),
            ),
        ),
    )


def test_book_plan_round_trip_and_hash_are_stable(tmp_path):
    store = BookPlanStore(tmp_path)
    plan = _plan()

    store.save(plan)
    loaded = store.load(expected_sha256=plan.sha256)

    assert loaded == plan
    assert loaded.sha256 == plan.sha256
    assert loaded.planner_version == QWEN_SEGMENT_PLANNER_VERSION


def test_book_plan_rejects_tampered_segment_text(tmp_path):
    store = BookPlanStore(tmp_path)
    store.save(_plan())
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["chapters"][0]["segments"][0]["text"] = "Tampered text."
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BookPlanError, match="text hash"):
        store.load()


def test_book_plan_rejects_checkpoint_hash_mismatch(tmp_path):
    store = BookPlanStore(tmp_path)
    store.save(_plan())

    with pytest.raises(BookPlanError, match="checkpoint"):
        store.load(expected_sha256="wrong")
