"""Offline-only subjective quality Worker Agent."""

from __future__ import annotations

from typing import Any

from nutripilot.evaluation.models import EvaluationOutput
from nutripilot.model_gateway import ModelGateway


class EvaluationAgent:
    name = "EvaluationAgent"
    prompt_version = "evaluation-v1"
    system_prompt = (
        "你是离线质量评价 Worker，只评价回复是否清晰且有依据。"
        "不得参与在线请求路由，必须返回0到1的分数和简短原因。"
    )

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def evaluate(self, payload: dict[str, Any]) -> EvaluationOutput:
        result = await self.gateway.generate_structured(
            worker="evaluation",
            system_prompt=self.system_prompt,
            payload=payload,
            response_model=EvaluationOutput,
            prompt_version=self.prompt_version,
        )
        return result.output
