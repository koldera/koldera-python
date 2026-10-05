"""Every operation of the SDK as a generator that yields what it needs done and receives the result.

The sync and async clients only run these flows (they perform the HTTP calls, sleeps, and file access), so the
behaviour that matters (idempotency, wait handling, polling, verified downloads) exists exactly once.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TypeVar

from . import _core as core
from .errors import ConfigurationError, JobTimeoutError, StorageError
from .models import (
    Artifact,
    ClientSession,
    Finding,
    Health,
    Info,
    Job,
    ReadingOrderRemediation,
    ReadingOrderSnapshot,
    RemediationAction,
    ResponseMeta,
    UploadTicket,
)

T = TypeVar("T")
OnResponse = Callable[[ResponseMeta], None] | None
OnUpdate = Callable[[Job], None] | None


@dataclass(frozen=True, slots=True)
class Call:
    method: str
    path: str
    body: Any = None
    key: str | None = None
    wait: int | None = None
    anonymous: bool = False
    retry_on_busy: bool = False


@dataclass(frozen=True, slots=True)
class Put:
    url: str
    data: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class Get:
    url: str
    expected: int


@dataclass(frozen=True, slots=True)
class Sleep:
    seconds: float


@dataclass(frozen=True, slots=True)
class Read:
    source: core.FileSource


@dataclass(frozen=True, slots=True)
class Digest:
    """SHA-256 of some bytes; the async client hashes large payloads in a worker thread."""

    data: bytes


@dataclass(frozen=True, slots=True)
class Write:
    destination: BinaryIO | Path | str
    data: bytes


Op = Call | Put | Get | Sleep | Read | Digest | Write
Flow = Generator[Op, Any, T]


def _notify(on_response: OnResponse, meta: ResponseMeta) -> None:
    if on_response is not None:
        on_response(meta)


def _read(path: str) -> Flow[Any]:
    result: core.CallResult = yield Call("GET", path, retry_on_busy=True)
    return result


def upload(source: core.FileSource, content_type: str, file_name: str, key: str, on_response: OnResponse) -> Flow[UploadTicket]:
    """Issue a ticket, then PUT the bytes to storage (no bearer). Returns the consumed ticket."""
    core.check_upload(content_type, file_name)
    data: bytes = yield Read(source)
    sha256: str = yield Digest(data)
    result: core.CallResult = yield Call("POST", "/v1/uploads", core.upload_body(file_name, len(data), sha256, content_type), key)
    _notify(on_response, result.meta)
    ticket = core.parse(UploadTicket.from_json, result.value, result.status)
    if ticket.max_bytes is not None and len(data) > ticket.max_bytes:
        raise ConfigurationError(f"the file ({len(data)} bytes) exceeds the ticket limit of {ticket.max_bytes} bytes")
    try:
        yield Put(ticket.upload_url, data, content_type)
    except StorageError as error:
        # A replayed ticket belongs to an earlier attempt whose PUT already stored these exact bytes
        # (size and hash are part of the replayed request).
        stored = error.status == 409 and error.code == "UPLOAD_ALREADY_COMPLETED"
        if not (result.meta.idempotent_replayed and stored):
            raise
    return ticket


def wait_for_job(job_id: str, timeout: float, initial_delay: float, on_update: OnUpdate) -> Flow[Job]:
    """Poll until the job is no longer queued or running. A failed job is returned, not raised."""
    path = f"/v1/jobs/{core.job_path(job_id)}"
    started = time.monotonic()
    delay = initial_delay
    while True:
        result: core.CallResult = yield from _read(path)
        job = core.parse(Job.from_json, result.value, result.status)
        if on_update is not None:
            on_update(job)
        if job.is_finished:
            return job
        pause = result.retry_after if result.retry_after is not None else delay
        if time.monotonic() - started + pause > timeout:
            raise JobTimeoutError(job_id, timeout)
        yield Sleep(pause)
        delay = min(delay * 2, core.MAX_POLL_DELAY)


def create_job(
    path: str, body: Any, key: str, wait: int | None, until_finished: bool, poll_timeout: float, on_response: OnResponse
) -> Flow[Job]:
    """POST a job request. A 200 (finished) and a 202 (still running) are returned as they are; the job is polled
    to its end only when ``until_finished`` asks for it."""
    result: core.CallResult = yield Call("POST", path, body, key, wait)
    _notify(on_response, result.meta)
    job = core.parse(Job.from_json, result.value, result.status)
    if until_finished and not job.is_finished:
        return (yield from wait_for_job(job.id, poll_timeout, 1.0, None))
    return job


def pdf_job(
    file: core.FileSource,
    mode: str,
    explicit_language: str | None,
    explicit_title: str | None,
    file_name: str | None,
    wait: int | None,
    until_finished: bool,
    poll_timeout: float,
    idempotency_key: str | None,
    on_response: OnResponse,
) -> Flow[Job]:
    core.check_mode(mode, core.PDF_MODES)
    core.check_language(explicit_language)
    title = core.check_title(explicit_title)
    core.check_wait(wait)
    key = core.key_or_new(idempotency_key)
    name = file_name or core.name_of(file, "document.pdf")
    ticket = yield from upload(file, "application/pdf", name, core.derived_key(key, "upload"), None)
    body = core.pdf_job_body(ticket.upload_id, mode, explicit_language, title)
    return (yield from create_job("/v1/pdf/jobs", body, key, wait, until_finished, poll_timeout, on_response))


def html_job(
    file: core.FileSource,
    mode: str,
    explicit_language: str | None,
    explicit_title: str | None,
    content_selector: str | None,
    file_name: str | None,
    wait: int | None,
    until_finished: bool,
    poll_timeout: float,
    idempotency_key: str | None,
    on_response: OnResponse,
) -> Flow[Job]:
    core.check_mode(mode, core.HTML_MODES)
    core.check_language(explicit_language)
    title = core.check_title(explicit_title)
    selector = core.check_selector(content_selector)
    core.check_wait(wait)
    key = core.key_or_new(idempotency_key)
    name = file_name or core.name_of(file, "page.html")
    ticket = yield from upload(file, "text/html", name, core.derived_key(key, "upload"), None)
    body = core.html_job_body(ticket.upload_id, mode, explicit_language, title, selector)
    return (yield from create_job("/v1/html-to-pdf/jobs", body, key, wait, until_finished, poll_timeout, on_response))


def web_scan(
    url: str, wait: int | None, until_finished: bool, poll_timeout: float, idempotency_key: str | None, on_response: OnResponse
) -> Flow[Job]:
    checked = core.check_scan_url(url)
    core.check_wait(wait)
    key = core.key_or_new(idempotency_key)
    return (yield from create_job("/v1/web/jobs", {"url": checked}, key, wait, until_finished, poll_timeout, on_response))


def document_job(
    document: Mapping[str, Any],
    wait: int | None,
    until_finished: bool,
    poll_timeout: float,
    idempotency_key: str | None,
    on_response: OnResponse,
) -> Flow[Job]:
    core.check_document(document)
    core.check_wait(wait)
    key = core.key_or_new(idempotency_key)
    return (yield from create_job("/v1/documents/jobs", document, key, wait, until_finished, poll_timeout, on_response))


def image_upload(
    source: core.FileSource, content_type: str | None, file_name: str | None, idempotency_key: str | None, on_response: OnResponse
) -> Flow[UploadTicket]:
    chosen = core.image_type_of(source, content_type, file_name)
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg"}[chosen]
    name = file_name or core.name_of(source, "image" + suffix)
    return (yield from upload(source, chosen, name, core.key_or_new(idempotency_key), on_response))


def upload_file(
    source: core.FileSource, content_type: str, file_name: str, idempotency_key: str | None, on_response: OnResponse
) -> Flow[UploadTicket]:
    return (yield from upload(source, content_type, file_name, core.key_or_new(idempotency_key), on_response))


def get_job(job_id: str) -> Flow[Job]:
    result: core.CallResult = yield from _read(f"/v1/jobs/{core.job_path(job_id)}")
    return core.parse(Job.from_json, result.value, result.status)


def findings(job_id: str) -> Flow[list[Finding]]:
    result: core.CallResult = yield from _read(f"/v1/jobs/{core.job_path(job_id)}/findings")
    return core.parse(lambda v: [Finding.from_json(i) for i in v["items"]], result.value, result.status)


def actions(job_id: str) -> Flow[list[RemediationAction]]:
    result: core.CallResult = yield from _read(f"/v1/jobs/{core.job_path(job_id)}/actions")
    return core.parse(lambda v: [RemediationAction.from_json(i) for i in v["items"]], result.value, result.status)


def artifacts(job_id: str) -> Flow[list[Artifact]]:
    result: core.CallResult = yield from _read(f"/v1/jobs/{core.job_path(job_id)}/artifacts")
    return core.parse(lambda v: [Artifact.from_json(i) for i in v["items"]], result.value, result.status)


def reading_order(job_id: str) -> Flow[ReadingOrderSnapshot]:
    result: core.CallResult = yield from _read(f"/v1/jobs/{core.job_path(job_id)}/reading-order")
    return core.parse(ReadingOrderSnapshot.from_json, result.value, result.status)


def remediate(
    job_id: str,
    approval: ReadingOrderRemediation,
    wait: int | None,
    until_finished: bool,
    poll_timeout: float,
    idempotency_key: str | None,
    on_response: OnResponse,
) -> Flow[Job]:
    path = f"/v1/jobs/{core.job_path(job_id)}/reading-order/remediations"
    body = core.remediation_body(approval)
    core.check_wait(wait)
    key = core.key_or_new(idempotency_key)
    return (yield from create_job(path, body, key, wait, until_finished, poll_timeout, on_response))


def download(artifact: Artifact, destination: BinaryIO | Path | str | None) -> Flow[bytes]:
    """Fetch through the signed URL, verify size and SHA-256, and only then write or return anything."""
    data: bytes = yield Get(artifact.download_url, artifact.size_bytes)
    if len(data) != artifact.size_bytes:
        raise StorageError(f"artifact {artifact.kind}: received {len(data)} bytes, expected {artifact.size_bytes}")
    digest: str = yield Digest(data)
    if digest != artifact.sha256.lower():
        raise StorageError(f"artifact {artifact.kind}: SHA-256 mismatch")
    if destination is not None:
        yield Write(destination, data)
    return data


def client_session(
    allowed_origin: str, scopes: list[str] | None, max_jobs: int | None, max_uploads: int | None, ttl_seconds: int | None
) -> Flow[ClientSession]:
    body = core.session_body(allowed_origin, scopes, max_jobs, max_uploads, ttl_seconds)
    result: core.CallResult = yield Call("POST", "/v1/client-sessions", body)
    return core.parse(ClientSession.from_json, result.value, result.status)


def info() -> Flow[Info]:
    result: core.CallResult = yield from _read("/v1/info")
    return core.parse(Info.from_json, result.value, result.status)


def health() -> Flow[Health]:
    result: core.CallResult = yield Call("GET", "/healthz", anonymous=True, retry_on_busy=True)
    return core.parse(Health.from_json, result.value, result.status)
