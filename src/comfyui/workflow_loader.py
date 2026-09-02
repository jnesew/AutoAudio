from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.config import GenerationSettings


QWEN_CUSTOM_VOICE_NODE = "FB_Qwen3TTSCustomVoice"
QWEN_VOICE_DESIGN_NODE = "FB_Qwen3TTSVoiceDesign"
QWEN_NODE_MODES = {
    QWEN_CUSTOM_VOICE_NODE: "preset",
    QWEN_VOICE_DESIGN_NODE: "design",
}


class WorkflowValidationError(ValueError):
    """Raised when a workflow is not a supported, non-cloning Qwen TTS graph."""


def load_workflow_template(workflow_path: Path) -> dict[str, Any]:
    with workflow_path.open("r", encoding="utf-8") as file:
        workflow = json.load(file)
    if not isinstance(workflow, dict):
        raise WorkflowValidationError("ComfyUI workflow root must be a JSON object.")
    return workflow


def find_qwen_generation_node(workflow: dict[str, Any]) -> tuple[str, str]:
    clone_nodes: list[str] = []
    generation_nodes: list[tuple[str, str]] = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        inputs = node.get("inputs", {})
        input_names = {str(name).lower() for name in inputs} if isinstance(inputs, dict) else set()
        has_reference_audio = bool(input_names & {"reference_audio", "ref_audio", "voice_reference", "voice_audio"})
        if "clone" in class_type.lower() or has_reference_audio:
            clone_nodes.append(str(node_id))
        mode = QWEN_NODE_MODES.get(class_type)
        if mode:
            generation_nodes.append((str(node_id), mode))

    if clone_nodes:
        raise WorkflowValidationError(f"Voice-cloning nodes are prohibited in v2 workflows: {', '.join(clone_nodes)}")
    if len(generation_nodes) != 1:
        raise WorkflowValidationError(
            "Qwen workflow must contain exactly one FB_Qwen3TTSCustomVoice or FB_Qwen3TTSVoiceDesign node."
        )
    return generation_nodes[0]


def build_runtime_workflow(
    *,
    workflow_template: dict[str, Any],
    text_segment: str,
    settings: GenerationSettings,
) -> dict[str, Any]:
    workflow = copy.deepcopy(workflow_template)
    node_id, workflow_mode = find_qwen_generation_node(workflow)
    if workflow_mode != settings.voice_mode:
        raise WorkflowValidationError(
            f"Qwen workflow mode {workflow_mode!r} does not match requested mode {settings.voice_mode!r}."
        )

    node = workflow[node_id]
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise WorkflowValidationError(f"Qwen generation node {node_id} has no inputs object.")

    inputs.update(
        {
            "text": text_segment,
            "instruct": settings.instruct,
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
        }
    )
    if workflow_mode == "preset":
        inputs["speaker"] = settings.speaker
    else:
        inputs.pop("speaker", None)

    return workflow
