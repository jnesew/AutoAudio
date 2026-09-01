from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pubparser import ParsingMode, normalize_project_gutenberg, open_epub

from metadata.models import BookMetadata, ChapterMetadata


EPUB_PARSER_POLICY_VERSION = "pubparser-v0.1.1-gutenberg-v1"
_COVER_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
}


class EpubParseError(RuntimeError):
    """Raised when an EPUB cannot be converted into AutoAudio's input model."""


@dataclass(frozen=True)
class EpubDiagnostic:
    severity: str
    code: str
    message: str
    resource: str | None = None


@dataclass(frozen=True)
class EpubCover:
    filename: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class ParsedEpub:
    metadata: BookMetadata
    text_blocks: tuple[tuple[str, str], ...]
    cover: EpubCover | None
    diagnostics: tuple[EpubDiagnostic, ...]
    gutenberg_detected: bool
    gutenberg_changed: bool


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _cover_filename(href: str, media_type: str) -> str:
    candidate = Path(unquote(urlsplit(href).path)).name
    if candidate and Path(candidate).suffix:
        return candidate

    extension = _COVER_EXTENSIONS.get(media_type, ".img")
    return f"cover{extension}"


def parse_epub(epub_path: str | Path) -> ParsedEpub:
    """Parse an EPUB once into normalized text, metadata, cover bytes, and diagnostics."""
    source = Path(epub_path)
    if not source.is_file():
        raise EpubParseError(f"EPUB file not found: {source}")

    try:
        with open_epub(source, mode=ParsingMode.COMPATIBILITY) as book:
            if book.encryption.has_unsupported_drm:
                raise EpubParseError("EPUB contains unsupported encrypted/DRM-protected resources.")

            normalization = normalize_project_gutenberg(book)
            diagnostics = [
                EpubDiagnostic(
                    severity=str(item.severity),
                    code=item.code,
                    message=item.message,
                    resource=item.resource,
                )
                for item in book.diagnostics
            ]
            diagnostics.extend(
                EpubDiagnostic(
                    severity="warning",
                    code="AUTOAUDIO_GUTENBERG_NORMALIZATION_WARNING",
                    message=warning,
                )
                for warning in normalization.warnings
            )

            text_blocks: list[tuple[str, str]] = []
            chapters: list[ChapterMetadata] = []
            for document in book.iter_documents(normalization=normalization):
                text = _clean(document.text)
                if text is None or len(text) <= 50:
                    continue
                title = _clean(document.title) or document.resource.id
                text_blocks.append((title, text))
                chapters.append(
                    ChapterMetadata(
                        index=len(chapters) + 1,
                        title=title,
                        source_id=document.resource.id,
                    )
                )

            package_metadata = book.metadata
            subjects = tuple(
                cleaned
                for value in package_metadata.all("subject")
                if (cleaned := _clean(value)) is not None
            )
            metadata = BookMetadata(
                title=_clean(package_metadata.primary_title),
                author=_clean(package_metadata.primary_author),
                language=_clean(package_metadata.primary_language),
                publisher=_clean(package_metadata.first("publisher")),
                rights=_clean(package_metadata.first("rights")),
                description=_clean(package_metadata.first("description")),
                identifier=_clean(package_metadata.primary_identifier),
                subjects=subjects,
                chapters=tuple(chapters),
            )

            cover: EpubCover | None = None
            if book.cover is not None:
                cover_item = book.cover.resource
                try:
                    cover_content = book.read_resource(cover_item.id)
                except Exception as exc:
                    diagnostics.append(
                        EpubDiagnostic(
                            severity="warning",
                            code="AUTOAUDIO_COVER_READ_FAILED",
                            message=str(exc),
                            resource=cover_item.href,
                        )
                    )
                else:
                    cover = EpubCover(
                        filename=_cover_filename(cover_item.href, cover_item.media_type),
                        media_type=cover_item.media_type,
                        content=cover_content,
                    )

            return ParsedEpub(
                metadata=metadata,
                text_blocks=tuple(text_blocks),
                cover=cover,
                diagnostics=tuple(diagnostics),
                gutenberg_detected=normalization.detected,
                gutenberg_changed=normalization.changed,
            )
    except EpubParseError:
        raise
    except Exception as exc:
        raise EpubParseError(f"Could not parse EPUB {source}: {exc}") from exc


def write_cover_art(parsed_epub: ParsedEpub, output_dir: str | Path) -> str | None:
    if parsed_epub.cover is None:
        return None

    declared_suffix = Path(parsed_epub.cover.filename).suffix.lower()
    suffix = _COVER_EXTENSIONS.get(parsed_epub.cover.media_type)
    if suffix is None:
        suffix = declared_suffix if declared_suffix in set(_COVER_EXTENSIONS.values()) else ".img"

    output_path = Path(output_dir) / f"cover{suffix}"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(parsed_epub.cover.content)
    return str(output_path)
