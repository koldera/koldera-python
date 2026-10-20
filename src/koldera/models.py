"""Responses of the koldera-core API as frozen dataclasses (attribute names are snake_case).

Enumerations are plain strings on purpose: a newer core may send values this SDK has never seen, and those are
kept as they are. The tuples below list the values known to this version.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .errors import ProblemError

PdfMode = Literal["analyze", "fix"]
HtmlToPdfMode = Literal["generate_analyze", "generate_fix"]
RemediationTarget = Literal["source_html", "pdf_tags"]
UploadContentType = Literal["application/pdf", "text/html", "image/png", "image/jpeg", "image/svg+xml"]
ImageContentType = Literal["image/png", "image/jpeg", "image/svg+xml"]
SessionScope = Literal["jobs:create", "jobs:read"]

KNOWN_JOB_KINDS = ("pdf", "html_to_pdf", "document_to_pdf", "web", "order_pdf", "order_html")
KNOWN_JOB_STATES = ("queued", "running", "completed", "failed")
KNOWN_ARTIFACT_KINDS = (
    "original",
    "remediated",
    "baseline_report",
    "final_report",
    "generated_pdf",
    "source_html",
    "web_report",
    "reordered",
)
KNOWN_VALIDATION_STATUSES = ("passed", "failed", "error", "not_run", "declaration_withheld")
KNOWN_REMEDIATION_STATUSES = ("not_requested", "no_applicable_fix", "applied", "skipped", "rolled_back")
KNOWN_FEATURES = ("document", "html", "pdf", "web")
_UNFINISHED_STATES = frozenset({"queued", "running"})
_FRACTION = re.compile(r"\.(\d+)")


def parse_datetime(value: str) -> datetime:
    """ISO 8601 with ``Z`` or an offset and a fraction of any length; always timezone-aware."""
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _dt(value: str | None) -> datetime | None:
    return None if value is None else parse_datetime(value)


@dataclass(frozen=True, slots=True)
class ResponseMeta:
    """Response metadata of a call, for callers that need more than the parsed body."""

    status: int
    #: Where to poll the job, as sent.
    location: str | None
    #: ``wait=N`` when the server held the request.
    preference_applied: str | None
    #: True when the stored answer of an earlier request was replayed.
    idempotent_replayed: bool


@dataclass(frozen=True, slots=True)
class FailedRule:
    rule_id: str
    count: int


@dataclass(frozen=True, slots=True)
class MachineValidation:
    """Automated validation (veraPDF or axe rules): a machine check, never a statement of conformance.

    ``declaration_withheld``: the only failing check is the missing PDF/UA identifier (veraPDF ``5-1``, still listed in
    ``failed_rules``), which was not written because declaring PDF/UA requires a human review. It is not a pass. Treat a
    status you do not know as no verdict.
    """

    profile: str
    status: str
    failed_rules: tuple[FailedRule, ...]

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> MachineValidation | None:
        if data is None:
            return None
        rules = tuple(FailedRule(r["ruleId"], int(r["count"])) for r in data.get("failedRules") or ())
        return cls(data["profile"], data["status"], rules)


@dataclass(frozen=True, slots=True)
class JobError:
    """Why a job failed (for example ``WORKER_TIMEOUT``, ``INVALID_INPUT``). Not a problem code."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Job:
    """A job as the API reports it.

    ``state`` is execution state only; ``validation`` is the machine check, and ``manual_review_required`` stays
    true: nothing here states conformance.
    """

    id: str
    kind: str
    mode: str | None
    state: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    validation: MachineValidation | None
    baseline_validation: MachineValidation | None
    final_validation: MachineValidation | None
    remediation_status: str | None
    manual_review_required: bool
    engine_version: str | None
    error: JobError | None
    #: Web jobs only, without query or fragment.
    target_url: str | None
    #: HTML jobs: the CSS selector that was printed instead of the whole document.
    content_selector: str | None = None

    @property
    def is_finished(self) -> bool:
        """Anything but ``queued`` and ``running``; a state a newer core adds counts as final."""
        return self.state not in _UNFINISHED_STATES

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Job:
        error = d.get("error")
        return cls(
            id=d["id"],
            kind=d["kind"],
            mode=d.get("mode"),
            state=d["state"],
            created_at=parse_datetime(d["createdAt"]),
            started_at=_dt(d.get("startedAt")),
            finished_at=_dt(d.get("finishedAt")),
            validation=MachineValidation.from_json(d.get("validation")),
            baseline_validation=MachineValidation.from_json(d.get("baselineValidation")),
            final_validation=MachineValidation.from_json(d.get("finalValidation")),
            remediation_status=d.get("remediationStatus"),
            manual_review_required=bool(d.get("manualReviewRequired", True)),
            engine_version=d.get("engineVersion"),
            error=JobError(error["code"], error["message"]) if error else None,
            target_url=d.get("targetUrl"),
            content_selector=d.get("contentSelector"),
        )


def is_finished(job: Job) -> bool:
    """True when the job is no longer ``queued`` or ``running``."""
    return job.is_finished


@dataclass(frozen=True, slots=True)
class Finding:
    source: str
    rule_id: str
    message: str
    page_number: int | None
    selector: str | None
    needs_human_review: bool
    #: ``fixed_in_copy`` or ``still_failing``, set only on machine checks of a job whose fix was applied.
    resolution: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Finding:
        return cls(
            d["source"], d["ruleId"], d["message"], d.get("pageNumber"), d.get("selector"), bool(d["needsHumanReview"]), d.get("resolution")
        )


@dataclass(frozen=True, slots=True)
class RemediationAction:
    """What one repair rule did on a ``fix`` job (``applied``, ``skipped``, ``failed``, ``rolled_back``)."""

    rule_id: str
    outcome: str
    reason: str
    before: str | None = None
    after: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RemediationAction:
        return cls(d["ruleId"], d["outcome"], d["reason"], d.get("before"), d.get("after"))


@dataclass(frozen=True, slots=True)
class Artifact:
    """A file of a job, downloadable through a short-lived signed URL."""

    id: str
    kind: str
    sha256: str
    size_bytes: int
    download_url: str
    expires_at: datetime

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Artifact:
        return cls(d["id"], d["kind"], d["sha256"], int(d["sizeBytes"]), d["downloadUrl"], parse_datetime(d["expiresAt"]))


@dataclass(frozen=True, slots=True)
class UploadTicket:
    upload_id: str
    method: str
    upload_url: str
    expires_at: datetime
    #: The configured size limit for this content type, if any.
    max_bytes: int | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> UploadTicket:
        limit = d.get("maxBytes")
        return cls(d["uploadId"], d["method"], d["uploadUrl"], parse_datetime(d["expiresAt"]), None if limit is None else int(limit))


@dataclass(frozen=True, slots=True)
class ReadingOrderNode:
    node_id: str
    parent_id: str | None
    role: str
    logical_index: int
    origin: str
    page_number: int | None = None
    text_preview: str | None = None
    bounding_box: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ReadingOrderNode:
        return cls(
            d["nodeId"],
            d.get("parentId"),
            d["role"],
            int(d["logicalIndex"]),
            d["origin"],
            d.get("pageNumber"),
            d.get("textPreview"),
            d.get("boundingBox"),
        )


@dataclass(frozen=True, slots=True)
class ReadingOrderSnapshot:
    """Evidence bound to one artifact hash. ``review_required`` is always true; it is never a verdict."""

    job_id: str
    source_artifact_sha256: str
    snapshot_version: int
    nodes: tuple[ReadingOrderNode, ...]
    review_required: bool
    reading_order_review: str
    focus_order_review: str
    focus_order_node_ids: tuple[str, ...]
    visual_order_candidate_node_ids: tuple[str, ...]
    artifact_hashes: dict[str, str]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ReadingOrderSnapshot:
        return cls(
            d["jobId"],
            d["sourceArtifactSha256"],
            int(d["snapshotVersion"]),
            tuple(ReadingOrderNode.from_json(n) for n in d["nodes"]),
            bool(d["reviewRequired"]),
            d["readingOrderReview"],
            d["focusOrderReview"],
            tuple(d.get("focusOrderNodeIds") or ()),
            tuple(d.get("visualOrderCandidateNodeIds") or ()),
            dict(d.get("artifactHashes") or {}),
        )


@dataclass(frozen=True, slots=True)
class ReadingOrderRemediation:
    """What a person approved: the complete children of one parent, in the new order."""

    base_artifact_sha256: str
    snapshot_version: int
    target: RemediationTarget
    ordered_node_ids: Sequence[str]
    #: Must be True: a person checked the sequence against the content.
    acknowledged: bool = False
    #: Audit label asserted by the caller, not authenticated (1-200 characters).
    reviewer: str | None = None


@dataclass(frozen=True, slots=True)
class ClientSession:
    """A minted session; hand ``session_token`` to the browser, never the server token."""

    session_token: str
    expires_at: datetime

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ClientSession:
        return cls(d["sessionToken"], parse_datetime(d["expiresAt"]))


@dataclass(frozen=True, slots=True)
class Info:
    """Version and enabled features of the server."""

    features: tuple[str, ...]
    version: str | None = None
    engine_version: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Info:
        return cls(tuple(d["features"]), d.get("version"), d.get("engineVersion"))


@dataclass(frozen=True, slots=True)
class Health:
    status: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Health:
        return cls(d["status"])


def problem_errors(value: Any) -> tuple[ProblemError, ...]:
    """The well-formed entries of a problem's ``errors`` array; anything else is dropped."""
    if not isinstance(value, list):
        return ()
    return tuple(
        ProblemError(e["path"], e["code"], e["message"])
        for e in value
        if isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("path", "code", "message"))
    )
