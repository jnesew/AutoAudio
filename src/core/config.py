from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from provenance.c2pa import ProvenanceConfig


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
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_dir", self.project_root / "resources")
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
        if self.voice_mode == "design" and not self.instruct.strip():
            raise ValueError("Designed voice mode requires a non-empty voice instruction.")
        if not self.model_choice.strip() or not self.device.strip() or not self.language.strip():
            raise ValueError("Qwen model, device, and language values cannot be empty.")
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
