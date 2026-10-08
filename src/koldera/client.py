"""Synchronous client for koldera-core."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, TypeVar

import httpx

from . import _core as core
from . import _flows as flows
from .document import StructuredDocument
from .errors import StorageError
from .models import (
    Artifact,
    ClientSession,
    Finding,
    Health,
    HtmlToPdfMode,
    ImageContentType,
    Info,
    Job,
    PdfMode,
    ReadingOrderRemediation,
    ReadingOrderSnapshot,
    RemediationAction,
    ResponseMeta,
    UploadContentType,
    UploadTicket,
)

T = TypeVar("T")
FileSource = core.FileSource
OnResponse = Callable[[ResponseMeta], None] | None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class _ExpiredError(Exception):
    """The storage transfer ran past ``storage_timeout``."""


def _chunks(data: bytes, deadline: float) -> Iterator[bytes]:
    for start in range(0, len(data), 65536):
        if time.monotonic() > deadline:
            raise _ExpiredError
        yield data[start : start + 65536]


class _Runner:
    """Performs what the flows ask for with a blocking ``httpx.Client``."""

    def __init__(self, config: core.Config, transport: httpx.BaseTransport | None, storage_transport: httpx.BaseTransport | None) -> None:
        self.config = config
        self.http = httpx.Client(timeout=config.timeout, transport=transport, follow_redirects=False)
        # Signed storage URLs carry their own authority: this client never sees the caller's transport or hooks.
        self.storage = httpx.Client(timeout=httpx.Timeout(config.storage_timeout), transport=storage_transport, follow_redirects=False)

    def close(self) -> None:
        self.http.close()
        self.storage.close()

    def run(self, flow: flows.Flow[T]) -> T:
        try:
            op = next(flow)
            while True:
                try:
                    result = self._execute(op)
                except BaseException as error:
                    op = flow.throw(error)
                else:
                    op = flow.send(result)
        except StopIteration as done:
            value: T = done.value
            return value

    def _execute(self, op: flows.Op) -> object:
        if isinstance(op, flows.Call):
            return self._call(op)
        if isinstance(op, flows.Sleep):
            _sleep(op.seconds)
            return None
        if isinstance(op, flows.Read):
            return core.read_source(op.source)
        if isinstance(op, flows.Digest):
            return core.sha256_hex(op.data)
        if isinstance(op, flows.Put):
            self._put(op)
            return None
        if isinstance(op, flows.Get):
            return self._get(op)
        if isinstance(op.destination, str | Path):
            Path(op.destination).write_bytes(op.data)
        else:
            op.destination.write(op.data)
        return None

    def _call(self, op: flows.Call) -> core.CallResult:
        config = self.config
        key = core.check_key(op.key)
        core.check_wait(op.wait)
        headers = core.request_headers(config, has_body=op.body is not None, key=key, wait=op.wait, anonymous=op.anonymous)
        content = core.encode_body(op.body)
        timeout = core.request_timeout(config, op.wait)
        policy = core.RetryPolicy(method=op.method, key=key, wait=op.wait, retry_on_busy=op.retry_on_busy)
        while True:
            try:
                response = self.http.request(op.method, config.base_url + op.path, headers=headers, content=content, timeout=timeout)
            except httpx.HTTPError as error:
                pause = policy.after_transport_error(error)
                if pause is None:
                    raise core.redact(error, config.token) from None
                _sleep(pause)
                continue
            retry_after = core.parse_retry_after(response.headers.get("Retry-After"))
            if response.is_success:
                return core.decode_success(response, retry_after)
            busy = policy.after_busy(response.status_code, retry_after)
            if busy is not None:
                _sleep(busy)
                continue
            raise core.api_error(response, retry_after)

    def _put(self, op: flows.Put) -> None:
        deadline = time.monotonic() + self.config.storage_timeout
        headers = {"Content-Type": op.content_type, "Content-Length": str(len(op.data))}
        reason: str | None = None
        response: httpx.Response | None = None
        try:
            response = self.storage.put(op.url, content=_chunks(op.data, deadline), headers=headers)
        except _ExpiredError:
            reason = core.storage_timeout_reason(self.config.storage_timeout)
        except httpx.HTTPError as error:
            reason = type(error).__name__
        if response is None:
            raise core.storage_fault("upload", reason or "no response")
        if not response.is_success:
            raise core.storage_failure(response, "PUT")

    def _get(self, op: flows.Get) -> bytes:
        deadline = time.monotonic() + self.config.storage_timeout
        reason: str | None = None
        failure: Exception | None = None
        chunks: list[bytes] = []
        try:
            with self.storage.stream("GET", op.url) as response:
                if not response.is_success:
                    response.read()
                    failure = core.storage_failure(response, "GET")
                else:
                    total = 0
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise _ExpiredError
                        total += len(chunk)
                        if total > op.expected:
                            failure = StorageError(f"storage sent more than the declared {op.expected} bytes")
                            break
                        chunks.append(chunk)
        except _ExpiredError:
            reason = core.storage_timeout_reason(self.config.storage_timeout)
        except httpx.HTTPError as error:
            reason = type(error).__name__
        if reason is not None:
            raise core.storage_fault("download", reason)
        if failure is not None:
            raise failure
        return b"".join(chunks)


class KolderaClient:
    """Sync client over ``httpx``. Use it as a context manager or call :meth:`close`.

    Args:
        base_url: origin of your koldera-core server (default: ``KOLDERA_URL``).
        token: server token, any non-empty string the server accepts (default: ``KOLDERA_TOKEN``). Never ship it
            to a browser; mint a session with ``client_sessions.create`` instead.
        timeout: per-request timeout in seconds (default 30); a ``wait`` stretches it to cover the wait.
        transport: an ``httpx.BaseTransport`` for API requests (test seam, custom proxies).
        storage_timeout: total seconds for one upload or download through a signed storage URL (default 600).
        storage_transport: an ``httpx.BaseTransport`` for storage requests (test seam); it must not add credentials.
        user_agent_suffix: product token appended to the SDK's User-Agent.

    Network failures surface as ``httpx`` exceptions; everything the server or the SDK refuses is a
    :class:`koldera.KolderaError`.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        storage_timeout: float = 600.0,
        storage_transport: httpx.BaseTransport | None = None,
        user_agent_suffix: str | None = None,
    ) -> None:
        config = core.resolve_config(base_url, token, timeout, user_agent_suffix, storage_timeout)
        self._runner = _Runner(config, transport, storage_transport)
        self.uploads = Uploads(self._runner)
        self.jobs = Jobs(self._runner)
        self.pdf = Pdf(self._runner)
        self.html_to_pdf = HtmlToPdf(self._runner)
        self.web = Web(self._runner)
        self.documents = Documents(self._runner)
        self.artifacts = Artifacts(self._runner)
        self.client_sessions = ClientSessions(self._runner)

    def info(self) -> Info:
        """Version and enabled features of the server (``GET /v1/info``)."""
        return self._runner.run(flows.info())

    def health(self) -> Health:
        """Liveness probe (``GET /healthz``); sends no credential."""
        return self._runner.run(flows.health())

    def close(self) -> None:
        self._runner.close()

    def __enter__(self) -> KolderaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"KolderaClient(base_url={self._runner.config.base_url!r}, token='***')"


class Uploads:
    def __init__(self, runner: _Runner) -> None:
        self._r = runner

    def upload(
        self,
        source: FileSource,
        content_type: UploadContentType,
        file_name: str,
        *,
        idempotency_key: str | None = None,
        on_response: OnResponse = None,
    ) -> UploadTicket:
        """Issue a ticket and PUT the bytes to storage (no bearer on the PUT). Returns the consumed ticket.

        ``source`` is bytes, a binary file object or a path (a ``str`` is a path).
        """
        return self._r.run(flows.upload_file(source, content_type, file_name, idempotency_key, on_response))


class Pdf:
    def __init__(self, runner: _Runner) -> None:
        self._r = runner

    def create_job(
        self,
        file: FileSource,
        *,
        mode: PdfMode,
        explicit_language: str | None = None,
        explicit_title: str | None = None,
        file_name: str | None = None,
        wait: int | None = None,
        until_finished: bool = False,
        poll_timeout: float = 600.0,
        idempotency_key: str | None = None,
        on_response: OnResponse = None,
    ) -> Job:
        """Upload a PDF and queue ``analyze`` or ``fix``.

        ``explicit_language`` (BCP 47) is used only when the document has none. ``explicit_title`` (1-500 printable
        characters, trimmed) replaces an existing title. Both are validated before any request.

        ``wait`` (1-900 seconds) asks the server to hold the request: a finished job comes back as it is, a job
        still running comes back unfinished (check ``job.is_finished``). Set ``until_finished`` to keep polling
        until it ends (``poll_timeout`` seconds at most), or call ``jobs.wait``.
        """
        return self._r.run(
            flows.pdf_job(
                file, mode, explicit_language, explicit_title, file_name, wait, until_finished, poll_timeout, idempotency_key, on_response
            )
        )


class HtmlToPdf:
    def __init__(self, runner: _Runner) -> None:
        self._r = runner

    def create_job(
        self,
        file: FileSource,
        *,
        mode: HtmlToPdfMode,
        explicit_language: str | None = None,
        explicit_title: str | None = None,
        content_selector: str | None = None,
        file_name: str | None = None,
        wait: int | None = None,
        until_finished: bool = False,
        poll_timeout: float = 600.0,
        idempotency_key: str | None = None,
        on_response: OnResponse = None,
    ) -> Job:
        """Upload one self-contained static HTML file and queue the render, inspect and validate pipeline.

        Never a URL. ``content_selector`` (a CSS selector, 1-200 printable characters) prints only the matching
        elements. ``wait`` and ``until_finished`` work as in :meth:`Pdf.create_job`.
        """
        return self._r.run(
            flows.html_job(
                file,
                mode,
                explicit_language,
                explicit_title,
                content_selector,
                file_name,
                wait,
                until_finished,
                poll_timeout,
                idempotency_key,
                on_response,
            )
        )


class Web:
    def __init__(self, runner: _Runner) -> None:
        self._r = runner

    def create_scan(
        self,
        url: str,
        *,
        wait: int | None = None,
        until_finished: bool = False,
        poll_timeout: float = 600.0,
        idempotency_key: str | None = None,
        on_response: OnResponse = None,
    ) -> Job:
        """Queue one automated scan of a public http(s) URL."""
        return self._r.run(flows.web_scan(url, wait, until_finished, poll_timeout, idempotency_key, on_response))


class Documents:
    def __init__(self, runner: _Runner) -> None:
        self._r = runner

    def create_job(
        self,
        document: StructuredDocument,
        *,
        wait: int | None = None,
        until_finished: bool = False,
        poll_timeout: float = 600.0,
        idempotency_key: str | None = None,
        on_response: OnResponse = None,
    ) -> Job:
        """Queue a structured document for rendering to a tagged PDF.

        Images are referenced by the ``upload_id`` of :meth:`upload_image` (``uploadId`` in the document).
        """
        return self._r.run(flows.document_job(document, wait, until_finished, poll_timeout, idempotency_key, on_response))

    def upload_image(
        self,
        source: FileSource,
        *,
        content_type: ImageContentType | None = None,
        file_name: str | None = None,
        idempotency_key: str | None = None,
        on_response: OnResponse = None,
    ) -> UploadTicket:
        """Upload one PNG, JPEG or SVG image for an ``image`` block.

        The type is ``content_type`` or, when absent, the one the file name's extension names.
        """
        return self._r.run(flows.image_upload(source, content_type, file_name, idempotency_key, on_response))


class Jobs:
    def __init__(self, runner: _Runner) -> None:
        self._r = runner

    def get(self, job_id: str) -> Job:
        return self._r.run(flows.get_job(job_id))

    def wait(
        self, job_id: str, *, timeout: float = 600.0, initial_delay: float = 1.0, on_update: Callable[[Job], None] | None = None
    ) -> Job:
        """Poll until the job is no longer ``queued`` or ``running`` and return it.

        A failed job is returned, not raised: read ``job.error``. Honours ``Retry-After``, otherwise backs off
        from ``initial_delay`` up to 10 s. Raises :class:`koldera.JobTimeoutError` when the next pause would pass
        ``timeout`` seconds; the job keeps running on the server.
        """
        return self._r.run(flows.wait_for_job(job_id, timeout, initial_delay, on_update))

    def findings(self, job_id: str) -> list[Finding]:
        return self._r.run(flows.findings(job_id))

    def actions(self, job_id: str) -> list[RemediationAction]:
        """What each repair rule did on a ``fix`` job (applied, skipped, failed, rolled_back, with a reason)."""
        return self._r.run(flows.actions(job_id))

    def artifacts(self, job_id: str) -> list[Artifact]:
        """Files of the job with fresh signed download URLs."""
        return self._r.run(flows.artifacts(job_id))

    def get_reading_order(self, job_id: str) -> ReadingOrderSnapshot:
        """Reading-order evidence bound to the artifact hash; ``review_required`` is always true."""
        return self._r.run(flows.reading_order(job_id))

    def remediate_reading_order(
        self,
        job_id: str,
        approval: ReadingOrderRemediation,
        *,
        wait: int | None = None,
        until_finished: bool = False,
        poll_timeout: float = 600.0,
        idempotency_key: str | None = None,
        on_response: OnResponse = None,
    ) -> Job:
        """Queue a correction a person approved.

        The complete children of one parent in the new order, bound to the snapshot's artifact hash and version.
        ``approval.acknowledged`` must be True. A :class:`koldera.ConflictError` (``STALE_READING_ORDER_SNAPSHOT``)
        means reload the snapshot and review again; a :class:`koldera.ValidationError` (``ORDER_FIX_UNSUPPORTED``)
        means the plan cannot be applied safely.
        """
        return self._r.run(flows.remediate(job_id, approval, wait, until_finished, poll_timeout, idempotency_key, on_response))


class Artifacts:
    def __init__(self, runner: _Runner) -> None:
        self._r = runner

    def download(self, artifact: Artifact, destination: BinaryIO | Path | str | None = None) -> bytes:
        """Fetch through the signed URL (no bearer) and verify size and SHA-256 before anything is returned or written.

        Raises :class:`koldera.StorageError` on a wrong size or hash; the bytes are then discarded.
        """
        return self._r.run(flows.download(artifact, destination))


class ClientSessions:
    def __init__(self, runner: _Runner) -> None:
        self._r = runner

    def create(
        self,
        *,
        allowed_origin: str,
        scopes: Iterable[str] | None = None,
        max_jobs: int | None = None,
        max_uploads: int | None = None,
        ttl_seconds: int | None = None,
    ) -> ClientSession:
        """Mint a scoped, short-lived session for an end user you authenticated (call it from your backend only).

        ``scopes`` are ``jobs:create`` and ``jobs:read`` (both by default). Hand ``session_token`` to the browser.
        """
        return self._r.run(
            flows.client_session(allowed_origin, None if scopes is None else list(scopes), max_jobs, max_uploads, ttl_seconds)
        )
