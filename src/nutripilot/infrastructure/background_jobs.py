"""Durable diet jobs sharing the existing queue's scheduling and ownership locks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection

from nutripilot.domain.diet import DietError
from nutripilot.domain.diet_jobs import BackgroundJob, ExportArtifact, JobEvent
from nutripilot.domain.execution import ExecutionOwnershipLost
from nutripilot.infrastructure.chat_tasks import ChatTaskStore
from nutripilot.model_gateway.base import ModelUsage
from nutripilot.model_gateway.budget import ExecutionLimitExceeded

metadata = MetaData()
jobs = Table(
    "background_jobs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("queue", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("state", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("request_hash", String, nullable=False),
    Column("idempotency_key", String, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("lease_owner", String),
    Column("lease_version", BigInteger, nullable=False),
    Column("heartbeat_at", DateTime(timezone=True)),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("budget", JSON, nullable=False),
    Column("usage", JSON, nullable=False),
    Column("result_refs", JSON, nullable=False),
    Column("error_code", String),
    Column("artifact", JSON),
)
events = Table(
    "background_job_events",
    metadata,
    Column("job_id", Uuid, primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_type", String, nullable=False),
    Column("safe_payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _job(row: Any) -> BackgroundJob:
    values = dict(row)
    # SQLite does not preserve timezone information; all persisted job instants are UTC.
    for key in ("created_at", "available_at", "heartbeat_at", "finished_at"):
        if values.get(key) is not None and values[key].tzinfo is None:
            values[key] = values[key].replace(tzinfo=UTC)
    return BackgroundJob.model_validate(values)


class BackgroundJobStore:
    def __init__(self, chat: ChatTaskStore) -> None:
        self.chat = chat
        self.owner = str(uuid4())

    def initialize(self) -> bool:
        with self.chat.transaction() as connection:
            if not self.chat.postgres:
                metadata.create_all(connection)
            available = inspect(connection).has_table("background_jobs")
            self.chat.background_jobs_enabled = available
            return available

    def _get(self, connection: Connection, job_id: UUID, user_id: int) -> BackgroundJob:
        row = (
            connection.execute(
                select(jobs).where(
                    jobs.c.id == job_id,
                    jobs.c.user_id == user_id,
                    jobs.c.queue == self.chat.queue,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DietError("NOT_FOUND", "Task not found.")
        return _job(row)

    def get(self, job_id: UUID, user_id: int) -> BackgroundJob:
        with self.chat.transaction() as connection:
            return self._get(connection, job_id, user_id)

    def artifact_key_exists(self, key: str) -> bool:
        with self.chat.transaction() as connection:
            return (
                connection.execute(
                    select(jobs.c.id)
                    .where(jobs.c.artifact["storage_key"].as_string() == key)
                    .limit(1)
                ).first()
                is not None
            )

    def list_owned(self, user_id: int, before: datetime | None = None) -> list[BackgroundJob]:
        with self.chat.transaction() as connection:
            query = select(jobs).where(jobs.c.user_id == user_id, jobs.c.queue == self.chat.queue)
            if before is not None:
                query = query.where(jobs.c.created_at < before)
            return [
                _job(row)
                for row in connection.execute(
                    query.order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(50)
                ).mappings()
            ]

    def expired_exports(self, now: datetime) -> list[BackgroundJob]:
        with self.chat.transaction() as connection:
            rows = connection.execute(
                select(jobs)
                .where(
                    jobs.c.queue == self.chat.queue,
                    jobs.c.kind == "export",
                    jobs.c.state.in_(("succeeded", "failed", "cancelled")),
                    jobs.c.artifact.is_not(None),
                )
                .order_by(jobs.c.created_at, jobs.c.id)
            ).mappings()
            result = []
            for row in rows:
                job = _job(row)
                if job.artifact and job.artifact.expires_at <= now:
                    result.append(job)
                    if len(result) == 100:
                        break
            return result

    def revoke_artifact(self, job: BackgroundJob) -> None:
        with self.chat.transaction() as connection:
            current = self._get(connection, job.id, job.user_id)
            if current.artifact == job.artifact:
                self._save(connection, current.model_copy(update={"artifact": None}))

    def prune_events(self, now: datetime) -> None:
        with self.chat.transaction() as connection:
            connection.execute(
                events.delete().where(
                    events.c.created_at < now - timedelta(days=30),
                    events.c.job_id.in_(
                        select(jobs.c.id).where(
                            jobs.c.queue == self.chat.queue,
                            jobs.c.state.in_(("succeeded", "failed", "cancelled")),
                        )
                    ),
                )
            )

    def submit(self, job: BackgroundJob, max_pending: int) -> BackgroundJob:
        with self.chat.transaction() as connection:
            self.chat._schedule_lock(connection)
            row = (
                connection.execute(
                    select(jobs).where(
                        jobs.c.user_id == job.user_id,
                        jobs.c.kind == job.kind,
                        jobs.c.idempotency_key == job.idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if row:
                saved = _job(row)
                if saved.request_hash != job.request_hash or saved.queue != job.queue:
                    raise DietError("IDEMPOTENCY_CONFLICT", "Task key has different content.")
                return saved
            count = connection.execute(
                select(func.count())
                .select_from(jobs)
                .where(
                    jobs.c.user_id == job.user_id,
                    jobs.c.queue == job.queue,
                    jobs.c.state.in_(("queued", "running")),
                )
            ).scalar_one()
            count += connection.execute(
                text(
                    "SELECT COUNT(*) FROM chat_runs WHERE queue=:queue AND user_id=:owner "
                    "AND status IN ('queued','running','cancelling')"
                ),
                {"queue": job.queue, "owner": job.user_id},
            ).scalar_one()
            if count >= max_pending:
                raise DietError("QUEUE_FULL", "待处理任务已达上限，请稍后重试。")
            self._save(connection, job, insert=True)
            self._event(connection, job)
            return job

    @staticmethod
    def _save(connection: Connection, job: BackgroundJob, *, insert: bool = False) -> None:
        values = job.model_dump(mode="json")
        for key in ("id", "created_at", "available_at", "heartbeat_at", "finished_at"):
            values[key] = getattr(job, key)
        if insert:
            connection.execute(jobs.insert().values(**values))
        else:
            connection.execute(update(jobs).where(jobs.c.id == job.id).values(**values))

    @staticmethod
    def _event(connection: Connection, job: BackgroundJob) -> None:
        sequence = (
            connection.execute(
                select(func.coalesce(func.max(events.c.sequence), 0)).where(
                    events.c.job_id == job.id,
                )
            ).scalar_one()
            + 1
        )
        connection.execute(
            events.insert().values(
                job_id=job.id,
                user_id=job.user_id,
                sequence=sequence,
                event_type="status",
                safe_payload={"state": job.state, "error_code": job.error_code},
                created_at=datetime.now(UTC),
            )
        )

    def events(self, job_id: UUID, user_id: int, after: int) -> list[JobEvent]:
        with self.chat.transaction() as connection:
            self._get(connection, job_id, user_id)
            rows = (
                connection.execute(
                    select(events)
                    .where(
                        events.c.job_id == job_id,
                        events.c.user_id == user_id,
                        events.c.sequence > after,
                    )
                    .order_by(events.c.sequence)
                    .limit(100)
                )
                .mappings()
                .all()
            )
            return [
                JobEvent(
                    job_id=row["job_id"],
                    sequence=row["sequence"],
                    event_type=row["event_type"],
                    state=row["safe_payload"]["state"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    def recover(self, *, startup: bool = False) -> None:
        now = datetime.now(UTC)
        with self.chat.transaction() as connection:
            self.chat._schedule_lock(connection)
            query = select(jobs).where(jobs.c.queue == self.chat.queue, jobs.c.state == "running")
            if not startup:
                query = query.where(jobs.c.heartbeat_at < now - timedelta(seconds=30))
            for row in connection.execute(query).mappings():
                job = _job(row)
                exhausted = (
                    job.attempt >= 2
                    or (job.budget.deadline_at is not None and job.budget.deadline_at <= now)
                    or job.usage.reserved_tokens >= job.budget.token_limit
                )
                job = job.model_copy(
                    update={
                        "state": "failed" if exhausted else "queued",
                        "lease_owner": None,
                        "lease_version": job.lease_version + 1,
                        "finished_at": now if exhausted else None,
                        "error_code": "RECOVERY_LIMIT_EXCEEDED" if exhausted else None,
                    }
                )
                self._save(connection, job)
                self._event(connection, job)

    def claim(self, concurrency: int, per_user: int) -> BackgroundJob | None:
        now = datetime.now(UTC)
        with self.chat.transaction() as connection:
            self.chat._schedule_lock(connection)
            active = list(
                connection.execute(
                    select(jobs.c.user_id).where(
                        jobs.c.queue == self.chat.queue,
                        jobs.c.state == "running",
                        jobs.c.kind.in_(("parse_text", "parse_image")),
                    )
                ).scalars()
            )
            chatting = list(
                connection.execute(
                    text(
                        "SELECT user_id FROM chat_runs WHERE queue=:queue "
                        "AND status IN ('running','cancelling')"
                    ),
                    {"queue": self.chat.queue},
                ).scalars()
            )
            if len(active) + len(chatting) >= concurrency:
                return None
            rows = (
                connection.execute(
                    select(jobs)
                    .where(
                        jobs.c.queue == self.chat.queue,
                        jobs.c.state == "queued",
                        jobs.c.kind.in_(("parse_text", "parse_image")),
                        jobs.c.available_at <= now,
                    )
                    .order_by(jobs.c.created_at, jobs.c.id)
                )
                .mappings()
                .all()
            )
            selected = next(
                (
                    row
                    for row in rows
                    if row["user_id"] not in active
                    and (active + chatting).count(row["user_id"]) < per_user
                ),
                None,
            )
            if selected is None:
                return None
            job = _job(selected)
            job = job.model_copy(
                update={
                    "state": "running",
                    "attempt": job.attempt + 1,
                    "lease_owner": self.owner,
                    "lease_version": job.lease_version + 1,
                    "heartbeat_at": now,
                    "budget": job.budget.model_copy(
                        update={
                            "deadline_at": job.budget.deadline_at or now + timedelta(seconds=90),
                        }
                    ),
                }
            )
            self._save(connection, job)
            self._event(connection, job)
            return job

    def claim_maintenance(self) -> BackgroundJob | None:
        with self.chat.transaction() as connection:
            self.chat._schedule_lock(connection)
            active = connection.execute(
                select(func.count())
                .select_from(jobs)
                .where(
                    jobs.c.queue == self.chat.queue,
                    jobs.c.kind.in_(("export", "purge")),
                    jobs.c.state == "running",
                )
            ).scalar_one()
            if active:
                return None
            row = (
                connection.execute(
                    select(jobs)
                    .where(
                        jobs.c.queue == self.chat.queue,
                        jobs.c.kind.in_(("export", "purge")),
                        jobs.c.state == "queued",
                        jobs.c.available_at <= datetime.now(UTC),
                    )
                    .order_by(jobs.c.created_at, jobs.c.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            job = _job(row).model_copy(
                update={
                    "state": "running",
                    "attempt": row["attempt"] + 1,
                    "lease_owner": self.owner,
                    "lease_version": row["lease_version"] + 1,
                    "heartbeat_at": datetime.now(UTC),
                }
            )
            self._save(connection, job)
            self._event(connection, job)
            return job

    def _check(self, connection: Connection, job: BackgroundJob) -> BackgroundJob:
        query = select(jobs).where(jobs.c.id == job.id, jobs.c.user_id == job.user_id)
        if self.chat.postgres:
            query = query.with_for_update()
        row = connection.execute(query).mappings().first()
        current = _job(row) if row else None
        if (
            current is None
            or current.state != "running"
            or (current.lease_version, current.lease_owner) != (job.lease_version, self.owner)
        ):
            raise ExecutionOwnershipLost("Task lease has changed.")
        return current

    @contextmanager
    def fence(self, job: BackgroundJob) -> Iterator[None]:
        with self.chat.transaction() as connection:
            self._check(connection, job)
            yield

    def heartbeat(self, job: BackgroundJob) -> None:
        with self.chat.transaction() as connection:
            current = self._check(connection, job)
            self._save(connection, current.model_copy(update={"heartbeat_at": datetime.now(UTC)}))

    def account(self, job: BackgroundJob, reserved: int, used: int | None = None) -> None:
        with self.chat.transaction() as connection:
            current = self._check(connection, job)
            tokens = current.usage.reserved_tokens + (reserved if used is None else used - reserved)
            if tokens > current.budget.token_limit:
                raise ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED")
            self._save(
                connection,
                current.model_copy(
                    update={
                        "usage": current.usage.model_copy(
                            update={"reserved_tokens": tokens},
                        )
                    }
                ),
            )

    def finish(
        self,
        job: BackgroundJob,
        *,
        error: str | None = None,
        result: str = "not_applied",
        refs: list[Any] | None = None,
        artifact: ExportArtifact | None = None,
    ) -> BackgroundJob:
        with self.chat.transaction() as connection:
            current = self._check(connection, job)
            current = BackgroundJob.model_validate(
                {
                    **current.model_dump(),
                    "state": "failed" if error else "succeeded",
                    "error_code": error,
                    "finished_at": datetime.now(UTC),
                    "result_refs": refs or [],
                    "artifact": artifact,
                    "usage": {**current.usage.model_dump(), "result": result},
                }
            )
            self._save(connection, current)
            self._event(connection, current)
            return current

    def record_usage(self, job: BackgroundJob, usage: ModelUsage) -> None:
        with self.chat.transaction() as connection:
            current = self._check(connection, job)
            self._save(
                connection,
                current.model_copy(
                    update={
                        "usage": current.usage.model_copy(
                            update={
                                "input_tokens": current.usage.input_tokens + usage.input_tokens,
                                "output_tokens": current.usage.output_tokens + usage.output_tokens,
                                "provider_usage_known": usage.total_tokens > 0,
                            }
                        )
                    }
                ),
            )

    def retry(self, job: BackgroundJob, code: str) -> None:
        with self.chat.transaction() as connection:
            current = self._check(connection, job)
            now = datetime.now(UTC)
            recoverable = (
                current.attempt < 2
                and (current.budget.deadline_at is not None and current.budget.deadline_at > now)
                and current.usage.reserved_tokens < current.budget.token_limit
            )
            current = current.model_copy(
                update={
                    "state": "queued" if recoverable else "failed",
                    "error_code": code,
                    "lease_owner": None,
                    "lease_version": current.lease_version + 1,
                    "available_at": now + timedelta(seconds=1),
                    "finished_at": None if recoverable else now,
                }
            )
            self._save(connection, current)
            self._event(connection, current)

    def cancel(self, job_id: UUID, user_id: int) -> BackgroundJob:
        with self.chat.transaction() as connection:
            self.chat._schedule_lock(connection)
            job = self._get(connection, job_id, user_id)
            if job.kind == "purge" and job.state in ("queued", "running"):
                raise DietError("PURGE_IRREVERSIBLE", "永久清除已生效，不能取消清理。")
            if job.state in ("queued", "running"):
                job = job.model_copy(
                    update={
                        "state": "cancelled",
                        "lease_version": job.lease_version + 1,
                        "finished_at": datetime.now(UTC),
                    }
                )
                self._save(connection, job)
                self._event(connection, job)
            return job
