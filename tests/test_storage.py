"""Signed storage exchanges use an SDK-owned client: no caller transport, no credential, own timeout, no URL in errors."""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from koldera import Artifact, AsyncKolderaClient, ConfigurationError, KolderaClient, StorageError
from koldera._core import Config
from support import BASE_URL, FIXTURES, PDF_BYTES, SECRET_KEY, Golden

SIGNED = "https://storage.test/blob?exp=1790000000&sig=c2lnbmF0dXJl"
TICKET = FIXTURES["upload-ticket-pdf"]["response"]["body"] | {"uploadUrl": SIGNED}


def _artifact(data: bytes) -> Artifact:
    return Artifact(
        id="a",
        kind="remediated",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        download_url=SIGNED,
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


class Drip(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, chunks: int, pause: float) -> None:
        self.chunks = chunks
        self.pause = pause

    def __iter__(self) -> Any:
        for _ in range(self.chunks):
            time.sleep(self.pause)
            yield b"x"

    async def __aiter__(self) -> Any:
        for _ in range(self.chunks):
            await asyncio.sleep(self.pause)
            yield b"x"


def _api(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=TICKET)

    return httpx.MockTransport(handler)


def _storage(seen: list[httpx.Request], content: bytes = b"") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=content)

    return httpx.MockTransport(handler)


def _failing(error: Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return httpx.MockTransport(handler)


def _assert_no_url(error: BaseException) -> None:
    assert "sig=" not in str(error) and "sig=" not in repr(error) and "storage.test" not in repr(error)
    assert all("sig=" not in repr(arg) for arg in error.args)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not hasattr(error, "request")


def test_upload_and_download_bypass_the_caller_transport_and_carry_no_credential(make: Any, golden: Golden) -> None:
    api: list[httpx.Request] = []
    stored: list[httpx.Request] = []
    data = b"artifact bytes"
    client = make(transport=_api(api), storage_transport=_storage(stored, data))
    client.uploads.upload(PDF_BYTES, "application/pdf", "sample.pdf")
    assert client.artifacts.download(_artifact(data)) == data
    assert [(r.method, r.url.path) for r in api] == [("POST", "/v1/uploads")]
    assert [r.method for r in stored] == ["PUT", "GET"]
    assert all("authorization" not in r.headers for r in stored)
    assert all(str(r.url) == SIGNED for r in stored)


def test_storage_does_not_follow_redirects(make: Any, golden: Golden) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.test/x"})

    client = make(storage_transport=httpx.MockTransport(handler))
    with pytest.raises(StorageError) as caught:
        client.artifacts.download(_artifact(b"x"))
    assert caught.value.status == 302
    assert len(seen) == 1


@pytest.mark.parametrize("error", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("bad")])
def test_a_storage_download_failure_is_a_storage_error_without_the_url(make: Any, golden: Golden, error: Exception) -> None:
    client = make(storage_transport=_failing(error))
    with pytest.raises(StorageError) as caught:
        client.artifacts.download(_artifact(b"x"))
    assert str(caught.value) == f"download failed: {type(error).__name__}"
    _assert_no_url(caught.value)


@pytest.mark.parametrize("error", [httpx.ConnectError("boom"), httpx.WriteTimeout("slow")])
def test_a_storage_upload_failure_is_a_storage_error_without_the_url(make: Any, golden: Golden, error: Exception) -> None:
    api: list[httpx.Request] = []
    client = make(transport=_api(api), storage_transport=_failing(error))
    with pytest.raises(StorageError) as caught:
        client.uploads.upload(PDF_BYTES, "application/pdf", "sample.pdf")
    assert str(caught.value) == f"upload failed: {type(error).__name__}"
    _assert_no_url(caught.value)


def test_a_storage_transfer_that_drips_past_storage_timeout_fails(make: Any, golden: Golden) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip(40, 0.05)))
    client = make(storage_transport=transport, storage_timeout=0.3)
    started = time.monotonic()
    with pytest.raises(StorageError) as caught:
        client.artifacts.download(_artifact(b"x" * 40))
    assert time.monotonic() - started < 1.5
    assert str(caught.value) == "download failed: storage_timeout of 0.3 seconds exceeded"
    _assert_no_url(caught.value)


def test_a_slow_upload_past_storage_timeout_fails(make: Any, golden: Golden) -> None:
    class Slow(httpx.BaseTransport, httpx.AsyncBaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            for _ in request.stream:  # type: ignore[union-attr]
                time.sleep(0.4)
            return httpx.Response(200)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(2)
            return httpx.Response(200)

    big = PDF_BYTES + b"0" * (256 * 1024)
    api: list[httpx.Request] = []
    client = make(transport=_api(api), storage_transport=Slow(), storage_timeout=0.3)
    with pytest.raises(StorageError) as caught:
        client.uploads.upload(big, "application/pdf", "sample.pdf")
    assert str(caught.value) == "upload failed: storage_timeout of 0.3 seconds exceeded"
    _assert_no_url(caught.value)


@pytest.mark.parametrize("value", [0, -1, float("nan")])
def test_storage_timeout_must_be_positive(value: float) -> None:
    with pytest.raises(ConfigurationError, match="storage_timeout"):
        KolderaClient(token=SECRET_KEY, base_url=BASE_URL, storage_timeout=value)


def test_the_default_storage_timeout_is_600_seconds_and_both_clients_close_with_the_client() -> None:
    client = KolderaClient(token=SECRET_KEY, base_url=BASE_URL)
    storage = client._runner.storage
    assert storage.timeout == httpx.Timeout(600.0)
    assert not storage.is_closed
    client.close()
    assert storage.is_closed and client._runner.http.is_closed


def test_the_async_client_closes_its_storage_client() -> None:
    async def scenario() -> tuple[httpx.Timeout, bool, bool]:
        async with AsyncKolderaClient(token=SECRET_KEY, base_url=BASE_URL, storage_timeout=5) as client:
            storage = client._runner.storage
            timeout = storage.timeout
        return timeout, storage.is_closed, client._runner.http.is_closed

    timeout, storage_closed, api_closed = asyncio.run(scenario())
    assert timeout == httpx.Timeout(5.0)
    assert storage_closed and api_closed


def test_the_config_repr_does_not_contain_the_token() -> None:
    config = Config(token=SECRET_KEY, base_url=BASE_URL, timeout=30.0, storage_timeout=600.0, user_agent="ua")
    assert SECRET_KEY not in repr(config)
