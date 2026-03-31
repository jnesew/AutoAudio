from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

from comfyui.client import (
    ComfyUIClient,
    ComfyUIClientError,
    ComfyUIConnectionError as ClientComfyUIConnectionError,
    ComfyUIProtocolError as ClientComfyUIProtocolError,
)
from comfyui.real_client import RealComfyUIClient
from comfyui.spoof_client import SpoofComfyUIClient
from comfyui.workflow_loader import load_workflow_template
from core.checkpoint import (
    CheckpointStore,
    create_initial_checkpoint,
    sha256_file,
    stable_settings_hash,
    validate_artifact,
)
from core.config import AppConfig, GenerationSettings
from core.errors import (
    AudioStitchError,
    ComfyUIConnectionError,
    ComfyUIProtocolError,
    InputValidationError,
    MetadataExtractionError,
    PipelineRuntimeError,
    ResumeStateError,
)
from core.logging_utils import configure_run_logger
from core.metadata_adapters import MetadataContext, adapter_for_extension
from metadata.extractors import extract_epub_metadata, extract_text_fallback_metadata
from metadata.source_mode import detect_source_mode
from metadata.gutenberg import fetch_gutenberg_metadata
from metadata.id_utils import guess_gutenberg_id
from metadata.models import BookMetadata, MetadataSources, merge_metadata
from provenance.audio_watermark import watermark_audio_bytes_best_effort
from provenance.c2pa import ProvenanceConfig, ProvenanceRuntimeMetadata, apply_c2pa_with_policy, parse_model_identity_version


def _sanitize_ffmpeg_metadata_value(value: str | None) -> str | None:
    if not value:
        return None
    sanitized = re.sub(r"[\r\n]+", " ", value).strip()
    return sanitized or None


def _ai_marking_metadata_args() -> list[str]:
    return [
        "-metadata",
        "ai_generated=true",
        "-metadata",
        "ai_system=AutoAudio",
        "-metadata",
        "ai_provider=ComfyUI",
        "-metadata",
        "ai_marking=audio_watermark+metadata+manifest",
    ]


def _write_ai_marking_manifest(
    artifact_path: str,
    *,
    content_id: str,
    metadata_embedded: bool,
    watermark_applied: bool,
    watermark_verified: bool,
    watermark_detail: str,
) -> None:
    artifact = Path(artifact_path)
    payload = {
        "schema": "autoaudio.ai_marking.v1",
        "artifact": artifact.name,
        "artifact_sha256": sha256_file(str(artifact)) if artifact.exists() else "",
        "ai_generated": True,
        "ai_system": "AutoAudio",
        "provider": "ComfyUI",
        "content_id": content_id,
        "marking_methods": {
            "metadata": metadata_embedded,
            "audio_watermark": {
                "applied": watermark_applied,
                "verified": watermark_verified,
                "detail": watermark_detail,
            },
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = artifact.with_suffix(f"{artifact.suffix}.ai.json")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_valid_cover_image(cover_image: str) -> bool:
    if not os.path.exists(cover_image):
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
        logging.getLogger("autoaudio.run").warning("Invalid cover image detected; skipping attached cover: %s", cover_image)
        return False


def extract_text_blocks_from_epub(epub_path: str) -> list[tuple[str, str]]:
    if not os.path.exists(epub_path):
        print(f"ERROR: File not found: {epub_path}")
        return []

    book = epub.read_epub(epub_path)
    blocks: list[tuple[str, str]] = []

    for spine_item in book.spine:
        item_id = spine_item[0] if isinstance(spine_item, tuple) else spine_item
        item = book.get_item_with_id(item_id)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        soup = BeautifulSoup(item.get_body_content(), "html.parser")
        for script in soup(["script", "style"]):
            script.extract()

        text = soup.get_text(separator="\n")
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        clean_text = " ".join(chunk for chunk in chunks if chunk)

        title_tag = soup.find("h1") or soup.find("h2") or soup.find("title")
        title = title_tag.get_text().strip() if title_tag else item.get_id()

        if len(clean_text) > 50:
            blocks.append((title, clean_text))

    return blocks


def extract_text_blocks_from_text_file(text_path: str) -> list[tuple[str, str]]:
    if not os.path.exists(text_path):
        print(f"ERROR: File not found: {text_path}")
        return []

    with open(text_path, "r", encoding="utf-8", errors="ignore") as file:
        raw = file.read()

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n+", raw)
        if paragraph.strip()
    ]

    blocks: list[tuple[str, str]] = []
    for i, paragraph in enumerate(paragraphs):
        if len(paragraph) > 20:
            blocks.append((f"Paragraph {i + 1}", paragraph))

    return blocks


def group_blocks_into_chapters(blocks: list[tuple[str, str]], pages_per_chapter: int) -> list[tuple[str, str]]:
    if pages_per_chapter < 1:
        pages_per_chapter = 1

    chapters: list[tuple[str, str]] = []
    for i in range(0, len(blocks), pages_per_chapter):
        batch = blocks[i : i + pages_per_chapter]
        if not batch:
            continue

        first_title = batch[0][0]
        combined_text = " ".join(text for _, text in batch).strip()
        chapter_num = len(chapters) + 1
        chapter_title = f"Chapter {chapter_num}: {first_title}"
        chapters.append((chapter_title, combined_text))

    return chapters


def group_paragraphs_into_chapters(
    blocks: list[tuple[str, str]], target_words_per_chapter: int = 2500, min_paragraphs_per_chapter: int = 3
) -> list[tuple[str, str]]:
    if target_words_per_chapter < 1:
        target_words_per_chapter = 2500
    if min_paragraphs_per_chapter < 1:
        min_paragraphs_per_chapter = 1

    chapters: list[tuple[str, str]] = []
    current: list[tuple[str, str]] = []
    current_words = 0

    for title, text in blocks:
        words = len(text.split())

        should_cut = current and current_words >= target_words_per_chapter and len(current) >= min_paragraphs_per_chapter
        if should_cut:
            chapter_num = len(chapters) + 1
            combined = " ".join(t for _, t in current).strip()
            chapters.append((f"Chapter {chapter_num}", combined))
            current = []
            current_words = 0

        current.append((title, text))
        current_words += words

    if current:
        chapter_num = len(chapters) + 1
        combined = " ".join(t for _, t in current).strip()
        chapters.append((f"Chapter {chapter_num}", combined))

    return chapters


def split_text_smart(text: str, max_words: int = 250) -> list[str]:
    sentences = re.split(r"(?<=[.!?]) +", text)
    chunks: list[str] = []
    current_chunk_str = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        word_count = len(sentence.split())
        if len(current_chunk_str.split()) + word_count <= max_words:
            current_chunk_str += f" {sentence}"
        else:
            if current_chunk_str.strip():
                chunks.append(current_chunk_str.strip())
            current_chunk_str = sentence

    if current_chunk_str.strip():
        chunks.append(current_chunk_str.strip())

    return chunks


def get_audio_duration_ms(file_path: str) -> int:
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
                file_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        return int(float(result.stdout.strip()) * 1000)
    except Exception as exc:
        logging.getLogger("autoaudio.run").warning("Could not get duration for %s (%s)", file_path, exc)
        return 0


def process_segment(
    *,
    text_segment: str,
    workflow_template: dict,
    settings: GenerationSettings,
    config: AppConfig,
    comfyui_client: ComfyUIClient,
) -> tuple[bytes | None, str | None]:
 
    final_text_segment = "This audio was generated synthetically with AutoAudio. [pause] " + text_segment

    try:
        artifact = comfyui_client.generate_audio(
            workflow_template=workflow_template,
            text_segment=final_text_segment,
            reference_voice=config.default_voice_filename,
            settings=settings,
            timeout_seconds=config.comfyui_timeout_seconds,
        )
        return artifact.content, artifact.extension
    except ClientComfyUIConnectionError as exc:
        raise ComfyUIConnectionError(str(exc)) from exc
    except ClientComfyUIProtocolError as exc:
        raise ComfyUIProtocolError(str(exc)) from exc
    except ComfyUIClientError as exc:
        raise PipelineRuntimeError(f"ComfyUI request failed: {exc}") from exc


def build_comfyui_client(config: AppConfig) -> ComfyUIClient:
    if config.comfyui_mode == "spoof":
        return SpoofComfyUIClient(scenario=config.comfyui_spoof_scenario)
    return RealComfyUIClient(server_address=config.comfyui_server_address)


def _extract_provenance_runtime_metadata(workflow_template: dict) -> ProvenanceRuntimeMetadata:
    backend_name = "unknown-backend"
    backend_version = "unknown"
    model_identity = ""
    model_version = ""

    for node in workflow_template.values():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs", {})
        if not backend_name or backend_name == "unknown-backend":
            if class_type:
                backend_name = str(class_type)
        if isinstance(inputs, dict) and not model_identity:
            model_value = inputs.get("model")
            if isinstance(model_value, str) and model_value.strip():
                model_identity, model_version = parse_model_identity_version(model_value.strip())
        meta = node.get("_meta", {})
        title = meta.get("title") if isinstance(meta, dict) else None
        if isinstance(title, str) and title.strip() and backend_version == "unknown":
            backend_version = title.strip()

    software_version = os.environ.get("AUTOAUDIO_VERSION", "dev")
    return ProvenanceRuntimeMetadata(
        model_name=model_identity or "unknown-model",
        model_version=model_version or "unknown",
        backend_name=backend_name,
        backend_version=backend_version,
        software_name="AutoAudio",
        software_version=software_version,
    )


def combine_audio_files(audio_files, output_filename, metadata=None, chapter_titles=None, cover_image=None):
    valid_files = [path for path in audio_files if os.path.exists(path)]
    if not valid_files:
        return False

    adapter = adapter_for_extension(output_filename)
    context = MetadataContext(
        title=_sanitize_ffmpeg_metadata_value((metadata or {}).get("title")),
        artist=_sanitize_ffmpeg_metadata_value((metadata or {}).get("artist")),
        album=_sanitize_ffmpeg_metadata_value((metadata or {}).get("album")),
        track=_sanitize_ffmpeg_metadata_value((metadata or {}).get("track")),
        disc=_sanitize_ffmpeg_metadata_value((metadata or {}).get("disc")),
    )

    list_file = output_filename + ".concat.txt"
    meta_file = output_filename + ".ffmeta"

    try:
        with open(list_file, "w", encoding="utf-8") as file:
            for path in valid_files:
                escaped_path = path.replace("'", "'\\''")
                file.write(f"file '{escaped_path}'\n")

        extract_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"]
        extract_proc = subprocess.run(extract_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        concat_bytes = extract_proc.stdout

        content_id = (metadata or {}).get("title") or Path(output_filename).name
        watermark_result, marked_audio_data = watermark_audio_bytes_best_effort(
            concat_bytes,
            content_id=content_id,
            logger=logging.getLogger("autoaudio.run"),
        )
        if not watermark_result.applied or not watermark_result.verified:
            raise RuntimeError(f"AI marking failed strict checks: applied={watermark_result.applied}, verified={watermark_result.verified}")

        cmd = ["ffmpeg", "-y", "-f", "wav", "-i", "pipe:0"]
        input_idx = 1

        if chapter_titles and len(chapter_titles) == len(valid_files):
            with open(meta_file, "w", encoding="utf-8") as file:
                file.write(";FFMETADATA1\n")
                if metadata:
                    for key, value in metadata.items():
                        sanitized_value = _sanitize_ffmpeg_metadata_value(value)
                        if sanitized_value:
                            file.write(f"{key}={sanitized_value}\n")

                current_ms = 0
                for i, path in enumerate(valid_files):
                    duration_ms = get_audio_duration_ms(path)
                    end_ms = current_ms + duration_ms
                    chapter_title = _sanitize_ffmpeg_metadata_value(chapter_titles[i]) or f"Chapter {i + 1}"
                    file.write(f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={current_ms}\nEND={end_ms}\ntitle={chapter_title}\n")
                    current_ms = end_ms

            cmd.extend(["-i", meta_file, "-map_metadata", str(input_idx)])
            input_idx += 1

        cmd.extend(adapter.ffmpeg_metadata_args(context))
        cmd.extend(_ai_marking_metadata_args())

        include_cover = bool(cover_image and _is_valid_cover_image(cover_image))
        if include_cover:
            cmd.extend(["-i", cover_image])
            cmd.extend(["-map", "0:a", "-map", f"{input_idx}:v"])
            cmd.extend(["-disposition:v", "attached_pic"])

        cmd.extend(adapter.ffmpeg_output_args())
        cmd.append(output_filename)

        try:
            subprocess.run(cmd, input=marked_audio_data, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            if include_cover:
                logging.getLogger("autoaudio.run").warning(
                    "Stitching failed with attached cover; retrying without cover art for %s", output_filename
                )
                cmd_no_cover = ["ffmpeg", "-y", "-f", "wav", "-i", "pipe:0"]
                input_idx_no_cover = 1
                if chapter_titles and len(chapter_titles) == len(valid_files):
                    cmd_no_cover.extend(["-i", meta_file, "-map_metadata", str(input_idx_no_cover)])
                cmd_no_cover.extend(adapter.ffmpeg_metadata_args(context))
                cmd_no_cover.extend(_ai_marking_metadata_args())
                cmd_no_cover.extend(adapter.ffmpeg_output_args())
                cmd_no_cover.append(output_filename)
                subprocess.run(cmd_no_cover, input=marked_audio_data, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                raise

        _write_ai_marking_manifest(
            output_filename,
            content_id=content_id,
            metadata_embedded=True,
            watermark_applied=watermark_result.applied,
            watermark_verified=watermark_result.verified,
            watermark_detail=watermark_result.detail,
        )
        return True
    except Exception as exc:
        raise AudioStitchError(f"Error during stitching: {exc}") from exc
    finally:
        try:
            if os.path.exists(list_file):
                os.remove(list_file)
            if os.path.exists(meta_file):
                os.remove(meta_file)
        except Exception:
            pass


def safe_name(text: str) -> str:
    return "".join(c for c in text if c.isalpha() or c.isdigit() or c in (" ", "_", "-")).rstrip()


def extract_cover_art(epub_path: str, output_dir: str):
    try:
        book = epub.read_epub(epub_path)
        cover_item = None

        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_COVER:
                cover_item = item
                break

        if not cover_item:
            for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
                if "cover" in item.get_name().lower():
                    cover_item = item
                    break

        if cover_item:
            ext = os.path.splitext(cover_item.get_name())[1] or ".jpg"
            cover_path = os.path.join(output_dir, f"cover{ext}")
            with open(cover_path, "wb") as file:
                file.write(cover_item.get_content())
            print(f"   [Cover Art] Extracted: {cover_path}")
            return cover_path

    except Exception as exc:
        logging.getLogger("autoaudio.run").warning("Cover art extraction failed: %s", exc)

    return None


def resolve_metadata(args: argparse.Namespace, input_book: str, source_mode: str, output_dir: str) -> BookMetadata:
    fallback = BookMetadata(title=os.path.splitext(os.path.basename(input_book))[0], author="Unknown")

    if source_mode == "epub":
        try:
            embedded = extract_epub_metadata(input_book)
        except Exception as exc:
            raise MetadataExtractionError(f"Embedded EPUB metadata extraction failed for {input_book}: {exc}") from exc
        cover = extract_cover_art(input_book, output_dir)
        if cover:
            embedded = BookMetadata(**{**embedded.__dict__, "cover_image_path": cover})
    else:
        embedded = extract_text_fallback_metadata(input_book)

    fetched = BookMetadata()
    if args.fetch_metadata:
        gutenberg_id = (
            guess_gutenberg_id(args.gutenberg_id)
            or guess_gutenberg_id(embedded.identifier)
            or guess_gutenberg_id(os.path.basename(input_book))
        )
        if gutenberg_id:
            try:
                fetched = fetch_gutenberg_metadata(gutenberg_id)
            except Exception as exc:
                raise MetadataExtractionError(f"Online metadata fetch failed for Gutenberg ID {gutenberg_id}: {exc}") from exc
            print(f"   [Metadata] Fetched metadata for Gutenberg ID {gutenberg_id}")
        else:
            print("   [Metadata] Fetch requested but no Gutenberg ID could be inferred.")

    user = BookMetadata(title=args.title, author=args.author)
    merged = merge_metadata(MetadataSources(user=user, embedded=embedded, fetched=fetched, fallback=fallback))
    print(f"   [Metadata] Title='{merged.title}' Author='{merged.author}' Language='{merged.language or 'unknown'}'")
    return merged


def build_argument_parser(project_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate audiobook audio from EPUB or plain text using ComfyUI/VibeVoice.")
    parser.add_argument("--input-book", default=str(project_root / "pg35-images-3.epub"), help="Path to the input EPUB/TXT/MD file.")
    parser.add_argument("--output-dir", default=str(project_root / "audiobook_output"), help="Directory for generated audio.")
    parser.add_argument("--source-mode", choices=["auto", "epub", "text"], default="auto")
    parser.add_argument("--pages-per-chapter", type=int, default=1)
    parser.add_argument("--target-words-per-chapter", type=int, default=2500)
    parser.add_argument("--min-paragraphs-per-chapter", type=int, default=3)
    parser.add_argument("--chapters-per-part", type=int, default=5)
    parser.add_argument("--max-words-per-chunk", type=int, default=250)
    parser.add_argument("--chunks-per-batch", type=int, default=7)
    parser.add_argument("--diffusion-steps", type=int, default=25)
    parser.add_argument("--temperature", type=float, default=0.95)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--cfg-scale", type=float, default=1.3)
    parser.add_argument("--free-memory-after-generate", action="store_true")
    parser.add_argument("--output-format", choices=["flac", "mp3", "m4b"], default="flac")
    parser.add_argument("--fetch-metadata", action="store_true", help="Optional online metadata lookup (disabled by default).")
    parser.add_argument("--gutenberg-id", default="", help="Optional explicit Gutenberg ID for online metadata fetch.")
    parser.add_argument("--title", default="", help="Override audiobook title (highest metadata priority).")
    parser.add_argument("--author", default="", help="Override audiobook author (highest metadata priority).")
    parser.add_argument("--comfyui-mode", choices=["network", "spoof"], default="network")
    parser.add_argument("--comfyui-server-address", default="127.0.0.1:8188")
    parser.add_argument("--comfyui-timeout-seconds", type=float, default=None, help="Overrides config default if provided.")
    parser.add_argument("--resume", choices=["auto", "yes", "no"], default="auto")
    parser.add_argument("--provenance-enabled", action="store_true", help="Enable C2PA signing and embedding.")
    parser.add_argument("--provenance-cert-path", default="", help="Path to X.509 certificate for C2PA signing.")
    parser.add_argument("--provenance-key-path", default="", help="Path to private key for C2PA signing.")
    parser.add_argument("--provenance-key-password", default="", help="Optional password for the provenance private key.")
    parser.add_argument(
        "--provenance-failure-mode",
        choices=["soft-fail", "hard-fail"],
        default="soft-fail",
        help="When hard-fail, provenance errors stop the pipeline; soft-fail logs warning and continues.",
    )
    parser.add_argument("--provenance-tool", default="c2patool", help="CLI tool used for C2PA embedding/signing.")
    parser.add_argument("--provenance-claim-generator", default="autoaudio", help="claim_generator value used in C2PA.")
    parser.add_argument(
        "--comfyui-spoof-scenario",
        choices=["success", "timeout", "malformed_history", "missing_view_payload", "connection_error"],
        default="success",
    )
    parser.add_argument("--gui", action="store_true", help="Launch the PySide6 desktop GUI.")
    return parser


def run_pipeline(args: argparse.Namespace, config: AppConfig) -> None:
    input_book = os.path.abspath(args.input_book)
    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(config.state_dir, exist_ok=True)
    logger, run_log_path = configure_run_logger(output_dir)
    logger.info("Pipeline started for input=%s output=%s", input_book, output_dir)

    settings = GenerationSettings(
        max_words_per_chunk=args.max_words_per_chunk,
        chunks_per_batch=args.chunks_per_batch,
        diffusion_steps=args.diffusion_steps,
        temperature=args.temperature,
        top_p=args.top_p,
        cfg_scale=args.cfg_scale,
        free_memory_after_generate=args.free_memory_after_generate,
    )
    workflow_template = load_workflow_template(config.workflow_path)
    provenance_runtime_metadata = _extract_provenance_runtime_metadata(workflow_template)
    comfyui_client = build_comfyui_client(config)
    checkpoint_store = CheckpointStore(state_dir=config.state_dir)
    input_hash = sha256_file(input_book)
    settings_hash = stable_settings_hash(
        {
            "source_mode": args.source_mode,
            "pages_per_chapter": args.pages_per_chapter,
            "target_words_per_chapter": args.target_words_per_chapter,
            "min_paragraphs_per_chapter": args.min_paragraphs_per_chapter,
            "chapters_per_part": args.chapters_per_part,
            "max_words_per_chunk": args.max_words_per_chunk,
            "chunks_per_batch": args.chunks_per_batch,
            "diffusion_steps": args.diffusion_steps,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "cfg_scale": args.cfg_scale,
            "free_memory_after_generate": args.free_memory_after_generate,
            "output_format": args.output_format,
            "fetch_metadata": args.fetch_metadata,
            "gutenberg_id": args.gutenberg_id,
            "title": args.title,
            "author": args.author,
            "comfyui_mode": args.comfyui_mode,
            "comfyui_server_address": args.comfyui_server_address,
            "comfyui_timeout_seconds": args.comfyui_timeout_seconds,
            "provenance_enabled": args.provenance_enabled,
            "provenance_cert_path": args.provenance_cert_path,
            "provenance_key_path": args.provenance_key_path,
            "provenance_failure_mode": args.provenance_failure_mode,
            "provenance_tool": args.provenance_tool,
            "provenance_claim_generator": args.provenance_claim_generator,
        }
    )
    checkpoint = checkpoint_store.load()
    can_resume = (
        checkpoint
        and checkpoint.get("status") in {"running", "failed"}
        and checkpoint.get("input", {}).get("sha256") == input_hash
        and checkpoint.get("settings_hash") == settings_hash
        and checkpoint.get("output", {}).get("dir") == output_dir
    )

    if args.resume == "yes" and not can_resume:
        raise ResumeStateError("Resume requested (--resume yes) but no compatible checkpoint state exists.")

    if can_resume and args.resume in {"auto", "yes"}:
        print(f"[Resume] Loaded checkpoint at {checkpoint_store.path}")
        checkpoint.setdefault("progress", {}).setdefault("completed_chapters", [])
        checkpoint["progress"].setdefault("completed_segments", {})
        checkpoint.setdefault("artifacts", {}).setdefault("segments", {})
        checkpoint["artifacts"].setdefault("chapters", {})
        checkpoint["artifacts"].setdefault("parts", {})
        checkpoint["artifacts"].setdefault("provenance", {})
        checkpoint.setdefault("errors", [])
    else:
        checkpoint = create_initial_checkpoint(
            input_path=input_book,
            input_hash=input_hash,
            settings_hash=settings_hash,
            output_dir=output_dir,
            output_format=args.output_format,
            ui_state={
                "input_book": args.input_book,
                "output_dir": args.output_dir,
                "source_mode": args.source_mode,
                "fetch_metadata": args.fetch_metadata,
                "title": args.title,
                "author": args.author,
                "resume_mode": args.resume,
            },
        )
        checkpoint_store.save(checkpoint)

    print(f"--- Processing Book: {input_book} ---")
    try:
        source_mode = detect_source_mode(input_book, args.source_mode)
    except ValueError as exc:
        raise InputValidationError(str(exc)) from exc

    metadata = resolve_metadata(args, input_book, source_mode, output_dir)

    if source_mode == "epub":
        blocks = extract_text_blocks_from_epub(input_book)
        chapters = group_blocks_into_chapters(blocks, args.pages_per_chapter)
    else:
        blocks = extract_text_blocks_from_text_file(input_book)
        chapters = group_paragraphs_into_chapters(
            blocks,
            target_words_per_chapter=args.target_words_per_chapter,
            min_paragraphs_per_chapter=args.min_paragraphs_per_chapter,
        )

    if not chapters:
        raise InputValidationError("No chapters found. Check the input file content/format and chapter grouping settings.")

    part_index = 1
    part_chapter_files: list[tuple[str, str]] = []
    segment_cache_dir = os.path.join(output_dir, ".segments")
    os.makedirs(segment_cache_dir, exist_ok=True)

    try:
        for ch_idx, (title, text) in enumerate(chapters):
            chapter_key = str(ch_idx)
            chapter_artifact = checkpoint.get("artifacts", {}).get("chapters", {}).get(chapter_key)
            if chapter_artifact and validate_artifact(chapter_artifact.get("path", ""), chapter_artifact.get("sha256")):
                print(f"\nProcessing {title}")
                print("   -> Resume skip: chapter artifact passed integrity checks.")
                part_chapter_files.append((chapter_artifact["path"], title))
                continue

            print(f"\nProcessing {title}")
            if "Project Gutenberg" in text[:500]:
                print("   (Skipping likely Gutenberg preamble)")
                continue

            raw_chunks = split_text_smart(text, max_words=args.max_words_per_chunk)
            chunks = []
            for i in range(0, len(raw_chunks), args.chunks_per_batch):
                chunks.append(" [pause] ".join(raw_chunks[i : i + args.chunks_per_batch]))

            print(f"   -> Split into {len(raw_chunks)} raw segments, grouped into {len(chunks)} batches.")
            segment_files = []
            segment_keys_for_chapter: list[str] = []

            for seg_idx, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue

                segment_key = f"{ch_idx}:{seg_idx}"
                segment_artifact = checkpoint.get("artifacts", {}).get("segments", {}).get(segment_key)
                if segment_artifact and validate_artifact(segment_artifact.get("path", ""), segment_artifact.get("sha256")):
                    segment_files.append(segment_artifact["path"])
                    segment_keys_for_chapter.append(segment_key)
                    print(f"   -> Segment {seg_idx + 1}/{len(chunks)} resume hit [OK]")
                    continue

                print(f"   -> Generating Segment {seg_idx + 1}/{len(chunks)}...", end="\r")
                audio_data, audio_ext = process_segment(
                    text_segment=chunk,
                    workflow_template=workflow_template,
                    settings=settings,
                    config=config,
                    comfyui_client=comfyui_client,
                )

                if audio_data and len(audio_data) > 16:
                    ext_to_use = audio_ext if audio_ext in [".wav", ".flac", ".mp3", ".opus"] else ".flac"
                    temp_filename = os.path.join(segment_cache_dir, f"temp_ch{ch_idx + 1}_seg{seg_idx + 1}{ext_to_use}")

                    segment_title = safe_name(title) or f"Chapter_{ch_idx + 1:03d}"
                    segment_content_id = f"{segment_title}_seg_{seg_idx + 1:03d}"

                    watermark_result, marked_audio_data = watermark_audio_bytes_best_effort(
                        audio_data,
                        content_id=segment_content_id,
                        logger=logger,
                    )

                    if not watermark_result.applied or not watermark_result.verified:
                        raise RuntimeError(f"AI marking failed strict checks: applied={watermark_result.applied}, verified={watermark_result.verified}")

                    adapter = adapter_for_extension(temp_filename)
                    cmd = ["ffmpeg", "-y", "-f", "wav", "-i", "pipe:0"]
                    cmd.extend(_ai_marking_metadata_args())
                    cmd.extend(adapter.ffmpeg_output_args())
                    cmd.append(temp_filename)

                    try:
                        subprocess.run(cmd, input=marked_audio_data, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except subprocess.CalledProcessError as exc:
                        raise ComfyUIProtocolError(f"Error encoding segment {seg_idx + 1}/{len(chunks)}: {exc}") from exc

                    _write_ai_marking_manifest(
                        temp_filename,
                        content_id=segment_content_id,
                        metadata_embedded=True,
                        watermark_applied=watermark_result.applied,
                        watermark_verified=watermark_result.verified,
                        watermark_detail=watermark_result.detail,
                    )
                    segment_files.append(temp_filename)
                    segment_keys_for_chapter.append(segment_key)
                    checkpoint["artifacts"]["segments"][segment_key] = {
                        "path": temp_filename,
                        "sha256": sha256_file(temp_filename),
                    }
                    checkpoint["progress"]["completed_segments"].setdefault(chapter_key, [])
                    if seg_idx not in checkpoint["progress"]["completed_segments"][chapter_key]:
                        checkpoint["progress"]["completed_segments"][chapter_key].append(seg_idx)
                    checkpoint_store.save(checkpoint)
                    print(f"   -> Generated Segment {seg_idx + 1}/{len(chunks)} [OK]   ")
                else:
                    raise ComfyUIProtocolError(
                        f"Generated Segment {seg_idx + 1}/{len(chunks)} failed: ComfyUI returned invalid audio payload."
                    )

            if not segment_files:
                print("   -> Chapter failed (no audio generated).")
                continue

            safe_title = safe_name(title) or f"Chapter_{ch_idx + 1:03d}"
            chapter_filename = os.path.join(output_dir, f"Chapter_{ch_idx + 1:03d}_{safe_title}.{args.output_format}")
            chapter_meta = {"title": title, "artist": metadata.author, "album": metadata.title, "track": str(ch_idx + 1)}

            print(f"   -> Stitching chapter to {chapter_filename}...")
            if combine_audio_files(
                segment_files,
                chapter_filename,
                metadata=chapter_meta,
                cover_image=metadata.cover_image_path,
            ):
                provenance = apply_c2pa_with_policy(
                    artifact_path=chapter_filename,
                    config=config.provenance,
                    runtime_metadata=provenance_runtime_metadata,
                    logger=logger,
                )
                part_chapter_files.append((chapter_filename, title))
                checkpoint["artifacts"]["chapters"][chapter_key] = {
                    "path": chapter_filename,
                    "sha256": sha256_file(chapter_filename),
                    "title": title,
                }
                if provenance:
                    checkpoint["artifacts"]["provenance"][chapter_filename] = {
                        "manifest_id": provenance.manifest_id,
                        "embedding_path": provenance.embedding_path,
                    }
                    logger.info(
                        "Checkpointed C2PA manifest artifact=%s manifest_id=%s", chapter_filename, provenance.manifest_id
                    )
                if ch_idx not in checkpoint["progress"]["completed_chapters"]:
                    checkpoint["progress"]["completed_chapters"].append(ch_idx)
                checkpoint_store.save(checkpoint)

            for segment_key, filename in zip(segment_keys_for_chapter, segment_files):
                try:
                    os.remove(filename)
                except Exception:
                    pass
                checkpoint["artifacts"]["segments"].pop(segment_key, None)
            checkpoint["progress"]["completed_segments"].pop(chapter_key, None)
            checkpoint_store.save(checkpoint)

            print("   -> Chapter complete.")

            if len(part_chapter_files) >= args.chapters_per_part:
                stitch_part(
                    part_chapter_files,
                    output_dir,
                    metadata,
                    part_index,
                    args.output_format,
                    checkpoint,
                    checkpoint_store,
                    config,
                    provenance_runtime_metadata,
                    logger,
                )
                part_index += 1
                part_chapter_files = []

        if part_chapter_files:
            stitch_part(
                part_chapter_files,
                output_dir,
                metadata,
                part_index,
                args.output_format,
                checkpoint,
                checkpoint_store,
                config,
                provenance_runtime_metadata,
                logger,
            )
        checkpoint["status"] = "completed"
        checkpoint_store.save(checkpoint)
    except Exception as exc:
        checkpoint["status"] = "failed"
        checkpoint.setdefault("errors", []).append({"message": str(exc), "traceback": traceback.format_exc()})
        checkpoint_store.save(checkpoint)
        logger.exception("Pipeline failed")
        if isinstance(
            exc,
            (
                InputValidationError,
                MetadataExtractionError,
                ResumeStateError,
                AudioStitchError,
                ComfyUIConnectionError,
                ComfyUIProtocolError,
                PipelineRuntimeError,
            ),
        ):
            raise
        raise PipelineRuntimeError(f"Unexpected pipeline failure. See debug log: {run_log_path}") from exc

    logger.info("Pipeline completed successfully")
    print("\nDone.")


def stitch_part(
    part_chapter_files,
    output_dir,
    metadata: BookMetadata,
    part_index: int,
    output_format: str,
    checkpoint: dict,
    checkpoint_store: CheckpointStore,
    config: AppConfig,
    provenance_runtime_metadata: ProvenanceRuntimeMetadata,
    logger: logging.Logger,
):
    part_filename = os.path.join(output_dir, f"{metadata.title} - Part_{part_index:03d}.{output_format}")
    part_meta = {
        "title": f"{metadata.title} - Part {part_index}",
        "artist": metadata.author,
        "album": metadata.title,
        "disc": str(part_index),
    }

    files_to_stitch = [file_path for file_path, _ in part_chapter_files]
    titles_to_embed = [title for _, title in part_chapter_files]

    print(f"   -> Stitching {len(part_chapter_files)} chapters into {part_filename}...")
    if combine_audio_files(
        files_to_stitch,
        part_filename,
        metadata=part_meta,
        chapter_titles=titles_to_embed,
        cover_image=metadata.cover_image_path,
    ):
        provenance = apply_c2pa_with_policy(
            artifact_path=part_filename,
            config=config.provenance,
            runtime_metadata=provenance_runtime_metadata,
            logger=logger,
        )
        checkpoint["artifacts"]["parts"][str(part_index)] = {
            "path": part_filename,
            "sha256": sha256_file(part_filename),
            "title": f"{metadata.title} - Part {part_index}",
        }
        if provenance:
            checkpoint["artifacts"]["provenance"][part_filename] = {
                "manifest_id": provenance.manifest_id,
                "embedding_path": provenance.embedding_path,
            }
            logger.info("Checkpointed C2PA manifest artifact=%s manifest_id=%s", part_filename, provenance.manifest_id)
        checkpoint_store.save(checkpoint)
        print(f"   -> Part {part_index:03d} complete.")


def main(argv: list[str] | None = None) -> None:
    project_root = Path(__file__).resolve().parents[2]
    parser = build_argument_parser(project_root)
    args = parser.parse_args(argv)

    if args.gui:
        from gui.app import launch_gui

        raise SystemExit(launch_gui(project_root))

    kwargs = {
        "project_root": project_root,
        "comfyui_mode": args.comfyui_mode,
        "comfyui_server_address": args.comfyui_server_address,
        "comfyui_spoof_scenario": args.comfyui_spoof_scenario,
    }
    if args.comfyui_timeout_seconds is not None:
        kwargs["comfyui_timeout_seconds"] = args.comfyui_timeout_seconds

    config = AppConfig(
        **kwargs,
        provenance=ProvenanceConfig(
            enabled=args.provenance_enabled,
            cert_path=args.provenance_cert_path,
            key_path=args.provenance_key_path,
            key_password=args.provenance_key_password,
            hard_fail=args.provenance_failure_mode == "hard-fail",
            tool=args.provenance_tool,
            claim_generator=args.provenance_claim_generator,
        ),
    )
    try:
        run_pipeline(args, config)
    except (InputValidationError, MetadataExtractionError, ResumeStateError, AudioStitchError, ComfyUIConnectionError, ComfyUIProtocolError, PipelineRuntimeError) as exc:
        print(f"ERROR: [{exc.guidance.code}] {exc}")
        print(f"REMEDIATION: {exc.remediation}")
        raise SystemExit(exc.guidance.exit_code)
