from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Generic, Optional, TypeVar
from uuid import UUID

from ._common import parse_dt, parse_dt_required, require

T = TypeVar("T")


@dataclass
class RenderDocument:
    """Metadata for a persisted rendered document, as returned by
    ``PagrApiClient.get_documents`` / ``get_document``.

    ``document_base64`` contains the PDF bytes only when the document was
    rendered with ``include_document=True``; otherwise call
    ``PagrApiClient.download_document`` to fetch them separately.

    Fields of note:

    - ``environment`` — ``"test"`` or ``"production"``, decided by the API
      key that rendered it.
    - ``document_type`` — ``"Template"`` or ``"Invoice"``.
    - ``render_duration`` — server-side render time in milliseconds.
    - ``is_pdf_deleted`` — ``True`` when the stored PDF has been purged by
      retention; ``download_document`` then fails with HTTP 410
      (code ``"PdfDeleted"``) while this metadata stays available.
    - ``language`` — the language variant the document was rendered in, or
      ``None``.
    """

    id: UUID
    document_name: str
    template_id: UUID
    version_number: int
    environment: str
    file_size_bytes: int
    page_count: int
    rendered_at: datetime
    render_duration: float
    view_url: str
    document_type: str
    is_pdf_deleted: bool = False
    language: Optional[str] = None
    # repr=False: this can be megabytes of base64 PDF — keep it out of the
    # default repr / traceback dumps.
    document_base64: Optional[str] = field(default=None, repr=False)

    @classmethod
    def from_api(cls, data: dict) -> "RenderDocument":
        return cls(
            id=UUID(require(data, "id")),
            document_name=require(data, "documentName"),
            template_id=UUID(require(data, "templateId")),
            version_number=require(data, "versionNumber"),
            environment=require(data, "environment"),
            file_size_bytes=require(data, "fileSizeBytes"),
            page_count=require(data, "pageCount"),
            rendered_at=parse_dt_required(require(data, "renderedAt")),
            render_duration=require(data, "renderDuration"),
            view_url=require(data, "viewUrl"),
            document_type=require(data, "documentType"),
            is_pdf_deleted=data.get("isPdfDeleted", False),
            language=data.get("language"),
            document_base64=data.get("documentBase64"),
        )

    def __str__(self):
        return (
            f"RenderDocument {self.document_name} ({self.id})\n"
            f"  Template: {self.template_id} (v{self.version_number})\n"
            f"  Pages:    {self.page_count}, {self.file_size_bytes / 1024:.1f} KB\n"
            f"  Rendered: {self.rendered_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC"
        )


@dataclass
class PagedResult(Generic[T]):
    """A page of results. Iterable and indexable over :attr:`items`.

    ``total`` is the total number of matching records across all pages, not
    just this page. ``skip`` and ``take`` echo the paging the server actually
    applied: ``take`` defaults to 25 and is clamped to 1-200 server-side.
    """

    items: list[T] = field(default_factory=list)
    total: int = 0
    skip: int = 0
    take: int = 0

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, i):
        return self.items[i]

    @property
    def has_more(self) -> bool:
        """True when more records exist beyond this page (fetch them by
        advancing ``skip``)."""
        return self.skip + len(self.items) < self.total

    @classmethod
    def from_api(cls, data: dict, item_factory: Callable[[dict], T]) -> "PagedResult[T]":
        return cls(
            items=[item_factory(it) for it in data.get("items", [])],
            total=data.get("total", 0),
            skip=data.get("skip", 0),
            take=data.get("take", 0),
        )
