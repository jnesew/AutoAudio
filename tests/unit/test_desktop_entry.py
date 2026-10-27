from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

from gui import desktop


@pytest.fixture
def launcher_environment(tmp_path, monkeypatch):
    environment = tmp_path / "Python environment" / "bin"
    environment.mkdir(parents=True)
    command = environment / "autoaudio"
    command.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(desktop.sys, "executable", str(environment / "python"))
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    return command


def test_launcher_uses_active_environment_and_packaged_icon(
    tmp_path, launcher_environment
):
    data_home = tmp_path / "data"
    path = desktop.install_desktop_entry(data_home)
    assert path == data_home / "applications" / f"{desktop.DESKTOP_FILE_ID}.desktop"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    entry = parser["Desktop Entry"]
    assert entry["Exec"] == f'"{launcher_environment}" --gui'
    assert Path(entry["Icon"]).is_file()
    assert entry["Name"] == "AutoAudio"
    assert entry["Terminal"] == "false"
    assert desktop.install_desktop_entry(data_home) == path
    if validator := shutil.which("desktop-file-validate"):
        subprocess.run(
            [validator, str(path)], check=True, capture_output=True, text=True
        )


def test_launcher_respects_xdg_data_location(
    tmp_path, launcher_environment, monkeypatch
):
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    path = desktop.install_desktop_entry()
    assert path.parent == data_home / "applications"


def test_missing_install_does_not_write_launcher(tmp_path, launcher_environment):
    launcher_environment.unlink()
    data_home = tmp_path / "data"
    with pytest.raises(RuntimeError, match="Install AutoAudio"):
        desktop.install_desktop_entry(data_home)
    assert not data_home.exists()
