from __future__ import annotations

import hashlib
import json
import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Mock the uninstalled network client dependency.
if "websocket" not in sys.modules:
    websocket_module = types.ModuleType("websocket")
    websocket_module.WebSocket = object
    sys.modules["websocket"] = websocket_module


from core.audio_assembly import sanitize_metadata_value
from comfyui.workflow_loader import load_workflow_template
from core.checkpoint import CheckpointStore
from core.config import AppConfig, GenerationSettings
from core.pipeline import (
    _extract_provenance_runtime_metadata,
    _publish_chapter_from_master,
    build_qwen_book_plan,
    resolve_metadata,
)
from core.segmentation import SegmentPolicy
from metadata.epub_parser import ParsedEpub
from metadata.models import BookMetadata
from provenance.ai_marking import manifest_path_for, write_ai_marking_manifest
from provenance.c2pa import ProvenanceResult, ProvenanceRuntimeMetadata


def test_sanitize_ffmpeg_metadata_value_removes_newlines():
    assert sanitize_metadata_value("Chapter 2: I.\nIntroduction") == "Chapter 2: I. Introduction"
    assert sanitize_metadata_value("\n\n") is None


def test_book_plan_does_not_skip_chapter_for_gutenberg_words_alone():
    plan = build_qwen_book_plan(
        [("Preface", "Project Gutenberg publishes this sentence, but it is legitimate narration text.")],
        input_hash="input",
        settings_hash="settings",
        workflow_hash="workflow",
        segment_policy=SegmentPolicy(target_words=20, max_words=30),
    )

    assert plan.chapters[0].skipped_reason is None
    assert [segment.text for segment in plan.chapters[0].segments] == [
        "Project Gutenberg publishes this sentence, but it is legitimate narration text."
    ]


def test_resolve_metadata_reuses_parsed_epub_without_reopening(tmp_path):
    parsed = ParsedEpub(
        metadata=BookMetadata(title="Embedded title", author="Embedded author"),
        text_blocks=(("Chapter One", "Long enough chapter text for the already parsed EPUB snapshot."),),
        cover=None,
        diagnostics=(),
        gutenberg_detected=False,
        gutenberg_changed=False,
    )
    args = types.SimpleNamespace(fetch_metadata=False, gutenberg_id="", title="", author="")

    with patch("core.pipeline.parse_epub", side_effect=AssertionError("EPUB was reopened")):
        metadata = resolve_metadata(
            args,
            str(tmp_path / "book.epub"),
            "epub",
            str(tmp_path),
            parsed_epub=parsed,
        )

    assert metadata.title == "Embedded title"
    assert metadata.author == "Embedded author"


def test_resolve_metadata_treats_cover_write_failure_as_nonfatal(tmp_path):
    parsed = ParsedEpub(
        metadata=BookMetadata(title="Embedded title", author="Embedded author"),
        text_blocks=(),
        cover=None,
        diagnostics=(),
        gutenberg_detected=False,
        gutenberg_changed=False,
    )
    args = types.SimpleNamespace(fetch_metadata=False, gutenberg_id="", title="", author="")

    with patch("core.pipeline.write_cover_art", side_effect=OSError("read-only output")):
        metadata = resolve_metadata(
            args,
            str(tmp_path / "book.epub"),
            "epub",
            str(tmp_path),
            parsed_epub=parsed,
        )

    assert metadata.title == "Embedded title"
    assert metadata.cover_image_path is None


def test_provenance_identity_uses_effective_qwen_settings_not_template_defaults(monkeypatch):
    workflow = load_workflow_template(PROJECT_ROOT / "resources" / "workflows" / "qwen3_tts_custom_voice.json")
    settings = GenerationSettings(model_choice="0.6B")
    monkeypatch.setenv("AUTOAUDIO_VERSION", "2.0.0-test")

    runtime = _extract_provenance_runtime_metadata(workflow, settings)

    assert runtime.model_name == "Qwen3-TTS"
    assert runtime.model_version == "0.6B"
    assert runtime.backend_name == "FB_Qwen3TTSCustomVoice"
    assert runtime.backend_version == "unreported"
    assert runtime.software_version == "2.0.0-test"


def test_chapter_publish_refreshes_sidecar_and_checkpoints_final_c2pa_hash(tmp_path):
    chapter = tmp_path / "Chapter_001.flac"
    source_bytes = b"encoded-chapter"
    final_bytes = source_bytes + b"-c2pa"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    final_sha256 = hashlib.sha256(final_bytes).hexdigest()

    def fake_encode(_master_path, output_filename, **_kwargs):
        Path(output_filename).write_bytes(source_bytes)
        write_ai_marking_manifest(
            output_filename,
            content_id="chapter",
            metadata_embedded=True,
            watermark_applied=True,
            watermark_verified=True,
            watermark_detail="test",
        )

    def fake_c2pa(*, artifact_path, **_kwargs):
        Path(artifact_path).write_bytes(final_bytes)
        return ProvenanceResult(
            manifest_id="urn:uuid:test",
            embedding_path="chunk",
            source_sha256=source_sha256,
            final_sha256=final_sha256,
        )

    checkpoint = {
        "artifacts": {"chapters": {}, "provenance": {}},
        "progress": {"completed_chapters": []},
    }
    store = CheckpointStore(tmp_path / "state")
    runtime = ProvenanceRuntimeMetadata(
        model_name="Qwen3-TTS",
        model_version="1.7B",
        backend_name="FB_Qwen3TTSCustomVoice",
        backend_version="unreported",
        software_version="2.0.0",
    )

    with patch("core.pipeline.encode_lossless_master", side_effect=fake_encode), patch(
        "core.pipeline.apply_c2pa_with_policy", side_effect=fake_c2pa
    ):
        _publish_chapter_from_master(
            master_path=tmp_path / "master.flac",
            chapter_filename=str(chapter),
            title="Chapter One",
            chapter_index=0,
            metadata=BookMetadata(title="Book", author="Author"),
            checkpoint=checkpoint,
            checkpoint_store=store,
            config=AppConfig(project_root=PROJECT_ROOT),
            provenance_runtime_metadata=runtime,
            logger=logging.getLogger("test.publish"),
        )

    sidecar = json.loads(manifest_path_for(chapter).read_text(encoding="utf-8"))
    assert sidecar["artifact_sha256"] == final_sha256
    assert checkpoint["artifacts"]["chapters"]["0"]["sha256"] == final_sha256
    assert checkpoint["artifacts"]["provenance"][str(chapter)]["source_sha256"] == source_sha256
    assert checkpoint["artifacts"]["provenance"][str(chapter)]["final_sha256"] == final_sha256
