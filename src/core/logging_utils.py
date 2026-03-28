from __future__ import annotations

import logging
from pathlib import Path


def configure_run_logger(output_dir: str | Path) -> tuple[logging.Logger, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "autoaudio_debug.log"

    logger = logging.getLogger("autoaudio.run")
    logger.setLevel(logging.INFO)

    # Reconfigure handlers each run to avoid duplicate lines across test runs.
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger, log_path
