# AutoAudio v2 release qualification

This document tracks evidence required before `release/v2.0.0` can merge into `main` and receive the `v2.0.0` tag.

## Automated gates

| Gate | Status | Evidence |
|---|---|---|
| Unit and spoof/integration suite | Passed | 137 tests after packaging, installed-resource, and documentation-link coverage |
| Import and compile checks | Passed | Python 3.12 release workspace |
| Workflow fixtures | Passed | CustomVoice and VoiceDesign adapters covered |
| Synthetic EPUB fixtures | Passed | pubparser adapter tests |
| Real Project Gutenberg EPUB | Passed | pubparser v0.1.1 parsed book 84 with metadata, cover, and normalization |
| Stitched cover art | Passed | Real FFmpeg encodes retain chapter metadata, AI marking, and attached JPEG artwork in FLAC, MP3, and M4B |
| C2PA container compatibility | Passed | Live `c2patool 0.27.16` ES256 signing and parse-back succeeded for FLAC, MP3, and M4B; cover art and AI-marking audit remained valid |
| Source-package and wheel smoke | Passed on packaging candidate | Built sdist, rebuilt wheel from the sdist, and installed AutoAudio plus pinned pubparser into a fresh no-index virtual environment; both commands and packaged resources passed |
| Clean-environment dependency install | Pending | Package-only smoke passed; resolve and install the full platform dependency set after final versions are selected |
| Exact dependency inventory | Pending | Capture the final tested platform resolution |

## Manual runtime gates

| Gate | Status | Evidence |
|---|---|---|
| Real preset generation | Passed | Maintainer manual test |
| Real VoiceDesign generation | Passed | Maintainer manual test |
| Chapter stitching | Passed | Maintainer manual test |
| Stitched-part cover art | Passed | Maintainer re-stitched a part after the FFmpeg option-order fix and confirmed its cover |
| FLAC output | Passed | Maintainer manual test |
| MP3 output | Passed | Maintainer manual test |
| M4B output | Passed | Maintainer manual test |
| Interruption and resume | Passed | Maintainer manual test |
| Disclosure placement | Passed | Maintainer manual test |
| AudioSeal embedding/detection | Retest planned | Maintainer confirmed embedding/detection on the previous candidate; repeat after the new automatic device policy |
| Complete-book run | Passed | Maintainer completed the full processing and verification flow |
| C2PA signing and verification | Retest planned | Live and maintainer tests passed on the previous candidate; repeat provenance verification on the final candidate |

## Release actions

- Run `autoaudio-verify --output-dir <completed-book-output>`.
- Exercise C2PA in both soft-fail and hard-fail modes.
- Confirm final dependency versions and notices.
- Run tests and packaged smoke checks from the exact final commit.
- Review the final diff against `main` for prohibited reference-audio or cloning paths.
- Merge `release/v2.0.0` into `main` only after all required gates pass.
- Tag the merged commit `v2.0.0` and publish release notes describing the intentional v1 incompatibility.
