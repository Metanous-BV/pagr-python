import asyncio
import random
from typing import Awaitable, Callable, Optional

import httpx

from .exceptions import (
    ApiError,
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
    PagrConnectionError,
    PagrDecodeError,
    PagrTimeoutError,
    PayloadTooLargeError,
    RateLimitError,
    ValidationFailedError,
)

# Maps HTTP status codes to the exception type raised for them.
_STATUS_EXCEPTIONS = {
    401: AuthenticationError,
    403: ForbiddenError,
    404: NotFoundError,
    413: PayloadTooLargeError,
    422: ValidationFailedError,
    429: RateLimitError,
}

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_BACKOFF_MAX = 8.0
#: Defensive upper bound on a server ``Retry-After`` value we will actually
#: wait. The header is honored as-is up to this many seconds; a larger (or
#: hostile) value is clamped so a single retry can never park the caller for an
#: unbounded time. This is intentionally much larger than ``backoff_max`` — the
#: latter caps our *own* computed backoff, not the server's explicit request.
DEFAULT_RETRY_AFTER_MAX = 60.0

# HTTP statuses worth retrying on an idempotent (GET) request: transient
# server/gateway failures (500/502/504) and a full render queue (503
# ``QueueFull``). 4xx statuses are deterministic and never retried — including
# 429: rate limiting reflects the *caller's own* request volume, so it is
# surfaced as ``RateLimitError`` for the caller to handle (slow down, lower
# concurrency), not retried silently. Retrying it would also be futile here —
# the sliding 60-second window far exceeds our backoff and the server sends no
# ``Retry-After``.
RETRIABLE_STATUS = frozenset({500, 502, 503, 504})


def _clean_params(params: Optional[dict]) -> Optional[dict]:
    """Drop keys whose value is ``None`` so optional query args can be passed
    unconditionally without appearing in the URL."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header value expressed as an integer number of
    seconds. Returns ``None`` when absent or not an integer (e.g. an HTTP-date),
    since the SDK does not interpret the date form."""
    if not value:
        return None
    try:
        return float(int(value))
    except ValueError:
        return None


def _decode_json(response: httpx.Response):
    """Parse a successful response's JSON body, wrapping failures.

    A non-JSON or empty body (e.g. a redirect that was followed to an HTML
    page, or an unexpected ``204``) makes ``response.json()`` raise a bare
    ``json.JSONDecodeError``. Callers are promised they only ever see
    ``PagrError`` subclasses, so translate it into a
    :class:`~pagr.exceptions.PagrDecodeError`.
    """
    try:
        return response.json()
    except ValueError as exc:
        raise PagrDecodeError(
            "the Pagr API returned a response whose body was not valid JSON",
            status_code=response.status_code,
        ) from exc


class HttpTransport:
    """Thin async HTTP layer shared by all client methods.

    Owns the underlying ``httpx.AsyncClient``, attaches the bearer-token
    auth header, drops ``None`` query params, converts 4xx/5xx responses into
    the typed :class:`pagr.exceptions.PagrError` hierarchy, and wraps transport
    failures (timeouts, connection errors) in ``PagrTimeoutError`` /
    ``PagrConnectionError`` so callers only ever see ``PagrError`` subclasses.

    Idempotent GET requests are retried on transient failures (see
    :data:`RETRIABLE_STATUS` plus timeouts and connection errors) with capped
    exponential backoff and full jitter. Writes (POST/PATCH) are never retried:
    the API has no idempotency keys, so a request that was applied but whose
    response was lost must not be repeated (it would render/charge twice).
    """

    def __init__(
        self,
        baseurl: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        retry_after_max: float = DEFAULT_RETRY_AFTER_MAX,
    ):
        self.baseurl = baseurl.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.retry_after_max = retry_after_max
        # follow_redirects=True so a 3xx is resolved to its real body rather
        # than handed back as a redirect response whose ``.json()`` would raise.
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    def _timeout(self, timeout: Optional[float]):
        """Resolve a per-request timeout override to an httpx timeout argument.

        ``None`` means "use the client's configured default"; httpx spells that
        as :data:`httpx.USE_CLIENT_DEFAULT` (passing ``timeout=None`` would
        instead *disable* the timeout entirely).
        """
        return timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT

    def set_api_key(self, value: str):
        """Replace the API key used for subsequent requests.

        Args:
            value: The new API key.
        """
        self.api_key = value

    @property
    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def get(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        """Send a GET request and raise on error responses.

        Args:
            path: Path relative to the base URL.
            params: Optional query-string parameters. ``None`` values are
                dropped so callers can pass optional arguments unconditionally.
            timeout: Optional per-request timeout override in seconds; ``None``
                uses the client's configured default.

        Returns:
            The successful HTTP response. Read ``.json()`` for JSON bodies or
            ``.content`` for binary bodies (e.g. PDF downloads).
        """
        url = f"{self.baseurl}/{path}"
        return await self._send(
            lambda: self._client.get(
                url,
                headers=self._auth_headers,
                params=_clean_params(params),
                timeout=self._timeout(timeout),
            ),
            retriable=self.max_retries > 0,
        )

    async def post_json(
        self,
        path: str,
        payload: dict,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        non_raising_statuses: frozenset = frozenset(),
        *,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        """Send a JSON POST request and raise on error responses.

        Args:
            path: Path relative to the base URL.
            payload: The JSON-serialisable request body.
            params: Optional query-string parameters (``None`` values dropped).
            headers: Optional extra request headers, merged over the default
                auth + ``Content-Type`` headers (e.g. ``Accept`` negotiation).
            non_raising_statuses: HTTP status codes that should be returned as
                a normal response instead of raising a :class:`PagrError`, so
                the caller can inspect the body itself (e.g. a 422 that carries
                a business-outcome envelope rather than a bind error).
            timeout: Optional per-request timeout override in seconds; ``None``
                uses the client's configured default.

        Returns:
            The successful HTTP response (or a response whose status is in
            ``non_raising_statuses``).
        """
        url = f"{self.baseurl}/{path}"
        request_headers = {**self._auth_headers, "Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        return await self._send(
            lambda: self._client.post(
                url,
                json=payload,
                headers=request_headers,
                params=_clean_params(params),
                timeout=self._timeout(timeout),
            ),
            retriable=False,
            non_raising_statuses=non_raising_statuses,
        )

    async def patch_json(
        self, path: str, payload: dict, *, timeout: Optional[float] = None
    ) -> httpx.Response:
        """Send a JSON PATCH request and raise on error responses.

        Args:
            path: Path relative to the base URL.
            payload: The JSON-serialisable request body.
            timeout: Optional per-request timeout override in seconds; ``None``
                uses the client's configured default.

        Returns:
            The successful HTTP response.
        """
        url = f"{self.baseurl}/{path}"
        return await self._send(
            lambda: self._client.patch(
                url,
                json=payload,
                headers={**self._auth_headers, "Content-Type": "application/json"},
                timeout=self._timeout(timeout),
            ),
            retriable=False,
        )

    async def _send(
        self,
        send: Callable[[], Awaitable[httpx.Response]],
        *,
        retriable: bool,
        non_raising_statuses: frozenset = frozenset(),
    ) -> httpx.Response:
        """Run a request, wrapping transport errors and retrying when allowed.

        Args:
            send: A zero-argument factory returning a fresh request coroutine.
                It is called once per attempt — a coroutine can only be awaited
                once, so retries need a new one each time.
            retriable: Whether transient failures should be retried. Only GET
                passes ``True``; writes pass ``False`` so a request that was
                applied but whose response was lost is never silently repeated.
            non_raising_statuses: Status codes to return as-is instead of
                raising, letting the caller parse the response body.

        Returns:
            The successful HTTP response (or one whose status is in
            ``non_raising_statuses``).

        Raises:
            PagrTimeoutError: The request timed out (after exhausting retries).
            PagrConnectionError: The request never reached the API.
            PagrError: A typed error for a 4xx/5xx response.
        """
        max_attempts = (self.max_retries + 1) if retriable else 1
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await send()
            except httpx.TimeoutException as exc:
                if retriable and attempt < max_attempts:
                    await self._backoff(attempt, None)
                    continue
                raise PagrTimeoutError(
                    "Request to the Pagr API timed out"
                ) from exc
            except httpx.RequestError as exc:
                if retriable and attempt < max_attempts:
                    await self._backoff(attempt, None)
                    continue
                raise PagrConnectionError(
                    "Could not reach the Pagr API"
                ) from exc

            if (
                retriable
                and attempt < max_attempts
                and response.status_code in RETRIABLE_STATUS
            ):
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            # A caller may opt to handle certain statuses itself (e.g. a 422
            # that carries a business-outcome envelope, not a bind error).
            if response.status_code in non_raising_statuses:
                return response

            self._raise_for_status(response)
            return response

    async def _backoff(self, attempt: int, retry_after: Optional[str]):
        """Sleep before the next retry.

        When the server sends a ``Retry-After`` header carrying an integer
        number of seconds, that value is honored as-is — only clamped to
        ``retry_after_max`` (default 60s) as a defensive upper bound, never
        shortened below what the server asked for. Otherwise (no header, or a
        non-integer value such as an HTTP-date) it uses capped exponential
        backoff with full jitter.

        Args:
            attempt: The 1-based number of the attempt that just failed.
            retry_after: The response's ``Retry-After`` header value, if any.
        """
        delay = _parse_retry_after(retry_after)
        if delay is not None:
            delay = min(delay, self.retry_after_max)
        else:
            ceiling = min(
                self.backoff_base * (2 ** (attempt - 1)), self.backoff_max
            )
            delay = random.uniform(0, ceiling)
        await asyncio.sleep(delay)

    def _raise_for_status(self, response: httpx.Response):
        """Raise a typed :class:`PagrError` for any 4xx/5xx response.

        Args:
            response: The HTTP response to inspect.

        Raises:
            PagrError: A subclass matching the status code, or ``ApiError`` for
                unmapped codes.
        """
        # Successful responses are returned. Redirects are followed by httpx
        # (follow_redirects=True), so a 3xx never reaches here.
        if response.status_code < 400:
            return

        code, message = self._parse_error(response)
        exc_type = _STATUS_EXCEPTIONS.get(response.status_code, ApiError)
        if exc_type is RateLimitError:
            raise RateLimitError(
                message,
                status_code=response.status_code,
                code=code,
                retry_after=_parse_retry_after(response.headers.get("Retry-After")),
            )
        raise exc_type(message, status_code=response.status_code, code=code)

    @staticmethod
    def _parse_error(response: httpx.Response):
        """Extract a (code, message) pair from an error response.

        Reads the API's ``{"error": {"code", "message"}}`` envelope, falling back
        to the raw body when the response is not the expected JSON.

        Args:
            response: The error HTTP response.

        Returns:
            A ``(code, message)`` tuple; ``code`` may be ``None``.
        """
        code = None
        message = response.text
        try:
            body = response.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code = err.get("code")
                message = err.get("message") or message
        except (ValueError, AttributeError):
            pass
        return code, message

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
