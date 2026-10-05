"""Every behaviour is replayed against the golden fixtures with both clients (the `client` fixture is parametrised)."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import MakeClient
from koldera import (
    ApiError,
    Artifact,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    HostNotAllowedError,
    JobTimeoutError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ReadingOrderRemediation,
    ResponseMeta,
    ServerError,
    ServiceUnavailableError,
    StorageError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from support import (
    BASE_URL,
    FIXTURES,
    HTML_BYTES,
    JOB_ID,
    OTHER_JOB_ID,
    PDF_BYTES,
    PNG_BYTES,
    SECRET_KEY,
    SESSION_TOKEN,
    Exchange,
    Fail,
    Golden,
    Lost,
    Reply,
    body_of,
    job_json,
    problem_json,
    raw,
)

UPLOAD_PDF = ("upload-ticket-pdf", "upload-put-pdf")
UPLOAD_HTML = ("upload-ticket-html", "upload-put-html")
SCAN = "https://example.com/page"


def last(golden: Golden, count: int = 1) -> list[Exchange]:
    return golden.exchanges[-count:]


class TestConfiguration:
    def test_needs_a_base_url_and_a_non_empty_token(self, make: MakeClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KOLDERA_TOKEN", raising=False)
        monkeypatch.delenv("KOLDERA_URL", raising=False)
        with pytest.raises(ConfigurationError, match="token"):
            make(token="")
        with pytest.raises(ConfigurationError, match="token"):
            make(token="   ")
        with pytest.raises(ConfigurationError, match="KOLDERA_URL"):
            make(base_url="")
        with pytest.raises(ConfigurationError, match="base_url"):
            make(base_url="not a url")
        with pytest.raises(ConfigurationError, match="timeout"):
            make(timeout=0)

    def test_reads_koldera_url_and_koldera_token(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        from koldera import AsyncKolderaClient, KolderaClient

        cls = KolderaClient if kind == "sync" else AsyncKolderaClient
        monkeypatch.setenv("KOLDERA_TOKEN", SECRET_KEY)
        monkeypatch.setenv("KOLDERA_URL", BASE_URL + "/")
        assert isinstance(cls(), cls)

    def test_accepts_any_token_string(self, make: MakeClient) -> None:
        for token in ["x", "pk_looks_like_a_vendor_key", "plain token with spaces", "0123456789"]:
            make(token=token)

    def test_sends_the_token_as_bearer_and_identifies_the_sdk(self, client: Any, golden: Golden) -> None:
        golden.expect("info")
        client.info()
        sent = last(golden)[0].headers
        assert sent["authorization"] == "Bearer " + SECRET_KEY
        assert sent["user-agent"].startswith("koldera-sdk-python/")

    def test_user_agent_suffix(self, make: MakeClient, golden: Golden) -> None:
        golden.expect("info")
        make(user_agent_suffix="my-app/1.0").info()
        assert last(golden)[0].headers["user-agent"].endswith(" my-app/1.0")

    def test_repr_never_shows_the_token(self, client: Any) -> None:
        assert SECRET_KEY not in repr(client)


class TestService:
    def test_info_reads_version_and_features_also_for_a_session(self, make: MakeClient, client: Any, golden: Golden) -> None:
        golden.expect("info")
        info = client.info()
        assert info.features == ("document", "html", "pdf", "web")
        assert info.version == "0.1.0"
        assert info.engine_version == "0.1.0"
        golden.expect("info-session")
        assert len(make(token=SESSION_TOKEN).info().features) > 0

    def test_health_sends_no_credential(self, client: Any, golden: Golden) -> None:
        golden.expect("healthz")
        assert client.health().status == "ok"
        assert "authorization" not in last(golden)[0].headers


class TestJobCreation:
    def test_pdf_issues_a_ticket_puts_the_bytes_without_the_bearer_and_queues_the_job(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-accepted")
        job = client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="analyze")
        assert job.id == JOB_ID
        assert job.state == "queued"
        assert job.manual_review_required is True
        ticket, put, post = last(golden, 3)
        assert "idempotency-key" in ticket.headers
        assert ticket.headers["idempotency-key"] != post.headers["idempotency-key"]
        assert "authorization" not in put.headers

    def test_pdf_fix_sends_language_and_trimmed_title_and_omits_what_is_absent(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-accepted-fix")
        job = client.pdf.create_job(
            PDF_BYTES, file_name="sample.pdf", mode="fix", explicit_language="en", explicit_title="  Sample report  "
        )
        assert job.mode == "fix"
        body = last(golden)[0].json
        assert body["explicitTitle"] == "Sample report"
        assert body["explicitLanguage"] == "en"

    def test_absent_optional_fields_are_omitted_not_null(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-accepted")
        client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="analyze", explicit_language=None, explicit_title=None)
        assert last(golden)[0].json == {"uploadId": "11111111-1111-4111-8111-111111111111", "mode": "analyze", "profile": "ua1"}

    @pytest.mark.parametrize("mode", ["safe_fix", "structure_fix", "scan", "generate_fix"])
    def test_pdf_accepts_only_analyze_and_fix(self, client: Any, mode: str) -> None:
        with pytest.raises(ConfigurationError, match="analyze, fix"):
            client.pdf.create_job(PDF_BYTES, mode=mode)

    def test_html_to_pdf_uploads_text_html_with_selector_language_and_title(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_HTML, "html-job-accepted")
        job = client.html_to_pdf.create_job(
            HTML_BYTES,
            file_name="page.html",
            mode="generate_fix",
            content_selector="main",
            explicit_language="en",
            explicit_title="Sample page",
        )
        assert job.kind == "html_to_pdf"
        assert job.content_selector == "main"
        golden.expect(*UPLOAD_HTML, "html-job-analyze")
        assert client.html_to_pdf.create_job(HTML_BYTES, file_name="page.html", mode="generate_analyze").mode == "generate_analyze"

    def test_html_to_pdf_refuses_the_retired_mode_a_blank_selector_and_has_no_url_entry_point(self, client: Any) -> None:
        with pytest.raises(ConfigurationError, match="generate_analyze, generate_fix"):
            client.html_to_pdf.create_job(HTML_BYTES, mode="generate_safe_fix")
        with pytest.raises(ConfigurationError, match="content_selector"):
            client.html_to_pdf.create_job(HTML_BYTES, mode="generate_fix", content_selector="   ")
        assert not hasattr(client.html_to_pdf, "create_from_url")

    def test_web_scan_sends_the_url_and_refuses_other_schemes_locally(self, client: Any, golden: Golden) -> None:
        golden.expect("web-job-accepted")
        job = client.web.create_scan(SCAN)
        assert job.target_url == SCAN
        with pytest.raises(ConfigurationError, match="http"):
            client.web.create_scan("file:///etc/passwd")

    def test_validates_mode_language_title_and_file_before_any_request(self, client: Any) -> None:
        with pytest.raises(ConfigurationError, match="BCP 47"):
            client.pdf.create_job(PDF_BYTES, mode="analyze", explicit_language="english!")
        with pytest.raises(ConfigurationError, match="explicit_title must be 1-500 printable characters"):
            client.pdf.create_job(PDF_BYTES, mode="fix", explicit_title="  ")
        with pytest.raises(ConfigurationError, match="explicit_title"):
            client.pdf.create_job(PDF_BYTES, mode="fix", explicit_title="x" * 501)
        with pytest.raises(ConfigurationError, match="explicit_title"):
            client.pdf.create_job(PDF_BYTES, mode="fix", explicit_title="a\x00b")
        with pytest.raises(ConfigurationError, match="empty"):
            client.pdf.create_job(b"", mode="analyze")

    def test_a_title_of_500_code_points_is_accepted(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, Reply(202, job_json()))
        client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="analyze", explicit_title="\U0001f600" * 500)
        assert len(last(golden)[0].json["explicitTitle"]) == 500

    def test_accepts_bytes_like_paths_and_binary_files(self, client: Any, golden: Golden, tmp_path: Path) -> None:
        path = tmp_path / "sample.pdf"
        path.write_bytes(PDF_BYTES)
        sources: list[Any] = [bytearray(PDF_BYTES), memoryview(PDF_BYTES), io.BytesIO(PDF_BYTES), path, str(path)]
        for source in sources:
            golden.expect(*UPLOAD_PDF, "pdf-job-accepted")
            client.pdf.create_job(source, file_name="sample.pdf", mode="analyze")
        golden.expect(*UPLOAD_PDF, "pdf-job-accepted")
        client.pdf.create_job(path, mode="analyze")
        assert last(golden, 3)[0].json["fileName"] == "sample.pdf"

    def test_refuses_text_mode_files_and_closed_files(self, client: Any, tmp_path: Path) -> None:
        path = tmp_path / "a.pdf"
        path.write_bytes(PDF_BYTES)
        with path.open("r", encoding="latin1") as text, pytest.raises(ConfigurationError, match="binary"):
            client.pdf.create_job(text, mode="analyze")
        closed = path.open("rb")
        closed.close()
        with pytest.raises(ValueError, match="closed"):
            client.pdf.create_job(closed, mode="analyze")

    def test_a_file_larger_than_the_ticket_limit_is_refused_after_the_ticket(self, client: Any, golden: Golden) -> None:
        ticket = dict(FIXTURES["upload-ticket-pdf"]["response"]["body"], maxBytes=4, uploadUrl=BASE_URL + "/x")
        golden.expect(Reply(201, ticket))
        with pytest.raises(ConfigurationError, match="ticket limit"):
            client.uploads.upload(PDF_BYTES, "application/pdf", "sample.pdf")
        assert len(golden.exchanges) == 1

    def test_upload_validates_content_type_and_file_name(self, client: Any) -> None:
        with pytest.raises(ConfigurationError, match="content_type"):
            client.uploads.upload(PDF_BYTES, "text/plain", "a.txt")
        with pytest.raises(ConfigurationError, match="file_name"):
            client.uploads.upload(PDF_BYTES, "application/pdf", "")
        with pytest.raises(ConfigurationError, match="file_name"):
            client.uploads.upload(PDF_BYTES, "application/pdf", "n" * 256)


class TestStructuredDocuments:
    def test_posts_the_document_itself_unwrapped(self, client: Any, golden: Golden) -> None:
        golden.expect("document-job-accepted")
        job = client.documents.create_job(body_of("document-job-accepted"))
        assert job.kind == "document_to_pdf"
        assert job.mode == "generate_analyze"

    def test_upload_image_returns_the_ticket_whose_upload_id_an_image_block_uses(self, client: Any, golden: Golden) -> None:
        golden.expect("upload-ticket-image-png", "upload-put-image-png")
        ticket = client.documents.upload_image(PNG_BYTES, content_type="image/png", file_name="logo.png")
        assert ticket.upload_id == "33333333-3333-4333-8333-333333333333"
        assert ticket.max_bytes is None
        assert "authorization" not in last(golden)[0].headers

    def test_takes_the_image_type_from_the_file_name_and_refuses_anything_else(self, client: Any, golden: Golden, tmp_path: Path) -> None:
        path = tmp_path / "logo.png"
        path.write_bytes(PNG_BYTES)
        golden.expect("upload-ticket-image-png", "upload-put-image-png")
        client.documents.upload_image(path)
        with pytest.raises(ConfigurationError, match="image content_type"):
            client.documents.upload_image(PNG_BYTES)
        with pytest.raises(ConfigurationError, match="image content_type"):
            client.documents.upload_image(PNG_BYTES, content_type="application/pdf")
        with pytest.raises(ConfigurationError, match="document"):
            client.documents.create_job(None)
        with pytest.raises(ConfigurationError, match="document"):
            client.documents.create_job([])

    def test_a_rejected_document_carries_every_problem_in_errors(self, client: Any, golden: Golden) -> None:
        golden.expect("document-job-validation-errors")
        with pytest.raises(ValidationError) as caught:
            client.documents.create_job(body_of("document-job-validation-errors"))
        e = caught.value
        assert e.code == "VALIDATION_ERROR"
        assert [(p.path, p.code, p.message) for p in e.errors] == [
            ("/body/0/level", "HEADING_LEVEL_SKIPPED", "The first heading must be level 1 or 2."),
            ("/body/1/alt", "IMAGE_ALT_MISSING", "An image needs a description."),
        ]

    def test_a_failed_image_reference_is_reported_with_its_json_pointer(self, client: Any, golden: Golden) -> None:
        golden.expect("document-job-image-upload-invalid")
        with pytest.raises(ValidationError) as caught:
            client.documents.create_job(body_of("document-job-image-upload-invalid"))
        assert (caught.value.errors[0].path, caught.value.errors[0].code) == ("/body/3/uploadId", "IMAGE_UPLOAD_INVALID")


class TestWaitAndPrefer:
    def test_wait_sends_prefer_and_a_200_returns_the_finished_job(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-prefer-wait-finished")
        metas: list[ResponseMeta] = []
        done = client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix", wait=60, on_response=metas.append)
        assert done.state == "completed"
        assert done.final_validation.status == "passed"
        assert metas == [ResponseMeta(status=200, location=f"/v1/jobs/{JOB_ID}", preference_applied="wait=60", idempotent_replayed=False)]

    def test_a_202_after_the_wait_is_returned_unfinished_and_nothing_polls_on_its_own(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-prefer-wait-timeout")
        metas: list[ResponseMeta] = []
        pending = client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix", wait=5, on_response=metas.append)
        assert pending.state == "running"
        assert pending.finished_at is None
        assert pending.is_finished is False
        assert (metas[0].status, metas[0].preference_applied) == (202, "wait=5")
        assert len(golden.exchanges) == 3

    def test_until_finished_continues_with_jobs_wait_after_a_202(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-prefer-wait-timeout", "job-completed-fix")
        done = client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix", wait=5, until_finished=True)
        assert done.state == "completed"
        assert last(golden)[0].request.method == "GET"

    def test_until_finished_does_not_poll_when_the_wait_already_finished_the_job(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-prefer-wait-finished")
        assert client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix", wait=60, until_finished=True).state == "completed"

    def test_until_finished_without_wait_polls_after_a_plain_202(self, client: Any, golden: Golden) -> None:
        golden.expect(Reply(202, job_json(kind="web")), Reply(200, job_json(kind="web", state="completed")))
        done = client.web.create_scan(SCAN, until_finished=True)
        assert done.state == "completed"
        assert [(x.request.method, x.path) for x in golden.exchanges] == [("POST", "/v1/web/jobs"), ("GET", f"/v1/jobs/{JOB_ID}")]
        assert "prefer" not in golden.exchanges[0].headers

    def test_documents_and_reading_order_corrections_take_wait_too(self, client: Any, golden: Golden) -> None:
        golden.expect("document-job-prefer-wait-finished")
        assert client.documents.create_job(body_of("document-job-prefer-wait-finished"), wait=30).state == "completed"
        golden.expect("remediation-without-reviewer")
        body = body_of("remediation-without-reviewer")
        approval = ReadingOrderRemediation(
            body["baseArtifactSha256"], body["snapshotVersion"], body["target"], body["orderedNodeIds"], True
        )
        fixed = client.jobs.remediate_reading_order(JOB_ID, approval, wait=60)
        assert (fixed.kind, fixed.state) == ("order_pdf", "completed")

    @pytest.mark.parametrize("wait", [0, -1, 901, 1.5, float("nan"), True, "5"])
    def test_rejects_a_wait_outside_1_to_900_seconds_before_any_request(self, client: Any, wait: Any) -> None:
        with pytest.raises(ConfigurationError, match="wait must be"):
            client.web.create_scan(SCAN, wait=wait)

    def test_exposes_an_idempotent_replay(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-idempotent-replay")
        metas: list[ResponseMeta] = []
        client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix", idempotency_key="retry-1", on_response=metas.append)
        assert (metas[0].status, metas[0].idempotent_replayed) == (202, True)

    def test_stretches_the_request_timeout_over_the_wait(self, make: MakeClient, golden: Golden) -> None:
        client = make(timeout=5.0)
        golden.expect("web-job-accepted", "web-job-accepted")
        client.web.create_scan(SCAN, wait=60)
        client.web.create_scan(SCAN)
        waited, plain = (x.request.extensions["timeout"]["read"] for x in golden.exchanges)
        assert (waited, plain) == (70, 5.0)


class TestPollingAndReads:
    def test_jobs_wait_honours_retry_after_and_returns_the_terminal_job(self, client: Any, golden: Golden, sleeps: list[float]) -> None:
        golden.expect("job-queued-retry-after", "job-running", "job-completed")
        seen: list[str] = []
        done = client.jobs.wait(JOB_ID, on_update=lambda j: seen.append(j.state))
        assert seen == ["queued", "running", "completed"]
        assert sleeps == [2.0, 2.0]
        assert done.remediation_status == "not_requested"
        assert done.created_at == datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)

    def test_without_retry_after_the_delay_doubles_up_to_ten_seconds(self, client: Any, golden: Golden, sleeps: list[float]) -> None:
        steps: list[Any] = [Reply(200, job_json(state="running")) for _ in range(6)]
        golden.expect(*steps, Reply(200, job_json(state="completed")))
        client.jobs.wait(JOB_ID, initial_delay=1.0)
        assert sleeps == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]

    def test_a_failed_job_is_returned_not_raised(self, client: Any, golden: Golden) -> None:
        golden.expect("job-failed")
        done = client.jobs.wait(JOB_ID)
        assert done.state == "failed"
        assert done.error.code == "WORKER_TIMEOUT"

    def test_wait_raises_when_the_budget_cannot_cover_the_next_poll(self, client: Any, golden: Golden, sleeps: list[float]) -> None:
        golden.expect("job-queued-retry-after")
        with pytest.raises(JobTimeoutError) as caught:
            client.jobs.wait(JOB_ID, timeout=1.0)
        assert caught.value.job_id == JOB_ID
        assert sleeps == []

    def test_lists_findings_actions_and_artifacts(self, client: Any, golden: Golden) -> None:
        golden.expect("findings", "actions", "artifacts")
        findings = client.jobs.findings(JOB_ID)
        assert [f.resolution for f in findings] == ["fixed_in_copy", None]
        assert [f.needs_human_review for f in findings] == [False, True]
        actions = client.jobs.actions(JOB_ID)
        assert [a.outcome for a in actions] == ["applied", "rolled_back"]
        assert (actions[0].before, actions[0].after) == (None, "sample")
        artifacts = client.jobs.artifacts(JOB_ID)
        assert [a.kind for a in artifacts] == ["original", "remediated"]
        assert artifacts[0].size_bytes == 45

    def test_reads_use_the_job_id_lowercased_and_reject_a_malformed_one_locally(self, client: Any, golden: Golden) -> None:
        golden.expect("job-completed")
        client.jobs.get(JOB_ID.upper())
        assert last(golden)[0].path == f"/v1/jobs/{JOB_ID}"
        for bad in ["../organizations", "", "not-a-uuid"]:
            with pytest.raises(ConfigurationError, match="job_id"):
                client.jobs.get(bad)

    def test_reads_a_job_of_a_session(self, make: MakeClient, golden: Golden) -> None:
        golden.expect("job-completed-session")
        assert make(token=SESSION_TOKEN).jobs.get(JOB_ID).validation.status == "failed"


def artifact_for(name: str, **fields: Any) -> Artifact:
    fixture = FIXTURES[name]
    values: dict[str, Any] = {
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
        "kind": "original",
        "sha256": fixture["response"].get("sha256", "0" * 64),
        "size_bytes": len(raw(name, "response")) if "rawBody" in fixture["response"] else 1,
        "download_url": BASE_URL + fixture["request"]["path"],
        "expires_at": datetime(2026, 10, 5, 10, 10, tzinfo=timezone.utc),
    }
    values.update(fields)
    return Artifact(**values)


class TestArtifactDownloads:
    def test_returns_the_bytes_when_size_and_hash_match_without_the_bearer(self, client: Any, golden: Golden, tmp_path: Path) -> None:
        golden.expect("artifact-download-original", "artifact-download-original")
        assert client.artifacts.download(artifact_for("artifact-download-original")) == PDF_BYTES
        assert "authorization" not in last(golden)[0].headers
        target = tmp_path / "out.pdf"
        client.artifacts.download(artifact_for("artifact-download-original"), target)
        assert target.read_bytes() == PDF_BYTES

    def test_writes_to_a_file_object(self, client: Any, golden: Golden) -> None:
        golden.expect("artifact-download-original")
        sink = io.BytesIO()
        client.artifacts.download(artifact_for("artifact-download-original"), sink)
        assert sink.getvalue() == PDF_BYTES

    def test_raises_on_a_wrong_hash_and_returns_and_writes_nothing(self, client: Any, golden: Golden, tmp_path: Path) -> None:
        golden.expect("artifact-download-original")
        target = tmp_path / "out.pdf"
        with pytest.raises(StorageError, match="SHA-256"):
            client.artifacts.download(artifact_for("artifact-download-original", sha256="0" * 64), target)
        assert not target.exists()

    def test_raises_on_a_wrong_size_even_when_the_hash_would_match(self, client: Any, golden: Golden) -> None:
        golden.expect("artifact-download-original")
        real = artifact_for("artifact-download-original")
        with pytest.raises(StorageError, match="bytes"):
            client.artifacts.download(artifact_for("artifact-download-original", size_bytes=real.size_bytes + 1))

    def test_raises_when_more_bytes_arrive_than_declared(self, client: Any, golden: Golden) -> None:
        golden.expect("artifact-download-original")
        with pytest.raises(StorageError, match="more than"):
            client.artifacts.download(artifact_for("artifact-download-original", size_bytes=10))

    def test_the_hash_comparison_ignores_case(self, client: Any, golden: Golden) -> None:
        golden.expect("artifact-download-original")
        upper = artifact_for("artifact-download-original").sha256.upper()
        assert client.artifacts.download(artifact_for("artifact-download-original", sha256=upper)) == PDF_BYTES

    def test_maps_a_refused_download_to_a_storage_error_with_the_problem_code(self, client: Any, golden: Golden) -> None:
        golden.expect("artifact-download-signature-invalid", "artifact-download-not-found")
        with pytest.raises(StorageError) as denied:
            client.artifacts.download(artifact_for("artifact-download-signature-invalid"))
        assert (denied.value.status, denied.value.code) == (403, "SIGNATURE_INVALID")
        with pytest.raises(StorageError) as missing:
            client.artifacts.download(artifact_for("artifact-download-not-found"))
        assert (missing.value.status, missing.value.code) == (404, "NOT_FOUND")

    def test_maps_a_refused_upload_to_a_storage_error_with_the_problem_code(self, client: Any, golden: Golden) -> None:
        golden.expect("upload-ticket-pdf", "upload-put-checksum-mismatch")
        with pytest.raises(StorageError) as caught:
            client.uploads.upload(PDF_BYTES, "application/pdf", "sample.pdf")
        assert (caught.value.status, caught.value.code) == (422, "CHECKSUM_MISMATCH")

    @pytest.mark.parametrize(
        ("name", "status", "code"),
        [("upload-put-expired", 410, "UPLOAD_EXPIRED"), ("upload-put-signature-invalid", 403, "SIGNATURE_INVALID")],
    )
    def test_other_refused_uploads(self, client: Any, golden: Golden, name: str, status: int, code: str) -> None:
        golden.expect("upload-ticket-pdf", name)
        with pytest.raises(StorageError) as caught:
            client.uploads.upload(PDF_BYTES, "application/pdf", "sample.pdf")
        assert (caught.value.status, caught.value.code) == (status, code)


class TestReadingOrder:
    approval = body_of("remediation-accepted")

    def make_approval(self, **changes: Any) -> ReadingOrderRemediation:
        values: dict[str, Any] = {
            "base_artifact_sha256": self.approval["baseArtifactSha256"],
            "snapshot_version": self.approval["snapshotVersion"],
            "target": self.approval["target"],
            "ordered_node_ids": self.approval["orderedNodeIds"],
            "acknowledged": True,
            "reviewer": self.approval["reviewer"],
        }
        values.update(changes)
        return ReadingOrderRemediation(**values)

    def test_returns_the_snapshot_and_queues_an_acknowledged_correction_with_a_reviewer(self, client: Any, golden: Golden) -> None:
        golden.expect("reading-order", "remediation-accepted")
        snapshot = client.jobs.get_reading_order(JOB_ID)
        assert snapshot.review_required is True
        assert snapshot.reading_order_review == "needs_review"
        assert snapshot.focus_order_review == "not_run"
        assert snapshot.nodes[0].bounding_box is None
        assert snapshot.nodes[1].bounding_box == {"x": 72, "y": 720, "width": 200, "height": 12}
        created = client.jobs.remediate_reading_order(JOB_ID, self.make_approval())
        assert (created.kind, created.mode) == ("order_pdf", "reorder")

    def test_omits_the_reviewer_when_none_is_given(self, client: Any, golden: Golden) -> None:
        golden.expect("remediation-without-reviewer")
        client.jobs.remediate_reading_order(JOB_ID, self.make_approval(reviewer=None), wait=60)

    def test_sends_the_trimmed_reviewer_and_counts_its_length_after_trimming(self, client: Any, golden: Golden) -> None:
        golden.expect(Reply(202, job_json(kind="order_pdf")))
        client.jobs.remediate_reading_order(JOB_ID, self.make_approval(reviewer=" " + "r" * 200 + " "))
        assert last(golden)[0].json["reviewer"] == "r" * 200
        for bad in ["r" * 201, "   ", "a\nb"]:
            with pytest.raises(ConfigurationError, match="reviewer"):
                client.jobs.remediate_reading_order(JOB_ID, self.make_approval(reviewer=bad))

    def test_refuses_an_unacknowledged_or_malformed_approval_before_any_request(self, client: Any) -> None:
        with pytest.raises(ConfigurationError, match="acknowledged"):
            client.jobs.remediate_reading_order(JOB_ID, self.make_approval(acknowledged=False))
        with pytest.raises(ConfigurationError, match="distinct"):
            client.jobs.remediate_reading_order(JOB_ID, self.make_approval(ordered_node_ids=["n1", "n1"]))
        with pytest.raises(ConfigurationError, match="distinct"):
            client.jobs.remediate_reading_order(JOB_ID, self.make_approval(ordered_node_ids=["n1"]))
        with pytest.raises(ConfigurationError, match="hash"):
            client.jobs.remediate_reading_order(JOB_ID, self.make_approval(base_artifact_sha256="nope"))
        with pytest.raises(ConfigurationError, match="target"):
            client.jobs.remediate_reading_order(JOB_ID, self.make_approval(target="pdf"))

    def test_maps_stale_expired_unsafe_and_missing_to_typed_errors_with_the_code(self, client: Any, golden: Golden) -> None:
        golden.expect("remediation-stale", "remediation-artifact-expired", "remediation-unsupported", "reading-order-not-found")
        approval = self.make_approval(reviewer=None)
        with pytest.raises(ConflictError) as stale:
            client.jobs.remediate_reading_order(JOB_ID, approval)
        assert stale.value.code == "STALE_READING_ORDER_SNAPSHOT"
        with pytest.raises(ConflictError) as expired:
            client.jobs.remediate_reading_order(JOB_ID, approval)
        assert expired.value.code == "ARTIFACT_EXPIRED"
        with pytest.raises(ValidationError) as unsupported:
            client.jobs.remediate_reading_order(JOB_ID, self.make_approval(reviewer=None, target="source_html"))
        assert unsupported.value.code == "ORDER_FIX_UNSUPPORTED"
        with pytest.raises(NotFoundError):
            client.jobs.get_reading_order(OTHER_JOB_ID)


class TestClientSessions:
    def test_mints_a_scoped_session_with_the_request_shape_of_core(self, client: Any, golden: Golden) -> None:
        golden.expect("client-session")
        minted = client.client_sessions.create(
            allowed_origin="https://app.example.com", scopes=["jobs:create", "jobs:read"], max_jobs=3, max_uploads=6, ttl_seconds=600
        )
        assert minted.session_token.startswith("v1.")
        assert minted.expires_at == datetime(2026, 10, 5, 10, 10, tzinfo=timezone.utc)
        assert not hasattr(minted, "publishable_key")

    def test_sends_only_allowed_origin_when_nothing_else_is_set(self, client: Any, golden: Golden) -> None:
        golden.expect("client-session-defaults")
        client.client_sessions.create(allowed_origin="https://app.example.com")

    def test_refuses_an_origin_with_a_path_locally_and_maps_the_servers_refusals(
        self, make: MakeClient, client: Any, golden: Golden
    ) -> None:
        with pytest.raises(ConfigurationError, match="origin"):
            client.client_sessions.create(allowed_origin="https://app.example.com/path")
        golden.expect("client-session-forbidden")
        with pytest.raises(PermissionDeniedError) as forbidden:
            make(token=SESSION_TOKEN).client_sessions.create(allowed_origin="https://app.example.com")
        assert forbidden.value.code == "FORBIDDEN"
        invalid = FIXTURES["client-session-invalid"]["response"]
        golden.expect(Reply(invalid["status"], invalid["body"], invalid["headers"]), "client-session-unsupported-media-type")
        with pytest.raises(ValidationError):
            client.client_sessions.create(allowed_origin="https://app.example.com", max_jobs=5000)
        with pytest.raises(UnsupportedMediaTypeError):
            client.client_sessions.create(allowed_origin="https://app.example.com")

    def test_a_session_token_works_as_the_bearer_for_job_creation(self, make: MakeClient, golden: Golden) -> None:
        golden.expect("upload-ticket-session", "upload-put-pdf", "pdf-job-session")
        make(token=SESSION_TOKEN).pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="analyze")


class TestProblems:
    @pytest.mark.parametrize(
        ("name", "error", "status", "code", "options"),
        [
            ("pdf-job-unauthorized", AuthenticationError, 401, "UNAUTHORIZED", {"mode": "fix"}),
            ("pdf-job-feature-off", NotFoundError, 404, "NOT_FOUND", {"mode": "fix"}),
            ("pdf-job-idempotency-key-reused", ConflictError, 409, "IDEMPOTENCY_KEY_REUSED", {"mode": "analyze"}),
            ("pdf-job-unsupported-input", UnsupportedMediaTypeError, 415, "UNSUPPORTED_INPUT", {"mode": "fix"}),
            ("pdf-job-validation-error", ValidationError, 422, "VALIDATION_ERROR", {"mode": "analyze", "explicit_title": "x"}),
            ("pdf-job-invalid-json", ValidationError, 400, "INVALID_JSON", {"mode": "fix"}),
        ],
    )
    def test_pdf_creation_problems(
        self, client: Any, golden: Golden, name: str, error: type[ApiError], status: int, code: str, options: dict[str, Any]
    ) -> None:
        golden.expect(*UPLOAD_PDF, name)
        with pytest.raises(error) as caught:
            client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", **options)
        assert (caught.value.status, caught.value.code) == (status, code)
        assert type(caught.value) is error

    def test_403_insufficient_scope_on_a_creation(self, make: MakeClient, golden: Golden) -> None:
        golden.expect("upload-ticket-session", "upload-put-pdf", "pdf-job-insufficient-scope")
        with pytest.raises(PermissionDeniedError) as caught:
            make(token=SESSION_TOKEN).pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix")
        assert caught.value.code == "INSUFFICIENT_SCOPE"

    def test_403_insufficient_scope_on_a_read(self, make: MakeClient, golden: Golden) -> None:
        golden.expect("job-insufficient-scope")
        with pytest.raises(PermissionDeniedError) as caught:
            make(token=SESSION_TOKEN).jobs.get(JOB_ID)
        assert caught.value.code == "INSUFFICIENT_SCOPE"

    def test_401_and_404_on_a_read(self, client: Any, golden: Golden) -> None:
        golden.expect("job-unauthorized", "job-not-found")
        with pytest.raises(AuthenticationError):
            client.jobs.get(JOB_ID)
        with pytest.raises(NotFoundError):
            client.jobs.get(OTHER_JOB_ID)

    def test_421_is_a_host_not_allowed_error(self, client: Any, golden: Golden) -> None:
        golden.expect("web-job-host-not-allowed")
        with pytest.raises(HostNotAllowedError) as caught:
            client.web.create_scan(SCAN)
        assert caught.value.code == "HOST_NOT_ALLOWED"

    def test_a_rejected_scan_url_is_a_validation_error(self, client: Any, golden: Golden) -> None:
        golden.expect("web-job-url-rejected")
        with pytest.raises(ValidationError):
            client.web.create_scan("https://internal.invalid/")

    def test_429_session_limits_are_rate_limit_errors_without_retry_after_and_never_retried(self, make: MakeClient, golden: Golden) -> None:
        session = make(token=SESSION_TOKEN)
        golden.expect("upload-ticket-session", "upload-put-pdf", "pdf-job-session-job-limit")
        with pytest.raises(RateLimitError) as jobs:
            session.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix")
        assert (jobs.value.code, jobs.value.retry_after) == ("SESSION_JOB_LIMIT", None)
        golden.expect("upload-ticket-session-limit")
        with pytest.raises(RateLimitError) as uploads:
            session.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix")
        assert uploads.value.code == "SESSION_UPLOAD_LIMIT"

    def test_503_queue_full_is_a_service_unavailable_error_with_retry_after_and_is_not_retried(self, client: Any, golden: Golden) -> None:
        golden.expect(*UPLOAD_PDF, "pdf-job-queue-full")
        with pytest.raises(ServiceUnavailableError) as caught:
            client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix")
        e = caught.value
        assert isinstance(e, ServerError)
        assert (e.code, e.retry_after) == ("QUEUE_FULL", 30.0)
        assert sum(1 for x in golden.exchanges if x.fixture and x.fixture["name"] == "pdf-job-queue-full") == 1

    def test_422_file_too_large_on_the_upload_ticket(self, client: Any, golden: Golden) -> None:
        answer = FIXTURES["upload-ticket-too-large"]["response"]
        golden.expect(Reply(answer["status"], answer["body"], answer["headers"]))
        with pytest.raises(ValidationError) as caught:
            client.uploads.upload(PDF_BYTES, "application/pdf", "huge.pdf")
        assert caught.value.code == "FILE_TOO_LARGE"

    def test_an_errors_text_never_contains_the_credential(self, client: Any, golden: Golden) -> None:
        golden.expect("job-unauthorized")
        with pytest.raises(AuthenticationError) as caught:
            client.jobs.get(JOB_ID)
        assert SECRET_KEY not in str(caught.value) + repr(caught.value) + repr(vars(caught.value))

    def test_a_network_error_text_never_contains_the_credential(self, client: Any, golden: Golden) -> None:
        golden.expect(*[Fail(httpx.ConnectError(f"cannot connect with {SECRET_KEY}")) for _ in range(3)])
        with pytest.raises(httpx.ConnectError) as caught:
            client.info()
        assert SECRET_KEY not in str(caught.value)


class TestNewerCore:
    def test_keeps_unknown_enum_values_as_they_are(self, client: Any, golden: Golden) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/artifacts"):
                item = {
                    "id": "a",
                    "kind": "stamped_copy",
                    "sha256": "0" * 64,
                    "sizeBytes": 1,
                    "downloadUrl": "https://x.invalid/a",
                    "expiresAt": "2030-01-01T00:00:00Z",
                }
                return httpx.Response(200, json={"items": [item]})
            if path.endswith("/findings"):
                item = {"source": "epub", "ruleId": "X", "message": "m", "needsHumanReview": True, "resolution": "needs_a_person"}
                return httpx.Response(200, json={"items": [item]})
            if path.endswith("/actions"):
                return httpx.Response(200, json={"items": [{"ruleId": "R", "outcome": "deferred", "reason": "r"}]})
            if path.endswith("/v1/info"):
                return httpx.Response(200, json={"features": ["pdf", "ocr"]})
            validation = {"profile": "pdfua2", "status": "partial", "failedRules": []}
            fields = {
                "kind": "epub_to_pdf",
                "mode": "polish",
                "state": "paused",
                "remediationStatus": "queued_for_review",
                "validation": validation,
                "futureField": 1,
            }
            return httpx.Response(200, json=job_json(**fields))

        golden.default = respond
        fetched = client.jobs.get(JOB_ID)
        assert (fetched.kind, fetched.mode, fetched.state, fetched.remediation_status) == (
            "epub_to_pdf",
            "polish",
            "paused",
            "queued_for_review",
        )
        assert fetched.validation.status == "partial"
        assert client.jobs.artifacts(JOB_ID)[0].kind == "stamped_copy"
        finding = client.jobs.findings(JOB_ID)[0]
        assert (finding.source, finding.resolution) == ("epub", "needs_a_person")
        assert client.jobs.actions(JOB_ID)[0].outcome == "deferred"
        assert client.info().features == ("pdf", "ocr")

    def test_wait_treats_an_unknown_state_as_final_instead_of_polling_forever(self, client: Any, golden: Golden) -> None:
        golden.expect(Reply(200, job_json(state="paused")))
        done = client.jobs.wait(JOB_ID)
        assert (done.state, done.is_finished) == ("paused", True)

    def test_an_unknown_problem_code_or_status_still_becomes_an_api_error_that_keeps_the_code(self, client: Any, golden: Golden) -> None:
        body = problem_json(418, "BREWING", errors=[{"path": "/x", "code": "NEW_RULE", "message": "m"}, {"nonsense": True}])
        golden.expect(Reply(418, body, {"Content-Type": "application/problem+json"}))
        with pytest.raises(ApiError) as caught:
            client.jobs.get(JOB_ID)
        e = caught.value
        assert type(e) is ApiError
        assert (e.status, e.code) == (418, "BREWING")
        assert [(p.path, p.code, p.message) for p in e.errors] == [("/x", "NEW_RULE", "m")]

    def test_a_body_that_is_not_a_problem_still_becomes_an_api_error(self, client: Any, golden: Golden) -> None:
        golden.default = lambda request: httpx.Response(502, content=b"<html>bad gateway</html>")
        with pytest.raises(ServerError) as caught:
            client.jobs.get(JOB_ID)
        assert caught.value.status == 502
        assert caught.value.code == "INTERNAL_ERROR"

    def test_a_success_body_that_is_not_a_job_is_a_server_error_not_a_key_error(self, client: Any, golden: Golden) -> None:
        golden.expect(Reply(200, {"unexpected": True}))
        with pytest.raises(ServerError, match="Malformed"):
            client.jobs.get(JOB_ID)
        golden.expect(Reply(200, None))
        with pytest.raises(ServerError, match="Malformed"):
            client.jobs.get(JOB_ID)


class TestRetries:
    def test_a_creation_repeated_after_a_lost_response_carries_the_same_key_and_the_server_replays(
        self, client: Any, golden: Golden, sleeps: list[float]
    ) -> None:
        golden.expect(*UPLOAD_PDF, Lost("pdf-job-idempotent-replay"), "pdf-job-idempotent-replay")
        metas: list[ResponseMeta] = []
        created = client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix", on_response=metas.append)
        assert created.id == JOB_ID
        first, second = last(golden, 2)
        assert [first.path, second.path] == ["/v1/pdf/jobs", "/v1/pdf/jobs"]
        assert second.headers["idempotency-key"] == first.headers["idempotency-key"]
        assert second.request.content == first.request.content
        assert metas[0].idempotent_replayed is True
        assert sleeps == [0.25]

    def test_gives_up_after_two_repeats_and_surfaces_the_network_error(self, client: Any, golden: Golden, sleeps: list[float]) -> None:
        golden.expect(*[Fail(httpx.ConnectError("down")) for _ in range(3)])
        with pytest.raises(httpx.ConnectError, match="down"):
            client.web.create_scan(SCAN)
        assert len(golden.exchanges) == 3
        assert sleeps == [0.25, 0.5]

    def test_every_creation_carries_a_key_so_a_failed_first_attempt_is_repeated(self, client: Any, golden: Golden) -> None:
        golden.expect(Fail(httpx.ConnectError("down")), "web-job-accepted")
        client.web.create_scan(SCAN)
        assert all("idempotency-key" in x.headers for x in golden.exchanges)

    def test_a_client_session_mint_has_no_key_and_is_not_repeated(self, client: Any, golden: Golden) -> None:
        golden.expect(Fail(httpx.ConnectError("down")))
        with pytest.raises(httpx.ConnectError):
            client.client_sessions.create(allowed_origin="https://app.example.com")
        assert len(golden.exchanges) == 1

    def test_retries_a_read_after_a_503_with_retry_after_but_never_a_creation(
        self, client: Any, golden: Golden, sleeps: list[float]
    ) -> None:
        golden.expect(Reply(503, problem_json(503, "QUEUE_FULL"), {"Retry-After": "0"}), "job-completed")
        assert client.jobs.get(JOB_ID).state == "completed"
        assert sleeps == [0.0]
        golden.expect(Reply(503, problem_json(503, "QUEUE_FULL")))
        before = len(golden.exchanges)
        with pytest.raises(ServiceUnavailableError):
            client.web.create_scan(SCAN)
        assert len(golden.exchanges) - before == 1

    def test_a_read_gives_up_after_two_busy_retries(self, client: Any, golden: Golden, sleeps: list[float]) -> None:
        busy = Reply(429, problem_json(429, "SESSION_JOB_LIMIT"), {"Retry-After": "3"})
        golden.expect(busy, busy, busy)
        with pytest.raises(RateLimitError):
            client.jobs.get(JOB_ID)
        assert sleeps == [3.0, 3.0]

    def test_retry_after_may_be_an_http_date_and_is_capped_when_waiting_between_retries(
        self, client: Any, golden: Golden, sleeps: list[float]
    ) -> None:
        golden.expect(Reply(503, problem_json(503, "QUEUE_FULL"), {"Retry-After": "300"}), "job-completed")
        client.jobs.get(JOB_ID)
        assert sleeps == [10.0]
        golden.expect(Reply(503, problem_json(503, "QUEUE_FULL"), {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
        with pytest.raises(ServiceUnavailableError) as caught:
            client.web.create_scan(SCAN)
        assert caught.value.retry_after == 0.0

    def test_idempotency_keys_of_1_to_255_printable_ascii_and_derived_upload_keys_stay_within_the_limit(
        self, client: Any, golden: Golden
    ) -> None:
        key = "k" * 255
        golden.expect(*UPLOAD_PDF, "pdf-job-accepted")
        client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="analyze", idempotency_key=key)
        ticket, _, post = last(golden, 3)
        assert post.headers["idempotency-key"] == key
        assert len(ticket.headers["idempotency-key"]) <= 255
        assert ticket.headers["idempotency-key"] != key

    @pytest.mark.parametrize("bad", ["", "   ", "k" * 256, "tab\t", "é", "new\nline"])
    def test_rejects_unusable_idempotency_keys_before_any_request(self, client: Any, bad: str) -> None:
        with pytest.raises(ConfigurationError, match="idempotency_key"):
            client.web.create_scan(SCAN, idempotency_key=bad)
        with pytest.raises(ConfigurationError, match="idempotency_key"):
            client.pdf.create_job(PDF_BYTES, mode="fix", idempotency_key=bad)

    def test_interior_spaces_are_kept_and_the_key_is_sent_trimmed(self, client: Any, golden: Golden) -> None:
        golden.expect("web-job-accepted", "web-job-accepted")
        client.web.create_scan(SCAN, idempotency_key="two words-1")
        client.web.create_scan(SCAN, idempotency_key="  padded  ")
        assert [x.headers["idempotency-key"] for x in golden.exchanges] == ["two words-1", "padded"]

    def test_a_caller_retry_with_the_same_key_survives_a_replayed_upload_ticket(self, client: Any, golden: Golden) -> None:
        key = "caller-retry-key"
        golden.expect(*UPLOAD_PDF, "pdf-job-queue-full")
        with pytest.raises(ServiceUnavailableError):
            client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix", idempotency_key=key)
        ticket = FIXTURES["upload-ticket-pdf"]["response"]
        replayed = Reply(201, json.loads(json.dumps(ticket["body"]).replace("{{baseUrl}}", BASE_URL)), {"Idempotent-Replayed": "true"})
        golden.expect(replayed, "upload-put-already-completed", "pdf-job-idempotent-replay")
        second = client.pdf.create_job(PDF_BYTES, file_name="sample.pdf", mode="fix", idempotency_key=key)
        assert second.state == "queued"
        keys = [x.headers["idempotency-key"] for x in golden.exchanges if x.request.url.path == "/v1/uploads"]
        assert keys[0] == keys[1]

    def test_a_409_on_the_put_without_a_replayed_ticket_is_still_an_error(self, client: Any, golden: Golden) -> None:
        golden.expect("upload-ticket-pdf", "upload-put-already-completed")
        with pytest.raises(StorageError) as caught:
            client.uploads.upload(PDF_BYTES, "application/pdf", "sample.pdf")
        assert caught.value.code == "UPLOAD_ALREADY_COMPLETED"

    def test_does_not_repeat_a_post_with_wait_after_a_request_timeout_but_repeats_a_network_failure(
        self, client: Any, golden: Golden
    ) -> None:
        golden.expect(Fail(httpx.ReadTimeout("t")))
        with pytest.raises(httpx.ReadTimeout):
            client.web.create_scan(SCAN, wait=5)
        assert len(golden.exchanges) == 1
        golden.exchanges.clear()
        golden.expect(*[Fail(httpx.ReadTimeout("t")) for _ in range(3)])
        with pytest.raises(httpx.ReadTimeout):
            client.web.create_scan(SCAN)
        assert len(golden.exchanges) == 3
        golden.exchanges.clear()
        golden.expect(*[Fail(httpx.ConnectError("down")) for _ in range(3)])
        with pytest.raises(httpx.ConnectError):
            client.web.create_scan(SCAN, wait=5)
        assert len(golden.exchanges) == 3


def test_the_golden_upload_bytes_match_the_hash_in_the_ticket_request() -> None:
    assert hashlib.sha256(PDF_BYTES).hexdigest() == body_of("upload-ticket-pdf")["sha256"]


class TestUploadRetries:
    def test_a_lost_ticket_answer_is_repeated_with_the_same_key_and_the_same_request(
        self, client: Any, golden: Golden, sleeps: list[float]
    ) -> None:
        golden.expect(Lost("upload-ticket-pdf"), "upload-ticket-pdf", "upload-put-pdf")
        ticket = client.uploads.upload(PDF_BYTES, "application/pdf", "sample.pdf")
        assert ticket.upload_id == "11111111-1111-4111-8111-111111111111"
        first, second = golden.exchanges[:2]
        assert first.headers["idempotency-key"] == second.headers["idempotency-key"]
        assert first.request.content == second.request.content
        assert sleeps == [0.25]


class TestRemainingFixtures:
    def test_a_job_without_artifacts_has_an_empty_list(self, client: Any, golden: Golden) -> None:
        golden.expect("artifacts-empty")
        assert client.jobs.artifacts(OTHER_JOB_ID) == []

    def test_downloads_the_remediated_copy_with_its_own_hash(self, client: Any, golden: Golden) -> None:
        golden.expect("artifact-download-remediated")
        copy = artifact_for("artifact-download-remediated", kind="remediated")
        assert hashlib.sha256(client.artifacts.download(copy)).hexdigest() == copy.sha256


def test_every_golden_fixture_is_replayed_by_some_test() -> None:
    sources = "".join(p.read_text(encoding="utf8") for p in Path(__file__).parent.glob("test_*.py"))
    unused = [name for name in FIXTURES if f'"{name}"' not in sources]
    assert unused == []
