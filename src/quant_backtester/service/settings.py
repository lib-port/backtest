from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from quant_backtester.config import database_url_from_env

AuthMode = Literal["api-key", "oidc", "disabled"]
Environment = Literal["development", "test", "staging", "production"]
KNOWN_ROLES = frozenset({"observer", "researcher", "admin"})
PRODUCTION_ENVIRONMENTS = frozenset({"staging", "production"})
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class ApiKeyIdentity:
    token_sha256: str
    actor_id: str
    tenant_id: str
    role: str
    key_id: str = "default"
    expires_at: dt.datetime | None = None


@dataclass(frozen=True)
class ServiceSettings:
    database_url: str
    data_root: Path
    artifact_root: Path
    environment: Environment = "development"
    auth_mode: AuthMode = "api-key"
    api_keys: tuple[ApiKeyIdentity, ...] = ()
    api_key_pepper: str | None = None
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_tenant_claim: str = "tenant_id"
    oidc_roles_claim: str = "roles"
    api_thread_workers: int = 8
    max_active_jobs_per_tenant: int = 20
    max_request_bytes: int = 256_000
    max_dataset_bytes: int = 250_000_000
    max_symbols_per_run: int = 250
    max_events_per_run: int = 2_000_000
    max_unsorted_events_per_run: int = 250_000
    max_persisted_equity_points: int = 100_000
    max_attempts_per_job: int = 5
    require_strict_dataset_schema: bool = True
    queue_url: str | None = None
    dataset_bucket: str | None = None
    dataset_prefix: str = "datasets/"
    require_registered_datasets: bool = False

    def __post_init__(self) -> None:
        if self.environment not in ("development", "test", "staging", "production"):
            raise ValueError("QBT_ENVIRONMENT must be development, test, staging, or production")
        if self.auth_mode not in ("api-key", "oidc", "disabled"):
            raise ValueError("QBT_AUTH_MODE must be api-key, oidc, or disabled")
        if self.environment in PRODUCTION_ENVIRONMENTS and self.auth_mode == "disabled":
            raise ValueError("Authentication cannot be disabled in staging or production")
        if self.environment in PRODUCTION_ENVIRONMENTS and not self.require_registered_datasets:
            raise ValueError("Staging and production require registered immutable datasets")
        if self.environment in PRODUCTION_ENVIRONMENTS and not self.dataset_bucket:
            raise ValueError("Staging and production require QBT_DATASET_BUCKET")
        if self.auth_mode == "api-key" and not self.api_keys:
            raise ValueError("QBT_API_KEYS_JSON must configure at least one hashed API key")
        if (
            self.environment in PRODUCTION_ENVIRONMENTS
            and self.auth_mode == "api-key"
            and (self.api_key_pepper is None or len(self.api_key_pepper) < 32)
        ):
            raise ValueError("Staging and production API keys require a 32-character pepper")
        seen_digests: set[str] = set()
        seen_key_ids: set[str] = set()
        for identity in self.api_keys:
            if len(identity.token_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in identity.token_sha256
            ):
                raise ValueError("API key token_sha256 values must be lowercase SHA-256 digests")
            if identity.token_sha256 in seen_digests:
                raise ValueError("API key token digests must be unique")
            seen_digests.add(identity.token_sha256)
            if not 1 <= len(identity.key_id) <= 64 or not IDENTIFIER_PATTERN.fullmatch(
                identity.key_id
            ):
                raise ValueError("API key key_id is invalid")
            if identity.key_id in seen_key_ids:
                raise ValueError("API key key_id values must be unique")
            seen_key_ids.add(identity.key_id)
            if (
                not 1 <= len(identity.actor_id.strip()) <= 256
                or not 1 <= len(identity.tenant_id.strip()) <= 128
                or not IDENTIFIER_PATTERN.fullmatch(identity.actor_id)
                or not IDENTIFIER_PATTERN.fullmatch(identity.tenant_id)
            ):
                raise ValueError("API key identities require actor_id and tenant_id")
            if identity.role not in KNOWN_ROLES:
                raise ValueError(f"API key role must be one of {sorted(KNOWN_ROLES)}")
            if identity.expires_at is not None and identity.expires_at.tzinfo is None:
                raise ValueError("API key expires_at must be timezone-aware")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC mode requires issuer, audience, and JWKS URL")
        if self.environment in PRODUCTION_ENVIRONMENTS and self.auth_mode == "oidc":
            if not str(self.oidc_issuer).startswith("https://") or not str(
                self.oidc_jwks_url
            ).startswith("https://"):
                raise ValueError("Production OIDC issuer and JWKS URL must use HTTPS")
        if self.max_active_jobs_per_tenant <= 0:
            raise ValueError("max_active_jobs_per_tenant must be > 0")
        if not 1 <= self.api_thread_workers <= 64:
            raise ValueError("api_thread_workers must be in [1, 64]")
        if self.max_request_bytes <= 0 or self.max_dataset_bytes <= 0:
            raise ValueError("request and dataset byte limits must be > 0")
        if (
            min(
                self.max_symbols_per_run,
                self.max_events_per_run,
                self.max_unsorted_events_per_run,
                self.max_persisted_equity_points,
                self.max_attempts_per_job,
            )
            <= 0
        ):
            raise ValueError("Per-run limits must be > 0")
        if self.max_symbols_per_run > 1_000:
            raise ValueError("max_symbols_per_run must not exceed 1000")
        if self.max_events_per_run > 50_000_000:
            raise ValueError("max_events_per_run must not exceed 50000000")
        if self.max_unsorted_events_per_run > self.max_events_per_run:
            raise ValueError("max_unsorted_events_per_run must not exceed max_events_per_run")
        if self.max_attempts_per_job > 10:
            raise ValueError("max_attempts_per_job must not exceed 10")
        if self.environment in PRODUCTION_ENVIRONMENTS and not self.require_strict_dataset_schema:
            raise ValueError("Staging and production require strict dataset schemas")
        _validate_storage_prefix(self.dataset_prefix, name="QBT_DATASET_PREFIX")

    @classmethod
    def from_env(cls) -> ServiceSettings:
        environment = cast(Environment, os.getenv("QBT_ENVIRONMENT", "development"))
        auth_mode = cast(AuthMode, os.getenv("QBT_AUTH_MODE", "api-key"))
        raw_keys = os.getenv("QBT_API_KEYS_JSON", "[]")
        try:
            parsed_keys = json.loads(raw_keys)
        except json.JSONDecodeError as exc:
            raise ValueError("QBT_API_KEYS_JSON must be valid JSON") from exc
        if not isinstance(parsed_keys, list):
            raise ValueError("QBT_API_KEYS_JSON must be a JSON list")
        identities: list[ApiKeyIdentity] = []
        for entry in parsed_keys:
            if not isinstance(entry, dict):
                raise ValueError("Each API key identity must be an object")
            raw_expiry = entry.get("expires_at")
            expires_at: dt.datetime | None = None
            if raw_expiry is not None:
                try:
                    expires_at = dt.datetime.fromisoformat(str(raw_expiry))
                except ValueError as exc:
                    raise ValueError("API key expires_at must be an ISO-8601 timestamp") from exc
            identities.append(
                ApiKeyIdentity(
                    token_sha256=str(entry.get("token_sha256", "")),
                    actor_id=str(entry.get("actor_id", "")),
                    tenant_id=str(entry.get("tenant_id", "")),
                    role=str(entry.get("role", "")),
                    key_id=str(entry.get("key_id", "default")),
                    expires_at=expires_at,
                )
            )
        return cls(
            database_url=database_url_from_env(),
            data_root=Path(os.getenv("QBT_DATA_ROOT", ".")).resolve(),
            artifact_root=Path(os.getenv("QBT_ARTIFACT_ROOT", "runs")).resolve(),
            environment=environment,
            auth_mode=auth_mode,
            api_keys=tuple(identities),
            api_key_pepper=os.getenv("QBT_API_KEY_PEPPER"),
            oidc_issuer=os.getenv("QBT_OIDC_ISSUER"),
            oidc_audience=os.getenv("QBT_OIDC_AUDIENCE"),
            oidc_jwks_url=os.getenv("QBT_OIDC_JWKS_URL"),
            oidc_tenant_claim=os.getenv("QBT_OIDC_TENANT_CLAIM", "tenant_id"),
            oidc_roles_claim=os.getenv("QBT_OIDC_ROLES_CLAIM", "roles"),
            api_thread_workers=int(os.getenv("QBT_API_THREAD_WORKERS", "8")),
            max_active_jobs_per_tenant=int(os.getenv("QBT_MAX_ACTIVE_JOBS_PER_TENANT", "20")),
            max_request_bytes=int(os.getenv("QBT_MAX_REQUEST_BYTES", "256000")),
            max_dataset_bytes=int(os.getenv("QBT_MAX_DATASET_BYTES", "250000000")),
            max_symbols_per_run=int(os.getenv("QBT_MAX_SYMBOLS_PER_RUN", "250")),
            max_events_per_run=int(os.getenv("QBT_MAX_EVENTS_PER_RUN", "2000000")),
            max_unsorted_events_per_run=int(os.getenv("QBT_MAX_UNSORTED_EVENTS_PER_RUN", "250000")),
            max_persisted_equity_points=int(os.getenv("QBT_MAX_PERSISTED_EQUITY_POINTS", "100000")),
            max_attempts_per_job=int(os.getenv("QBT_MAX_ATTEMPTS_PER_JOB", "5")),
            require_strict_dataset_schema=env_bool(
                "QBT_REQUIRE_STRICT_DATASET_SCHEMA",
                True,
            ),
            queue_url=os.getenv("QBT_QUEUE_URL"),
            dataset_bucket=os.getenv("QBT_DATASET_BUCKET"),
            dataset_prefix=os.getenv("QBT_DATASET_PREFIX", "datasets/"),
            require_registered_datasets=env_bool(
                "QBT_REQUIRE_REGISTERED_DATASETS",
                environment in PRODUCTION_ENVIRONMENTS,
            ),
        )


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    data_root: Path
    artifact_root: Path
    worker_id: str
    environment: Environment = "development"
    poll_seconds: float = 1.0
    lease_seconds: int = 30
    max_dataset_bytes: int = 250_000_000
    max_symbols_per_run: int = 250
    max_events_per_run: int = 2_000_000
    max_unsorted_events_per_run: int = 250_000
    max_persisted_equity_points: int = 100_000
    max_run_seconds: float = 3_600.0
    artifact_bucket: str | None = None
    artifact_prefix: str = "runs"
    artifact_kms_key_id: str | None = None
    s3_endpoint_url: str | None = None
    queue_url: str | None = None
    dataset_bucket: str | None = None
    dataset_prefix: str = "datasets/"
    allow_local_datasets: bool = True
    require_versioned_datasets: bool = False
    require_dataset_hash: bool = False
    require_sorted_data: bool = False
    require_strict_dataset_schema: bool = True
    aws_endpoint_url: str | None = None

    def __post_init__(self) -> None:
        if self.environment not in ("development", "test", "staging", "production"):
            raise ValueError("QBT_ENVIRONMENT must be development, test, staging, or production")
        if self.poll_seconds <= 0 or self.lease_seconds <= 0:
            raise ValueError("worker poll and lease settings must be > 0")
        if self.max_dataset_bytes <= 0:
            raise ValueError("max_dataset_bytes must be > 0")
        if (
            min(
                self.max_symbols_per_run,
                self.max_events_per_run,
                self.max_unsorted_events_per_run,
                self.max_persisted_equity_points,
                self.max_run_seconds,
            )
            <= 0
        ):
            raise ValueError("Worker compute limits must be > 0")
        if self.max_symbols_per_run > 1_000 or self.max_events_per_run > 50_000_000:
            raise ValueError("Worker compute limits exceed engine safety bounds")
        if self.max_unsorted_events_per_run > self.max_events_per_run:
            raise ValueError("max_unsorted_events_per_run must not exceed max_events_per_run")
        if self.max_run_seconds > 86_400:
            raise ValueError("max_run_seconds must not exceed 86400")
        if self.require_versioned_datasets and not self.dataset_bucket:
            raise ValueError("Versioned datasets require QBT_DATASET_BUCKET")
        _validate_storage_prefix(self.dataset_prefix, name="QBT_DATASET_PREFIX")
        _validate_storage_prefix(self.artifact_prefix, name="QBT_ARTIFACT_PREFIX")
        if self.environment in PRODUCTION_ENVIRONMENTS:
            if self.allow_local_datasets:
                raise ValueError("Staging and production workers must disable local datasets")
            if not self.require_versioned_datasets or not self.require_dataset_hash:
                raise ValueError(
                    "Staging and production workers require versioned, hash-verified datasets"
                )
            if not self.require_sorted_data:
                raise ValueError("Staging and production workers require sorted market data")
            if not self.require_strict_dataset_schema:
                raise ValueError("Staging and production workers require strict dataset schemas")
            if not self.artifact_bucket:
                raise ValueError("Staging and production workers require QBT_ARTIFACT_BUCKET")


def _validate_storage_prefix(value: str, *, name: str) -> None:
    normalized = value.strip("/")
    if (
        not normalized
        or len(normalized) > 512
        or "\\" in normalized
        or any(part in ("", ".", "..") for part in normalized.split("/"))
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"{name} must be a safe, non-empty object key prefix")
