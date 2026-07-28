from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text

from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import AuditEvent, DatasetVersion
from quant_backtester.service.auth import Principal


@dataclass(frozen=True)
class DatasetSnapshot:
    id: str
    tenant_id: str
    name: str
    content_hash: str
    schema_version: int
    source_uri: str
    metadata: dict[str, object]
    created_at: dt.datetime

    @classmethod
    def from_model(cls, dataset: DatasetVersion) -> DatasetSnapshot:
        return cls(
            id=dataset.id,
            tenant_id=dataset.tenant_id,
            name=dataset.name,
            content_hash=dataset.content_hash,
            schema_version=dataset.schema_version,
            source_uri=dataset.source_uri,
            metadata=dict(dataset.metadata_json),
            created_at=dataset.created_at,
        )


@dataclass
class DatasetRepository:
    database: Database

    def register(
        self,
        *,
        principal: Principal,
        name: str,
        content_hash: str,
        schema_version: int,
        source_uri: str,
        metadata: dict[str, object],
    ) -> DatasetSnapshot:
        now = dt.datetime.now(dt.UTC)
        with self.database.transaction(tenant_id=principal.tenant_id) as session:
            if not self.database.is_sqlite:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant_id, 1))"),
                    {"tenant_id": principal.tenant_id},
                )
            existing = session.scalar(
                select(DatasetVersion).where(
                    DatasetVersion.tenant_id == principal.tenant_id,
                    DatasetVersion.content_hash == content_hash,
                )
            )
            if existing is not None:
                if existing.source_uri != source_uri:
                    raise ValueError(
                        "This content hash is already registered with a different source URI"
                    )
                return DatasetSnapshot.from_model(existing)

            dataset = DatasetVersion(
                id=str(uuid.uuid4()),
                tenant_id=principal.tenant_id,
                name=name,
                content_hash=content_hash,
                schema_version=schema_version,
                source_uri=source_uri,
                metadata_json=metadata,
                created_at=now,
            )
            session.add(dataset)
            session.add(
                AuditEvent(
                    tenant_id=principal.tenant_id,
                    actor_id=principal.actor_id,
                    action="dataset.register",
                    resource_type="dataset_version",
                    resource_id=dataset.id,
                    details={"content_hash": content_hash, "source_uri": source_uri},
                    created_at=now,
                )
            )
            session.flush()
            return DatasetSnapshot.from_model(dataset)

    def get(self, dataset_id: str, *, tenant_id: str) -> DatasetSnapshot | None:
        with self.database.session(tenant_id=tenant_id) as session:
            dataset = session.scalar(
                select(DatasetVersion).where(
                    DatasetVersion.id == dataset_id,
                    DatasetVersion.tenant_id == tenant_id,
                )
            )
            return DatasetSnapshot.from_model(dataset) if dataset is not None else None

    def list(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DatasetSnapshot]:
        if not 1 <= limit <= 1_000 or offset < 0:
            raise ValueError("Invalid pagination")
        with self.database.session(tenant_id=tenant_id) as session:
            datasets = session.scalars(
                select(DatasetVersion)
                .where(DatasetVersion.tenant_id == tenant_id)
                .order_by(DatasetVersion.created_at.desc(), DatasetVersion.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [DatasetSnapshot.from_model(dataset) for dataset in datasets]
