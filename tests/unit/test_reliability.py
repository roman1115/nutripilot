from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from anyio import create_task_group
from apps.nutrition_mcp.service import NutritionMCPService
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nutripilot.agent.contracts import IntentOutput, PlanSpec
from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.agent.planning import PlanBuilder, PlanBuildError
from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.chat_service import ThreadBusyError
from nutripilot.application.evaluation_service import EvaluationService
from nutripilot.config import Settings
from nutripilot.domain.models import FinalResponse, MealRecommendation
from nutripilot.evaluation import EvalRunner
from nutripilot.evaluation.models import EvalReport
from nutripilot.infrastructure.run_store import SQLiteRunStore
from nutripilot.model_gateway import FakeModelGateway, ModelGatewayError, ModelUsage
from nutripilot.model_gateway.budget import ExecutionLimitExceeded, execution_budget
from nutripilot.tools import ToolCall, ToolContext, ToolDefinition, ToolExecutor, ToolRegistry
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.models import GetUserProfileArgs
from nutripilot.tools.mcp.models import GetNutritionMCPArgs
from nutripilot.tracing.recorder import SQLiteTraceRecorder

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
TOKEN_ONE = "a" * 40
TOKEN_TWO = "b" * 40


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_explanation_cannot_turn_meal_allergens_into_personal_facts() -> None:
    response = FinalResponse(
        message="你对鱼过敏",
        recommendations=[
            MealRecommendation(
                meal_id=4,
                name="鱼",
                reason="你已标记鱼和甲壳类过敏",
            )
        ],
        warnings=["你对鱼过敏"],
        suggested_actions=["移除你的鱼类过敏"],
    )
    results = [
        {"tool_name": "get_user_profile", "status": "success", "data": {"allergens": ["花生"]}},
        {
            "tool_name": "search_meals",
            "status": "success",
            "data": {
                "hits": [
                    {
                        "meal_id": 4,
                        "name": "清蒸鲈鱼配时蔬",
                        "allergens": ["鱼"],
                        "nutrition": {"calories_kcal": 346, "protein_g": 45, "serving_size": "1份"},
                    }
                ]
            },
        },
    ]
    grounded = ground_recommendation_facts(response, results)
    text = grounded.model_dump_json()
    assert "你对鱼过敏" not in text
    assert "甲壳类" not in text
    assert "移除你的鱼类过敏" not in text
    assert "用户画像记录的过敏原：花生" in text
    assert "餐食记录标注的过敏原：鱼" in text
    assert grounded.recommendations[0].calories_kcal == 346
    assert "346" not in grounded.recommendations[0].reason


def test_unknown_recommendation_does_not_survive_grounding() -> None:
    response = FinalResponse(
        message="推荐",
        recommendations=[MealRecommendation(meal_id=999, name="假的", reason="假的")],
    )
    result = ground_recommendation_facts(
        response,
        [
            {
                "tool_name": "search_meals",
                "status": "success",
                "data": {"hits": []},
            }
        ],
    )
    assert not result.recommendations
    assert result.status == "degraded"
    assert "假的" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_nutrition_alias_resolves_to_the_authoritative_record() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    result = await service.get_nutrition(GetNutritionMCPArgs(name="糖 拌 西 红 柿"))
    assert result.meal_id == 1004
    assert result.nutrition.calories_kcal == 113.4


@pytest.mark.asyncio
async def test_partial_and_fuzzy_names_require_confirmation() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    with pytest.raises(ToolRuntimeError) as caught:
        await service.get_nutrition(GetNutritionMCPArgs(name="糖拌"))
    assert caught.value.code == "MEAL_NAME_AMBIGUOUS"
    assert caught.value.details["candidates"][0]["meal_id"] == 1004


@pytest.mark.asyncio
async def test_unknown_name_stops_after_one_lookup_and_the_required_profile() -> None:
    gateway = FakeModelGateway()
    container = build_container(gateway=gateway, settings=_settings())
    result = await container.chat_service.chat(
        user_id=1,
        message="月球营养超级碗有多少热量？",
    )
    assert result["phase"] == "CLARIFY"
    assert [item["tool_name"] for item in result["tool_results"]] == [
        "get_user_profile", "get_nutrition",
    ]
    assert sum(call["worker"] == "planner" for call in gateway.call_history) == 1
    assert "没有找到" in result["final_response"]["message"]


@pytest.mark.asyncio
async def test_name_clarification_continues_with_selected_candidate() -> None:
    container = build_container(settings=_settings())
    first = await container.chat_service.chat(
        user_id=1,
        message="糖拌有多少热量？",
        thread_id="alias-clarify",
    )
    assert first["phase"] == "CLARIFY"
    second = await container.chat_service.chat(
        user_id=1,
        message="糖拌西红柿",
        thread_id="alias-clarify",
    )
    assert second["phase"] == "COMPLETED"
    assert "113.4" in second["final_response"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("food_name", "phase"),
    [("糖拌西红柿", "COMPLETED"), ("糖拌", "CLARIFY"), ("月球营养超级碗", "CLARIFY")],
)
async def test_named_nutrition_query_cannot_use_another_candidate_id(
    food_name: str, phase: str
) -> None:
    gateway = FakeModelGateway()
    container = build_container(gateway=gateway, settings=_settings())
    await container.chat_service.chat(
        user_id=1, message="推荐一道晚餐", thread_id="named-nutrition"
    )
    gateway.queue_response(
        "intent",
        {
            "intent": "NUTRITION_QUERY",
            "confidence": 1,
            "extracted_slots": {"food_name": food_name},
        },
    )
    gateway.queue_response(
        "planner",
        {
            "complete": False,
            "plan_summary": "查询营养",
            "tool_calls": [
                {
                    "call_id": "wrong-candidate",
                    "name": "get_nutrition",
                    "arguments": {"meal_id": 1011, "name": "清炒花菜"},
                }
            ],
        },
    )
    result = await container.chat_service.chat(
        user_id=1, message=f"{food_name}有多少热量？", thread_id="named-nutrition"
    )
    assert result["phase"] == phase
    nutrition = next(
        item for item in result["tool_results"] if item["tool_name"] == "get_nutrition"
    )
    if phase == "COMPLETED":
        assert nutrition["data"]["meal_id"] == 1004
        assert "113.4" in result["final_response"]["message"]
    else:
        assert nutrition["status"] == "error"


@pytest.mark.asyncio
async def test_optional_missing_slots_cannot_block_a_valid_plan() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response(
        "intent",
        {
            "intent": "MEAL_PLAN",
            "confidence": 1,
            "extracted_slots": {"meal_type": "dinner", "plan_range": "one_week"},
            "missing_slots": ["dietary_restrictions"],
        },
    )
    container = build_container(gateway=gateway, settings=_settings())
    state = await container.chat_service.chat(user_id=1, message="一周晚餐计划")
    assert state["phase"] == "AWAITING_CONFIRMATION"
    assert state["missing_slots"] == []


@pytest.mark.asyncio
async def test_explicit_date_survives_intent_model_failure() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response("intent", RuntimeError("offline"))
    container = build_container(gateway=gateway, settings=_settings())
    state = await container.chat_service.chat(
        user_id=1,
        message="从2026年10月22日起制定一周晚餐计划",
    )
    assert state["slots"]["start_date"] == "2026-10-22"
    assert state["slots"]["plan_range"] == "one_week"
    assert state["pending_tool_calls"][0]["arguments"]["start_date"] == "2026-10-22"


@pytest.mark.asyncio
async def test_invalid_date_is_clarified_before_tool_calls() -> None:
    container = build_container(settings=_settings())
    first = await container.chat_service.chat(
        user_id=1,
        message="从2026-02-30起制定一周晚餐计划",
        thread_id="invalid-date",
    )
    assert first["phase"] == "CLARIFY"
    assert first["missing_slots"] == ["start_date"]
    assert first["tool_results"] == []
    second = await container.chat_service.chat(
        user_id=1,
        message="2026-03-01",
        thread_id="invalid-date",
    )
    assert second["phase"] == "AWAITING_CONFIRMATION"
    assert second["pending_tool_calls"][0]["arguments"]["start_date"] == "2026-03-01"


def test_intent_schema_exposes_the_typed_slots_to_the_model() -> None:
    slots = IntentOutput.model_json_schema()["properties"]["extracted_slots"]
    assert slots["additionalProperties"] is False
    assert "one_week" in str(slots["properties"]["plan_range"])
    assert "start_date" in slots["properties"]


@pytest.mark.asyncio
async def test_failed_model_attempt_usage_is_recorded_in_fallback_trace() -> None:
    error = ModelGatewayError("INVALID_STRUCTURED_OUTPUT", "invalid")
    error.usage = ModelUsage(input_tokens=100, output_tokens=30)
    gateway = FakeModelGateway()
    gateway.queue_response("intent", error)
    container = build_container(gateway=gateway, settings=_settings())
    state = await container.chat_service.chat(user_id=1, message="推荐晚餐")
    fallback = next(
        event
        for event in container.recorder.get_trace(state["trace_id"])
        if event.event_type == "model.fallback"
    )
    assert fallback.input_tokens == 100
    assert fallback.output_tokens == 30


@pytest.mark.parametrize(
    "slots",
    [
        {"meal_type": "unknown"},
        {"max_calories_kcal": -1},
        {"allergens": "花生"},
        {"start_date": "明天"},
        {"made_up_field": True},
    ],
)
def test_intent_slots_reject_invalid_model_values(slots: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IntentOutput(intent="MEAL_PLAN", confidence=1, extracted_slots=slots)


def test_plan_spec_reads_user_start_date_and_rejects_wrong_model_date() -> None:
    spec = PlanSpec.from_slots(
        {
            "plan_range": "one_day",
            "meal_type": "dinner",
            "start_date": "2026-10-22",
        },
        start_date=date(2026, 11, 1),
    )
    assert spec.start_date == date(2026, 10, 22)
    candidates = [
        {
            "meal_id": 2,
            "meal_types": ["dinner"],
            "ingredients": ["鸡胸肉"],
            "allergens": [],
        }
    ]
    wrong = PlanBuilder().build(
        spec.model_copy(update={"start_date": date(2026, 11, 1)}),
        candidates,
    )
    with pytest.raises(PlanBuildError) as caught:
        PlanBuilder().validate_prewrite(
            spec=spec,
            request=wrong,
            candidates=candidates,
            allowed_meal_ids={2},
            declared_allergens=[],
            excluded_ingredients=[],
        )
    assert caught.value.code == "PLAN_START_DATE_MISMATCH"


class SlowGateway(FakeModelGateway):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate_structured(self, **kwargs: Any) -> Any:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.mark.asyncio
async def test_total_deadline_cancels_model_and_persists_termination() -> None:
    gateway = SlowGateway()
    container = build_container(
        gateway=gateway,
        settings=_settings(agent_deadline_seconds=0.02),
    )
    state = await container.chat_service.chat(
        user_id=1,
        message="推荐晚餐",
        thread_id="timeout",
    )
    assert gateway.cancelled.is_set()
    assert state["error"]["code"] == "DEADLINE_EXCEEDED"
    restored = await container.chat_service.get_thread(thread_id="timeout", user_id=1)
    assert restored["phase"] == "FAILED"


@pytest.mark.asyncio
async def test_stream_deadline_yields_a_final_failed_checkpoint() -> None:
    container = build_container(
        gateway=SlowGateway(),
        settings=_settings(agent_deadline_seconds=0.02),
    )
    events = [
        event
        async for event in container.chat_service.stream(
            user_id=1,
            message="推荐晚餐",
        )
    ]
    assert events[-1][0] == "completed"
    assert events[-1][1]["error"]["code"] == "DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_token_budget_stops_before_another_model_call() -> None:
    gateway = FakeModelGateway()
    container = build_container(gateway=gateway, settings=_settings(agent_token_budget=1))
    state = await container.chat_service.chat(user_id=1, message="推荐晚餐")
    assert state["error"]["code"] == "TOKEN_BUDGET_EXCEEDED"
    assert not gateway.call_history


def test_completion_reservation_rejects_oversized_input() -> None:
    with (
        execution_budget(seconds=5, tokens=200) as budget,
        pytest.raises(ExecutionLimitExceeded),
    ):
        budget.completion_limit({"input": "很多输入" * 100})


@pytest.mark.asyncio
async def test_stop_cancels_model_and_concurrent_turn_is_rejected() -> None:
    gateway = SlowGateway()
    container = build_container(gateway=gateway, settings=_settings())
    task = asyncio.create_task(
        container.chat_service.chat(
            user_id=1,
            message="推荐晚餐",
            thread_id="cancel",
        )
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=1)
    with pytest.raises(ThreadBusyError):
        await container.chat_service.chat(user_id=1, message="推荐午餐", thread_id="cancel")
    assert not container.chat_service.cancel(thread_id="cancel", user_id=2)
    assert container.chat_service.cancel(thread_id="cancel", user_id=1)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gateway.cancelled.is_set()
    assert not container.chat_service.cancel(thread_id="cancel", user_id=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["chat", "stream", "confirm"])
async def test_disconnect_persists_cancelled_turn_and_allows_continuation(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    gateway = FakeModelGateway()
    container = build_container(gateway=gateway, settings=_settings())
    thread_id = f"disconnect-{operation}"
    if operation == "confirm":
        pending = await container.chat_service.chat(
            user_id=1, message="制定明天的晚餐餐单", thread_id=thread_id
        )
        assert pending["requires_confirmation"]
    slow = SlowGateway()
    generate = gateway.generate_structured
    monkeypatch.setattr(gateway, "generate_structured", slow.generate_structured)
    update = container.graph.compiled.aupdate_state

    async def yielding_update(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0)
        return await update(*args, **kwargs)

    monkeypatch.setattr(container.graph.compiled, "aupdate_state", yielding_update)

    async def run() -> None:
        if operation == "stream":
            async for _ in container.chat_service.stream(
                user_id=1, message="推荐晚餐", thread_id=thread_id
            ):
                pass
        elif operation == "confirm":
            await container.chat_service.confirm(
                user_id=1, thread_id=thread_id, approved=True
            )
        else:
            await container.chat_service.chat(
                user_id=1, message="推荐晚餐", thread_id=thread_id
            )

    async with create_task_group() as group:
        group.start_soon(run)
        await asyncio.wait_for(slow.started.wait(), timeout=2)
        group.cancel_scope.cancel()

    state = await container.chat_service.get_thread(thread_id=thread_id, user_id=1)
    assert state["phase"] == "FAILED"
    assert state["error"]["code"] == "CANCELLED"
    assert not state["requires_confirmation"]
    messages = await container.chat_service.messages(thread_id=thread_id, user_id=1)
    assert messages[-1].role == "assistant"
    assert "已停止本轮请求" in messages[-1].content
    assert not container.chat_service.cancel(thread_id=thread_id, user_id=1)
    if operation == "confirm":
        assert len(await container.data_store.list_plans(1)) == 1
    monkeypatch.setattr(gateway, "generate_structured", generate)
    result = await container.chat_service.chat(
        user_id=1, message="糖拌西红柿有多少热量", thread_id=thread_id
    )
    assert result["phase"] == "COMPLETED"


@pytest.mark.asyncio
async def test_tool_does_not_swallow_task_cancellation() -> None:
    started = asyncio.Event()

    async def handler(arguments: Any, context: Any) -> dict[str, str]:
        started.set()
        await asyncio.Event().wait()
        return {}

    executor = ToolExecutor(
        ToolRegistry(
            [
                ToolDefinition("slow", "slow", GetUserProfileArgs, handler),
            ]
        )
    )
    task = asyncio.create_task(
        executor.execute(
            ToolCall(call_id="slow", name="slow"),
            ToolContext(),
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_trace_and_reports_survive_reopening_and_redact_credentials(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    recorder = SQLiteTraceRecorder(SQLiteRunStore(path))
    recorder.record(
        trace_id="trace",
        thread_id="thread",
        turn_id="turn",
        user_id=1,
        event_type="test",
        component="test",
        payload={"api_key": "sensitive"},
    )
    restarted_store = SQLiteRunStore(path)
    events = SQLiteTraceRecorder(restarted_store).get_trace("trace")
    assert events[0].payload["api_key"] == "***"
    SQLiteTraceRecorder(restarted_store).record(
        trace_id="trace",
        thread_id="thread",
        turn_id="turn",
        user_id=1,
        event_type="after_restart",
        component="test",
    )
    assert len(restarted_store.get_trace("trace")) == 2
    report = EvalReport(dataset_version="test", total_cases=1, passed_cases=0, status="running")
    restarted_store.save_report(report)
    restarted_store.interrupt_running_reports()
    persisted = SQLiteRunStore(path).get_report(report.run_id)
    assert persisted is not None and persisted.status == "interrupted"
    runner = EvalRunner(build_container(settings=_settings()).chat_service)
    service = EvaluationService(runner, SQLiteRunStore(path))
    assert service.get(report.run_id).status == "interrupted"


def test_token_identity_overrides_header_and_protects_resources() -> None:
    container = build_container(settings=_settings(api_access_tokens={TOKEN_ONE: 1, TOKEN_TWO: 2}))
    headers = {"Authorization": f"Bearer {TOKEN_ONE}"}
    with TestClient(create_app(container)) as client:
        assert client.get("/api/v1/profile").status_code == 401
        profile = client.get("/api/v1/profile", headers={**headers, "X-User-ID": "2"})
        assert profile.json()["user_id"] == 1
        spoofed = client.post(
            "/api/v1/chat",
            headers=headers,
            json={"user_id": 2, "message": "推荐晚餐"},
        )
        assert spoofed.status_code == 403
        chat = client.post(
            "/api/v1/chat",
            headers=headers,
            json={"user_id": 1, "message": "推荐晚餐"},
        ).json()
        assert (
            client.get(
                f"/api/v1/traces/{chat['trace_id']}",
                headers={"Authorization": f"Bearer {TOKEN_TWO}"},
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/v1/traces/{chat['trace_id']}",
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v1/threads/{chat['thread_id']}?user_id=1",
                headers={"Authorization": f"Bearer {TOKEN_TWO}"},
            ).status_code
            == 403
        )


def test_authenticated_requests_are_rate_limited() -> None:
    container = build_container(
        settings=_settings(
            api_access_tokens={TOKEN_ONE: 1},
            api_rate_limit_per_minute=1,
        )
    )
    with TestClient(create_app(container)) as client:
        headers = {"Authorization": f"Bearer {TOKEN_ONE}"}
        assert client.get("/api/v1/profile", headers=headers).status_code == 200
        assert client.get("/api/v1/profile", headers=headers).status_code == 429


def test_production_refuses_to_start_without_authentication() -> None:
    with pytest.raises(ValidationError, match="API_ACCESS_TOKENS"):
        _settings(app_mode="production")


@pytest.mark.asyncio
async def test_new_regression_dataset_runs_without_external_services() -> None:
    from nutripilot.evaluation.runner import load_dataset

    version, cases = load_dataset(DATA_DIR.parent / "evals/datasets/real-regressions.json")
    report = await EvalRunner(build_container(settings=_settings()).chat_service).run(
        cases,
        dataset_version=version,
    )
    assert report.passed_cases == report.total_cases, json.dumps(
        report.failures, ensure_ascii=False
    )
    assert report.runtime["mode"] == "fake"
    assert report.metrics["input_tokens"] > 0
