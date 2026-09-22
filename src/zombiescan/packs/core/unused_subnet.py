"""Subnets with nothing in them.

A subnet costs nothing. It is reported because the address range it holds is
not available to anything else while it exists, and a virtual network cannot
be deleted until every subnet in it is gone. In a peered network an
overlapping range that nobody is using is the reason the next network cannot
be attached.

A subnet is considered empty when it has no IP configurations, no delegation
to a service that manages its own addresses, no private endpoints and no NAT
gateway. Each of those is a way for a subnet to be genuinely in use while
reporting no IP configurations, and treating any of them as empty would
propose deleting a subnet that is serving traffic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-subnet"

RESOURCE_TYPE = "Microsoft.Network/virtualNetworks"

# Azure reserves five addresses in every subnet -- network, gateway, two DNS
# and broadcast -- so a /24 offers 251 usable, not 256. Worth stating in the
# finding, because "251 addresses reserved against nothing" is the number the
# operator can act on.
RESERVED_PER_SUBNET = 5


def _in_use(subnet: dict[str, Any]) -> bool:
    properties = helpers.properties(subnet)
    return bool(
        properties.get("ipConfigurations")
        or properties.get("delegations")
        or properties.get("privateEndpoints")
        or properties.get("natGateway")
        or properties.get("serviceAssociationLinks")
        or properties.get("resourceNavigationLinks")
    )


def _usable(prefix: str) -> int | None:
    """How many addresses a CIDR prefix actually offers, less Azure's five."""
    try:
        bits = int(str(prefix).rsplit("/", 1)[1])
    except (IndexError, ValueError):
        return None
    total = 2 ** (32 - bits) if bits <= 32 else 0
    return max(total - RESERVED_PER_SUBNET, 0)


def build_finding(ctx: ScanContext, network: dict[str, Any], subnet: dict[str, Any]) -> Finding:
    name = subnet["name"]
    arm_id = subnet.get("id") or ""
    group = network.get("resourceGroup") or azure.resource_group_of(network.get("id") or "")
    location = helpers.location_of(network)
    properties = helpers.properties(subnet)

    prefix = properties.get("addressPrefix") or (properties.get("addressPrefixes") or [""])[0]
    usable = _usable(prefix)
    where = f"{usable} usable address(es)" if usable is not None else "its address range"

    return Finding(
        check=CHECK_NAME,
        # A subnet name is unique only inside its virtual network, so the
        # report names both. The ARM id carries the authoritative path.
        resource_id=f"{network['name']}/{name}",
        resource_type="subnet",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            f"Subnet {prefix} holds nothing -- no IP configuration, delegation, private "
            f"endpoint or NAT gateway -- while reserving {where}"
        ),
        monthly_cost=0.0,
        remediation=helpers.az(
            f"az network vnet subnet delete --name {helpers.arg(name)} "
            f"--vnet-name {helpers.arg(network['name'])}",
            ctx.subscription,
            group,
        ),
        details={
            "virtual_network": network["name"],
            "address_prefix": prefix,
            "usable_addresses": usable,
            "network_security_group": azure.name_of(
                (properties.get("networkSecurityGroup") or {}).get("id")
            )
            or None,
            "route_table": azure.name_of((properties.get("routeTable") or {}).get("id")) or None,
            "note": (
                "no charge. Reported because the range is unavailable to anything else "
                "and the virtual network cannot be deleted while it exists"
            ),
        },
    )


@check(CHECK_NAME, "Subnets reserving a range against nothing", providers="Microsoft.Network")
def unused_subnet(ctx: ScanContext) -> Iterator[Finding]:
    for network in ctx.list(RESOURCE_TYPE):
        for subnet in helpers.properties(network).get("subnets") or []:
            # GatewaySubnet and AzureFirewallSubnet are named by Azure and
            # exist to be empty until the gateway lands in them.
            if subnet.get("name") in ("GatewaySubnet", "AzureFirewallSubnet", "AzureBastionSubnet"):
                continue
            if not _in_use(subnet):
                yield build_finding(ctx, network, subnet)
