#!/usr/bin/env python3
"""Measure real AudioSeal marking/verification on a bounded synthetic tone."""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="cpu")
    parser.add_argument("--duration", type=float, default=5.0, help="Tone duration in seconds (1 to 120).")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs (1 to 100).")
    parser.add_argument("--input-audio", type=Path, help="Use an existing WAV/FLAC clip instead of a tone.")
    parser.add_argument("--threads", type=int, help="Explicit Torch CPU thread count for this benchmark process.")
    args = parser.parse_args()
    if not 1 <= args.duration <= 120 or not 1 <= args.runs <= 100:
        parser.error("Duration must be between 1 and 120 seconds; runs must be between 1 and 100.")
    if args.threads is not None and args.threads < 1:
        parser.error("Threads must be positive.")

    import numpy as np
    import soundfile as sf
    import torch
    from provenance.audio_watermark import resolve_audioseal_device, watermark_audio_bytes

    if args.threads is not None:
        torch.set_num_threads(args.threads)
        os.environ["AUTOAUDIO_WATERMARK_CPU_THREADS"] = str(args.threads)
    device = resolve_audioseal_device(args.device)
    sample_rate = 24_000
    if args.input_audio:
        duration = sf.info(args.input_audio).duration
        if not 1 <= duration <= 120:
            parser.error("Input clip must be between 1 and 120 seconds long.")
        source = args.input_audio.read_bytes()
    else:
        duration = args.duration
        tone = (0.08 * np.sin(np.arange(round(sample_rate * duration)) * 2 * np.pi * 220 / sample_rate)).astype(np.float32)
        output = io.BytesIO()
        sf.write(output, tone, sample_rate, format="WAV", subtype="PCM_16")
        source = output.getvalue()
    print(json.dumps({
        "python": platform.python_version(), "torch": torch.__version__,
        "requested_device": args.device, "resolved_device": device,
        "torch_default_threads": torch.get_num_threads(), "audio_seconds": duration,
        "cpu_inference_threads": int(os.environ.get("AUTOAUDIO_WATERMARK_CPU_THREADS") or min(torch.get_num_threads(), 4)),
        "source": str(args.input_audio) if args.input_audio else "synthetic tone",
        "note": "First run includes model loading, not Python imports. No TTS requests.",
    }), flush=True)
    for index in range(args.runs):
        started = time.perf_counter()
        watermark_audio_bytes(source, content_id="benchmark", secret_key="test-secret", device=device)
        elapsed = time.perf_counter() - started
        print(json.dumps({
            "run": index + 1, "elapsed_seconds": round(elapsed, 4),
            "processing_seconds_per_audio_second": round(elapsed / duration, 4),
            "verified": True,
        }), flush=True)


if __name__ == "__main__":
    main()
