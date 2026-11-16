"""Trusted conversation preparation, confirmation and private context regression checks."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from tests.diet_helpers import approval

from nutripilot.application import build_container
from nutripilot.application.chat_service import InvalidConfirmationStateError
from nutripilot.config import Settings
from nutripilot.domain.diet import DietError, SubmissionAnchor
from nutripilot.domain.diet_records import IntakeDraft
from nutripilot.memory.context import conversation_context
from nutripilot.memory.nodes import MemoryNodes
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.tools import ToolCall, ToolContext
from nutripilot.tools.local.diet_models import CreateIntake, ReviseIntake

NOW = datetime(2026, 11, 12, 18, tzinfo=UTC)
MESSAGE = "我今天午餐吃了苹果，重量不知道"


def request(turn: str, *, subject="self", actuality="actual", meal_type="lunch", day="2026-11-12"):
    return {
        "action": "create",
        "entry": {
            "subject": subject,
            "actuality": actuality,
            "temporal": {"local_date": day, "meal_type": meal_type},
            "items": [
                {
                    "client_item_key": "apple",
                    "food": {"kind": "unknown", "name": "苹果"},
                    "quantity": {"kind": "unknown", "description": "重量不知道"},
                }
            ],
        },
        "evidence": [
            {
                "field": "food",
                "source_kind": "user_message",
                "source_ref": turn,
                "original_text": "苹果",
                "operation": "set",
                "status": "explicit",
            }
        ],
    }


def runtime():
    gateway = FakeModelGateway()
    gateway.register_handler(
        "intent",
        lambda _: {
            "intent": "DIET_MANAGEMENT",
            "confidence": 1,
            "extracted_slots": {},
            "missing_slots": [],
            "diet_operation": "intake",
        },
    )
    gateway.register_handler(
        "diet_planner",
        lambda payload: {
            "complete": False,
            "plan_summary": "准备草稿",
            "tool_calls": [
                {
                    "name": "prepare_intake_draft",
                    "call_id": "untrusted-model-key",
                    "arguments": request(payload["turn_id"]),
                }
            ],
        },
    )
    gateway.register_handler(
        "diet_parser",
        lambda _: {"entries": [request("parser")["entry"]], "clarification": ""},
    )
    container = build_container(gateway=gateway, settings=Settings(_env_file=None))
    container.diet.clock = lambda: NOW
    container.diet.intake.clock = lambda: NOW
    return container, gateway


async def prepare(container):
    state = await container.chat_service.chat(user_id=1, message=MESSAGE, thread_id=uuid4().hex)
    assert state["phase"] == "AWAITING_CONFIRMATION"
    assert state["pending_tool_calls"] == []
    assert state["tool_results"] == []
    return state


async def logs(container):
    async with container.diet.store.transaction(
        1, default_timezone=container.diet.default_timezone
    ) as tx:
        return await tx.list_logs(date(2026, 11, 12), date(2026, 11, 12))


@pytest.mark.asyncio
async def test_chat_prepare_unknown_quantity_and_trusted_commit_without_model():
    container, gateway = runtime()
    state = await prepare(container)
    assert await logs(container) == []
    ref = state["pending_diet_preview_ref"]
    draft = await container.diet.preview(1, UUID(ref["id"]))
    assert isinstance(draft, IntakeDraft)
    assert draft.content.items[0].nutrition_snapshot.calories_kcal.value is None
    calls = len(gateway.call_history)
    completed = await container.chat_service.confirm(
        user_id=1,
        thread_id=state["thread_id"],
        approved=True,
        **approval(state),
    )
    assert completed["phase"] == "COMPLETED"
    assert len(gateway.call_history) == calls
    assert len(await logs(container)) == 1
    await container.diet.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    assert len(await logs(container)) == 1
    events = container.recorder.get_trace(state["trace_id"])
    assert all("苹果" not in str(event.payload) for event in events)


@pytest.mark.asyncio
async def test_diet_final_tool_call_is_executed_even_when_model_marks_planning_complete():
    container, gateway = runtime()
    gateway.register_handler(
        "diet_planner",
        lambda payload: {
            "complete": True,
            "plan_summary": "准备完成，等待确认。",
            "tool_calls": [
                {
                    "name": "prepare_intake_draft",
                    "call_id": "final-call",
                    "arguments": request(payload["turn_id"]),
                }
            ],
        },
    )
    state = await prepare(container)
    assert state["requires_confirmation"]
    assert await logs(container) == []


def test_diet_planner_validates_nested_arguments_before_tool_execution():
    from pydantic import ValidationError

    from nutripilot.agent.contracts import PlannerOutput

    container, _ = runtime()
    arguments = request("turn")
    arguments["entry"]["items"][0]["quantity"] = {"kind": "mass", "grams": "not-a-number"}
    with pytest.raises(ValidationError):
        PlannerOutput.model_validate(
            {
                "complete": True,
                "plan_summary": "test",
                "tool_calls": [
                    {"call_id": "x", "name": "prepare_intake_draft", "arguments": arguments}
                ],
            },
            context={
                "payload": {
                    "available_tools": container.tool_executor.registry.planner_schemas(
                        {"prepare_intake_draft"},
                    )
                }
            },
        )


@pytest.mark.parametrize("failed_intent", [False, True])
@pytest.mark.asyncio
async def test_explicit_record_request_cannot_route_to_recommendation(failed_intent):
    container, gateway = runtime()
    gateway.register_handler(
        "intent",
        lambda _: {
            "intent": "MEAL_RECOMMENDATION",
            "confidence": 1,
            "extracted_slots": {"meal_type": "lunch"},
            "missing_slots": [],
        },
    )
    if failed_intent:
        gateway.queue_response("intent", RuntimeError("invalid model response"))
    state = await container.chat_service.chat(
        user_id=1,
        thread_id=uuid4().hex,
        message="我今天午餐吃了苹果，帮我记录。",
    )
    assert state["intent"] == "DIET_MANAGEMENT"
    assert state["diet_operation"] == "intake"
    assert state["phase"] == "AWAITING_CONFIRMATION"
    assert await logs(container) == []


@pytest.mark.asyncio
async def test_new_record_uses_shared_parser_and_never_needs_a_matching_catalog_food():
    container, gateway = runtime()
    gateway.register_handler(
        "diet_planner", lambda _: pytest.fail("A new record must not loop over catalog searches.")
    )
    state = await container.chat_service.chat(
        user_id=1, thread_id=uuid4().hex,
        message="我今天午餐吃了苹果，重量不知道，帮我记。",
    )
    assert state["phase"] == "AWAITING_CONFIRMATION"
    draft = await container.diet.preview(1, UUID(state["active_draft_ref"]["id"]))
    assert draft.content.items[0].quantity.kind == "unknown"
    assert draft.content.items[0].nutrition_snapshot.calories_kcal.value is None
    assert all(e.source_ref == state["turn_id"] for e in draft.evidence)
    assert any(call["worker"] == "diet_parser" for call in gateway.call_history)
    assert await logs(container) == []


@pytest.mark.asyncio
async def test_summary_returns_authoritative_result_without_another_planning_call():
    container, gateway = runtime()
    gateway.register_handler("intent", lambda _: {
        "intent": "DIET_MANAGEMENT", "diet_operation": "query", "confidence": 1,
        "extracted_slots": {}, "missing_slots": [],
    })
    gateway.queue_response("diet_planner", {
        "complete": False, "plan_summary": "查询今日",
        "tool_calls": [{
            "name": "get_nutrition_summary", "call_id": "summary",
            "arguments": {
                "scope": {
                    "kind": "range", "start_date": "2026-11-12", "end_date": "2026-11-12",
                },
                "granularity": "day",
            },
        }],
    })
    state = await container.chat_service.chat(
        user_id=1, thread_id=uuid4().hex, message="只查询今天实际饮食营养汇总。"
    )
    assert state["phase"] == "COMPLETED"
    assert "总量未知" in state["final_response"]["message"]
    assert sum(call["worker"] == "diet_planner" for call in gateway.call_history) == 1


@pytest.mark.asyncio
async def test_unchanged_draft_patch_explains_no_change_without_repeated_model_calls():
    container, gateway = runtime()
    state = await prepare(container)
    count = len(gateway.call_history)

    def unchanged(payload):
        return {
            "complete": False, "plan_summary": "保留原份量",
            "tool_calls": [{
                "name": "prepare_intake_draft", "call_id": "unchanged",
                "arguments": {
                    "action": "revise", "draft_id": payload["active_draft"]["id"],
                    "base_version": payload["active_draft"]["version"],
                    "patch": {"item_edits": [{
                        "action": "update", "item_id": payload["active_draft"]["items"][0]["id"],
                        "changes": {"quantity": {"kind": "unknown", "description": "重量不知道"}},
                    }]},
                    "evidence": [{
                        "field": "quantity", "source_kind": "user_message",
                        "source_ref": payload["turn_id"], "original_text": payload["user_message"],
                        "operation": "keep", "status": "explicit",
                    }],
                },
            }],
        }

    gateway.register_handler("diet_planner", unchanged)
    result = await container.chat_service.chat(
        user_id=1, thread_id=state["thread_id"], message="苹果份量保留未知。"
    )
    assert result["phase"] == "COMPLETED"
    assert "未生成新的变更" in result["final_response"]["message"]
    calls = gateway.call_history[count:]
    assert sum(call["worker"] == "diet_planner" for call in calls) == 1
    assert await logs(container) == []


@pytest.mark.parametrize("message", ["帮我记住我对花生过敏", "把我的过敏偏好记下来"])
def test_profile_memory_requests_do_not_override_intent_as_intake(message):
    from nutripilot.agent.agents.intent import _explicit_intake_request

    assert not _explicit_intake_request(message)


@pytest.mark.asyncio
async def test_explicit_record_word_preserves_recognized_future_plan_operation():
    from nutripilot.agent.agents.intent import IntentAgent
    from nutripilot.agent.state import new_agent_state

    container, gateway = runtime()
    gateway.register_handler("intent", lambda _: {
        "intent": "DIET_MANAGEMENT", "confidence": 1, "diet_operation": "plan_change",
        "extracted_slots": {}, "missing_slots": [],
    })
    state = new_agent_state(
        message="帮我记录明天午餐的苹果计划。", user_id=1, current_date=date(2026, 11, 12)
    )
    result = await IntentAgent(gateway, container.recorder).run(state)
    assert result["diet_operation"] == "plan_change"


@pytest.mark.parametrize(
    "message,intent,expected",
    [
        ("米饭是150克熟重。", "OTHER", "DIET_MANAGEMENT"),
        ("是午餐，份量保留未知。", "NUTRITION_QUERY", "DIET_MANAGEMENT"),
        ("推荐一道100克鸡肉的菜。", "MEAL_RECOMMENDATION", "MEAL_RECOMMENDATION"),
        ("查询100克米饭有多少热量。", "NUTRITION_QUERY", "NUTRITION_QUERY"),
        ("把药物改成100克。", "HEALTH_RISK", "HEALTH_RISK"),
    ],
)
@pytest.mark.asyncio
async def test_active_draft_corrections_keep_the_diet_route(message, intent, expected):
    from nutripilot.agent.agents.intent import IntentAgent
    from nutripilot.agent.state import new_agent_state

    container, gateway = runtime()
    gateway.register_handler("intent", lambda _: {
        "intent": intent, "confidence": 1, "extracted_slots": {}, "missing_slots": [],
    })
    state = new_agent_state(message=message, user_id=1)
    state["active_draft_ref"] = {"id": str(uuid4()), "version": 1}
    result = await IntentAgent(gateway, container.recorder).run(state)
    assert result["intent"] == expected


@pytest.mark.asyncio
async def test_model_can_discard_active_draft_with_current_operation_evidence():
    container, gateway = runtime()
    state = await prepare(container)
    ref = state["active_draft_ref"]

    def discard(payload):
        return {
            "complete": True,
            "plan_summary": "放弃当前草稿",
            "tool_calls": [{
                "name": "prepare_intake_draft",
                "call_id": "discard",
                "arguments": {
                    "action": "discard",
                    "draft_id": payload["active_draft"]["id"],
                    "base_version": payload["active_draft"]["version"],
                    "evidence": [{
                        "field": "operation",
                        "operation": "clear",
                        "source_kind": "user_message",
                        "source_ref": payload["turn_id"],
                        "original_text": payload["user_message"],
                        "status": "explicit",
                    }],
                },
            }],
        }

    gateway.register_handler("diet_planner", discard)
    result = await container.chat_service.chat(
        user_id=1, thread_id=state["thread_id"], message="撤回刚才的话，我没吃苹果，不要记。"
    )
    assert result["final_response"]["diet_context"]["state"] == "discarded"
    assert (await container.diet.preview(1, UUID(ref["id"]))).state == "discarded"
    assert not result["requires_confirmation"]
    assert await logs(container) == []


@pytest.mark.asyncio
async def test_typed_planner_patch_preserves_omitted_date_and_item_notes():
    container, gateway = runtime()

    def missing_meal(payload):
        return {
            "complete": True, "plan_summary": "需要餐次",
            "tool_calls": [{
                "name": "prepare_intake_draft", "call_id": "create",
                "arguments": request(payload["turn_id"], meal_type=None),
            }],
        }

    gateway.register_handler("diet_planner", missing_meal)
    state = await container.chat_service.chat(
        user_id=1, thread_id=uuid4().hex, message=MESSAGE
    )
    original = await container.diet.preview(1, UUID(state["active_draft_ref"]["id"]))
    assert original.state == "needs_input"

    def fill_meal(payload):
        return {
            "complete": True, "plan_summary": "补充午餐",
            "tool_calls": [{
                "name": "prepare_intake_draft", "call_id": "revise",
                "arguments": {
                    "action": "revise", "draft_id": str(original.id),
                    "base_version": original.version,
                    "patch": {"temporal": {"meal_type": "lunch"}},
                    "evidence": [{
                        "field": "meal_type", "source_kind": "user_message",
                        "source_ref": payload["turn_id"], "original_text": "午餐",
                        "operation": "set", "status": "explicit",
                    }],
                },
            }],
        }

    gateway.register_handler("diet_planner", fill_meal)
    revised = await container.chat_service.chat(
        user_id=1, thread_id=state["thread_id"], message="是午餐。"
    )
    draft = await container.diet.preview(1, original.id)
    assert revised["phase"] == "AWAITING_CONFIRMATION"
    assert draft.content.entry.temporal.local_date == original.content.entry.temporal.local_date
    assert draft.content.items == original.content.items
    assert await logs(container) == []


@pytest.mark.parametrize(
    "message,allowed",
    [
        ("把那个苹果改成100克。", False),
        ("把第一份苹果改成100克。", True),
        ("把第二份苹果改成100克。", False),
        ("把50克的苹果改成100克。", True),
    ],
)
@pytest.mark.asyncio
async def test_model_cannot_choose_first_of_duplicate_foods_without_matching_selection(
    message, allowed
):
    container, gateway = runtime()

    def create(payload):
        arguments = request(payload["turn_id"])
        arguments["entry"]["items"] = [
            {
                "client_item_key": f"apple_{index}",
                "food": {"kind": "unknown", "name": "苹果"},
                "quantity": {"kind": "mass", "grams": str(grams), "food_state": "raw"},
            }
            for index, grams in enumerate((50, 80))
        ]
        return {
            "complete": True, "plan_summary": "准备草稿",
            "tool_calls": [{
                "name": "prepare_intake_draft", "call_id": "create", "arguments": arguments,
            }],
        }

    gateway.register_handler("diet_planner", create)
    state = await prepare(container)
    draft_id = UUID(state["active_draft_ref"]["id"])

    def revise(payload):
        return {
            "complete": True, "plan_summary": "更正第一份",
            "tool_calls": [{
                "name": "prepare_intake_draft", "call_id": "revise",
                "arguments": {
                    "action": "revise", "draft_id": str(draft_id), "base_version": 1,
                    "patch": {"item_edits": [{
                        "action": "update", "item_id": payload["active_draft"]["items"][0]["id"],
                        "changes": {
                            "quantity": {"kind": "mass", "grams": "100", "food_state": "raw"},
                        },
                    }]},
                    "evidence": [{
                        "field": "quantity", "source_kind": "user_message",
                        "source_ref": payload["turn_id"], "original_text": message,
                        "operation": "set", "status": "explicit",
                    }],
                },
            }],
        }

    gateway.register_handler("diet_planner", revise)
    result = await container.chat_service.chat(
        user_id=1, thread_id=state["thread_id"], message=message
    )
    draft = await container.diet.preview(1, draft_id)
    assert draft.version == (2 if allowed else 1)
    assert result["phase"] == ("AWAITING_CONFIRMATION" if allowed else "CLARIFY")
    assert draft.content.items[1].quantity.grams == 80
    assert await logs(container) == []


@pytest.mark.parametrize("reply", ["确认保存"])
@pytest.mark.asyncio
async def test_explicit_natural_approval_uses_presented_version(reply):
    container, gateway = runtime()
    state = await prepare(container)
    count = len(gateway.call_history)
    result = await container.chat_service.chat(
        user_id=1,
        thread_id=state["thread_id"],
        message=reply,
    )
    assert result["phase"] == "COMPLETED"
    assert len(await logs(container)) == 1
    assert len(gateway.call_history) == count


@pytest.mark.parametrize("reply", ["好", "继续", "知道了", "确认保存但改成晚餐"])
@pytest.mark.asyncio
async def test_ambiguous_or_editing_replies_never_approve(reply):
    container, gateway = runtime()
    state = await prepare(container)
    gateway.register_handler(
        "diet_planner",
        lambda _: {
            "complete": True,
            "tool_calls": [],
            "plan_summary": "需要核对",
        },
    )
    await container.chat_service.chat(user_id=1, thread_id=state["thread_id"], message=reply)
    assert await logs(container) == []


@pytest.mark.asyncio
async def test_diet_clarification_reaches_user_and_planner_sees_reference_context():
    container, gateway = runtime()
    state = await prepare(container)

    def clarify(payload):
        assert payload["active_draft"]["id"] == state["active_draft_ref"]["id"]
        assert payload["active_draft"]["items"][0]["id"]
        assert payload["calendar_hint"]["next_week_monday"] == "2026-11-16"
        assert "recent_messages" in payload
        return {"complete": True, "tool_calls": [], "plan_summary": "要更正午餐还是另外记录晚餐？"}

    gateway.register_handler("diet_planner", clarify)
    result = await container.chat_service.chat(
        user_id=1,
        thread_id=state["thread_id"],
        message="改一下那餐",
    )
    assert result["phase"] == "CLARIFY"
    assert result["final_response"]["message"] == "要更正午餐还是另外记录晚餐？"
    assert await logs(container) == []


@pytest.mark.parametrize("reply", ["确认删除", "确认恢复", "确认设置", "确认停用"])
@pytest.mark.asyncio
async def test_wrong_operation_word_cannot_approve_intake(reply):
    container, _ = runtime()
    state = await prepare(container)
    with pytest.raises(InvalidConfirmationStateError):
        await container.chat_service.chat(user_id=1, thread_id=state["thread_id"], message=reply)
    assert await logs(container) == []


@pytest.mark.asyncio
async def test_rejection_persists_discard_and_blocks_original_approval():
    container, _ = runtime()
    state = await prepare(container)
    ref = state["pending_diet_preview_ref"]
    await container.chat_service.confirm(
        user_id=1,
        thread_id=state["thread_id"],
        approved=False,
        **approval(state),
    )
    assert (await container.diet.preview(1, UUID(ref["id"]))).state == "discarded"
    with pytest.raises(DietError):
        await container.diet.confirm(
            1, UUID(ref["id"]), version=ref["version"], approved_hash=ref["hash"]
        )
    assert await logs(container) == []


@pytest.mark.parametrize("changed", ["hash", "version", "owner", "expired"])
@pytest.mark.asyncio
async def test_confirmation_rejects_changed_identity_version_or_expiry(changed):
    container, _ = runtime()
    state = await prepare(container)
    values = approval(state)
    if changed == "hash":
        values["content_hash"] = "0" * 64
    elif changed == "version":
        values["preview_version"] = 2
    elif changed == "expired":
        container.diet.clock = lambda: NOW + timedelta(hours=24)
    with pytest.raises((DietError, LookupError)):
        await container.chat_service.confirm(
            user_id=2 if changed == "owner" else 1,
            thread_id=state["thread_id"],
            approved=True,
            **values,
        )
    assert await logs(container) == []


@pytest.mark.parametrize(
    "subject,actuality,meal_type,day",
    [
        ("third_party", "actual", "lunch", "2026-11-12"),
        ("unknown", "actual", "lunch", "2026-11-12"),
        ("self", "planned", "lunch", "2026-11-12"),
        ("self", "hypothetical", "lunch", "2026-11-12"),
        ("self", "unknown", "lunch", "2026-11-12"),
        ("self", "actual", None, "2026-11-12"),
        ("self", "actual", "lunch", "2026-11-13"),
    ],
)
@pytest.mark.asyncio
async def test_unresolved_subject_time_or_actuality_cannot_be_confirmed(
    subject, actuality, meal_type, day
):
    container, _ = runtime()
    draft = await container.diet.intake.prepare(
        1,
        CreateIntake.model_validate(
            request("turn", subject=subject, actuality=actuality, meal_type=meal_type, day=day)
        ),
        anchor=SubmissionAnchor.capture(NOW, "America/Denver"),
        idempotency_key=uuid4().hex,
    )
    assert draft.state == "needs_input"
    with pytest.raises(DietError):
        await container.diet.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    assert await logs(container) == []


@pytest.mark.asyncio
async def test_database_idempotency_uses_call_id_and_rejects_changed_content():
    container, _ = runtime()
    ctx = ToolContext(
        user_id=1,
        thread_id="same",
        turn_id="turn",
        source_message=MESSAGE,
        idempotency_key="whole-run",
        permissions=frozenset({"diet:draft"}),
        submission_anchor=SubmissionAnchor.capture(NOW, "America/Denver"),
    )
    call = ToolCall(call_id="stable-step", name="prepare_intake_draft", arguments=request("turn"))
    first = await container.tool_executor.execute(call, ctx)
    assert first.ok
    assert (await container.tool_executor.execute(call, ctx)).data["preview"] == first.data[
        "preview"
    ]
    changed = request("turn")
    changed["entry"]["temporal"]["meal_type"] = "dinner"
    result = await container.tool_executor.execute(
        call.model_copy(update={"arguments": changed}), ctx
    )
    assert result.error.code == "IDEMPOTENCY_CONFLICT"
    assert not result.cached


@pytest.mark.asyncio
async def test_manual_field_locks_prevent_model_overwrite():
    container, _ = runtime()
    state = await prepare(container)
    ref = state["pending_diet_preview_ref"]
    draft = await container.diet.preview(1, UUID(ref["id"]))
    revision = ReviseIntake.model_validate(
        {
            "action": "revise",
            "draft_id": str(draft.id),
            "base_version": 1,
            "patch": {"temporal": {"meal_type": "dinner"}},
            "evidence": request("turn")["evidence"],
        }
    )
    edited = await container.diet.intake.revise(1, revision, idempotency_key="manual")
    assert "temporal.meal_type" in edited.content.field_locks
    assert edited.field_locks_version == 2
    model_revision = revision.model_copy(update={"base_version": 2})
    with pytest.raises(DietError, match="锁定"):
        await container.diet.intake.revise(
            1, model_revision, idempotency_key="model", actor="model"
        )


def test_summary_and_later_context_replace_both_sides_with_references():
    messages = [
        HumanMessage(
            id="turn:user", content="吃了苹果123克", additional_kwargs={"turn_id": "turn"}
        ),
        AIMessage(
            id="turn:response",
            content="苹果热量999",
            additional_kwargs={
                "turn_id": "turn",
                "diet_context": {"state": "committed", "object_ids": ["ref"]},
            },
        ),
    ]
    state = {"messages": messages, "turn_id": "next", "slots": {}}
    container, gateway = runtime()
    summary = MemoryNodes(gateway, container.recorder).summary_payload(state, messages)
    context = conversation_context(state)
    assert "苹果" not in str(summary)
    assert "苹果" not in str(context)
    assert "ref" in str(context)
    assert "苹果" in messages[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["CANCELLED", "DEADLINE_EXCEEDED", "EXECUTION_FAILED"])
async def test_failed_diet_turn_keeps_reference_marker_for_memory_and_archive(code):
    from nutripilot.agent.state import new_agent_state
    from nutripilot.application.chat_runs import response_message

    container, _ = runtime()
    state = new_agent_state(message="今天吃了苹果123克", user_id=1, thread_id=f"failed-{code}")
    state["intent"] = "DIET_MANAGEMENT"
    state["diet_operation"] = "intake"
    state["messages"] = [
        HumanMessage(
            id=f"{state['turn_id']}:user",
            content=state["user_message"],
            additional_kwargs={"turn_id": state["turn_id"]},
        )
    ]
    config = container.chat_service._config(state["thread_id"])
    result = await container.graph._terminate(state, config, code)
    assert result["final_response"]["diet_context"]["state"] == (
        "cancelled" if code == "CANCELLED" else "failed"
    )
    assert response_message(result, "response", 0).response["diet_context"]
    context = conversation_context({**result, "turn_id": "next"})
    assert "苹果" not in str(context) and "123" not in str(context)
