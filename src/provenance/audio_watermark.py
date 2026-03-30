from __future__ import annotations

import hashlib
import hmac
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _decode_to_wav_24k_mono(input_path: Path, wav_path: Path) -> None:
    _run(["ffmpeg", "-y", "-i", str(input_path), "-vn", "-ac", "1", "-ar", "24000", str(wav_path)])


def _encode_wav_to_target(wav_path: Path, output_path: Path) -> None:
    suffix = output_path.suffix.lower()
    if suffix == ".wav":
        output_path.write_bytes(wav_path.read_bytes())
        return
    if suffix in {".mp4", ".m4b"}:
        _run(["ffmpeg", "-y", "-i", str(wav_path), "-c:a", "aac", str(output_path)])
        return
    _run(["ffmpeg", "-y", "-i", str(wav_path), str(output_path)])


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


def watermark_audio_output(
    input_path: str | os.PathLike,
    *,
    content_id: str,
    secret_key: str,
    device: str = "cpu",
    verify: bool = True,
    verify_threshold: float = 0.5,
) -> Path:
    """Embed an AudioSeal watermark into an audio artifact (24 kHz mono processing)."""
    import librosa
    import soundfile as sf
    import torch

    source_path = Path(input_path)
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    suffix = source_path.suffix.lower()
    if suffix not in {".flac", ".mp3", ".wav", ".opus", ".mp4", ".m4b"}:
        raise ValueError(f"Unsupported input format for watermarking: {suffix}")

    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    msg = _derive_16bit_message(secret_key, content_id, source_sha256)
    generator, detector = _load_audioseal_models(device)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        decoded_wav = td_path / "decoded_24k.wav"
        watermarked_wav = td_path / "watermarked_24k.wav"
        verify_wav = td_path / "verify_24k.wav"

        _decode_to_wav_24k_mono(source_path, decoded_wav)
        wav_np, sr = librosa.load(str(decoded_wav), sr=24000, mono=True)
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

        sf.write(str(watermarked_wav), watermarked.squeeze(0).squeeze(0).detach().cpu().numpy(), sr)
        _encode_wav_to_target(watermarked_wav, source_path)

        if verify:
            _decode_to_wav_24k_mono(source_path, verify_wav)
            verify_np, verify_sr = librosa.load(str(verify_wav), sr=24000, mono=True)
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

    return source_path


def watermark_audio_output_best_effort(
    artifact_path: str | os.PathLike,
    *,
    content_id: str,
    logger: logging.Logger | None = None,
) -> WatermarkResult:
    """Best-effort wrapper so pipeline output remains available if watermarking deps/tools are missing."""
    log = logger or logging.getLogger("autoaudio.run")
    secret_key = os.environ.get("AUTOAUDIO_WATERMARK_SECRET", "").strip()
    if not secret_key:
        log.info("Audio watermarking skipped for %s (AUTOAUDIO_WATERMARK_SECRET not set)", artifact_path)
        return WatermarkResult(applied=False, verified=False, method="audioseal", detail="secret_missing")

    device = os.environ.get("AUTOAUDIO_WATERMARK_DEVICE", "cpu")
    try:
        watermark_audio_output(
            artifact_path,
            content_id=content_id,
            secret_key=secret_key,
            device=device,
            verify=True,
            verify_threshold=0.5,
        )
        return WatermarkResult(applied=True, verified=True, method="audioseal", detail="ok")
    except Exception as exc:
        log.warning("Audio watermarking skipped for %s (%s)", artifact_path, exc)
        return WatermarkResult(applied=False, verified=False, method="audioseal", detail=str(exc))
