from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from comfyui.workflow_loader import WorkflowValidationError, build_runtime_workflow, find_qwen_generation_node, load_workflow_template
from core.config import GenerationSettings


def test_custom_voice_workflow_receives_all_runtime_settings():
    template = load_workflow_template(PROJECT_ROOT / "resources" / "workflows" / "qwen3_tts_custom_voice.json")
    original = copy.deepcopy(template)
    settings = GenerationSettings(
        voice_mode="preset",
        speaker="Ryan",
        instruct="Calm, measured delivery.",
        language="English",
        seed=42,
        top_p=0.7,
        top_k=30,
        temperature=0.9,
        attention="flash_attn",
    )

    runtime = build_runtime_workflow(workflow_template=template, text_segment="Chapter text.", settings=settings)
    inputs = runtime["39"]["inputs"]

    assert inputs["text"] == "Chapter text."
    assert inputs["speaker"] == "Ryan"
    assert inputs["instruct"] == "Calm, measured delivery."
    assert inputs["seed"] == 42
    assert inputs["top_p"] == 0.7
    assert inputs["top_k"] == 30
    assert inputs["temperature"] == 0.9
    assert inputs["attention"] == "flash_attn"
    assert template == original


def test_voice_design_workflow_uses_instruction_without_speaker():
    template = load_workflow_template(PROJECT_ROOT / "resources" / "workflows" / "qwen3_tts_voice_design.json")
    settings = GenerationSettings(
        voice_mode="design",
        instruct="Warm adult narrator with a steady low register and restrained emotion.",
    )

    runtime = build_runtime_workflow(workflow_template=template, text_segment="Chapter text.", settings=settings)
    inputs = runtime["38"]["inputs"]

    assert inputs["instruct"] == settings.instruct
    assert "speaker" not in inputs


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("qwen3_tts_custom_voice.json", ("39", "preset")),
        ("qwen3_tts_voice_design.json", ("38", "design")),
    ],
)
def test_bundled_qwen_workflows_are_non_cloning_and_have_one_generator(filename, expected):
    workflow = load_workflow_template(PROJECT_ROOT / "resources" / "workflows" / filename)

    assert find_qwen_generation_node(workflow) == expected


def test_runtime_builder_rejects_mode_mismatch():
    template = load_workflow_template(PROJECT_ROOT / "resources" / "workflows" / "qwen3_tts_custom_voice.json")
    settings = GenerationSettings(
        voice_mode="design",
        instruct="A clear, mature documentary narrator.",
    )

    with pytest.raises(WorkflowValidationError, match="does not match"):
        build_runtime_workflow(workflow_template=template, text_segment="Text.", settings=settings)


def test_runtime_builder_rejects_any_clone_node():
    workflow = {
        "1": {"class_type": "FB_Qwen3TTSCustomVoice", "inputs": {}},
        "2": {"class_type": "FB_Qwen3TTSVoiceClone", "inputs": {"reference_audio": "voice.wav"}},
    }

    with pytest.raises(WorkflowValidationError, match="prohibited"):
        build_runtime_workflow(workflow_template=workflow, text_segment="Text.", settings=GenerationSettings())


def test_runtime_builder_rejects_reference_audio_inputs_even_without_clone_class_name():
    workflow = {
        "1": {
            "class_type": "FB_Qwen3TTSCustomVoice",
            "inputs": {"reference_audio": "voice.wav"},
        }
    }

    with pytest.raises(WorkflowValidationError, match="prohibited"):
        build_runtime_workflow(workflow_template=workflow, text_segment="Text.", settings=GenerationSettings())


def test_voice_design_requires_a_description():
    with pytest.raises(ValueError, match="requires"):
        GenerationSettings(voice_mode="design", instruct="")
