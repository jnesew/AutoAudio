Architecture Plan
1) Project Structure Refactor

Proposed layout:

AutoAudio/
  src/
    core/
      pipeline.py
      checkpoint.py
      errors.py
      events.py
    comfyui/
      client.py
      real_client.py
      spoof_client.py
      workflow_loader.py
    metadata/
      extractors.py
      gutenberg.py
      models.py
    io/
      text_extract.py
      epub_extract.py
      audio_stitch.py
    gui/
      app.py
      widgets/
      controllers/
  resources/
    workflows/
      vibevoice_single_speaker.json
  tests/
    unit/
    integration/
    fixtures/
  Docs/
    implementation-plan.md
    AGENT_SCOPE_NOTES.md

Key outcomes

    Shared core logic used by both CLI and GUI.

    No hardcoded workflow JSON in logic modules.

    Testability through composable clients/providers.

2) ComfyUI Abstraction + Spoof Endpoint

Define interface:

    submit_prompt(workflow) -> prompt_id

    wait_for_completion(prompt_id, timeout)

    fetch_audio(file_ref) -> bytes

Implementations:

    RealComfyUIClient: current HTTP + websocket behavior.

    SpoofComfyUIClient: deterministic fake for tests (and optionally local dev).

Spoof server/client test scenarios:

    Success path with synthetic audio payload.

    Delayed completion / timeout.

    Malformed /history response.

    Missing /view payload.

    Connection refused.

This replaces direct coupling currently present in process_segment and related helpers.
3) GUI Plan (PySide6)
Main window elements

    Input file selector (.epub, .txt, .md, .markdown, .rst).

    Drag-and-drop target for files.

    Optional output directory selector.

    “Fetch metadata” checkbox (unchecked by default).

    “Start”, “Resume”, “Cancel”.

    Progress bar + status log panel.

    Metadata preview/edit panel (title, author, language, subjects, chapter info if detected).

Behavior

    Drag/drop and Browse use same validation path.

    Resume button enabled when checkpoint file exists.

    On startup, if incomplete run exists, prepopulate fields and show non-blocking resume prompt.

    Progress updates via event bus from pipeline (chapter/segment events).

4) Metadata Strategy (Offline-first + Optional Fetch)
Offline (default)

    Read EPUB package metadata (DC title/creator/language/identifier/etc.).

    Parse table of contents/spine/chapter titles from EPUB.

    For TXT: detect limited local metadata from frontmatter/header heuristics.

    Preserve chapter titles from source where possible.

Online (only when “Fetch metadata” checked)

    Resolve Gutenberg ID and query authoritative catalog endpoint(s).

    Normalize external fields and merge with local metadata by priority rules.

    Never overwrite explicit user edits in UI.

Merge priority

    User-edited values (highest).

    File-embedded metadata.

    Fetched external metadata.

    Filename-derived fallback.

This improves on current minimal extraction and heuristic preamble handling.
5) Metadata Model & Output Format Differences

Unified internal metadata object:

    Work-level: title, creator, contributors, language, publisher, rights, description, subjects, identifier.

    Structural: chapter list with index, title, start_ms, end_ms.

    Asset references: cover image path.

    Technical: container (flac|mp3|m4b|opus), codec details.

Format-specific writers:

    FLAC/Vorbis comments: album/artist/title/track/disc + custom tags where supported.

    MP3/ID3v2: TIT2/TALB/TPE1/TRCK/TPOS, APIC for cover, chapter frames if feasible.

    M4B/MP4 atoms: title/artist/album + chapter atom map + cover art.

    Fallback gracefully when a tag/chapter primitive is unsupported.

(Important because chapter and rich metadata handling differs significantly by container.)
6) Resume Design (CLI + GUI)

Checkpoint file: output/.autoaudio_state.json

Stores:

    Input path + hash.

    Key generation settings hash.

    Selected output format.

    Completed chapter indices.

    Completed segment indices per chapter.

    Temporary segment files and final stitched outputs.

    Metadata snapshot + last UI settings.

Resume logic:

    Validate compatibility (same input hash + relevant settings).

    Offer resume path:

        GUI: Resume button (prepopulated state on reopen).

        CLI: --resume auto|yes|no.

    Atomic writes for checkpoint updates (write temp then rename).

    Recovery paths for interrupted stitching.

7) Error Handling & Diagnostics

Create typed errors:

    InputValidationError

    MetadataExtractionError

    ComfyUIConnectionError

    ComfyUIProtocolError

    AudioStitchError

    ResumeStateError

Standards:

    User-friendly errors in GUI + terminal summary.

    Detailed debug logs in output directory.

    Retry policy for transient network calls.

    Non-zero exit codes by failure class.

8) Agent Scope Note (Portable ComfyUI)

Create Docs/AGENT_SCOPE_NOTES.md with explicit statement:

    A portable ComfyUI bundle will be added by maintainer later.

    Bundling/vendorizing/moving portable ComfyUI is out of scope for agent tasks.

    In-scope now: integration interfaces, config hooks, path detection, and fallbacks only.

9) Milestones
Milestone 1 — Core Refactor

    Move workflow to resources/workflows.

    Create pipeline + client abstractions.

Milestone 2 — Resume + Error Model

    Checkpoint engine, typed errors, logging.

Milestone 3 — Metadata Layer

    Offline extraction + optional fetch architecture + merge rules.

Milestone 4 — GUI

    PySide6 interface, drag/drop, progress, resume button + prepopulation.

Milestone 5 — Testing

    Unit tests for metadata/checkpoint.

    Integration tests with spoof ComfyUI scenarios.

10) Acceptance Criteria

    App runs fully offline by default.

    “Fetch metadata is optional and off by default.

    GUI supports drag/drop and native file dialogs across platforms.

    Resume works from both GUI and CLI and survives app restart.

    Chapter metadata is preserved where source provides it and embedded where format supports it.

    Tests pass against spoof ComfyUI without requiring real ComfyUI runtime.
