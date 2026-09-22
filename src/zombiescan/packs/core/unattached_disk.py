"""Unattached managed disks.

A disk whose ``diskState`` is ``Unattached`` is attached to no virtual
machine. You are billed for it in full anyway. This is the single most common
form of Azure waste and usually the largest by dollar value -- deleting a VM
in the portal leaves its data disks behind unless the box was ticked, and a VM
created before 2020 leaves its OS disk behind too.

**What an unattached disk costs is not proportional to what is on it.** Azure
bills a managed disk by the tier its provisioned size falls into: a 1 GiB
Premium SSD and a 128 GiB one are both a P10 and both cost the same. So a
forgotten 32 GiB Premium disk is billed as a P4 and a 33 GiB one as a P6 --
and the way to save money on one is to delete it, not to shrink it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unattached-disk"

RESOURCE_TYPE = "Microsoft.Compute/disks"

# The state Azure reports for a disk that no VM holds. `ActiveSAS` is a disk
# with a live shared-access signature against it -- something is reading it
# right now -- and `Reserved` belongs to a stopped VM, which the
# deallocated-vm check reports instead so the disk is not counted twice.
UNATTACHED = "Unattached"


def build_finding(ctx: ScanContext, disk: dict[str, Any]) -> Finding:
    name = disk["name"]
    arm_id = disk.get("id") or ""
    group = disk.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(disk)
    properties = helpers.properties(disk)

    size_gb = helpers.gb(properties.get("diskSizeGB"))
    sku = helpers.disk_sku(disk)
    tier = helpers.disk_tier(sku, size_gb)
    cost, approximate = helpers.disk_monthly_cost(ctx, disk, azure.region_of(location))
    age = helpers.age_days(properties.get("timeCreated"))

    billed = f"billed as {tier}" if tier else f"billed per provisioned GiB ({sku})"
    reason = f"{size_gb:g} GiB {sku} disk attached to no VM, {billed}"
    if age is not None:
        reason += f"; created {age} days ago"

    details: dict[str, Any] = {
        "size_gb": size_gb,
        "sku": sku,
        "billed_tier": tier,
        "disk_state": properties.get("diskState"),
        "age_days": age,
        "os_type": properties.get("osType"),
        "tags": disk.get("tags") or {},
        "created_from": (properties.get("creationData") or {}).get("createOption"),
    }
    if tier is None:
        details["note"] = (
            "capacity cost only; provisioned IOPS and throughput are billed separately "
            f"for {sku} and are not included"
        )
    else:
        details["note"] = (
            f"a {sku} disk is billed at its tier, not per GiB: {tier} costs the same "
            "whether the disk is full or empty"
        )

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="managed-disk",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        remediation=helpers.az(
            f"az disk delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate,
        details=details,
    )


@check(CHECK_NAME, "Unattached managed disks", providers="Microsoft.Compute")
def unattached_disk(ctx: ScanContext) -> Iterator[Finding]:
    # One list call covers every resource group and every region, so there is
    # no per-region fan-out and no region can be missed by not being asked
    # about.
    for disk in ctx.list(RESOURCE_TYPE):
        properties = helpers.properties(disk)
        if properties.get("diskState") != UNATTACHED:
            continue
        # A disk can report Unattached while still owned by something that is
        # not a VM -- a snapshot restore in flight, or a disk pool. managedBy
        # names whatever holds it, and a disk with an owner is not waste.
        if properties.get("managedBy") or disk.get("managedBy"):
            continue
        yield build_finding(ctx, disk)
