"""Log Analytics workspaces that grow without limit.

A Log Analytics workspace bills on data ingested -- about $2.30 a GB past the
first five a month -- and again on retention past the included period. Neither
charge has a ceiling unless one is set, and Azure sets none by default: a
workspace with a daily cap of -1 will accept whatever is sent to it, and a
diagnostic setting pointed at the wrong resource can put a terabyte a month
through it before anyone notices.

Unlike everything else in this catalog, this is not a resource sitting idle.
It is a resource with no limit on it, and the cost it will have is not the
cost it has. So it is reported as growth with no dollar figure attached
rather than priced at a number that would be invented.

A workspace is reported when it has **no daily ingestion cap** and retention
beyond the free period. One or the other alone is a deliberate choice; both
together is the shape that produces a surprise invoice.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unbounded-log-workspace"

RESOURCE_TYPE = "Microsoft.OperationalInsights/workspaces"

# Azure spells "no cap" as -1 in the workspace's capping block.
NO_DAILY_CAP = -1.0

# Every workspace includes 31 days of retention at no extra charge; past that
# it bills per GB-month.
FREE_RETENTION_DAYS = 31


def _daily_cap_gb(properties: dict[str, Any]) -> float:
    capping = properties.get("workspaceCapping") or {}
    try:
        return float(capping.get("dailyQuotaGb", NO_DAILY_CAP))
    except (TypeError, ValueError):
        return NO_DAILY_CAP


def build_finding(ctx: ScanContext, workspace: dict[str, Any]) -> Finding:
    name = workspace["name"]
    arm_id = workspace.get("id") or ""
    group = workspace.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(workspace)
    properties = helpers.properties(workspace)

    retention = int(properties.get("retentionInDays") or 0)
    ingestion, _ = ctx.pricing.rate("log.ingestion_gb")
    retention_rate, _ = ctx.pricing.rate("log.retention_gb_month")
    billable_days = max(retention - FREE_RETENTION_DAYS, 0)

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="log-analytics-workspace",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"Workspace has no daily ingestion cap and keeps data for {retention} days, "
            f"so both what it accepts and what it stores grow without a ceiling"
        ),
        # The list call does not report how much has been ingested, and asking
        # would mean querying the workspace. Reporting unbounded growth is
        # honest; inventing a volume would not be.
        monthly_cost=0.0,
        remediation=helpers.az(
            f"az monitor log-analytics workspace update --name {helpers.arg(name)} "
            "--set workspaceCapping.dailyQuotaGb=5",
            ctx.subscription,
            group,
        ),
        details={
            "retention_days": retention,
            "billable_retention_days": billable_days,
            "daily_quota_gb": None,
            "sku": ((properties.get("sku") or {}).get("name")),
            "tags": workspace.get("tags") or {},
            "usd_per_gb_ingested": ingestion,
            "usd_per_gb_month_retained": retention_rate,
            "note": (
                "unpriced: the workspace listing does not report ingested volume, and "
                "measuring it means querying the workspace. Reported as unbounded "
                f"growth at ${ingestion:,.2f}/GB ingested and "
                f"${retention_rate:,.2f}/GB-month retained past {FREE_RETENTION_DAYS} days"
            ),
            "suggested_daily_cap_gb": 5,
        },
    )


@check(
    CHECK_NAME,
    "Log workspaces with no ingestion cap",
    providers="Microsoft.OperationalInsights",
    uncleanable=(
        "the right daily cap depends on how much this workspace is expected to "
        "ingest, which nothing in the API says, and a cap set too low silently "
        "drops the logs an incident would be investigated from. zombiescan reports "
        "a suggested cap in the finding's details and leaves setting it to you"
    ),
)
def unbounded_log_workspace(ctx: ScanContext) -> Iterator[Finding]:
    for workspace in ctx.list(RESOURCE_TYPE):
        properties = helpers.properties(workspace)
        if _daily_cap_gb(properties) != NO_DAILY_CAP:
            continue
        if int(properties.get("retentionInDays") or 0) <= FREE_RETENTION_DAYS:
            continue
        yield build_finding(ctx, workspace)
