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

from core.checkpoint import CheckpointStore
from core.config import AppConfig
from core.pipeline import CHAPTER_DISCLOSURE_TEXT, ensure_disclosure_asset, process_segment
from tts import AudioArtifact, ProviderIdentity


class RecordingProvider:
    def __init__(self):
        self.calls = []
        self.identity = ProviderIdentity(
            provider_id="test",
            provider_name="Test TTS",
            model_name="test-model",
            model_version="test",
            backend_name="test-backend",
            backend_version="test",
            compatibility={},
        )

    def generate_audio(self, **kwargs):
        self.calls.append(kwargs)
        return AudioArtifact(content=b"RIFF-valid-disclosure-audio", extension=".wav")


def test_process_segment_sends_only_narration_text(tmp_path):
    provider = RecordingProvider()

    process_segment(
        text_segment="Narration begins here.",
        speech_provider=provider,
        config=AppConfig(project_root=tmp_path, comfyui_mode="spoof"),
    )

    assert provider.calls[0]["text_segment"] == "Narration begins here."
    assert provider.calls[0]["purpose"] == "narration"
    assert CHAPTER_DISCLOSURE_TEXT not in provider.calls[0]["text_segment"]


def test_disclosure_is_generated_once_with_fixed_preset_and_checkpointed(tmp_path):
    state_dir = tmp_path / ".autoaudio_state"
    store = CheckpointStore(state_dir)
    checkpoint = {"version": 2, "artifacts": {}}
    provider = RecordingProvider()
    marking = SimpleNamespace(applied=True, verified=True, detail="verified")

    def fake_encode(command, **kwargs):
        del kwargs
        Path(command[-1]).write_bytes(b"encoded-disclosure")
        return SimpleNamespace(returncode=0)

    call_args = {
        "state_dir": state_dir,
        "config": AppConfig(project_root=tmp_path, comfyui_mode="spoof"),
        "speech_provider": provider,
        "checkpoint": checkpoint,
        "checkpoint_store": store,
        "watermark_device": "cpu",
        "logger": logging.getLogger("test.disclosure"),
    }
    with patch(
        "core.pipeline.watermark_audio_bytes_best_effort", return_value=(marking, b"marked")
    ) as watermark, patch(
        "core.pipeline.subprocess.run", side_effect=fake_encode
    ):
        first = ensure_disclosure_asset(**call_args)
        second = ensure_disclosure_asset(**call_args)

    assert first == second
    assert len(provider.calls) == 1
    assert provider.calls[0]["text_segment"] == CHAPTER_DISCLOSURE_TEXT
    assert provider.calls[0]["purpose"] == "disclosure"
    assert watermark.call_args.kwargs["device"] == "cpu"
    assert checkpoint["artifacts"]["disclosure"]["policy_version"] == "chapter-disclosure-v1"
    assert Path(checkpoint["artifacts"]["disclosure"]["manifest_path"]).exists()
