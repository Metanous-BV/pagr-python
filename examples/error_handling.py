"""Error handling: the exception hierarchy and when to catch what.

All errors the SDK raises derive from pagr.PagrError:

    PagrError
    ├── AuthenticationError    401 — missing/invalid API key
    ├── ForbiddenError         403 — key lacks access to the resource
    ├── NotFoundError          404 — unknown template/version/document/job
    ├── PayloadTooLargeError   413 — request body over the limit
    ├── ValidationFailedError  422 — request rejected by validation
    ├── RateLimitError         429 — too many requests
    ├── ApiError               any other non-success status
    ├── PagrTimeoutError       request exceeded the timeout
    └── PagrConnectionError    request never reached the API (connection/DNS)

Because transport failures are wrapped too, `except PagrError` is a complete
safety net — you never see a raw httpx exception leak through.

Retries: read-only (GET) calls are retried automatically on transient
server-side failures (5xx, timeouts, connection errors) with backoff, so a
PagrConnectionError from a GET means retries were already exhausted. Rate limits
(429) are NOT retried — they reflect your own call volume, so RateLimitError is
raised for you to handle. Writes are never retried either (no idempotency keys —
a retry could render/charge twice).

Business outcomes — a document that fails to render, insufficient credit —
are NOT exceptions: they come back as data on the result object (result.ok,
result.issues, result.insufficient_credit).

Run with the [examples] extra installed and a .env file providing
TEST_KEY_PUBLIC.
"""

import asyncio
import os
from uuid import UUID

from dotenv import load_dotenv

from pagr import (
    AuthenticationError,
    NotFoundError,
    PagrApiClient,
    PagrConnectionError,
    PagrError,
    PagrTimeoutError,
    RateLimitError,
)

load_dotenv()
API_KEY = os.getenv("TEST_KEY_PUBLIC", "")

NONEXISTENT_ID = UUID("00000000-0000-0000-0000-000000000000")


async def main():
    # Catch specific types when you can act on them:
    async with PagrApiClient(api_key=API_KEY) as client:
        try:
            await client.get_template(NONEXISTENT_ID)
        except NotFoundError as exc:
            print(f"NotFoundError: status={exc.status_code} code={exc.code}")

    # A bad key fails on the first request:
    try:
        async with PagrApiClient(api_key="bad-key") as client:
            await client.get_org_stats()
    except AuthenticationError as exc:
        print(f"AuthenticationError: status={exc.status_code} code={exc.code}")

    # Catch PagrError as the safety net around any SDK call. Every exception
    # exposes status_code, code, and message (status_code/code are None for
    # the transport-level errors, which never reached the server):
    async with PagrApiClient(api_key=API_KEY) as client:
        try:
            await client.get_template(NONEXISTENT_ID)
        except RateLimitError:
            # Not auto-retried — this reflects your own call volume, so it's
            # yours to handle: slow down / lower concurrency and try later.
            print("rate limited — reduce call volume and retry later")
        except (PagrTimeoutError, PagrConnectionError) as exc:
            print(f"transport error, could not reach the API: {exc}")
        except PagrError as exc:
            print(f"{type(exc).__name__}: status={exc.status_code} code={exc.code}")


if __name__ == "__main__":
    asyncio.run(main())
