from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.config import AppConfig  # noqa: E402
from core.version import AUTOAUDIO_VERSION  # noqa: E402


def _runtime_requirements() -> list[str]:
    return [
        line.strip()
        for line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_uses_runtime_version_as_single_source():
    project = _pyproject()

    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "core.version.AUTOAUDIO_VERSION"
    }
    assert AUTOAUDIO_VERSION == "2.0.0"


def test_pyproject_and_requirements_declare_the_same_direct_dependencies():
    assert set(_pyproject()["project"]["dependencies"]) == set(_runtime_requirements())


def test_pyproject_exposes_application_and_verifier_commands():
    scripts = _pyproject()["project"]["scripts"]

    assert scripts == {
        "autoaudio": "core.pipeline:main",
        "autoaudio-verify": "provenance.verify:main",
    }


def test_packaging_contract_includes_runtime_packages_resources_and_notices():
    project = _pyproject()
    setuptools = project["tool"]["setuptools"]

    assert set(setuptools["packages"]) == {
        "autoaudio_resources",
        "comfyui",
        "core",
        "gui",
        "metadata",
        "provenance",
    }
    assert setuptools["package-dir"]["autoaudio_resources"] == "resources"
    assert set(setuptools["package-data"]["autoaudio_resources"]) == {
        "narrators/*.json",
        "workflows/*.json",
    }
    assert set(project["project"]["license-files"]) == {
        "LICENSE",
        "LICENSES/*",
        "THIRD_PARTY_DEPENDENCIES.md",
    }


def test_source_checkout_app_config_resolves_bundled_resources():
    config = AppConfig(project_root=PROJECT_ROOT)

    assert config.narrator_profiles_path.is_file()
    assert config.workflow_path_for("preset").is_file()
    assert config.workflow_path_for("design").is_file()


def test_local_documentation_links_resolve():
    documents = [PROJECT_ROOT / "README.md", *(PROJECT_ROOT / "Docs").rglob("*.md")]
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    missing: list[str] = []
    for document in documents:
        for target in link_pattern.findall(document.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative_target = target.split("#", 1)[0]
            if relative_target and not (document.parent / relative_target).resolve().exists():
                missing.append(f"{document.relative_to(PROJECT_ROOT)} -> {target}")

    assert missing == []
