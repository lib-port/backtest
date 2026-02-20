from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.UTC),
        nullable=False,
        index=True,
    )

    run_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    short_window: Mapped[int] = mapped_column(Integer, nullable=False)
    long_window: Mapped[int] = mapped_column(Integer, nullable=False)

    initial_cash: Mapped[float] = mapped_column(Float, nullable=False)
    final_equity: Mapped[float] = mapped_column(Float, nullable=False)
    total_return: Mapped[float] = mapped_column(Float, nullable=False)
    sharpe: Mapped[float] = mapped_column(Float, nullable=False)
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False)

    total_commission: Mapped[float] = mapped_column(Float, nullable=False)
    total_slippage_cost: Mapped[float] = mapped_column(Float, nullable=False)

    halted: Mapped[int] = mapped_column(Integer, nullable=False)  # 0/1
    halt_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    extra: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
