from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import json
import os
import platform
import subprocess
import sys
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
        extra = dict(raw_extra)
        extra.pop("manifest", None)
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
    sharpe: float
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
        extra = dict(self.extra)
        extra.pop("manifest", None)
        payload["extra"] = extra
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
    strategy: str
    source: SourceRevision
    python_version: str
    platform: str
    rng_seed: int

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
    strategy: str,
) -> RunManifest:
    return RunManifest(
        schema_version=1,
        run_id=run_id,
        engine_version=__version__,
        engine_semantics_version=cfg.engine_semantics_version,
        config_hash=cfg.fingerprint(),
        dataset_hash=dataset_hash,
        strategy=strategy,
        source=source_revision(),
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        rng_seed=cfg.execution.rng_seed,
    )


@dataclass(frozen=True)
class ArtifactDescriptor:
    uri: str
    content_hash: str
    size_bytes: int


class ArtifactStore(Protocol):
    def write_result(
        self,
        result: BacktestResult,
        *,
        manifest: RunManifest,
    ) -> ArtifactDescriptor: ...


def _result_bytes(result: BacktestResult, manifest: RunManifest) -> bytes:
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
        destination = self.root / result.run_id / "result.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        serialized = _result_bytes(result, manifest)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_bytes(serialized)
        os.replace(temporary, destination)
        return ArtifactDescriptor(
            uri=str(destination),
            content_hash=hashlib.sha256(serialized).hexdigest(),
            size_bytes=len(serialized),
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
        import boto3

        body = _result_bytes(result, manifest)
        digest = hashlib.sha256(body).hexdigest()
        key = f"{self.prefix.strip('/')}/{result.run_id}/result.json"
        client = boto3.client("s3", endpoint_url=self.endpoint_url)
        request: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentType": "application/json",
            "Metadata": {"sha256": digest, "run-id": result.run_id},
            "ServerSideEncryption": "aws:kms" if self.kms_key_id else "AES256",
        }
        if self.kms_key_id:
            request["SSEKMSKeyId"] = self.kms_key_id
        request["ChecksumSHA256"] = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        response = client.put_object(**request)
        version_id = response.get("VersionId")
        version_suffix = f"?versionId={quote(str(version_id), safe='')}" if version_id else ""
        return ArtifactDescriptor(
            uri=f"s3://{self.bucket}/{key}{version_suffix}",
            content_hash=digest,
            size_bytes=len(body),
        )
