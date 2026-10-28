# Narration sample for the README

The [published sample](narration-demo%20-%20Part_001.mp3) is a 37-second English rendering of the bundled
passage using Qwen3-TTS 1.7B and Aiden, including the audible disclosure. Its
[verification record](narration-demo%20-%20Part_001.mp3.ai.json) retains the expected AudioSeal payloads.

The [video preview](narration-sample.mp4) pairs the same passage with a
[static cover](narration-sample-cover.png). It uses H.264 video and AAC audio and is under 0.5 MB.
Both AudioSeal messages were recovered exactly from the final AAC waveform; its
[verification record](narration-sample.mp4.ai.json) links it to the original MP3.
The MP3 remains the original audio download.

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

For an inline GitHub player:

1. Open the repository's `README.md` on GitHub and choose **Edit**.
2. Drag `docs/media/narration-sample.mp4` into the editor, or use its attachment control.
3. Wait for the upload to finish. Replace the **Listen to a narration sample** link with the returned
   attachment URL on its own line, with a blank line before and after it. Keep the sample description
   and MP3 download link below it.
4. Check **Preview** for the player and test playback before saving.

The attachment uploads immediately. To keep the README change in your local Git workflow, copy the
returned URL into the local README, discard the browser edit, then commit and push normally.

GitHub documents video attachment support and recommends H.264 for compatibility; see
[attaching media](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files).
Retain the original MP3 and sidecar alongside the listening preview; converting audio to a new video
container does not transfer an original C2PA signature.

To check either published file locally:

```bash
autoaudio-verify --file 'docs/media/narration-demo - Part_001.mp3' --check-signal
autoaudio-verify --file docs/media/narration-sample.mp4 --check-signal
```
