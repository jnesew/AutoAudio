from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

if "ebooklib" not in sys.modules:
    ebooklib_module = types.ModuleType("ebooklib")
    ebooklib_module.ITEM_COVER = 1
    ebooklib_module.ITEM_IMAGE = 2
    ebooklib_module.ITEM_DOCUMENT = 3
    epub_module = types.ModuleType("ebooklib.epub")
    ebooklib_module.epub = epub_module
    sys.modules["ebooklib"] = ebooklib_module
    sys.modules["ebooklib.epub"] = epub_module

if "bs4" not in sys.modules:
    bs4_module = types.ModuleType("bs4")
    bs4_module.BeautifulSoup = object
    sys.modules["bs4"] = bs4_module

if "websocket" not in sys.modules:
    websocket_module = types.ModuleType("websocket")
    websocket_module.WebSocket = object
    sys.modules["websocket"] = websocket_module

from core.pipeline import _apply_ai_marking_to_artifact, _sanitize_ffmpeg_metadata_value, combine_audio_files


def test_sanitize_ffmpeg_metadata_value_removes_newlines():
    assert _sanitize_ffmpeg_metadata_value("Chapter 2: I.\nIntroduction") == "Chapter 2: I. Introduction"
    assert _sanitize_ffmpeg_metadata_value("\n\n") is None


def test_combine_audio_files_retries_without_cover(tmp_path):
    segment = tmp_path / "segment.flac"
    segment.write_bytes(b"stub")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"stub")
    output = tmp_path / "chapter.flac"

    calls: list[list[str]] = []

    def fake_run(cmd, check, stdout=None, stderr=None, text=None):  # noqa: ANN001
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="mjpeg\n")
        if "-disposition:v" in cmd:
            raise subprocess.CalledProcessError(returncode=234, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("core.pipeline._apply_ai_marking_to_artifact") as apply_marking_mock, patch(
        "core.pipeline.subprocess.run", side_effect=fake_run
    ):
        assert combine_audio_files(
            [str(segment)],
            str(output),
            metadata={"title": "Chapter 2: I.\nIntroduction"},
            cover_image=str(cover),
        )
    apply_marking_mock.assert_called_once()

    ffmpeg_calls = [call for call in calls if call and call[0] == "ffmpeg"]
    assert len(ffmpeg_calls) == 2
    assert "-disposition:v" in ffmpeg_calls[0]
    assert "-disposition:v" not in ffmpeg_calls[1]
    assert "title=Chapter 2: I. Introduction" in ffmpeg_calls[1]


def test_apply_ai_marking_to_artifact_writes_manifest_even_when_metadata_tagging_fails(tmp_path):
    artifact = tmp_path / "segment.flac"
    artifact.write_bytes(b"audio")

    with patch("core.pipeline._embed_ai_marking_metadata_inplace", return_value=False), patch(
        "core.pipeline.watermark_audio_output_best_effort"
    ) as watermark_mock:
        watermark_mock.return_value = types.SimpleNamespace(
            applied=True,
            verified=True,
            detail="ok_default_public_key",
        )
        _apply_ai_marking_to_artifact(str(artifact), content_id="segment_001", logger=types.SimpleNamespace())

    manifest_path = artifact.with_suffix(".flac.ai.json")
    assert manifest_path.exists()
