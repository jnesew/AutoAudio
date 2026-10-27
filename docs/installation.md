# Installation and provider setup

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

`requirements/constraints-linux-py314.txt` is an optional point-in-time Linux/Python 3.14 constraint
snapshot. It is not applied automatically and is not a complete current runtime lock. Keep the chosen
Torch accelerator build explicit when recording a tested environment.

PyTorch accelerator wheels are platform-specific. Install the appropriate tested CPU, CUDA, or ROCm wheel first when the default PyPI resolution is not suitable; `python -m pip install .` will accept an already installed compatible `torch` distribution. Intel XPU is not part of the v2 qualification matrix.

PySide6 is required only for the desktop GUI. AudioSeal, PyTorch, SoundFile, and NumPy provide non-audible watermark embedding and verification. AutoAudio normalizes audio through ffmpeg and reads the normalized PCM with SoundFile, so it does not require librosa or its JIT/scientific-computing dependency chain.

## Upgrading from v1

AutoAudio v2 replaces the VibeVoice/reference-voice workflow with non-cloning preset, text-designed,
or existing provider voices. Reference-voice audio is not accepted. Legacy checkpoint schema v1 cannot
be resumed by v2; keep the old output intact and choose a fresh destination for a v2 job. See
[resume compatibility](provenance-and-resume.md#bookplan-and-checkpoint-contract).

## System tools

Install `ffmpeg` and `ffprobe` and make both commands available on `PATH`. AutoAudio uses them for audio normalization, lossless assembly, final encoding, and metadata.

Optional C2PA signing requires `c2patool` or a compatible executable selected with `--provenance-tool`. AutoAudio supplies signing credentials through a private temporary manifest and handles the tool's unsigned-M4B extension limitation internally.

## Choose a TTS provider

The default provider is ComfyUI, preserving existing commands and resumable jobs. OpenAI-compatible and ElevenLabs adapters use only Python's standard HTTP client, so no provider SDK is required. Full options and outbound-request behavior are described in [TTS providers](tts-providers.md).

API keys are read from the environment only immediately before an explicitly requested HTTP operation. Put the secret in an environment variable, then give AutoAudio the variable's name with `--tts-api-key-env`. Secret values are not stored in configuration, logs, compatibility hashes, or checkpoints. Base URLs containing embedded credentials, query strings, or fragments are rejected so credentials cannot accidentally enter resumable state.

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

AutoAudio does not perform provider health checks at startup or when a provider is selected. For an HTTP provider, a request is made after the user starts/resumes a conversion, generates a sample, or requests supported voice discovery.

Once the provider is configured, follow [GUI usage](desktop-workflow.md) or
[Command-line usage](command-line-usage.md) to create an audiobook.

## Troubleshooting

- **Cannot connect to ComfyUI:** confirm the server address and that the Qwen nodes loaded without errors.
- **HTTP TTS request fails:** confirm the base URL, model, existing voice ID, response format, and API-key environment variable.
- **Voice discovery unavailable:** OpenAI-compatible endpoints have no standardized voice-list route; enter a voice ID manually. ElevenLabs discovery must be requested explicitly.
- **Missing audio output:** verify the selected model exists and matches the narrator mode.
- **Qwen model class failed to import:** inspect the external ComfyUI server's startup traceback and consult the installed Qwen node's setup instructions. Its Python dependencies, including librosa, are independent of AutoAudio's environment. ComfyUI installation and container dependency management are outside this repository's scope.
- **Qwen `pad_token_id` attribute error:** check the external server's Transformers compatibility against the installed node's requirements and the [Qwen-TTS dependency requirements](https://github.com/QwenLM/Qwen3-TTS/blob/main/pyproject.toml). Those dependencies belong to the external runtime and are separate from AutoAudio's environment.
- **Watermarking failure:** confirm the installed PyTorch and AudioSeal stack works on the selected platform. Start with `--watermark-device auto`; use `cpu` to diagnose accelerator problems or `cuda` to require NVIDIA CUDA/AMD ROCm. AutoAudio rejects unverified segment output.
- **GUI unavailable:** install PySide6 in the active Python environment.
- **Metadata lookup fails:** online Gutenberg metadata is optional; omit `--fetch-metadata` to remain offline.

For redistribution and exact license-notice requirements, see [../THIRD_PARTY_DEPENDENCIES.md](../THIRD_PARTY_DEPENDENCIES.md).

## AudioSeal CPU performance

AutoAudio runs the pinned AudioSeal 0.2 models in eager mode using AudioSeal's own scoped `no_compile` context. Its default SEANet forwards compile lazily, which can make the first segment and changing audio lengths much slower than inference itself. The context covers both generation and verification; AutoAudio serializes these calls because the upstream context uses a shared flag.

CPU inference uses at most four Torch intra-op threads by default, preserving an already smaller pool. Larger pools can increase first-use convolution overhead for new speech lengths. Set `AUTOAUDIO_WATERMARK_CPU_THREADS` to a positive integer to override this limit after benchmarking the current environment. The previous thread count is restored when marking finishes or fails. Torch's intra-op setting is process-wide during that scoped call, so an application embedding AutoAudio should account for other concurrent Torch work. CUDA/ROCm inference does not change the CPU thread setting.

The CPU detector uses native kernels by default to avoid potential oneDNN transposed-convolution
startup overhead for new audio lengths. `AUTOAUDIO_WATERMARK_CPU_MKLDNN=1` retains the caller's oneDNN
setting when benchmarking favors it. Compare cold and warm calls on the current runtime; performance
depends on the backend and input shapes. The override affects CPU detection, restores the previous
backend flag, and keeps the serialized waveform and 16-bit message checks.

Each marked WAV is decoded again for verification, including the identifying 16-bit message. At most two wrong bits are accepted with a presence score of at least 0.9; partial recovery is recorded explicitly. See [watermark verification policy](provenance-and-resume.md#audioseal-marking). The run log records the selected device and elapsed watermark time. CPU, CUDA, and AMD ROCm still use the same device selection rules; a CPU-wheel installation cannot use a GPU even if the machine has one.

Use a bounded local benchmark without contacting a provider:

```bash
python scripts/benchmark_audioseal.py --device cpu --duration 5 --runs 3
```

This uses a synthetic tone and the real embedding/detection models. The first run includes model loading, and can download AudioSeal's published model checkpoints if they are absent from its cache. These results measure watermarking, not speech synthesis quality or speed.

Use a representative existing WAV/FLAC clip between 1 and 120 seconds to test speech and changing
input lengths. `--threads` sets the benchmark's CPU thread override. Compare detector backends in
fresh processes so a warmed convolution cache does not hide cold-start costs (POSIX shell syntax):

```bash
AUTOAUDIO_WATERMARK_CPU_MKLDNN=0 python scripts/benchmark_audioseal.py \
  --device cpu --input-audio /path/to/short-speech.flac --threads 4 --runs 3
AUTOAUDIO_WATERMARK_CPU_MKLDNN=1 python scripts/benchmark_audioseal.py \
  --device cpu --input-audio /path/to/short-speech.flac --threads 4 --runs 3
```

These commands reuse local audio and do not submit TTS requests. See the
[CLI environment settings](cli-reference.md#audioseal-environment-settings) for device overrides.
