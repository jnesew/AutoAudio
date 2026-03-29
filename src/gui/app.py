from __future__ import annotations

import argparse
import contextlib
import io
import os
import traceback
from pathlib import Path

from comfyui.real_client import RealComfyUIClient
from comfyui.workflow_loader import load_workflow_template
from core.checkpoint import CheckpointStore
from core.config import AppConfig
from core.errors import format_user_error
from core.pipeline import build_argument_parser, detect_source_mode, run_pipeline, resolve_metadata
from gui.state import bool_from_ui_state, load_resume_context


class _SignalWriter(io.TextIOBase):
    def __init__(self, emit_fn):
        super().__init__()
        self._emit = emit_fn

    def write(self, s: str) -> int:
        text = s.rstrip("\n")
        if text:
            self._emit(text)
        return len(s)


def launch_gui(project_root: Path) -> int:
    try:
        from PySide6.QtCore import QMimeData, QObject, Qt, QThread, Signal
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QFileDialog,
            QFormLayout,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPushButton,
            QProgressBar,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise RuntimeError("PySide6 is required for --gui mode. Install with `pip install PySide6`.") from exc

    class FileDropLineEdit(QLineEdit):
        file_dropped = Signal(str)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.setAcceptDrops(True)

        def dragEnterEvent(self, event):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event):
            urls = event.mimeData().urls()
            if not urls:
                return
            first = urls[0].toLocalFile()
            if first and os.path.isfile(first):
                self.setText(first)
                self.file_dropped.emit(first)

    class PipelineWorker(QObject):
        finished = Signal(bool, str)
        log_line = Signal(str)

        def __init__(self, args: argparse.Namespace, config: AppConfig):
            super().__init__()
            self.args = args
            self.config = config

        def run(self):
            out_writer = _SignalWriter(self.log_line.emit)
            try:
                with contextlib.redirect_stdout(out_writer), contextlib.redirect_stderr(out_writer):
                    run_pipeline(self.args, self.config)
                self.finished.emit(True, "Pipeline run completed.")
            except Exception as exc:  # noqa: BLE001
                self.log_line.emit(traceback.format_exc())
                self.finished.emit(False, format_user_error(exc))

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("AutoAudio")
            self.resize(960, 680)

            self.project_root = project_root
            self.parser = build_argument_parser(project_root)
            self.default_args = self.parser.parse_args([])

            self.worker_thread: QThread | None = None
            self.worker: PipelineWorker | None = None

            central = QWidget()
            self.setCentralWidget(central)
            layout = QVBoxLayout(central)

            io_group = QGroupBox("Input / Output")
            io_layout = QGridLayout(io_group)

            self.input_edit = FileDropLineEdit(self.default_args.input_book)
            self.input_edit.file_dropped.connect(self._on_input_changed)
            self.input_edit.editingFinished.connect(lambda: self._on_input_changed(self.input_edit.text()))
            in_btn = QPushButton("Browse…")
            in_btn.clicked.connect(self._pick_input)

            self.output_edit = QLineEdit(self.default_args.output_dir)
            out_btn = QPushButton("Browse…")
            out_btn.clicked.connect(self._pick_output_dir)

            self.reference_voice_edit = FileDropLineEdit("")
            self.reference_voice_edit.file_dropped.connect(self._on_reference_voice_changed)
            self.reference_voice_edit.editingFinished.connect(
                lambda: self._on_reference_voice_changed(self.reference_voice_edit.text())
            )
            ref_btn = QPushButton("Browse…")
            ref_btn.clicked.connect(self._pick_reference_voice)

            io_layout.addWidget(QLabel("Input file"), 0, 0)
            io_layout.addWidget(self.input_edit, 0, 1)
            io_layout.addWidget(in_btn, 0, 2)

            io_layout.addWidget(QLabel("Output directory"), 1, 0)
            io_layout.addWidget(self.output_edit, 1, 1)
            io_layout.addWidget(out_btn, 1, 2)

            io_layout.addWidget(QLabel("Reference voice"), 2, 0)
            io_layout.addWidget(self.reference_voice_edit, 2, 1)
            io_layout.addWidget(ref_btn, 2, 2)

            options_group = QGroupBox("Options")
            options_layout = QHBoxLayout(options_group)
            self.fetch_metadata_checkbox = QCheckBox("Fetch metadata")
            self.fetch_metadata_checkbox.setChecked(False)
            options_layout.addWidget(self.fetch_metadata_checkbox)
            options_layout.addStretch(1)

            metadata_group = QGroupBox("Metadata preview")
            form = QFormLayout(metadata_group)
            self.meta_title = QLabel("-")
            self.meta_author = QLabel("-")
            self.meta_language = QLabel("-")
            form.addRow("Title", self.meta_title)
            form.addRow("Author", self.meta_author)
            form.addRow("Language", self.meta_language)

            controls = QHBoxLayout()
            self.start_btn = QPushButton("Start")
            self.resume_btn = QPushButton("Resume")
            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.setEnabled(False)
            self.resume_btn.setEnabled(False)
            self.start_btn.clicked.connect(self._start_run)
            self.resume_btn.clicked.connect(self._resume_run)
            self.cancel_btn.clicked.connect(self._cancel_run)
            controls.addWidget(self.start_btn)
            controls.addWidget(self.resume_btn)
            controls.addWidget(self.cancel_btn)
            controls.addStretch(1)

            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)

            self.log = QTextEdit()
            self.log.setReadOnly(True)

            layout.addWidget(io_group)
            layout.addWidget(options_group)
            layout.addWidget(metadata_group)
            layout.addLayout(controls)
            layout.addWidget(self.progress)
            layout.addWidget(self.log, 1)

            self._prepopulate_from_checkpoint()
            self._on_input_changed(self.input_edit.text())

        def _pick_input(self):
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Select input file",
                self.input_edit.text() or str(self.project_root),
                "Books (*.epub *.txt *.md *.markdown *.rst);;All files (*.*)",
            )
            if file_path:
                self.input_edit.setText(file_path)
                self._on_input_changed(file_path)

        def _pick_output_dir(self):
            dir_path = QFileDialog.getExistingDirectory(
                self,
                "Select output directory",
                self.output_edit.text() or str(self.project_root / "audiobook_output"),
            )
            if dir_path:
                self.output_edit.setText(dir_path)

        def _pick_reference_voice(self):
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Select reference voice audio",
                self.reference_voice_edit.text() or str(self.project_root),
                (
                    "Audio files (*.wav *.mp3 *.flac *.m4a *.ogg *.aac *.aif *.aiff *.wma);;"
                    "All files (*.*)"
                ),
            )
            if file_path:
                self.reference_voice_edit.setText(file_path)
                self._on_reference_voice_changed(file_path)

        def _on_reference_voice_changed(self, file_path: str):
            normalized = file_path.strip()
            if not normalized:
                return

            if not os.path.isfile(normalized):
                QMessageBox.warning(self, "Invalid audio", "Please select a valid reference voice audio file.")
                return

            args = self._collect_args(resume_mode="auto")
            if args.comfyui_mode != "network":
                self._append_log("Reference voice upload is only available in network ComfyUI mode.")
                return

            try:
                config = self._build_config(args)
                upload_workflow = load_workflow_template(config.workflows_dir / "upload_voice.json")
                client = RealComfyUIClient(config.comfyui_server_address)
                self._append_log(f"Uploading reference voice from: {normalized}")
                client.upload_reference_voice(
                    file_path=normalized,
                    target_filename=config.default_voice_filename,
                    upload_workflow_template=upload_workflow,
                    timeout_seconds=config.comfyui_timeout_seconds,
                )
                self._append_log("Reference voice uploaded as default_voice.wav.")
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Upload failed", f"Failed to upload reference voice:\n{format_user_error(exc)}")

        def _on_input_changed(self, file_path: str):
            if not file_path or not os.path.exists(file_path):
                self.meta_title.setText("-")
                self.meta_author.setText("-")
                self.meta_language.setText("-")
                return

            try:
                args = self._collect_args(resume_mode="auto")
                source_mode = detect_source_mode(file_path, args.source_mode)
                metadata = resolve_metadata(args, file_path, source_mode, args.output_dir)
                self.meta_title.setText(metadata.title or "-")
                self.meta_author.setText(metadata.author or "-")
                self.meta_language.setText(metadata.language or "-")
            except Exception:  # noqa: BLE001
                self.meta_title.setText("(unavailable)")
                self.meta_author.setText("(unavailable)")
                self.meta_language.setText("(unavailable)")

        def _append_log(self, line: str):
            self.log.append(line)
            if "Processing" in line:
                self.progress.setRange(0, 0)

        def _set_running(self, running: bool):
            self.start_btn.setEnabled(not running)
            self.resume_btn.setEnabled((not running) and self.resume_btn.isEnabled())
            self.cancel_btn.setEnabled(running)
            if running:
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(100)

        def _collect_args(self, *, resume_mode: str) -> argparse.Namespace:
            args = self.parser.parse_args([])
            args.input_book = self.input_edit.text().strip() or args.input_book
            args.output_dir = self.output_edit.text().strip() or args.output_dir
            args.fetch_metadata = self.fetch_metadata_checkbox.isChecked()
            args.resume = resume_mode
            return args

        def _build_config(self, args: argparse.Namespace) -> AppConfig:
            return AppConfig(
                project_root=self.project_root,
                comfyui_mode=args.comfyui_mode,
                comfyui_server_address=args.comfyui_server_address,
                comfyui_timeout_seconds=args.comfyui_timeout_seconds,
                comfyui_spoof_scenario=args.comfyui_spoof_scenario,
            )

        def _start_run(self):
            self._launch_pipeline(self._collect_args(resume_mode="no"))

        def _resume_run(self):
            self._launch_pipeline(self._collect_args(resume_mode="yes"))

        def _cancel_run(self):
            if self.worker_thread and self.worker_thread.isRunning():
                self.worker_thread.requestInterruption()
                self.worker_thread.quit()
                self.worker_thread.wait(2000)
                self._append_log("Run canceled by user.")
                self._set_running(False)

        def _launch_pipeline(self, args: argparse.Namespace):
            input_file = args.input_book
            if not os.path.isfile(input_file):
                QMessageBox.warning(self, "Invalid input", "Please select a valid input book file.")
                return

            os.makedirs(args.output_dir, exist_ok=True)
            self.log.clear()
            self._set_running(True)

            self.worker_thread = QThread(self)
            self.worker = PipelineWorker(args=args, config=self._build_config(args))
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.log_line.connect(self._append_log)
            self.worker.finished.connect(self._on_worker_finished)
            self.worker.finished.connect(lambda *_: self.worker_thread.quit())
            self.worker_thread.start()

        def _on_worker_finished(self, ok: bool, message: str):
            self._set_running(False)
            if ok:
                self._append_log(message)
                QMessageBox.information(self, "AutoAudio", "Generation finished.")
            else:
                self._append_log(f"Failed: {message}")
                QMessageBox.critical(self, "AutoAudio", f"Generation failed:\n{message}")
            self._prepopulate_from_checkpoint()

        def _prepopulate_from_checkpoint(self):
            checkpoint_store = CheckpointStore(state_dir=AppConfig(project_root=self.project_root).state_dir)
            resume_context = load_resume_context(checkpoint_store)
            if not resume_context:
                return

            ui_state = resume_context.ui_state
            self.input_edit.setText(str(ui_state.get("input_book", self.input_edit.text())))
            self.output_edit.setText(str(ui_state.get("output_dir", self.output_edit.text())))
            self.fetch_metadata_checkbox.setChecked(bool_from_ui_state(ui_state.get("fetch_metadata"), default=False))
            self.resume_btn.setEnabled(True)
            self._append_log(f"Detected resumable run at {resume_context.checkpoint_path}")

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    return app.exec()
