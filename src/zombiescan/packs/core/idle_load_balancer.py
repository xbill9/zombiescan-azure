"""Load balancers with no backend to send traffic to.

A Standard load balancer bills a flat hourly rate covering its first five
rules -- about $18.25 a month -- plus data processed. That rate is charged
whether or not the backend pool behind the rule has a single member in it, so
a load balancer whose VMs were all deleted keeps billing for nothing.

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


def build_finding(ctx: ScanContext, balancer: dict[str, Any], why: str) -> Finding:
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

    reason = f"{sku} load balancer {why}"
    if sku == "Basic":
        reason += ". Basic load balancers are free but retired, so this is a migration"
    else:
        reason += ", and its hourly rule charge is billed regardless"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="load-balancer",
        subscription=ctx.subscription,
        resource_group=group,
        arm_id=arm_id,
        location=location,
        reason=reason,
        monthly_cost=cost,
        remediation=helpers.az(
            f"az network lb delete --name {helpers.arg(name)}", ctx.subscription, group
        ),
        approximate_cost=approximate and bool(cost),
        details={
            "sku": sku,
            "backend_pools": [
                {"name": pool.get("name"), "members": _pool_members(pool)}
                for pool in properties.get("backendAddressPools") or []
            ],
            "load_balancing_rules": len(properties.get("loadBalancingRules") or []),
            "frontend_ip_configurations": len(properties.get("frontendIPConfigurations") or []),
            "tags": balancer.get("tags") or {},
            "note": "included rule charge only; data processed is not included",
        },
    )


@check(CHECK_NAME, "Load balancers with no backends", providers="Microsoft.Network")
def idle_load_balancer(ctx: ScanContext) -> Iterator[Finding]:
    for balancer in ctx.list(RESOURCE_TYPE):
        properties = helpers.properties(balancer)
        pools = properties.get("backendAddressPools") or []
        if not pools:
            yield build_finding(ctx, balancer, "has no backend pool at all")
        elif all(_pool_members(pool) == 0 for pool in pools):
            yield build_finding(
                ctx, balancer, f"has {len(pools)} backend pool(s) and nothing in any of them"
            )
