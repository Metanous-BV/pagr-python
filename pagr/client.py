import asyncio
import json
import time
from datetime import datetime
from typing import Optional, Union
from uuid import UUID

from ._http import HttpTransport, DEFAULT_TIMEOUT, DEFAULT_MAX_RETRIES, _decode_json
from .exceptions import PagrError, PagrTimeoutError
from .filters import (
    DOCUMENT_FILTERS,
    TEMPLATE_FILTERS,
    TEMPLATE_VERSION_FILTERS,
    validate_filter,
)
from .models.template import Template, TemplateVersion
from .models.render import (
    RenderResult,
    BatchRenderResult,
    PdfDocument,
    PdfRenderResult,
    RenderJob,
    RenderJobStatus,
)
from .models.validation import ValidationResponse
from .models.organisation import OrgStats
from .models.document import RenderDocument, PagedResult

#: Base URL of the hosted Pagr Public API, used when the caller does not pass
#: an explicit ``base_url`` (e.g. to target a local dev instance).
DEFAULT_BASE_URL = "https://api.pagr.eu"

#: Default overall deadline for :meth:`PagrApiClient.wait_for_job`, in seconds.
#: Applied whenever ``timeout=None`` (the default) so polling can never hang
#: forever by accident. Pass ``timeout=math.inf`` to opt into truly unbounded
#: polling.
WAIT_FOR_JOB_DEFAULT_TIMEOUT = 300.0


def _to_payload(data: Union[str, dict]) -> dict:
    """Normalise a single caller-supplied document into a plain dict.

    Args:
        data: A document either as a dict or as a JSON-encoded object string.

    Returns:
        The document as a plain dict.

    Raises:
        PagrError: If ``data`` is not a dict or a JSON string, or the JSON
            string decodes to something other than an object (e.g. an array or
            scalar) — caught client-side rather than surfacing as an opaque
            server-side 422.
    """
    if isinstance(data, dict):
        return data
    if not isinstance(data, str):
        raise PagrError(
            f"document data must be a dict or a JSON string, not {type(data).__name__}"
        )
    try:
        parsed = json.loads(data)
    except ValueError as exc:
        raise PagrError(f"document data is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PagrError(
            "document data JSON must encode an object, not a "
            f"{type(parsed).__name__}"
        )
    return parsed


def _wire_value(value):
    """Coerce a filter value to a form httpx can serialise as a query param.

    httpx only accepts ``str``/``int``/``float``/``bool``/``None`` query values,
    yet the filter docstrings meaningfully type some fields as ``UUID`` (guid
    fields) or datetime (``createdAt`` etc.). Coerce those to their wire string
    form so a natural typed value does not raise a raw ``TypeError``.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _list_params(
    skip: Optional[int],
    take: Optional[int],
    sort_by: Optional[str],
    sort_direction: Optional[str],
    filters: Optional[list[dict]],
    search: Optional[str],
    allowed_filters: dict,
) -> dict:
    """Build the query params for a ``ListQuery``-bound endpoint.

    ``filters`` is a list of ``{"field", "op", "value"}`` dicts, expanded to the
    indexed model-binding form (``filters[0].field=...``) the API expects.
    ``field``/``op`` are validated against ``allowed_filters`` (the endpoint's
    table in :mod:`pagr.filters`) so an unknown field/operator is rejected
    client-side instead of silently returning the unfiltered result set.
    """
    params: dict = {
        "skip": skip,
        "take": take,
        "sortBy": sort_by,
        "sortDirection": sort_direction,
        "search": search,
    }
    for i, f in enumerate(filters or []):
        if "field" not in f or "value" not in f:
            raise ValueError(
                f"filters[{i}] must have 'field' and 'value' keys "
                f"(and an optional 'op'); got {sorted(f)}"
            )
        field = f["field"]
        op = f.get("op", "eq")
        validate_filter(i, field, op, allowed_filters)
        params[f"filters[{i}].field"] = field
        params[f"filters[{i}].op"] = op
        params[f"filters[{i}].value"] = _wire_value(f["value"])
    return params


class PagrApiClient:
    """Async HTTP client for the Pagr Public API (``/v1``).

    Provides methods for managing templates and versions, rendering documents
    (synchronously, or via fire-and-forget jobs with webhook
    callbacks or polling), validating data, browsing rendered documents and
    fonts, and retrieving organisation statistics.

    Use as an async context manager so the underlying HTTP connection is closed:

        async with PagrApiClient(api_key) as client:
            templates = await client.get_templates()

    Error model: HTTP-level failures raise a typed
    :class:`pagr.exceptions.PagrError` subclass — 401
    :class:`~pagr.exceptions.AuthenticationError`, 403
    :class:`~pagr.exceptions.ForbiddenError`, 404
    :class:`~pagr.exceptions.NotFoundError`, 413
    :class:`~pagr.exceptions.PayloadTooLargeError`, 422
    :class:`~pagr.exceptions.ValidationFailedError`, 429
    :class:`~pagr.exceptions.RateLimitError`, anything else
    :class:`~pagr.exceptions.ApiError`. Transport failures are wrapped too:
    a timeout raises :class:`~pagr.exceptions.PagrTimeoutError` and a
    connection/DNS failure raises :class:`~pagr.exceptions.PagrConnectionError`
    (both subclasses of :class:`~pagr.exceptions.PagrError`), so ``except
    PagrError`` catches every failure the SDK can produce. Business outcomes —
    a document that failed validation, insufficient credit — come back as data
    on the result objects, not as exceptions. Requests are rate limited per
    organisation over a sliding 60-second window; exceeding it raises
    :class:`~pagr.exceptions.RateLimitError`.

    Retries: read-only (GET) calls are retried on transient *server-side*
    failures — HTTP 500/502/503/504, timeouts, and connection errors — with
    capped exponential backoff and jitter (see ``max_retries``). Rate limits
    (429) are deliberately not retried: they reflect the caller's own request
    volume, so ``RateLimitError`` is raised for you to handle (slow down, lower
    concurrency). Writes (rendering, template edits) are never retried either:
    the API has no idempotency keys, so a request that was applied but whose
    response was lost must not be repeated (it would render/charge twice).

    All timestamps returned by the API are timezone-aware ``datetime`` objects
    in UTC.

    Args:
        api_key: The organisation API key, sent as a bearer token. The key
            prefix selects the mode: ``pagr_test_`` keys render with test
            restrictions (watermarked output, batches capped at 10 documents
            per request), ``pagr_prod_`` keys render fully and consume credit.
        base_url: Base URL of the Pagr Public API. Defaults to the hosted API
            (:data:`DEFAULT_BASE_URL`); pass this only to target another
            instance, e.g. a local dev server.
        timeout: Per-request timeout in seconds (default 30).
        max_retries: Maximum retries for transient failures on idempotent
            (GET) requests (default 2). Set to 0 to disable retries. Writes are
            never retried regardless of this value.
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self._http = HttpTransport(
            base_url or DEFAULT_BASE_URL,
            api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    def set_api_key(self, value: str):
        """Replace the API key used for subsequent requests.

        Args:
            value: The new API key.
        """
        self._http.set_api_key(value)

    # ── Templates ────────────────────────────────────────────────────────────

    async def get_templates(
        self,
        project_id: Optional[UUID] = None,
        *,
        skip: Optional[int] = None,
        take: Optional[int] = None,
        sort_by: Optional[str] = None,
        sort_direction: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        search: Optional[str] = None,
    ) -> PagedResult[Template]:
        """List templates available to the authenticated organisation.

        Args:
            project_id: When given, list only templates in that project.
            skip: Number of records to skip. Defaults to 0.
            take: Page size. Defaults to 25; the server clamps it to 1-200.
            sort_by: Field to sort on, using the API's camelCase wire name.
                Sortable fields: ``"name"``, ``"createdAt"``, ``"updatedAt"``
                (the default). Unknown values silently fall back to the
                default sort.
            sort_direction: ``"asc"`` (default) or ``"desc"``.
            filters: A list of ``{"field", "op", "value"}`` dicts, combined
                with AND; ``op`` defaults to ``"eq"``. Invalid filters are
                validated client-side — an unknown field/operator raises
                ``ValueError`` (the server would otherwise silently ignore it
                and return the unfiltered result set). Allowed fields and their
                operators:

                - ``"name"`` (string) — ``"eq"``, ``"contains"``.
                - ``"project.guid"`` (project UUID) — ``"eq"``.
                - ``"createdAt"``, ``"updatedAt"`` (ISO-8601 datetime) —
                  ``"eq"``, ``"gt"``, ``"gte"``, ``"lt"``, ``"lte"``.

            search: Free-text search; ``contains``-matches across the text
                fields (``name``).

        Example:
            page = await client.get_templates(
                take=50,
                sort_by="name",
                filters=[{"field": "name", "op": "contains", "value": "invoice"}],
            )

        Returns:
            A page of :class:`~pagr.models.template.Template`; use ``.items``
            and ``.total``.
        """
        path = (
            f"v1/projects/{project_id}/templates"
            if project_id is not None
            else "v1/templates"
        )
        params = _list_params(
            skip, take, sort_by, sort_direction, filters, search, TEMPLATE_FILTERS
        )
        response = await self._http.get(path, params=params)
        return PagedResult.from_api(_decode_json(response), Template.from_api)

    async def get_template(self, template_id: UUID) -> Template:
        """Fetch a single template by ID.

        Args:
            template_id: The template's UUID.

        Returns:
            The template's catalogue metadata. The template content itself
            lives on its versions — see :meth:`get_template_version`.

        Raises:
            NotFoundError: If the template does not exist or belongs to a
                different organisation.
        """
        response = await self._http.get(f"v1/templates/{template_id}")
        return Template.from_api(_decode_json(response))

    async def get_template_versions(
        self,
        template_id: UUID,
        *,
        skip: Optional[int] = None,
        take: Optional[int] = None,
        sort_by: Optional[str] = None,
        sort_direction: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        search: Optional[str] = None,
    ) -> PagedResult[TemplateVersion]:
        """List versions of a template.

        Args:
            template_id: The template whose versions to list.
            skip: Number of records to skip. Defaults to 0.
            take: Page size. Defaults to 25; the server clamps it to 1-200.
            sort_by: Field to sort on, using the API's camelCase wire name.
                Sortable fields: ``"versionNumber"`` (the default),
                ``"publishedAt"``, ``"createdAt"``, ``"updatedAt"``. Unknown
                values silently fall back to the default sort.
            sort_direction: ``"asc"`` (default) or ``"desc"``.
            filters: A list of ``{"field", "op", "value"}`` dicts, combined
                with AND; ``op`` defaults to ``"eq"``. Invalid filters are
                validated client-side — an unknown field/operator raises
                ``ValueError`` (the server would otherwise silently ignore it
                and return the unfiltered result set). Allowed fields and their
                operators:

                - ``"versionNumber"`` (number) — ``"eq"``, ``"gt"``,
                  ``"gte"``, ``"lt"``, ``"lte"``.
                - ``"publishedAt"``, ``"createdAt"``, ``"updatedAt"``
                  (ISO-8601 datetime) — ``"eq"``, ``"gt"``, ``"gte"``,
                  ``"lt"``, ``"lte"``.

            search: Free-text search across the endpoint's text fields.

        Returns:
            A page of :class:`~pagr.models.template.TemplateVersion`.

        Raises:
            NotFoundError: If the template does not exist.
        """
        params = _list_params(
            skip, take, sort_by, sort_direction, filters, search,
            TEMPLATE_VERSION_FILTERS,
        )
        response = await self._http.get(
            f"v1/templates/{template_id}/versions", params=params
        )
        return PagedResult.from_api(_decode_json(response), TemplateVersion.from_api)

    async def get_template_version(
        self, template_id: UUID, version: Optional[Union[int, str]] = None
    ) -> TemplateVersion:
        """Fetch a specific template version, or the latest published one.

        Args:
            template_id: The template.
            version: A version number, or ``None`` (default) for the latest
                published version. The string ``"latest"`` is also accepted
                for backwards compatibility.

        Returns:
            The requested version, including the template DSL
            (``template_json``) and its ``sample_data``.

        Raises:
            NotFoundError: If the template or version does not exist — or,
                for the latest-published form, when the template has no
                published version yet (error code ``"NoPublishedVersion"``).
        """
        suffix = "latest" if version is None or version == "latest" else str(version)
        response = await self._http.get(
            f"v1/templates/{template_id}/versions/{suffix}"
        )
        return TemplateVersion.from_api(_decode_json(response))

    async def update_document_name_template(
        self,
        template_id: UUID,
        version_number: int,
        document_name_template: Optional[str],
    ) -> TemplateVersion:
        """Update a version's document-name template.

        The document-name template is the pattern used to name documents
        rendered from this version (see ``RenderedDocument.document_name``).

        Args:
            template_id: The template.
            version_number: The version to update.
            document_name_template: The new name template (or ``None`` to clear).

        Returns:
            The updated version.

        Raises:
            NotFoundError: If the template or version does not exist.
        """
        response = await self._http.patch_json(
            f"v1/templates/{template_id}/versions/{version_number}/document-name-template",
            {"documentNameTemplate": document_name_template},
        )
        return TemplateVersion.from_api(_decode_json(response))

    async def get_preview_image_url(
        self, template_id: UUID, version_number: int
    ) -> Optional[str]:
        """Return the URL of a version's preview image.

        Args:
            template_id: The template.
            version_number: The version whose preview image to look up.

        Returns:
            A URL to the preview image (may be a time-limited link), or
            ``None`` when the version exists but has no preview image (the
            response carries no ``url``).

        Raises:
            NotFoundError: If the template or version does not exist.
        """
        response = await self._http.get(
            f"v1/templates/{template_id}/versions/{version_number}/preview-image"
        )
        return _decode_json(response).get("url")

    # ── Render ───────────────────────────────────────────────────────────────

    def _render_path(self, template_id: UUID, version: Optional[int], suffix: str = "") -> str:
        """Build a render endpoint path. ``version=None`` targets the latest
        published version; otherwise the specific version."""
        base = (
            f"v1/render/{template_id}"
            if version is None
            else f"v1/render/{template_id}/versions/{version}"
        )
        return base + suffix

    async def _post_render(
        self,
        template_id: UUID,
        version: Optional[int],
        documents: list[dict],
        include_document: bool,
        language: Optional[str],
        persist: bool,
        timeout: Optional[float] = None,
    ) -> RenderResult:
        """POST documents to the render endpoint and parse the JSON envelope.

        The render endpoints always return the JSON ``RenderResultDto``
        envelope for the default (``Accept: application/json``) content
        negotiation this method uses — including when ``persist=False`` (the
        document's ``id``/``view_url`` come back ``null`` and its base64 is
        forced on). The raw ``application/pdf`` stream is a separate, opt-in
        path exposed by :meth:`render_pdf`.
        """
        response = await self._http.post_json(
            self._render_path(template_id, version),
            {"documents": documents, "includeDocument": include_document},
            params={"language": language, "persist": persist},
            timeout=timeout,
        )
        return RenderResult.from_api(_decode_json(response))

    async def render(
        self,
        template_id: UUID,
        json_data: Union[str, dict],
        *,
        version: Optional[int] = None,
        include_document: bool = False,
        language: Optional[str] = None,
        persist: bool = True,
        timeout: Optional[float] = None,
    ) -> RenderResult:
        """Render a single document.

        Args:
            template_id: The template to render.
            json_data: The document data, as a dict or JSON string. Limits,
                enforced per document: at most 50 MB of JSON, nested at most
                32 levels deep.
            version: A specific version number, or ``None`` (default) for the
                latest published version.
            include_document: Whether to return the rendered PDF inline
                (base64 on the wire); read it with
                ``result.document.to_bytes()`` or save it with
                ``result.document.save(path)``.
            language: Language variant to render, for templates with
                translations. ``None`` renders the template's default language.
            persist: When ``True`` (default) the render is stored server-side
                and appears in :meth:`get_documents` /
                :meth:`download_document`. When ``False`` nothing is stored:
                the same JSON envelope comes back, but ``result.document.id``
                and ``.view_url`` are ``None`` (there is nothing to
                reference) and the PDF bytes are always included inline
                (``document_base64`` is forced on, since they are then the
                only copy) — so ``result.document.to_bytes()`` / ``.save()``
                work regardless of ``include_document``. To stream the raw
                PDF binary instead of the JSON envelope, use
                :meth:`render_pdf`.
            timeout: Optional per-request timeout override in seconds for this
                call only; ``None`` uses the client's configured default. Use a
                larger value than the default (30s) for a document that may
                approach the server's 60-second render budget.

        Returns:
            The render result. ``result.status`` is one of ``"ok"``,
            ``"partial"``, ``"failed"`` or ``"insufficient_credit"``;
            ``result.ok`` is ``True`` when a document was produced. Reasons a
            document did not render are reported as :class:`RenderIssue`
            objects in ``result.issues`` — for example an overrun of the
            per-document 60-second render budget surfaces as a
            ``RenderTimeout`` issue and disallowed content in the data as
            ``DangerousContent``. These are outcomes, not exceptions.

        Raises:
            NotFoundError: If the template or version does not exist, or no
                published version exists.
            PayloadTooLargeError: If the document exceeds the 50 MB limit.
            ValidationFailedError: If the request body cannot be bound.
        """
        return await self._post_render(
            template_id, version, [_to_payload(json_data)],
            include_document, language, persist, timeout,
        )

    async def render_pdf(
        self,
        template_id: UUID,
        json_data: Union[str, dict],
        *,
        version: Optional[int] = None,
        language: Optional[str] = None,
        persist: bool = True,
        timeout: Optional[float] = None,
    ) -> PdfRenderResult:
        """Render a single document and stream the raw PDF back.

        This is the opt-in ``Accept: application/pdf`` path: instead of the
        JSON envelope :meth:`render` returns, the API streams the PDF binary
        directly, carrying the document metadata in ``X-Pagr-*`` response
        headers. Use it when you want the bytes without base64-decoding a JSON
        field.

        Only single-document renders are supported — this method always sends
        exactly one document. (The API rejects a raw-PDF request for a batch
        with HTTP 406, surfaced as :class:`~pagr.exceptions.ApiError`.)

        Args:
            template_id: The template to render.
            json_data: The document data, as a dict or JSON string.
            version: A specific version number, or ``None`` (default) for the
                latest published version.
            language: Language variant to render, for templates with
                translations. ``None`` renders the default language.
            persist: When ``True`` (default) the render is stored server-side
                (``result.document.document_id`` / ``.view_url`` are then
                populated); when ``False`` nothing is stored and both are
                ``None``.
            timeout: Optional per-request timeout override in seconds for this
                call only; ``None`` uses the client's configured default.

        Returns:
            A :class:`~pagr.models.render.PdfRenderResult`. On a clean render
            ``result.ok`` is ``True`` and ``result.document`` is a
            :class:`~pagr.models.render.PdfDocument` (``.to_bytes()`` /
            ``.save(path)``). When the document is blocked or fails to render
            there is no PDF to stream, so ``result.ok`` is ``False`` and the
            reasons are in ``result.issues`` / ``result.status`` — a business
            outcome, not an exception.

        Raises:
            NotFoundError: If the template or version does not exist, or no
                published version exists.
            PayloadTooLargeError: If the document exceeds the 50 MB limit.
            ValidationFailedError: If the request body cannot be bound.
        """
        response = await self._http.post_json(
            self._render_path(template_id, version),
            {"documents": [_to_payload(json_data)]},
            params={"language": language, "persist": persist},
            headers={"Accept": "application/pdf"},
            non_raising_statuses=frozenset({422}),
            timeout=timeout,
        )
        if response.status_code == 422:
            return PdfRenderResult.from_error_envelope(_decode_json(response))
        return PdfRenderResult(
            document=PdfDocument.from_response(response),
            status="ok",
        )

    async def render_batch(
        self,
        template_id: UUID,
        json_data_sets: Union[list[str], list[dict]],
        *,
        version: Optional[int] = None,
        include_document: bool = False,
        language: Optional[str] = None,
        persist: bool = True,
        timeout: Optional[float] = None,
    ) -> BatchRenderResult:
        """Render multiple documents in a single request.

        Args:
            template_id: The template to render.
            json_data_sets: The document data sets, each a dict or JSON
                string. Each document may be at most 50 MB of JSON, nested at
                most 32 levels deep. Test-mode keys (``pagr_test_``) are
                limited to 10 documents per request.
            version: A specific version number, or ``None`` for the latest
                published version.
            include_document: Whether to return each rendered PDF inline
                (base64 on the wire).
            language: Language variant to render. ``None`` renders the
                template's default language.
            persist: When ``False`` the renders are not stored server-side.
            timeout: Optional per-request timeout override in seconds for this
                call only; ``None`` uses the client's configured default.

        Returns:
            An iterable result correlating each submitted input, by position,
            to its rendered document or the issues that prevented it from
            rendering. ``result.status`` is one of ``"ok"``, ``"partial"``,
            ``"failed"`` or ``"insufficient_credit"``.

        Raises:
            NotFoundError: If the template or version does not exist, or no
                published version exists.
            PayloadTooLargeError: If a document exceeds the 50 MB limit.
            ValidationFailedError: If the request body cannot be bound.
            ApiError: HTTP 400 when a test-mode key submits more than 10
                documents in one batch.
        """
        inputs = [_to_payload(d) for d in json_data_sets]
        response = await self._http.post_json(
            self._render_path(template_id, version),
            {"documents": inputs, "includeDocument": include_document},
            params={"language": language, "persist": persist},
            timeout=timeout,
        )
        return BatchRenderResult.from_api(_decode_json(response), inputs=inputs)

    async def enqueue_batch_render(
        self,
        template_id: UUID,
        json_data_sets: Union[list[str], list[dict]],
        callback_url: str,
        *,
        version: Optional[int] = None,
        include_document: bool = False,
        language: Optional[str] = None,
        persist: bool = True,
        timeout: Optional[float] = None,
    ) -> RenderJob:
        """Enqueue a fire-and-forget batch render.

        Returns immediately with a job reference (initial ``job.state`` of
        ``"queued"``). The server renders in the background and POSTs
        webhooks to ``callback_url``:

        - one *progress* callback per successfully rendered document, and
        - one final *completion* callback when the job finishes.

        Parse them with :func:`pagr.models.render.parse_callback`. Deliveries
        carry no auth header, are not retried, and time out after roughly 30
        seconds — respond quickly. As a reliable alternative (or complement),
        poll :meth:`get_job_status`.

        Args:
            template_id: The template to render.
            json_data_sets: The document data sets, each a dict or JSON
                string. The same limits as :meth:`render_batch` apply (50 MB
                and 32 nesting levels per document, 10-document cap for
                test-mode keys) and are checked at enqueue time — an accepted
                job will not fail later on payload size.
            callback_url: A URL the Pagr server can reach to POST the
                webhooks to.
            version: A specific version number, or ``None`` for the latest
                published version.
            include_document: Whether progress webhooks include the rendered
                PDF inline (base64).
            language: Language variant to render.
            persist: Whether the renders are stored server-side.
            timeout: Optional per-request timeout override in seconds for this
                call only; ``None`` uses the client's configured default.

        Returns:
            A reference to the enqueued job (``job.requested_count`` documents,
            ``job.state == "queued"``); poll it with :meth:`get_job_status`
            using ``job.job_id``.

        Raises:
            NotFoundError: If the template or version does not exist, or no
                published version exists.
            PayloadTooLargeError: If a document exceeds the 50 MB limit.
            ValidationFailedError: If the request body cannot be bound.
            ApiError: HTTP 503 with code ``"QueueFull"`` when the render
                queue is at capacity — retry later.
        """
        response = await self._http.post_json(
            self._render_path(template_id, version, "/async"),
            {
                "documents": [_to_payload(d) for d in json_data_sets],
                "callbackUrl": callback_url,
                "includeDocument": include_document,
            },
            params={"language": language, "persist": persist},
            timeout=timeout,
        )
        return RenderJob.from_api(_decode_json(response))

    async def get_job_status(self, job_id: UUID) -> RenderJobStatus:
        """Poll the status of an async render job.

        A reliable alternative to the webhook callback: returns the job's
        lifecycle state and how many documents it has produced so far.

        Args:
            job_id: The job returned by :meth:`enqueue_batch_render`.

        Returns:
            The job status. ``status.state`` is the lifecycle as a
            :class:`~pagr.models.render.RenderJobState`
            (``PENDING`` / ``COMPLETED`` / ``FAILED``, or ``UNKNOWN`` for an
            unrecognised value); ``status.status`` is the render outcome as a
            :class:`~pagr.models.render.RenderOutcome`
            (``OK`` / ``PARTIAL`` / ``FAILED`` / ``INSUFFICIENT_CREDIT`` /
            ``UNKNOWN``), or ``None`` while pending. Poll on an interval until
            ``status.done`` is ``True`` (or use :meth:`wait_for_job`), then
            check ``status.ok`` / ``status.failure_reason``; ``status.issues``,
            ``status.rendered_count`` / ``requested_count`` / ``missing_count``
            carry the per-document detail.

        Raises:
            NotFoundError: If no job with this ID exists.
        """
        response = await self._http.get(f"v1/render/jobs/{job_id}")
        return RenderJobStatus.from_api(_decode_json(response))

    async def wait_for_job(
        self,
        job_id: UUID,
        *,
        poll_interval: float = 2.0,
        timeout: Optional[float] = None,
    ) -> RenderJobStatus:
        """Poll :meth:`get_job_status` until the job reaches a terminal state.

        A convenience wrapper over the hand-rolled ``while not status.done``
        loop. Because ``done`` treats an unrecognised state as terminal
        (fail-open), this never spins forever on a server state the SDK does
        not know about.

        This method itself never polls forever by *default*: ``timeout=None``
        selects a :data:`WAIT_FOR_JOB_DEFAULT_TIMEOUT` (5 minute) overall
        deadline, not unbounded waiting. Pass ``timeout=math.inf`` to opt into
        truly unbounded polling.

        Cancellation (``task.cancel()``) breaks out of whichever
        ``asyncio.sleep`` this is currently waiting on immediately — it does
        not wait out the remaining poll interval — and propagates
        ``asyncio.CancelledError`` uncaught; it is never wrapped in a
        :class:`PagrError`. See the User Guide's Cancellation section.

        Args:
            job_id: The job returned by :meth:`enqueue_batch_render`.
            poll_interval: Seconds to wait between status polls (default 2.0).
            timeout: Overall deadline in seconds across all polls. ``None``
                (default) applies :data:`WAIT_FOR_JOB_DEFAULT_TIMEOUT` (300s /
                5 minutes). Pass ``math.inf`` for unbounded polling.

        Returns:
            The terminal :class:`~pagr.models.render.RenderJobStatus` (its
            ``state`` is ``COMPLETED``/``FAILED``, or ``UNKNOWN``).

        Raises:
            PagrTimeoutError: If the deadline elapses before the job finishes.
            NotFoundError: If no job with this ID exists.
        """
        effective_timeout = (
            WAIT_FOR_JOB_DEFAULT_TIMEOUT if timeout is None else timeout
        )
        deadline = time.monotonic() + effective_timeout
        while True:
            status = await self.get_job_status(job_id)
            if status.done:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PagrTimeoutError(
                    f"job {job_id} did not finish within {effective_timeout}s"
                )
            await asyncio.sleep(min(poll_interval, remaining))

    # ── Validate ─────────────────────────────────────────────────────────────

    async def validate(
        self,
        template_id: UUID,
        json_data: Union[str, dict, list],
        *,
        version: Optional[int] = None,
    ) -> ValidationResponse:
        """Validate document data against a template without rendering.

        Validation consumes no render credit. The same checks run before a
        real render; severity gates rendering server-side — production
        rendering blocks on any issue of ``Warning`` severity or higher,
        test/preview rendering blocks only on ``Error``.

        Args:
            template_id: The template to validate against.
            json_data: A single document (dict/JSON string) or a list of them.
                A JSON string encoding an array is treated as a batch.
            version: A specific version number, or ``None`` for the latest
                published version.

        Returns:
            The validation results; ``result.is_valid`` is ``True`` when no
            issue has ``Error`` severity. Each issue carries the zero-based
            ``document_index`` of the document it pertains to (``None`` for
            batch-wide issues).

        Raises:
            NotFoundError: If the template or version does not exist.
            ValidationFailedError: If the request body cannot be bound.
            PagrError: If ``json_data`` (or an element of it) is not an object /
                JSON object string.
        """
        # A list is always a batch; a JSON string may itself encode an array
        # (also a batch) or a single object. Anything else is a single document.
        if isinstance(json_data, str):
            try:
                raw = json.loads(json_data)
            except ValueError as exc:
                raise PagrError(f"json_data is not valid JSON: {exc}") from exc
        else:
            raw = json_data
        items = raw if isinstance(raw, list) else [raw]
        documents = [_to_payload(d) for d in items]
        response = await self._http.post_json(
            self._render_path(template_id, version, "/validate"),
            {"documents": documents},
        )
        return ValidationResponse.from_api(_decode_json(response))

    # ── Documents ──────────────────────────────────────────────────────────────

    async def get_documents(
        self,
        *,
        skip: Optional[int] = None,
        take: Optional[int] = None,
        sort_by: Optional[str] = None,
        sort_direction: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        search: Optional[str] = None,
    ) -> PagedResult[RenderDocument]:
        """List rendered documents for the authenticated organisation.

        Returns document *metadata* only
        (:class:`~pagr.models.document.RenderDocument`); fetch the PDF bytes
        separately with :meth:`download_document`. Only renders made with
        ``persist=True`` (the default) appear here.

        Args:
            skip: Number of records to skip. Defaults to 0.
            take: Page size. Defaults to 25; the server clamps it to 1-200.
            sort_by: Field to sort on, using the API's camelCase wire name.
                Sortable fields: ``"documentName"``, ``"versionNumber"``,
                ``"fileSizeBytes"``, ``"pageCount"``, ``"renderedAt"`` (the
                default), ``"renderDuration"``, ``"environment"``,
                ``"createdAt"``, ``"updatedAt"``. Unknown values silently
                fall back to the default sort.
            sort_direction: ``"asc"`` (default) or ``"desc"``.
            filters: A list of ``{"field", "op", "value"}`` dicts, combined
                with AND; ``op`` defaults to ``"eq"``. Invalid filters are
                validated client-side — an unknown field/operator raises
                ``ValueError`` (the server would otherwise silently ignore it
                and return the unfiltered result set). Allowed fields and their
                operators:

                - ``"documentName"`` (string) — ``"eq"``, ``"contains"``.
                - ``"template.guid"`` (template UUID) — ``"eq"``.
                - ``"versionNumber"``, ``"fileSizeBytes"``, ``"pageCount"``
                  (number) — ``"eq"``, ``"gt"``, ``"gte"``, ``"lt"``, ``"lte"``.
                - ``"renderedAt"``, ``"createdAt"``, ``"updatedAt"``
                  (ISO-8601 datetime) — ``"eq"``, ``"gt"``, ``"gte"``,
                  ``"lt"``, ``"lte"``.
                - ``"environment"`` — ``"eq"``, ``"neq"``; value ``"test"``
                  or ``"production"``.
                - ``"language"`` (string) — ``"eq"``, ``"neq"``.

                ``renderDuration`` can be sorted on but not filtered;
                ``documentType`` supports neither.

            search: Free-text search; ``contains``-matches across the text
                fields (``documentName``).

        Example:
            page = await client.get_documents(
                take=50,
                sort_by="renderedAt",
                sort_direction="desc",
                filters=[
                    {"field": "environment", "value": "production"},
                    {"field": "renderedAt", "op": "gte", "value": "2026-01-01T00:00:00Z"},
                ],
            )

        Returns:
            A page of :class:`~pagr.models.document.RenderDocument`; use
            ``.items`` and ``.total``.
        """
        params = _list_params(
            skip, take, sort_by, sort_direction, filters, search, DOCUMENT_FILTERS
        )
        response = await self._http.get("v1/documents", params=params)
        return PagedResult.from_api(_decode_json(response), RenderDocument.from_api)

    async def get_document(self, document_id: UUID) -> RenderDocument:
        """Fetch a single rendered document's metadata by ID.

        The result carries metadata only — no PDF bytes. Use
        :meth:`download_document` for the file; its ``is_pdf_deleted`` flag
        tells you up front whether the file is still stored.

        Args:
            document_id: The document's UUID.

        Returns:
            The document metadata.

        Raises:
            NotFoundError: If the document does not exist.
        """
        response = await self._http.get(f"v1/documents/{document_id}")
        return RenderDocument.from_api(_decode_json(response))

    async def download_document(
        self, document_id: UUID, *, timeout: Optional[float] = None
    ) -> bytes:
        """Download a rendered document's PDF bytes.

        Only documents rendered with ``persist=True`` have a stored file.

        Args:
            document_id: The document's UUID.
            timeout: Optional per-request timeout override in seconds for this
                call only; ``None`` uses the client's configured default. Handy
                for a large PDF over a slow link.

        Returns:
            The PDF file content as bytes.

        Raises:
            NotFoundError: If the document does not exist.
            ApiError: HTTP 410 with code ``"PdfDeleted"`` when the stored PDF
                has been purged by retention. The metadata remains available
                via :meth:`get_document`; its ``is_pdf_deleted`` flag
                predicts this error.
        """
        response = await self._http.get(
            f"v1/documents/{document_id}/file", timeout=timeout
        )
        return response.content

    # ── Fonts ────────────────────────────────────────────────────────────────

    async def get_fonts(self) -> list[str]:
        """List the font family names available for rendering.

        Returns:
            The font family names that templates can reference in their font
            settings. Referencing a family outside this list produces an
            ``UnresolvedFont`` render issue.
        """
        response = await self._http.get("v1/fonts")
        return list(_decode_json(response))

    # ── Organisation ───────────────────────────────────────────────────────────

    async def get_org_stats(self) -> OrgStats:
        """Fetch usage and credit statistics for the authenticated organisation.

        Returns:
            The organisation's usage for the current billing period. A value
            of ``-1`` for ``pages_available``, ``included_tokens_per_month``
            or ``tokens_available`` means unlimited.
        """
        response = await self._http.get("v1/organisation/stats")
        return OrgStats.from_api(_decode_json(response))

    # ── Meta ─────────────────────────────────────────────────────────────────

    async def get_status(self) -> bool:
        """Check API health. Returns ``True`` when the service reports healthy;
        raises :class:`pagr.exceptions.ApiError` (503) otherwise."""
        await self._http.get("v1/meta/status")
        return True

    async def get_version(self) -> Optional[str]:
        """Return the deployed API version string."""
        response = await self._http.get("v1/meta/version")
        return _decode_json(response).get("version")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self):
        """Close the underlying HTTP connection pool.

        Call this for a long-lived client that is not used as an
        ``async with`` context manager (e.g. a process-wide singleton), so the
        pool is released on shutdown. Equivalent to what ``async with`` does on
        exit; after it the client should not be reused.
        """
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
