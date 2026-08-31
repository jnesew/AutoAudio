from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHECKPOINT_SCHEMA_VERSION = 2


class CheckpointError(ValueError):
    """Raised when checkpoint state is unreadable or structurally unsafe."""


class UnsupportedCheckpointVersion(CheckpointError):
    def __init__(self, version: Any):
        super().__init__(
            f"Unsupported checkpoint schema version {version!r}; "
            f"expected {CHECKPOINT_SCHEMA_VERSION}. Start a new run instead of resuming it."
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def stable_settings_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(serialized)


@dataclass
class CheckpointStore:
    state_dir: Path
    checkpoint_name: str = "checkpoint_state.json"

    @property
    def path(self) -> Path:
        return self.state_dir / self.checkpoint_name

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"Could not read checkpoint at {self.path}: {exc}") from exc

        if not isinstance(data, dict):
            raise CheckpointError("Checkpoint root must be a JSON object.")
        version = data.get("version")
        if version != CHECKPOINT_SCHEMA_VERSION:
            raise UnsupportedCheckpointVersion(version)
        return data

    def save(self, data: dict[str, Any]) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        data["updated_at"] = _utc_now()
        fd, temp_path = tempfile.mkstemp(prefix=".checkpoint.", suffix=".tmp", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2, sort_keys=True)
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


def create_initial_checkpoint(
    *,
    input_path: str,
    input_hash: str,
    settings_hash: str,
    workflow_hash: str,
    plan_path: str,
    plan_hash: str,
    output_dir: str,
    output_format: str,
    ui_state: dict[str, Any],
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "version": CHECKPOINT_SCHEMA_VERSION,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "input": {
            "path": input_path,
            "sha256": input_hash,
        },
        "compatibility": {
            "settings_sha256": settings_hash,
            "workflow_sha256": workflow_hash,
        },
        "plan": {
            "path": plan_path,
            "sha256": plan_hash,
            "schema_version": 1,
        },
        "output": {
            "dir": output_dir,
            "format": output_format,
        },
        "progress": {
            "completed_chapters": [],
            "completed_segments": {},
        },
        "artifacts": {
            "segments": {},
            "chapters": {},
            "parts": {},
            "provenance": {},
        },
        "errors": [],
        "ui_state": ui_state,
    }


def validate_artifact(path: str, expected_sha256: str | None) -> bool:
    if not os.path.exists(path):
        return False
    if expected_sha256:
        try:
            return sha256_file(path) == expected_sha256
        except OSError:
            return False
    return True
