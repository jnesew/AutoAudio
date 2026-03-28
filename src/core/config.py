from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    """Central app configuration with runtime/resource defaults."""

    project_root: Path
    resource_dir: Path = field(init=False)
    workflows_dir: Path = field(init=False)
    state_dir: Path = field(init=False)
    default_workflow_filename: str = "vibevoice_single_speaker.json"
    default_voice_filename: str = "default_voice.wav"
    comfyui_mode: str = "network"
    comfyui_server_address: str = "127.0.0.1:8188"
    comfyui_timeout_seconds: float = 120.0
    comfyui_spoof_scenario: str = "success"

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_dir", self.project_root / "resources")
        object.__setattr__(self, "workflows_dir", self.resource_dir / "workflows")
        object.__setattr__(self, "state_dir", self.resource_dir / ".autoaudio_state")

    @property
    def workflow_path(self) -> Path:
        return self.workflows_dir / self.default_workflow_filename


@dataclass(frozen=True)
class GenerationSettings:
    max_words_per_chunk: int = 250
    diffusion_steps: int = 25
    temperature: float = 0.95
    top_p: float = 0.95
    cfg_scale: float = 1.3
    free_memory_after_generate: bool = False
