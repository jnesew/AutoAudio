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
    shape = (1, 16)
    device = "cpu"
    dtype = "int64"

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

    def __gt__(self, _other):
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
        with (
            warnings.catch_warnings(record=True) as captured,
            patch.dict(sys.modules, {"audioseal": audioseal_module}),
            patch(
                "provenance.audio_watermark.warnings.filterwarnings",
                wraps=warnings.filterwarnings,
            ) as filterwarnings,
        ):
            warnings.simplefilter("always")
            loaded_generator, loaded_detector = audio_watermark._load_audioseal_models(
                "cpu"
            )
    finally:
        audio_watermark._load_audioseal_models.cache_clear()

    assert (loaded_generator, loaded_detector) == (generator, detector)
    assert generator.devices == ["cpu"]
    assert detector.devices == ["cpu"]
    assert generator.eval_calls == 1
    assert detector.eval_calls == 1
    assert [str(warning.message) for warning in captured] == [
        "unrelated AudioSeal warning"
    ]
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


@pytest.mark.parametrize(
    ("probability", "message_shape", "message_matches", "error"),
    (
        (1.0, (1, 16), True, None),
        (0.1, (1, 16), True, "detector confidence"),
        (float("nan"), (1, 16), True, "detector confidence"),
        (float("inf"), (1, 16), True, "detector confidence"),
        (1.0, (1, 8), True, "invalid message shape"),
        (1.0, None, True, "invalid message shape"),
        (1.0, (1, 16), False, "did not round-trip"),
    ),
)
def test_audioseal_verifies_serialized_pcm_and_required_message(
    probability,
    message_shape,
    message_matches,
    error,
):
    source_tensor = _FakeTensor("source")
    verification_tensor = _FakeTensor("verification")
    tensors = iter((source_tensor, verification_tensor))
    torch_module = ModuleType("torch")
    torch_module.from_numpy = Mock(side_effect=lambda _array: next(tensors))
    torch_module.no_grad = contextlib.nullcontext
    torch_module.clamp = Mock(side_effect=lambda tensor, _minimum, _maximum: tensor)
    torch_module.is_tensor = lambda value: isinstance(value, _FakeTensor)
    torch_module.equal = Mock(return_value=message_matches)

    soundfile_module = ModuleType("soundfile")
    decoded_streams = []

    def read_pcm(stream, *, dtype):
        decoded_streams.append(stream.read())
        assert dtype == "float32"
        return "pcm", 24000

    soundfile_module.read = Mock(side_effect=read_pcm)
    soundfile_module.write = Mock(
        side_effect=lambda output, *_args, **_kwargs: output.write(b"encoded-wav")
    )

    message = _FakeTensor("message")
    watermark = _FakeTensor("watermark")
    generator = SimpleNamespace(get_watermark=Mock(return_value=watermark))
    detected = _FakeTensor("detected") if message_shape else object()
    if message_shape:
        detected.shape = message_shape
    detector = SimpleNamespace(
        detect_watermark=Mock(return_value=(probability, detected))
    )

    with (
        patch.dict(
            sys.modules,
            {"soundfile": soundfile_module, "torch": torch_module},
        ),
        patch("provenance.audio_watermark._derive_16bit_message", return_value=message),
        patch(
            "provenance.audio_watermark._load_audioseal_models",
            return_value=(generator, detector),
        ),
        patch(
            "provenance.audio_watermark.subprocess.run",
            return_value=SimpleNamespace(stdout=b"decoded-wav"),
        ),
        patch(
            "provenance.audio_watermark._audioseal_inference_context",
            lambda _device: contextlib.nullcontext(),
        ),
        patch(
            "provenance.audio_watermark._audioseal_detector_context",
            lambda _device: contextlib.nullcontext(),
        ),
    ):

        def run():
            return audio_watermark.watermark_audio_bytes(
                b"input-audio",
                content_id="book/chapter/segment",
                secret_key="test-secret",
            )

        if error:
            with pytest.raises(RuntimeError, match=error):
                run()
        else:
            assert run() == b"encoded-wav"

    assert decoded_streams == [b"decoded-wav", b"encoded-wav"]
    generator.get_watermark.assert_called_once_with(source_tensor, message=message)
    detector.detect_watermark.assert_called_once_with(verification_tensor)


@pytest.mark.parametrize(
    ("device", "initial_threads", "configured", "expected_threads"),
    (("cpu", 6, "", 4), ("cpu", 2, "", 2), ("cpu", 6, "1", 1), ("cuda", 6, "", 6)),
)
def test_audioseal_inference_restores_compilation_and_thread_settings_on_failure(
    monkeypatch,
    device,
    initial_threads,
    configured,
    expected_threads,
):
    compiler = SimpleNamespace(disabled=False, threads=initial_threads)
    torch_module = SimpleNamespace(
        get_num_threads=lambda: compiler.threads,
        set_num_threads=lambda value: setattr(compiler, "threads", value),
    )
    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setenv("AUTOAUDIO_WATERMARK_CPU_THREADS", configured)

    @contextlib.contextmanager
    def no_compile():
        compiler.disabled = True
        try:
            yield
        finally:
            compiler.disabled = False

    monkeypatch.setitem(
        sys.modules,
        "audioseal.libs.moshi.utils.compile",
        SimpleNamespace(no_compile=no_compile),
    )
    with pytest.raises(RuntimeError, match="inference failure"):
        with audio_watermark._audioseal_inference_context(device):
            assert compiler.disabled
            assert compiler.threads == expected_threads
            raise RuntimeError("inference failure")
    assert not compiler.disabled
    assert compiler.threads == initial_threads


@pytest.mark.parametrize("configured", ("0", "-1", "invalid"))
def test_invalid_cpu_thread_override_is_rejected_without_changing_threads(
    monkeypatch, configured
):
    set_threads = Mock()
    monkeypatch.setenv("AUTOAUDIO_WATERMARK_CPU_THREADS", configured)
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(get_num_threads=lambda: 6, set_num_threads=set_threads),
    )
    monkeypatch.setitem(
        sys.modules,
        "audioseal.libs.moshi.utils.compile",
        SimpleNamespace(no_compile=contextlib.nullcontext),
    )
    with pytest.raises(ValueError, match="positive integer"):
        with audio_watermark._audioseal_inference_context("cpu"):
            pytest.fail("Invalid configuration must not run inference")
    set_threads.assert_not_called()


@pytest.mark.parametrize(
    ("requested", "available", "expected"),
    (
        ("auto", True, "cuda"),
        ("auto", False, "cpu"),
        ("cpu", True, "cpu"),
        ("CPU", False, "cpu"),
    ),
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
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
    )

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
    with pytest.raises(
        audio_watermark.AudioSealCompatibilityError, match="required 0.2 message API"
    ):
        audio_watermark._watermark_with_message(
            IncompatibleGenerator(), object(), message
        )

    assert len(calls) == 1
    assert calls[0][1]["message"] is message


def test_auto_device_retries_cpu_after_cuda_runtime_failure(monkeypatch, caplog):
    monkeypatch.delenv("AUTOAUDIO_WATERMARK_DEVICE", raising=False)
    caplog.set_level(logging.WARNING)

    with (
        patch(
            "provenance.audio_watermark.resolve_audioseal_device", return_value="cuda"
        ),
        patch(
            "provenance.audio_watermark.watermark_audio_bytes",
            side_effect=[RuntimeError("GPU allocation failed"), b"cpu-watermarked"],
        ) as watermark,
    ):
        result, audio = audio_watermark.watermark_audio_bytes_best_effort(
            b"source", content_id="segment", device="auto"
        )

    assert result.applied is True
    assert result.verified is True
    assert result.detail == "verified; requested=auto; device=cpu; fallback_from=cuda"
    assert audio == b"cpu-watermarked"
    assert [entry.kwargs["device"] for entry in watermark.call_args_list] == [
        "cuda",
        "cpu",
    ]
    assert "retrying on CPU" in caplog.text


def test_explicit_cuda_failure_does_not_fall_back(monkeypatch):
    monkeypatch.delenv("AUTOAUDIO_WATERMARK_DEVICE", raising=False)

    with (
        patch(
            "provenance.audio_watermark.resolve_audioseal_device", return_value="cuda"
        ),
        patch(
            "provenance.audio_watermark.watermark_audio_bytes",
            side_effect=RuntimeError("GPU failure"),
        ) as watermark,
    ):
        result, audio = audio_watermark.watermark_audio_bytes_best_effort(
            b"source", content_id="segment", device="cuda"
        )

    assert result.applied is False
    assert audio == b"source"
    assert watermark.call_count == 1


def test_environment_device_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("AUTOAUDIO_WATERMARK_DEVICE", "cpu")

    with (
        patch(
            "provenance.audio_watermark.resolve_audioseal_device", return_value="cpu"
        ) as resolve,
        patch(
            "provenance.audio_watermark.watermark_audio_bytes", return_value=b"marked"
        ) as watermark,
    ):
        result, audio = audio_watermark.watermark_audio_bytes_best_effort(
            b"source", content_id="segment", device="auto"
        )

    assert result.applied is True
    assert audio == b"marked"
    assert resolve.call_args == call("cpu")
    assert watermark.call_args.kwargs["device"] == "cpu"


def test_message_api_incompatibility_does_not_trigger_device_fallback(monkeypatch):
    monkeypatch.delenv("AUTOAUDIO_WATERMARK_DEVICE", raising=False)

    with (
        patch(
            "provenance.audio_watermark.resolve_audioseal_device", return_value="cuda"
        ),
        patch(
            "provenance.audio_watermark.watermark_audio_bytes",
            side_effect=audio_watermark.AudioSealCompatibilityError(
                "message API missing"
            ),
        ) as watermark,
    ):
        result, audio = audio_watermark.watermark_audio_bytes_best_effort(
            b"source", content_id="segment", device="auto"
        )

    assert result.applied is False
    assert audio == b"source"
    assert watermark.call_count == 1


def test_auto_gpu_failure_is_remembered_only_for_the_current_job(monkeypatch):
    monkeypatch.delenv("AUTOAUDIO_WATERMARK_DEVICE", raising=False)

    def mark(_audio, **kwargs):
        if kwargs["device"] == "cuda":
            raise RuntimeError("GPU unavailable for this job")
        return b"cpu marked"

    with (
        patch(
            "provenance.audio_watermark.resolve_audioseal_device", return_value="cuda"
        ) as resolve,
        patch(
            "provenance.audio_watermark.watermark_audio_bytes",
            side_effect=mark,
        ) as watermark,
    ):
        with audio_watermark.audioseal_runtime_session():
            first, _ = audio_watermark.watermark_audio_bytes_best_effort(
                b"audio", content_id="one"
            )
            second, _ = audio_watermark.watermark_audio_bytes_best_effort(
                b"audio", content_id="two"
            )
        with audio_watermark.audioseal_runtime_session():
            third, _ = audio_watermark.watermark_audio_bytes_best_effort(
                b"audio", content_id="three"
            )
    assert all(result.verified for result in (first, second, third))
    assert [call.kwargs["device"] for call in watermark.call_args_list] == [
        "cuda",
        "cpu",
        "cpu",
        "cuda",
        "cpu",
    ]
    assert resolve.call_count == 2
    assert "fallback_from=cuda" in second.detail


@pytest.mark.parametrize(
    ("device", "opt_in", "expected"),
    [("cpu", "0", False), ("cpu", "1", True), ("cuda", "0", True)],
)
def test_detector_native_cpu_override_restores_backend_on_failure(
    monkeypatch, device, opt_in, expected
):
    state = SimpleNamespace(enabled=True)

    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(backends=SimpleNamespace(mkldnn=state))
    )
    monkeypatch.setenv("AUTOAUDIO_WATERMARK_CPU_MKLDNN", opt_in)
    with pytest.raises(RuntimeError, match="detector failed"):
        with audio_watermark._audioseal_detector_context(device):
            assert state.enabled is expected
            raise RuntimeError("detector failed")
    assert state.enabled is True
