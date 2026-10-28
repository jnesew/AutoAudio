from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path
import sqlite3
import urllib.error
from unittest.mock import Mock

import pytest

from metadata.gutenberg_catalog import GutenbergCatalogClient, GutenbergCatalogError
from metadata.gutenberg_index import (
    CATALOG_URL, REFRESH_INTERVAL, RETRY_INTERVAL, GutenbergCatalogIndex,
)


MODIFIED = "Sun, 25 Oct 2026 21:41:56 GMT"


def catalog(rows=None):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Text#", "Type", "Issued", "Title", "Language", "Authors", "Subjects", "LoCC", "Bookshelves"))
    for row in rows or [
        (1727, "Text", "1999-01-01", "The Odyssey", "en", "Homer; Butler, Samuel [Translator]", "Epic poetry", "PA", "Classics"),
        (30615, "Text", "2000-01-01", "Ομήρου Οδύσσεια", "el", "Homer", "Epic poetry", "PA", "Classics"),
        (33, "Text", "2000-01-01", 'Étrange, livre\n"français"', "en; fr", "Émile Test", "Novels", "PQ", "Fiction"),
        (55, "Text", "2000-01-01", "100%_real", "", "", "", "", ""),
        (1, "Sound", "2000-01-01", "Audio Odyssey", "en", "Homer", "", "", ""),
    ]:
        writer.writerow(row)
    return gzip.compress(output.getvalue().encode("utf-8-sig"))


class Response(io.BytesIO):
    def __init__(self, payload, url, *, modified=MODIFIED):
        super().__init__(payload)
        self.url = url
        self.headers = {"Last-Modified": modified, "Content-Length": str(len(payload))}

    def geturl(self):
        return self.url


def client(tmp_path, payload=None):
    clock = [1_800_000_000.]
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(payload if payload is not None else catalog(), request.full_url)

    result = GutenbergCatalogClient(cache_dir=tmp_path, opener=opener, minimum_request_interval=0,
                                   wall_clock=lambda: clock[0])
    return result, requests, clock


def test_startup_and_invalid_search_never_create_cache_or_make_requests(tmp_path):
    app, requests, _ = client(tmp_path)
    assert not list(tmp_path.iterdir())
    for query in ("", "x" * 301, " ".join(["word"] * 21)):
        with pytest.raises(GutenbergCatalogError):
            app.search(query)
    assert not list(tmp_path.iterdir())
    assert not requests


def test_local_search_handles_unicode_quoted_csv_languages_and_book_ids(tmp_path):
    app, requests, _ = client(tmp_path)
    first = app.search("odyssey")
    assert first.total == 1  # Non-text records are excluded.
    assert first.books[0].authors == ("Homer", "Butler, Samuel [Translator]")
    assert first.books[0].details_url == "https://www.gutenberg.org/ebooks/1727.opds"
    assert first.books[0].preferred_epub is None
    assert first.catalog_date == "2026-10-25"
    assert first.languages == ("el", "en", "fr")
    assert app.search("οδυσσεια").books[0].gutenberg_id == "30615"
    french = app.search("emile francais", language="fr")
    assert french.books[0].title == 'Étrange, livre "français"'
    assert french.books[0].language == "en, fr"
    assert not app.search("emile", language="el").books
    assert app.search("30615").books[0].language == "el"
    assert app.search("100%_").books[0].gutenberg_id == "55"
    assert not app.search("' OR 1=1 --").books
    assert len(requests) == 1 and requests[0].full_url == CATALOG_URL


def test_pagination_is_local_stable_and_rejects_other_queries_and_remote_urls(tmp_path):
    rows = [(i, "Text", "2000-01-01", f"Book {i:03}", "en", "Author", "", "", "") for i in range(1, 62)]
    app, requests, _ = client(tmp_path, catalog(rows))
    first = app.search("book")
    second = app.search("book", page_url=first.next_url)
    third = app.search("book", page_url=second.next_url)
    ids = [b.gutenberg_id for p in (first, second, third) for b in p.books]
    assert len(ids) == len(set(ids)) == first.total == 61
    assert third.next_url is None
    for query, url in (("other", first.next_url), ("book", "https://www.gutenberg.org/ebooks/search.opds/?query=book")):
        with pytest.raises(GutenbergCatalogError):
            app.search(query, page_url=url)
    with pytest.raises(GutenbergCatalogError):
        app.search("book", language="en", page_url=first.next_url)
    assert len(requests) == 1


def test_cached_catalog_survives_restart_offline(tmp_path):
    app, requests, clock = client(tmp_path)
    app.search("Homer")
    offline = Mock(side_effect=OSError("offline"))
    restarted = GutenbergCatalogClient(cache_dir=tmp_path, opener=offline, wall_clock=lambda: clock[0])
    assert restarted.search("Homer", language="el").books[0].language == "el"
    offline.assert_not_called()
    assert len(requests) == 1


def test_weekly_refresh_uses_if_modified_since_and_304_without_reindex(tmp_path):
    app, requests, clock = client(tmp_path)
    app.search("Homer")
    generation = app.index._metadata()["generation"]
    clock[0] += REFRESH_INTERVAL + 1

    def unchanged(request, **_kwargs):
        requests.append(request)
        assert request.get_header("If-modified-since") == MODIFIED
        raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, io.BytesIO())

    app.opener = unchanged
    assert app.search("Homer").books
    assert app.index._metadata()["generation"] == generation
    app.search("Odyssey")
    assert len(requests) == 2


@pytest.mark.parametrize("failure", ["network", "gzip", "schema", "empty", "duplicate", "truncated"])
def test_failed_refresh_preserves_catalog_and_backs_off_across_restarts(tmp_path, failure):
    app, _, clock = client(tmp_path)
    app.search("Homer")
    generation = app.index._metadata()["generation"]
    clock[0] += REFRESH_INTERVAL + 1
    bad = {
        "gzip": b"not gzip", "schema": gzip.compress(b"bad,header\n1,2\n"),
        "empty": gzip.compress(b"Text#,Type,Title,Language,Authors,Subjects\n"),
        "duplicate": catalog([(1, "Text", "", "A", "en", "", "", "", "")] * 2),
        "truncated": catalog()[:-4],
    }

    def unavailable(request, **_kwargs):
        if failure == "network":
            raise OSError("offline")
        return Response(bad[failure], request.full_url)

    app.opener = unavailable
    result = app.search("Homer")
    assert result.books and "using the saved catalog" in result.notice
    assert app.index._metadata()["generation"] == generation
    restarted = GutenbergCatalogClient(cache_dir=tmp_path, opener=Mock(side_effect=AssertionError("retry too early")),
                                       wall_clock=lambda: clock[0] + RETRY_INTERVAL - 1)
    assert restarted.search("Homer").notice == result.notice
    assert not list(tmp_path.glob(".catalog-*.sqlite3"))


def test_new_catalog_replaces_snapshot_and_invalidates_old_pagination(tmp_path):
    rows = [(i, "Text", "", f"Book {i}", "en", "Author", "", "", "") for i in range(1, 30)]
    app, _, clock = client(tmp_path, catalog(rows))
    first = app.search("book")
    clock[0] += REFRESH_INTERVAL + 1
    app.opener = lambda req, **_kwargs: Response(catalog(), req.full_url)
    app.search("book")
    with pytest.raises(GutenbergCatalogError, match="updated"):
        app.search("book", page_url=first.next_url)
    assert app.search("Homer").total == 2


def test_pagination_never_triggers_a_refresh_even_when_catalog_becomes_stale(tmp_path):
    rows = [(i, "Text", "", f"Book {i}", "en", "Author", "", "", "") for i in range(1, 30)]
    app, requests, clock = client(tmp_path, catalog(rows))
    first = app.search("book")
    clock[0] += REFRESH_INTERVAL + 1
    app.opener = Mock(side_effect=AssertionError("pagination must stay offline"))
    assert app.search("book", page_url=first.next_url).books
    app.opener.assert_not_called()
    assert len(requests) == 1


def test_first_download_failure_can_be_retried_without_a_blank_index(tmp_path):
    app, _, _ = client(tmp_path, b"not gzip")
    with pytest.raises(GutenbergCatalogError, match="Could not prepare"):
        app.search("Homer")
    assert not app.index.path.exists()
    app.opener = lambda req, **_kwargs: Response(catalog(), req.full_url)
    assert app.search("Homer").total == 2


def test_refresh_lock_allows_reading_old_snapshot_without_duplicate_download(tmp_path):
    app, _, clock = client(tmp_path)
    app.search("Homer")
    clock[0] += REFRESH_INTERVAL + 1
    app.opener = Mock(side_effect=AssertionError("another process is refreshing"))
    with sqlite3.connect(tmp_path / "refresh.lock.sqlite3") as lock:
        lock.execute("BEGIN IMMEDIATE")
        assert app.search("Homer").books
        app.opener.assert_not_called()


@pytest.mark.parametrize("limit", ["MAX_CSV_BYTES", "MAX_COMPRESSED_BYTES", "MAX_CATALOG_ROWS"])
def test_catalog_resource_limits_fail_without_installing_partial_index(tmp_path, monkeypatch, limit):
    monkeypatch.setattr("metadata.gutenberg_index." + limit, 1)
    index = GutenbergCatalogIndex(tmp_path)
    with pytest.raises(RuntimeError):
        index.ensure(lambda _modified: (catalog(), MODIFIED))
    assert not index.path.exists()
