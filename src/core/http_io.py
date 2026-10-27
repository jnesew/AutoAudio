from __future__ import annotations

import time

from core.cancellation import CancellationToken


class ResponseTooLarge(ValueError):
    """The response cannot fit within the configured memory budget."""


def read_response_bytes(
    response, *, maximum_bytes: int, cancellation: CancellationToken | None = None,
    deadline: float | None = None,
) -> bytes:
    """Bound response memory and check cancellation/deadlines between reads."""
    headers = getattr(response, "headers", None)
    length = headers.get("Content-Length") if headers is not None else None
    if length is not None:
        try:
            declared = int(length)
        except (TypeError, ValueError):
            declared = None
        if declared is not None and (declared < 0 or declared > maximum_bytes):
            raise ResponseTooLarge(f"Response exceeds the {maximum_bytes}-byte size limit.")
    read = getattr(response, "read1", None) or response.read
    chunks = []
    size = 0
    while True:
        if cancellation:
            cancellation.raise_if_cancelled()
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Response exceeded its total deadline.")
        chunk = read(min(64 * 1024, maximum_bytes - size + 1))
        if not chunk:
            if cancellation:
                cancellation.raise_if_cancelled()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Response exceeded its total deadline.")
            return b"".join(chunks)
        size += len(chunk)
        if size > maximum_bytes:
            raise ResponseTooLarge(f"Response exceeds the {maximum_bytes}-byte size limit.")
        chunks.append(chunk)
