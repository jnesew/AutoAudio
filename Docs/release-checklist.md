# AutoAudio v2 release qualification

This document tracks evidence required before `release/v2.0.0` can merge into `main` and receive the `v2.0.0` tag.

## Automated gates

| Gate | Status | Evidence |
|---|---|---|
| Unit and spoof/integration suite | Passed | 108 tests after progress/ETA work |
| Import and compile checks | Passed | Python 3.12 release workspace |
| Workflow fixtures | Passed | CustomVoice and VoiceDesign adapters covered |
| Synthetic EPUB fixtures | Passed | pubparser adapter tests |
| Real Project Gutenberg EPUB | Passed | pubparser v0.1.1 parsed book 84 with metadata, cover, and normalization |
| Source-package/install smoke | Pending | Run from the final release commit |
| Exact dependency inventory | Pending | Capture the final tested platform resolution |

## Manual runtime gates

| Gate | Status | Evidence |
|---|---|---|
| Real preset generation | Passed | Maintainer manual test |
| Real VoiceDesign generation | Passed | Maintainer manual test |
| Chapter stitching | Passed | Maintainer manual test |
| FLAC output | Passed | Maintainer manual test |
| MP3 output | Passed | Maintainer manual test |
| M4B output | Passed | Maintainer manual test |
| Interruption and resume | Passed | Maintainer manual test |
| Disclosure placement | Passed | Maintainer manual test |
| AudioSeal embedding/detection | Passed indirectly | Strict assembly completed from verified segments; run retained-output verifier for final audit |
| Complete-book run | In progress | Maintainer processing run |
| C2PA signing and verification | Pending | Requires real certificate/key/toolchain test |

## Release actions

- Run `python scripts/verify.py --output-dir <completed-book-output>`.
- Exercise C2PA in both soft-fail and hard-fail modes.
- Confirm final dependency versions and notices.
- Run tests and packaged smoke checks from the exact final commit.
- Review the final diff against `main` for prohibited reference-audio or cloning paths.
- Merge `release/v2.0.0` into `main` only after all required gates pass.
- Tag the merged commit `v2.0.0` and publish release notes describing the intentional v1 incompatibility.
