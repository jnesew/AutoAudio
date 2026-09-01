from __future__ import annotations

import argparse
import contextlib
import io
import os
import traceback
from pathlib import Path

from core.cancellation import CancellationToken
from core.checkpoint import CheckpointStore
from core.config import AppConfig
from core.errors import PipelineCancelled, format_user_error
from core.narrator import NarratorCatalog
from core.pipeline import (
    build_app_config,
    build_argument_parser,
    detect_source_mode,
    resolve_metadata,
    run_pipeline,
)
from gui.state import bool_from_ui_state, load_resume_context


class _SignalWriter(io.TextIOBase):
    def __init__(self, emit_fn):
        super().__init__()
        self._emit = emit_fn

    def write(self, value: str) -> int:
        text = value.rstrip("\n")
        if text:
            self._emit(text)
        return len(value)


def launch_gui(project_root: Path) -> int:
    try:
        from PySide6.QtCore import QObject, QThread, Signal
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QDoubleSpinBox,
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
            QScrollArea,
            QSpinBox,
            QTabWidget,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise RuntimeError("PySide6 is required for --gui mode. Install with `pip install PySide6`.") from exc

    def combo(values: tuple[str, ...], current: str) -> QComboBox:
        widget = QComboBox()
        widget.addItems(values)
        index = widget.findText(current)
        if index >= 0:
            widget.setCurrentIndex(index)
        return widget

    def spin(value: int, minimum: int, maximum: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        return widget

    def decimal(value: float, minimum: float, maximum: float, decimals: int = 3) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        return widget

    def scrollable(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(widget)
        return area

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
        finished = Signal(str, str)
        log_line = Signal(str)

        def __init__(self, args: argparse.Namespace, config: AppConfig):
            super().__init__()
            self.args = args
            self.config = config
            self.cancellation = CancellationToken()

        def cancel(self) -> None:
            self.cancellation.cancel()

        def run(self) -> None:
            out_writer = _SignalWriter(self.log_line.emit)
            try:
                with contextlib.redirect_stdout(out_writer), contextlib.redirect_stderr(out_writer):
                    run_pipeline(self.args, self.config, cancellation=self.cancellation)
                self.finished.emit("completed", "Pipeline run completed.")
            except PipelineCancelled as exc:
                self.finished.emit("cancelled", str(exc))
            except Exception as exc:  # noqa: BLE001
                self.log_line.emit(traceback.format_exc())
                self.finished.emit("failed", format_user_error(exc))

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("AutoAudio")
            self.resize(980, 760)

            self.project_root = project_root
            self.parser = build_argument_parser(project_root)
            self.default_args = self.parser.parse_args([])
            self.app_config = AppConfig(project_root=self.project_root)
            self.narrator_catalog = NarratorCatalog.load(self.app_config.narrator_profiles_path)
            self.worker_thread: QThread | None = None
            self.worker: PipelineWorker | None = None
            self.resume_available = False

            central = QWidget()
            self.setCentralWidget(central)
            layout = QVBoxLayout(central)
            self.tabs = QTabWidget()
            self.tabs.addTab(scrollable(self._build_book_tab()), "Book")
            self.tabs.addTab(scrollable(self._build_narrator_tab()), "Narrator")
            self.tabs.addTab(scrollable(self._build_output_runtime_tab()), "Output & Runtime")
            self.tabs.addTab(scrollable(self._build_provenance_tab()), "Provenance")
            layout.addWidget(self.tabs, 1)

            controls = QHBoxLayout()
            self.start_btn = QPushButton("Start")
            self.resume_btn = QPushButton("Resume")
            self.cancel_btn = QPushButton("Cancel")
            self.resume_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            self.start_btn.clicked.connect(lambda: self._launch_from_ui("no"))
            self.resume_btn.clicked.connect(lambda: self._launch_from_ui("yes"))
            self.cancel_btn.clicked.connect(self._cancel_run)
            controls.addWidget(self.start_btn)
            controls.addWidget(self.resume_btn)
            controls.addWidget(self.cancel_btn)
            controls.addStretch(1)
            layout.addLayout(controls)

            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            layout.addWidget(self.progress)
            self.log = QTextEdit()
            self.log.setReadOnly(True)
            layout.addWidget(self.log, 1)

            self._on_narrator_profile_changed(self.narrator_profile_combo.currentIndex())
            self._prepopulate_from_checkpoint()
            self._on_input_changed(self.input_edit.text())

        def _build_book_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            paths = QGroupBox("Input / Output")
            path_layout = QGridLayout(paths)
            self.input_edit = FileDropLineEdit(self.default_args.input_book)
            self.input_edit.file_dropped.connect(self._on_input_changed)
            self.input_edit.editingFinished.connect(lambda: self._on_input_changed(self.input_edit.text()))
            input_button = QPushButton("Browse…")
            input_button.clicked.connect(self._pick_input)
            self.output_edit = QLineEdit(self.default_args.output_dir)
            self.output_edit.editingFinished.connect(self._prepopulate_from_checkpoint)
            output_button = QPushButton("Browse…")
            output_button.clicked.connect(self._pick_output_dir)
            path_layout.addWidget(QLabel("Input file"), 0, 0)
            path_layout.addWidget(self.input_edit, 0, 1)
            path_layout.addWidget(input_button, 0, 2)
            path_layout.addWidget(QLabel("Output directory"), 1, 0)
            path_layout.addWidget(self.output_edit, 1, 1)
            path_layout.addWidget(output_button, 1, 2)
            layout.addWidget(paths)

            planning = QGroupBox("Parsing and planning")
            form = QFormLayout(planning)
            self.source_mode_combo = combo(("auto", "epub", "text"), self.default_args.source_mode)
            self.pages_per_chapter_spin = spin(self.default_args.pages_per_chapter, 1, 100_000)
            self.target_words_per_chapter_spin = spin(self.default_args.target_words_per_chapter, 1, 1_000_000)
            self.min_paragraphs_per_chapter_spin = spin(self.default_args.min_paragraphs_per_chapter, 1, 100_000)
            self.chapters_per_part_spin = spin(self.default_args.chapters_per_part, 1, 100_000)
            self.target_words_per_segment_edit = QLineEdit()
            self.target_words_per_segment_edit.setPlaceholderText("Profile default")
            self.max_words_per_segment_edit = QLineEdit()
            self.max_words_per_segment_edit.setPlaceholderText("Profile default")
            form.addRow("Source mode", self.source_mode_combo)
            form.addRow("EPUB documents per chapter", self.pages_per_chapter_spin)
            form.addRow("Target words per text chapter", self.target_words_per_chapter_spin)
            form.addRow("Minimum paragraphs per chapter", self.min_paragraphs_per_chapter_spin)
            form.addRow("Chapters per part", self.chapters_per_part_spin)
            form.addRow("Target words per segment", self.target_words_per_segment_edit)
            form.addRow("Maximum words per segment", self.max_words_per_segment_edit)
            layout.addWidget(planning)

            metadata_options = QGroupBox("Metadata")
            metadata_form = QFormLayout(metadata_options)
            self.fetch_metadata_checkbox = QCheckBox("Fetch optional Gutenberg metadata")
            self.gutenberg_id_edit = QLineEdit()
            self.title_edit = QLineEdit()
            self.author_edit = QLineEdit()
            metadata_form.addRow(self.fetch_metadata_checkbox)
            metadata_form.addRow("Gutenberg ID", self.gutenberg_id_edit)
            metadata_form.addRow("Title override", self.title_edit)
            metadata_form.addRow("Author override", self.author_edit)
            layout.addWidget(metadata_options)

            preview = QGroupBox("Metadata preview")
            preview_form = QFormLayout(preview)
            self.meta_title = QLabel("-")
            self.meta_author = QLabel("-")
            self.meta_language = QLabel("-")
            preview_form.addRow("Title", self.meta_title)
            preview_form.addRow("Author", self.meta_author)
            preview_form.addRow("Language", self.meta_language)
            layout.addWidget(preview)
            layout.addStretch(1)
            return page

        def _build_narrator_tab(self) -> QWidget:
            page = QWidget()
            form = QFormLayout(page)
            self.narrator_profile_combo = QComboBox()
            for profile in self.narrator_catalog.profiles:
                self.narrator_profile_combo.addItem(profile.name, profile.id)
            self.narrator_detail = QLabel()
            self.narrator_detail.setWordWrap(True)
            self.speaker_edit = QLineEdit()
            self.voice_instruct_edit = QLineEdit()
            self.model_choice_edit = QLineEdit()
            self.device_edit = QLineEdit()
            self.precision_edit = QLineEdit()
            self.language_edit = QLineEdit()
            self.seed_edit = QLineEdit()
            self.max_new_tokens_spin = spin(2048, 1, 1_000_000)
            self.top_p_spin = decimal(0.8, 0.001, 1.0)
            self.top_k_spin = spin(20, 1, 1_000_000)
            self.temperature_spin = decimal(1.0, 0.001, 100.0)
            self.repetition_penalty_spin = decimal(1.05, 0.001, 100.0)
            self.attention_combo = combo(("sdpa", "flash_attn"), "sdpa")
            self.unload_model_checkbox = QCheckBox("Unload model after each generation")
            form.addRow("Narrator profile", self.narrator_profile_combo)
            form.addRow(self.narrator_detail)
            form.addRow("Preset speaker", self.speaker_edit)
            form.addRow("Voice/style instruction", self.voice_instruct_edit)
            form.addRow("Model choice", self.model_choice_edit)
            form.addRow("Device", self.device_edit)
            form.addRow("Precision", self.precision_edit)
            form.addRow("Language", self.language_edit)
            form.addRow("Seed", self.seed_edit)
            form.addRow("Maximum new tokens", self.max_new_tokens_spin)
            form.addRow("Top-p", self.top_p_spin)
            form.addRow("Top-k", self.top_k_spin)
            form.addRow("Temperature", self.temperature_spin)
            form.addRow("Repetition penalty", self.repetition_penalty_spin)
            form.addRow("Attention", self.attention_combo)
            form.addRow(self.unload_model_checkbox)
            self.narrator_profile_combo.currentIndexChanged.connect(self._on_narrator_profile_changed)
            return page

        def _build_output_runtime_tab(self) -> QWidget:
            page = QWidget()
            form = QFormLayout(page)
            self.output_format_combo = combo(("flac", "mp3", "m4b"), self.default_args.output_format)
            self.disclosure_gap_spin = spin(self.default_args.disclosure_gap_ms, 0, 60_000)
            self.segment_gap_spin = spin(self.default_args.segment_gap_ms, 0, 60_000)
            self.chapter_gap_spin = spin(self.default_args.chapter_gap_ms, 0, 60_000)
            self.comfyui_mode_combo = combo(("network", "spoof"), self.default_args.comfyui_mode)
            self.comfyui_address_edit = QLineEdit(self.default_args.comfyui_server_address)
            self.comfyui_timeout_edit = QLineEdit()
            self.comfyui_timeout_edit.setPlaceholderText("900")
            self.spoof_scenario_combo = combo(
                ("success", "timeout", "malformed_history", "missing_view_payload", "connection_error"),
                self.default_args.comfyui_spoof_scenario,
            )
            form.addRow("Output format", self.output_format_combo)
            form.addRow("Disclosure gap (ms)", self.disclosure_gap_spin)
            form.addRow("Segment gap (ms)", self.segment_gap_spin)
            form.addRow("Chapter gap (ms)", self.chapter_gap_spin)
            form.addRow("ComfyUI mode", self.comfyui_mode_combo)
            form.addRow("ComfyUI server", self.comfyui_address_edit)
            form.addRow("ComfyUI timeout (seconds)", self.comfyui_timeout_edit)
            form.addRow("Spoof test scenario", self.spoof_scenario_combo)
            return page

        def _build_provenance_tab(self) -> QWidget:
            page = QWidget()
            form = QFormLayout(page)
            self.provenance_enabled_checkbox = QCheckBox("Enable C2PA provenance")
            self.provenance_cert_edit = QLineEdit()
            self.provenance_key_edit = QLineEdit()
            self.provenance_password_edit = QLineEdit()
            self.provenance_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.provenance_failure_combo = combo(("soft-fail", "hard-fail"), "soft-fail")
            self.provenance_tool_edit = QLineEdit("c2patool")
            self.provenance_claim_edit = QLineEdit("autoaudio")
            warning = QLabel("Private-key passwords are used for the current run only and are never written to checkpoints.")
            warning.setWordWrap(True)
            form.addRow(self.provenance_enabled_checkbox)
            form.addRow("Certificate path", self.provenance_cert_edit)
            form.addRow("Private-key path", self.provenance_key_edit)
            form.addRow("Private-key password", self.provenance_password_edit)
            form.addRow("Failure policy", self.provenance_failure_combo)
            form.addRow("C2PA tool", self.provenance_tool_edit)
            form.addRow("Claim generator", self.provenance_claim_edit)
            form.addRow(warning)
            return page

        def _pick_input(self) -> None:
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Select input file",
                self.input_edit.text() or str(self.project_root),
                "Books (*.epub *.txt *.md *.markdown *.rst);;All files (*.*)",
            )
            if file_path:
                self.input_edit.setText(file_path)
                self._on_input_changed(file_path)

        def _pick_output_dir(self) -> None:
            directory = QFileDialog.getExistingDirectory(
                self,
                "Select output directory",
                self.output_edit.text() or str(self.project_root / "audiobook_output"),
            )
            if directory:
                self.output_edit.setText(directory)
                self._prepopulate_from_checkpoint()

        def _on_narrator_profile_changed(self, _index: int) -> None:
            profile_id = self.narrator_profile_combo.currentData()
            if not profile_id:
                return
            profile = self.narrator_catalog.get(str(profile_id))
            settings = profile.settings
            self.speaker_edit.setText(settings.speaker)
            self.speaker_edit.setEnabled(settings.voice_mode == "preset")
            self.voice_instruct_edit.setText(settings.instruct)
            self.model_choice_edit.setText(settings.model_choice)
            self.device_edit.setText(settings.device)
            self.precision_edit.setText(settings.precision)
            self.language_edit.setText(settings.language)
            self.seed_edit.setText(str(settings.seed))
            self.max_new_tokens_spin.setValue(settings.max_new_tokens)
            self.top_p_spin.setValue(settings.top_p)
            self.top_k_spin.setValue(settings.top_k)
            self.temperature_spin.setValue(settings.temperature)
            self.repetition_penalty_spin.setValue(settings.repetition_penalty)
            self.attention_combo.setCurrentText(settings.attention)
            self.unload_model_checkbox.setChecked(settings.unload_model_after_generate)
            label = "Stable preset" if profile.stability == "stable" else "Experimental VoiceDesign"
            self.narrator_detail.setText(f"{label}: {profile.description}")

        def _on_input_changed(self, file_path: str) -> None:
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

        @staticmethod
        def _optional_int(text: str, label: str) -> int | None:
            value = text.strip()
            if not value:
                return None
            try:
                return int(value)
            except ValueError as exc:
                raise ValueError(f"{label} must be an integer.") from exc

        @staticmethod
        def _optional_float(text: str, label: str) -> float | None:
            value = text.strip()
            if not value:
                return None
            try:
                return float(value)
            except ValueError as exc:
                raise ValueError(f"{label} must be a number.") from exc

        def _collect_args(self, *, resume_mode: str) -> argparse.Namespace:
            args = self.parser.parse_args([])
            args.input_book = self.input_edit.text().strip() or args.input_book
            args.output_dir = self.output_edit.text().strip() or args.output_dir
            args.source_mode = self.source_mode_combo.currentText()
            args.pages_per_chapter = self.pages_per_chapter_spin.value()
            args.target_words_per_chapter = self.target_words_per_chapter_spin.value()
            args.min_paragraphs_per_chapter = self.min_paragraphs_per_chapter_spin.value()
            args.chapters_per_part = self.chapters_per_part_spin.value()
            args.target_words_per_segment = self._optional_int(
                self.target_words_per_segment_edit.text(), "Target words per segment"
            )
            args.max_words_per_segment = self._optional_int(
                self.max_words_per_segment_edit.text(), "Maximum words per segment"
            )
            args.disclosure_gap_ms = self.disclosure_gap_spin.value()
            args.segment_gap_ms = self.segment_gap_spin.value()
            args.chapter_gap_ms = self.chapter_gap_spin.value()
            args.narrator_profile = str(self.narrator_profile_combo.currentData())
            args.speaker = self.speaker_edit.text().strip() or None
            args.voice_instruct = self.voice_instruct_edit.text().strip() or None
            args.model_choice = self.model_choice_edit.text().strip() or None
            args.device = self.device_edit.text().strip() or None
            args.precision = self.precision_edit.text().strip() or None
            args.language = self.language_edit.text().strip() or None
            args.seed = self._optional_int(self.seed_edit.text(), "Seed")
            args.max_new_tokens = self.max_new_tokens_spin.value()
            args.top_p = self.top_p_spin.value()
            args.top_k = self.top_k_spin.value()
            args.temperature = self.temperature_spin.value()
            args.repetition_penalty = self.repetition_penalty_spin.value()
            args.attention = self.attention_combo.currentText()
            args.unload_model_after_generate = self.unload_model_checkbox.isChecked()
            args.output_format = self.output_format_combo.currentText()
            args.fetch_metadata = self.fetch_metadata_checkbox.isChecked()
            args.gutenberg_id = self.gutenberg_id_edit.text().strip()
            args.title = self.title_edit.text().strip()
            args.author = self.author_edit.text().strip()
            args.comfyui_mode = self.comfyui_mode_combo.currentText()
            args.comfyui_server_address = self.comfyui_address_edit.text().strip()
            args.comfyui_timeout_seconds = self._optional_float(
                self.comfyui_timeout_edit.text(), "ComfyUI timeout"
            )
            args.comfyui_spoof_scenario = self.spoof_scenario_combo.currentText()
            args.resume = resume_mode
            args.provenance_enabled = self.provenance_enabled_checkbox.isChecked()
            args.provenance_cert_path = self.provenance_cert_edit.text().strip()
            args.provenance_key_path = self.provenance_key_edit.text().strip()
            args.provenance_key_password = self.provenance_password_edit.text()
            args.provenance_failure_mode = self.provenance_failure_combo.currentText()
            args.provenance_tool = self.provenance_tool_edit.text().strip()
            args.provenance_claim_generator = self.provenance_claim_edit.text().strip()
            return args

        def _append_log(self, line: str) -> None:
            self.log.append(line)

        def _set_running(self, running: bool) -> None:
            self.tabs.setEnabled(not running)
            self.start_btn.setEnabled(not running)
            self.resume_btn.setEnabled((not running) and self.resume_available)
            self.cancel_btn.setEnabled(running)
            self.cancel_btn.setText("Cancel")
            if running:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(100)

        def _launch_from_ui(self, resume_mode: str) -> None:
            try:
                args = self._collect_args(resume_mode=resume_mode)
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid setting", str(exc))
                return
            self._launch_pipeline(args)

        def _cancel_run(self) -> None:
            if self.worker and self.worker_thread and self.worker_thread.isRunning():
                self.worker.cancel()
                self.cancel_btn.setEnabled(False)
                self.cancel_btn.setText("Canceling…")
                self._append_log("Cancellation requested; waiting for the current safe operation to stop.")

        def _launch_pipeline(self, args: argparse.Namespace) -> None:
            if self.worker_thread and self.worker_thread.isRunning():
                return
            if not os.path.isfile(args.input_book):
                QMessageBox.warning(self, "Invalid input", "Please select a valid input book file.")
                return
            os.makedirs(args.output_dir, exist_ok=True)
            self.log.clear()
            self._set_running(True)
            self.worker_thread = QThread(self)
            self.worker = PipelineWorker(args=args, config=build_app_config(args, self.project_root))
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.log_line.connect(self._append_log)
            self.worker.finished.connect(self._on_worker_finished)
            self.worker.finished.connect(lambda *_: self.worker_thread.quit())
            self.worker_thread.start()

        def _on_worker_finished(self, status: str, message: str) -> None:
            self._set_running(False)
            if status == "completed":
                self._append_log(message)
                QMessageBox.information(self, "AutoAudio", "Generation finished.")
            elif status == "cancelled":
                self._append_log(f"Canceled: {message}")
                QMessageBox.information(self, "AutoAudio", "Run canceled. Resume state was saved.")
            else:
                self._append_log(f"Failed: {message}")
                QMessageBox.critical(self, "AutoAudio", f"Generation failed:\n{message}")
            self._prepopulate_from_checkpoint()

        @staticmethod
        def _set_combo(widget: QComboBox, value) -> None:
            index = widget.findData(value)
            if index < 0:
                index = widget.findText(str(value))
            if index >= 0:
                widget.setCurrentIndex(index)

        @staticmethod
        def _restore_text(widget: QLineEdit, state: dict, key: str) -> None:
            if key in state and state[key] is not None:
                widget.setText(str(state[key]))

        def _prepopulate_from_checkpoint(self) -> None:
            checkpoint_store = CheckpointStore(
                state_dir=AppConfig.state_dir_for(self.output_edit.text() or self.default_args.output_dir)
            )
            context = load_resume_context(checkpoint_store)
            self.resume_available = context is not None
            self.resume_btn.setEnabled(self.resume_available)
            if not context:
                return
            state = context.ui_state
            self._restore_text(self.input_edit, state, "input_book")
            self._restore_text(self.output_edit, state, "output_dir")
            self._set_combo(self.source_mode_combo, state.get("source_mode"))
            self._set_combo(self.narrator_profile_combo, state.get("narrator_profile"))
            self._set_combo(self.output_format_combo, state.get("output_format"))
            self._set_combo(self.comfyui_mode_combo, state.get("comfyui_mode"))
            self._set_combo(self.spoof_scenario_combo, state.get("comfyui_spoof_scenario"))
            self._set_combo(self.attention_combo, state.get("attention"))
            self._set_combo(self.provenance_failure_combo, state.get("provenance_failure_mode"))
            for widget, key in (
                (self.pages_per_chapter_spin, "pages_per_chapter"),
                (self.target_words_per_chapter_spin, "target_words_per_chapter"),
                (self.min_paragraphs_per_chapter_spin, "min_paragraphs_per_chapter"),
                (self.chapters_per_part_spin, "chapters_per_part"),
                (self.disclosure_gap_spin, "disclosure_gap_ms"),
                (self.segment_gap_spin, "segment_gap_ms"),
                (self.chapter_gap_spin, "chapter_gap_ms"),
                (self.max_new_tokens_spin, "max_new_tokens"),
                (self.top_k_spin, "top_k"),
            ):
                if state.get(key) is not None:
                    widget.setValue(int(state[key]))
            for widget, key in (
                (self.top_p_spin, "top_p"),
                (self.temperature_spin, "temperature"),
                (self.repetition_penalty_spin, "repetition_penalty"),
            ):
                if state.get(key) is not None:
                    widget.setValue(float(state[key]))
            for widget, key in (
                (self.target_words_per_segment_edit, "target_words_per_segment"),
                (self.max_words_per_segment_edit, "max_words_per_segment"),
                (self.speaker_edit, "speaker"),
                (self.voice_instruct_edit, "voice_instruct"),
                (self.model_choice_edit, "model_choice"),
                (self.device_edit, "device"),
                (self.precision_edit, "precision"),
                (self.language_edit, "language"),
                (self.seed_edit, "seed"),
                (self.gutenberg_id_edit, "gutenberg_id"),
                (self.title_edit, "title"),
                (self.author_edit, "author"),
                (self.comfyui_address_edit, "comfyui_server_address"),
                (self.comfyui_timeout_edit, "comfyui_timeout_seconds"),
                (self.provenance_cert_edit, "provenance_cert_path"),
                (self.provenance_key_edit, "provenance_key_path"),
                (self.provenance_tool_edit, "provenance_tool"),
                (self.provenance_claim_edit, "provenance_claim_generator"),
            ):
                self._restore_text(widget, state, key)
            self.fetch_metadata_checkbox.setChecked(bool_from_ui_state(state.get("fetch_metadata"), default=False))
            self.unload_model_checkbox.setChecked(
                bool_from_ui_state(state.get("unload_model_after_generate"), default=False)
            )
            self.provenance_enabled_checkbox.setChecked(
                bool_from_ui_state(state.get("provenance_enabled"), default=False)
            )
            self._append_log(f"Detected resumable run at {context.checkpoint_path}")

        def closeEvent(self, event) -> None:
            if self.worker and self.worker_thread and self.worker_thread.isRunning():
                self._cancel_run()
                event.ignore()
                return
            super().closeEvent(event)

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
