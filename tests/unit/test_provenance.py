from __future__ import annotations

import base64
import hashlib
import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from provenance.c2pa import (  # noqa: E402
    C2PAAssertionBuilder,
    ProvenanceConfig,
    ProvenanceError,
    ProvenanceRuntimeMetadata,
    apply_c2pa_with_policy,
    embedding_path_for_artifact,
    parse_model_identity_version,
    validate_assertions,
)


def _runtime() -> ProvenanceRuntimeMetadata:
    return ProvenanceRuntimeMetadata(
        model_name="VibeVoice",
        model_version="1.5B",
        backend_name="VibeVoiceSingleSpeakerNode",
        backend_version="VibeVoice Single Speaker",
        software_name="AutoAudio",
        software_version="dev",
    )


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
    result = apply_c2pa_with_policy(
        artifact_path=artifact,
        config=config,
        runtime_metadata=_runtime(),
        logger=logging.getLogger("test"),
    )
    assert result is None


def test_hard_fail_raises_when_credentials_missing(tmp_path):
    artifact = tmp_path / "book.flac"
    artifact.write_bytes(b"audio")
    config = ProvenanceConfig(enabled=True, cert_path="", key_path="", hard_fail=True)
    with pytest.raises(ProvenanceError):
        apply_c2pa_with_policy(
            artifact_path=artifact,
            config=config,
            runtime_metadata=_runtime(),
            logger=logging.getLogger("test"),
        )


def test_assertion_builder_includes_hash_data(tmp_path):
    artifact = tmp_path / "book.flac"
    payload = b"hello-audio-payload"
    artifact.write_bytes(payload)

    builder = C2PAAssertionBuilder(artifact_path=artifact, runtime_metadata=_runtime(), embedding_path="chunk")
    assertions = builder.build()
    by_label = {entry["label"]: entry for entry in assertions}

    digest_b64 = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    assert by_label["c2pa.hash.data"]["data"]["hash"] == digest_b64
    assert by_label["c2pa.actions"]["data"]["actions"][0]["action"] == "c2pa.created"


def test_validate_assertions_fails_for_missing_fields():
    with pytest.raises(ProvenanceError, match="missing required field"):
        validate_assertions(
            [
                {"label": "c2pa.ai.generative", "data": {"generator": {"name": "VibeVoice", "version": ""}}},
                {"label": "c2pa.actions", "data": {"actions": [{"action": "c2pa.created"}]}},
                {"label": "c2pa.hash.data", "data": {"alg": "sha256", "hash": "abc"}},
            ]
        )


def test_parse_model_identity_version():
    assert parse_model_identity_version("VibeVoice-1.5B") == ("VibeVoice", "1.5B")
    assert parse_model_identity_version("custom-model") == ("custom", "model")
