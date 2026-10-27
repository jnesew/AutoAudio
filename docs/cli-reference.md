# CLI reference

The authoritative option list is always available from:

```bash
autoaudio --help
```

Source checkouts may equivalently use `python auto_audiobook.py`.

For worked conversion and resume examples, see [Command-line usage](command-line-usage.md).

## Input and book planning

- `--input-book <path>`: EPUB, TXT, Markdown, or RST input.
- `--output-dir <path>`: destination for publishable files and job state.
- `--source-mode {auto,epub,text}`: automatic or forced source parser.
- `--pages-per-chapter <int>`: EPUB grouping helper.
- `--target-words-per-chapter <int>`: target size for text-derived chapters.
- `--min-paragraphs-per-chapter <int>`: lower grouping bound.
- `--chapters-per-part <int>`: chapters combined into each part.

EPUB parsing is offline. AutoAudio reads publication spine order, extracts metadata and cover data in the same parse, and removes recognized Project Gutenberg distribution material before freezing the BookPlan. Online metadata lookup is separate and optional.

## TTS provider

- `--tts-provider {comfyui,openai-compatible,elevenlabs}`: adapter used for synthesis. The default remains `comfyui`.
- `--tts-base-url <url>`: provider root URL. Blank uses the adapter default.
- `--tts-api-key-env <name>`: environment variable containing the API key. AutoAudio stores the variable name, never its value.
- `--tts-model <id>`: required for OpenAI-compatible endpoints; optional for ElevenLabs, which defaults to `eleven_multilingual_v2`.
- `--tts-voice <id>`: existing provider voice ID. Required for both HTTP adapters.
- `--tts-response-format <value>`: provider response format. Use a self-describing format such as WAV, MP3, FLAC, or Opus; raw PCM and μ-law are rejected.
- `--tts-language-code <code>`: optional ElevenLabs language code.
- `--tts-timeout-seconds <float>`: synthesis or explicit-discovery timeout. `--comfyui-timeout-seconds` remains a compatibility alias.
- `--discover-voices`: explicitly request eligible voices for the selected provider, print them, and exit without converting a book.

Changing provider settings does not contact an endpoint. Voice discovery occurs only with `--discover-voices`; synthesis occurs only as part of a user-started conversion. OpenAI-compatible speech APIs do not define a standard voice-list endpoint, so those voice IDs are entered manually. See [TTS providers](tts-providers.md).

## Narration text preparation

- `--narrate-toc` / `--no-narrate-toc`: include or omit confidently identified tables of contents. Omission is the default.
- `--replacement-file <json>`: shared narration replacement rule list.
- `--replacement-rule SOURCE=SPOKEN`: repeatable whole-word body replacement. A JSON object enables advanced fields.
- `--exclude-chapter <int>`: exclude a one-based chapter number after grouping. Repeat for multiple chapters; at least one chapter must remain included.
- `--chapter-text-override <json>`: replace a grouped chapter's spoken text after replacement rules. Repeat JSON objects containing `chapter` (a one-based number) and nonempty `text`.

For example, exclude front matter and replace the next chapter's spoken text:

```bash
autoaudio --input-book /path/to/book.epub --output-dir /path/to/new-run \
  --exclude-chapter 1 \
  --chapter-text-override '{"chapter": 2, "text": "The edited narration for this chapter."}'
```

Chapter numbers refer to the grouped plan, and excluded chapters retain their original numbers.
Changing grouping can invalidate existing chapter choices. Choices and edits participate in resume
compatibility; use a new output directory when changing an existing job's narration.

See [Narration text preparation](narration-text.md) for rule schema, matching behavior, and TOC confidence policy.

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

### AudioSeal environment settings

| Variable | Default | Effect |
| --- | --- | --- |
| `AUTOAUDIO_WATERMARK_DEVICE` | Value of `--watermark-device` | Process-level device override; takes precedence over the CLI/GUI selection. |
| `AUTOAUDIO_WATERMARK_CPU_THREADS` | Smaller of 4 and the current Torch intra-op count | Positive integer CPU thread override, scoped to watermark inference and restored afterward. |
| `AUTOAUDIO_WATERMARK_CPU_MKLDNN` | Unset / `0` | Native CPU detector kernels. Set `1` to retain the caller's oneDNN setting; does not change GPU execution. |
| `AUTOAUDIO_WATERMARK_SECRET` | Public reproducible default | Key used to derive the 16-bit identifying payload. A private value does not replace signed author provenance. |

For example, require CPU watermarking with a four-thread override (POSIX shell syntax):

```bash
AUTOAUDIO_WATERMARK_CPU_THREADS=4 autoaudio \
  --input-book /path/to/book.epub --output-dir /path/to/output --watermark-device cpu
```

See [CPU performance and benchmarking](installation.md#audioseal-cpu-performance) and
[watermark evidence](provenance-and-resume.md#audioseal-marking) for scope and verification details.

## Metadata

- `--fetch-metadata`: enable optional online Gutenberg metadata lookup.
- `--gutenberg-id <id>`: explicit Gutenberg identifier.
- `--title <value>`
- `--author <value>`

Metadata precedence is user override, embedded source metadata, fetched metadata, then fallback defaults.

## ComfyUI-specific runtime

- `--comfyui-mode {network,spoof}`
- `--comfyui-server-address <host:port>`
- `--comfyui-timeout-seconds <float>`: compatibility alias for `--tts-timeout-seconds`.
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

## Artifact verification (`autoaudio-verify`)

These options belong to the separate verification command:

| Option | Effect |
| --- | --- |
| `--output-dir <path>` | Audit published chapter and part files in a job directory. |
| `--file <path>` | Inspect one file; use either this or `--output-dir`. |
| `--include-segments` | Include cached `.segments` audio in a directory audit. |
| `--check-signal` | Decode and detect every recorded AudioSeal span. Can load/download models. |
| `--require-exact-payload` | Require zero wrong bits; implies a signal check. Otherwise up to two wrong bits require presence score ≥0.9. |
| `--check-signature` | Validate the embedded C2PA signature and its binding to the file. Requires `c2patool`. |
| `--require-trusted-signature` | Require both a valid signature and a signer trusted by the selected anchors; implies a signature check. |
| `--trust-anchors <path>` | Local PEM trust anchors; implies a signature check without changing the system trust store. |
| `--provenance-tool <path-or-name>` | C2PA inspection executable; defaults to `c2patool`. |
| `--watermark-device {cpu,cuda,auto}` | Signal verification device; defaults to `cpu`. |
| `--json` | Emit separate integrity, detection, payload, coverage, signature, and trust results. |

Exit codes are `0` when all requested checks pass, `1` on a verification failure, and `2` for invalid input or no matching audio. The default audit checks recorded evidence and metadata without running AudioSeal. A signal check requires a payload timeline from a sidecar or valid embedded C2PA assertion. See [verification scope and limitations](provenance-and-resume.md#audioseal-marking).
