"""Dedicated hosts with no virtual machine on them.

A dedicated host is a whole physical server reserved for one subscription,
and Azure bills it per host by the hour from the moment it is provisioned,
**regardless of how many VMs are deployed** -- the VMs on it show on the
statement at a price of zero. So a host with nothing on it costs exactly what
a full one does, which is several dollars an hour: an idle ``DSv3-Type3`` is
about $3,000 a month.

Hosts outlive their workloads because deleting a VM leaves the host behind,
and a host group created for a compliance project is easy to forget once the
project moves on.

The price is the host's own hourly rate. Windows Server and SQL Server
licences are billed separately from the host, and are not included.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-dedicated-host"

GROUP_TYPE = "Microsoft.Compute/hostGroups"
RESOURCE_TYPE = "Microsoft.Compute/hostGroups/hosts"


def build_finding(ctx: ScanContext, host: dict[str, Any]) -> Finding:
    name = host["name"]
    arm_id = host.get("id") or ""
    group = host.get("resourceGroup") or azure.resource_group_of(arm_id)
    host_group = azure.name_of(arm_id.rsplit("/hosts/", 1)[0])
    location = helpers.location_of(host)
    properties = helpers.properties(host)
    sku = (host.get("sku") or {}).get("name") or ""

    cost, approximate = ctx.pricing.rate(
        "dedicated_host.month", region=azure.region_of(location), variant=helpers.host_sku_key(sku)
    )
    age = helpers.age_days(properties.get("provisioningTime"))

    reason = f"{sku or 'Dedicated'} host runs no VMs and is billed per host regardless"
    if age is not None:
        reason += f"; provisioned {age} days ago"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="dedicated-host",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        remediation=helpers.az(
            f"az vm host delete --host-group {helpers.arg(host_group)} --name {helpers.arg(name)}",
            ctx.subscription,
            group,
        ),
        approximate_cost=approximate,
        details={
            "sku": sku,
            "host_group": host_group,
            "platform_fault_domain": properties.get("platformFaultDomain"),
            "license_type": properties.get("licenseType"),
            "age_days": age,
            "tags": host.get("tags") or {},
            "note": (
                "the host's hourly rate only; Windows Server and SQL Server licences "
                "are billed separately and are not included"
            ),
        },
    )


@check(CHECK_NAME, "Dedicated hosts running no VMs", providers="Microsoft.Compute")
def idle_dedicated_host(ctx: ScanContext) -> Iterator[Finding]:
    # Hosts are listed per host group; there is no subscription-wide list.
    # A group that refuses the call raises rather than being skipped, because
    # skipping it would report its hosts -- the most expensive finding this
    # tool makes -- as absent.
    for host_group in ctx.list(GROUP_TYPE):
        group_id = host_group.get("id")
        if not group_id:
            continue
        for host in ctx.arm.list(f"{group_id}/hosts", RESOURCE_TYPE):
            if not helpers.properties(host).get("virtualMachines"):
                yield build_finding(ctx, host)
