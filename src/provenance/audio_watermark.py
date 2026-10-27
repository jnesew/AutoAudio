from __future__ import annotations

import hashlib
import hmac
import io
import logging
import math
import os
import subprocess
import threading
import time
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache

from provenance.payloads import (
    PAYLOAD_SCHEMA, PAYLOAD_DERIVATION, attribution_id,
    MAX_PAYLOAD_BIT_ERRORS, PARTIAL_PAYLOAD_MIN_PRESENCE,
    partial_payload_accepted,
)


_AUDIOSEAL_JIT_WARNING = r"`torch\.jit\.script` is not supported in Python 3\.14\+"
_AUDIOSEAL_WEIGHT_NORM_WARNING = r"`torch\.nn\.utils\.weight_norm` is deprecated"
_AUDIOSEAL_INFERENCE_LOCK = threading.RLock()


@dataclass
class _RuntimeState:
    cpu_after_cuda_failure: bool = False


_RUNTIME_STATE: ContextVar[_RuntimeState | None] = ContextVar(
    "autoaudio_audioseal_runtime", default=None
)


@contextmanager
def audioseal_runtime_session():
    """Remember automatic device failure within one job, retry on the next job."""
    token = _RUNTIME_STATE.set(_RuntimeState())
    try:
        yield
    finally:
        _RUNTIME_STATE.reset(token)


class AudioSealCompatibilityError(RuntimeError):
    """Raised when the installed AudioSeal API cannot embed the required message."""


def _derive_16bit_message(secret_key: str, content_id: str, source_sha256: str):
    import numpy as np
    import torch

    mac = hmac.new(
        secret_key.encode("utf-8"),
        f"{content_id}|{source_sha256}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    bits = np.unpackbits(np.frombuffer(mac[:2], dtype=np.uint8)).astype(np.int64)
    return torch.from_numpy(bits).unsqueeze(0)


@lru_cache(maxsize=2)
def _load_audioseal_models(device: str):
    # AudioSeal 0.2.0 imports TorchScript-decorated streaming helpers and uses
    # the legacy weight_norm API while translating its published checkpoints.
    # Both paths still work for eager inference, but PyTorch emits warnings on
    # Python 3.14. Keep the suppression limited to AudioSeal import/model load
    # so unrelated FutureWarnings remain visible.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=_AUDIOSEAL_JIT_WARNING,
            category=FutureWarning,
            module=r"torch\.jit\._script",
        )
        warnings.filterwarnings(
            "ignore",
            message=_AUDIOSEAL_WEIGHT_NORM_WARNING,
            category=FutureWarning,
            module=r"torch\.nn\.utils\.weight_norm",
        )

        from audioseal import AudioSeal

        generator = AudioSeal.load_generator("audioseal_wm_16bits")
        detector = AudioSeal.load_detector("audioseal_detector_16bits")
    for model in (generator, detector):
        model.to(device)
        model.eval()
    return generator, detector


def resolve_audioseal_device(requested: str) -> str:
    """Resolve an AudioSeal device selection without silently overriding explicit choices."""
    import torch

    normalized = requested.strip().lower()
    if normalized not in {"auto", "cpu", "cuda"}:
        raise ValueError("AudioSeal device must be one of: auto, cpu, cuda.")
    if normalized == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if normalized == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "AudioSeal device 'cuda' was requested, but PyTorch reports no CUDA/ROCm device."
        )
    return normalized


def _watermark_with_message(generator, wav, message):
    """Call the pinned AudioSeal 0.2 API while always supplying the identifying payload."""
    try:
        return generator.get_watermark(wav, message=message)
    except TypeError as error:
        raise AudioSealCompatibilityError(
            "Installed AudioSeal generator does not support the required 0.2 message API."
        ) from error


def _as_float(x) -> float:
    import torch

    if isinstance(x, (float, int)):
        return float(x)
    if torch.is_tensor(x):
        return float(x.detach().float().mean().item())
    return float(x)


@contextmanager
def _audioseal_inference_context(device: str):
    # The pinned AudioSeal 0.2 SEANet forwards compile lazily by default. Book
    # segments have different lengths, so compilation/recompilation can cost
    # much more than inference. Use its own eager-mode context without changing
    # process-wide compiler configuration or environment variables. AudioSeal's context
    # uses a module-global flag, so keep concurrent AutoAudio calls serialized.
    from audioseal.libs.moshi.utils.compile import no_compile
    import torch

    with _AUDIOSEAL_INFERENCE_LOCK:
        previous_threads = None
        if device == "cpu":
            previous_threads = torch.get_num_threads()
            configured = os.environ.get("AUTOAUDIO_WATERMARK_CPU_THREADS", "").strip()
            try:
                threads = int(configured) if configured else min(previous_threads, 4)
            except ValueError as exc:
                raise ValueError(
                    "AUTOAUDIO_WATERMARK_CPU_THREADS must be a positive integer."
                ) from exc
            if threads < 1:
                raise ValueError(
                    "AUTOAUDIO_WATERMARK_CPU_THREADS must be a positive integer."
                )
            # Large intra-op pools can make first-use CPU convolutions much
            # slower for new speech lengths. Keep a small default pool, respect
            # an already smaller pool, and allow measured machine-specific tuning.
            if threads != previous_threads:
                torch.set_num_threads(threads)
        try:
            with no_compile():
                yield
        finally:
            if (
                previous_threads is not None
                and torch.get_num_threads() != previous_threads
            ):
                torch.set_num_threads(previous_threads)


@dataclass(frozen=True)
class WatermarkResult:
    applied: bool
    verified: bool
    method: str
    detail: str
    payload: dict | None = None


@contextmanager
def _audioseal_detector_context(device: str):
    """Avoid expensive cold oneDNN transposed-convolution kernels on CPU.

    oneDNN transposed-convolution startup can be expensive for new audio lengths.
    Limit native kernels to detection under the existing inference lock and
    restore the caller's backend state. An opt-in retains oneDNN when benchmarking
    favors it. Serialized-audio/message checks remain unchanged.
    """
    if (
        device == "cpu"
        and os.environ.get("AUTOAUDIO_WATERMARK_CPU_MKLDNN", "0").strip() != "1"
    ):
        import torch

        backend = torch.backends.mkldnn
        previous = backend.enabled
        backend.enabled = False
        try:
            yield
        finally:
            backend.enabled = previous
    else:
        yield


def watermark_audio_bytes(
    audio_data: bytes,
    *,
    content_id: str,
    secret_key: str,
    device: str = "cpu",
    verify: bool = True,
    verify_threshold: float = 0.5,
    evidence_out: dict | None = None,
    max_bit_errors: int = 0,
) -> bytes:
    """Embed an AudioSeal watermark into an audio byte stream (24 kHz mono processing)."""
    import soundfile as sf
    import torch

    if type(max_bit_errors) is not int or not 0 <= max_bit_errors <= MAX_PAYLOAD_BIT_ERRORS:
        raise ValueError("AudioSeal payload tolerance must be between zero and two bits.")

    source_sha256 = hashlib.sha256(audio_data).hexdigest()
    msg = _derive_16bit_message(secret_key, content_id, source_sha256).to(device)
    generator, detector = _load_audioseal_models(device)

    decode_proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            "pipe:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-f",
            "wav",
            "pipe:1",
        ],
        input=audio_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    decoded_wav_bytes = decode_proc.stdout

    # ffmpeg already normalized the stream. SoundFile avoids librosa's lazy
    # import/JIT startup cost and reads the same serialized PCM for verification.
    wav_np, sr = sf.read(io.BytesIO(decoded_wav_bytes), dtype="float32")
    wav = torch.from_numpy(wav_np).float().unsqueeze(0).unsqueeze(0).to(device)

    with _audioseal_inference_context(device), torch.no_grad():
        watermark = _watermark_with_message(generator, wav, msg)
        watermarked = torch.clamp(wav + watermark, -1.0, 1.0)

        out_io = io.BytesIO()
        sf.write(
            out_io,
            watermarked.squeeze(0).squeeze(0).detach().cpu().numpy(),
            sr,
            format="WAV",
        )
        watermarked_wav_bytes = out_io.getvalue()

        if verify:
            verify_np, _ = sf.read(io.BytesIO(watermarked_wav_bytes), dtype="float32")
            verify_tensor = (
                torch.from_numpy(verify_np).float().unsqueeze(0).unsqueeze(0).to(device)
            )
            with _audioseal_detector_context(device):
                prob, detected_msg = detector.detect_watermark(verify_tensor)

            confidence = _as_float(prob)
            if not math.isfinite(confidence) or confidence < verify_threshold:
                raise RuntimeError(
                    "AudioSeal verification failed: detector confidence below threshold or non-finite."
                )
            if not torch.is_tensor(detected_msg) or detected_msg.shape != msg.shape:
                raise RuntimeError(
                    "AudioSeal verification failed: detector returned an invalid message shape."
                )
            if not torch.isfinite(detected_msg).all():
                raise RuntimeError("AudioSeal verification failed: non-finite message probabilities.")
            expected = msg.to(detected_msg.device)
            decoded = (detected_msg > 0.5).to(expected.dtype)
            exact = torch.equal(decoded, expected)
            bit_errors = 0 if exact else 16
            if evidence_out is not None or max_bit_errors:
                expected_bits = "".join(str(int(bit)) for bit in expected.detach().cpu().tolist()[0])
                decoded_bits = "".join(str(int(bit)) for bit in decoded.detach().cpu().tolist()[0])
                bit_errors = sum(a != b for a, b in zip(expected_bits, decoded_bits))
            if evidence_out is not None:
                evidence_out.update({
                    "schema": PAYLOAD_SCHEMA,
                    "derivation": PAYLOAD_DERIVATION,
                    "generator": "audioseal_wm_16bits",
                    "detector": "audioseal_detector_16bits",
                    "message_bits": 16,
                    "expected_bits": expected_bits,
                    "source_sha256": source_sha256,
                    "content_id": content_id,
                    "attribution_id": attribution_id(content_id, source_sha256),
                    "sample_rate": sr,
                    "start_sample": 0,
                    "sample_count": int(wav.shape[-1]),
                    "embedding_strength": 1.0,
                    "generation_verification": {
                        "presence_score": confidence,
                        "frame_detection_threshold": 0.5,
                        "presence_threshold": verify_threshold,
                        "decoded_bits": decoded_bits,
                        "bit_errors": bit_errors,
                        "payload_status": "exact" if exact else "partial",
                        "max_bit_errors": max_bit_errors,
                        "partial_presence_threshold": PARTIAL_PAYLOAD_MIN_PRESENCE,
                    },
                })
            if not exact and not partial_payload_accepted(bit_errors, confidence, max_bit_errors):
                raise RuntimeError(
                    "AudioSeal verification failed: embedded message did not round-trip."
                )

    return watermarked_wav_bytes


def watermark_audio_bytes_best_effort(
    audio_data: bytes,
    *,
    content_id: str,
    device: str = "auto",
    logger: logging.Logger | None = None,
) -> tuple[WatermarkResult, bytes]:
    """Return watermark evidence and bytes; strict callers reject a failed result."""
    log = logger or logging.getLogger("autoaudio.run")
    secret_key = os.environ.get(
        "AUTOAUDIO_WATERMARK_SECRET", "default_public_autoaudio_key_123"
    ).strip()
    requested_device = (
        os.environ.get("AUTOAUDIO_WATERMARK_DEVICE", device).strip().lower()
    )
    resolved_device: str | None = None
    runtime = _RUNTIME_STATE.get()
    previous_fallback = (
        requested_device == "auto"
        and runtime is not None
        and runtime.cpu_after_cuda_failure
    )
    started = time.perf_counter()
    evidence: dict = {}
    try:
        resolved_device = (
            "cpu" if previous_fallback else resolve_audioseal_device(requested_device)
        )
        log.info(
            "AudioSeal device requested=%s resolved=%s",
            requested_device,
            resolved_device,
        )
        out_bytes = watermark_audio_bytes(
            audio_data,
            content_id=content_id,
            secret_key=secret_key,
            device=resolved_device,
            verify=True,
            verify_threshold=0.5,
            evidence_out=evidence,
            max_bit_errors=MAX_PAYLOAD_BIT_ERRORS,
        )
        detail = (
            "verified; requested=auto; device=cpu; fallback_from=cuda"
            if previous_fallback
            else f"verified; requested={requested_device}; device={resolved_device}"
        )
        quality = evidence.get("generation_verification", {})
        if quality.get("bit_errors", 0):
            detail += f"; payload=partial; bit_errors={quality['bit_errors']}/16"
            log.warning("AudioSeal payload partially recovered for %s: %d/16 bit errors (maximum %d).",
                        content_id, quality["bit_errors"], MAX_PAYLOAD_BIT_ERRORS)
        log.info(
            "AudioSeal verified %s on %s in %.3fs",
            content_id,
            resolved_device,
            time.perf_counter() - started,
        )
        return WatermarkResult(
            applied=True, verified=True, method="audioseal", detail=detail, payload=evidence or None
        ), out_bytes
    except AudioSealCompatibilityError as exc:
        log.warning("Audio watermarking skipped for %s (%s)", content_id, exc)
        return WatermarkResult(
            applied=False, verified=False, method="audioseal", detail=str(exc)
        ), audio_data
    except Exception as exc:
        if requested_device == "auto" and resolved_device == "cuda":
            if runtime is not None:
                runtime.cpu_after_cuda_failure = True
            log.warning(
                "AudioSeal automatic CUDA/ROCm execution failed for %s (%s); retrying on CPU",
                content_id,
                exc,
            )
            _load_audioseal_models.cache_clear()
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
            try:
                evidence.clear()
                out_bytes = watermark_audio_bytes(
                    audio_data,
                    content_id=content_id,
                    secret_key=secret_key,
                    device="cpu",
                    verify=True,
                    verify_threshold=0.5,
                    evidence_out=evidence,
                    max_bit_errors=MAX_PAYLOAD_BIT_ERRORS,
                )
                detail = "verified; requested=auto; device=cpu; fallback_from=cuda"
                quality = evidence.get("generation_verification", {})
                if quality.get("bit_errors", 0):
                    detail += f"; payload=partial; bit_errors={quality['bit_errors']}/16"
                    log.warning("AudioSeal payload partially recovered for %s: %d/16 bit errors (maximum %d).",
                                content_id, quality["bit_errors"], MAX_PAYLOAD_BIT_ERRORS)
                log.info(
                    "AudioSeal verified %s on CPU after fallback in %.3fs",
                    content_id,
                    time.perf_counter() - started,
                )
                return WatermarkResult(
                    applied=True, verified=True, method="audioseal", detail=detail, payload=evidence or None
                ), out_bytes
            except Exception as cpu_exc:
                detail = f"automatic CUDA/ROCm attempt failed ({exc}); CPU retry failed ({cpu_exc})"
                log.warning(
                    "Audio watermarking skipped for %s (%s)", content_id, detail
                )
                return WatermarkResult(
                    applied=False, verified=False, method="audioseal", detail=detail
                ), audio_data

        log.warning("Audio watermarking skipped for %s (%s)", content_id, exc)
        return WatermarkResult(
            applied=False, verified=False, method="audioseal", detail=str(exc)
        ), audio_data
