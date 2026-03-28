from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.config import GenerationSettings


def load_workflow_template(workflow_path: Path) -> dict[str, Any]:
    with workflow_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def build_runtime_workflow(
    *,
    workflow_template: dict[str, Any],
    text_segment: str,
    reference_voice: str,
    settings: GenerationSettings,
) -> dict[str, Any]:
    workflow = copy.deepcopy(workflow_template)
    workflow["15"]["inputs"]["audio"] = reference_voice

    generation_inputs = workflow["44"]["inputs"]
    generation_inputs["text"] = text_segment
    generation_inputs["max_words_per_chunk"] = settings.max_words_per_chunk
    generation_inputs["diffusion_steps"] = settings.diffusion_steps
    generation_inputs["temperature"] = settings.temperature
    generation_inputs["top_p"] = settings.top_p
    generation_inputs["cfg_scale"] = settings.cfg_scale
    generation_inputs["free_memory_after_generate"] = settings.free_memory_after_generate

    return workflow
