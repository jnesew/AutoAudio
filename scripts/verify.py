#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

AI_TAGS = {
    "ai_generated": "true",
    "ai_system": "AutoAudio",
    "ai_provider": "ComfyUI",
}
AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".opus", ".m4b", ".mp4", ".m4a"}


def _iter_audio_files(base_dir: Path) -> list[Path]:
    return sorted(path for path in base_dir.rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS)


def _manifest_path(artifact: Path) -> Path:
    return artifact.with_suffix(f"{artifact.suffix}.ai.json")


def _load_manifest(manifest_path: Path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _probe_tags(artifact: Path) -> dict[str, str]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(artifact)]
    output = subprocess.check_output(cmd, text=True)
    payload = json.loads(output)
    return {str(k).lower(): str(v) for k, v in (payload.get("format", {}).get("tags") or {}).items()}


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

        if manifest.get("schema") != "autoaudio.ai_marking.v1":
            errors.append(f"unexpected manifest schema for {artifact.name}: {manifest.get('schema')!r}")

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
        if actual is None or actual.lower() != expected:
            errors.append(f"metadata tag mismatch for {artifact.name}: {key}={actual!r}, expected {expected!r}")

    if not tags.get("ai_marking", ""):
        errors.append(f"metadata tag missing for {artifact.name}: ai_marking")

    return (not errors), errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify AutoAudio AI-marking metadata/watermark manifests for generated artifacts.")
    parser.add_argument("--output-dir", required=True, help="AutoAudio output directory to inspect")
    parser.add_argument("--include-segments", action="store_true", help="Also verify cached segment artifacts under <output-dir>/.segments")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    if not output_dir.exists() or not output_dir.is_dir():
        print(f"ERROR: output directory not found: {output_dir}", file=sys.stderr)
        return 2

    candidates = [path for path in _iter_audio_files(output_dir) if args.include_segments or path.parent.name != ".segments"]
    if not candidates:
        print(f"ERROR: no audio artifacts found in {output_dir}", file=sys.stderr)
        return 2

    failed = 0
    for artifact in candidates:
        ok, errors = verify_artifact(artifact)
        if ok:
            print(f"OK  {artifact}")
            continue
        failed += 1
        print(f"FAIL {artifact}")
        for err in errors:
            print(f"  - {err}")

    print(f"\nChecked {len(candidates)} artifact(s); failures: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
