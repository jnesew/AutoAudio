from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
class ProvenanceRuntimeMetadata:
    model_name: str = ""
    model_version: str = ""
    backend_name: str = ""
    backend_version: str = ""
    software_name: str = "AutoAudio"
    software_version: str = "dev"


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


class C2PAAssertionBuilder:
    def __init__(self, *, artifact_path: str | Path, runtime_metadata: ProvenanceRuntimeMetadata, embedding_path: str) -> None:
        self.artifact_path = str(artifact_path)
        self.runtime_metadata = runtime_metadata
        self.embedding_path = embedding_path

    def build(self) -> list[dict[str, Any]]:
        assertions = [
            self._build_ai_generative_assertion(),
            self._build_actions_assertion(),
            self._build_hash_data_assertion(),
            self._build_pipeline_assertion(),
        ]
        validate_assertions(assertions)
        return assertions

    def _build_ai_generative_assertion(self) -> dict[str, Any]:
        return {
            "label": "c2pa.ai.generative",
            "data": {
                "generator": {
                    "name": self.runtime_metadata.model_name,
                    "version": self.runtime_metadata.model_version,
                },
                "type": "audio/text-to-speech",
            },
        }

    def _build_actions_assertion(self) -> dict[str, Any]:
        return {
            "label": "c2pa.actions",
            "data": {
                "actions": [
                    {
                        "action": "c2pa.created",
                        "softwareAgent": {
                            "name": self.runtime_metadata.software_name,
                            "version": self.runtime_metadata.software_version,
                            "backend": {
                                "name": self.runtime_metadata.backend_name,
                                "version": self.runtime_metadata.backend_version,
                            },
                        },
                        "parameters": {
                            "embedding_path": self.embedding_path,
                        },
                    }
                ]
            },
        }

    def _build_hash_data_assertion(self) -> dict[str, Any]:
        with open(self.artifact_path, "rb") as file:
            payload = file.read()

        digest = hashlib.sha256(payload).digest()
        digest_b64 = base64.b64encode(digest).decode("ascii")
        return {
            "label": "c2pa.hash.data",
            "data": {
                "alg": "sha256",
                "hash": digest_b64,
                "pad": 0,
                "exclusions": [],
            },
        }

    def _build_pipeline_assertion(self) -> dict[str, Any]:
        artifact = Path(self.artifact_path)
        return {
            "label": "com.autoaudio.pipeline",
            "data": {
                "artifact": artifact.name,
                "container_embedding": self.embedding_path,
            },
        }


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not value
    if isinstance(value, dict):
        return not value
    return False


def _read_required(data: dict[str, Any], field_path: str) -> Any:
    current: Any = data
    for piece in field_path.split("."):
        if not isinstance(current, dict) or piece not in current:
            return None
        current = current[piece]
    return current


def validate_assertions(assertions: list[dict[str, Any]]) -> None:
    schema = {
        "c2pa.ai.generative": ["data.generator.name", "data.generator.version"],
        "c2pa.actions": ["data.actions"],
        "c2pa.hash.data": ["data.alg", "data.hash"],
    }

    by_label = {item.get("label"): item for item in assertions}
    errors: list[str] = []

    for label, required_fields in schema.items():
        assertion = by_label.get(label)
        if not assertion:
            errors.append(f"missing required assertion '{label}'")
            continue
        for required_field in required_fields:
            value = _read_required(assertion, required_field)
            if _missing(value):
                errors.append(f"assertion '{label}' is missing required field '{required_field}'")

    actions = _read_required(by_label.get("c2pa.actions", {}), "data.actions") or []
    has_created_action = any(isinstance(action, dict) and action.get("action") == "c2pa.created" for action in actions)
    if not has_created_action:
        errors.append("assertion 'c2pa.actions' must contain action 'c2pa.created'")

    if errors:
        raise ProvenanceError("Assertion schema validation failed: " + "; ".join(errors))


def embedding_path_for_artifact(path: str | Path) -> str:
    extension = Path(path).suffix.lower()
    if extension not in _EXTENSION_TO_EMBEDDING_PATH:
        raise ProvenanceError(f"Unsupported provenance embedding format: {extension or '<none>'}")
    return _EXTENSION_TO_EMBEDDING_PATH[extension]


def parse_model_identity_version(value: str) -> tuple[str, str]:
    if not value:
        return "", ""

    match = re.match(r"^(?P<name>[A-Za-z0-9_.]+?)-(?P<version>[A-Za-z0-9_.-]+)$", value)
    if match:
        return match.group("name"), match.group("version")
    return value, "unknown"


def _build_manifest(
    *,
    artifact_path: str | Path,
    claim_generator: str,
    embedding_path: str,
    manifest_id: str,
    runtime_metadata: ProvenanceRuntimeMetadata,
) -> dict:
    artifact = Path(artifact_path)
    assertions = C2PAAssertionBuilder(
        artifact_path=artifact_path,
        runtime_metadata=runtime_metadata,
        embedding_path=embedding_path,
    ).build()
    return {
        "vendor": "AutoAudio",
        "claim_generator": claim_generator,
        "title": artifact.name,
        "format": artifact.suffix.lower().lstrip("."),
        "instance_id": manifest_id,
        "assertions": assertions,
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


def apply_c2pa_provenance(
    *,
    artifact_path: str | Path,
    config: ProvenanceConfig,
    runtime_metadata: ProvenanceRuntimeMetadata,
) -> ProvenanceResult | None:
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
        runtime_metadata=runtime_metadata,
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


def apply_c2pa_with_policy(
    *,
    artifact_path: str | Path,
    config: ProvenanceConfig,
    runtime_metadata: ProvenanceRuntimeMetadata,
    logger: logging.Logger,
) -> ProvenanceResult | None:
    try:
        result = apply_c2pa_provenance(artifact_path=artifact_path, config=config, runtime_metadata=runtime_metadata)
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
