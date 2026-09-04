from __future__ import annotations

from typing import Any

from comfyui.client import (
    ComfyUIClientError,
    ComfyUIConnectionError,
    ComfyUIProtocolError,
    ComfyUITimeoutError,
)
from comfyui.real_client import RealComfyUIClient
from comfyui.spoof_client import SpoofComfyUIClient
from comfyui.workflow_loader import find_qwen_generation_node
from core.cancellation import CancellationToken
from core.config import AppConfig, GenerationSettings, QWEN_PRESET_SPEAKERS, TTSConfig
from tts.base import (
    AudioArtifact,
    ProviderIdentity,
    SpeechProvider,
    SynthesisPurpose,
    TTS_PROVIDER_ADAPTER_VERSION,
    TTSClientError,
    TTSConfigurationError,
    TTSConnectionError,
    TTSProtocolError,
    VoiceDiscoveryUnsupported,
    VoiceInfo,
)
from tts.http import ElevenLabsSpeechProvider, OpenAICompatibleSpeechProvider


class ComfyUISpeechProvider:
    supports_voice_discovery = True

    def __init__(
        self,
        *,
        config: AppConfig,
        narration_workflow: dict[str, Any],
        disclosure_workflow: dict[str, Any],
        narration_settings: GenerationSettings,
        disclosure_settings: GenerationSettings,
    ) -> None:
        if config.comfyui_mode == "spoof":
            self._client = SpoofComfyUIClient(scenario=config.comfyui_spoof_scenario)
        else:
            self._client = RealComfyUIClient(server_address=config.comfyui_server_address)
        self._narration_workflow = narration_workflow
        self._disclosure_workflow = disclosure_workflow
        self._narration_settings = narration_settings
        self._disclosure_settings = disclosure_settings

        node_id, _voice_mode = find_qwen_generation_node(narration_workflow)
        node = narration_workflow[node_id]
        class_type = str(node.get("class_type") or "unknown-backend")
        meta = node.get("_meta", {})
        reported_backend_version = meta.get("version") if isinstance(meta, dict) else None
        backend_version = (
            str(reported_backend_version).strip()
            if reported_backend_version is not None and str(reported_backend_version).strip()
            else "unreported"
        )
        self.identity = ProviderIdentity(
            provider_id="comfyui",
            provider_name="ComfyUI",
            model_name="Qwen3-TTS",
            model_version=narration_settings.model_choice.strip(),
            backend_name=class_type,
            backend_version=backend_version,
            compatibility={
                "contract": TTS_PROVIDER_ADAPTER_VERSION,
                "adapter": "comfyui-qwen-v1",
                "mode": config.comfyui_mode,
                "server_address": config.comfyui_server_address,
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
        del instructions
        if purpose == "disclosure":
            workflow = self._disclosure_workflow
            settings = self._disclosure_settings
        else:
            workflow = self._narration_workflow
            settings = self._narration_settings
        try:
            artifact = self._client.generate_audio(
                workflow_template=workflow,
                text_segment=text_segment,
                settings=settings,
                timeout_seconds=timeout_seconds,
                cancellation=cancellation,
            )
        except ComfyUIConnectionError as exc:
            raise TTSConnectionError(str(exc)) from exc
        except (ComfyUIProtocolError, ComfyUITimeoutError) as exc:
            raise TTSProtocolError(str(exc)) from exc
        except ComfyUIClientError as exc:
            raise TTSClientError(str(exc)) from exc
        return AudioArtifact(content=artifact.content, extension=artifact.extension)

    def discover_voices(self, *, timeout_seconds: float | None = None) -> tuple[VoiceInfo, ...]:
        del timeout_seconds
        return tuple(VoiceInfo(id=name, name=name, category="built-in") for name in QWEN_PRESET_SPEAKERS)


def build_speech_provider(
    *,
    config: AppConfig,
    narration_workflow: dict[str, Any] | None,
    disclosure_workflow: dict[str, Any] | None,
    narration_settings: GenerationSettings,
    disclosure_settings: GenerationSettings,
) -> SpeechProvider:
    provider = config.tts.provider
    if provider == "comfyui":
        if narration_workflow is None or disclosure_workflow is None:
            raise TTSProtocolError("ComfyUI provider requires bundled narration and disclosure workflows.")
        return ComfyUISpeechProvider(
            config=config,
            narration_workflow=narration_workflow,
            disclosure_workflow=disclosure_workflow,
            narration_settings=narration_settings,
            disclosure_settings=disclosure_settings,
        )
    if provider == "openai-compatible":
        if not config.tts.model.strip() or not config.tts.voice.strip():
            raise TTSConfigurationError("OpenAI-compatible TTS requires both a model and a voice.")
        return OpenAICompatibleSpeechProvider(config.tts)
    if provider == "elevenlabs":
        if not config.tts.voice.strip():
            raise TTSConfigurationError("ElevenLabs TTS requires an existing voice id.")
        return ElevenLabsSpeechProvider(config.tts)
    raise TTSProtocolError(f"Unsupported TTS provider: {provider!r}.")


def discover_provider_voices(
    config: TTSConfig,
    *,
    timeout_seconds: float | None = None,
) -> tuple[VoiceInfo, ...]:
    """Discover voices only when this function is explicitly invoked by a caller."""
    if config.provider == "comfyui":
        return tuple(VoiceInfo(id=name, name=name, category="built-in") for name in QWEN_PRESET_SPEAKERS)
    if config.provider == "openai-compatible":
        raise VoiceDiscoveryUnsupported(
            "OpenAI-compatible speech does not define a standard voice-list endpoint; enter a voice id manually."
        )
    if config.provider == "elevenlabs":
        return ElevenLabsSpeechProvider(config).discover_voices(timeout_seconds=timeout_seconds)
    raise VoiceDiscoveryUnsupported(f"Voice discovery is unavailable for provider {config.provider!r}.")
