from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.checkpoint import sha256_file


AI_MARKING_SCHEMA = "autoaudio.ai_marking.v1"


@dataclass(frozen=True)
class WatermarkEvidence:
    path: str
    sha256: str
    scope: str
    sources: tuple["WatermarkEvidence", ...] = ()


def ai_marking_metadata_args() -> list[str]:
    return [
        "-metadata",
        "ai_generated=true",
        "-metadata",
        "ai_system=AutoAudio",
        "-metadata",
        "ai_provider=ComfyUI",
        "-metadata",
        "ai_marking=audio_watermark+metadata+manifest",
    ]


def manifest_path_for(artifact_path: str | Path) -> Path:
    artifact = Path(artifact_path)
    return artifact.with_suffix(f"{artifact.suffix}.ai.json")


def write_ai_marking_manifest(
    artifact_path: str | Path,
    *,
    content_id: str,
    metadata_embedded: bool,
    watermark_applied: bool,
    watermark_verified: bool,
    watermark_detail: str,
    watermark_scope: str = "direct",
    source_artifacts: Iterable[WatermarkEvidence] = (),
) -> Path:
    artifact = Path(artifact_path)
    payload = {
        "schema": AI_MARKING_SCHEMA,
        "artifact": artifact.name,
        "artifact_sha256": sha256_file(artifact) if artifact.exists() else "",
        "ai_generated": True,
        "ai_system": "AutoAudio",
        "provider": "ComfyUI",
        "content_id": content_id,
        "marking_methods": {
            "metadata": metadata_embedded,
            "audio_watermark": {
                "applied": watermark_applied,
                "verified": watermark_verified,
                "scope": watermark_scope,
                "detail": watermark_detail,
            },
        },
        "source_artifacts": [
            {
                "artifact": Path(item.path).name,
                "sha256": item.sha256,
                "watermark_scope": item.scope,
            }
            for item in source_artifacts
        ],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = manifest_path_for(artifact)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def validate_watermarked_artifact(artifact_path: str | Path) -> WatermarkEvidence:
    artifact = Path(artifact_path)
    if not artifact.is_file():
        raise ValueError(f"Watermarked source is missing: {artifact}")

    manifest_path = manifest_path_for(artifact)
    if not manifest_path.is_file():
        raise ValueError(f"Watermark manifest is missing: {manifest_path}")

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Watermark manifest is unreadable: {manifest_path}: {exc}") from exc

    if payload.get("schema") != AI_MARKING_SCHEMA:
        raise ValueError(f"Unsupported watermark manifest schema: {payload.get('schema')!r}")

    actual_sha256 = sha256_file(artifact)
    if payload.get("artifact_sha256") != actual_sha256:
        raise ValueError(f"Watermark manifest hash does not match {artifact}")

    watermark = (payload.get("marking_methods") or {}).get("audio_watermark") or {}
    if not watermark.get("applied") or not watermark.get("verified"):
        raise ValueError(f"Watermark manifest does not contain verified marking evidence: {manifest_path}")

    raw_sources = payload.get("source_artifacts", [])
    if not isinstance(raw_sources, list):
        raise ValueError(f"Watermark manifest source evidence must be a list: {manifest_path}")
    sources: list[WatermarkEvidence] = []
    for source in raw_sources:
        if not isinstance(source, dict):
            raise ValueError(f"Watermark manifest contains malformed source evidence: {manifest_path}")
        source_name = source.get("artifact")
        source_sha256 = source.get("sha256")
        if not isinstance(source_name, str) or not source_name or not isinstance(source_sha256, str) or not source_sha256:
            raise ValueError(f"Watermark manifest contains incomplete source evidence: {manifest_path}")
        sources.append(
            WatermarkEvidence(
                path=source_name,
                sha256=source_sha256,
                scope=str(source.get("watermark_scope") or "direct"),
            )
        )

    return WatermarkEvidence(
        path=str(artifact),
        sha256=actual_sha256,
        scope=str(watermark.get("scope") or "direct"),
        sources=tuple(sources),
    )


def remove_artifact_and_manifest(artifact_path: str | Path) -> None:
    artifact = Path(artifact_path)
    if not artifact.name:
        return
    for target in (artifact, manifest_path_for(artifact)):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_orphan_manifests(directory: str | Path) -> int:
    base = Path(directory)
    if not base.is_dir():
        return 0

    removed = 0
    for manifest_path in base.glob("*.ai.json"):
        artifact_name = manifest_path.name.removesuffix(".ai.json")
        if (base / artifact_name).exists():
            continue
        try:
            manifest_path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
