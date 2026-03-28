from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from comfyui.client import ComfyUIConnectionError, ComfyUIProtocolError, ComfyUITimeoutError
from comfyui.spoof_client import SpoofComfyUIClient, SpoofComfyUIEndpoint
from comfyui.workflow_loader import load_workflow_template
from core.config import GenerationSettings


class SpoofComfyUIIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        workflow_path = PROJECT_ROOT / "resources" / "workflows" / "vibevoice_single_speaker.json"
        self.workflow_template = load_workflow_template(workflow_path)
        self.settings = GenerationSettings()

    def test_success_path_returns_audio(self) -> None:
        endpoint = SpoofComfyUIEndpoint(scenario="success", audio_payload=b"FAKE-FLAC-BYTES")
        client = SpoofComfyUIClient(endpoint=endpoint)

        artifact = client.generate_audio(
            workflow_template=self.workflow_template,
            text_segment="Hello sprint two",
            reference_voice="default_voice.wav",
            settings=self.settings,
            timeout_seconds=3,
        )

        self.assertEqual(artifact.extension, ".flac")
        self.assertEqual(artifact.content, b"FAKE-FLAC-BYTES")

    def test_timeout_path_raises_timeout_error(self) -> None:
        client = SpoofComfyUIClient(scenario="timeout")

        with self.assertRaises(ComfyUITimeoutError):
            client.generate_audio(
                workflow_template=self.workflow_template,
                text_segment="should timeout",
                reference_voice="default_voice.wav",
                settings=self.settings,
                timeout_seconds=1,
            )

    def test_malformed_history_raises_protocol_error(self) -> None:
        client = SpoofComfyUIClient(scenario="malformed_history")

        with self.assertRaises(ComfyUIProtocolError):
            client.generate_audio(
                workflow_template=self.workflow_template,
                text_segment="bad history",
                reference_voice="default_voice.wav",
                settings=self.settings,
                timeout_seconds=1,
            )

    def test_connection_error_raises_connection_error(self) -> None:
        client = SpoofComfyUIClient(scenario="connection_error")

        with self.assertRaises(ComfyUIConnectionError):
            client.generate_audio(
                workflow_template=self.workflow_template,
                text_segment="cannot connect",
                reference_voice="default_voice.wav",
                settings=self.settings,
                timeout_seconds=1,
            )


if __name__ == "__main__":
    unittest.main()
