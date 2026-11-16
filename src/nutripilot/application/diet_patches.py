"""Apply the same typed item edits to drafts and existing records."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from nutripilot.domain.diet import (
    DietError,
    DraftEntry,
    FieldEvidence,
    calculate_nutrition,
)
from nutripilot.domain.diet_images import ImageEstimation
from nutripilot.domain.diet_records import DietItem
from nutripilot.tools.local.diet_models import AddItem, EntryPatch, RemoveItem
from nutripilot.tools.local.ports import DietFoodResolver, DietTransaction, ResolvedFood


async def apply_entry_patch[T: DraftEntry](
    entry: T,
    items: list[DietItem],
    patch: EntryPatch,
    *,
    foods: DietFoodResolver,
    user_id: int,
    now: datetime,
    evidence: list[FieldEvidence],
    tx: DietTransaction,
    allow_empty: bool = False,
    image_estimation: ImageEstimation | None = None,
    media_ids: list[UUID] | None = None,
) -> tuple[T, list[DietItem]]:
    data = entry.model_dump(exclude_unset=True)
    if patch.temporal:
        data["temporal"] = {
            **entry.temporal.model_dump(exclude_unset=True),
            **patch.temporal.model_dump(exclude_unset=True),
        }
    proposed_by_key = {item.client_item_key: item for item in entry.items}
    items_by_id = {item.id: item for item in items}
    for edit in patch.item_edits or []:
        if isinstance(edit, AddItem):
            item = edit.item
            if item.client_item_key in proposed_by_key:
                raise DietError("ITEM_KEY_CONFLICT", "This item key already exists.")
            resolved = await foods.resolve(item.food, user_id=user_id, now=now, tx=tx)
            if image_estimation and item.food.kind == "unknown":
                estimate = image_estimation.evidence_for(
                    item_key=item.client_item_key,
                    name=resolved.name,
                    media_ids=media_ids or [],
                    now=now,
                )
                if estimate is not None:
                    resolved = ResolvedFood(resolved.name, estimate)
            item_id = uuid4()
            items_by_id[item_id] = DietItem(
                id=item_id,
                client_item_key=item.client_item_key,
                position=len(items_by_id),
                food_ref=item.food,
                name_snapshot=resolved.name,
                quantity=item.quantity,
                evidence_snapshot=resolved.evidence,
                nutrition_snapshot=calculate_nutrition(item.quantity, resolved.evidence),
                field_provenance=[
                    field for field in evidence if field.item_key in (None, item.client_item_key)
                ],
                note=item.note,
            )
            proposed_by_key[item.client_item_key] = item
            continue
        existing = items_by_id.get(edit.item_id)
        if existing is None:
            raise DietError("NOT_FOUND", "Item not found in this entry.")
        if isinstance(edit, RemoveItem):
            del items_by_id[edit.item_id]
            del proposed_by_key[existing.client_item_key]
            continue
        proposed = proposed_by_key[existing.client_item_key]
        patched_data = {
            **proposed.model_dump(exclude_unset=True),
            **edit.changes.model_dump(exclude_unset=True),
        }
        if patched_data.get("note") is None:
            patched_data.pop("note", None)
        patched = type(proposed).model_validate(patched_data)
        if patched == proposed:
            continue
        resolved = (
            await foods.resolve(patched.food, user_id=user_id, now=now, tx=tx)
            if patched.food != existing.food_ref
            else ResolvedFood(existing.name_snapshot, existing.evidence_snapshot)
        )
        items_by_id[existing.id] = DietItem.model_validate(
            {
                **existing.model_dump(),
                "version": existing.version + 1,
                "food_ref": patched.food,
                "quantity": patched.quantity,
                "name_snapshot": resolved.name,
                "evidence_snapshot": resolved.evidence,
                "nutrition_snapshot": calculate_nutrition(patched.quantity, resolved.evidence),
                "field_provenance": [
                    *existing.field_provenance,
                    *(
                        field
                        for field in evidence
                        if field.item_key in (None, existing.client_item_key, str(existing.id))
                    ),
                ],
                "note": patched.note,
            }
        )
        proposed_by_key[patched.client_item_key] = patched
    if not proposed_by_key and not allow_empty:
        raise DietError("EMPTY_RECORD", "An entry must retain at least one food item.")
    data["items"] = [item.model_dump(exclude_unset=True) for item in proposed_by_key.values()]
    revised_entry = type(entry).model_validate(data)
    revised_items = [
        DietItem.model_validate({**item.model_dump(), "position": position})
        for position, item in enumerate(items_by_id.values())
    ]
    return revised_entry, revised_items
