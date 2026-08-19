import base64
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Union
from uuid import UUID

import httpx

from ._common import parse_dt, parse_dt_required, require
from ..exceptions import PagrDecodeError


def _safe_filename(name: str) -> str:
    """Reduce a server-supplied document name to a bare, safe filename.

    ``document_name`` is data (it can embed values bound from the render
    payload), not a path, so it must never be able to steer a ``save`` outside
    the target directory. This strips any directory components, drive letters
    and traversal so the result is always a single path segment.
    """
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # drop directory components
    _, name = os.path.splitdrive(name)                 # drop a Windows drive (``C:``)
    name = name.lstrip("/\\").strip()
    if name in ("", ".", ".."):
        return "document"
    return name


class RenderIssueSeverity(Enum):
    """How much a :class:`RenderIssue` blocks rendering.

    Severity is ordered ``INFORMATION < WARNING < ERROR``. This is a plain
    (string-valued) ``Enum``, so the members are **not** directly comparable
    with ``<``/``>=`` — use :meth:`is_at_least` (or the
    :attr:`is_blocking_production` shortcut) to test severity thresholds:

        if issue.severity.is_at_least(RenderIssueSeverity.WARNING):
            ...  # would block a production render

    Production rendering blocks on any issue at or above ``WARNING``;
    test/preview blocks only on ``ERROR``; a document is production-valid only
    when all its issues are :attr:`INFORMATION`.
    """

    INFORMATION = "Information"
    WARNING = "Warning"
    ERROR = "Error"

    #: Rank used for ordering comparisons (higher = more severe).
    @property
    def _rank(self) -> int:
        return _SEVERITY_RANK[self]

    def is_at_least(self, other: "RenderIssueSeverity") -> bool:
        """True when this severity is ``other`` or more severe.

        The ordered replacement for the ``severity >= other`` comparison a
        plain ``Enum`` cannot do.
        """
        return self._rank >= other._rank

    @property
    def is_blocking_production(self) -> bool:
        """True when an issue of this severity blocks a production render
        (i.e. it is ``WARNING`` or ``ERROR``)."""
        return self.is_at_least(RenderIssueSeverity.WARNING)

    @classmethod
    def from_api(cls, value: Optional[str]) -> "RenderIssueSeverity":
        """Parse the API's string value; unknown/missing values fail closed to ERROR."""
        if value is not None:
            for member in cls:
                if member.value.lower() == str(value).lower():
                    return member
        return cls.ERROR


_SEVERITY_RANK = {
    RenderIssueSeverity.INFORMATION: 0,
    RenderIssueSeverity.WARNING: 1,
    RenderIssueSeverity.ERROR: 2,
}


class RenderIssueType(Enum):
    """Category of a :class:`RenderIssue`, mirroring the API's ``RenderIssueType``.

    Grouped by the severity the server assigns them:

    Error (blocks every render):

    - :attr:`INVALID_JSON` — the document data is not valid JSON.
    - :attr:`SCHEMA_INVALID` — the template failed schema validation.
    - :attr:`DANGEROUS_CONTENT` — the data contains disallowed content
      (script/HTML injection patterns, embedded executables).
    - :attr:`INVALID_PAGE_BACKGROUND` — a page background could not be used.
    - :attr:`RENDER_TIMEOUT` — the document exceeded the per-document render
      time budget (60 seconds).

    Warning (blocks production renders, allowed in test/preview):

    - :attr:`MISSING_BINDING` — the data lacks a field the template binds to.
    - :attr:`UNRESOLVED_IMAGE` — an image reference could not be resolved.
    - :attr:`UNRESOLVED_FONT` — a font family could not be resolved (see
      ``PagrApiClient.get_fonts``).
    - :attr:`INVALID_COLOR` — a colour value could not be parsed.
    - :attr:`INVALID_CONDITION` — a conditional expression could not be
      evaluated.
    - :attr:`DATA_SOURCE_NOT_ENUMERABLE` — a repeating element's data source
      is not a list.
    - :attr:`INVALID_CHART_CONFIG` — a chart's configuration is invalid.
    - :attr:`BINDING_FAILED_AT_RENDER` — a binding failed while rendering.
    - :attr:`RENDER_LAYOUT_DEGRADED` — the layout could not be fully
      honoured; output may look degraded.

    Information (never blocks):

    - :attr:`UNFORMATTED_VALUE` — a value was rendered without a format.
    - :attr:`INVALID_LAYOUT` — a non-blocking layout problem was detected.

    :attr:`UNKNOWN` is a client-side fallback, not a server value: unknown
    types parse to it rather than raising, so new server behaviour never
    crashes an older client.
    """

    INVALID_JSON = "InvalidJson"
    SCHEMA_INVALID = "SchemaInvalid"
    DANGEROUS_CONTENT = "DangerousContent"
    MISSING_BINDING = "MissingBinding"
    UNRESOLVED_IMAGE = "UnresolvedImage"
    UNRESOLVED_FONT = "UnresolvedFont"
    INVALID_COLOR = "InvalidColor"
    INVALID_CONDITION = "InvalidCondition"
    DATA_SOURCE_NOT_ENUMERABLE = "DataSourceNotEnumerable"
    INVALID_CHART_CONFIG = "InvalidChartConfig"
    INVALID_PAGE_BACKGROUND = "InvalidPageBackground"
    BINDING_FAILED_AT_RENDER = "BindingFailedAtRender"
    RENDER_TIMEOUT = "RenderTimeout"
    RENDER_LAYOUT_DEGRADED = "RenderLayoutDegraded"
    INVALID_LAYOUT = "InvalidLayout"
    UNFORMATTED_VALUE = "UnformattedValue"
    UNKNOWN = "Unknown"

    @classmethod
    def from_api(cls, value: Optional[str]) -> "RenderIssueType":
        if value is not None:
            for member in cls:
                if member.value.lower() == str(value).lower():
                    return member
        return cls.UNKNOWN


@dataclass
class RenderIssue:
    """A single render or validation issue.

    The category is carried by :attr:`type` and the blocking-ness by
    :attr:`severity`. ``document_index`` is the zero-based position of the
    document the issue pertains to in a batch, or ``None`` for single-document
    operations.
    """

    type: RenderIssueType
    severity: RenderIssueSeverity
    description: str
    element_id: Optional[str] = None
    document_index: Optional[int] = None

    @property
    def is_error(self) -> bool:
        """True when this issue has ``Error`` severity (blocks the document)."""
        return self.severity is RenderIssueSeverity.ERROR

    @classmethod
    def from_api(cls, data: dict) -> "RenderIssue":
        return cls(
            type=RenderIssueType.from_api(data.get("type")),
            severity=RenderIssueSeverity.from_api(data.get("severity")),
            description=data.get("description", ""),
            element_id=data.get("elementId"),
            document_index=data.get("documentIndex"),
        )

    def __str__(self) -> str:
        loc = f" [{self.element_id}]" if self.element_id else ""
        return f"{self.severity.value}: {self.type.value}{loc} — {self.description}"


@dataclass
class RenderedDocument:
    """A document produced by a render call.

    Returned inside :class:`RenderResult` / :class:`BatchRenderResult` and in
    async-render progress webhooks. Fields of note:

    - ``document_name`` — generated from the version's document-name
      template; carries no file extension.
    - ``environment`` — ``"test"`` or ``"production"``, decided by the API
      key that rendered it.
    - ``rendered_at`` — when the render happened (UTC).
    - ``render_duration`` — server-side render time in milliseconds.
    - ``id`` / ``view_url`` — the stored-document id and its web-app link.
      Both are ``None`` when the render was made with ``persist=False``:
      nothing was stored, so there is nothing to reference (no zero-GUID or
      empty-string placeholder). Everything else is always real.
    - ``document_type`` — ``"Template"`` or ``"Invoice"``.
    - ``language`` — the language variant the document was rendered in, for
      templates with translations; ``None`` when the template has no
      translations or the render did not specify one.
    - ``document_base64`` — the PDF, base64-encoded; present when rendered
      with ``include_document=True`` — and always when ``persist=False``,
      where the server forces it on because the inline bytes are then the
      only copy (decode via :meth:`to_bytes` or :meth:`save`), otherwise
      ``None``.
    - ``document_index`` — zero-based position of this document in the render
      request's data array, so it can be correlated with the input that
      produced it; ``None`` outside a render response (e.g. the document
      listing endpoints, where there is no request to index into).
    """

    id: Optional[UUID]
    document_name: str
    template_id: UUID
    version_number: int
    environment: str
    file_size_bytes: int
    page_count: int
    rendered_at: datetime
    render_duration: float
    view_url: Optional[str]
    document_type: str
    # repr=False: this can be megabytes of base64 PDF — keep it out of the
    # default repr / traceback dumps.
    document_base64: Optional[str] = field(repr=False)
    language: Optional[str] = None
    document_index: Optional[int] = None

    @classmethod
    def from_api(cls, data: dict) -> "RenderedDocument":
        return cls(
            # id/viewUrl are null when persist=False (nothing stored) — guard
            # against UUID(None) rather than assuming they are always present.
            id=UUID(data["id"]) if data.get("id") else None,
            document_name=require(data, "documentName"),
            template_id=UUID(require(data, "templateId")),
            version_number=require(data, "versionNumber"),
            environment=require(data, "environment"),
            file_size_bytes=require(data, "fileSizeBytes"),
            page_count=require(data, "pageCount"),
            rendered_at=parse_dt_required(require(data, "renderedAt")),
            render_duration=require(data, "renderDuration"),
            view_url=data.get("viewUrl"),
            document_type=require(data, "documentType"),
            document_base64=data.get("documentBase64"),
            language=data.get("language"),
            document_index=data.get("documentIndex"),
        )

    def to_bytes(self) -> bytes:
        """Return the decoded document bytes.

        Only available when the document was rendered with ``include_document=True``
        (the API then includes a base64 payload).

        Returns:
            The decoded document content.

        Raises:
            ValueError: If the document has no inline base64 content.
        """
        if self.document_base64 is None:
            raise ValueError(
                "This document has no inline content. Render with "
                "include_document=True to receive the document bytes."
            )
        return base64.b64decode(self.document_base64)

    def save(self, path: str) -> str:
        """Write the document to disk.

        Args:
            path: Destination path. If it is an existing directory,
                ``document_name`` (reduced to a safe, single-segment filename)
                is used as the filename inside it. A ``.pdf`` extension is
                appended unless the name already ends in ``.pdf`` (rendered
                output is always a PDF, and the API's ``document_name`` carries
                no extension).

        Returns:
            The path that was written.
        """
        if os.path.isdir(path):
            filename = _safe_filename(self.document_name)
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            path = os.path.join(path, filename)
        with open(path, "wb") as f:
            f.write(self.to_bytes())
        return path

    def __str__(self) -> str:
        return (
            f"RenderedDocument\n"
            f"  Name:        {self.document_name}\n"
            f"  ID:          {self.id}\n"
            f"  Template:    {self.template_id} (v{self.version_number})\n"
            f"  Environment: {self.environment}\n"
            f"  Pages:       {self.page_count}\n"
            f"  Size:        {self.file_size_bytes / 1024:.1f} KB\n"
            f"  Duration:    {self.render_duration:.2f}ms\n"
            f"  Rendered at: {self.rendered_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n"
            f"  URL:         {self.view_url}\n"
        )


def _filename_from_content_disposition(header: Optional[str]) -> str:
    """Extract a bare document name from a ``Content-Disposition`` header.

    The API sends ``attachment; filename="<documentName>.pdf"``. Returns the
    name without its ``.pdf`` extension (matching
    :attr:`RenderedDocument.document_name`'s no-extension convention), or
    ``"document"`` when the header is missing or carries no filename.

    The ``filename=`` marker is matched case-insensitively, as HTTP header
    parameter names are case-insensitive (RFC 6266).
    """
    if header:
        marker = "filename="
        at = header.lower().find(marker)
        if at != -1:
            name = header[at + len(marker):].split(";", 1)[0].strip().strip('"')
            if name.lower().endswith(".pdf"):
                name = name[:-4]
            if name:
                return name
    return "document"


@dataclass
class PdfDocument:
    """A single document returned as a raw PDF stream.

    Produced only by :meth:`PagrApiClient.render_pdf`, which opts into the
    ``Accept: application/pdf`` response. Unlike :class:`RenderedDocument`
    (built from the JSON envelope), this carries only what the raw-PDF
    response actually provides — the bytes plus the metadata the server puts
    in ``X-Pagr-*`` headers. Fields the headers do not carry (template id,
    version, environment, timestamp, type, language) are deliberately absent
    rather than fabricated.

    ``document_id`` and ``view_url`` are ``None`` when the render was not
    persisted (``persist=False``).
    """

    document_name: str
    content: bytes
    document_id: Optional[UUID] = None
    page_count: int = 0
    render_duration: float = 0.0
    view_url: Optional[str] = None
    issue_count: int = 0

    @classmethod
    def from_response(cls, response: "httpx.Response") -> "PdfDocument":
        """Build a :class:`PdfDocument` from a raw ``application/pdf`` response,
        reading the document metadata out of its ``X-Pagr-*`` headers and the
        name from ``Content-Disposition``."""
        headers = response.headers
        doc_id = headers.get("X-Pagr-Document-Id")
        return cls(
            document_name=_filename_from_content_disposition(
                headers.get("Content-Disposition")
            ),
            content=response.content,
            document_id=UUID(doc_id) if doc_id else None,
            page_count=int(headers.get("X-Pagr-Page-Count", 0) or 0),
            render_duration=float(headers.get("X-Pagr-Render-Duration-Ms", 0) or 0),
            view_url=headers.get("X-Pagr-View-Url"),
            issue_count=int(headers.get("X-Pagr-Issue-Count", 0) or 0),
        )

    def to_bytes(self) -> bytes:
        """Return the PDF bytes."""
        return self.content

    def save(self, path: str) -> str:
        """Write the PDF to disk.

        Args:
            path: Destination path. If it is an existing directory,
                ``document_name`` (reduced to a safe, single-segment filename)
                is used as the filename inside it, with ``.pdf`` appended.

        Returns:
            The path that was written.
        """
        if os.path.isdir(path):
            filename = _safe_filename(self.document_name)
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            path = os.path.join(path, filename)
        with open(path, "wb") as f:
            f.write(self.content)
        return path


@dataclass
class PdfRenderResult:
    """Result of a :meth:`PagrApiClient.render_pdf` call.

    ``document`` is the rendered :class:`PdfDocument` on success, or ``None``
    when the render was blocked/failed — inspect ``issues`` and ``status`` for
    why (a business outcome, not an exception). ``status`` is one of ``"ok"``,
    ``"partial"``, ``"failed"`` or ``"insufficient_credit"``.
    """

    document: Optional[PdfDocument]
    status: str
    message: Optional[str] = None
    issues: list[RenderIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when a rendered PDF came back."""
        return self.document is not None

    @property
    def insufficient_credit(self) -> bool:
        """True when the render was blocked for lack of credit
        (``status == "insufficient_credit"``)."""
        return self.status == "insufficient_credit"

    @classmethod
    def from_error_envelope(cls, data: dict) -> "PdfRenderResult":
        """Build a failed result from the JSON envelope the API returns (with
        HTTP 422) when there is no PDF to stream."""
        return cls(
            document=None,
            status=data.get("status", "failed"),
            message=data.get("message"),
            issues=[RenderIssue.from_api(i) for i in data.get("issues") or []],
        )


@dataclass
class RenderResult:
    """Result of a single-document render.

    ``status`` is one of ``"ok"``, ``"partial"``, ``"failed"`` or
    ``"insufficient_credit"``.

    ``document`` is ``None`` when the document did not render — e.g. it failed
    validation or the organisation had insufficient credit. Inspect ``ok``,
    ``issues`` and ``status`` to find out why.

    ``issues`` is the flat list of :class:`RenderIssue` returned by the API;
    filter it by ``severity`` to find blocking errors versus warnings.
    """

    document: Optional[RenderedDocument]
    status: str
    rendered_count: int = 0
    requested_count: int = 0
    missing_count: int = 0
    message: Optional[str] = None
    issues: list[RenderIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when a rendered document came back."""
        return self.document is not None

    @property
    def insufficient_credit(self) -> bool:
        """True when the render was blocked because the organisation is out
        of credit (``status == "insufficient_credit"``)."""
        return self.status == "insufficient_credit"

    @classmethod
    def from_api(cls, data: dict) -> "RenderResult":
        docs = data.get("documents") or []
        document = RenderedDocument.from_api(docs[0]) if docs else None
        issues = [RenderIssue.from_api(i) for i in data.get("issues") or []]

        return cls(
            document=document,
            status=data.get("status", "ok"),
            rendered_count=data.get("renderedCount", 1 if document else 0),
            requested_count=data.get("requestedCount", 1),
            missing_count=data.get("missingCount", 0 if document else 1),
            message=data.get("message"),
            issues=issues,
        )

    def __str__(self) -> str:
        if self.ok:
            return str(self.document)
        errors = [i.description for i in self.issues if i.is_error]
        reason = "; ".join(errors) or self.message or self.status or "not rendered"
        return f"RenderResult FAILED — {reason}"


@dataclass
class BatchItem:
    """The outcome of a single document within a batch render.

    Correlates one submitted input (by position) to its rendered document or
    the errors that prevented it from rendering.
    """

    index: int
    input: Optional[dict] = None
    document: Optional[RenderedDocument] = None
    issues: list[RenderIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when this input produced a rendered document."""
        return self.document is not None

    def __str__(self) -> str:
        if self.ok:
            return f"[{self.index}] OK — {self.document.document_name}"
        errors = [i.description for i in self.issues if i.is_error]
        reason = "; ".join(errors) or "not rendered"
        return f"[{self.index}] FAILED — {reason}"


@dataclass
class BatchRenderResult:
    """Result of a synchronous batch render.

    Iterable and indexable: ``len(result)``, ``result[i]`` and
    ``for item in result`` all operate over per-input :class:`BatchItem`s.

    ``status`` is one of ``"ok"``, ``"partial"``, ``"failed"`` or
    ``"insufficient_credit"``.

    Correlation contract: the API returns the rendered documents and a flat
    issue list. Each rendered document carries its own ``document_index``, and
    that index is the only correlation — a document whose index is absent or
    out of range is dropped, never guessed onto a slot by position. Each issue
    likewise attaches to its item via ``document_index`` (batch-wide issues,
    whose ``document_index`` is ``None``, attach to every item). A slot that
    ends up with neither a document nor an issue is marked failed with a
    synthetic "not rendered" issue.

    ``missing_count`` is ``requested_count − rendered_count`` — every document
    not rendered, whatever the reason (validation-blocked, render-failed, or
    left unattempted after a credit stop). It is computed here rather than read
    from the response, since that subtraction *is* its definition. :attr:`ok`
    is derived from it.
    """

    items: list[BatchItem]
    status: str
    message: Optional[str]
    requested_count: int
    rendered_count: int
    missing_count: int

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, i) -> BatchItem:
        return self.items[i]

    @property
    def succeeded(self) -> list[BatchItem]:
        """The items that produced a rendered document."""
        return [it for it in self.items if it.ok]

    @property
    def failed(self) -> list[BatchItem]:
        """The items that did not render; inspect each item's ``issues``."""
        return [it for it in self.items if not it.ok]

    @property
    def documents(self) -> list[RenderedDocument]:
        """All successfully rendered documents."""
        return [it.document for it in self.items if it.document is not None]

    @property
    def insufficient_credit(self) -> bool:
        """True when the batch was cut short because the organisation ran out
        of credit (``status == "insufficient_credit"``)."""
        return self.status == "insufficient_credit"

    @property
    def ok(self) -> bool:
        """True when every requested document rendered and credit sufficed."""
        return self.missing_count == 0 and not self.insufficient_credit

    def save_all(self, directory: str) -> list[str]:
        """Write every rendered document to a directory.

        Only documents that carry inline content (rendered with
        ``include_document=True``) are written.

        Args:
            directory: Destination directory; created if it does not exist.

        Returns:
            The paths that were written.
        """
        os.makedirs(directory, exist_ok=True)
        written = []
        for it in self.items:
            if it.document is not None and it.document.document_base64 is not None:
                written.append(it.document.save(directory))
        return written

    @classmethod
    def from_api(
        cls, data: dict, inputs: Optional[list[dict]] = None
    ) -> "BatchRenderResult":
        """Build a result from the API response, correlating inputs to outcomes.

        Args:
            data: The decoded render response body.
            inputs: The originally submitted document data sets, used to
                correlate each item by position. When omitted, items carry no
                ``input`` reference.

        Returns:
            The assembled batch result.
        """
        status = data.get("status", "ok")
        rendered_count = data.get("renderedCount", 0)
        requested_count = data.get("requestedCount", 0)

        docs = [RenderedDocument.from_api(d) for d in data.get("documents", [])]
        all_issues = [RenderIssue.from_api(i) for i in data.get("issues") or []]

        n = requested_count or (len(inputs) if inputs is not None else len(docs))
        items = [
            BatchItem(
                index=i,
                input=(inputs[i] if inputs is not None and i < len(inputs) else None),
            )
            for i in range(n)
        ]

        # Distribute issues to their slot. Batch-wide issues (document_index is
        # None) attach to every item.
        for issue in all_issues:
            idx = issue.document_index
            if idx is None:
                for it in items:
                    it.issues.append(issue)
            elif 0 <= idx < len(items):
                items[idx].issues.append(issue)

        # Place each rendered document at the slot it reports via
        # document_index. The API guarantees that index on every document, and
        # it is the only correlation: a document whose index is absent or out
        # of range is dropped, never guessed onto a slot by position.
        for doc in docs:
            idx = doc.document_index
            if idx is not None and 0 <= idx < len(items):
                items[idx].document = doc

        # Anything left without a document or a reason is a silent render failure.
        for it in items:
            if it.document is None and not it.issues:
                it.issues = [
                    RenderIssue(
                        type=RenderIssueType.UNKNOWN,
                        severity=RenderIssueSeverity.ERROR,
                        description="not rendered",
                        document_index=it.index,
                    )
                ]

        requested = requested_count or n
        rendered = rendered_count or len(docs)
        return cls(
            items=items,
            status=status,
            message=data.get("message"),
            requested_count=requested,
            rendered_count=rendered,
            # By definition, not a value read from the response — see the class
            # docstring. Clamped at 0 so a server sending rendered > requested
            # can never produce a negative count.
            missing_count=max(0, requested - rendered),
        )


class RenderJobState(Enum):
    """Lifecycle state of an async render job.

    ``QUEUED`` (just enqueued) and ``PENDING`` (queued or rendering) are
    non-terminal; ``COMPLETED`` (documents produced, including partial/
    credit-stopped runs) and ``FAILED`` (nothing produced) are terminal.

    :attr:`UNKNOWN` is a client-side fail-open fallback, not a server value:
    an unrecognised state parses to it rather than raising, and is treated as
    **terminal** so a new server state can never trap a ``while not done`` poll
    loop in an infinite wait.
    """

    QUEUED = "queued"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @classmethod
    def from_api(cls, value: Optional[str]) -> "RenderJobState":
        if value is not None:
            for member in cls:
                if member.value.lower() == str(value).lower():
                    return member
        return cls.UNKNOWN

    @property
    def is_terminal(self) -> bool:
        """True once the job has stopped advancing. ``UNKNOWN`` counts as
        terminal (fail-open) so an unrecognised state ends the poll loop."""
        return self not in (RenderJobState.QUEUED, RenderJobState.PENDING)


class RenderOutcome(Enum):
    """Render outcome of a job/callback, mirroring the sync envelope's status
    vocabulary. ``None`` (not this enum) is used while a job is still pending;
    once decided it is one of these.

    :attr:`UNKNOWN` is a client-side fail-open fallback for an unrecognised
    server value, so new outcomes never crash an older client.
    """

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    INSUFFICIENT_CREDIT = "insufficient_credit"
    UNKNOWN = "unknown"

    @classmethod
    def from_api(cls, value: Optional[str]) -> "RenderOutcome":
        if value is not None:
            for member in cls:
                if member.value.lower() == str(value).lower():
                    return member
        return cls.UNKNOWN


@dataclass
class RenderJob:
    """Reference to an enqueued async render job, returned by
    ``PagrApiClient.enqueue_batch_render``.

    ``state`` is the job lifecycle, normally :attr:`RenderJobState.QUEUED` on
    creation; ``requested_count`` is the number of documents submitted. Track
    progress via the webhook callbacks or by polling
    ``PagrApiClient.get_job_status`` (or ``PagrApiClient.wait_for_job``).
    """

    job_id: UUID
    requested_count: int
    state: RenderJobState

    @classmethod
    def from_api(cls, data: dict) -> "RenderJob":
        return cls(
            job_id=UUID(require(data, "jobId")),
            requested_count=data.get("requestedCount", 0),
            state=RenderJobState.from_api(data.get("state")),
        )

    def __str__(self) -> str:
        return (
            f"RenderJob {self.job_id} — {self.requested_count} doc(s), "
            f"state={self.state.value}"
        )


@dataclass
class RenderJobStatus:
    """Status of an async render job, returned by the polling endpoint
    ``GET /v1/render/jobs/{jobId}``.

    Lifecycle and outcome are separate fields. ``state`` is the job lifecycle:
    ``"pending"`` (queued or rendering), ``"completed"`` (finished; documents
    were produced, including partial/credit-stopped runs), or ``"failed"``
    (produced nothing; ``failure_reason`` describes why). ``status`` is the
    render outcome using the same vocabulary as the sync envelope —
    ``"ok"`` / ``"partial"`` / ``"failed"`` / ``"insufficient_credit"`` — and
    is ``None`` while the job is still pending. ``issues`` carries the
    per-document diagnostics (capped at 100 server-side); the counts stay
    exact.
    """

    job_id: UUID
    state: RenderJobState
    status: Optional[RenderOutcome]
    rendered_count: int
    requested_count: int
    missing_count: int
    started_at: datetime
    issues: list[RenderIssue] = field(default_factory=list)
    completed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None

    @property
    def done(self) -> bool:
        """True once the job reached a terminal state.

        Terminal means ``COMPLETED`` or ``FAILED`` — and also
        :attr:`RenderJobState.UNKNOWN` (fail-open), so an unrecognised server
        state ends a ``while not status.done`` poll loop rather than spinning
        forever."""
        return self.state.is_terminal

    @property
    def ok(self) -> bool:
        """True when the job completed and every document rendered."""
        return self.state is RenderJobState.COMPLETED and self.status is RenderOutcome.OK

    @property
    def insufficient_credit(self) -> bool:
        """True when the job stopped early because the organisation ran out
        of credit."""
        return self.status is RenderOutcome.INSUFFICIENT_CREDIT

    @classmethod
    def from_api(cls, data: dict) -> "RenderJobStatus":
        raw_status = data.get("status")
        return cls(
            job_id=UUID(require(data, "jobId")),
            state=RenderJobState.from_api(data.get("state")),
            # None (not UNKNOWN) while the job is pending and has no outcome yet.
            status=RenderOutcome.from_api(raw_status) if raw_status is not None else None,
            rendered_count=data.get("renderedCount", 0),
            requested_count=data.get("requestedCount", 0),
            missing_count=data.get("missingCount", 0),
            started_at=parse_dt_required(require(data, "startedAt")),
            issues=[RenderIssue.from_api(i) for i in data.get("issues") or []],
            completed_at=parse_dt(data.get("completedAt")),
            failure_reason=data.get("failureReason"),
        )

    def __str__(self) -> str:
        status = self.status.value if self.status is not None else None
        return (
            f"RenderJobStatus {self.job_id} — state={self.state.value} "
            f"status={status} ({self.rendered_count} rendered)"
        )


@dataclass
class RenderProgress:
    """A per-document progress webhook delivered during an async render.

    One is sent for each document that successfully renders. ``processed`` is
    how many have completed so far (completion order) and ``requested_count``
    is the batch size. Documents render in parallel, so callbacks arrive out
    of input order — ``document_index`` is the field that correlates this
    document back to its input (the embedded ``document`` carries the same
    value).
    """

    job_id: UUID
    processed: int
    requested_count: int
    document_index: int
    document: RenderedDocument

    @property
    def progress_pct(self) -> float:
        """Completion percentage (0-100), computed client-side from
        ``processed`` / ``requested_count`` — not a wire field."""
        return (self.processed / self.requested_count) * 100 if self.requested_count else 0.0

    @classmethod
    def from_api(cls, data: dict) -> "RenderProgress":
        return cls(
            job_id=UUID(require(data, "jobId")),
            processed=require(data, "processed"),
            requested_count=require(data, "requestedCount"),
            document_index=require(data, "documentIndex"),
            document=RenderedDocument.from_api(require(data, "document")),
        )


@dataclass
class RenderCompletion:
    """The final webhook delivered once an async render job finishes.

    ``state`` is the terminal lifecycle value — ``"completed"`` (one or more
    documents produced, including partial and credit-stopped runs) or
    ``"failed"`` (nothing produced); the callback only ever fires at a
    terminal state, so ``state`` is never ``"pending"`` here. ``status`` is
    the render outcome: ``"ok"``, ``"partial"``, ``"insufficient_credit"`` or
    ``"failed"``. ``missing_count`` is ``requested_count − rendered_count``
    (every document not rendered, whatever the reason); ``issues`` carries the
    per-document diagnostics, each with its ``document_index``.
    """

    job_id: UUID
    state: RenderJobState
    status: RenderOutcome
    rendered_count: int
    requested_count: int
    missing_count: int = 0
    message: Optional[str] = None
    issues: list[RenderIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every document in the job rendered."""
        return self.status is RenderOutcome.OK

    @property
    def insufficient_credit(self) -> bool:
        """True when the job stopped early because the organisation ran out
        of credit."""
        return self.status is RenderOutcome.INSUFFICIENT_CREDIT

    @classmethod
    def from_api(cls, data: dict) -> "RenderCompletion":
        return cls(
            job_id=UUID(require(data, "jobId")),
            state=RenderJobState.from_api(data.get("state")),
            status=RenderOutcome.from_api(data.get("status")),
            rendered_count=data.get("renderedCount", 0),
            requested_count=data.get("requestedCount", 0),
            missing_count=data.get("missingCount", 0),
            message=data.get("message"),
            issues=[RenderIssue.from_api(i) for i in data.get("issues") or []],
        )


def parse_callback(payload: dict) -> Union[RenderProgress, RenderCompletion]:
    """Parse an incoming async-render webhook body into the right typed object.

    A progress callback carries a ``document`` (plus ``processed`` /
    ``documentIndex``); the final completion callback does not (it carries
    ``state`` / ``status`` / ``renderedCount`` / ``requestedCount``). The full
    expected shape is validated before dispatch, so a payload matching neither
    shape raises :class:`~pagr.exceptions.PagrDecodeError` rather than being
    silently mis-parsed into a bogus-but-valid-looking completion.

    Args:
        payload: The decoded JSON body POSTed to the callback URL.

    Returns:
        A :class:`RenderProgress` for per-document callbacks, or a
        :class:`RenderCompletion` for the final callback.

    Raises:
        PagrDecodeError: If ``payload`` is not a dict, or matches neither the
            progress nor the completion shape.
    """
    if not isinstance(payload, dict):
        raise PagrDecodeError(
            f"webhook payload must be a JSON object, not {type(payload).__name__}"
        )
    if payload.get("document") is not None:
        # Progress: must carry the per-document correlation fields.
        _require_keys(payload, ("jobId", "processed", "requestedCount", "documentIndex"),
                      shape="progress")
        return RenderProgress.from_api(payload)
    # Completion: must carry the terminal-state fields.
    _require_keys(payload, ("jobId", "state", "status"), shape="completion")
    return RenderCompletion.from_api(payload)


def _require_keys(payload: dict, keys: tuple, *, shape: str) -> None:
    """Raise :class:`PagrDecodeError` if ``payload`` is missing any of ``keys``."""
    missing = [k for k in keys if k not in payload]
    if missing:
        raise PagrDecodeError(
            f"webhook payload looks like a {shape} callback but is missing "
            f"required field(s): {missing}"
        )
