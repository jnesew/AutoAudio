from __future__ import annotations

import io
import shutil
import subprocess
import wave
import logging
import sys
import warnings
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from provenance import audio_watermark  # noqa: E402


class _FakeModel:
    def __init__(self):
        self.devices: list[str] = []
        self.eval_calls = 0

    def to(self, device: str):
        self.devices.append(device)
        return self

    def eval(self):
        self.eval_calls += 1
        return self


def test_audioseal_model_loading_suppresses_only_known_dependency_warnings():
    generator = _FakeModel()
    detector = _FakeModel()

    def emit_dependency_warning(message: str, module: str):
        warnings.warn_explicit(
            message,
            FutureWarning,
            filename=f"{module.replace('.', '/')}.py",
            lineno=1,
            module=module,
        )

    def load_generator(_name: str):
        emit_dependency_warning(
            "`torch.jit.script` is not supported in Python 3.14+ and may break.",
            "torch.jit._script",
        )
        warnings.warn("unrelated AudioSeal warning", FutureWarning)
        return generator

    def load_detector(_name: str):
        emit_dependency_warning(
            "`torch.nn.utils.weight_norm` is deprecated in favor of its replacement.",
            "torch.nn.utils.weight_norm",
        )
        return detector

    audioseal_module = ModuleType("audioseal")
    audioseal_module.AudioSeal = SimpleNamespace(
        load_generator=Mock(side_effect=load_generator),
        load_detector=Mock(side_effect=load_detector),
    )

    audio_watermark._load_audioseal_models.cache_clear()
    try:
        with warnings.catch_warnings(record=True) as captured, patch.dict(
            sys.modules, {"audioseal": audioseal_module}
        ), patch(
            "provenance.audio_watermark.warnings.filterwarnings",
            wraps=warnings.filterwarnings,
        ) as filterwarnings:
            warnings.simplefilter("always")
            loaded_generator, loaded_detector = audio_watermark._load_audioseal_models("cpu")
    finally:
        audio_watermark._load_audioseal_models.cache_clear()

    assert (loaded_generator, loaded_detector) == (generator, detector)
    assert generator.devices == ["cpu"]
    assert detector.devices == ["cpu"]
    assert generator.eval_calls == 1
    assert detector.eval_calls == 1
    assert [str(warning.message) for warning in captured] == ["unrelated AudioSeal warning"]
    assert filterwarnings.call_args_list == [
        call(
            "ignore",
            message=audio_watermark._AUDIOSEAL_JIT_WARNING,
            category=FutureWarning,
            module=r"torch\.jit\._script",
        ),
        call(
            "ignore",
            message=audio_watermark._AUDIOSEAL_WEIGHT_NORM_WARNING,
            category=FutureWarning,
            module=r"torch\.nn\.utils\.weight_norm",
        ),
    ]


def _wav_bytes(samples, rate=24000, subtype="PCM_16"):
    sf = pytest.importorskip("soundfile")
    output = io.BytesIO()
    sf.write(output, samples, rate, format="WAV", subtype=subtype)
    return output.getvalue()


@pytest.mark.parametrize("output_format", ["wav", "s16le"])
def test_verification_uses_exact_exported_pcm_without_codec_roundtrip(monkeypatch, output_format):
    np = pytest.importorskip("numpy")
    torch = pytest.importorskip("torch")
    sf = pytest.importorskip("soundfile")
    samples = np.array([-1.0, -0.12345, 0, 0.12345, 0.99999, 1.0], dtype=np.float32)
    source = _wav_bytes(samples, subtype="FLOAT")
    message = torch.zeros((1, 16), dtype=torch.int64)
    verified = []

    def embed(wav, *, message):
        assert torch.is_inference_mode_enabled()
        assert wav.shape == (1, 1, len(samples))
        return torch.full_like(wav, 0.0001)

    def detect(wav):
        assert torch.is_inference_mode_enabled()
        verified.append(wav.clone())
        return torch.tensor([1.0]), message

    monkeypatch.setattr(audio_watermark, "_derive_16bit_message", lambda *args: message)
    generator = SimpleNamespace(get_watermark=Mock(side_effect=embed))
    detector = SimpleNamespace(detect_watermark=Mock(side_effect=detect))
    monkeypatch.setattr(audio_watermark, "_load_audioseal_models", lambda _: (generator, detector))
    with patch.object(audio_watermark.subprocess, "run", side_effect=AssertionError("unexpected FFmpeg")):
        result = audio_watermark.watermark_audio_bytes(
            source, content_id="segment", secret_key="key", output_format=output_format,
        )
    if output_format == "wav":
        with wave.open(io.BytesIO(result)) as output:
            assert (output.getframerate(), output.getnchannels(), output.getsampwidth()) == (24000, 1, 2)
            result = output.readframes(output.getnframes())
    actual = np.frombuffer(result, dtype="<i2").astype(np.float32) / 32768
    np.testing.assert_array_equal(verified[0].numpy().reshape(-1), actual)
    reference, _ = sf.read(io.BytesIO(_wav_bytes(samples + np.float32(0.0001))), dtype="float32")
    np.testing.assert_array_equal(actual, reference)
    assert generator.get_watermark.call_count == detector.detect_watermark.call_count == 1


@pytest.mark.parametrize("probability,message,error", [
    (0.49, "valid", RuntimeError), (float("nan"), "valid", RuntimeError),
    (float("inf"), "valid", RuntimeError), (1.0, "wrong", RuntimeError),
    (1.0, "shape", audio_watermark.AudioSealCompatibilityError),
    (1.0, "object", audio_watermark.AudioSealCompatibilityError),
])
def test_verification_rejects_invalid_evidence(probability, message, error):
    torch = pytest.importorskip("torch")
    expected = torch.zeros((1, 16), dtype=torch.int64)
    detected = {"valid": expected, "wrong": torch.ones_like(expected),
                "shape": torch.zeros(16), "object": object()}[message]
    detector = SimpleNamespace(detect_watermark=lambda _: (probability, detected))
    with pytest.raises(error, match="verification"):
        audio_watermark._verify_watermark(detector, object(), expected, 0.5)


@pytest.mark.parametrize("rate,channels", [(24000, 1), (48000, 2), (22050, 1)])
def test_decode_normalizes_provider_audio_once(rate, channels):
    np = pytest.importorskip("numpy")
    pytest.importorskip("soundfile")
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg required")
    samples = np.zeros((rate // 10, channels), dtype=np.float32)
    with patch.object(audio_watermark.subprocess, "run", wraps=subprocess.run) as decode:
        result = audio_watermark._decode_audio(_wav_bytes(samples, rate))
    assert result.shape == (2400,)
    assert result.dtype == np.float32
    assert decode.call_count == (0 if (rate, channels) == (24000, 1) else 1)


def test_decode_supports_compressed_provider_response():
    np = pytest.importorskip("numpy")
    pytest.importorskip("soundfile")
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg required")
    source = _wav_bytes(np.zeros(2400, dtype=np.float32))
    aac = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "adts", "pipe:1"],
                         input=source, capture_output=True, check=True).stdout
    with patch.object(audio_watermark.subprocess, "run", wraps=subprocess.run) as decode:
        result = audio_watermark._decode_audio(aac)
    assert result.size >= 2400
    assert decode.call_count == 1


@pytest.mark.parametrize("samples", [[], [float("nan")], [float("inf")]])
def test_decode_rejects_empty_or_nonfinite_input(samples):
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError, match="finite audio"):
        audio_watermark._decode_audio(_wav_bytes(np.array(samples, dtype=np.float32), subtype="FLOAT"))


def test_nonfinite_generator_output_is_rejected(monkeypatch):
    np = pytest.importorskip("numpy")
    torch = pytest.importorskip("torch")
    generator = SimpleNamespace(get_watermark=lambda wav, **kw: torch.full_like(wav, float("nan")))
    monkeypatch.setattr(audio_watermark, "_load_audioseal_models", lambda _: (generator, object()))
    with pytest.raises(RuntimeError, match="non-finite"):
        audio_watermark.watermark_audio_bytes(_wav_bytes(np.zeros(2400)), content_id="s", secret_key="k")


@pytest.mark.parametrize(
    ("requested", "available", "expected"),
    (("auto", True, "cuda"), ("auto", False, "cpu"), ("cpu", True, "cpu"), ("CPU", False, "cpu")),
)
def test_resolve_audioseal_device(requested, available, expected, monkeypatch):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: available))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert audio_watermark.resolve_audioseal_device(requested) == expected


def test_explicit_cuda_requires_available_pytorch_device(monkeypatch):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    with pytest.raises(RuntimeError, match="CUDA/ROCm"):
        audio_watermark.resolve_audioseal_device("cuda")


def test_invalid_audioseal_device_is_rejected(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))

    with pytest.raises(ValueError, match="auto, cpu, cuda"):
        audio_watermark.resolve_audioseal_device("xpu")


def test_audioseal_model_device_failure_is_not_ignored(monkeypatch):
    class FailingModel:
        def to(self, device):
            raise RuntimeError(f"cannot use {device}")

        def eval(self):
            raise AssertionError("eval must not follow a failed device transfer")

    class FakeAudioSeal:
        @staticmethod
        def load_generator(_name):
            return FailingModel()

        @staticmethod
        def load_detector(_name):
            return FailingModel()

    fake_audioseal = ModuleType("audioseal")
    fake_audioseal.AudioSeal = FakeAudioSeal
    monkeypatch.setitem(sys.modules, "audioseal", fake_audioseal)
    audio_watermark._load_audioseal_models.cache_clear()

    with pytest.raises(RuntimeError, match="cannot use cuda"):
        audio_watermark._load_audioseal_models("cuda")

    audio_watermark._load_audioseal_models.cache_clear()


def test_watermark_api_never_falls_back_to_an_unidentified_payload():
    calls = []

    class IncompatibleGenerator:
        def get_watermark(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise TypeError("unsupported signature")

    message = object()
    with pytest.raises(audio_watermark.AudioSealCompatibilityError, match="required 0.2 message API"):
        audio_watermark._watermark_with_message(IncompatibleGenerator(), object(), message)

    assert len(calls) == 1
    assert calls[0][1]["message"] is message


def test_auto_device_retries_cpu_after_cuda_runtime_failure(monkeypatch, caplog):
    monkeypatch.delenv("AUTOAUDIO_WATERMARK_DEVICE", raising=False)
    caplog.set_level(logging.WARNING)

    with patch("provenance.audio_watermark.resolve_audioseal_device", return_value="cuda"), patch(
        "provenance.audio_watermark.watermark_audio_bytes",
        side_effect=[RuntimeError("GPU allocation failed"), b"cpu-watermarked"],
    ) as watermark:
        result, audio = audio_watermark.watermark_audio_bytes_best_effort(
            b"source", content_id="segment", device="auto"
        )

    assert result.applied is True
    assert result.verified is True
    assert result.detail == "verified; requested=auto; device=cpu; fallback_from=cuda"
    assert audio == b"cpu-watermarked"
    assert [entry.kwargs["device"] for entry in watermark.call_args_list] == ["cuda", "cpu"]
    assert "retrying on CPU" in caplog.text


def test_explicit_cuda_failure_does_not_fall_back(monkeypatch):
    monkeypatch.delenv("AUTOAUDIO_WATERMARK_DEVICE", raising=False)

    with patch("provenance.audio_watermark.resolve_audioseal_device", return_value="cuda"), patch(
        "provenance.audio_watermark.watermark_audio_bytes", side_effect=RuntimeError("GPU failure")
    ) as watermark:
        result, audio = audio_watermark.watermark_audio_bytes_best_effort(
            b"source", content_id="segment", device="cuda"
        )

    assert result.applied is False
    assert audio == b"source"
    assert watermark.call_count == 1


def test_environment_device_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("AUTOAUDIO_WATERMARK_DEVICE", "cpu")

    with patch("provenance.audio_watermark.resolve_audioseal_device", return_value="cpu") as resolve, patch(
        "provenance.audio_watermark.watermark_audio_bytes", return_value=b"marked"
    ) as watermark:
        result, audio = audio_watermark.watermark_audio_bytes_best_effort(
            b"source", content_id="segment", device="auto"
        )

    assert result.applied is True
    assert audio == b"marked"
    assert resolve.call_args == call("cpu")
    assert watermark.call_args.kwargs["device"] == "cpu"


def test_message_api_incompatibility_does_not_trigger_device_fallback(monkeypatch):
    monkeypatch.delenv("AUTOAUDIO_WATERMARK_DEVICE", raising=False)

    with patch("provenance.audio_watermark.resolve_audioseal_device", return_value="cuda"), patch(
        "provenance.audio_watermark.watermark_audio_bytes",
        side_effect=audio_watermark.AudioSealCompatibilityError("message API missing"),
    ) as watermark:
        result, audio = audio_watermark.watermark_audio_bytes_best_effort(
            b"source", content_id="segment", device="auto"
        )

    assert result.applied is False
    assert audio == b"source"
    assert watermark.call_count == 1


def test_pipeline_flac_preserves_verified_samples_and_manifest(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    torch = pytest.importorskip("torch")
    sf = pytest.importorskip("soundfile")
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg required")
    from core.pipeline import write_watermarked_audio_artifact
    from provenance.ai_marking import validate_watermarked_artifact

    source = _wav_bytes(np.linspace(-0.9, 0.9, 2400, dtype=np.float32))
    expected = []
    message = torch.zeros((1, 16), dtype=torch.int64)

    def detect(wav):
        expected.append(wav.numpy().reshape(-1).copy())
        return 1.0, message

    monkeypatch.delenv("AUTOAUDIO_WATERMARK_DEVICE", raising=False)
    monkeypatch.setattr(audio_watermark, "_derive_16bit_message", lambda *args: message)
    monkeypatch.setattr(audio_watermark, "_load_audioseal_models", lambda _: (
        SimpleNamespace(get_watermark=lambda wav, **kw: torch.full_like(wav, 0.001)),
        SimpleNamespace(detect_watermark=detect),
    ))
    output = tmp_path / "segment.flac"
    write_watermarked_audio_artifact(
        audio_data=source, output_path=output, content_id="segment", watermark_device="cpu",
        ai_provider="test", logger=logging.getLogger("test"),
    )
    decoded, rate = sf.read(output, dtype="float32")
    assert rate == 24000
    np.testing.assert_array_equal(decoded, expected[0])
    validate_watermarked_artifact(output)
