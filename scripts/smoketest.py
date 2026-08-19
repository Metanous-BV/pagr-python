"""Comprehensive SDK walkthrough: every client method, model, enum, and exception.

Maintainer-only live-API walkthrough, not a pytest test (pytest does not
collect anything under scripts/). It hits a real Pagr API instance and needs a
real API key: run with the [examples] extra installed and a .env file
providing TEST_KEY_PUBLIC / PROD_KEY_PUBLIC.

Sections
--------
1.  Service health
2.  Organisation statistics
3.  Fonts
4.  Templates (list, get, paged)
4b. Listing options: search, project scoping, filters
5.  Template versions (list, get, update, preview)
6.  Single render: inline data, JSON-string data, language, persist=False, render_pdf
7.  Batch render: succeeded/failed, save_all, save individual
8.  Async (fire-and-forget) render: enqueue + parse_callback
9.  Job status polling: get_job_status, wait_for_job
10. Validation: is_valid, errors, warnings, issues_for
11. Document listing and download
12. Error handling: every exception type
13. API key rotation
"""

import asyncio
import copy
import json
import os
from typing import Optional
from uuid import UUID

from dotenv import load_dotenv

from pagr import (
    PagrApiClient,
    DEFAULT_BASE_URL,
    # models
    BatchItem,
    BatchRenderResult,
    OrgStats,
    PagedResult,
    RenderCompletion,
    RenderDocument,
    RenderIssueSeverity,
    RenderIssueType,
    RenderJob,
    RenderJobStatus,
    RenderProgress,
    RenderResult,
    RenderedDocument,
    Template,
    TemplateVersion,
    ValidationResponse,
    # utilities
    parse_callback,
    # exceptions
    ApiError,
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
    PagrError,
    PayloadTooLargeError,
    RateLimitError,
    ValidationFailedError,
)

load_dotenv()
BASE_URL = os.getenv("PAGR_BASE_URL", DEFAULT_BASE_URL)
API_KEY = os.getenv("TEST_KEY_PUBLIC", "")


# ── helpers ────────────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── 1. Service health ──────────────────────────────────────────────────────────

async def demo_health(client: PagrApiClient) -> None:
    section("1. Service health")

    healthy: bool = await client.get_status()
    print(f"  API healthy: {healthy}")

    version: str | None = await client.get_version()
    print(f"  API version: {version}")


# ── 2. Organisation statistics ─────────────────────────────────────────────────

async def demo_org_stats(client: PagrApiClient) -> None:
    section("2. Organisation statistics")

    stats: OrgStats = await client.get_org_stats()
    print(stats)                                          # __str__

    # Every OrgStats property:
    print(f"  organisation_name:          {stats.organisation_name}")
    print(f"  tier:                       {stats.tier}")
    print(f"  period_start / end:         {stats.period_start} – {stats.period_end}")
    print(f"  included_renders_per_month: {stats.included_renders_per_month}")
    print(f"  pages_used_this_period:     {stats.pages_used_this_period}")
    print(f"  pages_available:            {stats.pages_available}")
    print(f"  included_tokens_per_month:  {stats.included_tokens_per_month}")
    print(f"  tokens_used_this_period:    {stats.tokens_used_this_period}")
    print(f"  tokens_available:           {stats.tokens_available}")
    print(f"  user_count:                 {stats.user_count}")


# ── 3. Fonts ───────────────────────────────────────────────────────────────────

async def demo_fonts(client: PagrApiClient) -> None:
    section("3. Fonts")

    fonts: list[str] = await client.get_fonts()
    print(f"  {len(fonts)} font families available")
    for f in fonts[:5]:
        print(f"    • {f}")
    if len(fonts) > 5:
        print(f"    … and {len(fonts) - 5} more")


# ── 4. Templates ───────────────────────────────────────────────────────────────

async def demo_templates(client: PagrApiClient) -> PagedResult[Template]:
    section("4. Templates")

    # Paged listing with every query parameter:
    page: PagedResult[Template] = await client.get_templates(
        skip=0,
        take=10,
        sort_by="name",
        sort_direction="asc",
        search=None,
        filters=None,
    )

    # PagedResult properties:
    print(f"  total={page.total}  skip={page.skip}  take={page.take}  has_more={page.has_more}")
    print(f"  items on this page: {len(page)}")

    if len(page) == 0:
        print("  No templates found – skipping remaining demos.")
        return page

    # Iteration:
    for t in page:
        print(f"    • {t}")           # __str__: "{name} ({id})"

    # Index access:
    template: Template = page[0]

    # Every Template property:
    print(f"\n  Template detail for: {template.name}")
    print(f"    id:                    {template.id}")
    print(f"    document_name_template:{template.document_name_template}")
    print(f"    project_id:            {template.project_id}")
    print(f"    project_name:          {template.project_name}")
    print(f"    latest_version_number: {template.latest_version_number}")
    print(f"    version_count:         {template.version_count}")
    print(f"    updated_at / by:       {template.updated_at} / {template.updated_by}")
    print(f"    master_template_id:    {template.master_template_id}")
    print(f"    master_template_name:  {template.master_template_name}")

    # Fetch a single template by ID:
    fetched: Template = await client.get_template(template.id)
    print(f"\n  get_template({template.id}) → {fetched.name}")

    return page


# ── 4b. Listing options: search, project scoping, filters ──────────────────────

async def demo_listing_options(client: PagrApiClient, template: Template) -> None:
    """Demonstrate the query options every ``ListQuery``-bound endpoint accepts:
    free-text ``search``, resource scoping (``project_id``), and structured
    ``filters``. Each is wrapped defensively so an unsupported field on a given
    deployment degrades to a printed note rather than aborting the walkthrough."""
    section("4b. Listing options (search, project scoping, filters)")

    # Free-text search — matches the template we already have by (a prefix of) its name.
    term = template.name[:3]
    found: PagedResult[Template] = await client.get_templates(search=term, take=5)
    print(f"  search={term!r} → {found.total} match(es)")

    # Project scoping — list only the templates inside this template's project.
    if template.project_id is not None:
        in_project: PagedResult[Template] = await client.get_templates(
            project_id=template.project_id, take=5
        )
        print(
            f"  project_id={template.project_id} "
            f"({template.project_name}) → {in_project.total} template(s)"
        )
    else:
        print("  project_id: template has no project – skipping project scoping")

    # Structured filters — {field, op, value}; expanded to filters[0].field=… etc.
    # Wrapped in try/except because valid filter fields are deployment-defined.
    try:
        filtered: PagedResult[Template] = await client.get_templates(
            filters=[{"field": "name", "op": "eq", "value": template.name}],
            take=5,
        )
        print(f"  filters=[name eq {template.name!r}] → {filtered.total} match(es)")
    except PagrError as exc:
        print(f"  filters: not supported for this field/deployment ({exc})")


# ── 5. Template versions ───────────────────────────────────────────────────────

async def demo_versions(
    client: PagrApiClient, template: Template
) -> TemplateVersion | None:
    section("5. Template versions")

    # Paged version listing:
    versions: PagedResult[TemplateVersion] = await client.get_template_versions(
        template.id,
        skip=0,
        take=5,
        sort_by="versionNumber",
        sort_direction="desc",
    )
    print(f"  {versions.total} version(s) total, fetched {len(versions)}")

    if len(versions) == 0:
        return None

    # The list includes drafts (sorted newest-first above); the "latest" alias
    # resolves only to the latest *published* version and 404s when none is
    # published yet. Fall back to the newest version from the list in that case.
    newest: TemplateVersion = versions[0]
    try:
        latest: TemplateVersion = await client.get_template_version(template.id, "latest")
        print(f"  latest published version: {latest.version_number}")
    except NotFoundError:
        latest = newest
        print(f"  no published version – using newest v{latest.version_number} from the list")

    # Fetch a specific version by its number (works for drafts too):
    by_number: TemplateVersion = await client.get_template_version(
        template.id, newest.version_number
    )
    print(f"  fetched by number: v{by_number.version_number}")
    print(latest)           # __str__: multi-line info

    # Every TemplateVersion property:
    print(f"\n  TemplateVersion properties:")
    print(f"    id:                    {latest.id}")
    print(f"    version_number:        {latest.version_number}")
    print(f"    template_id:           {latest.template_id}")
    print(f"    document_name_template:{latest.document_name_template}")
    print(f"    published_at / by:     {latest.published_at} / {latest.published_by}")
    print(f"    updated_at:            {latest.updated_at}")
    print(f"    sample_data keys:      {list(latest.sample_data.keys())[:5]}")
    print(f"    template_json (100c):  {latest.template_json[:100]}…")
    print(f"    translations:          {'present' if latest.translations else 'none'}")

    # Update the document-name template (then restore it). This mutates server
    # state, so it is guarded: a locked/published version may reject the change.
    original_name_tpl = latest.document_name_template
    try:
        updated: TemplateVersion = await client.update_document_name_template(
            template.id, latest.version_number, "{{Title}}-{{today}}"
        )
        print(f"\n  Updated document_name_template → {updated.document_name_template}")
        # Restore:
        await client.update_document_name_template(
            template.id, latest.version_number, original_name_tpl
        )
        print(f"  Restored document_name_template → {original_name_tpl}")
    except PagrError as exc:
        print(f"\n  document_name_template not updatable for this version ({exc})")

    # Preview image URL (404s when the version has no preview image yet):
    try:
        preview_url: str | None = await client.get_preview_image_url(
            template.id, latest.version_number
        )
        print(f"\n  Preview image URL: {preview_url or '(none)'}")
    except NotFoundError:
        print(f"\n  Preview image URL: (none – no preview for this version)")

    return latest


# ── 6. Single render ───────────────────────────────────────────────────────────

async def demo_single_render(
    client: PagrApiClient, template: Template, version: TemplateVersion
) -> RenderResult:
    section("6. Single render")

    doc = copy.deepcopy(version.sample_data)
    doc["Title"] = "Alice"

    out_dir = os.path.join(os.path.dirname(__file__), "test_output")
    os.makedirs(out_dir, exist_ok=True)

    # Standard render with inline document:
    result: RenderResult = await client.render(
        template.id, doc, version=version.version_number, include_document=True
    )

    # RenderResult properties:
    print(f"  ok={result.ok}  status={result.status}")
    print(f"  rendered_count={result.rendered_count}  requested_count={result.requested_count}")
    print(f"  missing_count={result.missing_count}")
    print(f"  insufficient_credit={result.insufficient_credit}")
    print(f"  message={result.message}")
    print(f"  issues count: {len(result.issues)}")
    print(result)           # __str__

    if result.ok:
        doc_obj: RenderedDocument = result.document

        # Every RenderedDocument property:
        print(f"\n  RenderedDocument properties:")
        print(f"    id:               {doc_obj.id}")
        print(f"    document_name:    {doc_obj.document_name}")
        print(f"    template_id:      {doc_obj.template_id}")
        print(f"    version_number:   {doc_obj.version_number}")
        print(f"    environment:      {doc_obj.environment}")
        print(f"    file_size_bytes:  {doc_obj.file_size_bytes}")
        print(f"    page_count:       {doc_obj.page_count}")
        print(f"    rendered_at:      {doc_obj.rendered_at}")
        print(f"    render_duration:  {doc_obj.render_duration} ms")
        print(f"    view_url:         {doc_obj.view_url}")
        print(f"    document_type:    {doc_obj.document_type}")
        has_inline = doc_obj.document_base64 is not None
        print(f"    document_base64:  {'present' if has_inline else 'absent'}")
        print(doc_obj)      # __str__

        # to_bytes() and save():
        if has_inline:
            pdf_bytes: bytes = doc_obj.to_bytes()
            print(f"\n  to_bytes() → {len(pdf_bytes)} bytes")

            saved_path: str = doc_obj.save(out_dir)
            print(f"  Saved to: {saved_path}")

    # Render issues – RenderIssue properties and enums:
    for issue in result.issues:
        print(f"\n  Issue: {issue}")           # __str__
        print(f"    type:           {issue.type} (RenderIssueType.{issue.type.name})")
        print(f"    severity:       {issue.severity} (RenderIssueSeverity.{issue.severity.name})")
        print(f"    description:    {issue.description}")
        print(f"    element_id:     {issue.element_id}")
        print(f"    document_index: {issue.document_index}")
        print(f"    is_error:       {issue.is_error}")

    # Render with language override:
    result_lang: RenderResult = await client.render(
        template.id, doc, version=version.version_number,
        include_document=False, language="nl-BE"
    )
    print(f"\n  Render with language='nl-BE' → ok={result_lang.ok}")

    # Render without persistence (persist=False) → JSON envelope with id/view_url
    # null and the base64 forced on (no raw-PDF sniffing):
    result_no_persist: RenderResult = await client.render(
        template.id, doc, version=version.version_number,
        include_document=True, persist=False
    )
    if result_no_persist.ok:
        raw_pdf: bytes = result_no_persist.document.to_bytes()
        print(f"\n  persist=False render → {len(raw_pdf)} bytes, "
              f"id={result_no_persist.document.id}  "
              f"view_url={result_no_persist.document.view_url}")

    # render_pdf: opt-in raw-PDF stream with metadata in response headers.
    pdf_result = await client.render_pdf(
        template.id, doc, version=version.version_number
    )
    if pdf_result.ok:
        pdoc = pdf_result.document
        print(f"\n  render_pdf → {len(pdoc.to_bytes())} bytes, "
              f"name={pdoc.document_name!r}  pages={pdoc.page_count}  "
              f"document_id={pdoc.document_id}")
    else:
        print(f"\n  render_pdf → blocked: status={pdf_result.status}  "
              f"issues={len(pdf_result.issues)}")

    # Data may also be supplied as a JSON *string* (not just a dict); the client
    # normalises both. Handy when the data already arrives serialised (queue,
    # request body, file) and you don't want to round-trip it through a dict.
    json_str: str = json.dumps(doc)
    result_from_str: RenderResult = await client.render(
        template.id, json_str, version=version.version_number, include_document=False
    )
    print(f"\n  render(json_str) → ok={result_from_str.ok}")

    return result


# ── 7. Batch render ────────────────────────────────────────────────────────────

async def demo_batch_render(
    client: PagrApiClient, template: Template, version: TemplateVersion
) -> None:
    section("7. Batch render")

    base_doc = copy.deepcopy(version.sample_data)
    docs = []
    for i in range(1, 6):
        d = copy.deepcopy(base_doc)
        d["Title"] = f"Document {i}"
        docs.append(d)

    batch: BatchRenderResult = await client.render_batch(
        template.id, docs,
        version=version.version_number,
        include_document=True,
    )

    # BatchRenderResult properties:
    print(f"  ok={batch.ok}  status={batch.status}")
    print(f"  requested={batch.requested_count}  rendered={batch.rendered_count}  missing={batch.missing_count}")
    print(f"  insufficient_credit={batch.insufficient_credit}")
    print(f"  message={batch.message}")
    print(f"  len(batch)={len(batch)}")

    # Iteration:
    for item in batch:
        print(f"    [{item.index}] ok={item.ok}", end="")
        if item.ok:
            print(f"  name={item.document.document_name}", end="")
        print()
        print(f"  BatchItem: {item}")     # __str__

    # Index access:
    first_item: BatchItem = batch[0]
    print(f"\n  batch[0].index={first_item.index}  batch[0].input={first_item.input}")

    # succeeded / failed / documents slices:
    succeeded: list[BatchItem] = batch.succeeded
    failed: list[BatchItem] = batch.failed
    documents: list[RenderedDocument] = batch.documents
    print(f"\n  succeeded={len(succeeded)}  failed={len(failed)}  documents={len(documents)}")

    for fi in failed:
        reasons = [iss.description for iss in fi.issues]
        print(f"  FAILED [{fi.index}] reasons={reasons}")

    # save_all to a directory:
    out_dir = os.path.join(os.path.dirname(__file__), "test_output")
    os.makedirs(out_dir, exist_ok=True)
    saved_paths: list[str] = batch.save_all(out_dir)
    print(f"\n  save_all → {len(saved_paths)} file(s) written")
    for p in saved_paths:
        print(f"    {p}")


# ── 8. Async render: enqueue + callback parsing ─────────────────────────────────

async def demo_async_render(
    client: PagrApiClient, template: Template, version: TemplateVersion
) -> Optional[UUID]:
    section("8. Async (fire-and-forget) render: enqueue + parse_callback")

    base_doc = copy.deepcopy(version.sample_data)
    docs = [
        {**copy.deepcopy(base_doc), "Title": f"Async {i}"}
        for i in range(1, 4)
    ]

    # The server POSTs its progress/completion callbacks here. The SDK ships no
    # webhook server, so this walkthrough registers a URL it never reads and
    # tracks the job by polling instead (section 9).
    callback_url = os.getenv("PAGR_CALLBACK_URL", "https://example.test/pagr/callback")
    print(f"  callback_url: {callback_url}")

    job: RenderJob = await client.enqueue_batch_render(
        template.id, docs,
        callback_url=callback_url,
        version=version.version_number,
        include_document=True,
    )

    # RenderJob properties:
    print(f"\n  RenderJob:")
    print(f"    job_id:          {job.job_id}")
    print(f"    requested_count: {job.requested_count}")
    print(f"    state:           {job.state}")
    print(f"  {job}")     # __str__

    # parse_callback turns a raw webhook body into the right typed object. These
    # are the two shapes your own endpoint will receive.
    progress_payload = {
        "jobId": str(job.job_id),
        "processed": 1,
        "requestedCount": 3,
        "documentIndex": 0,
        "document": {
            "id": "00000000-0000-0000-0000-000000000001",
            "documentName": "Example",
            "templateId": str(template.id),
            "versionNumber": version.version_number,
            "environment": "test",
            "fileSizeBytes": 12345,
            "pageCount": 1,
            "renderedAt": "2024-01-01T00:00:00Z",
            "renderDuration": 250.0,
            "viewUrl": "https://example.com/doc/1",
            "documentType": "Template",
        },
    }
    completion_payload = {
        "jobId": str(job.job_id),
        "state": "completed",
        "status": "ok",
        "renderedCount": 3,
        "requestedCount": 3,
        "missingCount": 0,
        "issues": [],
        "message": None,
    }

    progress = parse_callback(progress_payload)
    assert isinstance(progress, RenderProgress)
    print(f"\n  parse_callback → {type(progress).__name__}")
    # RenderProgress properties:
    print(
        f"    Progress: {progress.processed}/{progress.requested_count}"
        f"  ({progress.progress_pct:.0f}%)"
        f"  job_id={progress.job_id}"
        f"  idx={progress.document_index}"
        f"  doc={progress.document.document_name}"
    )

    completion = parse_callback(completion_payload)
    assert isinstance(completion, RenderCompletion)
    print(f"\n  parse_callback → {type(completion).__name__}")
    # RenderCompletion properties:
    print(f"    job_id:              {completion.job_id}")
    print(f"    state:               {completion.state}")
    print(f"    status:              {completion.status}")
    print(f"    ok:                  {completion.ok}")
    print(f"    insufficient_credit: {completion.insufficient_credit}")
    print(f"    rendered_count:      {completion.rendered_count}")
    print(f"    requested_count:     {completion.requested_count}")
    print(f"    missing_count:       {completion.missing_count}")
    print(f"    issues:              {len(completion.issues)}")
    print(f"    message:             {completion.message}")

    # Hand the job id back so the polling demo (section 9) has something to poll.
    return job.job_id


# ── 9. Job status polling ──────────────────────────────────────────────────────

async def demo_job_polling(client: PagrApiClient, job_id) -> None:
    section("9. Job status polling")

    # A single point-in-time poll — the job from section 8 is likely still
    # running, since nothing waited on it.
    status: RenderJobStatus = await client.get_job_status(job_id)
    print(f"  first poll: state={status.state}  done={status.done}\n")

    # wait_for_job runs the poll loop for you, up to an overall deadline
    # (WAIT_FOR_JOB_DEFAULT_TIMEOUT — 5 minutes — when timeout is omitted).
    status = await client.wait_for_job(job_id, poll_interval=2)

    # RenderJobStatus properties:
    print(f"  job_id:        {status.job_id}")
    print(f"  state:         {status.state}")
    print(f"  status:        {status.status}")
    print(f"  ok:            {status.ok}")
    print(f"  done:          {status.done}")
    print(f"  rendered_count:{status.rendered_count}")
    print(f"  requested_count:{status.requested_count}")
    print(f"  missing_count: {status.missing_count}")
    print(f"  issues:        {len(status.issues)}")
    print(f"  started_at:    {status.started_at}")
    print(f"  completed_at:  {status.completed_at}")
    print(f"  failure_reason:{status.failure_reason}")
    print(f"  {status}")     # __str__


# ── 10. Validation ─────────────────────────────────────────────────────────────

async def demo_validation(
    client: PagrApiClient, template: Template, version: TemplateVersion
) -> None:
    section("10. Validation")

    doc = copy.deepcopy(version.sample_data)
    bad_doc = {"completely": "wrong"}   # intentionally invalid

    # Validate single document:
    validation: ValidationResponse = await client.validate(
        template.id, doc, version=version.version_number
    )
    print(f"  Single doc: is_valid={validation.is_valid}  issues={len(validation)}")
    print(validation)       # __str__

    # Validate a batch (list input):
    batch_validation: ValidationResponse = await client.validate(
        template.id, [doc, bad_doc], version=version.version_number
    )
    print(f"\n  Batch (2 docs): is_valid={batch_validation.is_valid}  issues={len(batch_validation)}")

    # Iteration over issues:
    for issue in batch_validation:
        print(f"    {issue}")

    # Index access:
    if len(batch_validation) > 0:
        first = batch_validation[0]
        print(f"\n  batch_validation[0]: {first}")

    # errors / warnings slices:
    errors = batch_validation.errors
    warnings = batch_validation.warnings
    print(f"\n  errors={len(errors)}  warnings={len(warnings)}")

    # issues_for(document_index):
    issues_doc1 = batch_validation.issues_for(1)
    print(f"  issues for doc index 1: {len(issues_doc1)}")
    for iss in issues_doc1:
        print(f"    [{iss.severity.value}] {iss.type.value}: {iss.description}")

    # Enum coverage – all RenderIssueSeverity values:
    for sev in RenderIssueSeverity:
        print(f"  severity: {sev.name} = {sev.value!r}")

    # Enum coverage – spot-check RenderIssueType values:
    interesting_types = [
        RenderIssueType.MISSING_BINDING,
        RenderIssueType.UNRESOLVED_IMAGE,
        RenderIssueType.RENDER_TIMEOUT,
        RenderIssueType.UNKNOWN,
    ]
    for rt in interesting_types:
        print(f"  issue type: {rt.name} = {rt.value!r}")


# ── 11. Documents ──────────────────────────────────────────────────────────────

async def demo_documents(client: PagrApiClient) -> None:
    section("11. Document listing and download")

    page: PagedResult[RenderDocument] = await client.get_documents(
        skip=0,
        take=5,
        sort_by="renderedAt",
        sort_direction="desc",
    )
    print(f"  total={page.total}  has_more={page.has_more}  fetched={len(page)}")

    if len(page) == 0:
        print("  No persisted documents found.")
        return

    # Fetch a single RenderDocument:
    rd: RenderDocument = await client.get_document(page[0].id)

    # Every RenderDocument property:
    print(f"\n  RenderDocument properties:")
    print(f"    id:                    {rd.id}")
    print(f"    document_name:         {rd.document_name}")
    print(f"    template_id:           {rd.template_id}")
    print(f"    version_number:        {rd.version_number}")
    print(f"    environment:           {rd.environment}")
    print(f"    file_size_bytes:       {rd.file_size_bytes}")
    print(f"    page_count:            {rd.page_count}")
    print(f"    rendered_at:           {rd.rendered_at}")
    print(f"    render_duration:       {rd.render_duration} ms")
    print(f"    view_url:              {rd.view_url}")
    print(f"    document_type:         {rd.document_type}")
    print(f"    language:              {rd.language}")
    print(f"    is_pdf_deleted:        {rd.is_pdf_deleted}")
    has_inline = rd.document_base64 is not None
    print(f"    document_base64:       {'present' if has_inline else 'absent'}")
    print(rd)   # __str__

    # Download PDF bytes:
    if not rd.is_pdf_deleted:
        pdf: bytes = await client.download_document(rd.id)
        print(f"\n  download_document → {len(pdf)} bytes")
        out_dir = os.path.join(os.path.dirname(__file__), "test_output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{rd.document_name}_downloaded.pdf")
        with open(out_path, "wb") as fh:
            fh.write(pdf)
        print(f"  Saved downloaded PDF to: {out_path}")


# ── 12. Error handling ─────────────────────────────────────────────────────────

async def demo_error_handling(client: PagrApiClient) -> None:
    section("12. Error handling")

    nonexistent = UUID("00000000-0000-0000-0000-000000000000")

    # 404 NotFoundError:
    try:
        await client.get_template(nonexistent)
    except NotFoundError as e:
        print(f"  NotFoundError: status={e.status_code}  code={e.code}")

    # 401 AuthenticationError (bad key):
    bad_client = PagrApiClient(api_key="bad-key", base_url=BASE_URL)
    try:
        async with bad_client:
            await bad_client.get_org_stats()
    except AuthenticationError as e:
        print(f"  AuthenticationError: status={e.status_code}  code={e.code}")

    # Catch-all base class:
    try:
        await client.get_template(nonexistent)
    except PagrError as e:
        print(f"  PagrError base: status={e.status_code}  code={e.code}")

    # Exception hierarchy for completeness (not triggered here, but shown):
    hierarchy = [
        AuthenticationError,
        ForbiddenError,
        NotFoundError,
        PayloadTooLargeError,
        ValidationFailedError,
        RateLimitError,
        ApiError,
    ]
    print("\n  Exception hierarchy (all inherit PagrError):")
    for exc in hierarchy:
        print(f"    {exc.__name__}")


# ── 13. API key rotation ───────────────────────────────────────────────────────

async def demo_key_rotation(client: PagrApiClient) -> None:
    section("13. API key rotation")

    original_key = API_KEY
    client.set_api_key("temporary-key")
    print("  set_api_key('temporary-key') – subsequent requests use new key")
    client.set_api_key(original_key)
    print("  Restored original key")


# ── main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    async with PagrApiClient(api_key=API_KEY, base_url=BASE_URL) as client:

        await demo_health(client)
        await demo_org_stats(client)
        await demo_fonts(client)

        templates_page = await demo_templates(client)
        if len(templates_page) == 0:
            return

        template = templates_page[0]
        await demo_listing_options(client, template)

        version = await demo_versions(client, template)
        if version is None:
            return

        await demo_single_render(client, template, version)
        await demo_batch_render(client, template, version)

        # Async render returns the job_id, which drives the polling demo:
        job_id: Optional[UUID] = None
        try:
            job_id = await demo_async_render(client, template, version)
        except Exception as exc:
            print(f"  (async render skipped – no loopback available: {exc})")

        if job_id:
            await demo_job_polling(client, job_id)

        await demo_validation(client, template, version)
        await demo_documents(client)
        await demo_error_handling(client)
        await demo_key_rotation(client)


if __name__ == "__main__":
    asyncio.run(main())
