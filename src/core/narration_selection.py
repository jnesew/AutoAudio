"""Explicit chapter choices shared by preview and conversion (one-based UI numbers)."""

from __future__ import annotations

from core.segmentation import SegmentPolicy, default_segment_policy


def selection_payload(args) -> dict:
    excluded = getattr(args, "excluded_chapters", None) or []
    overrides = getattr(args, "chapter_text_overrides", None) or []
    if not isinstance(excluded, (list, tuple)) or any(
        type(n) is not int or n < 1 for n in excluded
    ):
        raise ValueError("Excluded chapters must be positive chapter numbers.")
    if not isinstance(overrides, (list, tuple)):
        raise ValueError("Chapter text overrides must be a list.")
    normalized = {}
    for item in overrides:
        if (
            not isinstance(item, dict)
            or set(item) != {"chapter", "text"}
            or type(item["chapter"]) is not int
            or item["chapter"] < 1
            or not isinstance(item["text"], str)
            or not item["text"].strip()
        ):
            raise ValueError(
                "Each chapter override needs a positive chapter number and nonempty text."
            )
        if item["chapter"] in normalized:
            raise ValueError("Duplicate chapter text override.")
        normalized[item["chapter"]] = item["text"].strip()
    payload = {}
    # Empty choices deliberately retain compatibility with earlier checkpoints.
    if excluded:
        payload["excluded_chapters"] = sorted(set(excluded))
    if normalized:
        payload["chapter_text_overrides"] = [
            {"chapter": n, "text": text} for n, text in sorted(normalized.items())
        ]
    return payload


def apply_chapter_choices(chapters, payload):
    excluded = set(payload.get("excluded_chapters", []))
    overrides = {
        item["chapter"]: item["text"]
        for item in payload.get("chapter_text_overrides", [])
    }
    if (excluded | overrides.keys()) - set(range(1, len(chapters) + 1)):
        raise ValueError(
            "Chapter choices refer to chapters outside the current plan. Reset the choices after changing parsing."
        )
    if chapters and len(excluded) == len(chapters):
        raise ValueError("Include at least one chapter.")
    return [
        (title, overrides.get(n, text)) for n, (title, text) in enumerate(chapters, 1)
    ]


def generation_settings(args, profile):
    mapping = {"instruct": "voice_instruct"}
    keys = (
        "speaker",
        "instruct",
        "model_choice",
        "device",
        "precision",
        "language",
        "seed",
        "max_new_tokens",
        "top_p",
        "top_k",
        "temperature",
        "repetition_penalty",
        "attention",
        "unload_model_after_generate",
    )
    return profile.with_overrides(
        **{key: getattr(args, mapping.get(key, key), None) for key in keys}
    )


def segment_policy_for(args, settings):
    default = default_segment_policy(settings.voice_mode)
    return SegmentPolicy(
        target_words=args.target_words_per_segment
        if args.target_words_per_segment is not None
        else default.target_words,
        max_words=args.max_words_per_segment
        if args.max_words_per_segment is not None
        else default.max_words,
    )
