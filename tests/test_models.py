import base64
import os
from datetime import timedelta

import pytest

from conftest import make_doc
from pagr.exceptions import PagrDecodeError
from pagr.models._common import parse_dt
from pagr.models.organisation import OrgStats
from pagr.models.render import (
    BatchRenderResult,
    PdfDocument,
    PdfRenderResult,
    RenderCompletion,
    RenderIssue,
    RenderIssueSeverity,
    RenderIssueType,
    RenderProgress,
    RenderResult,
    RenderedDocument,
    RenderJob,
    RenderJobState,
    RenderJobStatus,
    RenderOutcome,
    parse_callback,
)
from pagr.models.template import Template, TemplateVersion
from pagr.models.validation import ValidationResponse


def _issue(index, *, severity="Error", type="MissingBinding", description="bad field"):
    return {
        "type": type,
        "severity": severity,
        "description": description,
        "documentIndex": index,
    }


# ── RenderIssue parsing ──────────────────────────────────────────────────────

def test_render_issue_parses_enums():
    issue = RenderIssue.from_api(
        {"type": "MissingBinding", "severity": "Warning", "description": "x", "elementId": "e1", "documentIndex": 2}
    )
    assert issue.type is RenderIssueType.MISSING_BINDING
    assert issue.severity is RenderIssueSeverity.WARNING
    assert issue.element_id == "e1"
    assert issue.document_index == 2
    assert not issue.is_error


def test_render_issue_unknown_type_and_severity_fail_safe():
    issue = RenderIssue.from_api({"type": "SomethingNew", "severity": "Bogus", "description": "x"})
    assert issue.type is RenderIssueType.UNKNOWN
    # unknown severity fails closed to ERROR
    assert issue.severity is RenderIssueSeverity.ERROR


# ── Single render result ─────────────────────────────────────────────────────

def test_render_result_reads_issues():
    data = {
        "status": "partial",
        "renderedCount": 0,
        "requestedCount": 1,
        "missingCount": 1,
        "documents": [],
        "issues": [_issue(0, description="Missing required field")],
    }
    result = RenderResult.from_api(data)
    assert not result.ok
    assert result.missing_count == 1
    assert len(result.issues) == 1
    assert result.issues[0].is_error


# ── BatchRenderResult correlation ───────────────────────────────────────────

def test_batch_all_success():
    inputs = [{"Title": f"Doc {i}"} for i in range(3)]
    data = {
        "status": "ok",
        "renderedCount": 3,
        "requestedCount": 3,
        "missingCount": 0,
        "documents": [make_doc(f"Doc {i}", document_index=i) for i in range(3)],
    }
    result = BatchRenderResult.from_api(data, inputs=inputs)

    assert len(result) == 3
    assert result.ok
    assert not result.insufficient_credit
    assert [it.index for it in result] == [0, 1, 2]
    assert all(it.ok for it in result)
    assert result.failed == []
    assert len(result.succeeded) == 3
    assert result[0].input == {"Title": "Doc 0"}
    assert result[0].document.document_name == "Doc 0"


def test_batch_error_issue_correlates_by_index():
    inputs = [{"Title": f"Doc {i}"} for i in range(3)]
    data = {
        "status": "partial",
        "renderedCount": 2,
        "requestedCount": 3,
        "missingCount": 1,
        # rendered docs carry their own documentIndex (0 and 2); input 1 failed.
        "documents": [make_doc("Doc 0", document_index=0), make_doc("Doc 2", document_index=2)],
        "issues": [_issue(1, description="bad field")],
    }
    result = BatchRenderResult.from_api(data, inputs=inputs)

    assert not result.ok
    # failed item carries its exact input + error issue
    assert result[1].ok is False
    assert [i.description for i in result[1].issues] == ["bad field"]
    assert result[1].input == {"Title": "Doc 1"}
    # docs land on the slot each reports via documentIndex, not positionally
    assert result[0].document.document_name == "Doc 0"
    assert result[2].document.document_name == "Doc 2"
    assert [it.index for it in result.failed] == [1]


def test_batch_correlates_by_index_when_warning_blocked_leaves_a_gap():
    # Regression: a Warning-blocked production doc at index 1 renders nothing
    # yet carries NO Error-severity issue, so nothing marks its slot failed.
    # The old positional fill slid Doc 2's render up into slot 1; index-based
    # placement must keep it at slot 2 and leave slot 1 empty.
    inputs = [{"Title": f"Doc {i}"} for i in range(3)]
    data = {
        "status": "partial",
        "renderedCount": 2,
        "requestedCount": 3,
        "missingCount": 1,
        "documents": [make_doc("Doc 0", document_index=0), make_doc("Doc 2", document_index=2)],
        # a non-blocking Warning on the middle doc — not an Error
        "issues": [_issue(1, severity="Warning", description="missing binding")],
    }
    result = BatchRenderResult.from_api(data, inputs=inputs)

    assert result[0].document.document_name == "Doc 0"
    assert result[2].document.document_name == "Doc 2"
    # slot 1 stayed empty (the warning is attached; not misfilled with Doc 2)
    assert result[1].document is None
    assert [i.description for i in result[1].issues] == ["missing binding"]


def test_batch_insufficient_credit_is_data():
    inputs = [{"Title": f"Doc {i}"} for i in range(3)]
    data = {
        "status": "insufficient_credit",
        "message": "out of credit",
        "renderedCount": 2,
        "requestedCount": 3,
        "missingCount": 1,
        "documents": [make_doc("Doc 0", document_index=0), make_doc("Doc 1", document_index=1)],
    }
    result = BatchRenderResult.from_api(data, inputs=inputs)

    assert result.insufficient_credit
    assert not result.ok
    assert result.message == "out of credit"
    # the un-rendered tail is surfaced as a failed item
    assert result[2].ok is False
    assert [i.description for i in result[2].issues] == ["not rendered"]


def test_batch_without_inputs_uses_requested_count():
    data = {
        "status": "partial",
        "renderedCount": 1,
        "requestedCount": 2,
        "missingCount": 1,
        "documents": [make_doc("Doc 0", document_index=0)],
    }
    result = BatchRenderResult.from_api(data)  # no inputs provided

    assert len(result) == 2
    assert result[0].document.document_name == "Doc 0"
    assert result[0].input is None
    assert [i.description for i in result[1].issues] == ["not rendered"]


def test_batch_drops_documents_without_an_index():
    # documentIndex is the only correlation: the API guarantees it on every
    # document, so one that arrives without it (or out of range) is dropped
    # rather than guessed onto a slot by position.
    inputs = [{"Title": f"Doc {i}"} for i in range(3)]
    data = {
        "status": "partial",
        "renderedCount": 2,
        "requestedCount": 3,
        "documents": [make_doc("Doc 0"), make_doc("Doc 2", document_index=99)],
        "issues": [_issue(1, description="bad field")],
    }
    result = BatchRenderResult.from_api(data, inputs=inputs)

    assert [it.document for it in result] == [None, None, None]
    assert [i.description for i in result[0].issues] == ["not rendered"]
    assert [i.description for i in result[1].issues] == ["bad field"]


def test_batch_ok_is_derived_from_the_counts():
    # ok reads missing_count (requested − rendered), not the items: a slot that
    # came back without a document does not by itself make the batch not-ok.
    data = {
        "status": "ok",
        "renderedCount": 2,
        "requestedCount": 2,
        "documents": [make_doc("Doc 0", document_index=0)],
    }
    result = BatchRenderResult.from_api(data)

    assert result.missing_count == 0
    assert result.ok
    assert [it.index for it in result.failed] == [1]  # per-item view still honest


def test_batch_missing_count_is_computed_not_read():
    # The server's own missingCount is ignored — the field is by definition
    # requestedCount − renderedCount, so a response disagreeing with itself
    # cannot report a batch that dropped a document as ok.
    data = {
        "status": "ok",
        "renderedCount": 2,
        "requestedCount": 3,
        "missingCount": 0,  # inconsistent with the counts above
        "documents": [make_doc(f"Doc {i}", document_index=i) for i in range(2)],
    }
    result = BatchRenderResult.from_api(data)

    assert result.missing_count == 1
    assert not result.ok


# ── Validation parsing (flat issues list) ────────────────────────────────────

def test_validation_response_parses_issues():
    data = {
        "issues": [
            {"type": "MissingBinding", "severity": "Error", "description": "Missing required field: customerName", "documentIndex": 1},
            {"type": "UnformattedValue", "severity": "Information", "description": "unusual date format", "documentIndex": 1},
        ]
    }
    resp = ValidationResponse.from_api(data)

    assert not resp.is_valid  # an Error-severity issue is present
    assert len(resp) == 2
    assert len(resp.errors) == 1
    assert [i.document_index for i in resp.issues_for(1)] == [1, 1]


def test_validation_response_valid_when_no_error_issue():
    # only a non-blocking Information issue → still valid. This is the exact
    # regression the issues-contract migration fixes.
    data = {"issues": [{"type": "UnformattedValue", "severity": "Information", "description": "x", "documentIndex": 0}]}
    resp = ValidationResponse.from_api(data)
    assert resp.is_valid


def test_validation_empty_is_valid():
    resp = ValidationResponse.from_api({"issues": []})
    assert resp.is_valid
    assert len(resp) == 0


def test_validation_response_invalid_when_warning_only():
    # is_valid is the production gate: any issue >= Warning invalidates,
    # even with no Error present.
    data = {"issues": [{"type": "MissingBinding", "severity": "Warning", "description": "x", "documentIndex": 0}]}
    resp = ValidationResponse.from_api(data)
    assert not resp.is_valid
    assert len(resp.errors) == 0
    assert len(resp.warnings) == 1


# ── Callback parsing ─────────────────────────────────────────────────────────

def test_parse_callback_routes_progress():
    payload = {
        "jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "processed": 2,
        "requestedCount": 5,
        "documentIndex": 7,
        "document": make_doc("Doc 2", document_index=7),
    }
    cb = parse_callback(payload)
    assert isinstance(cb, RenderProgress)
    assert cb.processed == 2
    assert cb.requested_count == 5
    assert cb.document_index == 7
    assert cb.progress_pct == 40.0
    assert cb.document.document_name == "Doc 2"


def test_parse_callback_routes_completion():
    payload = {
        "jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "state": "completed",
        "status": "partial",
        "renderedCount": 5,
        "requestedCount": 6,
        "missingCount": 1,
        "message": None,
        "issues": [_issue(4, severity="Warning", description="skipped")],
    }
    cb = parse_callback(payload)
    assert isinstance(cb, RenderCompletion)
    assert cb.state is RenderJobState.COMPLETED
    assert cb.status is RenderOutcome.PARTIAL
    assert not cb.ok
    assert cb.rendered_count == 5
    assert cb.missing_count == 1
    assert [i.description for i in cb.issues] == ["skipped"]


def test_render_job_parses_camelcase():
    job = RenderJob.from_api(
        {"jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "requestedCount": 10, "state": "queued"}
    )
    assert str(job.job_id) == "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    assert job.requested_count == 10
    assert job.state is RenderJobState.QUEUED


def test_render_job_status_parses_z_suffix_timestamps():
    status = RenderJobStatus.from_api(
        {
            "jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "state": "completed",
            "status": "ok",
            "renderedCount": 1,
            "requestedCount": 1,
            "missingCount": 0,
            "startedAt": "2026-07-16T10:00:00Z",
            "completedAt": "2026-07-16T10:00:05Z",
        }
    )
    assert status.started_at.tzinfo is not None
    assert status.completed_at.tzinfo is not None
    assert status.started_at.isoformat() == "2026-07-16T10:00:00+00:00"


def test_render_job_status_parses():
    status = RenderJobStatus.from_api(
        {
            "jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "state": "completed",
            "status": "partial",
            "renderedCount": 3,
            "requestedCount": 4,
            "missingCount": 1,
            "startedAt": "2026-07-16T10:00:00",
            "completedAt": "2026-07-16T10:00:05",
            "failureReason": None,
            "issues": [_issue(3, description="blocked")],
        }
    )
    assert status.done
    assert status.state is RenderJobState.COMPLETED
    assert status.status is RenderOutcome.PARTIAL
    assert not status.ok  # completed but outcome is partial, not ok
    assert status.rendered_count == 3
    assert status.missing_count == 1
    assert [i.description for i in status.issues] == ["blocked"]
    assert status.completed_at is not None


def test_render_job_status_pending_has_null_outcome():
    status = RenderJobStatus.from_api(
        {
            "jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "state": "pending",
            "status": None,
            "renderedCount": 0,
            "requestedCount": 5,
            "missingCount": 5,
            "startedAt": "2026-07-16T10:00:00",
        }
    )
    assert not status.done
    assert status.status is None
    assert not status.ok


# ── RenderedDocument bytes/save ──────────────────────────────────────────────

def test_rendered_document_to_bytes_and_save(tmp_path):
    payload = base64.b64encode(b"hello pdf").decode()
    doc = RenderedDocument.from_api(make_doc("file.pdf", base64=payload))
    assert doc.to_bytes() == b"hello pdf"

    out = doc.save(str(tmp_path))
    assert out.endswith("file.pdf")
    with open(out, "rb") as f:
        assert f.read() == b"hello pdf"


def test_rendered_document_null_id_and_view_url_when_not_persisted():
    # persist=False renders come back as JSON with id/viewUrl null — from_api
    # must not choke on UUID(None).
    data = make_doc("Doc", base64=base64.b64encode(b"pdf").decode())
    data["id"] = None
    data["viewUrl"] = None
    doc = RenderedDocument.from_api(data)
    assert doc.id is None
    assert doc.view_url is None
    assert doc.to_bytes() == b"pdf"


def test_rendered_document_reads_document_index():
    doc = RenderedDocument.from_api(make_doc("Doc", document_index=3))
    assert doc.document_index == 3


def test_rendered_document_to_bytes_without_content_raises():
    doc = RenderedDocument.from_api(make_doc("file.pdf"))
    with pytest.raises(ValueError):
        doc.to_bytes()


# ── save(): filename safety + extension handling ─────────────────────────────

def test_save_strips_path_traversal_from_document_name(tmp_path):
    target = tmp_path / "out"
    target.mkdir()
    doc = RenderedDocument.from_api(
        make_doc("../../evil_outside", base64=base64.b64encode(b"pdf").decode())
    )
    out = doc.save(str(target))
    # The write stays inside the target directory; the traversal is neutralised.
    assert os.path.abspath(out) == str(target / "evil_outside.pdf")
    assert (target / "evil_outside.pdf").read_bytes() == b"pdf"
    # And nothing escaped to an ancestor directory.
    assert not (tmp_path / "evil_outside.pdf").exists()


def test_save_ignores_absolute_document_name(tmp_path):
    target = tmp_path / "out"
    target.mkdir()
    doc = RenderedDocument.from_api(
        make_doc("/etc/passwd", base64=base64.b64encode(b"pdf").decode())
    )
    out = doc.save(str(target))
    # An absolute-looking name must not redirect the write out of the directory.
    assert os.path.abspath(out) == str(target / "passwd.pdf")


def test_save_appends_pdf_when_name_has_a_dotted_segment(tmp_path):
    # "Invoice 1.0" was previously mistaken for an already-extensioned name and
    # saved with no .pdf suffix.
    doc = RenderedDocument.from_api(
        make_doc("Invoice 1.0", base64=base64.b64encode(b"pdf").decode())
    )
    out = doc.save(str(tmp_path))
    assert out.endswith("Invoice 1.0.pdf")


def test_save_does_not_double_a_pdf_extension(tmp_path):
    doc = RenderedDocument.from_api(
        make_doc("report.PDF", base64=base64.b64encode(b"pdf").decode())
    )
    out = doc.save(str(tmp_path))
    assert out.endswith("report.PDF")
    assert not out.lower().endswith(".pdf.pdf")


# ── Timestamp parsing (always tz-aware) ──────────────────────────────────────

def test_parse_dt_naive_value_is_utc_aware():
    dt = parse_dt("2026-06-10T12:34:56")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_dt_preserves_explicit_offset():
    assert parse_dt("2026-06-10T12:34:56+02:00").utcoffset() == timedelta(hours=2)
    assert parse_dt("2026-06-10T12:34:56Z").utcoffset() == timedelta(0)


def test_rendered_document_rendered_at_is_tz_aware():
    # The make_doc fixture uses a naive "renderedAt"; the parsed value must
    # still be tz-aware so callers can compare it against aware datetimes.
    doc = RenderedDocument.from_api(make_doc("d"))
    assert doc.rendered_at.tzinfo is not None


# ── PdfDocument / PdfRenderResult (raw-PDF stream) ───────────────────────────

def _pdf_response(status=200, content=b"%PDF-1.7 body", headers=None):
    import httpx

    base = {"content-type": "application/pdf"}
    if headers:
        base.update(headers)
    return httpx.Response(status, content=content, headers=base)


def test_pdf_document_from_response_reads_headers():
    resp = _pdf_response(
        headers={
            "Content-Disposition": 'attachment; filename="Invoice 2026-001.pdf"',
            "X-Pagr-Document-Id": "550e8400-e29b-41d4-a716-446655440000",
            "X-Pagr-Page-Count": "3",
            "X-Pagr-Render-Duration-Ms": "412.7",
            "X-Pagr-View-Url": "https://example.test/doc",
            "X-Pagr-Issue-Count": "1",
        }
    )
    doc = PdfDocument.from_response(resp)
    assert doc.document_name == "Invoice 2026-001"  # .pdf stripped
    assert str(doc.document_id) == "550e8400-e29b-41d4-a716-446655440000"
    assert doc.page_count == 3
    assert doc.render_duration == 412.7
    assert doc.view_url == "https://example.test/doc"
    assert doc.issue_count == 1
    assert doc.to_bytes() == b"%PDF-1.7 body"


def test_pdf_document_matches_the_filename_marker_case_insensitively():
    # HTTP header parameter names are case-insensitive (RFC 6266), so a `Filename=`
    # must not fall back to "document" and silently lose the name. All six SDKs
    # agree on this; Python was case-sensitive until 2026-08-10.
    for marker in ("filename", "Filename", "FILENAME", "fileName"):
        resp = _pdf_response(
            headers={"Content-Disposition": f'attachment; {marker}="Invoice 2026-001.pdf"'}
        )
        assert PdfDocument.from_response(resp).document_name == "Invoice 2026-001"


def test_pdf_document_from_response_without_persist_headers():
    # persist=False: no stored id / view url, no Content-Disposition.
    resp = _pdf_response()
    doc = PdfDocument.from_response(resp)
    assert doc.document_id is None
    assert doc.view_url is None
    assert doc.document_name == "document"  # fallback
    assert doc.page_count == 0


def test_pdf_document_save(tmp_path):
    resp = _pdf_response(
        content=b"%PDF bytes",
        headers={"Content-Disposition": 'attachment; filename="out.pdf"'},
    )
    doc = PdfDocument.from_response(resp)
    out = doc.save(str(tmp_path))
    assert out.endswith("out.pdf")
    with open(out, "rb") as f:
        assert f.read() == b"%PDF bytes"


def test_pdf_render_result_from_error_envelope():
    result = PdfRenderResult.from_error_envelope(
        {
            "status": "failed",
            "message": "blocked",
            "issues": [_issue(0, description="dangerous content")],
        }
    )
    assert result.ok is False
    assert result.status == "failed"
    assert [i.description for i in result.issues] == ["dangerous content"]


# ── Severity ordering (is_at_least / is_blocking_production) ──────────────────


def test_severity_is_at_least_orders_information_warning_error():
    S = RenderIssueSeverity
    assert S.ERROR.is_at_least(S.WARNING)
    assert S.WARNING.is_at_least(S.WARNING)
    assert not S.INFORMATION.is_at_least(S.WARNING)
    assert not S.WARNING.is_at_least(S.ERROR)


def test_severity_is_blocking_production():
    S = RenderIssueSeverity
    assert S.WARNING.is_blocking_production
    assert S.ERROR.is_blocking_production
    assert not S.INFORMATION.is_blocking_production


# ── Fail-open job enums ───────────────────────────────────────────────────────


def test_render_job_state_unknown_is_terminal():
    st = RenderJobState.from_api("cancelled")  # not a known value
    assert st is RenderJobState.UNKNOWN
    assert st.is_terminal  # fail-open: unknown ends a poll loop


def test_render_job_status_unknown_state_is_done():
    status = RenderJobStatus.from_api(
        {
            "jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "state": "expired",  # brand-new server state
            "status": "ok",
            "renderedCount": 1,
            "requestedCount": 1,
            "missingCount": 0,
            "startedAt": "2026-07-16T10:00:00",
        }
    )
    assert status.state is RenderJobState.UNKNOWN
    assert status.done  # would otherwise spin forever


def test_render_outcome_unknown_fails_open():
    assert RenderOutcome.from_api("brand_new") is RenderOutcome.UNKNOWN


# ── parse_callback shape validation ───────────────────────────────────────────


def test_parse_callback_rejects_unknown_shape():
    # Neither a progress (no document) nor a valid completion (no state/status).
    with pytest.raises(PagrDecodeError):
        parse_callback({"jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479"})


def test_parse_callback_rejects_non_dict():
    with pytest.raises(PagrDecodeError):
        parse_callback(["not", "a", "dict"])  # type: ignore[arg-type]


def test_render_progress_from_api_missing_field_raises():
    with pytest.raises(PagrDecodeError):
        # has "document" (routes to progress) but missing processed/etc.
        parse_callback({"jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
                        "document": make_doc("x")})


# ── OrgStats: missing vs zero ─────────────────────────────────────────────────


def test_org_stats_missing_fields_are_none_not_zero():
    stats = OrgStats.from_api({"organisationName": "Acme", "tier": "pro"})
    # A field the server omitted is None, not a misleading 0.
    assert stats.pages_available is None
    assert stats.user_count is None
    assert stats.organisation_name == "Acme"


def test_org_stats_real_zero_is_preserved():
    stats = OrgStats.from_api({"pagesUsedThisPeriod": 0})
    assert stats.pages_used_this_period == 0


# ── Template / TemplateVersion decoding ───────────────────────────────────────


def _version_payload(**overrides):
    payload = {
        "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "versionNumber": 3,
        "templateJson": '{"elements":[]}',
        "templateId": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "raw",
    [
        None,                 # absent
        "",                   # empty
        "   ",                # blank
        "{not json",          # malformed
        "[1, 2]",             # non-object: array
        "42",                 # non-object: scalar
        "null",               # non-object: null
    ],
)
def test_template_version_sample_data_is_lenient(raw):
    # Empty, malformed, or non-object sampleData all decode to {} — never an
    # exception, and never a non-dict leaking through the `dict` annotation.
    version = TemplateVersion.from_api(_version_payload(sampleData=raw))
    assert version.sample_data == {}


def test_template_version_sample_data_parses_an_object():
    version = TemplateVersion.from_api(_version_payload(sampleData='{"customer": "Acme"}'))
    assert version.sample_data == {"customer": "Acme"}


@pytest.mark.parametrize("missing", ["id", "versionNumber", "templateJson", "templateId"])
def test_template_version_missing_required_field_raises_pagr_error(missing):
    payload = _version_payload()
    del payload[missing]
    with pytest.raises(PagrDecodeError):
        TemplateVersion.from_api(payload)


@pytest.mark.parametrize("missing", ["id", "name"])
def test_template_missing_required_field_raises_pagr_error(missing):
    payload = {"id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "name": "Invoice"}
    del payload[missing]
    with pytest.raises(PagrDecodeError):
        Template.from_api(payload)
