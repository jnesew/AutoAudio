"""Public AudioSeal attribution records; no secrets or model loading."""

from __future__ import annotations

import copy
import hashlib
import math
import re


PAYLOAD_SCHEMA = "autoaudio.audioseal-payload.v1"
PAYLOAD_DERIVATION = "hmac-sha256-trunc16-v1"
PAYLOAD_SAMPLE_RATE = 24_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BITS = re.compile(r"^[01]{16}$")
MAX_PAYLOAD_RECORDS = 100_000
MAX_PAYLOAD_BIT_ERRORS = 2
PARTIAL_PAYLOAD_MIN_PRESENCE = 0.9


def partial_payload_accepted(bit_errors: int, presence: float, maximum=MAX_PAYLOAD_BIT_ERRORS) -> bool:
    # Permit float32 representation error at the threshold, not a wider margin.
    enough_presence = presence >= PARTIAL_PAYLOAD_MIN_PRESENCE or math.isclose(
        presence, PARTIAL_PAYLOAD_MIN_PRESENCE, rel_tol=0, abs_tol=1e-7,
    )
    return math.isfinite(presence) and bit_errors <= maximum and enough_presence


def attribution_id(content_id: str, source_sha256: str) -> str:
    return hashlib.sha256(
        f"{PAYLOAD_SCHEMA}|{content_id}|{source_sha256}".encode("utf-8")
    ).hexdigest()


def validate_payloads(value) -> tuple[dict, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_PAYLOAD_RECORDS:
        raise ValueError("Watermark payload records must be a bounded list.")
    records = []
    end = 0
    for item in value:
        if not isinstance(item, dict) or item.get("schema") != PAYLOAD_SCHEMA:
            raise ValueError("Unsupported watermark payload record.")
        if item.get("derivation") != PAYLOAD_DERIVATION or type(item.get("message_bits")) is not int or item["message_bits"] != 16:
            raise ValueError("Unsupported watermark payload derivation or bit count.")
        if not isinstance(item.get("expected_bits"), str) or not _BITS.fullmatch(item["expected_bits"]):
            raise ValueError("Watermark expected_bits must contain exactly 16 binary digits.")
        source = item.get("source_sha256")
        content = item.get("content_id")
        if not isinstance(source, str) or not _SHA256.fullmatch(source) or not isinstance(content, str) or not content:
            raise ValueError("Watermark payload requires a source SHA-256 and content ID.")
        if item.get("attribution_id") != attribution_id(content, source):
            raise ValueError("Watermark attribution ID does not match its source identity.")
        if type(item.get("sample_rate")) is not int or item["sample_rate"] != PAYLOAD_SAMPLE_RATE:
            raise ValueError("Unsupported watermark payload timeline sample rate.")
        start, count = item.get("start_sample"), item.get("sample_count")
        if type(start) is not int or type(count) is not int or start < end or count <= 0:
            raise ValueError("Watermark payload spans must be positive, ordered, and non-overlapping.")
        strength = item.get("embedding_strength")
        if type(strength) not in (int, float) or not math.isfinite(strength) or strength <= 0:
            raise ValueError("Watermark embedding strength must be finite and positive.")
        end = start + count
        records.append(copy.deepcopy(item))
    return tuple(records)


def offset_payloads(records, offset: int) -> tuple[dict, ...]:
    copied = validate_payloads(records)
    for item in copied:
        item["start_sample"] += offset
    return copied
