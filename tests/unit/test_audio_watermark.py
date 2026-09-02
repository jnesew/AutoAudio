from __future__ import annotations

import contextlib
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


class _FakeTensor:
    def __init__(self, name: str):
        self.name = name

    def float(self):
        return self

    def unsqueeze(self, _dimension: int):
        return self

    def to(self, _device: str):
        return self

    def squeeze(self, _dimension: int):
        return self

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.name

    def __add__(self, _other):
        return self


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


def test_audioseal_02_calls_do_not_pass_ignored_sample_rate():
    source_tensor = _FakeTensor("source")
    verification_tensor = _FakeTensor("verification")
    tensors = iter((source_tensor, verification_tensor))
    torch_module = ModuleType("torch")
    torch_module.from_numpy = Mock(side_effect=lambda _array: next(tensors))
    torch_module.no_grad = contextlib.nullcontext
    torch_module.clamp = Mock(side_effect=lambda tensor, _minimum, _maximum: tensor)
    torch_module.is_tensor = Mock(return_value=False)

    librosa_module = ModuleType("librosa")
    librosa_module.load = Mock(side_effect=(("decoded", 24000), ("verified", 24000)))
    soundfile_module = ModuleType("soundfile")
    soundfile_module.write = Mock(side_effect=lambda output, *_args, **_kwargs: output.write(b"encoded-wav"))

    message = _FakeTensor("message")
    watermark = _FakeTensor("watermark")
    generator = SimpleNamespace(get_watermark=Mock(return_value=watermark))
    detector = SimpleNamespace(detect_watermark=Mock(return_value=(1.0, object())))

    with patch.dict(
        sys.modules,
        {"librosa": librosa_module, "soundfile": soundfile_module, "torch": torch_module},
    ), patch(
        "provenance.audio_watermark._derive_16bit_message", return_value=message
    ), patch(
        "provenance.audio_watermark._load_audioseal_models", return_value=(generator, detector)
    ), patch(
        "provenance.audio_watermark.subprocess.run",
        return_value=SimpleNamespace(stdout=b"decoded-wav"),
    ):
        result = audio_watermark.watermark_audio_bytes(
            b"input-audio",
            content_id="book/chapter/segment",
            secret_key="test-secret",
        )

    assert result == b"encoded-wav"
    generator.get_watermark.assert_called_once_with(source_tensor, message=message)
    detector.detect_watermark.assert_called_once_with(verification_tensor)


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
