from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from core.errors import AudioStitchError
from core.metadata_adapters import MetadataContext, adapter_for_extension
from provenance.ai_marking import (
    WatermarkEvidence,
    ai_marking_metadata_args,
    validate_watermarked_artifact,
    write_ai_marking_manifest,
)


AUDIO_ASSEMBLY_POLICY_VERSION = "lossless-flac-masters-v1"
ASSEMBLY_SAMPLE_RATE = 24_000
ASSEMBLY_CHANNELS = 1


@dataclass(frozen=True)
class ChapterMarker:
    title: str
    start_ms: int
    end_ms: int


def sanitize_metadata_value(value: str | None) -> str | None:
    if not value:
        return None
    sanitized = re.sub(r"[\r\n]+", " ", str(value)).strip()
    return sanitized or None


def _escape_ffmetadata(value: str) -> str:
    return value.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")


def get_audio_duration_ms(file_path: str | Path) -> int:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        return int(float(result.stdout.strip()) * 1000)
    except Exception:
        return 0


def ensure_silence_file(output_path: str | Path, duration_ms: int) -> str:
    if duration_ms <= 0:
        raise ValueError("Silence duration must be positive.")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=channel_layout=mono:sample_rate={ASSEMBLY_SAMPLE_RATE}",
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-c:a",
        "flac",
        "-ar",
        str(ASSEMBLY_SAMPLE_RATE),
        "-ac",
        str(ASSEMBLY_CHANNELS),
        str(output),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        raise AudioStitchError(f"Could not create {duration_ms} ms silence asset: {exc}") from exc
    return str(output)


def interleave_audio_files(audio_files: Sequence[str], gap_file: str | None) -> list[str]:
    ordered: list[str] = []
    for index, file_path in enumerate(audio_files):
        if index and gap_file:
            ordered.append(gap_file)
        ordered.append(file_path)
    return ordered


def chapter_markers_for_files(
    chapter_files: Sequence[tuple[str, str]],
    *,
    gap_duration_ms: int,
) -> tuple[ChapterMarker, ...]:
    markers: list[ChapterMarker] = []
    current_ms = 0
    for index, (file_path, title) in enumerate(chapter_files):
        duration_ms = get_audio_duration_ms(file_path)
        if duration_ms <= 0:
            raise AudioStitchError(f"Could not determine a positive chapter duration for {file_path}.")
        markers.append(ChapterMarker(title=title, start_ms=current_ms, end_ms=current_ms + duration_ms))
        current_ms += duration_ms
        if index < len(chapter_files) - 1:
            current_ms += max(0, gap_duration_ms)
    return tuple(markers)


def _write_concat_list(audio_files: Sequence[str], output_dir: Path) -> Path:
    fd, temp_path = tempfile.mkstemp(prefix=".autoaudio-concat-", suffix=".txt", dir=output_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        for path in audio_files:
            absolute_path = str(Path(path).resolve())
            escaped_path = absolute_path.replace("'", "'\\''")
            file.write(f"file '{escaped_path}'\n")
    return Path(temp_path)


def _run_ffmpeg(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def assemble_lossless_master(
    audio_files: Sequence[str],
    output_path: str | Path,
    *,
    content_id: str,
    watermarked_source_files: Sequence[str],
    logger: logging.Logger,
    ai_provider: str = "ComfyUI",
) -> str:
    if not audio_files:
        raise AudioStitchError("Cannot assemble an empty audio sequence.")
    if not watermarked_source_files:
        raise AudioStitchError("Lossless assembly requires verified watermarked source artifacts.")
    missing = [str(path) for path in audio_files if not Path(path).is_file()]
    if missing:
        raise AudioStitchError(f"Assembly input is missing: {missing[0]}")

    try:
        validated_sources = tuple(validate_watermarked_artifact(path) for path in watermarked_source_files)
    except ValueError as exc:
        raise AudioStitchError(f"Assembly rejected unverified watermark source: {exc}") from exc
    evidence = tuple(
        leaf
        for source in validated_sources
        for leaf in (source.sources if source.sources else (source,))
    )

    output = Path(output_path)
    if output.suffix.lower() != ".flac":
        raise AudioStitchError("Lossless assembly masters must use the .flac extension.")
    output.parent.mkdir(parents=True, exist_ok=True)
    concat_path = _write_concat_list(audio_files, output.parent)

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-map",
        "0:a:0",
        "-c:a",
        "flac",
        "-ar",
        str(ASSEMBLY_SAMPLE_RATE),
        "-ac",
        str(ASSEMBLY_CHANNELS),
        *ai_marking_metadata_args(provider=ai_provider),
        str(output),
    ]

    try:
        logger.debug("Assembling %d lossless inputs into %s", len(audio_files), output)
        _run_ffmpeg(command)

        write_ai_marking_manifest(
            output,
            content_id=content_id,
            metadata_embedded=True,
            watermark_applied=True,
            watermark_verified=True,
            watermark_scope="source-artifacts",
            watermark_detail="All marked source artifacts were hash-checked and verified before lossless assembly.",
            source_artifacts=evidence,
            provider=ai_provider,
        )
        return str(output)
    except Exception as exc:
        raise AudioStitchError(f"Error assembling lossless audio master: {exc}") from exc
    finally:
        concat_path.unlink(missing_ok=True)


def _write_ffmetadata(
    output_dir: Path,
    metadata: dict[str, str | None],
    chapter_markers: Sequence[ChapterMarker],
) -> Path:
    fd, temp_path = tempfile.mkstemp(prefix=".autoaudio-metadata-", suffix=".ffmeta", dir=output_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(";FFMETADATA1\n")
        for key, value in metadata.items():
            sanitized = sanitize_metadata_value(value)
            if sanitized:
                file.write(f"{key}={_escape_ffmetadata(sanitized)}\n")
        for index, marker in enumerate(chapter_markers):
            title = sanitize_metadata_value(marker.title) or f"Chapter {index + 1}"
            file.write("\n[CHAPTER]\n")
            file.write("TIMEBASE=1/1000\n")
            file.write(f"START={marker.start_ms}\n")
            file.write(f"END={marker.end_ms}\n")
            file.write(f"title={_escape_ffmetadata(title)}\n")
    return Path(temp_path)


def _is_valid_cover_image(cover_image: str) -> bool:
    if not Path(cover_image).is_file():
        return False
    try:
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                cover_image,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        return True
    except Exception:
        return False


def _encoding_command(
    master_path: str,
    output_filename: str,
    *,
    metadata: dict[str, str | None],
    metadata_path: Path | None,
    cover_image: str | None,
    include_cover: bool,
    ai_provider: str,
) -> list[str]:
    adapter = adapter_for_extension(output_filename)
    context = MetadataContext(
        title=sanitize_metadata_value(metadata.get("title")),
        artist=sanitize_metadata_value(metadata.get("artist")),
        album=sanitize_metadata_value(metadata.get("album")),
        track=sanitize_metadata_value(metadata.get("track")),
        disc=sanitize_metadata_value(metadata.get("disc")),
    )
    command = ["ffmpeg", "-y", "-i", master_path]
    next_input = 1
    metadata_input: int | None = None
    if metadata_path is not None:
        command.extend(["-i", str(metadata_path)])
        metadata_input = next_input
        next_input += 1
    if include_cover and cover_image:
        command.extend(["-i", cover_image])
        cover_input = next_input

    # Mapping options belong to the output. Keep them after every input so a
    # later cover input is not parsed as the target of an output-only option.
    if metadata_input is not None:
        command.extend(["-map_metadata", str(metadata_input), "-map_chapters", str(metadata_input)])
    if include_cover and cover_image:
        command.extend(
            [
                "-map",
                "0:a:0",
                "-map",
                f"{cover_input}:v:0",
                "-c:v",
                "copy",
                "-disposition:v:0",
                "attached_pic",
            ]
        )
    else:
        command.extend(["-map", "0:a:0"])

    command.extend(adapter.ffmpeg_metadata_args(context))
    command.extend(ai_marking_metadata_args(provider=ai_provider))
    if Path(output_filename).suffix.lower() == ".flac":
        command.extend(["-c:a", "copy"])
    else:
        command.extend(adapter.ffmpeg_output_args())
    command.append(output_filename)
    return command


def encode_lossless_master(
    master_path: str | Path,
    output_filename: str | Path,
    *,
    metadata: dict[str, str | None] | None = None,
    chapter_markers: Sequence[ChapterMarker] = (),
    cover_image: str | None = None,
    logger: logging.Logger,
    ai_provider: str = "ComfyUI",
) -> str:
    master = str(master_path)
    output = str(output_filename)
    try:
        evidence: WatermarkEvidence = validate_watermarked_artifact(master)
    except ValueError as exc:
        raise AudioStitchError(f"Encoding rejected invalid lossless master: {exc}") from exc

    output_parent = Path(output).parent
    output_parent.mkdir(parents=True, exist_ok=True)
    metadata_values = metadata or {}
    metadata_path = _write_ffmetadata(output_parent, metadata_values, chapter_markers) if chapter_markers else None
    include_cover = bool(cover_image and _is_valid_cover_image(cover_image))

    try:
        command = _encoding_command(
            master,
            output,
            metadata=metadata_values,
            metadata_path=metadata_path,
            cover_image=cover_image,
            include_cover=include_cover,
            ai_provider=ai_provider,
        )
        try:
            _run_ffmpeg(command)
        except subprocess.CalledProcessError:
            if not include_cover:
                raise
            logger.warning("Encoding failed with attached cover; retrying without cover art for %s", output)
            _run_ffmpeg(
                _encoding_command(
                    master,
                    output,
                    metadata=metadata_values,
                    metadata_path=metadata_path,
                    cover_image=None,
                    include_cover=False,
                    ai_provider=ai_provider,
                )
            )

        content_id = metadata_values.get("title") or Path(output).name
        write_ai_marking_manifest(
            output,
            content_id=str(content_id),
            metadata_embedded=True,
            watermark_applied=True,
            watermark_verified=True,
            watermark_scope="source-artifacts",
            watermark_detail="Verified source watermarks were preserved through final container encoding.",
            source_artifacts=evidence.sources if evidence.sources else (evidence,),
            provider=ai_provider,
        )
        return output
    except Exception as exc:
        raise AudioStitchError(f"Error encoding assembled audio: {exc}") from exc
    finally:
        if metadata_path is not None:
            metadata_path.unlink(missing_ok=True)
