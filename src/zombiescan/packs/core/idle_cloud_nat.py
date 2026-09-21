"""Cloud NAT gateways with nothing behind them.

Worth stating plainly, because the AWS instinct is wrong here: Google bills
Cloud NAT gateway uptime *per VM using it*, so a gateway with no VMs behind it
costs nothing for uptime. An AWS NAT gateway bills a flat hourly charge and is
one of the most expensive things to forget; a Cloud NAT is not.

What an idle Cloud NAT does cost is the external addresses it holds. A gateway
configured with manual IPs keeps those addresses reserved and billed for as
long as it exists, so the finding is priced at the addresses, and a gateway
using automatically allocated IPs is reported as hygiene at no cost.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "idle-cloud-nat"


def build_finding(
    ctx: ScanContext, location: str, router: dict[str, Any], nat: dict[str, Any]
) -> Finding:
    router_name = router["name"]
    nat_name = nat["name"]
    network = gcp.last_segment(router.get("network"))
    addresses = [gcp.last_segment(a) for a in (nat.get("natIps") or [])]
    price, approximate = ctx.pricing.rate("nat_ip.month")
    cost = len(addresses) * price

    reason = f"Cloud NAT '{nat_name}' serves network '{network}', which runs no instances"
    if addresses:
        reason += f"; it holds {len(addresses)} reserved address(es)"
    else:
        reason += "; its addresses are auto-allocated, so it bills nothing while idle"

    return Finding(
        check=CHECK_NAME,
        resource_id=f"{router_name}/{nat_name}",
        resource_type="cloud-nat",
        project=ctx.project,
        location=location,
        reason=reason,
        monthly_cost=cost,
        remediation=(
            f"gcloud compute routers nats delete {helpers.arg(nat_name)} --router={router_name} "
            f"--region={location} --project={ctx.project} --quiet"
        ),
        approximate_cost=approximate,
        details={
            "router": router_name,
            "network": network,
            "nat_ip_allocate_option": nat.get("natIpAllocateOption"),
            "reserved_addresses": addresses,
            "source_subnetwork_ip_ranges": nat.get("sourceSubnetworkIpRangesToNat"),
            "note": (
                "gateway uptime is billed per VM using it, so an idle gateway's only "
                "cost is the addresses it reserves"
            ),
        },
    )


@check(CHECK_NAME, "Cloud NAT gateways serving no instances", apis="compute")
def idle_cloud_nat(ctx: ScanContext) -> Iterator[Finding]:
    populated = helpers.instances_by_network(ctx)
    for scope, router in gcp.aggregated(
        ctx.client("compute"), "routers", "routers", project=ctx.project
    ):
        nats = router.get("nats") or []
        if not nats:
            continue
        if populated.get(gcp.last_segment(router.get("network")), 0):
            continue
        for nat in nats:
            yield build_finding(ctx, gcp.location_from_scope(scope), router, nat)
