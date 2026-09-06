from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from core.cancellation import CancellationToken


TTS_PROVIDER_ADAPTER_VERSION = "autoaudio-tts-provider-v1"
SynthesisPurpose = Literal["narration", "disclosure"]


class TTSClientError(RuntimeError):
    """Base failure raised by a speech provider adapter."""


class TTSConfigurationError(TTSClientError):
    """Raised when an explicitly selected provider is not fully configured."""


class TTSConnectionError(TTSClientError):
    """Raised when a selected speech endpoint cannot complete a request."""


class TTSProtocolError(TTSClientError):
    """Raised when a speech endpoint returns an invalid or unexpected response."""


class VoiceDiscoveryUnsupported(TTSClientError):
    """Raised when a provider has no portable voice-discovery operation."""


@dataclass(frozen=True)
class AudioArtifact:
    content: bytes
    extension: str = ".wav"


@dataclass(frozen=True)
class VoiceInfo:
    id: str
    name: str
    category: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.name} [{self.category}]" if self.category else self.name


@dataclass(frozen=True)
class ProviderIdentity:
    provider_id: str
    provider_name: str
    model_name: str
    model_version: str
    backend_name: str
    backend_version: str
    compatibility: dict[str, Any]


class SpeechProvider(Protocol):
    identity: ProviderIdentity
    supports_voice_discovery: bool

    def generate_audio(
        self,
        *,
        text_segment: str,
        purpose: SynthesisPurpose = "narration",
        timeout_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
        instructions: str = "",
    ) -> AudioArtifact:
        """Generate one complete audio segment."""

    def discover_voices(self, *, timeout_seconds: float | None = None) -> tuple[VoiceInfo, ...]:
        """Perform an explicit, caller-initiated voice discovery operation."""
