"""Offline, repeatable legacy conversion; callers must rehearse backups before cutover."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from nutripilot.domain.diet import (
    DECIMAL_STEP,
    DietModel,
    FieldEvidence,
    NutrientRange,
    NutritionGoals,
    NutritionSnapshot,
    SubmissionAnchor,
    UnknownFood,
    UnknownQuantity,
    content_hash,
)
from nutripilot.domain.diet_records import (
    DietItem,
    DietPlan,
    DietUserState,
    NutritionTarget,
    OperationReceipt,
    PlannedMeal,
    ResourceRef,
)
from nutripilot.infrastructure.diet_store import PostgresDietTransaction, _json_payload
from nutripilot.tools.local.models import MealPlan, UserProfile

# Frozen once released: regenerating the namespace would break legacy item links.
LEGACY_DIET_NAMESPACE = UUID("15f15df1-cc63-5ed3-b24d-9cd17dc600a7")


class MigrationIssue(DietModel):
    entity_type: str
    entity_id: str
    code: str


class LegacyMigrationReport(DietModel):
    dry_run: bool
    migrated_plans: int
    migrated_items: int
    migrated_targets: int
    skipped_plans: int


class DietMigrationError(ValueError):
    def __init__(self, issues: list[MigrationIssue]) -> None:
        super().__init__(f"Legacy diet migration blocked by {len(issues)} invalid rows.")
        self.issues = issues


def convert_legacy_plan(
    legacy: MealPlan,
    *,
    now: datetime,
    names: dict[int, str],
) -> tuple[DietPlan, list[PlannedMeal]]:
    plan = DietPlan(
        id=legacy.id,
        user_id=legacy.user_id,
        title=legacy.title,
        start_date=legacy.start_date,
        created_at=legacy.created_at,
        updated_at=now,
        migrated_at=now,
    )
    grouped: dict[tuple[date, str], list[tuple[int, DietItem]]] = defaultdict(list)
    for index, item in enumerate(legacy.items):
        if not 0 <= (item.meal_date - legacy.start_date).days < 7:
            raise ValueError("Legacy meal date is outside its plan.")
        ref = f"{legacy.id}:{index}"
        item_id = uuid5(LEGACY_DIET_NAMESPACE, f"item:{legacy.user_id}:{ref}")
        key = (item.meal_date, item.meal_type)
        name = names.get(item.meal_id) or f"Legacy meal #{item.meal_id}"
        grouped[key].append(
            (
                index,
                DietItem(
                    id=item_id,
                    client_item_key=f"legacy_{index}",
                    position=len(grouped[key]),
                    food_ref=UnknownFood(kind="unknown", name=name),
                    legacy_food_id=item.meal_id,
                    name_snapshot=name,
                    quantity=UnknownQuantity(kind="unknown", description="legacy_unknown"),
                    evidence_snapshot=None,
                    nutrition_snapshot=NutritionSnapshot.unknown("quantity_unknown"),
                    field_provenance=[
                        FieldEvidence(
                            field="quantity",
                            source_kind="server_default",
                            source_ref=ref,
                            original_text="legacy_unknown",
                            operation="set",
                            status="derived",
                        )
                    ],
                    note=item.note,
                ),
            )
        )
    meals = [
        PlannedMeal.model_validate(
            {
                "id": uuid5(
                    LEGACY_DIET_NAMESPACE, f"meal:{legacy.user_id}:{legacy.id}:{day}:{meal_type}"
                ),
                "user_id": legacy.user_id,
                "plan_id": legacy.id,
                "local_date": day,
                "meal_type": meal_type,
                "items": [item for _, item in items],
                "legacy_item_refs": {f"{legacy.id}:{index}": item.id for index, item in items},
                "created_at": legacy.created_at,
                "updated_at": now,
            }
        )
        for (day, meal_type), items in grouped.items()
    ]
    return plan, meals


def convert_legacy_target(
    profile: UserProfile,
    *,
    anchor: SubmissionAnchor,
) -> NutritionTarget | None:
    values: dict[str, NutrientRange | None] = {}
    for old, new in (
        ("daily_calorie_target", "calories_kcal"),
        ("daily_protein_target_g", "protein_g"),
    ):
        value = getattr(profile, old)
        if value is not None:
            exact = Decimal(str(value))
            values[new] = NutrientRange(
                lower=exact.quantize(DECIMAL_STEP, rounding=ROUND_FLOOR),
                upper=exact.quantize(DECIMAL_STEP, rounding=ROUND_CEILING),
            )
    if not values:
        return None
    return NutritionTarget(
        id=uuid5(LEGACY_DIET_NAMESPACE, f"target:{profile.user_id}:legacy_profile"),
        user_id=profile.user_id,
        effective_date=anchor.anchor_local_date,
        goals=NutritionGoals(**values),
        source="legacy_profile",
        source_ref=f"legacy-profile:{profile.user_id}",
        created_at=anchor.submitted_at,
        updated_at=anchor.submitted_at,
    )


async def migrate_legacy_diet(
    engine: AsyncEngine,
    *,
    default_timezone: str,
    now: datetime,
    dry_run: bool = True,
) -> LegacyMigrationReport:
    """All rows validate before any conversion writes; any write failure rolls back everything."""
    async with engine.begin() as connection:
        await connection.execute(
            text("LOCK TABLE meal_plans,user_profiles IN SHARE ROW EXCLUSIVE MODE")
        )
        plan_rows = (
            (await connection.execute(text("SELECT * FROM meal_plans ORDER BY user_id,id")))
            .mappings()
            .all()
        )
        profile_rows = (
            (await connection.execute(text("SELECT * FROM user_profiles ORDER BY user_id")))
            .mappings()
            .all()
        )
        name_rows = (
            (await connection.execute(text("SELECT id,payload FROM meals"))).mappings().all()
        )
        names = {
            int(row["id"]): value
            for row in name_rows
            if isinstance((value := _json_payload(row["payload"]).get("name")), str)
            and value.strip()
        }
        states = {
            row["user_id"]: DietUserState.model_validate(dict(row))
            for row in (
                await connection.execute(
                    text("SELECT * FROM diet_user_state ORDER BY user_id FOR UPDATE")
                )
            )
            .mappings()
            .all()
        }
        plans: list[tuple[DietPlan, list[PlannedMeal]]] = []
        targets: list[NutritionTarget] = []
        skipped = 0
        issues: list[MigrationIssue] = []
        for row in plan_rows:
            if row["migrated_at"] is not None:
                skipped += 1
                continue
            try:
                legacy = MealPlan.model_validate(_json_payload(row["payload"]))
                if legacy.id != row["id"] or legacy.user_id != row["user_id"]:
                    raise ValueError("Legacy owner/identity does not match its row.")
                plans.append(convert_legacy_plan(legacy, now=now, names=names))
            except (ValidationError, ValueError):
                issues.append(
                    MigrationIssue(
                        entity_type="plan", entity_id=str(row["id"]), code="INVALID_LEGACY_PLAN"
                    )
                )
        for row in profile_rows:
            owner = row["user_id"]
            try:
                profile = UserProfile.model_validate(_json_payload(row["payload"]))
                if profile.user_id != owner:
                    raise ValueError("Legacy profile owner does not match its row.")
                state = states.get(owner)
                anchor = SubmissionAnchor.capture(
                    now, state.timezone if state else default_timezone
                )
                target = convert_legacy_target(profile, anchor=anchor)
                if target is not None:
                    existing = (
                        await connection.execute(
                            text(
                                "SELECT id FROM nutrition_targets WHERE user_id=:owner "
                                "AND source='legacy_profile'"
                            ),
                            {"owner": owner},
                        )
                    ).first()
                    if existing is None:
                        conflict = (
                            await connection.execute(
                                text(
                                    "SELECT id FROM nutrition_targets WHERE user_id=:owner "
                                    "AND effective_date=:day"
                                ),
                                {"owner": owner, "day": target.effective_date},
                            )
                        ).first()
                        if conflict is not None:
                            raise ValueError("Existing target conflicts with legacy conversion.")
                        targets.append(target)
            except (ValidationError, ValueError):
                issues.append(
                    MigrationIssue(
                        entity_type="profile", entity_id=str(owner), code="INVALID_LEGACY_PROFILE"
                    )
                )
        if issues:
            raise DietMigrationError(issues)
        report = LegacyMigrationReport(
            dry_run=dry_run,
            migrated_plans=len(plans),
            migrated_items=sum(len(meal.items) for _, meals in plans for meal in meals),
            migrated_targets=len(targets),
            skipped_plans=skipped,
        )
        if dry_run:
            return report
        owners = sorted(
            {plan.user_id for plan, _ in plans} | {target.user_id for target in targets}
        )
        for owner in owners:
            await connection.execute(
                text(
                    "INSERT INTO diet_user_state(user_id,timezone) VALUES (:owner,:zone) "
                    "ON CONFLICT(user_id) DO NOTHING"
                ),
                {"owner": owner, "zone": default_timezone},
            )
            state_row = (
                (
                    await connection.execute(
                        text("SELECT * FROM diet_user_state WHERE user_id=:owner FOR UPDATE"),
                        {"owner": owner},
                    )
                )
                .mappings()
                .one()
            )
            tx = PostgresDietTransaction(connection, DietUserState.model_validate(dict(state_row)))
            user_targets = [target for target in targets if target.user_id == owner]
            for plan, meals in plans:
                if plan.user_id != owner:
                    continue
                receipt = _migration_receipt(owner, f"plan:{plan.id}", plan, now)
                await tx.save_plan(plan)
                for meal in meals:
                    await tx.save_planned_meal(meal)
                    await tx.add_revision(None, meal, receipt)
                await tx.add_revision(None, plan, receipt)
                await tx.save_receipt(receipt)
            for target in user_targets:
                receipt = _migration_receipt(owner, "legacy-profile", target, now)
                await tx.save_target(target)
                await tx.add_revision(None, target, receipt)
                await tx.save_receipt(receipt)
            await tx.advance_epoch(targets_changed=bool(user_targets))
        return report


def _migration_receipt(
    owner: int,
    key: str,
    resource: DietPlan | NutritionTarget,
    now: datetime,
) -> OperationReceipt:
    # Hash unbounded legacy IDs rather than truncating the database idempotency key.
    return OperationReceipt(
        id=uuid5(LEGACY_DIET_NAMESPACE, f"receipt:{owner}:{key}"),
        user_id=owner,
        operation="migrate_legacy_diet",
        idempotency_key=str(uuid5(LEGACY_DIET_NAMESPACE, key)),
        request_hash=content_hash(resource),
        result_status="committed",
        created_at=now,
        result_refs=[
            ResourceRef(
                kind="plan" if isinstance(resource, DietPlan) else "target",
                id=resource.id,
                version=resource.version,
            )
        ],
    )
