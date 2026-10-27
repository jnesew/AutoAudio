from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from core.cancellation import CancellationToken
from core.errors import PipelineCancelled, ResumeStateError
from core.job_lock import output_job_lock
from core.pipeline import build_app_config, build_argument_parser, run_pipeline
from core.plan import BookPlan, PlannedChapter, PlannedSegment
from core.checkpoint import CheckpointStore


ROOT = Path(__file__).resolve().parents[2]


def test_job_lock_excludes_another_process_and_releases_after_failure(tmp_path):
    state = tmp_path / ".autoaudio_state"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    code = (
        "from core.job_lock import output_job_lock; from pathlib import Path; "
        f"with_lock = output_job_lock(Path({str(state)!r})); with_lock.__enter__()"
    )
    with pytest.raises(RuntimeError, match="test failure"):
        with output_job_lock(state):
            probe = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=10)
            assert probe.returncode != 0
            assert "already in use" in probe.stderr
            raise RuntimeError("test failure")
    with output_job_lock(state):
        assert (state / "job.lock").is_file()


@pytest.fixture
def cancelled_job(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("An opening sentence with enough text to form a planned narration segment.", encoding="utf-8")
    output = tmp_path / "output"
    args = build_argument_parser(ROOT).parse_args([
        "--input-book", str(source), "--output-dir", str(output), "--comfyui-mode", "spoof",
    ])
    cancellation = CancellationToken()
    cancellation.cancel()
    with pytest.raises(PipelineCancelled):
        run_pipeline(args, build_app_config(args, ROOT), cancellation=cancellation)
    state = output / ".autoaudio_state"
    return args, state


@pytest.mark.parametrize("resume", ("auto", "no"))
def test_new_or_incompatible_job_preserves_existing_checkpoint_and_plan(cancelled_job, resume):
    args, state = cancelled_job
    checkpoint = (state / "checkpoint_state.json").read_bytes()
    plan = (state / "book_plan.json").read_bytes()
    args.resume = resume
    if resume == "auto":
        args.language = "Finnish"
    with patch("core.pipeline.process_segment", side_effect=AssertionError("must not synthesize")):
        with pytest.raises(ResumeStateError, match="existing work was preserved"):
            run_pipeline(args, build_app_config(args, ROOT))
    assert (state / "checkpoint_state.json").read_bytes() == checkpoint
    assert (state / "book_plan.json").read_bytes() == plan


@pytest.mark.parametrize("corrupt", ("checkpoint_state.json", "book_plan.json"))
def test_automatic_resume_preserves_corrupt_recovery_files(cancelled_job, corrupt):
    args, state = cancelled_job
    (state / corrupt).write_text("[]", encoding="utf-8")
    previous = {path.name: path.read_bytes() for path in state.glob("*.json")}
    with pytest.raises(ResumeStateError, match="preserved"):
        run_pipeline(args, build_app_config(args, ROOT))
    assert {path.name: path.read_bytes() for path in state.glob("*.json")} == previous


def test_loose_audio_is_protected_without_a_checkpoint(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("A short book to test existing output protection.", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    audio = output / "Chapter_001.flac"
    audio.write_bytes(b"existing recording")
    args = build_argument_parser(ROOT).parse_args([
        "--input-book", str(source), "--output-dir", str(output), "--comfyui-mode", "spoof",
    ])
    with pytest.raises(ResumeStateError, match="preserved"):
        run_pipeline(args, build_app_config(args, ROOT))
    assert audio.read_bytes() == b"existing recording"
    assert not (output / ".autoaudio_state" / "checkpoint_state.json").exists()


def test_old_unbounded_plan_is_rejected_before_synthesis(cancelled_job):
    args, state = cancelled_job
    payload = json.loads((state / "book_plan.json").read_text())
    plan = BookPlan.from_dict(payload)
    old_plan = BookPlan(
        input_sha256=plan.input_sha256, settings_sha256=plan.settings_sha256,
        workflow_sha256=plan.workflow_sha256, planner_version="qwen-semantic-v1",
        chapters=(PlannedChapter(index=0, title="Old chapter", segments=(
            PlannedSegment.from_text(chapter_index=0, segment_index=0, text="長" * 20_000),
        )),),
    )
    (state / "book_plan.json").write_text(json.dumps(old_plan.to_dict()))
    store = CheckpointStore(state)
    checkpoint = store.load()
    checkpoint["plan"]["sha256"] = old_plan.sha256
    store.save(checkpoint)
    previous = store.path.read_bytes()
    with patch("core.pipeline.process_segment", side_effect=AssertionError("must not synthesize")):
        with pytest.raises(ResumeStateError, match="safety limits"):
            run_pipeline(args, build_app_config(args, ROOT))
    assert store.path.read_bytes() == previous


def test_bounded_old_plan_remains_resumable_without_resegmentation(cancelled_job):
    args, state = cancelled_job
    payload = json.loads((state / "book_plan.json").read_text())
    payload["planner_version"] = "qwen-semantic-v1"
    old_plan = BookPlan.from_dict(payload)
    (state / "book_plan.json").write_text(json.dumps(old_plan.to_dict()))
    store = CheckpointStore(state)
    checkpoint = store.load()
    checkpoint["plan"]["sha256"] = old_plan.sha256
    store.save(checkpoint)
    cancellation = CancellationToken()
    cancellation.cancel()
    with patch("core.pipeline.build_qwen_book_plan", side_effect=AssertionError("must retain frozen plan")):
        with pytest.raises(PipelineCancelled):
            run_pipeline(args, build_app_config(args, ROOT), cancellation=cancellation)
    assert json.loads((state / "book_plan.json").read_text()) == old_plan.to_dict()
