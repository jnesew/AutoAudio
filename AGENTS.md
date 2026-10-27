# AutoAudio agent instructions

## Scope and workflow

- Read `README.md` and the relevant files in `Docs/` before changing behavior.
- Keep working plans, reviews, investigations, and dated validation reports under the gitignored `local_notes/` directory. Use `plans/`, `reviews/`, and `reports/` as appropriate.
- Promote useful configuration, behavior, and known-limit information from those notes into maintained `Docs/` guides before finishing. Tracked documentation must not depend on ignored notes.
- Keep machine-specific hardware descriptions, local runtime/build identifiers, and benchmark measurements in ignored reports. Tracked guidance and code comments should describe general behavior, configuration, and platform requirements.
- Follow `Docs/AGENT_SCOPE_NOTES.md`: ComfyUI and its models are external runtime assets. Do not vendor, move, or modify them.
- ComfyUI installation, container builds, and Qwen dependency pins are outside this repository's scope. Keep external runtime troubleshooting separate from AutoAudio fixes and recommendations; do not add files managing that runtime unless explicitly requested.
- Document pubparser integration issues with the exact pinned revision, fixture identity, reproduction, and expected/actual behavior; distinguish upstream parser limitations from AutoAudio adapter bugs.
- Keep provider selection and configuration network-inert. Synthesis and supported voice discovery require an explicit user action.
- Preserve non-cloning narration, verified AudioSeal marking, audible chapter disclosure, and safe checkpoint/resume behavior.
- Use the project's virtual environment and run tests appropriate to the changes. Record real-runtime results separately from mocked/spoof tests.

## Bounded end-to-end testing

- Use a short excerpt or one segment from an available test source for synthesis tests; prefer `books/waroftheworlds.epub` when provided. Do not assume local fixtures exist in a clone or generate a whole book unless the user explicitly requests it.
- Keep test outputs isolated beneath `audiobook_output/` or a temporary directory. Do not overwrite existing library jobs.
- Call only ComfyUI endpoints necessary for the requested software test. Do not probe arbitrary endpoints, change server settings, or interrupt unrelated work.
- Identify the current execution environment and installed PyTorch accelerator support before testing. Do not infer hardware, devices, or runtime versions from this repository or historical reports.
- Benchmark AudioSeal CPU thread and detector-backend settings on the current environment before making performance claims.
- Do not weaken watermark verification to improve speed. Verify the serialized audio and preserve the identifying message.

## Reporting

- Record the actual test environment and measured timings in ignored reports. Summarize what was tested, fixes, and remaining limitations without treating that environment as a requirement for other users.
- Keep recommendations grounded in the code or measured behavior; distinguish them from completed fixes.
