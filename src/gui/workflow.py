"""Job drafts, background planning, bounded samples, and the guided Qt workflow."""

from __future__ import annotations

import copy
import json
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.checkpoint import CheckpointStore
from core.config import AppConfig
from core.library import scan_library
from core.narration_selection import generation_settings, segment_policy_for
from core.pipeline import build_app_config
from core.progress import format_progress_text
from gui.jobs import DraftStore, fresh_output, merge_saved_jobs
from gui.preview import bounded_sample, build_preview, file_identity
from gui.state import load_resume_context
from gui.workers import FunctionThread, TaskReceiver


def fold(title, content):
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QPushButton(f"▸ {title}")
    button.setCheckable(True)
    button.setStyleSheet("text-align: left; padding: 7px;")
    content.setVisible(False)
    button.toggled.connect(content.setVisible)
    button.toggled.connect(
        lambda checked: button.setText(f"{'▾' if checked else '▸'} {title}")
    )
    layout.addWidget(button)
    layout.addWidget(content)
    wrapper.toggle_button = button
    return wrapper


def form_group(title, rows):
    group = QGroupBox(title)
    form = QFormLayout(group)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    for label, widget in rows:
        if label:
            form.addRow(label, widget)
        else:
            form.addRow(widget)
    return group


class WorkflowMixin:
    def _build_workflow_tabs(self, book, narrator, runtime, provenance):
        from gui.window import scrollable

        # Keep the old widget parents alive while reusing their validated bindings.
        self._control_pages = (narrator, runtime)
        for page in self._control_pages:
            page.setParent(self)
            page.hide()
        groups = {group.title(): group for group in book.findChildren(QGroupBox)}
        layout = book.layout()
        while layout.count():
            layout.takeAt(0)
        layout.addWidget(groups["Input / Output"])
        groups["Input / Output"].setTitle("Source book")
        layout.addWidget(groups["Metadata"])
        layout.addWidget(groups["Metadata preview"])
        layout.addWidget(
            fold("Parsing and chapter grouping", groups["Parsing and planning"])
        )
        layout.addWidget(
            fold("Contents and pronunciation replacements", groups["Narration text"])
        )
        layout.addStretch()
        self.book_tab_index = self.tabs.addTab(scrollable(book), "1 · Book")

        voice = QWidget()
        voice_layout = QVBoxLayout(voice)
        voice_layout.addWidget(
            form_group("Speech provider", [("Provider", self.tts_provider_combo)])
        )
        self.comfy_group = form_group(
            "Qwen through ComfyUI",
            [
                ("Server address", self.comfyui_address_edit),
                ("Narrator", self.narrator_profile_combo),
                ("", self.narrator_detail),
                ("Preset speaker", self.speaker_combo),
                ("Language", self.language_edit),
            ],
        )
        voice_layout.addWidget(self.comfy_group)
        self.endpoint_group = form_group(
            "Speech endpoint",
            [
                ("Base URL", self.tts_base_url_edit),
                ("API key environment variable", self.tts_api_key_env_edit),
                ("Model", self.tts_model_edit),
                ("Voice ID", self.tts_voice_combo),
                ("Response format", self.tts_response_format_edit),
            ],
        )
        voice_layout.addWidget(self.endpoint_group)
        self.instruction_group = form_group(
            "Delivery", [("Style instruction", self.voice_instruct_edit)]
        )
        voice_layout.addWidget(self.instruction_group)
        self.eleven_group = form_group(
            "ElevenLabs options",
            [
                ("Language code", self.tts_language_code_edit),
                ("", self.voice_discovery_button),
            ],
        )
        voice_layout.addWidget(self.eleven_group)
        voice_layout.addWidget(self.voice_discovery_status)
        advanced = form_group(
            "Generation settings",
            [
                ("Model size", self.model_choice_combo),
                ("TTS device (external server)", self.device_edit),
                ("Precision", self.precision_edit),
                ("Seed", self.seed_edit),
                ("Maximum new tokens", self.max_new_tokens_spin),
                ("Top-p", self.top_p_spin),
                ("Top-k", self.top_k_spin),
                ("Temperature", self.temperature_spin),
                ("Repetition penalty", self.repetition_penalty_spin),
                ("Attention", self.attention_combo),
                ("", self.unload_model_checkbox),
                ("ComfyUI mode", self.comfyui_mode_combo),
                ("Spoof scenario", self.spoof_scenario_combo),
            ],
        )
        self.qwen_advanced = fold("Advanced Qwen settings", advanced)
        voice_layout.addWidget(self.qwen_advanced)
        voice_layout.addWidget(
            form_group(
                "Request limits", [("Timeout (seconds)", self.comfyui_timeout_edit)]
            )
        )
        voice_layout.addStretch()
        self.narrator_tab_index = self.tabs.addTab(scrollable(voice), "2 · Voice")

        self.preview_tab_index = self.tabs.addTab(
            self._build_preview_page(), "3 · Preview"
        )
        output = QWidget()
        output_layout = QVBoxLayout(output)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.output_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_output_dir)
        path_layout.addWidget(browse)
        output_layout.addWidget(
            form_group(
                "Export",
                [
                    ("Output folder", path_row),
                    ("Format", self.output_format_combo),
                    ("Watermark device", self.watermark_device_combo),
                ],
            )
        )
        device_note = QLabel(
            "Watermarking runs locally. ‘cuda’ uses CUDA or ROCm when supported by this PyTorch build. "
            "TTS device settings belong to the external speech server."
        )
        device_note.setWordWrap(True)
        output_layout.addWidget(device_note)
        spacing = form_group(
            "Silence between audio",
            [
                ("Disclosure gap (ms)", self.disclosure_gap_spin),
                ("Segment gap (ms)", self.segment_gap_spin),
                ("Chapter gap (ms)", self.chapter_gap_spin),
            ],
        )
        output_layout.addWidget(fold("Audio spacing", spacing))
        output_layout.addWidget(fold("Signed provenance (C2PA)", provenance))
        actions = QHBoxLayout()
        self.variant_btn = QPushButton("New variant")
        self.variant_btn.clicked.connect(self._new_variant)
        self.replace_btn = QPushButton("Archive & replace run…")
        self.replace_btn.clicked.connect(self._replace_run)
        self.open_output_btn = QPushButton("Open output folder")
        self.open_output_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(Path(self.output_edit.text()).resolve()))
            )
        )
        for button in (self.variant_btn, self.replace_btn, self.open_output_btn):
            actions.addWidget(button)
        output_layout.addLayout(actions)
        self.run_summary = QLabel()
        self.run_summary.setWordWrap(True)
        output_layout.addWidget(self.run_summary)
        check = QPushButton("Check local setup")
        check.clicked.connect(self._check_setup)
        output_layout.addWidget(check)
        self.setup_status = QLabel()
        self.setup_status.hide()
        self.setup_status.setWordWrap(True)
        output_layout.addWidget(self.setup_status)
        output_layout.addStretch()
        self.runtime_tab_index = self.tabs.addTab(scrollable(output), "4 · Output")
        self.provenance_tab_index = self.runtime_tab_index

    def _build_preview_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QHBoxLayout()
        self.preview_refresh_btn = QPushButton("Refresh narration plan")
        self.preview_refresh_btn.clicked.connect(self._request_preview)
        self.reset_choices_btn = QPushButton("Reset chapter choices")
        self.reset_choices_btn.clicked.connect(self._reset_choices)
        heading.addWidget(self.preview_refresh_btn)
        heading.addWidget(self.reset_choices_btn)
        heading.addStretch()
        layout.addLayout(heading)
        self.preview_status = QLabel(
            "Select a book to prepare its local narration plan."
        )
        self.preview_status.setWordWrap(True)
        layout.addWidget(self.preview_status)
        splitter = QSplitter()
        self.chapter_tree = QTreeWidget()
        self.chapter_tree.setHeaderLabels(("Include / chapter", "Segments"))
        self.chapter_tree.setRootIsDecorated(False)
        from PySide6.QtWidgets import QHeaderView

        self.chapter_tree.header().setStretchLastSection(False)
        self.chapter_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.chapter_tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.chapter_tree.itemSelectionChanged.connect(self._select_preview_chapter)
        self.chapter_tree.itemChanged.connect(self._chapter_checked)
        splitter.addWidget(self.chapter_tree)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Spoken chapter text · after replacements"))
        self.narration_editor = QTextEdit()
        self.narration_editor.setAcceptRichText(False)
        self.narration_editor.textChanged.connect(self._editor_changed)
        right_layout.addWidget(self.narration_editor, 1)
        edit_controls = QHBoxLayout()
        self.apply_edit_btn = QPushButton("Apply chapter edit")
        self.apply_edit_btn.clicked.connect(self._apply_chapter_edit)
        self.revert_edit_btn = QPushButton("Use parsed text")
        self.revert_edit_btn.clicked.connect(self._revert_chapter_edit)
        edit_controls.addWidget(self.apply_edit_btn)
        edit_controls.addWidget(self.revert_edit_btn)
        right_layout.addLayout(edit_controls)
        self.segment_tree = QTreeWidget()
        self.segment_tree.setHeaderLabels(("Segment", "Exact request text"))
        self.segment_tree.setRootIsDecorated(False)
        self.segment_tree.setMaximumHeight(120)
        self.segment_tree.itemSelectionChanged.connect(self._select_preview_segment)
        right_layout.addWidget(self.segment_tree)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self.preview_notes = QTextEdit()
        self.preview_notes.setReadOnly(True)
        self.preview_notes.setMaximumHeight(120)
        layout.addWidget(fold("Omissions and parser diagnostics", self.preview_notes))
        saved_panel = QWidget()
        saved_layout = QVBoxLayout(saved_panel)
        self.saved_plan_text = QTextEdit()
        self.saved_plan_text.setReadOnly(True)
        self.saved_plan_text.setMaximumHeight(170)
        saved_layout.addWidget(self.saved_plan_text)
        self.restore_run_btn = QPushButton("Restore saved run settings")
        self.restore_run_btn.clicked.connect(self._restore_saved_run)
        saved_layout.addWidget(self.restore_run_btn)
        self.saved_plan_fold = fold("Saved plan for Resume", saved_panel)
        self.saved_plan_fold.hide()
        layout.addWidget(self.saved_plan_fold)
        self.sample_text_label = QLabel(
            "Sample uses at most 50 words / 180 characters from the selected segment, plus the audible disclosure."
        )
        self.sample_text_label.setWordWrap(True)
        layout.addWidget(self.sample_text_label)
        sample_controls = QHBoxLayout()
        self.sample_btn = QPushButton("Generate short sample")
        self.sample_btn.clicked.connect(self._generate_sample)
        self.play_sample_btn = QPushButton("Play sample")
        self.play_sample_btn.setEnabled(False)
        self.play_sample_btn.clicked.connect(self._play_sample)
        self.stop_sample_btn = QPushButton("Stop playback")
        self.stop_sample_btn.setEnabled(False)
        self.stop_sample_btn.clicked.connect(self._stop_sample)
        sample_controls.addWidget(self.sample_btn)
        sample_controls.addWidget(self.play_sample_btn)
        sample_controls.addWidget(self.stop_sample_btn)
        sample_controls.addStretch()
        layout.addLayout(sample_controls)
        self.sample_status = QLabel("No sample generated for these settings.")
        self.sample_status.setWordWrap(True)
        layout.addWidget(self.sample_status)
        return page

    def _initialize_workflow(self, root_layout):
        self.resize(1160, 900)
        self.current_job_id = None
        self._restoring = True
        self._threads = {}
        self._task_serial = {}
        self._pending_tasks = {}
        self._scan_cache = {}
        self._preview_cache = {}
        self._preview_result = None
        self._preview_key = None
        self._editor_dirty = False
        self._excluded_chapters = []
        self._chapter_text_overrides = []
        self._pending_edits = {}
        self._queue_armed = False
        self._sample_path = None
        self._sample_key = None
        self._player = None
        self._phase = None
        self._phase_started = 0.0
        self._phase_times = {}
        self._log_offset = 0
        self._active_args = None
        self._log_path = None
        self.drafts = DraftStore(
            self.project_root / "audiobook_output" / ".gui" / "drafts.json"
        )
        if self.drafts.locations.get("books_dir"):
            self.books_dir_edit.setText(self.drafts.locations["books_dir"])
        if self.drafts.locations.get("output_root"):
            self.library_output_root_edit.setText(self.drafts.locations["output_root"])
        self._default_draft = self._capture_controls()

        self.validation_status = QLabel()
        self.validation_status.setWordWrap(True)
        root_layout.insertWidget(
            root_layout.indexOf(self.progress), self.validation_status
        )
        self.active_title = QLabel("No active job")
        self.active_title.setWordWrap(True)
        self.progress_status.setWordWrap(True)
        for label in (
            self.meta_title,
            self.meta_author,
            self.meta_language,
            self.catalog_status,
        ):
            label.setWordWrap(True)
        self.active_title.setStyleSheet("font-weight: 600;")
        root_layout.insertWidget(root_layout.indexOf(self.progress), self.active_title)
        self.phase_status = QLabel()
        self.phase_status.setWordWrap(True)
        root_layout.insertWidget(root_layout.indexOf(self.log), self.phase_status)
        root_layout.removeWidget(self.log)
        self.log.setMinimumHeight(110)
        self.log.setMaximumHeight(200)
        self.log.document().setMaximumBlockCount(5000)
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        search_row = QHBoxLayout()
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("Find in log")
        find = QPushButton("Find next")
        find.clicked.connect(self._find_log)
        self.log_search.returnPressed.connect(self._find_log)
        copy_button = QPushButton("Copy log")
        copy_button.clicked.connect(lambda: self._copy_log())
        search_row.addWidget(self.log_search, 1)
        search_row.addWidget(find)
        search_row.addWidget(copy_button)
        log_layout.addLayout(search_row)
        log_layout.addWidget(self.log)
        self.log_fold = fold("Run log and failure details", log_panel)
        root_layout.addWidget(self.log_fold)

        queue_group = QGroupBox("Conversion queue")
        queue_layout = QVBoxLayout(queue_group)
        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(95)
        queue_layout.addWidget(self.queue_list)
        queue_controls = QHBoxLayout()
        self.queue_add_btn = QPushButton("Add current job")
        self.queue_add_btn.clicked.connect(self._enqueue_current)
        self.queue_run_btn = QPushButton("Run queue")
        self.queue_run_btn.clicked.connect(self._run_queue)
        self.queue_remove_btn = QPushButton("Remove")
        self.queue_remove_btn.clicked.connect(self._remove_queued)
        up = QPushButton("Move up")
        up.clicked.connect(lambda: self._move_queued(-1))
        down = QPushButton("Move down")
        down.clicked.connect(lambda: self._move_queued(1))
        for button in (
            self.queue_add_btn,
            self.queue_run_btn,
            self.queue_remove_btn,
            up,
            down,
        ):
            queue_controls.addWidget(button)
        queue_layout.addLayout(queue_controls)
        self.tabs.widget(self.library_tab_index).layout().addWidget(queue_group)
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(350)
        self._draft_timer.timeout.connect(self._save_current_draft)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(450)
        self._preview_timer.timeout.connect(self._request_preview)
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(300)
        self._log_timer.timeout.connect(self._tail_log)
        self._log_timer.start()
        for name, widget in self._draft_controls().items():
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._controls_changed)
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self._controls_changed)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._controls_changed)
            else:
                widget.valueChanged.connect(self._controls_changed)
        self.replacement_table.itemChanged.connect(self._controls_changed)
        self.replacement_table.model().rowsInserted.connect(self._controls_changed)
        self.replacement_table.model().rowsRemoved.connect(self._controls_changed)
        self._restoring = False
        self._on_tts_provider_changed(0)
        selected = self.drafts.selected
        if selected in self.drafts.jobs:
            self._select_job(selected)
        self._rescan_library()
        self._refresh_action_controls()
        if self.drafts.error:
            self._append_log(self.drafts.error)
            self.log_fold.toggle_button.setChecked(True)

    def _draft_controls(self):
        excluded = {
            "books_dir_edit",
            "library_output_root_edit",
            "catalog_search_edit",
            "catalog_language_combo",
            "log_search",
            "provenance_password_edit",
        }
        return {
            name: widget
            for name, widget in vars(self).items()
            if name not in excluded
            and isinstance(
                widget, (QLineEdit, QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox)
            )
        }

    def _capture_controls(self):
        state = {}
        for name, widget in self._draft_controls().items():
            if isinstance(widget, QLineEdit):
                state[name] = widget.text()
            elif isinstance(widget, QComboBox):
                state[name] = (
                    self._selected_tts_voice()
                    if name == "tts_voice_combo"
                    else widget.currentText()
                )
            elif isinstance(widget, QCheckBox):
                state[name] = widget.isChecked()
            else:
                state[name] = widget.value()
        state["replacement_rules"] = self._collect_replacement_rules()
        state["excluded_chapters"] = list(self._excluded_chapters)
        state["chapter_text_overrides"] = copy.deepcopy(self._chapter_text_overrides)
        state["pending_edits"] = dict(self._pending_edits)
        return state

    def _restore_controls(self, state):
        self._restoring = True
        try:
            if "narrator_profile_combo" in state:
                self.narrator_profile_combo.setCurrentText(
                    state["narrator_profile_combo"]
                )
            for name, widget in self._draft_controls().items():
                if name not in state:
                    continue
                value = state[name]
                blocker = QSignalBlocker(widget)
                if isinstance(widget, QLineEdit):
                    widget.setText(str(value))
                elif isinstance(widget, QComboBox):
                    widget.setCurrentText(str(value))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                else:
                    widget.setValue(value)
                del blocker
            self._restore_replacement_rules(state.get("replacement_rules", []))
            self._excluded_chapters = list(state.get("excluded_chapters", []))
            self._chapter_text_overrides = copy.deepcopy(
                state.get("chapter_text_overrides", [])
            )
            self._pending_edits = dict(state.get("pending_edits", {}))
            self.provenance_password_edit.clear()
            self._on_tts_provider_changed(0)
        finally:
            self._restoring = False

    def _save_current_draft(self):
        if self._restoring or not self.current_job_id:
            return
        try:
            if (
                self._editor_dirty
                and getattr(self, "_editor_chapter", None) is not None
            ):
                self._pending_edits[str(self._editor_chapter)] = (
                    self.narration_editor.toPlainText()
                )
            state = self._capture_controls()
            previous = self.drafts.jobs.get(self.current_job_id, {})
            if previous.get("archived"):
                state["archived"] = True
            if previous.get("input_edit"):
                # A partially typed source path is committed by editingFinished.
                state["input_edit"] = previous["input_edit"]
            self.drafts.jobs[self.current_job_id] = state
            self.drafts.selected = self.current_job_id
            self.drafts.save()
        except (OSError, ValueError) as exc:
            self._append_log(f"Could not save draft: {exc}")

    def _controls_changed(self, *_):
        if self._restoring:
            return
        self._preview_result = None
        self._preview_key = None
        self.preview_status.setText(
            "Settings changed · preparing an updated narration plan…"
        )
        self._stop_sample()
        self.play_sample_btn.setEnabled(False)
        self.sample_status.setText(
            "Settings changed. Generate a new sample to hear this draft."
        )
        self._draft_timer.start()
        self._preview_timer.start()
        self._refresh_action_controls()

    def _select_job(self, job_id):
        if job_id == self.current_job_id:
            return
        self._save_current_draft()
        self.current_job_id = job_id
        self.current_library_book_id = (
            job_id
            if job_id in self.library_books
            and not job_id.startswith(("manual:", "variant:"))
            else None
        )
        state = self.drafts.jobs.get(job_id)
        if state is None:
            entry = self.library_books[job_id]
            state = copy.deepcopy(self._default_draft)
            state["input_edit"] = str(entry.source_path)
            state["output_edit"] = str(entry.output_dir)
            self._restore_controls(state)
            self._prepopulate_from_checkpoint()
        else:
            self._restore_controls(state)
        self._editor_dirty = False
        self._controls_changed()
        self._save_current_draft()

    def _on_input_changed(self, file_path):
        if self._restoring:
            return
        if not file_path or not Path(file_path).is_file():
            self._controls_changed()
            return
        resolved = Path(file_path).resolve()
        current_source = self.drafts.jobs.get(self.current_job_id, {}).get("input_edit")
        if current_source and Path(current_source).resolve() == resolved:
            self._refresh_action_controls()
            return
        # editingFinished arrives after the line edit changed; preserve the old job's source.
        if self.current_job_id and current_source:
            new_text = self.input_edit.text()
            blocker = QSignalBlocker(self.input_edit)
            self.input_edit.setText(current_source)
            self._save_current_draft()
            self.input_edit.setText(new_text)
            del blocker
        matching = next(
            (
                key
                for key, entry in self.library_books.items()
                if entry.source_path == resolved
            ),
            None,
        )
        job_id = matching or f"manual:{resolved}"
        self.current_job_id = None
        if job_id not in self.drafts.jobs and not matching:
            state = copy.deepcopy(self._default_draft)
            state["input_edit"] = str(resolved)
            state["output_edit"] = str(
                fresh_output(self.library_output_root_edit.text(), resolved)
            )
            self.drafts.jobs[job_id] = state
        self._select_job(job_id)
        self._rescan_library()

    def _rescan_library(self, select_path=None):
        if not hasattr(self, "_threads") or self._closing_after_pause:
            return
        if isinstance(select_path, bool):
            select_path = None
        self._save_current_draft()
        books_dir = Path(
            self.books_dir_edit.text().strip() or self.project_root / "books"
        )
        output_root = Path(
            self.library_output_root_edit.text().strip()
            or self.project_root / "audiobook_output"
        )
        self.library_summary.setText("Scanning local books in the background…")
        self.drafts.locations = {
            "books_dir": str(books_dir),
            "output_root": str(output_root),
        }
        try:
            self.drafts.save()
        except OSError as exc:
            self._append_log(f"Could not save library locations: {exc}")

        def complete(entries):
            self.library_books = {entry.id: entry for entry in entries}
            self._render_library(select_path)

        states = copy.deepcopy(self.drafts.jobs)

        def scan():
            entries = scan_library(books_dir, output_root, cache=self._scan_cache)
            if len(self._scan_cache) > 5000:
                self._scan_cache.clear()
            return merge_saved_jobs(entries, states, self._scan_cache)

        self._background(
            "scan",
            scan,
            complete,
            lambda error: self.library_summary.setText(f"Scan failed: {error}"),
        )

    def _render_library(self, select_path=None):
        blocker = QSignalBlocker(self.library_tree)
        self.library_tree.clear()
        self.library_items.clear()
        self.library_progress_bars.clear()
        selected = self.current_job_id
        for entry in self.library_books.values():
            status = (
                "Running"
                if entry.id == self.active_job_id
                else "Queued"
                if entry.id in self._queued_book_ids()
                else entry.status
            )
            percent = (
                self.active_progress_percent
                if entry.id == self.active_job_id
                else entry.progress_percent
            )
            item = QTreeWidgetItem((entry.title, entry.author, status, f"{percent}%"))
            item.setData(0, Qt.ItemDataRole.UserRole, entry.id)
            item.setToolTip(0, f"{entry.source_path}\n{entry.output_dir}")
            self.library_tree.addTopLevelItem(item)
            self.library_items[entry.id] = item
            if select_path and entry.source_path == Path(select_path).resolve():
                selected = entry.id
        self.library_tree.resizeColumnToContents(0)
        if selected in self.library_items:
            self.library_tree.setCurrentItem(self.library_items[selected])
        del blocker
        self.library_summary.setText(f"{len(self.library_books)} books and saved runs")
        if not self.current_job_id and self.library_books:
            selected = (
                selected
                if selected in self.library_books
                else next(iter(self.library_books))
            )
        if selected and selected != self.current_job_id:
            self._select_job(selected)
        self._refresh_action_controls()

    def _on_library_selection_changed(self):
        selected = self.library_tree.selectedItems()
        if selected:
            self._select_job(selected[0].data(0, Qt.ItemDataRole.UserRole))

    def _background(self, name, function, callback, failed=None):
        serial = self._task_serial.get(name, 0) + 1
        self._task_serial[name] = serial
        if name in self._threads:
            self._pending_tasks[name] = (function, callback, failed)
            return
        thread = FunctionThread(self, function)

        def stopped():
            thread.wait()
            self._threads.pop(name, None)
            thread.deleteLater()
            pending = self._pending_tasks.pop(name, None)
            if self._closing_after_pause:
                QTimer.singleShot(0, self.close)
            elif pending:
                self._background(name, *pending)

        receiver = TaskReceiver(
            self,
            lambda result: (
                callback(result)
                if self._task_serial.get(name) == serial
                and not self._closing_after_pause
                else None
            ),
            lambda error: (
                (failed(error) if failed else self._append_log(error))
                if self._task_serial.get(name) == serial
                else None
            ),
            stopped,
        )
        self._threads[name] = (thread, receiver)
        thread.result.connect(receiver.on_result)
        thread.failed.connect(receiver.on_failed)
        thread.finished.connect(receiver.on_stopped)
        thread.start()

    def _settings_key(self):
        args = self._collect_args(resume_mode="no")
        payload = vars(args).copy()
        payload.pop("provenance_key_password", None)
        rules_identity = (
            file_identity(args.replacement_file) if args.replacement_file else None
        )
        checkpoint_path = CheckpointStore(AppConfig.state_dir_for(args.output_dir)).path
        checkpoint_identity = (
            file_identity(checkpoint_path) if checkpoint_path.is_file() else None
        )
        return (
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
            file_identity(args.input_book),
            rules_identity,
            checkpoint_identity,
        )

    def _request_preview(self, *_):
        if self._restoring or self._closing_after_pause:
            return
        source = self.input_edit.text().strip()
        saved_source = self.drafts.jobs.get(self.current_job_id, {}).get("input_edit")
        if (
            source
            and Path(source).is_file()
            and (
                not saved_source
                or Path(saved_source).resolve() != Path(source).resolve()
            )
        ):
            self._on_input_changed(source)
            return
        try:
            args = copy.deepcopy(self._collect_args(resume_mode="no"))
            key = self._settings_key()
            profile = self.narrator_catalog.get(args.narrator_profile)
        except (ValueError, OSError) as exc:
            self.preview_status.setText(str(exc))
            return
        if (
            self._preview_result is not None
            and self._preview_key == key
            and not self._editor_dirty
        ):
            return
        # Keep unsaved text in the draft; never replace it with a background result.
        if self._editor_dirty:
            self.preview_status.setText(
                "Apply the chapter edit or use parsed text before refreshing."
            )
            return
        self.preview_status.setText("Preparing narration plan locally…")

        def complete(result):
            try:
                if key != self._settings_key():
                    return
            except (ValueError, OSError):
                return
            self._preview_result = result
            self._preview_key = key
            selected = self._selected_chapter_number()
            blocker = QSignalBlocker(self.chapter_tree)
            self.chapter_tree.clear()
            for chapter in result.plan.chapters:
                item = QTreeWidgetItem(
                    (
                        f"{chapter.index + 1}. {chapter.title}",
                        str(len(chapter.segments)),
                    )
                )
                item.setData(0, Qt.ItemDataRole.UserRole, chapter.index + 1)
                item.setToolTip(0, chapter.title)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    0,
                    Qt.CheckState.Unchecked
                    if chapter.skipped_reason
                    else Qt.CheckState.Checked,
                )
                self.chapter_tree.addTopLevelItem(item)
                if selected == chapter.index + 1:
                    self.chapter_tree.setCurrentItem(item)
            if self.chapter_tree.currentItem() is None:
                self.chapter_tree.setCurrentItem(self.chapter_tree.topLevelItem(0))
            del blocker
            self._select_preview_chapter()
            metadata = result.metadata
            self.meta_title.setText(args.title or metadata.title or "—")
            self.meta_author.setText(args.author or metadata.author or "—")
            self.meta_language.setText(metadata.language or "—")
            active = [c for c in result.plan.chapters if not c.skipped_reason]
            self.preview_status.setText(
                f"{len(active)} included chapters · {sum(len(c.segments) for c in active)} speech segments · "
                f"{len(result.plan.chapters) - len(active)} excluded. Select a chapter to review or edit."
            )
            self.preview_notes.setPlainText("\n\n".join(result.notes))
            self.saved_plan_fold.setVisible(result.saved_plan is not None)
            if result.saved_plan is not None:
                lines = [
                    "This frozen text is used by Resume. The editable plan above describes a new run.",
                    "Input, settings, workflow, and artifact compatibility are checked again when resuming.",
                ]
                for chapter in result.saved_plan.chapters:
                    lines.append(f"\n{chapter.index + 1}. {chapter.title}")
                    if chapter.skipped_reason:
                        lines.append(chapter.skipped_reason)
                    for segment in chapter.segments:
                        lines.append(f"\nSegment {segment.index + 1}\n{segment.text}")
                self.saved_plan_text.setPlainText("\n".join(lines))
            self._refresh_action_controls()

        self._background(
            "preview",
            lambda: build_preview(args, profile, self._preview_cache),
            complete,
            lambda error: self.preview_status.setText(
                f"Cannot build narration plan: {error}"
            ),
        )

    def _selected_chapter_number(self):
        item = self.chapter_tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _select_preview_chapter(self):
        if self._editor_dirty:
            # Preserve the uncommitted text with its original chapter before changing selection.
            self._save_current_draft()
        number = self._selected_chapter_number()
        if number is None or self._preview_result is None:
            return
        self._editor_dirty = False
        blocker = QSignalBlocker(self.narration_editor)
        pending = self._pending_edits.get(str(number))
        self.narration_editor.setPlainText(
            pending
            if pending is not None
            else self._preview_result.chapters[number - 1][1]
        )
        del blocker
        self._editor_chapter = number
        self.segment_tree.clear()
        for segment in self._preview_result.plan.chapters[number - 1].segments:
            item = QTreeWidgetItem((str(segment.index + 1), segment.text))
            item.setToolTip(1, segment.text)
            self.segment_tree.addTopLevelItem(item)
        if self.segment_tree.topLevelItemCount():
            self.segment_tree.setCurrentItem(self.segment_tree.topLevelItem(0))
        self._editor_dirty = pending is not None
        self.apply_edit_btn.setEnabled(self._editor_dirty)
        self._refresh_action_controls()

    def _select_preview_segment(self):
        item = self.segment_tree.currentItem()
        text = bounded_sample(item.text(1)) if item else ""
        self.sample_text_label.setText(
            f"Sample text (up to 50 words / 180 characters, plus disclosure):\n{text}"
            if text
            else "Select an included chapter and segment for a bounded sample."
        )
        if text != getattr(self, "_sample_source_text", None):
            self._sample_key = None
            self.play_sample_btn.setEnabled(False)

    def _editor_changed(self):
        if self._restoring or not self._preview_result:
            return
        self._editor_dirty = True
        self.apply_edit_btn.setEnabled(True)
        self._draft_timer.start()
        self._refresh_action_controls()

    def _apply_chapter_edit(self):
        number = getattr(self, "_editor_chapter", self._selected_chapter_number())
        text = self.narration_editor.toPlainText().strip()
        if not number or not text:
            self.preview_status.setText(
                "An edited chapter must contain spoken text. Uncheck it to exclude it."
            )
            return
        self._chapter_text_overrides = [
            item for item in self._chapter_text_overrides if item["chapter"] != number
        ]
        self._chapter_text_overrides.append({"chapter": number, "text": text})
        self._pending_edits.pop(str(number), None)
        self._editor_dirty = False
        self._save_current_draft()
        self._controls_changed()

    def _revert_chapter_edit(self):
        number = getattr(self, "_editor_chapter", self._selected_chapter_number())
        self._chapter_text_overrides = [
            item for item in self._chapter_text_overrides if item["chapter"] != number
        ]
        self._pending_edits.pop(str(number), None)
        self._editor_dirty = False
        self._save_current_draft()
        self._controls_changed()

    def _chapter_checked(self, item, column):
        number = item.data(0, Qt.ItemDataRole.UserRole)
        excluded = set(self._excluded_chapters)
        if item.checkState(0) == Qt.CheckState.Checked:
            excluded.discard(number)
        else:
            excluded.add(number)
        self._excluded_chapters = sorted(excluded)
        self._controls_changed()

    def _reset_choices(self):
        self._excluded_chapters = []
        self._chapter_text_overrides = []
        self._pending_edits.clear()
        self._editor_dirty = False
        self._save_current_draft()
        self._controls_changed()

    def _validated_args(self, resume_mode):
        if (
            not self.input_edit.text().strip()
            or not Path(self.input_edit.text().strip()).is_file()
        ):
            raise ValueError("Select an existing source book.")
        if not self.output_edit.text().strip():
            raise ValueError("Choose an output folder.")
        if self._editor_dirty:
            raise ValueError(
                "Apply the chapter edit or use parsed text before generating audio."
            )
        if self._pending_edits:
            raise ValueError(
                "Apply or discard the saved chapter edits before generating audio."
            )
        args = self._collect_args(resume_mode=resume_mode)
        config = build_app_config(args, self.project_root)
        settings = generation_settings(
            args, self.narrator_catalog.get(args.narrator_profile)
        )
        segment_policy_for(args, settings)
        if args.tts_provider != "comfyui":
            if not args.tts_voice.strip():
                raise ValueError("Enter a voice ID for the selected provider.")
            if args.tts_provider == "openai-compatible" and not args.tts_model.strip():
                raise ValueError("Enter an endpoint model for the selected provider.")
            if args.tts_provider == "elevenlabs" and not os.environ.get(
                args.tts_api_key_env
            ):
                raise ValueError(
                    f"Set the API key in {args.tts_api_key_env} before generating speech."
                )
        if args.tts_provider == "comfyui" and not args.comfyui_server_address.strip():
            raise ValueError("Enter a ComfyUI server address.")
        if args.tts_provider == "comfyui":
            from urllib.parse import urlparse

            endpoint = args.comfyui_server_address.strip()
            parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
            if (
                parsed.scheme not in {"http", "https", "ws", "wss"}
                or not parsed.hostname
            ):
                raise ValueError("Enter a valid ComfyUI host and port or HTTP URL.")
            if (
                parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError(
                    "ComfyUI address must contain only a host and port, without credentials or a path."
                )
            try:
                parsed.port
            except ValueError as exc:
                raise ValueError("ComfyUI port is invalid.") from exc
        if (
            args.comfyui_timeout_seconds is not None
            and args.comfyui_timeout_seconds <= 0
        ):
            raise ValueError("TTS timeout must be greater than zero.")
        if self._preview_result is None or self._preview_key != self._settings_key():
            raise ValueError(
                "Wait for the current narration plan. Check Preview for parsing errors."
            )
        return args, config

    def _restore_saved_run(self):
        self._editor_dirty = False
        self._pending_edits.clear()
        self._prepopulate_from_checkpoint()
        self._save_current_draft()
        self._request_preview()

    def _refresh_action_controls(self):
        if not hasattr(self, "validation_status"):
            return
        try:
            args, config = self._validated_args("no")
            error = ""
        except (ValueError, OSError) as exc:
            args = None
            error = str(exc)
        busy = (
            self.current_job_id == self.active_job_id
            if self.current_job_id is not None
            else False
        )
        busy = busy or self.current_job_id in self._queued_book_ids()
        self.validation_status.setText(error)
        self.validation_status.setStyleSheet("color: #bf562b;" if error else "")
        context = (
            load_resume_context(
                CheckpointStore(AppConfig.state_dir_for(self.output_edit.text()))
            )
            if self.output_edit.text()
            else None
        )
        if (
            context
            and Path(context.ui_state.get("input_book", "")).resolve()
            != Path(self.input_edit.text()).resolve()
        ):
            context = None
        self.resume_available = context is not None
        self.start_btn.setText(
            "Convert book" if self.active_job_id is None else "Queue conversion"
        )
        self.start_btn.setEnabled(not error and not busy)
        self.resume_btn.setText(
            "Resume" if self.active_job_id is None else "Queue resume"
        )
        self.resume_btn.setEnabled(not error and not busy and self.resume_available)
        self.queue_add_btn.setEnabled(not error and not busy)
        self.queue_run_btn.setEnabled(
            bool(self.queued_jobs) and self.active_job_id is None
        )
        self.cancel_btn.setEnabled(
            self.active_job_id is not None
            and self.worker is not None
            and not self.worker.cancellation.is_cancelled
        )
        self.sample_btn.setEnabled(
            not error
            and self.active_job_id is None
            and self._preview_result is not None
            and bool(self.segment_tree.currentItem())
        )
        self.variant_btn.setEnabled(bool(self.input_edit.text()) and not busy)
        self.replace_btn.setEnabled(
            bool(
                context
                or CheckpointStore(
                    AppConfig.state_dir_for(self.output_edit.text())
                ).path.exists()
            )
            and not busy
        )
        if args:
            voice = (
                (
                    args.speaker
                    if settings_mode(self) == "preset"
                    else args.voice_instruct
                )
                if args.tts_provider == "comfyui"
                else args.tts_voice
            )
            action = "Resume saved work" if context else "New conversion"
            self.run_summary.setText(
                f"{action} · {args.tts_provider} · {voice or 'voice design'} · {args.language or 'profile language'}\n"
                f"{args.output_format.upper()} → {Path(args.output_dir).resolve()}\n"
                "Existing incompatible output is protected; use New variant or Archive & replace run."
            )

    def _enqueue_current(self, _checked=False, *, resume_mode=None):
        try:
            args, _ = self._validated_args(
                resume_mode or ("yes" if self.resume_available else "no")
            )
        except ValueError as exc:
            self.validation_status.setText(str(exc))
            return
        self._save_current_draft()
        job_id = self.current_job_id or f"manual:{Path(args.input_book).resolve()}"
        if job_id == self.active_job_id or job_id in self._queued_book_ids():
            return
        destinations = [Path(a.output_dir).resolve() for _, a in self.queued_jobs]
        if self._active_args:
            destinations.append(Path(self._active_args.output_dir).resolve())
        if Path(args.output_dir).resolve() in destinations:
            self.validation_status.setText(
                "Another active or queued job owns this output folder. Choose a new variant."
            )
            return
        self.queued_jobs.append((job_id, copy.deepcopy(args)))
        self.queued_jobs[-1][1]._gui_source_identity = file_identity(args.input_book)
        self._render_queue()

    def _render_queue(self):
        selected = self.queue_list.currentRow()
        self.queue_list.clear()
        for n, (_, args) in enumerate(self.queued_jobs, 1):
            voice = args.speaker if args.tts_provider == "comfyui" else args.tts_voice
            self.queue_list.addItem(
                f"{n}. {args.title or Path(args.input_book).stem} · {args.tts_provider} / {voice} · {args.output_format} → {args.output_dir}"
            )
        self.queue_list.setCurrentRow(min(selected, len(self.queued_jobs) - 1))
        self._refresh_action_controls()
        for key in self._queued_book_ids():
            self._set_library_runtime_state(key, "Queued")

    def _remove_queued(self):
        row = self.queue_list.currentRow()
        if 0 <= row < len(self.queued_jobs):
            self.queued_jobs.pop(row)
            self._render_queue()
            self._rescan_library()

    def _move_queued(self, delta):
        row = self.queue_list.currentRow()
        other = row + delta
        if 0 <= row < len(self.queued_jobs) and 0 <= other < len(self.queued_jobs):
            self.queued_jobs[row], self.queued_jobs[other] = (
                self.queued_jobs[other],
                self.queued_jobs[row],
            )
            self._render_queue()
            self.queue_list.setCurrentRow(other)

    def _run_queue(self):
        self._queue_armed = True
        self._dispatch_queue()

    def _dispatch_queue(self):
        if self.active_job_id is not None or not self._queue_armed:
            return
        if self.queued_jobs:
            job_id, args = self.queued_jobs.pop(0)
            self._render_queue()
            if not self._launch_pipeline(args, job_id):
                self._queue_armed = False
                self._append_log("Queue stopped because the next job could not start.")
        else:
            self._queue_armed = False

    def _launch_from_ui(self, resume_mode):
        self._on_input_changed(self.input_edit.text())
        try:
            args, _ = self._validated_args(resume_mode)
        except ValueError as exc:
            self.validation_status.setText(str(exc))
            self._request_preview()
            return
        self._save_current_draft()
        if self.active_job_id is not None:
            self._enqueue_current(resume_mode=resume_mode)
            self._queue_armed = True
        else:
            args._gui_source_identity = file_identity(args.input_book)
            self._launch_pipeline(
                copy.deepcopy(args),
                self.current_job_id or f"manual:{Path(args.input_book).resolve()}",
            )

    def _new_variant(self):
        if not Path(self.input_edit.text()).is_file():
            return
        self._save_current_draft()
        state = self._capture_controls()
        state["output_edit"] = str(
            fresh_output(self.library_output_root_edit.text(), self.input_edit.text())
        )
        state.pop("archived", None)
        job_id = f"variant:{uuid4().hex}"
        self.drafts.jobs[job_id] = state
        self._select_job(job_id)
        self._rescan_library()
        self.tabs.setCurrentIndex(self.runtime_tab_index)

    def _replace_run(self):
        if (
            self.current_job_id == self.active_job_id
            or self.current_job_id in self._queued_book_ids()
        ):
            return
        old_output = self.output_edit.text()
        new_output = fresh_output(
            self.library_output_root_edit.text(), self.input_edit.text()
        )
        decision = QMessageBox.question(
            self,
            "Archive and replace run",
            f"Keep the existing run as an archived library entry at:\n{old_output}\n\n"
            f"Prepare a fresh run at:\n{new_output}\n\nNo existing files will be moved or deleted. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if decision != QMessageBox.StandardButton.Yes:
            return
        self._save_current_draft()
        state = self._capture_controls()
        archive = copy.deepcopy(state)
        archive["archived"] = True
        self.drafts.jobs[f"variant:{uuid4().hex}"] = archive
        state["output_edit"] = str(new_output)
        self._restore_controls(state)
        self._controls_changed()
        self._save_current_draft()
        self._rescan_library()

    def _generate_sample(self):
        try:
            args, _ = self._validated_args("no")
            key = self._settings_key()
            if self._preview_key != key or self._preview_result is None:
                raise ValueError(
                    "Wait for the current narration preview before generating a sample."
                )
            item = self.segment_tree.currentItem()
            text = bounded_sample(item.text(1) if item else "")
            if not text:
                raise ValueError("Select an included speech segment.")
            if len(text) <= 20:
                raise ValueError(
                    "The text adapter omits excerpts of 20 characters or fewer. Select a longer segment for this sample."
                )
        except (ValueError, OSError) as exc:
            self.sample_status.setText(str(exc))
            return
        sample_root = self.project_root / "audiobook_output" / ".samples" / uuid4().hex
        sample_root.mkdir(parents=True)
        source = sample_root / "excerpt.txt"
        source.write_text(text, encoding="utf-8")
        args.input_book = str(source)
        args.output_dir = str(sample_root / "audio")
        args.source_mode = "text"
        args.fetch_metadata = False
        args.narrate_toc = True
        args.replacement_file = ""
        args.replacement_rules = []
        args.excluded_chapters = []
        args.chapter_text_overrides = []
        args.target_words_per_chapter = 1000
        args.min_paragraphs_per_chapter = 1
        args.target_words_per_segment = 100
        args.max_words_per_segment = 220
        args.output_format = "flac"
        args.title = f"Sample · {self.meta_title.text()}"
        self._sample_key = key
        self._sample_source_text = text
        self._sample_path = None
        self.play_sample_btn.setEnabled(False)
        self.sample_status.setText(
            "Generating disclosure and one bounded narration segment…"
        )
        self._launch_pipeline(args, f"sample:{uuid4().hex}")

    def _play_sample(self):
        if not self._sample_path or not Path(self._sample_path).is_file():
            return
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            if self._player is None:
                self._player = QMediaPlayer(self)
                self._audio_output = QAudioOutput(self)
                self._player.setAudioOutput(self._audio_output)
                self._player.errorOccurred.connect(
                    lambda *_: self.sample_status.setText(
                        f"Playback error: {self._player.errorString()}"
                    )
                )
            self._player.setSource(QUrl.fromLocalFile(self._sample_path))
            self._player.play()
            self.stop_sample_btn.setEnabled(True)
        except ImportError:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._sample_path))

    def _stop_sample(self):
        if self._player:
            self._player.stop()
        self.stop_sample_btn.setEnabled(False)

    def _check_setup(self):
        self.setup_status.show()
        try:
            args, _ = self._validated_args("no")
        except ValueError as exc:
            self.setup_status.setText(str(exc))
            return

        def check():
            import torch

            tools = ", ".join(
                f"{tool}: {'found' if shutil.which(tool) else 'MISSING'}"
                for tool in ("ffmpeg", "ffprobe")
            )
            gpu = "ROCm" if torch.version.hip else "CUDA"
            available = torch.cuda.is_available()
            from provenance.audio_watermark import resolve_audioseal_device

            requested = (
                os.environ.get("AUTOAUDIO_WATERMARK_DEVICE", args.watermark_device)
                .strip()
                .lower()
            )
            try:
                resolved = resolve_audioseal_device(requested)
            except (ValueError, RuntimeError) as exc:
                resolved = f"unavailable ({exc})"
            result = (
                f"{tools}\nPyTorch {torch.__version__} · {gpu} available: {available}. "
                f"Watermark selection: {args.watermark_device}; effective request: {requested}; local device: {resolved}."
            )
            if args.tts_provider != "comfyui":
                result += f"\nAPI key environment variable {args.tts_api_key_env}: {'set' if os.environ.get(args.tts_api_key_env) else 'not set (optional for local compatible endpoints)'}."
            return result + "\nGenerate a sample to check the speech provider."

        self.setup_status.setText("Checking local tools and accelerator support…")
        self._background("setup", check, self.setup_status.setText)

    def _set_running(self, running):
        self.cancel_btn.setText("Pause active")
        if running:
            self.progress.setRange(0, 0)
            self.progress_status.setText("Preparing narration plan…")
        self._refresh_action_controls()

    def _on_progress(self, job_id, update):
        if job_id != self.active_job_id:
            return
        now = time.monotonic()
        if update.phase != self._phase:
            if self._phase:
                self._phase_times[self._phase] = (
                    self._phase_times.get(self._phase, 0.0) + now - self._phase_started
                )
            self._phase_started = now
            self._phase = update.phase
        self.active_progress_percent = update.percent
        self.progress.setRange(0, 100)
        self.progress.setValue(update.percent)
        self.progress_status.setText(format_progress_text(update))
        self._set_library_runtime_state(job_id, "Running", update.percent)
        self._show_phase_times()

    def _show_phase_times(self):
        times = dict(self._phase_times)
        if self._phase:
            times[self._phase] = (
                times.get(self._phase, 0.0) + time.monotonic() - self._phase_started
            )
        grouped = {}
        for name, seconds in times.items():
            label = (
                "Speech"
                if name in {"Narrating", "Synthesizing disclosure"}
                else "Watermark"
                if "Watermarking" in name
                else "Assembly"
                if name.startswith("Assembling")
                else "Encoding"
                if name.startswith("Encoding")
                else "Signing"
                if name == "Signing"
                else None
            )
            if label:
                grouped[label] = grouped.get(label, 0.0) + seconds
        self.phase_status.setText(
            " · ".join(f"{name}: {seconds:.1f}s" for name, seconds in grouped.items())
        )

    def _tail_log(self):
        if self._log_path and self._log_path.is_file():
            try:
                with self._log_path.open(
                    "r", encoding="utf-8", errors="replace"
                ) as handle:
                    handle.seek(self._log_offset)
                    new = handle.read(65536)
                    self._log_offset = handle.tell()
                if new:
                    self._append_log(new.rstrip())
            except OSError:
                pass
        if self.active_job_id:
            self._show_phase_times()

    def _on_worker_finished(self, job_id, status, message):
        self._tail_log()
        self._show_phase_times()
        self._phase = None
        self.progress.setRange(0, 100)
        if status == "completed":
            self.progress.setValue(100)
        self.progress_status.setText(
            f"Failed · {message}" if status == "failed" else status.capitalize()
        )
        self._append_log(f"{status.capitalize()}: {message}")
        self._set_library_runtime_state(
            job_id, status.capitalize(), self.progress.value()
        )
        if status != "completed":
            self._queue_armed = False
        if status == "failed":
            self.log_fold.toggle_button.setChecked(True)
        if job_id.startswith("sample:"):
            if status == "completed" and self._active_args:
                checkpoint = CheckpointStore(
                    AppConfig.state_dir_for(self._active_args.output_dir)
                ).load()
                records = (checkpoint or {}).get("artifacts", {}).get("chapters", {})
                path = next((record.get("path") for record in records.values()), None)
                self._sample_path = path
                try:
                    current = self._sample_key == self._settings_key()
                except (ValueError, OSError):
                    current = False
                self.play_sample_btn.setEnabled(bool(path and current))
                self.sample_status.setText(
                    "Sample ready."
                    if current
                    else "Sample finished for earlier settings. Generate a new sample for this draft."
                )
            else:
                self.sample_status.setText(
                    "Sample paused"
                    if status == "paused"
                    else f"Sample {status}: {message}"
                )

    def _worker_thread_stopped(self):
        self.worker_thread.wait()
        self.worker = None
        self.worker_thread = None
        self.active_job_id = None
        self._active_args = None
        self._log_path = None
        self._rescan_library()
        self._set_running(False)
        if not self._closing_after_pause:
            self._request_preview()
        if self._closing_after_pause:
            QTimer.singleShot(0, self.close)
        else:
            QTimer.singleShot(0, self._dispatch_queue)

    def _find_log(self):
        if not self.log.find(self.log_search.text()):
            self.log.moveCursor(QTextCursor.MoveOperation.Start)
            self.log.find(self.log_search.text())

    def _copy_log(self):
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.log.toPlainText())

    def closeEvent(self, event):
        self._save_current_draft()
        self._draft_timer.stop()
        self._stop_sample()
        self._preview_timer.stop()
        self._pending_tasks.clear()
        self._queue_armed = False
        running = bool(
            self.worker_thread
            or self._threads
            or self.catalog_thread
            or self.voice_thread
        )
        if running:
            self._closing_after_pause = True
            if self.worker:
                self._cancel_run()
            event.ignore()
            # Keep the window alive until all owned threads have finished.
            QTimer.singleShot(100, self.close)
            return
        self._log_timer.stop()
        super().closeEvent(event)


def settings_mode(window):
    return window.narrator_catalog.get(
        str(window.narrator_profile_combo.currentData())
    ).settings.voice_mode
