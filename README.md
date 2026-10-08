# koldera-python

Python SDK for koldera-core, the commercially licensed, self-hosted service that checks PDFs
against the PDF/UA-1 machine rules, writes fixed copies, renders HTML and structured documents to tagged PDF, and scans
web pages. A synchronous client (`KolderaClient`) and an asynchronous client (`AsyncKolderaClient`) share one
implementation, are fully typed (`py.typed`) and run on Python 3.10 to 3.14.

Everything koldera-core reports is a **machine check**. A person still has to review the result:
`job.manual_review_required` is always `True`, and this SDK never turns a passing machine check into a statement that a
document is accessible.

`job.validation.status` is `passed`, `failed`, `error`, `not_run` or `declaration_withheld`. `declaration_withheld`
means the only failing check is the missing PDF/UA identifier (veraPDF `5-1`, still listed in `failed_rules`): it was
not written because declaring PDF/UA requires a human review. It is **not** a pass. Treat a status you do not know as
no verdict.

Status: early release (0.1). The API may change before 1.0.

## Install

```bash
pip install koldera
```

The only runtime dependency is [httpx](https://www.python-httpx.org/). The API reference (pdoc) is published to
GitHub Pages with each release and built in CI on every pull request.

## Quick start (sync)

Point the client at your koldera-core instance. `KOLDERA_URL` and `KOLDERA_TOKEN` are read from the environment when
the arguments are omitted; `base_url` and `token` take precedence.

```python
from koldera import KolderaClient

with KolderaClient() as client:  # KOLDERA_URL and KOLDERA_TOKEN
    # Upload, create a "fix" job, let the server hold the request for up to 60 s and poll if it is still running.
    job = client.pdf.create_job("report.pdf", mode="fix", wait=60, until_finished=True)
    print(job.state, job.remediation_status, job.manual_review_required)

    # Findings: machine checks, some flagged for a person.
    for finding in client.jobs.findings(job.id):
        print(finding.rule_id, finding.message, finding.needs_human_review)

    # The fixed copy. The download verifies size and SHA-256 and raises StorageError on a mismatch.
    for artifact in client.jobs.artifacts(job.id):
        if artifact.kind == "remediated":
            client.artifacts.download(artifact, "report.fixed.pdf")
```

`mode` is `"analyze"` (validate only) or `"fix"` (validate, write a separate repaired copy, validate again). Your
original file is never modified. For HTML the modes are `"generate_analyze"` and `"generate_fix"`
(`client.html_to_pdf.create_job`). The deprecated `safe_fix`, `structure_fix` and `generate_safe_fix` are not offered.

A file is bytes, a path (a `str` is a path, not text), or a binary file object.

`wait` (1 to 900 seconds) is sent as `Prefer: wait=N`. When the job is still running after the wait, the call returns
it in its real state (`job.is_finished` is `False`); set `until_finished=True` to keep polling, or call
`client.jobs.wait(job.id)` yourself. A failed job is returned, not raised: read `job.error`.

Other calls: `client.web.create_scan(url)`, `client.documents.create_job(document)`, `client.jobs.get(id)`,
`client.jobs.actions(id)`, `client.jobs.get_reading_order(id)`, `client.jobs.remediate_reading_order(id, approval)`,
`client.info()` and `client.health()`.

## Quick start (async)

The async client has the same resources with the same parameters; every method is a coroutine.

```python
import asyncio

from koldera import AsyncKolderaClient


async def main() -> None:
    async with AsyncKolderaClient() as client:  # KOLDERA_URL and KOLDERA_TOKEN
        job = await client.pdf.create_job("report.pdf", mode="analyze", wait=60, until_finished=True)
        print(job.state, job.validation)
        for finding in await client.jobs.findings(job.id):
            print(finding.rule_id, finding.needs_human_review)

        # Independent calls can run concurrently.
        scans = await asyncio.gather(
            client.web.create_scan("https://example.com", wait=30),
            client.web.create_scan("https://example.org", wait=30),
        )
        print([scan.state for scan in scans])


asyncio.run(main())
```

Large uploads and downloads are hashed in a worker thread so the event loop keeps running. Files and artifacts are held
in memory while they are uploaded, hashed and downloaded, so very large files need that much memory.

## Client sessions for browsers

A browser never gets your server token. Your backend authenticates the user, then mints a short-lived session for one
origin with limited scopes and budgets, and hands only `session_token` to the page (for example to
[koldera-js](https://github.com/koldera/koldera-js)):

```python
from koldera import KolderaClient

client = KolderaClient()  # server token from KOLDERA_TOKEN


def mint_session_for_signed_in_user() -> dict[str, str]:
    # Authenticate your user here before minting anything.
    session = client.client_sessions.create(
        allowed_origin="https://app.example",
        scopes=["jobs:create", "jobs:read"],
        max_jobs=5,
        ttl_seconds=600,
    )
    return {"sessionToken": session.session_token, "expiresAt": session.expires_at.isoformat()}
```

Origin and CORS checks on the server are a second layer, not a secret.

## Structured documents

A structured document is a dictionary that declares semantic blocks (`StructuredDocument` is a `TypedDict`, so a type
checker verifies it). Structure is declared by you, never inferred. Images are uploaded first and referenced by
`uploadId`, with either `alt` or `decorative`.

```python
from koldera import KolderaClient, StructuredDocument

with KolderaClient() as client:
    logo = client.documents.upload_image("logo.png", content_type="image/png")

    document: StructuredDocument = {
        "title": "Invoice 2026-0042",
        "language": "en",
        "page": {"size": "A4", "margins": "20mm"},
        "footer": [{"type": "pageNumber"}],
        "body": [
            {"type": "heading", "level": 1, "text": "Invoice 2026-0042"},
            {"type": "image", "uploadId": logo.upload_id, "alt": "Company logo: a blue square with the letters EC"},
            {
                "type": "table",
                "caption": "Items billed",
                "header": [["Description", "Amount"]],
                "rows": [["Consulting, 10 hours", "1000.00"]],
                "columnAlign": ["left", "right"],
            },
        ],
    }

    job = client.documents.create_job(document, wait=60, until_finished=True)
    for artifact in client.jobs.artifacts(job.id):
        if artifact.kind == "generated_pdf":
            client.artifacts.download(artifact, "invoice.pdf")
```

A rejected document raises `ValidationError` whose `errors` list `path`, `code` and `message` for each problem. The
block rules are in koldera-core's documents guide.

## Errors

Every non-2xx answer becomes a subclass of `ApiError` with the stable problem `code`, `status` and, when the server
sent one, `retry_after` (seconds). Unknown codes, states and kinds from a newer koldera-core do not crash the client:
the values are plain strings, and `jobs.wait` treats any state other than `queued` or `running` as final.

```python
import httpx

from koldera import (
    ApiError,
    JobTimeoutError,
    KolderaClient,
    RateLimitError,
    StorageError,
    ValidationError,
)

with KolderaClient() as client:
    try:
        job = client.pdf.create_job("report.pdf", mode="fix", wait=60, until_finished=True, poll_timeout=120)
    except ValidationError as error:
        for problem in error.errors:
            print(problem.path, problem.code, problem.message)
    except RateLimitError as error:
        print("retry in", error.retry_after, "seconds")
    except ApiError as error:
        print(error.status, error.code, error)
    except JobTimeoutError as error:
        print("still running:", error.job_id)
    except StorageError as error:
        print("upload or download failed:", error)
    except httpx.HTTPError as error:
        print("network failure:", error)
```

The classes are `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (404), `ConflictError`
(409), `ValidationError` (400, 422), `UnsupportedMediaTypeError` (415), `HostNotAllowedError` (421), `RateLimitError`
(429), `ServiceUnavailableError` (503), `ServerError` (other 5xx), plus `ConfigurationError` (the SDK was used wrongly;
also a `ValueError`), `JobTimeoutError` and `StorageError`. All derive from `KolderaError`. Network failures surface as
`httpx` exceptions. Error messages never contain a credential.

Creation calls carry an `Idempotency-Key` (generated, or pass `idempotency_key`). A network failure on a creation call
is repeated up to twice with the same key, so a lost response does not create a second job.

## Configuration

| Argument | Meaning |
| --- | --- |
| `base_url` | origin of koldera-core; env `KOLDERA_URL` |
| `token` | server token, any non-empty string the server accepts; env `KOLDERA_TOKEN` |
| `timeout` | per-request timeout in seconds (default 30); a `wait` stretches it to cover the wait |
| `transport` | an `httpx.BaseTransport` or `httpx.AsyncBaseTransport` for API requests (tests, custom proxies) |
| `storage_timeout` | total seconds for one upload or download through a signed storage URL (default 600) |
| `storage_transport` | transport for storage requests (tests); it must not add credentials |
| `user_agent_suffix` | product token appended to the SDK's User-Agent |

Per call on job creation: `wait`, `until_finished`, `poll_timeout`, `idempotency_key` and `on_response` (a callback
with the status, `Location`, `Preference-Applied` and `Idempotent-Replayed`).

## Compatibility

| SDK | koldera-core | Python |
| --- | --- | --- |
| 0.1.x | 0.1.x | 3.10, 3.11, 3.12, 3.13, 3.14 |

The vendored API description in [`contracts/`](contracts) records the koldera-core commit it was taken from
(`contracts/CORE_VERSION`) and the sdk-contract-kit commit it was synced from (`contracts/KIT_VERSION`), and CI checks
that the copy matches. Run koldera-core as described in its
README and deployment guide.

## Links

- [Contributing](CONTRIBUTING.md), [security policy](SECURITY.md), [changelog](CHANGELOG.md).

## Licence

This SDK is licensed under the [Apache License 2.0](LICENSE).

koldera-core is commercially licensed, self-hosted software; this SDK is Apache-2.0 and talks to it over its HTTP API.
