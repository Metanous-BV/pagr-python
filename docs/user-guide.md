# Pagr Python SDK — User Guide

Everything you need to render documents with the `pagr` client from your own
Python application.

> All examples assume you are inside an `async def` and have created a client.
> Every client method must be `await`ed.

## Table of contents

1. [Installation](#1-installation)
2. [Authentication & the client](#2-authentication--the-client)
3. [Working with templates](#3-working-with-templates)
4. [Rendering a single document](#4-rendering-a-single-document)
5. [Batch rendering (synchronous)](#5-batch-rendering-synchronous)
6. [Async rendering (fire-and-forget + webhooks)](#6-async-rendering-fire-and-forget--webhooks)
7. [Validation (no render, no credit)](#7-validation-no-render-no-credit)
8. [Understanding render issues](#8-understanding-render-issues)
9. [Documents & fonts](#9-documents--fonts)
10. [Organisation stats](#10-organisation-stats)
11. [Error handling](#11-error-handling)
12. [Cancellation](#12-cancellation)
13. [Complete example](#13-complete-example)
14. [FAQ / gotchas](#14-faq--gotchas)

## 1. Installation

```bash
pip install git+https://github.com/Metanous-BV/pagr-python.git
```

Optional extras:

```bash
pip install "pagr-sdk[examples] @ git+https://github.com/Metanous-BV/pagr-python.git"   # adds python-dotenv, to run examples/
```

- **Python 3.9+** required.
- Core runtime dependency: [`httpx`](https://www.python-httpx.org/) only.
- The package ships type hints (`py.typed`), so editors and `mypy` get full
  autocompletion and checking.

## 2. Authentication & the client

You need an **API key**. The **base URL** defaults to the hosted Pagr API, so
you only pass it to target another instance (e.g. a local dev server).

```python
import asyncio
from pagr import PagrApiClient

async def main():
    async with PagrApiClient("pagr_prod_xxx") as client:
        stats = await client.get_org_stats()
        print(stats)

asyncio.run(main())
```

### API keys: test vs production

The key **prefix** decides the mode, you do not pass a flag:

| Prefix | Mode | Behaviour |
|--------|------|-----------|
| `pagr_test_*` | Test | Renders are watermarked/limited; batches are capped at **10 documents per request**. Good for development. |
| `pagr_prod_*` | Production | Full rendering, larger batches, consumes real credit. |

Keep keys in environment variables or a secrets manager and never commit them. The
examples read `TEST_KEY_PUBLIC` / `PROD_KEY_PUBLIC` from a `.env` file.

### Managing the connection

`PagrApiClient` wraps a pooled `httpx.AsyncClient`. **Always** use it as an async
context manager (`async with`) so the connection pool is closed cleanly. If you
can't — e.g. a long-lived client kept for the lifetime of your app, the
recommended pattern for servers — call `await client.aclose()` yourself when
done.

There is no pool-size knob exposed (`httpx.Limits` appears nowhere in this
SDK) — you get `httpx`'s defaults. If you need to tune pool size, construct
your own `httpx.AsyncClient` and use `HttpTransport`/lower-level pieces
directly; this SDK does not currently support passing in a pre-configured
client.

> The client is bound to the event loop it is first used on. Do not share one
> instance across separate `asyncio.run()` calls; create it inside the loop that
> will use it (or reuse a single loop).

Constructor options:

```python
PagrApiClient(
    api_key: str,            # "pagr_test_…" or "pagr_prod_…"
    base_url: str = None,    # defaults to the hosted Pagr API; override for e.g. local dev
    timeout: float = 30.0,   # default per-request timeout in seconds
    max_retries: int = 2,    # retries for transient failures on GET requests (0 disables)
)
```

`timeout` is the *default* per request; it is applied afresh to each attempt, so
a GET that times out on every retry can take up to roughly
`timeout × (max_retries + 1)` plus backoff before raising. The render endpoints
(`render`, `render_pdf`, `render_batch`,
`enqueue_batch_render`) and `download_document` also accept a per-call `timeout=`
override — raise it for a document that may approach the server's 60-second
render budget (the 30s default would otherwise trip a client-side
`PagrTimeoutError` first).

You can swap the key at runtime with `client.set_api_key("pagr_prod_…")`. The key
is read fresh for each request attempt, so a swap takes effect on the next call;
if you rotate the key while a request is mid-retry, later attempts of that same
request use the new key.

**Timestamps.** Every `datetime` the SDK returns (e.g. `rendered_at`,
`published_at`, `started_at`) is timezone-aware and normalised to UTC. If the API
ever sends a value with no offset, the SDK assumes UTC rather than handing back a
naive `datetime`; an explicit non-UTC offset is preserved as sent.

## 3. Working with templates

```python
# List templates (paged)
page = await client.get_templates()
print(page.total, "templates")
for t in page:                       # PagedResult is iterable & indexable
    print(t.name, t.id)

# Only templates in a project
page = await client.get_templates(project_id=some_uuid)

# One template
template = await client.get_template(template_id)

# Its versions (paged)
versions = await client.get_template_versions(template_id)

# A specific version, or the latest published one
v = await client.get_template_version(template_id, version=2)
v = await client.get_template_version(template_id)             # "latest" (default)
```

`TemplateVersion.sample_data` is **already parsed into a dict**, a great
starting point for a render payload:

```python
import copy
doc = copy.deepcopy(version.sample_data)   # copy before mutating
doc["Title"] = "Acme Q3 Invoice"
```

Other version operations:

```python
# Update the document-name template on a version
await client.update_document_name_template(template_id, version_number=2,
                                           document_name_template="Invoice-{{Number}}")

# Preview image URL, if the version has one
url = await client.get_preview_image_url(template_id, version_number=2)
```

### Paging & filtering

Every list method (`get_templates`, `get_template_versions`, `get_documents`)
accepts the same keyword options:

```python
page = await client.get_templates(
    skip=0,
    take=25,
    sort_by="name",
    sort_direction="asc",
    search="invoice",
    filters=[{"field": "name", "op": "contains", "value": "2026"}],
)
```

**Filter fields are endpoint-specific.** Each list endpoint allows a different
set of `field`/`op` combinations (e.g. `get_documents` supports `environment` and
`renderedAt`, which the template endpoints do not) — see each method's docstring
for the allowed table, mirrored from `pagr.filters`. The SDK validates `field`
and `op` client-side and raises `ValueError` for an unknown one, rather than
sending it: the server silently *ignores* an unknown filter and returns the
**unfiltered** result set, so a typo'd field (`"documentNam"`) would otherwise
quietly return everything. Filter `value`s may be plain scalars, or a `UUID` /
`datetime` (coerced to their wire string form for you). `take` is clamped to
1–200 server-side.

`PagedResult` gives you `.items`, `.total` (across *all* pages), `.skip`,
`.take`, and `.has_more`:

```python
skip = 0
while True:
    page = await client.get_templates(skip=skip, take=50)
    for t in page:
        ...
    if not page.has_more:
        break
    skip += len(page)
```

The SDK does not currently provide an auto-paging iterator, so this loop is
the canonical pattern for walking all pages.

## 4. Rendering a single document

```python
result = await client.render(
    template_id,
    json_data={"Title": "Hello", "Amount": 42},
    version=None,
    include_document=True,    # not the default — see below
    language=None,
    persist=True,
)
```

**Parameters:**

- **`json_data`** (`dict | str`) — the document data. A dict or a JSON-encoded
  string; the SDK normalises either. A JSON string must encode an *object* —
  each document is limited to 50 MB of JSON, nested at most 32 levels deep.
- **`version`** (`int | None`) — the template version to render. Defaults to
  `None`, meaning the latest *published* version.
- **`include_document`** (`bool`, default `False`) — when `True`, the response
  carries the PDF inline (base64 on the wire). You never decode it yourself:
  `result.document.to_bytes()` returns the decoded PDF bytes.
- **`language`** (`str | None`) — language variant for multilingual templates.
- **`persist`** (`bool`) — when `True` (default), the server stores the render
  so it can be retrieved later via `get_documents` / `download_document`.

Inspect the result:

```python
if result.ok:                          # True when a document comes back
    doc = result.document              # RenderedDocument
    print(doc.document_name, doc.page_count, doc.view_url)
    result.document.save("out/")       # writes out/<name>.pdf
    raw = result.document.to_bytes()   # decoded PDF bytes
else:
    if result.insufficient_credit:
        print("Out of credit:", result.message)
    for issue in result.issues:
        print(issue)                   # e.g. "Error: MissingBinding [total] — …"
```

> ⚠️ **Note:** `to_bytes()` raises `ValueError` when the render was made
> *without* `include_document=True` — no bytes came back to decode. Either
> re-render with it, or fetch the stored PDF with `download_document`.

> ⚠️ **Note on `save()`:** when you pass a directory, the document's own name
> is used as the filename and `.pdf` is appended if missing (the API's names
> carry no extension).

### `persist=False` - do not store the render

`persist=False` returns the **same JSON envelope** as a normal render — only
the values differ: `result.document.id` and `.view_url` are `None` (nothing was
stored, so there is nothing to reference), and the PDF bytes are always included
inline (`document_base64` is forced on, since they are then the only copy). So
`result.document.to_bytes()` / `.save()` work regardless of `include_document`,
and every other field (`document_name`, `page_count`, `render_duration`,
`status`, `issues`) is real.

Use `persist=False` for one-off documents you don't want stored (and don't want
appearing in `get_documents`).

### `render_pdf` — stream the raw PDF instead of JSON

If you'd rather receive the PDF binary directly than base64-decode a JSON field,
`render_pdf` opts into the `Accept: application/pdf` response. It renders a
single document and carries the metadata in response headers:

```python
result = await client.render_pdf(template_id, {"Title": "Hello"}, persist=False)

if result.ok:
    result.document.save("out/")            # PdfDocument: .to_bytes() / .save()
    print(result.document.page_count, result.document.view_url)
else:
    # blocked/failed: no PDF to stream, reasons come back as data (not an error)
    for issue in result.issues:
        print(issue)
```

`render_pdf` handles exactly one document. A blocked or failed render has no PDF
to return, so `result.ok` is `False` and `result.issues` / `result.status`
explain why — a business outcome, never an exception. `result.document` is a
`PdfDocument`, a deliberately lean type exposing only what the raw-PDF response
actually carries (`document_id`, `page_count`, `render_duration`, `view_url`,
`issue_count`).

## 5. Batch rendering (synchronous)

Render many documents in one request. You get back a `BatchRenderResult` that
**correlates each input to its outcome by the `documentIndex` the server reports**.

```python
docs = [{"Title": f"Doc {i}"} for i in range(5)]
batch = await client.render_batch(template_id, docs, version=1, include_document=True)

print(f"{batch.rendered_count}/{batch.requested_count} rendered, ok={batch.ok}")

for item in batch:                     # iterable & indexable
    if item.ok:
        print(item.index, item.document.document_name)
    else:
        reasons = [i.description for i in item.issues]
        print(item.index, "FAILED", reasons, "input:", item.input)

batch.succeeded          # list[BatchItem] that rendered
batch.failed             # list[BatchItem] that did not
batch.documents          # list[RenderedDocument] (successes only)
batch.ok                 # True if nothing missing and credit sufficient
batch[0].document        # index access
batch.save_all("out/")   # write every rendered doc to a directory → list of paths
```

Each `BatchItem` carries `index`, the original `input`, the resulting
`document` (or `None`), and any `issues`. `save_all` only writes items that were
rendered with `include_document=True`. Correlation is exact: every rendered
document carries its own `document.document_index`, so each lands on the slot of
the input that produced it even when earlier documents are blocked and leave
gaps. That index is the *only* correlation — a document whose index is absent or
out of range is dropped, never guessed onto a slot by position.

`missing_count` is `requested_count - rendered_count` (computed by the SDK, since
that subtraction is the field's definition), and `ok` is `True` only when it is
`0` and credit sufficed — so `ok` answers "did the whole batch render", while
`failed` answers it slot by slot.

> **Test-mode batch cap:** with a `pagr_test_*` key the server rejects batches
> of more than **10 documents per request** (an HTTP 400, raised as `ApiError`).
> A production key is required for larger runs.

## 6. Async rendering (fire-and-forget + webhooks)

For large jobs, `enqueue_batch_render` returns **immediately** with a
`RenderJob`. The server renders in the background and reports progress two ways:
**webhook callbacks** you host, or **polling**.

```python
job = await client.enqueue_batch_render(
    template_id,
    docs,
    callback_url="https://your-app.example/pagr/callback",
    version=1,
    include_document=False,
    persist=True,
)
print(job.job_id, job.requested_count, job.state)
```

### Option A — receive webhooks

The server POSTs **N + 1** callbacks to your `callback_url`:

- one **progress** callback per rendered document —
  `{jobId, processed, requestedCount, documentIndex, document}`.
  The parsed `RenderProgress` object also exposes a computed `progress_pct`
  property (`processed / requested_count`, not a wire field). Documents render
  in parallel, so callbacks arrive out of order — `document_index` correlates
  each one back to its input.
- one **completion** callback at the end —
  `{jobId, state, status, renderedCount, requestedCount, missingCount, message, issues}`.
  `state` is the terminal lifecycle (`completed`/`failed`); `status` is the
  render outcome (`ok`/`partial`/`failed`/`insufficient_credit`).

Each callback carries three headers:

| Header | Meaning |
|---|---|
| `X-Pagr-Signature` | `t=<unix seconds>,v1=<hex>` — HMAC proof the POST came from Pagr. See below. |
| `X-Pagr-Event` | `render.progress`, `render.completed` or `render.failed`. |
| `X-Pagr-Delivery` | Stable id for one logical delivery; **retries repeat it**, so deduplicate on it. |

Delivery is **best-effort with retries** (up to 5 attempts, exponential backoff
from 2s, 30s timeout per attempt) and runs with bounded parallelism, so
callbacks can arrive **out of order and more than once** — treat your handler as
idempotent and use `X-Pagr-Delivery` to drop duplicates. Polling
(`get_job_status`, Option B) remains the authoritative signal. A document that
fails to render gets **no progress callback** — detect it via
`renderedCount < requestedCount` at completion.

#### Verifying the signature

Anyone who discovers your callback URL can POST to it, so verify the signature
before acting on a payload. `parse_signed_callback` verifies and parses in one
step — prefer it over `parse_callback`:

```python
import os

from fastapi import FastAPI, Request, Response
from pagr import (
    PagrSignatureError, RenderProgress, RenderCompletion, parse_signed_callback,
)

app = FastAPI()
SECRET = os.environ["PAGR_WEBHOOK_SECRET"]

@app.post("/pagr/callback")
async def callback(request: Request):
    try:
        cb = parse_signed_callback(
            await request.body(),                       # raw bytes, not .json()
            request.headers.get("X-Pagr-Signature"),
            SECRET,
        )
    except PagrSignatureError:
        return Response(status_code=400)                # not from Pagr — drop it

    if isinstance(cb, RenderProgress):
        print(f"{cb.processed}/{cb.requested_count} ({cb.progress_pct:.0f}%) {cb.document.document_name}")
    elif isinstance(cb, RenderCompletion):
        print("done:", cb.state, cb.status, cb.rendered_count, "/", cb.requested_count)
        if cb.insufficient_credit:
            print("stopped early:", cb.message)
    return {"ok": True}
```

> ⚠️ **Pass the raw body bytes.** The signature covers the exact bytes Pagr
> POSTed. A body that your framework parsed to a dict and you re-serialized
> will *not* verify, even though the JSON value is identical — key order and
> separators change. This is the most common cause of a signature that "should"
> match but doesn't.

Get the secret from **Settings → API keys** in the Pagr web app; it is
per-organisation and issued on first access. Rotating it there keeps the old
secret valid for a 24-hour grace period (Pagr signs with both, and verification
accepts either), so you can deploy the new value without dropping deliveries.

`verify_signature(body, header, secret)` is available separately if you need to
verify without parsing — e.g. to reject a bad delivery before queueing the raw
body for later processing. Both raise `PagrSignatureError` on any failure
(missing/malformed header, timestamp outside the 5-minute replay window,
no matching signature) rather than returning a bool, so a forgotten check
cannot silently let a forged callback through. `tolerance=` widens or narrows
the replay window if your clock skew demands it.

If you have a use case with no secret configured at all, `parse_callback`
still decodes an already-parsed payload without verifying anything — but then
put an unguessable token in the `callback_url` you register and reject requests
that don't carry it.

> Your endpoint must be reachable *by the Pagr server*. `127.0.0.1` works only
> when the API runs on the same host (e.g. local dev). Otherwise expose a public
> URL (ngrok, a real endpoint, etc.) — or use polling, below, which needs no
> inbound connectivity at all.

### Option B — poll for status

A reliable alternative that needs no public URL:

```python
status = await client.get_job_status(job.job_id)   # RenderJobStatus
print(status.state, status.status, status.rendered_count)
if status.done:                    # state is completed or failed
    print("ok" if status.ok else status.failure_reason)
```

Poll on an interval (e.g. `asyncio.sleep(2)`) until `status.done`. `status.state`
is the lifecycle (`pending`/`completed`/`failed`); `status.status` is the render
outcome (`ok`/`partial`/`failed`/`insufficient_credit`, `None` while pending).

Or let the SDK do the polling for you:

```python
status = await client.wait_for_job(job.job_id, poll_interval=2)
```

`wait_for_job` applies an overall **5-minute deadline by default**
(`timeout=None`, which resolves to `pagr.WAIT_FOR_JOB_DEFAULT_TIMEOUT`) so a
stuck job can't leave your `await` hanging forever — it raises
`PagrTimeoutError` if the job hasn't reached a terminal state by then. Pass a
different number of seconds to change the deadline, or `timeout=math.inf` to
opt into genuinely unbounded polling:

```python
import math
status = await client.wait_for_job(job.job_id, timeout=math.inf)  # no deadline
status = await client.wait_for_job(job.job_id, timeout=60)        # 1 minute deadline
```

See [Cancellation](#12-cancellation) for how to stop a `wait_for_job` call
early from your own code (e.g. a user-initiated cancel button), as opposed to
the deadline above (which is the SDK giving up on your behalf).

## 7. Validation (no render, no credit)

Check data against a template *before* rendering:

```python
result = await client.validate(template_id, {"Title": "Hello"}, version=1)

if result.is_valid:                    # the production gate: no issue >= Warning
    print("good to render")
else:
    for issue in result.errors:        # the narrower, Error-only check
        print(issue.document_index, issue)

# Validate a batch and inspect per-document issues
result = await client.validate(template_id, [doc_a, doc_b])
for i in range(2):
    print(i, result.issues_for(i))     # includes batch-wide issues too
```

`ValidationResponse` is iterable over its issues and exposes `.is_valid`,
`.errors`, and `.issues_for(index)`. `is_valid` is the production gate (no
issue of `Warning` or `Error` severity); inspect `.errors` directly for the
narrower, Error-only check.

## 8. Understanding render issues

Both rendering and validation return a flat list of **`RenderIssue`** objects.
Each has:

- **`severity`** a `RenderIssueSeverity`: `INFORMATION < WARNING < ERROR`.
- **`type`**: a `RenderIssueType` category (`MISSING_BINDING`, `INVALID_COLOR`,
  `RENDER_TIMEOUT`, … or `UNKNOWN` for categories newer than your SDK).
- **`description`**: human-readable text.
- **`element_id`**: the template element it relates to, if any.
- **`document_index`**: which document in the batch (or `None` for batch-wide).
- **`is_error`**: `severity is ERROR`.

`is_valid` is the production gate: it blocks on any issue `>= WARNING`, so a
document is production-valid only when all its issues are `INFORMATION`.
Inspect `.errors` directly for the narrower, Error-only check.

```python
for issue in result.issues:
    print(issue)   # "Error: MissingBinding [total] — Binding 'total' not found"
```

Unknown severities fail **closed** (treated as `ERROR`); unknown types map to
`UNKNOWN`. This means a newer server never crashes an older client.

## 9. Documents & fonts

```python
# List previously rendered documents (paged, same query options as templates)
page = await client.get_documents(take=50, search="invoice")

# Metadata for one document
doc = await client.get_document(document_id)     # RenderDocument

# Download its PDF bytes
pdf = await client.download_document(document_id)
with open("invoice.pdf", "wb") as f:
    f.write(pdf)

# Available font families
fonts = await client.get_fonts()                 # list[str]
```

> `RenderDocument` (from `get_documents`/`get_document`) carries **metadata
> only** — no inline bytes. Use `download_document` to get the PDF. This is
> different from `RenderedDocument` (returned by `render`), which *can* carry
> inline bytes when `include_document=True`.

## 10. Organisation stats

```python
stats = await client.get_org_stats()   # OrgStats
print(stats)                           # pretty multi-line summary
stats.pages_available                  # renders/pages left this period
stats.tokens_available                 # AI tokens left this period
stats.tier, stats.user_count
```

## 11. Error handling

Failures raise a typed exception; **catch `pagr.PagrError`** to handle any of
them — HTTP errors *and* transport failures (timeouts, connection errors) both
live under `PagrError`, so no raw `httpx` exception ever leaks through:

```python
from pagr import (
    PagrError, AuthenticationError, ForbiddenError, NotFoundError,
    PayloadTooLargeError, ValidationFailedError, RateLimitError, ApiError,
    PagrTimeoutError, PagrConnectionError,
)

try:
    result = await client.render(template_id, doc)
except AuthenticationError:          # 401 — bad/missing key
    ...
except (PagrTimeoutError, PagrConnectionError):  # never reached the server
    ...
except PagrError as e:               # any Pagr error
    print(e.status_code, e.code, str(e))
```

| Exception | HTTP | Meaning |
|-----------|------|---------|
| `AuthenticationError` | 401 | Invalid or missing API key |
| `ForbiddenError` | 403 | Authenticated but not allowed |
| `NotFoundError` | 404 | Template/resource not found |
| `PayloadTooLargeError` | 413 | A document exceeds the max payload size |
| `ValidationFailedError` | 422 | Request body couldn't be bound/validated |
| `RateLimitError` | 429 | Too many requests |
| `ApiError` | other 4xx/5xx | Anything else |
| `PagrTimeoutError` | — | Request exceeded the timeout |
| `PagrConnectionError` | — | Request never reached the API (connection/DNS) |

Every exception carries `.status_code` and `.code` (the API's error code, when
present; both `None` for the transport-level errors, which never reached the
server).

### Retries

Read-only (GET) calls — listing, fetching, downloading — are retried
automatically on transient *server-side* failures (HTTP 500/502/503/504,
timeouts, and connection errors) with capped exponential backoff and jitter. So
a `PagrConnectionError` from a GET means the retries were already exhausted.

**Rate limits (429) are not retried.** They reflect *your own* request volume,
so `RateLimitError` is raised immediately for you to handle — slow down, lower
concurrency, or spread calls out. (The limit is a sliding 60-second window with
no `Retry-After`, so a short client-side retry wouldn't clear it anyway.)

Tune the count with `PagrApiClient(..., max_retries=2)` (the default; pass `0`
to disable). Writes — rendering and template edits — are **never** retried: the
API has no idempotency keys, so repeating a request that was already applied
could render or charge twice. Handle a failed write by retrying it yourself only
when you know it is safe.

### Errors vs. outcomes 

Some things that "fail" are **not** exceptions — they are normal results you
inspect:

- **Insufficient credit** → `result.insufficient_credit == True` (the batch may
  be *partially* rendered; check `result.failed`). Not raised.
- **A document that fails validation** → comes back with `document is None` and
  `issues` explaining why. Not raised.

Raised exceptions are for *transport/protocol* problems (auth, rate limit,
malformed request). Business outcomes are data.

## 12. Cancellation

The SDK invents no cancellation abstraction of its own: the supported way to
stop an in-flight call is Python's native `asyncio.Task.cancel()`.

```python
task = asyncio.ensure_future(client.wait_for_job(job.job_id))
...
task.cancel()
try:
    await task
except asyncio.CancelledError:
    print("gave up waiting")
```

**Where cancellation takes effect.** Every `await` is a cancellation point.
The two loops most worth knowing about:

- The **retry backoff sleep** inside `HttpTransport._send` (the
  `await asyncio.sleep(delay)` in `_backoff`, `pagr/_http.py`) — cancelling a
  GET that is currently backing off between retries stops it immediately, it
  does not wait out the remaining backoff delay first.
- The **poll sleep** inside `wait_for_job` (`pagr/client.py`) — cancelling
  while it's asleep between polls stops it immediately rather than waiting
  out the rest of `poll_interval`.

In both cases cancellation breaks the sleep; it does not wait it out.

**Cancellation vs. timeouts — never confused.** These are deliberately
different exceptions:

- *The SDK's own timeout* (a per-request `timeout=` elapsing, or
  `wait_for_job`'s deadline elapsing) raises a `PagrTimeoutError` — a
  `PagrError` subclass, part of the normal error-handling contract in
  [section 11](#11-error-handling).
- *Your code cancelling the task* raises `asyncio.CancelledError` — **never**
  wrapped into a `PagrError`. Nothing in the SDK catches `BaseException` or a
  bare `except:`, so `CancelledError` (which derives from `BaseException`,
  not `Exception`, since Python 3.8) always propagates to your `await task`
  uncaught. Catch it separately from `except PagrError`, not alongside it.

**What cancellation does *not* do:** cancelling a task does not close the
client. `task.cancel()` only unwinds the one coroutine you cancelled — the
underlying pooled `httpx.AsyncClient` is untouched, connections stay open, and
nothing about your `async with PagrApiClient(...) as client:` block or a
manual `client.aclose()` changes. Cleanup is still your `async with` block's
job (or your own `finally: await client.aclose()`), even after cancelling
every outstanding call.

## 13. Complete example

```python
import asyncio, copy, os
from pagr import PagrApiClient

async def main():
    async with PagrApiClient(os.environ["TEST_KEY_PUBLIC"]) as client:
        # pick a template + version
        templates = await client.get_templates(search="invoice")
        template = templates[0]
        version = await client.get_template_version(template.id)   # latest

        # build data from the sample
        doc = copy.deepcopy(version.sample_data)
        doc["Title"] = "Acme Q3"

        # validate, then render
        if not (await client.validate(template.id, doc)).is_valid:
            print("invalid data"); return

        result = await client.render(template.id, doc, include_document=True)
        if result.ok:
            path = result.document.save("out/")
            print("saved", path)
        elif result.insufficient_credit:
            print("out of credit")
        else:
            print("failed:", result)

asyncio.run(main())
```

## 14. FAQ / gotchas

- **Do I have to use `async`?** Yes, the whole SDK is async. Wrap calls in
  `asyncio.run(...)` for scripts, or `await` them inside your framework's async
  handlers.
- **`to_bytes()` raised `ValueError`.** You didn't pass `include_document=True`,
  so no bytes came back. Either re-render with it, or use `download_document`.
  See the note in [section 4](#4-rendering-a-single-document).
- **My webhook receiver never fires.** The Pagr server must be able to reach your
  `callback_url`. `127.0.0.1` only works if the API is on the same machine. Or
  switch to `get_job_status` polling. See [section 6](#6-async-rendering-fire-and-forget--webhooks).
- **`save()` filename.** When you pass a directory, the document's own name is
  used and `.pdf` is appended if missing (the API's names carry no extension).
- **JSON string or dict?** Anywhere the SDK takes document/template data you can
  pass either a `dict` or a JSON-encoded `str`.
