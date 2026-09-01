# AutoAudio

AutoAudio converts a book file (EPUB, TXT, Markdown, or RST) into chapter and part audiobook files using **ComfyUI + Qwen3-TTS**.

The current v2 preparation build identifies itself as `2.0.0.dev0`. Set `AUTOAUDIO_VERSION` only when producing an intentionally versioned build.

```bash
python auto_audiobook.py --version
```

## What you need before running

### 1) Python and dependencies

AutoAudio v2 requires Python 3.11 or newer. EPUB input is handled by the
MIT-licensed `pubparser` v0.1.1 dependency pinned in `requirements.txt`.

Install project dependencies:

```bash
python -m pip install -r requirements.txt
```

The pubparser source is pinned to an exact commit. Other direct requirements are compatibility ranges; the release test matrix must capture an exact, platform-specific resolution before v2 packaging.

### 2) System tools

AutoAudio uses `ffmpeg` and `ffprobe` for stitching audio and writing metadata. Make sure both are installed and on your `PATH`.

### 3) ComfyUI runtime requirements (required for real generation)

AutoAudio expects a running ComfyUI server and a compatible workflow/node setup:

- ComfyUI server reachable at `127.0.0.1:8188` by default (or set `--comfyui-server-address`)
- The Qwen3-TTS custom nodes `FB_Qwen3TTSCustomVoice` and `FB_Qwen3TTSVoiceDesign`
- A compatible 1.7B Qwen3-TTS model available to those nodes
- One of the bundled non-cloning workflows:
  - `resources/workflows/qwen3_tts_custom_voice.json` (stable preset mode)
  - `resources/workflows/qwen3_tts_voice_design.json` (experimental designed mode)

AutoAudio v2 has no reference-audio upload or voice-cloning path. Narrators are selected from text/configuration profiles in `resources/narrators/default_profiles.json`.

At the start of a job, AutoAudio generates and checkpoints one fixed neutral disclosure asset using the preset Qwen voice. That same watermarked asset is placed exactly once at the beginning of every produced chapter. Narration calls contain only book text, and every generated narration segment receives a verified non-audible AudioSeal watermark before chapter assembly.

Assembly is file-streamed through normalized 24 kHz mono FLAC masters instead of buffering a whole chapter or part as raw audio in Python. Configurable lossless silence assets provide the disclosure, segment, and chapter spacing. MP3 and M4B outputs are encoded exactly once from those masters; part files never use already-lossy chapter outputs as inputs.

> If you do not have a live ComfyUI runtime yet, you can still run pipeline logic with `--comfyui-mode spoof` for testing/development.

## Quick usage flow

1. Start ComfyUI and verify the Qwen3-TTS nodes load correctly.
2. Choose a narrator profile and input book (`.epub`, `.txt`, `.md`, `.markdown`, or `.rst`).
3. Run AutoAudio from CLI or GUI.
4. Collect generated chapter/part files from your output directory (default: `audiobook_output/`).

## Run methods

### CLI

Basic run:

```bash
python auto_audiobook.py --input-book /path/to/book.epub --output-dir /path/to/output
```

Run with metadata fetch and MP3 output:

```bash
python auto_audiobook.py \
  --input-book /path/to/book.epub \
  --output-dir /path/to/output \
  --fetch-metadata \
  --output-format mp3
```

Resume a prior compatible run checkpoint:

```bash
python auto_audiobook.py --input-book /path/to/book.epub --output-dir /path/to/output --resume yes
```

### GUI

Launch desktop app:

```bash
python auto_audiobook.py --gui
```

Notes:

- GUI mode requires `PySide6` (already included in `requirements.txt`).
- The four GUI tabs expose the same book-planning, narrator, output/runtime, metadata, and provenance controls as the CLI.
- Choose a narrator profile, optionally tune its Qwen settings, then click **Start**. There is no reference-audio picker or cloning configuration.
- Preset profiles are the stable default. VoiceDesign profiles are labeled experimental because independent generations can drift.
- **Cancel** cooperatively stops ComfyUI work at a safe boundary and saves the run as resumable. The GUI enables **Resume** for compatible running, failed, or canceled checkpoints.

## CLI arguments

### Input/output and source parsing

- `--input-book <path>`: input book file path.
- `--output-dir <path>`: output directory for generated files.
- `--source-mode {auto,epub,text}`: force source parser mode.
- `--pages-per-chapter <int>`: EPUB chapter grouping helper.
- `--target-words-per-chapter <int>`: text chapter sizing target.
- `--min-paragraphs-per-chapter <int>`: lower bound when grouping text chapters.
- `--chapters-per-part <int>`: how many chapter files per final "part" file.

### Generation tuning

- `--narrator-profile <id>` selects a profile from the bundled narrator catalog.
- `--target-words-per-segment <int>` sets the soft semantic segment target.
- `--max-words-per-segment <int>` sets the strict segment ceiling.
- `--disclosure-gap-ms <0..60000>` sets silence after the chapter disclosure (default `700`).
- `--segment-gap-ms <0..60000>` sets silence between narration segments (default `150`).
- `--chapter-gap-ms <0..60000>` sets silence between chapters in part files (default `1000`).
- `--speaker <name>` overrides a preset profile speaker.
- `--voice-instruct <text>` overrides style guidance or the VoiceDesign description.
- `--model-choice <value>`, `--device <value>`, `--precision <value>`, `--language <value>`
- `--seed <int>`, `--max-new-tokens <int>`, `--top-k <int>`
- `--temperature <float>`
- `--top-p <float>`
- `--repetition-penalty <float>`, `--attention {sdpa,flash_attn}`
- `--unload-model-after-generate` / `--no-unload-model-after-generate`

### Output and metadata

- `--output-format {flac,mp3,m4b}`
- `--fetch-metadata` (flag; optional online Gutenberg/Gutendex lookup)
- `--gutenberg-id <id>` (manual Gutenberg ID override)
- `--title <value>` (manual title override)
- `--author <value>` (manual author override)

Metadata precedence is:

1. User overrides (`--title`, `--author`)
2. Embedded source metadata
3. Fetched online metadata (if enabled)
4. Fallback defaults

EPUB parsing is offline. AutoAudio reads spine documents in publication order,
uses compatibility recovery for imperfect EPUBs, extracts the embedded cover and
metadata in the same parse, and removes recognized Project Gutenberg distribution
headers and license footers before freezing the resumable BookPlan. Remote EPUB
resources are never fetched by the parser.

### ComfyUI connection/runtime controls

- `--comfyui-mode {network,spoof}`
- `--comfyui-server-address <host:port>`
- `--comfyui-timeout-seconds <float>`
- `--comfyui-spoof-scenario {success,timeout,malformed_history,missing_view_payload,connection_error}`

### Run control

- `--resume {auto,yes,no}`
- `--gui` (launches desktop GUI instead of CLI pipeline run)

Press `Ctrl+C` during a CLI run to request cooperative cancellation. AutoAudio interrupts/removes the active ComfyUI prompt when possible, finishes only the current safe file operation, records checkpoint status `cancelled`, and exits with code `130`. Resume with the same compatible settings and `--resume yes`.

### Provenance / C2PA controls

- `--provenance-enabled` enables post-processing provenance signing/embedding after each final chapter and part artifact is written.
- `--provenance-cert-path <path>` points to the X.509 signing certificate used by the C2PA toolchain.
- `--provenance-key-path <path>` points to the private key paired with the certificate.
- `--provenance-key-password <value>` optionally supplies the key password (passed via environment to the C2PA CLI).
- `--provenance-tool <path-or-name>` selects the C2PA CLI executable (default: `c2patool`).
- `--provenance-claim-generator <value>` sets the claim generator string in the manifest.
- `--provenance-failure-mode {soft-fail,hard-fail}` controls enforcement mode (`hard-fail` stops the run if provenance fails).

The default claim generator is `AutoAudio/2.0.0.dev0` for this preparation build.

When provenance is enabled, AutoAudio populates the following C2PA assertions:

- `c2pa.ai.generative`
  - `generator.name`: `Qwen3-TTS` for bundled workflows.
  - `generator.version`: the effective `model_choice` after narrator-profile and CLI/GUI overrides.
- `c2pa.actions`
  - Includes action `c2pa.created`.
  - `softwareAgent.name` / `softwareAgent.version`: `AutoAudio` plus the build version (or an explicit `AUTOAUDIO_VERSION` override).
  - `softwareAgent.backend.name`: the effective Qwen ComfyUI node `class_type`.
  - `softwareAgent.backend.version`: an explicit workflow `_meta.version` when exported; otherwise `unreported` (the human-readable node title is not treated as a version).
- `com.autoaudio.pipeline`
  - Records a SHA-256 digest explicitly scoped to the encoded source bytes before C2PA embedding.

`c2patool` creates the container-specific C2PA hard binding; AutoAudio does not inject a synthetic `c2pa.hash.data` assertion without valid manifest exclusion ranges. After successful embedding, AutoAudio hashes the complete final container again, refreshes the AI-marking sidecar, and records that same final digest in checkpoint and provenance state.

AutoAudio validates required assertion fields before signing and raises explicit schema errors when required fields are missing. Manifest identifiers and embedding paths are persisted to checkpoint state for later audit.

## Outputs and run artifacts

- Chapter files: `Chapter_###_<title>.<format>`
- Part files: `<book title> - Part_###.<format>`
- Segment cache: `<output-dir>/.segments/`
- Lossless assembly masters (checkpointed until their part succeeds): `<output-dir>/.autoaudio_state/masters/`
- Reusable silence assets: `<output-dir>/.autoaudio_state/silence/`
- Run log: `<output-dir>/autoaudio_debug.log`
- Resume checkpoint state: `<output-dir>/.autoaudio_state/checkpoint_state.json`
- Immutable synthesis plan: `<output-dir>/.autoaudio_state/book_plan.json`

Chapter and book titles are treated as untrusted metadata when used in filenames. AutoAudio normalizes them into a single cross-platform component, removes path/control characters, handles Windows device names, and applies a UTF-8 byte limit. Full original titles remain in container metadata.

### Verify AI marking and watermarking

The system automatically applies a bundled public fallback key to keep AudioSeal watermarking deterministic when no override is configured. It is not treated as a private credential.
During generation, segment sidecars record direct AudioSeal verification. Chapter and part sidecars record hash-checked inheritance from their verified source artifacts. Successfully assembled segment files and sidecars are removed together. Verify retained chapter and part outputs with:

```bash
python src/provenance/verify.py --output-dir "<output-dir>"
```
The command exits with a non-zero status if any publishable artifact is missing `ai_*` tags, missing a `.<ext>.ai.json` sidecar, has a stale whole-file SHA-256 digest, or reports an unverified watermark. Internal `.autoaudio_state` audio is excluded; cached `.segments` are checked only with `--include-segments`.

## Troubleshooting

- **Cannot connect to ComfyUI**: verify server is running and address matches `--comfyui-server-address`.
- **No audio generated**: verify the Qwen3-TTS nodes and selected model are installed and workflow-compatible.
- **Unknown narrator profile**: choose an id from `resources/narrators/default_profiles.json`.
- **Metadata fetch gives nothing**: this is optional; run without `--fetch-metadata` to stay fully offline.
- **Designed voice varies between segments**: use a preset profile for the most consistent long-form narration.

## License

AutoAudio source code is licensed under the MIT License. See `LICENSE`.

Third-party dependencies are licensed under their own terms. See `THIRD_PARTY_DEPENDENCIES.md` and `LICENSES/README.md`; binary distributors must also preserve notices shipped inside the exact wheels and external runtime components they bundle.
