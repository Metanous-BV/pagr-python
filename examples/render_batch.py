"""Synchronous batch rendering: many documents in one call.

The call blocks until the whole batch is processed. For large batches, prefer
the fire-and-forget flow with webhook callbacks — see batch_async.py.

Run with the [examples] extra installed and a .env file providing
PROD_KEY_PUBLIC.
"""

import asyncio
import copy
import os

from dotenv import load_dotenv

from pagr import PagrApiClient

load_dotenv()
API_KEY = os.getenv("PROD_KEY_PUBLIC", "")


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
        print(f"Using template: {template.name} (v{version.version_number})")

        # One data document per PDF to render:
        docs = []
        for i in range(1, 20):
            doc = copy.deepcopy(version.sample_data)
            doc["Title"] = f"Document {i}"
            docs.append(doc)

        batch = await client.render_batch(
            template.id, docs,
            version=version.version_number,
            include_document=True,
        )
        print(f"ok={batch.ok}  rendered {batch.rendered_count}/{batch.requested_count}")
        if batch.insufficient_credit:
            print(f"stopped early: {batch.message}")

        # Each item keeps its input index, so results map back to your inputs
        # even when some fail:
        for item in batch.succeeded:
            print(f"  [{item.index}] OK  {item.document.document_name}")
        for item in batch.failed:
            reasons = "; ".join(issue.description for issue in item.issues)
            print(f"  [{item.index}] FAILED  {reasons}")

        # Save every rendered PDF in one go:
        out_dir = os.path.join(os.path.dirname(__file__), "test_output")
        os.makedirs(out_dir, exist_ok=True)
        paths = batch.save_all(out_dir)
        print(f"save_all → {len(paths)} file(s) in {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
