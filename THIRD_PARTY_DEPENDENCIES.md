# Third-party dependencies

This project is MIT-licensed for original source code in this repository.
Third-party dependencies are licensed under their own terms.

## Runtime dependencies

| Package | Declared in | License (upstream) | Notes |
|---|---|---|---|
| beautifulsoup4 | `requirements.txt` | MIT License | HTML/text parsing from EPUB chapters. |
| EbookLib | `requirements.txt` | AGPL-3.0-or-later | EPUB parsing library; review AGPL obligations for your distribution/use model. |
| websocket-client | `requirements.txt` | Apache-2.0 | WebSocket client for ComfyUI events. |
| PySide6 | `requirements.txt` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only OR Commercial | Qt for Python licensing model; review LGPL/GPL/commercial obligations based on distribution strategy. |

## License hygiene checklist

- Keep this file updated when adding/removing dependencies.
- Verify license for each pinned version before release.
- Confirm compatibility with your intended distribution model (open source, commercial, SaaS, binary distribution).
- Preserve required notices and attribution texts where required by upstream licenses.
