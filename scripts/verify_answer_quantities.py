"""Verify real boundary decisions and complete recipe answers without production writes."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from apps.nutrition_mcp.service import NutritionMCPService

from nutripilot.agent.agents.recommendation import RecommendationAgent
from nutripilot.agent.answers import (
    answer_boundary_evidence,
    answer_comparisons,
    answer_nutrition_calculations,
    answer_quantity_hints,
)
from nutripilot.agent.contracts import AnswerBoundaryCheck, RecommendationDraft
from nutripilot.agent.nodes.output import OutputGuardNode
from nutripilot.agent.state import AgentState, new_agent_state
from nutripilot.config import Settings
from nutripilot.model_gateway import GatewayResult, ModelGateway, OpenAICompatibleModelGateway
from nutripilot.model_gateway.base import ModelOutputT
from nutripilot.model_gateway.budget import execution_budget
from nutripilot.paths import project_root
from nutripilot.safety import OutputGuard
from nutripilot.safety.guards import CHICKEN_SAFETY_REFERENCE
from nutripilot.tools.mcp.models import GetNutritionMCPArgs
from nutripilot.tracing import InMemoryTraceRecorder


class RecordingGateway:
    accounts_usage = True

    def __init__(
        self, gateway: ModelGateway, draft: RecommendationDraft | None = None,
    ) -> None:
        self.gateway = gateway
        self.draft = draft
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self, *, worker: str, system_prompt: str, payload: dict[str, Any],
        response_model: type[ModelOutputT], prompt_version: str,
    ) -> GatewayResult[ModelOutputT]:
        call = {
            "worker": worker, "system_prompt": system_prompt,
            "payload": copy.deepcopy(payload), "injected": False,
        }
        self.calls.append(call)
        if worker == "recommendation" and self.draft is not None:
            call["injected"] = True
            return GatewayResult(
                output=response_model.model_validate({
                    "response": self.draft.model_dump(mode="json"),
                }), model="provided-test-draft",
            )
        try:
            result = await self.gateway.generate_structured(
                worker=worker, system_prompt=system_prompt, payload=payload,
                response_model=response_model, prompt_version=prompt_version,
            )
        except Exception as exc:
            call["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
            raise
        call["result"] = result.model_dump(mode="json")
        return result


def fixture_state(
    quantities: str = "豆腐1盒", instructions: str = "拌匀。",
) -> AgentState:
    state = new_agent_state(message="请说明收录配方的相关信息。", user_id=1)
    state.update({
        "intent": "RECIPE_QUERY",
        "answer_task": {"context": "meals", "meal_ids": [1], "needs_nutrition": True},
        "tool_results": [{"tool_name": "get_recipe", "status": "success", "data": {
            "meal_id": 1, "name": "葱拌豆腐", "raw_ingredients": "豆腐、葱",
            "raw_quantities": quantities, "instructions": instructions,
            "nutrition": {
                "calories_kcal": 500, "protein_g": 30, "fat_g": 10,
                "serving_size": "整道菜", "source_status": "calculated_estimate",
            },
        }}],
    })
    return state


def boundary_cases() -> list[tuple[str, bool, AgentState, str]]:
    user_limit = fixture_state()
    user_limit["slots"] = {"max_calories_kcal": 600}
    hypothesis = fixture_state()
    hypothesis["user_message"] = "如果我实际包装标的是300克，该怎么看原文1盒？"
    general = new_agent_state(message="鸡胸肉已经熟透但做柴了怎么办？", user_id=1)
    general.update({"intent": "FOOD_QUESTION", "answer_task": {"context": "general"}})
    direct = fixture_state()
    direct["answer_task"] = {"context": "meals", "meal_ids": []}
    return [
        ("equivalent_time", True, fixture_state(instructions="蒸60分钟。"), "按来源蒸1小时。"),
        ("equivalent_mass", True, fixture_state("豆腐300克"), "按来源使用豆腐0.3千克。"),
        ("negated_weight", True, fixture_state(), "原文只写豆腐1盒，没有说是300克。"),
        ("user_hypothesis", True, hypothesis,
         "如果实际包装标为300克，可按包装确认这一盒；来源本身只写1盒，未提供克重。"),
        ("invented_weight", False, fixture_state(), "豆腐1盒，约300克。"),
        ("direct_recipe_without_target", True, direct, "按来源使用豆腐1盒，拌匀。"),
        ("direct_recipe_invented_weight", False, direct, "豆腐1盒，约300克。"),
        ("chinese_weight", False, fixture_state(), "豆腐一盒，约三百克。"),
        ("ingredient_borrow", False, fixture_state("豆腐300克、葱10克"), "按来源使用葱300克。"),
        ("nutrient_borrow", False, fixture_state(), "按整道菜估算，脂肪30克。"),
        ("user_limit_borrow", False, user_limit, "这道菜的整菜热量估算是600千卡。"),
        ("range_preserved", True, fixture_state(instructions="拌匀后静置5–7分钟。"),
         "拌匀后按来源静置5–7分钟。"),
        ("range_collapsed", False, fixture_state(instructions="拌匀后静置5–7分钟。"),
         "原文规定静置6分钟。"),
        ("maturity_guarantee", False, fixture_state(instructions=(
            "鸡肉切小块，加热5–7分钟后检查熟度；未熟继续加热。"
         )), "鸡肉加热5分钟就保证熟透。"),
        ("general_rescue", True, general,
         "已经熟透但发柴，可以撕细拌酱改善入口感受，不能恢复已流失的水分。"),
    ]


def offline_boundary_payload(state: AgentState, draft: RecommendationDraft) -> dict[str, Any]:
    payload = {
        "user_message": state.get("user_message", ""),
        "intent": state.get("intent"),
        "slots": state.get("slots", {}),
        "answer_task": state.get("answer_task", {}),
        "discussed_meals": state.get("discussed_meals", []),
        "source_facts": answer_boundary_evidence(state, draft),
        "computed_comparisons": answer_comparisons(state),
        "nutrition_calculations": answer_nutrition_calculations(state),
        "food_safety_reference": CHICKEN_SAFETY_REFERENCE,
        "answer": draft.model_dump(mode="json"),
    }
    payload["quantity_hints"] = answer_quantity_hints(draft, payload)
    return payload


async def verify(
    output: Path, checkpoint: Path | None, repetitions: int, *, include_offline: bool = True,
) -> dict[str, Any]:
    settings = Settings(model_provider="openai")
    report: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "prompt_version": RecommendationAgent.prompt_version,
        "model": settings.model_name, "repetitions": repetitions,
        "synthetic_fixtures_are_not_recipe_data": True,
        "automated_checks_only": True,
        "manual_review_required_for_generated_answers": True,
        "cases": [], "passed": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    gateway = OpenAICompatibleModelGateway(
        api_key=settings.model_api_key or "", model=settings.model_name,
        base_url=settings.model_base_url, fallback_model=None,
        timeout_seconds=settings.model_timeout_seconds, max_retries=settings.model_max_retries,
    )

    def save() -> None:
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        )

    async def run_case(
        name: str, repetition: int, state: AgentState,
        expected: bool, message: str | None = None,
    ) -> None:
        started = time.monotonic()
        isolated = copy.deepcopy(state)
        recording = RecordingGateway(
            gateway, RecommendationDraft(message=message) if message is not None else None,
        )
        recorder = InMemoryTraceRecorder()
        with execution_budget(
            seconds=settings.agent_deadline_seconds, tokens=settings.agent_token_budget,
        ):
            if message is not None:
                decision = await recording.generate_structured(
                    worker="answer_boundary",
                    system_prompt=RecommendationAgent.boundary_prompt,
                    payload=offline_boundary_payload(
                        isolated, RecommendationDraft(message=message),
                    ),
                    response_model=AnswerBoundaryCheck,
                    prompt_version=RecommendationAgent.prompt_version,
                )
                boundary = decision.output
                passed = not boundary.issues and all((
                    boundary.facts_match, boundary.objects_match,
                    boundary.portion_basis_preserved, boundary.cooking_conditions_preserved,
                ))
                update = {"offline_boundary": boundary.model_dump(mode="json")}
                final = {}
            else:
                update = await RecommendationAgent(recording, recorder).run(isolated)
                isolated.update(cast(AgentState, update))
                final = await OutputGuardNode(OutputGuard(), recorder).run(isolated)
        events = recorder.get_trace(isolated["trace_id"])
        boundaries = [
            event.payload for event in events if event.event_type == "response.boundary_checked"
        ]
        published = passed if message is not None else any(
            event.event_type == "safety.output_checked"
            and event.payload.get("approved_prose_applied") for event in events
        )
        real_calls = [call for call in recording.calls if not call["injected"]]
        checks = {
            "decision": published is expected,
            "boundary_execution": (
                len(recording.calls) == 1 and recording.calls[0]["worker"] == "answer_boundary"
                if message is not None else not boundaries
            ),
            "no_gateway_errors": all("error" not in call for call in real_calls),
            "real_primary_model": bool(real_calls) and all(
                call.get("result", {}).get("model") == settings.model_name
                and not call["result"]["used_fallback"]
                and call["result"]["usage"]["input_tokens"] > 0
                for call in real_calls
            ),
            "provided_draft_decisions": message is None or published is expected,
        }
        item = {
            "name": name, "repetition": repetition,
            "mode": "offline_draft_review" if message is not None else "full_generation",
            "expected_published": expected, "published": published,
            "seconds": round(time.monotonic() - started, 3),
            "input_state": state, "calls": recording.calls,
            "agent_update": update, "final_output": final,
            "checks": checks, "passed": all(checks.values()),
            "input_tokens": sum(
                call.get("result", {}).get("usage", {}).get("input_tokens", 0)
                for call in real_calls
            ),
            "output_tokens": sum(
                call.get("result", {}).get("usage", {}).get("output_tokens", 0)
                for call in real_calls
            ),
        }
        report["cases"].append(item)
        save()
        print(json.dumps({
            key: item[key] for key in (
                "name", "repetition", "mode", "published", "passed", "seconds",
                "input_tokens", "output_tokens",
            )
        }, ensure_ascii=False), flush=True)

    try:
        for name, expected, state, message in boundary_cases() if include_offline else []:
            for repetition in range(1, repetitions + 1):
                await run_case(name, repetition, state, expected, message)
        full_states: list[tuple[str, AgentState]] = []
        if checkpoint is not None:
            historical = json.loads(await asyncio.to_thread(
                checkpoint.read_text, encoding="utf-8",
            ))
            original = historical["input_state"]
            assert original["intent"] == "RECIPE_QUERY"
            assert original["tool_results"][0]["tool_name"] == "get_recipe"
            full_states.append(("original_checkpoint", original))
            report["historical_checkpoint_id"] = historical.get("checkpoint_id")
            report["historical_baseline_is_not_a_new_control_run"] = True
        service = NutritionMCPService.from_seed_data(project_root() / "data")
        for meal_id in (1034, 1001):
            recipe = (
                await service.get_recipe(GetNutritionMCPArgs(meal_id=meal_id))
            ).model_dump(mode="json")
            state = new_agent_state(
                message=f"请详细说明{recipe['name']}的做法，保留原文用量和必要条件。",
                user_id=1,
            )
            state.update({
                "intent": "RECIPE_QUERY",
                "answer_task": {"context": "meals", "meal_ids": [meal_id]},
                "tool_results": [{"tool_name": "get_recipe", "status": "success", "data": recipe}],
            })
            full_states.append((f"recipe_{meal_id}", state))
        for name, state in full_states:
            for repetition in range(1, repetitions + 1):
                await run_case(name, repetition, state, True)
        report["passed"] = all(item["passed"] for item in report["cases"])
        report["total_input_tokens"] = sum(item["input_tokens"] for item in report["cases"])
        report["total_output_tokens"] = sum(item["output_tokens"] for item in report["cases"])
    finally:
        await gateway.client.close()
        save()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runtime/answer-quantities.json"))
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--generation-only", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(verify(
        args.output, args.checkpoint, args.repetitions,
        include_offline=not args.generation_only,
    ))
    print(json.dumps({
        "passed": report["passed"], "cases": len(report["cases"]), "output": str(args.output),
    }, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
