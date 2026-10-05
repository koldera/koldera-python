"""Typed errors. Messages never include a credential."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProblemError:
    """One problem of a rejected structured document; ``path`` is a JSON Pointer."""

    path: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Problem:
    """RFC 9457 problem body of koldera-core."""

    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    errors: tuple[ProblemError, ...] = ()


class KolderaError(Exception):
    """Base class of every error the SDK raises itself (network failures stay ``httpx`` exceptions)."""


class ApiError(KolderaError):
    """Every non-2xx answer becomes one of these.

    ``code`` is the problem code (``QUEUE_FULL``, ``STALE_READING_ORDER_SNAPSHOT``, ...); the class tells the
    caller what to do. Unknown codes and statuses from a newer core still arrive as an ``ApiError``.
    """

    def __init__(self, problem: Problem, retry_after: float | None = None) -> None:
        detail = f": {problem.detail}" if problem.detail else ""
        super().__init__(f"{problem.title} ({problem.code}, HTTP {problem.status}){detail}")
        self.problem = problem
        self.status = problem.status
        self.code = problem.code
        self.title = problem.title
        self.detail = problem.detail
        #: Per-problem entries of a rejected structured document; empty otherwise.
        self.errors = problem.errors
        #: Seconds the server asked to wait (``Retry-After``), when it said so.
        self.retry_after = retry_after


class AuthenticationError(ApiError):
    """401: the token is missing, malformed, expired or unknown."""


class PermissionDeniedError(ApiError):
    """403: the token is valid but lacks the scope or the right (``INSUFFICIENT_SCOPE``, ``FORBIDDEN``)."""


class NotFoundError(ApiError):
    """404: the resource does not exist, is not visible to this token, or its feature is switched off."""


class ConflictError(ApiError):
    """409: idempotency key reused with another body, stale reading-order snapshot, expired artifact."""


class ValidationError(ApiError):
    """422, and 400 for a malformed body: the request or the input is invalid. See ``code`` and ``errors``."""


class UnsupportedMediaTypeError(ApiError):
    """415: the body is not JSON, or the input type is unsupported."""


class HostNotAllowedError(ApiError):
    """421: the request reached a host the server does not serve."""


class RateLimitError(ApiError):
    """429: a client-session budget is used up (``SESSION_JOB_LIMIT``, ``SESSION_UPLOAD_LIMIT``); mint a new session."""


class ServerError(ApiError):
    """Any other 5xx."""


class ServiceUnavailableError(ServerError):
    """503, including ``QUEUE_FULL``; ``retry_after`` says when to try again."""


class ConfigurationError(KolderaError, ValueError):
    """The SDK was used wrongly: bad options, an unacknowledged correction, an invalid argument."""


class JobTimeoutError(KolderaError):
    """``jobs.wait`` ran past its timeout. The job keeps running on the server."""

    def __init__(self, job_id: str, timeout: float) -> None:
        super().__init__(f"job {job_id} did not finish within {timeout:g} s")
        self.job_id = job_id


class StorageError(KolderaError):
    """A signed storage exchange (upload PUT or artifact download) failed, or the downloaded bytes did not match."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        #: Problem code of the storage answer (``SIGNATURE_INVALID``, ``UPLOAD_EXPIRED``, ...), when it sent one.
        self.code = code


def error_from_problem(problem: Problem, retry_after: float | None = None) -> ApiError:
    """Map a problem to its class by status."""
    status = problem.status
    if status in (400, 422):
        return ValidationError(problem, retry_after)
    if status == 401:
        return AuthenticationError(problem, retry_after)
    if status == 403:
        return PermissionDeniedError(problem, retry_after)
    if status == 404:
        return NotFoundError(problem, retry_after)
    if status == 409:
        return ConflictError(problem, retry_after)
    if status == 415:
        return UnsupportedMediaTypeError(problem, retry_after)
    if status == 421:
        return HostNotAllowedError(problem, retry_after)
    if status == 429:
        return RateLimitError(problem, retry_after)
    if status == 503:
        return ServiceUnavailableError(problem, retry_after)
    if status >= 500:
        return ServerError(problem, retry_after)
    return ApiError(problem, retry_after)
