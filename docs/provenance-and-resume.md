# Resume, watermarking, and provenance

## Chapter disclosure

At the beginning of a job, AutoAudio generates and checkpoints one neutral disclosure asset through the selected provider. ComfyUI uses its stable preset workflow; OpenAI-compatible endpoints receive a neutral announcement instruction; ElevenLabs uses the selected existing voice. The same verified asset is placed exactly once at the beginning of every produced chapter. Narration text itself never contains disclosure or pause tokens.

## AudioSeal marking

Every generated narration segment is converted to 24 kHz mono audio, marked with AudioSeal, and immediately passed through the AudioSeal detector. Unverified output is rejected before assembly.

Each mark carries a deterministic 16-bit message derived from the segment content ID, its pre-watermark audio SHA-256, and the configured watermark key. AutoAudio requires an AudioSeal API capable of embedding that message; it never falls back to an unidentified watermark. The public default key makes the payload reproducible rather than secret. Deployments may set `AUTOAUDIO_WATERMARK_SECRET` to a stable private value, but changing it does not make an already assembled audiobook retroactively verifiable from its final container alone.

The default `--watermark-device auto` setting prefers CUDA when PyTorch reports it available, including AMD ROCm installations, and retries on CPU if automatic GPU execution fails. Explicit `cpu` and `cuda` selections are strict. `AUTOAUDIO_WATERMARK_DEVICE` provides a process-level override.

CPU inference uses eager execution and at most four intra-op threads by default, respecting a smaller
existing pool. Set `AUTOAUDIO_WATERMARK_CPU_THREADS` to a positive override after benchmarking.
Detector inference temporarily uses native CPU kernels to avoid potential oneDNN startup overhead for
new audio lengths. `AUTOAUDIO_WATERMARK_CPU_MKLDNN=1` retains the caller's oneDNN setting when
benchmarking favors it. Backend/thread settings are restored after each call;
GPU execution and the serialized waveform/payload verification checks are unchanged. See
[CPU performance and benchmarking](installation.md#audioseal-cpu-performance) for examples and limits.

When `auto` falls back from GPU execution to CPU, that choice is remembered for the rest of the job so
each segment does not repeat the same failure. A new job tries automatic device selection again.

Sidecars distinguish two evidence scopes:

- Segment and disclosure sidecars record direct embedding and detector verification.
- Chapter and part sidecars record hash-checked inheritance from verified source artifacts.

Lossless assembly validates every source sidecar and source hash. Final-container hashes are refreshed after all output mutations.

`autoaudio-verify` audits container metadata, recorded watermark evidence, and final-file hashes. It does
not rerun the AudioSeal detector on the exported MP3/M4B waveform. Direct signal/message verification
happens on the serialized marked audio before assembly and export; the final audit is a different evidence
scope and does not establish watermark survival after a lossy encode.

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

Legacy schema-v1 checkpoints from the VibeVoice workflow are incompatible. Current runs preserve
incompatible recovery files; use a fresh output directory to start a v2 conversion.

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

- `--resume auto`: resume a compatible v2 job, or start a new job in an unused output directory. Incompatible, corrupted, or completed recovery state is preserved and reported instead of replaced.
- `--resume yes`: require a compatible checkpoint and report incompatibility.
- `--resume no`: start a new job in an unused output directory. Existing plans, checkpoints, or audio require a different destination.

Each output job is locked while AutoAudio is running. Another app or CLI instance cannot write to the same job concurrently. The `job.lock` file remains in `.autoaudio_state` after release; file presence alone does not mean a process is still running. The operating system releases ownership when the process exits.

Plans and checkpoints are flushed before atomic replacement. Invalid recovery files and changed settings never trigger automatic deletion or replacement of previous work. To create a different voice/format variant, select a new output directory or use **New variant** in the GUI. **Archive & replace run…** retains the old files and prepares a fresh destination; see [GUI usage](desktop-workflow.md).

New plans use Unicode sentence boundaries with hard ceilings of 2,400 characters and 7,200 UTF-8 bytes per request, in addition to word limits. CJK text also has a conservative ceiling of twice the maximum-word count in CJK characters (440 for the default preset), so unspaced text does not turn into an excessively long request. Compatible older frozen plans remain resumable without resegmentation if their segments fit those limits. Oversized legacy segments require a new plan in another output directory.

ComfyUI subscriptions are established before prompt submission. Timeout/disconnection recovery reads only the submitted prompt's history. Pause removes only that prompt's queued entry: AutoAudio does not issue a global server interrupt. A prompt already executing on the external server may finish independently, protecting unrelated work on shared or older servers.

Canceled, failed, and interrupted jobs remain resumable. Completed jobs are not presented as resumable GUI work.
The GUI presents a cooperatively canceled job as **Paused** and its Resume action uses the same compatibility
checks as `--resume yes`. Pending queue entries are session-local; once a job begins, its state is persisted in
its per-title checkpoint.

TOC narration policy and the normalized replacement-rule list are included in the settings compatibility
hash. Chapter exclusions and spoken-text overrides are also included when nonempty. Changing these
requires a new plan in an unused output directory rather than reusing segments generated from different
spoken text.
