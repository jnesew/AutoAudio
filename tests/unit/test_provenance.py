from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from provenance.c2pa import ProvenanceConfig, ProvenanceError, apply_c2pa_with_policy, embedding_path_for_artifact


def test_embedding_path_for_supported_containers():
    assert embedding_path_for_artifact("chapter.mp3") == "id3v2"
    assert embedding_path_for_artifact("chapter.m4b") == "mp4:c2pa-uuid-box"
    assert embedding_path_for_artifact("chapter.wav") == "chunk"


def test_embedding_path_rejects_unknown_extension():
    with pytest.raises(ProvenanceError):
        embedding_path_for_artifact("chapter.ogg")


def test_soft_fail_returns_none_when_credentials_missing(tmp_path):
    artifact = tmp_path / "book.flac"
    artifact.write_bytes(b"audio")
    config = ProvenanceConfig(enabled=True, cert_path="", key_path="", hard_fail=False)
    result = apply_c2pa_with_policy(artifact_path=artifact, config=config, logger=logging.getLogger("test"))
    assert result is None


def test_hard_fail_raises_when_credentials_missing(tmp_path):
    artifact = tmp_path / "book.flac"
    artifact.write_bytes(b"audio")
    config = ProvenanceConfig(enabled=True, cert_path="", key_path="", hard_fail=True)
    with pytest.raises(ProvenanceError):
        apply_c2pa_with_policy(artifact_path=artifact, config=config, logger=logging.getLogger("test"))
