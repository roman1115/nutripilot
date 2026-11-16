"""Diet transactions against a disposable schema, never existing application tables."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nutripilot.application.diet_management import DietManagement
from nutripilot.application.diet_parsing import DietParsingService, TextDraft
from nutripilot.application.diet_privacy import DietPrivacyService, PurgeRequest
from nutripilot.application.diet_service import DietService
from nutripilot.application.intake_service import IntakeService, ResolvedFood
from nutripilot.application.plan_service import PlanService
from nutripilot.config import Settings
from nutripilot.domain.diet import DietError, FoodRef, SubmissionAnchor
from nutripilot.domain.diet_jobs import ExportRequest
from nutripilot.domain.diet_records import PlanChangeContent
from nutripilot.domain.execution import ExecutionOwnershipLost
from nutripilot.infrastructure.background_jobs import BackgroundJobStore
from nutripilot.infrastructure.chat_tasks import ChatTaskStore
from nutripilot.infrastructure.diet_backup import replay_deletions, restore_media, snapshot_media
from nutripilot.infrastructure.diet_deletions import DeletionJournal
from nutripilot.infrastructure.diet_foods import CatalogDietFoodResolver
from nutripilot.infrastructure.diet_migration import DietMigrationError, migrate_legacy_diet
from nutripilot.infrastructure.diet_schema import (
    DIET_SCHEMA_V1,
    DIET_SCHEMA_V2,
    DIET_SCHEMA_V3,
    DIET_SCHEMA_V4,
)
from nutripilot.infrastructure.diet_store import PostgresDietStore, PostgresDietTransaction
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.retrieval.catalog import FoodCatalog
from nutripilot.tools.local.diet_models import (
    CorrectDietLog,
    CreateIntake,
    CreateMealPlan,
    SetTarget,
)
from nutripilot.tools.local.ports import DietTransaction

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NUTRIPILOT_TEST_EXTERNAL") != "1",
        reason="Set NUTRIPILOT_TEST_EXTERNAL=1 for isolated PostgreSQL diet tests.",
    ),
]
DATABASE_URL = os.getenv(
    "NUTRIPILOT_TEST_DATABASE_URL",
    "postgresql+asyncpg://nutripilot:nutripilot@127.0.0.1:5432/nutripilot",
)
NOW = datetime(2026, 11, 12, 4, tzinfo=UTC)
ANCHOR = SubmissionAnchor.capture(NOW, "Asia/Shanghai")


@pytest.fixture
async def diet_engine() -> AsyncIterator[AsyncEngine]:
    schema = f"test_diet_{uuid4().hex}"
    admin = create_async_engine(DATABASE_URL)
    scoped = create_async_engine(
        DATABASE_URL,
        connect_args={"server_settings": {"search_path": schema}},
        pool_size=6,
    )
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with scoped.begin() as connection:
            await connection.execute(
                text("""
                CREATE TABLE meal_plans(
                  id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,idempotency_key TEXT NOT NULL,
                  payload JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  UNIQUE(user_id,idempotency_key))
            """)
            )
            await connection.execute(
                text(
                    "CREATE TABLE user_profiles(user_id BIGINT PRIMARY KEY,payload JSONB NOT NULL)"
                )
            )
            await connection.execute(
                text("CREATE TABLE meals(id BIGINT PRIMARY KEY,payload JSONB NOT NULL)")
            )
            for statement in (*DIET_SCHEMA_V1, *DIET_SCHEMA_V2, *DIET_SCHEMA_V3, *DIET_SCHEMA_V4):
                await connection.execute(text(statement))
        yield scoped
    finally:
        await scoped.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


class UnknownFoodResolver:
    async def resolve(
        self, food: FoodRef, *, user_id: int, now: datetime, tx: DietTransaction
    ) -> ResolvedFood:
        return ResolvedFood(name="fixture-food", evidence=None)

    async def assert_available(self, food: FoodRef, *, user_id: int, tx: DietTransaction) -> None:
        return None


def intake_request() -> CreateIntake:
    return CreateIntake.model_validate(
        {
            "action": "create",
            "entry": {
                "actuality": "actual",
                "subject": "self",
                "temporal": {"local_date": "2026-11-12", "meal_type": "lunch"},
                "items": [
                    {
                        "client_item_key": "food1",
                        "food": {"kind": "unknown", "name": "fixture-food"},
                        "quantity": {"kind": "unknown", "description": "unspecified"},
                    }
                ],
            },
            "evidence": [
                {
                    "field": "food",
                    "source_kind": "user_form",
                    "source_ref": "fixture-form",
                    "original_text": "fixture-food",
                    "operation": "set",
                    "status": "explicit",
                }
            ],
        }
    )


async def test_real_concurrent_confirmation_and_reconnection(diet_engine: AsyncEngine) -> None:
    store = PostgresDietStore(diet_engine)
    service = IntakeService(
        store,
        UnknownFoodResolver(),
        default_timezone="Asia/Shanghai",
        clock=lambda: NOW,
    )
    draft = await service.prepare(1, intake_request(), idempotency_key="prepare", anchor=ANCHOR)
    receipts = await asyncio.gather(
        *[
            service.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
            for _ in range(12)
        ]
    )
    assert len({receipt.id for receipt in receipts}) == 1
    await diet_engine.dispose()
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        logs = await tx.list_logs(ANCHOR.anchor_local_date, ANCHOR.anchor_local_date)
        assert len(logs) == 1
        assert logs[0].items[0].id == draft.content.items[0].id
        assert logs[0].items[0].nutrition_snapshot.calories_kcal.value is None
        assert tx.state.diet_epoch == 1
        saved = await tx.get_draft(draft.id)
        assert saved is not None and saved.receipt_id == receipts[0].id
    async with store.transaction(2, default_timezone="Asia/Shanghai") as tx:
        assert await tx.get_draft(draft.id) is None
        assert await tx.get_log(receipts[0].result_refs[0].id) is None
        assert await tx.get_receipt("prepare_intake", "prepare") is None
    async with diet_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT COUNT(*) FROM diet_revisions"))
        ).scalar_one() == 1


async def test_real_resource_purge_and_legacy_plan_id(diet_engine: AsyncEngine) -> None:
    from tests.unit.test_diet_foods import food_input
    from tests.unit.test_diet_plans import create_request, entry

    from nutripilot.application.personal_food_service import PersonalFoodMutation
    from nutripilot.domain.diet_records import ResourceRef

    store = PostgresDietStore(diet_engine)
    diet = DietManagement(
        store,
        CatalogDietFoodResolver(FoodCatalog(sources=[], ingredients=[], recipes=[])),
        default_timezone="UTC",
        cursor_secret=b"s" * 32,
        clock=lambda: NOW,
    )
    food_preview = await diet.personal_foods.prepare(
        1, PersonalFoodMutation(action="create", data=food_input()), idempotency_key="food"
    )
    await diet.confirm(1, food_preview.id, version=1, approved_hash=food_preview.content_hash)
    food = food_preview.content.after
    deletion = await diet.personal_foods.prepare(
        1,
        PersonalFoodMutation(action="delete", food_id=food.id, base_version=1),
        idempotency_key="delete-food",
    )
    await diet.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
    preview = await diet.plans.prepare(
        1, create_request(entry("meal", "2026-11-12", "lunch")), idempotency_key="plan"
    )
    await diet.confirm(1, preview.id, version=1, approved_hash=preview.content_hash)
    meal = preview.content.after_meals[0]
    async with store.transaction(1, default_timezone="UTC") as tx:
        plan = await tx.get_plan(meal.plan_id)
        assert plan
        # A restored pre-migration plan may use a non-UUID identity.
        legacy = plan.model_copy(update={"id": "legacy-plan-%_", "deleted_at": NOW})
        await tx.save_plan(legacy)
        expired = await tx.expired_resources(NOW + timedelta(days=7))
        assert ResourceRef(kind="plan", id=legacy.id, version=1) in expired
        await tx.purge_resource(ResourceRef(kind="plan", id=legacy.id, version=1), NOW)
        assert await tx.get_tombstone("plan", legacy.id)
        await tx.purge_resource(ResourceRef(kind="personal_food", id=food.id, version=2), NOW)
        assert await tx.get_personal_food(food.id) is None
        assert await tx.get_preview(food_preview.id) is None
        assert await tx.get_preview(deletion.id) is None
        assert await tx.get_tombstone("personal_food", food.id)
        archived = plan.model_copy(update={"deleted_at": NOW, "version": 2})
        await tx.save_plan(archived)
        await tx.purge_resource(ResourceRef(kind="plan", id=plan.id, version=2), NOW)
        assert await tx.get_plan(plan.id) is None
        assert await tx.get_planned_meal(meal.id) is None
        assert await tx.get_preview(preview.id) is None
    await diet_engine.dispose()
    async with store.transaction(1, default_timezone="UTC") as tx:
        with pytest.raises(DietError) as exc:
            await tx.save_plan(plan)
        assert exc.value.code == "RESOURCE_PURGED"
    async with store.transaction(2, default_timezone="UTC") as tx:
        assert await tx.get_tombstone("plan", legacy.id) is None


async def test_real_privacy_export_purge_reconnect_and_job_budget(
    diet_engine: AsyncEngine, tmp_path
) -> None:
    async with diet_engine.connect() as connection:
        schema = (await connection.execute(text("SELECT current_schema()"))).scalar_one()
    engine = create_engine(
        diet_engine.url.set(drivername="postgresql+psycopg"),
        connect_args={"options": f"-c search_path={schema}"},
    )
    chat = ChatTaskStore(engine, f"diet-private-{uuid4().hex}")
    await asyncio.to_thread(chat.initialize)
    jobs = BackgroundJobStore(chat)
    assert await asyncio.to_thread(jobs.initialize)
    diet = DietManagement(
        PostgresDietStore(diet_engine),
        CatalogDietFoodResolver(FoodCatalog(sources=[], ingredients=[], recipes=[])),
        default_timezone="UTC",
        cursor_secret=b"s" * 32,
    )
    parser = DietParsingService(
        diet,
        jobs,
        FakeModelGateway(),
        Settings(
            _env_file=None,
            app_mode="test",
            user_timezone="UTC",
            chat_queue=chat.queue,
            diet_media_root=str(tmp_path / "media"),
            diet_export_root=str(tmp_path / "exports"),
        ),
    )
    privacy = DietPrivacyService(parser)
    try:
        # A recreated task adapter keeps reservations and the original absolute deadline.
        draft = await parser.create(1, TextDraft(mode="text", text="午餐"), key="parse")
        first = await asyncio.to_thread(jobs.claim, 4, 3)
        assert first and draft.job_id == first.id
        await asyncio.to_thread(jobs.account, first, 123)
        recreated = BackgroundJobStore(chat)
        await asyncio.to_thread(recreated.recover, startup=True)
        with pytest.raises(ExecutionOwnershipLost):
            await asyncio.to_thread(jobs.finish, first)
        second = await asyncio.to_thread(recreated.claim, 4, 3)
        assert second and second.attempt == 2 and second.usage.reserved_tokens == 123
        assert second.budget.deadline_at == first.budget.deadline_at
        await asyncio.to_thread(recreated.cancel, second.id, 1)
        with pytest.raises(ExecutionOwnershipLost):
            await asyncio.to_thread(recreated.account, second, 1)

        actual = await diet.intake.prepare(
            1, intake_request(), idempotency_key="actual", anchor=ANCHOR
        )
        receipt = await diet.confirm(1, actual.id, version=1, approved_hash=actual.preview_hash)
        record_id = receipt.result_refs[0].uuid
        exported = await privacy.submit_export(
            1,
            ExportRequest(
                start_date=ANCHOR.anchor_local_date,
                end_date=ANCHOR.anchor_local_date,
            ),
            "export",
        )
        claimed = await asyncio.to_thread(jobs.claim_maintenance)
        assert claimed and claimed.id == exported.id
        assert await asyncio.to_thread(jobs.claim_maintenance) is None
        artifact = await privacy.execute_export(claimed)
        await asyncio.to_thread(jobs.finish, claimed, artifact=artifact)
        assert (await privacy.download(1, exported.id)).startswith(b"PK")
        with pytest.raises(DietError) as exc:
            await privacy.download(2, exported.id)
        assert exc.value.code == "NOT_FOUND"
        deletion = await diet.records.prepare_delete_restore(
            1, record_id, base_version=1, action="delete", idempotency_key="delete"
        )
        await diet.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
        preview = await privacy.prepare_purge(1, PurgeRequest(record_ids=[record_id]), "purge")
        job = await privacy.confirm_purge(1, preview.id, 1, preview.content_hash)
        claimed = await asyncio.to_thread(jobs.claim_maintenance)
        assert claimed and claimed.id == job.id
        await privacy.execute_purge(claimed)
        await asyncio.to_thread(jobs.finish, claimed)
        await diet_engine.dispose()
        with pytest.raises(DietError) as exc:
            await diet.records.get_log(1, record_id)
        assert exc.value.code == "RESOURCE_PURGED"
        async with diet.store.transaction(1, default_timezone="UTC") as tx:
            assert await tx.get_tombstone("log", record_id)
            assert await tx.get_tombstone("draft", actual.id)
            assert await tx.get_draft(actual.id) is None
            with pytest.raises(DietError) as exc:
                await tx.save_draft(actual)
            assert exc.value.code == "RESOURCE_PURGED"
        async with diet_engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM diet_revisions WHERE entity_id=:id"),
                    {"id": str(record_id)},
                )
            ).scalar_one() == 0
        from tests.unit.test_diet_media import image_bytes

        asset = await parser.upload(1, image_bytes(), "image/png", "intake_image")
        async with diet.store.transaction(1, default_timezone="UTC") as tx:
            assert not await tx.media_referenced(asset.id)
            assert (await tx.list_media())[0].id == asset.id
        diet.clock = lambda: datetime.now(UTC) + timedelta(hours=25)
        await privacy.cleanup()
        async with diet.store.transaction(1, default_timezone="UTC") as tx:
            assert (await tx.get_media(asset.id)).state == "purged"
        assert not (parser.media.root / asset.storage_key).exists()
        diet.intake.clock = diet.clock
        diet.records.clock = diet.clock
        retained = await diet.intake.prepare(
            1, intake_request(), idempotency_key="retention", anchor=ANCHOR
        )
        retained_receipt = await diet.confirm(
            1, retained.id, version=1, approved_hash=retained.preview_hash
        )
        retained_id = retained_receipt.result_refs[0].uuid
        deletion = await diet.records.prepare_delete_restore(
            1, retained_id, base_version=1, action="delete", idempotency_key="retention-delete"
        )
        await diet.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
        deleted_at = diet.clock()
        diet.clock = lambda: deleted_at + timedelta(days=7)
        for _ in range(2):
            await privacy.cleanup()
        async with diet.store.transaction(1, default_timezone="UTC") as tx:
            assert await tx.get_log(retained_id) is None
            assert await tx.get_tombstone("log", retained_id)
            assert await tx.get_draft(retained.id) is None
    finally:
        await asyncio.to_thread(chat.close)


async def test_day_declaration_and_food_audit_survive_reconnection(
    diet_engine: AsyncEngine,
) -> None:
    from nutripilot.application.diet_management import DayStatusRequest, DietManagement
    from nutripilot.application.personal_food_service import PersonalFoodMutation
    from nutripilot.infrastructure.diet_foods import CatalogDietFoodResolver
    from nutripilot.retrieval.catalog import FoodCatalog

    store = PostgresDietStore(diet_engine)
    diet = DietManagement(
        store,
        CatalogDietFoodResolver(FoodCatalog(sources=[], ingredients=[], recipes=[])),
        default_timezone="Asia/Shanghai",
        cursor_secret=b"d" * 32,
        clock=lambda: NOW,
    )
    day = await diet.prepare_day(
        1,
        DayStatusRequest(local_date=ANCHOR.anchor_local_date, status="no_intake", base_version=1),
        idempotency_key="day",
    )
    receipts = await asyncio.gather(
        *[diet.confirm(1, day.id, version=1, approved_hash=day.content_hash) for _ in range(4)]
    )
    assert len({receipt.id for receipt in receipts}) == 1
    mutation = PersonalFoodMutation.model_validate(
        {
            "action": "create",
            "data": {
                "name": "test-label",
                "basis": "per_100g",
                "food_state": "as_sold",
                "source": "manual_label",
                "source_note": "test label only",
                "nutrients": dict.fromkeys(
                    [
                        "calories_kcal",
                        "protein_g",
                        "fat_g",
                        "carbohydrates_g",
                        "fiber_g",
                        "sodium_mg",
                    ]
                ),
                "allergens": [],
            },
        }
    )
    preview = await diet.personal_foods.prepare(1, mutation, idempotency_key="food")
    receipt = await diet.confirm(1, preview.id, version=1, approved_hash=preview.content_hash)
    await diet_engine.dispose()
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert (await tx.get_day_status(ANCHOR.anchor_local_date)).status == "no_intake"
        food = await tx.get_personal_food(receipt.result_refs[0].uuid)
        assert food is not None and food.evidence.nutrients.calories_kcal.value is None
    async with store.transaction(2, default_timezone="Asia/Shanghai") as tx:
        assert await tx.get_personal_food(receipt.result_refs[0].uuid) is None
        assert (await tx.get_day_status(ANCHOR.anchor_local_date)).status == "open"
    async with diet_engine.connect() as connection:
        revisions = (
            await connection.execute(
                text(
                    "SELECT entity_type,entity_id,entity_version FROM diet_revisions "
                    "ORDER BY entity_type"
                )
            )
        ).all()
        assert revisions == [("day_status", "2026-11-12", 2), ("personal_food", str(food.id), 1)]


async def test_real_rollback_and_owner_foreign_keys(diet_engine: AsyncEngine) -> None:
    store = PostgresDietStore(diet_engine)
    service = IntakeService(
        store,
        UnknownFoodResolver(),
        default_timezone="Asia/Shanghai",
        clock=lambda: NOW,
    )
    draft = await service.prepare(1, intake_request(), idempotency_key="prepare", anchor=ANCHOR)
    with pytest.raises(RuntimeError, match="simulated"):
        async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
            await tx.save_draft(draft.discarded(NOW))
            await tx.advance_epoch()
            raise RuntimeError("simulated failure")
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        saved = await tx.get_draft(draft.id)
        assert saved is not None and saved.state == "ready"
        assert tx.state.diet_epoch == 0
    receipt = await service.confirm(
        1,
        draft.id,
        version=1,
        approved_hash=draft.preview_hash,
    )
    async with store.transaction(2, default_timezone="Asia/Shanghai"):
        pass
    with pytest.raises(IntegrityError):
        async with diet_engine.begin() as connection:
            await connection.execute(
                text("""
                INSERT INTO diet_log_items(
                  id,user_id,diet_log_id,version,position,body,created_at,updated_at)
                VALUES (:id,2,:parent,1,1,'{}',NOW(),NOW())
            """),
                {"id": uuid4(), "parent": receipt.result_refs[0].id},
            )
    with pytest.raises(DietError) as exc:
        async with store.transaction(2, default_timezone="Asia/Shanghai") as tx:
            await tx.save_draft(draft)
    assert exc.value.code == "NOT_FOUND"


async def test_real_target_check_rejects_null_half_range(diet_engine: AsyncEngine) -> None:
    async with PostgresDietStore(diet_engine).transaction(1, default_timezone="Asia/Shanghai"):
        pass
    with pytest.raises(IntegrityError):
        async with diet_engine.begin() as connection:
            await connection.execute(
                text("""
                INSERT INTO nutrition_targets(
                  id,user_id,version,effective_date,calories_lower,source,source_ref,
                  created_at,updated_at)
                VALUES (:id,1,1,'2026-11-12',100,'user_form','fixture',NOW(),NOW())
            """),
                {"id": uuid4()},
            )


async def test_real_target_epoch_and_persisted_change_preview(diet_engine: AsyncEngine) -> None:
    service = DietService(
        PostgresDietStore(diet_engine), default_timezone="Asia/Shanghai", clock=lambda: NOW
    )
    request = SetTarget.model_validate(
        {
            "action": "set",
            "effective_date": "2026-11-12",
            "goals": {"protein_g": {"lower": "10", "upper": "20"}},
            "evidence": intake_request().evidence,
        }
    )
    first = await service.prepare_target(
        1, request, idempotency_key="first", source="user_form", source_ref="fixture"
    )
    stale = await service.prepare_target(
        1, request, idempotency_key="stale", source="user_form", source_ref="fixture"
    )
    await diet_engine.dispose()
    saved = await service.confirm(1, first.id, version=1, approved_hash=first.content_hash)
    repeated = await service.confirm(1, first.id, version=1, approved_hash=first.content_hash)
    assert saved == repeated
    with pytest.raises(DietError) as exc:
        await service.confirm(1, stale.id, version=1, approved_hash=stale.content_hash)
    assert exc.value.code == "VERSION_CONFLICT"
    targets = await service.targets(1)
    assert len(targets) == 1 and targets[0].target.goals.protein_g is not None
    async with service.store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert tx.state.diet_epoch == tx.state.target_epoch == 1
        preview = await tx.get_preview(first.id)
        assert preview is not None and preview.state == "committed"
    assert await service.targets(2) == []


async def test_real_correct_delete_restore_and_receipt_failure(
    diet_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PostgresDietStore(diet_engine)
    resolver = UnknownFoodResolver()
    intake = IntakeService(store, resolver, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    changes = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    draft = await intake.prepare(1, intake_request(), idempotency_key="first", anchor=ANCHOR)
    receipt = await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    record_id = receipt.result_refs[0].id
    correction = await changes.prepare_correction(
        1,
        CorrectDietLog.model_validate(
            {
                "action": "correct",
                "record_id": str(record_id),
                "base_version": 1,
                "patch": {"temporal": {"local_date": "2026-11-11"}},
                "evidence": intake_request().evidence,
            }
        ),
        idempotency_key="correct",
        foods=resolver,
    )
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        await tx.set_day_status(date(2026, 11, 12), "complete")
        await tx.set_day_status(date(2026, 11, 11), "no_intake")
    original_save = PostgresDietTransaction.save_receipt

    async def fail_receipt(self: PostgresDietTransaction, value: object) -> None:
        raise RuntimeError("simulated receipt failure")

    with monkeypatch.context() as context:
        context.setattr(PostgresDietTransaction, "save_receipt", fail_receipt)
        with pytest.raises(RuntimeError, match="simulated receipt"):
            await changes.confirm(
                1, correction.id, version=1, approved_hash=correction.content_hash
            )
    assert PostgresDietTransaction.save_receipt == original_save
    current = await changes.get_log(1, record_id)
    assert current.version == 1 and current.temporal.local_date == date(2026, 11, 12)
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert (await tx.get_day_status(date(2026, 11, 12))).status == "complete"
        assert (await tx.get_day_status(date(2026, 11, 11))).status == "no_intake"
        assert tx.state.diet_epoch == 1
        pending = await tx.get_preview(correction.id)
        assert pending is not None and pending.state == "ready"
    await changes.confirm(1, correction.id, version=1, approved_hash=correction.content_hash)
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert (await tx.get_day_status(date(2026, 11, 12))).status == "open"
        assert (await tx.get_day_status(date(2026, 11, 11))).status == "open"
        assert (await tx.list_logs(date(2026, 11, 11), date(2026, 11, 12)))[0].version == 2
    for action, version in (("delete", 2), ("restore", 3)):
        preview = await changes.prepare_delete_restore(
            1, record_id, base_version=version, action=action, idempotency_key=action
        )
        await changes.confirm(1, preview.id, version=1, approved_hash=preview.content_hash)
        replay = await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
        assert replay.result_status == ("deleted" if action == "delete" else "committed")
    current = await changes.get_log(1, record_id)
    assert current.version == 4 and current.deleted_at is None
    async with diet_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT COUNT(*) FROM diet_revisions"))
        ).scalar_one() == 4


async def test_real_plan_confirmation_and_default_intake_race(diet_engine: AsyncEngine) -> None:
    store = PostgresDietStore(diet_engine)
    resolver = UnknownFoodResolver()
    plans = PlanService(store, resolver, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    intake = IntakeService(store, resolver, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    request = CreateMealPlan.model_validate(
        {
            "action": "create",
            "parent": {"kind": "new", "title": "fixture", "start_date": "2026-11-09"},
            "entries": [
                {
                    "client_entry_key": "lunch",
                    "entry": {**intake_request().entry.model_dump(), "actuality": "planned"},
                }
            ],
            "evidence": intake_request().evidence,
        }
    )
    preview = await plans.prepare(1, request, idempotency_key="plan")
    await plans.confirm(1, preview.id, version=1, approved_hash=preview.content_hash)
    assert isinstance(preview.content, PlanChangeContent)
    meal = preview.content.after_meals[0]
    actual = CreateIntake.model_validate(
        {
            **intake_request().model_dump(),
            "source": {
                "kind": "plan",
                "planned_meal_id": meal.id,
                "base_version": 1,
                "record_again": False,
            },
        }
    )
    first, second = await asyncio.gather(
        *[
            intake.prepare(1, actual, idempotency_key=key, anchor=ANCHOR, channel="plan")
            for key in ("a", "b")
        ]
    )
    receipts = await asyncio.gather(
        *[
            intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
            for draft in (first, second)
        ]
    )
    assert receipts[0].result_refs == receipts[1].result_refs
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert len(await tx.list_source_logs(meal.id)) == 1
        log = await tx.get_log(receipts[0].result_refs[0].uuid)
        assert log is not None and log.items[0].source_planned_item_id == meal.items[0].id
        assert tx.state.diet_epoch == 2
    async with store.transaction(2, default_timezone="Asia/Shanghai") as tx:
        assert await tx.get_plan(meal.plan_id) is None
        assert await tx.get_planned_meal(meal.id) is None
    changes = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    deletion = await changes.prepare_delete_restore(
        1,
        receipts[0].result_refs[0].uuid,
        base_version=1,
        action="delete",
        idempotency_key="delete",
    )
    await changes.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        latest = await tx.get_planned_meal(meal.id)
        assert latest is not None and latest.version == 2 and latest.intake_generation == 2
        parent = await tx.get_plan(meal.plan_id)
        assert parent is not None and parent.version == 2
    replay = await intake.confirm(1, first.id, version=1, approved_hash=first.preview_hash)
    assert replay.result_status == "deleted"


async def test_real_legacy_migration_preflight_rerun_and_failure(diet_engine: AsyncEngine) -> None:
    legacy = {
        "id": "old:not-a-uuid",
        "user_id": 1,
        "title": "legacy",
        "start_date": "2026-10-20",
        "created_at": "2026-10-20T00:00:00Z",
        "items": [
            {"meal_date": "2026-10-20", "meal_type": "lunch", "meal_id": 2, "note": "first"},
            {"meal_date": "2026-10-20", "meal_type": "lunch", "meal_id": 9999, "note": "missing"},
        ],
    }
    async with diet_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO meal_plans(id,user_id,idempotency_key,payload,created_at) "
                "VALUES (:id,1,'legacy',CAST(:body AS JSONB),'2026-10-20T00:00:00Z')"
            ),
            {"id": legacy["id"], "body": json.dumps(legacy)},
        )
        await connection.execute(text('INSERT INTO meals VALUES (2,\'{"name":"legacy-food"}\')'))
        await connection.execute(
            text("INSERT INTO user_profiles VALUES (1,CAST(:body AS JSONB))"),
            {"body": json.dumps({"user_id": 1, "daily_calorie_target": 123.5})},
        )
    report = await migrate_legacy_diet(diet_engine, default_timezone="Asia/Shanghai", now=NOW)
    assert report.dry_run and report.migrated_items == 2 and report.migrated_targets == 1
    async with diet_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT COUNT(*) FROM planned_meals"))
        ).scalar_one() == 0
    # A bad row blocks the entire cutover instead of quietly skipping it.
    async with diet_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO meal_plans(id,user_id,idempotency_key,payload) "
                "VALUES ('broken',2,'bad','{}')"
            )
        )
    with pytest.raises(DietMigrationError) as exc:
        await migrate_legacy_diet(
            diet_engine, default_timezone="Asia/Shanghai", now=NOW, dry_run=False
        )
    assert exc.value.issues[0].entity_id == "broken"
    async with diet_engine.begin() as connection:
        assert (
            await connection.execute(text("SELECT COUNT(*) FROM planned_meals"))
        ).scalar_one() == 0
        await connection.execute(text("DELETE FROM meal_plans WHERE id='broken'"))
    applied = await migrate_legacy_diet(
        diet_engine, default_timezone="Asia/Shanghai", now=NOW, dry_run=False
    )
    assert applied.migrated_plans == 1 and applied.migrated_items == 2
    store = PostgresDietStore(diet_engine)
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        meals = await tx.list_planned_meals("old:not-a-uuid")
        assert len(meals) == 1 and len(meals[0].items) == 2
        assert set(meals[0].legacy_item_refs) == {"old:not-a-uuid:0", "old:not-a-uuid:1"}
        assert all(item.quantity.kind == "unknown" for item in meals[0].items)
        assert meals[0].items[1].name_snapshot == "Legacy meal #9999"
        target = (await tx.list_targets())[0]
        assert target.effective_date == date(2026, 11, 12)
        first_epoch = tx.state.diet_epoch
    repeated = await migrate_legacy_diet(
        diet_engine, default_timezone="Asia/Shanghai", now=NOW, dry_run=False
    )
    assert repeated.migrated_plans == repeated.migrated_targets == 0 and repeated.skipped_plans == 1
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert await tx.list_planned_meals("old:not-a-uuid") == meals
        assert tx.state.diet_epoch == first_epoch
    async with diet_engine.connect() as connection:
        original = (
            await connection.execute(
                text("SELECT payload FROM meal_plans WHERE id='old:not-a-uuid'")
            )
        ).scalar_one()
        assert original == legacy


@pytest.mark.skipif(
    os.getenv("NUTRIPILOT_TEST_BACKUP") != "1",
    reason="Set NUTRIPILOT_TEST_BACKUP=1 for the local Docker PostgreSQL restore rehearsal.",
)
async def test_real_isolated_schema_dump_and_restore(diet_engine: AsyncEngine, tmp_path) -> None:
    from tests.unit.test_diet_media import image_bytes

    from nutripilot.domain.diet_records import ResourceRef
    from nutripilot.infrastructure.diet_media import PrivateFiles

    journal = DeletionJournal(tmp_path / "independent-deletions")
    store = PostgresDietStore(diet_engine, journal)
    service = IntakeService(
        store,
        UnknownFoodResolver(),
        default_timezone="Asia/Shanghai",
        clock=lambda: NOW,
    )
    media = PrivateFiles(tmp_path / "media")
    from nutripilot.domain.diet_jobs import MediaAsset

    image = image_bytes()
    asset = MediaAsset(
        id=uuid4(),
        user_id=1,
        kind="intake_image",
        storage_key=media.write(image, "png"),
        content_sha256="a" * 64,
        mime="image/png",
        bytes=len(image),
        width=16,
        height=12,
        state="ready",
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        await tx.save_media(asset)
    draft = await service.prepare(1, intake_request(), idempotency_key="backup", anchor=ANCHOR)
    # Persist the reviewed-image fixture; API authorization is exercised in media tests.
    draft = draft.model_copy(update={"media_ids": [asset.id], "channel": "image"})
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        await tx.save_draft(draft)
    saved = await service.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    media_manifest = snapshot_media(media, tmp_path / "backup-media")
    async with diet_engine.connect() as connection:
        schema = (await connection.execute(text("SELECT current_schema()"))).scalar_one()
    assert schema.startswith("test_diet_") and len(schema) == len("test_diet_") + 32
    assert all(char in "0123456789abcdef" for char in schema[len("test_diet_") :])
    dump = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "nutripilot-postgres-1",
        "pg_dump",
        "-U",
        "nutripilot",
        "-d",
        diet_engine.url.database or "nutripilot",
        "--schema",
        schema,
        "--format",
        "custom",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    archive, errors = await asyncio.wait_for(dump.communicate(), timeout=30)
    assert dump.returncode == 0, errors.decode()
    assert archive.startswith(b"PGDMP")
    changes = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    deletion = await changes.prepare_delete_restore(
        1, saved.result_refs[0].uuid, base_version=1, action="delete", idempotency_key="delete"
    )
    await changes.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        await tx.purge_resource(ResourceRef(kind="log", id=saved.result_refs[0].id, version=2), NOW)
        assert not await tx.media_referenced(asset.id)
        await tx.save_media(asset.model_copy(update={"state": "purge_pending"}))
        await tx.advance_epoch()
    assert list(journal.entries())
    media.delete(asset.storage_key)
    pending = journal.files.root / "restore_pending"
    pending.touch(mode=0o600)
    with pytest.raises(DietError, match="恢复"):
        journal.assert_ready()
    async with diet_engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    restore = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        "nutripilot-postgres-1",
        "pg_restore",
        "-U",
        "nutripilot",
        "-d",
        diet_engine.url.database or "nutripilot",
        "--exit-on-error",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, errors = await asyncio.wait_for(restore.communicate(archive), timeout=30)
    assert restore.returncode == 0, errors.decode()
    restore_media(tmp_path / "backup-media", media_manifest, media)
    assert media.read(asset.storage_key) == image
    await diet_engine.dispose()
    replay = await service.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    assert replay.id == saved.id
    async with service.store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert len(await tx.list_logs(ANCHOR.anchor_local_date, ANCHOR.anchor_local_date)) == 1
        assert tx.state.diet_epoch == 1
    assert await replay_deletions(store, journal, media) >= 3
    assert await replay_deletions(store, journal, media) >= 3
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert await tx.get_log(saved.result_refs[0].uuid) is None
        assert await tx.get_tombstone("log", saved.result_refs[0].uuid)
        assert (await tx.get_media(asset.id)).state == "purged"
    assert not (media.root / asset.storage_key).exists()
    stale = await service.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    assert stale.result_status == "purged"
    pending.unlink()
    journal.assert_ready()


async def test_real_journal_failure_rolls_back_purge(diet_engine, tmp_path, monkeypatch) -> None:
    journal = DeletionJournal(tmp_path / "deletions")
    store = PostgresDietStore(diet_engine, journal)
    intake = IntakeService(store, UnknownFoodResolver(), default_timezone="UTC", clock=lambda: NOW)
    draft = await intake.prepare(1, intake_request(), idempotency_key="first", anchor=ANCHOR)
    saved = await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    changes = DietService(store, default_timezone="UTC", clock=lambda: NOW)
    deletion = await changes.prepare_delete_restore(
        1, saved.result_refs[0].uuid, base_version=1, action="delete", idempotency_key="delete"
    )
    await changes.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)

    def unavailable(*args):
        raise OSError("journal unavailable")

    monkeypatch.setattr(journal, "append", unavailable)
    with pytest.raises(OSError, match="unavailable"):
        async with store.transaction(1, default_timezone="UTC") as tx:
            await tx.purge_log(saved.result_refs[0].uuid, NOW)
    async with store.transaction(1, default_timezone="UTC") as tx:
        assert await tx.get_log(saved.result_refs[0].uuid)
        assert await tx.get_draft(draft.id)
        assert not await tx.get_tombstone("log", saved.result_refs[0].uuid)
