"""Disk snapshots whose source disk is gone.

A snapshot outlives the disk it was taken from, which is the point of it. But
a snapshot of a disk that no longer exists is usually a leftover rather than a
backup -- nobody is going to restore a disk they deliberately deleted a year
ago -- and it bills for its stored bytes forever.

Snapshots are global resources in Google Cloud, so there is one list call for
the whole project. Their storage is billed at the rate of the region they are
stored in, which ``storageLocations`` names.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "orphaned-snapshot"


def _storage_region(snapshot: dict[str, Any]) -> str:
    """Where the snapshot's bytes sit, which is what prices it.

    ``storageLocations`` holds either a region ("us-central1") or a multi-region
    ("us"). A multi-region has no entry in the price table, so it falls through
    to the default region and the finding is marked approximate.
    """
    locations = snapshot.get("storageLocations") or []
    return locations[0] if locations else gcp.GLOBAL


def build_finding(ctx: ScanContext, snapshot: dict[str, Any]) -> Finding:
    name = snapshot["name"]
    # storageBytes is what Google actually bills, and is smaller than the
    # source disk because snapshots are compressed and incremental.
    size_gb = helpers.bytes_to_gb(snapshot.get("storageBytes"))
    region = _storage_region(snapshot)
    price, approximate = ctx.pricing.rate("snapshot.gb_month", region=gcp.region_of(region))
    age = helpers.age_days(snapshot.get("creationTimestamp"))
    source = gcp.last_segment(snapshot.get("sourceDisk"))

    reason = f"Snapshot of {source or 'a disk'} that no longer exists"
    if age is not None:
        reason += f"; taken {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="compute-snapshot",
        project=ctx.project,
        location=region,
        reason=reason,
        monthly_cost=size_gb * price,
        remediation=(
            f"gcloud compute snapshots delete {helpers.arg(name)} --project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "stored_gb": round(size_gb, 2),
            "disk_size_gb": helpers.gb(snapshot.get("diskSizeGb")),
            "source_disk": source or None,
            "source_disk_id": snapshot.get("sourceDiskId"),
            "age_days": age,
            "storage_locations": snapshot.get("storageLocations") or [],
            "labels": snapshot.get("labels") or {},
            "usd_per_gb_month": price,
        },
    )


@check(CHECK_NAME, "Snapshots of deleted disks", apis="compute")
def orphaned_snapshot(ctx: ScanContext) -> Iterator[Finding]:
    client = ctx.client("compute")
    # Every disk that currently exists, by the id its snapshots recorded.
    # Matching on id rather than on name matters: a disk deleted and recreated
    # under the same name is a different disk, and the old snapshots really
    # are orphaned.
    live_disk_ids = {
        disk["id"]
        for _scope, disk in gcp.aggregated(client, "disks", "disks", project=ctx.project)
        if disk.get("id")
    }

    for snapshot in gcp.paginate(client, "snapshots", project=ctx.project):
        source_id = snapshot.get("sourceDiskId")
        if not source_id:
            # No recorded source at all: an imported or manually created
            # snapshot. Nothing says it is orphaned, so leave it alone.
            continue
        if source_id in live_disk_ids:
            continue
        yield build_finding(ctx, snapshot)
