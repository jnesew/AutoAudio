from __future__ import annotations

import re
import unicodedata


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    "CONIN$",
    "CONOUT$",
}
_ALLOWED_PUNCTUATION = frozenset(" -_(),.'")


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _clean_component(value: str, max_bytes: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    cleaned = "".join(
        character
        if (character.isalnum() or character in _ALLOWED_PUNCTUATION)
        and not unicodedata.category(character).startswith("C")
        else " "
        for character in normalized
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return _truncate_utf8(cleaned, max_bytes).rstrip(" .")


def safe_filename_component(
    value: str | None,
    *,
    fallback: str = "Untitled",
    max_bytes: int = 160,
) -> str:
    """Return one bounded, cross-platform filename component.

    Metadata is untrusted input: normalization happens before path separators,
    control characters, device names, and filesystem-hostile punctuation are
    handled. The result never contains a directory component.
    """
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive.")

    cleaned = _clean_component(str(value or ""), max_bytes)
    if not cleaned:
        cleaned = _clean_component(fallback, max_bytes) or _truncate_utf8("Untitled", max_bytes)

    device_stem = cleaned.split(".", 1)[0].rstrip(" ").upper()
    if device_stem in _WINDOWS_RESERVED_NAMES:
        cleaned = _truncate_utf8(f"_{cleaned}", max_bytes).rstrip(" .")

    return cleaned
