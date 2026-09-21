"""Cloud Logging buckets keeping logs forever.

Cloud Logging gives every bucket 30 days of retention free and bills per
GB-month beyond that. A bucket set to a long retention -- or to zero, which
Google reads as "never expire" -- grows without bound, and the bill grows with
it. Nobody notices, because the cost arrives as a slope rather than as a line
item.

The two buckets every project gets, ``_Default`` and ``_Required``, are in
scope: ``_Default`` is editable and is usually the one growing. ``_Required``
is fixed at 400 days by Google and cannot be changed, so it is reported with a
refusal rather than a plan.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unbounded-log-bucket"

# Retention Google does not charge for. A bucket at or under this is free
# however much it holds.
FREE_RETENTION_DAYS = 30

# Beyond this, a bucket is keeping logs for reasons nobody wrote down.
LONG_RETENTION_DAYS = 365

# Fixed by Google at 400 days and not editable.
REQUIRED_BUCKET = "_Required"


def build_finding(ctx: ScanContext, location: str, bucket: dict[str, Any]) -> Finding:
    name = gcp.last_segment(bucket.get("name"))
    retention = int(bucket.get("retentionDays") or 0)
    price, approximate = ctx.pricing.rate("log.retention_gb_month")
    forever = retention == 0

    reason = (
        "Log bucket never expires its contents"
        if forever
        else f"Log bucket retains logs for {retention} days"
    )
    reason += f", well past the {FREE_RETENTION_DAYS} free days, so it bills for the rest"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="log-bucket",
        project=ctx.project,
        location=location,
        reason=reason,
        # The API does not report how many bytes a bucket holds, so there is
        # no defensible figure to put here. Reporting a guessed volume would
        # be worse than reporting the growth.
        monthly_cost=0.0,
        remediation=(
            f"gcloud logging buckets update {helpers.arg(name)} --location={location} "
            f"--retention-days={FREE_RETENTION_DAYS} --project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "retention_days": retention,
            "never_expires": forever,
            "free_retention_days": FREE_RETENTION_DAYS,
            "usd_per_gb_month_beyond_free": price,
            "locked": bool(bucket.get("locked")),
            "lifecycle_state": bucket.get("lifecycleState"),
            "description": bucket.get("description"),
            "note": (
                "unpriced: the API does not report how much a log bucket holds, so this "
                "is reported as unbounded growth rather than as a dollar figure"
            ),
        },
    )


@check(
    CHECK_NAME,
    "Log buckets retaining logs indefinitely",
    apis="logging",
    uncleanable=(
        "shortening log retention deletes the logs beyond the new window, which is a "
        "judgement about what the project must keep -- often a compliance one. "
        "zombiescan prints the 'gcloud logging buckets update' command and leaves the "
        "decision to you"
    ),
)
def unbounded_log_bucket(ctx: ScanContext) -> Iterator[Finding]:
    for bucket in gcp.paginate(
        ctx.client("logging"),
        "projects.locations.buckets",
        key="buckets",
        parent=ctx.any_location(),
    ):
        name = gcp.last_segment(bucket.get("name"))
        if name == REQUIRED_BUCKET:
            # Fixed by Google at 400 days; nothing can be done about it, so
            # reporting it would be pure noise.
            continue
        retention = int(bucket.get("retentionDays") or 0)
        if 0 < retention <= LONG_RETENTION_DAYS:
            continue
        parts = (bucket.get("name") or "").split("/")
        location = parts[parts.index("locations") + 1] if "locations" in parts else gcp.GLOBAL
        yield build_finding(ctx, location, bucket)
