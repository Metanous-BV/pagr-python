"""Browsing templates and versions: paging, search, filters, and version metadata.

Covers:
  - paged template listing (skip/take, sorting)
  - free-text search, project scoping, and structured filters
  - fetching a single template by id
  - listing versions, resolving the latest published version
  - updating the document-name template
  - fetching the preview image URL

Run with the [examples] extra installed and a .env file providing
TEST_KEY_PUBLIC.
"""

import asyncio
import os

from dotenv import load_dotenv

from pagr import NotFoundError, PagrApiClient, PagrError

load_dotenv()
API_KEY = os.getenv("TEST_KEY_PUBLIC", "")


async def list_templates(client: PagrApiClient):
    """Paged listing — returns a PagedResult you can iterate and index."""
    page = await client.get_templates(
        skip=0, take=10, sort_by="name", sort_direction="asc"
    )
    print(f"{page.total} template(s) total, showing {len(page)} (has_more={page.has_more})")
    for template in page:
        print(f"  • {template.name} — v{template.latest_version_number}, "
              f"project={template.project_name or '(none)'}")
    return page


async def search_and_filter(client: PagrApiClient, template):
    """The listing endpoints accept search, project scoping, and filters."""
    # Free-text search:
    term = template.name[:3]
    found = await client.get_templates(search=term, take=5)
    print(f"\nsearch={term!r} → {found.total} match(es)")

    # Scope the listing to one project:
    if template.project_id is not None:
        in_project = await client.get_templates(project_id=template.project_id, take=5)
        print(f"project {template.project_name!r} → {in_project.total} template(s)")

    # Structured filters — {field, op, value}. Valid fields are deployment-defined.
    try:
        filtered = await client.get_templates(
            filters=[{"field": "name", "op": "eq", "value": template.name}], take=5
        )
        print(f"filter name eq {template.name!r} → {filtered.total} match(es)")
    except PagrError as exc:
        print(f"filter not supported on this deployment: {exc}")


async def explore_versions(client: PagrApiClient, template):
    """List versions and resolve the latest published one."""
    versions = await client.get_template_versions(
        template.id, take=5, sort_by="versionNumber", sort_direction="desc"
    )
    print(f"\n{template.name} has {versions.total} version(s):")
    for v in versions:
        state = f"published {v.published_at}" if v.published_at else "draft"
        print(f"  • v{v.version_number} — {state}")

    # "latest" resolves to the latest *published* version and 404s when nothing
    # is published yet — fall back to the newest version from the list.
    try:
        version = await client.get_template_version(template.id, "latest")
    except NotFoundError:
        version = versions[0]
        print(f"no published version — using newest v{version.version_number}")

    print(f"\nVersion v{version.version_number}:")
    print(f"  document_name_template: {version.document_name_template}")
    print(f"  sample_data keys:       {list(version.sample_data.keys())}")
    print(f"  translations:           {'present' if version.translations else 'none'}")
    return version


async def update_document_name(client: PagrApiClient, template, version):
    """The document-name template controls generated file names, e.g.
    "{{Title}}-{{today}}". A locked/published version may reject the change."""
    original = version.document_name_template
    try:
        updated = await client.update_document_name_template(
            template.id, version.version_number, "{{Title}}-{{today}}"
        )
        print(f"\nUpdated document_name_template → {updated.document_name_template}")
        await client.update_document_name_template(
            template.id, version.version_number, original
        )
        print(f"Restored to → {original}")
    except PagrError as exc:
        print(f"\ndocument_name_template not updatable for this version: {exc}")


async def preview_image(client: PagrApiClient, template, version):
    """Each published version can expose a preview image of its first page."""
    try:
        url = await client.get_preview_image_url(template.id, version.version_number)
        print(f"\nPreview image: {url or '(none)'}")
    except NotFoundError:
        print("\nPreview image: (none for this version)")


async def main():
    async with PagrApiClient(api_key=API_KEY) as client:
        page = await list_templates(client)
        if len(page) == 0:
            print("No templates in this organisation — create one first.")
            return

        template = await client.get_template(page[0].id)

        await search_and_filter(client, template)
        version = await explore_versions(client, template)
        await update_document_name(client, template, version)
        await preview_image(client, template, version)


if __name__ == "__main__":
    asyncio.run(main())
