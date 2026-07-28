from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from opentelemetry import trace

from quant_backtester.artifact_publication import (
    ArtifactPublicationError,
    publish_pending_artifacts,
)
from quant_backtester.backtest import BacktestCancelled, run_backtest
from quant_backtester.config import (
    backtest_config_from_mapping,
    database_url_from_env,
    enforce_compute_limits,
)
from quant_backtester.logging_context import log_context
from quant_backtester.logging_utils import configure_logging
from quant_backtester.observability import (
    INVARIANT_FAILURES,
    JOB_DURATION,
    JOBS,
    configure_telemetry,
    shutdown_telemetry,
)
from quant_backtester.persistence.db import Database
from quant_backtester.results import ArtifactStore, LocalArtifactStore, S3ArtifactStore
from quant_backtester.service.jobs import (
    FailureClass,
    HeartbeatOutcome,
    JobConflictError,
    JobRepository,
)
from quant_backtester.service.queue import SQSJobNotifier
from quant_backtester.service.settings import (
    PRODUCTION_ENVIRONMENTS,
    Environment,
    WorkerSettings,
    env_bool,
)

logger = logging.getLogger(__name__)


class ComputeLimitExceeded(ValueError):
    pass


def _classify_failure(exc: Exception) -> tuple[FailureClass, str]:
    if isinstance(exc, ComputeLimitExceeded):
        return FailureClass.PERMANENT, "COMPUTE_LIMIT_EXCEEDED"
    if isinstance(exc, RuntimeError) and "reconcile" in str(exc).lower():
        return FailureClass.INVARIANT, "PORTFOLIO_INVARIANT_FAILED"
    if isinstance(exc, (ValueError, TypeError, FileNotFoundError, PermissionError)):
        return FailureClass.PERMANENT, "INVALID_JOB_INPUT"
    return FailureClass.TRANSIENT, "TRANSIENT_WORKER_FAILURE"


@dataclass
class LeaseGuard:
    repository: JobRepository
    job_id: str
    worker_id: str
    lease_generation: int
    lease_seconds: int
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _outcome: HeartbeatOutcome = field(
        default=HeartbeatOutcome.ACTIVE,
        init=False,
        repr=False,
    )
    _last_success: float = field(default_factory=time.monotonic, init=False, repr=False)

    @property
    def outcome(self) -> HeartbeatOutcome:
        with self._lock:
            return self._outcome

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Lease guard is already running")
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"qbt-lease-{self.job_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.lease_seconds / 3 + 1.0))
            self._thread = None

    def cancellation_requested(self) -> bool:
        return self.outcome != HeartbeatOutcome.ACTIVE

    def ensure_active(self) -> None:
        outcome = self.outcome
        if outcome == HeartbeatOutcome.CANCEL_REQUESTED:
            raise BacktestCancelled(f"Backtest {self.job_id} was canceled")
        if outcome == HeartbeatOutcome.LEASE_LOST:
            raise JobConflictError("Worker no longer owns this job lease")

    def _set_outcome(self, outcome: HeartbeatOutcome) -> None:
        with self._lock:
            self._outcome = outcome

    def _heartbeat_loop(self) -> None:
        interval = max(0.25, self.lease_seconds / 3)
        while not self._stop_event.wait(interval):
            try:
                outcome = self.repository.heartbeat(
                    self.job_id,
                    worker_id=self.worker_id,
                    lease_generation=self.lease_generation,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                logger.exception(
                    "Job lease heartbeat failed",
                    extra={"event": {"job_id": self.job_id}},
                )
                if time.monotonic() - self._last_success >= max(1.0, self.lease_seconds * 0.8):
                    self._set_outcome(HeartbeatOutcome.LEASE_LOST)
                    return
                continue
            if outcome != HeartbeatOutcome.ACTIVE:
                self._set_outcome(outcome)
                return
            self._last_success = time.monotonic()


class Worker:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.database = Database(settings.database_url)
        self.database.prepare_schema()
        self.jobs = JobRepository(self.database)
        self.notifier = (
            SQSJobNotifier(settings.queue_url, endpoint_url=settings.aws_endpoint_url)
            if settings.queue_url
            else None
        )
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
        self.database.close()

    def run_once(
        self,
        *,
        preferred_job_id: str | None = None,
        preferred_tenant_id: str | None = None,
    ) -> bool:
        pending_publication = self.database.next_due_artifact_publication()
        if pending_publication is not None:
            try:
                publish_pending_artifacts(
                    self.database,
                    self._artifact_store(),
                    run_id=pending_publication.run_id,
                    tenant_id=pending_publication.tenant_id,
                )
                self.jobs.reconcile_published_result(
                    pending_publication.run_id,
                    tenant_id=pending_publication.tenant_id,
                )
                logger.info(
                    "Recovered pending artifact publication",
                    extra={
                        "event": {
                            "run_id": pending_publication.run_id,
                            "outcome": "artifact_recovered",
                        }
                    },
                )
            except ArtifactPublicationError:
                logger.exception(
                    "Pending artifact publication remains unavailable",
                    extra={
                        "event": {
                            "run_id": pending_publication.run_id,
                            "outcome": "artifact_deferred",
                        }
                    },
                )
            return True

        job = self.jobs.claim(
            worker_id=self.settings.worker_id,
            lease_seconds=self.settings.lease_seconds,
            job_id=preferred_job_id,
            tenant_id=preferred_tenant_id,
        )
        if job is None:
            return False
        logger.info(
            "Job claimed",
            extra={
                "event": {
                    "job_id": job.id,
                    "attempt": job.attempts,
                    "lease_generation": job.lease_generation,
                }
            },
        )
        started = time.perf_counter()
        guard = LeaseGuard(
            repository=self.jobs,
            job_id=job.id,
            worker_id=self.settings.worker_id,
            lease_generation=job.lease_generation,
            lease_seconds=self.settings.lease_seconds,
        )
        guard.start()
        deadline = started + self.settings.max_run_seconds
        deadline_exceeded = threading.Event()

        def computation_cancelled() -> bool:
            if self._stopping or guard.cancellation_requested():
                return True
            if time.monotonic() >= deadline:
                deadline_exceeded.set()
                return True
            return False

        try:
            with log_context(tenant_id=job.tenant_id, job_id=job.id, run_id=job.id):
                guard.ensure_active()
                existing = self.database.get_run(job.id, tenant_id=job.tenant_id)
                if existing is not None:
                    publish_pending_artifacts(
                        self.database,
                        self._artifact_store(),
                        run_id=existing.run_id,
                        tenant_id=job.tenant_id,
                        ensure_active=guard.ensure_active,
                    )
                    guard.ensure_active()
                    self.jobs.complete(
                        job.id,
                        worker_id=self.settings.worker_id,
                        lease_generation=job.lease_generation,
                        result_run_id=existing.run_id,
                    )
                    JOBS.labels(outcome="succeeded").inc()
                    JOB_DURATION.labels(outcome="succeeded").observe(time.perf_counter() - started)
                    logger.info(
                        "Recovered already-persisted job result",
                        extra={
                            "event": {
                                "job_id": job.id,
                                "run_id": existing.run_id,
                                "outcome": "succeeded",
                            }
                        },
                    )
                    return True
                config = backtest_config_from_mapping(
                    job.payload,
                    database_url=self.settings.database_url,
                    out_dir=str(self.settings.artifact_root),
                )
                if self.settings.require_sorted_data and not config.data.require_sorted:
                    raise ValueError("Unsorted market data is disabled for this worker")
                enforce_compute_limits(
                    config,
                    max_symbols=self.settings.max_symbols_per_run,
                    max_events=self.settings.max_events_per_run,
                    max_unsorted_events=self.settings.max_unsorted_events_per_run,
                    max_equity_points=self.settings.max_persisted_equity_points,
                    require_strict_schema=self.settings.require_strict_dataset_schema,
                )
                guard.ensure_active()
                with self._materialized_data_path(
                    config.csv_path,
                    expected_sha256=config.dataset_sha256,
                ) as csv_path:
                    guard.ensure_active()
                    if time.monotonic() >= deadline:
                        raise ComputeLimitExceeded(
                            f"Run exceeded max_run_seconds={self.settings.max_run_seconds:g}"
                        )
                    config = replace(config, csv_path=str(csv_path))
                    artifact_store = self._artifact_store()
                    tracer = trace.get_tracer("quant_backtester.worker")
                    with tracer.start_as_current_span(
                        "backtest.run",
                        attributes={
                            "qbt.job.id": job.id,
                            "qbt.tenant.id": job.tenant_id,
                            "qbt.attempt": job.attempts,
                        },
                    ):
                        result = run_backtest(
                            config,
                            persist=True,
                            run_id=job.id,
                            write_artifact=True,
                            cancel_requested=computation_cancelled,
                            tenant_id=job.tenant_id,
                            artifact_store=artifact_store,
                            persistence=self.database,
                        )
                guard.ensure_active()
                self.jobs.complete(
                    job.id,
                    worker_id=self.settings.worker_id,
                    lease_generation=job.lease_generation,
                    result_run_id=str(result["run_id"]),
                )
                logger.info(
                    "Job completed",
                    extra={
                        "event": {
                            "job_id": job.id,
                            "run_id": result["run_id"],
                            "outcome": "succeeded",
                        }
                    },
                )
            JOBS.labels(outcome="succeeded").inc()
            JOB_DURATION.labels(outcome="succeeded").observe(time.perf_counter() - started)
        except ArtifactPublicationError:
            try:
                self.jobs.wait_for_artifact(
                    job.id,
                    worker_id=self.settings.worker_id,
                    lease_generation=job.lease_generation,
                    result_run_id=job.id,
                )
            except JobConflictError:
                logger.warning(
                    "Artifact-deferred computation no longer owns its lease",
                    extra={"event": {"job_id": job.id}},
                )
            JOBS.labels(outcome="artifact_deferred").inc()
            JOB_DURATION.labels(outcome="artifact_deferred").observe(time.perf_counter() - started)
            logger.exception(
                "Job result persisted; artifact publication deferred",
                extra={"event": {"job_id": job.id, "outcome": "artifact_deferred"}},
            )
        except BacktestCancelled:
            if self._stopping:
                try:
                    self.jobs.release(
                        job.id,
                        worker_id=self.settings.worker_id,
                        lease_generation=job.lease_generation,
                    )
                except JobConflictError:
                    logger.warning(
                        "Shutting-down computation no longer owns its lease",
                        extra={"event": {"job_id": job.id}},
                    )
                JOBS.labels(outcome="shutdown").inc()
                JOB_DURATION.labels(outcome="shutdown").observe(time.perf_counter() - started)
                logger.info(
                    "Job lease released during worker shutdown",
                    extra={"event": {"job_id": job.id, "outcome": "shutdown"}},
                )
            elif deadline_exceeded.is_set():
                try:
                    self.jobs.fail(
                        job.id,
                        worker_id=self.settings.worker_id,
                        lease_generation=job.lease_generation,
                        error=(
                            "ComputeLimitExceeded: run exceeded "
                            f"max_run_seconds={self.settings.max_run_seconds:g}"
                        ),
                        failure_class=FailureClass.PERMANENT,
                        public_error_code="COMPUTE_LIMIT_EXCEEDED",
                    )
                except JobConflictError:
                    logger.warning(
                        "Timed-out computation no longer owns its lease",
                        extra={"event": {"job_id": job.id}},
                    )
                JOBS.labels(outcome="resource_limit").inc()
                JOB_DURATION.labels(outcome="resource_limit").observe(time.perf_counter() - started)
                logger.warning(
                    "Job exceeded its compute deadline",
                    extra={"event": {"job_id": job.id, "outcome": "resource_limit"}},
                )
            elif guard.outcome == HeartbeatOutcome.CANCEL_REQUESTED:
                try:
                    self.jobs.cancel_running(
                        job.id,
                        worker_id=self.settings.worker_id,
                        lease_generation=job.lease_generation,
                    )
                except JobConflictError:
                    logger.warning(
                        "Canceled computation no longer owns its lease",
                        extra={"event": {"job_id": job.id}},
                    )
                JOBS.labels(outcome="canceled").inc()
                JOB_DURATION.labels(outcome="canceled").observe(time.perf_counter() - started)
                logger.info(
                    "Job canceled",
                    extra={"event": {"job_id": job.id, "outcome": "canceled"}},
                )
            else:
                JOBS.labels(outcome="lease_lost").inc()
                JOB_DURATION.labels(outcome="lease_lost").observe(time.perf_counter() - started)
                logger.warning(
                    "Computation stopped after losing its lease",
                    extra={"event": {"job_id": job.id, "outcome": "lease_lost"}},
                )
        except Exception as exc:
            failure_class, public_error_code = _classify_failure(exc)
            if failure_class == FailureClass.INVARIANT:
                INVARIANT_FAILURES.labels(invariant="portfolio_reconciliation").inc()
                logger.error(
                    "Portfolio reconciliation invariant failed",
                    extra={"event": {"invariant": "portfolio_reconciliation"}},
                )
            try:
                status = self.jobs.fail(
                    job.id,
                    worker_id=self.settings.worker_id,
                    lease_generation=job.lease_generation,
                    error=f"{type(exc).__name__}: {exc}",
                    failure_class=failure_class,
                    public_error_code=public_error_code,
                )
            except JobConflictError:
                JOBS.labels(outcome="lease_lost").inc()
                JOB_DURATION.labels(outcome="lease_lost").observe(time.perf_counter() - started)
                logger.exception(
                    "Job attempt failed after its lease changed ownership",
                    extra={"event": {"job_id": job.id}},
                )
                return True
            logger.exception(
                "Job attempt failed",
                extra={
                    "event": {
                        "job_id": job.id,
                        "next_status": status.value,
                        "outcome": "failed",
                    }
                },
            )
            JOBS.labels(outcome="failed").inc()
            JOB_DURATION.labels(outcome="failed").observe(time.perf_counter() - started)
        finally:
            guard.stop()
        return True

    def _artifact_store(self) -> ArtifactStore:
        if self.settings.artifact_bucket:
            return S3ArtifactStore(
                bucket=self.settings.artifact_bucket,
                prefix=self.settings.artifact_prefix,
                kms_key_id=self.settings.artifact_kms_key_id,
                endpoint_url=self.settings.s3_endpoint_url,
            )
        return LocalArtifactStore(self.settings.artifact_root)

    def serve(self) -> None:
        while not self._stopping:
            receipt = None
            if self.notifier is not None:
                try:
                    receipt = self.notifier.receive()
                except Exception:
                    logger.exception("Job wake-up receive failed; falling back to database polling")
            if receipt is not None and self.notifier is not None:
                try:
                    self.notifier.acknowledge(receipt)
                except Exception:
                    logger.exception("Job wake-up acknowledgement failed")
            processed = self.run_once(
                preferred_job_id=receipt.job_id if receipt is not None else None,
                preferred_tenant_id=receipt.tenant_id if receipt is not None else None,
            )
            if not processed and receipt is not None:
                # A stale or concurrently claimed wake-up must not prevent this
                # worker from helping with another durable queued row.
                processed = self.run_once()
            if not processed:
                time.sleep(self.settings.poll_seconds)

    @contextmanager
    def _materialized_data_path(
        self,
        configured: str,
        *,
        expected_sha256: str | None,
    ) -> Iterator[Path]:
        if self.settings.require_dataset_hash and expected_sha256 is None:
            raise ValueError("dataset_sha256 is required for this worker")
        if configured.startswith("s3://"):
            with self._download_s3_dataset(
                configured,
                expected_sha256=expected_sha256,
            ) as downloaded:
                yield downloaded
            return
        local_path = self._validated_local_data_path(configured)
        with tempfile.TemporaryDirectory(prefix="qbt-data-") as directory:
            destination = Path(directory) / "dataset.csv"
            self._copy_and_verify_dataset(
                local_path,
                destination,
                expected_sha256=expected_sha256,
            )
            destination.chmod(0o400)
            yield destination

    def _copy_and_verify_dataset(
        self,
        source_path: Path,
        destination: Path,
        *,
        expected_sha256: str | None,
    ) -> None:
        digest = hashlib.sha256()
        total_bytes = 0
        with source_path.open("rb") as source, destination.open("xb") as target:
            while chunk := source.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > self.settings.max_dataset_bytes:
                    raise ValueError("Dataset exceeds QBT_MAX_DATASET_BYTES")
                digest.update(chunk)
                target.write(chunk)
        self._validate_materialized_dataset(
            total_bytes=total_bytes,
            digest=digest.hexdigest(),
            expected_sha256=expected_sha256,
        )

    @staticmethod
    def _validate_materialized_dataset(
        *,
        total_bytes: int,
        digest: str,
        expected_sha256: str | None,
    ) -> None:
        if total_bytes == 0:
            raise ValueError("Dataset must not be empty")
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("Dataset SHA-256 does not match the registered digest")

    def _validated_local_data_path(self, configured: str) -> Path:
        if not self.settings.allow_local_datasets:
            raise ValueError("Local datasets are disabled for this worker")
        raw_path = Path(configured)
        candidate = (
            raw_path.resolve()
            if raw_path.is_absolute()
            else (self.settings.data_root / raw_path).resolve()
        )
        try:
            candidate.relative_to(self.settings.data_root)
        except ValueError as exc:
            raise ValueError("csv_path must remain within QBT_DATA_ROOT") from exc
        if not candidate.is_file():
            raise ValueError(f"Dataset does not exist: {candidate}")
        if candidate.stat().st_size > self.settings.max_dataset_bytes:
            raise ValueError("Dataset exceeds QBT_MAX_DATASET_BYTES")
        return candidate

    @contextmanager
    def _download_s3_dataset(
        self,
        uri: str,
        *,
        expected_sha256: str | None,
    ) -> Iterator[Path]:
        import boto3
        from botocore.config import Config

        parsed = urlsplit(uri)
        bucket = parsed.netloc
        key = unquote(parsed.path.lstrip("/"))
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if set(query) - {"versionId"} or len(query.get("versionId", [])) > 1:
            raise ValueError("S3 dataset URI only supports one versionId parameter")
        version_id = next(iter(query.get("versionId", [])), None)
        if parsed.fragment:
            raise ValueError("S3 dataset URI must not contain a fragment")
        if self.settings.require_versioned_datasets and not version_id:
            raise ValueError("S3 dataset URI must include versionId")
        if not self.settings.dataset_bucket or bucket != self.settings.dataset_bucket:
            raise ValueError("S3 dataset bucket is not authorized")
        prefix = self.settings.dataset_prefix.strip("/") + "/"
        if not key.startswith(prefix):
            raise ValueError("S3 dataset key is outside the authorized prefix")
        client = boto3.client(
            "s3",
            endpoint_url=self.settings.aws_endpoint_url or self.settings.s3_endpoint_url,
            config=Config(
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        object_reference: dict[str, str] = {"Bucket": bucket, "Key": key}
        if version_id:
            object_reference["VersionId"] = version_id
        metadata = client.head_object(**object_reference)
        content_length = int(metadata["ContentLength"])
        if content_length <= 0:
            raise ValueError("Dataset must not be empty")
        if content_length > self.settings.max_dataset_bytes:
            raise ValueError("Dataset exceeds QBT_MAX_DATASET_BYTES")
        with tempfile.TemporaryDirectory(prefix="qbt-data-") as directory:
            filename = hashlib.sha256(uri.encode("utf-8")).hexdigest() + ".csv"
            destination = Path(directory) / filename
            response = client.get_object(**object_reference)
            body = response["Body"]
            digest = hashlib.sha256()
            total_bytes = 0
            try:
                with destination.open("xb") as target:
                    for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > self.settings.max_dataset_bytes:
                            raise ValueError("Dataset exceeds QBT_MAX_DATASET_BYTES")
                        digest.update(chunk)
                        target.write(chunk)
            finally:
                body.close()
            if total_bytes != content_length:
                raise RuntimeError("Downloaded dataset size does not match S3 metadata")
            self._validate_materialized_dataset(
                total_bytes=total_bytes,
                digest=digest.hexdigest(),
                expected_sha256=expected_sha256,
            )
            destination.chmod(0o400)
            yield destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-backtester-worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Validate the schema and database connection, then exit.",
    )
    parser.add_argument("--log-level", default=os.getenv("QBT_LOG_LEVEL", "INFO"))
    parser.add_argument("--json-logs", action="store_true")
    return parser


def settings_from_env() -> WorkerSettings:
    environment = cast(Environment, os.getenv("QBT_ENVIRONMENT", "development"))
    return WorkerSettings(
        database_url=database_url_from_env(),
        data_root=Path(os.getenv("QBT_DATA_ROOT", ".")).resolve(),
        artifact_root=Path(os.getenv("QBT_ARTIFACT_ROOT", "runs")).resolve(),
        worker_id=os.getenv(
            "QBT_WORKER_ID",
            f"{socket.gethostname()}-{os.getpid()}",
        ),
        environment=environment,
        poll_seconds=float(os.getenv("QBT_WORKER_POLL_SECONDS", "1")),
        lease_seconds=int(os.getenv("QBT_JOB_LEASE_SECONDS", "30")),
        max_dataset_bytes=int(os.getenv("QBT_MAX_DATASET_BYTES", "250000000")),
        max_symbols_per_run=int(os.getenv("QBT_MAX_SYMBOLS_PER_RUN", "250")),
        max_events_per_run=int(os.getenv("QBT_MAX_EVENTS_PER_RUN", "2000000")),
        max_unsorted_events_per_run=int(os.getenv("QBT_MAX_UNSORTED_EVENTS_PER_RUN", "250000")),
        max_persisted_equity_points=int(os.getenv("QBT_MAX_PERSISTED_EQUITY_POINTS", "100000")),
        max_run_seconds=float(os.getenv("QBT_MAX_RUN_SECONDS", "3600")),
        artifact_bucket=os.getenv("QBT_ARTIFACT_BUCKET"),
        artifact_prefix=os.getenv("QBT_ARTIFACT_PREFIX", "runs"),
        artifact_kms_key_id=os.getenv("QBT_ARTIFACT_KMS_KEY_ID"),
        s3_endpoint_url=os.getenv("QBT_S3_ENDPOINT_URL"),
        queue_url=os.getenv("QBT_QUEUE_URL"),
        dataset_bucket=os.getenv("QBT_DATASET_BUCKET"),
        dataset_prefix=os.getenv("QBT_DATASET_PREFIX", "datasets/"),
        allow_local_datasets=env_bool(
            "QBT_ALLOW_LOCAL_DATASETS",
            environment not in PRODUCTION_ENVIRONMENTS,
        ),
        require_versioned_datasets=env_bool(
            "QBT_REQUIRE_VERSIONED_DATASETS",
            environment in PRODUCTION_ENVIRONMENTS,
        ),
        require_dataset_hash=env_bool(
            "QBT_REQUIRE_DATASET_HASH",
            environment in PRODUCTION_ENVIRONMENTS,
        ),
        require_sorted_data=env_bool(
            "QBT_REQUIRE_SORTED_DATA",
            environment in PRODUCTION_ENVIRONMENTS,
        ),
        require_strict_dataset_schema=env_bool(
            "QBT_REQUIRE_STRICT_DATASET_SCHEMA",
            True,
        ),
        aws_endpoint_url=os.getenv("QBT_AWS_ENDPOINT_URL"),
    )


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(level=args.log_level, json_logs=args.json_logs)
    settings = settings_from_env()
    if args.healthcheck:
        database = Database(settings.database_url)
        try:
            database.prepare_schema()
            if not database.ping():
                raise SystemExit("Worker database health check failed")
        finally:
            database.close()
        return
    configure_telemetry(service_name="quant-backtester-worker")
    worker = Worker(settings)
    signal.signal(signal.SIGTERM, lambda *_args: worker.stop())
    signal.signal(signal.SIGINT, lambda *_args: worker.stop())
    try:
        if args.once:
            worker.run_once()
        else:
            worker.serve()
    finally:
        worker.close()
        shutdown_telemetry()


if __name__ == "__main__":
    main()
