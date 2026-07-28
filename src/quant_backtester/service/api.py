from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import parse_qs, unquote, urlsplit

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint

from quant_backtester import __version__
from quant_backtester.config import backtest_config_from_mapping
from quant_backtester.logging_context import log_context, tenant_id_var
from quant_backtester.logging_utils import configure_logging
from quant_backtester.observability import HTTP_DURATION, HTTP_REQUESTS, instrument_app
from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import Run
from quant_backtester.service.auth import AuthenticationError, Authenticator, Principal
from quant_backtester.service.datasets import DatasetRepository, DatasetSnapshot
from quant_backtester.service.jobs import (
    JobConflictError,
    JobQuotaError,
    JobRepository,
    JobSnapshot,
)
from quant_backtester.service.queue import SQSJobNotifier
from quant_backtester.service.settings import ServiceSettings

logger = logging.getLogger(__name__)


class RunSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any]
    dataset_id: str | None = Field(default=None, min_length=36, max_length=36)
    max_attempts: int = Field(default=3, ge=1, le=10)


class DatasetRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_uri: str = Field(min_length=1, max_length=2_048)
    schema_version: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _timestamp(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


def _job_document(job: JobSnapshot, *, include_payload: bool = False) -> dict[str, object]:
    document: dict[str, object] = {
        "id": job.id,
        "status": job.status.value,
        "result_run_id": job.result_run_id,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "cancel_requested": job.cancel_requested,
        "error": job.error,
        "created_at": _timestamp(job.created_at),
        "updated_at": _timestamp(job.updated_at),
        "started_at": _timestamp(job.started_at),
        "completed_at": _timestamp(job.completed_at),
    }
    if include_payload:
        document["config"] = job.payload
    return document


def _run_document(run: Run) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "run_name": run.run_name,
        "dataset_id": run.dataset_version_id,
        "status": run.status,
        "symbols": run.symbols,
        "created_at": _timestamp(run.created_at),
        "completed_at": _timestamp(run.completed_at),
        "initial_cash": float(run.initial_cash),
        "final_equity": float(run.final_equity),
        "total_return": run.total_return,
        "sharpe": run.sharpe,
        "max_drawdown": run.max_drawdown,
        "halted": run.halted,
        "halt_reason": run.halt_reason,
        "result_hash": run.result_hash,
        "config_hash": run.config_hash,
        "data_hash": run.data_hash,
        "engine_version": run.engine_version,
        "manifest": run.manifest,
    }


def _dataset_document(dataset: DatasetSnapshot) -> dict[str, object]:
    return {
        "id": dataset.id,
        "name": dataset.name,
        "content_hash": dataset.content_hash,
        "schema_version": dataset.schema_version,
        "source_uri": dataset.source_uri,
        "metadata": dataset.metadata,
        "created_at": _timestamp(dataset.created_at),
    }


def _validate_dataset_uri(uri: str, settings: ServiceSettings) -> None:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3":
        if settings.environment == "production":
            raise ValueError("Production datasets must use an s3:// URI")
        return
    if settings.dataset_bucket and parsed.netloc != settings.dataset_bucket:
        raise ValueError("Dataset URI uses an unauthorized S3 bucket")
    prefix = settings.dataset_prefix.strip("/") + "/"
    if not unquote(parsed.path.lstrip("/")).startswith(prefix):
        raise ValueError("Dataset URI is outside the authorized S3 prefix")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {"versionId"} or len(query["versionId"]) != 1 or not query["versionId"][0]:
        raise ValueError("Dataset S3 URI must contain exactly one versionId")
    if parsed.fragment:
        raise ValueError("Dataset URI must not contain a fragment")


def create_app(settings: ServiceSettings | None = None) -> FastAPI:
    resolved_settings = settings or ServiceSettings.from_env()
    database = Database(resolved_settings.database_url)
    database.prepare_schema()
    jobs = JobRepository(
        database,
        max_active_jobs_per_tenant=resolved_settings.max_active_jobs_per_tenant,
    )
    datasets = DatasetRepository(database)
    authenticator = Authenticator(resolved_settings)
    notifier = SQSJobNotifier(resolved_settings.queue_url) if resolved_settings.queue_url else None
    bearer = HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            database.close()

    app = FastAPI(
        title="Quant Backtester Control Plane",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = database
    app.state.jobs = jobs
    app.state.datasets = datasets

    def current_principal(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> Principal:
        token = credentials.credentials if credentials is not None else None
        try:
            principal = authenticator.authenticate(token)
            tenant_id_var.set(principal.tenant_id)
            return principal
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @app.middleware("http")
    async def operational_headers(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                request_bytes = int(content_length)
            except ValueError:
                return Response(status_code=status.HTTP_400_BAD_REQUEST)
            if request_bytes > resolved_settings.max_request_bytes:
                return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        started = time.perf_counter()
        with log_context(request_id=request_id, tenant_id="-"):
            response = await call_next(request)
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        HTTP_REQUESTS.labels(
            method=request.method,
            route=route_path,
            status=str(response.status_code),
        ).inc()
        HTTP_DURATION.labels(method=request.method, route=route_path).observe(
            time.perf_counter() - started
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready() -> dict[str, str]:
        if not database.ping():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database unavailable",
            )
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics(
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> Response:
        _require_role(principal, "observer")
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/jobs", status_code=status.HTTP_202_ACCEPTED)
    def submit_job(
        submission: RunSubmission,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "researcher")
        try:
            config_input = dict(submission.config)
            if "dataset_id" in config_input:
                raise ValueError("Pass dataset_id beside config, not inside config")
            if submission.dataset_id is not None:
                if "csv_path" in config_input or "dataset_sha256" in config_input:
                    raise ValueError(
                        "A registered dataset_id cannot be combined with csv_path or dataset_sha256"
                    )
                dataset = datasets.get(
                    submission.dataset_id,
                    tenant_id=principal.tenant_id,
                )
                if dataset is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Dataset version not found",
                    )
                config_input["dataset_id"] = dataset.id
                config_input["dataset_sha256"] = dataset.content_hash
                config_input["csv_path"] = dataset.source_uri
            elif resolved_settings.require_registered_datasets:
                raise ValueError("dataset_id is required")
            config = backtest_config_from_mapping(
                config_input,
                database_url=resolved_settings.database_url,
                out_dir=str(resolved_settings.artifact_root),
            )
            payload = config.to_dict(redact_secrets=True)
            payload.pop("database_url", None)
            payload.pop("out_dir", None)
            job = jobs.submit(
                principal=principal,
                idempotency_key=idempotency_key,
                payload=payload,
                max_attempts=submission.max_attempts,
            )
            if notifier is not None:
                try:
                    notifier.notify(job_id=job.id, tenant_id=job.tenant_id)
                except Exception:
                    # The database row is authoritative, and workers retain a
                    # periodic fallback poll if an SQS wake-up is lost.
                    logger.exception(
                        "Failed to publish job wake-up",
                        extra={"event": {"job_id": job.id}},
                    )
        except JobConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except JobQuotaError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        return _job_document(job, include_payload=True)

    @app.post("/v1/datasets", status_code=status.HTTP_201_CREATED)
    def register_dataset(
        registration: DatasetRegistration,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "researcher")
        try:
            _validate_dataset_uri(registration.source_uri, resolved_settings)
            dataset = datasets.register(
                principal=principal,
                name=registration.name.strip(),
                content_hash=registration.content_hash,
                schema_version=registration.schema_version,
                source_uri=registration.source_uri,
                metadata=dict(registration.metadata),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        return _dataset_document(dataset)

    @app.get("/v1/datasets")
    def list_datasets(
        principal: Principal = Depends(current_principal),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        return {
            "items": [
                _dataset_document(dataset)
                for dataset in datasets.list(
                    tenant_id=principal.tenant_id,
                    limit=limit,
                    offset=offset,
                )
            ],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/datasets/{dataset_id}")
    def get_dataset(
        dataset_id: str,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        dataset = datasets.get(dataset_id, tenant_id=principal.tenant_id)
        if dataset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _dataset_document(dataset)

    @app.get("/v1/jobs")
    def list_jobs(
        principal: Principal = Depends(current_principal),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        return {
            "items": [
                _job_document(job)
                for job in jobs.list(
                    tenant_id=principal.tenant_id,
                    limit=limit,
                    offset=offset,
                )
            ],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/jobs/{job_id}")
    def get_job(
        job_id: str,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        job = jobs.get(job_id, tenant_id=principal.tenant_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _job_document(job, include_payload=True)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(
        job_id: str,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "researcher")
        job = jobs.request_cancel(job_id, principal=principal)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _job_document(job)

    @app.get("/v1/runs")
    def list_runs(
        principal: Principal = Depends(current_principal),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        return {
            "items": [
                _run_document(run)
                for run in database.list_runs(
                    tenant_id=principal.tenant_id,
                    limit=limit,
                    offset=offset,
                )
            ],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/runs/{run_id}")
    def get_run(
        run_id: str,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        run = database.get_run(run_id, tenant_id=principal.tenant_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _run_document(run)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def console() -> str:
        return _CONSOLE_HTML

    instrument_app(app, service_name="quant-backtester-api")
    return app


def _require_role(principal: Principal, role: str) -> None:
    if not principal.permits(role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role {role!r} is required",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-backtester-api")
    parser.add_argument("--host", default=os.getenv("QBT_API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QBT_API_PORT", "8000")))
    parser.add_argument("--log-level", default=os.getenv("QBT_LOG_LEVEL", "INFO"))
    parser.add_argument("--json-logs", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(level=args.log_level, json_logs=args.json_logs)
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        proxy_headers=False,
        server_header=False,
    )


_CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quant Backtester</title>
  <style>
    body { font: 16px system-ui; max-width: 960px; margin: 3rem auto; padding: 0 1rem;
           color: #17202a; background: #f7f9fb; }
    textarea, input { box-sizing: border-box; width: 100%; padding: .7rem; margin: .3rem 0 1rem; }
    textarea { min-height: 18rem; font-family: ui-monospace, monospace; }
    button { padding: .7rem 1rem; cursor: pointer; }
    pre { background: #17202a; color: #ecf0f1; padding: 1rem; overflow: auto; }
  </style>
</head>
<body>
  <h1>Quant Backtester</h1>
  <p>Submit a versioned backtest job. The bearer token remains in this browser tab.</p>
  <label>Bearer token<input id="token" type="password" autocomplete="off"></label>
  <label>Idempotency key<input id="key"></label>
  <label>Configuration</label>
  <textarea id="config">{"symbols":["AAPL","MSFT"],"csv_path":"data/sample_prices.csv",
"short_window":5,"long_window":10,"run_name":"console"}</textarea>
  <button id="submit">Submit</button>
  <pre id="result">Ready.</pre>
  <script>
    document.querySelector("#submit").onclick = async () => {
      const output = document.querySelector("#result");
      try {
        const response = await fetch("/v1/jobs", {
          method: "POST",
          headers: {"Authorization": "Bearer " + document.querySelector("#token").value,
                    "Idempotency-Key": document.querySelector("#key").value,
                    "Content-Type": "application/json"},
          body: JSON.stringify({config: JSON.parse(document.querySelector("#config").value)})
        });
        output.textContent = JSON.stringify(await response.json(), null, 2);
      } catch (error) { output.textContent = String(error); }
    };
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
