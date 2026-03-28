Sprint 1 — Foundation Refactor (S/M)

    [M] Create package structure + shared pipeline module

        Move orchestration logic out of monolithic script into src/core/pipeline.py.

        Keep auto_audiobook.py as thin CLI wrapper.

        Depends on: none.

    [S] Externalize ComfyUI workflow JSON

        Add resources/workflows/vibevoice_single_speaker.json.

        Create workflow loader + runtime patching utility.

        Depends on: task 1.

    [S] Add central config model

        Include paths for resources/.autoaudio_state, default voice filename, ComfyUI mode/address.

        Depends on: task 1.

Sprint 2 — ComfyUI Abstraction + Testability (M/L)

    [M] Implement ComfyUI client interface

        RealComfyUIClient for HTTP/ws behavior currently embedded in script.

        Depends on: task 1.

    [M] Add spoof ComfyUI endpoint/client for tests

        Deterministic /prompt, /history, /view, ws execution events.

        Depends on: task 4.

    [M] Integration tests against spoof endpoint

        Success, timeout, malformed payload, connection error.

        Depends on: task 5.

Sprint 3 — Metadata System (M/L)

    [M] Build offline metadata extractor

        EPUB DC fields + chapter structure extraction.

        TXT local fallback parsing.

        Depends on: task 1.

    [M] Implement optional online fetch path (“Fetch metadata”)

        Disabled by default.

        Merge policy: user > embedded > fetched > fallback.

        Depends on: task 7.

    [S] Format-aware metadata writer contracts

        FLAC/MP3/M4B differences documented + implemented in adapter layer.

        Depends on: task 1.

Sprint 4 — Resume Engine (M)

    [M] Checkpoint persistence in resources/.autoaudio_state

        Store input hash/settings hash/progress/artifacts/errors/UI state.

        Depends on: task 1.

    [M] Resume orchestration in CLI + GUI hooks

        --resume auto|yes|no for CLI.

        Resume button path for GUI.

        Depends on: task 10.

    [S] Integrity checks for resume safety

        Validate artifact presence/hash before skipping work.

        Depends on: task 10.

Sprint 5 — GUI (PySide6) (L) ✅ Implemented

    [L] Build main window

        File picker + drag/drop + optional output dir + progress + logs.

        Depends on: task 1, 11.

    [M] Add “Fetch metadata” checkbox + preview panel

        Default unchecked.

        Depends on: task 8, 13.

    [M] Add Resume button + prepopulation on reopen

        If incomplete state exists, prefill fields.

        Depends on: task 10, 13.

Sprint 6 — Error Handling + Docs (M) ✅ Implemented

    [M] Introduce typed exceptions + centralized logging

        Replace broad print-only exception handling.

        Depends on: task 1.

    [S] Add user-facing error surfaces

        GUI dialogs + terminal summary + actionable remediation.

        Depends on: task 16.

    [S] Add Docs/AGENT_SCOPE_NOTES.md

        Portable ComfyUI out-of-scope note.

        Depends on: none.

Suggested dependency-critical path

1 → 4 → 5 → 6 (testability) and 1 → 10 → 11 → 15 (resume UX).
Test matrix (high-level)

    Input types: EPUB/TXT

    Modes: offline default / fetch metadata enabled

    Runtime: real ComfyUI / spoof ComfyUI

    Resume points: segment, chapter, part stitch

    Output formats: FLAC/MP3/M4B

    Error scenarios: network timeout, malformed metadata, ffmpeg failure, missing voice file
