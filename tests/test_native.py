"""Behaviour that differs between the clients, and the typed models and document model."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

import koldera
import koldera.async_client as async_module
from koldera import (
    AsyncKolderaClient,
    ConfigurationError,
    Job,
    KolderaClient,
    KolderaError,
    StructuredDocument,
    is_finished,
    new_idempotency_key,
)
from support import BASE_URL, JOB_ID, PDF_BYTES, SECRET_KEY, Golden, Reply, body_of, job_json


def test_sync_client_is_a_context_manager_that_closes_its_http_client(golden: Golden) -> None:
    golden.expect("info")
    with KolderaClient(token=SECRET_KEY, base_url=BASE_URL) as client:
        assert client.info().version == "0.1.0"
    with pytest.raises(RuntimeError):
        client.info()


def test_async_client_is_an_async_context_manager(golden: Golden) -> None:
    async def scenario() -> None:
        golden.expect("info", "healthz")
        async with AsyncKolderaClient(token=SECRET_KEY, base_url=BASE_URL) as client:
            assert (await client.info()).features == ("document", "html", "pdf", "web")
            assert (await client.health()).status == "ok"
        with pytest.raises(RuntimeError):
            await client.info()

    asyncio.run(scenario())


def test_async_calls_can_run_concurrently(golden: Golden) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=job_json(id=request.url.path.rsplit("/", 1)[1], state="completed"))

    async def scenario() -> list[Job]:
        golden.default = respond
        ids = [f"4d2d5a6e-2b1c-4e4a-9d6b-1f3c5e7a9b{n:02d}" for n in range(10)]
        async with AsyncKolderaClient(token=SECRET_KEY, base_url=BASE_URL) as client:
            return list(await asyncio.gather(*(client.jobs.get(i) for i in ids)))

    jobs = asyncio.run(scenario())
    assert len({j.id for j in jobs}) == 10


def test_cancelling_an_async_wait_stops_polling(golden: Golden, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()

    async def stuck(seconds: float) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(async_module, "_sleep", stuck)

    async def scenario() -> None:
        golden.expect("job-queued-retry-after")
        async with AsyncKolderaClient(token=SECRET_KEY, base_url=BASE_URL) as client:
            task = asyncio.ensure_future(client.jobs.wait(JOB_ID))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert len(golden.exchanges) == 1


def test_async_upload_reads_a_path_without_blocking_the_loop(golden: Golden, tmp_path: Any) -> None:
    path = tmp_path / "sample.pdf"
    path.write_bytes(PDF_BYTES)

    async def scenario() -> Job:
        golden.expect("upload-ticket-pdf", "upload-put-pdf", "pdf-job-accepted")
        async with AsyncKolderaClient(token=SECRET_KEY, base_url=BASE_URL) as client:
            return await client.pdf.create_job(path, mode="analyze")

    assert asyncio.run(scenario()).state == "queued"


def test_a_client_built_with_an_injected_transport_uses_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    with KolderaClient(token=SECRET_KEY, base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        assert client.health().status == "ok"


def test_every_error_derives_from_one_base_class() -> None:
    for name in ["ApiError", "AuthenticationError", "ConfigurationError", "JobTimeoutError", "StorageError", "ServiceUnavailableError"]:
        assert issubclass(getattr(koldera, name), KolderaError)
    assert issubclass(ConfigurationError, ValueError)


def test_idempotency_keys_are_32_hex_characters_and_unique() -> None:
    keys = {new_idempotency_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(len(k) == 32 and int(k, 16) >= 0 for k in keys)


def test_job_parses_timestamps_with_any_fraction_length_and_offsets() -> None:
    for text in ["2026-10-05T10:00:00Z", "2026-10-05T10:00:00.1Z", "2026-10-05T10:00:00.1234567Z", "2026-10-05T12:00:00+02:00"]:
        job = Job.from_json(job_json(createdAt=text))
        assert job.created_at.tzinfo is not None
        assert job.created_at.astimezone(timezone.utc).replace(microsecond=0) == datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


def test_is_finished_is_false_only_for_queued_and_running() -> None:
    states = {"queued": False, "running": False, "completed": True, "failed": True, "cancelled": True, "something_new": True}
    for state, finished in states.items():
        assert is_finished(Job.from_json(job_json(state=state))) is finished


def test_job_model_exposes_machine_validation_and_never_a_conformance_field() -> None:
    validation = {"profile": "pdfua1", "status": "failed", "failedRules": [{"ruleId": "7.1-3", "count": 2}]}
    job = Job.from_json(job_json(validation=validation))
    assert job.validation is not None
    assert [(r.rule_id, r.count) for r in job.validation.failed_rules] == [("7.1-3", 2)]
    assert job.manual_review_required is True
    assert not [name for name in dir(job) if "conform" in name]


def test_the_document_model_types_the_golden_document() -> None:
    document: StructuredDocument = body_of("document-job-accepted")
    assert document["body"][0]["type"] == "heading"
    typed: StructuredDocument = {
        "title": "Report",
        "language": "en",
        "page": {"size": "A4", "orientation": "portrait", "margins": "20mm"},
        "theme": {"fontSize": 11, "accent": "#112233"},
        "header": [{"type": "paragraph", "content": ["Header"]}],
        "footer": [{"type": "pageNumber"}],
        "body": [
            {"type": "heading", "level": 1, "text": "Report"},
            {
                "type": "paragraph",
                "content": ["a ", {"strong": "b"}, {"emphasis": "c"}, {"link": {"text": "d", "url": "https://example.com"}}],
                "lang": "en",
            },
            {"type": "list", "ordered": True, "items": [["one", {"list": {"items": [["nested"]]}}], ["two"]]},
            {
                "type": "table",
                "caption": "T",
                "header": [["A", "B"]],
                "rows": [["1", "2"]],
                "rowHeaders": False,
                "columnAlign": ["left", "right"],
            },
            {"type": "image", "uploadId": "33333333-3333-4333-8333-333333333333", "alt": "Logo"},
            {"type": "image", "uploadId": "33333333-3333-4333-8333-333333333333", "decorative": True},
            {"type": "link", "text": "x", "url": "https://example.com"},
            {"type": "pageBreak"},
            {"type": "spacer", "height": "8mm"},
            {"type": "keyValue", "rows": [("k", "v")]},
        ],
    }
    assert typed["language"] == "en"


def test_a_typed_document_is_posted_as_is(golden: Golden) -> None:
    golden.expect(Reply(202, job_json(kind="document_to_pdf")))
    document: StructuredDocument = {"title": "T", "language": "en", "body": [{"type": "heading", "level": 1, "text": "T"}]}
    with KolderaClient(token=SECRET_KEY, base_url=BASE_URL) as client:
        client.documents.create_job(document)
    assert golden.exchanges[0].json == document


def _big_artifact(size: int) -> tuple[bytes, Any]:
    import hashlib

    from koldera import Artifact

    data = bytes(range(256)) * (size // 256)
    artifact = Artifact(
        id="a",
        kind="remediated",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        download_url="https://storage.test/a",
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    return data, artifact


def test_async_hashes_large_data_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    import koldera._core as core

    data, artifact = _big_artifact(1024 * 1024)
    threads: list[int] = []
    real = core.sha256_hex

    def spy(payload: bytes) -> str:
        threads.append(threading.get_ident())
        return real(payload)

    monkeypatch.setattr(core, "sha256_hex", spy)

    async def scenario() -> bytes:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=data))
        async with AsyncKolderaClient(token=SECRET_KEY, base_url=BASE_URL, storage_transport=transport) as client:
            return await client.artifacts.download(artifact)

    assert asyncio.run(scenario()) == data
    assert threads
    assert threading.get_ident() not in threads


def test_async_hashes_small_data_inline_and_sync_always_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    import koldera._core as core

    data, artifact = _big_artifact(1024)
    threads: list[int] = []
    real = core.sha256_hex

    def spy(payload: bytes) -> str:
        threads.append(threading.get_ident())
        return real(payload)

    monkeypatch.setattr(core, "sha256_hex", spy)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=data))
    with KolderaClient(token=SECRET_KEY, base_url=BASE_URL, storage_transport=transport) as client:
        assert client.artifacts.download(artifact) == data
    assert threads == [threading.get_ident()]


def test_redact_keeps_the_request_on_the_redacted_error() -> None:
    from koldera._core import redact

    request = httpx.Request("GET", "http://core.test/v1/info")
    error = httpx.ConnectError("cannot connect with test-server-token", request=request)
    clean = redact(error, "test-server-token")
    assert "test-server-token" not in str(clean)
    assert clean.request is request


def test_redact_without_a_request_still_works() -> None:
    from koldera._core import redact

    clean = redact(httpx.ConnectError("cannot connect with test-server-token"), "test-server-token")
    assert "test-server-token" not in str(clean)


def _shape(function: Any) -> list[tuple[str, Any, Any, Any]]:
    import inspect

    return [(p.name, p.kind, p.default, p.annotation) for p in inspect.signature(function).parameters.values()]


def test_sync_and_async_resources_have_the_same_parameters() -> None:
    import inspect

    import koldera.async_client as a
    import koldera.client as s

    checked = 0
    for name in ("Uploads", "Pdf", "HtmlToPdf", "Web", "Documents", "Jobs", "Artifacts", "ClientSessions"):
        sync_cls, async_cls = getattr(s, name), getattr(a, name)
        methods = [m for m, v in vars(sync_cls).items() if callable(v) and not m.startswith("_")]
        assert methods, name
        assert methods == [m for m, v in vars(async_cls).items() if callable(v) and not m.startswith("_")], name
        for method in methods:
            assert _shape(getattr(sync_cls, method)) == _shape(getattr(async_cls, method)), f"{name}.{method}"
            assert inspect.iscoroutinefunction(getattr(async_cls, method)), f"{name}.{method}"
            checked += 1
    assert checked >= 15
    for method in ("info", "health"):
        assert _shape(getattr(s.KolderaClient, method)) == _shape(getattr(a.AsyncKolderaClient, method))


def test_the_package_version_matches_pyproject() -> None:
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf8")
    match = re.search(r'^version = "([^"]+)"', text, flags=re.M)
    assert match is not None
    assert koldera.__version__ == match.group(1)
