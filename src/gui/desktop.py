"""Explicit per-user Linux launcher installation; importing this module writes nothing."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from core.config import AppConfig
from core.pipeline import runtime_project_root


DESKTOP_FILE_ID = "io.github.jnesew.AutoAudio"


def _quote_exec_argument(value: str) -> str:
    # Desktop entries unescape strings before Exec quoting and field-code expansion.
    if any(character in value for character in "\r\n\0"):
        raise ValueError("Launcher executable paths cannot contain line breaks or NUL.")
    escaped = "".join(
        "\\" + character if character in '\\"`$' else character for character in value
    )
    return '"' + escaped.replace("\\", "\\\\").replace("%", "%%") + '"'


def install_desktop_entry(data_home: Path | None = None) -> Path:
    """Install a launcher for the active Python environment, without running the app."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Desktop-entry installation is supported on Linux.")
    executable = Path(sys.executable).absolute().with_name("autoaudio")
    if not executable.is_file():
        raise RuntimeError(
            "Install AutoAudio in this Python environment before registering its launcher."
        )
    config = AppConfig(project_root=runtime_project_root())
    icon_path = config.app_icon_path.resolve()
    if not icon_path.is_file():
        raise RuntimeError("AutoAudio's packaged application icon is missing.")
    if data_home is None:
        configured = os.environ.get("XDG_DATA_HOME", "")
        data_home = (
            Path(configured)
            if configured and Path(configured).is_absolute()
            else Path.home() / ".local" / "share"
        )
    destination = data_home / "applications" / f"{DESKTOP_FILE_ID}.desktop"
    icon_value = (
        str(icon_path)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AutoAudio\n"
        "Comment=Create audiobooks from ebooks and text\n"
        f"Exec={_quote_exec_argument(str(executable))} --gui\n"
        f"Icon={icon_value}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;\n"
        "StartupWMClass=AutoAudio\n"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


if __name__ == "__main__":
    print(install_desktop_entry())
