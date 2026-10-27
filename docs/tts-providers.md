# TTS providers

AutoAudio routes synthesis through a small provider interface. The audiobook pipeline asks the selected adapter for an audio segment and receives bytes plus a container extension; parsing, segmentation, AudioSeal marking, chapter assembly, resume, and provenance remain provider-independent.

| Provider | Synthesis contract | Voice selection | Discovery |
|---|---|---|---|
| `comfyui` | Existing Qwen workflow submission | Bundled preset or text-designed profile | Built-in speaker list; local only |
| `openai-compatible` | `POST /v1/audio/speech` | Manually entered existing voice ID | Unsupported because there is no standard listing route |
| `elevenlabs` | `POST /v1/text-to-speech/{voice_id}` | Existing voice ID | Explicit `GET /v2/voices`; only `premade` and `generated` results are retained |

## Outbound-action policy

Provider construction, selection, configuration editing, checkpoint restoration, and GUI startup perform no endpoint requests. There are only two TTS request triggers:

1. The user starts or resumes a conversion, or presses **Generate short sample**. Each planned narration/disclosure segment is then synthesized as part of that requested job.
2. The user presses **Discover voices** or invokes `--discover-voices`. Discovery is never run as a provider probe or selection side effect.

The GUI enables remote discovery only for ElevenLabs. ComfyUI speakers are already bundled locally. OpenAI-compatible implementations vary and have no portable voice-list API, so their voice IDs remain manual.

## OpenAI-compatible endpoints

Required settings are a model and voice. The base URL defaults to `http://127.0.0.1:8000/v1`, suitable for a locally hosted compatible server. If the configured base ends in `/v1`, AutoAudio appends `/audio/speech`; otherwise it appends `/v1/audio/speech`. An API key is optional so local servers can run without authentication. If the named key environment variable is populated, AutoAudio sends it as a bearer token.

Example:

```bash
export OPENAI_API_KEY="..."
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/output \
  --tts-provider openai-compatible \
  --tts-base-url https://api.openai.com/v1 \
  --tts-api-key-env OPENAI_API_KEY \
  --tts-model gpt-4o-mini-tts \
  --tts-voice alloy \
  --tts-response-format wav
```

## ElevenLabs existing voices

An API key and existing voice ID are required for synthesis. The base URL defaults to `https://api.elevenlabs.io`, the model defaults to `eleven_multilingual_v2`, and the response format defaults to `mp3_44100_128`.

Discover eligible existing voices only when needed:

```bash
export ELEVENLABS_API_KEY="..."
autoaudio \
  --tts-provider elevenlabs \
  --tts-api-key-env ELEVENLABS_API_KEY \
  --discover-voices
```

Then pass one returned ID to a user-started conversion:

```bash
autoaudio \
  --input-book /path/to/book.epub \
  --output-dir /path/to/output \
  --tts-provider elevenlabs \
  --tts-api-key-env ELEVENLABS_API_KEY \
  --tts-voice VOICE_ID
```

The adapter implements no upload, instant-clone, professional-clone, or voice-creation route. Discovery also filters clone categories from its result set.

## Request recovery and limits

HTTP audio responses are read in chunks with a 64 MiB ceiling; remote voice-list responses have a 2 MiB ceiling. Cancellation and the shared request deadline are checked between reads and retry backoff steps. A blocking connection/header wait is still bounded by the configured socket timeout; this is not an immediate network abort.

Read-only discovery can retry transient failures. Synthesis POSTs retry only an explicit HTTP 429 rejection. Ambiguous read/network failures and server errors are reported without automatic resubmission, because the provider may already have accepted or generated the audio. Check provider state before manually retrying an ambiguous synthesis failure.

## External runtime reproducibility

Checkpoint settings and workflow hashes identify the request configuration. An external server can
change its model weights or implementation while keeping the same URL and workflow, so those hashes
alone do not prove identical speech generation. Keep the server, models, and node revisions stable
across a resumable job. Explicit backend version metadata can be recorded when supplied by the workflow;
AutoAudio does not query exploratory version endpoints when it is absent.

## Adding another provider

A new adapter implements the `SpeechProvider` contract in `src/tts/base.py`, publishes a non-secret `ProviderIdentity`, and is selected in `src/tts/router.py`. Network activity belongs inside `generate_audio` or an explicitly invoked `discover_voices`; constructors and routing must remain network-inert. Adapter compatibility data must include every non-secret setting that could change generated audio so resume cannot mix providers or voices.
