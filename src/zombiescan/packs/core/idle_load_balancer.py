"""Load balancers with no backend to send traffic to.

A Standard load balancer bills a flat hourly rate covering its first five
rules -- about $18.25 a month -- plus data processed. That rate is charged
whether or not the backend pool behind the rule has a single member in it, so
a load balancer whose VMs were all deleted keeps billing for nothing.

Its frontend public IPs bill on top, and because each points its
``ipConfiguration`` at the load balancer, ``unused-public-ip`` counts them as
in use -- so this finding carries their cost.

Basic load balancers are free and are reported at no cost. They retired in
September 2025, so one that is still here is a migration rather than a bill.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import azure, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-load-balancer"

RESOURCE_TYPE = "Microsoft.Network/loadBalancers"


def _pool_members(pool: dict[str, Any]) -> int:
    """How many things are actually in one backend address pool.

    A pool is populated either by NIC IP configurations -- the classic way --
    or by explicit addresses against a virtual network, which is how a pool
    backed by scale sets or on-premises endpoints looks. A pool with neither
    cannot serve a request.
    """
    properties = helpers.properties(pool)
    return len(properties.get("backendIPConfigurations") or []) + len(
        properties.get("loadBalancerBackendAddresses") or []
    )


def _frontend_public_ips(balancer: dict[str, Any]) -> list[str]:
    ids = []
    for frontend in helpers.properties(balancer).get("frontendIPConfigurations") or []:
        public = (helpers.properties(frontend).get("publicIPAddress") or {}).get("id")
        if public:
            ids.append(public)
    return ids


def build_finding(
    ctx: ScanContext,
    balancer: dict[str, Any],
    why: str,
    addresses: dict[str, dict[str, Any]],
) -> Finding:
    name = balancer["name"]
    arm_id = balancer.get("id") or ""
    group = balancer.get("resourceGroup") or azure.resource_group_of(arm_id)
    location = helpers.location_of(balancer)
    properties = helpers.properties(balancer)

    sku = (balancer.get("sku") or {}).get("name") or "Basic"
    # Only the Standard SKU carries an hourly charge. A Basic load balancer is
    # free, so reporting it at the Standard rate would be an invented cost.
    price, approximate = ctx.pricing.rate("load_balancer.month")
    cost = price if sku != "Basic" else 0.0
    ip_cost, ip_approximate, frontend_ips = helpers.held_public_ips(
        ctx, _frontend_public_ips(balancer), addresses
    )

    reason = f"{sku} load balancer {why}"
    if sku == "Basic":
        reason += ". Basic load balancers are free but retired, so this is a migration"
    else:
        reason += ", and its hourly rule charge is billed regardless"
    if ip_cost:
        reason += f"; its {len(frontend_ips)} frontend public IP(s) add ${ip_cost:,.2f}/month"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="load-balancer",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost + ip_cost,
        remediation=helpers.az(
            f"az network lb delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=(approximate and bool(cost)) or ip_approximate,
        details={
            "sku": sku,
            "backend_pools": [
                {"name": pool.get("name"), "members": _pool_members(pool)}
                for pool in properties.get("backendAddressPools") or []
            ],
            "load_balancing_rules": len(properties.get("loadBalancingRules") or []),
            "frontend_ip_configurations": len(properties.get("frontendIPConfigurations") or []),
            "public_ips": frontend_ips,
            "tags": balancer.get("tags") or {},
            "note": (
                "rule charge plus frontend public IPs; data processed is not included. "
                "Deleting the load balancer leaves its public IPs behind, and "
                "unused-public-ip reports them on the next scan"
            ),
        },
    )


@check(CHECK_NAME, "Load balancers with no backends", providers="Microsoft.Network")
def idle_load_balancer(ctx: ScanContext) -> Iterator[Finding]:
    idle: list[tuple[dict[str, Any], str]] = []
    for balancer in ctx.list(RESOURCE_TYPE):
        properties = helpers.properties(balancer)
        pools = properties.get("backendAddressPools") or []
        if not pools:
            idle.append((balancer, "has no backend pool at all"))
        elif all(_pool_members(pool) == 0 for pool in pools):
            idle.append((balancer, f"has {len(pools)} backend pool(s) and nothing in any of them"))
    if not idle:
        return
    addresses = helpers.public_ips_by_id(ctx)
    for balancer, why in idle:
        yield build_finding(ctx, balancer, why, addresses)
