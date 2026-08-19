"""Canonical per-endpoint filter field/operator tables.

This module is the **authoritative source** for which fields each list endpoint
can be filtered on and, per field, which operators are valid.

Why validate client-side at all? The server **silently ignores** an unknown
filter field or operator and returns the *unfiltered* result set — so a typo
(``"documentNam"`` for ``"documentName"``) would not error, it would silently
return everything. Rejecting unknown fields/operators here turns that silent,
data-wrong outcome into an immediate, obvious ``ValueError``.

Field names are the API's camelCase wire names (matching the ``sort_by`` /
docstring tables in :mod:`pagr.client`).
"""

# Operator sets, reused across fields of the same kind.
_EQ = ("eq",)                                  # exact match only (ids/guids)
_STRING = ("eq", "contains")                   # text fields
_ORD = ("eq", "gt", "gte", "lt", "lte")        # numbers and datetimes
_ENUM = ("eq", "neq")                          # closed-vocabulary fields

#: Filters accepted by ``get_templates`` / project-scoped template listing.
TEMPLATE_FILTERS = {
    "name": _STRING,
    "project.guid": _EQ,
    "createdAt": _ORD,
    "updatedAt": _ORD,
}

#: Filters accepted by ``get_template_versions``.
TEMPLATE_VERSION_FILTERS = {
    "versionNumber": _ORD,
    "publishedAt": _ORD,
    "createdAt": _ORD,
    "updatedAt": _ORD,
}

#: Filters accepted by ``get_documents``. Note ``renderDuration`` can be sorted
#: on but not filtered, and ``documentType`` supports neither — so neither
#: appears here.
DOCUMENT_FILTERS = {
    "documentName": _STRING,
    "template.guid": _EQ,
    "versionNumber": _ORD,
    "fileSizeBytes": _ORD,
    "pageCount": _ORD,
    "renderedAt": _ORD,
    "createdAt": _ORD,
    "updatedAt": _ORD,
    "environment": _ENUM,
    "language": _ENUM,
}


def validate_filter(index: int, field: str, op: str, allowed: dict) -> None:
    """Raise ``ValueError`` if ``field``/``op`` are not in ``allowed``.

    Args:
        index: The filter's position in the caller's list (for the message).
        field: The requested filter field (camelCase wire name).
        op: The requested operator.
        allowed: The endpoint's ``{field: (op, ...)}`` table from this module.
    """
    if field not in allowed:
        raise ValueError(
            f"filters[{index}]: unknown field {field!r} for this endpoint; "
            f"allowed fields: {sorted(allowed)}"
        )
    if op not in allowed[field]:
        raise ValueError(
            f"filters[{index}]: operator {op!r} is not valid for field "
            f"{field!r}; allowed operators: {list(allowed[field])}"
        )
