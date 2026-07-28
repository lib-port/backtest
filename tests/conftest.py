from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import httpx
import pytest

from quant_backtester.persistence.db import Database
from quant_backtester.service.api import create_app
from quant_backtester.service.settings import ServiceSettings

DatabaseFactory = Callable[[Path], Database]
ApiClientFactory = Callable[
    [ServiceSettings],
    AbstractAsyncContextManager[httpx.AsyncClient],
]


@pytest.fixture
def database_factory() -> Iterator[DatabaseFactory]:
    databases: list[Database] = []

    def create(path: Path) -> Database:
        database = Database(f"sqlite:///{path}")
        database.prepare_schema()
        databases.append(database)
        return database

    yield create
    for database in databases:
        database.close()


@pytest.fixture
def api_client() -> ApiClientFactory:
    @asynccontextmanager
    async def open_client(settings: ServiceSettings) -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                yield client

    return open_client
