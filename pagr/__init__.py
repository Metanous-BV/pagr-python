"""Pagr API client for Python.

Exposes :class:`PagrApiClient` plus the response/webhook models and exception
types used by the SDK.
"""

from .client import PagrApiClient, DEFAULT_BASE_URL, WAIT_FOR_JOB_DEFAULT_TIMEOUT
from .exceptions import (
    ApiError,
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
    PagrConnectionError,
    PagrDecodeError,
    PagrError,
    PagrSignatureError,
    PagrTimeoutError,
    PayloadTooLargeError,
    RateLimitError,
    ValidationFailedError,
)
from .models import (
    BatchItem,
    BatchRenderResult,
    OrgStats,
    PagedResult,
    PdfDocument,
    PdfRenderResult,
    RenderCompletion,
    RenderDocument,
    RenderedDocument,
    RenderIssue,
    RenderIssueSeverity,
    RenderIssueType,
    RenderJob,
    RenderJobState,
    RenderJobStatus,
    RenderOutcome,
    RenderProgress,
    RenderResult,
    Template,
    TemplateVersion,
    ValidationResponse,
    parse_callback,
)
from .webhook import (
    DEFAULT_TOLERANCE,
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    parse_signed_callback,
    verify_signature,
)

__version__ = "1.0.0"

__all__ = [
    "PagrApiClient",
    "DEFAULT_BASE_URL",
    "WAIT_FOR_JOB_DEFAULT_TIMEOUT",

    # models
    "Template",
    "TemplateVersion",
    "RenderedDocument",
    "RenderDocument",
    "RenderResult",
    "RenderIssue",
    "RenderIssueSeverity",
    "RenderIssueType",
    "PdfDocument",
    "PdfRenderResult",
    "BatchItem",
    "BatchRenderResult",
    "PagedResult",
    "RenderJob",
    "RenderJobState",
    "RenderJobStatus",
    "RenderOutcome",
    "RenderProgress",
    "RenderCompletion",
    "parse_callback",
    "ValidationResponse",
    "OrgStats",

    # webhook signature verification
    "verify_signature",
    "parse_signed_callback",
    "SIGNATURE_HEADER",
    "EVENT_HEADER",
    "DELIVERY_HEADER",
    "DEFAULT_TOLERANCE",

    # exceptions
    "PagrError",
    "ApiError",
    "AuthenticationError",
    "ForbiddenError",
    "NotFoundError",
    "PayloadTooLargeError",
    "RateLimitError",
    "ValidationFailedError",
    "PagrConnectionError",
    "PagrTimeoutError",
    "PagrDecodeError",
    "PagrSignatureError",
    "__version__",
]
