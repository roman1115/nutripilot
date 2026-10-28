from __future__ import annotations

import inspect
import json
import os
from typing import Any, Literal

from apps.nutrition_mcp.service import NutritionService, build_default_service
from nutripilot.tools.errors import ToolRuntimeError

MCP_PATH = "/mcp"
MCP_TOKEN_ENV = "MCP_INTERNAL_TOKEN"


def create_mcp_server(service: NutritionService | None = None) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError as exc:
        raise RuntimeError("The official mcp package could not initialize") from exc

    service = service or build_default_service()
    server = FastMCP(
        "NutriPilot Nutrition Tools",
        stateless_http=True,
        json_response=True,
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_allowed_mcp_hosts(),
        ),
    )

    @server.tool(
        name="search_meals",
        description="Search safe meal candidates with structured filters and hybrid retrieval.",
    )
    async def search_meals(
        query: str,
        meal_type: str | None = None,
        meal_role: Literal["complete_meal", "dish", "staple", "soup", "snack"] | None = None,
        cuisines: list[str] | None = None,
        tastes: list[str] | None = None,
        health_goals: list[str] | None = None,
        dietary_tags: list[str] | None = None,
        excluded_allergens: list[str] | None = None,
        excluded_ingredients: list[str] | None = None,
        excluded_meal_ids: list[int] | None = None,
        max_calories_kcal: float | None = None,
        min_protein_g: float | None = None,
        max_prep_minutes: int | None = None,
        max_price_cny: float | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        return await _safe_call(
            service,
            "search_meals",
            {
                "query": query,
                "meal_type": meal_type,
                "meal_role": meal_role,
                "cuisines": cuisines or [],
                "tastes": tastes or [],
                "health_goals": health_goals or [],
                "dietary_tags": dietary_tags or [],
                "excluded_allergens": excluded_allergens or [],
                "excluded_ingredients": excluded_ingredients or [],
                "excluded_meal_ids": excluded_meal_ids or [],
                "max_calories_kcal": max_calories_kcal,
                "min_protein_g": min_protein_g,
                "max_prep_minutes": max_prep_minutes,
                "max_price_cny": max_price_cny,
                "limit": limit,
            },
        )

    @server.tool(
        name="get_nutrition",
        description="Read nutrition, ingredients and allergens for an authoritative meal.",
    )
    async def get_nutrition(meal_id: int | None = None, name: str | None = None) -> dict[str, Any]:
        return await _safe_call(service, "get_nutrition", {"meal_id": meal_id, "name": name})

    @server.tool(
        name="query_dietary_guidelines",
        description="Search sourced dietary guidance with a safety disclaimer.",
    )
    async def query_dietary_guidelines(query: str, limit: int = 3) -> dict[str, Any]:
        return await _safe_call(
            service, "query_dietary_guidelines", {"query": query, "limit": limit}
        )

    return server


def _allowed_mcp_hosts() -> list[str]:
    configured = os.getenv("MCP_ALLOWED_HOSTS", "")
    if configured.strip():
        return [host.strip() for host in configured.split(",") if host.strip()]
    return ["127.0.0.1:*", "localhost:*", "nutrition-mcp:*"]


async def _safe_call(
    service: NutritionService, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    try:
        return await service.call_tool(name, arguments)
    except ToolRuntimeError as exc:
        return {
            "_tool_error": {
                "code": exc.code,
                "message": exc.safe_message,
                "retryable": exc.retryable,
                "details": exc.details,
            }
        }


class BearerTokenMiddleware:
    def __init__(self, app: Any, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self.token and scope.get("type") == "http":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            if headers.get("authorization") != f"Bearer {self.token}":
                body = json.dumps({"error": "unauthorized"}, separators=(",", ":")).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class ServiceLifecycleMiddleware:
    def __init__(self, app: Any, service: NutritionService) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        finally:
            if scope.get("type") == "lifespan":
                close = getattr(self.service, "close", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result


def create_app(service: NutritionService | None = None, *, token: str | None = None) -> Any:
    active_service = service or build_default_service()
    server = create_mcp_server(active_service)
    configured_token = token if token is not None else os.getenv(MCP_TOKEN_ENV)
    if (
        os.getenv("APP_MODE", "local").casefold() in {"docker", "production"}
        and not configured_token
    ):
        raise RuntimeError(f"{MCP_TOKEN_ENV} is required outside local development")
    app = ServiceLifecycleMiddleware(server.streamable_http_app(), active_service)
    return BearerTokenMiddleware(app, configured_token)


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required to run the MCP server") from exc
    host = os.getenv("NUTRIPILOT_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("NUTRIPILOT_MCP_PORT", "8010"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
