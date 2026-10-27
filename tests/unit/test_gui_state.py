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
from gui.state import (
    GUI_CONTROLLED_DESTINATIONS,
    bool_from_ui_state,
    gui_cli_parity,
    load_resume_context,
)


def test_load_resume_context_for_incomplete_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save(
        {
            "version": 2,
            "status": "running",
            "ui_state": {"input_book": "book.epub", "fetch_metadata": True},
        }
    )

    context = load_resume_context(store)

    assert context is not None
    assert context.ui_state["input_book"] == "book.epub"


def test_load_resume_context_ignores_completed_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save(
        {"version": 2, "status": "completed", "ui_state": {"input_book": "done.epub"}}
    )

    assert load_resume_context(store) is None


def test_load_resume_context_accepts_cancelled_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save(
        {"version": 2, "status": "cancelled", "ui_state": {"input_book": "book.epub"}}
    )

    context = load_resume_context(store)

    assert context is not None
    assert context.ui_state["input_book"] == "book.epub"


def test_load_resume_context_ignores_legacy_checkpoint(tmp_path):
    store = CheckpointStore(state_dir=tmp_path)
    store.save(
        {"version": 1, "status": "running", "ui_state": {"input_book": "legacy.epub"}}
    )

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
    source = (PROJECT_ROOT / "src" / "gui" / "window.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    collect_args = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_collect_args"
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


def test_cli_voice_discovery_requires_an_explicit_flag():
    parser = build_argument_parser(PROJECT_ROOT)

    assert parser.parse_args([]).discover_voices is False
    assert parser.parse_args(["--discover-voices"]).discover_voices is True
    assert (
        parser.parse_args(["--tts-timeout-seconds", "4.5"]).comfyui_timeout_seconds
        == 4.5
    )
    assert (
        parser.parse_args(["--comfyui-timeout-seconds", "4.5"]).comfyui_timeout_seconds
        == 4.5
    )


def test_generated_and_downloaded_work_directories_keep_only_safe_placeholders_trackable():
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "books/*" in ignore
    assert "!books/.gitkeep" in ignore
    assert "audiobook_output/*" in ignore
    assert "!audiobook_output/.gitkeep" in ignore
    assert "audiobook_output/.autoaudio_state/*" in ignore
    assert "audiobook_output/.segments/*" in ignore
    assert (
        PROJECT_ROOT / "audiobook_output" / ".autoaudio_state" / ".gitkeep"
    ).is_file()
    assert (PROJECT_ROOT / "audiobook_output" / ".segments" / ".gitkeep").is_file()
