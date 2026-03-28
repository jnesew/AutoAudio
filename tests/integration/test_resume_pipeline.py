from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from types import ModuleType
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Lightweight stubs so pipeline can be imported without optional deps.
if "ebooklib" not in sys.modules:
    ebooklib_stub = ModuleType("ebooklib")
    ebooklib_stub.ITEM_DOCUMENT = 9
    ebooklib_stub.ITEM_COVER = 10
    ebooklib_stub.ITEM_IMAGE = 11
    epub_stub = ModuleType("ebooklib.epub")
    epub_stub.read_epub = lambda *_args, **_kwargs: None
    ebooklib_stub.epub = epub_stub
    sys.modules["ebooklib"] = ebooklib_stub
    sys.modules["ebooklib.epub"] = epub_stub

if "bs4" not in sys.modules:
    bs4_stub = ModuleType("bs4")
    bs4_stub.BeautifulSoup = object
    sys.modules["bs4"] = bs4_stub

if "websocket" not in sys.modules:
    websocket_stub = ModuleType("websocket")
    websocket_stub.WebSocketTimeoutException = TimeoutError
    websocket_stub.create_connection = lambda *_args, **_kwargs: None
    sys.modules["websocket"] = websocket_stub

from core.config import AppConfig
from core.pipeline import run_pipeline


class ResumePipelineIntegrationTests(unittest.TestCase):
    def _build_args(self, *, input_book: Path, output_dir: Path, resume: str) -> argparse.Namespace:
        return argparse.Namespace(
            input_book=str(input_book),
            output_dir=str(output_dir),
            source_mode="text",
            pages_per_chapter=1,
            target_words_per_chapter=1000,
            min_paragraphs_per_chapter=1,
            chapters_per_part=10,
            max_words_per_chunk=4,
            diffusion_steps=25,
            temperature=0.95,
            top_p=0.95,
            cfg_scale=1.3,
            free_memory_after_generate=False,
            output_format="flac",
            fetch_metadata=False,
            gutenberg_id="",
            title="",
            author="",
            comfyui_mode="spoof",
            comfyui_server_address="127.0.0.1:8188",
            comfyui_timeout_seconds=5.0,
            comfyui_spoof_scenario="success",
            resume=resume,
        )

    def test_resume_after_interrupted_conversion_uses_checkpointed_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            input_book = tmp / "book.txt"
            # Enough text to create multiple segments with max_words_per_chunk=4
            input_book.write_text(
                "one two three four. five six seven eight. nine ten eleven twelve.", encoding="utf-8"
            )
            output_dir = tmp / "output"
            project_root = tmp / "project"
            (project_root / "resources" / "workflows").mkdir(parents=True)
            (project_root / "resources" / "workflows" / "vibevoice_single_speaker.json").write_text("{}", encoding="utf-8")

            config = AppConfig(project_root=project_root, comfyui_mode="spoof")

            def fake_combine(audio_files, output_filename, metadata=None, chapter_titles=None, cover_image=None):
                del audio_files, metadata, chapter_titles, cover_image
                Path(output_filename).write_bytes(b"combined")
                return True

            call_count = {"n": 0}

            def interrupted_process_segment(**kwargs):
                del kwargs
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return (b"RIFF....FAKEAUDIO-1", ".flac")
                raise RuntimeError("spoofed interruption")

            first_args = self._build_args(input_book=input_book, output_dir=output_dir, resume="auto")
            with patch("core.pipeline.combine_audio_files", side_effect=fake_combine), patch(
                "core.pipeline.process_segment", side_effect=interrupted_process_segment
            ):
                with self.assertRaises(RuntimeError):
                    run_pipeline(first_args, config)

            second_call_count = {"n": 0}

            def normal_process_segment(**kwargs):
                del kwargs
                second_call_count["n"] += 1
                return (b"RIFF....FAKEAUDIO-2", ".flac")

            second_args = self._build_args(input_book=input_book, output_dir=output_dir, resume="yes")
            with patch("core.pipeline.combine_audio_files", side_effect=fake_combine), patch(
                "core.pipeline.process_segment", side_effect=normal_process_segment
            ):
                run_pipeline(second_args, config)

            # One segment should have resumed from checkpoint, so only remaining segments regenerate.
            self.assertGreaterEqual(second_call_count["n"], 1)
            self.assertLess(second_call_count["n"], 3)

            checkpoint_file = project_root / "resources" / ".autoaudio_state" / "checkpoint_state.json"
            self.assertTrue(checkpoint_file.exists())
            self.assertIn("Part_001.flac", "\n".join([p.name for p in output_dir.iterdir()]))


if __name__ == "__main__":
    unittest.main()
