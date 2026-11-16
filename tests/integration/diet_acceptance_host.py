"""Explicit Fake-only browser host; never imports real credentials or changes business data."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from tests.integration.test_diet_conversation import request as intake_request
from tests.integration.test_diet_image_estimates import photo_output
from tests.integration.test_diet_jobs import parsed
from tests.integration.test_diet_labels import label_output

from nutripilot.api.app import create_app as api_app
from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.model_gateway import FakeModelGateway


def create_app() -> FastAPI:
    gateway = FakeModelGateway()

    def parse(payload):
        first = parsed()["entries"][0]
        second = {
            **first,
            "temporal": {**first["temporal"], "meal_type": "dinner"},
        }
        return {"entries": [first, second]}

    gateway.register_handler("diet_parser", parse)
    gateway.register_handler(
        "intent",
        lambda _: {
            "intent": "DIET_MANAGEMENT",
            "confidence": 1,
            "extracted_slots": {},
            "missing_slots": [],
            "diet_operation": "intake",
        },
    )
    gateway.register_handler(
        "diet_planner",
        lambda payload: {
            "complete": False,
            "plan_summary": "Fake browser intake fixture",
            "tool_calls": [
                {
                    "name": "prepare_intake_draft",
                    "call_id": "browser-fixture-only",
                    "arguments": intake_request(payload["turn_id"]),
                }
            ],
        },
    )
    active = build_container(
        settings=Settings(
            _env_file=None,
            app_mode="test",
            user_timezone="UTC",
            web_origin="http://127.0.0.1:5190",
            diet_image_enabled=True,
            vision_model_name="fake-browser-only",
            diet_media_root=str(Path(".runtime/diet-browser-current/media").absolute()),
            diet_export_root=str(Path(".runtime/diet-browser-current/exports").absolute()),
        ),
        gateway=gateway,
    )
    app = api_app(active)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(app):
            assert active.diet_parsing and active.diet_parsing.vision
            vision = active.diet_parsing.vision.delegate
            assert isinstance(vision, FakeModelGateway)
            vision.register_handler("diet_label_parser", lambda _: label_output())
            vision.register_handler("diet_parser", lambda _: photo_output())
            yield

    app.router.lifespan_context = lifespan
    return app
