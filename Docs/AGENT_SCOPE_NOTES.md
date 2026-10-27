# Agent Scope Notes

## External ComfyUI runtime

ComfyUI, its model files, and its container/dependency management are external to AutoAudio. Runtime
packaging changes are a separate maintainer decision; current work must not assume a bundled distribution.

### Out of scope for agents
Agents must **not**:
- Vendor, download, or commit a portable ComfyUI distribution.
- Move/rename portable ComfyUI runtime assets.
- Rewrite third-party ComfyUI internals to “fit” AutoAudio.
- Add binary payloads for ComfyUI packaging.

### In scope for agents
Agents **may**:
- Build integration abstractions (client interfaces, adapters).
- Add config options for ComfyUI endpoint/runtime path detection.
- Implement graceful fallback when ComfyUI runtime is absent.
- Add test doubles/spoof endpoints for CI and local testing.
- Improve user messaging/documentation around runtime prerequisites.

## Practical expectation

Current implementation work should target:
1. Clean interface boundaries.
2. Robust error handling.
3. Testability without portable runtime assets.
