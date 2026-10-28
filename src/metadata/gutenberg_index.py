"""Persistent, locally searched Gutenberg CSV metadata; no network at import/startup."""

from __future__ import annotations

from contextlib import closing
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zlib


CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"
MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
MAX_CSV_BYTES = 128 * 1024 * 1024
MAX_CATALOG_ROWS = 250_000
REFRESH_INTERVAL = 7 * 24 * 60 * 60
RETRY_INTERVAL = 60 * 60
PAGE_SIZE = 25
INDEX_SCHEMA = 1


class CatalogIndexError(RuntimeError):
    pass


def default_cache_dir() -> Path:
    override = os.environ.get("AUTOAUDIO_CATALOG_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        candidate = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        base = candidate if candidate.is_absolute() else Path.home() / ".cache"
    return base / "autoaudio" / "gutenberg"


def normalize_search(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class GutenbergCatalogIndex:
    def __init__(self, cache_dir: Path | None = None, *, wall_clock=time.time):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
        self.path = self.cache_dir / "catalog.sqlite3"
        self.wall_clock = wall_clock
        self._lock = threading.Lock()

    def _connect(self):
        # Read-only access must never create a blank catalog on a failed open.
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)

    def _metadata(self) -> dict | None:
        if not self.path.is_file():
            return None
        try:
            with closing(self._connect()) as db:
                info = json.loads(db.execute("SELECT info FROM metadata WHERE id=1").fetchone()[0])
                if db.execute("SELECT id,title,authors,language,subjects,search_text,title_key FROM books LIMIT 1").fetchone() is None:
                    return None
            if (info["schema"] != INDEX_SCHEMA or not isinstance(info["generation"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", info["generation"])
                    or type(info["count"]) is not int or not 0 < info["count"] <= MAX_CATALOG_ROWS):
                return None
            if not isinstance(info["catalog_date"], str) or not isinstance(info["notice"], str):
                return None
            if info["last_modified"] is not None and not isinstance(info["last_modified"], str):
                return None
            if not isinstance(info["languages"], list) or not all(isinstance(x, str) for x in info["languages"]):
                return None
            for name in ("checked_at", "attempted_at", "downloaded_at"):
                if type(info[name]) not in (int, float) or not math.isfinite(info[name]):
                    return None
            return info
        except (sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
            return None

    def _save_metadata(self, info):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE metadata SET info=? WHERE id=1", (json.dumps(info),))
            db.commit()

    def ensure(self, fetch, *, progress=lambda _message: None) -> dict:
        """Refresh on explicit searches; failed updates retain the previous snapshot."""
        with self._lock:
            info = self._metadata()
            now = self.wall_clock()
            if info and now < max(info["checked_at"] + REFRESH_INTERVAL, info["attempted_at"] + RETRY_INTERVAL):
                return info
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                # A separate SQLite write lock serializes refreshes across app instances
                # while searches continue reading the previous catalog database.
                with closing(sqlite3.connect(self.cache_dir / "refresh.lock.sqlite3", timeout=.1)) as lock:
                    try:
                        lock.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as exc:
                        if info:
                            return {**info, "notice": "Catalog update in progress; using the saved catalog."}
                        raise CatalogIndexError("The book catalog is being prepared by another AutoAudio instance. Try Search again shortly.") from exc
                    info = self._metadata()
                    if info and now < max(info["checked_at"] + REFRESH_INTERVAL, info["attempted_at"] + RETRY_INTERVAL):
                        return info
                    if info:
                        info["attempted_at"] = now
                        self._save_metadata(info)
                    progress("Checking for catalog updates…" if info else "Downloading book catalog (about 6 MB)…")
                    payload, modified = fetch(info.get("last_modified") if info else None)
                    if payload is None:
                        if not info:
                            raise CatalogIndexError("Gutenberg returned no catalog for the first download.")
                        info.update(checked_at=now, attempted_at=now, notice="")
                        self._save_metadata(info)
                        return info
                    progress("Preparing book catalog…")
                    return self._install(payload, last_modified=modified, now=now)
            except (OSError, sqlite3.Error, ValueError, CatalogIndexError, RuntimeError) as exc:
                if not info:
                    raise CatalogIndexError(f"Could not prepare the book catalog: {exc}. Check your connection and try Search again.") from exc
                info["attempted_at"] = now
                logging.getLogger("autoaudio.catalog").warning("Catalog refresh failed: %s", exc)
                info["notice"] = "Catalog update failed; using the saved catalog. Updates will retry after an hour."
                try:
                    self._save_metadata(info)
                except (OSError, sqlite3.Error):
                    pass  # A readable cache remains useful on a read-only filesystem.
                return info

    def _install(self, payload: bytes, *, last_modified: str | None, now: float) -> dict:
        if len(payload) > MAX_COMPRESSED_BYTES:
            raise CatalogIndexError("Compressed catalog exceeds the size limit.")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
                raw = compressed.read(MAX_CSV_BYTES + 1)
            if len(raw) > MAX_CSV_BYTES:
                raise CatalogIndexError("Expanded catalog exceeds the size limit.")
            stream = io.StringIO(raw.decode("utf-8-sig"), newline="")
            del raw
            reader = csv.DictReader(stream, strict=True)
            required = {"Text#", "Type", "Title", "Language", "Authors", "Subjects"}
            if (not required.issubset(reader.fieldnames or ())
                    or len(reader.fieldnames) != len(set(reader.fieldnames))):
                raise CatalogIndexError("Catalog CSV has missing or duplicate columns.")
            descriptor, filename = tempfile.mkstemp(prefix=".catalog-", suffix=".sqlite3", dir=self.cache_dir)
            os.close(descriptor)
            temporary = Path(filename)
            try:
                with closing(sqlite3.connect(temporary)) as db:
                    db.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, authors TEXT, language TEXT, subjects TEXT, search_text TEXT, title_key TEXT)")
                    db.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY, info TEXT NOT NULL)")
                    languages = set()
                    count = 0
                    for number, row in enumerate(reader, 1):
                        if number > MAX_CATALOG_ROWS:
                            raise CatalogIndexError("Catalog has too many records.")
                        if None in row or any(row.get(key) is None for key in required):
                            raise CatalogIndexError("Catalog CSV contains an incomplete record.")
                        if row["Type"].strip().casefold() != "text":
                            continue
                        identity = row["Text#"].strip()
                        if not re.fullmatch(r"[1-9]\d{0,8}", identity):
                            raise CatalogIndexError("Catalog contains an invalid book ID.")
                        title = " ".join(row["Title"].split()) or f"Project Gutenberg #{identity}"
                        authors = tuple(x.strip() for x in row["Authors"].split(";") if x.strip())
                        codes = sorted({x.strip().lower() for x in row["Language"].split(";") if x.strip()})
                        languages.update(codes)
                        subjects = " ".join(row["Subjects"].split())
                        db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?)", (
                            int(identity), title, json.dumps(authors, ensure_ascii=False),
                            ";".join(codes), subjects,
                            normalize_search(" ".join((title, *authors, subjects))), normalize_search(title),
                        ))
                        count += 1
                    if not count:
                        raise CatalogIndexError("Catalog contains no text books.")
                    # Invalid date headers must not be replayed into HTTP requests.
                    try:
                        date = parsedate_to_datetime(last_modified) if last_modified else None
                        last_modified = last_modified if date else None
                    except (ValueError, TypeError, OverflowError):
                        date, last_modified = None, None
                    info = {
                        "schema": INDEX_SCHEMA, "generation": uuid.uuid4().hex, "count": count,
                        "languages": sorted(languages), "checked_at": now, "attempted_at": now,
                        "downloaded_at": now, "last_modified": last_modified, "notice": "",
                        "catalog_date": (date or datetime.fromtimestamp(now, timezone.utc)).strftime("%Y-%m-%d"),
                    }
                    db.execute("INSERT INTO metadata VALUES (1,?)", (json.dumps(info),))
                    db.execute("CREATE INDEX books_title ON books(title_key)")
                    db.commit()
                    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise CatalogIndexError("Catalog database validation failed.")
                os.replace(temporary, self.path)
                return info
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, EOFError, UnicodeError, csv.Error, sqlite3.Error, zlib.error) as exc:
            raise CatalogIndexError(f"Invalid compressed catalog: {exc}") from exc

    def search(self, query: str, *, language="", offset=0, generation=None):
        normalized = normalize_search(query)
        tokens = normalized.split()
        if not tokens or len(query) > 300 or len(tokens) > 20:
            raise CatalogIndexError("Enter up to 300 characters and 20 search words.")
        if type(offset) is not int or not 0 <= offset <= MAX_CATALOG_ROWS:
            raise CatalogIndexError("Invalid catalog page offset.")
        try:
            with closing(self._connect()) as db:
                info = json.loads(db.execute("SELECT info FROM metadata WHERE id=1").fetchone()[0])
                if generation is not None and generation != info["generation"]:
                    raise CatalogIndexError("The catalog was updated. Search again to view the new results.")
                clauses = ["search_text LIKE ? ESCAPE '\\'" for _ in tokens]
                arguments = ["%" + _like(token) + "%" for token in tokens]
                if re.fullmatch(r"[1-9]\d{0,8}", query.strip()):
                    clauses, arguments = ["id=?"], [int(query.strip())]
                if language:
                    clauses.append("(';' || language || ';') LIKE ? ESCAPE '\\'")
                    arguments.append("%;" + _like(language.lower()) + ";%")
                where = " AND ".join(clauses)
                total = db.execute("SELECT count(*) FROM books WHERE " + where, arguments).fetchone()[0]
                rows = db.execute(
                    "SELECT id,title,authors,language,subjects FROM books WHERE " + where +
                    " ORDER BY CASE WHEN CAST(id AS TEXT)=? THEN 0 WHEN title_key=? THEN 1 WHEN title_key LIKE ? ESCAPE '\\' THEN 2 ELSE 3 END, length(title),title_key,id LIMIT ? OFFSET ?",
                    [*arguments, query.strip(), normalized, _like(normalized) + "%", PAGE_SIZE, offset],
                ).fetchall()
                return rows, total, info
        except sqlite3.Error as exc:
            raise CatalogIndexError(f"The saved catalog could not be searched: {exc}") from exc
