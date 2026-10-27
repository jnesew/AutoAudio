from pathlib import Path

import pytest

from core.cancellation import CancellationToken
from core.checkpoint import CheckpointStore
from core.errors import PipelineCancelled, ResumeStateError
from core.narration_selection import apply_chapter_choices, selection_payload
from core.narrator import NarratorCatalog
from core.pipeline import build_app_config, build_argument_parser, run_pipeline
from core.plan import BookPlanStore
from gui.preview import bounded_sample, build_preview

ROOT = Path(__file__).resolve().parents[2]


def args_for(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text(
        "II arrives in the first long paragraph.\n\nII leaves in the second long paragraph."
    )
    return build_argument_parser(ROOT).parse_args(
        [
            "--input-book",
            str(source),
            "--output-dir",
            str(tmp_path / "output"),
            "--target-words-per-chapter",
            "1",
            "--min-paragraphs-per-chapter",
            "1",
            "--comfyui-mode",
            "spoof",
            "--replacement-rule",
            "II=two",
            "--exclude-chapter",
            "2",
            "--chapter-text-override",
            '{"chapter": 1, "text": "II remains literal."}',
        ]
    )


def test_preview_and_checkpoint_freeze_identical_choices(tmp_path, monkeypatch):
    args = args_for(tmp_path)
    config = build_app_config(args, ROOT)
    profile = NarratorCatalog.load(config.narrator_profiles_path).get(
        args.narrator_profile
    )
    preview = build_preview(args, profile)
    token = CancellationToken()
    token.cancel()
    network = lambda *a, **k: pytest.fail("Planning must stay offline")
    monkeypatch.setattr("urllib.request.urlopen", network)
    with pytest.raises(PipelineCancelled):
        run_pipeline(args, config, token)
    store = BookPlanStore(config.state_dir_for(args.output_dir))
    plan = store.load()
    assert [chapter.to_dict() for chapter in plan.chapters] == [
        chapter.to_dict() for chapter in preview.plan.chapters
    ]
    assert plan.chapters[0].segments[0].text == "II remains literal."
    assert not plan.chapters[1].segments
    assert plan.chapters[1].skipped_reason == "Excluded by user"
    checkpoint = CheckpointStore(config.state_dir_for(args.output_dir)).load()
    assert checkpoint["ui_state"]["excluded_chapters"] == [2]
    assert (
        checkpoint["ui_state"]["chapter_text_overrides"] == args.chapter_text_overrides
    )
    before = store.path.read_bytes()
    args.chapter_text_overrides[0]["text"] = "Different spoken text."
    args.resume = "auto"
    with pytest.raises(ResumeStateError):
        run_pipeline(args, config, token)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "overrides,excluded",
    [
        ([{"chapter": 0, "text": "bad"}], []),
        ([{"chapter": 1, "text": ""}], []),
        ([{"chapter": 1, "text": "a"}, {"chapter": 1, "text": "b"}], []),
        ([], [True]),
        ([], [-1]),
    ],
)
def test_invalid_chapter_choices(overrides, excluded):
    from argparse import Namespace

    with pytest.raises(ValueError):
        selection_payload(
            Namespace(chapter_text_overrides=overrides, excluded_chapters=excluded)
        )


def test_out_of_range_and_all_excluded_rejected():
    for payload in ({"excluded_chapters": [2]}, {"excluded_chapters": [1]}):
        with pytest.raises(ValueError):
            apply_chapter_choices([("chapter", "speech")], payload)


def test_empty_selection_does_not_change_legacy_settings_identity():
    from argparse import Namespace

    assert selection_payload(Namespace()) == {}


@pytest.mark.parametrize("text", ["word " * 500, "天地玄黄" * 300, "x" * 4000])
def test_sample_always_bounded(text):
    result = bounded_sample(text)
    assert 0 < len(result) <= 180
    assert len(result.split()) <= 50
