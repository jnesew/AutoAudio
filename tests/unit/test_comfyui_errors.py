from __future__ import annotations

import io
import json
from unittest.mock import Mock, patch

import pytest

from comfyui.client import ComfyUIProtocolError
from comfyui.real_client import RealComfyUIClient
from core.errors import InputValidationError, format_user_error
from core.config import GenerationSettings
from comfyui.workflow_loader import load_workflow_template
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / "resources/workflows/qwen3_tts_custom_voice.json"


def _node_error(prompt_id="test-prompt"):
    return {
        "prompt_id": prompt_id,
        "node_type": "FB_Qwen3TTSCustomVoice",
        "exception_message": "qwen_tts failed to import: No module named 'librosa'",
    }


@pytest.mark.parametrize("event", ["execution_error", "execution_interrupted"])
def test_history_preserves_the_actual_node_failure(event):
    payload = {
        "test-prompt": {
            "outputs": {},
            "status": {"status_str": "error", "messages": [[event, _node_error()]]},
        }
    }
    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
        with pytest.raises(ComfyUIProtocolError, match="qwen_tts.*librosa"):
            RealComfyUIClient("127.0.0.1:8188")._get_history("test-prompt", timeout_seconds=5)


def test_history_error_without_details_does_not_accept_partial_audio():
    payload = {"test-prompt": {"outputs": {"31": {"audio": []}}, "status": {"status_str": "error"}}}
    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
        with pytest.raises(ComfyUIProtocolError, match="check the ComfyUI server log"):
            RealComfyUIClient("127.0.0.1:8188")._get_history("test-prompt", timeout_seconds=5)


def test_websocket_fails_promptly_for_own_node_error_and_ignores_other_prompts():
    class Socket:
        closed = False

        def __init__(self):
            self.events = iter([
                {"type": "execution_error", "data": _node_error("other-prompt")},
                {"type": "execution_error", "data": _node_error()},
            ])

        def connect(self, *_args, **_kwargs):
            pass

        def settimeout(self, _timeout):
            pass

        def recv(self):
            return json.dumps(next(self.events))

        def close(self):
            self.closed = True

    socket = Socket()
    with patch("comfyui.real_client.websocket.WebSocket", return_value=socket, create=True):
        with pytest.raises(ComfyUIProtocolError, match="qwen_tts.*librosa"):
            RealComfyUIClient("127.0.0.1:8188")._wait_for_completion(
                "test-prompt", timeout_seconds=5, cancellation=None,
            )
    assert socket.closed


def test_user_error_formats_remediation_on_a_real_newline():
    formatted = format_user_error(InputValidationError("Invalid setting"))
    assert "\nRemediation:" in formatted
    assert "\\n" not in formatted


def test_cached_completion_is_subscribed_before_prompt_submission():
    events = []
    socket = Mock()
    socket.connect.side_effect = lambda *_a, **_k: events.append("connected")
    socket.recv.return_value = json.dumps({"type": "execution_success", "data": {"prompt_id": "cached"}})
    client = RealComfyUIClient("127.0.0.1:8188")

    def queue(*_args, **_kwargs):
        assert events == ["connected"]
        events.append("submitted")
        return "cached"

    outputs = {"31": {"audio": [{"filename": "sample.wav", "subfolder": "", "type": "temp"}]}}
    with patch("comfyui.real_client.websocket.WebSocket", return_value=socket, create=True), patch.object(
        client, "_queue_prompt", side_effect=queue,
    ), patch.object(client, "_get_history", return_value=outputs), patch.object(
        client, "_fetch_audio", return_value=b"saved audio",
    ):
        result = client.generate_audio(
            workflow_template=load_workflow_template(WORKFLOW), text_segment="Sample", settings=GenerationSettings(),
        )
    assert result.content == b"saved audio"
    assert events == ["connected", "submitted"]
    socket.close.assert_called_once()


@pytest.mark.parametrize("completed", (True, False))
def test_disconnected_websocket_checks_only_its_own_prompt_history(completed):
    client = RealComfyUIClient("127.0.0.1:8188")
    socket = Mock()
    socket.recv.return_value = ""
    with patch.object(client, "_history_entry", return_value={
        "status": {"completed": completed}, "outputs": {},
    }) as history:
        if completed:
            client._wait_for_completion("ours", timeout_seconds=2, cancellation=None, socket=socket)
        else:
            from comfyui.client import ComfyUIConnectionError
            with pytest.raises(ComfyUIConnectionError, match="closed"):
                client._wait_for_completion("ours", timeout_seconds=2, cancellation=None, socket=socket)
    history.assert_called_once_with("ours", 1.0)


def test_cancellation_deletes_only_the_owned_queue_entry():
    calls = []

    def request(req, **_kwargs):
        calls.append((req.full_url, json.loads(req.data)))
        return io.BytesIO()

    with patch("urllib.request.urlopen", side_effect=request):
        RealComfyUIClient("127.0.0.1:8188")._cancel_prompt("our-prompt")
    assert calls == [("http://127.0.0.1:8188/queue", {"delete": ["our-prompt"]})]


def test_failed_websocket_connection_never_submits_a_prompt():
    client = RealComfyUIClient("127.0.0.1:8188")
    socket = Mock()
    socket.connect.side_effect = OSError("offline")
    with patch("comfyui.real_client.websocket.WebSocket", return_value=socket, create=True), patch.object(
        client, "_queue_prompt",
    ) as queue:
        from comfyui.client import ComfyUIConnectionError
        with pytest.raises(ComfyUIConnectionError, match="offline"):
            client.generate_audio(
                workflow_template=load_workflow_template(WORKFLOW), text_segment="Sample", settings=GenerationSettings(),
            )
    queue.assert_not_called()
    socket.close.assert_called_once()


def test_generation_shares_one_deadline_across_phases():
    from comfyui.client import ComfyUITimeoutError
    client = RealComfyUIClient("127.0.0.1:8188")
    clock = [0.0]
    budgets = []
    socket = Mock()

    def phase(next_time, result):
        def run(*_args, **kwargs):
            budgets.append(kwargs["timeout_seconds"])
            clock[0] = next_time
            return result
        return run

    outputs = {"31": {"audio": [{"filename": "sample.wav", "subfolder": "", "type": "temp"}]}}
    with patch("comfyui.real_client.time.monotonic", side_effect=lambda: clock[0]), patch.object(
        client, "_connect_websocket", return_value=socket,
    ), patch.object(client, "_queue_prompt", side_effect=phase(2, "ours")), patch.object(
        client, "_wait_for_completion", side_effect=phase(5, None),
    ), patch.object(client, "_get_history", side_effect=phase(8, outputs)), patch.object(
        client, "_fetch_audio", side_effect=phase(11, b"late audio"),
    ), patch.object(client, "_cancel_prompt") as cancel:
        with pytest.raises(ComfyUITimeoutError, match="total deadline"):
            client.generate_audio(
                workflow_template=load_workflow_template(WORKFLOW), text_segment="Sample",
                settings=GenerationSettings(), timeout_seconds=10,
            )
    assert budgets == [10, 8, 5, 2]
    cancel.assert_called_once_with("ours")
    socket.close.assert_called_once()


@pytest.mark.parametrize("status", (None, [], "invalid"))
def test_malformed_history_status_is_a_protocol_error(status):
    payload = {"ours": {"outputs": {}, "status": status}}
    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
        with pytest.raises(ComfyUIProtocolError, match="malformed status"):
            RealComfyUIClient("127.0.0.1:8188")._get_history("ours", timeout_seconds=2)
