# Agent Scope Notes

## Portable ComfyUI Bundle (Future Maintainer Task)

A portable ComfyUI distribution will be added to this repository by the maintainer in a future change.

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
4. Future compatibility with a portable runtime location (to be defined by maintainer).

## Placeholder runtime location

Future portable runtime path is expected to be declared by maintainer (example only):  
`third_party/ComfyUI-portable/`

Until explicitly added by maintainer, treat this path as optional/non-existent.
