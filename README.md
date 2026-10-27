# AutoAudio

AutoAudio converts EPUB, TXT, Markdown, and RST books into audiobooks using ComfyUI with Qwen3-TTS,
OpenAI-compatible speech providers, or ElevenLabs. Use the desktop app or command line.

## Features

- Preset, text-designed, and existing provider voices.
- Narration previews, chapter editing, pronunciation rules, and short audio samples.
- A local book library, conversion queue, and pause/resume support.
- Project Gutenberg search and book downloads.
- FLAC, MP3, and M4B chapter and part exports.
- Audio watermarking and optional signed provenance.

## Get started

You need Python 3.11+, `ffmpeg`/`ffprobe`, and a configured speech provider.
Follow [Installation and provider setup](docs/installation.md) to prepare the environment, then launch:

```bash
autoaudio --gui
```

Follow **Book → Voice → Preview → Output** to review the narration, try a sample, and convert the book.
See [GUI usage](docs/desktop-workflow.md) for the full workflow, or
[Command-line usage](docs/command-line-usage.md) for conversion commands and examples.

## Documentation

| Guide | Covers |
| --- | --- |
| [Installation](docs/installation.md) | Dependencies, setup, troubleshooting, and CPU tuning |
| [GUI usage](docs/desktop-workflow.md) | Previews, samples, saved drafts, queues, and variants |
| [Command-line usage](docs/command-line-usage.md) | Conversion, output formats, resume, and verification |
| [TTS providers](docs/tts-providers.md) | Provider configuration and voice discovery |
| [Narrators and voices](docs/narrators.md) | Profiles, voice design, and generation settings |
| [Narration text](docs/narration-text.md) | Pronunciation rules, chapter edits, exclusions, and parsing limits |
| [Library and book downloads](docs/library-and-gutenberg.md) | Local sources and Project Gutenberg |
| [Watermarking and provenance](docs/provenance-and-resume.md) | Audio marking, signing, and recovery compatibility |
| [CLI reference](docs/cli-reference.md) | All options and environment settings |

## License

AutoAudio is [MIT licensed](LICENSE). See [third-party notices](THIRD_PARTY_DEPENDENCIES.md)
for dependency licenses.
