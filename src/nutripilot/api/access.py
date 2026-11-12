"""Identity and per-user request limits for the single-instance personal deployment."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from nutripilot.config import Settings


class AccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self.requests: dict[int, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)
        if not self.settings.api_access_tokens:
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        identity = next(
            (
                user_id
                for token, user_id in self.settings.api_access_tokens.items()
                if hmac.compare_digest(supplied.encode(), token.encode())
            ),
            None,
        )
        if identity is None:
            return JSONResponse(
                {"detail": "需要有效的访问令牌"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.user_id = identity
        now = time.monotonic()
        timestamps = self.requests[identity]
        while timestamps and timestamps[0] <= now - 60:
            timestamps.popleft()
        if len(timestamps) >= self.settings.api_rate_limit_per_minute:
            return JSONResponse(
                {"detail": "请求过于频繁，请稍后重试"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        timestamps.append(now)
        return await call_next(request)
