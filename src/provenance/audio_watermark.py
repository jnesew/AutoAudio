from __future__ import annotations

import hashlib
import hmac
import io
import logging
import math
import os
import subprocess
import warnings
import wave
from dataclasses import dataclass
from functools import lru_cache


WATERMARK_SAMPLE_RATE = 24_000
WATERMARK_CHANNELS = 1


_AUDIOSEAL_JIT_WARNING = r"`torch\.jit\.script` is not supported in Python 3\.14\+"
_AUDIOSEAL_WEIGHT_NORM_WARNING = r"`torch\.nn\.utils\.weight_norm` is deprecated"


class AudioSealCompatibilityError(RuntimeError):
    """Raised when the installed AudioSeal API cannot embed the required message."""


def _derive_16bit_message(secret_key: str, content_id: str, source_sha256: str):
    import numpy as np
    import torch

    mac = hmac.new(secret_key.encode("utf-8"), f"{content_id}|{source_sha256}".encode("utf-8"), hashlib.sha256).digest()
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
        raise RuntimeError("AudioSeal device 'cuda' was requested, but PyTorch reports no CUDA/ROCm device.")
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


@dataclass(frozen=True)
class WatermarkResult:
    applied: bool
    verified: bool
    method: str
    detail: str


def _decode_audio(audio_data: bytes):
    """Decode canonical audio directly; let FFmpeg normalize other provider formats."""
    import numpy as np
    import soundfile as sf

    try:
        with sf.SoundFile(io.BytesIO(audio_data)) as source:
            if source.samplerate == WATERMARK_SAMPLE_RATE and source.channels == WATERMARK_CHANNELS:
                samples = source.read(dtype="float32")
                if samples.size == 0 or not np.isfinite(samples).all():
                    raise ValueError("AudioSeal input must contain finite audio samples.")
                return samples
    except sf.LibsndfileError:
        # Containers/codecs unsupported by libsndfile still work through FFmpeg.
        pass

    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", "pipe:0", "-map", "0:a:0",
         "-vn", "-ac", str(WATERMARK_CHANNELS), "-ar", str(WATERMARK_SAMPLE_RATE),
         "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"],
        input=audio_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    # Copy the read-only bytes buffer before sharing it with PyTorch.
    samples = np.frombuffer(decoded.stdout, dtype="<f4").astype(np.float32, copy=True)
    if samples.size == 0 or not np.isfinite(samples).all():
        raise ValueError("AudioSeal input must contain finite audio samples.")
    return samples


def _verify_watermark(detector, samples, message, threshold: float) -> None:
    import torch

    probability, detected = detector.detect_watermark(samples)
    confidence = _as_float(probability)
    if not math.isfinite(confidence) or confidence < threshold:
        raise RuntimeError("AudioSeal verification failed: detector confidence below threshold or non-finite.")
    if not torch.is_tensor(detected) or detected.shape != message.shape:
        raise AudioSealCompatibilityError("AudioSeal verification returned an invalid message shape/type.")
    expected = message.to(detected.device)
    # The 0.2 high-level API returns binary bits, not unthresholded logits.
    if not torch.equal(detected, expected):
        raise RuntimeError("AudioSeal verification failed: embedded message did not round-trip.")


def watermark_audio_bytes(
    audio_data: bytes,
    *,
    content_id: str,
    secret_key: str,
    device: str = "cpu",
    verify: bool = True,
    verify_threshold: float = 0.5,
    output_format: str = "wav",
) -> bytes:
    """Embed and verify 24 kHz mono PCM16; return WAV or headerless s16le.

    AudioSeal 0.2 supports 24 kHz speech without internal resampling. Verification
    uses the exact PCM16 samples exported, including clipping and quantization,
    rather than the unquantized generator output. No encode/decode round trip is
    needed to obtain those samples.
    """
    import torch

    if output_format not in {"wav", "s16le"}:
        raise ValueError("Watermark output format must be wav or s16le.")
    if not math.isfinite(verify_threshold) or not 0 <= verify_threshold <= 1:
        raise ValueError("AudioSeal verification threshold must be between 0 and 1.")
    device = resolve_audioseal_device(device)
    samples = _decode_audio(audio_data)
    source_sha256 = hashlib.sha256(audio_data).hexdigest()
    generator, detector = _load_audioseal_models(device)

    with torch.inference_mode():
        message = _derive_16bit_message(secret_key, content_id, source_sha256).to(device)
        wav = torch.from_numpy(samples).unsqueeze(0).unsqueeze(0).to(device)
        watermark = _watermark_with_message(generator, wav, message)
        watermarked = wav + watermark
        if not torch.isfinite(watermarked).all().item():
            raise RuntimeError("AudioSeal generated non-finite audio samples.")
        # Match libsndfile's PCM16 conversion, explicitly handling +1.0 and -1.0.
        pcm = torch.floor(watermarked * 32768).clamp(-32768, 32767).to(torch.int16)
        del wav, watermark, watermarked
        if verify:
            _verify_watermark(detector, pcm.float() / 32768, message, verify_threshold)
        pcm_bytes = pcm.squeeze(0).squeeze(0).cpu().numpy().astype("<i2", copy=False).tobytes()

    if output_format == "s16le":
        return pcm_bytes
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(WATERMARK_CHANNELS)
        stream.setsampwidth(2)
        stream.setframerate(WATERMARK_SAMPLE_RATE)
        stream.writeframes(pcm_bytes)
    return output.getvalue()


def watermark_audio_bytes_best_effort(
    audio_data: bytes,
    *,
    content_id: str,
    device: str = "auto",
    logger: logging.Logger | None = None,
    output_format: str = "wav",
) -> tuple[WatermarkResult, bytes]:
    """Return watermark evidence and bytes; strict callers reject a failed result."""
    log = logger or logging.getLogger("autoaudio.run")
    secret_key = os.environ.get("AUTOAUDIO_WATERMARK_SECRET", "default_public_autoaudio_key_123").strip()
    requested_device = os.environ.get("AUTOAUDIO_WATERMARK_DEVICE", device).strip().lower()
    resolved_device: str | None = None
    try:
        resolved_device = resolve_audioseal_device(requested_device)
        log.info("AudioSeal device requested=%s resolved=%s", requested_device, resolved_device)
        out_bytes = watermark_audio_bytes(
            audio_data,
            content_id=content_id,
            secret_key=secret_key,
            device=resolved_device,
            verify=True,
            verify_threshold=0.5,
            output_format=output_format,
        )
        detail = f"verified; requested={requested_device}; device={resolved_device}"
        return WatermarkResult(applied=True, verified=True, method="audioseal", detail=detail), out_bytes
    except AudioSealCompatibilityError as exc:
        log.warning("Audio watermarking skipped for %s (%s)", content_id, exc)
        return WatermarkResult(applied=False, verified=False, method="audioseal", detail=str(exc)), audio_data
    except Exception as exc:
        if requested_device == "auto" and resolved_device == "cuda":
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
                out_bytes = watermark_audio_bytes(
                    audio_data,
                    content_id=content_id,
                    secret_key=secret_key,
                    device="cpu",
                    verify=True,
                    verify_threshold=0.5,
                    output_format=output_format,
                )
                detail = "verified; requested=auto; device=cpu; fallback_from=cuda"
                return WatermarkResult(applied=True, verified=True, method="audioseal", detail=detail), out_bytes
            except Exception as cpu_exc:
                detail = f"automatic CUDA/ROCm attempt failed ({exc}); CPU retry failed ({cpu_exc})"
                log.warning("Audio watermarking skipped for %s (%s)", content_id, detail)
                return WatermarkResult(applied=False, verified=False, method="audioseal", detail=detail), audio_data

        log.warning("Audio watermarking skipped for %s (%s)", content_id, exc)
        return WatermarkResult(applied=False, verified=False, method="audioseal", detail=str(exc)), audio_data
