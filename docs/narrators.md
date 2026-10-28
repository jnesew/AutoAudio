# Narrators, voices, and Qwen settings

## Provider voice selection

ComfyUI uses the bundled narrator profiles described below. OpenAI-compatible and ElevenLabs adapters instead use an existing endpoint voice ID supplied in **Voice ID** or `--tts-voice`.

For OpenAI-compatible synthesis, AutoAudio sends the selected profile's voice/style instruction when the endpoint supports the standard `instructions` field. ElevenLabs synthesis sends text, model ID, and optional language code; it does not call voice creation or cloning APIs.

ElevenLabs discovery is an explicit convenience action. Returned `cloned` and `professional` categories are excluded, leaving only `premade` and `generated` voices. No provider is queried merely because its configuration is selected or restored.

## Narrator modes

AutoAudio exposes two non-cloning Qwen3-TTS modes:

- **Preset / CustomVoice:** uses a built-in Qwen speaker identity. This is the stable default for multi-segment books.
- **VoiceDesign:** creates a voice from a textual description without reference audio. It is experimental because vocal identity can drift between independent generations.

No narrator setting accepts, uploads, or caches reference-voice audio.

## Bundled preset profiles

The narrator dropdown contains a neutral long-form profile for every built-in CustomVoice speaker:

| Profile ID | Display name | Qwen speaker | Native language |
|---|---|---|---|
| `preset-aiden-neutral` | Aiden — Clear (default) | `Aiden` | English (American) |
| `preset-eric-neutral` | Eric — Neutral | `Eric` | Chinese (Sichuan dialect) |
| `preset-dylan-neutral` | Dylan — Natural | `Dylan` | Chinese (Beijing dialect) |
| `preset-ono-anna-neutral` | Ono Anna — Light | `Ono_anna` | Japanese |
| `preset-ryan-neutral` | Ryan — Dynamic | `Ryan` | English |
| `preset-serena-neutral` | Serena — Warm | `Serena` | Chinese |
| `preset-sohee-neutral` | Sohee — Expressive | `Sohee` | Korean |
| `preset-uncle-fu-neutral` | Uncle Fu — Low | `Uncle_fu` | Chinese |
| `preset-vivian-neutral` | Vivian — Bright | `Vivian` | Chinese |

`preset-aiden-neutral` is the default for new jobs. Ryan is another native English option. Qwen supports
cross-language speech but recommends each speaker's native language for best quality; see its
[official speaker list](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice#supported-speakers).

Profiles are packaged in `default_profiles.json` and lock the complete generation configuration used for
checkpoint compatibility. Existing profile definitions and hashes are unchanged. Saved drafts retain their
selected voice; use **Restore saved run settings** before resuming an older GUI job. For an older CLI job
that used the previous default, pass `--narrator-profile preset-eric-neutral` with its original settings.

New jobs use the selected narration voice for the chapter disclosure as well, with neutral delivery for
preset voices. Resumed jobs retain their saved disclosure policy and audio. See
[chapter disclosure](provenance-and-resume.md#chapter-disclosure).

## Designed voice profile

`design-warm-narrator` provides the bundled VoiceDesign starting point. Its instruction requests a warm adult audiobook narrator with a steady register, clear diction, natural pacing, and restrained emotion.

VoiceDesign uses the 1.7B model. Keeping the seed and instruction fixed can reduce variation but does not guarantee identical vocal identity across segments.

## Adjustable settings

The GUI applies a profile and then allows supported overrides:

- Built-in speaker for preset mode
- Voice/style instruction
- Model choice
- Device and precision
- Language and seed
- Maximum generated tokens
- Top-p, top-k, temperature, and repetition penalty
- Attention implementation
- Model unload policy

Changing any synthesis-affecting value changes checkpoint compatibility. A prior ComfyUI job can resume only when its effective narrator settings and workflow bytes still match. HTTP-provider resume identity includes the adapter, endpoint, model, voice, response format, and language code.

For reliable long-form output:

- Prefer preset mode.
- Keep one profile and seed for the entire book.
- Leave model unloading disabled unless memory pressure requires it.
- Test a short representative chapter before starting a full book.

The default segmentation policy targets 150 words with a 220-word ceiling for preset voices, and
190 words with a 240-word ceiling for designed voices. `--target-words-per-segment` and
`--max-words-per-segment` override these defaults. Character, UTF-8-byte, and CJK limits still apply;
see [Narration text preparation](narration-text.md#request-size-limits).

In the GUI, use **Generate short sample** to check a bounded excerpt and audible disclosure before a
full conversion. See [GUI usage](desktop-workflow.md#preview-and-samples).
