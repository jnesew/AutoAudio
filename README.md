# AutoAudio

AutoAudio converts EPUB, TXT, Markdown, and RST books into chapter and part audiobook files through a selectable text-to-speech provider. It supports **ComfyUI + Qwen3-TTS**, **OpenAI-compatible `/v1/audio/speech` endpoints**, and **ElevenLabs**.

Version 2 is deliberately non-cloning: it uses built-in, text-designed, or already-existing provider voices, but it never accepts reference-voice audio or calls a voice-cloning API. Every narration segment receives a verified non-audible AudioSeal watermark, and an audible synthetic-audio disclosure is placed once at the beginning of each chapter.

The current release candidate identifies itself as `2.0.0`:

```bash
autoaudio --version
```

## Features

- Stable Qwen CustomVoice narration with nine bundled preset speakers.
- Experimental VoiceDesign narration from a textual voice description.
- Provider adapters for ComfyUI, OpenAI-compatible speech endpoints, and ElevenLabs existing voices.
- Explicit, on-request voice discovery with no automatic endpoint probes.
- EPUB parsing through MIT-licensed `pubparser` v0.1.1, including Project Gutenberg normalization.
- FLAC, MP3, and M4B chapter and part outputs.
- Lossless chapter assembly with configurable disclosure, segment, and chapter spacing.
- Resumable jobs with immutable book plans and artifact-integrity checkpoints.
- Local library view with isolated per-title jobs, queued single-worker conversion, and pause/resume.
- BookPlan-weighted per-title progress with a session-based remaining-time estimate.
- User-confirmed Project Gutenberg OPDS search and EPUB download without bulk fetching or prefetching.
- Cooperative pause from the GUI or cancellation with `Ctrl+C`.
- Verified AudioSeal marking, AI metadata sidecars, and optional C2PA provenance.
- Matching CLI and PySide6 GUI configuration.

## Requirements

- Python 3.11 or newer
- `ffmpeg` and `ffprobe` on `PATH`
- One configured TTS runtime: a compatible ComfyUI server, an OpenAI-compatible speech endpoint, or ElevenLabs
- Provider credentials in an environment variable when the selected endpoint requires them

See [Installation and provider setup](Docs/installation.md) and [TTS providers](Docs/tts-providers.md) for the full contracts.

## Quick start

Install Python dependencies:

```bash
python -m pip install .
```

Start ComfyUI, then run a book from the command line:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/audiobook
```

Choose another narrator and output format:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/audiobook \
  --narrator-profile preset-aiden-neutral \
  --output-format m4b
```

An OpenAI-compatible endpoint can be selected without changing the audiobook pipeline:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/audiobook \
  --tts-provider openai-compatible \
  --tts-base-url https://api.openai.com/v1 \
  --tts-api-key-env OPENAI_API_KEY \
  --tts-model gpt-4o-mini-tts \
  --tts-voice alloy \
  --tts-response-format wav
```

Provider selection and editing are network-inert. Remote synthesis begins only after the user starts or resumes a conversion. ElevenLabs voice discovery runs only when **Discover voices** is pressed or `--discover-voices` is explicitly supplied; OpenAI-compatible voice IDs are entered manually.

Launch the desktop GUI:

```bash
autoaudio --gui
```

The GUI scans the `books` directory for EPUB, TXT, Markdown, and RST sources. Each source receives a
content-addressed directory beneath `audiobook_output`, so an interrupted title can be resumed without
overwriting or displacing another title's state. Existing incomplete checkpoints created directly in the
old global `audiobook_output` directory remain discoverable and resumable.

The **Find books** tab searches Project Gutenberg only when **Search** is pressed. It requests one OPDS
result page at a time. Reviewing a result loads only that title's detail feed, and only explicit confirmation
downloads its selected EPUB. See
[Library jobs and Project Gutenberg acquisition](Docs/library-and-gutenberg.md).

Preset speakers are the stable default for long-form work. VoiceDesign remains experimental because independent generations may vary even with the same instruction and seed.

## Pause, cancel, and resume

Use **Pause active** in the GUI or press `Ctrl+C` in the CLI. AutoAudio requests cooperative cancellation,
stops at a safe file boundary, and preserves a resumable checkpoint. GUI jobs are
presented as paused; CLI interruption retains cancellation terminology and exit code 130.

Resume with the same input, settings, workflows, plan, and output directory:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/audiobook \
  --resume yes
```

## Outputs

- Chapters: `Chapter_###_<title>.<format>`
- Parts: `<book title> - Part_###.<format>`
- Run log: `<output>/autoaudio_debug.log`
- Resume state: `<output>/.autoaudio_state/checkpoint_state.json`
- Immutable plan: `<output>/.autoaudio_state/book_plan.json`

For library jobs, `<output>` is normally `audiobook_output/book-<source-hash-prefix>`. Downloaded source
books, generated audio, checkpoints, and segments are ignored by Git. Only `.gitkeep` placeholders are
tracked for the default directory structure.

Internal lossless masters, silence assets, and temporary segments are maintained beneath the output directory while needed for safe resume and assembly.

## Verify AI marking

After a completed run, verify retained chapter and part files:

```bash
autoaudio-verify --output-dir "/path/to/audiobook"
```

The command fails if a publishable artifact has missing AI metadata, missing or incompatible watermark evidence, or a stale final-file hash.

## Documentation

- [Installation and provider setup](Docs/installation.md)
- [TTS providers and explicit discovery](Docs/tts-providers.md)
- [Narrators, voices, and Qwen settings](Docs/narrators.md)
- [CLI reference](Docs/cli-reference.md)
- [Resume, watermarking, and provenance](Docs/provenance-and-resume.md)
- [Library jobs and Project Gutenberg acquisition](Docs/library-and-gutenberg.md)
- [v2 release qualification](Docs/release-checklist.md)
- [v2 migration record](Docs/v2-migration-plan.md)

## License

AutoAudio source code is licensed under the MIT License. See [LICENSE](LICENSE).

Third-party components retain their own licenses. See [THIRD_PARTY_DEPENDENCIES.md](THIRD_PARTY_DEPENDENCIES.md) and [LICENSES/README.md](LICENSES/README.md).
