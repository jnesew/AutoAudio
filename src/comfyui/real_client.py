from __future__ import annotations

import json
import logging
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
from core.http_io import ResponseTooLarge, read_response_bytes
from core.config import GenerationSettings
from core.errors import PipelineCancelled


_WEBSOCKET_TIMEOUT = getattr(websocket, "WebSocketTimeoutException", TimeoutError)


def _bounded_http_timeout(timeout_seconds: float | None) -> float:
    if timeout_seconds is None:
        return 30.0
    return max(0.1, min(float(timeout_seconds), 30.0))


def _execution_failure(data: dict[str, Any]) -> ComfyUIProtocolError:
    node = data.get("node_type") or data.get("node_id") or "unknown"
    detail = str(data.get("exception_message") or "Execution was interrupted.").strip()
    return ComfyUIProtocolError(f"ComfyUI node {node} failed: {detail[:2000]}")


def _close_websocket(ws) -> None:
    try:
        ws.close()
    except Exception:
        logging.getLogger("autoaudio.run").debug("Websocket cleanup failed", exc_info=True)


class RealComfyUIClient:
    def __init__(self, server_address: str, *, client_id: str | None = None) -> None:
        self.server_address = server_address
        self.client_id = client_id or str(uuid.uuid4())

    def _queue_prompt(self, prompt_workflow: dict[str, Any], *, timeout_seconds: float | None) -> str:
        payload = {"prompt": prompt_workflow, "client_id": self.client_id}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.server_address}/prompt", data=data,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=_bounded_http_timeout(timeout_seconds)) as response:
                result = json.loads(read_response_bytes(response, maximum_bytes=1024 * 1024))
            prompt_id = result.get("prompt_id") if isinstance(result, dict) else None
            if not isinstance(prompt_id, str) or not prompt_id:
                raise ComfyUIProtocolError("Missing prompt_id in /prompt response.")
            return prompt_id
        except ComfyUIProtocolError:
            raise
        except (json.JSONDecodeError, UnicodeError, ResponseTooLarge) as exc:
            raise ComfyUIProtocolError(f"Invalid ComfyUI prompt response: {exc}") from exc
        except Exception as exc:
            raise ComfyUIConnectionError(f"Failed to submit prompt to ComfyUI: {exc}") from exc

    def _connect_websocket(self, timeout_seconds: float | None):
        if websocket is None:
            raise ComfyUIConnectionError("websocket-client is required for network ComfyUI mode.")
        ws = websocket.WebSocket()
        try:
            timeout = min(timeout_seconds, 5.0) if timeout_seconds is not None else 5.0
            ws.connect(f"ws://{self.server_address}/ws?clientId={self.client_id}", timeout=timeout)
            return ws
        except Exception as exc:
            _close_websocket(ws)
            raise ComfyUIConnectionError(f"ComfyUI websocket connection failed: {exc}") from exc

    def _wait_for_completion(
        self,
        prompt_id: str,
        *,
        timeout_seconds: float | None,
        cancellation: CancellationToken | None,
        socket=None,
    ) -> None:
        owns_socket = socket is None
        ws = socket if socket is not None else self._connect_websocket(timeout_seconds)
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        try:
            while True:
                if cancellation:
                    cancellation.raise_if_cancelled()
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    if self._history_has_completed(prompt_id):
                        return
                    raise ComfyUITimeoutError(f"Prompt {prompt_id} timed out waiting for websocket completion.")
                ws.settimeout(min(0.5, remaining) if remaining is not None else 0.5)
                try:
                    out = ws.recv()
                except (TimeoutError, _WEBSOCKET_TIMEOUT):
                    continue
                except Exception as exc:
                    if self._history_has_completed(prompt_id):
                        return
                    raise ComfyUIConnectionError(f"ComfyUI websocket disconnected: {exc}") from exc

                if out == "":
                    if self._history_has_completed(prompt_id):
                        return
                    raise ComfyUIConnectionError("ComfyUI websocket closed before the prompt completed.")
                if not isinstance(out, str):
                    continue

                try:
                    message = json.loads(out)
                except json.JSONDecodeError as exc:
                    raise ComfyUIProtocolError("ComfyUI websocket returned malformed JSON.") from exc
                if not isinstance(message, dict) or not isinstance(message.get("data"), dict):
                    raise ComfyUIProtocolError("ComfyUI websocket returned a malformed event.")
                data = message.get("data", {})
                if (
                    message.get("type") in {"execution_error", "execution_interrupted"}
                    and data.get("prompt_id") == prompt_id
                ):
                    raise _execution_failure(data)
                if message.get("type") == "execution_success" and data.get("prompt_id") == prompt_id:
                    return
                if message.get("type") != "executing":
                    continue

                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    return
        except (ComfyUITimeoutError, ComfyUIProtocolError, PipelineCancelled):
            raise
        except Exception as exc:
            raise ComfyUIConnectionError(f"ComfyUI websocket connection failed: {exc}") from exc
        finally:
            if owns_socket:
                _close_websocket(ws)

    def _cancel_prompt(self, prompt_id: str) -> None:
        # Older ComfyUI versions ignore prompt_id on /interrupt and interrupt
        # unrelated work. Remove only our queued item; active server work may
        # finish independently after this client stops waiting.
        request = urllib.request.Request(
            f"http://{self.server_address}/queue",
            data=json.dumps({"delete": [prompt_id]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2):
                pass
        except Exception as exc:
            logging.getLogger("autoaudio.run").warning("Could not remove queued prompt %s: %s", prompt_id, exc)

    def _history_entry(self, prompt_id: str, timeout_seconds: float | None) -> dict | None:
        try:
            with urllib.request.urlopen(
                f"http://{self.server_address}/history/{prompt_id}",
                timeout=_bounded_http_timeout(timeout_seconds),
            ) as response:
                payload = json.loads(read_response_bytes(response, maximum_bytes=4 * 1024 * 1024))
        except (json.JSONDecodeError, UnicodeError, ResponseTooLarge) as exc:
            raise ComfyUIProtocolError(f"Invalid ComfyUI history response: {exc}") from exc
        except Exception as exc:
            raise ComfyUIConnectionError(f"Failed to fetch ComfyUI history: {exc}") from exc

        if not isinstance(payload, dict):
            raise ComfyUIProtocolError("ComfyUI history response must be an object.")
        history = payload.get(prompt_id)
        if history is None:
            return None
        if not isinstance(history, dict):
            raise ComfyUIProtocolError("ComfyUI prompt history must be an object.")

        status = history.get("status", {})
        if not isinstance(status, dict) or not isinstance(status.get("messages", []), list):
            raise ComfyUIProtocolError("ComfyUI history contains a malformed status.")
        for message in status.get("messages", []):
            if not isinstance(message, (list, tuple)) or len(message) != 2 or not isinstance(message[1], dict):
                raise ComfyUIProtocolError("ComfyUI history contains a malformed status event.")
            event, data = message
            if event in {"execution_error", "execution_interrupted"}:
                raise _execution_failure(data)
        if status.get("status_str") == "error":
            raise ComfyUIProtocolError(f"ComfyUI prompt {prompt_id} failed; check the ComfyUI server log.")
        return history

    def _history_has_completed(self, prompt_id: str) -> bool:
        try:
            history = self._history_entry(prompt_id, 1.0)
        except ComfyUIConnectionError:
            return False
        if history is None:
            return False
        status = history.get("status", {})
        return status.get("completed") is True or status.get("status_str") == "success" or (
            not status and bool(history.get("outputs"))
        )

    def _get_history(self, prompt_id: str, *, timeout_seconds: float | None) -> dict[str, Any]:
        history = self._history_entry(prompt_id, timeout_seconds)
        if history is None:
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
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> bytes:
        data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        url_values = urllib.parse.urlencode(data)
        try:
            with urllib.request.urlopen(
                f"http://{self.server_address}/view?{url_values}",
                timeout=_bounded_http_timeout(timeout_seconds),
            ) as response:
                return read_response_bytes(
                    response, maximum_bytes=64 * 1024 * 1024,
                    cancellation=cancellation, deadline=deadline,
                )
        except PipelineCancelled:
            raise
        except ResponseTooLarge as exc:
            raise ComfyUIProtocolError(str(exc)) from exc
        except TimeoutError as exc:
            raise ComfyUITimeoutError("ComfyUI audio download exceeded its timeout.") from exc
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

        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

        def remaining():
            if cancellation:
                cancellation.raise_if_cancelled()
            value = deadline - time.monotonic() if deadline is not None else None
            if value is not None and value <= 0:
                raise ComfyUITimeoutError("ComfyUI generation exceeded its total deadline.")
            return value

        # Establish the subscription before a fast/cached prompt can complete.
        ws = self._connect_websocket(remaining())
        prompt_id = None
        try:
            prompt_id = self._queue_prompt(workflow, timeout_seconds=remaining())
            self._wait_for_completion(
                prompt_id,
                timeout_seconds=remaining(),
                cancellation=cancellation,
                socket=ws,
            )
            outputs = self._get_history(prompt_id, timeout_seconds=remaining())
            if not isinstance(outputs, dict):
                raise ComfyUIProtocolError("ComfyUI history response missing outputs map.")
            for node_output in outputs.values():
                if not isinstance(node_output, dict):
                    raise ComfyUIProtocolError("ComfyUI returned a malformed node output.")
                audio_files = node_output.get("audio", [])
                if not isinstance(audio_files, list):
                    raise ComfyUIProtocolError("ComfyUI returned a malformed audio list.")
                for audio_file in audio_files:
                    if not isinstance(audio_file, dict) or not all(
                        isinstance(audio_file.get(key), str) for key in ("filename", "subfolder", "type")
                    ):
                        raise ComfyUIProtocolError("ComfyUI returned a malformed audio artifact.")
                    content = self._fetch_audio(
                        filename=audio_file["filename"],
                        subfolder=audio_file["subfolder"],
                        folder_type=audio_file["type"],
                        timeout_seconds=remaining(),
                        cancellation=cancellation,
                        deadline=deadline,
                    )
                    remaining()
                    _, ext = os.path.splitext(audio_file["filename"])
                    return AudioArtifact(content=content, extension=ext.lower() or ".flac")
            raise ComfyUIProtocolError("ComfyUI history did not include any audio outputs.")
        except (PipelineCancelled, ComfyUITimeoutError):
            if prompt_id is not None:
                self._cancel_prompt(prompt_id)
            raise
        finally:
            _close_websocket(ws)
