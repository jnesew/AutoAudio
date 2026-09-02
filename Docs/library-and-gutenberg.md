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

This reduces the chance that a broad `git add` will publish source or generated works from a fork. The source
and output directories are created automatically if absent.

## Project Gutenberg search

The discovery UI uses Project Gutenberg's official OPDS catalog endpoint rather than crawling website pages.
The client follows these boundaries:

- a search request occurs only when the user presses **Search** or Enter;
- only one result page is requested, with **More results** providing explicit pagination;
- search results remain lightweight; **Review & download selected EPUB…** requests the selected title's
  OPDS detail feed so its contributors, language, rights, and acquisition formats can be shown;
- no covers, related titles, unselected detail pages, speculative pages, or book files are prefetched;
- repeated identical result pages are cached for the current application session;
- requests are rate-limited and use `AutoAudio/<version>` with the project repository as a contact URL;
- OPDS and acquisition responses have strict size limits;
- catalog and download URLs must use HTTPS on an allowed Project Gutenberg host;
- XML document type and entity declarations are rejected;
- downloaded data must validate as an EPUB container before atomic installation;
- no bulk-selection or bulk-download action is provided.

The current provider implements OPDS1 behind a catalog-provider boundary so a future OPDS2 implementation
does not require UI changes. Project Gutenberg currently documents the OPDS entry point and has announced its
intent to retire the existing XML feed in 2027.

References:

- [Project Gutenberg Terms of Use](https://www.gutenberg.org/policy/terms_of_use.html)
- [Project Gutenberg Offline Catalogs and Feeds](https://www.gutenberg.org/ebooks/offline_catalogs.html)
- [Project Gutenberg robot access guidance](https://www.gutenberg.org/policy/robot_access.html)

## Explicit acquisition

Selecting a result does not make another request or download it. **Review & download selected EPUB…** loads
only that title's OPDS detail feed, merges its available editions, and then opens a confirmation containing
the specific title, contributors, language, preferred EPUB format, reported size, and catalog rights
statement. The default answer is No. Only acceptance starts the EPUB request. EPUB3 with images is preferred
when offered, followed by other image-bearing EPUBs and then compatible fallbacks.

The installed filename is `pg<gutenberg-id>.epub`. A neighboring
`pg<id>.epub.autoaudio-source.json` record preserves the canonical landing page, acquisition URL, retrieved
timestamp, catalog rights statement, and core bibliographic fields without modifying the downloaded EPUB.

Project Gutenberg generally describes its works' status under United States law. The UI therefore repeats
Project Gutenberg's warning that users outside the United States must check the law applicable to them.
