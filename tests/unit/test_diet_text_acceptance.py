import json
from pathlib import Path

from scripts.verify_diet_text import assess
from tests.integration.test_diet_jobs import parsed

from nutripilot.domain.diet_jobs import ParsedIntake


def test_frozen_text_expectations_are_unique_and_cover_sixty_inputs() -> None:
    dataset = json.loads(Path("data/eval_diet_text_20261112.json").read_text())
    assert len(dataset["cases"]) == len({row["id"] for row in dataset["cases"]}) == 60
    assert sum(row.get("reject_actual", False) for row in dataset["cases"]) == 10


def test_acceptance_rejects_false_actuality_missing_food_and_invented_grams() -> None:
    output = ParsedIntake.model_validate(parsed())
    case = {
        "food": "米饭",
        "actuality": "actual",
        "subject": "self",
        "date": "2026-11-12",
        "meal": "lunch",
    }
    assert assess(case, output)["correct"]
    assert not assess({**case, "food": "鸡蛋"}, output)["correct"]
    assert not assess({**case, "grams": "100"}, output)["correct"]
    assert assess({"reject_actual": True}, output)["unsafe_actual"]
    assert assess({**case, "subject": "third_party"}, output)["unsafe_actual"]


def test_model_empty_note_is_normalized_without_weakening_write_schema() -> None:
    import pytest
    from pydantic import ValidationError

    from nutripilot.domain.diet import DraftEntry

    result = parsed()
    result["entries"][0]["items"][0]["note"] = None
    assert ParsedIntake.model_validate(result).entries[0].items[0].note is None
    with pytest.raises(ValidationError, match="Omit optional"):
        DraftEntry.model_validate(result["entries"][0])


def test_model_internal_key_normalization_preserves_facts_and_strict_write_schema() -> None:
    import pytest
    from pydantic import ValidationError

    from nutripilot.domain.diet import DraftEntry
    from nutripilot.domain.diet_jobs import ParsedImageIntake

    result = parsed()
    result["entries"][0]["items"][0]["client_item_key"] = "鸡胸肉"
    output = ParsedIntake.model_validate(result)
    assert output.entries[0].items[0].client_item_key == "model_item_0"
    assert (
        output.entries[0].items[0].food
        == ParsedIntake.model_validate(parsed()).entries[0].items[0].food
    )
    with pytest.raises(ValidationError):
        DraftEntry.model_validate(result["entries"][0])
    image = json.loads(Path("docs/specs/diet-image-estimate.example.json").read_text())
    key = image["entries"][0]["items"][0]["client_item_key"]
    image["entries"][0]["items"][0]["client_item_key"] = "餐照食物"
    for estimate in image["estimates"]:
        if estimate["entry_index"] == 0 and estimate["client_item_key"] == key:
            estimate["client_item_key"] = "餐照食物"
    normalized = ParsedImageIntake.model_validate(image)
    assert normalized.estimates[0].client_item_key == normalized.entries[0].items[0].client_item_key
    result["entries"][0]["items"].append(dict(result["entries"][0]["items"][0]))
    with pytest.raises(ValidationError, match="unambiguous"):
        ParsedIntake.model_validate(result)


def test_frozen_image_references_and_main_food_match_do_not_require_exact_dish_name() -> None:
    from scripts.verify_diet_images import main_food_hit

    dataset = json.loads(Path("data/eval_diet_images_20261112.json").read_text())
    assert len(dataset["cases"]) == len({case["id"] for case in dataset["cases"]}) == 30
    case = dataset["cases"][5]
    assert main_food_hit(case, ["炒鸡肉（做法不确定）"])
    assert not main_food_hit(case, ["米饭"])


def test_calendar_hint_uses_local_anchor_and_blind_set_is_frozen() -> None:
    from datetime import datetime

    from nutripilot.domain.diet import SubmissionAnchor

    anchor = SubmissionAnchor.capture(
        datetime.fromisoformat("2026-11-12T18:00:00+00:00"), "America/Denver"
    )
    assert anchor.calendar_hint() == {
        "today_weekday": 4,
        "this_week_monday": "2026-11-09",
        "next_week_monday": "2026-11-16",
    }
    dataset = json.loads(Path("data/eval_diet_text_blind_20261112.json").read_text())
    assert len(dataset["cases"]) == len({case["id"] for case in dataset["cases"]}) == 24


def test_parser_submission_clock_is_not_an_eating_time():
    from datetime import time

    from nutripilot.application.diet_parsing import source_clock

    entry = ParsedIntake.model_validate(parsed()).entries[0]
    entry = entry.model_copy(
        update={
            "temporal": entry.temporal.model_copy(
                update={
                    "local_time": time(13, 32),
                    "utc_offset_minutes": -420,
                }
            )
        }
    )
    for text in ("这是我今天午餐吃的", "我午餐吃了150克米饭"):
        temporal = source_clock(entry, text).temporal
        assert temporal.local_time is None and temporal.utc_offset_minutes is None
    for text in ("我13:32吃的午餐", "我下午一点吃的午餐"):
        assert source_clock(entry, text).temporal.local_time == time(13, 32)
