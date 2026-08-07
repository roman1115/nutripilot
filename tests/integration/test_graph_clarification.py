import pytest

from nutripilot.application import build_container
from nutripilot.domain.enums import Phase


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_clarifies_one_slot_then_keeps_thread_state() -> None:
    container = build_container()

    first = await container.chat_service.chat(
        user_id=1,
        message="给我推荐点吃的",
        thread_id="clarify-flow",
    )
    assert first["phase"] == Phase.CLARIFY.value
    assert first["missing_slots"] == ["meal_type"]

    second = await container.chat_service.chat(
        user_id=1,
        message="晚餐, 清淡一些",
        thread_id="clarify-flow",
    )
    assert second["phase"] == Phase.COMPLETED.value
    assert second["slots"]["meal_type"] == "dinner"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_blocks_medical_boundary_before_model_call() -> None:
    container = build_container()

    state = await container.chat_service.chat(
        user_id=1,
        message="请给我糖尿病诊断和药物剂量",
        thread_id="risk-flow",
    )

    assert state["phase"] == Phase.BLOCKED.value
    assert state["intent"] is None
    assert "MEDICAL_BOUNDARY" in state["risk_flags"]
