from __future__ import annotations

import os


AUTOAUDIO_VERSION = "2.0.0.dev0"


def runtime_autoaudio_version() -> str:
    return (os.environ.get("AUTOAUDIO_VERSION") or AUTOAUDIO_VERSION).strip() or AUTOAUDIO_VERSION


def default_claim_generator() -> str:
    return f"AutoAudio/{runtime_autoaudio_version()}"
