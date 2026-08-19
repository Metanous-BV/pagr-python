"""render_pdf — stream the raw PDF instead of the JSON envelope.

`render_pdf` opts into the API's `Accept: application/pdf` response: it renders
a single document and streams the PDF binary back, carrying the document
metadata in `X-Pagr-*` response headers rather than a JSON body. Use it when you
want the bytes directly instead of base64-decoding a JSON field.

Covers:
  - a clean render → PdfDocument with header metadata, saved to disk
  - a blocked render → PdfRenderResult.ok is False, reasons come back as data
    (issues/status), never as an exception

The blocked case injects a value the content sanitizer rejects
(DangerousContent, an Error-severity failure blocked in both test and
production mode), so it reproduces with a free test key.

Run with the [examples] extra installed and a .env file providing
TEST_KEY_PUBLIC.
"""

import asyncio
import copy
import os

from dotenv import load_dotenv

from pagr import PagrApiClient

load_dotenv()
API_KEY = os.getenv("TEST_KEY_PUBLIC", "")

OUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")

# Rejected by the render data validator's content sanitizer → a DangerousContent
# issue (Error severity), which the render gate blocks in both test and prod.
_DANGEROUS_VALUE = "<script>alert('blocked by the content sanitizer')</script>"


async def clean_render(client: PagrApiClient, template, version, data: dict):
    """A successful render: the PDF streams back with metadata in the headers."""
    result = await client.render_pdf(
        template.id, data, version=version.version_number
    )
    if not result.ok:
        print(f"unexpected: render was blocked ({result.status})")
        for issue in result.issues:
            print(f"  [{issue.severity.value}] {issue.type.value}: {issue.description}")
        return

    doc = result.document
    print(f"ok — {doc.document_name}  pages={doc.page_count}  "
          f"duration={doc.render_duration}ms")
    print(f"document_id={doc.document_id}  view_url={doc.view_url}")

    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"saved: {doc.save(OUT_DIR)}  ({len(doc.to_bytes())} bytes)")


async def render_without_persistence(client: PagrApiClient, template, version, data: dict):
    """persist=False streams the bytes without storing a document — so
    document_id and view_url come back None."""
    result = await client.render_pdf(
        template.id, data, version=version.version_number, persist=False
    )
    if result.ok:
        doc = result.document
        print(f"\npersist=False → {len(doc.to_bytes())} bytes, "
              f"document_id={doc.document_id} view_url={doc.view_url}")


async def blocked_render(client: PagrApiClient, template, version):
    """A blocked render has no PDF to stream. The API returns 422 with the JSON
    envelope; the SDK surfaces it as data (ok=False), never an exception."""
    result = await client.render_pdf(
        template.id,
        {"value": _DANGEROUS_VALUE},
        version=version.version_number,
    )
    print(f"\nblocked render → ok={result.ok}  status={result.status}")
    if not result.ok:
        for issue in result.issues:
            print(f"  [{issue.severity.value}] {issue.type.value}: {issue.description}")


async def main():
    async with PagrApiClient(api_key=API_KEY) as client:
        templates = await client.get_templates(take=1)
        if len(templates) == 0:
            print("No templates in this organisation — create one first.")
            return
        template = templates[0]
        versions = await client.get_template_versions(
            template.id, sort_by="versionNumber", sort_direction="desc"
        )
        version = versions[0]
        print(f"Using template: {template.name} (v{version.version_number})\n")

        data = copy.deepcopy(version.sample_data)
        data["Title"] = "Alice"

        await clean_render(client, template, version, data)
        await render_without_persistence(client, template, version, data)
        await blocked_render(client, template, version)


if __name__ == "__main__":
    asyncio.run(main())
