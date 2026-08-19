"""Single-document rendering and its options.

Covers:
  - the standard render call and reading the RenderResult
  - inspecting render issues
  - language override
  - persist=False (get the PDF bytes without storing the document)
  - passing data as a JSON string instead of a dict

Run with the [examples] extra installed and a .env file providing
TEST_KEY_PUBLIC.
"""

import asyncio
import copy
import json
import os

from dotenv import load_dotenv

from pagr import PagrApiClient

load_dotenv()
API_KEY = os.getenv("TEST_KEY_PUBLIC", "")

OUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")


async def standard_render(client: PagrApiClient, template, version, data: dict):
    """Render one document and save the PDF. include_document=True returns the
    PDF inline (base64); leave it False when you only need the metadata."""
    result = await client.render(
        template.id, data, version=version.version_number, include_document=True
    )
    print(f"ok={result.ok}  rendered {result.rendered_count}/{result.requested_count}")

    # Business failures (invalid data, insufficient credit) come back as data
    # on the result, not as exceptions:
    if not result.ok:
        print(f"failed: {result.message or result.status}")
        for issue in result.issues:
            print(f"  [{issue.severity.value}] {issue.type.value}: {issue.description}")
        return

    document = result.document
    print(f"document: {document.document_name}  pages={document.page_count}  "
          f"duration={document.render_duration}ms")
    print(f"view online: {document.view_url}")

    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"saved: {document.save(OUT_DIR)}")

    # Warnings can be present even on a successful render:
    for issue in result.issues:
        print(f"  note [{issue.severity.value}]: {issue.description}")


async def render_with_language(client: PagrApiClient, template, version, data: dict):
    """Templates with translations render in the requested language.

    ``translations`` is an opaque JSON string keyed by language code — there's
    no dedicated "list languages" endpoint, so discover the available codes
    by parsing it yourself.
    """
    languages = list(json.loads(version.translations or "{}").keys())
    if not languages:
        print("\nno translations on this version — skipping language render")
        return

    language = languages[0]
    result = await client.render(
        template.id, data, version=version.version_number, language=language
    )
    print(f"\navailable languages: {languages}")
    print(f"language={language!r} → ok={result.ok}")


async def render_without_persistence(client: PagrApiClient, template, version, data: dict):
    """persist=False renders the PDF without storing a document server-side —
    the bytes in the response are the only copy."""
    result = await client.render(
        template.id, data,
        version=version.version_number, include_document=True, persist=False,
    )
    if result.ok:
        pdf = result.document.to_bytes()
        print(f"\npersist=False → {len(pdf)} bytes, nothing stored server-side")


async def render_from_json_string(client: PagrApiClient, template, version, data: dict):
    """Data may be a JSON string as well as a dict — handy when the payload
    already arrives serialised (queue, request body, file)."""
    result = await client.render(
        template.id, json.dumps(data), version=version.version_number
    )
    print(f"\nrender(json_string) → ok={result.ok}")


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

        await standard_render(client, template, version, data)
        await render_with_language(client, template, version, data)
        await render_without_persistence(client, template, version, data)
        await render_from_json_string(client, template, version, data)


if __name__ == "__main__":
    asyncio.run(main())
