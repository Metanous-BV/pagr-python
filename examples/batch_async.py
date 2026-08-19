"""Async (fire-and-forget) batch render, tracked by polling the job status.

`enqueue_batch_render` returns immediately and the server renders in the
background, reporting progress two ways: webhook callbacks POSTed to the
`callback_url` you register, or polling `get_job_status` / `wait_for_job`.

This example polls, because it needs no publicly reachable endpoint. For the
webhook route, host your own HTTPS endpoint and pass each raw body to
`pagr.parse_signed_callback()`, which verifies the `X-Pagr-Signature` HMAC and
decodes the payload in one step — the SDK is framework-agnostic and deliberately
ships no server of its own.

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

# Where the server POSTs the progress + completion callbacks. This example never
# reads them (it polls instead), but the parameter is required — point it at your
# own endpoint and verify the X-Pagr-Signature header on each delivery (see
# pagr.verify_signature / pagr.parse_signed_callback).
CALLBACK_URL = os.getenv("PAGR_CALLBACK_URL", "https://your-app.example/pagr/callback")


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

        doc = copy.deepcopy(version.sample_data)
        doc["Title"] = "Async render"
        docs = [doc] * 100

        job = await client.enqueue_batch_render(
            template.id, docs, CALLBACK_URL, version=version.version_number
        )
        print(f"Job {job.job_id} queued — {job.requested_count} document(s).")

        # Poll by hand to show progress as the job runs. `status.done` is True
        # once the state is terminal (completed or failed).
        while True:
            status = await client.get_job_status(job.job_id)
            print(
                f"  state={status.state}  rendered={status.rendered_count}"
                f"/{status.requested_count}"
            )
            if status.done:
                break
            await asyncio.sleep(2)

        print(
            f"Job {status.job_id} finished: state={status.state} status={status.status}, "
            f"{status.rendered_count}/{status.requested_count} rendered."
        )
        if status.failure_reason:
            print(f"  failure_reason: {status.failure_reason}")
        for issue in status.issues:
            print(f"  issue: [{issue.severity}] {issue.description}")

        # Or let the SDK run the poll loop for you — a 5-minute deadline by
        # default, so a stuck job can never hang the await forever:
        #   status = await client.wait_for_job(job.job_id, poll_interval=2)


if __name__ == "__main__":
    asyncio.run(main())
