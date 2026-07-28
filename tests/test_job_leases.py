from __future__ import annotations

import datetime as dt
from pathlib import Path

from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import RunJob
from quant_backtester.service.auth import Principal
from quant_backtester.service.jobs import (
    FailureClass,
    HeartbeatOutcome,
    JobRepository,
    JobStatus,
)


def _repository(tmp_path: Path, database_factory) -> tuple[Database, JobRepository, Principal]:
    database = database_factory(tmp_path / "jobs.db")
    repository = JobRepository(
        database,
        retry_base_seconds=1,
        retry_cap_seconds=2,
    )
    principal = Principal(
        actor_id="lease-test",
        tenant_id="tenant-a",
        roles=frozenset({"admin"}),
    )
    return database, repository, principal


def _expire(database: Database, job_id: str) -> None:
    with database.transaction() as session:
        job = session.get(RunJob, job_id)
        assert job is not None
        job.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)


def test_reaper_fails_expired_final_attempt(tmp_path: Path, database_factory) -> None:
    database, repository, principal = _repository(tmp_path, database_factory)
    submitted = repository.submit(
        principal=principal,
        idempotency_key="final-attempt",
        payload={"symbols": ["AAPL"]},
        max_attempts=1,
    )
    claimed = repository.claim(
        worker_id="dead-worker",
        lease_seconds=30,
        job_id=submitted.id,
    )
    assert claimed is not None
    _expire(database, submitted.id)

    summary = repository.reap_expired()
    snapshot = repository.get(submitted.id, tenant_id=principal.tenant_id)

    assert summary.failed == 1
    assert snapshot is not None
    assert snapshot.status == JobStatus.FAILED
    assert snapshot.public_error_code == "WORKER_LEASE_EXPIRED"
    assert snapshot.completed_at is not None
    assert (
        repository.claim(
            worker_id="new-worker",
            lease_seconds=30,
            job_id=submitted.id,
        )
        is None
    )


def test_reaper_cancels_expired_requested_job(tmp_path: Path, database_factory) -> None:
    database, repository, principal = _repository(tmp_path, database_factory)
    submitted = repository.submit(
        principal=principal,
        idempotency_key="cancel-dead-worker",
        payload={"symbols": ["AAPL"]},
    )
    claimed = repository.claim(
        worker_id="dead-worker",
        lease_seconds=30,
        job_id=submitted.id,
    )
    assert claimed is not None
    repository.request_cancel(submitted.id, principal=principal)
    _expire(database, submitted.id)

    summary = repository.reap_expired()
    snapshot = repository.get(submitted.id, tenant_id=principal.tenant_id)

    assert summary.canceled == 1
    assert snapshot is not None
    assert snapshot.status == JobStatus.CANCELED
    assert snapshot.completed_at is not None


def test_reclaim_increments_fence_and_rejects_stale_heartbeat(
    tmp_path: Path,
    database_factory,
) -> None:
    database, repository, principal = _repository(tmp_path, database_factory)
    submitted = repository.submit(
        principal=principal,
        idempotency_key="fenced-retry",
        payload={"symbols": ["AAPL"]},
    )
    first = repository.claim(
        worker_id="worker-one",
        lease_seconds=30,
        job_id=submitted.id,
    )
    assert first is not None
    _expire(database, submitted.id)
    assert repository.reap_expired().requeued == 1

    with database.transaction() as session:
        job = session.get(RunJob, submitted.id)
        assert job is not None
        job.next_attempt_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)

    second = repository.claim(
        worker_id="worker-two",
        lease_seconds=30,
        job_id=submitted.id,
    )
    assert second is not None
    assert second.lease_generation == first.lease_generation + 1
    assert (
        repository.heartbeat(
            submitted.id,
            worker_id="worker-one",
            lease_generation=first.lease_generation,
            lease_seconds=30,
        )
        == HeartbeatOutcome.LEASE_LOST
    )
    assert (
        repository.heartbeat(
            submitted.id,
            worker_id="worker-two",
            lease_generation=second.lease_generation,
            lease_seconds=30,
        )
        == HeartbeatOutcome.ACTIVE
    )


def test_permanent_failure_does_not_consume_retry_budget(
    tmp_path: Path,
    database_factory,
) -> None:
    _database, repository, principal = _repository(tmp_path, database_factory)
    submitted = repository.submit(
        principal=principal,
        idempotency_key="permanent-error",
        payload={"symbols": ["AAPL"]},
        max_attempts=3,
    )
    claimed = repository.claim(
        worker_id="worker",
        lease_seconds=30,
        job_id=submitted.id,
    )
    assert claimed is not None

    status = repository.fail(
        submitted.id,
        worker_id="worker",
        lease_generation=claimed.lease_generation,
        error="invalid dataset",
        failure_class=FailureClass.PERMANENT,
        public_error_code="INVALID_DATASET",
    )
    snapshot = repository.get(submitted.id, tenant_id=principal.tenant_id)

    assert status == JobStatus.FAILED
    assert snapshot is not None
    assert snapshot.attempts == 1
    assert snapshot.public_error_code == "INVALID_DATASET"
