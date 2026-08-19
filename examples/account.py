"""Account-level features: organisation usage stats, available fonts, key rotation.

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
        # Usage and quota for the organisation the API key belongs to —
        # useful to check remaining credit before queueing a large batch:
        stats = await client.get_org_stats()
        print(f"{stats.organisation_name} ({stats.tier} tier)")
        print(f"  period:  {stats.period_start} – {stats.period_end}")
        print(f"  pages:   {stats.pages_used_this_period} used, "
              f"{stats.pages_available} available "
              f"(included per month: {stats.included_renders_per_month})")
        print(f"  tokens:  {stats.tokens_used_this_period} used, "
              f"{stats.tokens_available} available")
        print(f"  users:   {stats.user_count}")

        # Font families available to templates:
        fonts = await client.get_fonts()
        print(f"\n{len(fonts)} font families available, e.g.: {', '.join(fonts[:5])}")

        # Swap the API key on a live client — no reconnect needed. Useful when
        # keys are rotated or when one client serves multiple environments:
        client.set_api_key(API_KEY)
        print("\nset_api_key() — subsequent requests use the new key")


if __name__ == "__main__":
    asyncio.run(main())
