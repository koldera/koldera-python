from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import respx

import koldera.async_client as async_module
import koldera.client as sync_module
from koldera import AsyncKolderaClient, KolderaClient
from support import BASE_URL, SECRET_KEY, Golden, SyncFacade

MakeClient = Callable[..., Any]


@pytest.fixture
def golden() -> Iterator[Golden]:
    script = Golden()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route().mock(side_effect=script.handler)
        yield script
    script.assert_consumed()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Records every pause of both clients instead of sleeping."""
    recorded: list[float] = []

    def sync_sleep(seconds: float) -> None:
        recorded.append(seconds)

    async def async_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(sync_module, "_sleep", sync_sleep)
    monkeypatch.setattr(async_module, "_sleep", async_sleep)
    return recorded


@pytest.fixture(params=["sync", "async"])
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def make(kind: str, golden: Golden, sleeps: list[float]) -> Iterator[MakeClient]:
    """Builds a client of the parametrised kind; the async one is wrapped so tests can call it like the sync one."""
    loop = asyncio.new_event_loop()
    made: list[Any] = []

    def factory(token: str = SECRET_KEY, **options: Any) -> Any:
        options.setdefault("base_url", BASE_URL)
        client: Any = (
            KolderaClient(token=token, **options) if kind == "sync" else SyncFacade(AsyncKolderaClient(token=token, **options), loop)
        )
        made.append(client)
        return client

    yield factory
    for client in made:
        client.close()
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


@pytest.fixture
def client(make: MakeClient) -> Any:
    return make()
