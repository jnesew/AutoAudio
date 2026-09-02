from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

if "websocket" not in sys.modules:
    websocket_stub = ModuleType("websocket")
    websocket_stub.WebSocket = object
    websocket_stub.WebSocketTimeoutException = TimeoutError
    sys.modules["websocket"] = websocket_stub

from core.checkpoint import CheckpointStore
from core.pipeline import build_argument_parser
from gui.state import GUI_CONTROLLED_DESTINATIONS, bool_from_ui_state, gui_cli_parity, load_resume_context


def test_load_resume_context_for_incomplete_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save({"version": 2, "status": "running", "ui_state": {"input_book": "book.epub", "fetch_metadata": True}})

    context = load_resume_context(store)

    assert context is not None
    assert context.ui_state["input_book"] == "book.epub"


def test_load_resume_context_ignores_completed_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save({"version": 2, "status": "completed", "ui_state": {"input_book": "done.epub"}})

    assert load_resume_context(store) is None


def test_load_resume_context_accepts_cancelled_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save({"version": 2, "status": "cancelled", "ui_state": {"input_book": "book.epub"}})

    context = load_resume_context(store)

    assert context is not None
    assert context.ui_state["input_book"] == "book.epub"


def test_load_resume_context_ignores_legacy_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save({"version": 1, "status": "running", "ui_state": {"input_book": "legacy.epub"}})

    assert load_resume_context(store) is None


def test_bool_from_ui_state_variants():
    assert bool_from_ui_state(True) is True
    assert bool_from_ui_state("yes") is True
    assert bool_from_ui_state("0") is False
    assert bool_from_ui_state("maybe", default=True) is True


def test_gui_control_contract_covers_every_cli_option():
    missing, stale = gui_cli_parity(build_argument_parser(PROJECT_ROOT))

    assert missing == set()
    assert stale == set()


def test_gui_collects_every_declared_control():
    source = (PROJECT_ROOT / "src" / "gui" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    collect_args = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_collect_args"
    )
    assigned = {
        target.attr
        for node in ast.walk(collect_args)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "args"
    }

    assert assigned == set(GUI_CONTROLLED_DESTINATIONS) | {"resume"}


def test_gui_source_contains_no_reference_voice_picker():
    source = (PROJECT_ROOT / "src" / "gui" / "app.py").read_text(encoding="utf-8").lower()

    assert "reference_audio" not in source
    assert "reference voice" not in source


def test_gui_uses_dropdowns_for_bundled_speaker_and_model_choices():
    source = (PROJECT_ROOT / "src" / "gui" / "app.py").read_text(encoding="utf-8")

    assert "self.speaker_combo = combo(QWEN_PRESET_SPEAKERS" in source
    assert "self.model_choice_combo = combo(QWEN_MODEL_CHOICES_BY_MODE" in source
    assert "self.speaker_edit = QLineEdit()" not in source
    assert "self.model_choice_edit = QLineEdit()" not in source


def test_gui_connects_structured_progress_and_eta_updates():
    source = (PROJECT_ROOT / "src" / "gui" / "app.py").read_text(encoding="utf-8")

    assert "progress_changed = Signal(str, object)" in source
    assert "progress_callback=lambda update: self.progress_changed.emit(self.job_id, update)" in source
    assert "self.worker.progress_changed.connect(self._on_progress)" in source
    assert "format_progress_text(update)" in source


def test_gui_exposes_per_title_library_queue_and_cooperative_pause():
    source = (PROJECT_ROOT / "src" / "gui" / "app.py").read_text(encoding="utf-8")

    assert 'self.tabs.addTab(self._build_library_tab(), "Library")' in source
    assert "scan_library(books_dir, output_root)" in source
    assert "self.library_tree.setItemWidget(item, 3, progress_bar)" in source
    assert 'self._set_library_runtime_state(job_id, "Queued")' in source
    assert 'self.cancel_btn = QPushButton("Pause active")' in source
    assert 'self._set_library_runtime_state(job_id, "Paused", current_percent)' in source


def test_gui_requires_confirmation_before_a_specific_gutenberg_download():
    source = (PROJECT_ROOT / "src" / "gui" / "app.py").read_text(encoding="utf-8")

    assert "confirmation = QMessageBox(self)" in source
    assert 'confirmation.setWindowTitle("Confirm Project Gutenberg download")' in source
    assert "confirmation.setTextFormat(Qt.TextFormat.PlainText)" in source
    assert "confirmation.setDefaultButton(QMessageBox.StandardButton.No)" in source
    assert "if decision != QMessageBox.StandardButton.Yes:" in source
    assert "self.client.download_epub(self.book, acquisition, self.books_dir)" in source


def test_gui_loads_only_selected_gutenberg_details_before_review():
    source = (PROJECT_ROOT / "src" / "gui" / "app.py").read_text(encoding="utf-8")

    assert "self.client.load_book_details(self.book)" in source
    assert 'return "Unavailable" if book.details_loaded else "Select to review"' in source
    assert 'QPushButton("Review & download selected EPUB…")' in source


def test_generated_and_downloaded_work_directories_keep_only_safe_placeholders_trackable():
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "books/*" in ignore
    assert "!books/.gitkeep" in ignore
    assert "audiobook_output/*" in ignore
    assert "!audiobook_output/.gitkeep" in ignore
    assert "audiobook_output/.autoaudio_state/*" in ignore
    assert "audiobook_output/.segments/*" in ignore
    assert (PROJECT_ROOT / "audiobook_output" / ".autoaudio_state" / ".gitkeep").is_file()
    assert (PROJECT_ROOT / "audiobook_output" / ".segments" / ".gitkeep").is_file()
