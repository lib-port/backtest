from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from quant_backtester.persistence.models import Base, Run


@dataclass
class Database:
    url: str
    _engine: Engine | None = field(default=None, init=False, repr=False)
    _session_factory: sessionmaker[Session] | None = field(default=None, init=False, repr=False)

    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self.url, future=True)
        return self._engine

    def create_tables(self) -> None:
        eng = self.engine()
        Base.metadata.create_all(eng)

    def session(self) -> Session:
        if self._session_factory is None:
            self._session_factory = sessionmaker(bind=self.engine(), future=True)
        return self._session_factory()

    def insert_runs_bulk(self, runs: Sequence[Run]) -> None:
        if not runs:
            return
        with self.session() as s:
            s.add_all(runs)
            s.commit()
