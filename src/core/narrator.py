from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.config import GenerationSettings


NARRATOR_CATALOG_SCHEMA_VERSION = 1
_PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class NarratorProfileError(ValueError):
    """Raised when narrator profile data is missing, invalid, or ambiguous."""


def _canonical_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class NarratorProfile:
    id: str
    name: str
    description: str
    stability: str
    settings: GenerationSettings

    def __post_init__(self) -> None:
        if not _PROFILE_ID_PATTERN.fullmatch(self.id):
            raise NarratorProfileError(
                "Narrator profile id must use lowercase letters, numbers, hyphens, or underscores."
            )
        if not self.name.strip():
            raise NarratorProfileError("Narrator profile name cannot be empty.")
        if self.stability not in {"stable", "experimental"}:
            raise NarratorProfileError("Narrator profile stability must be 'stable' or 'experimental'.")
        if self.settings.voice_mode == "preset" and self.stability != "stable":
            raise NarratorProfileError("Preset narrator profiles must be marked stable.")
        if self.settings.voice_mode == "design" and self.stability != "experimental":
            raise NarratorProfileError("VoiceDesign narrator profiles must be marked experimental.")

    @property
    def sha256(self) -> str:
        return _canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "stability": self.stability,
            "settings": asdict(self.settings),
        }

    def with_overrides(self, **overrides: Any) -> GenerationSettings:
        values = asdict(self.settings)
        values.update({key: value for key, value in overrides.items() if value is not None})
        try:
            return GenerationSettings(**values)
        except (TypeError, ValueError) as exc:
            raise NarratorProfileError(f"Invalid overrides for narrator profile {self.id!r}: {exc}") from exc

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NarratorProfile":
        try:
            raw_settings = payload["settings"]
            if not isinstance(raw_settings, dict):
                raise TypeError("settings must be an object")
            settings = GenerationSettings(**raw_settings)
            return cls(
                id=str(payload["id"]),
                name=str(payload["name"]),
                description=str(payload.get("description", "")),
                stability=str(payload["stability"]),
                settings=settings,
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, NarratorProfileError):
                raise
            raise NarratorProfileError("Narrator catalog contains an invalid profile.") from exc


@dataclass(frozen=True)
class NarratorCatalog:
    profiles: tuple[NarratorProfile, ...]
    schema_version: int = NARRATOR_CATALOG_SCHEMA_VERSION

    @classmethod
    def load(cls, path: str | Path) -> "NarratorCatalog":
        try:
            with open(path, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise NarratorProfileError(f"Could not read narrator catalog at {path}: {exc}") from exc

        if not isinstance(payload, dict):
            raise NarratorProfileError("Narrator catalog root must be a JSON object.")
        version = payload.get("schema_version")
        if version != NARRATOR_CATALOG_SCHEMA_VERSION:
            raise NarratorProfileError(
                f"Unsupported narrator catalog schema {version!r}; expected {NARRATOR_CATALOG_SCHEMA_VERSION}."
            )
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            raise NarratorProfileError("Narrator catalog must contain at least one profile.")
        profiles = tuple(NarratorProfile.from_dict(item) for item in raw_profiles)
        ids = [profile.id for profile in profiles]
        if len(ids) != len(set(ids)):
            raise NarratorProfileError("Narrator profile ids must be unique.")
        return cls(schema_version=version, profiles=profiles)

    def get(self, profile_id: str) -> NarratorProfile:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        available = ", ".join(profile.id for profile in self.profiles)
        raise NarratorProfileError(f"Unknown narrator profile {profile_id!r}. Available profiles: {available}")
