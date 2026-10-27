# Narration text preparation

AutoAudio keeps source extraction separate from the text sent to TTS. Tables of contents are filtered
first, extracted text is grouped into chapters, and user replacement rules are applied once. Chapter
choices and spoken-text edits are then applied before segmentation into the frozen resumable BookPlan.

## Table-of-contents handling

Confidently identified tables of contents are not narrated by default. EPUB identification comes from
`pubparser` package/navigation metadata, explicit XHTML semantics, and conservative content patterns.
Plain-text identification requires an early Contents heading followed by at least three chapter-like or
page-numbered entries. Ambiguous candidates remain in the narration.

Use the GUI checkbox or `--narrate-toc` to retain detected TOCs. This setting participates in the resume
compatibility hash.

### Current extraction limits

An embedded contents list in a document that also contains an epigraph, title page, or narrative may
remain unclassified and be retained. **Preview** exposes the resulting chapter text so you can exclude
the chapter or edit out the contents region while retaining the surrounding prose.

The EPUB adapter currently omits nonempty documents containing 50 characters or fewer. The text adapter
omits paragraphs containing 20 characters or fewer. Short poems, dedications, and epigraphs can therefore
be lost before grouping. These are AutoAudio consumer policies, separate from whether pubparser correctly
returns a document's text. Inspect sources with meaningful short passages before converting them.

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

## Chapter inclusion and spoken-text edits

**Preview** shows the grouped chapter numbers, inclusion checkboxes, and exact planned speech segments.
Uncheck a chapter to exclude it, or edit spoken text and press **Apply chapter edit**. The corresponding
CLI options are repeatable `--exclude-chapter N` and
`--chapter-text-override '{"chapter": N, "text": "Edited narration."}'`.

Numbers are one-based and refer to chapters after grouping; exclusions retain the original numbering.
At least one chapter must remain included. Overrides must have nonempty text, use an existing chapter
number, and cannot specify the same chapter twice. They replace the spoken body after replacement rules,
so an edit does not undergo another round of substitutions. An override can retain short text within an
already included chapter, but cannot restore a source document omitted before grouping.

After changing chapter grouping, check the numbering and reset outdated choices. Exclusions and overrides
are persisted in checkpoint UI state and affect resume compatibility. Use a new output destination or
GUI variant when changing a job's narration. **Saved plan for Resume** shows the frozen prior requests
separately from the editable new-run plan. See the [CLI reference](cli-reference.md#narration-text-preparation)
and [GUI usage](desktop-workflow.md).

## Request-size limits

In addition to the word target and maximum, requests have hard limits of 2,400 characters and 7,200 UTF-8
bytes. Unspaced CJK text also has a character ceiling of twice the maximum-word setting (440 CJK
characters for the default preset policy). Unicode sentence boundaries and these limits prevent a single
long unbroken token or unspaced paragraph from becoming an unbounded request.

Compatible older frozen plans keep their segmentation when every persisted segment still fits the size
limits. Oversized legacy plans require a new destination and plan. These planning bounds are not a
guarantee of a provider's token or duration capacity; test the chosen voice and language with a sample.
