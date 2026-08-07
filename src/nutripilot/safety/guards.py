"""Safety rules that models cannot override."""


from __future__ import annotations

import re

from pydantic import BaseModel, Field

from nutripilot.domain.enums import RiskLevel
from nutripilot.domain.models import FinalResponse

_ALLERGEN_SPLIT_RE = re.compile(r"[、，,/]|(?:和|及|与)")
_EXPLICIT_ALLERGY_RE = re.compile(
    r"(?:我|本人)?对(?:吃|食用)?(?P<items>[\u4e00-\u9fffa-z0-9、，,/和及与]{1,30}?)"
    r"(?:会|容易|可能)?过敏",
    re.IGNORECASE,
)
_BARE_ALLERGY_RE = re.compile(
    r"(?:^|[\s，,。；;！？!?])(?:而且|并且|另外|还|又)?(?:吃|食用)?"
    r"(?P<items>[\u4e00-\u9fffa-z0-9、/和及与]{1,30}?)(?:会|容易|可能)?过敏",
    re.IGNORECASE,
)


def extract_declared_allergens(message: str) -> list[str]:
    """Extract arbitrary user-declared foods from explicit allergy statements."""

    matches = list(_EXPLICIT_ALLERGY_RE.finditer(message))
    if not matches:
        matches = list(_BARE_ALLERGY_RE.finditer(message))
    allergens: list[str] = []
    for match in matches:
        for raw_item in _ALLERGEN_SPLIT_RE.split(match.group("items")):
            item = re.sub(r"^(?:而且|并且|另外|还|又)", "", raw_item.strip()).casefold()
            if item and item not in {"食物", "食品", "东西", "某些食物"}:
                allergens.append(item)
    return list(dict.fromkeys(allergens))


class RiskAssessment(BaseModel):
    level: RiskLevel = RiskLevel.NONE
    flags: list[str] = Field(default_factory=list)
    blocked: bool = False
    safe_message: str | None = None


class RiskGuard:
    _INJECTION_TERMS = (
        "忽略之前的指令",
        "忽略系统提示",
        "显示系统提示词",
        "泄露提示词",
        "ignore previous instructions",
        "reveal system prompt",
        "执行shell",
        "执行sql",
    )
    _MEDICAL_TERMS = ("诊断", "处方", "停药", "换药", "治疗方案", "药物剂量", "药物")
    _EXTREME_TERMS = ("绝食", "催吐", "一天不吃", "每天只吃", "快速瘦20斤")
    def assess(self, message: str) -> RiskAssessment:
        normalized = message.casefold()
        if any(term in normalized for term in self._INJECTION_TERMS):
            return RiskAssessment(
                level=RiskLevel.HIGH,
                flags=["PROMPT_INJECTION"],
                blocked=True,
                safe_message="我不能修改系统规则、泄露隐藏指令或执行未授权操作。",
            )
        if any(term in normalized for term in self._MEDICAL_TERMS):
            return RiskAssessment(
                level=RiskLevel.HIGH,
                flags=["MEDICAL_BOUNDARY"],
                blocked=True,
                safe_message=(
                    "我不能提供疾病诊断、处方或用药调整。请咨询医生或注册营养师；"
                    "我可以在一般饮食信息范围内协助你。"
                ),
            )
        if any(term in normalized for term in self._EXTREME_TERMS):
            return RiskAssessment(
                level=RiskLevel.HIGH,
                flags=["EXTREME_DIETING"],
                blocked=True,
                safe_message="这个目标可能伤害健康，我不能协助极端节食。建议寻求专业帮助。",
            )
        allergens = extract_declared_allergens(normalized)
        if allergens:
            return RiskAssessment(
                level=RiskLevel.LOW, flags=[f"ALLERGEN:{item}" for item in allergens]
            )
        return RiskAssessment()


class OutputGuard:
    _MEDICAL_CLAIMS = ("确诊", "治愈", "替代药物", "停药", "处方")

    def validate(
        self,
        response: FinalResponse,
        *,
        allowed_meal_ids: set[int],
        forbidden_meal_ids: set[int] | None = None,
    ) -> tuple[FinalResponse, list[str]]:
        flags: list[str] = []
        forbidden_meal_ids = forbidden_meal_ids or set()
        supported = [
            item for item in response.recommendations if item.meal_id in allowed_meal_ids
        ]
        if len(supported) != len(response.recommendations):
            flags.append("UNSUPPORTED_MEAL_ID_REMOVED")
        valid = [item for item in supported if item.meal_id not in forbidden_meal_ids]
        safety_items_removed = len(valid) != len(supported)
        if safety_items_removed:
            flags.append("DIETARY_CONSTRAINT_VIOLATION_REMOVED")
        message = response.message
        if any(term in message for term in self._MEDICAL_CLAIMS):
            flags.append("MEDICAL_CLAIM_BLOCKED")
            return (
                FinalResponse(
                    status="blocked",
                    message="该回复触及医疗建议边界，已被安全检查阻止。",
                    warnings=["请咨询医生或注册营养师。"],
                ),
                flags,
            )
        warnings = list(response.warnings)
        if safety_items_removed:
            warnings.append("已自动移除与过敏或忌口约束冲突的餐食。")
            message = (
                "已移除与过敏或忌口约束冲突的餐食，其余推荐如下。"
                if valid
                else "没有找到同时满足过敏与忌口约束的安全餐食，请调整非安全条件后重试。"
            )
        if not safety_items_removed and response.recommendations and not valid:
            message = "生成结果缺少可验证的餐食依据，请重新检索后再试。"
        valid_ids = {item.meal_id for item in valid}
        removed_ids = {
            item.meal_id for item in response.recommendations if item.meal_id not in valid_ids
        }
        removed_sources = {f"meal:{meal_id}" for meal_id in removed_ids}
        citations = [
            citation for citation in response.citations if citation.source_id not in removed_sources
        ]
        return (
            response.model_copy(
                update={
                    "message": message,
                    "recommendations": valid,
                    "citations": citations,
                    "warnings": list(dict.fromkeys(warnings)),
                }
            ),
            flags,
        )
