"""Stopped Compute Engine instances.

A TERMINATED instance costs nothing for its vCPU and memory. Its disks bill in
full, and so does any static IP it still holds, which is why "just stop it"
saves far less than people expect. SUSPENDED is worse: the instance's memory is
written to disk and that saved state bills too.

The finding is priced at the cost of the disks the instance would release, so
it answers the question actually being asked -- what does leaving this here
cost me?
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "stopped-instance"

# TERMINATED is Google's name for a stopped instance. SUSPENDED is a paused
# one, which additionally bills for the saved memory image.
STOPPED_STATES = frozenset({"TERMINATED", "SUSPENDED"})


def _disk_cost(
    ctx: ScanContext, location: str, instance: dict[str, Any], disks_by_name: dict[str, Any]
) -> tuple[float, bool, list[dict[str, Any]]]:
    """What the instance's attached disks cost a month, and what they are.

    Read from the disk list rather than from the instance, because the
    instance's own ``disks`` entries carry neither the size nor the type
    needed to price them.
    """
    total = 0.0
    approximate = False
    attached: list[dict[str, Any]] = []
    for entry in instance.get("disks") or []:
        name = gcp.last_segment(entry.get("source"))
        disk = disks_by_name.get(name)
        if disk is None:
            # A local SSD, or a disk in a zone the list did not cover. It has
            # no independent cost to report.
            continue
        size_gb = helpers.gb(disk.get("sizeGb"))
        disk_type = helpers.disk_variant(disk)
        price, is_approximate = ctx.pricing.rate(
            "disk.gb_month", region=gcp.region_of(location), variant=disk_type
        )
        total += size_gb * price
        approximate = approximate or is_approximate
        attached.append(
            {
                "name": name,
                "size_gb": size_gb,
                "disk_type": disk_type,
                "boot": bool(entry.get("boot")),
                "auto_delete": bool(entry.get("autoDelete")),
            }
        )
    return total, approximate, attached


def build_finding(
    ctx: ScanContext, location: str, instance: dict[str, Any], disks_by_name: dict[str, Any]
) -> Finding:
    name = instance["name"]
    status = instance.get("status", "TERMINATED")
    cost, approximate, attached = _disk_cost(ctx, location, instance, disks_by_name)
    since = helpers.age_days(instance.get("lastStopTimestamp"))

    reason = f"Instance is {status}; its {len(attached)} disk(s) bill in full while it sits there"
    if since is not None:
        reason += f"; stopped {since} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="compute-instance",
        project=ctx.project,
        location=location,
        reason=reason,
        monthly_cost=cost,
        # --keep-disks=all is deliberate: deleting the instance is reversible
        # if the disks survive, and the unattached-disk check will report them
        # next run with a snapshot-first plan of their own.
        remediation=helpers.gcloud(
            f"gcloud compute instances delete {helpers.arg(name)} --keep-disks=all",
            ctx.project,
            location,
        ),
        approximate_cost=approximate,
        details={
            "status": status,
            "machine_type": gcp.last_segment(instance.get("machineType")),
            "stopped_days_ago": since,
            "disks": attached,
            "labels": instance.get("labels") or {},
            "note": (
                "cost is the attached disks only; vCPU and memory are not billed while stopped"
            ),
        },
    )


@check(CHECK_NAME, "Stopped instances still paying for disks", apis="compute")
def stopped_instance(ctx: ScanContext) -> Iterator[Finding]:
    client = ctx.client("compute")
    disks_by_name = {
        disk["name"]: disk
        for _scope, disk in gcp.aggregated(client, "disks", "disks", project=ctx.project)
    }
    for scope, instance in gcp.aggregated(client, "instances", "instances", project=ctx.project):
        if instance.get("status") in STOPPED_STATES:
            yield build_finding(ctx, gcp.location_from_scope(scope), instance, disks_by_name)
