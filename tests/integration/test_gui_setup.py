"""Exercise the actual Qt setup controls without contacting a provider."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


@pytest.fixture(scope="module")
def qt_app():
    with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
        widgets = pytest.importorskip("PySide6.QtWidgets")
        app = widgets.QApplication.instance() or widgets.QApplication([])
        yield app


def wait_until(predicate, timeout=5000):
    # QTest.qWait in a tight Python loop can starve Python work in a QThread.
    # A Qt event loop releases the GIL between timer callbacks like the real app.
    from PySide6.QtCore import QEventLoop, QTimer

    if predicate():
        return
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    deadline = QTimer()
    deadline.setSingleShot(True)
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline.timeout.connect(loop.quit)
    timer.start()
    deadline.start(timeout)
    loop.exec()
    timer.stop()
    deadline.stop()
    assert predicate()


def test_find_books_searches_locally_filters_language_and_reviews_one_title(setup_window, tmp_path, monkeypatch):
    import gzip
    import io
    from metadata.gutenberg_catalog import GutenbergCatalogClient
    from metadata.gutenberg_index import CATALOG_URL

    window, network = setup_window
    compressed = gzip.compress(b'''Text#,Type,Title,Language,Authors,Subjects
1727,Text,The Odyssey,en,Homer,Epic poetry
30615,Text,Odyssey Greek,el,Homer,Epic poetry
''')
    detail = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dcterms="http://purl.org/dc/terms/">
<entry><id>urn:gutenberg:1727:3</id><title>The Odyssey</title><author><name>Homer</name></author>
<dcterms:language>en</dcterms:language><rights>Public domain in the USA.</rights>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/ebooks/1727.epub3.images" title="EPUB3 with images"/>
</entry></feed>'''
    requests = []

    def opener(request, **_kwargs):
        requests.append(request.full_url)
        result = io.BytesIO(compressed if request.full_url == CATALOG_URL else detail)
        result.headers = {}
        result.geturl = lambda: request.full_url
        return result

    window.catalog_client = GutenbergCatalogClient(cache_dir=tmp_path / "catalog", opener=opener,
                                                    minimum_request_interval=0)
    window.catalog_search_edit.setText("Homer")
    window.catalog_search_button.click()
    wait_until(lambda: window.catalog_thread is None)
    assert window.catalog_tree.topLevelItemCount() == 2
    assert {window.catalog_tree.topLevelItem(i).text(2) for i in range(2)} == {"en", "el"}
    assert "Showing 2 of 2 results" in window.catalog_status.text()
    window.catalog_language_combo.setCurrentIndex(window.catalog_language_combo.findData("en"))
    wait_until(lambda: window.catalog_thread is None)
    assert window.catalog_tree.topLevelItemCount() == 1
    assert window.catalog_tree.topLevelItem(0).text(1) == "Homer"
    assert requests == [CATALOG_URL]

    confirmation = Mock()
    monkeypatch.setattr(window, "_confirm_gutenberg_download", confirmation)
    window.catalog_tree.setCurrentItem(window.catalog_tree.topLevelItem(0))
    window.catalog_download_button.click()
    wait_until(lambda: window.catalog_thread is None and confirmation.called)
    reviewed = confirmation.call_args.args[0]
    assert reviewed.details_loaded and reviewed.preferred_epub is not None
    assert reviewed.gutenberg_id == "1727"
    assert requests == [CATALOG_URL, "https://www.gutenberg.org/ebooks/1727.opds"]
    assert window.catalog_language_combo.isEnabled()
    network.assert_not_called()


@pytest.fixture
def setup_window(tmp_path, monkeypatch, qt_app):
    from gui.window import MainWindow

    project_root = Path(__file__).resolve().parents[2]
    (tmp_path / "resources").symlink_to(
        project_root / "resources", target_is_directory=True
    )
    (tmp_path / "books").mkdir()
    (tmp_path / "books" / "sample.txt").write_text(
        "A short setup test book.", encoding="utf-8"
    )
    network = Mock(side_effect=AssertionError("Setup must not contact a provider"))
    monkeypatch.setattr("urllib.request.urlopen", network)
    window = MainWindow(tmp_path)
    window.show()
    wait_until(lambda: window._preview_result is not None)
    yield window, network
    window.close()
    wait_until(
        lambda: (
            not window.isVisible()
            and not window._threads
            and window.worker_thread is None
        )
    )
    window.deleteLater()
    qt_app.processEvents()


def test_initial_setup_finds_book_and_provider_changes_stay_offline(setup_window):
    window, network = setup_window
    assert window.library_tree.topLevelItemCount() == 1
    assert window.start_btn.isEnabled()
    assert not window.resume_btn.isEnabled()
    assert not window.cancel_btn.isEnabled()
    assert window.watermark_device_combo.currentText() == "auto"
    assert not window.windowIcon().isNull()
    assert not window.windowIcon().pixmap(32, 32).isNull()
    for provider in ("openai-compatible", "elevenlabs", "comfyui"):
        window.tts_provider_combo.setCurrentText(provider)
        assert window._collect_args(resume_mode="no").tts_provider == provider
    network.assert_not_called()


def test_manually_selected_input_can_start_and_tracks_its_own_busy_state(
    setup_window, tmp_path
):
    window, network = setup_window
    source = tmp_path / "outside-library.txt"
    source.write_text("A manually selected book.", encoding="utf-8")
    window.input_edit.setText(str(source))
    window._on_input_changed(str(source))
    assert window.current_library_book_id is None
    wait_until(lambda: window._preview_result is not None)
    assert window.start_btn.isEnabled()
    window.active_job_id = f"manual:{source.resolve()}"
    window._refresh_action_controls()
    assert not window.start_btn.isEnabled()
    window.active_job_id = None
    window.queued_jobs.append(
        (f"manual:{source.resolve()}", window._collect_args(resume_mode="no"))
    )
    window._refresh_action_controls()
    assert not window.start_btn.isEnabled()
    window.queued_jobs.clear()
    window._refresh_action_controls()
    wait_until(lambda: window._preview_result is not None)
    assert window.start_btn.isEnabled()
    network.assert_not_called()


@pytest.mark.parametrize(
    ("control", "value", "message"),
    (
        ("tts_base_url_edit", "invalid-url", "absolute http"),
        ("tts_api_key_env_edit", "invalid variable name", "environment variable name"),
    ),
)
def test_invalid_provider_settings_do_not_leave_a_phantom_running_job(
    setup_window,
    monkeypatch,
    control,
    value,
    message,
):
    from PySide6.QtWidgets import QMessageBox

    window, network = setup_window
    window.tts_provider_combo.setCurrentText("openai-compatible")
    getattr(window, control).setText(value)
    warning = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    window._launch_from_ui("no")
    warning.assert_not_called()
    assert message in window.validation_status.text()
    assert window.active_job_id is None
    assert window.worker is None
    assert window.worker_thread is None
    assert not window.start_btn.isEnabled()
    assert not window.cancel_btn.isEnabled()
    network.assert_not_called()


def test_provider_panels_and_collapsed_log(setup_window):
    window, network = setup_window
    assert not window.comfy_group.isHidden()
    assert window.endpoint_group.isHidden()
    assert not window.log.isVisible()
    window.tts_provider_combo.setCurrentText("elevenlabs")
    assert window.comfy_group.isHidden()
    assert not window.endpoint_group.isHidden()
    assert not window.eleven_group.isHidden()
    assert window.instruction_group.isHidden()
    window.tts_provider_combo.setCurrentText("openai-compatible")
    assert window.eleven_group.isHidden()
    assert not window.instruction_group.isHidden()
    network.assert_not_called()


def test_drafts_survive_rescan_manual_selection_and_relaunch(
    setup_window, tmp_path, qt_app
):
    from gui.window import MainWindow

    window, network = setup_window
    original = window.current_job_id
    window.title_edit.setText("Saved title")
    window.seed_edit.setText("unfinished seed")
    window.provenance_password_edit.setText("never-save-this")
    window._add_replacement_row({"source": "II", "spoken": "two"})
    window._rescan_library()
    wait_until(lambda: "scan" not in window._threads)
    assert window.current_job_id == original
    assert window.title_edit.text() == "Saved title"
    assert window.seed_edit.text() == "unfinished seed"
    source = tmp_path / "manual.txt"
    source.write_text("A manual source outside the library.")
    window.input_edit.setText(str(source))
    window._on_input_changed(str(source))
    manual = window.current_job_id
    window.title_edit.setText("Manual title")
    output = window.output_edit.text()
    window._rescan_library()
    wait_until(lambda: "scan" not in window._threads)
    assert window.current_job_id == manual
    assert window.output_edit.text() == output
    window._select_job(original)
    assert window.title_edit.text() == "Saved title"
    assert window.seed_edit.text() == "unfinished seed"
    assert window.replacement_table.rowCount() == 1
    assert not window.provenance_password_edit.text()
    window._select_job(manual)
    window._save_current_draft()
    assert "never-save-this" not in window.drafts.path.read_text()
    other = MainWindow(tmp_path)
    other.show()
    try:
        wait_until(lambda: "scan" not in other._threads)
        assert other.current_job_id == manual
        assert other.title_edit.text() == "Manual title"
        assert other.output_edit.text() == output
        assert other.active_job_id is None
        assert not other.queued_jobs
    finally:
        other.close()
        wait_until(lambda: not other.isVisible())
        other.deleteLater()
    network.assert_not_called()


def test_chapter_edits_exclusions_and_sample_use_same_text(setup_window, monkeypatch):
    from PySide6.QtCore import Qt

    window, network = setup_window
    source = Path(window.input_edit.text())
    source.write_text(
        "II was here in the first room.\n\nII was there in the second room."
    )
    window.target_words_per_chapter_spin.setValue(1)
    window.min_paragraphs_per_chapter_spin.setValue(1)
    window._add_replacement_row({"source": "II", "spoken": "two"})
    window._request_preview()
    wait_until(
        lambda: (
            window._preview_result is not None
            and window.chapter_tree.topLevelItemCount() == 2
        )
    )
    assert window.narration_editor.toPlainText() == "two was here in the first room."
    window.narration_editor.setPlainText("II stays literal after editing.")
    assert not window.start_btn.isEnabled()
    window._apply_chapter_edit()
    wait_until(lambda: window._preview_result is not None)
    assert (
        window.segment_tree.topLevelItem(0).text(1) == "II stays literal after editing."
    )
    window.chapter_tree.topLevelItem(1).setCheckState(0, Qt.CheckState.Unchecked)
    window._request_preview()
    wait_until(
        lambda: (
            window._preview_result is not None
            and window._preview_result.plan.chapters[1].skipped_reason
        )
    )
    args = window._collect_args(resume_mode="no")
    assert args.excluded_chapters == [2]
    assert args.chapter_text_overrides == [
        {"chapter": 1, "text": "II stays literal after editing."}
    ]
    launches = []
    monkeypatch.setattr(
        window, "_launch_pipeline", lambda args, job_id: launches.append((args, job_id))
    )
    window._generate_sample()
    assert len(launches) == 1
    sample, job_id = launches[0]
    assert job_id.startswith("sample:")
    assert Path(sample.input_book).read_text() == "II stays literal after editing."
    assert sample.replacement_rules == []
    assert sample.excluded_chapters == []
    assert sample.chapter_text_overrides == []
    assert sample.output_dir != args.output_dir
    assert not Path(args.output_dir).exists()
    network.assert_not_called()


def test_queue_has_captured_settings_and_reorder_remove(setup_window, monkeypatch):
    window, network = setup_window
    window.title_edit.setText("First job")
    window._request_preview()
    wait_until(lambda: window._preview_result is not None)
    window._enqueue_current()
    first = window.current_job_id
    window._new_variant()
    window.title_edit.setText("Second job")
    window._request_preview()
    wait_until(lambda: window._preview_result is not None)
    window._enqueue_current()
    window.title_edit.setText("Later draft")
    assert len(window.queued_jobs) == 2
    assert [args.title for _, args in window.queued_jobs] == ["First job", "Second job"]
    assert not window._queue_armed
    window.queue_list.setCurrentRow(1)
    window._move_queued(-1)
    assert [args.title for _, args in window.queued_jobs] == ["Second job", "First job"]
    window._remove_queued()
    assert len(window.queued_jobs) == 1
    assert window.queued_jobs[0][0] == first
    launched = []
    monkeypatch.setattr(
        window,
        "_launch_pipeline",
        lambda args, job: launched.append((args.title, job)) or True,
    )
    window._run_queue()
    assert launched == [("First job", first)]
    network.assert_not_called()


def test_replace_keeps_previous_files_and_checkpoint(setup_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window, network = setup_window
    output = Path(window.output_edit.text())
    output.mkdir(parents=True)
    (output / "precious.flac").write_bytes(b"existing audio")
    window._save_current_draft()
    old_id = window.current_job_id
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes
    )
    window._replace_run()
    assert window.current_job_id == old_id
    assert Path(window.output_edit.text()) != output
    assert (output / "precious.flac").read_bytes() == b"existing audio"
    archived = [state for state in window.drafts.jobs.values() if state.get("archived")]
    assert len(archived) == 1
    assert Path(archived[0]["output_edit"]) == output
    network.assert_not_called()


def test_background_results_are_applied_on_gui_thread_and_stale_work_is_ignored(
    setup_window, qt_app
):
    import threading
    from PySide6.QtCore import QThread

    window, _ = setup_window
    release = threading.Event()
    observed = []
    window._background(
        "probe",
        lambda: release.wait(2) or "old",
        lambda value: observed.append("stale"),
    )
    window._background(
        "probe",
        lambda: "new",
        lambda value: observed.append(
            (value, QThread.currentThread() == qt_app.thread())
        ),
    )
    release.set()
    wait_until(lambda: "probe" not in window._threads)
    assert observed == [("new", True)]


def test_close_waits_for_background_workers(setup_window):
    import threading

    window, _ = setup_window
    release = threading.Event()
    window._background("probe", lambda: release.wait(2), lambda value: None)
    assert not window.close()
    assert window.isVisible()
    release.set()
    wait_until(lambda: not window.isVisible() and not window._threads)


def test_pending_editor_draft_survives_job_switch(setup_window):
    window, _ = setup_window
    original = window.current_job_id
    window.narration_editor.setPlainText("Pending text that has not been applied.")
    window._new_variant()
    window._select_job(original)
    wait_until(lambda: window._preview_result is not None)
    assert (
        window.narration_editor.toPlainText()
        == "Pending text that has not been applied."
    )
    assert window._editor_dirty
    assert not window.start_btn.isEnabled()


def test_active_job_can_pause_without_losing_another_draft(setup_window, monkeypatch):
    import threading
    from core.errors import PipelineCancelled

    window, network = setup_window
    entered = threading.Event()

    def run(args, config, cancellation, progress_callback):
        entered.set()
        while not cancellation.is_cancelled:
            threading.Event().wait(0.01)
        raise PipelineCancelled("Test pause checkpoint")

    monkeypatch.setattr("gui.window.run_pipeline", run)
    original = window.current_job_id
    window._launch_from_ui("no")
    wait_until(entered.is_set)
    assert window.cancel_btn.isEnabled()
    window._new_variant()  # active job itself is disabled in the UI; select another source below
    source = window.project_root / "other.txt"
    source.write_text("Another source stays editable during generation.")
    window.input_edit.setText(str(source))
    window._on_input_changed(str(source))
    window.title_edit.setText("Draft during active job")
    selected = window.current_job_id
    window._cancel_run()
    wait_until(lambda: window.worker_thread is None)
    assert window.current_job_id == selected
    assert window.title_edit.text() == "Draft during active job"
    assert window.progress_status.text() == "Paused"
    assert "Test pause checkpoint" in window.log.toPlainText()
    assert not window._queue_armed
    assert not window.cancel_btn.isEnabled()
    network.assert_not_called()


def test_voice_discovery_is_explicit_and_voice_ids_survive_drafts(
    setup_window, monkeypatch
):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from tts.base import VoiceInfo

    window, network = setup_window
    discovered = Mock(
        return_value=(VoiceInfo("stable-id", "Friendly name", "premade"),)
    )
    monkeypatch.setattr("gui.window.discover_provider_voices", discovered)
    window.tts_provider_combo.setCurrentText("elevenlabs")
    window.tabs.setCurrentIndex(window.narrator_tab_index)
    discovered.assert_not_called()
    QTest.mouseClick(window.voice_discovery_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.voice_thread is None)
    discovered.assert_called_once()
    assert not window.voice_discovery_status.isHidden()
    assert window.voice_discovery_status.text() == "1 voice loaded."
    assert window._selected_tts_voice() == "stable-id"
    state = window._capture_controls()
    assert state["tts_voice_combo"] == "stable-id"
    window.tts_voice_combo.setEditText("manual-id")
    assert window._selected_tts_voice() == "manual-id"
    window._restore_controls(state)
    assert window._selected_tts_voice() == "stable-id"
    network.assert_not_called()


def test_external_rule_file_changes_invalidate_sample(
    setup_window, tmp_path, monkeypatch
):
    import json

    window, _ = setup_window
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {"version": 1, "replacements": [{"source": "short", "spoken": "brief"}]}
        )
    )
    window.replacement_file_edit.setText(str(rules))
    window._request_preview()
    wait_until(lambda: window._preview_result is not None)
    rules.write_text(
        json.dumps(
            {"version": 1, "replacements": [{"source": "short", "spoken": "different"}]}
        )
    )
    launch = Mock()
    monkeypatch.setattr(window, "_launch_pipeline", launch)
    window._generate_sample()
    launch.assert_not_called()
    assert (
        "current narration plan" in window.sample_status.text()
        or "preview" in window.sample_status.text()
    )


def test_redundant_preview_keeps_matching_sample_playable(setup_window):
    window, _ = setup_window
    key = window._settings_key()
    window._sample_key = key
    window._sample_source_text = window.sample_text_label.text().split("\n", 1)[1]
    window.play_sample_btn.setEnabled(True)
    window._request_preview()
    window._select_preview_segment()
    assert window._sample_key == key
    assert window.play_sample_btn.isEnabled()


def test_preview_shows_saved_frozen_resume_text_separately(setup_window):
    from core.cancellation import CancellationToken
    from core.errors import PipelineCancelled
    from core.pipeline import run_pipeline, build_app_config

    window, _ = setup_window
    args = window._collect_args(resume_mode="no")
    args.comfyui_mode = "spoof"
    token = CancellationToken()
    token.cancel()
    with pytest.raises(PipelineCancelled):
        run_pipeline(args, build_app_config(args, window.project_root), token)
    window._request_preview()
    wait_until(lambda: window._preview_result.saved_plan is not None)
    assert not window.saved_plan_fold.isHidden()
    assert "A short setup test book." in window.saved_plan_text.toPlainText()
    assert window.saved_plan_text.isReadOnly()
    assert "frozen" in window.saved_plan_text.toPlainText()
