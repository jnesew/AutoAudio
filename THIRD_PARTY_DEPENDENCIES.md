# Third-party dependencies

This project is MIT-licensed for original source code in this repository.
Third-party dependencies are licensed under their own terms.

## Direct runtime dependencies

| Package | Requirement | License / notice scope | Notes |
|---|---|---|---|
| pubparser | exact commit `6a03fa2` (`v0.1.0`; metadata `0.1.0.dev0`) | MIT | EPUB parsing, metadata, cover discovery, and Project Gutenberg normalization. |
| websocket-client | `>=1.8` | Apache-2.0 | WebSocket client for ComfyUI events. |
| PySide6 | `>=6.7` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only OR Commercial | Includes Qt/Shiboken components. Binary redistribution must preserve the license files shipped by the selected wheels and satisfy the chosen Qt licensing route. |
| numpy | unpinned | BSD-3-Clause plus bundled component notices | Numeric processing used by audio watermarking. The selected wheel can contain separately licensed numeric libraries. |
| torch | unpinned | BSD-3-Clause plus bundled third-party notices | Tensor runtime used by AudioSeal. Preserve the selected wheel's `LICENSE` and `NOTICE`; the upstream top-level license is copied at `LICENSES/pytorch-BSD-3-Clause.txt`. |
| librosa | unpinned | ISC plus dependency notices | Audio decoding and resampling for watermarking. |
| soundfile | unpinned | BSD-3-Clause for PySoundFile; libsndfile is LGPL-2.1-or-later | Audio serialization for watermarking; binary wheels may bundle libsndfile. |
| audioseal | unpinned | MIT | Non-audible audio watermarking. Model-weight terms must also be reviewed for the selected distribution. |

`LICENSES/third-party-licenses.md` is an installed-environment snapshot, not a lock file. See `LICENSES/README.md` for its scope and known metadata limitations.

## External runtime components

ComfyUI, the Qwen3-TTS custom-node package, Qwen3-TTS models, FFmpeg, and FFprobe are operational prerequisites but are not installed or redistributed by this repository's `requirements.txt`. A distributor that bundles any of them must add the exact selected versions, licenses, model terms, and notices to the release inventory.

## License hygiene checklist

- Keep this file updated when adding/removing dependencies.
- Resolve and lock exact package versions during release qualification.
- Regenerate the installed-environment notice snapshot from that exact lock on every supported platform.
- Confirm compatibility with your intended distribution model (open source, commercial, SaaS, binary distribution).
- Preserve required notices and attribution texts where required by upstream licenses.
