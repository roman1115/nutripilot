"""FastAPI factory used by local execution, Docker, and tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from nutripilot.agent.state import AgentState
from nutripilot.api.schemas import (
    ChatRequest,
    ChatResponse,
    ConfirmationRequest,
    EvaluationRequest,
    ProfileUpdateRequest,
)
from nutripilot.application import (
    ApplicationContainer,
    EvaluationNotFoundError,
    build_container,
)
from nutripilot.application.chat_service import (
    InvalidConfirmationStateError,
    ThreadNotFoundError,
)
from nutripilot.application.runtime import start_container, stop_container
from nutripilot.evaluation import EvalReport, load_dataset
from nutripilot.paths import project_root
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.models import FeedbackRecord, RecordFeedbackArgs, UserProfile


def create_app(container: ApplicationContainer | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = cast("ApplicationContainer", app.state.container)
        await start_container(active)
        try:
            yield
        finally:
            await stop_container(active)

    app = FastAPI(
        title="NutriPilot API",
        version="1.0.0",
        description="Observable multi-agent nutrition decision API",
        lifespan=lifespan,
    )
    app.state.container = container or build_container()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app.state.container.settings.web_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_container(request: Request) -> ApplicationContainer:
        return cast("ApplicationContainer", request.app.state.container)

    def get_user_id(x_user_id: int = Header(default=1, alias="X-User-ID", gt=0)) -> int:
        return x_user_id

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        active = app.state.container.settings
        return {
            "status": "ready",
            "app_mode": active.app_mode,
            "model_provider": active.model_provider,
            "model_name": active.model_name if not active.uses_fake_model else "fake-nutripilot-v1",
            "embedding_provider": active.embedding_provider,
            "embedding_model": active.embedding_model,
            "rerank_provider": active.rerank_provider,
            "rerank_model": active.rerank_model,
            "storage": "postgresql" if active.uses_external_infrastructure else "memory",
            "mcp_transport": (
                "streamable-http" if active.uses_external_infrastructure else "in-process"
            ),
        }

    @app.post("/api/v1/chat", response_model=ChatResponse, tags=["chat"])
    async def chat(
        payload: ChatRequest,
        container: ApplicationContainer = Depends(get_container),
    ) -> ChatResponse:
        try:
            state = await container.chat_service.chat(
                user_id=payload.user_id,
                message=payload.message,
                thread_id=payload.thread_id,
            )
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        return ChatResponse.from_state(state)

    @app.post("/api/v1/chat/stream", tags=["chat"])
    async def chat_stream(
        payload: ChatRequest,
        container: ApplicationContainer = Depends(get_container),
    ) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            try:
                async for event_type, data in container.chat_service.stream(
                    user_id=payload.user_id,
                    message=payload.message,
                    thread_id=payload.thread_id,
                ):
                    if event_type == "completed":
                        state = cast("AgentState", data)
                        yield _sse(
                            "completed",
                            ChatResponse.from_state(state).model_dump(mode="json"),
                        )
                    else:
                        yield _sse("status" if event_type == "started" else event_type, data)
            except ThreadNotFoundError:
                yield _sse("error", {"message": "Thread not found"})

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/api/v1/chat/{thread_id}/confirm", response_model=ChatResponse, tags=["chat"])
    async def confirm(
        thread_id: str,
        payload: ConfirmationRequest,
        container: ApplicationContainer = Depends(get_container),
    ) -> ChatResponse:
        try:
            state = await container.chat_service.confirm(
                thread_id=thread_id,
                user_id=payload.user_id,
                approved=payload.approved,
                confirmation_id=payload.confirmation_id,
            )
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        except InvalidConfirmationStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ChatResponse.from_state(state)

    @app.get("/api/v1/threads/{thread_id}", response_model=ChatResponse, tags=["chat"])
    async def thread(
        thread_id: str,
        container: ApplicationContainer = Depends(get_container),
        user_id: int = Query(default=1, gt=0),
    ) -> ChatResponse:
        try:
            state = await container.chat_service.get_thread(thread_id=thread_id, user_id=user_id)
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        return ChatResponse.from_state(state)

    @app.get("/api/v1/traces/{trace_id}", tags=["traces"])
    async def trace(
        trace_id: str,
        container: ApplicationContainer = Depends(get_container),
    ) -> dict[str, object]:
        return {
            "trace_id": trace_id,
            "events": [
                event.model_dump(mode="json") for event in container.recorder.get_trace(trace_id)
            ],
        }

    @app.get("/api/v1/profile", response_model=UserProfile, tags=["profile"])
    async def get_profile(
        container: ApplicationContainer = Depends(get_container),
        user_id: int = Depends(get_user_id),
    ) -> UserProfile:
        return UserProfile.model_validate(await container.data_store.get_user_profile(user_id))

    @app.put("/api/v1/profile", response_model=UserProfile, tags=["profile"])
    async def update_profile(
        payload: ProfileUpdateRequest,
        container: ApplicationContainer = Depends(get_container),
        user_id: int = Depends(get_user_id),
    ) -> UserProfile:
        return UserProfile.model_validate(
            await container.data_store.update_user_profile(user_id, payload.to_domain())
        )

    @app.get("/api/v1/meal-plans", tags=["meal-plans"])
    async def get_meal_plans(
        container: ApplicationContainer = Depends(get_container),
        user_id: int = Depends(get_user_id),
    ) -> list[dict[str, Any]]:
        plans = await container.data_store.list_plans(user_id)
        response: list[dict[str, Any]] = []
        for plan in plans:
            items: list[dict[str, Any]] = []
            for index, item in enumerate(plan.items):
                meal = await container.data_store.get_meal(item.meal_id)
                items.append(
                    {
                        "id": f"{plan.id}:{index}",
                        "date": item.meal_date.isoformat(),
                        "meal_type": item.meal_type,
                        "meal_id": item.meal_id,
                        "meal_name": meal.name if meal else f"餐食 {item.meal_id}",
                        "calories": meal.nutrition.calories_kcal if meal else None,
                        "note": item.note,
                    }
                )
            response.append(
                {
                    **plan.model_dump(mode="json", exclude={"items"}),
                    "status": "active",
                    "items": items,
                }
            )
        return response

    @app.post("/api/v1/feedback", response_model=FeedbackRecord, tags=["feedback"])
    async def record_feedback(
        payload: RecordFeedbackArgs,
        container: ApplicationContainer = Depends(get_container),
        user_id: int = Depends(get_user_id),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> FeedbackRecord:
        try:
            return FeedbackRecord.model_validate(
                await container.data_store.record_feedback(
                    user_id,
                    payload,
                    idempotency_key or str(uuid4()),
                )
            )
        except ToolRuntimeError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": exc.code, "message": exc.safe_message},
            ) from exc

    @app.post("/api/v1/evaluations/run", response_model=EvalReport, tags=["evaluation"])
    async def run_evaluation(
        payload: EvaluationRequest,
        container: ApplicationContainer = Depends(get_container),
    ) -> EvalReport:
        if payload.cases is None:
            file_version, cases = load_dataset(_default_eval_dataset())
            dataset_version = payload.dataset_version or file_version
        else:
            cases = payload.cases
            dataset_version = payload.dataset_version or "api-v1"
        return container.evaluation_service.start(
            cases,
            dataset_version=dataset_version,
        )

    @app.get(
        "/api/v1/evaluations/{run_id}",
        response_model=EvalReport,
        tags=["evaluation"],
    )
    async def get_evaluation(
        run_id: str,
        container: ApplicationContainer = Depends(get_container),
    ) -> EvalReport:
        try:
            return container.evaluation_service.get(run_id)
        except EvaluationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Evaluation run not found") from exc

    return app


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _default_eval_dataset() -> Path:
    return project_root() / "evals" / "datasets" / "core.json"
