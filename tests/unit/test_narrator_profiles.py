from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.narrator import NarratorCatalog, NarratorProfileError


CATALOG_PATH = PROJECT_ROOT / "resources" / "narrators" / "default_profiles.json"


def test_bundled_catalog_has_stable_preset_and_experimental_design_profiles():
    catalog = NarratorCatalog.load(CATALOG_PATH)

    preset = catalog.get("preset-eric-neutral")
    designed = catalog.get("design-warm-narrator")

    assert preset.settings.voice_mode == "preset"
    assert preset.stability == "stable"
    assert designed.settings.voice_mode == "design"
    assert designed.stability == "experimental"
    assert designed.settings.instruct


def test_profile_overrides_are_validated_without_mutating_profile():
    profile = NarratorCatalog.load(CATALOG_PATH).get("preset-eric-neutral")
    original_hash = profile.sha256

    overridden = profile.with_overrides(speaker="Ryan", seed=42, top_p=0.7)

    assert overridden.speaker == "Ryan"
    assert overridden.seed == 42
    assert overridden.top_p == 0.7
    assert profile.settings.speaker == "Eric"
    assert profile.sha256 == original_hash


def test_invalid_profile_override_is_rejected():
    profile = NarratorCatalog.load(CATALOG_PATH).get("preset-eric-neutral")

    with pytest.raises(NarratorProfileError, match="Invalid overrides"):
        profile.with_overrides(top_p=2.0)


def test_unknown_profile_lists_available_ids():
    catalog = NarratorCatalog.load(CATALOG_PATH)

    with pytest.raises(NarratorProfileError, match="preset-eric-neutral"):
        catalog.get("missing")


def test_duplicate_profile_ids_are_rejected(tmp_path):
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["profiles"].append(payload["profiles"][0])
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(NarratorProfileError, match="unique"):
        NarratorCatalog.load(path)


def test_legacy_reference_voice_workflows_are_not_bundled():
    workflows = PROJECT_ROOT / "resources" / "workflows"

    assert not (workflows / "upload_voice.json").exists()
    assert not (workflows / "vibevoice_single_speaker.json").exists()
