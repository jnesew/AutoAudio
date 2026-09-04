from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.checkpoint import CheckpointError, CheckpointStore


GUI_CLI_EXCLUDED_DESTINATIONS = frozenset({"help", "gui", "resume", "version"})
GUI_CONTROLLED_DESTINATIONS = frozenset(
    {
        "input_book",
        "output_dir",
        "source_mode",
        "pages_per_chapter",
        "target_words_per_chapter",
        "min_paragraphs_per_chapter",
        "chapters_per_part",
        "target_words_per_segment",
        "max_words_per_segment",
        "narrate_toc",
        "replacement_file",
        "replacement_rules",
        "disclosure_gap_ms",
        "segment_gap_ms",
        "chapter_gap_ms",
        "narrator_profile",
        "speaker",
        "voice_instruct",
        "model_choice",
        "device",
        "precision",
        "language",
        "seed",
        "max_new_tokens",
        "top_p",
        "top_k",
        "temperature",
        "repetition_penalty",
        "attention",
        "unload_model_after_generate",
        "output_format",
        "watermark_device",
        "fetch_metadata",
        "gutenberg_id",
        "title",
        "author",
        "comfyui_mode",
        "comfyui_server_address",
        "comfyui_timeout_seconds",
        "provenance_enabled",
        "provenance_cert_path",
        "provenance_key_path",
        "provenance_key_password",
        "provenance_failure_mode",
        "provenance_tool",
        "provenance_claim_generator",
        "comfyui_spoof_scenario",
    }
)


def gui_cli_parity(parser) -> tuple[set[str], set[str]]:
    """Return CLI destinations missing from the GUI contract and stale GUI entries."""
    cli_destinations = {
        action.dest
        for action in parser._actions
        if action.dest not in GUI_CLI_EXCLUDED_DESTINATIONS
    }
    return (
        cli_destinations - GUI_CONTROLLED_DESTINATIONS,
        GUI_CONTROLLED_DESTINATIONS - cli_destinations,
    )


@dataclass(frozen=True)
class ResumeContext:
    checkpoint_path: str
    ui_state: dict[str, Any]


def load_resume_context(checkpoint_store: CheckpointStore) -> ResumeContext | None:
    """Return resume-able UI state if an incomplete checkpoint exists."""
    try:
        checkpoint = checkpoint_store.load()
    except CheckpointError:
        return None
    if not checkpoint:
        return None

    if checkpoint.get("status") not in {"running", "failed", "cancelled"}:
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
