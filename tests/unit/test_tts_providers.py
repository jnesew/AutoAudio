from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.config import AppConfig, GenerationSettings, TTSConfig
from tts import TTSConfigurationError, VoiceDiscoveryUnsupported, discover_provider_voices
from tts.http import ElevenLabsSpeechProvider, OpenAICompatibleSpeechProvider
from tts.router import build_speech_provider


class FakeHeaders(dict):
    def get_content_type(self) -> str:
        return str(self.get("Content-Type", "")).split(";", 1)[0]


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str):
        self.payload = payload
        self.headers = FakeHeaders({"Content-Type": content_type})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


def test_provider_construction_is_network_inert(monkeypatch):
    calls = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("provider construction contacted the network")

    config = TTSConfig(
        provider="openai-compatible",
        base_url="http://localhost:9000/v1",
        model="local-model",
        voice="narrator",
    )
    provider = OpenAICompatibleSpeechProvider(config, urlopen=fail_if_called)

    assert provider.identity.provider_id == "openai-compatible"
    assert calls == []


def test_openai_compatible_generation_uses_standard_speech_contract(monkeypatch):
    requests = []

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse(b"RIFF" + b"\x00" * 32, "audio/wav")

    monkeypatch.setenv("TEST_TTS_KEY", "secret-value")
    provider = OpenAICompatibleSpeechProvider(
        TTSConfig(
            provider="openai-compatible",
            base_url="https://speech.example/v1",
            api_key_env="TEST_TTS_KEY",
            model="speech-model",
            voice="calm-reader",
            response_format="wav",
        ),
        urlopen=urlopen,
        sleep=lambda _seconds: None,
    )

    artifact = provider.generate_audio(
        text_segment="Narrate this paragraph.",
        instructions="Speak clearly.",
        timeout_seconds=12,
    )

    request, timeout = requests[0]
    assert request.full_url == "https://speech.example/v1/audio/speech"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer secret-value"
    assert timeout == 12
    assert json.loads(request.data) == {
        "model": "speech-model",
        "voice": "calm-reader",
        "input": "Narrate this paragraph.",
        "response_format": "wav",
        "instructions": "Speak clearly.",
    }
    assert artifact.extension == ".wav"


def test_openai_compatible_disclosure_uses_neutral_instruction():
    requests = []

    def urlopen(request, *, timeout):
        del timeout
        requests.append(request)
        return FakeResponse(b"ID3" + b"\x00" * 32, "audio/mpeg")

    provider = OpenAICompatibleSpeechProvider(
        TTSConfig(
            provider="openai-compatible",
            model="speech-model",
            voice="reader",
            response_format="mp3",
        ),
        urlopen=urlopen,
    )
    provider.generate_audio(
        text_segment="This audio was generated synthetically.",
        purpose="disclosure",
        instructions="Use a dramatic delivery.",
    )

    payload = json.loads(requests[0].data)
    assert payload["instructions"] == "Use a neutral, clear announcement voice with steady pacing."


def test_openai_compatible_voice_discovery_is_not_invented():
    provider = OpenAICompatibleSpeechProvider(
        TTSConfig(provider="openai-compatible", model="model", voice="voice")
    )

    with pytest.raises(VoiceDiscoveryUnsupported, match="standard voice-list"):
        provider.discover_voices()


def test_elevenlabs_generation_uses_existing_voice_without_clone_inputs(monkeypatch):
    requests = []

    def urlopen(request, *, timeout):
        del timeout
        requests.append(request)
        return FakeResponse(b"ID3" + b"\x00" * 32, "audio/mpeg")

    monkeypatch.setenv("TEST_ELEVEN_KEY", "eleven-secret")
    provider = ElevenLabsSpeechProvider(
        TTSConfig(
            provider="elevenlabs",
            base_url="https://api.elevenlabs.io/v1",
            api_key_env="TEST_ELEVEN_KEY",
            model="eleven_multilingual_v2",
            voice="voice/id",
            response_format="mp3_44100_128",
            language_code="fi",
        ),
        urlopen=urlopen,
    )

    provider.generate_audio(text_segment="Hyvää huomenta.")

    request = requests[0]
    assert request.full_url == (
        "https://api.elevenlabs.io/v1/text-to-speech/voice%2Fid?output_format=mp3_44100_128"
    )
    assert request.get_header("Xi-api-key") == "eleven-secret"
    payload = json.loads(request.data)
    assert payload == {
        "text": "Hyvää huomenta.",
        "model_id": "eleven_multilingual_v2",
        "language_code": "fi",
    }
    assert not any("reference" in key or "clone" in key for key in payload)


def test_elevenlabs_discovery_runs_only_when_explicitly_called_and_filters_clones(monkeypatch):
    calls = []
    response = {
        "voices": [
            {"voice_id": "premade-1", "name": "Alice", "category": "premade"},
            {"voice_id": "clone-1", "name": "Copied voice", "category": "cloned"},
            {"voice_id": "generated-1", "name": "Designed voice", "category": "generated"},
            {"voice_id": "professional-1", "name": "Professional clone", "category": "professional"},
        ],
        "has_more": False,
        "next_page_token": None,
    }

    def urlopen(request, *, timeout):
        del timeout
        calls.append(request)
        return FakeResponse(json.dumps(response).encode("utf-8"), "application/json")

    monkeypatch.setenv("TEST_ELEVEN_KEY", "secret")
    provider = ElevenLabsSpeechProvider(
        TTSConfig(provider="elevenlabs", api_key_env="TEST_ELEVEN_KEY"),
        urlopen=urlopen,
    )

    assert calls == []
    voices = provider.discover_voices(timeout_seconds=10)

    assert len(calls) == 1
    assert calls[0].full_url.startswith("https://api.elevenlabs.io/v2/voices?")
    assert [(voice.id, voice.category) for voice in voices] == [
        ("premade-1", "premade"),
        ("generated-1", "generated"),
    ]


def test_api_key_value_is_not_part_of_provider_identity(monkeypatch):
    monkeypatch.setenv("PRIVATE_TTS_TOKEN", "do-not-persist-this")
    provider = ElevenLabsSpeechProvider(
        TTSConfig(provider="elevenlabs", api_key_env="PRIVATE_TTS_TOKEN", voice="voice-id")
    )

    serialized = json.dumps(provider.identity.compatibility)
    assert "do-not-persist-this" not in serialized
    assert "PRIVATE_TTS_TOKEN" not in serialized


def test_router_validates_generation_settings_without_network(tmp_path):
    app_config = AppConfig(
        project_root=tmp_path,
        tts=TTSConfig(provider="openai-compatible", model="", voice=""),
    )

    with pytest.raises(TTSConfigurationError, match="model and a voice"):
        build_speech_provider(
            config=app_config,
            narration_workflow=None,
            disclosure_workflow=None,
            narration_settings=GenerationSettings(),
            disclosure_settings=GenerationSettings(),
        )


def test_raw_provider_audio_formats_are_rejected_before_request(monkeypatch):
    monkeypatch.setenv("TEST_ELEVEN_KEY", "secret")
    calls = []
    provider = ElevenLabsSpeechProvider(
        TTSConfig(
            provider="elevenlabs",
            api_key_env="TEST_ELEVEN_KEY",
            voice="voice-id",
            response_format="pcm_44100",
        ),
        urlopen=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(TTSConfigurationError, match="Raw PCM"):
        provider.generate_audio(text_segment="Text")
    assert calls == []


def test_config_rejects_invalid_endpoint_and_environment_variable():
    with pytest.raises(ValueError, match="absolute"):
        TTSConfig(provider="openai-compatible", base_url="localhost:8000")
    with pytest.raises(ValueError, match="cannot contain credentials"):
        TTSConfig(provider="openai-compatible", base_url="https://user:password@example.test/v1")
    with pytest.raises(ValueError, match="query string"):
        TTSConfig(provider="openai-compatible", base_url="https://example.test/v1?token=secret")
    with pytest.raises(ValueError, match="environment variable"):
        TTSConfig(provider="elevenlabs", api_key_env="bad-name")


def test_openai_discovery_router_does_not_contact_an_endpoint():
    with pytest.raises(VoiceDiscoveryUnsupported, match="standard voice-list"):
        discover_provider_voices(
            TTSConfig(
                provider="openai-compatible",
                base_url="https://speech.example/v1",
                model="model",
                voice="voice",
            )
        )
