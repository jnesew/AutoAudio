# CLI reference

The authoritative option list is always available from:

```bash
autoaudio --help
```

Source checkouts may equivalently use `python auto_audiobook.py`.

## Input and book planning

- `--input-book <path>`: EPUB, TXT, Markdown, or RST input.
- `--output-dir <path>`: destination for publishable files and job state.
- `--source-mode {auto,epub,text}`: automatic or forced source parser.
- `--pages-per-chapter <int>`: EPUB grouping helper.
- `--target-words-per-chapter <int>`: target size for text-derived chapters.
- `--min-paragraphs-per-chapter <int>`: lower grouping bound.
- `--chapters-per-part <int>`: chapters combined into each part.

EPUB parsing is offline. AutoAudio reads publication spine order, extracts metadata and cover data in the same parse, and removes recognized Project Gutenberg distribution material before freezing the BookPlan. Online metadata lookup is separate and optional.

## Narration and segmentation

- `--narrator-profile <id>`: bundled profile ID.
- `--speaker {Aiden,Dylan,Eric,Ono_anna,Ryan,Serena,Sohee,Uncle_fu,Vivian}`: preset-speaker override.
- `--voice-instruct <text>`: preset style guidance or VoiceDesign description.
- `--model-choice {0.6B,1.7B}`: Qwen model selection. VoiceDesign accepts only 1.7B.
- `--device <value>`
- `--precision <value>`
- `--language <value>`
- `--seed <int>`
- `--max-new-tokens <int>`
- `--top-p <float>`
- `--top-k <int>`
- `--temperature <float>`
- `--repetition-penalty <float>`
- `--attention {sdpa,flash_attn}`
- `--unload-model-after-generate` / `--no-unload-model-after-generate`
- `--target-words-per-segment <int>`: soft semantic target.
- `--max-words-per-segment <int>`: strict segment ceiling.

## Spacing and output

- `--output-format {flac,mp3,m4b}`
- `--watermark-device {auto,cpu,cuda}`: AudioSeal device. `auto` prefers a PyTorch CUDA/ROCm device and retries on CPU if automatic GPU execution fails.
- `--disclosure-gap-ms <0..60000>`: silence following each chapter disclosure.
- `--segment-gap-ms <0..60000>`: silence between narration segments.
- `--chapter-gap-ms <0..60000>`: silence between chapters in part files.

AutoAudio builds normalized 24 kHz mono FLAC masters and encodes MP3 or M4B once from those lossless masters.

An explicit `cpu` or `cuda` selection never changes devices silently. On AMD ROCm systems, select `cuda`, which is the device name exposed by PyTorch. The `AUTOAUDIO_WATERMARK_DEVICE` environment variable can override this setting for a process.

## Metadata

- `--fetch-metadata`: enable optional online Gutenberg metadata lookup.
- `--gutenberg-id <id>`: explicit Gutenberg identifier.
- `--title <value>`
- `--author <value>`

Metadata precedence is user override, embedded source metadata, fetched metadata, then fallback defaults.

## ComfyUI runtime

- `--comfyui-mode {network,spoof}`
- `--comfyui-server-address <host:port>`
- `--comfyui-timeout-seconds <float>`
- `--comfyui-spoof-scenario {success,timeout,malformed_history,missing_view_payload,connection_error}`

## Resume and application mode

- `--resume {auto,yes,no}`
- `--gui`
- `--version`

`Ctrl+C` requests cooperative cancellation and returns exit code 130 after resumable state is saved.

## C2PA provenance

- `--provenance-enabled`
- `--provenance-cert-path <path>`
- `--provenance-key-path <path>`
- `--provenance-key-password <value>`
- `--provenance-tool <path-or-name>`
- `--provenance-claim-generator <value>`
- `--provenance-failure-mode {soft-fail,hard-fail}`

Private-key passwords are used for the current process only and are not written to checkpoints.
