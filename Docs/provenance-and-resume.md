# Resume, watermarking, and provenance

## Chapter disclosure

At the beginning of a job, AutoAudio generates and checkpoints one neutral disclosure asset through the selected provider. ComfyUI uses its stable preset workflow; OpenAI-compatible endpoints receive a neutral announcement instruction; ElevenLabs uses the selected existing voice. The same verified asset is placed exactly once at the beginning of every produced chapter. Narration text itself never contains disclosure or pause tokens.

## AudioSeal marking

Every generated narration segment is converted to 24 kHz mono audio, marked with AudioSeal, and immediately passed through the AudioSeal detector. Unverified output is rejected before assembly.

Each mark carries a deterministic 16-bit message derived from the segment content ID, its pre-watermark audio SHA-256, and the configured watermark key. AutoAudio requires an AudioSeal API capable of embedding that message; it never falls back to an unidentified watermark. The public default key makes the payload reproducible rather than secret. Deployments may set `AUTOAUDIO_WATERMARK_SECRET` to a stable private value, but changing it does not make an already assembled audiobook retroactively verifiable from its final container alone.

The default `--watermark-device auto` setting prefers CUDA when PyTorch reports it available, including AMD ROCm installations, and retries on CPU if automatic GPU execution fails. Explicit `cpu` and `cuda` selections are strict. `AUTOAUDIO_WATERMARK_DEVICE` provides a process-level override.

Sidecars distinguish two evidence scopes:

- Segment and disclosure sidecars record direct embedding and detector verification.
- Chapter and part sidecars record hash-checked inheritance from verified source artifacts.

Lossless assembly validates every source sidecar and source hash. Final-container hashes are refreshed after all output mutations.

Verify retained output with:

```bash
autoaudio-verify --output-dir "/path/to/audiobook"
```

Add `--include-segments` when auditing a job that still retains segment-cache files. Internal `.autoaudio_state` audio is excluded from the publishable-output audit.

## Optional C2PA provenance

C2PA is disabled by default. When enabled, AutoAudio invokes the selected C2PA tool after a chapter or part container is complete.

The manifest records:

- The selected TTS model and provider backend.
- AutoAudio and its runtime version.
- For ComfyUI, the effective node class and an explicit backend version when the workflow supplies one.
- A digest explicitly scoped to the encoded source bytes before C2PA embedding.

The C2PA tool creates the container-specific hard binding. After embedding, AutoAudio rehashes the complete final container and refreshes its AI-marking sidecar and checkpoint records.

AutoAudio validates the newly signed container before replacing the unsigned artifact. Temporary signing files are created beside the output so replacement remains atomic even when the system temporary directory is on another filesystem. Current `c2patool` releases require an M4A filename while initially signing an M4B container; AutoAudio uses an internal M4A alias and restores the requested M4B filename without changing the signed bytes.

Use `--provenance-failure-mode hard-fail` when unsigned output must never be accepted. Certificate and private-key paths can be checkpointed as configuration; private-key passwords are never persisted.

## BookPlan and checkpoint contract

Each job stores:

- `.autoaudio_state/book_plan.json`: immutable ordered chapters and synthesis segments.
- `.autoaudio_state/checkpoint_state.json`: progress, artifact hashes, compatibility identity, errors, and non-secret UI state.

A resume is compatible only when all of these still match:

- Input content SHA-256
- Effective generation/settings SHA-256
- Exact narration and disclosure workflow bytes for ComfyUI, or the HTTP adapter compatibility identity
- BookPlan SHA-256
- Output directory

The parser policy, narrator profile content, output rules, selected provider, endpoint, model, voice, response format, language code, and AI-marking schema contribute to the effective settings identity where applicable. The API-key environment-variable name is checkpointed, but its value is never persisted. This prevents a job from silently mixing artifacts produced under incompatible behavior.

## Per-title library state

The GUI identifies a local source by its SHA-256 digest and assigns it a deterministic directory named
`book-<digest-prefix>` beneath the selected library output root. The existing BookPlan/checkpoint contract
is unchanged inside that directory; isolation comes from giving every source its own output and state root.

An incomplete checkpoint found directly beneath the selected output root is treated as a legacy global job.
If its input digest matches a current library title, that title continues to use the original directory so
it can resume without moving trusted artifacts or rewriting their recorded paths.

Library progress is reconstructed from checkpointed disclosure, segment, chapter, master, and part artifact
presence using the same BookPlan weighting model as an active run. It deliberately avoids re-hashing large
completed audio files during every library rescan, so the GUI labels the value as an estimate. Resume still
performs the full artifact hash and AI-marking validation before reuse. Active-run ETA remains session-local
because it depends on observed generation throughput.

## Resume modes

- `--resume auto`: resume a compatible v2 job; otherwise start a new one.
- `--resume yes`: require a compatible checkpoint and report incompatibility.
- `--resume no`: start a new job.

Canceled, failed, and interrupted jobs remain resumable. Completed jobs are not presented as resumable GUI work.
The GUI presents a cooperatively canceled job as **Paused** and its Resume action uses the same compatibility
checks as `--resume yes`. Pending queue entries are session-local; once a job begins, its state is persisted in
its per-title checkpoint.
