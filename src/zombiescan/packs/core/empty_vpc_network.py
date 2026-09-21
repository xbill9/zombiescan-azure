"""VPC networks with nothing running in them.

A VPC costs nothing by itself. It is reported because an empty VPC is where
the things that *do* cost money hide: a Cloud NAT nobody remembers, a reserved
address, a subnet holding a range someone needs back. Flagging the network
gives those a home in the report rather than leaving them as loose findings.

The cost carried here is the sum of the priced waste found inside it, so a
network is only expensive when the things in it are.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from zombiescan import gcp, helpers
from zombiescan.models import Finding, ScanContext
from zombiescan.registry import check

CHECK_NAME = "empty-vpc-network"

# The default network every new project gets. It ships with a permissive set
# of firewall rules and nobody chose it, which makes it the most common empty
# VPC by a wide margin -- and the one most worth deleting.
DEFAULT_NETWORK = "default"


def build_finding(
    ctx: ScanContext,
    network: dict[str, Any],
    contents: dict[str, Any],
    cost: float,
    approximate: bool = False,
) -> Finding:
    name = network["name"]
    inventory = ", ".join(f"{count} {kind}" for kind, count in contents.items() if count) or "none"

    reason = f"VPC network runs no instances; it still holds {inventory}"
    if name == DEFAULT_NETWORK:
        reason += ". This is the auto-created default network"

    return Finding(
        check=CHECK_NAME,
        resource_id=name,
        resource_type="vpc-network",
        project=ctx.project,
        location=gcp.GLOBAL,
        reason=reason,
        monthly_cost=cost,
        # A cost of zero is exact -- the network holds nothing priced -- so
        # only a non-zero figure inherits the rate lookup's uncertainty.
        approximate_cost=approximate and bool(cost),
        # Deleting a network requires its contents to go first, so the
        # generated command is the last step rather than the whole job. The
        # cleaner plans the dependencies in order.
        remediation=(
            f"gcloud compute networks delete {helpers.arg(name)} --project={ctx.project} --quiet"
        ),
        details={
            "is_default_network": name == DEFAULT_NETWORK,
            "auto_create_subnetworks": bool(network.get("autoCreateSubnetworks")),
            "contents": contents,
            "note": "cost is the priced waste inside this network, not the network itself",
        },
    )


@check(
    CHECK_NAME,
    "VPC networks with nothing running in them",
    apis="compute",
    uncleanable=(
        "a network cannot be deleted until everything inside it is gone -- subnets, "
        "routes, firewall rules, Cloud Routers, peerings and any Private Service "
        "Connect attachment -- and the order depends on what else references them. "
        "Clean the findings inside the network first; this one goes away with them"
    ),
)
def empty_vpc_network(ctx: ScanContext) -> Iterator[Finding]:
    client = ctx.client("compute")
    populated = helpers.instances_by_network(ctx)

    routers: dict[str, int] = {}
    nat_addresses: dict[str, int] = {}
    for _scope, router in gcp.aggregated(client, "routers", "routers", project=ctx.project):
        network = gcp.last_segment(router.get("network"))
        routers[network] = routers.get(network, 0) + 1
        for nat in router.get("nats") or []:
            nat_addresses[network] = nat_addresses.get(network, 0) + len(nat.get("natIps") or [])

    subnets: dict[str, int] = {}
    for _scope, subnet in gcp.aggregated(client, "subnetworks", "subnetworks", project=ctx.project):
        network = gcp.last_segment(subnet.get("network"))
        subnets[network] = subnets.get(network, 0) + 1

    nat_ip_price, approximate = ctx.pricing.rate("nat_ip.month")

    for network in gcp.paginate(client, "networks", project=ctx.project):
        name = network["name"]
        if populated.get(name, 0):
            continue
        contents = {
            "subnets": subnets.get(name, 0),
            "cloud routers": routers.get(name, 0),
            "reserved NAT addresses": nat_addresses.get(name, 0),
        }
        cost = nat_addresses.get(name, 0) * nat_ip_price
        yield build_finding(ctx, network, contents, cost, approximate)
