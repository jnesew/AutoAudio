# AutoAudio

AutoAudio converts a book file (EPUB, TXT, Markdown, or RST) into chapter and part audiobook files using **ComfyUI + VibeVoice**.

## What you need before running

### 1) Python and dependencies

Install project dependencies:

```bash
python -m pip install -r requirements.txt
```

### 2) System tools

AutoAudio uses `ffmpeg` and `ffprobe` for stitching audio and writing metadata. Make sure both are installed and on your `PATH`.

### 3) ComfyUI runtime requirements (required for real generation)

AutoAudio expects a running ComfyUI server and a compatible workflow/node setup:

- ComfyUI server reachable at `127.0.0.1:8188` by default (or set `--comfyui-server-address`)
- The **VibeVoice Single Speaker** custom node available in ComfyUI (`VibeVoiceSingleSpeakerNode`)
- https://huggingface.co/microsoft/VibeVoice-1.5B. Includes invisible watermarks.This software also adds invisible watermarks with audioseal separately.
    - Other models or variants are not supported.
  - All batch generations automatically prepend a synthesized provenance labeling. ("This audio was generated synthetically with AutoAudio. [pause]") before audio begins. Increase chunks_per_batch in configuration if the frequent repetition of this prompt becomes bothersome. Conversely, increase timeout to match.
   
- A reference voice file available in ComfyUI's input files as `default_voice.wav`(uploadable via GUI)
  - The bundled workflow `resources/workflows/vibevoice_single_speaker.json` loads this filename by default.
  - ⚠️ If you use the GUI **Reference voice** uploader, ComfyUI will overwrite any existing `default_voice.wav` in its input directory.
    - Ensure you have rights to use any reference voice used for voice styling in your jurisdiction  

> If you do not have a live ComfyUI runtime yet, you can still run pipeline logic with `--comfyui-mode spoof` for testing/development.

## Quick usage flow

1. Start ComfyUI and verify the VibeVoice node loads correctly.
2. Put your reference voice clip in ComfyUI input files as `default_voice.wav`.
3. Choose an input book (`.epub`, `.txt`, `.md`, `.markdown`, or `.rst`).
4. Run AutoAudio from CLI or GUI.
5. Collect generated chapter/part files from your output directory (default: `audiobook_output/`).

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
- In GUI, pick input/output paths, optionally enable **Fetch metadata**, then click **Start**.
- The **Reference voice** picker uploads your file to ComfyUI as `default_voice.wav` and will overwrite any existing file with that name in ComfyUI input.
- If a compatible checkpoint exists, the GUI enables **Resume** automatically.

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

- `--max-words-per-chunk <int>`
- `--diffusion-steps <int>`
- `--temperature <float>`
- `--top-p <float>`
- `--cfg-scale <float>`
- `--free-memory-after-generate` (flag)

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

### ComfyUI connection/runtime controls

- `--comfyui-mode {network,spoof}`
- `--comfyui-server-address <host:port>`
- `--comfyui-timeout-seconds <float>`
- `--comfyui-spoof-scenario {success,timeout,malformed_history,missing_view_payload,connection_error}`

### Run control

- `--resume {auto,yes,no}`
- `--gui` (launches desktop GUI instead of CLI pipeline run)

### Provenance / C2PA controls

- `--provenance-enabled` enables post-processing provenance signing/embedding after each final chapter and part artifact is written.
- `--provenance-cert-path <path>` points to the X.509 signing certificate used by the C2PA toolchain.
- `--provenance-key-path <path>` points to the private key paired with the certificate.
- `--provenance-key-password <value>` optionally supplies the key password (passed via environment to the C2PA CLI).
- `--provenance-tool <path-or-name>` selects the C2PA CLI executable (default: `c2patool`).
- `--provenance-claim-generator <value>` sets the claim generator string in the manifest.
- `--provenance-failure-mode {soft-fail,hard-fail}` controls enforcement mode (`hard-fail` stops the run if provenance fails).

When provenance is enabled, AutoAudio populates the following C2PA assertions:

- `c2pa.ai.generative`
  - `generator.name`: sourced from workflow node inputs (e.g., `inputs.model` in `VibeVoiceSingleSpeakerNode`).
  - `generator.version`: parsed from the same model identifier value.
- `c2pa.actions`
  - Includes action `c2pa.created`.
  - `softwareAgent.name` / `softwareAgent.version`: populated from AutoAudio runtime metadata (`AutoAudio` + `AUTOAUDIO_VERSION` env, default `dev`).
  - `softwareAgent.backend.name` / `softwareAgent.backend.version`: sourced from workflow metadata (`class_type` and `_meta.title` when present).
- `c2pa.hash.data`
  - `alg`: `sha256`.
  - `hash`: base64-encoded SHA-256 digest of final artifact bytes.

AutoAudio validates required assertion fields before signing and raises explicit schema errors when required fields are missing. Manifest identifiers and embedding paths are persisted to checkpoint state for later audit.

## Outputs and run artifacts

- Chapter files: `Chapter_###_<title>.<format>`
- Part files: `<book title> - Part_###.<format>`
- Segment cache: `<output-dir>/.segments/`
- Run log: `<output-dir>/autoaudio_debug.log`
- Resume checkpoint state: `resources/.autoaudio_state/checkpoint_state.json`

### Verify AI marking and watermarking

The system automatically applies a public default fallback secret key (`default_public_autoaudio_key_123`) to guarantee consistent AudioSeal PyTorch watermarking happens.
After generation, verify that segment and stitched outputs contain AI metadata tags,
watermark status manifests, and machine-readable marking sidecars:

```bash
python src/provenannce/verify.py --output-dir "<output-dir>" --include-segments
```
The command exits with a non-zero status if any artifact is missing `ai_*` tags,
missing a `.<ext>.ai.json` sidecar, or has a manifest that reports watermark not applied/verified.

## Troubleshooting

- **Cannot connect to ComfyUI**: verify server is running and address matches `--comfyui-server-address`.
- **No audio generated**: verify the VibeVoice node is installed and workflow-compatible.
- **Missing reference voice**: ensure `default_voice.wav` exists in ComfyUI input files.
- **Metadata fetch gives nothing**: this is optional; run without `--fetch-metadata` to stay fully offline.
- **Audio  waterkmarks are anoying**: Increase batch sample count parameter.

## License

AutoAudio source code is licensed under the MIT License. See `LICENSE`.

Third-party dependencies are licensed under their own terms. See `THIRD_PARTY_DEPENDENCIES.md`.
