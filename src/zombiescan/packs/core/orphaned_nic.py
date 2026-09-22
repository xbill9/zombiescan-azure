"""Network interfaces belonging to no virtual machine.

A NIC costs nothing by itself, and this check is here anyway -- because an
orphaned NIC is the thing that makes the *expensive* cleanup fail. Azure
refuses to delete a public IP that a NIC still references, refuses to delete a
subnet a NIC still sits in, and refuses to delete the virtual network above
it. So a single leftover NIC can be the reason a whole network's worth of
billed resources cannot be removed, and the error it produces names the NIC
rather than explaining the chain.

Orphaned NICs are common because deleting a VM in the portal leaves them
behind, and there is no view in the portal that lists them.

There is no Google Cloud equivalent: a Compute Engine network interface is a
property of the instance rather than a resource of its own, so it cannot
outlive one.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "orphaned-nic"

RESOURCE_TYPE = "Microsoft.Network/networkInterfaces"


def _held_by(nic: dict[str, Any]) -> str | None:
    """What owns this NIC, or None if nothing does.

    A NIC belongs to a VM, or backs a private endpoint or a private link
    service. The last two have no ``virtualMachine`` and are emphatically in
    use, so they are checked for by name.
    """
    properties = helpers.properties(nic)
    for field in ("virtualMachine", "privateEndpoint", "privateLinkService"):
        owner = properties.get(field) or {}
        target = owner.get("id") if isinstance(owner, dict) else None
        if target:
            return str(target)
    return None


def _blocks(nic: dict[str, Any]) -> dict[str, Any]:
    """What this NIC is holding on to, and therefore keeping undeletable."""
    addresses: list[str] = []
    subnets: set[str] = set()
    for configuration in helpers.properties(nic).get("ipConfigurations") or []:
        properties = helpers.properties(configuration)
        public = (properties.get("publicIPAddress") or {}).get("id")
        if public:
            addresses.append(azure.name_of(public))
        subnet = (properties.get("subnet") or {}).get("id")
        if subnet:
            subnets.add(subnet)
    return {"public_ips": addresses, "subnets": sorted(subnets)}


def build_finding(ctx: ScanContext, nic: dict[str, Any]) -> Finding:
    name = nic["name"]
    arm_id = nic.get("id") or ""
    group = nic.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(nic)
    properties = helpers.properties(nic)
    blocking = _blocks(nic)

    reason = "Network interface belongs to no VM"
    held = blocking["public_ips"]
    if held:
        reason += (
            f", and holds {len(held)} public IP address(es) that cannot be released until it goes"
        )
    elif blocking["subnets"]:
        reason += ", and keeps its subnet and virtual network from being deleted"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="network-interface",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        # A NIC has no charge of its own. The addresses it pins do, and they
        # are reported by unused-public-ip -- adding them here would count the
        # same dollars twice.
        monthly_cost=0.0,
        remediation=helpers.az(
            f"az network nic delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        details={
            "blocks": blocking,
            "network_security_group": azure.name_of(
                (properties.get("networkSecurityGroup") or {}).get("id")
            )
            or None,
            "accelerated_networking": properties.get("enableAcceleratedNetworking"),
            "tags": nic.get("tags") or {},
            "note": (
                "a NIC is not billed. It is reported because Azure refuses to delete "
                "the public IP, subnet or virtual network it references while it exists"
            ),
        },
    )


@check(CHECK_NAME, "Network interfaces with no VM", providers="Microsoft.Network")
def orphaned_nic(ctx: ScanContext) -> Iterator[Finding]:
    for nic in ctx.list(RESOURCE_TYPE):
        if _held_by(nic) is None:
            yield build_finding(ctx, nic)
