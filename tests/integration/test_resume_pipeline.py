from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from types import SimpleNamespace
from types import ModuleType
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Lightweight stub so pipeline can be imported without the network client dependency.
if "websocket" not in sys.modules:
    websocket_stub = ModuleType("websocket")
    websocket_stub.WebSocketTimeoutException = TimeoutError
    websocket_stub.create_connection = lambda *_args, **_kwargs: None
    sys.modules["websocket"] = websocket_stub

from core.checkpoint import CheckpointStore
from core.config import AppConfig
from core.pipeline import run_pipeline
from provenance.ai_marking import write_ai_marking_manifest


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
            target_words_per_segment=4,
            max_words_per_segment=4,
            disclosure_gap_ms=700,
            segment_gap_ms=150,
            chapter_gap_ms=1000,
            narrator_profile="preset-eric-neutral",
            speaker="Eric",
            voice_instruct="",
            model_choice="1.7B",
            device="auto",
            precision="bf16",
            language="English",
            seed=1234,
            max_new_tokens=2048,
            temperature=1.0,
            top_p=0.8,
            top_k=20,
            repetition_penalty=1.05,
            attention="sdpa",
            unload_model_after_generate=False,
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
            provenance_enabled=False,
            provenance_cert_path="",
            provenance_key_path="",
            provenance_key_password="",
            provenance_failure_mode="soft-fail",
            provenance_tool="c2patool",
            provenance_claim_generator="autoaudio",
        )

    def test_resume_after_interrupted_conversion_uses_checkpointed_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            input_book = tmp / "book.txt"
            # Enough text to create multiple segments with a four-word hard limit.
            input_book.write_text(
                "one two three four. five six seven eight. nine ten eleven twelve.", encoding="utf-8"
            )
            output_dir = tmp / "output"
            project_root = tmp / "project"
            (project_root / "resources" / "workflows").mkdir(parents=True)
            (project_root / "resources" / "workflows" / "qwen3_tts_custom_voice.json").write_text("{}", encoding="utf-8")
            (project_root / "resources" / "narrators").mkdir(parents=True)
            bundled_profiles = PROJECT_ROOT / "resources" / "narrators" / "default_profiles.json"
            (project_root / "resources" / "narrators" / "default_profiles.json").write_text(
                bundled_profiles.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            config = AppConfig(project_root=project_root, comfyui_mode="spoof")
            assembled_inputs: list[list[str]] = []

            def fake_assemble(audio_files, output_filename, **kwargs):
                del kwargs
                assembled_inputs.append([str(path) for path in audio_files])
                Path(output_filename).write_bytes(b"lossless-master")
                write_ai_marking_manifest(
                    output_filename,
                    content_id=Path(output_filename).stem,
                    metadata_embedded=True,
                    watermark_applied=True,
                    watermark_verified=True,
                    watermark_detail="test",
                )
                return str(output_filename)

            def fake_final_encode(master_path, output_filename, **kwargs):
                del master_path, kwargs
                Path(output_filename).write_bytes(b"final-audio")
                write_ai_marking_manifest(
                    output_filename,
                    content_id=Path(output_filename).stem,
                    metadata_embedded=True,
                    watermark_applied=True,
                    watermark_verified=True,
                    watermark_detail="test",
                )
                return str(output_filename)

            def fake_disclosure_asset(**kwargs):
                disclosure_path = Path(kwargs["state_dir"]) / "chapter_disclosure.flac"
                disclosure_path.parent.mkdir(parents=True, exist_ok=True)
                disclosure_path.write_bytes(b"disclosure")
                return str(disclosure_path)

            call_count = {"n": 0}

            def interrupted_process_segment(**kwargs):
                del kwargs
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return (b"RIFF....FAKEAUDIO-1", ".flac")
                raise RuntimeError("spoofed interruption")

            first_args = self._build_args(input_book=input_book, output_dir=output_dir, resume="auto")
            def fake_encode(command, **kwargs):
                del kwargs
                Path(command[-1]).write_bytes(b"encoded-audio")
                return SimpleNamespace(returncode=0)

            marking_result = SimpleNamespace(applied=True, verified=True, detail="test watermark")
            with patch("core.pipeline.assemble_lossless_master", side_effect=fake_assemble), patch(
                "core.pipeline.encode_lossless_master", side_effect=fake_final_encode
            ), patch(
                "core.pipeline.chapter_markers_for_files", return_value=()
            ), patch(
                "core.pipeline.process_segment", side_effect=interrupted_process_segment
            ), patch("core.pipeline.watermark_audio_bytes_best_effort", return_value=(marking_result, b"marked")), patch(
                "core.pipeline.subprocess.run", side_effect=fake_encode
            ), patch(
                "core.pipeline.ensure_disclosure_asset", side_effect=fake_disclosure_asset
            ):
                with self.assertRaises(RuntimeError):
                    run_pipeline(first_args, config)

            second_call_count = {"n": 0}

            def normal_process_segment(**kwargs):
                del kwargs
                second_call_count["n"] += 1
                return (b"RIFF....FAKEAUDIO-2", ".flac")

            second_args = self._build_args(input_book=input_book, output_dir=output_dir, resume="yes")
            with patch("core.pipeline.assemble_lossless_master", side_effect=fake_assemble), patch(
                "core.pipeline.encode_lossless_master", side_effect=fake_final_encode
            ), patch(
                "core.pipeline.chapter_markers_for_files", return_value=()
            ), patch(
                "core.pipeline.process_segment", side_effect=normal_process_segment
            ), patch("core.pipeline.watermark_audio_bytes_best_effort", return_value=(marking_result, b"marked")), patch(
                "core.pipeline.subprocess.run", side_effect=fake_encode
            ), patch(
                "core.pipeline.ensure_disclosure_asset", side_effect=fake_disclosure_asset
            ):
                run_pipeline(second_args, config)

            # One segment should have resumed from checkpoint, so only remaining segments regenerate.
            self.assertGreaterEqual(second_call_count["n"], 1)
            self.assertLess(second_call_count["n"], 3)

            checkpoint_file = output_dir / ".autoaudio_state" / "checkpoint_state.json"
            self.assertTrue(checkpoint_file.exists())
            self.assertTrue((output_dir / ".autoaudio_state" / "book_plan.json").exists())
            self.assertIn("Part_001.flac", "\n".join([p.name for p in output_dir.iterdir()]))
            chapter_inputs = [files for files in assembled_inputs if Path(files[0]).name == "chapter_disclosure.flac"]
            self.assertTrue(chapter_inputs)
            self.assertTrue(all(sum(Path(path).name == "chapter_disclosure.flac" for path in files) == 1 for files in chapter_inputs))
            self.assertTrue(all(Path(files[1]).name.startswith("disclosure_gap_") for files in chapter_inputs))
            self.assertFalse(list((output_dir / ".segments").glob("*.ai.json")))
            self.assertFalse(list((output_dir / ".segments").glob("*.flac")))
            self.assertFalse(list((output_dir / ".autoaudio_state" / "masters").glob("*")))

            # A failure after a completed part may resume without its intentionally cleaned chapter masters.
            checkpoint_store = CheckpointStore(output_dir / ".autoaudio_state")
            completed_checkpoint = checkpoint_store.load()
            self.assertFalse(completed_checkpoint["artifacts"]["chapter_masters"])
            self.assertFalse(completed_checkpoint["artifacts"]["part_masters"])
            completed_checkpoint["status"] = "failed"
            checkpoint_store.save(completed_checkpoint)
            third_args = self._build_args(input_book=input_book, output_dir=output_dir, resume="yes")
            with patch(
                "core.pipeline.process_segment", side_effect=AssertionError("completed part regenerated narration")
            ), patch(
                "core.pipeline.assemble_lossless_master",
                side_effect=AssertionError("completed part was assembled again"),
            ), patch(
                "core.pipeline.encode_lossless_master",
                side_effect=AssertionError("completed part was encoded again"),
            ), patch(
                "core.pipeline.ensure_disclosure_asset", side_effect=fake_disclosure_asset
            ):
                run_pipeline(third_args, config)


if __name__ == "__main__":
    unittest.main()
