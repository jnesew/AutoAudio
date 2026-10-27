# Release checklist

Use this checklist against the exact candidate commit. Keep dated pass/fail tables, plans, benchmark logs,
and review reports in gitignored `local_notes/reports/` or `local_notes/reviews/`. Promote lasting usage,
configuration, and known-limit findings into the maintained guides before release.

## Automated checks

- Run the unit and spoof/integration suite in the project's virtual environment.
- Compile/import the source and check dependency consistency with `python -m pip check`.
- Exercise both Qwen workflow fixtures and the declared Python/platform combinations.
- Run synthetic EPUB fixtures and actual Qt worker, draft, queue, pause, and shutdown tests.
- Build source/wheel packages and smoke-test the installed commands and bundled resources outside the checkout.
- Check local documentation links and verify generated books, audio, credentials, and local notes are untracked.

## Runtime qualification

- Record the tested Python, direct/transitive dependencies, Torch CPU/CUDA/ROCm build, external provider/node
  versions when available, and system tools. Keep ComfyUI installation and container pins outside this repo.
- Generate a short representative excerpt with each supported narrator mode being qualified. Verify a single
  audible disclosure per chapter, narration quality, spacing, chapter/part assembly, and metadata/cover art.
- Verify FLAC, MP3, and M4B exports and exercise interruption/resume at segment and chapter boundaries.
- Measure cold and warm AudioSeal CPU calls with representative speech. Check thread/backend tuning and
  serialized identifying-message verification; qualify GPU behavior separately on suitable hardware.
- Run `autoaudio-verify --output-dir <test-output>` and distinguish artifact/hash evidence from direct
  signal-level watermark checks. Exercise `--check-signal` on final FLAC/MP3/M4B, partial/exact recovery,
  and `--require-exact-payload`. See [Resume, watermarking, and provenance](provenance-and-resume.md).
- Exercise C2PA signing, parse-back validation, and both soft-fail/hard-fail policies on supported formats.
  Verify a renamed signed file without its sidecar, distinguish signer trust from signature validity,
  and confirm changed asset bytes fail binding. Keep test credentials out of packages and source control.
- Check startup and provider selection do not issue requests. Test samples/discovery through their explicit
  actions and ensure ComfyUI pause cannot interrupt unrelated server work.

Use isolated output paths and bounded excerpts for routine qualification. A whole-book test requires
explicit authorization. Record incomplete gates and platform limits accurately; an old report does not
establish that a new candidate passed them.

## Release actions

- Confirm final dependency requirements, tested resolution, licenses, and notices.
- Review the final diff for prohibited reference-audio or cloning paths and changes to resume compatibility.
- Prepare release notes for behavior changes and any intentional checkpoint or v1 compatibility breaks.
- Merge the reviewed candidate after required gates pass. Push, tag, and publish when authorized by the maintainer.
