"""Working with persisted documents: listing, metadata, and PDF download.

Every render with persist=True (the default) stores a document you can list
and download later. Run a render example first so there is something to list.

Run with the [examples] extra installed and a .env file providing
TEST_KEY_PUBLIC.
"""

import asyncio
import os

from dotenv import load_dotenv

from pagr import PagrApiClient

load_dotenv()
API_KEY = os.getenv("TEST_KEY_PUBLIC", "")


async def main():
    async with PagrApiClient(api_key=API_KEY) as client:
        # Newest first — the listing supports the same paging/sorting/search
        # options as the template listing:
        page = await client.get_documents(
            skip=0, take=2, sort_by="renderedAt", sort_direction="desc"
        )
        print(f"{page.total} document(s) total, showing {len(page)}")
        if len(page) == 0:
            print("Nothing rendered yet — run getting_started.py first.")
            return

        for doc in page:
            print(f"  • {doc.document_name}  ({doc.environment}, "
                  f"{doc.page_count} page(s), rendered {doc.rendered_at})")

        # Fetch full metadata for one document:
        document = await client.get_document(page[0].id)
        print(f"\n{document.document_name}:")
        print(f"  template:   {document.template_id} v{document.version_number}")
        print(f"  size:       {document.file_size_bytes} bytes")
        print(f"  language:   {document.language}")
        print(f"  view online:{document.view_url}")

        # Download the PDF bytes (unless the file has been cleaned up):
        if document.is_pdf_deleted:
            print("  PDF no longer stored — only metadata remains.")
            return

        pdf = await client.download_document(document.id)
        out_dir = os.path.join(os.path.dirname(__file__), "test_output")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{document.document_name}_downloaded.pdf")
        with open(path, "wb") as fh:
            fh.write(pdf)
        print(f"  downloaded {len(pdf)} bytes → {path}")


if __name__ == "__main__":
    asyncio.run(main())
