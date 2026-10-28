# Library jobs and Project Gutenberg acquisition

## Local library

The GUI library scans the configured books directory recursively for EPUB, TXT, Markdown, and RST files.
Directory symlinks are not followed. Embedded EPUB metadata is read without parsing every spine document;
malformed files remain isolated so they cannot prevent other titles from appearing.

Each source is identified by its complete SHA-256 digest. Its default conversion directory is:

```text
<output-root>/book-<first-16-digest-characters>/
```

This keeps `.autoaudio_state`, `.segments`, logs, and publishable output isolated per title while leaving the
pipeline's existing checkpoint and validation rules intact. The GUI scheduler runs one conversion at a time,
which avoids competing Qwen/ComfyUI work for the same GPU. Additional titles can be queued during a run.

**Pause active** requests cooperative cancellation. The current ComfyUI request is canceled when supported,
and the pipeline stops at a safe checkpoint boundary. Selecting that title later exposes **Resume**. A title
whose checkpoint still says `running` after process interruption is shown as **Interrupted**, not as an active
process.

## Repository safety

The repository tracks only placeholders beneath `books` and `audiobook_output`. The ignore rules cover:

- downloaded or manually supplied source works;
- generated chapter and part audio;
- per-title output directories;
- `.autoaudio_state` checkpoints and internal masters;
- `.segments` caches and their AI-marking sidecars.

This reduces the chance that a broad `git add` will publish source or generated works from a fork.
Scanning does not create a missing source directory. Downloads create the configured books directory;
starting a run creates its output directory. Local GUI drafts are stored separately under
`audiobook_output/.gui/drafts.json`.

## Project Gutenberg search

**Find books** searches a saved copy of Project Gutenberg's official CSV catalog. The first explicit
search downloads `pg_catalog.csv.gz` (roughly 6 MB) and builds a local SQLite index. Later searches,
language filters, and **More results** use that index without contacting Gutenberg. Starting the app or
opening the page does not download or refresh anything.

Search matches all entered words across titles, credited authors/contributors, and subjects, ignoring
case and diacritics. Enter a Gutenberg book ID to find that record directly. Results include author and
language immediately, with exact titles and title prefixes ranked ahead of other matches. This ranking
is local and does not reproduce Gutenberg's popularity order. Multilingual titles match each listed
language; choose a language from the filter after the catalog has loaded. Results are shown 25 at a time.
Non-text catalog records are excluded; EPUB availability is checked during review.

The client follows these boundaries:

- a download/update check occurs during an explicit search when no usable catalog exists or the last
  successful check was at least seven days ago;
- refresh uses `If-Modified-Since` when Gutenberg supplies `Last-Modified`, so an unchanged catalog is
  reused without downloading its body;
- failed updates retain the previous index, display a notice, and wait at least an hour before retrying;
  this backoff persists across restarts;
- catalog replacement is atomic after decompression, CSV/schema validation, and database validation;
  concurrent app instances coordinate refreshes and can continue reading the old snapshot;
- **Review & Download** requests the selected title's OPDS detail feed for current contributors,
  language, rights, and acquisition formats; these detail responses are cached for the session;
- no covers, related titles, unselected detail pages, or book files are prefetched;
- requests are rate-limited and use `AutoAudio/<version>` with the project repository as a contact URL;
- compressed catalogs, expanded CSV data, OPDS, and acquisition responses have strict size limits;
- catalog and download URLs must use HTTPS on an allowed Project Gutenberg host;
- XML document type and entity declarations are rejected;
- downloaded data must validate as an EPUB container before atomic installation;
- no bulk-selection or bulk-download action is provided.

The CSV snapshot is published weekly. Search can work offline after the first download, using the last
saved catalog if a refresh fails; the status line shows the catalog's date. Newly released books and
metadata corrections appear after a successful refresh. An unsuccessful first download needs a connection
and another explicit search. No online search fallback runs behind the scenes. Refreshing between result
pages invalidates old pagination; repeat the search to use the new snapshot.

### Catalog cache

The index is stored outside book sources and output jobs and reused across application sessions:

| Platform | Default directory |
| --- | --- |
| Linux and other Unix systems | `$XDG_CACHE_HOME/autoaudio/gutenberg`, normally `~/.cache/autoaudio/gutenberg` |
| macOS | `~/Library/Caches/autoaudio/gutenberg` |
| Windows | `%LOCALAPPDATA%/autoaudio/gutenberg` |

Set `AUTOAUDIO_CATALOG_CACHE_DIR` to override the directory. It contains `catalog.sqlite3` and a small
refresh-lock database. The index uses tens of megabytes; downloaded CSV data is not retained separately.
To rebuild it, close AutoAudio and remove this cache directory, then run a search. Book files and saved
conversion jobs are separate. The importer accepts up to 16 MiB compressed, 128 MiB expanded, and 250,000
catalog rows, rejecting invalid/empty replacements while keeping a usable older index.

Acquisition currently uses OPDS1 behind the catalog-provider boundary. Project Gutenberg has announced
its intent to retire the existing XML feed in 2027; a future acquisition adapter will be needed when its
replacement is available. Local CSV discovery is independent of that feed.

References:

- [Project Gutenberg Terms of Use](https://www.gutenberg.org/policy/terms_of_use.html)
- [Project Gutenberg Offline Catalogs and Feeds](https://www.gutenberg.org/ebooks/offline_catalogs.html)
- [Project Gutenberg robot access guidance](https://www.gutenberg.org/policy/robot_access.html)

## Explicit acquisition

Selecting a result does not make another request or download it. **Review & Download** uses the CSV's
`Text#` book ID to load `https://www.gutenberg.org/ebooks/<id>.opds`, merges its available editions,
and then opens a confirmation containing
the specific title, contributors, language, preferred EPUB format, reported size, and catalog rights
statement. The default answer is No. Acceptance starts the EPUB request using the URL returned by the
detail feed; download filenames and EPUB URLs are not guessed from the book ID. EPUB3 with images is preferred
when offered, followed by other image-bearing EPUBs and then compatible fallbacks.

The installed filename is `pg<gutenberg-id>.epub`. A neighboring
`pg<id>.epub.autoaudio-source.json` record preserves the canonical landing page, acquisition URL, retrieved
timestamp, catalog rights statement, and core bibliographic fields without modifying the downloaded EPUB.

Project Gutenberg generally describes its works' status under United States law. The UI therefore repeats
Project Gutenberg's warning that users outside the United States must check the law applicable to them.
