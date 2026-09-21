"""Versioned Cloud Storage buckets with nothing to prune them.

Object versioning keeps every overwrite and every delete as a noncurrent
version, and noncurrent versions bill at the full storage rate of their class.
Versioning with no lifecycle rule to expire those versions is therefore a
bucket that grows forever, and it grows fastest where it is least visible: a
build artifact overwritten nightly keeps a year of copies.

Turning versioning on without a lifecycle rule is a configuration mistake
rather than a leftover resource, which is why the remediation adds a rule
instead of deleting anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unmanaged-gcs-bucket"

# Lifecycle actions that actually remove noncurrent versions. A rule that only
# changes storage class slows the growth; it does not stop it.
_PRUNING_ACTIONS = frozenset({"Delete", "AbortIncompleteMultipartUpload"})


def _prunes_versions(bucket: dict[str, Any]) -> bool:
    for rule in (bucket.get("lifecycle") or {}).get("rule") or []:
        action = (rule.get("action") or {}).get("type")
        condition = rule.get("condition") or {}
        if action not in _PRUNING_ACTIONS:
            continue
        # A rule keyed on numNewerVersions or daysSinceNoncurrentTime is one
        # that targets noncurrent versions specifically.
        if "numNewerVersions" in condition or "daysSinceNoncurrentTime" in condition:
            return True
        if condition.get("isLive") is False:
            return True
    return False


def build_finding(ctx: ScanContext, bucket: dict[str, Any]) -> Finding:
    name = bucket["name"]
    location = (bucket.get("location") or gcp.GLOBAL).lower()
    storage_class = bucket.get("storageClass", "STANDARD")
    price, approximate = ctx.pricing.rate("gcs.gb_month", region=gcp.region_of(location))
    age = helpers.age_days(bucket.get("timeCreated"))

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="gcs-bucket",
        project=ctx.project,
        location=location,
        reason=(
            "Bucket has object versioning on and no lifecycle rule that expires "
            "noncurrent versions, so every overwrite is kept and billed forever"
        ),
        # The list call does not report how many bytes a bucket holds, and
        # asking would mean listing every object in it. Reporting unbounded
        # growth is honest; inventing a size would not be.
        monthly_cost=0.0,
        remediation=(
            f"gcloud storage buckets update gs://{name} "
            f"--lifecycle-file=lifecycle.json --project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "location": location,
            "storage_class": storage_class,
            "versioning_enabled": True,
            "lifecycle_rules": len((bucket.get("lifecycle") or {}).get("rule") or []),
            "age_days": age,
            "usd_per_gb_month": price,
            "note": (
                "unpriced: the bucket listing does not report stored bytes, and counting "
                "them would mean listing every object. Reported as unbounded growth"
            ),
            "suggested_lifecycle_rule": {
                "action": {"type": "Delete"},
                "condition": {"numNewerVersions": 3, "daysSinceNoncurrentTime": 30},
            },
        },
    )


@check(
    CHECK_NAME,
    "Versioned buckets with no lifecycle rule",
    apis="storage",
    uncleanable=(
        "the right lifecycle rule depends on how many versions the bucket must keep and "
        "for how long, which nothing in the API says. zombiescan reports a suggested "
        "rule in the finding's details and leaves applying it to you"
    ),
)
def unmanaged_gcs_bucket(ctx: ScanContext) -> Iterator[Finding]:
    for bucket in gcp.paginate(ctx.client("storage"), "buckets", project=ctx.project):
        if not (bucket.get("versioning") or {}).get("enabled"):
            continue
        if _prunes_versions(bucket):
            continue
        yield build_finding(ctx, bucket)
