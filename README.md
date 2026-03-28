# AutoAudio

## Python dependencies

Install required Python packages before running the CLI:

```bash
python -m pip install -r requirements.txt
```

Current required libraries:

- `EbookLib` (EPUB parsing)
- `beautifulsoup4` (HTML/text extraction from EPUB chapters)
- `websocket-client` (ComfyUI websocket events)

## Sprint 3 metadata system highlights

- Offline metadata extraction is the default behavior.
  - EPUB: DC metadata fields (`title`, `creator`, `language`, etc.) + chapter structure.
  - TXT/Markdown/RST: lightweight local header/front-matter heuristics.
- Online metadata fetch is optional and disabled by default.
  - Enable with `--fetch-metadata`.
  - Merge precedence is: **user overrides > embedded metadata > fetched metadata > fallback values**.
- Output format metadata adapters are container aware:
  - **FLAC**: Vorbis-comment style tags (`title`, `artist`, `album`, `track`, `disc`).
  - **MP3**: ID3v2-compatible mapping via ffmpeg metadata flags.
  - **M4B**: MP4 atom-compatible tags and audiobook container output (`-f ipod`).

## New CLI metadata options

- `--fetch-metadata` – attempt Gutenberg/Gutendex lookup.
- `--gutenberg-id <id>` – explicit Gutenberg ID override.
- `--title <value>` – user title override (highest precedence).
- `--author <value>` – user author override (highest precedence).
- `--output-format {flac,mp3,m4b}` – final chapter/part output format.


## License

AutoAudio source code is licensed under the MIT License. See `LICENSE`.

Third-party dependencies are licensed under their own terms. See `THIRD_PARTY_DEPENDENCIES.md`.
