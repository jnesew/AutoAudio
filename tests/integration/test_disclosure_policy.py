"""Check provider configuration and persisted resume identity without synthesis."""
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest

from core.checkpoint import CheckpointStore
from core.errors import PipelineCancelled
from core.pipeline import (
    CHAPTER_DISCLOSURE_POLICY_VERSION, LEGACY_CHAPTER_DISCLOSURE_POLICY_VERSION,
    build_argument_parser, build_app_config, run_pipeline,
)

ROOT = Path(__file__).resolve().parents[2]


def args_for(tmp_path, profile="preset-aiden-neutral", provider="comfyui"):
    source = tmp_path / "short.txt"
    source.write_text("The travelers reached the river early and carefully prepared their boat for a quiet journey through the green valley.")
    options = ["--input-book", str(source), "--output-dir", str(tmp_path / "output"),
               "--narrator-profile", profile, "--comfyui-mode", "spoof", "--seed", "42"]
    if provider != "comfyui":
        options += ["--tts-provider", provider, "--tts-model", "speech-model", "--tts-voice", "reader-id"]
    return build_argument_parser(ROOT).parse_args(options)


@pytest.mark.parametrize("profile,speaker,mode", [
    ("preset-aiden-neutral", "Aiden", "preset"),
    ("preset-ryan-neutral", "Ryan", "preset"),
    ("design-warm-narrator", "Eric", "design"),
])
def test_new_comfy_disclosure_follows_narration_voice_and_workflow(tmp_path, profile, speaker, mode):
    args = args_for(tmp_path, profile)
    captured = []

    def stop(**kwargs):
        captured.append(kwargs["speech_provider"])
        raise PipelineCancelled("Stopped before synthesis")

    with patch("core.pipeline.ensure_disclosure_asset", side_effect=stop):
        with pytest.raises(PipelineCancelled):
            run_pipeline(args, build_app_config(args, ROOT))
    provider = captured[0]
    narration, disclosure = provider._narration_settings, provider._disclosure_settings
    assert disclosure.speaker == narration.speaker == speaker
    assert disclosure.voice_mode == narration.voice_mode == mode
    assert disclosure.seed == narration.seed == 42
    assert provider._disclosure_workflow is provider._narration_workflow
    if mode == "design":
        assert disclosure.instruct == narration.instruct
    else:
        assert "neutral" in disclosure.instruct
    with patch.object(provider._client, "generate_audio", return_value=SimpleNamespace(content=b"mock speech", extension=".wav")) as generate:
        provider.generate_audio(text_segment="A disclosure.", purpose="disclosure")
        provider.generate_audio(text_segment="Narration.", purpose="narration")
    assert generate.call_args_list[0].kwargs["settings"] == disclosure
    assert generate.call_args_list[1].kwargs["settings"] == narration
    assert generate.call_args_list[0].kwargs["workflow_template"] == generate.call_args_list[1].kwargs["workflow_template"]
    checkpoint = CheckpointStore(Path(args.output_dir) / ".autoaudio_state").load()
    assert checkpoint["disclosure_policy_version"] == CHAPTER_DISCLOSURE_POLICY_VERSION


@pytest.mark.parametrize("legacy", [False, True])
def test_resume_preserves_current_or_legacy_policy_and_frozen_plan(tmp_path, legacy):
    args = args_for(tmp_path)
    stop = Mock(side_effect=PipelineCancelled("Stopped before synthesis"))
    with patch("core.pipeline.ensure_disclosure_asset", stop):
        if legacy:
            with patch("core.pipeline._disclosure_policy_for_checkpoint", return_value=LEGACY_CHAPTER_DISCLOSURE_POLICY_VERSION):
                with pytest.raises(PipelineCancelled):
                    run_pipeline(args, build_app_config(args, ROOT))
        else:
            with pytest.raises(PipelineCancelled):
                run_pipeline(args, build_app_config(args, ROOT))
    state_dir = Path(args.output_dir) / ".autoaudio_state"
    store = CheckpointStore(state_dir)
    checkpoint = store.load()
    compatibility = checkpoint["compatibility"].copy()
    plan = (state_dir / "book_plan.json").read_bytes()
    if legacy:
        checkpoint.pop("disclosure_policy_version")  # Older runs had no top-level policy, even before first disclosure.
        store.save(checkpoint)
    args.resume = "yes"
    stop.reset_mock()
    with patch("core.pipeline.ensure_disclosure_asset", stop):
        with pytest.raises(PipelineCancelled):
            run_pipeline(args, build_app_config(args, ROOT))
    provider = stop.call_args.kwargs["speech_provider"]
    assert provider._disclosure_settings.speaker == ("Eric" if legacy else "Aiden")
    assert provider._disclosure_settings.seed == (268583702137267 if legacy else 42)
    assert store.load()["compatibility"] == compatibility
    assert (state_dir / "book_plan.json").read_bytes() == plan


@pytest.mark.parametrize("provider", ["openai-compatible", "elevenlabs"])
def test_http_disclosure_setup_never_constructs_comfy_or_loads_its_graphs(tmp_path, provider):
    args = args_for(tmp_path, provider=provider)
    stop = Mock(side_effect=PipelineCancelled("Stopped before synthesis"))
    with patch("core.pipeline.load_workflow_template", side_effect=AssertionError("No ComfyUI workflows")), \
         patch("tts.router.RealComfyUIClient", side_effect=AssertionError("No ComfyUI client")), \
         patch("core.pipeline.ensure_disclosure_asset", stop):
        with pytest.raises(PipelineCancelled):
            run_pipeline(args, build_app_config(args, ROOT))
    selected = stop.call_args.kwargs["speech_provider"]
    assert selected.identity.provider_id == provider
    assert selected.config.model == "speech-model"
    assert selected.config.voice == "reader-id"
