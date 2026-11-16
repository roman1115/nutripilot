from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nutripilot.application.diet_queries import DietQueryService
from nutripilot.application.intake_service import IntakeService
from nutripilot.domain.diet import DietError, SubmissionAnchor
from nutripilot.domain.diet_records import ResourceRef
from nutripilot.infrastructure.diet_foods import CatalogDietFoodResolver
from nutripilot.infrastructure.diet_store import MemoryDietStore
from nutripilot.retrieval.catalog import FoodCatalog
from nutripilot.tools.local.diet_models import CreateIntake, GetDietLogs, SearchFoods

NOW = datetime(2026, 11, 12, 4, tzinfo=UTC)


@pytest.mark.parametrize("granularity", ["day", "week"])
async def test_chat_summary_keeps_unknown_totals_and_week_denominators(granularity):
    from nutripilot.agent.nodes.diet_tools import DietToolsNode
    from nutripilot.tools.local.diet_models import GetNutritionSummary

    store = MemoryDietStore()
    foods = CatalogDietFoodResolver(FoodCatalog(sources=[], ingredients=[], recipes=[]))
    queries = DietQueryService(
        store, foods, default_timezone="Asia/Shanghai", cursor_secret=b"x" * 32, clock=lambda: NOW
    )
    page = await queries.summary(1, GetNutritionSummary.model_validate({
        "scope": {"kind": "range", "start_date": "2026-11-09", "end_date": "2026-11-15"},
        "granularity": granularity,
    }))
    rendered = DietToolsNode._render([{"data": page.model_dump(mode="json")}])
    assert "热量" in rendered and "calories_kcal" not in rendered
    assert "{'lower'" not in rendered
    if granularity == "day":
        assert "总量未知" in rendered and "尚未声明完整" in rendered
    else:
        assert "已记录0天" in rendered and "七日均未知" in rendered


async def test_signed_pagination_rejects_tampering_owner_filter_and_epoch_changes() -> None:
    store = MemoryDietStore()
    foods = CatalogDietFoodResolver(FoodCatalog(sources=[], ingredients=[], recipes=[]))
    intake = IntakeService(store, foods, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    queries = DietQueryService(
        store, foods, default_timezone="Asia/Shanghai", cursor_secret=b"x" * 32, clock=lambda: NOW
    )
    for index in range(3):
        request = CreateIntake.model_validate(
            {
                "action": "create",
                "entry": {
                    "actuality": "actual",
                    "subject": "self",
                    "temporal": {"local_date": "2026-11-12", "meal_type": "lunch"},
                    "items": [
                        {
                            "client_item_key": "food",
                            "food": {"kind": "unknown", "name": f"food{index}"},
                            "quantity": {"kind": "unknown", "description": "unknown"},
                        }
                    ],
                },
                "evidence": [
                    {
                        "field": "food",
                        "source_kind": "user_form",
                        "source_ref": "fixture",
                        "original_text": "fixture",
                        "operation": "set",
                        "status": "explicit",
                    }
                ],
            }
        )
        draft = await intake.prepare(
            1,
            request,
            idempotency_key=str(index),
            anchor=SubmissionAnchor.capture(NOW, "Asia/Shanghai"),
        )
        await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    request = GetDietLogs.model_validate(
        {
            "scope": {"kind": "range", "start_date": "2026-11-12", "end_date": "2026-11-12"},
            "limit": 1,
        }
    )
    first = await queries.logs(1, request)
    assert first.truncated and first.next_cursor is not None and len(first.items) == 1
    next_request = GetDietLogs.model_validate({**request.model_dump(), "cursor": first.next_cursor})
    second = await queries.logs(1, next_request)
    assert first.items != second.items and second.diet_epoch == first.diet_epoch
    for owner, cursor_request in (
        (2, next_request),
        (1, GetDietLogs.model_validate({**next_request.model_dump(), "food_query": "food"})),
        (
            1,
            GetDietLogs.model_validate({**request.model_dump(), "cursor": first.next_cursor + "x"}),
        ),
    ):
        with pytest.raises(DietError) as exc:
            await queries.logs(owner, cursor_request)
        assert exc.value.code == "INVALID_CURSOR"
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        await tx.advance_epoch()
    with pytest.raises(DietError) as exc:
        await queries.logs(1, next_request)
    assert exc.value.code == "DATA_CHANGED"
    with pytest.raises(DietError) as exc:
        await queries.logs(
            2,
            GetDietLogs.model_validate(
                {
                    "scope": {"kind": "id", "id": str(first.items[0].id)},
                }
            ),
        )
    assert exc.value.code == "NOT_FOUND"
    assert (await queries.search(1, SearchFoods(query="missing"))).items == []


def test_uuid_shaped_legacy_plan_ids_preserve_exact_text() -> None:
    legacy_id = uuid4().hex.upper()
    ref = ResourceRef(kind="plan", id=legacy_id, version=1)
    assert ref.id == legacy_id
    assert ResourceRef.model_validate_json(ref.model_dump_json()).id == legacy_id
