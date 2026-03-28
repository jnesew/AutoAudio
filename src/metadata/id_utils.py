from __future__ import annotations

import re


def guess_gutenberg_id(path_or_identifier: str | None) -> str | None:
    if not path_or_identifier:
        return None

    match = re.search(r"(?:gutenberg[^\d]*|\bpg)(\d{2,7})\b", path_or_identifier.lower())
    if match:
        return match.group(1)

    plain_match = re.search(r"\b(\d{2,7})\b", path_or_identifier)
    if plain_match:
        return plain_match.group(1)
    return None
