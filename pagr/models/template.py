from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID
import json

from ._common import parse_dt, require


def _parse_sample_data(raw) -> dict:
    """Decode the ``sampleData`` JSON string leniently.

    Empty, malformed, or non-object JSON all decode to an empty dict, never an
    exception: sample data is authored content on the template, and a broken
    one must not take down an otherwise fine ``get_template_version()`` call.
    ``json.JSONDecodeError`` is a ``ValueError``, so letting it escape would
    also break the "callers only ever catch ``PagrError``" contract.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class Template:
    """A document template as listed by the API.

    Carries the template's identity and catalogue metadata (project, latest
    version number, audit fields). The actual template content lives on its
    versions — fetch one with ``PagrApiClient.get_template_version``.

    ``latest_version_number`` is the newest *published* version (``None`` if
    none has been published yet); ``version_count`` is the total number of
    versions, published or not — use ``PagrApiClient.get_template_versions``
    to list them. ``master_template_id``/``master_template_name`` identify the
    master template this template is a child of, when it has one
    (``None`` for a template with no master template).
    """

    id: UUID
    name: str
    document_name_template: Optional[str] = None
    project_id: Optional[UUID] = None
    project_name: Optional[str] = None
    latest_version_number: Optional[int] = None
    version_count: int = 0
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    master_template_id: Optional[UUID] = None
    master_template_name: Optional[str] = None

    @classmethod
    def from_api(cls, data: dict) -> "Template":
        project_id = data.get("projectId")
        master_id = data.get("masterTemplateId")
        return cls(
            id=UUID(require(data, "id")),
            name=require(data, "name"),
            document_name_template=data.get("documentNameTemplate"),
            project_id=UUID(project_id) if project_id else None,
            project_name=data.get("projectName"),
            latest_version_number=data.get("latestVersionNumber"),
            version_count=data.get("versionCount", 0),
            updated_at=parse_dt(data.get("updatedAt")),
            updated_by=data.get("updatedBy"),
            master_template_id=UUID(master_id) if master_id else None,
            master_template_name=data.get("masterTemplateName"),
        )

    def __str__(self):
        return f"{self.name} ({self.id})"


@dataclass
class TemplateVersion:
    """A single version of a template.

    ``template_json`` is the template DSL as a raw JSON string (there is no
    typed model for it yet), whereas ``sample_data`` has already been parsed
    to a dict — it matches the version's bindings and is a good starting
    point for building your own document data. Parsing is lenient: absent,
    malformed, or non-object sample data all yield ``{}``. ``translations``
    is a raw JSON string, or ``None`` when the version has no translations.
    """

    id: UUID
    version_number: int
    template_json: str
    sample_data: dict  # parsed from the JSON string
    template_id: UUID
    # Optional metadata — all default so a caller/test can build a version from
    # just its required fields (matching ``Template``). ``published_at`` and
    # ``published_by`` pair up.
    document_name_template: Optional[str] = None
    published_at: Optional[datetime] = None
    published_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    translations: Optional[str] = None  # raw JSON string, or None

    @classmethod
    def from_api(cls, data: dict) -> "TemplateVersion":
        return cls(
            id=UUID(require(data, "id")),
            version_number=require(data, "versionNumber"),
            # templateJson is kept as a raw string; no typed model yet.
            template_json=require(data, "templateJson"),
            sample_data=_parse_sample_data(data.get("sampleData")),
            document_name_template=data.get("documentNameTemplate"),
            published_at=parse_dt(data.get("publishedAt")),
            published_by=data.get("publishedBy"),
            template_id=UUID(require(data, "templateId")),
            updated_at=parse_dt(data.get("updatedAt")),
            translations=data.get("translations"),
        )

    def __str__(self):
        return (
            f"v{self.version_number} — {self.document_name_template}\n"
            f"  Published: {self.published_at} by {self.published_by}\n"
            f"  Updated:   {self.updated_at}"
        )
    __repr__ = __str__
