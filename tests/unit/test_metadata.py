from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.metadata_adapters import M4bMetadataAdapter, Mp3MetadataAdapter, adapter_for_extension
from metadata.id_utils import guess_gutenberg_id
from metadata.models import BookMetadata, MetadataSources, merge_metadata
from metadata.source_mode import detect_source_mode


class MetadataMergeTests(unittest.TestCase):
    def test_merge_priority_user_embedded_fetched_fallback(self) -> None:
        merged = merge_metadata(
            MetadataSources(
                user=BookMetadata(title="User Title"),
                embedded=BookMetadata(title="Embedded Title", author="Embedded Author"),
                fetched=BookMetadata(title="Fetched Title", author="Fetched Author", language="en"),
                fallback=BookMetadata(title="Fallback", author="Fallback Author"),
            )
        )

        self.assertEqual(merged.title, "User Title")
        self.assertEqual(merged.author, "Embedded Author")
        self.assertEqual(merged.language, "en")

    def test_guess_gutenberg_id(self) -> None:
        self.assertEqual(guess_gutenberg_id("pg35-images.epub"), "35")
        self.assertEqual(guess_gutenberg_id("gutenberg_12345.txt"), "12345")




class SourceModeDetectionTests(unittest.TestCase):
    def test_detect_source_mode_accepts_supported_types(self) -> None:
        self.assertEqual(detect_source_mode("book.epub", "auto"), "epub")
        self.assertEqual(detect_source_mode("book.txt", "auto"), "text")
        self.assertEqual(detect_source_mode("book.jpeg", "text"), "text")

    def test_detect_source_mode_rejects_unsupported_extension(self) -> None:
        with self.assertRaises(ValueError):
            detect_source_mode("cover.jpeg", "auto")

class MetadataAdapterTests(unittest.TestCase):
    def test_adapter_selection(self) -> None:
        self.assertIsInstance(adapter_for_extension("book.mp3"), Mp3MetadataAdapter)
        self.assertIsInstance(adapter_for_extension("book.m4b"), M4bMetadataAdapter)

    def test_m4b_output_flags_include_ipod(self) -> None:
        adapter = M4bMetadataAdapter()
        self.assertIn("ipod", adapter.ffmpeg_output_args())


if __name__ == "__main__":
    unittest.main()
