"""Versionable evaluation dataset and report schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field

from nutripilot.domain.enums import Intent


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    case_id: str = Field(alias="id")
    user_message: str = Field(min_length=1)
    thread_id: str | None = None
    expected_intent: Intent
    expected_slots: dict[str, Any] = Field(default_factory=dict)
    expected_tools: list[str] = Field(default_factory=list)
    expected_requires_confirmation: bool = False
    expected_blocked: bool = False
    expected_response_contains: list[str] = Field(default_factory=list)
    user_id: int = 1
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalResult(BaseModel):
    case_id: str
    passed: bool
    predicted_intent: str | None = None
    actual_tools: list[str] = Field(default_factory=list)
    actual_slots: dict[str, Any] = Field(default_factory=dict)
    checks: dict[str, bool] = Field(default_factory=dict)
    phase: str
    trace_id: str
    latency_ms: float = Field(ge=0)
    failure_reasons: list[str] = Field(default_factory=list)


class EvalReport(BaseModel):
    status: str = "completed"
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    dataset_version: str
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    total_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    metrics: dict[str, float] = Field(default_factory=dict)
    results: list[EvalResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    def failed_cases_count(self) -> int:
        return self.total_cases - self.passed_cases

    @computed_field
    def failures(self) -> list[dict[str, Any]]:
        return [
            {
                "case_id": result.case_id,
                "reason": ", ".join(result.failure_reasons),
                "actual": {
                    "intent": result.predicted_intent,
                    "tools": result.actual_tools,
                    "slots": result.actual_slots,
                    "phase": result.phase,
                },
            }
            for result in self.results
            if not result.passed
        ]


class EvaluationOutput(BaseModel):
    score: float = Field(ge=0, le=1)
    reason: str
