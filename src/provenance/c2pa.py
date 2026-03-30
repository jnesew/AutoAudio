from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


class ProvenanceError(RuntimeError):
    """Raised when C2PA provenance generation/signing fails."""


@dataclass(frozen=True)
class ProvenanceConfig:
    enabled: bool = False
    cert_path: str = ""
    key_path: str = ""
    key_password: str = ""
    hard_fail: bool = False
    tool: str = "c2patool"
    claim_generator: str = "autoaudio"


@dataclass(frozen=True)
class ProvenanceResult:
    manifest_id: str
    embedding_path: str


_EXTENSION_TO_EMBEDDING_PATH = {
    ".mp3": "id3v2",
    ".mp4": "mp4:c2pa-uuid-box",
    ".m4a": "mp4:c2pa-uuid-box",
    ".m4b": "mp4:c2pa-uuid-box",
    ".flac": "chunk",
    ".wav": "chunk",
    ".wave": "chunk",
    ".aif": "chunk",
    ".aiff": "chunk",
}


def embedding_path_for_artifact(path: str | Path) -> str:
    extension = Path(path).suffix.lower()
    if extension not in _EXTENSION_TO_EMBEDDING_PATH:
        raise ProvenanceError(f"Unsupported provenance embedding format: {extension or '<none>'}")
    return _EXTENSION_TO_EMBEDDING_PATH[extension]


def _build_manifest(*, artifact_path: str | Path, claim_generator: str, embedding_path: str, manifest_id: str) -> dict:
    artifact = Path(artifact_path)
    return {
        "vendor": "AutoAudio",
        "claim_generator": claim_generator,
        "title": artifact.name,
        "format": artifact.suffix.lower().lstrip("."),
        "instance_id": manifest_id,
        "assertions": [
            {
                "label": "c2pa.actions",
                "data": {
                    "actions": [
                        {
                            "action": "c2pa.created",
                            "softwareAgent": claim_generator,
                            "parameters": {
                                "embedding_path": embedding_path,
                            },
                        }
                    ]
                },
            },
            {
                "label": "com.autoaudio.pipeline",
                "data": {
                    "artifact": artifact.name,
                    "container_embedding": embedding_path,
                },
            },
        ],
    }


def _run_c2patool(
    *,
    input_path: str,
    output_path: str,
    manifest_path: str,
    config: ProvenanceConfig,
) -> None:
    if not shutil.which(config.tool):
        raise ProvenanceError(
            f"C2PA tool '{config.tool}' was not found in PATH. Install it or disable provenance."
        )

    command = [
        config.tool,
        input_path,
        "--manifest",
        manifest_path,
        "--sign_cert",
        config.cert_path,
        "--private_key",
        config.key_path,
        "--output",
        output_path,
    ]
    env = os.environ.copy()
    if config.key_password:
        env["C2PA_PRIVATE_KEY_PASSWORD"] = config.key_password

    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise ProvenanceError(f"C2PA tool execution failed: {details or exc}") from exc


def apply_c2pa_provenance(*, artifact_path: str | Path, config: ProvenanceConfig) -> ProvenanceResult | None:
    if not config.enabled:
        return None

    if not config.cert_path or not config.key_path:
        raise ProvenanceError("Provenance is enabled but certificate/key paths are missing.")
    if not os.path.exists(config.cert_path):
        raise ProvenanceError(f"C2PA certificate not found: {config.cert_path}")
    if not os.path.exists(config.key_path):
        raise ProvenanceError(f"C2PA private key not found: {config.key_path}")

    artifact_path = str(artifact_path)
    embedding_path = embedding_path_for_artifact(artifact_path)
    manifest_id = f"urn:uuid:{uuid.uuid4()}"
    manifest = _build_manifest(
        artifact_path=artifact_path,
        claim_generator=config.claim_generator,
        embedding_path=embedding_path,
        manifest_id=manifest_id,
    )

    with tempfile.TemporaryDirectory(prefix="autoaudio-c2pa-") as temp_dir:
        manifest_path = os.path.join(temp_dir, "manifest.json")
        signed_output_path = os.path.join(temp_dir, Path(artifact_path).name)
        with open(manifest_path, "w", encoding="utf-8") as file:
            json.dump(manifest, file, indent=2, sort_keys=True)

        _run_c2patool(
            input_path=artifact_path,
            output_path=signed_output_path,
            manifest_path=manifest_path,
            config=config,
        )

        os.replace(signed_output_path, artifact_path)

    return ProvenanceResult(manifest_id=manifest_id, embedding_path=embedding_path)


def apply_c2pa_with_policy(*, artifact_path: str | Path, config: ProvenanceConfig, logger: logging.Logger) -> ProvenanceResult | None:
    try:
        result = apply_c2pa_provenance(artifact_path=artifact_path, config=config)
        if result:
            logger.info(
                "C2PA manifest embedded artifact=%s manifest_id=%s embedding_path=%s",
                artifact_path,
                result.manifest_id,
                result.embedding_path,
            )
        return result
    except ProvenanceError:
        if config.hard_fail:
            raise
        logger.warning("C2PA provenance soft-fail artifact=%s", artifact_path, exc_info=True)
        return None
