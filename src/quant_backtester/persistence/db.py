from __future__ import annotations

import datetime as dt
import hashlib
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import Engine, create_engine, event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from quant_backtester.persistence.models import (
    ArtifactPublication,
    AuditEvent,
    Base,
    Run,
    RunArtifact,
)

EXPECTED_SCHEMA_REVISION = "0011"


@dataclass(frozen=True)
class PendingArtifactPublication:
    id: str
    tenant_id: str
    run_id: str
    kind: str
    payload: bytes
    content_hash: str
    size_bytes: int
    attempts: int


@dataclass
class Database:
    url: str
    _engine: Engine | None = field(default=None, init=False, repr=False)
    _session_factory: sessionmaker[Session] | None = field(default=None, init=False, repr=False)

    def engine(self) -> Engine:
        if self._engine is None:
            backend = make_url(self.url).get_backend_name()
            connect_args: dict[str, object] = {}
            engine_options: dict[str, object] = {
                "future": True,
                "pool_pre_ping": True,
            }
            if backend == "sqlite":
                connect_args["check_same_thread"] = False
                connect_args["timeout"] = 30
            else:
                pool_size = int(os.getenv("QBT_DB_POOL_SIZE", "5"))
                max_overflow = int(os.getenv("QBT_DB_MAX_OVERFLOW", "5"))
                if not 1 <= pool_size <= 100 or not 0 <= max_overflow <= 100:
                    raise ValueError("Database pool settings are outside safe bounds")
                connect_args["connect_timeout"] = 10
                connect_args["application_name"] = "quant-backtester"
                engine_options.update(
                    {
                        "pool_size": pool_size,
                        "max_overflow": max_overflow,
                        "pool_timeout": 10,
                        "pool_recycle": 1_800,
                    }
                )
            self._engine = create_engine(
                self.url,
                connect_args=connect_args,
                **engine_options,
            )
            if backend == "sqlite":
                event.listen(self._engine, "connect", _configure_sqlite)
        return self._engine

    @property
    def is_sqlite(self) -> bool:
        return make_url(self.url).get_backend_name() == "sqlite"

    def prepare_schema(self) -> None:
        """Create local schemas, but require Alembic-managed production databases."""
        if self.is_sqlite or os.getenv("QBT_AUTO_CREATE_SCHEMA", "").lower() == "true":
            self.create_tables()
            return
        if not inspect(self.engine()).has_table("alembic_version"):
            raise RuntimeError(
                "Database schema is not initialized. Run `alembic upgrade head` before startup."
            )
        with self.engine().connect() as connection:
            revisions = set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
        if revisions != {EXPECTED_SCHEMA_REVISION}:
            found = ", ".join(sorted(revisions)) or "none"
            raise RuntimeError(
                f"Database schema revision is {found}; expected {EXPECTED_SCHEMA_REVISION}. "
                "Run `alembic upgrade head` before startup."
            )

    def create_tables(self) -> None:
        Base.metadata.create_all(self.engine())
        if self.is_sqlite:
            with self.engine().begin() as connection:
                connection.execute(
                    text(
                        "CREATE TRIGGER IF NOT EXISTS audit_events_no_update "
                        "BEFORE UPDATE ON audit_events BEGIN "
                        "SELECT RAISE(ABORT, 'audit_events is append-only'); END"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TRIGGER IF NOT EXISTS audit_events_no_delete "
                        "BEFORE DELETE ON audit_events BEGIN "
                        "SELECT RAISE(ABORT, 'audit_events is append-only'); END"
                    )
                )

    def _new_session(self) -> Session:
        if self._session_factory is None:
            self._session_factory = sessionmaker(
                bind=self.engine(),
                future=True,
                expire_on_commit=False,
            )
        return self._session_factory()

    def session(self, *, tenant_id: str | None = None) -> Session:
        session = self._new_session()
        if tenant_id is not None:
            self._set_tenant_context(session, tenant_id)
        return session

    @contextmanager
    def transaction(self, *, tenant_id: str | None = None) -> Iterator[Session]:
        with self._new_session() as session:
            with session.begin():
                if tenant_id is not None:
                    self._set_tenant_context(session, tenant_id)
                yield session

    def insert_run(self, run: Run) -> Run:
        with self.transaction() as session:
            existing = session.scalar(select(Run).where(Run.run_id == run.run_id))
            if existing is not None:
                if existing.result_hash == run.result_hash:
                    return existing
                raise ValueError(f"run_id {run.run_id!r} already contains a different result")
            session.add(run)
        return run

    def stage_run_with_artifact(
        self,
        run: Run,
        *,
        kind: str,
        payload: bytes,
    ) -> Run:
        """Atomically persist a run and an exact artifact payload to publish."""
        normalized_kind = kind.strip()
        if not normalized_kind or len(normalized_kind) > 64:
            raise ValueError("Artifact kind must contain 1 to 64 characters")
        if not payload:
            raise ValueError("Artifact payload must not be empty")
        try:
            encoded_payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Artifact payload must be UTF-8") from exc
        digest = hashlib.sha256(payload).hexdigest()
        with self.transaction() as session:
            existing = session.scalar(
                select(Run).where(
                    Run.run_id == run.run_id,
                    Run.tenant_id == run.tenant_id,
                )
            )
            if existing is None:
                new_publication = ArtifactPublication(
                    tenant_id=run.tenant_id,
                    run_id=run.run_id,
                    kind=normalized_kind,
                    status="PENDING",
                    payload=encoded_payload,
                    content_hash=digest,
                    size_bytes=len(payload),
                )
                run.artifact_publications.append(new_publication)
                session.add(run)
                resolved = run
            else:
                if existing.result_hash != run.result_hash:
                    raise ValueError(f"run_id {run.run_id!r} already contains a different result")
                publication = session.scalar(
                    select(ArtifactPublication).where(
                        ArtifactPublication.run_id == run.run_id,
                        ArtifactPublication.tenant_id == run.tenant_id,
                        ArtifactPublication.kind == normalized_kind,
                    )
                )
                if publication is None:
                    session.add(
                        ArtifactPublication(
                            tenant_id=run.tenant_id,
                            run_id=run.run_id,
                            kind=normalized_kind,
                            status="PENDING",
                            payload=encoded_payload,
                            content_hash=digest,
                            size_bytes=len(payload),
                        )
                    )
                elif (
                    publication.content_hash != digest
                    or publication.size_bytes != len(payload)
                    or publication.payload != encoded_payload
                ):
                    raise ValueError(
                        "Staged artifact payload does not match the existing outbox row"
                    )
                resolved = existing
        return resolved

    def pending_artifact_publications(
        self,
        run_id: str,
        *,
        tenant_id: str,
        due_only: bool = False,
    ) -> list[PendingArtifactPublication]:
        with self.session(tenant_id=tenant_id) as session:
            statement = select(ArtifactPublication).where(
                ArtifactPublication.run_id == run_id,
                ArtifactPublication.tenant_id == tenant_id,
                ArtifactPublication.status == "PENDING",
            )
            if due_only:
                now = dt.datetime.now(dt.UTC)
                statement = statement.where(ArtifactPublication.next_attempt_at <= now)
            rows = session.scalars(
                statement.order_by(ArtifactPublication.created_at, ArtifactPublication.id)
            )
            return [
                PendingArtifactPublication(
                    id=row.id,
                    tenant_id=row.tenant_id,
                    run_id=row.run_id,
                    kind=row.kind,
                    payload=row.payload.encode("utf-8"),
                    content_hash=row.content_hash,
                    size_bytes=row.size_bytes,
                    attempts=row.attempts,
                )
                for row in rows
            ]

    def next_due_artifact_publication(self) -> PendingArtifactPublication | None:
        now = dt.datetime.now(dt.UTC)
        with self.session() as session:
            row = session.scalar(
                select(ArtifactPublication)
                .where(
                    ArtifactPublication.status == "PENDING",
                    ArtifactPublication.next_attempt_at <= now,
                )
                .order_by(
                    ArtifactPublication.next_attempt_at,
                    ArtifactPublication.created_at,
                    ArtifactPublication.id,
                )
                .limit(1)
            )
            if row is None:
                return None
            return PendingArtifactPublication(
                id=row.id,
                tenant_id=row.tenant_id,
                run_id=row.run_id,
                kind=row.kind,
                payload=row.payload.encode("utf-8"),
                content_hash=row.content_hash,
                size_bytes=row.size_bytes,
                attempts=row.attempts,
            )

    def mark_artifact_published(
        self,
        publication_id: str,
        *,
        tenant_id: str,
        uri: str,
        content_hash: str,
        size_bytes: int,
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        with self.transaction(tenant_id=tenant_id) as session:
            publication = session.scalar(
                select(ArtifactPublication)
                .where(
                    ArtifactPublication.id == publication_id,
                    ArtifactPublication.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if publication is None:
                raise ValueError("Artifact publication does not exist")
            if publication.content_hash != content_hash or publication.size_bytes != size_bytes:
                raise ValueError("Published artifact descriptor does not match staged bytes")
            if publication.status == "PUBLISHED":
                if publication.uri != uri:
                    raise ValueError("Artifact publication already records a different URI")
                return
            existing_artifact = session.scalar(
                select(RunArtifact).where(
                    RunArtifact.run_id == publication.run_id,
                    RunArtifact.kind == publication.kind,
                )
            )
            if existing_artifact is None:
                session.add(
                    RunArtifact(
                        run_id=publication.run_id,
                        kind=publication.kind,
                        uri=uri,
                        content_hash=content_hash,
                        size_bytes=size_bytes,
                        created_at=now,
                    )
                )
            elif (
                existing_artifact.uri != uri
                or existing_artifact.content_hash != content_hash
                or existing_artifact.size_bytes != size_bytes
            ):
                raise ValueError("Run already records a different artifact")
            publication.status = "PUBLISHED"
            publication.uri = uri
            publication.attempts += 1
            publication.last_error = None
            publication.updated_at = now
            publication.published_at = now
            publication.next_attempt_at = None
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    actor_id="system:artifact-publisher",
                    action="artifact.publish",
                    resource_type="run",
                    resource_id=publication.run_id,
                    details={
                        "kind": publication.kind,
                        "content_hash": content_hash,
                        "size_bytes": size_bytes,
                    },
                    created_at=now,
                )
            )

    def record_artifact_publication_failure(
        self,
        publication_id: str,
        *,
        tenant_id: str,
        error: str,
    ) -> None:
        with self.transaction(tenant_id=tenant_id) as session:
            publication = session.scalar(
                select(ArtifactPublication)
                .where(
                    ArtifactPublication.id == publication_id,
                    ArtifactPublication.tenant_id == tenant_id,
                    ArtifactPublication.status == "PENDING",
                )
                .with_for_update()
            )
            if publication is not None:
                publication.attempts += 1
                publication.last_error = error[:2_000]
                now = dt.datetime.now(dt.UTC)
                publication.updated_at = now
                exponential = min(300, 5 * (2 ** min(publication.attempts - 1, 10)))
                jitter = int(
                    hashlib.sha256(f"{publication.id}:{publication.attempts}".encode()).hexdigest()[
                        :8
                    ],
                    16,
                ) % (max(1, exponential // 2) + 1)
                publication.next_attempt_at = now + dt.timedelta(
                    seconds=min(300, exponential + jitter)
                )

    def insert_runs_bulk(self, runs: Sequence[Run]) -> None:
        if not runs:
            return
        try:
            with self.transaction() as session:
                for run in runs:
                    existing = session.scalar(select(Run).where(Run.run_id == run.run_id))
                    if existing is None:
                        session.add(run)
                    elif existing.result_hash != run.result_hash:
                        raise ValueError(
                            f"run_id {run.run_id!r} already contains a different result"
                        )
        except IntegrityError as exc:
            raise ValueError("Run persistence violated a uniqueness constraint") from exc

    def get_run(self, run_id: str, *, tenant_id: str = "default") -> Run | None:
        with self.session(tenant_id=tenant_id) as session:
            return session.scalar(
                select(Run).where(Run.run_id == run_id, Run.tenant_id == tenant_id)
            )

    def list_runs(
        self,
        *,
        tenant_id: str = "default",
        limit: int = 100,
        offset: int = 0,
    ) -> list[Run]:
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be in [1, 1000]")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        with self.session(tenant_id=tenant_id) as session:
            rows = session.scalars(
                select(Run)
                .where(Run.tenant_id == tenant_id)
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(rows)

    def ping(self) -> bool:
        try:
            with self.engine().connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    def _set_tenant_context(self, session: Session, tenant_id: str) -> None:
        normalized = tenant_id.strip()
        if not 1 <= len(normalized) <= 128:
            raise ValueError("tenant_id must contain 1 to 128 characters")
        if self.is_sqlite:
            return
        session.execute(
            text("SELECT set_config('qbt.tenant_id', :tenant_id, true)"),
            {"tenant_id": normalized},
        )


def _configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()
