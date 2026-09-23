"""Static public IP addresses attached to nothing.

A Standard public IP is billed by the hour from the moment it is reserved,
and Azure charges the same rate whether it is attached to a load balancer or
sitting idle. That is the opposite of Google Cloud, where a reserved address
costs *more* idle than in use -- on Azure nothing about the price tells you
the address is doing nothing. What makes it waste is simply that it is still
reserved, and reserved addresses accumulate: deleting a VM or a load balancer
leaves its address behind.

An address held by something that is itself waste -- an orphaned NIC, a
deallocated VM, an idle load balancer -- points its ``ipConfiguration`` at
that holder and is not reported here. The holder's finding carries its cost.

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

RESOURCE_TYPE = helpers.PUBLIC_IPS


def _attached_to(address: dict[str, Any]) -> str | None:
    """What holds this address, or None if nothing does.

    Azure points an address at whatever uses it through one of two fields:
    ``ipConfiguration`` for a NIC, a load balancer frontend or a gateway, and
    ``natGateway`` for a NAT gateway. ``publicIPPrefix`` is not one of them --
    it names the range the address was carved from, not anything using it.
    """
    properties = helpers.properties(address)
    for field in ("ipConfiguration", "natGateway"):
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
    cost, approximate = helpers.public_ip_monthly_cost(ctx, address)
    prefix = azure.name_of((properties.get("publicIPPrefix") or {}).get("id"))

    reason = f"{sku} {allocation.lower()} public IP attached to nothing"
    if prefix:
        reason += (
            f"; carved from prefix {prefix}, which bills for every address in its range "
            "whether or not it is used"
        )
    elif sku == "Basic":
        reason += "; Basic public IPs are retired and must be migrated to Standard"
    elif cost:
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
            "public_ip_prefix": prefix or None,
            "tags": address.get("tags") or {},
            "note": (
                "releasing an address gives it up permanently: Azure will not hand the "
                "same one back, so anything with it in a DNS record or an allow-list "
                "breaks"
                + (
                    f". Priced at $0: prefix {prefix} is billed per address in its range, "
                    "and deleting this address leaves that charge unchanged"
                    if prefix
                    else ""
                )
            ),
        },
    )


@check(CHECK_NAME, "Public IP addresses attached to nothing", providers="Microsoft.Network")
def unused_public_ip(ctx: ScanContext) -> Iterator[Finding]:
    for address in ctx.list(RESOURCE_TYPE):
        if _attached_to(address) is None:
            yield build_finding(ctx, address)
