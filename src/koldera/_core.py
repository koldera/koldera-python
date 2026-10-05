"""Validation, request bodies, response decoding and retry policy shared by the sync and async clients."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import IO, Any, BinaryIO, TypeVar

import httpx

from .errors import ConfigurationError, Problem, ServerError, StorageError, error_from_problem
from .models import ReadingOrderRemediation, ResponseMeta, problem_errors

SDK_NAME = "koldera-sdk-python"
SDK_VERSION = "0.1.0"
MAX_POLL_DELAY = 10.0
MAX_BUSY_RETRIES = 2
MAX_NETWORK_RETRIES = 2
NETWORK_RETRY_DELAY = 0.25

IMAGE_TYPES = ("image/png", "image/jpeg", "image/svg+xml")
UPLOAD_TYPES = ("application/pdf", "text/html", *IMAGE_TYPES)
PDF_MODES = ("analyze", "fix")
HTML_MODES = ("generate_analyze", "generate_fix")
_IMAGE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_IDEMPOTENCY = re.compile(r"^[\x20-\x7e]{1,255}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_ORIGIN = re.compile(r"^https?://[^/\s]+$")
_HTTP_URL = re.compile(r"^https?://[^/\s]+", re.IGNORECASE)

FileSource = bytes | bytearray | memoryview | BinaryIO | IO[bytes] | Path | str
T = TypeVar("T")


def new_idempotency_key() -> str:
    """32 hex characters from the CSPRNG; one per creation call unless the caller supplies a key."""
    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    base_url: str
    timeout: float
    user_agent: str


def resolve_config(base_url: str | None, token: str | None, timeout: float, user_agent_suffix: str | None) -> Config:
    secret = token if token is not None else os.environ.get("KOLDERA_TOKEN", "")
    if not isinstance(secret, str) or not secret.strip():
        raise ConfigurationError("token is required: pass token or set KOLDERA_TOKEN")
    base = base_url if base_url is not None else os.environ.get("KOLDERA_URL", "")
    if not base:
        raise ConfigurationError("base_url is required: pass base_url or set KOLDERA_URL")
    if not _HTTP_URL.match(base):
        raise ConfigurationError("base_url must be an absolute http(s) URL")
    if not timeout > 0:
        raise ConfigurationError("timeout must be positive")
    agent = f"{SDK_NAME}/{SDK_VERSION}" + (f" {user_agent_suffix}" if user_agent_suffix else "")
    return Config(secret, base.rstrip("/"), float(timeout), agent)


# --- argument validation ---------------------------------------------------------------------------------------


def check_key(key: str | None) -> str | None:
    """The key to send (trimmed), None when absent. 1-255 printable ASCII characters, not blank."""
    if key is None:
        return None
    trimmed = key.strip() if isinstance(key, str) and _IDEMPOTENCY.fullmatch(key) else ""
    if not trimmed:
        raise ConfigurationError("idempotency_key must be 1-255 printable ASCII characters and not blank")
    return trimmed


def key_or_new(key: str | None) -> str:
    return check_key(key) or new_idempotency_key()


def derived_key(key: str, purpose: str) -> str:
    """A key for a companion request of one logical call (the upload before a job), within the 255-character limit."""
    return f"{key[:240]}:{purpose}"


def check_wait(wait: int | None) -> None:
    if wait is not None and not (isinstance(wait, int) and not isinstance(wait, bool) and 1 <= wait <= 900):
        raise ConfigurationError("wait must be a whole number of seconds from 1 to 900")


def check_mode(mode: str, allowed: tuple[str, ...]) -> None:
    if mode not in allowed:
        raise ConfigurationError(f"mode must be one of {', '.join(allowed)}")


def check_language(tag: str | None) -> None:
    if tag is not None and not (isinstance(tag, str) and _LANGUAGE.fullmatch(tag)):
        raise ConfigurationError("explicit_language must be a well-formed BCP 47 tag")


def _has_control(text: str) -> bool:
    return any(unicodedata.category(c) == "Cc" for c in text)


def check_title(title: str | None) -> str | None:
    """Trimmed, 1-500 printable characters (counted as code points, like the API), or None when absent."""
    if title is None:
        return None
    trimmed = title.strip() if isinstance(title, str) else ""
    if not trimmed or len(trimmed) > 500 or _has_control(trimmed):
        raise ConfigurationError("explicit_title must be 1-500 printable characters")
    return trimmed


def check_selector(selector: str | None) -> str | None:
    """Bounded and printable here; whether it is valid CSS only the renderer can tell."""
    if selector is None:
        return None
    trimmed = selector.strip() if isinstance(selector, str) else ""
    if not trimmed or len(trimmed) > 200 or _has_control(trimmed):
        raise ConfigurationError("content_selector must be 1-200 printable characters (a CSS selector)")
    return trimmed


def check_reviewer(reviewer: str | None) -> str | None:
    if reviewer is None:
        return None
    trimmed = reviewer.strip() if isinstance(reviewer, str) else ""
    if not trimmed or len(trimmed) > 200 or _has_control(trimmed):
        raise ConfigurationError("reviewer must be 1-200 printable characters")
    return trimmed


def check_scan_url(url: str) -> str:
    if not isinstance(url, str) or not _HTTP_URL.match(url):
        raise ConfigurationError("url must be an absolute http(s) URL")
    return url


def check_origin(origin: str) -> str:
    if not isinstance(origin, str) or not _ORIGIN.fullmatch(origin):
        raise ConfigurationError("allowed_origin must be an absolute http(s) origin without a path")
    return origin


def job_path(job_id: str) -> str:
    if not isinstance(job_id, str) or not _UUID.fullmatch(job_id):
        raise ConfigurationError("job_id must be a UUID")
    return job_id.lower()


def check_upload(content_type: str, file_name: str) -> None:
    if content_type not in UPLOAD_TYPES:
        raise ConfigurationError(f"content_type must be one of {', '.join(UPLOAD_TYPES)}")
    if not isinstance(file_name, str) or not 1 <= len(file_name) <= 255:
        raise ConfigurationError("file_name must be 1-255 characters")


def check_document(document: Mapping[str, Any]) -> None:
    if not isinstance(document, Mapping):
        raise ConfigurationError("document must be a structured document object")


def image_type_of(source: FileSource, content_type: str | None, file_name: str | None) -> str:
    """The explicit type, else the one the file name's extension names; always an image type."""
    chosen = content_type
    if chosen is None:
        for candidate in (file_name, source if isinstance(source, str | Path) else None, getattr(source, "name", None)):
            if isinstance(candidate, str | Path):
                chosen = _IMAGE_EXTENSIONS.get(Path(candidate).suffix.lower())
                if chosen:
                    break
    if chosen not in IMAGE_TYPES:
        raise ConfigurationError(f"image content_type must be one of {', '.join(IMAGE_TYPES)}")
    return chosen


def name_of(source: FileSource, default: str) -> str:
    if isinstance(source, str | Path):
        return Path(source).name or default
    name = getattr(source, "name", None)
    return Path(name).name if isinstance(name, str) and name else default


#: Payloads at or above this size are hashed in a worker thread by the async client.
OFFLOAD_HASH_BYTES = 256 * 1024


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_source(source: FileSource) -> bytes:
    """The bytes to upload, from bytes-like objects, a path (``str`` is a path) or a binary file object."""
    if isinstance(source, bytes | bytearray | memoryview):
        data = bytes(source)
    elif isinstance(source, str | Path):
        data = Path(source).read_bytes()
    elif hasattr(source, "read"):
        if getattr(source, "closed", False):
            raise ValueError("I/O operation on closed file")
        content = source.read()
        if isinstance(content, str):
            raise ConfigurationError("open the file in binary mode ('rb')")
        data = bytes(content)
    else:
        raise ConfigurationError("file must be bytes, a path or a binary file object")
    if not data:
        raise ConfigurationError("the file is empty")
    return data


# --- request bodies (absent optional fields are omitted, never null) ----------------------------------------------


def compact(body: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


def upload_body(file_name: str, size: int, sha256: str, content_type: str) -> dict[str, Any]:
    return {"fileName": file_name, "sizeBytes": size, "sha256": sha256, "contentType": content_type}


def pdf_job_body(upload_id: str, mode: str, language: str | None, title: str | None) -> dict[str, Any]:
    return compact({"uploadId": upload_id, "mode": mode, "profile": "ua1", "explicitLanguage": language, "explicitTitle": title})


def html_job_body(upload_id: str, mode: str, language: str | None, title: str | None, selector: str | None) -> dict[str, Any]:
    return compact(
        {
            "uploadId": upload_id,
            "mode": mode,
            "profile": "ua1",
            "contentSelector": selector,
            "explicitLanguage": language,
            "explicitTitle": title,
        }
    )


def remediation_body(approval: ReadingOrderRemediation) -> dict[str, Any]:
    if approval.acknowledged is not True:
        raise ConfigurationError("a reading-order correction must be acknowledged by the reviewer (acknowledged=True)")
    ids = list(approval.ordered_node_ids)
    if len(ids) < 2 or len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
        raise ConfigurationError("ordered_node_ids must list at least two distinct node ids")
    if not isinstance(approval.base_artifact_sha256, str) or not _SHA256.fullmatch(approval.base_artifact_sha256):
        raise ConfigurationError("base_artifact_sha256 must be the 64-hex artifact hash from the snapshot")
    if approval.target not in ("pdf_tags", "source_html"):
        raise ConfigurationError("target must be pdf_tags or source_html")
    reviewer = check_reviewer(approval.reviewer)
    return compact(
        {
            "baseArtifactSha256": approval.base_artifact_sha256,
            "snapshotVersion": approval.snapshot_version,
            "target": approval.target,
            "orderedNodeIds": ids,
            "acknowledged": True,
            "reviewer": reviewer,
        }
    )


def session_body(
    allowed_origin: str, scopes: list[str] | None, max_jobs: int | None, max_uploads: int | None, ttl_seconds: int | None
) -> dict[str, Any]:
    return compact(
        {
            "allowedOrigin": check_origin(allowed_origin),
            "scopes": scopes,
            "maxJobs": max_jobs,
            "maxUploads": max_uploads,
            "ttlSeconds": ttl_seconds,
        }
    )


# --- http ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallResult:
    value: Any
    status: int
    retry_after: float | None
    meta: ResponseMeta


def request_headers(config: Config, *, has_body: bool, key: str | None, wait: int | None, anonymous: bool) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": config.user_agent}
    if not anonymous:
        headers["Authorization"] = f"Bearer {config.token}"
    if has_body:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["Idempotency-Key"] = key
    if wait is not None:
        headers["Prefer"] = f"wait={wait}"
    return headers


def request_timeout(config: Config, wait: int | None) -> httpx.Timeout:
    """The per-request timeout, stretched to cover a held request (`Prefer: wait`)."""
    seconds = config.timeout if wait is None else max(config.timeout, wait + 10.0)
    return httpx.Timeout(seconds)


def encode_body(body: Any) -> bytes | None:
    return None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return max(0.0, (at - datetime.now(timezone.utc)).total_seconds())
    return seconds if seconds >= 0 and seconds == seconds and seconds != float("inf") else None


def meta_of(response: httpx.Response) -> ResponseMeta:
    return ResponseMeta(
        status=response.status_code,
        location=response.headers.get("Location"),
        preference_applied=response.headers.get("Preference-Applied"),
        idempotent_replayed=response.headers.get("Idempotent-Replayed", "").lower() == "true",
    )


def read_problem(response: httpx.Response) -> Problem:
    status = response.status_code
    fallback = Problem("about:blank", response.reason_phrase or "HTTP error", status, "INTERNAL_ERROR" if status >= 500 else "UNKNOWN")
    try:
        body = response.json()
    except ValueError:
        return fallback
    if not isinstance(body, dict) or not isinstance(body.get("code"), str):
        return fallback
    detail = body.get("detail")
    return Problem(
        type=str(body.get("type", fallback.type)),
        title=str(body.get("title", fallback.title)),
        status=status,
        code=body["code"],
        detail=detail if isinstance(detail, str) else None,
        errors=problem_errors(body.get("errors")),
    )


def decode_success(response: httpx.Response, retry_after: float | None) -> CallResult:
    if response.status_code == 204:
        return CallResult(None, 204, retry_after, meta_of(response))
    try:
        value = response.json()
    except ValueError:
        raise malformed(response.status_code) from None
    return CallResult(value, response.status_code, retry_after, meta_of(response))


def malformed(status: int) -> ServerError:
    return ServerError(Problem("about:blank", "Malformed response", status, "INTERNAL_ERROR"))


def parse(factory: Callable[[Any], T], value: Any, status: int) -> T:
    """Build a model from a decoded body; a body of the wrong shape is a ``ServerError``, never a ``KeyError``."""
    try:
        return factory(value)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise malformed(status) from None


def api_error(response: httpx.Response, retry_after: float | None) -> Exception:
    return error_from_problem(read_problem(response), retry_after)


def redact(error: httpx.HTTPError, token: str) -> httpx.HTTPError:
    """The same kind of error with the token removed from its text."""
    text = str(error)
    if len(token) < 8 or token not in text:
        return error
    clean = text.replace(token, "***")
    try:
        replacement: httpx.HTTPError = type(error)(clean)
    except TypeError:
        replacement = httpx.TransportError(clean)
    if isinstance(error, httpx.RequestError) and isinstance(replacement, httpx.RequestError):
        # The request carries no credential in its text and callers use it to see what failed.
        with contextlib.suppress(RuntimeError):
            replacement.request = error.request
    return replacement


class RetryPolicy:
    """Which failures of one call are repeated, and after how long. Creations are repeated only with their key."""

    def __init__(self, *, method: str, key: str | None, wait: int | None, retry_on_busy: bool) -> None:
        self._method = method
        self._repeatable = method == "GET" or key is not None
        self._wait = wait
        self._retry_on_busy = retry_on_busy
        self._network = 0
        self._busy = 0

    def after_transport_error(self, error: httpx.HTTPError) -> float | None:
        """Seconds to pause before repeating, or None to give up."""
        # A timeout of a held request (wait) would only hold again for as long; connection failures are repeated.
        held_timeout = self._wait is not None and isinstance(error, httpx.TimeoutException)
        failed = isinstance(error, httpx.TransportError) and not isinstance(error, httpx.UnsupportedProtocol)
        if self._repeatable and failed and not held_timeout and self._network < MAX_NETWORK_RETRIES:
            self._network += 1
            return NETWORK_RETRY_DELAY * self._network
        return None

    def after_busy(self, status: int, retry_after: float | None) -> float | None:
        if self._method == "GET" and self._retry_on_busy and status in (429, 503) and self._busy < MAX_BUSY_RETRIES:
            self._busy += 1
            return min(retry_after if retry_after is not None else 1.0, MAX_POLL_DELAY)
        return None


def storage_failure(response: httpx.Response, method: str) -> Exception:
    problem = read_problem(response)
    return StorageError(f"storage answered HTTP {response.status_code} ({problem.code}) to {method}", response.status_code, problem.code)
