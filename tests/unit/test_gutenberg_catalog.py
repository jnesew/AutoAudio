from __future__ import annotations

import io
import zipfile

import pytest

from metadata.gutenberg_catalog import (
    GutenbergCatalogClient,
    GutenbergCatalogError,
    parse_gutenberg_opds,
)


OPDS = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dcterms="http://purl.org/dc/terms/">
  <link rel="next" href="/ebooks/search.opds/?query=alice&amp;start_index=26" />
  <entry>
    <id>https://www.gutenberg.org/ebooks/11</id>
    <title>Alice's Adventures in Wonderland</title>
    <author><name>Carroll, Lewis</name></author>
    <dcterms:language>en</dcterms:language>
    <rights>Public domain in the USA.</rights>
    <summary>Alice follows a white rabbit.</summary>
    <link rel="alternate" type="text/html" href="/ebooks/11" />
    <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
          title="EPUB (with images)" length="12345" href="/ebooks/11.epub.images" />
  </entry>
</feed>
"""


class FakeResponse:
    def __init__(self, payload: bytes, url: str, content_length: int | None = None):
        self.payload = payload
        self.url = url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int = -1):
        return self.payload if amount < 0 else self.payload[:amount]

    def geturl(self):
        return self.url


def _epub_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container />")
    return output.getvalue()


def test_parse_gutenberg_opds_extracts_metadata_acquisition_and_manual_next_page():
    page = parse_gutenberg_opds(OPDS, base_url="https://www.gutenberg.org/ebooks/search.opds/?query=alice")

    assert len(page.books) == 1
    book = page.books[0]
    assert book.gutenberg_id == "11"
    assert book.author_text == "Carroll, Lewis"
    assert book.preferred_epub is not None
    assert book.preferred_epub.length == 12345
    assert page.next_url == "https://www.gutenberg.org/ebooks/search.opds/?query=alice&start_index=26"


def test_client_search_uses_contact_user_agent_and_caches_identical_page():
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return FakeResponse(OPDS, request.full_url)

    client = GutenbergCatalogClient(opener=opener, minimum_request_interval=0)
    first = client.search("alice")
    second = client.search("alice")

    assert first is second
    assert len(requests) == 1
    assert "github.com/jnesew/AutoAudio" in requests[0].get_header("User-agent")
    assert "query=alice" in requests[0].full_url


def test_download_epub_is_validated_written_atomically_and_records_provenance(tmp_path):
    page = parse_gutenberg_opds(OPDS, base_url="https://www.gutenberg.org/ebooks/search.opds/?query=alice")
    book = page.books[0]
    acquisition = book.preferred_epub
    assert acquisition is not None
    epub = _epub_bytes()

    def opener(request, **_kwargs):
        return FakeResponse(epub, request.full_url, len(epub))

    client = GutenbergCatalogClient(opener=opener, minimum_request_interval=0)
    destination = client.download_epub(book, acquisition, tmp_path / "books")

    assert destination.name == "pg11.epub"
    assert zipfile.is_zipfile(destination)
    sidecar = destination.with_suffix(".epub.autoaudio-source.json")
    assert sidecar.is_file()
    assert '"gutenberg_id": "11"' in sidecar.read_text(encoding="utf-8")
    assert not list(destination.parent.glob("*.part"))


def test_catalog_rejects_non_gutenberg_pagination_or_acquisition_urls():
    unsafe = OPDS.replace(
        b'/ebooks/search.opds/?query=alice&amp;start_index=26',
        b'https://example.com/next',
    )

    with pytest.raises(GutenbergCatalogError, match="unsupported"):
        parse_gutenberg_opds(unsafe, base_url="https://www.gutenberg.org/ebooks/search.opds/?query=alice")


def test_catalog_rejects_xml_entities_before_parsing():
    payload = b'<!DOCTYPE feed [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><feed>&xxe;</feed>'

    with pytest.raises(GutenbergCatalogError, match="XML declarations"):
        parse_gutenberg_opds(payload, base_url="https://www.gutenberg.org/ebooks/search.opds/")


def test_download_rejects_non_epub_payload_without_leaving_a_partial_file(tmp_path):
    page = parse_gutenberg_opds(OPDS, base_url="https://www.gutenberg.org/ebooks/search.opds/?query=alice")
    book = page.books[0]
    acquisition = book.preferred_epub
    assert acquisition is not None
    client = GutenbergCatalogClient(
        opener=lambda request, **_kwargs: FakeResponse(b"not an epub", request.full_url),
        minimum_request_interval=0,
    )

    with pytest.raises(GutenbergCatalogError, match="valid EPUB"):
        client.download_epub(book, acquisition, tmp_path / "books")

    assert not list((tmp_path / "books").iterdir())
