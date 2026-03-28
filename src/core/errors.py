from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorGuidance:
    code: str
    remediation: str
    exit_code: int


class AutoAudioError(RuntimeError):
    """Base error for user-facing pipeline failures."""

    guidance = ErrorGuidance(code="AUTOAUDIO_ERROR", remediation="Review the run log for details.", exit_code=1)

    def __init__(self, message: str, *, remediation: str | None = None):
        super().__init__(message)
        self.remediation = remediation or self.guidance.remediation


class InputValidationError(AutoAudioError):
    guidance = ErrorGuidance(
        code="INPUT_VALIDATION_ERROR",
        remediation="Check input path, extension, and source mode arguments.",
        exit_code=2,
    )


class MetadataExtractionError(AutoAudioError):
    guidance = ErrorGuidance(
        code="METADATA_EXTRACTION_ERROR",
        remediation="Disable --fetch-metadata or provide explicit --title/--author overrides.",
        exit_code=3,
    )


class ResumeStateError(AutoAudioError):
    guidance = ErrorGuidance(
        code="RESUME_STATE_ERROR",
        remediation="Run with --resume no, or clear resources/.autoaudio_state/checkpoint_state.json.",
        exit_code=4,
    )


class AudioStitchError(AutoAudioError):
    guidance = ErrorGuidance(
        code="AUDIO_STITCH_ERROR",
        remediation="Verify ffmpeg/ffprobe are installed and generated segment files are valid.",
        exit_code=5,
    )


class ComfyUIConnectionError(AutoAudioError):
    guidance = ErrorGuidance(
        code="COMFYUI_CONNECTION_ERROR",
        remediation="Start ComfyUI or switch to --comfyui-mode spoof for local testing.",
        exit_code=6,
    )


class ComfyUIProtocolError(AutoAudioError):
    guidance = ErrorGuidance(
        code="COMFYUI_PROTOCOL_ERROR",
        remediation="Check ComfyUI workflow/runtime compatibility and retry.",
        exit_code=7,
    )


class PipelineRuntimeError(AutoAudioError):
    guidance = ErrorGuidance(
        code="PIPELINE_RUNTIME_ERROR",
        remediation="Inspect the run log and retry with smaller chunks or --comfyui-mode spoof.",
        exit_code=8,
    )


def format_user_error(exc: BaseException) -> str:
    if isinstance(exc, AutoAudioError):
        return f"[{exc.guidance.code}] {exc}\\nRemediation: {exc.remediation}"
    return f"[UNEXPECTED_ERROR] {exc}\\nRemediation: Review traceback in the run log."
