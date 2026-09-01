from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

if "websocket" not in sys.modules:
    websocket_stub = ModuleType("websocket")
    websocket_stub.WebSocket = object
    websocket_stub.WebSocketTimeoutException = TimeoutError
    sys.modules["websocket"] = websocket_stub

from comfyui.real_client import RealComfyUIClient, _bounded_http_timeout
from comfyui.spoof_client import SpoofComfyUIClient
from comfyui.workflow_loader import load_workflow_template
from core.cancellation import CancellationToken
from core.checkpoint import CheckpointStore
from core.config import AppConfig, GenerationSettings
from core.errors import PipelineCancelled
from core.pipeline import build_app_config, build_argument_parser, run_pipeline
from gui.state import load_resume_context


def test_cancellation_token_is_thread_safe_and_user_facing():
    token = CancellationToken()

    assert token.is_cancelled is False
    token.cancel()

    with pytest.raises(PipelineCancelled) as error:
        token.raise_if_cancelled()
    assert error.value.guidance.exit_code == 130


def test_spoof_client_rejects_pre_cancelled_request():
    token = CancellationToken()
    token.cancel()
    workflow = load_workflow_template(PROJECT_ROOT / "resources" / "workflows" / "qwen3_tts_custom_voice.json")

    with pytest.raises(PipelineCancelled):
        SpoofComfyUIClient().generate_audio(
            workflow_template=workflow,
            text_segment="This request must not be queued.",
            settings=GenerationSettings(),
            cancellation=token,
        )


def test_real_client_removes_and_interrupts_cancelled_prompt():
    client = RealComfyUIClient("127.0.0.1:8188")
    workflow = load_workflow_template(PROJECT_ROOT / "resources" / "workflows" / "qwen3_tts_custom_voice.json")

    with patch.object(client, "_queue_prompt", return_value="prompt-7") as queue_prompt, patch.object(
        client,
        "_wait_for_completion",
        side_effect=PipelineCancelled("cancelled"),
    ), patch.object(client, "_cancel_prompt") as cancel_prompt:
        with pytest.raises(PipelineCancelled):
            client.generate_audio(
                workflow_template=workflow,
                text_segment="Cancel this prompt.",
                settings=GenerationSettings(),
                cancellation=CancellationToken(),
            )

    queue_prompt.assert_called_once()
    assert queue_prompt.call_args.kwargs["timeout_seconds"] == 120
    cancel_prompt.assert_called_once_with("prompt-7")


def test_real_client_websocket_wait_polls_cancellation():
    token = CancellationToken()

    class FakeWebSocket:
        closed = False

        def connect(self, *_args, **_kwargs):
            return None

        def settimeout(self, _timeout):
            return None

        def recv(self):
            token.cancel()
            raise TimeoutError

        def close(self):
            self.closed = True

    socket = FakeWebSocket()
    client = RealComfyUIClient("127.0.0.1:8188")
    with patch("comfyui.real_client.websocket.WebSocket", return_value=socket, create=True):
        with pytest.raises(PipelineCancelled):
            client._wait_for_completion("prompt-8", timeout_seconds=5, cancellation=token)

    assert socket.closed is True


def test_real_client_http_phases_have_bounded_timeouts():
    assert _bounded_http_timeout(None) == 30.0
    assert _bounded_http_timeout(900) == 30.0
    assert _bounded_http_timeout(5.5) == 5.5
    assert _bounded_http_timeout(0) == 0.1


def test_cancelled_pipeline_writes_resumable_checkpoint_without_secret(tmp_path):
    input_book = tmp_path / "book.txt"
    input_book.write_text(
        "This paragraph is deliberately long enough to become one valid audiobook source block.",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    args = build_argument_parser(PROJECT_ROOT).parse_args(
        [
            "--input-book",
            str(input_book),
            "--output-dir",
            str(output_dir),
            "--source-mode",
            "text",
            "--comfyui-mode",
            "spoof",
            "--provenance-key-password",
            "must-not-be-checkpointed",
        ]
    )
    token = CancellationToken()
    token.cancel()

    with pytest.raises(PipelineCancelled):
        run_pipeline(args, build_app_config(args, PROJECT_ROOT), cancellation=token)

    store = CheckpointStore(output_dir / ".autoaudio_state")
    checkpoint = store.load()
    assert checkpoint["status"] == "cancelled"
    assert load_resume_context(store) is not None
    assert "provenance_key_password" not in checkpoint["ui_state"]
    assert checkpoint["ui_state"]["model_choice"] == "1.7B"
    assert checkpoint["ui_state"]["output_format"] == "flac"


def test_shared_app_config_preserves_cli_provenance_controls(tmp_path):
    args = build_argument_parser(PROJECT_ROOT).parse_args(
        [
            "--comfyui-server-address",
            "localhost:9999",
            "--comfyui-timeout-seconds",
            "42.5",
            "--provenance-enabled",
            "--provenance-cert-path",
            str(tmp_path / "cert.pem"),
            "--provenance-key-path",
            str(tmp_path / "key.pem"),
            "--provenance-key-password",
            "secret",
            "--provenance-failure-mode",
            "hard-fail",
            "--provenance-tool",
            "custom-c2pa",
            "--provenance-claim-generator",
            "AutoAudio Test",
        ]
    )

    config = build_app_config(args, PROJECT_ROOT)

    assert config.comfyui_server_address == "localhost:9999"
    assert config.comfyui_timeout_seconds == 42.5
    assert config.provenance.enabled is True
    assert config.provenance.cert_path.endswith("cert.pem")
    assert config.provenance.key_path.endswith("key.pem")
    assert config.provenance.key_password == "secret"
    assert config.provenance.hard_fail is True
    assert config.provenance.tool == "custom-c2pa"
    assert config.provenance.claim_generator == "AutoAudio Test"
