from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

try:
    import websocket
except ImportError:  # Keep --help, --version, and spoof mode available in minimal environments.
    websocket = None

from comfyui.client import (
    AudioArtifact,
    ComfyUIConnectionError,
    ComfyUIProtocolError,
    ComfyUITimeoutError,
)
from comfyui.workflow_loader import build_runtime_workflow
from core.cancellation import CancellationToken
from core.config import GenerationSettings
from core.errors import PipelineCancelled


_WEBSOCKET_TIMEOUT = getattr(websocket, "WebSocketTimeoutException", TimeoutError)


def _bounded_http_timeout(timeout_seconds: float | None) -> float:
    if timeout_seconds is None:
        return 30.0
    return max(0.1, min(float(timeout_seconds), 30.0))


class RealComfyUIClient:
    def __init__(self, server_address: str, *, client_id: str | None = None) -> None:
        self.server_address = server_address
        self.client_id = client_id or str(uuid.uuid4())

    def _queue_prompt(self, prompt_workflow: dict[str, Any], *, timeout_seconds: float | None) -> str:
        payload = {"prompt": prompt_workflow, "client_id": self.client_id}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"http://{self.server_address}/prompt", data=data)

        try:
            with urllib.request.urlopen(req, timeout=_bounded_http_timeout(timeout_seconds)) as response:
                result = json.loads(response.read())
            prompt_id = result.get("prompt_id")
            if not prompt_id:
                raise ComfyUIProtocolError("Missing prompt_id in /prompt response.")
            return prompt_id
        except ComfyUIProtocolError:
            raise
        except Exception as exc:
            raise ComfyUIConnectionError(f"Failed to submit prompt to ComfyUI: {exc}") from exc

    def _wait_for_completion(
        self,
        prompt_id: str,
        *,
        timeout_seconds: float | None,
        cancellation: CancellationToken | None,
    ) -> None:
        if websocket is None:
            raise ComfyUIConnectionError(
                "websocket-client is required for network ComfyUI mode. Install project dependencies or use spoof mode."
            )
        ws = websocket.WebSocket()
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        try:
            connect_timeout = min(timeout_seconds, 5.0) if timeout_seconds is not None else 5.0
            ws.connect(f"ws://{self.server_address}/ws?clientId={self.client_id}", timeout=connect_timeout)
            while True:
                if cancellation:
                    cancellation.raise_if_cancelled()
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise ComfyUITimeoutError(f"Prompt {prompt_id} timed out waiting for websocket completion.")
                ws.settimeout(min(0.5, remaining) if remaining is not None else 0.5)
                try:
                    out = ws.recv()
                except (TimeoutError, _WEBSOCKET_TIMEOUT):
                    continue

                if not isinstance(out, str):
                    continue

                message = json.loads(out)
                if message.get("type") != "executing":
                    continue

                data = message.get("data", {})
                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    return
        except (ComfyUITimeoutError, PipelineCancelled):
            raise
        except Exception as exc:
            raise ComfyUIConnectionError(f"ComfyUI websocket connection failed: {exc}") from exc
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _cancel_prompt(self, prompt_id: str) -> None:
        requests = (
            ("queue", {"delete": [prompt_id]}),
            ("interrupt", {}),
        )
        for endpoint, payload in requests:
            request = urllib.request.Request(
                f"http://{self.server_address}/{endpoint}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=2):
                    pass
            except Exception:
                pass

    def _get_history(self, prompt_id: str, *, timeout_seconds: float | None) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(
                f"http://{self.server_address}/history/{prompt_id}",
                timeout=_bounded_http_timeout(timeout_seconds),
            ) as response:
                payload = json.loads(response.read())
        except Exception as exc:
            raise ComfyUIConnectionError(f"Failed to fetch ComfyUI history: {exc}") from exc

        history = payload.get(prompt_id)
        if not history:
            raise ComfyUIProtocolError(f"ComfyUI history missing prompt id {prompt_id}.")

        outputs = history.get("outputs")
        if not isinstance(outputs, dict):
            raise ComfyUIProtocolError("ComfyUI history response missing outputs map.")

        return outputs

    def _fetch_audio(
        self,
        *,
        filename: str,
        subfolder: str,
        folder_type: str,
        timeout_seconds: float | None,
    ) -> bytes:
        data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        url_values = urllib.parse.urlencode(data)
        try:
            with urllib.request.urlopen(
                f"http://{self.server_address}/view?{url_values}",
                timeout=_bounded_http_timeout(timeout_seconds),
            ) as response:
                return response.read()
        except Exception as exc:
            raise ComfyUIConnectionError(f"Failed to download audio from ComfyUI: {exc}") from exc

    def generate_audio(
        self,
        *,
        workflow_template: dict[str, Any],
        text_segment: str,
        settings: GenerationSettings,
        timeout_seconds: float | None = 120,
        cancellation: CancellationToken | None = None,
    ) -> AudioArtifact:
        if cancellation:
            cancellation.raise_if_cancelled()
        workflow = build_runtime_workflow(
            workflow_template=workflow_template,
            text_segment=text_segment,
            settings=settings,
        )

        prompt_id = self._queue_prompt(workflow, timeout_seconds=timeout_seconds)
        try:
            self._wait_for_completion(
                prompt_id,
                timeout_seconds=timeout_seconds,
                cancellation=cancellation,
            )
        except PipelineCancelled:
            self._cancel_prompt(prompt_id)
            raise
        if cancellation:
            cancellation.raise_if_cancelled()
        outputs = self._get_history(prompt_id, timeout_seconds=timeout_seconds)

        for node_output in outputs.values():
            audio_files = node_output.get("audio", [])
            for audio_file in audio_files:
                content = self._fetch_audio(
                    filename=audio_file["filename"],
                    subfolder=audio_file["subfolder"],
                    folder_type=audio_file["type"],
                    timeout_seconds=timeout_seconds,
                )
                if cancellation:
                    cancellation.raise_if_cancelled()
                _, ext = os.path.splitext(audio_file["filename"])
                return AudioArtifact(content=content, extension=ext.lower() or ".flac")

        raise ComfyUIProtocolError("ComfyUI history did not include any audio outputs.")
