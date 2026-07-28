from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import AuditEvent, RunJob
from quant_backtester.results import canonical_json
from quant_backtester.service.auth import Principal


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


ACTIVE_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


@dataclass(frozen=True)
class JobSnapshot:
    id: str
    tenant_id: str
    idempotency_key: str
    status: JobStatus
    payload: dict[str, object]
    result_run_id: str | None
    attempts: int
    max_attempts: int
    cancel_requested: bool
    worker_id: str | None
    error: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    started_at: dt.datetime | None
    completed_at: dt.datetime | None

    @classmethod
    def from_model(cls, job: RunJob) -> JobSnapshot:
        return cls(
            id=job.id,
            tenant_id=job.tenant_id,
            idempotency_key=job.idempotency_key,
            status=JobStatus(job.status),
            payload=dict(job.payload),
            result_run_id=job.result_run_id,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            cancel_requested=job.cancel_requested,
            worker_id=job.worker_id,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
        )


class JobConflictError(ValueError):
    pass


class JobQuotaError(ValueError):
    pass


@dataclass
class JobRepository:
    database: Database
    max_active_jobs_per_tenant: int = 20

    def submit(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        payload: dict[str, object],
        max_attempts: int = 3,
    ) -> JobSnapshot:
        normalized_key = idempotency_key.strip()
        if not 1 <= len(normalized_key) <= 128:
            raise ValueError("Idempotency-Key must contain 1 to 128 characters")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be in [1, 10]")

        now = dt.datetime.now(dt.UTC)
        with self.database.transaction() as session:
            if not self.database.is_sqlite:
                # Serialize a tenant's submission transaction so idempotency and
                # active-job quota checks remain correct under concurrent calls.
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant_id, 0))"),
                    {"tenant_id": principal.tenant_id},
                )
            existing = session.scalar(
                select(RunJob).where(
                    RunJob.tenant_id == principal.tenant_id,
                    RunJob.idempotency_key == normalized_key,
                )
            )
            if existing is not None:
                if canonical_json(existing.payload) != canonical_json(payload):
                    raise JobConflictError(
                        "Idempotency-Key was already used with a different payload"
                    )
                return JobSnapshot.from_model(existing)

            active_count = session.scalar(
                select(func.count())
                .select_from(RunJob)
                .where(
                    RunJob.tenant_id == principal.tenant_id,
                    RunJob.status.in_(ACTIVE_STATUSES),
                )
            )
            if int(active_count or 0) >= self.max_active_jobs_per_tenant:
                raise JobQuotaError("Tenant active-job quota exceeded")

            job = RunJob(
                id=str(uuid.uuid4()),
                tenant_id=principal.tenant_id,
                idempotency_key=normalized_key,
                status=JobStatus.QUEUED.value,
                payload=payload,
                attempts=0,
                max_attempts=max_attempts,
                cancel_requested=False,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            self._audit(
                session,
                principal=principal,
                action="job.submit",
                resource_id=job.id,
            )
            session.flush()
            return JobSnapshot.from_model(job)

    def get(self, job_id: str, *, tenant_id: str) -> JobSnapshot | None:
        with self.database.session() as session:
            job = session.scalar(
                select(RunJob).where(
                    RunJob.id == job_id,
                    RunJob.tenant_id == tenant_id,
                )
            )
            return JobSnapshot.from_model(job) if job is not None else None

    def list(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobSnapshot]:
        if not 1 <= limit <= 1_000 or offset < 0:
            raise ValueError("Invalid pagination")
        with self.database.session() as session:
            jobs = session.scalars(
                select(RunJob)
                .where(RunJob.tenant_id == tenant_id)
                .order_by(RunJob.created_at.desc(), RunJob.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [JobSnapshot.from_model(job) for job in jobs]

    def request_cancel(self, job_id: str, *, principal: Principal) -> JobSnapshot | None:
        now = dt.datetime.now(dt.UTC)
        with self.database.transaction() as session:
            job = session.scalar(
                select(RunJob)
                .where(
                    RunJob.id == job_id,
                    RunJob.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
            if job is None:
                return None
            if job.status == JobStatus.QUEUED.value:
                job.status = JobStatus.CANCELED.value
                job.completed_at = now
            elif job.status == JobStatus.RUNNING.value:
                job.cancel_requested = True
            job.updated_at = now
            self._audit(
                session,
                principal=principal,
                action="job.cancel",
                resource_id=job.id,
            )
            session.flush()
            return JobSnapshot.from_model(job)

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        job_id: str | None = None,
        tenant_id: str | None = None,
    ) -> JobSnapshot | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        now = dt.datetime.now(dt.UTC)
        with self.database.transaction() as session:
            statement = (
                select(RunJob)
                .where(
                    RunJob.cancel_requested.is_(False),
                    RunJob.attempts < RunJob.max_attempts,
                    or_(
                        RunJob.status == JobStatus.QUEUED.value,
                        and_(
                            RunJob.status == JobStatus.RUNNING.value,
                            RunJob.lease_expires_at < now,
                        ),
                    ),
                )
                .order_by(RunJob.created_at, RunJob.id)
                .limit(1)
            )
            if job_id is not None:
                statement = statement.where(RunJob.id == job_id)
            if tenant_id is not None:
                statement = statement.where(RunJob.tenant_id == tenant_id)
            if not self.database.is_sqlite:
                statement = statement.with_for_update(skip_locked=True)
            job = session.scalar(statement)
            if job is None:
                return None
            job.status = JobStatus.RUNNING.value
            job.attempts += 1
            job.worker_id = worker_id
            job.lease_expires_at = now + dt.timedelta(seconds=lease_seconds)
            job.started_at = job.started_at or now
            job.updated_at = now
            job.error = None
            session.flush()
            return JobSnapshot.from_model(job)

    def heartbeat(self, job_id: str, *, worker_id: str, lease_seconds: int) -> bool:
        now = dt.datetime.now(dt.UTC)
        with self.database.transaction() as session:
            job = session.scalar(
                select(RunJob).where(
                    RunJob.id == job_id,
                    RunJob.status == JobStatus.RUNNING.value,
                    RunJob.worker_id == worker_id,
                )
            )
            if job is None:
                return False
            job.lease_expires_at = now + dt.timedelta(seconds=lease_seconds)
            job.updated_at = now
            return not job.cancel_requested

    def complete(self, job_id: str, *, worker_id: str, result_run_id: str) -> None:
        now = dt.datetime.now(dt.UTC)
        with self.database.transaction() as session:
            job = self._owned_running_job(session, job_id, worker_id)
            job.status = JobStatus.SUCCEEDED.value
            job.result_run_id = result_run_id
            job.completed_at = now
            job.updated_at = now
            job.lease_expires_at = None

    def cancel_running(self, job_id: str, *, worker_id: str) -> None:
        now = dt.datetime.now(dt.UTC)
        with self.database.transaction() as session:
            job = self._owned_running_job(session, job_id, worker_id)
            job.status = JobStatus.CANCELED.value
            job.completed_at = now
            job.updated_at = now
            job.lease_expires_at = None

    def fail(self, job_id: str, *, worker_id: str, error: str) -> JobStatus:
        now = dt.datetime.now(dt.UTC)
        with self.database.transaction() as session:
            job = self._owned_running_job(session, job_id, worker_id)
            job.error = error[:4_000]
            job.worker_id = None
            job.lease_expires_at = None
            job.updated_at = now
            if job.cancel_requested:
                job.status = JobStatus.CANCELED.value
                job.completed_at = now
            elif job.attempts < job.max_attempts:
                job.status = JobStatus.QUEUED.value
            else:
                job.status = JobStatus.FAILED.value
                job.completed_at = now
            return JobStatus(job.status)

    @staticmethod
    def _owned_running_job(session: Session, job_id: str, worker_id: str) -> RunJob:
        job = session.scalar(
            select(RunJob)
            .where(
                RunJob.id == job_id,
                RunJob.status == JobStatus.RUNNING.value,
                RunJob.worker_id == worker_id,
            )
            .with_for_update()
        )
        if job is None:
            raise JobConflictError("Worker no longer owns this job lease")
        return job

    @staticmethod
    def _audit(
        session: Session,
        *,
        principal: Principal,
        action: str,
        resource_id: str,
    ) -> None:
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_id=principal.actor_id,
                action=action,
                resource_type="run_job",
                resource_id=resource_id,
                details={},
            )
        )
