"""Explicit waveform checks against public, time-aligned AudioSeal payloads."""

from __future__ import annotations

import math
from pathlib import Path
import queue
import subprocess
import threading

from provenance.payloads import (
    PAYLOAD_SAMPLE_RATE, validate_payloads, MAX_PAYLOAD_BIT_ERRORS, PARTIAL_PAYLOAD_MIN_PRESENCE,
    partial_payload_accepted,
)


MAX_SIGNAL_SPAN_SECONDS = 600


def decode_signal_spans(artifact: Path, records):
    """Decode once in timeline order, avoiding codec seek/priming offsets."""
    import numpy as np

    if any(record["sample_count"] > MAX_SIGNAL_SPAN_SECONDS * PAYLOAD_SAMPLE_RATE for record in records):
        raise ValueError("Signal verification span exceeds the 10-minute limit.")
    command = [
        "ffmpeg", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-i", str(artifact), "-vn", "-ac", "1", "-ar", str(PAYLOAD_SAMPLE_RATE),
        "-acodec", "pcm_f32le", "-f", "f32le", "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    cursor = 0
    chunks = queue.Queue(maxsize=2)
    stop = threading.Event()

    def feed():
        # A bounded reader works with subprocess pipes on Windows as well as POSIX.
        while not stop.is_set():
            try:
                chunk = process.stdout.read1(64 * 1024)
            except OSError as exc:
                chunk = exc
            while not stop.is_set():
                try:
                    chunks.put(chunk, timeout=.1)
                    break
                except queue.Full:
                    pass
            if not chunk or isinstance(chunk, Exception):
                break

    reader = threading.Thread(target=feed, daemon=True)
    reader.start()
    pending = b""

    def read_chunk(count):
        nonlocal pending
        if not pending:
            try:
                pending = chunks.get(timeout=30)
            except queue.Empty as exc:
                raise ValueError("Audio decoder produced no output for 30 seconds.") from exc
            if isinstance(pending, Exception):
                raise ValueError("Audio decoder pipe failed.") from pending
        chunk, pending = pending[:count], pending[count:]
        return chunk

    try:
        for record in records:
            skip = (record["start_sample"] - cursor) * 4
            while skip:
                chunk = read_chunk(min(skip, 64 * 1024))
                if not chunk:
                    raise ValueError("Audio ends before the recorded watermark span.")
                skip -= len(chunk)
            count = record["sample_count"]
            data = bytearray()
            while len(data) < count * 4:
                chunk = read_chunk(min(count * 4 - len(data), 64 * 1024))
                if not chunk:
                    raise ValueError("Audio is truncated within a recorded watermark span.")
                data.extend(chunk)
            samples = np.frombuffer(data, dtype="<f4").copy()
            if not np.isfinite(samples).all():
                raise ValueError("Decoded waveform contains non-finite samples.")
            cursor = record["start_sample"] + count
            yield record, samples
    finally:
        stop.set()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=1)
        process.stdout.close()


def verify_signal(artifact: Path, records, *, device="cpu", complete=False) -> dict:
    from provenance.audio_watermark import (
        _load_audioseal_models, resolve_audioseal_device,
    )

    payloads = validate_payloads(records)
    if not payloads:
        return {"detection": "unavailable", "payload": "unavailable", "coverage": "unavailable", "spans": []}
    resolved = resolve_audioseal_device(device)
    _, detector = _load_audioseal_models(resolved)
    decoded_spans = decode_signal_spans(artifact, payloads)
    try:
        return _verify_decoded_spans(decoded_spans, detector, resolved, complete)
    finally:
        decoded_spans.close()


def _verify_decoded_spans(decoded_spans, detector, resolved, complete):
    import torch
    from provenance.audio_watermark import _audioseal_inference_context, _audioseal_detector_context

    spans = []
    for record, audio in decoded_spans:
        wave = torch.from_numpy(audio).unsqueeze(0).unsqueeze(0).to(resolved)
        with _audioseal_inference_context(resolved), _audioseal_detector_context(resolved), torch.no_grad():
            probability, message = detector.detect_watermark(wave)
        score = float(probability.detach().float().mean().cpu())
        if not torch.is_tensor(message) or tuple(message.shape) != (1, 16):
            raise ValueError("AudioSeal detector returned an invalid message shape.")
        if not torch.isfinite(message).all():
            raise ValueError("AudioSeal detector returned non-finite message probabilities.")
        decoded = "".join(str(int(bit)) for bit in (message > .5).int().cpu().tolist()[0])
        errors = sum(a != b for a, b in zip(decoded, record["expected_bits"]))
        detected = math.isfinite(score) and score >= .5
        partial = detected and partial_payload_accepted(errors, score)
        spans.append({
            "attribution_id": record["attribution_id"], "content_id": record["content_id"],
            "start_sample": record["start_sample"], "sample_count": record["sample_count"],
            "presence_score": score if math.isfinite(score) else None,
            "expected_bits": record["expected_bits"], "decoded_bits": decoded,
            "bit_errors": errors, "detection": "detected" if detected else "not_detected",
            "payload": "exact" if detected and errors == 0 else "partial" if partial else "mismatch" if detected else "not_established",
        })
    return {
        "detection": "detected" if all(item["detection"] == "detected" for item in spans) else "not_detected",
        "payload": "exact" if all(item["payload"] == "exact" for item in spans) else
                   "partial" if all(item["payload"] in {"exact", "partial"} for item in spans) else "mismatch",
        "policy": {"max_bit_errors": MAX_PAYLOAD_BIT_ERRORS, "partial_min_presence": PARTIAL_PAYLOAD_MIN_PRESENCE},
        "coverage": "complete" if complete else "partial",
        "device": resolved, "spans": spans,
    }
