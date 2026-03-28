from __future__ import annotations

import os


def detect_source_mode(input_path: str, requested_mode: str) -> str:
    if requested_mode in {"epub", "text"}:
        return requested_mode

    ext = os.path.splitext(input_path)[1].lower()
    if ext == ".epub":
        return "epub"
    if ext in {".txt", ".md", ".markdown", ".rst"}:
        return "text"
    raise ValueError("Unsupported input type. Use --source-mode epub or --source-mode text.")
