# Pagr SDK examples

Runnable scripts, one per topic. Start with `getting_started.py` and pick the
others as you need them.

| Example | What it shows |
|---|---|
| [`getting_started.py`](getting_started.py) | Connect, pick a template, render a document, save the PDF. |
| [`templates.py`](templates.py) | Browse templates and versions: paging, search, filters, document-name template, preview image. |
| [`render_single.py`](render_single.py) | Single render options: issues, language override, `persist=False`, JSON-string data. |
| [`render_pdf.py`](render_pdf.py) | Opt-in raw-PDF streaming: `render_pdf` returns the PDF bytes with metadata in headers; blocked renders come back as data. |
| [`render_batch.py`](render_batch.py) | Synchronous batch render: succeeded/failed items, `save_all`. |
| [`batch_async.py`](batch_async.py) | Fire-and-forget batch render, tracked by polling the job status. |
| [`validate.py`](validate.py) | Data validation: severity levels, per-document issues, test vs. production keys. |
| [`documents.py`](documents.py) | Listing and downloading previously rendered documents. |
| [`account.py`](account.py) | Organisation usage stats, available fonts, API key rotation. |
| [`error_handling.py`](error_handling.py) | The exception hierarchy and when to catch what. |

## Setup

```bash
pip install "pagr-sdk[examples] @ git+https://github.com/Metanous-BV/pagr-python.git"   # adds python-dotenv
```

Copy [`.env.example`](.env.example) to `.env` next to the scripts and fill in
your keys:

```env
TEST_KEY_PUBLIC=your-test-api-key
PROD_KEY_PUBLIC=your-prod-api-key     # needed by validate.py, render_batch.py, batch_async.py
```

Then run any example:

```bash
python examples/getting_started.py
```

Rendered PDFs are written to `examples/test_output/`.
