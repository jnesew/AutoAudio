# Resume, watermarking, and provenance

## Chapter disclosure

At the beginning of a new job, AutoAudio generates and checkpoints one disclosure asset through the
selected provider and narration voice. ComfyUI presets use the selected speaker and generation settings
with a neutral announcement instruction. VoiceDesign uses the same workflow, description, and settings
as narration; its independent generations can still vary in vocal identity. OpenAI-compatible endpoints
receive the selected model/voice with a neutral instruction; ElevenLabs uses the selected model/voice.
HTTP-provider disclosure never requires ComfyUI.

The same verified asset is placed exactly once at the beginning of every produced chapter. Narration
text itself never contains disclosure or pause tokens. New jobs record `chapter-disclosure-v2` in their
compatibility identity and checkpoint. Resuming a job preserves its saved policy and verified disclosure.
Older jobs with `chapter-disclosure-v1`, including those interrupted before generating the first asset,
retain the fixed Eric preset for ComfyUI and the original compatibility hashes. Choose a new output
directory to start an older book with the new disclosure voice policy.

## AudioSeal marking

Every generated narration segment is converted to 24 kHz mono audio, marked with AudioSeal, serialized, and immediately passed through the AudioSeal detector. Output that fails the acceptance policy is rejected before assembly.

Each mark carries a deterministic 16-bit message derived from the segment content ID, its pre-watermark audio SHA-256, and the configured watermark key. AutoAudio requires an AudioSeal API capable of embedding that message; it never falls back to an unidentified watermark. The public default key makes the payload reproducible rather than secret. Deployments may set `AUTOAUDIO_WATERMARK_SECRET` to a stable private value. This is a symmetric derivation key, not a public signing key; increasing its length does not increase AudioSeal's 16-bit payload capacity.

Generation accepts either:

- An exact 16-bit recovery with a watermark-presence score of at least **0.5**.
- **One or two wrong bits**, with a watermark-presence score of at least **0.9**. This is recorded and logged as partial recovery.

Three or more wrong bits, insufficient presence, malformed detector output, or non-finite results fail verification. The thresholds apply to the serialized marked waveform. A presence score describes detector output, not the probability that the recording is synthetic or that an attribution is correct. Two-bit tolerance accepts 137 of the 65,536 possible bit patterns around an expected identifier; that combinatorial count is not an empirically calibrated false-attribution rate. Treat a payload match as supporting evidence and use signed provenance when attribution matters.

AudioSeal detects a watermark that can also be applied to a human recording. Its presence alone cannot prove synthetic origin. The synthetic-origin declaration is a claim made by the generating software and, when signed, by the signer. The public AudioSeal model is not an authenticity authority. See the [AudioSeal documentation](https://github.com/facebookresearch/audioseal) for its detection and message-recovery capabilities.

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

New schema-v3 sidecars retain the expected bits, full pre-watermark source SHA-256, a full SHA-256 attribution ID, model/derivation identifiers, and actual generation detection results. Expected bits are public verification data; the derivation secret is not included. The full attribution ID identifies a record but is not embedded as a larger AudioSeal message and is not a cryptographic signature. Each payload has a sample range on the normalized 24 kHz timeline. Assembly offsets these ranges across disclosures, silence, segments, and repeated chapters, and preserves them during export.

The default `autoaudio-verify` command audits container metadata, recorded watermark evidence, and final-file hashes. It does not load AudioSeal models or establish watermark survival after lossy encoding. Add `--check-signal` to decode the exported waveform and check every retained payload range, including MP3/M4B. The report separates signal presence, exact/partial payload recovery, timeline coverage, file integrity, signature validity, and signer trust.

Verify retained output with:

```bash
autoaudio-verify --output-dir "/path/to/audiobook"
```

Add `--include-segments` when auditing a job that still retains segment-cache files. Internal `.autoaudio_state` audio is excluded from the publishable-output audit.

For a waveform check and machine-readable diagnostics:

```bash
autoaudio-verify --file "/path/to/chapter.mp3" --check-signal --json
```

Use `--require-exact-payload` to require zero bit errors; this implies `--check-signal`. Signal checks default to CPU, may download AudioSeal models on first use, and can take time for long books. Files are decoded once in timeline order to preserve codec priming and avoid repeated seeking. Individual payload ranges longer than ten minutes are rejected by this verifier. Editing, cropping, resampling, or re-encoding outside AutoAudio may invalidate the recorded timeline, signature, or recovered message; a failed check is not proof that a recording is human.

An unsigned sidecar is useful for local integrity checks but can be replaced with the audio. It cannot authenticate an author or source. For a signed export, use `--check-signature` with the optional C2PA tooling described below.

## Optional C2PA provenance

C2PA is disabled by default. When enabled, AutoAudio invokes the selected C2PA tool after a chapter or part container is complete.

The manifest records:

- The selected TTS model and provider backend.
- AutoAudio and its runtime version.
- For ComfyUI, the effective node class and an explicit backend version when the workflow supplies one.
- A digest explicitly scoped to the encoded source bytes before C2PA embedding.
- The public expected AudioSeal payload records, their sample ranges, and whether all generated speech has records.

The C2PA tool creates the container-specific hard binding. After embedding, AutoAudio rehashes the complete final container and refreshes its AI-marking sidecar and checkpoint records.

AutoAudio validates the newly signed container before replacing the unsigned artifact. Temporary signing files are created beside the output so replacement remains atomic even when the system temporary directory is on another filesystem. Current `c2patool` releases require an M4A filename while initially signing an M4B container; AutoAudio uses an internal M4A alias and restores the requested M4B filename without changing the signed bytes.

Use `--provenance-failure-mode hard-fail` when unsigned output must never be accepted. Certificate and private-key paths can be checkpointed as configuration; private-key passwords are never persisted.

Verify the embedded signature and asset binding separately from waveform recovery:

```bash
autoaudio-verify --file "/path/to/chapter.m4b" --check-signal --check-signature
```

A valid C2PA signature binds its recorded claims to the asset; it does not establish that the signer is trustworthy or that every claim is true. The verifier reports an untrusted signer separately from an invalid signature. To require trust, supply a local PEM anchor bundle obtained through a trusted channel:

```bash
autoaudio-verify --file "/path/to/chapter.m4b" --check-signal \
  --require-trusted-signature --trust-anchors "/path/to/trusted-anchors.pem"
```

The verifier uses isolated C2PA settings and does not modify the user's trust store. Trust anchors are a verification policy, not proof of identity merely because a certificate accompanies a file. Public demo credentials must never be used to establish production trust.

New signed exports contain their payload records inside the C2PA assertion. They can be verified after renaming and sharing, without the original job, audio source, watermark secret, or sidecar. When no sidecar is present, `--check-signal` attempts embedded C2PA verification automatically. A valid signature and asset binding are required before using those records; any conflicting sidecar is reported. Unsigned files need their sidecars for expected payloads.

Schema-v2 sidecars remain usable for resume and the ordinary metadata/hash audit. They lack the public payload timeline needed for retrospective signal attribution; the verifier reports that evidence as unavailable. A resumed job containing both old and new segments reports partial coverage and cannot pass a complete signal audit. New records do not fabricate evidence for previously generated audio. The frozen plan compatibility identity remains unchanged; schema-v3 sidecars require this version of AutoAudio to read them.

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
