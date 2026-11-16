"""Private exports and explicit permanent record deletion reuse diet transactions."""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, BinaryIO
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from nutripilot.domain.diet import DRAFT_LIFETIME, DietError, DietModel, ShortText, content_hash
from nutripilot.domain.diet_jobs import (
    BackgroundJob,
    ExportArtifact,
    ExportRequest,
    JobBudget,
    PurgePayload,
)
from nutripilot.domain.diet_records import (
    DietChangePreview,
    DietLog,
    OperationReceipt,
    PurgeChangeContent,
    ResourceRef,
    target_periods,
)
from nutripilot.domain.execution import execution_fence
from nutripilot.infrastructure.diet_export import EXPORT_LIMIT, DietArchive
from nutripilot.infrastructure.diet_export import csv_bytes as csv_bytes
from nutripilot.infrastructure.diet_media import PrivateFiles, file_sha256
from nutripilot.infrastructure.diet_store import _media_ids

if TYPE_CHECKING:
    from nutripilot.application.diet_parsing import DietParsingService


class PurgeRequest(DietModel):
    record_ids: Annotated[list[UUID], Field(max_length=10000)] = Field(default_factory=list)
    food_ids: Annotated[list[UUID], Field(max_length=10000)] = Field(default_factory=list)
    plan_ids: Annotated[list[ShortText], Field(max_length=10000)] = Field(default_factory=list)
    target_ids: Annotated[list[UUID], Field(max_length=10000)] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounded_selection(self) -> PurgeRequest:
        groups = (self.record_ids, self.food_ids, self.plan_ids, self.target_ids)
        if not 1 <= sum(len(group) for group in groups) <= 10000:
            raise ValueError("请选择1至10000个对象。")
        if any(len(set(group)) != len(group) for group in groups):
            raise ValueError("不能重复选择清除对象。")
        return self


class DietPrivacyService:
    def __init__(self, parser: DietParsingService) -> None:
        self.parser, self.diet, self.jobs = parser, parser.diet, parser.jobs
        self.files = PrivateFiles(
            Path(parser.settings.diet_export_root)
            if parser.settings.diet_export_root
            else parser.media.root.parent / "diet_exports"
        )
        self._cleanup_owner = 0
        self._cleanup_media: dict[int, UUID] = {}
        self._orphan_after = {"media": "", "export": ""}

    async def cleanup(self) -> None:
        now = self.diet.clock()
        owners = await self.diet.store.list_users(self._cleanup_owner, 20)
        if not owners:
            self._cleanup_owner = 0
        for owner in owners:
            self._cleanup_owner = owner
            pending_media: list[UUID] = []
            async with self.diet.store.transaction(
                owner, default_timezone=self.diet.default_timezone
            ) as tx:
                await tx.expire_previews(now)
                expired = await tx.expired_logs(now)
                expired_resources = await tx.expired_resources(now)
                refs = [ResourceRef(kind="log", id=log.id, version=log.version) for log in expired]
                for ref in [*refs, *expired_resources]:
                    media_ids = await tx.purge_resource(ref, now)
                    for media_id in media_ids:
                        asset = await tx.get_media(media_id)
                        if asset and not await tx.media_referenced(media_id):
                            await tx.save_media(
                                asset.model_copy(
                                    update={
                                        "state": "purge_pending",
                                        "version": asset.version + 1,
                                        "updated_at": now,
                                    }
                                )
                            )
                for log in expired:
                    if log.temporal.local_date:
                        await tx.set_day_status(log.temporal.local_date, "open")
                if expired or expired_resources:
                    await tx.advance_epoch()
                assets = await tx.list_media(self._cleanup_media.get(owner))
                if len(assets) == 100:
                    self._cleanup_media[owner] = assets[-1].id
                else:
                    self._cleanup_media.pop(owner, None)
                for asset in assets:
                    if asset.state == "purged" or await tx.media_referenced(asset.id):
                        continue
                    if asset.state != "purge_pending" and (
                        asset.expires_at is None or asset.expires_at > now
                    ):
                        continue
                    await tx.save_media(
                        asset.model_copy(
                            update={
                                "state": "purge_pending",
                                "updated_at": now,
                            }
                        )
                    )
                    pending_media.append(asset.id)
            # Commit the pending state and independent intent before removing the file.
            for media_id in pending_media:
                await self._delete_media(owner, media_id)
        for job in await asyncio.to_thread(self.jobs.expired_exports, now):
            assert job.artifact is not None
            await asyncio.to_thread(self.files.delete, job.artifact.storage_key)
            await asyncio.to_thread(self.jobs.revoke_artifact, job)
        await asyncio.to_thread(self.jobs.prune_events, now)
        cutoff = (now - DRAFT_LIFETIME).timestamp()
        for kind, files in (("media", self.parser.media), ("export", self.files)):
            keys = await asyncio.to_thread(files.old_files, cutoff, self._orphan_after[kind])
            for key in keys:
                linked = (
                    await self.diet.store.media_key_exists(key)
                    if kind == "media"
                    else await asyncio.to_thread(self.jobs.artifact_key_exists, key)
                )
                if not linked:
                    await asyncio.to_thread(files.delete_old, key, cutoff)
            self._orphan_after[kind] = keys[-1] if len(keys) == 100 else ""

    async def submit_export(self, user_id: int, request: ExportRequest, key: str) -> BackgroundJob:
        async with self.diet.store.transaction(
            user_id, default_timezone=self.diet.default_timezone
        ):
            pass
        now = self.diet.clock()
        return await asyncio.to_thread(
            self.jobs.submit,
            BackgroundJob(
                id=uuid4(),
                user_id=user_id,
                queue=self.parser.settings.chat_queue,
                kind="export",
                payload=request,
                request_hash=content_hash(request),
                idempotency_key=key,
                created_at=now,
                available_at=now,
                budget=JobBudget(token_limit=1),
            ),
            self.parser.settings.chat_max_queued,
        )

    async def execute_export(self, job: BackgroundJob) -> ExportArtifact:
        if not isinstance(job.payload, ExportRequest):
            raise DietError("INVALID_TASK", "Expected an export request.")
        request = job.payload
        async with (
            execution_fence(),
            self.diet.store.transaction(
                job.user_id, default_timezone=self.diet.default_timezone
            ) as tx,
        ):
            epoch = tx.state.diet_epoch
            metadata = {
                "user_id": job.user_id,
                "diet_epoch": epoch,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
            }
            with self.files.stage("zip") as (key, output), DietArchive(output, metadata) as archive:
                exported_media: set[UUID] = set()
                async for kind, batch in tx.export_batches(request.start_date, request.end_date):
                    await asyncio.to_thread(archive.batch, kind, batch)
                    if not request.include_media:
                        continue
                    media_ids = set()
                    for resource in batch:
                        media_ids.update(_media_ids(resource.model_dump(mode="json")))
                    if kind == "logs":
                        media_ids.update(
                            await tx.log_media_ids(
                                [resource.id for resource in batch if isinstance(resource, DietLog)]
                            )
                        )
                    for media_id in sorted(media_ids - exported_media):
                        asset = await tx.get_media(media_id)
                        if asset is None or asset.state != "ready":
                            raise DietError(
                                "MEDIA_UNAVAILABLE", "授权媒体不可用，未生成不完整导出。"
                            )
                        content = await asyncio.to_thread(self.parser.media.read, asset.storage_key)
                        await asyncio.to_thread(archive.media, f"media/{asset.id}.png", content)
                        exported_media.add(media_id)
                checksum, size = await asyncio.to_thread(archive.finish)
        return ExportArtifact(
            storage_key=key,
            checksum=checksum,
            bytes=size,
            expires_at=self.diet.clock() + DRAFT_LIFETIME,
            diet_epoch=epoch,
        )

    async def download(self, user_id: int, job_id: UUID) -> bytes:
        source = await self.open_download(user_id, job_id)
        try:
            return await asyncio.to_thread(source.read)
        finally:
            source.close()

    async def open_download(self, user_id: int, job_id: UUID) -> BinaryIO:
        job = await asyncio.to_thread(self.jobs.get, job_id, user_id)
        if job.kind != "export" or job.state != "succeeded" or job.artifact is None:
            raise DietError("EXPORT_NOT_READY", "导出尚未完成。")
        if job.artifact.expires_at <= self.diet.clock():
            raise DietError("EXPORT_EXPIRED", "导出已过期，请重新发起。")
        async with self.diet.store.transaction(
            user_id, default_timezone=self.diet.default_timezone
        ) as tx:
            if tx.state.diet_epoch != job.artifact.diet_epoch:
                raise DietError("EXPORT_REVOKED", "饮食数据已变化，此下载已撤销，请重新导出。")
            source = await asyncio.to_thread(
                self.files.open_read, job.artifact.storage_key, limit=EXPORT_LIMIT
            )
            try:
                digest = await asyncio.to_thread(file_sha256, source)
                if not hmac.compare_digest(digest, job.artifact.checksum):
                    raise DietError("EXPORT_CORRUPTED", "导出校验失败，请重新发起。")
                source.seek(0)
                return source
            except BaseException:
                source.close()
                raise

    async def prepare_purge(
        self, user_id: int, request: PurgeRequest, key: str
    ) -> DietChangePreview:
        async with self.diet.store.transaction(
            user_id, default_timezone=self.diet.default_timezone
        ) as tx:
            previous = await tx.get_receipt("prepare_purge", key)
            digest = content_hash(request)
            if previous:
                if previous.request_hash != digest:
                    raise DietError(
                        "IDEMPOTENCY_CONFLICT", "Purge request key has different content."
                    )
                preview = await tx.get_preview(previous.result_refs[0].uuid)
                if preview is None:
                    raise DietError("RESOURCE_PURGED", "Purge preview was removed.")
                return preview
            if len(set(request.record_ids)) != len(request.record_ids):
                raise DietError("INVALID_FIELDS", "Do not duplicate purge records.")
            refs = []
            for record_id in request.record_ids:
                log = await tx.get_log(record_id)
                if log is None:
                    raise DietError("NOT_FOUND", "Record not found.")
                if log.deleted_at is None:
                    raise DietError("PURGE_REQUIRES_DELETED", "请先删除记录，再核对永久清除影响。")
                refs.append(ResourceRef(kind="log", id=log.id, version=log.version))
            notices = []
            for food_id in request.food_ids:
                food = await tx.get_personal_food(food_id)
                if food is None:
                    raise DietError("NOT_FOUND", "Food not found.")
                if food.deleted_at is None:
                    raise DietError("PURGE_REQUIRES_DELETED", "请先删除个人食物。")
                refs.append(ResourceRef(kind="personal_food", id=food.id, version=food.version))
            if request.food_ids:
                notices.append(
                    "仅清除食物资料及其编辑副本；独立记录/餐单已冻结的营养证据和共享图片保留，须另行清除这些对象才能删除。"
                )
            for plan_id in request.plan_ids:
                plan = await tx.get_plan(plan_id)
                if plan is None:
                    raise DietError("NOT_FOUND", "Plan not found.")
                if plan.deleted_at is None:
                    raise DietError("PURGE_REQUIRES_DELETED", "请先归档餐单。")
                refs.append(ResourceRef(kind="plan", id=plan.id, version=plan.version))
            if request.plan_ids:
                notices.append(
                    "清除餐单和计划餐次；关联实际记录保留其营养快照并解除计划关联，不删除实际摄入。"
                )
            timeline = await tx.list_targets() if request.target_ids else []
            for target_id in request.target_ids:
                target = next((value for value in timeline if value.id == target_id), None)
                if target is None:
                    raise DietError("NOT_FOUND", "Target not found.")
                refs.append(ResourceRef(kind="target", id=target.id, version=target.version))
                period = next(
                    value for value in target_periods(timeline) if value.target.id == target_id
                )
                preceding = [
                    value
                    for value in timeline
                    if value.effective_date < target.effective_date
                    and value.id not in request.target_ids
                ]
                fallback = (
                    max(preceding, key=lambda value: value.effective_date) if preceding else None
                )
                notices.append(
                    f"清除目标{target.id}：{target.effective_date}至"
                    f"{period.end_date_exclusive or '未来（无后续生效点）'}（右端不含），"
                    f"改由目标{fallback.id}生效。"
                    if fallback
                    else f"清除目标{target.id}：自{target.effective_date}至"
                    f"{period.end_date_exclusive or '未来（无后续生效点）'}（右端不含）变为无目标。"
                )
            threads = set()
            for draft in await tx.list_drafts():
                receipt = await tx.get_receipt_by_id(draft.receipt_id) if draft.receipt_id else None
                if (
                    receipt
                    and draft.thread_id
                    and any(
                        ref.kind == "log" and ref.id in request.record_ids
                        for ref in receipt.result_refs
                    )
                ):
                    threads.add(draft.thread_id)
            now = self.diet.clock()
            content = PurgeChangeContent(
                owner=user_id,
                base_diet_epoch=tx.state.diet_epoch,
                refs=refs,
                related_thread_ids=sorted(threads),
                impact_notices=notices,
            )
            preview = DietChangePreview(
                id=uuid4(),
                user_id=user_id,
                created_at=now,
                updated_at=now,
                content=content,
                content_hash=content_hash(content),
                expires_at=now + DRAFT_LIFETIME,
            )
            await tx.save_preview(preview)
            await tx.save_receipt(
                OperationReceipt(
                    id=uuid4(),
                    user_id=user_id,
                    operation="prepare_purge",
                    idempotency_key=key,
                    request_hash=digest,
                    result_refs=[ResourceRef(kind="preview", id=preview.id, version=1)],
                    result_status="prepared",
                    created_at=now,
                )
            )
            return preview

    async def confirm_purge(
        self, user_id: int, preview_id: UUID, version: int, approved_hash: str
    ) -> BackgroundJob:
        receipt = await self._commit_purge(user_id, preview_id, version, approved_hash)
        # Pending media is durable even if submission fails; an identical confirmation repairs it.
        now = self.diet.clock()
        payload = PurgePayload(refs=receipt.result_refs)
        return await asyncio.to_thread(
            self.jobs.submit,
            BackgroundJob(
                id=uuid4(),
                user_id=user_id,
                queue=self.parser.settings.chat_queue,
                kind="purge",
                payload=payload,
                request_hash=content_hash(payload),
                idempotency_key=f"purge:{preview_id}:{version}",
                available_at=now,
                created_at=now,
                budget=JobBudget(token_limit=1),
            ),
            self.parser.settings.chat_max_queued,
        )

    async def _commit_purge(
        self, user_id: int, preview_id: UUID, version: int, approved_hash: str
    ) -> OperationReceipt:
        async with self.diet.store.transaction(
            user_id, default_timezone=self.diet.default_timezone
        ) as tx:
            key = f"{preview_id}:{version}"
            previous = await tx.get_receipt("confirm:purge", key)
            if previous:
                if previous.request_hash != approved_hash:
                    raise DietError("IDEMPOTENCY_CONFLICT", "Approved purge is different.")
                return previous
            preview = await tx.get_preview(preview_id)
            if preview is None or not isinstance(preview.content, PurgeChangeContent):
                raise DietError("NOT_FOUND", "Purge preview not found.")
            if preview.version != version or not hmac.compare_digest(
                preview.content_hash, approved_hash
            ):
                raise DietError("VERSION_CONFLICT", "Purge preview changed.")
            if preview.expires_at <= self.diet.clock():
                raise DietError("PREVIEW_EXPIRED", "Purge preview expired.")
            if preview.state != "ready" or preview.content.base_diet_epoch != tx.state.diet_epoch:
                raise DietError("VERSION_CONFLICT", "Diet data changed; review purge again.")
            media_ids: set[UUID] = set()
            for ref in preview.content.refs:
                log = await tx.get_log(ref.uuid) if ref.kind == "log" else None
                if ref.kind == "log" and (log is None or log.version != ref.version):
                    raise DietError("VERSION_CONFLICT", "Record changed; review purge again.")
                media_ids.update(await tx.purge_resource(ref, self.diet.clock()))
                if log and log.temporal.local_date:
                    await tx.set_day_status(log.temporal.local_date, "open")
            media_refs = []
            for media_id in media_ids:
                asset = await tx.get_media(media_id)
                if asset is not None and not await tx.media_referenced(media_id):
                    asset = asset.model_copy(
                        update={
                            "state": "purge_pending",
                            "version": asset.version + 1,
                            "updated_at": self.diet.clock(),
                        }
                    )
                    await tx.save_media(asset)
                    media_refs.append(ResourceRef(kind="media", id=media_id, version=asset.version))
            receipt = OperationReceipt(
                id=uuid4(),
                user_id=user_id,
                operation="confirm:purge",
                idempotency_key=key,
                request_hash=approved_hash,
                preview_id=preview_id,
                preview_version=version,
                result_refs=[*preview.content.refs, *media_refs],
                result_status="purged",
                created_at=self.diet.clock(),
            )
            await tx.save_receipt(receipt)
            await tx.save_preview(
                preview.model_copy(update={"state": "committed", "receipt_id": receipt.id})
            )
            await tx.advance_epoch(
                targets_changed=any(ref.kind == "target" for ref in preview.content.refs)
            )
            return receipt

    async def execute_purge(self, job: BackgroundJob) -> None:
        if not isinstance(job.payload, PurgePayload):
            raise DietError("INVALID_TASK", "Expected a purge request.")
        for ref in job.payload.refs:
            if ref.kind != "media":
                continue
            await self._delete_media(job.user_id, ref.uuid)

    async def _delete_media(self, user_id: int, media_id: UUID) -> None:
        async with (
            execution_fence(),
            self.diet.store.transaction(user_id, default_timezone=self.diet.default_timezone) as tx,
        ):
            asset = await tx.get_media(media_id)
            if asset is None or asset.state == "purged":
                return
            if await tx.media_referenced(media_id):
                raise DietError("MEDIA_REFERENCED", "清除中的图片被重新引用，拒绝删除。")
            if asset.state != "purge_pending":
                raise DietError("VERSION_CONFLICT", "Media deletion state changed.")
            await asyncio.to_thread(self.parser.media.delete, asset.storage_key)
            await tx.save_media(
                asset.model_copy(
                    update={
                        "state": "purged",
                        "version": asset.version + 1,
                        "purged_at": self.diet.clock(),
                        "updated_at": self.diet.clock(),
                    }
                )
            )
