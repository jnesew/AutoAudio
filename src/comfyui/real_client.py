from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse
import urllib.request
import uuid
from typing import Any

import websocket

from comfyui.client import (
    AudioArtifact,
    ComfyUIConnectionError,
    ComfyUIProtocolError,
    ComfyUITimeoutError,
)
from comfyui.workflow_loader import build_runtime_workflow
from core.config import GenerationSettings


class RealComfyUIClient:
    def __init__(self, server_address: str, *, client_id: str | None = None) -> None:
        self.server_address = server_address
        self.client_id = client_id or str(uuid.uuid4())

    def _queue_prompt(self, prompt_workflow: dict[str, Any]) -> str:
        payload = {"prompt": prompt_workflow, "client_id": self.client_id}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"http://{self.server_address}/prompt", data=data)

        try:
            response = urllib.request.urlopen(req)
            result = json.loads(response.read())
            prompt_id = result.get("prompt_id")
            if not prompt_id:
                raise ComfyUIProtocolError("Missing prompt_id in /prompt response.")
            return prompt_id
        except ComfyUIProtocolError:
            raise
        except Exception as exc:
            raise ComfyUIConnectionError(f"Failed to submit prompt to ComfyUI: {exc}") from exc

    def _wait_for_completion(self, prompt_id: str, *, timeout_seconds: float | None) -> None:
        ws = websocket.WebSocket()
        ws.settimeout(timeout_seconds)
        try:
            ws.connect(f"ws://{self.server_address}/ws?clientId={self.client_id}")
            while True:
                try:
                    out = ws.recv()
                except TimeoutError as exc:
                    raise ComfyUITimeoutError(f"Prompt {prompt_id} timed out waiting for websocket completion.") from exc

                if not isinstance(out, str):
                    continue

                message = json.loads(out)
                if message.get("type") != "executing":
                    continue

                data = message.get("data", {})
                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    return
        except ComfyUITimeoutError:
            raise
        except Exception as exc:
            raise ComfyUIConnectionError(f"ComfyUI websocket connection failed: {exc}") from exc
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _get_history(self, prompt_id: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"http://{self.server_address}/history/{prompt_id}") as response:
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

    def _fetch_audio(self, *, filename: str, subfolder: str, folder_type: str) -> bytes:
        data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        url_values = urllib.parse.urlencode(data)
        try:
            with urllib.request.urlopen(f"http://{self.server_address}/view?{url_values}") as response:
                return response.read()
        except Exception as exc:
            raise ComfyUIConnectionError(f"Failed to download audio from ComfyUI: {exc}") from exc

    def _upload_audio(self, *, file_path: str, target_filename: str) -> None:
        boundary = f"----AutoAudioBoundary{uuid.uuid4().hex}"
        mime_type = mimetypes.guess_type(target_filename)[0] or "application/octet-stream"
        with open(file_path, "rb") as file:
            file_bytes = file.read()

        body = bytearray()

        def add_field(name: str, value: str) -> None:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        add_field("type", "input")
        add_field("overwrite", "true")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="image"; filename="{target_filename}"\r\n'
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(file_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        request = urllib.request.Request(
            f"http://{self.server_address}/upload/image",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request).read()
        except Exception as exc:
            raise ComfyUIConnectionError(f"Failed to upload reference voice to ComfyUI: {exc}") from exc

    def generate_audio(
        self,
        *,
        workflow_template: dict[str, Any],
        text_segment: str,
        reference_voice: str,
        settings: GenerationSettings,
        timeout_seconds: float | None = 120,
    ) -> AudioArtifact:
        workflow = build_runtime_workflow(
            workflow_template=workflow_template,
            text_segment=text_segment,
            reference_voice=reference_voice,
            settings=settings,
        )

        prompt_id = self._queue_prompt(workflow)
        self._wait_for_completion(prompt_id, timeout_seconds=timeout_seconds)
        outputs = self._get_history(prompt_id)

        for node_output in outputs.values():
            audio_files = node_output.get("audio", [])
            for audio_file in audio_files:
                content = self._fetch_audio(
                    filename=audio_file["filename"],
                    subfolder=audio_file["subfolder"],
                    folder_type=audio_file["type"],
                )
                _, ext = os.path.splitext(audio_file["filename"])
                return AudioArtifact(content=content, extension=ext.lower() or ".flac")

        raise ComfyUIProtocolError("ComfyUI history did not include any audio outputs.")

    def upload_reference_voice(
        self,
        *,
        file_path: str,
        target_filename: str,
        upload_workflow_template: dict[str, Any],
        timeout_seconds: float | None = 120,
    ) -> None:
        self._upload_audio(file_path=file_path, target_filename=target_filename)

        workflow = dict(upload_workflow_template)
        load_audio_node = dict(workflow.get("1", {}))
        load_audio_inputs = dict(load_audio_node.get("inputs", {}))
        load_audio_inputs["audio"] = target_filename
        load_audio_node["inputs"] = load_audio_inputs
        workflow["1"] = load_audio_node

        prompt_id = self._queue_prompt(workflow)
        self._wait_for_completion(prompt_id, timeout_seconds=timeout_seconds)
