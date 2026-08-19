from typing import Optional


class PagrError(Exception):
    """Base exception for all Pagr API errors.

    Catch this to handle any error raised by the SDK's HTTP layer.

    Attributes:
        status_code: The HTTP status code, when known.
        code: The API's machine-readable error code (e.g. ``"EntityNotFound"``,
            ``"TemplateNotFound"``, ``"VersionNotFound"``,
            ``"DocumentNotFound"``, ``"InvalidApiKey"``,
            ``"NoPublishedVersion"``, ``"InsufficientCredit"``,
            ``"PayloadTooLarge"``, ``"QueueFull"``, ``"PdfDeleted"``,
            ``"ValidationError"``), or ``None`` when the error response did
            not carry the expected ``{"error": {"code", "message"}}`` body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
    ):
        self.status_code = status_code
        self.code = code
        super().__init__(message)


class AuthenticationError(PagrError):
    """401 — invalid or missing API key."""


class ForbiddenError(PagrError):
    """403 — authenticated but not allowed to access this resource."""


class NotFoundError(PagrError):
    """404 — template, version, document or job not found."""


class PayloadTooLargeError(PagrError):
    """413 — a submitted document exceeds the maximum payload size (50 MB)."""


class ValidationFailedError(PagrError):
    """422 — the request body could not be bound/validated."""


class RateLimitError(PagrError):
    """429 — too many requests; the organisation exceeded its rate limit for
    this endpoint category over the current sliding 60-second window.

    Attributes:
        retry_after: The number of seconds the server asked the caller to wait
            before retrying, parsed from the ``Retry-After`` response header
            when it carries an integer number of seconds. ``None`` when the
            header is absent or not an integer (e.g. an HTTP-date) — the API
            does not currently send one on 429s, so treat ``None`` as "back off
            using your own policy."
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(message, status_code=status_code, code=code)
        self.retry_after = retry_after


class ApiError(PagrError):
    """Any other unexpected API error (4xx/5xx), including HTTP 400
    (e.g. a test-mode batch over the 10-document limit), 410
    (``code="PdfDeleted"``) and 503 (``code="QueueFull"``)."""


class PagrConnectionError(PagrError):
    """The request never produced an HTTP response.

    Raised when the transport layer fails before or during the request —
    connection refused, DNS failure, TLS handshake error, connection reset,
    or a protocol error. Wraps the underlying ``httpx.RequestError`` (available
    via ``__cause__``). ``status_code`` and ``code`` are ``None``.
    """


class PagrDecodeError(PagrError):
    """A successful HTTP response could not be parsed into the expected shape.

    Raised when the transport received an HTTP response the SDK accepted at the
    status-code level, but whose body was not the JSON/structure a method needs
    — a non-JSON or empty body where JSON was expected, or a payload missing a
    required field. Wraps the underlying ``ValueError``/``KeyError`` (available
    via ``__cause__``) so callers still only ever catch ``PagrError``. When it
    stems from an HTTP response, ``status_code`` carries that response's status;
    ``code`` is ``None``.
    """


class PagrSignatureError(PagrError):
    """An async-render webhook callback could not be proven to come from Pagr.

    Raised by :func:`~pagr.verify_signature` and
    :func:`~pagr.parse_signed_callback` when the ``X-Pagr-Signature`` header is
    absent or malformed, when its timestamp falls outside the accepted replay
    window, or when no signature it carries matches the configured signing
    secret. Every case means the same thing to a receiver: do not act on the
    payload — answer the POST with a 4xx and drop it. ``status_code`` and
    ``code`` are ``None``; this is a local verification failure, not an API
    response.
    """


class PagrTimeoutError(PagrError):
    """The request exceeded the configured timeout.

    Wraps ``httpx.TimeoutException`` (connect/read/write/pool timeout;
    available via ``__cause__``). ``status_code`` and ``code`` are ``None``.
    """
