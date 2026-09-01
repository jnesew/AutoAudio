from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.version import default_claim_generator


class ProvenanceError(RuntimeError):
    """Raised when C2PA provenance generation/signing fails."""


C2PA_SIGNING_ALGORITHM = "es256"
C2PA_TRAINED_ALGORITHMIC_MEDIA = (
    "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
)
_ALLOWED_LOCAL_VALIDATION_CODES = {"signingCredential.untrusted"}


@dataclass(frozen=True)
class ProvenanceConfig:
    enabled: bool = False
    cert_path: str = ""
    key_path: str = ""
    key_password: str = ""
    hard_fail: bool = False
    tool: str = "c2patool"
    claim_generator: str = field(default_factory=default_claim_generator)


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
    source_sha256: str
    final_sha256: str


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
_EXTENSION_TO_MEDIA_TYPE = {
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
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
                        "digitalSourceType": C2PA_TRAINED_ALGORITHMIC_MEDIA,
                    }
                ]
            },
        }

    def _build_pipeline_assertion(self) -> dict[str, Any]:
        artifact = Path(self.artifact_path)
        return {
            "label": "com.autoaudio.pipeline",
            "data": {
                "artifact": artifact.name,
                "container_embedding": self.embedding_path,
                "source_sha256": _sha256_hex(artifact),
                "source_hash_scope": "pre-c2pa-embedding",
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
        "com.autoaudio.pipeline": ["data.artifact", "data.source_sha256", "data.source_hash_scope"],
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
    created_actions = [
        action
        for action in actions
        if isinstance(action, dict) and action.get("action") == "c2pa.created"
    ]
    if not created_actions:
        errors.append("assertion 'c2pa.actions' must contain action 'c2pa.created'")
    elif any(_missing(action.get("digitalSourceType")) for action in created_actions):
        errors.append("assertion 'c2pa.actions' created action is missing 'digitalSourceType'")

    pipeline_assertion = by_label.get("com.autoaudio.pipeline", {})
    source_sha256 = _read_required(pipeline_assertion, "data.source_sha256")
    if isinstance(source_sha256, str) and not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        errors.append("assertion 'com.autoaudio.pipeline' contains an invalid source SHA-256")
    source_scope = _read_required(pipeline_assertion, "data.source_hash_scope")
    if source_scope and source_scope != "pre-c2pa-embedding":
        errors.append("assertion 'com.autoaudio.pipeline' contains an invalid source hash scope")

    if errors:
        raise ProvenanceError("Assertion schema validation failed: " + "; ".join(errors))


def embedding_path_for_artifact(path: str | Path) -> str:
    extension = Path(path).suffix.lower()
    if extension not in _EXTENSION_TO_EMBEDDING_PATH:
        raise ProvenanceError(f"Unsupported provenance embedding format: {extension or '<none>'}")
    return _EXTENSION_TO_EMBEDDING_PATH[extension]


def media_type_for_artifact(path: str | Path) -> str:
    extension = Path(path).suffix.lower()
    if extension not in _EXTENSION_TO_MEDIA_TYPE:
        raise ProvenanceError(f"Unsupported provenance media type: {extension or '<none>'}")
    return _EXTENSION_TO_MEDIA_TYPE[extension]


def parse_model_identity_version(value: str) -> tuple[str, str]:
    if not value:
        return "", ""

    match = re.match(r"^(?P<name>.+?)[-_/](?P<version>v?\d[A-Za-z0-9_.-]*)$", value.strip())
    if match:
        return match.group("name"), match.group("version")
    return value.strip(), "unknown"


def _sha256_hex(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _build_manifest(
    *,
    artifact_path: str | Path,
    claim_generator: str,
    embedding_path: str,
    manifest_id: str,
    runtime_metadata: ProvenanceRuntimeMetadata,
    cert_path: str,
    key_path: str,
) -> dict:
    artifact = Path(artifact_path)
    assertions = C2PAAssertionBuilder(
        artifact_path=artifact_path,
        runtime_metadata=runtime_metadata,
        embedding_path=embedding_path,
    ).build()
    return {
        "alg": C2PA_SIGNING_ALGORITHM,
        "sign_cert": cert_path,
        "private_key": key_path,
        "vendor": "AutoAudio",
        "claim_generator": claim_generator,
        "title": artifact.name,
        "format": media_type_for_artifact(artifact),
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


def _active_manifest_from_report(report: dict[str, Any]) -> str:
    active_manifest = report.get("active_manifest")
    if not isinstance(active_manifest, str) or not active_manifest.strip():
        raise ProvenanceError("C2PA verification report is missing an active manifest identifier.")

    validation_status = report.get("validation_status") or []
    if not isinstance(validation_status, list):
        raise ProvenanceError("C2PA verification report contains malformed validation status data.")
    rejected_statuses = [
        status
        for status in validation_status
        if not isinstance(status, dict) or status.get("code") not in _ALLOWED_LOCAL_VALIDATION_CODES
    ]
    if rejected_statuses:
        details = "; ".join(
            str(status.get("explanation") or status.get("code") or status)
            if isinstance(status, dict)
            else str(status)
            for status in rejected_statuses
        )
        raise ProvenanceError(f"C2PA verification rejected the signed artifact: {details}")
    return active_manifest


def _inspect_c2pa_output(*, artifact_path: str, config: ProvenanceConfig) -> str:
    try:
        result = subprocess.run(
            [config.tool, artifact_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        report = json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise ProvenanceError(f"C2PA verification command failed: {details or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProvenanceError("C2PA verification returned invalid JSON.") from exc
    return _active_manifest_from_report(report)


def _c2patool_paths(artifact: Path, temp_dir: Path) -> tuple[Path, Path]:
    """Return c2patool-compatible input/output paths for an artifact.

    c2patool 0.27.x can read a signed M4B, but it does not recognize an
    unsigned ``.m4b`` input as ISO BMFF when embedding a new manifest.  M4B
    uses the same container as M4A, so expose the input through an M4A hard
    link and keep the tool output under that extension.  Renaming the signed
    bytes back to M4B does not alter the C2PA hard binding.
    """
    if artifact.suffix.lower() != ".m4b":
        return artifact, temp_dir / artifact.name

    input_path = temp_dir / f"{artifact.stem}.unsigned.m4a"
    output_path = temp_dir / f"{artifact.stem}.signed.m4a"
    try:
        os.link(artifact, input_path)
    except OSError:
        shutil.copyfile(artifact, input_path)
    return input_path, output_path


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
    cert_path = str(Path(config.cert_path).expanduser().resolve())
    key_path = str(Path(config.key_path).expanduser().resolve())
    if not os.path.isfile(cert_path):
        raise ProvenanceError(f"C2PA certificate not found: {cert_path}")
    if not os.path.isfile(key_path):
        raise ProvenanceError(f"C2PA private key not found: {key_path}")

    artifact = Path(artifact_path).expanduser().resolve()
    artifact_path = str(artifact)
    source_sha256 = _sha256_hex(artifact)
    embedding_path = embedding_path_for_artifact(artifact)
    instance_id = f"urn:uuid:{uuid.uuid4()}"
    manifest = _build_manifest(
        artifact_path=artifact_path,
        claim_generator=config.claim_generator,
        embedding_path=embedding_path,
        manifest_id=instance_id,
        runtime_metadata=runtime_metadata,
        cert_path=cert_path,
        key_path=key_path,
    )

    # Keep the temporary output on the artifact filesystem so the final
    # replacement is atomic and cannot fail with EXDEV on systems where /tmp
    # and the audiobook output directory are separate mounts.
    with tempfile.TemporaryDirectory(prefix=".autoaudio-c2pa-", dir=artifact.parent) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        manifest_path = temp_dir / "manifest.json"
        tool_input_path, signed_output_path = _c2patool_paths(artifact, temp_dir)
        with open(manifest_path, "w", encoding="utf-8") as file:
            json.dump(manifest, file, indent=2, sort_keys=True)

        _run_c2patool(
            input_path=str(tool_input_path),
            output_path=str(signed_output_path),
            manifest_path=str(manifest_path),
            config=config,
        )

        manifest_id = _inspect_c2pa_output(artifact_path=str(signed_output_path), config=config)

        os.replace(signed_output_path, artifact)

    return ProvenanceResult(
        manifest_id=manifest_id,
        embedding_path=embedding_path,
        source_sha256=source_sha256,
        final_sha256=_sha256_hex(artifact),
    )


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
