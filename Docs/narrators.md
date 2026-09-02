# Narrators and Qwen settings

## Narrator modes

AutoAudio exposes two non-cloning Qwen3-TTS modes:

- **Preset / CustomVoice:** uses a built-in Qwen speaker identity. This is the stable default for multi-segment books.
- **VoiceDesign:** creates a voice from a textual description without reference audio. It is experimental because vocal identity can drift between independent generations.

No narrator setting accepts, uploads, or caches reference-voice audio.

## Bundled preset profiles

The narrator dropdown contains a neutral long-form profile for every built-in CustomVoice speaker:

| Profile ID | Display name | Qwen speaker |
|---|---|---|
| `preset-eric-neutral` | Eric — Neutral | `Eric` |
| `preset-aiden-neutral` | Aiden — Clear | `Aiden` |
| `preset-dylan-neutral` | Dylan — Natural | `Dylan` |
| `preset-ono-anna-neutral` | Ono Anna — Light | `Ono_anna` |
| `preset-ryan-neutral` | Ryan — Dynamic | `Ryan` |
| `preset-serena-neutral` | Serena — Warm | `Serena` |
| `preset-sohee-neutral` | Sohee — Expressive | `Sohee` |
| `preset-uncle-fu-neutral` | Uncle Fu — Low | `Uncle_fu` |
| `preset-vivian-neutral` | Vivian — Bright | `Vivian` |

`preset-eric-neutral` remains the default. Profiles are packaged in `default_profiles.json` and lock the complete generation configuration used for checkpoint compatibility.

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

Changing any synthesis-affecting value changes checkpoint compatibility. A prior job can resume only when its effective narrator settings and workflow bytes still match.

For reliable long-form output:

- Prefer preset mode.
- Keep one profile and seed for the entire book.
- Leave model unloading disabled unless memory pressure requires it.
- Test a short representative chapter before starting a full book.
