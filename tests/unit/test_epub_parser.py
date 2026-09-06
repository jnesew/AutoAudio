from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from metadata.epub_parser import (
    EPUB_PARSER_POLICY_VERSION,
    EpubParseError,
    parse_epub,
    read_epub_metadata,
    write_cover_art,
)


def _write_fixture(path: Path) -> None:
    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    package_xml = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">https://www.gutenberg.org/ebooks/99999</dc:identifier>
    <dc:title>Fixture Book</dc:title>
    <dc:creator>Example Author</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Fixture Press</dc:publisher>
    <dc:rights>Public domain</dc:rights>
    <dc:description>A parser integration fixture.</dc:description>
    <dc:subject>Testing</dc:subject>
    <dc:subject>Audiobooks</dc:subject>
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="chapter-one" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="chapter-two" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>
  </manifest>
  <spine>
    <itemref idref="chapter-one"/>
    <itemref idref="chapter-two"/>
  </spine>
</package>
"""
    chapter_one = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Opening</title></head><body>
<h1>Chapter One</h1>
<p>*** START OF THE PROJECT GUTENBERG EBOOK FIXTURE BOOK ***</p>
<p>This is the first real paragraph of the book, and it is deliberately long enough
to survive AutoAudio's document filter without special handling.</p>
</body></html>
"""
    chapter_two = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Closing</title></head><body>
<h1>Chapter Two</h1>
<p>This is the second chapter's narrative text, which must remain available after
Project Gutenberg footer normalization has completed.</p>
<p>*** END OF THE PROJECT GUTENBERG EBOOK FIXTURE BOOK ***</p>
<p>The full distribution license must not enter a synthesis segment.</p>
</body></html>
"""

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container_xml)
        archive.writestr("OEBPS/content.opf", package_xml)
        archive.writestr("OEBPS/chapter1.xhtml", chapter_one)
        archive.writestr("OEBPS/chapter2.xhtml", chapter_two)
        archive.writestr("OEBPS/images/cover.jpg", b"\xff\xd8\xff\xe0fixture-cover")


def test_parse_epub_builds_autoaudio_snapshot_and_normalizes_gutenberg(tmp_path):
    source = tmp_path / "fixture.epub"
    _write_fixture(source)

    parsed = parse_epub(source)

    assert EPUB_PARSER_POLICY_VERSION == "pubparser-document-semantics-v1-gutenberg-v1"
    assert parsed.metadata.title == "Fixture Book"
    assert parsed.metadata.author == "Example Author"
    assert parsed.metadata.language == "en"
    assert parsed.metadata.publisher == "Fixture Press"
    assert parsed.metadata.rights == "Public domain"
    assert parsed.metadata.identifier == "https://www.gutenberg.org/ebooks/99999"
    assert parsed.metadata.subjects == ("Testing", "Audiobooks")
    assert [chapter.title for chapter in parsed.metadata.chapters] == ["Chapter One", "Chapter Two"]
    assert [chapter.source_id for chapter in parsed.metadata.chapters] == ["chapter-one", "chapter-two"]
    assert [title for title, _text in parsed.text_blocks] == ["Chapter One", "Chapter Two"]
    combined_text = " ".join(text for _title, text in parsed.text_blocks)
    assert "START OF THE PROJECT GUTENBERG EBOOK" not in combined_text
    assert "END OF THE PROJECT GUTENBERG EBOOK" not in combined_text
    assert "full distribution license" not in combined_text
    assert "first real paragraph" in combined_text
    assert "second chapter's narrative text" in combined_text
    assert parsed.gutenberg_detected
    assert parsed.gutenberg_changed
    assert parsed.cover is not None
    assert parsed.cover.filename == "cover.jpg"
    assert parsed.cover.media_type == "image/jpeg"
    assert parsed.cover.content.startswith(b"\xff\xd8\xff")


def test_write_cover_art_uses_safe_output_name(tmp_path):
    source = tmp_path / "fixture.epub"
    _write_fixture(source)
    parsed = parse_epub(source)

    cover_path = write_cover_art(parsed, tmp_path / "output")

    assert cover_path == str(tmp_path / "output" / "cover.jpg")
    assert Path(cover_path).read_bytes() == parsed.cover.content


def test_read_epub_metadata_skips_spine_text_parsing(tmp_path):
    source = tmp_path / "fixture.epub"
    _write_fixture(source)

    metadata = read_epub_metadata(source)

    assert metadata.title == "Fixture Book"
    assert metadata.author == "Example Author"
    assert metadata.language == "en"
    assert metadata.chapters == ()


def test_parse_epub_reports_missing_file(tmp_path):
    with pytest.raises(EpubParseError, match="file not found"):
        parse_epub(tmp_path / "missing.epub")


def test_parse_epub_omits_confident_toc_and_allows_override(tmp_path):
    source = tmp_path / "toc.epub"
    container_xml = """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    package_xml = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>TOC fixture</dc:title></metadata>
      <manifest>
        <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
        <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
      </manifest>
      <spine><itemref idref="nav"/><itemref idref="chapter"/></spine>
    </package>"""
    nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
      <nav epub:type="toc"><h1>Contents</h1><ol>
        <li><a href="chapter.xhtml#one">Chapter One and its opening</a></li>
        <li><a href="chapter.xhtml#two">Chapter Two and its continuation</a></li>
        <li><a href="chapter.xhtml#three">Chapter Three and its conclusion</a></li>
      </ol></nav></body></html>"""
    chapter = """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter One</h1>
      <p>This narrative document is deliberately long enough to survive the AutoAudio length filter.</p>
    </body></html>"""
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container_xml)
        archive.writestr("EPUB/package.opf", package_xml)
        archive.writestr("EPUB/nav.xhtml", nav)
        archive.writestr("EPUB/chapter.xhtml", chapter)

    parsed = parse_epub(source)
    included = parse_epub(source, narrate_toc=True)

    assert parsed.omitted_toc_documents == ("nav",)
    assert [title for title, _text in parsed.text_blocks] == ["Chapter One"]
    assert any(item.code == "AUTOAUDIO_TOC_OMITTED" for item in parsed.diagnostics)
    assert [title for title, _text in included.text_blocks] == ["Contents", "Chapter One"]
    assert included.omitted_toc_documents == ()
