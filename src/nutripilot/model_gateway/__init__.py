"""Model gateway contracts and offline implementation."""

from nutripilot.model_gateway.base import (
    GatewayResult,
    ModelGateway,
    ModelGatewayError,
    ModelUsage,
)
from nutripilot.model_gateway.fake import FakeModelGateway
from nutripilot.model_gateway.openai_compatible import OpenAICompatibleModelGateway

__all__ = [
    "FakeModelGateway",
    "GatewayResult",
    "ModelGateway",
    "ModelGatewayError",
    "ModelUsage",
    "OpenAICompatibleModelGateway",
]
