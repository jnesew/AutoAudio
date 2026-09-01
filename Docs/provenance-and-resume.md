# Resume, watermarking, and provenance

## Chapter disclosure

At the beginning of a job, AutoAudio generates and checkpoints one neutral disclosure asset with the stable preset workflow. The same verified asset is placed exactly once at the beginning of every produced chapter. Narration text itself never contains disclosure or pause tokens.

## AudioSeal marking

Every generated narration segment is converted to 24 kHz mono audio, marked with AudioSeal, and immediately passed through the AudioSeal detector. Unverified output is rejected before assembly.

Sidecars distinguish two evidence scopes:

- Segment and disclosure sidecars record direct embedding and detector verification.
- Chapter and part sidecars record hash-checked inheritance from verified source artifacts.

Lossless assembly validates every source sidecar and source hash. Final-container hashes are refreshed after all output mutations.

Verify retained output with:

```bash
python scripts/verify.py --output-dir "/path/to/audiobook"
```

Add `--include-segments` when auditing a job that still retains segment-cache files. Internal `.autoaudio_state` audio is excluded from the publishable-output audit.

## Optional C2PA provenance

C2PA is disabled by default. When enabled, AutoAudio invokes the selected C2PA tool after a chapter or part container is complete.

The manifest records:

- Qwen3-TTS and the effective model choice.
- AutoAudio and its runtime version.
- The effective ComfyUI node class and an explicit backend version when the workflow supplies one.
- A digest explicitly scoped to the encoded source bytes before C2PA embedding.

The C2PA tool creates the container-specific hard binding. After embedding, AutoAudio rehashes the complete final container and refreshes its AI-marking sidecar and checkpoint records.

Use `--provenance-failure-mode hard-fail` when unsigned output must never be accepted. Certificate and private-key paths can be checkpointed as configuration; private-key passwords are never persisted.

## BookPlan and checkpoint contract

Each job stores:

- `.autoaudio_state/book_plan.json`: immutable ordered chapters and synthesis segments.
- `.autoaudio_state/checkpoint_state.json`: progress, artifact hashes, compatibility identity, errors, and non-secret UI state.

A resume is compatible only when all of these still match:

- Input content SHA-256
- Effective generation/settings SHA-256
- Exact narration and disclosure workflow bytes
- BookPlan SHA-256
- Output directory

The parser policy, narrator profile content, output rules, and AI-marking schema contribute to the effective settings identity. This prevents a job from silently mixing artifacts produced under incompatible behavior.

## Resume modes

- `--resume auto`: resume a compatible v2 job; otherwise start a new one.
- `--resume yes`: require a compatible checkpoint and report incompatibility.
- `--resume no`: start a new job.

Canceled, failed, and interrupted jobs remain resumable. Completed jobs are not presented as resumable GUI work.
