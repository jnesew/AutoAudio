# Third-party dependencies

This project is MIT-licensed for original source code in this repository.
Third-party dependencies are licensed under their own terms.

## Runtime dependencies

| Package | Declared in | License (upstream) | Notes |
|---|---|---|---|
| pubparser (`v0.1.0` tag; package metadata `0.1.0.dev0`) | `requirements.txt` | MIT License | EPUB parsing, metadata, cover discovery, and Project Gutenberg normalization. |
| websocket-client | `requirements.txt` | Apache-2.0 | WebSocket client for ComfyUI events. |
| PySide6 | `requirements.txt` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only OR Commercial | Qt for Python licensing model; review LGPL/GPL/commercial obligations based on distribution strategy. |
| numpy | `requirements.txt` | BSD-3-Clause | Numeric processing used by audio watermarking. |
| torch | `requirements.txt` | BSD-3-Clause | Tensor runtime used by AudioSeal. |
| librosa | `requirements.txt` | ISC | Audio decoding and resampling for watermarking. |
| soundfile | `requirements.txt` | BSD-3-Clause | Audio serialization for watermarking. |
| audioseal | `requirements.txt` | MIT License | Non-audible audio watermarking. |

## License hygiene checklist

- Keep this file updated when adding/removing dependencies.
- Verify license for each pinned version before release.
- Confirm compatibility with your intended distribution model (open source, commercial, SaaS, binary distribution).
- Preserve required notices and attribution texts where required by upstream licenses.
