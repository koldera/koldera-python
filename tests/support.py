"""Replays contracts/golden/*.json through respx and drives sync and async clients with one set of tests."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "contracts" / "golden"
SECRET_KEY = "test-server-token"
SESSION_TOKEN = "v1.eyJmYWtlIjoidHJ1ZSJ9.ZmFrZS1zaWduYXR1cmU"
BASE_URL = "http://core.test"
JOB_ID = "4d2d5a6e-2b1c-4e4a-9d6b-1f3c5e7a9b01"
OTHER_JOB_ID = "4d2d5a6e-2b1c-4e4a-9d6b-1f3c5e7a9b02"
_PRINTABLE = re.compile(r"^[\x20-\x7e]{1,255}$")

Fixture = dict[str, Any]


def _load() -> dict[str, Fixture]:
    out: dict[str, Fixture] = {}
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf8"))
        out[data["name"]] = data
    return out


FIXTURES = _load()


def raw(name: str, side: str = "request") -> bytes:
    return str(FIXTURES[name][side]["rawBody"]).encode("latin1")


PDF_BYTES = raw("upload-put-pdf")
HTML_BYTES = raw("upload-put-html")
PNG_BYTES = raw("upload-put-image-png")


def body_of(name: str) -> Any:
    return FIXTURES[name]["request"]["body"]


@dataclass
class Lost:
    """The server handles the request (and the fixture is verified) but the answer never arrives."""

    name: str
    error: Exception = field(default_factory=lambda: httpx.ReadError("connection reset"))


@dataclass
class Fail:
    """The request never reaches the server."""

    error: Exception


@dataclass
class Reply:
    """A hand-made answer, not verified against a fixture."""

    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


Step = str | Lost | Fail | Reply


@dataclass
class Exchange:
    fixture: Fixture | None
    request: httpx.Request

    @property
    def path(self) -> str:
        return self.request.url.raw_path.decode()

    @property
    def headers(self) -> httpx.Headers:
        return self.request.headers

    @property
    def json(self) -> Any:
        return json.loads(self.request.content)


class Golden:
    def __init__(self) -> None:
        self.script: list[Step] = []
        self.exchanges: list[Exchange] = []
        self.mismatches: list[str] = []
        self.default: Callable[[httpx.Request], httpx.Response] | None = None

    def expect(self, *steps: Step) -> None:
        self.script.extend(steps)

    def assert_consumed(self) -> None:
        problems = [*(m[:400] for m in self.mismatches), *[f"step not reached: {s}" for s in self.script]]
        self.mismatches.clear()
        self.script.clear()
        assert not problems, "\n".join(problems)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.script:
            self.exchanges.append(Exchange(None, request))
            if self.default is not None:
                return self.default(request)
            self.mismatches.append(f"unexpected request {request.method} {request.url}")
            return httpx.Response(500, json={"type": "about:blank", "title": "unscripted", "status": 500, "code": "INTERNAL_ERROR"})
        step = self.script.pop(0)
        if isinstance(step, Fail):
            self.exchanges.append(Exchange(None, request))
            raise step.error
        if isinstance(step, Reply):
            self.exchanges.append(Exchange(None, request))
            headers = {"Content-Type": "application/json", **step.headers}
            content = b"" if step.body is None else json.dumps(step.body).encode()
            return httpx.Response(step.status, headers=headers, content=content)
        name = step.name if isinstance(step, Lost) else step
        fixture = FIXTURES[name]
        self.exchanges.append(Exchange(fixture, request))
        self._verify(fixture, request)
        response = self._respond(fixture)
        if isinstance(step, Lost):
            raise step.error
        return response

    @staticmethod
    def _resolve(text: str) -> str:
        return text.replace("{{baseUrl}}", BASE_URL).replace("{{secretKey}}", SECRET_KEY).replace("{{sessionToken}}", SESSION_TOKEN)

    def _respond(self, fixture: Fixture) -> httpx.Response:
        spec = fixture["response"]
        headers = {k: self._resolve(v) for k, v in spec["headers"].items() if k.lower() != "content-length"}
        if "rawBody" in spec:
            content = spec["rawBody"].encode("latin1")
        elif "body" in spec:
            content = self._resolve(json.dumps(spec["body"])).encode()
        else:
            content = b""
        return httpx.Response(spec["status"], headers=headers, content=content)

    def _verify(self, fixture: Fixture, request: httpx.Request) -> None:
        at = f"{fixture['name']}: "
        want = fixture["request"]
        path = request.url.raw_path.decode()
        if request.method != want["method"]:
            self.mismatches.append(at + f"method {request.method} != {want['method']}")
        if path != want["path"]:
            self.mismatches.append(at + f"path {path} != {want['path']}")
        for name, value in want["headers"].items():
            actual = request.headers.get(name)
            if actual is None:
                self.mismatches.append(at + f"missing header {name}")
            elif value == "{{idempotencyKey}}":
                if not _PRINTABLE.match(actual):
                    self.mismatches.append(at + f"Idempotency-Key not 1-255 printable ASCII: {actual!r}")
            elif actual.split(";")[0].strip() != self._resolve(value):
                self.mismatches.append(at + f"header {name}: {actual} != {self._resolve(value)}")
        for name in want.get("forbiddenHeaders", []):
            if name.lower() in request.headers:
                self.mismatches.append(at + f"forbidden header {name} present")
        content = request.content
        if "rawBody" in want:
            if content != want["rawBody"].encode("latin1"):
                self.mismatches.append(at + "raw body differs")
        elif "body" in want:
            try:
                parsed = json.loads(content)
            except ValueError:
                self.mismatches.append(at + "body is not JSON")
                return
            if json.dumps(parsed, sort_keys=True) != json.dumps(want["body"], sort_keys=True):
                self.mismatches.append(at + f"body {parsed} != {want['body']}")
        elif content:
            self.mismatches.append(at + "unexpected request body")


class SyncFacade:
    """Lets one test body drive the async client: coroutine methods are run to completion on the test's loop."""

    def __init__(self, target: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._target = target
        self._loop = loop

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if inspect.iscoroutinefunction(attr):

            def run(*args: Any, **kwargs: Any) -> Any:
                return self._loop.run_until_complete(attr(*args, **kwargs))

            return run
        if callable(attr) or isinstance(attr, str | int | float | bytes | bool | tuple | type(None)):
            return attr
        return SyncFacade(attr, self._loop)

    def close(self) -> None:
        self._loop.run_until_complete(self._target.aclose())


def job_json(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": JOB_ID,
        "kind": "pdf",
        "mode": "analyze",
        "state": "queued",
        "createdAt": "2026-10-05T10:00:00Z",
        "startedAt": None,
        "finishedAt": None,
        "validation": None,
        "baselineValidation": None,
        "finalValidation": None,
        "remediationStatus": None,
        "manualReviewRequired": True,
        "engineVersion": None,
        "error": None,
        "targetUrl": None,
    }
    base.update(fields)
    return base


def problem_json(status: int, code: str, **extra: Any) -> dict[str, Any]:
    return {"type": "about:blank", "title": "stub", "status": status, "code": code, **extra}
