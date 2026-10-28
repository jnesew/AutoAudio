# Narration sample for the README

Keep curated public demo clips here. Generate the clip separately from normal library jobs, which remain
under the ignored `audiobook_output/` directory.

Aim for **30–45 seconds total**, including the audible disclosure and a complete short passage. Around
80–100 narration words lets listeners judge pacing, pronunciation, and continuity without a long download.
The original example passage in [narration-demo.txt](../../examples/narration-demo.txt) includes narration,
brief dialogue, a name, and a number.

From the repository root, with the normal speech provider configured, render that short source as one chapter:

```bash
autoaudio --input-book examples/narration-demo.txt \
  --output-dir audiobook_output/readme-demo \
  --narrator-profile preset-aiden-neutral --output-format mp3
```

Add your usual provider/server arguments when needed. This converts the short example source, not a full
book. The GUI's **Generate short sample** is capped at 180 characters, so it is useful for a quick voice
check but shorter than this public demo target.

Check the exported watermark signal before publishing:

```bash
autoaudio-verify --output-dir audiobook_output/readme-demo --check-signal
```

Copy the exported chapter MP3 and its matching `.mp3.ai.json` sidecar into this directory, keeping their
generated filenames together. This preserves the disclosure, watermark timeline, and verification record
without trimming or reprocessing the audio. If signed, keep the signed original intact as well.

Add a **Listen to a narration sample** link near the README's opening paragraph, before **Features**, once
the clip exists. Label the sample with its provider, model, voice, language, and source text; for example,
“Qwen3-TTS 1.7B · Aiden · English · bundled demo passage.” This makes clear which setup the clip represents.

For an inline GitHub player, an MP4 with a static cover/title image and the audio is another option.
GitHub documents video attachment support and recommends H.264 for compatibility; see
[attaching media](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files).
Use the uploaded video URL in the README. Retain the original MP3 and sidecar alongside that listening
preview; wrapping audio in a new video container does not transfer its original C2PA signature.
