from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from core.cancellation import CancellationToken
from core.config import TTSConfig
from core.errors import PipelineCancelled
from tts.base import (
    AudioArtifact,
    ProviderIdentity,
    SynthesisPurpose,
    TTS_PROVIDER_ADAPTER_VERSION,
    TTSConfigurationError,
    TTSConnectionError,
    TTSProtocolError,
    VoiceDiscoveryUnsupported,
    VoiceInfo,
)


DEFAULT_OPENAI_COMPATIBLE_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
_RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
_AUDIO_CONTENT_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
}


def _ensure_self_describing_audio_format(response_format: str) -> None:
    normalized = response_format.strip().lower()
    if normalized in {"pcm", "ulaw", "mulaw"} or normalized.startswith(("pcm_", "ulaw_", "mulaw_")):
        raise TTSConfigurationError(
            "Raw PCM/μ-law provider responses are not supported; choose a self-describing format such as WAV, MP3, FLAC, or Opus."
        )


def _bounded_timeout(timeout_seconds: float | None) -> float:
    if timeout_seconds is None:
        return 120.0
    return max(0.1, float(timeout_seconds))


def _content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    get_content_type = getattr(headers, "get_content_type", None)
    if callable(get_content_type):
        return str(get_content_type()).lower()
    value = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
    return str(value).split(";", 1)[0].strip().lower()


def _error_detail(payload: bytes) -> str:
    if not payload:
        return "no response detail"
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload[:300].decode("utf-8", errors="replace").replace("\n", " ")
    if isinstance(decoded, dict):
        candidate = decoded.get("error", decoded.get("detail", decoded.get("message")))
        if isinstance(candidate, dict):
            candidate = candidate.get("message", candidate.get("detail"))
        if candidate:
            return str(candidate)[:300].replace("\n", " ")
    return "endpoint returned a JSON error"


class _HTTPProvider:
    supports_voice_discovery = False

    def __init__(
        self,
        config: TTSConfig,
        *,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._urlopen = urlopen
        self._sleep = sleep

    def _api_key(self, *, required: bool) -> str:
        key = os.environ.get(self.config.api_key_env, "").strip() if self.config.api_key_env else ""
        if required and not key:
            variable = self.config.api_key_env or "(not configured)"
            raise TTSConfigurationError(
                f"The selected provider requires an API key in environment variable {variable}."
            )
        return key

    def _request_bytes(
        self,
        request: urllib.request.Request,
        *,
        timeout_seconds: float | None,
        cancellation: CancellationToken | None = None,
        attempts: int = 3,
    ) -> tuple[bytes, str]:
        timeout = _bounded_timeout(timeout_seconds)
        for attempt in range(max(1, attempts)):
            if cancellation:
                cancellation.raise_if_cancelled()
            try:
                with self._urlopen(request, timeout=timeout) as response:
                    payload = response.read()
                    content_type = _content_type(response)
                if cancellation:
                    cancellation.raise_if_cancelled()
                return payload, content_type
            except urllib.error.HTTPError as exc:
                try:
                    payload = exc.read()
                except Exception:
                    payload = b""
                if exc.code in _RETRYABLE_HTTP_STATUS and attempt + 1 < attempts:
                    if cancellation:
                        cancellation.raise_if_cancelled()
                    self._sleep(min(0.5 * (2**attempt), 2.0))
                    continue
                raise TTSConnectionError(
                    f"TTS endpoint returned HTTP {exc.code}: {_error_detail(payload)}"
                ) from exc
            except PipelineCancelled:
                raise
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                if attempt + 1 < attempts:
                    if cancellation:
                        cancellation.raise_if_cancelled()
                    self._sleep(min(0.5 * (2**attempt), 2.0))
                    continue
                reason = getattr(exc, "reason", exc)
                raise TTSConnectionError(f"TTS endpoint request failed: {reason}") from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _validate_audio(payload: bytes, content_type: str) -> None:
        if len(payload) <= 16:
            raise TTSProtocolError("TTS endpoint returned an empty or invalid audio payload.")
        if content_type == "application/json" or content_type.endswith("+json"):
            raise TTSProtocolError(f"TTS endpoint returned JSON instead of audio: {_error_detail(payload)}")
        if content_type and not content_type.startswith("audio/") and content_type not in _AUDIO_CONTENT_TYPES:
            raise TTSProtocolError(f"TTS endpoint returned unsupported content type {content_type!r}.")


def _openai_speech_url(base_url: str) -> str:
    base = (base_url or DEFAULT_OPENAI_COMPATIBLE_BASE_URL).rstrip("/")
    path = urllib.parse.urlparse(base).path.rstrip("/")
    if path.endswith("/audio/speech"):
        return base
    if path.endswith("/v1"):
        return f"{base}/audio/speech"
    return f"{base}/v1/audio/speech"


def _elevenlabs_root(base_url: str) -> str:
    base = (base_url or DEFAULT_ELEVENLABS_BASE_URL).rstrip("/")
    parsed = urllib.parse.urlparse(base)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1") or path.endswith("/v2"):
        path = path[:-3]
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "", ""))


class OpenAICompatibleSpeechProvider(_HTTPProvider):
    """Adapter for the conventional POST /v1/audio/speech contract."""

    def __init__(self, config: TTSConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        base_url = config.base_url or DEFAULT_OPENAI_COMPATIBLE_BASE_URL
        response_format = config.response_format or "wav"
        self.identity = ProviderIdentity(
            provider_id="openai-compatible",
            provider_name="OpenAI-compatible",
            model_name=config.model or "unconfigured",
            model_version="configured-endpoint",
            backend_name="OpenAI-compatible speech API",
            backend_version="v1/audio/speech",
            compatibility={
                "contract": TTS_PROVIDER_ADAPTER_VERSION,
                "adapter": "openai-compatible-v1",
                "base_url": base_url.rstrip("/"),
                "model": config.model,
                "voice": config.voice,
                "response_format": response_format,
                "language_code": config.language_code,
            },
        )

    def generate_audio(
        self,
        *,
        text_segment: str,
        purpose: SynthesisPurpose = "narration",
        timeout_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
        instructions: str = "",
    ) -> AudioArtifact:
        model = self.config.model.strip()
        voice = self.config.voice.strip()
        if not model or not voice:
            raise TTSConfigurationError("OpenAI-compatible TTS requires both a model and a voice.")
        _ensure_self_describing_audio_format(self.config.response_format or "wav")
        payload: dict[str, Any] = {
            "model": model,
            "voice": voice,
            "input": text_segment,
            "response_format": self.config.response_format or "wav",
        }
        effective_instructions = (
            "Use a neutral, clear announcement voice with steady pacing."
            if purpose == "disclosure"
            else instructions.strip()
        )
        if effective_instructions:
            payload["instructions"] = effective_instructions
        headers = {"Content-Type": "application/json", "Accept": "audio/*"}
        api_key = self._api_key(required=False)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            _openai_speech_url(self.config.base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        content, content_type = self._request_bytes(
            request,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
        )
        self._validate_audio(content, content_type)
        response_format = self.config.response_format or "wav"
        return AudioArtifact(content=content, extension=f".{response_format.lower().lstrip('.')}")

    def discover_voices(self, *, timeout_seconds: float | None = None) -> tuple[VoiceInfo, ...]:
        del timeout_seconds
        raise VoiceDiscoveryUnsupported(
            "OpenAI-compatible speech does not define a standard voice-list endpoint; enter the endpoint's voice id manually."
        )


class ElevenLabsSpeechProvider(_HTTPProvider):
    """Native non-cloning ElevenLabs speech adapter.

    This adapter can synthesize with an existing voice and list eligible voices.
    It intentionally implements no voice upload, cloning, or voice-creation API.
    """

    supports_voice_discovery = True

    def __init__(self, config: TTSConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        base_url = config.base_url or DEFAULT_ELEVENLABS_BASE_URL
        model = config.model or "eleven_multilingual_v2"
        response_format = config.response_format or "mp3_44100_128"
        self.identity = ProviderIdentity(
            provider_id="elevenlabs",
            provider_name="ElevenLabs",
            model_name=model,
            model_version="configured-endpoint",
            backend_name="ElevenLabs text-to-speech API",
            backend_version="v1",
            compatibility={
                "contract": TTS_PROVIDER_ADAPTER_VERSION,
                "adapter": "elevenlabs-v1",
                "base_url": base_url.rstrip("/"),
                "model": model,
                "voice": config.voice,
                "response_format": response_format,
                "language_code": config.language_code,
            },
        )

    def generate_audio(
        self,
        *,
        text_segment: str,
        purpose: SynthesisPurpose = "narration",
        timeout_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
        instructions: str = "",
    ) -> AudioArtifact:
        del purpose, instructions
        voice_id = self.config.voice.strip()
        if not voice_id:
            raise TTSConfigurationError("ElevenLabs TTS requires an existing voice id.")
        model = self.config.model.strip() or "eleven_multilingual_v2"
        output_format = self.config.response_format.strip() or "mp3_44100_128"
        _ensure_self_describing_audio_format(output_format)
        payload: dict[str, Any] = {"text": text_segment, "model_id": model}
        if self.config.language_code.strip():
            payload["language_code"] = self.config.language_code.strip()
        root = _elevenlabs_root(self.config.base_url)
        voice_path = urllib.parse.quote(voice_id, safe="")
        query = urllib.parse.urlencode({"output_format": output_format})
        request = urllib.request.Request(
            f"{root}/v1/text-to-speech/{voice_path}?{query}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "audio/*",
                "xi-api-key": self._api_key(required=True),
            },
            method="POST",
        )
        content, content_type = self._request_bytes(
            request,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
        )
        self._validate_audio(content, content_type)
        extension = ".mp3"
        if output_format.startswith("wav_"):
            extension = ".wav"
        elif output_format.startswith("pcm_"):
            extension = ".pcm"
        elif output_format.startswith("opus_"):
            extension = ".opus"
        elif output_format.startswith("ulaw_"):
            extension = ".ulaw"
        return AudioArtifact(content=content, extension=extension)

    def discover_voices(self, *, timeout_seconds: float | None = None) -> tuple[VoiceInfo, ...]:
        root = _elevenlabs_root(self.config.base_url)
        api_key = self._api_key(required=True)
        voices: dict[str, VoiceInfo] = {}
        next_page_token = ""
        for _page in range(5):
            query_values = {"page_size": "100", "include_total_count": "false"}
            if next_page_token:
                query_values["next_page_token"] = next_page_token
            request = urllib.request.Request(
                f"{root}/v2/voices?{urllib.parse.urlencode(query_values)}",
                headers={"Accept": "application/json", "xi-api-key": api_key},
                method="GET",
            )
            payload, content_type = self._request_bytes(
                request,
                timeout_seconds=timeout_seconds,
                attempts=2,
            )
            if content_type and content_type != "application/json" and not content_type.endswith("+json"):
                raise TTSProtocolError(
                    f"ElevenLabs voice discovery returned unsupported content type {content_type!r}."
                )
            try:
                decoded = json.loads(payload.decode("utf-8"))
                raw_voices = decoded["voices"]
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise TTSProtocolError("ElevenLabs voice discovery returned malformed JSON.") from exc
            if not isinstance(raw_voices, list):
                raise TTSProtocolError("ElevenLabs voice discovery response has no voice list.")
            for raw_voice in raw_voices:
                if not isinstance(raw_voice, dict):
                    continue
                voice_id = str(raw_voice.get("voice_id") or "").strip()
                name = str(raw_voice.get("name") or "").strip()
                category = str(raw_voice.get("category") or "").strip().lower()
                # Deliberately keep discovery within AutoAudio's non-cloning scope.
                if voice_id and name and category in {"premade", "generated"}:
                    voices[voice_id] = VoiceInfo(id=voice_id, name=name, category=category)
            if not decoded.get("has_more"):
                break
            next_page_token = str(decoded.get("next_page_token") or "").strip()
            if not next_page_token:
                raise TTSProtocolError("ElevenLabs voice discovery pagination omitted its next-page token.")
        return tuple(sorted(voices.values(), key=lambda voice: (voice.name.casefold(), voice.id)))
