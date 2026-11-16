"""Assertions read the authoritative preview, never private checkpoint tool arguments."""

from typing import Any
from uuid import UUID

from nutripilot.agent.nodes.diet import confirmation_id
from nutripilot.agent.state import AgentState
from nutripilot.application.container import ApplicationContainer
from nutripilot.domain.diet_records import DietChangePreview, PlanChangeContent


def approval(state: AgentState) -> dict[str, Any]:
    ref = state["pending_diet_preview_ref"]
    assert ref is not None
    return {
        "confirmation_id": confirmation_id(ref),
        "preview_version": ref["version"],
        "content_hash": ref["hash"],
    }


async def plan_preview(container: ApplicationContainer, state: AgentState) -> DietChangePreview:
    assert state["pending_tool_calls"] == []
    ref = state["pending_diet_preview_ref"]
    assert ref is not None
    preview = await container.diet.preview(state["user_id"], UUID(ref["id"]))
    assert isinstance(preview, DietChangePreview)
    assert isinstance(preview.content, PlanChangeContent)
    assert preview.thread_id == state["thread_id"]
    return preview


async def plan_items(container: ApplicationContainer, state: AgentState) -> list[dict[str, Any]]:
    preview = await plan_preview(container, state)
    assert isinstance(preview.content, PlanChangeContent)
    return [
        {
            "meal_id": item.food_ref.id,
            "meal_date": meal.local_date.isoformat(),
            "meal_type": meal.meal_type,
            "item": item,
        }
        for meal in preview.content.after_meals
        for item in meal.items
        if item.food_ref.kind == "recipe"
    ]
