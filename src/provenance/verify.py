from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    source_root = Path(__file__).resolve().parents[1]
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from provenance.ai_marking import AI_MARKING_SCHEMA


AI_TAGS = {
    "ai_generated": "true",
    "ai_system": "AutoAudio",
    "ai_provider": "ComfyUI",
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
    return json.loads(manifest_path.read_text(encoding="utf-8"))


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
    output = subprocess.check_output(command, text=True)
    payload = json.loads(output)
    return {str(key).lower(): str(value) for key, value in (payload.get("format", {}).get("tags") or {}).items()}


def verify_artifact(artifact: Path) -> tuple[bool, list[str]]:
    errors: list[str] = []

    manifest_path = _manifest_path(artifact)
    if not manifest_path.exists():
        errors.append(f"missing manifest: {manifest_path}")
    else:
        try:
            manifest = _load_manifest(manifest_path)
        except Exception as exc:
            errors.append(f"invalid manifest JSON ({manifest_path}): {exc}")
            manifest = {}

        if manifest.get("schema") != AI_MARKING_SCHEMA:
            errors.append(f"unexpected manifest schema for {artifact.name}: {manifest.get('schema')!r}")
        if manifest.get("artifact") != artifact.name:
            errors.append(f"manifest artifact name mismatch for {artifact.name}")
        if manifest.get("artifact_hash_scope") != "entire-artifact-bytes":
            errors.append(f"manifest hash scope mismatch for {artifact.name}")
        expected_sha256 = manifest.get("artifact_sha256")
        actual_sha256 = _sha256_file(artifact)
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

    for key, expected in AI_TAGS.items():
        actual = tags.get(key)
        if actual is None or actual.lower() != expected.lower():
            errors.append(f"metadata tag mismatch for {artifact.name}: {key}={actual!r}, expected {expected!r}")

    if not tags.get("ai_marking", ""):
        errors.append(f"metadata tag missing for {artifact.name}: ai_marking")

    return (not errors), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify AutoAudio AI-marking metadata, final hashes, and watermark manifests."
    )
    parser.add_argument("--output-dir", required=True, help="AutoAudio output directory to inspect")
    parser.add_argument(
        "--include-segments",
        action="store_true",
        help="Also verify cached segment artifacts under <output-dir>/.segments",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).expanduser().resolve()
    if not output_dir.exists() or not output_dir.is_dir():
        print(f"ERROR: output directory not found: {output_dir}", file=sys.stderr)
        return 2

    candidates = _iter_audio_files(output_dir, include_segments=args.include_segments)
    if not candidates:
        print(f"ERROR: no publishable audio artifacts found in {output_dir}", file=sys.stderr)
        return 2

    failed = 0
    for artifact in candidates:
        ok, errors = verify_artifact(artifact)
        if ok:
            print(f"OK  {artifact}")
            continue
        failed += 1
        print(f"FAIL {artifact}")
        for error in errors:
            print(f"  - {error}")

    print(f"\nChecked {len(candidates)} artifact(s); failures: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
