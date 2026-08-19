from datetime import datetime, timezone
from typing import Optional

from ..exceptions import PagrDecodeError


def require(data: dict, key: str):
    """Return ``data[key]``, raising :class:`PagrDecodeError` if it is absent.

    Use for fields the wire format guarantees. A bare ``KeyError`` from
    ``data[key]`` would escape a ``from_api`` as a non-``PagrError`` exception,
    breaking the SDK's "callers only ever catch ``PagrError``" contract; this
    wraps it so a malformed response surfaces as a ``PagrDecodeError`` instead.
    """
    try:
        return data[key]
    except KeyError as exc:
        raise PagrDecodeError(
            f"response is missing required field {key!r}"
        ) from exc


def parse_dt_required(value: Optional[str]) -> datetime:
    """Like :func:`parse_dt`, but for a timestamp the wire format guarantees.

    Raises :class:`PagrDecodeError` for a missing/empty value instead of
    returning ``None``, so a field typed non-``Optional[datetime]`` never
    silently holds ``None``.
    """
    dt = parse_dt(value)
    if dt is None:
        raise PagrDecodeError("response is missing a required timestamp")
    return dt


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp from the API, or ``None`` for missing values.

    Always returns a timezone-aware ``datetime``. A trailing ``Z`` (which
    ``datetime.fromisoformat`` only understands from Python 3.11 onwards) is
    normalised to ``+00:00``; an explicit offset is kept as-is; a value with no
    offset — which some API fields emit, since they serialise a naive
    ``DateTime`` — is assumed to be UTC rather than returned naive.
    """
    if not value:
        return None
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
