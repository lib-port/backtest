from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from quant_backtester.persistence.models import AuditEvent, DatasetVersion, Run
from quant_backtester.service.auth import AuthenticationError, Authenticator
from quant_backtester.service.settings import ApiKeyIdentity, ServiceSettings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _identity(token: str, tenant_id: str) -> ApiKeyIdentity:
    return ApiKeyIdentity(
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        actor_id=f"user@{tenant_id}",
        tenant_id=tenant_id,
        role="researcher",
        key_id=f"{tenant_id}-primary",
    )


def _run(*, tenant_id: str, dataset_id: str) -> Run:
    now = dt.datetime.now(dt.UTC)
    return Run(
        run_id="22222222-2222-4222-8222-222222222222",
        tenant_id=tenant_id,
        dataset_version_id=dataset_id,
        run_name="tenant-fk",
        status="SUCCEEDED",
        completed_at=now,
        symbols=["AAPL"],
        short_window=2,
        long_window=3,
        initial_cash=10_000.0,
        final_equity=10_100.0,
        total_return=0.01,
        sharpe=1.0,
        max_drawdown=0.0,
        total_commission=0.0,
        total_slippage_cost=0.0,
        halted=False,
        result_hash="a" * 64,
        config_hash="b" * 64,
        data_hash="c" * 64,
        engine_version="test",
    )


def test_database_rejects_cross_tenant_dataset_reference(
    tmp_path: Path,
    database_factory,
) -> None:
    database = database_factory(tmp_path / "isolation.db")
    dataset_id = "11111111-1111-4111-8111-111111111111"
    with database.transaction() as session:
        session.add(
            DatasetVersion(
                id=dataset_id,
                tenant_id="tenant-b",
                name="tenant-b-data",
                content_hash="d" * 64,
                source_uri="fixture.csv",
            )
        )

    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            session.add(_run(tenant_id="tenant-a", dataset_id=dataset_id))


def test_audit_events_are_append_only(tmp_path: Path, database_factory) -> None:
    database = database_factory(tmp_path / "audit.db")
    event_id = "33333333-3333-4333-8333-333333333333"
    with database.transaction() as session:
        session.add(
            AuditEvent(
                id=event_id,
                tenant_id="tenant-a",
                actor_id="security-test",
                action="test.create",
                resource_type="test",
                resource_id="one",
            )
        )

    with pytest.raises(IntegrityError, match="append-only"):
        with database.transaction() as session:
            event = session.get(AuditEvent, event_id)
            assert event is not None
            event.action = "test.tamper"

    with pytest.raises(IntegrityError, match="append-only"):
        with database.transaction() as session:
            event = session.get(AuditEvent, event_id)
            assert event is not None
            session.delete(event)


@pytest.mark.anyio
async def test_api_resources_are_invisible_across_tenants(tmp_path: Path, api_client) -> None:
    settings = ServiceSettings(
        database_url=f"sqlite:///{tmp_path / 'api-isolation.db'}",
        data_root=Path.cwd(),
        artifact_root=tmp_path,
        environment="test",
        auth_mode="api-key",
        api_keys=(
            _identity("tenant-a-token", "tenant-a"),
            _identity("tenant-b-token", "tenant-b"),
        ),
    )
    tenant_a = {"Authorization": "Bearer tenant-a-token"}
    tenant_b = {"Authorization": "Bearer tenant-b-token"}
    dataset_hash = hashlib.sha256(Path("data/sample_prices.csv").read_bytes()).hexdigest()
    async with api_client(settings) as client:
        registered = await client.post(
            "/v1/datasets",
            headers=tenant_a,
            json={
                "name": "tenant-a-dataset",
                "content_hash": dataset_hash,
                "source_uri": "data/sample_prices.csv",
            },
        )
        assert registered.status_code == 201
        dataset_id = registered.json()["id"]

        assert (await client.get(f"/v1/datasets/{dataset_id}", headers=tenant_b)).status_code == 404
        assert (await client.get("/v1/datasets", headers=tenant_b)).json()["items"] == []

        submitted = await client.post(
            "/v1/jobs",
            headers={**tenant_a, "Idempotency-Key": "tenant-bound-job"},
            json={
                "dataset_id": dataset_id,
                "config": {
                    "symbols": ["AAPL"],
                    "short_window": 2,
                    "long_window": 3,
                },
            },
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]
        assert (await client.get(f"/v1/jobs/{job_id}", headers=tenant_b)).status_code == 404
        assert (await client.get("/v1/jobs", headers=tenant_b)).json()["items"] == []


def test_oidc_claims_only_accept_known_roles(tmp_path: Path) -> None:
    settings = ServiceSettings(
        database_url="sqlite://",
        data_root=tmp_path,
        artifact_root=tmp_path,
        environment="test",
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="quant-backtester",
        oidc_jwks_url="https://identity.example.test/.well-known/jwks.json",
    )
    authenticator = Authenticator(settings)
    with pytest.raises(AuthenticationError, match="lacks subject"):
        authenticator._principal_from_claims(  # noqa: SLF001
            {"sub": "alice", "tenant_id": "tenant-a", "roles": ["root"]}
        )

    principal = authenticator._principal_from_claims(  # noqa: SLF001
        {
            "sub": "alice",
            "tenant_id": "tenant-a",
            "roles": ["researcher", "untrusted-role"],
        }
    )
    assert principal.roles == frozenset({"researcher"})
    assert principal.permits("observer")


@pytest.mark.anyio
async def test_production_hides_console_and_sets_strict_headers(
    tmp_path: Path,
    api_client,
) -> None:
    settings = ServiceSettings(
        database_url=f"sqlite:///{tmp_path / 'production.db'}",
        data_root=tmp_path,
        artifact_root=tmp_path,
        environment="production",
        auth_mode="api-key",
        api_keys=(_identity("production-token", "tenant-a"),),
        api_key_pepper="production-test-pepper-value-32+",
        dataset_bucket="immutable-datasets",
        require_registered_datasets=True,
    )
    async with api_client(settings) as client:
        response = await client.get("/")
        assert response.status_code == 404
        assert response.headers["strict-transport-security"].startswith("max-age=")
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        assert (await client.get("/docs")).status_code == 404
