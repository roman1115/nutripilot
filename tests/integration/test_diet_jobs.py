"""Task completion never approves food; delayed results and leases cannot overwrite users."""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.diet_parsing import TextDraft
from nutripilot.config import Settings
from nutripilot.domain.diet import DietError, DraftEntry, content_hash
from nutripilot.domain.execution import ExecutionOwnershipLost
from nutripilot.model_gateway import FakeModelGateway


def parsed() -> dict:
    return {
        "entries": [
            {
                "actuality": "actual",
                "subject": "self",
                "temporal": {"local_date": "2026-11-12", "meal_type": "lunch"},
                "items": [
                    {
                        "client_item_key": "rice",
                        "food": {"kind": "unknown", "name": "米饭"},
                        "quantity": {"kind": "unknown", "description": "半碗"},
                    }
                ],
            }
        ]
    }


def test_independent_text_job_persists_events_and_requires_explicit_confirmation() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response("diet_parser", parsed())
    active = build_container(
        settings=Settings(_env_file=None, app_mode="test", user_timezone="UTC"), gateway=gateway
    )
    active.diet.clock = lambda: datetime(2026, 11, 12, 0, tzinfo=UTC)
    active.diet.intake.clock = active.diet.clock
    with TestClient(create_app(active)) as client:
        response = client.post(
            "/api/v1/diet/drafts",
            json={"mode": "text", "text": "今天午餐吃了半碗米饭"},
            headers={"Idempotency-Key": "text-1"},
        )
        assert response.status_code == 201, response.text
        draft = response.json()
        assert draft["job_id"] and draft["content"]["items"] == []
        for _ in range(200):
            job = client.get(f"/api/v1/diet/jobs/{draft['job_id']}").json()
            if job["state"] not in ("queued", "running"):
                break
            import time

            time.sleep(0.02)
        assert job["state"] == "succeeded", job
        assert job["usage"]["result"] == "applied"
        result = client.get(f"/api/v1/diet/drafts/{draft['id']}").json()
        assert result["version"] == 2 and result["state"] == "ready"
        assert result["content"]["items"][0]["nutrition_snapshot"]["calories_kcal"]["value"] is None
        assert (
            client.get("/api/v1/diet/logs?start_date=2026-11-12&end_date=2026-11-12").json()[
                "items"
            ]
            == []
        )
        events = client.get(f"/api/v1/diet/jobs/{draft['job_id']}/events").json()["items"]
        assert [event["state"] for event in events] == ["queued", "running", "succeeded"]
        assert "米饭" not in str(events)
        approved = client.post(
            f"/api/v1/diet/previews/{draft['id']}/confirm",
            json={
                "approved": True,
                "preview_version": result["version"],
                "content_hash": result["preview_hash"],
            },
            headers={"Idempotency-Key": "approve", "If-Match": '"2"'},
        )
        assert approved.status_code == 200, approved.text


@pytest.mark.asyncio
async def test_lease_recovery_and_cancel_fence_old_attempt() -> None:
    from nutripilot.application.runtime import start_container, stop_container

    active = build_container(settings=Settings(_env_file=None, app_mode="test"))
    await start_container(active)
    try:
        await active.chat_worker.close()
        await active.chat_worker_task
        assert active.diet_parsing is not None
        parser = active.diet_parsing
        draft = await parser.create(1, TextDraft(mode="text", text="午餐"), key="lease")
        job = await asyncio.to_thread(parser.jobs.claim, 4, 3)
        assert job is not None
        await asyncio.to_thread(parser.jobs.recover, startup=True)
        with pytest.raises(ExecutionOwnershipLost):
            await asyncio.to_thread(parser.jobs.finish, job)
        resumed = await asyncio.to_thread(parser.jobs.claim, 4, 3)
        assert resumed and resumed.attempt == 2
        assert resumed.budget.deadline_at == job.budget.deadline_at
        await asyncio.to_thread(parser.jobs.cancel, resumed.id, 1)
        with pytest.raises(ExecutionOwnershipLost):
            await asyncio.to_thread(parser.jobs.account, resumed, 1)
        assert (await parser.diet.preview(1, draft.id)).state == "needs_input"
        with pytest.raises(DietError) as exc:
            await asyncio.to_thread(parser.jobs.get, resumed.id, 2)
        assert exc.value.code == "NOT_FOUND"
    finally:
        await stop_container(active)


@pytest.mark.asyncio
@pytest.mark.parametrize("locked_same_version", [False, True])
async def test_late_parse_only_proposes_and_never_overwrites_manual_fields(
    locked_same_version: bool,
) -> None:
    from nutripilot.application.runtime import start_container, stop_container

    active = build_container(settings=Settings(_env_file=None, app_mode="test"))
    await start_container(active)
    try:
        await active.chat_worker.close()
        await active.chat_worker_task
        parser = active.diet_parsing
        assert parser is not None
        draft = await parser.create(1, TextDraft(mode="text", text="午餐"), key="late")
        if locked_same_version:
            content = draft.content.model_copy(update={"field_locks": ["items"]})
            draft = draft.model_copy(
                update={"content": content, "preview_hash": content_hash(content)}
            )
            async with active.diet.store.transaction(1, default_timezone="UTC") as tx:
                await tx.save_draft(draft)
        job = await asyncio.to_thread(parser.jobs.claim, 4, 3)
        assert job
        if not locked_same_version:
            async with active.diet.store.transaction(1, default_timezone="UTC") as tx:
                await tx.save_draft(draft.model_copy(update={"version": 2}))
        result, _ = await parser.apply(job, DraftEntry.model_validate(parsed()["entries"][0]))
        assert result == "suggestion"
        current = await active.diet.preview(1, draft.id)
        assert current.content.items == [] and current.version == 2
        assert current.parse_suggestion is not None
    finally:
        await stop_container(active)


@pytest.mark.asyncio
async def test_multiple_events_split_atomically_preserve_expiry_and_never_auto_commit() -> None:
    from nutripilot.application.runtime import start_container, stop_container
    from nutripilot.domain.diet_jobs import ParsedIntake

    gateway = FakeModelGateway()
    first = parsed()["entries"][0]
    second = {**first, "temporal": {"meal_type": "dinner"}, "actuality": "planned"}
    gateway.queue_response("diet_parser", {"entries": [first, second]})
    active = build_container(
        settings=Settings(_env_file=None, app_mode="test", user_timezone="UTC"), gateway=gateway
    )
    active.diet.clock = lambda: datetime(2026, 11, 12, 0, tzinfo=UTC)
    active.diet.intake.clock = active.diet.clock
    await start_container(active)
    try:
        await active.chat_worker.close()
        await active.chat_worker_task
        parser = active.diet_parsing
        assert parser is not None
        draft = await parser.create(1, TextDraft(mode="text", text="午餐和明天晚餐"), key="multi")
        job = await asyncio.to_thread(parser.jobs.claim, 4, 3)
        assert job
        result, _ = await parser.execute(job)
        assert result == "not_applied"
        source = await active.diet.preview(1, draft.id)
        assert source.version == 2 and len(source.parse_candidates) == 2
        with pytest.raises(DietError) as exc:
            await parser.split(2, draft.id, 2, key="split")
        assert exc.value.code == "NOT_FOUND"
        with pytest.raises(DietError) as exc:
            await parser.split(1, draft.id, 1, key="split")
        assert exc.value.code == "VERSION_CONFLICT"
        children = await parser.split(1, draft.id, 2, key="split")
        assert len(children) == 2 and len({child.id for child in children}) == 2
        assert [child.state for child in children] == ["ready", "needs_input"]
        assert all(
            child.expires_at == source.expires_at and child.job_id is None for child in children
        )
        assert all(
            child.content.entry.temporal == original.temporal
            for child, original in zip(
                children,
                ParsedIntake.model_validate({"entries": [first, second]}).entries,
                strict=True,
            )
        )
        assert [child.id for child in await parser.split(1, draft.id, 2, key="split")] == [
            child.id for child in children
        ]
        assert (await active.diet.preview(1, draft.id)).state == "discarded"
        async with active.diet.store.transaction(1, default_timezone="UTC") as tx:
            assert tx.state.diet_epoch == 0
            assert not await tx.list_logs(
                source.content.anchor.anchor_local_date, source.content.anchor.anchor_local_date
            )
        saved = await active.diet.confirm(
            1, children[0].id, version=1, approved_hash=children[0].preview_hash
        )
        assert saved.result_status == "committed"
        assert (await active.diet.preview(1, children[1].id)).state == "needs_input"
    finally:
        await stop_container(active)


@pytest.mark.asyncio
async def test_reparse_merges_unlocked_fields_and_preserves_quantity_identity() -> None:
    from nutripilot.application.runtime import start_container, stop_container
    from nutripilot.domain.diet import MassQuantity

    active = build_container(
        settings=Settings(_env_file=None, app_mode="test", user_timezone="UTC")
    )
    active.diet.clock = lambda: datetime(2026, 11, 12, 0, tzinfo=UTC)
    active.diet.intake.clock = active.diet.clock
    await start_container(active)
    try:
        await active.chat_worker.close()
        await active.chat_worker_task
        parser = active.diet_parsing
        draft = await parser.create(1, TextDraft(mode="text", text="午餐"), key="lock-merge")
        job = await asyncio.to_thread(parser.jobs.claim, 4, 3)
        assert job
        original = DraftEntry.model_validate(parsed()["entries"][0])
        await parser.apply(job, original)
        async with active.diet.store.transaction(1, default_timezone="UTC") as tx:
            draft = await tx.get_draft(draft.id)
            item = draft.content.items[0]
            content = draft.content.model_copy(
                update={"field_locks": [f"items.{item.id}.quantity"]}
            )
            draft = draft.model_copy(
                update={"content": content, "preview_hash": content_hash(content)}
            )
            await tx.save_draft(draft)
        changed = original.model_copy(
            update={
                "temporal": original.temporal.model_copy(update={"meal_type": "dinner"}),
                "items": [
                    original.items[0].model_copy(
                        update={
                            "quantity": MassQuantity(kind="mass", grams="100", food_state="cooked")
                        }
                    )
                ],
            }
        )
        changed = DraftEntry.model_validate(changed.model_dump())
        job = job.model_copy(
            update={"payload": job.payload.model_copy(update={"draft_version": draft.version})}
        )
        status, _ = await parser.apply(job, changed)
        current = await active.diet.preview(1, draft.id)
        assert status == "suggestion"
        assert current.content.entry.temporal.meal_type == "dinner"
        assert current.content.items[0].id == item.id
        assert current.content.items[0].quantity == item.quantity
        assert current.preview_hash != draft.preview_hash
        assert current.parse_suggestion.items[0].quantity != item.quantity
    finally:
        await stop_container(active)
