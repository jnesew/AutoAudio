from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

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
    media_type_for_artifact,
    parse_model_identity_version,
    validate_assertions,
)


def _runtime() -> ProvenanceRuntimeMetadata:
    return ProvenanceRuntimeMetadata(
        model_name="Qwen3-TTS",
        model_version="1.7B",
        backend_name="FB_Qwen3TTSCustomVoice",
        backend_version="unreported",
        software_name="AutoAudio",
        software_version="2.0.0.dev0",
    )


def test_embedding_path_for_supported_containers():
    assert embedding_path_for_artifact("chapter.mp3") == "id3v2"
    assert embedding_path_for_artifact("chapter.m4b") == "mp4:c2pa-uuid-box"
    assert embedding_path_for_artifact("chapter.wav") == "chunk"
    assert media_type_for_artifact("chapter.mp3") == "audio/mpeg"
    assert media_type_for_artifact("chapter.m4b") == "audio/mp4"
    assert media_type_for_artifact("chapter.flac") == "audio/flac"


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


def test_assertion_builder_records_source_hash_without_forging_c2pa_hard_binding(tmp_path):
    artifact = tmp_path / "book.flac"
    payload = b"hello-audio-payload"
    artifact.write_bytes(payload)

    builder = C2PAAssertionBuilder(artifact_path=artifact, runtime_metadata=_runtime(), embedding_path="chunk")
    assertions = builder.build()
    by_label = {entry["label"]: entry for entry in assertions}

    assert "c2pa.hash.data" not in by_label
    assert by_label["com.autoaudio.pipeline"]["data"]["source_sha256"] == hashlib.sha256(payload).hexdigest()
    assert by_label["com.autoaudio.pipeline"]["data"]["source_hash_scope"] == "pre-c2pa-embedding"
    assert by_label["c2pa.actions"]["data"]["actions"][0]["action"] == "c2pa.created"


def test_validate_assertions_fails_for_missing_fields():
    with pytest.raises(ProvenanceError, match="missing required field"):
        validate_assertions(
            [
                {"label": "c2pa.ai.generative", "data": {"generator": {"name": "Qwen3-TTS", "version": ""}}},
                {"label": "c2pa.actions", "data": {"actions": [{"action": "c2pa.created"}]}},
                {
                    "label": "com.autoaudio.pipeline",
                    "data": {
                        "artifact": "book.flac",
                        "source_sha256": "abc",
                        "source_hash_scope": "pre-c2pa-embedding",
                    },
                },
            ]
        )


def test_parse_model_identity_version():
    assert parse_model_identity_version("Qwen3-TTS-1.7B") == ("Qwen3-TTS", "1.7B")
    assert parse_model_identity_version("custom-model") == ("custom-model", "unknown")


def test_successful_embedding_reports_distinct_source_and_final_hashes(tmp_path):
    artifact = tmp_path / "book.flac"
    artifact.write_bytes(b"unsigned-audio")
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    captured_manifest = {}

    def fake_c2pa_tool(*, input_path, output_path, manifest_path, config):
        del config
        captured_manifest.update(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
        Path(output_path).write_bytes(Path(input_path).read_bytes() + b"-signed")

    from provenance.c2pa import apply_c2pa_provenance

    with patch("provenance.c2pa._run_c2patool", side_effect=fake_c2pa_tool):
        result = apply_c2pa_provenance(
            artifact_path=artifact,
            config=ProvenanceConfig(enabled=True, cert_path=str(cert), key_path=str(key)),
            runtime_metadata=_runtime(),
        )

    assert result is not None
    assert result.source_sha256 == hashlib.sha256(b"unsigned-audio").hexdigest()
    assert result.final_sha256 == hashlib.sha256(b"unsigned-audio-signed").hexdigest()
    assert artifact.read_bytes() == b"unsigned-audio-signed"
    assert captured_manifest["format"] == "audio/flac"
    assert all(assertion["label"] != "c2pa.hash.data" for assertion in captured_manifest["assertions"])


def test_claim_generator_default_tracks_explicit_build_version(monkeypatch):
    monkeypatch.setenv("AUTOAUDIO_VERSION", "2.0.0-rc1")

    assert ProvenanceConfig().claim_generator == "AutoAudio/2.0.0-rc1"
