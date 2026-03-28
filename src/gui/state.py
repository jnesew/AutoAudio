from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.checkpoint import CheckpointStore


@dataclass(frozen=True)
class ResumeContext:
    checkpoint_path: str
    ui_state: dict[str, Any]


def load_resume_context(checkpoint_store: CheckpointStore) -> ResumeContext | None:
    """Return resume-able UI state if an incomplete checkpoint exists."""
    checkpoint = checkpoint_store.load()
    if not checkpoint:
        return None

    if checkpoint.get("status") not in {"running", "failed"}:
        return None

    ui_state = checkpoint.get("ui_state")
    if not isinstance(ui_state, dict):
        return None

    return ResumeContext(checkpoint_path=str(checkpoint_store.path), ui_state=ui_state)


def bool_from_ui_state(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default
