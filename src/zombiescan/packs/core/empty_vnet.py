"""Virtual networks with nothing running in them.

A virtual network costs nothing by itself. It is reported because an empty
VNet is where the things that *do* cost money hide: a NAT gateway nobody
remembers, a reserved address, a subnet holding a range someone needs back. A
finding on the network gives those a home in the report rather than leaving
them as loose rows.

The cost carried here is the sum of the priced waste found inside it, so a
network is only expensive when the things in it are.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "empty-vnet"

RESOURCE_TYPE = "Microsoft.Network/virtualNetworks"


def build_finding(
    ctx: ScanContext,
    network: dict[str, Any],
    contents: dict[str, int],
    cost: float,
    approximate: bool = False,
) -> Finding:
    name = network["name"]
    arm_id = network.get("id") or ""
    group = network.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(network)
    properties = helpers.properties(network)
    inventory = ", ".join(f"{count} {kind}" for kind, count in contents.items() if count) or "none"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="virtual-network",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"Virtual network has no network interface in any subnet; it still holds {inventory}"
        ),
        monthly_cost=cost,
        # A cost of zero is exact -- the network holds nothing priced -- so
        # only a non-zero figure inherits the rate lookup's uncertainty.
        approximate_cost=approximate and bool(cost),
        # Deleting a network requires its contents to go first, so the
        # generated command is the last step rather than the whole job.
        remediation=helpers.az(
            f"az network vnet delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        details={
            "address_space": (properties.get("addressSpace") or {}).get("addressPrefixes") or [],
            "contents": contents,
            "peerings": [
                peering.get("name") for peering in properties.get("virtualNetworkPeerings") or []
            ],
            "tags": network.get("tags") or {},
            "note": "cost is the priced waste inside this network, not the network itself",
        },
    )


@check(
    CHECK_NAME,
    "Virtual networks with nothing running in them",
    providers="Microsoft.Network",
    uncleanable=(
        "a virtual network cannot be deleted until everything inside it is gone -- "
        "subnets, NAT gateways, private endpoints, peerings and any gateway -- and "
        "the order depends on what else references them. Clean the findings inside "
        "the network first; this one goes away with them"
    ),
)
def empty_vnet(ctx: ScanContext) -> Iterator[Finding]:
    gateways_by_subnet: dict[str, str] = {}
    for gateway in ctx.list("Microsoft.Network/natGateways"):
        for subnet in helpers.properties(gateway).get("subnets") or []:
            target = str(subnet.get("id") or "")
            if target:
                gateways_by_subnet[target.lower()] = gateway["name"]

    nat_price, approximate = ctx.pricing.rate("nat_gateway.month")

    for network in ctx.list(RESOURCE_TYPE):
        subnets = helpers.properties(network).get("subnets") or []
        if any(helpers.properties(subnet).get("ipConfigurations") for subnet in subnets):
            continue

        attached_gateways = {
            gateways_by_subnet[key]
            for subnet in subnets
            if (key := str(subnet.get("id") or "").lower()) in gateways_by_subnet
        }
        endpoints = sum(
            len(helpers.properties(subnet).get("privateEndpoints") or []) for subnet in subnets
        )
        contents = {
            "subnets": len(subnets),
            "NAT gateways": len(attached_gateways),
            "private endpoints": endpoints,
        }
        yield build_finding(ctx, network, contents, len(attached_gateways) * nat_price, approximate)
