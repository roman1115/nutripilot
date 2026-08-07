"""Domain contracts shared by agents, tools, and APIs."""

from nutripilot.domain.enums import Intent, Phase, RiskLevel
from nutripilot.domain.models import (
    Citation,
    FinalResponse,
    MealRecommendation,
)

__all__ = [
    "Citation",
    "FinalResponse",
    "Intent",
    "MealRecommendation",
    "Phase",
    "RiskLevel",
]
