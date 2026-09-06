from tts.base import (
    AudioArtifact,
    ProviderIdentity,
    SpeechProvider,
    SynthesisPurpose,
    TTSClientError,
    TTSConfigurationError,
    TTSConnectionError,
    TTSProtocolError,
    VoiceDiscoveryUnsupported,
    VoiceInfo,
)
from tts.router import build_speech_provider, discover_provider_voices

__all__ = [
    "AudioArtifact",
    "ProviderIdentity",
    "SpeechProvider",
    "SynthesisPurpose",
    "TTSClientError",
    "TTSConfigurationError",
    "TTSConnectionError",
    "TTSProtocolError",
    "VoiceDiscoveryUnsupported",
    "VoiceInfo",
    "build_speech_provider",
    "discover_provider_voices",
]
