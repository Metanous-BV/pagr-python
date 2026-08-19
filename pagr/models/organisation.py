from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ._common import parse_dt


@dataclass
class OrgStats:
    """Usage and credit statistics for the authenticated organisation,
    covering the current billing period (``period_start``–``period_end``).

    "Pages" is the render-credit unit (rendered document pages); "tokens" are
    AI tokens consumed by AI-assisted template features. Both have an
    included monthly allowance (``included_renders_per_month`` /
    ``included_tokens_per_month``), an amount used this period
    (``pages_used_this_period`` / ``tokens_used_this_period``), and an amount
    remaining (``pages_available`` / ``tokens_available``). A value of
    ``-1`` in ``pages_available``, ``included_tokens_per_month`` or
    ``tokens_available`` means unlimited for the organisation's ``tier``.
    """

    organisation_name: Optional[str]
    period_start: Optional[datetime]
    period_end: Optional[datetime]
    tier: Optional[str]
    # Usage/count fields default to ``None`` (field absent) rather than ``0`` so
    # a server that omits a field is distinguishable from a genuine zero.
    included_renders_per_month: Optional[int]
    pages_used_this_period: Optional[int]
    pages_available: Optional[int]
    included_tokens_per_month: Optional[int]
    tokens_used_this_period: Optional[int]
    tokens_available: Optional[int]
    user_count: Optional[int]

    @classmethod
    def from_api(cls, data: dict) -> "OrgStats":
        return cls(
            organisation_name=data.get("organisationName"),
            period_start=parse_dt(data.get("periodStart")),
            period_end=parse_dt(data.get("periodEnd")),
            tier=data.get("tier"),
            included_renders_per_month=data.get("includedRendersPerMonth"),
            pages_used_this_period=data.get("pagesUsedThisPeriod"),
            pages_available=data.get("pagesAvailable"),
            included_tokens_per_month=data.get("includedTokensPerMonth"),
            tokens_used_this_period=data.get("tokensUsedThisPeriod"),
            tokens_available=data.get("tokensAvailable"),
            user_count=data.get("userCount"),
        )

    def __str__(self):
        period = (
            f"{self.period_start.date()} → {self.period_end.date()}"
            if self.period_start and self.period_end
            else "—"
        )
        name = self.organisation_name or "?"
        return (
            f"OrgStats | {name} ({self.tier})\n"
            f"  Period:  {period}\n"
            f"  Pages:   {self.pages_used_this_period} used / "
            f"{self.included_renders_per_month} included / {self.pages_available} remaining\n"
            f"  Tokens:  {self.tokens_used_this_period} used / "
            f"{self.included_tokens_per_month} included / {self.tokens_available} remaining\n"
            f"  Users:   {self.user_count}"
        )
