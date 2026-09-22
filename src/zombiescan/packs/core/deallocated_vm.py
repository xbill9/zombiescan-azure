"""Virtual machines that are stopped but still holding their disks.

A deallocated VM costs nothing for its vCPU and memory. Its OS disk and every
data disk bill in full, and so does any static public IP it still holds, which
is why "just shut it down" saves far less than people expect.

**Stopped is not deallocated, and the difference is the whole bill.** Shutting
a VM down from inside the guest -- or with ``az vm stop`` -- leaves it in
``VM stopped``, where Azure keeps the compute reserved and keeps charging for
it. Only ``az vm deallocate``, or the portal's Stop button, releases the
hardware and reaches ``VM deallocated``. Both states are reported here,
because a VM nobody is using is waste either way, and a merely stopped one is
the more expensive mistake.

The finding is priced at the cost of the disks the VM would release, so it
answers the question actually being asked -- what does leaving this here cost
me? A VM in ``VM stopped`` also adds its compute, which the reason names.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "deallocated-vm"

RESOURCE_TYPE = "Microsoft.Compute/virtualMachines"

# Azure reports power state as an instance-view status code. Only these two
# mean the VM is not running.
DEALLOCATED = "PowerState/deallocated"
STOPPED = "PowerState/stopped"
IDLE_STATES = (DEALLOCATED, STOPPED)


def _power_state(vm: dict[str, Any]) -> str | None:
    """A VM's power state, from the instance view the Graph query projects.

    The plain list call does not include it: ARM returns power state only
    under ``$expand=instanceView`` or from Resource Graph, which is why this
    check asks Resource Graph rather than listing.
    """
    state = vm.get("powerState")
    if isinstance(state, str) and state:
        return state
    for status in helpers.properties(vm).get("statuses") or []:
        code = status.get("code") or ""
        if str(code).startswith("PowerState/"):
            return code
    return None


def _disk_cost(
    ctx: ScanContext, region: str, vm: dict[str, Any], disks_by_id: dict[str, dict[str, Any]]
) -> tuple[float, bool, list[dict[str, Any]]]:
    """What the VM's attached disks cost a month, and what they are.

    Read from the disk list rather than from the VM, because the VM's own
    storage profile carries neither the provisioned size nor the SKU needed to
    price them.
    """
    total = 0.0
    approximate = False
    attached: list[dict[str, Any]] = []
    profile = helpers.properties(vm).get("storageProfile") or {}
    references = [("os", profile.get("osDisk") or {})]
    references += [("data", entry) for entry in profile.get("dataDisks") or []]

    for role, entry in references:
        managed = (entry.get("managedDisk") or {}).get("id") or ""
        disk = disks_by_id.get(managed.lower())
        if disk is None:
            # An ephemeral OS disk, or one in a subscription this scan does
            # not cover. It has no independent cost to report.
            continue
        cost, is_approximate = helpers.disk_monthly_cost(ctx, disk, region)
        total += cost
        approximate = approximate or is_approximate
        size_gb = helpers.gb(helpers.properties(disk).get("diskSizeGB"))
        sku = helpers.disk_sku(disk)
        attached.append(
            {
                "name": disk.get("name"),
                "size_gb": size_gb,
                "sku": sku,
                "billed_tier": helpers.disk_tier(sku, size_gb),
                "role": role,
                "monthly_cost": round(cost, 2),
            }
        )
    return total, approximate, attached


def build_finding(
    ctx: ScanContext, vm: dict[str, Any], disks_by_id: dict[str, dict[str, Any]]
) -> Finding:
    name = vm["name"]
    arm_id = vm.get("id") or ""
    group = vm.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(vm)
    state = _power_state(vm) or DEALLOCATED

    cost, approximate, attached = _disk_cost(ctx, azure.region_of(location), vm, disks_by_id)
    size = (helpers.properties(vm).get("hardwareProfile") or {}).get("vmSize")

    if state == STOPPED:
        reason = (
            f"VM is stopped but not deallocated, so its compute is still reserved and "
            f"still billed, on top of its {len(attached)} disk(s)"
        )
    else:
        reason = f"VM is deallocated; its {len(attached)} disk(s) bill in full while it sits there"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="virtual-machine",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        # Deleting the VM leaves the disks behind by default, which is what
        # makes this reversible: the unattached-disk check reports them on the
        # next scan with a snapshot-first plan of their own.
        remediation=helpers.az(f"az vm delete --name {helpers.arg(name)}", ctx.subscription, group),
        approximate_cost=approximate,
        details={
            "power_state": state,
            "vm_size": size,
            "disks": attached,
            "tags": vm.get("tags") or {},
            "note": (
                "cost is the attached disks only. A deallocated VM is not billed for "
                "compute; a stopped-but-not-deallocated one is, and that is not "
                "included here because the rate depends on the VM size and its licence"
                if state == STOPPED
                else "cost is the attached disks only; compute is not billed while deallocated"
            ),
        },
    )


@check(CHECK_NAME, "Stopped VMs still paying for disks", providers="Microsoft.Compute")
def deallocated_vm(ctx: ScanContext) -> Iterator[Finding]:
    disks_by_id = {
        str(disk.get("id") or "").lower(): disk
        for disk in ctx.list("Microsoft.Compute/disks")
        if disk.get("id")
    }
    # Power state is not in the plain list call. Resource Graph carries it,
    # and one query covers every resource group at once.
    rows = ctx.graph(
        "Resources | where type =~ 'microsoft.compute/virtualmachines' "
        "| extend powerState = tostring(properties.extended.instanceView.powerState.code) "
        "| project id, name, location, resourceGroup, tags, properties, powerState"
    )
    for vm in rows:
        if _power_state(vm) in IDLE_STATES:
            yield build_finding(ctx, vm, disks_by_id)
