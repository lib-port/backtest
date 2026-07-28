from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def uuid_string() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_tenant_created_at", "tenant_id", "created_at"),
        Index("ix_runs_tenant_status", "tenant_id", "status"),
        UniqueConstraint("tenant_id", "run_id", name="uq_runs_tenant_run_id"),
        ForeignKeyConstraint(
            ["tenant_id", "dataset_version_id"],
            ["dataset_versions.tenant_id", "dataset_versions.id"],
            name="fk_runs_tenant_dataset_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "initial_cash > 0 AND short_window > 0 AND long_window > short_window",
            name="ck_runs_valid_configuration",
        ),
        CheckConstraint(
            "total_commission >= 0 AND status IN ('SUCCEEDED', 'FAILED', 'CANCELED')",
            name="ck_runs_valid_result",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=uuid_string
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="SUCCEEDED")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dataset_version_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    run_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    short_window: Mapped[int] = mapped_column(Integer, nullable=False)
    long_window: Mapped[int] = mapped_column(Integer, nullable=False)

    initial_cash: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)
    final_equity: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)
    total_return: Mapped[float] = mapped_column(Float, nullable=False)
    annualized_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    annualized_volatility: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sharpe: Mapped[float] = mapped_column(Float, nullable=False)
    sortino: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    calmar: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False)
    total_commission: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)
    total_slippage_cost: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)

    halted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    halt_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    result_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    data_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    extra: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)

    artifacts: Mapped[list[RunArtifact]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    artifact_publications: Mapped[list[ArtifactPublication]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    orders: Mapped[list[RunOrder]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    fills: Mapped[list[RunFill]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    equity_snapshots: Mapped[list[EquitySnapshot]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    journal_entries: Mapped[list[RunJournalEntry]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash"),
        UniqueConstraint("tenant_id", "id", name="uq_dataset_versions_tenant_id"),
        CheckConstraint("schema_version >= 1", name="ck_dataset_versions_schema"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class RunArtifact(Base):
    __tablename__ = "run_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", "uri"),
        CheckConstraint("size_bytes >= 0", name="ck_run_artifacts_size"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    run: Mapped[Run] = relationship(back_populates="artifacts")


class ArtifactPublication(Base):
    __tablename__ = "artifact_publications"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", name="uq_artifact_publications_run_kind"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.run_id"],
            name="fk_artifact_publications_tenant_run",
            ondelete="CASCADE",
        ),
        Index(
            "ix_artifact_publications_status_updated",
            "status",
            "updated_at",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED')",
            name="ck_artifact_publications_status",
        ),
        CheckConstraint(
            "size_bytes > 0 AND attempts >= 0",
            name="ck_artifact_publications_values",
        ),
        CheckConstraint(
            "(status = 'PUBLISHED' AND uri IS NOT NULL AND published_at IS NOT NULL) "
            "AND next_attempt_at IS NULL "
            "OR (status = 'PENDING' AND uri IS NULL AND published_at IS NULL "
            "AND next_attempt_at IS NOT NULL)",
            name="ck_artifact_publications_completion",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=utc_now,
    )

    run: Mapped[Run] = relationship(back_populates="artifact_publications")


class RunOrder(Base):
    __tablename__ = "run_orders"
    __table_args__ = (
        UniqueConstraint("run_id", "order_id"),
        CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND remaining_quantity >= 0 "
            "AND filled_quantity + remaining_quantity = quantity",
            name="ck_run_orders_quantities",
        ),
        CheckConstraint(
            "status IN ('CREATED', 'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', "
            "'CANCELED', 'REJECTED', 'EXPIRED')",
            name="ck_run_orders_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    client_order_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)
    time_in_force: Mapped[str] = mapped_column(String(8), nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="orders")


class RunFill(Base):
    __tablename__ = "run_fills"
    __table_args__ = (
        UniqueConstraint("run_id", "execution_id"),
        CheckConstraint(
            "quantity > 0 AND fill_price > 0 AND commission >= 0",
            name="ck_run_fills_values",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)
    mid_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)
    commission: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)
    slippage: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)

    run: Mapped[Run] = relationship(back_populates="fills")


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint("sequence > 0", name="ck_equity_snapshots_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)

    run: Mapped[Run] = relationship(back_populates="equity_snapshots")


class RunJournalEntry(Base):
    __tablename__ = "run_journal_entries"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint(
            "sequence > 0 AND amount >= 0",
            name="ck_run_journal_entries_values",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(256), nullable=False)
    debit_account: Mapped[str] = mapped_column(String(256), nullable=False)
    credit_account: Mapped[str] = mapped_column(String(256), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped[Run] = relationship(back_populates="journal_entries")


class RunJob(Base):
    __tablename__ = "run_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["tenant_id", "result_run_id"],
            ["runs.tenant_id", "runs.run_id"],
            name="fk_run_jobs_tenant_result_run",
            ondelete="RESTRICT",
        ),
        Index("ix_run_jobs_status_created_at", "status", "created_at"),
        Index("ix_run_jobs_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_run_jobs_tenant_created_at", "tenant_id", "created_at"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'WAITING_ARTIFACT', "
            "'SUCCEEDED', 'FAILED', 'CANCELED')",
            name="ck_run_jobs_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND max_attempts <= 10 "
            "AND attempts <= max_attempts",
            name="ck_run_jobs_attempts",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND worker_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND lease_generation > 0) "
            "OR (status != 'RUNNING' AND worker_id IS NULL AND lease_expires_at IS NULL)",
            name="ck_run_jobs_lease_state",
        ),
        CheckConstraint(
            "status != 'QUEUED' OR next_attempt_at IS NOT NULL",
            name="ck_run_jobs_queued_schedule",
        ),
        CheckConstraint(
            "status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELED') OR completed_at IS NOT NULL",
            name="ck_run_jobs_terminal_completion",
        ),
        CheckConstraint(
            "status != 'SUCCEEDED' OR result_run_id IS NOT NULL",
            name="ck_run_jobs_success_result",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("runs.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=utc_now,
    )
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    public_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    internal_error_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_tenant_created_at", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
