# Pagr Python SDK

Async Python client for the Pagr document rendering API: manage templates, render
documents (single, batch, or fire-and-forget with webhooks), validate data, and
read organisation usage stats.

> [!TIP]
> Want to chat live with Pagr engineers? Join us on our
> [Discord server](https://discord.gg/GajJxfKXZ5).

## Requirements

- **Python 3.9** or later.
- A **Pagr API key** — grab it from **Settings → API keys** in the Pagr web app.
  The prefix picks the mode: `pagr_test_*` renders are watermarked and batches are
  capped at 10 documents; `pagr_prod_*` renders for real and consumes credit.
- [`httpx`](https://www.python-httpx.org/) 0.24 or later — installed for you as a
  dependency; no other runtime packages are needed.

## Installation

```bash
pip install git+https://github.com/Metanous-BV/pagr-python.git
```

## Quickstart

```python
import asyncio
from pagr import PagrApiClient

async def main():
    # base_url defaults to the hosted Pagr API; pass it only to target another instance.
    async with PagrApiClient("YOUR_API_KEY") as client:
        templates = await client.get_templates()
        template = templates[0]

        # Single render
        result = await client.render(template.id, {"Title": "Hello"})
        if result.ok:
            print(result.document.document_name)

asyncio.run(main())
```

The client is fully typed. All errors derive from `pagr.PagrError`
(`AuthenticationError`, `ForbiddenError`, `NotFoundError`, `PayloadTooLargeError`,
`ValidationFailedError`, `RateLimitError`, or a generic `ApiError`; plus
`PagrTimeoutError` / `PagrConnectionError` for transport failures), so
`except PagrError` is a complete safety net. Business outcomes — a failed
validation or insufficient credit — come back as data on the result object,
not as exceptions.

Read-only (GET) calls are retried automatically on transient server-side
failures (HTTP 500/502/503/504, timeouts, connection errors) with exponential
backoff; tune with `PagrApiClient(..., max_retries=2)` (`0` disables). Rate
limits (429) are not retried — they reflect your own call volume, so
`RateLimitError` is raised for you to handle. Writes are never retried.

## Documentation

The full documentation lives in the [docs wiki](docs/README.md):

- **[User Guide](docs/user-guide.md)** — rendering (single, batch,
  async with webhooks), validation, error handling, and more.
- **[Contributing](CONTRIBUTING.md)** — for maintainers of the SDK.

Runnable scripts are in [`examples/`](examples/) — one per topic, from
[`getting_started.py`](examples/getting_started.py) to batch rendering,
async jobs, validation, and error handling. See the
[examples README](examples/README.md) for the full list and setup.

## License

Apache-2.0. See [LICENSE](LICENSE).

- Repository: https://github.com/Metanous-BV/pagr-python
- Issues: https://github.com/Metanous-BV/pagr-python/issues
