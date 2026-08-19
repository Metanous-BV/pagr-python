from dataclasses import dataclass, field

from .render import RenderIssue, RenderIssueSeverity


@dataclass
class ValidationResponse:
    """Validation results for a batch of documents.

    The API returns a single flat list of :class:`RenderIssue`s
    (``ValidateResultDto``); each issue carries the ``document_index`` of the
    document it pertains to (``None`` for batch-wide issues). ``is_valid`` is
    the production gate: it is ``True`` only when no issue is
    :attr:`RenderIssue.is_blocking_production` (i.e. ``Warning`` or
    ``Error``). Callers who want the narrower, Error-only check should
    inspect :attr:`errors` directly instead.

    Iterable and indexable over the issues.
    """

    issues: list[RenderIssue] = field(default_factory=list)

    def __iter__(self):
        return iter(self.issues)

    def __len__(self) -> int:
        return len(self.issues)

    def __getitem__(self, i) -> RenderIssue:
        return self.issues[i]

    @property
    def is_valid(self) -> bool:
        """True when no issue is ``Warning`` or ``Error`` severity (i.e. no
        issue blocks a production render). For the narrower, Error-only
        check, use :attr:`errors` directly."""
        return not any(i.severity.is_blocking_production for i in self.issues)

    @property
    def errors(self) -> list[RenderIssue]:
        return [i for i in self.issues if i.severity is RenderIssueSeverity.ERROR]

    @property
    def warnings(self) -> list[RenderIssue]:
        return [i for i in self.issues if i.severity is RenderIssueSeverity.WARNING]

    def issues_for(self, document_index: int) -> list[RenderIssue]:
        """Issues pertaining to a specific document.

        Includes batch-wide issues (those whose ``document_index`` is ``None``).
        """
        return [
            i
            for i in self.issues
            if i.document_index is None or i.document_index == document_index
        ]

    @classmethod
    def from_api(cls, data: dict) -> "ValidationResponse":
        return cls(
            issues=[RenderIssue.from_api(i) for i in data.get("issues", [])],
        )

    def __str__(self) -> str:
        if self.is_valid:
            header = "valid"
        else:
            header = f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        body = "\n".join(f"  {i}" for i in self.issues)
        return f"ValidationResponse ({header})\n{body}" if body else f"ValidationResponse ({header})"
