from __future__ import annotations

from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

from metadata.models import BookMetadata, ChapterMetadata


def _first_metadata(book: epub.EpubBook, namespace: str, key: str) -> str | None:
    values = book.get_metadata(namespace, key)
    if not values:
        return None
    return str(values[0][0]).strip() if values[0] and values[0][0] else None


def extract_epub_metadata(epub_path: str) -> BookMetadata:
    book = epub.read_epub(epub_path)

    chapters: list[ChapterMetadata] = []
    for idx, spine_item in enumerate(book.spine, start=1):
        item_id = spine_item[0] if isinstance(spine_item, tuple) else spine_item
        item = book.get_item_with_id(item_id)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_body_content(), "html.parser")
        heading = soup.find("h1") or soup.find("h2") or soup.find("title")
        title = heading.get_text(strip=True) if heading else f"Section {idx}"
        chapters.append(ChapterMetadata(index=len(chapters) + 1, title=title, source_id=item.get_id()))

    subjects = tuple(value[0].strip() for value in book.get_metadata("DC", "subject") if value and value[0])

    return BookMetadata(
        title=_first_metadata(book, "DC", "title"),
        author=_first_metadata(book, "DC", "creator"),
        language=_first_metadata(book, "DC", "language"),
        publisher=_first_metadata(book, "DC", "publisher"),
        rights=_first_metadata(book, "DC", "rights"),
        description=_first_metadata(book, "DC", "description"),
        identifier=_first_metadata(book, "DC", "identifier"),
        subjects=subjects,
        chapters=tuple(chapters),
    )


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

