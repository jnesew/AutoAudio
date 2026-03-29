from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from comfyui.client import AudioArtifact, ComfyUIConnectionError, ComfyUIProtocolError, ComfyUITimeoutError
from comfyui.workflow_loader import build_runtime_workflow
from core.config import GenerationSettings


@dataclass
class SpoofComfyUIEndpoint:
    """Deterministic in-memory spoof for ComfyUI's /prompt, /history, /view, and ws events."""

    scenario: str = "success"
    audio_payload: bytes = b"RIFF....FAKEAUDIO"
    _prompt_counter: itertools.count = field(default_factory=lambda: itertools.count(1), init=False)
    _audio_store: dict[tuple[str, str, str], bytes] = field(default_factory=dict, init=False)

    def prompt(self, workflow: dict[str, Any], client_id: str) -> dict[str, str]:
        del workflow, client_id
        if self.scenario == "connection_error":
            raise ComfyUIConnectionError("Spoofed connection refused for /prompt")

        prompt_id = f"prompt-{next(self._prompt_counter)}"
        key = (f"{prompt_id}.flac", "", "output")
        self._audio_store[key] = self.audio_payload
        return {"prompt_id": prompt_id}

    def ws_events(self, prompt_id: str) -> list[dict[str, Any]]:
        if self.scenario == "timeout":
            return [{"type": "executing", "data": {"node": 123, "prompt_id": prompt_id}}]

        return [
            {"type": "executing", "data": {"node": 123, "prompt_id": prompt_id}},
            {"type": "executing", "data": {"node": None, "prompt_id": prompt_id}},
        ]

    def history(self, prompt_id: str) -> dict[str, Any]:
        if self.scenario == "malformed_history":
            return {prompt_id: {"outputs": {"nodeA": {"not_audio": []}}}}

        filename = f"{prompt_id}.flac"
        return {
            prompt_id: {
                "outputs": {
                    "nodeA": {
                        "audio": [
                            {
                                "filename": filename,
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                }
            }
        }

    def view(self, *, filename: str, subfolder: str, folder_type: str) -> bytes:
        key = (filename, subfolder, folder_type)
        if self.scenario == "missing_view_payload" or key not in self._audio_store:
            raise ComfyUIProtocolError("Spoofed /view missing payload")
        return self._audio_store[key]


class SpoofComfyUIClient:
    def __init__(
        self,
        *,
        endpoint: SpoofComfyUIEndpoint | None = None,
        scenario: str = "success",
        client_id: str = "spoof-client",
    ) -> None:
        self.endpoint = endpoint or SpoofComfyUIEndpoint(scenario=scenario)
        self.client_id = client_id

    def generate_audio(
        self,
        *,
        workflow_template: dict[str, Any],
        text_segment: str,
        reference_voice: str,
        settings: GenerationSettings,
        timeout_seconds: float | None = None,
    ) -> AudioArtifact:
        del timeout_seconds
        workflow = build_runtime_workflow(
            workflow_template=workflow_template,
            text_segment=text_segment,
            reference_voice=reference_voice,
            settings=settings,
        )

        prompt = self.endpoint.prompt(workflow, self.client_id)
        prompt_id = prompt.get("prompt_id")
        if not prompt_id:
            raise ComfyUIProtocolError("Spoof endpoint returned no prompt_id")

        events = self.endpoint.ws_events(prompt_id)
        completed = any(e.get("type") == "executing" and e.get("data", {}).get("node") is None for e in events)
        if not completed:
            raise ComfyUITimeoutError(f"Spoof timeout waiting for prompt {prompt_id}")

        history = self.endpoint.history(prompt_id)
        outputs = history.get(prompt_id, {}).get("outputs", {})
        for node_output in outputs.values():
            audio_files = node_output.get("audio", [])
            for audio_file in audio_files:
                content = self.endpoint.view(
                    filename=audio_file["filename"],
                    subfolder=audio_file["subfolder"],
                    folder_type=audio_file["type"],
                )
                return AudioArtifact(content=content, extension=".flac")

        raise ComfyUIProtocolError("Spoof /history response missing audio payload")

    def upload_reference_voice(
        self,
        *,
        file_path: str,
        target_filename: str,
        upload_workflow_template: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> None:
        del file_path, target_filename, upload_workflow_template, timeout_seconds
        if self.endpoint.scenario == "connection_error":
            raise ComfyUIConnectionError("Spoofed connection refused for /upload")
