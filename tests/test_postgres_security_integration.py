from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from quant_backtester.persistence.bootstrap import bootstrap_application_role
from quant_backtester.persistence.db import Database
from quant_backtester.service.auth import Principal
from quant_backtester.service.jobs import JobRepository

ADMIN_DATABASE_URL = os.getenv("QBT_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not ADMIN_DATABASE_URL,
    reason="QBT_TEST_POSTGRES_URL is required for PostgreSQL integration tests",
)


def _role_url(admin_url: str, *, username: str, password: str) -> str:
    return (
        make_url(admin_url)
        .set(username=username, password=password)
        .render_as_string(hide_password=False)
    )


@pytest.mark.integration
def test_runtime_roles_enforce_privileges_and_rls() -> None:
    assert ADMIN_DATABASE_URL is not None
    api_username = "qbt_test_api"
    worker_username = "qbt_test_worker"
    api_password = "api-integration-password-32-value"
    worker_password = "worker-integration-password-value"

    bootstrap_application_role(
        admin_database_url=ADMIN_DATABASE_URL,
        application_username=api_username,
        application_password=api_password,
        role_kind="api",
    )
    bootstrap_application_role(
        admin_database_url=ADMIN_DATABASE_URL,
        application_username=worker_username,
        application_password=worker_password,
        role_kind="worker",
    )

    api_database = Database(
        _role_url(ADMIN_DATABASE_URL, username=api_username, password=api_password)
    )
    worker_database = Database(
        _role_url(ADMIN_DATABASE_URL, username=worker_username, password=worker_password)
    )
    try:
        api_database.prepare_schema()
        worker_database.prepare_schema()

        jobs = JobRepository(api_database)
        tenant_a = f"integration-a-{uuid.uuid4()}"
        tenant_b = f"integration-b-{uuid.uuid4()}"
        principal_a = Principal("integration", tenant_a, frozenset({"researcher"}))
        principal_b = Principal("integration", tenant_b, frozenset({"researcher"}))
        job_a = jobs.submit(
            principal=principal_a,
            idempotency_key="security-integration",
            payload={"csv_path": "dataset-a.csv"},
        )
        job_b = jobs.submit(
            principal=principal_b,
            idempotency_key="security-integration",
            payload={"csv_path": "dataset-b.csv"},
        )

        assert jobs.get(job_a.id, tenant_id=tenant_a) is not None
        assert jobs.get(job_a.id, tenant_id=tenant_b) is None
        with api_database.engine().connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM run_jobs")) == 0
            with pytest.raises(ProgrammingError):
                connection.execute(
                    text("DELETE FROM run_jobs WHERE id = :job_id"),
                    {"job_id": job_a.id},
                )

        with worker_database.engine().connect() as connection:
            visible_ids = set(
                connection.execute(
                    text("SELECT id FROM run_jobs WHERE id IN (:job_a, :job_b)"),
                    {"job_a": job_a.id, "job_b": job_b.id},
                ).scalars()
            )
        assert visible_ids == {job_a.id, job_b.id}

        admin_engine = create_engine(ADMIN_DATABASE_URL)
        try:
            with admin_engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT has_table_privilege(:role, 'run_jobs', 'DELETE')"),
                        {"role": api_username},
                    )
                    is False
                )
                assert (
                    connection.scalar(
                        text("SELECT has_table_privilege(:role, 'run_jobs', 'UPDATE')"),
                        {"role": worker_username},
                    )
                    is True
                )
        finally:
            admin_engine.dispose()
    finally:
        api_database.close()
        worker_database.close()
