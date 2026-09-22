"""Static public IP addresses attached to nothing.

A Standard public IP is billed by the hour from the moment it is reserved,
and Azure charges the same rate whether it is attached to a load balancer or
sitting idle. That is the opposite of Google Cloud, where a reserved address
costs *more* idle than in use -- on Azure nothing about the price tells you
the address is doing nothing. What makes it waste is simply that it is still
reserved, and reserved addresses accumulate: deleting a VM or a load balancer
leaves its address behind.

Basic-tier addresses are reported too. Basic public IPs retired in September
2025 and any that remain are both waste and a migration Azure will eventually
force.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "unused-public-ip"

RESOURCE_TYPE = "Microsoft.Network/publicIPAddresses"


def _attached_to(address: dict[str, Any]) -> str | None:
    """What holds this address, or None if nothing does.

    Azure points an address at whatever uses it through one of four fields
    depending on the kind of thing: a NIC's IP configuration, a NAT gateway, a
    public IP prefix it was carved from, or a gateway's configuration. An
    address with any of them is in use.
    """
    properties = helpers.properties(address)
    for field in ("ipConfiguration", "natGateway", "publicIPPrefix"):
        reference = properties.get(field) or {}
        target = reference.get("id") if isinstance(reference, dict) else None
        if target:
            return str(target)
    return None


def build_finding(ctx: ScanContext, address: dict[str, Any]) -> Finding:
    name = address["name"]
    arm_id = address.get("id") or ""
    group = address.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(address)
    properties = helpers.properties(address)

    sku = (address.get("sku") or {}).get("name") or "Basic"
    allocation = properties.get("publicIPAllocationMethod") or "Static"
    price, approximate = ctx.pricing.rate("public_ip.month", region=azure.region_of(location))

    # A dynamic Basic address is only billed while it is attached to a running
    # resource, so an unattached one costs nothing -- it is still worth
    # reporting as the migration it will become.
    billed = not (sku == "Basic" and allocation == "Dynamic")
    cost = price if billed else 0.0

    reason = f"{sku} {allocation.lower()} public IP attached to nothing"
    if sku == "Basic":
        reason += "; Basic public IPs are retired and must be migrated to Standard"
    elif billed:
        reason += ", billed at the same hourly rate as an address in use"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="public-ip",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        remediation=helpers.az(
            f"az network public-ip delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate and bool(cost),
        details={
            "sku": sku,
            "allocation_method": allocation,
            "ip_address": properties.get("ipAddress"),
            "version": properties.get("publicIPAddressVersion"),
            "dns_name": (properties.get("dnsSettings") or {}).get("fqdn"),
            "zones": address.get("zones") or [],
            "tags": address.get("tags") or {},
            "note": (
                "releasing an address gives it up permanently: Azure will not hand the "
                "same one back, so anything with it in a DNS record or an allow-list "
                "breaks"
            ),
        },
    )


@check(CHECK_NAME, "Public IP addresses attached to nothing", providers="Microsoft.Network")
def unused_public_ip(ctx: ScanContext) -> Iterator[Finding]:
    for address in ctx.list(RESOURCE_TYPE):
        if _attached_to(address) is None:
            yield build_finding(ctx, address)
