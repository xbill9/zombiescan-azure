"""Unattached Persistent Disks.

A disk with no ``users`` is attached to no instance. You are billed for every
provisioned GB of it anyway. This is the single most common form of Google
Cloud waste and usually the largest by dollar value -- deleting a VM in the
console leaves its data disks behind by default.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.cleaners import backup_name
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unattached-disk"

# Hyperdisk bills provisioned IOPS and throughput as separate line items on
# top of capacity, so a capacity-only figure understates it.
_EXTRA_BILLED_PREFIXES = ("hyperdisk-",)


def build_finding(ctx: ScanContext, location: str, disk: dict[str, Any]) -> Finding:
    name = disk["name"]
    size_gb = helpers.gb(disk.get("sizeGb"))
    disk_type = helpers.disk_variant(disk)
    price, approximate = ctx.pricing.rate(
        "disk.gb_month", region=gcp.region_of(location), variant=disk_type
    )
    age = helpers.age_days(disk.get("creationTimestamp"))

    reason = f"{size_gb:g} GB {disk_type} disk attached to no instance"
    if age is not None:
        reason += f"; created {age} days ago"

    details: dict[str, Any] = {
        "size_gb": size_gb,
        "disk_type": disk_type,
        "age_days": age,
        "description": disk.get("description"),
        "labels": disk.get("labels") or {},
        "source_image": gcp.last_segment(disk.get("sourceImage")) or None,
        "usd_per_gb_month": price,
    }
    if disk_type.startswith(_EXTRA_BILLED_PREFIXES):
        details["note"] = "capacity cost only; provisioned IOPS and throughput not included"

    snapshot = backup_name(name)
    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="compute-disk",
        project=ctx.project,
        location=location,
        reason=reason,
        monthly_cost=size_gb * price,
        remediation=(
            helpers.gcloud(
                f"gcloud compute disks snapshot {helpers.arg(name)} --snapshot-names={snapshot}",
                ctx.project,
                location,
            )
            + " && "
            + helpers.gcloud(
                f"gcloud compute disks delete {helpers.arg(name)}", ctx.project, location
            )
        ),
        approximate_cost=approximate,
        details=details,
    )


@check(CHECK_NAME, "Unattached Persistent Disks", apis="compute")
def unattached_disk(ctx: ScanContext) -> Iterator[Finding]:
    # aggregatedList covers every zone and region in one call, so there is no
    # per-zone fan-out and no zone can be missed by not being asked about.
    for scope, disk in gcp.aggregated(ctx.client("compute"), "disks", "disks", project=ctx.project):
        if disk.get("users"):
            continue
        yield build_finding(ctx, gcp.location_from_scope(scope), disk)
