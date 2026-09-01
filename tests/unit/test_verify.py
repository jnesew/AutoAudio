from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from provenance.ai_marking import write_ai_marking_manifest
from provenance.verify import AI_TAGS, _iter_audio_files, verify_artifact


def _marked_artifact(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio-container")
    write_ai_marking_manifest(
        path,
        content_id=path.stem,
        metadata_embedded=True,
        watermark_applied=True,
        watermark_verified=True,
        watermark_detail="test",
    )
    return path


def test_verifier_accepts_matching_final_hash_and_tags(tmp_path):
    artifact = _marked_artifact(tmp_path / "Chapter_001.flac")
    tags = {**AI_TAGS, "ai_marking": "audio_watermark+metadata+manifest"}

    with patch("provenance.verify._probe_tags", return_value=tags):
        ok, errors = verify_artifact(artifact)

    assert ok is True
    assert errors == []


def test_verifier_rejects_artifact_mutated_after_sidecar_hash(tmp_path):
    artifact = _marked_artifact(tmp_path / "Chapter_001.flac")
    artifact.write_bytes(b"mutated-after-sidecar")
    tags = {**AI_TAGS, "ai_marking": "audio_watermark+metadata+manifest"}

    with patch("provenance.verify._probe_tags", return_value=tags):
        ok, errors = verify_artifact(artifact)

    assert ok is False
    assert any("hash mismatch" in error for error in errors)


def test_verifier_ignores_internal_state_and_optionally_includes_segments(tmp_path):
    chapter = _marked_artifact(tmp_path / "Chapter_001.flac")
    segment = _marked_artifact(tmp_path / ".segments" / "segment.flac")
    _marked_artifact(tmp_path / ".autoaudio_state" / "silence" / "gap.flac")

    assert _iter_audio_files(tmp_path) == [chapter]
    assert _iter_audio_files(tmp_path, include_segments=True) == [segment, chapter]
