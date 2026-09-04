from __future__ import annotations

import argparse
import contextlib
import io
import os
import traceback
from pathlib import Path

from core.cancellation import CancellationToken
from core.checkpoint import CheckpointStore
from core.config import (
    AppConfig,
    QWEN_MODEL_CHOICES_BY_MODE,
    QWEN_PRESET_SPEAKERS,
    TTSConfig,
    TTS_PROVIDER_CHOICES,
)
from core.errors import PipelineCancelled, format_user_error
from core.library import LibraryBook, scan_library
from core.narrator import NarratorCatalog
from core.pipeline import (
    build_app_config,
    build_argument_parser,
    detect_source_mode,
    resolve_metadata,
    run_pipeline,
)
from core.progress import ProgressUpdate, format_progress_text
from gui.state import bool_from_ui_state, load_resume_context
from metadata.gutenberg_catalog import (
    GutenbergBook,
    GutenbergCatalogClient,
    GutenbergSearchPage,
)
from tts import discover_provider_voices


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
        from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
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
            QTreeWidget,
            QTreeWidgetItem,
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
        finished = Signal(str, str, str)
        log_line = Signal(str, str)
        progress_changed = Signal(str, object)

        def __init__(self, job_id: str, args: argparse.Namespace, config: AppConfig):
            super().__init__()
            self.job_id = job_id
            self.args = args
            self.config = config
            self.cancellation = CancellationToken()

        def cancel(self) -> None:
            self.cancellation.cancel()

        def run(self) -> None:
            out_writer = _SignalWriter(lambda line: self.log_line.emit(self.job_id, line))
            try:
                with contextlib.redirect_stdout(out_writer), contextlib.redirect_stderr(out_writer):
                    run_pipeline(
                        self.args,
                        self.config,
                        cancellation=self.cancellation,
                        progress_callback=lambda update: self.progress_changed.emit(self.job_id, update),
                    )
                self.finished.emit(self.job_id, "completed", "Pipeline run completed.")
            except PipelineCancelled as exc:
                self.finished.emit(self.job_id, "paused", str(exc))
            except Exception as exc:  # noqa: BLE001
                self.log_line.emit(self.job_id, traceback.format_exc())
                self.finished.emit(self.job_id, "failed", format_user_error(exc))

    class CatalogSearchWorker(QObject):
        finished = Signal(object)
        failed = Signal(str)

        def __init__(self, client: GutenbergCatalogClient, query: str, page_url: str | None):
            super().__init__()
            self.client = client
            self.query = query
            self.page_url = page_url

        def run(self) -> None:
            try:
                self.finished.emit(self.client.search(self.query, page_url=self.page_url))
            except Exception as exc:  # noqa: BLE001 - worker failures must always release the UI thread.
                self.failed.emit(str(exc))

    class CatalogDetailsWorker(QObject):
        finished = Signal(object)
        failed = Signal(str)

        def __init__(self, client: GutenbergCatalogClient, book: GutenbergBook):
            super().__init__()
            self.client = client
            self.book = book

        def run(self) -> None:
            try:
                self.finished.emit(self.client.load_book_details(self.book))
            except Exception as exc:  # noqa: BLE001 - worker failures must always release the UI thread.
                self.failed.emit(str(exc))

    class CatalogDownloadWorker(QObject):
        finished = Signal(str)
        failed = Signal(str)

        def __init__(
            self,
            client: GutenbergCatalogClient,
            book: GutenbergBook,
            books_dir: Path,
        ):
            super().__init__()
            self.client = client
            self.book = book
            self.books_dir = books_dir

        def run(self) -> None:
            acquisition = self.book.preferred_epub
            if acquisition is None:
                self.failed.emit("This catalog entry does not offer an EPUB download.")
                return
            try:
                path = self.client.download_epub(self.book, acquisition, self.books_dir)
            except Exception as exc:  # noqa: BLE001 - worker failures must always release the UI thread.
                self.failed.emit(str(exc))
                return
            self.finished.emit(str(path))

    class VoiceDiscoveryWorker(QObject):
        finished = Signal(str, object)
        failed = Signal(str, str)

        def __init__(self, config: TTSConfig, timeout_seconds: float | None):
            super().__init__()
            self.config = config
            self.timeout_seconds = timeout_seconds

        def run(self) -> None:
            try:
                voices = discover_provider_voices(
                    self.config,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - worker failures must always release the UI thread.
                self.failed.emit(self.config.provider, str(exc))
                return
            self.finished.emit(self.config.provider, voices)

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
            self.catalog_client = GutenbergCatalogClient()
            self.worker_thread: QThread | None = None
            self.worker: PipelineWorker | None = None
            self.catalog_thread: QThread | None = None
            self.catalog_worker: CatalogSearchWorker | CatalogDetailsWorker | CatalogDownloadWorker | None = None
            self.voice_thread: QThread | None = None
            self.voice_worker: VoiceDiscoveryWorker | None = None
            self.library_books: dict[str, LibraryBook] = {}
            self.library_items: dict[str, QTreeWidgetItem] = {}
            self.library_progress_bars: dict[str, QProgressBar] = {}
            self.current_library_book_id: str | None = None
            self.active_job_id: str | None = None
            self.queued_jobs: list[tuple[str, argparse.Namespace]] = []
            self.active_progress_percent = 0
            self._catalog_books: dict[str, GutenbergBook] = {}
            self._catalog_items: dict[str, QTreeWidgetItem] = {}
            self._catalog_next_url: str | None = None
            self._catalog_append_results = False
            self._pending_catalog_confirmation_id: str | None = None
            self._closing_after_pause = False
            self.resume_available = False

            central = QWidget()
            self.setCentralWidget(central)
            layout = QVBoxLayout(central)
            self.tabs = QTabWidget()
            self.library_tab_index = self.tabs.addTab(self._build_library_tab(), "Library")
            self.discover_tab_index = self.tabs.addTab(self._build_discover_tab(), "Find books")
            self.book_tab_index = self.tabs.addTab(scrollable(self._build_book_tab()), "Book")
            self.narrator_tab_index = self.tabs.addTab(scrollable(self._build_narrator_tab()), "Narrator")
            self.runtime_tab_index = self.tabs.addTab(
                scrollable(self._build_output_runtime_tab()), "Output & Runtime"
            )
            self.provenance_tab_index = self.tabs.addTab(
                scrollable(self._build_provenance_tab()), "Provenance"
            )
            layout.addWidget(self.tabs, 1)

            controls = QHBoxLayout()
            self.start_btn = QPushButton("Start")
            self.resume_btn = QPushButton("Resume")
            self.cancel_btn = QPushButton("Pause active")
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
            self.progress_status = QLabel("Ready")
            layout.addWidget(self.progress_status)
            self.log = QTextEdit()
            self.log.setReadOnly(True)
            layout.addWidget(self.log, 1)

            self._on_narrator_profile_changed(self.narrator_profile_combo.currentIndex())
            self._on_tts_provider_changed(self.tts_provider_combo.currentIndex())
            self._rescan_library()
            if not self.current_library_book_id:
                self._prepopulate_from_checkpoint()
                self._on_input_changed(self.input_edit.text())

        def _build_library_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)

            locations = QGroupBox("Library locations")
            location_layout = QGridLayout(locations)
            self.books_dir_edit = QLineEdit(str(self.project_root / "books"))
            self.library_output_root_edit = QLineEdit(str(self.project_root / "audiobook_output"))
            books_button = QPushButton("Browse…")
            books_button.clicked.connect(self._pick_books_dir)
            output_button = QPushButton("Browse…")
            output_button.clicked.connect(self._pick_library_output_root)
            location_layout.addWidget(QLabel("Books directory"), 0, 0)
            location_layout.addWidget(self.books_dir_edit, 0, 1)
            location_layout.addWidget(books_button, 0, 2)
            location_layout.addWidget(QLabel("Output root"), 1, 0)
            location_layout.addWidget(self.library_output_root_edit, 1, 1)
            location_layout.addWidget(output_button, 1, 2)
            layout.addWidget(locations)

            self.library_tree = QTreeWidget()
            self.library_tree.setColumnCount(4)
            self.library_tree.setHeaderLabels(("Title", "Author", "Status", "Progress"))
            self.library_tree.setRootIsDecorated(False)
            self.library_tree.setAlternatingRowColors(True)
            self.library_tree.itemSelectionChanged.connect(self._on_library_selection_changed)
            layout.addWidget(self.library_tree, 1)

            controls = QHBoxLayout()
            rescan_button = QPushButton("Rescan")
            rescan_button.clicked.connect(self._rescan_library)
            controls.addWidget(rescan_button)
            controls.addStretch(1)
            layout.addLayout(controls)
            self.library_summary = QLabel("No supported books found.")
            self.library_summary.setWordWrap(True)
            layout.addWidget(self.library_summary)
            return page

        def _build_discover_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            search_row = QHBoxLayout()
            self.catalog_search_edit = QLineEdit()
            self.catalog_search_edit.setPlaceholderText("Title, author, or search term")
            self.catalog_search_edit.returnPressed.connect(self._search_gutenberg)
            self.catalog_search_button = QPushButton("Search")
            self.catalog_search_button.clicked.connect(self._search_gutenberg)
            search_row.addWidget(self.catalog_search_edit, 1)
            search_row.addWidget(self.catalog_search_button)
            layout.addLayout(search_row)

            notice = QLabel(
                "Search uses Project Gutenberg's OPDS catalog. Details are loaded only for the selected "
                "title, and downloads occur only after confirmation. "
                "Project Gutenberg generally describes status under United States law; users outside the "
                "United States must check their local copyright law."
            )
            notice.setWordWrap(True)
            layout.addWidget(notice)

            self.catalog_tree = QTreeWidget()
            self.catalog_tree.setColumnCount(4)
            self.catalog_tree.setHeaderLabels(("Title", "Author", "Language", "EPUB"))
            self.catalog_tree.setRootIsDecorated(False)
            self.catalog_tree.setAlternatingRowColors(True)
            self.catalog_tree.itemSelectionChanged.connect(self._on_catalog_selection_changed)
            layout.addWidget(self.catalog_tree, 1)

            controls = QHBoxLayout()
            self.catalog_next_button = QPushButton("More results")
            self.catalog_next_button.setEnabled(False)
            self.catalog_next_button.clicked.connect(self._load_next_gutenberg_page)
            self.catalog_download_button = QPushButton("Review & download selected EPUB…")
            self.catalog_download_button.setEnabled(False)
            self.catalog_download_button.clicked.connect(self._download_selected_gutenberg)
            controls.addWidget(self.catalog_next_button)
            controls.addWidget(self.catalog_download_button)
            controls.addStretch(1)
            layout.addLayout(controls)
            self.catalog_status = QLabel("Enter a search and press Search.")
            layout.addWidget(self.catalog_status)
            return page

        def _build_book_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            paths = QGroupBox("Input / Output")
            path_layout = QGridLayout(paths)
            initial_input = self.default_args.input_book if os.path.isfile(self.default_args.input_book) else ""
            self.input_edit = FileDropLineEdit(initial_input)
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
            self.speaker_combo = combo(QWEN_PRESET_SPEAKERS, "Eric")
            self.voice_instruct_edit = QLineEdit()
            self.model_choice_combo = combo(QWEN_MODEL_CHOICES_BY_MODE["preset"], "1.7B")
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
            self.tts_model_edit = QLineEdit()
            self.tts_model_edit.setPlaceholderText("Required for OpenAI-compatible; ElevenLabs has a default")
            self.tts_voice_combo = QComboBox()
            self.tts_voice_combo.setEditable(True)
            self.tts_voice_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            self.tts_response_format_edit = QLineEdit()
            self.tts_response_format_edit.setPlaceholderText("Provider default")
            self.tts_language_code_edit = QLineEdit()
            self.tts_language_code_edit.setPlaceholderText("Optional ISO 639-1 code, e.g. en")
            self.voice_discovery_button = QPushButton("Discover voices")
            self.voice_discovery_button.clicked.connect(self._discover_voices)
            self.voice_discovery_status = QLabel(
                "Voice discovery never runs automatically; use this button to request it."
            )
            self.voice_discovery_status.setWordWrap(True)
            form.addRow("Narrator profile", self.narrator_profile_combo)
            form.addRow(self.narrator_detail)
            form.addRow("Preset speaker", self.speaker_combo)
            form.addRow("Voice/style instruction", self.voice_instruct_edit)
            form.addRow("Model choice", self.model_choice_combo)
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
            form.addRow("Endpoint model", self.tts_model_edit)
            form.addRow("Endpoint voice id", self.tts_voice_combo)
            form.addRow("Endpoint response format", self.tts_response_format_edit)
            form.addRow("Endpoint language code", self.tts_language_code_edit)
            form.addRow(self.voice_discovery_button)
            form.addRow(self.voice_discovery_status)
            self.narrator_profile_combo.currentIndexChanged.connect(self._on_narrator_profile_changed)
            return page

        def _build_output_runtime_tab(self) -> QWidget:
            page = QWidget()
            form = QFormLayout(page)
            self.output_format_combo = combo(("flac", "mp3", "m4b"), self.default_args.output_format)
            self.watermark_device_combo = combo(("auto", "cpu", "cuda"), self.default_args.watermark_device)
            self.disclosure_gap_spin = spin(self.default_args.disclosure_gap_ms, 0, 60_000)
            self.segment_gap_spin = spin(self.default_args.segment_gap_ms, 0, 60_000)
            self.chapter_gap_spin = spin(self.default_args.chapter_gap_ms, 0, 60_000)
            self.tts_provider_combo = combo(TTS_PROVIDER_CHOICES, self.default_args.tts_provider)
            self.tts_base_url_edit = QLineEdit()
            self.tts_base_url_edit.setPlaceholderText("Blank uses the provider default")
            self.tts_api_key_env_edit = QLineEdit(self.default_args.tts_api_key_env)
            self.comfyui_mode_combo = combo(("network", "spoof"), self.default_args.comfyui_mode)
            self.comfyui_address_edit = QLineEdit(self.default_args.comfyui_server_address)
            self.comfyui_timeout_edit = QLineEdit()
            self.comfyui_timeout_edit.setPlaceholderText("900")
            self.spoof_scenario_combo = combo(
                ("success", "timeout", "malformed_history", "missing_view_payload", "connection_error"),
                self.default_args.comfyui_spoof_scenario,
            )
            form.addRow("Output format", self.output_format_combo)
            form.addRow("AudioSeal device", self.watermark_device_combo)
            form.addRow("Disclosure gap (ms)", self.disclosure_gap_spin)
            form.addRow("Segment gap (ms)", self.segment_gap_spin)
            form.addRow("Chapter gap (ms)", self.chapter_gap_spin)
            form.addRow("TTS provider", self.tts_provider_combo)
            form.addRow("TTS base URL", self.tts_base_url_edit)
            form.addRow("API key environment variable", self.tts_api_key_env_edit)
            form.addRow("ComfyUI mode", self.comfyui_mode_combo)
            form.addRow("ComfyUI server", self.comfyui_address_edit)
            form.addRow("TTS timeout (seconds)", self.comfyui_timeout_edit)
            form.addRow("Spoof test scenario", self.spoof_scenario_combo)
            self.tts_provider_combo.currentIndexChanged.connect(self._on_tts_provider_changed)
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
            self.provenance_claim_edit = QLineEdit(self.default_args.provenance_claim_generator)
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

        def _pick_books_dir(self) -> None:
            directory = QFileDialog.getExistingDirectory(
                self,
                "Select books directory",
                self.books_dir_edit.text() or str(self.project_root / "books"),
            )
            if directory:
                self.books_dir_edit.setText(directory)
                self._rescan_library()

        def _pick_library_output_root(self) -> None:
            directory = QFileDialog.getExistingDirectory(
                self,
                "Select library output root",
                self.library_output_root_edit.text() or str(self.project_root / "audiobook_output"),
            )
            if directory:
                self.library_output_root_edit.setText(directory)
                self._rescan_library()

        def _queued_book_ids(self) -> set[str]:
            return {job_id for job_id, _args in self.queued_jobs}

        def _rescan_library(self, select_path: str | None = None) -> None:
            books_dir = Path(self.books_dir_edit.text().strip() or self.project_root / "books")
            output_root = Path(
                self.library_output_root_edit.text().strip() or self.project_root / "audiobook_output"
            )
            books_dir.mkdir(parents=True, exist_ok=True)
            output_root.mkdir(parents=True, exist_ok=True)
            selected_id = self.current_library_book_id
            entries = scan_library(books_dir, output_root)
            self.library_books = {entry.id: entry for entry in entries}
            self.library_items.clear()
            self.library_progress_bars.clear()
            self.library_tree.clear()
            queued = self._queued_book_ids()
            path_to_select = Path(select_path).resolve() if select_path else None
            for entry in entries:
                status = entry.status
                percent = entry.progress_percent
                if entry.id == self.active_job_id:
                    status = "Running"
                    percent = self.active_progress_percent
                elif entry.id in queued:
                    status = "Queued"
                item = QTreeWidgetItem(
                    (entry.title, entry.author, status, f"{percent}%" if percent else "—")
                )
                item.setData(0, Qt.ItemDataRole.UserRole, entry.id)
                item.setToolTip(0, str(entry.source_path))
                if entry.state_error:
                    item.setToolTip(2, entry.state_error)
                self.library_tree.addTopLevelItem(item)
                self.library_items[entry.id] = item
                progress_bar = QProgressBar()
                progress_bar.setRange(0, 100)
                progress_bar.setValue(percent)
                progress_bar.setTextVisible(True)
                self.library_tree.setItemWidget(item, 3, progress_bar)
                self.library_progress_bars[entry.id] = progress_bar
                if path_to_select and entry.source_path == path_to_select:
                    selected_id = entry.id

            self.library_tree.resizeColumnToContents(0)
            self.library_tree.resizeColumnToContents(1)
            self.library_tree.resizeColumnToContents(2)
            self.library_summary.setText(
                f"{len(entries)} supported title{'s' if len(entries) != 1 else ''}. "
                "Generated audio, checkpoints, and segments remain outside version control."
                if entries
                else "No supported EPUB, TXT, Markdown, or RST books found."
            )
            if selected_id in self.library_items:
                self.library_tree.setCurrentItem(self.library_items[selected_id])
            elif entries:
                self.library_tree.setCurrentItem(self.library_items[entries[0].id])
            else:
                self.current_library_book_id = None
                self.resume_available = False
                self._refresh_action_controls()

        def _on_library_selection_changed(self) -> None:
            selected = self.library_tree.selectedItems()
            if not selected:
                return
            book_id = selected[0].data(0, Qt.ItemDataRole.UserRole)
            entry = self.library_books.get(str(book_id))
            if entry is None:
                return
            self.current_library_book_id = entry.id
            self.input_edit.setText(str(entry.source_path))
            self.output_edit.setText(str(entry.output_dir))
            self.fetch_metadata_checkbox.setChecked(False)
            self.gutenberg_id_edit.clear()
            self.title_edit.clear()
            self.author_edit.clear()
            self._prepopulate_from_checkpoint()
            self._on_input_changed(str(entry.source_path))
            self._refresh_action_controls()

        def _set_library_runtime_state(self, book_id: str, status: str, percent: int | None = None) -> None:
            item = self.library_items.get(book_id)
            if item is None:
                return
            item.setText(2, status)
            if percent is not None:
                item.setText(3, f"{percent}%" if percent else "—")
                progress_bar = self.library_progress_bars.get(book_id)
                if progress_bar is not None:
                    progress_bar.setValue(percent)

        def _search_gutenberg(self, _checked: bool = False, *, page_url: str | None = None) -> None:
            if self.catalog_thread and self.catalog_thread.isRunning():
                return
            query = self.catalog_search_edit.text().strip()
            if not query:
                QMessageBox.warning(self, "Project Gutenberg", "Enter a title, author, or search term.")
                return
            self._catalog_append_results = page_url is not None
            self.catalog_search_button.setEnabled(False)
            self.catalog_next_button.setEnabled(False)
            self.catalog_download_button.setEnabled(False)
            self.catalog_status.setText("Searching Project Gutenberg…")
            self.catalog_thread = QThread(self)
            self.catalog_worker = CatalogSearchWorker(self.catalog_client, query, page_url)
            self.catalog_worker.moveToThread(self.catalog_thread)
            self.catalog_thread.started.connect(self.catalog_worker.run)
            self.catalog_worker.finished.connect(self._on_gutenberg_results)
            self.catalog_worker.failed.connect(self._on_gutenberg_error)
            self.catalog_worker.finished.connect(lambda *_: self.catalog_thread.quit())
            self.catalog_worker.failed.connect(lambda *_: self.catalog_thread.quit())
            self.catalog_thread.finished.connect(self._catalog_thread_stopped)
            self.catalog_thread.start()

        def _load_next_gutenberg_page(self) -> None:
            if self._catalog_next_url:
                self._search_gutenberg(page_url=self._catalog_next_url)

        def _on_gutenberg_results(self, page: GutenbergSearchPage) -> None:
            if not self._catalog_append_results:
                self.catalog_tree.clear()
                self._catalog_books.clear()
                self._catalog_items.clear()
            for book in page.books:
                existing = self._catalog_books.get(book.gutenberg_id)
                if existing is not None and existing.details_loaded and not book.details_loaded:
                    book = existing
                self._catalog_books[book.gutenberg_id] = book
                item = self._catalog_items.get(book.gutenberg_id)
                if item is None:
                    item = QTreeWidgetItem()
                    item.setData(0, Qt.ItemDataRole.UserRole, book.gutenberg_id)
                    self.catalog_tree.addTopLevelItem(item)
                    self._catalog_items[book.gutenberg_id] = item
                self._update_catalog_item(item, book)
            self._catalog_next_url = page.next_url
            self.catalog_next_button.setEnabled(page.next_url is not None)
            self.catalog_status.setText(
                f"Showing {len(self._catalog_books)} result{'s' if len(self._catalog_books) != 1 else ''}."
            )
            self._on_catalog_selection_changed()

        def _on_gutenberg_error(self, message: str) -> None:
            self.catalog_status.setText(f"Search failed: {message}")
            QMessageBox.warning(self, "Project Gutenberg", message)

        def _catalog_thread_stopped(self) -> None:
            self.catalog_thread = None
            self.catalog_worker = None
            self.catalog_search_button.setEnabled(True)
            self.catalog_next_button.setEnabled(self._catalog_next_url is not None)
            self._on_catalog_selection_changed()
            pending_id = self._pending_catalog_confirmation_id
            self._pending_catalog_confirmation_id = None
            if pending_id is not None:
                QTimer.singleShot(0, lambda: self._review_resolved_gutenberg_book(pending_id))

        @staticmethod
        def _catalog_format_text(book: GutenbergBook) -> str:
            acquisition = book.preferred_epub
            if acquisition is not None:
                return acquisition.title
            return "Unavailable" if book.details_loaded else "Select to review"

        def _update_catalog_item(self, item: QTreeWidgetItem, book: GutenbergBook) -> None:
            for column, value in enumerate(
                (book.title, book.author_text, book.language, self._catalog_format_text(book))
            ):
                item.setText(column, value)
            item.setToolTip(0, book.summary or book.landing_url)

        def _on_catalog_selection_changed(self) -> None:
            selected = self.catalog_tree.selectedItems()
            if not selected:
                self.catalog_download_button.setEnabled(False)
                return
            gutenberg_id = str(selected[0].data(0, Qt.ItemDataRole.UserRole))
            book = self._catalog_books.get(gutenberg_id)
            can_review = book is not None and (
                book.preferred_epub is not None
                or (not book.details_loaded and book.details_url is not None)
            )
            self.catalog_download_button.setEnabled(can_review)

        @staticmethod
        def _format_download_size(length: int | None) -> str:
            if length is None:
                return "not provided by the catalog"
            if length < 1024 * 1024:
                return f"{max(1, round(length / 1024))} KiB"
            return f"{length / (1024 * 1024):.1f} MiB"

        def _download_selected_gutenberg(self) -> None:
            if self.catalog_thread and self.catalog_thread.isRunning():
                return
            selected = self.catalog_tree.selectedItems()
            if not selected:
                return
            gutenberg_id = str(selected[0].data(0, Qt.ItemDataRole.UserRole))
            book = self._catalog_books.get(gutenberg_id)
            if book is None:
                return
            if not book.details_loaded:
                self.catalog_download_button.setEnabled(False)
                self.catalog_search_button.setEnabled(False)
                self.catalog_next_button.setEnabled(False)
                self.catalog_status.setText(f"Loading details for {book.title}…")
                self.catalog_thread = QThread(self)
                self.catalog_worker = CatalogDetailsWorker(self.catalog_client, book)
                self.catalog_worker.moveToThread(self.catalog_thread)
                self.catalog_thread.started.connect(self.catalog_worker.run)
                self.catalog_worker.finished.connect(self._on_gutenberg_details)
                self.catalog_worker.failed.connect(self._on_gutenberg_details_error)
                self.catalog_worker.finished.connect(lambda *_: self.catalog_thread.quit())
                self.catalog_worker.failed.connect(lambda *_: self.catalog_thread.quit())
                self.catalog_thread.finished.connect(self._catalog_thread_stopped)
                self.catalog_thread.start()
                return
            self._confirm_gutenberg_download(book)

        def _on_gutenberg_details(self, book: GutenbergBook) -> None:
            self._catalog_books[book.gutenberg_id] = book
            item = self._catalog_items.get(book.gutenberg_id)
            if item is not None:
                self._update_catalog_item(item, book)
            self.catalog_status.setText(f"Loaded details for {book.title}.")
            self._pending_catalog_confirmation_id = book.gutenberg_id

        def _on_gutenberg_details_error(self, message: str) -> None:
            self.catalog_status.setText(f"Could not load book details: {message}")
            QMessageBox.warning(self, "Project Gutenberg", message)

        def _review_resolved_gutenberg_book(self, gutenberg_id: str) -> None:
            book = self._catalog_books.get(gutenberg_id)
            if book is None:
                return
            if book.preferred_epub is None:
                self.catalog_status.setText(f"No EPUB is available for {book.title}.")
                QMessageBox.information(
                    self,
                    "Project Gutenberg",
                    "The selected catalog entry does not offer an EPUB download.",
                )
                self._on_catalog_selection_changed()
                return
            self._confirm_gutenberg_download(book)

        def _confirm_gutenberg_download(self, book: GutenbergBook) -> None:
            acquisition = book.preferred_epub
            if acquisition is None:
                return
            confirmation = QMessageBox(self)
            confirmation.setIcon(QMessageBox.Icon.Question)
            confirmation.setWindowTitle("Confirm Project Gutenberg download")
            confirmation.setTextFormat(Qt.TextFormat.PlainText)
            confirmation.setText(
                f"Download this specific book?\n\n"
                f"Title: {book.title}\n"
                f"Author: {book.author_text}\n"
                f"Language: {book.language}\n"
                f"Format: {acquisition.title}\n"
                f"Size: {self._format_download_size(acquisition.length)}\n\n"
                f"Catalog rights statement: {book.rights}\n\n"
                "Project Gutenberg's United States public-domain assessment may not apply in your jurisdiction."
            )
            confirmation.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            confirmation.setDefaultButton(QMessageBox.StandardButton.No)
            decision = confirmation.exec()
            if decision != QMessageBox.StandardButton.Yes:
                return

            self.catalog_download_button.setEnabled(False)
            self.catalog_search_button.setEnabled(False)
            self.catalog_next_button.setEnabled(False)
            self.catalog_status.setText(f"Downloading {book.title}…")
            self.catalog_thread = QThread(self)
            self.catalog_worker = CatalogDownloadWorker(
                self.catalog_client,
                book,
                Path(self.books_dir_edit.text().strip() or self.project_root / "books"),
            )
            self.catalog_worker.moveToThread(self.catalog_thread)
            self.catalog_thread.started.connect(self.catalog_worker.run)
            self.catalog_worker.finished.connect(self._on_gutenberg_downloaded)
            self.catalog_worker.failed.connect(self._on_gutenberg_download_error)
            self.catalog_worker.finished.connect(lambda *_: self.catalog_thread.quit())
            self.catalog_worker.failed.connect(lambda *_: self.catalog_thread.quit())
            self.catalog_thread.finished.connect(self._catalog_thread_stopped)
            self.catalog_thread.start()

        def _on_gutenberg_downloaded(self, path: str) -> None:
            self.catalog_status.setText(f"Downloaded {Path(path).name}.")
            self._rescan_library(select_path=path)
            self.tabs.setCurrentIndex(self.library_tab_index)
            QMessageBox.information(self, "Project Gutenberg", f"Downloaded to:\n{path}")

        def _on_gutenberg_download_error(self, message: str) -> None:
            self.catalog_status.setText(f"Download failed: {message}")
            QMessageBox.warning(self, "Project Gutenberg", message)

        def _on_narrator_profile_changed(self, _index: int) -> None:
            profile_id = self.narrator_profile_combo.currentData()
            if not profile_id:
                return
            profile = self.narrator_catalog.get(str(profile_id))
            settings = profile.settings
            self._set_combo(self.speaker_combo, settings.speaker)
            self.speaker_combo.setEnabled(settings.voice_mode == "preset")
            self.voice_instruct_edit.setText(settings.instruct)
            self.model_choice_combo.clear()
            self.model_choice_combo.addItems(QWEN_MODEL_CHOICES_BY_MODE[settings.voice_mode])
            self._set_combo(self.model_choice_combo, settings.model_choice)
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

        def _on_tts_provider_changed(self, _index: int) -> None:
            """Update controls only; provider changes must never contact an endpoint."""
            provider = self.tts_provider_combo.currentText()
            is_comfyui = provider == "comfyui"
            is_elevenlabs = provider == "elevenlabs"
            for widget in (
                self.narrator_profile_combo,
                self.speaker_combo,
                self.model_choice_combo,
                self.device_edit,
                self.precision_edit,
                self.language_edit,
                self.seed_edit,
                self.max_new_tokens_spin,
                self.top_p_spin,
                self.top_k_spin,
                self.temperature_spin,
                self.repetition_penalty_spin,
                self.attention_combo,
                self.unload_model_checkbox,
                self.comfyui_mode_combo,
                self.comfyui_address_edit,
                self.spoof_scenario_combo,
            ):
                widget.setEnabled(is_comfyui)
            self.voice_instruct_edit.setEnabled(provider in {"comfyui", "openai-compatible"})
            for widget in (
                self.tts_model_edit,
                self.tts_voice_combo,
                self.tts_response_format_edit,
                self.tts_base_url_edit,
                self.tts_api_key_env_edit,
            ):
                widget.setEnabled(not is_comfyui)
            self.tts_language_code_edit.setEnabled(is_elevenlabs)
            self.voice_discovery_button.setEnabled(is_elevenlabs and self.voice_thread is None)
            if is_comfyui:
                self.voice_discovery_status.setText(
                    "Bundled Qwen voices are already listed locally; no endpoint request is needed."
                )
            elif provider == "openai-compatible":
                self.voice_discovery_status.setText(
                    "OpenAI-compatible TTS has no standard voice-list endpoint; enter a voice id manually."
                )
            else:
                self.voice_discovery_status.setText(
                    "Voice discovery is idle. Press Discover voices to contact ElevenLabs explicitly."
                )

        def _voice_discovery_config(self) -> TTSConfig:
            return TTSConfig(
                provider=self.tts_provider_combo.currentText(),
                base_url=self.tts_base_url_edit.text().strip(),
                api_key_env=self.tts_api_key_env_edit.text().strip(),
                model=self.tts_model_edit.text().strip(),
                voice=self._selected_tts_voice(),
                response_format=self.tts_response_format_edit.text().strip(),
                language_code=self.tts_language_code_edit.text().strip(),
            )

        def _selected_tts_voice(self) -> str:
            value = self.tts_voice_combo.currentData()
            return str(value).strip() if value else self.tts_voice_combo.currentText().strip()

        def _discover_voices(self) -> None:
            if self.voice_thread and self.voice_thread.isRunning():
                return
            try:
                config = self._voice_discovery_config()
                timeout = self._optional_float(self.comfyui_timeout_edit.text(), "TTS timeout")
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid TTS setting", str(exc))
                return
            self.voice_discovery_button.setEnabled(False)
            self.voice_discovery_status.setText(f"Requesting {config.provider} voices…")
            self.voice_thread = QThread(self)
            self.voice_worker = VoiceDiscoveryWorker(config, timeout)
            self.voice_worker.moveToThread(self.voice_thread)
            self.voice_thread.started.connect(self.voice_worker.run)
            self.voice_worker.finished.connect(self._on_voices_discovered)
            self.voice_worker.failed.connect(self._on_voice_discovery_failed)
            self.voice_worker.finished.connect(lambda *_: self.voice_thread.quit())
            self.voice_worker.failed.connect(lambda *_: self.voice_thread.quit())
            self.voice_thread.finished.connect(self._voice_discovery_stopped)
            self.voice_thread.start()

        def _on_voices_discovered(self, provider: str, voices) -> None:
            if provider != self.tts_provider_combo.currentText():
                return
            selected = self._selected_tts_voice()
            self.tts_voice_combo.clear()
            for voice in voices:
                self.tts_voice_combo.addItem(voice.display_name, voice.id)
            if selected:
                self._set_combo(self.tts_voice_combo, selected)
                if self.tts_voice_combo.findData(selected) < 0:
                    self.tts_voice_combo.setEditText(selected)
            self.voice_discovery_status.setText(
                f"Loaded {len(voices)} eligible premade or generated voice{'s' if len(voices) != 1 else ''}."
            )

        def _on_voice_discovery_failed(self, provider: str, message: str) -> None:
            if provider == self.tts_provider_combo.currentText():
                self.voice_discovery_status.setText(f"Voice discovery failed: {message}")
                QMessageBox.warning(self, "Voice discovery", message)

        def _voice_discovery_stopped(self) -> None:
            self.voice_thread = None
            self.voice_worker = None
            self._on_tts_provider_changed(self.tts_provider_combo.currentIndex())

        def _on_input_changed(self, file_path: str) -> None:
            if not file_path or not os.path.exists(file_path):
                self.current_library_book_id = None
                self.meta_title.setText("-")
                self.meta_author.setText("-")
                self.meta_language.setText("-")
                self._refresh_action_controls()
                return
            resolved_input = Path(file_path).resolve()
            current = self.library_books.get(self.current_library_book_id or "")
            if current is None or current.source_path != resolved_input:
                self.current_library_book_id = next(
                    (
                        book.id
                        for book in self.library_books.values()
                        if book.source_path == resolved_input
                    ),
                    None,
                )
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
            self._refresh_action_controls()

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
            args.speaker = self.speaker_combo.currentText().strip() or None
            args.voice_instruct = self.voice_instruct_edit.text().strip() or None
            args.model_choice = self.model_choice_combo.currentText().strip() or None
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
            args.watermark_device = self.watermark_device_combo.currentText()
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
            args.tts_provider = self.tts_provider_combo.currentText()
            args.tts_base_url = self.tts_base_url_edit.text().strip()
            args.tts_api_key_env = self.tts_api_key_env_edit.text().strip()
            args.tts_model = self.tts_model_edit.text().strip()
            args.tts_voice = self._selected_tts_voice()
            args.tts_response_format = self.tts_response_format_edit.text().strip()
            args.tts_language_code = self.tts_language_code_edit.text().strip()
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
            for index in (
                self.discover_tab_index,
                self.book_tab_index,
                self.narrator_tab_index,
                self.runtime_tab_index,
                self.provenance_tab_index,
            ):
                self.tabs.setTabEnabled(index, not running)
            self.tabs.setTabEnabled(self.library_tab_index, True)
            self.cancel_btn.setEnabled(running)
            self.cancel_btn.setText("Pause active")
            if running:
                self.progress.setRange(0, 0)
                self.progress.setValue(0)
                self.progress_status.setText("Preparing book plan…")
            self._refresh_action_controls()

        def _refresh_action_controls(self) -> None:
            running = self.active_job_id is not None
            current_id = self.current_library_book_id
            current = self.library_books.get(current_id or "")
            current_is_busy = current_id == self.active_job_id or current_id in self._queued_book_ids()
            has_valid_input = os.path.isfile(self.input_edit.text().strip())
            self.start_btn.setText("Queue conversion" if running else "Start")
            self.resume_btn.setText("Queue resume" if running else "Resume")
            self.start_btn.setEnabled(has_valid_input and not current_is_busy)
            resumable = current.resumable if current is not None else self.resume_available
            self.resume_btn.setEnabled(bool(resumable) and not current_is_busy)

        def _launch_from_ui(self, resume_mode: str) -> None:
            try:
                args = self._collect_args(resume_mode=resume_mode)
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid setting", str(exc))
                return
            job_id = self.current_library_book_id or f"manual:{Path(args.input_book).resolve()}"
            if self.worker_thread and self.worker_thread.isRunning():
                if job_id == self.active_job_id or job_id in self._queued_book_ids():
                    return
                self.queued_jobs.append((job_id, args))
                self._set_library_runtime_state(job_id, "Queued")
                self._append_log(f"Queued {Path(args.input_book).name}.")
                self._refresh_action_controls()
                return
            self._launch_pipeline(args, job_id)

        def _cancel_run(self) -> None:
            if self.worker and self.worker_thread and self.worker_thread.isRunning():
                self.worker.cancel()
                self.cancel_btn.setEnabled(False)
                self.cancel_btn.setText("Pausing…")
                if self.active_job_id:
                    self._set_library_runtime_state(
                        self.active_job_id, "Pausing", self.active_progress_percent
                    )
                self._append_log("Pause requested; waiting for the current safe operation to checkpoint.")

        def _launch_pipeline(self, args: argparse.Namespace, job_id: str) -> bool:
            if self.worker_thread and self.worker_thread.isRunning():
                return False
            if not os.path.isfile(args.input_book):
                QMessageBox.warning(self, "Invalid input", "Please select a valid input book file.")
                return False
            try:
                os.makedirs(args.output_dir, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "Invalid output", f"Could not create the output directory:\n{exc}")
                return False
            if not self.queued_jobs:
                self.log.clear()
            self.active_job_id = job_id
            self.active_progress_percent = 0
            self._set_library_runtime_state(job_id, "Running", 0)
            self.tabs.setCurrentIndex(self.library_tab_index)
            self._set_running(True)
            self.worker_thread = QThread(self)
            self.worker = PipelineWorker(
                job_id=job_id,
                args=args,
                config=build_app_config(args, self.project_root),
            )
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.log_line.connect(self._on_worker_log)
            self.worker.progress_changed.connect(self._on_progress)
            self.worker.finished.connect(self._on_worker_finished)
            self.worker.finished.connect(lambda *_: self.worker_thread.quit())
            self.worker_thread.finished.connect(self._worker_thread_stopped)
            self.worker_thread.start()
            return True

        def _on_worker_log(self, job_id: str, line: str) -> None:
            prefix = ""
            entry = self.library_books.get(job_id)
            if entry is not None:
                prefix = f"[{entry.title}] "
            self._append_log(f"{prefix}{line}")

        def _on_progress(self, job_id: str, update: ProgressUpdate) -> None:
            if job_id != self.active_job_id:
                return
            self.active_progress_percent = update.percent
            self.progress.setRange(0, 100)
            self.progress.setValue(update.percent)
            self.progress_status.setText(format_progress_text(update))
            self._set_library_runtime_state(job_id, "Running", update.percent)

        def _on_worker_finished(self, job_id: str, status: str, message: str) -> None:
            current_percent = max(0, self.progress.value())
            self.progress.setRange(0, 100)
            self.progress.setValue(current_percent)
            if status == "completed":
                self.progress.setValue(100)
                self.progress_status.setText("Completed · 100%")
                self._append_log(message)
                self._set_library_runtime_state(job_id, "Complete", 100)
                if not self.queued_jobs:
                    QMessageBox.information(self, "AutoAudio", "Generation finished.")
            elif status == "paused":
                self.progress_status.setText(f"Paused · {current_percent}%")
                self._append_log(f"Paused: {message}")
                self._set_library_runtime_state(job_id, "Paused", current_percent)
                if not self.queued_jobs and not self._closing_after_pause:
                    QMessageBox.information(self, "AutoAudio", "Run paused. Resume state was saved.")
            else:
                self.progress_status.setText(f"Failed · {current_percent}%")
                self._append_log(f"Failed: {message}")
                self._set_library_runtime_state(job_id, "Failed", current_percent)
                QMessageBox.critical(self, "AutoAudio", f"Generation failed:\n{message}")

        def _worker_thread_stopped(self) -> None:
            finished_job_id = self.active_job_id
            self.worker = None
            self.worker_thread = None
            self.active_job_id = None
            self.active_progress_percent = 0
            self._rescan_library()
            if self._closing_after_pause:
                self.queued_jobs.clear()
                self._closing_after_pause = False
                QTimer.singleShot(0, self.close)
                return
            while self.queued_jobs:
                next_job_id, next_args = self.queued_jobs.pop(0)
                entry = self.library_books.get(next_job_id)
                next_item = self.library_items.get(next_job_id)
                if entry is not None and next_item is not None:
                    self.library_tree.setCurrentItem(next_item)
                if self._launch_pipeline(next_args, next_job_id):
                    return
                self._append_log(f"Skipped unavailable queued input: {next_args.input_book}")
            self._set_running(False)
            if finished_job_id == self.current_library_book_id:
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
            self._set_combo(self.speaker_combo, state.get("speaker"))
            self._set_combo(self.model_choice_combo, state.get("model_choice"))
            self._set_combo(self.output_format_combo, state.get("output_format"))
            self._set_combo(self.watermark_device_combo, state.get("watermark_device"))
            self._set_combo(self.comfyui_mode_combo, state.get("comfyui_mode"))
            self._set_combo(self.tts_provider_combo, state.get("tts_provider"))
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
                (self.voice_instruct_edit, "voice_instruct"),
                (self.device_edit, "device"),
                (self.precision_edit, "precision"),
                (self.language_edit, "language"),
                (self.seed_edit, "seed"),
                (self.gutenberg_id_edit, "gutenberg_id"),
                (self.title_edit, "title"),
                (self.author_edit, "author"),
                (self.comfyui_address_edit, "comfyui_server_address"),
                (self.comfyui_timeout_edit, "comfyui_timeout_seconds"),
                (self.tts_base_url_edit, "tts_base_url"),
                (self.tts_api_key_env_edit, "tts_api_key_env"),
                (self.tts_model_edit, "tts_model"),
                (self.tts_response_format_edit, "tts_response_format"),
                (self.tts_language_code_edit, "tts_language_code"),
                (self.provenance_cert_edit, "provenance_cert_path"),
                (self.provenance_key_edit, "provenance_key_path"),
                (self.provenance_tool_edit, "provenance_tool"),
                (self.provenance_claim_edit, "provenance_claim_generator"),
            ):
                self._restore_text(widget, state, key)
            if state.get("tts_voice") is not None:
                self.tts_voice_combo.setEditText(str(state["tts_voice"]))
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
                self._closing_after_pause = True
                self.queued_jobs.clear()
                self._cancel_run()
                event.ignore()
                return
            if self.catalog_thread and self.catalog_thread.isRunning():
                QMessageBox.information(
                    self,
                    "AutoAudio",
                    "Please wait for the current Project Gutenberg request to finish.",
                )
                event.ignore()
                return
            if self.voice_thread and self.voice_thread.isRunning():
                QMessageBox.information(
                    self,
                    "AutoAudio",
                    "Please wait for the explicitly requested voice discovery to finish.",
                )
                event.ignore()
                return
            super().closeEvent(event)

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
