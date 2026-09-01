from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import signal
import subprocess
import traceback
from collections.abc import Callable
from pathlib import Path

from comfyui.client import (
    ComfyUIClient,
    ComfyUIClientError,
    ComfyUIConnectionError as ClientComfyUIConnectionError,
    ComfyUIProtocolError as ClientComfyUIProtocolError,
)
from comfyui.real_client import RealComfyUIClient
from comfyui.spoof_client import SpoofComfyUIClient
from comfyui.workflow_loader import find_qwen_generation_node, load_workflow_template
from core.audio_assembly import (
    AUDIO_ASSEMBLY_POLICY_VERSION,
    ASSEMBLY_CHANNELS,
    ASSEMBLY_SAMPLE_RATE,
    assemble_lossless_master,
    chapter_markers_for_files,
    encode_lossless_master,
    ensure_silence_file,
    interleave_audio_files,
)
from core.checkpoint import (
    CheckpointError,
    CheckpointStore,
    create_initial_checkpoint,
    sha256_file,
    stable_settings_hash,
    validate_artifact,
)
from core.cancellation import CancellationToken
from core.config import AppConfig, GenerationSettings, QWEN_MODEL_CHOICES, QWEN_PRESET_SPEAKERS
from core.errors import (
    AudioStitchError,
    ComfyUIConnectionError,
    ComfyUIProtocolError,
    InputValidationError,
    MetadataExtractionError,
    PipelineRuntimeError,
    PipelineCancelled,
    ResumeStateError,
)
from core.filenames import safe_filename_component
from core.logging_utils import configure_run_logger
from core.metadata_adapters import adapter_for_extension
from core.narrator import NarratorCatalog, NarratorProfileError
from core.plan import BookPlan, BookPlanError, BookPlanStore, PlannedChapter, PlannedSegment
from core.progress import ProgressTracker, ProgressUpdate
from core.segmentation import SegmentPolicy, default_segment_policy, segment_text_for_qwen
from core.version import default_claim_generator, runtime_autoaudio_version
from metadata.epub_parser import (
    EPUB_PARSER_POLICY_VERSION,
    EpubParseError,
    ParsedEpub,
    parse_epub,
    write_cover_art,
)
from metadata.extractors import extract_text_fallback_metadata
from metadata.source_mode import detect_source_mode
from metadata.gutenberg import fetch_gutenberg_metadata
from metadata.id_utils import guess_gutenberg_id
from metadata.models import BookMetadata, MetadataSources, merge_metadata
from provenance.ai_marking import (
    AI_MARKING_SCHEMA,
    ai_marking_metadata_args,
    cleanup_orphan_manifests,
    manifest_path_for,
    remove_artifact_and_manifest,
    refresh_ai_marking_manifest_hash,
    validate_watermarked_artifact,
    write_ai_marking_manifest,
)
from provenance.audio_watermark import watermark_audio_bytes_best_effort
from provenance.c2pa import (
    ProvenanceConfig,
    ProvenanceRuntimeMetadata,
    apply_c2pa_with_policy,
)


CHAPTER_DISCLOSURE_TEXT = "This audio was generated synthetically with AutoAudio."
CHAPTER_DISCLOSURE_POLICY_VERSION = "chapter-disclosure-v1"
DEFAULT_DISCLOSURE_GAP_MS = 700
DEFAULT_SEGMENT_GAP_MS = 150
DEFAULT_CHAPTER_GAP_MS = 1000


def extract_text_blocks_from_epub(epub_path: str) -> list[tuple[str, str]]:
    if not os.path.exists(epub_path):
        print(f"ERROR: File not found: {epub_path}")
        return []

    return list(parse_epub(epub_path).text_blocks)


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


def build_qwen_book_plan(
    chapters: list[tuple[str, str]],
    *,
    input_hash: str,
    settings_hash: str,
    workflow_hash: str,
    segment_policy: SegmentPolicy,
) -> BookPlan:
    """Freeze Qwen-aware semantic segments so a run cannot drift on resume."""
    planned_chapters: list[PlannedChapter] = []
    for chapter_index, (title, text) in enumerate(chapters):
        segment_texts = segment_text_for_qwen(text, segment_policy)

        planned_chapters.append(
            PlannedChapter(
                index=chapter_index,
                title=title,
                segments=tuple(
                    PlannedSegment.from_text(
                        chapter_index=chapter_index,
                        segment_index=segment_index,
                        text=segment_text,
                    )
                    for segment_index, segment_text in enumerate(segment_texts)
                ),
                skipped_reason=None,
            )
        )

    return BookPlan(
        input_sha256=input_hash,
        settings_sha256=settings_hash,
        workflow_sha256=workflow_hash,
        chapters=tuple(planned_chapters),
    )


def process_segment(
    *,
    text_segment: str,
    workflow_template: dict,
    settings: GenerationSettings,
    config: AppConfig,
    comfyui_client: ComfyUIClient,
    cancellation: CancellationToken | None = None,
) -> tuple[bytes | None, str | None]:
    try:
        artifact = comfyui_client.generate_audio(
            workflow_template=workflow_template,
            text_segment=text_segment,
            settings=settings,
            timeout_seconds=config.comfyui_timeout_seconds,
            cancellation=cancellation,
        )
        return artifact.content, artifact.extension
    except ClientComfyUIConnectionError as exc:
        raise ComfyUIConnectionError(str(exc)) from exc
    except ClientComfyUIProtocolError as exc:
        raise ComfyUIProtocolError(str(exc)) from exc
    except ComfyUIClientError as exc:
        raise PipelineRuntimeError(f"ComfyUI request failed: {exc}") from exc


def write_watermarked_audio_artifact(
    *,
    audio_data: bytes,
    output_path: str | Path,
    content_id: str,
    logger: logging.Logger,
) -> None:
    output_path = str(output_path)
    watermark_result, marked_audio_data = watermark_audio_bytes_best_effort(
        audio_data,
        content_id=content_id,
        logger=logger,
    )
    if not watermark_result.applied or not watermark_result.verified:
        raise PipelineRuntimeError(
            f"AI marking failed strict checks: applied={watermark_result.applied}, "
            f"verified={watermark_result.verified}"
        )

    adapter = adapter_for_extension(output_path)
    command = ["ffmpeg", "-y", "-f", "wav", "-i", "pipe:0"]
    command.extend(ai_marking_metadata_args())
    command.extend(adapter.ffmpeg_output_args())
    command.extend(["-ar", str(ASSEMBLY_SAMPLE_RATE), "-ac", str(ASSEMBLY_CHANNELS)])
    command.append(output_path)
    try:
        subprocess.run(
            command,
            input=marked_audio_data,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise PipelineRuntimeError(f"Error encoding watermarked audio artifact {output_path}: {exc}") from exc

    write_ai_marking_manifest(
        output_path,
        content_id=content_id,
        metadata_embedded=True,
        watermark_applied=watermark_result.applied,
        watermark_verified=watermark_result.verified,
        watermark_detail=watermark_result.detail,
    )


def ensure_disclosure_asset(
    *,
    state_dir: Path,
    workflow_template: dict,
    config: AppConfig,
    comfyui_client: ComfyUIClient,
    checkpoint: dict,
    checkpoint_store: CheckpointStore,
    logger: logging.Logger,
    cancellation: CancellationToken | None = None,
) -> str:
    if cancellation:
        cancellation.raise_if_cancelled()
    state_dir.mkdir(parents=True, exist_ok=True)
    disclosure = checkpoint.setdefault("artifacts", {}).setdefault("disclosure", {})
    manifest_path = disclosure.get("manifest_path", "")
    if validate_artifact(disclosure.get("path", ""), disclosure.get("sha256")) and validate_artifact(
        manifest_path, disclosure.get("manifest_sha256")
    ):
        try:
            validate_watermarked_artifact(disclosure["path"])
            return str(disclosure["path"])
        except ValueError:
            remove_artifact_and_manifest(disclosure.get("path", ""))

    settings = GenerationSettings(
        voice_mode="preset",
        speaker="Eric",
        instruct="Use a neutral, clear announcement voice with steady pacing.",
        model_choice="1.7B",
        device="auto",
        precision="bf16",
        language="English",
        seed=268583702137267,
        max_new_tokens=2048,
        top_p=0.8,
        top_k=20,
        temperature=1.0,
        repetition_penalty=1.05,
        attention="sdpa",
        unload_model_after_generate=False,
    )
    audio_data, _audio_ext = process_segment(
        text_segment=CHAPTER_DISCLOSURE_TEXT,
        workflow_template=workflow_template,
        settings=settings,
        config=config,
        comfyui_client=comfyui_client,
        cancellation=cancellation,
    )
    if not audio_data or len(audio_data) <= 16:
        raise ComfyUIProtocolError("Chapter disclosure generation returned an invalid audio payload.")

    output_path = state_dir / "chapter_disclosure.flac"
    content_id = "autoaudio_chapter_disclosure_v1"
    write_watermarked_audio_artifact(
        audio_data=audio_data,
        output_path=output_path,
        content_id=content_id,
        logger=logger,
    )
    sidecar_path = manifest_path_for(output_path)
    checkpoint["artifacts"]["disclosure"] = {
        "path": str(output_path),
        "sha256": sha256_file(output_path),
        "manifest_path": str(sidecar_path),
        "manifest_sha256": sha256_file(sidecar_path),
        "policy_version": CHAPTER_DISCLOSURE_POLICY_VERSION,
        "text_sha256": hashlib.sha256(CHAPTER_DISCLOSURE_TEXT.encode("utf-8")).hexdigest(),
    }
    checkpoint_store.save(checkpoint)
    return str(output_path)


def ensure_silence_asset(
    *,
    state_dir: Path,
    kind: str,
    duration_ms: int,
    checkpoint: dict,
    checkpoint_store: CheckpointStore,
) -> str | None:
    """Create one deterministic, lossless silence asset per configured gap."""
    if duration_ms == 0:
        return None

    silence_artifacts = checkpoint.setdefault("artifacts", {}).setdefault("silence", {})
    record = silence_artifacts.get(kind, {})
    if record.get("duration_ms") == duration_ms and validate_artifact(
        record.get("path", ""), record.get("sha256")
    ):
        return str(record["path"])

    output_path = state_dir / "silence" / f"{kind}_{duration_ms}ms.flac"
    ensure_silence_file(output_path, duration_ms)
    silence_artifacts[kind] = {
        "path": str(output_path),
        "sha256": sha256_file(output_path),
        "duration_ms": duration_ms,
    }
    checkpoint_store.save(checkpoint)
    return str(output_path)


def _marked_checkpoint_artifact_is_valid(record: dict | None) -> bool:
    if not record:
        return False
    path = record.get("path", "")
    manifest_path = record.get("manifest_path", str(manifest_path_for(path)) if path else "")
    if not validate_artifact(path, record.get("sha256")) or not validate_artifact(
        manifest_path, record.get("manifest_sha256")
    ):
        return False
    try:
        validate_watermarked_artifact(path)
        return True
    except ValueError:
        return False


def _marked_artifact_record(path: str | Path, **extra) -> dict:
    artifact = Path(path)
    manifest = manifest_path_for(artifact)
    return {
        "path": str(artifact),
        "sha256": sha256_file(artifact),
        "manifest_path": str(manifest),
        "manifest_sha256": sha256_file(manifest),
        **extra,
    }


def _completed_progress_keys(book_plan: BookPlan, checkpoint: dict) -> set[str]:
    """Recover only work that the pipeline can actually reuse from this checkpoint."""
    completed: set[str] = set()
    artifacts = checkpoint.get("artifacts", {})
    if _marked_checkpoint_artifact_is_valid(artifacts.get("disclosure")):
        completed.add(ProgressTracker.disclosure_key())

    completed_part_chapters: set[int] = set()
    for raw_part_index, record in artifacts.get("parts", {}).items():
        if not _marked_checkpoint_artifact_is_valid(record):
            continue
        try:
            part_index = int(raw_part_index)
            chapter_indexes = {int(value) for value in record.get("chapter_indexes", [])}
        except (TypeError, ValueError):
            continue
        completed.add(ProgressTracker.part_key(part_index))
        completed_part_chapters.update(chapter_indexes)

    chapter_artifacts = artifacts.get("chapters", {})
    chapter_masters = artifacts.get("chapter_masters", {})
    segment_artifacts = artifacts.get("segments", {})
    for chapter in book_plan.chapters:
        if chapter.skipped_reason:
            continue
        chapter_key = str(chapter.index)
        chapter_is_valid = _marked_checkpoint_artifact_is_valid(chapter_artifacts.get(chapter_key))
        master_is_valid = _marked_checkpoint_artifact_is_valid(chapter_masters.get(chapter_key))
        fully_reusable = master_is_valid or (chapter.index in completed_part_chapters and chapter_is_valid)

        if fully_reusable:
            completed.update(
                ProgressTracker.segment_key(chapter.index, segment.index)
                for segment in chapter.segments
            )
        else:
            for segment in chapter.segments:
                if _marked_checkpoint_artifact_is_valid(segment_artifacts.get(segment.id)):
                    completed.add(ProgressTracker.segment_key(chapter.index, segment.index))

        if chapter_is_valid and fully_reusable:
            completed.add(ProgressTracker.chapter_key(chapter.index))
    return completed


def _cleanup_chapter_segment_artifacts(
    *,
    chapter_index: int,
    checkpoint: dict,
    checkpoint_store: CheckpointStore,
    segment_cache_dir: Path,
) -> None:
    chapter_prefix = f"{chapter_index}:"
    segment_artifacts = checkpoint["artifacts"]["segments"]
    for segment_key, record in list(segment_artifacts.items()):
        if record.get("chapter_index") != chapter_index and not segment_key.startswith(chapter_prefix):
            continue
        remove_artifact_and_manifest(record.get("path", ""))
        segment_artifacts.pop(segment_key, None)
    checkpoint["progress"]["completed_segments"].pop(str(chapter_index), None)
    checkpoint_store.save(checkpoint)
    cleanup_orphan_manifests(segment_cache_dir)


def build_comfyui_client(config: AppConfig) -> ComfyUIClient:
    if config.comfyui_mode == "spoof":
        return SpoofComfyUIClient(scenario=config.comfyui_spoof_scenario)
    return RealComfyUIClient(server_address=config.comfyui_server_address)


def _extract_provenance_runtime_metadata(
    workflow_template: dict,
    settings: GenerationSettings,
) -> ProvenanceRuntimeMetadata:
    node_id, _voice_mode = find_qwen_generation_node(workflow_template)
    node = workflow_template[node_id]
    class_type = str(node.get("class_type") or "unknown-backend")
    meta = node.get("_meta", {})
    reported_backend_version = meta.get("version") if isinstance(meta, dict) else None
    backend_version = (
        str(reported_backend_version).strip()
        if reported_backend_version is not None and str(reported_backend_version).strip()
        else "unreported"
    )

    return ProvenanceRuntimeMetadata(
        model_name="Qwen3-TTS",
        model_version=settings.model_choice.strip(),
        backend_name=class_type,
        backend_version=backend_version,
        software_name="AutoAudio",
        software_version=runtime_autoaudio_version(),
    )


def extract_cover_art(epub_path: str, output_dir: str):
    try:
        cover_path = write_cover_art(parse_epub(epub_path), output_dir)
        if cover_path:
            print(f"   [Cover Art] Extracted: {cover_path}")
        return cover_path
    except Exception as exc:
        logging.getLogger("autoaudio.run").warning("Cover art extraction failed: %s", exc)

    return None


def resolve_metadata(
    args: argparse.Namespace,
    input_book: str,
    source_mode: str,
    output_dir: str,
    *,
    parsed_epub: ParsedEpub | None = None,
) -> BookMetadata:
    fallback = BookMetadata(title=os.path.splitext(os.path.basename(input_book))[0], author="Unknown")

    if source_mode == "epub":
        try:
            parsed_epub = parsed_epub or parse_epub(input_book)
            embedded = parsed_epub.metadata
        except Exception as exc:
            raise MetadataExtractionError(f"Embedded EPUB metadata extraction failed for {input_book}: {exc}") from exc
        try:
            cover = write_cover_art(parsed_epub, output_dir)
        except Exception as exc:
            logging.getLogger("autoaudio.run").warning("Cover art extraction failed: %s", exc)
            cover = None
        if cover:
            print(f"   [Cover Art] Extracted: {cover}")
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
    parser = argparse.ArgumentParser(description="Generate audiobook audio from EPUB or plain text using ComfyUI/Qwen3-TTS.")
    parser.add_argument("--input-book", default=str(project_root / "pg35-images-3.epub"), help="Path to the input EPUB/TXT/MD file.")
    parser.add_argument("--output-dir", default=str(project_root / "audiobook_output"), help="Directory for generated audio.")
    parser.add_argument("--source-mode", choices=["auto", "epub", "text"], default="auto")
    parser.add_argument("--pages-per-chapter", type=int, default=1)
    parser.add_argument("--target-words-per-chapter", type=int, default=2500)
    parser.add_argument("--min-paragraphs-per-chapter", type=int, default=3)
    parser.add_argument("--chapters-per-part", type=int, default=5)
    parser.add_argument("--target-words-per-segment", type=int, default=None)
    parser.add_argument("--max-words-per-segment", type=int, default=None)
    parser.add_argument(
        "--disclosure-gap-ms",
        type=int,
        default=DEFAULT_DISCLOSURE_GAP_MS,
        help="Lossless silence inserted after the chapter disclosure (0 disables).",
    )
    parser.add_argument(
        "--segment-gap-ms",
        type=int,
        default=DEFAULT_SEGMENT_GAP_MS,
        help="Lossless silence inserted between narration segments (0 disables).",
    )
    parser.add_argument(
        "--chapter-gap-ms",
        type=int,
        default=DEFAULT_CHAPTER_GAP_MS,
        help="Lossless silence inserted between chapters in part files (0 disables).",
    )
    parser.add_argument("--narrator-profile", default="preset-eric-neutral")
    parser.add_argument(
        "--speaker",
        choices=QWEN_PRESET_SPEAKERS,
        default=None,
        help="Override the selected preset profile's built-in Qwen speaker.",
    )
    parser.add_argument("--voice-instruct", default=None, help="Override the selected profile's voice/style instruction.")
    parser.add_argument("--model-choice", choices=QWEN_MODEL_CHOICES, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--precision", default=None)
    parser.add_argument("--language", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--max-new-tokens", type=int, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--repetition-penalty", type=float, default=None)
    parser.add_argument("--attention", choices=["sdpa", "flash_attn"], default=None)
    parser.add_argument(
        "--unload-model-after-generate",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
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
    parser.add_argument(
        "--provenance-claim-generator",
        default=default_claim_generator(),
        help="claim_generator value used in C2PA.",
    )
    parser.add_argument(
        "--comfyui-spoof-scenario",
        choices=["success", "timeout", "malformed_history", "missing_view_payload", "connection_error"],
        default="success",
    )
    parser.add_argument("--gui", action="store_true", help="Launch the PySide6 desktop GUI.")
    parser.add_argument(
        "--version",
        dest="version",
        action="version",
        version=f"%(prog)s {runtime_autoaudio_version()}",
    )
    return parser


def run_pipeline(
    args: argparse.Namespace,
    config: AppConfig,
    cancellation: CancellationToken | None = None,
    progress_callback: Callable[[ProgressUpdate], None] | None = None,
) -> None:
    input_book = os.path.abspath(args.input_book)
    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    state_dir = config.state_dir_for(output_dir)
    os.makedirs(state_dir, exist_ok=True)
    logger, run_log_path = configure_run_logger(output_dir)
    logger.info("Pipeline started for input=%s output=%s", input_book, output_dir)

    gap_settings = {
        "disclosure_gap_ms": getattr(args, "disclosure_gap_ms", DEFAULT_DISCLOSURE_GAP_MS),
        "segment_gap_ms": getattr(args, "segment_gap_ms", DEFAULT_SEGMENT_GAP_MS),
        "chapter_gap_ms": getattr(args, "chapter_gap_ms", DEFAULT_CHAPTER_GAP_MS),
    }
    invalid_gap = next(
        (name for name, duration_ms in gap_settings.items() if duration_ms < 0 or duration_ms > 60_000),
        None,
    )
    if invalid_gap:
        raise InputValidationError(f"{invalid_gap.replace('_', '-')} must be between 0 and 60000 milliseconds.")
    disclosure_gap_ms = gap_settings["disclosure_gap_ms"]
    segment_gap_ms = gap_settings["segment_gap_ms"]
    chapter_gap_ms = gap_settings["chapter_gap_ms"]

    try:
        source_mode = detect_source_mode(input_book, args.source_mode)
    except ValueError as exc:
        raise InputValidationError(str(exc)) from exc

    try:
        narrator_catalog = NarratorCatalog.load(config.narrator_profiles_path)
        narrator_profile = narrator_catalog.get(args.narrator_profile)
        settings = narrator_profile.with_overrides(
            speaker=args.speaker,
            instruct=args.voice_instruct,
            model_choice=args.model_choice,
            device=args.device,
            precision=args.precision,
            language=args.language,
            seed=args.seed,
            max_new_tokens=args.max_new_tokens,
            top_p=args.top_p,
            top_k=args.top_k,
            temperature=args.temperature,
            repetition_penalty=args.repetition_penalty,
            attention=args.attention,
            unload_model_after_generate=args.unload_model_after_generate,
        )
    except NarratorProfileError as exc:
        raise InputValidationError(f"Invalid narrator profile: {exc}") from exc
    default_policy = default_segment_policy(settings.voice_mode)
    try:
        segment_policy = SegmentPolicy(
            target_words=(
                args.target_words_per_segment
                if args.target_words_per_segment is not None
                else default_policy.target_words
            ),
            max_words=(
                args.max_words_per_segment if args.max_words_per_segment is not None else default_policy.max_words
            ),
        )
    except ValueError as exc:
        raise InputValidationError(f"Invalid Qwen segment settings: {exc}") from exc
    workflow_path = config.workflow_path_for(settings.voice_mode)
    workflow_template = load_workflow_template(workflow_path)
    narration_workflow_hash = sha256_file(workflow_path)
    disclosure_workflow_path = config.workflow_path_for("preset")
    disclosure_workflow_template = (
        workflow_template
        if disclosure_workflow_path == workflow_path
        else load_workflow_template(disclosure_workflow_path)
    )
    disclosure_workflow_hash = sha256_file(disclosure_workflow_path)
    workflow_hash = stable_settings_hash(
        {
            "narration_workflow_sha256": narration_workflow_hash,
            "disclosure_workflow_sha256": disclosure_workflow_hash,
        }
    )
    provenance_runtime_metadata = _extract_provenance_runtime_metadata(workflow_template, settings)
    comfyui_client = build_comfyui_client(config)
    checkpoint_store = CheckpointStore(state_dir=state_dir)
    plan_store = BookPlanStore(state_dir=state_dir)
    input_hash = sha256_file(input_book)
    settings_hash = stable_settings_hash(
        {
            "source_mode": source_mode,
            "source_parser_policy": EPUB_PARSER_POLICY_VERSION if source_mode == "epub" else "text-v1",
            "pages_per_chapter": args.pages_per_chapter,
            "target_words_per_chapter": args.target_words_per_chapter,
            "min_paragraphs_per_chapter": args.min_paragraphs_per_chapter,
            "chapters_per_part": args.chapters_per_part,
            "target_words_per_segment": segment_policy.target_words,
            "max_words_per_segment": segment_policy.max_words,
            "narrator_profile": narrator_profile.id,
            "narrator_profile_sha256": narrator_profile.sha256,
            "voice_mode": settings.voice_mode,
            "speaker": settings.speaker,
            "voice_instruct": settings.instruct,
            "model_choice": settings.model_choice,
            "device": settings.device,
            "precision": settings.precision,
            "language": settings.language,
            "seed": settings.seed,
            "max_new_tokens": settings.max_new_tokens,
            "top_k": settings.top_k,
            "temperature": settings.temperature,
            "top_p": settings.top_p,
            "repetition_penalty": settings.repetition_penalty,
            "attention": settings.attention,
            "unload_model_after_generate": settings.unload_model_after_generate,
            "chapter_disclosure_policy": CHAPTER_DISCLOSURE_POLICY_VERSION,
            "chapter_disclosure_text": CHAPTER_DISCLOSURE_TEXT,
            "audio_assembly_policy": AUDIO_ASSEMBLY_POLICY_VERSION,
            "ai_marking_schema": AI_MARKING_SCHEMA,
            "disclosure_gap_ms": disclosure_gap_ms,
            "segment_gap_ms": segment_gap_ms,
            "chapter_gap_ms": chapter_gap_ms,
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
    checkpoint = None
    checkpoint_load_error: CheckpointError | None = None
    try:
        checkpoint = checkpoint_store.load()
    except CheckpointError as exc:
        checkpoint_load_error = exc
        logger.warning("Ignoring incompatible checkpoint for non-forced resume: %s", exc)

    can_resume = (
        checkpoint
        and checkpoint.get("status") in {"running", "failed", "cancelled"}
        and checkpoint.get("input", {}).get("sha256") == input_hash
        and checkpoint.get("compatibility", {}).get("settings_sha256") == settings_hash
        and checkpoint.get("compatibility", {}).get("workflow_sha256") == workflow_hash
        and checkpoint.get("output", {}).get("dir") == output_dir
        and checkpoint.get("plan", {}).get("path") == str(plan_store.path)
        and bool(checkpoint.get("plan", {}).get("sha256"))
    )

    if args.resume == "yes" and not can_resume:
        detail = f" ({checkpoint_load_error})" if checkpoint_load_error else ""
        raise ResumeStateError(f"Resume requested (--resume yes) but no compatible checkpoint state exists{detail}.")

    book_plan: BookPlan | None = None
    if can_resume and args.resume in {"auto", "yes"}:
        try:
            book_plan = plan_store.load(expected_sha256=checkpoint["plan"]["sha256"])
            if (
                book_plan.input_sha256 != input_hash
                or book_plan.settings_sha256 != settings_hash
                or book_plan.workflow_sha256 != workflow_hash
            ):
                raise BookPlanError("Book plan compatibility fields do not match the current run.")
        except BookPlanError as exc:
            if args.resume == "yes":
                raise ResumeStateError(f"Resume requested, but the persisted book plan is invalid: {exc}") from exc
            logger.warning("Ignoring invalid book plan for automatic resume: %s", exc)
            book_plan = None

    parsed_epub: ParsedEpub | None = None
    if source_mode == "epub":
        try:
            parsed_epub = parse_epub(input_book)
        except EpubParseError as exc:
            raise InputValidationError(str(exc)) from exc
        for diagnostic in parsed_epub.diagnostics:
            log_method = logger.warning if diagnostic.severity in {"warning", "error", "fatal"} else logger.info
            log_method(
                "EPUB parser diagnostic code=%s resource=%s message=%s",
                diagnostic.code,
                diagnostic.resource or "-",
                diagnostic.message,
            )
        if parsed_epub.gutenberg_detected:
            logger.info(
                "Project Gutenberg source detected; boilerplate normalization changed=%s",
                parsed_epub.gutenberg_changed,
            )

    if book_plan is not None:
        print(f"[Resume] Loaded checkpoint at {checkpoint_store.path}")
        checkpoint["status"] = "running"
        checkpoint.setdefault("progress", {}).setdefault("completed_chapters", [])
        checkpoint["progress"].setdefault("completed_segments", {})
        checkpoint.setdefault("artifacts", {}).setdefault("segments", {})
        checkpoint["artifacts"].setdefault("disclosure", {})
        checkpoint["artifacts"].setdefault("silence", {})
        checkpoint["artifacts"].setdefault("chapter_masters", {})
        checkpoint["artifacts"].setdefault("part_masters", {})
        checkpoint["artifacts"].setdefault("chapters", {})
        checkpoint["artifacts"].setdefault("parts", {})
        checkpoint["artifacts"].setdefault("provenance", {})
        checkpoint.setdefault("errors", [])
        checkpoint_store.save(checkpoint)
    else:
        if source_mode == "epub":
            assert parsed_epub is not None
            blocks = list(parsed_epub.text_blocks)
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

        book_plan = build_qwen_book_plan(
            chapters,
            input_hash=input_hash,
            settings_hash=settings_hash,
            workflow_hash=workflow_hash,
            segment_policy=segment_policy,
        )
        plan_store.save(book_plan)
        checkpoint = create_initial_checkpoint(
            input_path=input_book,
            input_hash=input_hash,
            settings_hash=settings_hash,
            workflow_hash=workflow_hash,
            plan_path=str(plan_store.path),
            plan_hash=book_plan.sha256,
            output_dir=output_dir,
            output_format=args.output_format,
            ui_state={
                "input_book": args.input_book,
                "output_dir": args.output_dir,
                "source_mode": args.source_mode,
                "pages_per_chapter": args.pages_per_chapter,
                "target_words_per_chapter": args.target_words_per_chapter,
                "min_paragraphs_per_chapter": args.min_paragraphs_per_chapter,
                "chapters_per_part": args.chapters_per_part,
                "target_words_per_segment": segment_policy.target_words,
                "max_words_per_segment": segment_policy.max_words,
                "narrator_profile": narrator_profile.id,
                "speaker": settings.speaker,
                "voice_instruct": settings.instruct,
                "model_choice": settings.model_choice,
                "device": settings.device,
                "precision": settings.precision,
                "language": settings.language,
                "seed": settings.seed,
                "max_new_tokens": settings.max_new_tokens,
                "top_p": settings.top_p,
                "top_k": settings.top_k,
                "temperature": settings.temperature,
                "repetition_penalty": settings.repetition_penalty,
                "attention": settings.attention,
                "unload_model_after_generate": settings.unload_model_after_generate,
                "output_format": args.output_format,
                "disclosure_gap_ms": disclosure_gap_ms,
                "segment_gap_ms": segment_gap_ms,
                "chapter_gap_ms": chapter_gap_ms,
                "fetch_metadata": args.fetch_metadata,
                "gutenberg_id": args.gutenberg_id,
                "title": args.title,
                "author": args.author,
                "comfyui_mode": args.comfyui_mode,
                "comfyui_server_address": args.comfyui_server_address,
                "comfyui_timeout_seconds": args.comfyui_timeout_seconds,
                "comfyui_spoof_scenario": args.comfyui_spoof_scenario,
                "provenance_enabled": args.provenance_enabled,
                "provenance_cert_path": args.provenance_cert_path,
                "provenance_key_path": args.provenance_key_path,
                "provenance_failure_mode": args.provenance_failure_mode,
                "provenance_tool": args.provenance_tool,
                "provenance_claim_generator": args.provenance_claim_generator,
                "resume_mode": args.resume,
            },
        )
        checkpoint_store.save(checkpoint)

    print(f"--- Processing Book: {input_book} ---")
    metadata = resolve_metadata(args, input_book, source_mode, output_dir, parsed_epub=parsed_epub)

    progress = ProgressTracker(
        book_plan=book_plan,
        chapters_per_part=args.chapters_per_part,
        completed_keys=_completed_progress_keys(book_plan, checkpoint),
        callback=progress_callback,
    )
    progress.report("Preparing")

    part_index = 1
    part_chapter_files: list[tuple[str, str, int]] = []
    segment_cache_dir = Path(output_dir) / ".segments"
    master_dir = state_dir / "masters"
    segment_cache_dir.mkdir(parents=True, exist_ok=True)
    master_dir.mkdir(parents=True, exist_ok=True)
    cleanup_orphan_manifests(segment_cache_dir)

    try:
        if cancellation:
            cancellation.raise_if_cancelled()
        progress.report("Preparing disclosure")
        disclosure_file = ensure_disclosure_asset(
            state_dir=state_dir,
            workflow_template=disclosure_workflow_template,
            config=config,
            comfyui_client=comfyui_client,
            checkpoint=checkpoint,
            checkpoint_store=checkpoint_store,
            logger=logger,
            cancellation=cancellation,
        )
        progress.complete(ProgressTracker.disclosure_key(), "Disclosure ready")
        disclosure_gap_file = ensure_silence_asset(
            state_dir=state_dir,
            kind="disclosure_gap",
            duration_ms=disclosure_gap_ms,
            checkpoint=checkpoint,
            checkpoint_store=checkpoint_store,
        )
        segment_gap_file = ensure_silence_asset(
            state_dir=state_dir,
            kind="segment_gap",
            duration_ms=segment_gap_ms,
            checkpoint=checkpoint,
            checkpoint_store=checkpoint_store,
        )
        chapter_gap_file = ensure_silence_asset(
            state_dir=state_dir,
            kind="chapter_gap",
            duration_ms=chapter_gap_ms,
            checkpoint=checkpoint,
            checkpoint_store=checkpoint_store,
        )

        completed_part_chapters: set[int] = set()
        for stored_part_index, part_artifact in checkpoint["artifacts"]["parts"].items():
            if not validate_artifact(part_artifact.get("path", ""), part_artifact.get("sha256")):
                continue
            try:
                stored_chapter_indexes = [int(index) for index in part_artifact.get("chapter_indexes", [])]
                part_index = max(part_index, int(stored_part_index) + 1)
            except (TypeError, ValueError):
                logger.warning("Ignoring malformed completed-part checkpoint entry: %s", stored_part_index)
                continue
            completed_part_chapters.update(stored_chapter_indexes)

        for chapter in book_plan.chapters:
            if cancellation:
                cancellation.raise_if_cancelled()
            ch_idx = chapter.index
            title = chapter.title
            chapter_key = str(ch_idx)
            progress.report("Checking resume state", chapter_index=ch_idx)
            chapter_artifact = checkpoint.get("artifacts", {}).get("chapters", {}).get(chapter_key)
            chapter_is_valid = bool(
                chapter_artifact
                and validate_artifact(chapter_artifact.get("path", ""), chapter_artifact.get("sha256"))
            )
            master_artifact = checkpoint["artifacts"]["chapter_masters"].get(chapter_key)
            master_is_valid = _marked_checkpoint_artifact_is_valid(master_artifact)

            if ch_idx in completed_part_chapters and chapter_is_valid:
                print(f"\nProcessing {title}")
                print("   -> Resume skip: chapter belongs to an integrity-checked completed part.")
                progress.report("Resume skip", chapter_index=ch_idx)
                continue

            if master_is_valid:
                print(f"\nProcessing {title}")
                if chapter_is_valid:
                    print("   -> Resume skip: chapter and lossless master passed integrity checks.")
                else:
                    print("   -> Resume encode: rebuilding chapter output from its lossless master.")
                    progress.report("Encoding chapter", chapter_index=ch_idx)
                    chapter_filename = os.path.join(
                        output_dir,
                        f"Chapter_{ch_idx + 1:03d}_{safe_filename_component(title, fallback=f'Chapter {ch_idx + 1:03d}')}.{args.output_format}",
                    )
                    _publish_chapter_from_master(
                        master_path=master_artifact["path"],
                        chapter_filename=chapter_filename,
                        title=title,
                        chapter_index=ch_idx,
                        metadata=metadata,
                        checkpoint=checkpoint,
                        checkpoint_store=checkpoint_store,
                        config=config,
                        provenance_runtime_metadata=provenance_runtime_metadata,
                        logger=logger,
                        cancellation=cancellation,
                    )
                progress.complete(
                    ProgressTracker.chapter_key(ch_idx),
                    "Chapter complete",
                    chapter_index=ch_idx,
                )
                _cleanup_chapter_segment_artifacts(
                    chapter_index=ch_idx,
                    checkpoint=checkpoint,
                    checkpoint_store=checkpoint_store,
                    segment_cache_dir=segment_cache_dir,
                )
                part_chapter_files.append((master_artifact["path"], title, ch_idx))
                if len(part_chapter_files) >= args.chapters_per_part:
                    progress.report("Assembling part")
                    stitch_part(
                        part_chapter_files,
                        output_dir,
                        metadata,
                        part_index,
                        args.output_format,
                        chapter_gap_file,
                        chapter_gap_ms,
                        master_dir,
                        checkpoint,
                        checkpoint_store,
                        config,
                        provenance_runtime_metadata,
                        logger,
                        cancellation,
                    )
                    progress.complete(ProgressTracker.part_key(part_index), "Part complete")
                    part_index += 1
                    part_chapter_files = []
                continue

            print(f"\nProcessing {title}")
            if chapter.skipped_reason:
                print(f"   (Skipping {chapter.skipped_reason})")
                progress.report("Skipped chapter", chapter_index=ch_idx)
                continue

            chunks = chapter.segments
            print(f"   -> Loaded {len(chunks)} planned synthesis segments.")
            segment_files: list[str] = []

            for segment in chunks:
                if cancellation:
                    cancellation.raise_if_cancelled()
                seg_idx = segment.index
                chunk = segment.text
                if not chunk.strip():
                    continue

                progress.report(
                    "Narrating",
                    chapter_index=ch_idx,
                    segment_index=seg_idx,
                    total_segments=len(chunks),
                )

                segment_key = segment.id
                segment_artifact = checkpoint.get("artifacts", {}).get("segments", {}).get(segment_key)
                if _marked_checkpoint_artifact_is_valid(segment_artifact):
                    segment_files.append(segment_artifact["path"])
                    print(f"   -> Segment {seg_idx + 1}/{len(chunks)} resume hit [OK]")
                    progress.report(
                        "Resume segment",
                        chapter_index=ch_idx,
                        segment_index=seg_idx,
                        total_segments=len(chunks),
                    )
                    continue

                print(f"   -> Generating Segment {seg_idx + 1}/{len(chunks)}...", end="\r")
                audio_data, _audio_ext = process_segment(
                    text_segment=chunk,
                    workflow_template=workflow_template,
                    settings=settings,
                    config=config,
                    comfyui_client=comfyui_client,
                    cancellation=cancellation,
                )
                if cancellation:
                    cancellation.raise_if_cancelled()

                if audio_data and len(audio_data) > 16:
                    temp_filename = str(segment_cache_dir / f"temp_ch{ch_idx + 1}_seg{seg_idx + 1}.flac")

                    segment_title = safe_filename_component(title, fallback=f"Chapter {ch_idx + 1:03d}")
                    segment_content_id = f"{segment_title}_seg_{seg_idx + 1:03d}"

                    write_watermarked_audio_artifact(
                        audio_data=audio_data,
                        output_path=temp_filename,
                        content_id=segment_content_id,
                        logger=logger,
                    )
                    segment_files.append(temp_filename)
                    checkpoint["artifacts"]["segments"][segment_key] = {
                        **_marked_artifact_record(temp_filename),
                        "chapter_index": ch_idx,
                        "segment_index": seg_idx,
                    }
                    checkpoint["progress"]["completed_segments"].setdefault(chapter_key, [])
                    if seg_idx not in checkpoint["progress"]["completed_segments"][chapter_key]:
                        checkpoint["progress"]["completed_segments"][chapter_key].append(seg_idx)
                    checkpoint_store.save(checkpoint)
                    print(f"   -> Generated Segment {seg_idx + 1}/{len(chunks)} [OK]   ")
                    progress.complete(
                        ProgressTracker.segment_key(ch_idx, seg_idx),
                        "Narrating",
                        chapter_index=ch_idx,
                        segment_index=seg_idx,
                        total_segments=len(chunks),
                    )
                else:
                    raise ComfyUIProtocolError(
                        f"Generated Segment {seg_idx + 1}/{len(chunks)} failed: ComfyUI returned invalid audio payload."
                    )

            if not segment_files:
                print("   -> Chapter failed (no audio generated).")
                continue

            safe_title = safe_filename_component(title, fallback=f"Chapter {ch_idx + 1:03d}")
            chapter_filename = os.path.join(output_dir, f"Chapter_{ch_idx + 1:03d}_{safe_title}.{args.output_format}")
            chapter_master = master_dir / f"chapter_{ch_idx + 1:03d}.flac"

            chapter_inputs = [disclosure_file]
            if disclosure_gap_file:
                chapter_inputs.append(disclosure_gap_file)
            chapter_inputs.extend(interleave_audio_files(segment_files, segment_gap_file))

            print(f"   -> Assembling lossless chapter master for {chapter_filename}...")
            progress.report("Assembling chapter", chapter_index=ch_idx)
            if cancellation:
                cancellation.raise_if_cancelled()
            assemble_lossless_master(
                chapter_inputs,
                chapter_master,
                content_id=f"{safe_title}_chapter_master",
                watermarked_source_files=[disclosure_file, *segment_files],
                logger=logger,
            )
            checkpoint["artifacts"]["chapter_masters"][chapter_key] = _marked_artifact_record(
                chapter_master,
                title=title,
                chapter_index=ch_idx,
            )
            checkpoint_store.save(checkpoint)

            if cancellation:
                cancellation.raise_if_cancelled()
            progress.report("Encoding chapter", chapter_index=ch_idx)
            _publish_chapter_from_master(
                master_path=chapter_master,
                chapter_filename=chapter_filename,
                title=title,
                chapter_index=ch_idx,
                metadata=metadata,
                checkpoint=checkpoint,
                checkpoint_store=checkpoint_store,
                config=config,
                provenance_runtime_metadata=provenance_runtime_metadata,
                logger=logger,
                cancellation=cancellation,
            )
            progress.complete(
                ProgressTracker.chapter_key(ch_idx),
                "Chapter complete",
                chapter_index=ch_idx,
            )
            part_chapter_files.append((str(chapter_master), title, ch_idx))

            _cleanup_chapter_segment_artifacts(
                chapter_index=ch_idx,
                checkpoint=checkpoint,
                checkpoint_store=checkpoint_store,
                segment_cache_dir=segment_cache_dir,
            )

            print("   -> Chapter complete.")

            if len(part_chapter_files) >= args.chapters_per_part:
                progress.report("Assembling part")
                stitch_part(
                    part_chapter_files,
                    output_dir,
                    metadata,
                    part_index,
                    args.output_format,
                    chapter_gap_file,
                    chapter_gap_ms,
                    master_dir,
                    checkpoint,
                    checkpoint_store,
                    config,
                    provenance_runtime_metadata,
                    logger,
                    cancellation,
                )
                progress.complete(ProgressTracker.part_key(part_index), "Part complete")
                part_index += 1
                part_chapter_files = []

        if part_chapter_files:
            progress.report("Assembling part")
            stitch_part(
                part_chapter_files,
                output_dir,
                metadata,
                part_index,
                args.output_format,
                chapter_gap_file,
                chapter_gap_ms,
                master_dir,
                checkpoint,
                checkpoint_store,
                config,
                provenance_runtime_metadata,
                logger,
                cancellation,
            )
            progress.complete(ProgressTracker.part_key(part_index), "Part complete")
        checkpoint["status"] = "completed"
        checkpoint_store.save(checkpoint)
        progress.finish()
    except PipelineCancelled:
        checkpoint["status"] = "cancelled"
        checkpoint_store.save(checkpoint)
        progress.report("Canceled")
        logger.info("Pipeline canceled by user; resumable state was saved")
        raise
    except Exception as exc:
        checkpoint["status"] = "failed"
        checkpoint.setdefault("errors", []).append({"message": str(exc), "traceback": traceback.format_exc()})
        checkpoint_store.save(checkpoint)
        progress.report("Failed")
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


def _publish_chapter_from_master(
    *,
    master_path: str | Path,
    chapter_filename: str,
    title: str,
    chapter_index: int,
    metadata: BookMetadata,
    checkpoint: dict,
    checkpoint_store: CheckpointStore,
    config: AppConfig,
    provenance_runtime_metadata: ProvenanceRuntimeMetadata,
    logger: logging.Logger,
    cancellation: CancellationToken | None = None,
) -> None:
    chapter_meta = {
        "title": title,
        "artist": metadata.author,
        "album": metadata.title,
        "track": str(chapter_index + 1),
    }
    if cancellation:
        cancellation.raise_if_cancelled()
    encode_lossless_master(
        master_path,
        chapter_filename,
        metadata=chapter_meta,
        cover_image=metadata.cover_image_path,
        logger=logger,
    )
    if cancellation:
        cancellation.raise_if_cancelled()
    provenance = apply_c2pa_with_policy(
        artifact_path=chapter_filename,
        config=config.provenance,
        runtime_metadata=provenance_runtime_metadata,
        logger=logger,
    )
    if provenance:
        if provenance.final_sha256 != sha256_file(chapter_filename):
            raise PipelineRuntimeError(f"C2PA final artifact hash mismatch for {chapter_filename}.")
        refresh_ai_marking_manifest_hash(chapter_filename)
    chapter_record = _marked_artifact_record(
        chapter_filename,
        title=title,
        master_path=str(master_path),
    )
    checkpoint["artifacts"]["chapters"][str(chapter_index)] = chapter_record
    if provenance:
        checkpoint["artifacts"]["provenance"][chapter_filename] = {
            "manifest_id": provenance.manifest_id,
            "embedding_path": provenance.embedding_path,
            "source_sha256": provenance.source_sha256,
            "final_sha256": provenance.final_sha256,
        }
        logger.info("Checkpointed C2PA manifest artifact=%s manifest_id=%s", chapter_filename, provenance.manifest_id)
    if chapter_index not in checkpoint["progress"]["completed_chapters"]:
        checkpoint["progress"]["completed_chapters"].append(chapter_index)
    checkpoint_store.save(checkpoint)


def stitch_part(
    part_chapter_files: list[tuple[str, str, int]],
    output_dir: str,
    metadata: BookMetadata,
    part_index: int,
    output_format: str,
    chapter_gap_file: str | None,
    chapter_gap_ms: int,
    master_dir: Path,
    checkpoint: dict,
    checkpoint_store: CheckpointStore,
    config: AppConfig,
    provenance_runtime_metadata: ProvenanceRuntimeMetadata,
    logger: logging.Logger,
    cancellation: CancellationToken | None = None,
) -> None:
    safe_book_title = safe_filename_component(metadata.title, fallback="Untitled")
    part_filename = os.path.join(output_dir, f"{safe_book_title} - Part_{part_index:03d}.{output_format}")
    part_master = master_dir / f"part_{part_index:03d}.flac"
    part_meta = {
        "title": f"{metadata.title} - Part {part_index}",
        "artist": metadata.author,
        "album": metadata.title,
        "disc": str(part_index),
    }

    chapter_master_files = [file_path for file_path, _, _ in part_chapter_files]
    files_to_stitch = interleave_audio_files(chapter_master_files, chapter_gap_file)
    chapter_indexes = [chapter_index for _, _, chapter_index in part_chapter_files]
    chapter_markers = chapter_markers_for_files(
        [(file_path, title) for file_path, title, _ in part_chapter_files],
        gap_duration_ms=chapter_gap_ms,
    )

    print(f"   -> Stitching {len(part_chapter_files)} chapters into {part_filename}...")
    if cancellation:
        cancellation.raise_if_cancelled()
    assemble_lossless_master(
        files_to_stitch,
        part_master,
        content_id=f"{safe_book_title}_part_{part_index:03d}_master",
        watermarked_source_files=chapter_master_files,
        logger=logger,
    )
    checkpoint["artifacts"]["part_masters"][str(part_index)] = _marked_artifact_record(
        part_master,
        chapter_indexes=chapter_indexes,
    )
    checkpoint_store.save(checkpoint)

    if cancellation:
        cancellation.raise_if_cancelled()
    encode_lossless_master(
        part_master,
        part_filename,
        metadata=part_meta,
        chapter_markers=chapter_markers,
        cover_image=metadata.cover_image_path,
        logger=logger,
    )
    if cancellation:
        cancellation.raise_if_cancelled()
    provenance = apply_c2pa_with_policy(
        artifact_path=part_filename,
        config=config.provenance,
        runtime_metadata=provenance_runtime_metadata,
        logger=logger,
    )
    if provenance:
        if provenance.final_sha256 != sha256_file(part_filename):
            raise PipelineRuntimeError(f"C2PA final artifact hash mismatch for {part_filename}.")
        refresh_ai_marking_manifest_hash(part_filename)
    part_record = _marked_artifact_record(
        part_filename,
        title=f"{metadata.title} - Part {part_index}",
        chapter_indexes=chapter_indexes,
    )
    checkpoint["artifacts"]["parts"][str(part_index)] = part_record
    if provenance:
        checkpoint["artifacts"]["provenance"][part_filename] = {
            "manifest_id": provenance.manifest_id,
            "embedding_path": provenance.embedding_path,
            "source_sha256": provenance.source_sha256,
            "final_sha256": provenance.final_sha256,
        }
        logger.info("Checkpointed C2PA manifest artifact=%s manifest_id=%s", part_filename, provenance.manifest_id)
    checkpoint_store.save(checkpoint)

    remove_artifact_and_manifest(part_master)
    checkpoint["artifacts"]["part_masters"].pop(str(part_index), None)
    for chapter_index, chapter_master in zip(chapter_indexes, chapter_master_files):
        remove_artifact_and_manifest(chapter_master)
        checkpoint["artifacts"]["chapter_masters"].pop(str(chapter_index), None)
    checkpoint_store.save(checkpoint)
    print(f"   -> Part {part_index:03d} complete.")


def build_app_config(args: argparse.Namespace, project_root: Path) -> AppConfig:
    kwargs = {
        "project_root": project_root,
        "comfyui_mode": args.comfyui_mode,
        "comfyui_server_address": args.comfyui_server_address,
        "comfyui_spoof_scenario": args.comfyui_spoof_scenario,
    }
    if args.comfyui_timeout_seconds is not None:
        kwargs["comfyui_timeout_seconds"] = args.comfyui_timeout_seconds

    return AppConfig(
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


def main(argv: list[str] | None = None) -> None:
    project_root = Path(__file__).resolve().parents[2]
    parser = build_argument_parser(project_root)
    args = parser.parse_args(argv)

    if args.gui:
        from gui.app import launch_gui

        raise SystemExit(launch_gui(project_root))

    config = build_app_config(args, project_root)
    cancellation = CancellationToken()
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    def _request_cli_cancellation(_signum, _frame) -> None:
        if not cancellation.is_cancelled:
            print("\nCancellation requested; finishing the current safe operation...")
        cancellation.cancel()

    signal.signal(signal.SIGINT, _request_cli_cancellation)
    try:
        run_pipeline(args, config, cancellation=cancellation)
    except PipelineCancelled as exc:
        print(f"CANCELED: {exc}")
        raise SystemExit(exc.guidance.exit_code)
    except (
        InputValidationError,
        MetadataExtractionError,
        ResumeStateError,
        AudioStitchError,
        ComfyUIConnectionError,
        ComfyUIProtocolError,
        PipelineRuntimeError,
    ) as exc:
        print(f"ERROR: [{exc.guidance.code}] {exc}")
        print(f"REMEDIATION: {exc.remediation}")
        raise SystemExit(exc.guidance.exit_code)
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
