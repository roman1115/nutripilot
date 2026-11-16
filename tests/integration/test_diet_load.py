"""Opt-in A13 workload on real PostgreSQL, scoped to a disposable test schema."""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import time
from collections import defaultdict
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_diet_api import payload
from tests.integration.test_diet_persistence import (  # noqa: F401
    ANCHOR,
    diet_engine,
    intake_request,
)

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.diet_management import DietManagement
from nutripilot.config import Settings
from nutripilot.infrastructure.diet_store import PostgresDietStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NUTRIPILOT_TEST_EXTERNAL") != "1"
        or os.getenv("NUTRIPILOT_TEST_DIET_LOAD") != "1",
        reason="Set NUTRIPILOT_TEST_EXTERNAL=1 and NUTRIPILOT_TEST_DIET_LOAD=1 for A13.",
    ),
]


async def test_twenty_owners_with_ten_thousand_records_each(diet_engine: AsyncEngine) -> None:  # noqa: F811
    tokens = {f"diet-load-owner-{owner:032d}": owner for owner in range(1, 21)}
    container = build_container(
        settings=Settings(
            _env_file=None,
            app_mode="test",
            api_access_tokens=tokens,
            user_timezone="UTC",
        )
    )
    template = await container.diet.intake.prepare(
        1,
        intake_request(),
        idempotency_key="template",
        anchor=ANCHOR,
    )
    container.diet = DietManagement(
        PostgresDietStore(diet_engine),
        container.diet.foods,
        default_timezone="UTC",
        cursor_secret=b"load-test-only" * 3,
    )
    async with diet_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO diet_user_state(user_id,timezone,updated_at) "
                "SELECT owner,'UTC',now() FROM generate_series(1,20) owner"
            )
        )
        await connection.execute(
            text("""
            INSERT INTO diet_logs(id,user_id,version,local_date,timezone,meal_type,channel,
                                  created_at,updated_at)
            SELECT md5(owner::text || ':' || n::text)::uuid,owner,1,
                   DATE '2026-11-12' - (n % 365),'UTC','lunch','manual',now(),now()
            FROM generate_series(1,20) owner CROSS JOIN generate_series(1,10000) n
        """)
        )
        await connection.execute(
            text("""
            INSERT INTO diet_log_items(id,user_id,diet_log_id,version,position,body,
                                       created_at,updated_at)
            SELECT md5(id::text || '_item')::uuid,user_id,id,1,0,
                   jsonb_set(CAST(:body AS jsonb),'{id}',to_jsonb(md5(id::text || '_item'))),
                   now(),now() FROM diet_logs
        """),
            {"body": template.content.items[0].model_dump_json()},
        )
        await connection.execute(text("ANALYZE diet_logs"))
        await connection.execute(text("ANALYZE diet_log_items"))
        counts = (
            await connection.execute(
                text("SELECT user_id,count(*) FROM diet_logs GROUP BY user_id ORDER BY user_id")
            )
        ).all()
        assert len(counts) == 20 and all(row[1] == 10000 for row in counts)
        db_version = (await connection.execute(text("SELECT version()"))).scalar_one()
        indexes = (
            (
                await connection.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
                        "AND tablename IN ('diet_logs','diet_log_items','diet_user_state') "
                        "ORDER BY indexname"
                    )
                )
            )
            .scalars()
            .all()
        )

    timings: dict[str, list[float]] = defaultdict(list)
    errors: list[dict[str, object]] = []
    transport = httpx.ASGITransport(app=create_app(container))

    async with httpx.AsyncClient(transport=transport, base_url="http://acceptance") as client:

        async def request(owner: int, operation: str, method: str, path: str, **kwargs):
            headers = {
                "Authorization": f"Bearer diet-load-owner-{owner:032d}",
                "Idempotency-Key": uuid4().hex,
                **kwargs.pop("headers", {}),
            }
            started = time.perf_counter()
            response = await client.request(
                method, "/api/v1/diet" + path, headers=headers, **kwargs
            )
            timings[operation].append(time.perf_counter() - started)
            if response.status_code not in (200, 201):
                errors.append(
                    {
                        "operation": operation,
                        "status": response.status_code,
                        "body": response.text[:500],
                    }
                )
            assert response.status_code in (200, 201), response.text
            return response.json()

        async def confirm(owner: int, preview: dict, phase: str):
            return await request(
                owner,
                phase,
                "POST",
                f"/previews/{preview['id']}/confirm",
                headers={"If-Match": f'"{preview["version"]}"'},
                json={
                    "approved": True,
                    "preview_version": preview["version"],
                    "content_hash": preview.get("preview_hash", preview.get("content_hash")),
                },
            )

        async def exercise(owner: int) -> None:
            for _ in range(5):
                draft = await request(owner, "create_prepare", "POST", "/drafts", json=payload())
                saved = await confirm(owner, draft, "create_confirm")
                repeated = await confirm(owner, draft, "confirm_replay")
                assert saved["id"] == repeated["id"]
                record_id = saved["result_refs"][0]["id"]
                record = await request(owner, "read", "GET", f"/logs/{record_id}")
                assert record["user_id"] == owner
                other = owner % 20 + 1
                forbidden = await client.get(
                    f"/api/v1/diet/logs/{record_id}",
                    headers={
                        "Authorization": f"Bearer diet-load-owner-{other:032d}",
                    },
                )
                assert forbidden.status_code == 404, forbidden.text
                for action, version in (("correct", 1), ("delete", 2)):
                    arguments = {
                        "action": action,
                        "record_id": record_id,
                        "base_version": version,
                        "evidence": payload()["evidence"],
                    }
                    if action == "correct":
                        arguments["patch"] = {"temporal": {"note": "load-test-correction"}}
                    preview = await request(
                        owner,
                        action + "_prepare",
                        "POST",
                        "/previews",
                        headers={"If-Match": f'"{version}"'},
                        json={"operation": "log_" + action, "arguments": arguments},
                    )
                    await confirm(owner, preview, action + "_confirm")
                date_range = "?start_date=2026-11-12&end_date=2026-11-12"
                await request(owner, "day_list", "GET", "/logs" + date_range)
                await request(owner, "day_summary", "GET", "/summary" + date_range)
                async with container.diet.store.transaction(owner, default_timezone="UTC") as tx:
                    assert (await tx.get_log(UUID(record_id))).deleted_at is not None

        started = time.perf_counter()
        await asyncio.gather(*(exercise(owner) for owner in range(1, 21)))
        elapsed = time.perf_counter() - started

    metrics = {
        name: {
            "samples": len(values),
            "p50_ms": round(sorted(values)[len(values) // 2] * 1000, 2),
            "p95_ms": round(sorted(values)[math.ceil(len(values) * 0.95) - 1] * 1000, 2),
            "max_ms": round(max(values) * 1000, 2),
        }
        for name, values in timings.items()
    }
    report = {
        "acceptance": "A13",
        "host": platform.platform(),
        "cpu": platform.processor(),
        "cpu_count": os.cpu_count(),
        "database": db_version,
        "indexes": list(indexes),
        "owners": 20,
        "records_per_owner": 10000,
        "items_per_record": 1,
        "date_distribution": "uniform modulo 365 days ending 2026-11-12",
        "transport": "real ASGI API/auth/serialization + PostgreSQL; no network/model",
        "pool_size": diet_engine.pool.size(),
        "concurrent_users": 20,
        "rounds_per_user": 5,
        "elapsed_seconds": round(elapsed, 2),
        "metrics": metrics,
        "errors": errors,
        "error_rate": len(errors) / sum(map(len, timings.values())),
    }
    directory = Path(".runtime/diet-acceptance-20261112")
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        (directory / "load.json").write_text, json.dumps(report, ensure_ascii=False, indent=2)
    )
    assert not errors
    assert all(value["p95_ms"] <= 1000 for value in metrics.values()), metrics
