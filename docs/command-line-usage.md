# Command-line usage

Complete [installation and provider setup](installation.md) first. The default provider is ComfyUI with
Qwen3-TTS; start that server before converting. For OpenAI-compatible or ElevenLabs configuration, use
[TTS providers](tts-providers.md). Run `autoaudio --help` for options or consult the [CLI reference](cli-reference.md).

## Convert a book

Select a source and a fresh output directory:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/audiobook
```

EPUB, TXT, Markdown, and RST are supported. The default narrator is the Aiden preset and the default
output format is FLAC. Source checkouts may also use `python auto_audiobook.py` with the same arguments.

## Choose a narrator and format

For example, use the Aiden profile and export M4B:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/aiden-m4b \
  --narrator-profile preset-aiden-neutral \
  --output-format m4b
```

Use a separate output directory for each voice/format variant. Preset voices are the default for
long-form work; VoiceDesign is experimental because voice identity can vary between segments. See
[Narrators and voices](narrators.md) for profiles and settings, and
[Narration text preparation](narration-text.md) for replacements, chapter edits, and exclusions.

To hear a bounded excerpt before a full conversion, use **Generate short sample** in the
[GUI Preview page](desktop-workflow.md#preview-and-samples). A CLI conversion processes its selected book plan.

## Pause and resume

Press `Ctrl+C` to request cooperative cancellation. AutoAudio stops at a safe boundary and saves
recovery state. Resume using the same input, output directory, and generation settings:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/audiobook \
  --resume yes
```

Include any profile, format, provider, or other overrides from the original command. Incompatible or
completed jobs are preserved; `--resume no` does not overwrite their audio or recovery state. Choose a
fresh destination for a new run. See [Resume modes](provenance-and-resume.md#resume-modes) for full
compatibility and shared-server cancellation behavior.

## Output files

Each output directory contains:

| File | Purpose |
| --- | --- |
| `Chapter_###_<title>.<format>` | Individual narrated chapters |
| `<book title> - Part_###.<format>` | Chapters combined into parts |
| `autoaudio_debug.log` | Run diagnostics |
| `.autoaudio_state/checkpoint_state.json` | Recovery state and artifact hashes |
| `.autoaudio_state/book_plan.json` | Frozen chapter and speech-segment plan |

Internal lossless masters, silence assets, and temporary segments remain beneath the output directory
while needed for recovery and assembly. GUI library jobs use per-title destinations described in
[Library jobs](library-and-gutenberg.md#local-library). Default source/output directories are ignored by Git.

## Verify output

Audit retained chapter and part files after conversion:

```bash
autoaudio-verify --output-dir /path/to/audiobook
```

The default audit reports missing AI metadata, invalid recorded watermark evidence, and stale final-file hashes. To test watermark recovery on the exported waveform, including lossy formats:

```bash
autoaudio-verify --file /path/to/chapter.m4b --check-signal --json
```

Add `--check-signature` to validate optional C2PA provenance, or `--require-exact-payload` to require zero payload errors. See [Watermarking and provenance](provenance-and-resume.md) for evidence requirements, partial-recovery policy, signer trust, and legacy output limitations.
