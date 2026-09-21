"""Secret Manager secrets nothing has read in a long time.

Google bills per active secret *version* per month, not per secret, so a
secret rotated weekly and never pruned costs a multiple of one that is not.
The rotation is usually the point; the versions piling up behind it are not.

A secret is reported when every one of its enabled versions is older than the
staleness window. Access times are not exposed by the API, so age is the
available evidence and the finding says so rather than claiming the secret is
unread.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "stale-secret"

# Versions older than this with nothing newer behind them are the ones worth
# asking about. Ninety days is long enough that a quarterly rotation is not
# flagged the week before it runs.
STALE_AFTER_DAYS = 90

ENABLED = "ENABLED"


def build_finding(
    ctx: ScanContext, secret: dict[str, Any], versions: list[dict[str, Any]], newest_age: int
) -> Finding:
    name = gcp.last_segment(secret.get("name"))
    price, approximate = ctx.pricing.rate("secret.version_month")
    enabled = [v for v in versions if v.get("state") == ENABLED]
    # A secret replicated to several regions bills per replica, so the
    # multiplier is part of the cost rather than a footnote.
    replicas = max(1, len(_replica_locations(secret)))

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="secret",
        project=ctx.project,
        location=gcp.GLOBAL,
        reason=(
            f"Secret has {len(enabled)} enabled version(s), the newest {newest_age} days old; "
            f"nothing has added a version since"
        ),
        monthly_cost=len(enabled) * replicas * price,
        remediation=f"gcloud secrets delete {helpers.arg(name)} --project={ctx.project} --quiet",
        approximate_cost=approximate,
        details={
            "enabled_versions": len(enabled),
            "total_versions": len(versions),
            "replica_count": replicas,
            "newest_version_age_days": newest_age,
            "labels": secret.get("labels") or {},
            "note": (
                "age of the newest version, not time since last access: Secret Manager "
                "does not report access times through this API"
            ),
        },
    )


def _replica_locations(secret: dict[str, Any]) -> list[str]:
    replication = secret.get("replication") or {}
    if "automatic" in replication:
        return ["automatic"]
    replicas = (replication.get("userManaged") or {}).get("replicas") or []
    return [r.get("location", "") for r in replicas]


@check(CHECK_NAME, "Secrets nothing has updated in 90 days", apis="secretmanager")
def stale_secret(ctx: ScanContext) -> Iterator[Finding]:
    client = ctx.client("secretmanager")
    for secret in gcp.paginate(client, "projects.secrets", key="secrets", parent=ctx.parent):
        name = secret.get("name")
        if not name:
            continue
        versions = list(
            gcp.paginate(client, "projects.secrets.versions", key="versions", parent=name)
        )
        enabled = [v for v in versions if v.get("state") == ENABLED]
        if not enabled:
            # Every version is disabled or destroyed, so there is nothing
            # billing and nothing to report.
            continue
        ages = [helpers.age_days(v.get("createTime")) for v in enabled]
        known = [age for age in ages if age is not None]
        if not known:
            continue
        newest = min(known)
        if newest < STALE_AFTER_DAYS:
            continue
        yield build_finding(ctx, secret, versions, newest)
