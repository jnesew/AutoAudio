from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from core.config import GenerationSettings


class ComfyUIClientError(RuntimeError):
    """Base error for ComfyUI client operations."""


class ComfyUIConnectionError(ComfyUIClientError):
    """Raised when the ComfyUI runtime cannot be reached."""


class ComfyUIProtocolError(ComfyUIClientError):
    """Raised when ComfyUI responses are malformed or incomplete."""


class ComfyUITimeoutError(ComfyUIClientError):
    """Raised when ComfyUI processing does not finish in time."""


@dataclass(frozen=True)
class AudioArtifact:
    content: bytes
    extension: str = ".flac"


class ComfyUIClient(Protocol):
    def generate_audio(
        self,
        *,
        workflow_template: dict[str, Any],
        text_segment: str,
        reference_voice: str,
        settings: GenerationSettings,
        timeout_seconds: float | None = None,
    ) -> AudioArtifact:
        """Generate audio for a text segment and return audio bytes + extension."""

    def upload_reference_voice(
        self,
        *,
        file_path: str,
        target_filename: str,
        upload_workflow_template: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> None:
        """Upload a reference voice file and execute the upload workflow."""
