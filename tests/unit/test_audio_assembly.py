from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.audio_assembly import (
    ChapterMarker,
    assemble_lossless_master,
    chapter_markers_for_files,
    encode_lossless_master,
    ensure_silence_file,
    get_audio_duration_ms,
    interleave_audio_files,
)
from core.errors import AudioStitchError
from provenance.ai_marking import (
    cleanup_orphan_manifests,
    manifest_path_for,
    refresh_ai_marking_manifest_hash,
    validate_watermarked_artifact,
    write_ai_marking_manifest,
)
from provenance.verify import verify_artifact


LOGGER = logging.getLogger("test.audio_assembly")


def _make_marked_tone(path: Path, *, duration_ms: int = 240, frequency: int = 440) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=24000:duration={duration_ms / 1000}",
            "-ac",
            "1",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    write_ai_marking_manifest(
        path,
        content_id=path.stem,
        metadata_embedded=True,
        watermark_applied=True,
        watermark_verified=True,
        watermark_detail="test evidence",
    )
    return path


def _make_cover(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x32:d=0.04",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path


def test_lossless_assembly_streams_files_and_preserves_explicit_silence(tmp_path):
    first = _make_marked_tone(tmp_path / "first.flac", frequency=330)
    second = _make_marked_tone(tmp_path / "second.flac", frequency=550)
    gap = Path(ensure_silence_file(tmp_path / "gap.flac", 120))
    master = tmp_path / "chapter-master.flac"

    ordered = interleave_audio_files([str(first), str(second)], str(gap))
    assemble_lossless_master(
        ordered,
        master,
        content_id="chapter-master",
        watermarked_source_files=[str(first), str(second)],
        logger=LOGGER,
    )

    assert master.exists()
    assert 560 <= get_audio_duration_ms(master) <= 640
    manifest = json.loads(manifest_path_for(master).read_text(encoding="utf-8"))
    assert manifest["marking_methods"]["audio_watermark"]["scope"] == "source-artifacts"
    assert len(manifest["source_artifacts"]) == 2
    evidence = validate_watermarked_artifact(master)
    assert evidence.scope == "source-artifacts"
    assert len(evidence.sources) == 2


def test_assembly_rejects_tampered_watermark_source_before_ffmpeg(tmp_path):
    source = _make_marked_tone(tmp_path / "source.flac")
    source.write_bytes(source.read_bytes() + b"tampered")

    with patch("core.audio_assembly._run_ffmpeg") as ffmpeg_mock, pytest.raises(
        AudioStitchError, match="unverified watermark source"
    ):
        assemble_lossless_master(
            [str(source)],
            tmp_path / "master.flac",
            content_id="master",
            watermarked_source_files=[str(source)],
            logger=LOGGER,
        )

    ffmpeg_mock.assert_not_called()


def test_lossless_assembly_command_never_routes_full_audio_through_python_pipes(tmp_path):
    source = _make_marked_tone(tmp_path / "source.flac")
    output = tmp_path / "master.flac"
    commands: list[list[str]] = []

    def fake_ffmpeg(command: list[str]) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"lossless-master")

    with patch("core.audio_assembly._run_ffmpeg", side_effect=fake_ffmpeg):
        assemble_lossless_master(
            [str(source)],
            output,
            content_id="master",
            watermarked_source_files=[str(source)],
            logger=LOGGER,
        )

    assert len(commands) == 1
    assert ["-c:a", "flac"] == commands[0][commands[0].index("-c:a") : commands[0].index("-c:a") + 2]
    assert not any(str(argument).startswith("pipe:") for argument in commands[0])


def test_final_encoding_reads_a_lossless_master_without_python_audio_pipes(tmp_path):
    master = _make_marked_tone(tmp_path / "master.flac")
    output = tmp_path / "chapter.mp3"
    commands: list[list[str]] = []

    def fake_ffmpeg(command: list[str]) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"encoded")

    with patch("core.audio_assembly._run_ffmpeg", side_effect=fake_ffmpeg):
        encode_lossless_master(
            master,
            output,
            metadata={"title": "Chapter 1"},
            logger=LOGGER,
        )

    assert len(commands) == 1
    assert commands[0][:4] == ["ffmpeg", "-y", "-i", str(master)]
    assert "libmp3lame" in commands[0]
    assert not any(str(argument).startswith("pipe:") for argument in commands[0])


@pytest.mark.parametrize(
    ("extension", "expected_codec"),
    [("flac", "flac"), ("mp3", "mp3"), ("m4b", "aac")],
)
def test_real_final_encoding_supports_each_output_format(tmp_path, extension, expected_codec):
    master = _make_marked_tone(tmp_path / "master.flac", duration_ms=400)
    output = tmp_path / f"chapter.{extension}"

    encode_lossless_master(
        master,
        output,
        metadata={"title": "Chapter 1", "artist": "Narrator"},
        chapter_markers=(ChapterMarker(title="Chapter 1", start_ms=0, end_ms=400),),
        logger=LOGGER,
    )

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(output),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert probe.stdout.strip() == expected_codec
    assert get_audio_duration_ms(output) >= 350
    assert manifest_path_for(output).exists()


@pytest.mark.parametrize("extension", ["flac", "mp3", "m4b"])
def test_real_stitched_encoding_keeps_cover_with_chapter_metadata(tmp_path, extension):
    master = _make_marked_tone(tmp_path / "part-master.flac", duration_ms=400)
    cover = _make_cover(tmp_path / "cover.jpg")
    output = tmp_path / f"part.{extension}"

    encode_lossless_master(
        master,
        output,
        metadata={"title": "Part 1", "artist": "Narrator"},
        chapter_markers=(ChapterMarker(title="Chapter 1", start_ms=0, end_ms=400),),
        cover_image=str(cover),
        logger=LOGGER,
    )

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name:stream_disposition=attached_pic",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "mjpeg"
    assert stream["disposition"]["attached_pic"] == 1
    assert manifest_path_for(output).exists()
    verified, errors = verify_artifact(output)
    assert verified, errors


def test_final_encoding_retries_without_invalid_cover_mapping(tmp_path):
    master = _make_marked_tone(tmp_path / "master.flac")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover")
    output = tmp_path / "chapter.flac"
    commands: list[list[str]] = []

    def fake_ffmpeg(command: list[str]) -> None:
        commands.append(command)
        if len(commands) == 1:
            raise subprocess.CalledProcessError(1, command)
        Path(command[-1]).write_bytes(b"encoded")

    with patch("core.audio_assembly._is_valid_cover_image", return_value=True), patch(
        "core.audio_assembly._run_ffmpeg", side_effect=fake_ffmpeg
    ):
        encode_lossless_master(master, output, cover_image=str(cover), logger=LOGGER)

    assert len(commands) == 2
    assert "attached_pic" in commands[0]
    assert "attached_pic" not in commands[1]
    assert "-c:a" in commands[1] and "copy" in commands[1]


def test_chapter_markers_include_configured_interchapter_gap(tmp_path):
    first = _make_marked_tone(tmp_path / "first.flac", duration_ms=200)
    second = _make_marked_tone(tmp_path / "second.flac", duration_ms=300)

    markers = chapter_markers_for_files(
        [(str(first), "One"), (str(second), "Two")],
        gap_duration_ms=900,
    )

    assert markers[0].start_ms == 0
    assert 190 <= markers[0].end_ms <= 210
    assert markers[1].start_ms == markers[0].end_ms + 900
    assert markers[1].end_ms > markers[1].start_ms


def test_orphan_marking_manifests_are_removed_without_touching_live_artifacts(tmp_path):
    live = _make_marked_tone(tmp_path / "live.flac")
    orphan = tmp_path / "deleted.flac.ai.json"
    orphan.write_text("{}", encoding="utf-8")

    assert cleanup_orphan_manifests(tmp_path) == 1
    assert not orphan.exists()
    assert live.exists()
    assert manifest_path_for(live).exists()


def test_marking_manifest_hash_can_be_refreshed_after_final_container_mutation(tmp_path):
    artifact = _make_marked_tone(tmp_path / "signed-later.flac")
    artifact.write_bytes(artifact.read_bytes() + b"c2pa-container-mutation")

    with pytest.raises(ValueError, match="hash does not match"):
        validate_watermarked_artifact(artifact)

    refresh_ai_marking_manifest_hash(artifact)

    evidence = validate_watermarked_artifact(artifact)
    payload = json.loads(manifest_path_for(artifact).read_text(encoding="utf-8"))
    assert evidence.sha256 == payload["artifact_sha256"]
    assert payload["artifact_hash_scope"] == "entire-artifact-bytes"
