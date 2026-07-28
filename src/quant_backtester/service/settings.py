from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from quant_backtester.config import database_url_from_env

AuthMode = Literal["api-key", "oidc", "disabled"]
Environment = Literal["development", "test", "production"]
KNOWN_ROLES = frozenset({"observer", "researcher", "admin"})


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


@dataclass(frozen=True)
class ServiceSettings:
    database_url: str
    data_root: Path
    artifact_root: Path
    environment: Environment = "development"
    auth_mode: AuthMode = "api-key"
    api_keys: tuple[ApiKeyIdentity, ...] = ()
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_tenant_claim: str = "tenant_id"
    oidc_roles_claim: str = "roles"
    max_active_jobs_per_tenant: int = 20
    max_request_bytes: int = 256_000
    max_dataset_bytes: int = 250_000_000
    queue_url: str | None = None
    dataset_bucket: str | None = None
    dataset_prefix: str = "datasets/"
    require_registered_datasets: bool = False

    def __post_init__(self) -> None:
        if self.environment not in ("development", "test", "production"):
            raise ValueError("QBT_ENVIRONMENT must be development, test, or production")
        if self.auth_mode not in ("api-key", "oidc", "disabled"):
            raise ValueError("QBT_AUTH_MODE must be api-key, oidc, or disabled")
        if self.environment == "production" and self.auth_mode == "disabled":
            raise ValueError("Authentication cannot be disabled in production")
        if self.environment == "production" and not self.require_registered_datasets:
            raise ValueError("Production requires registered immutable datasets")
        if self.environment == "production" and not self.dataset_bucket:
            raise ValueError("Production requires QBT_DATASET_BUCKET")
        if self.auth_mode == "api-key" and not self.api_keys:
            raise ValueError("QBT_API_KEYS_JSON must configure at least one hashed API key")
        for identity in self.api_keys:
            if len(identity.token_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in identity.token_sha256
            ):
                raise ValueError("API key token_sha256 values must be lowercase SHA-256 digests")
            if not identity.actor_id.strip() or not identity.tenant_id.strip():
                raise ValueError("API key identities require actor_id and tenant_id")
            if identity.role not in KNOWN_ROLES:
                raise ValueError(f"API key role must be one of {sorted(KNOWN_ROLES)}")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC mode requires issuer, audience, and JWKS URL")
        if self.environment == "production" and self.auth_mode == "oidc":
            if not str(self.oidc_issuer).startswith("https://") or not str(
                self.oidc_jwks_url
            ).startswith("https://"):
                raise ValueError("Production OIDC issuer and JWKS URL must use HTTPS")
        if self.max_active_jobs_per_tenant <= 0:
            raise ValueError("max_active_jobs_per_tenant must be > 0")
        if self.max_request_bytes <= 0 or self.max_dataset_bytes <= 0:
            raise ValueError("request and dataset byte limits must be > 0")

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
            identities.append(
                ApiKeyIdentity(
                    token_sha256=str(entry.get("token_sha256", "")),
                    actor_id=str(entry.get("actor_id", "")),
                    tenant_id=str(entry.get("tenant_id", "")),
                    role=str(entry.get("role", "")),
                )
            )
        return cls(
            database_url=database_url_from_env(),
            data_root=Path(os.getenv("QBT_DATA_ROOT", ".")).resolve(),
            artifact_root=Path(os.getenv("QBT_ARTIFACT_ROOT", "runs")).resolve(),
            environment=environment,
            auth_mode=auth_mode,
            api_keys=tuple(identities),
            oidc_issuer=os.getenv("QBT_OIDC_ISSUER"),
            oidc_audience=os.getenv("QBT_OIDC_AUDIENCE"),
            oidc_jwks_url=os.getenv("QBT_OIDC_JWKS_URL"),
            oidc_tenant_claim=os.getenv("QBT_OIDC_TENANT_CLAIM", "tenant_id"),
            oidc_roles_claim=os.getenv("QBT_OIDC_ROLES_CLAIM", "roles"),
            max_active_jobs_per_tenant=int(os.getenv("QBT_MAX_ACTIVE_JOBS_PER_TENANT", "20")),
            max_request_bytes=int(os.getenv("QBT_MAX_REQUEST_BYTES", "256000")),
            max_dataset_bytes=int(os.getenv("QBT_MAX_DATASET_BYTES", "250000000")),
            queue_url=os.getenv("QBT_QUEUE_URL"),
            dataset_bucket=os.getenv("QBT_DATASET_BUCKET"),
            dataset_prefix=os.getenv("QBT_DATASET_PREFIX", "datasets/"),
            require_registered_datasets=env_bool(
                "QBT_REQUIRE_REGISTERED_DATASETS",
                environment == "production",
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
    aws_endpoint_url: str | None = None

    def __post_init__(self) -> None:
        if self.environment not in ("development", "test", "production"):
            raise ValueError("QBT_ENVIRONMENT must be development, test, or production")
        if self.poll_seconds <= 0 or self.lease_seconds <= 0:
            raise ValueError("worker poll and lease settings must be > 0")
        if self.max_dataset_bytes <= 0:
            raise ValueError("max_dataset_bytes must be > 0")
        if self.require_versioned_datasets and not self.dataset_bucket:
            raise ValueError("Versioned datasets require QBT_DATASET_BUCKET")
        if self.environment == "production":
            if self.allow_local_datasets:
                raise ValueError("Production workers must disable local datasets")
            if not self.require_versioned_datasets or not self.require_dataset_hash:
                raise ValueError("Production workers require versioned, hash-verified datasets")
            if not self.require_sorted_data:
                raise ValueError("Production workers require sorted market data")
            if not self.artifact_bucket:
                raise ValueError("Production workers require QBT_ARTIFACT_BUCKET")
