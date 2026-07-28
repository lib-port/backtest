from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal, ParamSpec, TypeVar, cast
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from quant_backtester import __version__
from quant_backtester.config import backtest_config_from_mapping, enforce_compute_limits
from quant_backtester.events import normalize_symbol
from quant_backtester.logging_context import log_context, tenant_id_var
from quant_backtester.logging_utils import configure_logging
from quant_backtester.observability import (
    HTTP_DURATION,
    HTTP_REQUESTS,
    instrument_app,
    shutdown_telemetry,
)
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
from quant_backtester.service.settings import PRODUCTION_ENVIRONMENTS, ServiceSettings

logger = logging.getLogger(__name__)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
PUBLIC_JOB_ERROR_MESSAGES = {
    "WORKER_LEASE_EXPIRED": "The worker stopped before the attempt completed.",
    "INVALID_JOB_INPUT": "The job configuration or dataset is invalid.",
    "PORTFOLIO_INVARIANT_FAILED": "The simulation failed an accounting invariant.",
    "TRANSIENT_WORKER_FAILURE": "The worker encountered a temporary failure.",
    "COMPUTE_LIMIT_EXCEEDED": "The run exceeded a configured compute limit.",
    "JOB_ATTEMPT_FAILED": "The job attempt failed.",
}
P = ParamSpec("P")
T = TypeVar("T")


class _RequestBodyTooLarge(ValueError):
    pass


class OperationalMiddleware:
    """Enforce streaming body limits and attach operational response headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        production: bool,
    ) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.production = production

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        raw_request_id = headers.get("x-request-id", "")
        request_id = (
            raw_request_id if REQUEST_ID_PATTERN.fullmatch(raw_request_id) else str(uuid.uuid4())
        )
        started = time.perf_counter()
        received_bytes = 0
        response_started = False
        response_status = status.HTTP_500_INTERNAL_SERVER_ERROR

        async def bounded_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_request_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def operational_send(message: Message) -> None:
            nonlocal response_started, response_status
            if message["type"] == "http.response.start":
                response_started = True
                response_status = int(message["status"])
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers["Cache-Control"] = "no-store"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                if self.production:
                    response_headers["Content-Security-Policy"] = (
                        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                    )
                    response_headers["Strict-Transport-Security"] = (
                        "max-age=63072000; includeSubDomains"
                    )
                else:
                    response_headers["Content-Security-Policy"] = (
                        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
                    )
            await send(message)

        try:
            with log_context(request_id=request_id, tenant_id="-"):
                content_length = headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        response_status = status.HTTP_400_BAD_REQUEST
                        await Response(status_code=response_status)(
                            scope,
                            receive,
                            operational_send,
                        )
                        return
                    if declared_size < 0:
                        response_status = status.HTTP_400_BAD_REQUEST
                        await Response(status_code=response_status)(
                            scope,
                            receive,
                            operational_send,
                        )
                        return
                    if declared_size > self.max_request_bytes:
                        response_status = status.HTTP_413_CONTENT_TOO_LARGE
                        await Response(status_code=response_status)(
                            scope,
                            receive,
                            operational_send,
                        )
                        return
                await self.app(scope, bounded_receive, operational_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            response_status = status.HTTP_413_CONTENT_TOO_LARGE
            await Response(status_code=response_status)(scope, receive, operational_send)
        finally:
            route = scope.get("route")
            route_path = cast(str, getattr(route, "path", "unmatched"))
            raw_method = str(scope.get("method", "OTHER")).upper()
            method = (
                raw_method
                if raw_method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER"
            )
            HTTP_REQUESTS.labels(
                method=method,
                route=route_path,
                status=str(response_status),
            ).inc()
            HTTP_DURATION.labels(
                method=method,
                route=route_path,
            ).observe(time.perf_counter() - started)


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MicrostructureSubmission(StrictRequestModel):
    latency_events: int = Field(default=1, ge=0, le=1_000_000)
    default_tick_volume: float = Field(default=5_000.0, gt=0, allow_inf_nan=False)
    max_participation_rate: float = Field(default=0.2, ge=0, le=1, allow_inf_nan=False)
    queue_ahead_fraction: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    base_fill_probability: float = Field(default=0.8, ge=0, le=1, allow_inf_nan=False)


class ExecutionSubmission(StrictRequestModel):
    default_spread_bps: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    impact_bps_per_unit: float = Field(default=2.0, ge=0, allow_inf_nan=False)
    impact_volume: float = Field(default=10_000.0, gt=0, allow_inf_nan=False)
    rng_seed: int = Field(default=7, ge=-(2**63), le=2**63 - 1)
    micro: MicrostructureSubmission = Field(default_factory=MicrostructureSubmission)


class RiskSubmission(StrictRequestModel):
    max_position_per_symbol: int = Field(default=1_000, ge=1, le=1_000_000_000)
    stop_loss_pct: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)
    max_drawdown_pct: float = Field(default=0.2, ge=0, le=1, allow_inf_nan=False)
    max_gross_exposure: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_net_exposure: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_order_notional: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_leverage: float = Field(default=1.0, gt=0, le=100, allow_inf_nan=False)
    max_daily_loss_pct: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    allow_short: bool = True


class DataSubmission(StrictRequestModel):
    source_timezone: str = Field(default="UTC", min_length=1, max_length=128)
    duplicate_policy: Literal["error", "last"] = "error"
    chunksize: int = Field(default=50_000, ge=1, le=1_000_000)
    require_sorted: bool = True
    max_events: int = Field(default=2_000_000, ge=1, le=50_000_000)
    strict_schema: bool = True
    require_all_symbols: bool = True


class MetricsSubmission(StrictRequestModel):
    annual_risk_free_rate: float = Field(default=0.0, gt=-1, allow_inf_nan=False)
    periods_per_year: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class EndOfRunSubmission(StrictRequestModel):
    pending_order_policy: Literal["cancel", "retain"] = "cancel"
    position_policy: Literal["mark_to_market", "liquidate", "require_flat"] = "mark_to_market"


class BacktestSubmission(StrictRequestModel):
    symbols: list[str] = Field(min_length=1, max_length=1_000)
    initial_cash: float = Field(default=100_000.0, gt=0, le=1e15, allow_inf_nan=False)
    trade_quantity: int = Field(default=100, ge=1, le=1_000_000_000)
    commission_per_trade: float = Field(default=1.0, ge=0, le=1e9, allow_inf_nan=False)
    short_window: int = Field(default=20, ge=1, le=10_000_000)
    long_window: int = Field(default=50, ge=2, le=10_000_000)
    csv_path: str | None = Field(default=None, min_length=1, max_length=2_048)
    run_name: str = Field(default="run", min_length=1, max_length=128)
    max_persisted_equity_points: int = Field(default=100_000, ge=2, le=10_000_000)
    execution: ExecutionSubmission = Field(default_factory=ExecutionSubmission)
    risk: RiskSubmission = Field(default_factory=RiskSubmission)
    data: DataSubmission = Field(default_factory=DataSubmission)
    metrics: MetricsSubmission = Field(default_factory=MetricsSubmission)
    end_of_run: EndOfRunSubmission = Field(default_factory=EndOfRunSubmission)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = [normalize_symbol(symbol) for symbol in symbols]
        if len(set(normalized)) != len(normalized):
            raise ValueError("symbols must not contain duplicates")
        return normalized

    @field_validator("run_name")
    @classmethod
    def validate_run_name(cls, run_name: str) -> str:
        normalized = run_name.strip()
        if not normalized:
            raise ValueError("run_name must not be blank")
        return normalized


class RunSubmission(StrictRequestModel):
    config: BacktestSubmission
    dataset_id: str | None = Field(default=None, min_length=36, max_length=36)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("dataset_id")
    @classmethod
    def validate_dataset_id(cls, dataset_id: str | None) -> str | None:
        if dataset_id is None:
            return None
        try:
            canonical = str(UUID(dataset_id))
        except ValueError as exc:
            raise ValueError("dataset_id must be a canonical UUID") from exc
        if canonical != dataset_id:
            raise ValueError("dataset_id must be a canonical UUID")
        return canonical


class DatasetRegistration(StrictRequestModel):
    name: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_uri: str = Field(min_length=1, max_length=2_048)
    schema_version: Literal[1] = 1
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("source_uri")
    @classmethod
    def validate_source_uri(cls, source_uri: str) -> str:
        normalized = source_uri.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("source_uri contains invalid characters")
        return normalized

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata_value(metadata)
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 16_384:
            raise ValueError("metadata must not exceed 16384 encoded bytes")
        return metadata


def _validate_metadata_value(value: object, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("metadata nesting exceeds 8 levels")
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("metadata objects must not contain more than 256 keys")
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise ValueError("metadata keys must contain 1 to 128 characters")
            _validate_metadata_value(item, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > 1_000:
            raise ValueError("metadata arrays must not contain more than 1000 items")
        for item in value:
            _validate_metadata_value(item, depth=depth + 1)
        return
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise ValueError("metadata numbers must be finite")
        return
    if isinstance(value, str) and len(value) <= 2_048:
        return
    raise ValueError("metadata contains an unsupported or oversized value")


def _timestamp(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


def _job_document(job: JobSnapshot, *, include_payload: bool = False) -> dict[str, object]:
    public_error = (
        PUBLIC_JOB_ERROR_MESSAGES.get(
            job.public_error_code,
            "The job could not be completed.",
        )
        if job.public_error_code
        else None
    )
    document: dict[str, object] = {
        "id": job.id,
        "status": job.status.value,
        "result_run_id": job.result_run_id,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "cancel_requested": job.cancel_requested,
        "error": public_error,
        "error_code": job.public_error_code,
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
        "annualized_return": run.annualized_return,
        "annualized_volatility": run.annualized_volatility,
        "sharpe": run.sharpe,
        "sortino": run.sortino,
        "calmar": run.calmar,
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


def _validate_dataset_uri(
    uri: str,
    settings: ServiceSettings,
    *,
    expected_sha256: str,
) -> None:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3":
        if settings.environment in PRODUCTION_ENVIRONMENTS:
            raise ValueError("Production datasets must use an s3:// URI")
        if parsed.scheme:
            raise ValueError("Development dataset URIs must be local paths or s3:// URIs")
        raw_path = Path(uri)
        candidate = (
            raw_path.resolve()
            if raw_path.is_absolute()
            else (settings.data_root / raw_path).resolve()
        )
        try:
            candidate.relative_to(settings.data_root)
        except ValueError as exc:
            raise ValueError("Dataset path must remain within QBT_DATA_ROOT") from exc
        if not candidate.is_file():
            raise ValueError("Dataset path does not identify a regular file")
        if not 0 < candidate.stat().st_size <= settings.max_dataset_bytes:
            raise ValueError("Dataset is empty or exceeds QBT_MAX_DATASET_BYTES")
        digest = hashlib.sha256()
        with candidate.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("Dataset content_hash does not match the local file")
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
    executor = ThreadPoolExecutor(
        max_workers=resolved_settings.api_thread_workers,
        thread_name_prefix="qbt-api",
    )

    async def run_blocking(
        function: Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, partial(function, *args, **kwargs))

    async def run_database(
        function: Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        # SQLite is a local/test backend. Keeping its calls on the event-loop
        # thread avoids crossing a connection between threads; PostgreSQL
        # operations use the bounded executor in deployed environments.
        if database.is_sqlite:
            return function(*args, **kwargs)
        return await run_blocking(function, *args, **kwargs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            database.close()
            executor.shutdown(wait=True, cancel_futures=True)
            shutdown_telemetry()

    app = FastAPI(
        title="Quant Backtester Control Plane",
        version=__version__,
        docs_url=(None if resolved_settings.environment in PRODUCTION_ENVIRONMENTS else "/docs"),
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        OperationalMiddleware,
        max_request_bytes=resolved_settings.max_request_bytes,
        production=resolved_settings.environment in PRODUCTION_ENVIRONMENTS,
    )
    app.state.settings = resolved_settings
    app.state.database = database
    app.state.jobs = jobs
    app.state.datasets = datasets

    async def current_principal(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> Principal:
        token = credentials.credentials if credentials is not None else None
        try:
            principal = (
                await run_blocking(authenticator.authenticate, token)
                if resolved_settings.auth_mode == "oidc" and not database.is_sqlite
                else authenticator.authenticate(token)
            )
            tenant_id_var.set(principal.tenant_id)
            return principal
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> dict[str, str]:
        if not await run_database(database.ping):
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
    async def submit_job(
        submission: RunSubmission,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "researcher")
        try:
            config_input = submission.config.model_dump(exclude_none=True)
            if submission.dataset_id is not None:
                if "csv_path" in config_input:
                    raise ValueError("A registered dataset_id cannot be combined with csv_path")
                dataset = await run_database(
                    datasets.get,
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
                config_input["dataset_schema_version"] = dataset.schema_version
                config_input["csv_path"] = dataset.source_uri
            elif resolved_settings.require_registered_datasets:
                raise ValueError("dataset_id is required")
            config = backtest_config_from_mapping(
                config_input,
                database_url=resolved_settings.database_url,
                out_dir=str(resolved_settings.artifact_root),
            )
            enforce_compute_limits(
                config,
                max_symbols=resolved_settings.max_symbols_per_run,
                max_events=resolved_settings.max_events_per_run,
                max_unsorted_events=resolved_settings.max_unsorted_events_per_run,
                max_equity_points=resolved_settings.max_persisted_equity_points,
                require_strict_schema=resolved_settings.require_strict_dataset_schema,
            )
            if submission.max_attempts > resolved_settings.max_attempts_per_job:
                raise ValueError(
                    "max_attempts exceeds the configured per-job limit "
                    f"of {resolved_settings.max_attempts_per_job}"
                )
            payload = config.to_dict(redact_secrets=True)
            payload.pop("database_url", None)
            payload.pop("out_dir", None)
            job = await run_database(
                jobs.submit,
                principal=principal,
                idempotency_key=idempotency_key,
                payload=payload,
                max_attempts=submission.max_attempts,
            )
            if notifier is not None:
                try:
                    await run_blocking(
                        notifier.notify,
                        job_id=job.id,
                        tenant_id=job.tenant_id,
                    )
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
    async def register_dataset(
        registration: DatasetRegistration,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "researcher")
        try:
            validation_arguments = {
                "expected_sha256": registration.content_hash,
            }
            if database.is_sqlite:
                _validate_dataset_uri(
                    registration.source_uri,
                    resolved_settings,
                    **validation_arguments,
                )
            else:
                await run_blocking(
                    _validate_dataset_uri,
                    registration.source_uri,
                    resolved_settings,
                    **validation_arguments,
                )
            dataset = await run_database(
                datasets.register,
                principal=principal,
                name=registration.name,
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
    async def list_datasets(
        principal: Principal = Depends(current_principal),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        _require_role(principal, "observer")
        dataset_rows = await run_database(
            datasets.list,
            tenant_id=principal.tenant_id,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [_dataset_document(dataset) for dataset in dataset_rows],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/datasets/{dataset_id}")
    async def get_dataset(
        dataset_id: str,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "observer")
        dataset = await run_database(
            datasets.get,
            dataset_id,
            tenant_id=principal.tenant_id,
        )
        if dataset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _dataset_document(dataset)

    @app.get("/v1/jobs")
    async def list_jobs(
        principal: Principal = Depends(current_principal),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        _require_role(principal, "observer")
        job_rows = await run_database(
            jobs.list,
            tenant_id=principal.tenant_id,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [_job_document(job) for job in job_rows],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/jobs/{job_id}")
    async def get_job(
        job_id: str,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "observer")
        job = await run_database(jobs.get, job_id, tenant_id=principal.tenant_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _job_document(job, include_payload=True)

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "researcher")
        job = await run_database(jobs.request_cancel, job_id, principal=principal)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _job_document(job)

    @app.get("/v1/runs")
    async def list_runs(
        principal: Principal = Depends(current_principal),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        _require_role(principal, "observer")
        run_rows = await run_database(
            database.list_runs,
            tenant_id=principal.tenant_id,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [_run_document(run) for run in run_rows],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/runs/{run_id}")
    async def get_run(
        run_id: str,
        principal: Principal = Depends(current_principal),  # noqa: B008
    ) -> dict[str, object]:
        _require_role(principal, "observer")
        run = await run_database(
            database.get_run,
            run_id,
            tenant_id=principal.tenant_id,
        )
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _run_document(run)

    if resolved_settings.environment not in PRODUCTION_ENVIRONMENTS:

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
