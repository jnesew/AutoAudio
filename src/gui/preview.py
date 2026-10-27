"""Network-inert narration planning using the conversion pipeline's text policies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.checkpoint import CheckpointStore, sha256_file
from core.config import AppConfig
from core.plan import BookPlanStore
from core.narration_selection import (
    apply_chapter_choices,
    generation_settings,
    segment_policy_for,
    selection_payload,
)
from core.narration_text import apply_rules_to_chapters, load_replacement_rules
from core.pipeline import (
    _extract_text_blocks_with_toc_info,
    build_qwen_book_plan,
    group_blocks_into_chapters,
    group_paragraphs_into_chapters,
)
from metadata.epub_parser import parse_epub
from metadata.extractors import extract_text_fallback_metadata
from metadata.source_mode import detect_source_mode


def file_identity(path):
    stat = Path(path).stat()
    return (
        str(Path(path).resolve()),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_ino,
        stat.st_dev,
    )


@dataclass(frozen=True)
class Preview:
    chapters: tuple
    plan: object
    metadata: object
    notes: tuple[str, ...]
    source_identity: tuple
    saved_plan: object = None


def build_preview(args, profile, cache=None):
    identity = file_identity(args.input_book)
    mode = detect_source_mode(args.input_book, args.source_mode)
    key = (identity, mode, args.narrate_toc)
    parsed = cache.get(key) if cache is not None else None
    if parsed is None:
        if mode == "epub":
            epub = parse_epub(args.input_book, narrate_toc=args.narrate_toc)
            blocks = list(epub.text_blocks)
            metadata = epub.metadata
            notes = [f"{d.code}: {d.message}" for d in epub.diagnostics]
            notes.append(
                "EPUB documents containing 50 characters or fewer are omitted by the current adapter."
            )
        else:
            blocks, omitted = _extract_text_blocks_with_toc_info(
                args.input_book, narrate_toc=args.narrate_toc
            )
            metadata = extract_text_fallback_metadata(args.input_book)
            notes = [f"Omitted {omitted} confidently identified contents paragraphs."]
            notes.append(
                "Text paragraphs containing 20 characters or fewer are omitted by the current adapter."
            )
        parsed = (blocks, metadata, notes, sha256_file(args.input_book))
        if file_identity(args.input_book) != identity:
            raise ValueError(
                "The source changed while it was being read. Refresh the preview."
            )
        if cache is not None:
            cache.clear()  # Bound memory to the current source.
            cache[key] = parsed
    blocks, metadata, notes, source_hash = parsed
    chapters = (
        group_blocks_into_chapters(blocks, args.pages_per_chapter)
        if mode == "epub"
        else group_paragraphs_into_chapters(
            blocks,
            target_words_per_chapter=args.target_words_per_chapter,
            min_paragraphs_per_chapter=args.min_paragraphs_per_chapter,
        )
    )
    chapters, count = apply_rules_to_chapters(
        chapters, load_replacement_rules(args.replacement_file, args.replacement_rules)
    )
    choices = selection_payload(args)
    chapters = apply_chapter_choices(chapters, choices)
    if not chapters:
        raise ValueError(
            "No narratable chapters found. The current adapter omits EPUB documents of 50 characters or fewer and text paragraphs of 20 characters or fewer."
        )
    policy = segment_policy_for(args, generation_settings(args, profile))
    plan = build_qwen_book_plan(
        chapters,
        input_hash=source_hash,
        settings_hash="preview",
        workflow_hash="preview",
        segment_policy=policy,
        excluded_chapters=tuple(choices.get("excluded_chapters", [])),
    )
    extra = [
        f"{count} replacement matches. Chapter edits are applied after replacement rules.",
        "Inspect front matter for embedded contents, epigraphs, and credits before converting.",
        "Each generated chapter also includes the fixed audible AI disclosure.",
    ]
    if args.fetch_metadata:
        extra.append(
            "Online metadata is fetched only when you start conversion; preview uses local metadata."
        )
    saved_plan = None
    state_dir = AppConfig.state_dir_for(args.output_dir)
    try:
        checkpoint = CheckpointStore(state_dir).load()
        if checkpoint and checkpoint.get("status") in {
            "running",
            "failed",
            "cancelled",
        }:
            if checkpoint.get("input", {}).get("sha256") == source_hash:
                saved_plan = BookPlanStore(state_dir).load(
                    expected_sha256=checkpoint.get("plan", {}).get("sha256")
                )
                extra.append(
                    "Resume uses the separate saved plan shown below. Restore saved settings if you changed this draft."
                )
    except ValueError as exc:
        extra.append(f"Saved plan cannot be displayed: {exc}")
    return Preview(
        tuple(chapters), plan, metadata, tuple(notes + extra), identity, saved_plan
    )


def bounded_sample(text: str) -> str:
    # A single normal segment; bound both whitespace words and unbroken/CJK text.
    words = " ".join(text.split()[:50])
    cut = words[:180]
    if len(words) > 180 and not words[180].isspace() and not cut[-1].isspace():
        boundary = cut.rfind(" ")
        if boundary >= 90:
            cut = cut[:boundary]
    return cut.strip()
