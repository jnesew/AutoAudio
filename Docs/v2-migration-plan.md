# AutoAudio v2 migration plan

## Release intent

AutoAudio v2 intentionally breaks compatibility with the VibeVoice/reference-audio pipeline. It removes voice cloning and adopts Qwen3-TTS through ComfyUI with two narrator modes:

- **Preset voice** (`FB_Qwen3TTSCustomVoice`) is the stable default.
- **Designed voice** (`FB_Qwen3TTSVoiceDesign`) is an opt-in, experimental mode for creating a voice from a textual description without reference audio.

No v2 interface may accept, upload, cache, or inject reference-voice audio. The v1 implementation remains available from `legacy/v1-vibevoice-final` while v2 is developed.

## Branch and integration policy

`v2-preparation` is the long-lived integration branch. Each stage is developed on a short-lived branch, tested independently, and merged into `v2-preparation` before the next stage begins. `main` remains the working v1 release until v2 release qualification is complete.

Planned stages:

1. `refactor/v2-book-plan-checkpoints`
   - Persist a deterministic BookPlan before synthesis.
   - Introduce checkpoint schema v2 and output-local job state.
   - Bind resume compatibility to the input, settings, workflow bytes, and plan hash.
2. `feature/v2-qwen-backend`
   - Add Qwen CustomVoice and VoiceDesign workflow adapters.
   - Remove VibeVoice-specific request fields from the core client contract.
3. `feature/v2-narrator-profiles`
   - Add saved, validated narrator profiles and remove every cloning path.
4. `feature/v2-qwen-segmentation`
   - Replace the compatibility planner with Qwen-aware semantic segmentation.
5. `feature/v2-disclosure-watermarking`
   - Insert an audible disclosure once at the start of each chapter.
   - Retain non-audible AudioSeal marking for every generated narration segment.
6. `refactor/v2-pubparser`
   - Replace EbookLib/Beautiful Soup EPUB handling with MIT-licensed pubparser.
   - Parse text, metadata, cover data, diagnostics, and Gutenberg normalization through one adapter.
   - Bind the parser/normalizer policy to checkpoint compatibility.
7. `refactor/v2-audio-assembly`
   - Assemble disclosure, narration, and silence without text tokens such as `[pause]`.
   - Stream normalized FLAC masters through ffmpeg instead of buffering full raw chapters or parts in Python.
   - Encode MP3/M4B outputs once, build parts from lossless masters, and clean intermediate audio/manifest pairs.
8. `feature/v2-gui-cli`
   - Replace the reference-voice picker with narrator-profile controls across Book, Narrator, Output/Runtime, and Provenance tabs.
   - Keep a tested GUI control contract in parity with every applicable CLI option; use one shared runtime configuration builder.
   - Add thread-safe cooperative cancellation for GUI and `SIGINT`, interrupt the active ComfyUI prompt, and make canceled checkpoints resumable.
   - Never persist provenance private-key passwords in checkpoint UI state.
9. `fix/v2-metadata-packaging`
   - Correct provenance model identity, final artifact hashes, filename safety, dependency notices, and documentation.
10. `release/v2.0.0`
   - Run the release test matrix, migration review, and packaged smoke tests before merging to `main` and tagging.

## BookPlan and resume contract

Every new job writes these files beneath `<output>/.autoaudio_state/`:

- `book_plan.json` contains the ordered chapter and synthesis-segment text.
- `checkpoint_state.json` contains progress and artifact integrity data.

The plan is immutable for the lifetime of a resumable job. A resume is accepted only when all of the following match:

- input content SHA-256;
- generation/settings SHA-256;
- exact ComfyUI workflow SHA-256;
- persisted BookPlan SHA-256;
- selected output directory.

Checkpoint schema v1 is deliberately not resumable by v2. `--resume yes` reports this incompatibility; `--resume auto` starts a new v2 job.

## Narrator consistency rules

- Preset voice is the default because it is the more repeatable multi-segment mode.
- A narrator profile locks mode, model size, language, voice or design instruction, seed, sampling parameters, precision, attention backend, and model-unload policy.
- VoiceDesign profiles are marked experimental because independent generations can vary even with a fixed prompt and seed.
- Generation is sequential within a narrator profile and the model remains loaded by default.
- The planner favors fewer, larger semantic segments within measured memory and token limits. Initial calibration targets are 120–180 words for preset voices and 160–220 words for designed voices, subject to fixture-based testing.

## Disclosure and provenance rules

- An audible synthetic-audio disclosure appears exactly once at the beginning of each produced chapter.
- The disclosure is a controlled neutral asset or other deterministic source; it is not synthesized separately with a designed narrator for each segment.
- Every generated narration segment is non-audibly watermarked before it enters chapter assembly.
- Assembly rejects missing, tampered, or unverified watermark evidence and records source-artifact inheritance in output sidecars.
- Disclosure, segment, and chapter spacing comes from explicit lossless silence assets; narration text never contains pause tokens.
- Chapter and part containers retain AI metadata and optional C2PA manifests.
- The checkpoint records hashes after all final mutations, including provenance embedding.

## Stage acceptance gates

Each stage must pass unit tests, spoof/integration tests, import/compile checks, and a focused review of checkpoint compatibility. Parser changes additionally require synthetic EPUB fixtures and a checksum-reviewed Project Gutenberg real-world smoke test. Qwen stages require exported-workflow fixture tests for both narrator modes. The release branch requires real ComfyUI smoke tests for preset and designed voices, interruption/resume at segment and chapter boundaries, all supported output formats, disclosure placement checks, and watermark verification.
