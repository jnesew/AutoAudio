from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    source_root = Path(__file__).resolve().parents[1]
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from provenance.ai_marking import AI_MARKING_COMMENT, SUPPORTED_MARKING_SCHEMAS, LEGACY_COMFYUI_AI_MARKING_COMMENT, load_ai_marking_manifest
from provenance.payloads import validate_payloads


AI_TAGS = {
    "ai_generated": "true",
    "ai_system": "AutoAudio",
}
AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".opus", ".m4b", ".mp4", ".m4a"}


def _iter_audio_files(base_dir: Path, *, include_segments: bool = False) -> list[Path]:
    candidates: list[Path] = []
    for path in base_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        relative_parts = path.relative_to(base_dir).parts
        if ".autoaudio_state" in relative_parts:
            continue
        if ".segments" in relative_parts and not include_segments:
            continue
        candidates.append(path)
    return sorted(candidates)


def _manifest_path(artifact: Path) -> Path:
    return artifact.with_suffix(f"{artifact.suffix}.ai.json")


def _load_manifest(manifest_path: Path) -> dict:
    return load_ai_marking_manifest(manifest_path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _probe_tags(artifact: Path) -> dict[str, str]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format_tags",
        "-of",
        "json",
        str(artifact),
    ]
    output = subprocess.check_output(command, text=True, timeout=30)
    payload = json.loads(output)
    return {str(key).lower(): str(value) for key, value in (payload.get("format", {}).get("tags") or {}).items()}


def verify_artifact(artifact: Path) -> tuple[bool, list[str]]:
    errors: list[str] = []
    manifest: dict = {}

    manifest_path = _manifest_path(artifact)
    if not manifest_path.exists():
        errors.append(f"missing manifest: {manifest_path}")
    else:
        try:
            manifest = _load_manifest(manifest_path)
        except Exception as exc:
            errors.append(f"invalid manifest ({manifest_path}): {exc}")
            manifest = {}

        if manifest.get("schema") not in SUPPORTED_MARKING_SCHEMAS:
            errors.append(f"unexpected manifest schema for {artifact.name}: {manifest.get('schema')!r}")
        if manifest.get("artifact") != artifact.name:
            errors.append(f"manifest artifact name mismatch for {artifact.name}")
        if manifest.get("artifact_hash_scope") != "entire-artifact-bytes":
            errors.append(f"manifest hash scope mismatch for {artifact.name}")
        if not isinstance(manifest.get("provider"), str) or not manifest.get("provider", "").strip():
            errors.append(f"manifest provider is missing for {artifact.name}")
        expected_sha256 = manifest.get("artifact_sha256")
        try:
            actual_sha256 = _sha256_file(artifact)
        except OSError as exc:
            errors.append(f"could not read artifact {artifact}: {exc}")
            actual_sha256 = None
        if expected_sha256 != actual_sha256:
            errors.append(
                f"manifest artifact hash mismatch for {artifact.name}: "
                f"stored={expected_sha256!r}, actual={actual_sha256}"
            )

        watermark = (manifest.get("marking_methods") or {}).get("audio_watermark") or {}
        if not watermark.get("applied"):
            errors.append(f"watermark not applied in manifest for {artifact.name}")
        if not watermark.get("verified"):
            errors.append(f"watermark not verified in manifest for {artifact.name}")

    try:
        tags = _probe_tags(artifact)
    except Exception as exc:
        errors.append(f"ffprobe failed for {artifact}: {exc}")
        tags = {}

    portable_comment_marking = (
        artifact.suffix.lower() in {".m4b", ".m4a", ".mp4"}
        and tags.get("comment", "").casefold()
        in {AI_MARKING_COMMENT.casefold(), LEGACY_COMFYUI_AI_MARKING_COMMENT.casefold()}
    )
    if not portable_comment_marking:
        for key, expected in AI_TAGS.items():
            actual = tags.get(key)
            if actual is None or actual.lower() != expected.lower():
                errors.append(f"metadata tag mismatch for {artifact.name}: {key}={actual!r}, expected {expected!r}")

        if not tags.get("ai_marking", ""):
            errors.append(f"metadata tag missing for {artifact.name}: ai_marking")
        expected_provider = manifest.get("provider")
        actual_provider = tags.get("ai_provider")
        if expected_provider and (actual_provider is None or actual_provider.casefold() != str(expected_provider).casefold()):
            errors.append(
                f"metadata tag mismatch for {artifact.name}: "
                f"ai_provider={actual_provider!r}, expected {expected_provider!r}"
            )

    return (not errors), errors


def _same_evidence(left, right) -> bool:
    """Allow one binary64 serialization step without relaxing payload identity."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_evidence(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_evidence(a, b) for a, b in zip(left, right))
    if type(left) is float and type(right) is float:
        # C2PA's JSON rendering can shift a stored metric by one representable
        # step. Signed evidence remains authoritative for all subsequent checks.
        return math.isfinite(left) and math.isfinite(right) and (
            left == right or math.nextafter(left, right) == right
        )
    return type(left) is type(right) and left == right


def inspect_artifact(
    artifact: Path, *, check_signal=False, check_signature=False, require_trusted=False,
    tool="c2patool", trust_anchors=None, device="cpu",
    require_exact_payload=False,
) -> dict:
    metadata_ok, metadata_errors = verify_artifact(artifact)
    errors = list(metadata_errors)
    report = {
        "artifact": str(artifact),
        "integrity": {"status": "valid" if metadata_ok else "invalid", "method": "sidecar-hash-and-metadata"},
        "signature": {"signature": "not_checked", "binding": "not_checked", "trust": "unknown"},
        "signal": {"detection": "not_checked", "payload": "not_checked", "coverage": "not_checked", "spans": []},
        "evidence_source": "unavailable",
        "errors": errors,
    }
    marking = None
    try:
        if _manifest_path(artifact).is_file():
            marking = _load_manifest(_manifest_path(artifact))
    except ValueError:
        pass  # The ordinary metadata audit already records this failure.
    evidence = None
    if marking and marking.get("watermark_payloads"):
        evidence = {
            "payloads": marking["watermark_payloads"],
            "complete": marking["watermark_payloads_complete"],
            "provider": marking.get("provider"),
        }
        report["evidence_source"] = "unsigned-sidecar"
    need_signature = check_signature or require_trusted or bool(trust_anchors) or (check_signal and marking is None)
    if need_signature:
        from provenance.signature import inspect_signature
        try:
            signature = inspect_signature(artifact, tool=tool, trust_anchors=trust_anchors)
            report["signature"] = {key: value for key, value in signature.items() if key != "evidence"}
            signed = signature.get("evidence")
            if signature["signature"] != "valid" or signature["binding"] != "valid":
                errors.append("C2PA signature or asset binding is absent or invalid.")
            elif signed:
                if evidence and not _same_evidence(evidence, signed):
                    errors.append("Sidecar watermark evidence differs from the signed C2PA record.")
                evidence = signed
                report["evidence_source"] = "signed-c2pa"
                if not _manifest_path(artifact).exists():
                    errors[:] = [error for error in errors if not error.startswith("missing manifest:")]
                    report["integrity"] = {"status": "valid" if not errors else "invalid", "method": "c2pa-hard-binding-and-metadata"}
            if require_trusted and signature["trust"] != "trusted":
                errors.append("C2PA signer is not trusted by the selected trust anchors.")
        except Exception as exc:
            report["signature"] = {"signature": "error", "binding": "not_established", "trust": "unknown"}
            errors.append(f"C2PA check failed: {exc}")
    if check_signal:
        from provenance.signal import verify_signal
        try:
            signal = verify_signal(
                artifact, validate_payloads(evidence["payloads"]) if evidence else (),
                device=device, complete=bool(evidence and evidence["complete"]),
            )
            report["signal"] = signal
            if signal["detection"] != "detected":
                errors.append("Watermark signal detection did not pass or no payload timeline is available.")
            if signal["payload"] not in {"exact", "partial"} or (require_exact_payload and signal["payload"] != "exact"):
                errors.append("Watermark payload recovery does not meet the requested policy.")
            if signal["coverage"] != "complete":
                errors.append("Payload timeline coverage is incomplete or unavailable.")
        except Exception as exc:
            report["signal"] = {"detection": "error", "payload": "not_established", "coverage": "not_established", "spans": []}
            errors.append(f"Signal check failed: {exc}")
    report["checks_passed"] = not errors
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit audio integrity, optional AudioSeal waveform/payload recovery, and C2PA signatures."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--output-dir", help="AutoAudio output directory to inspect")
    source.add_argument("--file", help="One audio file to inspect, including an embedded signed payload record")
    parser.add_argument(
        "--include-segments",
        action="store_true",
        help="Also verify cached segment artifacts under <output-dir>/.segments",
    )
    parser.add_argument("--check-signal", action="store_true", help="Decode each retained payload span with AudioSeal (can load/download models)")
    parser.add_argument("--require-exact-payload", action="store_true", help="Require zero payload bit errors; implies --check-signal")
    parser.add_argument("--check-signature", action="store_true", help="Validate embedded C2PA signature and asset binding")
    parser.add_argument("--require-trusted-signature", action="store_true", help="Require a valid signature and a trusted signer")
    parser.add_argument("--trust-anchors", help="Local PEM trust-anchor bundle; implies a signature check")
    parser.add_argument("--provenance-tool", default="c2patool")
    parser.add_argument("--watermark-device", choices=("cpu", "cuda", "auto"), default="cpu")
    parser.add_argument("--json", action="store_true", help="Emit separate integrity, detection, payload, signature, and trust results as JSON")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    if output_dir is not None and not output_dir.is_dir():
        print(f"ERROR: output directory not found: {output_dir}", file=sys.stderr)
        return 2

    if output_dir is not None:
        candidates = _iter_audio_files(output_dir, include_segments=args.include_segments)
    else:
        artifact = Path(args.file).expanduser().resolve()
        if not artifact.is_file() or artifact.suffix.lower() not in AUDIO_EXTENSIONS:
            print(f"ERROR: audio file not found or unsupported: {artifact}", file=sys.stderr)
            return 2
        candidates = [artifact]
    if not candidates:
        print(f"ERROR: no publishable audio artifacts found in {output_dir}", file=sys.stderr)
        return 2

    failed = 0
    reports = []
    for artifact in candidates:
        result = inspect_artifact(
            artifact, check_signal=args.check_signal or args.require_exact_payload, check_signature=args.check_signature,
            require_trusted=args.require_trusted_signature, tool=args.provenance_tool,
            trust_anchors=args.trust_anchors, device=args.watermark_device,
            require_exact_payload=args.require_exact_payload,
        )
        reports.append(result)
        ok, errors = result["checks_passed"], result["errors"]
        if args.json:
            failed += not ok
            continue
        if ok:
            print(f"OK  {artifact}")
        else:
            failed += 1
            print(f"FAIL {artifact}")
        print(f"  integrity={result['integrity']['status']}; signal={result['signal']['detection']}; "
              f"payload={result['signal']['payload']}; signature={result['signature']['signature']}; "
              f"signer_trust={result['signature']['trust']}")
        for error in errors:
            print(f"  - {error}")

    if args.json:
        print(json.dumps({"schema": "autoaudio.verification.v1", "artifacts": reports, "failures": failed}, indent=2))
    else:
        print(f"\nChecked {len(candidates)} artifact(s); failures: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
