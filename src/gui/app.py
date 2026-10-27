from __future__ import annotations

from pathlib import Path


def launch_gui(project_root: Path) -> int:
    try:
        from PySide6.QtWidgets import QApplication
        from gui.window import MainWindow
    except ImportError as exc:
        raise RuntimeError(
            "PySide6 is required for --gui mode. Install project GUI dependencies."
        ) from exc
    app = QApplication.instance() or QApplication([])
    from gui.desktop import DESKTOP_FILE_ID

    app.setApplicationName("AutoAudio")
    app.setApplicationDisplayName("AutoAudio")
    app.setDesktopFileName(DESKTOP_FILE_ID)
    window = MainWindow(project_root)
    app.setWindowIcon(window.windowIcon())
    window.show()
    return app.exec()
