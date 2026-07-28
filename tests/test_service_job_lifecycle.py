from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import httpx2
import pytest

from quant_backtester.service.api import create_app
from quant_backtester.service.auth import Authenticator
from quant_backtester.service.settings import (
    ApiKeyIdentity,
    ServiceSettings,
    WorkerSettings,
)
from quant_backtester.service.worker import Worker


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        database_url=f"sqlite:///{tmp_path / 'service.db'}",
        data_root=Path.cwd(),
        artifact_root=tmp_path / "artifacts",
        environment="test",
        auth_mode="disabled",
    )


def _submission(run_name: str = "api-run") -> dict[str, object]:
    return {
        "config": {
            "symbols": ["AAPL", "MSFT"],
            "csv_path": "data/sample_prices.csv",
            "run_name": run_name,
            "short_window": 5,
            "long_window": 10,
        }
    }


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_api_and_worker_complete_idempotent_job(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "request-1"},
            json=_submission(),
        )
        assert first.status_code == 202
        job_id = first.json()["id"]

        replay = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "request-1"},
            json=_submission(),
        )
        assert replay.status_code == 202
        assert replay.json()["id"] == job_id

        conflict = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "request-1"},
            json=_submission("different"),
        )
        assert conflict.status_code == 409

        worker = Worker(
            WorkerSettings(
                database_url=settings.database_url,
                data_root=settings.data_root,
                artifact_root=settings.artifact_root,
                worker_id="test-worker",
                poll_seconds=0.01,
                lease_seconds=30,
            )
        )
        assert worker.run_once()
        assert not worker.run_once()

        job = await client.get(f"/v1/jobs/{job_id}")
        assert job.status_code == 200
        assert job.json()["status"] == "SUCCEEDED"
        assert job.json()["result_run_id"] == job_id

        run = await client.get(f"/v1/runs/{job_id}")
        assert run.status_code == 200
        assert run.json()["result_hash"]
        assert run.json()["data_hash"]

    artifact = settings.artifact_root / job_id / "result.json"
    assert artifact.is_file()

    connection = sqlite3.connect(tmp_path / "service.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM run_orders").fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM run_fills").fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] > 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_queued_job_can_be_canceled(tmp_path: Path) -> None:
    transport = httpx2.ASGITransport(app=create_app(_settings(tmp_path)))
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "cancel-me"},
            json=_submission(),
        )
        job_id = submitted.json()["id"]
        canceled = await client.post(f"/v1/jobs/{job_id}/cancel")
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "CANCELED"


@pytest.mark.anyio
async def test_registered_dataset_is_linked_and_hash_verified(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    transport = httpx2.ASGITransport(app=create_app(settings))
    dataset_path = Path("data/sample_prices.csv")
    digest = hashlib.sha256(dataset_path.read_bytes()).hexdigest()

    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        registered = await client.post(
            "/v1/datasets",
            json={
                "name": "sample-v1",
                "content_hash": digest,
                "source_uri": str(dataset_path),
                "metadata": {"vendor": "test"},
            },
        )
        assert registered.status_code == 201
        dataset_id = registered.json()["id"]
        replay = await client.post(
            "/v1/datasets",
            json={
                "name": "sample-v1",
                "content_hash": digest,
                "source_uri": str(dataset_path),
            },
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == dataset_id

        submitted = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "registered-data"},
            json={
                "dataset_id": dataset_id,
                "config": {
                    "symbols": ["AAPL", "MSFT"],
                    "short_window": 5,
                    "long_window": 10,
                },
            },
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]

    worker = Worker(
        WorkerSettings(
            database_url=settings.database_url,
            data_root=settings.data_root,
            artifact_root=settings.artifact_root,
            worker_id="dataset-worker",
            poll_seconds=0.01,
            lease_seconds=30,
            require_dataset_hash=True,
        )
    )
    assert worker.run_once()

    transport = httpx2.ASGITransport(app=create_app(settings))
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        run = await client.get(f"/v1/runs/{job_id}")
        assert run.status_code == 200
        assert run.json()["dataset_id"] == dataset_id


@pytest.mark.anyio
async def test_hashed_api_key_authentication_and_production_guard(tmp_path: Path) -> None:
    token = "correct-horse-battery-staple"
    identity = ApiKeyIdentity(
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        actor_id="researcher@example.test",
        tenant_id="tenant-a",
        role="researcher",
    )
    settings = ServiceSettings(
        database_url=f"sqlite:///{tmp_path / 'auth.db'}",
        data_root=Path.cwd(),
        artifact_root=tmp_path,
        environment="test",
        auth_mode="api-key",
        api_keys=(identity,),
    )
    principal = Authenticator(settings).authenticate(token)
    assert principal.tenant_id == "tenant-a"
    assert principal.permits("researcher")

    transport = httpx2.ASGITransport(app=create_app(settings))
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/jobs")).status_code == 401
        assert (
            await client.get(
                "/v1/jobs",
                headers={"Authorization": f"Bearer {token}"},
            )
        ).status_code == 200

    with pytest.raises(ValueError, match="cannot be disabled"):
        ServiceSettings(
            database_url="sqlite://",
            data_root=Path.cwd(),
            artifact_root=tmp_path,
            environment="production",
            auth_mode="disabled",
        )

    with pytest.raises(ValueError, match="disable local datasets"):
        WorkerSettings(
            database_url="sqlite://",
            data_root=Path.cwd(),
            artifact_root=tmp_path,
            worker_id="unsafe-production-worker",
            environment="production",
        )
