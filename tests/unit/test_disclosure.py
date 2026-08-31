from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from comfyui.client import AudioArtifact
from core.checkpoint import CheckpointStore
from core.config import AppConfig, GenerationSettings
from core.pipeline import CHAPTER_DISCLOSURE_TEXT, ensure_disclosure_asset, process_segment


class RecordingClient:
    def __init__(self):
        self.calls = []

    def generate_audio(self, **kwargs):
        self.calls.append(kwargs)
        return AudioArtifact(content=b"RIFF-valid-disclosure-audio", extension=".wav")


def test_process_segment_sends_only_narration_text(tmp_path):
    client = RecordingClient()

    process_segment(
        text_segment="Narration begins here.",
        workflow_template={},
        settings=GenerationSettings(),
        config=AppConfig(project_root=tmp_path, comfyui_mode="spoof"),
        comfyui_client=client,
    )

    assert client.calls[0]["text_segment"] == "Narration begins here."
    assert CHAPTER_DISCLOSURE_TEXT not in client.calls[0]["text_segment"]


def test_disclosure_is_generated_once_with_fixed_preset_and_checkpointed(tmp_path):
    state_dir = tmp_path / ".autoaudio_state"
    store = CheckpointStore(state_dir)
    checkpoint = {"version": 2, "artifacts": {}}
    client = RecordingClient()
    marking = SimpleNamespace(applied=True, verified=True, detail="verified")

    def fake_encode(command, **kwargs):
        del kwargs
        Path(command[-1]).write_bytes(b"encoded-disclosure")
        return SimpleNamespace(returncode=0)

    call_args = {
        "state_dir": state_dir,
        "workflow_template": {},
        "config": AppConfig(project_root=tmp_path, comfyui_mode="spoof"),
        "comfyui_client": client,
        "checkpoint": checkpoint,
        "checkpoint_store": store,
        "logger": logging.getLogger("test.disclosure"),
    }
    with patch("core.pipeline.watermark_audio_bytes_best_effort", return_value=(marking, b"marked")), patch(
        "core.pipeline.subprocess.run", side_effect=fake_encode
    ):
        first = ensure_disclosure_asset(**call_args)
        second = ensure_disclosure_asset(**call_args)

    assert first == second
    assert len(client.calls) == 1
    assert client.calls[0]["text_segment"] == CHAPTER_DISCLOSURE_TEXT
    assert client.calls[0]["settings"].voice_mode == "preset"
    assert checkpoint["artifacts"]["disclosure"]["policy_version"] == "chapter-disclosure-v1"
    assert Path(checkpoint["artifacts"]["disclosure"]["manifest_path"]).exists()
