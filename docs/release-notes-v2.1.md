# AutoAudio v2.1

Package version: **2.1.0**. This release builds on v2.0 with a fuller desktop workflow, more speech
providers, offline book discovery, and stronger verification and recovery.

## Added and improved

- **Library and conversion queue:** per-title output directories, saved drafts, voice/format variants,
  checkpoint-based progress, and clearer pause/resume controls.
- **Offline Project Gutenberg search:** a persistent local catalog supplies titles, authors, and
  languages. Search, filtering, and pagination reuse the index. Selected-title review still checks
  current acquisition formats before an explicitly confirmed download.
- **Speech providers:** OpenAI-compatible speech endpoints and ElevenLabs existing voices, with
  explicit ElevenLabs voice discovery. Provider selection and configuration make no network requests.
- **Narration control:** pronunciation replacements, chapter text overrides and exclusions, and
  table-of-contents filtering.
- **Desktop presentation:** a clearer Book → Voice → Preview → Output workflow, illustrated usage
  documentation, and a headphones-and-book application icon.
- **English narration defaults:** Aiden is the default Qwen narrator. New jobs generate the chapter
  disclosure through the selected provider and narration voice; compatible older jobs retain their
  saved disclosure policy.
- **Published narration preview:** the README includes a short video player and an MP3 download.

## Watermarking, provenance, and reliability

- AudioSeal CPU inference uses scoped thread/backend controls to avoid excessive initialization
  overhead. GPU fallback is remembered for the remainder of a job.
- New watermark evidence retains expected payloads, full attribution identifiers, and sample ranges
  through chapter/part assembly. The verifier can check the final encoded waveform and report file
  integrity, signal detection, payload recovery, signature validity, and signer trust separately.
- Generation accepts exact 16-bit recovery with presence at least 0.5, or at most two wrong bits with
  presence at least 0.9. Partial recovery is recorded explicitly. `--require-exact-payload` remains
  available for stricter final-output audits. A watermark match supports attribution; it does not
  prove synthetic origin or signer identity.
- Truncated HTTP responses and broken chunk framing produce provider connection errors. Ambiguous
  synthesis failures are not automatically resubmitted, avoiding duplicate generation or charges.
- Signed-evidence comparisons tolerate a single floating-point serialization step in numeric metrics,
  preventing false sidecar conflicts after C2PA JSON round-tripping. Payload identities and sample
  timelines still require exact agreement.
- Dependency advisory fixes, packaging improvements, and consolidated documentation are included.

## Upgrading and compatibility

- Continue using the original input, provider, voice, output directory, and saved settings when
  resuming a job. Changed or incompatible recovery state is preserved instead of overwritten.
- For CLI resumes, retain the saved `--provenance-claim-generator` value; its default now includes
  `2.1.0`. A job started with the 2.0.0 default needs `--provenance-claim-generator AutoAudio/2.0.0`,
  even if signing is disabled. The GUI restores the saved value.
- Schema-v2 watermark sidecars remain readable. New schema-v3 records need this release for reading;
  older records cannot gain retrospective payload timelines. Mixed old/new jobs may have only partial
  signal-verification coverage. Legacy v1/VibeVoice checkpoints remain incompatible.
- ComfyUI, Qwen nodes, models, and their installation remain external prerequisites. This release
  does not bundle or manage that runtime. Reference-audio upload and voice cloning are not included.

## Validation and limits

Automated regression checks, source/wheel packaging and installed-command checks, and bounded local
audio verification are part of release validation. HTTP-provider tests use local simulators of the
documented request/response contracts, including failures and resume. Actual paid-service credentials,
account entitlements, voice availability, and vendor speech quality have not been validated.
Platform and GPU support still depend on the installed PyTorch and external speech runtime; this
release does not establish new qualification for every platform/device combination.

See [Installation](installation.md), [GUI usage](desktop-workflow.md),
[TTS providers](tts-providers.md), [Library and Gutenberg](library-and-gutenberg.md), and
[Watermarking, provenance, and resume](provenance-and-resume.md) for configuration and limits.
