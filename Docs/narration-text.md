# Narration text preparation

AutoAudio keeps source extraction separate from the text sent to TTS. Tables of contents are filtered
first, user replacement rules are applied once, and the resulting spoken text is then segmented and
frozen into the resumable BookPlan.

## Table-of-contents handling

Confidently identified tables of contents are not narrated by default. EPUB identification comes from
`pubparser` package/navigation metadata, explicit XHTML semantics, and conservative content patterns.
Plain-text identification requires an early Contents heading followed by at least three chapter-like or
page-numbered entries. Ambiguous candidates remain in the narration.

Use the GUI checkbox or `--narrate-toc` to retain detected TOCs. This setting participates in the resume
compatibility hash.

## Replacement rules

The Book tab contains a per-run replacement table. Rules may also be shared across books in a UTF-8 JSON
file selected in the GUI or passed with `--replacement-file`:

```json
{
  "version": 1,
  "replacements": [
    {
      "source": "IV",
      "spoken": "four",
      "match": "whole-word",
      "scope": "body",
      "case_sensitive": true
    },
    {
      "source": "Dr.",
      "spoken": "Doctor",
      "match": "literal",
      "scope": "all",
      "case_sensitive": false
    }
  ]
}
```

A copyable file is included at [`examples/replacement-rules.json`](../examples/replacement-rules.json).

Available match modes are:

- `whole-word`: requires Unicode word boundaries on both sides and is the safest default;
- `literal`: matches the source characters anywhere;
- `regex`: treats `source` as a Python regular expression while inserting `spoken` literally.

Scopes are `body`, `title`, and `all`. Title replacements affect derived chapter names and markers;
body replacements affect text sent to TTS. File rules are applied before table/CLI rules. At the same
text position, the longest match wins; overlapping matches are applied only once against the original
text, so replacement output cannot trigger another rule.

For simple CLI use, repeat `--replacement-rule SOURCE=SPOKEN`. This shorthand creates a case-sensitive,
whole-word body rule. Pass a JSON object as the argument when advanced fields are needed.

Replacement content, rather than merely the file path, participates in the resume compatibility hash.
Editing a rule therefore requires a new plan and prevents stale narration segments from being reused.
