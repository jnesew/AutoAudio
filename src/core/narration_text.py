from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


NARRATION_TEXT_POLICY_VERSION = "narration-text-v1"
REPLACEMENT_RULE_SCHEMA_VERSION = 1
MAX_REPLACEMENT_RULES = 1_000
MAX_REPLACEMENT_FILE_BYTES = 1_000_000

MATCH_MODES = ("whole-word", "literal", "regex")
RULE_SCOPES = ("body", "title", "all")


class ReplacementRuleError(ValueError):
    """Raised when narration replacement configuration is unsafe or malformed."""


@dataclass(frozen=True)
class ReplacementRule:
    source: str
    spoken: str
    match: str = "whole-word"
    scope: str = "body"
    case_sensitive: bool = True

    def __post_init__(self) -> None:
        if not self.source:
            raise ReplacementRuleError("Replacement rule source cannot be empty.")
        if self.match not in MATCH_MODES:
            raise ReplacementRuleError(
                f"Unknown replacement match mode {self.match!r}; expected one of {', '.join(MATCH_MODES)}."
            )
        if self.scope not in RULE_SCOPES:
            raise ReplacementRuleError(
                f"Unknown replacement scope {self.scope!r}; expected one of {', '.join(RULE_SCOPES)}."
            )
        if self.match == "regex":
            try:
                re.compile(self.source)
            except re.error as exc:
                raise ReplacementRuleError(f"Invalid replacement regular expression {self.source!r}: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "spoken": self.spoken,
            "match": self.match,
            "scope": self.scope,
            "case_sensitive": self.case_sensitive,
        }


@dataclass(frozen=True)
class ReplacementResult:
    text: str
    count: int


def _coerce_rule(payload: Any, *, location: str) -> ReplacementRule:
    if isinstance(payload, ReplacementRule):
        return payload
    if isinstance(payload, str):
        if payload.lstrip().startswith("{"):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ReplacementRuleError(f"Invalid JSON replacement rule at {location}: {exc}") from exc
        else:
            if "=" not in payload:
                raise ReplacementRuleError(
                    f"Replacement rule at {location} must use SOURCE=SPOKEN or be a JSON object."
                )
            source, spoken = payload.split("=", 1)
            payload = {"source": source, "spoken": spoken}
    if not isinstance(payload, dict):
        raise ReplacementRuleError(f"Replacement rule at {location} must be an object.")

    allowed = {"source", "spoken", "match", "scope", "case_sensitive"}
    unknown = set(payload) - allowed
    if unknown:
        raise ReplacementRuleError(
            f"Replacement rule at {location} has unknown field(s): {', '.join(sorted(unknown))}."
        )
    try:
        source = payload["source"]
        spoken = payload["spoken"]
    except KeyError as exc:
        raise ReplacementRuleError(f"Replacement rule at {location} requires source and spoken fields.") from exc
    if not isinstance(source, str) or not isinstance(spoken, str):
        raise ReplacementRuleError(f"Replacement rule at {location} source and spoken values must be strings.")
    case_sensitive = payload.get("case_sensitive", True)
    if not isinstance(case_sensitive, bool):
        raise ReplacementRuleError(f"Replacement rule at {location} case_sensitive must be true or false.")
    return ReplacementRule(
        source=source,
        spoken=spoken,
        match=str(payload.get("match", "whole-word")),
        scope=str(payload.get("scope", "body")),
        case_sensitive=case_sensitive,
    )


def _read_rule_file(path: str | Path) -> list[Any]:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ReplacementRuleError(f"Could not read replacement rules file {source}: {exc}") from exc
    if size > MAX_REPLACEMENT_FILE_BYTES:
        raise ReplacementRuleError(
            f"Replacement rules file exceeds the {MAX_REPLACEMENT_FILE_BYTES}-byte safety limit."
        )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplacementRuleError(f"Could not read replacement rules file {source}: {exc}") from exc

    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ReplacementRuleError("Replacement rules file root must be an object or list.")
    version = payload.get("version")
    if version != REPLACEMENT_RULE_SCHEMA_VERSION:
        raise ReplacementRuleError(
            f"Unsupported replacement rules version {version!r}; expected {REPLACEMENT_RULE_SCHEMA_VERSION}."
        )
    unknown = set(payload) - {"version", "replacements"}
    if unknown:
        raise ReplacementRuleError(
            f"Replacement rules file has unknown field(s): {', '.join(sorted(unknown))}."
        )
    rules = payload.get("replacements")
    if not isinstance(rules, list):
        raise ReplacementRuleError("Replacement rules file replacements field must be a list.")
    return rules


def load_replacement_rules(
    replacement_file: str | Path | None = None,
    inline_rules: Iterable[Any] = (),
) -> tuple[ReplacementRule, ...]:
    raw: list[Any] = []
    if replacement_file:
        raw.extend(_read_rule_file(replacement_file))
    raw.extend(inline_rules or ())
    if len(raw) > MAX_REPLACEMENT_RULES:
        raise ReplacementRuleError(f"At most {MAX_REPLACEMENT_RULES} replacement rules are allowed.")
    return tuple(_coerce_rule(item, location=f"rule {index + 1}") for index, item in enumerate(raw))


def _rule_pattern(rule: ReplacementRule) -> re.Pattern[str]:
    source = rule.source if rule.match == "regex" else re.escape(rule.source)
    if rule.match == "whole-word":
        source = rf"(?<!\w){source}(?!\w)"
    flags = 0 if rule.case_sensitive else re.IGNORECASE
    return re.compile(source, flags)


def apply_replacement_rules(
    text: str,
    rules: Iterable[ReplacementRule],
    *,
    scope: str = "body",
) -> ReplacementResult:
    """Apply leftmost, longest replacements against the original text exactly once."""
    if scope not in {"body", "title"}:
        raise ReplacementRuleError("Replacement application scope must be body or title.")

    matches: list[tuple[int, int, int, int, str]] = []
    for rule_index, rule in enumerate(rules):
        if rule.scope not in {scope, "all"}:
            continue
        for match in _rule_pattern(rule).finditer(text):
            if match.start() == match.end():
                raise ReplacementRuleError(
                    f"Replacement regular expression {rule.source!r} produced an empty match."
                )
            matches.append((match.start(), match.end(), -(match.end() - match.start()), rule_index, rule.spoken))

    matches.sort(key=lambda item: (item[0], item[2], item[3]))
    chunks: list[str] = []
    cursor = 0
    count = 0
    for start, end, _negative_length, _rule_index, spoken in matches:
        if start < cursor:
            continue
        chunks.append(text[cursor:start])
        chunks.append(spoken)
        cursor = end
        count += 1
    if count == 0:
        return ReplacementResult(text=text, count=0)
    chunks.append(text[cursor:])
    return ReplacementResult(text="".join(chunks), count=count)


def apply_rules_to_chapters(
    chapters: Iterable[tuple[str, str]],
    rules: Iterable[ReplacementRule],
) -> tuple[list[tuple[str, str]], int]:
    frozen_rules = tuple(rules)
    transformed: list[tuple[str, str]] = []
    total = 0
    for title, body in chapters:
        title_result = apply_replacement_rules(title, frozen_rules, scope="title")
        body_result = apply_replacement_rules(body, frozen_rules, scope="body")
        transformed.append((title_result.text, body_result.text))
        total += title_result.count + body_result.count
    return transformed, total


def replacement_rules_payload(rules: Iterable[ReplacementRule]) -> list[dict[str, Any]]:
    return [rule.to_dict() for rule in rules]


_TEXT_TOC_HEADINGS = frozenset(
    {
        "contents",
        "table of contents",
        "contenido",
        "contenidos",
        "conteudo",
        "cuprins",
        "indice",
        "indhold",
        "indholdsfortegnelse",
        "inhalt",
        "inhaltsverzeichnis",
        "innehall",
        "innehallsforteckning",
        "inhoud",
        "inhoudsopgave",
        "innhold",
        "innholdsfortegnelse",
        "oglavlenie",
        "spis tresci",
        "sisallys",
        "sisallysluettelo",
        "sommaire",
        "sommario",
        "soderzhanie",
        "sumario",
        "tabla de contenidos",
        "table des matieres",
        "tartalomjegyzek",
        "оглавление",
        "содержание",
        "目录",
        "目次",
        "目錄",
    }
)
_NUMBERED_TOC_LINE = re.compile(r"(?:\.{2,}|\s)\s*\d{1,5}\s*$")
_NAMED_TOC_LINE = re.compile(
    r"^(?:chapter|book|part|act|section)\s+(?:\d+|[ivxlcdm]+)\b",
    re.IGNORECASE,
)


def _fold_heading(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    ascii_like = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", " ", ascii_like).strip()


def high_confidence_plain_text_toc_indexes(paragraphs: Iterable[str]) -> frozenset[int]:
    """Identify a bounded, early plain-text TOC without deleting ambiguous lists."""
    values = list(paragraphs)
    for heading_index, paragraph in enumerate(values[:20]):
        heading_lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if len(heading_lines) != 1 or _fold_heading(heading_lines[0]) not in _TEXT_TOC_HEADINGS:
            continue

        entry_count = 0
        last_entry_index: int | None = None
        for index in range(heading_index + 1, min(len(values), heading_index + 13)):
            lines = [line.strip() for line in values[index].splitlines() if line.strip()]
            word_count = len(values[index].split())
            if word_count > 80 or (len(values[index]) > 500 and len(lines) <= 2):
                break
            matches = sum(
                bool(_NUMBERED_TOC_LINE.search(line) or _NAMED_TOC_LINE.search(line))
                for line in lines
            )
            if matches:
                entry_count += matches
                last_entry_index = index
            elif entry_count:
                break

        if entry_count >= 3 and last_entry_index is not None:
            return frozenset(range(heading_index, last_entry_index + 1))
    return frozenset()
