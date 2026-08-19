"""Your first render: connect, pick a template, render a document, save the PDF.

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


async def main():
    async with PagrApiClient(api_key=API_KEY) as client:
        # Check the service is reachable before doing any work.
        if not await client.get_status():
            print("Pagr API is not healthy — aborting.")
            return
        print(f"Connected to Pagr API {await client.get_version()}")

        # Pick the first template and its newest version.
        templates = await client.get_templates(take=1)
        if len(templates) == 0:
            print("No templates in this organisation — create one first.")
            return
        template = templates[0]

        versions = await client.get_template_versions(
            template.id, sort_by="versionNumber", sort_direction="desc"
        )
        version = versions[0]
        print(f"Rendering template: {template.name} (v{version.version_number})")

        # Every version carries sample data matching its bindings — a good
        # starting point for your own document.
        data = copy.deepcopy(version.sample_data)
        data["Title"] = "My first render"

        result = await client.render(
            template.id, data, version=version.version_number, include_document=True
        )
        if not result.ok:
            print(f"Render failed: {result.message or result.status}")
            for issue in result.issues:
                print(f"  [{issue.severity.value}] {issue.description}")
            return

        document = result.document
        print(
            f"Rendered {document.document_name}: "
            f"{document.page_count} page(s), {document.file_size_bytes} bytes"
        )

        out_dir = os.path.join(os.path.dirname(__file__), "test_output")
        os.makedirs(out_dir, exist_ok=True)
        print(f"Saved to {document.save(out_dir)}")


if __name__ == "__main__":
    asyncio.run(main())
