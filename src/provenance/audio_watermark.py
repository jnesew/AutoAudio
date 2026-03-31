from __future__ import annotations

import hashlib
import hmac
import io
import logging
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache


def _derive_16bit_message(secret_key: str, content_id: str, source_sha256: str):
    import numpy as np
    import torch

    mac = hmac.new(secret_key.encode("utf-8"), f"{content_id}|{source_sha256}".encode("utf-8"), hashlib.sha256).digest()
    bits = np.unpackbits(np.frombuffer(mac[:2], dtype=np.uint8)).astype(np.int64)
    return torch.from_numpy(bits).unsqueeze(0)


@lru_cache(maxsize=2)
def _load_audioseal_models(device: str):
    from audioseal import AudioSeal

    generator = AudioSeal.load_generator("audioseal_wm_16bits")
    detector = AudioSeal.load_detector("audioseal_detector_16bits")
    for model in (generator, detector):
        try:
            model.to(device)
        except Exception:
            pass
        model.eval()
    return generator, detector


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


def watermark_audio_bytes(
    audio_data: bytes,
    *,
    content_id: str,
    secret_key: str,
    device: str = "cpu",
    verify: bool = True,
    verify_threshold: float = 0.5,
) -> bytes:
    """Embed an AudioSeal watermark into an audio byte stream (24 kHz mono processing)."""
    import librosa
    import soundfile as sf
    import torch

    source_sha256 = hashlib.sha256(audio_data).hexdigest()
    msg = _derive_16bit_message(secret_key, content_id, source_sha256)
    generator, detector = _load_audioseal_models(device)

    decode_proc = subprocess.run(
        ["ffmpeg", "-y", "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "24000", "-f", "wav", "pipe:1"],
        input=audio_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True
    )
    decoded_wav_bytes = decode_proc.stdout

    wav_np, sr = librosa.load(io.BytesIO(decoded_wav_bytes), sr=24000, mono=True)
    wav = torch.from_numpy(wav_np).float().unsqueeze(0).unsqueeze(0).to(device)

    with torch.no_grad():
        try:
            watermark = generator.get_watermark(wav, sr, message=msg)
        except TypeError:
            try:
                watermark = generator.get_watermark(wav, sample_rate=sr, message=msg)
            except TypeError:
                watermark = generator.get_watermark(wav, sr)
        watermarked = torch.clamp(wav + watermark, -1.0, 1.0)

    out_io = io.BytesIO()
    sf.write(out_io, watermarked.squeeze(0).squeeze(0).detach().cpu().numpy(), sr, format="WAV")
    watermarked_wav_bytes = out_io.getvalue()

    if verify:
        verify_np, verify_sr = librosa.load(io.BytesIO(watermarked_wav_bytes), sr=24000, mono=True)
        verify_tensor = torch.from_numpy(verify_np).float().unsqueeze(0).unsqueeze(0).to(device)
        with torch.no_grad():
            try:
                prob, detected_msg = detector.detect_watermark(verify_tensor, verify_sr)
            except TypeError:
                prob, detected_msg = detector.detect_watermark(verify_tensor)

        if _as_float(prob) < verify_threshold:
            raise RuntimeError("AudioSeal verification failed: detector confidence below threshold.")
        if torch.is_tensor(detected_msg):
            expected = msg.to(detected_msg.device)
            if detected_msg.shape == expected.shape and not torch.equal((detected_msg > 0.5).to(expected.dtype), expected):
                raise RuntimeError("AudioSeal verification failed: embedded message did not round-trip.")

    return watermarked_wav_bytes


def watermark_audio_bytes_best_effort(
    audio_data: bytes,
    *,
    content_id: str,
    logger: logging.Logger | None = None,
) -> tuple[WatermarkResult, bytes]:
    """Best-effort wrapper so pipeline output remains available if watermarking deps/tools are missing."""
    log = logger or logging.getLogger("autoaudio.run")
    secret_key = os.environ.get("AUTOAUDIO_WATERMARK_SECRET", "default_public_autoaudio_key_123").strip()

    device = os.environ.get("AUTOAUDIO_WATERMARK_DEVICE", "cpu")
    try:
        out_bytes = watermark_audio_bytes(
            audio_data,
            content_id=content_id,
            secret_key=secret_key,
            device=device,
            verify=True,
            verify_threshold=0.5,
        )
        return WatermarkResult(applied=True, verified=True, method="audioseal", detail="ok"), out_bytes
    except Exception as exc:
        log.warning("Audio watermarking skipped for %s (%s)", content_id, exc)
        return WatermarkResult(applied=False, verified=False, method="audioseal", detail=str(exc)), audio_data

