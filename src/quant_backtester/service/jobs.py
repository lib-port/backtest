from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.orm import Session

from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import ArtifactPublication, AuditEvent, RunJob
from quant_backtester.results import canonical_json
from quant_backtester.service.auth import Principal


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_ARTIFACT = "WAITING_ARTIFACT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class FailureClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    INVARIANT = "INVARIANT"


class HeartbeatOutcome(StrEnum):
    ACTIVE = "ACTIVE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    LEASE_LOST = "LEASE_LOST"


ACTIVE_STATUSES = (
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.WAITING_ARTIFACT.value,
)


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
    lease_generation: int
    lease_expires_at: dt.datetime | None
    last_heartbeat_at: dt.datetime | None
    next_attempt_at: dt.datetime | None
    failure_class: FailureClass | None
    public_error_code: str | None
    internal_error_ref: str | None
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
            lease_generation=job.lease_generation,
            lease_expires_at=job.lease_expires_at,
            last_heartbeat_at=job.last_heartbeat_at,
            next_attempt_at=job.next_attempt_at,
            failure_class=FailureClass(job.failure_class) if job.failure_class else None,
            public_error_code=job.public_error_code,
            internal_error_ref=job.internal_error_ref,
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


@dataclass(frozen=True)
class ReapSummary:
    requeued: int = 0
    failed: int = 0
    canceled: int = 0
    waiting_artifact: int = 0
    succeeded: int = 0


@dataclass
class JobRepository:
    database: Database
    max_active_jobs_per_tenant: int = 20
    retry_base_seconds: int = 5
    retry_cap_seconds: int = 300
    reaper_batch_size: int = 1_000

    def __post_init__(self) -> None:
        if self.max_active_jobs_per_tenant <= 0:
            raise ValueError("max_active_jobs_per_tenant must be > 0")
        if self.retry_base_seconds <= 0 or self.retry_cap_seconds < self.retry_base_seconds:
            raise ValueError("retry delay settings are invalid")
        if self.reaper_batch_size <= 0:
            raise ValueError("reaper_batch_size must be > 0")

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

        with self.database.transaction(tenant_id=principal.tenant_id) as session:
            now = self._database_now(session)
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
                lease_generation=0,
                next_attempt_at=now,
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
        with self.database.session(tenant_id=tenant_id) as session:
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
        with self.database.session(tenant_id=tenant_id) as session:
            jobs = session.scalars(
                select(RunJob)
                .where(RunJob.tenant_id == tenant_id)
                .order_by(RunJob.created_at.desc(), RunJob.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [JobSnapshot.from_model(job) for job in jobs]

    def request_cancel(self, job_id: str, *, principal: Principal) -> JobSnapshot | None:
        with self.database.transaction(tenant_id=principal.tenant_id) as session:
            now = self._database_now(session)
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
                job.next_attempt_at = None
            elif job.status == JobStatus.RUNNING.value:
                job.cancel_requested = True
            elif job.status == JobStatus.WAITING_ARTIFACT.value:
                job.status = JobStatus.CANCELED.value
                job.completed_at = now
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
        with self.database.transaction() as session:
            now = self._database_now(session)
            self._reap_expired_in_session(
                session,
                now=now,
                limit=self.reaper_batch_size,
            )
            statement = (
                select(RunJob)
                .where(
                    RunJob.status == JobStatus.QUEUED.value,
                    RunJob.cancel_requested.is_(False),
                    RunJob.attempts < RunJob.max_attempts,
                    ~exists(
                        select(ArtifactPublication.id).where(
                            ArtifactPublication.tenant_id == RunJob.tenant_id,
                            ArtifactPublication.run_id == RunJob.id,
                            ArtifactPublication.status == "PENDING",
                        )
                    ),
                    or_(
                        RunJob.next_attempt_at.is_(None),
                        RunJob.next_attempt_at <= now,
                    ),
                )
                .order_by(RunJob.next_attempt_at, RunJob.created_at, RunJob.id)
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
            job.lease_generation += 1
            job.lease_expires_at = now + dt.timedelta(seconds=lease_seconds)
            job.last_heartbeat_at = now
            job.next_attempt_at = None
            job.started_at = job.started_at or now
            job.updated_at = now
            job.error = None
            job.failure_class = None
            job.public_error_code = None
            job.internal_error_ref = None
            session.flush()
            return JobSnapshot.from_model(job)

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        lease_seconds: int,
    ) -> HeartbeatOutcome:
        with self.database.transaction() as session:
            now = self._database_now(session)
            job = session.scalar(
                select(RunJob)
                .where(
                    RunJob.id == job_id,
                    RunJob.status == JobStatus.RUNNING.value,
                    RunJob.worker_id == worker_id,
                    RunJob.lease_generation == lease_generation,
                    RunJob.lease_expires_at >= now,
                )
                .with_for_update()
            )
            if job is None:
                return HeartbeatOutcome.LEASE_LOST
            job.lease_expires_at = now + dt.timedelta(seconds=lease_seconds)
            job.last_heartbeat_at = now
            job.updated_at = now
            return (
                HeartbeatOutcome.CANCEL_REQUESTED
                if job.cancel_requested
                else HeartbeatOutcome.ACTIVE
            )

    def complete(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        result_run_id: str,
    ) -> None:
        with self.database.transaction() as session:
            now = self._database_now(session)
            job = self._owned_running_job(
                session,
                job_id,
                worker_id,
                lease_generation,
                now,
                allow_cancel_requested=False,
            )
            unpublished = session.scalar(
                select(func.count())
                .select_from(ArtifactPublication)
                .where(
                    ArtifactPublication.tenant_id == job.tenant_id,
                    ArtifactPublication.run_id == result_run_id,
                    ArtifactPublication.status != "PUBLISHED",
                )
            )
            if int(unpublished or 0) > 0:
                raise JobConflictError("Run artifacts have not finished publishing")
            job.status = JobStatus.SUCCEEDED.value
            job.result_run_id = result_run_id
            job.completed_at = now
            job.updated_at = now
            job.worker_id = None
            job.lease_expires_at = None
            job.next_attempt_at = None

    def wait_for_artifact(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        result_run_id: str,
    ) -> None:
        with self.database.transaction() as session:
            now = self._database_now(session)
            job = self._owned_running_job(
                session,
                job_id,
                worker_id,
                lease_generation,
                now,
                allow_cancel_requested=False,
            )
            pending = session.scalar(
                select(func.count())
                .select_from(ArtifactPublication)
                .where(
                    ArtifactPublication.tenant_id == job.tenant_id,
                    ArtifactPublication.run_id == result_run_id,
                    ArtifactPublication.status == "PENDING",
                )
            )
            if int(pending or 0) == 0:
                raise JobConflictError("Run has no pending artifact publication")
            job.status = JobStatus.WAITING_ARTIFACT.value
            job.result_run_id = result_run_id
            job.updated_at = now
            job.worker_id = None
            job.lease_expires_at = None
            job.next_attempt_at = None
            job.failure_class = None
            job.public_error_code = None
            job.internal_error_ref = None
            job.error = None
            self._system_audit(
                session,
                job=job,
                action="job.waiting_artifact",
                details={"lease_generation": lease_generation},
            )

    def reconcile_published_result(self, run_id: str, *, tenant_id: str) -> bool:
        """Finish a waiting/recoverable job after every outbox row is published."""
        with self.database.transaction() as session:
            now = self._database_now(session)
            job = session.scalar(
                select(RunJob)
                .where(
                    RunJob.id == run_id,
                    RunJob.tenant_id == tenant_id,
                    RunJob.status.in_(
                        (
                            JobStatus.WAITING_ARTIFACT.value,
                            JobStatus.QUEUED.value,
                            JobStatus.FAILED.value,
                        )
                    ),
                    RunJob.cancel_requested.is_(False),
                )
                .with_for_update()
            )
            if job is None:
                return False
            pending = session.scalar(
                select(func.count())
                .select_from(ArtifactPublication)
                .where(
                    ArtifactPublication.tenant_id == tenant_id,
                    ArtifactPublication.run_id == run_id,
                    ArtifactPublication.status == "PENDING",
                )
            )
            published = session.scalar(
                select(func.count())
                .select_from(ArtifactPublication)
                .where(
                    ArtifactPublication.tenant_id == tenant_id,
                    ArtifactPublication.run_id == run_id,
                    ArtifactPublication.status == "PUBLISHED",
                )
            )
            if int(pending or 0) > 0 or int(published or 0) == 0:
                return False
            job.status = JobStatus.SUCCEEDED.value
            job.result_run_id = run_id
            job.completed_at = now
            job.updated_at = now
            job.worker_id = None
            job.lease_expires_at = None
            job.next_attempt_at = None
            job.failure_class = None
            job.public_error_code = None
            job.internal_error_ref = None
            job.error = None
            self._system_audit(
                session,
                job=job,
                action="job.artifact_reconciled",
                details={},
            )
            return True

    def cancel_running(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int,
    ) -> None:
        with self.database.transaction() as session:
            now = self._database_now(session)
            job = self._owned_running_job(
                session,
                job_id,
                worker_id,
                lease_generation,
                now,
                allow_cancel_requested=True,
            )
            job.status = JobStatus.CANCELED.value
            job.completed_at = now
            job.updated_at = now
            job.worker_id = None
            job.lease_expires_at = None
            job.next_attempt_at = None

    def release(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int,
    ) -> None:
        """Return an administratively interrupted lease without charging an attempt."""
        with self.database.transaction() as session:
            now = self._database_now(session)
            job = self._owned_running_job(
                session,
                job_id,
                worker_id,
                lease_generation,
                now,
                allow_cancel_requested=True,
            )
            job.worker_id = None
            job.lease_expires_at = None
            job.updated_at = now
            if job.cancel_requested:
                job.status = JobStatus.CANCELED.value
                job.completed_at = now
                job.next_attempt_at = None
                action = "job.release_canceled"
            else:
                job.status = JobStatus.QUEUED.value
                job.attempts = max(0, job.attempts - 1)
                job.next_attempt_at = now
                job.failure_class = None
                job.public_error_code = None
                job.internal_error_ref = None
                job.error = None
                action = "job.release_shutdown"
            self._system_audit(
                session,
                job=job,
                action=action,
                details={"lease_generation": lease_generation},
            )

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        error: str,
        failure_class: FailureClass = FailureClass.TRANSIENT,
        public_error_code: str = "JOB_ATTEMPT_FAILED",
        internal_error_ref: str | None = None,
    ) -> JobStatus:
        with self.database.transaction() as session:
            now = self._database_now(session)
            job = self._owned_running_job(
                session,
                job_id,
                worker_id,
                lease_generation,
                now,
                allow_cancel_requested=True,
            )
            job.error = error[:4_000]
            job.failure_class = failure_class.value
            job.public_error_code = public_error_code[:64]
            job.internal_error_ref = internal_error_ref or str(uuid.uuid4())
            job.worker_id = None
            job.lease_expires_at = None
            job.updated_at = now
            if job.cancel_requested:
                job.status = JobStatus.CANCELED.value
                job.completed_at = now
                job.next_attempt_at = None
            elif failure_class == FailureClass.TRANSIENT and job.attempts < job.max_attempts:
                job.status = JobStatus.QUEUED.value
                job.next_attempt_at = now + dt.timedelta(
                    seconds=self._retry_delay_seconds(job.id, job.attempts)
                )
            else:
                job.status = JobStatus.FAILED.value
                job.completed_at = now
                job.next_attempt_at = None
            return JobStatus(job.status)

    def reap_expired(self, *, limit: int | None = None) -> ReapSummary:
        resolved_limit = self.reaper_batch_size if limit is None else limit
        if resolved_limit <= 0:
            raise ValueError("reaper limit must be > 0")
        with self.database.transaction() as session:
            now = self._database_now(session)
            return self._reap_expired_in_session(
                session,
                now=now,
                limit=resolved_limit,
            )

    def _reap_expired_in_session(
        self,
        session: Session,
        *,
        now: dt.datetime,
        limit: int,
    ) -> ReapSummary:
        statement = (
            select(RunJob)
            .where(
                RunJob.status == JobStatus.RUNNING.value,
                RunJob.lease_expires_at < now,
            )
            .order_by(RunJob.lease_expires_at, RunJob.id)
            .limit(limit)
        )
        if not self.database.is_sqlite:
            statement = statement.with_for_update(skip_locked=True)
        expired = list(session.scalars(statement))
        requeued = failed = canceled = waiting_artifact = succeeded = 0
        for job in expired:
            artifact_statuses = (
                set()
                if job.cancel_requested
                else set(
                    session.scalars(
                        select(ArtifactPublication.status).where(
                            ArtifactPublication.tenant_id == job.tenant_id,
                            ArtifactPublication.run_id == job.id,
                        )
                    )
                )
            )
            job.worker_id = None
            job.lease_expires_at = None
            job.updated_at = now
            job.failure_class = FailureClass.TRANSIENT.value
            job.public_error_code = "WORKER_LEASE_EXPIRED"
            job.internal_error_ref = str(uuid.uuid4())
            job.error = "Worker lease expired before the attempt reached a terminal state"
            if job.cancel_requested:
                job.status = JobStatus.CANCELED.value
                job.completed_at = now
                job.next_attempt_at = None
                canceled += 1
                action = "job.reap_canceled"
            else:
                if "PENDING" in artifact_statuses:
                    job.status = JobStatus.WAITING_ARTIFACT.value
                    job.result_run_id = job.id
                    job.failure_class = None
                    job.public_error_code = None
                    job.internal_error_ref = None
                    job.error = None
                    job.next_attempt_at = None
                    waiting_artifact += 1
                    action = "job.reap_waiting_artifact"
                elif artifact_statuses == {"PUBLISHED"}:
                    job.status = JobStatus.SUCCEEDED.value
                    job.result_run_id = job.id
                    job.failure_class = None
                    job.public_error_code = None
                    job.internal_error_ref = None
                    job.error = None
                    job.completed_at = now
                    job.next_attempt_at = None
                    succeeded += 1
                    action = "job.reap_artifact_reconciled"
                elif job.attempts >= job.max_attempts:
                    job.status = JobStatus.FAILED.value
                    job.completed_at = now
                    job.next_attempt_at = None
                    failed += 1
                    action = "job.reap_failed"
                else:
                    job.status = JobStatus.QUEUED.value
                    job.next_attempt_at = now + dt.timedelta(
                        seconds=self._retry_delay_seconds(job.id, job.attempts)
                    )
                    requeued += 1
                    action = "job.reap_requeued"
            self._system_audit(
                session,
                job=job,
                action=action,
                details={"attempt": job.attempts, "lease_generation": job.lease_generation},
            )
        return ReapSummary(
            requeued=requeued,
            failed=failed,
            canceled=canceled,
            waiting_artifact=waiting_artifact,
            succeeded=succeeded,
        )

    @staticmethod
    def _owned_running_job(
        session: Session,
        job_id: str,
        worker_id: str,
        lease_generation: int,
        now: dt.datetime,
        *,
        allow_cancel_requested: bool,
    ) -> RunJob:
        statement = select(RunJob).where(
            RunJob.id == job_id,
            RunJob.status == JobStatus.RUNNING.value,
            RunJob.worker_id == worker_id,
            RunJob.lease_generation == lease_generation,
            RunJob.lease_expires_at >= now,
        )
        if not allow_cancel_requested:
            statement = statement.where(RunJob.cancel_requested.is_(False))
        job = session.scalar(statement.with_for_update())
        if job is None:
            raise JobConflictError("Worker no longer owns this job lease")
        return job

    def _retry_delay_seconds(self, job_id: str, attempt: int) -> int:
        exponential: int = min(
            self.retry_cap_seconds,
            self.retry_base_seconds * (2 ** max(0, attempt - 1)),
        )
        jitter_range: int = max(1, exponential // 2)
        digest_prefix = hashlib.sha256(f"{job_id}:{attempt}".encode()).hexdigest()[:8]
        jitter = int(digest_prefix, 16) % (jitter_range + 1)
        return int(min(self.retry_cap_seconds, exponential + jitter))

    @staticmethod
    def _database_now(session: Session) -> dt.datetime:
        value = session.scalar(select(func.now()))
        if not isinstance(value, dt.datetime):
            raise RuntimeError("Database did not return a timestamp")
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)

    @staticmethod
    def _system_audit(
        session: Session,
        *,
        job: RunJob,
        action: str,
        details: dict[str, object],
    ) -> None:
        session.add(
            AuditEvent(
                tenant_id=job.tenant_id,
                actor_id="system:job-reaper",
                action=action,
                resource_type="run_job",
                resource_id=job.id,
                details=details,
            )
        )

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
