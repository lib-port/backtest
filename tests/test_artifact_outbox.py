from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from quant_backtester.artifact_publication import publish_pending_artifacts
from quant_backtester.backtest import run_backtest
from quant_backtester.config import BacktestConfig
from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import ArtifactPublication, RunArtifact
from quant_backtester.results import ArtifactDescriptor, LocalArtifactStore
from quant_backtester.service.auth import Principal
from quant_backtester.service.jobs import JobConflictError, JobRepository, JobStatus


class FailingArtifactStore:
    def write_payload(
        self,
        *,
        run_id: str,
        kind: str,
        payload: bytes,
    ) -> ArtifactDescriptor:
        raise RuntimeError("simulated object-store outage")


def _config(tmp_path: Path, database: Database) -> BacktestConfig:
    return BacktestConfig(
        symbols=("AAPL", "MSFT"),
        csv_path="data/sample_prices.csv",
        out_dir=str(tmp_path / "artifacts"),
        database_url=database.url,
        short_window=5,
        long_window=10,
    )


def test_failed_publication_is_recoverable_before_job_completion(
    tmp_path: Path,
    database_factory,
) -> None:
    database = database_factory(tmp_path / "outbox.db")
    repository = JobRepository(database)
    principal = Principal(
        actor_id="outbox-test",
        tenant_id="tenant-a",
        roles=frozenset({"admin"}),
    )
    submitted = repository.submit(
        principal=principal,
        idempotency_key="recover-artifact",
        payload={"symbols": ["AAPL"]},
    )
    claimed = repository.claim(
        worker_id="worker-a",
        lease_seconds=60,
        job_id=submitted.id,
    )
    assert claimed is not None

    with pytest.raises(RuntimeError, match="Artifact publication failed"):
        run_backtest(
            _config(tmp_path, database),
            persist=True,
            run_id=submitted.id,
            tenant_id=principal.tenant_id,
            artifact_store=FailingArtifactStore(),
            persistence=database,
        )

    pending = database.pending_artifact_publications(
        submitted.id,
        tenant_id=principal.tenant_id,
    )
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert database.get_run(submitted.id, tenant_id=principal.tenant_id) is not None

    with pytest.raises(JobConflictError, match="artifacts"):
        repository.complete(
            submitted.id,
            worker_id="worker-a",
            lease_generation=claimed.lease_generation,
            result_run_id=submitted.id,
        )

    repository.wait_for_artifact(
        submitted.id,
        worker_id="worker-a",
        lease_generation=claimed.lease_generation,
        result_run_id=submitted.id,
    )
    waiting = repository.get(submitted.id, tenant_id=principal.tenant_id)
    assert waiting is not None
    assert waiting.status == JobStatus.WAITING_ARTIFACT
    with database.transaction() as session:
        publication = session.scalar(select(ArtifactPublication))
        assert publication is not None
        publication.next_attempt_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)

    publish_pending_artifacts(
        database,
        LocalArtifactStore(tmp_path / "recovered"),
        run_id=submitted.id,
        tenant_id=principal.tenant_id,
    )
    assert repository.reconcile_published_result(
        submitted.id,
        tenant_id=principal.tenant_id,
    )

    completed = repository.get(submitted.id, tenant_id=principal.tenant_id)
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED
    assert (
        database.pending_artifact_publications(
            submitted.id,
            tenant_id=principal.tenant_id,
        )
        == []
    )
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(RunArtifact)) == 1
        publication = session.scalar(select(ArtifactPublication))
        assert publication is not None
        assert publication.status == "PUBLISHED"
        assert publication.attempts == 2


def test_database_failure_cannot_leave_an_orphan_artifact(
    tmp_path: Path,
    database_factory,
) -> None:
    database = database_factory(tmp_path / "atomic.db")
    run_id = "44444444-4444-4444-8444-444444444444"
    config = _config(tmp_path, database)
    config = BacktestConfig(
        **{
            **config.__dict__,
            "dataset_id": "55555555-5555-4555-8555-555555555555",
        }
    )

    with pytest.raises(IntegrityError):
        run_backtest(
            config,
            persist=True,
            run_id=run_id,
            tenant_id="tenant-a",
            artifact_store=LocalArtifactStore(tmp_path / "must-remain-empty"),
            persistence=database,
        )

    assert not (tmp_path / "must-remain-empty" / run_id / "result.json").exists()
    assert database.get_run(run_id, tenant_id="tenant-a") is None


def test_local_artifact_publication_is_idempotent_and_immutable(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    run_id = "66666666-6666-4666-8666-666666666666"
    first = store.write_payload(run_id=run_id, kind="result", payload=b'{"ok":true}\n')
    replay = store.write_payload(run_id=run_id, kind="result", payload=b'{"ok":true}\n')
    assert replay == first

    with pytest.raises(RuntimeError, match="different bytes"):
        store.write_payload(run_id=run_id, kind="result", payload=b'{"ok":false}\n')
