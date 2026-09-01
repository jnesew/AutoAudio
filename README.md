# AutoAudio

AutoAudio converts EPUB, TXT, Markdown, and RST books into chapter and part audiobook files using **ComfyUI + Qwen3-TTS**.

Version 2 is deliberately non-cloning: it supports Qwen built-in preset speakers and text-designed voices, but it never accepts reference-voice audio. Every narration segment receives a verified non-audible AudioSeal watermark, and an audible synthetic-audio disclosure is placed once at the beginning of each chapter.

The current release candidate identifies itself as `2.0.0.dev0`:

```bash
python auto_audiobook.py --version
```

## Features

- Stable Qwen CustomVoice narration with nine bundled preset speakers.
- Experimental VoiceDesign narration from a textual voice description.
- EPUB parsing through MIT-licensed `pubparser` v0.1.1, including Project Gutenberg normalization.
- FLAC, MP3, and M4B chapter and part outputs.
- Lossless chapter assembly with configurable disclosure, segment, and chapter spacing.
- Resumable jobs with immutable book plans and artifact-integrity checkpoints.
- Cooperative cancellation from the GUI or `Ctrl+C`.
- BookPlan-weighted GUI progress with a session-based remaining-time estimate.
- Verified AudioSeal marking, AI metadata sidecars, and optional C2PA provenance.
- Matching CLI and four-tab PySide6 GUI configuration.

## Requirements

- Python 3.11 or newer
- `ffmpeg` and `ffprobe` on `PATH`
- A running ComfyUI server with compatible `FB_Qwen3TTSCustomVoice` and `FB_Qwen3TTSVoiceDesign` nodes
- The appropriate Qwen3-TTS models available to those nodes

See [Installation and ComfyUI setup](Docs/installation.md) for the full setup contract.

## Quick start

Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Start ComfyUI, then run a book from the command line:

```bash
python auto_audiobook.py \
  --input-book /path/to/book.epub \
  --output-dir /path/to/audiobook
```

Choose another narrator and output format:

```bash
python auto_audiobook.py \
  --input-book /path/to/book.epub \
  --output-dir /path/to/audiobook \
  --narrator-profile preset-aiden-neutral \
  --output-format m4b
```

Launch the desktop GUI:

```bash
python auto_audiobook.py --gui
```

Preset speakers are the stable default for long-form work. VoiceDesign remains experimental because independent generations may vary even with the same instruction and seed.

## Cancel and resume

Use **Cancel** in the GUI or press `Ctrl+C` in the CLI. AutoAudio requests cancellation from the active ComfyUI prompt, stops at a safe file boundary, and preserves a resumable checkpoint.

Resume with the same input, settings, workflows, plan, and output directory:

```bash
python auto_audiobook.py \
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

Internal lossless masters, silence assets, and temporary segments are maintained beneath the output directory while needed for safe resume and assembly.

## Verify AI marking

After a completed run, verify retained chapter and part files:

```bash
python scripts/verify.py --output-dir "/path/to/audiobook"
```

The command fails if a publishable artifact has missing AI metadata, missing or incompatible watermark evidence, or a stale final-file hash.

## Documentation

- [Installation and ComfyUI setup](Docs/installation.md)
- [Narrators and Qwen settings](Docs/narrators.md)
- [CLI reference](Docs/cli-reference.md)
- [Resume, watermarking, and provenance](Docs/provenance-and-resume.md)
- [v2 release qualification](Docs/release-checklist.md)
- [v2 migration record](Docs/v2-migration-plan.md)

## License

AutoAudio source code is licensed under the MIT License. See [LICENSE](LICENSE).

Third-party components retain their own licenses. See [THIRD_PARTY_DEPENDENCIES.md](THIRD_PARTY_DEPENDENCIES.md) and [LICENSES/README.md](LICENSES/README.md).
