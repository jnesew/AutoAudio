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
    C2PA_TRAINED_ALGORITHMIC_MEDIA,
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
    assert (
        by_label["c2pa.actions"]["data"]["actions"][0]["digitalSourceType"]
        == C2PA_TRAINED_ALGORITHMIC_MEDIA
    )


def test_validate_assertions_fails_for_missing_fields():
    with pytest.raises(ProvenanceError, match="missing required field") as error:
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

    assert "created action is missing 'digitalSourceType'" in str(error.value)


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

    with patch("provenance.c2pa._run_c2patool", side_effect=fake_c2pa_tool), patch(
        "provenance.c2pa._inspect_c2pa_output", return_value="urn:c2pa:test:autoaudio"
    ):
        result = apply_c2pa_provenance(
            artifact_path=artifact,
            config=ProvenanceConfig(enabled=True, cert_path=str(cert), key_path=str(key)),
            runtime_metadata=_runtime(),
        )

    assert result is not None
    assert result.manifest_id == "urn:c2pa:test:autoaudio"
    assert result.source_sha256 == hashlib.sha256(b"unsigned-audio").hexdigest()
    assert result.final_sha256 == hashlib.sha256(b"unsigned-audio-signed").hexdigest()
    assert artifact.read_bytes() == b"unsigned-audio-signed"
    assert captured_manifest["format"] == "audio/flac"
    assert captured_manifest["alg"] == "es256"
    assert captured_manifest["sign_cert"] == str(cert.resolve())
    assert captured_manifest["private_key"] == str(key.resolve())
    assert all(assertion["label"] != "c2pa.hash.data" for assertion in captured_manifest["assertions"])


def test_m4b_signing_uses_same_filesystem_m4a_alias(tmp_path):
    artifact = tmp_path / "book.m4b"
    artifact.write_bytes(b"unsigned-mp4-audio")
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    captured_paths: dict[str, Path] = {}

    def fake_c2pa_tool(*, input_path, output_path, manifest_path, config):
        del manifest_path, config
        captured_paths["input"] = Path(input_path)
        captured_paths["output"] = Path(output_path)
        assert Path(input_path).read_bytes() == b"unsigned-mp4-audio"
        Path(output_path).write_bytes(b"signed-mp4-audio")

    def fake_inspect(*, artifact_path, config):
        del config
        captured_paths["inspect"] = Path(artifact_path)
        assert Path(artifact_path).read_bytes() == b"signed-mp4-audio"
        return "urn:c2pa:test:m4b"

    from provenance.c2pa import apply_c2pa_provenance

    with patch("provenance.c2pa._run_c2patool", side_effect=fake_c2pa_tool), patch(
        "provenance.c2pa._inspect_c2pa_output", side_effect=fake_inspect
    ):
        result = apply_c2pa_provenance(
            artifact_path=artifact,
            config=ProvenanceConfig(enabled=True, cert_path=str(cert), key_path=str(key)),
            runtime_metadata=_runtime(),
        )

    assert result is not None
    assert result.manifest_id == "urn:c2pa:test:m4b"
    assert artifact.read_bytes() == b"signed-mp4-audio"
    assert captured_paths["input"].suffix == ".m4a"
    assert captured_paths["output"].suffix == ".m4a"
    assert captured_paths["inspect"] == captured_paths["output"]
    assert captured_paths["input"].parent.parent == tmp_path


def test_failed_signed_output_validation_preserves_original_artifact(tmp_path):
    artifact = tmp_path / "book.flac"
    artifact.write_bytes(b"unsigned-audio")
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    def fake_c2pa_tool(*, input_path, output_path, manifest_path, config):
        del input_path, manifest_path, config
        Path(output_path).write_bytes(b"invalid-signed-audio")

    from provenance.c2pa import apply_c2pa_provenance

    with patch("provenance.c2pa._run_c2patool", side_effect=fake_c2pa_tool), patch(
        "provenance.c2pa._inspect_c2pa_output",
        side_effect=ProvenanceError("verification failed"),
    ), pytest.raises(ProvenanceError, match="verification failed"):
        apply_c2pa_provenance(
            artifact_path=artifact,
            config=ProvenanceConfig(enabled=True, cert_path=str(cert), key_path=str(key)),
            runtime_metadata=_runtime(),
        )

    assert artifact.read_bytes() == b"unsigned-audio"
    assert not list(tmp_path.glob(".autoaudio-c2pa-*"))


def test_c2patool_reads_signing_credentials_from_manifest(tmp_path):
    from provenance.c2pa import _run_c2patool

    config = ProvenanceConfig(
        enabled=True,
        cert_path=str(tmp_path / "cert.pem"),
        key_path=str(tmp_path / "key.pem"),
    )
    manifest = tmp_path / "manifest.json"
    source = tmp_path / "source.flac"
    output = tmp_path / "signed.flac"

    with patch("provenance.c2pa.shutil.which", return_value="/usr/bin/c2patool"), patch(
        "provenance.c2pa.subprocess.run"
    ) as run:
        _run_c2patool(
            input_path=str(source),
            output_path=str(output),
            manifest_path=str(manifest),
            config=config,
        )

    command = run.call_args.args[0]
    assert command == [
        "c2patool",
        str(source),
        "--manifest",
        str(manifest),
        "--output",
        str(output),
    ]
    assert "--sign_cert" not in command
    assert "--private_key" not in command


def test_c2pa_report_accepts_untrusted_test_certificate():
    from provenance.c2pa import _active_manifest_from_report

    manifest_id = _active_manifest_from_report(
        {
            "active_manifest": "urn:c2pa:test:autoaudio",
            "validation_status": [
                {
                    "code": "signingCredential.untrusted",
                    "explanation": "signing certificate untrusted",
                }
            ],
        }
    )

    assert manifest_id == "urn:c2pa:test:autoaudio"


def test_c2pa_report_rejects_malformed_assertion():
    from provenance.c2pa import _active_manifest_from_report

    with pytest.raises(ProvenanceError, match="created action must have a digitalSourceType"):
        _active_manifest_from_report(
            {
                "active_manifest": "urn:c2pa:test:autoaudio",
                "validation_status": [
                    {
                        "code": "assertion.action.malformed",
                        "explanation": "c2pa.created action must have a digitalSourceType",
                    }
                ],
            }
        )


def test_claim_generator_default_tracks_explicit_build_version(monkeypatch):
    monkeypatch.setenv("AUTOAUDIO_VERSION", "2.0.0-rc1")

    assert ProvenanceConfig().claim_generator == "AutoAudio/2.0.0-rc1"
