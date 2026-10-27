from __future__ import annotations

import sys
from unittest.mock import patch
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.checkpoint import CheckpointStore, UnsupportedCheckpointVersion, CheckpointError, create_initial_checkpoint, validate_artifact


def test_checkpoint_v2_round_trip(tmp_path):
    store = CheckpointStore(tmp_path)
    checkpoint = create_initial_checkpoint(
        input_path="book.txt",
        input_hash="input-hash",
        settings_hash="settings-hash",
        workflow_hash="workflow-hash",
        plan_path=str(tmp_path / "book_plan.json"),
        plan_hash="plan-hash",
        output_dir="output",
        output_format="flac",
        ui_state={"input_book": "book.txt"},
    )

    store.save(checkpoint)
    loaded = store.load()

    assert loaded is not None
    assert loaded["version"] == 2
    assert loaded["compatibility"]["workflow_sha256"] == "workflow-hash"
    assert loaded["plan"]["sha256"] == "plan-hash"
    assert loaded["artifacts"]["silence"] == {}
    assert loaded["artifacts"]["chapter_masters"] == {}
    assert loaded["artifacts"]["part_masters"] == {}


def test_checkpoint_v1_is_explicitly_incompatible(tmp_path):
    store = CheckpointStore(tmp_path)
    store.save({"version": 1, "status": "failed"})

    with pytest.raises(UnsupportedCheckpointVersion, match="Start a new run"):
        store.load()


def test_checkpoint_write_failure_preserves_previous_file_and_cleans_temporary_file(tmp_path):
    store = CheckpointStore(tmp_path)
    store.save({"version": 2, "status": "cancelled"})
    original = store.path.read_bytes()
    with patch("core.checkpoint.os.fsync", side_effect=OSError("disk failure")):
        with pytest.raises(OSError, match="disk failure"):
            store.save({"version": 2, "status": "running"})
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob(".checkpoint.*.tmp"))


@pytest.mark.parametrize("field", ("input", "compatibility", "plan", "output", "progress", "artifacts", "ui_state"))
def test_wrong_checkpoint_field_shapes_are_rejected(tmp_path, field):
    store = CheckpointStore(tmp_path)
    store.save({"version": 2, field: []})
    with pytest.raises(CheckpointError, match=field):
        store.load()


def test_unhashed_artifact_is_not_trusted_for_resume(tmp_path):
    audio = tmp_path / "audio.flac"
    audio.write_bytes(b"data without integrity evidence")
    assert not validate_artifact(str(audio), None)
