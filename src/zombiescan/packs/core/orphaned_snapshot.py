"""Snapshots whose source disk no longer exists.

A snapshot outlives the disk it was taken from. That is the point of it -- but
it means a disk deleted six months ago can still be billing for a full copy of
itself, and nothing in the portal's disk view will ever show it.

Snapshot storage is billed on the data actually stored, not on the source
disk's provisioned size, and incremental snapshots after the first store only
the blocks that changed. Neither number is in a list call, so the cost here is
an **upper bound**: the source disk's provisioned size at the cheapest
snapshot rate. The finding says so rather than presenting the ceiling as the
bill.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "orphaned-snapshot"

RESOURCE_TYPE = "Microsoft.Compute/snapshots"


def build_finding(ctx: ScanContext, snapshot: dict[str, Any], source: str) -> Finding:
    name = snapshot["name"]
    arm_id = snapshot.get("id") or ""
    group = snapshot.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(snapshot)
    properties = helpers.properties(snapshot)

    size_gb = helpers.gb(properties.get("diskSizeGB"))
    price, approximate = ctx.pricing.rate("snapshot.gb_month", region=azure.region_of(location))
    age = helpers.age_days(properties.get("timeCreated"))
    incremental = bool(properties.get("incremental"))

    source_name = azure.name_of(source) or "a disk"
    reason = f"{size_gb:g} GiB snapshot of {source_name}, which no longer exists"
    if age is not None:
        reason += f"; taken {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="snapshot",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=size_gb * price,
        remediation=helpers.az(
            f"az snapshot delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate,
        details={
            "size_gb": size_gb,
            "source_disk": source or None,
            "incremental": incremental,
            "age_days": age,
            "sku": (snapshot.get("sku") or {}).get("name"),
            "tags": snapshot.get("tags") or {},
            "usd_per_gb_month": price,
            "note": (
                "upper bound: snapshots bill the data actually stored, and an "
                "incremental snapshot stores only the blocks that changed since the "
                "last one. The list call reports neither, so this prices the source "
                "disk's full provisioned size"
            ),
        },
    )


@check(CHECK_NAME, "Snapshots of deleted disks", providers="Microsoft.Compute")
def orphaned_snapshot(ctx: ScanContext) -> Iterator[Finding]:
    live_disks = {str(disk.get("id") or "").lower() for disk in ctx.list("Microsoft.Compute/disks")}
    for snapshot in ctx.list(RESOURCE_TYPE):
        creation = helpers.properties(snapshot).get("creationData") or {}
        source = creation.get("sourceResourceId") or ""
        # A snapshot built from a blob or an image has no source disk to
        # outlive, so "the source is gone" says nothing about it.
        if not source:
            continue
        if source.lower() in live_disks:
            continue
        yield build_finding(ctx, snapshot, source)
