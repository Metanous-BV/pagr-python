"""Validation examples: severity levels and test vs. production key behaviour.

Severity semantics:
  Information  — always renders (test and production)
  Warning      — only renders in test/preview; blocked in production
  Error        — never renders

Run with the [examples] extra installed and a .env file providing
TEST_KEY_PUBLIC / PROD_KEY_PUBLIC.
"""

import asyncio
import copy
import os

from dotenv import load_dotenv

from pagr import PagrApiClient, RenderIssueSeverity

load_dotenv()
TEST_KEY = os.getenv("TEST_KEY_PUBLIC")
PROD_KEY = os.getenv("PROD_KEY_PUBLIC")


def print_validation(label: str, result):
    """Print a ValidationResponse with severity breakdown."""
    print(f"\n  {label}")
    if not result.issues:
        print("    No issues — valid in all environments.")
        return
    for issue in result.issues:
        doc = f" (doc {issue.document_index})" if issue.document_index is not None else ""
        elem = f" [{issue.element_id}]" if issue.element_id else ""
        print(f"    [{issue.severity.value:11}] {issue.type.value}{elem}{doc}: {issue.description}")
    prod_safe = result.is_valid and not result.warnings
    print(f"    renders in test/preview : {result.is_valid}  (no errors)")
    print(f"    renders in production   : {prod_safe}  (no errors and no warnings)")


async def demo_single(client: PagrApiClient, template_id, version: int, base_doc: dict):
    """Show how a single document's issues are categorised."""
    print("\n--- Single document validation ---")

    # Valid document: no issues expected.
    result = await client.validate(template_id, base_doc, version=version)
    print_validation("Base document (should be clean)", result)

    # Introduce a missing optional binding to trigger a warning.
    doc_warning = copy.deepcopy(base_doc)
    doc_warning.pop(next(iter(doc_warning)), None)   # remove first field as a stand-in
    result = await client.validate(template_id, doc_warning, version=version)
    print_validation("Document with a field removed (may trigger warning)", result)
    if result.warnings and result.is_valid:
        print("    -> Renders in test/preview but may fail in production.")

    # Empty document: likely to produce errors.
    result = await client.validate(template_id, {}, version=version)
    print_validation("Empty document (likely errors)", result)
    if result.errors:
        print("    -> Will not render in any environment.")


async def demo_batch(client: PagrApiClient, template_id, version: int, base_doc: dict):
    """Show per-document issue attribution in a batch."""
    print("\n--- Batch validation ---")

    docs = [
        base_doc,                                        # 0: should be clean
        {**base_doc, **{next(iter(base_doc)): None}},    # 1: null value — may warn
        {},                                              # 2: empty — likely errors
    ]
    result = await client.validate(template_id, docs, version=version)

    for idx in range(len(docs)):
        per_doc = result.issues_for(idx)
        has_error = any(i.severity is RenderIssueSeverity.ERROR for i in per_doc)
        has_warn  = any(i.severity is RenderIssueSeverity.WARNING for i in per_doc)
        status = "ERROR" if has_error else ("WARNING" if has_warn else "OK")
        print(f"  doc[{idx}] {status} — {len(per_doc)} issue(s)")
        for issue in per_doc:
            print(f"    [{issue.severity.value}] {issue.type.value}: {issue.description}")


def print_render(label: str, result):
    """Print a RenderResult."""
    print(f"\n  {label}")
    if result.ok:
        doc = result.document
        print(f"    OK — {doc.document_name}  pages={doc.page_count}  bytes={doc.file_size_bytes}")
    else:
        print(f"    FAILED — {result.message or result.status}")
        for issue in result.issues:
            print(f"    [{issue.severity.value}] {issue.description}")


async def demo_test_vs_prod(template_id, version: int, doc: dict):
    """Validate and render the same document with each key."""
    print("\n--- Test key vs. production key ---")

    async with PagrApiClient(api_key=TEST_KEY) as client:
        test_validation = await client.validate(template_id, doc, version=version)
        test_render     = await client.render(template_id, doc, version=version, include_document=True)

    async with PagrApiClient(api_key=PROD_KEY) as client:
        prod_validation = await client.validate(template_id, doc, version=version)
        prod_render     = await client.render(template_id, doc, version=version, include_document=True)

    print_validation("Test key — validation", test_validation)
    print_render("Test key — render",         test_render)
    print_validation("Prod key — validation", prod_validation)
    print_render("Prod key — render",         prod_render)


async def main():
    if not TEST_KEY:
        print("TEST_KEY_PUBLIC not set in .env — aborting.")
        return

    async with PagrApiClient(api_key=TEST_KEY) as client:
        templates = await client.get_templates()
        if not templates:
            print("No templates available — aborting.")
            return

        template = templates[0]
        versions = await client.get_template_versions(template.id)
        version  = versions[0]
        version_number = version.version_number
        base_doc = copy.deepcopy(version.sample_data) or {}

        print(f"Using template: {template.name} (v{version_number})")

        await demo_single(client, template.id, version_number, base_doc)
        await demo_batch(client, template.id, version_number, base_doc)

    doc_with_warning = copy.deepcopy(base_doc)
    doc_with_warning.pop(next(iter(doc_with_warning)), None)
    await demo_test_vs_prod(template.id, version_number, doc_with_warning)


if __name__ == "__main__":
    asyncio.run(main())
