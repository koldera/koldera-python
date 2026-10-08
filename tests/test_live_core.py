"""Runs only against a real koldera-core; skipped unless KOLDERA_URL and KOLDERA_TOKEN are set.

KOLDERA_URL=http://localhost:8080 KOLDERA_TOKEN=... uv run pytest tests/test_live_core.py -s
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from koldera import KolderaClient

pytestmark = pytest.mark.skipif(
    not (os.environ.get("KOLDERA_URL") and os.environ.get("KOLDERA_TOKEN")),
    reason="set KOLDERA_URL and KOLDERA_TOKEN to run against a real koldera-core",
)

SAMPLE = Path(__file__).parent / "fixtures" / "synthetic-report.pdf"
HTML = b'<!doctype html><html lang="en"><head><title>Synthetic page</title></head><body><h1>Synthetic page</h1></body></html>'


def test_health_and_info() -> None:
    with KolderaClient() as client:
        assert client.health().status == "ok"
        assert "pdf" in client.info().features


def test_analyze_and_fix_synthetic_pdf() -> None:
    with KolderaClient() as client:
        analyzed = client.pdf.create_job(SAMPLE, mode="analyze", wait=60, until_finished=True)
        assert analyzed.state == "completed"
        assert analyzed.manual_review_required is True
        assert client.jobs.findings(analyzed.id)

        fixed = client.pdf.create_job(SAMPLE, mode="fix", explicit_language="en", wait=120, until_finished=True)
        assert fixed.state in ("completed", "failed")
        assert fixed.manual_review_required is True
        remediated = [a for a in client.jobs.artifacts(fixed.id) if a.kind == "remediated"]
        for artifact in remediated:
            assert hashlib.sha256(client.artifacts.download(artifact)).hexdigest() == artifact.sha256
        validation = analyzed.validation.status if analyzed.validation else None
        final = fixed.final_validation.status if fixed.final_validation else None
        print(f"analyze {analyzed.id} {validation}; fix {fixed.id} {fixed.state} {fixed.remediation_status} final {final}")
        print(f"remediated sha {[a.sha256 for a in remediated]}")


def test_html_to_tagged_pdf() -> None:
    with KolderaClient() as client:
        job = client.html_to_pdf.create_job(HTML, mode="generate_analyze", wait=120, until_finished=True)
        assert job.state == "completed"
        [pdf] = [a for a in client.jobs.artifacts(job.id) if a.kind == "generated_pdf"]
        assert hashlib.sha256(client.artifacts.download(pdf)).hexdigest() == pdf.sha256
        print(f"html {job.id} {job.validation.status if job.validation else None}")


def test_structured_document_to_tagged_pdf() -> None:
    with KolderaClient() as client:
        job = client.documents.create_job(
            {
                "title": "Synthetic note",
                "language": "en",
                "body": [
                    {"type": "heading", "level": 1, "text": "Synthetic note"},
                    {"type": "paragraph", "content": ["Invented text for a live test."]},
                ],
            },
            wait=120,
            until_finished=True,
        )
        assert job.state == "completed"
        [pdf] = [a for a in client.jobs.artifacts(job.id) if a.kind == "generated_pdf"]
        assert hashlib.sha256(client.artifacts.download(pdf)).hexdigest() == pdf.sha256
        print(f"document {job.id} {job.validation.status if job.validation else None}")
