from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.checkpoint import CheckpointStore
from gui.state import bool_from_ui_state, load_resume_context


def test_load_resume_context_for_incomplete_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save({"status": "running", "ui_state": {"input_book": "book.epub", "fetch_metadata": True}})

    context = load_resume_context(store)

    assert context is not None
    assert context.ui_state["input_book"] == "book.epub"


def test_load_resume_context_ignores_completed_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save({"status": "completed", "ui_state": {"input_book": "done.epub"}})

    assert load_resume_context(store) is None


def test_bool_from_ui_state_variants():
    assert bool_from_ui_state(True) is True
    assert bool_from_ui_state("yes") is True
    assert bool_from_ui_state("0") is False
    assert bool_from_ui_state("maybe", default=True) is True
