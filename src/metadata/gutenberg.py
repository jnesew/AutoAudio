from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from metadata.models import BookMetadata


GUTENDEX_URL = "https://gutendex.com/books"


def fetch_gutenberg_metadata(gutenberg_id: str, timeout_seconds: float = 8.0) -> BookMetadata:
    query = urllib.parse.urlencode({"ids": gutenberg_id})
    url = f"{GUTENDEX_URL}?{query}"

    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return BookMetadata()

    results = payload.get("results") or []
    if not results:
        return BookMetadata()

    record = results[0]
    authors = record.get("authors") or []
    first_author = authors[0].get("name") if authors and isinstance(authors[0], dict) else None

    subjects = tuple(subject for subject in (record.get("subjects") or []) if isinstance(subject, str))

    return BookMetadata(
        title=record.get("title"),
        author=first_author,
        language=(record.get("languages") or [None])[0],
        subjects=subjects,
        identifier=str(record.get("id")) if record.get("id") is not None else None,
    )
