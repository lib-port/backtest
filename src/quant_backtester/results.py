from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import inspect
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from quant_backtester import __version__
from quant_backtester.config import BacktestConfig


def _json_default(value: object) -> object:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def result_hash_from_mapping(result: dict[str, object]) -> str:
    """Hash the deterministic portion of a serialized backtest result."""
    payload = dict(result)
    for key in ("run_id", "run_name", "created_at", "completed_at", "result_hash"):
        payload.pop(key, None)
    raw_extra = payload.get("extra")
    if isinstance(raw_extra, dict):
        deterministic_context = raw_extra.get("deterministic_context")
        if isinstance(deterministic_context, dict):
            payload["extra"] = {
                "deterministic_context": dict(deterministic_context),
            }
        else:
            # Compatibility for serialized schema-v1 results.
            extra = dict(raw_extra)
            extra.pop("manifest", None)
            effective_config = extra.get("effective_config")
            if isinstance(effective_config, dict):
                semantic_config = dict(effective_config)
                for key in ("run_name", "out_dir", "database_url", "csv_path"):
                    semantic_config.pop(key, None)
                extra["effective_config"] = semantic_config
            payload["extra"] = extra
    return content_hash(payload)


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    run_name: str
    symbols: tuple[str, ...]
    short_window: int
    long_window: int
    initial_cash: float
    final_equity: float
    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    total_commission: float
    total_slippage_cost: float
    realized_pnl: float
    halted: bool
    halt_reason: str | None
    created_at: dt.datetime
    completed_at: dt.datetime
    extra: dict[str, object] = field(default_factory=dict)

    def deterministic_payload(self) -> dict[str, object]:
        payload = dataclasses.asdict(self)
        payload.pop("run_id")
        payload.pop("run_name")
        payload.pop("created_at")
        payload.pop("completed_at")
        deterministic_context = self.extra.get("deterministic_context")
        if not isinstance(deterministic_context, dict):
            raise ValueError("Backtest results require a deterministic_context mapping")
        payload["extra"] = {
            "deterministic_context": dict(deterministic_context),
        }
        return payload

    @property
    def result_hash(self) -> str:
        return content_hash(self.deterministic_payload())

    def to_dict(self) -> dict[str, object]:
        payload = dataclasses.asdict(self)
        payload["symbols"] = list(self.symbols)
        payload["created_at"] = self.created_at.isoformat()
        payload["completed_at"] = self.completed_at.isoformat()
        payload["result_hash"] = self.result_hash
        return payload


@dataclass(frozen=True)
class SourceRevision:
    commit: str
    dirty: bool
    dirty_diff_hash: str | None


@dataclass(frozen=True)
class RunManifest:
    schema_version: int
    run_id: str
    engine_version: str
    engine_semantics_version: str
    config_hash: str
    dataset_hash: str
    raw_dataset_hash: str | None
    warmup_hash: str
    warmup_event_count: int
    strategy: str
    strategy_hash: str
    source: SourceRevision
    python_version: str
    platform: str
    rng_seed: int
    rng_stream_version: str
    image_digest: str | None

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def new_run_id() -> str:
    return str(uuid.uuid4())


def source_revision(repo_root: Path | None = None) -> SourceRevision:
    configured_commit = os.getenv("QBT_SOURCE_REVISION")
    if configured_commit:
        return SourceRevision(
            commit=configured_commit,
            dirty=os.getenv("QBT_SOURCE_DIRTY", "false").lower() == "true",
            dirty_diff_hash=os.getenv("QBT_SOURCE_DIFF_HASH"),
        )

    root = repo_root or Path.cwd()
    try:
        commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        dirty = bool(status_result.stdout.strip())
        diff_hash: str | None = None
        if dirty:
            diff_result = subprocess.run(
                ["git", "diff", "--no-ext-diff", "--binary", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                timeout=5,
            )
            diff_hash = hashlib.sha256(diff_result.stdout).hexdigest()
        return SourceRevision(
            commit=commit_result.stdout.strip(),
            dirty=dirty,
            dirty_diff_hash=diff_hash,
        )
    except (OSError, subprocess.SubprocessError):
        return SourceRevision(commit="unknown", dirty=False, dirty_diff_hash=None)


def build_manifest(
    cfg: BacktestConfig,
    *,
    run_id: str,
    dataset_hash: str,
    warmup_hash: str,
    warmup_event_count: int,
    strategy: object,
) -> RunManifest:
    strategy_type = type(strategy)
    return RunManifest(
        schema_version=2,
        run_id=run_id,
        engine_version=__version__,
        engine_semantics_version=cfg.engine_semantics_version,
        config_hash=cfg.fingerprint(),
        dataset_hash=dataset_hash,
        raw_dataset_hash=cfg.dataset_sha256,
        warmup_hash=warmup_hash,
        warmup_event_count=warmup_event_count,
        strategy=f"{strategy_type.__module__}.{strategy_type.__qualname__}",
        strategy_hash=strategy_fingerprint(strategy),
        source=source_revision(),
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        rng_seed=cfg.execution.rng_seed,
        rng_stream_version="2",
        image_digest=os.getenv("QBT_IMAGE_DIGEST"),
    )


def strategy_fingerprint(strategy: object) -> str:
    """Hash the strategy implementation without including its filesystem path."""
    strategy_type = type(strategy)
    identity = f"{strategy_type.__module__}.{strategy_type.__qualname__}"
    try:
        source = inspect.getsource(strategy_type)
    except (OSError, TypeError):
        source = identity
    return content_hash({"identity": identity, "source": source})


@dataclass(frozen=True)
class ArtifactDescriptor:
    uri: str
    content_hash: str
    size_bytes: int


class ArtifactStore(Protocol):
    def write_payload(
        self,
        *,
        run_id: str,
        kind: str,
        payload: bytes,
    ) -> ArtifactDescriptor: ...


def serialize_result(result: BacktestResult, manifest: RunManifest) -> bytes:
    payload: dict[str, Any] = result.to_dict()
    payload["manifest"] = manifest.to_dict()
    return (json.dumps(payload, default=_json_default, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


@dataclass(frozen=True)
class LocalArtifactStore:
    root: Path

    def write_result(
        self,
        result: BacktestResult,
        *,
        manifest: RunManifest,
    ) -> ArtifactDescriptor:
        return self.write_payload(
            run_id=result.run_id,
            kind="result",
            payload=serialize_result(result, manifest),
        )

    def write_payload(
        self,
        *,
        run_id: str,
        kind: str,
        payload: bytes,
    ) -> ArtifactDescriptor:
        _validate_artifact_identity(run_id, kind)
        if not payload:
            raise ValueError("Artifact payload must not be empty")
        destination = self.root / run_id / f"{kind}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        if destination.exists():
            existing = destination.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest or len(existing) != len(payload):
                raise RuntimeError("Artifact destination already contains different bytes")
            return ArtifactDescriptor(
                uri=str(destination),
                content_hash=digest,
                size_bytes=len(payload),
            )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{kind}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return ArtifactDescriptor(
            uri=str(destination),
            content_hash=digest,
            size_bytes=len(payload),
        )


@dataclass(frozen=True)
class S3ArtifactStore:
    bucket: str
    prefix: str = "runs"
    kms_key_id: str | None = None
    endpoint_url: str | None = None

    def write_result(
        self,
        result: BacktestResult,
        *,
        manifest: RunManifest,
    ) -> ArtifactDescriptor:
        return self.write_payload(
            run_id=result.run_id,
            kind="result",
            payload=serialize_result(result, manifest),
        )

    def write_payload(
        self,
        *,
        run_id: str,
        kind: str,
        payload: bytes,
    ) -> ArtifactDescriptor:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError

        _validate_artifact_identity(run_id, kind)
        if not payload:
            raise ValueError("Artifact payload must not be empty")
        digest = hashlib.sha256(payload).hexdigest()
        key = f"{self.prefix.strip('/')}/{run_id}/{kind}.json"
        client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            config=Config(
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        existing = _existing_s3_artifact(
            client,
            bucket=self.bucket,
            key=key,
            digest=digest,
            size_bytes=len(payload),
        )
        if existing is not None:
            return existing
        request: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": payload,
            "ContentType": "application/json",
            "Metadata": {"sha256": digest, "run-id": run_id, "kind": kind},
            "ServerSideEncryption": "aws:kms" if self.kms_key_id else "AES256",
            "IfNoneMatch": "*",
        }
        if self.kms_key_id:
            request["SSEKMSKeyId"] = self.kms_key_id
        request["ChecksumSHA256"] = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        try:
            response = client.put_object(**request)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code not in {"PreconditionFailed", "412"}:
                raise
            concurrent = _existing_s3_artifact(
                client,
                bucket=self.bucket,
                key=key,
                digest=digest,
                size_bytes=len(payload),
            )
            if concurrent is None:
                raise RuntimeError("Concurrent artifact publication could not be verified") from exc
            return concurrent
        version_id = response.get("VersionId")
        version_suffix = f"?versionId={quote(str(version_id), safe='')}" if version_id else ""
        return ArtifactDescriptor(
            uri=f"s3://{self.bucket}/{key}{version_suffix}",
            content_hash=digest,
            size_bytes=len(payload),
        )


def _validate_artifact_identity(run_id: str, kind: str) -> None:
    try:
        canonical_run_id = str(uuid.UUID(run_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Artifact run_id must be a canonical UUID") from exc
    if canonical_run_id != run_id:
        raise ValueError("Artifact run_id must be a canonical UUID")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", kind):
        raise ValueError("Artifact kind must be a safe lowercase identifier")


def _existing_s3_artifact(
    client: Any,
    *,
    bucket: str,
    key: str,
    digest: str,
    size_bytes: int,
) -> ArtifactDescriptor | None:
    from botocore.exceptions import ClientError

    try:
        response = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", ""))
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    metadata = response.get("Metadata", {})
    if (
        not isinstance(metadata, dict)
        or metadata.get("sha256") != digest
        or int(response.get("ContentLength", -1)) != size_bytes
    ):
        raise RuntimeError("Artifact destination already contains different bytes")
    version_id = response.get("VersionId")
    version_suffix = f"?versionId={quote(str(version_id), safe='')}" if version_id else ""
    return ArtifactDescriptor(
        uri=f"s3://{bucket}/{key}{version_suffix}",
        content_hash=digest,
        size_bytes=size_bytes,
    )
