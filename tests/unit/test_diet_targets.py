from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from nutripilot.application.diet_service import DietService
from nutripilot.domain.diet import DietError, NutritionGoals
from nutripilot.domain.diet_records import DietChangePreview, target_on
from nutripilot.infrastructure.diet_store import MemoryDietStore
from nutripilot.tools.local.diet_models import CorrectTarget, SetTarget, StopTarget

NOW = datetime(2026, 11, 12, 4, tzinfo=UTC)
EVIDENCE = [
    {
        "field": "goal",
        "source_kind": "user_form",
        "source_ref": "fixture-form",
        "original_text": "synthetic goal",
        "operation": "set",
        "status": "explicit",
    }
]


def target_request(local_date: str, **goals: object) -> SetTarget:
    return SetTarget.model_validate(
        {
            "action": "set",
            "effective_date": local_date,
            "goals": goals,
            "evidence": EVIDENCE,
        }
    )


async def prepare(
    service: DietService,
    request: SetTarget | CorrectTarget | StopTarget,
    key: str,
) -> DietChangePreview:
    return await service.prepare_target(
        1,
        request,
        idempotency_key=key,
        source="user_form",
        source_ref="fixture-form",
    )


async def save(service: DietService, preview: DietChangePreview) -> None:
    await service.confirm(
        1,
        preview.id,
        version=preview.version,
        approved_hash=preview.content_hash,
    )


async def test_target_timeline_merges_fields_and_preserves_future_points() -> None:
    store = MemoryDietStore()
    service = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    first = await prepare(
        service,
        target_request(
            "2026-11-12",
            calories_kcal={"lower": "100", "upper": "200"},
        ),
        "first",
    )
    assert await service.targets(1) == []
    await save(service, first)
    future = await prepare(
        service,
        target_request(
            "2026-11-20",
            calories_kcal={"lower": "200", "upper": "300"},
        ),
        "future",
    )
    await save(service, future)
    middle = await prepare(
        service,
        target_request(
            "2026-11-15",
            protein_g={"lower": "10", "upper": "20"},
        ),
        "middle",
    )
    await save(service, middle)
    periods = await service.targets(1)
    assert [period.target.effective_date for period in periods] == [
        date(2026, 11, 12),
        date(2026, 11, 15),
        date(2026, 11, 20),
    ]
    assert periods[0].end_date_exclusive == date(2026, 11, 15)
    assert periods[1].target.goals.calories_kcal == periods[0].target.goals.calories_kcal
    assert periods[1].target.goals.protein_g is not None
    assert periods[2].target.goals.protein_g is None
    assert target_on([period.target for period in periods], date(2026, 11, 11)) is None


async def test_stop_is_a_new_point_and_does_not_erase_history() -> None:
    store = MemoryDietStore()
    service = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    first = await prepare(
        service,
        target_request(
            "2026-11-12",
            protein_g={"lower": "10", "upper": "20"},
        ),
        "first",
    )
    await save(service, first)
    selected = (await service.targets(1))[0].target
    stop = await prepare(
        service,
        StopTarget.model_validate(
            {
                "action": "stop",
                "target_id": str(selected.id),
                "base_version": selected.version,
                "effective_date": "2026-11-15",
                "evidence": EVIDENCE,
            }
        ),
        "stop",
    )
    await save(service, stop)
    periods = await service.targets(1)
    assert len(periods) == 2
    assert periods[0].target == selected
    assert periods[1].target.goals == NutritionGoals()


async def test_two_old_target_previews_cannot_overwrite_each_other() -> None:
    store = MemoryDietStore()
    service = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    first = await prepare(
        service,
        target_request(
            "2026-11-12",
            protein_g={"lower": "10", "upper": "20"},
        ),
        "first",
    )
    stale = await prepare(
        service,
        target_request(
            "2026-11-15",
            protein_g={"lower": "20", "upper": "30"},
        ),
        "stale",
    )
    await save(service, first)
    with pytest.raises(DietError) as exc:
        await save(service, stale)
    assert exc.value.code == "VERSION_CONFLICT"
    assert len(await service.targets(1)) == 1


async def test_clear_is_distinct_from_omitted_field_and_same_day_updates_version() -> None:
    store = MemoryDietStore()
    service = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    first = await prepare(
        service,
        target_request(
            "2026-11-12",
            calories_kcal={"lower": "100", "upper": "200"},
            protein_g={"lower": "10", "upper": "20"},
        ),
        "first",
    )
    await save(service, first)
    clear = await prepare(service, target_request("2026-11-12", protein_g=None), "clear")
    await save(service, clear)
    periods = await service.targets(1)
    assert len(periods) == 1
    assert periods[0].target.id == first.content.after.id
    assert periods[0].target.version == 2
    assert periods[0].target.goals.protein_g is None
    assert periods[0].target.goals.calories_kcal is not None


async def test_target_preview_retry_is_bound_to_content_and_owner() -> None:
    store = MemoryDietStore()
    service = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    change = target_request("2026-11-12", protein_g={"lower": "10", "upper": "20"})
    preview = await prepare(service, change, "first")
    assert (await prepare(service, change, "first")).id == preview.id
    with pytest.raises(DietError) as exc:
        await prepare(
            service,
            target_request(
                "2026-11-13",
                protein_g={"lower": "10", "upper": "20"},
            ),
            "first",
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(DietError) as exc:
        await service.confirm(2, preview.id, version=1, approved_hash=preview.content_hash)
    assert exc.value.code == "NOT_FOUND"


async def test_same_effective_values_do_not_create_a_new_date_point() -> None:
    service = DietService(MemoryDietStore(), default_timezone="Asia/Shanghai", clock=lambda: NOW)
    first = await prepare(
        service,
        target_request("2026-11-12", protein_g={"lower": "10", "upper": "20"}),
        "first",
    )
    await save(service, first)
    with pytest.raises(DietError) as exc:
        await prepare(
            service,
            target_request("2026-11-13", protein_g={"lower": "10", "upper": "20"}),
            "unchanged",
        )
    assert exc.value.code == "NO_CHANGE"
    assert len(await service.targets(1)) == 1
