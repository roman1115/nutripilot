from nutripilot.agent.state import new_agent_state
from nutripilot.domain.enums import Phase
from nutripilot.memory.context import merge_slots


def test_new_agent_state_has_explicit_serializable_defaults() -> None:
    state = new_agent_state(message="  推荐晚餐  ", user_id=7, thread_id="thread-1")

    assert state["thread_id"] == "thread-1"
    assert state["user_message"] == "推荐晚餐"
    assert state["phase"] == Phase.RECEIVED.value
    assert state["slots"] == {}
    assert state["tool_results"] == []
    assert state["requires_confirmation"] is False


def test_merge_slots_preserves_existing_and_deduplicates_lists() -> None:
    merged = merge_slots(
        {"meal_type": "dinner", "allergens": ["花生"]},
        {"allergens": ["花生", "牛奶"], "tastes": ["清淡"], "empty": None},
    )

    assert merged == {
        "meal_type": "dinner",
        "allergens": ["花生", "牛奶"],
        "tastes": ["清淡"],
    }
