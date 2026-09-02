from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Protocol
from xml.etree import ElementTree

from core.version import AUTOAUDIO_VERSION


GUTENBERG_OPDS_SEARCH_URL = "https://www.gutenberg.org/ebooks/search.opds/"
GUTENBERG_REPOSITORY_CONTACT = "https://github.com/jnesew/AutoAudio"
GUTENBERG_ALLOWED_HOSTS = frozenset({"gutenberg.org", "www.gutenberg.org"})
DEFAULT_MAX_CATALOG_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_EPUB_BYTES = 100 * 1024 * 1024
ATOM_NS = "http://www.w3.org/2005/Atom"
DCTERMS_NS = "http://purl.org/dc/terms/"
OPDS_ACQUISITION_PREFIX = "http://opds-spec.org/acquisition"


class GutenbergCatalogError(RuntimeError):
    """Raised when the catalog or selected acquisition cannot be used safely."""


@dataclass(frozen=True)
class GutenbergAcquisition:
    url: str
    mime_type: str
    title: str
    length: int | None = None


@dataclass(frozen=True)
class GutenbergBook:
    gutenberg_id: str
    title: str
    authors: tuple[str, ...]
    language: str
    rights: str
    summary: str
    landing_url: str
    acquisitions: tuple[GutenbergAcquisition, ...]

    @property
    def author_text(self) -> str:
        return ", ".join(self.authors) if self.authors else "Unknown"

    @property
    def preferred_epub(self) -> GutenbergAcquisition | None:
        epubs = [item for item in self.acquisitions if item.mime_type == "application/epub+zip"]
        if not epubs:
            return None
        return next((item for item in epubs if "with images" in item.title.casefold()), epubs[0])


@dataclass(frozen=True)
class GutenbergSearchPage:
    books: tuple[GutenbergBook, ...]
    next_url: str | None = None


class CatalogProvider(Protocol):
    def search(self, query: str, *, page_url: str | None = None) -> GutenbergSearchPage: ...


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _validated_gutenberg_url(url: str, *, base_url: str = GUTENBERG_OPDS_SEARCH_URL) -> str:
    absolute = urllib.parse.urljoin(base_url, url)
    parsed = urllib.parse.urlsplit(absolute)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in GUTENBERG_ALLOWED_HOSTS:
        raise GutenbergCatalogError("Project Gutenberg returned an unsupported or non-HTTPS URL.")
    if parsed.username or parsed.password:
        raise GutenbergCatalogError("Project Gutenberg URL unexpectedly contained credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise GutenbergCatalogError("Project Gutenberg URL contained an invalid port.") from exc
    if port not in {None, 443}:
        raise GutenbergCatalogError("Project Gutenberg URL unexpectedly used a non-standard port.")
    return absolute


class _GutenbergRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validated_gutenberg_url(newurl, base_url=req.full_url)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_GUTENBERG_OPENER = urllib.request.build_opener(_GutenbergRedirectHandler()).open


def _read_limited(response: BinaryIO, maximum: int) -> bytes:
    headers = getattr(response, "headers", {})
    raw_length = headers.get("Content-Length") if hasattr(headers, "get") else None
    if raw_length:
        try:
            if int(raw_length) > maximum:
                raise GutenbergCatalogError("Project Gutenberg response exceeds the configured size limit.")
        except ValueError:
            pass
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise GutenbergCatalogError("Project Gutenberg response exceeds the configured size limit.")
    return payload


def parse_gutenberg_opds(payload: bytes, *, base_url: str) -> GutenbergSearchPage:
    """Parse one OPDS1 page without resolving external XML entities."""
    lowered_prefix = payload[:4096].lower()
    if b"<!doctype" in lowered_prefix or b"<!entity" in lowered_prefix:
        raise GutenbergCatalogError("Project Gutenberg returned unsupported XML declarations.")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise GutenbergCatalogError(f"Project Gutenberg returned malformed OPDS XML: {exc}") from exc

    def text(parent, tag: str) -> str:
        node = parent.find(tag)
        return _clean(node.text if node is not None else "")

    next_url: str | None = None
    for link in root.findall(f"{{{ATOM_NS}}}link"):
        if link.get("rel") == "next" and link.get("href"):
            next_url = _validated_gutenberg_url(link.get("href", ""), base_url=base_url)
            break

    books: list[GutenbergBook] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        entry_id = text(entry, f"{{{ATOM_NS}}}id")
        match = re.search(r"/ebooks/(\d+)", entry_id)
        if not match:
            continue
        gutenberg_id = match.group(1)
        acquisitions: list[GutenbergAcquisition] = []
        landing_url = f"https://www.gutenberg.org/ebooks/{gutenberg_id}"
        for link in entry.findall(f"{{{ATOM_NS}}}link"):
            href = link.get("href")
            if not href:
                continue
            rel = link.get("rel", "")
            mime_type = link.get("type", "")
            if rel == "alternate" and mime_type.startswith("text/html"):
                landing_url = _validated_gutenberg_url(href, base_url=base_url)
            if not rel.startswith(OPDS_ACQUISITION_PREFIX):
                continue
            try:
                length = int(link.get("length", ""))
            except ValueError:
                length = None
            acquisitions.append(
                GutenbergAcquisition(
                    url=_validated_gutenberg_url(href, base_url=base_url),
                    mime_type=mime_type,
                    title=_clean(link.get("title")) or mime_type or "Download",
                    length=length,
                )
            )
        authors = tuple(
            name
            for author in entry.findall(f"{{{ATOM_NS}}}author")
            if (name := text(author, f"{{{ATOM_NS}}}name"))
        )
        books.append(
            GutenbergBook(
                gutenberg_id=gutenberg_id,
                title=text(entry, f"{{{ATOM_NS}}}title") or f"Project Gutenberg #{gutenberg_id}",
                authors=authors,
                language=text(entry, f"{{{DCTERMS_NS}}}language") or "unknown",
                rights=(
                    text(entry, f"{{{ATOM_NS}}}rights")
                    or text(entry, f"{{{DCTERMS_NS}}}rights")
                    or "Public-domain status varies by jurisdiction."
                ),
                summary=(
                    text(entry, f"{{{ATOM_NS}}}summary")
                    or text(entry, f"{{{ATOM_NS}}}content")
                ),
                landing_url=landing_url,
                acquisitions=tuple(acquisitions),
            )
        )
    return GutenbergSearchPage(books=tuple(books), next_url=next_url)


class GutenbergCatalogClient:
    """Rate-limited, user-initiated Project Gutenberg OPDS1 client."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        minimum_request_interval: float = 1.0,
        opener: Callable[..., object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.minimum_request_interval = max(0.0, minimum_request_interval)
        self.opener = opener or _SAFE_GUTENBERG_OPENER
        self.clock = clock
        self.sleeper = sleeper
        self._request_lock = threading.Lock()
        self._last_request_at: float | None = None
        self._cache: dict[str, GutenbergSearchPage] = {}

    @property
    def user_agent(self) -> str:
        return f"AutoAudio/{AUTOAUDIO_VERSION} (+{GUTENBERG_REPOSITORY_CONTACT})"

    def _request(self, url: str, *, accept: str, maximum_bytes: int) -> bytes:
        validated_url = _validated_gutenberg_url(url)
        with self._request_lock:
            if self._last_request_at is not None:
                remaining = self.minimum_request_interval - (self.clock() - self._last_request_at)
                if remaining > 0:
                    self.sleeper(remaining)
            request = urllib.request.Request(
                validated_url,
                headers={"User-Agent": self.user_agent, "Accept": accept},
            )
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    final_url = response.geturl() if hasattr(response, "geturl") else validated_url
                    _validated_gutenberg_url(str(final_url))
                    return _read_limited(response, maximum_bytes)
            except GutenbergCatalogError:
                raise
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                raise GutenbergCatalogError(f"Project Gutenberg request failed: {exc}") from exc
            finally:
                self._last_request_at = self.clock()

    def search(self, query: str, *, page_url: str | None = None) -> GutenbergSearchPage:
        normalized_query = _clean(query)
        if page_url is None:
            if not normalized_query:
                raise GutenbergCatalogError("Enter a title, author, or search term.")
            if len(normalized_query) > 300:
                raise GutenbergCatalogError("Project Gutenberg search terms must be 300 characters or fewer.")
            url = f"{GUTENBERG_OPDS_SEARCH_URL}?{urllib.parse.urlencode({'query': normalized_query})}"
        else:
            url = _validated_gutenberg_url(page_url)
        if url in self._cache:
            return self._cache[url]
        payload = self._request(
            url,
            accept="application/atom+xml;profile=opds-catalog, application/atom+xml;q=0.9",
            maximum_bytes=DEFAULT_MAX_CATALOG_BYTES,
        )
        page = parse_gutenberg_opds(payload, base_url=url)
        self._cache[url] = page
        return page

    def download_epub(
        self,
        book: GutenbergBook,
        acquisition: GutenbergAcquisition,
        books_dir: str | Path,
        *,
        maximum_bytes: int = DEFAULT_MAX_EPUB_BYTES,
    ) -> Path:
        if acquisition.mime_type != "application/epub+zip":
            raise GutenbergCatalogError("The selected Project Gutenberg item is not an EPUB.")
        if acquisition.length is not None and acquisition.length > maximum_bytes:
            raise GutenbergCatalogError("The selected EPUB exceeds the configured size limit.")
        payload = self._request(
            acquisition.url,
            accept="application/epub+zip, application/octet-stream;q=0.8",
            maximum_bytes=maximum_bytes,
        )

        destination_dir = Path(books_dir).resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"pg{book.gutenberg_id}.epub"
        if destination.exists():
            raise GutenbergCatalogError(f"{destination.name} is already present in the books directory.")

        fd, temp_name = tempfile.mkstemp(prefix=f".pg{book.gutenberg_id}.", suffix=".part", dir=destination_dir)
        sidecar = destination.with_suffix(destination.suffix + ".autoaudio-source.json")
        sidecar_fd: int | None = None
        sidecar_temp_name: str | None = None
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(payload)
            temp_path = Path(temp_name)
            if not zipfile.is_zipfile(temp_path):
                raise GutenbergCatalogError("Downloaded data is not a valid EPUB/ZIP container.")
            with zipfile.ZipFile(temp_path) as archive:
                names = set(archive.namelist())
                if "META-INF/container.xml" not in names and "mimetype" not in names:
                    raise GutenbergCatalogError("Downloaded ZIP does not contain an EPUB package marker.")
            source_record = {
                "provider": "Project Gutenberg",
                "gutenberg_id": book.gutenberg_id,
                "title": book.title,
                "authors": list(book.authors),
                "language": book.language,
                "rights": book.rights,
                "landing_url": book.landing_url,
                "acquisition_url": acquisition.url,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
            sidecar_fd, sidecar_temp_name = tempfile.mkstemp(
                prefix=f".pg{book.gutenberg_id}.", suffix=".source.part", dir=destination_dir
            )
            with os.fdopen(sidecar_fd, "w", encoding="utf-8") as file:
                sidecar_fd = None
                json.dump(source_record, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temp_path, destination)
            try:
                os.replace(sidecar_temp_name, sidecar)
            except OSError:
                destination.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise GutenbergCatalogError(f"Could not save the selected Project Gutenberg EPUB: {exc}") from exc
        finally:
            if sidecar_fd is not None:
                os.close(sidecar_fd)
            for temporary in (temp_name, sidecar_temp_name):
                if temporary and os.path.exists(temporary):
                    os.remove(temporary)
        return destination
