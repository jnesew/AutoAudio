from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChapterMetadata:
    index: int
    title: str
    source_id: str | None = None


@dataclass(frozen=True)
class BookMetadata:
    title: str | None = None
    author: str | None = None
    language: str | None = None
    publisher: str | None = None
    rights: str | None = None
    description: str | None = None
    subjects: tuple[str, ...] = ()
    identifier: str | None = None
    chapters: tuple[ChapterMetadata, ...] = field(default_factory=tuple)
    cover_image_path: str | None = None


@dataclass(frozen=True)
class MetadataSources:
    user: BookMetadata
    embedded: BookMetadata
    fetched: BookMetadata
    fallback: BookMetadata


PRIORITY_FIELDS = (
    "title",
    "author",
    "language",
    "publisher",
    "rights",
    "description",
    "identifier",
    "cover_image_path",
)


def merge_metadata(sources: MetadataSources) -> BookMetadata:
    """Merge by priority user > embedded > fetched > fallback."""
    layered = [sources.user, sources.embedded, sources.fetched, sources.fallback]

    merged_values: dict[str, object] = {}
    for field_name in PRIORITY_FIELDS:
        merged_values[field_name] = next((getattr(md, field_name) for md in layered if getattr(md, field_name)), None)

    merged_subjects = next((md.subjects for md in layered if md.subjects), ())
    merged_chapters = next((md.chapters for md in layered if md.chapters), ())

    return BookMetadata(
        title=merged_values["title"],
        author=merged_values["author"],
        language=merged_values["language"],
        publisher=merged_values["publisher"],
        rights=merged_values["rights"],
        description=merged_values["description"],
        subjects=tuple(merged_subjects),
        identifier=merged_values["identifier"],
        chapters=tuple(merged_chapters),
        cover_image_path=merged_values["cover_image_path"],
    )
