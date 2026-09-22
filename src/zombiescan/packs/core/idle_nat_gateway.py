"""NAT gateways with no subnet behind them.

**An idle Azure NAT Gateway is expensive, and that is the reverse of the
Google Cloud answer.** Cloud NAT bills gateway uptime per VM using it, so a
gateway with nothing behind it costs almost nothing. Azure bills a NAT Gateway
a flat hourly fee the way an AWS NAT gateway does -- about $32.85 a month --
from the moment it exists until it is deleted, whether one subnet is attached
or none.

So a NAT Gateway left behind after its subnets were re-plumbed is one of the
larger single line items a small subscription can carry, and nothing about it
looks busy in the portal.

The addresses it holds are billed on top, and are reported separately by
``unused-public-ip`` once the gateway is gone.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-nat-gateway"

RESOURCE_TYPE = "Microsoft.Network/natGateways"


def build_finding(ctx: ScanContext, gateway: dict[str, Any]) -> Finding:
    name = gateway["name"]
    arm_id = gateway.get("id") or ""
    group = gateway.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(gateway)
    properties = helpers.properties(gateway)

    price, approximate = ctx.pricing.rate("nat_gateway.month")
    addresses = properties.get("publicIpAddresses") or []
    prefixes = properties.get("publicIpPrefixes") or []

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="nat-gateway",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=(
            "NAT gateway is attached to no subnet, and Azure charges its hourly fee "
            "whether or not anything routes through it"
        ),
        monthly_cost=price,
        remediation=helpers.az(
            f"az network nat gateway delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate,
        details={
            "sku": (gateway.get("sku") or {}).get("name"),
            "idle_timeout_minutes": properties.get("idleTimeoutInMinutes"),
            "public_ip_addresses": [azure.name_of(a.get("id")) for a in addresses],
            "public_ip_prefixes": [azure.name_of(p.get("id")) for p in prefixes],
            "zones": gateway.get("zones") or [],
            "tags": gateway.get("tags") or {},
            "note": (
                f"gateway uptime only. The {len(addresses)} address(es) it holds are "
                "billed separately and are reported by unused-public-ip once this is gone"
            ),
        },
    )


@check(CHECK_NAME, "NAT gateways with no subnets", providers="Microsoft.Network")
def idle_nat_gateway(ctx: ScanContext) -> Iterator[Finding]:
    for gateway in ctx.list(RESOURCE_TYPE):
        if not (helpers.properties(gateway).get("subnets") or []):
            yield build_finding(ctx, gateway)
