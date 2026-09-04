from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from provenance.c2pa import ProvenanceConfig


QWEN_PRESET_SPEAKERS = (
    "Aiden",
    "Dylan",
    "Eric",
    "Ono_anna",
    "Ryan",
    "Serena",
    "Sohee",
    "Uncle_fu",
    "Vivian",
)
QWEN_MODEL_CHOICES_BY_MODE = {
    "preset": ("0.6B", "1.7B"),
    "design": ("1.7B",),
}
QWEN_MODEL_CHOICES = tuple(
    dict.fromkeys(choice for choices in QWEN_MODEL_CHOICES_BY_MODE.values() for choice in choices)
)

TTS_PROVIDER_CHOICES = ("comfyui", "openai-compatible", "elevenlabs")
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class TTSConfig:
    """Provider-neutral speech endpoint configuration.

    API key *values* are deliberately never stored here. ``api_key_env`` names
    an environment variable that is read only when an explicitly requested
    provider operation is about to make an HTTP request.
    """

    provider: Literal["comfyui", "openai-compatible", "elevenlabs"] = "comfyui"
    base_url: str = ""
    api_key_env: str = "AUTOAUDIO_TTS_API_KEY"
    model: str = ""
    voice: str = ""
    response_format: str = ""
    language_code: str = ""

    def __post_init__(self) -> None:
        if self.provider not in TTS_PROVIDER_CHOICES:
            raise ValueError(f"Unsupported TTS provider: {self.provider!r}.")
        if self.api_key_env and not _ENVIRONMENT_VARIABLE_PATTERN.fullmatch(self.api_key_env):
            raise ValueError("TTS API key environment variable name is invalid.")
        if self.base_url:
            parsed = urlparse(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("TTS base URL must be an absolute http:// or https:// URL.")
            if parsed.username or parsed.password:
                raise ValueError("TTS base URL cannot contain credentials; use an API-key environment variable.")
            if parsed.params or parsed.query or parsed.fragment:
                raise ValueError("TTS base URL cannot contain parameters, a query string, or a fragment.")


@dataclass(frozen=True)
class AppConfig:
    """Central app configuration with runtime/resource defaults."""

    project_root: Path
    resource_dir: Path = field(init=False)
    workflows_dir: Path = field(init=False)
    qwen_custom_workflow_filename: str = "qwen3_tts_custom_voice.json"
    qwen_design_workflow_filename: str = "qwen3_tts_voice_design.json"
    narrator_profiles_filename: str = "default_profiles.json"
    comfyui_mode: str = "network"
    comfyui_server_address: str = "127.0.0.1:8188"
    comfyui_timeout_seconds: float = 900.0
    comfyui_spoof_scenario: str = "success"
    tts: TTSConfig = field(default_factory=TTSConfig)
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)

    def __post_init__(self) -> None:
        source_resource_dir = self.project_root / "resources"
        if source_resource_dir.is_dir():
            resource_dir = source_resource_dir
        else:
            try:
                resource_dir = Path(str(files("autoaudio_resources")))
            except ModuleNotFoundError:
                # Preserve a useful path in minimal source-only test contexts.
                resource_dir = source_resource_dir
        object.__setattr__(self, "resource_dir", resource_dir)
        object.__setattr__(self, "workflows_dir", self.resource_dir / "workflows")

    @property
    def workflow_path(self) -> Path:
        return self.workflow_path_for("preset")

    def workflow_path_for(self, voice_mode: str) -> Path:
        if voice_mode == "preset":
            return self.workflows_dir / self.qwen_custom_workflow_filename
        if voice_mode == "design":
            return self.workflows_dir / self.qwen_design_workflow_filename
        raise ValueError(f"Unsupported Qwen voice mode: {voice_mode!r}")

    @property
    def narrator_profiles_path(self) -> Path:
        return self.resource_dir / "narrators" / self.narrator_profiles_filename

    @staticmethod
    def state_dir_for(output_dir: str | Path) -> Path:
        """Keep resumable state scoped to the output job rather than the installation."""
        return Path(output_dir).resolve() / ".autoaudio_state"


@dataclass(frozen=True)
class GenerationSettings:
    voice_mode: Literal["preset", "design"] = "preset"
    speaker: str = "Eric"
    instruct: str = ""
    model_choice: str = "1.7B"
    device: str = "auto"
    precision: str = "bf16"
    language: str = "English"
    seed: int = 268583702137267
    max_new_tokens: int = 2048
    top_p: float = 0.8
    top_k: int = 20
    temperature: float = 1.0
    repetition_penalty: float = 1.05
    attention: str = "sdpa"
    unload_model_after_generate: bool = False

    def __post_init__(self) -> None:
        if self.voice_mode not in {"preset", "design"}:
            raise ValueError("voice_mode must be 'preset' or 'design'.")
        if self.voice_mode == "preset" and not self.speaker.strip():
            raise ValueError("Preset voice mode requires a speaker name.")
        if self.voice_mode == "preset" and self.speaker not in QWEN_PRESET_SPEAKERS:
            raise ValueError(f"Unsupported Qwen preset speaker: {self.speaker!r}.")
        if self.voice_mode == "design" and not self.instruct.strip():
            raise ValueError("Designed voice mode requires a non-empty voice instruction.")
        if not self.model_choice.strip() or not self.device.strip() or not self.language.strip():
            raise ValueError("Qwen model, device, and language values cannot be empty.")
        if self.model_choice not in QWEN_MODEL_CHOICES_BY_MODE[self.voice_mode]:
            choices = ", ".join(QWEN_MODEL_CHOICES_BY_MODE[self.voice_mode])
            raise ValueError(
                f"Qwen {self.voice_mode} mode does not support model {self.model_choice!r}; choose {choices}."
            )
        if self.seed < 0:
            raise ValueError("Qwen seed cannot be negative.")
        if self.max_new_tokens <= 0 or self.top_k <= 0:
            raise ValueError("Qwen max_new_tokens and top_k must be positive.")
        if not 0 < self.top_p <= 1:
            raise ValueError("Qwen top_p must be in the range (0, 1].")
        if self.temperature <= 0 or self.repetition_penalty <= 0:
            raise ValueError("Qwen temperature and repetition_penalty must be positive.")
        if self.attention not in {"sdpa", "flash_attn"}:
            raise ValueError("Qwen attention must be 'sdpa' or 'flash_attn'.")
        if not isinstance(self.unload_model_after_generate, bool):
            raise ValueError("Qwen unload_model_after_generate must be a boolean.")
