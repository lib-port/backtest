from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from quant_backtester.service.settings import ServiceSettings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(tmp_path: Path, **overrides: object) -> ServiceSettings:
    values: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'limits.db'}",
        "data_root": Path.cwd(),
        "artifact_root": tmp_path,
        "environment": "test",
        "auth_mode": "disabled",
    }
    values.update(overrides)
    return ServiceSettings(**values)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_api_uses_strict_nested_configuration_models(tmp_path: Path, api_client) -> None:
    async with api_client(_settings(tmp_path)) as client:
        coerced = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "coerced"},
            json={"config": {"symbols": ["AAPL"], "initial_cash": "100000"}},
        )
        assert coerced.status_code == 422

        typo = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "typo"},
            json={
                "config": {
                    "symbols": ["AAPL"],
                    "risk": {"max_postion_per_symbol": 10},
                }
            },
        )
        assert typo.status_code == 422

        duplicate = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "duplicates"},
            json={"config": {"symbols": ["aapl", "AAPL"]}},
        )
        assert duplicate.status_code == 422


@pytest.mark.anyio
async def test_api_enforces_deployment_compute_and_body_limits(tmp_path: Path, api_client) -> None:
    settings = _settings(
        tmp_path,
        max_symbols_per_run=1,
        max_attempts_per_job=2,
        max_request_bytes=512,
    )
    async with api_client(settings) as client:
        too_many_symbols = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "symbols"},
            json={"config": {"symbols": ["AAPL", "MSFT"]}},
        )
        assert too_many_symbols.status_code == 422

        too_many_attempts = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "attempts"},
            json={"config": {"symbols": ["AAPL"]}, "max_attempts": 3},
        )
        assert too_many_attempts.status_code == 422

        oversized = await client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "oversized"},
            content=b"{" + b" " * 1_000 + b"}",
        )
        assert oversized.status_code == 413


@pytest.mark.anyio
async def test_local_dataset_registration_verifies_claimed_hash(
    tmp_path: Path,
    api_client,
) -> None:
    actual_hash = hashlib.sha256(Path("data/sample_prices.csv").read_bytes()).hexdigest()
    async with api_client(_settings(tmp_path)) as client:
        mismatch = await client.post(
            "/v1/datasets",
            json={
                "name": "tampered",
                "content_hash": "0" * 64,
                "source_uri": "data/sample_prices.csv",
            },
        )
        assert mismatch.status_code == 422

        valid = await client.post(
            "/v1/datasets",
            json={
                "name": "verified",
                "content_hash": actual_hash,
                "source_uri": "data/sample_prices.csv",
            },
        )
        assert valid.status_code == 201
