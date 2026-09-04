# Installation and ComfyUI setup

## Python environment

AutoAudio v2 requires Python 3.11 or newer. A virtual environment is recommended:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

For development, use an editable install:

```bash
python -m pip install -e .
```

`pyproject.toml` is the install and packaging contract. `requirements.txt` mirrors its direct runtime dependencies for compatibility and is not the final transitive lock. Both pin the tested `pubparser` document-semantics revision to an immutable commit and AudioSeal to the tested 0.2 API.

PyTorch accelerator wheels are platform-specific. Install the appropriate tested CPU, CUDA, or ROCm wheel first when the default PyPI resolution is not suitable; `python -m pip install .` will accept an already installed compatible `torch` distribution. Intel XPU is not part of the v2 qualification matrix.

PySide6 is required only for the desktop GUI. AudioSeal, PyTorch, librosa, SoundFile, and NumPy provide non-audible watermark embedding and verification.

## System tools

Install `ffmpeg` and `ffprobe` and make both commands available on `PATH`. AutoAudio uses them for audio normalization, lossless assembly, final encoding, and metadata.

Optional C2PA signing requires `c2patool` or a compatible executable selected with `--provenance-tool`. The release candidate is qualified against `c2patool 0.27.16`. AutoAudio supplies signing credentials through a private temporary manifest as required by current `c2patool`; it also handles the tool's unsigned-M4B extension limitation internally.

## ComfyUI contract

AutoAudio expects a running ComfyUI server at `127.0.0.1:8188` by default. Change it with `--comfyui-server-address` or the GUI runtime settings.

The server must provide these non-cloning node classes:

- `FB_Qwen3TTSCustomVoice`
- `FB_Qwen3TTSVoiceDesign`

AutoAudio packages compatible workflow templates:

- `qwen3_tts_custom_voice.json`
- `qwen3_tts_voice_design.json`

Preset narration supports the Qwen 0.6B and 1.7B CustomVoice models. VoiceDesign requires the 1.7B VoiceDesign model. Model files and ComfyUI itself are external runtime components and are not distributed by AutoAudio.

AutoAudio validates workflows before submission and rejects voice-cloning nodes or reference-audio inputs.

## Connectivity check

Start ComfyUI before AutoAudio. The normal runtime mode is `network`:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/output \
  --comfyui-mode network \
  --comfyui-server-address 127.0.0.1:8188
```

`spoof` mode exercises orchestration without a live model and is intended for tests and development:

```bash
autoaudio \
  --input-book /path/to/short-test.txt \
  --output-dir /path/to/test-output \
  --comfyui-mode spoof
```

## Troubleshooting

- **Cannot connect to ComfyUI:** confirm the server address and that the Qwen nodes loaded without errors.
- **Missing audio output:** verify the selected model exists and matches the narrator mode.
- **Watermarking failure:** confirm the installed PyTorch and AudioSeal stack works on the selected platform. Start with `--watermark-device auto`; use `cpu` to diagnose accelerator problems or `cuda` to require NVIDIA CUDA/AMD ROCm. AutoAudio rejects unverified segment output.
- **GUI unavailable:** install PySide6 in the active Python environment.
- **Metadata lookup fails:** online Gutenberg metadata is optional; omit `--fetch-metadata` to remain offline.

For redistribution and exact license-notice requirements, see [../THIRD_PARTY_DEPENDENCIES.md](../THIRD_PARTY_DEPENDENCIES.md).
