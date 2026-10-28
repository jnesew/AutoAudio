from __future__ import annotations

import contextlib
import copy
import io
import json
import math
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import soundfile as sf
import torch

from core.audio_assembly import _assembled_payloads
from provenance.ai_marking import validate_watermarked_artifact, write_ai_marking_manifest
from provenance import audio_watermark as aw
from provenance.payloads import PAYLOAD_SCHEMA, PAYLOAD_DERIVATION, attribution_id, validate_payloads
from provenance.signature import parse_signature_report
from provenance.signal import verify_signal
from provenance.verify import inspect_artifact, main, AI_TAGS


def record(name="narration", count=2400):
    return {
        "schema": PAYLOAD_SCHEMA, "derivation": PAYLOAD_DERIVATION,
        "message_bits": 16, "expected_bits": "01" * 8,
        "source_sha256": "a" * 64, "content_id": name,
        "attribution_id": attribution_id(name, "a" * 64),
        "sample_rate": 24000, "start_sample": 0, "sample_count": count,
        "embedding_strength": 1.0,
    }


def marked(path, payload=None):
    sf.write(path, np.zeros(2400, dtype="float32"), 24000)
    write_ai_marking_manifest(
        path, content_id=path.stem, metadata_embedded=True, watermark_applied=True,
        watermark_verified=True, watermark_detail="mock evidence",
        payloads=(payload,) if payload else (), payloads_complete=payload is not None,
    )
    return validate_watermarked_artifact(path)


def signature_report(payload=None, *, trusted=False, invalid=False):
    success = [{"code": "claimSignature.validated"}, {"code": "assertion.dataHash.match"}]
    if trusted:
        success.append({"code": "signingCredential.trusted"})
    failure = [{"code": "claimSignature.mismatch"}] if invalid else []
    data = {"watermark_evidence": {"payloads": [payload or record()], "complete": True, "provider": "ComfyUI"}}
    return {
        "active_manifest": "active", "validation_state": "Invalid" if invalid else "Trusted" if trusted else "Valid",
        "manifests": {"active": {"assertions": [{"label": "com.autoaudio.pipeline", "data": data}]}},
        "validation_results": {"activeManifest": {"success": success, "failure": failure}},
    }


def test_marking_retains_public_payload_identity_and_actual_detection_evidence(monkeypatch):
    message = torch.tensor([[0, 1] * 8])
    stream = io.BytesIO()
    sf.write(stream, np.ones(2400, dtype="float32") * .02, 24000, format="WAV")
    generator = SimpleNamespace(get_watermark=lambda wave, message: torch.zeros_like(wave))
    detector = SimpleNamespace(detect_watermark=lambda wave: (torch.tensor([1.]), message))
    monkeypatch.setattr(aw, "_derive_16bit_message", lambda *_: message)
    monkeypatch.setattr(aw, "_load_audioseal_models", lambda *_: (generator, detector))
    monkeypatch.setattr(aw, "_audioseal_inference_context", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(aw, "_audioseal_detector_context", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(aw.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=stream.getvalue()))
    evidence = {}
    output = aw.watermark_audio_bytes(b"original TTS bytes", content_id="speech", secret_key="not-published", evidence_out=evidence)
    assert evidence["expected_bits"] == "01" * 8
    assert evidence["generation_verification"]["bit_errors"] == 0
    assert evidence["generation_verification"]["presence_score"] == 1.
    assert evidence["sample_count"] == 2400
    assert "not-published" not in json.dumps(evidence)
    assert validate_payloads([evidence])[0]["attribution_id"] == evidence["attribution_id"]
    assert sf.info(io.BytesIO(output)).samplerate == 24000


@pytest.mark.parametrize("change", [
    {"expected_bits": "1"}, {"source_sha256": "invalid"}, {"sample_rate": 16000},
    {"start_sample": -1}, {"sample_count": 0}, {"attribution_id": "b" * 64},
    {"embedding_strength": float("nan")},
])
def test_invalid_public_payload_records_are_rejected(change):
    with pytest.raises(ValueError):
        validate_payloads([{**record(), **change}])


def test_payload_timeline_preserves_gaps_repetition_and_partial_legacy_coverage(tmp_path):
    first, second, gap = (tmp_path / name for name in ("first.flac", "second.flac", "gap.flac"))
    a = marked(first, record("first"))
    b = marked(second, record("second"))
    sf.write(gap, np.zeros(480, dtype="float32"), 24000)
    payloads, complete = _assembled_payloads([str(first), str(gap), str(second), str(first)], (a, b))
    assert complete
    assert [p["start_sample"] for p in payloads] == [0, 2880, 5280]
    assert [p["content_id"] for p in payloads] == ["first", "second", "first"]
    assert a.payloads[0]["start_sample"] == 0
    legacy = marked(tmp_path / "legacy.flac")
    _, complete = _assembled_payloads([str(first), legacy.path], (a, legacy))
    assert not complete


@pytest.mark.parametrize("trusted", [False, True])
def test_signature_validity_binding_and_trust_are_separate(trusted):
    result = parse_signature_report(signature_report(trusted=trusted))
    assert result["signature"] == "valid"
    assert result["binding"] == "valid"
    assert result["trust"] == ("trusted" if trusted else "untrusted")
    assert result["evidence"]["payloads"][0]["expected_bits"] == "01" * 8


def test_invalid_signature_never_supplies_authenticated_payload_evidence():
    result = parse_signature_report(signature_report(trusted=True, invalid=True))
    assert result["signature"] == "invalid"
    assert result["evidence"] is None


def test_signature_requires_positive_validation_not_merely_no_errors():
    report = signature_report()
    report["validation_results"]["activeManifest"]["success"] = []
    assert parse_signature_report(report)["signature"] == "invalid"


@pytest.mark.parametrize("errors,score,status", [
    (0, .5, "exact"), (1, .9, "partial"), (2, 1., "partial"),
    (3, 1., "mismatch"), (1, .89, "mismatch"), (0, .49, "mismatch"),
])
def test_signal_presence_and_payload_policy_boundaries(monkeypatch, tmp_path, errors, score, status):
    from provenance import signal
    decoded = torch.tensor([[0, 1] * 8])
    decoded[0, :errors] = 1 - decoded[0, :errors]
    detector = SimpleNamespace(detect_watermark=lambda wave: (torch.tensor([score], dtype=torch.float64), decoded))
    def decoded_spans(_artifact, records):
        yield records[0], np.zeros(2400, dtype="float32")
    monkeypatch.setattr(signal, "decode_signal_spans", decoded_spans)
    monkeypatch.setattr(aw, "_load_audioseal_models", lambda *_: (None, detector))
    monkeypatch.setattr(aw, "_audioseal_inference_context", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(aw, "_audioseal_detector_context", lambda *_: contextlib.nullcontext())
    result = verify_signal(tmp_path / "clip.flac", [record()], complete=True)
    assert result["detection"] == ("detected" if score >= .5 else "not_detected")
    assert result["payload"] == status
    assert result["spans"][0]["bit_errors"] == errors


@pytest.mark.parametrize("errors,score,accepted", [
    (0, .5, True), (1, .9, True), (2, 1., True), (3, 1., False), (1, .89, False),
])
def test_generation_partial_payload_policy(monkeypatch, errors, score, accepted):
    expected = torch.tensor([[0, 1] * 8])
    decoded = expected.clone()
    decoded[0, :errors] = 1 - decoded[0, :errors]
    stream = io.BytesIO()
    sf.write(stream, np.ones(2400, dtype="float32") * .02, 24000, format="WAV")
    generator = SimpleNamespace(get_watermark=lambda wave, message: torch.zeros_like(wave))
    detector = SimpleNamespace(detect_watermark=lambda wave: (torch.tensor([score], dtype=torch.float64), decoded))
    monkeypatch.setattr(aw, "_derive_16bit_message", lambda *_: expected)
    monkeypatch.setattr(aw, "_load_audioseal_models", lambda *_: (generator, detector))
    monkeypatch.setattr(aw, "_audioseal_inference_context", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(aw, "_audioseal_detector_context", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(aw.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=stream.getvalue()))
    evidence = {}
    result, _ = aw.watermark_audio_bytes_best_effort(b"raw audio", content_id="speech", device="cpu")
    assert result.verified is accepted
    if accepted:
        assert result.payload["generation_verification"]["bit_errors"] == errors
        if errors:
            assert "payload=partial" in result.detail
    if errors:
        with pytest.raises(RuntimeError, match="did not round-trip"):
            aw.watermark_audio_bytes(b"raw audio", content_id="speech", secret_key="public", evidence_out=evidence)


def test_exact_verifier_option_rejects_partial_recovery(monkeypatch, tmp_path):
    artifact = tmp_path / "partial.flac"
    marked(artifact, record())
    monkeypatch.setattr("provenance.verify._probe_tags", lambda *_: {**AI_TAGS, "ai_marking": "audio_watermark", "ai_provider": "ComfyUI"})
    monkeypatch.setattr("provenance.signal.verify_signal", lambda *_a, **_k: {
        "detection": "detected", "payload": "partial", "coverage": "complete", "spans": [],
    })
    assert inspect_artifact(artifact, check_signal=True)["checks_passed"]
    assert not inspect_artifact(artifact, check_signal=True, require_exact_payload=True)["checks_passed"]


def test_legacy_evidence_remains_auditable_without_inventing_payloads(tmp_path):
    from provenance.ai_marking import manifest_path_for, AI_MARKING_COMPATIBILITY_SCHEMA
    artifact = tmp_path / "legacy.flac"
    marked(artifact)
    sidecar = manifest_path_for(artifact)
    data = json.loads(sidecar.read_text())
    data["schema"] = AI_MARKING_COMPATIBILITY_SCHEMA
    sidecar.write_text(json.dumps(data))
    evidence = validate_watermarked_artifact(artifact)
    assert evidence.payloads == ()
    assert not evidence.payloads_complete


def test_aac_stream_decoder_preserves_recorded_sample_boundaries(tmp_path):
    import shutil
    import subprocess
    from provenance.signal import decode_signal_spans
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    source, encoded = tmp_path / "source.wav", tmp_path / "encoded.m4b"
    sf.write(source, np.zeros(10317, dtype="float32"), 24000)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-c:a", "aac", str(encoded)], check=True)
    records = [record("first", 5011), {**record("second", 5006), "start_sample": 5311}]
    spans = list(decode_signal_spans(encoded, records))
    assert [len(samples) for _, samples in spans] == [5011, 5006]


def test_signal_decoder_rejects_truncated_span(tmp_path):
    import shutil
    from provenance.signal import decode_signal_spans
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    artifact = tmp_path / "short.wav"
    sf.write(artifact, np.zeros(100, dtype="float32"), 24000)
    with pytest.raises(ValueError, match="truncated"):
        list(decode_signal_spans(artifact, [record()]))


def test_c2pa_signing_binds_payload_records_and_rejects_stale_audio(tmp_path):
    from provenance.c2pa import C2PAAssertionBuilder, ProvenanceRuntimeMetadata, ProvenanceError
    artifact = tmp_path / "signed.flac"
    marked(artifact, record())
    builder = C2PAAssertionBuilder(artifact_path=artifact, runtime_metadata=ProvenanceRuntimeMetadata(), embedding_path="chunk")
    data = builder._build_pipeline_assertion()["data"]
    assert data["watermark_evidence"]["payloads"] == [record()]
    assert data["watermark_evidence"]["complete"]
    artifact.write_bytes(b"changed audio")
    with pytest.raises(ProvenanceError, match="changed audio"):
        builder._build_pipeline_assertion()


def test_verifier_cli_exact_option_implies_signal_check(monkeypatch, tmp_path, capsys):
    artifact = tmp_path / "chapter.flac"
    artifact.write_bytes(b"mock audio")
    inspect = Mock(return_value={"checks_passed": False, "errors": ["partial recovery"]})
    monkeypatch.setattr("provenance.verify.inspect_artifact", inspect)
    assert main(["--file", str(artifact), "--require-exact-payload", "--json"]) == 1
    assert inspect.call_args.kwargs["check_signal"]
    assert inspect.call_args.kwargs["require_exact_payload"]
    assert json.loads(capsys.readouterr().out)["failures"] == 1


def test_signed_payload_can_be_verified_without_sidecar_or_original_job(monkeypatch, tmp_path):
    artifact = tmp_path / "shared.flac"
    artifact.write_bytes(b"signed audio")
    signed = parse_signature_report(signature_report())
    monkeypatch.setattr("provenance.signature.inspect_signature", lambda *_a, **_k: signed)
    monkeypatch.setattr("provenance.verify._probe_tags", lambda *_: {**AI_TAGS, "ai_marking": "audio_watermark"})
    signal = Mock(return_value={"detection": "detected", "payload": "exact", "coverage": "complete", "spans": []})
    monkeypatch.setattr("provenance.signal.verify_signal", signal)
    result = inspect_artifact(artifact, check_signal=True)
    assert result["checks_passed"]
    assert result["evidence_source"] == "signed-c2pa"
    assert result["signature"]["trust"] == "untrusted"
    assert signal.call_args.args[1][0]["expected_bits"] == "01" * 8
    assert not inspect_artifact(artifact, check_signature=True, require_trusted=True)["checks_passed"]


def test_sidecar_cannot_override_the_signed_expected_payload(monkeypatch, tmp_path):
    artifact = tmp_path / "shared.flac"
    marked(artifact, record())
    different = {**record(), "expected_bits": "10" * 8}
    monkeypatch.setattr("provenance.signature.inspect_signature", lambda *_a, **_k: parse_signature_report(signature_report(different)))
    monkeypatch.setattr("provenance.verify._probe_tags", lambda *_: {**AI_TAGS, "ai_marking": "audio_watermark", "ai_provider": "ComfyUI"})
    result = inspect_artifact(artifact, check_signature=True)
    assert not result["checks_passed"]
    assert any("differs from the signed" in error for error in result["errors"])


@pytest.mark.parametrize("steps,accepted", [(1, True), (-1, True), (2, False), (-2, False)])
def test_signed_metric_json_roundtrip_allows_only_one_float_step(monkeypatch, tmp_path, steps, accepted):
    artifact = tmp_path / "signed.flac"
    payload = {**record(), "generation_verification": {"presence_score": 0.9975879788398743, "bit_errors": 0}}
    marked(artifact, payload)
    signed = copy.deepcopy(payload)
    for _ in range(abs(steps)):
        signed["generation_verification"]["presence_score"] = math.nextafter(
            signed["generation_verification"]["presence_score"], math.inf if steps > 0 else -math.inf,
        )
    monkeypatch.setattr("provenance.signature.inspect_signature", lambda *_a, **_k: parse_signature_report(signature_report(signed)))
    monkeypatch.setattr("provenance.verify._probe_tags", lambda *_: {**AI_TAGS, "ai_marking": "audio_watermark", "ai_provider": "ComfyUI"})
    result = inspect_artifact(artifact, check_signature=True)
    assert result["checks_passed"] is accepted
    assert result["evidence_source"] == "signed-c2pa"


@pytest.mark.parametrize("change", [{"start_sample": 1}, {"sample_count": 2401}, {"expected_bits": "10" * 8}])
def test_signed_payload_identity_and_timeline_still_require_exact_agreement(monkeypatch, tmp_path, change):
    artifact = tmp_path / "signed.flac"
    marked(artifact, record())
    signed = {**record(), **change}
    monkeypatch.setattr("provenance.signature.inspect_signature", lambda *_a, **_k: parse_signature_report(signature_report(signed)))
    monkeypatch.setattr("provenance.verify._probe_tags", lambda *_: {**AI_TAGS, "ai_marking": "audio_watermark", "ai_provider": "ComfyUI"})
    result = inspect_artifact(artifact, check_signature=True)
    assert not result["checks_passed"]
    assert any("differs from the signed" in error for error in result["errors"])
