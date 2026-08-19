import asyncio  # noqa: E402
import math  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from uuid import UUID  # noqa: E402

import pytest

respx = pytest.importorskip("respx")
import httpx  # noqa: E402

from conftest import make_doc  # noqa: E402
from pagr import (  # noqa: E402
    ApiError,
    AuthenticationError,
    DEFAULT_BASE_URL,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    PagrConnectionError,
    PagrDecodeError,
    PagrError,
    PagrApiClient,
    PagrTimeoutError,
    RateLimitError,
    RenderJobState,
    RenderOutcome,
    ValidationFailedError,
)
from pagr._http import HttpTransport, _parse_retry_after  # noqa: E402

BASE = "http://api.test"
TID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
RENDER_URL = f"{BASE}/v1/render/{TID}/versions/1"
RENDER_LATEST_URL = f"{BASE}/v1/render/{TID}"
DID = "550e8400-e29b-41d4-a716-446655440000"
DOC_FILE_URL = f"{BASE}/v1/documents/{DID}/file"


def _pdf(status: int = 200) -> httpx.Response:
    """A PDF response, for exercising the GET download path in retry tests."""
    return httpx.Response(
        status, content=b"%PDF ok", headers={"content-type": "application/pdf"}
    )


def _no_backoff(client: PagrApiClient) -> PagrApiClient:
    """Make retries sleep instantly so tests don't wait on real backoff."""
    client._http.backoff_base = 0.0
    client._http.backoff_max = 0.0
    return client


@respx.mock
async def test_render_batch_correlates_inputs():
    respx.post(RENDER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "renderedCount": 2,
                "requestedCount": 2,
                "missingCount": 0,
                "documents": [
                    make_doc("Doc 0", document_index=0),
                    make_doc("Doc 1", document_index=1),
                ],
                "issues": [],
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        result = await client.render_batch(TID, [{"Title": "a"}, {"Title": "b"}], version=1)

    assert len(result) == 2
    assert result.ok
    assert result[0].input == {"Title": "a"}
    assert result[0].document.document_name == "Doc 0"


@respx.mock
async def test_render_latest_uses_versionless_path():
    route = respx.post(RENDER_LATEST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "renderedCount": 1,
                "requestedCount": 1,
                "missingCount": 0,
                "documents": [make_doc("Doc 0")],
                "issues": [],
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        result = await client.render(TID, {"Title": "a"})  # no version → latest

    assert route.called
    assert result.ok
    assert result.document.document_name == "Doc 0"


async def test_get_templates_malformed_filter_raises_value_error():
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(ValueError):
            # missing the required "value" key
            await client.get_templates(filters=[{"field": "name"}])


@respx.mock
async def test_render_persist_false_returns_json_envelope_with_null_ids():
    # persist=False no longer streams a raw PDF: it returns the JSON envelope
    # with id/viewUrl null and the base64 forced on. The SDK must parse it as
    # normal JSON (no content-type sniffing).
    doc = make_doc("Doc 0")
    doc["id"] = None
    doc["viewUrl"] = None
    doc["documentBase64"] = "JVBERi0xLjc="  # "%PDF-1.7"
    respx.post(RENDER_LATEST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "renderedCount": 1,
                "requestedCount": 1,
                "missingCount": 0,
                "documents": [doc],
                "issues": [],
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        result = await client.render(
            TID, {"Title": "a"}, include_document=True, persist=False
        )

    assert result.ok
    assert result.document.id is None
    assert result.document.view_url is None
    assert result.document.to_bytes() == b"%PDF-1.7"


@respx.mock
async def test_render_pdf_streams_bytes_with_header_metadata():
    route = respx.post(RENDER_LATEST_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"%PDF-1.7 real",
            headers={
                "content-type": "application/pdf",
                "Content-Disposition": 'attachment; filename="Doc 0.pdf"',
                "X-Pagr-Document-Id": DID,
                "X-Pagr-Page-Count": "2",
                "X-Pagr-Render-Duration-Ms": "99.5",
                "X-Pagr-View-Url": "https://example.test/doc",
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        result = await client.render_pdf(TID, {"Title": "a"})

    assert route.called
    # the Accept header opted into the raw-PDF path
    assert route.calls.last.request.headers["accept"] == "application/pdf"
    assert result.ok
    assert result.document.document_name == "Doc 0"
    assert result.document.page_count == 2
    assert result.document.to_bytes() == b"%PDF-1.7 real"


@respx.mock
async def test_render_pdf_422_is_business_outcome_not_exception():
    # A blocked render has no PDF to stream → 422 with the JSON envelope. The
    # SDK returns it as data (PdfRenderResult.ok is False), never raises.
    respx.post(RENDER_LATEST_URL).mock(
        return_value=httpx.Response(
            422,
            json={
                "status": "failed",
                "message": "blocked by content sanitizer",
                "issues": [
                    {"type": "DangerousContent", "severity": "Error",
                     "description": "script tag", "documentIndex": 0}
                ],
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        result = await client.render_pdf(TID, {"Title": "a"})

    assert result.ok is False
    assert result.status == "failed"
    assert result.document is None
    assert [i.description for i in result.issues] == ["script tag"]


@respx.mock
async def test_enqueue_returns_typed_job():
    respx.post(f"{RENDER_URL}/async").mock(
        return_value=httpx.Response(
            202,
            json={"jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "requestedCount": 3, "state": "queued"},
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        job = await client.enqueue_batch_render(
            TID, [{"Title": "a"}], "http://localhost/cb", version=1
        )

    assert job.requested_count == 3
    assert job.state is RenderJobState.QUEUED
    assert str(job.job_id) == "f47ac10b-58cc-4372-a567-0e02b2c3d479"


@respx.mock
async def test_get_job_status_polls():
    jid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    respx.get(f"{BASE}/v1/render/jobs/{jid}").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobId": jid,
                "state": "completed",
                "status": "ok",
                "renderedCount": 3,
                "requestedCount": 3,
                "missingCount": 0,
                "startedAt": "2026-07-16T10:00:00",
                "completedAt": "2026-07-16T10:00:05",
                "failureReason": None,
                "issues": [],
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        status = await client.get_job_status(jid)
    assert status.done and status.ok
    assert status.state is RenderJobState.COMPLETED
    assert status.status is RenderOutcome.OK
    assert status.rendered_count == 3


@respx.mock
async def test_get_template_version_defaults_to_latest():
    payload = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "versionNumber": 3,
        "templateJson": "{}",
        "sampleData": "{}",
        "templateId": TID,
    }
    route = respx.get(f"{BASE}/v1/templates/{TID}/versions/latest").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        version = await client.get_template_version(TID)  # no version → latest
        legacy = await client.get_template_version(TID, "latest")  # still accepted

    assert route.call_count == 2
    assert version.version_number == 3
    assert legacy.version_number == 3


@respx.mock
async def test_validate_json_string_array_is_treated_as_batch():
    import json

    route = respx.post(f"{RENDER_URL}/validate").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        await client.validate(TID, '[{"Title": "a"}, {"Title": "b"}]', version=1)

    body = json.loads(route.calls.last.request.content)
    assert body["documents"] == [{"Title": "a"}, {"Title": "b"}]


@respx.mock
async def test_validate_reads_issues():
    respx.post(f"{RENDER_URL}/validate").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [
                    {"type": "MissingBinding", "severity": "Error", "description": "missing", "documentIndex": 0}
                ]
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        resp = await client.validate(TID, {"Title": "a"}, version=1)
    assert not resp.is_valid
    assert len(resp.errors) == 1


@respx.mock
async def test_get_org_stats_includes_tokens():
    respx.get(f"{BASE}/v1/organisation/stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "organisationName": "Acme",
                "periodStart": "2026-07-01T00:00:00",
                "periodEnd": "2026-07-31T00:00:00",
                "tier": "pro",
                "includedRendersPerMonth": 1000,
                "pagesUsedThisPeriod": 120,
                "pagesAvailable": 880,
                "includedTokensPerMonth": 500000,
                "tokensUsedThisPeriod": 12345,
                "tokensAvailable": 487655,
                "userCount": 4,
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        stats = await client.get_org_stats()
    assert stats.organisation_name == "Acme"
    assert stats.included_tokens_per_month == 500000
    assert stats.tokens_used_this_period == 12345
    assert stats.tokens_available == 487655


@respx.mock
async def test_get_templates_returns_paged_result():
    respx.get(f"{BASE}/v1/templates").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": "550e8400-e29b-41d4-a716-446655440000", "name": "Invoice"}],
                "total": 1,
                "skip": 0,
                "take": 25,
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        page = await client.get_templates()
    assert page.total == 1
    assert len(page) == 1
    assert page[0].name == "Invoice"


@respx.mock
async def test_get_fonts_returns_list():
    respx.get(f"{BASE}/v1/fonts").mock(
        return_value=httpx.Response(200, json=["Arial", "Roboto"])
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        fonts = await client.get_fonts()
    assert fonts == ["Arial", "Roboto"]


@respx.mock
async def test_download_document_returns_bytes():
    did = "550e8400-e29b-41d4-a716-446655440000"
    respx.get(f"{BASE}/v1/documents/{did}/file").mock(
        return_value=httpx.Response(
            200, content=b"%PDF bytes", headers={"content-type": "application/pdf"}
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        data = await client.download_document(did)
    assert data == b"%PDF bytes"


@respx.mock
async def test_401_raises_authentication_error():
    respx.get(f"{BASE}/v1/templates").mock(
        return_value=httpx.Response(401, json={"error": {"code": "Unauthorized", "message": "bad key"}})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(AuthenticationError) as exc:
            await client.get_templates()
    assert exc.value.status_code == 401
    assert exc.value.code == "Unauthorized"
    assert "bad key" in str(exc.value)


@respx.mock
async def test_422_maps_to_validation_failed_and_is_pagr_error():
    respx.post(f"{RENDER_URL}/validate").mock(
        return_value=httpx.Response(422, json={"error": {"code": "BindingError", "message": "nope"}})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(PagrError) as exc:  # base class catches it
            await client.validate(TID, {"Title": "a"}, version=1)
    assert isinstance(exc.value, ValidationFailedError)
    assert exc.value.code == "BindingError"


async def test_base_url_defaults_to_hosted_api():
    # base_url is optional; omitting it targets the hosted Pagr API.
    async with PagrApiClient("key") as client:
        assert client._http.baseurl == DEFAULT_BASE_URL


async def test_base_url_can_be_overridden():
    async with PagrApiClient("key", "https://api.example.com/") as client:
        # explicit base_url wins and the trailing slash is normalised away
        assert client._http.baseurl == "https://api.example.com"


# ── Retries (idempotent GET only) ─────────────────────────────────────────────


@respx.mock
async def test_get_429_is_not_retried():
    # 429 reflects the caller's own call volume — surface it, don't retry.
    route = respx.get(DOC_FILE_URL).mock(
        return_value=httpx.Response(
            429, json={"error": {"code": "RateLimit", "message": "slow down"}}
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(RateLimitError) as exc:
            await _no_backoff(client).download_document(DID)
    assert route.call_count == 1
    assert exc.value.status_code == 429


@respx.mock
async def test_get_retries_on_503_then_succeeds():
    route = respx.get(DOC_FILE_URL).mock(side_effect=[httpx.Response(503), _pdf()])
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        data = await _no_backoff(client).download_document(DID)
    assert data == b"%PDF ok"
    assert route.call_count == 2


@respx.mock
async def test_get_retries_on_transient_connect_error_then_succeeds():
    route = respx.get(DOC_FILE_URL).mock(
        side_effect=[httpx.ConnectError("boom"), _pdf()]
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        data = await _no_backoff(client).download_document(DID)
    assert data == b"%PDF ok"
    assert route.call_count == 2


@respx.mock
async def test_get_retries_exhausted_raises_typed_error():
    # max_retries defaults to 2 → 3 attempts, all 503 → the typed error surfaces.
    route = respx.get(DOC_FILE_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(503)]
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(ApiError) as exc:
            await _no_backoff(client).download_document(DID)
    assert route.call_count == 3
    assert exc.value.status_code == 503


@respx.mock
async def test_post_is_not_retried_on_503():
    # Writes must never be retried (no idempotency keys → would render twice).
    route = respx.post(RENDER_LATEST_URL).mock(
        return_value=httpx.Response(
            503, json={"error": {"code": "QueueFull", "message": "full"}}
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(ApiError) as exc:
            await _no_backoff(client).render(TID, {"Title": "a"})
    assert route.call_count == 1
    assert exc.value.status_code == 503
    assert exc.value.code == "QueueFull"


@respx.mock
async def test_max_retries_zero_disables_retry():
    route = respx.get(DOC_FILE_URL).mock(side_effect=[httpx.Response(503), _pdf()])
    async with PagrApiClient(api_key="key", base_url=BASE, max_retries=0) as client:
        with pytest.raises(ApiError):
            await client.download_document(DID)
    assert route.call_count == 1


# ── Transport errors are wrapped in the PagrError tree ────────────────────────


@respx.mock
async def test_timeout_wrapped_as_pagr_timeout_error():
    respx.get(DOC_FILE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    async with PagrApiClient(api_key="key", base_url=BASE, max_retries=0) as client:
        with pytest.raises(PagrTimeoutError) as exc:
            await client.download_document(DID)
    assert isinstance(exc.value, PagrError)
    assert exc.value.status_code is None


@respx.mock
async def test_connection_error_wrapped_as_pagr_connection_error():
    respx.get(DOC_FILE_URL).mock(side_effect=httpx.ConnectError("no route"))
    async with PagrApiClient(api_key="key", base_url=BASE, max_retries=0) as client:
        with pytest.raises(PagrConnectionError) as exc:
            await client.download_document(DID)
    assert isinstance(exc.value, PagrError)


@respx.mock
async def test_post_transport_error_wrapped_but_not_retried():
    route = respx.post(RENDER_LATEST_URL).mock(side_effect=httpx.ConnectError("down"))
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(PagrConnectionError):
            await _no_backoff(client).render(TID, {"Title": "a"})
    assert route.call_count == 1


# ── Retriable statuses beyond 503 ─────────────────────────────────────────────


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 504])
async def test_get_retries_on_all_retriable_5xx(status):
    route = respx.get(DOC_FILE_URL).mock(
        side_effect=[httpx.Response(status), _pdf()]
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        data = await _no_backoff(client).download_document(DID)
    assert data == b"%PDF ok"
    assert route.call_count == 2


@respx.mock
async def test_get_retries_on_timeout_then_succeeds():
    route = respx.get(DOC_FILE_URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), _pdf()]
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        data = await _no_backoff(client).download_document(DID)
    assert data == b"%PDF ok"
    assert route.call_count == 2


# ── Backoff / Retry-After timing (previously never exercised) ─────────────────


def test_parse_retry_after_integer_and_fallback():
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    # An HTTP-date (non-integer) is not interpreted.
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


async def test_retry_after_header_honored_not_capped_to_backoff_max(monkeypatch):
    # A Retry-After larger than backoff_max (8s) must be honored, not silently
    # shortened — the old code capped it to backoff_max.
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr("pagr._http.asyncio.sleep", fake_sleep)
    with respx.mock:
        respx.get(DOC_FILE_URL).mock(
            side_effect=[httpx.Response(503, headers={"Retry-After": "20"}), _pdf()]
        )
        async with PagrApiClient(api_key="key", base_url=BASE) as client:
            await client.download_document(DID)
    assert slept == [20.0]


async def test_retry_after_clamped_to_retry_after_max(monkeypatch):
    slept = []
    monkeypatch.setattr(
        "pagr._http.asyncio.sleep", lambda d: slept.append(d) or _noop()
    )
    with respx.mock:
        respx.get(DOC_FILE_URL).mock(
            side_effect=[httpx.Response(503, headers={"Retry-After": "9999"}), _pdf()]
        )
        async with PagrApiClient(api_key="key", base_url=BASE) as client:
            client._http.retry_after_max = 60.0
            await client.download_document(DID)
    assert slept == [60.0]


async def test_backoff_uses_capped_exponential_when_no_retry_after(monkeypatch):
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    # random.uniform(0, ceiling) -> ceiling, so we observe the ceiling growth.
    monkeypatch.setattr("pagr._http.random.uniform", lambda a, b: b)
    monkeypatch.setattr("pagr._http.asyncio.sleep", fake_sleep)
    t = HttpTransport(BASE, "key", backoff_base=0.5, backoff_max=8.0)
    await t._backoff(1, None)
    await t._backoff(2, None)
    await t._backoff(3, None)
    await t._backoff(99, None)  # far past the cap
    await t.aclose()
    assert slept == [0.5, 1.0, 2.0, 8.0]


async def _noop():
    return None


# ── Error-code mapping (403/404/413/410) + raw-body fallback ──────────────────


@respx.mock
@pytest.mark.parametrize(
    "status,exc,code",
    [
        (403, ForbiddenError, "Forbidden"),
        (404, NotFoundError, "TemplateNotFound"),
        (413, PayloadTooLargeError, "PayloadTooLarge"),
    ],
)
async def test_error_codes_map_to_typed_exceptions(status, exc, code):
    respx.get(f"{BASE}/v1/templates/{TID}").mock(
        return_value=httpx.Response(status, json={"error": {"code": code, "message": "x"}})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(exc) as ei:
            await client.get_template(TID)
    assert ei.value.status_code == status
    assert ei.value.code == code


@respx.mock
async def test_410_pdf_deleted_maps_to_api_error():
    respx.get(DOC_FILE_URL).mock(
        return_value=httpx.Response(410, json={"error": {"code": "PdfDeleted", "message": "gone"}})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(ApiError) as ei:
            await client.download_document(DID)
    assert ei.value.status_code == 410
    assert ei.value.code == "PdfDeleted"


@respx.mock
async def test_error_body_non_json_falls_back_to_raw_text():
    respx.get(f"{BASE}/v1/templates/{TID}").mock(
        return_value=httpx.Response(500, text="upstream boom")
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(ApiError) as ei:
            await client.get_template(TID)
    assert ei.value.code is None
    assert "upstream boom" in str(ei.value)


@respx.mock
async def test_rate_limit_error_exposes_retry_after():
    respx.get(f"{BASE}/v1/templates").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "42"},
            json={"error": {"code": "RateLimit", "message": "slow"}},
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(RateLimitError) as ei:
            await client.get_templates()
    assert ei.value.retry_after == 42.0


# ── Decode failures surface as PagrDecodeError, not a raw exception ───────────


@respx.mock
async def test_empty_body_raises_pagr_decode_error():
    respx.get(f"{BASE}/v1/meta/version").mock(return_value=httpx.Response(200, text=""))
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(PagrDecodeError) as ei:
            await client.get_version()
    assert isinstance(ei.value, PagrError)


@respx.mock
async def test_missing_required_field_raises_pagr_decode_error():
    # documentName is required; omitting it must raise PagrDecodeError, not KeyError.
    doc = make_doc("x")
    del doc["documentName"]
    respx.get(f"{BASE}/v1/documents/{DID}").mock(
        return_value=httpx.Response(200, json=doc)
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(PagrDecodeError):
            await client.get_document(DID)


# ── set_api_key ───────────────────────────────────────────────────────────────


@respx.mock
async def test_set_api_key_changes_authorization_header():
    route = respx.get(f"{BASE}/v1/fonts").mock(
        return_value=httpx.Response(200, json=["Arial"])
    )
    async with PagrApiClient(api_key="old", base_url=BASE) as client:
        client.set_api_key("new")
        await client.get_fonts()
    assert route.calls.last.request.headers["Authorization"] == "Bearer new"


# ── Per-request timeout override ──────────────────────────────────────────────


def test_timeout_resolver():
    t = HttpTransport(BASE, "key", timeout=30.0)
    assert t._timeout(None) is httpx.USE_CLIENT_DEFAULT
    assert t._timeout(90.0) == 90.0


# ── Filter validation + value coercion + take=0 ───────────────────────────────


async def test_unknown_filter_field_raises_value_error():
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(ValueError):
            await client.get_documents(filters=[{"field": "documentNam", "value": "x"}])


async def test_unknown_filter_operator_raises_value_error():
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(ValueError):
            # "contains" is not valid for the numeric pageCount field.
            await client.get_documents(
                filters=[{"field": "pageCount", "op": "contains", "value": 3}]
            )


@respx.mock
async def test_filter_value_uuid_and_datetime_coerced():
    route = respx.get(f"{BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0, "skip": 0, "take": 25})
    )
    tpl = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        await client.get_documents(
            filters=[
                {"field": "template.guid", "value": tpl},
                {"field": "renderedAt", "op": "gte", "value": dt},
            ]
        )
    q = route.calls.last.request.url.params
    assert q["filters[0].value"] == str(tpl)
    assert q["filters[1].value"] == dt.isoformat()


@respx.mock
async def test_take_zero_is_sent_untouched():
    route = respx.get(f"{BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0, "skip": 0, "take": 1})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        await client.get_documents(take=0)
    assert route.calls.last.request.url.params["take"] == "0"


# ── _to_payload hardening ─────────────────────────────────────────────────────


async def test_render_rejects_json_array_string():
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(PagrError):
            await client.render(TID, "[{\"a\": 1}]")


async def test_render_rejects_non_str_non_dict():
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(PagrError):
            await client.render(TID, 123)  # type: ignore[arg-type]


# ── PagedResult.has_more ──────────────────────────────────────────────────────


@respx.mock
async def test_paged_result_has_more():
    respx.get(f"{BASE}/v1/templates").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": TID, "name": "T"}],
                "total": 50,
                "skip": 0,
                "take": 1,
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        page = await client.get_templates(take=1)
    assert page.has_more is True
    assert page.total == 50


# ── wait_for_job ──────────────────────────────────────────────────────────────


def _job_status(state, status=None):
    body = {
        "jobId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "state": state,
        "status": status,
        "renderedCount": 1,
        "requestedCount": 1,
        "missingCount": 0,
        "startedAt": "2026-07-16T10:00:00",
        "issues": [],
    }
    return httpx.Response(200, json=body)


@respx.mock
async def test_wait_for_job_polls_until_terminal(monkeypatch):
    monkeypatch.setattr("pagr.client.asyncio.sleep", lambda d: _noop())
    jid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    respx.get(f"{BASE}/v1/render/jobs/{jid}").mock(
        side_effect=[_job_status("pending"), _job_status("pending"), _job_status("completed", "ok")]
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        status = await client.wait_for_job(jid, poll_interval=0.01)
    assert status.done and status.state is RenderJobState.COMPLETED


@respx.mock
async def test_wait_for_job_unknown_state_is_terminal(monkeypatch):
    monkeypatch.setattr("pagr.client.asyncio.sleep", lambda d: _noop())
    jid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    # A brand-new server state the SDK does not know must still end the loop.
    respx.get(f"{BASE}/v1/render/jobs/{jid}").mock(
        return_value=_job_status("cancelled")
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        status = await client.wait_for_job(jid, poll_interval=0.01)
    assert status.done and status.state is RenderJobState.UNKNOWN


@respx.mock
async def test_wait_for_job_times_out(monkeypatch):
    monkeypatch.setattr("pagr.client.asyncio.sleep", lambda d: _noop())
    clock = iter([0.0] + [100.0] * 20)
    monkeypatch.setattr("pagr.client.time.monotonic", lambda: next(clock))
    jid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    respx.get(f"{BASE}/v1/render/jobs/{jid}").mock(return_value=_job_status("pending"))
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(PagrTimeoutError):
            await client.wait_for_job(jid, poll_interval=0.01, timeout=5)


@respx.mock
async def test_wait_for_job_default_deadline_is_not_unbounded(monkeypatch):
    """``timeout=None`` (the default) must apply a bounded deadline rather than
    poll forever — a never-finishing job has to eventually raise. The default
    itself is overridden to a tiny value so the test doesn't run for 5 real
    minutes."""
    monkeypatch.setattr("pagr.client.asyncio.sleep", lambda d: _noop())
    monkeypatch.setattr("pagr.client.WAIT_FOR_JOB_DEFAULT_TIMEOUT", 5.0)
    clock = iter([0.0] + [100.0] * 20)
    monkeypatch.setattr("pagr.client.time.monotonic", lambda: next(clock))
    jid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    respx.get(f"{BASE}/v1/render/jobs/{jid}").mock(return_value=_job_status("pending"))
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        with pytest.raises(PagrTimeoutError):
            # No timeout= passed at all: relies purely on the default.
            await client.wait_for_job(jid, poll_interval=0.01)


@respx.mock
async def test_wait_for_job_math_inf_opts_into_unbounded_polling(monkeypatch):
    """``timeout=math.inf`` is the documented opt-out: even a huge elapsed
    monotonic gap must not trip the deadline."""
    monkeypatch.setattr("pagr.client.asyncio.sleep", lambda d: _noop())
    clock = iter([0.0] + [1e12] * 20)
    monkeypatch.setattr("pagr.client.time.monotonic", lambda: next(clock))
    jid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    respx.get(f"{BASE}/v1/render/jobs/{jid}").mock(
        side_effect=[_job_status("pending"), _job_status("completed", "ok")]
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        status = await client.wait_for_job(jid, poll_interval=0.01, timeout=math.inf)
    assert status.done and status.state is RenderJobState.COMPLETED


@respx.mock
async def test_wait_for_job_cancellation_propagates_uncaught():
    """``task.cancel()`` while ``wait_for_job`` is asleep between polls must
    surface as ``asyncio.CancelledError`` — never wrapped in a ``PagrError`` —
    and must interrupt the sleep rather than waiting it out."""
    jid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    respx.get(f"{BASE}/v1/render/jobs/{jid}").mock(return_value=_job_status("pending"))
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        task = asyncio.ensure_future(client.wait_for_job(jid, poll_interval=60))
        await asyncio.sleep(0.05)  # let it reach the first poll's sleep
        started = asyncio.get_event_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Cancellation must interrupt the 60s sleep, not wait it out.
        assert asyncio.get_event_loop().time() - started < 1.0


# ── Previously-untested public methods (wire format) ──────────────────────────


@respx.mock
async def test_get_template_wire():
    respx.get(f"{BASE}/v1/templates/{TID}").mock(
        return_value=httpx.Response(200, json={"id": TID, "name": "Invoice"})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        t = await client.get_template(TID)
    assert t.name == "Invoice"


@respx.mock
async def test_get_template_versions_wire():
    respx.get(f"{BASE}/v1/templates/{TID}/versions").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": TID, "versionNumber": 3, "templateJson": "{}",
                     "sampleData": "{}", "templateId": TID}
                ],
                "total": 1, "skip": 0, "take": 25,
            },
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        page = await client.get_template_versions(TID)
    assert page.items[0].version_number == 3


@respx.mock
async def test_update_document_name_template_wire():
    route = respx.patch(
        f"{BASE}/v1/templates/{TID}/versions/2/document-name-template"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": TID, "versionNumber": 2, "templateJson": "{}",
                  "sampleData": "{}", "templateId": TID,
                  "documentNameTemplate": "Inv-{{n}}"},
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        v = await client.update_document_name_template(TID, 2, "Inv-{{n}}")
    assert route.called
    assert v.document_name_template == "Inv-{{n}}"


@respx.mock
async def test_get_preview_image_url_returns_none_when_absent():
    respx.get(f"{BASE}/v1/templates/{TID}/versions/2/preview-image").mock(
        return_value=httpx.Response(200, json={})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        url = await client.get_preview_image_url(TID, 2)
    assert url is None


@respx.mock
async def test_get_documents_wire():
    respx.get(f"{BASE}/v1/documents").mock(
        return_value=httpx.Response(
            200, json={"items": [make_doc("D0")], "total": 1, "skip": 0, "take": 25}
        )
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        page = await client.get_documents()
    assert page.items[0].document_name == "D0"


@respx.mock
async def test_get_document_wire():
    respx.get(f"{BASE}/v1/documents/{DID}").mock(
        return_value=httpx.Response(200, json=make_doc("D1"))
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        doc = await client.get_document(DID)
    assert doc.document_name == "D1"


@respx.mock
async def test_get_status_and_version_wire():
    respx.get(f"{BASE}/v1/meta/status").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/v1/meta/version").mock(
        return_value=httpx.Response(200, json={"version": "1.2.3"})
    )
    async with PagrApiClient(api_key="key", base_url=BASE) as client:
        assert await client.get_status() is True
        assert await client.get_version() == "1.2.3"


@respx.mock
async def test_aclose_without_context_manager():
    respx.get(f"{BASE}/v1/fonts").mock(return_value=httpx.Response(200, json=[]))
    client = PagrApiClient(api_key="key", base_url=BASE)
    await client.get_fonts()
    await client.aclose()  # public close outside the context-manager path
    assert client._http._client.is_closed
