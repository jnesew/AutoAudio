"""Local drafts; no secrets, network activity, or automatic job execution."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class DraftStore:
    def __init__(self, path: Path):
        self.path = path
        self.jobs: dict[str, dict] = {}
        self.selected: str | None = None
        self.locations: dict[str, str] = {}
        self.error = ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("jobs"), dict)
            ):
                raise ValueError("Unsupported GUI draft file")
            self.jobs = {
                key: value
                for key, value in data["jobs"].items()
                if isinstance(key, str) and isinstance(value, dict)
            }
            selected = data.get("selected")
            self.selected = selected if isinstance(selected, str) else None
            locations = data.get("locations", {})
            if isinstance(locations, dict):
                self.locations = {
                    key: value
                    for key, value in locations.items()
                    if isinstance(value, str)
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.error = (
                f"Could not load drafts: {exc}. The existing file has been preserved."
            )

    def save(self):
        if self.error:
            return  # Preserve unreadable state for recovery.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix="drafts-", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "selected": self.selected,
                        "jobs": self.jobs,
                        "locations": self.locations,
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)


def fresh_output(root: str | Path, source: str | Path) -> Path:
    from uuid import uuid4

    safe_stem = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in Path(source).stem
    )[:60]
    return Path(root).resolve() / f"{safe_stem or 'book'}-{uuid4().hex[:10]}"


def merge_saved_jobs(entries, states, cache):
    """Read saved run summaries off the GUI thread, including manual sources."""
    from core.checkpoint import sha256_file
    from core.library import LibraryBook, _job_summary
    from gui.preview import file_identity

    books = {entry.id: entry for entry in entries}
    for key, state in states.items():
        source, output = state.get("input_edit"), state.get("output_edit")
        if (
            not isinstance(source, str)
            or not isinstance(output, str)
            or not source
            or not output
        ):
            continue
        original = books.get(key)
        title = state.get("title_edit") or (
            original.title if original else Path(source).stem
        )
        author = state.get("author_edit") or (
            original.author if original else "Unknown"
        )
        try:
            identity = ("manual", file_identity(source))
            digest = cache.get(identity)
            if digest is None:
                digest = sha256_file(source)
                cache[identity] = digest
            status, percent, resumable, checkpoint_status, error = _job_summary(
                Path(output), digest
            )
        except OSError as exc:
            status, percent, resumable, checkpoint_status, error = (
                "Source missing",
                0,
                False,
                None,
                str(exc),
            )
        if state.get("archived"):
            title = f"Archived · {title}"
        elif key.startswith("variant:"):
            title = f"{title} · variant {key[-6:]}"
        books[key] = LibraryBook(
            key,
            Path(source).resolve(),
            title,
            author,
            original.language if original else "unknown",
            Path(output).resolve(),
            status,
            percent,
            resumable,
            checkpoint_status,
            error,
        )
    return tuple(books.values())
