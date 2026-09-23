"""On-demand capacity reservations holding room for VMs that are not there.

A capacity reservation guarantees that a number of VMs of one size can be
started in one region or zone, and Azure bills it **at the pay-as-you-go rate
of that VM size for every reserved slot, used or not**. Reserve ten
``D2s_v3`` and run six, and the bill shows six VMs plus four unused
reservations, all at the ``D2s_v3`` rate. An unused slot costs what a running
VM does.

Reservations are made for a launch, a failover drill or a seasonal peak, and
nothing reminds anyone to release them afterwards.

**Associated is not allocated.** A VM can be associated with a reservation
while deallocated, and then it occupies nothing: the slot is billed as unused.
So the count of VMs actually using a reservation is read from the group's
instance view, which ARM only returns under ``$expand=instanceView``. Where
that is missing, the associated count stands in -- an upper bound on use, so
the finding can understate the waste but never overstate it.

The price is the Linux compute rate for the size: a reservation carries no
operating system licence. A reserved instance or savings plan can cover an
unused slot too, and this report prices at list.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-capacity-reservation"

GROUP_TYPE = "Microsoft.Compute/capacityReservationGroups"
RESOURCE_TYPE = "Microsoft.Compute/capacityReservationGroups/capacityReservations"


def utilization_by_name(group: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each reservation's ``utilizationInfo``, keyed by lowercased name."""
    view = helpers.properties(group).get("instanceView") or {}
    return {
        str(entry.get("name") or "").lower(): entry.get("utilizationInfo") or {}
        for entry in view.get("capacityReservations") or []
        if entry.get("name")
    }


def usage(reservation: dict[str, Any], utilization: dict[str, Any] | None) -> tuple[int, int, str]:
    """``(slots billed, slots in use, where the in-use count came from)``."""
    sku = reservation.get("sku") or {}
    reserved = int(sku.get("capacity") or 0)
    if utilization:
        billed = int(utilization.get("currentCapacity", reserved) or 0)
        used = len(utilization.get("virtualMachinesAllocated") or [])
        return billed, used, "instance view: VMs allocated"
    associated = helpers.properties(reservation).get("virtualMachinesAssociated") or []
    return reserved, len(associated), "no instance view: VMs associated, an upper bound"


def build_finding(
    ctx: ScanContext, reservation: dict[str, Any], billed: int, used: int, source: str
) -> Finding:
    name = reservation["name"]
    arm_id = reservation.get("id") or ""
    group = reservation.get("resourceGroup") or azure.resource_group_of(arm_id)
    reservation_group = azure.name_of(arm_id.rsplit("/capacityReservations/", 1)[0])
    location = helpers.location_of(reservation)
    size = (reservation.get("sku") or {}).get("name") or ""
    unused = billed - used

    rate, approximate = ctx.pricing.rate("vm.month", region=azure.region_of(location), variant=size)
    cost = rate * unused

    if used:
        reason = (
            f"{unused} of {billed} reserved {size} slot(s) unused, each billed at the "
            f"{size} pay-as-you-go rate"
        )
        command = (
            f"az capacity reservation update --capacity-reservation-group "
            f"{helpers.arg(reservation_group)} --name {helpers.arg(name)} --capacity {used}"
        )
    else:
        reason = (
            f"all {billed} reserved {size} slot(s) unused, each billed at the "
            f"{size} pay-as-you-go rate"
        )
        command = (
            f"az capacity reservation delete --capacity-reservation-group "
            f"{helpers.arg(reservation_group)} --name {helpers.arg(name)}"
        )

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="capacity-reservation",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        remediation=helpers.az(command, ctx.subscription, group),
        approximate_cost=approximate,
        details={
            "vm_size": size,
            "capacity_reservation_group": reservation_group,
            "slots_billed": billed,
            "slots_in_use": used,
            "slots_unused": unused,
            "in_use_from": source,
            "zones": reservation.get("zones") or [],
            "tags": reservation.get("tags") or {},
            "note": (
                "Linux pay-as-you-go compute rate per unused slot. A reserved instance "
                "or savings plan may cover some of it; this is list price"
            ),
        },
    )


@check(CHECK_NAME, "Capacity reservations holding unused slots", providers="Microsoft.Compute")
def unused_capacity_reservation(ctx: ScanContext) -> Iterator[Finding]:
    for listed in ctx.list(GROUP_TYPE):
        group_id = listed.get("id")
        if not group_id:
            continue
        # The subscription-wide list carries no instance view; each group is
        # read once more to get it.
        utilization = utilization_by_name(
            ctx.arm.get(group_id, GROUP_TYPE, **{"$expand": "instanceView"})
        )
        for reservation in ctx.arm.list(f"{group_id}/capacityReservations", RESOURCE_TYPE):
            billed, used, source = usage(
                reservation, utilization.get(str(reservation.get("name") or "").lower())
            )
            if billed > used:
                yield build_finding(ctx, reservation, billed, used, source)
