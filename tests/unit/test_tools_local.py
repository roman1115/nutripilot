from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nutripilot.infrastructure import StaticWeatherProvider, load_seed_store
from nutripilot.tools import ToolCall, ToolContext, ToolExecutor, ToolRegistry
from nutripilot.tools.local import register_local_tools

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@pytest.mark.asyncio
async def test_local_profile_and_plan_tools_round_trip() -> None:
    store = load_seed_store(DATA_DIR)
    registry = register_local_tools(ToolRegistry(), store, StaticWeatherProvider())
    executor = ToolExecutor(registry)

    profile = await executor.execute(
        ToolCall(call_id="profile", name="get_user_profile", arguments={}),
        ToolContext(user_id=1, permissions=frozenset({"profile:read"})),
    )
    assert profile.ok
    assert "花生" in profile.data["allergens"]

    plan_call = ToolCall(
        call_id="plan",
        name="create_meal_plan",
        arguments={
            "title": "明日计划",
            "start_date": date.today().isoformat(),
            "items": [
                {
                    "meal_date": date.today().isoformat(),
                    "meal_type": "lunch",
                    "meal_id": 2,
                }
            ],
        },
    )
    context = ToolContext(
        user_id=1,
        permissions=frozenset({"meal_plan:write"}),
        confirmed=True,
        idempotency_key="create-plan-1",
    )
    created = await executor.execute(plan_call, context)
    repeated = await executor.execute(plan_call.model_copy(update={"call_id": "plan-2"}), context)

    assert created.ok
    assert repeated.ok and repeated.cached
    assert created.data["id"] == repeated.data["id"]
    assert len(await store.list_plans(1)) == 1
