# Pagr Python SDK — Wiki

`pagr` is the official **async** Python client for the Pagr document-rendering
API. You give it a template ID and some JSON data; it renders PDFs — one at a
time, in batches, or as fire-and-forget background jobs with webhook callbacks.

This wiki is for application developers *using* the SDK:

| Doc | For | Read it if you… |
|-----|-----|-----------------|
| **[User Guide](./user-guide.md)** | Application developers *using* the SDK | …want to render documents from your own Python app. |

Maintainer-facing setup, testing, and release conventions live in
[CONTRIBUTING.md](../CONTRIBUTING.md) instead.

## 30-second overview

```python
import asyncio
from pagr import PagrApiClient

async def main():
    async with PagrApiClient("pagr_prod_…") as client:
        result = await client.render(template_id, {"Title": "Hello"}, include_document=True)
        if result.ok:
            result.document.save("out/")

asyncio.run(main())
```

## What it covers

- **Templates** — list templates & versions, read sample data, update the document-name template.
- **Rendering** — single, synchronous batch, and async (webhook/polling) renders.
- **Validation** — check data against a template without rendering or spending credit.
- **Documents** — list, fetch metadata, and download previously rendered PDFs.
- **Fonts & org stats** — list available fonts; read usage/credit for the organisation.

## Key facts at a glance

- **Everything is `async`.** All client methods are coroutines; use `await` and `async with`.
- **Auth is a bearer API key** with a `pagr_test_*` or `pagr_prod_*` prefix — the prefix decides test vs production mode server-side.
- **HTTP errors raise typed exceptions** (`pagr.PagrError` and subclasses).
- **Business outcomes are data, not errors.** A render that fails validation or
  runs out of credit comes back as a normal result object you inspect — it does
  *not* raise.
- **Requires Python 3.9+** and depends only on `httpx`.

## Source layout

```
pagr/
├── __init__.py          # public exports (client, models, exceptions)
├── client.py            # PagrApiClient — the API surface
├── _http.py             # HttpTransport — httpx wrapper + error mapping
├── exceptions.py        # PagrError hierarchy
├── filters.py           # canonical per-endpoint filter field/operator tables
├── webhook.py           # verify_signature, parse_signed_callback (HMAC)
├── py.typed             # PEP 561 marker — SDK ships type hints
└── models/
    ├── template.py      # Template, TemplateVersion
    ├── render.py        # RenderResult, BatchRenderResult, RenderJob, issues, callbacks
    ├── document.py      # RenderDocument, PagedResult[T]
    ├── validation.py    # ValidationResponse
    ├── organisation.py  # OrgStats
    └── _common.py       # shared model helpers
examples/                # runnable scripts, one per topic (see examples/README.md)
tests/                   # pytest suite (respx-mocked HTTP)
```

The authoritative list of public exports is `pagr.__all__` in `__init__.py`.
