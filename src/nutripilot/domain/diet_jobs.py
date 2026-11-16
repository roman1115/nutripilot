"""Private task references and persistent budgets, never model approval authority."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from nutripilot.domain.diet import DietModel, DraftEntry, Version
from nutripilot.domain.diet_images import ImageFoodEstimate
from nutripilot.domain.diet_records import ResourceRef


class JobPayload(DietModel):
    task: Literal["parse"] = "parse"
    draft_id: UUID
    draft_version: Version
    field_locks_version: Version
    media_ids: Annotated[list[UUID], Field(max_length=4)] = Field(default_factory=list)
    send_images_authorized: bool = False


class ExportRequest(DietModel):
    task: Literal["export"] = "export"
    start_date: date
    end_date: date
    include_media: bool = False
    format: Literal["zip"] = "zip"

    @model_validator(mode="after")
    def valid_range(self) -> ExportRequest:
        if self.start_date > self.end_date:
            raise ValueError("Export start must not follow end.")
        return self


class PurgePayload(DietModel):
    task: Literal["purge"] = "purge"
    refs: Annotated[list[ResourceRef], Field(min_length=1)]


class ExportArtifact(DietModel):
    storage_key: str
    checksum: str
    bytes: Annotated[int, Field(gt=0, le=209715200)]
    expires_at: AwareDatetime
    diet_epoch: int


class JobBudget(DietModel):
    deadline_at: AwareDatetime | None = None
    token_limit: Annotated[int, Field(ge=1)]


class JobUsage(DietModel):
    reserved_tokens: Annotated[int, Field(ge=0)] = 0
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    provider_usage_known: bool = False
    result: Literal["pending", "applied", "suggestion", "not_applied"] = "pending"


class BackgroundJob(DietModel):
    id: UUID
    user_id: Annotated[int, Field(gt=0)]
    queue: str
    kind: Literal["parse_text", "parse_image", "export", "purge"]
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"] = "queued"
    payload: Annotated[JobPayload | ExportRequest | PurgePayload, Field(discriminator="task")]
    request_hash: str
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]
    attempt: Annotated[int, Field(ge=0, le=2)] = 0
    lease_owner: str | None = None
    lease_version: Annotated[int, Field(ge=0)] = 0
    heartbeat_at: AwareDatetime | None = None
    available_at: AwareDatetime
    created_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    budget: JobBudget
    usage: JobUsage = Field(default_factory=JobUsage)
    result_refs: list[ResourceRef] = Field(default_factory=list)
    error_code: str | None = None
    artifact: ExportArtifact | None = None


class JobEvent(DietModel):
    job_id: UUID
    sequence: int
    event_type: str
    state: str
    created_at: datetime


class ParsedIntake(DietModel):
    entries: Annotated[list[DraftEntry], Field(max_length=10)]
    clarification: Annotated[str, Field(max_length=1000)] = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_model_fields(cls, value: object) -> object:
        # Model output often spells an absent note as null; write API patches stay strict.
        if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
            return value
        result = dict(value)
        entries = []
        remapped = {}
        for entry_index, entry in enumerate(value["entries"]):
            if not isinstance(entry, dict) or not isinstance(entry.get("items"), list):
                entries.append(entry)
                continue
            items = []
            used = {
                item.get("client_item_key")
                for item in entry["items"]
                if isinstance(item, dict) and isinstance(item.get("client_item_key"), str)
            }
            for index, item in enumerate(entry["items"]):
                if not isinstance(item, dict):
                    items.append(item)
                    continue
                item = {
                    key: part for key, part in item.items() if key != "note" or part is not None
                }
                key = item.get("client_item_key")
                if isinstance(key, str) and not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", key):
                    if (entry_index, key) in remapped:
                        raise ValueError("Model item keys must remain unambiguous.")
                    # This is an internal correlation key, not a user-supplied food fact.
                    replacement = f"model_item_{index}"
                    suffix = 0
                    while replacement in used:
                        suffix += 1
                        replacement = f"model_item_{index}_{suffix}"
                    used.add(replacement)
                    item["client_item_key"] = replacement
                    remapped[(entry_index, key)] = replacement
                items.append(item)
            entries.append({**entry, "items": items})
        result["entries"] = entries
        if isinstance(value.get("estimates"), list):
            result["estimates"] = [
                {
                    **estimate,
                    "client_item_key": remapped.get(
                        (estimate["entry_index"], estimate["client_item_key"]),
                        estimate.get("client_item_key"),
                    ),
                }
                if isinstance(estimate, dict)
                and isinstance(estimate.get("client_item_key"), str)
                and type(estimate.get("entry_index")) is int
                else estimate
                for estimate in value["estimates"]
            ]
        return result


class ParsedImageIntake(ParsedIntake):
    estimates: Annotated[list[ImageFoodEstimate], Field(max_length=500)] = Field(
        default_factory=list
    )


class MediaAsset(DietModel):
    id: UUID
    user_id: Annotated[int, Field(gt=0)]
    version: Version = 1
    kind: Literal["intake_image", "label_image"]
    storage_key: str
    content_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    mime: Literal["image/jpeg", "image/png", "image/webp"]
    bytes: Annotated[int, Field(gt=0, le=10485760)]
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]
    state: Literal["pending", "ready", "purge_pending", "purged"] = "ready"
    owner_refs: list[ResourceRef] = Field(default_factory=list)
    expires_at: AwareDatetime | None
    purged_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
