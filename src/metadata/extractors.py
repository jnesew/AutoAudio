from __future__ import annotations

from pathlib import Path

from metadata.epub_parser import read_epub_metadata
from metadata.models import BookMetadata


def extract_epub_metadata(epub_path: str) -> BookMetadata:
    return read_epub_metadata(epub_path)


def extract_text_fallback_metadata(text_path: str) -> BookMetadata:
    filename_title = Path(text_path).stem.replace("_", " ").strip() or "Untitled"
    author = None
    language = None
    subjects: tuple[str, ...] = ()

    with open(text_path, "r", encoding="utf-8", errors="ignore") as file:
        preview = file.read(5000)

    lines = [line.strip() for line in preview.splitlines() if line.strip()]

    for line in lines[:20]:
        lowered = line.lower()
        if lowered.startswith("title:"):
            filename_title = line.split(":", 1)[1].strip() or filename_title
        elif lowered.startswith("author:") or lowered.startswith("by "):
            author = line.split(":", 1)[1].strip() if ":" in line else line[3:].strip()
        elif lowered.startswith("language:"):
            language = line.split(":", 1)[1].strip()
        elif lowered.startswith("subject:"):
            subjects = tuple(part.strip() for part in line.split(":", 1)[1].split(",") if part.strip())

    return BookMetadata(title=filename_title, author=author, language=language, subjects=subjects)
