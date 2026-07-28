from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import socket
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from opentelemetry import trace

from quant_backtester.backtest import BacktestCancelled, run_backtest
from quant_backtester.config import backtest_config_from_mapping, database_url_from_env
from quant_backtester.logging_context import log_context
from quant_backtester.logging_utils import configure_logging
from quant_backtester.observability import (
    INVARIANT_FAILURES,
    JOB_DURATION,
    JOBS,
    configure_telemetry,
)
from quant_backtester.persistence.db import Database
from quant_backtester.results import S3ArtifactStore
from quant_backtester.service.jobs import JobConflictError, JobRepository
from quant_backtester.service.queue import SQSJobNotifier
from quant_backtester.service.settings import Environment, WorkerSettings, env_bool

logger = logging.getLogger(__name__)


@dataclass
class LeaseGuard:
    repository: JobRepository
    job_id: str
    worker_id: str
    lease_seconds: int
    _last_heartbeat: float = 0.0

    def cancellation_requested(self) -> bool:
        now = time.monotonic()
        if now - self._last_heartbeat < max(1.0, self.lease_seconds / 3):
            return False
        self._last_heartbeat = now
        return not self.repository.heartbeat(
            self.job_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )


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

    def run_once(
        self,
        *,
        preferred_job_id: str | None = None,
        preferred_tenant_id: str | None = None,
    ) -> bool:
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
            extra={"event": {"job_id": job.id, "attempt": job.attempts}},
        )
        started = time.perf_counter()
        try:
            with log_context(tenant_id=job.tenant_id, job_id=job.id, run_id=job.id):
                existing = self.database.get_run(job.id, tenant_id=job.tenant_id)
                if existing is not None:
                    self.jobs.complete(
                        job.id,
                        worker_id=self.settings.worker_id,
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
                with self._materialized_data_path(
                    config.csv_path,
                    expected_sha256=config.dataset_sha256,
                ) as csv_path:
                    config = replace(config, csv_path=str(csv_path))
                    guard = LeaseGuard(
                        repository=self.jobs,
                        job_id=job.id,
                        worker_id=self.settings.worker_id,
                        lease_seconds=self.settings.lease_seconds,
                    )
                    artifact_store = (
                        S3ArtifactStore(
                            bucket=self.settings.artifact_bucket,
                            prefix=self.settings.artifact_prefix,
                            kms_key_id=self.settings.artifact_kms_key_id,
                            endpoint_url=self.settings.s3_endpoint_url,
                        )
                        if self.settings.artifact_bucket
                        else None
                    )
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
                            cancel_requested=guard.cancellation_requested,
                            tenant_id=job.tenant_id,
                            artifact_store=artifact_store,
                            persistence=self.database,
                        )
                self.jobs.complete(
                    job.id,
                    worker_id=self.settings.worker_id,
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
        except BacktestCancelled:
            try:
                self.jobs.cancel_running(job.id, worker_id=self.settings.worker_id)
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
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "reconcile" in str(exc).lower():
                INVARIANT_FAILURES.labels(invariant="portfolio_reconciliation").inc()
                logger.error(
                    "Portfolio reconciliation invariant failed",
                    extra={"event": {"invariant": "portfolio_reconciliation"}},
                )
            try:
                status = self.jobs.fail(
                    job.id,
                    worker_id=self.settings.worker_id,
                    error=f"{type(exc).__name__}: {exc}",
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
        return True

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
            with self._download_s3_dataset(configured) as downloaded:
                self._verify_dataset_hash(downloaded, expected_sha256)
                yield downloaded
            return
        local_path = self._validated_local_data_path(configured)
        self._verify_dataset_hash(local_path, expected_sha256)
        yield local_path

    @staticmethod
    def _verify_dataset_hash(path: Path, expected_sha256: str | None) -> None:
        if expected_sha256 is None:
            return
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
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
    def _download_s3_dataset(self, uri: str) -> Iterator[Path]:
        import boto3

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
        )
        object_reference: dict[str, str] = {"Bucket": bucket, "Key": key}
        if version_id:
            object_reference["VersionId"] = version_id
        metadata = client.head_object(**object_reference)
        if int(metadata["ContentLength"]) > self.settings.max_dataset_bytes:
            raise ValueError("Dataset exceeds QBT_MAX_DATASET_BYTES")
        with tempfile.TemporaryDirectory(prefix="qbt-data-") as directory:
            filename = hashlib.sha256(uri.encode("utf-8")).hexdigest() + ".csv"
            destination = Path(directory) / filename
            extra_args = {"VersionId": version_id} if version_id else None
            client.download_file(
                bucket,
                key,
                str(destination),
                ExtraArgs=extra_args,
            )
            if destination.stat().st_size != int(metadata["ContentLength"]):
                raise RuntimeError("Downloaded dataset size does not match S3 metadata")
            yield destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-backtester-worker")
    parser.add_argument("--once", action="store_true")
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
        artifact_bucket=os.getenv("QBT_ARTIFACT_BUCKET"),
        artifact_prefix=os.getenv("QBT_ARTIFACT_PREFIX", "runs"),
        artifact_kms_key_id=os.getenv("QBT_ARTIFACT_KMS_KEY_ID"),
        s3_endpoint_url=os.getenv("QBT_S3_ENDPOINT_URL"),
        queue_url=os.getenv("QBT_QUEUE_URL"),
        dataset_bucket=os.getenv("QBT_DATASET_BUCKET"),
        dataset_prefix=os.getenv("QBT_DATASET_PREFIX", "datasets/"),
        allow_local_datasets=env_bool(
            "QBT_ALLOW_LOCAL_DATASETS",
            environment != "production",
        ),
        require_versioned_datasets=env_bool(
            "QBT_REQUIRE_VERSIONED_DATASETS",
            environment == "production",
        ),
        require_dataset_hash=env_bool(
            "QBT_REQUIRE_DATASET_HASH",
            environment == "production",
        ),
        require_sorted_data=env_bool(
            "QBT_REQUIRE_SORTED_DATA",
            environment == "production",
        ),
        aws_endpoint_url=os.getenv("QBT_AWS_ENDPOINT_URL"),
    )


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(level=args.log_level, json_logs=args.json_logs)
    configure_telemetry(service_name="quant-backtester-worker")
    worker = Worker(settings_from_env())
    signal.signal(signal.SIGTERM, lambda *_args: worker.stop())
    signal.signal(signal.SIGINT, lambda *_args: worker.stop())
    if args.once:
        worker.run_once()
    else:
        worker.serve()


if __name__ == "__main__":
    main()
