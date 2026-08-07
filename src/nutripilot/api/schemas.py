"""Public API request and response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nutripilot.agent.state import AgentState
from nutripilot.domain.models import FinalResponse
from nutripilot.evaluation.models import EvalCase, EvalReport
from nutripilot.tools.local.models import UpdateUserProfileArgs


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)
    user_id: int = Field(default=1, gt=0)
    thread_id: str | None = None


class ConfirmationRequest(BaseModel):
    user_id: int = Field(default=1, gt=0)
    approved: bool
    confirmation_id: str | None = None
    idempotency_key: str | None = None


class PendingAction(BaseModel):
    id: str
    title: str
    description: str
    operation: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


class ChatResponse(BaseModel):
    thread_id: str
    turn_id: str
    trace_id: str
    phase: str
    intent: str | None = None
    slots: dict[str, Any] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    pending_action: PendingAction | None = None
    final_response: FinalResponse | None = None

    @classmethod
    def from_state(cls, state: AgentState) -> ChatResponse:
        raw = state.get("final_response")
        pending_calls = state.get("pending_tool_calls", [])
        pending_action = None
        if state.get("requires_confirmation") and pending_calls:
            call = pending_calls[0]
            call_id = str(call.get("call_id", ""))
            operation = str(call.get("name", "write_operation"))
            pending_action = PendingAction(
                id=call_id,
                title="确认数据写入",
                description="请核对以下内容；只有确认后 Agent 才会执行持久化写入。",
                operation=operation,
                payload=dict(call.get("arguments") or {}),
                idempotency_key=call_id,
            )
        return cls(
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            trace_id=state["trace_id"],
            phase=state["phase"],
            intent=state.get("intent"),
            slots=state.get("slots", {}),
            missing_slots=state.get("missing_slots", []),
            requires_confirmation=state.get("requires_confirmation", False),
            pending_action=pending_action,
            final_response=FinalResponse.model_validate(raw) if raw else None,
        )


class EvaluationRequest(BaseModel):
    dataset_version: str | None = None
    prompt_version: str | None = None
    cases: list[EvalCase] | None = None


class EvaluationResponse(BaseModel):
    report: EvalReport


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = None
    goals: list[str] = Field(default_factory=list)
    tastes: list[str] = Field(default_factory=list)
    cuisines: list[str] = Field(default_factory=list)
    avoidances: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    dietary_restrictions: list[str] = Field(default_factory=list)
    daily_calorie_target: float | None = Field(default=None, gt=0)
    location: str | None = None

    def to_domain(self) -> UpdateUserProfileArgs:
        return UpdateUserProfileArgs(
            display_name=self.display_name,
            preferences=self.tastes,
            cuisines=self.cuisines,
            dislikes=self.avoidances,
            allergens=self.allergens,
            health_goals=self.goals,
            dietary_tags=self.dietary_restrictions,
            daily_calorie_target=self.daily_calorie_target,
            location=self.location,
        )
