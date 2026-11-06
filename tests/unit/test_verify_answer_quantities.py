import copy

import pytest
from scripts.verify_answer_quantities import RecordingGateway, boundary_cases

from nutripilot.agent.contracts import (
    AnswerBoundaryCheck,
    RecommendationDraft,
    RecommendationOutput,
)
from nutripilot.model_gateway import FakeModelGateway


@pytest.mark.asyncio
async def test_recording_gateway_injects_only_drafts_and_records_real_boundary_results():
    underlying = FakeModelGateway()
    gateway = RecordingGateway(underlying, RecommendationDraft(message="豆腐300克。"))
    payload = {"answer": {"message": "豆腐300克。"}}
    original = copy.deepcopy(payload)
    injected = await gateway.generate_structured(
        worker="recommendation", system_prompt="generate", payload=payload,
        response_model=RecommendationOutput, prompt_version="test",
    )
    assert injected.output.response.message == "豆腐300克。"
    assert underlying.call_history == []
    underlying.queue_response("answer_boundary", {
        "facts_match": False, "objects_match": True, "portion_basis_preserved": True,
        "cooking_conditions_preserved": True, "issues": ["无依据克重。"],
    })
    result = await gateway.generate_structured(
        worker="answer_boundary", system_prompt="check", payload=payload,
        response_model=AnswerBoundaryCheck, prompt_version="test",
    )
    payload["answer"]["message"] = "后续变动"
    assert result.output.facts_match is False
    assert gateway.calls[0]["injected"] is True
    assert gateway.calls[1]["injected"] is False
    assert gateway.calls[1]["payload"] == original
    assert gateway.calls[1]["result"]["output"]["facts_match"] is False
    assert underlying.call_history[0]["worker"] == "answer_boundary"


def test_boundary_fixtures_cover_direct_queries_without_hidden_target_ids():
    cases = boundary_cases()
    assert len({name for name, *_ in cases}) == len(cases)
    direct_cases = [item for item in cases if item[0].startswith("direct_recipe_")]
    assert {expected for _, expected, _, _ in direct_cases} == {True, False}
    for _, _, state, _ in direct_cases:
        assert state["answer_task"]["meal_ids"] == []
        assert state["tool_results"][0]["status"] == "success"
        assert state["tool_results"][0]["data"]["raw_quantities"] == "豆腐1盒"
